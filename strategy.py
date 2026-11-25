"""
10 倍快进快出策略（纯函数，不碰 API，方便单测）。

版本：2026-11-10 用户确认（综合 3Commas / Cryptohopper / Pionex / Freqtrade 等做法后定）。
2026-11-11 用户决定改成少而精：信号 15 分钟，3/3 全票，ADX>25，加 4 小时趋势过滤，保证金 400 U/单。
2026-11-16 用户决定：去掉 EMA50 方向过滤——短线看波动选方向，不再只顺大趋势做。
反弹本身就是机会：跌多了出现做多信号就做多吃反弹，反弹到压力位上不去再做空。
2026-11-16 用户决定（解决进场晚）：去掉 ADX 门槛 + 实时信号——不再等 15 分钟 K 线
收盘，每 60 秒用当前未收盘的 K 线判断一次，3/3 票就进，多空来回做。
2026-11-16 用户决定（保留全部策略，按大趋势智能调配）：三种模式共用风控，只换开单规则，
模式由 1 小时 ADX 决定——>=25 强趋势用 trend（原 6 胜策略：EMA50 方向过滤 + ADX 门槛，
前两天大跌时能用上），15~25 过渡用 momentum（当前动量策略），<15 震荡用 range（反转策略，
只在 6 小时区间下 1/4 做多、上 1/4 做空）。
2026-11-17 用户决定（1000 点横盘里动量模式 3/3 票凑不齐、震荡模式够不着）：震荡上限
从 15 提到 25，<25 全走震荡；震荡模式 2/3 票就开，止损 = 区间沿外 1.5 倍 ATR，
止盈 = 对面沿；动量模式只剩 1h ADX 拿不到时的默认兜底。
2026-11-18 用户决定（15 分钟信号反弹抓不住：均线翻得慢，等反弹确认又要等下一个 15 分钟，
机会全错过）：信号周期改成 5 分钟；1h ADX 定模式、止损止盈倍数、固定 200U、
冷却/熔断都不变。
2026-11-20 用户决定（5 单趋势单全亏，3 个空离 EMA50 4.4-5.2 倍 ATR）：趋势进场加 EMA50 距离门——现价离 EMA50 不超过 1 倍 ATR 才进，太远等回踩；回测挡住 3 个空，2 个多不受影响。
- 信号：实时判断（当前未收盘的 5 分钟 K 线，每 60 秒一次）；均线方向、分界线、
  量能三项必须全中（趋势/动量 3/3 全票，震荡 2/3 票），多空由 5 分钟波动决定（均线/分界线/量能）；
  止盈目标 < 0.30% 不做；冷却与额度由风控器管。
- 过度延伸过滤（2026-11-16 用户决定）：做空时现价比 6 小时高点跌超 3 倍 ATR
  就不追空，做多时现价比 6 小时低点涨超 3 倍 ATR 就不追多——信号打在反转点
  是因为跌得越狠信号越强，过度延伸就是衰竭。
- 止损止盈：趋势/动量按 5 分钟 ATR 算，止损 2 倍 ATR，止盈 3 倍 ATR，盈亏比 1:1.5；
  震荡按区间沿算，止损 = 沿外 1.5 倍 ATR，止盈 = 对面沿。
- 手续费地板：止盈目标 < 0.30% 不做（按实际止盈距离算）。
- 保本和出场由 main.py 管：赚到 1 倍风险就把止损移到成本价。

入场按 Post Only 限价单（maker 手续费），止损按市价单；Dry-Run 下按信号价成交。
"""
from config import (
    CROSS_SL_POINTS,
    CROSS_TP_POINTS,
    FEE_FLOOR_PCT,
    OVEREXTENSION_ATR_MULT,
    RANGE_EDGE_FRACTION,
    RANGE_SL_ATR_MULT,
    REGIME_ADX_RANGE,
    REGIME_ADX_TREND,
    SL_ATR_MULT,
    TP_ATR_MULT,
    TREND_ADX_MIN,
    TREND_EMA_MAX_DIST_ATR,
    TREND_MODE_ENABLED,
)


def select_mode(adx1h: float | None) -> str:
    """
    按大趋势选模式（2026-11-16 用户决定：保留全部策略，按大趋势智能调配；
    2026-11-17 用户决定：震荡上限从 15 提到 25，横盘让震荡接管）。
    1 小时 ADX 是大趋势强度：>=25 强趋势 -> trend（原 6 胜策略思路，前两天大跌时能用上），
    <25 震荡 -> range（反转策略）。动量模式只剩 1 小时 ADX 拿不到时的默认兜底。
    1 小时 ADX 拿不到时默认 momentum（当前策略，最稳妥的中间态）。
    2026-11-20 用户决定：趋势模式先停，只做震荡（TREND_MODE_ENABLED=False 时，
    1h ADX>=25 或拿不到 1h ADX 都返回 "paused"，evaluate 直接挡掉、不开单；
    只有 1h ADX<25 才走 range）。
    """
    if adx1h is None:
        return "paused" if not TREND_MODE_ENABLED else "momentum"
    if adx1h >= REGIME_ADX_TREND:
        return "paused" if not TREND_MODE_ENABLED else "trend"
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


def cross_signal(price: float, ma_fast_prev: float | None, ma_slow_prev: float | None,
                 ma_fast_now: float | None, ma_slow_now: float | None,
                 symbol: str, log=None) -> dict | None:
    """
    均线交叉模式（2026-11-20 用户决定：趋势/震荡全部停用，改成均线交叉 + 固定点数止盈止损）。
    只看已收盘 K 线：上一根收盘 MA5 在 MA20 下方、最新收盘 MA5 到 MA20 上方 = 上穿做多；
    反过来 = 下穿做空。没发生交叉就不开单（均线一直多头/空头时不追单，只等下一次交叉）。
    止盈止损是固定点数（BTC 200/1000 点，ETH 6/28 点），不看 ATR、ADX、量能、区间位置。
    反向交叉出现时手里有单不强制平仓，只等止盈/止损出场（冷却与额度由风控器管）。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    if ma_fast_prev is None or ma_slow_prev is None or ma_fast_now is None or ma_slow_now is None:
        return _blocked("均线交叉模式：收盘均线还没算出来（5 分钟 K 线不够），等预热。")
    tp_points = CROSS_TP_POINTS.get(symbol)
    sl_points = CROSS_SL_POINTS.get(symbol)
    if tp_points is None or sl_points is None:
        return _blocked(f"均线交叉模式：{symbol} 没设固定止盈止损点数，不做。")

    if ma_fast_prev <= ma_slow_prev and ma_fast_now > ma_slow_now:
        side = "LONG"
    elif ma_fast_prev >= ma_slow_prev and ma_fast_now < ma_slow_now:
        side = "SHORT"
    else:
        return _blocked("均线交叉模式：5m MA5/MA20 还没交叉，继续等。")

    # 手续费地板：止盈目标 < 0.30% 不做（BTC 200 点约 0.33%、ETH 6 点约 0.34%，刚过线）
    if tp_points / price < FEE_FLOOR_PCT:
        return _blocked(f"均线交叉模式：止盈目标只有 {tp_points / price * 100:.2f}%，"
                        f"低于手续费地板 0.30%，不做。")
    if side == "LONG":
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": price - sl_points,
            "take_profit": price + tp_points,
            "take_profit_1": price + tp_points,
            "take_profit_2": None,
            "atr": None,
            "votes": 1,
            "reason": f"均线交叉模式：5m MA5 上穿 MA20 做多，止盈 {tp_points:.0f} 点、止损 {sl_points:.0f} 点",
            "mode": "cross",
        }
    return {
        "side": "SHORT",
        "entry": price,
        "stop_loss": price + sl_points,
        "take_profit": price - tp_points,
        "take_profit_1": price - tp_points,
        "take_profit_2": None,
        "atr": None,
        "votes": 1,
        "reason": f"均线交叉模式：5m MA5 下穿 MA20 做空，止盈 {tp_points:.0f} 点、止损 {sl_points:.0f} 点",
        "mode": "cross",
    }


def evaluate(price: float, atr: float | None,
             adx15: float | None, ma_fast: float | None, ma_slow: float | None,
             midline: float | None,
             volume: float | None, volume_sma: float | None,
             range_high: float | None = None, range_low: float | None = None,
             mode: str = "momentum", ema_trend: float | None = None,
             log=None, symbol: str | None = None,
             ma_fast_prev: float | None = None, ma_slow_prev: float | None = None) -> dict | None:
    """
    实时判断：每 60 秒用当前未收盘的 5 分钟 K 线调用一次。price 是判断时的实时价
    （不是那根 K 线的收盘价，避免入场价滞后）；指标（ATR/ADX/均线/分界线/量能）
    按含当前未收盘 K 线的 5 分钟 K 线算（2026-11-16 用户决定，解决进场晚）。
    range_high / range_low 是过去 6 小时区间的最高最低价（不传则不过度延伸过滤，
    range 模式也做不了位置确认）。
    mode 是策略模式（select_mode 按 1 小时 ADX 选出）：trend / momentum / range /
    paused（趋势已停时，evaluate 直接挡掉不开单）；cross 是 2026-11-20 用户决定的
    均线交叉模式（main.py 在 CROSS_MODE_ENABLED=True 时直接传 mode="cross"，不经过 select_mode）。
    ema_trend 是 5 分钟 EMA50（只 trend 模式用）。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被某道闸门挡住时，如传了 log 就记一条原因，方便查为什么没开单。

    三种模式（2026-11-16 用户决定：保留全部策略，按大趋势智能调配，共用风控，
    只换开单规则；2026-11-17 用户决定：震荡上限从 15 提到 25，横盘让震荡接管，
    震荡模式 2/3 票就开）：
    - trend（原 6 胜策略思路）：5 分钟 ADX > 25 才做；方向必须顺 EMA50（现价在
      EMA50 上方只做多、下方只做空）；3/3 票；进场离 EMA50 不超过 1 倍 ATR
      （2026-11-20 修追单：离线太远不追，等回踩）。过度延伸过滤不适用（大趋势就是要追，
      过滤会把大跌里的空单全挡掉）。止损 2 倍 ATR、止盈 3 倍 ATR。
    - momentum（当前动量策略）：3/3 票就进，多空由 5 分钟波动决定，ADX 只参考；
      再加过度延伸过滤（跌多了不追空、涨多了不追多）。止损 2 倍 ATR、止盈 3 倍 ATR。
      2026-11-17 起只剩 1h ADX 拿不到时的默认兜底。
    - range（反转策略）：2/3 票 + 位置确认——只在 6 小时区间下 1/4 做多、上 1/4
      做空；再加过度延伸过滤。止损 = 区间沿外 1.5 倍 ATR，止盈 = 对面沿。
    - paused（2026-11-20 用户决定：趋势模式先停）：不开单，等 1h ADX 回落到 25 以下再做震荡。
    共通：手续费地板（止盈目标 < 0.30% 不做，按实际止盈距离算）；冷却与额度由风控器管，不在这里判断。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    # 均线交叉模式（2026-11-20 用户决定）：接管全部信号，趋势/震荡/paused 都不再走；
    # 只看收盘均线交叉 + 固定点数止盈止损。放在 paused 检查前面——交叉模式下不看 1h ADX。
    if mode == "cross":
        if symbol is None:
            return _blocked("均线交叉模式：没传币种，不知道用哪组固定点数。")
        return cross_signal(price, ma_fast_prev, ma_slow_prev, ma_fast, ma_slow,
                            symbol, log=log)

    # 趋势模式已停（2026-11-20 用户决定：只做震荡）：paused 下任何信号都不开，
    # 放在预热检查前面——停用期连指标都不用等，直接写明原因。
    if mode == "paused":
        return _blocked("趋势模式已停（2026-11-20 用户决定：只做震荡），1h ADX 在 25 以上或拿不到，空仓等回落到 25 以下再做震荡。")

    if atr is None or atr <= 0:
        return _blocked("ATR 还没算出来（5 分钟 K 线不够 15 根），等预热。")
    if adx15 is None:
        return _blocked("5 分钟 ADX 还没算出来，等预热。")
    if ma_fast is None or ma_slow is None:
        return _blocked("5 分钟均线还没算出来，等预热。")

    votes_long = _votes_long(price, ma_fast, ma_slow, midline, volume, volume_sma)
    votes_short = _votes_short(price, ma_fast, ma_slow, midline, volume, volume_sma)

    # 震荡模式只要 2/3 票（2026-11-17 用户决定：横盘里 3/3 票凑不齐）；趋势/动量仍要 3/3。
    required = 2 if mode == "range" else 3

    # ---- 模式专属闸门（2026-11-16 用户决定：保留全部策略，按大趋势智能调配）----
    if mode == "trend":
        # 原 6 胜策略：ADX 门槛 + EMA50 方向过滤；过度延伸过滤不适用（大趋势就是要追，
        # 过滤会把大跌里的空单全挡掉）。
        if adx15 <= TREND_ADX_MIN:
            return _blocked(f"趋势模式：5 分钟 ADX {adx15:.0f} 不到 {TREND_ADX_MIN:.0f}，不做。")
        if ema_trend is None:
            return _blocked("趋势模式：EMA50 还没算出来，等预热。")
        if votes_long == 3 and price <= ema_trend:
            return _blocked("趋势模式：做多 3/3 票但现价在 EMA50 下方（逆势），不做。")
        if votes_short == 3 and price >= ema_trend:
            return _blocked("趋势模式：做空 3/3 票但现价在 EMA50 上方（逆势），不做。")
        # 趋势进场只在 EMA50 附近（2026-11-20 用户决定：5 单趋势单全亏，3 个空离 EMA50
        # 4.4-5.2 倍 ATR，都是等确认后追在极端点；离线太远不追，等回踩 EMA50 再进。
        # 回测：#1 5.22 倍、#2 4.77 倍、#3 4.43 倍被挡；#4 0.35 倍、#5 0.55 倍不受影响）。
        if abs(price - ema_trend) > TREND_EMA_MAX_DIST_ATR * atr:
            return _blocked(f"趋势模式：现价离 EMA50 {abs(price - ema_trend):.0f} 点"
                            f"（{abs(price - ema_trend) / atr:.1f} 倍 ATR），太远了，等回踩 EMA50 再进。")
        mode_label = f"趋势模式（EMA50，5m ADX {adx15:.0f}>{TREND_ADX_MIN:.0f}）"
    elif mode == "range":
        # 反转策略：位置确认——只在 6 小时区间下 1/4 做多、上 1/4 做空；2/3 票就开。
        if range_high is None or range_low is None:
            return _blocked("反转模式：6 小时区间还没算出来，等预热。")
        edge = RANGE_EDGE_FRACTION * (range_high - range_low)
        long_ok = votes_long >= required and price <= range_low + edge
        short_ok = votes_short >= required and price >= range_high - edge
        if not long_ok and not short_ok:
            if votes_long >= required or votes_short >= required:
                return _blocked(f"反转模式：有 {max(votes_long, votes_short)}/3 票但现价不在 6 小时区间边缘"
                                f"（离低点 {price - range_low:.0f} 点、离高点 {range_high - price:.0f} 点），不做。")
            return _blocked(f"反转模式：共振不足，做多 {votes_long}/3 票、做空 {votes_short}/3 票，"
                            f"要 {required} 票。")
        # 只留位置确认通过的方向：两边同到 2 票时，边缘位置决定方向
        # （价格不可能同时在区间上下沿）。
        if not long_ok:
            votes_long = 0
        if not short_ok:
            votes_short = 0
        mode_label = "反转模式（2h 区间边缘）"
    else:  # momentum
        mode_label = f"动量模式（ADX {adx15:.0f} 仅参考）"

    # 过度延伸过滤（momentum / range 模式；trend 模式不适用——大趋势就是要追）：
    # 跌得越狠信号越强，但过度延伸就是衰竭——上一单信号价 64143.7 就是 10 小时最低点。
    if mode != "trend":
        if votes_short >= required and range_high is not None:
            drop = range_high - price
            if drop > OVEREXTENSION_ATR_MULT * atr:
                return _blocked(f"跌多了不追空：现价比 6 小时高点跌了 {drop:.0f} 点"
                                f"（{drop / atr:.1f} 倍 ATR），不追空。")
        if votes_long >= required and range_low is not None:
            rise = price - range_low
            if rise > OVEREXTENSION_ATR_MULT * atr:
                return _blocked(f"涨多了不追多：现价比 6 小时低点涨了 {rise:.0f} 点"
                                f"（{rise / atr:.1f} 倍 ATR），不追多。")

    # 止损止盈距离：趋势/动量按 2 倍 / 3 倍 ATR；反转按区间沿算——止损 = 沿外 1.5 倍 ATR，
    # 止盈 = 对面沿（2026-11-17 用户决定：沿外 0.5 倍 ATR 太紧，一根 15 分钟 K 线就能打到）。
    def _distances(side: str) -> tuple[float, float]:
        if mode == "range":
            if side == "LONG":
                return (price - (range_low - RANGE_SL_ATR_MULT * atr),
                        range_high - price)
            return ((range_high + RANGE_SL_ATR_MULT * atr) - price,
                    price - range_low)
        return (SL_ATR_MULT * atr, TP_ATR_MULT * atr)

    def _signal(side: str, votes: int) -> dict | None:
        stop_distance, take_distance = _distances(side)
        # 手续费地板：按实际止盈距离算，止盈目标 < 0.30% 不做
        if take_distance / price < FEE_FLOOR_PCT:
            return _blocked(f"止盈目标只有 {take_distance / price * 100:.2f}%，"
                            f"低于手续费地板 0.30%，不做。")
        if mode == "range":
            sl_note = f"止损沿外 1.5 倍 ATR（{stop_distance:.0f} 点），止盈到对面沿（{take_distance:.0f} 点）"
        else:
            sl_note = f"止损 {stop_distance:.0f} 点，止盈 {take_distance:.0f} 点"
        if side == "LONG":
            return {
                "side": "LONG",
                "entry": price,
                "stop_loss": price - stop_distance,
                "take_profit": price + take_distance,
                "take_profit_1": price + take_distance,
                "take_profit_2": None,
                "atr": atr,
                "votes": votes,
                "reason": f"{mode_label}做多 {votes}/3 票（5m 均线多头），{sl_note}",
                "mode": mode,
            }
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": price + stop_distance,
            "take_profit": price - take_distance,
            "take_profit_1": price - take_distance,
            "take_profit_2": None,
            "atr": atr,
            "votes": votes,
            "reason": f"{mode_label}做空 {votes}/3 票（5m 均线空头），{sl_note}",
            "mode": mode,
        }

    if votes_long >= required:
        return _signal("LONG", votes_long)
    if votes_short >= required:
        return _signal("SHORT", votes_short)
    return _blocked(f"共振不足：做多 {votes_long}/3 票、做空 {votes_short}/3 票（均线/分界线/量能），"
                     f"要 {required} 票。")
