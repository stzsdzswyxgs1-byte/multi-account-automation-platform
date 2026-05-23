"""TG Forum 體驗改造 — 帳號選單 + 客戶列表 + 完整聊天記錄 (v6.0.83)

整個 Yahoo IM 在 TG 內可瀏覽:
  /accounts           — 列所有 Yahoo 帳號 (inline keyboard)
  /buyers <acc>       — 列該帳號所有 active 對話 (按未讀數排序)
  /history <chat_id>  — 顯示完整聊天記錄

inline keyboard 流程:
  /accounts → 24 個帳號按鈕
  → 點某帳號 → 列該帳號的 active buyer 清單(顯示 chID/未讀數)
  → 點某 buyer → 顯示最近 50 條訊息 + reply 按鈕
  → 點 reply → force_reply prompt → 輸入內容 → 自動 send 給該 buyer

整合方式:
  在 telegram_bot.on_message 內判斷 /accounts /buyers /history 命令呼叫對應 handler。
  callback_query 處理 inline button 點擊。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


class TGForumMenu:
    """TG bot 命令面板:選 Yahoo 帳號 → 列客戶 → 看聊天記錄 → reply。

    需要既有 telegram_bot 提供 send_message_with_buttons 和 callback_query 路由。
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        on_log: Optional[LogFn] = None,
        acl=None,  # TGUserACL instance(可選,沒提供時不過濾)
    ):
        self.base_dir = Path(base_dir)
        self.on_log = on_log or (lambda *_: None)
        self.acl = acl
        # callback_data 反查緩存:把長 chID 縮為 short token,放在 button data
        # callback_data 限制 64 字節,而 chID 可能 60+ 字
        # v6.0.83:per-user token cache(每個 user 自己的 token,避免跨用戶串)
        self._cb_cache: Dict[str, Dict[str, str]] = {}
        self._cb_counter = 0

    def _short_token(self, user_id: str, value: str) -> str:
        """為 callback_data 生成 short token(per-user)。"""
        ucache = self._cb_cache.setdefault(str(user_id), {})
        for tok, val in ucache.items():
            if val == value:
                return tok
        self._cb_counter += 1
        tok = f"t{self._cb_counter:06d}"
        ucache[tok] = value
        # 限制每 user cache size
        if len(ucache) > 500:
            keys = sorted(ucache.keys())[:250]
            for k in keys:
                ucache.pop(k, None)
        return tok

    def _resolve_token(self, user_id: str, tok: str) -> str:
        ucache = self._cb_cache.get(str(user_id)) or {}
        return ucache.get(tok, "")

    # ─── /accounts ───

    def list_accounts_keyboard(self, user_id: str) -> Dict[str, Any]:
        """列出此 TG user 允許訪問的 Yahoo 帳號(ACL 過濾)。"""
        try:
            from .accounts import load_accounts
            accs = load_accounts() or []
        except Exception as e:
            return {"inline_keyboard": [[{"text": f"err: {e}", "callback_data": "noop"}]]}

        all_names = [a.get("profile_id") or a.get("name") or "" for a in accs]
        all_names = [n for n in all_names if n]

        # ACL 過濾
        if self.acl:
            allowed = self.acl.get_allowed_accounts(user_id, all_names)
        else:
            allowed = all_names  # 沒 ACL 全顯示(向後相容)

        buttons: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for name in allowed:
            tok = self._short_token(user_id, f"acc|{name}")
            label = f"📦 {name}"
            row.append({"text": label[:20], "callback_data": f"fm:acc:{tok}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if not buttons:
            buttons.append([{"text": "(無可訪問帳號 — 請聯絡主管)", "callback_data": "noop"}])
        return {"inline_keyboard": buttons}

    def render_accounts_message(self, user_id: str) -> Tuple[str, Dict[str, Any]]:
        kb = self.list_accounts_keyboard(user_id)
        # 顯示用戶資訊
        user_info = ""
        if self.acl:
            entry = self.acl.get_user_entry(user_id)
            if entry:
                name = entry.get("name", "")
                admin = " (admin)" if entry.get("is_admin") else ""
                user_info = f"👤 {name}{admin}\n\n" if name else ""
        text = (
            f"{user_info}🛍 **Yahoo 拍賣 IM 帳號清單**\n\n"
            "點選帳號查看買家對話列表。\n"
            "每個帳號的所有未讀訊息會列出可逐條處理。"
        )
        return text, kb

    # ─── /buyers (拉某帳號的 active 對話) ───

    def list_buyers_for_account(
        self,
        user_id: str,
        profile_id: str,
        *,
        limit: int = 30,
    ) -> Tuple[str, Dict[str, Any]]:
        """拉某帳號的 active buyer 清單。"""
        # ACL 檢查
        if self.acl and not self.acl.can_access(user_id, profile_id):
            return f"⚠️ 你無權訪問 `{profile_id}` 對話", {"inline_keyboard": []}

        profile_dir = self.base_dir / "profiles" / profile_id
        if not profile_dir.exists():
            return f"⚠️ profile {profile_id} 不存在", {"inline_keyboard": []}

        err1 = ""
        err2 = ""
        channels = []
        unread_resp = {}
        try:
            from .yahoo_im_bosh_ext import BOSHSession
            with BOSHSession(profile_dir, on_log=self.on_log) as sess:
                channels, err1 = sess.list_channels_by_last_msg_time(
                    last_msg_time_ms=0, limit=limit, asc_sort=False,
                )
                unread_resp, err2 = sess.get_user_unread_channels()
        except Exception as e:
            # 區分 cookies/JWT 失敗 vs 真的沒對話
            err_text = str(e)
            hint = ""
            if "JWT" in err_text or "wssid" in err_text or "cookie" in err_text.lower():
                hint = "\n\n💡 此帳號 cookies/wssid 失效,需要先讓監控跑一輪刷新 cookies cache,或重新登入 Yahoo。"
            return (
                f"⚠️ **{profile_id}** BOSH 連線失敗\n\n`{err_text[:300]}`{hint}",
                {"inline_keyboard": [[{"text": "« 返回帳號列表", "callback_data": "fm:back:accounts"}]]}
            )

        unread_map: Dict[str, int] = {}
        if isinstance(unread_resp, dict):
            for entry in (unread_resp.get("result") or []):
                if isinstance(entry, dict):
                    cid = str(entry.get("chID") or "")
                    cnt = int(entry.get("unread") or entry.get("unreadCount") or 0)
                    if cid:
                        unread_map[cid] = cnt

        # 先拿自己 user.id 才能識別 chID 內哪段是「對方」
        my_id_lower = ""
        try:
            from .yahoo_im_jwt import fetch_im_user_info
            me_info, _ = fetch_im_user_info(profile_dir)
            my_id_lower = ((me_info or {}).get("id") or "").lower()
        except Exception:
            pass

        def _extract_buyer(cid: str) -> str:
            """從 chID 內取對方 Y-id(非 my_id 那一段)。"""
            parts = cid.split(":")
            # parts[0] = "yahoo-bid-logbot1",後面是 2 個 user-id
            for p in parts[1:]:
                if p and p.lower() != my_id_lower:
                    return p
            return parts[-1] if parts else ""

        # v6.0.83 修正:批次拉所有 buyer 暱稱 — buyer 識別用 _extract_buyer
        nickname_map: Dict[str, str] = {}
        try:
            buyer_yids = []
            for _ch in channels[:limit]:
                if not isinstance(_ch, dict):
                    continue
                _cid = str(_ch.get("chID") or "")
                _by = _extract_buyer(_cid)
                if _by:
                    _by_upper = _by.upper() if _by.startswith("y") else _by
                    if _by_upper not in buyer_yids:
                        buyer_yids.append(_by_upper)
            if buyer_yids:
                from .im_http_ops import _build_session
                session, _wssid, _e = _build_session(profile_dir)
                if session:
                    ids_csv = ",".join(buyer_yids[:30])
                    r = session.get(
                        f"https://tw.bid.yahoo.com/fe/api/im/users?userIds={ids_csv}",
                        timeout=10,
                    )
                    if r.status_code == 200:
                        users = (r.json() or {}).get("users") or []
                        for u in users:
                            if isinstance(u, dict):
                                uid = (u.get("userId") or u.get("id") or "").upper()
                                nick = u.get("nickname") or u.get("name") or ""
                                if uid and nick:
                                    nickname_map[uid] = nick
        except Exception as _e_nick:
            self.on_log(f"[TG-MENU] 批次拉暱稱失敗(不阻塞): {_e_nick}")

        if not channels:
            # 區分:err1 非空 → IQ 失敗;err1 空 → 真的沒對話
            if err1:
                return (
                    f"⚠️ **{profile_id}** 拉對話列表失敗\n\n`{err1[:300]}`\n\n"
                    "💡 可能 BOSH IQ stanza 結構需校正,或此帳號 wssid 過期需重新登入",
                    {"inline_keyboard": [[{"text": "« 返回帳號列表", "callback_data": "fm:back:accounts"}]]}
                )
            return (
                f"📭 **{profile_id}** 沒有 active 對話(總未讀: {unread_resp.get('totalUnread', 0) if isinstance(unread_resp, dict) else 0})",
                {"inline_keyboard": [[{"text": "« 返回帳號列表", "callback_data": "fm:back:accounts"}]]}
            )

        # build buttons
        buttons: List[List[Dict[str, str]]] = []
        lines = [f"🛍 **{profile_id}** 的對話 ({len(channels)} 個)\n"]
        for ch in channels[:limit]:
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("chID") or "")
            if not cid:
                continue
            # 結合 channels.unreadCount(內部欄位)和 get_user_unread_channels result
            unread = ch.get("unreadCount") or unread_map.get(cid, 0)
            last_ts = ch.get("lastMsgTime") or 0
            # v6.0.83 修正:從 chID 取對方 Y-id (非 my_id 那段) — chID 內 shop/buyer 位置不固定
            buyer = _extract_buyer(cid)
            buyer_upper = buyer.upper() if buyer.startswith("y") else buyer
            # 優先用暱稱,沒拿到就用 Y-id
            label_name = nickname_map.get(buyer_upper) or buyer_upper
            # lastMsg 內容預覽
            last_preview = ""
            last_content_raw = ch.get("lastMsgContent") or ""
            if isinstance(last_content_raw, str) and last_content_raw:
                try:
                    import json as _j
                    lc = _j.loads(last_content_raw)
                    lc_type = lc.get("type", "")
                    lc_val = lc.get("value") or {}
                    if lc_type == "text" and isinstance(lc_val, dict):
                        last_preview = (lc_val.get("content") or "")[:20]
                    elif lc_type == "image":
                        last_preview = "📷"
                    elif lc_type == "video":
                        last_preview = "🎬"
                    elif lc_type == "sticker":
                        last_preview = "😀"
                except Exception:
                    pass
            # 時間
            ts_str = ""
            if last_ts:
                try:
                    ts_str = time.strftime("%m/%d %H:%M", time.localtime(int(last_ts) / 1000))
                except Exception:
                    pass
            dot = f"🔴{unread}" if unread else "  "
            btn_label = f"{dot}{label_name[:12]}|{ts_str}"
            tok = self._short_token(user_id, f"buyer|{profile_id}|{cid}")
            buttons.append([{"text": btn_label[:40], "callback_data": f"fm:buyer:{tok}"}])
            lines.append(f"{dot} `{label_name}` {ts_str} {last_preview}")

        # 加返回鈕
        buttons.append([{"text": "« 返回帳號列表", "callback_data": "fm:back:accounts"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    # ─── /history (顯示完整聊天記錄) ───

    def show_history_for_buyer(
        self,
        user_id: str,
        profile_id: str,
        channel_id: str,
        *,
        limit: int = 20,
    ) -> Tuple[str, Dict[str, Any]]:
        """顯示某 buyer 的完整聊天記錄(最近 N 條)。"""
        if self.acl and not self.acl.can_access(user_id, profile_id):
            return f"⚠️ 你無權訪問 `{profile_id}` 對話", {"inline_keyboard": []}

        profile_dir = self.base_dir / "profiles" / profile_id
        if not profile_dir.exists():
            return f"⚠️ profile {profile_id} 不存在", {"inline_keyboard": []}

        try:
            from .yahoo_im_bosh_ext import BOSHSession
            with BOSHSession(profile_dir, on_log=self.on_log) as sess:
                resp, err = sess.query_message(channel_id, after_n=-limit)
                from .yahoo_im_jwt import fetch_im_user_info
                me_info, _ = fetch_im_user_info(profile_dir)
                my_id = (me_info or {}).get("id", "")
        except Exception as e:
            return f"⚠️ BOSH 查歷史失敗: {e}", {"inline_keyboard": []}

        msgs = resp.get("messages") if isinstance(resp, dict) else None
        if not isinstance(msgs, list) or not msgs:
            text = f"📭 對話 `{channel_id[-30:]}` 沒有訊息\n(err: {err})"
            buttons = [[{"text": "💬 直接 reply 文字", "callback_data": f"fm:reply:{self._short_token(user_id, 'reply|'+profile_id+'|'+channel_id)}"}]]
            return text, {"inline_keyboard": buttons}

        # v6.0.83 修正:msg 結構從實機 BOSH 抓的真實欄位
        #   msgID / senderID / sendTime / msgContent(JSON 字串)/ msgType / encrypted
        # parse msgContent JSON 後是 {property, type, value, version, device}
        import json as _json

        # v6.0.83 修正:從 chID 內取對方 Y-id (非 my_id 那段)
        my_lower = my_id.lower() if my_id else ""
        parts = channel_id.split(":")
        buyer_yid = ""
        for p in parts[1:]:
            if p and p.lower() != my_lower:
                buyer_yid = p
                break
        if not buyer_yid:
            buyer_yid = parts[-1] if parts else ""
        buyer_name = ""
        try:
            from .im_http_ops import _build_session
            session, _wssid, _e = _build_session(profile_dir)
            if session and buyer_yid:
                buyer_y_upper = buyer_yid.upper() if buyer_yid.startswith("y") else buyer_yid
                r = session.get(
                    f"https://tw.bid.yahoo.com/fe/api/im/users?userIds={buyer_y_upper}",
                    timeout=10,
                )
                if r.status_code == 200:
                    udata = r.json()
                    users = udata.get("users") or []
                    if users and isinstance(users[0], dict):
                        buyer_name = users[0].get("nickname") or users[0].get("name") or ""
        except Exception:
            pass

        header_name = buyer_name or (buyer_yid.upper() if buyer_yid else channel_id[-20:])

        # v6.0.83 修正:按 sendTime 升序排(舊在上、新在下)
        msgs_sorted = sorted(
            [m for m in msgs if isinstance(m, dict)],
            key=lambda m: int(m.get("sendTime") or m.get("ts") or 0),
        )
        # 只取最後 limit 條(最近的)
        msgs_sorted = msgs_sorted[-limit:]

        # 解析出 parsed list:每條 {ts_str, role, type, text_content, media_url, thumb_url, sticker_id}
        # 給上層決定是 inline 顯示 or 拆成多個 TG 訊息(媒體展開)
        parsed: List[Dict[str, Any]] = []
        for m in msgs_sorted:
            sender = m.get("senderID", "") or m.get("from", "") or ""
            ts = int(m.get("sendTime") or m.get("ts") or 0)
            ts_str = ""
            if ts:
                try:
                    ts_str = time.strftime("%m/%d %H:%M", time.localtime(ts / 1000))
                except Exception:
                    pass
            is_me = my_id and str(sender).lower() == my_id.lower()
            role = "💚 我" if is_me else f"👤 {buyer_name or (buyer_yid.upper() if buyer_yid else '對方')}"

            raw = m.get("msgContent", "") or m.get("content", "")
            entry: Dict[str, Any] = {
                "ts": ts, "ts_str": ts_str, "role": role,
                "type": "text", "text": "", "media_url": "", "thumb_url": "",
            }
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    cdict = _json.loads(raw)
                    t = cdict.get("type", "") or ""
                    v = cdict.get("value") or {}
                    entry["type"] = t
                    if t == "text" and isinstance(v, dict):
                        entry["text"] = (v.get("content") or "").strip()
                    elif t == "image" and isinstance(v, dict):
                        src = v.get("src") or v.get("origin") or {}
                        entry["media_url"] = src.get("url") if isinstance(src, dict) else ""
                        thumb = v.get("thumbnail") or {}
                        entry["thumb_url"] = thumb.get("url") if isinstance(thumb, dict) else ""
                    elif t == "video" and isinstance(v, dict):
                        src = v.get("src") or {}
                        entry["media_url"] = src.get("url") if isinstance(src, dict) else ""
                        # 用 resizeVideos 中第一個 mp4 URL(更穩定可播)
                        rv = v.get("resizeVideos") or []
                        if isinstance(rv, list) and rv and isinstance(rv[0], dict):
                            entry["media_url"] = rv[0].get("url") or entry["media_url"]
                        thumb = v.get("thumbnail") or {}
                        entry["thumb_url"] = thumb.get("url") if isinstance(thumb, dict) else ""
                    elif t == "sticker" and isinstance(v, dict):
                        entry["media_url"] = v.get("url", "")
                        entry["text"] = v.get("id", "")
                    elif t == "item" and isinstance(v, dict):
                        title = v.get("title") or v.get("name") or ""
                        entry["text"] = f"🛍 商品:{title[:30]}"
                    else:
                        entry["text"] = f"[{t or '未知'}]"
                except Exception:
                    entry["text"] = raw[:60]
            elif isinstance(raw, str):
                entry["text"] = raw[:80]
            parsed.append(entry)

        # 文字摘要 — text/sticker/item 顯示 inline,image/video 標 [展開]
        lines = [f"💬 **{header_name}** 聊天記錄(時間升序,最近 {len(parsed)} 條)\n"]
        for p in parsed:
            t = p["type"]
            if t == "text":
                lines.append(f"  {p['ts_str']} {p['role']}: {p['text'][:120]}")
            elif t == "image":
                lines.append(f"  {p['ts_str']} {p['role']}: 📷 [圖片] {(p['media_url'] or '')[:60]}")
            elif t == "video":
                lines.append(f"  {p['ts_str']} {p['role']}: 🎬 [視頻] {(p['media_url'] or '')[:60]}")
            elif t == "sticker":
                lines.append(f"  {p['ts_str']} {p['role']}: 😀 [貼圖 {p['text']}]")
            else:
                lines.append(f"  {p['ts_str']} {p['role']}: {p['text'][:80]}")

        # 暴露 parsed 給上層拿,讓 tg_conversation 拆出來發 TG sendPhoto/sendVideo
        if not hasattr(self, "_last_history_parsed"):
            self._last_history_parsed = {}
        self._last_history_parsed[user_id] = parsed

        buttons = [
            [{"text": "💬 reply 文字", "callback_data": f"fm:reply:{self._short_token(user_id, 'reply|'+profile_id+'|'+channel_id)}"}],
            [{"text": "📷 reply 圖片", "callback_data": f"fm:reply_img:{self._short_token(user_id, 'reply_img|'+profile_id+'|'+channel_id)}"}],
            [{"text": "🎬 reply 視頻", "callback_data": f"fm:reply_vid:{self._short_token(user_id, 'reply_vid|'+profile_id+'|'+channel_id)}"}],
            [{"text": "« 返回客戶列表", "callback_data": f"fm:back:buyers|{self._short_token(user_id, 'acc|'+profile_id)}"}],
        ]
        return "\n".join(lines), {"inline_keyboard": buttons}

    # ─── callback routing ───

    def handle_callback(
        self,
        user_id: str,
        data: str,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """處理 callback_data(per-user),返回 (新 message text, 新 inline keyboard)。"""
        if not data.startswith("fm:"):
            return None
        parts = data.split(":", 2)
        if len(parts) < 2:
            return None
        action = parts[1]
        tok_or_arg = parts[2] if len(parts) >= 3 else ""

        if action == "back":
            if tok_or_arg == "accounts":
                return self.render_accounts_message(user_id)
            if tok_or_arg.startswith("buyers|"):
                inner_tok = tok_or_arg[len("buyers|"):]
                v = self._resolve_token(user_id, inner_tok)
                if v.startswith("acc|"):
                    return self.list_buyers_for_account(user_id, v.split("|", 1)[1])
            return None

        v = self._resolve_token(user_id, tok_or_arg)
        if not v:
            return ("⚠️ token 已過期,請重新 /accounts", {"inline_keyboard": []})

        if action == "acc":
            if v.startswith("acc|"):
                profile_id = v.split("|", 1)[1]
                return self.list_buyers_for_account(user_id, profile_id)
        elif action == "buyer":
            if v.startswith("buyer|"):
                p = v.split("|", 2)
                if len(p) == 3:
                    return self.show_history_for_buyer(user_id, p[1], p[2])
        elif action == "reply":
            return ("💬 請直接回覆此訊息(內容會自動 send 給該對話)", {"force_reply": True, "selective": True})
        elif action == "reply_img":
            return ("📷 請傳一張圖片在這(會自動 send 給該對話)", {"force_reply": True, "selective": True})
        elif action == "reply_vid":
            return ("🎬 請傳一段視頻在這(會自動 send 給該對話)", {"force_reply": True, "selective": True})
        return None
