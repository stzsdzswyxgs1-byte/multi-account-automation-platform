"""纯 HTTP 批量操作 Yahoo 卖家商品（上架/下架/删除/查列表）。

替代 merch_batch.py / merch_id_ops.py 的 Playwright UI 自动化方案。
只需 cookies + wssid，不需要渲染页面、找 checkbox、点按钮。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Set

import requests as _stdlib_requests  # noqa  — D1 worker call,跟 SSL 容錯走 ssl_helper
from curl_cffi.requests import Session as CffiSession

# v6.1.21:Yahoo 批量下架/刪除後,自動同步刪 D1 對應記錄
# 機制:Yahoo 下架後拿到 product_codes → 拉 D1 全量 barcodes →
#       找 product_code 匹配的 barcode → POST /api/delete-barcodes
_D1_WORKER_URL = "https://product-query.<PHONE_REDACTED>.workers.dev"
_D1_UPLOAD_TOKEN = "<D1_UPLOAD_TOKEN_REDACTED>"

from .client_runtime_compat import (
    async_playwright, YAHOO_CURL_CFFI_IMPERSONATE as CURL_CFFI_IMPERSONATE,
    CHROME_UA, CHROME_HTTP_UA,
    get_api_headers, get_launch_args, get_ignore_default_args,
    apply_runtime_normalization_async,
)
from .profile_lock import acquire_or_clear, release
from .human import human_interval_sec, human_jitter_ms
import random as _rng_mod
from .cookie_store import (
    load_cookie_cache, save_cookie_cache, invalidate_cookie_cache,
    DEFAULT_MAX_AGE,
)

LogFn = Callable[[str], None]

# ── 系统代理检测 ───────────────────────────────────────

def _detect_system_proxy() -> str:
    """自动检测系统代理（环境变量 → Windows 注册表）。
    VPN 工具（Clash/V2Ray 等）通常在本地开代理端口。
    """
    # 1. 环境变量优先（HTTPS_PROXY / HTTP_PROXY / ALL_PROXY）
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(var, "").strip()
        if v:
            return v

    # 2. Windows 注册表
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    s = server.strip()
                    if not s.startswith(("http://", "https://", "socks")):
                        s = "http://" + s
                    return s
    except Exception:
        pass

    return ""

# ── 常量 ──────────────────────────────────────────────

RESERVICE_URL = "https://tw.bid.yahoo.com/fe/_reservice_/"
LIST_MERCH_PAGE = "https://tw.bid.yahoo.com/partner/merchandise/list_merchandise"

# curl_cffi 模式下只需覆盖 API 相关头（HTTP 客户端 profile + 基础头由 impersonate 自动生成）
_API_HEADERS = get_api_headers(
    referer="https://tw.bid.yahoo.com/partner/merchandise/list_merchandise",
)

_JS_PARSE_ISOREDUX = """() => {
    const el = document.getElementById('isoredux-data');
    if (!el) return null;
    try { return JSON.parse(el.textContent); }
    catch (e) { return null; }
}"""

# ── 数据结构 ──────────────────────────────────────────

@dataclass
class AuthSession:
    cookies: Dict[str, str] = field(default_factory=dict)
    wssid: str = ""
    extracted_at: float = 0.0
    raw_cookies: List[dict] = field(default_factory=list)
    is_login: bool = False
    # v6.1.45:isoredux 解析失敗(Yahoo 維護/server 異常)時 = True,讓上層不誤判為登出
    # 修「Yahoo 維護期間頁面回 shell 沒 JSON,軟件當登出推 user 重新登入」bug
    is_login_unknown: bool = False
    _http: Optional[object] = field(default=None, repr=False)

    @property
    def is_valid(self) -> bool:
        return bool(self.cookies and self.wssid)

    def build_http(self, proxy: str = ""):
        """构建 HTTP Session（复用 TCP 连接）。
        curl_cffi（HTTP 客户端 profile = 真实 Chrome）。
        若未指定 proxy，自动检测系统代理（VPN 工具常用 127.0.0.1:7890）。
        """
        if self._http is not None:
            return self._http
        kw = dict(impersonate=CURL_CFFI_IMPERSONATE)
        if not proxy:
            proxy = _detect_system_proxy()
        if proxy:
            kw["proxy"] = proxy
        s = CffiSession(**kw)
        s.headers.update(_API_HEADERS)
        # cookies 按 domain 分组设置，确保子域名匹配
        for k, v in self.cookies.items():
            s.cookies.set(k, v, domain=".yahoo.com")
        self._http = s
        return s

    @property
    def http(self):
        return self.build_http()


class AuthExpiredError(Exception):
    pass


@dataclass
class HttpBatchConfig:
    mode: str = "下架"
    repeat: int = 1
    interval_sec: float = 0.0
    headless: bool = True
    batch_size: int = 10


# ── 工具函数 ──────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg, flush=True)


def _norm_mode(mode: str) -> str:
    m = (mode or "").strip()
    if m in ("删除", "刪除"):
        return "刪除"
    if m in ("上架", "下架"):
        return m
    if "商品編號" in m or "商品编号" in m:
        return "根據商品編號下架刪除"
    raise ValueError(f"Unknown mode: {mode!r}")


# ── Cookie + wssid 提取 ──────────────────────────────

def _copy_login_profile(src: Path, dst: Path) -> None:
    """轻量复制 Chrome profile（仅 cookies 相关，约 1MB）。"""
    dst.mkdir(parents=True, exist_ok=True)

    def _safe_copy(s, d):
        try:
            shutil.copy2(s, d)
        except Exception:
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_bytes(s.read_bytes())
            except Exception:
                pass

    def _safe_copy_sqlite(s, d):
        for _ in range(4):
            try:
                sc = sqlite3.connect(str(s), timeout=5)
                dc = sqlite3.connect(str(d))
                sc.backup(dc)
                dc.close(); sc.close()
                return
            except Exception:
                try:
                    shutil.copy2(s, d)
                    return
                except Exception:
                    time.sleep(1.5)

    ls = src / "Local State"
    if ls.exists():
        _safe_copy(ls, dst / "Local State")
    (dst / "First Run").touch()
    sd, dd = src / "Default", dst / "Default"
    dd.mkdir(exist_ok=True)
    pf = sd / "Preferences"
    if pf.exists():
        _safe_copy(pf, dd / "Preferences")
    net_src = sd / "Network"
    if net_src.is_dir():
        net_dst = dd / "Network"
        net_dst.mkdir(exist_ok=True)
        for f in net_src.iterdir():
            if f.name == "Cookies":
                _safe_copy_sqlite(f, net_dst / f.name)
            elif f.name != "Cookies-journal":
                _safe_copy(f, net_dst / f.name)
    for sub in ("Local Storage", "IndexedDB", "Service Worker"):
        ss = sd / sub
        if ss.is_dir():
            for dp, _, fns in os.walk(ss):
                dp = Path(dp)
                rel = dp.relative_to(ss)
                (dd / sub / rel).mkdir(parents=True, exist_ok=True)
                for fn in fns:
                    if fn == "LOCK":
                        continue
                    _safe_copy(dp / fn, dd / sub / rel / fn)


async def extract_auth_session(
    *,
    profile_dir: Path,
    chrome_path: str,
    headless: bool = True,
    proxy: str = "",
    log: Optional[LogFn] = None,
) -> AuthSession:
    """Brief Playwright open (~3-5s) 提取 cookies + wssid，然后关闭浏览器。"""
    profile_dir = Path(profile_dir)
    tmp = profile_dir.parent / f"{profile_dir.name}_http_{int(time.time())}"
    session = AuthSession()

    _log(log, f"[HTTP {_ts()}] 提取 cookies + wssid ...")
    _copy_login_profile(profile_dir, tmp)

    args = get_launch_args(headless=headless)

    try:
        async with async_playwright() as pw:
            lkw = dict(
                user_data_dir=str(tmp),
                executable_path=chrome_path,
                headless=headless,
                args=args,
                ignore_default_args=get_ignore_default_args(headless=headless),
                no_viewport=not headless,
            )
            try:
                ctx = await pw.chromium.launch_persistent_context(**lkw)
            except TypeError:
                lkw.pop("no_viewport", None)
                ctx = await pw.chromium.launch_persistent_context(**lkw)

            await apply_runtime_normalization_async(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(LIST_MERCH_PAGE, wait_until="domcontentloaded", timeout=30000)

            if "login.yahoo" in page.url:
                _log(log, f"[HTTP {_ts()}] 登录已过期! 被重定向到 {page.url}")
                await ctx.close()
                shutil.rmtree(tmp, ignore_errors=True)
                return session

            # 提取 wssid + 確認 isLogin(避免寫無效 cache)
            state = await page.evaluate(_JS_PARSE_ISOREDUX)
            if state:
                session.wssid = (state.get("page") or {}).get("wssid", "")
                user = (state.get("page") or {}).get("user") or {}
                session.is_login = bool(user.get("isLogin", False))
                nickname = user.get("nickname", "?")
                _log(log, f"[HTTP {_ts()}] isLogin={session.is_login}, nickname={nickname}, wssid={session.wssid}")
            else:
                # v6.1.45:解析失敗 ≠ 登出。Yahoo 維護時頁面回 shell 沒 isoredux JSON,
                # 此時 cookies 仍可能有效。標 is_login_unknown,讓上層保留現有 cache,
                # 不要誤推「需要重新登入」
                session.is_login_unknown = True
                _log(log, f"[HTTP {_ts()}] 无法解析 isoredux-data(Yahoo 維護/異常,登入狀態未知)")

            # 提取 cookies
            raw = await ctx.cookies()
            session.cookies = {c["name"]: c["value"]
                               for c in raw if "yahoo" in c.get("domain", "")}
            session.raw_cookies = raw
            session.extracted_at = time.time()
            _log(log, f"[HTTP {_ts()}] cookies={len(session.cookies)} wssid={session.wssid[:8]}...")

            await ctx.close()
    except Exception as e:
        _log(log, f"[HTTP {_ts()}] 浏览器提取失败: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return session


# ── Yahoo API 函数（纯 HTTP） ────────────────────────

def _build_payload(name: str, params: dict) -> dict:
    return {
        "type": "CALL_RESERVICE",
        "payload": params,
        "reservice": {"name": name, "state": "BEGIN"},
        "rtk2": True,
    }


def _post_reservice(session: AuthSession, name: str, params: dict,
                    timeout: int = 30) -> dict:
    """统一 POST 到 _reservice_ 端点（复用 TCP 连接）。

    自動重試(最多 2 次):
    - **網路層**:VPN 場景常見 — TCP timeout / Connection reset / DNS 失敗 /
      TLS 握手 fail → 長 backoff(3/9/21s)給 VPN 重連時間
    - **HTTP 層**:429 限流 / 5xx 服務端錯誤 → 短 backoff(3/6s)
    - **JSON 層**:HTTP 200 但 body 內 SERVICE_ERROR(Yahoo gateway 包裝)→ 短 backoff
    """
    from .ssl_helper import is_curl_cffi_tls_error
    import random as _rnd

    payload = _build_payload(name, params)
    for attempt in range(3):
        # ── A. 網路層:用 try 包住 post,捕獲 VPN 場景常見 curl_cffi 錯誤 ──
        try:
            resp = session.http.post(RESERVICE_URL, json=payload, timeout=timeout)
        except Exception as e:
            # v6.1.25:VPN / 中國跨境網路常見錯誤(timeout/reset/DNS 失敗)→ 長 backoff retry
            # 用 is_curl_cffi_tls_error 統一判定(跟 ssl_helper.cffi_retry_call 同邏輯)
            if is_curl_cffi_tls_error(e) and attempt < 2:
                # 3, 8, 18 秒 + jitter — 給 VPN 重連時間(跟 ssl_helper.cffi_retry_call L137-139 一致)
                wait = (3 ** attempt) + 2 + _rnd.random() * 2
                time.sleep(wait)
                continue
            raise

        # ── B. HTTP 層 429 → 短 backoff retry ──
        if resp.status_code == 429:
            if attempt < 2:
                wait = 3.0 + attempt * 3.0 + human_jitter_ms(500) / 1000.0
                time.sleep(wait)
                continue
            raise AuthExpiredError("429 Too Many Requests (retries exhausted)")

        # ── C. v6.1.25:HTTP 5xx → 短 backoff retry(給 Yahoo backend 恢復) ──
        if resp.status_code in (500, 502, 503, 504):
            if attempt < 2:
                wait = 3.0 + attempt * 3.0 + human_jitter_ms(500) / 1000.0
                time.sleep(wait)
                continue
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            # ── D. v6.1.25:HTTP 200 + JSON SERVICE_ERROR(Yahoo gateway 包 backend 5xx)→ retry ──
            payload_data = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}
            err_name = payload_data.get("name", "") or ""
            err_status_code = payload_data.get("statusCode", 0) or 0
            is_transient = (
                err_name == "SERVICE_ERROR"
                or err_status_code in (500, 502, 503, 504)
            )
            if is_transient and attempt < 2:
                wait = 3.0 + attempt * 3.0 + human_jitter_ms(500) / 1000.0
                time.sleep(wait)
                continue
            err_detail = json.dumps(payload_data, ensure_ascii=False)[:300]
            raise AuthExpiredError(f"API error ({name}): {err_detail}")
        return data
    return {}


def _update_seller_timestamp(session: AuthSession) -> None:
    """模拟浏览器行为：更新卖家最后访问时间戳（易刊也有此调用）。"""
    try:
        _post_reservice(session, "FETCH_UPDATE_SELLER_LAST_ACCESSED_TIMESTAMP", {
            "wssid": session.wssid,
        })
    except Exception:
        pass


def _fetch_wssid_http(cookies: Dict[str, str], log: Optional[LogFn] = None,
                      proxy: str = "") -> str:
    """用 HTTP 从 Yahoo 首页提取 wssid（不需要 Playwright）。

    原理：带 cookies 访问 tw.bid.yahoo.com 首页，从 HTML 中的
    isoredux-data 或 JSON 里提取 wssid。

    v6.1.4:加診斷 log + 多 endpoint fallback,定位「cookies 有效但拉不到 wssid」根因。
    """
    import re

    try:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        kw = dict(impersonate=CURL_CFFI_IMPERSONATE)
        effective_proxy = proxy or _detect_system_proxy()
        if effective_proxy:
            kw["proxy"] = effective_proxy
        s = CffiSession(**kw)
        # v6.1.4:試 2 個 endpoint,主頁不行就試 myauc(都包含 isoredux-data 含 wssid)
        endpoints = [
            "https://tw.bid.yahoo.com/",
            "https://tw.bid.yahoo.com/myauc",
        ]
        for url in endpoints:
            try:
                r = s.get(
                    url,
                    headers={
                        "Cookie": cookie_str,
                        "User-Agent": CHROME_HTTP_UA,
                        "Accept": "text/html",
                    },
                    timeout=15,
                    allow_redirects=False,
                )
                if r.status_code != 200:
                    redir = r.headers.get("Location", "") if 300 <= r.status_code < 400 else ""
                    _log(log, f"[HTTP {_ts()}] _fetch_wssid {url} status={r.status_code} cookies_n={len(cookies)} redirect→{redir[:100]}")
                    continue
                m = re.search(r'"wssid"\s*:\s*"([^"]+)"', r.text)
                if m:
                    _log(log, f"[HTTP {_ts()}] _fetch_wssid OK from {url} wssid_len={len(m.group(1))}")
                    return m.group(1)
                # status=200 但 HTML 內找不到 wssid → cookies 可能不足以登入態
                must = [c for c in ("B", "Y", "T", "F", "ySID") if c in cookies]
                _log(log, f"[HTTP {_ts()}] _fetch_wssid {url} status=200 但 HTML 無 wssid(cookies 有 {must},缺 {[c for c in ('B','Y','T','F','ySID') if c not in cookies]})")
            except Exception as _ee:
                _log(log, f"[HTTP {_ts()}] _fetch_wssid {url} 異常: {_ee}")
    except Exception as e:
        _log(log, f"[HTTP {_ts()}] _fetch_wssid_http 总异常: {e}")
    return ""


def _try_cached_session(
    profile_dir: Path,
    proxy: str = "",
    log: Optional[LogFn] = None,
    max_age: float = 2592000,  # 30天（cookie实际有效期由Yahoo控制）
) -> Optional[AuthSession]:
    """尝试从 cookie cache 加载 session（不需要浏览器、不需要锁）。

    如果 cookies 有效但 wssid 为空，会用 HTTP 从 Yahoo 首页补提取 wssid。
    返回 AuthSession 或 None。
    """
    cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
    if not cookies:
        return None

    # wssid 为空时，用 HTTP 补提取（不需要 Playwright）
    if not wssid:
        _log(log, f"[HTTP {_ts()}] wssid 为空，HTTP 补提取...")
        wssid = _fetch_wssid_http(cookies, log=log, proxy=proxy)
        if wssid:
            # 回写到缓存
            save_cookie_cache(profile_dir, cookies, wssid)
            _log(log, f"[HTTP {_ts()}] wssid 补提取成功: {wssid[:8]}...")
        else:
            _log(log, f"[HTTP {_ts()}] wssid 补提取失败")
            return None

    age = time.time() - saved_at if saved_at > 0 else -1
    _log(log, f"[HTTP {_ts()}] cookie cache 命中 (age={age:.0f}s, wssid={wssid[:8]}...)")
    session = AuthSession(cookies=cookies, wssid=wssid, extracted_at=saved_at)
    session.build_http(proxy=proxy)
    return session


async def _extract_and_save(
    profile_dir: Path,
    chrome_path: str,
    headless: bool,
    proxy: str,
    log: Optional[LogFn],
) -> AuthSession:
    """浏览器提取 cookies + wssid，成功后保存到 cache。"""
    session = await extract_auth_session(
        profile_dir=profile_dir,
        chrome_path=chrome_path,
        headless=headless,
        proxy=proxy, log=log)
    if session.is_valid:
        session.build_http(proxy=proxy)
        # 保存到 cache 供后续使用
        save_cookie_cache(profile_dir, session.cookies, session.wssid,
                          raw_cookies=session.raw_cookies or None)
    return session


def fetch_merchandise_list(
    session: AuthSession,
    *,
    item_status: str = "",
    sort_by: str = "-createTime",
    offset: int = 0,
    limit: int = 40,
) -> Tuple[List[dict], int]:
    """查询商品列表。返回 (items, total)。"""
    params = {
        "sortBy": sort_by,
        "offset": offset,
        "limit": limit,
        "wssid": session.wssid,
        "reset": True,
        "fetchBehaviorKey": "isRefetching",
    }
    if item_status:
        params["itemStatus"] = item_status
    data = _post_reservice(session, "FETCH_MERCHANDISE_LIST", params)
    items = data.get("payload", {}).get("items", [])
    total = data.get("payload", {}).get("total", 0)
    return items, total



def batch_shelve_items(session: AuthSession, ids: List[str]) -> dict:
    """上架。"""
    return _post_reservice(session, "BATCH_SHELVE_ITEMS", {
        "ids": ids,
        "wssid": session.wssid,
    })


def batch_unshelve_items(session: AuthSession, ids: List[str]) -> dict:
    """下架。"""
    return _post_reservice(session, "BATCH_UNSHELVE_ITEMS", {
        "ids": ids,
        "wssid": session.wssid,
        "excludedIdMap": {},
    })


def batch_delete_items(session: AuthSession, ids: List[str]) -> dict:
    """删除。"""
    return _post_reservice(session, "BATCH_DELETE_ITEMS", {
        "ids": ids,
        "wssid": session.wssid,
    })


def _parse_batch_result(data: dict) -> Tuple[int, int, List[str]]:
    """解析批量操作响应。返回 (success_count, fail_count, failed_ids)。"""
    pl = data.get("payload", {})
    success_ids = pl.get("successIds", [])
    failed_items = pl.get("failedItems", [])
    failed_ids = []
    for fi in failed_items:
        if isinstance(fi, str):
            failed_ids.append(fi)
        elif isinstance(fi, dict):
            failed_ids.append(fi.get("id", str(fi)))
    return len(success_ids), len(failed_ids), failed_ids


# ── 编排函数 ─────────────────────────────────────────

def _mode_to_status_and_sort(mode: str) -> Tuple[str, str]:
    """根据模式返回 (item_status, sort_by)。"""
    if mode == "下架":
        return "shelve", "+onTime"
    elif mode == "上架":
        return "close", "-offTime"
    elif mode == "刪除":
        return "close", "+offTime"
    return "", "-createTime"


def _get_batch_fn(mode: str):
    """根据模式返回对应的批量操作函数。"""
    if mode == "上架":
        return batch_shelve_items
    elif mode == "下架":
        return batch_unshelve_items
    elif mode == "刪除":
        return batch_delete_items
    raise ValueError(f"Unknown action mode: {mode}")


async def run_http_batch(
    *,
    profile_dir: Path,
    chrome_path: str,
    cfg: HttpBatchConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
    d1_owner: Optional[str] = None,  # v6.1.21:下架/刪除 模式自動同步刪 D1
) -> Tuple[str, int]:
    """基本模式入口（上架/下架/删除）。

    优先从 cookie cache 加载（不需要浏览器、不需要锁）。
    cache 不可用时退化到 Playwright 提取。
    返回 (status, done_rounds)。

    v6.1.21:`d1_owner` 非空 + mode 為 下架/刪除 時,動作完成後自動清掉
            D1 對應 barcode(防閒魚/煤炉檢測誤判)。
    """
    mode = _norm_mode(cfg.mode)
    # v6.1.21:跨輪累積處理過的 yahoo 商品 ID,結束統一 cleanup
    _processed_ids: List[str] = []
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    used_cache = False
    locked = False

    _engine = "curl_cffi"

    try:
        # ── 1. 优先尝试 cookie cache（零开销、无锁） ──
        session = _try_cached_session(profile_dir, proxy=proxy, log=log)

        if session is None:
            # ── 2. cache 不可用 → 走浏览器提取（需要锁） ──
            _log(log, f"[HTTP {_ts()}] cookie cache 未命中，启动浏览器提取...")
            ok, reason = acquire_or_clear(profile_dir, owner=f"http-batch:{mode}",
                                          log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
            if not ok:
                _log(log, f"[HTTP {_ts()}] LOCKED: {reason}")
                return "locked", 0
            locked = True
            session = await _extract_and_save(
                profile_dir, chrome_path, cfg.headless, proxy, log)
            if not session.is_valid:
                _log(log, f"[HTTP {_ts()}] AUTH FAILED: cookies 或 wssid 无效")
                return "auth_error", 0
        else:
            used_cache = True

        _sys_proxy = _detect_system_proxy()
        _proxy_info = f" proxy={proxy or _sys_proxy}" if (proxy or _sys_proxy) else ""
        _log(log, f"[HTTP {_ts()}] START mode={mode} repeat={cfg.repeat} engine={_engine} cache={'hit' if used_cache else 'miss'}{_proxy_info}")

        # 模拟浏览器行为：更新卖家最后访问时间戳
        _update_seller_timestamp(session)

        item_status, sort_by = _mode_to_status_and_sort(mode)
        batch_fn = _get_batch_fn(mode)
        total_ok, total_fail = 0, 0

        for r in range(cfg.repeat):
            if is_stop and is_stop():
                return "stopped", done
            if is_pause and is_pause():
                return "paused", done

            _log(log, f"[HTTP {_ts()}] ROUND {r+1}/{cfg.repeat} -> {mode}")

            # 每轮只处理当前页（40 件）
            try:
                items, total = fetch_merchandise_list(
                    session, item_status=item_status,
                    sort_by=sort_by, offset=0, limit=40)
            except AuthExpiredError:
                _log(log, f"[HTTP {_ts()}] AUTH 过期，重新提取...")
                invalidate_cookie_cache(profile_dir)
                if not locked:
                    ok, reason = acquire_or_clear(profile_dir, owner=f"http-batch:{mode}",
                                                  log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
                    if not ok:
                        _log(log, f"[HTTP {_ts()}] LOCKED: {reason}")
                        return "locked", done
                    locked = True
                session = await _extract_and_save(
                    profile_dir, chrome_path, cfg.headless, proxy, log)
                if not session.is_valid:
                    return "auth_error", done
                try:
                    items, total = fetch_merchandise_list(
                        session, item_status=item_status,
                        sort_by=sort_by, offset=0, limit=40)
                except Exception as e2:
                    _log(log, f"[HTTP {_ts()}] 重试仍失败: {e2}")
                    return "error", done

            if not items:
                _log(log, f"[HTTP {_ts()}] 无商品可操作 (status={item_status})")
                # 关键：返回 exhausted 让外层 while 循环立即终止，否则会死循环（每秒重启 N 次）
                return "exhausted", done

            ids = [it["id"] for it in items]
            _log(log, f"[HTTP {_ts()}] 第 {r+1} 页: {len(ids)} 件 (剩余 {total})")

            try:
                data = batch_fn(session, ids)
                ok_cnt, fail_cnt, failed = _parse_batch_result(data)
                total_ok += ok_cnt
                total_fail += fail_cnt
                # v6.1.21:記下成功處理的 ID(扣掉 failed),稍後 D1 cleanup
                if mode in ("下架", "刪除"):
                    _fail_set = set(failed or [])
                    _processed_ids.extend(x for x in ids if x not in _fail_set)
                if failed:
                    _log(log, f"[HTTP {_ts()}]   ok={ok_cnt} fail={fail_cnt} failed={failed[:5]}")
                else:
                    _log(log, f"[HTTP {_ts()}]   ok={ok_cnt}")
            except AuthExpiredError:
                _log(log, f"[HTTP {_ts()}] AUTH 过期 (batch)，重新提取...")
                invalidate_cookie_cache(profile_dir)
                if not locked:
                    ok, reason = acquire_or_clear(profile_dir, owner=f"http-batch:{mode}",
                                                  log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
                    if not ok:
                        return "locked", done
                    locked = True
                session = await _extract_and_save(
                    profile_dir, chrome_path, cfg.headless, proxy, log)
                if not session.is_valid:
                    return "auth_error", done
                try:
                    data = batch_fn(session, ids)
                    ok_cnt, fail_cnt, failed = _parse_batch_result(data)
                    total_ok += ok_cnt
                    total_fail += fail_cnt
                except Exception as e2:
                    _log(log, f"[HTTP {_ts()}] 重试仍失败: {e2}")
                    return "error", done

            done = r + 1

            # 操作后刷新列表（模拟真实浏览器行为：BATCH → FETCH 刷新）
            try:
                fetch_merchandise_list(
                    session, item_status=item_status,
                    sort_by=sort_by, offset=0, limit=40)
            except Exception:
                pass

            # 每轮操作完等待 interval + 异常处理随机延迟
            _extra = _rng_mod.uniform(3.0, 6.0)
            if cfg.interval_sec > 0:
                delay = human_interval_sec(cfg.interval_sec) + _extra
            else:
                delay = _extra
            _log(log, f"[HTTP {_ts()}] interval wait: {delay:.1f}s")
            end_t = time.time() + delay
            while time.time() < end_t:
                if is_stop and is_stop():
                    return "stopped", done
                await asyncio.sleep(min(0.5, end_t - time.time()))

            # 短暂间隔后下一轮
            time.sleep(human_jitter_ms(300) / 1000.0)

        _log(log, f"[HTTP {_ts()}] DONE: ok={total_ok} fail={total_fail} ({done} rounds)")

        # v6.1.21:下架/刪除 結束後同步刪 D1 對應 barcode
        if mode in ("下架", "刪除") and d1_owner and _processed_ids:
            try:
                cleanup_d1_after_unshelve(_processed_ids, owner=d1_owner, log=log)
            except Exception as _e_d1:
                _log(log, f"[D1] cleanup 異常(不影響 Yahoo 結果): {_e_d1}")

        return "ok", done

    except Exception as e:
        _log(log, f"[HTTP {_ts()}] ERROR: {e}")
        # v6.1.21:即使 error,已下架/刪除的部分也要清 D1
        if mode in ("下架", "刪除") and d1_owner and _processed_ids:
            try:
                cleanup_d1_after_unshelve(_processed_ids, owner=d1_owner, log=log)
            except Exception:
                pass
        return "error", done
    finally:
        if locked:
            release(profile_dir)


def cleanup_d1_after_unshelve(
    unshelved_product_codes: List[str],
    *,
    owner: str,
    log: Optional[LogFn] = None,
) -> Tuple[int, int]:
    """Yahoo 批量下架/刪除後,清掉 D1 對應記錄(防止閒魚/煤炉檢測誤判)。

    v6.1.21:用新 /api/delete-by-product-codes endpoint(精準 + 快)
      • Yahoo product_code 是 unique → DELETE WHERE product_code IN (?) 不會誤刪
      • 帶 owner 雙保險:DELETE ... AND owner = ?
      • 不用先拉全量 D1(省 30-60s),直接 POST 帶 product_codes

    Args:
        unshelved_product_codes: Yahoo 剛下架的商品編號 list
        owner: D1 帳號 TG ID(從 settings tg_chat_id)
        log: 日誌 callback

    Returns:
        (matched_count, deleted_count)
        v6.1.21:matched == deleted(因為精準刪,不再有過刪)
    """
    if not unshelved_product_codes:
        return 0, 0
    if not owner:
        _log(log, f"[D1] 跳過清理:沒設定 owner(TG ID)")
        return 0, 0

    # v6.1.21 fix:strip 後再 filter 空字串(防 '  '/None/'' 等空白漏網)
    unshelved_list: List[str] = []
    for x in unshelved_product_codes:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            unshelved_list.append(s)
    if not unshelved_list:
        return 0, 0

    _log(log, f"[D1] 開始清理:Yahoo 下架 {len(unshelved_list)} 件 → 精準刪 product_code")

    # 分批刪(每 200 個 product_code,worker 內部還會切 50/批 SQL prepare)
    BATCH = 200
    deleted = 0
    for i in range(0, len(unshelved_list), BATCH):
        chunk = unshelved_list[i:i + BATCH]
        try:
            r = _stdlib_requests.post(
                f"{_D1_WORKER_URL}/api/delete-by-product-codes",
                json={
                    "token": _D1_UPLOAD_TOKEN,
                    "product_codes": chunk,
                    "owner": owner,  # 雙保險,只刪自己帳號的
                },
                timeout=30,
            )
            data = r.json()
            if data.get("ok"):
                deleted += data.get("deleted", 0)
            else:
                _log(log, f"[D1] 刪除批次失敗: {data.get('error', '?')}")
        except Exception as e:
            _log(log, f"[D1] 刪除異常: {e}")

    _log(log, f"[D1] 清理完成:刪除 {deleted} 條 D1 記錄(精準匹配 product_code)")
    return len(unshelved_list), deleted


async def run_http_batch_unshelve_by_date(
    *,
    profile_dir: Path,
    chrome_path: str,
    cutoff_date: str,
    headless: bool = True,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    d1_owner: Optional[str] = None,  # v6.1.21:傳 owner 啟用 D1 同步刪
) -> Tuple[str, int, int]:
    """按上架日期筛选，批量下架在售商品（仅下架，不删除）。

    Args:
        cutoff_date: 截止日期 "YYYY/MM/DD"，下架此日期之前上架的在售商品

    Returns:
        (status, total_found, total_ok)
    """
    from datetime import datetime as _dt

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    locked = False

    try:
        cutoff = _dt.strptime(cutoff_date, "%Y/%m/%d")
    except ValueError:
        _log(log, f"[HTTP {_ts()}] 日期格式错误: {cutoff_date}，应为 YYYY/MM/DD")
        return "error", 0, 0

    try:
        session = _try_cached_session(profile_dir, proxy=proxy, log=log)
        if session is None:
            _log(log, f"[HTTP {_ts()}] cookie cache 未命中，启动浏览器提取...")
            ok, reason = acquire_or_clear(profile_dir, owner="http-batch:date-filter",
                                          log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
            if not ok:
                return "locked", 0, 0
            locked = True
            session = await _extract_and_save(profile_dir, chrome_path, headless, proxy, log)
            if not session.is_valid:
                return "auth_error", 0, 0

        _update_seller_timestamp(session)

        # ── Phase 1: 分页拉取全部在售商品，筛选日期 ──
        _log(log, f"[HTTP {_ts()}] 开始按日期筛选下架: cutoff={cutoff_date}（仅在售商品）")
        target_ids: list = []
        offset = 0
        page_size = 40

        while True:
            if is_stop and is_stop():
                return "stopped", len(target_ids), 0

            items, total = fetch_merchandise_list(
                session, item_status="shelve", sort_by="+onTime",
                offset=offset, limit=page_size)

            if not items:
                break

            all_before_cutoff = True
            for it in items:
                on_str = it.get("onDateTime", "-")
                if on_str == "-" or not on_str:
                    continue
                try:
                    on_dt = _dt.strptime(on_str, "%Y/%m/%d %H:%M:%S")
                except ValueError:
                    continue

                if on_dt < cutoff:
                    target_ids.append(it["id"])
                    # 前 3 条打印明细（方便验证日期过滤是否正确）
                    if len(target_ids) <= 3:
                        _log(log, f"[HTTP {_ts()}]   命中: id={it['id']} title={it.get('title','')[:20]} onDateTime={on_str}")
                else:
                    # 按 +onTime 排序，遇到 >= cutoff 的说明后面都不用看了
                    all_before_cutoff = False
                    break

            if not all_before_cutoff:
                break  # 后面的都比 cutoff 新，停止分页

            offset += page_size
            if offset >= total:
                break

            time.sleep(human_jitter_ms(300) / 1000.0)

        _log(log, f"[HTTP {_ts()}] 筛选完成: {len(target_ids)} 件商品在 {cutoff_date} 之前上架 (总计 {total})")

        if not target_ids:
            return "ok", 0, 0

        # ── Phase 2: 批量下架 ──
        total_ok = 0
        succeeded_ids: list = []  # v6.1.21:精準記錄真正成功下架的 IDs(給 D1 cleanup)
        batch_size = 10
        _log(log, f"[HTTP {_ts()}] 开始下架 {len(target_ids)} 件...")

        for i in range(0, len(target_ids), batch_size):
            if is_stop and is_stop():
                return "stopped", len(target_ids), total_ok
            chunk = target_ids[i:i + batch_size]
            try:
                data = batch_unshelve_items(session, chunk)
                ok_cnt, fail_cnt, failed_chunk = _parse_batch_result(data)
                total_ok += ok_cnt
                # v6.1.21 fix:chunk 中扣掉 failed = 真正成功的
                _fail_set = set(failed_chunk or [])
                succeeded_ids.extend(x for x in chunk if x not in _fail_set)
                _log(log, f"[HTTP {_ts()}] 下架批次 {i // batch_size + 1}: ok={ok_cnt} fail={fail_cnt}")
                # 部分失败 → 对失败的 id 再试一次,失败就不管
                if failed_chunk:
                    time.sleep(human_jitter_ms(500) / 1000.0)
                    try:
                        data2 = batch_unshelve_items(session, failed_chunk)
                        ok_c2, fail_c2, failed_chunk2 = _parse_batch_result(data2)
                        total_ok += ok_c2
                        # v6.1.21 fix:retry 也精準記成功的 IDs
                        _fail_set2 = set(failed_chunk2 or [])
                        succeeded_ids.extend(x for x in failed_chunk if x not in _fail_set2)
                        _log(log, f"[HTTP {_ts()}] 下架批次 {i // batch_size + 1} (失败重试 {len(failed_chunk)} 条): ok={ok_c2} fail={fail_c2}")
                    except Exception as e_r:
                        _log(log, f"[HTTP {_ts()}] 下架批次 {i // batch_size + 1} (失败重试)异常: {e_r}")
            except AuthExpiredError:
                _log(log, f"[HTTP {_ts()}] AUTH 过期，重新提取...")
                invalidate_cookie_cache(profile_dir)
                if not locked:
                    ok, reason = acquire_or_clear(profile_dir, owner="http-batch:date-filter",
                                                  log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
                    if not ok:
                        return "locked", len(target_ids), total_ok
                    locked = True
                session = await _extract_and_save(profile_dir, chrome_path, headless, proxy, log)
                if not session.is_valid:
                    return "auth_error", len(target_ids), total_ok
                try:
                    data = batch_unshelve_items(session, chunk)
                    ok_cnt, _, failed_chunk_auth = _parse_batch_result(data)
                    total_ok += ok_cnt
                    # v6.1.21 fix:AUTH retry 路徑也精準記成功 IDs
                    _fail_set_a = set(failed_chunk_auth or [])
                    succeeded_ids.extend(x for x in chunk if x not in _fail_set_a)
                except Exception as e2:
                    _log(log, f"[HTTP {_ts()}] 重试仍失败: {e2}")
            time.sleep(human_jitter_ms(500) / 1000.0)

        _log(log, f"[HTTP {_ts()}] 按日期下架完成: found={len(target_ids)} ok={total_ok}")

        # v6.1.21:Yahoo 下架成功後,同步刪 D1 對應記錄(防閒魚/煤炉檢測誤判)
        # v6.1.21 fix:用 succeeded_ids(精準成功列表),不再用 target_ids[:total_ok]
        if succeeded_ids and d1_owner:
            try:
                cleanup_d1_after_unshelve(
                    [str(x) for x in succeeded_ids if x],
                    owner=d1_owner, log=log,
                )
            except Exception as _e_d1:
                _log(log, f"[D1] cleanup 異常(不影響下架結果): {_e_d1}")

        return "ok", len(target_ids), total_ok

    except Exception as e:
        _log(log, f"[HTTP {_ts()}] ERROR: {e}")
        return "error", 0, 0
    finally:
        if locked:
            release(profile_dir)


# ═══════════════════════════════════════════════════════════
# 复制上新：下架 → 从 publish?id=XXX&mode=clone 抓 merchandise → 重新发布
# ═══════════════════════════════════════════════════════════

def _extract_merchandise_from_clone_page(http_session, item_id: str) -> Tuple[dict, str]:
    """GET /partner/merchandise/publish?id=XXX&mode=clone,
    从 HTML 的 isoredux-data 提取 merchandiseSubmit.merchandise 字典。
    返回 (merchandise_dict, error_msg)
    """
    url = f"https://tw.bid.yahoo.com/partner/merchandise/publish?id={item_id}&mode=clone"
    try:
        r = http_session.get(url, timeout=30)
    except Exception as e:
        return {}, f"GET 失败: {type(e).__name__}: {str(e)[:120]}"
    if r.status_code != 200:
        return {}, f"HTTP {r.status_code}"
    # 正则放宽：允许 id 不在 <script 首个属性
    m = re.search(
        r'<script\b[^>]*?\bid=["\']isoredux-data["\'][^>]*>(.*?)</script>',
        r.text, re.DOTALL,
    )
    if not m:
        return {}, "no isoredux-data"
    try:
        state = json.loads(m.group(1))
    except Exception as e:
        return {}, f"JSON 解析失败: {e}"
    merch = (state.get("merchandiseSubmit") or {}).get("merchandise") or {}
    if not merch or not merch.get("title"):
        return {}, "merchandise 为空（可能商品已删除或未登录）"
    return merch, ""


def _clean_merchandise_for_new_publish(merch: dict) -> dict:
    """把 clone 页面返回的 merchandise 转成 FETCH_PUBLISH_MERCHANDISE 要求的格式。

    主要差异（实测验证）：
    - clone 里 brief/detail 是顶层 → publish 要合在 description: {brief, detail}
    - clone 里 images 是 [{src, id, ...}] → publish 要 [url_string, ...]
    - clone 里 categoryId/attributes 顶层 → publish 要 category: {id, attributes}
    - clone 里 specIds/modelIds/attributes 可能是 {} → publish 可能要 [] 或跳过
    - 移除 id/status/sellerId（新商品由 Yahoo 分配）
    """
    # images: [{src, origin, ...}] → [url_string]（优先用 origin，因为 src 是缩略图）
    raw_imgs = merch.get("images") or []
    images = []
    for img in raw_imgs:
        if isinstance(img, str):
            images.append(img)
        elif isinstance(img, dict):
            url = img.get("origin") or img.get("src") or ""
            if url:
                images.append(url)

    # category 组装
    # clone 返回 attributes: {"分級": ["普級"]}
    # publish 要 attributes: [{"title": "分級", "values": ["普級"], "fail": false}]
    cat_id = str(merch.get("categoryId") or "")
    clone_attrs = merch.get("attributes")
    attrs = []
    if isinstance(clone_attrs, dict):
        for _title, _values in clone_attrs.items():
            if not _title:
                continue
            if not isinstance(_values, list):
                _values = [str(_values)]
            if _values:
                attrs.append({"title": _title, "values": _values, "fail": False})
    elif isinstance(clone_attrs, list):
        # 已经是 list 格式就直接用（兼容后续可能变化）
        attrs = clone_attrs

    # 价格 / 数量从 product.models 拿（clone 的 product.models 是真实数据）
    clone_product = merch.get("product") or {}
    clone_models = clone_product.get("models") if isinstance(clone_product, dict) else []
    models = []
    if isinstance(clone_models, list) and clone_models:
        for cm in clone_models:
            if not isinstance(cm, dict):
                continue
            try:
                _qty = str(int(cm.get("quantity", 1) or 1))
            except Exception:
                _qty = "1"
            try:
                _selling = f"{float(str(cm.get('selling', '0')).replace(',', '')):.2f}"
            except Exception:
                _selling = "0.00"
            models.append({
                "quantity": _qty,
                "price": {"selling": _selling},
                "partNumber": cm.get("partNumber") or {"first": "", "second": ""},
                "barcode": cm.get("barcode", ""),
            })
    if not models:
        # fallback：用顶层 price
        price = merch.get("price") or {}
        try:
            _s = f"{float(str(price.get('selling', '0')).replace(',', '')):.2f}"
        except Exception:
            _s = "0.00"
        models = [{
            "quantity": "1",
            "price": {"selling": _s},
            "partNumber": {"first": "", "second": ""},
            "barcode": "",
        }]

    # shipments：publish 要 dict {isApplyShippingRule: true}（用全局运费规则最稳）
    # clone 返回的是 list，直接用会 deserialize 错误
    shipments_obj = {"isApplyShippingRule": True}

    # listing: 强制立即上架（clone 返回历史时间戳，格式不兼容）
    listing = {"type": "afterdays", "afterdays": 0}

    # bid（竞标设置）— 定价商品为空 dict
    bid = merch.get("bid") if isinstance(merch.get("bid"), dict) else {}

    # presale: 空字符串 type 会被拒，改成 {}
    presale = merch.get("presale")
    if not isinstance(presale, dict) or not presale.get("type"):
        presale = {}

    # purchaseLimit 保证有 min/max 字段
    pl = merch.get("purchaseLimit")
    if not isinstance(pl, dict):
        pl = {}
    pl_out = {
        "minQuantity": str(pl.get("minQuantity", "") or ""),
        "maxQuantity": str(pl.get("maxQuantity", "") or ""),
    }

    # type 必须是 "bid"|"basic"。clone 返回的是 "buynow"（实际是 price.type），改成 "basic"
    _orig_type = merch.get("type") or ""
    out_type = _orig_type if _orig_type in ("bid", "basic") else "basic"

    return {
        "type": out_type,
        "title": merch.get("title") or "",
        "description": {
            "brief": (merch.get("brief") or "").replace("\n", " ").replace("\r", " ").strip(),
            "detail": merch.get("detail") or "",
        },
        "hashtags": merch.get("hashtags") or [],
        "labels": merch.get("label") or merch.get("labels") or [],
        "images": images,
        "location": merch.get("location") or "",
        "video": merch.get("video") or {},
        "useStatus": merch.get("useStatus") or "new",
        "category": {"id": cat_id, "attributes": attrs},
        "payments": merch.get("payments") or [],
        "purchaseLimit": pl_out,
        "presale": presale,
        "shipments": shipments_obj,
        "product": {"models": models},
        "buyMorePromotions": merch.get("buyMorePromotions") or [],
        "listing": listing,
        "bid": bid,
        "saveLocation": True,
    }


def _publish_merchandise(session: AuthSession, merch: dict, timeout: int = 60) -> Tuple[str, str]:
    """调 FETCH_PUBLISH_MERCHANDISE 提交新商品。
    返回 (new_merch_id, error_msg)
    """
    payload = {
        "wssid": session.wssid,
        "merchandise": merch,
    }
    # 自己构造请求（绕开 _post_reservice 的 error throw，保留原始 error detail）
    body = _build_payload("FETCH_PUBLISH_MERCHANDISE", payload)
    try:
        resp = session.http.post(RESERVICE_URL, json=body, timeout=timeout)
    except Exception as e:
        return "", f"POST 失败: {type(e).__name__}: {str(e)[:120]}"
    if resp.status_code != 200:
        return "", f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return "", f"非 JSON: {resp.text[:120]}"
    if data.get("error"):
        pl = data.get("payload") or {}
        errs = (pl.get("errorData") or {}).get("error") or []
        if errs:
            detail = "; ".join(e.get("message", str(e))[:100] for e in errs[:2])
        else:
            detail = pl.get("message", "")[:200] or str(pl)[:200]
        return "", f"Yahoo 拒绝: {detail}"
    new_id = (data.get("payload") or {}).get("id", "")
    return str(new_id), ""


async def run_http_batch_clone_and_relist(
    *,
    profile_dir: Path,
    chrome_path: str,
    count: int = 1,
    interval_sec: float = 0.0,
    headless: bool = True,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> Tuple[str, int]:
    """复制上新：找最久在架 count 个商品，逐个「下架 → 复制数据 → 重新上架」。

    返回 (status, ok_count)
    status: 'done' / 'stopped' / 'locked' / 'auth_error'
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    locked = False

    try:
        # ── 1. 创建 session ──
        session = _try_cached_session(profile_dir, proxy=proxy, log=log)
        if session is None:
            _log(log, f"[CLONE {_ts()}] cookie cache 未命中，启动浏览器提取...")
            ok, reason = acquire_or_clear(profile_dir, owner="http-clone",
                                          log_fn=lambda msg: _log(log, f"[CLONE {_ts()}] {msg}"))
            if not ok:
                _log(log, f"[CLONE {_ts()}] LOCKED: {reason}")
                return "locked", 0
            locked = True
            session = await _extract_and_save(profile_dir, chrome_path, headless, proxy, log)
            if not session.is_valid:
                _log(log, f"[CLONE {_ts()}] AUTH FAILED")
                return "auth_error", 0

        _update_seller_timestamp(session)

        _log(log, f"[CLONE {_ts()}] START count={count} interval={interval_sec}s")

        # ── 2. 拉最久在架 count 条 ──
        try:
            items, _total = fetch_merchandise_list(
                session, item_status="shelve", sort_by="+onTime",
                offset=0, limit=min(count, 40),
            )
        except Exception as e:
            _log(log, f"[CLONE {_ts()}] 拉列表失败: {e}")
            return "auth_error", 0
        # 拉更多页（Yahoo 单次最多 40）
        while len(items) < count and _total > len(items):
            try:
                more, _ = fetch_merchandise_list(
                    session, item_status="shelve", sort_by="+onTime",
                    offset=len(items), limit=min(count - len(items), 40),
                )
                if not more:
                    break
                items.extend(more)
            except Exception:
                break
        items = items[:count]
        if not items:
            _log(log, f"[CLONE {_ts()}] 无在架商品")
            return "done", 0
        _log(log, f"[CLONE {_ts()}] 取到 {len(items)} 个最久在架商品")

        # ── 3. 逐个处理 ──
        ok = 0
        fail = 0
        for idx, item in enumerate(items, 1):
            if is_stop and is_stop():
                _log(log, f"[CLONE {_ts()}] STOP requested")
                return "stopped", ok
            # 暂停
            while is_pause and is_pause():
                await asyncio.sleep(0.25)
                if is_stop and is_stop():
                    return "stopped", ok

            item_id = str(item.get("id", ""))
            title = (item.get("title") or "")[:30]

            # 3a. 下架
            try:
                r = batch_unshelve_items(session, [item_id])
                succ, _, _ = _parse_batch_result(r)
                if succ == 0:
                    _log(log, f"[CLONE {idx}/{len(items)}] 下架失败 id={item_id}")
                    fail += 1
                    continue
            except Exception as e:
                _log(log, f"[CLONE {idx}/{len(items)}] 下架异常 id={item_id}: {e}")
                fail += 1
                continue

            # 3b. 抓 clone 页面
            merch, err = _extract_merchandise_from_clone_page(session.http, item_id)
            if err:
                _log(log, f"[CLONE {idx}/{len(items)}] 抓 clone 失败 id={item_id}: {err}")
                fail += 1
                continue

            # 3c. 清洗 + 重新发布
            new_merch = _clean_merchandise_for_new_publish(merch)
            new_id, pub_err = _publish_merchandise(session, new_merch)
            if pub_err:
                _log(log, f"[CLONE {idx}/{len(items)}] 发布失败 原id={item_id}: {pub_err}")
                fail += 1
                continue

            ok += 1
            _log(log, f"[CLONE {idx}/{len(items)}] ✓ 原id={item_id} → 新id={new_id} 「{title}」")

            # 3d. 间隔
            if interval_sec > 0 and idx < len(items):
                await asyncio.sleep(interval_sec)

        _log(log, f"[CLONE {_ts()}] DONE 成功={ok} 失败={fail}")
        return "done", ok

    except AuthExpiredError as e:
        _log(log, f"[CLONE {_ts()}] AUTH EXPIRED: {e}")
        return "auth_error", 0
    except Exception as e:
        _log(log, f"[CLONE {_ts()}] ERROR: {type(e).__name__}: {e}")
        return "auth_error", 0
    finally:
        if locked:
            release(profile_dir)


async def run_http_merch_id_ops(
    *,
    base_dir: Path,
    profile_dir: Path,
    chrome_path: str,
    account_name: str,
    profile_id: str,
    batches: List[List[str]],
    cfg: HttpBatchConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> str:
    """根据商品编号批量下架 + 删除（纯 HTTP）。

    优先从 cookie cache 加载（不需要浏览器、不需要锁）。

    返回:"done" / "auth_failed" / "stopped" / "locked" / "error" / "skip"
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    locked = False

    if not batches:
        _log(log, f"[HTTP {_ts()}] {account_name}: 无批次")
        return "skip"

    _engine = "curl_cffi"

    try:
        # ── 1. 优先尝试 cookie cache ──
        session = _try_cached_session(profile_dir, proxy=proxy, log=log)

        if session is None:
            # ── 2. cache 不可用 → 浏览器提取 ──
            _log(log, f"[HTTP {_ts()}] cookie cache 未命中，启动浏览器提取...")
            ok, reason = acquire_or_clear(profile_dir, owner=f"http-id-ops:{account_name}",
                                          log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
            if not ok:
                _log(log, f"[HTTP {_ts()}] LOCKED: {reason}")
                return "locked"
            locked = True
            session = await _extract_and_save(
                profile_dir, chrome_path, cfg.headless, proxy, log)
            if not session.is_valid:
                _log(log, f"[HTTP {_ts()}] {account_name}: AUTH FAILED")
                return "auth_failed"

        _log(log, f"[HTTP {_ts()}] {account_name}: START ({len(batches)} 批) engine={_engine}")

        # 模拟浏览器行为：更新卖家最后访问时间戳
        _update_seller_timestamp(session)

        for i, ids in enumerate(batches, 1):
            if is_stop and is_stop():
                _log(log, f"[HTTP {_ts()}] STOP requested")
                return "stopped"
            if is_pause and is_pause():
                _log(log, f"[HTTP {_ts()}] PAUSE requested")
                while is_pause and is_pause():
                    if is_stop and is_stop():
                        return "stopped"
                    await asyncio.sleep(0.5)

            _log(log, f"[HTTP {_ts()}] {account_name}: batch {i}/{len(batches)} ({len(ids)} ids)")

            # 下架
            try:
                data_unshelve = batch_unshelve_items(session, ids)
                ok_u, fail_u, failed_u = _parse_batch_result(data_unshelve)
                _log(log, f"[HTTP {_ts()}]   下架: ok={ok_u} fail={fail_u}")
                # 部分失败 → 对失败的 id 再试一次,失败就不管(避免殭屍商品)
                if failed_u:
                    time.sleep(human_jitter_ms(500) / 1000.0)
                    try:
                        data_u2 = batch_unshelve_items(session, failed_u)
                        ok_u2, fail_u2, _ = _parse_batch_result(data_u2)
                        _log(log, f"[HTTP {_ts()}]   下架(失败重试 {len(failed_u)} 条): ok={ok_u2} fail={fail_u2}")
                    except Exception as e_r:
                        _log(log, f"[HTTP {_ts()}]   下架(失败重试)异常: {e_r}")
            except AuthExpiredError:
                _log(log, f"[HTTP {_ts()}]   AUTH 过期，重新提取...")
                invalidate_cookie_cache(profile_dir)
                if not locked:
                    ok, reason = acquire_or_clear(profile_dir, owner=f"http-id-ops:{account_name}",
                                                  log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
                    if not ok:
                        _log(log, f"[HTTP {_ts()}]   LOCKED: {reason}")
                        return "locked"
                    locked = True
                session = await _extract_and_save(
                    profile_dir, chrome_path, cfg.headless, proxy, log)
                if not session.is_valid:
                    _log(log, f"[HTTP {_ts()}]   重新提取失败，终止")
                    return "auth_failed"
                try:
                    data_unshelve = batch_unshelve_items(session, ids)
                    ok_u, fail_u, _ = _parse_batch_result(data_unshelve)
                    _log(log, f"[HTTP {_ts()}]   下架(重试): ok={ok_u} fail={fail_u}")
                except Exception as e:
                    _log(log, f"[HTTP {_ts()}]   下架(重试)失败: {e}")
                    continue
            except Exception as e:
                _log(log, f"[HTTP {_ts()}]   下架失败: {e}")

            # 下架后刷新列表（模拟真实浏览器行为）
            try:
                fetch_merchandise_list(
                    session, item_status="close", sort_by="-offTime",
                    offset=0, limit=40)
            except Exception:
                pass

            # 短暂间隔
            time.sleep(human_jitter_ms(300) / 1000.0)

            # 删除
            try:
                data_delete = batch_delete_items(session, ids)
                ok_d, fail_d, failed_d = _parse_batch_result(data_delete)
                _log(log, f"[HTTP {_ts()}]   删除: ok={ok_d} fail={fail_d}")
                # 部分失败 → 对失败的 id 再试一次,失败就不管
                if failed_d:
                    time.sleep(human_jitter_ms(500) / 1000.0)
                    try:
                        data_d2 = batch_delete_items(session, failed_d)
                        ok_d2, fail_d2, _ = _parse_batch_result(data_d2)
                        _log(log, f"[HTTP {_ts()}]   删除(失败重试 {len(failed_d)} 条): ok={ok_d2} fail={fail_d2}")
                    except Exception as e_r:
                        _log(log, f"[HTTP {_ts()}]   删除(失败重试)异常: {e_r}")
            except AuthExpiredError:
                invalidate_cookie_cache(profile_dir)
                if not locked:
                    ok, reason = acquire_or_clear(profile_dir, owner=f"http-id-ops:{account_name}",
                                                  log_fn=lambda msg: _log(log, f"[HTTP {_ts()}] {msg}"))
                    if not ok:
                        return "locked"
                    locked = True
                session = await _extract_and_save(
                    profile_dir, chrome_path, cfg.headless, proxy, log)
                if session.is_valid:
                    try:
                        data_delete = batch_delete_items(session, ids)
                        ok_d, fail_d, _ = _parse_batch_result(data_delete)
                        _log(log, f"[HTTP {_ts()}]   删除(重试): ok={ok_d} fail={fail_d}")
                    except Exception as e:
                        _log(log, f"[HTTP {_ts()}]   删除(重试)失败: {e}")
            except Exception as e:
                _log(log, f"[HTTP {_ts()}]   删除失败: {e}")

            # 删除后刷新列表（模拟真实浏览器行为）
            try:
                fetch_merchandise_list(
                    session, item_status="close", sort_by="-offTime",
                    offset=0, limit=40)
            except Exception:
                pass

            # 间隔等待 + 异常处理随机延迟
            if i < len(batches):
                _extra = _rng_mod.uniform(3.0, 6.0)
                delay = (human_interval_sec(cfg.interval_sec) + _extra) if cfg.interval_sec > 0 else _extra
                _log(log, f"[HTTP {_ts()}] 等待 {delay:.1f}s")
                end_t = time.time() + delay
                while time.time() < end_t:
                    if is_stop and is_stop():
                        return "stopped"
                    await asyncio.sleep(min(0.5, end_t - time.time()))

        _log(log, f"[HTTP {_ts()}] {account_name}: DONE")
        return "done"

    except Exception as e:
        _log(log, f"[HTTP {_ts()}] {account_name}: ERROR {e}")
        return "error"
    finally:
        if locked:
            release(profile_dir)
