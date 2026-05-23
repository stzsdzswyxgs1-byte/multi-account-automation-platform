"""Yahoo IM BOSH IQ 擴充 — 純 HTTP 對話列表/紅點/撤回/已讀/歷史 (v6.0.83+)

對應 Juiker SDK constant.PROTOPCOL_NAME (52 個 IQ name,實機從 chat/Y9000000002 抓):

  send_message              ← 通用訊息 send (BOSH 內路徑)
  delete_message            ← 刪訊息
  recall_messages           ← 撤回
  mark_read                 ← 標記已讀
  mark_read_all             ← 全部已讀
  mark_read_msgids          ← 按 msgID 已讀
  forward_messages          ← 轉發
  broadcast_messages        ← 廣播
  delete_all_messages       ← 清空訊息
  delete_channel_messages   ← 清空頻道訊息

  list_channels_by_lastmsgtime    ← 對話列表(按最後訊息時間)
  get_user_unread_channels        ← 紅點未讀(實證 body={}, 返 {returnCode, totalUnread, result})
  clear_user_unread_count          ← 清未讀計數
  channel_user_active              ← 進入/離開頻道(既有 bosh_mark_read 用過)

  juiker:iq:queryMessage           ← 訊息歷史(既有用過,body={chID, afterN})
  juiker:iq:listChannels           ← 列頻道
  juiker:iq:listChannelMembers     ← 列頻道成員
  juiker:iq:getChannelProfile      ← 頻道資料
  juiker:iq:getChannelSubject      ← 頻道主題
  juiker:iq:queryChannelReadInfo   ← 頻道已讀資訊
  juiker:iq:queryMessageReadTime   ← 訊息已讀時間
  juiker:iq:listChannelsReadTime   ← 列頻道已讀時間
  juiker:iq:listCorpChannels       ← 列企業頻道

  query_user_profile, set_user_profile, set_channel_pref, set_channel_user_priv
  create_channel, dismiss_channel, leave_channel, invite_member, kick_member, answer_invitation
  modify_subject, modify_profile, modify_role
  create_vote, query_vote_list, cast_vote, query_vote_options
  query_message_read_count, query_message_unread_count
  query_message_read_users, query_message_unread_users
  query_message_feeling_counts, query_message_feeling_users, post_message_feeling
  query_scheduled_messages
  sip_cdr, sip_invite, sip_cancel
  send_channel_group_messages

Stanza 結構(實機確認):
  REQUEST  <iq xmlns="jabber:client" type="get|set" id="..." to="service@..." from="user@.../res"
                developerID="yahoo">
             <query item="<IQ_NAME>">JSON_BODY</query>
           </iq>
  RESPONSE <iq xmlns="jabber:client" type="result" id="..." from="..." to="..." version="1.0">
             <result xmlns="juiker:im" item="<IQ_NAME>">JSON_RESPONSE</result>
           </iq>

Body 約定:某些 IQ 包 chID(channel id) + 其他欄位,某些 IQ body 為 {}。
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
from typing import Callable, Optional, Tuple, Dict, Any, List

try:
    from curl_cffi.requests import Session as CffiSession
    HAS_CFFI = True
except ImportError:
    import requests as _req
    CffiSession = _req.Session
    HAS_CFFI = False

from .client_runtime_compat import CURL_CFFI_IMPERSONATE
from .im_bosh_ops import BOSH_URL, IM_SERVER, SERVICE_JID, _escape_xml, _post_bosh, _uid

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


class BOSHSession:
    """BOSH XMPP session 上下文管理器 — 一次 connect 跑多個 IQ。

    Usage:
        with BOSHSession(profile_dir, on_log=log) as sess:
            chs = sess.list_channels_by_last_msg_time(limit=20)
            unread = sess.get_user_unread_channels()
            sess.recall_message("yahoo-bid-...:y...:y...", msg_id)
            sess.mark_read("yahoo-bid-...:y...:y...", int(time.time() * 1000))
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        on_log: Optional[LogFn] = None,
        connect_timeout: int = 15,
    ):
        self.profile_dir = Path(profile_dir)
        self.on_log = on_log or (lambda *_: None)
        self.connect_timeout = connect_timeout
        self.session: Optional[CffiSession] = None
        self.sid: str = ""
        self.rid: int = 0
        self.user: str = ""
        self.jwt: str = ""
        self.bare_jid: str = ""
        self.full_jid: str = ""

    # ---- context manager ----

    def __enter__(self) -> "BOSHSession":
        self._connect()
        return self

    def __exit__(self, *args) -> None:
        self._disconnect()

    # ---- connection ----

    def _connect(self) -> None:
        # 1. 純 HTTP 拿 JWT
        from .yahoo_im_jwt import ensure_bosh_jwt
        jwt, user, err = ensure_bosh_jwt(self.profile_dir, on_log=self.on_log)
        if not jwt:
            raise RuntimeError(f"BOSH JWT 取得失敗: {err}")
        self.jwt = jwt
        self.user = user
        self.bare_jid = f"{user}@{IM_SERVER}"
        resource = f"yahoo_Chrome145_Windows10_4.6.0_tw.bid.yahoo.com{random.randint(10000000, 99999999)}"

        self.session = CffiSession()
        self.rid = random.randint(1000000000, 9999999999)

        # 2. BOSH session init
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind'"
            f" to='{IM_SERVER}' xml:lang='en' wait='30' hold='1'"
            f" content='text/xml; charset=utf-8' ver='1.6'"
            f" xmpp:version='1.0' xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        status, resp = _post_bosh(self.session, xml, timeout=self.connect_timeout)
        if status != 200:
            raise RuntimeError(f"BOSH init {status}")
        m = re.search(r"sid=['\"]([^'\"]+)['\"]", resp)
        if not m:
            raise RuntimeError("BOSH init: no SID")
        self.sid = m.group(1)

        # 3. SASL PLAIN
        sasl = f"{self.bare_jid}\x00{self.user}\x00{self.jwt}"
        b64 = base64.b64encode(sasl.encode("utf-8")).decode("ascii")
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>{b64}</auth>"
            f"</body>"
        )
        status, resp = _post_bosh(self.session, xml, timeout=self.connect_timeout)
        if "<success" not in resp:
            raise RuntimeError(f"BOSH SASL failed: {resp[:200]}")

        # 4. stream restart
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'"
            f" to='{IM_SERVER}' xml:lang='en' xmpp:restart='true'"
            f" xmlns:xmpp='urn:xmpp:xbosh'/>"
        )
        _post_bosh(self.session, xml, timeout=self.connect_timeout)

        # 5. bind
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<iq type='set' id='_bind_auth_2' xmlns='jabber:client'>"
            f"<bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>"
            f"<resource>{resource}</resource>"
            f"</bind></iq></body>"
        )
        status, resp = _post_bosh(self.session, xml, timeout=self.connect_timeout)
        m = re.search(r"<jid>([^<]+)</jid>", resp)
        self.full_jid = m.group(1) if m else f"{self.bare_jid}/{resource}"

        # 6. session
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<iq type='set' id='_session_auth_2' xmlns='jabber:client'>"
            f"<session xmlns='urn:ietf:params:xml:ns:xmpp-session'/>"
            f"</iq></body>"
        )
        _post_bosh(self.session, xml, timeout=self.connect_timeout)

        # 7. presence
        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<presence xmlns='jabber:client'/>"
            f"</body>"
        )
        _post_bosh(self.session, xml, timeout=self.connect_timeout)
        self.on_log(f"[BOSH-EXT] session ready user={self.user} sid={self.sid[:8]}...")

    def _disconnect(self) -> None:
        if not self.session or not self.sid:
            return
        try:
            self.rid += 1
            xml = (
                f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'"
                f" type='terminate'>"
                f"<presence xmlns='jabber:client' type='unavailable'/>"
                f"</body>"
            )
            _post_bosh(self.session, xml, timeout=5)
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass

    # ---- generic message (fire-and-forget,對應 SDK getXmppMsgObject) ----

    def msg(
        self,
        item_name: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        msg_type: str = "",
        wait_response: bool = True,
        poll_retries: int = 3,
        poll_wait_sec: float = 1.0,
    ) -> Tuple[Dict[str, Any], str]:
        """發送 message stanza(SDK 的 getXmppMsgObject 對應路徑)。

        結構: <message xmlns="jabber:client" type="<item_name>" id="..." to="service@..."
                       from="user@.../res" developerID="yahoo">
                <body>JSON_BODY</body>
              </message>

        Returns: (response_dict, error)。
        對應的 response 是 server 送回的 <message type="<item_name>_response"> 或類似。
        若 wait_response=False 則直接返回 ({}, "")(fire-and-forget)。
        """
        if not self.session or not self.sid:
            return {}, "session not connected"
        msg_id = _uid()
        body_json = json.dumps(body or {}, separators=(",", ":"))
        body_escaped = _escape_xml(body_json)
        stanza_type = msg_type or item_name

        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<message xmlns='jabber:client' type='{stanza_type}' id='{msg_id}'"
            f" to='{SERVICE_JID}' from='{self.bare_jid}' developerID='yahoo'>"
            f"<body>{body_escaped}</body></message>"
            f"</body>"
        )
        status, resp = _post_bosh(self.session, xml, timeout=15)
        if status != 200:
            return {}, f"send msg {item_name} HTTP {status}"

        if not wait_response:
            return {}, ""

        # 看 response 是否含對應 id 或 item_name 的 message
        for _ in range(poll_retries):
            found = self._extract_msg_response(resp, msg_id, item_name)
            if found is not None:
                return found, ""
            time.sleep(poll_wait_sec)
            self.rid += 1
            poll_xml = (
                f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'/>"
            )
            try:
                st2, resp = _post_bosh(self.session, poll_xml, timeout=10)
                if st2 != 200:
                    continue
            except Exception:
                continue
        # 沒等到 response 但 stanza 已發送
        return {}, ""

    def _extract_msg_response(
        self, resp_xml: str, sent_id: str, item_name: str
    ) -> Optional[Dict[str, Any]]:
        """從 BOSH response 內找對應的 message response。

        Server response format(實機抓:create_channel 的 response 結構)::
          <message from='<EMAIL_REDACTED>'
                   to='y9000000004@.../resource'
                   type='create_channel'    ← 同 request type,不加 _response 後綴
                   version='1.0'
                   id='<server-generated-uuid>'   ← 不是 client 的 id
          ><body>JSON_RESPONSE</body></message>

        匹配策略(優先序):
          1. 先試 client 送的 sent_id matching(舊行為,IQ 結果模式)
          2. 試 type='<item_name>_response'(舊行為,某些 IQ 結果用)
          3. 試 type='<item_name>' from='service@...'(server push 模式 — create_channel/send_message 結果)
        """
        # 先試 id matching
        m = re.search(
            rf'<message[^>]*id=[\'"][^\'"]*{re.escape(sent_id[:8])}[^\'"]*[\'"][^>]*>(.*?)</message>',
            resp_xml, re.DOTALL,
        )
        if not m:
            # 試 item_name 的 _response 後綴
            m = re.search(
                rf'<message[^>]*type=[\'"]{re.escape(item_name)}_response[\'"][^>]*>(.*?)</message>',
                resp_xml, re.DOTALL,
            )
        if not m:
            # ✅ 新增:server push 模式 — type='<item_name>' from='service@...'
            # 這是 create_channel 等 message 操作的真實 response 格式
            m = re.search(
                rf'<message[^>]*from=[\'"]service@[^\'"]+[\'"][^>]*type=[\'"]{re.escape(item_name)}[\'"][^>]*>(.*?)</message>',
                resp_xml, re.DOTALL,
            )
        if not m:
            # 試 type 在前 from 在後的順序
            m = re.search(
                rf'<message[^>]*type=[\'"]{re.escape(item_name)}[\'"][^>]*from=[\'"]service@[^\'"]+[\'"][^>]*>(.*?)</message>',
                resp_xml, re.DOTALL,
            )
        if not m:
            return None
        inner = m.group(1)
        body_match = re.search(r'<body[^>]*>(.*?)</body>', inner, re.DOTALL)
        if not body_match:
            return {"raw": inner[:500]}
        body_text = body_match.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        try:
            return json.loads(body_text)
        except Exception:
            return {"raw": body_text[:500]}

    # ---- generic IQ ----

    def iq(
        self,
        item_name: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        iq_type: str = "get",
        poll_retries: int = 3,
        poll_wait_sec: float = 1.0,
    ) -> Tuple[Dict[str, Any], str]:
        """發送 IQ stanza,等 response (poll BOSH 直到拿到對應 id 的 result)。

        Returns: (response_json_dict, error_message)
        """
        if not self.session or not self.sid:
            return {}, "session not connected"
        iq_id = _uid()
        body_json = json.dumps(body or {}, separators=(",", ":"))
        body_escaped = _escape_xml(body_json)

        self.rid += 1
        xml = (
            f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'>"
            f"<iq to='{SERVICE_JID}' from='{self.bare_jid}' type='{iq_type}'"
            f" id='{iq_id}' developerID='yahoo' xmlns='jabber:client'>"
            f"<query item='{item_name}'>{body_escaped}</query></iq>"
            f"</body>"
        )
        status, resp = _post_bosh(self.session, xml, timeout=15)
        if status != 200:
            return {}, f"send IQ {item_name} HTTP {status}"

        # 看 response 是否含目標 id 的 result
        found = self._extract_result_for_id(resp, iq_id)
        if found is not None:
            return found, ""

        # 否則 poll 多次
        for _ in range(poll_retries):
            time.sleep(poll_wait_sec)
            self.rid += 1
            poll_xml = (
                f"<body rid='{self.rid}' xmlns='http://jabber.org/protocol/httpbind' sid='{self.sid}'/>"
            )
            try:
                st2, resp2 = _post_bosh(self.session, poll_xml, timeout=10)
                if st2 != 200:
                    continue
                f2 = self._extract_result_for_id(resp2, iq_id)
                if f2 is not None:
                    return f2, ""
            except Exception:
                continue
        return {}, f"IQ {item_name} response 超時 (poll retries 用盡)"

    def _extract_result_for_id(self, resp_xml: str, iq_id: str) -> Optional[Dict[str, Any]]:
        """從 BOSH response 內找 type=result 且 id=iq_id 的 IQ,提取 query JSON body。"""
        # 一次 BOSH response 可能有多個 IQ — 用 regex 抓
        # XML 內 &quot; 等需要 unescape
        # 先找 type='result' id='iq_id'
        m = re.search(
            rf'<iq[^>]*id=[\'"]{re.escape(iq_id)}[\'"][^>]*type=[\'"]result[\'"][^>]*>(.*?)</iq>',
            resp_xml, re.DOTALL
        )
        if not m:
            # 順序可能是 type 在 id 前
            m = re.search(
                rf'<iq[^>]*type=[\'"]result[\'"][^>]*id=[\'"]{re.escape(iq_id)}[\'"][^>]*>(.*?)</iq>',
                resp_xml, re.DOTALL
            )
        if not m:
            return None
        inner = m.group(1)
        # 找 <result item="..."> JSON </result>
        rm = re.search(r'<result[^>]*>(.*?)</result>', inner, re.DOTALL)
        if not rm:
            return {"raw": inner[:500]}
        body_text = rm.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        try:
            return json.loads(body_text)
        except Exception:
            return {"raw": body_text[:500]}

    # ---- 高階 helper(基於 52 個 IQ name) ----
    # 注意:SDK 區分兩種 stanza:
    #   getXmppqIqObject → <iq> stanza (query/get/result 模式,有 response)
    #   getXmppMsgObject → <message> stanza (fire-and-forget,server 處理但 client 不一定有 response)
    # 已從 webimsdk-4.6.0.min.js source 確認 body 結構:

    # ── IQ stanza ──────────────────────────────────────

    def list_channels_by_last_msg_time(
        self,
        *,
        last_msg_time_ms: int = 0,
        limit: int = 50,
        unread: int = 0,       # UnReadChannelType: 0=全部, 1=未讀, 2=已讀
        asc_sort: bool = True,
        ch_types: Optional[List[int]] = None,
        with_members: Optional[bool] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """對話列表(按最後訊息時間)— ✅ body 從 SDK source 確認 + 實機修正。

        SDK source(webimsdk.js):
          UnReadChannelType = {ALL_CHANNEL_TYPE:0, UNREAD_MESSAGE_CHANNEL:1, READ_MESSAGE_CHANNEL:2}
          SortOrder = {ASCEDING:1, DESCENDING:2}
          body = {ascSort:bool, lastMsgTime:int, unread:enum, count:int(-100~100,非0), [chType, withMembers]}

        2026-05-15 實機修正:unread 是 enum number 不是 bool,boolean 會返 returnCode=1001。
        """
        # count 限制 -100~100 非 0
        if limit == 0:
            limit = 50
        limit = max(-100, min(100, limit))
        body: Dict[str, Any] = {
            "ascSort": asc_sort,
            "lastMsgTime": last_msg_time_ms,
            "unread": int(unread),  # 必須是 0/1/2
            "count": limit,
        }
        if ch_types:
            body["chType"] = ch_types
        if with_members is not None:
            body["withMembers"] = with_members
        resp, err = self.iq("list_channels_by_lastmsgtime", body, iq_type="get")
        if err:
            return [], err
        if not isinstance(resp, dict):
            return [], f"unexpected resp type: {type(resp).__name__}"
        rc = resp.get("returnCode")
        if rc not in (0, None):
            return [], f"returnCode={rc} msg={resp.get('errorDescript', resp)}"
        # server 返 'channels' key(實機驗證 2026-05-15)— 不是 'result'
        channels = resp.get("channels", resp.get("result", []))
        return channels if isinstance(channels, list) else [], ""

    def get_user_unread_channels(self) -> Tuple[Dict[str, Any], str]:
        """未讀頻道清單 — ✅ 實機 body={} → {returnCode, totalUnread, result:[]}"""
        return self.iq("get_user_unread_channels", {}, iq_type="get")

    def clear_user_unread_count(
        self,
        channel_id: str = "",
    ) -> Tuple[Dict[str, Any], str]:
        """清未讀計數 — ⚠️ UNVERIFIED body 結構。"""
        body = {"chID": channel_id} if channel_id else {}
        return self.iq("clear_user_unread_count", body, iq_type="set")

    def query_message_read_count(
        self,
        channel_id: str,
        *,
        msg_id: str = "",
        read_or_unread: int = 0,
    ) -> Tuple[Dict[str, Any], str]:
        """訊息已讀數 — ✅ SDK source 確認 {chID, msgID, readOrUnread}。"""
        body: Dict[str, Any] = {"chID": channel_id, "readOrUnread": read_or_unread}
        if msg_id:
            body["msgID"] = msg_id
        return self.iq("query_message_read_count", body, iq_type="get")

    def query_message_read_users(
        self,
        channel_id: str,
        *,
        last_send_time: int = 0,
        count: int = 0,
    ) -> Tuple[Dict[str, Any], str]:
        """訊息已讀用戶列表 — ✅ SDK source 確認。"""
        body: Dict[str, Any] = {"chID": channel_id}
        if last_send_time:
            body["lastSendTime"] = last_send_time
        if count > 0:
            body["count"] = count
        return self.iq("query_message_read_users", body, iq_type="get")

    def query_message(
        self,
        channel_id: str,
        *,
        after_n: int = -20,
    ) -> Tuple[Dict[str, Any], str]:
        """拉訊息歷史 — ⭐ BOSH 用 WITH-prefix chID(2026-05-20 實機 Chrome XHR hook 驗證)。

        Yahoo SDK 對「正規(button click 建)」channel send_message body chID =
        'yahoo-bid-logbot1:y{seller}:y{buyer}' — WITH prefix,seller first。
        所有 BOSH IQ 也用 WITH prefix。
        """
        body = {"chID": channel_id, "afterN": after_n}
        return self.iq("juiker:iq:queryMessage", body, iq_type="get")

    def get_channel_profile(self, channel_id: str) -> Tuple[Dict[str, Any], str]:
        """⭐ BOSH 用 WITH-prefix chID(實機驗證,returnCode=0 + subject='yahoo bid single')。"""
        return self.iq("juiker:iq:getChannelProfile", {"chID": channel_id}, iq_type="get")

    def query_channel_read_info(self, channel_id: str) -> Tuple[Dict[str, Any], str]:
        """⭐ BOSH 用 WITH-prefix chID(實機驗證)。"""
        return self.iq("juiker:iq:queryChannelReadInfo", {"chID": channel_id}, iq_type="get")

    def channel_user_active(
        self,
        channel_id: str,
        is_active: bool = True,
    ) -> Tuple[Dict[str, Any], str]:
        """進出頻道(觸發已讀)— ⭐ BOSH 用 WITH-prefix chID.

        對 channel 不存在或 user 不是 member,returnCode=1106 'UserID is not in the channel'.
        新買家必須先由 Yahoo「即時通 button click」流程建 channel(走 bootstrap),
        否則 BOSH 建的 shadow channel 連 SDK 自己都拒絕 send.
        """
        body = {"chID": channel_id, "isActive": is_active}
        return self.iq("channel_user_active", body, iq_type="set")

    # ── message stanza(SDK 用 getXmppMsgObject)──
    # v6.0.83 修正:從 SDK source 確認這些操作用 message stanza 不是 IQ。
    # 本實作用 .msg() helper 構造 <message> 而非 <iq>。

    def mark_read(
        self,
        channel_id: str,
        mark_ts_ms: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """標記已讀 — ✅ message stanza + body={chID, markTS}。⭐ WITH-prefix chID。"""
        if mark_ts_ms is None:
            mark_ts_ms = int(time.time() * 1000)
        body = {"chID": channel_id, "markTS": mark_ts_ms}
        return self.msg("mark_read", body)

    def mark_read_all(self) -> Tuple[Dict[str, Any], str]:
        """全部已讀 — ✅ message stanza body={}"""
        return self.msg("mark_read_all", {})

    def mark_read_with_msg_ids(
        self,
        channel_id: str,
        msg_ids: List[str],
    ) -> Tuple[Dict[str, Any], str]:
        """⚠️ UNVERIFIED body 結構,猜測 {chID, msgIDs}。"""
        body = {"chID": channel_id, "msgIDs": msg_ids}
        return self.msg("mark_read_msgids", body)

    def recall_message(
        self,
        msg_ids: List[str],
        silent_mode: bool = False,
    ) -> Tuple[Dict[str, Any], str]:
        """撤回訊息 — ✅ SDK source 確認 message stanza + body={msgIDs, silentMode}。

        注意 msg_ids 是「全局唯一 msgID 列表」,不需要 chID。
        """
        body = {"msgIDs": msg_ids, "silentMode": silent_mode}
        return self.msg("recall_messages", body)

    def delete_message(self, msg_id: str) -> Tuple[Dict[str, Any], str]:
        """刪訊息 — ✅ SDK source 確認 message stanza + body={msgID}。"""
        return self.msg("delete_message", {"msgID": msg_id})

    def delete_channel_messages(self, channel_id: str) -> Tuple[Dict[str, Any], str]:
        """刪頻道訊息 — ✅ message stanza,body 推測 {chID}。"""
        return self.msg("delete_channel_messages", {"chID": channel_id})

    def forward_messages(
        self,
        target_channel_id: str,
        msg_ids: List[str],
    ) -> Tuple[Dict[str, Any], str]:
        """轉發訊息 — ✅ SDK source 確認 message stanza + body={chID:target, msgIDs}。"""
        body = {"chID": target_channel_id, "msgIDs": msg_ids}
        return self.msg("forward_messages", body)

    def broadcast_messages(
        self,
        channel_ids: List[str],
        messages: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], str]:
        """廣播訊息 — ✅ SDK source 確認 message stanza + body={chIDs, messages}。"""
        body = {"chIDs": channel_ids, "messages": messages}
        return self.msg("broadcast_messages", body)

    # ⚠️ create_channel BOSH stanza method **REMOVED** — 之前實機證明會建 SDK 拒絕 send 的
    # 「shadow channel」(returnCode=0 看起來成功,但 Yahoo SDK 自己 send 也 fire SEND_MESSAGES_END 99999999).
    # 正確的 channel 建立要走 bootstrap_channel(yahoo_im_channel_bootstrap.py):
    # 1) 純 HTTP 序列複製 SDK /chat 頁載入流程,或 2) Playwright 點 即時通 button.

    def send_text_message(
        self,
        channel_id: str,
        *,
        text: str,
        msg_type: int = 1,
        property_name: str = "auction2",
        device: str = "web",
        attach_order_id: str = "",
        role: int = 1,
    ) -> Tuple[Dict[str, Any], str]:
        """送純文字訊息給 Yahoo IM(走 BOSH <message> stanza).

        ✅ 2026-05-20 完整實機驗證(Chrome MCP + Python BOSH 對 Yahoo 官方建的 channel 雙路測):

        Yahoo SDK 在 /chat 頁點 send 真實送的 BOSH stanza body:
          {chID: "yahoo-bid-logbot1:y{seller}:y{buyer}" (WITH prefix, **seller first**),
           chType: "yahoo_single",
           msgType: 1,
           msgContent: JSON{property:auction2, type:text, value:{content}, version:1, device:web}}

        所有 BOSH ops (channel_user_active / get_channel_profile / query_message / mark_read)
        對「正規 channel」也用 WITH-prefix。

        ⚠️ 注意:**BOSH create_channel stanza 不能用!**(會建 SDK 拒絕 send 的 shadow channel)
        新買家正規 channel 建立 = Yahoo 「訂單頁→即時通 button」UI 流程,須用 Playwright
        bootstrap.參見 yahoo_im_channel_bootstrap.bootstrap_channel_via_order_page.

        Args:
            channel_id: yahoo-bid-logbot1:y{seller}:y{buyer}(WITH prefix, seller first)
            attach_order_id: 若不為空,額外 send 一個 order stanza 附帶訂單卡(主動發起場景).
            role: order stanza 內 role 值, 1 = 賣家(從訂單聯繫買家); 預設 1.

        ⚠️ 注意:不再有「假成功」 path —— STEP0 channel_user_active 必須回 rc=0,
        否則 return error,caller 該走 bootstrap_channel(HTTP-first + PW fallback)而不是
        繼續硬 send.這樣不會出現「BOSH OK 但對方收不到」的騙人成功狀態.
        """
        if not (channel_id and text):
            return {}, "send_text_message 缺必要參數"

        # ⭐ BOSH 用 WITH-prefix(SDK 實機抓 stanza 驗證)
        if not channel_id.startswith("yahoo-bid-logbot1:") and ":" in channel_id:
            channel_id = "yahoo-bid-logbot1:" + channel_id

        # ⭐ chID 方向反向 fallback:Yahoo server 存 chID 順序不固定
        # (取決於誰先創建,SDK 創 = lex-smaller first;buyer 創 = 用戶順序;等等)
        # 例:y9000000002:y9000000005 rc=1106 但 y9000000005:y9000000002 rc=0
        # STEP 0 channel_user_active 失敗時,自動 try 反向
        def _reverse_chid(chid: str) -> str:
            if chid.startswith("yahoo-bid-logbot1:"):
                _parts = chid.split(":")
                if len(_parts) == 3:
                    return f"{_parts[0]}:{_parts[2]}:{_parts[1]}"
            return chid

        # STEP 0: channel_user_active(觸發已讀 + 確認 channel 存在且我是 member)
        effective_chid = channel_id
        try:
            _resp0, err0 = self.channel_user_active(channel_id, is_active=True)
            rc0 = _resp0.get("returnCode") if isinstance(_resp0, dict) else None
            self.on_log(
                f"[BOSH-EXT] STEP0 channel_user_active(forward) returnCode={rc0!r} "
                f"err={err0!r}"
            )
            if rc0 == 1106:
                # 試反向
                reversed_chid = _reverse_chid(channel_id)
                if reversed_chid != channel_id:
                    _resp0, err0 = self.channel_user_active(reversed_chid, is_active=True)
                    rc0 = _resp0.get("returnCode") if isinstance(_resp0, dict) else None
                    self.on_log(
                        f"[BOSH-EXT] STEP0 channel_user_active(reversed) "
                        f"chID={reversed_chid[-40:]} returnCode={rc0!r}"
                    )
                    if rc0 == 0:
                        # 反向工作 → 整個 send 改用反向 chID
                        effective_chid = reversed_chid
                        self.on_log(f"[BOSH-EXT] 用反向 chID 繼續 send")

            if rc0 not in (0, None):
                err_desc = _resp0.get("errorDescript", "") if isinstance(_resp0, dict) else ""
                return {}, (
                    f"channel_user_active rc={rc0} {err_desc} — "
                    f"新買家需先 first-contact bootstrap"
                )
        except Exception as _e0:
            self.on_log(f"[BOSH-EXT] STEP0 channel_user_active 異常: {_e0}")
            return {}, f"channel_user_active 異常: {_e0}"

        # 之後全部用 effective_chid(可能反向過)
        channel_id = effective_chid

        # Stanza #1: text 訊息(WITH-prefix chID + chType="yahoo_single")
        msg_content_obj: Dict[str, Any] = {
            "property": property_name,
            "type": "text",
            "value": {"content": text},
            "version": 1,
            "device": device,
        }
        msg_content_str = json.dumps(
            msg_content_obj, ensure_ascii=False, separators=(",", ":"),
        )
        body: Dict[str, Any] = {
            "chID": channel_id,           # ⭐ WITH prefix
            "chType": "yahoo_single",
            "msgType": msg_type,
            "msgContent": msg_content_str,
        }
        _, err1 = self.msg("send_message", body, wait_response=False)
        if err1:
            return {}, f"text stanza 失敗: {err1}"

        # Stanza #2(主動發起場景): order 附加卡片
        if attach_order_id:
            order_content_obj: Dict[str, Any] = {
                "property": property_name,
                "type": "order",
                "value": {"id": str(attach_order_id), "role": int(role)},
                "version": 1,
                "device": device,
            }
            order_content_str = json.dumps(
                order_content_obj, ensure_ascii=False, separators=(",", ":"),
            )
            order_body: Dict[str, Any] = {
                "chID": channel_id,           # ⭐ WITH prefix
                "chType": "yahoo_single",
                "msgType": msg_type,
                "msgContent": order_content_str,
            }
            _, err2 = self.msg("send_message", order_body, wait_response=False)
            if err2:
                return {}, f"text stanza OK 但 order attach 失敗: {err2}"

        return {}, ""

    def send_reply_message(
        self,
        channel_id: str,
        *,
        msg_type: int = 1,
        reply_text: str = "",
        parent_msg_id: str,
        ch_type: int = 1,
        property_name: str = "auction2",
        device: str = "web",
    ) -> Tuple[Dict[str, Any], str]:
        """送 reply 訊息給 Yahoo IM,讓對方看到原生 reply 卡片 UI。

        实机适配(query_message 拿到 Yahoo 客戶端真實 reply 訊息結構):
            msgContent = {
              "property": "auction2",
              "type": "text",
              "value": {"content": reply_text},
              "replyMsgId": <parent.msgID>,   ← 關鍵!嵌在 msgContent 內
              "version": 1,
              "device": "web"
            }
            extInfo = null  ← 不用 SDK source 的 extInfo.reply 路徑
            parentMsgID = null  ← 不用 stanza body 字段

        ⚠️ 跟 SDK source `sendReplyMessage` 邏輯不同 — 那是 SDK 內部抽象,
        實際送出的 BOSH stanza msgContent 內就帶 replyMsgId。
        """
        if not (channel_id and reply_text and parent_msg_id):
            return {}, "send_reply_message 缺必要參數"

        # 構造 Yahoo 真實格式的 msgContent JSON
        msg_content_obj: Dict[str, Any] = {
            "property": property_name,
            "type": "text",
            "value": {"content": reply_text},
            "replyMsgId": parent_msg_id,
            "version": 1,
            "device": device,
        }
        msg_content_str = json.dumps(
            msg_content_obj, ensure_ascii=False, separators=(",", ":"),
        )

        body: Dict[str, Any] = {
            "chID": channel_id,
            "chType": ch_type,
            "msgType": msg_type,
            "msgContent": msg_content_str,
        }
        return self.msg("send_message", body, wait_response=False)


# ── 高階便利函數(每次都新建一個 BOSHSession,適合單一操作) ──


def bosh_recall_messages(
    profile_dir: Path,
    msg_ids: List[str],
    *,
    silent_mode: bool = False,
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """純 HTTP 撤回訊息(注意 msg_ids 是全局唯一 ID,不需 chID)。"""
    try:
        with BOSHSession(profile_dir, on_log=on_log) as sess:
            resp, err = sess.recall_message(msg_ids, silent_mode)
            if err:
                return False, err
            rc = resp.get("returnCode") if isinstance(resp, dict) else None
            if rc == 0:
                return True, "recall OK"
            return False, f"recall returnCode={rc} resp={resp}"
    except Exception as e:
        return False, f"recall 異常: {e}"


def bosh_mark_read_strict(
    profile_dir: Path,
    channel_id: str,
    *,
    mark_ts_ms: Optional[int] = None,
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """純 HTTP 標記已讀(mark_read IQ,跟既有 channel_user_active 不同)。"""
    try:
        with BOSHSession(profile_dir, on_log=on_log) as sess:
            resp, err = sess.mark_read(channel_id, mark_ts_ms)
            if err:
                return False, err
            rc = resp.get("returnCode") if isinstance(resp, dict) else None
            if rc == 0:
                return True, "mark_read OK"
            return False, f"mark_read returnCode={rc}"
    except Exception as e:
        return False, f"mark_read 異常: {e}"


def bosh_list_channels(
    profile_dir: Path,
    *,
    last_msg_time_ms: int = 0,
    limit: int = 50,
    on_log: Optional[LogFn] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """純 HTTP 拉對話列表(按最後訊息時間)。"""
    try:
        with BOSHSession(profile_dir, on_log=on_log) as sess:
            return sess.list_channels_by_last_msg_time(
                last_msg_time_ms=last_msg_time_ms, limit=limit,
            )
    except Exception as e:
        return [], f"list_channels 異常: {e}"


def bosh_get_unread_summary(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
) -> Tuple[Dict[str, Any], str]:
    """純 HTTP 拉紅點未讀(已實證 IQ)。"""
    try:
        with BOSHSession(profile_dir, on_log=on_log) as sess:
            return sess.get_user_unread_channels()
    except Exception as e:
        return {}, f"unread 異常: {e}"
