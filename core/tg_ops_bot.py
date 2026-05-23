"""运营 Telegram Bot（独立 Bot，多用户）

职责：
- 独立于其他 Bot，使用单独的 Bot Token
- 支持多用户：/start 注册，按 chat_id 区分
- 长轮询接收消息，转发给 OpsCommandHandler
- broadcast() 广播通知给所有已注册用户
- 已注册用户持久化到 ops_tg_users.json
- 支持 InlineKeyboard 按钮交互（callback_query）
- 支持文件/文档接收（document）

功能范围：代付查询、物流系统、業績核對、煤爐檢查、鹹魚檢查、文檔處理
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from core.tg_kv_poller import KvPoller


ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT_DIR / "ops_tg_users.json"
DOWNLOAD_DIR = ROOT_DIR / "tg_uploads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TG_API = "https://api.telegram.org/bot{token}/{method}"


class OpsTelegramBot:
    """多用户运营 TG Bot，完全独立于其他 Bot。"""

    def __init__(self, on_log: Callable[[str], None],
                 relay_config: Optional[Dict[str, str]] = None):
        self.token: str = ""
        self.on_log = on_log

        self._users: Dict[str, Dict[str, Any]] = {}
        self._users_lock = threading.Lock()

        self.on_message: Optional[Callable[[str, int, str], None]] = None
        self.on_callback: Optional[Callable[[str, str, str, int], None]] = None
        self.on_document: Optional[Callable[[str, str, str, str], None]] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset: int = 0
        self._backoff: float = 1.0

        # KV 中转轮询（如果配置了 relay_config 则使用 KV 模式）
        self._kv_poller: Optional[KvPoller] = None
        if relay_config and relay_config.get("worker_url"):
            self._kv_poller = KvPoller("ops", relay_config, on_log)

        self._load_users()

    # ---------- 配置与启停 ----------

    def configure(self, token: str) -> None:
        self.token = (token or "").strip()

    def start(self) -> bool:
        if not self.token:
            self.on_log("[TG-OPS] 未设置 Token，无法启动")
            return False
        if self._running:
            return True
        self._running = True
        self._backoff = 1.0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        user_count = len(self._users)
        self.on_log(f"[TG-OPS] 轮询已启动，已注册用户: {user_count}")
        return True

    def stop(self) -> None:
        self._running = False
        self.on_log("[TG-OPS] 轮询已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------- 发送消息 ----------

    def send_to(self, chat_id: str, text: str,
                reply_markup: Optional[Dict] = None) -> Optional[int]:
        if not chat_id or not self.token:
            return None
        return self._api_send_message(chat_id, text, reply_markup=reply_markup)

    def edit_message(self, chat_id: str, message_id: int, text: str,
                     reply_markup: Optional[Dict] = None) -> bool:
        try:
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            r = requests.post(
                self._api_url("editMessageText"),
                json=payload, timeout=15,
            )
            return r.json().get("ok", False)
        except Exception:
            return False

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        try:
            requests.post(
                self._api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    def broadcast(self, text: str) -> None:
        with self._users_lock:
            chat_ids = list(self._users.keys())
        for cid in chat_ids:
            try:
                self._api_send_message(cid, text)
            except Exception as e:
                self.on_log(f"[TG-OPS] 广播到 {cid} 失败: {e}")

    def send_photo(self, chat_id: str, photo_path: str,
                   caption: str = "",
                   reply_markup: Optional[Dict] = None) -> Optional[int]:
        """发送本地图片到指定 chat_id。"""
        if not chat_id or not self.token:
            return None
        try:
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                if reply_markup:
                    import json as _json
                    data["reply_markup"] = _json.dumps(reply_markup)
                r = requests.post(
                    self._api_url("sendPhoto"),
                    data=data, files=files, timeout=30,
                )
            resp = r.json()
            if resp.get("ok"):
                return resp["result"]["message_id"]
            self.on_log(f"[TG-OPS] sendPhoto 失败: {resp.get('description', '')}")
            return None
        except Exception as e:
            self.on_log(f"[TG-OPS] sendPhoto 异常: {e}")
            return None

    # ---------- 用户管理 ----------

    def get_registered_users(self) -> Dict[str, Dict[str, Any]]:
        with self._users_lock:
            return dict(self._users)

    def _register_user(self, chat_id: str, from_user: Dict) -> None:
        name = (
            from_user.get("first_name", "")
            or from_user.get("username", "")
            or "unknown"
        )
        with self._users_lock:
            is_new = chat_id not in self._users
            self._users[chat_id] = {
                "name": name,
                "registered_at": time.time(),
            }
            self._save_users()

        if is_new:
            self.on_log(f"[TG-OPS] 新用户注册: {name} ({chat_id})")

        # 自动保存 tg_chat_id 到 settings.json（与管理Bot一致）
        try:
            from core.accounts import load_settings, save_settings
            s = load_settings()
            if not s.get("tg_chat_id"):
                s["tg_chat_id"] = chat_id
                save_settings(s)
                self.on_log(f"[TG-OPS] 已自动绑定 tg_chat_id={chat_id}")
        except Exception:
            pass

        self._api_send_message(
            chat_id,
            f"✅ 已注册运营 Bot！\n"
            f"Chat ID: {chat_id}\n"
            f"用户: {name}\n"
            f"\n💡 你的 TG ID 已自动绑定，无需手动填写。",
        )
        self._send_main_menu(chat_id)

    def _load_users(self) -> None:
        try:
            if USERS_FILE.exists():
                data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._users = data
        except Exception as e:
            self.on_log(f"[TG-OPS] 加载用户失败: {e}")

    def _save_users(self) -> None:
        try:
            USERS_FILE.write_text(
                json.dumps(self._users, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.on_log(f"[TG-OPS] 保存用户失败: {e}")

    # ---------- 文件下载 ----------

    def download_file(self, file_id: str, save_name: str = "") -> Optional[str]:
        try:
            r = requests.post(
                self._api_url("getFile"),
                json={"file_id": file_id},
                timeout=15,
            )
            data = r.json()
            if not data.get("ok"):
                return None
            file_path = data["result"]["file_path"]
            url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                return None
            if not save_name:
                save_name = os.path.basename(file_path)
            local_path = DOWNLOAD_DIR / save_name
            local_path.write_bytes(resp.content)
            return str(local_path)
        except Exception as e:
            self.on_log(f"[TG-OPS] 下载文件失败: {e}")
            return None

    # ---------- TG API 封装 ----------

    def _api_url(self, method: str) -> str:
        return TG_API.format(token=self.token, method=method)

    def _api_send_message(self, chat_id: str, text: str,
                          reply_markup: Optional[Dict] = None) -> Optional[int]:
        try:
            payload: Dict[str, Any] = {
                "chat_id": chat_id, "text": text, "parse_mode": "Markdown",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            r = requests.post(
                self._api_url("sendMessage"), json=payload, timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            if "can't parse" in str(data.get("description", "")).lower():
                payload.pop("parse_mode", None)
                r2 = requests.post(
                    self._api_url("sendMessage"), json=payload, timeout=15,
                )
                data2 = r2.json()
                if data2.get("ok"):
                    return data2["result"]["message_id"]
            self.on_log(
                f"[TG-OPS] sendMessage 失败: "
                f"{data.get('description', '')}"
            )
            return None
        except Exception as e:
            self.on_log(f"[TG-OPS] sendMessage 异常: {e}")
            return None

    def _api_get_updates(self, timeout: int = 30) -> List[Dict[str, Any]]:
        try:
            r = requests.post(
                self._api_url("getUpdates"),
                json={
                    "offset": self._offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "callback_query"],
                },
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
        self._setup_commands()
        if self._kv_poller and self._kv_poller.is_configured:
            self.on_log(f"[TG-OPS] 使用 KV 中转模式轮询 (绑定TG ID: {self._kv_poller.user_id})")
            self._poll_loop_kv()
        else:
            self.on_log("[TG-OPS] 使用 getUpdates 直连模式轮询")
            self._poll_loop_direct()

    def _poll_loop_kv(self) -> None:
        """v6.2:走統一 run_kv_poll_loop(指數退避 + 全局網路健康 + log 降噪)。"""
        from core.network_health import run_kv_poll_loop
        run_kv_poll_loop(
            poller=self._kv_poller,
            poller_name="KV-OPS",
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
                self.on_log(f"[TG-OPS] 轮询异常: {e}")
                time.sleep(min(self._backoff, 60.0))
                self._backoff = min(self._backoff * 2, 60.0)

    def _setup_commands(self) -> None:
        # v6.0.76:徹底換新 — 移除舊 /mercari /goofish,新增 /check /aichat /pquery
        commands = [
            {"command": "start",      "description": "📌 註冊 / 重新註冊"},
            {"command": "status",     "description": "📊 系統狀態總覽"},
            # ━ 物流操作 ━
            {"command": "ship",       "description": "🚚 物流/出貨狀態"},
            {"command": "sybupload",  "description": "📤 上傳出貨資料"},
            {"command": "syblabels",  "description": "🏷 上傳面單 PDF"},
            {"command": "sybmon",     "description": "📡 發貨監控"},
            {"command": "sybcheck",   "description": "📦 檢查訂單發貨"},
            {"command": "syblogin",   "description": "🔑 物流系統登入"},
            {"command": "sybrefresh", "description": "🔄 刷新物流驗證碼"},
            # ━ 數據查詢 ━
            {"command": "pay",        "description": "💰 代付情況查詢"},
            {"command": "perf",       "description": "📋 業績核對"},
            {"command": "pquery",     "description": "🔎 商品查詢"},
            # ━ AI 客服 ━
            {"command": "aichat",     "description": "💬 AI 客服待處理列表"},
            # ━ 商品檢測(已整合)━
            {"command": "check",      "description": "🛒 商品狀態檢測(閒魚+煤爐)"},
            # ━ 其他 ━
            {"command": "docs",       "description": "📄 文檔管理"},
            {"command": "cancel",     "description": "❌ 取消當前操作"},
            {"command": "help",       "description": "🏠 顯示主菜單"},
        ]
        try:
            requests.post(
                self._api_url("setMyCommands"),
                json={"commands": commands},
                timeout=10,
            )
        except Exception as e:
            self.on_log(f"[TG-OPS] 设置命令菜单失败: {e}")

    def _handle_update(self, upd: Dict[str, Any]) -> None:
        cbq = upd.get("callback_query")
        if cbq:
            self._handle_callback_query(cbq)
            return

        msg = upd.get("message")
        if not msg:
            return

        chat = msg.get("chat", {})
        # 只处理私聊，忽略群聊/超级群聊
        if chat.get("type") not in ("private",):
            return

        text = (msg.get("text") or "").strip()
        from_user = msg.get("from", {})
        msg_chat_id = str(chat.get("id", ""))
        message_id = int(msg.get("message_id", 0))

        # 处理文档上传
        doc = msg.get("document")
        if doc and msg_chat_id:
            self._handle_document(msg, msg_chat_id)
            return

        if not text:
            return

        if text == "/start":
            self._register_user(msg_chat_id, from_user)
            return

        if text == "/help":
            self._send_main_menu(msg_chat_id)
            return

        with self._users_lock:
            if msg_chat_id not in self._users:
                self._api_send_message(
                    msg_chat_id,
                    "⚠️ 请先发送 /start 注册。",
                )
                return

        if self.on_message:
            self.on_message(text, message_id, msg_chat_id)

    def _handle_document(self, msg: Dict, chat_id: str) -> None:
        with self._users_lock:
            if chat_id not in self._users:
                self._api_send_message(chat_id, "⚠️ 请先发送 /start 注册。")
                return

        doc = msg.get("document", {})
        file_id = doc.get("file_id", "")
        file_name = doc.get("file_name", "unknown")
        caption = (msg.get("caption") or "").strip()

        if not file_id:
            return

        local_path = self.download_file(file_id, save_name=file_name)
        if not local_path:
            self._api_send_message(chat_id, f"⚠️ 文件下载失败: {file_name}")
            return

        self._api_send_message(chat_id, f"📥 文件已接收: {file_name}")

        if self.on_document:
            self.on_document(local_path, file_name, caption, chat_id)

    # ---------- callback_query 处理 ----------

    def _handle_callback_query(self, cbq: Dict[str, Any]) -> None:
        cb_id = str(cbq.get("id", ""))
        data = cbq.get("data", "")
        msg = cbq.get("message", {})
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = int(msg.get("message_id", 0))

        # 只处理私聊
        if chat.get("type") not in ("private",):
            self.answer_callback(cb_id)
            return

        if not chat_id:
            self.answer_callback(cb_id)
            return

        with self._users_lock:
            if chat_id not in self._users:
                self.answer_callback(cb_id, "请先发送 /start 注册")
                return

        self.answer_callback(cb_id)

        if self.on_callback:
            self.on_callback(data, cb_id, chat_id, message_id)

    # ---------- 主菜单 ----------

    @staticmethod
    def make_keyboard(buttons: List[List[Dict[str, str]]]) -> Dict:
        return {
            "inline_keyboard": [
                [{"text": b["text"], "callback_data": b["callback_data"]}
                 for b in row]
                for row in buttons
            ]
        }

    def _send_main_menu(self, chat_id: str) -> None:
        kb = self.make_keyboard([
            [
                {"text": "📊 系统状态", "callback_data": "ops:status"},
                {"text": "💰 代付查询", "callback_data": "ops:pay"},
            ],
            [
                {"text": "🚚 物流出货", "callback_data": "ops:ship"},
                {"text": "📋 業績核對", "callback_data": "ops:perf"},
            ],
            [
                {"text": "🔍 煤爐檢查", "callback_data": "ops:mercari"},
                {"text": "🐟 鹹魚檢查", "callback_data": "ops:goofish"},
            ],
            [
                {"text": "📄 文档管理", "callback_data": "ops:docs"},
                {"text": "🔑 物流登录", "callback_data": "ops:syblogin"},
            ],
            [
                {"text": "📤 上传资料", "callback_data": "ops:sybupload"},
                {"text": "📋 上传面单", "callback_data": "ops:syblabels"},
            ],
            [
                {"text": "📦 检查发货", "callback_data": "ops:sybcheck"},
                {"text": "📡 发货监控", "callback_data": "ops:sybmon"},
            ],
            [
                {"text": "🔎 商品查询", "callback_data": "ops:pquery"},
                {"text": "📤 编码数据更新", "callback_data": "ops:pupload"},
            ],
            [
                {"text": "❌ 取消操作", "callback_data": "ops:cancel"},
            ],
        ])
        self._api_send_message(
            chat_id,
            "🔧 运营控制台 - 请选择操作：",
            reply_markup=kb,
        )
