"""
10 倍快进快出策略（纯函数，不碰 API，方便单测）。

版本：2026-11-10 用户确认（综合 3Commas / Cryptohopper / Pionex / Freqtrade 等做法后定）。
2026-11-11 用户决定改成少而精：信号 15 分钟，3/3 全票，ADX>25，加 4 小时趋势过滤，保证金 400 U/单。
2026-11-16 用户决定：去掉 EMA50 方向过滤——短线看波动选方向，不再只顺大趋势做。
反弹本身就是机会：跌多了出现做多信号就做多吃反弹，反弹到压力位上不去再做空。
- 信号周期：15 分钟 K 线收盘时判断；持仓管理每 5 秒轮询（止盈止损 / 保本）。
- 闸门：15 分钟 ADX 必须 >25（没趋势不动）；均线方向、分界线、量能三项必须
  全中（3/3 全票），多空由 15 分钟波动决定（均线/分界线/量能）；止盈目标 < 0.30% 不做；
  冷却与额度由风控器管。
- 止损止盈：按 15 分钟 ATR 算。止损 2 倍 ATR，止盈 3 倍 ATR，盈亏比 1:1.5。
- 手续费地板：止盈目标 < 0.30% 不做（手续费就把利润吃掉了）。
- 保本和出场由 main.py 管：赚到 1 倍风险就把止损移到成本价。

入场按 Post Only 限价单（maker 手续费），止损按市价单；Dry-Run 下按信号价成交。
"""
from config import (
    ADX_MIN,
    FEE_FLOOR_PCT,
    SL_ATR_MULT,
    TP_ATR_MULT,
)


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
             log=None) -> dict | None:
    """
    在一根 15 分钟 K 线收盘时调用。price 是判断时的实时价（不是那根 K 线的收盘价，
    避免入场价滞后）；指标（ATR/ADX/均线/分界线/量能）仍按已收盘 K 线算。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被某道闸门挡住时，如传了 log 就记一条原因，方便查为什么没开单。

    闸门（2026-11-16 用户决定：去掉 EMA50 方向过滤，短线看波动选方向）：
    1. 强度：15 分钟 ADX 必须 >25，否则说明没趋势；
    2. 共振：均线方向、分界线、量能三项必须全中（3/3 全票）；多空由 15 分钟
       波动决定——做多 3/3 就做多，做空 3/3 就做空；
    3. 手续费地板：止盈目标 < 0.30% 不做；
    4. 冷却与额度：由风控器管，不在这里判断。
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
    if adx15 <= ADX_MIN:
        return _blocked(f"没趋势：15 分钟 ADX {adx15:.0f} <= {ADX_MIN}，震荡市不动。")

    # 手续费地板：止盈目标 < 0.30% 不做
    tp_pct = TP_ATR_MULT * atr / price
    if tp_pct < FEE_FLOOR_PCT:
        return _blocked(f"止盈目标只有 {tp_pct*100:.2f}%，低于手续费地板 0.30%，不做。")

    stop_distance = SL_ATR_MULT * atr
    take_distance = TP_ATR_MULT * atr

    votes_long = _votes_long(price, ma_fast, ma_slow, midline, volume, volume_sma)
    votes_short = _votes_short(price, ma_fast, ma_slow, midline, volume, volume_sma)
    if votes_long == 3:
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": price - stop_distance,
            "take_profit": price + take_distance,
            "take_profit_1": price + take_distance,
            "take_profit_2": None,
            "atr": atr,
            "votes": votes_long,
            "reason": f"15m 波动做多 {votes_long}/3 票（ADX {adx15:.0f}/均线多头），"
                      f"止损 {stop_distance:.0f} 点，止盈 {take_distance:.0f} 点",
            "mode": "scalp10x",
        }
    if votes_short == 3:
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": price + stop_distance,
            "take_profit": price - take_distance,
            "take_profit_1": price - take_distance,
            "take_profit_2": None,
            "atr": atr,
            "votes": votes_short,
            "reason": f"15m 波动做空 {votes_short}/3 票（ADX {adx15:.0f}/均线空头），"
                      f"止损 {stop_distance:.0f} 点，止盈 {take_distance:.0f} 点",
            "mode": "scalp10x",
        }
    return _blocked(f"共振不足：做多 {votes_long}/3 票、做空 {votes_short}/3 票（均线/分界线/量能），要 3 票全中。")
