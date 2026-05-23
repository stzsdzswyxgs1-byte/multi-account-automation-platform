from __future__ import annotations

import json
import os
import threading
import webbrowser
from typing import Any, Optional

import openpyxl
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

from core.accounts import load_settings, save_settings
from core.tg_kv_poller import load_relay_config


# ===== 云端 D1 配置 =====
DEFAULT_WORKER_URL = "https://product-query.<PHONE_REDACTED>.workers.dev"
DEFAULT_UPLOAD_TOKEN = "<D1_UPLOAD_TOKEN_REDACTED>"


class DocUploadFeatureTab:
    """
    编码数据更新 — 上传 / 下载 / 查询云端 D1 商品编码数据
    """

    def __init__(self, app: Any, frame: tk.Widget):
        self.app = app
        self.frame = frame

        self._busy = False
        self._selected_path: str = ""

        # owner 自动从 KV 中转配置的绑定 TG ID 读取
        _relay = load_relay_config()
        _owner = str(_relay.get("user_id", "") or "").strip()

        self.var_owner = tk.StringVar(value=_owner)
        self.var_file = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="")
        self.var_query_code = tk.StringVar(value="")
        self.var_query_result = tk.StringVar(value="")
        self.var_stats = tk.StringVar(value="")
        self.var_upload_mode = tk.StringVar(value="追加更新")

        self._btn_upload: Optional[ttk.Button] = None
        self._btn_pick: Optional[ttk.Button] = None
        self._btn_query: Optional[ttk.Button] = None
        self._btn_dl_mercari: Optional[ttk.Button] = None
        self._btn_dl_goofish: Optional[ttk.Button] = None
        self._btn_dl_all: Optional[ttk.Button] = None
        self._query_result_text: Optional[tk.Text] = None

    def _log(self, msg: str) -> None:
        try:
            self.app.log(msg)
        except Exception:
            print(msg)

    def _ui(self, fn):
        try:
            self.app.after(0, fn)
        except Exception:
            fn()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy

        def _apply():
            st = "disabled" if busy else "normal"
            for btn in (self._btn_upload, self._btn_pick,
                        self._btn_query, self._btn_dl_mercari,
                        self._btn_dl_goofish, self._btn_dl_all):
                if btn:
                    btn.configure(state=st)

        self._ui(_apply)

    def build(self) -> None:
        # v6.1.25:加 Canvas + Scrollbar 讓 tab 內容超過視窗高度時可滾動
        # (D1 對齊區 + 查詢區放一起會擠出視窗,跟「檢測」tab 同樣 pattern)
        outer = self.frame
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        root = ttk.Frame(canvas)

        root.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=root, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        # 鼠標滾輪
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 讓內部 frame 寬度跟隨 canvas
        def _on_canvas_resize(event):
            canvas.itemconfig(canvas.find_all()[0], width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        root.columnconfigure(0, weight=1)

        hint = "上传 Excel 到云端数据库，或下载/查询已有数据。"
        ttk.Label(root, text=hint, justify="left", wraplength=900).pack(
            anchor="w", padx=8, pady=(8, 6))

        # ===== 上传区 =====
        box_up = ttk.LabelFrame(root, text="上传编码数据")
        box_up.pack(fill="x", padx=8, pady=6)
        box_up.columnconfigure(1, weight=1)

        ttk.Label(box_up, text="Excel 文件").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(box_up, textvariable=self.var_file).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        self._btn_pick = ttk.Button(
            box_up, text="选择文件", command=self._pick_file)
        self._btn_pick.grid(row=0, column=2, sticky="e", padx=6, pady=4)

        ttk.Label(box_up, text="格式：商品條碼 | 商品編號 | 帳號（3列）",
                  foreground="gray").grid(
            row=1, column=1, sticky="w", padx=6, pady=0)

        btn_frame = ttk.Frame(box_up)
        btn_frame.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 6))
        self._btn_upload = ttk.Button(
            btn_frame, text="开始上传", command=self._start_upload)
        self._btn_upload.pack(side="left")
        ttk.Combobox(
            btn_frame, textvariable=self.var_upload_mode, width=10,
            values=["追加更新", "覆盖更新"], state="readonly",
        ).pack(side="left", padx=(8, 0))

        ttk.Label(box_up, textvariable=self.var_status).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))

        # ===== 下载区 =====
        box_dl = ttk.LabelFrame(root, text="下载数据 (CSV)")
        box_dl.pack(fill="x", padx=8, pady=6)

        dl_frame = ttk.Frame(box_dl)
        dl_frame.pack(fill="x", padx=6, pady=6)

        self._btn_dl_mercari = ttk.Button(
            dl_frame, text="下载煤炉数据", command=lambda: self._download("mercari"))
        self._btn_dl_mercari.pack(side="left", padx=(0, 8))

        self._btn_dl_goofish = ttk.Button(
            dl_frame, text="下载闲鱼数据", command=lambda: self._download("goofish"))
        self._btn_dl_goofish.pack(side="left", padx=(0, 8))

        self._btn_dl_all = ttk.Button(
            dl_frame, text="下载全部数据", command=lambda: self._download("all"))
        self._btn_dl_all.pack(side="left", padx=(0, 8))

        stats_frame = ttk.Frame(box_dl)
        stats_frame.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(stats_frame, textvariable=self.var_stats,
                  foreground="gray").pack(side="left")
        ttk.Button(stats_frame, text="刷新统计",
                   command=lambda: threading.Thread(target=self._load_stats, daemon=True).start()
                   ).pack(side="left", padx=(8, 0))

        # ===== D1 對齊區(v6.1.25)— 移到查詢前,確保視窗高度不夠時也能看到 =====
        box_rec = ttk.LabelFrame(
            root, text="D1 對齊(刪除 Yahoo 已下架但 D1 還有的編碼)")
        box_rec.pack(fill="x", padx=8, pady=6)

        hint_rec = (
            "邏輯:奇摩帳號還在上架 → D1 保留;Yahoo 已下架 → D1 刪掉;"
            "非奇摩帳號(例 rosa9855)→ 一律保留。\n"
            "預覽完不會刪,確認後再按「實際執行」。每次都會自動備份 D1 全量到 "
            "runtime/d1_backups/(可手動 POST upload-batch 回滾)。"
        )
        ttk.Label(
            box_rec, text=hint_rec, justify="left", wraplength=900,
            foreground="gray",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 4))

        # 並發數
        ttk.Label(box_rec, text="並發帳號數:").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        self.var_recon_concurrency = tk.IntVar(value=5)
        ttk.Spinbox(
            box_rec, from_=1, to=20, width=5,
            textvariable=self.var_recon_concurrency,
        ).grid(row=1, column=1, sticky="w", padx=(0, 12), pady=4)

        # buttons — v6.1.25 簡化:一個「開始對齊」會先預覽再彈窗確認
        btn_rec_frame = ttk.Frame(box_rec)
        btn_rec_frame.grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 4))

        self._btn_recon_start = ttk.Button(
            btn_rec_frame, text="開始對齊",
            command=self._start_reconcile,
        )
        self._btn_recon_start.pack(side="left")

        self._btn_recon_stop = ttk.Button(
            btn_rec_frame, text="停止",
            command=self._stop_reconcile,
        )
        self._btn_recon_stop.pack(side="left", padx=(8, 0))

        # v6.1.25:下架 Yahoo 多出來的(讀最近 missing_plan.xlsx)
        self._btn_cleanup_yahoo = ttk.Button(
            btn_rec_frame, text="下架 Yahoo 多出",
            command=self._cleanup_yahoo_extras,
        )
        self._btn_cleanup_yahoo.pack(side="left", padx=(24, 0))

        # 從備份恢復 — 隔開放右邊強調是事後補救
        self._btn_recon_restore = ttk.Button(
            btn_rec_frame, text="從備份恢復",
            command=self._restore_from_backup,
        )
        self._btn_recon_restore.pack(side="left", padx=(8, 0))

        self.var_recon_status = tk.StringVar(value="")
        ttk.Label(
            box_rec, textvariable=self.var_recon_status,
            foreground="gray", wraplength=900, justify="left",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        self._recon_stop_flag = False
        self._recon_running = False

        # ===== 查询区(移到 D1 對齊下面,height 縮到 3 行省空間)=====
        box_q = ttk.LabelFrame(root, text="商品编号查询")
        box_q.pack(fill="x", padx=8, pady=6)
        box_q.columnconfigure(1, weight=1)

        ttk.Label(box_q, text="商品编号").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        entry_q = ttk.Entry(box_q, textvariable=self.var_query_code)
        entry_q.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        entry_q.bind("<Return>", lambda e: self._do_query())

        self._btn_query = ttk.Button(
            box_q, text="查询", command=self._do_query)
        self._btn_query.grid(row=0, column=2, sticky="e", padx=6, pady=4)

        # 查询结果区域（支持可点击链接）
        self._query_result_text = tk.Text(
            box_q, height=6, wrap="word", relief="flat",
            background=root.winfo_toplevel().cget("bg"),
            cursor="arrow", state="disabled",
        )
        self._query_result_text.grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 6))
        self._query_result_text.tag_configure(
            "link", foreground="blue", underline=True)
        self._query_result_text.tag_bind(
            "link", "<Enter>",
            lambda e: self._query_result_text.configure(cursor="hand2"))
        self._query_result_text.tag_bind(
            "link", "<Leave>",
            lambda e: self._query_result_text.configure(cursor="arrow"))

        # 启动时加载统计
        threading.Thread(target=self._load_stats, daemon=True).start()

    # ===== 文件选择 =====

    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要上传的 Excel",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._selected_path = path
        self.var_file.set(path)
        self.var_status.set("")
        self._log(f"[DOC] 已选择文件：{path}")

    # ===== 上传 =====

    def _start_upload(self) -> None:
        if self._busy:
            return

        owner = self.var_owner.get().strip()
        file_path = (self._selected_path or self.var_file.get() or "").strip()

        if not owner:
            messagebox.showwarning("提示", "未检测到绑定 TG ID，请先配置 KV 中转")
            return
        if not file_path:
            messagebox.showwarning("提示", "请先选择 Excel 文件")
            return
        if not file_path.lower().endswith(".xlsx"):
            messagebox.showwarning("提示", "只支持 .xlsx 格式")
            return

        append_mode = self.var_upload_mode.get() == "追加更新"
        threading.Thread(
            target=self._upload_worker, args=(owner, file_path, append_mode),
            daemon=True).start()

    def _upload_worker(self, owner: str, file_path: str, append_mode: bool = False) -> None:
        self._set_busy(True)
        self._ui(lambda: self.var_status.set("⏳ 解析 Excel 中…"))
        self._log(f"[DOC] 上传到云端：owner={owner} file={file_path}")

        try:
            # 客户端解析 xlsx
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
            ncols = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not ncols:
                    ncols = len(row)
                bc = str(row[col_map["barcode"]] or "").strip() if "barcode" in col_map and col_map["barcode"] < ncols else ""
                pc = str(row[col_map["product_code"]] or "").strip() if "product_code" in col_map and col_map["product_code"] < ncols else ""
                acc = str(row[col_map["account"]] or "").strip() if "account" in col_map and col_map["account"] < ncols else ""
                if bc or pc:
                    rows.append({"barcode": bc, "product_code": pc, "account": acc})
            wb.close()

            if not rows:
                self._ui(lambda: self.var_status.set("❌ Excel 无有效数据"))
                return

            total = len(rows)
            self._log(f"[DOC] 解析完成：{total} 条记录，开始分批上传")

            # 分批上传
            BATCH = 10000
            inserted = 0
            for i in range(0, total, BATCH):
                batch = rows[i:i + BATCH]
                clear = False if append_mode else (i == 0)  # 追加模式不清空
                msg = f"⏳ 上传中… {min(i + BATCH, total)}/{total}"
                self._ui(lambda m=msg: self.var_status.set(m))
                resp = requests.post(
                    f"{DEFAULT_WORKER_URL}/api/upload-batch",
                    json={"token": DEFAULT_UPLOAD_TOKEN, "owner": owner, "rows": batch, "clear": clear},
                    timeout=60,
                )
                result = resp.json()
                if not result.get("ok"):
                    err = result.get("error", "未知错误")
                    self._ui(lambda e=err, n=inserted: self.var_status.set(f"❌ 上传失败：{e}（已上传 {n} 条，共 {total} 条）"))
                    self._log(f"[DOC] 上传失败：{err}（已上传 {inserted}/{total}）")
                    return
                inserted += result.get("inserted", 0)

            msg = f"✅ 上传成功！更新 {inserted} 条记录"
            self._ui(lambda: self.var_status.set(msg))
            self._log(f"[DOC] 上传成功：{owner} → {inserted} 条")
            self._load_stats()
        except Exception as e:
            self._ui(lambda: self.var_status.set(f"❌ 上传失败：{e}"))
            self._log(f"[DOC] 上传异常：{e}")
        finally:
            self._set_busy(False)

    # ===== 下载 =====

    def _download(self, dtype: str) -> None:
        owner = self.var_owner.get().strip()
        if not owner:
            messagebox.showwarning("提示", "未检测到绑定 TG ID，请先配置 KV 中转")
            return
        if self._busy:
            return

        save_path = filedialog.asksaveasfilename(
            title="保存 CSV",
            defaultextension=".csv",
            initialfile=f"export_{owner}_{dtype}.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not save_path:
            return

        threading.Thread(
            target=self._download_worker, args=(owner, dtype, save_path),
            daemon=True).start()

    def _download_worker(self, owner: str, dtype: str, save_path: str) -> None:
        self._set_busy(True)
        label = {"mercari": "煤炉", "goofish": "闲鱼", "all": "全部"}[dtype]
        self._ui(lambda: self.var_status.set(f"⏳ 正在下载{label}数据…"))

        try:
            params = {"owner": owner}
            if dtype != "all":
                params["type"] = dtype
            resp = requests.get(
                f"{DEFAULT_WORKER_URL}/api/export",
                params=params, timeout=60,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")

            with open(save_path, "wb") as f:
                f.write(resp.content)

            self._ui(lambda: self.var_status.set(
                f"✅ {label}数据已保存到：{save_path}"))
            self._log(f"[DOC] 下载{label}数据：{save_path}")
        except Exception as e:
            self._ui(lambda: self.var_status.set(f"❌ 下载失败：{e}"))
            self._log(f"[DOC] 下载失败：{e}")
        finally:
            self._set_busy(False)

    # ===== D1 對齊(v6.1.25)=====

    def _start_reconcile(self) -> None:
        """v6.1.25 新流程:跑分析 → 彈窗顯示表格 → 用戶按確認才刪除。"""
        if self._recon_running:
            messagebox.showinfo("提示", "對齊正在執行中,請等待或按停止")
            return

        owner = (self.var_owner.get() or "").strip()
        if not owner:
            messagebox.showwarning("提示", "未檢測到綁定 TG ID,請先配置 KV 中轉")
            return

        try:
            concurrency = int(self.var_recon_concurrency.get() or 5)
            concurrency = max(1, min(20, concurrency))
        except Exception:
            concurrency = 5

        self._recon_stop_flag = False
        self._recon_running = True
        self._ui(lambda: self.var_recon_status.set(
            "⏳ 分析中(預計 2-3 分鐘),請看下方日誌..."))

        threading.Thread(
            target=self._reconcile_worker,
            args=(owner, concurrency),
            daemon=True,
        ).start()

    def _stop_reconcile(self) -> None:
        if not self._recon_running:
            return
        self._recon_stop_flag = True
        self._log("[D1對齊] 收到停止信號,正在收尾...")
        self._ui(lambda: self.var_recon_status.set(
            "⚠️ 收到停止信號,正在收尾..."))

    def _cleanup_yahoo_extras(self) -> None:
        """v6.1.25:一鍵把 Yahoo 上「D1 沒對應」的商品批量下架+刪除。

        資料來源:最近一次「開始對齊」產出的 missing_plan.xlsx。
        如果沒最近 backup → 提示用戶先跑對齊。
        """
        if self._recon_running:
            messagebox.showinfo("提示", "對齊/清理操作中,等完成再點")
            return

        from pathlib import Path
        base = Path(__file__).resolve().parent.parent
        backup_root = base / "runtime" / "d1_backups"
        if not backup_root.exists():
            messagebox.showinfo(
                "沒有資料",
                "找不到對齊備份。請先按「開始對齊」算出 missing 清單,"
                "再回來用「下架 Yahoo 多出」一鍵處理。",
            )
            return

        recent = sorted(
            [d for d in backup_root.iterdir()
             if d.is_dir() and d.name.startswith("reconcile_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not recent:
            messagebox.showinfo(
                "沒有資料",
                "runtime/d1_backups/ 內沒找到任何 reconcile_xxx 目錄,"
                "請先按「開始對齊」",
            )
            return

        latest = recent[0]
        missing_xlsx = latest / "missing_plan.xlsx"
        if not missing_xlsx.exists():
            messagebox.showinfo(
                "沒有 missing 資料",
                f"最近的對齊({latest.name})沒產出 missing_plan.xlsx,"
                "可能那次對齊時 Yahoo 跟 D1 已經完全對齊。",
            )
            return

        # 讀 xlsx
        try:
            wb = openpyxl.load_workbook(missing_xlsx, read_only=True)
            ws = wb.active
            headers = [
                str(c.value or "").strip().lower()
                for c in next(ws.iter_rows(min_row=1, max_row=1))
            ]
            col_map = {}
            for i, h in enumerate(headers):
                if h in ("商品條碼", "商品条码", "條碼", "条码", "barcode"):
                    col_map["barcode"] = i
                elif h in ("商品編號", "商品编号", "編號", "编号",
                           "product_code", "商品編碼", "商品编码"):
                    col_map["product_code"] = i
                elif h in ("帳號", "帐号", "賬號", "账号", "account"):
                    col_map["account"] = i

            records = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                pc = str(row[col_map["product_code"]] or "").strip() if "product_code" in col_map else ""
                acc = str(row[col_map["account"]] or "").strip() if "account" in col_map else ""
                if pc and acc:
                    records.append({"product_code": pc, "account": acc})
            wb.close()
        except Exception as e:
            messagebox.showerror("錯誤", f"讀取 missing_plan.xlsx 失敗:{e}")
            return

        if not records:
            messagebox.showinfo("沒有資料", "missing_plan.xlsx 沒有有效記錄")
            return

        # 按 account 分組,顯示確認
        from collections import Counter
        by_acc = Counter(r["account"] for r in records)
        by_acc_text = "\n".join(
            f"  • {acc}: {n} 件"
            for acc, n in sorted(by_acc.items(), key=lambda x: -x[1])
        )

        confirm = messagebox.askyesno(
            "確認從 Yahoo 下架 + 刪除",
            f"來源:{latest.name}/missing_plan.xlsx\n"
            f"準備從 Yahoo **下架後刪除** 以下商品:\n\n"
            f"{by_acc_text}\n\n"
            f"合計 {len(records)} 件,{len(by_acc)} 個帳號\n\n"
            f"⚠️ 此操作會「真實修改」Yahoo 上架狀態(下架後刪除),不可逆!\n"
            f"確定繼續嗎?",
        )
        if not confirm:
            return

        self._recon_running = True
        self._recon_stop_flag = False
        self._ui(lambda: self.var_recon_status.set(
            f"⏳ 下架 Yahoo 多出 {len(records)} 件中,看下方日誌..."))

        threading.Thread(
            target=self._yahoo_cleanup_worker,
            args=(records,),
            daemon=True,
        ).start()

    def _yahoo_cleanup_worker(self, records: list) -> None:
        """v6.1.25:背景執行 cleanup_yahoo_extras。"""
        import asyncio
        from pathlib import Path
        from core.d1_reconcile import cleanup_yahoo_extras

        try:
            chrome_path = ""
            try:
                chrome_path = (
                    str(self.app.settings.get("browser_path", "") or "").strip()
                )
            except Exception:
                pass

            base_dir = Path(__file__).resolve().parent.parent

            def _is_stop():
                return self._recon_stop_flag

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                summary = loop.run_until_complete(
                    cleanup_yahoo_extras(
                        base_dir=base_dir,
                        chrome_path=chrome_path,
                        missing_records=records,
                        headless=True,
                        log=self._log,
                        is_stop=_is_stop,
                        concurrency=3,
                        batch_size=10,
                    )
                )
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

            msg = (
                f"✅ Yahoo 清理完成 — 成功 {summary['accounts_done']} 帳號 / "
                f"失敗 {summary['accounts_failed']} / 共 {summary['total_codes']} 件"
            )
            self._log(f"[Yahoo清理] {msg}")
            self._ui(lambda s=msg: self.var_recon_status.set(s))

        except Exception as e:
            import traceback
            err = f"❌ Yahoo 清理異常:{e}"
            self._log(f"[Yahoo清理] {err}")
            self._log(f"[Yahoo清理] traceback:\n{traceback.format_exc()}")
            self._ui(lambda m=err: self.var_recon_status.set(m))
        finally:
            self._recon_running = False
            self._recon_stop_flag = False

    def _open_backup_folder(self) -> None:
        """打開 runtime/d1_backups/ 給用戶查看。"""
        from pathlib import Path
        import os, subprocess
        base = Path(__file__).resolve().parent.parent
        folder = base / "runtime" / "d1_backups"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.run(["xdg-open", str(folder)], check=False)
        except Exception as e:
            messagebox.showerror("錯誤", f"打開目錄失敗:{e}")

    def _restore_from_backup(self) -> None:
        """從 delete_plan.xlsx 一鍵恢復:列最近 5 個備份給用戶挑,
        或讓用戶自己 browse 任意 delete_plan.xlsx。
        """
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent
        backup_root = base / "runtime" / "d1_backups"

        # 列最近的備份目錄
        if not backup_root.exists():
            messagebox.showinfo(
                "沒有備份",
                "還沒做過 D1 對齊,沒有備份可恢復。\n"
                "(備份位於 runtime/d1_backups/)",
            )
            return

        recent = sorted(
            [d for d in backup_root.iterdir() if d.is_dir() and d.name.startswith("reconcile_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )[:10]

        if not recent:
            messagebox.showinfo("沒有備份", "runtime/d1_backups/ 內沒找到 reconcile_xxx 目錄")
            return

        # 顯示選單給用戶挑(top window)
        sel_win = tk.Toplevel(self.frame.winfo_toplevel())
        sel_win.title("選擇要恢復的備份")
        sel_win.geometry("700x400")
        sel_win.transient(self.frame.winfo_toplevel())
        sel_win.grab_set()

        ttk.Label(sel_win, text="選擇要恢復的備份(會把該次刪掉的 records 加回 D1):",
                  wraplength=680).pack(anchor="w", padx=12, pady=(10, 6))

        listbox = tk.Listbox(sel_win, height=12, font=("Consolas", 10))
        listbox.pack(fill="both", expand=True, padx=12, pady=4)

        # 為每個備份建索引並顯示摘要
        for d in recent:
            try:
                meta_p = d / "metadata.json"
                summary_p = d / "delete_plan_summary.json"
                meta = json.loads(meta_p.read_text("utf-8")) if meta_p.exists() else {}
                summary = json.loads(summary_p.read_text("utf-8")) if summary_p.exists() else {}
                stale_n = summary.get("stale_count", "?")
                full_n = meta.get("full_record_count", "?")
                line = f"{d.name}  |  全量 {full_n} 條  |  待刪/已刪 {stale_n} 條"
            except Exception:
                line = f"{d.name}  |  (無法解讀 metadata)"
            listbox.insert("end", line)

        if recent:
            listbox.selection_set(0)  # 預選最新

        btn_frame = ttk.Frame(sel_win)
        btn_frame.pack(fill="x", padx=12, pady=(6, 12))

        def _do_restore():
            try:
                idx = listbox.curselection()
                if not idx:
                    messagebox.showwarning("提示", "請選擇一個備份", parent=sel_win)
                    return
                chosen = recent[idx[0]]
                plan_xlsx = chosen / "delete_plan.xlsx"
                if not plan_xlsx.exists():
                    messagebox.showerror("錯誤",
                                         f"找不到 delete_plan.xlsx:\n{plan_xlsx}",
                                         parent=sel_win)
                    return

                if not messagebox.askyesno(
                    "確認恢復",
                    f"即將把這次對齊刪掉的 records 加回 D1:\n\n"
                    f"  備份目錄:{chosen.name}\n"
                    f"  恢復檔  :delete_plan.xlsx\n\n"
                    f"執行方式:模擬上方「上傳編碼數據 → 追加更新」流程\n"
                    f"(不會碰非奇摩 account 的記錄,只把刪掉的補回去)\n\n"
                    f"確定恢復嗎?",
                    parent=sel_win,
                ):
                    return

                sel_win.destroy()

                # 從 xlsx 讀回 records,然後跑既有的 upload-batch 流程
                threading.Thread(
                    target=self._restore_worker, args=(plan_xlsx,),
                    daemon=True,
                ).start()
            except Exception as e:
                messagebox.showerror("錯誤", f"恢復失敗:{e}", parent=sel_win)

        ttk.Button(btn_frame, text="恢復", command=_do_restore).pack(side="right")
        ttk.Button(btn_frame, text="取消",
                   command=sel_win.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(btn_frame, text="打開備份目錄",
                   command=self._open_backup_folder).pack(side="left")

    def _restore_worker(self, plan_xlsx) -> None:
        """從 delete_plan.xlsx 讀 records → /api/upload-batch 追加。"""
        owner = (self.var_owner.get() or "").strip()
        if not owner:
            self._ui(lambda: messagebox.showwarning("提示", "未檢測到綁定 TG ID"))
            return

        self._set_busy(True)
        try:
            self._log(f"[D1恢復] 開始讀 {plan_xlsx}")
            wb = openpyxl.load_workbook(plan_xlsx, read_only=True)
            ws = wb.active
            # 解析 header(用既有 _upload_worker 同邏輯,寬鬆匹配欄位名)
            headers = [str(c.value or "").strip().lower()
                       for c in next(ws.iter_rows(min_row=1, max_row=1))]
            col_map = {}
            for i, h in enumerate(headers):
                if h in ("商品條碼", "商品条码", "條碼", "条码", "barcode"):
                    col_map["barcode"] = i
                elif h in ("商品編號", "商品编号", "編號", "编号",
                           "product_code", "商品編碼", "商品编码"):
                    col_map["product_code"] = i
                elif h in ("帳號", "帐号", "賬號", "账号", "account"):
                    col_map["account"] = i

            rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                bc = str(row[col_map["barcode"]] or "").strip() if "barcode" in col_map else ""
                pc = str(row[col_map["product_code"]] or "").strip() if "product_code" in col_map else ""
                acc = str(row[col_map["account"]] or "").strip() if "account" in col_map else ""
                if bc or pc:
                    rows.append({"barcode": bc, "product_code": pc, "account": acc})
            wb.close()

            if not rows:
                self._log("[D1恢復] ❌ xlsx 沒有效資料")
                self._ui(lambda: self.var_recon_status.set("❌ 恢復檔沒有資料"))
                return

            total = len(rows)
            self._log(f"[D1恢復] 解析完成:{total} 條,開始追加 D1...")

            BATCH = 10000
            inserted = 0
            for i in range(0, total, BATCH):
                batch = rows[i:i + BATCH]
                resp = requests.post(
                    f"{DEFAULT_WORKER_URL}/api/upload-batch",
                    json={"token": DEFAULT_UPLOAD_TOKEN,
                          "owner": owner, "rows": batch, "clear": False},
                    timeout=120,
                )
                result = resp.json()
                if not result.get("ok"):
                    err = result.get("error", "未知錯誤")
                    self._log(f"[D1恢復] ❌ 批次 {i // BATCH + 1} 失敗:{err}")
                    self._ui(lambda e=err: self.var_recon_status.set(
                        f"❌ 恢復失敗:{e}(已恢復 {inserted}/{total})"))
                    return
                inserted += result.get("inserted", 0)
                self._log(f"[D1恢復]   進度 {min(i + BATCH, total)}/{total}")

            msg = f"✅ 恢復完成:加回 {inserted} 條 records"
            self._log(f"[D1恢復] {msg}")
            self._ui(lambda: self.var_recon_status.set(msg))
        except Exception as e:
            self._log(f"[D1恢復] ❌ 異常:{e}")
            self._ui(lambda: self.var_recon_status.set(f"❌ 恢復異常:{e}"))
        finally:
            self._set_busy(False)

    def _reconcile_worker(self, owner: str, concurrency: int) -> None:
        """v6.1.25:一次性處理 — 分析 + 寫備份 + 直接刪除,完成後狀態列顯示結果。

        用戶要求:不要確認對話、不要表格彈窗、直接執行。
        備份 / delete_plan.xlsx / missing_plan.xlsx 一律寫好,事後可從「從備份恢復」查看。
        """
        import asyncio
        from pathlib import Path
        from core.d1_reconcile import reconcile_d1_with_yahoo
        from core.accounts import load_accounts

        try:
            base_dir = Path(__file__).resolve().parent.parent
            accounts = load_accounts()

            def _is_stop():
                return self._recon_stop_flag

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    reconcile_d1_with_yahoo(
                        base_dir=base_dir,
                        owner=owner,
                        accounts=accounts,
                        dry_run=False,   # 直接刪除,不彈窗確認
                        log=self._log,
                        is_stop=_is_stop,
                        concurrency=concurrency,
                    )
                )
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

            if self._recon_stop_flag:
                self._ui(lambda: self.var_recon_status.set("⚠️ 已停止"))
                return

            # 簡潔狀態:刪了幾條 + Yahoo 有 D1 沒幾條(需手動補)+ 備份路徑
            backup_dir_name = Path(result.backup_path).parent.name if result.backup_path else "?"
            summary = (
                f"✅ 完成 — 刪除 stale {result.deleted_count}/"
                f"{result.stale_codes_total} 條 | "
                f"Yahoo 有 D1 沒 {result.missing_total} 條(需手動補) | "
                f"備份:{backup_dir_name} | 耗時 {result.elapsed_sec:.1f}s"
            )
            self._log(f"[D1對齊] {summary}")
            self._ui(lambda s=summary: self.var_recon_status.set(s))

            # 刪除後刷新雲端統計(D1 size 變了)
            threading.Thread(target=self._load_stats, daemon=True).start()

        except Exception as e:
            import traceback
            err_msg = f"❌ 對齊異常:{e}"
            self._log(f"[D1對齊] {err_msg}")
            self._log(f"[D1對齊] traceback:\n{traceback.format_exc()}")
            self._ui(lambda m=err_msg: self.var_recon_status.set(m))
        finally:
            self._recon_running = False
            self._recon_stop_flag = False

    # ===== 查询 =====

    def _set_query_text(self, text: str) -> None:
        """设置查询结果纯文本"""
        w = self._query_result_text
        if not w:
            return
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", text)
        w.configure(state="disabled")

    def _do_query(self) -> None:
        code = self.var_query_code.get().strip()
        if not code:
            self._set_query_text("请输入商品编号")
            return
        if self._busy:
            return

        threading.Thread(
            target=self._query_worker, args=(code,),
            daemon=True).start()

    def _query_worker(self, code: str) -> None:
        self._ui(lambda: self._set_query_text("查询中…"))
        try:
            resp = requests.get(
                f"{DEFAULT_WORKER_URL}/api/query",
                params={"code": code}, timeout=15,
            )
            data = resp.json()

            if not data.get("found") or not data.get("data"):
                self._ui(lambda: self._set_query_text(
                    f"查不到商品编号：{code}"))
                return

            rows = data["data"]

            def _fill():
                w = self._query_result_text
                if not w:
                    return
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.insert("end", f"找到 {len(rows)} 笔：\n")
                for r in rows:
                    barcode = r.get("barcode", "")
                    product_code = r.get("product_code", "")
                    account = r.get("account", "")
                    owner = r.get("owner", "")
                    if barcode.startswith("http"):
                        source = "煤炉"
                        link = barcode
                    else:
                        source = "闲鱼"
                        link = f"https://h5.m.goofish.com/item?forceFlush=1&id={barcode}"
                    w.insert("end", f"  [{source}] 链接:")
                    tag_name = f"link_{barcode}"
                    w.tag_configure(tag_name, foreground="blue", underline=True)
                    w.insert("end", link, tag_name)
                    _url = link  # 闭包捕获
                    w.tag_bind(tag_name, "<Button-1>",
                               lambda e, u=_url: webbrowser.open(u))
                    w.tag_bind(tag_name, "<Enter>",
                               lambda e: w.configure(cursor="hand2"))
                    w.tag_bind(tag_name, "<Leave>",
                               lambda e: w.configure(cursor="arrow"))
                    w.insert("end",
                             f"  编号:{product_code}  "
                             f"帐号:{account}  "
                             f"来源:{owner}\n")
                w.configure(state="disabled")

            self._ui(_fill)
        except Exception as e:
            self._ui(lambda: self._set_query_text(f"查询失败：{e}"))

    # ===== 统计 =====

    def _load_stats(self) -> None:
        try:
            owner = self.var_owner.get().strip()
            resp = requests.get(
                f"{DEFAULT_WORKER_URL}/api/stats", timeout=10)
            data = resp.json()
            if data.get("ok"):
                by_owner = data.get("by_owner", [])
                my_count = 0
                for item in by_owner:
                    if item.get("owner", "") == owner:
                        my_count = item.get("count", 0)
                        break
                txt = f"你的云端数据：{my_count} 条" if owner else "未检测到绑定 TG ID"
                self._ui(lambda: self.var_stats.set(txt))
        except Exception:
            pass
