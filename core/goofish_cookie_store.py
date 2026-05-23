"""闲鱼 Cookie 缓存 — 从 purchase_monitor 浏览器 Profile 提取并管理闲鱼 cookie。

架构:
  login_browser / scrape_xianyu (Playwright) 成功后 → save_goofish_cookies()
  HTTP order fetch → load_goofish_cookies() → 获得可用 session

文件位置: profiles/purchase_monitor/goofish_cookie_cache.json

与 Yahoo 的 cookie_store.py 独立:
  - Yahoo cookie 在 .yahoo.com 域名
  - 闲鱼 cookie 在 .goofish.com / .taobao.com 域名
  - 两者不互相干扰
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

GOOFISH_CACHE_FILE = "goofish_cookie_cache.json"

# token 有效期: 2 小时 (闲鱼 _m_h5_tk 的实际有效期)
GOOFISH_TOKEN_MAX_AGE = 7200

# cookie session 最大有效期: 24 小时 (cookie 本身可能更长，但保守设置)
GOOFISH_SESSION_MAX_AGE = 86400

# 需要保留的 cookie 域名
GOOFISH_DOMAINS = (".goofish.com", ".taobao.com", ".mmstat.com", ".tbcdn.cn",
                   "goofish.com", "taobao.com")


# ── 工具函数 ──────────────────────────────────────────

def _cache_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / GOOFISH_CACHE_FILE


def _extract_token(m_h5_tk_value: str) -> str:
    """从 _m_h5_tk cookie 值中提取签名用 token。

    '1780e697...fc0f3698_1773051288539' -> '1780e697...fc0f3698'
    """
    if not m_h5_tk_value:
        return ""
    return m_h5_tk_value.split("_")[0]


def _is_goofish_domain(domain: str) -> bool:
    """判断 cookie 域名是否属于闲鱼/淘宝。"""
    return any(d in domain for d in GOOFISH_DOMAINS)


# ── 保存 ──────────────────────────────────────────────

def save_goofish_cookies(
    profile_dir: Path,
    raw_cookies: List[dict],
    *,
    account_hint: str = "",
) -> bool:
    """从 Playwright ctx.cookies() 提取闲鱼 cookie 并保存到缓存文件。

    Args:
        profile_dir: profile 目录 (e.g. profiles/purchase_monitor/)
        raw_cookies: Playwright 格式的完整 cookie 列表
        account_hint: 账号标识 (用于日志)

    Returns:
        True 表示保存成功。
    """
    if not raw_cookies:
        return False

    profile_dir = Path(profile_dir)
    fp = _cache_path(profile_dir)

    # 过滤闲鱼/淘宝域名的 cookie
    gf_cookies = [c for c in raw_cookies if _is_goofish_domain(c.get("domain", ""))]
    if not gf_cookies:
        log.warning("[goofish_cookie] 未找到闲鱼/淘宝域名的 cookie")
        return False

    # 构建 {name: value} 扁平字典 (用于 HTTP 请求)
    cookie_dict: Dict[str, str] = {}
    m_h5_tk = ""
    m_h5_tk_enc = ""

    for c in gf_cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if not name:
            continue
        cookie_dict[name] = value
        if name == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
            m_h5_tk = value
        elif name == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
            m_h5_tk_enc = value

    token_hex = _extract_token(m_h5_tk)

    data = {
        "cookies": cookie_dict,
        "m_h5_tk": m_h5_tk,
        "m_h5_tk_enc": m_h5_tk_enc,
        "token_hex": token_hex,
        "account_hint": account_hint,
        "saved_at": time.time(),
        "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookie_count": len(gf_cookies),
        # 保留 Playwright 格式 raw cookies (用于浏览器注入)
        "raw_cookies": gf_cookies,
    }

    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[goofish_cookie] 保存成功: %d 条 cookie, token=%s...",
                 len(gf_cookies), token_hex[:12] if token_hex else "无")
        return True
    except Exception as e:
        log.error("[goofish_cookie] 保存失败: %s", e)
        return False


# ── 加载 ──────────────────────────────────────────────

def load_goofish_cookies(
    profile_dir: Path,
    max_age: float = GOOFISH_SESSION_MAX_AGE,
    _auto_sync: bool = True,
) -> Tuple[Dict[str, str], str, float]:
    """从缓存文件加载闲鱼 cookie。

    v6.0.78:cache 過期/空時自動從 Chrome SQLite 同步重抓
    (用戶看瀏覽器中閒魚有登錄但程式報需登錄 — 就是這個 24h cache 過期問題)

    Args:
        profile_dir: profile 目录
        max_age: 最大有效期 (秒)
        _auto_sync: 內部參數,True 時 cache 空會嘗試從 SQLite 重抓(防無限遞迴用)

    Returns:
        (cookie_dict, token_hex, saved_at)
        cookie_dict: {name: value} 扁平字典
        token_hex: 签名用 token (从 _m_h5_tk 提取)
        saved_at: 保存时间戳

        全部为空值 ({}, "", 0.0) 表示不可用。
    """
    fp = _cache_path(Path(profile_dir))

    # 內部讀檔邏輯
    def _read_once() -> Tuple[Dict[str, str], str, float, bool]:
        """回 (cookies, token, saved_at, expired_or_empty)"""
        if not fp.exists():
            return {}, "", 0.0, True
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return {}, "", 0.0, True
        saved_at = data.get("saved_at", 0.0)
        if saved_at <= 0:
            return {}, "", 0.0, True
        age = time.time() - saved_at
        if age > max_age:
            log.info("[goofish_cookie] 缓存已过期 (age=%.0fh)", age / 3600)
            return {}, "", 0.0, True
        cookies = data.get("cookies", {})
        token_hex = data.get("token_hex", "")
        if not cookies:
            return {}, "", 0.0, True
        return cookies, token_hex, saved_at, False

    cookies, token_hex, saved_at, expired = _read_once()

    # v6.0.78:cache 空/過期時自動從 Chrome SQLite 同步重抓
    # 前提:Chrome 採購監控 profile 不在運行(SQLite 文件不被鎖)
    if expired and _auto_sync:
        log.info("[goofish_cookie] cache 空/過期 → 嘗試從 Chrome SQLite 自動同步重抓...")
        try:
            ok = extract_cookies_from_profile(profile_dir, force=True)
            if ok:
                # 重讀,但這次禁用 auto_sync 防無限遞迴
                cookies, token_hex, saved_at, _ = _read_once()
                if cookies and token_hex:
                    log.info("[goofish_cookie] ✓ SQLite 同步成功,cookie 恢復")
                else:
                    log.warning("[goofish_cookie] SQLite 同步後 cache 仍空(SQLite 也無有效 cookie)")
            else:
                log.warning("[goofish_cookie] SQLite 同步失敗(Chrome 可能在運行 / 未登錄)")
        except Exception as e:
            log.warning("[goofish_cookie] SQLite 自動同步異常: %s", e)

    return cookies, token_hex, saved_at


def load_goofish_raw_cookies(
    profile_dir: Path,
    max_age: float = GOOFISH_SESSION_MAX_AGE,
) -> List[dict]:
    """加载 Playwright 格式的完整 cookie 列表 (用于浏览器注入)。"""
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

    return data.get("raw_cookies", [])


# ── 更新 token ────────────────────────────────────────

def update_goofish_token(
    profile_dir: Path,
    new_m_h5_tk: str,
    new_m_h5_tk_enc: str = "",
) -> bool:
    """更新缓存中的 _m_h5_tk token (HTTP 刷新后回写)。

    保留其他 cookie 不变，只更新 token 相关字段和 saved_at。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return False

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return False

    cookies = data.get("cookies", {})
    if not cookies:
        return False

    # 更新 token 字段
    data["m_h5_tk"] = new_m_h5_tk
    data["token_hex"] = _extract_token(new_m_h5_tk)
    data["saved_at"] = time.time()
    data["saved_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 更新 cookies dict 中的对应值
    cookies["_m_h5_tk"] = new_m_h5_tk
    if new_m_h5_tk_enc:
        data["m_h5_tk_enc"] = new_m_h5_tk_enc
        cookies["_m_h5_tk_enc"] = new_m_h5_tk_enc
    data["cookies"] = cookies

    # 更新 raw_cookies 中的对应值
    for c in data.get("raw_cookies", []):
        if c.get("name") == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
            c["value"] = new_m_h5_tk
        if new_m_h5_tk_enc and c.get("name") == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
            c["value"] = new_m_h5_tk_enc

    try:
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# ── 失效 ──────────────────────────────────────────────

def invalidate_goofish_cookies(profile_dir: Path) -> None:
    """标记闲鱼 cookie 缓存失效 (设 saved_at=0)。

    ⚠ 关键保护：Playwright 写入的 cache 包含完整 session cookie set（含 cookie2/_m_h5_tk 等
    Chrome SQLite 不持久化的关键 cookie），如果 invalidate 后被 SQLite 重读覆盖，
    cookie 集会变残缺导致用户「监控失败 → cookie 丢失 → 浏览器变未登录」。
    Playwright 写入的 cache 只能由用户重新扫码登录来更新，不能由 SQLite 重读触发。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        hint = (data.get("account_hint") or "").lower()
        if "playwright" in hint:
            log.info("[goofish_cookie] cache 由 Playwright 写入（含完整 session cookie），"
                     "skip invalidate（避免被 SQLite 残缺数据覆盖）")
            return
        data["saved_at"] = 0
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── 从 Chrome Profile 直接读取 cookie（不开浏览器） ───

# Chrome 新版 (v130+) 解密后 cookie 值前有 32 字节校验头，实际值从 offset 32 开始
_CHROME_VALUE_HEADER_LEN = 32


def _get_chrome_aes_key(profile_dir: Path) -> Optional[bytes]:
    """从 Local State 读取 Chrome 加密密钥并用 DPAPI 解密。

    仅 Windows 可用。返回 32 字节 AES-256 密钥。
    """
    import base64
    import ctypes
    import ctypes.wintypes

    local_state = profile_dir / "Local State"
    if not local_state.exists():
        return None

    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        enc_key_b64 = data.get("os_crypt", {}).get("encrypted_key", "")
        if not enc_key_b64:
            return None
    except Exception:
        return None

    enc_key = base64.b64decode(enc_key_b64)
    if enc_key[:5] != b"DPAPI":
        return None
    enc_key = enc_key[5:]

    # Windows DPAPI 解密
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    blob_in = DATA_BLOB(
        len(enc_key),
        ctypes.cast(
            ctypes.create_string_buffer(enc_key),
            ctypes.POINTER(ctypes.c_char),
        ),
    )
    blob_out = DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        return None

    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result if len(result) == 32 else None


def _decrypt_cookie_value(enc_val: bytes, aesgcm) -> str:
    """解密单个 Chrome cookie 值。"""
    if not enc_val or len(enc_val) < 16:
        return ""
    prefix = enc_val[:3]
    if prefix in (b"v10", b"v11"):
        nonce = enc_val[3:15]
        ct = enc_val[15:]
        raw = aesgcm.decrypt(nonce, ct, None)
        # Chrome 新版: 前 32 字节是校验头，实际 ASCII 值从 offset 32 开始
        if len(raw) > _CHROME_VALUE_HEADER_LEN:
            return raw[_CHROME_VALUE_HEADER_LEN:].decode("utf-8", errors="replace")
        return raw.decode("utf-8", errors="replace")
    return ""


def _read_cookies_from_sqlite(profile_dir: Path) -> Dict[str, str]:
    """直接从 Chrome Cookies SQLite 数据库读取闲鱼/淘宝 cookie。

    不需要打开浏览器。
    返回: {name: value} 字典, 空字典表示失败。
    """
    import shutil
    import sqlite3
    import tempfile

    profile_dir = Path(profile_dir)

    # 找到 Cookies DB 文件
    db_path = profile_dir / "Default" / "Network" / "Cookies"
    if not db_path.exists():
        db_path = profile_dir / "Default" / "Cookies"
    if not db_path.exists():
        log.warning("[goofish_cookie] Cookies 数据库不存在: %s", profile_dir)
        return {}

    # 获取 AES 密钥
    aes_key = _get_chrome_aes_key(profile_dir)
    if not aes_key:
        log.warning("[goofish_cookie] 无法获取 Chrome 加密密钥")
        return {}

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        log.error("[goofish_cookie] cryptography 库未安装, 无法解密 cookie")
        return {}

    # 复制 DB 文件（避免 Chrome 锁冲突）
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        import os
        os.close(tmp_fd)
        shutil.copy2(str(db_path), tmp_path)

        conn = sqlite3.connect(tmp_path)
        # 读取完整的 cookie 字段（包含 path/secure/is_httponly）
        rows = conn.execute(
            "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly "
            "FROM cookies "
            "WHERE host_key LIKE '%goofish%' OR host_key LIKE '%taobao%' OR host_key LIKE '%mmstat%'"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.error("[goofish_cookie] 读取 Cookies DB 失败: %s", e)
        return {}
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    if not rows:
        log.warning("[goofish_cookie] Cookies DB 中无闲鱼/淘宝 cookie (未登录?)")
        return {}

    # 解密
    aesgcm = AESGCM(aes_key)
    cookie_dict: Dict[str, str] = {}
    raw_list: List[dict] = []
    ok_count = 0

    skipped_punish = 0
    # punish 标记 cookie 名单：闲鱼限流时设置，带上会立刻继续 punish
    _PUNISH_NAMES = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}
    for row in rows:
        host = row[0]
        name = row[1]
        enc_val = row[2]
        path = row[3] if len(row) > 3 else "/"
        is_secure = bool(row[4]) if len(row) > 4 else False
        is_httponly = bool(row[5]) if len(row) > 5 else False
        # 跳过 punish 标记 cookie（路径包含 _____tmd_____ 或名字在黑名单）
        if "_____tmd_____" in path or "punish" in path.lower():
            skipped_punish += 1
            continue
        if name in _PUNISH_NAMES:
            skipped_punish += 1
            continue
        try:
            val = _decrypt_cookie_value(enc_val, aesgcm)
            if val:
                cookie_dict[name] = val
                # 同时保存 raw 格式（保留 domain/path/secure 信息）
                raw_list.append({
                    "name": name,
                    "value": val,
                    "domain": host,
                    "path": path,
                    "secure": is_secure,
                    "httpOnly": is_httponly,
                })
                ok_count += 1
        except Exception:
            pass

    if skipped_punish > 0:
        log.warning("[goofish_cookie] SQLite 跳过 %d 条 punish 标记 cookie", skipped_punish)

    log.info("[goofish_cookie] SQLite 直读: %d/%d cookie 解密成功", ok_count, len(rows))
    # 把 raw 格式作为字典的特殊键（不破坏现有调用方接口）
    cookie_dict["__raw__"] = raw_list
    return cookie_dict


def extract_cookies_from_profile(
    profile_dir: Path,
    chrome_path: str = "",
    force: bool = False,
) -> bool:
    """从 Chrome Profile 的 SQLite 数据库直接提取闲鱼 cookie（不开浏览器）。

    流程: 读 Local State 获取 AES 密钥 → 读 Cookies SQLite → 解密 → 保存缓存

    Args:
        force: True 时跳过缓存检查，强制从 SQLite 重新读取（用于换帐号场景）
    """
    profile_dir = Path(profile_dir)

    # ★ 优先：Playwright 写入的 cache 永远不被 SQLite 覆盖（除非 force=True 或换账号）
    # SQLite 不持久化 session cookies，覆盖只会让 cookie 残缺。
    # 监控周期 6h，旧版限制 1h 会让多数监控触发覆盖 → cookie 被毁。
    fp = _cache_path(profile_dir)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            hint = data.get("account_hint", "") or ""
            if not force and "playwright" in hint:
                saved_at = data.get("saved_at", 0.0)
                age = time.time() - saved_at
                log.info("[goofish_cookie] cache 由 Playwright 写入 (age=%.1fh), 跳过 SQLite 覆盖",
                         age / 3600)
                return True
        except Exception:
            pass

    # ── 1. 先读 SQLite 拿到当前 unb（用户ID）──
    sqlite_cookies = _read_cookies_from_sqlite(profile_dir)
    sqlite_raw = sqlite_cookies.pop("__raw__", []) if sqlite_cookies else []
    sqlite_unb = sqlite_cookies.get("unb", "") if sqlite_cookies else ""

    # ── 2. 检查现有缓存 ──
    # v6.0.78:禁用 _auto_sync 防無限遞迴(我們此刻就是在做同步)
    existing_cookies, existing_token, existing_ts = load_goofish_cookies(profile_dir, _auto_sync=False)
    if not force and existing_cookies and existing_token:
        existing_unb = existing_cookies.get("unb", "")
        # 关键：对比 unb 检测是否换了帐号
        if sqlite_unb and existing_unb and sqlite_unb == existing_unb:
            _age = time.time() - existing_ts
            log.info("[goofish_cookie] 缓存仍有效 (unb=%s, age=%.1fh), 跳过提取",
                     sqlite_unb[:8], _age / 3600)
            return True
        elif sqlite_unb and existing_unb and sqlite_unb != existing_unb:
            log.warning("[goofish_cookie] 检测到帐号变更! 旧unb=%s 新unb=%s, 强制刷新",
                        existing_unb[:8], sqlite_unb[:8])
            # 继续往下走，强制重新提取

    # ── 3. SQLite 读取失败 ──
    cookie_dict = sqlite_cookies
    if not cookie_dict:
        return False

    # ── 4. 提取关键字段并保存缓存 ──
    m_h5_tk = cookie_dict.get("_m_h5_tk", "")
    m_h5_tk_enc = cookie_dict.get("_m_h5_tk_enc", "")
    token_hex = _extract_token(m_h5_tk)

    if not m_h5_tk:
        log.warning("[goofish_cookie] 解密的 cookie 中缺少 _m_h5_tk (闲鱼未登录?)")
        return False

    # 详细诊断：列出关键 cookie 状态（方便定位个别用户登录问题）
    _diag_keys = ["unb", "cookie2", "_tb_token_", "_m_h5_tk", "sgcookie", "tracknick", "havana_lgc2_77"]
    _diag = ", ".join(f"{k}={'有' if cookie_dict.get(k) else '无'}" for k in _diag_keys)
    log.info("[goofish_cookie] [诊断] SQLite 共%d条: %s", len(cookie_dict), _diag)

    # 关键：检查登录态 cookie。unb 是用户ID（持久 cookie），代表已登录
    # 注意：cookie2/_tb_token_ 是 session cookie，Chrome 退出会清除，所以不能用作判断标准
    if not sqlite_unb:
        log.warning("[goofish_cookie] cookie 缺少 unb (用户ID) - 闲鱼未登录或 Chrome 未保存登录态")
        return False

    # 检查 _m_h5_tk 中的时间戳年龄（仅日志提示，不阻断）
    try:
        parts = m_h5_tk.split("_")
        if len(parts) >= 2:
            tk_ts = int(parts[-1]) / 1000.0  # ms → s
            tk_age = time.time() - tk_ts
            if tk_age > GOOFISH_SESSION_MAX_AGE:
                log.warning("[goofish_cookie] SQLite 中 _m_h5_tk 较旧 (%.0fh)，可能需要刷新", tk_age / 3600)
    except (ValueError, IndexError):
        pass

    data = {
        "cookies": cookie_dict,
        "m_h5_tk": m_h5_tk,
        "m_h5_tk_enc": m_h5_tk_enc,
        "token_hex": token_hex,
        "account_hint": f"sqlite_extract|unb={sqlite_unb[:12]}",
        "saved_at": time.time(),
        "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookie_count": len(cookie_dict),
        "raw_cookies": sqlite_raw,  # 保存 raw 格式（含 domain/path/secure）
    }

    fp = _cache_path(profile_dir)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[goofish_cookie] SQLite 提取成功: %d 条 cookie, token=%s...",
                 len(cookie_dict), token_hex[:12] if token_hex else "无")
        return True
    except Exception as e:
        log.error("[goofish_cookie] 保存缓存失败: %s", e)
        return False


# ── 后台无头刷新 session（保持 cookie 不过期） ────────

def refresh_goofish_session(
    profile_dir: Path,
    chrome_path: str = "",
) -> bool:
    """后台无头打开 Playwright 访问闲鱼首页，刷新服务端 session，提取最新 cookie。

    目的: 防止闲鱼 session cookie 过期 (服务端约 24h 失效)。
    每次监控循环 (6h) 前调用一次即可保持永不过期。
    约 5-8 秒完成。
    """
    from .client_runtime_compat import (
        sync_playwright, get_launch_args, get_ignore_default_args,
        apply_runtime_normalization_sync, CHROME_UA, GOOFISH_PROXY_BYPASS,
    )
    from .profile_lock import try_acquire, release, detect_chrome_profile_in_use

    profile_dir = Path(profile_dir)

    # 检查 profile 是否被占用
    in_use, reason = detect_chrome_profile_in_use(profile_dir)
    if in_use:
        log.warning("[goofish_refresh] profile 被占用, 跳过刷新: %s", reason)
        return False

    # 清理残留锁文件
    _now = time.time()
    for _base in (profile_dir, profile_dir / "Default"):
        for _lname in ("SingletonLock", "SingletonCookie", "SingletonSocket", "SingletonPort"):
            _lp = _base / _lname
            if _lp.exists():
                try:
                    if (_now - _lp.stat().st_mtime) > 30:
                        _lp.unlink(missing_ok=True)
                except Exception:
                    pass

    ok, _ = try_acquire(profile_dir, owner="gf_session_refresh")
    if not ok:
        log.warning("[goofish_refresh] 获取 profile 锁失败, 跳过刷新")
        return False

    try:
        p = sync_playwright().start()
        try:
            _args = get_launch_args(headless=True, lang="zh-CN", extra=[
                "--disable-features=TranslateUI,ThirdPartyCookiesDeprecation,"
                "TrackingProtection3pcd,PrivacySandboxSettings4",
                f"--proxy-bypass-list={GOOFISH_PROXY_BYPASS}",
            ])
            _lkw = dict(
                user_data_dir=str(profile_dir),
                headless=False,
                no_viewport=True,
                locale="zh-CN",
                accept_downloads=False,
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=True),
                user_agent=CHROME_UA,
                timeout=30000,
            )
            # 必须传 executable_path，否则 Playwright 自带 Chromium 无法兼容系统 Chrome profile
            if chrome_path:
                _lkw["executable_path"] = chrome_path

            try:
                ctx = p.chromium.launch_persistent_context(**_lkw)
            except TypeError:
                # 某些 Playwright 版本不支持 no_viewport
                _lkw.pop("no_viewport", None)
                _lkw["viewport"] = {"width": 1280, "height": 860}
                ctx = p.chromium.launch_persistent_context(**_lkw)

            apply_runtime_normalization_sync(ctx)

            # 关键：把 Playwright 写入的完整 cookie 集注入到 headless Chrome
            # 否则 headless Chrome 只有 SQLite 里残缺的持久 cookie，
            # ctx.cookies() 只能拿回那些残缺的，覆盖 cache.json 把好 cookie 毁掉
            _existing_playwright_cache = False
            try:
                _existing_data = json.loads(_cache_path(profile_dir).read_text(encoding="utf-8"))
                _existing_hint = (_existing_data.get("account_hint") or "").lower()
                if "playwright" in _existing_hint:
                    _raw = _existing_data.get("raw_cookies", [])
                    if isinstance(_raw, list) and _raw:
                        _cleaned = []
                        for c in _raw:
                            if not isinstance(c, dict):
                                continue
                            cc = dict(c)
                            ss = cc.get("sameSite")
                            if ss and str(ss).capitalize() not in ("Strict", "Lax", "None"):
                                cc.pop("sameSite", None)
                            elif ss:
                                cc["sameSite"] = str(ss).capitalize()
                            _cleaned.append(cc)
                        ctx.add_cookies(_cleaned)
                        _existing_playwright_cache = True
                        log.info("[goofish_refresh] 已注入 %d 条 Playwright cookie 到 headless Chrome", len(_cleaned))
            except Exception as _e:
                log.warning("[goofish_refresh] 注入 Playwright cookie 异常（继续）：%s", _e)

            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # v6.0.75:同時攔截 login.token 響應,順手刷 access_token (WebSocket 用)
            # 不額外開瀏覽器,完全寄生在 6h session refresh 流程內
            _ws_token_captured = {"access": "", "refresh": "", "exp_ms": 0}

            def _intercept_ws_token(resp):
                try:
                    url = resp.url
                    if "mtop.taobao.idlemessage.pc.login.token" not in url:
                        return
                    body = resp.text()
                    import re as _re_local
                    m = _re_local.search(r'\{.*\}', body, _re_local.DOTALL)
                    if not m:
                        return
                    j = json.loads(m.group())
                    ret = j.get("ret", [])
                    if not any("SUCCESS" in str(x) for x in ret):
                        log.warning("[goofish_refresh] login.token 攔截到但非 SUCCESS: %s", ret)
                        return
                    data = j.get("data", {}) or {}
                    tk = str(data.get("accessToken", "") or "")
                    if tk:
                        _ws_token_captured["access"] = tk
                        _ws_token_captured["refresh"] = str(data.get("refreshToken", "") or "")
                        try:
                            _ws_token_captured["exp_ms"] = int(data.get("accessTokenExpiredTime", 0) or 0)
                        except Exception:
                            _ws_token_captured["exp_ms"] = 86400000
                        log.info("[goofish_refresh] ✓ 攔截到 WS access_token: %s...%s (len=%d)",
                                 tk[:24], tk[-12:], len(tk))
                except Exception as _e:
                    log.warning("[goofish_refresh] login.token 攔截異常: %s", _e)

            page.on("response", _intercept_ws_token)

            def _try_click_quick_entry(_log_prefix: str) -> bool:
                """检查并点击「快速进入」按钮恢复登录态。返回是否成功点击。"""
                # 多种选择器尝试
                _selectors = [
                    'button:has-text("快速进入")',
                    'a:has-text("快速进入")',
                    'div:has-text("快速进入")',
                    'span:has-text("快速进入")',
                    '[class*="quick"]:has-text("快速")',
                    '[class*="login-btn"]',
                    '[class*="fast-login"]',
                    'text=快速进入',
                ]
                for _sel in _selectors:
                    try:
                        _btn = page.query_selector(_sel)
                        if _btn and _btn.is_visible():
                            _btn.click()
                            page.wait_for_timeout(3000)
                            log.info(f"[goofish_refresh] {_log_prefix} 已点击「快速进入」")
                            return True
                    except Exception:
                        continue
                # 用 JS 直接点击（更可靠）
                try:
                    _result = page.evaluate("""
                        () => {
                            const all = document.querySelectorAll('button, a, div, span');
                            for (const el of all) {
                                const t = (el.innerText || '').trim();
                                if (t === '快速进入' || t === '快速進入') {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    if _result:
                        page.wait_for_timeout(3000)
                        log.info(f"[goofish_refresh] {_log_prefix} 已用 JS 点击「快速进入」")
                        return True
                except Exception:
                    pass
                return False

            # 访问闲鱼首页 — 这会让服务端刷新 session cookie
            try:
                page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=20000)
            except Exception as nav_err:
                log.warning("[goofish_refresh] 导航异常(非致命): %s", nav_err)
            page.wait_for_timeout(3500)

            # 关键：首页加载后立刻检查并点击「快速进入」恢复登录
            _try_click_quick_entry("首页")

            # ── 访问商品详情页 — 触发 detail API 端的设备指纹 cookie ──
            try:
                page.goto("https://www.goofish.com/item?id=990000000001", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2500)
            except Exception as nav_err2:
                log.warning("[goofish_refresh] 商品页导航异常(非致命): %s", nav_err2)
            # 商品页可能也弹窗
            _try_click_quick_entry("商品页")

            # ── 访问个人中心, 触发 cookie2 / _tb_token_ 等订单 API 必需 cookie ──
            try:
                page.goto("https://www.goofish.com/personal", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2500)
            except Exception as nav_err3:
                log.warning("[goofish_refresh] 个人中心导航异常(非致命): %s", nav_err3)
            # 个人中心一定会弹窗
            _try_click_quick_entry("个人中心")

            # ── 最后再回首页确认 cookie ──
            try:
                page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            # v6.0.75:訪問 /im 頁觸發 login.token (給 WebSocket 用)
            # 這頁初始化時瀏覽器自己會呼 mtop.taobao.idlemessage.pc.login.token,
            # 我們在 page.on("response") 已註冊 listener 攔截
            try:
                log.info("[goofish_refresh] 訪問 /im 觸發 login.token (WS 用)...")
                page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=15000)
                # 等 15 秒讓頁面 SDK 完成初始化呼叫
                for _ in range(30):
                    page.wait_for_timeout(500)
                    if _ws_token_captured["access"]:
                        break
                if not _ws_token_captured["access"]:
                    log.warning("[goofish_refresh] /im 頁訪問完仍未攔到 login.token,WS 暫不可用")
            except Exception as _e_im:
                log.warning("[goofish_refresh] /im 訪問異常 (非致命): %s", _e_im)

            # ── 最后检查是否还有弹窗 ──
            _login_popup = False
            try:
                _popup_sel = page.query_selector("text=扫码安全登录") or \
                             page.query_selector("text=快速进入") or \
                             page.query_selector("text=手机扫码安全登录")
                if _popup_sel:
                    _login_popup = True
            except Exception:
                pass
            try:
                cur_url = page.url or ""
                if "login" in cur_url.lower():
                    _login_popup = True
            except Exception:
                pass

            if _login_popup:
                # 最后尝试一次
                _quick_clicked = _try_click_quick_entry("最后")
                if not _quick_clicked:
                    log.warning("[goofish_refresh] 找不到「快速进入」按钮 — cookie 可能不完整，建议使用「打开登录浏览器」手动完成验证")
                else:
                    page.wait_for_timeout(1500)
                    try:
                        _check = page.query_selector("text=扫码安全登录") or \
                                 page.query_selector("text=快速进入")
                        if _check and _check.is_visible():
                            log.warning("[goofish_refresh] 「快速进入」点击后弹窗仍存在，cookie 可能不完整")
                    except Exception:
                        pass

            # 提取刷新后的 cookie 并保存缓存
            raw_cookies = ctx.cookies()
            # 过滤：只保留闲鱼相关 domain，且跳过 punish 标记 cookie
            _PUNISH_NAMES_PW = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}
            gf_cookies = []
            for c in raw_cookies:
                if not _is_goofish_domain(c.get("domain", "")):
                    continue
                _path = c.get("path", "/") or "/"
                _name = c.get("name", "")
                # 跳过 punish 标记 cookie（路径含 _____tmd_____ 或名字在黑名单）
                if "_____tmd_____" in _path or "punish" in _path.lower():
                    log.warning("[goofish_refresh] 跳过 punish 标记 cookie: %s @ %s%s",
                                _name, c.get("domain"), _path)
                    continue
                if _name in _PUNISH_NAMES_PW:
                    log.warning("[goofish_refresh] 跳过 punish 名 cookie: %s", _name)
                    continue
                gf_cookies.append(c)

            cookie_dict: Dict[str, str] = {}
            m_h5_tk = ""
            m_h5_tk_enc = ""
            for c in gf_cookies:
                name = c.get("name", "")
                value = c.get("value", "")
                if not name:
                    continue
                cookie_dict[name] = value
                if name == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
                    m_h5_tk = value
                elif name == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
                    m_h5_tk_enc = value

            # v6.0.75:在關閉前,若有攔到 WS access_token 寫入緩存(24h 用)
            if _ws_token_captured["access"]:
                try:
                    from core.xianyu_im_http import save_cached_access_token
                    save_cached_access_token(
                        profile_dir,
                        _ws_token_captured["access"],
                        _ws_token_captured["refresh"],
                        _ws_token_captured["exp_ms"] or 86400000,
                    )
                    log.info("[goofish_refresh] WS access_token 已寫入緩存(24h 有效)")
                except Exception as _e_save:
                    log.warning("[goofish_refresh] WS access_token 寫緩存異常: %s", _e_save)

            ctx.close()
            p.stop()

            if not cookie_dict or not m_h5_tk:
                log.warning("[goofish_refresh] 刷新后未获取到有效 cookie (共%d条, m_h5_tk=%s)",
                            len(gf_cookies), "有" if m_h5_tk else "无")
                return False

            # ── 验证 session 是否真正有效 ──
            # 如果 session 有效，前端 JS 的 mtop 调用会让服务端刷新 _m_h5_tk（时间戳≈当前时间）
            # 如果 session 无效，_m_h5_tk 时间戳停留在上次有效时刻 → 时间差很大
            token_hex = _extract_token(m_h5_tk)
            try:
                _tk_parts = m_h5_tk.split("_")
                if len(_tk_parts) >= 2:
                    _tk_ts_ms = int(_tk_parts[-1])
                    _tk_age = time.time() - _tk_ts_ms / 1000.0
                    if _tk_age > 600:  # 10 分钟
                        log.warning(
                            "[goofish_refresh] ⚠ session 验证失败: _m_h5_tk 未被服务端刷新 "
                            "(token age=%.0f分钟) — cookie 存在但服务端不认可, "
                            "请「打开登录浏览器」手动重新登录闲鱼",
                            _tk_age / 60,
                        )
                        # 注意: 不再调用 invalidate_goofish_cookies()
                        # 保留 SQLite 提取的缓存 — HTTP 路径有自己的验证+重试逻辑
                        return False
                    log.info("[goofish_refresh] token 时间戳验证通过 (age=%.0f秒)", _tk_age)
            except (ValueError, IndexError):
                pass

            # 防回退：如果原本是 Playwright 写的（含完整 session cookie），refresh 结果 cookie 数少
            # 就不要覆盖（避免毁了 Playwright 保存的好数据）
            fp = _cache_path(profile_dir)
            try:
                _old = json.loads(fp.read_text(encoding="utf-8"))
                _old_hint = (_old.get("account_hint") or "").lower()
                _old_count = _old.get("cookie_count", 0)
                if "playwright" in _old_hint and _old_count > len(gf_cookies):
                    log.info("[goofish_refresh] 现有 Playwright cache (%d cookie) 比 refresh 结果 (%d) 完整，跳过覆盖",
                             _old_count, len(gf_cookies))
                    return True
            except Exception:
                pass

            # 关键：保留 playwright 标签，让后续 SQLite 直读不覆盖此 cache
            new_hint = "playwright+session_refresh" if _existing_playwright_cache else "session_refresh"
            data = {
                "cookies": cookie_dict,
                "m_h5_tk": m_h5_tk,
                "m_h5_tk_enc": m_h5_tk_enc,
                "token_hex": token_hex,
                "account_hint": new_hint,
                "saved_at": time.time(),
                "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cookie_count": len(gf_cookies),
                "raw_cookies": gf_cookies,
            }
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("[goofish_refresh] session 刷新成功: %d cookie, token=%s..., hint=%s",
                     len(cookie_dict), token_hex[:12] if token_hex else "无", new_hint)
            return True

        except Exception as e:
            log.error("[goofish_refresh] 刷新失败: %s", e)
            try:
                p.stop()
            except Exception:
                pass
            return False
    finally:
        release(profile_dir)
