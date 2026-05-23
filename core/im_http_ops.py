"""纯 HTTP 读取/发送 Yahoo IM 消息（不需要浏览器）。

替代 Playwright 自动化方案：
- 读消息：GET /fe/api/im/messages  （替代浏览器内 fetch）
- 发消息：POST /fe/api/im/message/send （替代 fill+Enter）

依赖 cookie_store 提供的 cookies + wssid（由监控模块每 300s 刷新）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import quote

from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import YAHOO_CURL_CFFI_IMPERSONATE as CURL_CFFI_IMPERSONATE, get_api_headers
from .cookie_store import load_cookie_cache, invalidate_cookie_cache, save_cookie_cache, DEFAULT_MAX_AGE, load_raw_cookies, load_from_chrome_sqlite_yahoo
from .human import human_jitter_ms
from .merch_http_ops import _detect_system_proxy, _fetch_wssid_http

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# ── 常量 ──────────────────────────────────────────────

_IM_API_BASE = "https://tw.bid.yahoo.com/fe/api/im"

# curl_cffi 模式：HTTP 客户端 profile = 真实 Chrome，基础头由 impersonate 自动生成
_IM_HEADERS = get_api_headers(origin="https://tw.bid.yahoo.com")

# channelId 方向缓存：{原始id: 正确id}，避免每次都先尝试错误方向再反转
_CHANNEL_DIR_CACHE: dict = {}

# ── 工具函数 ──────────────────────────────────────────


def _yahoo_cookies_dict(session: CffiSession) -> dict:
    """从 session 中只提取 Yahoo 域名的 cookies 为 flat dict。

    raw_cookies 可能含多域名 cookie（如 .mercari.com），
    dict(session.cookies) 遇到同名跨域 cookie（如 _ga）会崩溃。
    """
    result = {}
    try:
        for cookie in session.cookies.jar:
            domain = getattr(cookie, "domain", "") or ""
            if "yahoo" in domain:
                result[cookie.name] = cookie.value
    except Exception:
        pass
    return result


def _im_referer(buyer_cid: str = "") -> str:
    """根据上下文生成 Referer（IM 页面用 /chat/...）。"""
    if buyer_cid:
        cid = buyer_cid if buyer_cid.startswith("Y") else f"Y{buyer_cid}"
        return f"https://tw.bid.yahoo.com/chat/{cid}"
    return "https://tw.bid.yahoo.com/chat"


def build_channel_id(shop_id: str, buyer_id: str) -> str:
    """构建 Yahoo IM channelId。

    格式：yahoo-bid-logbot1:y{seller}:y{buyer}
    """
    s = shop_id.lower().lstrip("y")
    b = buyer_id.lower().lstrip("y")
    return f"yahoo-bid-logbot1:y{s}:y{b}"


def _extract_receiver(channel_id: str, my_id: str) -> str:
    """从 channelId 提取对方的 Y-ID（receiver）。"""
    my_num = my_id.lower().lstrip("y")
    for part in channel_id.split(":"):
        m = re.match(r'^y(\d{5,})$', part, re.IGNORECASE)
        if m and m.group(1) != my_num:
            return f"Y{m.group(1)}"
    return ""


# ── HTTP 会话构建 ─────────────────────────────────────


def _build_session(profile_dir, buyer_cid: str = "", max_age: float = DEFAULT_MAX_AGE):
    """从 cookie_cache 构建 HTTP 会话。

    优先使用 raw_cookies（保留原始 domain/path），与浏览器行为一致。
    fallback 到 flat cookies（全部设为 .yahoo.com）。

    v6.1:cookie cache 過期/不存在時,自動 fallback 從 Chrome SQLite 強讀(Yahoo cookie 有效期 1 年)
    返回 (session, wssid, error_msg)。
    session 为 None 表示失败。
    """
    profile_dir = Path(profile_dir)
    cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
    if not cookies:
        # v6.1 修復:cache 過期 → 從 Chrome SQLite 強讀(不依賴 mtime 比較)
        # 避免「cache 24h 過期 + Chrome SQLite mtime 沒變 → 永遠需登入」死鎖
        try:
            flat, raw = load_from_chrome_sqlite_yahoo(profile_dir)
            if flat and len(flat) >= 5:
                save_cookie_cache(profile_dir, flat, "", raw_cookies=raw)
                # 重新 load(saved_at 已 refresh)
                cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
        except Exception:
            pass
        if not cookies:
            return None, "", "cookie cache 不存在或已过期"

    # wssid 为空时，用 HTTP 从 Yahoo 首页补提取（monitor 在 myauc 页面无法提取 wssid）
    if not wssid:
        log.info("[IM-HTTP] wssid 为空，HTTP 补提取...")
        wssid = _fetch_wssid_http(cookies, proxy=_detect_system_proxy())
        if wssid:
            save_cookie_cache(profile_dir, cookies, wssid)
            log.info("[IM-HTTP] wssid 补提取成功: %s...", wssid[:8])
            # v6.1.6:成功補拉 → 清除「需重登」flag(可能之前 cookies 缺失被標記)
            try:
                from .needs_relogin import clear_needs_relogin
                clear_needs_relogin(profile_dir)
            except Exception:
                pass
        else:
            # v6.1.4:cache 內 cookies 可能不全 → 從 Chrome SQLite 強讀更完整 cookies 重試
            log.warning("[IM-HTTP] wssid 补提取失败,試 SQLite fallback 重抓 cookies...")
            try:
                flat_sql, raw_sql = load_from_chrome_sqlite_yahoo(profile_dir)
                if flat_sql and len(flat_sql) > len(cookies):
                    log.info("[IM-HTTP] SQLite cookies={} > cache cookies={} 用 SQLite 重試".format(
                        len(flat_sql), len(cookies)))
                    wssid = _fetch_wssid_http(flat_sql, proxy=_detect_system_proxy())
                    if wssid:
                        # 寫回 cache(SQLite 拿到的更完整版本 + 新 wssid)
                        save_cookie_cache(profile_dir, flat_sql, wssid, raw_cookies=raw_sql)
                        cookies = flat_sql
                        log.info("[IM-HTTP] SQLite 重試成功: wssid=%s...", wssid[:8])
                        try:
                            from .needs_relogin import clear_needs_relogin
                            clear_needs_relogin(profile_dir)
                        except Exception:
                            pass
                    else:
                        log.warning("[IM-HTTP] SQLite 重試也失敗")
            except Exception as _e_sql:
                log.warning("[IM-HTTP] SQLite fallback 異常: %s", _e_sql)
            if not wssid:
                # v6.1.6:Yahoo 把該帳號當未登入(常見:缺 B cookie / 登入態過期)
                # 自動標記「需重登」,GUI 帳號列表會顯示提示,使用者一眼看到要重登哪些
                account_name = profile_dir.name
                try:
                    from .needs_relogin import mark_needs_relogin
                    is_new = mark_needs_relogin(
                        profile_dir,
                        reason="cookies 不全或登入態過期(Yahoo 跳轉到 login)"
                    )
                    if is_new:
                        log.warning(
                            "[IM-HTTP] ⚠️ %s 需重新登入該帳號 Chrome(cookies 缺 B 或登入態過期)",
                            account_name,
                        )
                except Exception:
                    pass
                return None, "", f"wssid 提取失败({account_name} 需重登)"

    referer = _im_referer(buyer_cid)

    kw = dict(impersonate=CURL_CFFI_IMPERSONATE)
    proxy = _detect_system_proxy()
    if proxy:
        kw["proxy"] = proxy
    s = CffiSession(**kw)
    s.headers.update(_IM_HEADERS)
    s.headers["Referer"] = referer

    # 优先使用 raw_cookies（保留 domain/path，与浏览器完全一致）
    raw = load_raw_cookies(profile_dir, max_age=max_age)
    if raw:
        for c in raw:
            domain = c.get("domain", ".yahoo.com")
            s.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    else:
        for k, v in cookies.items():
            s.cookies.set(k, v, domain=".yahoo.com")

    return s, wssid, ""


# ── 读取消息 ──────────────────────────────────────────


# IM 元数据日志去重（hook C）— 模块级 in-memory set，避免重复 append 同一条消息
_LOGGED_IM_KEYS: set = set()
_LOGGED_IM_CAP = 100000


def _maybe_log_im_metadata(account_name: str, channel_id: str, shop_id: str,
                            messages: list) -> None:
    """从 messages 列表里挑没记过的，append metadata（无文本内容，PII safe）。
    settings.json 的 enable_im_metadata_log 默认 True；False 时跳过。
    任何异常都吞掉，不影响主流程。
    """
    try:
        # 检查设置开关
        try:
            from core.accounts import load_settings
            if not load_settings().get("enable_im_metadata_log", True):
                return
        except Exception:
            pass
        from core.runtime_hooks import log_im_metadata
        shop_lower = (shop_id or "").lower()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            mid = str(msg.get("messageId") or msg.get("id") or msg.get("createdTs") or "")
            if not mid:
                continue
            key = (channel_id, mid)
            if key in _LOGGED_IM_KEYS:
                continue
            _LOGGED_IM_KEYS.add(key)
            if len(_LOGGED_IM_KEYS) > _LOGGED_IM_CAP:
                _LOGGED_IM_KEYS.clear()  # 简单容量保护
            sender = str(msg.get("sender") or msg.get("senderEcid") or "")
            is_seller = bool(shop_lower and shop_lower in sender.lower())
            value = msg.get("value")
            if isinstance(value, dict):
                text = value.get("content") or ""
            else:
                text = msg.get("text") or msg.get("content") or msg.get("body") or ""
            log_im_metadata(
                account=account_name or "",
                channel_id=channel_id,
                sender_id=sender[:20],
                has_gpt_draft=False,  # im_read 上下文不知道是否有 GPT draft
                replied=is_seller,    # 卖家发的 = 已回复
                msg_len=len(str(text or "")),
            )
    except Exception:
        pass


def im_read_messages(
    profile_dir,
    channel_id: str,
    shop_id: str = "",
    limit: int = 30,
    on_log: Optional[LogFn] = None,
    account_name: str = "",  # hook C：用于 runtime/im_metadata.jsonl 写盘（可选）
) -> str:
    """纯 HTTP 读取 IM 消息，返回 【买家】/【卖家】 格式文本。

    不调用 putReadInfo → 红点自然保持。

    Args:
        profile_dir: 账号 profile 目录
        channel_id:  yahoo-bid-logbot1:y{seller}:y{buyer}
        shop_id:     卖家 Y-ID（如 Y9000000003），用于区分买卖方
        limit:       最多读取消息数
        on_log:      日志回调

    Returns:
        格式化的消息文本，空字符串表示失败。
    """
    if on_log is None:
        on_log = lambda *_: None

    # 从 channel_id 提取 buyer_cid 用于 Referer
    buyer_cid = _extract_receiver(channel_id, shop_id) if shop_id else ""

    session, wssid, err = _build_session(profile_dir, buyer_cid=buyer_cid)
    if not session:
        on_log(f"[IM-HTTP] read failed: {err}")
        return ""

    try:
        # 使用缓存的正确方向（如果有），否则先试原始再反转
        cached = _CHANNEL_DIR_CACHE.get(channel_id)
        if cached:
            ids_to_try = [cached]
            rev = _reverse_channel_id(cached)
            if rev != cached:
                ids_to_try.append(rev)
        else:
            ids_to_try = [channel_id]
            rev = _reverse_channel_id(channel_id)
            if rev != channel_id:
                ids_to_try.append(rev)

        for attempt_id in ids_to_try:
            url = (
                f"{_IM_API_BASE}/messages"
                f"?property=auction2"
                f"&channelId={quote(attempt_id)}"
                f"&sortBy=-createdTs"
                f"&limit={limit}"
            )
            if attempt_id != ids_to_try[0]:
                on_log(f"[IM-HTTP] trying reversed channelId: {attempt_id}")

            try:
                resp = session.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    messages = data.get("messages", [])
                    total = data.get("pagination", {}).get("resultsTotal", len(messages))
                    if messages:
                        on_log(f"[IM-HTTP] read OK: {total} messages from {attempt_id}")
                        # 缓存正确的 channelId 方向
                        _CHANNEL_DIR_CACHE[channel_id] = attempt_id
                        # hook C：写盘 IM metadata（在 _parse_messages 之前抓原始 dict）
                        _maybe_log_im_metadata(account_name, attempt_id, shop_id, messages)
                        text = _parse_messages(data, shop_id=shop_id, on_log=on_log)
                        if text:
                            return text
                    elif total == 0:
                        on_log(f"[IM-HTTP] channel empty: {attempt_id}")
                        if attempt_id == ids_to_try[0] and len(ids_to_try) > 1:
                            continue  # 尝试反序
                        return ""
                elif resp.status_code in (401, 403):
                    on_log(f"[IM-HTTP] auth expired ({resp.status_code})")
                    invalidate_cookie_cache(profile_dir)
                    return ""
                else:
                    on_log(f"[IM-HTTP] read error: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                on_log(f"[IM-HTTP] read exception: {e}")

        return ""
    finally:
        try:
            session.close()
        except Exception:
            pass


def _reverse_channel_id(channel_id: str) -> str:
    """反转 channelId 中买卖方顺序。"""
    parts = channel_id.split(":")
    if len(parts) == 3:
        return f"{parts[0]}:{parts[2]}:{parts[1]}"
    return channel_id


# ── 发送消息 ──────────────────────────────────────────


def im_prime_order_channel(
    profile_dir,
    order_id: str,
    *,
    on_log=None,
) -> Tuple[bool, str]:
    """v6.1.33:對「主動發起對話」場景,GET /fe/api/im/orders?orderId=X 預建 channel.

    适配 Yahoo 賣家進入 chat URL 時的呼叫順序:
      1. GET /fe/api/im/orders?orderId={oid}   ← Yahoo server 用 buyer + orderId 自動建 channel
      2. BOSH channel_user_active(set isActive=true) ← 標記 channel 為「進入」
      3. BOSH send_message text + send_message order ← 發送

    沒第 1 步直接走 BOSH 對全新 channel:
      - channel_user_active 會 fail(channel 不存在)
      - send_message 看起來成功但 server silent drop

    Args:
        profile_dir: Chrome profile 目錄(取 cookie)
        order_id: Yahoo 訂單號

    Returns:
        (success, info_or_error)
    """
    if not order_id:
        return False, "order_id 空"
    _log = on_log or (lambda *_: None)
    session, _wssid, err = _build_session(profile_dir)
    if err or session is None:
        return False, f"build session fail: {err}"
    try:
        url = f"{_IM_API_BASE}/orders?orderId={order_id}"
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            _log(f"[IM-HTTP] prime_order_channel OK order={order_id}")
            return True, f"orders API 200, len={len(r.text)}"
        return False, f"orders API {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, f"orders API 異常: {e}"


def im_send_message(
    profile_dir,
    channel_id: str,
    receiver: str,
    message: str,
    buyer_cid: str = "",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """纯 HTTP 发送 IM 消息。

    Args:
        profile_dir: 账号 profile 目录
        channel_id:  yahoo-bid-logbot1:y{seller}:y{buyer}
        receiver:    对方 Y-ID（如 Y9000000002）
        message:     消息内容
        buyer_cid:   买家 CID（用于 Referer）
        on_log:      日志回调

    Returns:
        (成功, 信息描述)
    """
    if on_log is None:
        on_log = lambda *_: None

    if not message:
        return False, "消息为空"

    # receiver 格式统一
    if receiver and not receiver.startswith("Y"):
        receiver = f"Y{receiver.lstrip('y')}"

    session, wssid, err = _build_session(
        profile_dir, buyer_cid=buyer_cid or receiver
    )
    if not session:
        return False, f"cookie cache 不可用: {err}"

    try:
        # 使用缓存的正确 channelId 方向
        effective_channel = _CHANNEL_DIR_CACHE.get(channel_id, channel_id)
        payload = {
            "onlyValidate": False,
            "channelId": effective_channel,
            "type": "text",
            "value": {"content": message},
            "receiver": receiver,
            "wssid": wssid,
        }

        url = f"{_IM_API_BASE}/message/send"

        # v6.1.19:curl_cffi TLS lib bug retry — 內層自動 retry 解決偶發 IM send 失敗
        from .ssl_helper import cffi_retry_call as _cffi_retry_im
        for attempt in range(3):
            try:
                resp = _cffi_retry_im(
                    session.post, url, json=payload, timeout=15,
                    max_retries=2,
                    on_retry=lambda att, e: on_log(f"[IM-HTTP] TLS lib bug retry #{att}"),
                )

                if resp.status_code == 200:
                    body = resp.json() if resp.text.strip() else {}
                    msg_id = body.get("validation", {}).get("messageId", "")
                    on_log(f"[IM-HTTP] send OK: msgId={msg_id}")
                    # 缓存正确的 channelId 方向
                    _CHANNEL_DIR_CACHE[channel_id] = payload["channelId"]
                    # v6.0.83:send 成功後自動 mark_read(紅點消 + 對方看到我已讀)
                    # 攔截已讀機制:平時拉訊息(GET /fe/api/im/messages)不發已讀回執
                    # 只有真的 send 訊息回去時才主動 mark_read,讓對方看到「已讀」與我們的 reply 同時發生
                    try:
                        from .im_bosh_ops import bosh_mark_read
                        # background thread,不阻塞 send 返回
                        import threading as _th
                        _th.Thread(
                            target=lambda: bosh_mark_read(profile_dir, payload["channelId"], on_log=on_log),
                            daemon=True,
                        ).start()
                    except Exception as _e_mr:
                        on_log(f"[IM-HTTP] auto mark_read schedule 失敗(不阻塞): {_e_mr}")
                    return True, f"消息已发送(HTTP) msgId={msg_id}"

                if resp.status_code == 429:
                    if attempt < 2:
                        wait = 3.0 + attempt * 3.0 + human_jitter_ms(500) / 1000.0
                        on_log(f"[IM-HTTP] 429, wait {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    return False, "429 Too Many Requests"

                if resp.status_code in (401, 403):
                    # wssid 可能过期 — 尝试刷新一次再重试
                    if attempt == 0:
                        on_log(f"[IM-HTTP] send {resp.status_code}, 尝试刷新 wssid...")
                        new_wssid = _fetch_wssid_http(
                            _yahoo_cookies_dict(session),
                            proxy=_detect_system_proxy(),
                        )
                        if new_wssid and new_wssid != wssid:
                            on_log(f"[IM-HTTP] wssid 刷新成功: {new_wssid[:8]}...")
                            wssid = new_wssid
                            payload["wssid"] = new_wssid
                            save_cookie_cache(profile_dir,
                                              _yahoo_cookies_dict(session),
                                              new_wssid)
                            continue
                    invalidate_cookie_cache(profile_dir)
                    return False, f"auth expired ({resp.status_code})"

                # 500 或其他错误
                err_detail = ""
                try:
                    err_body = resp.json()
                    errors = err_body.get("errors", [])
                    if errors:
                        err_detail = errors[0].get("message", "") or errors[0].get("detail", "")
                except Exception:
                    err_detail = resp.text[:200]

                # channelId 方向错误时尝试反序
                _err_str = str(err_detail)
                if ("Wrong parameters" in _err_str or "Channel not found" in _err_str or resp.status_code == 404) and attempt == 0:
                    alt_id = _reverse_channel_id(payload["channelId"])
                    if alt_id != payload["channelId"]:
                        on_log(f"[IM-HTTP] wrong channelId, trying reversed: {alt_id}")
                        payload["channelId"] = alt_id
                        # receiver 保持不变（对方 ID 不因 channelId 顺序而改变）
                        continue

                # v6.1.53:Yahoo 後端 5xx / UDB validation 短暫故障 → retry 一次(等 5s)
                # 跟 publish_http_ops 的 503 UDB retry 邏輯對齊
                # 修「自動發送失敗:HTTP 503: UDB validation unavailable」用戶體驗
                _is_gateway_err = (
                    resp.status_code in (502, 503, 504)
                    or "UDB validation" in _err_str
                    or "Gateway" in _err_str
                    or "BadGateway" in _err_str
                )
                if _is_gateway_err and attempt < 2:
                    _wait = 5.0 + attempt * 5.0  # 5s, 10s
                    on_log(f"[IM-HTTP] Yahoo 網關錯誤 {resp.status_code}({_err_str[:60]}),"
                           f"{_wait:.0f}s 後重試一次")
                    time.sleep(_wait)
                    continue

                return False, f"HTTP {resp.status_code}: {err_detail}"

            except Exception as e:
                on_log(f"[IM-HTTP] send exception: {e}")
                if attempt < 2:
                    time.sleep(1)
                    continue
                return False, str(e)[:200]

        return False, "重试耗尽"
    finally:
        try:
            session.close()
        except Exception:
            pass


# ── 消红点 ────────────────────────────────────────────


def im_mark_read(
    profile_dir,
    channel_id: str,
    buyer_cid: str = "",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """标记频道为已读（消红点）。

    策略：BOSH 优先 → REST API 兜底
    1. BOSH: 通过 XMPP channel_user_active + queryMessage 真正消红点
    2. REST: putLastAccessedTs + putReadInfo（辅助，不一定清除红点）

    回傳訊息規約(2026-04-30 v6.0.49):
    - BOSH 成功:return (True, <bosh_info>)
    - BOSH 失敗 + REST 成功:return (True, "rest_fallback_no_guarantee")
      → daemon 看到此訊息可選擇再呼叫一次(BOSH 通常下一次會成功)
    - 兩條都失敗:return (False, <rest_err>)
    """
    if on_log is None:
        on_log = lambda *_: None

    # ── BOSH 首选（真正消红点） ──
    try:
        from .im_bosh_ops import bosh_mark_read as _bosh_mark_read
        ok, info = _bosh_mark_read(profile_dir, channel_id, on_log=on_log)
        if ok:
            return True, info
        on_log(f"[IM-HTTP] BOSH 消红点失败({info})，降级到 REST API")
    except Exception as e:
        on_log(f"[IM-HTTP] BOSH 消红点异常({e})，降级到 REST API")

    # ── REST API 兜底 ──
    rest_ok, rest_info = _im_mark_read_rest(profile_dir, channel_id, buyer_cid, on_log)
    if rest_ok:
        # 標註是 fallback 路徑(REST 不一定真清紅點)
        # daemon 看到 "rest_fallback_no_guarantee" 可選擇 retry,GUI 內部 caller 只看 ok 不影響
        return True, "rest_fallback_no_guarantee"
    return False, rest_info


def _im_mark_read_rest(
    profile_dir,
    channel_id: str,
    buyer_cid: str = "",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """REST API 标记频道为已读（putReadInfo + putLastAccessedTs）。

    注意：REST API 不一定能清除红点，仅作为 BOSH 的兜底。
    """
    if on_log is None:
        on_log = lambda *_: None

    session, wssid, err = _build_session(
        profile_dir, buyer_cid=buyer_cid
    )
    if not session:
        return False, f"cookie cache 不可用: {err}"

    # Referer 设置为 /chat?qType=text（与浏览器 SPA 完全一致）
    session.headers["Referer"] = "https://tw.bid.yahoo.com/chat?qType=text"

    try:
        any_ok = False

        # ── 1. putLastAccessedTs（全局时间戳，浏览器先调这个） ──
        try:
            url_ts = f"{_IM_API_BASE}/putLastAccessedTs?from=messageHome"
            resp_ts = session.put(url_ts, json={"wssid": wssid}, timeout=10)
            body_ts = ""
            try:
                body_ts = resp_ts.text[:300]
            except Exception:
                pass
            if resp_ts.status_code == 200:
                on_log(f"[IM-HTTP] putLastAccessedTs OK: {body_ts}")
                any_ok = True
            else:
                on_log(f"[IM-HTTP] putLastAccessedTs failed: {resp_ts.status_code} {body_ts}")
        except Exception as e:
            on_log(f"[IM-HTTP] putLastAccessedTs error: {e}")

        # ── 2. putReadInfo（频道级已读，两个方向都调用） ──
        url_read = f"{_IM_API_BASE}/putReadInfo"
        reversed_id = _reverse_channel_id(channel_id)
        ids = [channel_id]
        if reversed_id != channel_id:
            ids.append(reversed_id)

        for cid in ids:
            try:
                payload = {"property": "auction2", "channelId": cid, "wssid": wssid}
                resp = session.put(url_read, json=payload, timeout=10)
                body_rd = ""
                try:
                    body_rd = resp.text[:300]
                except Exception:
                    pass
                if resp.status_code == 200:
                    on_log(f"[IM-HTTP] putReadInfo OK: {cid} resp={body_rd}")
                    any_ok = True
                else:
                    on_log(f"[IM-HTTP] putReadInfo fail: {cid} {resp.status_code} {body_rd}")
            except Exception:
                pass

        return (True, "已消红点") if any_ok else (False, "mark read failed")
    except Exception as e:
        return False, str(e)[:200]
    finally:
        try:
            session.close()
        except Exception:
            pass


# ── 消息解析（复用 yahoo_im_fulltext 的逻辑） ─────────


def _parse_messages(data: dict, shop_id: str = "", on_log: Optional[LogFn] = None) -> str:
    """解析 Yahoo IM API 返回的消息数据。

    与 yahoo_im_fulltext._parse_im_api_messages 逻辑一致。
    """
    if on_log is None:
        on_log = lambda *_: None

    messages = []
    if isinstance(data, dict):
        msgs = data.get("messages")
        if msgs is None:
            msgs = data.get("data") or data.get("result") or []
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("items") or msgs.get("list") or []
        messages = msgs if isinstance(msgs, list) else []

    if not messages:
        return ""

    shop_lower = shop_id.lower().lstrip("y") if shop_id else ""

    # API 返回新→旧，反转为时间正序
    messages = list(reversed(messages))

    lines = []
    for msg in messages[-50:]:
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type", "")
        if msg_type == "sticker":
            continue
        if msg_type == "recalled" or msg.get("recalled"):
            continue

        sender = str(msg.get("sender") or msg.get("senderEcid") or "").lower()
        is_seller = bool(shop_lower and shop_lower in sender)
        prefix = "【卖家】" if is_seller else "【买家】"

        value = msg.get("value")
        if isinstance(value, dict):
            text = value.get("content") or ""

            if msg_type == "listing":
                item_id = value.get("id") or ""
                title = value.get("title") or ""
                price = value.get("price") or ""
                listing_line = ""
                if item_id:
                    listing_line = f"https://tw.bid.yahoo.com/item/{item_id}"
                if title:
                    listing_line += f" {title}"
                if price:
                    listing_line += f" ${price}"
                if listing_line:
                    lines.append(f"{prefix}{listing_line.strip()}")
                continue

            if text:
                text = str(text).strip()
                if text and text.lower() != "recalled":
                    lines.append(f"{prefix}{text}")
        else:
            text = msg.get("text") or msg.get("content") or msg.get("body") or ""
            if text:
                text = str(text).strip()
                if text and text.lower() != "recalled":
                    lines.append(f"{prefix}{text}")

    return "\n".join(lines)
