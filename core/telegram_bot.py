"""Telegram Bot 核心模块（纯 requests，不加新依赖）

职责：
- 发送消息到指定 chat_id
- 长轮询接收用户回复
- /start 命令自动记录 Chat ID
- 回调分发给 ConversationManager
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from core.accounts import load_settings, save_settings
from core.tg_kv_poller import KvPoller


TG_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self, on_log: Callable[[str], None],
                 relay_config: Optional[Dict[str, str]] = None):
        self.token: str = ""
        self.chat_id: str = ""
        self.on_message: Optional[Callable[[str, int, Optional[int]], None]] = None  # (text, message_id, reply_to_msg_id)
        self.on_callback: Optional[Callable[[str, str, int], None]] = None  # (data, chat_id, msg_id)
        # v6.0.83:Forum supergroup topic 回覆 callback
        # (message_thread_id, text, message_id, from_user_id, photo_file_id, video_file_id,
        #  reply_to_text, reply_to_msg_id)
        # reply_to_* 用於「引用某條訊息回覆」場景 — text 拿不到就傳空字串
        self.on_forum_message: Optional[Callable[..., None]] = None
        # v6.0.83:Forum supergroup chat_id(用來區分 forum 訊息 vs 一般私聊)
        self.forum_chat_id: str = ""
        # v6.0.83:當前訊息的 from_user.id(per-message,給 on_message callback 拿 ACL 用)
        self.last_from_user_id: str = ""
        self.on_log = on_log

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset: int = 0
        self._backoff: float = 1.0

        # KV 中转轮询
        self._kv_poller: Optional[KvPoller] = None
        if relay_config and relay_config.get("worker_url"):
            self._kv_poller = KvPoller("ai", relay_config, on_log)

    # ---------- 公开方法 ----------

    def configure(self, token: str, chat_id: str = "") -> None:
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.on_log(f"[TG-DIAG] configure: token={'有' if self.token else '⚠️空'}, chat_id={self.chat_id or '⚠️空'}")

    def start(self) -> bool:
        if not self.token:
            self.on_log("[TG] 未设置 Bot Token，无法启动")
            return False
        if self._running:
            return True
        self._running = True
        self._backoff = 1.0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.on_log("[TG] Bot 轮询已启动")
        return True

    def stop(self) -> None:
        self._running = False
        self.on_log("[TG] Bot 轮询已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    def send(self, text: str, chat_id: str = "") -> Optional[int]:
        """发送消息，返回 message_id 或 None。"""
        cid = (chat_id or self.chat_id or "").strip()
        if not cid:
            self.on_log("[TG] ⚠️ 发送失败: chat_id 为空，请先向 Bot 发送 /start 绑定")
            return None
        if not self.token:
            self.on_log("[TG] ⚠️ 发送失败: Bot Token 未设置")
            return None
        return self._api_send_message(cid, text)

    def send_with_reply_keyboard(self, text: str, keyboard: List[List[str]],
                                chat_id: str = "",
                                resize: bool = True) -> Optional[int]:
        """发送带持久键盘按钮的消息。keyboard 格式: [["按钮1", "按钮2"]]"""
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token:
            self.on_log(f"[TG] ⚠️ send_with_reply_keyboard 失败: cid={cid!r}, token={'有' if self.token else '无'}")
            return None
        buttons = [[{"text": t} for t in row] for row in keyboard]
        try:
            r = requests.post(
                self._api_url("sendMessage"),
                json={
                    "chat_id": cid,
                    "text": text,
                    "reply_markup": {
                        "keyboard": buttons,
                        "resize_keyboard": resize,
                        "is_persistent": True,
                    },
                },
                timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            return None
        except Exception as e:
            self.on_log(f"[TG] ⚠️ send_with_reply_keyboard 异常: {e}")
            return None

    def remove_reply_keyboard(self, text: str, chat_id: str = "") -> Optional[int]:
        """发送消息并移除持久键盘。"""
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token:
            self.on_log(f"[TG] ⚠️ remove_reply_keyboard 失败: cid={cid!r}, token={'有' if self.token else '无'}")
            return None
        try:
            r = requests.post(
                self._api_url("sendMessage"),
                json={
                    "chat_id": cid,
                    "text": text,
                    "reply_markup": {"remove_keyboard": True},
                },
                timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            return None
        except Exception as e:
            self.on_log(f"[TG] ⚠️ remove_reply_keyboard 异常: {e}")
            return None

    def send_inline_keyboard(self, text: str, buttons: List[List[Dict[str, str]]],
                             chat_id: str = "",
                             disable_web_page_preview: bool = True) -> Optional[int]:
        """发送带 inline keyboard 的消息。buttons 格式: [[{"text": "...", "callback_data": "..."}]]
        v6.0.74:默认关闭 URL 预览 — 按钮区不需要被链接图片挤走。
        """
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token:
            self.on_log(f"[TG] ⚠️ send_inline_keyboard 失败: cid={cid!r}, token={'有' if self.token else '无'}")
            return None
        try:
            r = requests.post(
                self._api_url("sendMessage"),
                json={
                    "chat_id": cid,
                    "text": text,
                    "reply_markup": {"inline_keyboard": buttons},
                    "disable_web_page_preview": disable_web_page_preview,
                },
                timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            self.on_log(f"[TG] sendInlineKeyboard 失败: {data.get('description', '')}")
            return None
        except Exception as e:
            self.on_log(f"[TG] sendInlineKeyboard 异常: {e}")
            return None

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """回复 callback query，消除按钮加载动画。"""
        try:
            requests.post(
                self._api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    def send_force_reply(self, text: str, chat_id: str = "",
                         reply_to_msg_id: Optional[int] = None,
                         disable_web_page_preview: bool = True) -> Optional[int]:
        """v6.0.74 新增:发送一条 force_reply 消息。

        TG 客户端会自动把输入框聚焦到引用回复模式,用户打字直接发送即可。
        Bot 端通过 reply_to_message 关联到 pending action。
        v6.0.74:默认关闭 URL 预览,避免提示消息被大图挤走。
        """
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token:
            return None
        payload = {
            "chat_id": cid,
            "text": text,
            "reply_markup": {"force_reply": True, "selective": False},
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to_msg_id:
            payload["reply_to_message_id"] = reply_to_msg_id
            payload["allow_sending_without_reply"] = True
        try:
            r = requests.post(self._api_url("sendMessage"), json=payload, timeout=15)
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            self.on_log(f"[TG] send_force_reply 失败: {data.get('description', '')}")
            return None
        except Exception as e:
            self.on_log(f"[TG] send_force_reply 异常: {e}")
            return None

    def edit_message_reply_markup(self, message_id: int,
                                   buttons: Optional[List[List[Dict[str, str]]]] = None,
                                   chat_id: str = "") -> bool:
        """v6.0.74 新增:更新已发消息的 inline_keyboard (用于点完按钮后清除按钮)。
        buttons=None 表示移除所有按钮。
        """
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token or not message_id:
            return False
        payload = {"chat_id": cid, "message_id": message_id}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        try:
            r = requests.post(self._api_url("editMessageReplyMarkup"), json=payload, timeout=10)
            return bool(r.json().get("ok"))
        except Exception:
            return False

    def edit_message_text(self, message_id: int, text: str, chat_id: str = "",
                          buttons: Optional[List[List[Dict[str, str]]]] = None) -> bool:
        """v6.0.74 新增:更新已发消息文字+按钮 (用于「✓ 已发送」状态切换)。"""
        cid = (chat_id or self.chat_id or "").strip()
        if not cid or not self.token or not message_id:
            return False
        payload = {"chat_id": cid, "message_id": message_id, "text": text}
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = requests.post(self._api_url("editMessageText"), json=payload, timeout=10)
            return bool(r.json().get("ok"))
        except Exception:
            return False

    # ---------- TG API 封装 ----------

    def _set_my_commands(self) -> None:
        """设置 Bot 命令菜单和菜单按钮。"""
        # v6.0.76:優化 — 加 emoji
        # v6.0.83:加 /accounts /menu — TG forum 命令面板(列 Yahoo 帳號 → 客戶 → 聊天記錄 → reply)
        commands = [
            {"command": "start",     "description": "📌 綁定 Chat ID / 重新註冊"},
            {"command": "status",    "description": "💬 查看 AI 對話列表"},
            {"command": "pending",   "description": "📋 待處理對話清單"},
            {"command": "accounts",  "description": "🛍 Yahoo 帳號 / 客戶列表 / 聊天記錄"},
            {"command": "menu",      "description": "📋 顯示完整命令面板"},
            {"command": "translate", "description": "🌏 中日雙向翻譯"},
            {"command": "help",      "description": "🏠 顯示說明"},
        ]
        try:
            requests.post(
                self._api_url("setMyCommands"),
                json={"commands": commands},
                timeout=10,
            )
        except Exception:
            pass
        # 设置聊天菜单按钮，让用户在输入框旁看到 "菜單" 按钮
        try:
            requests.post(
                self._api_url("setChatMenuButton"),
                json={
                    "menu_button": {
                        "type": "commands",
                    },
                },
                timeout=10,
            )
        except Exception:
            pass

    def _api_url(self, method: str) -> str:
        return TG_API.format(token=self.token, method=method)

    def _api_send_message(
        self, chat_id: str, text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        message_thread_id: Optional[int] = None,
    ) -> Optional[int]:
        payload_extra: Dict[str, Any] = {}
        if reply_markup:
            payload_extra["reply_markup"] = reply_markup
        if message_thread_id:
            payload_extra["message_thread_id"] = message_thread_id
        try:
            r = requests.post(
                self._api_url("sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", **payload_extra},
                timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            # Markdown 解析失败时回退纯文本
            if "can't parse" in str(data.get("description", "")).lower():
                r2 = requests.post(
                    self._api_url("sendMessage"),
                    json={"chat_id": chat_id, "text": text, **payload_extra},
                    timeout=15,
                )
                data2 = r2.json()
                if data2.get("ok"):
                    return data2["result"]["message_id"]
            self.on_log(f"[TG] sendMessage 失败: {data.get('description', '')}")
            return None
        except Exception as e:
            self.on_log(f"[TG] sendMessage 异常: {e}")
            return None

    def _api_get_updates(self, timeout: int = 30) -> List[Dict[str, Any]]:
        try:
            r = requests.get(
                self._api_url("getUpdates"),
                params={"offset": self._offset, "timeout": timeout},
                timeout=timeout + 10,
            )
            data = r.json()
            if data.get("ok"):
                return data.get("result", [])
            return []
        except Exception:
            return []

    # ---------- 轮询循环 ----------

    def _poll_loop(self) -> None:
        self._set_my_commands()  # 在后台线程执行，不阻塞 UI
        if self._kv_poller and self._kv_poller.is_configured:
            self.on_log(f"[TG] 使用 KV 中转模式轮询 (绑定TG ID: {self._kv_poller.user_id})")
            self._poll_loop_kv()
        else:
            self.on_log("[TG] 使用 getUpdates 直连模式轮询")
            self._poll_loop_direct()

    def _poll_loop_kv(self) -> None:
        """v6.2:走統一 run_kv_poll_loop(指數退避 + 全局網路健康 + log 降噪)。"""
        from core.network_health import run_kv_poll_loop
        run_kv_poll_loop(
            poller=self._kv_poller,
            poller_name="KV-AI",
            on_update=self._handle_update,
            on_log=self.on_log,
            is_running=lambda: self._running,
        )

    def _poll_loop_direct(self) -> None:
        """传统 getUpdates 直连轮询。"""
        while self._running:
            try:
                updates = self._api_get_updates(timeout=30)
                if updates:
                    self._backoff = 1.0
                    for upd in updates:
                        self._offset = max(self._offset, upd["update_id"] + 1)
                        self._handle_update(upd)
                else:
                    self._backoff = 1.0
            except Exception as e:
                self.on_log(f"[TG] 轮询异常: {e}")
                time.sleep(min(self._backoff, 60.0))
                self._backoff = min(self._backoff * 2, 60.0)

    def _handle_update(self, upd: Dict[str, Any]) -> None:
        # --- callback_query 处理 ---
        cbq = upd.get("callback_query")
        if cbq:
            self._handle_callback_query(cbq)
            return

        msg = upd.get("message")
        if not msg:
            return

        text = (msg.get("text") or "").strip()
        chat = msg.get("chat", {})
        from_user = msg.get("from", {})
        msg_chat_id = str(chat.get("id", ""))
        message_id = int(msg.get("message_id", 0))

        # /chatid 通用 handler — 任何 chat type 都 reply chat_id
        # 用於 forum supergroup 啟用前拿 chat_id 設定 settings.json
        if text.lower().split("@")[0] == "/chatid":
            try:
                chat_type = chat.get("type", "?")
                is_forum = chat.get("is_forum", False)
                thread_id = msg.get("message_thread_id", "")
                reply = (
                    f"📋 *Chat ID*\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"chat_id: `{msg_chat_id}`\n"
                    f"type: {chat_type}\n"
                    f"is_forum: {is_forum}\n"
                )
                if thread_id:
                    reply += f"message_thread_id: `{thread_id}`\n"
                if chat_type == "supergroup" and is_forum:
                    reply += (
                        f"\n✅ 這是 forum supergroup,可用於 tg_forum_chat_id\n"
                        f"加到 `settings.json`:\n"
                        f"```\n"
                        f'  "tg_forum_enabled": true,\n'
                        f'  "tg_forum_chat_id": "{msg_chat_id}"\n'
                        f"```\n"
                        f"設完重啟軟件即啟用 forum 模式"
                    )
                self._api_send_message(
                    msg_chat_id, reply,
                    message_thread_id=thread_id if thread_id else None,
                )
            except Exception as e:
                self.on_log(f"[TG] /chatid handler 異常: {e}")
            return

        # 提取引用回复的 message_id（用于定位目标对话）
        reply_to = msg.get("reply_to_message")
        reply_to_msg_id: Optional[int] = None
        if reply_to:
            reply_to_msg_id = int(reply_to.get("message_id", 0)) or None

        # v6.0.83:暴露當前 message 的 from_user.id 給 callback 使用(多用戶 ACL)
        self.last_from_user_id = str(from_user.get("id", ""))

        # v6.0.83:暴露 photo/video file_id(per-message)— 給 reply photo/video 用
        # 私聊內含媒體 → 文字在 caption,要 fallback;file_id 給 _on_tg_reply 拿
        self.last_photo_file_id: str = ""
        self.last_video_file_id: str = ""
        try:
            _photos = msg.get("photo") or []
            if _photos:
                # photo array,最後一個是最大解析度
                self.last_photo_file_id = _photos[-1].get("file_id", "")
            _video = msg.get("video")
            if _video:
                self.last_video_file_id = _video.get("file_id", "")
            _anim = msg.get("animation")
            if _anim and not self.last_video_file_id:
                self.last_video_file_id = _anim.get("file_id", "")
            # caption fallback (圖片/視頻訊息 text 在 caption)
            if not text:
                text = (msg.get("caption") or "").strip()
        except Exception:
            pass

        # v6.0.83:Forum supergroup topic 回覆 — 不在私聊但帶 message_thread_id
        message_thread_id = msg.get("message_thread_id")
        if (chat.get("type") in ("supergroup",)
                and message_thread_id
                and self.forum_chat_id
                and msg_chat_id == self.forum_chat_id):
            try:
                # 過濾自己 bot 在 topic 內發的訊息(避免循環)
                if from_user.get("is_bot"):
                    return
                # caption fallback(圖片/視頻訊息 text 是空,文字在 caption)
                forum_text = text or (msg.get("caption") or "").strip()
                # 提取媒體 file_id
                photo_file_id = None
                video_file_id = None
                photos = msg.get("photo") or []
                if photos:
                    # photo 是 array, 取最大尺寸的 file_id
                    photo_file_id = photos[-1].get("file_id")
                video = msg.get("video")
                if video:
                    video_file_id = video.get("file_id")
                # animation (gif)
                animation = msg.get("animation")
                if animation and not video_file_id:
                    video_file_id = animation.get("file_id")
                # 提取「引用某條訊息」上下文(用戶在 topic 內 reply 客戶某條訊息)
                reply_to_text = ""
                reply_to_msg_id_forum = 0
                _rto = msg.get("reply_to_message") or {}
                if _rto and _rto.get("message_thread_id") == message_thread_id:
                    # 排除 topic 的根訊息(forum topic 開頭那條 service msg)
                    if not _rto.get("forum_topic_created"):
                        reply_to_msg_id_forum = int(_rto.get("message_id", 0) or 0)
                        # 拿原文:text 或 caption
                        reply_to_text = (_rto.get("text") or _rto.get("caption") or "").strip()
                if self.on_forum_message:
                    from_user_id = str(from_user.get("id", ""))
                    try:
                        self.on_forum_message(
                            int(message_thread_id),
                            forum_text,
                            message_id,
                            from_user_id,
                            photo_file_id,
                            video_file_id,
                            reply_to_text,
                            reply_to_msg_id_forum,
                        )
                    except TypeError:
                        # 舊版 callback(沒接 reply_to_*)— 兼容
                        self.on_forum_message(
                            int(message_thread_id),
                            forum_text,
                            message_id,
                            from_user_id,
                            photo_file_id,
                            video_file_id,
                        )
            except Exception as e:
                try: self.on_log(f"[TG] forum on_message 異常: {e}")
                except: pass
            return

        # 只处理私聊，忽略群聊/超级群聊
        if chat.get("type") not in ("private",):
            return

        # v6.0.83:純圖片/視頻訊息(無 text/caption)也可以通過 — 留給 on_message 用 file_id
        if not text and not self.last_photo_file_id and not self.last_video_file_id:
            return

        # /start 命令：自动绑定 Chat ID
        if text == "/start":
            self._handle_start(msg_chat_id, from_user)
            return

        # /help 命令
        if text == "/help":
            self._handle_help(msg_chat_id)
            return

        # /status 命令
        if text == "/status":
            if self.on_message:
                self.on_message("/status", message_id, None)
            return

        # 只处理已绑定的 chat_id 的消息
        if self.chat_id and msg_chat_id != self.chat_id:
            self._api_send_message(msg_chat_id, "⚠️ 你不是已绑定的用户。")
            return

        # /translate 命令：翻译功能（需要绑定后才能使用）
        if text == "/translate":
            self._handle_translate_menu(msg_chat_id)
            return

        # 转发给 ConversationManager
        if self.on_message:
            self.on_message(text, message_id, reply_to_msg_id)

    def _handle_callback_query(self, cbq: Dict[str, Any]) -> None:
        """处理 inline keyboard 按钮回调。"""
        cbq_id = str(cbq.get("id", ""))
        data = (cbq.get("data") or "").strip()
        msg = cbq.get("message", {})
        chat = msg.get("chat", {})
        msg_chat_id = str(chat.get("id", ""))
        message_id = int(msg.get("message_id", 0))

        # v6.0.83:暴露 from_user.id(per-callback)
        cbq_from = cbq.get("from", {})
        self.last_from_user_id = str(cbq_from.get("id", ""))

        # 消除按钮加载动画
        self.answer_callback_query(cbq_id)

        if not data:
            return

        # 分发给注册的回调处理器
        if self.on_callback:
            self.on_callback(data, msg_chat_id, message_id)

    def _handle_translate_menu(self, chat_id: str) -> None:
        """发送翻译功能选择菜单。"""
        buttons = [
            [
                {"text": "中文 → 日本語", "callback_data": "tr:zh2ja"},
                {"text": "日本語 → 中文", "callback_data": "tr:ja2zh"},
            ],
            [
                {"text": "取消", "callback_data": "tr:cancel"},
            ],
        ]
        self.send_inline_keyboard(
            "請選擇翻譯方向：\n\n"
            "中文 → 日本語：將繁體中文翻譯成日文\n"
            "日本語 → 中文：將日文翻譯成繁體中文\n\n"
            "選擇後，直接發送要翻譯的文字即可。",
            buttons,
            chat_id,
        )

    def _handle_start(self, chat_id: str, from_user: Dict) -> None:
        self.on_log(f"[TG-DIAG] /start 绑定: chat_id={chat_id}, user={from_user.get('first_name','')}")
        self.chat_id = chat_id
        # 保存到 settings.json
        try:
            s = load_settings()
            s["tg_chat_id"] = chat_id
            save_settings(s)
        except Exception as e:
            self.on_log(f"[TG] 保存 chat_id 失败: {e}")

        name = from_user.get("first_name", "") or from_user.get("username", "")
        self._api_send_message(
            chat_id,
            f"✅ 已绑定！\n"
            f"你的 Chat ID: {chat_id}\n"
            f"用户: {name}\n\n"
            f"AI 客服通知将发送到这里。\n"
            f"发送 /help 查看使用说明。",
        )
        self.on_log(f"[TG] /start 绑定 chat_id={chat_id} user={name}")

    def _handle_help(self, chat_id: str) -> None:
        self._api_send_message(
            chat_id,
            "🤖 *Yahoo IM AI 客服助手*\n\n"
            "自动流程:\n"
            "1\\. 买家发消息 → 你收到通知\n"
            "2\\. AI 生成草稿 → 你确认/编辑\n"
            "3\\. 确认后展示最终文本，你复制到 Yahoo IM\n\n"
            "操作指令:\n"
            "• 回复 `ok` → 确认发送 AI 草稿\n"
            "• 回复 `edit:内容` → 用你的内容替换\n"
            "• 回复 `skip` → 跳过当前对话\n"
            "• 直接回复文字 → 提供卖家答案\n\n"
            "命令:\n"
            "/start \\- 绑定 Chat ID\n"
            "/status \\- 查看活跃对话\n"
            "/translate \\- 中日翻譯\n"
            "/accounts \\- 列出此實例的 Yahoo 帳號 \\(內含對話列表/聊天記錄/reply\\)\n"
            "/menu \\- 顯示完整命令面板\n"
            "/help \\- 显示帮助",
        )
