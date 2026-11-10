"""
Binance BTCUSDT 永续合约短线机器人 —— 骨架版（默认 Dry-Run，不下真实单）

安全原则：
- 默认 DRY_RUN = True：只看行情、只记录“本来会下的单”，绝不调下单接口。
- 想走实盘必须同时满足：环境变量 BINANCE_LIVE_TRADING=1，
  且 config.json 里 live_trading_enabled=true，且 API Key 只开
  “读取 + 合约交易”权限，绝对不要开提现 / 转账。
- API Key / Secret 只从环境变量或 config.json 读，绝不写死在代码里。
  本仓库不含任何真实 Key、Secret、账号、密码。

风控（写死在 config.py，见 RISK 部分）：
- 只做 BTCUSDT 永续，不做山寨币
- 杠杆 5 倍
- 一单最多 200 USDT 保证金
- 每单必须带止损止盈，否则拒绝下单
- 单笔最大亏损 40 USDT（本金 2000 的 2%），目标控制在 20-40 USDT
- 一天最多亏 100 USDT，达到即停手
- 一天最多 30 单
"""
import json
import os
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    DRY_RUN,
    LIVE_TRADING_ENABLED,
    LONG_TRIGGER,
    MA_FAST_PERIOD,
    MA_SLOW_PERIOD,
    MAX_DAILY_LOSS_USDT,
    MAX_TRADES_PER_DAY,
    MIDLINE_WINDOW_MINUTES,
    SYMBOL,
    TREND_SHORT_TRIGGER,
    load_api_credentials,
)
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


def get_price_dry_run() -> float | None:
    """Dry-Run 下用币安公开行情接口读价（无需 API Key，不交易）。"""
    try:
        import requests

        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": SYMBOL},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:  # noqa: BLE001 - 骨架里只记日志，不崩
        log(f"读价失败: {e}")
        return None


def place_order_dry_run(signal: dict, qty: float, margin_usdt: float) -> None:
    """Dry-Run：只记录，不下单。"""
    log(
        "[DRY-RUN] 本来会下单: "
        f"{signal['side']} {SYMBOL} qty={qty:.5f} 保证金={margin_usdt:.2f} USDT "
        f"入场~{signal['entry']:.1f} 止损={signal['stop_loss']:.1f} "
        f"止盈1={signal['take_profit_1']:.1f} 止盈2={signal.get('take_profit_2') or '-'} "
        f"预期最大亏损={signal['max_loss_usdt']:.2f} USDT"
    )


def place_order_live(signal: dict, qty: float, margin_usdt: float) -> None:
    """
    实盘下单入口（骨架：默认永不执行）。

    只有在 DRY_RUN=False 且 LIVE_TRADING_ENABLED=True 时才会被调用，
    且调用前 RiskManager 已校验过：止损止盈齐全、单笔亏损 <= 40 USDT、
    当日亏损未超 100 USDT、当日单数未超 30 单。
    真正接通时在这里用 python-binance 下单，并立即挂止损止盈单。
    当前版本为防止误操作，直接拒绝执行。
    """
    raise RuntimeError(
        "实盘下单未接通：本骨架默认 Dry-Run。请先在 README.md 里完成 "
        "API 权限检查、风控确认和试运行，再由人工打开实盘开关。"
    )


def main() -> None:
    log("=" * 60)
    log("BTCUSDT 合约短线机器人启动（组合自适应版）")
    log(f"标的: {SYMBOL} | Dry-Run: {DRY_RUN} | 实盘开关: {LIVE_TRADING_ENABLED}")
    log("策略: 趋势3单 + 小区间200点 + 大区间1000点，按市场情况合理选用；碰边确认才进，分界线滚动跟市场走，均线金叉只做多、死叉只做空，同一方向10分钟冷却，一天总共最多30单")
    log("风控: 5倍 / 单笔保证金<=200U / 单笔亏损<=40U / 日亏<=100U / 趋势单日最多3单 / 总共日最多30单")
    if not DRY_RUN and not LIVE_TRADING_ENABLED:
        log("已拒绝启动：DRY_RUN=False 但未在 config.json 开 live_trading_enabled。")
        return
    if not DRY_RUN:
        load_api_credentials()  # 校验 Key 存在，不打印 Secret
        log("实盘模式：API 凭证已加载（Secret 不打印）。")

    risk = RiskManager()
    price_history: deque = deque(maxlen=MIDLINE_WINDOW_MINUTES)  # 最近 60 分钟现价，算均线和滚动分界线
    prev_price: float | None = None
    closes_above_long_trigger = 0  # 连续站稳 76600 的 1 分钟次数
    closes_below_trend_short = 0  # 连续收在 75200 下方的 1 分钟次数
    stop_file = Path(__file__).resolve().parent / "STOP"
    state_file = Path(__file__).resolve().parent / "state.json"

    # 断点续跑：读回当天的已用单数 / 已亏（只做本地记录）
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            if saved.get("date") == str(date.today()):
                risk.trades_today = saved.get("trades_today", 0)
                risk.trend_trades_today = saved.get("trend_trades_today", 0)
                risk.daily_loss_usdt = saved.get("daily_loss_usdt", 0.0)
                risk.last_trade_at = {k: float(v) for k, v in saved.get("last_trade_at", {}).items()}
                log(f"恢复今日状态: 已开 {risk.trades_today} 单 (其中趋势 {risk.trend_trades_today} 单), 今日亏损 {risk.daily_loss_usdt:.2f} U")
        except Exception:  # noqa: BLE001
            pass

    while True:
        if stop_file.exists():
            log("检测到 STOP 文件，一键停机，退出。")
            break
        if risk.should_stop_for_day():
            log("今日亏损已达 100 U 上限，停手，不再开单。")
            break

        price = get_price_dry_run()
        if price is None:
            time.sleep(60)
            continue

        if price >= LONG_TRIGGER:
            closes_above_long_trigger += 1
        else:
            closes_above_long_trigger = 0

        if price < TREND_SHORT_TRIGGER:
            closes_below_trend_short += 1
        else:
            closes_below_trend_short = 0

        price_history.append(price)
        # 方向指标：快线（5 分钟均线）、慢线（20 分钟均线）、滚动分界线（最近 60 分钟高低点中点）
        if len(price_history) >= MA_SLOW_PERIOD:
            ma_fast = sum(list(price_history)[-MA_FAST_PERIOD:]) / MA_FAST_PERIOD
            ma_slow = sum(list(price_history)[-MA_SLOW_PERIOD:]) / MA_SLOW_PERIOD
            midline = (max(price_history) + min(price_history)) / 2
        else:
            ma_fast = ma_slow = midline = None  # 预热期数据不够，不过滤

        signal = evaluate(price=price, prev_price=prev_price,
                          closes_above_long_trigger=closes_above_long_trigger,
                          closes_below_trend_short=closes_below_trend_short,
                          ma_fast=ma_fast, ma_slow=ma_slow, midline=midline)
        prev_price = price

        if signal is None:
            log(f"无信号，BTC 现价 {price:.1f}，继续等 1 分钟。")
            time.sleep(60)
            continue

        log(f"信号触发: {signal['side']} @ {price:.1f} 原因: {signal['reason']} 模式: {signal.get('mode', '?')}")
        now = time.time()
        if not risk.can_trade(mode=signal.get("mode", "small_range"),
                              side=signal["side"], now=now):
            log("风控拦截：今日单数（趋势单最多3单 / 总共最多30单）、亏损已达上限，或同一方向 10 分钟冷却期内，不开单。")
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
            # Dry-Run 下不计入真实盈亏，只计单数，避免反复刷同一信号
            risk.record_trade_opened(mode=signal.get("mode", "small_range"),
                                         side=signal["side"], now=now)
        else:
            place_order_live(signal, qty, margin_usdt)
            risk.record_trade_opened(mode=signal.get("mode", "small_range"),
                                     side=signal["side"], now=now)

        state_file.write_text(json.dumps({
            "date": str(date.today()),
            "trades_today": risk.trades_today,
            "trend_trades_today": risk.trend_trades_today,
            "last_trade_at": risk.last_trade_at,
            "daily_loss_usdt": risk.daily_loss_usdt,
        }, ensure_ascii=False), encoding="utf-8")

        time.sleep(60)


if __name__ == "__main__":
    main()
