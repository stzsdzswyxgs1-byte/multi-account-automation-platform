"""采购 Telegram Bot（独立 Bot，多用户）

职责：
- 独立于 AI 客服 Bot，使用单独的 Bot Token
- 支持多用户：/start 注册，按 chat_id 区分
- 长轮询接收消息，转发给 PurchaseCommandHandler
- broadcast() 广播通知给所有已注册用户
- 已注册用户持久化到 purchase_tg_users.json
- 支持 InlineKeyboard 按钮交互（callback_query）
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from core.tg_kv_poller import KvPoller


ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT_DIR / "purchase_tg_users.json"

TG_API = "https://api.telegram.org/bot{token}/{method}"


class PurchaseTelegramBot:
    """多用户采购 TG Bot，完全独立于 AI 客服 Bot。"""

    def __init__(self, on_log: Callable[[str], None],
                 relay_config: Optional[Dict[str, str]] = None):
        self.token: str = ""
        self.on_log = on_log

        # 已注册用户: {chat_id_str: {"name": str, "registered_at": float}}
        self._users: Dict[str, Dict[str, Any]] = {}
        self._users_lock = threading.Lock()

        # 消息回调: (text, message_id, chat_id) -> None
        self.on_message: Optional[Callable[[str, int, str], None]] = None
        # 按钮回调: (callback_data, callback_query_id, chat_id, message_id) -> None
        self.on_callback: Optional[Callable[[str, str, str, int], None]] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset: int = 0
        self._backoff: float = 1.0

        # KV 中转轮询
        self._kv_poller: Optional[KvPoller] = None
        if relay_config and relay_config.get("worker_url"):
            self._kv_poller = KvPoller("purchase", relay_config, on_log)

        # 启动时加载已注册用户
        self._load_users()

    # ---------- 配置与启停 ----------

    def configure(self, token: str) -> None:
        self.token = (token or "").strip()

    def start(self) -> bool:
        if not self.token:
            self.on_log("[TG-PURCHASE-BOT] 未设置 Token，无法启动")
            return False
        if self._running:
            return True
        self._running = True
        self._backoff = 1.0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        user_count = len(self._users)
        self.on_log(f"[TG-PURCHASE-BOT] 轮询已启动，已注册用户: {user_count}")
        return True

    def stop(self) -> None:
        self._running = False
        self.on_log("[TG-PURCHASE-BOT] 轮询已停止")

    def _setup_commands(self) -> None:
        """调用 setMyCommands 设置 Bot 命令菜单。"""
        # v6.0.76:優化 — 加 emoji,功能分組重新排序
        commands = [
            {"command": "start",      "description": "📌 註冊 / 重新註冊"},
            {"command": "status",     "description": "📊 系統狀態總覽"},
            # ━ 查詢 ━
            {"command": "list",       "description": "📋 查看所有採購綁定"},
            {"command": "detail",     "description": "🔍 查看詳情 (例:/detail 1)"},
            {"command": "tracking",   "description": "🚚 查看監控中的狀態"},
            {"command": "orders",     "description": "📦 查看最近訂單變化"},
            # ━ 綁定操作 ━
            {"command": "bind",       "description": "➕ 綁定採購訂單"},
            {"command": "edit",       "description": "✏️ 修改綁定資訊"},
            # ━ 監控控制 ━
            {"command": "watch",      "description": "🟢 開啟監控 (例:/watch 1)"},
            {"command": "unwatch",    "description": "⚪ 取消監控 (例:/unwatch 1)"},
            {"command": "watchall",   "description": "🟢 全部開啟監控"},
            {"command": "unwatchall", "description": "⚪ 全部取消監控"},
            {"command": "startmon",   "description": "▶️ 啟動採購監控"},
            {"command": "stopmon",    "description": "⏹ 停止採購監控"},
            {"command": "fetch",      "description": "🔄 手動抓取一次"},
            # ━ 出貨 ━
            {"command": "ship",       "description": "🚚 創建出貨任務 (例:/ship 1)"},
            # ━ 其他 ━
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
            self.on_log(f"[TG-PURCHASE-BOT] 设置命令菜单失败: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------- 发送消息 ----------

    def send_to(self, chat_id: str, text: str,
                reply_markup: Optional[Dict] = None) -> Optional[int]:
        """发送消息给指定用户，可附带 inline keyboard。"""
        if not chat_id or not self.token:
            return None
        return self._api_send_message(chat_id, text, reply_markup=reply_markup)

    def edit_message(self, chat_id: str, message_id: int, text: str,
                     reply_markup: Optional[Dict] = None) -> bool:
        """编辑已发送的消息（用于更新 inline keyboard 状态）。"""
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
        """应答 callback_query（消除按钮上的加载动画）。"""
        try:
            requests.post(
                self._api_url("answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    def broadcast(self, text: str) -> None:
        """广播消息给所有已注册用户。"""
        with self._users_lock:
            chat_ids = list(self._users.keys())
        for cid in chat_ids:
            try:
                self._api_send_message(cid, text)
            except Exception as e:
                self.on_log(f"[TG-PURCHASE-BOT] 广播到 {cid} 失败: {e}")

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
            self.on_log(f"[TG-PURCHASE-BOT] 新用户注册: {name} ({chat_id})")

        # 自动保存 tg_chat_id 到 settings.json（与管理Bot一致）
        try:
            from core.accounts import load_settings, save_settings
            s = load_settings()
            if not s.get("tg_chat_id"):
                s["tg_chat_id"] = chat_id
                save_settings(s)
                self.on_log(f"[TG-PURCHASE-BOT] 已自动绑定 tg_chat_id={chat_id}")
        except Exception:
            pass

        self._api_send_message(
            chat_id,
            f"✅ 已注册！\n"
            f"Chat ID: {chat_id}\n"
            f"用户: {name}\n"
            f"\n💡 你的 TG ID 已自动绑定，无需手动填写。",
        )
        # 注册后直接显示主菜单
        self._send_main_menu(chat_id)

    def _load_users(self) -> None:
        try:
            if USERS_FILE.exists():
                data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._users = data
        except Exception as e:
            self.on_log(f"[TG-PURCHASE-BOT] 加载用户失败: {e}")

    def _save_users(self) -> None:
        """必须在持有 _users_lock 时调用。"""
        try:
            USERS_FILE.write_text(
                json.dumps(self._users, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.on_log(f"[TG-PURCHASE-BOT] 保存用户失败: {e}")

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
            # Markdown 解析失败时回退纯文本
            if "can't parse" in str(data.get("description", "")).lower():
                payload.pop("parse_mode", None)
                r2 = requests.post(
                    self._api_url("sendMessage"), json=payload, timeout=15,
                )
                data2 = r2.json()
                if data2.get("ok"):
                    return data2["result"]["message_id"]
            self.on_log(f"[TG-PURCHASE-BOT] sendMessage 失败: {data.get('description', '')}")
            return None
        except Exception as e:
            self.on_log(f"[TG-PURCHASE-BOT] sendMessage 异常: {e}")
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
        # 在后台线程中设置命令菜单（避免阻塞主线程）
        self._setup_commands()
        if self._kv_poller and self._kv_poller.is_configured:
            self.on_log(f"[TG-PURCHASE-BOT] 使用 KV 中转模式轮询 (绑定TG ID: {self._kv_poller.user_id})")
            self._poll_loop_kv()
        else:
            self.on_log("[TG-PURCHASE-BOT] 使用 getUpdates 直连模式轮询")
            self._poll_loop_direct()

    def _poll_loop_kv(self) -> None:
        """v6.2:走統一 run_kv_poll_loop(指數退避 + 全局網路健康 + log 降噪)。"""
        from core.network_health import run_kv_poll_loop
        run_kv_poll_loop(
            poller=self._kv_poller,
            poller_name="KV-PURCHASE",
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
                self.on_log(f"[TG-PURCHASE-BOT] 轮询异常: {e}")
                time.sleep(min(self._backoff, 60.0))
                self._backoff = min(self._backoff * 2, 60.0)

    def _handle_update(self, upd: Dict[str, Any]) -> None:
        # ---- 处理 callback_query（按钮点击） ----
        cbq = upd.get("callback_query")
        if cbq:
            self._handle_callback_query(cbq)
            return

        # ---- 处理普通消息 ----
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

        if not text:
            return

        # /start：注册用户
        if text == "/start":
            self._register_user(msg_chat_id, from_user)
            return

        # /help：显示主菜单（按钮）
        if text == "/help":
            self._send_main_menu(msg_chat_id)
            return

        # 其他消息：必须已注册
        with self._users_lock:
            if msg_chat_id not in self._users:
                self._api_send_message(
                    msg_chat_id,
                    "⚠️ 请先发送 /start 注册。",
                )
                return

        # 转发给 PurchaseCommandHandler（带 chat_id）
        if self.on_message:
            self.on_message(text, message_id, msg_chat_id)

    # ---------- callback_query 处理 ----------

    def _handle_callback_query(self, cbq: Dict[str, Any]) -> None:
        """处理 inline keyboard 按钮点击。"""
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

        # 必须已注册
        with self._users_lock:
            if chat_id not in self._users:
                self.answer_callback(cb_id, "请先发送 /start 注册")
                return

        # 应答按钮（消除加载动画）
        self.answer_callback(cb_id)

        # 转发给 PurchaseCommandHandler
        if self.on_callback:
            self.on_callback(data, cb_id, chat_id, message_id)

    # ---------- 主菜单（inline keyboard） ----------

    @staticmethod
    def make_keyboard(buttons: List[List[Dict[str, str]]]) -> Dict:
        """构建 InlineKeyboardMarkup。
        buttons: [[{"text": "显示文字", "callback_data": "cb_data"}, ...], ...]
        """
        return {
            "inline_keyboard": [
                [{"text": b["text"], "callback_data": b["callback_data"]}
                 for b in row]
                for row in buttons
            ]
        }

    def _send_main_menu(self, chat_id: str) -> None:
        """发送主菜单（按钮式）。"""
        kb = self.make_keyboard([
            [
                {"text": "📊 系统状态", "callback_data": "menu:status"},
                {"text": "📋 绑定列表", "callback_data": "menu:list"},
            ],
            [
                {"text": "🔍 监控状态", "callback_data": "menu:tracking"},
                {"text": "📦 最近订单", "callback_data": "menu:orders"},
            ],
            [
                {"text": "➕ 新建绑定", "callback_data": "menu:bind"},
                {"text": "✏️ 修改绑定", "callback_data": "menu:edit"},
            ],
            [
                {"text": "▶️ 启动监控", "callback_data": "menu:startmon"},
                {"text": "⏹ 停止监控", "callback_data": "menu:stopmon"},
            ],
            [
                {"text": "🔄 手动抓取", "callback_data": "menu:fetch"},
                {"text": "❌ 取消操作", "callback_data": "menu:cancel"},
            ],
        ])
        self._api_send_message(
            chat_id,
            "📦 采购订单管理 - 请选择操作：",
            reply_markup=kb,
        )
