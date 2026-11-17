"""实盘下单 Mock 测试（2026-11-14 接通实盘时新增）。

覆盖：入场成交后止损止盈都挂上；入场超时未成交撤单且不记持仓；
挂止损失败后立刻市价平仓；挂止盈失败撤止损并市价平仓；
止盈成交被检测到；止损成交被检测到；保本移动止损；
开关缺失时拒绝实盘；缺凭证时拒绝启动；启动对账（孤儿持仓认领、
停机期间已平仓）；精度取整；手续费拉取失败回退。
全部用 Mock 的 Binance 客户端，不碰真实 API（本机连不上币安，HTTP 451）。

用法：python3 tests/test_live_orders.py
"""
import os
import sys
import time
from pathlib import Path

try:
    import pytest
except ImportError:  # 直接 python3 运行测试文件时 pytest 未装：类测试跳过
    pytest = None  # type: ignore[assignment]

# 导入 main 会启动 health server（读 $PORT）：绑 0 号端口（系统自动分配），免得占 3000。
os.environ["PORT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import main
from main import (
    LiveOrderError,
    LiveOrderRejected,
    LiveTrader,
    live_close_pnl,
    manage_live_position,
    place_order_live,
    reconcile_live_positions,
)
from risk import RiskManager

FAILURES = []
LOGS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def capture_log(msg):
    LOGS.append(msg)


main.log = capture_log
# 入场轮询改快一点，测试不用真等 2 秒；超时测试里再单独改小超时。
main.ENTRY_POLL_SECONDS = 0.01


class FakeBinanceAPIException(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.message = msg


class FakeClient:
    """模拟 python-binance Client（futures 方法子集），订单按状态机走。"""

    def __init__(self):
        self.orders = {}
        self.next_id = 1000
        self.positions = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        self.entries = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        self.marks = {"BTCUSDT": 70000.0, "ETHUSDT": 2000.0}
        self.trades = []
        self.cancelled_ids = []
        self.market_closes = []
        self.leverage_set = {}
        self.margin_type_set = {}
        self.auto_fill_entry = True
        self.fail_create = None
        self.fail_trades = False
        self.recv_windows = []  # 记录每次下单带的 recvWindow（-1021 回归用）
        self.position_sides = []  # 记录每次下单带的 positionSide（-4061 回归用）
        self.dual_side_position = True  # 默认双向持仓（和真实账户一致）；单向测试再改 False
        self.fill_on_poll = {}
        self.poll_counts = {}
        self.exchange_info_ok = True
        self.book = {}  # symbol -> (bid, ask)；不设就按 marks 给一个小点差
        self.fail_book_ticker = False
        self.fail_position_symbols = set()  # 这些币种拉持仓信息时抛错（对账失败回归用）
        # --- algo 单模拟（新版 python-binance >=1.0.33 的行为）---
        # 默认关闭：STOP_MARKET 走普通订单（旧版行为），现有测试不动。
        # algo_routing=True 时：STOP_MARKET 返回 algoId（无 orderId），
        # 存在 algo_orders 里，只能用 futures_get/cancel_algo_order 查/撤。
        self.algo_routing = False
        self.algo_orders = {}
        self.next_algo_id = 100000
        self.create_params = []  # 每次下单的原始参数（断言 reduceOnly 用）
        self.fail_reduceonly_once = False  # 置 True：下次带 reduceOnly 的单报 -1106

    def futures_orderbook_ticker(self, symbol, recvWindow=None):
        if self.fail_book_ticker:
            raise FakeBinanceAPIException(-1000, "book ticker unavailable")
        if symbol in self.book:
            bid, ask = self.book[symbol]
        else:
            mark = self.marks[symbol]
            bid, ask = mark * 0.9995, mark * 1.0005
        return {"symbol": symbol, "bidPrice": str(bid), "askPrice": str(ask),
                "bidQty": "1", "askQty": "1"}

    def futures_get_position_mode(self, recvWindow=None):
        return {"dualSidePosition": self.dual_side_position}

    def futures_exchange_info(self):
        if not self.exchange_info_ok:
            raise FakeBinanceAPIException(-1000, "exchange info unavailable")
        return {"symbols": [
            {"symbol": "BTCUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
            {"symbol": "ETHUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        ]}

    def futures_change_leverage(self, symbol, leverage, recvWindow=None):
        self.leverage_set[symbol] = leverage
        return {"leverage": leverage, "symbol": symbol}

    def futures_change_margin_type(self, symbol, marginType, recvWindow=None):
        self.margin_type_set[symbol] = marginType
        return {"code": 200, "msg": "success"}

    def futures_create_order(self, **params):
        if self.fail_create is not None:
            exc = self.fail_create
            self.fail_create = None
            raise exc
        self.create_params.append(dict(params))  # 记录每一次下单尝试（含被拒的）
        if self.fail_reduceonly_once and params.get("reduceOnly"):
            self.fail_reduceonly_once = False
            raise FakeBinanceAPIException(
                -1106, "Parameter 'reduceonly' sent when not required.")
        self.recv_windows.append(params.get("recvWindow"))
        self.position_sides.append(params.get("positionSide"))
        if self.algo_routing and params["type"] == "STOP_MARKET":
            # 新版库：条件单走 algoOrder 端点，返回 algoId，没有 orderId。
            self.next_algo_id += 1
            aid = self.next_algo_id
            algo = {"algoId": aid,
                    "clientAlgoId": f"TESTALGO{aid}",
                    "algoType": "CONDITIONAL",
                    "orderType": "STOP_MARKET",
                    "symbol": params["symbol"], "side": params["side"],
                    "positionSide": params.get("positionSide", "BOTH"),
                    "timeInForce": "GTC",
                    "quantity": str(params.get("quantity", "0")),
                    "algoStatus": "NEW",
                    "actualOrderId": "", "actualPrice": "0",
                    "triggerPrice": str(params.get("stopPrice", "0")),
                    "price": "0",
                    "closePosition": params.get("closePosition", False),
                    "reduceOnly": params.get("reduceOnly", False),
                    "createTime": int(time.time() * 1000),
                    "updateTime": int(time.time() * 1000),
                    "triggerTime": 0}
            self.algo_orders[aid] = algo
            return {"algoId": aid, "clientAlgoId": algo["clientAlgoId"],
                    "algoStatus": "NEW", "orderType": "STOP_MARKET"}
        self.next_id += 1
        oid = self.next_id
        order = {"orderId": oid, "symbol": params["symbol"], "side": params["side"],
                 "type": params["type"], "status": "NEW",
                 "price": str(params.get("price", "0")),
                 "stopPrice": str(params.get("stopPrice", "0")),
                 "origQty": str(params.get("quantity", "0")),
                 "executedQty": "0", "avgPrice": "0",
                 "reduceOnly": params.get("reduceOnly", False),
                 "closePosition": params.get("closePosition", False),
                 "positionSide": params.get("positionSide", "BOTH"),
                 "timeInForce": params.get("timeInForce", ""),
                 "time": int(time.time() * 1000), "updateTime": int(time.time() * 1000)}
        self.orders[oid] = order
        if params["type"] == "LIMIT" and params.get("timeInForce") == "GTX":
            if self.auto_fill_entry:
                self._fill(order, float(params["price"]))
        elif params["type"] == "MARKET":
            self.market_closes.append(order)
            self._fill(order, self.marks[params["symbol"]])
        return {"orderId": oid, "status": order["status"]}

    def futures_get_algo_order(self, symbol, algoId, recvWindow=None):
        algo = self.algo_orders.get(algoId)
        if algo is None:
            raise FakeBinanceAPIException(-2013, "Order does not exist.")
        return dict(algo)

    def futures_cancel_algo_order(self, symbol, algoId, recvWindow=None):
        algo = self.algo_orders.get(algoId)
        if algo is None:
            raise FakeBinanceAPIException(-2013, "Order does not exist.")
        if algo["algoStatus"] in ("TRIGGERED", "CANCELED"):
            raise FakeBinanceAPIException(-2011, "Unknown order sent.")
        algo["algoStatus"] = "CANCELED"
        algo["updateTime"] = int(time.time() * 1000)
        self.cancelled_ids.append(algoId)
        return {"algoId": algoId, "status": "CANCELED"}

    def futures_get_open_algo_orders(self, symbol=None, recvWindow=None):
        return [dict(a) for a in self.algo_orders.values()
                if a["algoStatus"] == "NEW"
                and (symbol is None or a["symbol"] == symbol)]

    def trigger_algo_order(self, algoId, avg_price=None):
        """测试驱动：让某张 algo 止损单触发（产生一笔真实市价成交单，平掉持仓）。"""
        algo = self.algo_orders[algoId]
        if avg_price is None:
            avg_price = float(algo["triggerPrice"])
        self.next_id += 1
        oid = self.next_id
        order = {"orderId": oid, "symbol": algo["symbol"], "side": algo["side"],
                 "type": "MARKET", "origType": "STOP_MARKET", "status": "NEW",
                 "price": "0", "stopPrice": algo["triggerPrice"],
                 "origQty": str(abs(self.positions[algo["symbol"]])),
                 "executedQty": "0", "avgPrice": "0",
                 "reduceOnly": False, "closePosition": True,
                 "time": int(time.time() * 1000), "updateTime": int(time.time() * 1000)}
        self.orders[oid] = order
        self._fill(order, avg_price)
        algo["algoStatus"] = "TRIGGERED"
        algo["actualOrderId"] = str(oid)
        algo["actualPrice"] = str(avg_price)
        algo["updateTime"] = int(time.time() * 1000)
        return order

    def _fill(self, order, avg_price):
        if order.get("closePosition"):
            qty = abs(self.positions[order["symbol"]])
        else:
            qty = float(order.get("origQty") or 0)
        order["status"] = "FILLED"
        order["executedQty"] = str(qty)
        order["avgPrice"] = str(avg_price)
        order["updateTime"] = int(time.time() * 1000)
        symbol = order["symbol"]
        side = order["side"]
        if order.get("closePosition") or order.get("reduceOnly"):
            delta = qty if side == "BUY" else -qty
            self.positions[symbol] += delta
            if abs(self.positions[symbol]) < 1e-9:
                self.positions[symbol] = 0.0
                self.entries[symbol] = 0.0
        else:
            delta = qty if side == "BUY" else -qty
            if self.positions[symbol] == 0:
                self.entries[symbol] = avg_price
            self.positions[symbol] += delta
        self.trades.append({"orderId": order["orderId"], "symbol": symbol,
                            "qty": str(qty), "price": str(avg_price),
                            "commission": "0.0200", "commissionAsset": "USDT",
                            "realizedPnl": "0", "time": int(time.time() * 1000)})

    def fill_order(self, orderId, avg_price=None):
        """测试驱动：让某张挂单成交（止损按 stopPrice，止盈按限价，市价按 mark）。"""
        order = self.orders[orderId]
        if avg_price is None:
            if order["type"] == "STOP_MARKET":
                avg_price = float(order["stopPrice"])
            elif order["type"] == "MARKET":
                avg_price = self.marks[order["symbol"]]
            else:
                avg_price = float(order["price"])
        self._fill(order, avg_price)

    def futures_get_order(self, symbol, orderId, recvWindow=None):
        order = self.orders.get(orderId)
        if order is None:
            raise FakeBinanceAPIException(-2013, "Order does not exist.")
        n = self.poll_counts.get(orderId, 0) + 1
        self.poll_counts[orderId] = n
        if orderId in self.fill_on_poll and n >= self.fill_on_poll[orderId] \
                and order["status"] == "NEW":
            self._fill(order, float(order["price"] or self.marks[symbol]))
        return dict(order)

    def futures_cancel_order(self, symbol, orderId, recvWindow=None):
        order = self.orders.get(orderId)
        if order is None or order["status"] == "FILLED":
            raise FakeBinanceAPIException(-2011, "Unknown order sent.")
        order["status"] = "CANCELED"
        self.cancelled_ids.append(orderId)
        return {"orderId": orderId, "status": "CANCELED"}

    def futures_get_open_orders(self, symbol=None, recvWindow=None):
        return [dict(o) for o in self.orders.values()
                if o["status"] in ("NEW", "PARTIALLY_FILLED")
                and (symbol is None or o["symbol"] == symbol)]

    def futures_get_all_orders(self, symbol, limit=20, recvWindow=None):
        rows = [dict(o) for o in self.orders.values() if o["symbol"] == symbol]
        rows.sort(key=lambda o: o["updateTime"])
        return rows[-limit:]

    def futures_position_information(self, symbol=None, recvWindow=None):
        if symbol in self.fail_position_symbols:
            raise FakeBinanceAPIException(-1000, "position info unavailable")
        out = []
        for s in (["BTCUSDT", "ETHUSDT"] if symbol is None else [symbol]):
            out.append({"symbol": s, "positionAmt": str(self.positions[s]),
                        "entryPrice": str(self.entries[s]),
                        "markPrice": str(self.marks[s]),
                        "leverage": "10"})
        return out

    def futures_account_trades(self, symbol, startTime=None, recvWindow=None):
        if self.fail_trades:
            raise FakeBinanceAPIException(-1000, "trade list unavailable")
        return [dict(t) for t in self.trades if t["symbol"] == symbol]


def make_trader(auto_fill_entry=True, dual_side_position=True):
    client = FakeClient()
    client.auto_fill_entry = auto_fill_entry
    client.dual_side_position = dual_side_position
    trader = LiveTrader(client, log_fn=capture_log)
    trader.setup_account()
    return client, trader


def make_signal(symbol="BTCUSDT", side="SHORT", entry=70000.0,
                sl=70600.0, tp=69100.0):
    return {"symbol": symbol, "side": side, "entry": entry,
            "stop_loss": sl, "take_profit": tp, "take_profit_1": tp,
            "atr": 300.0, "votes": 3, "reason": "test", "max_loss_usdt": 10.0}


def save_gate():
    return (config.DRY_RUN, config.LIVE_TRADING_ENABLED,
            os.environ.get("BINANCE_LIVE_TRADING"))


def enable_live_gate():
    """打开实盘三项开关（只改内存里的 config 状态和环境变量）。"""
    config.DRY_RUN = False
    config.LIVE_TRADING_ENABLED = True
    os.environ["BINANCE_LIVE_TRADING"] = "1"


def restore_gate(saved):
    config.DRY_RUN, config.LIVE_TRADING_ENABLED, old_env = saved
    if old_env is None:
        os.environ.pop("BINANCE_LIVE_TRADING", None)
    else:
        os.environ["BINANCE_LIVE_TRADING"] = old_env


def open_short(trader):
    """开一单空单（SHORT @70000，止损 70600，止盈 69100），返回 (client 无，持仓)。"""
    return place_order_live(make_signal(), 0.02, 140.0, trader, set())


# ---------- 开关 ----------
def test_live_switch():
    check(config.live_trading_active() is False, "开关：默认 Dry-Run 不开实盘")
    check(len(config.live_trading_missing()) >= 1, "开关：默认缺失列表非空")
    saved = save_gate()
    try:
        enable_live_gate()
        check(config.live_trading_active() is True, "开关：三项全满足才开实盘")
        check(config.live_trading_missing() == [], "开关：三项全满足时缺失列表为空")
        os.environ.pop("BINANCE_LIVE_TRADING", None)
        check(config.live_trading_active() is False, "开关：缺 BINANCE_LIVE_TRADING=1 不开实盘")
        os.environ["BINANCE_LIVE_TRADING"] = "1"
        config.LIVE_TRADING_ENABLED = False
        check(config.live_trading_active() is False, "开关：缺 config live 开关不开实盘")
        config.LIVE_TRADING_ENABLED = True
        config.DRY_RUN = True
        check(config.live_trading_active() is False, "开关：BINANCE_DRY_RUN 不是 0 不开实盘")
    finally:
        restore_gate(saved)


def test_place_order_live_refuses_when_gate_off():
    client, trader = make_trader()
    try:
        place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(False, "开关缺失时拒绝实盘下单")
    except LiveOrderError:
        check(True, "开关缺失时拒绝实盘下单")
    check(len(client.orders) == 0, "开关缺失时没下任何单")


def test_missing_credentials():
    saved_env = (os.environ.pop("BINANCE_API_KEY", None),
                 os.environ.pop("BINANCE_API_SECRET", None))
    orig_load_config = config.load_config
    config.load_config = lambda: {}
    try:
        try:
            config.load_api_credentials()
            check(False, "缺凭证时拒绝启动")
        except RuntimeError:
            check(True, "缺凭证时拒绝启动")
    finally:
        config.load_config = orig_load_config
        if saved_env[0] is not None:
            os.environ["BINANCE_API_KEY"] = saved_env[0]
        if saved_env[1] is not None:
            os.environ["BINANCE_API_SECRET"] = saved_env[1]


# ---------- 入场 + 保护 ----------
def test_entry_fill_places_protection():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "入场成交后返回持仓")
        check(pos["live"] is True, "持仓标记 live")
        check(abs(pos["entry"] - 70000.0) < 1e-9, "入场价用实际成交均价")
        check(abs(pos["qty"] - 0.02) < 1e-9, "数量=实际成交量")
        sl = client.orders[pos["sl_order_id"]]
        check(sl["type"] == "STOP_MARKET" and sl["closePosition"] in ("true", True),
              "止损是市价单 closePosition")
        check(abs(float(sl["stopPrice"]) - 70600.0) < 1e-9, "止损价=信号止损")
        tp = client.orders[pos["tp_order_id"]]
        check(tp["type"] == "LIMIT" and tp["reduceOnly"] not in ("true", True),
              "止盈是限价单；双向模式下不带 reduceOnly（-1106 修复）")
        check(abs(float(tp["price"]) - 69100.0) < 1e-9, "止盈价=信号止盈")
        check(abs(client.positions["BTCUSDT"] + 0.02) < 1e-9, "交易所持仓 -0.02")
        check(client.leverage_set.get("BTCUSDT") == 10, "启动设了 10 倍杠杆")
    finally:
        restore_gate(saved)


def test_entry_timeout_cancels_and_no_position():
    saved = save_gate()
    old_timeout = main.ENTRY_FILL_TIMEOUT_SECONDS
    try:
        enable_live_gate()
        main.ENTRY_FILL_TIMEOUT_SECONDS = 2
        client, trader = make_trader(auto_fill_entry=False)
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "入场超时未成交不记持仓")
        entry_id = next(iter(client.orders))
        check(client.orders[entry_id]["status"] == "CANCELED", "入场超时撤单")
        check(client.positions["BTCUSDT"] == 0.0, "入场超时交易所无持仓")
        check(any("未成交" in m for m in LOGS), "入场超时记了日志")
    finally:
        main.ENTRY_FILL_TIMEOUT_SECONDS = old_timeout
        restore_gate(saved)


def test_entry_rejected_post_only():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.fail_create = FakeBinanceAPIException(
            -2010, "Order would immediately match and take.")
        try:
            trader.place_entry(make_signal(), 0.02, sleep_fn=lambda s: None)
            check(False, "Post Only 会立即成交时被拒")
        except LiveOrderRejected:
            check(True, "Post Only 会立即成交时被拒")
        check(client.positions["BTCUSDT"] == 0.0, "被拒后无持仓")
    finally:
        restore_gate(saved)


def test_stop_failure_market_closes():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        orig_create = client.futures_create_order
        calls = {"n": 0}

        def flaky_create(**params):
            calls["n"] += 1
            if calls["n"] == 2:
                raise FakeBinanceAPIException(-2010, "Order would immediately trigger.")
            return orig_create(**params)

        client.futures_create_order = flaky_create
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "挂止损失败不记持仓")
        check(len(client.market_closes) == 1, "挂止损失败后立刻市价平仓")
        check(client.market_closes[0]["reduceOnly"] not in ("true", True),
              "双向模式下市价平仓不带 reduceOnly（-1106 修复）")
        check(client.positions["BTCUSDT"] == 0.0, "市价平仓后交易所无持仓")
        check(any("裸仓" in m for m in LOGS), "挂保护失败记了严重日志")
    finally:
        restore_gate(saved)


def test_tp_failure_cancels_sl_and_market_closes():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        orig_create = client.futures_create_order
        calls = {"n": 0}

        def flaky_create(**params):
            calls["n"] += 1
            if calls["n"] == 3:
                raise FakeBinanceAPIException(-2010, "Order would immediately match and take.")
            return orig_create(**params)

        client.futures_create_order = flaky_create
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "挂止盈失败不记持仓")
        sl_id = next(oid for oid, o in client.orders.items() if o["type"] == "STOP_MARKET")
        check(client.orders[sl_id]["status"] == "CANCELED", "挂止盈失败先撤止损")
        check(len(client.market_closes) == 1, "挂止盈失败后立刻市价平仓")
        check(client.positions["BTCUSDT"] == 0.0, "市价平仓后交易所无持仓")
    finally:
        restore_gate(saved)


# ---------- 持仓管理 ----------
def test_take_profit_detected():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        client.fill_order(pos["tp_order_id"])
        manage_live_position("BTCUSDT", pos, 69100.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(positions["BTCUSDT"] is None, "止盈成交后清持仓")
        check(abs(risk.daily_pnl_usdt - 17.96) < 0.01,
              f"止盈盈亏计入当日（实得 {risk.daily_pnl_usdt:.2f}，预期 17.96）")
        check(client.orders[pos["sl_order_id"]]["status"] == "CANCELED",
              "止盈成交后撤掉止损单")
        check(any("平仓 [止盈]" in m for m in LOGS), "止盈记了平仓日志")
    finally:
        restore_gate(saved)


def test_stop_loss_detected():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        client.fill_order(pos["sl_order_id"])
        manage_live_position("BTCUSDT", pos, 70600.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(positions["BTCUSDT"] is None, "止损成交后清持仓")
        check(abs(risk.daily_pnl_usdt - (-12.04)) < 0.01,
              f"止损盈亏计入当日（实得 {risk.daily_pnl_usdt:.2f}，预期 -12.04）")
        check(risk.last_close_was_sl["BTCUSDT"] is True, "止损出场记为 was_sl")
        check(client.orders[pos["tp_order_id"]]["status"] == "CANCELED",
              "止损成交后撤掉止盈单")
        check(any("平仓 [止损]" in m for m in LOGS), "止损记了平仓日志")
    finally:
        restore_gate(saved)


def test_breakeven_moves_stop():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        old_sl = pos["sl_order_id"]
        manage_live_position("BTCUSDT", pos, 69400.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(pos["breakeven_done"] is True, "保本已触发")
        check(abs(pos["stop_loss"] - 70000.0) < 1e-9, "止损移到成本价")
        check(client.orders[old_sl]["status"] == "CANCELED", "旧止损已撤")
        check(abs(float(client.orders[pos["sl_order_id"]]["stopPrice"]) - 70000.0) < 1e-9,
              "新止损挂在成本价")
        check(positions["BTCUSDT"] is pos, "保本后持仓还在")
    finally:
        restore_gate(saved)


def test_stop_after_breakeven_is_breakeven_exit():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        manage_live_position("BTCUSDT", pos, 69400.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(pos["breakeven_done"] is True, "保本先触发")
        client.fill_order(pos["sl_order_id"])
        manage_live_position("BTCUSDT", pos, 70000.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(positions["BTCUSDT"] is None, "保本止损成交后清持仓")
        check(risk.last_close_was_sl["BTCUSDT"] is False, "保本出场不算真止损（冷却按 15 分钟）")
        check(any("平仓 [保本出场]" in m for m in LOGS), "保本出场记了平仓日志")
    finally:
        restore_gate(saved)


def test_live_close_pnl_math():
    short = {"side": "SHORT", "entry": 70000.0, "qty": 0.02}
    check(abs(live_close_pnl(short, 69100.0, 0.04) - 17.96) < 1e-9,
          "实盘盈亏=实际价差-实际手续费（做空）")
    long = {"side": "LONG", "entry": 70000.0, "qty": 0.02}
    check(abs(live_close_pnl(long, 70900.0, 0.04) - 17.96) < 1e-9,
          "实盘盈亏=实际价差-实际手续费（做多）")


def test_fetch_fees_failure_returns_none():
    client, trader = make_trader()
    client.fail_trades = True
    fees = trader.fetch_fees("BTCUSDT", [1], int(time.time() * 1000),
                             sleep_fn=lambda s: None)
    check(fees is None, "手续费账单拉不到返回 None（调用方改用估算）")


# ---------- 精度 ----------
def test_rounding():
    _, trader = make_trader()
    check(abs(trader.round_price("BTCUSDT", 70000.04) - 70000.0) < 1e-9, "BTC 价格对到 0.1")
    check(abs(trader.round_price("BTCUSDT", 70000.06) - 70000.1) < 1e-9, "BTC 价格向上对到 0.1")
    check(abs(trader.round_qty("BTCUSDT", 0.0255) - 0.025) < 1e-9, "数量向下对到 0.001")
    check(abs(trader.round_price("ETHUSDT", 2000.014) - 2000.01) < 1e-9, "ETH 价格对到 0.01")
    check(abs(trader.round_qty("ETHUSDT", 0.015) - 0.01) < 1e-9, "ETH 数量向下对到 0.01")


# ---------- 启动对账 ----------
def test_reconcile_orphan_position():
    client, trader = make_trader(auto_fill_entry=True)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    sl = client.futures_create_order(symbol="BTCUSDT", side="BUY", type="STOP_MARKET",
                                     stopPrice=70600.0, closePosition="true")
    tp = client.futures_create_order(symbol="BTCUSDT", side="BUY", type="LIMIT",
                                     quantity=0.02, price=69100.0,
                                     timeInForce="GTC", reduceOnly="true")
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None)
    pos = positions["BTCUSDT"]
    check(pos is not None and pos.get("orphan") is True, "孤儿持仓被认领")
    check(pos["sl_order_id"] == sl["orderId"] and pos["tp_order_id"] == tp["orderId"],
          "认领了现有的止损止盈单")
    check(abs(pos["stop_loss"] - 70600.0) < 1e-9 and abs(pos["take_profit"] - 69100.0) < 1e-9,
          "认领后止损止盈价正确")
    check("BTCUSDT" not in blocked, "认领后不暂停该币种（随持仓自动停新单）")


def test_reconcile_orphan_without_protection_market_closes():
    client, trader = make_trader(auto_fill_entry=True)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None)
    check(positions["BTCUSDT"] is None, "无保护的孤儿持仓被市价平掉")
    check(len(client.market_closes) == 1, "无保护孤儿持仓触发市价平仓")
    check(client.positions["BTCUSDT"] == 0.0, "市价平仓后交易所无持仓")


def test_reconcile_closed_while_down():
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = open_short(trader)
        client.fill_order(pos["sl_order_id"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        reconcile_live_positions(trader, positions, risk, set(), lambda: None)
        check(positions["BTCUSDT"] is None, "停机期间已平的持仓被清掉")
        check(abs(risk.daily_pnl_usdt - (-12.04)) < 0.01,
              f"停机平账盈亏计入当日（实得 {risk.daily_pnl_usdt:.2f}，预期 -12.04）")
        check(risk.last_close_was_sl["BTCUSDT"] is True, "停机期间止损出场记为 was_sl")
    finally:
        restore_gate(saved)


def test_reconcile_cancels_stray_orders():
    client, trader = make_trader(auto_fill_entry=False)
    stray = client.futures_create_order(symbol="BTCUSDT", side="SELL", type="LIMIT",
                                        quantity=0.02, price=70000.0, timeInForce="GTX")
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    reconcile_live_positions(trader, positions, risk, set(), lambda: None)
    check(client.orders[stray["orderId"]]["status"] == "CANCELED", "无持仓的多余挂单被撤掉")


# ---------- -1021 回归：所有下单都带 recvWindow ----------
def test_recv_window_sent():
    """2026-11-14 实盘第一个信号因 -1021 丢单：Cloady 容器时钟与币安
    差 >5 秒，默认 recvWindow 5000 毫秒不够。下单（入场/止损/止盈）
    必须都带 recvWindow=60000（币安上限）。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "recvWindow 回归：入场成交")
        check(len(client.recv_windows) == 3, "recvWindow 回归：入场+止损+止盈共 3 单")
        check(all(w == config.RECV_WINDOW_MS for w in client.recv_windows),
              "recvWindow 回归：每单都带 recvWindow=60000")
        check(config.RECV_WINDOW_MS == 60000, "recvWindow 回归：常量是币安上限 60000")
    finally:
        restore_gate(saved)


# ---------- -4061 回归：持仓模式与 positionSide ----------
def test_hedge_mode_position_side():
    """2026-11-14 实盘 23:15/23:16 两单因 -4061 丢单：账户是双向持仓，
    下单没带 positionSide（默认 BOTH），币安拒单。双向模式下入场/止损/
    止盈都必须带 positionSide=LONG/SHORT。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
        check(trader.hedge_mode is True, "-4061 回归：双向持仓被识别")
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "-4061 回归：双向模式下入场成交")
        check(len(client.position_sides) == 3, "-4061 回归：入场+止损+止盈共 3 单")
        check(all(s == "SHORT" for s in client.position_sides),
              "-4061 回归：做空单 positionSide 全是 SHORT")
    finally:
        restore_gate(saved)


def test_oneway_mode_position_side():
    """单向持仓模式下，下单要带 positionSide=BOTH。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True, dual_side_position=False)
        check(trader.hedge_mode is False, "-4061 回归：单向持仓被识别")
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "-4061 回归：单向模式下入场成交")
        check(all(s == "BOTH" for s in client.position_sides),
              "-4061 回归：单向模式下 positionSide 全是 BOTH")
    finally:
        restore_gate(saved)


# ---------- -5022 回归：Post Only 被拒后按盘口现价重挂 ----------
def test_entry_5022_retries_at_book_price():
    """2026-11-15 实盘 BTC SHORT @65330.4 因 -5022 丢单：Post Only 限价单
    到币安时价格已穿过盘口（会立即成交），被拒。修复：-5022 后按盘口
    现价重挂一次（做空取最优卖价，做多取最优买价），保证是 maker 单；
    重挂价留 0.05% 缓冲（2026-11-15 实盘 ETH：贴盘口的价 1 秒内又被穿，
    重挂再被 -5022 拒）。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.book["BTCUSDT"] = (69990.0, 70010.0)  # 做空重挂取卖价 70010 + 0.05% 缓冲 = 70045
        client.fail_create = FakeBinanceAPIException(
            -5022, "Due to the order could not be executed as maker, "
                   "the Post Only order will be rejected.")
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "-5022 回归：重挂后入场成交")
        check(abs(pos["entry"] - 70045.0) < 1e-9, "-5022 回归：重挂价是最优卖价+0.05%缓冲")
        entry_orders = [o for o in client.orders.values()
                        if o["type"] == "LIMIT" and o.get("timeInForce") == "GTX"]
        check(len(entry_orders) == 1, "-5022 回归：只下一张入场单（重挂那张）")
        check(abs(float(entry_orders[0]["price"]) - 70045.0) < 1e-9,
              "-5022 回归：入场单价格是盘口卖价+0.05%缓冲")
    finally:
        restore_gate(saved)


def test_entry_5022_retry_also_rejected():
    """重挂也被 -5022 拒了就放弃，不记持仓。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.book["BTCUSDT"] = (69990.0, 70010.0)
        orig_create = client.futures_create_order
        calls = {"n": 0}

        def flaky_create(**params):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise FakeBinanceAPIException(
                    -5022, "Due to the order could not be executed as maker, "
                           "the Post Only order will be rejected.")
            return orig_create(**params)

        client.futures_create_order = flaky_create
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "-5022 回归：重挂也被拒就不记持仓")
        check(client.positions["BTCUSDT"] == 0.0, "-5022 回归：重挂被拒后无持仓")
    finally:
        restore_gate(saved)


def test_entry_5022_no_book_ticker_abandons():
    """-5022 后拿不到盘口价就不重挂，放弃不记持仓。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.fail_book_ticker = True
        client.fail_create = FakeBinanceAPIException(
            -5022, "Due to the order could not be executed as maker, "
                   "the Post Only order will be rejected.")
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "-5022 回归：无盘口价时不重挂、不记持仓")
        check(client.positions["BTCUSDT"] == 0.0, "-5022 回归：无盘口价时无持仓")
    finally:
        restore_gate(saved)


def test_maker_retry_price_has_buffer():
    """-5022 重挂价留 0.05% 缓冲：做空=最优卖价+0.05%，做多=最优买价-0.05%。
    贴盘口的价一秒内被穿就再被拒（2026-11-15 实盘 ETH 遇到过）。"""
    client, trader = make_trader(auto_fill_entry=True)
    client.book["BTCUSDT"] = (69990.0, 70010.0)
    check(abs(trader._maker_retry_price("BTCUSDT", "SHORT") - 70045.0) < 1e-9,
          "重挂价缓冲：做空取卖价+0.05%")
    check(abs(trader._maker_retry_price("BTCUSDT", "LONG") - 69955.0) < 1e-9,
          "重挂价缓冲：做多取买价-0.05%")


# ---------- 2026-11-15 回归：启动对账失败不能永久堵死币种 ----------
def test_reconcile_fetch_failure_blocks_but_retryable():
    """2026-11-15 实盘事故：BTC 启动对账一次拉不到交易所状态，之后 6 个 3/3
    信号全被“实盘对账异常”挡掉，15 小时没开出一单。修复：拉不到状态的币种
    暂停新开单，但记进 retryable，主循环每 15 分钟重试，成功自动解除。"""
    client, trader = make_trader(auto_fill_entry=True)
    client.fail_position_symbols = {"BTCUSDT"}  # 只 BTC 拉不到，ETH 正常
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    retryable = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None,
                             retryable=retryable)
    check("BTCUSDT" in blocked, "对账拉不到：BTC 暂停新开单")
    check("BTCUSDT" in retryable, "对账拉不到：BTC 记进可重试")
    check("ETHUSDT" not in blocked, "对账拉不到：ETH 不受影响")
    check("ETHUSDT" not in retryable, "对账拉不到：ETH 不进可重试")


def test_reconcile_retry_unblocks_after_transient_failure():
    """瞬时失败恢复后重试对账：BTC 解除暂停，可正常开单。"""
    client, trader = make_trader(auto_fill_entry=True)
    client.fail_position_symbols = {"BTCUSDT"}
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    retryable = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None,
                             retryable=retryable)
    check("BTCUSDT" in blocked, "重试前：BTC 仍暂停")
    client.fail_position_symbols = set()  # 网络恢复
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None,
                             symbols=["BTCUSDT"], retryable=retryable)
    check("BTCUSDT" not in blocked, "重试成功：BTC 解除暂停")
    check("BTCUSDT" not in retryable, "重试成功：BTC 移出可重试")


def test_reconcile_hard_failure_not_retryable():
    """裸仓市价平仓也失败（交易所仍有持仓）：暂停但不进可重试，等人工处理。"""
    client, trader = make_trader(auto_fill_entry=True)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    client.fail_create = FakeBinanceAPIException(-1000, "market close unavailable")
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    retryable = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None,
                             retryable=retryable)
    check("BTCUSDT" in blocked, "硬失败：BTC 暂停新开单")
    check("BTCUSDT" not in retryable, "硬失败：BTC 不进可重试（等人工处理）")


# ---------- -1106 回归：双向持仓模式下不许带 reduceOnly ----------
def test_market_close_hedge_mode_no_reduce_only():
    """2026-11-15 实盘事故：BTC SHORT 挂保护失败后，兜底市价平仓带了
    reduceOnly，币安在双向持仓模式下报 -1106，裸仓没平掉。双向模式下
    市价平仓只靠 positionSide 限定方向，不带 reduceOnly。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    trader.market_close("BTCUSDT", "SHORT", 0.02)
    check(client.positions["BTCUSDT"] == 0.0, "-1106 回归：双向模式下市价平仓成交")
    close_params = client.create_params[-1]
    check("reduceOnly" not in close_params, "-1106 回归：双向模式下市价平仓不带 reduceOnly")
    check(close_params.get("positionSide") == "SHORT", "-1106 回归：市价平仓仍带 positionSide=SHORT")


def test_market_close_oneway_mode_has_reduce_only():
    """单向持仓模式下，市价平仓仍要带 reduceOnly（只减仓不开反向仓）。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=False)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    trader.market_close("BTCUSDT", "SHORT", 0.02)
    check(client.positions["BTCUSDT"] == 0.0, "-1106 回归：单向模式下市价平仓成交")
    check(client.create_params[-1].get("reduceOnly") == "true",
          "-1106 回归：单向模式下市价平仓带 reduceOnly")


def test_market_close_1106_retries_without_reduce_only():
    """持仓模式识别错了（以为单向、实际双向）：第一次带 reduceOnly 被 -1106
    拒单，必须去掉 reduceOnly 重试一次、真正把仓平掉，而不是直接报错。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=False)
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    client.fail_reduceonly_once = True
    trader.market_close("BTCUSDT", "SHORT", 0.02)
    check(client.positions["BTCUSDT"] == 0.0, "-1106 回归：重试后市价平仓成交")
    check(len(client.create_params) == 2, "-1106 回归：只重试一次")
    check(client.create_params[0].get("reduceOnly") == "true"
          and "reduceOnly" not in client.create_params[1],
          "-1106 回归：第一次带 reduceOnly 被拒，第二次去掉重发")


def test_take_profit_hedge_mode_no_reduce_only():
    """止盈单同样：双向模式下 LIMIT 止盈带 reduceOnly 也会被 -1106 拒单
    （事故里止损先失败所以没走到这一步，但同样是 bug）。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
    oid = trader.place_take_profit("BTCUSDT", "SHORT", 0.02, 69100.0)
    check(client.orders[oid]["type"] == "LIMIT", "-1106 回归：双向模式下止盈单挂上")
    check("reduceOnly" not in client.create_params[-1],
          "-1106 回归：双向模式下止盈单不带 reduceOnly")


def test_take_profit_oneway_mode_has_reduce_only():
    """单向模式下止盈单仍带 reduceOnly。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=False)
    trader.place_take_profit("BTCUSDT", "SHORT", 0.02, 69100.0)
    check(client.create_params[-1].get("reduceOnly") == "true",
          "-1106 回归：单向模式下止盈单带 reduceOnly")


def test_take_profit_1106_retries_without_reduce_only():
    """止盈单 -1106 时同样去掉 reduceOnly 重试一次。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=False)
    client.fail_reduceonly_once = True
    oid = trader.place_take_profit("BTCUSDT", "SHORT", 0.02, 69100.0)
    check(oid in client.orders, "-1106 回归：止盈重试后挂上")
    check(len(client.create_params) == 2, "-1106 回归：止盈只重试一次")
    check("reduceOnly" not in client.create_params[1], "-1106 回归：止盈重试去掉 reduceOnly")


# ---------- KeyError 'orderId' 回归：止损是 algo 单，返回 algoId ----------
def test_place_stop_returns_algo_id():
    """2026-11-15 实盘事故：新版 python-binance 把 STOP_MARKET 路由到
    algoOrder 端点，返回只有 algoId、没有 orderId，place_stop 取
    order['orderId'] 抛 KeyError。修复后返回 algoId，止损正常挂上。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
    client.algo_routing = True
    oid = trader.place_stop("BTCUSDT", "SHORT", 70600.0)
    check(oid in client.algo_orders, "algo 回归：止损返回 algoId 并挂上")
    algo = client.algo_orders[oid]
    check(algo["orderType"] == "STOP_MARKET", "algo 回归：止损是 STOP_MARKET algo 单")
    check(abs(float(algo["triggerPrice"]) - 70600.0) < 1e-9, "algo 回归：触发价=信号止损")
    check(algo["closePosition"] in ("true", True), "algo 回归：止损是 closePosition")
    check(algo["positionSide"] == "SHORT", "algo 回归：止损带 positionSide=SHORT")


def test_place_stop_response_without_id_raises_clear_error():
    """下单返回里既没有 algoId 也没有 orderId：记下原始返回再抛明确错误，
    绝不能抛 KeyError（会把下单状态淹没），也不能静默吞掉。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
    orig_create = client.futures_create_order

    def bad_create(**params):
        if params.get("type") == "STOP_MARKET":
            return {"success": True, "code": 0, "msg": "success"}
        return orig_create(**params)

    client.futures_create_order = bad_create
    try:
        trader.place_stop("BTCUSDT", "SHORT", 70600.0)
        check(False, "algo 回归：无订单 id 的返回要抛明确错误")
    except LiveOrderError as e:
        check("没有订单 id" in str(e), "algo 回归：无订单 id 时抛明确错误（不是 KeyError）")
    except KeyError:
        check(False, "algo 回归：无订单 id 时不能抛 KeyError")
    check(any("原始返回" in m for m in LOGS), "algo 回归：无订单 id 时记下原始返回")


def test_place_stop_old_style_order_id_still_works():
    """旧版库返回 orderId 时 place_stop 照样工作（向后兼容）。"""
    client, trader = make_trader(auto_fill_entry=True, dual_side_position=True)
    oid = trader.place_stop("BTCUSDT", "SHORT", 70600.0)
    check(oid in client.orders, "algo 回归：旧版 orderId 返回仍可用")
    check(client.orders[oid]["type"] == "STOP_MARKET", "algo 回归：旧版止损仍是 STOP_MARKET")


def test_entry_fill_places_protection_algo_routing():
    """全流程（新版库）：入场成交后止损是 algo 单、止盈是普通 LIMIT 单，
    且止盈在双向模式下不带 reduceOnly。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.algo_routing = True
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is not None, "algo 回归：新版库下入场成交返回持仓")
        check(pos["sl_order_id"] in client.algo_orders, "algo 回归：持仓止损记的是 algoId")
        tp = client.orders[pos["tp_order_id"]]
        check(tp["type"] == "LIMIT" and tp["reduceOnly"] not in ("true", True),
              "algo 回归：新版库下止盈是 LIMIT 且不带 reduceOnly")
        check(client.positions["BTCUSDT"] == -0.02, "algo 回归：新版库下交易所持仓 -0.02")
    finally:
        restore_gate(saved)


def test_stop_failure_market_closes_algo_routing():
    """全流程（新版库）：挂止损失败后兜底市价平仓必须真正发出（不带
    reduceOnly），不能重蹈 2026-11-15 事故（-1106 导致裸仓）。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.algo_routing = True
        orig_create = client.futures_create_order
        calls = {"n": 0}

        def flaky_create(**params):
            calls["n"] += 1
            if calls["n"] == 2:
                raise FakeBinanceAPIException(-2010, "Order would immediately trigger.")
            return orig_create(**params)

        client.futures_create_order = flaky_create
        pos = place_order_live(make_signal(), 0.02, 140.0, trader, set())
        check(pos is None, "algo 回归：新版库下挂止损失败不记持仓")
        check(len(client.market_closes) == 1, "algo 回归：新版库下挂止损失败后市价平仓")
        check(client.positions["BTCUSDT"] == 0.0, "algo 回归：新版库下市价平仓后无持仓")
        check("reduceOnly" not in client.create_params[-1],
              "algo 回归：新版库下兜底平仓不带 reduceOnly")
    finally:
        restore_gate(saved)


def test_stop_loss_detected_via_algo_order():
    """持仓管理（新版库）：algo 止损触发后被检测到，平仓、计盈亏、撤止盈。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.algo_routing = True
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        client.trigger_algo_order(pos["sl_order_id"])
        manage_live_position("BTCUSDT", pos, 70600.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(positions["BTCUSDT"] is None, "algo 回归：algo 止损触发后清持仓")
        check(abs(risk.daily_pnl_usdt - (-12.04)) < 0.01,
              f"algo 回归：algo 止损盈亏计入当日（实得 {risk.daily_pnl_usdt:.2f}，预期 -12.04）")
        check(risk.last_close_was_sl["BTCUSDT"] is True, "algo 回归：algo 止损出场记为 was_sl")
        check(client.orders[pos["tp_order_id"]]["status"] == "CANCELED",
              "algo 回归：algo 止损成交后撤掉止盈单")
        check(any("平仓 [止损]" in m for m in LOGS), "algo 回归：algo 止损记了平仓日志")
    finally:
        restore_gate(saved)


def test_breakeven_moves_algo_stop():
    """保本（新版库）：旧 algo 止损用 algo 接口撤掉，新止损重挂在成本价。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.algo_routing = True
        pos = open_short(trader)
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        hb = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
        old_sl = pos["sl_order_id"]
        manage_live_position("BTCUSDT", pos, 69400.0, time.time(), risk, trader,
                             lambda: None, positions, hb)
        check(pos["breakeven_done"] is True, "algo 回归：保本已触发")
        check(client.algo_orders[old_sl]["algoStatus"] == "CANCELED",
              "algo 回归：旧 algo 止损已撤")
        check(abs(float(client.algo_orders[pos["sl_order_id"]]["triggerPrice"]) - 70000.0) < 1e-9,
              "algo 回归：新 algo 止损挂在成本价")
        check(positions["BTCUSDT"] is pos, "algo 回归：保本后持仓还在")
    finally:
        restore_gate(saved)


def test_reconcile_orphan_with_algo_stop():
    """启动对账（新版库）：孤儿持仓的止损在 algo 挂单列表里，要认领而不是
    当成裸仓市价平掉；双向模式下的止盈（无 reduceOnly）也要认出来。"""
    client, trader = make_trader(auto_fill_entry=True)
    client.algo_routing = True
    client.positions["BTCUSDT"] = -0.02
    client.entries["BTCUSDT"] = 70000.0
    sl = client.futures_create_order(symbol="BTCUSDT", side="BUY", type="STOP_MARKET",
                                     stopPrice=70600.0, closePosition="true",
                                     positionSide="SHORT")
    tp = client.futures_create_order(symbol="BTCUSDT", side="BUY", type="LIMIT",
                                     quantity=0.02, price=69100.0,
                                     timeInForce="GTC", positionSide="SHORT")
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    blocked = set()
    reconcile_live_positions(trader, positions, risk, blocked, lambda: None)
    pos = positions["BTCUSDT"]
    check(pos is not None and pos.get("orphan") is True, "algo 回归：孤儿持仓被认领")
    check(pos["sl_order_id"] == sl["algoId"], "algo 回归：认领了 algo 止损单")
    check(pos["tp_order_id"] == tp["orderId"], "algo 回归：认领了无 reduceOnly 的止盈单")
    check(abs(pos["stop_loss"] - 70600.0) < 1e-9 and abs(pos["take_profit"] - 69100.0) < 1e-9,
          "algo 回归：认领后止损止盈价正确")
    check(len(client.market_closes) == 0, "algo 回归：有保护的孤儿持仓不市价平仓")
    check("BTCUSDT" not in blocked, "algo 回归：认领后不暂停该币种")


def test_reconcile_cancels_stray_algo_orders():
    """启动对账：无持仓时多余的 algo 挂单也要撤掉。"""
    client, trader = make_trader(auto_fill_entry=False)
    client.algo_routing = True
    stray = client.futures_create_order(symbol="BTCUSDT", side="BUY", type="STOP_MARKET",
                                        stopPrice=70600.0, closePosition="true",
                                        positionSide="SHORT")
    positions = {"BTCUSDT": None, "ETHUSDT": None}
    risk = RiskManager(["BTCUSDT", "ETHUSDT"])
    reconcile_live_positions(trader, positions, risk, set(), lambda: None)
    check(client.algo_orders[stray["algoId"]]["algoStatus"] == "CANCELED",
          "algo 回归：无持仓的多余 algo 挂单被撤掉")


def test_reconcile_closed_while_down_by_algo_stop():
    """启动对账：停机期间 algo 止损触发平了仓，要按实际成交价平账、
    且记为真止损（冷却按 30 分钟）。"""
    saved = save_gate()
    try:
        enable_live_gate()
        client, trader = make_trader(auto_fill_entry=True)
        client.algo_routing = True
        pos = open_short(trader)
        client.trigger_algo_order(pos["sl_order_id"])
        positions = {"BTCUSDT": pos, "ETHUSDT": None}
        risk = RiskManager(["BTCUSDT", "ETHUSDT"])
        reconcile_live_positions(trader, positions, risk, set(), lambda: None)
        check(positions["BTCUSDT"] is None, "algo 回归：停机期间 algo 止损平的仓被清掉")
        check(abs(risk.daily_pnl_usdt - (-12.04)) < 0.01,
              f"algo 回归：停机平账盈亏计入当日（实得 {risk.daily_pnl_usdt:.2f}，预期 -12.04）")
        check(risk.last_close_was_sl["BTCUSDT"] is True,
              "algo 回归：停机期间 algo 止损出场记为 was_sl")
    finally:
        restore_gate(saved)


if __name__ == "__main__":
    test_live_switch()
    test_place_order_live_refuses_when_gate_off()
    test_missing_credentials()
    test_entry_fill_places_protection()
    test_entry_timeout_cancels_and_no_position()
    test_entry_rejected_post_only()
    test_stop_failure_market_closes()
    test_tp_failure_cancels_sl_and_market_closes()
    test_take_profit_detected()
    test_stop_loss_detected()
    test_breakeven_moves_stop()
    test_stop_after_breakeven_is_breakeven_exit()
    test_live_close_pnl_math()
    test_fetch_fees_failure_returns_none()
    test_rounding()
    test_reconcile_orphan_position()
    test_reconcile_orphan_without_protection_market_closes()
    test_reconcile_closed_while_down()
    test_reconcile_cancels_stray_orders()
    test_recv_window_sent()
    test_hedge_mode_position_side()
    test_oneway_mode_position_side()
    test_entry_5022_retries_at_book_price()
    test_entry_5022_retry_also_rejected()
    test_entry_5022_no_book_ticker_abandons()
    test_maker_retry_price_has_buffer()
    test_reconcile_fetch_failure_blocks_but_retryable()
    test_reconcile_retry_unblocks_after_transient_failure()
    test_reconcile_hard_failure_not_retryable()
    test_market_close_hedge_mode_no_reduce_only()
    test_market_close_oneway_mode_has_reduce_only()
    test_market_close_1106_retries_without_reduce_only()
    test_take_profit_hedge_mode_no_reduce_only()
    test_take_profit_oneway_mode_has_reduce_only()
    test_take_profit_1106_retries_without_reduce_only()
    test_place_stop_returns_algo_id()
    test_place_stop_response_without_id_raises_clear_error()
    test_place_stop_old_style_order_id_still_works()
    test_entry_fill_places_protection_algo_routing()
    test_stop_failure_market_closes_algo_routing()
    test_stop_loss_detected_via_algo_order()
    test_breakeven_moves_algo_stop()
    test_reconcile_orphan_with_algo_stop()
    test_reconcile_cancels_stray_algo_orders()
    test_reconcile_closed_while_down_by_algo_stop()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项")
        sys.exit(1)
    print("ALL PASS")


# --- 持仓模式检测：-1021 重试与拒绝启动（2026-11-15 quwuwp51 启动警告） ---
class TestDetectPositionModeRetry:
    """_detect_position_mode：-1021 时重试 5 次；5 次都失败则抛错拒绝启动，
    绝不默认单向（默认单向会让双向账户的每单都被 -4061 拒单）。"""

    def _trader_with_mode_calls(self, results):
        """results 是每次 futures_get_position_mode 的返回值或抛出的异常。"""
        trader = LiveTrader(FakeClient(), log_fn=capture_log)
        calls = {"n": 0}

        def fake_mode(recvWindow=None):
            item = results[min(calls["n"], len(results) - 1)]
            calls["n"] += 1
            if isinstance(item, Exception):
                raise item
            return item

        trader.client.futures_get_position_mode = fake_mode
        return trader, calls

    def test_retry_then_success_hedge(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        err = FakeBinanceAPIException(-1021, "Timestamp for this request is outside of the recvWindow.")
        trader, calls = self._trader_with_mode_calls([err, err, {"dualSidePosition": True}])
        trader._detect_position_mode()
        assert trader.hedge_mode is True
        assert calls["n"] == 3

    def test_retry_then_success_oneway(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        err = FakeBinanceAPIException(-1021, "Timestamp for this request is outside of the recvWindow.")
        trader, calls = self._trader_with_mode_calls([err, {"dualSidePosition": False}])
        trader._detect_position_mode()
        assert trader.hedge_mode is False
        assert calls["n"] == 2

    def test_all_retries_fail_raises(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        err = FakeBinanceAPIException(-1021, "Timestamp for this request is outside of the recvWindow.")
        trader, calls = self._trader_with_mode_calls([err])
        with pytest.raises(RuntimeError, match="查持仓模式 5 次都失败"):
            trader._detect_position_mode()
        assert calls["n"] == 5
