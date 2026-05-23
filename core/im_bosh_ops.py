"""纯 HTTP BOSH/XMPP 消红点 — 通过 Juiker IM 服务器标记频道已读。

原理：Yahoo IM 使用 BOSH (XMPP over HTTP) 通信。红点的清除需要：
  1. BOSH session → SASL PLAIN auth (JWT) → bind → session → presence
  2. channel_user_active(isActive=true) — 标记用户进入该频道
  3. queryMessage — 加载消息（触发 read mark 更新）
  4. channel_user_active(isActive=false) — 离开频道
  5. terminate

JWT 来源：
  - 监控模块浏览器打开 chat 页时，BOSH SASL auth 请求中携带 JWT
  - capture_bosh_jwt.py 工具可手动提取
  - 监控 Playwright 页面在线时，自动拦截并缓存 JWT

缓存文件：profiles/{account}/bosh_cache.json
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple

try:
    from curl_cffi.requests import Session as CffiSession
    HAS_CFFI = True
except ImportError:
    import requests
    CffiSession = requests.Session
    HAS_CFFI = False

from .client_runtime_compat import YAHOO_CURL_CFFI_IMPERSONATE as CURL_CFFI_IMPERSONATE

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# ── BOSH 常量 ─────────────────────────────────────────

BOSH_URL = "https://imapi-ap-94600.tw.juiker.net/http-bind"
IM_SERVER = "ismarus-ap-94600.tw.juiker.net"
SERVICE_JID = f"service@{IM_SERVER}"

# JWT 最大有效期（从缓存文件保存时间算起），默认 50 分钟
# Yahoo JWT 实际有效期约 1 小时，留 10 分钟余量
JWT_MAX_AGE = 3000

# BOSH cache 文件名
BOSH_CACHE_FILE = "bosh_cache.json"


# ── 工具函数 ──────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def _escape_xml(s: str) -> str:
    """HTML entity escape for XML text content."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _reverse_channel_id(channel_id: str) -> str:
    """反转 channelId 中买卖方顺序。"""
    parts = channel_id.split(":")
    if len(parts) == 3:
        return f"{parts[0]}:{parts[2]}:{parts[1]}"
    return channel_id


def _post_bosh(session: CffiSession, xml_body: str, timeout: int = 15) -> Tuple[int, str]:
    """发送 BOSH 请求，返回 (status_code, response_text)。"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "Origin": "https://tw.bid.yahoo.com",
        "Referer": "https://tw.bid.yahoo.com/",
    }
    kw = {}
    if HAS_CFFI:
        kw["impersonate"] = CURL_CFFI_IMPERSONATE
    resp = session.post(
        BOSH_URL,
        data=xml_body.encode("utf-8"),
        headers=headers,
        timeout=timeout,
        **kw,
    )
    return resp.status_code, resp.text


# ── BOSH Cache 管理 ──────────────────────────────────

def save_bosh_cache(
    profile_dir: Path,
    jwt: str,
    user: str,
    *,
    jid: str = "",
    resource: str = "",
    im_server: str = IM_SERVER,
    bosh_url: str = BOSH_URL,
) -> bool:
    """保存 BOSH JWT 到缓存文件。"""
    profile_dir = Path(profile_dir)
    fp = profile_dir / BOSH_CACHE_FILE
    data = {
        "jwt": jwt,
        "user": user,
        "jid": jid,
        "resource": resource,
        "im_server": im_server,
        "bosh_url": bosh_url,
        "captured_at": time.time(),
        "captured_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_bosh_cache(
    profile_dir: Path,
    max_age: float = JWT_MAX_AGE,
) -> Tuple[str, str, float]:
    """加载 BOSH JWT 缓存。

    返回 (jwt, user, captured_at)。
    不可用时返回 ("", "", 0.0)。
    """
    fp = Path(profile_dir) / BOSH_CACHE_FILE
    if not fp.exists():
        return "", "", 0.0

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return "", "", 0.0

    jwt = data.get("jwt", "")
    user = data.get("user", "")
    captured_at = data.get("captured_at", 0.0)

    if not jwt or not user:
        return "", "", 0.0

    # 检查缓存文件年龄
    age = time.time() - captured_at
    if age > max_age:
        return "", "", 0.0

    # 检查 JWT 本身的过期时间
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        exp = payload.get("exp", 0)
        if exp and time.time() >= exp:
            return "", "", 0.0  # JWT 已过期
    except Exception:
        pass  # JWT 解析失败不阻塞，依赖 BOSH auth 失败检测

    return jwt, user, captured_at


# ── BOSH Mark Read ────────────────────────────────────

def bosh_mark_read(
    profile_dir: Path,
    channel_id: str,
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """纯 HTTP BOSH 消红点。

    完整流程：
      BOSH init → SASL auth → restart → bind → session → presence
      → queryMessage + channel_user_active → deactivate → terminate

    返回 (success, info_message)。
    """
    if on_log is None:
        on_log = lambda *_: None

    # 加载 JWT
    jwt, user, captured_at = load_bosh_cache(profile_dir)
    if not jwt:
        # v6.0.83:cache 未命中 → 純 HTTP 刷新(取代 Playwright 攔截)
        # 邏輯:GET /fe/api/im/user 拿 encrypted token + wssid → AES-128-CBC decrypt → plain JWT
        on_log("[BOSH] JWT cache miss,純 HTTP 刷新...")
        try:
            from .yahoo_im_jwt import ensure_bosh_jwt
            jwt, user, err = ensure_bosh_jwt(profile_dir, on_log=on_log)
            if not jwt:
                return False, f"BOSH JWT 純 HTTP 取得失敗: {err}"
            captured_at = time.time()
        except Exception as e:
            return False, f"BOSH JWT 純 HTTP 取得異常: {e}"

    age = time.time() - captured_at
    on_log(f"[BOSH] JWT user={user} age={age:.0f}s channel={channel_id}")

    bare_jid = f"{user}@{IM_SERVER}"
    rid = random.randint(1000000000, 9999999999)
    resource = f"yahoo_Chrome145_Windows10_4.6.0_tw.bid.yahoo.com{random.randint(10000000, 99999999)}"

    session = CffiSession()
    sid = ""
    full_jid = ""

    try:
        # 1. BOSH session init
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind'"
            f" to='{IM_SERVER}' xml:lang='en' wait='5' hold='1'"
            f" content='text/xml; charset=utf-8' ver='1.6'"
            f" xmpp:version='1.0' xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        status, resp = _post_bosh(session, xml)
        if status != 200:
            return False, f"BOSH init failed: {status}"
        m = re.search(r"sid='([^']+)'", resp) or re.search(r'sid="([^"]+)"', resp)
        if not m:
            return False, "BOSH init: no SID"
        sid = m.group(1)

        # 2. SASL PLAIN auth
        sasl_plain = f"{bare_jid}\x00{user}\x00{jwt}"
        b64 = base64.b64encode(sasl_plain.encode("utf-8")).decode("ascii")
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>{b64}</auth>"
            f"</body>"
        )
        status, resp = _post_bosh(session, xml)
        if "<success" not in resp:
            # JWT 可能已失效
            on_log(f"[BOSH] SASL auth failed, JWT may be expired")
            return False, "BOSH SASL auth failed (JWT expired?)"

        # 3. Stream restart
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'"
            f" to='{IM_SERVER}' xml:lang='en' xmpp:restart='true'"
            f" xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        _post_bosh(session, xml)

        # 4. Bind resource
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"<iq type='set' id='_bind_auth_2' xmlns='jabber:client'>"
            f"<bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>"
            f"<resource>{resource}</resource>"
            f"</bind></iq></body>"
        )
        status, resp = _post_bosh(session, xml)
        m = re.search(r"<jid>([^<]+)</jid>", resp)
        full_jid = m.group(1) if m else f"{bare_jid}/{resource}"

        # 5. Start session
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"<iq type='set' id='_session_auth_2' xmlns='jabber:client'>"
            f"<session xmlns='urn:ietf:params:xml:ns:xmpp-session'/>"
            f"</iq></body>"
        )
        _post_bosh(session, xml)

        # 6. Presence
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"<presence xmlns='jabber:client'/>"
            f"</body>"
        )
        _post_bosh(session, xml)

        # 7. queryMessage + channel_user_active (消红点核心!)
        # channelId 方向不确定（y{seller}:y{buyer} 或 y{buyer}:y{seller}），
        # 两个方向都发，确保消到正确的红点
        ids_to_try = [channel_id]
        rev = _reverse_channel_id(channel_id)
        if rev != channel_id:
            ids_to_try.append(rev)

        stanzas = ""
        for cid in ids_to_try:
            q_json = _escape_xml(json.dumps(
                {"chID": cid, "afterN": -10}, separators=(",", ":")
            ))
            a_json = _escape_xml(json.dumps(
                {"chID": cid, "isActive": True}, separators=(",", ":")
            ))
            stanzas += (
                f"<iq to='{SERVICE_JID}' from='{bare_jid}' type='get'"
                f" id='{_uid()}' developerID='yahoo' xmlns='jabber:client'>"
                f"<query item='juiker:iq:queryMessage'>{q_json}</query></iq>"
                f"<iq to='{SERVICE_JID}' from='{bare_jid}' type='set'"
                f" id='{_uid()}' developerID='yahoo' xmlns='jabber:client'>"
                f"<query item='channel_user_active'>{a_json}</query></iq>"
            )

        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"{stanzas}"
            f"</body>"
        )
        status, resp = _post_bosh(session, xml)

        # 检查结果
        got_active = "channel_user_active" in resp and "returnCode" in resp.replace("&quot;", '"')
        got_query = "juiker:iq:queryMessage" in resp

        if not (got_active or got_query):
            # 响应可能在下一个 poll 中
            time.sleep(1)
            rid += 1
            xml = f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'/>"
            try:
                status, resp = _post_bosh(session, xml, timeout=8)
                got_active = "channel_user_active" in resp
                got_query = "juiker:iq:queryMessage" in resp
            except Exception:
                pass

        # 提取 markTS
        mark_ts = 0
        if "markTS" in resp:
            m_ts = re.search(r'"markTS"\s*:\s*(\d+)', resp.replace("&quot;", '"'))
            if m_ts:
                mark_ts = int(m_ts.group(1))

        # 8. Deactivate channels (两个方向都 deactivate)
        deactive_stanzas = ""
        for cid in ids_to_try:
            d_json = _escape_xml(json.dumps(
                {"chID": cid, "isActive": False}, separators=(",", ":")
            ))
            deactive_stanzas += (
                f"<iq to='{SERVICE_JID}' from='{bare_jid}' type='set'"
                f" id='{_uid()}' developerID='yahoo' xmlns='jabber:client'>"
                f"<query item='channel_user_active'>{d_json}</query></iq>"
            )
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'>"
            f"{deactive_stanzas}"
            f"</body>"
        )
        try:
            _post_bosh(session, xml, timeout=5)
        except Exception:
            pass

        # 9. Terminate
        rid += 1
        xml = (
            f"<body rid='{rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{sid}'"
            f" type='terminate'>"
            f"<presence xmlns='jabber:client' type='unavailable'/>"
            f"</body>"
        )
        try:
            _post_bosh(session, xml, timeout=5)
        except Exception:
            pass

        # 结果
        if got_active or got_query:
            ts_info = ""
            if mark_ts:
                ts_info = f" markTS={time.strftime('%H:%M:%S', time.localtime(mark_ts / 1000))}"
            on_log(f"[BOSH] 消红点成功: {channel_id}{ts_info}")
            return True, f"BOSH mark_read OK{ts_info}"
        else:
            on_log(f"[BOSH] 消红点: stanza 已发送但未确认结果")
            return True, "BOSH stanzas sent (unconfirmed)"

    except Exception as e:
        on_log(f"[BOSH] 消红点失败: {e}")
        return False, str(e)[:200]
    finally:
        try:
            session.close()
        except Exception:
            pass


# ── JWT 捕获（从 Playwright BOSH 流量中提取） ─────────

def extract_jwt_from_bosh_request(post_data: str) -> Tuple[str, str]:
    """从 BOSH SASL PLAIN auth 请求中提取 JWT 和 user。

    由监控模块的 page.on("request") 回调调用。
    返回 (jwt, user)，失败返回 ("", "")。
    """
    m = re.search(r'mechanism=["\']PLAIN["\'][^>]*>([^<]+)</auth>', post_data)
    if not m:
        return "", ""

    b64 = m.group(1)
    try:
        decoded = base64.b64decode(b64).decode("utf-8", errors="replace")
        parts = decoded.split("\x00")
        if len(parts) >= 3:
            jwt_token = parts[2]
            user = parts[1] if parts[1] else parts[0].split("@")[0]
            if jwt_token and user:
                return jwt_token, user
    except Exception:
        pass

    return "", ""


async def install_bosh_jwt_capture(page, profile_dir, on_log: Optional[LogFn] = None) -> None:
    """在 Playwright page 上安装 BOSH JWT 自动捕获。

    当页面加载 chat 页时，Yahoo IM SDK 会自动建立 BOSH 连接并发送 SASL PLAIN auth。
    此函数安装 page.on("request") 监听器，自动从中提取 JWT 并保存到 bosh_cache.json。

    应在 page.goto() 之前调用（只需调一次，page 生命周期内有效）。
    """
    if getattr(page, "_bosh_jwt_capture_installed", False):
        return  # 已安装，避免重复
    page._bosh_jwt_capture_installed = True

    _profile_dir = Path(profile_dir)
    _log = on_log or (lambda *_: None)

    def _on_request(request):
        try:
            if "http-bind" not in request.url:
                return
            post = request.post_data
            if not post or "PLAIN" not in post:
                return
            jwt, user = extract_jwt_from_bosh_request(post)
            if jwt and user:
                save_bosh_cache(_profile_dir, jwt, user)
                _log(f"[BOSH] JWT 自动捕获成功: user={user}, jwt_len={len(jwt)}")
        except Exception:
            pass

    page.on("request", _on_request)
