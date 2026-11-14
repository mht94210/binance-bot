"""
Binance BTC+ETH 永续合约短线机器人 —— 10 倍快进快出方案（默认 Dry-Run，不下真实单）

版本：2026-11-10 用户确认（综合 3Commas / Cryptohopper / Pionex / Freqtrade 等做法后定）。
2026-11-11 用户确认加 ETHUSDT，双币种同规则各跑各的。

安全原则：
- 默认 DRY_RUN = True：只看行情、只记录“本来会下的单”，绝不调下单接口。
- 想走实盘必须同时满足三项（缺一个都不开，代码里真的检查，不只写注释）：
  环境变量 BINANCE_DRY_RUN=0，且 config.json 里 live_trading_enabled=true，
  且环境变量 BINANCE_LIVE_TRADING=1。且 API Key 只开
  “读取 + 合约交易”权限，绝对不要开提现 / 转账。
- API Key / Secret 只从环境变量或 config.json 读，绝不写死在代码里。
  本仓库不含任何真实 Key、Secret、账号、密码。
- 实盘持仓以交易所为准：止盈止损挂在交易所，轮询订单状态判断成交，
  不再用 ticker 价模拟成交；挂保护失败立刻市价平仓，绝不留裸仓。
  启动时拉一次交易所持仓对账，孤儿持仓绝不能丢。

策略（写死在 config.py）：
- 10 倍杠杆，一单最多 400 U 保证金，名义仓位 4000 U
- 信号：15 分钟 K 线收盘时判断（4 小时 EMA200 + 1 小时 EMA200 同向定方向 + 15 分钟 ADX>30 + 15 分钟均线，3/3 全票）
- 止损 2 倍 ATR，止盈 3 倍 ATR，盈亏比 1:1.5；止盈目标 < 0.30% 不做
- 赚到 1 倍风险就把止损移到成本价（保本）；进场限价单，止损市价单
- 冷却 15 分钟，止损后 30 分钟；每个币种每天最多 10 单、同时最多 1 单
  （全账户同时最多 2 单、每天最多 20 单）
  （2026-11-11 用户决定：3/3 强信号可跳过冷却直接进场，2/3 票继续等；
  连亏暂停、消息禁入、每日熔断是硬刹车，再强也不能跳过）
- 熔断：一天净亏 100 U（本金 5%）停；连亏 3 单休息 1 小时；大消息前后 30 分钟不交易
- 当日计数按 UTC 日期自动清零（2026-11-07 的教训：旧版只在重启时清零，跑着不动就一直卡在上限）
- 入场按判断时的实时价，不用滞后的 5 分钟 K 线收盘价（2026-11-10 的教训）
"""
import json
import math
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
    EMA_PERIOD_1H,
    EMA_PERIOD_4H,
    ENTRY_FILL_TIMEOUT_SECONDS,
    ENTRY_POLL_SECONDS,
    FLAT_POLL_SECONDS,
    HEARTBEAT_LOG_SECONDS,
    HOLDING_POLL_SECONDS,
    LEVERAGE,
    LIVE_TRADING_ENABLED,
    MA_FAST_PERIOD,
    MA_SLOW_PERIOD,
    MAKER_FEE_PCT,
    MAX_DAILY_LOSS_USDT,
    MAX_OPEN_POSITIONS,
    SYMBOLS,
    TAKER_FEE_PCT,
    VOL_SMA_PERIOD,
    live_trading_active,
    live_trading_missing,
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
    """拉三组 K 线，算出信号要用的全部指标。失败返回 None。
    只用已收盘的 K 线（去掉每组最后一根未收盘的）。
    信号周期 15 分钟（2026-11-11 用户决定少而精：5 分钟噪音太多）；
    4 小时 EMA200 做大趋势过滤，1 小时 EMA200 定方向。"""
    k15 = get_klines(symbol, "15m", 40)
    k1h = get_klines(symbol, "1h", 210)
    k4h = get_klines(symbol, "4h", 210)
    if len(k15) < ATR_PERIOD + 2 or len(k15) < ADX_PERIOD * 2 + 1 or len(k1h) < EMA_PERIOD_1H + 1 or len(k4h) < EMA_PERIOD_4H + 1:
        log(f"{symbol} K 线不够，指标算不出来，等下一根 15 分钟 K 线。")
        return None
    closed15 = k15[:-1]
    closed1h = k1h[:-1]
    closed4h = k4h[:-1]
    closes15 = [k["close"] for k in closed15]
    closes1h = [k["close"] for k in closed1h]
    closes4h = [k["close"] for k in closed4h]
    volumes15 = [k["volume"] for k in closed15]
    atr_value = atr(closed15, ATR_PERIOD)
    adx_value, _, _ = adx(closed15, ADX_PERIOD)
    # 滚动分界线：最近 4 根 15 分钟 K 线（60 分钟）最高最低价的中点，跟着市场走
    recent = closed15[-4:]
    midline = (max(k["high"] for k in recent) + min(k["low"] for k in recent)) / 2
    return {
        "candle_open_time": closed15[-1]["open_time"],
        "price": closed15[-1]["close"],
        "atr": atr_value,
        "ema200_1h": ema(closes1h, EMA_PERIOD_1H),
        "ema200_4h": ema(closes4h, EMA_PERIOD_4H),
        "adx15": adx_value,
        "ma_fast": sma(closes15, MA_FAST_PERIOD),
        "ma_slow": sma(closes15, MA_SLOW_PERIOD),
        "midline": midline,
        "volume": closed15[-1]["volume"],
        "volume_sma": sma(volumes15, VOL_SMA_PERIOD),
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


class LiveOrderError(RuntimeError):
    """实盘下单失败（挂保护失败、市价平仓失败等，调用方必须记日志、绝不静默）。"""


class LiveOrderTimeout(LiveOrderError):
    """入场单在有界等待内没成交（已撤单）。"""


class LiveOrderRejected(LiveOrderError):
    """入场单被交易所拒绝（比如 Post Only 会立即成交、数量/价格不合规）。"""


class LiveTrader:
    """实盘下单器：封装 python-binance 调用，只在实盘模式创建。

    约定（币安 USD-M 永续）：
    - 入场：LIMIT + timeInForce=GTX（Post Only，只吃 maker 费）。
    - 止损：STOP_MARKET + closePosition="true"（整仓平掉，不怕部分成交后数量对不上）。
    - 止盈：LIMIT + reduceOnly="true"，数量 = 实际成交量。
    - 布尔参数必须传字符串 "true"/"false"（python-binance 文档要求），不能传 Python 布尔值。
    - 订单状态以交易所返回为准；任何一步失败都抛异常或返回 False，绝不靠猜。
    """

    def __init__(self, client, log_fn=None):
        self.client = client
        self.log = log_fn or log
        self._tick: dict[str, float] = {}
        self._tick_decimals: dict[str, int] = {}
        self._step: dict[str, float] = {}
        self._step_decimals: dict[str, int] = {}
        self._min_qty: dict[str, float] = {}
        self._min_notional: dict[str, float] = {}

    # ---------- 精度（下单前必须从交易所拿 filters，否则价格/数量可能被拒）----------
    @staticmethod
    def _decimals(step_str: str) -> int:
        if "." not in step_str:
            return 0
        return len(step_str.split(".")[1].rstrip("0")) or 0

    def load_filters(self) -> None:
        """从 futures_exchange_info 读 PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL。拿不到就抛错，
        宁可拒绝启动，也不拿猜出来的精度下单。"""
        info = self.client.futures_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") not in SYMBOLS:
                continue
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            tick = filters["PRICE_FILTER"]["tickSize"]
            step = filters["LOT_SIZE"]["stepSize"]
            self._tick[s["symbol"]] = float(tick)
            self._tick_decimals[s["symbol"]] = self._decimals(tick)
            self._step[s["symbol"]] = float(step)
            self._step_decimals[s["symbol"]] = self._decimals(step)
            self._min_qty[s["symbol"]] = float(filters["LOT_SIZE"]["minQty"])
            notional = filters.get("MIN_NOTIONAL", {}).get("notional", "0")
            self._min_notional[s["symbol"]] = float(notional or 0)
        missing = [s for s in SYMBOLS if s not in self._tick]
        if missing:
            raise RuntimeError(f"交易所精度信息里没有 {missing}，无法安全下单。")

    def round_price(self, symbol: str, price: float) -> float:
        """价格对到最近一个 tick（误差不到 1 个 tick，相对止损距离可忽略）。"""
        tick = self._tick[symbol]
        return round(round(price / tick) * tick, self._tick_decimals[symbol])

    def round_qty(self, symbol: str, qty: float) -> float:
        """数量向下对到 step（向下取，绝不多买）。"""
        step = self._step[symbol]
        return round(math.floor(qty / step) * step, self._step_decimals[symbol])

    def _check_min_notional(self, symbol: str, price: float, qty: float) -> None:
        if qty < self._min_qty[symbol]:
            raise LiveOrderRejected(f"数量 {qty} 低于最小下单量 {self._min_qty[symbol]}。")
        min_notional = self._min_notional[symbol]
        if min_notional > 0 and qty * price < min_notional:
            raise LiveOrderRejected(f"名义价值 {qty * price:.2f} U 低于最小 {min_notional} U。")

    # ---------- 账户准备（启动时一次）----------
    def setup_account(self) -> None:
        """逐仓 + 10 倍杠杆 + 精度。杠杆/逐仓设不上只记警告（可能已经是目标值），
        但精度拿不到必须抛错。"""
        for symbol in SYMBOLS:
            try:
                res = self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
                self.log(f"实盘准备：{symbol} 杠杆设为 {res.get('leverage', LEVERAGE)}x。")
            except Exception as e:  # noqa: BLE001 - 杠杆可能已是 10x，记一笔继续
                self.log(f"实盘准备警告：{symbol} 改杠杆失败（{e}），请确认该币种已是 {LEVERAGE}x，否则不要实盘。")
            try:
                self.client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
            except Exception as e:  # noqa: BLE001 - 已是逐仓时会报错，记一笔继续
                self.log(f"实盘准备：{symbol} 改逐仓未成功（{e}），可能已是逐仓。")
        self.load_filters()
        self.log("实盘准备：交易所精度已加载。")

    # ---------- 下单 ----------
    def place_entry(self, signal: dict, qty: float,
                    sleep_fn=time.sleep) -> dict:
        """Post Only 限价入场，有界等待成交。
        返回 {"order_id", "qty", "avg_price"}（qty/avg_price 是实际成交量/均价）。
        超时未成交：撤单后抛 LiveOrderTimeout；部分成交：按实际成交量建仓；
        被交易所拒绝：抛 LiveOrderRejected。"""
        symbol = signal["symbol"]
        side = signal["side"]
        price = self.round_price(symbol, signal["entry"])
        qty_r = self.round_qty(symbol, qty)
        self._check_min_notional(symbol, price, qty_r)
        order_side = "BUY" if side == "LONG" else "SELL"
        try:
            order = self.client.futures_create_order(
                symbol=symbol, side=order_side, type="LIMIT",
                quantity=qty_r, price=price, timeInForce="GTX")
        except Exception as e:  # noqa: BLE001 - 区分“会立即成交被拒”和其它异常
            msg = str(e)
            code = getattr(e, "code", None)
            if "immediately" in msg.lower() or code in (-2010, -2011):
                raise LiveOrderRejected(f"Post Only 入场被拒（会立即成交）：{msg}")
            raise LiveOrderError(f"入场下单异常：{msg}")
        order_id = order["orderId"]
        deadline = time.time() + ENTRY_FILL_TIMEOUT_SECONDS
        while time.time() < deadline:
            sleep_fn(ENTRY_POLL_SECONDS)
            try:
                cur = self.client.futures_get_order(symbol=symbol, orderId=order_id)
            except Exception as e:  # noqa: BLE001 - 查单失败不致命，下轮再查
                self.log(f"查入场单 {order_id} 失败：{e}，继续等。")
                continue
            status = cur.get("status")
            if status == "FILLED":
                return {"order_id": order_id,
                        "qty": float(cur.get("executedQty") or qty_r),
                        "avg_price": float(cur.get("avgPrice") or 0) or price}
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                raise LiveOrderRejected(f"入场单状态 {status}，没成交。")
        # 超时：查最后状态，撤掉没成交的部分
        try:
            cur = self.client.futures_get_order(symbol=symbol, orderId=order_id)
        except Exception as e:  # noqa: BLE001
            self.log(f"超时后查入场单 {order_id} 失败：{e}，按未成交处理。")
            cur = {}
        filled = float(cur.get("executedQty") or 0)
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
        except Exception as e:  # noqa: BLE001 - 可能已全部成交，撤单报错正常
            self.log(f"超时撤单 {order_id} 失败（可能已成交）：{e}")
        if filled <= 0:
            raise LiveOrderTimeout(
                f"入场单 {ENTRY_FILL_TIMEOUT_SECONDS} 秒未成交，已撤单。")
        avg = float(cur.get("avgPrice") or 0) or price
        self.log(f"入场部分成交：成交 {filled}/{qty_r}，按实际成交量建仓。")
        return {"order_id": order_id, "qty": filled, "avg_price": avg}

    def place_stop(self, symbol: str, side: str, stop_price: float) -> int:
        """挂止损市价单（closePosition，全仓平掉）。返回订单 id。"""
        exit_side = "SELL" if side == "LONG" else "BUY"
        order = self.client.futures_create_order(
            symbol=symbol, side=exit_side, type="STOP_MARKET",
            stopPrice=self.round_price(symbol, stop_price), closePosition="true")
        return order["orderId"]

    def place_take_profit(self, symbol: str, side: str, qty: float, tp_price: float) -> int:
        """挂止盈限价单（reduceOnly）。返回订单 id。"""
        exit_side = "SELL" if side == "LONG" else "BUY"
        order = self.client.futures_create_order(
            symbol=symbol, side=exit_side, type="LIMIT",
            quantity=self.round_qty(symbol, qty),
            price=self.round_price(symbol, tp_price),
            timeInForce="GTC", reduceOnly="true")
        return order["orderId"]

    def place_protection(self, symbol: str, side: str, qty: float,
                         stop_loss: float, take_profit: float) -> dict:
        """成交后立刻挂止损 + 止盈。先挂止损（保护优先），再挂止盈。
        任一失败：撤掉已挂上的那条，抛 LiveOrderError（调用方立刻市价平仓）。"""
        try:
            sl_id = self.place_stop(symbol, side, stop_loss)
        except Exception as e:  # noqa: BLE001
            raise LiveOrderError(f"挂止损失败：{e}")
        try:
            tp_id = self.place_take_profit(symbol, side, qty, take_profit)
        except Exception as e:  # noqa: BLE001
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=sl_id)
            except Exception:  # noqa: BLE001 - 撤不掉也继续，调用方会市价平仓
                pass
            raise LiveOrderError(f"挂止盈失败：{e}")
        return {"sl_order_id": sl_id, "tp_order_id": tp_id}

    def market_close(self, symbol: str, side: str, qty: float) -> dict:
        """市价单平掉指定数量（reduceOnly）。挂保护失败后的兜底，绝不留裸仓。"""
        exit_side = "SELL" if side == "LONG" else "BUY"
        return self.client.futures_create_order(
            symbol=symbol, side=exit_side, type="MARKET",
            quantity=self.round_qty(symbol, qty), reduceOnly="true")

    # ---------- 持仓查询 ----------
    def poll_exit_orders(self, symbol: str, position: dict) -> dict:
        """查止盈/止损订单状态。返回 {"closed", "kind"("TP"/"SL"), "exit_price", "exit_order_id"}。
        成交的那条一经发现，立刻撤掉另一条（免得反手又成交）。"""
        result = {"closed": False, "kind": None, "exit_price": None, "exit_order_id": None}
        for kind, oid in (("TP", position.get("tp_order_id")),
                          ("SL", position.get("sl_order_id"))):
            if not oid:
                continue
            try:
                order = self.client.futures_get_order(symbol=symbol, orderId=oid)
            except Exception as e:  # noqa: BLE001 - 查单失败不等于没成交，下轮再查
                self.log(f"查订单 {oid} 失败：{e}，下轮再查。")
                continue
            if order.get("status") == "FILLED":
                result["closed"] = True
                result["kind"] = kind
                result["exit_price"] = float(order.get("avgPrice") or 0) or None
                result["exit_order_id"] = oid
                other = position.get("sl_order_id") if kind == "TP" else position.get("tp_order_id")
                if other:
                    try:
                        self.client.futures_cancel_order(symbol=symbol, orderId=other)
                    except Exception:  # noqa: BLE001 - 对方可能已撤/已成交，忽略
                        pass
                break
        return result

    def get_position_state(self, symbol: str) -> dict:
        """交易所持仓快照：{"amt", "entry", "mark", "leverage"}。"""
        infos = self.client.futures_position_information(symbol=symbol)
        for info in infos or []:
            if info.get("symbol") == symbol:
                return {"amt": float(info.get("positionAmt") or 0),
                        "entry": float(info.get("entryPrice") or 0),
                        "mark": float(info.get("markPrice") or 0),
                        "leverage": int(float(info.get("leverage") or 0))}
        return {"amt": 0.0, "entry": 0.0, "mark": 0.0, "leverage": 0}

    def get_open_orders(self, symbol: str) -> list:
        return list(self.client.futures_get_open_orders(symbol=symbol) or [])

    def get_all_orders(self, symbol: str, limit: int = 20) -> list:
        return list(self.client.futures_get_all_orders(symbol=symbol, limit=limit) or [])

    def move_stop_to_breakeven(self, symbol: str, position: dict,
                               sleep_fn=time.sleep) -> bool:
        """保本：撤掉旧止损，在成本价重挂 STOP_MARKET。成功返回 True，并更新
        position["sl_order_id"]。重挂失败立即重试 3 次（每次 1 秒），还失败就
        返回 False（旧止损可能已撤，下轮再试；原止盈单一直都在）。"""
        old_sl = position.get("sl_order_id")
        if old_sl:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=old_sl)
            except Exception as e:  # noqa: BLE001 - 旧止损可能已成交，调用方下轮会发现平仓
                self.log(f"保本撤旧止损 {old_sl} 失败：{e}，本次不移动。")
                return False
        for attempt in range(3):
            try:
                new_id = self.place_stop(symbol, position["side"], position["entry"])
                position["sl_order_id"] = new_id
                return True
            except Exception as e:  # noqa: BLE001
                self.log(f"保本重挂止损失败（第 {attempt + 1}/3 次）：{e}")
                sleep_fn(1)
        self.log(f"严重：{symbol} 保本重挂止损 3 次都失败，持仓暂无止损保护，下轮再试。")
        return False

    def fetch_fees(self, symbol: str, order_ids: list,
                   start_time_ms: int, sleep_fn=time.sleep) -> float | None:
        """拉交易所成交账单（futures_account_trades），按 orderId 汇总手续费（USDT）。
        拉不到返回 None（调用方改用估算，并记一笔）。非 USDT 手续费（比如 BNB）
        金额小，不计入但记一笔。"""
        wanted = {oid for oid in order_ids if oid}
        for attempt in range(3):
            try:
                trades = self.client.futures_account_trades(symbol=symbol, startTime=start_time_ms)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    self.log(f"拉交易所手续费账单失败：{e}，改用估算。")
                    return None
                sleep_fn(2)
        else:
            return None
        total = 0.0
        non_usdt = set()
        for t in trades or []:
            if t.get("orderId") in wanted:
                asset = t.get("commissionAsset")
                if asset == "USDT":
                    total += float(t.get("commission") or 0)
                elif asset:
                    non_usdt.add(asset)
        if non_usdt:
            self.log(f"手续费里有非 USDT 币种 {non_usdt}，未计入（金额小）。")
        return total


def log_egress_ip() -> None:
    """实盘启动时记一笔本机出口 IP：币安 API 白名单必须包含它才能交易。
    Cloady 换出口 IP 时靠这行发现新 IP，再去币安更新白名单。查不到只记警告，不影响启动。"""
    import urllib.request

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
            if ip:
                log(f"本机出口 IP：{ip}（币安 API 白名单需包含此 IP 才能交易）")
                return
        except Exception:  # noqa: BLE001 - 查不到就换下一个源
            continue
    log("出口 IP 查询失败（不影响交易；币安白名单仍需人工核对）。")


def create_live_trader(api_key: str, api_secret: str) -> LiveTrader:
    """建实盘下单器：只在这里 import python-binance，Dry-Run 完全不依赖它。
    Secret 只传进 Client，不打印、不写文件。"""
    from binance.client import Client

    client = Client(api_key, api_secret, requests_params={"timeout": 10})
    trader = LiveTrader(client)
    trader.setup_account()
    return trader


def place_order_live(signal: dict, qty: float, margin_usdt: float,
                     live_trader: LiveTrader, blocked: set) -> dict | None:
    """实盘下单入口（2026-11-14 接通：原来是故意留空的桩）。

    流程：开关三查 -> Post Only 入场（60 秒有界等待）-> 立刻挂止损+止盈。
    返回实盘持仓 dict；放弃返回 None（已记日志，调用方不要记持仓、不要计数）。
    挂保护失败：立刻市价平仓，绝不留裸仓；连市价平仓都失败：大声记日志，
    把该币种加进 blocked（暂停新开单），等人工处理 / 下次重启对账发现。
    入场价/数量用实际成交值，不用信号价（信号价只用来下限价单）。
    """
    if not live_trading_active():
        raise LiveOrderError(f"实盘开关不全（缺：{'；'.join(live_trading_missing())}），拒绝下单。")
    symbol = signal["symbol"]
    side = signal["side"]
    try:
        fill = live_trader.place_entry(signal, qty)
    except LiveOrderTimeout:
        log(f"实盘放弃：{symbol} 入场 {ENTRY_FILL_TIMEOUT_SECONDS} 秒未成交，已撤单，不记持仓。")
        return None
    except LiveOrderRejected as e:
        log(f"实盘放弃：{symbol} 入场被拒：{e}，不记持仓。")
        return None
    except LiveOrderError as e:
        log(f"实盘放弃：{symbol} 入场异常：{e}，不记持仓。")
        return None
    try:
        prot = live_trader.place_protection(
            symbol, side, fill["qty"], signal["stop_loss"], signal["take_profit_1"])
    except LiveOrderError as e:
        log(f"严重：{symbol} 挂止损/止盈失败（{e}），立刻市价平仓，绝不留裸仓。")
        try:
            live_trader.market_close(symbol, side, fill["qty"])
            log(f"已市价平掉刚开的仓：{symbol} {side} qty={fill['qty']:.5f}。")
        except Exception as e2:  # noqa: BLE001
            log(f"严重！！{symbol} 市价平仓也失败：{e2}。交易所可能仍有持仓，"
                f"请立即人工处理；该币种已暂停新开单。")
            blocked.add(symbol)
        return None
    return {
        "side": side,
        "entry": fill["avg_price"],
        "qty": fill["qty"],
        "margin": fill["qty"] * fill["avg_price"] / LEVERAGE,
        "stop_loss": signal["stop_loss"],
        "take_profit": signal["take_profit_1"],
        "breakeven_done": False,
        "opened_at": time.time(),
        "atr": signal.get("atr"),
        "live": True,
        "entry_order_id": fill["order_id"],
        "sl_order_id": prot["sl_order_id"],
        "tp_order_id": prot["tp_order_id"],
    }


def live_close_pnl(position: dict, exit_price: float, fees_usdt: float) -> float:
    """实盘平仓盈亏：实际成交价算毛利，减交易所实际手续费。"""
    qty = position["qty"]
    entry = position["entry"]
    direction = 1 if position["side"] == "LONG" else -1
    gross = direction * qty * (exit_price - entry)
    return gross - fees_usdt


def manage_live_position(symbol: str, position: dict, price: float, now: float,
                         risk: RiskManager, live_trader: LiveTrader, save_state,
                         positions: dict, last_heartbeat_log: dict) -> None:
    """实盘持仓管理（持仓时每 5 秒一轮），以交易所订单状态为准：
    1. 查止盈/止损订单：成交了就按实际成交价算盈亏（手续费按交易所账单，拉不到用估算）、
       记风控、清持仓；2. 兜底：交易所持仓已为 0 但订单没显示成交（比如人工平了），
       按现价平账并大声记一笔；3. 保本：赚到 1 倍风险就通过交易所撤单重挂，把止损移到成本价；
    4. 心跳：和 Dry-Run 同格式（监控脚本靠“持仓中”行报持仓）。
    平仓后把 positions[symbol] 清掉；没平就原地不动。"""
    try:
        res = live_trader.poll_exit_orders(symbol, position)
    except Exception as e:  # noqa: BLE001 - 查单失败不等于没成交，下轮再查
        log(f"{symbol} 查止盈/止损订单失败：{e}，下轮再查。")
        res = {"closed": False}
    if res.get("closed"):
        exit_price = res.get("exit_price")
        if exit_price is None:
            exit_price = price
            log(f"严重：{symbol} 止盈/止损已成交但没拿到成交均价，用现价 {price:.1f} 估算盈亏。")
        fees = live_trader.fetch_fees(
            symbol, [position.get("entry_order_id"), res.get("exit_order_id")],
            start_time_ms=int(position["opened_at"] * 1000))
        if fees is None:
            pnl = close_pnl(position, exit_price, exit_is_maker=(res["kind"] == "TP"))
            log(f"{symbol} 手续费按估算（交易所账单拉取失败）。")
        else:
            pnl = live_close_pnl(position, exit_price, fees)
        was_sl = (res["kind"] == "SL") and not position["breakeven_done"]
        kind = "止盈" if res["kind"] == "TP" else ("止损" if was_sl else "保本出场")
        log(f"平仓 [{kind}]：{symbol} {position['side']} 入场 {position['entry']:.1f} 出场 {exit_price:.1f}，"
            f"本单盈亏 {pnl:+.2f} U，今日累计 {risk.daily_pnl_usdt + pnl:+.2f} U。")
        risk.record_trade_closed(symbol, pnl, was_sl=was_sl, now=now)
        positions[symbol] = None
        save_state()
        return
    try:
        st = live_trader.get_position_state(symbol)
    except Exception as e:  # noqa: BLE001
        log(f"{symbol} 查交易所持仓失败：{e}，下轮再查。")
        st = None
    if st is not None and st["amt"] == 0:
        log(f"严重：{symbol} 交易所持仓已为 0，但止盈/止损订单没显示成交（可能人工平仓），"
            f"按现价 {price:.1f} 平账（盈亏为估算）。")
        pnl = close_pnl(position, price, exit_is_maker=False)
        risk.record_trade_closed(symbol, pnl, was_sl=False, now=now)
        positions[symbol] = None
        save_state()
        return
    if not position["breakeven_done"]:
        entry = position["entry"]
        risk_distance = abs(entry - position["stop_loss"])
        side = position["side"]
        if (side == "LONG" and price >= entry + risk_distance) or \
           (side == "SHORT" and price <= entry - risk_distance):
            if live_trader.move_stop_to_breakeven(symbol, position):
                position["stop_loss"] = entry
                position["breakeven_done"] = True
                log(f"保本触发：{symbol} {side} 已赚 1 倍风险，止损移到成本价 {entry:.1f}（交易所已重挂）。")
                save_state()
            else:
                log(f"{symbol} 保本移动止损失败，下轮再试（原止损仍在）。")
    if now - last_heartbeat_log[symbol] >= HEARTBEAT_LOG_SECONDS:
        last_heartbeat_log[symbol] = now
        float_pnl = (price - position["entry"]) * position["qty"] if position["side"] == "LONG" \
            else (position["entry"] - price) * position["qty"]
        log(f"持仓中：{symbol} {position['side']} @{position['entry']:.1f}，现价 {price:.1f}，浮盈 {float_pnl:+.2f} U，"
            f"止损 {position['stop_loss']:.1f}，止盈 {position['take_profit']:.1f}，"
            f"保本{'已移' if position['breakeven_done'] else '未触发'}。")


def reconcile_live_positions(live_trader: LiveTrader, positions: dict,
                             risk: RiskManager, blocked: set, save_state) -> None:
    """实盘启动对账：交易所持仓 vs state.json（只在实盘启动时跑一次）。
    - 交易所没有、state 有：持仓已在停机期间平掉（止损/止盈成交），按成交订单的均价平账；
      查不到成交价就按现价估算并大声记一笔。
    - 交易所和 state 都有：以交易所的数量/入场价为准；止损/止盈订单丢了就重挂，
      重挂不上就市价平仓。
    - 交易所里有、state 里没有（孤儿持仓）：绝不能丢。先从挂单里找止损/止盈认领；
      缺哪条按策略盈亏比（止盈 = 1.5 倍止损距离）从另一条反推重建；两条都没有
      就立刻市价平仓。认领后该币种随持仓自动停新单（每币种最多 1 单）。
    - 交易所和 state 都没有，但有挂单：多余挂单全撤掉，免得以后意外成交。
    对账本身拉不到交易所状态：该币种加进 blocked，暂停新开单，等下次重启再对。"""
    for symbol in SYMBOLS:
        try:
            st = live_trader.get_position_state(symbol)
            open_orders = live_trader.get_open_orders(symbol)
        except Exception as e:  # noqa: BLE001
            log(f"对账失败：{symbol} 拉不到交易所状态（{e}），该币种暂停新开单，等下次重启再对。")
            blocked.add(symbol)
            continue
        amt = st["amt"]
        saved = positions.get(symbol)
        if amt == 0:
            if saved is None:
                for o in open_orders:
                    try:
                        live_trader.client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                        log(f"对账：{symbol} 无持仓，撤掉多余挂单 {o['orderId']}。")
                    except Exception:  # noqa: BLE001
                        pass
                continue
            # state 有、交易所没有：停机期间已平仓，找最近成交的出场单算盈亏
            exit_price = None
            exit_order_id = None
            exit_order = None
            exit_time = -1
            try:
                for o in live_trader.get_all_orders(symbol):
                    if o.get("status") != "FILLED":
                        continue
                    if o.get("reduceOnly") not in ("true", True) and \
                            o.get("closePosition") not in ("true", True):
                        continue
                    t = int(o.get("updateTime") or o.get("time") or 0)
                    if t >= exit_time:
                        exit_time = t
                        exit_price = float(o.get("avgPrice") or 0) or None
                        exit_order_id = o["orderId"]
                        exit_order = o
            except Exception as e:  # noqa: BLE001
                log(f"对账：{symbol} 拉历史订单失败（{e}），按现价估算。")
            if exit_price is None:
                exit_price = st["mark"] or saved["entry"]
                log(f"严重：{symbol} 停机期间已平仓，但查不到成交价，按现价 {exit_price:.1f} 估算盈亏。")
            else:
                log(f"对账：{symbol} 停机期间已平仓，出场 {exit_price:.1f}（订单 {exit_order_id}）。")
            fees = live_trader.fetch_fees(
                symbol, [saved.get("entry_order_id"), exit_order_id],
                start_time_ms=int(saved["opened_at"] * 1000))
            if fees is None:
                pnl = close_pnl(saved, exit_price, exit_is_maker=False)
            else:
                pnl = live_close_pnl(saved, exit_price, fees)
            # 出场单是止损市价单且止损价不在成本价上，才算真的止损（影响冷却时长）；
            # 止损价已在成本价上就是保本出场，冷却按 15 分钟算。
            was_sl = bool(exit_order and exit_order.get("type") == "STOP_MARKET"
                          and abs(float(exit_order.get("stopPrice") or 0) - saved["entry"]) > 1e-9)
            risk.record_trade_closed(symbol, pnl, was_sl=was_sl, now=time.time())
            positions[symbol] = None
            save_state()
            continue
        # 交易所里有持仓
        side = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        entry = st["entry"] or (saved or {}).get("entry", 0.0)
        sl_order = next((o for o in open_orders if o.get("type") == "STOP_MARKET"), None)
        tp_order = next((o for o in open_orders
                         if o.get("type") == "LIMIT" and o.get("reduceOnly") in ("true", True)), None)
        if saved is None:
            log(f"严重：交易所里 {symbol} 有持仓 {side} {qty:.5f} @ {entry:.1f}，但 state.json 里没有！"
                f"已认领并接管，绝不丢单。")
        position = dict(saved) if saved else {
            "side": side, "entry": entry, "qty": qty,
            "margin": qty * entry / LEVERAGE if entry else 0.0,
            "stop_loss": 0.0, "take_profit": 0.0,
            "breakeven_done": False, "opened_at": time.time(), "atr": None,
            "live": True, "orphan": True,
            "entry_order_id": None, "sl_order_id": None, "tp_order_id": None,
        }
        position.update({"side": side, "qty": qty, "entry": entry, "live": True})
        if sl_order is not None:
            position["sl_order_id"] = sl_order["orderId"]
            position["stop_loss"] = float(sl_order.get("stopPrice") or position["stop_loss"] or entry)
        if tp_order is not None:
            position["tp_order_id"] = tp_order["orderId"]
            position["take_profit"] = float(tp_order.get("price") or position["take_profit"] or entry)
        # 止损已在成本价上 = 保本早已触发，别让主循环再撤单重挂一次
        if sl_order is not None and abs(position["stop_loss"] - entry) < 1e-9:
            position["breakeven_done"] = True
        # 缺哪条保护就重建：止盈 = 入场 ± 1.5 倍止损距离（策略原定盈亏比），止损同理反推
        need_close = False
        if sl_order is None and tp_order is not None:
            tp_dist = abs(position["take_profit"] - entry)
            sl = entry - tp_dist / 1.5 if side == "LONG" else entry + tp_dist / 1.5
            try:
                position["sl_order_id"] = live_trader.place_stop(symbol, side, sl)
                position["stop_loss"] = sl
                log(f"对账：{symbol} 止损单丢了，按止盈反推重挂止损 {sl:.1f}。")
            except Exception as e:  # noqa: BLE001
                log(f"严重：{symbol} 重挂止损失败（{e}），立刻市价平仓。")
                need_close = True
        if tp_order is None and sl_order is not None and not need_close:
            sl_dist = abs(position["stop_loss"] - entry)
            tp = entry + sl_dist * 1.5 if side == "LONG" else entry - sl_dist * 1.5
            try:
                position["tp_order_id"] = live_trader.place_take_profit(symbol, side, qty, tp)
                position["take_profit"] = tp
                log(f"对账：{symbol} 止盈单丢了，按止损反推重挂止盈 {tp:.1f}。")
            except Exception as e:  # noqa: BLE001
                log(f"严重：{symbol} 重挂止盈失败（{e}），立刻市价平仓。")
                need_close = True
        if sl_order is None and tp_order is None:
            log(f"严重：{symbol} 交易所持仓没有任何保护单（裸仓），立刻市价平仓。")
            need_close = True
        if need_close:
            try:
                live_trader.market_close(symbol, side, qty)
                log(f"对账：已市价平掉 {symbol} {side} qty={qty:.5f}。")
            except Exception as e:  # noqa: BLE001
                log(f"严重！！{symbol} 对账市价平仓也失败：{e}。交易所仍有持仓，请立即人工处理；"
                    f"该币种已暂停新开单。")
                blocked.add(symbol)
                continue
            if saved is not None:
                pnl = close_pnl(saved, st["mark"] or entry, exit_is_maker=False)
                risk.record_trade_closed(symbol, pnl, was_sl=False, now=time.time())
            positions[symbol] = None
            save_state()
            continue
        positions[symbol] = position
        save_state()
        log(f"对账：{symbol} 持仓已确认 {side} {qty:.5f} @ {entry:.1f}，"
            f"止损 {position['stop_loss']:.1f}，止盈 {position['take_profit']:.1f}。")


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
    log("BTC+ETH 合约短线机器人启动（10 倍快进快出方案，2026-11-10 用户确认；2026-11-11 加 ETH）")
    log(f"标的: {', '.join(SYMBOLS)} | Dry-Run: {DRY_RUN} | 实盘开关: {LIVE_TRADING_ENABLED} | 实盘已开: {live_trading_active()}")
    log("策略: 15m 共振少而精（4h EMA200 + 1h EMA200 定方向 + 15m ADX>30 + 15m 均线，3/3 全票）+ 量能过滤；"
        "止损 2 倍 ATR，止盈 3 倍 ATR，盈亏比 1:1.5；赚 1 倍风险保本；"
        "进场限价单，止损市价单；冷却 15 分钟（止损后 30 分钟，3/3 强信号可跳过）；"
        "每个币种一天最多 10 单、同时最多 1 单（全账户同时最多 2 单）")
    log("风控: 10 倍 / 单笔保证金<=400U / 单笔风险目标 10U（本金 0.5%）/ 单笔亏损硬上限 40U / "
        f"日亏熔断 {MAX_DAILY_LOSS_USDT:.0f}U / 连亏 3 单休息 1 小时 / 大消息前后 30 分钟不交易")
    # 启动就记出口 IP（模拟盘和实盘都记）：币安白名单必须包含它；
    # 实盘下单器建失败会直接退出，放在这里才能保证 IP 一定被记下来。
    log_egress_ip()
    if not DRY_RUN and not live_trading_active():
        log(f"已拒绝启动：想走实盘但条件不全：{'；'.join(live_trading_missing())}。")
        return
    if live_trading_active():
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

    # 实盘：建下单器 + 启动对账（下单器建不起来就拒绝启动，绝不半实盘跑）
    live_trader = None
    live_blocked: set = set()
    if live_trading_active():
        try:
            api_key, api_secret = load_api_credentials()
            live_trader = create_live_trader(api_key, api_secret)
            log("实盘下单器已就绪：逐仓 10x，交易所精度已加载。")
        except Exception as e:  # noqa: BLE001
            log(f"已拒绝启动：实盘下单器创建失败：{e}")
            return
        reconcile_live_positions(live_trader, positions, risk, live_blocked, save_state)

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
                if position.get("live"):
                    # 实盘持仓：以交易所订单状态为准，不是用 ticker 价模拟成交
                    if live_trader is None:
                        log(f"严重：{symbol} 有实盘持仓但下单器不在，跳过本轮，等人工处理。")
                        continue
                    manage_live_position(symbol, position, price, now, risk,
                                         live_trader, save_state, positions,
                                         last_heartbeat_log)
                    continue
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

            # 入场按判断时的实时价，不用滞后的 5 分钟 K 线收盘价（2026-11-10 的教训：
            # 收盘价 72964.9 开单时实时价已到约 73290，差了 325 点）
            signal = evaluate(price=price, atr=indicators["atr"],
                              ema200_1h=indicators["ema200_1h"],
                              ema200_4h=indicators["ema200_4h"], adx15=indicators["adx15"],
                              ma_fast=indicators["ma_fast"], ma_slow=indicators["ma_slow"],
                              midline=indicators["midline"],
                              volume=indicators["volume"], volume_sma=indicators["volume_sma"],
                              log=log)
            if signal is None:
                log(f"无信号，{symbol} 现价 {price:.1f}（15m 收 {indicators['price']:.1f}），继续等。")
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
                # 3/3 强信号跳过冷却：放行，但要把原因记进日志，免得事后看不懂
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

            if live_trading_active():
                # 实盘：下单抛错绝不能杀死主循环；没成交不记持仓、不计数
                if symbol in live_blocked:
                    log(f"风控拦截：{symbol} 实盘对账异常，已暂停新开单。")
                    continue
                if live_trader is None:
                    log("严重：实盘模式但下单器不在，放弃这单。")
                    continue
                try:
                    new_position = place_order_live(signal, qty, margin_usdt,
                                                    live_trader, live_blocked)
                except Exception as e:  # noqa: BLE001 - 实盘下单异常只记日志，绝不杀主循环
                    log(f"实盘下单异常：{e}，放弃这单，继续跑。")
                    continue
                if new_position is None:
                    continue
                positions[symbol] = new_position
            else:
                place_order_dry_run(signal, qty, margin_usdt)
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
            risk.record_trade_opened(symbol, now=now)
            save_state()

        # 有持仓时 5 秒一轮（止损查得勤，滑点小），没持仓时 60 秒一轮
        time.sleep(HOLDING_POLL_SECONDS if any_holding else FLAT_POLL_SECONDS)


if __name__ == "__main__":
    main()
