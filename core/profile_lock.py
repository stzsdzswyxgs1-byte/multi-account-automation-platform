from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Optional, Tuple

LOCK_NAME = ".panel_lock"
LOCK_EXPIRE_SEC = 600  # 锁超过 10 分钟自动视为过期（防止崩溃后死锁）

def _now() -> int:
    return int(time.time())

def lock_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / LOCK_NAME

def try_acquire(profile_dir: Path, owner: str) -> Tuple[bool, str]:
    """创建一个轻量锁文件，避免同一 profile 被监控/批量同时占用。
    返回 (True,'') 表示拿到锁；(False, reason) 表示被占用。
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    lp = lock_path(profile_dir)
    payload = {"owner": owner, "ts": _now()}
    try:
        # O_EXCL: 文件存在就失败
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return True, ""
    except FileExistsError:
        # 读取锁信息（尽力）
        try:
            data = json.loads(lp.read_text(encoding="utf-8"))
            who = data.get("owner","?")
            ts = data.get("ts", 0)
            age = _now() - int(ts or 0)
            if age < 0:
                age = 0
            # 锁超时自动过期：防止程序崩溃后死锁
            if age > LOCK_EXPIRE_SEC:
                try:
                    lp.unlink()
                except Exception:
                    return False, f"Profile 锁已过期（{who}，{age}s）但清理失败"
                # 重新尝试获取
                try:
                    fd2 = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd2, "w", encoding="utf-8") as f2:
                        json.dump(payload, f2, ensure_ascii=False)
                    return True, ""
                except Exception:
                    return False, f"Profile 锁已过期但重新获取失败"
            return False, f"Profile 正在被占用（{who}，已 {age}s）"
        except Exception:
            return False, "Profile 正在被占用（锁文件存在）"
    except Exception as e:
        return False, f"创建锁失败：{e}"

def release(profile_dir: Path) -> None:
    lp = lock_path(Path(profile_dir))
    try:
        if lp.exists():
            lp.unlink()
    except Exception:
        pass

def force_clear(profile_dir: Path) -> Tuple[bool, str]:
    lp = lock_path(Path(profile_dir))
    if not lp.exists():
        return True, "无锁"
    try:
        lp.unlink()
        return True, "已清理"
    except Exception as e:
        return False, f"清理失败：{e}"


def force_clear_all(profile_dir: Path) -> str:
    """清理 .panel_lock 和 Chrome Singleton 文件，返回清理摘要。"""
    d = Path(profile_dir)
    cleared = []
    # 清理 panel_lock
    lp = lock_path(d)
    if lp.exists():
        try:
            lp.unlink()
            cleared.append(".panel_lock")
        except Exception:
            pass
    # 清理 Chrome Singleton 文件
    for base in (d, d / "Default"):
        for name in CHROME_SINGLETON_FILES:
            fp = base / name
            if fp.exists():
                try:
                    fp.unlink()
                    cleared.append(name)
                except Exception:
                    pass
    return ", ".join(cleared) if cleared else ""


def acquire_or_clear(profile_dir: Path, owner: str, log_fn=None) -> Tuple[bool, str]:
    """尝试获取锁，如果被占用则自动清理后重试一次。返回 (ok, reason)。"""
    # 第一次：检测 Chrome 占用
    in_use, reason = detect_chrome_profile_in_use(profile_dir)
    if not in_use:
        ok, reason = try_acquire(profile_dir, owner=owner)
        if ok:
            return True, ""

    # 被占用 → 自动清理
    cleared = force_clear_all(profile_dir)
    if cleared and log_fn:
        log_fn(f"自动清理占用状态: {cleared}")

    # 重试
    in_use2, reason2 = detect_chrome_profile_in_use(profile_dir)
    if in_use2:
        return False, reason2
    ok2, reason2 = try_acquire(profile_dir, owner=owner)
    return ok2, reason2

# --- Chrome/Chromium native profile-in-use detection (stability only) ---
# Chrome writes Singleton* files under the profile dir while running.
# We treat 'recently modified' as a signal to avoid false positives from old leftovers.
CHROME_SINGLETON_FILES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "SingletonPort",
)


def detect_chrome_profile_in_use(profile_dir: Path, recent_sec: int = 600) -> Tuple[bool, str]:
    # Returns (True, reason) if the profile looks like it is still used by a Chrome process.
    # Some Chrome builds may place Singleton* in the user-data-dir root, others under the default profile subdir.
    try:
        d = Path(profile_dir)
        now = time.time()
        hits = []
        for base in (d, d / "Default"):
            for name in CHROME_SINGLETON_FILES:
                fp = base / name
                if not fp.exists():
                    continue
                try:
                    age = now - fp.stat().st_mtime
                except Exception:
                    age = None
                # If we cannot read mtime, treat as 'in use' to be safe.
                if age is None or age <= float(recent_sec or 600):
                    if age is None:
                        hits.append(str(fp))
                    else:
                        hits.append(f"{fp}(age={int(age)}s)")
        if hits:
            return True, "Chrome 可能仍占用该 Profile（请先关闭该账号相关 Chrome 窗口后重试）：" + ", ".join(hits)
    except Exception:
        pass

    return False, ""
