"""
组合自适应策略（纯函数，不碰 API，方便单测）。

市场背景（2026-11-07 09:10 左右，BTC 约 75,905）：
- 昨晚到今早一直在 75,720–76,032 之间徘徊，现价在小区间中间。
- 价格在 200 日线 76,504 下方，空头趋势，做空是顺势，做多是逆势。
- 用户 2026-11-07 陆续确认：改成区间快进快出；一天 30 单；中间 200 点能不能扩大；
  有没有胜率高一些的打法；结合市场把这几个都部署了；有新趋势再由 Muse 自己分析改动；
  原来一天 3 单的趋势打法也加上，按市场情况合理选用。

组合规则（按优先级，趋势优先，大区间其次，小区间最后）：
A. 趋势打法（原来一天 3 单那个，单独计数：趋势单一天最多 3 单）：
   1. 做多：连续 2 个 1 分钟收在 76,600 上方，止损 75,800，止盈 77,700。
   2. 破位做空：连续 2 个 1 分钟收在 75,200 下方，止损 76,000，止盈 74,500 / 73,700。
   3. 顶部受阻做空：冲进 76,500-77,000 又跌回 76,600 下方，止损 77,800，止盈 75,200 / 74,500。
B. 大区间 1000 点（75,200–76,600，强位置，次数少、单笔大）：
   4. 大区间空：上一分钟在 76,400 上方，这一分钟收回 76,400 下方（且还在 76,000 上方），
      止损 76,700，止盈 75,400，目标 1000 点，净赚约 12 U。
   5. 大区间多：上一分钟在 75,400 下方，这一分钟收回 75,400 上方（且还在 75,800 下方），
      止损 75,100，止盈 76,400，目标 1000 点，净赚约 12 U。
C. 小区间 200 点快进快出（75,700–76,050，次数多、单笔小，高胜率过滤版）：
   6. 小区间空：上一分钟在 76,000 上方，这一分钟收回 76,000 下方（且还在 75,800 上方），
      止损 76,200，止盈 75,800。做空是顺势，优先。
   7. 小区间多：上一分钟在 75,800 下方，这一分钟收回 75,800 上方（且还在 76,000 下方），
      止损 75,600，止盈 76,000。做多是逆势，只吃确认后的反弹，不追跌。

高胜率过滤说明：小区间和大区间都用“碰边 + 1 分钟收回来确认”才进，不是一碰就进，
能过滤掉真突破追错方向的情况。代价是入场晚 1 分钟、触发次数比快进快出版少一点。

方向过滤（2026-11-07 用户确认加上，三个问题一次修好）：
1. 冷却：同一方向 10 分钟内只做一次（风控器里管），不叠单、不白交手续费。
2. 分界线跟着市场走：最近 60 分钟最高最低价的中点当分界线，线上只做多、线下只做空。
   75,875 只是 2026-11-07 早上那个区间的中间价，定死它价格一走就失效，所以改成滚动计算。
3. 均线交叉：5 分钟均线向上穿 20 分钟均线（金叉）只做多，向下跌破（死叉）只做空。
   区间单（大小区间）要分界线和均线两个方向一致才进；趋势单是突破驱动的，不受这两个过滤限制。
   启动预热、数据不够时不过滤，攒够数据再按过滤走。
500 点档由小区间 200 点 + 大区间 1000 点共同覆盖：小单高频、大单吃波段，不再单独设 500 点档。
手续费按 Taker 来回约 1 U 算：小区间一单净赚约 1.6 U，盈亏平衡胜率约 69%；
大区间一单净赚约 12 U，盈亏平衡胜率约 29%。胜率看的是期望值，不是单看高低。
其余时间：不追单，等信号。每 1 分钟检查一次（main.py 的循环）。
"""
from config import (
    BIG_RANGE_LONG_ENTRY,
    BIG_RANGE_LONG_STOP,
    BIG_RANGE_LONG_TP,
    BIG_RANGE_SHORT_ENTRY,
    BIG_RANGE_SHORT_STOP,
    BIG_RANGE_SHORT_TP,
    LONG_STOP,
    LONG_TP,
    LONG_TRIGGER,
    RANGE_LONG_ENTRY,
    RANGE_LONG_STOP,
    RANGE_LONG_TP,
    RANGE_SHORT_ENTRY,
    RANGE_SHORT_STOP,
    RANGE_SHORT_TP,
    SHORT_STOP,
    SHORT_TP1,
    SHORT_TP2,
    SHORT_ZONE_HIGH,
    SHORT_ZONE_LOW,
    TREND_SHORT_STOP,
    TREND_SHORT_TP1,
    TREND_SHORT_TP2,
    TREND_SHORT_TRIGGER,
)


def _direction_ok(side: str, price: float, ma_fast: float | None,
                  ma_slow: float | None, midline: float | None) -> bool:
    """区间单的方向过滤：分界线 + 均线两个都要顺着才放行。
    数据不够（预热期）不过滤，返回 True。"""
    if midline is None or ma_fast is None or ma_slow is None:
        return True
    if side == "LONG":
        return price > midline and ma_fast >= ma_slow
    return price < midline and ma_fast < ma_slow


def evaluate(price: float, prev_price: float | None,
             closes_above_long_trigger: int,
             closes_below_trend_short: int = 0,
             ma_fast: float | None = None,
             ma_slow: float | None = None,
             midline: float | None = None) -> dict | None:
    """
    输入现价、上一次现价、连续站稳 76,600 的分钟数、连续收在 75,200 下方的分钟数，
    以及快线、慢线、滚动分界线（没有时传 None，不过滤）。
    返回信号 dict，或 None（无信号）。
    信号一定带 stop_loss 和 take_profit_1，缺一个都不算信号。
    """
    # --- A1. 趋势做多：连续 2 分钟站稳 76,600 ---
    if closes_above_long_trigger >= 2:
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": LONG_STOP,
            "take_profit_1": LONG_TP,
            "take_profit_2": None,
            "reason": f"趋势突破：连续 2 分钟站稳 {LONG_TRIGGER:.0f}，做多",
            "mode": "trend",
        }

    # --- A2. 趋势破位做空：连续 2 分钟收在 75,200 下方 ---
    if closes_below_trend_short >= 2:
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": TREND_SHORT_STOP,
            "take_profit_1": TREND_SHORT_TP1,
            "take_profit_2": TREND_SHORT_TP2,
            "reason": f"趋势破位：连续 2 分钟收在 {TREND_SHORT_TRIGGER:.0f} 下方，做空",
            "mode": "trend",
        }

    if prev_price is None:
        return None

    # --- A3. 顶部受阻做空：进过 76,500-77,000 又跌回 76,600 下方 ---
    was_in_zone = SHORT_ZONE_LOW <= prev_price <= SHORT_ZONE_HIGH
    now_back_below = price < LONG_TRIGGER
    if was_in_zone and now_back_below and price < prev_price:
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": SHORT_STOP,
            "take_profit_1": SHORT_TP1,
            "take_profit_2": SHORT_TP2,
            "reason": f"冲进 {SHORT_ZONE_LOW:.0f}-{SHORT_ZONE_HIGH:.0f} 压力区又跌回 "
                      f"{LONG_TRIGGER:.0f} 下方，做空",
            "mode": "trend",
        }

    # --- B1. 大区间空：碰 76,400 又收回来确认 ---
    if prev_price >= BIG_RANGE_SHORT_ENTRY > price >= 76000.0 and _direction_ok("SHORT", price, ma_fast, ma_slow, midline):
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": BIG_RANGE_SHORT_STOP,
            "take_profit_1": BIG_RANGE_SHORT_TP,
            "take_profit_2": None,
            "reason": f"大区间高抛确认：碰 {BIG_RANGE_SHORT_ENTRY:.0f} 又收回来，做空目标 1000 点",
            "mode": "big_range",
        }

    # --- B2. 大区间多：碰 75,400 又收回来确认 ---
    if prev_price <= BIG_RANGE_LONG_ENTRY < price <= 75800.0 and _direction_ok("LONG", price, ma_fast, ma_slow, midline):
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": BIG_RANGE_LONG_STOP,
            "take_profit_1": BIG_RANGE_LONG_TP,
            "take_profit_2": None,
            "reason": f"大区间低吸确认：碰 {BIG_RANGE_LONG_ENTRY:.0f} 又收回来，做多目标 1000 点",
            "mode": "big_range",
        }

    # --- C1. 小区间空（顺势优先）：碰 76,000 又收回来确认 ---
    if prev_price >= RANGE_SHORT_ENTRY > price >= RANGE_LONG_ENTRY and _direction_ok("SHORT", price, ma_fast, ma_slow, midline):
        return {
            "side": "SHORT",
            "entry": price,
            "stop_loss": RANGE_SHORT_STOP,
            "take_profit_1": RANGE_SHORT_TP,
            "take_profit_2": None,
            "reason": f"小区间高抛确认：碰 {RANGE_SHORT_ENTRY:.0f} 又收回来，做空快进快出",
            "mode": "small_range",
        }

    # --- C2. 小区间多（逆势，只吃确认反弹）：碰 75,800 又收回来确认 ---
    if prev_price <= RANGE_LONG_ENTRY < price <= RANGE_SHORT_ENTRY and _direction_ok("LONG", price, ma_fast, ma_slow, midline):
        return {
            "side": "LONG",
            "entry": price,
            "stop_loss": RANGE_LONG_STOP,
            "take_profit_1": RANGE_LONG_TP,
            "take_profit_2": None,
            "reason": f"小区间低吸确认：碰 {RANGE_LONG_ENTRY:.0f} 又收回来，做多快进快出",
            "mode": "small_range",
        }

    return None
