"""runtime/* 写盘 hooks — 给外部 daemon (TG bot @example_daemon_bot) 当事实源用。

设计原则：
  - 全部 try/except 包，写盘失败绝不影响主流程
  - 文件超过 10MB 自动 rotate 到 .old（约 50000 行 × 200 bytes）
  - JSON dump atomic：tmp + os.replace
  - B 加 60 秒节流，避免高频 I/O

文件清单：
  runtime/orders.jsonl       — 新订单 append (A)
  runtime/account_stats.json — 账号状态快照 atomic rewrite (B)
  runtime/im_metadata.jsonl  — IM 元数据 append（无文本，PII safe）(C)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent
_RUNTIME_DIR = _BASE_DIR / "runtime"

_JSONL_MAX_SIZE = 10 * 1024 * 1024  # 10 MB → ~50000 行

# B 节流状态
_LAST_STATS_WRITE_TS: float = 0.0
_STATS_WRITE_LOCK = threading.Lock()
_STATS_THROTTLE_SEC = 60.0


def _ensure_runtime_dir() -> None:
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _append_jsonl_with_rotation(path: Path, record: Dict[str, Any]) -> None:
    """append 一行 JSON。文件 > 10MB 时 rotate 到 .old。失败不抛。"""
    try:
        _ensure_runtime_dir()
        # rotate 检查
        try:
            if path.exists() and path.stat().st_size > _JSONL_MAX_SIZE:
                old_path = path.with_suffix(path.suffix + ".old")
                try:
                    if old_path.exists():
                        old_path.unlink()
                except Exception:
                    pass
                try:
                    path.rename(old_path)
                except Exception:
                    pass
        except Exception:
            pass
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 静默失败，绝不影响主流程


def _atomic_write_json(path: Path, data: Any) -> None:
    """atomic 写 JSON：tmp + os.replace。失败不抛。"""
    try:
        _ensure_runtime_dir()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


# ── A. 订单推送 hook ────────────────────────────────────
def log_order_pushed(*, account: str, item_id: str, title: str,
                      price: str, buyer: str, yahoo_url: str,
                      # 2026-04-30 v6.0.48:從 isoredux-data 補資料,daemon TG 卡顯示完整訂單
                      # 全部 optional + default "",舊 caller 不變仍兼容
                      order_id: str = "",
                      buyer_label: str = "",
                      payment_status: str = "",
                      payment_type: str = "",
                      shipping_status: str = "",
                      shipping_method: str = "",
                      order_status: str = "") -> None:
    """新订单推送主管 TG 后调用。append 1 行到 runtime/orders.jsonl"""
    _append_jsonl_with_rotation(_RUNTIME_DIR / "orders.jsonl", {
        "ts": time.time(),
        "account": account,
        "item_id": item_id,
        "title": title,
        "price": price,
        "buyer": buyer,
        "yahoo_url": yahoo_url,
        # v6.0.48 新欄位(舊 caller 不傳就空,daemon 自己 fallback)
        "order_id": order_id,
        "buyer_label": buyer_label,
        "payment_status": payment_status,
        "payment_type": payment_type,
        "shipping_status": shipping_status,
        "shipping_method": shipping_method,
        "order_status": order_status,
    })


# ── B. 账号状态快照 hook ─────────────────────────────
def dump_account_stats(stats: Dict[str, Dict[str, Any]]) -> None:
    """节流 dump 账号状态到 runtime/account_stats.json。
    stats: {profile_id: {item_count, paid_to_ship, cod, im, updated_ts}}
    60 秒内最多 1 次写盘（避免高频 I/O）。
    """
    global _LAST_STATS_WRITE_TS
    with _STATS_WRITE_LOCK:
        now = time.time()
        if now - _LAST_STATS_WRITE_TS < _STATS_THROTTLE_SEC:
            return
        _LAST_STATS_WRITE_TS = now
    _atomic_write_json(_RUNTIME_DIR / "account_stats.json", stats)


# ── C. IM 元数据 hook（无文本，PII safe）────────────
def log_im_metadata(*, account: str, channel_id: str, sender_id: str,
                     has_gpt_draft: bool, replied: bool, msg_len: int) -> None:
    """新 IM 来时调用。只记 metadata，不记文本内容（避免 PII 落盘）。"""
    _append_jsonl_with_rotation(_RUNTIME_DIR / "im_metadata.jsonl", {
        "ts": time.time(),
        "account": account,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "has_gpt_draft": bool(has_gpt_draft),
        "replied": bool(replied),
        "msg_len": int(msg_len or 0),
    })
