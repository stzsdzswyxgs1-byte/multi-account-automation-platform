from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

requests.packages.urllib3.disable_warnings()

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = (BASE_DIR / "output").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_item_id(s: str) -> str:
    """兼容纯 ID / 链接：优先匹配 id=xxx，再退回最长数字串"""
    s = (s or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    import re
    for pat in (r"[?&]id=(\d+)", r"/item\?id=(\d+)", r"id=(\d+)"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    m = re.search(r"(\d{6,})", s)
    return m.group(1) if m else s


def _load_mapping(txt_path: str) -> Optional[Dict[str, List[Dict[str, str]]]]:
    p = Path(txt_path)
    mapping_path = p.with_suffix(".mapping.json")
    if not mapping_path.exists():
        return None
    try:
        with open(mapping_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _expand_row_by_mapping(row: Dict[str, Any], mapping: Optional[Dict]) -> List[Dict[str, Any]]:
    if not mapping:
        return [row]
    item_id = row.get("item_id", "")
    entries = mapping.get(item_id)
    if not entries:
        return [row]
    expanded = []
    for entry in entries:
        r = dict(row)
        r["account"] = entry.get("account", "")
        r["product_code"] = entry.get("product_code", "")
        expanded.append(r)
    return expanded


def _save_results_to_excel(rows_iter, out_path: str) -> None:
    import openpyxl
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet(title="goofish_status")
    headers = ["item_id", "status", "account", "product_code"]
    ws.append(headers)
    for r in rows_iter:
        ws.append([r.get(h, "") for h in headers])
    wb.save(out_path)


def _split_by_account(
    results: List[Optional[Dict[str, Any]]],
    item_ids: List[str],
    mapping: Dict[str, List[Dict[str, str]]],
    ids_dir: Path,
) -> int:
    account_codes: Dict[str, set] = {}
    for i, r in enumerate(results):
        if r is None:
            continue
        status = r.get("status", "")
        if status in ("在线", "未知", "多次失败"):
            continue
        item_id = item_ids[i]
        entries = mapping.get(item_id)
        if not entries:
            continue
        for entry in entries:
            acc = entry.get("account", "").strip()
            pc = entry.get("product_code", "").strip()
            if acc and pc:
                account_codes.setdefault(acc, set()).add(pc)

    for acc, codes in account_codes.items():
        out_file = ids_dir / f"{acc}.txt"
        with open(out_file, "w", encoding="utf-8") as f:
            for code in sorted(codes):
                f.write(code + "\n")

    return len(account_codes)


def _extract_unknown_ids(excel_path: str, out_txt: str, original_mapping_path: Optional[str] = None) -> int:
    import openpyxl
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return 0

    headers = [str(h or "") for h in rows[0]]
    id_idx = headers.index("item_id") if "item_id" in headers else 0
    status_idx = headers.index("status") if "status" in headers else 1

    unknown_ids: List[str] = []
    seen: set = set()
    for row in rows[1:]:
        status = str(row[status_idx] or "").strip()
        item_id = str(row[id_idx] or "").strip()
        if status in ("未知", "多次失败") and item_id and item_id not in seen:
            unknown_ids.append(item_id)
            seen.add(item_id)

    if not unknown_ids:
        return 0

    with open(out_txt, "w", encoding="utf-8") as f:
        for iid in unknown_ids:
            f.write(iid + "\n")

    if original_mapping_path:
        try:
            with open(original_mapping_path, "r", encoding="utf-8") as f:
                full_mapping = json.load(f)
            filtered = {iid: full_mapping[iid] for iid in unknown_ids if iid in full_mapping}
            out_mapping = Path(out_txt).with_suffix(".mapping.json")
            with open(out_mapping, "w", encoding="utf-8") as f:
                json.dump(filtered, ensure_ascii=False, fp=f)
        except Exception:
            pass

    return len(unknown_ids)


class GoofishCheckFeatureTab:
    """闲鱼商品状态检测（HTTP API 直连，不走中转/代理）"""

    def __init__(self, *, app: Any, frame: ttk.Frame):
        self.app = app
        self.frame = frame

        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.skip_post_actions = False  # unified 调用时设为 True

        # UI vars
        self.var_input = tk.StringVar(value="")
        self.var_out = tk.StringVar(value=str(OUTPUT_DIR / "xianyu_已检测.txt"))
        self.var_out_fail = tk.StringVar(value=str(OUTPUT_DIR / "xianyu_多次失败.txt"))

        # 手机辅助签名模式：手机 /sign 是瓶颈，并发开高也不会被限流
        self.var_max_workers = tk.IntVar(value=32)
        self.var_interval_ms = tk.IntVar(value=0)  # 手机模式无需间隔（acs.m 无 BX）
        self.var_max_attempts = tk.IntVar(value=1)  # 兼容 unified_check_feature 的接口（已不再使用）

        self.var_progress = tk.StringVar(value="未開始")
        self.var_done = tk.IntVar(value=0)
        self.var_total = tk.IntVar(value=0)

        self.btn_start: Optional[ttk.Button] = None
        self.btn_stop: Optional[ttk.Button] = None
        self.btn_open_out: Optional[ttk.Button] = None

    # ---------------- utils ----------------
    def log(self, s: str) -> None:
        try:
            self.app.log(f"[XIAN-YU] {s}")
        except Exception:
            print(s)

    def _set_progress(self, s: str) -> None:
        def _():
            self.var_progress.set(s)
        try:
            self.app.after(0, _)
        except Exception:
            self.var_progress.set(s)

    def _set_counts(self, done: int, total: int) -> None:
        def _():
            self.var_done.set(done)
            self.var_total.set(total)
        try:
            self.app.after(0, _)
        except Exception:
            self.var_done.set(done)
            self.var_total.set(total)

    def _set_btn_state(self, running: bool) -> None:
        def _():
            if self.btn_start:
                self.btn_start.configure(state=("disabled" if running else "normal"))
            if self.btn_stop:
                self.btn_stop.configure(state=("normal" if running else "disabled"))
        try:
            self.app.after(0, _)
        except Exception:
            pass

    # ---------------- UI ----------------
    def build(self) -> None:
        self.frame.columnconfigure(0, weight=1)

        lf_in = ttk.Labelframe(self.frame, text="輸入/輸出")
        lf_in.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        lf_in.columnconfigure(1, weight=1)

        ttk.Label(lf_in, text="ID清單(txt)：").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(lf_in, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_in, text="瀏覽", command=self._pick_input).grid(row=0, column=2, sticky="ew", padx=4, pady=4)

        ttk.Label(lf_in, text="非在售輸出(txt)：").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(lf_in, textvariable=self.var_out).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_in, text="另存為", command=self._pick_out).grid(row=1, column=2, sticky="ew", padx=4, pady=4)

        ttk.Label(lf_in, text="多次失敗輸出(txt)：").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(lf_in, textvariable=self.var_out_fail).grid(row=2, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_in, text="另存為", command=self._pick_out_fail).grid(row=2, column=2, sticky="ew", padx=4, pady=4)

        lf_cfg = ttk.Labelframe(self.frame, text="參數")
        lf_cfg.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        for i in range(4):
            lf_cfg.columnconfigure(i, weight=1)

        ttk.Label(lf_cfg, text="併發數").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=1, to=200, textvariable=self.var_max_workers, width=6).grid(row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(lf_cfg, text="請求間隔(ms)").grid(row=0, column=2, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=0, to=5000, increment=50, textvariable=self.var_interval_ms, width=8).grid(row=0, column=3, sticky="w", padx=4, pady=4)

        lf_ctl = ttk.Frame(self.frame)
        lf_ctl.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        lf_ctl.columnconfigure((0, 1, 2, 3), weight=1)

        self.btn_start = ttk.Button(lf_ctl, text="開始檢測", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self.btn_stop = ttk.Button(lf_ctl, text="停止", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.btn_recheck = ttk.Button(lf_ctl, text="重檢未知", command=self._recheck_unknown)
        self.btn_recheck.grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        self.btn_open_out = ttk.Button(lf_ctl, text="打開輸出資料夾", command=self.open_output_dir)
        self.btn_open_out.grid(row=0, column=3, sticky="ew", padx=4, pady=4)

        lf_stat = ttk.Labelframe(self.frame, text="狀態")
        lf_stat.grid(row=3, column=0, sticky="ew", padx=6, pady=6)
        lf_stat.columnconfigure(1, weight=1)

        ttk.Label(lf_stat, text="進度：").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, textvariable=self.var_progress).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, text="已完成/總數：").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, textvariable=self.var_done).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, text="/").grid(row=1, column=2, sticky="w")
        ttk.Label(lf_stat, textvariable=self.var_total).grid(row=1, column=3, sticky="w", padx=4, pady=4)

    def _pick_input(self) -> None:
        p = filedialog.askopenfilename(
            title="選擇 闲鱼 商品ID清單",
            filetypes=[("Text", "*.txt;*.csv;*.*"), ("All", "*.*")],
        )
        if p:
            self.var_input.set(p)

    def _pick_out(self) -> None:
        p = filedialog.asksaveasfilename(
            title="另存為（非在售）",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
            initialfile=os.path.basename(self.var_out.get() or "xianyu_已检测.txt"),
        )
        if p:
            self.var_out.set(p)

    def _pick_out_fail(self) -> None:
        p = filedialog.asksaveasfilename(
            title="另存為（多次失败）",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt")],
            initialfile=os.path.basename(self.var_out_fail.get() or "xianyu_多次失败.txt"),
        )
        if p:
            self.var_out_fail.set(p)

    def open_output_dir(self) -> None:
        try:
            os.startfile(str(OUTPUT_DIR))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showinfo("提示", f"打開失敗：{e}")

    # ---------------- run/stop ----------------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在檢測中…")
            return

        in_path = self.var_input.get().strip()
        if not in_path:
            p = BASE_DIR / "test.txt"
            if p.is_file():
                in_path = str(p)
                self.var_input.set(in_path)

        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("錯誤", "找不到輸入檔（請選擇 txt）")
            return

        out_path = self.var_out.get().strip() or str(OUTPUT_DIR / "xianyu_已检测.txt")
        out_fail_path = self.var_out_fail.get().strip() or str(OUTPUT_DIR / "xianyu_多次失败.txt")
        self.var_out.set(out_path)
        self.var_out_fail.set(out_fail_path)

        self._stop_evt.clear()
        self._set_btn_state(True)
        self._set_progress("讀取ID中…")

        def _run():
            try:
                self._run_check(in_path, out_path, out_fail_path)
            except RuntimeError as e:
                self.log(f"程序已终止：{e}")
                self._set_progress(f"程序已终止：{e}")
            except Exception as e:
                self.log(f"崩溃：{e}")
                self._set_progress(f"崩溃：{e}")
            finally:
                self._set_btn_state(False)

        self._worker = threading.Thread(target=_run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._set_progress("正在停止（會在當前任務結束後停止）…")

    def _recheck_unknown(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在檢測中，請先停止")
            return

        in_path = self.var_input.get().strip()
        excel_path = ""
        if in_path:
            candidate = str(Path(in_path).with_suffix(".xlsx"))
            if os.path.exists(candidate):
                excel_path = candidate
        if not excel_path:
            for name in ("cloud_goofish_<SUPERVISOR_CHAT_ID>.xlsx", "xianyu_已检测.xlsx"):
                candidate = str(OUTPUT_DIR / name)
                if os.path.exists(candidate):
                    excel_path = candidate
                    break
        if not excel_path:
            messagebox.showerror("錯誤", "找不到檢測結果 Excel，請先完成一次檢測")
            return

        self._set_progress("正在提取未知項…")

        def _extract():
            base = Path(excel_path)
            unknown_txt = str(base.parent / (base.stem + "_unknown.txt"))
            original_mapping = None
            if in_path:
                mp = Path(in_path).with_suffix(".mapping.json")
                if mp.exists():
                    original_mapping = str(mp)
            count = _extract_unknown_ids(excel_path, unknown_txt, original_mapping)

            def _on_done():
                if count == 0:
                    self._set_progress("未開始")
                    messagebox.showinfo("提示", "沒有「未知」或「多次失敗」的項目需要重檢")
                    return
                self.var_input.set(unknown_txt)
                out_new = str(base.parent / (base.stem + "_unknown_result.txt"))
                self.var_out.set(out_new)
                self.var_out_fail.set(str(base.parent / (base.stem + "_unknown_fail.txt")))
                self.log(f"已提取 {count} 條未知/失敗 ID，開始重新檢測")
                self.start()
            self.frame.after(0, _on_done)

        threading.Thread(target=_extract, daemon=True).start()

    def _run_check(self, in_path: str, out_path: str, out_fail_path: str) -> None:
        # 读 ID
        with open(in_path, "r", encoding="utf-8") as f:
            raw_lines = [line.strip() for line in f if line.strip()]

        all_item_ids = [extract_item_id(s) for s in raw_lines]
        all_item_ids = [x for x in all_item_ids if x]

        # 去重
        orig_count = len(all_item_ids)
        seen = set()
        deduped = []
        for iid in all_item_ids:
            if iid not in seen:
                seen.add(iid)
                deduped.append(iid)
        all_item_ids = deduped
        if orig_count != len(all_item_ids):
            self.log(f"ID去重：{orig_count} → {len(all_item_ids)}（移除 {orig_count - len(all_item_ids)} 條重複）")

        item_ids = list(all_item_ids)
        total = len(all_item_ids)
        self._set_counts(0, total)

        if not item_ids:
            self.log("没有 ID 需要检测")
            self._set_progress("完成（无 ID）")
            return

        # 清空输出
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_fail_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as out:
            out.write("")
        with open(out_fail_path, "w", encoding="utf-8") as out:
            out.write("")

        # ══════ 检测引擎：仅手机辅助签名模式（零限流，唯一可靠方案） ══════
        try:
            from core.goofish_phone_check import PhoneApiChecker
        except Exception as e:
            self.log(f"[检测引擎] 加载手机模块失败：{e}")
            self._set_progress("手机模块加载失败")
            return

        api_checker = PhoneApiChecker(log=self.log)
        if not api_checker.load():
            self.log("[检测引擎] ✗ 手机连接失败！HTTP 模式已禁用（必被 BX 限流，无意义）")
            self.log("[检测引擎] 请确认：1) 手机已配置 LSPosed + appsign_patched.apk")
            self.log("[检测引擎]          2) 手机和 PC 在同一 WiFi")
            self.log("[检测引擎]          3) settings.json 的 goofish_phone_ip 与手机 IP 一致")
            self._set_progress("手机连接失败")
            return
        self.log("[检测引擎] ✓ 手机辅助签名模式（acs.m 直连，零限流）")

        max_workers = max(1, int(self.var_max_workers.get() or 15))
        interval_s = max(0.0, float(self.var_interval_ms.get() or 0) / 1000.0)
        self.log(f"[API] 开始检测 {len(item_ids)} 条（{max_workers} 并发 + {int(interval_s*1000)}ms间隔）...")
        self._set_progress("API 检测中…")

        import concurrent.futures
        api_done = 0
        api_fail = 0
        _consecutive_fail = 0
        _circuit_broken = False
        _circuit_lock = threading.Lock()
        _request_lock = threading.Lock()
        _last_request_time = [0.0]
        _pause_until = [0.0]      # 限流触发时所有线程暂停到这个时间戳
        _pause_count = [0]        # 累计 pause 次数（>=4 次说明 cookie 真不行了，彻底停）
        results_api: List[Optional[Dict[str, Any]]] = [None] * len(item_ids)
        failed_items: List[str] = []

        def _api_check(idx):
            nonlocal _consecutive_fail
            if self._stop_evt.is_set() or _circuit_broken:
                return idx, None
            # 限流冷却期：所有线程等待
            _wait = _pause_until[0] - time.time()
            if _wait > 0:
                time.sleep(min(_wait, 60))
            iid = item_ids[idx]
            if interval_s > 0:
                with _request_lock:
                    _now = time.time()
                    _gap = _now - _last_request_time[0]
                    if _gap < interval_s:
                        time.sleep(interval_s - _gap)
                    _last_request_time[0] = time.time()
            if self._stop_evt.is_set() or _circuit_broken:
                return idx, None
            r = api_checker.check_item(iid)
            return idx, r

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_api_check, i): i for i in range(len(item_ids))}
            for future in concurrent.futures.as_completed(futures):
                if self._stop_evt.is_set():
                    for f in futures:
                        f.cancel()
                    break
                if _circuit_broken:
                    for f in futures:
                        f.cancel()
                    break
                idx, result = future.result()
                if result:
                    status = result["status"]
                    results_api[idx] = {"item_id": item_ids[idx], "status": status}
                    if status not in ("在线",):
                        failed_items.append(item_ids[idx])
                    api_done += 1
                    with _circuit_lock:
                        _consecutive_fail = 0
                else:
                    results_api[idx] = {"item_id": item_ids[idx], "status": "未知"}
                    api_fail += 1
                    with _circuit_lock:
                        _consecutive_fail += 1
                        # 连续 10 条失败 → pause 60 秒让 BX 冷却，然后重置计数继续
                        # 累计 pause 4 次（即限流反复触发）才彻底停止，避免 cookie 真坏了死磕
                        if _consecutive_fail >= 10:
                            _pause_count[0] += 1
                            if _pause_count[0] >= 4:
                                _circuit_broken = True
                                self.log(f"[API] ⚠ 已 pause 重试 {_pause_count[0]} 次仍失败，cookie 可能已被 BX 拉黑")
                                self.log(f"[API] 已处理 {api_done + api_fail}/{len(item_ids)}（成功 {api_done}）")
                                self.log(f"[API] 请重新点【闲鱼检测专用登录】并完成滑块验证")
                            else:
                                _pause_until[0] = time.time() + 60
                                _consecutive_fail = 0
                                self.log(f"[API] ⚠ 连续 10 条失败，pause 60 秒等 BX 冷却（第 {_pause_count[0]}/3 次重试）")
                                self.log(f"[API] 当前进度 {api_done + api_fail}/{len(item_ids)}（成功 {api_done}）")

                if (api_done + api_fail) % 200 == 0:
                    self._set_counts(api_done + api_fail, total)
                    self._set_progress(f"API 检测 {api_done + api_fail}/{len(item_ids)}…")

        self._set_counts(api_done + api_fail, total)
        self.log(f"[API] 完成：成功 {api_done}，失败 {api_fail}")
        try:
            _err_summary = api_checker.get_error_summary()
            if api_fail > 0:
                self.log(f"[API] [诊断] 失败原因统计: {_err_summary}")
        except Exception:
            pass

        # 写非在售输出（拍卖也算非在售 — 我们只保留 一口价 货源）
        with open(out_path, "w", encoding="utf-8") as out:
            for r in results_api:
                if r and r["status"] in ("卖掉了", "已下架", "已删除", "拍卖"):
                    out.write(f"{r['item_id']}\t{r['status']}\n")

        # 写多次失败输出
        if failed_items_unknown := [it["item_id"] for it in results_api if it and it["status"] == "未知"]:
            with open(out_fail_path, "w", encoding="utf-8") as f:
                for iid in failed_items_unknown:
                    f.write(iid + "\n")

        if self._stop_evt.is_set():
            self._set_progress("已停止")
            return

        if api_fail > 0:
            self._set_progress(f"部分完成（成功 {api_done}，失败 {api_fail}）")
            self.log(f"⚠ 检测未完整！成功 {api_done}，失败 {api_fail} / 总 {total}")
        else:
            self._set_progress("完成（結果已輸出）")
        self.log(f"非在售商品写入：{out_path}")

        # v6.0.63: 记录最后一次检测的失败计数,unified_check_feature 可读这个判断是否跳过下架/D1
        # v6.0.71: 加 _last_run_total,使下游能算 fail rate 做容忍判斷
        self._last_run_failed = api_fail
        self._last_run_total = total

        # v6.0.71 容忍閾值:少量失敗(<=10 條 OR <0.1%)視為「夠完整」,繼續下架/D1
        # 因為 1 條 timeout / 漏抓對 11 萬筆來說無感,跳過全部太武斷
        # (使用者反映:「就差 1 個就沒有下架,這太不合理了」)
        TOLERANCE_ABS = 10
        TOLERANCE_PCT = 0.001
        is_incomplete = api_fail > 0 and api_fail > TOLERANCE_ABS and (
            total <= 0 or (api_fail / total) > TOLERANCE_PCT
        )
        if api_fail > 0 and not is_incomplete:
            self.log(f"[CHECK] {api_fail} 条失败 / 总 {total}({api_fail*100/max(total,1):.4f}%),"
                     f"容忍范围内,继续下架/D1 清理")

        # 后处理：Excel + 帐号拆分 + D1 清理(达到容忍阈值才跳过 D1)
        self._goto_post_process(in_path, out_path, out_fail_path,
                                results_api, all_item_ids, failed_items_unknown,
                                skip_d1=is_incomplete)

    def _goto_post_process(self, in_path, out_path, out_fail_path,
                           results, all_item_ids, failed_items, skip_d1=False):
        """后处理：写 Excel + 按账号拆分 + D1 清理"""
        mapping = _load_mapping(in_path)
        excel_path = str(Path(in_path).with_suffix(".xlsx"))
        try:
            def _rows_iter():
                for r in results:
                    if r is None:
                        continue
                    yield from _expand_row_by_mapping(r, mapping)
            _save_results_to_excel(_rows_iter(), excel_path)
            self.log(f"Excel 結果已輸出：{excel_path}")
        except Exception as e:
            self.log(f"寫入 Excel 失敗：{e}")

        if mapping and not self.skip_post_actions:
            try:
                ids_dir = BASE_DIR / "ids"
                ids_dir.mkdir(parents=True, exist_ok=True)
                split_count = _split_by_account(results, all_item_ids, mapping, ids_dir)
                if split_count:
                    self.log(f"已按帳號拆分到 ids/ 目錄（{split_count} 個帳號）")
            except Exception as e:
                self.log(f"按帳號拆分失敗：{e}")

        if not self.skip_post_actions and not skip_d1:
            self._d1_cleanup_from_excel(excel_path)
        elif skip_d1:
            self.log("⚠ 结果不完整，跳过自动 D1 清理（防止误删在售商品）")

    def _d1_cleanup_from_excel(self, excel_path: str):
        """从检测结果 Excel 中收集非在售 barcode，调用 D1 删除。"""
        import requests as _req
        import openpyxl
        from core.doc_upload_feature import DEFAULT_WORKER_URL, DEFAULT_UPLOAD_TOKEN

        non_active_statuses = ("卖掉了", "已下架", "已删除", "拍卖")
        try:
            wb = openpyxl.load_workbook(excel_path, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
        except Exception as e:
            self.log(f"[D1清理] 读取Excel失败：{e}")
            return

        if len(rows) < 2:
            return

        headers = [str(h or "") for h in rows[0]]
        id_idx = headers.index("item_id") if "item_id" in headers else 0
        st_idx = headers.index("status") if "status" in headers else 1

        barcodes = []
        for row in rows[1:]:
            status = str(row[st_idx] or "").strip()
            if status in non_active_statuses:
                item_id = str(row[id_idx] or "").strip()
                if item_id:
                    barcodes.append(item_id)

        if not barcodes:
            self.log("[D1清理] 无需清理")
            return

        barcodes = list(set(barcodes))
        self.log(f"[D1清理] 开始清理 {len(barcodes)} 条非在售记录")
        self._set_progress("D1清理中…")

        BATCH = 200
        deleted = 0
        for i in range(0, len(barcodes), BATCH):
            chunk = barcodes[i:i + BATCH]
            try:
                resp = _req.post(
                    f"{DEFAULT_WORKER_URL}/api/delete-barcodes",
                    json={"token": DEFAULT_UPLOAD_TOKEN, "barcodes": chunk},
                    timeout=30,
                )
                data = resp.json()
                if data.get("ok"):
                    deleted += data.get("deleted", 0)
                else:
                    self.log(f"[D1清理] 批次失败：{data.get('error', '?')}")
            except Exception as e:
                self.log(f"[D1清理] 请求异常：{e}")

        self.log(f"[D1清理] 完成：删除 {deleted} 条")
        self._set_progress("完成（結果已輸出）")
