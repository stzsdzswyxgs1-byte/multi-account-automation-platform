"""Yahoo IM BOSH long-polling listener — 即時接收 server push (v6.0.83)

通過 BOSH long-polling 連線 Juiker IM server,即時接收:
- <message type="send_message">     → 對方發訊息給我們(包含 chID, content)
- <message type="mark_read">         → 對方已讀我們的訊息(已讀回執)
- <message type="recall_messages">   → 對方撤回某條訊息
- <message type="forward_messages">  → 對方轉發訊息
- <iq type="result">                  → 對應某個 IQ 的回應(由 BOSHSession.iq() 處理)

BOSH long-polling 機制:
- client POST <body sid="..." rid="N"/> 給 http-bind
- server hold 連線最多 wait 秒(預設 30)
- 期間有 stanza 推來 server 立即返回
- 沒 stanza 則 wait 到期返回空 body
- client 收到 response 立即再 POST 下一個 poll(rid+1)
- 任何時候 server push 都會通過下一個 poll 返回

整合方式:
    listener = BOSHListener(profile_dir, on_message=cb1, on_mark_read=cb2, on_recall=cb3)
    listener.start()  # 背景 thread
    ...
    listener.stop()
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

try:
    from curl_cffi.requests import Session as CffiSession
    HAS_CFFI = True
except ImportError:
    import requests as _req
    CffiSession = _req.Session
    HAS_CFFI = False

from .im_bosh_ops import BOSH_URL, IM_SERVER, SERVICE_JID, _post_bosh

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


class IncomingMessage:
    """server 推來的 message stanza 解析結果。"""
    def __init__(self):
        self.message_id: str = ""
        self.channel_id: str = ""        # chID
        self.sender_jid: str = ""         # from JID
        self.msg_type: str = ""           # send_message / mark_read / recall_messages
        self.content: Dict[str, Any] = {}  # body JSON parsed
        self.timestamp_ms: int = 0
        self.raw_xml: str = ""


# Callback type definitions:
#   on_message(IncomingMessage)         — 收到對方發的訊息
#   on_mark_read(channel_id, mark_ts)   — 對方已讀我們的訊息
#   on_recall(msg_ids: List[str])       — 對方撤回訊息
#   on_disconnect(error_msg)            — BOSH 斷線


class BOSHListener:
    """BOSH long-polling listener — 背景 thread 持續接收 server push。

    使用方式:
        listener = BOSHListener(
            profile_dir,
            on_log=log,
            on_message=lambda m: print(f"got {m.channel_id}: {m.content}"),
            on_mark_read=lambda cid, ts: print(f"{cid} read at {ts}"),
            on_recall=lambda mids: print(f"recall {mids}"),
        )
        listener.start()
        # ...
        listener.stop()
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        on_log: Optional[LogFn] = None,
        on_message: Optional[Callable[[IncomingMessage], None]] = None,
        on_mark_read: Optional[Callable[[str, int], None]] = None,
        on_recall: Optional[Callable[[List[str]], None]] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        poll_wait_sec: int = 30,
        reconnect_delay_sec: int = 5,
    ):
        self.profile_dir = Path(profile_dir)
        self.on_log = on_log or (lambda *_: None)
        self.on_message = on_message
        self.on_mark_read = on_mark_read
        self.on_recall = on_recall
        self.on_disconnect = on_disconnect
        self.poll_wait_sec = poll_wait_sec
        self.reconnect_delay_sec = reconnect_delay_sec

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[CffiSession] = None
        self._sid: str = ""
        self._rid: int = 0
        self._user: str = ""
        self._jwt: str = ""
        self._bare_jid: str = ""

    def start(self) -> bool:
        """背景 thread 啟動 listener。返回是否成功啟動。"""
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="bosh-listener")
        self._thread.start()
        return True

    def stop(self, *, terminate: bool = True) -> None:
        """停止 listener + 終止 BOSH session。"""
        self._running = False
        if terminate and self._sid and self._session:
            try:
                self._rid += 1
                xml = (
                    f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind'"
                    f" sid='{self._sid}' type='terminate'>"
                    f"<presence xmlns='jabber:client' type='unavailable'/>"
                    f"</body>"
                )
                _post_bosh(self._session, xml, timeout=5)
            except Exception:
                pass
        try:
            if self._session:
                self._session.close()
        except Exception:
            pass
        self._sid = ""
        self._session = None

    def _connect(self) -> str:
        """連線 BOSH(JWT + SASL + bind + session + presence)。返回 error 或空字串。"""
        from .yahoo_im_jwt import ensure_bosh_jwt

        jwt, user, err = ensure_bosh_jwt(self.profile_dir, on_log=self.on_log)
        if not jwt:
            return f"JWT 取得失敗: {err}"
        self._jwt = jwt
        self._user = user
        self._bare_jid = f"{user}@{IM_SERVER}"
        resource = f"yahoo_Chrome145_Windows10_4.6.0_tw.bid.yahoo.com{random.randint(10000000, 99999999)}"

        self._session = CffiSession()
        self._rid = random.randint(1000000000, 9999999999)

        # BOSH init
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind'"
            f" to='{IM_SERVER}' xml:lang='en' wait='{self.poll_wait_sec}' hold='1'"
            f" content='text/xml; charset=utf-8' ver='1.6'"
            f" xmpp:version='1.0' xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        status, resp = _post_bosh(self._session, xml, timeout=self.poll_wait_sec + 5)
        if status != 200:
            return f"BOSH init {status}"
        m = re.search(r"sid=['\"]([^'\"]+)['\"]", resp)
        if not m:
            return "no SID"
        self._sid = m.group(1)

        # SASL PLAIN
        sasl = f"{self._bare_jid}\x00{self._user}\x00{self._jwt}"
        b64 = base64.b64encode(sasl.encode("utf-8")).decode("ascii")
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'>"
            f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>{b64}</auth>"
            f"</body>"
        )
        status, resp = _post_bosh(self._session, xml, timeout=15)
        if "<success" not in resp:
            return f"SASL failed: {resp[:200]}"

        # stream restart
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'"
            f" to='{IM_SERVER}' xml:lang='en' xmpp:restart='true'"
            f" xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        _post_bosh(self._session, xml, timeout=15)

        # bind
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'>"
            f"<iq type='set' id='_bind_auth_2' xmlns='jabber:client'>"
            f"<bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>"
            f"<resource>{resource}</resource>"
            f"</bind></iq></body>"
        )
        _post_bosh(self._session, xml, timeout=15)

        # session
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'>"
            f"<iq type='set' id='_session_auth_2' xmlns='jabber:client'>"
            f"<session xmlns='urn:ietf:params:xml:ns:xmpp-session'/>"
            f"</iq></body>"
        )
        _post_bosh(self._session, xml, timeout=15)

        # presence
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'>"
            f"<presence xmlns='jabber:client'/>"
            f"</body>"
        )
        _post_bosh(self._session, xml, timeout=15)
        self.on_log(f"[BOSH-LISTENER] connected user={self._user} sid={self._sid[:8]}...")
        return ""

    def _poll_once(self) -> Optional[str]:
        """發送一個空 body poll,等 server push。返回 response XML 或 None(失敗)。"""
        if not self._sid or not self._session:
            return None
        self._rid += 1
        xml = (
            f"<body rid='{self._rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self._sid}'/>"
        )
        try:
            status, resp = _post_bosh(self._session, xml, timeout=self.poll_wait_sec + 10)
            if status != 200:
                self.on_log(f"[BOSH-LISTENER] poll HTTP {status},need reconnect")
                return None
            return resp
        except Exception as e:
            self.on_log(f"[BOSH-LISTENER] poll 異常: {e}")
            return None

    def _parse_and_dispatch(self, resp_xml: str) -> None:
        """從 BOSH response XML 抽 message/iq stanza,callback 分發。"""
        if not resp_xml:
            return
        # 找所有 <message ...>...</message>
        for m in re.finditer(r'<message([^>]*)>(.*?)</message>', resp_xml, re.DOTALL):
            attrs_str = m.group(1)
            inner = m.group(2)
            msg = self._parse_message(attrs_str, inner)
            if not msg:
                continue
            self._dispatch_message(msg)

    def _parse_message(self, attrs_str: str, inner: str) -> Optional[IncomingMessage]:
        """解析 <message> stanza。"""
        try:
            msg = IncomingMessage()
            # 屬性
            type_m = re.search(r'type=[\'"]([^\'"]+)[\'"]', attrs_str)
            if type_m:
                msg.msg_type = type_m.group(1)
            id_m = re.search(r'id=[\'"]([^\'"]+)[\'"]', attrs_str)
            if id_m:
                msg.message_id = id_m.group(1)
            from_m = re.search(r'from=[\'"]([^\'"]+)[\'"]', attrs_str)
            if from_m:
                msg.sender_jid = from_m.group(1)
            # body JSON
            body_match = re.search(r'<body[^>]*>(.*?)</body>', inner, re.DOTALL)
            if body_match:
                body_text = (body_match.group(1)
                             .replace("&quot;", '"')
                             .replace("&lt;", "<")
                             .replace("&gt;", ">")
                             .replace("&amp;", "&"))
                try:
                    msg.content = json.loads(body_text)
                except Exception:
                    msg.content = {"raw": body_text[:500]}
            # 從 content 拿 chID + timestamp
            if isinstance(msg.content, dict):
                msg.channel_id = str(msg.content.get("chID") or msg.content.get("channelId") or "")
                msg.timestamp_ms = int(msg.content.get("ts") or msg.content.get("timestamp") or 0)
            msg.raw_xml = inner[:500]
            return msg
        except Exception as e:
            self.on_log(f"[BOSH-LISTENER] parse 異常: {e}")
            return None

    def _dispatch_message(self, msg: IncomingMessage) -> None:
        """根據 msg_type callback 分發。"""
        try:
            t = msg.msg_type
            if t == "send_message":
                if self.on_message:
                    self.on_message(msg)
            elif t in ("mark_read", "mark_read_response"):
                if self.on_mark_read and msg.channel_id:
                    self.on_mark_read(msg.channel_id, msg.timestamp_ms)
            elif t in ("recall_messages", "recall_messages_response"):
                if self.on_recall:
                    msg_ids = msg.content.get("msgIDs") or []
                    if isinstance(msg_ids, list):
                        self.on_recall(msg_ids)
            else:
                # 其他類型 — 仍 dispatch on_message 讓上層判斷
                if self.on_message:
                    self.on_message(msg)
        except Exception as e:
            self.on_log(f"[BOSH-LISTENER] dispatch 異常 type={msg.msg_type}: {e}")

    def _run_loop(self) -> None:
        """主迴圈:connect + poll loop + reconnect on error。"""
        backoff = self.reconnect_delay_sec
        while self._running:
            err = self._connect()
            if err:
                self.on_log(f"[BOSH-LISTENER] connect 失敗: {err},{backoff}s 後重試")
                if self.on_disconnect:
                    try: self.on_disconnect(err)
                    except Exception: pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            backoff = self.reconnect_delay_sec  # reset

            # poll loop
            consecutive_fails = 0
            while self._running:
                resp = self._poll_once()
                if resp is None:
                    consecutive_fails += 1
                    if consecutive_fails >= 3:
                        self.on_log("[BOSH-LISTENER] 連續失敗 3 次,reconnect")
                        break
                    time.sleep(2)
                    continue
                consecutive_fails = 0
                try:
                    self._parse_and_dispatch(resp)
                except Exception as e:
                    self.on_log(f"[BOSH-LISTENER] dispatch 異常: {e}")

            # 內層退出 → 嘗試 terminate 然後重連
            try:
                self.stop(terminate=True)
            except Exception:
                pass
            if not self._running:
                break
            self._running = True  # 重連
        self.on_log("[BOSH-LISTENER] 主迴圈退出")
