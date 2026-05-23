"""純 HTTP 抓取 myauc 統計數字 — 取代 Playwright scraper (v6.0.83+)

從 `https://tw.bid.yahoo.com/myauc` 解析:
- 上架中商品數 (item_count)
- 已付款待出貨訂單數 (paid_to_ship)
- 取貨付款訂單數 (cod)

实机适配:myauc 是 SSR HTML,數字直接寫在 HTML 內,
格式 `<a href="/myauc?sellerTab=X">標籤 <em>N</em></a>`,
沒 <em> 即為 0。

跟 BOSH IM 模組一致,共用 _build_session(cookies + curl_cffi chrome136 impersonate),
HTTP 層 fingerprint 跟真 Chrome 不可區分。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .client_runtime_compat import YAHOO_CURL_CFFI_IMPERSONATE as CURL_CFFI_IMPERSONATE, get_html_headers
from .cookie_store import load_cookie_cache, load_raw_cookies, save_cookie_cache, load_from_chrome_sqlite_yahoo
from .im_http_ops import _build_session
from .merch_http_ops import _detect_system_proxy

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

_MYAUC_URL = "https://tw.bid.yahoo.com/myauc"

# 從 myauc HTML 抓 <a href="/myauc?sellerTab=X">標籤 <em>N</em></a>
_RE_TAB = re.compile(
    r'<a\s+href="/myauc\?sellerTab=([^"]+)">\s*([^<]+?)(?:\s*<em>(\d+)</em>)?\s*</a>',
    re.DOTALL,
)

# tab key → 統計欄位名(對齊 AccountState.last_values 的 key)
_TAB_TO_FIELD = {
    "onSaleItem": "item_count",       # 上架中商品
    "generalOrder": "paid_to_ship",   # 已付款待出貨訂單
    "cvsOrder": "cod",                # 取貨付款訂單
}


# Throttle:同 profile 5 分鐘內最多 save 1 次(避免每分鐘寫 24 次)
_AUTOSAVE_THROTTLE_SEC = 300
_last_autosave_ts: Dict[str, float] = {}


def _autosave_cookies(profile_dir: Path, session, on_log: LogFn) -> None:
    """把 session 內 server 返回的 Set-Cookie 寫回 cache,讓 cache 自然延壽。

    保護:
    1. Throttle:同 profile 5 分鐘最多寫 1 次(降低 I/O 頻率)
    2. Atomic write(在 save_cookie_cache 內):中斷不會壞檔

    v6.1 根本修復:**移除 content diff** — 即使 cookies 跟 cache 一樣也要寫
    理由:Yahoo session cookies 通常不變,舊版「沒變不寫」導致 saved_at 永遠不更新,
    24h 後 cache 「偽過期」,軟件以為需登入但實際 cookie 還有效 → 標需登入死循環。
    每 5 分鐘 update saved_at 一次,讓 cache 真實反映「最後一次驗證成功的時間」。
    """
    try:
        fresh = {}
        for c in session.cookies.jar:
            if "yahoo" in (c.domain or ""):
                fresh[c.name] = c.value
        if not fresh:
            return

        # Throttle:5 分鐘冷卻
        pid_key = str(profile_dir.resolve())
        now = time.time()
        last = _last_autosave_ts.get(pid_key, 0)
        if now - last < _AUTOSAVE_THROTTLE_SEC:
            return

        # v6.1:不再 content diff — 即使 cookies 一樣也寫,把 saved_at 更新
        # 這樣 cache 不會因「cookies 穩定不變」而 24h 後偽過期
        _, cached_wssid, _ = load_cookie_cache(
            profile_dir, max_age=86400 * 365,
        )
        save_cookie_cache(
            profile_dir, fresh, cached_wssid or "",
            raw_cookies=load_raw_cookies(profile_dir, max_age=86400 * 365) or None,
        )
        _last_autosave_ts[pid_key] = now
    except Exception as e:
        on_log(f"[MYAUC-HTTP] cookie auto-save 跳過: {e}")


def _build_minimal_session_with_stale_cookies(profile_dir: Path, on_log: LogFn):
    """替代路径 _build_session 的 24h 過期 + wssid 強制要求,直接用 stale cookies 試 myauc。

    myauc 是 SSR HTML 頁面,只要有 B token 等基本 cookies 就能 GET,不需要 wssid。
    用於診斷「cookie 過期 24h 但帳號可能其實還活著或被停權」的情況。
    """
    try:
        from curl_cffi.requests import Session as CffiSession
        # max_age 設一年,實質禁用過期 check
        cookies, _wssid, saved_at = load_cookie_cache(profile_dir, max_age=86400 * 365)
        if not cookies:
            return None
        on_log(f"[MYAUC-HTTP] cookie cache 過期(saved={int(time.time()-saved_at)//3600}h前),用 stale 試診斷")
        kw = dict(impersonate=CURL_CFFI_IMPERSONATE)
        proxy = _detect_system_proxy()
        if proxy:
            kw["proxy"] = proxy
        s = CffiSession(**kw)
        # 載 raw cookies(保留 domain/path)
        raw = load_raw_cookies(profile_dir, max_age=86400 * 365)
        if raw:
            for c in raw:
                try:
                    s.cookies.set(
                        c.get("name", ""), c.get("value", ""),
                        domain=c.get("domain", ".yahoo.com"),
                        path=c.get("path", "/"),
                    )
                except Exception:
                    continue
        else:
            for k, v in cookies.items():
                try:
                    s.cookies.set(k, v, domain=".yahoo.com")
                except Exception:
                    continue
        return s
    except Exception as e:
        on_log(f"[MYAUC-HTTP] minimal session build 失敗: {e}")
        return None


def fetch_myauc_stats(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
    timeout: int = 15,
) -> Tuple[Optional[Dict[str, int]], str]:
    """純 HTTP 抓取單一帳號的 myauc 統計。

    Returns:
        (stats_dict, error_msg)
        stats_dict: {"item_count": N, "paid_to_ship": N, "cod": N}
        失敗回 (None, error)。
    """
    on_log = on_log or (lambda *_: None)

    session, _wssid, err = _build_session(profile_dir)
    if not session:
        # v6.1 修復:cache 過期/wssid 提取失敗時,從 Chrome SQLite 強讀 cookies
        # root cause:cache 24h 過期 + Chrome SQLite mtime 沒變 → 死循環標需登入
        # 但 Yahoo cookie 有效期 1 年,SQLite 內 cookies 通常還有效 — 直接讀就能用
        # myauc HTML SSR 頁面本身不需要 wssid,只要 cookies 200 OK 就能 parse stats
        try:
            flat, raw = load_from_chrome_sqlite_yahoo(profile_dir)
            if flat and len(flat) >= 5:
                # 寫回 cache(wssid 空,後續其他流程會 HTTP 補)
                save_cookie_cache(profile_dir, flat, "", raw_cookies=raw)
                on_log(f"[MYAUC-HTTP] cache 過期,從 Chrome SQLite fallback 拿到 {len(flat)} cookies")
                # 試 _build_session — wssid HTTP 補提取可能還是失敗,所以保底用 minimal session
                session, _wssid, err = _build_session(profile_dir)
                if not session:
                    # wssid HTTP 補提取失敗 — 但 myauc HTML 不需 wssid,用 minimal 試
                    on_log(f"[MYAUC-HTTP] _build_session 還是失敗 ({err}),改用 minimal session")
                    session = _build_minimal_session_with_stale_cookies(profile_dir, on_log)
        except Exception as e:
            on_log(f"[MYAUC-HTTP] SQLite fallback 異常: {e}")
        if not session:
            return None, "需登入(cookie 過期)"

    try:
        # HTML 頁面 headers(navigate / document)— allow_redirects=False 偵測 consent redirect
        headers = get_html_headers()
        # v6.1.19:curl_cffi TLS lib bug retry — 修「監控離線 flap」根因
        from .ssl_helper import cffi_retry_call
        r = cffi_retry_call(
            session.get,
            _MYAUC_URL, headers=headers, timeout=timeout, allow_redirects=False,
            max_retries=2,
            on_retry=lambda att, e: on_log(f"[MYAUC-HTTP] TLS lib bug retry #{att}"),
        )

        # v6.1.12:cache 內 cookies 過期但仍能建 session(被 Yahoo 302 到 login)→ 試 SQLite fallback
        # 修同事「右擊打開帳號正常但 GUI 仍顯示需登入」bug — Chrome 內 cookies 可能比 cache 新
        _need_sqlite_retry = False
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "") or r.headers.get("Location", "")
            if "login" in loc or "consent" in loc or "guce" in loc:
                _need_sqlite_retry = True
        elif r.status_code in (401, 403):
            _need_sqlite_retry = True

        if _need_sqlite_retry:
            try:
                flat_sql, raw_sql = load_from_chrome_sqlite_yahoo(profile_dir)
                if flat_sql and len(flat_sql) >= 5:
                    # 用 SQLite cookies 重建 session 試一次
                    save_cookie_cache(profile_dir, flat_sql, "", raw_cookies=raw_sql)
                    on_log(f"[MYAUC-HTTP] 302/401 → SQLite fallback({len(flat_sql)} cookies)重試")
                    try:
                        session.close()
                    except Exception:
                        pass
                    session2, _, _ = _build_session(profile_dir)
                    if session2 is None:
                        session2 = _build_minimal_session_with_stale_cookies(profile_dir, on_log)
                    if session2 is not None:
                        session = session2
                        # v6.1.19:同樣套 TLS retry
                        r = cffi_retry_call(
                            session.get,
                            _MYAUC_URL, headers=headers, timeout=timeout, allow_redirects=False,
                            max_retries=2,
                        )
            except Exception as _e_sql:
                on_log(f"[MYAUC-HTTP] SQLite fallback 異常: {_e_sql}")

        # 307/302 redirect 到 consent / login = cookie 不認
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "") or r.headers.get("Location", "")
            if "consent" in loc or "guce" in loc:
                return None, "需登入(cookie 同意頁)"
            if "login" in loc:
                return None, "需登入(導至登入頁)"
            return None, f"redirect {r.status_code} → {loc[:80]}"
        if r.status_code != 200:
            if r.status_code in (401, 403):
                return None, f"未登入(HTTP {r.status_code})"
            # v6.1.45:429 = 限流限速,傳 Retry-After 讓上層精確退避
            # 業界做法:Yahoo 限流啟動會先 429,正確退避避免被進一步加重
            if r.status_code == 429:
                _retry_after = r.headers.get("Retry-After", "") or r.headers.get("retry-after", "")
                try:
                    _ra_sec = max(30, min(600, int(_retry_after)))  # clip [30, 600]
                except Exception:
                    _ra_sec = 60  # 沒帶或解析失敗用 60s 預設
                return None, f"RATE_LIMITED:{_ra_sec}"
            # v6.1.62:5xx 時 dump 完整 request+response 供診斷 root cause
            # 不改邏輯,只加診斷;Yahoo 官方說「被限制時返 500」,看 response body 找實際 reason
            if 500 <= r.status_code < 600:
                try:
                    _dump_myauc_5xx_diag(
                        profile_dir, _MYAUC_URL, headers, session, r, on_log
                    )
                except Exception as _e_dump:
                    on_log(f"[MYAUC-HTTP] 5xx dump 異常(忽略): {_e_dump}")
            return None, f"myauc HTTP {r.status_code}"

        # ✅ 200 OK = cookie 還有效 → 不管後續識別「停權」/「正常」,都先 auto-save cookies 保活
        # 否則停權帳號的 cookies 也會慢慢過期,將來解除停權後又要重新登入
        _autosave_cookies(profile_dir, session, on_log)

        html = r.text
        # 偵測登入頁 / 停權頁 / 帳號異常
        if any(kw in html for kw in ("我要登入", "您未登入", "請先登入", "用戶名稱", "密碼")):
            if "我的拍賣" not in html and "上架中商品" not in html:
                return None, "未登入(cookie 過期)"
        if any(kw in html for kw in ("帳號暫停", "帳號停權", "已停權", "已停用", "凍結", "您的帳號已被")):
            return None, "帳號停權"
        if "我的拍賣" not in html and "myauc" not in html.lower():
            return None, "myauc 內容異常(可能未登入)"

        stats: Dict[str, int] = {
            "item_count": 0, "paid_to_ship": 0, "cod": 0,
        }
        for m in _RE_TAB.finditer(html):
            tab = m.group(1)
            count = int(m.group(3) or 0)
            field = _TAB_TO_FIELD.get(tab)
            if field:
                stats[field] = count

        # v6.1.9:商品 > 9999 時 Yahoo tab 內 <em> 顯示「9999+」,regex \d+ 抓 9999 但實際是
        # 「9999+」字串,正則匹配失敗 → 解析 0。fallback 從頁面文字區「直購商品 N 件」抓真實數字
        if stats["item_count"] == 0 or stats["item_count"] >= 9999:
            m_total = re.search(r'直購商品\s*(?:<[^>]+>\s*)?(\d{1,7})\s*(?:<[^>]+>\s*)?件', html)
            if m_total:
                real_count = int(m_total.group(1))
                if real_count > stats["item_count"]:
                    stats["item_count"] = real_count

        return stats, ""
    except Exception as e:
        return None, f"fetch_myauc_stats 異常: {e}"
    finally:
        try:
            session.close()
        except Exception:
            pass


def fetch_unread_im_total(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
) -> Tuple[int, Dict[str, int], str]:
    """純 HTTP(BOSH)抓取單一帳號的 IM 未讀總數 + per-channel 細節。

    Returns:
        (total_unread, channel_unread_map, error)
        channel_unread_map: {channel_id: unread_count}
    """
    on_log = on_log or (lambda *_: None)
    try:
        from .yahoo_im_bosh_ext import BOSHSession
        with BOSHSession(profile_dir, on_log=on_log) as s:
            resp, err = s.get_user_unread_channels()
            if err or not isinstance(resp, dict):
                return 0, {}, err or "BOSH 回應異常"
            total = int(resp.get("totalUnread", 0) or 0)
            result = resp.get("result") or resp.get("channels") or []
            channel_map: Dict[str, int] = {}
            for c in result:
                cid = c.get("chID") or c.get("channelId") or ""
                # BOSH 實機返回 key 是 "count",不是 "unreadCount"/"unread"
                cnt = int(c.get("count", 0) or c.get("unreadCount", 0) or c.get("unread", 0) or 0)
                if cid:
                    channel_map[cid] = cnt
            return total, channel_map, ""
    except Exception as e:
        return 0, {}, f"fetch_unread_im_total 異常: {e}"


# ── v6.1.62:myauc 5xx 診斷 dump ───────────────────────────

def _dump_myauc_5xx_diag(
    profile_dir: Path,
    request_url: str,
    request_headers: Dict[str, str],
    session,
    response,
    on_log: Optional[LogFn] = None,
) -> None:
    """v6.1.62:撞 5xx 時 dump 完整 request+response 到 runtime/myauc_500_diag/。

    Yahoo 官方文檔說「被限流時返回 500」,看 response body / headers 能
    定位 server 到底嫌什麼(過頻/cookie 異常/fingerprint/etc)。

    同帳號保留最近 5 份,自動清舊。
    """
    import time as _t
    import json as _json
    _log = on_log or (lambda *_: None)

    try:
        profile_dir = Path(profile_dir)
        # 找到 runtime/myauc_500_diag/ 目錄(用 profile_dir 的父父目錄,即 XDZHGL2.0指令版/)
        base_dir = profile_dir.parent.parent
        out_dir = base_dir / "runtime" / "myauc_500_diag"
        out_dir.mkdir(parents=True, exist_ok=True)
        account = profile_dir.name

        ts = _t.strftime("%Y%m%d_%H%M%S")
        out_fp = out_dir / f"{account}_{ts}.txt"

        lines = [
            f"=== myauc 5xx 診斷 dump (v6.1.62) ===",
            f"帳號: {account}",
            f"時間: {ts}",
            f"",
            f"--- Request ---",
            f"URL:    {request_url}",
            f"Method: GET",
        ]

        # Request headers
        lines.append(f"")
        lines.append(f"Request Headers ({len(request_headers)} 個):")
        for k in sorted(request_headers.keys()):
            v = str(request_headers[k])
            if len(v) > 200:
                v = v[:200] + f"...(共 {len(str(request_headers[k]))} 字)"
            lines.append(f"  {k}: {v}")

        # Cookies(只列 key + 值長度,避免泄漏完整值)
        lines.append(f"")
        try:
            cookies = session.cookies
            cookie_list = []
            try:
                cookie_list = list(cookies.jar) if hasattr(cookies, "jar") else list(cookies)
            except Exception:
                cookie_list = []
            lines.append(f"Cookies ({len(cookie_list)} 個):")
            for c in sorted(cookie_list, key=lambda x: getattr(x, "name", "") or ""):
                name = getattr(c, "name", "?") or "?"
                value = getattr(c, "value", "") or ""
                domain = getattr(c, "domain", "?") or "?"
                lines.append(f"  {name:<30} (domain={domain}, len={len(value)})")
        except Exception as e:
            lines.append(f"Cookies: <讀取失敗 {e}>")

        # Response
        lines.append(f"")
        lines.append(f"--- Response ---")
        lines.append(f"Status: HTTP {response.status_code}")
        lines.append(f"")
        lines.append(f"Response Headers ({len(response.headers)} 個):")
        for k in sorted(response.headers.keys()):
            v = str(response.headers[k])
            if len(v) > 300:
                v = v[:300] + f"...(共 {len(str(response.headers[k]))} 字)"
            lines.append(f"  {k}: {v}")

        # Response body 前 3000 chars
        lines.append(f"")
        lines.append(f"--- Response Body (前 3000 chars) ---")
        try:
            body = response.text or ""
            lines.append(body[:3000])
            if len(body) > 3000:
                lines.append(f"\n...(總共 {len(body)} 字,後面已截斷)")
        except Exception as e:
            lines.append(f"<讀取 body 失敗: {e}>")

        lines.append(f"")
        lines.append(f"=== 分析提示 ===")
        lines.append(f"1. response body 內有沒有 server 給的具體 error message?")
        lines.append(f"   - 「Will be right back」/「請稍後再試」= 軟性限流")
        lines.append(f"   - 「Unauthorized」/「Token expired」= cookies/wssid 問題")
        lines.append(f"   - HTML 頁面 = server 真的給維護頁")
        lines.append(f"2. response headers 有沒有 `Retry-After` / `X-RateLimit-*`?")
        lines.append(f"3. cookies 完整嗎?跟『正常 200 時』對比有什麼差異?")
        lines.append(f"4. request headers 跟真實 Chrome F12 看到的有什麼差別?")

        out_fp.write_text("\n".join(lines), encoding="utf-8")

        # 同帳號保留最近 5 份
        try:
            same_acc_files = sorted(
                out_dir.glob(f"{account}_*.txt"),
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

        _log(f"[MYAUC-HTTP] 5xx 診斷 dump → {out_fp.name}")
    except Exception as e:
        _log(f"[MYAUC-HTTP] 5xx dump 異常: {e}")
