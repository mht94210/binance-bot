"""剥头皮测试版规则回归（放在仓库里，虚拟机重启也不丢）。

和德国实盘版的区别只有四处（用户 2026-11-17 决定：先做模拟盘版本测几天，放日本服务器跑）：
A) 止损 3 倍 ATR（放远了，不会被噪音突然打掉；实盘版是 2 倍）
B) 止盈不再按 ATR：浮盈扣掉全部手续费，净赚 2U 就平（止盈价按仓位反推）
C) 每天每币上限 50 单（用户目标一天 100 单；模拟盘测试，额度不做瓶颈）
D) 每单固定 200U 保证金（用户 2026-11-17 决定；10 倍杠杆 = 2000U 名义仓位；仓位不再按风险倒推）
其余照抄实盘版：三模式信号、3/3 全票、冷却、熔断、保本逻辑都不动。

用法：python3 tests/test_scalp_rules.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    MAKER_FEE_PCT,
    MAX_TRADES_PER_DAY,
    RANGE_LOOKBACK_CANDLES,
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
              adx5=10.0, ma_fast=64400.0, ma_slow=64500.0, midline=64550.0,
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


# ---------- 0) 信号周期 5 分钟，6 小时区间 = 72 根 ----------
check(SIGNAL_INTERVAL == "5m", "0 信号周期 5 分钟")
check(RANGE_LOOKBACK_CANDLES == 72, "0 6 小时区间 = 72 根 5 分钟 K 线")

# ---------- A) 止损 3 倍 ATR ----------
sig_s = evaluate(**base_kwargs())
check(sig_s is not None and sig_s["side"] == "SHORT",
      "A 3/3 全票做空信号触发")
check(sig_s is not None and abs(sig_s["stop_loss"] - sig_s["entry"]) == 3 * 242.0,
      "A 止损 = 3 倍 ATR（726 点）")
sig_l = evaluate(**long_kwargs())
check(sig_l is not None and sig_l["side"] == "LONG",
      "A 3/3 全票做多信号触发")
check(sig_l is not None and abs(sig_l["entry"] - sig_l["stop_loss"]) == 3 * 242.0,
      "A 做多止损也是 3 倍 ATR")
check(SL_ATR_MULT == 3.0, "A SL_ATR_MULT = 3.0")

# ---------- B) 止盈净赚 2U（扣掉全部手续费）----------
check(SCALP_NET_TP_USDT == 2.0, "B SCALP_NET_TP_USDT = 2.0")
# 做空：按信号的仓位和止盈价，用真正的 close_pnl 算，出场 maker 费
qty_s = position_qty(sig_s["entry"], sig_s["stop_loss"])
pnl_s = net_pnl_at("SHORT", sig_s["entry"], qty_s, sig_s["take_profit"], True)
check(abs(pnl_s - 2.0) < 0.01, f"B 做空到止盈价净赚 2U（算出 {pnl_s:+.2f}U）")
qty_l = position_qty(sig_l["entry"], sig_l["stop_loss"])
pnl_l = net_pnl_at("LONG", sig_l["entry"], qty_l, sig_l["take_profit"], True)
check(abs(pnl_l - 2.0) < 0.01, f"B 做多到止盈价净赚 2U（算出 {pnl_l:+.2f}U）")
# 止盈价离入场很近（远小于止损距离），才配叫剥头皮
check(abs(sig_s["take_profit"] - sig_s["entry"]) < (1.0/3.0) * abs(sig_s["stop_loss"] - sig_s["entry"]),
      "B 止盈距离 < 止损距离的 1/3")
check("净赚 2U" in sig_s["reason"] and "净赚 2U" in sig_l["reason"],
      "B 信号原因写明止盈净赚 2U")
# 公式本身：BTC 做多 64500、数量 0.01375，止盈约 64671.3
tp_formula = scalp_tp_price(64500.0, "LONG", 0.01375)
check(abs(tp_formula - 64671.3) < 1.0, f"B 公式算做多止盈约 64671.3（算出 {tp_formula:.1f}）")
tp_formula_s = scalp_tp_price(64500.0, "SHORT", 0.01375)
check(abs(tp_formula_s - 64328.8) < 1.0, f"B 公式算做空止盈约 64328.8（算出 {tp_formula_s:.1f}）")
# 止损出场是市价单（taker 费），固定 200U 保证金后一单净亏约 23.9U——
# 仓位比风险倒推时大（BTC 0.031 对 0.0138），亏满也更大；写进测试免得以后有人偷偷改小
pnl_sl = net_pnl_at("SHORT", sig_s["entry"], qty_s, sig_s["stop_loss"], False)
check(-25.0 < pnl_sl < -23.0, f"B 止损出场净亏约 23.9U（算出 {pnl_sl:+.2f}U）")

# ---------- B2) 手续费地板不启用：ATR 再小也不挡信号 ----------
sig_tiny = evaluate(**base_kwargs(atr=1.0))
check(sig_tiny is not None, "B2 ATR 只有 1 点也不挡信号（地板已取消）")

# ---------- C) 每天每币上限 50 单 ----------
check(MAX_TRADES_PER_DAY == 50, "C MAX_TRADES_PER_DAY = 50")

# ---------- D) 仓位：每单固定 200U 保证金（用户 2026-11-17 决定）----------
risk = RiskManager(["BTCUSDT", "ETHUSDT"])
qty, margin, max_loss = risk.size_position(64500.0, 64500.0 + 726.0)
check(margin == 200.0, f"D 3 倍 ATR 仓位保证金固定 200U（算出 {margin:.1f}U）")
check(max_loss < 40.0, f"D 预期最大亏损 < 40U 上限（算出 {max_loss:.2f}U）")
qty2, margin2, _ = risk.size_position(64500.0, 64490.0)
check(margin2 == 200.0, f"D 止损很近时保证金仍是固定 200U，不再封顶（算出 {margin2:.1f}U）")
qty3, margin3, _ = risk.size_position(1840.0, 1840.0 - 12.0)
check(abs(margin3 - 200.0) < 0.01 and abs(qty3 - 2000.0 / 1840.0) < 1e-6,
      f"D ETH 入场 1840 数量 = 2000/1840（算出 qty {qty3:.5f}，保证金 {margin3:.1f}U）")

# ---------- 3/3 全票仍是硬门槛 ----------
check(evaluate(**base_kwargs(volume=50.0)) is None, "D 2/3 票挡住（量能不足）")

# ---------- 模拟盘开关：config.json 明确关实盘 ----------
cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
check(cfg.get("live_trading_enabled") is False, "E config.json 里 live_trading_enabled = false（模拟盘）")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项：{FAILURES}")
    sys.exit(1)
print("全部通过。")
