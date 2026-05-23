from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

import openpyxl
import re as _re
import urllib.request

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = (BASE_DIR / "output").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _extract_mercari_item_id(url: str) -> str:
    """从 URL 提取 Mercari 商品 ID（如 m90000000002）。"""
    m = _re.search(r'/item/(m\d+)', url or "")
    return m.group(1) if m else ""


# ── Mercari HTTP API 检测（无需浏览器，~100ms/件） ──

def _make_dpop_signer():
    """生成 DPoP 签名器（一次性生成密钥对，重复签名）。"""
    import base64 as _b64
    import uuid as _uuid
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.backends import default_backend as _backend
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature as _decode_sig

    pk = _ec.generate_private_key(_ec.SECP256R1(), _backend())
    pub = pk.public_key().public_numbers()
    x = pub.x.to_bytes(32, 'big')
    y = pub.y.to_bytes(32, 'big')

    def _b64url(data: bytes) -> str:
        return _b64.urlsafe_b64encode(data).rstrip(b'=').decode()

    header_b64 = _b64url(json.dumps({
        'typ': 'dpop+jwt', 'alg': 'ES256',
        'jwk': {'crv': 'P-256', 'kty': 'EC', 'x': _b64url(x), 'y': _b64url(y)}
    }, separators=(',', ':')).encode())

    def sign() -> str:
        payload = {'iat': int(time.time()), 'jti': str(_uuid.uuid4()),
                   'htu': 'https://api.mercari.jp/items/get', 'htm': 'GET'}
        p_b64 = _b64url(json.dumps(payload, separators=(',', ':')).encode())
        sig_der = pk.sign(f'{header_b64}.{p_b64}'.encode(), _ec.ECDSA(_hashes.SHA256()))
        r_int, s_int = _decode_sig(sig_der)
        return f'{header_b64}.{p_b64}.{_b64url(r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big"))}'

    return sign


def _check_mercari_api(item_id: str, session, dpop_sign) -> Optional[Dict[str, Any]]:
    """通过 Mercari API 检测单个商品状态。返回 result dict 或 None（API 失败时）。"""
    for _attempt in range(3):
        try:
            dpop = dpop_sign()
            r = session.get(
                f'https://api.mercari.jp/items/get?id={item_id}',
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'X-Platform': 'web',
                    'DPoP': dpop,
                    'Referer': 'https://jp.mercari.com/',
                    'Accept-Language': 'ja',
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get('data', {})
                status = data.get('status', '')
                if status == 'on_sale':
                    return {"status": "在线", "deleted_hit": False, "sold_hit": False, "buy_hit": True}
                elif status == 'sold_out':
                    return {"status": "卖掉了", "deleted_hit": False, "sold_hit": True, "buy_hit": False}
                elif status == 'trading':
                    return {"status": "卖掉了", "deleted_hit": False, "sold_hit": True, "buy_hit": False}
                else:
                    return {"status": status or "未知", "deleted_hit": False, "sold_hit": False, "buy_hit": False}
            elif r.status_code in (404, 403):
                return {"status": "已删除", "deleted_hit": True, "sold_hit": False, "buy_hit": False}
            elif r.status_code == 429:
                time.sleep(2)
                continue
        except Exception:
            if _attempt < 2:
                time.sleep(1)
                continue
    return None


async def _cdn_image_exists(item_id: str) -> bool:
    """检查 Mercari CDN 图片是否还在（HEAD 请求，3s 超时）。

    返回 True 表示图片存在 → 商品可能仍在售（仅网页不可见）。
    返回 False 表示图片不存在 → 商品确实已删除。
    """
    cdn_url = f"https://static.mercdn.net/item/detail/orig/photos/{item_id}_1.jpg"
    def _check():
        try:
            req = urllib.request.Request(cdn_url, method="HEAD",
                                        headers={"Referer": "https://jp.mercari.com/",
                                                 "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False
    return await asyncio.to_thread(_check)


# === 資源攔截設定，避免加載圖片 / 字體 / 視頻 / CSS 等 ===
BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "ping", "eventsource", "websocket", "manifest", "texttrack"}

# 屏蔽第三方追蹤/廣告域名，大幅加快頁面加載
BLOCK_DOMAINS = {
    "www.google-analytics.com", "www.googletagmanager.com",
    "analytics.google.com", "stats.g.doubleclick.net",
    "www.facebook.com", "connect.facebook.net",
    "static.ads-twitter.com", "t.co",
    "cdn.branch.io", "app.link",
    "sentry.io", "o*.ingest.sentry.io",
    "datadog", "rum-http-intake",
}


async def _route_handler(route, request):
    """
    攔截不必要的資源，加快加載速度。
    同時用 try/except 吃掉 driver 關閉後殘留的錯誤。
    """
    rtype = request.resource_type
    url = request.url
    try:
        if rtype in BLOCK_RESOURCE_TYPES:
            await route.abort()
        elif any(d in url for d in BLOCK_DOMAINS):
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


def _load_urls(path: str) -> List[str]:
    urls: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            urls.append(s)
    return urls


def _load_mapping(txt_path: str) -> Optional[Dict[str, List[Dict[str, str]]]]:
    """載入 sidecar mapping.json（與 txt 同名）。不存在則返回 None。"""
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
    """根據 mapping 展開一行結果為多行（每個帳號一行）。無 mapping 時原樣返回。"""
    if not mapping:
        return [row]
    url = row.get("url", "")
    entries = mapping.get(url)
    if not entries:
        return [row]
    expanded = []
    for entry in entries:
        r = dict(row)
        r["account"] = entry.get("account", "")
        r["product_code"] = entry.get("product_code", "")
        expanded.append(r)
    return expanded


def _classify_from_text(text: str) -> Dict[str, Any]:
    """根據頁面文字判斷狀態（保持你原腳本邏輯）。"""
    deleted_hit = (
        "該当する商品は削除されています" in text
        or "該当する商品は削除されて" in text
        or "お探しのページは見つかりませんでした" in text
    )

    # 拍賣：只要看到「オークション商品」或「入札する」就當作拍賣
    auction_hit = ("オークション商品" in text) or ("入札する" in text)

    # 普通購入相關按鈕
    buy_hit = (
        "購入手続きへ" in text
        or "支払う" in text
        or "購入する" in text
        or "購入に進む" in text
        or "カートに入れる" in text
    )

    # 已售完
    sold_hit = (
        "売り切れ" in text
        or "この商品は売り切れました" in text
        or "売り切れました" in text
    )

    # 判斷順序：刪除 > 已售完 > 拍賣 > 在售 > 未知
    if deleted_hit:
        status = "刪除或不存在"
    elif sold_hit:
        status = "已售完"
    elif auction_hit:
        # 兩種都算拍賣中，只是文字稍微區分一下
        if buy_hit:
            status = "拍賣中（入札+可直接購買）"
        else:
            status = "拍賣中（入札受付中）"
    elif buy_hit:
        status = "在售"
    else:
        status = "未知"

    return {
        "status": status,
        "deleted_hit": deleted_hit,
        "auction_hit": auction_hit,
        "buy_hit": buy_hit,
        "sold_hit": sold_hit,
    }


def _save_results_to_excel(rows_iter, out_path: str) -> None:
    """寫入 Excel（覆蓋輸出）。

    rows_iter: 可為 list，也可為 generator/iterator（避免反覆建立超大 list）。
    """
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet(title="mercari_status")

    headers = ["url", "status", "account", "product_code", "deleted_hit", "auction_hit", "buy_hit", "sold_hit", "error_msg"]
    ws.append(headers)

    for r in rows_iter:
        ws.append([r.get(h, "") for h in headers])

    # write_only 模式不支援 column_dimensions 設置列寬（否則會非常慢/占用記憶體）
    wb.save(out_path)


def _split_by_account(
    results: List[Optional[Dict[str, Any]]],
    urls: List[str],
    mapping: Dict[str, List[Dict[str, str]]],
    ids_dir: Path,
) -> int:
    """按帳號拆分非在售項目的 product_code，寫入 ids/{account}.txt。返回帳號數。"""
    # account → set of product_codes
    account_codes: Dict[str, set] = {}
    for i, r in enumerate(results):
        if r is None:
            continue
        status = r.get("status", "")
        if status in ("在售", "未知", "請求錯誤"):
            continue
        url = urls[i]
        entries = mapping.get(url)
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


def _extract_unknown_urls(excel_path: str, out_txt: str, original_mapping_path: Optional[str] = None) -> int:
    """從檢測結果 Excel 提取狀態為「未知」的 URL，寫入新 txt。返回未知 URL 數量。"""
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return 0

    headers = [str(h or "") for h in rows[0]]
    url_idx = headers.index("url") if "url" in headers else 0
    status_idx = headers.index("status") if "status" in headers else 1

    unknown_urls: List[str] = []
    seen = set()
    for row in rows[1:]:
        status = str(row[status_idx] or "").strip()
        url = str(row[url_idx] or "").strip()
        if status in ("未知", "請求錯誤") and url and url not in seen:
            unknown_urls.append(url)
            seen.add(url)

    if not unknown_urls:
        return 0

    # 寫入去重後的未知 URL txt
    with open(out_txt, "w", encoding="utf-8") as f:
        for url in unknown_urls:
            f.write(url + "\n")

    # 如果有原始 mapping，生成過濾版 mapping.json
    if original_mapping_path:
        try:
            with open(original_mapping_path, "r", encoding="utf-8") as f:
                full_mapping = json.load(f)
            filtered = {url: full_mapping[url] for url in unknown_urls if url in full_mapping}
            out_mapping = Path(out_txt).with_suffix(".mapping.json")
            with open(out_mapping, "w", encoding="utf-8") as f:
                json.dump(filtered, ensure_ascii=False, fp=f)
        except Exception:
            pass

    return len(unknown_urls)


def _merge_recheck_into_original(original_excel: str, recheck_excel: str) -> int:
    """將重檢結果合併回原始 Excel：把原始中「未知」的行用重檢結果覆蓋。

    返回成功合併的行數。
    """
    # 讀取重檢結果，建立 url→row 映射
    wb_re = openpyxl.load_workbook(recheck_excel, read_only=True)
    ws_re = wb_re.active
    re_rows = list(ws_re.iter_rows(values_only=True))
    wb_re.close()

    if not re_rows or len(re_rows) < 2:
        return 0

    re_headers = [str(h or "") for h in re_rows[0]]
    re_url_idx = re_headers.index("url") if "url" in re_headers else 0
    re_status_idx = re_headers.index("status") if "status" in re_headers else 1

    # url → 最新一行（去重取最後出現的）
    recheck_map: Dict[str, tuple] = {}
    for row in re_rows[1:]:
        url = str(row[re_url_idx] or "").strip()
        status = str(row[re_status_idx] or "").strip()
        if url and status != "未知":
            recheck_map[url] = row

    if not recheck_map:
        return 0

    # 讀取原始 Excel（完整模式，需要寫回）
    wb_orig = openpyxl.load_workbook(original_excel)
    ws_orig = wb_orig.active

    orig_headers = [str(c.value or "") for c in ws_orig[1]]
    orig_url_idx = orig_headers.index("url") if "url" in orig_headers else 0
    orig_status_idx = orig_headers.index("status") if "status" in orig_headers else 1

    merged = 0
    for row_idx in range(2, ws_orig.max_row + 1):
        url_cell = ws_orig.cell(row=row_idx, column=orig_url_idx + 1)
        status_cell = ws_orig.cell(row=row_idx, column=orig_status_idx + 1)
        url_val = str(url_cell.value or "").strip()
        status_val = str(status_cell.value or "").strip()

        if status_val == "未知" and url_val in recheck_map:
            new_row = recheck_map[url_val]
            # 按 header 對齊覆蓋每一列
            for col_idx, header in enumerate(orig_headers):
                if header in re_headers:
                    re_col = re_headers.index(header)
                    new_val = new_row[re_col] if re_col < len(new_row) else None
                    ws_orig.cell(row=row_idx, column=col_idx + 1, value=new_val)
            merged += 1

    if merged > 0:
        wb_orig.save(original_excel)
    wb_orig.close()

    return merged


@dataclass
class _MercariConfig:
    max_concurrent: int = 8
    batch_size: int = 500
    max_retries: int = 3
    auto_save_every: int = 1000
    headless: bool = True


class MercariCheckFeatureTab:
    """煤爐（Mercari）商品狀態檢測（整合進主程式的獨立功能頁）"""

    def __init__(self, *, app: Any, frame: ttk.Frame):
        self.app = app
        self.frame = frame

        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._recheck_original_excel: Optional[str] = None
        self.skip_post_actions = False  # unified 调用时设为 True，跳过内部拆分/D1清理

        # UI vars
        self.var_input = tk.StringVar(value="")
        self.var_out = tk.StringVar(value=str(OUTPUT_DIR / "mercari_status_result.xlsx"))
        self.var_max_concurrent = tk.IntVar(value=8)
        self.var_batch_size = tk.IntVar(value=500)
        self.var_max_retries = tk.IntVar(value=3)
        self.var_auto_save_every = tk.IntVar(value=1000)
        self.var_headless = tk.BooleanVar(value=True)
        self.var_progress = tk.StringVar(value="未開始")
        self.var_done = tk.IntVar(value=0)
        self.var_total = tk.IntVar(value=0)

        self.btn_start: Optional[ttk.Button] = None
        self.btn_stop: Optional[ttk.Button] = None
        self.btn_open: Optional[ttk.Button] = None

    # ---------------- utils ----------------
    def log(self, s: str) -> None:
        try:
            self.app.log(f"[MERCARI] {s}")
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

        ttk.Label(lf_in, text="URL清單(txt)：").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ent_in = ttk.Entry(lf_in, textvariable=self.var_input)
        ent_in.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_in, text="瀏覽", command=self._pick_input).grid(row=0, column=2, sticky="ew", padx=4, pady=4)

        ttk.Label(lf_in, text="輸出Excel：").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ent_out = ttk.Entry(lf_in, textvariable=self.var_out)
        ent_out.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_in, text="另存為", command=self._pick_output).grid(row=1, column=2, sticky="ew", padx=4, pady=4)

        lf_cfg = ttk.Labelframe(self.frame, text="參數")
        lf_cfg.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        for i in range(8):
            lf_cfg.columnconfigure(i, weight=1)

        ttk.Label(lf_cfg, text="併發").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=1, to=50, textvariable=self.var_max_concurrent, width=6).grid(row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(lf_cfg, text="批次").grid(row=0, column=2, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=10, to=2000, textvariable=self.var_batch_size, width=7).grid(row=0, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(lf_cfg, text="重試").grid(row=0, column=4, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=0, to=10, textvariable=self.var_max_retries, width=6).grid(row=0, column=5, sticky="w", padx=4, pady=4)

        ttk.Label(lf_cfg, text="自動保存(條)").grid(row=0, column=6, sticky="e", padx=4, pady=4)
        ttk.Spinbox(lf_cfg, from_=0, to=100000, textvariable=self.var_auto_save_every, width=9).grid(row=0, column=7, sticky="w", padx=4, pady=4)

        ttk.Checkbutton(lf_cfg, text="Headless", variable=self.var_headless).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        lf_ctl = ttk.Frame(self.frame)
        lf_ctl.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        lf_ctl.columnconfigure((0, 1, 2, 3, 4), weight=1)

        self.btn_start = ttk.Button(lf_ctl, text="開始檢測", command=self.start)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self.btn_stop = ttk.Button(lf_ctl, text="停止", command=self.stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.btn_open = ttk.Button(lf_ctl, text="打開輸出", command=self.open_output)
        self.btn_open.grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_ctl, text="清空進度", command=self._reset_progress).grid(row=0, column=3, sticky="ew", padx=4, pady=4)
        ttk.Button(lf_ctl, text="重檢未知", command=self._recheck_unknown).grid(row=0, column=4, sticky="ew", padx=4, pady=4)

        lf_stat = ttk.Labelframe(self.frame, text="狀態")
        lf_stat.grid(row=3, column=0, sticky="ew", padx=6, pady=6)
        lf_stat.columnconfigure(1, weight=1)

        ttk.Label(lf_stat, text="進度：").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, textvariable=self.var_progress).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, text="已完成/總數：").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, textvariable=tk.StringVar()).grid_forget()  # placeholder, avoid style odd
        ttk.Label(lf_stat, textvariable=self.var_done).grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(lf_stat, text="/").grid(row=1, column=2, sticky="w")
        ttk.Label(lf_stat, textvariable=self.var_total).grid(row=1, column=3, sticky="w", padx=4, pady=4)

    def _reset_progress(self) -> None:
        self._set_counts(0, 0)
        self._set_progress("未開始")

    def _recheck_unknown(self) -> None:
        """從當前輸出 Excel 提取「未知」URL，設為新輸入並啟動檢測。"""
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在檢測中，請先停止")
            return

        excel_path = self.var_out.get().strip()
        if not excel_path or not os.path.exists(excel_path):
            messagebox.showerror("錯誤", "找不到輸出 Excel，請先完成一次檢測")
            return

        # 記住原始 Excel 路徑，重檢完後合併回去
        self._recheck_original_excel = excel_path

        self._set_progress("正在提取未知項…")

        # 在後台線程提取，避免凍結 UI
        def _extract():
            base = Path(excel_path)
            unknown_txt = str(base.parent / (base.stem + "_unknown.txt"))

            # 嘗試找原始 mapping
            in_path = self.var_input.get().strip()
            original_mapping = None
            if in_path:
                mp = Path(in_path).with_suffix(".mapping.json")
                if mp.exists():
                    original_mapping = str(mp)

            count = _extract_unknown_urls(excel_path, unknown_txt, original_mapping)

            def _on_done():
                if count == 0:
                    self._set_progress("未開始")
                    messagebox.showinfo("提示", "沒有「未知」狀態的項目需要重檢")
                    return
                self.var_input.set(unknown_txt)
                out_new = str(base.parent / (base.stem + "_unknown_result.xlsx"))
                self.var_out.set(out_new)
                self.log(f"已提取 {count} 條未知 URL，開始重新檢測")
                self.start()

            self.frame.after(0, _on_done)

        threading.Thread(target=_extract, daemon=True).start()

    def _pick_input(self) -> None:
        p = filedialog.askopenfilename(
            title="選擇 Mercari URL 清單",
            filetypes=[("Text", "*.txt;*.csv;*.*"), ("All", "*.*")],
        )
        if p:
            self.var_input.set(p)

    def _pick_output(self) -> None:
        p = filedialog.asksaveasfilename(
            title="另存為",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=os.path.basename(self.var_out.get() or "mercari_status_result.xlsx"),
        )
        if p:
            self.var_out.set(p)

    def open_output(self) -> None:
        p = self.var_out.get().strip()
        if not p:
            return
        try:
            os.startfile(p)  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showinfo("提示", f"打開失敗：{e}")

    # ---------------- run/stop ----------------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("提示", "正在檢測中…")
            return

        in_path = self.var_input.get().strip()
        if not in_path:
            # 兼容你原來習慣：如果不選，就找根目錄的 test / test.txt / mercari_urls.txt
            for name in ("test", "test.txt", "mercari_urls.txt"):
                p = (BASE_DIR / name)
                if p.is_file():
                    in_path = str(p)
                    self.var_input.set(in_path)
                    break

        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("錯誤", "找不到輸入檔（請選擇 txt）")
            return

        out_path = self.var_out.get().strip()
        if not out_path:
            out_path = str(OUTPUT_DIR / "mercari_status_result.xlsx")
            self.var_out.set(out_path)

        cfg = _MercariConfig(
            max_concurrent=max(1, int(self.var_max_concurrent.get() or 5)),
            batch_size=max(10, int(self.var_batch_size.get() or 200)),
            max_retries=max(0, int(self.var_max_retries.get() or 3)),
            auto_save_every=max(0, int(self.var_auto_save_every.get() or 0)),
            headless=bool(self.var_headless.get()),
        )

        self._stop_evt.clear()
        self._set_btn_state(True)
        self._set_progress("讀取URL中…")

        def _run():
            try:
                asyncio.run(self._run_check(in_path, out_path, cfg))
            except Exception as e:
                self.log(f"崩潰：{e}")
                self._set_progress(f"崩潰：{e}")
            finally:
                self._set_btn_state(False)

        self._worker = threading.Thread(target=_run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._set_progress("正在停止（會在當前批次結束後停止）…")

    async def _run_check(self, in_path: str, out_path: str, cfg: _MercariConfig) -> None:
        self.log(f"開始：input={in_path} out={out_path} headless={cfg.headless} conc={cfg.max_concurrent}")
        try:
            urls = _load_urls(in_path)
        except Exception as e:
            self._set_progress(f"讀取失敗：{e}")
            return

        # URL 去重（保持順序）
        orig_count = len(urls)
        seen = set()
        deduped = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        urls = deduped
        if orig_count != len(urls):
            self.log(f"URL去重：{orig_count} → {len(urls)}（移除 {orig_count - len(urls)} 條重複）")

        total = len(urls)
        if total <= 0:
            self._set_progress("輸入檔為空")
            return

        # 載入 sidecar mapping（雲端下載時生成，獨立使用時為 None）
        mapping = _load_mapping(in_path)
        if mapping:
            self.log(f"已載入帳號映射：{len(mapping)} 條 URL 有帳號資訊")

        # 不使用缓存，全部重新检测
        cached_count = 0
        remaining_indices = list(range(total))

        # results and retry count
        results: List[Optional[Dict[str, Any]]] = [None] * total
        retries: List[int] = [0] * total

        self._set_counts(0, total)

        if not remaining_indices:
            self._set_progress("輸入檔為空")
            # 直接跳到保存
            remaining_indices = []
            # fall through to save logic below

        last_saved_completed = cached_count

        # ══════ Phase 1: HTTP API 快速检测（不需要浏览器，~100ms/件）══════
        if remaining_indices and not self._stop_evt.is_set():
            self.log(f"[API模式] 开始 HTTP API 检测 {len(remaining_indices)} 条...")
            self._set_progress(f"API 检测中（{len(remaining_indices)} 条）…")
            try:
                from curl_cffi.requests import Session as CffiSession
                from core.merch_http_ops import _detect_system_proxy
                _proxy = _detect_system_proxy()
                _proxy_kw = {"proxy": _proxy} if _proxy else {}
                if _proxy:
                    self.log(f"[API模式] 检测到系统代理: {_proxy}")
                api_session = CffiSession(impersonate="chrome", **_proxy_kw)
                dpop_sign = _make_dpop_signer()

                api_done = 0
                api_fail = 0
                api_indices_done = []

                # 并发检测
                import concurrent.futures
                def _api_check_one(idx):
                    if self._stop_evt.is_set():
                        return idx, None
                    url = urls[idx]
                    item_id = _extract_mercari_item_id(url)
                    if not item_id:
                        return idx, None
                    return idx, _check_mercari_api(item_id, api_session, dpop_sign)

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, cfg.max_concurrent * 2)) as pool:
                    futures = {pool.submit(_api_check_one, idx): idx for idx in remaining_indices}
                    for future in concurrent.futures.as_completed(futures):
                        if self._stop_evt.is_set():
                            for f in futures:
                                f.cancel()
                            break
                        idx, result = future.result()
                        if result is not None:
                            results[idx] = {
                                "url": urls[idx],
                                "status": result["status"],
                                "deleted_hit": result["deleted_hit"],
                                "auction_hit": False,
                                "buy_hit": result.get("buy_hit", False),
                                "sold_hit": result["sold_hit"],
                                "error_msg": "",
                            }
                            api_indices_done.append(idx)
                            api_done += 1
                        else:
                            api_fail += 1

                        if (api_done + api_fail) % 100 == 0:
                            self._set_counts(cached_count + api_done, total)
                            self._set_progress(f"API 检测中 {api_done + api_fail}/{len(remaining_indices)}…")

                # 移除已完成的索引
                done_set = set(api_indices_done)
                remaining_indices = [i for i in remaining_indices if i not in done_set]
                self._set_counts(cached_count + api_done, total)

                self.log(f"[API模式] 完成：成功 {api_done}，失败 {api_fail}，剩余 {len(remaining_indices)} 条")

                # API 失败的标记为未知
                if remaining_indices:
                    for idx in remaining_indices:
                        if results[idx] is None:
                            results[idx] = {
                                "url": urls[idx],
                                "status": "未知",
                                "deleted_hit": False,
                                "auction_hit": False,
                                "buy_hit": False,
                                "sold_hit": False,
                                "error_msg": "API check failed",
                            }
                    remaining_indices = []  # 不走浏览器

            except Exception as e:
                self.log(f"[API模式] 初始化失败：{e}")

        def _is_driver_closed_error(msg: str) -> bool:
            m = (msg or "").lower()
            return (
                "connection closed while reading from the driver" in m
                or "target closed" in m
                or "browser has been closed" in m
                or "browser closed" in m
                or "playwright connection closed" in m
            )

        async def process_batch(batch_indices: List[int], attempt: int):
            """packable 思路：每批重新開 browser/context，避免 driver 一次掉線就全軍覆沒。"""
            results_dict: Dict[int, Dict[str, Any]] = {}
            retry_penalty: List[int] = []
            retry_no_penalty: List[int] = []

            driver_dead = False

            async with async_playwright() as p:
                browser = None
                last_err = None
                for kwargs in (
                    {"headless": cfg.headless, "channel": "chrome"},
                    {"headless": cfg.headless, "channel": "msedge"},
                    {"headless": cfg.headless},
                ):
                    try:
                        browser = await p.chromium.launch(**kwargs)
                        break
                    except Exception as e:
                        last_err = e

                if browser is None:
                    raise last_err  # type: ignore[misc]

                context = await browser.new_context(locale="ja-JP")
                await apply_runtime_normalization_async(context)
                await context.route("**/*", _route_handler)

                sem = asyncio.Semaphore(cfg.max_concurrent)
                driver_dead_evt = asyncio.Event()

                async def check_one(global_idx: int):
                    # 若已要求停止，直接取消
                    if self._stop_evt.is_set() or driver_dead_evt.is_set():
                        raise asyncio.CancelledError()

                    async with sem:
                        # 拿到信號量後再檢查一次，避免用已死的 browser 開頁面
                        if driver_dead_evt.is_set():
                            raise asyncio.CancelledError()
                        url = urls[global_idx]
                        page = None
                        try:
                            page = await context.new_page()
                            import time as _time
                            _t0 = _time.time()
                            resp = await page.goto(url, wait_until="domcontentloaded", timeout=6000)
                            _t_dom = round((_time.time() - _t0) * 1000)

                            # 智能等待：輪詢 main 內文字長度，內容渲染後才抓取
                            # 提前判定：刪除/售完頁面文字少但已可判定，無需等到50字
                            _EARLY_KEYWORDS = ("該当する商品は削除されて", "お探しのページは見つかりませんでした",
                                               "売り切れ", "この商品は売り切れました")
                            _poll_count = 0
                            _main_len = 0
                            _empty_streak = 0
                            for _poll in range(40):          # 最多 40×250ms = 10s
                                _poll_count = _poll + 1
                                try:
                                    _txt = await page.inner_text("main")
                                    _main_len = len(_txt)
                                    if _main_len > 50:       # 有實質內容了
                                        break
                                    if _main_len > 5 and any(k in _txt for k in _EARLY_KEYWORDS):
                                        break                # 刪除/售完，提前結束
                                    if _main_len == 0:
                                        _empty_streak += 1
                                    else:
                                        _empty_streak = 0
                                    # main 連續20輪為空(5s)，大概率是錯誤頁面，提前退出
                                    if _empty_streak >= 20:
                                        break
                                except Exception:
                                    # main 元素不存在也算空
                                    _empty_streak += 1
                                    if _empty_streak >= 20:
                                        break
                                await page.wait_for_timeout(250)
                            _t_ready = round((_time.time() - _t0) * 1000)

                            # main 持續為空 → 錯誤頁面（upstream error 等），直接標記重試
                            if _empty_streak >= 5 and _main_len == 0:
                                self.log(f"[DEBUG] {url[-12:]} main為空 | poll={_poll_count} dom={_t_dom}ms ready={_t_ready}ms → 標記重試")
                                return global_idx, {
                                    "url": url,
                                    "status": "請求錯誤",
                                    "deleted_hit": False,
                                    "auction_hit": False,
                                    "buy_hit": False,
                                    "sold_hit": False,
                                    "error_msg": "main empty (server error page)",
                                }, True, False

                            status_code = None
                            try:
                                status_code = resp.status if resp else None
                            except Exception:
                                pass
                            if status_code is not None and status_code >= 500:
                                self.log(f"[DEBUG] {url[-12:]} HTTP {status_code} | dom={_t_dom}ms")
                                return global_idx, {
                                    "url": url,
                                    "status": "請求錯誤",
                                    "deleted_hit": False,
                                    "auction_hit": False,
                                    "buy_hit": False,
                                    "sold_hit": False,
                                    "error_msg": f"http {status_code}",
                                }, True, False

                            try:
                                text = await page.inner_text("main")
                            except Exception:
                                try:
                                    text = await page.inner_text("body")
                                except Exception:
                                    text = await page.content()

                            info = _classify_from_text(text)

                            # CDN 二次验证：网页显示"已删除"时，检查图片是否仍在 CDN
                            if info["status"] == "刪除或不存在":
                                _item_id = _extract_mercari_item_id(url)
                                if _item_id:
                                    try:
                                        _img_alive = await _cdn_image_exists(_item_id)
                                    except Exception:
                                        _img_alive = False
                                    if _img_alive:
                                        info["status"] = "可能在售(網頁不可見)"
                                        self.log(f"[CDN] {url[-12:]} 網頁顯示已刪除，但CDN圖片仍存在 → 可能在售")

                            row = {
                                "url": url,
                                "status": info["status"],
                                "deleted_hit": bool(info["deleted_hit"]),
                                "auction_hit": bool(info["auction_hit"]),
                                "buy_hit": bool(info["buy_hit"]),
                                "sold_hit": bool(info["sold_hit"]),
                                "error_msg": "",
                            }

                            should_retry = False
                            if info["status"] == "未知":
                                row["error_msg"] = "status=未知，自動標記重試"
                                should_retry = True
                                # 未知時輸出詳細調試信息
                                _preview = text[:80].replace('\n', ' ') if text else "(empty)"
                                self.log(f"[DEBUG-未知] {url[-12:]} poll={_poll_count} mainLen={_main_len} dom={_t_dom}ms ready={_t_ready}ms text=[{_preview}]")
                            else:
                                self.log(f"[DEBUG] {url[-12:]} → {info['status']} | poll={_poll_count} mainLen={_main_len} dom={_t_dom}ms ready={_t_ready}ms")

                            return global_idx, row, should_retry, False

                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            msg = str(e)
                            # driver 掉線：這一批直接中止，讓外層下一批重開 browser 重試（不消耗 retries）
                            if _is_driver_closed_error(msg):
                                driver_dead_evt.set()
                                return global_idx, {
                                    "url": url,
                                    "status": "請求錯誤",
                                    "deleted_hit": False,
                                    "auction_hit": False,
                                    "buy_hit": False,
                                    "sold_hit": False,
                                    "error_msg": f"driver closed: {msg}",
                                }, True, True

                            return global_idx, {
                                "url": url,
                                "status": "請求錯誤",
                                "deleted_hit": False,
                                "auction_hit": False,
                                "buy_hit": False,
                                "sold_hit": False,
                                "error_msg": f"goto/content error: {msg}",
                            }, True, False

                        finally:
                            if page is not None:
                                try:
                                    await page.close()
                                except Exception:
                                    pass

                tasks = [asyncio.create_task(check_one(idx)) for idx in batch_indices]
                pending = set(tasks)

                def _collect_done(done_set):
                    for t in done_set:
                        if t.cancelled():
                            continue
                        try:
                            global_idx, row, should_retry, no_penalty = t.result()
                            results_dict[global_idx] = row
                            if should_retry:
                                if no_penalty:
                                    retry_no_penalty.append(global_idx)
                                else:
                                    retry_penalty.append(global_idx)
                        except asyncio.CancelledError:
                            continue
                        except Exception as e:
                            self.log(f"任務崩潰：{e}")

                # 用 asyncio.wait + timeout 讓「停止」可以在 0.2s 內生效
                while pending:
                    if self._stop_evt.is_set():
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        break

                    # driver 掉線：不立即取消全部，給正在執行的任務 2 秒收尾
                    if driver_dead_evt.is_set():
                        try:
                            done, pending = await asyncio.wait(pending, timeout=2.0)
                            _collect_done(done)
                        except Exception:
                            pass
                        for t in pending:
                            t.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        break

                    done, pending = await asyncio.wait(
                        pending,
                        timeout=0.2,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    _collect_done(done)

                # 关闭浏览器时抑制 Playwright 的 Call log 噪音
                _orig_stderr = sys.stderr
                try:
                    sys.stderr = open(os.devnull, "w")
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
                try:
                    sys.stderr.close()
                except Exception:
                    pass
                sys.stderr = _orig_stderr

                driver_dead = driver_dead_evt.is_set()

            return results_dict, retry_penalty, retry_no_penalty, driver_dead

        attempt = 1
        while remaining_indices and not self._stop_evt.is_set():
            batch_indices = remaining_indices[: cfg.batch_size]
            completed_before = total - len(remaining_indices)

            self._set_progress(f"第 {attempt} 輪：處理 {len(batch_indices)} 條（剩餘 {len(remaining_indices)}）…")

            try:
                batch_results, retry_penalty, retry_no_penalty, driver_dead = await process_batch(batch_indices, attempt)
            except Exception as e:
                # browser 啟動失敗等：整批視作無結果，稍後重試（不消耗 retries）
                self.log(f"批次啟動/執行失敗：{e}")
                batch_results, retry_penalty, retry_no_penalty, driver_dead = {}, [], batch_indices.copy(), True

            retry_penalty_set = set(retry_penalty)
            retry_no_penalty_set = set(retry_no_penalty)

            # 記錄本輪統計
            got_results = len(batch_results)
            no_penalty_count = len(retry_no_penalty)
            penalty_count = len(retry_penalty)
            if driver_dead:
                self.log(f"第 {attempt} 輪：瀏覽器崩潰，已收回 {got_results}/{len(batch_indices)} 條結果"
                         f"（{no_penalty_count} 條需無懲罰重試）")

            finished_this_round: List[int] = []
            for idx in batch_indices:
                row = batch_results.get(idx)

                if row is not None:
                    # 覆蓋寫入（保持 packable 行為：重試成功會蓋掉前面的錯誤）
                    results[idx] = row

                # row 缺失或被 driver 中斷（no_penalty）：保持在 remaining，下輪重試
                if (row is None) or (idx in retry_no_penalty_set):
                    continue

                if idx in retry_penalty_set:
                    retries[idx] += 1
                    if retries[idx] >= cfg.max_retries:
                        finished_this_round.append(idx)
                    else:
                        # keep in remaining
                        pass
                else:
                    finished_this_round.append(idx)

            # 移除已完成
            if finished_this_round:
                finished_set = set(finished_this_round)
                remaining_indices = [i for i in remaining_indices if i not in finished_set]

            completed_now = total - len(remaining_indices)
            # O(1) 更新計數（避免 sum 掃全表卡死）
            self._set_counts(completed_now, total)

            # ── 每批結束後輸出狀態統計 ──
            from collections import Counter as _Counter
            batch_statuses = [batch_results[i]["status"] for i in batch_indices if i in batch_results and batch_results[i].get("status")]
            if batch_statuses:
                sc = _Counter(batch_statuses)
                parts = [f"{s}:{n}" for s, n in sc.most_common()]
                self.log(f"第{attempt}輪 [{len(batch_statuses)}條] {' | '.join(parts)}  (累計 {completed_now}/{total})")

            # 保存缓存（每批完成后）
            for idx in finished_this_round:
                r = results[idx]
                if r and r.get("status"):
                    st = r["status"]
                    if st not in ("請求錯誤", "未檢測到"):
                        mc_cache[urls[idx]] = st
            try:
                with open(cache_path, "w", encoding="utf-8") as cf:
                    json.dump(mc_cache, cf, ensure_ascii=False)
            except Exception:
                pass

            if cfg.auto_save_every > 0 and completed_now - last_saved_completed >= cfg.auto_save_every:
                self._set_progress(f"自動保存中…（已完成 {completed_now}）")
                try:
                    def _rows_iter():
                        for i, r in enumerate(results):
                            row = r if r is not None else {
                                "url": urls[i],
                                "status": "未檢測到",
                                "deleted_hit": False,
                                "auction_hit": False,
                                "buy_hit": False,
                                "sold_hit": False,
                                "error_msg": "no result yet",
                            }
                            yield from _expand_row_by_mapping(row, mapping)
                    _save_results_to_excel(_rows_iter(), out_path)
                    last_saved_completed = completed_now
                    self.log("自動保存完成")
                except Exception as e:
                    self.log(f"自動保存失敗：{e}")

            attempt += 1
            if remaining_indices and not self._stop_evt.is_set():
                try:
                    # driver 崩潰後多等一會兒，讓系統恢復
                    wait = 3.0 if driver_dead else 1.0
                    await asyncio.sleep(wait)
                except Exception:
                    pass

        # stop or finished — 保存缓存
        try:
            with open(cache_path, "w", encoding="utf-8") as cf:
                json.dump(mc_cache, cf, ensure_ascii=False)
        except Exception:
            pass

        if self._stop_evt.is_set():
            self.log(f"已停止，缓存已保存 {len(mc_cache)} 条结果，下次启动自动续检")
            self._set_progress("已停止（下次可续检）")

        # fill empty
        def _final_rows_iter():
            for i, r in enumerate(results):
                row = r if r is not None else {
                    "url": urls[i],
                    "status": "未檢測到",
                    "deleted_hit": False,
                    "auction_hit": False,
                    "buy_hit": False,
                    "sold_hit": False,
                    "error_msg": "no result at end",
                }
                yield from _expand_row_by_mapping(row, mapping)

        try:
            _save_results_to_excel(_final_rows_iter(), out_path)
        except Exception as e:
            self.log(f"寫入 Excel 失敗：{e}")
            self._set_progress(f"寫入失敗：{e}")
            return

        self._set_counts(total - len(remaining_indices), total)

        if self._stop_evt.is_set():
            # 停止：只保存 Excel，不做拆分/下架/D1清理（数据不完整）
            self._set_progress("已停止（下次可续检）")
            return

        # === 以下仅在全部完成时执行 ===

        # 按帳號拆分 ids/（unified 调用时跳过，由外层统一处理）
        if mapping and not self.skip_post_actions:
            try:
                ids_dir = BASE_DIR / "ids"
                ids_dir.mkdir(parents=True, exist_ok=True)
                split_count = _split_by_account(results, urls, mapping, ids_dir)
                if split_count:
                    self.log(f"已按帳號拆分到 ids/ 目錄（{split_count} 個帳號）")
            except Exception as e:
                self.log(f"按帳號拆分失敗：{e}")

        # 清除缓存（unified 模式下保留，由外层决定何时清）
        if not self.skip_post_actions:
            try:
                if Path(cache_path).exists():
                    Path(cache_path).unlink()
                    self.log("[缓存] 检测完成，缓存已清除")
            except Exception:
                pass

        # D1 清理（unified 调用时跳过）
        if not self.skip_post_actions:
            self._d1_cleanup_from_excel(out_path)

        self._set_progress("完成（結果已輸出）")
        self.log(f"完成：已輸出 {out_path}")

        # 如果是重檢未知，自動合併回原始 Excel
        original_excel = getattr(self, "_recheck_original_excel", None)
        if original_excel and os.path.exists(original_excel) and original_excel != out_path:
            try:
                merged = _merge_recheck_into_original(original_excel, out_path)
                if merged > 0:
                    self.log(f"已將 {merged} 條重檢結果合併回原始 Excel")
            except Exception as e:
                self.log(f"合併重檢結果失敗：{e}")
            finally:
                self._recheck_original_excel = None

    def _d1_cleanup_from_excel(self, excel_path: str):
        """从检测结果 Excel 中收集非在售 barcode，调用 D1 删除。"""
        import requests as _req
        from core.doc_upload_feature import DEFAULT_WORKER_URL, DEFAULT_UPLOAD_TOKEN

        non_active_statuses = ("已售完", "刪除或不存在", "拍賣中（入札受付中）", "拍賣中（入札+可直接購買）")
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
        url_idx = headers.index("url") if "url" in headers else 0
        st_idx = headers.index("status") if "status" in headers else 1

        barcodes = []
        for row in rows[1:]:
            status = str(row[st_idx] or "").strip()
            if status in non_active_statuses:
                url = str(row[url_idx] or "").strip()
                if url:
                    barcodes.append(url)

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
