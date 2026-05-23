from __future__ import annotations
import json
import os
import re
import threading
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "accounts.json"
SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

# 线程锁：防止 UI 线程和监控线程同时读写 JSON 导致数据损坏
_accounts_lock = threading.Lock()
_settings_lock = threading.Lock()


def _atomic_write_json(filepath: Path, data: Any) -> None:
    """原子写入 JSON：先写临时文件再 rename，防止写入一半崩溃导致文件损坏。"""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(filepath.parent), suffix=".tmp", prefix=filepath.stem + "_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Windows 上 rename 目标存在会报错，需要先删除
        if filepath.exists():
            filepath.unlink()
        os.rename(tmp_path, str(filepath))
    except Exception:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise

# Windows 保留文件名（不区分大小写）
_WINDOWS_RESERVED = {
    "con","prn","aux","nul",
    *(f"com{i}" for i in range(1,10)),
    *(f"lpt{i}" for i in range(1,10)),
}

def sanitize_profile_id(raw: str) -> str:
    """把 ProfileID 处理成安全的目录名（避免空格/中文/特殊符号/路径穿越）。
    - 只保留 [a-zA-Z0-9_@.+-]（兼容邮箱/常见标识符）
    - 其他字符统一替换为 _
    - 去掉首尾 _-
    - 长度限制 48（避免路径过长）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # 防止用户直接填了路径
    s = s.replace("\\", "_").replace("/", "_")
    # 允许中文其实也能用，但为了“跨机器/跨脚本稳定”，统一做安全化
    s = re.sub(r"[^a-zA-Z0-9_@.\-+]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    # 额外保护：避免 ProfileID = "." 或 ".." 造成路径穿越；并处理 Windows 尾部 "." / 空格
    s = s.rstrip(" .")
    if not s or s in {".", ".."} or re.fullmatch(r"\.+", s):
        return ""
    if not s:
        return ""
    # Windows 保留名处理
    if s.lower() in _WINDOWS_RESERVED:
        s = f"p_{s}"
    # 长度限制
    return s[:48]

def normalize_profile_id(pid: str) -> str:
    """用于判重：Windows/多数环境下路径大小写不敏感，所以用 casefold 判重更稳。"""
    return (pid or "").strip().casefold()

def validate_unique_profile_ids(accounts: List[Dict[str, Any]]) -> Tuple[bool, str]:
    seen: Dict[str, str] = {}
    dups: List[str] = []
    for a in accounts:
        pid = str(a.get("profile_id","") or "").strip()
        n = normalize_profile_id(pid)
        if not pid:
            dups.append("(空 ProfileID)")
            continue
        if n in seen and seen[n] != pid:
            # 记录一条“原样”提示即可
            dups.append(f"{seen[n]}  <->  {pid}")
        else:
            seen[n] = pid
    if dups:
        msg = "发现重复/无效 ProfileID（会导致 Cookie 混用或覆盖）：\n- " + "\n- ".join(dups[:30])
        if len(dups) > 30:
            msg += f"\n... 还有 {len(dups)-30} 条"
        return False, msg
    return True, ""

def load_accounts() -> List[Dict[str, Any]]:
    with _accounts_lock:
        if not ACCOUNTS_FILE.exists():
            return []
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))

def save_accounts(accounts: List[Dict[str, Any]]) -> None:
    ok, msg = validate_unique_profile_ids(accounts)
    if not ok:
        raise ValueError(msg)
    with _accounts_lock:
        _atomic_write_json(ACCOUNTS_FILE, accounts)

def load_settings() -> Dict[str, Any]:
    with _settings_lock:
        if not SETTINGS_FILE.exists():
            return {}
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))

def save_settings(settings: Dict[str, Any]) -> None:
    with _settings_lock:
        _atomic_write_json(SETTINGS_FILE, settings)
