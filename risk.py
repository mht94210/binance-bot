"""风控器：所有开单决定都要先过它。没有止损止盈的单，一律拒绝。

计数按币种分开（BTC 和 ETH 各算各的：每天各最多 10 单、冷却、连亏暂停都按币种算）；
盈亏按账户合起来算（每天净亏 -40U 熔断是整个账户的）。
"""
from datetime import datetime, timezone

from config import (
    ACCOUNT_SIZE_USDT,
    CONSERVATIVE_DAILY_LOSS_USDT,
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
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        # 按币种各算各的
        self.trades_today: dict[str, int] = {s: 0 for s in self.symbols}
        self.consecutive_losses: dict[str, int] = {s: 0 for s in self.symbols}
        self.last_close_at: dict[str, float | None] = {s: None for s in self.symbols}
        self.last_close_was_sl: dict[str, bool] = {s: False for s in self.symbols}
        self.pause_until: dict[str, float | None] = {s: None for s in self.symbols}
        # 全账户合起来算
        self.daily_loss_usdt = 0.0
        self.daily_pnl_usdt = 0.0
        self.current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 当日熔断线（正常 40 U；state.json 丢失且非 UTC 日初启动时收紧到 20 U，见 apply_conservative_halt）
        self.daily_loss_limit = MAX_DAILY_LOSS_USDT

    def apply_conservative_halt(self) -> float:
        """state.json 丢失且不是 UTC 日初：当日计数可能已被容器重启清零，
        把当日熔断线收紧一半（40 U -> 20 U），免得今天实际亏超 40 U。
        UTC 午夜 reset_if_new_day 会恢复 40 U。返回收紧后的熔断线。"""
        self.daily_loss_limit = CONSERVATIVE_DAILY_LOSS_USDT
        return self.daily_loss_limit

    # ---------- 状态存取（给 state.json 用）----------
    def state_dict(self) -> dict:
        """返回可存进 state.json 的当日状态。"""
        return {
            "trades_today": dict(self.trades_today),
            "consecutive_losses": dict(self.consecutive_losses),
            "daily_loss_usdt": self.daily_loss_usdt,
            "daily_pnl_usdt": self.daily_pnl_usdt,
            "daily_loss_limit": self.daily_loss_limit,
            "last_close_at": dict(self.last_close_at),
            "last_close_was_sl": dict(self.last_close_was_sl),
            "pause_until": dict(self.pause_until),
        }

    def restore_state(self, saved: dict) -> None:
        """从 state.json 恢复当日计数；缺的币种默认 0，不报错。"""
        for key in ("trades_today", "consecutive_losses"):
            src = saved.get(key) or {}
            setattr(self, key, {s: int(src.get(s, 0)) for s in self.symbols})
        self.daily_loss_usdt = float(saved.get("daily_loss_usdt", 0.0))
        self.daily_pnl_usdt = float(saved.get("daily_pnl_usdt", 0.0))
        # 熔断线也恢复（收紧过的 20 U 不能因一次进程重启就悄悄变回 40 U）；
        # 旧 state.json 没有这个字段时默认 40 U。
        self.daily_loss_limit = float(saved.get("daily_loss_limit", MAX_DAILY_LOSS_USDT))
        for key in ("last_close_at", "pause_until"):
            src = saved.get(key) or {}
            setattr(self, key, {s: src.get(s) for s in self.symbols})
        src = saved.get("last_close_was_sl") or {}
        self.last_close_was_sl = {s: bool(src.get(s, False)) for s in self.symbols}

    # ---------- 日期清零（2026-11-07 的教训：旧版只在重启时清零，跑着不动就一直卡在 30 单上限）----------
    def reset_if_new_day(self, utc_date: str) -> bool:
        """UTC 日期变了就清零当日计数，返回 True 表示刚清过零。"""
        if utc_date == self.current_date:
            return False
        self.current_date = utc_date
        self.trades_today = {s: 0 for s in self.symbols}
        self.consecutive_losses = {s: 0 for s in self.symbols}
        self.daily_loss_usdt = 0.0
        self.daily_pnl_usdt = 0.0
        self.daily_loss_limit = MAX_DAILY_LOSS_USDT  # 新的一天，熔断线恢复 40 U
        self.last_close_at = {s: None for s in self.symbols}
        self.last_close_was_sl = {s: False for s in self.symbols}
        self.pause_until = {s: None for s in self.symbols}
        return True

    # ---------- 当日上限 ----------
    def can_trade(self, symbol: str, now: float | None = None,
                  votes: int = 0) -> tuple[bool, str]:
        """返回 (能不能开单, 原因)。原因空字符串表示可以开。

        冷却例外（2026-11-11 用户决定）：3/3 强信号可跳过冷却直接进场，
        2/3 票的信号继续等满冷却。连亏暂停、消息禁入、每日熔断是硬刹车，
        再强的信号也不能跳过。
        """
        if self.trades_today.get(symbol, 0) >= MAX_TRADES_PER_DAY:
            return False, f"{symbol} 今日已开 {self.trades_today.get(symbol, 0)} 单，达到上限 {MAX_TRADES_PER_DAY} 单。"
        if self.daily_pnl_usdt <= -self.daily_loss_limit:
            return False, f"今日净盈亏 {self.daily_pnl_usdt:.2f} U，达到熔断 -{self.daily_loss_limit:.0f} U。"
        if now is None:
            return True, ""
        if self.pause_until.get(symbol) is not None and now < self.pause_until[symbol]:
            left = int((self.pause_until[symbol] - now) / 60)
            return False, f"{symbol} 连亏 {MAX_CONSECUTIVE_LOSSES} 单熔断中，还要休息约 {left} 分钟。"
        if is_news_pause(now):
            return False, "大消息禁入窗口内（CPI / 非农 / FOMC 前后 30 分钟），不交易。"
        last_close = self.last_close_at.get(symbol)
        if last_close is not None:
            was_sl = self.last_close_was_sl.get(symbol, False)
            need = (COOLDOWN_AFTER_SL_MINUTES if was_sl
                    else COOLDOWN_MINUTES) * 60
            if now - last_close < need:
                left = int((need - (now - last_close)) / 60) + 1
                kind = "止损后" if was_sl else "平仓后"
                if votes >= 3:
                    return True, f"3/3 强信号，跳过{symbol} {kind}冷却（本来还要等约 {left} 分钟）。"
                return False, f"{symbol} {kind}冷却中，还要等约 {left} 分钟。"
        return True, ""

    def should_stop_for_day(self) -> bool:
        return self.daily_pnl_usdt <= -self.daily_loss_limit

    def record_trade_opened(self, symbol: str, now: float | None = None) -> None:
        self.trades_today[symbol] = self.trades_today.get(symbol, 0) + 1

    def record_trade_closed(self, symbol: str, pnl_usdt: float, was_sl: bool, now: float | None = None) -> None:
        """平仓后调用：更新当日盈亏（全账户）、连亏计数和冷却时间（按币种）。"""
        self.daily_pnl_usdt += pnl_usdt
        if pnl_usdt < 0:
            self.daily_loss_usdt += -pnl_usdt
            self.consecutive_losses[symbol] = self.consecutive_losses.get(symbol, 0) + 1
        else:
            self.consecutive_losses[symbol] = 0
        if now is not None:
            self.last_close_at[symbol] = now
            self.last_close_was_sl[symbol] = was_sl
            if self.consecutive_losses.get(symbol, 0) >= MAX_CONSECUTIVE_LOSSES:
                self.pause_until[symbol] = now + CONSECUTIVE_LOSS_PAUSE_MINUTES * 60

    # ---------- 仓位计算 ----------
    def size_position(self, entry: float, stop_loss: float) -> tuple[float, float, float]:
        """
        按风险倒推仓位：名义仓位 = 每单风险额 / 止损距离%，保证金封顶 200 U。
        每单风险额 = 本金 x 0.5%（10 U）。返回 (数量, 保证金 USDT, 预期最大亏损 USDT)。
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
