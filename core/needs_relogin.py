"""v6.1.6:標記「需重新登入」帳號 — 偵測到 cookies 不全 + login redirect 時自動寫 flag。

GUI 帳號列表會顯示「需重登」狀態,使用者一眼看出該重登哪些帳號。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional


def _flag_path(profile_dir: Path) -> Path:
    """flag 檔位置:profile_dir/.needs_relogin.flag"""
    return Path(profile_dir) / ".needs_relogin.flag"


def mark_needs_relogin(profile_dir: Path, reason: str = "") -> bool:
    """寫 flag 標記該帳號需重登。返回 True 表示「第一次」標記(可送 1 次性通知)。"""
    profile_dir = Path(profile_dir)
    fp = _flag_path(profile_dir)
    is_new = not fp.exists()
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        fp.write_text(
            f"{int(time.time())}\n{reason or '需重登'}",
            encoding="utf-8",
        )
    except Exception:
        return False
    return is_new


def clear_needs_relogin(profile_dir: Path) -> None:
    """清除 flag(重登成功 / cookie 恢復後呼叫)。"""
    fp = _flag_path(Path(profile_dir))
    try:
        if fp.exists():
            fp.unlink()
    except Exception:
        pass


def is_needs_relogin(profile_dir: Path) -> bool:
    """檢查該帳號是否被標記為需重登。"""
    return _flag_path(Path(profile_dir)).exists()


def get_relogin_info(profile_dir: Path) -> Optional[dict]:
    """讀 flag 內容,返回 {marked_at, reason}。"""
    fp = _flag_path(Path(profile_dir))
    if not fp.exists():
        return None
    try:
        content = fp.read_text(encoding="utf-8").strip()
        parts = content.split("\n", 1)
        return {
            "marked_at": int(parts[0]) if parts[0].isdigit() else 0,
            "reason": parts[1] if len(parts) > 1 else "",
        }
    except Exception:
        return None
