"""TG 管理指令处理器

处理监控、批量操作、自动刊登等管理相关 TG 指令和 inline keyboard 按钮交互。
支持：系统状态、账号管理、监控控制、批量上下架删除、根据编号操作、
      自动刊登、设置修改、文档上传处理等。
"""
from __future__ import annotations

import os
import re
import time
import threading
import asyncio
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.accounts import load_accounts, load_settings, save_settings
from core.ai_forwarder_feature import (
    _HARDCODED_API_KEY, _HARDCODED_BASE_URL,
    _HARDCODED_ENDPOINT, _HARDCODED_MODEL,
    call_openai,
)


# ---------- 文本清理 ----------

def _sanitize(text: str) -> str:
    """清理 TG 输入中的不可见/特殊 Unicode 字符。"""
    text = re.sub(r'[\ufffc\ufeff\u200b\u200c\u200d\u2060\u00a0]', '', text)
    text = re.sub(r'[^\S \t\n\r]+', '', text)
    return text.strip()


# ---------- 会话状态 ----------

class SessionStep(Enum):
    IDLE = auto()
    # 批量操作
    BATCH_WAIT_MODE = auto()
    BATCH_WAIT_ACCOUNTS = auto()
    BATCH_WAIT_REPEAT = auto()
    BATCH_WAIT_CONFIRM = auto()
    # 编号操作
    IDOPS_WAIT_ACCOUNTS = auto()
    IDOPS_WAIT_CONFIRM = auto()
    # 自动刊登
    PUBLISH_WAIT_ACCOUNTS = auto()
    PUBLISH_WAIT_CONFIRM = auto()
    # 设置修改
    SETTINGS_WAIT_FIELD = auto()
    SETTINGS_WAIT_VALUE = auto()
    # 文档上传等待指令
    DOC_WAIT_ACTION = auto()
    DOC_WAIT_ACCOUNT = auto()  # 等待选择账号（txt→ids/需要按账号命名）
    # 远程登录
    LOGIN_WAIT_ACCOUNT = auto()
    LOGIN_WAIT_USERNAME = auto()
    LOGIN_WAIT_PASSWORD = auto()
    LOGIN_WAIT_CAPTCHA = auto()   # 等待验证码
    LOGIN_WAIT_NEW_NAME = auto()  # 等待输入新账号名称
    # 拆分 Excel
    SPLIT_WAIT_COUNTS = auto()     # 等待输入每个帐号的数量
    SPLIT_WAIT_CONFIRM = auto()    # 等待确认拆分
    # 定期刊登
    SCHED_WAIT_TIME = auto()       # 等待输入时间
    SCHED_WAIT_ASSIGN = auto()     # 等待输入分配方案
    SCHED_WAIT_CONFIRM = auto()    # 等待确认分配方案
    SCHED_WAIT_DEL = auto()        # 等待选择删除编号


@dataclass
class UserSession:
    step: SessionStep = SessionStep.IDLE
    # 批量操作参数
    batch_mode: str = ""
    batch_accounts: List[str] = field(default_factory=list)
    batch_repeat: int = 1
    # 编号操作参数
    idops_accounts: List[str] = field(default_factory=list)
    # 自动刊登参数
    publish_accounts: List[str] = field(default_factory=list)
    # 设置修改
    setting_field: str = ""
    # 文档上传
    doc_path: str = ""
    doc_name: str = ""
    doc_target_account: str = ""  # 目标账号名（用于 ids/ 文件命名）
    doc_target_pid: str = ""     # 目标 profile_id
    # 远程登录
    login_pid: str = ""           # 选中的 profile_id
    login_acc_name: str = ""      # 账号名
    login_username: str = ""      # Yahoo 用户名
    login_session: Any = None     # RemoteLoginSession 实例
    # 拆分 Excel
    split_accounts: List[str] = field(default_factory=list)  # 选中的帐号
    split_counts: List[Tuple[str, int]] = field(default_factory=list)  # [(帐号, 数量)]
    split_total: int = 0  # test.xlsx 总行数
    # 定期刊登
    sched_time: str = ""           # 逗号分隔的多个时间
    sched_accounts: List[str] = field(default_factory=list)
    sched_assign: str = ""         # 暂存分配方案（确认前）
    created_at: float = 0.0


SESSION_TIMEOUT = 1800  # 30分钟

ROOT_DIR = Path(__file__).resolve().parent.parent


# ---------- 主类 ----------

class ManageCommandHandler:

    def __init__(self, tg_manage_bot, on_log: Callable[[str], None],
                 app=None):
        self.tg = tg_manage_bot
        self.on_log = on_log
        self.app = app  # GUI App 实例（可选）
        self._sessions: Dict[str, UserSession] = {}
        self._session_lock = threading.Lock()
        # 定期刊登调度器
        self._sched_fired: set = set()
        self._sched_thread = threading.Thread(
            target=self._schedule_loop, daemon=True)
        self._sched_thread.start()

    def _sync_gui_schedule(self):
        """TG 修改定期计划后，同步刷新 GUI 面板。"""
        if not self.app:
            return
        try:
            from core.accounts import load_settings
            self.app.settings["publish_schedule"] = load_settings().get("publish_schedule", [])
            pt = getattr(self.app, "publish_tab", None)
            if pt:
                self.app.after(0, pt._sched_refresh)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 公共入口：处理 TG 消息
    # ------------------------------------------------------------------

    def handle_message(self, text: str, message_id: int, chat_id: str) -> bool:
        t = _sanitize(text)

        # 指令分发
        if t == "/status":
            self._cmd_status(chat_id)
            return True
        if t == "/accounts":
            self._cmd_accounts(chat_id)
            return True
        if t == "/startmon":
            self._cmd_startmon(chat_id)
            return True
        if t == "/stopmon":
            self._cmd_stopmon(chat_id)
            return True
        if t == "/batch":
            self._cmd_batch(chat_id)
            return True
        if t == "/stopbatch":
            self._cmd_stopbatch(chat_id)
            return True
        if t == "/idops":
            self._cmd_idops(chat_id)
            return True
        if t == "/publish":
            self._cmd_publish(chat_id)
            return True
        if t == "/stoppublish":
            self._cmd_stoppublish(chat_id)
            return True
        if t == "/settings":
            self._cmd_settings(chat_id)
            return True
        if t == "/cancel":
            self._cancel_session(chat_id)
            return True
        if t == "/login":
            self._cmd_login(chat_id)
            return True
        if t == "/split":
            self._cmd_split(chat_id)
            return True
        if t == "/schedule":
            self._cmd_schedule(chat_id)
            return True
        if t.startswith("/logs"):
            self._cmd_logs(chat_id, t)
            return True

        # 会话中的后续输入
        with self._session_lock:
            has_session = chat_id in self._sessions
        if has_session:
            self._handle_session_input(chat_id, t)
            return True

        return False

    # ------------------------------------------------------------------
    # 公共入口：处理 inline keyboard 按钮回调
    # ------------------------------------------------------------------

    def handle_callback(self, data: str, cb_id: str,
                        chat_id: str, message_id: int) -> None:
        parts = data.split(":", 2)
        prefix = parts[0] if parts else ""

        if prefix == "mgr":
            self._handle_mgr_cb(chat_id, parts)
        elif prefix == "batch":
            self._handle_batch_cb(chat_id, parts)
        elif prefix == "idops":
            self._handle_idops_cb(chat_id, parts)
        elif prefix == "pub":
            self._handle_publish_cb(chat_id, parts)
        elif prefix == "set":
            self._handle_settings_cb(chat_id, parts)
        elif prefix == "acc":
            self._handle_acc_cb(chat_id, parts)
        elif prefix == "doc":
            self._handle_doc_cb(chat_id, parts)
        elif prefix == "login":
            self._handle_login_cb(chat_id, parts)
        elif prefix == "split":
            self._handle_split_cb(chat_id, parts)
        elif prefix == "sched":
            self._handle_sched_cb(chat_id, parts)

    # ------------------------------------------------------------------
    # 公共入口：处理文档上传
    # ------------------------------------------------------------------

    def handle_document(self, file_path: str, file_name: str,
                        caption: str, chat_id: str) -> None:
        self._handle_uploaded_doc(chat_id, file_path, file_name, caption)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _send(self, chat_id: str, text: str,
              reply_markup: Optional[Dict] = None) -> None:
        try:
            self.tg.send_to(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            self.on_log(f"[TG-MANAGE] 发送失败: {e}")

    def _send_main_menu(self, chat_id: str) -> None:
        """v6.0.75:動態主菜單 — 帶系統狀態欄 + 重新分組,常用功能優先。
        失敗時 fallback 到 tg.bot 內建的舊版菜單(保證可用性)。
        """
        try:
            text = self._build_main_menu_text()
            kb = self._build_main_menu_kb()
            self.tg.send_to(chat_id, text, reply_markup=kb)
        except Exception as e:
            self.on_log(f"[TG-MANAGE] 動態主菜單異常,退回舊版: {e}")
            self.tg._send_main_menu(chat_id)

    def _build_main_menu_text(self) -> str:
        """組裝主菜單頂部狀態文字 — 一眼看系統運作狀況。"""
        app = self.app
        lines = ["🔧 【管理控制台】\n"]
        if not app:
            lines.append("⚠️ 應用未連接")
            return "\n".join(lines)

        # 系統狀態
        mon = "✅" if getattr(app, "monitoring", False) else "⏸"
        bat = "🔄" if getattr(app, "merch_running", False) else "⏸"
        pub_running = False
        try:
            pub_tab = getattr(app, "publish_tab", None)
            if pub_tab:
                fut = getattr(pub_tab, "_future", None)
                if fut is not None and not fut.done():
                    pub_running = True
        except Exception:
            pass
        pub = "🔄" if pub_running else "⏸"
        lines.append(f"📡 監控 {mon}  |  📦 批量 {bat}  |  📝 刊登 {pub}")

        # 帳號統計
        try:
            states = getattr(app, "states", {}) or {}
            total = len(states)
            online = sum(1 for st in states.values()
                         if getattr(st, "status", "") not in ("離線", "离线", "未启动", "未啟動", ""))
            selected = sum(1 for st in states.values() if getattr(st, "selected", False))
        except Exception:
            total = online = selected = 0
        lines.append(f"👥 帳號 {online}/{total} 在線  |  ✓ {selected} 選中")

        # 排程
        try:
            from core.accounts import load_settings
            sch = load_settings().get("publish_schedule", []) or []
            sch_count = len(sch)
        except Exception:
            sch_count = 0
        if sch_count:
            lines.append(f"📅 排程任務:{sch_count} 條")

        lines.append("")
        lines.append("選擇功能:")
        return "\n".join(lines)

    def _build_main_menu_kb(self) -> Dict:
        """重新分組的主菜單按鈕 — 全部保留舊版 callback_data 不破壞向下相容。"""
        return self.tg.make_keyboard([
            # ━ 監控操作(最常用)━
            [
                {"text": "📊 系統狀態", "callback_data": "mgr:status"},
                {"text": "👥 帳號列表", "callback_data": "mgr:accounts"},
            ],
            [
                {"text": "▶️ 啟動監控", "callback_data": "mgr:startmon"},
                {"text": "⏹ 停止監控", "callback_data": "mgr:stopmon"},
            ],
            # ━ 商品操作 ━
            [
                {"text": "📦 批量操作", "callback_data": "mgr:batch"},
                {"text": "🛑 停止批量", "callback_data": "mgr:stopbatch"},
            ],
            [
                {"text": "🔢 編號下架刪除", "callback_data": "mgr:idops"},
                {"text": "📝 自動刊登", "callback_data": "mgr:publish"},
            ],
            [
                {"text": "📅 定期刊登", "callback_data": "mgr:schedule"},
                {"text": "⏹ 停止刊登", "callback_data": "mgr:stoppublish"},
            ],
            # ━ 工具 ━
            [
                {"text": "✂️ 拆分 Excel", "callback_data": "mgr:split"},
                {"text": "📋 診斷日誌", "callback_data": "mgr:logs"},
            ],
            [
                {"text": "⚙️ 設置", "callback_data": "mgr:settings"},
                {"text": "🔑 遠程登錄", "callback_data": "mgr:login"},
            ],
            # ━ 其他 ━
            [
                {"text": "❌ 取消當前操作", "callback_data": "mgr:cancel"},
            ],
        ] + self._build_cross_bot_jump_rows())

    def _build_cross_bot_jump_rows(self) -> list:
        """v6.0.76:跨 bot 跳轉 — 主菜單底部一行 url 按鈕,點擊直接切換到其他 bot 對話。"""
        try:
            from core.tg_bot_registry import get_all_jump_buttons
            btns = get_all_jump_buttons(exclude_kind="manage")
            if btns:
                return [btns]
        except Exception:
            pass
        return []

    def _cancel_session(self, chat_id: str) -> None:
        with self._session_lock:
            sess = self._sessions.pop(chat_id, None)
        # 显示具体取消了什么
        if sess:
            step_labels = {
                SessionStep.BATCH_WAIT_MODE: "批量操作",
                SessionStep.BATCH_WAIT_ACCOUNTS: "批量操作",
                SessionStep.BATCH_WAIT_REPEAT: "批量操作",
                SessionStep.BATCH_WAIT_CONFIRM: "批量操作",
                SessionStep.IDOPS_WAIT_ACCOUNTS: "编号操作",
                SessionStep.IDOPS_WAIT_CONFIRM: "编号操作",
                SessionStep.PUBLISH_WAIT_ACCOUNTS: "自动刊登",
                SessionStep.PUBLISH_WAIT_CONFIRM: "自动刊登",
                SessionStep.SETTINGS_WAIT_FIELD: "设置修改",
                SessionStep.SETTINGS_WAIT_VALUE: "设置修改",
                SessionStep.DOC_WAIT_ACTION: "文档上传",
                SessionStep.DOC_WAIT_ACCOUNT: "文档上传",
                SessionStep.LOGIN_WAIT_ACCOUNT: "远程登录",
                SessionStep.LOGIN_WAIT_USERNAME: "远程登录",
                SessionStep.LOGIN_WAIT_PASSWORD: "远程登录",
                SessionStep.LOGIN_WAIT_CAPTCHA: "远程登录",
                SessionStep.LOGIN_WAIT_NEW_NAME: "新增账号",
                SessionStep.SPLIT_WAIT_COUNTS: "拆分Excel",
                SessionStep.SPLIT_WAIT_CONFIRM: "拆分Excel",
            }
            label = step_labels.get(sess.step, "")
            # 如果是登录相关，需要清理浏览器
            if sess.step in (SessionStep.LOGIN_WAIT_USERNAME,
                             SessionStep.LOGIN_WAIT_PASSWORD,
                             SessionStep.LOGIN_WAIT_CAPTCHA):
                self._login_cleanup(chat_id)
            if label:
                self._send(chat_id, f"已取消{label}。")
            else:
                self._send(chat_id, "已取消当前操作。")
        else:
            self._send(chat_id, "当前没有进行中的操作。")
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

        if sess.step == SessionStep.BATCH_WAIT_REPEAT:
            self._batch_step_repeat(chat_id, sess, text)
        elif sess.step == SessionStep.SETTINGS_WAIT_VALUE:
            self._settings_step_value(chat_id, sess, text)
        elif sess.step == SessionStep.DOC_WAIT_ACTION:
            self._doc_step_action(chat_id, sess, text)
        elif sess.step == SessionStep.DOC_WAIT_ACCOUNT:
            self._doc_step_account(chat_id, sess, text)
        elif sess.step == SessionStep.LOGIN_WAIT_USERNAME:
            self._login_step_username(chat_id, sess, text)
        elif sess.step == SessionStep.LOGIN_WAIT_PASSWORD:
            self._login_step_password(chat_id, sess, text)
        elif sess.step == SessionStep.LOGIN_WAIT_CAPTCHA:
            self._login_step_captcha(chat_id, sess, text)
        elif sess.step == SessionStep.LOGIN_WAIT_NEW_NAME:
            self._login_create_account(chat_id, sess, text)
        elif sess.step == SessionStep.SPLIT_WAIT_COUNTS:
            self._split_step_counts(chat_id, sess, text)
        elif sess.step == SessionStep.SCHED_WAIT_TIME:
            self._sched_step_time(chat_id, sess, text)
        elif sess.step == SessionStep.SCHED_WAIT_ASSIGN:
            self._sched_step_assign(chat_id, sess, text)
        elif sess.step == SessionStep.SCHED_WAIT_CONFIRM:
            self._sched_step_confirm(chat_id, sess, text)
        elif sess.step == SessionStep.SCHED_WAIT_DEL:
            self._sched_step_del(chat_id, sess, text)
        elif sess.step == SessionStep.PUBLISH_WAIT_ACCOUNTS and getattr(sess, "publish_field", ""):
            self._handle_publish_input(chat_id, sess, text)

    # ------------------------------------------------------------------
    # 主菜单按钮回调
    # ------------------------------------------------------------------

    def _handle_mgr_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "status":
            self._cmd_status(chat_id)
        elif action == "accounts":
            self._cmd_accounts(chat_id)
        elif action == "startmon":
            self._cmd_startmon(chat_id)
        elif action == "stopmon":
            self._cmd_stopmon(chat_id)
        elif action == "batch":
            self._cmd_batch(chat_id)
        elif action == "stopbatch":
            self._cmd_stopbatch(chat_id)
        elif action == "idops":
            self._cmd_idops(chat_id)
        elif action == "publish":
            self._cmd_publish(chat_id)
        elif action == "stoppublish":
            self._cmd_stoppublish(chat_id)
        elif action == "settings":
            self._cmd_settings(chat_id)
        elif action == "cancel":
            self._cancel_session(chat_id)
        elif action == "login":
            self._cmd_login(chat_id)
        elif action == "split":
            self._cmd_split(chat_id)
        elif action == "schedule":
            self._cmd_schedule(chat_id)
        elif action == "logs":
            self._cmd_logs(chat_id, "/logs")
        elif action == "home":
            self._send_main_menu(chat_id)

    # ------------------------------------------------------------------
    # /status 系统状态总览
    # ------------------------------------------------------------------

    def _cmd_status(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        lines = ["📊【系统状态】"]

        # 监控状态
        mon_running = getattr(app, "monitoring", False)
        lines.append(f"Yahoo 监控: {'✅ 运行中' if mon_running else '⏹ 未启动'}")

        # 批量任务状态
        merch_running = getattr(app, "merch_running", False)
        lines.append(f"批量操作: {'🔄 运行中' if merch_running else '⏹ 未运行'}")

        # 自动刊登状态
        pub_running = False
        pub_tab = getattr(app, "publish_tab", None)
        if pub_tab:
            fut = getattr(pub_tab, "_future", None)
            if fut is not None and not fut.done():
                pub_running = True
        lines.append(f"自动刊登: {'🔄 运行中' if pub_running else '⏹ 未运行'}")

        # 账号信息
        states = getattr(app, "states", {})
        if states:
            lines.append(f"\n👥 账号 ({len(states)}):")
            for pid, st in states.items():
                name = getattr(st, "name", "?")
                status = getattr(st, "status", "离线")
                sel = "✅" if getattr(st, "selected", False) else "  "
                paid = st.last_values.get("paid_to_ship", 0) if hasattr(st, "last_values") else 0
                cod = st.last_values.get("cod", 0) if hasattr(st, "last_values") else 0
                lines.append(f"  {sel} {name} | {status} | 待出货:{paid} 取货付款:{cod}")
        else:
            lines.append("\n暂无账号")

        # 设置摘要
        settings = getattr(app, "settings", {})
        headless = settings.get("headless", True)
        merch_headless = settings.get("merch_headless", False)
        conc = settings.get("concurrency", 3)
        lines.append(f"\n⚙️ 监控无头:{headless} | 批量无头:{merch_headless} | 并发:{conc}")

        kb = self.tg.make_keyboard([
            [
                {"text": "👥 账号列表", "callback_data": "mgr:accounts"},
                {"text": "⚙️ 设置", "callback_data": "mgr:settings"},
            ],
            [
                {"text": "🏠 主菜单", "callback_data": "mgr:home"},
            ],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # /accounts 查看所有账号
    # ------------------------------------------------------------------

    def _cmd_accounts(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        states = getattr(app, "states", {})
        if not states:
            self._send(chat_id, "暂无账号。")
            return

        lines = ["👥【账号列表】"]
        rows = []
        for i, (pid, st) in enumerate(states.items()):
            name = getattr(st, "name", "?")
            status = getattr(st, "status", "离线")
            sel = "✅" if getattr(st, "selected", False) else "⬜"
            susp = " ⛔停权" if getattr(st, "suspended", False) else ""
            err = getattr(st, "last_error", "")
            err_hint = f" ❗{err[:20]}" if err else ""
            # 最后变化时间
            ts = getattr(st, "last_change_ts", 0)
            if ts:
                import time as _t
                ts_str = _t.strftime("%m-%d %H:%M", _t.localtime(ts))
                ts_hint = f" ({ts_str})"
            else:
                ts_hint = ""
            lines.append(f"  {i+1}. {sel} {name} | {status}{susp}{err_hint}{ts_hint}")
            # 勾选/取消勾选按钮
            if getattr(st, "selected", False):
                rows.append([{"text": f"⬜ 取消选择 {name}", "callback_data": f"acc:desel:{pid}"}])
            else:
                rows.append([{"text": f"✅ 选择 {name}", "callback_data": f"acc:sel:{pid}"}])

        lines.append(f"\n共 {len(states)} 个账号（✅=已选择）")
        lines.append("选择的账号将用于批量操作/刊登等")

        rows.append([
            {"text": "✅ 全选", "callback_data": "acc:selall"},
            {"text": "⬜ 全不选", "callback_data": "acc:deselall"},
        ])
        rows.append([{"text": "🏠 主菜单", "callback_data": "mgr:home"}])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # 账号选择按钮回调
    # ------------------------------------------------------------------

    def _handle_acc_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        app = self.app
        if not app:
            return

        states = getattr(app, "states", {})

        if action == "sel" and len(parts) > 2:
            pid = parts[2]
            st = states.get(pid)
            if st:
                st.selected = True
                self._send(chat_id, f"✅ 已选择: {st.name}")
                self._cmd_accounts(chat_id)
        elif action == "desel" and len(parts) > 2:
            pid = parts[2]
            st = states.get(pid)
            if st:
                st.selected = False
                self._send(chat_id, f"⬜ 已取消选择: {st.name}")
                self._cmd_accounts(chat_id)
        elif action == "selall":
            for st in states.values():
                st.selected = True
            self._send(chat_id, "✅ 已全选")
            self._cmd_accounts(chat_id)
        elif action == "deselall":
            for st in states.values():
                st.selected = False
            self._send(chat_id, "⬜ 已全部取消选择")
            self._cmd_accounts(chat_id)

    # ------------------------------------------------------------------
    # /startmon 启动 Yahoo 监控
    # ------------------------------------------------------------------

    def _cmd_startmon(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "monitoring", False):
            self._send(chat_id, "Yahoo 监控已在运行中。")
            return

        try:
            app.after(0, app._start_monitor)
            self._send(chat_id, "✅ 已发送启动监控指令。")
            self.on_log("[TG-MANAGE] 远程启动 Yahoo 监控")
        except Exception as e:
            self._send(chat_id, f"启动失败: {e}")

    # ------------------------------------------------------------------
    # /stopmon 停止 Yahoo 监控
    # ------------------------------------------------------------------

    def _cmd_stopmon(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if not getattr(app, "monitoring", False):
            self._send(chat_id, "Yahoo 监控当前未运行。")
            return

        try:
            app.after(0, app._stop_monitor)
            self._send(chat_id, "⏹ 已发送停止监控指令。")
            self.on_log("[TG-MANAGE] 远程停止 Yahoo 监控")
        except Exception as e:
            self._send(chat_id, f"停止失败: {e}")

    # ------------------------------------------------------------------
    # /batch 批量操作（上架/下架/刪除）
    # ------------------------------------------------------------------

    def _cmd_batch(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "merch_running", False):
            self._send(chat_id, "批量操作已在运行中。\n发送 /stopbatch 可停止。")
            return

        kb = self.tg.make_keyboard([
            [
                {"text": "下架", "callback_data": "batch:mode:下架"},
                {"text": "上架", "callback_data": "batch:mode:上架"},
            ],
            [
                {"text": "刪除", "callback_data": "batch:mode:刪除"},
            ],
            [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
        ])
        self._send(chat_id, "📦【批量操作】请选择模式：", reply_markup=kb)

    def _handle_batch_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if action == "mode":
            self._batch_select_mode(chat_id, arg)
        elif action == "confirm":
            self._batch_confirm(chat_id)

    def _batch_select_mode(self, chat_id: str, mode: str) -> None:
        if mode not in ("上架", "下架", "刪除"):
            self._send(chat_id, "无效模式。")
            return

        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.BATCH_WAIT_REPEAT,
                batch_mode=mode,
                created_at=time.time(),
            )

        settings = getattr(self.app, "settings", {})
        default_repeat = settings.get("merch_repeat", 2)
        self._send(
            chat_id,
            f"模式：{mode}\n"
            f"请输入执行次数（默认 {default_repeat}）：\n"
            f"直接回复数字，或发送 /cancel 取消",
        )

    def _batch_step_repeat(self, chat_id: str, sess: UserSession, text: str) -> None:
        settings = getattr(self.app, "settings", {})
        default_repeat = settings.get("merch_repeat", 2)
        try:
            repeat = int(text.strip()) if text.strip() else default_repeat
            repeat = max(1, repeat)
        except ValueError:
            self._send(chat_id, "请输入有效数字：")
            return

        sess.batch_repeat = repeat

        # 获取已选账号
        states = getattr(self.app, "states", {})
        selected = [st for st in states.values() if getattr(st, "selected", False)]
        if not selected:
            self._send(chat_id, "⚠️ 没有选择账号。请先用 /accounts 选择账号。")
            del self._sessions[chat_id]
            return

        names = ", ".join(st.name for st in selected)
        headless = settings.get("merch_headless", False)

        kb = self.tg.make_keyboard([
            [{"text": "✅ 确认执行", "callback_data": "batch:confirm"}],
            [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
        ])
        self._send(
            chat_id,
            f"📦【批量操作确认】\n"
            f"模式：{sess.batch_mode}\n"
            f"次数：{repeat}\n"
            f"无头：{headless}\n"
            f"账号：{names}\n\n"
            f"确认执行？",
            reply_markup=kb,
        )
        sess.step = SessionStep.BATCH_WAIT_CONFIRM

    def _batch_confirm(self, chat_id: str) -> None:
        with self._session_lock:
            sess = self._sessions.pop(chat_id, None)
        if not sess or sess.step != SessionStep.BATCH_WAIT_CONFIRM:
            self._send(chat_id, "没有待确认的批量操作。")
            return

        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "merch_running", False):
            self._send(chat_id, "批量操作已在运行中。")
            return

        # 通过 GUI 设置模式和次数，然后触发启动
        try:
            settings = getattr(app, "settings", {})
            settings["merch_mode"] = sess.batch_mode
            settings["merch_repeat"] = sess.batch_repeat
            save_settings(settings)

            # 更新 GUI 变量（如果存在）
            if hasattr(app, "var_merch_mode"):
                app.after(0, lambda: app.var_merch_mode.set(sess.batch_mode))
            if hasattr(app, "var_merch_repeat"):
                app.after(0, lambda: app.var_merch_repeat.set(str(sess.batch_repeat)))

            app.after(100, app._start_merch_batch)
            self._send(chat_id, f"✅ 批量操作已启动: {sess.batch_mode} x{sess.batch_repeat}")
            self.on_log(f"[TG-MANAGE] 远程启动批量: {sess.batch_mode} x{sess.batch_repeat}")
        except Exception as e:
            self._send(chat_id, f"批量启动失败: {e}")

    # ------------------------------------------------------------------
    # /stopbatch 停止批量操作
    # ------------------------------------------------------------------

    def _cmd_stopbatch(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        if not getattr(app, "merch_running", False):
            self._send(chat_id, "批量操作当前未运行。")
            return
        try:
            app.after(0, app._stop_merch_batch)
            self._send(chat_id, "🛑 已发送停止批量操作指令。")
            self.on_log("[TG-MANAGE] 远程停止批量操作")
        except Exception as e:
            self._send(chat_id, f"停止批量失败: {e}")

    # ------------------------------------------------------------------
    # /idops 根据商品编号下架删除
    # ------------------------------------------------------------------

    def _cmd_idops(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        if getattr(app, "merch_running", False):
            self._send(chat_id, "批量操作已在运行中，请先停止。")
            return

        # 检查 ids/ 目录
        ids_dir = ROOT_DIR / "ids"
        if not ids_dir.exists():
            ids_dir.mkdir(parents=True, exist_ok=True)

        # 列出已有的 ids 文件
        files = sorted(ids_dir.glob("*.txt"))
        states = getattr(app, "states", {})
        selected = [st for st in states.values()
                    if getattr(st, "selected", False)]

        if not selected:
            self._send(chat_id,
                       "⚠️ 没有选择账号。\n"
                       "请先用 /accounts 选择账号。")
            return

        lines = ["🔢【根据商品编号下架删除】"]
        lines.append(f"已选账号: {', '.join(st.name for st in selected)}")

        if files:
            lines.append(f"\nids/ 目录已有 {len(files)} 个文件:")
            for f in files[:10]:
                lines.append(f"  {f.name}")
        else:
            lines.append("\nids/ 目录暂无文件")

        lines.append("\n每个账号需要对应的 txt 文件（文件名=账号名或ProfileID）")
        lines.append("可直接发送 txt 文件到此 Bot")

        kb = self.tg.make_keyboard([
            [{"text": "✅ 开始执行", "callback_data": "idops:confirm"}],
            [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_idops_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "confirm":
            self._idops_confirm(chat_id)

    def _idops_confirm(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        if getattr(app, "merch_running", False):
            self._send(chat_id, "批量操作已在运行中。")
            return
        try:
            settings = getattr(app, "settings", {})
            settings["merch_mode"] = "根據商品編號下架刪除"
            save_settings(settings)
            if hasattr(app, "var_merch_mode"):
                app.after(0, lambda: app.var_merch_mode.set("根據商品編號下架刪除"))
            app.after(100, app._start_merch_batch)
            self._send(chat_id, "✅ 根据商品编号下架删除已启动")
            self.on_log("[TG-MANAGE] 远程启动编号下架删除")
        except Exception as e:
            self._send(chat_id, f"编号操作启动失败: {e}")

    # ------------------------------------------------------------------
    # /publish 启动自动刊登
    # ------------------------------------------------------------------

    def _cmd_publish(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        pub_tab = getattr(app, "publish_tab", None)
        if not pub_tab:
            self._send(chat_id, "⚠️ 自动刊登模块未加载。")
            return

        fut = getattr(pub_tab, "_future", None)
        if fut is not None and not fut.done():
            self._send(chat_id, "自动刊登已在运行中。\n发送 /stoppublish 可停止。")
            return

        # 检查 publish_excels 目录
        pub_dir = ROOT_DIR / "publish_excels"
        if not pub_dir.exists():
            pub_dir.mkdir(parents=True, exist_ok=True)

        files = sorted([
            p for p in pub_dir.glob("*.xlsx")
            if not p.name.startswith("~$") and not p.stem.endswith("_done") and p.stem != "test" and p.name != "成功汇总.xlsx"
        ])

        # 读取当前参数
        conc = pub_tab.var_conc.get().strip() or "3"
        step_delay = (getattr(pub_tab, "var_step_delay", None) or type("", (), {"get": lambda s: "0.4"})()).get().strip() or "0.4"
        headless = bool(getattr(pub_tab, "var_headless", type("", (), {"get": lambda s: False})()).get())

        lines = ["📝【自动刊登】"]
        if files:
            lines.append(f"publish_excels/ 目录有 {len(files)} 个 Excel:")
            for f in files[:10]:
                lines.append(f"  {f.name}")
        else:
            lines.append("publish_excels/ 目录暂无 Excel 文件")
            lines.append("请先上传 Excel 文件到该目录或通过 TG 发送")

        lines.append("")
        lines.append(f"⚙️ 当前参数：")
        lines.append(f"  并发数: {conc}")
        lines.append(f"  每步延迟: {step_delay}s")
        lines.append(f"  无头模式: {'是' if headless else '否'}")

        kb = self.tg.make_keyboard([
            [
                {"text": f"并发:{conc}", "callback_data": "pub:setconc"},
                {"text": f"延迟:{step_delay}s", "callback_data": "pub:setdelay"},
            ],
            [
                {"text": f"无头:{'开' if headless else '关'}", "callback_data": "pub:headless"},
            ],
            [{"text": "✅ 开始刊登", "callback_data": "pub:confirm"}],
            [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_publish_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "confirm":
            self._publish_confirm(chat_id)
        elif action == "headless":
            pub_tab = getattr(self.app, "publish_tab", None) if self.app else None
            if not pub_tab:
                self._send(chat_id, "⚠️ 自动刊登模块未加载。")
                return
            cur = bool(pub_tab.var_headless.get())
            pub_tab.var_headless.set(not cur)
            self._cmd_publish(chat_id)  # 刷新显示
        elif action == "setconc":
            with self._session_lock:
                sess = UserSession(created_at=time.time())
                sess.step = SessionStep.PUBLISH_WAIT_ACCOUNTS  # 复用此步骤等待输入
                sess.publish_field = "conc"
                self._sessions[chat_id] = sess
            self._send(chat_id, "请输入并发数（1~10）：")
        elif action == "setdelay":
            with self._session_lock:
                sess = UserSession(created_at=time.time())
                sess.step = SessionStep.PUBLISH_WAIT_ACCOUNTS
                sess.publish_field = "delay"
                self._sessions[chat_id] = sess
            self._send(chat_id, "请输入每步延迟（秒，如 0.4）：")

    def _handle_publish_input(self, chat_id: str, sess, text: str) -> None:
        field = getattr(sess, "publish_field", "")
        pub_tab = getattr(self.app, "publish_tab", None) if self.app else None
        if not pub_tab:
            self._send(chat_id, "⚠️ 自动刊登模块未加载。")
            return
        text = text.strip()
        if field == "conc":
            try:
                val = int(text)
                val = max(1, min(10, val))
            except ValueError:
                self._send(chat_id, "请输入数字（1~10）")
                return
            pub_tab.var_conc.set(str(val))
        elif field == "delay":
            try:
                val = float(text)
                if val < 0:
                    val = 0.0
            except ValueError:
                self._send(chat_id, "请输入数字（如 0.4）")
                return
            pub_tab.var_step_delay.set(str(val))
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._cmd_publish(chat_id)  # 刷新显示

    def _publish_confirm(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        pub_tab = getattr(app, "publish_tab", None)
        if not pub_tab:
            self._send(chat_id, "⚠️ 自动刊登模块未加载。")
            return
        try:
            app.after(0, pub_tab.start)
            self._send(chat_id, "✅ 自动刊登已启动")
            self.on_log("[TG-MANAGE] 远程启动自动刊登")
        except Exception as e:
            self._send(chat_id, f"刊登启动失败: {e}")

    # ------------------------------------------------------------------
    # /stoppublish 停止自动刊登
    # ------------------------------------------------------------------

    def _cmd_stoppublish(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return
        pub_tab = getattr(app, "publish_tab", None)
        if not pub_tab:
            self._send(chat_id, "⚠️ 自动刊登模块未加载。")
            return
        fut = getattr(pub_tab, "_future", None)
        if fut is None or fut.done():
            self._send(chat_id, "自动刊登当前未运行。")
            return
        try:
            app.after(0, pub_tab.stop)
            self._send(chat_id, "🛑 已发送停止自动刊登指令。")
            self.on_log("[TG-MANAGE] 远程停止自动刊登")
        except Exception as e:
            self._send(chat_id, f"停止刊登失败: {e}")

    # ------------------------------------------------------------------
    # /split 拆分 test.xlsx
    # ------------------------------------------------------------------

    def _cmd_split(self, chat_id: str) -> None:
        import openpyxl as _xl

        pub_dir = ROOT_DIR / "publish_excels"
        source = pub_dir / "test.xlsx"
        if not source.exists():
            self._send(chat_id, "⚠️ publish_excels/test.xlsx 不存在。")
            return

        try:
            wb = _xl.load_workbook(source, read_only=True)
            total = wb.active.max_row - 1
            wb.close()
        except Exception as e:
            self._send(chat_id, f"读取 test.xlsx 失败: {e}")
            return

        if total <= 0:
            self._send(chat_id, "test.xlsx 没有数据行。")
            return

        accounts = load_accounts()
        if not accounts:
            self._send(chat_id, "⚠️ 没有账号。")
            return

        acc_names = [a.get("name", "") for a in accounts if a.get("name")]

        with self._session_lock:
            sess = UserSession(created_at=time.time())
            sess.step = SessionStep.SPLIT_WAIT_COUNTS
            sess.split_accounts = acc_names  # 所有账号备用
            sess.split_total = total
            self._sessions[chat_id] = sess

        lines = [f"📂 test.xlsx 共 {total} 条数据",
                 "",
                 "📋 可用账号："]
        for i, name in enumerate(acc_names, 1):
            lines.append(f"  {i}. {name}")
        lines.append("")
        lines.append("请输入 编号:数量，多个用逗号分隔")
        lines.append(f"例如：1:200,2:200,3:150")
        lines.append("")
        lines.append("发送 /cancel 取消")

        self._send(chat_id, "\n".join(lines))

    def _handle_split_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        sess = self._get_session(chat_id)

        if action == "confirm":
            if not sess or not sess.split_counts:
                self._send(chat_id, "会话已过期，请重新 /split")
                return
            self._split_execute(chat_id, sess)

    def _split_step_counts(self, chat_id: str, sess: UserSession, text: str) -> None:
        text = text.strip()
        assignments = []
        acc_names = sess.split_accounts  # 完整账号列表（带编号）

        # 解析 "编号:数量" 或 "账号:数量" 格式
        for part in re.split(r'[,，\s]+', text):
            part = part.strip()
            if not part:
                continue
            if ':' in part or '：' in part:
                kv = re.split(r'[:：]', part, 1)
                key = kv[0].strip()
                try:
                    cnt = int(kv[1].strip())
                except (ValueError, IndexError):
                    self._send(chat_id, f"格式错误: {part}\n请重新输入。")
                    return
                if cnt <= 0:
                    continue
                # 支持编号（纯数字）→ 转换为账号名
                if key.isdigit():
                    idx = int(key) - 1
                    if 0 <= idx < len(acc_names):
                        name = acc_names[idx]
                    else:
                        self._send(chat_id, f"编号 {key} 不存在，有效范围 1~{len(acc_names)}")
                        return
                else:
                    name = key
                assignments.append((name, cnt))
            else:
                self._send(chat_id, f"格式错误: {part}\n请用 编号:数量 格式，例如：1:200,2:200")
                return

        if not assignments:
            self._send(chat_id, "未解析到任何分配，请重新输入。")
            return

        total_assigned = sum(c for _, c in assignments)
        if total_assigned > sess.split_total:
            self._send(chat_id,
                f"分配总数 {total_assigned} 超过可用 {sess.split_total} 条。\n请重新输入。")
            return

        sess.split_counts = assignments
        sess.step = SessionStep.SPLIT_WAIT_CONFIRM

        lines = ["📋 拆分预览："]
        for name, cnt in assignments:
            lines.append(f"  {name}: {cnt} 条")
        lines.append(f"合计: {total_assigned} 条")
        lines.append(f"剩余: {sess.split_total - total_assigned} 条")

        kb = self.tg.make_keyboard([
            [{"text": "✅ 确认拆分", "callback_data": "split:confirm"}],
            [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _split_execute(self, chat_id: str, sess: UserSession) -> None:
        from core.auto_publish_feature import split_excel, PUBLISH_DIR

        source = PUBLISH_DIR / "test.xlsx"
        try:
            result = split_excel(source, sess.split_counts, PUBLISH_DIR)
        except Exception as e:
            self._send(chat_id, f"❌ 拆分失败: {e}")
            with self._session_lock:
                self._sessions.pop(chat_id, None)
            return

        total_assigned = sum(c for _, c in sess.split_counts)
        lines = ["✅ 拆分完成！"]
        for acc, path in result.items():
            lines.append(f"  {acc} → {Path(path).name}")
        lines.append(f"test.xlsx 剩余 {sess.split_total - total_assigned} 条")

        with self._session_lock:
            self._sessions.pop(chat_id, None)

        kb = self.tg.make_keyboard([
            [{"text": "📝 启动自动刊登", "callback_data": "mgr:publish"}],
            [{"text": "🏠 主菜单", "callback_data": "mgr:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)
        self.on_log(f"[TG-MANAGE] 拆分Excel: {', '.join(f'{n}:{c}' for n, c in sess.split_counts)}")

    # ------------------------------------------------------------------
    # /schedule 定期刊登计划
    # ------------------------------------------------------------------

    # /logs 远程查看诊断日志
    def _cmd_logs(self, chat_id: str, text: str) -> None:
        parts = text.strip().split()
        # /logs → 查看自己的日志; /logs <USER_CHAT_ID> → 查看指定用户
        from core.tg_kv_poller import load_relay_config
        cfg = load_relay_config()
        worker_url = (cfg.get("worker_url") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        target_uid = parts[1] if len(parts) > 1 else (cfg.get("user_id") or "")
        if not worker_url or not target_uid:
            self._send(chat_id, "⚠️ 未配置 relay 或未指定用户ID\n用法: /logs [用户TG ID]")
            return
        try:
            import requests
            r = requests.get(
                f"{worker_url}/logs/{target_uid}",
                params={"key": api_key, "n": "80"},
                timeout=15,
            )
            data = r.json()
            logs = data.get("logs", [])
            if not logs:
                self._send(chat_id, f"📋 用户 {target_uid} 暂无诊断日志")
                return
            # 分段发送，每条消息最多 4000 字符
            chunk = f"📋 用户 {target_uid} 最近 {len(logs)} 条诊断日志:\n\n"
            for line in logs:
                if len(chunk) + len(line) > 3800:
                    self._send(chat_id, chunk)
                    chunk = ""
                chunk += line + "\n"
            if chunk.strip():
                self._send(chat_id, chunk)
        except Exception as e:
            self._send(chat_id, f"⚠️ 拉取日志失败: {e}")

    def _cmd_schedule(self, chat_id: str) -> None:
        schedules = load_settings().get("publish_schedule", [])
        accounts = load_accounts()
        acc_names = [a.get("name", "") for a in accounts if a.get("name")]

        lines = ["📅【定期刊登计划】"]
        if schedules:
            for i, sch in enumerate(schedules, 1):
                lines.append(f"  {i}. ⏰ {sch['time']}  📦 {sch['assignments']}")
        else:
            lines.append("  （暂无计划）")

        lines.append("")
        if acc_names:
            lines.append("📋 可用账号：")
            for i, name in enumerate(acc_names, 1):
                lines.append(f"  {i}. {name}")

        kb = self.tg.make_keyboard([
            [{"text": "➕ 添加计划", "callback_data": "sched:add"}],
            [{"text": "🗑 删除计划", "callback_data": "sched:del"}],
            [{"text": "🧹 清空所有", "callback_data": "sched:clear"}],
            [{"text": "🏠 主菜单", "callback_data": "mgr:home"}],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_sched_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "add":
            with self._session_lock:
                self._sessions[chat_id] = UserSession(
                    step=SessionStep.SCHED_WAIT_TIME,
                    sched_accounts=[a.get("name", "") for a in load_accounts() if a.get("name")],
                    created_at=time.time(),
                )
            self._send(chat_id, "请输入执行时间（24小时制 HH:MM）：\n多个时间用逗号隔开，如 09:00,14:00,20:00\n发送 /cancel 取消")
        elif action == "del":
            schedules = load_settings().get("publish_schedule", [])
            if not schedules:
                self._send(chat_id, "当前没有计划可删除。")
                return
            with self._session_lock:
                self._sessions[chat_id] = UserSession(
                    step=SessionStep.SCHED_WAIT_DEL,
                    created_at=time.time(),
                )
            lines = ["请输入要删除的计划编号："]
            for i, sch in enumerate(schedules, 1):
                lines.append(f"  {i}. ⏰ {sch['time']}  📦 {sch['assignments']}")
            self._send(chat_id, "\n".join(lines))
        elif action == "clear":
            settings = load_settings()
            settings["publish_schedule"] = []
            save_settings(settings)
            self._send(chat_id, "✅ 已清空所有定期刊登计划。")
            self.on_log("[TG-MANAGE] 清空定期刊登计划")
            self._sync_gui_schedule()

    def _sched_step_time(self, chat_id: str, sess: UserSession, text: str) -> None:
        text = text.strip()
        time_list = []
        for t in re.split(r'[,，\s]+', text):
            t = t.strip().replace("：", ":")
            if not t:
                continue
            if not re.fullmatch(r'\d{1,2}:\d{2}', t):
                self._send(chat_id, f"格式错误: {t}\n请输入 HH:MM，多个用逗号隔开")
                return
            h, m = t.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                self._send(chat_id, f"时间无效: {t}")
                return
            time_list.append(f"{int(h):02d}:{m}")
        if not time_list:
            self._send(chat_id, "请输入至少一个时间")
            return
        sess.sched_time = ",".join(time_list)
        sess.step = SessionStep.SCHED_WAIT_ASSIGN

        acc_names = sess.sched_accounts
        lines = [f"⏰ 时间：{sess.sched_time}", ""]
        lines.append("帳號列表：")
        for i, n in enumerate(acc_names, 1):
            lines.append(f"  {i}. {n}")
        lines.append("")
        lines.append("请输入分配方案（编号:数量，逗号分隔）：")
        lines.append("例如：1:200,2:200")
        self._send(chat_id, "\n".join(lines))

    def _sched_step_assign(self, chat_id: str, sess: UserSession, text: str) -> None:
        text = text.strip()
        acc_names = sess.sched_accounts
        assignments = []
        for part in re.split(r'[,，]+', text):
            part = part.strip()
            if not part:
                continue
            if ':' not in part and '：' not in part:
                self._send(chat_id, f"⚠️ 「{part}」缺少冒号\n正确格式：编号:数量\n例如 1:20\n\n请检查后重新输入完整方案")
                return
            kv = re.split(r'[:：]', part, 1)
            key = kv[0].strip()
            try:
                cnt = int(kv[1].strip())
            except (ValueError, IndexError):
                self._send(chat_id, f"格式错误: {part}")
                return
            if cnt <= 0:
                continue
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(acc_names):
                    key = acc_names[idx]
                else:
                    self._send(chat_id, f"编号 {int(key)} 不存在，有效范围 1~{len(acc_names)}")
                    return
            assignments.append(f"{key}:{cnt}")

        if not assignments:
            self._send(chat_id, "未解析到分配，请重新输入。")
            return

        assign_str = ",".join(assignments)

        # 显示解析结果让用户确认
        preview = [f"⏰ 时间：{sess.sched_time}", "📦 分配方案："]
        for a in assignments:
            name, cnt = a.rsplit(":", 1)
            preview.append(f"  {name} → {cnt} 条")
        preview.append(f"\n回复 ok 确认，其他内容重新输入")
        sess.sched_assign = assign_str
        sess.step = SessionStep.SCHED_WAIT_CONFIRM
        self._send(chat_id, "\n".join(preview))

    def _sched_step_confirm(self, chat_id: str, sess: UserSession, text: str) -> None:
        if text.strip().lower() != "ok":
            sess.step = SessionStep.SCHED_WAIT_ASSIGN
            self._send(chat_id, "已取消，请重新输入分配方案（编号:数量，逗号分隔）：")
            return
        settings = load_settings()
        schedules = settings.get("publish_schedule", [])
        for t in sess.sched_time.split(","):
            schedules.append({"time": t, "assignments": sess.sched_assign})
        schedules.sort(key=lambda x: x["time"])
        settings["publish_schedule"] = schedules
        save_settings(settings)
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, f"✅ 已添加定期刊登计划：\n⏰ {sess.sched_time}  📦 {sess.sched_assign}")
        self.on_log(f"[TG-MANAGE] 添加定期刊登: {sess.sched_time} {sess.sched_assign}")
        self._sync_gui_schedule()
        self._cmd_schedule(chat_id)

    def _sched_step_del(self, chat_id: str, sess: UserSession, text: str) -> None:
        try:
            idx = int(text.strip()) - 1
        except ValueError:
            self._send(chat_id, "请输入有效的编号：")
            return
        settings = load_settings()
        schedules = settings.get("publish_schedule", [])
        if idx < 0 or idx >= len(schedules):
            self._send(chat_id, f"编号无效，有效范围 1~{len(schedules)}")
            return
        removed = schedules.pop(idx)
        settings["publish_schedule"] = schedules
        save_settings(settings)
        with self._session_lock:
            self._sessions.pop(chat_id, None)
        self._send(chat_id, f"✅ 已删除：⏰ {removed['time']}  📦 {removed['assignments']}")
        self.on_log(f"[TG-MANAGE] 删除定期刊登: {removed['time']}")
        self._sync_gui_schedule()
        self._cmd_schedule(chat_id)

    # ------------------------------------------------------------------
    # 定期刊登 - 后台调度器
    # ------------------------------------------------------------------

    def _schedule_loop(self) -> None:
        last_date = ""
        while True:
            time.sleep(30)
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                hm = now.strftime("%H:%M")
                if today != last_date:
                    self._sched_fired.clear()
                    last_date = today
                schedules = list(load_settings().get("publish_schedule", []))
                for sch in schedules:
                    key = f"{today}_{sch['time']}_{sch['assignments']}"
                    if sch["time"] == hm and key not in self._sched_fired:
                        self._sched_fired.add(key)
                        self._execute_scheduled_publish(sch)
            except Exception as e:
                try:
                    self.on_log(f"[SCHEDULE] 调度异常: {e}")
                except Exception:
                    pass

    def _execute_scheduled_publish(self, sch: dict) -> None:
        from core.auto_publish_feature import split_excel, PUBLISH_DIR
        import openpyxl as _xl

        # 定期刊登通知只发给本机绑定的TG用户
        def _notify(text):
            cid = getattr(getattr(self.tg, '_kv_poller', None), 'user_id', '') or ''
            if not cid:
                # fallback: 第一个非群组用户
                for k in self.tg.get_registered_users():
                    if not str(k).startswith("-"):
                        cid = str(k)
                        break
            if cid:
                try:
                    self.tg._api_send_message(str(cid), text)
                except Exception:
                    pass
        self.on_log(f"[SCHEDULE] 开始执行定期刊登: {sch['time']} {sch['assignments']}")

        # 解析分配方案
        accounts = load_accounts()
        acc_names = [a.get("name", "") for a in accounts if a.get("name")]
        assignments = []
        for part in sch["assignments"].split(","):
            part = part.strip()
            if not part or ':' not in part:
                continue
            kv = part.split(":", 1)
            try:
                name, cnt = kv[0].strip(), int(kv[1].strip())
            except (ValueError, IndexError):
                self.on_log(f"[SCHEDULE] 分配解析失败: {part}")
                continue
            if cnt <= 0:
                continue
            if name.isdigit():
                idx = int(name) - 1
                if 0 <= idx < len(acc_names):
                    name = acc_names[idx]
                else:
                    self.on_log(f"[SCHEDULE] 账号编号 {int(name)} 超出范围")
                    continue
            assignments.append((name, cnt))

        if not assignments:
            _notify("⚠️ 定期刊登失败：分配方案解析失败或为空")
            return

        # 检查 test.xlsx
        source = PUBLISH_DIR / "test.xlsx"
        if not source.exists():
            msg = "⚠️ 定期刊登跳过：test.xlsx 不存在"
            self.on_log(f"[SCHEDULE] {msg}")
            _notify(msg)
            return

        try:
            wb = _xl.load_workbook(source, read_only=True)
            total = wb.active.max_row - 1
            wb.close()
        except Exception as e:
            _notify(f"⚠️ 定期刊登失败：读取 test.xlsx 出错 {e}")
            return

        needed = sum(c for _, c in assignments)
        if total < needed:
            _notify(
                f"⚠️ 定期刊登跳过：test.xlsx 仅 {total} 条，需要 {needed} 条")
            return

        # 拆分
        try:
            result = split_excel(source, assignments, PUBLISH_DIR)
        except Exception as e:
            _notify(f"❌ 定期刊登拆分失败: {e}")
            return

        lines = [f"📅 定期刊登 {sch['time']} 拆分完成："]
        for acc, path in result.items():
            lines.append(f"  {acc} → {Path(path).name}")
        lines.append(f"剩余 {total - needed} 条")
        _notify("\n".join(lines))

        # 启动刊登
        app = self.app
        if not app:
            return
        pub_tab = getattr(app, "publish_tab", None)
        if not pub_tab:
            _notify("⚠️ 自动刊登模块未加载，拆分已完成但未启动刊登。")
            return
        fut = getattr(pub_tab, "_future", None)
        if fut and not fut.done():
            _notify("⚠️ 刊登正在运行中，拆分已完成，等当前刊登结束后请手动 /publish。")
            return
        try:
            pub_tab._trigger_label = f"定时刊登 {sch.get('time', '')}"
            app.after(0, pub_tab.start)
            _notify("✅ 自动刊登已启动")
            self.on_log("[SCHEDULE] 定期刊登已启动")
        except Exception as e:
            _notify(f"❌ 刊登启动失败: {e}")

    # ------------------------------------------------------------------
    # /settings 查看/修改设置
    # ------------------------------------------------------------------

    # 可修改的设置字段
    SETTING_FIELDS = {
        "headless": ("监控无头模式", bool),
        "merch_headless": ("批量无头模式", bool),
        "concurrency": ("监控并发数", int),
        "merch_concurrency": ("批量并发数", int),
        "merch_interval": ("批量间隔(秒)", int),
        "merch_repeat": ("批量默认次数", int),
        "default_refresh": ("刷新间隔(秒)", int),
        "timeout_sec": ("超时(秒)", int),
        "ship_headless": ("出货无头模式", bool),
    }

    def _cmd_settings(self, chat_id: str) -> None:
        app = self.app
        if not app:
            self._send(chat_id, "⚠️ 应用未连接。")
            return

        settings = getattr(app, "settings", {})
        lines = ["⚙️【当前设置】"]
        rows = []
        for key, (label, typ) in self.SETTING_FIELDS.items():
            val = settings.get(key, "?")
            if typ == bool:
                disp = "✅ 开" if val else "❌ 关"
            else:
                disp = str(val)
            lines.append(f"  {label}: {disp}")
            rows.append([{
                "text": f"{label}: {disp}",
                "callback_data": f"set:{key}",
            }])

        lines.append("\n点击按钮修改设置")
        rows.append([{"text": "🏠 主菜单", "callback_data": "mgr:home"}])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    def _handle_settings_cb(self, chat_id: str, parts: List[str]) -> None:
        key = parts[1] if len(parts) > 1 else ""
        if key not in self.SETTING_FIELDS:
            return

        label, typ = self.SETTING_FIELDS[key]
        settings = getattr(self.app, "settings", {})
        cur = settings.get(key, "?")

        if typ == bool:
            new_val = not bool(cur)
            settings[key] = new_val
            save_settings(settings)
            disp = "✅ 开" if new_val else "❌ 关"
            self._send(chat_id, f"已修改【{label}】为：{disp}")
            self.on_log(f"[TG-MANAGE] 设置 {key} = {new_val}")
            self._cmd_settings(chat_id)
        else:
            with self._session_lock:
                self._sessions[chat_id] = UserSession(
                    step=SessionStep.SETTINGS_WAIT_VALUE,
                    setting_field=key,
                    created_at=time.time(),
                )
            self._send(
                chat_id,
                f"请输入【{label}】的新值（当前: {cur}）：\n"
                f"发送 /cancel 取消",
            )

    def _settings_step_value(self, chat_id: str, sess: 'UserSession',
                             text: str) -> None:
        key = sess.setting_field
        if key not in self.SETTING_FIELDS:
            self._send(chat_id, "无效设置项。")
            with self._session_lock:
                self._sessions.pop(chat_id, None)
            return

        label, typ = self.SETTING_FIELDS[key]
        try:
            new_val = int(text.strip())
            if new_val < 1:
                raise ValueError
        except ValueError:
            self._send(chat_id, "请输入有效的正整数：")
            return

        settings = getattr(self.app, "settings", {})
        settings[key] = new_val
        save_settings(settings)

        with self._session_lock:
            self._sessions.pop(chat_id, None)

        self._send(chat_id, f"✅ 已修改【{label}】为：{new_val}")
        self.on_log(f"[TG-MANAGE] 设置 {key} = {new_val}")
        self._cmd_settings(chat_id)

    # ------------------------------------------------------------------
    # 文档上传处理
    # ------------------------------------------------------------------

    def _handle_uploaded_doc(self, chat_id: str, file_path: str,
                             file_name: str, caption: str) -> None:
        """处理通过 TG 上传的文档文件。"""
        ext = Path(file_name).suffix.lower()

        # 根据扩展名自动判断用途
        if ext == ".xlsx":
            # Excel → publish_excels/
            dest_dir = ROOT_DIR / "publish_excels"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file_name
            try:
                import shutil
                shutil.copy2(file_path, str(dest))
                self._send(
                    chat_id,
                    f"📄 Excel 已保存到 publish_excels/\n"
                    f"文件：{file_name}\n"
                    f"可发送 /publish 启动自动刊登",
                )
                self.on_log(f"[TG-MANAGE] 文档保存: {file_name} → publish_excels/")
            except Exception as e:
                self._send(chat_id, f"文件保存失败: {e}")
            return

        if ext == ".txt":
            # txt → 询问用途
            with self._session_lock:
                self._sessions[chat_id] = UserSession(
                    step=SessionStep.DOC_WAIT_ACTION,
                    doc_path=file_path,
                    doc_name=file_name,
                    created_at=time.time(),
                )
            kb = self.tg.make_keyboard([
                [{"text": "📦 商品编号(ids/)", "callback_data": "doc:ids"}],
                [{"text": "❌ 取消", "callback_data": "mgr:cancel"}],
            ])
            self._send(
                chat_id,
                f"📄 收到文件：{file_name}\n请选择用途：",
                reply_markup=kb,
            )
            return

        # 其他格式
        self._send(
            chat_id,
            f"📄 收到文件：{file_name}\n"
            f"支持的格式：.txt（商品编号）、.xlsx（刊登Excel）",
        )

    def _handle_doc_cb(self, chat_id: str, parts: List[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        sess = self._get_session(chat_id)
        if not sess:
            return
        if sess.step == SessionStep.DOC_WAIT_ACTION and action == "ids":
            self._doc_ask_account(chat_id, sess)
        elif sess.step == SessionStep.DOC_WAIT_ACCOUNT and action.startswith("acc_"):
            self._doc_select_account(chat_id, sess, action)

    def _doc_step_action(self, chat_id: str, sess: 'UserSession',
                         text: str) -> None:
        """DOC_WAIT_ACTION 状态下的文本输入处理。"""
        t = text.strip().lower()
        if t in ("ids", "编号", "1"):
            self._doc_ask_account(chat_id, sess)
        else:
            self._send(chat_id, "请点击按钮选择用途，或发送 /cancel 取消。")

    def _doc_ask_account(self, chat_id: str, sess: 'UserSession') -> None:
        """显示账号列表，让用户选择此 txt 文件对应哪个账号。"""
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
        rows.append([{"text": "❌ 取消", "callback_data": "mgr:cancel"}])
        kb = self.tg.make_keyboard(rows)

        sess.step = SessionStep.DOC_WAIT_ACCOUNT
        self._send(
            chat_id,
            "请选择此文件对应的账号：\n"
            "（文件将按账号名保存到 ids/ 目录）",
            reply_markup=kb,
        )

    def _doc_select_account(self, chat_id: str,
                            sess: 'UserSession', action: str) -> None:
        """按钮回调：用户选择了账号。"""
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
                          sess: 'UserSession', text: str) -> None:
        """DOC_WAIT_ACCOUNT 状态下的文本输入：按名称匹配账号。"""
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

    def _doc_save_to_ids(self, chat_id: str, sess: 'UserSession') -> None:
        """将上传的 txt 文件保存到 ids/ 目录，按账号名命名，并清理格式。"""
        ids_dir = ROOT_DIR / "ids"
        ids_dir.mkdir(parents=True, exist_ok=True)

        src = Path(sess.doc_path)

        # 按账号名命名（resolve_ids_file 按 profile_id / account_name 查找）
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
            line_count = len([l for l in cleaned.splitlines() if l.strip()])

            # 检查基础清理结果是否可疑，如果是则尝试 AI 辅助
            ai_used = False
            if line_count == 0 and len(raw.strip()) > 10:
                # 有内容但基础清理提取不到编号 → 尝试 AI
                self._send(chat_id, "⏳ 格式异常，正在使用 AI 辅助提取编号...")
                ai_result = self._ai_clean_ids_content(raw, chat_id)
                if ai_result and ai_result.strip():
                    cleaned = ai_result
                    line_count = len([l for l in cleaned.splitlines() if l.strip()])
                    ai_used = True

            dest.write_text(cleaned, encoding="utf-8")
            ai_tag = "（AI辅助提取）" if ai_used else ""
            self._send(
                chat_id,
                f"✅ 已保存到 ids/{save_name}{ai_tag}\n"
                f"对应账号：{sess.doc_target_account or '未指定'}\n"
                f"有效编号行数：{line_count}\n"
                f"可发送 /idops 执行编号下架删除",
            )
            self.on_log(
                f"[TG-MANAGE] 文档保存: {sess.doc_name} → "
                f"ids/{save_name} ({line_count} 行)"
            )
        except Exception as e:
            self._send(chat_id, f"文件处理失败: {e}")

    @staticmethod
    def _clean_ids_content(raw: str) -> str:
        """清理商品编号文本：去除空行、多余空格、BOM、不可见字符。"""
        lines = raw.splitlines()
        cleaned = []
        for line in lines:
            # 去除 BOM 和不可见字符
            line = re.sub(
                r'[\ufffc\ufeff\u200b\u200c\u200d\u2060\u00a0]', '', line
            )
            line = line.strip()
            if not line:
                continue
            # 如果一行有多个编号（逗号/空格/制表符分隔），拆成多行
            parts = re.split(r'[,，\t\s]+', line)
            for p in parts:
                p = p.strip()
                if p:
                    cleaned.append(p)
        return "\n".join(cleaned) + "\n" if cleaned else ""

    def _ai_clean_ids_content(self, raw: str, chat_id: str) -> Optional[str]:
        """用 AI 辅助清理格式不规范的商品编号文本。

        当基础清理后内容看起来异常（如编号格式不统一、混入无关文字等），
        调用 AI 提取纯编号列表。返回 None 表示 AI 不可用或失败。
        """
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
                self.on_log("[TG-MANAGE] AI 辅助清理编号文本成功")
                return self._clean_ids_content(result)
            return None
        except Exception as e:
            self.on_log(f"[TG-MANAGE] AI 清理失败: {e}")
            return None

    # ==================================================================
    # 远程登录
    # ==================================================================

    def _cmd_login(self, chat_id: str) -> None:
        """发送 /login 时，列出所有账号供选择（含新增账号）。"""
        from core.accounts import load_accounts
        accs = load_accounts()

        buttons = []
        # 新增账号按钮放最前面
        buttons.append([{"text": "➕ 新增账号并登录", "callback_data": "login:new"}])
        for a in accs:
            name = a.get("name", "")
            pid = a.get("profile_id", "")
            buttons.append([{
                "text": name or pid,
                "callback_data": f"login:pick:{pid}",
            }])
        buttons.append([{"text": "❌ 取消", "callback_data": "login:cancel"}])

        self._send(chat_id, "🔑 *远程登录*\n请选择要登录的账号：",
                   reply_markup={"inline_keyboard": buttons})

    def _handle_login_cb(self, chat_id: str, parts: list) -> None:
        """处理 login: 前缀的 callback。"""
        action = parts[1] if len(parts) > 1 else ""
        if action == "cancel":
            self._login_cleanup(chat_id)
            self._send(chat_id, "已取消登录。")
            return
        if action == "pick":
            pid = parts[2] if len(parts) > 2 else ""
            self._login_pick_account(chat_id, pid)
            return
        if action == "new":
            self._login_new_account(chat_id)
            return
        if action == "screenshot":
            self._login_send_screenshot(chat_id)
            return

    def _login_new_account(self, chat_id: str) -> None:
        """用户点击了「新增账号并登录」，要求输入账号名称。"""
        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.LOGIN_WAIT_NEW_NAME,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            "➕ *新增账号*\n\n"
            "请输入账号名称（例如：shop001）：\n"
            "此名称将作为 ProfileID 和显示名\n\n"
            "发送 /cancel 取消",
        )

    def _login_create_account(self, chat_id: str, sess, text: str) -> None:
        """用户输入了新账号名称，创建账号并进入登录流程。"""
        from core.accounts import load_accounts, save_accounts, sanitize_profile_id

        name = text.strip()
        if not name:
            self._send(chat_id, "⚠️ 名称不能为空，请重新输入：")
            return

        pid = sanitize_profile_id(name)
        if not pid:
            self._send(chat_id, "⚠️ 名称包含太多特殊字符，请用英文/数字重新输入：")
            return

        # 检查是否重复
        accs = load_accounts()
        existing_pids = [a.get("profile_id", "").lower() for a in accs]
        if pid.lower() in existing_pids:
            self._send(chat_id, f"⚠️ 账号 `{pid}` 已存在，请换一个名称：")
            return

        # 创建新账号
        new_acc = {
            "name": name,
            "profile_id": pid,
            "start_url": "https://tw.bid.yahoo.com/myauc",
            "refresh_sec": 300,
            "proxy": "",
            "note": "",
        }
        accs.append(new_acc)
        try:
            save_accounts(accs)
        except Exception as e:
            self._send(chat_id, f"⚠️ 保存失败：{e}")
            return

        # 同步到 app 内存
        if self.app and hasattr(self.app, 'accounts'):
            self.app.accounts = accs
            self.app.log(f"[TG-LOGIN] 新增账号: {name} ({pid})")
            # 同步到 states 字典（GUI 列表从这里读取）
            if hasattr(self.app, 'states'):
                from core.monitor import AccountState
                st = AccountState(
                    name=name, profile_id=pid,
                    start_url="https://tw.bid.yahoo.com/myauc",
                    refresh_sec=300, proxy="", note="",
                    selected=True,
                )
                self.app.states[pid] = st
            if hasattr(self.app, '_refresh_table'):
                try:
                    self.app._refresh_table()
                except Exception:
                    pass

        self._send(chat_id, f"✅ 账号 `{name}` 已创建\n\n正在进入登录流程...")

        # 直接进入登录流程
        self._login_pick_account(chat_id, pid)

    def _login_pick_account(self, chat_id: str, pid: str) -> None:
        """用户选择了账号，检查是否已登录，然后要求输入用户名。"""
        from core.accounts import load_accounts
        accs = load_accounts()
        acc = next((a for a in accs if a.get("profile_id") == pid), None)
        if not acc:
            self._send(chat_id, "⚠️ 找不到该账号。")
            return

        acc_name = acc.get("name", pid)

        # 检查监控是否需要暂停
        if self.app and hasattr(self.app, 'mon') and self.app.mon:
            try:
                loop = getattr(self.app, 'loop', None)
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.app.mon.set_hold(pid, True, reason="remote_login"),
                        loop,
                    )
            except Exception:
                pass

        with self._session_lock:
            self._sessions[chat_id] = UserSession(
                step=SessionStep.LOGIN_WAIT_USERNAME,
                login_pid=pid,
                login_acc_name=acc_name,
                created_at=time.time(),
            )

        self._send(
            chat_id,
            f"🔑 *远程登录 - {acc_name}*\n\n"
            f"请输入 Yahoo 账号（邮箱或用户名）：\n\n"
            f"发送 /cancel 取消",
        )

    def _login_step_username(self, chat_id: str, sess: UserSession,
                             text: str) -> None:
        """用户输入了 Yahoo 用户名，启动浏览器并填入。"""
        username = text.strip()
        if not username:
            self._send(chat_id, "⚠️ 用户名不能为空，请重新输入：")
            return

        sess.login_username = username
        self._send(chat_id, "⏳ 正在启动浏览器，请稍候...")

        def _worker():
            try:
                self._login_do_username_sync(chat_id, sess, username)
            except Exception as e:
                self.on_log(f"[REMOTE-LOGIN] 用户名步骤异常: {e}")
                self._send(chat_id, f"❌ 登录异常：{e}")
                self._login_cleanup(chat_id)

        threading.Thread(target=_worker, daemon=True).start()

    def _login_do_username_sync(self, chat_id: str, sess: UserSession,
                                 username: str) -> None:
        """同步：启动浏览器 → 导航 → 填用户名（通过 rls 专属线程）。"""
        from core.remote_login import RemoteLoginSession

        settings = load_settings()
        browser_path = settings.get("browser_path", "")
        profile_dir = str(ROOT_DIR / "profiles" / sess.login_pid)

        from core.accounts import load_accounts
        accs = load_accounts()
        acc = next((a for a in accs if a.get("profile_id") == sess.login_pid), None)
        proxy = (acc.get("proxy", "") if acc else "").strip()

        rls = RemoteLoginSession(
            profile_dir=profile_dir,
            browser_path=browser_path,
            on_log=self.on_log,
            proxy=proxy,
        )
        sess.login_session = rls

        rls.start_sync()

        start_url = (acc.get("start_url", "") if acc else "") or ""
        status = rls.run_async(rls.navigate_and_check(start_url))

        if status == "already_logged_in":
            self._send(chat_id, "✅ 该账号已登录，无需重复操作。")
            rls.close_sync()
            self._login_cleanup(chat_id)
            return

        if status == "error":
            self._send(chat_id, "❌ 导航失败，请检查网络。")
            rls.close_sync()
            self._login_cleanup(chat_id)
            return

        result = rls.run_async(rls.fill_username(username))

        if result == "ok":
            sess.step = SessionStep.LOGIN_WAIT_PASSWORD
            self._send(chat_id, "✅ 用户名已填入。\n\n请输入密码：",
                       reply_markup={"inline_keyboard": [
                           [{"text": "📸 截图查看", "callback_data": "login:screenshot"}],
                       ]})
        elif result == "captcha":
            sess.step = SessionStep.LOGIN_WAIT_CAPTCHA
            self._login_screenshot_and_send_sync(chat_id, rls,
                                                  "⚠️ 出现验证码，请查看截图并输入验证码：")
        else:
            self._login_screenshot_and_send_sync(chat_id, rls,
                                                  "❌ 用户名提交后页面异常，请查看截图：")
            rls.close_sync()
            self._login_cleanup(chat_id)

    def _login_step_password(self, chat_id: str, sess: UserSession,
                             text: str) -> None:
        """用户输入了密码，填入并提交。"""
        password = text.strip()
        if not password:
            self._send(chat_id, "⚠️ 密码不能为空，请重新输入：")
            return

        self._send(chat_id, "⏳ 正在提交密码...")

        def _worker():
            try:
                self._login_do_password_sync(chat_id, sess, password)
            except Exception as e:
                self.on_log(f"[REMOTE-LOGIN] 密码步骤异常: {e}")
                self._send(chat_id, f"❌ 登录异常：{e}")
                self._login_cleanup(chat_id)

        threading.Thread(target=_worker, daemon=True).start()

    def _login_do_password_sync(self, chat_id: str, sess: UserSession,
                                 password: str) -> None:
        """同步：填入密码并提交（通过 rls 专属线程）。"""
        rls = sess.login_session
        if not rls:
            self._send(chat_id, "❌ 登录会话已失效，请重新 /login")
            self._login_cleanup(chat_id)
            return

        result = rls.run_async(rls.fill_password(password))

        if result == "success":
            self._send(chat_id,
                       f"✅ *{sess.login_acc_name}* 登录成功！\n"
                       f"Cookie 已保存，监控将自动恢复。")
            rls.close_sync()
            self._login_finish(chat_id, sess)
        elif result == "captcha":
            sess.step = SessionStep.LOGIN_WAIT_CAPTCHA
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "⚠️ 出现验证码，请查看截图并输入验证码：")
        elif result == "challenge":
            sess.step = SessionStep.LOGIN_WAIT_CAPTCHA
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "⚠️ 需要二次验证，请查看截图并输入验证码：")
        elif result == "wrong_password":
            sess.step = SessionStep.LOGIN_WAIT_PASSWORD
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "❌ 密码错误，请重新输入正确的密码：")
        else:
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "❌ 登录失败（未知错误），请查看截图：")
            rls.close_sync()
            self._login_cleanup(chat_id)

    # -- 验证码步骤 --

    def _login_step_captcha(self, chat_id: str, sess: UserSession,
                            text: str) -> None:
        """用户输入了验证码。"""
        code = text.strip()
        if not code:
            self._send(chat_id, "⚠️ 验证码不能为空，请重新输入：")
            return

        self._send(chat_id, "⏳ 正在提交验证码...")

        def _worker():
            try:
                self._login_do_captcha_sync(chat_id, sess, code)
            except Exception as e:
                self.on_log(f"[REMOTE-LOGIN] 验证码步骤异常: {e}")
                self._send(chat_id, f"❌ 异常：{e}")
                self._login_cleanup(chat_id)

        threading.Thread(target=_worker, daemon=True).start()

    def _login_do_captcha_sync(self, chat_id: str, sess: UserSession,
                                code: str) -> None:
        """同步：填入验证码并提交（通过 rls 专属线程）。"""
        rls = sess.login_session
        if not rls:
            self._send(chat_id, "❌ 登录会话已失效，请重新 /login")
            self._login_cleanup(chat_id)
            return

        result = rls.run_async(rls.fill_captcha(code))

        if result == "success":
            self._send(chat_id,
                       f"✅ *{sess.login_acc_name}* 登录成功！\n"
                       f"Cookie 已保存，监控将自动恢复。")
            rls.close_sync()
            self._login_finish(chat_id, sess)
        elif result in ("captcha", "challenge"):
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "⚠️ 验证码可能有误，请重新查看截图并输入：")
        else:
            self._login_screenshot_and_send_sync(
                chat_id, rls,
                "❌ 验证失败，请查看截图：")
            rls.close_sync()
            self._login_cleanup(chat_id)

    # -- 辅助方法 --

    async def _login_screenshot_and_send(self, chat_id: str, rls,
                                         caption: str) -> None:
        """截图并发送到 TG，附带再次截图按钮。"""
        import os
        path = await rls.screenshot()
        if path:
            self.tg.send_photo(chat_id, path, caption=caption)
            try:
                os.unlink(path)
            except Exception:
                pass
        else:
            self._send(chat_id, caption + "\n（截图失败）")
        # 附带截图按钮，方便用户再次查看
        self._send(chat_id, "输入内容继续，或点击按钮查看当前页面：",
                   reply_markup={"inline_keyboard": [
                       [{"text": "📸 再次截图", "callback_data": "login:screenshot"}],
                   ]})

    def _login_screenshot_and_send_sync(self, chat_id: str, rls,
                                         caption: str) -> None:
        """同步版截图并发送（通过 rls 专属线程）。"""
        import os
        try:
            path = rls.run_async(rls.screenshot(), timeout=15)
        except Exception:
            path = None
        if path:
            self.tg.send_photo(chat_id, path, caption=caption)
            try:
                os.unlink(path)
            except Exception:
                pass
        else:
            self._send(chat_id, caption + "\n（截图失败）")
        self._send(chat_id, "输入内容继续，或点击按钮查看当前页面：",
                   reply_markup={"inline_keyboard": [
                       [{"text": "📸 再次截图", "callback_data": "login:screenshot"}],
                   ]})

    def _login_send_screenshot(self, chat_id: str) -> None:
        """用户点击截图按钮时，发送当前页面截图。"""
        with self._session_lock:
            sess = self._sessions.get(chat_id)
        if not sess or not sess.login_session:
            self._send(chat_id, "⚠️ 没有进行中的登录会话。")
            return

        def _worker():
            try:
                self._login_screenshot_and_send_sync(
                    chat_id, sess.login_session, "📸 当前页面截图：")
            except Exception as e:
                self._send(chat_id, f"截图失败：{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _login_finish(self, chat_id: str, sess: UserSession) -> None:
        """登录成功后的收尾：释放监控 hold，清理会话。"""
        pid = sess.login_pid
        if self.app and hasattr(self.app, 'mon') and self.app.mon:
            try:
                loop = getattr(self.app, 'loop', None)
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.app.mon.set_hold(pid, False),
                        loop,
                    )
            except Exception:
                pass
        self.on_log(f"[REMOTE-LOGIN] {sess.login_acc_name} 登录完成，已恢复监控")
        with self._session_lock:
            self._sessions.pop(chat_id, None)

    def _login_cleanup(self, chat_id: str) -> None:
        """清理登录会话，恢复监控 hold。"""
        with self._session_lock:
            sess = self._sessions.pop(chat_id, None)
        if not sess:
            return
        if sess.login_pid and self.app and hasattr(self.app, 'mon') and self.app.mon:
            try:
                loop = getattr(self.app, 'loop', None)
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.app.mon.set_hold(sess.login_pid, False), loop)
            except Exception:
                pass

    def _login_cleanup_async(self, chat_id: str, sess: UserSession) -> None:
        """在异步 worker 异常时关闭浏览器并清理。"""
        rls = sess.login_session
        if rls:
            try:
                rls.close_sync()
            except Exception:
                pass
        self._login_cleanup(chat_id)