"""
10 倍快进快出策略（纯函数，不碰 API，方便单测）。

版本：2026-11-10 用户确认（综合 3Commas / Cryptohopper / Pionex / Freqtrade 等做法后定）。
- 信号周期：5 分钟 K 线收盘时判断；1 分钟只用于持仓管理（止盈止损 / 保本）。
- 五道闸门：1 小时 EMA200 定方向（上方只做多、下方只做空）；
  15 分钟 ADX 必须 >23（没趋势不动）；均线方向、分界线、量能 3 选 2 共振；
  止盈目标 < 0.30% 不做；冷却与额度由风控器管。
- 止损止盈：按 ATR 算。止损 2 倍 ATR，止盈 3 倍 ATR，盈亏比 1:1.5。
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


def evaluate(price: float, atr: float | None, ema200_1h: float | None,
             adx15: float | None, ma_fast: float | None, ma_slow: float | None,
             midline: float | None,
             volume: float | None, volume_sma: float | None,
             log=None) -> dict | None:
    """
    在一根 5 分钟 K 线收盘时调用。price 是判断时的实时价（不是那根 K 线的收盘价，
    避免入场价滞后）；指标（ATR/ADX/EMA/均线/分界线/量能）仍按已收盘 K 线算。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被某道闸门挡住时，如传了 log 就记一条原因，方便查为什么没开单。

    五道闸门：
    1. 方向：1 小时 EMA200 上方只做多、下方只做空；
    2. 强度：15 分钟 ADX 必须 >23，否则说明没趋势；
    3. 共振：均线方向、分界线、量能三项里至少两项同意（3 选 2）；
    4. 手续费地板：止盈目标 < 0.30% 不做；
    5. 冷却与额度：由风控器管，不在这里判断。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    if atr is None or atr <= 0:
        return _blocked("ATR 还没算出来（5 分钟 K 线不够 15 根），等预热。")
    if ema200_1h is None:
        return _blocked("1 小时 EMA200 还没算出来（1 小时 K 线不够 200 根），等预热。")
    if adx15 is None:
        return _blocked("15 分钟 ADX 还没算出来，等预热。")
    if ma_fast is None or ma_slow is None:
        return _blocked("5 分钟均线还没算出来，等预热。")
    if adx15 <= ADX_MIN:
        return _blocked(f"没趋势：15 分钟 ADX {adx15:.0f} <= {ADX_MIN}，震荡市不动。")

    # 手续费地板：止盈目标 < 0.30% 不做
    tp_pct = TP_ATR_MULT * atr / price
    if tp_pct < FEE_FLOOR_PCT:
        return _blocked(f"止盈目标只有 {tp_pct*100:.2f}%，低于手续费地板 0.30%，不做。")

    stop_distance = SL_ATR_MULT * atr
    take_distance = TP_ATR_MULT * atr

    if price > ema200_1h:
        votes = _votes_long(price, ma_fast, ma_slow, midline, volume, volume_sma)
        if votes < 2:
            return _blocked(f"做多共振不足：{votes}/3 票（均线/分界线/量能），不到 2 票。")
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": price - stop_distance,
            "take_profit": price + take_distance,
            "take_profit_1": price + take_distance,
            "take_profit_2": None,
            "atr": atr,
            "reason": f"5m 共振做多 {votes}/3 票（EMA200 上方/ADX {adx15:.0f}/均线多头），"
                      f"止损 {stop_distance:.0f} 点，止盈 {take_distance:.0f} 点",
            "mode": "scalp10x",
        }
    if price < ema200_1h:
        votes = _votes_short(price, ma_fast, ma_slow, midline, volume, volume_sma)
        if votes < 2:
            return _blocked(f"做空共振不足：{votes}/3 票（均线/分界线/量能），不到 2 票。")
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": price + stop_distance,
            "take_profit": price - take_distance,
            "take_profit_1": price - take_distance,
            "take_profit_2": None,
            "atr": atr,
            "reason": f"5m 共振做空 {votes}/3 票（EMA200 下方/ADX {adx15:.0f}/均线空头），"
                      f"止损 {stop_distance:.0f} 点，止盈 {take_distance:.0f} 点",
            "mode": "scalp10x",
        }
    return _blocked("现价正好压在 1 小时 EMA200 上，分不清方向，不动。")
