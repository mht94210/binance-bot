"""
10 倍快进快出策略（纯函数，不碰 API，方便单测）。

【剥头皮测试版 2026-11-17，用户决定：先做模拟盘版本测几天，放日本服务器跑】
进场规则和德国实盘版完全一样（三模式按 1 小时 ADX 调配，实时信号，3/3 全票）；
只改出场：
- 止损 3 倍 ATR（放远了，不会被噪音突然打掉；实盘版是 2 倍）
- 止盈不再按 ATR：浮盈扣掉全部手续费，净赚 2U 就平（止盈价按仓位反推，见 risk.scalp_tp_price）
- 手续费地板不启用：止盈本身就是净赚 2U（已扣费），不可能被手续费吃掉。
- 保本和出场由 main.py 管：赚到 1 倍风险就把止损移到成本价（止盈只有约 1.35U 毛利，
  远没到 1 倍风险 10U，保本实际触发不了，留着也不碍事）。

入场按 Post Only 限价单（maker 手续费），止损按市价单；Dry-Run 下按信号价成交。
"""
from config import (
    EMA_TREND_PERIOD,
    OVEREXTENSION_ATR_MULT,
    RANGE_EDGE_FRACTION,
    REGIME_ADX_RANGE,
    REGIME_ADX_TREND,
    SL_ATR_MULT,
    TREND_ADX_MIN,
)
from risk import position_qty, scalp_tp_price


def select_mode(adx1h: float | None) -> str:
    """
    按大趋势选模式（2026-11-16 用户决定：保留全部策略，按大趋势智能调配）。
    1 小时 ADX 是大趋势强度：>=25 强趋势 -> trend（原 6 胜策略思路，前两天大跌时能用上），
    15~25 过渡 -> momentum（当前动量策略，多空来回做），<15 震荡 -> range（反转策略）。
    1 小时 ADX 拿不到时默认 momentum（当前策略，最稳妥的中间态）。
    """
    if adx1h is None:
        return "momentum"
    if adx1h >= REGIME_ADX_TREND:
        return "trend"
    if adx1h < REGIME_ADX_RANGE:
        return "range"
    return "momentum"


def _votes_long(price: float, ma_fast: float | None, ma_slow: float | None,
               midline: float | None, volume: float | None, volume_sma: float | None) -> int:
    """做多方向得票数（0-3）：均线多头、分界线上方、量能高于均量。"""
    votes = 0
    if ma_fast is not None and ma_slow is not None and ma_fast >= ma_slow:
        votes += 1
    if midline is not None and price > midline:
        votes += 1
    if volume is not None and volume_sma is not None and volume > volume_sma:
        votes += 1
    return votes


def _votes_short(price: float, ma_fast: float | None, ma_slow: float | None,
                midline: float | None, volume: float | None, volume_sma: float | None) -> int:
    """做空方向得票数（0-3）：均线空头、分界线下方、量能高于均量。"""
    votes = 0
    if ma_fast is not None and ma_slow is not None and ma_fast < ma_slow:
        votes += 1
    if midline is not None and price < midline:
        votes += 1
    if volume is not None and volume_sma is not None and volume > volume_sma:
        votes += 1
    return votes


def evaluate(price: float, atr: float | None,
             adx15: float | None, ma_fast: float | None, ma_slow: float | None,
             midline: float | None,
             volume: float | None, volume_sma: float | None,
             range_high: float | None = None, range_low: float | None = None,
             mode: str = "momentum", ema_trend: float | None = None,
             log=None) -> dict | None:
    """
    实时判断：每 60 秒用当前未收盘的 15 分钟 K 线调用一次。price 是判断时的实时价
    （不是那根 K 线的收盘价，避免入场价滞后）；指标（ATR/ADX/均线/分界线/量能）
    按含当前未收盘 K 线的 15 分钟 K 线算（2026-11-16 用户决定，解决进场晚）。
    range_high / range_low 是过去 6 小时区间的最高最低价（不传则不过度延伸过滤，
    range 模式也做不了位置确认）。
    mode 是策略模式（select_mode 按 1 小时 ADX 选出）：trend / momentum / range。
    ema_trend 是 15 分钟 EMA50（只 trend 模式用）。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被某道闸门挡住时，如传了 log 就记一条原因，方便查为什么没开单。

    三种模式（2026-11-16 用户决定：保留全部策略，按大趋势智能调配，共用风控，
    只换开单规则）：
    - trend（原 6 胜策略思路）：15 分钟 ADX > 25 才做；方向必须顺 EMA50（现价在
      EMA50 上方只做多、下方只做空）；3/3 票。过度延伸过滤不适用（大趋势就是要追，
      过滤会把大跌里的空单全挡掉）。
    - momentum（当前动量策略）：3/3 票就进，多空由 15 分钟波动决定，ADX 只参考；
      再加过度延伸过滤（跌多了不追空、涨多了不追多）。
    - range（反转策略）：3/3 票 + 位置确认——只在 6 小时区间下 1/4 做多、上 1/4
      做空；再加过度延伸过滤。
    共通：止盈按净赚 2U 算（已扣费，不用手续费地板）；冷却与额度由风控器管，不在这里判断。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    if atr is None or atr <= 0:
        return _blocked("ATR 还没算出来（15 分钟 K 线不够 15 根），等预热。")
    if adx15 is None:
        return _blocked("15 分钟 ADX 还没算出来，等预热。")
    if ma_fast is None or ma_slow is None:
        return _blocked("15 分钟均线还没算出来，等预热。")

    # 剥头皮版没有手续费地板：止盈价是按仓位反推的净赚 2U（已扣进场 maker + 出场 maker
    # 手续费），本身就不可能被手续费吃掉；ATR 再小也不挡信号。
    stop_distance = SL_ATR_MULT * atr

    votes_long = _votes_long(price, ma_fast, ma_slow, midline, volume, volume_sma)
    votes_short = _votes_short(price, ma_fast, ma_slow, midline, volume, volume_sma)

    # ---- 模式专属闸门（2026-11-16 用户决定：保留全部策略，按大趋势智能调配）----
    if mode == "trend":
        # 原 6 胜策略：ADX 门槛 + EMA50 方向过滤；过度延伸过滤不适用（大趋势就是要追，
        # 过滤会把大跌里的空单全挡掉）。
        if adx15 <= TREND_ADX_MIN:
            return _blocked(f"趋势模式：15 分钟 ADX {adx15:.0f} 不到 {TREND_ADX_MIN:.0f}，不做。")
        if ema_trend is None:
            return _blocked("趋势模式：EMA50 还没算出来，等预热。")
        if votes_long == 3 and price <= ema_trend:
            return _blocked("趋势模式：做多 3/3 票但现价在 EMA50 下方（逆势），不做。")
        if votes_short == 3 and price >= ema_trend:
            return _blocked("趋势模式：做空 3/3 票但现价在 EMA50 上方（逆势），不做。")
        mode_label = f"趋势模式（EMA50，15m ADX {adx15:.0f}>{TREND_ADX_MIN:.0f}）"
    elif mode == "range":
        # 反转策略：位置确认——只在 6 小时区间下 1/4 做多、上 1/4 做空。
        if range_high is None or range_low is None:
            return _blocked("反转模式：6 小时区间还没算出来，等预热。")
        edge = RANGE_EDGE_FRACTION * (range_high - range_low)
        if votes_long == 3 and price > range_low + edge:
            return _blocked(f"反转模式：做多 3/3 票但现价不在 6 小时区间下 1/4"
                            f"（离低点 {price - range_low:.0f} 点），不做。")
        if votes_short == 3 and price < range_high - edge:
            return _blocked(f"反转模式：做空 3/3 票但现价不在 6 小时区间上 1/4"
                            f"（离高点 {range_high - price:.0f} 点），不做。")
        mode_label = "反转模式（6h 区间边缘）"
    else:  # momentum
        mode_label = f"动量模式（ADX {adx15:.0f} 仅参考）"

    # 过度延伸过滤（momentum / range 模式；trend 模式不适用——大趋势就是要追）：
    # 跌得越狠信号越强，但过度延伸就是衰竭——上一单信号价 64143.7 就是 10 小时最低点。
    if mode != "trend":
        if votes_short == 3 and range_high is not None:
            drop = range_high - price
            if drop > OVEREXTENSION_ATR_MULT * atr:
                return _blocked(f"跌多了不追空：现价比 6 小时高点跌了 {drop:.0f} 点"
                                f"（{drop / atr:.1f} 倍 ATR），不追空。")
        if votes_long == 3 and range_low is not None:
            rise = price - range_low
            if rise > OVEREXTENSION_ATR_MULT * atr:
                return _blocked(f"涨多了不追多：现价比 6 小时低点涨了 {rise:.0f} 点"
                                f"（{rise / atr:.1f} 倍 ATR），不追多。")

    if votes_long == 3:
        qty = position_qty(price, price - stop_distance)
        if qty <= 0:
            return _blocked("仓位算出来是 0，不做。")
        tp = scalp_tp_price(price, "LONG", qty)
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": price - stop_distance,
            "take_profit": tp,
            "take_profit_1": tp,
            "take_profit_2": None,
            "atr": atr,
            "votes": votes_long,
            "reason": f"{mode_label}做多 {votes_long}/3 票（15m 均线多头），"
                      f"止损 {stop_distance:.0f} 点，止盈净赚 2U（约 {tp - price:.0f} 点）",
            "mode": mode,
        }
    if votes_short == 3:
        qty = position_qty(price, price + stop_distance)
        if qty <= 0:
            return _blocked("仓位算出来是 0，不做。")
        tp = scalp_tp_price(price, "SHORT", qty)
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": price + stop_distance,
            "take_profit": tp,
            "take_profit_1": tp,
            "take_profit_2": None,
            "atr": atr,
            "votes": votes_short,
            "reason": f"{mode_label}做空 {votes_short}/3 票（15m 均线空头），"
                      f"止损 {stop_distance:.0f} 点，止盈净赚 2U（约 {price - tp:.0f} 点）",
            "mode": mode,
        }
    return _blocked(f"共振不足：做多 {votes_long}/3 票、做空 {votes_short}/3 票（均线/分界线/量能），要 3 票全中。")
