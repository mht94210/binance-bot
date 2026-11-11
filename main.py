"""
Binance BTC+ETH 永续合约短线机器人 —— 10 倍快进快出方案（默认 Dry-Run，不下真实单）

版本：2026-11-11 用户确认：日本机器人改成震荡版（高空低多），
和德国趋势版（少而精，ADX>30 才做）互补；震荡市日本做、趋势市德国做。

安全原则：
- 默认 DRY_RUN = True：只看行情、只记录“本来会下的单”，绝不调下单接口。
- 想走实盘必须同时满足：环境变量 BINANCE_LIVE_TRADING=1，
  且 config.json 里 live_trading_enabled=true，且 API Key 只开
  “读取 + 合约交易”权限，绝对不要开提现 / 转账。
- API Key / Secret 只从环境变量或 config.json 读，绝不写死在代码里。
  本仓库不含任何真实 Key、Secret、账号、密码。

策略（写死在 config.py）：
- 10 倍杠杆，一单最多 200 U 保证金，名义仓位 2000 U
- 信号：15 分钟 K 线收盘时判断；区间 = 最近 24 根 15 分钟 K 线（6 小时）最高/最低；
  只做震荡：15 分钟 ADX 必须 <25（有趋势不动）；现价在区间上沿 25% 区域内做空，
  下沿 25% 区域内做多；区间中部不动；已突破上下沿不追；区间太窄（<0.6%）不动
- 止损：区间沿外 0.5 倍 ATR；止盈：对侧沿（整段区间），盈亏比约 1:2 到 1:4；
  止盈目标 < 0.30% 不做
- 赚到 1 倍风险就把止损移到成本价（保本）；进场限价单，止损市价单
- 冷却 15 分钟，止损后 30 分钟；每个币种每天最多 10 单、同时最多 1 单
  （全账户同时最多 2 单、每天最多 20 单）
  （震荡信号没有票数，不跳过冷却；连亏暂停、消息禁入、每日熔断是硬刹车）
- 熔断：一天净亏 100 U（本金 5%）停；连亏 3 单休息 1 小时；大消息前后 30 分钟不交易
- 当日计数按 UTC 日期自动清零（2026-11-07 的教训：旧版只在重启时清零，跑着不动就一直卡在上限）
- 入场按判断时的实时价，不用滞后的 5 分钟 K 线收盘价（2026-11-10 的教训）
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
    CONSERVATIVE_DAILY_LOSS_USDT,
    DATA_BASE_URL,
    DRY_RUN,
    FLAT_POLL_SECONDS,
    HEARTBEAT_LOG_SECONDS,
    HOLDING_POLL_SECONDS,
    LIVE_TRADING_ENABLED,
    MAKER_FEE_PCT,
    MAX_DAILY_LOSS_USDT,
    MAX_OPEN_POSITIONS,
    RANGE_LOOKBACK,
    SYMBOLS,
    TAKER_FEE_PCT,
    load_api_credentials,
)
from indicators import adx, atr
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
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            try:
                # 双币种后日志翻倍，留 16000 字符（约 24 分钟心跳），和单币种 8000 字符覆盖一样长
                log_text = Path(__file__).resolve().parent.joinpath("bot.log").read_text(encoding="utf-8")[-16000:]
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


def get_price_dry_run(symbol: str) -> float | None:
    """现价（ticker）。"""
    data = _http_get("/fapi/v1/ticker/price", {"symbol": symbol})
    if not isinstance(data, dict) or "price" not in data:
        return None
    try:
        return float(data["price"])
    except (TypeError, ValueError):
        return None


def get_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """K 线：返回已收盘 + 未收盘的全部 K 线（调用方自己去掉最后一根未收盘的）。
    每根：{"open_time","open","high","low","close","volume"}。"""
    data = _http_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
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


def compute_indicators(symbol: str) -> dict | None:
    """只拉 15 分钟 K 线，算出震荡信号要用的指标。失败返回 None。
    只用已收盘的 K 线（去掉最后一根未收盘的）。
    区间 = 最近 24 根 15 分钟 K 线（6 小时）的最高价 / 最低价。"""
    k15 = get_klines(symbol, "15m", 40)
    if len(k15) < ATR_PERIOD + 2 or len(k15) < ADX_PERIOD * 2 + 1 or len(k15) < RANGE_LOOKBACK + 1:
        log(f"{symbol} K 线不够，指标算不出来，等下一根 15 分钟 K 线。")
        return None
    closed15 = k15[:-1]
    atr_value = atr(closed15, ATR_PERIOD)
    adx_value, _, _ = adx(closed15, ADX_PERIOD)
    # 区间：最近 24 根 15 分钟 K 线（6 小时）的最高/最低
    recent = closed15[-RANGE_LOOKBACK:]
    range_top = max(k["high"] for k in recent)
    range_bottom = min(k["low"] for k in recent)
    return {
        "candle_open_time": closed15[-1]["open_time"],
        "price": closed15[-1]["close"],
        "atr": atr_value,
        "adx15": adx_value,
        "range_top": range_top,
        "range_bottom": range_bottom,
        "midline": (range_top + range_bottom) / 2,
    }


# ---------- 下单 ----------
def place_order_dry_run(signal: dict, qty: float, margin_usdt: float) -> None:
    """Dry-Run：只记录，不下单。"""
    log(
        "[DRY-RUN] 本来会下单: "
        f"{signal['side']} {signal.get('symbol', '?')} qty={qty:.5f} 保证金={margin_usdt:.2f} USDT "
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
    log("BTC+ETH 合约震荡机器人启动（震荡版：高空低多，2026-11-11 用户确认；和德国趋势版互补）")
    log(f"标的: {', '.join(SYMBOLS)} | Dry-Run: {DRY_RUN} | 实盘开关: {LIVE_TRADING_ENABLED}")
    log("策略: 15m 震荡高空低多（最近 6 小时区间上下沿，ADX<25 才做，现价到上沿 25% 做空、下沿 25% 做多）；"
        "止损沿外 0.5 倍 ATR，止盈对侧沿，盈亏比约 1:2 到 1:4；赚 1 倍风险保本；"
        "进场限价单，止损市价单；冷却 15 分钟（止损后 30 分钟）；"
        "每个币种一天最多 10 单、同时最多 1 单（全账户同时最多 2 单）")
    log("风控: 10 倍 / 单笔保证金<=200U / 单笔风险目标 10U（本金 0.5%）/ 单笔亏损硬上限 40U / "
        f"日亏熔断 {MAX_DAILY_LOSS_USDT:.0f}U / 连亏 3 单休息 1 小时 / 大消息前后 30 分钟不交易")
    if not DRY_RUN and not LIVE_TRADING_ENABLED:
        log("已拒绝启动：DRY_RUN=False 但未在 config.json 开 live_trading_enabled。")
        return
    if not DRY_RUN:
        load_api_credentials()  # 校验 Key 存在，不打印 Secret
        log("实盘模式：API 凭证已加载（Secret 不打印）。")

    risk = RiskManager(SYMBOLS)
    stop_file = Path(__file__).resolve().parent / "STOP"
    state_file = Path(__file__).resolve().parent / "state.json"
    positions: dict[str, dict | None] = {s: None for s in SYMBOLS}
    last_eval_candle: dict[str, int | None] = {s: None for s in SYMBOLS}
    halted_logged = False
    # 持仓时主循环 5 秒一轮：空仓币种每 60 秒最多判断一次（否则每 5 秒拉 3 组 K 线、刷屏日志）；
    # 持仓心跳每 20 秒记一行（止损每 5 秒都查，只是不每轮都写日志）。
    last_flat_eval: dict[str, float] = {s: 0.0 for s in SYMBOLS}
    last_heartbeat_log: dict[str, float] = {s: 0.0 for s in SYMBOLS}

    def save_state() -> None:
        state_file.write_text(json.dumps({
            "date": risk.current_date,
            **risk.state_dict(),
            "positions": positions,
        }, ensure_ascii=False), encoding="utf-8")

    # 断点续跑：持仓不论日期都恢复（跨 UTC 午夜重启也不丢单）；
    # 当日计数只在同一天才恢复，换了日期就从 0 开始。
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            positions = {s: (saved.get("positions") or {}).get(s) for s in SYMBOLS}
            if saved.get("date") == utc_today():
                risk.restore_state(saved)
                detail = "；".join(
                    f"{s} 已开 {risk.trades_today[s]} 单、持仓 {'有' if positions[s] else '无'}"
                    for s in SYMBOLS
                )
                log(f"恢复今日状态: 今日盈亏 {risk.daily_pnl_usdt:+.2f} U；{detail}")
            else:
                for s in SYMBOLS:
                    if positions[s]:
                        log(f"跨日重启：恢复未平持仓 {s} {positions[s]['side']} "
                            f"@{positions[s]['entry']:.1f}，今日计数从 0 开始。")
        except Exception:  # noqa: BLE001
            pass
    else:
        # state.json 只存在容器本地文件里，Cloady 重新部署/重启容器会丢；
        # 丢了当日熔断计数就从 0 开始（2026-11-10 晚就重置过两次），
        # 必须在日志里留一句，免得静默重置后多亏。
        log("未找到 state.json：当日计数从 0 开始（首次启动，或容器重启导致状态丢失；注意当日熔断计数可能已重置）。")
        # 如果不是 UTC 日初（00:00-01:00），很可能是容器中途重启，今天前面可能已经亏过钱，
        # 计数丢了就看不出来：把当日熔断线收紧一半（100 U -> 50 U）兜底，UTC 午夜自动恢复 100 U。
        _utc_hour = datetime.now(timezone.utc).hour
        if _utc_hour >= 1:
            risk.apply_conservative_halt()
            log(f"状态丢失且现在是 UTC 当天 {_utc_hour:02d} 点（非日初）：今日熔断线从 "
                f"{MAX_DAILY_LOSS_USDT:.0f} U 收紧到 {CONSERVATIVE_DAILY_LOSS_USDT:.0f} U，"
                f"免得今天实际亏超 {MAX_DAILY_LOSS_USDT:.0f} U；UTC 午夜自动恢复。")

    # 启动预热：跳过当前这根 15 分钟 K 线，等下一根收盘再判断，
    # 避免重启后几秒钟就按一根快收完的 K 线立即开单。
    _warmed = False
    for symbol in SYMBOLS:
        try:
            _warm = compute_indicators(symbol)
            if _warm is not None:
                last_eval_candle[symbol] = _warm["candle_open_time"]
                _warmed = True
        except Exception:  # noqa: BLE001
            pass
    if _warmed:
        log("启动预热：已跳过当前 15 分钟 K 线，等下一根收盘再判断。")

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
                log(f"今日净亏损已达 {risk.daily_loss_limit:.0f} U 熔断，停手，等 UTC 午夜自动清零。")
                halted_logged = True
            time.sleep(60)
            continue
        halted_logged = False

        now = time.time()
        any_holding = False

        for symbol in SYMBOLS:
            price = get_price_dry_run(symbol)
            if price is None:
                continue
            position = positions[symbol]

            # ---------- 持仓管理（持仓时每 5 秒查一次止盈/止损/保本）----------
            if position is not None:
                any_holding = True
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
                        log(f"保本触发：{symbol} {side} 已赚 1 倍风险，止损移到成本价 {entry:.1f}。")
                        save_state()  # 保本后的止损要落盘，重启也不丢
                    elif side == "SHORT" and price <= entry - risk_distance:
                        position["stop_loss"] = entry
                        position["breakeven_done"] = True
                        stop_loss = entry
                        log(f"保本触发：{symbol} {side} 已赚 1 倍风险，止损移到成本价 {entry:.1f}。")
                        save_state()  # 保本后的止损要落盘，重启也不丢

                hit_tp = (side == "LONG" and price >= take_profit) or \
                         (side == "SHORT" and price <= take_profit)
                hit_sl = (side == "LONG" and price <= stop_loss) or \
                         (side == "SHORT" and price >= stop_loss)
                if hit_tp or hit_sl:
                    was_sl = hit_sl and not position["breakeven_done"]
                    pnl = close_pnl(position, price, exit_is_maker=hit_tp)
                    kind = "止盈" if hit_tp else ("止损" if was_sl else "保本出场")
                    log(f"平仓 [{kind}]：{symbol} {side} 入场 {entry:.1f} 出场 {price:.1f}，"
                        f"本单盈亏 {pnl:+.2f} U，今日累计 {risk.daily_pnl_usdt + pnl:+.2f} U。")
                    risk.record_trade_closed(symbol, pnl, was_sl=was_sl, now=now)
                    positions[symbol] = None
                    save_state()
                    continue
                # 持仓心跳：止损/止盈/保本每 5 秒都查，但日志每 20 秒记一行，
                # 免得 5 秒一行把日志页尾部刷满；价格尖峰 5 秒内就能抓住。
                if now - last_heartbeat_log[symbol] >= HEARTBEAT_LOG_SECONDS:
                    last_heartbeat_log[symbol] = now
                    float_pnl = (price - entry) * position["qty"] if side == "LONG" else (entry - price) * position["qty"]
                    log(f"持仓中：{symbol} {side} @{entry:.1f}，现价 {price:.1f}，浮盈 {float_pnl:+.2f} U，"
                        f"止损 {stop_loss:.1f}，止盈 {take_profit:.1f}，"
                        f"保本{'已移' if position['breakeven_done'] else '未触发'}。")
                continue

            # ---------- 无持仓：等 15 分钟 K 线收盘再判断 ----------
            # 持仓时主循环 5 秒一轮，空仓币种不能每 5 秒拉 3 组 K 线：60 秒最多判断一次。
            if now - last_flat_eval[symbol] < 55:
                continue
            last_flat_eval[symbol] = now
            indicators = compute_indicators(symbol)
            if indicators is None:
                continue
            if indicators["candle_open_time"] == last_eval_candle[symbol]:
                # 这根 15 分钟 K 线已经判断过，等下一根
                log(f"无信号，{symbol} 现价 {price:.1f}，继续等 1 分钟。")
                continue
            last_eval_candle[symbol] = indicators["candle_open_time"]

            # 入场按判断时的实时价，不用滞后的 K 线收盘价（2026-11-10 的教训：
            # 收盘价 72964.9 开单时实时价已到约 73290，差了 325 点）
            signal = evaluate(price=price, atr=indicators["atr"],
                              adx15=indicators["adx15"],
                              range_top=indicators["range_top"],
                              range_bottom=indicators["range_bottom"],
                              log=log)
            if signal is None:
                log(f"无信号，{symbol} 现价 {price:.1f}（区间 {indicators['range_bottom']:.1f}-{indicators['range_top']:.1f}），继续等。")
                continue

            signal["symbol"] = symbol
            log(f"信号触发: {symbol} {signal['side']} @ {signal['entry']:.1f} 原因: {signal['reason']}")

            # 全账户同时持仓上限（每个币种最多 1 单）
            open_count = sum(1 for p in positions.values() if p is not None)
            if open_count >= MAX_OPEN_POSITIONS * len(SYMBOLS):
                log(f"风控拦截：同时持仓已满 {open_count} 单，不开 {symbol}。")
                continue

            ok, reason = risk.can_trade(symbol, now=now,
                                        votes=signal.get("votes", 0))
            if not ok:
                log(f"风控拦截：{reason}")
                continue
            if reason:
                # 强信号跳过冷却：放行，但要把原因记进日志，免得事后看不懂
                log(f"风控放行：{reason}")

            qty, margin_usdt, max_loss = risk.size_position(
                entry=signal["entry"], stop_loss=signal["stop_loss"]
            )
            if qty <= 0:
                log(f"风控拦截：按止损距离算，亏损会超 40 U 上限（算出 {max_loss:.2f} U），放弃这单。")
                continue
            signal["max_loss_usdt"] = max_loss

            # 下单前校验：没止损止盈、方向不对、亏损超上限，一律拒绝
            try:
                risk.validate_order(signal)
            except ValueError as e:
                log(f"风控拦截：{e}")
                continue

            if DRY_RUN:
                place_order_dry_run(signal, qty, margin_usdt)
            else:
                place_order_live(signal, qty, margin_usdt)
            risk.record_trade_opened(symbol, now=now)
            positions[symbol] = {
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

        # 有持仓时 5 秒一轮（止损查得勤，滑点小），没持仓时 60 秒一轮
        time.sleep(HOLDING_POLL_SECONDS if any_holding else FLAT_POLL_SECONDS)


if __name__ == "__main__":
    main()
