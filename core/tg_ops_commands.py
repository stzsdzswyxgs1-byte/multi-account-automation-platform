"""TG 运营指令处理器

处理运营相关 TG 指令和 inline keyboard 按钮交互。
支持：代付查询、物流系统、業績核對、煤爐檢查、鹹魚檢查、文檔處理、異常通知。
"""
from __future__ import annotations

import os
import re
import time
import json
import threading
import requests
import openpyxl
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.purchase_feature import PurchaseLink, load_links, save_links
from core.accounts import load_settings, save_settings
from core.ai_forwarder_feature import (
    _HARDCODED_API_KEY, _HARDCODED_BASE_URL,
    _HARDCODED_ENDPOINT, _HARDCODED_MODEL,
    call_openai,
)


# ---------- 文本清理 ----------

def _sanitize(text: str) -> str:
    text = re.sub(r'[\ufffc\ufeff\u200b\u200c\u200d\u2060\u00a0]', '', text)
    text = re.sub(r'[^\S \t\n\r]+', '', text)
    return text.strip()


# ---------- 会话状态 ----------

class SessionStep(Enum):
    IDLE = auto()
    DOC_WAIT_ACTION = auto()
    DOC_WAIT_ACCOUNT = auto()
    MERCARI_WAIT_INPUT = auto()
    GOOFISH_WAIT_INPUT = auto()
    PERF_WAIT_INPUT = auto()
    SYB_WAIT_CAPTCHA = auto()
    SYB_WAIT_CHECK_ORDERS = auto()
    SYB_WAIT_MON_ORDERS = auto()
    SYB_WAIT_MON_REMOVE = auto()
    SYB_WAIT_MON_EDIT_SELECT = auto()
    SYB_WAIT_MON_EDIT_NEWNO = auto()
    PQUERY_WAIT_CODE = auto()
    PUPLOAD_WAIT_FILE = auto()


@dataclass
class UserSession:
    step: SessionStep = SessionStep.IDLE
    doc_path: str = ""
    doc_name: str = ""
    doc_target_account: str = ""
    doc_target_pid: str = ""
    created_at: float = 0.0
    mon_order_list: list = None
    mon_edit_old_no: str = ""


SESSION_TIMEOUT = 1800
ROOT_DIR = Path(__file__).resolve().parent.parent

# ---------- 云端商品查询 API ----------
PRODUCT_QUERY_WORKER_URL = "https://product-query.<PHONE_REDACTED>.workers.dev"
PRODUCT_QUERY_UPLOAD_TOKEN = "<D1_UPLOAD_TOKEN_REDACTED>"


# ---------- 主类 ----------

class OpsCommandHandler:

    def __init__(self, tg_ops_bot, on_log: Callable[[str], None],
                 app=None):
        self.tg = tg_ops_bot
        self.on_log = on_log
        self.app = app
        self._sessions: Dict[str, UserSession] = {}
        self._session_lock = threading.Lock()

    # ---- 公共入口：消息 ----

    def handle_message(self, text: str, message_id: int, chat_id: str) -> bool:
        t = _sanitize(text)
        # v6.0.76:徹底換新 — 移除 /mercari /goofish (已合併為 /check)
        cmd_map = {
            "/status":     self._cmd_status,
            "/pay":        self._cmd_pay,
            "/ship":       self._cmd_ship,
            "/perf":       self._cmd_perf,
            "/pquery":     self._cmd_pquery,
            "/check":      self._cmd_check_unified,   # v6.0.76:統一商品檢測
            "/aichat":     self._cmd_ai_chat_list,    # v6.0.76:AI 客服列表
            "/docs":       self._cmd_docs,
            "/syblogin":   self._cmd_syb_login,
            "/sybrefresh": self._cmd_syb_refresh,
            "/sybupload":  self._cmd_syb_upload,
            "/syblabels":  self._cmd_syb_labels,
            "/sybcheck":   self._cmd_syb_check,
            "/sybmon":     self._cmd_syb_mon,
            "/cancel":     self._cancel_session,
            # 保留無 / 斜線選單列出的指令(僅程式內呼叫)
            "/query":      self._cmd_pquery,
            "/upload":     self._cmd_pupload,
        }
        if t in cmd_map:
            cmd_map[t](chat_id)
            return True
        if t == "/help":
            self._send_main_menu(chat_id)
            return True
        with self._session_lock:
            has_session = chat_id in self._sessions
        if has_session:
            self._handle_session_input(chat_id, t)
            return True
        # 直接发数字 → 自动查询商品编号
        if re.fullmatch(r'\d{6,20}', t):
            self._pquery_direct(chat_id, t)
            return True
        return False

    # ---- 公共入口：按钮回调 ----

    def handle_callback(self, data: str, cb_id: str,
                        chat_id: str, message_id: int) -> None:
        parts = data.split(":", 2)
        prefix = parts[0] if parts else ""
        dispatch = {
            "ops":   self._handle_ops_cb,
            "pay":   self._handle_pay_cb,
            "ship":  self._handle_ship_cb,
            "perf":  self._handle_perf_cb,
            "doc":   self._handle_doc_cb,
            "syb":   self._handle_syb_cb,
            "sybv":  self._handle_sybv_cb,   # v6.0.75:SYB 一鍵作廢並重上傳
            "check": self._handle_check_cb,  # v6.0.76:統一商品檢測
            # v6.0.76:舊 "merc"/"goof" prefix 已移除 — 改用 "check"
            # (殘留舊訊息上的按鈕點下無反應,需重新從主菜單進入)
        }
        handler = dispatch.get(prefix)
        if handler:
            handler(chat_id, parts)

    # ---- 公共入口：文档上传 ----

    def handle_document(self, file_path: str, file_name: str,
                        caption: str, chat_id: str) -> None:
        # 如果正在等待编码数据更新的文件，走云端上传流程
        sess = self._get_session(chat_id)
        if sess and sess.step == SessionStep.PUPLOAD_WAIT_FILE:
            self._handle_pupload_file(chat_id, file_path, file_name)
            return
        self._handle_uploaded_doc(chat_id, file_path, file_name, caption)

    # ---- 内部工具 ----

    def _send(self, chat_id: str, text: str,
              reply_markup: Optional[Dict] = None) -> None:
        try:
            self.tg.send_to(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            self.on_log(f"[TG-OPS] 发送失败: {e}")

    def _send_main_menu(self, chat_id: str) -> None:
        """v6.0.75:動態主菜單 — 帶待辦狀態欄 + 重新分組,常用功能優先。"""
        try:
            text = self._build_main_menu_text()
            kb = self._build_main_menu_kb()
            self.tg.send_to(chat_id, text, reply_markup=kb)
        except Exception as e:
            # fallback 到舊版主菜單(萬一新版崩了不影響使用)
            self.on_log(f"[TG-OPS] 動態主菜單異常,退回舊版: {e}")
            self.tg._send_main_menu(chat_id)

    def _build_main_menu_text(self) -> str:
        """組裝主菜單頂部狀態文字 — 一眼看待辦量。"""
        app = self.app
        lines = ["🔧 【運營控制台】\n"]
        if not app:
            lines.append("⚠️ 應用未連接")
            return "\n".join(lines)

        # 系統狀態
        mon = "✅" if getattr(app, "monitoring", False) else "⏸"
        # 採購監控:tab 名於 v5.x 改為 purchase_ship_tab,舊版兼容 purchase_tab
        purchase_tab = (getattr(app, "purchase_ship_tab", None)
                        or getattr(app, "purchase_tab", None))
        purchase_run = False
        try:
            if purchase_tab:
                pt = getattr(purchase_tab, "_thread", None)
                if pt and pt.is_alive():
                    purchase_run = True
        except Exception:
            pass
        prc = "✅" if purchase_run else "⏸"
        lines.append(f"📡 Yahoo 監控 {mon}  |  💼 採購監控 {prc}")

        # 待辦數量
        try:
            ship_tasks = getattr(app, "ship_tasks", []) or []
            pending_ship = sum(1 for t in ship_tasks if not t.get("done"))
        except Exception:
            pending_ship = 0
        try:
            from core.purchase_feature import load_links
            links = load_links() or []
            watching = sum(1 for x in links if x.watch)
        except Exception:
            watching = 0

        # SYB 發貨監控待發貨數
        syb_pending = 0
        try:
            syb_tab = getattr(app, "syb_upload_tab", None)
            if syb_tab:
                items = getattr(syb_tab, "_mon_items", None) or {}
                syb_pending = len(items)
        except Exception:
            pass

        # AI 客服 — 待處理對話數
        # 屬性於 v4.7.x 統一為 _conv_mgr,舊版兼容 tg_conversation_mgr
        ai_pending = 0
        try:
            tg_conv = (getattr(app, "_conv_mgr", None)
                       or getattr(app, "tg_conversation_mgr", None))
            if tg_conv:
                with tg_conv._lock:
                    from core.tg_conversation import ConvPhase
                    ai_pending = sum(
                        1 for c in tg_conv._convs.values()
                        if c.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER_QUESTION,
                                       ConvPhase.PREVIEW_SELLER, ConvPhase.WAIT_SELLER,
                                       ConvPhase.AUTO_ASKING_SELLER)
                    )
        except Exception:
            pass

        lines.append("")
        lines.append("📋 【待辦事項】")
        if pending_ship:
            lines.append(f"  🚚 待出貨:{pending_ship} 條")
        if watching:
            lines.append(f"  💼 採購監控中:{watching} 條")
        if syb_pending:
            lines.append(f"  📡 發貨監控中:{syb_pending} 單")
        if ai_pending:
            lines.append(f"  🤖 AI 客服待處理:{ai_pending} 條")
        if not (pending_ship or watching or syb_pending or ai_pending):
            lines.append("  ✓ 暫無待辦")

        lines.append("")
        lines.append("選擇功能:")
        return "\n".join(lines)

    def _build_main_menu_kb(self) -> Dict:
        """重新分組的主菜單按鈕。"""
        return self.tg.make_keyboard([
            # ━ 物流操作(最常用)━
            [
                {"text": "🚚 物流出貨", "callback_data": "ops:ship"},
                {"text": "📤 上傳資料", "callback_data": "ops:sybupload"},
            ],
            [
                {"text": "🏷️ 上傳面單", "callback_data": "ops:syblabels"},
                {"text": "📡 發貨監控", "callback_data": "ops:sybmon"},
            ],
            [
                {"text": "📦 檢查發貨", "callback_data": "ops:sybcheck"},
                {"text": "🔑 物流登入", "callback_data": "ops:syblogin"},
            ],
            # ━ 數據查詢 ━
            [
                {"text": "💰 代付查詢", "callback_data": "ops:pay"},
                {"text": "📋 業績核對", "callback_data": "ops:perf"},
            ],
            [
                {"text": "🔎 商品查詢", "callback_data": "ops:pquery"},
                {"text": "📊 系統狀態", "callback_data": "ops:status"},
            ],
            # ━ AI 客服總覽 ━(v6.0.76:只看不操作,操作請到 AI 客服 Bot)
            [
                {"text": "💬 AI 客服列表", "callback_data": "ops:aichat"},
            ],
            # ━ 商品檢測 ━(v6.0.76:閒魚/煤爐已合併為統一檢測)
            [
                {"text": "🛒 商品狀態檢測", "callback_data": "ops:check"},
            ],
            # ━ 其他 ━
            [
                {"text": "📄 文檔管理", "callback_data": "ops:docs"},
                {"text": "📤 編碼更新", "callback_data": "ops:pupload"},
            ],
            [
                {"text": "❌ 取消當前操作", "callback_data": "ops:cancel"},
            ],
        ] + self._build_cross_bot_jump_rows())

    def _build_cross_bot_jump_rows(self) -> list:
        """v6.0.76:跨 bot 跳轉 — 主菜單底部一行 url 按鈕,點擊直接切換到其他 bot 對話。
        registry 還沒拉到 username 時返回空 list,不影響主菜單。
        """
        try:
            from core.tg_bot_registry import get_all_jump_buttons
            btns = get_all_jump_buttons(exclude_kind="ops")
            if btns:
                return [btns]  # 一整排
        except Exception:
            pass
        return []

    def _cancel_session(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, "已取消当前操作。")
        self._send_main_menu(chat_id)

    def _get_session(self, chat_id: str) -> Optional[UserSession]:
        sess = self._sessions.get(chat_id)
        if sess and time.time() - sess.created_at > SESSION_TIMEOUT:
            del self._sessions[chat_id]
            self._send(chat_id, "操作已超时，请重新开始。")
            return None
        return sess

    def _handle_session_input(self, chat_id: str, text: str) -> None:
        sess = self._get_session(chat_id)
        if not sess:
            return
        step_map = {
            SessionStep.DOC_WAIT_ACTION: self._doc_step_action,
            SessionStep.DOC_WAIT_ACCOUNT: self._doc_step_account,
            SessionStep.MERCARI_WAIT_INPUT: self._mercari_step_input,
            SessionStep.GOOFISH_WAIT_INPUT: self._goofish_step_input,
            SessionStep.PERF_WAIT_INPUT: self._perf_step_input,
            SessionStep.SYB_WAIT_CAPTCHA: self._syb_step_captcha,
            SessionStep.SYB_WAIT_CHECK_ORDERS: self._syb_check_step_input,
            SessionStep.SYB_WAIT_MON_ORDERS: self._syb_mon_step_input,
            SessionStep.SYB_WAIT_MON_REMOVE: self._syb_mon_step_remove,
            SessionStep.SYB_WAIT_MON_EDIT_SELECT: self._syb_mon_step_edit_select,
            SessionStep.SYB_WAIT_MON_EDIT_NEWNO: self._syb_mon_step_edit_newno,
            SessionStep.PQUERY_WAIT_CODE: self._pquery_step_input,
        }
        handler = step_map.get(sess.step)
        if handler:
            handler(chat_id, sess, text)

    # ---- 主菜单按钮回调 ----

    def _handle_ops_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        # v6.0.76:徹底換新 — 商品檢測統一為 "check",舊 "mercari"/"goofish" callback 也指向新入口
        # (向下相容舊訊息上殘留的按鈕,不影響新菜單)
        action_map = {
            "status":    self._cmd_status,
            "pay":       self._cmd_pay,
            "ship":      self._cmd_ship,
            "perf":      self._cmd_perf,
            "check":     self._cmd_check_unified,   # v6.0.76:統一商品檢測(新)
            "mercari":   self._cmd_check_unified,   # 兼容舊 callback,行為=新版
            "goofish":   self._cmd_check_unified,   # 兼容舊 callback,行為=新版
            "docs":      self._cmd_docs,
            "syblogin":  self._cmd_syb_login,
            "sybupload": self._cmd_syb_upload,
            "syblabels": self._cmd_syb_labels,
            "sybcheck":  self._cmd_syb_check,
            "sybmon":    self._cmd_syb_mon,
            "cancel":    self._cancel_session,
            "pquery":    self._cmd_pquery,
            "pupload":   self._cmd_pupload,
            "aichat":    self._cmd_ai_chat_list,    # v6.0.76:AI 客服列表
        }
        handler = action_map.get(action)
        if handler:
            handler(chat_id)
        elif action == "home":
            self._send_main_menu(chat_id)

    # ==================================================================
    # /status 系统状态总览
    # ==================================================================

    def _cmd_status(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        lines = ["📊【运营状态总览】"]

        mon_running = getattr(app, "monitoring", False)
        lines.append(f"Yahoo 监控: {'✅ 运行中' if mon_running else '⏹ 未启动'}")

        merch_running = getattr(app, "merch_running", False)
        lines.append(f"批量操作: {'🔄 运行中' if merch_running else '⏹ 未运行'}")

        # tab 名於 v5.x 改為 purchase_ship_tab,舊版兼容 purchase_tab
        purchase_tab = (getattr(app, "purchase_ship_tab", None)
                        or getattr(app, "purchase_tab", None))
        purchase_running = False
        if purchase_tab:
            pt = getattr(purchase_tab, "_thread", None)
            if pt and pt.is_alive():
                purchase_running = True
        lines.append(f"采购监控: {'✅ 运行中' if purchase_running else '⏹ 未启动'}")

        links = load_links()
        watching = [x for x in links if x.watch]
        lines.append(f"\n💰 代付绑定: {len(links)} 条 | 监控中: {len(watching)}")

        ship_tasks = getattr(app, "ship_tasks", [])
        pending_ship = [t for t in ship_tasks if not t.get("done")]
        lines.append(f"🚚 出货任务: {len(ship_tasks)} 条 | 待处理: {len(pending_ship)}")

        states = getattr(app, "states", {})
        if states:
            lines.append(f"\n👥 账号 ({len(states)}):")
            for pid, st in states.items():
                name = getattr(st, "name", "?")
                status = getattr(st, "status", "离线")
                paid = st.last_values.get("paid_to_ship", 0) if hasattr(st, "last_values") else 0
                cod = st.last_values.get("cod", 0) if hasattr(st, "last_values") else 0
                lines.append(f"  {name} | {status} | 待出货:{paid} 取货付款:{cod}")

        kb = self.tg.make_keyboard([
            [
                {"text": "💰 代付查询", "callback_data": "ops:pay"},
                {"text": "🚚 物流出货", "callback_data": "ops:ship"},
            ],
            [
                {"text": "📋 業績核對", "callback_data": "ops:perf"},
                {"text": "🔍 煤爐檢查", "callback_data": "ops:mercari"},
            ],
            [
                {"text": "🐟 鹹魚檢查", "callback_data": "ops:goofish"},
                {"text": "📄 文档管理", "callback_data": "ops:docs"},
            ],
            [
                {"text": "🔑 物流登录", "callback_data": "ops:syblogin"},
                {"text": "📤 上传资料", "callback_data": "ops:sybupload"},
            ],
            [
                {"text": "📦 检查发货", "callback_data": "ops:sybcheck"},
                {"text": "📡 发货监控", "callback_data": "ops:sybmon"},
            ],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ==================================================================
    # v6.0.76:AI 客服快捷對話列表(設計 A — 只看不操作)
    # 操作(reply/edit/skip)請使用 AI 客服 Bot
    # ==================================================================

    _AI_PHASE_LABELS = {
        "PENDING_AI": "🤔 AI 分析中",
        "PREVIEW_SENT": "📝 草稿待確認",
        "PREVIEW_SELLER_QUESTION": "❓ 賣家問題待確認",
        "AUTO_ASKING_SELLER": "📤 正在問賣家",
        "WAIT_SELLER": "⏳ 等賣家回覆",
        "PREVIEW_SELLER": "📋 整合稿待確認",
        "ERROR": "⚠️ 處理出錯",
    }

    def _cmd_ai_chat_list(self, chat_id: str) -> None:
        """列出當前所有 pending AI 對話 — 只看不操作。"""
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 應用未連接。")
            return

        # 取 ConversationManager(v4.7.x 為 _conv_mgr,舊版兼容)
        conv_mgr = (getattr(app, "_conv_mgr", None)
                    or getattr(app, "tg_conversation_mgr", None))
        if not conv_mgr:
            self._send(chat_id, "⚠️ AI 客服未啟用。")
            return

        # 列出 pending 對話
        try:
            from core.tg_conversation import ConvPhase
            pending_phases = {
                ConvPhase.PENDING_AI,
                ConvPhase.PREVIEW_SENT,
                ConvPhase.PREVIEW_SELLER_QUESTION,
                ConvPhase.AUTO_ASKING_SELLER,
                ConvPhase.WAIT_SELLER,
                ConvPhase.PREVIEW_SELLER,
                ConvPhase.ERROR,
            }
            with conv_mgr._lock:
                items = []
                for cid, conv in conv_mgr._convs.items():
                    if conv.phase in pending_phases:
                        items.append((cid, conv))
            # 按更新時間倒序(最新的在前)
            items.sort(key=lambda x: getattr(x[1], "updated_ts", 0), reverse=True)
        except Exception as e:
            self._send(chat_id, f"⚠️ 讀取對話失敗:{e}")
            return

        # 組裝訊息
        if not items:
            lines = [
                "💬 【AI 客服列表】",
                "",
                "✨ 暫無待處理對話",
                "",
                "所有買家訊息都已處理完。",
            ]
        else:
            lines = [f"💬 【AI 客服列表】 共 {len(items)} 條待處理\n"]
            now = time.time()
            for idx, (cid, conv) in enumerate(items[:15], 1):
                phase_str = conv.phase.value if hasattr(conv.phase, 'value') else str(conv.phase)
                phase_label = self._AI_PHASE_LABELS.get(phase_str, phase_str)

                acc = getattr(conv, "account_name", "?")
                buyer = getattr(conv, "buyer_label", "")[:20]
                title = (getattr(conv, "product_title", "") or "(未知商品)")[:30]

                age_sec = max(0, int(now - getattr(conv, "created_ts", now)))
                if age_sec < 60:
                    age = f"{age_sec}秒前"
                elif age_sec < 3600:
                    age = f"{age_sec // 60}分前"
                elif age_sec < 86400:
                    age = f"{age_sec // 3600}小時前"
                else:
                    age = f"{age_sec // 86400}天前"

                lines.append(f"[{idx}] {acc} {phase_label}")
                if buyer:
                    lines.append(f"    買家:{buyer}")
                lines.append(f"    商品:{title}")
                lines.append(f"    ⏱ {age}")

                # 階段相關附加資訊
                if conv.phase == ConvPhase.WAIT_SELLER:
                    q = (getattr(conv, "seller_sent_question", "") or "")[:40]
                    if q:
                        lines.append(f"    問卖家:{q}")
                elif conv.phase == ConvPhase.PREVIEW_SENT:
                    draft = (getattr(conv, "ai_draft", "") or "")[:50]
                    if draft:
                        lines.append(f"    草稿:{draft}")
                lines.append("")

            if len(items) > 15:
                lines.append(f"... 還有 {len(items) - 15} 條未顯示")
                lines.append("")

        lines.append("💡 reply/edit/skip 等操作請去 AI 客服 Bot")

        # 按鈕:刷新 + 跳到 AI 客服 bot(若 username 已 cached)+ 主菜單
        rows = [[{"text": "🔄 刷新", "callback_data": "ops:aichat"}]]
        try:
            from core.tg_bot_registry import get_jump_url
            ai_url = get_jump_url("ai_cs")
            if ai_url:
                rows.append([{"text": "→ 開啟 AI 客服 Bot", "url": ai_url}])
        except Exception:
            pass
        rows.append([{"text": "🏠 主菜單", "callback_data": "ops:home"}])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ==================================================================
    # /pay 代付情况查询
    # ==================================================================

    def _cmd_pay(self, chat_id: str) -> None:
        links = load_links()
        if not links:
            self._send(chat_id, "暂无采购绑定记录。")
            return

        by_status: Dict[str, List[PurchaseLink]] = {}
        for x in links:
            s = x.status or "待监控"
            by_status.setdefault(s, []).append(x)

        lines = ["💰【代付情况查询】"]
        lines.append(f"总计: {len(links)} 条绑定")

        for status, items in by_status.items():
            lines.append(f"\n📌 {status} ({len(items)}):")
            for x in items[:5]:
                plat = "闲鱼" if x.platform == "xianyu" else "煤炉"
                lines.append(
                    f"  {x.yahoo_acc_name} | {x.yahoo_order_no}\n"
                    f"    {plat} {x.purchase_order_id} | 金额:{x.pay_amount or '-'} | {x.pay_dt_raw or '-'}"
                )
            if len(items) > 5:
                lines.append(f"  ...还有 {len(items) - 5} 条")

        # 云端代付状态
        pay_tab = getattr(self.app, "pay_status_tab", None) if self.app else None
        if pay_tab:
            rows = getattr(pay_tab, "_rows", [])
            if rows:
                lines.append(f"\n☁️ 云端代付记录: {len(rows)} 条")
                for r in rows[:3]:
                    lines.append(
                        f"  {r.get('pay_date', '')} | {r.get('name', '')} | "
                        f"¥{r.get('amount', '')} | {r.get('code', '')}"
                    )
                if len(rows) > 3:
                    lines.append(f"  ...还有 {len(rows) - 3} 条")

        kb = self.tg.make_keyboard([
            [
                {"text": "🔄 刷新云端", "callback_data": "pay:refresh"},
                {"text": "📋 详细列表", "callback_data": "pay:list"},
            ],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_pay_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "refresh":
            self._pay_refresh(chat_id)
        elif action == "list":
            self._pay_detail_list(chat_id)

    def _pay_refresh(self, chat_id: str) -> None:
        pay_tab = getattr(self.app, "pay_status_tab", None) if self.app else None
        if not pay_tab:
            self._send(chat_id, "⚠️ 代付模块未加载。")
            return
        try:
            pay_tab.refresh()
            self._send(chat_id, "🔄 已发送刷新指令，请稍后再查询。")
            self.on_log("[TG-OPS] 远程刷新代付状态")
        except Exception as e:
            self._send(chat_id, f"刷新失败: {e}")

    def _pay_detail_list(self, chat_id: str) -> None:
        links = load_links()
        if not links:
            self._send(chat_id, "暂无采购绑定记录。")
            return
        lines = ["💰【代付详细列表】"]
        for i, x in enumerate(links):
            plat = "闲鱼" if x.platform == "xianyu" else "煤炉"
            watch = "👁" if x.watch else "  "
            lines.append(
                f"{watch} {i+1}. {x.yahoo_acc_name} | {x.yahoo_order_no}\n"
                f"     {plat} {x.purchase_order_id}\n"
                f"     金额:{x.pay_amount or '-'} | 物流:{x.tracking_no or '-'} | {x.status}"
            )
        lines.append(f"\n共 {len(links)} 条（👁=监控中）")
        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # /ship 物流/出货状态
    # ==================================================================

    def _cmd_ship(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        ship_tasks = getattr(app, "ship_tasks", [])
        lines = ["🚚【物流/出货状态】"]

        if not ship_tasks:
            lines.append("暂无出货任务。")
        else:
            pending = [t for t in ship_tasks if not t.get("done")]
            done = [t for t in ship_tasks if t.get("done")]
            lines.append(f"总计: {len(ship_tasks)} | 待处理: {len(pending)} | 已完成: {len(done)}")
            if pending:
                lines.append("\n📦 待处理:")
                for t in pending[:8]:
                    lines.append(
                        f"  {t.get('acc_name', '?')} | {t.get('order_no', '?')} | "
                        f"物流:{t.get('tracking_no', '-')}"
                    )
                if len(pending) > 8:
                    lines.append(f"  ...还有 {len(pending) - 8} 条")
            if done:
                lines.append("\n✅ 最近完成 (最新5条):")
                for t in done[-5:]:
                    lines.append(f"  {t.get('acc_name', '?')} | {t.get('order_no', '?')}")

        links = load_links()
        with_tracking = [x for x in links if (x.tracking_no or "").strip()]
        if with_tracking:
            lines.append(f"\n📦 采购已出货: {len(with_tracking)} 条")
            for x in with_tracking[:5]:
                plat = "闲鱼" if x.platform == "xianyu" else "煤炉"
                lines.append(f"  {x.yahoo_acc_name} | {x.yahoo_order_no} | {plat} | 物流:{x.tracking_no}")

        syb_tab = getattr(app, "syb_upload_tab", None)
        if syb_tab:
            mon_items = getattr(syb_tab, "_mon_items", {})
            active = [it for it in mon_items.values() if it.active]
            if active:
                lines.append(f"\n📡 顺运宝监控: {len(active)} 条活跃")

        kb = self.tg.make_keyboard([
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_ship_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "tracking":
            self._ship_tracking(chat_id)

    def _ship_tracking(self, chat_id: str) -> None:
        links = load_links()
        watching = [x for x in links if x.watch]
        if not watching:
            self._send(chat_id, "当前没有监控中的采购绑定。")
            return
        lines = ["📦【采购物流追踪】"]
        for x in watching:
            plat = "闲鱼" if x.platform == "xianyu" else "煤炉"
            lines.append(
                f"  {x.yahoo_acc_name} | {x.yahoo_order_no}\n"
                f"    {plat} {x.purchase_order_id} | {x.status} | 物流:{x.tracking_no or '-'}"
            )
        lines.append(f"\n共 {len(watching)} 条监控中")
        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # /perf 業績核對
    # ==================================================================

    def _cmd_perf(self, chat_id: str) -> None:
        app = self.app
        perf_tab = getattr(app, "perf_check_tab", None) if app else None
        lines = ["📋【業績核對】"]

        if not perf_tab:
            lines.append("⚠️ 業績核對模块未加载。")
            self._send(chat_id, "\n".join(lines))
            return

        worker = getattr(perf_tab, "_worker_thread", None)
        running = worker is not None and worker.is_alive()
        lines.append(f"状态: {'🔄 运行中' if running else '⏹ 未运行'}")
        results = getattr(perf_tab, "_results", [])
        lines.append(f"已有结果: {len(results)} 条")
        lines.append("\n点击「输入订单号」可查询")
        lines.append("上传 Excel 文件可批量核对")

        kb = self.tg.make_keyboard([
            [
                {"text": "📝 输入订单号", "callback_data": "perf:input"},
                {"text": "📊 查看结果", "callback_data": "perf:results"},
            ],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_perf_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "input":
            self._perf_ask_input(chat_id)
        elif action == "results":
            self._perf_show_results(chat_id)

    def _perf_ask_input(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.PERF_WAIT_INPUT,
                created_at=time.time(),
            )
        self._send(chat_id, "请输入要核对的订单号（每行一个，最多20个）：\n发送 /cancel 取消")

    def _perf_step_input(self, chat_id: str, sess: UserSession, text: str) -> None:
        order_nos = [x.strip() for x in text.splitlines() if x.strip()][:20]
        if not order_nos:
            self._send(chat_id, "未检测到有效订单号，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, f"⏳ 正在查询 {len(order_nos)} 个订单...")
        self.on_log(f"[TG-OPS] 業績核對查询: {len(order_nos)} 个订单")
        threading.Thread(
            target=self._perf_query_orders,
            args=(chat_id, order_nos),
            daemon=True,
        ).start()

    def _perf_query_orders(self, chat_id: str, order_nos: List[str]) -> None:
        links = load_links()
        lines = [f"📋【業績核對结果】共 {len(order_nos)} 个订单"]
        found = 0
        for ono in order_nos:
            matched = [x for x in links if x.yahoo_order_no == ono]
            if matched:
                found += 1
                x = matched[0]
                plat = "闲鱼" if x.platform == "xianyu" else "煤炉"
                lines.append(
                    f"\n✅ {ono}\n"
                    f"  账号:{x.yahoo_acc_name} | {plat} {x.purchase_order_id}\n"
                    f"  金额:{x.pay_amount or '-'} | 物流:{x.tracking_no or '-'} | {x.status}"
                )
            else:
                lines.append(f"\n❌ {ono} - 未找到绑定记录")
        lines.append(f"\n匹配: {found}/{len(order_nos)}")
        self._send(chat_id, "\n".join(lines))

    def _perf_show_results(self, chat_id: str) -> None:
        perf_tab = getattr(self.app, "perf_check_tab", None) if self.app else None
        if not perf_tab:
            self._send(chat_id, "⚠️ 業績核對模块未加载。")
            return
        results = getattr(perf_tab, "_results", [])
        if not results:
            self._send(chat_id, "暂无核对结果。")
            return
        lines = [f"📊【核对结果】共 {len(results)} 条"]
        for r in results[-10:]:
            ok_str = "✅" if r.get("ok") else "❌"
            lines.append(f"  {ok_str} {r.get('order_no', '?')} | ¥{r.get('amount', '?')} | {r.get('badge', '')}")
        if len(results) > 10:
            lines.append(f"  ...共 {len(results)} 条，仅显示最新10条")
        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # v6.0.76:統一商品檢測(替代舊版獨立的 mercari/goofish check)
    # 適配:閒魚+煤爐已合併為 unified_check_tab,TG 一鍵啟動雲端檢測
    # ==================================================================

    def _cmd_check_unified(self, chat_id: str) -> None:
        """統一檢測入口頁 — 雲端拉資料 → 兩平台檢測 → 自動下架/D1 清理。"""
        app = self.app
        tab = getattr(app, "unified_check_tab", None) if app else None
        if not tab:
            self._send(chat_id, "⚠️ 統一檢測模組未加載,請更新到桌面端 v5.x+")
            return

        # 狀態
        running = bool(getattr(tab, "_worker", None)
                       and tab._worker.is_alive())
        phase = ""
        try:
            phase = tab.var_phase.get() or ""
        except Exception:
            pass

        lines = ["🛒 【商品狀態檢測】"]
        if running:
            lines.append(f"🔄 正在執行:{phase}")
            try:
                gf_p = tab.var_gf_progress.get() or ""
                mc_p = tab.var_mc_progress.get() or ""
                if gf_p:
                    lines.append(f"  閒魚:{gf_p}")
                if mc_p:
                    lines.append(f"  煤爐:{mc_p}")
            except Exception:
                pass
        else:
            lines.append(f"💤 當前狀態:{phase or '就緒'}")

        lines.append("")
        lines.append("從雲端 D1 拉取你的商品清單,")
        lines.append("自動檢測閒魚/煤爐是否仍在售,")
        lines.append("非在售自動 Yahoo 下架 + D1 清理。")
        lines.append("")
        lines.append("選擇要檢測的平台:")

        rows = []
        if not running:
            rows.append([
                {"text": "☁️ 全部檢測(閒魚+煤爐)",
                 "callback_data": "check:start:both"},
            ])
            rows.append([
                {"text": "🐟 只檢閒魚",
                 "callback_data": "check:start:goofish"},
                {"text": "🔍 只檢煤爐",
                 "callback_data": "check:start:mercari"},
            ])
        else:
            rows.append([
                {"text": "🛑 停止當前檢測",
                 "callback_data": "check:stop"},
                {"text": "🔄 刷新進度",
                 "callback_data": "ops:check"},
            ])
        rows.append([
            {"text": "📊 查看最新結果", "callback_data": "check:view"},
        ])
        rows.append([
            {"text": "🏠 主菜單", "callback_data": "ops:home"},
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=self.tg.make_keyboard(rows))

    def _handle_check_cb(self, chat_id: str, parts: List[str]) -> None:
        """check:start:both/goofish/mercari · check:stop · check:view"""
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if action == "start":
            platforms = {"both": "goofish,mercari",
                         "goofish": "goofish",
                         "mercari": "mercari"}.get(arg, "goofish,mercari")
            threading.Thread(
                target=self._unified_check_worker,
                args=(chat_id, platforms),
                daemon=True,
            ).start()
            self._send(chat_id, "⏳ 統一檢測啟動中,稍後會推送進度...")
        elif action == "stop":
            self._stop_unified_check(chat_id)
        elif action == "view":
            self._view_unified_result(chat_id)

    def _unified_check_worker(self, chat_id: str, platforms: str) -> None:
        """後台 worker:設置 → 拉資料 → 啟動 _pipeline_worker → 監控進度。"""
        app = self.app
        tab = getattr(app, "unified_check_tab", None) if app else None
        if not tab:
            self._send(chat_id, "⚠️ 統一檢測模組未加載")
            return

        # 防重入
        if getattr(tab, "_worker", None) and tab._worker.is_alive():
            self._send(chat_id, "⚠️ 統一檢測已在運行中,請等待完成或先停止")
            return

        # 設置平台 + 雲端模式
        try:
            tab._ui(lambda: tab.var_source.set("cloud"))
            tab._ui(lambda: tab.var_check_gf.set("goofish" in platforms))
            tab._ui(lambda: tab.var_check_mc.set("mercari" in platforms))
        except Exception as e:
            self._send(chat_id, f"⚠️ 設置平台失敗:{e}")
            return

        # 取 owner
        try:
            owner = (tab.var_owner.get() or "").strip()
        except Exception:
            owner = ""
        if not owner:
            self._send(chat_id, "⚠️ 桌面端未配置 KV 中轉 TG ID")
            return

        # 拉雲端資料
        self._send(chat_id, f"⏳ 正在從雲端拉取商品清單(owner={owner})...")
        try:
            tab._pull_worker(owner)
        except Exception as e:
            self._send(chat_id, f"⚠️ 雲端拉取失敗:{e}")
            return

        records = list(getattr(tab, "_records", None) or [])
        if not records:
            self._send(chat_id, "⚠️ 雲端無待檢資料")
            return

        # 過濾後預估(只看選了的平台)
        gf_count = sum(1 for r in records
                       if not (r.get("barcode") or "").strip().startswith("http"))
        mc_count = sum(1 for r in records
                       if (r.get("barcode") or "").strip().startswith("http"))

        info_lines = [f"📦 雲端資料 {len(records)} 條"]
        if "goofish" in platforms:
            info_lines.append(f"  🐟 閒魚:{gf_count}")
        if "mercari" in platforms:
            info_lines.append(f"  🔍 煤爐:{mc_count}")
        self._send(chat_id, "\n".join(info_lines))

        # 啟動 pipeline(繞開 tab.start() 內部 messagebox 邏輯)
        try:
            tab._stop_evt.clear()
            tab._set_btn_state(True)
            tab._set_phase("准备中")
            tab._set_gf_progress("")
            tab._set_mc_progress("")
            tab._set_delist("")
            tab._worker = threading.Thread(
                target=tab._pipeline_worker, args=(records,), daemon=True,
            )
            tab._worker.start()
            self._send(chat_id, f"▶️ 統一檢測已啟動")
            self.on_log(f"[TG-OPS] 統一檢測啟動:{platforms} / {len(records)} 條")
        except Exception as e:
            self._send(chat_id, f"⚠️ 啟動失敗:{e}")
            return

        # 進度監控
        self._monitor_unified_progress(chat_id, tab, platforms)

    def _monitor_unified_progress(self, chat_id: str, tab, platforms: str) -> None:
        """阻塞輪詢 — worker 運行期間每變化/15s 推送一次。"""
        last_phase = ""
        last_gf = ""
        last_mc = ""
        last_del = ""
        last_ping = 0.0
        PING_INTERVAL = 15.0

        # 等 worker 啟動(最多 5 秒)
        wait_start = time.time()
        while time.time() - wait_start < 5:
            if tab._worker and tab._worker.is_alive():
                break
            time.sleep(0.3)

        while True:
            is_alive = bool(tab._worker and tab._worker.is_alive())
            try:
                phase = tab.var_phase.get() or ""
                gf = tab.var_gf_progress.get() or ""
                mc = tab.var_mc_progress.get() or ""
                de = tab.var_delist_status.get() or ""
            except Exception:
                phase = gf = mc = de = ""

            changed = (phase != last_phase) or (gf != last_gf) or (mc != last_mc) or (de != last_del)
            now = time.time()
            should_push = changed or (now - last_ping > PING_INTERVAL)

            if should_push and (phase or gf or mc):
                msg_lines = ["📊 統一檢測進度"]
                if phase:
                    msg_lines.append(f"狀態:{phase}")
                if "goofish" in platforms and gf:
                    msg_lines.append(f"閒魚:{gf}")
                if "mercari" in platforms and mc:
                    msg_lines.append(f"煤爐:{mc}")
                if de:
                    msg_lines.append(f"下架:{de}")
                self._send(chat_id, "\n".join(msg_lines))
                last_phase, last_gf, last_mc, last_del = phase, gf, mc, de
                last_ping = now

            if not is_alive:
                final_msg = ["✅ 統一檢測結束"]
                if phase:
                    final_msg.append(f"最終狀態:{phase}")
                if de:
                    final_msg.append(f"下架結果:{de}")
                self._send(chat_id, "\n".join(final_msg))
                self.on_log(f"[TG-OPS] 統一檢測結束:{phase}")
                break

            time.sleep(3)

    def _stop_unified_check(self, chat_id: str) -> None:
        """通知 worker 停止。"""
        app = self.app
        tab = getattr(app, "unified_check_tab", None) if app else None
        if not tab:
            self._send(chat_id, "⚠️ 統一檢測模組未加載")
            return
        if not (tab._worker and tab._worker.is_alive()):
            self._send(chat_id, "ℹ️ 沒有運行中的檢測任務")
            return
        try:
            tab._stop_evt.set()
            tab._set_phase("正在停止…")
            self._send(chat_id, "🛑 已發送停止信號,worker 會在當前 batch 結束後停止")
        except Exception as e:
            self._send(chat_id, f"⚠️ 停止失敗:{e}")

    def _view_unified_result(self, chat_id: str) -> None:
        """查看最新檢測結果摘要(讀 output 目錄下的 Excel)。"""
        app = self.app
        tab = getattr(app, "unified_check_tab", None) if app else None
        if not tab:
            self._send(chat_id, "⚠️ 統一檢測模組未加載")
            return

        # 嘗試從 tab 取最後狀態
        try:
            phase = tab.var_phase.get() or "未知"
            gf = tab.var_gf_progress.get() or ""
            mc = tab.var_mc_progress.get() or ""
            de = tab.var_delist_status.get() or ""
        except Exception:
            phase = "未知"
            gf = mc = de = ""

        lines = ["📊 【最新檢測狀態】"]
        lines.append(f"狀態:{phase}")
        if gf:
            lines.append(f"🐟 閒魚:{gf}")
        if mc:
            lines.append(f"🔍 煤爐:{mc}")
        if de:
            lines.append(f"📦 自動下架:{de}")
        lines.append("")
        lines.append("💡 詳細結果請看桌面端「商品檢查」分頁,")
        lines.append("   或 output/ 資料夾下的 Excel/txt")

        kb = self.tg.make_keyboard([
            [{"text": "↩️ 回檢測入口", "callback_data": "ops:check"}],
            [{"text": "🏠 主菜單", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ==================================================================
    # 舊版 /mercari 煤爐檢查 入口(v6.0.76:統一導向 _cmd_check_unified)
    # ==================================================================

    def _cmd_mercari(self, chat_id: str) -> None:
        """v6.0.76:導向統一檢測入口(舊版已合併)。"""
        return self._cmd_check_unified(chat_id)

    def _cmd_mercari_legacy_disabled(self, chat_id: str) -> None:
        """v6.0.x 之前的舊版邏輯,已停用。保留代碼供參考。"""
        app = self.app
        merc_tab = getattr(app, "mercari_check_tab", None) if app else None
        lines = ["🔍【煤爐檢查】"]

        if not merc_tab:
            lines.append("⚠️ 煤爐檢查模块未加载。")
            self._send(chat_id, "\n".join(lines))
            return

        lines.append("输入煤炉商品 URL 可检查状态")
        lines.append("支持批量检查（每行一个 URL）")
        lines.append("\n也可以从云端数据库拉取你的煤炉条码批量检测")

        kb = self.tg.make_keyboard([
            [{"text": "📝 输入URL检查", "callback_data": "merc:input"}],
            [{"text": "☁️ 从云端数据检测", "callback_data": "merc:cloud"}],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_mercari_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "input":
            self._mercari_ask_input(chat_id)
        elif action == "cloud":
            threading.Thread(target=self._cloud_check, args=(chat_id, "mercari"), daemon=True).start()
        elif action == "cloudstart":
            threading.Thread(target=self._cloud_start_check, args=(chat_id, "mercari"), daemon=True).start()
        elif action == "dlall":
            threading.Thread(target=self._cloud_download, args=(chat_id, "mercari"), daemon=True).start()
        elif action == "recheck_unknown":
            threading.Thread(target=self._cloud_recheck_unknown, args=(chat_id, "mercari"), daemon=True).start()
        elif action == "idops":
            self._start_merch_id_ops(chat_id)
        elif action == "idops_confirm":
            self._confirm_merch_id_ops(chat_id)

    def _mercari_ask_input(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.MERCARI_WAIT_INPUT,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "请输入煤炉商品 URL（每行一个，最多20个）：\n"
            "格式: https://jp.mercari.com/item/mXXXXX\n"
            "发送 /cancel 取消",
        )

    def _mercari_step_input(self, chat_id: str, sess: UserSession, text: str) -> None:
        urls = [x.strip() for x in text.splitlines() if x.strip()][:20]
        if not urls:
            self._send(chat_id, "未检测到有效 URL，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, f"⏳ 正在检查 {len(urls)} 个煤炉商品...")
        self.on_log(f"[TG-OPS] 煤爐檢查: {len(urls)} 个")
        threading.Thread(
            target=self._mercari_check_urls, args=(chat_id, urls), daemon=True,
        ).start()

    def _mercari_check_urls(self, chat_id: str, urls: List[str]) -> None:
        """后台线程：逐个检查煤炉商品 URL 状态。"""
        import asyncio
        merc_tab = getattr(self.app, "mercari_check_tab", None) if self.app else None
        if not merc_tab:
            self._send(chat_id, "⚠️ 煤爐檢查模块未加载。")
            return

        results: List[str] = []
        for url in urls:
            url = url.strip()
            if not url:
                continue
            try:
                # 尝试使用 mercari_check_tab 的检查方法
                checker = getattr(merc_tab, "_check_single_url", None)
                if checker:
                    status = checker(url)
                else:
                    status = "模块无检查接口"
            except Exception as e:
                status = f"检查失败: {e}"
            results.append(f"  {url}\n    → {status}")

        lines = [f"🔍【煤爐檢查结果】共 {len(urls)} 个"]
        lines.extend(results)
        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # /goofish 鹹魚檢查
    # ==================================================================

    def _cmd_goofish(self, chat_id: str) -> None:
        """v6.0.76:導向統一檢測入口(舊版已合併)。"""
        return self._cmd_check_unified(chat_id)

    def _cmd_goofish_legacy_disabled(self, chat_id: str) -> None:
        """v6.0.x 之前的舊版邏輯,已停用。保留代碼供參考。"""
        app = self.app
        goof_tab = getattr(app, "goofish_check_tab", None) if app else None
        lines = ["🐟【鹹魚檢查】"]

        if not goof_tab:
            lines.append("⚠️ 鹹魚檢查模块未加载。")
            self._send(chat_id, "\n".join(lines))
            return

        lines.append("输入闲鱼商品 ID 可检查状态")
        lines.append("支持批量检查（每行一个 ID）")
        lines.append("\n也可以从云端数据库拉取你的闲鱼条码批量检测")

        kb = self.tg.make_keyboard([
            [{"text": "📝 输入ID检查", "callback_data": "goof:input"}],
            [{"text": "☁️ 从云端数据检测", "callback_data": "goof:cloud"}],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_goofish_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "input":
            self._goofish_ask_input(chat_id)
        elif action == "cloud":
            threading.Thread(target=self._cloud_check, args=(chat_id, "goofish"), daemon=True).start()
        elif action == "cloudstart":
            threading.Thread(target=self._cloud_start_check, args=(chat_id, "goofish"), daemon=True).start()
        elif action == "dlall":
            threading.Thread(target=self._cloud_download, args=(chat_id, "goofish"), daemon=True).start()
        elif action == "recheck_unknown":
            threading.Thread(target=self._cloud_recheck_unknown_goofish, args=(chat_id,), daemon=True).start()
        elif action == "idops":
            self._start_merch_id_ops(chat_id)
        elif action == "idops_confirm":
            self._confirm_merch_id_ops(chat_id)

    def _goofish_ask_input(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.GOOFISH_WAIT_INPUT,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "请输入闲鱼商品 ID（每行一个，最多20个）：\n"
            "发送 /cancel 取消",
        )

    def _goofish_step_input(self, chat_id: str, sess: UserSession, text: str) -> None:
        item_ids = [x.strip() for x in text.splitlines() if x.strip()][:20]
        if not item_ids:
            self._send(chat_id, "未检测到有效 ID，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, f"⏳ 正在检查 {len(item_ids)} 个闲鱼商品...")
        self.on_log(f"[TG-OPS] 鹹魚檢查: {len(item_ids)} 个")
        threading.Thread(
            target=self._goofish_check_items,
            args=(chat_id, item_ids),
            daemon=True,
        ).start()

    def _goofish_check_items(self, chat_id: str, item_ids: List[str]) -> None:
        """后台线程：逐个检查闲鱼商品状态。"""
        goof_tab = getattr(self.app, "goofish_check_tab", None) if self.app else None
        if not goof_tab:
            self._send(chat_id, "⚠️ 鹹魚檢查模块未加载。")
            return

        results: List[str] = []
        for item_id in item_ids:
            item_id = item_id.strip()
            if not item_id:
                continue
            try:
                checker = getattr(goof_tab, "_check_single_item", None)
                if checker:
                    status = checker(item_id)
                else:
                    status = "模块无检查接口"
            except Exception as e:
                status = f"检查失败: {e}"
            results.append(f"  {item_id} → {status}")

        lines = [f"🐟【鹹魚檢查结果】共 {len(item_ids)} 个"]
        lines.extend(results)
        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # /docs 文档管理
    # ==================================================================

    def _cmd_docs(self, chat_id: str) -> None:
        ids_dir = ROOT_DIR / "ids"
        pub_dir = ROOT_DIR / "publish_excels"

        lines = ["📄【文档管理】"]

        # ids 目录
        if ids_dir.exists():
            txt_files = sorted(ids_dir.glob("*.txt"))
            lines.append(f"\n📦 ids/ 目录: {len(txt_files)} 个文件")
            for f in txt_files[:8]:
                lines.append(f"  {f.name}")
            if len(txt_files) > 8:
                lines.append(f"  ...还有 {len(txt_files) - 8} 个")
        else:
            lines.append("\n📦 ids/ 目录: 不存在")

        # publish_excels 目录
        if pub_dir.exists():
            xlsx_files = sorted([
                p for p in pub_dir.glob("*.xlsx")
                if not p.name.startswith("~$")
            ])
            lines.append(f"\n📝 publish_excels/ 目录: {len(xlsx_files)} 个文件")
            for f in xlsx_files[:8]:
                lines.append(f"  {f.name}")
            if len(xlsx_files) > 8:
                lines.append(f"  ...还有 {len(xlsx_files) - 8} 个")
        else:
            lines.append("\n📝 publish_excels/ 目录: 不存在")

        lines.append("\n直接发送文件到此 Bot：")
        lines.append("  .txt → 商品编号（保存到 ids/）")
        lines.append("  .xlsx → 刊登Excel（保存到 publish_excels/）")

        kb = self.tg.make_keyboard([
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_doc_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        sess = self._get_session(chat_id)
        if not sess:
            return
        if sess.step == SessionStep.DOC_WAIT_ACTION and action == "ids":
            self._doc_ask_account(chat_id, sess)
        elif sess.step == SessionStep.DOC_WAIT_ACCOUNT and action.startswith("acc_"):
            self._doc_select_account(chat_id, sess, action)

    # ==================================================================
    # 文档上传处理
    # ==================================================================

    def _handle_uploaded_doc(self, chat_id: str, file_path: str,
                             file_name: str, caption: str) -> None:
        ext = Path(file_name).suffix.lower()

        if ext == ".xlsx":
            dest_dir = ROOT_DIR / "publish_excels"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file_name
            try:
                import shutil
                shutil.copy2(file_path, str(dest))
                self._send(
                    chat_id,
                    f"📄 Excel 已保存到 publish_excels/\n"
                    f"文件：{file_name}",
                )
                self.on_log(f"[TG-OPS] 文档保存: {file_name} → publish_excels/")
            except Exception as e:
                self._send(chat_id, f"文件保存失败: {e}")
            return

        if ext == ".txt":
            with self._session_lock:
                self._sessions[chat_id] = UserSession(
                    step=SessionStep.DOC_WAIT_ACTION,
                    doc_path=file_path,
                    doc_name=file_name,
                    created_at=time.time(),
                )
            kb = self.tg.make_keyboard([
                [{"text": "📦 商品编号(ids/)", "callback_data": "doc:ids"}],
                [{"text": "❌ 取消", "callback_data": "ops:cancel"}],
            ])
            self._send(
                chat_id,
                f"📄 收到文件：{file_name}\n请选择用途：",
                reply_markup=kb,
            )
            return

        if ext == ".pdf":
            dest_dir = ROOT_DIR / "tg_uploads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file_name
            try:
                import shutil
                shutil.copy2(file_path, str(dest))
                self._send(
                    chat_id,
                    f"📋 面单 PDF 已保存到 tg_uploads/\n"
                    f"文件：{file_name}\n"
                    f"发送 /syblabels 可批量上传到物流系统",
                )
                self.on_log(
                    f"[TG-OPS] PDF保存: {file_name} → tg_uploads/")
            except Exception as e:
                self._send(chat_id, f"文件保存失败: {e}")
            return

        self._send(
            chat_id,
            f"📄 收到文件：{file_name}\n"
            f"支持的格式：.txt .xlsx .pdf",
        )

    def _doc_step_action(self, chat_id: str, sess: UserSession,
                         text: str) -> None:
        t = text.strip().lower()
        if t in ("ids", "编号", "1"):
            self._doc_ask_account(chat_id, sess)
        else:
            self._send(chat_id, "请点击按钮选择用途，或发送 /cancel 取消。")

    def _doc_ask_account(self, chat_id: str, sess: UserSession) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        states = getattr(app, "states", {})
        if not states:
            self._send(chat_id, "⚠️ 暂无账号，请先在 GUI 添加账号。")
            return
        rows = []
        for pid, st in states.items():
            name = getattr(st, "name", "?")
            rows.append([{
                "text": name,
                "callback_data": f"doc:acc_{pid}",
            }])
        rows.append([{"text": "❌ 取消", "callback_data": "ops:cancel"}])
        kb = self.tg.make_keyboard(rows)
        sess.step = SessionStep.DOC_WAIT_ACCOUNT
        self._send(
            chat_id,
            "请选择此文件对应的账号：\n"
            "（文件将按账号名保存到 ids/ 目录）",
            reply_markup=kb,
        )

    def _doc_select_account(self, chat_id: str,
                            sess: UserSession, action: str) -> None:
        pid = action[4:]  # 去掉 "acc_" 前缀
        app = self.app
        if not app:
            return
        states = getattr(app, "states", {})
        st = states.get(pid)
        if not st:
            self._send(chat_id, "⚠️ 账号不存在，请重新选择。")
            return
        sess.doc_target_account = getattr(st, "name", pid)
        sess.doc_target_pid = pid
        self._doc_save_to_ids(chat_id, sess)

    def _doc_step_account(self, chat_id: str,
                          sess: UserSession, text: str) -> None:
        app = self.app
        if not app:
            return
        states = getattr(app, "states", {})
        for pid, st in states.items():
            name = getattr(st, "name", "")
            if name == text.strip() or pid == text.strip():
                sess.doc_target_account = name
                sess.doc_target_pid = pid
                self._doc_save_to_ids(chat_id, sess)
                return
        self._send(chat_id, "未找到该账号，请点击按钮选择。")

    def _doc_save_to_ids(self, chat_id: str, sess: UserSession) -> None:
        ids_dir = ROOT_DIR / "ids"
        ids_dir.mkdir(parents=True, exist_ok=True)

        src = Path(sess.doc_path)
        target_name = sess.doc_target_account or sess.doc_target_pid
        if not target_name:
            target_name = Path(sess.doc_name).stem
        save_name = f"{target_name}.txt"
        dest = ids_dir / save_name

        with self._session_lock:
            self._sessions.pop(chat_id, None)

        if not src.exists():
            self._send(chat_id, "⚠️ 文件已过期，请重新上传。")
            return

        try:
            raw = src.read_text(encoding="utf-8", errors="replace")
            cleaned = self._clean_ids_content(raw)
            line_count = len([ln for ln in cleaned.splitlines() if ln.strip()])

            ai_used = False
            if line_count == 0 and len(raw.strip()) > 10:
                self._send(chat_id, "⏳ 格式异常，正在使用 AI 辅助提取编号...")
                ai_result = self._ai_clean_ids_content(raw, chat_id)
                if ai_result and ai_result.strip():
                    cleaned = ai_result
                    line_count = len([ln for ln in cleaned.splitlines() if ln.strip()])
                    ai_used = True

            dest.write_text(cleaned, encoding="utf-8")
            ai_tag = "（AI辅助提取）" if ai_used else ""
            self._send(
                chat_id,
                f"✅ 已保存到 ids/{save_name}{ai_tag}\n"
                f"对应账号：{sess.doc_target_account or '未指定'}\n"
                f"有效编号行数：{line_count}",
            )
            self.on_log(
                f"[TG-OPS] 文档保存: {sess.doc_name} → "
                f"ids/{save_name} ({line_count} 行)"
            )
        except Exception as e:
            self._send(chat_id, f"文件处理失败: {e}")

    @staticmethod
    def _clean_ids_content(raw: str) -> str:
        lines = raw.splitlines()
        cleaned = []
        for line in lines:
            line = re.sub(
                r'[\ufffc\ufeff\u200b\u200c\u200d\u2060\u00a0]', '', line
            )
            line = line.strip()
            if not line:
                continue
            parts = re.split(r'[,，\t\s]+', line)
            for p in parts:
                p = p.strip()
                if p:
                    cleaned.append(p)
        return "\n".join(cleaned) + "\n" if cleaned else ""

    def _ai_clean_ids_content(self, raw: str, chat_id: str) -> Optional[str]:
        if not _HARDCODED_API_KEY:
            return None

        system_prompt = (
            "你是一个数据清洗助手。用户会发送一段文本，里面包含商品编号（ID）。\n"
            "请从文本中提取所有商品编号，每行一个，去除重复。\n"
            "只输出编号，不要输出任何其他文字、解释或标记。\n"
            "如果文本中没有可识别的编号，输出空。"
        )
        user_prompt = f"请从以下文本中提取商品编号：\n\n{raw[:3000]}"

        try:
            ok, result = call_openai(
                api_key=_HARDCODED_API_KEY,
                base_url=_HARDCODED_BASE_URL,
                endpoint_mode=_HARDCODED_ENDPOINT,
                model=_HARDCODED_MODEL,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_sec=30,
            )
            if ok and result.strip():
                self.on_log("[TG-OPS] AI 辅助清理编号文本成功")
                return self._clean_ids_content(result)
            return None
        except Exception as e:
            self.on_log(f"[TG-OPS] AI 清理失败: {e}")
            return None

    # ==================================================================
    # /syblogin 物流系统（顺运宝）TG 登录
    # ==================================================================

    def _cmd_syb_login(self, chat_id: str) -> None:
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        self._send(chat_id, "⏳ 正在获取验证码，请稍候...")
        self.on_log("[TG-OPS] SYB 远程登录：获取验证码")
        threading.Thread(
            target=self._syb_get_captcha_and_send,
            args=(chat_id, syb_tab),
            daemon=True,
        ).start()

    def _syb_get_captcha_and_send(self, chat_id: str, syb_tab) -> None:
        """后台线程：通过 SYBWebAgent 获取验证码截图，发送到 TG。"""
        done_event = threading.Event()
        captcha_ok = [False]

        def task(page):
            try:
                from core.shunyunbao_upload_feature import (
                    _syb_try_goto_login, _syb_fill_credentials,
                    _syb_capture_captcha_png, SYB_WAIT_SELECTOR_MS,
                )
                _syb_try_goto_login(page, self.on_log)
                _syb_fill_credentials(page)
                try:
                    page.wait_for_selector(
                        'img[src*="pcode"]',
                        timeout=SYB_WAIT_SELECTOR_MS,
                    )
                except Exception:
                    pass
                ok = _syb_capture_captcha_png(
                    page, syb_tab._captcha_path, self.on_log,
                )
                if ok:
                    import time as _t
                    syb_tab._captcha_ts = _t.time()
                captcha_ok[0] = ok
            except Exception as e:
                self.on_log(f"[TG-OPS] SYB 获取验证码失败: {e}")
            finally:
                done_event.set()

        syb_tab._ensure_agent()
        syb_tab._agent.submit(task, "TG远程获取验证码")
        done_event.wait(timeout=60)

        if not captcha_ok[0]:
            self._send(chat_id, "❌ 获取验证码失败，请稍后重试。")
            return

        # 发送验证码图片到 TG
        captcha_path = str(syb_tab._captcha_path)
        kb = self.tg.make_keyboard([
            [{"text": "🔄 刷新验证码", "callback_data": "syb:refresh"}],
            [{"text": "❌ 取消", "callback_data": "ops:cancel"}],
        ])
        self.tg.send_photo(
            chat_id, captcha_path,
            caption="请输入验证码（直接回复文字）：",
            reply_markup=kb,
        )

        # 进入等待验证码输入的会话
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_CAPTCHA,
                created_at=time.time(),
            )
        self.on_log("[TG-OPS] SYB 验证码已发送，等待用户输入")

    def _syb_step_captcha(self, chat_id: str, sess: UserSession,
                          text: str) -> None:
        """用户输入了验证码文字，执行登录。"""
        code = text.strip()
        if not code:
            self._send(chat_id, "请输入验证码内容：")
            return

        with self._session_lock:
            self._sessions.pop(chat_id, None)

        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        self._send(chat_id, f"⏳ 正在使用验证码 [{code}] 登录...")
        self.on_log(f"[TG-OPS] SYB 远程登录：提交验证码")
        threading.Thread(
            target=self._syb_do_login,
            args=(chat_id, syb_tab, code),
            daemon=True,
        ).start()

    def _syb_do_login(self, chat_id: str, syb_tab, code: str) -> None:
        """后台线程：填写验证码并点击登录。"""
        done_event = threading.Event()
        login_result = [None]  # None=未完成, True=成功, str=失败原因

        def task(page):
            try:
                from core.shunyunbao_upload_feature import (
                    _syb_is_login_page, _syb_fill_credentials,
                    _syb_collect_login_error, _syb_capture_captcha_png,
                    _syb_locate_captcha_img,
                    SYB_IMPORT_URL, SYB_GOTO_TIMEOUT_MS,
                )
                in_login = _syb_is_login_page(page)
                if not in_login:
                    login_result[0] = True
                    return

                _syb_fill_credentials(page)
                # 填写验证码
                cap_input = page.locator(
                    "input[placeholder*='验证码'], "
                    "input[placeholder*='验证'], "
                    "input[aria-label*='验证码']"
                ).first
                cap_input.fill(code, timeout=3000)

                # 点击登录按钮
                btn = page.locator("button:has-text('登录')").first
                btn.click(timeout=8000)
                page.wait_for_timeout(1500)

                # 检查错误
                err = _syb_collect_login_error(page)
                if err:
                    fatal = [
                        "图片验证码不正确", "验证码不正确",
                        "验证码错误", "账号或密码",
                        "用户名或密码", "密码错误", "用户不存在",
                    ]
                    if any(k in err for k in fatal):
                        login_result[0] = f"登录失败: {err}"
                        self._syb_refresh_captcha_for_tg(
                            chat_id, syb_tab, page,
                        )
                        return

                # 验证：访问导入页确认登录成功
                page.goto(
                    SYB_IMPORT_URL,
                    wait_until="commit",
                    timeout=SYB_GOTO_TIMEOUT_MS,
                )
                page.wait_for_timeout(800)

                url_lower = (page.url or "").lower()
                if "/sys/login" in url_lower or _syb_is_login_page(page):
                    login_result[0] = "登录失败：仍在登录页"
                    return

                login_result[0] = True
            except Exception as e:
                login_result[0] = f"登录异常: {e}"
            finally:
                done_event.set()

        syb_tab._ensure_agent()
        syb_tab._agent.submit(task, "TG远程登录")
        done_event.wait(timeout=60)

        result = login_result[0]
        if result is True:
            self._send(chat_id, "✅ 物流系统登录成功！")
            self.on_log("[TG-OPS] SYB 远程登录成功")
            # 同步 UI 状态
            try:
                app = self.app
                if app:
                    app.after(0, lambda: syb_tab.var_status.set("会话：登录成功"))
            except Exception:
                pass
        elif isinstance(result, str):
            self._send(chat_id, f"❌ {result}")
            self.on_log(f"[TG-OPS] SYB 远程登录失败: {result}")
        else:
            self._send(chat_id, "❌ 登录超时，请重试。")
            self.on_log("[TG-OPS] SYB 远程登录超时")

    def _syb_refresh_captcha_for_tg(self, chat_id: str,
                                     syb_tab, page) -> None:
        """登录失败后，在同一 page 上刷新验证码并重新发送到 TG。"""
        try:
            from core.shunyunbao_upload_feature import (
                _syb_locate_captcha_img, _syb_capture_captcha_png,
            )
            img = _syb_locate_captcha_img(page)
            if img is not None:
                try:
                    img.click(timeout=3000)
                    page.wait_for_timeout(600)
                except Exception:
                    pass
            ok = _syb_capture_captcha_png(
                page, syb_tab._captcha_path, self.on_log,
            )
            if ok:
                import time as _t
                syb_tab._captcha_ts = _t.time()
                kb = self.tg.make_keyboard([
                    [{"text": "🔄 刷新验证码", "callback_data": "syb:refresh"}],
                    [{"text": "❌ 取消", "callback_data": "ops:cancel"}],
                ])
                self.tg.send_photo(
                    chat_id, str(syb_tab._captcha_path),
                    caption="验证码已刷新，请重新输入：",
                    reply_markup=kb,
                )
                with self._session_lock:
                    self._sessions[chat_id] = UserSession(
                        step=SessionStep.SYB_WAIT_CAPTCHA,
                        created_at=_t.time(),
                    )
        except Exception as e:
            self.on_log(f"[TG-OPS] SYB 刷新验证码失败: {e}")

    def _cmd_syb_refresh(self, chat_id: str) -> None:
        """刷新验证码并重新发送到 TG。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        self._send(chat_id, "⏳ 正在刷新验证码...")
        self.on_log("[TG-OPS] SYB 远程刷新验证码")
        threading.Thread(
            target=self._syb_get_captcha_and_send,
            args=(chat_id, syb_tab),
            daemon=True,
        ).start()

    def _handle_syb_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "refresh":
            self._cmd_syb_refresh(chat_id)
        elif action == "store":
            self._syb_do_upload_data(chat_id, "store")
        elif action == "home":
            self._syb_do_upload_data(chat_id, "home")
        elif action == "checkinput":
            self._syb_check_ask_input(chat_id)
        elif action == "monadd":
            self._syb_mon_ask_input(chat_id)
        elif action == "monstatus":
            self._syb_mon_show_status(chat_id)
        elif action == "monnow":
            self._syb_mon_check_now(chat_id)
        elif action == "montoggle":
            self._syb_mon_toggle(chat_id)
        elif action == "monremove":
            self._syb_mon_ask_remove(chat_id)
        elif action == "monedit":
            self._syb_mon_ask_edit(chat_id)
        elif action == "labelgo":
            self._syb_do_upload_labels(chat_id)

    def _handle_sybv_cb(self, chat_id: str, parts: List[str]) -> None:
        """v6.0.75:SYB「一鍵作廢並重上傳」按鈕回調。
        callback_data 格式:sybv:{ok|no}:{token}
        token 對應 runtime/syb_void_pending.json 內的 pending action。
        """
        import json as _json
        import threading as _th
        from pathlib import Path as _P

        action = parts[1] if len(parts) > 1 else ""
        token = parts[2] if len(parts) > 2 else ""
        if action not in ("ok", "no") or not token:
            self._send(chat_id, "⚠️ 無效操作")
            return

        # 從 pending cache 讀取訂單資訊
        try:
            from app import BASE_DIR as _BD
            pending_fp = _BD / "runtime" / "syb_void_pending.json"
        except Exception:
            pending_fp = _P("runtime/syb_void_pending.json")

        try:
            if not pending_fp.exists():
                self._send(chat_id, "⚠️ 待處理項已過期或不存在(可能已超過 24h)")
                return
            pending = _json.loads(pending_fp.read_text(encoding="utf-8"))
            info = pending.get(token)
            if not info:
                self._send(chat_id, "⚠️ 待處理項已過期或已處理")
                return
        except Exception as e:
            self._send(chat_id, f"⚠️ 讀取待處理項失敗:{e}")
            return

        perf_code = info.get("perf_code", "")
        order_code = info.get("order_code", "")
        tpl_path = info.get("tpl_path", "")

        # 用完即刪
        try:
            pending.pop(token, None)
            pending_fp.write_text(_json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        if action == "no":
            self._send(
                chat_id,
                f"❌ 已駁回\n訂單:{order_code}\n業績:{perf_code}\n\n"
                f"SYB 已有此訂單但業績未作廢,維持現狀。"
            )
            return

        # action == "ok":作廢 + 重上傳
        self._send(chat_id, f"⏳ 處理中:作廢業績 {perf_code}|{order_code} ...")

        def _do_void_and_reupload():
            try:
                from core.performance_feature import set_void_status
                pk = f"{perf_code}|{order_code}"
                ok, err = set_void_status(pk, "final")
                if not ok:
                    self._send(chat_id, f"❌ 業績作廢失敗:{err}\nPK={pk}")
                    return
                self._send(chat_id, f"✓ 業績已作廢 (PK={pk}),正在重新上傳 SYB...")

                # 觸發 SYB 重新上傳該訂單(走 +N 修正版本)
                syb_tab = getattr(self.app, "syb_upload_tab", None)
                if not syb_tab or not tpl_path:
                    self._send(
                        chat_id,
                        "⚠️ 已作廢,但找不到模板路徑自動重上傳。\n"
                        "請手動到主程序【物流系統】上傳資料。"
                    )
                    return

                # 構造 orders → 呼 on_ship_results
                # 帶 force_voided_orders={order_code} bypass D1 同步延遲
                # (剛 set_void_status,D1 可能還沒同步出來)
                orders = [{
                    "order_no": order_code,
                    "from_purchase_monitor": True,
                    "purchase_platform": "xianyu",
                    "syb_auto_upload": True,
                    "syb_latest_template_path": tpl_path,
                }]
                try:
                    syb_tab.on_ship_results(
                        "", "", orders, [],
                        force_voided_orders={order_code},
                    )

                    # v6.0.75:作廢後原始訂單號不需要再監控發貨
                    # (有效的是 +N 修正版本,_mon_add_items_bulk 內部已自動加入新號)
                    removed = False
                    try:
                        removed = syb_tab._mon_remove_one(order_code)
                    except Exception as _e_rm:
                        self.on_log(f"[TG-OPS] [sybv] 移除原始訂單發貨監控異常: {_e_rm}")

                    self._send(
                        chat_id,
                        f"✅ 完成!\n"
                        f"訂單:{order_code}\n"
                        f"業績 PK:{pk}\n"
                        f"狀態:已作廢 final + SYB +N 重上傳\n"
                        f"發貨監控:{'已移除原始訂單號(改監控 +N 版本)' if removed else '原始訂單不在監控,無需處理'}\n"
                        f"請去 SYB 後台確認修正版本訂單。"
                    )
                except Exception as e:
                    self._send(chat_id, f"⚠️ 已作廢但重上傳異常:{e}")
            except Exception as e:
                self._send(chat_id, f"❌ 一鍵作廢處理異常:{e}")

        _th.Thread(target=_do_void_and_reupload, daemon=True).start()

    # ==================================================================
    # /sybupload 上传出货资料（店配/宅配）
    # ==================================================================

    def _cmd_syb_upload(self, chat_id: str) -> None:
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        tpl = syb_tab._find_latest_template()
        if not tpl:
            self._send(chat_id,
                       "⚠️ 找不到最新模板文件。\n"
                       "请确认已生成 最新模板_YYYYMMDD.xlsx")
            return

        lines = ["📤【上传出货资料】"]
        lines.append(f"模板文件：{tpl.name}")
        lines.append("\n选择上传类型：")
        lines.append("  店配 = 线上贴单资料")
        lines.append("  宅配 = 宅配打包资料")

        kb = self.tg.make_keyboard([
            [
                {"text": "🏪 店配上传", "callback_data": "syb:store"},
                {"text": "🏠 宅配上传", "callback_data": "syb:home"},
            ],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _syb_do_upload_data(self, chat_id: str, kind: str) -> None:
        """后台执行店配/宅配资料上传。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        tpl = syb_tab._find_latest_template()
        if not tpl:
            self._send(chat_id, "⚠️ 找不到最新模板文件。")
            return

        label = "店配" if kind == "store" else "宅配"
        self._send(chat_id, f"⏳ 正在上传{label}资料...")
        self.on_log(f"[TG-OPS] SYB 远程上传{label}资料")

        threading.Thread(
            target=self._syb_upload_data_thread,
            args=(chat_id, syb_tab, tpl, kind),
            daemon=True,
        ).start()

    def _syb_upload_data_thread(self, chat_id: str, syb_tab,
                                tpl, kind: str) -> None:
        from pathlib import Path as _Path
        from core.shunyunbao_upload_feature import (
            SHEET_STORE, SHEET_HOME, _has_data_rows,
            _copy_single_sheet, _safe_mkdir,
            SYB_IMPORT_URL, SYB_GOTO_TIMEOUT_MS,
        )

        sheet = SHEET_STORE if kind == "store" else SHEET_HOME
        option = "店配" if kind == "store" else "线下"
        label = "店配" if kind == "store" else "宅配"

        try:
            if not _has_data_rows(tpl, sheet):
                self._send(chat_id,
                           f"⚠️ 模板 {tpl.name} 的『{sheet}』没有数据行，跳过。")
                return
        except Exception as e:
            self._send(chat_id, f"⚠️ 读取模板失败：{e}")
            return

        import time as _t
        tmpdir = _Path(tpl).parent.parent / "output" / "syb_upload_tmp"
        _safe_mkdir(tmpdir)
        tmp_xlsx = tmpdir / f"syb_import_{kind}_{_t.strftime('%Y%m%d_%H%M%S')}.xlsx"
        try:
            rows, cols = _copy_single_sheet(tpl, sheet, tmp_xlsx)
        except Exception as e:
            self._send(chat_id, f"⚠️ 生成导入文件失败：{e}")
            return

        done_event = threading.Event()
        upload_result = [None]

        def task(page):
            try:
                page.goto(SYB_IMPORT_URL, wait_until="commit",
                          timeout=SYB_GOTO_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("domcontentloaded",
                                             timeout=SYB_GOTO_TIMEOUT_MS)
                except Exception:
                    pass

                cur = getattr(page, "url", "") or ""
                if ("/login" in cur or "/sys/login" in cur) \
                        and "/sys/admin/" not in cur:
                    upload_result[0] = "导入页被重定向到登录页（未登录/会话失效）"
                    return

                # 等页面控件出现
                ready = False
                for sel in ["input[placeholder*='导入类型']",
                            "text=导入类型", ".el-select"]:
                    try:
                        page.wait_for_selector(sel, timeout=25_000)
                        ready = True
                        break
                    except Exception:
                        pass
                if not ready:
                    upload_result[0] = "导入页面未加载到可操作状态"
                    return

                # 选择导入类型：新增出货资料
                syb_tab._upload_data(tpl, kind)
                upload_result[0] = True
            except Exception as e:
                upload_result[0] = f"上传异常：{e}"
            finally:
                done_event.set()

        syb_tab._ensure_agent()
        syb_tab._agent.submit(task, f"TG远程上传{label}资料")
        done_event.wait(timeout=120)

        # 清理临时文件
        try:
            tmp_xlsx.unlink(missing_ok=True)
        except Exception:
            pass

        result = upload_result[0]
        if result is True:
            self._send(chat_id,
                       f"✅ {label}资料上传完成！\n"
                       f"模板：{tpl.name}\n"
                       f"Sheet：{sheet}（{rows}行 {cols}列）")
            self.on_log(f"[TG-OPS] SYB {label}资料上传成功")
        elif isinstance(result, str):
            self._send(chat_id, f"❌ {label}上传失败：{result}")
        else:
            self._send(chat_id, f"❌ {label}上传超时，请重试。")

    # ==================================================================
    # /syblabels 上传面单 PDF
    # ==================================================================

    def _cmd_syb_labels(self, chat_id: str) -> None:
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        # 检查 tg_uploads 目录中的 PDF 文件
        pdf_dir = ROOT_DIR / "tg_uploads"
        pdfs = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []

        lines = ["📋【上传面单 PDF】"]
        lines.append("面单文件名必须 = 订单编号.pdf")
        lines.append("例如：12345678.pdf")
        lines.append("\n请直接发送 PDF 文件到此 Bot，")
        lines.append("系统会自动保存到上传目录。")

        if pdfs:
            lines.append(f"\n当前待上传 PDF：{len(pdfs)} 个")
            for p in pdfs[:10]:
                lines.append(f"  {p.name}")
            if len(pdfs) > 10:
                lines.append(f"  ...还有 {len(pdfs) - 10} 个")

        kb = self.tg.make_keyboard([
            [{"text": "📤 开始上传面单", "callback_data": "syb:labelgo"}],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _syb_do_upload_labels(self, chat_id: str) -> None:
        """执行面单 PDF 批量上传。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        pdf_dir = ROOT_DIR / "tg_uploads"
        if not pdf_dir.exists():
            self._send(chat_id, "⚠️ tg_uploads 目录不存在。")
            return

        pdfs = sorted(pdf_dir.glob("*.pdf"))
        if not pdfs:
            self._send(chat_id,
                       "⚠️ tg_uploads 目录中没有 PDF 文件。\n"
                       "请先发送 PDF 到此 Bot。")
            return

        self._send(chat_id, f"⏳ 正在上传 {len(pdfs)} 个面单 PDF...")
        self.on_log(f"[TG-OPS] SYB 远程上传面单: {len(pdfs)} 个")

        threading.Thread(
            target=self._syb_upload_labels_thread,
            args=(chat_id, syb_tab, pdfs),
            daemon=True,
        ).start()

    def _syb_upload_labels_thread(self, chat_id: str, syb_tab,
                                  pdfs: list) -> None:
        """后台线程：批量上传面单 PDF 到物流系统。"""
        from core.shunyunbao_upload_feature import SYB_STOCK_URL

        done_event = threading.Event()
        results = []

        def task(page):
            try:
                page.goto(SYB_STOCK_URL, wait_until="domcontentloaded")
                # 等工具栏和表格出现
                page.wait_for_selector(
                    "div.ctrl-left", state="visible", timeout=30_000)
                end_t = time.time() + 30
                while time.time() < end_t:
                    if (page.locator(".vxe-table").count() > 0
                            or page.locator(".el-table").count() > 0):
                        break
                    page.wait_for_timeout(200)

                # 关闭残留弹窗
                try:
                    old = page.locator(
                        ".el-dialog__wrapper:visible"
                    ).filter(has=page.locator(
                        ".el-dialog__title", has_text="批量上传面单"
                    )).first
                    if old.count() > 0:
                        old.locator(".el-dialog__headerbtn").first.click(
                            timeout=2000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass

                # 点击"上传面单"按钮
                self._syb_click_toolbar(page, "上传面单", timeout=15_000)

                # 等弹窗出现
                wrap = page.locator(
                    ".el-dialog__wrapper:visible"
                ).filter(has=page.locator(
                    ".el-dialog__title", has_text="批量上传面单"
                )).first
                wrap.wait_for(timeout=30_000)
                dlg = wrap.locator("div.el-dialog").first

                file_input = dlg.locator("input[type=file]").first
                if file_input.count() == 0:
                    results.append(("ERROR", "未找到上传 file input"))
                    return

                for p in pdfs:
                    try:
                        file_input.set_input_files(str(p))
                        # 等结果出现
                        row_el = dlg.locator(
                            ".el-table__body-wrapper tbody tr"
                        ).filter(has_text=p.name).first
                        row_vxe = dlg.locator(
                            ".vxe-table--body-wrapper .vxe-body--row"
                        ).filter(has_text=p.name).first

                        deadline = time.time() + 60
                        while time.time() < deadline:
                            if row_vxe.count() > 0 or row_el.count() > 0:
                                break
                            page.wait_for_timeout(250)

                        row = row_vxe if row_vxe.count() > 0 else row_el
                        if row.count() == 0:
                            results.append((p.name, "等待结果超时"))
                            continue

                        txt = row.inner_text()
                        if "保存成功" in txt:
                            results.append((p.name, "保存成功"))
                        else:
                            results.append((p.name, txt[:80]))
                    except Exception as e:
                        results.append((p.name, f"失败: {e}"))

                # 关闭弹窗
                try:
                    wrap.locator(".el-dialog__headerbtn").first.click(
                        timeout=2000)
                except Exception:
                    pass
            except Exception as e:
                results.append(("ERROR", str(e)))
            finally:
                done_event.set()

        syb_tab._ensure_agent()
        syb_tab._agent.submit(task, f"TG远程上传面单({len(pdfs)}个)")
        done_event.wait(timeout=180)

        if not results:
            self._send(chat_id, "❌ 面单上传超时，请重试。")
            return

        ok_count = sum(1 for _, s in results if s == "保存成功")
        lines = [f"📋【面单上传结果】共 {len(pdfs)} 个"]
        for name, status in results:
            icon = "✅" if status == "保存成功" else "❌"
            lines.append(f"  {icon} {name} → {status}")
        lines.append(f"\n成功: {ok_count}/{len(pdfs)}")
        self._send(chat_id, "\n".join(lines))

    @staticmethod
    def _syb_click_toolbar(page, label: str, timeout: int = 15_000) -> None:
        """在物流系统工具栏中点击指定按钮。"""
        root = page.locator("div.ctrl-left").first
        end_t = time.time() + timeout / 1000
        while time.time() < end_t:
            try:
                spans = root.locator("span.txt")
                for i in range(min(spans.count(), 50)):
                    s = spans.nth(i)
                    if not s.is_visible():
                        continue
                    try:
                        if s.inner_text().strip() != label:
                            continue
                    except Exception:
                        continue
                    clickable = s.locator(
                        "xpath=ancestor::*[self::a or self::button][1]")
                    if clickable.count() == 0:
                        clickable = s
                    clickable.scroll_into_view_if_needed()
                    try:
                        clickable.click(timeout=2000)
                    except Exception:
                        clickable.click(timeout=2000, force=True)
                    return
            except Exception:
                pass
            page.wait_for_timeout(250)
        raise RuntimeError(f"找不到工具栏按钮：{label}")

    # ==================================================================
    # /sybcheck 检查订单是否已发货
    # ==================================================================

    def _cmd_syb_check(self, chat_id: str) -> None:
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        kb = self.tg.make_keyboard([
            [{"text": "📝 输入订单号", "callback_data": "syb:checkinput"}],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(
            chat_id,
            "📦【检查发货状态】\n"
            "输入订单号可查询物流系统中是否已发货。\n"
            "支持多个订单号（每行一个，最多50个）",
            reply_markup=kb,
        )

    def _syb_check_ask_input(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_CHECK_ORDERS,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "请输入要检查的订单号（每行一个，最多50个）：\n"
            "发送 /cancel 取消",
        )

    def _syb_check_step_input(self, chat_id: str,
                               sess: UserSession, text: str) -> None:
        order_nos = [x.strip() for x in text.splitlines() if x.strip()][:50]
        if not order_nos:
            self._send(chat_id, "未检测到有效订单号，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id,
                   f"⏳ 正在检查 {len(order_nos)} 个订单的发货状态...")
        self.on_log(f"[TG-OPS] SYB 检查发货: {len(order_nos)} 个")
        threading.Thread(
            target=self._syb_check_shipped_thread,
            args=(chat_id, order_nos),
            daemon=True,
        ).start()

    def _syb_check_shipped_thread(self, chat_id: str,
                                   order_nos: List[str]) -> None:
        """后台线程：检查订单是否已发货。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        from core.shunyunbao_upload_feature import SYB_STOCK_URL
        done_event = threading.Event()
        results: Dict[str, str] = {}

        def task(page):
            try:
                page.goto(SYB_STOCK_URL,
                          wait_until="domcontentloaded")
                page.wait_for_selector(
                    "div.ctrl-left", state="visible",
                    timeout=30_000)
                end_t = time.time() + 30
                while time.time() < end_t:
                    if (page.locator(".vxe-table").count() > 0
                            or page.locator(".el-table").count() > 0):
                        break
                    page.wait_for_timeout(200)

                for ono in order_nos:
                    try:
                        shipped = self._syb_check_one_order(page, ono)
                        results[ono] = "已发货" if shipped else "未发货"
                    except Exception as e:
                        results[ono] = f"查询失败: {e}"
            except Exception as e:
                for ono in order_nos:
                    if ono not in results:
                        results[ono] = f"系统错误: {e}"
            finally:
                done_event.set()

        syb_tab._ensure_agent()
        syb_tab._agent.submit(
            task, f"TG远程检查发货({len(order_nos)}个)")
        done_event.wait(timeout=180)

        if not results:
            self._send(chat_id, "❌ 检查超时，请重试。")
            return

        shipped_count = sum(1 for s in results.values() if s == "已发货")
        lines = [f"📦【发货状态检查】共 {len(order_nos)} 个"]
        for ono in order_nos:
            status = results.get(ono, "未知")
            icon = "✅" if status == "已发货" else "❌"
            lines.append(f"  {icon} {ono} → {status}")
        lines.append(f"\n已发货: {shipped_count}/{len(order_nos)}")
        self._send(chat_id, "\n".join(lines))

    @staticmethod
    def _syb_check_one_order(page, order_no: str) -> bool:
        """在物流查询页检查单个订单是否已发货。"""
        import re as _re

        # 展开高级搜索
        try:
            adv = page.locator("text=高级搜索").first
            if adv.count() > 0 and adv.is_visible():
                adv.click(timeout=3000)
                page.wait_for_timeout(500)
        except Exception:
            pass

        # 填写订单号搜索
        search_input = page.locator(
            "input[placeholder*='订单编号'], "
            "input[placeholder*='订单号']"
        ).first
        if search_input.count() == 0:
            raise RuntimeError("找不到订单号搜索框")

        search_input.fill("", timeout=2000)
        search_input.fill(order_no, timeout=3000)

        # 点击搜索
        search_btn = page.locator(
            "button:has-text('搜索'), "
            "button:has-text('查询')"
        ).first
        if search_btn.count() > 0:
            search_btn.click(timeout=3000)
        page.wait_for_timeout(1500)

        # 检查结果行
        shipped = False
        try:
            rows = page.locator(
                ".vxe-body--row, "
                ".el-table__body-wrapper tbody tr"
            )
            for i in range(min(rows.count(), 5)):
                row = rows.nth(i)
                if not row.is_visible():
                    continue
                txt = row.inner_text()
                if order_no in txt:
                    # 尝试点击该行并查看物流记录
                    try:
                        row.click(timeout=2000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass

                    try:
                        OpsCommandHandler._syb_click_toolbar(
                            page, "物流记录", timeout=5000)
                        dlg = page.locator(
                            ".el-dialog__wrapper:visible"
                        ).filter(has=page.locator(
                            ".el-dialog__title",
                            has_text="物流"
                        )).first
                        dlg.wait_for(
                            state="visible", timeout=6000)
                        modal_txt = dlg.inner_text()
                        shipped = bool(
                            _re.search(r"已\s*发\s*货", modal_txt)
                        ) or bool(
                            _re.search(r"已\s*發\s*貨", modal_txt)
                        )
                        # 关闭弹窗
                        try:
                            dlg.locator(
                                ".el-dialog__headerbtn"
                            ).click(timeout=2000)
                            dlg.wait_for(
                                state="hidden", timeout=5000)
                        except Exception:
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        return shipped

    # ==================================================================
    # /sybmon 发货监控
    # ==================================================================

    def _cmd_syb_mon(self, chat_id: str) -> None:
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        active = [it for it in items.values() if it.active]
        enabled = False
        try:
            enabled = syb_tab.var_mon_enabled.get()
        except Exception:
            pass

        lines = ["📡【发货监控】"]
        lines.append(f"状态: {'✅ 已启用' if enabled else '⏹ 已停用'}")
        lines.append(f"监控订单: {len(active)} 个活跃 / {len(items)} 个总计")

        if active:
            lines.append("\n最近监控订单:")
            for it in list(active)[:10]:
                status = it.last_status or "待检查"
                lines.append(f"  {it.order_no} | {it.account_name} | {status}")
            if len(active) > 10:
                lines.append(f"  ...还有 {len(active) - 10} 个")

        kb = self.tg.make_keyboard([
            [
                {"text": "📝 添加订单", "callback_data": "syb:monadd"},
                {"text": "📊 详细状态", "callback_data": "syb:monstatus"},
            ],
            [
                {"text": "✏️ 修改单号", "callback_data": "syb:monedit"},
                {"text": "🗑 移除订单", "callback_data": "syb:monremove"},
            ],
            [
                {"text": "🔄 立即检查", "callback_data": "syb:monnow"},
                {"text": "⏯ 开关监控", "callback_data": "syb:montoggle"},
            ],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _syb_mon_ask_input(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_MON_ORDERS,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "请输入要加入监控的订单号（每行一个）：\n"
            "发送 /cancel 取消",
        )

    def _syb_mon_step_input(self, chat_id: str,
                             sess: UserSession, text: str) -> None:
        order_nos = [x.strip() for x in text.splitlines()
                     if x.strip()]
        if not order_nos:
            self._send(chat_id, "未检测到有效订单号，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)

        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        added = syb_tab._mon_add_items_bulk(
            "", "TG手动", order_nos, source="tg_manual")
        self._send(
            chat_id,
            f"✅ 已加入发货监控：{added} 个新订单\n"
            f"（共提交 {len(order_nos)} 个，"
            f"已存在的会重新启用）")
        self.on_log(
            f"[TG-OPS] SYB 监控添加: {len(order_nos)} 个")

    def _syb_mon_show_status(self, chat_id: str) -> None:
        """显示发货监控详细状态。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        if not items:
            self._send(chat_id, "📡 发货监控列表为空。")
            return

        lines = [f"📡【发货监控详情】共 {len(items)} 个"]
        active_list = []
        inactive_list = []
        for ono, it in items.items():
            if it.active:
                active_list.append(it)
            else:
                inactive_list.append(it)

        if active_list:
            lines.append(f"\n✅ 活跃 ({len(active_list)}):")
            for it in active_list[:20]:
                status = it.last_status or "待检查"
                checked = ""
                if it.last_checked_at > 0:
                    import time as _t
                    checked = _t.strftime(
                        "%m-%d %H:%M",
                        _t.localtime(it.last_checked_at))
                lines.append(
                    f"  {it.order_no} | "
                    f"{it.account_name} | "
                    f"{status} | {checked}")
            if len(active_list) > 20:
                lines.append(
                    f"  ...还有 {len(active_list) - 20} 个")

        if inactive_list:
            lines.append(f"\n⏹ 停用 ({len(inactive_list)}):")
            for it in inactive_list[:5]:
                lines.append(f"  {it.order_no} | {it.last_status}")
            if len(inactive_list) > 5:
                lines.append(
                    f"  ...还有 {len(inactive_list) - 5} 个")

        self._send(chat_id, "\n".join(lines))

    def _syb_mon_check_now(self, chat_id: str) -> None:
        """立即触发所有监控订单检查。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        active = [it for it in items.values() if it.active]
        if not active:
            self._send(chat_id, "📡 没有活跃的监控订单。")
            return

        try:
            syb_tab._mon_check_all_now()
            self._send(
                chat_id,
                f"🔄 已触发立即检查 {len(active)} 个订单。\n"
                f"检查完成后可用 /sybmon 查看结果。")
            self.on_log("[TG-OPS] SYB 触发立即检查全部")
        except Exception as e:
            self._send(chat_id, f"❌ 触发检查失败：{e}")

    def _syb_mon_toggle(self, chat_id: str) -> None:
        """切换发货监控开关。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        try:
            cur = syb_tab.var_mon_enabled.get()
            syb_tab.var_mon_enabled.set(not cur)
            syb_tab._mon_on_toggle()
            new_state = "启用" if not cur else "停用"
            self._send(chat_id, f"📡 发货监控已{new_state}")
            self.on_log(f"[TG-OPS] SYB 监控切换: {new_state}")
        except Exception as e:
            self._send(chat_id, f"❌ 切换失败：{e}")

    # ---- 移除订单 ----

    def _syb_mon_ask_remove(self, chat_id: str) -> None:
        """显示订单列表，让用户选择要移除的序号。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        order_list = list(items.keys())
        if not order_list:
            self._send(chat_id, "📡 监控列表为空，无订单可移除。")
            return

        lines = ["🗑 请输入要移除的序号（可多个，用空格或逗号分隔）：\n"]
        for i, ono in enumerate(order_list, 1):
            it = items[ono]
            status = it.last_status or "待检查"
            state = "✅" if it.active else "⏹"
            lines.append(f"  {i}. {state} {ono} | {it.account_name} | {status}")

        lines.append("\n发送 /cancel 取消")

        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_MON_REMOVE,
                created_at=time.time(),
                mon_order_list=order_list,
            )
        self._send(chat_id, "\n".join(lines))

    def _syb_mon_step_remove(self, chat_id: str,
                              sess: UserSession, text: str) -> None:
        """用户输入序号后执行移除。"""
        with self._session_lock:
            self._sessions.pop(chat_id, None)

        order_list = sess.mon_order_list or []
        if not order_list:
            self._send(chat_id, "⚠️ 订单列表已过期，请重新操作。")
            return

        # 解析序号
        raw = text.replace(",", " ").replace("，", " ")
        indices = []
        for part in raw.split():
            try:
                idx = int(part.strip())
                if 1 <= idx <= len(order_list):
                    indices.append(idx)
            except ValueError:
                continue

        if not indices:
            self._send(chat_id, "⚠️ 未识别到有效序号，操作取消。")
            return

        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        removed = 0
        for idx in sorted(set(indices)):
            ono = order_list[idx - 1]
            if ono in items:
                try:
                    del items[ono]
                except Exception:
                    pass
                try:
                    ring = getattr(syb_tab, "_mon_order_ring", None)
                    if ring is not None:
                        while ono in ring:
                            ring.remove(ono)
                except Exception:
                    pass
                removed += 1

        if removed > 0:
            syb_tab._mon_save_state()
            syb_tab._mon_refresh_tree()

        self._send(chat_id, f"🗑 已移除 {removed} 个订单。")
        self.on_log(f"[TG-OPS] SYB 监控移除: {removed} 个")

    # ---- 修改单号 ----

    def _syb_mon_ask_edit(self, chat_id: str) -> None:
        """显示订单列表，让用户选择要修改的序号。"""
        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        order_list = list(items.keys())
        if not order_list:
            self._send(chat_id, "📡 监控列表为空，无订单可修改。")
            return

        lines = ["✏️ 请输入要修改的订单序号（单个数字）：\n"]
        for i, ono in enumerate(order_list, 1):
            it = items[ono]
            status = it.last_status or "待检查"
            state = "✅" if it.active else "⏹"
            lines.append(f"  {i}. {state} {ono} | {it.account_name} | {status}")

        lines.append("\n发送 /cancel 取消")

        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_MON_EDIT_SELECT,
                created_at=time.time(),
                mon_order_list=order_list,
            )
        self._send(chat_id, "\n".join(lines))

    def _syb_mon_step_edit_select(self, chat_id: str,
                                   sess: UserSession, text: str) -> None:
        """用户输入序号后，进入第二步：输入新单号。"""
        order_list = sess.mon_order_list or []
        if not order_list:
            with self._session_lock:
                self._sessions.pop(chat_id, None)
            self._send(chat_id, "⚠️ 订单列表已过期，请重新操作。")
            return

        try:
            idx = int(text.strip())
        except ValueError:
            self._send(chat_id, "⚠️ 请输入有效的数字序号：")
            return

        if idx < 1 or idx > len(order_list):
            self._send(chat_id,
                        f"⚠️ 序号超出范围（1-{len(order_list)}），请重新输入：")
            return

        old_no = order_list[idx - 1]
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.SYB_WAIT_MON_EDIT_NEWNO,
                created_at=time.time(),
                mon_edit_old_no=old_no,
            )
        self._send(
            chat_id,
            f"当前单号：{old_no}\n"
            f"请输入新的订单号：\n"
            f"发送 /cancel 取消",
        )

    def _syb_mon_step_edit_newno(self, chat_id: str,
                                  sess: UserSession, text: str) -> None:
        """用户输入新单号后执行替换。"""
        with self._session_lock:
            self._sessions.pop(chat_id, None)

        old_no = sess.mon_edit_old_no or ""
        new_no = text.strip()
        if not old_no:
            self._send(chat_id, "⚠️ 会话已过期，请重新操作。")
            return
        if not new_no:
            self._send(chat_id, "⚠️ 新单号不能为空，操作取消。")
            return
        if new_no == old_no:
            self._send(chat_id, "⚠️ 新单号与旧单号相同，操作取消。")
            return

        app = self.app
        syb_tab = getattr(app, "syb_upload_tab", None) if app else None
        if not syb_tab:
            self._send(chat_id, "⚠️ 物流系统模块未加载。")
            return

        items = getattr(syb_tab, "_mon_items", {}) or {}
        if old_no not in items:
            self._send(chat_id, f"⚠️ 原单号 {old_no} 已不在监控列表中。")
            return
        if new_no in items:
            self._send(chat_id, f"⚠️ 新单号 {new_no} 已在监控列表中，操作取消。")
            return

        # 复制旧条目，替换单号
        old_item = items.pop(old_no)
        old_item.order_no = new_no
        old_item.last_status = ""
        old_item.last_checked_at = 0.0
        old_item.next_check_at = time.time()
        items[new_no] = old_item

        # 更新 ring
        ring = getattr(syb_tab, "_mon_order_ring", None)
        if ring is not None:
            try:
                idx = ring.index(old_no)
                ring[idx] = new_no
            except ValueError:
                ring.append(new_no)

        syb_tab._mon_save_state()
        syb_tab._mon_refresh_tree()

        self._send(
            chat_id,
            f"✅ 单号已修改：\n"
            f"  旧：{old_no}\n"
            f"  新：{new_no}\n"
            f"将在下一轮自动检查新单号。",
        )
        self.on_log(f"[TG-OPS] SYB 监控修改单号: {old_no} → {new_no}")

    # ==================================================================
    # 商品查询（云端 D1）
    # ==================================================================

    def _cmd_pquery(self, chat_id: str) -> None:
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.PQUERY_WAIT_CODE,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "🔎 请输入商品编号进行查询：\n"
            "发送 /cancel 取消",
        )

    def _pquery_direct(self, chat_id: str, code: str) -> None:
        """直接发数字自动查询，不需要进 session"""
        self._do_query(chat_id, code)

    def _pquery_step_input(self, chat_id: str, sess: UserSession,
                           text: str) -> None:
        code = text.strip()
        if not code:
            self._send(chat_id, "⚠️ 商品编号不能为空，请重新输入：")
            return
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._do_query(chat_id, code)

    def _do_query(self, chat_id: str, code: str) -> None:
        """实际查询逻辑"""
        try:
            resp = requests.get(
                f"{PRODUCT_QUERY_WORKER_URL}/api/query",
                params={"code": code},
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            self._send(chat_id, f"⚠️ 查询失败：{e}")
            return

        if not data.get("found") or not data.get("data"):
            self._send(chat_id, f"查不到商品编号：{code}")
            return

        rows = data["data"]
        lines = [f"🔎 查询结果（{len(rows)} 笔）："]
        for r in rows:
            barcode = r.get("barcode", "")
            if barcode.startswith("http"):
                source = "煤爐"
                link = barcode
            else:
                source = "閒魚"
                link = f"https://h5.m.goofish.com/item?forceFlush=1&id={barcode}"
            lines.append(
                f"\n📦 商品编号：{r.get('product_code', '')}\n"
                f"🔗 链接：{link}\n"
                f"🏷 来源：{source}\n"
                f"👤 帐号：{r.get('account', '')}\n"
                f"📁 数据：{r.get('owner', '')}"
            )

        self._send(chat_id, "\n".join(lines))

    # ==================================================================
    # 编号下架删除（从运营 Bot 触发管理 Bot 的 merch_id_ops）
    # ==================================================================

    def _start_merch_id_ops(self, chat_id: str) -> None:
        """触发根据商品编号下架删除，读取 ids/ 目录的文件"""
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "merch_running", False):
            self._send(chat_id, "⚠️ 批量操作已在运行中，请等待完成。")
            return

        # 检查 ids/ 目录是否有文件
        ids_dir = ROOT_DIR / "ids"
        if not ids_dir.exists():
            self._send(chat_id, "⚠️ ids/ 目录不存在，没有可下架的编号。")
            return

        txt_files = sorted(ids_dir.glob("*.txt"))
        if not txt_files:
            self._send(chat_id, "⚠️ ids/ 目录中没有 .txt 文件，没有可下架的编号。")
            return

        # 显示文件列表
        lines = ["🔢【编号下架删除】", f"ids/ 目录有 {len(txt_files)} 个文件："]
        total_ids = 0
        for f in txt_files[:15]:
            count = sum(1 for ln in f.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
            total_ids += count
            lines.append(f"  {f.name}（{count} 条）")
        if len(txt_files) > 15:
            lines.append(f"  ...还有 {len(txt_files) - 15} 个")
        lines.append(f"\n共 {total_ids} 条编号待下架删除")

        kb = self.tg.make_keyboard([
            [{"text": "✅ 确认启动下架删除", "callback_data": "merc:idops_confirm"}],
            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _confirm_merch_id_ops(self, chat_id: str) -> None:
        """确认后启动下架删除"""
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "merch_running", False):
            self._send(chat_id, "⚠️ 批量操作已在运行中。")
            return

        try:
            from core.accounts import save_settings
            settings = getattr(app, "settings", {})
            settings["merch_mode"] = "根據商品編號下架刪除"
            save_settings(settings)
            if hasattr(app, "var_merch_mode"):
                app.after(0, lambda: app.var_merch_mode.set("根據商品編號下架刪除"))
            app.after(100, app._start_merch_batch)
            self._send(chat_id, "✅ 编号下架删除已启动！\n可在 /status 查看批量操作状态。")
            self.on_log("[TG-OPS] 远程启动编号下架删除")
        except Exception as e:
            self._send(chat_id, f"❌ 启动失败：{e}")

    # ==================================================================
    # 云端数据检测（煤炉/闲鱼）
    # ==================================================================

    def _cloud_check(self, chat_id: str, check_type: str) -> None:
        """从云端拉取该用户的条码数据，写入本地txt，提供开始检测/下载按钮"""
        owner = str(chat_id)
        api_type = check_type  # "mercari" or "goofish"
        label = "煤爐" if check_type == "mercari" else "閒魚"
        prefix = "merc" if check_type == "mercari" else "goof"

        self._send(chat_id, f"⏳ 正在从云端拉取你的{label}数据...")

        # 分页拉取全部数据
        all_items: List[Dict] = []
        offset = 0
        page_limit = 5000
        total = 0
        try:
            while True:
                resp = requests.get(
                    f"{PRODUCT_QUERY_WORKER_URL}/api/barcodes",
                    params={"owner": owner, "type": api_type,
                            "limit": str(page_limit), "offset": str(offset)},
                    timeout=30,
                )
                data = resp.json()
                if not data.get("ok"):
                    self._send(chat_id, f"⚠️ 拉取失败：{data.get('error', '未知错误')}")
                    return
                total = data.get("total", 0)
                items = data.get("data", [])
                all_items.extend(items)
                if len(all_items) >= total or len(items) < page_limit:
                    break
                offset += page_limit
        except Exception as e:
            self._send(chat_id, f"⚠️ 拉取失败：{e}")
            return

        if total == 0:
            self._send(chat_id, f"你的云端数据中没有{label}条码。\n请先通过「编码数据更新」上传你的 Excel。")
            return

        # 写入本地 txt 文件 + sidecar mapping.json（保留帐号/商品编号映射）
        output_dir = ROOT_DIR / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        txt_name = f"cloud_{api_type}_{owner}.txt"
        txt_path = output_dir / txt_name
        mapping_path = output_dir / f"cloud_{api_type}_{owner}.mapping.json"

        # 构建 URL→[{account, product_code}] 映射（处理重复URL）
        url_mapping: Dict[str, List[Dict[str, str]]] = {}
        seen_urls_ordered: List[str] = []
        for item in all_items:
            barcode = item.get("barcode", "").strip()
            if not barcode:
                continue
            account = item.get("account", "").strip()
            product_code = item.get("product_code", "").strip()
            if barcode not in url_mapping:
                url_mapping[barcode] = []
                seen_urls_ordered.append(barcode)
            url_mapping[barcode].append({
                "account": account,
                "product_code": product_code,
            })

        # 去重后写入 txt（格式不变）
        with open(txt_path, "w", encoding="utf-8") as f:
            for url in seen_urls_ordered:
                f.write(url + "\n")

        # 写入 sidecar mapping JSON
        try:
            with open(mapping_path, "w", encoding="utf-8") as f:
                json.dump(url_mapping, f, ensure_ascii=False)
        except Exception as e:
            self.on_log(f"[TG-OPS] 写入 mapping.json 失败: {e}")

        dedup_count = len(seen_urls_ordered)

        self._send(chat_id, f"☁️ 已拉取 {total} 条{label}数据（去重后 {dedup_count} 条），正在启动检测...")

        # 直接启动检测
        self._cloud_start_check(chat_id, check_type)

    def _cloud_start_check(self, chat_id: str, check_type: str) -> None:
        """用云端拉取的本地txt触发软件端的煤炉/闲鱼检测"""
        owner = str(chat_id)
        api_type = check_type
        label = "煤爐" if check_type == "mercari" else "閒魚"

        output_dir = ROOT_DIR / "output"
        txt_path = output_dir / f"cloud_{api_type}_{owner}.txt"

        if not txt_path.exists():
            self._send(chat_id, "⚠️ 找不到本地数据文件，请先点「从云端数据检测」拉取数据。")
            return

        # 检查文件行数
        with open(txt_path, "r", encoding="utf-8") as f:
            line_count = sum(1 for line in f if line.strip())

        if line_count == 0:
            self._send(chat_id, "⚠️ 数据文件为空，请重新拉取。")
            return

        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if check_type == "mercari":
            tab = getattr(app, "mercari_check_tab", None)
            if not tab:
                self._send(chat_id, "⚠️ 煤爐檢查模块未加载。")
                return
            if tab._worker and tab._worker.is_alive():
                self._send(chat_id, "⚠️ 煤爐檢查正在运行中，请等待完成或先停止。")
                return
            # 设置输入文件并启动
            tab.var_input.set(str(txt_path))
            tab._stop_evt.clear()
            tab._set_btn_state(True)
            tab._set_progress("讀取URL中…")
            tab._set_counts(0, 0)

            import asyncio
            from core.mercari_check_feature import _MercariConfig

            cfg = _MercariConfig(
                max_concurrent=max(1, int(tab.var_max_concurrent.get() or 5)),
                batch_size=max(10, int(tab.var_batch_size.get() or 200)),
                max_retries=max(0, int(tab.var_max_retries.get() or 3)),
                auto_save_every=max(0, int(tab.var_auto_save_every.get() or 0)),
                headless=bool(tab.var_headless.get()),
            )
            out_path = tab.var_out.get().strip() or str(ROOT_DIR / "output" / "mercari_status_result.xlsx")

            def _run():
                try:
                    asyncio.run(tab._run_check(str(txt_path), out_path, cfg))
                except Exception as e:
                    tab.log(f"崩潰：{e}")
                    tab._set_progress(f"崩潰：{e}")
                finally:
                    tab._set_btn_state(False)

            tab._worker = threading.Thread(target=_run, daemon=True)
            tab._worker.start()

            self._send(chat_id, f"▶️ 已启动煤爐检测！共 {line_count} 条URL")
            self.on_log(f"[TG-OPS] 云端煤爐检测启动: {line_count} 条")

            # 启动进度同步线程
            threading.Thread(
                target=self._cloud_progress_monitor,
                args=(chat_id, tab, label, "mercari"),
                daemon=True,
            ).start()

        elif check_type == "goofish":
            tab = getattr(app, "goofish_check_tab", None)
            if not tab:
                self._send(chat_id, "⚠️ 鹹魚檢查模块未加载。")
                return
            if tab._worker and tab._worker.is_alive():
                self._send(chat_id, "⚠️ 鹹魚檢查正在运行中，请等待完成或先停止。")
                return
            # 设置输入文件并启动
            tab.var_input.set(str(txt_path))
            tab._stop_evt.clear()
            tab._set_btn_state(True)
            tab._set_progress("讀取ID中…")
            tab._set_counts(0, 0)

            out_path = tab.var_out.get().strip() or str(ROOT_DIR / "output" / "xianyu_已检测.txt")
            out_fail_path = tab.var_out_fail.get().strip() or str(ROOT_DIR / "output" / "xianyu_多次失败.txt")

            def _run():
                try:
                    tab._run_check(str(txt_path), out_path, out_fail_path)
                except RuntimeError as e:
                    tab.log(f"程序已终止：{e}")
                    tab._set_progress(f"程序已终止：{e}")
                except Exception as e:
                    tab.log(f"崩溃：{e}")
                    tab._set_progress(f"崩溃：{e}")
                finally:
                    tab._set_btn_state(False)

            tab._worker = threading.Thread(target=_run, daemon=True)
            tab._worker.start()

            self._send(chat_id, f"▶️ 已启动鹹魚检测！共 {line_count} 条ID")
            self.on_log(f"[TG-OPS] 云端鹹魚检测启动: {line_count} 条")

            # 启动进度同步线程
            threading.Thread(
                target=self._cloud_progress_monitor,
                args=(chat_id, tab, label, "goofish"),
                daemon=True,
            ).start()

    def _cloud_recheck_unknown(self, chat_id: str, check_type: str) -> None:
        """从已有检测结果中提取「未知」项，重新检测"""
        owner = str(chat_id)
        label = "煤爐" if check_type == "mercari" else "閒魚"

        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        tab = getattr(app, "mercari_check_tab", None)
        if not tab:
            self._send(chat_id, "⚠️ 煤爐檢查模块未加载。")
            return
        if tab._worker and tab._worker.is_alive():
            self._send(chat_id, "⚠️ 煤爐檢查正在运行中，请等待完成或先停止。")
            return

        # 找到现有的检测结果 Excel
        excel_path = tab.var_out.get().strip()
        if not excel_path or not os.path.exists(excel_path):
            excel_path = str(ROOT_DIR / "output" / "mercari_status_result.xlsx")
        if not os.path.exists(excel_path):
            self._send(chat_id, "⚠️ 找不到检测结果文件，请先执行一次完整检测。")
            return

        # 原始 mapping 路径
        output_dir = ROOT_DIR / "output"
        original_mapping = output_dir / f"cloud_{check_type}_{owner}.mapping.json"
        mapping_arg = str(original_mapping) if original_mapping.exists() else None

        # 提取未知 URL
        unknown_txt = str(output_dir / f"cloud_{check_type}_{owner}_unknown.txt")
        try:
            from core.mercari_check_feature import _extract_unknown_urls
            count = _extract_unknown_urls(excel_path, unknown_txt, mapping_arg)
        except Exception as e:
            self._send(chat_id, f"⚠️ 提取未知项失败：{e}")
            return

        if count == 0:
            self._send(chat_id, "✅ 没有未知项需要重新检测。")
            return

        self._send(chat_id, f"🔍 找到 {count} 条未知URL，正在启动重新检测…")

        # 设置输入文件并启动检测
        tab.var_input.set(unknown_txt)
        tab._stop_evt.clear()
        tab._set_btn_state(True)
        tab._set_progress("讀取URL中…")
        tab._set_counts(0, 0)

        import asyncio
        from core.mercari_check_feature import _MercariConfig

        cfg = _MercariConfig(
            max_concurrent=max(1, int(tab.var_max_concurrent.get() or 5)),
            batch_size=max(10, int(tab.var_batch_size.get() or 200)),
            max_retries=max(0, int(tab.var_max_retries.get() or 3)),
            auto_save_every=max(0, int(tab.var_auto_save_every.get() or 0)),
            headless=bool(tab.var_headless.get()),
        )
        out_path = str(output_dir / f"mercari_recheck_unknown_result.xlsx")
        tab.var_out.set(out_path)

        def _run():
            try:
                asyncio.run(tab._run_check(unknown_txt, out_path, cfg))
            except Exception as e:
                tab.log(f"崩潰：{e}")
                tab._set_progress(f"崩潰：{e}")
            finally:
                tab._set_btn_state(False)

        tab._worker = threading.Thread(target=_run, daemon=True)
        tab._worker.start()

        self._send(chat_id, f"▶️ 已启动未知项重新检测！共 {count} 条URL")
        self.on_log(f"[TG-OPS] 煤爐未知项重检启动: {count} 条")

        threading.Thread(
            target=self._cloud_progress_monitor,
            args=(chat_id, tab, f"{label}(重检未知)", "mercari"),
            daemon=True,
        ).start()

    def _cloud_recheck_unknown_goofish(self, chat_id: str) -> None:
        """从闲鱼检测结果中提取「未知」和「多次失败」项，重新检测"""
        owner = str(chat_id)
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        tab = getattr(app, "goofish_check_tab", None)
        if not tab:
            self._send(chat_id, "⚠️ 鹹魚檢查模块未加载。")
            return
        if tab._worker and tab._worker.is_alive():
            self._send(chat_id, "⚠️ 鹹魚檢查正在运行中。")
            return

        output_dir = ROOT_DIR / "output"
        txt_path = output_dir / f"cloud_goofish_{owner}.txt"
        excel_path = str(txt_path.with_suffix(".xlsx"))
        if not os.path.exists(excel_path):
            self._send(chat_id, "⚠️ 找不到检测结果文件，请先执行一次完整检测。")
            return

        original_mapping = output_dir / f"cloud_goofish_{owner}.mapping.json"
        mapping_arg = str(original_mapping) if original_mapping.exists() else None

        unknown_txt = str(output_dir / f"cloud_goofish_{owner}_unknown.txt")
        try:
            from core.goofish_check_feature import _extract_unknown_ids
            count = _extract_unknown_ids(excel_path, unknown_txt, mapping_arg)
        except Exception as e:
            self._send(chat_id, f"⚠️ 提取未知项失败：{e}")
            return

        if count == 0:
            self._send(chat_id, "✅ 没有未知/失败项需要重新检测。")
            return

        self._send(chat_id, f"🔍 找到 {count} 条未知/失败ID，正在启动重新检测…")

        tab.var_input.set(unknown_txt)
        tab._stop_evt.clear()
        tab._set_btn_state(True)
        tab._set_progress("讀取ID中…")
        tab._set_counts(0, 0)

        out_path = tab.var_out.get().strip() or str(output_dir / "xianyu_已检测.txt")
        out_fail_path = tab.var_out_fail.get().strip() or str(output_dir / "xianyu_多次失败.txt")

        def _run():
            try:
                tab._run_check(unknown_txt, out_path, out_fail_path)
            except Exception as e:
                tab.log(f"崩溃：{e}")
                tab._set_progress(f"崩溃：{e}")
            finally:
                tab._set_btn_state(False)

        tab._worker = threading.Thread(target=_run, daemon=True)
        tab._worker.start()

        self._send(chat_id, f"▶️ 已启动闲鱼未知项重新检测！共 {count} 条ID")
        self.on_log(f"[TG-OPS] 闲鱼未知项重检启动: {count} 条")

        threading.Thread(
            target=self._cloud_progress_monitor,
            args=(chat_id, tab, "閒魚(重检未知)", "goofish"),
            daemon=True,
        ).start()

    def _cloud_progress_monitor(self, chat_id: str, tab, label: str, check_type: str = "mercari") -> None:
        """后台线程：定期把检测进度同步到 TG"""
        last_done = -1
        last_report_time = time.time()
        report_interval = 30  # 每30秒汇报一次进度

        # 等检测线程真正开始（total > 0）
        for _ in range(30):
            time.sleep(1)
            try:
                total = tab.var_total.get()
                if total > 0:
                    break
            except Exception:
                pass

        while True:
            time.sleep(5)
            try:
                worker = tab._worker
                if not worker or not worker.is_alive():
                    # 检测结束，发送最终结果
                    done = tab.var_done.get()
                    total = tab.var_total.get()
                    progress_text = tab.var_progress.get()
                    if check_type == "goofish":
                        kb = self.tg.make_keyboard([
                            [{"text": "🔄 重新检测未知项", "callback_data": "goof:recheck_unknown"}],
                            [{"text": "🔢 编号下架删除", "callback_data": "goof:idops"}],
                            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
                        ])
                    else:
                        kb = self.tg.make_keyboard([
                            [{"text": "🔄 重新检测未知项", "callback_data": "merc:recheck_unknown"}],
                            [{"text": "🔢 编号下架删除", "callback_data": "merc:idops"}],
                            [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
                        ])
                    self._send(
                        chat_id,
                        f"✅ {label}检测完成！\n"
                        f"已完成：{done}/{total}\n"
                        f"状态：{progress_text}",
                        reply_markup=kb,
                    )
                    self.on_log(f"[TG-OPS] {label}检测完成: {done}/{total}")
                    return

                done = tab.var_done.get()
                total = tab.var_total.get()
                now = time.time()

                # 每 report_interval 秒汇报一次，或者进度有明显变化
                if done != last_done and (now - last_report_time >= report_interval):
                    pct = f"{done * 100 // total}%" if total > 0 else "0%"
                    self._send(
                        chat_id,
                        f"⏳ {label}检测中… {done}/{total} ({pct})",
                    )
                    last_done = done
                    last_report_time = now
            except Exception:
                pass

    def _cloud_download(self, chat_id: str, check_type: str) -> None:
        """下载该用户的条码数据为 CSV"""
        owner = str(chat_id)
        label = "煤爐" if check_type == "mercari" else "閒魚"

        self._send(chat_id, f"⏳ 正在生成{label}数据文件...")

        try:
            resp = requests.get(
                f"{PRODUCT_QUERY_WORKER_URL}/api/export",
                params={"owner": owner, "type": check_type},
                timeout=60,
            )
            if resp.status_code != 200:
                self._send(chat_id, f"⚠️ 下载失败：HTTP {resp.status_code}")
                return
        except Exception as e:
            self._send(chat_id, f"⚠️ 下载失败：{e}")
            return

        # 保存到临时文件并发送
        import tempfile
        filename = f"{label}数据_{owner}.csv"
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        try:
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            self._send_file(chat_id, tmp_path, filename,
                            f"📥 {label}数据（{owner}）")
        except Exception as e:
            self._send(chat_id, f"⚠️ 发送文件失败：{e}")
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def _send_file(self, chat_id: str, file_path: str,
                   file_name: str, caption: str = "") -> None:
        """通过 TG 发送文件"""
        try:
            with open(file_path, "rb") as f:
                import requests as _req
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                url = f"https://api.telegram.org/bot{self.tg.token}/sendDocument"
                _req.post(url, data=data,
                          files={"document": (file_name, f)},
                          timeout=60)
        except Exception as e:
            self.on_log(f"[TG-OPS] 发送文件失败: {e}")

    # ==================================================================
    # 编码数据更新（上传 Excel 到云端 D1）
    # ==================================================================

    def _cmd_pupload(self, chat_id: str) -> None:
        # 用 TG chat_id 作为 owner（区分使用人）
        owner = str(chat_id)

        with self._session_lock:
            sess = UserSession(
                step=SessionStep.PUPLOAD_WAIT_FILE,
                created_at=time.time(),
            )
            sess.doc_target_account = owner  # 复用字段存 owner
            self._sessions[chat_id] = sess

        # 先查一下该 owner 当前数据量
        count_text = ""
        try:
            resp = requests.get(
                f"{PRODUCT_QUERY_WORKER_URL}/api/stats",
                timeout=10,
            )
            stats = resp.json()
            for item in stats.get("by_owner", []):
                if item.get("owner") == owner:
                    count_text = f"\n当前数据：{item['count']} 条"
                    break
        except Exception:
            pass

        # 获取用户名用于显示
        users = self.tg.get_registered_users()
        user_info = users.get(chat_id, {})
        display_name = user_info.get("name", chat_id)

        self._send(
            chat_id,
            f"📤 编码数据更新\n"
            f"身份：{display_name}（{owner}）{count_text}\n\n"
            f"请直接发送 Excel 文件（.xlsx）\n"
            f"格式：商品條碼 | 商品編號 | 帳號（3列）\n\n"
            f"上传后将替换你的所有旧数据。\n"
            f"发送 /cancel 取消",
        )

    def _handle_pupload_file(self, chat_id: str, file_path: str,
                             file_name: str) -> None:
        """处理编码数据更新的文件上传"""
        sess = self._get_session(chat_id)
        if not sess or sess.step != SessionStep.PUPLOAD_WAIT_FILE:
            return

        owner = sess.doc_target_account or chat_id

        with self._session_lock:
            self._sessions.pop(chat_id, None)

        if not file_name.lower().endswith(".xlsx"):
            self._send(chat_id, "⚠️ 只支持 .xlsx 格式，请重新上传。")
            return

        self._send(chat_id, f"⏳ 正在解析 {file_name}...")

        try:
            wb = openpyxl.load_workbook(file_path, read_only=True)
            ws = wb.active
            headers = [str(c.value or "").strip().lower() for c in next(ws.iter_rows(min_row=1, max_row=1))]
            col_map = {}
            for i, h in enumerate(headers):
                if h in ("商品條碼", "商品条码", "條碼", "条码", "barcode"):
                    col_map["barcode"] = i
                elif h in ("商品編號", "商品编号", "編號", "编号", "product_code", "商品編碼", "商品编码"):
                    col_map["product_code"] = i
                elif h in ("帳號", "帐号", "賬號", "账号", "account"):
                    col_map["account"] = i

            rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                bc = str(row[col_map.get("barcode", 0)] or "").strip()
                pc = str(row[col_map.get("product_code", 1)] or "").strip()
                acc = str(row[col_map.get("account", 2)] or "").strip()
                if bc or pc:
                    rows.append({"barcode": bc, "product_code": pc, "account": acc})
            wb.close()

            if not rows:
                self._send(chat_id, "⚠️ Excel 无有效数据")
                return

            total = len(rows)
            self._send(chat_id, f"📊 解析完成：{total} 条记录，开始分批上传...")

            BATCH = 10000
            inserted = 0
            for i in range(0, total, BATCH):
                batch = rows[i:i + BATCH]
                clear = (i == 0)
                resp = requests.post(
                    f"{PRODUCT_QUERY_WORKER_URL}/api/upload-batch",
                    json={"token": PRODUCT_QUERY_UPLOAD_TOKEN, "owner": owner, "rows": batch, "clear": clear},
                    timeout=60,
                )
                result = resp.json()
                if not result.get("ok"):
                    self._send(chat_id, f"⚠️ 上传失败：{result.get('error', '未知错误')}")
                    return
                inserted += result.get("inserted", 0)

        except Exception as e:
            self._send(chat_id, f"⚠️ 上传失败：{e}")
            return

        if inserted > 0:
            kb = self.tg.make_keyboard([
                [{"text": "🔎 商品查询", "callback_data": "ops:pquery"}],
                [{"text": "🏠 主菜单", "callback_data": "ops:home"}],
            ])
            self._send(
                chat_id,
                f"✅ 上传成功！\n"
                f"身份：{owner}\n"
                f"更新：{inserted} 条记录",
                reply_markup=kb,
            )
            self.on_log(f"[TG-OPS] 编码数据更新: {owner} 上传 {file_name}, {inserted} 条")
        else:
            self._send(chat_id, f"⚠️ 上传失败：{result.get('error', '未知错误')}")