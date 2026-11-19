"""剥头皮测试版规则回归（放在仓库里，虚拟机重启也不丢）。

和德国实盘版的区别只有五处（用户 2026-11-17 决定：先做模拟盘版本测几天，放日本服务器跑）：
A) 止损 3 倍 ATR（放远了，不会被噪音突然打掉；实盘版是 2 倍）
B) 止盈不再按 ATR：浮盈扣掉全部手续费，净赚 15U 就平（止盈价按仓位反推）
C) 每天每币上限 50 单（用户目标一天 100 单；模拟盘测试，额度不做瓶颈）
D) 每单固定 500U 保证金（用户 2026-11-18 决定：500U 快进快出方案；10 倍杠杆 = 5000U 名义仓位；仓位不再按风险倒推）
E) 简化信号（用户 2026-11-17 决定：均线定方向 + 2/3 票就开）：方向只看 5 分钟 MA5/MA20，
   三个投票（均线/分界线/量能）里 2 票就开；不要三模式、不要 ADX、不要 EMA50、
   不要过度延伸过滤（原来三模式 + 3/3 全票太严，200U 版上线 1 小时一单没开）
其余照抄实盘版；冷却改过三次（2026-11-17 用户决定：同方向 3/3 不跳过冷却；平仓后冷却 15 分钟改 10 分钟；止损后反向直接进），熔断、保本逻辑不动。

用法：python3 tests/test_scalp_rules.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    COOLDOWN_MINUTES,
    MAKER_FEE_PCT,
    MAX_TRADES_PER_DAY,
    SCALP_NET_TP_USDT,
    SIGNAL_INTERVAL,
    SL_ATR_MULT,
    TAKER_FEE_PCT,
)
from main import close_pnl
from risk import RiskManager, position_qty, scalp_tp_price
from strategy import evaluate

FAILURES = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def base_kwargs(**over):
    # BTC 价位：价格 64500，5m ATR 242（测试用合成值；逻辑与 ATR 大小无关）
    kw = dict(price=64500.0, atr=242.0,
              ma_fast=64400.0, ma_slow=64500.0, midline=64550.0,
              volume=100.0, volume_sma=80.0)
    kw.update(over)
    return kw


def long_kwargs(**over):
    kw = base_kwargs(ma_fast=64600.0, ma_slow=64500.0, midline=64450.0)
    kw.update(over)
    return kw


def net_pnl_at(side, entry, qty, exit_price, exit_is_maker):
    pos = {"side": side, "entry": entry, "qty": qty}
    return close_pnl(pos, exit_price, exit_is_maker)


# ---------- 0) 信号周期 5 分钟 ----------
check(SIGNAL_INTERVAL == "5m", "0 信号周期 5 分钟")

# ---------- A) 止损 3 倍 ATR ----------
sig_s = evaluate(**base_kwargs())
check(sig_s is not None and sig_s["side"] == "SHORT",
      "A 做空信号触发（均线向下 + 2/3 票）")
check(sig_s is not None and abs(sig_s["stop_loss"] - sig_s["entry"]) == 3 * 242.0,
      "A 止损 = 3 倍 ATR（726 点）")
sig_l = evaluate(**long_kwargs())
check(sig_l is not None and sig_l["side"] == "LONG",
      "A 做多信号触发（均线向上 + 2/3 票）")
check(sig_l is not None and abs(sig_l["entry"] - sig_l["stop_loss"]) == 3 * 242.0,
      "A 做多止损也是 3 倍 ATR")
check(SL_ATR_MULT == 3.0, "A SL_ATR_MULT = 3.0")

# ---------- B) 止盈净赚 15U（扣掉全部手续费）----------
check(SCALP_NET_TP_USDT == 15.0, "B SCALP_NET_TP_USDT = 15.0")
# 做空：按信号的仓位和止盈价，用真正的 close_pnl 算，出场 maker 费
qty_s = position_qty(sig_s["entry"], sig_s["stop_loss"])
pnl_s = net_pnl_at("SHORT", sig_s["entry"], qty_s, sig_s["take_profit"], True)
check(abs(pnl_s - 15.0) < 0.01, f"B 做空到止盈价净赚 15U（算出 {pnl_s:+.2f}U）")
qty_l = position_qty(sig_l["entry"], sig_l["stop_loss"])
pnl_l = net_pnl_at("LONG", sig_l["entry"], qty_l, sig_l["take_profit"], True)
check(abs(pnl_l - 15.0) < 0.01, f"B 做多到止盈价净赚 15U（算出 {pnl_l:+.2f}U）")
# 止盈价离入场很近（远小于止损距离），才配叫剥头皮
check(abs(sig_s["take_profit"] - sig_s["entry"]) < (1.0/3.0) * abs(sig_s["stop_loss"] - sig_s["entry"]),
      "B 止盈距离 < 止损距离的 1/3")
check("净赚 15U" in sig_s["reason"] and "净赚 15U" in sig_l["reason"],
      "B 信号原因写明止盈净赚 15U")
# 公式本身：BTC 做多 64500、数量 0.01375，止盈约 65616.9（15U 版；5U 版是 64889.5）
tp_formula = scalp_tp_price(64500.0, "LONG", 0.01375)
check(abs(tp_formula - 65616.9) < 1.0, f"B 公式算做多止盈约 65616.9（算出 {tp_formula:.1f}）")
tp_formula_s = scalp_tp_price(64500.0, "SHORT", 0.01375)
check(abs(tp_formula_s - 63383.5) < 1.0, f"B 公式算做空止盈约 63383.5（算出 {tp_formula_s:.1f}）")
# 止损出场是市价单（taker 费），固定 500U 保证金后一单净亏约 59.8U——
# 仓位比风险倒推时大（BTC 0.0775 对 0.0138），亏满也更大；写进测试免得以后有人偷偷改小
pnl_sl = net_pnl_at("SHORT", sig_s["entry"], qty_s, sig_s["stop_loss"], False)
check(-61.0 < pnl_sl < -59.0, f"B 止损出场净亏约 59.8U（算出 {pnl_sl:+.2f}U）")

# ---------- B2) 手续费地板不启用：ATR 再小也不挡信号 ----------
sig_tiny = evaluate(**base_kwargs(atr=1.0))
check(sig_tiny is not None, "B2 ATR 只有 1 点也不挡信号（地板已取消）")

# ---------- C) 每天每币上限 50 单 ----------
check(MAX_TRADES_PER_DAY == 50, "C MAX_TRADES_PER_DAY = 50")

# ---------- D) 仓位：每单固定 500U 保证金（用户 2026-11-18 决定：500U 快进快出方案）----------
risk = RiskManager(["BTCUSDT", "ETHUSDT"])
qty, margin, max_loss = risk.size_position(64500.0, 64500.0 + 726.0)
check(margin == 500.0, f"D 3 倍 ATR 仓位保证金固定 500U（算出 {margin:.1f}U）")
check(max_loss < 60.0, f"D 预期最大亏损 < 60U 上限（算出 {max_loss:.2f}U）")
qty2, margin2, _ = risk.size_position(64500.0, 64490.0)
check(margin2 == 500.0, f"D 止损很近时保证金仍是固定 500U，不再封顶（算出 {margin2:.1f}U）")
qty3, margin3, _ = risk.size_position(1840.0, 1840.0 - 12.0)
check(abs(margin3 - 500.0) < 0.01 and abs(qty3 - 5000.0 / 1840.0) < 1e-6,
      f"D ETH 入场 1840 数量 = 5000/1840（算出 qty {qty3:.5f}，保证金 {margin3:.1f}U）")

# ---------- E) 简化信号：均线定方向 + 2/3 票就开 ----------
# 量能不足：均线 + 分界线 = 2 票，照样开（原来 3/3 全票会挡住）
check(evaluate(**base_kwargs(volume=50.0)) is not None, "E 2/3 票就开（量能不足）")
# 只有均线一票：挡住
check(evaluate(**base_kwargs(volume=50.0, midline=64450.0)) is None, "E 1/3 票挡住（只有均线一票）")
# 方向只看均线：做多也有 2 票（分界线+量能），但 MA5 在 MA20 下方，只做空不做多
sig_both = evaluate(**base_kwargs(midline=64450.0))
check(sig_both is not None and sig_both["side"] == "SHORT",
      "E 均线向下时做空（即使做多也有 2 票，方向只看均线）")
# 反向：做空也有 2 票，但 MA5 在 MA20 上方，只做多不做空
sig_both_l = evaluate(**long_kwargs(midline=64550.0))
check(sig_both_l is not None and sig_both_l["side"] == "LONG",
      "E 均线向上时做多（即使做空也有 2 票，方向只看均线）")
# ADX 再低也不挡信号（没有 ADX 门槛了）；EMA50/过度延伸也不挡
check(evaluate(**base_kwargs()) is not None, "E 不用 ADX/EMA50/过度延伸过滤也能开单")

# ---------- F) 同方向冷却：刚平仓后同方向 3/3 也不跳过冷却，反向才跳过（用户 2026-11-17 决定）----------
# 背景：J4 ETH 空单止盈后 5 秒，J5 又开了同方向空单（3/3 跳过了冷却）——用户要求同方向必须等满冷却。
# 补充（用户 2026-11-17 决定）：止损后反向有机会就直接进，不要 30 分钟不动——
# 上一单止损出的，反向信号 2 票 3 票都直接跳过冷却；止盈后反向 2 票还是要等冷却。
_t0 = 1794906000.0  # 2026-11-17 09:00 UTC，不在大消息窗口里
risk_f = RiskManager(["ETHUSDT"])
risk_f.record_trade_closed("ETHUSDT", 3.02, was_sl=False, now=_t0, side="SHORT")
ok_same, _ = risk_f.can_trade("ETHUSDT", now=_t0 + 5, votes=3, side="SHORT")
check(not ok_same, "F 同方向做空 3/3 也要等冷却（刚平空单 5 秒）")
ok_rev, _ = risk_f.can_trade("ETHUSDT", now=_t0 + 5, votes=3, side="LONG")
check(ok_rev, "F 反向做多 3/3 可跳过冷却")
ok_rev2, _ = risk_f.can_trade("ETHUSDT", now=_t0 + 5, votes=2, side="LONG")
check(not ok_rev2, "F 反向 2/3 票仍要等冷却")
check(COOLDOWN_MINUTES == 10, "F 平仓后冷却 = 10 分钟（用户 2026-11-17 决定）")
ok_later, _ = risk_f.can_trade("ETHUSDT", now=_t0 + 10 * 60 + 1, votes=2, side="SHORT")
check(ok_later, "F 冷却满 10 分钟后同方向可开")
# 止损后冷却 30 分钟：同方向 3/3 在 20 分钟时仍挡住，反向放行
risk_f2 = RiskManager(["ETHUSDT"])
risk_f2.record_trade_closed("ETHUSDT", -11.21, was_sl=True, now=_t0, side="SHORT")
ok_sl, _ = risk_f2.can_trade("ETHUSDT", now=_t0 + 20 * 60, votes=3, side="SHORT")
check(not ok_sl, "F 止损后 30 分钟冷却，同方向 3/3 在 20 分钟时仍挡住")
ok_sl_rev, _ = risk_f2.can_trade("ETHUSDT", now=_t0 + 20 * 60, votes=3, side="LONG")
check(ok_sl_rev, "F 止损后反向 3/3 可跳过冷却")
# 止损后反向：2 票 3 票都直接进，不等 30 分钟；同方向还是要等满
ok_sl_rev2, _ = risk_f2.can_trade("ETHUSDT", now=_t0 + 5 * 60, votes=2, side="LONG")
check(ok_sl_rev2, "F 止损后反向 2/3 票也直接进（不等 30 分钟）")
ok_sl_same2, _ = risk_f2.can_trade("ETHUSDT", now=_t0 + 5 * 60, votes=2, side="SHORT")
check(not ok_sl_same2, "F 止损后同方向 2/3 票仍要等冷却")
# 止盈后反向 2 票：还是要等冷却（直接进只适用于止损后反向）
ok_tp_rev2, _ = risk_f.can_trade("ETHUSDT", now=_t0 + 5, votes=2, side="LONG")
check(not ok_tp_rev2, "F 止盈后反向 2/3 票仍要等冷却")
# 状态持久化：last_close_side 进 state.json，恢复后规则还在
_st = risk_f.state_dict()
risk_f3 = RiskManager(["ETHUSDT"])
risk_f3.restore_state(_st)
ok_restored, _ = risk_f3.can_trade("ETHUSDT", now=_t0 + 5, votes=3, side="SHORT")
check(not ok_restored, "F state.json 恢复后同方向冷却仍生效")

# ---------- 模拟盘开关：config.json 明确关实盘 ----------
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
check(cfg.get("live_trading_enabled") is False, "E config.json 里 live_trading_enabled = false（模拟盘）")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项：{FAILURES}")
    sys.exit(1)
print("全部通过。")
