"""
风控常量 —— 写死在这里，策略代码改不了它们。

改这些数 = 改交易规则，必须用户亲口点头才能改。
本金假设 2000 USDT；如果用户本金变了，先改 ACCOUNT_SIZE_USDT，
其余上限按比例自动跟着变（单笔 2%、日亏 5%）。
"""
import json
import os
from pathlib import Path

# ---------- 标的与杠杆（写死）----------
SYMBOL = "BTCUSDT"            # 只做 BTCUSDT 永续，不做山寨币
LEVERAGE = 5                  # 杠杆 5 倍（用户平时用 10 倍，这次听 Muse 的降到 5 倍）
ACCOUNT_SIZE_USDT = 2000.0    # 本金（用户自述 2000+ USDT）

# ---------- 单笔风控（写死）----------
MAX_MARGIN_PER_TRADE_USDT = 200.0   # 一单最多 200 USDT 保证金
MAX_LOSS_PER_TRADE_USDT = 40.0      # 单笔最大亏损 40 USDT（本金 2%）
TARGET_LOSS_PER_TRADE_USDT = 20.0   # 单笔目标亏损 20 USDT（本金 1%），优先按这个算仓位
MIN_LOSS_PER_TRADE_USDT = 20.0      # 低于这个数说明止损太紧，信号多半是噪音

# ---------- 当日风控（写死）----------
MAX_DAILY_LOSS_USDT = 100.0   # 一天最多亏 100 USDT，达到即停手
MAX_TRADES_PER_DAY = 30       # 一天最多 30 单（2026-11-07 用户确认：区间快进快出改成 30 单）
MAX_TREND_TRADES_PER_DAY = 3  # 其中趋势单一天最多 3 单（2026-11-07 用户确认：原来一天3单的趋势打法也加上）

# ---------- 方向与冷却（2026-11-07 用户确认加上：不叠单、分界线跟市场走、均线交叉）----------
COOLDOWN_MINUTES = 10         # 同一方向冷却 10 分钟：同一位置 10 分钟内只做一次，不叠单、不白交手续费
MA_FAST_PERIOD = 5            # 快线：5 分钟均线
MA_SLOW_PERIOD = 20           # 慢线：20 分钟均线（金叉做多、死叉做空）
MIDLINE_WINDOW_MINUTES = 60   # 分界线：最近 60 分钟最高最低价的中点，跟着市场走，不是定死的 75,875

# ---------- 策略价位（2026-11-07 09:00 左右 BTC 约 75,890 时定，用户改成区间快进快出）----------
# 区间：昨晚到今早一直在 75,720–76,032 之间徘徊，现价 75,890 在区间中间。
# 区间上下沿取整：下沿 75,700，上沿 76,050。
SHORT_ZONE_LOW = 76500.0     # 趋势做空压力区下沿（突破后用）
SHORT_ZONE_HIGH = 77000.0    # 趋势做空压力区上沿
SHORT_STOP = 77800.0         # 趋势做空止损（压力区上沿之上）
SHORT_TP1 = 75200.0          # 趋势做空止盈 1
SHORT_TP2 = 74500.0          # 趋势做空止盈 2
LONG_TRIGGER = 76600.0       # 趋势做多触发：站稳 76600
LONG_STOP = 75800.0          # 趋势做多止损
LONG_TP = 77700.0            # 趋势做多止盈

# ---------- 区间快进快出（2026-11-07 用户确认改成这个）----------
RANGE_LOW = 75700.0          # 小区间下沿
RANGE_HIGH = 76050.0         # 小区间上沿
RANGE_LONG_ENTRY = 75800.0   # 小区间多：跌到 75,800 附近再收回来确认后买
RANGE_LONG_STOP = 75600.0    # 小区间多止损
RANGE_LONG_TP = 76000.0      # 小区间多止盈（区间上沿附近）
RANGE_SHORT_ENTRY = 76000.0  # 小区间空：涨到 76,000 附近再收回来确认后卖
RANGE_SHORT_STOP = 76200.0   # 小区间空止损
RANGE_SHORT_TP = 75800.0     # 小区间空止盈（区间下沿附近）

# ---------- 扩大区间 500 / 1000 点（2026-11-07 用户问能不能扩大，合成进组合版）----------
# 500 点档：用 75,500–76,500 这一档，目标 500 点，止损 200 点，净赚约 5.6 U，盈亏平衡胜率约 38%。
# 1000 点档：用 75,200–76,600 这一档，目标 1000 点，止损 300 点，净赚约 12 U，触达次数少。
# 组合版里 500 点档由小区间 + 大区间共同覆盖：小区间 200 点快进快出，大区间 1000 点吃大波段。
BIG_RANGE_LONG_ENTRY = 75400.0   # 大区间多：跌到 75,400 附近再收回来确认后买
BIG_RANGE_LONG_STOP = 75100.0    # 大区间多止损
BIG_RANGE_LONG_TP = 76400.0      # 大区间多止盈（1000 点）
BIG_RANGE_SHORT_ENTRY = 76400.0  # 大区间空：涨到 76,400 附近再收回来确认后卖
BIG_RANGE_SHORT_STOP = 76700.0   # 大区间空止损
BIG_RANGE_SHORT_TP = 75400.0     # 大区间空止盈（1000 点）

# ---------- 趋势突破自动切换（用户：有新趋势你再自己分析改动）----------
# 向上突破：连续 2 分钟站稳 76,600 -> 按趋势做多（沿用 LONG_TRIGGER/LONG_STOP/LONG_TP）
# 向下破位：连续 2 分钟收在 75,200 下方 -> 按趋势做空
TREND_SHORT_TRIGGER = 75200.0  # 趋势破位做空触发
TREND_SHORT_STOP = 76000.0     # 趋势破位做空止损
TREND_SHORT_TP1 = 74500.0      # 趋势破位做空止盈 1
TREND_SHORT_TP2 = 73700.0      # 趋势破位做空止盈 2

# ---------- 运行模式 ----------
# 默认 Dry-Run：只看行情、只记“本来会下的单”，绝不下真实单。
# 实盘要同时满足：环境变量 BINANCE_LIVE_TRADING=1
# 且 config.json 里 live_trading_enabled=true。缺一个都不开实盘。
DRY_RUN = os.environ.get("BINANCE_DRY_RUN", "1") != "0"
LIVE_TRADING_ENABLED = False  # 默认 False；config.json 可覆盖，但仍需环境变量配合


def load_config() -> dict:
    """读 config.json（不存在则返回空 dict，不报错）。"""
    p = Path(__file__).resolve().parent / "config.json"
    if not p.exists():
        return {}
    cfg = json.loads(p.read_text(encoding="utf-8"))
    global LIVE_TRADING_ENABLED
    if cfg.get("live_trading_enabled") is True:
        LIVE_TRADING_ENABLED = True
    return cfg


def load_api_credentials() -> tuple[str, str]:
    """
    只从环境变量或 config.json 读 Key，绝不写死。
    返回 (api_key, api_secret)，Secret 只返回给调用方，绝不打印。
    优先环境变量，其次 config.json。
    """
    cfg = load_config()
    api_key = os.environ.get("BINANCE_API_KEY") or cfg.get("api_key", "")
    api_secret = os.environ.get("BINANCE_API_SECRET") or cfg.get("api_secret", "")
    if not api_key or not api_secret:
        raise RuntimeError(
            "未找到 BINANCE_API_KEY / BINANCE_API_SECRET："
            "请设环境变量，或在 config.json 里填（不要提交到任何公开地方）。"
        )
    return api_key, api_secret


# 启动时先读一次 config.json（只为确认 live 开关，不读 Secret 到日志）
load_config()
