"""风控器：所有开单决定都要先过它。没有止损止盈的单，一律拒绝。"""
from config import (
    ACCOUNT_SIZE_USDT,
    COOLDOWN_MINUTES,
    LEVERAGE,
    MAX_DAILY_LOSS_USDT,
    MAX_LOSS_PER_TRADE_USDT,
    MAX_MARGIN_PER_TRADE_USDT,
    MAX_TRADES_PER_DAY,
    MAX_TREND_TRADES_PER_DAY,
    TARGET_LOSS_PER_TRADE_USDT,
)


class RiskManager:
    def __init__(self) -> None:
        self.trades_today = 0
        self.trend_trades_today = 0
        self.daily_loss_usdt = 0.0
        self.last_trade_at: dict[str, float] = {}  # 同一方向上次开单时间戳（秒），管 10 分钟冷却

    # ---------- 当日上限 ----------
    def can_trade(self, mode: str = "small_range", side: str = "LONG", now: float | None = None) -> bool:
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False
        if mode == "trend" and self.trend_trades_today >= MAX_TREND_TRADES_PER_DAY:
            return False
        if self.daily_loss_usdt >= MAX_DAILY_LOSS_USDT:
            return False
        # 冷却：同一方向 10 分钟内只做一次，不叠单、不白交手续费
        if now is not None and side in self.last_trade_at:
            if now - self.last_trade_at[side] < COOLDOWN_MINUTES * 60:
                return False
        return True

    def should_stop_for_day(self) -> bool:
        return self.daily_loss_usdt >= MAX_DAILY_LOSS_USDT

    def record_trade_opened(self, mode: str = "small_range", side: str = "LONG", now: float | None = None) -> None:
        self.trades_today += 1
        if mode == "trend":
            self.trend_trades_today += 1
        if now is not None:
            self.last_trade_at[side] = now

    def record_trade_closed(self, pnl_usdt: float) -> None:
        """平仓后调用：亏了就累计到今日亏损里。pnl 为负表示亏损。"""
        if pnl_usdt < 0:
            self.daily_loss_usdt += -pnl_usdt

    # ---------- 仓位计算 ----------
    def size_position(self, entry: float, stop_loss: float) -> tuple[float, float, float]:
        """
        按止损距离算仓位，保证单笔最大亏损 <= 40 USDT。
        返回 (数量 BTC, 保证金 USDT, 预期最大亏损 USDT)。
        数量 <= 0 表示止损距离太大，200 U 保证金都兜不住 40 U 亏损，放弃这单。
        """
        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0 or entry <= 0:
            return 0.0, 0.0, 0.0

        # 先按目标亏损 20 U 算，数量 = 20 / 止损距离
        qty = TARGET_LOSS_PER_TRADE_USDT / stop_distance
        notional = qty * entry
        margin = notional / LEVERAGE

        # 保证金超 200 U：按 200 U 封顶倒推数量
        if margin > MAX_MARGIN_PER_TRADE_USDT:
            qty = (MAX_MARGIN_PER_TRADE_USDT * LEVERAGE) / entry
            notional = qty * entry
            margin = notional / LEVERAGE

        max_loss = qty * stop_distance
        if max_loss > MAX_LOSS_PER_TRADE_USDT:
            return 0.0, 0.0, max_loss
        return qty, margin, max_loss

    # ---------- 下单前校验 ----------
    @staticmethod
    def validate_order(signal: dict) -> None:
        """没止损、没止盈、方向不对、保证金超标，全部拒绝。"""
        if not signal.get("stop_loss") or not signal.get("take_profit_1"):
            raise ValueError("拒绝下单：缺少止损或止盈。")
        if signal["side"] not in ("LONG", "SHORT"):
            raise ValueError("拒绝下单：方向不是 LONG/SHORT。")
        if signal.get("max_loss_usdt", 9999) > MAX_LOSS_PER_TRADE_USDT:
            raise ValueError("拒绝下单：单笔亏损超 40 U 上限。")
