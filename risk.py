"""风控器：所有开单决定都要先过它。没有止损止盈的单，一律拒绝。"""
from datetime import datetime, timezone

from config import (
    ACCOUNT_SIZE_USDT,
    CONSECUTIVE_LOSS_PAUSE_MINUTES,
    COOLDOWN_AFTER_SL_MINUTES,
    COOLDOWN_MINUTES,
    LEVERAGE,
    MAKER_FEE_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_USDT,
    MAX_LOSS_PER_TRADE_USDT,
    MAX_MARGIN_PER_TRADE_USDT,
    MAX_TRADES_PER_DAY,
    NEWS_PAUSE_WINDOWS,
    RISK_PER_TRADE_PCT,
    TAKER_FEE_PCT,
    TARGET_LOSS_PER_TRADE_USDT,
)


def _parse_window(window: tuple[str, str]) -> tuple[float, float]:
    """把 (开始, 结束) 的 ISO 时间（UTC）转成时间戳。"""
    start = datetime.fromisoformat(window[0].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window[1].replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start.timestamp(), end.timestamp()


def is_news_pause(now: float) -> bool:
    """现在是不是在大消息禁入窗口里（CPI / 非农 / FOMC 前后 30 分钟）。"""
    for window in NEWS_PAUSE_WINDOWS:
        try:
            start_ts, end_ts = _parse_window(window)
        except ValueError:
            continue
        if start_ts <= now <= end_ts:
            return True
    return False


class RiskManager:
    def __init__(self) -> None:
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_loss_usdt = 0.0
        self.daily_pnl_usdt = 0.0
        self.last_close_at: float | None = None   # 上次平仓时间戳（秒）
        self.last_close_was_sl = False            # 上次平仓是不是止损
        self.pause_until: float | None = None     # 连亏熔断的暂停结束时间
        self.current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ---------- 日期清零（2026-11-07 的教训：旧版只在重启时清零，跑着不动就一直卡在 30 单上限）----------
    def reset_if_new_day(self, utc_date: str) -> bool:
        """UTC 日期变了就清零当日计数，返回 True 表示刚清过零。"""
        if utc_date == self.current_date:
            return False
        self.current_date = utc_date
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_loss_usdt = 0.0
        self.daily_pnl_usdt = 0.0
        self.last_close_at = None
        self.last_close_was_sl = False
        self.pause_until = None
        return True

    # ---------- 当日上限 ----------
    def can_trade(self, now: float | None = None) -> tuple[bool, str]:
        """返回 (能不能开单, 原因)。原因空字符串表示可以开。"""
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return False, f"今日已开 {self.trades_today} 单，达到上限 {MAX_TRADES_PER_DAY} 单。"
        if self.daily_loss_usdt >= MAX_DAILY_LOSS_USDT:
            return False, f"今日亏损 {self.daily_loss_usdt:.2f} U，达到熔断 {MAX_DAILY_LOSS_USDT:.0f} U。"
        if now is None:
            return True, ""
        if self.pause_until is not None and now < self.pause_until:
            left = int((self.pause_until - now) / 60)
            return False, f"连亏 {MAX_CONSECUTIVE_LOSSES} 单熔断中，还要休息约 {left} 分钟。"
        if is_news_pause(now):
            return False, "大消息禁入窗口内（CPI / 非农 / FOMC 前后 30 分钟），不交易。"
        if self.last_close_at is not None:
            need = (COOLDOWN_AFTER_SL_MINUTES if self.last_close_was_sl
                    else COOLDOWN_MINUTES) * 60
            if now - self.last_close_at < need:
                left = int((need - (now - self.last_close_at)) / 60) + 1
                kind = "止损后" if self.last_close_was_sl else "平仓后"
                return False, f"{kind}冷却中，还要等约 {left} 分钟。"
        return True, ""

    def should_stop_for_day(self) -> bool:
        return self.daily_loss_usdt >= MAX_DAILY_LOSS_USDT

    def record_trade_opened(self, now: float | None = None) -> None:
        self.trades_today += 1

    def record_trade_closed(self, pnl_usdt: float, was_sl: bool, now: float | None = None) -> None:
        """平仓后调用：更新当日盈亏、连亏计数、冷却时间。"""
        self.daily_pnl_usdt += pnl_usdt
        if pnl_usdt < 0:
            self.daily_loss_usdt += -pnl_usdt
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if now is not None:
            self.last_close_at = now
            self.last_close_was_sl = was_sl
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                self.pause_until = now + CONSECUTIVE_LOSS_PAUSE_MINUTES * 60

    # ---------- 仓位计算 ----------
    def size_position(self, entry: float, stop_loss: float) -> tuple[float, float, float]:
        """
        按风险倒推仓位：名义仓位 = 每单风险额 / 止损距离%，保证金封顶 200 U。
        每单风险额 = 本金 x 0.5%（10 U）。返回 (数量 BTC, 保证金 USDT, 预期最大亏损 USDT)。
        预期最大亏损按 Taker 手续费保守估算；超 40 U 硬上限返回数量 0，放弃这单。
        """
        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0 or entry <= 0:
            return 0.0, 0.0, 0.0

        risk_amount = ACCOUNT_SIZE_USDT * RISK_PER_TRADE_PCT
        stop_pct = stop_distance / entry
        notional = risk_amount / stop_pct if stop_pct > 0 else 0.0
        margin = notional / LEVERAGE

        # 保证金超 200 U：按 200 U 封顶倒推数量
        if margin > MAX_MARGIN_PER_TRADE_USDT:
            margin = MAX_MARGIN_PER_TRADE_USDT
            notional = margin * LEVERAGE
        qty = notional / entry

        # 预期最大亏损 = 止损距离的亏损 + 一进一出手续费（保守按 Taker 算）
        max_loss = qty * stop_distance + notional * (MAKER_FEE_PCT + TAKER_FEE_PCT)
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
