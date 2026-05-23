"""閒魚 IM 訊息 HTTP API — 純 HTTP 監控賣家回覆,不開瀏覽器。

v6.0.75 新增:替代原本的 Playwright 輪詢方式。
- 原版 _xianyu_check_inner:每 60s/300s 開一次 Chrome 開聊天頁抓 DOM
- 新版 check_seller_reply_http:純 HTTP POST mtop API,15s 輪詢一次都沒壓力

核心 API(從 Chrome DevTools Network 抓包確認):
- mtop.taobao.idlemessage.pc.session.sync  v3.0  → 拉所有對話列表 + 最新訊息摘要
- mtop.idle.trade.pc.message.headinfo      v1.0  → 查特定對話頭部(備用)

對話識別策略:
- session.sync 返回 sessions[].session.userInfo.userId == peerUserId (我們已有)
- 用 peerUserId 反查 sessionId,不需要事先存映射

監控算法(對比版本號 + 未讀數):
- 啟動時:baseline = (sessionId, version, ts)
- 輪詢時:if new_version > baseline_version and unread > 0 → 賣家回覆了
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

# 複用 goofish_api_check 的常量
APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
API_BASE = "https://h5api.m.goofish.com/h5"
IMPERSONATE = "chrome142"

# v6.0.75:session.sync 全局緩存,避免多 conv 並發時對同 API 高頻(限流保護)
# 並發 100 個 conv × 15s 輪詢 = 400 次/分鐘 → 10s 緩存後變 6 次/分鐘
_SESSION_SYNC_CACHE_TTL = 10  # 秒
_session_sync_cache_lock = threading.Lock()
_session_sync_cache: Dict[str, Any] = {
    "ts": 0.0,
    "sessions": [],
    "token": "",
    "fetch_num": 0,
}

# IM API 端點
API_SESSION_SYNC = "mtop.taobao.idlemessage.pc.session.sync"
API_HEADINFO = "mtop.idle.trade.pc.message.headinfo"
API_TOKEN_REFRESH = "mtop.taobao.idle.item.web.recommend.list"  # 通用 token 刷新接口
API_LOGIN_TOKEN = "mtop.taobao.idlemessage.pc.login.token"      # v6.0.75 WS 連線用 accessToken

# WebSocket 用的 app-key (不是 mtop 的 <XIANYU_APP_KEY_REDACTED>,WS 是另一個)
WS_APP_KEY = "<XIANYU_WS_APP_KEY_REDACTED>"

log = logging.getLogger(__name__)

LogFn = Optional[Callable[[str], None]]


# ── 簽名 / 時間戳 ──

def _sign(token: str, t: str, data_str: str) -> str:
    """閒魚 mtop sign 算法 (跟 goofish_api_check._sign 一致)。"""
    return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


# ── HTTP headers ──

_BASE_HEADERS = {
    # v6.0.75:對齊 goofish_order_http.HEADERS_BASE,確保跟採購 HTTP 同套參數
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/145.0.7632.160 Safari/537.36",
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ── 結果類型 ──

class XianyuHttpCheckResult:
    """check_seller_reply_http 的返回值。"""
    __slots__ = (
        "has_reply", "seller_reply", "is_read", "need_login",
        "error", "new_version", "new_ts", "session_id", "found",
        "is_platform_msg", "is_suspected_ai",  # v6.0.75 自動回覆過濾
    )

    def __init__(
        self,
        *,
        has_reply: bool = False,
        seller_reply: str = "",
        is_read: bool = False,
        need_login: bool = False,
        error: str = "",
        new_version: int = 0,
        new_ts: int = 0,
        session_id: str = "",
        found: bool = False,
        is_platform_msg: bool = False,
        is_suspected_ai: bool = False,
    ):
        self.has_reply = has_reply
        self.seller_reply = seller_reply
        self.is_read = is_read
        self.need_login = need_login
        self.error = error
        self.new_version = new_version
        self.new_ts = new_ts
        self.session_id = session_id
        self.found = found  # 是否在 session.sync 列表中找到了目标对话
        self.is_platform_msg = is_platform_msg  # 是否平台/系統訊息(應跳過)
        self.is_suspected_ai = is_suspected_ai  # 是否疑似賣家 AI 自動回覆


# ── Session 構建(curl_cffi + 閒魚 cookies) ──

def _build_session(profile_dir: Path, on_log: LogFn = None):
    """從 goofish_cookie_store 載入 cookies 並建立 curl_cffi Session。

    v6.0.75 對齊 goofish_order_http._build_goofish_session:
    - proxy="" 跳系統代理(對國內用戶無感,避免少數機器設了系統代理污染)
    - 過濾只保留 Alibaba 系 cookie(避免 Cookie header > 8KB 觸發 431)
    - 同個 cookie 跨域設到 .goofish.com 和 .taobao.com(login.token 跟 cna 等需要跨域)
    Returns: (session, token_hex, error_msg) — session 為 None 時 error_msg 說明原因
    """
    try:
        from curl_cffi.requests import Session
    except ImportError as e:
        return None, "", f"缺少 curl_cffi 库: {e}"

    try:
        from core.goofish_cookie_store import (
            load_goofish_cookies, load_goofish_raw_cookies, _is_goofish_domain,
        )
    except ImportError as e:
        return None, "", f"导入 goofish_cookie_store 失败: {e}"

    # v6.0.78:load_goofish_cookies 內部已自動做 SQLite 同步(cache 空/過期時)
    # 用戶確認瀏覽器中閒魚有登錄但程式報需登錄 → 由 load_goofish_cookies 自動恢復
    cookies_dict, token_hex, saved_at = load_goofish_cookies(profile_dir)
    if not cookies_dict:
        return None, "", "采购 Profile 没有闲鱼 cookie 缓存(请先在采购页登录闲鱼)"

    if not token_hex:
        return None, "", "_m_h5_tk token 缺失,需要重新登录闲鱼"

    # v6.0.75:過濾只保留 Alibaba 系 cookie(避免 Cookie header > 8KB 觸發 431,
    # 跟 goofish_order_http 同邏輯)
    raw_cookies = load_goofish_raw_cookies(profile_dir)
    if raw_cookies:
        ali_names = {c["name"] for c in raw_cookies
                     if c.get("name") and _is_goofish_domain(c.get("domain", ""))}
        if ali_names:
            cookies_dict = {k: v for k, v in cookies_dict.items() if k in ali_names}

    # v6.0.75:proxy="" 跳系統代理(無 VPN 時無感,跟採購 HTTP 同邏輯)
    sess = Session(impersonate=IMPERSONATE, proxy="")

    # v6.0.75:同 cookie 跨域同時設到 .goofish.com 和 .taobao.com,跟 goofish_order_http 同邏輯
    # (login.token / cna 等需要跨域 cookie 才能通過 server 校驗)
    _PUNISH_NAMES = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}
    _set = 0
    for name, value in cookies_dict.items():
        if not name or name in _PUNISH_NAMES:
            continue
        try:
            sess.cookies.set(name, value, domain=".goofish.com")
            sess.cookies.set(name, value, domain=".taobao.com")
            _set += 1
        except Exception:
            try:
                sess.cookies.set(name, value)
                _set += 1
            except Exception:
                pass

    if on_log:
        on_log(f"[XY-IM-HTTP] cookie 加载 {_set} 条(已過濾 Alibaba 系,跨 .goofish.com+.taobao.com), token={token_hex[:12]}...")

    return sess, token_hex, ""


def _refresh_token(sess, current_token: str, profile_dir: Path, on_log: LogFn = None) -> str:
    """token 過期時用通用接口刷新,返回新 token(失敗返回原 token)。"""
    t = _ts_ms()
    payload = json.dumps({"itemId": "0", "pageSize": 1, "pageNum": 1}, separators=(",", ":"))
    sign = _sign(current_token, t, payload)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
        "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
        "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
        "api": API_TOKEN_REFRESH,
    }
    try:
        r = sess.post(
            f"{API_BASE}/{API_TOKEN_REFRESH}/1.0/",
            params=params, data={"data": payload},
            headers=_BASE_HEADERS, timeout=15,
        )
        # v6.0.78:_m_h5_tk 同時存在 .goofish.com 和 .taobao.com(_build_session 跨域設置),
        # curl_cffi 的 cookies.get 不傳 domain 時拋 "Multiple cookies exist" 錯
        # 必須指定 domain 避開歧義
        new_tk = _safe_get_cookie(sess, "_m_h5_tk")
        if new_tk and "_" in new_tk:
            new_token = new_tk.split("_")[0]
            # 同步寫回 cookie cache
            try:
                from core.goofish_cookie_store import update_goofish_token
                new_enc = _safe_get_cookie(sess, "_m_h5_tk_enc") or ""
                update_goofish_token(profile_dir, new_tk, new_enc)
            except Exception:
                pass
            if on_log:
                on_log(f"[XY-IM-HTTP] token 已刷新: {new_token[:12]}...")
            return new_token
    except Exception as e:
        if on_log:
            on_log(f"[XY-IM-HTTP] token 刷新失败: {e}")
    return current_token


def _safe_get_cookie(sess, name: str) -> str:
    """v6.0.78:安全讀 cookie,處理「多 domain 同名」歧義。

    _build_session 跨域設置(.goofish.com + .taobao.com)後,curl_cffi.cookies.get(name)
    若不傳 domain 會拋 ValueError「Multiple cookies exist with name=...」。
    解法:依次嘗試 .goofish.com → .taobao.com → 無 domain,任一拿到就回傳。
    """
    for dom in (".goofish.com", ".taobao.com", None):
        try:
            kw = {"domain": dom} if dom else {}
            v = sess.cookies.get(name, **kw)
            if v:
                return str(v)
        except Exception:
            continue
    return ""


# ── 核心 API:session.sync ──

def session_sync(
    sess,
    token: str,
    *,
    fetch_num: int = 20,
    on_log: LogFn = None,
    bypass_cache: bool = False,
) -> Tuple[List[Dict], str]:
    """呼叫 mtop.taobao.idlemessage.pc.session.sync v3.0 拉所有對話列表。

    v6.0.75:加 10s 全局緩存,多 conv 並發共用結果(限流保護)。
    緩存命中條件:同 token + 同 fetch_num + 10s 內(用 bypass_cache=True 強制刷新)。

    Returns:
        (sessions_list, error_msg)
    """
    # 緩存命中?(限流保護:多 conv 並發時共用結果)
    if not bypass_cache:
        now_ts = time.time()
        with _session_sync_cache_lock:
            cache_age = now_ts - _session_sync_cache["ts"]
            if (cache_age < _SESSION_SYNC_CACHE_TTL
                and _session_sync_cache["token"] == token
                and _session_sync_cache["fetch_num"] == fetch_num
                and _session_sync_cache["sessions"]):
                if on_log:
                    on_log(f"[XY-IM-HTTP] session.sync 緩存命中({cache_age:.1f}s 前),{len(_session_sync_cache['sessions'])} 个对话")
                return list(_session_sync_cache["sessions"]), ""

    t = _ts_ms()
    data_str = json.dumps({"fetchNum": fetch_num}, separators=(",", ":"))
    sign = _sign(token, t, data_str)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "3.0",
        "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
        "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
        "api": API_SESSION_SYNC,
    }
    try:
        r = sess.post(
            f"{API_BASE}/{API_SESSION_SYNC}/3.0/",
            params=params, data={"data": data_str},
            headers=_BASE_HEADERS, timeout=20,
        )
        try:
            j = r.json()
        except Exception:
            return [], f"响应解析失败 status={r.status_code} body={r.text[:200]}"

        ret = j.get("ret", [])
        ret_str = " ".join(str(x) for x in ret)

        # v6.0.75:統一 token 失效識別
        # mtop 失效錯誤系列:
        # - FAIL_SYS_TOKEN_EXOIRED (mtop 拼字錯誤)
        # - FAIL_SYS_SESSION_EXPIRED (session 失效)
        # - 含 TOKEN/EXPIRED/EXOIRED 任一關鍵字都觸發 refresh
        # (ILLEGAL_ACCESS 是徹底失效需用戶重新登入,排在後面)
        if "ILLEGAL_ACCESS" in ret_str:
            return [], "ILLEGAL_ACCESS"
        if any(k in ret_str for k in ("EXOIRED", "EXPIRED", "TOKEN_EMPTY", "TOKEN_EXPIRED")):
            return [], "TOKEN_EXPIRED"
        if "SUCCESS" not in ret_str:
            return [], f"API 错误: {ret_str[:120]}"

        data = j.get("data", {}) or {}
        sessions = data.get("sessions", []) or []
        if on_log:
            on_log(f"[XY-IM-HTTP] session.sync 拿到 {len(sessions)} 个对话")

        # 寫入緩存(限流保護)
        with _session_sync_cache_lock:
            _session_sync_cache["ts"] = time.time()
            _session_sync_cache["sessions"] = list(sessions)
            _session_sync_cache["token"] = token
            _session_sync_cache["fetch_num"] = fetch_num
        return sessions, ""
    except Exception as e:
        return [], f"请求异常: {e}"


# ── 高階入口:自動處理 token 刷新 ──

def _try_sync_cookies_from_chrome_sqlite(profile_dir: Path, on_log: LogFn = None) -> bool:
    """v6.0.75:嘗試從 Chrome profile SQLite 強制重讀 cookie 寫入 cache。

    觸發場景:mtop API 返回 ILLEGAL_ACCESS 或 _refresh_token 失敗時,
    Python cache 內 cookie 可能舊了(Chrome 內部 cookie 已更新但 Python 沒同步)。

    前提:Chrome (採購監控 profile) 不在運行(否則 SQLite 文件被鎖)。
    Returns: True 表示有同步到新 cookie,可以 retry;False 表示沒同步。
    """
    try:
        from core.goofish_cookie_store import extract_cookies_from_profile
        ok = extract_cookies_from_profile(profile_dir, force=True)
        if on_log:
            on_log(f"[XY-IM-HTTP] cookie SQLite 同步: {'成功' if ok else '失敗'}(Chrome 可能在跑或未登入)")
        return bool(ok)
    except Exception as e:
        if on_log:
            on_log(f"[XY-IM-HTTP] cookie SQLite 同步異常: {e}")
        return False


def list_sessions(
    profile_dir: Path,
    *,
    fetch_num: int = 20,
    on_log: LogFn = None,
) -> Tuple[List[Dict], str]:
    """高階入口:自動 build_session + 處理 TOKEN_EXPIRED 刷新 + 重試一次。

    三層自動救援:
    1. session_sync 拿到 TOKEN_EXPIRED → _refresh_token (撈新 _m_h5_tk cookie)
    2. _refresh_token 失敗或 ILLEGAL_ACCESS → 從 Chrome SQLite 強制同步 cookie
    3. 仍失敗 → 返回 error 給上層通知用戶重新登入
    Returns: (sessions, error_msg)
    """
    sess, token, err = _build_session(profile_dir, on_log)
    if not sess:
        return [], err

    sessions, sync_err = session_sync(sess, token, fetch_num=fetch_num, on_log=on_log)

    # Layer 1:token 刷新
    if sync_err == "TOKEN_EXPIRED":
        if on_log:
            on_log("[XY-IM-HTTP] token/session 过期,自动刷新中...")
        new_token = _refresh_token(sess, token, profile_dir, on_log)
        if new_token != token:
            sessions, sync_err = session_sync(
                sess, new_token, fetch_num=fetch_num, on_log=on_log, bypass_cache=True,
            )
            token = new_token

    # Layer 2:仍 EXPIRED 或 ILLEGAL → 從 Chrome SQLite 強制同步 cookie 再試
    if sync_err in ("TOKEN_EXPIRED", "ILLEGAL_ACCESS"):
        if on_log:
            on_log(f"[XY-IM-HTTP] {sync_err} → 嘗試從 Chrome profile SQLite 同步最新 cookie")
        if _try_sync_cookies_from_chrome_sqlite(profile_dir, on_log):
            sess, token, err = _build_session(profile_dir, on_log)
            if sess:
                sessions, sync_err = session_sync(
                    sess, token, fetch_num=fetch_num, on_log=on_log, bypass_cache=True,
                )

    return sessions, sync_err


# ── peerUserId → sessionId 反查 ──

def find_session_by_peer(
    sessions: List[Dict],
    peer_user_id: str,
) -> Optional[Dict]:
    """從 session.sync 結果中找對方為 peer_user_id 的對話。

    閒魚 session.sync 返回結構:
    - 真實用戶對話: session.ownerInfo.userId = 對方賣家, session.userInfo.userId = 我自己
    - 系統消息: session.ownerInfo.userId = 我自己, session.userInfo.userId = 系統(9/100/200/...)

    為了相容兩種情況,先檢查 ownerInfo(真實對話用)→ 失敗 fallback userInfo(系統)。
    若同一賣家有多個對話(對應多個商品),返回 sortIndex 最大的(最近活躍)。
    """
    peer_user_id = str(peer_user_id or "").strip()
    if not peer_user_id:
        return None

    matched = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sess_obj = s.get("session", {}) or {}
        owner_info = sess_obj.get("ownerInfo", {}) or {}
        user_info = sess_obj.get("userInfo", {}) or {}
        owner_uid = str(owner_info.get("userId", ""))
        user_uid = str(user_info.get("userId", ""))
        # 對方 ID 可能在 ownerInfo (真實對話) 或 userInfo (系統消息)
        if owner_uid == peer_user_id or user_uid == peer_user_id:
            matched.append(s)

    if not matched:
        return None

    if len(matched) == 1:
        return matched[0]

    # 多個對話:選 sortIndex 最大
    def _sort_key(s):
        try:
            return int((s.get("message", {}) or {}).get("summary", {}).get("sortIndex", "0"))
        except Exception:
            return 0
    matched.sort(key=_sort_key, reverse=True)
    return matched[0]


def get_peer_user_id_from_session(session_obj: Dict, my_user_id: str = "") -> str:
    """從 session 結構抽出「對方 userId」(非 my_user_id 的那個)。

    Args:
        session_obj: session.sync 返回的單條對話的 'session' 子物件
        my_user_id: 我自己的 userId (用於排除)

    Returns: 對方 userId,沒找到時返回 ""
    """
    if not session_obj:
        return ""
    owner = (session_obj.get("ownerInfo") or {}).get("userId", "")
    user = (session_obj.get("userInfo") or {}).get("userId", "")
    owner = str(owner or "")
    user = str(user or "")
    my = str(my_user_id or "")
    # 兩邊都檢查,排除 my_user_id 的那個就是對方
    if owner and owner != my:
        return owner
    if user and user != my:
        return user
    return ""


# ── 自動回覆 / 系統消息過濾 ──

# 閒魚平台/系統消息提示詞(出現時跳過,不視為賣家真實回覆)
_PLATFORM_MARKERS = (
    "小主回来啦",       # 賣家回到頁面平台提示
    "AI回复已暂停",
    "AI回覆已暫停",
    "AI已为你",          # 平台 AI 自動回覆標記
    "AI已為你",
    "AI助手已",
    "[系统]",
    "[系統]",
    "[活动]",
    "[活動]",
    "客服小蜜",          # 阿里客服機器人
    "智能客服",
)

# 賣家 AI 自動回覆的可疑訊號(發出後不要立即觸發我方 AI,降低互轟風險)
_SELLER_AI_HEURISTIC_PHRASES = (
    "您好,我现在不在线",
    "您好,我現在不在線",
    "稍后回复您",
    "稍後回覆您",
    "我看到您的消息会尽快回复",
    "我看到您的消息會盡快回覆",
    "已收到您的消息",
    "已收到您的訊息",
    "稍等",  # 過短:可能誤判,只在內容只有此詞時才命中
)


def is_platform_message(summary_text: str, user_type: str = "0") -> bool:
    """v6.0.75:判斷一條訊息是否為閒魚平台/系統消息(應跳過,不視為真實賣家回覆)。

    判定條件:
    1. userInfo.type != "0" 一定是系統消息 (type=10 系統提示 / type=20 活動)
    2. summary 內容含平台標記詞 (_PLATFORM_MARKERS)
    """
    if str(user_type) != "0":
        return True
    if not summary_text:
        return False
    for marker in _PLATFORM_MARKERS:
        if marker in summary_text:
            return True
    return False


def looks_like_seller_ai_reply(summary_text: str) -> bool:
    """v6.0.75:啟發式判斷一條訊息是否為賣家 AI 自動回覆(降低我方 AI 觸發風險)。

    返回 True 表示「疑似 AI 回覆」 → 上層可決定是否仍要觸發 AI 處理
    (建議:疑似 AI 時通知用戶人工確認,而不是直接 AI 互動避免 ping-pong)。
    """
    if not summary_text:
        return False
    s = summary_text.strip()
    # 短促命中:summary 完全等於下面的可疑詞才算 AI(避免買家也說「稍等」誤判)
    if s in ("稍等", "在", "好的"):
        return False  # 這些常見短回常是真人,放行
    for phrase in _SELLER_AI_HEURISTIC_PHRASES:
        if phrase in s:
            return True
    return False


# ── 核心入口:檢查賣家是否回覆 ──

def check_seller_reply_http(
    profile_dir: Path,
    *,
    peer_user_id: str,
    sent_question: str = "",
    baseline_version: int = 0,
    baseline_ts: int = 0,
    on_log: LogFn = None,
) -> XianyuHttpCheckResult:
    """純 HTTP 檢查指定賣家是否回覆 — 不開瀏覽器。

    Args:
        profile_dir: 採購監控 profile 路徑 (PURCHASE_PROFILE_DIR)
        peer_user_id: 賣家的閒魚 userId (從 chat_url 的 peerUserId 取)
        sent_question: 我們發給賣家的問題(用來篩掉自己的訊息)
        baseline_version: 上一次檢查時記錄的 version(賣家發訊息 version+1)
        baseline_ts: 上一次檢查時的 ts(毫秒)

    Returns:
        XianyuHttpCheckResult,字段:
        - has_reply:賣家有新訊息
        - seller_reply:賣家回覆文本
        - new_version / new_ts:新基線值(下次傳進來作 baseline)
        - is_read:賣家已讀(目前 mtop 沒直接返回,留 False)
        - need_login:登入失效,需要重新掃碼
    """
    sess, token, err = _build_session(profile_dir, on_log)
    if not sess:
        return XianyuHttpCheckResult(error=err, need_login="cookie" in err.lower())

    # v6.0.75:fetch_num=500 跟 ask_xianyu_seller_via_ws 一致,
    # 確保剛新開的對話能在列表內,並能共用 session.sync 全局緩存
    sessions, sync_err = session_sync(sess, token, fetch_num=500, on_log=on_log)
    if sync_err == "TOKEN_EXPIRED":
        new_token = _refresh_token(sess, token, profile_dir, on_log)
        if new_token != token:
            sessions, sync_err = session_sync(sess, new_token, fetch_num=500, on_log=on_log, bypass_cache=True)
            token = new_token
    if sync_err:
        need_login = "ILLEGAL_ACCESS" in sync_err or "TOKEN" in sync_err
        return XianyuHttpCheckResult(error=sync_err, need_login=need_login)

    target = find_session_by_peer(sessions, peer_user_id)
    if not target:
        # 第一次找不到 → 強制 bypass cache 重新拉一次(剛發訊息可能 server 未即時索引)
        if on_log:
            on_log(f"[XY-IM-HTTP] 緩存中未找到 peer={peer_user_id},強制刷新後重試")
        sessions, sync_err2 = session_sync(sess, token, fetch_num=500, on_log=on_log, bypass_cache=True)
        if sync_err2:
            need_login = "ILLEGAL_ACCESS" in sync_err2 or "TOKEN" in sync_err2
            return XianyuHttpCheckResult(error=sync_err2, need_login=need_login)
        target = find_session_by_peer(sessions, peer_user_id)
        if not target:
            if on_log:
                on_log(f"[XY-IM-HTTP] 強刷後仍未找到 peer={peer_user_id} (200 個列表內無此對話)")
            return XianyuHttpCheckResult(
                error=f"未找到 peerUserId={peer_user_id} 的对话(可能在列表外或 ID 错误)",
                found=False,
            )

    sess_obj = target.get("session", {}) or {}
    msg_obj = target.get("message", {}) or {}
    summary_obj = msg_obj.get("summary", {}) or {}
    user_info = sess_obj.get("userInfo", {}) or {}

    session_id = str(sess_obj.get("sessionId", ""))
    try:
        new_version = int(summary_obj.get("version", "0") or 0)
    except Exception:
        new_version = 0
    try:
        new_ts = int(summary_obj.get("ts", "0") or 0)
    except Exception:
        new_ts = 0
    try:
        unread = int(summary_obj.get("unread", "0") or 0)
    except Exception:
        unread = 0
    summary_text = (summary_obj.get("summary") or "").strip()
    sender_type = str(user_info.get("type", "0"))

    # 判斷是否有新訊息:
    # - 純看 version 比 baseline 大 → 對話有變化
    # - 但 version 變化可能是「我自己發了訊息」,所以加上「unread > 0」確認是賣家發的
    # - 為了穩妥,也比對 ts 確認新訊息時間在 baseline 之後
    has_change = (new_version > baseline_version) if baseline_version > 0 else (new_ts > baseline_ts if baseline_ts > 0 else False)
    has_seller_reply = has_change and unread > 0

    # 避免「賣家回覆內容就是我剛發的問題」(極端 race condition)
    if has_seller_reply and sent_question and summary_text == sent_question.strip():
        has_seller_reply = False

    # v6.0.75:自動回覆過濾
    # 1. 平台/系統消息(「小主回來啦,AI回復已暫停」等)→ 直接跳過
    # 2. 疑似賣家 AI 回覆(「您好,我現在不在線」等)→ 標記但仍視為回覆(上層決定要不要 AI 處理)
    is_platform = is_platform_message(summary_text, sender_type)
    is_suspect_ai = looks_like_seller_ai_reply(summary_text)

    if has_seller_reply and is_platform:
        if on_log:
            on_log(f"[XY-IM-HTTP] 跳过平台/系统消息: type={sender_type} text={summary_text[:60]}")
        has_seller_reply = False  # 不視為真實回覆,繼續輪詢等待真人

    if on_log and has_change:
        on_log(f"[XY-IM-HTTP] 对话 {session_id} 版本变化: {baseline_version} → {new_version}, "
               f"unread={unread}, summary={summary_text[:40]}"
               + (" [疑似AI]" if is_suspect_ai and has_seller_reply else "")
               + (" [平台消息已跳过]" if is_platform else ""))

    return XianyuHttpCheckResult(
        has_reply=has_seller_reply,
        seller_reply=summary_text if has_seller_reply else "",
        new_version=new_version,
        new_ts=new_ts,
        session_id=session_id,
        found=True,
        is_platform_msg=is_platform,
        is_suspected_ai=is_suspect_ai and has_seller_reply,
    )


# ── 啟動基線初始化 ──

def find_session_by_id(
    sessions: List[Dict],
    session_id: str,
) -> Optional[Dict]:
    """從 session.sync 結果中找指定 sessionId 的對話。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sess_obj = s.get("session", {}) or {}
        if str(sess_obj.get("sessionId", "")) == sid:
            return s
    return None


def get_baseline_for_session(
    profile_dir: Path,
    session_id: str,
    on_log: LogFn = None,
) -> Tuple[int, int, str]:
    """已知 sessionId,讀 version/ts 作為 baseline(避免回頭從 peer 反查)。

    Returns: (version, ts, error)
    sessionId 在 session.sync 找不到時 (0, 0, error_msg) — 但不阻斷流程,
    WS 路徑可以用 (0, 0) baseline 跑,後續訊息照樣會推送。
    """
    sess, token, err = _build_session(profile_dir, on_log)
    if not sess:
        return 0, 0, err

    sessions, sync_err = session_sync(sess, token, on_log=on_log)
    if sync_err == "TOKEN_EXPIRED":
        new_token = _refresh_token(sess, token, profile_dir, on_log)
        if new_token != token:
            sessions, sync_err = session_sync(sess, new_token, on_log=on_log, bypass_cache=True)
    if sync_err:
        return 0, 0, sync_err

    target = find_session_by_id(sessions, session_id)
    if not target:
        # 新對話 sessionId 還沒在 sync 出現是正常的(剛 frontend 計算出),返回 0 baseline 即可
        return 0, 0, f"session.sync 暫無 sessionId={session_id}(新對話正常)"

    summary_obj = (target.get("message", {}) or {}).get("summary", {}) or {}
    try:
        version = int(summary_obj.get("version", "0") or 0)
    except Exception:
        version = 0
    try:
        ts = int(summary_obj.get("ts", "0") or 0)
    except Exception:
        ts = 0
    return version, ts, ""


def get_baseline_for_peer(
    profile_dir: Path,
    peer_user_id: str,
    on_log: LogFn = None,
) -> Tuple[int, int, str, str]:
    """初次發送問題給賣家後,讀取當前 version/ts 作為 baseline。

    Returns: (version, ts, session_id, error)
    沒找到 peer 時 (0, 0, "", error_msg)
    """
    sess, token, err = _build_session(profile_dir, on_log)
    if not sess:
        return 0, 0, "", err

    sessions, sync_err = session_sync(sess, token, on_log=on_log)
    if sync_err == "TOKEN_EXPIRED":
        new_token = _refresh_token(sess, token, profile_dir, on_log)
        if new_token != token:
            sessions, sync_err = session_sync(sess, new_token, on_log=on_log, bypass_cache=True)
    if sync_err:
        return 0, 0, "", sync_err

    target = find_session_by_peer(sessions, peer_user_id)
    if not target:
        return 0, 0, "", f"未找到 peerUserId={peer_user_id}"

    sess_obj = target.get("session", {}) or {}
    summary_obj = (target.get("message", {}) or {}).get("summary", {}) or {}
    session_id = str(sess_obj.get("sessionId", ""))
    try:
        version = int(summary_obj.get("version", "0") or 0)
    except Exception:
        version = 0
    try:
        ts = int(summary_obj.get("ts", "0") or 0)
    except Exception:
        ts = 0
    return version, ts, session_id, ""


# ── v6.0.75 WebSocket 用:取 accessToken ──

def get_login_token(
    profile_dir: Path,
    device_id: str,
    on_log: LogFn = None,
) -> Tuple[str, str, int, str]:
    """v6.0.75:呼叫 mtop.taobao.idlemessage.pc.login.token 取得 WebSocket /reg 用的 accessToken。

    Returns: (access_token, refresh_token, expired_ms, error)
    expired_ms: 86400000 = 24 小時毫秒數
    """
    sess, mtop_token, err = _build_session(profile_dir, on_log)
    if not sess:
        return "", "", 0, err

    t = _ts_ms()
    # 注意:payload 用 WS_APP_KEY,但 mtop 簽名仍用 mtop 的 token + APP_KEY(<XIANYU_APP_KEY_REDACTED>)
    payload = json.dumps(
        {"appKey": WS_APP_KEY, "deviceId": device_id},
        separators=(",", ":"),
    )
    sign = _sign(mtop_token, t, payload)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
        "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
        "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
        "api": API_LOGIN_TOKEN,
    }

    def _do_request():
        return sess.post(
            f"{API_BASE}/{API_LOGIN_TOKEN}/1.0/",
            params=params, data={"data": payload},
            headers=_BASE_HEADERS, timeout=15,
        )

    try:
        r = _do_request()
        j = r.json()
        ret = j.get("ret", [])
        ret_str = " ".join(str(x) for x in ret)
        # v6.0.75:統一識別所有 EXPIRED/TOKEN 失效錯誤
        if any(k in ret_str for k in ("EXOIRED", "EXPIRED", "TOKEN_EMPTY", "TOKEN")):
            new_token = _refresh_token(sess, mtop_token, profile_dir, on_log)
            if new_token != mtop_token:
                sign2 = _sign(new_token, t, payload)
                params["sign"] = sign2
                r = _do_request()
                j = r.json()
                ret = j.get("ret", [])
                ret_str = " ".join(str(x) for x in ret)

        if "SUCCESS" not in ret_str:
            # RGV587 等限流失敗 → 上層 fallback Playwright (auto_refresh_ws_token)
            return "", "", 0, f"login.token 失败: {ret_str[:120]}"
        data = j.get("data", {}) or {}
        access = str(data.get("accessToken", "") or "")
        refresh = str(data.get("refreshToken", "") or "")
        try:
            exp_ms = int(data.get("accessTokenExpiredTime", 0) or 0)
        except Exception:
            exp_ms = 0
        if not access:
            return "", "", 0, "login.token 返回空 accessToken"
        if on_log:
            on_log(f"[XY-IM-HTTP] login.token OK: access={access[:24]}... exp_ms={exp_ms}")
        return access, refresh, exp_ms, ""
    except Exception as e:
        return "", "", 0, f"login.token 异常: {e}"


def get_my_user_id(profile_dir: Path) -> str:
    """從 goofish cookie 取當前登入 userId (cookie 'unb' 值)。"""
    try:
        from core.goofish_cookie_store import load_goofish_cookies
        cookies, _, _ = load_goofish_cookies(profile_dir)
        return str(cookies.get("unb", "")).strip()
    except Exception:
        return ""


# ── access_token 本地缓存(24h TTL,RGV587 异常码只影响第一次) ──

def _access_token_cache_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "goofish_ws_access_token.json"


def load_cached_access_token(profile_dir: Path) -> Tuple[str, int]:
    """读 access_token 缓存,返回 (token, expires_at_ts_ms)。
    expires_at_ts_ms <= now 视为过期。
    """
    fp = _access_token_cache_path(profile_dir)
    if not fp.exists():
        return "", 0
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        token = str(data.get("access_token", ""))
        expires_at = int(data.get("expires_at_ms", 0))
        if not token or expires_at <= int(time.time() * 1000):
            return "", 0
        return token, expires_at
    except Exception:
        return "", 0


def save_cached_access_token(
    profile_dir: Path,
    access_token: str,
    refresh_token: str,
    expired_ms: int,
) -> None:
    """写 access_token 缓存。expired_ms 是相对时间(86400000=24h)。"""
    if not access_token:
        return
    fp = _access_token_cache_path(profile_dir)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 留 1 小时 buffer 提前刷新
        expires_at_ms = int(time.time() * 1000) + max(0, expired_ms - 3600000)
        fp.write_text(json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expired_ms": expired_ms,
            "expires_at_ms": expires_at_ms,
            "saved_at": time.time(),
            "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_access_token_cached_or_fetch(
    profile_dir: Path,
    device_id: str,
    on_log: LogFn = None,
    force_refresh: bool = False,
) -> Tuple[str, str]:
    """高阶入口:优先用 24h 内缓存,过期才呼叫 login.token (RGV587 异常码触发概率低)。
    Returns: (access_token, error)
    """
    if not force_refresh:
        cached, expires_at = load_cached_access_token(profile_dir)
        if cached:
            age_h = (int(time.time() * 1000) - (expires_at - 82800000)) / 3600000  # 82800000 = 23h
            if on_log:
                on_log(f"[XY-IM-HTTP] 用缓存 accessToken (age {age_h:.1f}h, 还有 "
                       f"{(expires_at - int(time.time()*1000))/3600000:.1f}h)")
            return cached, ""

    if on_log:
        on_log(f"[XY-IM-HTTP] 缓存空/过期,呼叫 login.token...")
    access, refresh, exp_ms, err = get_login_token(profile_dir, device_id, on_log)
    if err:
        return "", err
    save_cached_access_token(profile_dir, access, refresh, exp_ms)
    return access, ""


# ── 工具:從 chat_url 取 peerUserId ──

def extract_peer_user_id(chat_url: str) -> str:
    """從閒魚對話 URL 取 peerUserId。

    URL 範例:
    - https://www.goofish.com/im?spm=...&peerUserId=2214106370143&itemId=xxx
    - https://h5.m.goofish.com/im?peerUserId=...
    """
    if not chat_url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(chat_url)
        qs = parse_qs(u.query)
        v = qs.get("peerUserId", [""])[0]
        return v.strip()
    except Exception:
        return ""


def extract_item_id(url: str) -> str:
    """從閒魚 URL 提取 itemId,支援:
    - https://www.goofish.com/item?id=9900000000001
    - https://h5.m.goofish.com/item?forceFlush=1&id=9900000000001
    - https://www.goofish.com/im?...&itemId=9900000000001
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(url)
        qs = parse_qs(u.query)
        # 試 id / itemId 兩種 query key
        for key in ("id", "itemId"):
            v = qs.get(key, [""])[0]
            if v.strip():
                return v.strip()
    except Exception:
        pass
    return ""


# ── peer_uid + item_id → sessionId 持久化映射 (避免重複開 Playwright) ──

def _session_mapping_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "goofish_session_mapping.json"


def save_session_mapping(profile_dir: Path, peer_uid: str, item_id: str, session_id: str) -> None:
    """存 (peer_uid, item_id) → sessionId 對應(後續同對話直接 WS,不用再開瀏覽器)。"""
    fp = _session_mapping_path(profile_dir)
    try:
        data = {}
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
        key = f"{peer_uid}|{item_id}"
        data[key] = {
            "session_id": session_id,
            "saved_at": time.time(),
            "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_session_mapping(profile_dir: Path, peer_uid: str, item_id: str) -> str:
    """讀 (peer_uid, item_id) → sessionId 對應。Returns: sessionId 或 ""。"""
    fp = _session_mapping_path(profile_dir)
    if not fp.exists():
        return ""
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        key = f"{peer_uid}|{item_id}"
        item = data.get(key)
        if isinstance(item, dict):
            return str(item.get("session_id", ""))
    except Exception:
        pass
    return ""


# ── 從 itemId 反查 sellerUserId (= peerUserId) ──

API_DETAIL_UNIT = "mtop.taobao.idle.awesome.detail.unit"

_SELLER_USER_ID_RES = (
    re.compile(r'"sellerInfo"\s*:\s*\{[^}]*"userId"\s*:\s*"?(\d{6,16})"?'),
    re.compile(r'"sellerId"\s*:\s*"?(\d{6,16})"?'),
    re.compile(r'"userId"\s*:\s*"?(\d{8,16})"?,\s*"userType"'),
)


# v6.1.45:itemId → sellerUserId 永久 cache,避免 detail API 反覆呼叫收到 RGV587 异常码
# sellerUserId 不會變(同個商品永遠是同個賣家),所以無 TTL
# 修「閒魚剛登入但 detail API 撞 RGV587 → 用戶送賣家失敗」bug
def _seller_uid_cache_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "goofish_item_seller_cache.json"


def load_seller_uid(profile_dir: Path, item_id: str) -> str:
    """itemId → sellerUserId 緩存讀取。命中返回 sellerUserId,沒命中返回 ""。"""
    if not item_id:
        return ""
    try:
        p = _seller_uid_cache_path(profile_dir)
        if not p.exists():
            return ""
        with p.open("r", encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get(str(item_id), "") or "")
    except Exception:
        return ""


def save_seller_uid(profile_dir: Path, item_id: str, seller_uid: str) -> None:
    """寫入 itemId → sellerUserId 緩存(atomic)。"""
    if not item_id or not seller_uid:
        return
    try:
        p = _seller_uid_cache_path(profile_dir)
        d = {}
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
        d[str(item_id)] = str(seller_uid)
        # atomic write
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        import os as _os
        _os.replace(str(tmp), str(p))
    except Exception:
        pass


def fetch_seller_uid_from_item_html(
    profile_dir: Path,
    item_id: str,
    on_log: LogFn = None,
) -> Tuple[str, str]:
    """v6.1.45 反向工程:直接 GET 商品頁 HTML,從中 regex 抽 peerUserId/sellerUserId。

    商品頁 https://www.goofish.com/item?id={item_id} 是普通 HTTP 請求,
    HTML 裡有「私信賣家」連結帶 ?peerUserId=X 參數。
    這個請求**不會收到 RGV587 异常码**(不是 mtop API),完全繞開 detail.unit。

    Returns: (seller_user_id, error_msg)
    """
    if not item_id or not str(item_id).strip():
        return "", "item_id 空"

    sess, _tok, err = _build_session(profile_dir, on_log)
    if not sess:
        return "", err or "build_session 失敗"

    url = f"https://www.goofish.com/item?id={item_id}"
    headers = {
        "User-Agent": _BASE_HEADERS.get("User-Agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    try:
        r = sess.get(url, headers=headers, timeout=15)
    except Exception as e:
        return "", f"item HTML 請求異常: {e}"

    if r.status_code != 200:
        return "", f"item HTML status={r.status_code}"

    html = r.text or ""
    if not html:
        return "", "item HTML 空"

    # 多 pattern 抽 sellerUserId(從實機 browser scrape 驗證:peerUserId 命中率最高)
    _HTML_SELLER_PATTERNS = (
        re.compile(r'peerUserId=(\d{6,16})'),
        re.compile(r'"sellerInfo"\s*:\s*\{[^}]*"userId"\s*:\s*"?(\d{6,16})'),
        re.compile(r'"sellerId"\s*:\s*"?(\d{6,16})'),
        re.compile(r'"userId"\s*:\s*"?(\d{6,16})"?,\s*"userType"'),
        re.compile(r'sellerId=(\d{6,16})'),
    )
    for pat in _HTML_SELLER_PATTERNS:
        m = pat.search(html)
        if m:
            seller_uid = m.group(1)
            if on_log:
                on_log(
                    f"[XY-IM-HTTP] item HTML 抽到 sellerUserId={seller_uid} "
                    f"(itemId={item_id}, pattern={pat.pattern[:30]})"
                )
            try:
                save_seller_uid(profile_dir, item_id, seller_uid)
            except Exception:
                pass
            return seller_uid, ""

    return "", "item HTML 解析 sellerUserId 失敗"


def fetch_seller_uid_by_item_id(
    profile_dir: Path,
    item_id: str,
    on_log: LogFn = None,
) -> Tuple[str, str]:
    """從商品 itemId 反查 sellerUserId(= peerUserId)。

    v6.1.45 三層反向工程策略,避開 RGV587 异常码:
    1. 永久 cache 命中 → 直接返回,不打任何 API
    2. 商品頁 HTML scrape(普通網頁 GET,不收到 RGV587)← 主路徑
    3. detail API(mtop.taobao.idle.awesome.detail.unit)← 最後 fallback

    Returns: (seller_user_id, error_msg)
    """
    # Layer 1:cache 命中直接返回
    cached_uid = load_seller_uid(profile_dir, item_id)
    if cached_uid:
        if on_log:
            on_log(f"[XY-IM-HTTP] sellerUserId cache 命中: item={item_id} → seller={cached_uid}")
        return cached_uid, ""

    # Layer 2:HTML scrape 商品頁(不收到 RGV587)
    html_uid, html_err = fetch_seller_uid_from_item_html(profile_dir, item_id, on_log=on_log)
    if html_uid:
        return html_uid, ""
    if on_log:
        on_log(f"[XY-IM-HTTP] HTML scrape 失敗({html_err}),fallback detail API")

    # Layer 3:detail API(可能撞 RGV587)
    sess, mtop_token, err = _build_session(profile_dir, on_log)
    if not sess:
        return "", err
    if not item_id or not str(item_id).strip():
        return "", "item_id 空"

    t = _ts_ms()
    payload = json.dumps({
        "commerceAdPlanId": "",
        "extra": '{"labelIds":"36,35,9,12"}',
        "fishAdCode": "440902",
        "flowVersion": "6.0",
        "gps": "0,0",
        "isOld": False,
        "itemId": str(item_id),
        "latitude": "",
        "longitude": "",
        "needSimpleDetail": False,
    }, separators=(",", ":"))
    sign = _sign(mtop_token, t, payload)
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
        "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
        "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
        "api": API_DETAIL_UNIT,
        # v6.1.45:加入 spm 追蹤參數 + sessionOption(對齊 GoofishApiChecker,
        # server 用這些參數判斷是否為「正常從商品頁發起的合法請求」,缺了會增加 RGV587 風險)
        "spm_cnt": "a21ybx.item.0.0",
        "spm_pre": "widle.12011849.0.0",
        "sessionOption": "AutoLoginOnly",
    }
    headers = dict(_BASE_HEADERS)
    headers["Referer"] = f"https://www.goofish.com/item?id={item_id}"
    # v6.0.75:網絡層 timeout 自動 retry(瞬時網絡抖動 / 對端忙)
    import time as _time
    last_exc = None
    r = None
    # v6.1.45:每次 response 後刪 punish cookie,避免「server set x5secdata 限流標記
    # → 下次帶上同 punish cookie → server 立刻繼續 punish」死循環(對齊 GoofishApiChecker)
    def _strip_punish_cookies(_sess):
        try:
            for _bad in ("x5secdata", "x5sectag", "tb_xs_id", "bxuuid"):
                for _d in (".goofish.com", "goofish.com", "h5api.m.goofish.com",
                           ".taobao.com", "taobao.com", "passport.goofish.com"):
                    try:
                        _sess.cookies.delete(_bad, domain=_d)
                    except Exception:
                        pass
                try:
                    _sess.cookies.delete(_bad)
                except Exception:
                    pass
        except Exception:
            pass
    for attempt in range(3):
        try:
            r = sess.post(
                f"{API_BASE}/{API_DETAIL_UNIT}/1.0/",
                params=params, data={"data": payload},
                headers=headers, timeout=15,
            )
            # 立刻刪 punish cookie(server Set-Cookie 後自動加到 jar)
            _strip_punish_cookies(sess)
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            msg = str(e)
            # curl: (28) timeout / connection / Failed to perform → 網絡層,retry
            is_net = any(k in msg.lower() for k in ("timeout", "connection", "failed to perform", "curl: (28)", "curl: (7)"))
            if not is_net or attempt == 2:
                break
            if on_log:
                on_log(f"[XY-IM-HTTP] detail API 網絡異常 (第 {attempt+1} 次): {msg[:80]},2 秒後 retry")
            _time.sleep(2)

    if last_exc is not None or r is None:
        return "", f"detail API 請求異常: {last_exc}"

    try:
        text = r.text
        try:
            j = r.json()
        except Exception:
            return "", f"detail 解析失敗 status={r.status_code}"
        ret = j.get("ret", [])
        ret_str = " ".join(str(x) for x in ret)
        if "SUCCESS" not in ret_str:
            # v6.0.75 Layer 1:統一識別 EXPIRED/TOKEN 失效 → 刷新 token retry
            if any(k in ret_str for k in ("EXOIRED", "EXPIRED", "TOKEN_EMPTY", "TOKEN")):
                new_token = _refresh_token(sess, mtop_token, profile_dir, on_log)
                if new_token != mtop_token:
                    sign2 = _sign(new_token, t, payload)
                    params["sign"] = sign2
                    r = sess.post(
                        f"{API_BASE}/{API_DETAIL_UNIT}/1.0/",
                        params=params, data={"data": payload},
                        headers=headers, timeout=20,
                    )
                    text = r.text
                    j = r.json()
                    ret = j.get("ret", [])
                    ret_str = " ".join(str(x) for x in ret)

            # v6.0.75 Layer 2:仍失敗 + ILLEGAL/EXPIRED → SQLite 同步 cookie retry
            if "SUCCESS" not in ret_str and any(k in ret_str for k in ("ILLEGAL", "EXOIRED", "EXPIRED", "TOKEN")):
                if on_log:
                    on_log(f"[XY-IM-HTTP] detail API 仍失敗 ({ret_str[:60]}) → 嘗試 SQLite 同步 cookie")
                if _try_sync_cookies_from_chrome_sqlite(profile_dir, on_log):
                    # 重建 session 後重試
                    sess2, token2, _e2 = _build_session(profile_dir, on_log)
                    if sess2:
                        t3 = _ts_ms()
                        sign3 = _sign(token2, t3, payload)
                        params["sign"] = sign3
                        params["t"] = t3
                        r = sess2.post(
                            f"{API_BASE}/{API_DETAIL_UNIT}/1.0/",
                            params=params, data={"data": payload},
                            headers=headers, timeout=20,
                        )
                        text = r.text
                        try:
                            j = r.json()
                            ret = j.get("ret", [])
                            ret_str = " ".join(str(x) for x in ret)
                        except Exception:
                            pass

            if "SUCCESS" not in ret_str:
                return "", f"detail API 錯誤: {ret_str[:120]}"

        # 從 response text 用 regex 抽 sellerInfo.userId
        for pat in _SELLER_USER_ID_RES:
            m = pat.search(text)
            if m:
                seller_uid = m.group(1)
                if on_log:
                    on_log(f"[XY-IM-HTTP] detail API 找到 sellerUserId={seller_uid} (itemId={item_id})")
                # v6.1.45:寫入 cache,下次同 itemId 不再打 API(避免 RGV587)
                try:
                    save_seller_uid(profile_dir, item_id, seller_uid)
                except Exception:
                    pass
                return seller_uid, ""
        return "", "detail API 成功但未找到 sellerInfo.userId"
    except Exception as e:
        return "", f"detail API 請求異常: {e}"
