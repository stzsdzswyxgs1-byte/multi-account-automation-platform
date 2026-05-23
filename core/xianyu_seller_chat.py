"""閒魚賣家自動問答 — 純 WebSocket + HTTP 路徑(v6.0.78)

v6.0.78 後純 HTTP+WS,不再依賴 Playwright/瀏覽器:
- WS 連線:goofish_ws_client (wss-goofish.dingtalk.com)
- HTTP mtop API:xianyu_im_http (session.sync / token refresh / detail.unit)
- Cookie 同步:goofish_cookie_store (從 Chrome SQLite 抓 cookie)

公開入口:
- ask_xianyu_seller_via_ws — 純 WS 發送提問給賣家
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import urlparse, urlencode, parse_qs


import opencc as _opencc_mod
_T2S_CONVERTER = _opencc_mod.OpenCC("t2s")


def _to_simplified(text: str) -> str:
    """繁体转简体，使用 opencc 确保完整覆盖所有繁体字。"""
    return _T2S_CONVERTER.convert(text)


SELLER_REPLY_TIMEOUT = 300  # 等待卖家回复的最长时间(5分钟,給上層 HTTP 監控用)


def _clean_chat_url(url: str) -> str:
    """去掉 spm 等追踪参数，只保留 itemId 和 peerUserId。"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    keep = {}
    for key in ("itemId", "peerUserId"):
        if key in qs:
            keep[key] = qs[key][0]
    if not keep:
        return url  # 无法解析，原样返回
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(keep)}"
    return clean


@dataclass
class XianyuChatResult:
    success: bool
    seller_reply: str = ""
    error: str = ""
    need_login: bool = False
    timed_out: bool = False
    # v6.1.45:這幾個欄位之前誤放在 _ensure_ws_ready 函式內 unreachable code,
    # 導致 success 路徑構造 XianyuChatResult(chat_url=..., ...) 時 TypeError。
    # 修「auto_ask_fallback bug 修了後 WS 真的能送出,但 result 構造 crash」bug
    chat_url: str = ""             # 聊天頁 URL,用於後續檢查
    msg_count_after_send: int = 0  # 發送後的消息數,用於後續比對
    sent_question: str = ""        # 實際發送的問題文本(簡體)
    peer_user_id: str = ""         # WS 路徑已知的 peerUserId (避免 caller 再從商品頁 URL 解析)
    session_id: str = ""           # WS 路徑已知的 sessionId


# v6.1.43:智能判斷 WS 連線失敗是不是真的「需要登入」
# 修「閒魚 cookie + token 都在,WS 偶發失敗(網路/RGV587/server)被誤判需要登入」bug
# 之前 WS start 失敗一律標 need_login=True → 用戶看到「需要登入」誤導
# 實際上 cookie 還有效,只是 WS 暫時失敗,該重試或標 transient 錯誤
_LOGIN_ERROR_KEYWORDS = (
    "access_token 取得失敗",
    "access_token",
    "deviceId 缺失",
    "需要登入",
    "需要登錄",
    "未登入",
    "未登錄",
    "未登录",
    "login required",
    "auth_failed",
    "請去採購頁",  # goofish_token_fetcher 明確指示重登
)


def _is_login_error(err_detail: str) -> bool:
    """err_detail 含登入相關字眼才算真的需要登入,否則視為 transient(網路/RGV587/server)。"""
    if not err_detail:
        return False
    return any(kw in err_detail for kw in _LOGIN_ERROR_KEYWORDS)


def _ensure_ws_ready_with_token_retry(ws, _log_fn) -> tuple:
    """v6.1.43:嘗試啟動 WS,首次失敗如果是 401/token 問題,等背景 auto_refresh 60s 後重試。

    解決「token cache 24h 內但 server invalidate → 401 → 主循環 auto_refresh 需 ~30s
    但 ws.start(30s) timeout 提前返回 → 用戶看到誤判失敗」bug。

    Returns: (ok: bool, last_err: str)
    """
    # 第 1 次:30s timeout(常見成功 case)
    first_ok = ws.start(wait_ready=True, timeout=30)
    if first_ok:
        return True, ""
    first_err = getattr(ws, "last_connect_error", "") or ""
    # 401 / token 問題 → 主循環在 auto_refresh,等 60s 給時間
    _is_token_issue = (
        "401" in first_err or "token" in first_err.lower() or
        getattr(ws, "_token_invalid", False)
    )
    if not _is_token_issue:
        return False, first_err
    _log_fn(f"[XY-WS] 首次連線失敗(疑似 token 401: {first_err[:80]}),等背景 auto_refresh + 重連 60s...")
    second_ok = ws.start(wait_ready=True, timeout=60)
    if second_ok:
        _log_fn("[XY-WS] [OK] 背景 auto_refresh 完成,重連成功")
        return True, ""
    return False, getattr(ws, "last_connect_error", "") or first_err


def ask_xianyu_seller_via_ws(
    *,
    goofish_url: str,
    question: str,
    image_urls: Optional[List[str]] = None,
    on_log: Optional[Callable] = None,
    on_captcha: Optional[Callable] = None,
) -> XianyuChatResult:
    """純 WebSocket 發送賣家問題(不開瀏覽器,v6.0.78 移除所有 Playwright fallback)。

    流程:
      1. 從 goofish_url 提取 peerUserId(或用 item_id 反查 sellerUserId)
      2. HTTP session.sync 用 peerUserId 反查 sessionId
      3. v6.1.54:若有 image_urls,先把買家圖中轉給賣家(讓賣家看實物)
      4. WS send_text_sync 發送提問文字(走長連接,500ms 內完成)
      5. 返回 XianyuChatResult(含 chat_url 給後續 HTTP 監控用)

    image_urls: 買家在 Yahoo IM 發給賣家的圖片 URL list(最多 3 張,避免刷屏)
    """
    from core.purchase_feature import PURCHASE_PROFILE_DIR
    from core.xianyu_im_http import (
        list_sessions, find_session_by_peer, extract_peer_user_id,
        extract_item_id, fetch_seller_uid_by_item_id,
    )
    from core.goofish_ws_client import XianyuWsClient
    # _to_simplified 是本檔案的 module-level function

    def _log(m):
        if on_log:
            on_log(m)

    # 闲鱼必须用简体中文
    question_sc = _to_simplified(question)

    # 1. 取 peerUserId
    # URL 可能是商品頁 /item?id=X (沒 peerUserId) 或對話頁 /im?peerUserId=Y
    peer_id = extract_peer_user_id(goofish_url)
    if not peer_id:
        # 是商品頁 URL → 用 itemId 反查 sellerUserId
        item_id = extract_item_id(goofish_url)
        if not item_id:
            return XianyuChatResult(success=False,
                                    error=f"无法从 URL 提取 itemId/peerUserId: {goofish_url[:80]}")
        _log(f"[XY-WS] URL 無 peerUserId,用 itemId={item_id} 反查 sellerUserId...")
        peer_id, err = fetch_seller_uid_by_item_id(PURCHASE_PROFILE_DIR, item_id, on_log=on_log)
        if err or not peer_id:
            return XianyuChatResult(success=False,
                                    error=f"detail API 反查 sellerUserId 失敗: {err}",
                                    need_login=("TOKEN" in (err or "") or "ILLEGAL" in (err or "")))
        _log(f"[XY-WS] 反查到 peerUserId={peer_id}")

    # 2. 反查 sessionId
    session_id = ""

    # 2a. 先看 (peer, item) 緩存映射
    item_id_for_cache = extract_item_id(goofish_url) or ""
    if item_id_for_cache:
        try:
            from core.xianyu_im_http import load_session_mapping
            session_id = load_session_mapping(PURCHASE_PROFILE_DIR, peer_id, item_id_for_cache)
            if session_id:
                _log(f"[XY-WS] 用緩存 (peer+item) → sessionId={session_id}")
        except Exception:
            pass

    # 2b. 試 session.sync 拿(已聊過的對話)
    # v6.0.78:純 HTTP/WS 路徑,不再 fallback Playwright
    # 核心修復:_safe_get_cookie 解決 _m_h5_tk 跨域歧義問題,token refresh 真正生效
    # → list_sessions 在絕大多數場景能直接 SUCCESS,不再需要 Playwright lite 救場
    if not session_id:
        _log(f"[XY-WS] 查 session.sync for peer={peer_id}...")
        sessions, err = list_sessions(PURCHASE_PROFILE_DIR, fetch_num=500, on_log=on_log)
        if err:
            return XianyuChatResult(success=False, error=f"session.sync 失敗: {err}",
                                    need_login="TOKEN" in err or "ILLEGAL" in err)
        target = find_session_by_peer(sessions, peer_id)
        if target:
            session_id = str((target.get("session", {}) or {}).get("sessionId", ""))
            if session_id and item_id_for_cache:
                try:
                    from core.xianyu_im_http import save_session_mapping
                    save_session_mapping(PURCHASE_PROFILE_DIR, peer_id, item_id_for_cache, session_id)
                except Exception:
                    pass

    # 2c. v6.0.80:新對話 → 用 WS LWP /r/SingleChatConversation/create 建立 + 拿 sessionId
    # 純 WebSocket 路徑,完全不開瀏覽器(替代舊版 Playwright lite)
    # 靈感來自 fancyboi999/goofish-cli 的协议实现
    if not session_id and item_id_for_cache:
        _log(f"[XY-WS] 新對話 — 用 WS /r/SingleChatConversation/create 建立 session...")

        # 確保 WS 已連線
        ws_for_create = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=on_log or (lambda m: None))
        if not ws_for_create.is_connected():
            # v6.1.43:用 helper 嘗試啟動,首次 token 問題自動等 60s 重試
            _ok_create, _err_create = _ensure_ws_ready_with_token_retry(ws_for_create, _log)
            if not _ok_create:
                _err_msg = _err_create or "未知錯誤"
                return XianyuChatResult(
                    success=False,
                    error=f"WS 未連線,無法建立新對話 session:{_err_msg[:200]}",
                    need_login=_is_login_error(_err_msg),
                )

        new_sid, sid_err = ws_for_create.create_chat_sync(
            peer_user_id=peer_id,
            item_id=item_id_for_cache,
            timeout=15,
        )
        if new_sid:
            session_id = new_sid
            _log(f"[XY-WS] [OK] 新對話建立成功 sessionId={session_id}")
            # 寫入緩存,避免下次再建一次
            try:
                from core.xianyu_im_http import save_session_mapping
                save_session_mapping(PURCHASE_PROFILE_DIR, peer_id, item_id_for_cache, session_id)
            except Exception:
                pass
        elif sid_err in ("WAIT_VULCAN_TIMEOUT", "WAIT_HTTP_FALLBACK"):
            # ACK OK 但 5 種策略都抓不到 sessionId → HTTP session.sync 多次 retry
            # 閒魚 server 對「剛建立的新會話」有 5-10s 同步延遲,單次 sleep 2s 不夠
            # v6.0.81:retry 4 次,累計等 14 秒(2+3+4+5)
            _log(f"[XY-WS] ACK 沒帶 sessionId,fallback HTTP session.sync(最多 retry 4 次)...")
            try:
                import time as _t
                for attempt in range(4):
                    _t.sleep(2 + attempt)  # 2, 3, 4, 5 秒
                    sessions2, err2 = list_sessions(PURCHASE_PROFILE_DIR, fetch_num=500, on_log=on_log)
                    if err2:
                        _log(f"[XY-WS] retry {attempt+1}/4 list_sessions 失敗: {err2}")
                        continue
                    target = find_session_by_peer(sessions2, peer_id)
                    if target:
                        session_id = str((target.get("session", {}) or {}).get("sessionId", ""))
                        if session_id:
                            _log(f"[XY-WS] [OK] HTTP fallback 第 {attempt+1} 次 retry 拿到 sessionId={session_id}")
                            try:
                                from core.xianyu_im_http import save_session_mapping
                                save_session_mapping(PURCHASE_PROFILE_DIR, peer_id, item_id_for_cache, session_id)
                            except Exception:
                                pass
                            break
                    _log(f"[XY-WS] retry {attempt+1}/4: 還沒找到 session for peer={peer_id}({len(sessions2)} 個 session 中)")
            except Exception as _e_fb:
                _log(f"[XY-WS] HTTP fallback 異常: {_e_fb}")

        if not session_id:
            return XianyuChatResult(
                success=False,
                error=f"建立新對話失敗:{sid_err or '未知錯誤'} — 已 retry 4 次 HTTP fallback 仍找不到 sessionId,可能 server 同步延遲過長",
                need_login=False,
            )

    # 3. 啟動 WS (單例,首次連接會等握手完成)
    ws = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=on_log or (lambda m: None))
    if not ws.is_connected():
        # v6.0.79:修「軟件重啟後 access_token cache 被誤刪導致 RGV587 异常码」bug
        # 舊版每次 WS 未連線都強制刪 token cache,逼 ensure_access_token 走純 HTTP login.token,
        # 但 login.token 是 RGV587 重點監控目標,反覆呼叫會被限流擋
        # 修法:首次連線時優先用 cache(可能還有效,24h 內);只在 ws._token_invalid (上次 401) 才清
        _log("[XY-WS] WS 未連線,嘗試啟動 WS(優先使用 access_token cache)...")
        # v6.1.43:用 helper 嘗試啟動,首次 401/token 問題自動等 60s 給背景 auto_refresh 完成
        # 修「閒魚 cookie 還有效但 WS 401 → user 看到誤判需要登入」bug
        _ws_ok, _ws_err = _ensure_ws_ready_with_token_retry(ws, _log)
        if not _ws_ok:
            err_detail = _ws_err or "未知錯誤"
            return XianyuChatResult(
                success=False,
                error=f"閒魚 WS 連線失敗:{err_detail[:200]}",
                need_login=_is_login_error(err_detail),
            )

    # 4. v6.1.54:先中轉買家圖給賣家(若有)
    # 場景:買家在 Yahoo IM 發圖問「這個有貨嗎」,需要把同一張圖傳給閒魚賣家比對
    # 注意:cap 3 張避免刷屏;失敗不阻塞文字提問(降級到純文字)
    if image_urls:
        _log(f"[XY-WS] 開始中轉買家圖給賣家(共 {len(image_urls)} 張,最多送 3 張)...")
        import time as _t
        for idx, img_url in enumerate(image_urls[:3]):
            try:
                ok_img, info_img = ws.send_image_from_url_sync(
                    peer_id, session_id, img_url, timeout=30, download_timeout=20,
                )
                if ok_img:
                    _log(f"[XY-WS] [OK] 中轉買家圖 {idx+1}/{min(len(image_urls), 3)}: {img_url[:80]}")
                else:
                    _log(f"[XY-WS] 中轉買家圖 {idx+1} 失敗(不阻塞文字): {info_img[:120]}")
                _t.sleep(0.5)  # 段間避免限流
            except Exception as e:
                _log(f"[XY-WS] 中轉買家圖 {idx+1} 異常(不阻塞): {e}")

    # 5. 發送文字提問
    _log(f"[XY-WS] send_text: peer={peer_id} sid={session_id} text={question_sc[:60]!r}")
    ok, send_err = ws.send_text_sync(peer_id, session_id, question_sc, timeout=10)
    if not ok:
        return XianyuChatResult(success=False, error=f"WS send 失败: {send_err}")

    return XianyuChatResult(
        success=True,
        chat_url=goofish_url,         # 保留,給後續 HTTP 監控用
        msg_count_after_send=0,       # WS 模式不依賴這個
        sent_question=question_sc,
        peer_user_id=peer_id,         # caller 不用再從 chat_url 解析(商品頁 URL 沒這個欄位)
        session_id=session_id,        # caller 不用再查 baseline 拿 sessionId(已知)
    )


@dataclass
class XianyuCheckResult:
    has_reply: bool = False
    seller_reply: str = ""
    is_read: bool = False          # 消息是否已读(对方看了但没回)
    error: str = ""
    need_login: bool = False

