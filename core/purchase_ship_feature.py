"""
采购出货一体化 Tab

整合「采购绑定/监控」+「订单获取/出货」为一个简化界面：
- 上半部分：绑定输入区（账号、订单号、平台、采购订单、商品信息）
- 下半部分：记录表格（状态流转：待监控→监控中→已获取单号→生成Excel中→上传中→已完成）
- 全自动流程：绑定 → 监控物流 → 生成出货Excel → 上传物流系统
"""
from __future__ import annotations

import json
import re
import threading
import time
import queue
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from core.purchase_feature import (
    PurchaseLink, load_links, save_links, scrape, open_login_browser,
    _to_mmdd, _clean_amount, _now_iso,
)
from core.order_export import export_order_to_excel
from core.accounts import load_settings

ROOT_DIR = Path(__file__).resolve().parent.parent

# ── 备注快捷模板 ──
_REMARK_TEMPLATES_FILE = ROOT_DIR / "remark_templates.json"
_DEFAULT_REMARK_TEMPLATES = [
    "检查出货（检查货物完整度）",
    "核对货物图片（货物需对应图片一致才打包）",
    "易碎物品使用气柱袋打包",
]


def _load_remark_templates() -> List[str]:
    """加载备注模板（用户文件优先，不存在则用默认）。"""
    if _REMARK_TEMPLATES_FILE.exists():
        try:
            with open(_REMARK_TEMPLATES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass
    return list(_DEFAULT_REMARK_TEMPLATES)


def _save_remark_templates(templates: List[str]) -> None:
    """保存备注模板到用户文件。"""
    with open(_REMARK_TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)


class PurchaseShipTab:
    """采购出货一体化页签"""

    def __init__(
        self,
        app: Any,
        frame: ttk.Frame,
    ):
        self.app = app
        self.frame = frame

        self.links: List[PurchaseLink] = load_links()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

        # --- 绑定录入区变量 ---
        self.var_account = tk.StringVar()
        self.var_yahoo_order = tk.StringVar()
        self.var_platform = tk.StringVar(value="xianyu")

        # 副订单（最多 5 个）
        self.sub_order_vars: List[tk.StringVar] = []
        self._sub_orders_holder: Optional[ttk.Frame] = None

        # 动态行：采购订单号/商品名称/规格/备注（可多条）
        self.purchase_rows: List[Dict[str, tk.StringVar]] = []

        # --- 监控/出货参数 ---
        settings = getattr(app, "settings", {})
        _saved_interval = int(settings.get("purchase_interval_sec", 21600) or 21600)
        if _saved_interval <= 600:
            _saved_interval = 21600
        self.var_interval = tk.IntVar(value=_saved_interval)
        self.var_headless = tk.BooleanVar(value=True)
        self.var_auto_ship = tk.BooleanVar(value=True)

        # 出货参数
        self.var_user_code = tk.StringVar(value=str(settings.get("ship_user_code", "") or ""))
        self.var_owner = tk.StringVar(value=str(settings.get("ship_owner", "") or ""))
        self.var_outdir = tk.StringVar(value=str(settings.get("ship_outdir", "") or ""))

        # --- 手动编辑变量 ---
        self.var_edit_purchase_id = tk.StringVar()
        self.var_edit_product = tk.StringVar()
        self.var_edit_spec = tk.StringVar()
        self.var_edit_remark = tk.StringVar()
        self.var_edit_pay_dt = tk.StringVar()
        self.var_edit_pay_amount = tk.StringVar()
        self.var_edit_tracking = tk.StringVar()

        # tree
        self.tree: Optional[ttk.Treeview] = None

        # 出货运行状态
        self._ship_running = False
        self._ship_stop = threading.Event()

    # =====================
    # helpers
    # =====================
    def log(self, s: str) -> None:
        try:
            self.app.log(s)
        except Exception:
            print(s)

    def _save_settings_patch(self) -> None:
        try:
            st = getattr(self.app, "settings", {})
            st["purchase_interval_sec"] = int(self.var_interval.get())
            st["purchase_headless"] = bool(self.var_headless.get())
            st["ship_user_code"] = self.var_user_code.get()
            st["ship_owner"] = self.var_owner.get()
            st["ship_outdir"] = self.var_outdir.get()
            if hasattr(self.app, "_save_settings"):
                self.app._save_settings()
            else:
                try:
                    from core.accounts import save_settings as _save
                    _save(st)
                except Exception:
                    pass
        except Exception:
            pass

    def _persist(self) -> None:
        save_links(self.links)

    def _mmdd_to_show(self, mmdd: str) -> str:
        mmdd = (mmdd or "").strip()
        if re.fullmatch(r"\d{4}", mmdd):
            return f"{int(mmdd[:2])}月{int(mmdd[2:])}日"
        return ""

    def _norm_mmdd_input(self, s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        m = re.fullmatch(r"(\d{1,2})[/-]?(\d{2})", s)
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{mm:02d}{dd:02d}"
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{mm:02d}{dd:02d}"
        mmdd = _to_mmdd(s)
        if mmdd:
            return mmdd
        return ""

    # =====================
    # account helpers
    # =====================
    def _refresh_accounts(self) -> None:
        accs = getattr(self.app, "accounts", []) or []
        vals = []
        for a in accs:
            name = str(a.get("name", "") or "").strip()
            pid = str(a.get("profile_id", "") or "").strip()
            if not pid:
                continue
            vals.append(f"{name} ({pid})" if name else pid)
        vals = sorted(set(vals))
        if hasattr(self, "cmb_account"):
            self.cmb_account["values"] = vals
        if vals and not (self.var_account.get() or "").strip():
            self.var_account.set(vals[0])

    def _parse_account_display(self, disp: str):
        s = (disp or "").strip()
        m = re.search(r"\(([^()]+)\)\s*$", s)
        if m:
            pid = m.group(1).strip()
            name = s[: m.start()].strip()
            return pid, name
        return s, s

    # =====================
    # sub-order helpers
    # =====================
    def _get_sub_orders(self) -> List[str]:
        out, seen = [], set()
        for v in (self.sub_order_vars or []):
            s = (v.get() or "").strip()
            if "+" in s:
                s = s.split("+", 1)[0].strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out[:5]

    def _add_sub_order_row(self) -> None:
        if len(self.sub_order_vars) >= 5:
            messagebox.showinfo("提示", "副订单编号最多 5 个")
            return
        self.sub_order_vars.append(tk.StringVar())
        self._rebuild_sub_order_rows()

    def _clear_sub_orders(self) -> None:
        for v in (self.sub_order_vars or []):
            v.set("")
        if len(self.sub_order_vars) > 1:
            self.sub_order_vars = self.sub_order_vars[:1]
        self._rebuild_sub_order_rows()

    def _rebuild_sub_order_rows(self) -> None:
        holder = self._sub_orders_holder
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        for i, v in enumerate(self.sub_order_vars or []):
            ttk.Entry(holder, textvariable=v, width=16).grid(row=0, column=i, sticky="w", padx=(0, 6))

    # =====================
    # purchase rows (dynamic input rows)
    # =====================
    def _add_purchase_row(self, init=None) -> None:
        init = init or {}
        row = {
            "purchase_order_id": tk.StringVar(value=init.get("purchase_order_id", "")),
            "product_name": tk.StringVar(value=init.get("product_name", "")),
            "spec": tk.StringVar(value=init.get("spec", "")),
            "remark": tk.StringVar(value=init.get("remark", "")),
            "image_path": tk.StringVar(value=init.get("image_path", "")),  # 图片绝对路径（internal cache）
        }
        self.purchase_rows.append(row)
        self._rebuild_purchase_rows()

    def _remove_purchase_row(self, idx: int) -> None:
        if len(self.purchase_rows) <= 1:
            for k in ("purchase_order_id", "product_name", "spec", "remark", "image_path"):
                if k in self.purchase_rows[0]:
                    self.purchase_rows[0][k].set("")
            self._rebuild_purchase_rows()
            return
        if 0 <= idx < len(self.purchase_rows):
            self.purchase_rows.pop(idx)
        self._rebuild_purchase_rows()

    def _rebuild_purchase_rows(self) -> None:
        if not hasattr(self, "purchase_rows_holder"):
            return
        holder = self.purchase_rows_holder
        for w in holder.winfo_children():
            w.destroy()
        ttk.Label(holder, text="采购订单号").grid(row=0, column=0, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="商品名称").grid(row=0, column=1, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="规格").grid(row=0, column=2, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="备注").grid(row=0, column=3, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="图片").grid(row=0, column=4, sticky="w", padx=3, pady=(2, 1))
        holder.columnconfigure(0, weight=0)
        holder.columnconfigure(1, weight=1)
        holder.columnconfigure(2, weight=1)
        holder.columnconfigure(3, weight=1)
        holder.columnconfigure(4, weight=0)
        holder.columnconfigure(5, weight=0)
        holder.columnconfigure(6, weight=0)
        for i, r in enumerate(self.purchase_rows, start=1):
            ttk.Entry(holder, textvariable=r["purchase_order_id"], width=28).grid(row=i, column=0, sticky="w", padx=3, pady=2)
            ttk.Entry(holder, textvariable=r["product_name"]).grid(row=i, column=1, sticky="ew", padx=3, pady=2)
            ttk.Entry(holder, textvariable=r["spec"]).grid(row=i, column=2, sticky="ew", padx=3, pady=2)
            # 备注输入框 + 快选按钮
            remark_frame = ttk.Frame(holder)
            remark_frame.grid(row=i, column=3, sticky="ew", padx=3, pady=2)
            remark_frame.columnconfigure(0, weight=1)
            ttk.Entry(remark_frame, textvariable=r["remark"]).grid(row=0, column=0, sticky="ew")
            ttk.Button(remark_frame, text="▼", width=2,
                       command=lambda var=r["remark"]: self._show_remark_menu(var)).grid(row=0, column=1, sticky="e", padx=(2, 0))
            ttk.Button(remark_frame, text="模板", width=4,
                       command=self._manage_remark_templates).grid(row=0, column=2, sticky="e", padx=(2, 0))
            # 图片按钮：未选 → "+图片"，已选 → "✓ 文件名 ✕"
            img_var = r.setdefault("image_path", tk.StringVar(value=""))
            self._build_image_button(holder, i, 4, img_var, r)
            # ＋和－在同一列，＋在第一行数据旁，－在后续行
            if i == 1:
                btn_col = ttk.Frame(holder)
                btn_col.grid(row=i, column=5, sticky="w", padx=1, pady=2)
                ttk.Button(btn_col, text="＋", width=3, command=lambda: self._add_purchase_row()).pack(side="top", pady=(0, 1))
                ttk.Button(btn_col, text="－", width=3, command=lambda ii=i-1: self._remove_purchase_row(ii)).pack(side="top")
            else:
                ttk.Button(holder, text="－", width=3, command=lambda ii=i-1: self._remove_purchase_row(ii)).grid(row=i, column=5, sticky="w", padx=1, pady=2)

    def _build_image_button(self, holder, row_i, col_i, img_var, row_dict) -> None:
        """渲染图片选择按钮：未选 → '+图片'，已选 → '✓ 文件名 ✕'"""
        cur_path = (img_var.get() or "").strip()
        frame = ttk.Frame(holder)
        frame.grid(row=row_i, column=col_i, sticky="w", padx=3, pady=2)
        if not cur_path:
            ttk.Button(frame, text="+图片", width=8,
                       command=lambda: self._on_pick_image(row_dict)).pack(side="left")
        else:
            from pathlib import Path as _P
            name = _P(cur_path).name
            label_text = f"✓ {name[:14]}{'…' if len(name) > 14 else ''}"
            ttk.Button(frame, text=label_text, width=18,
                       command=lambda: self._on_pick_image(row_dict)).pack(side="left")
            ttk.Button(frame, text="✕", width=2,
                       command=lambda: self._on_clear_image(row_dict)).pack(side="left", padx=(2, 0))

    def _on_pick_image(self, row_dict) -> None:
        """打开文件选择器，选完图片复制到 internal cache"""
        from .purchase_image_feature import ingest_image, ALLOWED_EXTS
        # 文件类型过滤
        types = [("图片", " ".join("*" + e for e in sorted(ALLOWED_EXTS))), ("所有文件", "*.*")]
        path = filedialog.askopenfilename(title="选择商品图片", filetypes=types)
        if not path:
            return
        # 复制到 internal cache（用账号+订单号+采购单号做文件名）
        y_order = (self.var_yahoo_order.get() or "").strip() or "_unbound"
        p_order = (row_dict["purchase_order_id"].get() or "").strip() or "main"
        from pathlib import Path as _P
        dst, err = ingest_image(_P(path), y_order, p_order, log=self.log)
        if err:
            messagebox.showerror("错误", err)
            return
        row_dict["image_path"].set(str(dst))
        self._rebuild_purchase_rows()

    def _on_clear_image(self, row_dict) -> None:
        """删 internal cache 里的图 + 清 var"""
        from .purchase_image_feature import remove_image
        from pathlib import Path as _P
        cur = (row_dict["image_path"].get() or "").strip()
        if cur:
            remove_image(_P(cur))
        row_dict["image_path"].set("")
        self._rebuild_purchase_rows()

    # =====================
    # 备注快捷模板
    # =====================
    def _show_remark_menu(self, var: tk.StringVar) -> None:
        """显示备注快选下拉菜单。"""
        menu = tk.Menu(self.frame, tearoff=0)
        templates = _load_remark_templates()
        for t in templates:
            menu.add_command(label=t, command=lambda txt=t: self._append_remark(var, txt))
        menu.add_separator()
        menu.add_command(label="清空备注", command=lambda: var.set(""))
        try:
            menu.tk_popup(self.frame.winfo_pointerx(), self.frame.winfo_pointery())
        finally:
            menu.grab_release()

    def _append_remark(self, var: tk.StringVar, text: str) -> None:
        """追加备注文字（已有内容用分号分隔）。"""
        current = (var.get() or "").strip()
        if current:
            if text not in current:
                var.set(f"{current}; {text}")
        else:
            var.set(text)

    def _manage_remark_templates(self) -> None:
        """打开备注模板管理窗口。"""
        dlg = tk.Toplevel(self.frame)
        dlg.title("备注模板管理")
        dlg.geometry("450x350")
        dlg.transient(self.frame.winfo_toplevel())

        ttk.Label(dlg, text="每行一个模板，可自由添加/删除/修改：").pack(anchor="w", padx=10, pady=(10, 5))

        text_widget = tk.Text(dlg, width=50, height=12, font=("Microsoft YaHei", 10))
        text_widget.pack(fill="both", expand=True, padx=10, pady=5)

        templates = _load_remark_templates()
        text_widget.insert("1.0", "\n".join(templates))

        def _save():
            content = text_widget.get("1.0", "end").strip()
            new_templates = [line.strip() for line in content.split("\n") if line.strip()]
            if not new_templates:
                messagebox.showwarning("提示", "至少保留一个模板", parent=dlg)
                return
            _save_remark_templates(new_templates)
            messagebox.showinfo("成功", f"已保存 {len(new_templates)} 个模板", parent=dlg)
            dlg.destroy()

        def _reset():
            text_widget.delete("1.0", "end")
            text_widget.insert("1.0", "\n".join(_DEFAULT_REMARK_TEMPLATES))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=10, pady=(5, 10))
        ttk.Button(btn_frame, text="恢复默认", command=_reset).pack(side="left")
        ttk.Button(btn_frame, text="取消", command=dlg.destroy).pack(side="right", padx=(5, 0))
        ttk.Button(btn_frame, text="保存", command=_save).pack(side="right")

    # =====================
    # tree render / interaction
    # =====================
    def _render_tree(self) -> None:
        if not self.tree:
            return
        self.tree.delete(*self.tree.get_children())
        for idx, x in enumerate(self.links):
            y_disp = x.yahoo_order_no
            try:
                subs = getattr(x, "sub_order_nos", None) or []
                subs = [str(s).strip() for s in subs if str(s).strip()]
                if subs:
                    y_disp = f"{x.yahoo_order_no}+" + "+".join(subs)
            except Exception:
                pass
            watch_disp = "☑" if bool(getattr(x, "watch", True)) else "☐"
            # 图片列：已上传 ✓ / 已选未传 📷 / 无图 —
            img_path = (getattr(x, "image_path", "") or "").strip()
            img_uploaded = bool(getattr(x, "image_uploaded", False))
            if img_path and img_uploaded:
                image_disp = "✓"
            elif img_path:
                image_disp = "📷"
            else:
                image_disp = "—"
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(
                    watch_disp,
                    x.yahoo_acc_name,
                    y_disp,
                    "闲鱼" if x.platform == "xianyu" else ("煤炉" if x.platform == "mercari" else x.platform),
                    x.purchase_order_id,
                    x.product_name,
                    image_disp,
                    x.tracking_no,
                    self._mmdd_to_show(x.pay_mmdd) or x.pay_mmdd,
                    x.pay_amount,
                    x.status,
                    (x.error or "")[:60],
                ),
            )

    def _on_tree_click(self, event):
        if not self.tree:
            return
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":  # 第一列 = watch
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        try:
            idx = int(row_id)
        except Exception:
            return
        if not (0 <= idx < len(self.links)):
            return
        x = self.links[idx]
        x.watch = not bool(getattr(x, "watch", True))
        self._persist()
        self._render_tree()

    # =====================
    # actions: add / delete / edit
    # =====================
    def action_add_bindings(self) -> None:
        disp = (self.var_account.get() or "").strip()
        pid, name = self._parse_account_display(disp)
        y_order = (self.var_yahoo_order.get() or "").strip()
        sub_orders = self._get_sub_orders()
        platform = (self.var_platform.get() or "").strip().lower()
        if not pid or not y_order:
            messagebox.showerror("错误", "请填写：Yahoo账号、Yahoo订单号")
            return
        if platform not in ("xianyu", "mercari"):
            messagebox.showerror("错误", "平台只支持：闲鱼(xianyu) / 煤炉(mercari)")
            return

        self.links = load_links()
        created, existed = 0, 0

        for r in self.purchase_rows:
            p_order = (r["purchase_order_id"].get() or "").strip()
            if not p_order:
                continue
            product_name = (r["product_name"].get() or "").strip()
            spec = (r["spec"].get() or "").strip()
            remark = (r["remark"].get() if isinstance(r.get("remark"), tk.StringVar) else "") or ""
            remark = remark.strip()
            image_path = (r["image_path"].get() if isinstance(r.get("image_path"), tk.StringVar) else "") or ""
            image_path = image_path.strip()

            dup = any(
                ex.yahoo_profile_id == pid and ex.yahoo_order_no == y_order
                and ex.platform == platform and ex.purchase_order_id == p_order
                for ex in self.links
            )
            if dup:
                existed += 1
                continue

            link = PurchaseLink(
                yahoo_profile_id=pid,
                yahoo_acc_name=name,
                yahoo_order_no=y_order,
                sub_order_nos=sub_orders,
                platform=platform,
                purchase_order_id=p_order,
                product_name=product_name,
                spec=spec,
                remark=remark,
                watch=True,
                status="待监控",
                image_path=image_path,
            )
            self.links.append(link)
            created += 1

        if created == 0 and existed == 0:
            messagebox.showerror("错误", "请至少填写 1 条采购订单号")
            return

        self._persist()
        self._render_tree()
        self.log(f"[采购出货] 已新增绑定：{created} 条" + (f"（重复跳过：{existed}）" if existed else ""))

    def action_delete_selected(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            return
        self.links = load_links()
        for i in sorted([int(x) for x in sel], reverse=True):
            if 0 <= i < len(self.links):
                del self.links[i]
        self._persist()
        self._render_tree()

    def _open_edit_dialog(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一行")
            return
        try:
            idx = int(sel[0])
        except Exception:
            return
        if not (0 <= idx < len(self.links)):
            return
        x = self.links[idx]
        self.var_edit_purchase_id.set(x.purchase_order_id)
        self.var_edit_product.set(x.product_name)
        self.var_edit_spec.set(x.spec)
        self.var_edit_remark.set(getattr(x, "remark", ""))
        self.var_edit_pay_dt.set(x.pay_dt_raw or x.pay_mmdd)
        self.var_edit_pay_amount.set(x.pay_amount)
        self.var_edit_tracking.set(x.tracking_no)

        w = tk.Toplevel(self.app)
        w.title("编辑选中记录")
        w.resizable(False, False)
        g = ttk.Frame(w)
        g.pack(fill="x", padx=10, pady=10)
        fields = [
            ("采购订单号", self.var_edit_purchase_id),
            ("商品名称", self.var_edit_product),
            ("规格", self.var_edit_spec),
            ("代付日期", self.var_edit_pay_dt),
            ("代付金额", self.var_edit_pay_amount),
            ("物流单号", self.var_edit_tracking),
            ("备注", self.var_edit_remark),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(g, textvariable=var, width=40).grid(row=i, column=1, sticky="ew", padx=6, pady=3)
        g.columnconfigure(1, weight=1)

        def _save_and_close():
            self._apply_edit_to_selected(sel)
            w.destroy()
        bf = ttk.Frame(w)
        bf.pack(pady=(0, 10))
        ttk.Button(bf, style="Accent.TButton", text="保存", command=_save_and_close).pack(side="left", padx=6)
        ttk.Button(bf, text="取消", command=w.destroy).pack(side="left", padx=6)

    def _apply_edit_to_selected(self, sel) -> None:
        purchase_id = (self.var_edit_purchase_id.get() or "").strip()
        product = (self.var_edit_product.get() or "").strip()
        spec = (self.var_edit_spec.get() or "").strip()
        remark = (self.var_edit_remark.get() or "").strip()
        pay_dt = (self.var_edit_pay_dt.get() or "").strip()
        pay_amount = (self.var_edit_pay_amount.get() or "").strip()
        tracking = (self.var_edit_tracking.get() or "").strip()
        mmdd = self._norm_mmdd_input(pay_dt)

        self.links = load_links()
        changed = False
        for iid in sel:
            try:
                idx = int(iid)
            except Exception:
                continue
            if not (0 <= idx < len(self.links)):
                continue
            x = self.links[idx]
            if purchase_id and purchase_id != x.purchase_order_id:
                x.purchase_order_id = purchase_id; changed = True
            if product != x.product_name:
                x.product_name = product; changed = True
            if spec != x.spec:
                x.spec = spec; changed = True
            if remark != getattr(x, "remark", ""):
                x.remark = remark; changed = True
            if pay_amount != x.pay_amount:
                x.pay_amount = pay_amount; changed = True
            if tracking != x.tracking_no:
                x.tracking_no = tracking; changed = True
                if tracking:
                    self._notify_suda68_tracking(x, tracking)
            if pay_dt:
                x.pay_dt_raw = pay_dt
                if mmdd:
                    x.pay_mmdd = mmdd
                changed = True
            x.status = "已获取单号" if x.tracking_no else "待监控"

        if changed:
            self._persist()
            self._render_tree()
            self.log("[采购出货] 已更新选中记录")

    # =====================
    # scrape once (manual)
    # =====================
    def action_scrape_selected_once(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择记录")
            return
        # 自动出货开启时，必须先确认物流系统已登录
        if self.var_auto_ship.get() and not self._check_ship_login():
            messagebox.showwarning("提示", "物流系统未登录，请先到「物流系统」页签登录后再抓取。")
            return
        indices = []
        for iid in sel:
            try:
                indices.append(int(iid))
            except Exception:
                pass
        t = threading.Thread(
            target=self._scrape_indices,
            kwargs={"indices": indices, "notify": False, "headless": bool(self.var_headless.get())},
            daemon=True,
        )
        t.start()

    def action_ship_selected_now(self) -> None:
        """v6.0.73 新增「立即出货」按钮 — 对选中记录直接走自动出货流程。

        场景:
          - 用户手动「编辑选中」填入物流单号(系统抓不到时)
          - 抓取后由于物流系统未登录/参数没填等原因没自动出货
          - 不想等监控间隔(21600秒=6小时),想立刻出货

        与正常自动出货走同一条路径 (_auto_ship_batch),后续 Excel 生成、物流系统上传
        等流程完全一致。only_selected=True 确保只处理选中,不连带补出 stuck 订单。
        """
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择记录")
            return

        # 收集选中的 links,检查 tracking_no 是否齐全
        targets: List[PurchaseLink] = []
        no_tracking: List[str] = []
        already_shipped: List[str] = []
        for iid in sel:
            try:
                idx = int(iid)
            except Exception:
                continue
            if not (0 <= idx < len(self.links)):
                continue
            link = self.links[idx]
            tag = f"{link.yahoo_acc_name or '?'}/{(link.purchase_order_id or '')[-8:]}"
            if not (link.tracking_no or "").strip():
                no_tracking.append(tag)
                continue
            if getattr(link, "created_ship_task", False):
                already_shipped.append(tag)
                continue
            targets.append(link)

        # 缺物流单号的不能出货
        if no_tracking:
            messagebox.showwarning(
                "提示",
                f"以下 {len(no_tracking)} 条记录没有物流单号,无法出货:\n\n"
                + "\n".join(f"  {t}" for t in no_tracking[:8])
                + (f"\n... 还有 {len(no_tracking) - 8} 条" if len(no_tracking) > 8 else "")
                + "\n\n请先「编辑选中」填入物流单号。"
            )
            return

        if not targets:
            if already_shipped:
                messagebox.showinfo(
                    "提示",
                    f"选中的 {len(already_shipped)} 条记录都已经出过货 (created_ship_task=True),无需重复出货。"
                )
            return

        # 物流系统登录检查
        if not self._check_ship_login():
            messagebox.showwarning("提示", "物流系统未登录,请先到「物流系统」页签登录后再立即出货。")
            return

        # 必填参数检查
        user_code = (self.var_user_code.get() or "").strip()
        owner = (self.var_owner.get() or "").strip()
        if not user_code or not owner:
            messagebox.showwarning("提示", "请填写「使用者简称」和「所属人」后再立即出货。")
            return

        # 确认对话框
        _list = "\n".join(
            f"  {t.yahoo_acc_name or '?'}/{(t.purchase_order_id or '')[-8:]} -> {t.tracking_no}"
            for t in targets[:8]
        )
        if len(targets) > 8:
            _list += f"\n  ... 还有 {len(targets) - 8} 条"
        _skipped_msg = ""
        if already_shipped:
            _skipped_msg = f"\n\n(已跳过 {len(already_shipped)} 条已出货记录)"
        _confirm = messagebox.askyesno(
            "确认立即出货",
            f"即将对选中的 {len(targets)} 条记录立即出货:\n\n{_list}\n\n"
            f"出货后会自动生成 Excel + 上传物流系统(与「自动出货」流程一致)。"
            f"{_skipped_msg}\n\n是否继续?"
        )
        if not _confirm:
            return

        self.log(f"[采购出货] 立即出货:对 {len(targets)} 条选中记录触发出货流程")
        for _t in targets:
            self.log(f"[采购出货]   {_t.yahoo_acc_name}/{_t.yahoo_order_no} purchase={(_t.purchase_order_id or '')[-8:]} tracking={_t.tracking_no}")

        # 后台线程执行(避免阻塞 UI;_auto_ship_batch 内部会同步跑 Playwright + Excel 生成)
        t = threading.Thread(
            target=self._auto_ship_batch,
            args=(targets,),
            kwargs={"only_selected": True},
            daemon=True,
        )
        t.start()

    def _scrape_indices(self, indices, *, notify: bool, headless=None) -> None:
        changed = False
        newly_tracked: list = []
        for idx in indices:
            link = self.links[idx]
            prev_tracking = (link.tracking_no or "").strip()
            changed = self._scrape_one(idx, headless=headless) or changed
            if link.tracking_no and not prev_tracking:
                newly_tracked.append(link)
        if changed:
            self._merge_and_persist()
        self._ui_queue.put(("refresh", None))

        # 串联自动出货：抓取到新单号时自动触发出货流程
        if self.var_auto_ship.get() and newly_tracked:
            self._auto_ship_batch(newly_tracked)

    # =====================
    # core scrape logic
    # =====================
    def _scrape_one(self, idx: int, headless=None) -> bool:
        if idx < 0 or idx >= len(self.links):
            return False
        link = self.links[idx]
        link.error = ""
        link.last_checked_at = _now_iso()
        try:
            use_headless = bool(self.var_headless.get()) if headless is None else bool(headless)
            self.log(f"[采购出货] scrape_one: {link.platform} / {link.purchase_order_id} headless={use_headless}")
            fields, need_login, err_detail = scrape(link.platform, link.purchase_order_id, headless=use_headless)
            self.log(f"[采购出货] scrape_one 完成: need_login={need_login} fields={fields}"
                     + (f" error={err_detail}" if err_detail else ""))
            if need_login:
                link.status = "需登录"
                return True
            if link.status == "需登录":
                link.notified_login = False
            if fields.get("pay_amount"):
                link.pay_amount = str(fields["pay_amount"]).strip()
            if fields.get("pay_dt_raw"):
                link.pay_dt_raw = str(fields["pay_dt_raw"]).strip()
                mmdd = _to_mmdd(link.pay_dt_raw)
                if mmdd:
                    link.pay_mmdd = mmdd
            new_tracking = str(fields.get("tracking_no") or "").strip()
            if new_tracking:
                link.tracking_no = new_tracking
                # 通知日台線路模块
                self._notify_suda68_tracking(link, new_tracking)
            link.status = "已获取单号" if link.tracking_no else "监控中"
            if link.tracking_no and link.watch:
                link.watch = False
            return True
        except Exception as e:
            link.error = str(e)
            link.status = "失败"
            return True

    def _notify_suda68_tracking(self, link, tracking_no: str) -> None:
        """通知日台線路模块检测到快递单号（仅 mercari 平台）。"""
        try:
            if getattr(link, "platform", "") != "mercari":
                return
            suda_tab = getattr(self.app, "suda68_tab", None)
            if suda_tab:
                suda_tab.on_tracking_detected(
                    yahoo_order_no=link.yahoo_order_no,
                    tracking_no=tracking_no,
                    yahoo_profile_id=getattr(link, "yahoo_profile_id", ""),
                    yahoo_acc_name=getattr(link, "yahoo_acc_name", ""),
                    purchase_order_id=getattr(link, "purchase_order_id", ""),
                    product_name=getattr(link, "product_name", ""),
                    remark=getattr(link, "remark", ""),
                )
        except Exception:
            pass

    def _merge_and_persist(self) -> None:
        fresh = load_links()
        fresh_map = {}
        for fx in fresh:
            key = (fx.yahoo_order_no, fx.purchase_order_id)
            fresh_map[key] = fx
        for mem in self.links:
            key = (mem.yahoo_order_no, mem.purchase_order_id)
            fx = fresh_map.get(key)
            if not fx:
                continue
            fx.status = mem.status
            fx.tracking_no = mem.tracking_no
            fx.pay_amount = mem.pay_amount
            fx.pay_dt_raw = mem.pay_dt_raw
            fx.pay_mmdd = mem.pay_mmdd
            fx.error = mem.error
            fx.last_checked_at = mem.last_checked_at
            fx.notified_login = mem.notified_login
            fx.notified_tracking = mem.notified_tracking
            fx.created_ship_task = mem.created_ship_task
            if not mem.watch and fx.watch:
                fx.watch = False
        self.links = fresh
        save_links(fresh)

    # =====================
    # monitoring loop
    # =====================
    def _check_ship_login(self) -> bool:
        """检查物流系统是否已登录，返回 True 表示已登录。

        v6.1.41:修「stoken 自动恢复登录但 GUI var_status 未同步,导致采购出货监控
                  误判未登录」bug。
        修法:source of truth 改用 stoken cache(load_stoken),GUI label 字符只作 fallback
        (避免 race condition / 繁簡差異 / 顯示異步問題)。
        """
        # 1. 主檢查:stoken cache 有效?(實際軟件能不能用 SYB API 的 source of truth)
        try:
            from .syb_http_ops import load_stoken
            if load_stoken():
                return True
        except Exception:
            pass
        # 2. Fallback:GUI label 字符檢查(向下兼容舊邏輯)
        try:
            syb_tab = getattr(self.app, "syb_upload_tab", None)
            if syb_tab:
                status_text = (syb_tab.var_status.get() or "")
                if "已登录" in status_text or "登录成功" in status_text:
                    return True
        except Exception:
            pass
        return False

    def action_start(self) -> None:
        if self._thread and self._thread.is_alive():
            messagebox.showinfo("提示", "监控已在运行")
            return

        # 自动出货开启时，必须先检查物流系统登录状态
        if self.var_auto_ship.get() and not self._check_ship_login():
            messagebox.showwarning("提示", "物流系统未登录，请先到「物流系统」页签登录后再开始监控。")
            return

        self._stop_event.clear()
        self._save_settings_patch()
        interval = max(30, int(self.var_interval.get() or 21600))
        headless = bool(self.var_headless.get())

        self._thread = threading.Thread(
            target=self._loop,
            kwargs={"interval": interval, "headless": headless},
            daemon=True,
        )
        self._thread.start()
        self._update_monitor_btn(True)
        self.log(f"[采购出货] 监控已启动（间隔={interval}s）")

    def action_stop(self) -> None:
        self._stop_event.set()
        self._update_monitor_btn(False)
        self.log("[采购出货] 已发送停止指令")

    def _toggle_monitor(self) -> None:
        """切换监控状态：运行中则停止，未运行则启动。"""
        if self._thread and self._thread.is_alive():
            self.action_stop()
        else:
            self.action_start()

    def _update_monitor_btn(self, running: bool) -> None:
        """更新监控按钮文字和停止按钮显隐。"""
        if not hasattr(self, "btn_monitor"):
            return
        if running:
            self.btn_monitor.configure(text="监控中...", style="")
            if hasattr(self, "btn_stop"):
                self.btn_stop.pack(side="left", padx=(0, 6))
        else:
            self.btn_monitor.configure(text="开始监控", style="Accent.TButton")
            if hasattr(self, "btn_stop"):
                self.btn_stop.pack_forget()

    def _loop(self, interval: int, headless: bool) -> None:
        first_round = True

        # 获取系统 Chrome 路径
        _chrome_path = ""
        try:
            _chrome_path = (self.app.var_browser.get() or "").strip()
        except Exception:
            _chrome_path = str(getattr(self.app, "settings", {}).get("browser_path", "") or "")

        # 首轮前: 根据待监控订单的平台，按需提取 cookie/token
        _pre_links = load_links()
        _need_platforms = {
            getattr(x, "platform", "xianyu")
            for x in _pre_links if bool(getattr(x, "watch", True))
        }

        if "xianyu" in _need_platforms:
            try:
                from .goofish_cookie_store import extract_cookies_from_profile, refresh_goofish_session
                from .purchase_feature import PURCHASE_PROFILE_DIR
                # 1) SQLite 直读提取 cookie（瞬间，不开浏览器）
                self.log("[采购出货] 预提取闲鱼 cookie（SQLite 直读）...")
                if extract_cookies_from_profile(PURCHASE_PROFILE_DIR, chrome_path=_chrome_path):
                    self.log("[采购出货] 闲鱼 cookie SQLite 提取成功")
                else:
                    self.log("[采购出货] 闲鱼 cookie SQLite 提取失败")
                # 2) 后台无头刷新 session（防止 cookie 过期）
                self.log("[采购出货] 后台无头刷新闲鱼 session...")
                if refresh_goofish_session(PURCHASE_PROFILE_DIR, chrome_path=_chrome_path):
                    self.log("[采购出货] 闲鱼 session 刷新成功，cookie 已更新")
                else:
                    self.log("[采购出货] ⚠ 闲鱼 session 已过期，请使用「打开登录浏览器」重新登录闲鱼")
            except Exception as e:
                self.log(f"[采购出货] 闲鱼 cookie 预提取跳过: {e}")

        if "mercari" in _need_platforms:
            try:
                from .mercari_token_store import extract_token_from_profile
                from .purchase_feature import PURCHASE_PROFILE_DIR as _pdir
                self.log("[采购出货] 提取煤炉 token...")
                if extract_token_from_profile(_pdir, chrome_path=_chrome_path):
                    self.log("[采购出货] 煤炉 token 提取成功")
                else:
                    self.log("[采购出货] 煤炉 token 提取失败")
            except Exception as e:
                self.log(f"[采购出货] 煤炉 token 提取跳过: {e}")

        while not self._stop_event.is_set():
            try:
                self.links = load_links()
                watch_count = sum(1 for x in self.links if bool(getattr(x, "watch", True)))
                if first_round:
                    self.log(f"[采购出货] 开始第一轮监控（共 {watch_count} 条待监控记录）")
                    first_round = False
                else:
                    # 非首轮: 刷新闲鱼 session (首轮已在 pre-loop 刷新过)
                    _round_platforms = {
                        getattr(x, "platform", "xianyu")
                        for x in self.links if bool(getattr(x, "watch", True))
                    }
                    if "xianyu" in _round_platforms:
                        try:
                            from .goofish_cookie_store import refresh_goofish_session
                            from .purchase_feature import PURCHASE_PROFILE_DIR
                            self.log("[采购出货] 后台无头刷新闲鱼 session...")
                            if refresh_goofish_session(PURCHASE_PROFILE_DIR, chrome_path=_chrome_path):
                                self.log("[采购出货] 闲鱼 session 刷新成功")
                            else:
                                self.log("[采购出货] ⚠ 闲鱼 session 已过期，请使用「打开登录浏览器」重新登录闲鱼")
                        except Exception as e:
                            self.log(f"[采购出货] 闲鱼 session 刷新跳过: {e}")
                changed = False
                newly_tracked: List[PurchaseLink] = []
                for i, x in enumerate(self.links):
                    if self._stop_event.is_set():
                        break
                    if not bool(getattr(x, "watch", True)):
                        continue
                    prev_tracking = (x.tracking_no or "").strip()
                    x.status = "监控中"
                    did = self._scrape_one(i, headless=headless)
                    changed = did or changed
                    if x.tracking_no and not prev_tracking:
                        newly_tracked.append(x)

                if changed:
                    self._merge_and_persist()
                self._ui_queue.put(("refresh", None))

                # 自动出货：对新获取到物流单号的记录触发出货流程
                if newly_tracked:
                    self.log(f"[采购出货] 本轮新获取单号: {len(newly_tracked)} 条 auto_ship={self.var_auto_ship.get()}")
                    for _nt in newly_tracked:
                        self.log(f"[采购出货]   {_nt.yahoo_order_no}/{_nt.purchase_order_id[-8:]} -> {_nt.tracking_no}")
                if self.var_auto_ship.get() and newly_tracked:
                    self._auto_ship_batch(newly_tracked)

            except Exception as e:
                self.log(f"[采购出货] 监控循环异常：{e}")

            for _ in range(int(interval * 10)):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)
        # 监控线程退出，通知 UI 恢复按钮
        self._ui_queue.put(("monitor_stopped", None))

    # =====================
    # auto-ship flow
    # =====================
    def _auto_ship_batch(self, tracked_links: List[PurchaseLink], *, only_selected: bool = False) -> None:
        """对新获取到物流单号的记录，按 Yahoo 账号分组执行出货。

        v6.0.73 新增 only_selected 参数:
        - False (默认): 监控循环触发,会扫描所有 stuck 订单一起补出货
        - True: 「立即出货」按钮触发,只处理传入的 tracked_links,跳过 stuck 扫描
        """
        # 按 (profile_id, order_no) 分组（含 newly_tracked 涉及的订单）
        groups: Dict[Tuple[str, str], List[PurchaseLink]] = {}
        for x in tracked_links:
            key = (x.yahoo_profile_id, x.yahoo_order_no)
            groups.setdefault(key, []).append(x)

        # 补充：检查所有「已获取单号」但未出货的订单（修复多采购单号分轮到齐后不触发的问题）
        # v6.0.73:「立即出货」(only_selected=True) 时跳过此扫描 — 用户已经明确点了哪几条
        if not only_selected:
            for x in self.links:
                if x.status == "已获取单号" and (x.tracking_no or "").strip() and not getattr(x, "created_ship_task", False):
                    key = (x.yahoo_profile_id, x.yahoo_order_no)
                    if key not in groups:
                        groups[key] = [x]

        outdir = (self.var_outdir.get() or "").strip()
        if not outdir:
            outdir = str((ROOT_DIR / "output").resolve())
        Path(outdir).mkdir(parents=True, exist_ok=True)

        user_code = (self.var_user_code.get() or "").strip()
        owner = (self.var_owner.get() or "").strip()
        if not user_code or not owner:
            self.log("[采购出货] 自动出货跳过：请填写「使用者简称」和「所属人」")
            return

        for (pid, order_no), items in groups.items():
            acc_name = items[0].yahoo_acc_name

            # 多采购单号场景：同一个 Yahoo 订单绑了多个采购单号时，
            # 必须等所有采购单号都有物流单号才触发出货，避免部分发货就去做资料。
            _all_siblings = [x for x in self.links
                             if x.yahoo_profile_id == pid
                             and x.yahoo_order_no == order_no]
            self.log(f"[采购出货] {acc_name}/{order_no}: siblings={len(_all_siblings)}, "
                     + ", ".join(f"{s.purchase_order_id[-8:]}={'有' if s.tracking_no else '无'}单号 watch={getattr(s,'watch',True)} status={s.status}" for s in _all_siblings))
            _missing = [s.purchase_order_id for s in _all_siblings if not (s.tracking_no or "").strip()]
            if _missing:
                self.log(f"[采购出货] {acc_name}/{order_no}: 等待其他采购单号发货 (缺{len(_missing)}个: {', '.join(_missing[:5])})")
                continue

            # sibling 检查通过：用所有 siblings 构建 shipments（不仅是本轮新获取的）
            items = _all_siblings

            # 更新状态
            for x in items:
                x.status = "生成中"
            self._merge_and_persist()
            self._ui_queue.put(("refresh", None))
            # _merge_and_persist 会替换 self.links，需要刷新 items 引用
            items = [x for x in self.links
                     if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]

            shipments = []
            for x in items:
                shipments.append({
                    "tracking_no": (x.tracking_no or "").strip(),
                    "purchase_order_id": (x.purchase_order_id or "").strip(),
                    "product_name": (x.product_name or "").strip(),
                    "spec": (x.spec or "").strip(),
                    "remark": (getattr(x, "remark", "") or "").strip(),
                    "pay_mmdd": (x.pay_mmdd or "").strip(),
                    "pay_amount": _clean_amount(x.pay_amount),
                })

            # 合并副订单 + 备注
            sub_order_nos = []
            for _x in items:
                for _s in (getattr(_x, "sub_order_nos", None) or []):
                    _s = str(_s).strip()
                    if _s and _s != order_no and _s not in sub_order_nos:
                        sub_order_nos.append(_s)
            _rs = []
            for _x in items:
                _r = (getattr(_x, "remark", "") or "").strip()
                if _r and _r not in _rs:
                    _rs.append(_r)
            order_remark = " / ".join(_rs).strip()

            orders = [{
                "order_no": order_no,
                "order_no_display": order_no + ("+" + "+".join(sub_order_nos) if sub_order_nos else ""),
                "sub_order_nos": sub_order_nos,
                "shipments": shipments,
                "remark": order_remark,
                "from_purchase_monitor": True,
                "purchase_platform": str(items[0].platform or "").strip(),
                "syb_auto_upload": False,
                "syb_latest_template_path": "",
            }]

            profile_dir = (ROOT_DIR / "profiles" / pid).resolve()
            browser_path = None
            try:
                browser_path = (self.app.var_browser.get() or "").strip() or None
            except Exception:
                browser_path = str(getattr(self.app, "settings", {}).get("browser_path", "") or "") or None

            headless = bool(self.var_headless.get())
            timeout_sec = int(getattr(self.app, "settings", {}).get("timeout_sec", 45) or 45)

            try:
                self.log(f"[采购出货] 开始生成出货Excel：{acc_name} / {order_no}")
                results = export_order_to_excel(
                    profile_id=pid,
                    account_name=acc_name,
                    profile_dir=profile_dir,
                    browser_path=browser_path,
                    headless=headless,
                    timeout_sec=timeout_sec,
                    template_path=Path(""),
                    output_dir=Path(outdir),
                    user_code=user_code,
                    owner_name=owner,
                    orders=orders,
                    on_log=self.log,
                    stop_event=self._stop_event,
                )

                # 检查结果
                ok = any(r.get("found") for r in (results or []))
                if ok:
                    self.log(f"[采购出货] 出货Excel生成成功：{acc_name} / {order_no}")

                    # 标记已生成
                    for x in items:
                        x.status = "已生成"
                    self._merge_and_persist()
                    self._ui_queue.put(("refresh", None))
                    items = [x for x in self.links
                             if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]

                    # 生成最新模板（闲鱼订单）
                    try:
                        platform = str(items[0].platform or "").strip()
                        if platform == "xianyu":
                            from core.latest_template_builder import build_latest_template_from_ship_excel
                            date_str = time.strftime("%Y%m%d", time.localtime())
                            ship_xlsx = Path(outdir) / f"出货资料_{date_str}.xlsx"
                            out_tpl = Path(outdir) / f"最新模板_{date_str}.xlsx"
                            if ship_xlsx.exists():
                                build_latest_template_from_ship_excel(
                                    ship_excel_path=ship_xlsx,
                                    output_path=out_tpl,
                                    order_nos=[order_no],
                                    log_fn=self.log,
                                )
                                orders[0]["syb_auto_upload"] = True
                                orders[0]["syb_latest_template_path"] = str(out_tpl)
                                self.log(f"[采购出货] 已生成最新模板：{out_tpl.name}")
                    except Exception as e:
                        self.log(f"[采购出货] 生成最新模板失败：{e}")

                    # 触发物流系统上传
                    # v6.1.42:用 _check_ship_login() 統一檢查(load_stoken source of truth),
                    # 修「stoken 自動恢復登入但 GUI var_status 沒同步,自動上傳被誤判跳過」bug
                    _uploaded = False
                    try:
                        syb_tab = getattr(self.app, "syb_upload_tab", None)
                        if syb_tab and self._check_ship_login():
                            for x in items:
                                x.status = "上传中"
                            self._merge_and_persist()
                            self._ui_queue.put(("refresh", None))
                            items = [x for x in self.links
                                     if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]
                            syb_tab.on_ship_results(pid, acc_name, orders, results)
                            self.log(f"[采购出货] 已触发物流系统上传：{acc_name} / {order_no}")
                            _uploaded = True
                        elif syb_tab:
                            self.log(f"[采购出货] 物流系统未登录，跳过自动上传（请先到物流系统登录）")
                    except Exception as e:
                        self.log(f"[采购出货] 物流系统上传失败：{e}")

                    # 自动上传商品图到物流系统（异步，不阻塞主流程，失败 TG 通知）
                    try:
                        if _uploaded:
                            self._schedule_image_upload(order_no)
                    except Exception as e:
                        self.log(f"[采购出货] 触发图片上传失败：{e}")

                    # 注册自动列印面单任务
                    try:
                        alw = getattr(self.app, "auto_label_worker", None)
                        if alw:
                            ch = ""
                            for r in (results or []):
                                ch = str(r.get("channel") or "")
                                if ch:
                                    break
                            if ch:
                                alw.register_task(order_no, pid, acc_name, ch)
                    except Exception as e:
                        self.log(f"[采购出货] 注册面单任务失败: {e}")

                    # 刷新 items 引用（防止上方操作后 self.links 已被替换）
                    items = [x for x in self.links
                             if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]
                    # 标记最终状态 + 自动取消监控
                    final_status = "已上传" if _uploaded else "已生成"
                    for x in items:
                        x.status = final_status
                        x.created_ship_task = True
                        x.watch = False
                else:
                    # 刷新 items 引用
                    items = [x for x in self.links
                             if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]
                    err_msg = ""
                    for r in (results or []):
                        if r.get("error"):
                            err_msg = str(r["error"])[:60]
                            break
                    for x in items:
                        x.status = "失败"
                        x.error = err_msg or "出货Excel生成失败"

            except Exception as e:
                self.log(f"[采购出货] 自动出货异常：{acc_name} / {order_no} - {e}")
                # 刷新 items 引用
                items = [x for x in self.links
                         if x.yahoo_profile_id == pid and x.yahoo_order_no == order_no]
                for x in items:
                    x.status = "失败"
                    x.error = str(e)[:60]

            self._merge_and_persist()
            self._ui_queue.put(("refresh", None))

    # =====================
    # open login browser
    # =====================
    def action_open_login_browser(self) -> None:
        chrome_path = ""
        try:
            chrome_path = self.app.var_browser.get()
        except Exception:
            pass
        t = threading.Thread(
            target=open_login_browser,
            kwargs={"app_log": self.log, "chrome_path": chrome_path},
            daemon=True,
        )
        t.start()

    def _schedule_image_upload(self, yahoo_order_no: str, delay_sec: float = 3.0) -> None:
        """异步上传该 yahoo_order 下所有带图的 PurchaseLink 到物流系统。
        delay_sec 只是「让出 UI 线程」的最短等待，实际 detail 查不到会在 upload_images_for_yahoo_order 里
        指数退避重试（0/8/15/25/40 秒），所以 doImport 在后台慢也撑得住。
        """
        def _runner():
            try:
                time.sleep(delay_sec)
                # 重读最新的 links（doImport 期间可能有更新）
                links_now = load_links()
                target = [x for x in links_now if x.yahoo_order_no == yahoo_order_no]
                if not target:
                    return
                # 仅有图且没上传过的才走流程
                has_image = [x for x in target
                             if (x.image_path or "").strip() and not getattr(x, "image_uploaded", False)]
                if not has_image:
                    return
                self.log(f"[采购图片] 开始上传 {len(has_image)} 张图（{yahoo_order_no}）")
                from .purchase_image_feature import upload_images_for_yahoo_order

                def _tg_notify(text: str) -> None:
                    try:
                        ops_bot = getattr(self.app, "_ops_tg_bot", None)
                        if ops_bot:
                            cid = str(getattr(self.app, "settings", {}).get("tg_chat_id", "")).strip()
                            if cid:
                                ops_bot.send_to(cid, text)
                    except Exception:
                        pass

                ok_n, fail_n, errs = upload_images_for_yahoo_order(
                    yahoo_order_no, target, log=self.log, tg_notify=_tg_notify,
                )
                # 把 image_uploaded 标记 持久化回 self.links
                if ok_n > 0:
                    self.links = load_links()
                    by_pk = {(x.yahoo_order_no, x.purchase_order_id): x for x in self.links}
                    for src in target:
                        if getattr(src, "image_uploaded", False):
                            dst = by_pk.get((src.yahoo_order_no, src.purchase_order_id))
                            if dst is not None:
                                dst.image_uploaded = True
                    self._persist()
                    self._ui_queue.put(("refresh", None))
                    self.log(f"[采购图片] 完成: 成功 {ok_n}/{len(has_image)}")
            except Exception as e:
                self.log(f"[采购图片] 上传线程异常：{e}")

        threading.Thread(target=_runner, daemon=True).start()

    def action_force_relogin_xianyu(self) -> None:
        """强制清空闲鱼 cookie cache，下次「打开登录浏览器」走完整登录流程（BX 指纹采集）。
        用于解决保存的 cookie 残缺/失效导致每次重开仍弹登录框的问题。
        """
        if not messagebox.askyesno("确认",
                                    "将清空已保存的闲鱼 cookie，需要重新扫码登录。\n\n"
                                    "适用场景：每次打开浏览器仍弹登录框 / 监控持续报 session_dead\n\n"
                                    "继续吗？"):
            return
        try:
            from core.purchase_login_playwright import wipe_cookie_cache, CACHE_JSON_PATH
            ok = wipe_cookie_cache()
            if ok:
                self.log(f"[采购登录] ✓ 已清空 {CACHE_JSON_PATH.name}，请点「打开登录浏览器」重新扫码登录")
                messagebox.showinfo("完成", "已清空 cookie\n请点「打开登录浏览器」扫码重新登录")
            else:
                self.log("[采购登录] ⚠ 清空 cookie 失败")
                messagebox.showerror("失败", "清空 cookie 失败，请检查文件权限")
        except Exception as e:
            self.log(f"[采购登录] 强制重新登录异常：{e}")
            messagebox.showerror("异常", str(e))

    # =====================
    # build UI
    # =====================
    def build(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        # ---- 上半部分：绑定输入区 ----
        lf = ttk.Labelframe(self.frame, text="采购订单绑定")
        lf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for c in range(7):
            lf.columnconfigure(c, weight=0)
        lf.columnconfigure(1, weight=1)
        lf.columnconfigure(4, weight=1)

        # Row 0: 账号 + Yahoo订单号 + 平台
        ttk.Label(lf, text="账号").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.cmb_account = ttk.Combobox(lf, textvariable=self.var_account, width=18)
        self.cmb_account.grid(row=0, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(lf, text="Yahoo订单号").grid(row=0, column=2, sticky="w", padx=4, pady=3)
        ttk.Entry(lf, textvariable=self.var_yahoo_order).grid(row=0, column=3, columnspan=2, sticky="ew", padx=4, pady=3)

        ttk.Label(lf, text="平台").grid(row=0, column=5, sticky="w", padx=4, pady=3)
        self.cmb_platform = ttk.Combobox(lf, textvariable=self.var_platform, values=["xianyu", "mercari"], width=10, state="readonly")
        self.cmb_platform.grid(row=0, column=6, sticky="w", padx=4, pady=3)

        # Row 1: 副订单编号
        ttk.Label(lf, text="副订单").grid(row=1, column=0, sticky="w", padx=4, pady=(0, 3))
        self._sub_orders_holder = ttk.Frame(lf)
        self._sub_orders_holder.grid(row=1, column=1, columnspan=4, sticky="ew", padx=4, pady=(0, 3))
        ttk.Button(lf, text="+副单", command=self._add_sub_order_row).grid(row=1, column=5, sticky="w", padx=4, pady=(0, 3))
        ttk.Button(lf, text="清空", command=self._clear_sub_orders).grid(row=1, column=6, sticky="w", padx=4, pady=(0, 3))
        if not self.sub_order_vars:
            self.sub_order_vars.append(tk.StringVar())
        self._rebuild_sub_order_rows()

        # Row 2: 采购订单动态行
        self.purchase_rows_holder = ttk.Frame(lf)
        self.purchase_rows_holder.grid(row=2, column=0, columnspan=7, sticky="ew", padx=4, pady=(2, 4))
        self.purchase_rows_holder.columnconfigure(1, weight=1)
        self.purchase_rows_holder.columnconfigure(2, weight=1)
        self.purchase_rows_holder.columnconfigure(3, weight=1)
        if not self.purchase_rows:
            self._add_purchase_row()

        # Row 3: 绑定按钮 + 登录浏览器
        btnrow = ttk.Frame(lf)
        btnrow.grid(row=3, column=0, columnspan=7, sticky="ew", padx=4, pady=(0, 4))
        ttk.Button(btnrow, style="Accent.TButton", text="添加绑定", command=self.action_add_bindings).pack(side="left", padx=(0, 8))
        ttk.Button(btnrow, text="打开登录浏览器", command=self.action_open_login_browser).pack(side="left", padx=(0, 8))
        ttk.Button(btnrow, text="强制重新登录闲鱼", command=self.action_force_relogin_xianyu).pack(side="left", padx=(0, 8))

        # Row 4: 出货参数 + 监控控制
        paramrow = ttk.Frame(lf)
        paramrow.grid(row=4, column=0, columnspan=7, sticky="ew", padx=4, pady=(0, 6))

        ttk.Label(paramrow, text="使用者简称:").pack(side="left")
        ttk.Entry(paramrow, textvariable=self.var_user_code, width=8).pack(side="left", padx=(2, 8))
        ttk.Label(paramrow, text="所属人:").pack(side="left")
        ttk.Entry(paramrow, textvariable=self.var_owner, width=8).pack(side="left", padx=(2, 8))
        ttk.Label(paramrow, text="输出目录:").pack(side="left")
        ttk.Entry(paramrow, textvariable=self.var_outdir, width=20).pack(side="left", padx=(2, 4))
        ttk.Button(paramrow, text="选择", command=self._choose_outdir).pack(side="left", padx=(0, 8))

        # Row 5: 监控控制
        ctrlrow = ttk.Frame(lf)
        ctrlrow.grid(row=5, column=0, columnspan=7, sticky="ew", padx=4, pady=(0, 6))

        ttk.Checkbutton(ctrlrow, text="无头模式", variable=self.var_headless).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(ctrlrow, text="自动出货", variable=self.var_auto_ship).pack(side="left", padx=(0, 8))
        ttk.Label(ctrlrow, text="监控间隔:").pack(side="left")
        ttk.Entry(ctrlrow, textvariable=self.var_interval, width=8).pack(side="left", padx=(2, 2))
        ttk.Label(ctrlrow, text="秒").pack(side="left", padx=(0, 12))

        self.btn_monitor = ttk.Button(ctrlrow, style="Accent.TButton", text="开始监控", command=self.action_start)
        self.btn_monitor.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(ctrlrow, text="停止监控", command=self.action_stop)
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_stop.pack_forget()  # 默认隐藏

        # ---- 下半部分：记录表格 ----
        lf_table = ttk.Labelframe(self.frame, text="采购记录")
        lf_table.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        lf_table.columnconfigure(0, weight=1)
        lf_table.rowconfigure(0, weight=1)

        cols = ('watch', 'acc', 'yahoo_order', 'platform', 'purchase_order', 'product_name', 'image', 'tracking', 'pay_date', 'pay_amount', 'status', 'error')
        self.tree = ttk.Treeview(lf_table, columns=cols, show="headings")

        headings = {
            "watch": "监控", "acc": "账号", "yahoo_order": "Yahoo订单", "platform": "平台",
            "purchase_order": "采购订单", "product_name": "商品名称",
            "image": "图片",
            "tracking": "物流单号", "pay_date": "代付日期",
            "pay_amount": "代付金额", "status": "状态", "error": "错误",
        }
        widths = {
            "watch": 50, "acc": 120, "yahoo_order": 130, "platform": 60,
            "purchase_order": 150, "product_name": 180,
            "image": 60,
            "tracking": 140, "pay_date": 80,
            "pay_amount": 80, "status": 90, "error": 160,
        }

        for c in cols:
            self.tree.heading(c, text=headings.get(c, c))
            self.tree.column(c, width=widths.get(c, 120), anchor="w", stretch=True)
        self.tree.column("watch", anchor="center", stretch=False)
        self.tree.column("platform", anchor="center", stretch=False)
        self.tree.column("image", anchor="center", stretch=False)
        self.tree.column("pay_date", anchor="center", stretch=False)
        self.tree.column("pay_amount", anchor="e", stretch=False)
        self.tree.column("status", anchor="center", stretch=False)

        vsb = ttk.Scrollbar(lf_table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(lf_table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_tree_click)

        # 操作按钮
        lf_ctl = ttk.Frame(self.frame)
        lf_ctl.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

        ttk.Button(lf_ctl, text="抓取一次", command=self.action_scrape_selected_once).pack(side="left", padx=(0, 8))
        ttk.Button(lf_ctl, text="删除选中", command=self.action_delete_selected).pack(side="left", padx=(0, 8))
        ttk.Button(lf_ctl, text="编辑选中", command=self._open_edit_dialog).pack(side="left", padx=(0, 8))
        # v6.0.73 新增:立即出货按钮 — 选中记录直接走自动出货流程(列印/上传都自动)
        ttk.Button(lf_ctl, text="立即出货", style="Accent.TButton",
                   command=self.action_ship_selected_now).pack(side="left", padx=(0, 8))

        # 初始化
        self._refresh_accounts()
        self._render_tree()
        self.frame.after(400, self._poll_ui_queue)

    # =====================
    # UI polling / helpers
    # =====================
    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "refresh":
                    self.links = load_links()
                    self._render_tree()
                elif kind == "monitor_stopped":
                    self._update_monitor_btn(False)
        except Exception:
            pass
        finally:
            self.frame.after(400, self._poll_ui_queue)

    def _choose_outdir(self) -> None:
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(d)
