"""TG Forum Topic 整合 — 每個 Yahoo 對話 ↔ 一個 TG forum topic (v6.0.83+)

把整個 Yahoo IM 搬到 TG forum supergroup:
- 每個 Yahoo 買家對話 → 一個 TG forum topic
- Yahoo inbound 訊息 → forward 到對應 topic
- TG topic 內用戶回覆 → send 回 Yahoo IM
- 媒體雙向中轉(圖片/視頻 → upload pixelframe → send_image_message)
- 紅點/已讀同步(BOSH IQ mark_read)

Setup 要求:
1. 創建 Telegram supergroup,啟用 "Topics" (forum 模式)
2. 在 group 內把 bot 設為 admin(權限:manage topics, post messages)
3. settings.json:
     "tg_forum_chat_id": "-1001xxxxxxxxxxx"   # supergroup chat id
     "tg_forum_enabled": true

API 文檔:
- createForumTopic   https://core.telegram.org/bots/api#createforumtopic
- editForumTopic     https://core.telegram.org/bots/api#editforumtopic
- closeForumTopic    https://core.telegram.org/bots/api#closeforumtopic
- sendMessage with message_thread_id
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

import requests

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

# ── 存儲 conv_id/yahoo_chat_id ↔ topic_id 映射 ──


class TGForumStore:
    """JSON 持久化:Yahoo conv_key ↔ TG forum topic_id。

    conv_key 格式建議: "<profile_id>|<yahoo_chat_id>" (對應一個 Yahoo 買家對話)
    """

    def __init__(self, base_dir: Path):
        self.path = Path(base_dir) / "tg_forum_topics.json"
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        """v6.1:atomic write — 寫 .tmp 再 os.replace,防軟件 crash 中半損壞 json。"""
        try:
            import os as _os
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _os.replace(str(tmp_path), str(self.path))
        except Exception as e:
            log.warning("TGForumStore save failed: %s", e)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    def get_topic_id(self, conv_key: str) -> Optional[int]:
        with self._lock:
            entry = self._data.get(conv_key) or {}
            return entry.get("topic_id")

    def set_topic_id(
        self,
        conv_key: str,
        topic_id: int,
        *,
        title: str = "",
        profile_id: str = "",
        chat_id: str = "",
        info_msg_id: int = 0,
        forum_chat_id: str = "",  # v6.1:multi-tenant — 該 topic 在哪個 supergroup
    ) -> None:
        with self._lock:
            self._data[conv_key] = {
                "topic_id": topic_id,
                "title": title,
                "profile_id": profile_id,
                "chat_id": chat_id,
                "info_msg_id": info_msg_id,
                "forum_chat_id": forum_chat_id,
                "created_ts": time.time(),
            }
            self._save()

    def set_info_msg_id(self, conv_key: str, info_msg_id: int) -> None:
        """更新該 conv 的資訊卡 message_id(用於 editMessageText 時找回)。"""
        with self._lock:
            if conv_key in self._data:
                self._data[conv_key]["info_msg_id"] = info_msg_id
                self._save()

    def set_last_order_id(self, conv_key: str, order_id: str) -> None:
        """v6.1.27:記下該 conv 對應的訂單號(從訂單中心「聯繫買家」進來時設定).

        dispatch_topic_reply 對全新 channel 用 BOSH 兜底時,
        需要這個 order_id attach 訂單卡片,否則訊息不會送達買家.
        """
        if not order_id:
            return
        with self._lock:
            if conv_key in self._data:
                self._data[conv_key]["last_order_id"] = str(order_id)
                self._save()

    def touch_activity(self, conv_key: str, ts: Optional[float] = None) -> None:
        """更新 last_activity_ts — 每次 forward/push 訊息到 topic 都 call。

        用於 cleanup 邏輯:>N 天無活動的 topic 自動 delete 釋放配額。
        """
        with self._lock:
            if conv_key in self._data:
                self._data[conv_key]["last_activity_ts"] = ts or time.time()
                # 不每次 touch 都 save(I/O 太頻繁),只記憶體更新
                # cleanup_old_topics 之前會強制 flush 一次

    def flush(self) -> None:
        """強制把記憶體 state 寫回磁碟(touch_activity 不會即時寫)。"""
        with self._lock:
            self._save()

    def list_stale_topics(self, days_threshold: int) -> List[Tuple[str, Dict[str, Any]]]:
        """找出所有 last_activity_ts 早於 (now - days_threshold * 86400) 的 entry。

        Returns: [(conv_key, entry), ...]
        last_activity_ts 沒記過的 fallback 用 created_ts。

        v6.1:過濾掉系統 key(`__order_center__|*` / `__order_msg__|*`),
        否則 cleanup 跑了會誤刪訂單中心 topic。
        """
        cutoff = time.time() - days_threshold * 86400
        results = []
        with self._lock:
            for k, v in self._data.items():
                # v6.1:跳過系統 key,只 cleanup 一般買家 topic
                if k.startswith("__"):
                    continue
                ts = v.get("last_activity_ts") or v.get("created_ts") or 0
                if ts and ts < cutoff:
                    results.append((k, dict(v)))
        return results

    def remove_entry(self, conv_key: str) -> None:
        """從 store 移除整個 entry(delete topic 後呼叫)。"""
        with self._lock:
            if conv_key in self._data:
                del self._data[conv_key]
                self._save()

    # ── v6.1:訂單中心 topic 管理 ──
    # 每個 forum group(multi-tenant 場景下不同同事的 group 各一)有自己的訂單中心 topic
    # key 格式 "__order_center__|<group_chat_id>"
    def get_order_center_topic(self, group_chat_id: str) -> Optional[int]:
        return self.get_topic_id(f"__order_center__|{group_chat_id}")

    def set_order_center_topic(self, group_chat_id: str, topic_id: int) -> None:
        self.set_topic_id(
            f"__order_center__|{group_chat_id}",
            topic_id,
            title="📋 訂單中心",
            forum_chat_id=group_chat_id,
        )

    # v6.1.20:訂單中心置頂訊息 msg_id(讓重啟時可 edit 刷新)
    def get_order_center_pin_msg(self, group_chat_id: str) -> Optional[int]:
        with self._lock:
            entry = self._data.get(f"__order_center_pin__|{group_chat_id}") or {}
            return entry.get("msg_id")

    def set_order_center_pin_msg(self, group_chat_id: str, msg_id: int) -> None:
        with self._lock:
            self._data[f"__order_center_pin__|{group_chat_id}"] = {
                "msg_id": int(msg_id),
                "group_chat_id": group_chat_id,
            }
            self._save()

    # ── v6.1:訂單卡 msg_id 映射(讓 editMessage 找到原訊息)──
    # key 格式 "__order_msg__|<order_id>"
    def get_order_msg(self, order_id: str) -> Dict[str, Any]:
        """取訂單 → 訂單中心主卡 msg_id 映射。
        Returns: {topic_id, msg_id, group_chat_id, last_status, last_payment, last_updated_ts}
        """
        with self._lock:
            return dict(self._data.get(f"__order_msg__|{order_id}", {}))

    def set_order_msg(
        self, order_id: str, *,
        topic_id: int, msg_id: int, group_chat_id: str,
        status: str = "", payment: str = "",
    ) -> None:
        with self._lock:
            self._data[f"__order_msg__|{order_id}"] = {
                "topic_id": topic_id,
                "msg_id": msg_id,
                "group_chat_id": group_chat_id,
                "last_status": status,
                "last_payment": payment,
                "last_updated_ts": time.time(),
            }
            self._save()

    def list_active_order_msgs(self) -> List[Tuple[str, Dict[str, Any]]]:
        """列出所有訂單 msg 映射。Returns: [(order_id, entry), ...]"""
        with self._lock:
            return [
                (k.split("|", 1)[1], dict(v))
                for k, v in self._data.items()
                if k.startswith("__order_msg__|")
            ]

    def find_by_topic_id(self, topic_id: int) -> Tuple[str, Dict[str, Any]]:
        """反向找:用 topic_id 找 conv_key + entry。v6.1:跳過系統 key。"""
        with self._lock:
            for k, v in self._data.items():
                if k.startswith("__"):
                    continue
                if v.get("topic_id") == topic_id:
                    return k, dict(v)
        return "", {}

    def find_by_buyer(self, buyer_chat_id: str) -> List[Dict[str, Any]]:
        """找同個 buyer 在所有帳號的 topic — 跨帳號客戶識別用。v6.1:跳過系統 key。"""
        results = []
        bid_lower = (buyer_chat_id or "").lower().lstrip("y")
        with self._lock:
            for k, v in self._data.items():
                if k.startswith("__"):
                    continue
                cid = (v.get("chat_id") or "").lower().lstrip("y")
                if cid == bid_lower:
                    results.append(dict(v))
        return results

    def get_entry(self, conv_key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(conv_key) or {})

    # ── topic msg_id ↔ Yahoo msg_id mapping(供 reply 引用反查)──

    def add_msg_map(
        self,
        conv_key: str,
        topic_msg_id: int,
        yahoo_info: Dict[str, Any],
        *,
        max_keep: int = 200,
    ) -> None:
        """記 topic_msg_id ↔ Yahoo msg 資訊(yahoo_msg_id/msg_type/content/sender_*)。

        超過 max_keep 條時 LRU 淘汰最舊。
        """
        if not topic_msg_id:
            return
        with self._lock:
            entry = self._data.get(conv_key)
            if not entry:
                return
            mm = entry.get("msg_map") or {}
            mm[str(topic_msg_id)] = yahoo_info
            if len(mm) > max_keep:
                # 按插入順序砍舊(Python 3.7+ dict 保序)
                excess = len(mm) - max_keep
                for k in list(mm.keys())[:excess]:
                    mm.pop(k, None)
            entry["msg_map"] = mm
            self._save()

    def get_msg_map(self, conv_key: str, topic_msg_id: int) -> Dict[str, Any]:
        with self._lock:
            entry = self._data.get(conv_key) or {}
            mm = entry.get("msg_map") or {}
            return dict(mm.get(str(topic_msg_id)) or {})


# ── TG Forum API wrapper ──


class TGForumBot:
    """Telegram Bot API forum topic 包裝。

    使用既有 telegram_bot 的 token / requests session,只是調用 forum 相關 API。
    """

    def __init__(
        self,
        token: str,
        forum_chat_id: str,
        *,
        on_log: Optional[LogFn] = None,
        timeout: int = 15,
    ):
        self.token = token
        self.forum_chat_id = forum_chat_id  # supergroup -100... ID
        self.on_log = on_log or (lambda *_: None)
        self.timeout = timeout
        self._base = f"https://api.telegram.org/bot{token}"
        # polling 狀態
        self._poll_running = False
        self._poll_thread: Optional[threading.Thread] = None
        # offset 持久化:避免重啟時重播歷史 update
        self._offset_path = Path(".") / "runtime" / f"forum_bot_offset_{token[:10]}.txt"
        self._poll_offset = self._load_offset()
        # v6.1:KV 中轉模式(多軟件 instance 共存)
        self._kv_poller = None

    def _load_offset(self) -> int:
        try:
            if self._offset_path.exists():
                return int(self._offset_path.read_text().strip() or 0)
        except Exception:
            pass
        return 0

    def _save_offset(self) -> None:
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.write_text(str(self._poll_offset), encoding="utf-8")
        except Exception:
            pass
        # callback:收到 forum message 時觸發
        # (topic_id, text, msg_id, from_user_id, photo_fid, video_fid, reply_to_text, reply_to_msg_id)
        self.on_forum_message: Optional[Callable[..., None]] = None

    # ─── Telegram getUpdates polling(forum bot 沒 webhook 才能用)───

    def setup_commands_menu(self) -> None:
        """v6.2:設輸入框旁邊的命令選單(打 / 自動補完,點選即執行)。

        TG 內無論在主聊天或 topic 內,使用者打 / 就會看到下拉提示。
        """
        commands = [
            {"command": "orders",      "description": "📋 查待出貨訂單(自動帶聯繫買家按鈕)"},
            {"command": "myaccounts",  "description": "👤 看我綁定的帳號"},
            {"command": "myid",        "description": "📇 顯示 TG ID / chat_id(綁定用)"},
            {"command": "help",        "description": "❓ 指令說明"},
        ]
        try:
            requests.post(
                f"{self._base}/setMyCommands",
                json={"commands": commands},
                timeout=10,
            )
            requests.post(
                f"{self._base}/setChatMenuButton",
                json={"menu_button": {"type": "commands"}},
                timeout=10,
            )
            self.on_log("[TG-FORUM] commands menu 已設定 (/orders /myaccounts /myid /help)")
        except Exception as e:
            self.on_log(f"[TG-FORUM] setup_commands_menu 異常: {e}")

    def start_polling(self) -> None:
        """啟動背景 polling thread,接 supergroup forum 訊息。

        v6.1:KV 中轉模式(多軟件 instance 共存,避開 409 衝突)
        - settings.json `forum_bot_use_kv: true` 開啟
        - 需 worker 端配 forum bot webhook(否則 KV poll 永遠空)
        - 預設 false:走 direct getUpdates(單一 instance 模式)
        """
        # v6.2:首次啟動順手設 commands 選單
        self.setup_commands_menu()
        if self._poll_running:
            return
        # 看 settings 決定模式
        kv_ok = False
        try:
            from .accounts import load_settings
            st = load_settings() or {}
            if bool(st.get("forum_bot_use_kv", False)):
                from .tg_kv_poller import KvPoller, load_relay_config
                cfg = load_relay_config()
                if cfg.get("user_id") and cfg.get("worker_url") and cfg.get("api_key"):
                    self._kv_poller = KvPoller("forum", cfg, self.on_log)
                    if self._kv_poller.is_configured:
                        kv_ok = True
        except Exception as e:
            self.on_log(f"[TG-FORUM] KV 模式初始化失敗,fallback direct: {e}")
            self._kv_poller = None

        if kv_ok:
            self._poll_running = True
            self._poll_thread = threading.Thread(target=self._poll_loop_kv, daemon=True)
            self._poll_thread.start()
            self.on_log(
                f"[TG-FORUM] polling 已啟動(KV 中轉模式,綁定 TG ID={self._kv_poller.user_id})"
            )
            return

        # Direct getUpdates 模式(回退)— 多 instance 同時跑會撞 409
        try:
            r = requests.get(f"{self._base}/getWebhookInfo", timeout=10)
            wh = (r.json().get("result") or {}).get("url", "")
            if wh:
                self.on_log(f"[TG-FORUM] ⚠️ bot 已設 webhook 但 KV 未配置,polling 會 409")
                self.on_log(f"[TG-FORUM] 解法:在 tg_relay_config.json 設 user_id,或拆掉 webhook")
                return
        except Exception as e:
            self.on_log(f"[TG-FORUM] getWebhookInfo 異常: {e}")
            return
        self._poll_running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self.on_log("[TG-FORUM] polling 已啟動(direct getUpdates,單一 instance 模式)")

    def _poll_loop_kv(self) -> None:
        """v6.2:走統一 run_kv_poll_loop(指數退避 + 全局網路健康 + log 降噪)。"""
        from .network_health import run_kv_poll_loop
        run_kv_poll_loop(
            poller=self._kv_poller,
            poller_name="KV-FORUM",
            on_update=self._handle_update,
            on_log=self.on_log,
            is_running=lambda: self._poll_running,
        )

    # callback_query handler — 接 forum 內 inline button 點擊
    on_callback_query: Optional[Callable[[str, str, int], None]] = None

    def stop_polling(self) -> None:
        self._poll_running = False

    def _poll_loop(self) -> None:
        backoff = 1.0
        while self._poll_running:
            try:
                params = {"timeout": 30, "offset": self._poll_offset}
                # v6.0.83:同時接 message + callback_query(讓 topic 內 button 能點)
                params["allowed_updates"] = json.dumps(["message", "callback_query"])
                r = requests.get(
                    f"{self._base}/getUpdates", params=params, timeout=40,
                )
                j = r.json()
                if not j.get("ok"):
                    self.on_log(f"[TG-FORUM] getUpdates fail: {j.get('description','')}")
                    time.sleep(min(backoff, 60.0))
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = 1.0
                updates = j.get("result") or []
                for upd in updates:
                    self._poll_offset = max(self._poll_offset, upd.get("update_id", 0) + 1)
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        self.on_log(f"[TG-FORUM] handle_update 異常: {e}")
                # 持久化 offset(避免重啟重播)
                if updates:
                    self._save_offset()
            except Exception as e:
                self.on_log(f"[TG-FORUM] poll 異常: {e}")
                time.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)

    def _handle_update(self, upd: Dict[str, Any]) -> None:
        """從 supergroup 拿到 message / callback_query → 過濾出 forum chat → 觸發 callback。"""
        # callback_query(inline button 點擊)
        cbq = upd.get("callback_query")
        if cbq:
            try:
                self._handle_callback_query(cbq)
            except Exception as e:
                self.on_log(f"[TG-FORUM] callback_query 異常: {e}")
            return

        msg = upd.get("message") or {}
        if not msg:
            return
        chat = msg.get("chat") or {}
        msg_chat_id = str(chat.get("id", ""))
        # 多同事:認主 forum + employees 內所有同事 group
        # 也接 General topic 內的訊息(沒 message_thread_id 也走 handler 給 /bind 用)
        from_user = msg.get("from") or {}
        if from_user.get("is_bot"):
            return  # 過濾自己 bot 訊息避免迴圈

        is_main_forum = msg_chat_id == str(self.forum_chat_id)
        is_employee_forum = False
        try:
            from . import employees as _emp
            if _emp.find_employee_by_forum_chat_id(msg_chat_id):
                is_employee_forum = True
        except Exception:
            pass
        if not (is_main_forum or is_employee_forum):
            return

        thread_id = msg.get("message_thread_id")
        # 沒 thread_id(group 主聊天而非 topic 內)→ 仍 dispatch,讓 /bind 等指令能用
        if not thread_id:
            thread_id = 0

        text = (msg.get("text") or "").strip()
        if not text:
            text = (msg.get("caption") or "").strip()
        msg_id = int(msg.get("message_id", 0))
        from_user_id = str(from_user.get("id", ""))

        # photo / video file_id
        photo_fid = None
        video_fid = None
        photos = msg.get("photo") or []
        if photos:
            photo_fid = photos[-1].get("file_id")
        video = msg.get("video")
        if video:
            video_fid = video.get("file_id")
        anim = msg.get("animation")
        if anim and not video_fid:
            video_fid = anim.get("file_id")

        # v6.1.53:用戶選「原檔發送」時 TG 用 document 類型不是 video
        # 看 mime_type 判斷:video/* → 當視頻處理(不壓縮的高清原檔),image/* → 當圖片
        # 修「視頻選原檔發送 → ❌ reply 內容為空」bug
        doc = msg.get("document")
        if doc and not photo_fid and not video_fid:
            _mime = str(doc.get("mime_type", "") or "").lower()
            _fid = doc.get("file_id")
            if _fid:
                if _mime.startswith("video/"):
                    video_fid = _fid
                elif _mime.startswith("image/"):
                    photo_fid = _fid

        # reply_to_message
        reply_to_text = ""
        reply_to_msg_id = 0
        rto = msg.get("reply_to_message") or {}
        if rto and rto.get("message_thread_id") == thread_id:
            if not rto.get("forum_topic_created"):
                reply_to_msg_id = int(rto.get("message_id", 0) or 0)
                reply_to_text = (rto.get("text") or rto.get("caption") or "").strip()

        if self.on_forum_message:
            try:
                # v6.1:傳 chat_id 給 callback 讓多 group 場景能識別
                self.on_forum_message(
                    int(thread_id), text, msg_id, from_user_id,
                    photo_fid, video_fid, reply_to_text, reply_to_msg_id,
                    msg_chat_id,  # ← 新增
                )
            except TypeError:
                # 向下兼容舊 callback 簽名(沒接 chat_id 參數)
                try:
                    self.on_forum_message(
                        int(thread_id), text, msg_id, from_user_id,
                        photo_fid, video_fid, reply_to_text, reply_to_msg_id,
                    )
                except Exception as e:
                    self.on_log(f"[TG-FORUM] on_forum_message callback 異常: {e}")
            except Exception as e:
                self.on_log(f"[TG-FORUM] on_forum_message callback 異常: {e}")

    def _handle_callback_query(self, cbq: Dict[str, Any]) -> None:
        """forum 內 inline button 點擊處理 — 觸發 on_callback_query callback。"""
        cbq_id = str(cbq.get("id", ""))
        data = (cbq.get("data") or "").strip()
        msg = cbq.get("message") or {}
        chat = msg.get("chat") or {}
        msg_chat_id = str(chat.get("id", ""))
        message_id = int(msg.get("message_id", 0))

        # 過濾非 forum 群組訊息
        if msg_chat_id != str(self.forum_chat_id):
            return

        # 消按鈕 loading 動畫
        try:
            requests.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": cbq_id}, timeout=5,
            )
        except Exception:
            pass

        if not data or not self.on_callback_query:
            return
        try:
            self.on_callback_query(data, msg_chat_id, message_id)
        except Exception as e:
            self.on_log(f"[TG-FORUM] on_callback_query 異常: {e}")

    def _post(self, method: str, payload: Dict[str, Any], _retry: int = 0) -> Tuple[Dict[str, Any], str]:
        """TG Bot API call 帶 429 retry-after honor。

        業界限制:supergroup 20 msg/min per group_id,撞了會 429 + retry_after。
        正確處理:讀 retry_after + 10% jitter sleep,然後重試。最多 retry 3 次。
        """
        try:
            r = requests.post(f"{self._base}/{method}", json=payload, timeout=self.timeout)
            j = r.json()
            if j.get("ok"):
                return j.get("result") or {}, ""
            # 429 flood control
            if r.status_code == 429 or j.get("error_code") == 429:
                params = j.get("parameters") or {}
                retry_after = int(params.get("retry_after", 1))
                if _retry < 3:
                    import random as _r
                    sleep_sec = retry_after + max(1.0, retry_after * 0.1) + _r.uniform(0, 1.0)
                    self.on_log(f"[TG] 429 flood, retry_after={retry_after}s, sleep={sleep_sec:.1f}s ({method})")
                    time.sleep(sleep_sec)
                    return self._post(method, payload, _retry=_retry + 1)
            return {}, f"{method} fail: {j.get('description','')}"
        except Exception as e:
            return {}, f"{method} exc: {e}"

    def create_topic(
        self,
        name: str,
        *,
        icon_color: int = 7322096,
        icon_custom_emoji_id: str = "",
        chat_id: Optional[str] = None,  # v6.1:multi-tenant 支援
    ) -> Tuple[int, str]:
        """createForumTopic → 返回 message_thread_id。

        icon_color 是 Telegram 預設 7 種顏色之一(7322096=藍/9367192=紫/16766590=橙/...)。
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id or self.forum_chat_id,
            "name": name[:128],
            "icon_color": icon_color,
        }
        if icon_custom_emoji_id:
            payload["icon_custom_emoji_id"] = icon_custom_emoji_id
        result, err = self._post("createForumTopic", payload)
        if err:
            return 0, err
        topic_id = result.get("message_thread_id")
        if not topic_id:
            return 0, f"no message_thread_id in result: {result}"
        return int(topic_id), ""

    def edit_topic_name(self, topic_id: int, new_name: str, *, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        _, err = self._post("editForumTopic", {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
            "name": new_name[:128],
        })
        return (not err), err

    def close_topic(self, topic_id: int, *, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        _, err = self._post("closeForumTopic", {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
        })
        return (not err), err

    def reopen_topic(self, topic_id: int, *, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        _, err = self._post("reopenForumTopic", {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
        })
        return (not err), err

    def delete_topic(self, topic_id: int, *, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        """deleteForumTopic — 永久刪除整個 topic 含內部所有訊息,釋放 1000 配額。"""
        _, err = self._post("deleteForumTopic", {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
        })
        return (not err), err

    def delete_message(self, message_id: int, *, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        """deleteMessage — 刪單條訊息(撤回)。"""
        _, err = self._post("deleteMessage", {
            "chat_id": chat_id or self.forum_chat_id,
            "message_id": message_id,
        })
        return (not err), err

    def send_text(
        self,
        topic_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,  # v6.1:multi-tenant 支援
    ) -> Tuple[int, str]:
        """sendMessage with message_thread_id,返回 message_id。"""
        payload: Dict[str, Any] = {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
            "text": text[:4096],
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result, err = self._post("sendMessage", payload)
        if err:
            return 0, err
        return int(result.get("message_id") or 0), ""

    def send_photo(
        self,
        topic_id: int,
        photo_url: str,
        *,
        caption: str = "",
        chat_id: Optional[str] = None,
    ) -> Tuple[int, str]:
        """sendPhoto to topic。photo_url 可以是 HTTP URL 或 file_id。"""
        payload = {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
            "photo": photo_url,
        }
        if caption:
            payload["caption"] = caption[:1024]
        result, err = self._post("sendPhoto", payload)
        if err:
            return 0, err
        return int(result.get("message_id") or 0), ""

    def send_video(
        self,
        topic_id: int,
        video_url: str,
        *,
        caption: str = "",
        thumb_url: str = "",
        supports_streaming: bool = True,
        chat_id: Optional[str] = None,
    ) -> Tuple[int, str]:
        """sendVideo to topic。

        - supports_streaming=True 讓 TG inline 播放
        - thumb_url 提供視頻縮圖,TG 預覽顯示
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id or self.forum_chat_id,
            "message_thread_id": topic_id,
            "video": video_url,
            "supports_streaming": supports_streaming,
        }
        if caption:
            payload["caption"] = caption[:1024]
        if thumb_url:
            payload["thumb"] = thumb_url
        result, err = self._post("sendVideo", payload)
        if err:
            return 0, err
        return int(result.get("message_id") or 0), ""


# ── 業務層:Yahoo 對話 ↔ TG forum topic 雙向同步 ──


class YahooIMForumBridge:
    """整合 Yahoo IM 與 TG forum 的雙向橋接。

    使用方式:
        bridge = YahooIMForumBridge(
            forum_bot=TGForumBot(token, chat_id),
            store=TGForumStore(base_dir),
            base_dir=base_dir,
            on_log=on_log,
        )

        # Yahoo 收到買家訊息 → forward 到 topic(自動建 topic 如不存在)
        bridge.forward_yahoo_inbound(
            profile_id="xian678",
            yahoo_chat_id="Y9000000009",
            buyer_label="李先生",
            text="請問尺寸多少",
            media_url="",  # 圖片/視頻 URL
        )

        # TG topic 內用戶回覆 → 透過 telegram_bot polling 拿到 message,
        # 帶 message_thread_id, 呼叫 dispatch_topic_reply 找回 Yahoo 對話 send
        bridge.dispatch_topic_reply(
            topic_id=123,
            reply_text="好的,大概 20cm",
        )
    """

    def __init__(
        self,
        forum_bot: TGForumBot,
        store: TGForumStore,
        base_dir: Path,
        *,
        on_log: Optional[LogFn] = None,
    ):
        self.bot = forum_bot
        self.store = store
        self.base_dir = Path(base_dir)
        self.on_log = on_log or (lambda *_: None)
        # v6.1.27 修復:per-conv_key lock 防止並發 ensure_topic 造成同 buyer 建兩個 topic
        # bug 場景:monitor poll + new_conv 同時觸發 ensure_topic,兩個 thread check existing=None
        # 各自呼叫 createForumTopic → 同個買家 2 個 topic
        self._ensure_topic_locks: Dict[str, threading.Lock] = {}
        self._ensure_topic_meta_lock = threading.Lock()

    def _get_ensure_topic_lock(self, key: str) -> threading.Lock:
        """取得 per-key lock(同 conv_key 並發 ensure_topic 序列化)."""
        with self._ensure_topic_meta_lock:
            lk = self._ensure_topic_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._ensure_topic_locks[key] = lk
                # LRU 防止無限增長:超過 500 個 conv_key,清掉最舊一半
                if len(self._ensure_topic_locks) > 500:
                    keys = list(self._ensure_topic_locks.keys())
                    for old_k in keys[:250]:
                        # 安全 evict:不會 evict 正在持有的 lock(別人 acquire 後 dict 沒此 key 也安全)
                        self._ensure_topic_locks.pop(old_k, None)
            return lk

    def _conv_key(self, profile_id: str, yahoo_chat_id: str) -> str:
        return f"{profile_id}|{yahoo_chat_id}"

    def _resolve_chat_id_for_profile(self, profile_id: str) -> str:
        """v6.1:根據 Yahoo profile_id 找 owner.forum_chat_id。

        - 已綁定同事 → 該同事的 supergroup
        - 沒綁定 → 主管 group(self.bot.forum_chat_id)
        """
        try:
            from . import employees as _emp
            owner = _emp.find_owner_by_account(profile_id)
            if owner and owner.get("forum_chat_id"):
                return str(owner["forum_chat_id"])
        except Exception:
            pass
        return str(self.bot.forum_chat_id)

    def _resolve_chat_id_for_conv(self, conv_key: str) -> str:
        """從 store entry 拿 forum_chat_id(舊資料沒記就 fallback)。"""
        entry = self.store.get_entry(conv_key) or {}
        chat_id = entry.get("forum_chat_id", "")
        if chat_id:
            return str(chat_id)
        # 舊資料:用 profile_id 查 owner
        profile_id = entry.get("profile_id", "")
        return self._resolve_chat_id_for_profile(profile_id)

    def ensure_topic(
        self,
        profile_id: str,
        yahoo_chat_id: str,
        *,
        buyer_label: str = "",
        account_name: str = "",
        skip_backfill: bool = False,
    ) -> Tuple[int, str]:
        """確保該 Yahoo 對話有對應的 TG topic;沒有就建一個。

        建立時:
        1. createForumTopic
        2. 發資訊卡(含「直接回覆 = 自動 send 給買家」說明)→ pin
        3. 用 BOSH query_message 拉最近 20 條訊息,逐條 push(含媒體展示)

        v6.1.27 修復:整個 check-and-create 用 per-key lock 包住,
        避免並發呼叫造成同買家建兩個 topic.
        """
        key = self._conv_key(profile_id, yahoo_chat_id)
        # v6.1.27:per-key lock 序列化(不同 conv 仍可並發,同一 conv 等前者完成)
        with self._get_ensure_topic_lock(key):
            return self._ensure_topic_locked(
                key=key,
                profile_id=profile_id,
                yahoo_chat_id=yahoo_chat_id,
                buyer_label=buyer_label,
                account_name=account_name,
                skip_backfill=skip_backfill,
            )

    def _ensure_topic_locked(
        self,
        *,
        key: str,
        profile_id: str,
        yahoo_chat_id: str,
        buyer_label: str = "",
        account_name: str = "",
        skip_backfill: bool = False,
    ) -> Tuple[int, str]:
        """ensure_topic 的實際實作.必須在 per-key lock 內呼叫."""
        existing = self.store.get_topic_id(key)
        if existing:
            # ✅ 既有 topic reuse:檢查是否已 backfill 過,沒有就補一次
            # 之前建的空白 topic(skip_backfill=True 或舊版)會在這裡補上歷史
            if not skip_backfill:
                entry = self.store.get_entry(key) or {}
                if not entry.get("backfilled_ts"):
                    try:
                        from .accounts import load_settings as _ls_bf
                        _bf_n = int(_ls_bf().get("tg_forum_backfill_count", 200) or 200)
                    except Exception:
                        _bf_n = 200
                    try:
                        self.on_log(
                            f"[TG-FORUM] 既有 topic={existing} 沒 backfill 過,補拉歷史"
                        )
                        # v6.1.2:_backfill_history 返回 bool,只在成功時標記;
                        # 失敗時下次 ensure_topic 會再試(修「失敗 silent return 但仍標記」bug)
                        _ok = self._backfill_history(
                            topic_id=existing,
                            profile_id=profile_id,
                            yahoo_chat_id=yahoo_chat_id,
                            buyer_label=buyer_label or "對方",
                            max_total=_bf_n,
                        )
                        if _ok:
                            with self.store._lock:
                                if key in self.store._data:
                                    self.store._data[key]["backfilled_ts"] = time.time()
                                    self.store._save()
                        else:
                            self.on_log(
                                f"[TG-FORUM] backfill {yahoo_chat_id} 失敗,不標記 ts,下次再試"
                            )
                    except Exception as e:
                        self.on_log(f"[TG-FORUM] 補 backfill 異常(忽略): {e}")
            return existing, ""
        # TG forum topic name 限制:128 字元 + 不能空 + 不能含 \r\n
        raw_label = (buyer_label or yahoo_chat_id or "").strip()
        # 移除控制字元 / 換行
        import re as _re_safe
        raw_label = _re_safe.sub(r"[\r\n\t\x00-\x1f]", " ", raw_label)
        # 過長 truncate
        title = f"[{account_name or profile_id}] {raw_label}"[:120].strip() or f"[{profile_id}] _new_"
        # 警告:接近 1000 topic 上限(TG forum 硬限制)
        try:
            existing_count = len(self.store._data)
            if existing_count >= 900:
                self.on_log(
                    f"[TG-FORUM] ⚠️ 已有 {existing_count} 個 topics,接近 TG 1000 上限!"
                    "請考慮 archive 已結案的對話 topics(右鍵 → 關閉/刪除)"
                )
        except Exception:
            pass
        # v6.1:multi-tenant — 根據 profile_id 找 owner.forum_chat_id 決定建在哪個 group
        target_chat_id = self._resolve_chat_id_for_profile(profile_id)
        topic_id, err = self.bot.create_topic(title, chat_id=target_chat_id)
        if err:
            # TOPICS_TOO_MUCH 特殊處理
            if "TOPICS_TOO_MUCH" in err or "topic" in err.lower() and "too" in err.lower():
                self.on_log(f"[TG-FORUM] ❌ TG 1000 topic 上限已到,請先刪除/archive 已結案 topics")
            return 0, err
        self.store.set_topic_id(
            key, topic_id, title=title,
            profile_id=profile_id, chat_id=yahoo_chat_id,
            forum_chat_id=target_chat_id,  # v6.1:記下這個 topic 在哪個 group
        )
        self.on_log(f"[TG-FORUM] 新建 topic={topic_id} group={target_chat_id} for {key} title={title!r}")

        # ── 頂部資訊卡(後續 update_info_card 動態更新)──
        info_text = self._build_info_card_text(
            account_name=account_name or profile_id,
            buyer_label=buyer_label or "(未知)",
            yahoo_chat_id=yahoo_chat_id,
            current_profile_id=profile_id,
        )
        refresh_btn = {
            "inline_keyboard": [[
                {"text": "🔄 刷新狀態", "callback_data": f"refresh_card:{key}"},
            ]],
        }
        info_msg_id, _ = self.bot.send_text(
            topic_id, info_text, parse_mode="Markdown",
            reply_markup=refresh_btn,
            chat_id=target_chat_id,  # v6.1
        )
        if info_msg_id:
            # 寫入 store 讓後續 update_info_card 找回
            self.store.set_info_msg_id(key, info_msg_id)
            try:
                self.bot._post("pinChatMessage", {
                    "chat_id": target_chat_id,  # v6.1
                    "message_id": info_msg_id,
                    "disable_notification": True,
                })
            except Exception:
                pass

        # ── 拉歷史(skip_backfill=True 跳過,首次大規模同步用)──
        # 預設 packed 模式:拉 200 條(REST API 上限)然後合併成 1-3 個 TG msg
        # settings: tg_forum_backfill_count(預設 200)
        if not skip_backfill:
            try:
                from .accounts import load_settings as _ls
                _backfill_n = int(_ls().get("tg_forum_backfill_count", 200) or 200)
            except Exception:
                _backfill_n = 200
            try:
                # v6.1.2:返回 bool,只在成功才標記(否則下次再試,修同事「拉不到歷史」bug)
                _ok = self._backfill_history(
                    topic_id=topic_id,
                    profile_id=profile_id,
                    yahoo_chat_id=yahoo_chat_id,
                    buyer_label=buyer_label or "對方",
                    max_total=_backfill_n,
                )
                if _ok:
                    with self.store._lock:
                        if key in self.store._data:
                            self.store._data[key]["backfilled_ts"] = time.time()
                            self.store._save()
                else:
                    self.on_log(
                        f"[TG-FORUM] backfill {yahoo_chat_id} 失敗,不標記 ts,下次再試"
                    )
            except Exception as e:
                self.on_log(f"[TG-FORUM] backfill 異常(忽略): {e}")

        return topic_id, ""

    def ensure_order_center_topic(self, group_chat_id: str = "") -> Tuple[int, str]:
        """v6.1:確保「📋 訂單中心」topic 存在(每個 forum group 一個)。

        - 既有就 reuse(從 store 拿)
        - 不存在就 createForumTopic + 發一個說明訊息(pin 起來)
        - 不做 backfill(訂單中心是事件流,不需要歷史)

        Returns: (topic_id, err)
        """
        if not group_chat_id:
            group_chat_id = str(self.bot.forum_chat_id)
        existing = self.store.get_order_center_topic(group_chat_id)
        if existing:
            return existing, ""

        # 建新 topic
        topic_id, err = self.bot.create_topic(
            "📋 訂單中心", chat_id=group_chat_id,
            icon_color=0xFB6F5F,  # 紅色(跟一般買家 topic 視覺區隔)
        )
        if err or not topic_id:
            return 0, f"create 訂單中心 topic 失敗: {err}"

        # 寫進 store
        self.store.set_order_center_topic(group_chat_id, topic_id)
        self.on_log(f"[TG-FORUM] 新建訂單中心 topic={topic_id} group={group_chat_id}")

        # v6.1:說明訊息 + inline buttons 一起 pin(用戶 1 鍵過濾 / 看清單 / 看總結)
        info_text = self._build_order_center_pin_text()
        quick_panel_buttons = {
            "inline_keyboard": self._build_order_center_pin_buttons(),
        }
        info_msg_id, _ = self.bot.send_text(
            topic_id, info_text, parse_mode="HTML",
            reply_markup=quick_panel_buttons,
            chat_id=group_chat_id,
        )
        if info_msg_id:
            try:
                self.bot._post("pinChatMessage", {
                    "chat_id": group_chat_id,
                    "message_id": info_msg_id,
                    "disable_notification": True,
                })
            except Exception:
                pass
            # 記下 pin msg_id 給後續 refresh 用
            self.store.set_order_center_pin_msg(group_chat_id, info_msg_id)
        return topic_id, ""

    def _build_order_center_pin_text(self) -> str:
        """訂單中心置頂訊息內容(v6.1.20 加一鍵轉刊提示)。"""
        return (
            "📋 <b>訂單中心 · 快捷面板</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "<b>狀態 icon:</b>\n"
            "  🟡 未付款 · 🔴 待出貨 · 🔵 已出貨 · 🟢 完成 · ⚫ 取消 · 🚨 逾期\n"
            "\n"
            "<b>按下面 button 即時查詢 ↓</b>\n"
            "<i>或打 /orders 配參數查詢</i>\n"
            "\n"
            "<b>📦 一鍵轉刊:</b> 直接<b>貼 Yahoo 商品連結到本 topic</b>,"
            "Bot 自動拉資料 → 選目標帳號 → 編輯 → 刊登"
        )

    def _build_order_center_pin_buttons(self) -> list:
        """訂單中心置頂訊息 inline buttons(v6.1.20 加一鍵轉刊入口)。"""
        return [
            [
                {"text": "🔴 待出貨", "callback_data": "oc:filter:wp"},
                {"text": "🟡 待付款", "callback_data": "oc:filter:wu"},
            ],
            [
                {"text": "🔵 已出貨", "callback_data": "oc:filter:sh"},
                {"text": "🚨 逾期", "callback_data": "oc:filter:od"},
            ],
            [
                {"text": "📋 待處理清單(Yahoo 後台跳轉)", "callback_data": "oc:batch:todo_list"},
            ],
            [
                {"text": "📊 今日業績總結", "callback_data": "oc:summary:today"},
            ],
            [
                {"text": "📦 一鍵轉刊(貼連結用)", "callback_data": "oc:relist:hint"},
            ],
        ]

    def refresh_order_center_pin(self, group_chat_id: str = "") -> bool:
        """v6.1.20:強制刷新訂單中心置頂訊息(內容 + buttons)。

        重啟時可呼叫,確保使用者看到的是最新版面(例如本次更新加了「一鍵轉刊」按鈕)。

        三層 fallback:
          1. 有 stored pin_msg_id → editMessageText 就地更新
          2. 沒 pin_msg_id 或 edit 失敗 → 發新訊息 + pinChatMessage(老使用者場景)
          3. 仍失敗 → return False

        Returns: True 表示刷新成功。
        """
        if not group_chat_id:
            group_chat_id = str(self.bot.forum_chat_id)
        topic_id = self.store.get_order_center_topic(group_chat_id) or 0
        if not topic_id:
            return False

        new_text = self._build_order_center_pin_text()
        new_buttons = self._build_order_center_pin_buttons()

        # ── Layer 1:嘗試 edit 既有 pin ──
        pin_msg_id = self.store.get_order_center_pin_msg(group_chat_id) or 0
        if pin_msg_id:
            try:
                payload = {
                    "chat_id": group_chat_id,
                    "message_id": pin_msg_id,
                    "text": new_text[:4096],
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": new_buttons},
                }
                r, e = self.bot._post("editMessageText", payload)
                if not e:
                    return True
                _low = e.lower()
                if "not modified" in _low:
                    return True
                # 其他錯誤(message_id 失效 / 太老不可編輯)→ fallback
                self.on_log(f"[TG-FORUM] edit 既有 pin 失敗,fallback 發新訊息: {e[:80]}")
            except Exception as e:
                self.on_log(f"[TG-FORUM] edit pin 異常,fallback: {e}")

        # ── Layer 2:沒 pin_msg_id 或 edit 失敗 → 發新訊息 + pin ──
        # 老使用者場景(訂單中心 topic 在 v6.1.20 之前建立)走這條
        try:
            self._rate_gate(group_chat_id)
            new_msg_id, send_err = self.bot.send_text(
                topic_id, new_text, parse_mode="HTML",
                reply_markup={"inline_keyboard": new_buttons},
                chat_id=group_chat_id,
            )
            if not new_msg_id:
                self.on_log(f"[TG-FORUM] 發新 pin 訊息失敗: {send_err}")
                return False
            # pin 起來
            try:
                self.bot._post("pinChatMessage", {
                    "chat_id": group_chat_id,
                    "message_id": new_msg_id,
                    "disable_notification": True,
                })
            except Exception as e:
                self.on_log(f"[TG-FORUM] pinChatMessage 失敗(訊息已發,只是沒 pin): {e}")
            # 寫 store 給下次 refresh 用
            self.store.set_order_center_pin_msg(group_chat_id, new_msg_id)
            self.on_log(
                f"[TG-FORUM] 訂單中心 pin 已重發 msg_id={new_msg_id}"
                f"(舊 pin 仍在 topic 內,可手動刪)"
            )
            return True
        except Exception as e:
            self.on_log(f"[TG-FORUM] refresh_order_center_pin fallback 異常: {e}")
            return False

    # v6.1:per-chat rate limit token bucket(TG per-chat 限 1 msg/s)
    # 為避免 24 帳號同時 status_changed 撞 429 阻塞整 bot,訂單中心 push/edit 加 inter-call sleep
    _ORDER_PUSH_GATE_LOCK = threading.Lock()
    _ORDER_PUSH_LAST_TS: Dict[str, float] = {}  # group_chat_id → last push ts
    _ORDER_PUSH_GAP_SEC = 1.1                   # 每 chat 至少間隔 1.1s

    def _rate_gate(self, group_chat_id: str) -> None:
        """簡單 token bucket:確保同 chat push 間隔 >= 1.1s 避免 429。"""
        with self._ORDER_PUSH_GATE_LOCK:
            last = self._ORDER_PUSH_LAST_TS.get(group_chat_id, 0.0)
            now = time.time()
            wait = self._ORDER_PUSH_GAP_SEC - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._ORDER_PUSH_LAST_TS[group_chat_id] = time.time()

    def edit_order_card_in_center(
        self, *, order_id: str, html_text: str,
        buttons: Optional[list] = None,
    ) -> bool:
        """v6.1:狀態變更時 edit 訂單中心主卡(parse_mode=HTML,商品標題含特殊字元安全)。

        Returns: True 表 edit 成功;False 表 msg 已不存在(caller 應 fallback push 新訊息)
        """
        msg_info = self.store.get_order_msg(order_id)
        if not msg_info:
            return False
        msg_id = int(msg_info.get("msg_id") or 0)
        group_chat = msg_info.get("group_chat_id", "")
        if not msg_id or not group_chat:
            return False
        self._rate_gate(group_chat)
        try:
            payload = {
                "chat_id": group_chat,
                "message_id": msg_id,
                "text": html_text[:4096],
                "parse_mode": "HTML",
            }
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            r, e = self.bot._post("editMessageText", payload)
            if e:
                e_low = e.lower()
                # message_id 不存在 / topic 不存在 / 已被刪 → return False fallback
                if any(k in e_low for k in (
                    "message to edit not found", "message_id_invalid",
                    "message thread not found", "message can't be edited",
                )):
                    return False
                # message is not modified — 視為成功(內容沒變)
                if "not modified" in e_low:
                    return True
                self.on_log(f"[TG-FORUM] edit 訂單卡 {order_id} 失敗: {e}")
                return False
            return True
        except Exception as e:
            self.on_log(f"[TG-FORUM] edit_order_card 異常 {order_id}: {e}")
            return False

    def reply_in_order_center(
        self, *, order_id: str, html_text: str,
    ) -> int:
        """v6.1:reply 主卡發短摘要(關鍵狀態變更感知 push)。HTML mode。"""
        msg_info = self.store.get_order_msg(order_id)
        if not msg_info:
            return 0
        msg_id = int(msg_info.get("msg_id") or 0)
        group_chat = msg_info.get("group_chat_id", "")
        topic_id = int(msg_info.get("topic_id") or 0)
        if not topic_id or not group_chat:
            return 0
        self._rate_gate(group_chat)
        try:
            payload = {
                "chat_id": group_chat,
                "message_thread_id": topic_id,
                "text": html_text[:4096],
                "parse_mode": "HTML",
            }
            if msg_id:
                payload["reply_to_message_id"] = msg_id
            r, e = self.bot._post("sendMessage", payload)
            if r and not e:
                return int(r.get("message_id") or 0)
            return 0
        except Exception as e:
            self.on_log(f"[TG-FORUM] reply_in_order_center 異常 {order_id}: {e}")
            return 0

    def push_to_order_center(
        self, *, account_name: str, html_text: str,
        buttons: Optional[list] = None,
        group_chat_id: str = "",
    ) -> int:
        """v6.1:把訂單卡 push 到訂單中心 topic。HTML mode。

        - html_text:已 HTML escape 的訂單卡(含 [帳號名] 標識)
        - buttons:inline keyboard
        - group_chat_id:multi-tenant 目標
        Returns: msg_id(0 表失敗)

        v6.1 變更:**不再用 sendPhoto**(caption 1024 上限切爛進度條),純 sendMessage
        + auto-rebuild topic on 「message thread not found」
        """
        if not group_chat_id:
            group_chat_id = str(self.bot.forum_chat_id)
        topic_id, err = self.ensure_order_center_topic(group_chat_id)
        if not topic_id:
            self.on_log(f"[TG-FORUM] 訂單中心 ensure 失敗: {err}")
            return 0
        self._rate_gate(group_chat_id)
        # prefix 加 <b>[帳號名]</b>(HTML mode,商品標題含 *_ 安全)
        import html as _html
        full_text = f"<b>[{_html.escape(account_name)}]</b>\n{html_text}"
        reply_markup = {"inline_keyboard": buttons} if buttons else None
        try:
            payload = {
                "chat_id": group_chat_id,
                "message_thread_id": topic_id,
                "text": full_text[:4096],
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            r, e = self.bot._post("sendMessage", payload)
            if r and not e:
                return int(r.get("message_id") or 0)
            # auto-rebuild on 「message thread not found」(topic 被誤刪)
            if e and "message thread not found" in e.lower():
                self.on_log(f"[TG-FORUM] 訂單中心 topic 不存在,自動重建 + 清舊 order_msg...")
                # 清掉 store 的 entry,下次 ensure 會建新的
                self.store.remove_entry(f"__order_center__|{group_chat_id}")
                # v6.1:同時清掉所有指向舊 topic 的 __order_msg__|* entries
                # 否則 edit_order_card_in_center / reply_in_order_center 會持續用舊 topic_id 失敗
                try:
                    for old_oid, old_entry in self.store.list_active_order_msgs():
                        if str(old_entry.get("group_chat_id", "")) == str(group_chat_id):
                            self.store.remove_entry(f"__order_msg__|{old_oid}")
                    self.on_log(f"[TG-FORUM] 訂單中心重建:已清舊 order_msg entries for {group_chat_id}")
                except Exception as _e_clean:
                    self.on_log(f"[TG-FORUM] 清舊 order_msg 異常(忽略): {_e_clean}")
                topic_id, _ = self.ensure_order_center_topic(group_chat_id)
                if topic_id:
                    payload["message_thread_id"] = topic_id
                    r, e2 = self.bot._post("sendMessage", payload)
                    if r and not e2:
                        return int(r.get("message_id") or 0)
            return 0
        except Exception as e:
            self.on_log(f"[TG-FORUM] push_to_order_center 異常: {e}")
            return 0

    def _backfill_history(
        self,
        *,
        topic_id: int,
        profile_id: str,
        yahoo_chat_id: str,
        buyer_label: str,
        page_size: int = 100,
        max_total: int = 10,         # ⚠️ 業界 20 msg/min per group 限制 → 預設只拉 10 條摘要
        push_delay_sec: float = 3.5, # 3.5s = 17/min < 20/min,留 safety margin
    ) -> bool:
        """從 BOSH query_message 分頁拉「完整」歷史進 topic(舊→新)。

        - page_size: 每頁拉 N 條(BOSH 上限通常 100)
        - max_total: 最多拉幾條,避免極端對話打爆 TG
        - push_delay_sec: 每條 TG push 之間 sleep,避免 TG flood(30 msg/sec limit)
        - 順序:舊→新(query_message 返回新→舊,本實作反轉後遍歷)
        """
        from .yahoo_im_bosh_ext import BOSHSession
        from .im_http_ops import build_channel_id

        profile_dir = self.base_dir / "profiles" / profile_id
        if not profile_dir.exists():
            self.on_log(f"[TG-FORUM] backfill {yahoo_chat_id}: profile {profile_id} 不存在")
            return False

        my_id, _ = self._resolve_my_id(profile_dir)
        if not my_id:
            self.on_log(f"[TG-FORUM] backfill {yahoo_chat_id}: 拿不到 my_id(JWT/cookies 失效?)")
            return False
        buyer_y = yahoo_chat_id if yahoo_chat_id.upper().startswith("Y") else f"Y{yahoo_chat_id}"
        channel = build_channel_id(my_id, buyer_y)

        conv_key = self._conv_key(profile_id, yahoo_chat_id)

        # ── 改走 REST /fe/api/im/messages(比 BOSH retention 久 + listing 卡支援)──
        from .im_http_ops import _build_session
        from urllib.parse import quote as _qt

        session, _, err = _build_session(profile_dir, buyer_cid=buyer_y)
        if not session:
            self.on_log(f"[TG-FORUM] backfill session 失敗: {err}")
            return False

        # REST API server cap = 200 一次
        api_limit = min(max_total, 200)
        url = (
            f"https://tw.bid.yahoo.com/fe/api/im/messages"
            f"?property=auction2&channelId={_qt(channel)}"
            f"&sortBy=-createdTs&limit={api_limit}"
        )
        try:
            r = session.get(url, timeout=20)
        except Exception as e:
            self.on_log(f"[TG-FORUM] backfill REST 異常: {e}")
            return False

        # 試反方向條件:status!=200 或 status=200 但 messages 為空
        # (Yahoo 對某些 channel 方向敏感,shop:buyer 返 0 / buyer:shop 才有)
        need_reverse = (r.status_code != 200) or (
            r.status_code == 200 and not (r.json() or {}).get("messages")
        )
        if need_reverse:
            rev = channel.split(":")
            if len(rev) == 3:
                channel_rev = f"{rev[0]}:{rev[2]}:{rev[1]}"
                url_rev = url.replace(_qt(channel), _qt(channel_rev))
                try:
                    r2 = session.get(url_rev, timeout=20)
                    if r2.status_code == 200 and (r2.json() or {}).get("messages"):
                        self.on_log(f"[TG-FORUM] backfill 反方向命中: {channel_rev[-50:]}")
                        r = r2
                        channel = channel_rev
                    elif r.status_code != 200:
                        self.on_log(f"[TG-FORUM] backfill REST 兩方向都失敗 {r.status_code}")
                        return False
                except Exception:
                    if r.status_code != 200:
                        return False

        if r.status_code != 200:
            self.on_log(f"[TG-FORUM] backfill REST status={r.status_code} (cookie/wssid 過期?)")
            return False

        data = r.json()
        all_msgs = data.get("messages", []) or []
        # REST sortBy=-createdTs 給 desc,本實作要 asc(舊→新)
        all_msgs.sort(key=lambda m: m.get("createdUts", 0) or 0)

        self.on_log(
            f"[TG-FORUM] backfill {yahoo_chat_id}: 共 {len(all_msgs)} 條歷史"
        )

        my_id_l = my_id.lower()

        # ── 打包模式:純文字合併成 < 4096 字塊,媒體單獨 push ──
        # 大幅降低 TG API call 數,1 個對話通常 1-2 個 TG msg 解決
        # settings: tg_forum_backfill_mode = "packed"(預設)/ "per_message"
        try:
            from .accounts import load_settings as _ls_mode
            _bf_mode = (_ls_mode().get("tg_forum_backfill_mode", "packed") or "packed").lower()
        except Exception:
            _bf_mode = "packed"

        if _bf_mode == "packed":
            # v6.1:multi-tenant 找該 conv 的 group chat_id 帶進去
            target_chat = self._resolve_chat_id_for_conv(conv_key)
            self._backfill_pack_and_push(
                topic_id=topic_id,
                conv_key=conv_key,
                buyer_label=buyer_label,
                all_msgs=all_msgs,
                my_id_l=my_id_l,
                channel=channel,
                push_delay_sec=push_delay_sec,
                chat_id=target_chat,
            )
            return True

        # ── 舊邏輯:每條獨立 push(留著供 per_message 模式)──
        for m in all_msgs:
            sender = (m.get("sender") or "").lower()
            yahoo_msg_id = m.get("messageId", "")
            mtype = m.get("type", "")
            value = m.get("value") or {}
            role = "🟢 我" if sender == my_id_l else f"👤 {buyer_label}"
            ts = m.get("createdUts", 0)
            ts_str = time.strftime("%m-%d %H:%M", time.localtime(ts / 1000)) if ts else ""

            pushed_msg_id = 0
            if mtype == "text":
                body = value.get("content", "") or ""
                if body.strip():
                    pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}:\n{body[:3000]}")
                    enrich = self._enrich_yahoo_items(body)
                    if enrich:
                        self.bot.send_text(topic_id, enrich, parse_mode="Markdown")
                        if push_delay_sec > 0:
                            time.sleep(push_delay_sec)
            elif mtype == "image":
                u = (value.get("src") or {}).get("url") or (value.get("origin") or {}).get("url") or ""
                if u:
                    pushed_msg_id, _ = self.bot.send_photo(topic_id, u, caption=f"{ts_str} {role}")
                else:
                    pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}: 📷 [圖片無 URL]")
            elif mtype == "video":
                u = ""
                resized = value.get("resizeVideos") or []
                if resized and isinstance(resized, list):
                    for rv in resized:
                        uu = (rv or {}).get("url", "")
                        if uu.lower().endswith(".mp4"):
                            u = uu
                            break
                    if not u:
                        u = (resized[0] or {}).get("url", "")
                if not u:
                    u = (value.get("src") or {}).get("url") or value.get("url", "") or ""
                thumb_url = (value.get("thumbnail") or {}).get("url", "")
                if u:
                    pushed_msg_id, _ = self.bot.send_video(
                        topic_id, u, caption=f"{ts_str} {role}", thumb_url=thumb_url,
                    )
                else:
                    pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}: 🎬 [視頻無 URL]")
            elif mtype == "sticker":
                u = value.get("url", "")
                if u:
                    pushed_msg_id, _ = self.bot.send_photo(topic_id, u, caption=f"{ts_str} {role} 😀")
                else:
                    pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}: 😀 [貼圖 {value.get('id','')}]")
            elif mtype == "listing":
                # 商品連結卡 — buyer/seller 發的 yahoo item link
                yid = value.get("id", "")
                title = value.get("title", "")[:120]
                price = value.get("price", "")
                # 第一張縮圖
                imgs = value.get("images") or []
                first_img = ""
                if imgs:
                    first_img = ((imgs[0] or {}).get("thumbnail") or {}).get("url", "") or \
                                ((imgs[0] or {}).get("origin") or {}).get("url", "")
                yh_url = f"https://tw.bid.yahoo.com/item/{yid}" if yid else ""
                caption = (
                    f"{ts_str} {role}: 🏷 *[商品]*\n"
                    f"{title}\n"
                    f"💰 NT${price}\n"
                    f"[→ Yahoo 商品頁]({yh_url})"
                )
                if first_img:
                    pushed_msg_id, _ = self.bot.send_photo(topic_id, first_img, caption=caption)
                else:
                    pushed_msg_id, _ = self.bot.send_text(topic_id, caption, parse_mode="Markdown")
                # listing 直接帶 yid,查 D1
                if yid:
                    enrich = self._enrich_yahoo_items(yid)
                    if enrich:
                        self.bot.send_text(topic_id, enrich, parse_mode="Markdown")
                        if push_delay_sec > 0:
                            time.sleep(push_delay_sec)
            elif mtype == "order":
                oid = value.get("id", "")
                pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}: 📋 [訂單卡] #{oid}")
            elif mtype:
                pushed_msg_id, _ = self.bot.send_text(topic_id, f"{ts_str} {role}: [{mtype}] {str(value)[:200]}")

            # mapping 給 4b reply 引用
            if pushed_msg_id and yahoo_msg_id:
                msg_type_int = {"text": 1, "emoji": 2, "image": 3, "video": 4, "audio": 5}.get(mtype, 1)
                self.store.add_msg_map(
                    conv_key, pushed_msg_id,
                    {
                        "yahoo_msg_id": yahoo_msg_id,
                        "msg_type": msg_type_int,
                        "content_snippet": json.dumps({"type": mtype, "value": value}, ensure_ascii=False)[:300],
                        "sender_id": sender,
                        "sender_nickname": "",
                        "channel_id": channel,
                        "ts": ts,
                    },
                )

            if push_delay_sec > 0:
                time.sleep(push_delay_sec)
        return True

    def _backfill_pack_and_push(
        self,
        *,
        topic_id: int,
        conv_key: str,
        buyer_label: str,
        all_msgs: List[Dict[str, Any]],
        my_id_l: str,
        channel: str,
        push_delay_sec: float = 3.5,
        chat_id: Optional[str] = None,  # v6.1:multi-tenant
    ) -> None:
        """打包模式:聊天 app 風排版 + 純文字合併成 < 4096 字塊。

        排版策略(參考 GramDesk / Front / Slack export):
        - 跨日插日期分隔線 `— 4 月 12 日 週日 —`
        - 同 sender 連續訊息合併,只第一條顯示「角色  HH:MM」header
        - 不同 sender 之間 1 行空白
        - 🛒 買家 / 🏪 我 — 語意化角色 emoji,視覺對比強
        - 媒體(圖片/視頻)獨立 push,在文字塊間穿插
        """
        BUFFER_MAX = 3800
        buf: List[str] = []
        buf_size = 0
        last_sender_in_buf: Optional[str] = None
        last_date_in_buf: Optional[str] = None

        def _flush_buf() -> None:
            nonlocal buf, buf_size, last_sender_in_buf, last_date_in_buf
            if not buf:
                return
            text = "\n".join(buf)
            try:
                self.bot.send_text(topic_id, text, chat_id=chat_id)
            except Exception as e:
                self.on_log(f"[TG-FORUM] backfill packed flush 失敗: {e}")
            buf = []
            buf_size = 0
            last_sender_in_buf = None
            last_date_in_buf = None
            if push_delay_sec > 0:
                time.sleep(push_delay_sec)

        def _add_lines(lines: List[str]) -> None:
            """加多行到 buf,超 buffer 自動 flush。"""
            nonlocal buf_size
            joined_len = sum(len(l) + 1 for l in lines)
            if buf_size + joined_len > BUFFER_MAX:
                _flush_buf()
            buf.extend(lines)
            buf_size += joined_len

        # ── header ──
        _WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
        header_lines = [
            "📜 對話歷史",
            f"共 {len(all_msgs)} 條訊息",
            "─────────────",
            "",
        ]
        _add_lines(header_lines)

        def _format_date_sep(ts_sec: float) -> str:
            lt = time.localtime(ts_sec)
            wd = _WEEKDAYS[lt.tm_wday]
            return f"— {lt.tm_mon} 月 {lt.tm_mday} 日 {wd} —"

        def _role_for(sender: str) -> str:
            return "🏪 我" if sender == my_id_l else f"🛒 {buyer_label}"

        def _ts_hhmm(ts_sec: float) -> str:
            return time.strftime("%H:%M", time.localtime(ts_sec))

        for m in all_msgs:
            sender = (m.get("sender") or "").lower()
            yahoo_msg_id = m.get("messageId", "")
            mtype = m.get("type", "")
            value = m.get("value") or {}
            ts = m.get("createdUts", 0)
            ts_sec = ts / 1000 if ts else 0
            cur_date = time.strftime("%Y-%m-%d", time.localtime(ts_sec)) if ts else ""

            # ── 媒體類:獨立 push(會 reset header/date state)──
            if mtype in ("image", "video"):
                # 先 flush 累積的純文字
                _flush_buf()
                # 媒體 caption 用簡潔格式
                caption_role = _role_for(sender)
                date_part = ""
                if cur_date:
                    lt = time.localtime(ts_sec)
                    date_part = f"{lt.tm_mon}/{lt.tm_mday} "
                cap = f"{caption_role}  {date_part}{_ts_hhmm(ts_sec)}"

                if mtype == "image":
                    u = (value.get("src") or {}).get("url") or (value.get("origin") or {}).get("url") or ""
                    if u:
                        pushed, _ = self.bot.send_photo(topic_id, u, caption=cap, chat_id=chat_id)
                        if pushed and yahoo_msg_id:
                            self.store.add_msg_map(conv_key, pushed, {
                                "yahoo_msg_id": yahoo_msg_id, "msg_type": 3,
                                "sender_id": sender, "channel_id": channel, "ts": ts,
                            })
                else:  # video
                    u = ""
                    resized = value.get("resizeVideos") or []
                    if resized and isinstance(resized, list):
                        for rv in resized:
                            uu = (rv or {}).get("url", "")
                            if uu.lower().endswith(".mp4"):
                                u = uu; break
                        if not u:
                            u = (resized[0] or {}).get("url", "")
                    if not u:
                        u = (value.get("src") or {}).get("url") or value.get("url", "") or ""
                    thumb_url = (value.get("thumbnail") or {}).get("url", "")
                    if u:
                        pushed, _ = self.bot.send_video(
                            topic_id, u, caption=cap, thumb_url=thumb_url,
                            chat_id=chat_id,
                        )
                        if pushed and yahoo_msg_id:
                            self.store.add_msg_map(conv_key, pushed, {
                                "yahoo_msg_id": yahoo_msg_id, "msg_type": 4,
                                "sender_id": sender, "channel_id": channel, "ts": ts,
                            })
                if push_delay_sec > 0:
                    time.sleep(push_delay_sec)
                continue

            # ── 純文字 / sticker / listing / order:走 packed buffer ──
            # 1. 日期變化 → 插日期分隔線(前後空行)
            new_lines: List[str] = []
            if cur_date and cur_date != last_date_in_buf:
                if last_date_in_buf is not None:
                    new_lines.append("")
                new_lines.append(_format_date_sep(ts_sec))
                new_lines.append("")
                last_date_in_buf = cur_date
                last_sender_in_buf = None  # 跨日後第一條一定要 header

            # 2. sender 變化 → 插空行 + header
            if sender != last_sender_in_buf:
                if last_sender_in_buf is not None:
                    new_lines.append("")  # sender 切換前空行
                new_lines.append(f"{_role_for(sender)}  {_ts_hhmm(ts_sec)}")
                last_sender_in_buf = sender

            # 3. 各 type 的內容行
            if mtype == "text":
                body = (value.get("content", "") or "").strip()
                if not body:
                    continue
                new_lines.append(body[:1500])

            elif mtype == "sticker":
                new_lines.append("😀 [貼圖]")

            elif mtype == "listing":
                yid = value.get("id", "")
                title = (value.get("title", "") or "")[:80]
                price = value.get("price", "")
                new_lines.append("🏷 [商品]")
                if title:
                    new_lines.append(title)
                if price:
                    new_lines.append(f"💰 NT${price}")
                if yid:
                    new_lines.append(f"https://tw.bid.yahoo.com/item/{yid}")

            elif mtype == "order":
                oid = value.get("id", "")
                new_lines.append(f"📋 [訂單卡] #{oid}")

            elif mtype:
                new_lines.append(f"[{mtype}]")

            if new_lines:
                # 若加進 buf 會爆 → 先 flush(會 reset state,新 flush 後第一條要重 header)
                joined_len = sum(len(l) + 1 for l in new_lines)
                if buf_size + joined_len > BUFFER_MAX:
                    _flush_buf()
                    # flush 後重新生成 date + sender header
                    redo: List[str] = []
                    if cur_date:
                        redo.append(_format_date_sep(ts_sec))
                        redo.append("")
                        last_date_in_buf = cur_date
                    redo.append(f"{_role_for(sender)}  {_ts_hhmm(ts_sec)}")
                    last_sender_in_buf = sender
                    # 內容(從 new_lines 取最後 N 條,跳過原本的 date/header)
                    # 簡單做法:重新跑 type 邏輯生成內容部分
                    if mtype == "text":
                        redo.append((value.get("content", "") or "")[:1500])
                    elif mtype == "sticker":
                        redo.append("😀 [貼圖]")
                    elif mtype == "listing":
                        yid = value.get("id", "")
                        title = (value.get("title", "") or "")[:80]
                        price = value.get("price", "")
                        redo.append("🏷 [商品]")
                        if title: redo.append(title)
                        if price: redo.append(f"💰 NT${price}")
                        if yid: redo.append(f"https://tw.bid.yahoo.com/item/{yid}")
                    elif mtype == "order":
                        redo.append(f"📋 [訂單卡] #{value.get('id','')}")
                    else:
                        redo.append(f"[{mtype}]")
                    buf.extend(redo)
                    buf_size += sum(len(l) + 1 for l in redo)
                else:
                    buf.extend(new_lines)
                    buf_size += joined_len

        # 最後 flush
        _flush_buf()

    def forward_yahoo_inbound(
        self,
        *,
        profile_id: str,
        yahoo_chat_id: str,
        buyer_label: str,
        account_name: str = "",
        text: str = "",
        media_url: str = "",
        media_kind: str = "",  # "image" | "video"
    ) -> Tuple[bool, str]:
        """Yahoo 收到買家訊息 → forward 到對應 TG topic。"""
        topic_id, err = self.ensure_topic(
            profile_id, yahoo_chat_id,
            buyer_label=buyer_label, account_name=account_name,
        )
        if not topic_id:
            return False, err

        # v6.1:multi-tenant — 找該 topic 在哪個 group
        target = self._resolve_chat_id_for_conv(self._conv_key(profile_id, yahoo_chat_id))

        try:
            if media_kind == "image" and media_url:
                msg_id, e = self.bot.send_photo(
                    topic_id, media_url,
                    caption=text or f"📷 {buyer_label} 傳圖片",
                    chat_id=target,
                )
            elif media_kind == "video" and media_url:
                msg_id, e = self.bot.send_video(
                    topic_id, media_url,
                    caption=text or f"🎬 {buyer_label} 傳視頻",
                    chat_id=target,
                )
            else:
                msg_id, e = self.bot.send_text(topic_id, f"👤 {buyer_label}:\n{text}", chat_id=target)
            if e:
                return False, e
            # 含 Yahoo 商品 ID → 查 D1 push 貨源卡(monitor 新訊息也適用)
            enrich = self._enrich_yahoo_items(text)
            if enrich:
                self.bot.send_text(topic_id, enrich, parse_mode="Markdown", chat_id=target)
            # ✅ touch_activity:cleanup_old_topics 用 last_activity_ts 判定
            try:
                self.store.touch_activity(self._conv_key(profile_id, yahoo_chat_id))
            except Exception:
                pass
            return True, f"topic_id={topic_id} msg_id={msg_id}"
        except Exception as ex:
            return False, f"forward 異常: {ex}"

    def dispatch_topic_reply(
        self,
        *,
        topic_id: int,
        reply_text: str = "",
        photo_file_id: str = "",
        video_file_id: str = "",
        parent_yahoo_info: Optional[Dict[str, Any]] = None,
        source_topic_msg_id: int = 0,
    ) -> Tuple[bool, str]:
        """TG topic 內用戶回覆 → 找回 Yahoo 對話 → send 回去。

        - parent_yahoo_info 不為空 → 走 BOSH send_reply_message 帶 parentMsgID
          (buyer 端看到 Yahoo IM 原生 reply UI)
        - 否則:
          - 文字: REST im_send_message
          - 圖片: yahoo_im_media.send_image_from_url
          - 視頻: yahoo_im_media.send_video_from_url
        """
        key, entry = self.store.find_by_topic_id(topic_id)
        if not key or not entry:
            return False, f"topic_id={topic_id} 沒對應 Yahoo 對話"
        profile_id = entry.get("profile_id", "")
        chat_id = entry.get("chat_id", "")
        if not (profile_id and chat_id):
            return False, f"entry 缺欄位: {entry}"
        profile_dir = self.base_dir / "profiles" / profile_id
        if not profile_dir.exists():
            return False, f"profile_dir 不存在: {profile_dir}"

        # ✅ 使用者在 topic 內回覆 = 活躍訊號,touch_activity 重置 cleanup 計時
        try:
            self.store.touch_activity(key)
        except Exception:
            pass

        try:
            my_id, err = self._resolve_my_id(profile_dir)
            if not my_id:
                return False, f"拿不到 my_id: {err}"
            buyer_y = chat_id if chat_id.upper().startswith("Y") else f"Y{chat_id}"

            # ─── 4b:有 parent_yahoo_info → BOSH send_reply_message ───
            # Yahoo 真實格式:msgContent JSON 內帶 replyMsgId,不用 parentMsgID/extInfo
            if parent_yahoo_info and reply_text and not photo_file_id and not video_file_id:
                from .yahoo_im_bosh_ext import BOSHSession
                from .im_http_ops import build_channel_id
                channel = parent_yahoo_info.get("channel_id") or build_channel_id(my_id, buyer_y)
                with BOSHSession(profile_dir, on_log=self.on_log) as s:
                    _, err2 = s.send_reply_message(
                        channel,
                        msg_type=1,  # TEXT
                        reply_text=reply_text,
                        parent_msg_id=parent_yahoo_info.get("yahoo_msg_id", ""),
                    )
                    if err2:
                        return False, f"BOSH reply 失敗: {err2}"
                    # mark_read 主動 fire(讓對方看到我已讀 + reply 同時)
                    try:
                        s.mark_read(channel)
                    except Exception:
                        pass
                return True, f"已 reply 連結 yahoo_msg_id={parent_yahoo_info.get('yahoo_msg_id','')[:12]}"

            # 圖片
            if photo_file_id:
                file_url = self._tg_file_url(photo_file_id)
                if not file_url:
                    return False, "拿不到 photo file URL"
                from .yahoo_im_media import send_image_from_url
                ok, info = send_image_from_url(
                    profile_dir,
                    shop_id=my_id, buyer_id=buyer_y,
                    image_url=file_url, on_log=self.on_log,
                )
                # v6.1.53:寫 mapping 讓圖片可被撤回(同文字邏輯)
                if ok and source_topic_msg_id:
                    import re as _re_img
                    from .im_http_ops import build_channel_id as _build_ch
                    _m_mid = _re_img.search(r'msgId=([A-Za-z0-9-]+)', info or '')
                    if _m_mid:
                        my_id_l = my_id.lower()
                        channel = _build_ch(my_id, buyer_y)
                        self.store.add_msg_map(
                            key, source_topic_msg_id,
                            {
                                "yahoo_msg_id": _m_mid.group(1),
                                "yahoo_msg_ids": [_m_mid.group(1)],
                                "msg_type": 2,  # IMAGE
                                "content_snippet": "[圖片]",
                                "sender_id": my_id_l,
                                "sender_nickname": "",
                                "channel_id": channel,
                                "ts": int(time.time() * 1000),
                            },
                        )
                return ok, info

            # 視頻
            if video_file_id:
                file_url = self._tg_file_url(video_file_id)
                if not file_url:
                    return False, "拿不到 video file URL"
                # v6.1.53:用 autosplit 版,> 30 秒自動 ffmpeg 切分多段順序送
                # Yahoo IM 視頻上限 30 秒(我們用 29s 留餘量),超過會被 server 拒絕或對方播不到
                from .yahoo_im_media import send_video_from_url_autosplit
                ok, info = send_video_from_url_autosplit(
                    profile_dir,
                    shop_id=my_id, buyer_id=buyer_y,
                    video_url=file_url, on_log=self.on_log,
                )
                # v6.1.53:寫 mapping 讓視頻可被撤回(多段視頻撤回會撤所有 segment)
                if ok and source_topic_msg_id:
                    import re as _re_vid
                    from .im_http_ops import build_channel_id as _build_ch_v
                    # 優先抓 msgIds(autosplit 多段),沒有就抓 msgId(單段)
                    _msg_ids = []
                    _m_plural = _re_vid.search(r'msgIds=([A-Za-z0-9,\-]+)', info or '')
                    if _m_plural:
                        _msg_ids = [m for m in _m_plural.group(1).split(',') if m]
                    else:
                        _m_single = _re_vid.search(r'msgId=([A-Za-z0-9-]+)', info or '')
                        if _m_single:
                            _msg_ids = [_m_single.group(1)]
                    if _msg_ids:
                        my_id_l = my_id.lower()
                        channel = _build_ch_v(my_id, buyer_y)
                        self.store.add_msg_map(
                            key, source_topic_msg_id,
                            {
                                "yahoo_msg_id": _msg_ids[0],  # 第一條供 legacy 用
                                "yahoo_msg_ids": _msg_ids,    # v6.1.53 新加:全部段
                                "msg_type": 4,  # VIDEO
                                "content_snippet": f"[視頻 {len(_msg_ids)} 段]" if len(_msg_ids) > 1 else "[視頻]",
                                "sender_id": my_id_l,
                                "sender_nickname": "",
                                "channel_id": channel,
                                "ts": int(time.time() * 1000),
                            },
                        )
                return ok, info

            # 文字(無 reply 引用)
            if reply_text:
                from .im_http_ops import im_send_message, build_channel_id, im_mark_read
                channel = build_channel_id(my_id, buyer_y)
                # v6.1.36 設計:
                #   REST 優先(已有 channel 99% case + 拿得到 msg_id 支援 /recall 撤回)
                #   REST 404(全新買家)→ bootstrap_channel(HTTP-first + PW fallback)→ BOSH send
                #   不再有「BOSH create_channel + 假成功」路徑(已移除,實機證明會建 shadow 對方收不到)
                ok, info = im_send_message(
                    profile_dir, channel_id=channel,
                    receiver=buyer_y, message=reply_text,
                    on_log=self.on_log,
                )

                # REST 失敗 → 先試 BOSH(自帶 chID 反向 fallback,可解大部分 case)
                # BOSH 也失敗才走 Chrome subprocess bootstrap(真新買家、SDK 還沒建過 channel)
                if not ok and ("404" in str(info) or "Channel not found" in str(info)):
                    _order_id = ""
                    try:
                        _order_id = (entry or {}).get("last_order_id", "") or ""
                    except Exception:
                        pass

                    # ⭐ STEP 1: BOSH send_text_message(自帶 chID 反向 fallback)
                    # 實機驗證:Yahoo 對某些 channel 存 chID 順序跟 build_channel_id 相反,
                    # BOSH 內部 STEP0 channel_user_active 1106 時自動反向 → rc=0
                    try:
                        from .yahoo_im_bosh_ext import BOSHSession
                        self.on_log(f"[TG-FORUM] REST 404 → BOSH send 兜底(自帶反向 chID fallback)")
                        with BOSHSession(profile_dir, on_log=self.on_log) as s:
                            _, _err_bosh = s.send_text_message(
                                channel, text=reply_text,
                                attach_order_id=_order_id, role=1,
                            )
                            if not _err_bosh:
                                ok = True
                                info = f"BOSH send OK{'(含 order)' if _order_id else ''}"
                                self.on_log("[TG-FORUM] BOSH send OK")
                                try:
                                    s.mark_read(channel)
                                except Exception:
                                    pass
                            else:
                                self.on_log(f"[TG-FORUM] BOSH 失敗: {_err_bosh}")
                                info = f"BOSH 失敗: {_err_bosh}"
                    except Exception as _e_bosh:
                        self.on_log(f"[TG-FORUM] BOSH 異常: {_e_bosh}")
                        info = f"BOSH 異常: {_e_bosh}"

                    # ⭐ STEP 2: BOSH 還失敗 → 用軟件 profile 開 Chrome subprocess
                    # (channel 真不存在於 server, 須先用 Yahoo SDK 創建)
                    # ⭐ 直接把用戶實際輸入的文字傳進 bootstrap,UI send 一次就完成
                    # (不要先發 "1" 再 BOSH 補一次 — 那買家會看到兩條)
                    if not ok and _order_id and buyer_y:
                        try:
                            from .yahoo_im_channel_bootstrap import open_profile_chrome_first_contact
                            self.on_log(
                                f"[TG-FORUM] BOSH 也 fail → Chrome subprocess bootstrap "
                                f"buyer={buyer_y} order={_order_id[-6:]} (send 用戶實際文字)"
                            )
                            chrome_ok, chrome_info = open_profile_chrome_first_contact(
                                profile_dir, buyer_y, _order_id,
                                on_log=self.on_log,
                                auto_send_text=reply_text,  # ⭐ 用戶輸入的文字直接 send
                            )
                            self.on_log(
                                f"[TG-FORUM] Chrome bootstrap: ok={chrome_ok} info={chrome_info[:120]}"
                            )
                            if chrome_ok:
                                # bootstrap 已把用戶文字 send 到買家(同時 order attach 自動因為 chat URL 有 orderId)
                                # 不再 BOSH 補送,避免重複訊息
                                ok = True
                                info = "Chrome bootstrap 自動 send 完成(用戶文字已透過 Yahoo UI 送達)"
                                self.on_log("[TG-FORUM] bootstrap OK,用戶文字已送達買家")
                        except Exception as _e_ch:
                            self.on_log(f"[TG-FORUM] Chrome bootstrap 異常: {_e_ch}")

                    # 全部 fail → 告訴用戶手動操作
                    if not ok:
                        info = (
                            f"❌ 軟件無法送達買家 {buyer_y}。"
                            f"請打開 Yahoo 訂單管理 → 訂單 {_order_id} → 點 即時通 button → "
                            f"用 Yahoo UI 親手發一句後再試。"
                        )
                # 寫 outgoing msg mapping(讓「reply 自己訊息 + /recall」可工作)
                if ok and source_topic_msg_id:
                    import re as _re
                    m = _re.search(r"msgId=([a-f0-9-]+)", info or "")
                    if m:
                        my_id_l = my_id.lower()
                        self.store.add_msg_map(
                            key, source_topic_msg_id,
                            {
                                "yahoo_msg_id": m.group(1),
                                "msg_type": 1,
                                "content_snippet": reply_text[:300],
                                "sender_id": my_id_l,
                                "sender_nickname": "",
                                "channel_id": channel,
                                "ts": int(time.time() * 1000),
                            },
                        )
                return ok, info

            return False, "reply 內容為空"
        except Exception as e:
            return False, f"dispatch 異常: {e}"

    def lookup_parent_yahoo_info(
        self,
        topic_id: int,
        topic_msg_id: int,
    ) -> Dict[str, Any]:
        """從 topic_id + topic_msg_id 反查對應 Yahoo msg(reply 引用用)。"""
        key, _ = self.store.find_by_topic_id(topic_id)
        if not key:
            return {}
        return self.store.get_msg_map(key, topic_msg_id)

    def _tg_file_url(self, file_id: str) -> str:
        """TG getFile API → 拿 file_path → 組 URL。"""
        try:
            r, err = self.bot._post("getFile", {"file_id": file_id})
            if err:
                return ""
            fp = r.get("file_path", "")
            if not fp:
                return ""
            return f"https://api.telegram.org/file/bot{self.bot.token}/{fp}"
        except Exception:
            return ""

    def _enrich_yahoo_items(self, text: str) -> str:
        """從文字提取 Yahoo 商品 ID 並查 D1,返回多行 markdown(無命中返空)。

        對買家發來的商品連結/編號,push 一條附加卡片顯示貨源、編號、owner。
        """
        if not text:
            return ""
        try:
            from .tg_conversation import (
                extract_yahoo_item_ids, _query_product_d1,
                _classify_source, _to_mobile_xianyu_url,
            )
        except Exception:
            return ""
        ids = extract_yahoo_item_ids(text)
        if not ids:
            return ""
        lines = []
        for yid in ids:
            d = _query_product_d1(yid, on_log=self.on_log)
            if not d:
                lines.append(f"  • `{yid}` — ❌ D1 沒記錄")
                continue
            barcode = d.get("barcode", "")
            source, src_url = _classify_source(barcode)
            mobile = _to_mobile_xianyu_url(src_url) if source == "xianyu" else src_url
            owner = d.get("owner", "") or "?"
            pcode = d.get("product_code", "") or "?"
            account = d.get("account", "") or "?"
            src_label = {"mercari": "煤炉", "xianyu": "闲鱼"}.get(source, source or "未知")
            line = f"  • `{yid}` → {src_label} | owner={owner} | account={account}"
            if mobile:
                line += f"\n    [→ 貨源頁]({mobile})"
            lines.append(line)
        if not lines:
            return ""
        return "📦 *D1 貨源查詢*\n" + "\n".join(lines)

    def _build_info_card_text(
        self,
        *,
        account_name: str,
        buyer_label: str,
        yahoo_chat_id: str,
        current_profile_id: str = "",
        product_title: str = "",
        product_no: str = "",
        yahoo_price: str = "",
        yahoo_shipping: str = "",
        source_platform: str = "",
        source_status: str = "",
        source_url: str = "",
        last_haggle: str = "",
        purchase_status: str = "",
        is_fragile: str = "",
        extra_lines: Optional[list] = None,
    ) -> str:
        """構造資訊卡內容(Markdown)。空欄位省略。"""
        L = [
            "🛍 *Yahoo 拍賣 IM 對話*",
            "━━━━━━━━━━━━━━━━━━━",
            f"🏪 賣家: `{account_name}`",
            f"👤 買家: {buyer_label} `{yahoo_chat_id}`",
        ]

        # ── 買家評價(從 booth 頁抓,24h 緩存)──
        try:
            from .yahoo_buyer_rating import fetch_buyer_rating, format_rating_for_card
            rating, err = fetch_buyer_rating(yahoo_chat_id, on_log=self.on_log)
            if rating:
                block = format_rating_for_card(rating)
                if block:
                    L.append("━━━━━━━━━━━━━━━━━━━")
                    L.extend(block.split("\n"))
        except Exception as e:
            self.on_log(f"[TG-FORUM] buyer rating 抓失敗(忽略): {e}")

        # ── 即時狀態:對方上線時間 + 已讀/未讀(BOSH 拉,點 🔄 刷新)──
        try:
            from .yahoo_im_read_status import fetch_read_status, format_read_status_for_card
            from .im_http_ops import build_channel_id
            profile_dir = self.base_dir / "profiles" / current_profile_id
            if profile_dir.exists():
                my_id, _ = self._resolve_my_id(profile_dir)
                if my_id:
                    buyer_y = yahoo_chat_id if yahoo_chat_id.upper().startswith("Y") else f"Y{yahoo_chat_id}"
                    channel = build_channel_id(my_id, buyer_y)
                    status, err = fetch_read_status(
                        profile_dir,
                        channel_id=channel,
                        my_id=my_id.lower(),
                        on_log=lambda *_: None,
                    )
                    if status:
                        block = format_read_status_for_card(status)
                        if block:
                            L.append("━━━━━━━━━━━━━━━━━━━")
                            L.append("📡 即時狀態")
                            L.extend(block.split("\n"))

                    # v6.1:買家深度智能 — 議價 / 回應時間 / 首次互動 + AI 優質度
                    try:
                        from .yahoo_buyer_intel import (
                            analyze_buyer_conversation,
                            evaluate_buyer_quality_with_ai,
                            format_intel_for_card,
                        )
                        intel, _ = analyze_buyer_conversation(
                            profile_dir, channel_id=channel,
                            my_id=my_id.lower(), on_log=lambda *_: None,
                        )
                        # rating 可能已從上面拿過(這裡 use_cache=True 拿緩存)
                        from .yahoo_buyer_rating import fetch_buyer_rating
                        rating_cached, _ = fetch_buyer_rating(yahoo_chat_id, use_cache=True)
                        quality = evaluate_buyer_quality_with_ai(
                            rating_cached, intel, buyer_label=buyer_label,
                        )
                        intel_block = format_intel_for_card(intel, quality)
                        if intel_block:
                            L.append("━━━━━━━━━━━━━━━━━━━")
                            L.append("🛒 客戶深度智能")
                            L.extend(intel_block.split("\n"))
                    except Exception as e:
                        self.on_log(f"[TG-FORUM] buyer intel 失敗(忽略): {e}")
        except Exception as e:
            self.on_log(f"[TG-FORUM] read status 抓失敗(忽略): {e}")
        if product_title or product_no:
            L.append("━━━━━━━━━━━━━━━━━━━")
            if product_title:
                L.append(f"📦 商品: {product_title[:80]}")
            if product_no:
                L.append(f"🔢 編號: `{product_no}`")
            if yahoo_price:
                L.append(f"💰 Yahoo 定價: {yahoo_price}")
            if yahoo_shipping:
                L.append(f"🚚 運費: {yahoo_shipping}")
            if is_fragile:
                L.append(f"⚠️ 易碎: {is_fragile}")
        if source_platform or source_status:
            L.append("━━━━━━━━━━━━━━━━━━━")
            if source_platform:
                L.append(f"🛒 貨源: {source_platform}")
            if source_status:
                L.append(f"📊 狀態: {source_status}")
            if source_url:
                # 連結用 markdown
                L.append(f"🔗 [貨源頁]({source_url})")
        if purchase_status or last_haggle:
            L.append("━━━━━━━━━━━━━━━━━━━")
            if purchase_status:
                L.append(f"📦 採購: {purchase_status}")
            if last_haggle:
                L.append(f"💬 最近議價: {last_haggle}")
        if extra_lines:
            L.append("━━━━━━━━━━━━━━━━━━━")
            L.extend(extra_lines[:5])
        # 跨帳號同 buyer 提示
        try:
            other_topics = self.store.find_by_buyer(yahoo_chat_id)
            # 過濾當前帳號自己(避免顯示自己 topic 在列表內)
            other_topics = [
                t for t in other_topics
                if t.get("profile_id") and t.get("profile_id") != current_profile_id
            ]
            if other_topics:
                L.append("━━━━━━━━━━━━━━━━━━━")
                L.append("🔗 *該客戶在其他帳號也有對話:*")
                for t in other_topics[:5]:
                    pid = t.get("profile_id", "")
                    tid = t.get("topic_id", 0)
                    L.append(f"  • [{pid}] (topic #{tid})")
        except Exception:
            pass

        L.append("━━━━━━━━━━━━━━━━━━━")
        L.append("💬 *直接打字* → 自動 send 買家")
        L.append("📷 *傳圖/視頻* → 自動轉發")
        L.append("↩️ *引用訊息回覆* → 原生 reply 連結")
        L.append("🗑 *引用某條 + 打「撤回」* → 雙端撤回")
        return "\n".join(L)

    def update_info_card(
        self,
        profile_id: str,
        yahoo_chat_id: str,
        **fields,
    ) -> Tuple[bool, str]:
        """動態更新 topic 頂部資訊卡。

        Args 同 _build_info_card_text:product_title/product_no/yahoo_price/...
        """
        key = self._conv_key(profile_id, yahoo_chat_id)
        entry = self.store.get_entry(key)
        if not entry:
            return False, f"無對應 entry: {key}"
        info_msg_id = entry.get("info_msg_id", 0)
        if not info_msg_id:
            return False, "info_msg_id 缺失"

        text = self._build_info_card_text(
            account_name=entry.get("title", "").lstrip("[").split("]", 1)[0] or profile_id,
            buyer_label=fields.pop("buyer_label", "") or "對方",
            yahoo_chat_id=yahoo_chat_id,
            current_profile_id=profile_id,
            **fields,
        )
        refresh_btn = {
            "inline_keyboard": [[
                {"text": "🔄 刷新狀態", "callback_data": f"refresh_card:{key}"},
            ]],
        }
        # v6.1:用 entry.forum_chat_id 而非 default,multi-tenant 才能編對 group
        target_chat = self._resolve_chat_id_for_conv(key)
        _, err = self.bot._post("editMessageText", {
            "chat_id": target_chat,
            "message_id": info_msg_id,
            "text": text[:4096],
            "parse_mode": "Markdown",
            "reply_markup": refresh_btn,
        })
        if err and "message is not modified" not in err.lower():
            return False, err
        return True, ""

    def refresh_info_card(self, conv_key: str) -> Tuple[bool, str]:
        """callback「🔄 刷新狀態」入口:重抓 rating + read status 重 render 資訊卡。"""
        entry = self.store.get_entry(conv_key)
        if not entry:
            return False, f"無對應 entry: {conv_key}"
        profile_id = entry.get("profile_id", "")
        yahoo_chat_id = entry.get("chat_id", "")
        if not (profile_id and yahoo_chat_id):
            return False, "profile_id/chat_id 缺失"
        # 從 title 解 buyer_label
        title = entry.get("title", "") or ""
        if "] " in title:
            buyer_label = title.split("] ", 1)[1]
        else:
            buyer_label = yahoo_chat_id

        # 清 buyer rating 緩存讓真正重抓(不是 24h 老資料)
        try:
            from .yahoo_buyer_rating import _CACHE
            _CACHE.pop(yahoo_chat_id.upper(), None)
        except Exception:
            pass

        return self.update_info_card(
            profile_id, yahoo_chat_id, buyer_label=buyer_label,
        )

    def _resolve_my_id(self, profile_dir: Path) -> Tuple[str, str]:
        """從 BOSH JWT 拿 my_id (= shop Y-ID,例如 Y9000000001)。

        ensure_bosh_jwt 返回 (jwt, user, err),user 是 "y9000000001"(小寫),
        轉成 "Y9000000001" 大寫格式給 send_*_message。
        """
        try:
            from .yahoo_im_jwt import ensure_bosh_jwt
            _, user, err = ensure_bosh_jwt(profile_dir, on_log=self.on_log)
            if not user:
                return "", err or "JWT user 空"
            return user.upper() if not user.startswith("Y") else user, ""
        except Exception as e:
            return "", f"ensure_bosh_jwt 異常: {e}"

    def cleanup_old_topics(
        self,
        days_threshold: int = 730,
        *,
        max_delete_per_run: int = 500,
        api_throttle_sec: float = 0.5,
    ) -> Tuple[int, int]:
        """掃所有 topic,>days_threshold 天無活動 → deleteForumTopic + 從 store 移除。

        Args:
            days_threshold: 多少天無活動視為過期(預設 730 = 2 年)
            max_delete_per_run: 單次最多刪幾個(防 flood control)
            api_throttle_sec: 每次 delete 間隔(TG flood limit 30 msg/s,0.5s 安全)

        Returns:
            (success_count, fail_count)
        """
        # 先 flush 一下 touch_activity 累積的記憶體 state
        try:
            self.store.flush()
        except Exception:
            pass

        stale = self.store.list_stale_topics(days_threshold)
        if not stale:
            self.on_log(f"[TG-FORUM] cleanup: 沒有 >{days_threshold}d 過期 topic")
            return 0, 0

        self.on_log(
            f"[TG-FORUM] cleanup: 找到 {len(stale)} 個 >{days_threshold}d 過期 topic,"
            f"準備 delete(最多 {max_delete_per_run})"
        )
        ok_count = 0
        fail_count = 0
        # 按 last_activity 升序(最舊先刪)
        stale_sorted = sorted(
            stale,
            key=lambda x: x[1].get("last_activity_ts") or x[1].get("created_ts") or 0,
        )
        for key, entry in stale_sorted[:max_delete_per_run]:
            topic_id = entry.get("topic_id", 0)
            if not topic_id:
                self.store.remove_entry(key)
                continue
            try:
                # v6.1:用 entry 內的 forum_chat_id 刪對應 group 的 topic
                _entry_chat = entry.get("forum_chat_id") or self.bot.forum_chat_id
                ok, err = self.bot.delete_topic(int(topic_id), chat_id=_entry_chat)
                if ok:
                    self.store.remove_entry(key)
                    ok_count += 1
                    self.on_log(
                        f"[TG-FORUM] cleanup: deleted topic={topic_id} key={key} "
                        f"title={entry.get('title','')}"
                    )
                else:
                    # 401 / topic 不存在 / message_thread_not_found → 從 store 移除(已經沒了)
                    err_l = (err or "").lower()
                    if any(k in err_l for k in (
                        "not found", "topic", "thread_not_found", "deleted",
                    )):
                        self.store.remove_entry(key)
                        ok_count += 1
                        self.on_log(
                            f"[TG-FORUM] cleanup: topic={topic_id} 已不存在,"
                            f"從 store 移除 key={key}"
                        )
                    else:
                        fail_count += 1
                        self.on_log(
                            f"[TG-FORUM] cleanup fail topic={topic_id}: {err}"
                        )
            except Exception as e:
                fail_count += 1
                self.on_log(f"[TG-FORUM] cleanup 異常 topic={topic_id}: {e}")
            # throttle 防 flood
            time.sleep(api_throttle_sec)

        self.on_log(
            f"[TG-FORUM] cleanup 完成:成功 {ok_count} / 失敗 {fail_count}"
        )
        return ok_count, fail_count


# ── 簡化入口:從 settings 構造 bridge ──


def build_forum_bridge_from_settings(
    base_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
) -> Optional[YahooIMForumBridge]:
    """讀 settings.json + tg_tokens.json → 構造 bridge(若 forum 未啟用返 None)。"""
    try:
        from .accounts import load_settings
        st = load_settings() or {}
        if not bool(st.get("tg_forum_enabled", False)):
            return None
        forum_chat_id = (st.get("tg_forum_chat_id") or "").strip()
        if not forum_chat_id:
            return None

        # token 從 tg_tokens.json 拿。
        # ⚠️ ai_bot 設了 webhook → Cloudflare Worker → KV poller,Worker 不 forward supergroup
        # 訊息。所以 forum 必須用「沒設 webhook」的 bot 直接 polling。
        # 預設用 supervisor_bot(沒 webhook),其次 forum_bot_token(user 自訂),再不行最後 ai_bot。
        tokens_path = Path(base_dir) / "tg_tokens.json"
        token = ""
        if tokens_path.exists():
            try:
                tok_data = json.loads(tokens_path.read_text(encoding="utf-8"))
                token = (tok_data.get("forum_bot_token")
                         or tok_data.get("supervisor_bot_token")
                         or tok_data.get("ai_bot_token")
                         or tok_data.get("bot_token") or "").strip()
            except Exception:
                pass
        if not token:
            return None

        bot = TGForumBot(token, forum_chat_id, on_log=on_log)
        store = TGForumStore(Path(base_dir))
        return YahooIMForumBridge(bot, store, Path(base_dir), on_log=on_log)
    except Exception as e:
        log.warning("build_forum_bridge_from_settings failed: %s", e)
        return None
