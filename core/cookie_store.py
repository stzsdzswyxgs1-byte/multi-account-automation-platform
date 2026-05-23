"""集中式 Cookie 缓存 — 监控写入，其他功能读取，消除 profile 锁冲突。

架构：
  监控(Monitor) 每轮成功后 → save_cookie_cache() 写入 JSON
  批量上下架/删除/IM回复等 → load_cookie_cache() 读取 → 直接 HTTP POST（不需要浏览器、不需要锁）

文件位置：profiles/{account}/cookie_cache.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

# ── Cookie Cache 文件 ────────────────────────────────

CACHE_FILENAME = "cookie_cache.json"
# 默认最大有效期：24 小时（监控每 300 秒刷新；即使监控停了，Yahoo session 通常也能撑 24h+）
DEFAULT_MAX_AGE = 86400


def _cache_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / CACHE_FILENAME


def save_cookie_cache(
    profile_dir: Path,
    cookies: Dict[str, str],
    wssid: str = "",
    *,
    nickname: str = "",
    raw_cookies: list | None = None,
) -> bool:
    """保存 cookies + wssid 到 JSON 文件。

    由监控模块在每轮成功检查后调用。
    raw_cookies: Playwright context.cookies() 的完整列表（含 domain/path/expires），
                 供 add_cookies() 注入使用。
    返回 True 表示保存成功。
    """
    if not cookies:
        return False

    profile_dir = Path(profile_dir)
    fp = _cache_path(profile_dir)

    # 读取旧数据作为 backup，同时保留 raw_cookies / nickname
    backup_cookies = {}
    backup_wssid = ""
    old_raw_cookies = None
    old_nickname = ""
    try:
        if fp.exists():
            old = json.loads(fp.read_text(encoding="utf-8"))
            backup_cookies = old.get("cookies", {})
            backup_wssid = old.get("wssid", "")
            old_raw_cookies = old.get("raw_cookies")
            old_nickname = old.get("nickname", "")
    except Exception:
        pass

    data = {
        "cookies": cookies,
        "wssid": wssid,
        "nickname": nickname or old_nickname,
        "saved_at": time.time(),
        "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 上一次成功的 cookies 做备份
        "backup": {
            "cookies": backup_cookies,
            "wssid": backup_wssid,
        } if backup_cookies else {},
    }
    # Playwright 完整 cookie 列表（含 domain/path/expires）
    # 调用方未提供时保留旧值（避免 wssid 补写等局部更新丢失 raw_cookies）
    effective_raw = raw_cookies if raw_cookies is not None else old_raw_cookies
    if effective_raw:
        data["raw_cookies"] = effective_raw

    # ✅ 原子寫:先寫 .tmp 再 os.replace 重命名。中斷只會留下 tmp 檔,主檔不壞。
    # 避免「軟件突然關閉導致 cookies 文件半寫損壞 → cookies 徹底丟」這種情況。
    try:
        import os as _os
        tmp_fp = fp.with_suffix(fp.suffix + ".tmp")
        tmp_fp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _os.replace(str(tmp_fp), str(fp))
        return True
    except Exception:
        # 清理可能殘留的 tmp(下次 write 也會被覆蓋,不要緊)
        try:
            if tmp_fp.exists():
                tmp_fp.unlink()
        except Exception:
            pass
        return False


def load_cookie_cache(
    profile_dir: Path,
    max_age: float = DEFAULT_MAX_AGE,
) -> Tuple[Dict[str, str], str, float]:
    """从 JSON 文件加载 cookies + wssid。

    返回 (cookies, wssid, saved_at)。
    如果文件不存在、已过期或无效，返回空 ({}, "", 0.0)。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return {}, "", 0.0

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}, "", 0.0

    saved_at = data.get("saved_at", 0.0)
    age = time.time() - saved_at

    # 检查是否过期
    if age > max_age:
        return {}, "", 0.0

    cookies = data.get("cookies", {})
    wssid = data.get("wssid", "")

    if not cookies:
        return {}, "", 0.0

    return cookies, wssid, saved_at


def load_cookie_cache_with_backup(
    profile_dir: Path,
    max_age: float = DEFAULT_MAX_AGE,
) -> Tuple[Dict[str, str], str, float]:
    """先尝试加载主 cache，失败则尝试 backup。"""
    cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age)
    if cookies:
        return cookies, wssid, saved_at

    # 尝试 backup
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return {}, "", 0.0

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        backup = data.get("backup", {})
        if not backup:
            return {}, "", 0.0

        b_cookies = backup.get("cookies", {})
        b_wssid = backup.get("wssid", "")
        if b_cookies:
            return b_cookies, b_wssid, 0.0  # saved_at=0 表示来自 backup
    except Exception:
        pass

    return {}, "", 0.0


def invalidate_cookie_cache(profile_dir: Path) -> None:
    """标记 cookie cache 为已失效（将 saved_at 设为 0）。

    当 API 返回 auth 错误时调用，强制下次使用浏览器提取。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        data["saved_at"] = 0
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_cache_age(profile_dir: Path) -> Optional[float]:
    """返回 cookie cache 的年龄（秒），不存在返回 None。"""
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at", 0.0)
        if saved_at <= 0:
            return None
        return time.time() - saved_at
    except Exception:
        return None


def load_raw_cookies(
    profile_dir: Path,
    max_age: float = DEFAULT_MAX_AGE,
) -> list:
    """加载 Playwright 完整 cookie 列表（含 domain/path/expires）。

    供 context.add_cookies() 直接注入使用。
    返回空列表表示不可用。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return []

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return []

    saved_at = data.get("saved_at", 0.0)
    if saved_at <= 0 or (time.time() - saved_at) > max_age:
        return []

    raw = data.get("raw_cookies")
    if isinstance(raw, list) and raw:
        return raw
    return []


def load_from_chrome_sqlite_yahoo(profile_dir: Path) -> Tuple[Dict[str, str], list]:
    """v6.1:cache 過期/不存在時的 fallback — 直接從 Chrome SQLite 解密讀 Yahoo cookies。

    不依賴 cache mtime,不開 Playwright。只解密 Chrome SQLite Cookies DB(複用 goofish_cookie_store 的 AES key + decrypt 邏輯)。

    Yahoo cookie 有效期 1 年,即使 cache 過期 24 小時,Chrome SQLite 內的 cookie 通常還有效。
    用這個 fallback 可以避免「cache 過期 → 等 user handoff」的死鎖。

    Returns:
        (cookies_dict, raw_cookies_list)
        cookies_dict: {name: value} flat dict
        raw_cookies_list: [{name, value, domain, path, secure, httpOnly}, ...]
        失敗時返 ({}, [])
    """
    import shutil
    import sqlite3
    import tempfile
    import os

    profile_dir = Path(profile_dir)
    db_path = profile_dir / "Default" / "Network" / "Cookies"
    if not db_path.exists():
        db_path = profile_dir / "Default" / "Cookies"
    if not db_path.exists():
        return {}, []

    try:
        from .goofish_cookie_store import _get_chrome_aes_key, _decrypt_cookie_value
    except Exception:
        return {}, []

    aes_key = _get_chrome_aes_key(profile_dir)
    if not aes_key:
        return {}, []

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return {}, []

    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        shutil.copy2(str(db_path), tmp_path)

        conn = sqlite3.connect(tmp_path)
        rows = conn.execute(
            "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly, expires_utc "
            "FROM cookies "
            "WHERE host_key LIKE '%yahoo%'"
        ).fetchall()
        conn.close()
    except Exception:
        return {}, []
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    if not rows:
        return {}, []

    aesgcm = AESGCM(aes_key)
    flat_cookies: Dict[str, str] = {}
    raw_list: list = []
    # 過濾過期 cookie:expires_utc 是 webkit timestamp (1601-01-01 epoch, microseconds)
    # 0 表示 session cookie(不過期 / 看 browser session,我們當有效)
    # 轉 unix: (expires_utc / 1_000_000) - 11644473600
    now_unix = time.time()
    skipped_expired = 0
    for row in rows:
        host, name, enc_val, path, is_secure, is_httponly, exp_us = row
        # 過濾過期 cookie
        if exp_us and exp_us > 0:
            exp_unix = (exp_us / 1_000_000) - 11644473600
            if exp_unix < now_unix:
                skipped_expired += 1
                continue
        try:
            val = _decrypt_cookie_value(enc_val, aesgcm)
            if val:
                flat_cookies[name] = val
                raw_list.append({
                    "name": name,
                    "value": val,
                    "domain": host,
                    "path": path or "/",
                    "secure": bool(is_secure),
                    "httpOnly": bool(is_httponly),
                })
        except Exception:
            pass

    return flat_cookies, raw_list


def load_cookie_cache_with_sqlite_fallback(
    profile_dir: Path,
    max_age: float = DEFAULT_MAX_AGE,
    on_log=None,
) -> Tuple[Dict[str, str], str, float]:
    """v6.1:cache 過期時自動 fallback 從 Chrome SQLite 強讀。

    流程:
    1. 先試 load_cookie_cache (走原本 24h max_age 邏輯)
    2. cache 過期/不存在 → 從 Chrome SQLite 解密讀 Yahoo cookies
    3. 拿到 cookies 後 save_cookie_cache 寫回(更新 saved_at,wssid 空)
    4. 返 (cookies, wssid, saved_at)

    這修了「cache 24h 過期 + Chrome SQLite mtime 沒變 → 死鎖等 handoff」的核心 bug。
    Yahoo cookie 有效期 1 年,SQLite 內幾乎一定還有效。

    Returns: (cookies, wssid, saved_at) — 失敗時返 ({}, "", 0)
    """
    cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
    if cookies:
        return cookies, wssid, saved_at

    # cache miss → 嘗試從 Chrome SQLite 強讀
    flat, raw = load_from_chrome_sqlite_yahoo(profile_dir)
    if not flat:
        return {}, "", 0.0

    # 寫回 cache(wssid 空,讓上層自己 HTTP 補提取)
    try:
        save_cookie_cache(profile_dir, flat, "", raw_cookies=raw)
        if on_log:
            on_log(
                f"[COOKIE-STORE] {profile_dir.name} cache miss → "
                f"Chrome SQLite fallback 成功({len(flat)} cookies)"
            )
    except Exception:
        pass

    return flat, "", time.time()


async def migrate_raw_cookies_for_all(
    profiles_dir: Path,
    profile_ids: list[str],
    chrome_path: str,
    on_log=None,
) -> int:
    """一次性迁移：对所有缺少 raw_cookies 的账号，快速打开 Chrome 提取完整 cookie。

    只在有 cookie_cache.json 但缺少 raw_cookies 时执行（说明是旧格式）。
    全新账号（无 cache 文件）跳过。

    返回成功迁移的账号数。
    """
    import asyncio

    profiles_dir = Path(profiles_dir)
    need_migrate = []
    for pid in profile_ids:
        pdir = profiles_dir / pid
        if not pdir.is_dir():
            continue
        fp = pdir / CACHE_FILENAME
        if fp.exists():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                raw = data.get("raw_cookies")
                if isinstance(raw, list) and raw:
                    continue  # 已有 raw_cookies，跳过
            except Exception:
                pass
        # 检查 Chrome profile 是否存在（有 cookie 数据库）
        has_chrome = (pdir / "Default" / "Network" / "Cookies").exists() or \
                     (pdir / "Default" / "Cookies").exists()
        if has_chrome:
            need_migrate.append(pid)

    if not need_migrate:
        return 0

    _log = on_log or (lambda msg: None)
    _log(f"[Cookie迁移] 发现 {len(need_migrate)} 个账号需要迁移 raw_cookies: {', '.join(need_migrate[:10])}")

    try:
        from .client_runtime_compat import async_playwright, get_launch_args, get_ignore_default_args, apply_runtime_normalization_async
    except ImportError:
        from playwright.async_api import async_playwright
        get_launch_args = lambda **kw: []
        get_ignore_default_args = lambda **kw: []
        apply_runtime_normalization_async = None

    migrated = 0
    async with async_playwright() as p:
        for pid in need_migrate:
            pdir = profiles_dir / pid
            _log(f"[Cookie迁移] {pid}: 正在提取...")

            # 清理残留锁文件（防止上次崩溃遗留的 SingletonLock 阻止启动）
            for _lf in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                _lp = pdir / _lf
                try:
                    if _lp.exists():
                        _lp.unlink()
                except Exception:
                    pass

            ctx = None
            _ok = False
            # 与监控完全一致的启动方式：
            #   Playwright headless=False + Chrome --headless=new（通过 get_launch_args）
            # 如果失败，fallback 到真正的有头模式（窗口放屏幕外）
            for _use_chrome_headless in (True, False):
                if _ok:
                    break
                ctx = None
                try:
                    _args = get_launch_args(headless=_use_chrome_headless, lang="zh-TW")
                    if _use_chrome_headless:
                        _args.append("--window-size=800,600")
                    else:
                        _args.append("--window-size=800,600")
                        _args.append("--window-position=-3000,-3000")
                    _ignore = get_ignore_default_args(headless=_use_chrome_headless)
                    _kw = dict(
                        user_data_dir=str(pdir),
                        executable_path=chrome_path,
                        headless=False,          # 关键：始终 False（与监控一致）
                        no_viewport=True,        # 关键：与监控一致
                        args=_args,
                        ignore_default_args=_ignore,
                    )
                    ctx = await asyncio.wait_for(
                        p.chromium.launch_persistent_context(**_kw),
                        timeout=30,
                    )
                    raw_cookies = await ctx.cookies()
                    if raw_cookies:
                        yahoo_cookies = {
                            c["name"]: c["value"]
                            for c in raw_cookies
                            if "yahoo" in c.get("domain", "").lower()
                        }
                        if yahoo_cookies:
                            save_cookie_cache(
                                pdir, yahoo_cookies, "",
                                nickname=pid,
                                raw_cookies=raw_cookies,
                            )
                            migrated += 1
                            _ok = True
                            _hmode = "headless=new" if _use_chrome_headless else "headed"
                            _log(f"[Cookie迁移] {pid}: 成功 ({len(raw_cookies)} cookies, {_hmode})")
                        else:
                            _log(f"[Cookie迁移] {pid}: 无 Yahoo cookie")
                            _ok = True
                    else:
                        _log(f"[Cookie迁移] {pid}: 浏览器返回空 cookie")
                        _ok = True
                except Exception as e:
                    _emsg = str(e)[:80]
                    if _use_chrome_headless:
                        _log(f"[Cookie迁移] {pid}: headless=new 失败({_emsg})，尝试有头模式...")
                    else:
                        _log(f"[Cookie迁移] {pid}: 失败 - {_emsg}")
                finally:
                    if ctx:
                        try:
                            await ctx.close()
                        except Exception:
                            pass

    _log(f"[Cookie迁移] 完成，成功 {migrated}/{len(need_migrate)}")
    return migrated


# ── v6.1.57:cookie diff log 診斷異常帳號 ───────────────────────────


def dump_cookie_diff_for_abnormal(
    abnormal_profile_dir: Path,
    healthy_profile_dirs: list,
    *,
    base_dir: Optional[Path] = None,
    on_log=None,
    error_context: str = "",
) -> Optional[Path]:
    """v6.1.57:當帳號變「異常」時 dump cookies + 對比一個「在線」帳號,找污染源。

    寫到 runtime/abnormal_cookie_diff/{account}_{ts}.txt(同帳號最多保留最近 5 份)。

    輸出內容:
      - 異常帳號獨有的 cookies(可能是污染源)
      - 正常帳號獨有的 cookies(可能是缺失關鍵)
      - 共同名稱但值不同的 cookies(可能是被污染的版本)

    Args:
      abnormal_profile_dir: 異常帳號的 profile 目錄
      healthy_profile_dirs: 在線帳號 profile 目錄 list(挑第一個能讀到 cookies 的當對照)
      base_dir: runtime 目錄根,None 用 abnormal_profile_dir.parent.parent
      error_context: 觸發原因(寫進 dump 開頭)

    Returns:
      dump 檔路徑(成功),None(失敗)
    """
    def _log(m):
        if on_log:
            try:
                on_log(m)
            except Exception:
                pass

    try:
        abnormal_profile_dir = Path(abnormal_profile_dir)
        if base_dir is None:
            base_dir = abnormal_profile_dir.parent.parent  # XDZHGL2.0
        out_dir = Path(base_dir) / "runtime" / "abnormal_cookie_diff"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. 讀異常帳號 cookies(優先用 cache,失敗用 SQLite)
        ab_cookies, ab_wssid, _ = load_cookie_cache(abnormal_profile_dir, max_age=86400 * 7)
        if not ab_cookies:
            try:
                ab_cookies, _ = load_from_chrome_sqlite_yahoo(abnormal_profile_dir)
            except Exception:
                ab_cookies = {}
        if not ab_cookies:
            _log(f"[COOKIE-DIFF] {abnormal_profile_dir.name} 無 cookies,放棄 dump")
            return None

        # 2. 找一個健康對照組 cookies
        healthy_cookies: Dict[str, str] = {}
        healthy_name = ""
        for hp in healthy_profile_dirs:
            hp = Path(hp)
            if hp == abnormal_profile_dir:
                continue
            try:
                hc, _, _ = load_cookie_cache(hp, max_age=86400 * 7)
                if not hc:
                    hc, _ = load_from_chrome_sqlite_yahoo(hp)
                if hc and len(hc) >= 5:
                    healthy_cookies = hc
                    healthy_name = hp.name
                    break
            except Exception:
                continue

        # 3. 計算 diff
        ab_only = {k: v for k, v in ab_cookies.items() if k not in healthy_cookies}
        healthy_only = {k: v for k, v in healthy_cookies.items() if k not in ab_cookies}
        common_diff = {
            k: (ab_cookies[k], healthy_cookies[k])
            for k in set(ab_cookies) & set(healthy_cookies)
            if ab_cookies[k] != healthy_cookies[k]
        }

        # 4. 寫 dump
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_fp = out_dir / f"{abnormal_profile_dir.name}_{ts}.txt"
        lines = [
            f"=== 異常帳號 cookies diff dump (v6.1.57) ===",
            f"異常帳號: {abnormal_profile_dir.name}",
            f"  cookies 總數: {len(ab_cookies)}",
            f"  wssid: {ab_wssid[:8]}..." if ab_wssid else "  wssid: (空)",
            f"健康對照: {healthy_name or '(找不到健康對照組)'}",
            f"  cookies 總數: {len(healthy_cookies)}",
            f"觸發原因: {error_context}",
            f"dump 時間: {ts}",
            "",
            f"--- 異常獨有 cookies ({len(ab_only)} 個 — 最可疑,污染源候選) ---",
        ]
        for k in sorted(ab_only.keys()):
            v = str(ab_only[k])
            lines.append(f"  {k:<28} = {v[:80]}{'...' if len(v) > 80 else ''}")
        lines.extend([
            "",
            f"--- 健康獨有(異常缺失)cookies ({len(healthy_only)} 個 — 可能必要 auth 被吃掉) ---",
        ])
        for k in sorted(healthy_only.keys()):
            v = str(healthy_only[k])
            lines.append(f"  {k:<28} = {v[:80]}{'...' if len(v) > 80 else ''}")
        lines.extend([
            "",
            f"--- 共同名稱但值不同({len(common_diff)} 個 — 被改寫的 cookie) ---",
        ])
        for k in sorted(common_diff.keys()):
            ab_v, h_v = common_diff[k]
            lines.append(f"  {k}:")
            lines.append(f"    異常 = {str(ab_v)[:120]}")
            lines.append(f"    健康 = {str(h_v)[:120]}")
        lines.extend([
            "",
            "=== 分析建議 ===",
            "1. 異常獨有 cookies 內常見污染源:`B`/`AS`/`PRS`/`F` 等動態 cookies 值異常,",
            "   或 server 動態加的 risk-tracking cookies(`_*`、`X-*` 開頭等)",
            "2. 健康獨有 cookies 內缺失的 auth cookies(`B`、`Y`、`T`)→ 重登才能補",
            "3. 共同名稱但值不同 → 看是不是 server 把該值 rewrite 成 risk 標記",
            "4. 把這個 dump 給 Claude 分析,可以決定要 whitelist 哪些 cookies 寫回",
        ])

        out_fp.write_text("\n".join(lines), encoding="utf-8")

        # 5. 清舊 dump(同帳號保留最近 5 份)
        try:
            same_acc_files = sorted(
                out_dir.glob(f"{abnormal_profile_dir.name}_*.txt"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            for old in same_acc_files[5:]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

        _log(
            f"[COOKIE-DIFF] {abnormal_profile_dir.name} dump 已寫入 → {out_fp.name} "
            f"(獨有 {len(ab_only)} / 缺失 {len(healthy_only)} / 變值 {len(common_diff)})"
        )
        return out_fp
    except Exception as e:
        _log(f"[COOKIE-DIFF] dump 異常: {e}")
        return None
