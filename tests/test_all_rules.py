"""机器人规则回归总测（放在仓库里，虚拟机重启也不丢）。

覆盖用户确认过的全部规则（2026-11-11）：
A) 少而精：3/3 全票、保证金 400U、5m 信号/ATR；2026-11-16 去掉 EMA50 方向过滤，短线看 5 分钟波动选方向；
   2026-11-16 再去掉 ADX 门槛 + 实时信号（当前未收盘 K 线，每 60 秒判断一次），解决进场晚；
   2026-11-16 再加三模式（保留全部策略，按 1h ADX 智能调配：>=25 trend 原 6 胜策略，15~25 momentum，
   <15 range 反转）+ 过度延伸过滤（跌多了不追空、涨多了不追多）
   2026-11-17 用户决定（1000 点横盘里动量 3/3 票凑不齐）：震荡上限 15 -> 25（<25 全走震荡），
   震荡模式 2/3 票就开，止损 = 沿外 1.5 倍 ATR，止盈 = 对面沿；每单固定 200U 保证金
   2026-11-18 用户决定（15 分钟信号反弹抓不住）：信号周期改成 5 分钟；1h ADX 定模式、2 小时区间、
   止损止盈倍数、固定 200U、冷却/熔断都不变；区间回看 24 根 5 分钟 K 线，分界线回看 12 根
   2026-11-19 用户决定（2 小时窗口跟着价格滑、震荡开不出单）：区间窗口 2 小时 -> 6 小时，区间回看 72 根
B) 放宽止损：趋势/动量止损 2 倍 ATR、止盈 3 倍 ATR、盈亏比 1:1.5
C) 冷却跳过：3/3 强信号跳过冷却；连亏暂停/消息禁入/每日熔断是硬刹车
D) 熔断：日熔断 100U；state.json 丢失时收紧到 50U；UTC 午夜恢复 100U
E) 轮询：持仓 5 秒一轮、空仓 60 秒一轮

用法：python3 tests/test_all_rules.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    FLAT_POLL_SECONDS,
    HOLDING_POLL_SECONDS,
    MAX_DAILY_LOSS_USDT,
    MAX_MARGIN_PER_TRADE_USDT,
    MIDLINE_LOOKBACK_CANDLES,
    RANGE_LOOKBACK_CANDLES,
    SIGNAL_INTERVAL,
    SIGNAL_INTERVAL_MINUTES,
    SL_ATR_MULT,
    TP_ATR_MULT,
)
import main
from risk import RiskManager
from strategy import evaluate, select_mode

FAILURES = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def base_kwargs(**over):
    kw = dict(price=2040.0, atr=7.5,
              adx15=35.0, ma_fast=2041.0, ma_slow=2045.0, midline=2046.0,
              volume=100.0, volume_sma=80.0)
    kw.update(over)
    return kw


# ---------- A) 少而精 ----------
def test_votes_3of3_only():
    sig = evaluate(**base_kwargs())
    check(sig is not None and sig["side"] == "SHORT" and sig["votes"] == 3,
          "A 3/3 全票做空信号触发")
    check(evaluate(**base_kwargs(volume=50.0)) is None, "A 2/3 票挡住（量能不足）")
    check(evaluate(**base_kwargs(ma_fast=2046.0, midline=2050.0, volume=50.0)) is None, "A 1/3 票挡住（两边都不足 3/3）")
    sig4 = evaluate(**base_kwargs(price=2070.0,
                                  ma_fast=2065.0, ma_slow=2060.0, midline=2060.0))
    check(sig4 is not None and sig4["side"] == "LONG" and sig4["votes"] == 3,
          "A 3/3 全票做多信号触发")
    sig5 = evaluate(**base_kwargs(price=2070.0,
                                  ma_fast=2065.0, ma_slow=2060.0, midline=2080.0))
    check(sig5 is None, "A 做多 2/3 票挡住（分界线下方）")


def test_no_adx_gate():
    # 2026-11-16 用户决定：去掉 ADX 门槛（解决进场晚：ADX 是最慢的确认，等它上 25 行情已走完）。
    # ADX 只作参考记进信号原因，低 ADX 下 3/3 票照样开单；ADX 缺失（预热期）仍挡住。
    check(evaluate(**base_kwargs(adx15=10.0)) is not None, "A ADX 10 不挡 3/3 票")
    check(evaluate(**base_kwargs(adx15=5.0)) is not None, "A ADX 5 不挡 3/3 票")
    check(evaluate(**base_kwargs(adx15=None)) is None, "A ADX 缺失仍挡住（预热期）")


def test_overextension_filter():
    # 2026-11-16 用户决定：过度延伸过滤，双向。上一单信号价 64143.7 就是 10 小时最低点，
    # 3/3 票 + ADX 29 全是最强看空，但跌得越狠离反弹越近——信号打在底上。
    # 规则：做空时现价比 2 小时高点跌超 3 倍 ATR 就不追空；做多时现价比 2 小时低点
    # 涨超 3 倍 ATR 就不追多。3 倍 ATR = 22.5（atr=7.5）。
    check(evaluate(**base_kwargs(range_high=2070.0)) is None,
          "A 跌多了不追空：比 2h 高点跌 30 点（>22.5）挡住")
    check(evaluate(**base_kwargs(range_high=2050.0)) is not None,
          "A 跌 10 点（<22.5）不挡，做空信号触发")
    check(evaluate(**base_kwargs(range_high=2062.5)) is not None,
          "A 正好 3 倍 ATR（22.5）不挡（只挡超过的）")
    check(evaluate(**base_kwargs(range_high=None)) is not None,
          "A 无 2h 高点数据时不过滤，做空信号触发")
    long_kw = dict(price=2070.0, ma_fast=2065.0, ma_slow=2060.0, midline=2060.0)
    check(evaluate(**base_kwargs(**long_kw, range_low=2040.0)) is None,
          "A 涨多了不追多：比 2h 低点涨 30 点（>22.5）挡住")
    check(evaluate(**base_kwargs(**long_kw, range_low=2060.0)) is not None,
          "A 涨 10 点（<22.5）不挡，做多信号触发")
    check(evaluate(**base_kwargs(**long_kw, range_low=None)) is not None,
          "A 无 2h 低点数据时不过滤，做多信号触发")


def test_select_mode():
    # 2026-11-17 用户决定：震荡上限从 15 提到 25，<25 全走震荡，>=25 走趋势；动量只剩 1h ADX 拿不到时的默认兜底。
    check(select_mode(30.0) == "trend", "模式 1h ADX 30 -> trend")
    check(select_mode(25.0) == "trend", "模式 1h ADX 25（边界）-> trend")
    check(select_mode(20.0) == "range", "模式 1h ADX 20 -> range（震荡上限提到 25）")
    check(select_mode(15.0) == "range", "模式 1h ADX 15 -> range")
    check(select_mode(10.0) == "range", "模式 1h ADX 10 -> range")
    check(select_mode(None) == "momentum", "模式 1h ADX 拿不到 -> 默认 momentum")


def test_trend_mode():
    # 2026-11-16 用户决定：trend 模式 = 原 6 胜策略思路（EMA50 方向过滤 + 5m ADX>25 + 3/3 票）。
    # 过度延伸过滤不适用（大趋势就是要追）。
    check(evaluate(**base_kwargs(mode="trend", ema_trend=2050.0, range_high=2070.0)) is not None,
          "趋势 做空 3/3 + 现价在 EMA50 下方 + ADX 35>25 触发（跌 30 点也不被过度延伸挡）")
    sig = evaluate(**base_kwargs(mode="trend", ema_trend=2050.0))
    check(sig is not None and sig["side"] == "SHORT" and sig["mode"] == "trend",
          "趋势 做空信号 mode 字段是 trend")
    check(evaluate(**base_kwargs(mode="trend", ema_trend=2030.0)) is None,
          "趋势 做空 3/3 但现价在 EMA50 上方（逆势）挡住")
    check(evaluate(**base_kwargs(mode="trend", ema_trend=2050.0, adx15=20.0)) is None,
          "趋势 5m ADX 20 不到 25 挡住")
    check(evaluate(**base_kwargs(mode="trend", ema_trend=None)) is None,
          "趋势 EMA50 没算出来挡住")
    long_kw = dict(price=2070.0, ma_fast=2065.0, ma_slow=2060.0, midline=2060.0)
    check(evaluate(**base_kwargs(**long_kw, mode="trend", ema_trend=2060.0)) is not None,
          "趋势 做多 3/3 + 现价在 EMA50 上方 + ADX 35>25 触发")
    check(evaluate(**base_kwargs(**long_kw, mode="trend", ema_trend=2080.0)) is None,
          "趋势 做多 3/3 但现价在 EMA50 下方（逆势）挡住")


def test_range_mode():
    # 2026-11-17 用户决定：range 模式 = 反转策略（2/3 票 + 只在 6h 区间下 1/4 做多、上 1/4 做空；
    # 止损 = 沿外 1.5 倍 ATR，止盈 = 对面沿）。区间 2030~2070，下 1/4 <= 2040，上 1/4 >= 2060；
    # 1.5 倍 ATR = 11.25。
    rng = dict(range_high=2070.0, range_low=2030.0)
    long_kw = dict(price=2035.0, ma_fast=2045.0, ma_slow=2041.0, midline=2030.0)
    sig = evaluate(**base_kwargs(**long_kw, mode="range", **rng))
    check(sig is not None and sig["side"] == "LONG" and sig["votes"] == 3,
          "反转 做多 3/3 + 现价在区间下 1/4（2035）触发")
    check(abs(sig["stop_loss"] - 2018.75) < 0.01,
          f"反转 做多止损 = 沿外 1.5 倍 ATR：2030-11.25=2018.75（实得 {sig['stop_loss']:.2f}）")
    check(abs(sig["take_profit_1"] - 2070.0) < 0.01,
          f"反转 做多止盈 = 对面沿 2070（实得 {sig['take_profit_1']:.1f}）")
    # 2/3 票也触发：量能不足（volume=50 < 80），均线+分界线两票
    sig2 = evaluate(**base_kwargs(**long_kw, volume=50.0, mode="range", **rng))
    check(sig2 is not None and sig2["side"] == "LONG" and sig2["votes"] == 2,
          "反转 做多 2/3 票触发（量能不足也开）")
    # 1/3 票挡住
    check(evaluate(**base_kwargs(price=2035.0, ma_fast=2045.0, ma_slow=2041.0, midline=2040.0,
                                volume=50.0, mode="range", **rng)) is None,
          "反转 做多 1/3 票挡住")
    check(evaluate(**base_kwargs(price=2050.0, ma_fast=2055.0, ma_slow=2051.0, midline=2040.0,
                                mode="range", **rng)) is None,
          "反转 做多 3/3 但现价在区间中间（2050）挡住")
    short_kw = dict(price=2065.0, ma_fast=2060.0, ma_slow=2065.0, midline=2070.0)
    sig3 = evaluate(**base_kwargs(**short_kw, mode="range", **rng))
    check(sig3 is not None and sig3["side"] == "SHORT",
          "反转 做空 3/3 + 现价在区间上 1/4（2065）触发")
    check(abs(sig3["stop_loss"] - 2081.25) < 0.01,
          f"反转 做空止损 = 沿外 1.5 倍 ATR：2070+11.25=2081.25（实得 {sig3['stop_loss']:.2f}）")
    check(abs(sig3["take_profit_1"] - 2030.0) < 0.01,
          f"反转 做空止盈 = 对面沿 2030（实得 {sig3['take_profit_1']:.1f}）")
    check(evaluate(**base_kwargs(price=2050.0, ma_fast=2045.0, ma_slow=2050.0, midline=2055.0,
                                mode="range", **rng)) is None,
          "反转 做空 3/3 但现价在区间中间（2050）挡住")
    check(evaluate(**base_kwargs(**long_kw, mode="range")) is None,
          "反转 没有 2h 区间数据挡住")
    # 两边同到 2 票时边缘位置决定方向：均线多头 + 分界线下方 + 量能足 = 做多 2 票、做空 2 票，
    # 现价在下沿区 -> 只做多
    both = dict(price=2035.0, ma_fast=2045.0, ma_slow=2041.0, midline=2040.0)
    sig4 = evaluate(**base_kwargs(**both, mode="range", **rng))
    check(sig4 is not None and sig4["side"] == "LONG",
          "反转 两边同 2 票、现价在下沿区 -> 做多")
    # 窄区间：止盈距离低于手续费地板 0.30% 挡住（区间 2048~2053，做多 @2048.5，止盈 4.5 点 = 0.22%）
    narrow = dict(range_high=2053.0, range_low=2048.0)
    check(evaluate(**base_kwargs(price=2048.5, ma_fast=2050.0, ma_slow=2049.0, midline=2048.0,
                                mode="range", **narrow)) is None,
          "反转 窄区间止盈 4.5 点（0.22%）低于手续费地板挡住")


def test_direction_from_5m():
    # 2026-11-16 用户决定：去掉 EMA50 方向过滤，短线看 5 分钟波动选方向。
    # 做多/做空只看 5 分钟三票，不再被高周期均线挡住。
    sig_long = evaluate(**base_kwargs(price=2070.0,
                                      ma_fast=2065.0, ma_slow=2060.0, midline=2060.0))
    check(sig_long is not None and sig_long["side"] == "LONG",
          "A 5m 做多 3/3 票直接放行（无 EMA 方向限制）")
    sig_short = evaluate(**base_kwargs())
    check(sig_short is not None and sig_short["side"] == "SHORT",
          "A 5m 做空 3/3 票直接放行（无 EMA 方向限制）")
    check(evaluate(**base_kwargs(ma_fast=2046.0, midline=2050.0, volume=50.0)) is None,
          "A 两边都不足 3/3 票时挡住")


def test_fixed_margin_200():
    # 2026-11-17 用户决定：每单固定 200U 保证金（名义 2000U），不再按固定 10U 风险倒推仓位。
    rm = RiskManager(["BTCUSDT", "ETHUSDT"])
    qty, margin, max_loss = rm.size_position(entry=64885.0, stop_loss=64885.0 - 460.0)
    check(abs(margin - 200.0) < 0.01, f"F 保证金固定 200U（实得 {margin:.1f}U）")
    check(abs(qty - 2000.0 / 64885.0) < 1e-9, f"F BTC 数量 = 2000/64885（实得 {qty:.5f}）")
    check(abs(max_loss - (qty * 460.0 + 2000.0 * 0.0007)) < 0.01,
          f"F BTC 止损 460 点预期最大亏损约 15.6U（实得 {max_loss:.2f}U）")
    check(max_loss < 40.0, "F BTC 单笔最大亏损 < 40U")
    qty2, margin2, max_loss2 = rm.size_position(entry=1815.0, stop_loss=1815.0 - 17.0)
    check(abs(margin2 - 200.0) < 0.01, f"F ETH 保证金固定 200U（实得 {margin2:.1f}U）")
    check(max_loss2 < 40.0, f"F ETH 单笔最大亏损 {max_loss2:.2f}U < 40U")
    # 止损距离过大（3%）预期亏损超 40U：放弃这单
    qty3, margin3, max_loss3 = rm.size_position(entry=100.0, stop_loss=97.0)
    check(qty3 == 0.0 and margin3 == 0.0, f"F 止损 3% 预期亏损 {max_loss3:.1f}U 超 40U 上限，放弃（数量 {qty3}）")
    check(max_loss3 > 40.0, "F 超限时返回的预期亏损 > 40U")


def test_atr5_sl_tp():
    sig = evaluate(**base_kwargs(atr=225.0))
    check(sig is not None, "A 5m ATR 信号触发")
    check(abs(sig["stop_loss"] - 2490.0) < 0.01, f"A 止损 2040+450=2490（实得 {sig['stop_loss']:.1f}）")
    check(abs(sig["take_profit_1"] - 1365.0) < 0.01, f"A 止盈 2040-675=1365（实得 {sig['take_profit_1']:.1f}）")
    check("5m" in sig["reason"], "A 信号原因里写 5m 共振")


def test_signal_timeframe_5m():
    # 2026-11-18 用户决定：信号周期 15 分钟 -> 5 分钟（反弹抓不住），区间窗口 6 小时 -> 2 小时；
    # 2026-11-19 用户决定：区间窗口 2 小时 -> 6 小时（2 小时窗口跟着价格滑、震荡开不出单）。
    # 1h ADX 定模式、止损止盈倍数不变。回看窗口按时间换算：区间 6 小时 = 72 根 5 分钟 K 线，
    # 分界线 60 分钟 = 12 根 5 分钟 K 线。
    check(SIGNAL_INTERVAL == "5m", f"A 信号周期 5m（实得 {SIGNAL_INTERVAL}）")
    check(SIGNAL_INTERVAL_MINUTES == 5, f"A 信号周期 5 分钟（实得 {SIGNAL_INTERVAL_MINUTES}）")
    check(RANGE_LOOKBACK_CANDLES == 72, f"A 6 小时区间回看 72 根 5m K 线（实得 {RANGE_LOOKBACK_CANDLES}）")
    check(MIDLINE_LOOKBACK_CANDLES == 12, f"A 分界线回看 12 根 5m K 线（实得 {MIDLINE_LOOKBACK_CANDLES}）")
    # compute_indicators 必须拉 5m K 线（80 根，够 72 根区间回看 + EMA50），
    # 区间高低取最后 72 根，分界线取最后 12 根，1h ADX 仍拉 1h K 线定模式。
    from unittest.mock import patch

    def _candle(i, base):
        c = base + i * 10.0
        return {"open_time": i * 300000, "open": c, "high": c + 5.0,
                "low": c - 5.0, "close": c, "volume": 100.0}

    k5m = [_candle(i, 63000.0) for i in range(80)]
    k1h = [{"open_time": i * 3600000, "open": 64000.0, "high": 64010.0,
            "low": 63990.0, "close": 64000.0, "volume": 50.0} for i in range(40)]
    calls = []

    def _fake_klines(symbol, interval, limit):
        calls.append((symbol, interval, limit))
        return k5m if interval == "5m" else k1h

    with patch.object(main, "get_klines", side_effect=_fake_klines):
        ind = main.compute_indicators("BTCUSDT")
    check(calls[0] == ("BTCUSDT", "5m", 80), f"A 信号 K 线拉 5m 80 根（实得 {calls[0]}）")
    check(calls[1] == ("BTCUSDT", "1h", 40), f"A 1h ADX 仍拉 1h K 线（实得 {calls[1]}）")
    check(ind is not None, "A 5m K 线够用，指标算得出来")
    if ind is not None:
        win72 = k5m[-72:]
        exp_high = max(k["high"] for k in win72)
        exp_low = min(k["low"] for k in win72)
        check(abs(ind["range_high"] - exp_high) < 1e-9,
              f"A 区间高点 = 最后 72 根最高价（实得 {ind['range_high']:.1f}，预期 {exp_high:.1f}）")
        check(abs(ind["range_low"] - exp_low) < 1e-9,
              f"A 区间低点 = 最后 72 根最低价（实得 {ind['range_low']:.1f}，预期 {exp_low:.1f}）")
        win12 = k5m[-12:]
        exp_mid = (max(k["high"] for k in win12) + min(k["low"] for k in win12)) / 2
        check(abs(ind["midline"] - exp_mid) < 1e-9,
              f"A 分界线 = 最后 12 根高低中点（实得 {ind['midline']:.1f}，预期 {exp_mid:.1f}）")
        check(ind["mode"] == "range", f"A 1h ADX 小 -> range 模式（实得 {ind['mode']}）")


# ---------- B) 放宽止损 ----------
def test_widen_stop():
    check(SL_ATR_MULT == 2.0, f"B 止损倍数 2.0（实得 {SL_ATR_MULT}）")
    check(TP_ATR_MULT == 3.0, f"B 止盈倍数 3.0（实得 {TP_ATR_MULT}）")
    check(abs(TP_ATR_MULT / SL_ATR_MULT - 1.5) < 1e-9, "B 盈亏比仍 1:1.5")
    sig = evaluate(price=70000.0, atr=100.0,
                   adx15=31.0, ma_fast=69900.0, ma_slow=70100.0, midline=70200.0,
                   volume=200.0, volume_sma=100.0)
    check(sig is not None and sig["side"] == "SHORT", "B 做空信号触发")
    check(sig["stop_loss"] == 70200.0, f"B 止损 70000+200=70200（实得 {sig['stop_loss']}）")
    check(sig["take_profit_1"] == 69700.0, f"B 止盈 70000-300=69700（实得 {sig['take_profit_1']}）")
    sig2 = evaluate(price=70000.0, atr=100.0,
                    adx15=31.0, ma_fast=70100.0, ma_slow=69900.0, midline=69800.0,
                    volume=200.0, volume_sma=100.0)
    check(sig2 is not None and sig2["side"] == "LONG", "B 做多信号触发")
    check(sig2["stop_loss"] == 69800.0, f"B 做多止损 69800（实得 {sig2['stop_loss']}）")
    check(sig2["take_profit_1"] == 70300.0, f"B 做多止盈 70300（实得 {sig2['take_profit_1']}）")


# ---------- C) 冷却跳过 ----------
def test_cooldown_bypass():
    base = 1_700_000_000.0
    sym = "ETHUSDT"
    rm = RiskManager(["BTCUSDT", "ETHUSDT"])
    rm.record_trade_closed(sym, -6.0, True, now=base)  # 止损出场
    ok, reason = rm.can_trade(sym, now=base + 60, votes=2)
    check(not ok and "冷却" in reason, "C 止损后冷却：2/3 票挡住")
    ok, reason = rm.can_trade(sym, now=base + 60, votes=3)
    check(ok and "跳过" in reason and "止损后" in reason, "C 止损后冷却：3/3 票放行")
    rm2 = RiskManager(["BTCUSDT", "ETHUSDT"])
    rm2.record_trade_closed(sym, 5.0, False, now=base)  # 止盈出场
    ok, reason = rm2.can_trade(sym, now=base + 60, votes=2)
    check(not ok and "冷却" in reason, "C 平仓后冷却：2/3 票挡住")
    ok, reason = rm2.can_trade(sym, now=base + 60, votes=3)
    check(ok and "跳过" in reason and "平仓后" in reason, "C 平仓后冷却：3/3 票放行")
    # 硬刹车不能跳过
    rm3 = RiskManager(["BTCUSDT", "ETHUSDT"])
    for _ in range(3):
        rm3.record_trade_closed(sym, -6.0, True, now=base)
    ok, reason = rm3.can_trade(sym, now=base + 60, votes=3)
    check(not ok and "连亏" in reason, "C 连亏暂停 3/3 票也挡住")
    rm4 = RiskManager(["BTCUSDT", "ETHUSDT"])
    rm4.record_trade_closed(sym, -105.0, True, now=base)
    ok, reason = rm4.can_trade(sym, now=base + 40 * 60, votes=3)
    check(not ok and "熔断" in reason, "C 每日熔断 3/3 票也挡住")
    cpi = datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc).timestamp()
    rm5 = RiskManager(["BTCUSDT", "ETHUSDT"])
    ok, reason = rm5.can_trade(sym, now=cpi, votes=3)
    check(not ok and "大消息" in reason, "C 消息窗口 3/3 票也挡住")


# ---------- D) 熔断 ----------
def test_daily_halt():
    from config import CONSERVATIVE_DAILY_LOSS_USDT
    rm = RiskManager(["BTCUSDT", "ETHUSDT"])
    check(rm.daily_loss_limit == MAX_DAILY_LOSS_USDT == 100.0, "D 默认熔断线 100U")
    rm.daily_pnl_usdt = -99.99
    check(not rm.should_stop_for_day(), "D -99.99U 不熔断")
    rm.daily_pnl_usdt = -100.0
    check(rm.should_stop_for_day(), "D -100U 熔断")
    import time as _t
    ok, reason = rm.can_trade("BTCUSDT", now=_t.time())
    check(not ok and "100 U" in reason, "D can_trade 在 -100U 拒绝并报 100U")
    limit = rm.apply_conservative_halt()
    check(limit == CONSERVATIVE_DAILY_LOSS_USDT == 50.0, "D 收紧后熔断线 50U")
    rm.daily_pnl_usdt = -49.99
    check(not rm.should_stop_for_day(), "D 收紧后 -49.99U 不熔断")
    rm.daily_pnl_usdt = -50.0
    check(rm.should_stop_for_day(), "D 收紧后 -50U 熔断")
    # state.json 存取保留收紧线；旧 state.json 无字段默认 100U；换日恢复 100U
    saved = {"date": "2026-11-11", **rm.state_dict(), "positions": {}}
    rm2 = RiskManager(["BTCUSDT", "ETHUSDT"])
    rm2.restore_state(saved)
    check(rm2.daily_loss_limit == 50.0, "D state.json 恢复保留 50U 收紧线")
    old_saved = dict(saved)
    old_saved.pop("daily_loss_limit")
    rm3 = RiskManager(["BTCUSDT", "ETHUSDT"])
    rm3.restore_state(old_saved)
    check(rm3.daily_loss_limit == 100.0, "D 旧 state.json 无字段时默认 100U")
    check(rm2.reset_if_new_day("2099-01-01"), "D 换日触发清零")
    check(rm2.daily_loss_limit == 100.0, "D 换日后熔断线恢复 100U")


# ---------- E) 轮询 ----------
def test_polling_constants():
    check(HOLDING_POLL_SECONDS == 5, f"E 持仓 5 秒一轮（实得 {HOLDING_POLL_SECONDS}）")
    check(FLAT_POLL_SECONDS == 60, f"E 空仓 60 秒一轮（实得 {FLAT_POLL_SECONDS}）")


def test_constants():
    check(MAX_MARGIN_PER_TRADE_USDT == 400.0, f"常量保证金上限 400U（实得 {MAX_MARGIN_PER_TRADE_USDT}）")
    check(MAX_DAILY_LOSS_USDT == 100.0, f"常量日熔断 100U（实得 {MAX_DAILY_LOSS_USDT}）")


if __name__ == "__main__":
    test_votes_3of3_only()
    test_no_adx_gate()
    test_overextension_filter()
    test_select_mode()
    test_trend_mode()
    test_range_mode()
    test_direction_from_5m()
    test_fixed_margin_200()
    test_atr5_sl_tp()
    test_signal_timeframe_5m()
    test_widen_stop()
    test_cooldown_bypass()
    test_daily_halt()
    test_polling_constants()
    test_constants()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项")
        sys.exit(1)
    print("ALL PASS")
