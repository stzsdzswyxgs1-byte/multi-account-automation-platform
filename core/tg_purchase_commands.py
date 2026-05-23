"""TG 采购指令处理器

处理采购相关 TG 指令和 inline keyboard 按钮交互，
以及监控推送新订单通知、物流通知。
支持：绑定、查看详情、修改、监控控制等。
"""
from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

from core.purchase_feature import PurchaseLink, load_links, save_links


# ---------- 文本清理 ----------

def _sanitize(text: str) -> str:
    """清理 TG 输入中的不可见/特殊 Unicode 字符。

    TG 消息可能包含 Object Replacement Character (\\ufffc)、
    零宽空格 (\\u200b)、BOM (\\ufeff) 等不可见字符，
    这些字符会导致订单号搜索失败。
    """
    # 去掉常见不可见字符
    text = re.sub(r'[\ufffc\ufeff\u200b\u200c\u200d\u2060\u00a0]', '', text)
    # 去掉所有 Unicode 控制字符（C0/C1），但保留换行和制表符
    text = re.sub(r'[^\S \t\n\r]+', '', text)
    return text.strip()


# ---------- 绑定会话状态 ----------

class BindStep(Enum):
    WAIT_ACCOUNT = auto()
    WAIT_YAHOO_ORDER = auto()
    WAIT_SUB_ORDERS = auto()
    WAIT_PLATFORM = auto()
    WAIT_PURCHASE_ID = auto()
    WAIT_ITEM_PRODUCT_NAME = auto()  # 每个采购订单的商品名称
    WAIT_ITEM_SPEC = auto()          # 每个采购订单的规格
    WAIT_REMARK = auto()             # 备注（整单共用）


# ---------- 修改会话状态 ----------

class EditStep(Enum):
    WAIT_INDEX = auto()
    WAIT_FIELD = auto()
    WAIT_VALUE = auto()


@dataclass
class BindSession:
    step: BindStep = BindStep.WAIT_ACCOUNT
    acc_name: str = ""
    profile_id: str = ""
    yahoo_order_no: str = ""
    sub_order_nos: List[str] = field(default_factory=list)
    platform: str = ""
    purchase_ids: List[str] = field(default_factory=list)
    # 每个采购订单各自的商品名称和规格（与 purchase_ids 一一对应）
    item_names: List[str] = field(default_factory=list)
    item_specs: List[str] = field(default_factory=list)
    current_item_idx: int = 0  # 当前正在填写第几个采购订单
    created_at: float = 0.0  # time.time()


@dataclass
class EditSession:
    step: EditStep = EditStep.WAIT_INDEX
    link_index: int = -1
    field_name: str = ""
    created_at: float = 0.0


SESSION_TIMEOUT = 1800  # 绑定会话超时（秒）30分钟

# 可修改的字段映射
EDITABLE_FIELDS = {
    "yahoo_order_no": "Yahoo订单号",
    "sub_order_nos": "副订单编号",
    "purchase_order_id": "采购订单号",
    "product_name": "商品名称",
    "spec": "规格",
    "remark": "备注",
    "platform": "采购平台",
}


# ---------- 主类 ----------

class PurchaseCommandHandler:

    def __init__(self, tg_purchase_bot, on_log: Callable[[str], None],
                 purchase_tab=None, owner_chat_id: str = ""):
        self.tg = tg_purchase_bot  # PurchaseTelegramBot（多用户）
        self.on_log = on_log
        self.purchase_tab = purchase_tab  # GUI PurchaseFeatureTab（可选）
        self.owner_chat_id = (owner_chat_id or "").strip()  # 当前使用者 TG chat_id
        # 每个用户独立的绑定会话，key=chat_id
        self._bind_sessions: Dict[str, BindSession] = {}
        # 每个用户独立的修改会话，key=chat_id
        self._edit_sessions: Dict[str, EditSession] = {}
        self._session_lock = threading.Lock()
        # 最近推送的订单上下文：[{acc_name, profile_id, newv, oldv, ts}]
        self._order_context: List[Dict[str, Any]] = []
        # 监控中的账号列表（由 monitor 设置）
        self._monitor_accounts: List[Any] = []

    def _notify_owner(self, text: str) -> None:
        """只通知当前使用者（不广播给所有注册用户）。"""
        if self.owner_chat_id and self.tg:
            try:
                self.tg.send_to(self.owner_chat_id, text)
            except Exception as e:
                self.on_log(f"[TG-PURCHASE] 通知失败: {e}")

    # ------------------------------------------------------------------
    # 公共入口：处理 TG 消息
    # ------------------------------------------------------------------

    def handle_message(self, text: str, message_id: int, chat_id: str) -> bool:
        """处理 TG 消息。返回 True 表示已处理（是采购指令或绑定会话输入）。"""
        t = _sanitize(text)

        # 指令
        if t == "/bind":
            self._start_bind(chat_id)
            return True
        if t == "/orders":
            self._show_orders(chat_id)
            return True
        if t == "/tracking":
            self._show_tracking(chat_id)
            return True
        if t == "/status":
            self._show_status(chat_id)
            return True
        if t == "/list":
            self._show_list(chat_id)
            return True
        if t == "/startmon":
            self._cmd_startmon(chat_id)
            return True
        if t == "/stopmon":
            self._cmd_stopmon(chat_id)
            return True
        if t == "/fetch":
            self._cmd_fetch(chat_id)
            return True
        if t == "/watchall":
            self._cmd_watch_all(chat_id, True)
            return True
        if t == "/unwatchall":
            self._cmd_watch_all(chat_id, False)
            return True
        if t.startswith("/ship"):
            self._cmd_ship(chat_id, t)
            return True
        if t.startswith("/unwatch"):
            self._cmd_watch(chat_id, t, False)
            return True
        if t.startswith("/watch"):
            self._cmd_watch(chat_id, t, True)
            return True
        if t.startswith("/detail"):
            self._show_detail(chat_id, t)
            return True
        if t == "/edit":
            self._start_edit(chat_id)
            return True
        if t == "/cancel":
            with self._session_lock:
                cancelled = False
                if chat_id in self._bind_sessions:
                    del self._bind_sessions[chat_id]
                    cancelled = True
                if chat_id in self._edit_sessions:
                    del self._edit_sessions[chat_id]
                    cancelled = True
                if cancelled:
                    self._send(chat_id, "已取消当前操作。")
                    self._send_main_menu(chat_id)
                    return True
            return False

        # 绑定会话中的后续输入
        with self._session_lock:
            if chat_id in self._bind_sessions:
                self._handle_bind_input(chat_id, t)
                return True
            if chat_id in self._edit_sessions:
                self._handle_edit_input(chat_id, t)
                return True

        return False

    # ------------------------------------------------------------------
    # 公共入口：处理 inline keyboard 按钮回调
    # ------------------------------------------------------------------

    def handle_callback(self, data: str, cb_id: str,
                        chat_id: str, message_id: int) -> None:
        """处理按钮点击。data 格式: 'prefix:action' 或 'prefix:action:arg'"""
        parts = data.split(":", 2)
        prefix = parts[0] if parts else ""

        if prefix == "menu":
            self._handle_menu_cb(chat_id, parts)
        elif prefix == "acc":
            self._handle_acc_cb(chat_id, parts)
        elif prefix == "plat":
            self._handle_plat_cb(chat_id, parts)
        elif prefix == "watch":
            self._handle_watch_cb(chat_id, parts)
        elif prefix == "detail":
            self._handle_detail_cb(chat_id, parts)
        elif prefix == "edit":
            self._handle_edit_cb(chat_id, parts)
        elif prefix == "editf":
            self._handle_editfield_cb(chat_id, parts)
        elif prefix == "del":
            self._handle_delete_cb(chat_id, parts)
        elif prefix == "skip":
            self._handle_skip_cb(chat_id, parts)
        elif prefix == "ship":
            self._handle_ship_cb(chat_id, parts)

    # ------------------------------------------------------------------
    # /bind 交互式绑定
    # ------------------------------------------------------------------

    def _start_bind(self, chat_id: str) -> None:
        accounts = self._get_account_list()
        if not accounts:
            self._send(chat_id, "当前没有监控中的账号，请先在 GUI 启动监控。")
            return

        with self._session_lock:
            self._bind_sessions[chat_id] = BindSession(
                step=BindStep.WAIT_ACCOUNT,
                created_at=time.time(),
            )

        # 用 inline keyboard 按钮选择账号
        rows = []
        for i, acc in enumerate(accounts):
            rows.append([{"text": acc.name, "callback_data": f"acc:{i}"}])
        rows.append([{"text": "❌ 取消", "callback_data": "menu:cancel"}])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "【采购绑定】请选择账号：", reply_markup=kb)

    def _handle_bind_input(self, chat_id: str, text: str) -> None:
        sess = self._bind_sessions.get(chat_id)
        if not sess:
            return

        # 超时检查
        if time.time() - sess.created_at > SESSION_TIMEOUT:
            del self._bind_sessions[chat_id]
            self._send(chat_id, "绑定操作已超时，请重新发送 /bind。")
            return

        if sess.step == BindStep.WAIT_ACCOUNT:
            self._bind_step_account(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_YAHOO_ORDER:
            self._bind_step_yahoo_order(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_SUB_ORDERS:
            self._bind_step_sub_orders(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_PLATFORM:
            self._bind_step_platform(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_PURCHASE_ID:
            self._bind_step_purchase_id(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_ITEM_PRODUCT_NAME:
            self._bind_step_item_product_name(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_ITEM_SPEC:
            self._bind_step_item_spec(chat_id, sess, text)
        elif sess.step == BindStep.WAIT_REMARK:
            self._bind_step_remark(chat_id, sess, text)

    def _bind_step_account(self, chat_id: str, sess: BindSession, text: str) -> None:
        accounts = self._get_account_list()
        try:
            idx = int(text) - 1
            if 0 <= idx < len(accounts):
                acc = accounts[idx]
                sess.acc_name = acc.name
                sess.profile_id = acc.profile_id
                sess.step = BindStep.WAIT_YAHOO_ORDER
                self._send(chat_id, f"已选择：{acc.name}\n请输入 Yahoo 订单号：")
                return
        except ValueError:
            pass
        # 尝试按名称匹配
        for acc in accounts:
            if acc.name == text:
                sess.acc_name = acc.name
                sess.profile_id = acc.profile_id
                sess.step = BindStep.WAIT_YAHOO_ORDER
                self._send(chat_id, f"已选择：{acc.name}\n请输入 Yahoo 订单号：")
                return
        self._send(chat_id, "无效输入，请输入序号或账号名。")

    def _bind_step_yahoo_order(self, chat_id: str, sess: BindSession, text: str) -> None:
        if not text:
            self._send(chat_id, "订单号不能为空，请重新输入：")
            return
        sess.yahoo_order_no = text.strip()
        sess.step = BindStep.WAIT_SUB_ORDERS
        kb = self.tg.make_keyboard([
            [{"text": "⏭ 跳过（无副订单）", "callback_data": "skip:sub_orders"}],
        ])
        self._send(
            chat_id,
            "请输入副订单编号（多个用逗号分隔，最多5个）：\n"
            "如无副订单可点击跳过",
            reply_markup=kb,
        )

    def _bind_step_sub_orders(self, chat_id: str, sess: BindSession, text: str) -> None:
        subs = [x.strip() for x in text.replace("，", ",").replace("+", ",").split(",") if x.strip()]
        sess.sub_order_nos = subs[:5]  # 最多5个
        self._goto_platform_step(chat_id, sess)

    def _goto_platform_step(self, chat_id: str, sess: BindSession) -> None:
        sess.step = BindStep.WAIT_PLATFORM
        kb = self.tg.make_keyboard([
            [
                {"text": "闲鱼", "callback_data": "plat:xianyu"},
                {"text": "煤炉", "callback_data": "plat:mercari"},
            ],
        ])
        self._send(chat_id, "请选择采购平台：", reply_markup=kb)

    def _bind_step_platform(self, chat_id: str, sess: BindSession, text: str) -> None:
        t = text.lower()
        if t in ("1", "闲鱼", "xianyu"):
            sess.platform = "xianyu"
        elif t in ("2", "煤炉", "mercari"):
            sess.platform = "mercari"
        else:
            self._send(chat_id, "无效输入，请输入 1（闲鱼）或 2（煤炉）：")
            return
        sess.step = BindStep.WAIT_PURCHASE_ID
        platform_name = "闲鱼" if sess.platform == "xianyu" else "煤炉"
        self._send(chat_id, f"平台：{platform_name}\n请输入采购订单号（多个用逗号分隔）：")

    def _bind_step_purchase_id(self, chat_id: str, sess: BindSession, text: str) -> None:
        if not text:
            self._send(chat_id, "采购订单号不能为空，请重新输入：")
            return

        purchase_ids = [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]
        if not purchase_ids:
            self._send(chat_id, "采购订单号不能为空，请重新输入：")
            return

        sess.purchase_ids = purchase_ids
        sess.item_names = []
        sess.item_specs = []
        sess.current_item_idx = 0
        # 开始逐个填写商品信息
        self._ask_item_product_name(chat_id, sess)

    def _ask_item_product_name(self, chat_id: str, sess: BindSession) -> None:
        """提示用户输入当前采购订单的商品名称。"""
        idx = sess.current_item_idx
        total = len(sess.purchase_ids)
        pid = sess.purchase_ids[idx]
        if total == 1:
            prompt = f"采购订单：{pid}\n请输入商品名称（必填）："
        else:
            prompt = f"【{idx+1}/{total}】采购订单：{pid}\n请输入商品名称（必填）："
        sess.step = BindStep.WAIT_ITEM_PRODUCT_NAME
        self._send(chat_id, prompt)

    def _bind_step_item_product_name(self, chat_id: str, sess: BindSession, text: str) -> None:
        if not text.strip():
            self._send(chat_id, "商品名称不能为空，请重新输入：")
            return
        sess.item_names.append(text.strip())
        # 接着问规格
        idx = sess.current_item_idx
        total = len(sess.purchase_ids)
        pid = sess.purchase_ids[idx]
        if total == 1:
            prompt = f"请输入规格（必填）："
        else:
            prompt = f"【{idx+1}/{total}】{pid}\n请输入规格（必填）："
        sess.step = BindStep.WAIT_ITEM_SPEC
        self._send(chat_id, prompt)

    def _bind_step_item_spec(self, chat_id: str, sess: BindSession, text: str) -> None:
        if not text.strip():
            self._send(chat_id, "规格不能为空，请重新输入：")
            return
        sess.item_specs.append(text.strip())
        sess.current_item_idx += 1
        # 还有下一个采购订单？
        if sess.current_item_idx < len(sess.purchase_ids):
            self._ask_item_product_name(chat_id, sess)
        else:
            # 所有采购订单的商品信息已填完，进入备注
            sess.step = BindStep.WAIT_REMARK
            self._send(chat_id, "请输入备注（整单共用，必填）：")

    def _bind_step_remark(self, chat_id: str, sess: BindSession, text: str) -> None:
        if not text.strip():
            self._send(chat_id, "备注不能为空，请重新输入：")
            return
        self._bind_finish(chat_id, sess, text.strip())

    def _bind_finish(self, chat_id: str, sess: BindSession, remark: str) -> None:
        links = load_links()
        for i, pid in enumerate(sess.purchase_ids):
            link = PurchaseLink(
                yahoo_profile_id=sess.profile_id,
                yahoo_acc_name=sess.acc_name,
                yahoo_order_no=sess.yahoo_order_no.strip(),
                sub_order_nos=[s.strip() for s in sess.sub_order_nos],
                platform=sess.platform,
                purchase_order_id=pid.strip(),
                product_name=sess.item_names[i].strip() if i < len(sess.item_names) else "",
                spec=sess.item_specs[i].strip() if i < len(sess.item_specs) else "",
                remark=(remark or "").strip(),
                watch=True,
            )
            links.append(link)
        save_links(links)

        del self._bind_sessions[chat_id]
        self._refresh_gui()

        platform_name = "闲鱼" if sess.platform == "xianyu" else "煤炉"
        subs_str = ", ".join(sess.sub_order_nos) if sess.sub_order_nos else "-"
        # 构建每个采购订单的商品信息
        item_lines = []
        for i, pid in enumerate(sess.purchase_ids):
            name = sess.item_names[i] if i < len(sess.item_names) else "-"
            spec = sess.item_specs[i] if i < len(sess.item_specs) else "-"
            item_lines.append(f"  {pid} | {name} | {spec}")
        items_str = "\n".join(item_lines)
        ids_str = ", ".join(sess.purchase_ids)
        self._send(
            chat_id,
            f"✅ 绑定成功！\n"
            f"账号：{sess.acc_name}\n"
            f"Yahoo订单：{sess.yahoo_order_no}\n"
            f"副订单：{subs_str}\n"
            f"平台：{platform_name}\n"
            f"采购订单（订单号 | 商品 | 规格）：\n{items_str}\n"
            f"备注：{remark or '-'}"
        )
        self.on_log(f"[TG-PURCHASE] 绑定: {sess.yahoo_order_no} ↔ {platform_name} {ids_str}")

        # 绑定后自动启动采购监控（如果未运行）
        self._auto_start_monitor(chat_id)

    def _auto_start_monitor(self, chat_id: str) -> None:
        """绑定完成后，如果采购监控未运行则自动启动。"""
        tab = self.purchase_tab
        if not tab:
            return
        t = getattr(tab, "_thread", None)
        if t and t.is_alive():
            return  # 已在运行
        try:
            tab.action_start()
            self._send(chat_id, "▶️ 采购监控已自动启动。")
            self.on_log("[TG-PURCHASE] 绑定后自动启动采购监控")
        except Exception as e:
            self._send(chat_id, f"⚠️ 自动启动采购监控失败: {e}\n请手动发送 /startmon")

    # ------------------------------------------------------------------
    # /status 系统状态总览
    # ------------------------------------------------------------------

    def _show_status(self, chat_id: str) -> None:
        links = load_links()
        watching = [x for x in links if x.watch]
        total = len(links)

        # 采购监控运行状态
        mon_running = False
        if self.purchase_tab:
            t = getattr(self.purchase_tab, "_thread", None)
            if t and t.is_alive():
                mon_running = True

        # 账号监控状态
        accounts = self._get_account_list()
        acc_lines = []
        for a in accounts:
            name = getattr(a, "name", "?")
            status = getattr(a, "status", "?")
            paid = getattr(a, "paid_to_ship", 0)
            cod = getattr(a, "cod", 0)
            acc_lines.append(f"  {name} | {status} | 待出货:{paid} 取货付款:{cod}")

        lines = ["📊【系统状态】"]
        lines.append(f"采购监控: {'✅ 运行中' if mon_running else '⏹ 未启动'}")
        lines.append(f"绑定总数: {total} | 监控中: {len(watching)}")

        # 按状态统计
        status_count: Dict[str, int] = {}
        for x in links:
            s = x.status or "待监控"
            status_count[s] = status_count.get(s, 0) + 1
        if status_count:
            parts = [f"{k}:{v}" for k, v in status_count.items()]
            lines.append(f"状态分布: {' | '.join(parts)}")

        if acc_lines:
            lines.append(f"\n📋 监控账号 ({len(accounts)}):")
            lines.extend(acc_lines)
        else:
            lines.append("\n暂无监控账号")

        kb = self.tg.make_keyboard([
            [
                {"text": "📋 绑定列表", "callback_data": "menu:list"},
                {"text": "🏠 主菜单", "callback_data": "menu:home"},
            ],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # /list 查看所有采购绑定
    # ------------------------------------------------------------------

    def _show_list(self, chat_id: str) -> None:
        links = load_links()
        if not links:
            kb = self.tg.make_keyboard([
                [{"text": "➕ 新建绑定", "callback_data": "menu:bind"}],
            ])
            self._send(chat_id, "暂无采购绑定记录。", reply_markup=kb)
            return

        lines = ["📋【采购绑定列表】"]
        for i, x in enumerate(links):
            platform = "闲鱼" if x.platform == "xianyu" else "煤炉"
            watch_icon = "👁" if x.watch else "  "
            tracking = x.tracking_no or "-"
            lines.append(
                f"{watch_icon} {i+1}. {x.yahoo_acc_name} | {x.yahoo_order_no}\n"
                f"     {platform} {x.purchase_order_id}\n"
                f"     {x.status} | 物流:{tracking}"
            )

        lines.append(f"\n共 {len(links)} 条（👁=监控中）")

        # 每条记录的操作按钮（每行最多4个按钮，分批发送避免过长）
        # 先发文本列表
        self._send(chat_id, "\n".join(lines))

        # 再发操作按钮（每条一行：详情 | 监控切换 | 编辑 | 删除）
        rows = []
        for i, x in enumerate(links):
            w_text = "🔕取消监控" if x.watch else "👁开启监控"
            w_data = f"watch:{i}:{'off' if x.watch else 'on'}"
            rows.append([
                {"text": f"📄{i+1}", "callback_data": f"detail:{i}"},
                {"text": w_text, "callback_data": w_data},
                {"text": "✏️", "callback_data": f"edit:{i}"},
                {"text": "🗑", "callback_data": f"del:{i}"},
            ])
        rows.append([
            {"text": "➕ 新建绑定", "callback_data": "menu:bind"},
            {"text": "🏠 主菜单", "callback_data": "menu:home"},
        ])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "选择操作：", reply_markup=kb)

    # ------------------------------------------------------------------
    # /startmon 启动采购监控
    # ------------------------------------------------------------------

    def _cmd_startmon(self, chat_id: str) -> None:
        tab = self.purchase_tab
        if not tab:
            self._send(chat_id, "采购监控模块未加载，请在 GUI 中操作。")
            return

        t = getattr(tab, "_thread", None)
        if t and t.is_alive():
            self._send(chat_id, "采购监控已在运行中。")
            return

        try:
            tab.action_start()
            self._send(chat_id, "✅ 采购监控已启动。")
            self.on_log("[TG-PURCHASE] 远程启动采购监控")
        except Exception as e:
            self._send(chat_id, f"启动失败: {e}")

    # ------------------------------------------------------------------
    # /stopmon 停止采购监控
    # ------------------------------------------------------------------

    def _cmd_stopmon(self, chat_id: str) -> None:
        tab = self.purchase_tab
        if not tab:
            self._send(chat_id, "采购监控模块未加载，请在 GUI 中操作。")
            return

        t = getattr(tab, "_thread", None)
        if not (t and t.is_alive()):
            self._send(chat_id, "采购监控当前未运行。")
            return

        try:
            tab.action_stop()
            self._send(chat_id, "⏹ 已发送停止指令，会在当前抓取结束后停止。")
            self.on_log("[TG-PURCHASE] 远程停止采购监控")
        except Exception as e:
            self._send(chat_id, f"停止失败: {e}")

    # ------------------------------------------------------------------
    # /fetch 手动抓取一次（所有监控中的）
    # ------------------------------------------------------------------

    def _cmd_fetch(self, chat_id: str) -> None:
        tab = self.purchase_tab
        if not tab:
            self._send(chat_id, "采购监控模块未加载，请在 GUI 中操作。")
            return

        links = load_links()
        indices = [i for i, x in enumerate(links) if x.watch]
        if not indices:
            self._send(chat_id, "没有监控中的记录可抓取。")
            return

        self._send(chat_id, f"开始抓取 {len(indices)} 条监控记录，请稍候...")
        self.on_log(f"[TG-PURCHASE] 远程抓取 {len(indices)} 条")

        def _do_fetch():
            try:
                tab._scrape_indices(indices=indices, notify=True, headless=True)
                self._send(chat_id, f"✅ 抓取完成（{len(indices)} 条）。\n发送 /tracking 查看结果。")
            except Exception as e:
                self._send(chat_id, f"抓取异常: {e}")

        threading.Thread(target=_do_fetch, daemon=True).start()

    # ------------------------------------------------------------------
    # /watch N  /unwatch N  切换监控
    # ------------------------------------------------------------------

    def _cmd_watch(self, chat_id: str, text: str, enable: bool) -> None:
        parts = text.split()
        if len(parts) < 2:
            action = "监控" if enable else "取消监控"
            self._send(chat_id, f"用法: /{'watch' if enable else 'unwatch'} 序号\n例: /{'watch' if enable else 'unwatch'} 1\n\n发送 /list 查看序号")
            return

        try:
            idx = int(parts[1]) - 1
        except ValueError:
            self._send(chat_id, "请输入有效的序号数字。")
            return

        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, f"序号超出范围（共 {len(links)} 条）。")
            return

        link = links[idx]
        link.watch = enable
        save_links(links)
        self._refresh_gui()

        platform = "闲鱼" if link.platform == "xianyu" else "煤炉"
        action = "已开启监控" if enable else "已取消监控"
        self._send(
            chat_id,
            f"{action}: #{idx+1}\n"
            f"{link.yahoo_acc_name} | {link.yahoo_order_no}\n"
            f"{platform} {link.purchase_order_id}"
        )

    # ------------------------------------------------------------------
    # /orders 查看最近推送的订单
    # ------------------------------------------------------------------

    def _show_orders(self, chat_id: str) -> None:
        if not self._order_context:
            self._send(chat_id, "暂无最近推送的订单。\n发送 /bind 可手动绑定采购订单。")
            return

        lines = ["【最近订单变化】"]
        for ctx in self._order_context[-10:]:  # 最多显示10条
            acc = ctx.get("acc_name", "?")
            newv = ctx.get("newv", {})
            oldv = ctx.get("oldv", {})
            ts = ctx.get("ts", 0)
            t_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"

            parts = []
            for k, label in [("paid_to_ship", "待出货"), ("cod", "取货付款")]:
                o = int(oldv.get(k, 0))
                n = int(newv.get(k, 0))
                if n > o:
                    parts.append(f"{label}+{n - o}")
            change = ", ".join(parts) if parts else "变化"
            lines.append(f"  {t_str} | {acc} | {change}")

        lines.append(f"\n发送 /bind 绑定采购订单")
        kb = self.tg.make_keyboard([
            [
                {"text": "➕ 新建绑定", "callback_data": "menu:bind"},
                {"text": "🏠 主菜单", "callback_data": "menu:home"},
            ],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # /tracking 查看物流状态
    # ------------------------------------------------------------------

    def _show_tracking(self, chat_id: str) -> None:
        links = load_links()
        watching = [x for x in links if x.watch]
        if not watching:
            self._send(chat_id, "当前没有监控中的采购绑定。\n发送 /bind 可新建绑定。")
            return

        lines = ["【采购监控状态】"]
        for x in watching:
            platform = "闲鱼" if x.platform == "xianyu" else "煤炉"
            tracking = x.tracking_no or "-"
            lines.append(
                f"  {x.yahoo_acc_name} | {x.yahoo_order_no}\n"
                f"    {platform} {x.purchase_order_id} | {x.status} | 物流: {tracking}"
            )

        lines.append(f"\n共 {len(watching)} 条监控中")
        kb = self.tg.make_keyboard([
            [
                {"text": "🔄 手动抓取", "callback_data": "menu:fetch"},
                {"text": "🏠 主菜单", "callback_data": "menu:home"},
            ],
        ])
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # 推送：新订单通知
    # ------------------------------------------------------------------

    def notify_new_order(self, acc_name: str, profile_id: str,
                         newv: Dict[str, int], oldv: Dict[str, int]) -> None:
        """监控检测到新订单时调用，广播给所有注册用户。"""
        # 缓存上下文
        self._order_context.append({
            "acc_name": acc_name,
            "profile_id": profile_id,
            "newv": dict(newv),
            "oldv": dict(oldv),
            "ts": time.time(),
        })
        # 只保留最近 50 条
        if len(self._order_context) > 50:
            self._order_context = self._order_context[-50:]

        # 构建通知
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = []
        for k, label in [("paid_to_ship", "待出货(已付款)"), ("cod", "取货付款")]:
            o = int(oldv.get(k, 0))
            n = int(newv.get(k, 0))
            if n > o:
                parts.append(f"{label}: +{n - o} (当前 {n})")
            else:
                parts.append(f"{label}: {n}")

        msg = (
            f"📦【新订单】{acc_name}\n"
            + "\n".join(parts)
            + f"\n时间：{t}\n"
            + "\n发送 /bind 绑定采购订单"
        )
        self._notify_owner(msg)
        self.on_log(f"[TG-PURCHASE] 通知新订单: {acc_name}")

    def notify_new_im(self, acc_name: str, new_im: int,
                      old_im: int, preview_items=None) -> None:
        """监控检测到 IM 新消息时调用，广播给所有注册用户。"""
        delta = new_im - old_im
        lines = [f"💬【即时通新消息】{acc_name}"]
        lines.append(f"新增：{delta} 条  当前未读：{new_im}")

        if preview_items:
            for it in preview_items[:5]:
                label = (it.get("label") or it.get("chat_id") or "未知").strip()
                unread_n = str(it.get("unread") or "").strip() or "1"
                preview = (it.get("preview") or "").strip()
                if preview and len(preview) > 80:
                    preview = preview[:80] + "..."
                lines.append(f"  {label} (未读{unread_n})")
                if preview:
                    lines.append(f"    {preview}")

        t = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"时间：{t}")

        self._notify_owner("\n".join(lines))
        self.on_log(f"[TG-PURCHASE] 通知IM新消息: {acc_name} +{delta}")

    # ------------------------------------------------------------------
    # 推送：物流单号通知
    # ------------------------------------------------------------------

    def notify_tracking_found(self, link: PurchaseLink) -> None:
        """采购监控发现物流单号时调用，广播给所有注册用户。"""
        platform = "闲鱼" if link.platform == "xianyu" else "煤炉"
        msg = (
            f"🚚【采购已出货】\n"
            f"账号：{link.yahoo_acc_name}\n"
            f"Yahoo订单：{link.yahoo_order_no}\n"
            f"平台：{platform}\n"
            f"采购订单：{link.purchase_order_id}\n"
            f"物流单号：{link.tracking_no}\n"
            f"→ 已自动停止监控该订单"
        )
        self._notify_owner(msg)
        self.on_log(f"[TG-PURCHASE] 通知物流: {link.yahoo_order_no} → {link.tracking_no}")

    # ------------------------------------------------------------------
    # 推送：采购平台掉登提醒
    # ------------------------------------------------------------------

    def notify_login_required(self, platform: str, order_id: str = "") -> None:
        """采购监控检测到需要登录时调用，广播给所有注册用户。"""
        plat_name = "闲鱼" if platform == "xianyu" else "煤炉"
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🔑【采购平台需登录】\n"
            f"平台：{plat_name}\n"
            f"时间：{t}\n"
            f"→ 请尽快在采购监控 Profile 中重新登录"
        )
        self._notify_owner(msg)
        self.on_log(f"[TG-PURCHASE] 通知掉登提醒: {plat_name}")

    # ------------------------------------------------------------------
    # 推送：出货资料已生成
    # ------------------------------------------------------------------

    def notify_export_done(self, acc_name: str, order_count: int,
                           success_count: int, fail_count: int) -> None:
        """出货资料生成完成时调用，广播给所有注册用户。"""
        t = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"📋【出货资料已生成】"]
        lines.append(f"账号：{acc_name}")
        lines.append(f"总计：{order_count} 单")
        if success_count:
            lines.append(f"成功：{success_count} 单")
        if fail_count:
            lines.append(f"失败：{fail_count} 单")
        lines.append(f"时间：{t}")
        self._notify_owner("\n".join(lines))
        self.on_log(f"[TG-PURCHASE] 通知出货完成: {acc_name} {success_count}/{order_count}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _send(self, chat_id: str, text: str,
              reply_markup: Optional[Dict] = None) -> None:
        try:
            self.tg.send_to(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            self.on_log(f"[TG-PURCHASE] 发送失败: {e}")

    def _get_account_list(self) -> list:
        """获取监控中的账号列表。"""
        return [a for a in self._monitor_accounts
                if getattr(a, "monitor_selected", True)]

    def _refresh_gui(self) -> None:
        """通知 GUI 刷新采购绑定表格。"""
        if self.purchase_tab and hasattr(self.purchase_tab, "_ui_queue"):
            try:
                self.purchase_tab._ui_queue.put(("refresh", None))
            except Exception:
                pass

    def _send_main_menu(self, chat_id: str) -> None:
        """v6.0.75:動態主菜單 — 帶採購監控狀態欄 + 重新分組。
        失敗時 fallback 到 tg.bot 內建的舊版菜單(保證可用性)。
        """
        try:
            text = self._build_main_menu_text()
            kb = self._build_main_menu_kb()
            self.tg.send_to(chat_id, text, reply_markup=kb)
        except Exception as e:
            self.on_log(f"[TG-PURCHASE] 動態主菜單異常,退回舊版: {e}")
            self.tg._send_main_menu(chat_id)

    def _build_main_menu_text(self) -> str:
        """組裝主菜單頂部狀態文字 — 一眼看採購監控狀況。"""
        app = self.app
        lines = ["📦 【採購訂單管理】\n"]

        # 監控狀態
        # tab 名於 v5.x 改為 purchase_ship_tab,舊版兼容 purchase_tab
        mon = "⏸"
        try:
            if app:
                purchase_tab = (getattr(app, "purchase_ship_tab", None)
                                or getattr(app, "purchase_tab", None))
                if purchase_tab:
                    pt = getattr(purchase_tab, "_thread", None)
                    if pt and pt.is_alive():
                        mon = "✅"
        except Exception:
            pass

        # 綁定統計
        try:
            links = load_links() or []
        except Exception:
            links = []
        total = len(links)
        watching = sum(1 for x in links if x.watch)
        need_login = sum(1 for x in links if x.status == "需登錄" or x.status == "需登录")
        got_track = sum(1 for x in links if x.status == "已獲取單號" or x.status == "已获取单号")
        waiting = sum(1 for x in links if x.status in ("等待出貨", "等待出货", "待監控", "待监控"))

        lines.append(f"🔍 監控 {mon}  |  📋 綁定 {total} 條")
        lines.append(f"👀 監控中:{watching}  ⏳ 等待中:{waiting}")
        if got_track:
            lines.append(f"✅ 已抓單:{got_track} 條")
        if need_login:
            lines.append(f"⚠️ 需登錄:{need_login} 條")

        lines.append("")
        lines.append("選擇功能:")
        return "\n".join(lines)

    def _build_main_menu_kb(self) -> Dict:
        """重新分組的主菜單按鈕 — 全部保留舊版 callback_data 不破壞向下相容。"""
        return self.tg.make_keyboard([
            # ━ 查詢(最常用)━
            [
                {"text": "📊 系統狀態", "callback_data": "menu:status"},
                {"text": "📋 綁定列表", "callback_data": "menu:list"},
            ],
            [
                {"text": "🔍 監控狀態", "callback_data": "menu:tracking"},
                {"text": "📦 最近訂單", "callback_data": "menu:orders"},
            ],
            # ━ 綁定操作 ━
            [
                {"text": "➕ 新建綁定", "callback_data": "menu:bind"},
                {"text": "✏️ 修改綁定", "callback_data": "menu:edit"},
            ],
            # ━ 監控控制 ━
            [
                {"text": "▶️ 啟動監控", "callback_data": "menu:startmon"},
                {"text": "⏹ 停止監控", "callback_data": "menu:stopmon"},
            ],
            [
                {"text": "🔄 手動抓取", "callback_data": "menu:fetch"},
            ],
            # ━ 其他 ━
            [
                {"text": "❌ 取消當前操作", "callback_data": "menu:cancel"},
            ],
        ] + self._build_cross_bot_jump_rows())

    def _build_cross_bot_jump_rows(self) -> list:
        """v6.0.76:跨 bot 跳轉 — 主菜單底部一行 url 按鈕,點擊直接切換到其他 bot 對話。"""
        try:
            from core.tg_bot_registry import get_all_jump_buttons
            btns = get_all_jump_buttons(exclude_kind="purchase")
            if btns:
                return [btns]
        except Exception:
            pass
        return []

    def _handle_menu_cb(self, chat_id: str, parts: List[str]) -> None:
        """处理主菜单按钮。"""
        action = parts[1] if len(parts) > 1 else ""
        if action == "home":
            self._send_main_menu(chat_id)
        elif action == "status":
            self._show_status(chat_id)
        elif action == "list":
            self._show_list(chat_id)
        elif action == "tracking":
            self._show_tracking(chat_id)
        elif action == "orders":
            self._show_orders(chat_id)
        elif action == "bind":
            self._start_bind(chat_id)
        elif action == "edit":
            self._start_edit(chat_id)
        elif action == "startmon":
            self._cmd_startmon(chat_id)
        elif action == "stopmon":
            self._cmd_stopmon(chat_id)
        elif action == "fetch":
            self._cmd_fetch(chat_id)
        elif action == "cancel":
            self._cancel_session(chat_id)

    def _cancel_session(self, chat_id: str) -> None:
        with self._session_lock:
            self._bind_sessions.pop(chat_id, None)
            self._edit_sessions.pop(chat_id, None)
        self._send(chat_id, "已取消当前操作。")
        self._send_main_menu(chat_id)

    # ------------------------------------------------------------------
    # 按钮回调：账号选择（bind 流程）
    # ------------------------------------------------------------------

    def _handle_acc_cb(self, chat_id: str, parts: List[str]) -> None:
        idx = int(parts[1]) if len(parts) > 1 else -1
        with self._session_lock:
            sess = self._bind_sessions.get(chat_id)
        if not sess or sess.step != BindStep.WAIT_ACCOUNT:
            return
        accounts = self._get_account_list()
        if 0 <= idx < len(accounts):
            acc = accounts[idx]
            sess.acc_name = acc.name
            sess.profile_id = acc.profile_id
            sess.step = BindStep.WAIT_YAHOO_ORDER
            self._send(chat_id, f"已选择：{acc.name}\n请输入 Yahoo 订单号：")

    # ------------------------------------------------------------------
    # 按钮回调：平台选择（bind 流程）
    # ------------------------------------------------------------------

    def _handle_plat_cb(self, chat_id: str, parts: List[str]) -> None:
        plat = parts[1] if len(parts) > 1 else ""
        with self._session_lock:
            sess = self._bind_sessions.get(chat_id)
        if not sess or sess.step != BindStep.WAIT_PLATFORM:
            return
        if plat in ("xianyu", "mercari"):
            sess.platform = plat
            sess.step = BindStep.WAIT_PURCHASE_ID
            name = "闲鱼" if plat == "xianyu" else "煤炉"
            self._send(chat_id, f"平台：{name}\n请输入采购订单号（多个用逗号分隔）：")

    # ------------------------------------------------------------------
    # 按钮回调：跳过（bind 流程中的可选步骤）
    # ------------------------------------------------------------------

    def _handle_skip_cb(self, chat_id: str, parts: List[str]) -> None:
        field = parts[1] if len(parts) > 1 else ""
        with self._session_lock:
            sess = self._bind_sessions.get(chat_id)
        if not sess:
            return
        if field == "sub_orders" and sess.step == BindStep.WAIT_SUB_ORDERS:
            sess.sub_order_nos = []
            self._goto_platform_step(chat_id, sess)

    # ------------------------------------------------------------------
    # 按钮回调：监控切换
    # ------------------------------------------------------------------

    def _handle_watch_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 3:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        enable = parts[2] == "on"
        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, "序号无效。")
            return
        link = links[idx]
        link.watch = enable
        save_links(links)
        self._refresh_gui()
        action = "已开启监控 👁" if enable else "已取消监控 🔕"
        platform = "闲鱼" if link.platform == "xianyu" else "煤炉"
        self._send(
            chat_id,
            f"{action}: #{idx+1}\n"
            f"{link.yahoo_acc_name} | {link.yahoo_order_no}\n"
            f"{platform} {link.purchase_order_id}",
        )

    # ------------------------------------------------------------------
    # 按钮回调 / 指令：查看详情
    # ------------------------------------------------------------------

    def _handle_detail_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 2:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        self._show_detail_by_index(chat_id, idx)

    def _show_detail(self, chat_id: str, text: str) -> None:
        """处理 /detail N 指令。"""
        parts = text.split()
        if len(parts) < 2:
            self._send(chat_id, "用法: /detail 序号\n例: /detail 1")
            return
        try:
            idx = int(parts[1]) - 1
        except ValueError:
            self._send(chat_id, "请输入有效的序号。")
            return
        self._show_detail_by_index(chat_id, idx)

    def _show_detail_by_index(self, chat_id: str, idx: int) -> None:
        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, f"序号无效（共 {len(links)} 条）。")
            return
        x = links[idx]
        platform = "闲鱼" if x.platform == "xianyu" else "煤炉"
        subs = ", ".join(x.sub_order_nos) if x.sub_order_nos else "-"
        watch_str = "是 👁" if x.watch else "否"
        lines = [
            f"📄【详情 #{idx+1}】",
            f"账号：{x.yahoo_acc_name}",
            f"Yahoo订单：{x.yahoo_order_no}",
            f"副订单：{subs}",
            f"平台：{platform}",
            f"采购订单：{x.purchase_order_id}",
            f"商品：{x.product_name or '-'}",
            f"规格：{x.spec or '-'}",
            f"备注：{x.remark or '-'}",
            f"代付金额：{x.pay_amount or '-'}",
            f"付款时间：{x.pay_dt_raw or '-'}",
            f"物流单号：{x.tracking_no or '-'}",
            f"状态：{x.status}",
            f"监控：{watch_str}",
        ]
        w_text = "🔕取消监控" if x.watch else "👁开启监控"
        w_data = f"watch:{idx}:{'off' if x.watch else 'on'}"
        rows = [
            [
                {"text": "✏️ 修改", "callback_data": f"edit:{idx}"},
                {"text": w_text, "callback_data": w_data},
            ],
        ]
        # 有物流单号时显示出货按钮
        if (x.tracking_no or "").strip():
            rows.append([
                {"text": "📦 创建出货任务", "callback_data": f"ship:{idx}"},
            ])
        rows.append([
            {"text": "🗑 删除", "callback_data": f"del:{idx}"},
            {"text": "📋 列表", "callback_data": "menu:list"},
        ])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "\n".join(lines), reply_markup=kb)

    # ------------------------------------------------------------------
    # 按钮回调：编辑（选择记录 → 显示可修改字段）
    # ------------------------------------------------------------------

    def _handle_edit_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 2:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        self._show_edit_fields(chat_id, idx)

    def _show_edit_fields(self, chat_id: str, idx: int) -> None:
        """显示可修改字段的按钮列表。"""
        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, "序号无效。")
            return
        x = links[idx]
        rows = []
        for fkey, flabel in EDITABLE_FIELDS.items():
            val = getattr(x, fkey, "") or ""
            if isinstance(val, list):
                val = ", ".join(val) if val else "-"
            disp = val[:15] if val else "-"
            rows.append([{
                "text": f"{flabel}: {disp}",
                "callback_data": f"editf:{idx}:{fkey}",
            }])
        rows.append([
            {"text": "📄 详情", "callback_data": f"detail:{idx}"},
            {"text": "❌ 取消", "callback_data": "menu:list"},
        ])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, f"✏️ 修改 #{idx+1} - 选择要修改的字段：", reply_markup=kb)

    # ------------------------------------------------------------------
    # 按钮回调：选择要修改的字段
    # ------------------------------------------------------------------

    def _handle_editfield_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 3:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        fkey = parts[2]
        if fkey not in EDITABLE_FIELDS:
            return
        flabel = EDITABLE_FIELDS[fkey]
        with self._session_lock:
            self._edit_sessions[chat_id] = EditSession(
                step=EditStep.WAIT_VALUE,
                link_index=idx,
                field_name=fkey,
                created_at=time.time(),
            )
        self._send(
            chat_id,
            f"请输入 #{idx+1} 的新【{flabel}】值：\n"
            f"（副订单编号用逗号分隔，平台输入 xianyu 或 mercari）",
        )

    # ------------------------------------------------------------------
    # 编辑会话：处理用户输入的新值
    # ------------------------------------------------------------------

    def _handle_edit_input(self, chat_id: str, text: str) -> None:
        sess = self._edit_sessions.get(chat_id)
        if not sess:
            return
        if time.time() - sess.created_at > SESSION_TIMEOUT:
            del self._edit_sessions[chat_id]
            self._send(chat_id, "修改操作已超时。")
            return
        if sess.step != EditStep.WAIT_VALUE:
            return
        self._apply_edit(chat_id, sess, text)

    def _apply_edit(self, chat_id: str, sess: EditSession, text: str) -> None:
        links = load_links()
        idx = sess.link_index
        if idx < 0 or idx >= len(links):
            del self._edit_sessions[chat_id]
            self._send(chat_id, "记录已不存在。")
            return

        link = links[idx]
        fkey = sess.field_name
        flabel = EDITABLE_FIELDS.get(fkey, fkey)

        if fkey == "sub_order_nos":
            val = [x.strip() for x in text.replace("，", ",").replace("+", ",").split(",") if x.strip()]
            link.sub_order_nos = val[:5]
            disp = ", ".join(link.sub_order_nos) or "-"
        elif fkey == "platform":
            t = text.strip().lower()
            if t in ("xianyu", "闲鱼", "1"):
                link.platform = "xianyu"
            elif t in ("mercari", "煤炉", "2"):
                link.platform = "mercari"
            else:
                self._send(chat_id, "无效平台，请输入 xianyu 或 mercari：")
                return
            disp = "闲鱼" if link.platform == "xianyu" else "煤炉"
        else:
            setattr(link, fkey, text.strip())
            disp = text.strip()

        save_links(links)
        del self._edit_sessions[chat_id]
        self._refresh_gui()

        self._send(chat_id, f"✅ 已修改 #{idx+1} 的【{flabel}】为：{disp}")
        self._show_detail_by_index(chat_id, idx)

    # ------------------------------------------------------------------
    # 按钮回调：删除记录
    # ------------------------------------------------------------------

    def _handle_delete_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 2:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, "序号无效。")
            return
        removed = links.pop(idx)
        save_links(links)
        self._refresh_gui()
        platform = "闲鱼" if removed.platform == "xianyu" else "煤炉"
        self._send(
            chat_id,
            f"🗑 已删除 #{idx+1}\n"
            f"{removed.yahoo_acc_name} | {removed.yahoo_order_no}\n"
            f"{platform} {removed.purchase_order_id}",
        )
        self.on_log(
            f"[TG-PURCHASE] 删除: {removed.yahoo_order_no} "
            f"{platform} {removed.purchase_order_id}"
        )

    # ------------------------------------------------------------------
    # /edit 入口：选择要修改的记录
    # ------------------------------------------------------------------

    def _start_edit(self, chat_id: str) -> None:
        links = load_links()
        if not links:
            self._send(chat_id, "暂无记录可修改。")
            return
        rows = []
        for i, x in enumerate(links):
            plat = "闲" if x.platform == "xianyu" else "煤"
            label = f"{i+1}. {x.yahoo_acc_name} {plat} {x.purchase_order_id[:10]}"
            rows.append([{"text": label, "callback_data": f"edit:{i}"}])
        rows.append([{"text": "❌ 取消", "callback_data": "menu:home"}])
        kb = self.tg.make_keyboard(rows)
        self._send(chat_id, "✏️ 选择要修改的记录：", reply_markup=kb)

    # ------------------------------------------------------------------
    # /watchall /unwatchall 批量切换监控
    # ------------------------------------------------------------------

    def _cmd_watch_all(self, chat_id: str, enable: bool) -> None:
        links = load_links()
        if not links:
            self._send(chat_id, "暂无记录。")
            return
        for x in links:
            x.watch = enable
        save_links(links)
        self._refresh_gui()
        action = "全部开启监控 👁" if enable else "全部取消监控 🔕"
        self._send(chat_id, f"✅ {action}（共 {len(links)} 条）")

    # ------------------------------------------------------------------
    # /ship N  创建出货任务
    # ------------------------------------------------------------------

    def _cmd_ship(self, chat_id: str, text: str) -> None:
        tab = self.purchase_tab
        if not tab or not getattr(tab, "on_create_ship_task", None):
            self._send(chat_id, "出货模块未加载，请在 GUI 中操作。")
            return

        parts = text.split()
        if len(parts) < 2:
            self._send(
                chat_id,
                "用法: /ship 序号\n例: /ship 1\n\n发送 /list 查看序号",
            )
            return

        try:
            idx = int(parts[1]) - 1
        except ValueError:
            self._send(chat_id, "请输入有效的序号数字。")
            return

        links = load_links()
        if idx < 0 or idx >= len(links):
            self._send(chat_id, f"序号超出范围（共 {len(links)} 条）。")
            return

        link = links[idx]
        if not (link.tracking_no or "").strip():
            self._send(
                chat_id,
                f"#{idx+1} 尚无物流单号，无法创建出货任务。\n"
                "请先等待采购监控抓取物流信息。",
            )
            return

        try:
            payload = tab._make_ship_task_payload(link)
            tab.on_create_ship_task(payload)
        except Exception as e:
            self._send(chat_id, f"创建出货任务失败: {e}")
            return

        platform = "闲鱼" if link.platform == "xianyu" else "煤炉"
        self._send(
            chat_id,
            f"✅ 已创建出货任务\n"
            f"账号：{link.yahoo_acc_name}\n"
            f"Yahoo订单：{link.yahoo_order_no}\n"
            f"{platform} {link.purchase_order_id}\n"
            f"物流：{link.tracking_no}",
        )
        self.on_log(
            f"[TG-PURCHASE] 创建出货: {link.yahoo_order_no} "
            f"→ {link.tracking_no}"
        )

    # ------------------------------------------------------------------
    # 按钮回调：创建出货任务
    # ------------------------------------------------------------------

    def _handle_ship_cb(self, chat_id: str, parts: List[str]) -> None:
        if len(parts) < 2:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        # 复用 /ship 逻辑（内部用 0-based index，/ship 用 1-based）
        self._cmd_ship(chat_id, f"/ship {idx + 1}")
