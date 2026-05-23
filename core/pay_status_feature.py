from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, messagebox

import requests

from core.accounts import save_settings

# 备注（状态）里这些视为“已完结/不可改”
STATUS_DONE_SET = {"已出貨", "已退款", "異常"}



# 默认内置（分发给同事时无需手动填写）
DEFAULT_PAYSTATUS_API_URL = "https://script.google.com/macros/s/<GAS_DEPLOYMENT_ID>/exec"
DEFAULT_PAYSTATUS_TOKEN = "<PLACEHOLDER_TOKEN_REDACTED>"
class PayStatusFeatureTab:
    """代付情况（云端 Google Sheets + Apps Script）

    关键点（按你现在的需求）：
    - 列表默认只看“未完成”（已出貨/已退款/異常 不显示）
    - 这页只允许改「備註」文本，不允许改「备注(状态)」
    - 保存成功后：立刻在列表里更新该行的「備註」，并自动刷新一次；同时给出“回传成功”提示
    """

    def __init__(self, app, frame):
        self.app = app
        self.frame = frame

        st = getattr(app, "settings", {}) or {}
        self.var_name = tk.StringVar(value=str(st.get("paystatus_name", "") or ""))
        self.var_api_url = tk.StringVar(value=str(st.get("paystatus_api_url") or DEFAULT_PAYSTATUS_API_URL))
        self.var_token = tk.StringVar(value=str(st.get("paystatus_token") or DEFAULT_PAYSTATUS_TOKEN))

        # 默认只看未完成（你要求“已出貨/已退款/異常 不显示”）
        self.var_only_unfinished = tk.BooleanVar(value=bool(st.get("paystatus_only_unfinished", True)))

        self.var_sel_rid = tk.StringVar(value="")
        self.var_msg = tk.StringVar(value="")

        self.tree: Optional[ttk.Treeview] = None
        self.txt_note: Optional[tk.Text] = None
        self.btn_save: Optional[ttk.Button] = None
        self.btn_refresh: Optional[ttk.Button] = None

        self._rows: List[Dict[str, Any]] = []
        self._sel_tree_iid: Optional[str] = None
        self._sel_index: Optional[int] = None

    # ---------------- utils ----------------
    def log(self, s: str) -> None:
        try:
            self.app.log(s)
        except Exception:
            print(s)

    def _set_msg(self, s: str, auto_clear_ms: int = 3500) -> None:
        self.var_msg.set(s)
        if auto_clear_ms > 0:
            try:
                self.app.after(auto_clear_ms, lambda: self.var_msg.set(""))
            except Exception:
                pass

    # ---------------- UI ----------------
    def build(self) -> None:
        root = self.frame
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        top.columnconfigure(3, weight=1)

        _fam = getattr(self.app, "_base_family", "Microsoft JhengHei UI")
        ttk.Label(top, text="姓名").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.var_name, width=14, font=(_fam, 10)).grid(row=0, column=1, sticky="w", padx=(6, 16))

        ttk.Checkbutton(top, text="只看未完成", variable=self.var_only_unfinished).grid(row=0, column=2, sticky="w", padx=(0, 10))

        ttk.Button(top, text="保存設定", command=self._save_settings).grid(row=0, column=3, padx=4)
        self.btn_refresh = ttk.Button(top, text="刷新", command=self.refresh)
        self.btn_refresh.grid(row=0, column=4, padx=4)

        ttk.Label(top, textvariable=self.var_msg, foreground="#007700").grid(row=0, column=5, sticky="w", padx=(10, 0))

        # tree
        mid = ttk.Frame(root)
        mid.grid(row=1, column=0, sticky="nsew", padx=6)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        # 注意：这里 columns 与 _render_rows values 必须严格对齐，否则“備註”会显示不出来
        # 按你截图：列表不显示“备注(status)”列，只显示“備註”列
        cols = ("pay_date", "amount", "name", "total", "ship_date", "time", "code", "note")
        tree = ttk.Treeview(mid, columns=cols, show="headings", height=16)
        self.tree = tree

        headings = {
            "pay_date": "代付日期",
            "amount": "金額",
            "name": "姓名",
            "total": "合总",
            "ship_date": "出貨日期",
            "time": "時間",
            "code": "編碼",
            "note": "備註",
        }
        widths = {
            "pay_date": 90,
            "amount": 70,
            "name": 90,
            "total": 70,
            "ship_date": 90,
            "time": 90,
            "code": 90,
            "note": 280,
        }

        for c in cols:
            tree.heading(c, text=headings.get(c, c))
            tree.column(c, width=widths.get(c, 100), anchor="center")
        tree.column("note", anchor="w")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        tree.bind("<<TreeviewSelect>>", self._on_select)

        # editor
        bot = ttk.Labelframe(root, text="编辑选中记录（只回写：備註）")
        bot.grid(row=2, column=0, sticky="ew", padx=6, pady=(6, 8))
        bot.columnconfigure(3, weight=1)

        # 使用指引
        guide = ttk.Label(bot, text="使用方式：填写姓名 → 点刷新查看代付记录 → 选中一行 → 在備註栏写入代付情况（如：已付款/已收到货/有问题等）→ 点保存，管理员即可看到",
                          foreground="gray", wraplength=600, justify="left")
        guide.grid(row=0, column=0, columnspan=5, sticky="w", padx=6, pady=(4, 2))

        self.btn_save = ttk.Button(bot, text="保存选中", command=self.save_selected)
        self.btn_save.grid(row=1, column=0, sticky="w", padx=6, pady=4)

        ttk.Label(bot, text="備註").grid(row=1, column=1, sticky="nw", padx=6, pady=4)
        txt = tk.Text(bot, height=3, width=60)
        txt.grid(row=1, column=2, columnspan=3, sticky="ew", padx=(0, 6), pady=(0, 6))
        self.txt_note = txt

        self.refresh()

    def _save_settings(self) -> None:
        st = getattr(self.app, "settings", {}) or {}
        st["paystatus_name"] = (self.var_name.get() or "").strip()
        st["paystatus_api_url"] = (self.var_api_url.get() or "").strip()
        st["paystatus_token"] = (self.var_token.get() or "").strip()
        st["paystatus_only_unfinished"] = bool(self.var_only_unfinished.get())
        try:
            save_settings(st)
            self.log("[PAY] 已保存代付情况设置")
        except Exception as e:
            self.log(f"[PAY] 保存设置失败：{e}")

    # ---------------- data ----------------
    def refresh(self) -> None:
        name = (self.var_name.get() or "").strip()
        api_url = (self.var_api_url.get() or "").strip()
        token = (self.var_token.get() or "").strip()

        if not name:
            self.log("[PAY] 请先填写姓名")
            return
        if not api_url:
            self.log("[PAY] 请先填写 API URL（Apps Script Web App 地址）")
            return

        self._save_settings()

        if self.btn_refresh:
            try:
                self.btn_refresh.configure(state="disabled")
            except Exception:
                pass

        self._set_msg("刷新中...", auto_clear_ms=0)

        # 主執行緒先讀 Tk Var(避免 thread 內讀拋 main thread is not in main loop)
        only_unfinished = bool(self.var_only_unfinished.get())

        def worker():
            try:
                params = {
                    "action": "fetch",
                    "name": name,
                    "token": token,
                    "only_open": 1 if only_unfinished else 0,
                    "limit": 250,
                }
                # 你之前 20 秒会 timeout；这里给 60 秒，避免网络偶发慢
                r = requests.get(api_url, params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not data.get("ok"):
                    raise RuntimeError(str((data or {}).get("error") or "云端返回异常"))

                rows = data.get("rows") or []
                if not isinstance(rows, list):
                    rows = []

                # 保险：前端再次过滤“已完结”
                if only_unfinished:
                    rows = [x for x in rows if str(x.get("status", "") or "").strip() not in STATUS_DONE_SET]

                self._rows = rows
                self.app.after(0, lambda: self._render_rows(rows))
            except Exception as e:
                self.log(f"[PAY] 刷新失败：{e}")
                self.app.after(0, lambda: self._set_msg("❌ 刷新失败", auto_clear_ms=3000))
            finally:
                if self.btn_refresh:
                    try:
                        self.app.after(0, lambda: self.btn_refresh.configure(state="normal"))
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _render_rows(self, rows: List[Dict[str, Any]]) -> None:
        if not self.tree:
            return
        tree = self.tree
        tree.delete(*tree.get_children())

        for x in rows:
            tree.insert(
                "",
                "end",
                values=(
                    str(x.get("pay_date", "") or ""),
                    str(x.get("amount", "") or ""),
                    str(x.get("name", "") or ""),
                    str(x.get("total", "") or ""),
                    str(x.get("ship_date", "") or ""),
                    str(x.get("time", "") or ""),
                    str(x.get("code", "") or ""),
                    str(x.get("note", "") or ""),
                ),
            )

        # 清空编辑器
        self.var_sel_rid.set("")
        self._sel_tree_iid = None
        self._sel_index = None

        if self.txt_note:
            self.txt_note.configure(state="normal")
            self.txt_note.delete("1.0", "end")

        if self.btn_save:
            try:
                self.btn_save.configure(state="disabled")
            except Exception:
                pass

        self._set_msg(f"✅ 已刷新 {len(rows)} 条", auto_clear_ms=2500)

    def _on_select(self, _evt=None) -> None:
        if not self.tree:
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        self._sel_tree_iid = iid

        # 用 index 对齐缓存 rows（这是最稳的方式）
        try:
            idx = int(self.tree.index(iid))
        except Exception:
            idx = -1
        self._sel_index = idx if 0 <= idx < len(self._rows) else None

        row = self._rows[self._sel_index] if self._sel_index is not None else {}
        rid = str(row.get("rid", "") or "")
        status = str(row.get("status", "") or "").strip()
        note = str(row.get("note", "") or "")

        self.var_sel_rid.set(rid)

        if self.txt_note:
            self.txt_note.configure(state="normal")
            self.txt_note.delete("1.0", "end")
            self.txt_note.insert("1.0", note)

        # 已完结记录：只读（你要求“已出貨/已退款/異常 只能看不能改”）
        if status in STATUS_DONE_SET:
            if self.txt_note:
                self.txt_note.configure(state="disabled")
            if self.btn_save:
                try:
                    self.btn_save.configure(state="disabled")
                except Exception:
                    pass
            self._set_msg(f"此记录为「{status}」，只读", auto_clear_ms=2500)
        else:
            if self.txt_note:
                self.txt_note.configure(state="normal")
            if self.btn_save:
                try:
                    self.btn_save.configure(state="normal")
                except Exception:
                    pass

    def save_selected(self) -> None:
        api_url = (self.var_api_url.get() or "").strip()
        token = (self.var_token.get() or "").strip()
        rid = (self.var_sel_rid.get() or "").strip()

        if not api_url:
            messagebox.showerror("错误", "未设置 API URL")
            return
        if not rid:
            messagebox.showinfo("提示", "请先选中一行")
            return

        note = ""
        if self.txt_note:
            try:
                note = (self.txt_note.get("1.0", "end") or "").strip()
            except Exception:
                note = ""

        # 防重复点击
        if self.btn_save:
            try:
                self.btn_save.configure(state="disabled")
            except Exception:
                pass

        self._set_msg("回传中...", auto_clear_ms=0)

        # 重要：你现在的 Apps Script（applyUpdates_）会**无条件**写入 status 字段：
        #   sh.getRange(...status...).setValue(String(u.status || '').trim())
        # 如果我们不发送 status，就会被当成 '' 写回去，导致“备注”列被清空。
        # 所以这里固定把“当前 status 原样带回去”，实现“软件端只改備註，不改备注”。
        current_status = ""
        try:
            if self._sel_index is not None and 0 <= self._sel_index < len(self._rows):
                current_status = str(self._rows[self._sel_index].get("status", "") or "").strip()
        except Exception:
            current_status = ""

        payload = {
            "action": "update",
            "token": token,
            "updates": [
                {
                    "rid": rid,
                    "status": current_status,  # 原样回传（只为防清空）
                    "note": note,
                }
            ],
        }

        def worker():
            try:
                r = requests.post(
                    api_url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or not data.get("ok"):
                    raise RuntimeError(str((data or {}).get("error") or "云端返回异常"))

                # 1) 立即更新本地列表（让用户马上看到已经写入）
                def ui_apply_local():
                    if self._sel_index is not None and 0 <= self._sel_index < len(self._rows):
                        self._rows[self._sel_index]["note"] = note

                    if self.tree and self._sel_tree_iid is not None:
                        try:
                            vals = list(self.tree.item(self._sel_tree_iid, "values") or [])
                            # columns 最后一个就是 note
                            if len(vals) >= 9:
                                vals[8] = note
                                self.tree.item(self._sel_tree_iid, values=tuple(vals))
                        except Exception:
                            pass

                    self._set_msg("✅ 回传成功，自动刷新中...", auto_clear_ms=2500)

                self.app.after(0, ui_apply_local)

                # 2) 再自动刷新一次（确保云端/本地一致）
                self.app.after(200, self.refresh)

            except Exception as e:
                self.log(f"[PAY] 保存失败：{e}")
                self.app.after(0, lambda: self._set_msg("❌ 回传失败", auto_clear_ms=3500))
            finally:
                # 重新放开保存按钮（如果当前选中的是可编辑记录）
                def ui_unlock():
                    if not self.btn_save:
                        return
                    try:
                        if self._sel_index is None or not (0 <= self._sel_index < len(self._rows)):
                            self.btn_save.configure(state="disabled")
                            return
                        status = str(self._rows[self._sel_index].get("status", "") or "").strip()
                        if status in STATUS_DONE_SET:
                            self.btn_save.configure(state="disabled")
                        else:
                            self.btn_save.configure(state="normal")
                    except Exception:
                        pass

                self.app.after(0, ui_unlock)

        threading.Thread(target=worker, daemon=True).start()
