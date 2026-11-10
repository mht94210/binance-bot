"""
技术指标（纯函数，不碰 API，方便单测）。

K 线统一用 dict 表示：{"open","high","low","close","volume"}，
全部是 float。只用已收盘的 K 线算指标，未收盘的那根不算。
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """简单均线；数据不够返回 None。"""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(closes: list[float], period: int) -> list[float]:
    """EMA 序列：前 period 根用 SMA 当种子，之后按 Wilder 递推。
    数据不够返回空 list。"""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(closes[:period]) / period]
    for c in closes[period:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def ema(closes: list[float], period: int) -> float | None:
    """最新一根的 EMA；数据不够返回 None。"""
    series = ema_series(closes, period)
    return series[-1] if series else None


def atr(klines: list[dict], period: int = 14) -> float | None:
    """Wilder ATR：先算 True Range，再用 Wilder 平滑。
    需要至少 period+1 根 K 线（第一根没有昨收，TR 用 high-low）。"""
    if len(klines) < period + 1:
        return None
    trs: list[float] = []
    for i, k in enumerate(klines):
        if i == 0:
            trs.append(k["high"] - k["low"])
        else:
            prev_close = klines[i - 1]["close"]
            trs.append(max(
                k["high"] - k["low"],
                abs(k["high"] - prev_close),
                abs(k["low"] - prev_close),
            ))
    # 种子：前 period 个 TR 的均值，之后 Wilder 递推
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx(klines: list[dict], period: int = 14) -> tuple[float | None, float | None, float | None]:
    """Wilder ADX，返回 (adx, plus_di, minus_di)。
    需要至少 2*period 根 K 线（先平滑出 DX，再对 DX 做 period 平滑）。
    数据不够时对应项返回 None。"""
    n = len(klines)
    if n < period + 1:
        return None, None, None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i, k in enumerate(klines):
        if i == 0:
            trs.append(k["high"] - k["low"])
            plus_dm.append(0.0)
            minus_dm.append(0.0)
            continue
        prev = klines[i - 1]
        trs.append(max(
            k["high"] - k["low"],
            abs(k["high"] - prev["close"]),
            abs(k["low"] - prev["close"]),
        ))
        up = k["high"] - prev["high"]
        down = prev["low"] - k["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    # Wilder 平滑：种子是前 period 个之和
    tr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dxs: list[float] = []
    for i in range(period, n):
        tr_s = tr_s - tr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        if tr_s == 0:
            dxs.append(0.0)
            continue
        plus_di = 100.0 * plus_s / tr_s
        minus_di = 100.0 * minus_s / tr_s
        di_sum = plus_di + minus_di
        dxs.append(100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

    if not dxs:
        return None, None, None
    # 最后的 +DI / -DI（用最新平滑值）
    plus_di = 100.0 * plus_s / tr_s if tr_s else 0.0
    minus_di = 100.0 * minus_s / tr_s if tr_s else 0.0
    if len(dxs) < period:
        return None, plus_di, minus_di
    adx_value = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_value = (adx_value * (period - 1) + dx) / period
    return adx_value, plus_di, minus_di
