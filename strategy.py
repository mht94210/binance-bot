"""
10 倍快进快出策略（纯函数，不碰 API，方便单测）。

【剥头皮测试版 2026-11-17，用户决定：先做模拟盘版本测几天，放日本服务器跑】
出场：
- 止损 3 倍 ATR（放远了，不会被噪音突然打掉；实盘版是 2 倍）
- 止盈不再按 ATR：浮盈扣掉全部手续费，净赚 5U 就平（止盈价按仓位反推，见 risk.scalp_tp_price）
- 手续费地板不启用：止盈本身就是净赚 5U（已扣费），不可能被手续费吃掉。
- 保本和出场由 main.py 管：赚到 1 倍风险就把止损移到成本价（止盈净赚 5U，
  远没到 1 倍风险约 30-40U，保本实际触发不了，留着也不碍事）。

入场（2026-11-17 用户决定简化：均线定方向 + 2/3 票就开）：
- 方向只看 5 分钟均线：MA5 在 MA20 上面就做多、下面就做空。
- 三个投票（均线/分界线/量能）里 2 票就开（均线定了方向，票数只是确认）。
- 不要 1 小时 ADX 分模式（趋势/动量/震荡全删）、不要 EMA50 方向过滤、
  不要过度延伸过滤、不要 5 分钟 ADX 门槛。
- 原因：三模式 + 3/3 全票太严，200U 版上线 1 小时一单没开（BTC 票不够、ETH ADX 不够），
  而 J1、J2 两单复杂的过滤也没救回来，都是止损走的。赚 2U 就走，不用这么繁琐。
  注意盈亏比：止盈 +2U，止损约 -14U（ETH）/-24U（BTC），赢率要 88%/92% 才打平；
  简化开单 = 开单更多、赢率更低，模拟盘测试，盈亏自见分晓。

入场按 Post Only 限价单（maker 手续费），止损按市价单；Dry-Run 下按信号价成交。
"""
from config import SL_ATR_MULT
from risk import position_qty, scalp_tp_price


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
             ma_fast: float | None, ma_slow: float | None,
             midline: float | None,
             volume: float | None, volume_sma: float | None,
             log=None) -> dict | None:
    """
    实时判断：每 60 秒用当前未收盘的 5 分钟 K 线调用一次。price 是判断时的实时价
    （不是那根 K 线的收盘价，避免入场价滞后）；指标（ATR/均线/分界线/量能）
    按含当前未收盘 K 线的 5 分钟 K 线算（2026-11-16 用户决定，解决进场晚）。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被挡住时，如传了 log 就记一条原因，方便查为什么没开单。

    简化规则（2026-11-17 用户决定：均线定方向 + 2/3 票）：
    - 方向：5 分钟 MA5 在 MA20 上面只做多、下面只做空。
    - 票数：做多/做空方向 2/3 票就进（均线票 + 分界线/量能其中一票）。
    - 没有模式、没有 ADX 门槛、没有 EMA50 过滤、没有过度延伸过滤。
    止盈按净赚 5U 算（已扣费，不用手续费地板）；冷却与额度由风控器管，不在这里判断。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    if atr is None or atr <= 0:
        return _blocked("ATR 还没算出来（5 分钟 K 线不够 15 根），等预热。")
    if ma_fast is None or ma_slow is None:
        return _blocked("5 分钟均线还没算出来，等预热。")

    # 剥头皮版没有手续费地板：止盈价是按仓位反推的净赚 5U（已扣进场 maker + 出场 maker
    # 手续费），本身就不可能被手续费吃掉；ATR 再小也不挡信号。
    stop_distance = SL_ATR_MULT * atr

    votes_long = _votes_long(price, ma_fast, ma_slow, midline, volume, volume_sma)
    votes_short = _votes_short(price, ma_fast, ma_slow, midline, volume, volume_sma)

    # 方向只看均线（MA5 在 MA20 上面只做多、下面只做空），再加一票确认就开。
    if ma_fast >= ma_slow and votes_long >= 2:
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
            "reason": f"均线方向做多 {votes_long}/3 票（5m MA5 在 MA20 上方），"
                      f"止损 {stop_distance:.0f} 点，止盈净赚 5U（约 {tp - price:.0f} 点）",
            "mode": "ma2",
        }
    if ma_fast < ma_slow and votes_short >= 2:
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
            "reason": f"均线方向做空 {votes_short}/3 票（5m MA5 在 MA20 下方），"
                      f"止损 {stop_distance:.0f} 点，止盈净赚 5U（约 {price - tp:.0f} 点）",
            "mode": "ma2",
        }
    return _blocked(f"共振不足：做多 {votes_long}/3 票、做空 {votes_short}/3 票（均线/分界线/量能），要均线方向一致 + 2 票。")
