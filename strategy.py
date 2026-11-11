"""
震荡版策略（高空低多，均值回归；纯函数，不碰 API，方便单测）。

版本：2026-11-11 用户确认：日本机器人改成震荡版，和德国趋势版（少而精，ADX>30）互补。
- 信号周期：15 分钟 K 线收盘时判断；持仓管理每 5 秒轮询（止盈止损 / 保本）。
- 区间：最近 24 根 15 分钟 K 线（6 小时）的最高价 / 最低价。
- 四道闸门：
  1. 只做震荡：15 分钟 ADX 必须 <25（高于 25 说明有趋势，震荡策略不动，趋势交给德国版）；
  2. 区间不能太窄：宽度 <0.6% 不做（死水里噪音会反复打止损）；
  3. 位置：现价在区间上沿 25% 区域内做空，下沿 25% 区域内做多，中间 50% 不动；
     已突破上沿/下沿（疑似趋势启动）不追；
  4. 手续费地板：止盈目标 < 0.30% 不做。
- 止损止盈：止损放在区间沿外 0.5 倍 ATR；止盈看对侧沿（整段区间），盈亏比约 1:2 到 1:4。
- 冷却与额度由风控器管（震荡信号没有“票数”，不跳过冷却）。
- 保本和出场由 main.py 管：赚到 1 倍风险就把止损移到成本价。

入场按 Post Only 限价单（maker 手续费），止损按市价单；Dry-Run 下按信号价成交。
"""
from config import (
    ADX_MAX,
    EDGE_ZONE,
    FEE_FLOOR_PCT,
    RANGE_MIN_WIDTH_PCT,
    SL_EDGE_ATR_MULT,
)


def evaluate(price: float, atr: float | None, adx15: float | None,
             range_top: float | None, range_bottom: float | None,
             log=None) -> dict | None:
    """
    在一根 15 分钟 K 线收盘时调用。price 是判断时的实时价（不是那根 K 线的收盘价，
    避免入场价滞后）；指标（ATR/ADX/区间）仍按已收盘 K 线算。
    指标缺失（预热期）时直接返回 None，不过滤也不开单。
    返回信号 dict，或 None（无信号）。信号一定带 stop_loss 和 take_profit。
    被某道闸门挡住时，如传了 log 就记一条原因，方便查为什么没开单。
    """
    def _blocked(reason: str):
        if log is not None:
            log(f"信号被挡：{reason}")
        return None

    if atr is None or atr <= 0:
        return _blocked("ATR 还没算出来（15 分钟 K 线不够 15 根），等预热。")
    if adx15 is None:
        return _blocked("15 分钟 ADX 还没算出来，等预热。")
    if range_top is None or range_bottom is None or range_top <= range_bottom:
        return _blocked("区间还没算出来（15 分钟 K 线不够 24 根），等预热。")
    if adx15 >= ADX_MAX:
        return _blocked(f"有趋势：15 分钟 ADX {adx15:.0f} >= {ADX_MAX}，震荡策略不动（趋势交给德国版）。")

    width = range_top - range_bottom
    width_pct = width / price
    if width_pct < RANGE_MIN_WIDTH_PCT:
        return _blocked(f"区间太窄：宽度只有 {width_pct*100:.2f}%（< 0.6%），死水不动。")

    # 已突破区间沿：疑似趋势启动，不追
    if price > range_top:
        return _blocked(f"已突破区间上沿 {range_top:.1f}（现价 {price:.1f}），疑似趋势启动，不追多。")
    if price < range_bottom:
        return _blocked(f"已跌破区间下沿 {range_bottom:.1f}（现价 {price:.1f}），疑似趋势启动，不追空。")

    upper_line = range_bottom + (1 - EDGE_ZONE) * width  # 上沿 25% 区域的下界
    lower_line = range_bottom + EDGE_ZONE * width        # 下沿 25% 区域的上界
    edge_sl = SL_EDGE_ATR_MULT * atr

    if price >= upper_line:
        # 上沿区做空：止损在上沿外 0.5 倍 ATR，止盈看下沿（整段区间）
        stop_loss = range_top + edge_sl
        take_profit = range_bottom
        tp_pct = (price - take_profit) / price
        if tp_pct < FEE_FLOOR_PCT:
            return _blocked(f"止盈目标只有 {tp_pct*100:.2f}%，低于手续费地板 0.30%，不做。")
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "take_profit_1": take_profit,
            "take_profit_2": None,
            "atr": atr,
            "votes": 0,
            "reason": f"震荡做空（区间 {range_bottom:.1f}-{range_top:.1f}，现价在上沿区/ADX {adx15:.0f}），"
                      f"止损 {stop_loss:.1f}，止盈 {take_profit:.1f}",
            "mode": "range",
        }
    if price <= lower_line:
        # 下沿区做多：止损在下沿外 0.5 倍 ATR，止盈看上沿（整段区间）
        stop_loss = range_bottom - edge_sl
        take_profit = range_top
        tp_pct = (take_profit - price) / price
        if tp_pct < FEE_FLOOR_PCT:
            return _blocked(f"止盈目标只有 {tp_pct*100:.2f}%，低于手续费地板 0.30%，不做。")
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "take_profit_1": take_profit,
            "take_profit_2": None,
            "atr": atr,
            "votes": 0,
            "reason": f"震荡做多（区间 {range_bottom:.1f}-{range_top:.1f}，现价在下沿区/ADX {adx15:.0f}），"
                      f"止损 {stop_loss:.1f}，止盈 {take_profit:.1f}",
            "mode": "range",
        }
    return _blocked(f"价格在区间中部（{range_bottom:.1f}-{range_top:.1f}，现价 {price:.1f}），不在上下沿，不做。")
