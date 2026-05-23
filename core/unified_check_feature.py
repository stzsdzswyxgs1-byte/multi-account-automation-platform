"""统一检测 tab — 合并闲鱼/煤炉检测 + 自动下架删除 + D1 清理"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

from core.tg_kv_poller import load_relay_config
from core.doc_upload_feature import DEFAULT_WORKER_URL, DEFAULT_UPLOAD_TOKEN

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = (BASE_DIR / "output").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
IDS_DIR = (BASE_DIR / "ids").resolve()
IDS_DIR.mkdir(parents=True, exist_ok=True)


class UnifiedCheckFeatureTab:
    """统一检测 tab：云端/文件 → 自动识别平台 → 检测 → 下架删除 → D1 清理"""

    def __init__(self, *, app: Any, frame: tk.Widget):
        self.app = app
        self.frame = frame

        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None

        # 数据
        self._records: List[Dict[str, str]] = []  # [{barcode, product_code, account}]

        # owner
        _relay = load_relay_config()
        _owner = str(_relay.get("user_id", "") or "").strip()

        # UI vars
        self.var_source = tk.StringVar(value="cloud")  # cloud / file
        self.var_owner = tk.StringVar(value=_owner)
        self.var_file = tk.StringVar(value="")
        self.var_platform_info = tk.StringVar(value="")

        # 平台选择
        self.var_check_gf = tk.BooleanVar(value=True)
        self.var_check_mc = tk.BooleanVar(value=True)

        # 闲鱼参数
        self.var_gf_workers = tk.IntVar(value=5)
        self.var_gf_retries = tk.IntVar(value=3)
        # 煤炉参数
        self.var_mc_concurrent = tk.IntVar(value=8)
        self.var_mc_retries = tk.IntVar(value=3)
        self.var_mc_headless = tk.BooleanVar(value=True)

        self.var_auto_delist = tk.BooleanVar(value=True)
        self.var_auto_d1_cleanup = tk.BooleanVar(value=True)

        self.var_phase = tk.StringVar(value="就绪")
        self.var_gf_progress = tk.StringVar(value="")
        self.var_mc_progress = tk.StringVar(value="")
        self.var_delist_status = tk.StringVar(value="")

        # buttons
        self.btn_start: Optional[ttk.Button] = None
        self.btn_stop: Optional[ttk.Button] = None

    # ---- utils ----
    def log(self, s: str) -> None:
        try:
            self.app.log(f"[CHECK] {s}")
        except Exception:
            print(s)

    def _ui(self, fn):
        try:
            self.app.after(0, fn)
        except Exception:
            fn()

    def _set_phase(self, s: str):
        self._ui(lambda: self.var_phase.set(s))

    def _set_gf_progress(self, s: str):
        self._ui(lambda: self.var_gf_progress.set(s))

    def _set_mc_progress(self, s: str):
        self._ui(lambda: self.var_mc_progress.set(s))

    def _set_delist(self, s: str):
        self._ui(lambda: self.var_delist_status.set(s))

    def _set_btn_state(self, running: bool):
        def _():
            if self.btn_start:
                self.btn_start.configure(state="disabled" if running else "normal")
            if self.btn_stop:
                self.btn_stop.configure(state="normal" if running else "disabled")
        self._ui(_)

    # ---- UI ----
    def build(self) -> None:
        outer = self.frame
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        # 可滾動容器
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        root = ttk.Frame(canvas)

        root.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=root, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        # 鼠標滾輪支持
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 讓內部 frame 寬度跟隨 canvas
        def _on_canvas_resize(event):
            canvas.itemconfig(canvas.find_all()[0], width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        root.columnconfigure(0, weight=1)

        # === 数据来源 ===
        lf_src = ttk.Labelframe(root, text="数据来源")
        lf_src.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        lf_src.columnconfigure(1, weight=1)

        # 云端模式
        r0 = ttk.Frame(lf_src)
        r0.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 3))
        ttk.Radiobutton(r0, text="从云端拉取", variable=self.var_source,
                         value="cloud", command=self._toggle_source).pack(side="left")
        ttk.Button(r0, text="拉取数据", command=self._pull_from_cloud).pack(side="left", padx=(12, 0))
        ttk.Label(r0, textvariable=self.var_platform_info,
                  foreground="#555").pack(side="left", padx=(12, 0))

        # 文件模式
        r1 = ttk.Frame(lf_src)
        r1.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(3, 6))
        ttk.Radiobutton(r1, text="手动选文件", variable=self.var_source,
                         value="file", command=self._toggle_source).pack(side="left")
        self._ent_file = ttk.Entry(r1, textvariable=self.var_file, width=40)
        self._ent_file.pack(side="left", padx=(12, 4), fill="x", expand=True)
        self._btn_browse = ttk.Button(r1, text="浏览", command=self._pick_file)
        self._btn_browse.pack(side="left")

        self._build_params(root)
        self._build_controls(root)
        self._build_status(root)
        self._toggle_source()

    def _build_params(self, root):
        lf = ttk.Labelframe(root, text="参数")
        lf.grid(row=1, column=0, sticky="ew", padx=6, pady=4)

        # 平台勾选
        r_plat = ttk.Frame(lf)
        r_plat.pack(fill="x", padx=6, pady=5)
        ttk.Checkbutton(r_plat, text="闲鱼", variable=self.var_check_gf).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(r_plat, text="煤炉", variable=self.var_check_mc).pack(side="left")

        # 检测后操作
        lf2 = ttk.Labelframe(root, text="检测后操作")
        lf2.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        r = ttk.Frame(lf2)
        r.pack(fill="x", padx=6, pady=6)
        ttk.Checkbutton(r, text="自动下架删除", variable=self.var_auto_delist).pack(
            side="left", padx=(0, 16))
        ttk.Checkbutton(r, text="自动清理云端D1记录", variable=self.var_auto_d1_cleanup).pack(
            side="left")

        # ── 定期检测计划 ──
        lf3 = ttk.Labelframe(root, text="定期检测计划")
        lf3.grid(row=3, column=0, sticky="ew", padx=6, pady=4)

        sched_top = ttk.Frame(lf3)
        sched_top.pack(fill="x", padx=6, pady=(6, 3))

        self.var_sched_enabled = tk.BooleanVar(value=bool(self.app.settings.get("check_schedule_enabled", False)))
        ttk.Checkbutton(sched_top, text="启用定期检测", variable=self.var_sched_enabled,
                        command=self._on_sched_toggle).pack(side="left")

        ttk.Label(sched_top, text="  时间:").pack(side="left", padx=(16, 2))
        self.var_sched_time = tk.StringVar(value="04:00")
        ttk.Entry(sched_top, textvariable=self.var_sched_time, width=8).pack(side="left")

        ttk.Label(sched_top, text="  平台:").pack(side="left", padx=(16, 2))
        self.var_sched_gf = tk.BooleanVar(value=True)
        self.var_sched_mc = tk.BooleanVar(value=True)
        ttk.Checkbutton(sched_top, text="闲鱼", variable=self.var_sched_gf).pack(side="left")
        ttk.Checkbutton(sched_top, text="煤炉", variable=self.var_sched_mc).pack(side="left")

        sched_btn = ttk.Frame(lf3)
        sched_btn.pack(fill="x", padx=6, pady=(3, 3))
        ttk.Button(sched_btn, text="添加计划", command=self._sched_add).pack(side="left", padx=(0, 8))
        ttk.Button(sched_btn, text="删除选中", command=self._sched_del).pack(side="left", padx=(0, 8))
        ttk.Button(sched_btn, text="清空所有", command=self._sched_clear).pack(side="left")

        # 计划列表
        cols = ("time", "platforms")
        self.sched_tree = ttk.Treeview(lf3, columns=cols, show="headings", height=3)
        self.sched_tree.heading("time", text="时间")
        self.sched_tree.heading("platforms", text="平台")
        self.sched_tree.column("time", width=80, anchor="center")
        self.sched_tree.column("platforms", width=150, anchor="center")
        self.sched_tree.pack(fill="x", padx=6, pady=(0, 6))

        self._load_check_schedule()
        self._start_schedule_loop()

    def _build_controls(self, root):
        lf = ttk.Frame(root)
        lf.grid(row=4, column=0, sticky="ew", padx=6, pady=4)
        lf.columnconfigure((0, 1, 2, 3), weight=1)

        self.btn_start = ttk.Button(lf, text="开始全流程", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        self.btn_stop = ttk.Button(lf, text="停止", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        ttk.Button(lf, text="打开输出", command=self._open_output).grid(
            row=0, column=2, sticky="ew", padx=5, pady=5)
        ttk.Button(lf, text="重检未知", command=self._recheck_unknown).grid(
            row=0, column=3, sticky="ew", padx=5, pady=5)

        # 闲鱼检测改用「手机辅助签名」模式（无 cookie，无浏览器登录）
        # 原 row=5 的「闲鱼检测专用登录 / 清除检测cookie缓存」两按钮已移除
        # 状态由手机模块自动处理，无需任何用户操作
        # 提示行：告诉用户手机连接状态由 settings.json 配
        lf2 = ttk.Frame(root)
        lf2.grid(row=5, column=0, sticky="ew", padx=6, pady=(2, 4))
        lf2.columnconfigure(0, weight=1)
        ttk.Label(lf2,
                  text="闲鱼检测：手机辅助签名模式（局域网手机自动签名，无需扫码登录）",
                  foreground="#666").grid(row=0, column=0, sticky="w", padx=5, pady=4)

    def _open_check_login(self):
        """打开闲鱼检测专用浏览器（Playwright + JSON 持久化方案）。

        使用 Playwright launch_persistent_context 启动 Chrome，登录成功后
        主动调用 browser.cookies() 导出全部 cookie 到 goofish_cookies.json
        （含 Chrome session restore 不会保存的 session cookie）。
        关浏览器再保存一次兜底。检测时直接读 JSON，不再依赖 Chrome SQLite。
        """
        try:
            from .goofish_login_playwright import (
                launch_login_browser_threaded, CHECK_PROFILE_DIR,
            )
            from .profile_lock import detect_chrome_profile_in_use
            in_use, detail = detect_chrome_profile_in_use(CHECK_PROFILE_DIR)
            if in_use:
                self.log(f"[CHECK] ⚠ 检测专用浏览器已经在运行，请先关闭再试 ({detail})")
                return

            self.log("[CHECK] ============ 操作步骤 ============")
            self.log("[CHECK] 1) 用闲鱼 APP 扫码登录")
            self.log("[CHECK] 2) 等首页显示「我的闲鱼」表示登录成功")
            self.log("[CHECK] 3) 浏览 2-3 个商品页面（让 cookie 更完整）")
            self.log("[CHECK] 4) 手动点 X 关闭浏览器（关闭时自动保存 cookie）")
            self.log("[CHECK] ==================================")
            launch_login_browser_threaded(self.log)
        except Exception as e:
            self.log(f"[CHECK] 打开登录浏览器失败: {e}")

    def _clear_check_cookie(self):
        """清除检测专用 cookie：删 goofish_cookies.json + 整个 check_goofish profile 目录。

        新方案下检测只读 JSON，所以清 JSON + Playwright profile 目录就够了。
        不影响采购出货 profile（purchase_monitor）。
        """
        from tkinter import messagebox
        from .goofish_login_playwright import (
            CHECK_PROFILE_DIR, COOKIE_JSON_PATH, clear_all,
        )

        if not CHECK_PROFILE_DIR.exists() and not COOKIE_JSON_PATH.exists():
            self.log("[CHECK] check_goofish 数据不存在，无需清除")
            return

        # 检测浏览器是否还在跑
        try:
            from .profile_lock import detect_chrome_profile_in_use
            in_use, detail = detect_chrome_profile_in_use(CHECK_PROFILE_DIR)
            if in_use:
                self.log(f"[CHECK] ⚠ 检测专用浏览器正在运行 ({detail})")
                self.log(f"[CHECK] ⚠ 请先关闭浏览器再点清除")
                return
        except Exception:
            pass

        if not messagebox.askyesno(
            "确认清除",
            "将彻底清除检测专用 cookie 数据（不影响采购出货）：\n\n"
            "• goofish_cookies.json（已保存的登录 cookie）\n"
            "• check_goofish/ profile 目录（含浏览历史 / 反爬指纹）\n\n"
            "清除后下次需要重新扫码登录闲鱼。\n\n确认清除？"
        ):
            return

        clear_all(self.log)
        self.log("[CHECK] ✓ 下次点「闲鱼检测专用登录」可以重新扫码登录干净 session")

    def _build_status(self, root):
        lf = ttk.Labelframe(root, text="状态")
        lf.grid(row=6, column=0, sticky="ew", padx=6, pady=(4, 6))
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="阶段：").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Label(lf, textvariable=self.var_phase).grid(row=0, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(lf, text="闲鱼：").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Label(lf, textvariable=self.var_gf_progress).grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(lf, text="煤炉：").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Label(lf, textvariable=self.var_mc_progress).grid(row=2, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(lf, text="下架：").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        ttk.Label(lf, textvariable=self.var_delist_status).grid(row=3, column=1, sticky="w", padx=5, pady=(3, 5))

    # ---- source toggle / file pick / open output ----
    # ── 定期检测计划 ──
    def _load_check_schedule(self):
        """从 settings.json 加载定期检测计划"""
        schedules = self.app.settings.get("check_schedule", [])
        for item in self.sched_tree.get_children():
            self.sched_tree.delete(item)
        for sch in schedules:
            t = sch.get("time", "")
            platforms = sch.get("platforms", "")
            label = []
            if "goofish" in platforms:
                label.append("闲鱼")
            if "mercari" in platforms:
                label.append("煤炉")
            self.sched_tree.insert("", "end", values=(t, "+".join(label) or "无"))

    def _save_check_schedule(self):
        """保存定期检测计划到 settings.json"""
        schedules = []
        for item in self.sched_tree.get_children():
            vals = self.sched_tree.item(item, "values")
            platforms = []
            plat_str = vals[1] if len(vals) > 1 else ""
            if "闲鱼" in plat_str:
                platforms.append("goofish")
            if "煤炉" in plat_str:
                platforms.append("mercari")
            schedules.append({"time": vals[0], "platforms": ",".join(platforms)})
        self.app.settings["check_schedule"] = schedules
        self.app.settings["check_schedule_enabled"] = self.var_sched_enabled.get()
        from core.accounts import save_settings
        save_settings(self.app.settings)

    def _sched_add(self):
        t = self.var_sched_time.get().strip().replace("：", ":")  # 支持全角冒号
        if not t or ":" not in t:
            from tkinter import messagebox
            messagebox.showwarning("提示", "请输入正确的时间格式（HH:MM）", parent=self.frame)
            return
        platforms = []
        label = []
        if self.var_sched_gf.get():
            platforms.append("goofish")
            label.append("闲鱼")
        if self.var_sched_mc.get():
            platforms.append("mercari")
            label.append("煤炉")
        if not platforms:
            from tkinter import messagebox
            messagebox.showwarning("提示", "请至少选择一个平台", parent=self.frame)
            return
        self.sched_tree.insert("", "end", values=(t, "+".join(label)))
        self._save_check_schedule()
        self.log(f"[CHECK] 已添加定期检测计划：{t} {'+'.join(label)}")

    def _sched_del(self):
        sel = self.sched_tree.selection()
        if sel:
            for s in sel:
                self.sched_tree.delete(s)
            self._save_check_schedule()

    def _sched_clear(self):
        for item in self.sched_tree.get_children():
            self.sched_tree.delete(item)
        self._save_check_schedule()

    def _on_sched_toggle(self):
        self._save_check_schedule()
        if self.var_sched_enabled.get():
            self.log("[CHECK] 定期检测已启用")
        else:
            self.log("[CHECK] 定期检测已停用")

    def _start_schedule_loop(self):
        """启动定期检测后台线程"""
        self._sched_fired = set()
        t = threading.Thread(target=self._schedule_loop, daemon=True)
        t.start()

    def _schedule_loop(self):
        """后台轮询，每 30 秒检查一次是否到了定期检测时间"""
        import time as _time
        while True:
            _time.sleep(30)
            if not self.var_sched_enabled.get():
                continue
            if self._stop_evt.is_set():
                continue

            now = _time.strftime("%H:%M")
            today = _time.strftime("%Y-%m-%d")
            schedules = self.app.settings.get("check_schedule", [])

            for sch in schedules:
                t = sch.get("time", "")
                if t != now:
                    continue
                fire_key = f"{today}_{t}_{sch.get('platforms', '')}"
                if fire_key in self._sched_fired:
                    continue
                self._sched_fired.add(fire_key)

                # 触发定期检测
                platforms = sch.get("platforms", "")
                self.log(f"[SCHEDULE] 开始定期检测：{t} 平台={platforms}")
                try:
                    self._execute_scheduled_check(platforms)
                except Exception as e:
                    self.log(f"[SCHEDULE] 定期检测异常：{e}")

    def _execute_scheduled_check(self, platforms: str):
        """执行定期检测（从云端拉取数据 → 检测 → 下架/D1清理）"""
        import time as _time

        # 设置平台
        self._ui(lambda: self.var_check_gf.set("goofish" in platforms))
        self._ui(lambda: self.var_check_mc.set("mercari" in platforms))
        self._ui(lambda: self.var_source.set("cloud"))

        # 直接在当前线程拉取数据（不走 _pull_from_cloud 的异步线程）
        owner = self.var_owner.get().strip()
        if not owner:
            self.log("[SCHEDULE] 未配置 TG ID，无法拉取云端数据")
            return

        self.log(f"[SCHEDULE] 正在拉取云端数据（owner={owner}）...")
        try:
            self._pull_worker(owner)
        except Exception as e:
            self.log(f"[SCHEDULE] 数据拉取失败：{e}")
            return

        if self._records:
            self.log(f"[SCHEDULE] 数据拉取完成：{len(self._records)} 条，开始检测...")
            self._ui(lambda: self.start())
        else:
            self.log("[SCHEDULE] 数据为空，跳过本次检测")

    def _toggle_source(self):
        is_cloud = self.var_source.get() == "cloud"
        st_file = "disabled" if is_cloud else "normal"
        self._ent_file.configure(state=st_file)
        self._btn_browse.configure(state=st_file)

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="选择商品ID/URL清单",
            filetypes=[("Text", "*.txt;*.csv"), ("All", "*.*")],
        )
        if p:
            self.var_file.set(p)

    def _open_output(self):
        try:
            os.startfile(str(OUTPUT_DIR))
        except Exception as e:
            messagebox.showinfo("提示", f"打开失败：{e}")

    # ---- cloud pull ----
    def _pull_from_cloud(self):
        owner = self.var_owner.get().strip()
        if not owner:
            messagebox.showwarning("提示", "未检测到绑定 TG ID，请先配置 KV 中转")
            return
        self.var_platform_info.set("拉取中…")
        threading.Thread(target=self._pull_worker, args=(owner,), daemon=True).start()

    def _pull_worker(self, owner: str):
        try:
            records: List[Dict[str, str]] = []
            for dtype in ("goofish", "mercari"):
                offset = 0
                while True:
                    resp = requests.get(
                        f"{DEFAULT_WORKER_URL}/api/barcodes",
                        params={"owner": owner, "type": dtype, "limit": 5000, "offset": offset},
                        timeout=30,
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        break
                    rows = data.get("data", [])
                    for r in rows:
                        records.append({
                            "barcode": (r.get("barcode") or "").strip(),
                            "product_code": (r.get("product_code") or "").strip(),
                            "account": (r.get("account") or "").strip(),
                        })
                    if len(rows) < 5000:
                        break
                    offset += len(rows)

            self._records = records
            gf = sum(1 for r in records if not r["barcode"].startswith("http"))
            mc = sum(1 for r in records if r["barcode"].startswith("http"))
            info = f"闲鱼: {gf} 条  煤炉: {mc} 条  共 {len(records)} 条"
            self._ui(lambda: self.var_platform_info.set(info))
            self.log(f"云端拉取完成：{info}")
        except Exception as e:
            self._ui(lambda: self.var_platform_info.set(f"拉取失败：{e}"))
            self.log(f"云端拉取失败：{e}")

    # ---- start / stop ----
    def start(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在运行中…")
            return

        # 加载数据
        if self.var_source.get() == "cloud":
            if not self._records:
                messagebox.showwarning("提示", "请先点击「拉取数据」")
                return
            records = list(self._records)
        else:
            records = self._load_file_records()
            if records is None:
                return

        if not records:
            messagebox.showwarning("提示", "没有可检测的数据")
            return

        if not self.var_check_gf.get() and not self.var_check_mc.get():
            messagebox.showwarning("提示", "请至少选择一个检测平台")
            return

        self._stop_evt.clear()
        self._set_btn_state(True)
        self._set_phase("准备中")
        self._set_gf_progress("")
        self._set_mc_progress("")
        self._set_delist("")

        self._worker = threading.Thread(
            target=self._pipeline_worker, args=(records,), daemon=True)
        self._worker.start()

    def stop(self):
        self._stop_evt.set()
        self._set_phase("正在停止…")

    # ---- file loading ----
    def _load_file_records(self) -> Optional[List[Dict[str, str]]]:
        """从本地 txt 文件加载 barcode 列表，尝试从 D1 查询 account/product_code。"""
        fpath = self.var_file.get().strip()
        if not fpath or not os.path.exists(fpath):
            messagebox.showerror("错误", "找不到输入文件")
            return None

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if not lines:
            messagebox.showwarning("提示", "文件为空")
            return None

        # 尝试加载 sidecar mapping.json
        mapping_path = Path(fpath).with_suffix(".mapping.json")
        mapping: Optional[Dict] = None
        if mapping_path.exists():
            try:
                with open(mapping_path, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
            except Exception:
                pass

        records: List[Dict[str, str]] = []
        for line in lines:
            bc = line.strip()
            if not bc:
                continue
            rec = {"barcode": bc, "product_code": "", "account": ""}
            if mapping and bc in mapping:
                entries = mapping[bc]
                if entries:
                    rec["product_code"] = entries[0].get("product_code", "")
                    rec["account"] = entries[0].get("account", "")
            records.append(rec)

        self.log(f"文件加载：{len(records)} 条记录")
        return records

    # ---- main pipeline ----
    def _pipeline_worker(self, records: List[Dict[str, str]]):
        """全流程：分组 → 检测 → 收集非在售 → 下架删除 → D1 清理"""
        try:
            self._pipeline_inner(records)
        except Exception as e:
            self.log(f"流程异常：{e}")
            self._set_phase(f"异常：{e}")
        finally:
            self._set_btn_state(False)

    def _pipeline_inner(self, records: List[Dict[str, str]]):
        if self._stop_evt.is_set():
            return

        # ---- Phase 1: 按平台分组（根据用户选择过滤） ----
        self._set_phase("分组中")
        check_gf = self.var_check_gf.get()
        check_mc = self.var_check_mc.get()
        goofish_recs = []  # barcode 为纯数字
        mercari_recs = []  # barcode 以 http 开头

        # 诊断：打印不同位置的 barcode 样本 + 统计
        if records:
            n = len(records)
            http_count = sum(1 for r in records if (r.get("barcode") or "").strip().startswith("http"))
            self.log(f"[诊断] 总记录={n}, 以http开头={http_count}, 非http={n - http_count}")
            # 打印首尾和中间的样本
            sample_idxs = [0, 1, n // 2, n - 2, n - 1] if n >= 5 else list(range(n))
            for idx in sample_idxs:
                bc = repr((records[idx].get("barcode") or "")[:80])
                self.log(f"[诊断] records[{idx}] barcode={bc}")

        for r in records:
            bc = (r.get("barcode") or "").strip()
            if bc.startswith("http"):
                if check_mc:
                    mercari_recs.append(r)
            elif bc:
                if check_gf:
                    goofish_recs.append(r)

        self.log(f"分组完成：闲鱼 {len(goofish_recs)} 条，煤炉 {len(mercari_recs)} 条")
        total = len(goofish_recs) + len(mercari_recs)
        if total == 0:
            self._set_phase("无数据")
            return

        # 收集所有非在售的 barcode（用于 D1 清理）
        non_active_barcodes: List[str] = []
        # 收集按 account 分组的 product_code（用于下架删除）
        account_codes: Dict[str, set] = {}

        # v6.0.63: 闲鱼检测完整性 flag(true=不完整,跳过下架+D1)
        self._gf_check_incomplete = False

        # ---- Phase 2: 并行检测闲鱼 + 煤炉 ----
        threads: List[threading.Thread] = []

        if goofish_recs and not self._stop_evt.is_set():
            gf_non_active: List[str] = []
            gf_account_codes: Dict[str, set] = {}
            def _gf():
                self._run_goofish_check(goofish_recs, gf_non_active, gf_account_codes)
            t = threading.Thread(target=_gf, daemon=True)
            threads.append(t)

        if mercari_recs and not self._stop_evt.is_set():
            mc_non_active: List[str] = []
            mc_account_codes: Dict[str, set] = {}
            def _mc():
                self._run_mercari_check(mercari_recs, mc_non_active, mc_account_codes)
            t = threading.Thread(target=_mc, daemon=True)
            threads.append(t)

        if threads:
            self._set_phase("检测中")
            for t in threads:
                t.start()
            # 用短超時輪詢，讓停止信號能及時響應
            for t in threads:
                while t.is_alive():
                    t.join(timeout=1)
                    if self._stop_evt.is_set():
                        break

        # 合并结果
        if goofish_recs:
            non_active_barcodes.extend(gf_non_active)
            for acc, codes in gf_account_codes.items():
                account_codes.setdefault(acc, set()).update(codes)
        if mercari_recs:
            non_active_barcodes.extend(mc_non_active)
            for acc, codes in mc_account_codes.items():
                account_codes.setdefault(acc, set()).update(codes)

        if self._stop_evt.is_set():
            self._set_phase("已停止")
            return

        # ---- Phase 4: 写入 ids/{account}.txt ----
        self._write_ids_files(account_codes)

        # v6.0.63: 闲鱼检测不完整时跳过下架 + D1 清理(对称处理,防止把还在售但漏检的商品误下架)
        # 与 goofish_check_feature.py:603 跳过 D1 清理逻辑对称
        gf_incomplete = bool(getattr(self, '_gf_check_incomplete', False))

        # ---- Phase 5: 自动下架删除 ----
        if self.var_auto_delist.get() and account_codes:
            if gf_incomplete:
                self.log("⚠ 闲鱼检测不完整,跳过自动下架(下次检测会重新发现非在售商品)")
            else:
                self._run_delist(account_codes)

        # ---- Phase 6: D1 清理 ----
        if self.var_auto_d1_cleanup.get() and non_active_barcodes:
            if gf_incomplete:
                self.log("⚠ 闲鱼检测不完整,跳过 D1 清理(防止误删在售商品)")
            else:
                self._run_d1_cleanup(non_active_barcodes)

        if not self._stop_evt.is_set():
            self._set_phase("全部完成")
            self.log("全流程完成")

    # ---- Phase 2: 闲鱼检测 ----
    def _run_goofish_check(self, recs: List[Dict[str, str]],
                           non_active: List[str], account_codes: Dict[str, set]):
        """委托 GoofishCheckFeatureTab 引擎检测闲鱼商品状态。"""
        from core.goofish_check_feature import GoofishCheckFeatureTab, extract_item_id

        total = len(recs)
        self._set_gf_progress(f"0/{total}")

        # 准备临时输入文件 + mapping
        owner = self.var_owner.get().strip() or "unified"
        tmp_txt = OUTPUT_DIR / f"cloud_goofish_{owner}.txt"
        tmp_mapping: Dict[str, List[Dict[str, str]]] = {}

        with open(tmp_txt, "w", encoding="utf-8") as f:
            for r in recs:
                item_id = extract_item_id(r["barcode"])
                if not item_id:
                    continue
                f.write(item_id + "\n")
                # mapping: item_id → [{account, product_code}]
                if r.get("account") or r.get("product_code"):
                    tmp_mapping.setdefault(item_id, []).append({
                        "account": r.get("account", ""),
                        "product_code": r.get("product_code", ""),
                    })

        # 写 mapping.json
        mapping_path = tmp_txt.with_suffix(".mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(tmp_mapping, ensure_ascii=False, fp=f)

        # 创建一个隐藏的 tab 实例来委托检测
        dummy_frame = ttk.Frame(self.frame)
        tab = GoofishCheckFeatureTab(app=self.app, frame=dummy_frame)
        tab.skip_post_actions = True  # 由 unified 外层统一处理拆分/D1清理

        # 配置参数（闲鱼参数由服务器端控制，这里固定值）
        tab.var_input.set(str(tmp_txt))
        tab.var_out.set(str(OUTPUT_DIR / f"goofish_checked_{owner}.txt"))
        tab.var_out_fail.set(str(OUTPUT_DIR / f"goofish_fail_{owner}.txt"))
        # 手机辅助签名模式：acs.m 无 BX，瓶颈在手机 /sign，开高并发不会触发限流
        tab.var_max_workers.set(32)
        tab.var_interval_ms.set(0)
        tab.var_max_attempts.set(3)
        tab._stop_evt = self._stop_evt  # 共享停止信号

        # 进度回调
        orig_set_counts = tab._set_counts
        def _on_counts(done, tot):
            orig_set_counts(done, tot)
            self._set_gf_progress(f"{done}/{tot}")
        tab._set_counts = _on_counts

        # 同步运行检测
        out_path = tab.var_out.get()
        out_fail = tab.var_out_fail.get()
        tab._run_check(str(tmp_txt), out_path, out_fail)

        # v6.0.63: 记录闲鱼检测是否不完整(api_fail > 0),用于跳过下架/D1 清理
        # v6.0.71: 加容忍閾值 — 少量失敗(<=10 條 OR <0.1%)不算「不完整」,繼續下架/D1
        # (使用者反映 1/111902 失敗就跳過全部太武斷)
        _fails = int(getattr(tab, '_last_run_failed', 0))
        _total = int(getattr(tab, '_last_run_total', 0))
        TOLERANCE_ABS = 10
        TOLERANCE_PCT = 0.001
        if _fails <= 0:
            self._gf_check_incomplete = False
        elif _fails <= TOLERANCE_ABS:
            self._gf_check_incomplete = False
            self.log(f"⚠ 闲鱼检测有 {_fails} 条失败 / 总 {_total},容忍范围内(<=10),继续下架/D1 清理")
        elif _total > 0 and (_fails / _total) <= TOLERANCE_PCT:
            self._gf_check_incomplete = False
            self.log(f"⚠ 闲鱼检测有 {_fails} 条失败({_fails*100/_total:.4f}%),容忍范围内(<0.1%),继续下架/D1 清理")
        else:
            self._gf_check_incomplete = True
            _rate = (_fails*100/_total) if _total > 0 else 0
            self.log(f"⚠ 闲鱼检测有 {_fails} 条失败({_rate:.2f}%),超过容忍阈值,跳过自动下架和 D1 清理(防止误处理)")

        # 收集结果：读取 Excel
        self._collect_goofish_results(
            tmp_txt, recs, non_active, account_codes, tmp_mapping)

        dummy_frame.destroy()

    def _collect_goofish_results(self, tmp_txt, recs, non_active, account_codes, mapping):
        """从闲鱼检测 Excel 结果中收集非在售商品。"""
        from core.goofish_check_feature import extract_item_id
        excel_path = Path(tmp_txt).with_suffix(".xlsx")
        if not excel_path.exists():
            return

        import openpyxl
        wb = openpyxl.load_workbook(str(excel_path), read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        if len(rows) < 2:
            return

        headers = [str(h or "") for h in rows[0]]
        id_idx = headers.index("item_id") if "item_id" in headers else 0
        st_idx = headers.index("status") if "status" in headers else 1

        # barcode → rec 的快速查找
        bc_map = {}
        for r in recs:
            iid = extract_item_id(r["barcode"])
            if iid:
                bc_map[iid] = r

        for row in rows[1:]:
            item_id = str(row[id_idx] or "").strip()
            status = str(row[st_idx] or "").strip()
            if not item_id:
                continue

            # 非在售状态
            if status in ("卖掉了", "已下架", "已删除"):
                rec = bc_map.get(item_id)
                if rec:
                    non_active.append(rec["barcode"])
                    acc = rec.get("account", "").strip()
                    pc = rec.get("product_code", "").strip()
                    if acc and pc:
                        account_codes.setdefault(acc, set()).add(pc)
                # 也检查 mapping 中的多账号
                entries = mapping.get(item_id, [])
                for entry in entries:
                    acc = entry.get("account", "").strip()
                    pc = entry.get("product_code", "").strip()
                    if acc and pc:
                        account_codes.setdefault(acc, set()).add(pc)

    # ---- Phase 3: 煤炉检测 ----
    def _run_mercari_check(self, recs: List[Dict[str, str]],
                           non_active: List[str],
                           account_codes: Dict[str, set]):
        """委托 MercariCheckFeatureTab 引擎检测煤炉商品状态。"""
        import asyncio
        from core.mercari_check_feature import (
            MercariCheckFeatureTab, _MercariConfig,
        )

        total = len(recs)
        self._set_mc_progress(f"0/{total}")

        owner = self.var_owner.get().strip() or "unified"
        tmp_txt = OUTPUT_DIR / f"cloud_mercari_{owner}.txt"
        tmp_mapping: Dict[str, List[Dict[str, str]]] = {}

        with open(tmp_txt, "w", encoding="utf-8") as f:
            for r in recs:
                url = r["barcode"]
                f.write(url + "\n")
                if r.get("account") or r.get("product_code"):
                    tmp_mapping.setdefault(url, []).append({
                        "account": r.get("account", ""),
                        "product_code": r.get("product_code", ""),
                    })

        mapping_path = tmp_txt.with_suffix(".mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(tmp_mapping, ensure_ascii=False, fp=f)

        # 创建隐藏 tab 实例委托检测
        dummy_frame = ttk.Frame(self.frame)
        tab = MercariCheckFeatureTab(app=self.app, frame=dummy_frame)
        tab.skip_post_actions = True  # 由 unified 外层统一处理拆分/D1清理

        tab.var_input.set(str(tmp_txt))
        out_xlsx = str(OUTPUT_DIR / f"mercari_checked_{owner}.xlsx")
        tab.var_out.set(out_xlsx)
        tab.var_max_concurrent.set(self.var_mc_concurrent.get())
        tab.var_max_retries.set(self.var_mc_retries.get())
        tab.var_headless.set(self.var_mc_headless.get())
        tab._stop_evt = self._stop_evt

        orig_set_counts = tab._set_counts
        def _on_counts(done, tot):
            orig_set_counts(done, tot)
            self._set_mc_progress(f"{done}/{tot}")
        tab._set_counts = _on_counts

        cfg = _MercariConfig(
            max_concurrent=self.var_mc_concurrent.get(),
            max_retries=3,
            headless=self.var_mc_headless.get(),
        )
        try:
            asyncio.run(tab._run_check(str(tmp_txt), out_xlsx, cfg))
        except Exception as e:
            self.log(f"煤炉检测异常：{e}")

        # 收集结果
        self._collect_mercari_results(
            out_xlsx, recs, non_active, account_codes, tmp_mapping)

        dummy_frame.destroy()

    def _collect_mercari_results(self, excel_path, recs, non_active,
                                 account_codes, mapping):
        """从煤炉检测 Excel 结果中收集非在售商品。"""
        if not os.path.exists(excel_path):
            return

        import openpyxl
        wb = openpyxl.load_workbook(excel_path, read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        if len(rows) < 2:
            return

        headers = [str(h or "") for h in rows[0]]
        url_idx = headers.index("url") if "url" in headers else 0
        st_idx = headers.index("status") if "status" in headers else 1

        url_map = {r["barcode"]: r for r in recs}
        non_active_statuses = (
            # API 模式状态
            "卖掉了", "已删除", "交易中", "未知",
            # 旧 Playwright 模式状态（兼容）
            "已售完", "刪除或不存在", "売り切れ", "削除済み",
            "拍賣中（入札受付中）", "拍賣中（入札+可直接購買）",
        )

        for row in rows[1:]:
            url = str(row[url_idx] or "").strip()
            status = str(row[st_idx] or "").strip()
            if not url:
                continue
            if status in non_active_statuses:
                rec = url_map.get(url)
                if rec:
                    non_active.append(rec["barcode"])
                entries = mapping.get(url, [])
                for entry in entries:
                    acc = entry.get("account", "").strip()
                    pc = entry.get("product_code", "").strip()
                    if acc and pc:
                        account_codes.setdefault(acc, set()).add(pc)

    # ---- Phase 4: 写入 ids 文件 ----
    def _write_ids_files(self, account_codes: Dict[str, set]):
        """按 account 写入 ids/{account}.txt"""
        if not account_codes:
            return
        self._set_phase("写入下架列表")
        for acc, codes in account_codes.items():
            out_file = IDS_DIR / f"{acc}.txt"
            with open(out_file, "w", encoding="utf-8") as f:
                for code in sorted(codes):
                    f.write(code + "\n")
        self.log(f"已写入 {len(account_codes)} 个账号的下架列表到 ids/ 目录")

    # ---- Phase 5: 自动下架删除 ----
    def _run_delist(self, account_codes: Dict[str, set]):
        """对每个 account 调用 HTTP 纯接口下架删除（并行处理多账号）。"""
        import asyncio
        import concurrent.futures
        from core.merch_id_ops import load_merch_id_batches
        from core.merch_http_ops import HttpBatchConfig, run_http_merch_id_ops
        from core.accounts import load_accounts, load_settings

        self._set_phase("下架删除中")
        accounts = load_accounts()
        settings = load_settings()
        chrome = settings.get("browser_path", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        headless = self.var_mc_headless.get()
        max_workers = max(1, self.var_mc_concurrent.get())

        acc_map = {}
        for a in accounts:
            acc_map[a.get("name", "")] = a

        # 预处理：准备每个账号的任务参数
        tasks = []
        for acc_name, codes in account_codes.items():
            a = acc_map.get(acc_name)
            if not a:
                self.log(f"[下架] 账号 {acc_name} 未在系统中找到，跳过")
                continue
            pid = a.get("profile_id", "")
            if not pid:
                self.log(f"[下架] 账号 {acc_name} 无 profile_id，跳过")
                continue
            try:
                batches, ids_path = load_merch_id_batches(
                    base_dir=BASE_DIR, account_name=acc_name,
                    profile_id=pid, batch_size=10, log=self.log,
                )
            except Exception as e:
                self.log(f"[下架] {acc_name} 加载批次失败：{e}")
                continue
            if not batches:
                continue
            tasks.append((acc_name, pid, batches, a.get("proxy", "") or "", ids_path))

        total_accs = len(tasks)
        if not tasks:
            self.log("无需下架的账号")
            return

        done_count = [0]

        def _do_one(acc_name, pid, batches, proxy, ids_path):
            if self._stop_evt.is_set():
                return
            self.log(f"[下架] 开始: {acc_name} ({len(batches)} 批)")
            profile_dir = BASE_DIR / "profiles" / pid
            profile_dir.mkdir(parents=True, exist_ok=True)
            cfg = HttpBatchConfig(mode="根據商品編號下架刪除",
                                  interval_sec=0.0, headless=headless, batch_size=10)
            status = "error"
            try:
                loop = asyncio.new_event_loop()
                status = loop.run_until_complete(run_http_merch_id_ops(
                    base_dir=BASE_DIR, profile_dir=profile_dir,
                    chrome_path=chrome, account_name=acc_name,
                    profile_id=pid, batches=batches, cfg=cfg,
                    proxy=proxy, log=self.log,
                    is_stop=lambda: self._stop_evt.is_set(),
                    is_pause=lambda: False,
                )) or "error"
                loop.close()
            except Exception as e:
                self.log(f"[下架] {acc_name} 执行失败：{e}")
            # 跑完且全部批次走完 → 清空 ids 档,避免下次手动重跑挑到已删商品再失败
            if status == "done" and ids_path:
                try:
                    Path(ids_path).write_text("", encoding="utf-8")
                    self.log(f"[下架] {acc_name} 已清空 ids 文件 ({Path(ids_path).name})")
                except Exception as e:
                    self.log(f"[下架] {acc_name} 清空 ids 文件失败：{e}")
            done_count[0] += 1
            self._set_delist(f"进行中 {done_count[0]}/{total_accs}")

        self.log(f"[下架] 共 {total_accs} 个账号，并发={max_workers}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_do_one, *t) for t in tasks]
            concurrent.futures.wait(futs)

        self._set_delist(f"完成 {done_count[0]}/{total_accs}")
        self.log(f"下架删除完成：{done_count[0]}/{total_accs} 个账号")

    # ---- Phase 6: D1 清理 ----
    def _run_d1_cleanup(self, barcodes: List[str]):
        """POST /api/delete-barcodes 删除非在售记录。"""
        self._set_phase("清理D1")
        total = len(barcodes)
        self.log(f"开始清理 D1：{total} 条非在售记录")

        try:
            BATCH = 200
            deleted = 0
            for i in range(0, total, BATCH):
                if self._stop_evt.is_set():
                    break
                chunk = barcodes[i:i + BATCH]
                resp = requests.post(
                    f"{DEFAULT_WORKER_URL}/api/delete-barcodes",
                    json={"token": DEFAULT_UPLOAD_TOKEN, "barcodes": chunk},
                    timeout=30,
                )
                data = resp.json()
                if data.get("ok"):
                    deleted += data.get("deleted", 0)
                else:
                    self.log(f"D1 清理批次失败：{data.get('error', '?')}")
                self._set_phase(f"清理D1 {min(i + BATCH, total)}/{total}")

            self.log(f"D1 清理完成：删除 {deleted} 条记录")
        except Exception as e:
            self.log(f"D1 清理异常：{e}")

    # ---- 重检未知 ----
    def _recheck_unknown(self):
        """从上次检测结果中提取未知/失败项，重新检测。"""
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在运行中，请先停止")
            return

        owner = self.var_owner.get().strip() or "unified"

        # 查找闲鱼和煤炉的检测结果 Excel
        gf_xlsx = OUTPUT_DIR / f"cloud_goofish_{owner}.xlsx"
        mc_xlsx = OUTPUT_DIR / f"mercari_checked_{owner}.xlsx"

        recheck_records: List[Dict[str, str]] = []

        # 闲鱼：提取未知/多次失败
        if gf_xlsx.exists():
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(gf_xlsx), read_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
                if len(rows) >= 2:
                    headers = [str(h or "") for h in rows[0]]
                    id_idx = headers.index("item_id") if "item_id" in headers else 0
                    st_idx = headers.index("status") if "status" in headers else 1
                    for row in rows[1:]:
                        iid = str(row[id_idx] or "").strip()
                        status = str(row[st_idx] or "").strip()
                        if iid and status in ("未知", "多次失败"):
                            recheck_records.append({
                                "barcode": iid,
                                "product_code": "",
                                "account": "",
                            })
            except Exception as e:
                self.log(f"读取闲鱼结果失败：{e}")

        # 煤炉：提取未知/请求错误
        if mc_xlsx.exists():
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(mc_xlsx), read_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
                if len(rows) >= 2:
                    headers = [str(h or "") for h in rows[0]]
                    url_idx = headers.index("url") if "url" in headers else 0
                    st_idx = headers.index("status") if "status" in headers else 1
                    for row in rows[1:]:
                        url = str(row[url_idx] or "").strip()
                        status = str(row[st_idx] or "").strip()
                        if url and status in ("未知", "請求錯誤"):
                            recheck_records.append({
                                "barcode": url,
                                "product_code": "",
                                "account": "",
                            })
            except Exception as e:
                self.log(f"读取煤炉结果失败：{e}")

        if not recheck_records:
            messagebox.showinfo("提示", "没有需要重检的未知项")
            return

        # 尝试从云端补充 account/product_code 映射
        self._enrich_from_cloud(recheck_records)

        self.log(f"重检：{len(recheck_records)} 条未知项")
        self._records = recheck_records
        self.var_source.set("cloud")
        self.start()

    def _enrich_from_cloud(self, records: List[Dict[str, str]]):
        """尝试从云端 D1 补充 account/product_code 信息。"""
        owner = self.var_owner.get().strip()
        if not owner:
            return
        try:
            # 拉取全部数据建立 barcode → {account, product_code} 映射
            bc_map: Dict[str, Dict[str, str]] = {}
            offset = 0
            while True:
                resp = requests.get(
                    f"{DEFAULT_WORKER_URL}/api/barcodes",
                    params={"owner": owner, "limit": 5000, "offset": offset},
                    timeout=30,
                )
                data = resp.json()
                if not data.get("ok"):
                    break
                rows = data.get("data", [])
                for r in rows:
                    bc = r.get("barcode", "")
                    if bc:
                        bc_map[bc] = {
                            "account": r.get("account", ""),
                            "product_code": r.get("product_code", ""),
                        }
                if len(rows) < 5000:
                    break
                offset += len(rows)

            enriched = 0
            for rec in records:
                info = bc_map.get(rec["barcode"])
                if info:
                    rec["account"] = info["account"]
                    rec["product_code"] = info["product_code"]
                    enriched += 1
            if enriched:
                self.log(f"从云端补充了 {enriched} 条映射信息")
        except Exception as e:
            self.log(f"云端补充映射失败：{e}")