"""
风控常量 —— 写死在这里，策略代码改不了它们。

【剥头皮测试版 2026-11-17，用户决定：先做模拟盘版本测几天，放日本服务器跑】
和德国实盘版的区别只有四处，其余全部照抄（信号三模式、冷却、熔断、保本逻辑都不动）：
1. 止损 3 倍 ATR（用户：亏损线放远一点，不会被噪音突然打掉）
2. 止盈不再按 3 倍 ATR：浮盈扣掉全部手续费，净赚 SCALP_NET_TP_USDT（2U）就平
3. 每天每币上限 50 单（用户目标一天 100 单；模拟盘测试，额度不做瓶颈）
4. 每单固定 200U 保证金（用户 2026-11-17 决定：不想收紧止损，改固定仓位；10 倍杠杆 = 2000U 名义仓位；仓位不再按风险倒推，止损距离只决定最大亏损，不决定仓位大小）
手续费地板不启用：止盈本身就是净赚 2U（已扣费），不可能被手续费吃掉。

改这些数 = 改交易规则，必须用户亲口点头才能改。
本金假设 2000 USDT；如果用户本金变了，先改 ACCOUNT_SIZE_USDT，
其余上限按比例自动跟着变。
"""
import json
import os
from pathlib import Path

# ---------- 标的与杠杆（写死）----------
SYMBOLS = ["BTCUSDT", "ETHUSDT"]  # BTC + ETH 永续，同规则各跑各的（2026-11-11 用户确认加 ETH）
LEVERAGE = 10                 # 杠杆 10 倍（2026-11-10 用户确认：10 倍快进快出）
ACCOUNT_SIZE_USDT = 2000.0    # 本金（用户自述 2000+ USDT）

# ---------- 行情接口 ----------
# 部署环境（Cloady）能直连 fapi.binance.com；本地测试时把环境变量
# BINANCE_DATA_BASE_URL 设成 https://data-api.binance.vision。
DATA_BASE_URL = os.environ.get(
    "BINANCE_DATA_BASE_URL", "https://fapi.binance.com"
).rstrip("/")

# ---------- 单笔风控（写死）----------
MAX_MARGIN_PER_TRADE_USDT = 400.0   # 一单最多 400 USDT 保证金（2026-11-11 用户决定少而精方案：从 200 U 提到 400 U）
FIXED_MARGIN_USDT = 200.0           # 剥头皮测试：每单固定 200U 保证金（用户 2026-11-17 决定：不想收紧、放大止损后改固定仓位；10 倍杠杆 = 2000U 名义仓位；仓位不再按风险倒推）
RISK_PER_TRADE_PCT = 0.005          # 每单风险目标：本金 0.5%（10 U）——固定保证金后不再用于算仓位，仅保留作参考
MAX_LOSS_PER_TRADE_USDT = 40.0      # 单笔最大亏损 40 USDT（本金 2%），硬上限

# ---------- 当日风控（写死）----------
MAX_DAILY_LOSS_USDT = 100.0   # 一天最多亏 100 USDT（本金 5%），达到即停手（2026-11-11 用户决定：从 40 U 改回 100 U）
CONSERVATIVE_DAILY_LOSS_USDT = 50.0  # state.json 丢失且非 UTC 日初启动时，当日熔断收紧到 50 U
                                      # （2026-11-11 修计数重置：容器重启会丢当日盈亏计数，收紧一半兜底；
                                      # 正常日子仍是 100 U，UTC 午夜自动恢复）
MAX_TRADES_PER_DAY = 50       # 剥头皮测试：每个币种一天最多 50 单（用户目标一天 100 单；模拟盘测试，额度不做瓶颈）
MAX_OPEN_POSITIONS = 1        # 每个币种同时最多 1 单，不叠单

# ---------- 冷却与熔断（写死）----------
COOLDOWN_MINUTES = 10                 # 平仓后冷却 10 分钟（用户 2026-11-17 决定：15 分钟改 10 分钟）
COOLDOWN_AFTER_SL_MINUTES = 30        # 止损出场后冷却 30 分钟
MAX_CONSECUTIVE_LOSSES = 3            # 连亏 3 单
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60  # 连亏 3 单后休息 60 分钟

# ---------- 信号与指标（写死）----------
ATR_PERIOD = 14              # ATR 周期（5 分钟 K 线）
ADX_PERIOD = 14              # ADX 周期（5 分钟 K 线；只作参考，不再设门槛）
MA_FAST_PERIOD = 5           # 15 分钟快线
MA_SLOW_PERIOD = 20          # 15 分钟慢线
VOL_SMA_PERIOD = 20          # 量能均线周期（15 分钟）
SL_ATR_MULT = 3.0            # 止损 = 3 倍 ATR（剥头皮测试：用户决定放远，2026-11-11 实盘版是 2 倍；放远是不被噪音突然打掉）
TP_ATR_MULT = 3.0            # 剥头皮版不用（止盈按净赚 2U 算，见 SCALP_NET_TP_USDT）；留着是免得别处引用报错
FEE_FLOOR_PCT = 0.0030       # 剥头皮版不启用（止盈本身就是净赚 2U，已扣费）；留着是免得别处引用报错
SCALP_NET_TP_USDT = 2.0      # 剥头皮止盈：浮盈扣掉全部手续费，净赚 2U 就平（用户 2026-11-17 决定）
BREAKEVEN_AT_R = 1.0         # 赚到 1 倍风险就把止损移到成本价（保本）

# ---------- 过度延伸过滤（2026-11-16 用户决定，治“信号打在反转点”）----------
# 上一单的教训：信号价 64143.7 就是 10 小时最低点（09:15 UTC 那根 K 线最低 64128），
# 3/3 票 + ADX 29 全是最强看空，但跌得越狠离反弹越近——信号打在底上。
# 规则：做空信号时，现价比过去 6 小时高点跌了超过 3 倍 ATR 就不追空；
# 做多信号时，现价比过去 6 小时低点涨了超过 3 倍 ATR 就不追多。
# 代价：强趋势里也会挡掉一些好信号（趋势中价格同样会跌很多才出信号），
# 阈值是起点，可调。
OVEREXTENSION_ATR_MULT = 3.0  # 过度延伸阈值：离 6 小时极端点超过 3 倍 ATR 就不追
SIGNAL_INTERVAL = "5m"        # 信号 K 线周期（用户 2026-11-17 决定改成 5 分钟：假信号也没啥，赚 2U 就走了）
RANGE_LOOKBACK_CANDLES = 72    # 6 小时区间 = 72 根 5 分钟 K 线

# ---------- 策略模式（2026-11-16 用户决定：保留全部策略，按大趋势智能调配）----------
# 三种模式共用风控（止损止盈 2x/3x ATR、保本、冷却、额度），只换开单规则。
# 模式由 1 小时 ADX（大趋势强度）决定：>=25 强趋势用 trend（原 6 胜策略思路，
# 前两天大跌时能用上），15~25 过渡用 momentum（当前动量策略），<15 震荡用 range
# （反转策略）。2026-11-16 晚 BTC/ETH 的 1h ADX 只有 5.0/10.6，都是 range。
REGIME_ADX_TREND = 25.0   # 1h ADX >= 25：强趋势 -> trend 模式
REGIME_ADX_RANGE = 15.0   # 1h ADX < 15：震荡 -> range 模式；15~25 之间 -> momentum 模式
EMA_TREND_PERIOD = 50     # trend 模式的方向过滤均线（5 分钟 EMA50；用户 2026-11-15 嫌 EMA200 太慢改成 50）
TREND_ADX_MIN = 25.0      # trend 模式的 ADX 门槛（5 分钟 ADX > 25 才做；用户 2026-11-16 从 30 降到 25）
RANGE_EDGE_FRACTION = 0.25  # range 模式：只在 6 小时区间的下 1/4 做多、上 1/4 做空

# ---------- 轮询间隔（2026-11-11 修止损滑点：20 秒太长，价格尖峰会穿过去）----------
HOLDING_POLL_SECONDS = 5    # 有持仓时每 5 秒查一次止盈/止损/保本
FLAT_POLL_SECONDS = 60      # 空仓时每 60 秒判断一次信号
HEARTBEAT_LOG_SECONDS = 20  # 持仓心跳每 20 秒记一行（只记日志，止损每 5 秒都查）

# ---------- 手续费（币安 USD-M 永续 VIP0，算盈亏用）----------
TAKER_FEE_PCT = 0.0005       # Taker 0.05%/边
MAKER_FEE_PCT = 0.0002       # Maker 0.02%/边

# ---------- 大消息禁入窗口（写死，UTC 时间）----------
# 格式：[("2026-11-12T13:30:00", "2026-11-12T14:30:00"), ...]
# 分别是窗口开始和结束（各含前后 30 分钟）。没有已知大消息时留空。
# CPI / 非农 / FOMC 前后 30 分钟不交易（2026-11-10 用户确认的方案）。
NEWS_PAUSE_WINDOWS: list[tuple[str, str]] = [
    ("2026-11-13T13:00:00", "2026-11-13T14:00:00"),  # PPI（10 月），北京时间 11-13 21:30（BLS 核实）
    ("2026-11-18T18:30:00", "2026-11-18T19:30:00"),  # FOMC 会议纪要（10 月会议），北京时间 11-19 03:00
    ("2026-11-25T13:00:00", "2026-11-25T14:00:00"),  # PCE（10 月）+ GDP 三季度二读，北京时间 11-25 21:30（BEA 核实）
    ("2026-12-04T13:00:00", "2026-12-04T14:00:00"),  # 非农（11 月），北京时间 12-04 21:30
    ("2026-12-09T18:30:00", "2026-12-09T19:30:00"),  # FOMC 决议，北京时间 12-10 03:00
    ("2026-12-10T13:00:00", "2026-12-10T14:00:00"),  # CPI（11 月），北京时间 12-10 21:30
    ("2026-12-15T13:00:00", "2026-12-15T14:00:00"),  # PPI（11 月），北京时间 12-15 21:30（BLS 核实）
]

# ---------- 实盘入场等待（有界等待：超时未成交就撤单，绝不等死）----------
ENTRY_FILL_TIMEOUT_SECONDS = 60  # Post Only 入场单最多等 60 秒
ENTRY_POLL_SECONDS = 2           # 等成交时每 2 秒查一次订单状态
RECV_WINDOW_MS = 60000           # 币安签名请求容忍时间（毫秒），上限 60000；2026-11-14 实盘 -1021：Cloady 容器时钟与币安差 >5 秒，默认 5000 毫秒不够
RECONCILE_RETRY_SECONDS = 900    # 启动对账失败的币种，主循环每 15 分钟重试一次（2026-11-15 实盘事故：启动时一次瞬时失败就永久堵死 BTC，6 个 3/3 信号全被挡）
MAKER_RETRY_BUFFER_PCT = 0.0005  # -5022 重挂价的 maker 安全缓冲 0.05%：做空=最优卖价+0.05%，做多=最优买价-0.05%（2026-11-15 实盘：贴盘口的重挂价 1 秒内又被穿，再被 -5022 拒）

# ---------- 运行模式 ----------
# 默认 Dry-Run：只看行情、只记“本来会下的单”，绝不下真实单。
# 实盘要同时满足下面三项，缺一个都不开实盘（2026-11-14 接通实盘时把
# BINANCE_LIVE_TRADING=1 的检查真正写进代码，之前只在注释里）：
#   1. 环境变量 BINANCE_DRY_RUN=0
#   2. config.json 里 live_trading_enabled=true
#   3. 环境变量 BINANCE_LIVE_TRADING=1
DRY_RUN = os.environ.get("BINANCE_DRY_RUN", "1") != "0"
LIVE_TRADING_ENABLED = False  # 默认 False；config.json 可覆盖，但仍需环境变量配合


def live_trading_missing() -> list[str]:
    """返回实盘三项条件里缺了哪几项（空列表 = 三项全满足）。"""
    missing = []
    if DRY_RUN:
        missing.append("BINANCE_DRY_RUN 不是 0（现在是 Dry-Run）")
    if not LIVE_TRADING_ENABLED:
        missing.append("config.json 里 live_trading_enabled 不是 true")
    if os.environ.get("BINANCE_LIVE_TRADING", "0") != "1":
        missing.append("环境变量 BINANCE_LIVE_TRADING 不是 1")
    return missing


def live_trading_active() -> bool:
    """实盘开关：三项条件同时满足才返回 True。调用时才判断，不只看启动时的快照。"""
    return not live_trading_missing()


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
