from __future__ import annotations
import random
from typing import Optional

_rng = random.Random()

def human_interval_sec(base_sec: float, factor: float = 2.0, min_sec: float = 0.0, max_cap_sec: Optional[float] = None) -> float:
    """
    将固定间隔变成“人类式”的随机间隔。
    - base_sec=300 -> 每次随机 [300, 600]
    - factor 默认 2.0
    - min_sec: 保底最小值
    - max_cap_sec: 可选上限，避免极端慢（不传则不限制）
    """
    try:
        b = float(base_sec)
    except Exception:
        b = 0.0
    b = max(min_sec, b)
    if b <= 0:
        return 0.0
    hi = b * float(factor if factor and factor > 0 else 2.0)
    if max_cap_sec is not None:
        hi = min(hi, float(max_cap_sec))
    if hi <= b:
        return b
    return _rng.uniform(b, hi)

def human_jitter_ms(base_ms: float, low: float = 0.90, high: float = 1.40, min_ms: int = 50, max_ms: Optional[int] = None) -> int:
    """
    小等待用的抖动（毫秒），默认只在 0.9x~1.4x 之间随机，
    既有随机性，也尽量不把等待变得太短导致页面未就绪。
    """
    try:
        b = float(base_ms)
    except Exception:
        b = 0.0
    if b <= 0:
        return 0
    lo = max(0.0, b * float(low))
    hi = max(lo + 1.0, b * float(high))
    v = _rng.uniform(lo, hi)
    v = max(float(min_ms), v)
    if max_ms is not None:
        v = min(float(max_ms), v)
    return int(v)

def maybe_extra_think_ms(chance: float = 0.06, extra_min_ms: int = 600, extra_max_ms: int = 1800) -> int:
    """
    偶尔多停一下，模拟“看一眼/想一下”，默认 6% 概率，多停 0.6~1.8 秒。
    为了不拖慢整体节奏，概率和区间都很克制。
    """
    if chance <= 0:
        return 0
    if _rng.random() >= chance:
        return 0
    if extra_max_ms <= extra_min_ms:
        return int(extra_min_ms)
    return int(_rng.uniform(extra_min_ms, extra_max_ms))
