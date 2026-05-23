"""日台線路 Tab — 监控面板 + 自动出货。

功能：
  1. 自动接收采购出货的 mercari tracking → 创建出货记录
  2. 后台监控：检测入库 → 提交集运 → 跟踪物流 → 触发 Yahoo 出货
  3. 状态跟踪：待发货 → 已发货 → 已入库 → 已提交 → 监控中 → 已出货
  4. 宅配: ship_order_http → 填 tracking  /  超商: ship_order_http → 下载面单

数据持久化：suda68_shipments.json
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import suda68_http_ops as _api

_log_mod = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT_DIR / "suda68_shipments.json"
_data_lock = threading.Lock()

_ACCESS_PASSWORD = "4577"

# 触发级别: 越往右越晚触发
_TRIGGER_LEVELS = {"已发货": 1, "已集货": 2, "配送中": 3, "已签收": 4}

# 第三方超商地址固定参数
_THIRD_PARTY_PROVINCE = 12402      # 台北市 → 但超商地址已固定
_THIRD_PARTY_CITY = 35347          # 信義區 parent city
_THIRD_PARTY_AREA = 0


def _resolve_taiwan_address(
    session,
    address_text: str,
    country_id: int = _api.DEFAULT_COUNTRY,
    log=None,
) -> Tuple[int, int, int]:
    """解析台湾地址文本 → (province_id, city_id, area_id)。

    台湾在 suda68 的层级:
      Province = 台灣 (12402) — 唯一
      City = 高雄市/台北市/新北市... (24个)
      Area = 鳳山區/信義區... (各市下辖区)

    Args:
        address_text: 完整地址，如 "高雄市鳳山區鳳北路117號"
    Returns:
        (province_id, city_id, area_id)
    """
    def _get_list(url: str, params: dict) -> list:
        try:
            r = session.post(f"{_api.SUDA_BASE}{url}", data=params, timeout=30)
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # 1. Province — 台灣固定 12402
    provinces = _get_list("/GetData/GetProvince", {"Countryid": country_id, "IsAgent": False})
    province_id = provinces[0].get("ID", 12402) if provinces else 12402

    # 2. City — 匹配 高雄市/台北市/新北市 等
    cities = _get_list("/GetData/GetCity", {"Provinceid": province_id, "IsAgent": False})
    if not cities:
        return province_id, 0, 0

    # 台↔臺 变体处理
    addr_variants = [address_text, address_text.replace("台", "臺"), address_text.replace("臺", "台")]

    city_id = 0
    for c in cities:
        name = c.get("Name", "")
        if name and any(name in v for v in addr_variants):
            city_id = c.get("ID", 0)
            break
    if not city_id:
        for c in cities:
            name = c.get("Name", "").rstrip("市縣")
            if name and len(name) >= 2 and any(name in v for v in addr_variants):
                city_id = c.get("ID", 0)
                break
    if not city_id:
        return province_id, 0, 0

    # 3. Area — 匹配 鳳山區/信義區 等
    areas = _get_list("/GetData/GetArea", {"Cityid": city_id, "IsAgent": False})
    area_id = 0
    for a in areas:
        name = a.get("Name", "")
        if name and any(name in v for v in addr_variants):
            area_id = a.get("ID", 0)
            break
    if not area_id:
        for a in areas:
            name = a.get("Name", "").rstrip("區鄉鎮")
            if name and len(name) >= 2 and any(name in v for v in addr_variants):
                area_id = a.get("ID", 0)
                break

    return province_id, city_id, area_id


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据结构                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

@dataclass
class SudaShipment:
    """一条日台出货记录。"""
    # 关联
    yahoo_order_no: str = ""
    yahoo_acc: str = ""
    yahoo_profile_id: str = ""         # Chrome profile ID
    purchase_order_id: str = ""        # 煤炉采购订单号
    mercari_tracking: str = ""         # 煤炉日本国内快递单号
    group_key: str = ""                # "{profile_id}:{yahoo_order_no}"

    # 收件信息 (自动获取)
    ship_method: str = ""              # "宅配" | "超商" (Yahoo订单确定后填入)
    receiver_name: str = ""
    receiver_phone: str = ""
    receiver_address: str = ""
    goods_type: int = 10212            # 10212=普货
    goods_name: str = ""
    remark: str = ""

    # 集运相关
    suda_package_id: str = ""          # 入库后的包裹 ID
    suda_order_id: str = ""            # 提交后的集运订单 ID
    suda_order_code: str = ""          # 集运订单号
    suda_address_id: int = 0           # 收货地址 ID
    suda_freight: float = 0.0          # 运费
    suda_tracking_code: str = ""       # 发货主号 (台湾配送单号)
    suda_delivery_status: str = ""     # suda68 最新物流状态

    # 状态
    status: str = "待发货"
    # 待发货 → 已发货 → 已入库 → 已提交 → 监控中 → 已出货
    watch: bool = True
    created_at: str = ""
    updated_at: str = ""
    error: str = ""


# ── 数据持久化 ──

def _atomic_write(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_shipments() -> List[SudaShipment]:
    with _data_lock:
        if not DATA_FILE.exists():
            return []
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            out: List[SudaShipment] = []
            fields = set(SudaShipment.__dataclass_fields__.keys())
            for x in raw if isinstance(raw, list) else []:
                if not isinstance(x, dict):
                    continue
                kwargs = {k: x.get(k) for k in fields if k in x}
                out.append(SudaShipment(**kwargs))
            return out
        except Exception:
            return []


def save_shipments(items: List[SudaShipment]) -> None:
    with _data_lock:
        _atomic_write(DATA_FILE, [asdict(x) for x in items])


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ╔══════════════════════════════════════════════════════════════╗
# ║  Tab UI                                                     ║
# ╚══════════════════════════════════════════════════════════════╝

class Suda68Tab:
    """日台線路管理 Tab — 监控面板。"""

    def __init__(self, app: Any, frame: tk.Widget):
        self.app = app
        self.frame = frame
        self._authenticated = False
        self.shipments: List[SudaShipment] = []
        self._ui_queue: queue.Queue = queue.Queue()
        self._monitor_running = False
        self._monitor_thread: Optional[threading.Thread] = None

        # 读取 settings
        settings = getattr(app, "settings", {}) if app else {}
        self._username = settings.get("suda68_username", "<PHONE_REDACTED>")
        self._password = settings.get("suda68_password", "<SSH_PASSWORD_REDACTED>")
        self._third_party_name = settings.get("suda68_third_party_name", "于子晴")
        self._third_party_phone = settings.get("suda68_third_party_phone", "<PHONE_REDACTED>")
        self._third_party_address = settings.get(
            "suda68_third_party_address", "台北市信義區嘉興街227號3樓（鍾）"
        )
        self._monitor_interval = int(settings.get("suda68_monitor_interval", 600))
        self._auto_submit = bool(settings.get("suda68_auto_submit", False))
        self._chrome_path = settings.get(
            "browser_path", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        )

        self._build_lock_screen()

    # ══════════════════════════════════════════════════════════════
    # 密码锁定
    # ══════════════════════════════════════════════════════════════

    def _build_lock_screen(self) -> None:
        self._lock_frame = ttk.Frame(self.frame)
        self._lock_frame.place(relx=0.5, rely=0.4, anchor="center")

        ttk.Label(self._lock_frame, text="此页面需要密码访问",
                  font=("", 14, "bold")).pack(pady=(0, 16))

        row = ttk.Frame(self._lock_frame)
        row.pack()
        ttk.Label(row, text="密码:").pack(side="left", padx=(0, 6))
        self._var_pw = tk.StringVar()
        self._ent_pw = ttk.Entry(row, textvariable=self._var_pw, show="*", width=16)
        self._ent_pw.pack(side="left", padx=(0, 6))
        self._ent_pw.bind("<Return>", lambda e: self._verify_password())
        ttk.Button(row, text="确认", command=self._verify_password).pack(side="left")

        self._lbl_pw_err = ttk.Label(self._lock_frame, text="", foreground="red")
        self._lbl_pw_err.pack(pady=(8, 0))
        self._ent_pw.focus_set()

    def _verify_password(self) -> None:
        if self._var_pw.get() == _ACCESS_PASSWORD:
            self._authenticated = True
            self._lock_frame.destroy()
            self._init_main_ui()
        else:
            self._lbl_pw_err.configure(text="密码错误")
            self._var_pw.set("")
            self._ent_pw.focus_set()

    def _init_main_ui(self) -> None:
        self.shipments = load_shipments()

        # UI 变量
        self.var_interval = tk.StringVar(value=str(self._monitor_interval))
        self.var_auto_submit = tk.BooleanVar(value=self._auto_submit)
        self.var_auto_ship = tk.BooleanVar(value=False)
        self.var_trigger_level = tk.StringVar(value="已发货")

        self._build_ui()
        self._render_tree()
        self._update_stats()
        self.frame.after(400, self._poll_ui_queue)

    # ══════════════════════════════════════════════════════════════
    # 构建主界面
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        # ---- 监控控制 ----
        lf = ttk.Labelframe(self.frame, text="监控控制")
        lf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        # Row 0: 启用 + 间隔 + 按钮
        r0 = ttk.Frame(lf)
        r0.pack(fill="x", padx=4, pady=3)

        ttk.Label(r0, text="监控间隔:").pack(side="left")
        ttk.Entry(r0, textvariable=self.var_interval, width=6).pack(side="left", padx=(2, 2))
        ttk.Label(r0, text="秒").pack(side="left", padx=(0, 12))

        self.btn_monitor = ttk.Button(r0, text="开始监控", command=self._action_start_monitor)
        self.btn_monitor.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(r0, text="停止", command=self._action_stop_monitor)
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_stop.pack_forget()

        ttk.Button(r0, text="立即检查", command=self._action_check_now).pack(side="left", padx=(0, 12))

        self.lbl_stats = ttk.Label(r0, text="")
        self.lbl_stats.pack(side="right", padx=4)

        # Row 1: 触发级别 + 自动选项
        r1 = ttk.Frame(lf)
        r1.pack(fill="x", padx=4, pady=3)

        ttk.Label(r1, text="触发级别:").pack(side="left", padx=(0, 4))
        for lv in ("已发货", "已集货", "配送中"):
            ttk.Radiobutton(
                r1, text=lv, variable=self.var_trigger_level, value=lv
            ).pack(side="left", padx=(0, 8))

        ttk.Separator(r1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Checkbutton(r1, text="自动提交", variable=self.var_auto_submit).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(r1, text="自动出货", variable=self.var_auto_ship).pack(side="left", padx=(0, 8))

        # Row 2: 手动操作按钮
        r2 = ttk.Frame(lf)
        r2.pack(fill="x", padx=4, pady=(3, 6))

        ttk.Button(r2, text="手动提交", command=self._action_submit).pack(side="left", padx=(0, 8))
        ttk.Button(r2, text="手动出货", command=self._action_ship).pack(side="left", padx=(0, 8))
        ttk.Button(r2, text="删除选中", command=self._action_delete).pack(side="left", padx=(0, 8))

        # ---- 出货记录 ----
        lf_table = ttk.Labelframe(self.frame, text="出货记录")
        lf_table.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        lf_table.columnconfigure(0, weight=1)
        lf_table.rowconfigure(0, weight=1)

        cols = (
            "watch", "yahoo_acc", "yahoo_order", "purchase_order",
            "mercari_tracking", "ship_method", "receiver",
            "delivery_status", "status", "error",
        )
        self.tree = ttk.Treeview(lf_table, columns=cols, show="headings")

        headings = {
            "watch": "监控",
            "yahoo_acc": "账号",
            "yahoo_order": "Yahoo订单",
            "purchase_order": "煤炉订单",
            "mercari_tracking": "日本快递",
            "ship_method": "配送",
            "receiver": "收件人",
            "delivery_status": "物流状态",
            "status": "状态",
            "error": "错误",
        }
        widths = {
            "watch": 45, "yahoo_acc": 110, "yahoo_order": 130,
            "purchase_order": 130, "mercari_tracking": 140,
            "ship_method": 50, "receiver": 80,
            "delivery_status": 80, "status": 60, "error": 160,
        }
        for c in cols:
            self.tree.heading(c, text=headings.get(c, c))
            self.tree.column(c, width=widths.get(c, 100), anchor="w", stretch=True)
        self.tree.column("watch", anchor="center", stretch=False, width=45)
        self.tree.column("ship_method", anchor="center", stretch=False, width=50)
        self.tree.column("status", anchor="center", stretch=False, width=60)

        vsb = ttk.Scrollbar(lf_table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(lf_table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_tree_click)

        # ---- 日志区 ----
        self.txt = tk.Text(self.frame, height=6, wrap="word", state="normal")
        self.txt.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

    # ══════════════════════════════════════════════════════════════
    # Treeview
    # ══════════════════════════════════════════════════════════════

    def _render_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, s in enumerate(self.shipments):
            self.tree.insert("", "end", iid=str(i), values=(
                "V" if s.watch else "",
                s.yahoo_acc,
                s.yahoo_order_no,
                s.purchase_order_id,
                s.mercari_tracking,
                s.ship_method,
                s.receiver_name,
                s.suda_delivery_status,
                s.status,
                s.error,
            ))

    def _on_tree_click(self, event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        if 0 <= idx < len(self.shipments):
            self.shipments[idx].watch = not self.shipments[idx].watch
            save_shipments(self.shipments)
            self._render_tree()

    def _update_stats(self) -> None:
        c = {}
        for s in self.shipments:
            c[s.status] = c.get(s.status, 0) + 1
        text = (
            f"发:{c.get('已发货', 0)} "
            f"库:{c.get('已入库', 0)} "
            f"提:{c.get('已提交', 0)} "
            f"监:{c.get('监控中', 0)} "
            f"出:{c.get('已出货', 0)}"
        )
        if hasattr(self, "lbl_stats"):
            self.lbl_stats.configure(text=text)

    # ══════════════════════════════════════════════════════════════
    # UI 操作
    # ══════════════════════════════════════════════════════════════

    def _action_delete(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的记录")
            return
        indices = sorted([int(s) for s in sel], reverse=True)
        for idx in indices:
            if 0 <= idx < len(self.shipments):
                removed = self.shipments.pop(idx)
                self._log(f"已删除: {removed.yahoo_order_no}")
        save_shipments(self.shipments)
        self._render_tree()
        self._update_stats()

    def _action_check_now(self) -> None:
        """立即执行一轮检查。"""
        self._log("手动触发检查...")
        threading.Thread(target=self._monitor_tick, daemon=True).start()

    def _action_submit(self) -> None:
        """手动提交选中的已入库记录。"""
        sel = self.tree.selection()
        targets = []
        if sel:
            for s in sel:
                idx = int(s)
                if 0 <= idx < len(self.shipments):
                    sh = self.shipments[idx]
                    if sh.status == "已入库":
                        targets.append(sh)
        else:
            targets = [s for s in self.shipments if s.status == "已入库" and s.watch]
        if not targets:
            messagebox.showinfo("提示", "没有已入库且可提交的记录")
            return
        if not messagebox.askyesno("确认", f"确认提交 {len(targets)} 条记录？"):
            return
        self._log(f"手动提交: {len(targets)} 条")
        threading.Thread(
            target=self._submit_ready_groups, args=(targets,), daemon=True
        ).start()

    def _action_ship(self) -> None:
        """手动触发 Yahoo 出货。"""
        sel = self.tree.selection()
        targets = []
        if sel:
            for s in sel:
                idx = int(s)
                if 0 <= idx < len(self.shipments):
                    sh = self.shipments[idx]
                    if sh.status in ("已提交", "监控中"):
                        targets.append(sh)
        else:
            targets = [s for s in self.shipments
                       if s.status in ("已提交", "监控中") and s.watch]
        if not targets:
            messagebox.showinfo("提示", "没有可出货的记录")
            return
        if not messagebox.askyesno("确认", f"确认对 {len(targets)} 条记录执行 Yahoo 出货？"):
            return
        self._log(f"手动出货: {len(targets)} 条")
        threading.Thread(
            target=self._execute_yahoo_ship_batch, args=(targets,), daemon=True
        ).start()

    # ══════════════════════════════════════════════════════════════
    # 监控
    # ══════════════════════════════════════════════════════════════

    def _action_start_monitor(self) -> None:
        if self._monitor_running:
            return
        self._monitor_running = True
        self.btn_monitor.pack_forget()
        self.btn_stop.pack(side="left", padx=(0, 6))
        try:
            self._monitor_interval = int(self.var_interval.get())
        except ValueError:
            self._monitor_interval = 600
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        self._log(f"监控已启动 (间隔 {self._monitor_interval}s)")

    def _action_stop_monitor(self) -> None:
        self._monitor_running = False
        self.btn_stop.pack_forget()
        self.btn_monitor.pack(side="left", padx=(0, 6))
        self._log("监控已停止")

    def _monitor_loop(self) -> None:
        while self._monitor_running:
            try:
                self._monitor_tick()
            except Exception as e:
                self._log(f"监控异常: {e}")
            for _ in range(self._monitor_interval):
                if not self._monitor_running:
                    break
                time.sleep(1)
        self._ui_queue.put(("monitor_stopped", None))

    def _monitor_tick(self) -> None:
        """单次监控 — 三阶段。"""
        # Phase A: 入库检查 (已发货 → 已入库)
        shipped = [s for s in self.shipments
                   if s.status == "已发货" and s.watch and s.mercari_tracking]
        if shipped:
            self._log(f"[A] 检查入库: {len(shipped)} 条")
            self._check_warehouse_batch(shipped)

        # Phase B: 自动提交集运 (已入库 → 已提交)
        if self.var_auto_submit.get():
            instock = [s for s in self.shipments if s.status == "已入库" and s.watch]
            if instock:
                self._submit_ready_groups(instock)

        # Phase C: 物流跟踪 + 触发 (已提交/监控中 → 已出货)
        tracking = [s for s in self.shipments
                    if s.status in ("已提交", "监控中") and s.watch
                    and s.suda_order_id]
        if tracking:
            self._log(f"[C] 跟踪物流: {len(tracking)} 条")
            self._check_delivery_batch(tracking)

        self._ui_queue.put(("refresh", None))

    # ══════════════════════════════════════════════════════════════
    # Phase A: 入库检查
    # ══════════════════════════════════════════════════════════════

    def _check_warehouse_batch(self, targets: List[SudaShipment]) -> None:
        session, err = _api.ensure_session(self._username, self._password, self._log)
        if err:
            self._log(f"连接失败: {err}")
            return

        packages, err = _api.get_package_list(session, log=self._log)
        if err:
            self._log(f"获取包裹列表失败: {err}")
            return

        pkg_map = {p.bill_code: p for p in packages}
        for sh in targets:
            if not sh.mercari_tracking:
                continue
            pkg = pkg_map.get(sh.mercari_tracking)
            if pkg:
                sh.suda_package_id = pkg.package_id
                sh.status = "已入库"
                sh.error = ""
                sh.updated_at = _now_iso()
                self._log(f"已入库: {sh.yahoo_order_no}/{sh.purchase_order_id} → 包裹ID={pkg.package_id}")

        save_shipments(self.shipments)

    # ══════════════════════════════════════════════════════════════
    # Phase B: 提交集运
    # ══════════════════════════════════════════════════════════════

    def _get_siblings(self, sh: SudaShipment) -> List[SudaShipment]:
        """获取同一 Yahoo 订单的所有记录。"""
        return [s for s in self.shipments
                if s.yahoo_order_no == sh.yahoo_order_no
                and s.yahoo_profile_id == sh.yahoo_profile_id]

    def _group_ready_for_submit(self, sh: SudaShipment) -> bool:
        """同一 Yahoo 订单的所有包裹是否都已入库。"""
        siblings = self._get_siblings(sh)
        if not siblings:
            return False
        return all(
            s.status in ("已入库", "已提交", "监控中", "已出货")
            for s in siblings
        )

    def _submit_ready_groups(self, candidates: List[SudaShipment]) -> None:
        """对所有满足条件的组提交集运。"""
        seen_groups = set()
        for sh in candidates:
            gk = sh.group_key or f"{sh.yahoo_profile_id}:{sh.yahoo_order_no}"
            if gk in seen_groups:
                continue
            seen_groups.add(gk)
            if sh.status != "已入库":
                continue
            if not self._group_ready_for_submit(sh):
                missing = [s for s in self._get_siblings(sh)
                           if s.status not in ("已入库", "已提交", "监控中", "已出货")]
                self._log(f"等待: {sh.yahoo_order_no} 还有 {len(missing)} 个包裹未入库")
                continue
            self._submit_one_group(sh)

    def _submit_one_group(self, representative: SudaShipment) -> None:
        """提交一个 Yahoo 订单组到集运。"""
        siblings = [s for s in self._get_siblings(representative) if s.status == "已入库"]
        if not siblings:
            return

        # 1. 确定配送方式 (从 Yahoo 订单获取)
        ship_method = representative.ship_method
        if not ship_method:
            ship_method = self._detect_ship_method(representative)

        # 2. 确定收件地址
        if ship_method == "超商":
            recv_name = self._third_party_name
            recv_phone = self._third_party_phone
            recv_address = self._third_party_address
        else:
            # 宅配: 从 Yahoo 订单获取
            recv_name, recv_phone, recv_address = self._fetch_yahoo_receiver(representative)
            if not recv_name:
                for s in siblings:
                    s.error = "无法获取收件人信息"
                save_shipments(self.shipments)
                return

        # 更新所有 siblings 的收件信息
        for s in siblings:
            s.ship_method = ship_method
            s.receiver_name = recv_name
            s.receiver_phone = recv_phone
            s.receiver_address = recv_address

        # 3. 登录 suda68
        session, err = _api.ensure_session(self._username, self._password, self._log)
        if err:
            self._log(f"连接失败: {err}")
            return

        # 4. 查找/创建地址
        address_id = siblings[0].suda_address_id
        if not address_id:
            # 尝试从现有地址列表匹配 (名字+电话双重匹配, 避免同名误匹配)
            addrs, err = _api.get_address_list(session, log=self._log)
            if not err:
                for a in addrs:
                    if ship_method == "超商" and self._third_party_name in a.person:
                        address_id = a.address_id
                        break
                    elif ship_method == "宅配" and recv_name and recv_phone:
                        if recv_name in a.person and recv_phone in a.phone:
                            address_id = a.address_id
                            break

        if not address_id:
            # 自动创建地址
            self._log(f"未找到地址, 自动创建: {recv_name} {recv_phone}")
            prov_id, city_id, area_id = _resolve_taiwan_address(
                session, recv_address, log=self._log,
            )
            if not prov_id:
                self._log(f"无法解析地址区域: {recv_address}")
                for s in siblings:
                    s.error = f"无法解析地址区域: {recv_address}"
                save_shipments(self.shipments)
                return

            # 详细地址 = 去掉省市区前缀
            detail_address = recv_address
            # 找到区/鄉/鎮 后面的部分作为详细地址
            for pat in [r'[區鄉鎮]\s*', r'市\s*']:
                m = re.search(pat, detail_address)
                if m:
                    candidate = detail_address[m.end():]
                    if len(candidate) >= 4:
                        detail_address = candidate
                        break

            new_id, err = _api.add_address(
                session=session,
                person=recv_name,
                phone=recv_phone,
                country_id=_api.DEFAULT_COUNTRY,
                province_id=prov_id,
                city_id=city_id,
                area_id=area_id,
                address=detail_address,
                card=_api.DEFAULT_CARD,
                log=self._log,
            )
            if err or not new_id:
                for s in siblings:
                    s.error = f"创建地址失败: {err}"
                save_shipments(self.shipments)
                self._log(f"创建地址失败: {err}")
                return
            address_id = new_id
            self._log(f"地址已创建: ID={address_id} {recv_name}")

        # 5. 提交集运 (所有包裹合并一个订单)
        package_ids = [s.suda_package_id for s in siblings if s.suda_package_id]
        if not package_ids:
            return

        try:
            result = _api.submit_order(
                session=session,
                package_ids=package_ids,
                address_id=address_id,
                receiver_name=recv_name,
                receiver_phone=recv_phone,
                receiver_address=recv_address,
                goods_type=siblings[0].goods_type,
                goods_name=siblings[0].goods_name,
                remark=siblings[0].remark,
                card=_api.DEFAULT_CARD,
                pay_type=2,  # 貨到付款 (避免余额自动扣款)
                log=self._log,
            )
            if result.success:
                for s in siblings:
                    s.suda_order_id = result.order_id
                    s.suda_order_code = result.order_code
                    s.suda_freight = result.receivables
                    s.suda_address_id = address_id
                    s.status = "已提交"
                    s.error = ""
                    s.updated_at = _now_iso()
                self._log(
                    f"提交成功: {representative.yahoo_order_no} → "
                    f"订单号={result.order_code} 费用={result.receivables}"
                )
                self._notify_tg(
                    f"日台提交: {representative.yahoo_acc}/{representative.yahoo_order_no} "
                    f"→ {result.order_code}"
                )
            else:
                for s in siblings:
                    s.error = result.error
                self._log(f"提交失败: {representative.yahoo_order_no} → {result.error}")
        except Exception as e:
            for s in siblings:
                s.error = str(e)
            self._log(f"提交异常: {representative.yahoo_order_no} → {e}")

        save_shipments(self.shipments)

    def _detect_ship_method(self, sh: SudaShipment) -> str:
        """从 Yahoo 订单判断配送方式 (宅配/超商)。"""
        try:
            from core.ship_http_ops import fetch_order_shipment_info, _try_cached_session
            profile_dir = ROOT_DIR / "profiles" / sh.yahoo_profile_id
            session = _try_cached_session(profile_dir)
            if not session or not session.is_valid:
                return "宅配"  # 默认宅配
            info = fetch_order_shipment_info(session, sh.yahoo_order_no, self._log)
            if not isinstance(info, dict):
                return "宅配"
            method = str(info.get("shipping_method", ""))
            self._log(f"Yahoo 配送方式: {sh.yahoo_order_no} → {method}")
            if method in ("tCat", "homeDelivery"):
                return "宅配"
            elif method:
                # sevenCvs, familyCvs, hilifeCvs, okCvs 等都是超商
                return "超商"
            return "宅配"
        except Exception:
            return "宅配"

    def _fetch_yahoo_receiver(self, sh: SudaShipment) -> Tuple[str, str, str]:
        """从 Yahoo 订单获取宅配收件人信息。

        Returns:
            (name, phone, full_address) — full_address 包含邮编+市+区+街道
        """
        try:
            from core.ship_http_ops import fetch_order_shipment_info, _try_cached_session
            profile_dir = ROOT_DIR / "profiles" / sh.yahoo_profile_id
            session = _try_cached_session(profile_dir)
            if not session or not session.is_valid:
                return "", "", ""
            info = fetch_order_shipment_info(session, sh.yahoo_order_no, self._log)
            if isinstance(info, dict):
                recv = info.get("receiver", {})
                name = str(recv.get("name", "") or "").strip()
                phone = str(recv.get("phone", "") or recv.get("mobile", "") or "").strip()
                # 组合完整地址: 邮编 + 市 + 区 + 街道
                _zip = str(recv.get("zipcode", "") or recv.get("zipCode", "") or recv.get("postalCode", "") or "").strip()
                _city = str(recv.get("city", "") or "").strip()
                _district = str(recv.get("town", "") or recv.get("district", "") or recv.get("area", "") or "").strip()
                _street = str(recv.get("address", "") or recv.get("street", "") or "").strip()
                full_address = f"{_zip} {_city}{_district}{_street}".strip()
                if not full_address:
                    full_address = _street
                self._log(f"Yahoo 收件人: {name} {phone} {full_address}")
                return name, phone, full_address
            return "", "", ""
        except Exception as e:
            self._log(f"获取收件人异常: {e}")
            return "", "", ""

    # ══════════════════════════════════════════════════════════════
    # Phase C: 物流跟踪 + 触发
    # ══════════════════════════════════════════════════════════════

    def _check_delivery_batch(self, targets: List[SudaShipment]) -> None:
        session, err = _api.ensure_session(self._username, self._password, self._log)
        if err:
            self._log(f"连接失败: {err}")
            return

        # 按 suda_order_id 去重
        order_ids = list({s.suda_order_id for s in targets if s.suda_order_id})
        trigger_level = self.var_trigger_level.get()

        for oid in order_ids:
            result, err = _api.get_order_tracking(session, oid, log=self._log)
            if err:
                self._log(f"物流查询失败: {oid} → {err}")
                continue
            if not result:
                continue

            # 更新所有关联记录
            affected = [s for s in targets if s.suda_order_id == oid]
            for s in affected:
                s.suda_delivery_status = result.latest_status
                if result.tracking_code and not s.suda_tracking_code:
                    s.suda_tracking_code = result.tracking_code
                if s.status == "已提交":
                    s.status = "监控中"
                s.updated_at = _now_iso()

            # 检查是否达到触发级别
            if self._should_trigger(result.latest_status, trigger_level):
                representative = affected[0] if affected else None
                if representative and self.var_auto_ship.get():
                    self._log(f"触发出货: {representative.yahoo_order_no} (状态={result.latest_status})")
                    self._execute_yahoo_ship(representative)

        save_shipments(self.shipments)

    def _should_trigger(self, current_status: str, trigger_level: str) -> bool:
        """当前状态是否达到触发级别。"""
        cur = _TRIGGER_LEVELS.get(current_status, 0)
        tgt = _TRIGGER_LEVELS.get(trigger_level, 0)
        return cur >= tgt > 0

    # ══════════════════════════════════════════════════════════════
    # Yahoo 出货执行
    # ══════════════════════════════════════════════════════════════

    def _execute_yahoo_ship_batch(self, targets: List[SudaShipment]) -> None:
        """批量执行 Yahoo 出货 (按组)。"""
        seen_groups = set()
        for sh in targets:
            gk = sh.group_key or f"{sh.yahoo_profile_id}:{sh.yahoo_order_no}"
            if gk in seen_groups:
                continue
            seen_groups.add(gk)
            self._execute_yahoo_ship(sh)
            time.sleep(2)

    def _execute_yahoo_ship(self, sh: SudaShipment) -> None:
        """对一个订单组执行 Yahoo 出货。"""
        from core.ship_http_ops import ship_order_http, download_store_label_pdf

        siblings = self._get_siblings(sh)
        profile_dir = ROOT_DIR / "profiles" / sh.yahoo_profile_id
        ship_method = sh.ship_method or "宅配"

        try:
            if ship_method == "宅配":
                tracking_code = sh.suda_tracking_code or sh.suda_order_code
                if not tracking_code:
                    for s in siblings:
                        s.error = "无物流单号"
                    save_shipments(self.shipments)
                    self._log(f"出货失败: {sh.yahoo_order_no} — 无物流单号")
                    return
                result = ship_order_http(
                    profile_dir=profile_dir,
                    order_id=sh.yahoo_order_no,
                    tracking_code=tracking_code,
                    channel="黑貓",
                    chrome_path=self._chrome_path,
                    log=self._log,
                )
            else:
                # 超商: 不需要 tracking_code
                result = ship_order_http(
                    profile_dir=profile_dir,
                    order_id=sh.yahoo_order_no,
                    tracking_code="",
                    channel="",
                    chrome_path=self._chrome_path,
                    log=self._log,
                )

            if result.success:
                for s in siblings:
                    s.status = "已出货"
                    s.error = ""
                    s.watch = False
                    s.updated_at = _now_iso()
                self._log(f"Yahoo出货成功: {sh.yahoo_acc}/{sh.yahoo_order_no} ({ship_method})")
                self._notify_tg(
                    f"日台出货成功: {sh.yahoo_acc}/{sh.yahoo_order_no} ({ship_method})"
                )
                # 超商: 下载面单
                if ship_method == "超商" and result.print_delivery_url:
                    self._download_label(sh, result.print_delivery_url)
            else:
                for s in siblings:
                    s.error = result.error
                self._log(f"Yahoo出货失败: {sh.yahoo_order_no} → {result.error}")
        except Exception as e:
            for s in siblings:
                s.error = str(e)
            self._log(f"Yahoo出货异常: {sh.yahoo_order_no} → {e}")

        save_shipments(self.shipments)
        self._ui_queue.put(("refresh", None))

    def _download_label(self, sh: SudaShipment, print_url: str) -> None:
        """下载超商面单 PDF。"""
        try:
            from core.ship_http_ops import download_store_label_pdf
            profile_dir = ROOT_DIR / "profiles" / sh.yahoo_profile_id
            pdf_dir = ROOT_DIR / "output" / "面单" / datetime.now().strftime("%Y%m%d")
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"{sh.yahoo_order_no}.pdf"
            ok = download_store_label_pdf(
                profile_dir=profile_dir,
                print_url=print_url,
                save_path=str(pdf_path),
                chrome_path=self._chrome_path,
                log=self._log,
            )
            if ok:
                self._log(f"面单已下载: {pdf_path}")
            else:
                self._log(f"面单下载失败: {sh.yahoo_order_no}")
        except Exception as e:
            self._log(f"面单下载异常: {e}")

    # ══════════════════════════════════════════════════════════════
    # 外部调用接口
    # ══════════════════════════════════════════════════════════════

    def on_tracking_detected(
        self,
        yahoo_order_no: str,
        tracking_no: str,
        yahoo_profile_id: str = "",
        yahoo_acc_name: str = "",
        purchase_order_id: str = "",
        product_name: str = "",
        remark: str = "",
    ) -> None:
        """采购监控检测到 mercari tracking_no 后调用。

        自动创建/更新 SudaShipment。
        """
        # 检查是否已有对应记录
        existing = None
        for sh in self.shipments:
            if (sh.yahoo_order_no == yahoo_order_no
                    and sh.purchase_order_id == purchase_order_id):
                existing = sh
                break

        if existing:
            if not existing.mercari_tracking:
                existing.mercari_tracking = tracking_no
                if existing.status == "待发货":
                    existing.status = "已发货"
                existing.updated_at = _now_iso()
                save_shipments(self.shipments)
                self._ui_queue.put(("refresh", None))
                self._log(f"更新快递: {yahoo_order_no}/{purchase_order_id} → {tracking_no}")
            # 补充 goods_name/remark (可能之前未填)
            if product_name and not existing.goods_name:
                existing.goods_name = product_name
            if remark and not existing.remark:
                existing.remark = remark
                save_shipments(self.shipments)
            return

        # 自动创建新记录
        shipment = SudaShipment(
            yahoo_order_no=yahoo_order_no,
            yahoo_acc=yahoo_acc_name,
            yahoo_profile_id=yahoo_profile_id,
            purchase_order_id=purchase_order_id,
            mercari_tracking=tracking_no,
            group_key=f"{yahoo_profile_id}:{yahoo_order_no}",
            goods_name=product_name,
            remark=remark,
            status="已发货",
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        self.shipments.append(shipment)
        save_shipments(self.shipments)
        self._ui_queue.put(("refresh", None))
        self._log(f"自动创建: {yahoo_acc_name}/{yahoo_order_no} ← {purchase_order_id} 快递={tracking_no}")

    # ══════════════════════════════════════════════════════════════
    # 日志 & 通知
    # ══════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        full_msg = f"[{ts}] {msg}"
        _log_mod.info("%s", msg)
        try:
            self.frame.after(0, lambda: self._append_log(full_msg))
        except Exception:
            pass

    def _append_log(self, msg: str) -> None:
        try:
            if hasattr(self, "txt") and self.txt:
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
                lines = int(self.txt.index("end-1c").split(".")[0])
                if lines > 500:
                    self.txt.delete("1.0", f"{lines - 500}.0")
        except Exception:
            pass

    def _notify_tg(self, msg: str) -> None:
        try:
            if hasattr(self.app, "_ops_bot") and self.app._ops_bot:
                self.app._ops_bot.send_text(msg)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    # UI 轮询
    # ══════════════════════════════════════════════════════════════

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "refresh":
                    self.shipments = load_shipments()
                    self._render_tree()
                    self._update_stats()
                elif kind == "monitor_stopped":
                    self.btn_stop.pack_forget()
                    self.btn_monitor.pack(side="left", padx=(0, 6))
        except Exception:
            pass
        finally:
            self.frame.after(400, self._poll_ui_queue)
