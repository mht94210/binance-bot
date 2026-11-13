"""
风控常量 —— 写死在这里，策略代码改不了它们。

改这些数 = 改交易规则，必须用户亲口点头才能改。
本金假设 2000 USDT；如果用户本金变了，先改 ACCOUNT_SIZE_USDT，
其余上限按比例自动跟着变。

版本：震荡版（高空低多），2026-11-11 用户确认：日本机器人从老版改成震荡版，
和德国趋势版（少而精，ADX>30 才做）互补。
- 杠杆 10 倍，保证金 200 U/单，名义仓位 2000 U
- 信号：15 分钟 K 线收盘时判断；区间 = 最近 24 根 15 分钟 K 线（6 小时）最高/最低；
  只做震荡：15 分钟 ADX 必须 <25（有趋势不动，趋势交给德国版）；
  现价在区间上沿 25% 区域内做空，下沿 25% 区域内做多；区间中部不动；
  已突破上沿/下沿（疑似趋势启动）不追；区间太窄（<0.6%）不动
- 止损：上沿/下沿外 0.5 倍 ATR；止盈：对侧沿（整段区间）；盈亏比约 1:2 到 1:4
- 保证金 200 U/单；冷却 15 分钟，止损后 30 分钟；每个币种每天最多 10 单、同时最多 1 单
  （全账户同时最多 2 单、每天最多 20 单）；单数、冷却、连亏都按币种各算各的，
  每天净亏熔断按全账户合起来算
- 进场限价单（Post Only，吃 maker 手续费），止损市价单
- 保本：赚到 1 倍风险就把止损移到成本价
- 熔断：一天亏 5%（100 U）停；连亏 3 单休息 1 小时；大消息前后 30 分钟不交易
- 手续费地板：止盈目标 < 0.30% 不做
"""
import json
import os
from pathlib import Path

# ---------- 标的与杠杆（写死）----------
SYMBOLS = ["BTCUSDT", "ETHUSDT"]  # BTC + ETH 永续，同规则各跑各的
LEVERAGE = 10                 # 杠杆 10 倍
ACCOUNT_SIZE_USDT = 2000.0    # 本金（用户自述 2000+ USDT）

# ---------- 行情接口 ----------
# 部署环境（Cloady）能直连 fapi.binance.com；本地测试时把环境变量
# BINANCE_DATA_BASE_URL 设成 https://data-api.binance.vision。
DATA_BASE_URL = os.environ.get(
    "BINANCE_DATA_BASE_URL", "https://fapi.binance.com"
).rstrip("/")

# ---------- 单笔风控（写死）----------
MAX_MARGIN_PER_TRADE_USDT = 200.0   # 一单最多 200 USDT 保证金（震荡版单多、单笔小）
RISK_PER_TRADE_PCT = 0.005          # 每单风险目标：本金 0.5%（10 U），按止损距离倒推仓位
MAX_LOSS_PER_TRADE_USDT = 40.0      # 单笔最大亏损 40 USDT（本金 2%），硬上限

# ---------- 当日风控（写死）----------
MAX_DAILY_LOSS_USDT = 100.0   # 一天最多亏 100 USDT（本金 5%），达到即停手
CONSERVATIVE_DAILY_LOSS_USDT = 50.0  # state.json 丢失且非 UTC 日初启动时，当日熔断收紧到 50 U
                                      # （容器重启会丢当日盈亏计数，收紧一半兜底；
                                      # 正常日子仍是 100 U，UTC 午夜自动恢复）
MAX_TRADES_PER_DAY = 10       # 每个币种一天最多 10 单
MAX_OPEN_POSITIONS = 1        # 每个币种同时最多 1 单，不叠单

# ---------- 冷却与熔断（写死）----------
COOLDOWN_MINUTES = 15                 # 平仓后冷却 15 分钟
COOLDOWN_AFTER_SL_MINUTES = 30        # 止损出场后冷却 30 分钟
MAX_CONSECUTIVE_LOSSES = 3            # 连亏 3 单
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60  # 连亏 3 单后休息 60 分钟

# ---------- 信号与指标（写死）----------
ATR_PERIOD = 14              # ATR 周期（15 分钟 K 线）
ADX_PERIOD = 14              # ADX 周期（15 分钟 K 线）
ADX_MAX = 25                 # ADX 低于 25 才算震荡（高于 25 说明有趋势，震荡策略不动）
RANGE_LOOKBACK = 24          # 区间回看：最近 24 根 15 分钟 K 线（6 小时）的最高/最低
RANGE_MIN_WIDTH_PCT = 0.006  # 区间宽度至少 0.6%（太窄是死水，噪音会反复打止损）
EDGE_ZONE = 0.25             # 上沿/下沿各 25% 的区域是入场区（中间 50% 不动）
SL_EDGE_ATR_MULT = 0.5       # 止损放在区间沿外 0.5 倍 ATR
FEE_FLOOR_PCT = 0.0030       # 手续费地板：止盈目标 < 0.30% 不做
BREAKEVEN_AT_R = 1.0         # 赚到 1 倍风险就把止损移到成本价（保本）

# ---------- 轮询间隔（持仓 5 秒一轮防滑点，空仓 60 秒一轮）----------
HOLDING_POLL_SECONDS = 5    # 有持仓时每 5 秒查一次止盈/止损/保本
FLAT_POLL_SECONDS = 60      # 空仓时每 60 秒判断一次信号
HEARTBEAT_LOG_SECONDS = 20  # 持仓心跳每 20 秒记一行（只记日志，止损每 5 秒都查）

# ---------- 手续费（币安 USD-M 永续 VIP0，算盈亏用）----------
TAKER_FEE_PCT = 0.0005       # Taker 0.05%/边
MAKER_FEE_PCT = 0.0002       # Maker 0.02%/边

# ---------- 大消息禁入窗口（写死，UTC 时间）----------
# 格式：[("2026-11-12T13:30:00", "2026-11-12T14:30:00"), ...]
# 分别是窗口开始和结束（各含前后 30 分钟）。没有已知大消息时留空。
# CPI / 非农 / FOMC 前后 30 分钟不交易。
NEWS_PAUSE_WINDOWS: list[tuple[str, str]] = [
    ("2026-11-13T13:00:00", "2026-11-13T14:00:00"),  # PPI（10 月），北京时间 11-13 21:30（BLS 核实）
    ("2026-11-18T18:30:00", "2026-11-18T19:30:00"),  # FOMC 会议纪要（10 月会议），北京时间 11-19 03:00
    ("2026-11-25T13:00:00", "2026-11-25T14:00:00"),  # PCE（10 月）+ GDP 三季度二读，北京时间 11-25 21:30（BEA 核实）
    ("2026-12-04T13:00:00", "2026-12-04T14:00:00"),  # 非农（11 月），北京时间 12-04 21:30
    ("2026-12-09T18:30:00", "2026-12-09T19:30:00"),  # FOMC 决议，北京时间 12-10 03:00
    ("2026-12-10T13:00:00", "2026-12-10T14:00:00"),  # CPI（11 月），北京时间 12-10 21:30
    ("2026-12-15T13:00:00", "2026-12-15T14:00:00"),  # PPI（11 月），北京时间 12-15 21:30（BLS 核实）
]

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
