"""
Binance BTCUSDT 永续合约短线机器人 —— 10 倍快进快出方案（默认 Dry-Run，不下真实单）

版本：2026-11-10 用户确认（综合 3Commas / Cryptohopper / Pionex / Freqtrade 等做法后定）。

安全原则：
- 默认 DRY_RUN = True：只看行情、只记录“本来会下的单”，绝不调下单接口。
- 想走实盘必须同时满足：环境变量 BINANCE_LIVE_TRADING=1，
  且 config.json 里 live_trading_enabled=true，且 API Key 只开
  “读取 + 合约交易”权限，绝对不要开提现 / 转账。
- API Key / Secret 只从环境变量或 config.json 读，绝不写死在代码里。
  本仓库不含任何真实 Key、Secret、账号、密码。

策略（写死在 config.py）：
- 10 倍杠杆，一单最多 200 U 保证金，名义仓位 2000 U
- 信号：5 分钟 K 线收盘时判断（1 小时 EMA200 定方向 + 15 分钟 ADX>23 + 5 分钟均线，3 选 2）
- 止损 1.5 倍 ATR，止盈 2.25 倍 ATR，盈亏比 1:1.5；止盈目标 < 0.30% 不做
- 赚到 1 倍风险就把止损移到成本价（保本）；进场限价单，止损市价单
- 冷却 15 分钟，止损后 30 分钟；每天最多 10 单，同时最多 1 单
- 熔断：一天亏 40 U（本金 2%）停；连亏 3 单休息 1 小时；大消息前后 30 分钟不交易
- 当日计数按 UTC 日期自动清零（2026-11-07 的教训：旧版只在重启时清零，跑着不动就一直卡在上限）
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    ADX_PERIOD,
    ATR_PERIOD,
    DATA_BASE_URL,
    DRY_RUN,
    EMA_PERIOD_1H,
    LIVE_TRADING_ENABLED,
    MA_FAST_PERIOD,
    MA_SLOW_PERIOD,
    MAKER_FEE_PCT,
    MAX_DAILY_LOSS_USDT,
    MAX_TRADES_PER_DAY,
    SYMBOL,
    TAKER_FEE_PCT,
    VOL_SMA_PERIOD,
    load_api_credentials,
)
from indicators import adx, atr, ema, sma
from risk import RiskManager
from strategy import evaluate


# --- Health endpoint (runs in background thread; port from $PORT, default 3000) ---
def _start_health_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    _port = int(os.environ.get("PORT", "3000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                log_text = Path(__file__).resolve().parent.joinpath("bot.log").read_text(encoding="utf-8")[-2000:]
            except Exception:
                log_text = "(no log yet)"
            self.wfile.write(f"Binance bot dry-run alive\n\n{log_text}".encode())
        def log_message(self, *a): pass
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", _port), H).serve_forever(), daemon=True).start()
_start_health_server()



def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(Path(__file__).resolve().parent / "bot.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------- 行情（公开接口，无需 API Key，不交易）----------
def _http_get(path: str, params: dict) -> object | None:
    try:
        import requests

        r = requests.get(f"{DATA_BASE_URL}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 - 读行情失败只记日志，不崩
        log(f"读行情失败 {path}: {e}")
        return None


def get_price_dry_run() -> float | None:
    """现价（ticker）。"""
    data = _http_get("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    if not isinstance(data, dict) or "price" not in data:
        return None
    try:
        return float(data["price"])
    except (TypeError, ValueError):
        return None


def get_klines(interval: str, limit: int) -> list[dict]:
    """K 线：返回已收盘 + 未收盘的全部 K 线（调用方自己去掉最后一根未收盘的）。
    每根：{"open_time","open","high","low","close","volume"}。"""
    data = _http_get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        try:
            out.append({
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


def compute_indicators() -> dict | None:
    """拉三组 K 线，算出信号要用的全部指标。失败返回 None。
    只用已收盘的 K 线（去掉每组最后一根未收盘的）。"""
    k5 = get_klines("5m", 40)
    k15 = get_klines("15m", 40)
    k1h = get_klines("1h", 210)
    if len(k5) < ATR_PERIOD + 2 or len(k15) < ADX_PERIOD * 2 + 1 or len(k1h) < EMA_PERIOD_1H + 1:
        log("K 线不够，指标算不出来，等下一根 5 分钟 K 线。")
        return None
    closed5 = k5[:-1]
    closed15 = k15[:-1]
    closed1h = k1h[:-1]
    closes5 = [k["close"] for k in closed5]
    closes1h = [k["close"] for k in closed1h]
    volumes5 = [k["volume"] for k in closed5]
    atr_value = atr(closed5, ATR_PERIOD)
    adx_value, _, _ = adx(closed15, ADX_PERIOD)
    # 滚动分界线：最近 12 根 5 分钟 K 线（60 分钟）最高最低价的中点，跟着市场走
    recent = closed5[-12:]
    midline = (max(k["high"] for k in recent) + min(k["low"] for k in recent)) / 2
    return {
        "candle_open_time": closed5[-1]["open_time"],
        "price": closed5[-1]["close"],
        "atr": atr_value,
        "ema200_1h": ema(closes1h, EMA_PERIOD_1H),
        "adx15": adx_value,
        "ma_fast": sma(closes5, MA_FAST_PERIOD),
        "ma_slow": sma(closes5, MA_SLOW_PERIOD),
        "midline": midline,
        "volume": closed5[-1]["volume"],
        "volume_sma": sma(volumes5, VOL_SMA_PERIOD),
    }


# ---------- 下单 ----------
def place_order_dry_run(signal: dict, qty: float, margin_usdt: float) -> None:
    """Dry-Run：只记录，不下单。"""
    log(
        "[DRY-RUN] 本来会下单: "
        f"{signal['side']} {SYMBOL} qty={qty:.5f} 保证金={margin_usdt:.2f} USDT "
        f"入场~{signal['entry']:.1f} 止损={signal['stop_loss']:.1f} "
        f"止盈={signal['take_profit_1']:.1f} "
        f"预期最大亏损={signal['max_loss_usdt']:.2f} USDT | {signal['reason']}"
    )


def place_order_live(signal: dict, qty: float, margin_usdt: float) -> None:
    """
    实盘下单入口（骨架：默认永不执行）。

    只有在 DRY_RUN=False 且 LIVE_TRADING_ENABLED=True 时才会被调用，
    且调用前 RiskManager 已校验过。真正接通时在这里用 python-binance
    下 Post Only 限价单进场，并立即挂止损市价单和止盈限价单。
    当前版本为防止误操作，直接拒绝执行。
    """
    raise RuntimeError(
        "实盘下单未接通：本版本默认 Dry-Run。请先在 README.md 里完成 "
        "API 权限检查、风控确认和试运行，再由人工打开实盘开关。"
    )


def close_pnl(position: dict, exit_price: float, exit_is_maker: bool) -> float:
    """算一单的模拟盈亏（含手续费）。进场永远是 maker（Post Only 限价单）；
    出场走止盈是 maker，走止损是 taker（市价单）。"""
    qty = position["qty"]
    entry = position["entry"]
    direction = 1 if position["side"] == "LONG" else -1
    gross = direction * qty * (exit_price - entry)
    entry_fee = qty * entry * MAKER_FEE_PCT
    exit_fee = qty * exit_price * (MAKER_FEE_PCT if exit_is_maker else TAKER_FEE_PCT)
    return gross - entry_fee - exit_fee


def main() -> None:
    log("=" * 60)
    log("BTCUSDT 合约短线机器人启动（10 倍快进快出方案，2026-11-10 用户确认）")
    log(f"标的: {SYMBOL} | Dry-Run: {DRY_RUN} | 实盘开关: {LIVE_TRADING_ENABLED}")
    log("策略: 5m 共振（1h EMA200 + 15m ADX>23 + 5m 均线，3 选 2）+ 量能过滤；"
        "止损 1.5 倍 ATR，止盈 2.25 倍 ATR，盈亏比 1:1.5；赚 1 倍风险保本；"
        "进场限价单，止损市价单；冷却 15 分钟（止损后 30 分钟）；一天最多 10 单，同时最多 1 单")
    log("风控: 10 倍 / 单笔保证金<=200U / 单笔风险目标 10U（本金 0.5%）/ 单笔亏损硬上限 40U / "
        f"日亏熔断 {MAX_DAILY_LOSS_USDT:.0f}U / 连亏 3 单休息 1 小时 / 大消息前后 30 分钟不交易")
    if not DRY_RUN and not LIVE_TRADING_ENABLED:
        log("已拒绝启动：DRY_RUN=False 但未在 config.json 开 live_trading_enabled。")
        return
    if not DRY_RUN:
        load_api_credentials()  # 校验 Key 存在，不打印 Secret
        log("实盘模式：API 凭证已加载（Secret 不打印）。")

    risk = RiskManager()
    stop_file = Path(__file__).resolve().parent / "STOP"
    state_file = Path(__file__).resolve().parent / "state.json"
    position: dict | None = None
    last_eval_candle: int | None = None
    halted_logged = False

    def save_state() -> None:
        state_file.write_text(json.dumps({
            "date": risk.current_date,
            "trades_today": risk.trades_today,
            "consecutive_losses": risk.consecutive_losses,
            "daily_loss_usdt": risk.daily_loss_usdt,
            "daily_pnl_usdt": risk.daily_pnl_usdt,
            "last_close_at": risk.last_close_at,
            "last_close_was_sl": risk.last_close_was_sl,
            "pause_until": risk.pause_until,
            "position": position,
        }, ensure_ascii=False), encoding="utf-8")

    # 断点续跑：读回当天的计数和持仓（只做本地记录）
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            if saved.get("date") == utc_today():
                risk.trades_today = saved.get("trades_today", 0)
                risk.consecutive_losses = saved.get("consecutive_losses", 0)
                risk.daily_loss_usdt = saved.get("daily_loss_usdt", 0.0)
                risk.daily_pnl_usdt = saved.get("daily_pnl_usdt", 0.0)
                risk.last_close_at = saved.get("last_close_at")
                risk.last_close_was_sl = saved.get("last_close_was_sl", False)
                risk.pause_until = saved.get("pause_until")
                position = saved.get("position")
                log(f"恢复今日状态: 已开 {risk.trades_today} 单，连亏 {risk.consecutive_losses} 单，"
                    f"今日盈亏 {risk.daily_pnl_usdt:+.2f} U，持仓: {'有' if position else '无'}")
        except Exception:  # noqa: BLE001
            pass

    while True:
        if stop_file.exists():
            log("检测到 STOP 文件，一键停机，退出。")
            break

        # UTC 日期变了就自动清零（不用重启）
        if risk.reset_if_new_day(utc_today()):
            halted_logged = False
            log("UTC 日期已变，当日计数自动清零，继续交易。")
            save_state()

        if risk.should_stop_for_day():
            if not halted_logged:
                log(f"今日亏损已达 {MAX_DAILY_LOSS_USDT:.0f} U 熔断，停手，等 UTC 午夜自动清零。")
                halted_logged = True
            time.sleep(60)
            continue
        halted_logged = False

        price = get_price_dry_run()
        if price is None:
            time.sleep(60)
            continue
        now = time.time()

        # ---------- 持仓管理（每分钟）----------
        if position is not None:
            side = position["side"]
            entry = position["entry"]
            risk_distance = abs(entry - position["stop_loss"])
            stop_loss = position["stop_loss"]
            take_profit = position["take_profit"]

            # 保本：赚到 1 倍风险就把止损移到成本价
            if not position["breakeven_done"]:
                if side == "LONG" and price >= entry + risk_distance:
                    position["stop_loss"] = entry
                    position["breakeven_done"] = True
                    stop_loss = entry
                    log(f"保本触发：{side} 已赚 1 倍风险，止损移到成本价 {entry:.1f}。")
                    save_state()  # 保本后的止损要落盘，重启也不丢
                elif side == "SHORT" and price <= entry - risk_distance:
                    position["stop_loss"] = entry
                    position["breakeven_done"] = True
                    stop_loss = entry
                    log(f"保本触发：{side} 已赚 1 倍风险，止损移到成本价 {entry:.1f}。")
                    save_state()  # 保本后的止损要落盘，重启也不丢

            hit_tp = (side == "LONG" and price >= take_profit) or \
                     (side == "SHORT" and price <= take_profit)
            hit_sl = (side == "LONG" and price <= stop_loss) or \
                     (side == "SHORT" and price >= stop_loss)
            if hit_tp or hit_sl:
                was_sl = hit_sl and not position["breakeven_done"]
                pnl = close_pnl(position, price, exit_is_maker=hit_tp)
                kind = "止盈" if hit_tp else ("止损" if was_sl else "保本出场")
                log(f"平仓 [{kind}]：{side} 入场 {entry:.1f} 出场 {price:.1f}，"
                    f"本单盈亏 {pnl:+.2f} U，今日累计 {risk.daily_pnl_usdt + pnl:+.2f} U。")
                risk.record_trade_closed(pnl, was_sl=was_sl, now=now)
                position = None
                save_state()
                time.sleep(60)
                continue
            # 持仓心跳：持仓时每 20 秒记一行，证明循环活着；止损/止盈/保本也查得更勤，
            # 避免 1 分钟才查一次错过价格尖峰
            float_pnl = (price - entry) * position["qty"] if side == "LONG" else (entry - price) * position["qty"]
            log(f"持仓中：{side} @{entry:.1f}，现价 {price:.1f}，浮盈 {float_pnl:+.2f} U，"
                f"止损 {stop_loss:.1f}，止盈 {take_profit:.1f}，"
                f"保本{'已移' if position['breakeven_done'] else '未触发'}。")
            time.sleep(20)
            continue

        # ---------- 无持仓：等 5 分钟 K 线收盘再判断 ----------
        indicators = compute_indicators()
        if indicators is None:
            time.sleep(60)
            continue
        if indicators["candle_open_time"] == last_eval_candle:
            # 这根 5 分钟 K 线已经判断过，等下一根
            log(f"无信号，BTC 现价 {price:.1f}，继续等 1 分钟。")
            time.sleep(60)
            continue
        last_eval_candle = indicators["candle_open_time"]

        signal = evaluate(price=indicators["price"], atr=indicators["atr"],
                          ema200_1h=indicators["ema200_1h"], adx15=indicators["adx15"],
                          ma_fast=indicators["ma_fast"], ma_slow=indicators["ma_slow"],
                          midline=indicators["midline"],
                          volume=indicators["volume"], volume_sma=indicators["volume_sma"],
                          log=log)
        if signal is None:
            log(f"无信号，BTC 现价 {price:.1f}（5m 收 {indicators['price']:.1f}），继续等。")
            time.sleep(60)
            continue

        log(f"信号触发: {signal['side']} @ {signal['entry']:.1f} 原因: {signal['reason']}")
        ok, reason = risk.can_trade(now=now)
        if not ok:
            log(f"风控拦截：{reason}")
            time.sleep(60)
            continue

        qty, margin_usdt, max_loss = risk.size_position(
            entry=signal["entry"], stop_loss=signal["stop_loss"]
        )
        if qty <= 0:
            log(f"风控拦截：按止损距离算，亏损会超 40 U 上限（算出 {max_loss:.2f} U），放弃这单。")
            time.sleep(60)
            continue
        signal["max_loss_usdt"] = max_loss

        if DRY_RUN:
            place_order_dry_run(signal, qty, margin_usdt)
        else:
            place_order_live(signal, qty, margin_usdt)
        risk.record_trade_opened(now=now)
        position = {
            "side": signal["side"],
            "entry": signal["entry"],
            "qty": qty,
            "margin": margin_usdt,
            "stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit_1"],
            "breakeven_done": False,
            "opened_at": now,
            "atr": signal.get("atr"),
        }
        save_state()
        time.sleep(60)


if __name__ == "__main__":
    main()
