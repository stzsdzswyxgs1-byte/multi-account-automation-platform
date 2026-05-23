from __future__ import annotations
"""Yahoo 拍賣『自動刊登』功能（獨立文件，方便維護）

你需求的核心：
1) 讀取指定資料夾內的 Excel（可多檔=多帳號），逐行自動刊登
2) 以『現有監控功能使用的帳號瀏覽器(Profile)』進行操作（避免影響登入）
3) 刊登成功後抓取『商品編碼』回寫 Excel；失敗則記錄原因並繼續下一筆
4) 同時支援多帳號並發（預設上限 3，可在 UI 設置）
5) 刊登_toggle：執行期間自動暫停該帳號監控(HOLD)，完成後自動恢復

注意：Yahoo 介面偶爾會改版，本模組對 selector 做了多重 fallback，
若仍失敗會自動保存 debug 截圖，方便你快速定位修改。
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import random
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, List, Optional, Callable, Set, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

import openpyxl
import requests
from .client_runtime_compat import async_playwright, PwTimeoutError, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args, _USING_PATCHRIGHT

from core.profile_lock import detect_chrome_profile_in_use, try_acquire, release
from core.cookie_store import load_raw_cookies, load_from_chrome_sqlite_yahoo, save_cookie_cache
from core.publish_http_ops import (
    create_publish_session as _http_create_session,
    fetch_publish_page as _http_fetch_page,
    extract_publish_config as _http_extract_config,
    upload_images as _http_upload_images,
    submit_merchandise as _http_submit_merchandise,
    _build_merchandise as _http_build_merchandise,
    match_location as _http_match_location,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLISH_DIR = ROOT_DIR / "publish_excels"  # 你要求：像 ids 一樣專門放刊登用 Excel
PUBLISH_DIR.mkdir(parents=True, exist_ok=True)


# ─── 403037 跨账号 VPN 故障熔断 ─────────────────────────────────────
# Yahoo API 错误 403037 = "Operation is not allowed" — 多半是 VPN 异常导致 IP 不是台湾
# 行为：单账号遇到 1 次 → 停止该账号；60 秒内 ≥2 个不同账号都遇到 → 全局熔断 + TG 告警
import collections as _collections_403
import threading as _threading_403
_VPN_ERROR_HISTORY: "_collections_403.deque[Tuple[str, float]]" = _collections_403.deque(maxlen=200)
_VPN_DOWN_FLAG = _threading_403.Event()
_VPN_DOWN_REASON = ""
_VPN_LOCK = _threading_403.Lock()


def report_vpn_error(account_name: str, error_msg: str) -> Tuple[bool, str]:
    """记一次 403037 事件。返回 (是否触发全局熔断, 触发原因)。
    60 秒内 ≥2 个不同账号都报 403037 时熔断，并把 _VPN_DOWN_FLAG.set()。
    """
    with _VPN_LOCK:
        now = time.time()
        _VPN_ERROR_HISTORY.append((account_name, now))
        cutoff = now - 60.0
        recent = [(a, t) for (a, t) in _VPN_ERROR_HISTORY if t >= cutoff]
        distinct_accs = {a for a, _ in recent}
        if len(distinct_accs) >= 2 and not _VPN_DOWN_FLAG.is_set():
            global _VPN_DOWN_REASON
            _VPN_DOWN_REASON = f"60s 内 {len(distinct_accs)} 个账号触发 403037: {', '.join(sorted(distinct_accs)[:5])}"
            _VPN_DOWN_FLAG.set()
            return True, _VPN_DOWN_REASON
    return False, ""


def is_vpn_down() -> bool:
    return _VPN_DOWN_FLAG.is_set()


def get_vpn_down_reason() -> str:
    return _VPN_DOWN_REASON


def clear_vpn_down() -> None:
    """重置熔断（用户重启刊登 / 切换 VPN 后调）"""
    with _VPN_LOCK:
        global _VPN_DOWN_REASON
        _VPN_DOWN_REASON = ""
        _VPN_DOWN_FLAG.clear()
        _VPN_ERROR_HISTORY.clear()

# ---------- 分类白名单 ----------
_ALLOWED_CAT_IDS: Set[str] = set()
_allowed_cat_path = ROOT_DIR / "allowed_categories.json"
if _allowed_cat_path.exists():
    try:
        import json as _json_tmp
        with open(_allowed_cat_path, "r", encoding="utf-8") as _f:
            _allowed_data = _json_tmp.load(_f)
        _ALLOWED_CAT_IDS = set(str(k) for k in _allowed_data.keys())
    except Exception:
        pass

# ---------- 詳細日誌（寫入文件，方便排查） ----------
PUBLISH_LOG_DIR = ROOT_DIR / "publish_logs"
PUBLISH_LOG_DIR.mkdir(parents=True, exist_ok=True)

_pub_logger = logging.getLogger("auto_publish")
_pub_logger.setLevel(logging.DEBUG)
_pub_logger.propagate = False
if not _pub_logger.handlers:
    _log_file = PUBLISH_LOG_DIR / f"publish_{datetime.now().strftime('%Y%m%d')}.log"
    _fh = logging.FileHandler(str(_log_file), encoding="utf-8")
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    _pub_logger.addHandler(_fh)


def _plog(msg: str, level: str = "info") -> None:
    """寫入 publish 詳細日誌文件。"""
    getattr(_pub_logger, level, _pub_logger.info)(msg)


# -------------------------- Excel --------------------------

RE_SPLIT_PICTURES = re.compile(r"\s*\|\s*")

# 零寬空格、BOM、零寬連接符等不可見 Unicode 字符
RE_INVISIBLE_CHARS = re.compile(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2060\u180e]')


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""  # bool 是 int 子类，必须先判断
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))  # 20452.0 → "20452"
        return str(v)
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def _clean_path(p: str) -> str:
    """清除路径中的不可见 Unicode 字符（零宽空格等）。
    如果清除后文件不存在但原始路径存在，則保留原始路径。
    額外處理：採集插件有時把特殊字符（如 ・）替換成 ?，這裡做模糊匹配修復。"""
    cleaned = RE_INVISIBLE_CHARS.sub('', p).strip()
    if os.path.exists(cleaned):
        return cleaned
    raw = p.strip()
    if raw != cleaned and os.path.exists(raw):
        return raw
    # 嘗試在目錄裡模糊匹配（文件名去掉不可見字符後比對）
    try:
        parent = os.path.dirname(cleaned)
        base = os.path.basename(cleaned)
        if os.path.isdir(parent):
            for f in os.listdir(parent):
                if RE_INVISIBLE_CHARS.sub('', f).strip() == base:
                    return os.path.join(parent, f)
    except Exception:
        pass
    # 路徑含 ? → 採集插件把特殊字符替換成了 ?，逐級目錄模糊匹配
    if '?' in cleaned:
        resolved = _fuzzy_resolve_path(cleaned)
        if resolved:
            return resolved
    return cleaned


def _fuzzy_resolve_path(path_str: str) -> str:
    """逐級解析含 ? 的路徑，在每級目錄中模糊匹配（? 匹配任意單字符）。"""
    import fnmatch
    parts = path_str.replace('/', os.sep).split(os.sep)
    resolved = ""
    for i, part in enumerate(parts):
        if not part:
            continue
        # 處理盤符（如 C:）
        if i == 0 and len(part) == 2 and part[1] == ':':
            resolved = part + os.sep
            continue
        candidate = os.path.join(resolved, part)
        if os.path.exists(candidate):
            resolved = candidate
            continue
        if '?' not in part:
            return ""  # 不含 ? 卻不存在，無法修復
        # 用 fnmatch 在父目錄中匹配
        if not resolved or not os.path.isdir(resolved):
            return ""
        matched = None
        for entry in os.listdir(resolved):
            if fnmatch.fnmatch(entry, part):
                matched = entry
                break
        if not matched:
            return ""
        resolved = os.path.join(resolved, matched)
    return resolved if os.path.exists(resolved) else ""


def _fix_title_from_pics(title: str, pics: List[str]) -> str:
    """採集插件把特殊字符（如 ・）替換成 ? ，標題也受影響。
    利用已解析的圖片路徑中的資料夾名（含正確字符）來修復標題中的 ? 。
    優先用資料夾名還原，否則直接去掉 ? 。"""
    if '?' not in title or not pics:
        return title
    # 從第一張圖片路徑提取資料夾名（倒數第二級）
    first = pics[0].replace('/', os.sep)
    parts = first.split(os.sep)
    if len(parts) < 2:
        return title
    folder_name = parts[-2]  # 圖片所在資料夾名 = 商品原始標題
    # 用 fnmatch 驗證：title 當 pattern（? 匹配任意單字符），folder_name 當目標
    import fnmatch
    if fnmatch.fnmatch(folder_name, title):
        return folder_name
    # 退而求其次：直接把 ? 去掉（至少不會顯示亂碼）
    cleaned = title.replace('?', '')
    if cleaned:
        return cleaned
    return title


def _split_picture_paths(cell_value: str) -> List[str]:
    s = _norm_str(cell_value)
    if not s:
        return []
    parts = [_clean_path(p) for p in RE_SPLIT_PICTURES.split(s) if p.strip()]
    # 最多 10 張
    return parts[:10]


def _copy_login_profile(src: Path, dst: Path) -> None:
    """只复制 cookies 等登录必需文件到轻量临时 profile（约1MB）。
    使用 robust 逐文件复制，处理 Chrome 锁定文件的情况。"""
    import sqlite3
    dst.mkdir(parents=True, exist_ok=True)
    _failed: list[str] = []

    def _safe_copy(s, d):
        try:
            shutil.copy2(s, d)
        except Exception:
            # fallback: 二进制读写（Windows 上即使 copy2 失败，read 通常仍可用）
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_bytes(s.read_bytes())
            except Exception as e2:
                _failed.append(f"{s.name}: {e2}")

    def _safe_copy_sqlite(s, d, retries=6):
        """用 SQLite backup API 安全复制，带重试（监控巡检几秒就结束，等一下就能拿到锁）"""
        for attempt in range(retries):
            src_conn = None
            dst_conn = None
            try:
                src_conn = sqlite3.connect(str(s), timeout=5)
                dst_conn = sqlite3.connect(str(d))
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
                return
            except Exception:
                # 关闭连接，避免资源泄露和文件锁残留
                for _c in (dst_conn, src_conn):
                    if _c:
                        try:
                            _c.close()
                        except Exception:
                            pass
                # 清理可能创建的空/损坏文件
                try:
                    if d.exists():
                        d.unlink()
                except Exception:
                    pass
                try:
                    shutil.copy2(s, d)
                    return
                except Exception:
                    pass
                try:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    d.write_bytes(s.read_bytes())
                    return
                except Exception:
                    if attempt < retries - 1:
                        time.sleep(2.5)
        _failed.append(f"{s.name}: 重试{retries}次仍无法复制")

    def _robust_copytree(s: Path, d: Path):
        """逐文件复制目录树，跳过 LOCK 文件，对锁定文件用 read_bytes fallback。"""
        for dirpath, dirnames, filenames in os.walk(s):
            dp = Path(dirpath)
            rel = dp.relative_to(s)
            (d / rel).mkdir(parents=True, exist_ok=True)
            for fn in filenames:
                if fn == "LOCK":
                    continue  # LevelDB LOCK 文件不需要复制
                _safe_copy(dp / fn, d / rel / fn)

    ls = src / "Local State"
    if ls.exists():
        _safe_copy(ls, dst / "Local State")
    fr = src / "First Run"
    if fr.exists():
        _safe_copy(fr, dst / "First Run")
    else:
        (dst / "First Run").touch()
    sd = src / "Default"
    dd = dst / "Default"
    dd.mkdir(exist_ok=True)
    pf = sd / "Preferences"
    if pf.exists():
        _safe_copy(pf, dd / "Preferences")
    # Network（含 Cookies）— Cookies 用 SQLite backup 安全复制
    net_src = sd / "Network"
    if net_src.is_dir():
        net_dst = dd / "Network"
        net_dst.mkdir(exist_ok=True)
        for f in net_src.iterdir():
            if f.name == "Cookies":
                _safe_copy_sqlite(f, net_dst / f.name)
            elif f.name == "Cookies-journal":
                pass
            else:
                _safe_copy(f, net_dst / f.name)
    # 不复制 Local Storage / IndexedDB / Service Worker
    # 这些缓存数据可能包含陈旧的 JS bundle 或 state，导致 Yahoo React hydration 失败
    # 只保留 Cookie 即可登录，让 Chrome 每次从零初始化页面状态
    if _failed:
        logging.warning("[PROFILE COPY] 部分文件复制失败: %s", "; ".join(_failed[:10]))
        # Cookies 是关键文件，失败则直接抛异常阻止启动
        # 用 startswith 精确匹配，避免 Cookies-wal/Cookies-shm 误触发
        if any(f.startswith("Cookies:") for f in _failed):
            raise RuntimeError(f"Cookies 复制失败: {'; '.join(_failed[:5])}")


def _verify_cookies(tmp_profile: Path) -> int:
    """验证临时 profile 的 Yahoo cookie 数量，返回数量（-1 表示读取失败）"""
    import sqlite3
    ck = tmp_profile / "Default" / "Network" / "Cookies"
    if not ck.exists():
        return -1
    try:
        conn = sqlite3.connect(str(ck), timeout=5)
        n = conn.execute("SELECT count(*) FROM cookies WHERE host_key LIKE '%yahoo%'").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# CSV / XLS → XLSX 自动转换
# ---------------------------------------------------------------------------

def _convert_csv_to_xlsx(csv_path: Path) -> Path:
    """将 CSV 转换为 XLSX，返回生成的 xlsx 路径。"""
    import csv as csv_mod

    xlsx_path = csv_path.with_suffix(".xlsx")
    raw = csv_path.read_bytes()

    # 自动检测编码
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "cp950", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise ValueError(f"CSV 编码无法识别: {csv_path.name}")

    wb = openpyxl.Workbook()
    ws = wb.active
    reader = csv_mod.reader(text.splitlines())
    for r_idx, row in enumerate(reader, 1):
        for c_idx, val in enumerate(row, 1):
            # 纯数字字段写为数值（避免文本型数字问题）
            if val and val.strip().isdigit():
                try:
                    val = int(val.strip())
                except (ValueError, OverflowError):
                    pass
            ws.cell(row=r_idx, column=c_idx, value=val)
    wb.save(str(xlsx_path))
    return xlsx_path


def _convert_xls_to_xlsx(xls_path: Path) -> Path:
    """将旧版 .xls 转换为 XLSX，返回生成的 xlsx 路径。"""
    import xlrd

    xlsx_path = xls_path.with_suffix(".xlsx")
    xls_wb = xlrd.open_workbook(str(xls_path))
    xls_ws = xls_wb.sheet_by_index(0)

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(xls_ws.nrows):
        for c in range(xls_ws.ncols):
            ws.cell(row=r + 1, column=c + 1, value=xls_ws.cell_value(r, c))
    wb.save(str(xlsx_path))
    return xlsx_path


def _ensure_xlsx(path: Path) -> Optional[Path]:
    """如果是 CSV/XLS，自动转换为 XLSX 并返回；已是 XLSX 则直接返回。
    转换失败返回 None。"""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return path
    xlsx_candidate = path.with_suffix(".xlsx")
    # 如果同名 xlsx 已存在且比源文件新，跳过转换
    if xlsx_candidate.exists() and xlsx_candidate.stat().st_mtime >= path.stat().st_mtime:
        return xlsx_candidate
    try:
        if suffix == ".csv":
            return _convert_csv_to_xlsx(path)
        elif suffix == ".xls":
            return _convert_xls_to_xlsx(path)
    except Exception as e:
        _pub_logger.warning(f"转换 {path.name} 失败: {e}")
    return None


# ---------------------------------------------------------------------------
# Excel 拆分功能：把 test.xlsx 按帳號+數量拆分成多個 {account}.xlsx
# ---------------------------------------------------------------------------

def split_excel(
    source: Path,
    assignments: List[Tuple[str, int]],
    dest_dir: Path | None = None,
) -> Dict[str, str]:
    """將 *source* Excel 按 assignments 拆分。

    Parameters
    ----------
    source : Path
        來源 Excel（通常是 test.xlsx）。
    assignments : list[(account_name, count)]
        每個帳號分配多少條。
    dest_dir : Path | None
        輸出目錄，預設與 source 同目錄。

    Returns
    -------
    dict  {account_name: 輸出檔路徑}
        拆分結果。若某帳號分配 0 條則跳過。

    Side Effects
    ------------
    - 產生 {account}.xlsx 到 dest_dir
    - source 會被覆寫，只保留剩餘未分配的行
    """
    if dest_dir is None:
        dest_dir = source.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.load_workbook(source)
    ws = wb.active

    # 讀表頭
    max_col = ws.max_column
    header_vals = []
    for c in range(1, max_col + 1):
        header_vals.append(ws.cell(row=1, column=c).value)

    # 收集所有資料行（row 2 ~ max_row）
    all_rows = []
    for r in range(2, ws.max_row + 1):
        row_data = []
        for c in range(1, max_col + 1):
            row_data.append(ws.cell(row=r, column=c).value)
        # 跳過全空行
        if any(v is not None and str(v).strip() for v in row_data):
            all_rows.append(row_data)

    cursor = 0
    result = {}

    for account, count in assignments:
        if count <= 0:
            continue
        # 取出本帳號的行
        chunk = all_rows[cursor: cursor + count]
        if not chunk:
            break
        cursor += len(chunk)

        # 如果目標檔已存在，追加到末尾
        out_path = dest_dir / f"{account}.xlsx"
        if out_path.exists():
            dst_wb = openpyxl.load_workbook(out_path)
            dst_ws = dst_wb.active
        else:
            dst_wb = openpyxl.Workbook()
            dst_ws = dst_wb.active
            for ci, hv in enumerate(header_vals, start=1):
                dst_ws.cell(row=1, column=ci, value=hv)

        # 找到拍賣類別列的索引（用于写出时强制数值化）
        _cat_col_idx = None
        for _ci, _hv in enumerate(header_vals):
            if _hv and "拍賣類別" in str(_hv):
                _cat_col_idx = _ci
                break

        start_row = dst_ws.max_row + 1
        for ri, row_data in enumerate(chunk):
            for ci, val in enumerate(row_data, start=1):
                # 拍賣類別列：强制转数值（防止文本型数字传播）
                if ci - 1 == _cat_col_idx and val is not None:
                    try:
                        val = int(float(str(val).strip().lstrip("'")))
                    except (ValueError, TypeError):
                        pass
                dst_ws.cell(row=start_row + ri, column=ci, value=val)

        dst_wb.save(out_path)
        result[account] = str(out_path)

    # 把剩餘行寫回 source
    remaining = all_rows[cursor:]
    # 寫入剩餘行（覆蓋原位置）
    for ri, row_data in enumerate(remaining):
        for ci, val in enumerate(row_data, start=1):
            ws.cell(row=2 + ri, column=ci, value=val)
    # 刪除多餘的空行（不只是設 None，要真正刪掉行，否則 max_row 不減）
    last_data_row = 1 + len(remaining)  # row 1 = header
    if ws.max_row > last_data_row:
        ws.delete_rows(last_data_row + 1, ws.max_row - last_data_row)
    wb.save(source)

    return result


def calculate_auto_distribution(
    accounts_data: List[Tuple[str, Optional[int], bool]],
    total_items: int,
    limit_total: int,
    limit_per_account: int = 0,
) -> Tuple[List[Tuple[str, int]], dict]:
    """計算平均分配方案"""
    caps = []
    failed_count = 0

    for name, current, is_checked in accounts_data:
        if not is_checked:
            continue
        if current is None:
            caps.append((name, 0))
            failed_count += 1
            continue
        available = limit_total - current
        if available <= 0:
            caps.append((name, 0))
            continue
        cap = min(limit_per_account, available) if limit_per_account > 0 else available
        caps.append((name, cap))

    assignments = []
    remaining = total_items
    for name, cap in caps:
        actual = min(cap, remaining)
        assignments.append((name, actual))
        remaining -= actual

    stats = {
        "success": len([c for _, c in assignments if c > 0]),
        "zero": len([c for _, c in assignments if c == 0]),
        "failed": failed_count,
        "assigned_total": total_items - remaining,
        "remaining_test": remaining,
        "insufficient": sum(c for _, c in caps) > total_items,
    }
    return assignments, stats


def _load_rows_from_excel(xlsx_path: Path, *, retry_pending_review: bool = False) -> Tuple[openpyxl.Workbook, openpyxl.worksheet.worksheet.Worksheet, Dict[str, int], List[int]]:
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active
    headers = {}
    for c in range(1, ws.max_column + 1):
        v = _norm_str(ws.cell(row=1, column=c).value)
        if v:
            headers[v] = c

    # 必要欄位（你描述的對應）
    # ※ 2026-01-12：你回报「商品簡述」这一步会卡住/误触，因此先把它改为『可选欄位』。
    #   若 Excel 里有就读取，但流程默认不输入（直接跳到分类）。
    required = ["圖片", "標題", "拍賣類別名稱", "商品狀況", "所在地", "說明", "起標價", "數量"]
    miss = [k for k in required if k not in headers]
    if miss:
        raise ValueError(f"Excel 缺少欄位: {', '.join(miss)}")

    # 商品編號：如果不存在就自動新增一欄
    if "商品編號" not in headers:
        ws.cell(row=1, column=ws.max_column + 1, value="商品編號")
        headers["商品編號"] = ws.max_column

    if "刊登狀態" not in headers:
        ws.cell(row=1, column=ws.max_column + 1, value="刊登狀態")
        headers["刊登狀態"] = ws.max_column

    if "刊登錯誤" not in headers:
        ws.cell(row=1, column=ws.max_column + 1, value="刊登錯誤")
        headers["刊登錯誤"] = ws.max_column

    if "實際分類" not in headers:
        ws.cell(row=1, column=ws.max_column + 1, value="實際分類")
        headers["實際分類"] = ws.max_column

    # 需要處理的 row index（跳過已經有商品編號的）
    # v10: 若刊登狀態=『待人工核對』，默認跳過（避免重跑造成重複刊登）
    todo_rows: List[int] = []
    for r in range(2, ws.max_row + 1):
        code = _norm_str(ws.cell(row=r, column=headers["商品編號"]).value)
        title = _norm_str(ws.cell(row=r, column=headers["標題"]).value)
        status = _norm_str(ws.cell(row=r, column=headers["刊登狀態"]).value)

        if not title:
            continue
        if code:
            continue
        if (not retry_pending_review) and status == "待人工核對":
            continue

        todo_rows.append(r)
    return wb, ws, headers, todo_rows


def _save_wb(wb: openpyxl.Workbook, xlsx_path: Path) -> None:
    """更耐用的保存逻辑（修复：已抓到商品編號但回寫/收錄失败）。

    背景：
    - Windows 上若目标 xlsx 正被 Excel/杀软占用，os.replace/Path.replace 会 PermissionError。
    - 旧逻辑会把这个异常抛到外层，导致『刊登其實成功，但被標記為失败/收錄失败』。

    行为（不改变正常情况下的功能）：
    - 仍然优先原子覆盖保存到同一個 xlsx。
    - 若覆盖失败：把最新版本写到同目录的 *.pending.xlsx，等下一次保存时再自动回填覆盖。
      （这样不会因为"文件占用"而把刊登结果判成失败，也不会丢数据）
    """
    xlsx_path = Path(xlsx_path)
    pending = xlsx_path.with_suffix(".pending.xlsx")

    # 1) 若存在 pending，先尝试回填（文件不再占用时可自动恢复）
    try:
        if pending.exists():
            pending.replace(xlsx_path)
    except PermissionError:
        pass
    except Exception:
        pass

    # 2) 保存到唯一临时文件（避免 .tmp.xlsx 被杀软短暂占用导致保存失败）
    ts = time.strftime("%Y%m%d_%H%M%S")
    tmp = xlsx_path.with_suffix(f".tmp_{ts}.xlsx")
    wb.save(tmp)

    # 3) 尝试原子覆盖；若目标被占用则写入 pending 并返回（不抛异常）
    try:
        tmp.replace(xlsx_path)
        # 覆盖成功后，尽量清理 pending
        try:
            if pending.exists():
                pending.unlink()
        except Exception:
            pass
    except PermissionError:
        # 最新版本留在 pending，等待下一次保存回填
        try:
            if pending.exists():
                pending.unlink()
        except Exception:
            pass
        try:
            tmp.replace(pending)
        except Exception:
            # 兜底：保留 tmp，不让异常冒泡
            pass
    finally:
        # 若 tmp 仍存在，尽量清理
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass



# -------------------------- Playwright helpers --------------------------

def _ts() -> str:
    return time.strftime("%H:%M:%S")

# -------------------------- Humanize（純視覺 / 高延遲 / 抖動） --------------------------

@dataclass(frozen=True)
class HumanizeConfig:
    """只改『節奏/視覺行為』，不改功能。

    重要：此設定會透過 ContextVar 綁定到每個並發任務，避免多帳號互相污染。
    """
    enabled: bool = False

    # 每一步（step_delay）額外抖動：base ± jitter（秒）
    step_jitter: float = 0.0

    # 每個動作（click/fill/type）前後小停頓（秒）
    action_pre_delay_min: float = 0.0
    action_pre_delay_max: float = 0.0
    action_post_delay_min: float = 0.0
    action_post_delay_max: float = 0.0

    # 點擊按住時間（ms）：mousedowm -> mouseup
    click_hold_ms_min: int = 0
    click_hold_ms_max: int = 0

    # 打字每字延遲（ms）
    type_delay_ms_min: int = 0
    type_delay_ms_max: int = 0

    # 偶爾長停頓（像人看畫面/確認）
    long_pause_prob: float = 0.0  # 0~1
    long_pause_min: float = 0.0
    long_pause_max: float = 0.0

    # 微動作（更視覺）：注意，滾輪可能影響定位，默認建議 0
    micro_move_prob: float = 0.0
    micro_scroll_prob: float = 0.0

    # 小字串才用「打字」，長文本仍用 fill（避免太慢 / 卡死）
    type_max_chars: int = 120


_HUMANIZE_CFG: ContextVar[HumanizeConfig] = ContextVar("_HUMANIZE_CFG", default=HumanizeConfig())


def _get_hcfg() -> HumanizeConfig:
    try:
        return _HUMANIZE_CFG.get()
    except Exception:
        return HumanizeConfig()


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def _rand_float(a: float, b: float) -> float:
    try:
        a = float(a)
        b = float(b)
    except Exception:
        return 0.0
    if b < a:
        a, b = b, a
    return random.uniform(a, b)


def _rand_int(a: int, b: int, default: int = 0) -> int:
    try:
        a = int(a)
        b = int(b)
    except Exception:
        return int(default)
    if b < a:
        a, b = b, a
    return random.randint(a, b)


async def _human_pre_delay() -> None:
    cfg = _get_hcfg()
    if not cfg.enabled:
        return
    d = _rand_float(cfg.action_pre_delay_min, cfg.action_pre_delay_max)
    if d > 0:
        await asyncio.sleep(d)


async def _human_post_delay() -> None:
    cfg = _get_hcfg()
    if not cfg.enabled:
        return
    d = _rand_float(cfg.action_post_delay_min, cfg.action_post_delay_max)
    if d > 0:
        await asyncio.sleep(d)


def _click_delay_ms(default_ms: int = 0) -> int:
    cfg = _get_hcfg()
    if not cfg.enabled:
        return int(default_ms or 0)
    ms = _rand_int(cfg.click_hold_ms_min, cfg.click_hold_ms_max, default=int(default_ms or 0))
    return max(0, int(ms))


def _type_delay_ms(default_ms: int = 0) -> int:
    cfg = _get_hcfg()
    if not cfg.enabled:
        return int(default_ms or 0)
    ms = _rand_int(cfg.type_delay_ms_min, cfg.type_delay_ms_max, default=int(default_ms or 0))
    return max(0, int(ms))


async def _human_mouse_click(page, x: float, y: float) -> None:
    """座標點擊（關閉遮罩等），加入少量抖動/按住時間。"""
    cfg = _get_hcfg()
    if not cfg.enabled:
        await page.mouse.click(x, y)
        return

    await _human_pre_delay()
    # 小幅抖動（像手抖）
    j = 2.0
    try:
        x2 = float(x) + random.uniform(-j, j)
        y2 = float(y) + random.uniform(-j, j)
    except Exception:
        x2, y2 = x, y
    try:
        await page.mouse.click(x2, y2, delay=_click_delay_ms(30))
    except Exception:
        await page.mouse.click(x, y, delay=_click_delay_ms(30))
    await _human_post_delay()


async def _human_fill_locator(loc, value: str) -> None:
    """把 value 寫入 locator：人類化時，小字串用 type，長字串用 fill。"""
    value = _norm_str(value)
    if value == "":
        return

    cfg = _get_hcfg()
    if (not cfg.enabled) or (cfg.type_delay_ms_max <= 0) or (len(value) > int(cfg.type_max_chars or 120)):
        # 保持原本行為（最快/最穩）
        await loc.fill(value)
        return

    await _human_pre_delay()
    # 先聚焦
    try:
        await loc.click(timeout=6000)
    except Exception:
        pass

    # 盡量清空（不依賴 fill，避免瞬移）
    cleared = False
    try:
        await loc.press("Control+A")
        await loc.press("Backspace")
        cleared = True
    except Exception:
        pass
    if not cleared:
        try:
            await loc.fill("")
        except Exception:
            pass

    await loc.type(value, delay=_type_delay_ms(20))
    await _human_post_delay()


async def _safe_click(loc, timeout_ms: int = 8000, force: bool = True, **_kw) -> None:
    """click + 人類化節奏（不改功能）"""
    cfg = _get_hcfg()
    if cfg.enabled:
        await _human_pre_delay()
    try:
        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass

    if not cfg.enabled:
        await loc.click(timeout=timeout_ms, force=force)
        return

    d = _click_delay_ms(30)
    # 先用更像真人的方式（force=False），失敗再回退到原本的 force 設定
    try:
        await loc.click(timeout=timeout_ms, force=False, delay=d)
    except Exception:
        await loc.click(timeout=timeout_ms, force=force, delay=d)

    await _human_post_delay()


async def _maybe_click_first(page, selectors: List[str], timeout_ms: int = 3000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await _safe_click(loc.first, timeout_ms=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _wait_any_visible(page, selectors: List[str], timeout_ms: int = 12000) -> Optional[str]:
    t0 = time.time()
    while time.time() - t0 < (timeout_ms / 1000.0):
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return sel
            except Exception:
                pass
        await asyncio.sleep(0.25)
    return None


async def _fill_by_label(page, label_text: str, value: str, *, prefer_textarea: bool = False) -> None:
    value = _norm_str(value)
    if value == "":
        return

    # 1) 先用 placeholder（偶爾能一次命中）
    try:
        ph = page.get_by_placeholder(label_text)
        if await ph.count() > 0:
            await _human_fill_locator(ph.first, value)
            return
    except Exception:
        pass

    # 1.5) Playwright get_by_label（利用 HTML label/aria-label 關聯）
    try:
        lbl = page.get_by_label(label_text, exact=False)
        if await lbl.count() > 0:
            el = lbl.first
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea", "select"):
                await _human_fill_locator(el, value)
                return
    except Exception:
        pass

    # 2) label -> 後續第一個 input/textarea
    #    改進：用兩層 XPath 策略
    #    a) 優先找「最小葉子節點」（自身文字精確包含 label，但子元素不包含）
    #       這樣不會匹配到大容器（如整個銷售資訊區塊），避免 following::input 指向錯誤欄位
    #    b) 回退到原來的寬鬆匹配
    leaf_xpath = (
        f"xpath=//*[contains(normalize-space(text()), '{label_text}')]"
    )
    broad_xpath = (
        f"xpath=//*[contains(normalize-space(.), '{label_text}')]"
    )

    for base_xp in [leaf_xpath, broad_xpath]:
        xpath_targets = []
        if prefer_textarea:
            xpath_targets.append(f"{base_xp}/following::textarea[1]")
            xpath_targets.append(f"{base_xp}/following::input[1]")
        else:
            xpath_targets.append(f"{base_xp}/following::input[1]")
            xpath_targets.append(f"{base_xp}/following::textarea[1]")

        for xp in xpath_targets:
            try:
                loc = page.locator(xp)
                if await loc.count() > 0:
                    await _human_fill_locator(loc.first, value)
                    return
            except Exception:
                continue

    # 3) contenteditable / role=textbox（Yahoo 的「描述」多為富文本編輯器，非 textarea）
    try:
        x_label = f"xpath=//*[contains(normalize-space(.), '{label_text.strip()}')][1]"
        label_loc = page.locator(x_label).first
        editable = label_loc.locator("xpath=following::*[@contenteditable='true' or @role='textbox'][1]")
        if await editable.count() > 0:
            await _safe_click(editable.first, timeout_ms=6000, force=False)
            await _human_fill_locator(editable.first, value)
            return
    except Exception:
        pass

    # 特判：Jodit 編輯器（描述欄常見）
    if label_text.strip() in ("描述", "商品描述"):
        try:
            jodit = page.locator("div.jodit-wysiwyg[contenteditable='true'], div.jodit-wysiwyg[role='textbox']").first
            if await jodit.count() > 0:
                await _safe_click(jodit, timeout_ms=6000, force=False)
                await _human_fill_locator(jodit, value)
                return
        except Exception:
            pass

    raise RuntimeError(f"找不到輸入框：{label_text}")


async def _check_title_illegal(page) -> Optional[str]:
    """填完標題後，檢查 Yahoo 是否顯示「內容不合法」等驗證錯誤。

    返回錯誤文字（如有），否則返回 None。
    """
    # 等一下讓驗證觸發
    await page.wait_for_timeout(600)

    # Yahoo 會在標題輸入框附近顯示紅色錯誤提示
    error_patterns = [
        "內容不合法",
        "内容不合法",
        "標題不合法",
        "标题不合法",
        "含有違規",
        "含有违规",
        "輸入資料內容格式有誤",
        "输入资料内容格式有误",
    ]
    for pat in error_patterns:
        try:
            loc = page.get_by_text(pat, exact=False)
            if await loc.count() > 0:
                el = loc.first
                if await el.is_visible():
                    txt = (await el.text_content() or "").strip()
                    return txt or pat
        except Exception:
            continue
    return None


async def _find_input_near_label(page, label_text: str):
    """用 JS 找到 label 文字後面最近的 input（按 DOM 順序，非容器內搜索）。

    策略：
    1. 用 TreeWalker 找到包含 label_text 的最小文字節點
    2. 過濾掉「大容器」（innerText 長度遠超 label 本身的元素）
    3. 從該文字節點開始，按 DOM 順序往後遍歷，找到第一個 input
    """
    js = """(labelText) => {
        // 1) 找所有包含 labelText 的文字節點
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null);
        let bestNode = null;
        let bestLen = Infinity;
        while (walker.nextNode()) {
            const t = walker.currentNode;
            const txt = t.textContent.trim();
            if (txt.includes(labelText) && txt.length < bestLen) {
                // 選最短的文字節點（最精確的 label）
                bestNode = t;
                bestLen = txt.length;
            }
        }
        if (!bestNode) return null;

        // 2) 從這個文字節點的父元素開始，用 DOM 順序找下一個 input
        //    關鍵：不用 querySelector（會找容器內第一個），而是按文檔順序遍歷
        const labelEl = bestNode.parentElement;
        if (!labelEl) return null;

        // 用 document.createTreeWalker 從 labelEl 之後遍歷所有元素
        const allWalker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_ELEMENT, null);
        // 先定位到 labelEl
        let found = false;
        while (allWalker.nextNode()) {
            if (allWalker.currentNode === labelEl) {
                found = true;
                break;
            }
        }
        if (!found) return null;

        // 繼續往後走，找第一個 input
        while (allWalker.nextNode()) {
            const el = allWalker.currentNode;
            const tag = el.tagName.toLowerCase();
            if (tag === 'input') {
                const tp = (el.type || '').toLowerCase();
                if (tp === '' || tp === 'text' || tp === 'number' || tp === 'tel') {
                    return el;
                }
            }
        }
        return null;
    }"""
    handle = await page.evaluate_handle(js, label_text)
    if handle:
        try:
            tag = await handle.evaluate("el => el ? el.tagName : null")
            if tag:
                return handle.as_element()
        except Exception:
            pass
    return None


async def _fill_price_and_qty(page, price: str, qty: str, account_name: str, app_log) -> None:
    """專門處理定價和商品數量 — 直接用 HTML 屬性精確定位，不依賴 label 文字查找。

    根據 Yahoo 拍賣頁面的實際 DOM：
    - 定價 input: name="item-price", max="3000000"
    - 商品數量 input: 在 <p> 包含 "商品數量" 的 row-set 裡, max="9999"
    """
    price = _norm_str(price)
    qty = _norm_str(qty)
    _plog(f"[{account_name}] _fill_price_and_qty 開始: price={price!r}, qty={qty!r}")

    # --- 定價：用 name 屬性直接定位 ---
    price_loc = None
    if price:
        # 優先用 name 屬性（最精確）
        price_loc = page.locator('input[name="item-price"]')
        cnt = await price_loc.count()
        _plog(f"[{account_name}] 定價 input[name=item-price] count={cnt}")
        if cnt > 0:
            await _human_fill_locator(price_loc.first, price)
            _plog(f"[{account_name}] 定價 → {price}")
        else:
            # fallback
            _plog(f"[{account_name}] 定價 fallback 到 _fill_by_label")
            await _fill_by_label(page, "定價", price)
        await page.wait_for_timeout(400)

    # --- 商品數量：用 row-set 結構定位 ---
    qty_loc = None
    if qty:
        # 方法1: 找包含 "商品數量" 文字的 row-set，取其中的 input
        qty_row = page.locator('.row-set:has(p.row-set__subject:text("商品數量")) input')
        cnt = await qty_row.count()
        _plog(f"[{account_name}] 商品數量 via row-set selector count={cnt}")
        if cnt > 0:
            qty_loc = qty_row.first
        else:
            # 方法2: 用 max=9999 的 number input（商品數量的特徵）
            qty_max = page.locator('input[type="number"][max="9999"]')
            cnt2 = await qty_max.count()
            _plog(f"[{account_name}] 商品數量 via max=9999 count={cnt2}")
            if cnt2 > 0:
                qty_loc = qty_max.first
        if qty_loc:
            await _human_fill_locator(qty_loc, qty)
            _plog(f"[{account_name}] 商品數量 → {qty}")
        else:
            _plog(f"[{account_name}] 商品數量 fallback 到 _fill_by_label")
            await _fill_by_label(page, "商品數量", qty)
        await page.wait_for_timeout(400)

    # --- 校驗 ---
    try:
        if price:
            p_loc = page.locator('input[name="item-price"]')
            if await p_loc.count() > 0:
                cur_p = await p_loc.first.input_value()
                _plog(f"[{account_name}] 校驗定價: 當前={cur_p!r}, 期望={price!r}")
                if cur_p.strip() != price:
                    _plog(f"[{account_name}] 定價被覆蓋({cur_p}→{price})，重新填寫", "warning")
                    _plog(f"[{account_name}] 定價被覆蓋，重新填寫", "warning")
                    await _human_fill_locator(p_loc.first, price)
        if qty and qty_loc:
            cur_q = await qty_loc.input_value()
            _plog(f"[{account_name}] 校驗數量: 當前={cur_q!r}, 期望={qty!r}")
            if cur_q.strip() != qty:
                _plog(f"[{account_name}] 數量被覆蓋({cur_q}→{qty})，重新填寫", "warning")
                _plog(f"[{account_name}] 數量被覆蓋，重新填寫", "warning")
                await _human_fill_locator(qty_loc, qty)
    except Exception as e:
        _plog(f"[{account_name}] 校驗定價/數量異常: {e}", "error")
    _plog(f"[{account_name}] _fill_price_and_qty 完成")




async def _dismiss_autocomplete_overlay(page: Page) -> None:
    """用于关闭「商品简述」等输入框触发的下拉提示，避免遮挡后续步骤。"""
    # 1) blur 当前焦点（最稳）
    try:
        await page.evaluate("""() => {
            const el = document.activeElement;
            if (el && typeof el.blur === 'function') el.blur();
        }""")
    except Exception:
        pass

    # 2) ESC 关闭可能的下拉
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    # 3) 点一下页面空白/标题（触发关闭）
    for sel in ("text=商品資訊", "text=新增 直購商品", "text=新增直購商品"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=800)
                break
        except Exception:
            continue
    else:
        try:
            await _human_mouse_click(page, 40, 40)
        except Exception:
            pass

    try:
        await page.wait_for_timeout(150)
    except Exception:
        pass

async def _fill_description_richtext(page: Page, value: str, tracer: "Tracer" = None) -> None:
    """
    Yahoo 直購頁的「描述」是 Jodit 富文本（contenteditable），不是 input/textarea。
    用更穩的方式定位並寫入，避免誤點把「分類」彈窗又打開。
    """
    if value is None:
        value = ""
    # NaN 之類
    try:
        if isinstance(value, float) and value != value:
            value = ""
    except Exception:
        pass
    value = str(value)

    # 可能有殘留遮罩/彈窗，先按一次 ESC
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    except Exception:
        pass

    # 先用「描述」附近的容器縮小搜尋範圍
    scope = page
    try:
        label = page.locator("xpath=(//*[normalize-space()='描述' or starts-with(normalize-space(.),'描述')])[1]")
        if await label.count():
            container = label.locator(
                "xpath=ancestor::div[.//div[contains(@class,'jodit-wysiwyg') and @contenteditable='true']][1]"
            )
            if await container.count():
                scope = container
    except Exception:
        pass

    selectors = [
        "css=div.jodit-wysiwyg[contenteditable='true']",
        "css=div.jodit-wysiwyg[contenteditable='true'][role='textbox']",
        "css=div[contenteditable='true'][class*='jodit']",
        "css=div[class*='jodit'][contenteditable='true']",
        "css=div[contenteditable='true'][data-jodit]",
    ]

    async def _try_fill(loc: Locator) -> bool:
        if await loc.count() == 0:
            return False
        el = loc.first
        try:
            await el.scroll_into_view_if_needed()
        except Exception:
            pass

        # 先試 Playwright fill（支援 contenteditable）
        try:
            await el.click(timeout=2000)
            await el.fill(value, timeout=4000)
            try:
                await el.evaluate(
                    "(node) => { node.dispatchEvent(new Event('input',{bubbles:true})); node.dispatchEvent(new Event('change',{bubbles:true})); }"
                )
            except Exception:
                pass
            return True
        except Exception:
            pass

        # 保底：JS 直接塞文字（保留換行）
        try:
            await el.click(timeout=2000)
        except Exception:
            pass
        try:
            await el.evaluate(
                """(node, v) => {
                    node.focus();
                    node.innerHTML = '';
                    const s = String(v).replace(/\\r\\n/g,'\\n').replace(/\\r/g,'\\n');
                    const parts = s.split('\\n');
                    for (let i = 0; i < parts.length; i++) {
                        node.appendChild(document.createTextNode(parts[i]));
                        if (i < parts.length - 1) node.appendChild(document.createElement('br'));
                    }
                    node.dispatchEvent(new Event('input', { bubbles: true }));
                    node.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            return True
        except Exception:
            return False

    for sel in selectors:
        if await _try_fill(scope.locator(sel)):
            return

    for sel in selectors:
        if await _try_fill(page.locator(sel)):
            return

    raise RuntimeError("找不到輸入框：描述")


async def _dismiss_popups(page) -> None:
    """尽量收起可能遮挡点击的浮层（例如『標籤』搜尋下拉等）。"""
    # ESC 通常能关闭下拉、提示框、弹窗（不会影响主流程）
    for _ in range(3):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.wait_for_timeout(80)
        except Exception:
            pass

    # 轻点页面空白处（避免某些站点 ESC 不生效）
    try:
        await _human_mouse_click(page, 10, 10)
    except Exception:
        pass


async def _pick_dropdown_value(page, label_text: str, value_text: str) -> None:
    value_text = _norm_str(value_text)
    if not value_text:
        return

    # 先嘗試：label 後第一個可點容器（常見為 div/combobox）
    candidates = [
        f"xpath=//*[contains(normalize-space(.), '{label_text}')]/following::*[self::div or self::span or self::input][1]",
        f"xpath=//*[contains(normalize-space(.), '{label_text}')]/following::input[1]",
    ]

    opened = False
    for xp in candidates:
        try:
            loc = page.locator(xp)
            if await loc.count() > 0:
                await _safe_click(loc.first, timeout_ms=6000)
                opened = True
                break
        except Exception:
            continue

    if not opened:
        # 最後兜底：直接點 value_text（如果已經有列表）
        pass

    # 選項：role=option/列表項/純文字
    option_selectors = [
        f"[role='option'] >> text={value_text}",
        f"[role='listbox'] >> text={value_text}",
        f"li:has-text('{value_text}')",
        f"div:has-text('{value_text}')",
        f"text={value_text}",
    ]
    for sel in option_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await _safe_click(loc.first, timeout_ms=8000)
                return
        except Exception:
            continue

    raise RuntimeError(f"下拉選擇失敗：{label_text} -> {value_text}")


def _map_condition_to_radio_text(condition: str) -> str:
    """把 Excel 里的『商品狀況/狀態』文本映射到页面的两项：全新品 / 二手品。"""
    c = (condition or "").strip()
    if not c:
        return ""
    c2 = c.replace(" ", "")
    # 常见来源：Mercari/闲鱼/煤炉抓取会给出『全新、未使用』『未使用』『近新』等
    if any(k in c2 for k in ["全新品", "新品", "全新", "未使用", "未拆封", "近新", "全新未使用"]):
        return "全新品"
    if any(k in c2 for k in ["二手", "中古", "使用", "有使用", "有痕", "旧", "瑕疵"]):
        return "二手品"
    return c


# v6.0.50: 大類自動加「收藏品」標籤
# user 要求:商品分類大類為「古董、藝術與礦石」或「偶像、球員卡與郵幣」時,
# 二手品的「item-label」自動填「收藏品」(原本默認「無」)
_COLLECTION_TOP_CATS = ("古董、藝術與礦石", "偶像、球員卡與郵幣", "居家、家具與園藝")

# cat_id → top_category 映射(lazy load 自 category_tree_full.json)
# 用於 Excel「拍賣類別」是純數字 cat_id 場景(實際 user 用法是純數字)
_COLLECTION_CAT_IDS_CACHE: Optional[set] = None


def _load_collection_cat_ids() -> set:
    """讀 category_tree_full.json,回 古董/偶像 大類下所有 cat_id 的 set。

    cached(只讀 1 次)。失敗回空 set(等於 fallback 到中文路徑判斷)。
    """
    global _COLLECTION_CAT_IDS_CACHE
    if _COLLECTION_CAT_IDS_CACHE is not None:
        return _COLLECTION_CAT_IDS_CACHE
    out = set()
    try:
        p = ROOT_DIR / "category_tree_full.json"
        if p.exists():
            import json as _json
            d = _json.loads(p.read_text(encoding="utf-8"))
            for cid, entry in (d or {}).items():
                path = ((entry or {}).get("path") or "").strip()
                if not path:
                    continue
                top = path.split(">", 1)[0].strip()
                if top in _COLLECTION_TOP_CATS:
                    out.add(str(cid))
    except Exception:
        pass
    _COLLECTION_CAT_IDS_CACHE = out
    return out


def _is_collection_top_category(cat_kw: str) -> bool:
    """判斷 Excel「拍賣類別」是否屬於「古董、藝術與礦石」/「偶像、球員卡與郵幣」大類。

    支援兩種格式:
    - 純數字 cat_id(實際 user 慣例,如 "2092101364")→ 查 category_tree_full.json
    - 中文路徑(如 "古董、藝術與礦石 > 銅雕")→ 看 head 段
    """
    if not cat_kw:
        return False
    s = str(cat_kw).strip().replace(" ", "")
    if not s:
        return False

    # case 1: 純數字 cat_id → 查預載 set
    if s.isdigit():
        return s in _load_collection_cat_ids()

    # case 2: 中文路徑 → 檢查 head 段
    head = s.split(">", 1)[0]
    for top in _COLLECTION_TOP_CATS:
        if head == top.replace(" ", "") or head.startswith(top.replace(" ", "")):
            return True
    return False


def _build_item_labels(cat_kw: str, use_status: str) -> list:
    """構建 Yahoo merchandise.labels 欄位。

    schema(從 Yahoo bundle 反編譯確認):
      - 全新品 → []
      - 二手品 + 「無」標籤 → []
      - 二手品 + 收藏品 → [{"label": "收藏品"}]

    目前自動填規則:大類為古董/偶像 + 二手品 → 收藏品。
    """
    if use_status != "used":
        return []
    if _is_collection_top_category(cat_kw):
        return [{"label": "收藏品"}]
    return []


# Yahoo merchandise.hashtags 規則(從 yecTagsCollector bundle 反編譯):
#   - 不能含空格、不能含 #
#   - 最多 5 個
#   - 每個最長 16 字元(中英文都算 1)
#   - 同一商品不能重複
# Excel「標籤」欄目前用空格分隔,兼容逗號/頓號/豎線。
def _parse_hashtags(raw: str) -> List[str]:
    if not raw:
        return []
    candidates = re.split(r'[\s,，、|]+', str(raw).strip())
    cleaned: List[str] = []
    seen = set()
    for tag in candidates:
        tag = tag.replace('#', '').replace('　', '').strip()
        if not tag or tag in seen:
            continue
        if len(tag) > 16:
            tag = tag[:16]
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= 5:
            break
    return cleaned


async def _set_condition_radio(page, condition: str) -> None:
    """设置『狀態』单选。"""
    opt = _map_condition_to_radio_text(condition)
    if not opt:
        return

    # 先限定在『狀態』附近找，避免误点到其它文字
    near = page.locator(
        "xpath=//*[contains(normalize-space(.), '狀態')]/ancestor::div[1]"
    ).first
    try:
        if await near.count() > 0:
            cand = near.locator(f"text={opt}").first
            if await cand.count() > 0:
                await _safe_click(cand, timeout_ms=8000, name=f"狀態:{opt}")
                return
    except Exception:
        pass

    # 兜底：全页找
    cand2 = page.locator(f"text={opt}").first
    if await cand2.count() > 0:
        await _safe_click(cand2, timeout_ms=8000, name=f"狀態:{opt}")
        return

    raise RuntimeError(f"找不到『狀態』選項：{opt}（原始：{condition}）")


async def _set_location_dropdown(page, location_text: str) -> None:
    """设置『所在地區』下拉。

    你提供的截图显示该控件为原生 <select>（浏览器自带下拉列表，可滚动）。
    对这种控件，Playwright 用 select_option 直接选择最稳定，也不需要滚动到可见位置。

    台↔臺 变体处理：Excel 可能写 "臺北市" 但 Yahoo 下拉是 "台北市"（或反过来），
    先试原文，失败后自动换 台↔臺 再试一次。
    """
    loc = _norm_str(location_text)
    if not loc:
        return

    # 生成 台↔臺 变体
    variants = [loc]
    if "臺" in loc:
        variants.append(loc.replace("臺", "台"))
    elif "台" in loc:
        variants.append(loc.replace("台", "臺"))

    # 1) 优先：真正的 <select>
    select_xpaths = [
        "xpath=//*[contains(normalize-space(.), '所在地區')]/following::select[1]",
        "xpath=//*[contains(normalize-space(.), '所在地')]/following::select[1]",
    ]
    for xp in select_xpaths:
        try:
            sel = page.locator(xp).first
            if await sel.count() > 0:
                for v in variants:
                    try:
                        await sel.select_option(label=v)
                        return
                    except Exception:
                        pass
                    try:
                        await sel.select_option(value=v)
                        return
                    except Exception:
                        pass
        except Exception:
            pass

    # 2) 兜底：如果页面改成了自绘下拉，再回退到"点击 + 选文字"
    for v in variants:
        try:
            await _pick_dropdown_value(page, "所在地區", v)
            return
        except Exception:
            pass
    for v in variants:
        try:
            await _pick_dropdown_value(page, "所在地", v)
            return
        except Exception:
            pass



async def _ensure_direct_buy(page) -> None:
    """确保进入『直購品(buynow)』表单。

    注意：这个 publish 页「背后」可能已经渲染出表单文字，但前面仍有『競標品/直購品』两张大卡片遮罩。
    所以这里**不能**用"看见表单文字"来判断，而是以「buynow/直購品卡片是否还可见」为准：
    只要卡片还在，就持续点击『直購品』，直到卡片消失并且表单可操作。
    """

    async def _picker_visible() -> bool:
        # 最稳：卡片上有 (buynow)
        try:
            loc = page.locator("text=buynow").first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            pass

        # 次稳：同时看到「直購品」与「bid」两张卡片
        try:
            loc2 = page.locator("text=直購品").first
            if await loc2.count() > 0 and await loc2.is_visible():
                bid = page.locator("text=bid").first
                if await bid.count() > 0 and await bid.is_visible():
                    return True
        except Exception:
            pass

        return False

    async def _click_buynow() -> bool:
        # 优先点包含 buynow 的卡片
        candidates = ["buynow", "直購品", "直购品"]
        for t in candidates:
            try:
                text_loc = page.locator(f"text={t}").first
                if await text_loc.count() == 0:
                    continue
                if not await text_loc.is_visible():
                    continue

                # 1) 最近的可点祖先
                for xp in [
                    "xpath=ancestor::*[self::button or self::a or @role='button' or @tabindex='0'][1]",
                    "xpath=ancestor::div[@role='button'][1]",
                    "xpath=ancestor::div[1]",
                ]:
                    try:
                        host = text_loc.locator(xp)
                        if await host.count() > 0:
                            await _safe_click(host.first, timeout_ms=8000)
                            return True
                    except Exception:
                        continue

                # 2) bbox 兜底：直接点文字中心
                try:
                    box = await text_loc.bounding_box()
                    if box:
                        await _human_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        return True
                except Exception:
                    pass

            except Exception:
                continue
        return False

    # 多次循环：应对「点了又自动刷新回选择卡片」的情况
    _nothing_rounds = 0  # 连续「既无picker又无表单」的轮次
    _fn_start = time.time()
    _FN_TIMEOUT = 45  # 整体超时秒数，避免无限等待被120s看门狗杀掉
    for _round in range(12):
        # 整体超时检查
        if time.time() - _fn_start > _FN_TIMEOUT:
            raise RuntimeError(f"_ensure_direct_buy 超时({_FN_TIMEOUT}s)，页面可能未完整渲染")

        if await _picker_visible():
            _nothing_rounds = 0
            await _click_buynow()

            # 等待可能的刷新/切换
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            # 等卡片消失（看不见 buynow）
            try:
                await page.locator("text=buynow").first.wait_for(state="hidden", timeout=12000)
            except Exception:
                pass

            await page.wait_for_timeout(700)
            continue

        # 卡片不在了：等待关键表单元素出现（从12s缩短到5s，快速判断）
        if await _wait_any_visible(page, ["input[type='file']", "text=商品標題", "text=確定新增"], timeout_ms=5000):
            return

        # picker 和表单都不可见 — 页面可能未完整渲染(React hydration 失败)
        _nothing_rounds += 1
        if _nothing_rounds >= 1:
            # 立即 reload（从原来等3轮改为1轮，快速恢复）
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass
            _nothing_rounds = 0  # reload 后重新计数
            continue

        await page.wait_for_timeout(700)

    # 超过12轮仍未成功，抛异常触发上层重试
    raise RuntimeError("_ensure_direct_buy 12轮未成功，页面渲染异常")




# --- Category picker helpers (v11) -------------------------------------------------

def _parse_category_path(raw: str) -> list[str]:
    '''Parse Excel '拍賣類別名稱' (full path) into segments.

    Example:
        '男性精品與服飾 > 戒指 > 銀戒' -> ['男性精品與服飾', '戒指', '銀戒']
    '''
    s = _norm_str(raw)
    if not s:
        return []
    # Normalize separators
    s = s.replace('＞', '>').replace('›', '>')
    parts = [p.strip() for p in re.split(r"\s*>\s*", s) if p.strip()]
    return parts





async def _open_category_search(page, tracer=None):
    """點擊『商品分類』欄位，打開『編輯分類』彈窗。"""
    # 這個欄位在不同版本/語言下 placeholder 會略有差異
    field_candidates = [
        "[placeholder='請選擇拍賣商品分類']",
        "[placeholder='請選擇商品分類']",
        "[placeholder='请选择拍卖商品分类']",
        "[placeholder='请选择商品分类']",
        "input[placeholder*='請選擇']",
        "input[placeholder*='请选择']",
    ]

    field = None
    for sel in field_candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                field = loc
                break
        except Exception:
            continue

    # 兜底：用 label『商品分類』去抓旁邊可點的區塊
    if field is None:
        try:
            label = page.get_by_text(re.compile(r"商品分類|拍賣商品分類|拍卖商品分类"), exact=False).first
            # 往上找一層，再找 input 或可點容器
            field = label.locator("xpath=ancestor-or-self::*[1]").locator("input, [role='textbox'], [role='button']").first
        except Exception:
            field = None

    if field is None:
        raise RuntimeError("找不到『商品分類』欄位，無法打開分類彈窗")

    try:
        await field.scroll_into_view_if_needed(timeout=6000)
    except Exception:
        pass
    # 无头模式下 scroll_into_view_if_needed 可能不够，用 JS 强制滚动
    try:
        await field.evaluate("el => el.scrollIntoView({block:'center'})")
        await page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        await field.click(delay=_click_delay_ms(30))
    except Exception:
        try:
            await field.click(force=True, delay=_click_delay_ms(30))
        except Exception:
            try:
                await field.locator("xpath=.. ").click(force=True, delay=_click_delay_ms(30))
            except Exception:
                # 最终兜底：JS 派發完整滑鼠事件序列（mousedown→mouseup→click）
                await field.evaluate("""el => {
                    const opts = {bubbles:true, cancelable:true, view:window};
                    el.dispatchEvent(new MouseEvent('mousedown', opts));
                    el.dispatchEvent(new MouseEvent('mouseup', opts));
                    el.dispatchEvent(new MouseEvent('click', opts));
                    el.focus();
                    if(el.parentElement) {
                        el.parentElement.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.parentElement.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.parentElement.dispatchEvent(new MouseEvent('click', opts));
                    }
                }""")

    await page.wait_for_timeout(600)

    # 如果彈窗還沒出現，嘗試用 page.mouse 在元素座標直接點擊
    try:
        title_check = page.get_by_text(re.compile(r"編輯分類|编辑分类")).first
        if not await title_check.is_visible():
            box = await field.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await page.wait_for_timeout(400)
    except Exception:
        pass

    if tracer:
        await tracer.snap("after_click_category")

async def _locate_category_picker(page, tracer=None):
    """定位 Yahoo 拍賣分類的『編輯分類』彈窗。

    注意：Yahoo 這個分類選擇器是 `yec-category-picker` web component（多半在 shadow DOM 內）。
    舊版用 MUI 的 `.MuiDialog-root` / `role=dialog` 會抓不到，導致後續一直在捲外層頁面。
    """
    # host 元件在 light DOM，可先抓到它
    picker = page.locator("yec-category-picker").first

    # 彈窗標題：會出現在 shadow DOM 裡，但 Playwright 的 text/role selector 通常可穿透 open shadow root。
    title = page.get_by_text(re.compile(r"編輯分類|编辑分类"))
    await title.first.wait_for(state="visible", timeout=15000)

    # 有些情況 host 會晚一點才掛上（或被重新 mount），再保險等一下
    try:
        await picker.wait_for(state="attached", timeout=3000)
    except Exception:
        pass

    return picker


def _split_category_path(full_path: str):
    # 兼容多種分隔符: "A > B > C", "A->B->C", "A -> B -> C", "A＞B＞C" 等
    # 注意: -> 必須在 > 之前匹配，否則 "->" 會被拆成 "-" + ">"
    parts = [p.strip() for p in re.split(r"\s*(?:->|[>＞])\s*", (full_path or "").strip()) if p.strip()]
    return parts


async def _picker_scroll_inner_list(picker, delta: int, bounds: dict = None):
    """嘗試只捲動分類彈窗內的清單，不要去捲外層頁面。
    bounds: picker 可視區域 {x, y, width, height}，用於過濾滾動容器。
    """
    b = bounds or {}
    return await picker.evaluate(
        """(host, arg) => {
            const delta = arg.delta;
            const bx = arg.bx, by = arg.by, bw = arg.bw, bh = arg.bh;
            const hasBounds = bw > 0 && bh > 0;

            function isVisible(el){
                if(!el) return false;
                const r = el.getBoundingClientRect();
                if(r.width <= 0 || r.height <= 0) return false;
                const style = window.getComputedStyle(el);
                if(style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
                const cx = r.left + r.width/2;
                const cy = r.top + r.height/2;
                if(cx < 0 || cx > window.innerWidth || cy < 0 || cy > window.innerHeight) return false;
                // 如果有 picker 邊界，必須在範圍內
                if(hasBounds){
                    if(cx < bx - 30 || cx > bx + bw + 30) return false;
                    if(cy < by - 30 || cy > by + bh + 30) return false;
                }
                return true;
            }
            function collectDeep(root, out){
                const nodes = root.querySelectorAll('*');
                for(const el of nodes){
                    out.push(el);
                    if(el.shadowRoot) collectDeep(el.shadowRoot, out);
                }
            }
            const root = host.shadowRoot || host;
            const all = [];
            collectDeep(root, all);

            const candidates = [];
            for(const el of all){
                if(!isVisible(el)) continue;
                const st = window.getComputedStyle(el);
                const oy = st.overflowY;
                if((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 5){
                    candidates.push(el);
                }
            }
            if(!candidates.length) return { ok:false, reason:'no-scrollable' };

            candidates.sort((a,b)=>b.clientHeight - a.clientHeight);
            const target = candidates[0];
            const before = target.scrollTop;
            target.scrollTop = before + delta;
            return { ok:true, before, after: target.scrollTop, h: target.scrollHeight, ch: target.clientHeight };
        }""",
        {"delta": delta,
         "bx": b.get("x", 0), "by": b.get("y", 0),
         "bw": b.get("width", 0), "bh": b.get("height", 0)},
    )


async def _picker_click_text(picker, text_value: str, *, timeout: int = 6000):
    """在分類彈窗內點擊指定文字（精準匹配）。

    修復：Yahoo 分類彈窗經常同名（例如「其他」）在 DOM 裡出現多個節點。
    舊寫法用 `.first` 很容易抓到不可見/不可點的那個，結果就一直滾動重試，看起來像『卡住』。
    這裡改成：在所有 exact match 裡，優先挑「清單區域且更靠右（通常代表更深層那一列）」的那個，避免同名（尤其是「其他」）點到上一層；並盡量點它的可點父層。
    """
    tv = (text_value or "").strip()
    if not tv:
        raise RuntimeError("分類文字是空的，無法點擊")

    # Yahoo 部分分類名稱後面帶 @ 符號（如「西裝褲@」「足球套裝@」），
    # 但 Excel 裡通常不帶 @，所以同時嘗試原文和帶 @ 的版本
    locs = picker.get_by_text(tv, exact=True)
    locs_at = picker.get_by_text(tv + "@", exact=True) if not tv.endswith("@") else None
    deadline = time.monotonic() + (timeout / 1000.0)
    last_err = None

    while time.monotonic() < deadline:
        try:
            n = await locs.count()
            # 也統計帶 @ 的候選數量
            n_at = 0
            if locs_at is not None:
                try:
                    n_at = await locs_at.count()
                except Exception:
                    pass
            best = None
            best_key = None

            # 合併兩組候選：先原文，再帶 @ 的
            all_candidates = [(locs, i) for i in range(n)] + [(locs_at, i) for i in range(n_at)]

            for src_locs, i in all_candidates:
                cand = src_locs.nth(i)
                try:
                    if not await cand.is_visible():
                        continue

                    # 點擊可點父層（避免點到 span 文字而不觸發選中）
                    target = cand
                    try:
                        host = cand.locator(
                            "xpath=ancestor-or-self::*[self::button or self::a or @role='button' or @role='option' or @role='treeitem' or @role='menuitem' or @tabindex='0'][1]"
                        )
                        if await host.count() > 0 and await host.first.is_visible():
                            target = host.first
                    except Exception:
                        pass

                    # Yahoo 分類彈窗同名（尤其「其他」）會在多列同時出現：
                    #  - y 更靠下不一定是正確那列（上一層的「其他」可能更靠下）
                    #  - 這裡改成：優先點『更靠右（通常是更深層那一列）』且位於清單區域的那個
                    box = None
                    try:
                        box = await target.bounding_box()
                    except Exception:
                        box = None

                    if not box:
                        continue

                    x = float(box.get('x', 0) or 0)
                    y = float(box.get('y', 0) or 0)
                    w = float(box.get('width', 0) or 0)
                    right = x + w

                    # 避免點到上方 breadcrumb/標題：清單區域通常 y 會更大
                    if y < 120:
                        key = (0.0, y)  # 只當兜底
                    else:
                        key = (right, y)

                    if best_key is None or key > best_key:
                        best_key = key
                        best = target
                except Exception:
                    continue

            if best is not None:
                # 盡量把它放到視野中間（避免被底部『確定』按鈕區遮住）
                try:
                    await best.evaluate("(el)=>el.scrollIntoView({block:'center', inline:'nearest'})")
                except Exception:
                    try:
                        await best.scroll_into_view_if_needed(timeout=800)
                    except Exception:
                        pass

                try:
                    await best.click(delay=_click_delay_ms(30), timeout=timeout)
                    return
                except Exception as e:
                    last_err = e
                    # 有些節點會被認為不可點，最後再 force 一次
                    try:
                        await best.click(delay=_click_delay_ms(30), timeout=timeout, force=True)
                        return
                    except Exception as e2:
                        last_err = e2
        except Exception as e:
            last_err = e

        await asyncio.sleep(0.15)

    if last_err:
        raise last_err
    raise RuntimeError(f"在分類彈窗裡找不到可點的項：{tv}")


async def _auto_drill_until_confirm_enabled(picker, page, keyword: str, max_depth: int = 5) -> None:
    """確認按鈕 disabled 時，自動往下選子分類直到 enabled。

    Yahoo 分類彈窗要求選到末級葉子分類才能確定。
    如果 Excel 裡的路徑不夠深（如 '女包精品與女鞋 > 斜背包'），
    這裡會自動選第一個可見子項（優先選「其他」類）。
    支持滾動查找不在可視區域的項目。
    """
    confirm_sel = "button:has-text('確定'), button:has-text('确定')"

    # 記錄已點擊過的文字+位置，避免重複點同一個
    clicked_history: list = []  # [(text, x, y), ...]

    def _already_clicked(text: str, x: float, y: float) -> bool:
        """判斷是否已經點過（同文字且位置接近）"""
        for ct, cx, cy in clicked_history:
            if ct == text and abs(cx - x) < 30 and abs(cy - y) < 30:
                return True
        return False

    # 只要包含「其他」就可以選，不再硬編碼具體分類名
    drill_keyword = "其他"

    for depth in range(max_depth):
        # ── 檢查確認按鈕 ──
        btn = picker.locator(confirm_sel).first
        try:
            if await btn.count() > 0 and await btn.is_enabled():
                _plog(f"[category] 確認按鈕已啟用 (depth={depth})")
                return
        except Exception:
            pass

        _plog(f"[category] 確認按鈕仍disabled, 自動下鑽 depth={depth} keyword={keyword}")

        # ── 取得 picker bounding box ──
        # 注意: yec-category-picker 是 web component，自身 bounding box 常為 w=0 h=0
        # 需要從 shadow DOM 內部取得實際對話框的邊界
        picker_box = None
        try:
            picker_box = await picker.bounding_box()
        except Exception:
            pass

        # 如果 picker 自身尺寸為 0，嘗試從 shadow DOM 取得實際對話框邊界
        if not picker_box or picker_box.get("width", 0) < 10 or picker_box.get("height", 0) < 10:
            _plog(f"[category] picker自身邊界無效(w={picker_box.get('width',0) if picker_box else '?'} "
                  f"h={picker_box.get('height',0) if picker_box else '?'}), 嘗試取shadow DOM內部邊界")
            try:
                real_box = await picker.evaluate("""(host) => {
                    const root = host.shadowRoot || host;
                    const all = root.querySelectorAll('*');
                    let best = null;
                    let bestArea = 0;
                    for (const el of all) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 50 && r.height > 50) {
                            const area = r.width * r.height;
                            if (area > bestArea) {
                                bestArea = area;
                                best = { x: r.x, y: r.y, width: r.width, height: r.height };
                            }
                        }
                    }
                    return best;
                }""")
                if real_box and real_box.get("width", 0) > 50:
                    picker_box = real_box
                    _plog(f"[category] shadow DOM內部邊界: x={picker_box['x']:.0f} y={picker_box['y']:.0f} "
                          f"w={picker_box['width']:.0f} h={picker_box['height']:.0f}")
            except Exception as e:
                _plog(f"[category] 取shadow DOM邊界失敗: {e}")

        # 如果仍然無法取得有效邊界，設為 None 表示跳過邊界檢查
        skip_bounds = False
        if not picker_box or picker_box.get("width", 0) < 10 or picker_box.get("height", 0) < 10:
            _plog("[category] 無法取得有效picker邊界, 將跳過邊界檢查")
            skip_bounds = True
        else:
            _plog(f"[category] picker區域: x={picker_box['x']:.0f} y={picker_box['y']:.0f} "
                  f"w={picker_box['width']:.0f} h={picker_box['height']:.0f}")

        # ── DOM 快照（用於偵測點擊後是否有變化）──
        snapshot_before = ""
        try:
            snapshot_before = await picker.evaluate("el => el.innerHTML.length.toString()")
        except Exception:
            pass

        # ── 搜尋候選項（含滾動重試）──
        clicked = False
        max_scroll_attempts = 4  # 最多滾動幾次來找項目

        # 用 picker.evaluate 找到滾動容器，滾到底部，取最後幾個可見子元素的座標
        # 因為列表項在 closed shadow DOM 裡，JS 無法讀取文字，
        # 但「其他」類項目總是在列表最底部，所以滾到底點最後一個即可
        async def _scroll_bottom_get_last_children():
            """picker.evaluate: 找滾動容器 → 滾到底 → 回傳最後幾個可見子元素的座標。
            回傳 dict {items, totalChildren, ...} 或 None。"""
            pb = picker_box or {}
            return await picker.evaluate("""(host, bounds) => {
                const bx = bounds.x || 0, by = bounds.y || 0;
                const bw = bounds.w || 9999, bh = bounds.h || 9999;

                function collectDeep(root, out) {
                    const nodes = root.querySelectorAll('*');
                    for (const el of nodes) {
                        out.push(el);
                        if (el.shadowRoot) collectDeep(el.shadowRoot, out);
                    }
                }
                const root = host.shadowRoot || host;
                const all = [];
                collectDeep(root, all);

                // 找滾動容器，必須在 picker 可視區域內
                const candidates = [];
                for (const el of all) {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    if (cx < 0 || cx > window.innerWidth) continue;
                    if (cy < 0 || cy > window.innerHeight) continue;
                    // 必須在 picker 邊界內
                    if (cx < bx - 30 || cx > bx + bw + 30) continue;
                    if (cy < by - 30 || cy > by + bh + 30) continue;
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden'
                        || st.opacity === '0') continue;
                    const oy = st.overflowY;
                    if ((oy === 'auto' || oy === 'scroll')
                        && el.scrollHeight > el.clientHeight + 5) {
                        candidates.push({el, r, ch: el.clientHeight});
                    }
                }
                if (!candidates.length) return null;

                // 取 clientHeight 最大的滾動容器
                candidates.sort((a, b) => b.ch - a.ch);
                const container = candidates[0].el;
                const cRect = candidates[0].r;

                // 滾到底部
                container.scrollTop = container.scrollHeight;

                // 收集容器內可見的子元素（列表項）
                const children = container.children;
                const items = [];
                for (let i = 0; i < children.length; i++) {
                    const child = children[i];
                    const r = child.getBoundingClientRect();
                    if (r.width < 20 || r.height < 10) continue;
                    if (r.height > 80) continue;
                    const st = window.getComputedStyle(child);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    if (r.bottom < 0 || r.top > window.innerHeight) continue;
                    items.push({x: r.x, y: r.y, w: r.width, h: r.height, index: i});
                }
                if (!items.length) return null;

                const last3 = items.slice(-3);
                return {
                    items: last3,
                    totalChildren: children.length,
                    totalVisible: items.length,
                    scrollH: container.scrollHeight,
                    clientH: container.clientHeight,
                    scrollTop: container.scrollTop,
                    containerX: cRect.x, containerY: cRect.y,
                    containerW: cRect.width, containerH: cRect.height
                };
            }""", {"x": pb.get("x", 0), "y": pb.get("y", 0),
                          "w": pb.get("width", 0), "h": pb.get("height", 0)})

        def _in_picker_bounds(x, y):
            """安全檢查: 座標是否在 picker 區域內"""
            if not picker_box or picker_box.get("width", 0) < 10:
                return True  # 無邊界資訊時放行
            px, py = picker_box["x"], picker_box["y"]
            pw, ph = picker_box["width"], picker_box["height"]
            return (px - 20 <= x <= px + pw + 20 and
                    py - 20 <= y <= py + ph + 20)

        for scroll_round in range(max_scroll_attempts + 1):
            scroll_exhausted = False
            if scroll_round > 0:
                # 滾動 picker 內部列表
                _plog(f"[category] 第{scroll_round}次滾動查找子分類...")
                try:
                    ret = await _picker_scroll_inner_list(picker, 300, bounds=picker_box)
                    await page.wait_for_timeout(300)
                    if isinstance(ret, dict):
                        if not ret.get("ok"):
                            _plog(f"[category] 滾動失敗: {ret.get('reason','unknown')}, 停止滾動")
                            scroll_exhausted = True
                        elif ret.get("after") == ret.get("before"):
                            _plog(f"[category] 已滾到底部(scrollTop不變), 停止滾動")
                            scroll_exhausted = True
                        else:
                            _plog(f"[category] 滾動成功: scrollTop {ret.get('before')}->{ret.get('after')} "
                                  f"(scrollH={ret.get('h')}, clientH={ret.get('ch')})")
                except Exception as e:
                    _plog(f"[category] 滾動異常: {e}")
                    scroll_exhausted = True

            # 用模糊匹配搜尋包含「其他」的項目
            try:
                loc = picker.get_by_text(drill_keyword)
                n = await loc.count()
                if n > 0:
                    # 收集所有可見匹配項的位置
                    visible_items = []
                    # 計算列表區域起始 y（跳過頂部面包屑/標籤欄，約 80px）
                    list_top_y = (picker_box["y"] + 80) if picker_box else 0
                    for i in range(n):
                        el = loc.nth(i)
                        try:
                            if not await el.is_visible():
                                continue
                        except Exception:
                            continue
                        box = await el.bounding_box()
                        if not box or box.get("height", 0) < 5:
                            continue
                        ex, ey = box["x"], box["y"]
                        ew, eh = box["width"], box["height"]
                        # 確認在 picker 範圍內
                        if not skip_bounds and picker_box:
                            px, py = picker_box["x"], picker_box["y"]
                            pw, ph = picker_box["width"], picker_box["height"]
                            if ex < px - 5 or ex > px + pw + 5 or ey < py - 5 or ey > py + ph + 5:
                                continue
                        # 跳過頂部面包屑/標籤區域（避免點到面包屑導致回退）
                        if ey < list_top_y:
                            continue
                        # 列表項高度通常 > 30px，面包屑/標籤高度較小
                        if eh < 25:
                            continue
                        # 取得文字內容用於日誌和去重
                        try:
                            item_text = (await el.text_content() or "").strip()
                        except Exception:
                            item_text = f"其他_{i}"
                        if _already_clicked(item_text, ex, ey):
                            continue
                        visible_items.append((el, ex, ey, ew, eh, i, item_text))

                    if visible_items:
                        # 選最右邊的（最深層列）
                        visible_items.sort(key=lambda t: t[1], reverse=True)
                        best_el, bx, by, bw, bh, bi, best_text = visible_items[0]

                        _plog(f"[category] 找到含'{drill_keyword}'共{n}個, "
                              f"可點{len(visible_items)}個, "
                              f"選 {best_text!r} x={bx:.0f} y={by:.0f}")

                        await best_el.click(delay=50, timeout=3000)
                        await page.wait_for_timeout(500)
                        clicked_history.append((best_text, bx, by))
                        clicked = True

                        # 檢查點擊後 DOM 是否變化
                        snapshot_after = ""
                        try:
                            snapshot_after = await picker.evaluate(
                                "el => el.innerHTML.length.toString()")
                        except Exception:
                            pass
                        if snapshot_before and snapshot_after:
                            if snapshot_before == snapshot_after:
                                _plog(f"[category] 點擊 {best_text!r} 後DOM無變化")
                                try:
                                    if await btn.is_enabled():
                                        _plog(f"[category] 確認按鈕已啟用 (depth={depth})")
                                        return
                                except Exception:
                                    pass
                            else:
                                _plog(f"[category] 點擊 {best_text!r} 後DOM已變化")
            except Exception as e:
                _plog(f"[category] 搜尋 '{drill_keyword}' 異常: {e}")

            if clicked:
                break  # 跳出滾動循環

            # picker.get_by_text 找不到（列表項在 closed shadow DOM 裡），
            # 改用 picker.evaluate 滾到底部，點擊最後一個可見子元素
            # （「其他」類項目總是在列表最底部）
            try:
                result = await _scroll_bottom_get_last_children()
                if result and result.get("items"):
                    items = result["items"]
                    _plog(f"[category] 滾到底部, 共{result['totalChildren']}子元素, "
                          f"可見{result['totalVisible']}個, 取最後{len(items)}個")
                    # 從最後一個往前嘗試（列表最底部 = 「其他」）
                    for item in reversed(items):
                        lx, ly = item["x"], item["y"]
                        lw, lh = item["w"], item["h"]
                        cx, cy = lx + lw / 2, ly + lh / 2
                        tag = f"__bottom_{item['index']}__"
                        if _already_clicked(tag, cx, cy):
                            continue
                        if not _in_picker_bounds(cx, cy):
                            _plog(f"[category] 底部項 index={item['index']} "
                                  f"cx={cx:.0f} cy={cy:.0f} 超出picker範圍, 跳過")
                            continue
                        _plog(f"[category] 點擊列表底部項 index={item['index']} "
                              f"cx={cx:.0f} cy={cy:.0f} w={lw:.0f} h={lh:.0f}")
                        await page.mouse.click(cx, cy)
                        await page.wait_for_timeout(500)
                        clicked_history.append((tag, cx, cy))
                        clicked = True
                        break
                else:
                    _plog("[category] 滾到底部但未找到可見子元素")
            except Exception as e:
                _plog(f"[category] 滾底點擊異常: {e}")

            if clicked:
                break  # 跳出滾動循環

            # 滾動已耗盡，不再繼續
            if scroll_exhausted:
                break

        # ── 如果所有候選+滾動都沒找到，最後再試一次滾到底點最後項 ──
        if not clicked:
            _plog("[category] 所有候選名稱均未匹配, 最後嘗試滾到底部點擊最後一項...")
            try:
                result = await _scroll_bottom_get_last_children()
                if result and result.get("items"):
                    items = result["items"]
                    _plog(f"[category] 最終兜底: 共{result['totalChildren']}子元素, "
                          f"可見{result['totalVisible']}個, 取最後{len(items)}個")
                    for item in reversed(items):
                        ix, iy = item["x"], item["y"]
                        iw, ih = item["w"], item["h"]
                        cx, cy = ix + iw / 2, iy + ih / 2
                        tag = f"__final_bottom_{item['index']}__"
                        if _already_clicked(tag, cx, cy):
                            continue
                        if not _in_picker_bounds(cx, cy):
                            _plog(f"[category] 最終兜底 index={item['index']} "
                                  f"cx={cx:.0f} cy={cy:.0f} 超出picker範圍, 跳過")
                            continue
                        _plog(f"[category] 最終兜底點擊 index={item['index']} "
                              f"cx={cx:.0f} cy={cy:.0f}")
                        await page.mouse.click(cx, cy)
                        await page.wait_for_timeout(500)
                        clicked_history.append((tag, cx, cy))
                        clicked = True
                        break
                else:
                    _plog("[category] 最終兜底: 未找到可見子元素")
            except Exception as e:
                _plog(f"[category] 最終兜底異常: {e}")

        if not clicked:
            _plog(f"[category] depth={depth} 所有策略均未找到可點子分類, "
                  f"已點歷史={clicked_history}, 放棄下鑽")
            return

    _plog(f"[category] 已達最大下鑽深度{max_depth}, 已點歷史={clicked_history}, "
          f"確認按鈕可能仍disabled")


async def _picker_click_confirm(picker, page, tracer=None):
    """點擊分類彈窗右下「確定」，並且必須等彈窗關閉才算完成。

    之前常見問題：按到了但彈窗沒關、或點到非真正按鈕，導致後續步驟在彈窗遮罩下失敗。
    """
    # 嘗試拿到對應的 dialog 容器（有些按鈕在 web component 外層）
    dialog = picker.locator(
        "xpath=ancestor::*[@role='dialog' or contains(@class,'MuiDialog') or contains(@class,'modal')][1]"
    )
    if await dialog.count() > 0:
        dialog = dialog.first
    else:
        dialog = None

    candidates = []
    if dialog is not None:
        candidates.append(dialog.get_by_role("button", name=re.compile(r"^(確定|确定)$")))
        candidates.append(dialog.locator("button:has-text('確定'), button:has-text('确定')"))
    candidates.append(picker.get_by_role("button", name=re.compile(r"^(確定|确定)$")))
    candidates.append(picker.locator("button:has-text('確定'), button:has-text('确定')"))

    last_err = None
    for cand in candidates:
        if await cand.count() == 0:
            continue
        btn = cand.first
        # 有些情況要先選到末級分類才會 enable
        for _ in range(30):
            try:
                if await btn.is_enabled():
                    break
            except Exception:
                pass
            await page.wait_for_timeout(200)

        try:
            await btn.click(delay=_click_delay_ms(30), timeout=5000)
        except Exception as e:
            last_err = e
            continue

        # 點完一定要等彈窗/組件消失（否則後面「描述」會被遮罩擋住）
        closed = False
        if dialog is not None:
            try:
                await dialog.wait_for(state="hidden", timeout=6000)
                closed = True
            except Exception:
                pass
        try:
            await picker.wait_for(state="hidden", timeout=6000)
            closed = True
        except Exception:
            pass

        if closed:
            return

    # 最後保底：按 ESC 嘗試關閉
    try:
        await page.keyboard.press("Escape")
        if dialog is not None:
            await dialog.wait_for(state="hidden", timeout=3000)
            return
    except Exception:
        pass

    # 還是關不掉就直接拋錯，避免悄悄繼續導致後續一連串錯誤
    if last_err:
        raise last_err
    raise RuntimeError("分類彈窗未關閉（可能未按到『確定』或未選到末級分類）")

async def _picker_try_search_mode(picker, page, keyword: str, tracer=None) -> bool:
    """嘗試點放大鏡 -> 輸入關鍵字/完整路徑 -> 選擇結果。

    由於 Yahoo 這邊是 web component，放大鏡按鈕的 selector 可能不穩。
    所以這個函式盡量多策略，但失敗就回退到『逐層點完整路徑』。
    """
    # 1) 找放大鏡按鈕
    search_btn_candidates = [
        "button[aria-label*='搜尋']",
        "button[aria-label*='search']",
        "button[title*='搜尋']",
        "button[title*='search']",
        "[role='button'][aria-label*='搜尋']",
        "[role='button'][aria-label*='search']",
        "button:has(svg[aria-label*='search'])",
        "button:has(svg[data-icon*='search'])",
        "button:has(svg[class*='search'])",
        "button:has(i[class*='search'])",
    ]

    clicked = False
    for sel in search_btn_candidates:
        btn = picker.locator(sel).first
        try:
            await btn.wait_for(state="visible", timeout=1000)
            await btn.click(delay=_click_delay_ms(20))
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        return False

    await page.wait_for_timeout(300)

    # 2) 找輸入框
    kw_input = picker.locator(
        "input[placeholder*='關鍵字'], input[placeholder*='关键字'], input[placeholder*='keyword'], input[type='search']"
    ).first

    try:
        await kw_input.wait_for(state="visible", timeout=2500)
    except Exception:
        return False

    await kw_input.fill("")
    await kw_input.type(keyword, delay=_type_delay_ms(20))
    try:
        await kw_input.press("Enter")
    except Exception:
        pass

    await page.wait_for_timeout(500)

    # 3) 嘗試點結果（優先完整路徑，其次末級）
    parts = _split_category_path(keyword)
    candidates = []
    if parts:
        candidates.append(keyword)
        candidates.append(parts[-1])

    for t in candidates:
        try:
            # 用同一套『找可見同名項』策略，避免 `.first` 抓錯（尤其是「其他」）
            await _picker_click_text(picker, t, timeout=1500)
            await _picker_click_confirm(picker, page, tracer)
            return True
        except Exception:
            continue

    return False


async def _select_category_by_keyword(page, keyword: str, tracer=None) -> str:
    """選擇 Yahoo 拍賣分類。回傳實際選中的分類路徑字串。

    Excel 的「拍賣類別名稱」永遠是完整路徑：A > B > C。

    修復重點：分類彈窗是 web component（yec-category-picker），不能再用 MUI dialog selector。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise RuntimeError("Excel 的『拍賣類別名稱』是空的，無法選擇分類")

    # 打開分類彈窗（點『請選擇拍賣商品分類』那個欄位），最多重試3次
    picker = None
    for _cat_try in range(3):
        if _cat_try == 2:
            # 最後一次重試前刷新頁面，解決彈窗 DOM 存在但 hidden 的問題
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        await _open_category_search(page, tracer)
        try:
            picker = await _locate_category_picker(page, tracer)
            break
        except Exception:
            if _cat_try == 2:
                raise
            await page.wait_for_timeout(800)
    if tracer:
        await tracer.snap("category_picker_open")

    # 先嘗試『放大鏡搜尋』模式（如果 selector 抓不到就回退）
    try:
        ok = await _picker_try_search_mode(picker, page, keyword, tracer)
        if ok:
            if tracer:
                await tracer.snap("category_selected_search")
            return keyword  # 搜尋模式精確匹配，直接回傳原路徑
    except Exception:
        # 不讓搜尋模式的問題影響後續 fallback
        pass

    # fallback：按完整路徑逐層點
    parts = _split_category_path(keyword)
    if not parts:
        raise RuntimeError(f"無法解析分類路徑：{keyword!r}")

    # 逐層點擊（如果某層找不到，會嘗試在彈窗內捲動幾次）
    for idx, part in enumerate(parts):
        found = False
        for _ in range(18):
            try:
                await _picker_click_text(picker, part, timeout=2500)
                found = True
                break
            except Exception:
                # 往下捲動彈窗內清單
                try:
                    ret = await _picker_scroll_inner_list(picker, 420)
                    await page.wait_for_timeout(200)
                    # 已經捲到底（scrollTop 不再變化）就不要再反覆等 18 次了，避免看起來像『卡住』
                    if isinstance(ret, dict) and ret.get("ok") and ret.get("after") == ret.get("before"):
                        break
                except Exception:
                    await page.wait_for_timeout(200)

        if not found:
            raise RuntimeError(f"分類彈窗內找不到：{part}（完整路徑：{keyword}）")

        # 非最後一層，等下一層渲染一下
        if idx < len(parts) - 1:
            await page.wait_for_timeout(350)

    # 點完所有層後，確認按鈕可能仍 disabled（Yahoo 要求選到末級葉子分類）
    # 如果 disabled，自動往下選子分類直到 enabled
    await page.wait_for_timeout(400)
    await _auto_drill_until_confirm_enabled(picker, page, keyword)

    # 點確定前先檢查按鈕是否已啟用，避免無意義的 5s 超時等待
    confirm_sel = "button:has-text('確定'), button:has-text('确定')"
    pre_btn = picker.locator(confirm_sel).first
    try:
        if await pre_btn.count() > 0 and not await pre_btn.is_enabled():
            _plog(f"[category] 自動下鑽後確認按鈕仍disabled, 關閉picker並跳過", "warning")
            # 嘗試關閉 picker（點 X 或按 Escape）
            try:
                close_btn = picker.locator("button[value='cancel'], button:has-text('✕'), button:has-text('×')").first
                if await close_btn.count() > 0:
                    await close_btn.click(timeout=2000)
                else:
                    await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
            raise RuntimeError(f"分類下鑽失敗：確認按鈕仍disabled（{keyword}）")
    except RuntimeError:
        raise
    except Exception:
        pass

    # 點確定
    await _picker_click_confirm(picker, page, tracer)

    if tracer:
        await tracer.snap("category_selected_path")

    # 等彈窗關閉（不強制，避免偶發卡住）
    try:
        title = page.get_by_text(re.compile(r"編輯分類|编辑分类")).first
        await title.wait_for(state="hidden", timeout=8000)
    except Exception:
        pass

    # 彈窗關閉後，從頁面上的分類輸入框讀取實際選中的分類
    actual_category = ""
    try:
        for sel in [
            "[placeholder='請選擇拍賣商品分類']",
            "[placeholder='請選擇商品分類']",
            "input[placeholder*='請選擇']",
        ]:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                val = await loc.input_value()
                if val and val.strip():
                    actual_category = val.strip()
                    break
    except Exception as e:
        _plog(f"[category] 讀取實際分類失敗: {e}")

    if actual_category:
        _plog(f"[category] 實際選中分類: {actual_category}")

    return actual_category

async def _upload_images(page, image_paths: List[str], upload_wait_s: float = 6.0) -> None:
    if not image_paths:
        return
    # 主匹配：accept 含 image
    selectors = [
        "input[type='file'][accept*='image' i]",
        "input[type='file']",
    ]
    loc = None
    for sel in selectors:
        try:
            l = page.locator(sel)
            if await l.count() > 0:
                loc = l.first
                break
        except Exception:
            continue
    if loc is None:
        # 有時表單還在渲染（或 ensure_direct_buy 因為看到文字就提前返回），先等一下再找一次
        try:
            await page.wait_for_selector("input[type='file']", timeout=12000)
        except Exception:
            pass

        for sel in selectors:
            try:
                l = page.locator(sel)
                if await l.count() > 0:
                    loc = l.first
                    break
            except Exception:
                continue

    if loc is None:
        raise RuntimeError("找不到圖片上傳 input[type=file]")

    # 檢查檔案存在
    missing = [p for p in image_paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError("圖片文件不存在: " + "; ".join(missing[:3]) + ("..." if len(missing) > 3 else ""))

    await loc.set_input_files(image_paths)

    # 1) 等 input.files 真的帶上檔案（可觀測）
    try:
        handle = await loc.element_handle()
        if handle is not None:
            await page.wait_for_function(
                "(el, n) => !!el && !!el.files && el.files.length >= n",
                handle,
                len(image_paths),
                timeout=2000,
            )
    except Exception:
        pass

    # 2) 等待上傳/網路穩定（UI 可自定義，避免 800ms 太樂觀）
    try:
        upload_wait_s = float(upload_wait_s)
    except Exception:
        upload_wait_s = 6.0
    if upload_wait_s < 0:
        upload_wait_s = 0.0
    ms = int(upload_wait_s * 1000)

    if ms <= 0:
        # 仍保留極小的穩定等待，避免剛設完 files 就立刻點下一步
        await page.wait_for_timeout(300)
        return

    # 優先用 networkidle（可觀測），取不到就讓它自然超時
    try:
        await page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        # networkidle 不一定能達成（頁面可能長連線），此時至少已等到 timeout
        pass

    # 根据图片数量随机追加"看一看"延迟，模拟真人上传后检查预览
    import random as _rnd
    _extra = _rnd.uniform(0.3, 0.8) * len(image_paths)  # 每张图 0.3-0.8 秒
    await page.wait_for_timeout(int(_extra * 1000))



async def _extract_merch_code(page) -> str:
    """尽量稳地抓『商品編號/商品ID』，并避免"随便抓到一串数字就当商品编号"。

    你的截图里，真正的商品编号出现在成功弹窗（例如「編輯商品成功」→「刊登資訊」→「商品編號」右侧）。
    旧逻辑的问题是：在弹窗出现前，就从页面其它位置（或 URL 参数）抓到某串数字，导致写回 Excel 的商品编号是错的。

    这里的修复思路：
    1) **优先、且只认**：和「商品編號/商品编号」标签绑定在同一行/同一区块的数字（弹窗/页面都可）
    2) 如果还没出现，就返回空串，让外层重试等待（而不是提前返回别的数字）
    3) 只有在完全找不到"标签绑定"的情况下，才进行非常保守的 URL/属性兜底（避免误判）
    """
    LABELS = [
        "商品編號", "商品编号",
        "商品編碼", "商品编码",
        "商品ID", "商品 Id", "商品 Id", "商品 id", "商品ID：", "商品編號："
    ]

    # --- JS: 在指定 root 下按"标签->值"关系提取 ---
    js_extract_by_label = r"""(labels) => {
        const DIG_RE = /\b\d{6,20}\b/;
        const LABEL_RE = new RegExp('^(?:' + labels.map(s => s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|') + ')$');

        function textOf(el) {
            try { return (el && el.textContent) ? el.textContent.trim() : ''; } catch (e) { return ''; }
        }

        function pickDigits(s) {
            if (!s) return '';
            const m = String(s).match(DIG_RE);
            return m ? m[0] : '';
        }

        // 1) 表格行：tr 里 label + value
        function tryTable(labelEl) {
            const tr = labelEl.closest && labelEl.closest('tr');
            if (!tr) return '';
            const cells = Array.from(tr.querySelectorAll('td,th')).map(textOf).filter(Boolean);
            if (cells.length < 2) return '';
            // 倾向取最后一格（多数表格 value 在最后）
            return pickDigits(cells[cells.length - 1]);
        }

        // 2) 常见布局：同一父元素下 label 与 value 是兄弟节点
        function trySiblings(labelEl) {
            const p = labelEl.parentElement;
            if (!p) return '';
            const kids = Array.from(p.children || []);
            // 若父元素只有两块（label/value），直接取另一块
            if (kids.length >= 2) {
                for (const k of kids) {
                    if (k === labelEl) continue;
                    const v = pickDigits(textOf(k));
                    if (v) return v;
                }
            }
            // nextElementSibling
            const sib = labelEl.nextElementSibling;
            if (sib) {
                const v = pickDigits(textOf(sib));
                if (v) return v;
            }
            // 同一父块的整行文本：例如"商品編號 880000000002"
            const line = textOf(p);
            const v2 = pickDigits(line);
            return v2;
        }

        // 3) 更松的：在祖先块里找"商品編號: 123..."
        function tryAncestorRegex(labelEl) {
            let cur = labelEl;
            for (let i = 0; i < 6 && cur; i++) {
                const box = cur.closest ? cur.closest('tr,li,section,div') : null;
                if (box) {
                    const t = textOf(box);
                    const m = t.match(/商品(?:編號|编号|編碼|编码)\s*[:：]?\s*(\d{6,20})/);
                    if (m && m[1]) return m[1];
                }
                cur = cur.parentElement;
            }
            return '';
        }

        function findInRoot(root) {
            if (!root) return '';
            // 先找"叶子"节点中恰好等于标签的
            const all = Array.from(root.querySelectorAll('*'));
            const leafLabels = all.filter(el => {
                if (!el) return false;
                if (el.children && el.children.length > 0) return false;
                const t = textOf(el);
                return t && LABEL_RE.test(t);
            });

            for (const el of leafLabels) {
                let v = tryTable(el);
                if (v) return v;
                v = trySiblings(el);
                if (v) return v;
                v = tryAncestorRegex(el);
                if (v) return v;
            }

            // 再尝试：某些页面 label 和 value 在同一个节点，例如"商品編號：101..."
            const rootText = textOf(root);
            const m2 = rootText.match(/商品(?:編號|编号|編碼|编码)\s*[:：]?\s*(\d{6,20})/);
            if (m2 && m2[1]) return m2[1];

            return '';
        }

        return findInRoot(document);
    }"""


    async def _try_in_frame(frame, dialogs_only: bool) -> str:
        """在某个 frame 内提取：先弹窗，再整页。"""
        try:
            if dialogs_only:
                # 只在弹窗/对话框区域找：避免误抓页面其它数字
                return await frame.evaluate(
                    r"""(labels) => {
                        const candidates = [];
                        // 尽量通用地选"弹窗根"
                        const sels = [
                            '[role="dialog"]',
                            '[aria-modal="true"]',
                            '.modal',
                            '.Modal',
                            '.dialog',
                            '.Dialog'
                        ];
                        for (const sel of sels) {
                            try {
                                document.querySelectorAll(sel).forEach(el => candidates.push(el));
                            } catch(e) {}
                        }
                        // 去重
                        const uniq = [];
                        const seen = new Set();
                        for (const el of candidates) {
                            if (!el) continue;
                            if (seen.has(el)) continue;
                            seen.add(el);
                            uniq.push(el);
                        }

                        const DIG_RE = /\b\d{6,20}\b/;
                        const LABEL_RE = new RegExp('^(?:' + labels.map(s => s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|') + ')$');
                        const textOf = (el) => (el && el.textContent) ? el.textContent.trim() : '';
                        const pickDigits = (s) => {
                            if (!s) return '';
                            const m = String(s).match(DIG_RE);
                            return m ? m[0] : '';
                        };

                        function extractFrom(root) {
                            if (!root) return '';
                            const all = Array.from(root.querySelectorAll('*'));
                            const leafLabels = all.filter(el => {
                                if (!el) return false;
                                if (el.children && el.children.length > 0) return false;
                                const t = textOf(el);
                                return t && LABEL_RE.test(t);
                            });
                            for (const el of leafLabels) {
                                // 表格行
                                const tr = el.closest && el.closest('tr');
                                if (tr) {
                                    const cells = Array.from(tr.querySelectorAll('td,th')).map(textOf).filter(Boolean);
                                    if (cells.length >= 2) {
                                        const v = pickDigits(cells[cells.length - 1]);
                                        if (v) return v;
                                    }
                                }
                                // siblings
                                const p = el.parentElement;
                                if (p) {
                                    const kids = Array.from(p.children || []);
                                    for (const k of kids) {
                                        if (k === el) continue;
                                        const v = pickDigits(textOf(k));
                                        if (v) return v;
                                    }
                                    const sib = el.nextElementSibling;
                                    if (sib) {
                                        const v = pickDigits(textOf(sib));
                                        if (v) return v;
                                    }
                                    const line = textOf(p);
                                    const v2 = pickDigits(line);
                                    if (v2) return v2;
                                }
                                // ancestor regex
                                let cur = el;
                                for (let i = 0; i < 6 && cur; i++) {
                                    const box = cur.closest ? cur.closest('tr,li,section,div') : null;
                                    if (box) {
                                        const t = textOf(box);
                                        const m = t.match(/商品(?:編號|编号|編碼|编码)\s*[:：]?\s*(\d{6,20})/);
                                        if (m && m[1]) return m[1];
                                    }
                                    cur = cur.parentElement;
                                }
                            }
                            const t2 = textOf(root);
                            const m2 = t2.match(/商品(?:編號|编号|編碼|编码)\s*[:：]?\s*(\d{6,20})/);
                            return (m2 && m2[1]) ? m2[1] : '';
                        }

                        // 优先弹窗里带"刊登資訊/编辑商品成功/发布成功"等字样的
                        const prefer = uniq.filter(el => /刊登資訊|編輯商品成功|刊登成功|發佈成功|发布成功/.test(textOf(el)));
                        const pool = prefer.length ? prefer : uniq;

                        for (const root of pool) {
                            const v = extractFrom(root);
                            if (v) return v;
                        }
                        return '';
                    }""",
                    LABELS,
                )
            else:
                return await frame.evaluate(js_extract_by_label, LABELS)
        except Exception:
            return ""

    # 1) 优先：成功弹窗（main frame + 所有 iframe）
    try:
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
    except Exception:
        frames = []

    for fr in frames:
        code = await _try_in_frame(fr, dialogs_only=True)
        if code:
            return code

    # 2) 次优：整页内按"商品編號标签"找（仍然要求标签绑定）
    for fr in frames:
        code = await _try_in_frame(fr, dialogs_only=False)
        if code:
            return code

    # 3) 非常保守兜底：只在"明显像 auction/item id"的 URL 参数中取（避免 publish?id=xxxx 误判）
    try:
        u = page.url or ""
        pu = urlparse(u)
        qs = parse_qs(pu.query or "")
        # 只信这些"语义明确"的参数名，不信通用 id/no（publish 页面会误判）
        for k in ("auction_id", "auctionId", "auctionID", "item_id", "itemId", "itemID"):
            if k in qs:
                for v in qs.get(k) or []:
                    m = re.search(r"(\d{6,20})", str(v))
                    if m:
                        return m.group(1)

        # 路径里只信 /auction/123 或 /item/123 这种，不信 publish/edit
        m = re.search(r"/(?:auction|item|detail)/(\d{6,20})", pu.path or "", re.I)
        if m:
            return m.group(1)
    except Exception:
        pass

    # 4) 最後兜底：頁面全文正則匹配「商品編號」附近的數字
    try:
        body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        m = re.search(r'商品[編编][號号碼码]\s*[:：]?\s*(\d{6,20})', body_text or "")
        if m:
            return m.group(1)
    except Exception:
        pass

    return ""

async def _save_debug_screenshot(page, out_dir: Path, tag: str) -> Optional[Path]:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        p = out_dir / f"publish_{tag}_{ts}.png"
        await page.screenshot(path=str(p), full_page=True)
        return p
    except Exception:
        return None



async def _save_debug_html(page, out_dir: Path, tag: str) -> Optional[Path]:
    """保存當前頁 HTML（給『待人工核對』等特殊情況保留現場）。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        p = out_dir / f"publish_{tag}_{ts}.html"
        html = await page.content()
        p.write_text(html, encoding="utf-8", errors="ignore")
        return p
    except Exception:
        return None




class StepTracer:
    """逐步截圖/保存 HTML 的調試器（v8）。

    - enabled=False 時完全不做事
    - save_html=True 時每步會額外保存 html（較慢，必要時再開）
    - snap() 會把資料寫入 base_dir / row_{row} 子資料夾
    """

    def __init__(self, page, base_dir: Path, *, enabled: bool = True, save_html: bool = False):
        self.page = page
        self.base_dir = Path(base_dir)
        self.enabled = bool(enabled)
        self.save_html = bool(save_html)
        self._row: int = 0
        self._step: int = 0
        self._row_dir: Optional[Path] = None
        self._last_snap_ts: float = 0.0
        self._meta_path: Optional[Path] = None

    def start_row(self, row: int) -> None:
        self._row = int(row)
        self._step = 0
        if not self.enabled:
            self._row_dir = None
            return
        self._row_dir = self.base_dir / f"row_{self._row}"
        self._row_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self._row_dir / "meta.jsonl"

    async def snap(self, tag: str, *, throttle_sec: float = 0.0, full_page: bool = True) -> Optional[Path]:
        if not self.enabled:
            return None
        now = time.time()
        if throttle_sec and (now - self._last_snap_ts) < float(throttle_sec):
            return None
        self._last_snap_ts = now

        self._step += 1
        safe_tag = re.sub(r"[^0-9a-zA-Z_\-\.]+", "_", (tag or "step"))[:60]
        row_dir = self._row_dir or self.base_dir
        row_dir.mkdir(parents=True, exist_ok=True)

        img_path = row_dir / f"{self._step:03d}_{safe_tag}.png"
        try:
            await self.page.screenshot(path=str(img_path), full_page=bool(full_page))
        except Exception:
            return None

        # 元信息（URL/时间）
        try:
            rec = {
                "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tag": safe_tag,
                "url": getattr(self.page, "url", "") or "",
                "img": img_path.name,
            }
            mp = self._meta_path or (row_dir / "meta.jsonl")
            with open(mp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

        if self.save_html:
            try:
                html = await self.page.content()
                html_path = row_dir / f"{self._step:03d}_{safe_tag}.html"
                html_path.write_text(html, encoding="utf-8", errors="ignore")
            except Exception:
                pass

        return img_path

# -------------------------- Core runner --------------------------


@dataclass
class PublishStat:
    account: str
    excel: str
    success: int = 0
    fail: int = 0
    running: bool = False
    started_at: str = ""
    ended_at: str = ""
    last_error: str = ""
    current_row: int = 0
    base_success: int = 0   # 累计模式：上一批的累积基数
    base_fail: int = 0      # 累计模式：上一批的累积基数


class AutoPublishFeatureTab:
    def __init__(self, app, frame):
        self.app = app
        self.frame = frame
        self._stop = threading.Event()
        self._pause = asyncio.Event()
        self._pause.set()  # set=運行中, clear=已暫停
        self._future = None
        self._stats: Dict[str, PublishStat] = {}
        self._PAUSE_STATE_FILE = "publish_pause_state.json"

    def build(self):
        f = self.frame
        f.columnconfigure(0, weight=1)

        top = ttk.Labelframe(f, text="自動刊登（Yahoo拍賣）")
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 3))
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="Excel資料夾").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.var_dir = tk.StringVar(value=str(PUBLISH_DIR))
        ttk.Entry(top, textvariable=self.var_dir).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(top, text="打开资料夹", command=self._open_dir).grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        ttk.Button(top, text="刷新", command=self._refresh_files).grid(row=0, column=3, sticky="ew", padx=4, pady=4)

        ttk.Label(top, text="並發帳號").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        _saved_conc = str(self.app.settings.get("publish_concurrency", 3)) if self.app else "3"
        self.var_conc = tk.StringVar(value=_saved_conc)
        ttk.Entry(top, textvariable=self.var_conc, width=8).grid(row=1, column=1, sticky="w", padx=4, pady=4)

        def _on_conc_change(*_a):
            try:
                val = int(self.var_conc.get().strip())
                if val < 1:
                    return
            except (ValueError, TypeError):
                return
            if self.app:
                self.app.settings["publish_concurrency"] = val
                from core.accounts import save_settings
                save_settings(self.app.settings)
        self.var_conc.trace_add("write", _on_conc_change)

        btns = ttk.Frame(top)
        btns.grid(row=1, column=2, columnspan=2, sticky="e", padx=4, pady=4)
        ttk.Button(btns, text="拆分Excel", command=self._open_split_dialog).pack(side="left", padx=(0, 6))
        self.btn_start = ttk.Button(btns, style="Accent.TButton", text="开始刊登", command=self.start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_pause = ttk.Button(btns, text="暫停", command=self._toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")

        # 使用說明
        help_text = (
            "定期刊登：將 test.xlsx 放入資料夾 → 在下方「定期刊登計劃」添加計劃 → 系統自動拆分並刊登\n"
            "手動刊登：放入 {帳號}.xlsx → 點「刷新」→ 勾選帳號 → 點「開始刊登」"
        )
        ttk.Label(top, text=help_text, foreground="gray",
                  justify="left", wraplength=560).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 2))

        # 定期刊登计划
        sched_frame = ttk.Labelframe(f, text="定期刊登計劃")
        sched_frame.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))
        sched_frame.columnconfigure(0, weight=1)

        self._sched_tree = ttk.Treeview(
            sched_frame, columns=("time", "assignments"), show="headings", height=2)
        self._sched_tree.heading("time", text="時間")
        self._sched_tree.heading("assignments", text="分配方案")
        self._sched_tree.column("time", width=80, stretch=False)
        self._sched_tree.column("assignments", width=400, stretch=True)
        self._sched_tree.grid(row=0, column=0, sticky="ew", padx=4, pady=(3, 1))

        sched_btns = ttk.Frame(sched_frame)
        sched_btns.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 2))
        ttk.Button(sched_btns, text="添加計劃", command=self._sched_add).pack(side="left", padx=(0, 6))
        ttk.Button(sched_btns, text="刪除選中", command=self._sched_del).pack(side="left", padx=(0, 6))
        ttk.Button(sched_btns, text="清空所有", command=self._sched_clear).pack(side="left")

        self._sched_refresh()

        # 記錄表
        mid = ttk.Labelframe(f, text="刊登記錄")
        mid.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 3))
        f.rowconfigure(2, weight=1)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        cols = ("sel", "account", "success", "fail", "status", "error")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8, selectmode="none")
        self._all_selected = True  # 全选状态
        self.tree.heading("sel", text="\u2611", command=self._toggle_select_all)
        self.tree.heading("account", text="账号")
        self.tree.heading("success", text="成功")
        self.tree.heading("fail", text="失败")
        self.tree.heading("status", text="状态")
        self.tree.heading("error", text="错误信息")

        self.tree.column("sel", width=40, anchor="center", stretch=False)
        self.tree.column("account", width=180, minwidth=120, stretch=False)
        self.tree.column("success", width=45, anchor="center", stretch=False)
        self.tree.column("fail", width=45, anchor="center", stretch=False)
        self.tree.column("status", width=50, anchor="center", stretch=False)
        self.tree.column("error", width=200, minwidth=80, stretch=True)
        self.tree.bind("<Button-1>", self._on_sel_click)

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        action_bar = ttk.Frame(mid)
        action_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(action_bar, text="刪除選中", command=self._remove_selected_excels).pack(side="left", padx=4)
        ttk.Button(action_bar, text="导出成功", command=lambda: self._open_batch_summary("成功汇总.xlsx")).pack(side="left", padx=4)
        ttk.Button(action_bar, text="导出失败", command=lambda: self._open_batch_summary("失败汇总.xlsx")).pack(side="left", padx=4)
        # 右侧：操作记录 + 累计记录
        ttk.Button(action_bar, text="操作记录", command=self._show_history).pack(side="right", padx=4)
        _saved_cumulative = bool(self.app.settings.get("publish_cumulative", False)) if self.app else False
        self._var_cumulative = tk.BooleanVar(value=_saved_cumulative)
        self._var_cumulative.trace_add("write", self._on_cumulative_changed)
        ttk.Checkbutton(action_bar, text="累计记录", variable=self._var_cumulative).pack(side="right", padx=4)

        self._batch_summary_start = {}  # {"成功汇总.xlsx": row_count_before_batch, ...}

        self._refresh_files()

    # ------------------------------------------------------------------
    # 定期刊登計劃 GUI
    # ------------------------------------------------------------------

    def _sched_refresh(self):
        for row in self._sched_tree.get_children():
            self._sched_tree.delete(row)
        settings = self.app.settings if self.app else {}
        for sch in settings.get("publish_schedule", []):
            self._sched_tree.insert("", "end", values=(sch["time"], sch["assignments"]))

    def _sched_add(self):
        from core.accounts import load_accounts, load_settings, save_settings
        import openpyxl
        # 读取 test.xlsx 行数（使用与拆分对话框相同的路径）
        _pub_dir = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        _src = _pub_dir / "test.xlsx"
        total_rows = 0
        if _src.exists():
            try:
                _wb = openpyxl.load_workbook(_src, read_only=True)
                _ws = _wb.active
                total_rows = max(0, _ws.max_row - 1)
                _wb.close()
            except Exception:
                pass

        dlg = tk.Toplevel(self.frame)
        dlg.title(f"添加定期刊登計劃（test.xlsx 共 {total_rows} 條）")
        dlg.transient(self.frame.winfo_toplevel())
        dlg.grab_set()

        accounts = load_accounts()
        acc_names = [a.get("name", "") for a in accounts if a.get("name")]
        dlg_h = min(160 + len(acc_names) * 30, 600)

        dlg.update_idletasks()
        w = 550
        x = self.frame.winfo_toplevel().winfo_x() + (self.frame.winfo_toplevel().winfo_width() - w) // 2
        y = self.frame.winfo_toplevel().winfo_y() + (self.frame.winfo_toplevel().winfo_height() - dlg_h) // 2
        dlg.geometry(f"{w}x{dlg_h}+{x}+{y}")
        dlg.minsize(500, 250)

        # --- 時間（支持多個，逗號隔開） ---
        time_frm = ttk.Frame(dlg)
        time_frm.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(time_frm, text="執行時間：").pack(side="left")
        var_time = tk.StringVar(value="09:00")
        ttk.Entry(time_frm, textvariable=var_time, width=20).pack(side="left", padx=(4, 0))
        ttk.Label(dlg, text="24小時制，多個時間用逗號隔開，如 09:00,14:00,20:00", foreground="gray").pack(anchor="w", padx=12)

        # --- 參數區 ---
        param_frame = ttk.Frame(dlg)
        param_frame.pack(fill="x", padx=12, pady=(8, 5))

        ttk.Label(param_frame, text="總數上限:").pack(side="left")
        var_limit_total = tk.StringVar(value="3000")
        ttk.Entry(param_frame, textvariable=var_limit_total, width=8).pack(side="left", padx=(4, 12))

        ttk.Label(param_frame, text="單賬號最多:").pack(side="left")
        var_limit_per = tk.StringVar(value="")
        ttk.Entry(param_frame, textvariable=var_limit_per, width=8).pack(side="left", padx=(4, 12))

        btn_query = ttk.Button(param_frame, text="查詢勾選商品數")
        btn_query.pack(side="left", padx=(4, 0))

        account_current_counts = {}

        # --- 全選 ---
        select_frame = ttk.Frame(dlg)
        select_frame.pack(fill="x", padx=12, pady=(0, 2))
        var_select_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(select_frame, text="全選", variable=var_select_all).pack(side="left")

        # --- 帳號 + 數量（可滾動） ---
        ttk.Label(dlg, text=f"每個帳號刊登數量（test.xlsx 共 {total_rows} 條）：").pack(anchor="w", padx=12, pady=(2, 2))

        canvas_frm = ttk.Frame(dlg)
        canvas_frm.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        canvas = tk.Canvas(canvas_frm, highlightthickness=0, height=200)
        sb = ttk.Scrollbar(canvas_frm, orient="vertical", command=canvas.yview)
        canvas.config(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        canvas.bind("<MouseWheel>", _on_mousewheel)
        inner.bind("<MouseWheel>", _on_mousewheel)
        dlg.bind("<MouseWheel>", _on_mousewheel)

        entries = []  # [(BooleanVar, name, StringVar, count_label)]
        for name in acc_names:
            row_f = ttk.Frame(inner)
            row_f.pack(fill="x", pady=2)
            bv = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_f, variable=bv).pack(side="left")
            ttk.Label(row_f, text=name, width=28, anchor="w").pack(side="left", padx=(4, 8))
            count_lbl = ttk.Label(row_f, text="0", width=6, anchor="e", foreground="green")
            count_lbl.pack(side="left", padx=(0, 4))
            sv = tk.StringVar(value="0")
            ttk.Entry(row_f, textvariable=sv, width=8).pack(side="left")
            ttk.Label(row_f, text="條").pack(side="left", padx=(2, 0))
            entries.append((bv, name, sv, count_lbl))
            _bind_mousewheel(row_f)

        # 全選功能綁定
        def _on_select_all(*_args):
            state = var_select_all.get()
            for bv, _, _, _ in entries:
                bv.set(state)
        var_select_all.trace_add("write", _on_select_all)

        def _on_inner_configure(e):
            canvas.config(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_inner_configure)
        def _on_canvas_configure(e):
            canvas.itemconfig(win_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # --- 剩餘計數器 + 平均分配按鈕 ---
        bottom_frame = ttk.Frame(dlg)
        bottom_frame.pack(fill="x", padx=12, pady=(4, 10))

        lbl_remain = ttk.Label(bottom_frame, text=f"剩餘: {total_rows}")
        lbl_remain.pack(side="left")

        def _update_remain(*_args):
            used = 0
            for bv, _, sv, _ in entries:
                if bv.get():
                    try:
                        used += max(0, int(sv.get()))
                    except ValueError:
                        pass
            lbl_remain.config(text=f"剩餘: {total_rows - used}")

        for bv, _, sv, _ in entries:
            bv.trace_add("write", _update_remain)
            sv.trace_add("write", _update_remain)

        # --- 查詢功能 ---
        def _do_query():
            checked_names = [name for bv, name, _, _ in entries if bv.get()]
            if not checked_names:
                messagebox.showwarning("查詢", "請至少勾選一個帳號。", parent=dlg)
                return

            btn_query.config(state="disabled", text="查詢中...")
            dlg.update_idletasks()

            def _run():
                from .cookie_store import load_cookie_cache_with_backup, save_cookie_cache
                from .merch_http_ops import AuthSession, fetch_merchandise_list, _fetch_wssid_http
                results = {}
                for name in checked_names:
                    try:
                        profile_dir = ROOT_DIR / "profiles" / name
                        cookies, wssid, _ = load_cookie_cache_with_backup(profile_dir)
                        if not cookies:
                            results[name] = None
                            continue
                        if not wssid:
                            wssid = _fetch_wssid_http(cookies, log=None, proxy="")
                            if wssid:
                                save_cookie_cache(profile_dir, cookies, wssid)
                            else:
                                results[name] = None
                                continue
                        session = AuthSession(cookies=cookies, wssid=wssid)
                        session.build_http()
                        _, total = fetch_merchandise_list(session, item_status="shelve", limit=1)
                        results[name] = total
                    except Exception as e:
                        print(f"[查詢] {name} 失敗: {e}")
                        results[name] = None
                try:
                    if dlg.winfo_exists():
                        dlg.after(0, lambda: _on_query_done(results))
                except Exception:
                    pass

            def _on_query_done(results):
                btn_query.config(state="normal", text="查詢勾選商品數")
                account_current_counts.update(results)
                for bv, name, sv, count_lbl in entries:
                    if name in results:
                        count = results[name]
                        if count is not None:
                            count_lbl.config(text=str(count), foreground="green")
                        else:
                            count_lbl.config(text="失敗", foreground="red")
                failed = [n for n, c in results.items() if c is None]
                if failed:
                    messagebox.showinfo("查詢結果", f"查詢完成。{len(failed)} 個帳號失敗。", parent=dlg)
                else:
                    messagebox.showinfo("查詢結果", f"查詢完成。成功查詢 {len(results)} 個帳號。", parent=dlg)

            threading.Thread(target=_run, daemon=True).start()

        btn_query.config(command=_do_query)

        # --- 平均分配功能 ---
        btn_average = ttk.Button(bottom_frame, text="平均分配")
        btn_average.pack(side="right", padx=(0, 6))

        def _do_average():
            if total_rows <= 0:
                messagebox.showwarning("平均分配", "test.xlsx 沒有可分配資料。", parent=dlg)
                return
            try:
                limit_total = int(var_limit_total.get().strip())
                if limit_total <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("平均分配", "總數上限必須是正整數。", parent=dlg)
                return
            limit_per = None
            limit_per_str = var_limit_per.get().strip()
            if limit_per_str:
                try:
                    limit_per = int(limit_per_str)
                    if limit_per <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning("平均分配", "單賬號最多必須是正整數或留空。", parent=dlg)
                    return
            checked_names = [name for bv, name, _, _ in entries if bv.get()]
            if not checked_names:
                messagebox.showwarning("平均分配", "請至少勾選一個帳號。", parent=dlg)
                return

            accounts_data = []
            for bv, name, sv, _ in entries:
                current = account_current_counts.get(name)
                is_checked = bv.get()
                accounts_data.append((name, current, is_checked))

            assignments, stats = calculate_auto_distribution(accounts_data, total_rows, limit_total, limit_per)
            assignment_dict = dict(assignments)
            for bv, name, sv, _ in entries:
                if name in assignment_dict:
                    sv.set(str(assignment_dict[name]))
            _update_remain()

            lines = []
            if stats["assigned_total"] > 0:
                lines.append("平均分配完成！")
            else:
                lines.append("平均分配完成，但沒有可分配資料。")
            if stats["success"] > 0:
                lines.append(f"✓ 成功填充 {stats['success']} 個帳號")
            if stats["zero"] > 0:
                lines.append(f"• {stats['zero']} 個帳號最終分配為 0")
            if stats["failed"] > 0:
                lines.append(f"✗ 其中 {stats['failed']} 個帳號因查詢失敗而未分配")
            if stats["insufficient"]:
                lines.append("⚠ test.xlsx 資料不足，已按順序優先分配前面的勾選帳號")
            lines.append(f"\n實際分配：{stats['assigned_total']} 條")
            lines.append(f"test.xlsx 剩餘：{stats['remaining_test']} 條")
            messagebox.showinfo("平均分配結果", "\n".join(lines), parent=dlg)

        btn_average.config(command=_do_average)

        # --- 確定 ---
        def _save():
            import re
            times_raw = var_time.get().strip()
            if not times_raw:
                messagebox.showwarning("錯誤", "請輸入時間", parent=dlg)
                return
            time_list = []
            for t in re.split(r'[,，\s]+', times_raw):
                t = t.strip().replace("：", ":")
                if not t:
                    continue
                if not re.fullmatch(r'\d{1,2}:\d{2}', t):
                    messagebox.showwarning("錯誤", f"時間格式錯誤: {t}", parent=dlg)
                    return
                hh, mm = t.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    messagebox.showwarning("錯誤", f"時間無效: {t}", parent=dlg)
                    return
                time_list.append(f"{int(hh):02d}:{mm}")
            if not time_list:
                messagebox.showwarning("錯誤", "請輸入時間", parent=dlg)
                return

            parts = []
            for bv, name, sv, _ in entries:
                if not bv.get():
                    continue
                raw = sv.get().strip()
                if not raw or raw == "0":
                    continue
                try:
                    cnt = int(raw)
                    if cnt <= 0:
                        continue
                except ValueError:
                    messagebox.showwarning("錯誤", f"帳號 {name} 的數量無效: {raw}", parent=dlg)
                    return
                parts.append(f"{name}:{cnt}")
            if not parts:
                messagebox.showwarning("錯誤", "請至少為一個帳號設置數量", parent=dlg)
                return

            assign_str = ",".join(parts)
            total_assigned = sum(int(p.split(":")[1]) for p in parts)
            if total_rows > 0 and total_assigned > total_rows:
                if not messagebox.askyesno("提示",
                        f"分配總數 ({total_assigned}) 超過 test.xlsx 現有數量 ({total_rows})，確定保存？",
                        parent=dlg):
                    return
            settings = load_settings()
            schedules = settings.get("publish_schedule", [])
            for t in time_list:
                schedules.append({"time": t, "assignments": assign_str})
            schedules.sort(key=lambda x: x["time"])
            settings["publish_schedule"] = schedules
            save_settings(settings)
            if self.app:
                self.app.settings = settings
            dlg.destroy()
            self._sched_refresh()

        ttk.Button(dlg, text="確定", command=_save).pack(pady=(8, 12))

    def _sched_del(self):
        from core.accounts import load_settings, save_settings
        sel = self._sched_tree.selection()
        if not sel:
            return
        idx = self._sched_tree.index(sel[0])
        settings = load_settings()
        schedules = settings.get("publish_schedule", [])
        if 0 <= idx < len(schedules):
            schedules.pop(idx)
            settings["publish_schedule"] = schedules
            save_settings(settings)
            if self.app:
                self.app.settings = settings
        self._sched_refresh()

    def _sched_clear(self):
        from core.accounts import load_settings, save_settings
        if not messagebox.askyesno("確認", "確定清空所有定期刊登計劃？"):
            return
        settings = load_settings()
        settings["publish_schedule"] = []
        save_settings(settings)
        if self.app:
            self.app.settings = settings
        self._sched_refresh()

    # ------------------------------------------------------------------
    # 拆分 Excel 對話框
    # ------------------------------------------------------------------

    def _open_split_dialog(self):
        """彈出拆分對話框：選擇來源 test.xlsx，為每個帳號指定數量。"""
        import json as _json

        pub_dir = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        source = pub_dir / "test.xlsx"
        if not source.exists():
            messagebox.showwarning("拆分", f"找不到 test.xlsx\n路徑: {source}")
            return

        # 讀取 test.xlsx 行數
        try:
            wb_tmp = openpyxl.load_workbook(source, read_only=True)
            ws_tmp = wb_tmp.active
            total_rows = ws_tmp.max_row - 1  # 扣掉表頭
            wb_tmp.close()
        except Exception as e:
            messagebox.showerror("拆分", f"讀取 test.xlsx 失敗:\n{e}")
            return

        if total_rows <= 0:
            messagebox.showinfo("拆分", "test.xlsx 沒有資料行。")
            return

        # 讀取帳號列表
        acc_file = ROOT_DIR / "accounts.json"
        try:
            with open(acc_file, "r", encoding="utf-8") as fh:
                accounts = _json.load(fh)
        except Exception:
            accounts = []
        acc_names = [a.get("name", "") for a in accounts if a.get("name")]

        if not acc_names:
            messagebox.showwarning("拆分", "accounts.json 中沒有帳號。")
            return

        # --- 建立對話框 ---
        dlg = tk.Toplevel(self.frame)
        dlg.title(f"拆分 test.xlsx（共 {total_rows} 條）")
        dlg.geometry("560x560")
        dlg.resizable(False, True)
        dlg.grab_set()

        ttk.Label(dlg, text=f"test.xlsx 共 {total_rows} 條資料，可手動填寫或使用平均分配自動填充。",
                  wraplength=530).pack(padx=10, pady=(10, 5), anchor="w")

        # 頂部參數區
        param_frame = ttk.Frame(dlg)
        param_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(param_frame, text="總數上限:").pack(side="left")
        var_limit_total = tk.StringVar(value="3000")
        ttk.Entry(param_frame, textvariable=var_limit_total, width=8).pack(side="left", padx=(4, 12))

        ttk.Label(param_frame, text="單賬號最多:").pack(side="left")
        var_limit_per = tk.StringVar(value="")
        ttk.Entry(param_frame, textvariable=var_limit_per, width=8).pack(side="left", padx=(4, 12))

        account_current_counts = {}

        # 底部先 pack（確保按鈕始終可見）
        bottom = ttk.Frame(dlg)
        bottom.pack(side="bottom", fill="x", padx=10, pady=(5, 10))

        # 全選控件
        select_all_frame = ttk.Frame(dlg)
        select_all_frame.pack(fill="x", padx=10, pady=(0, 5))
        var_select_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(select_all_frame, text="全選", variable=var_select_all).pack(side="left")

        # 可滾動區域（填充剩餘空間）
        scroll_frame = ttk.Frame(dlg)
        scroll_frame.pack(side="top", fill="both", expand=True, padx=10, pady=5)

        canvas = tk.Canvas(scroll_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # 鼠標滾輪支援
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        canvas.bind("<MouseWheel>", _on_mousewheel)
        inner.bind("<MouseWheel>", _on_mousewheel)
        dlg.bind("<MouseWheel>", _on_mousewheel)

        # 每個帳號一行：勾選 + 名稱 + 當前商品數 + 數量輸入
        entries = []  # [(BooleanVar, name, StringVar, count_label)]
        for name in acc_names:
            row_f = ttk.Frame(inner)
            row_f.pack(fill="x", pady=2)
            bv = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_f, variable=bv).pack(side="left")
            ttk.Label(row_f, text=name, width=28, anchor="w").pack(side="left", padx=(4, 8))
            count_lbl = ttk.Label(row_f, text="0", width=6, anchor="e", foreground="green")
            count_lbl.pack(side="left", padx=(0, 4))
            sv = tk.StringVar(value="0")
            ttk.Entry(row_f, textvariable=sv, width=8).pack(side="left")
            ttk.Label(row_f, text="條").pack(side="left", padx=(2, 0))
            entries.append((bv, name, sv, count_lbl))
            _bind_mousewheel(row_f)

        # 全選功能綁定
        def _on_select_all(*_args):
            state = var_select_all.get()
            for bv, _, _, _ in entries:
                bv.set(state)
        var_select_all.trace_add("write", _on_select_all)

        # 底部控件（bottom 已在上方 pack）
        lbl_remain = ttk.Label(bottom, text=f"剩餘: {total_rows}")
        lbl_remain.pack(side="left")

        def _update_remain(*_args):
            used = 0
            for bv, _, sv, _ in entries:
                if bv.get():
                    try:
                        used += max(0, int(sv.get()))
                    except ValueError:
                        pass
            lbl_remain.config(text=f"剩餘: {total_rows - used}")

        for bv, _, sv, _ in entries:
            bv.trace_add("write", _update_remain)
            sv.trace_add("write", _update_remain)

        def _do_query():
            checked_names = [name for bv, name, _, _ in entries if bv.get()]
            if not checked_names:
                messagebox.showwarning("查詢", "請至少勾選一個帳號。", parent=dlg)
                return

            btn_query.config(state="disabled", text="查詢中...")
            btn_average.config(state="disabled")
            btn_confirm.config(state="disabled")
            dlg.update_idletasks()

            def _run():
                from .cookie_store import load_cookie_cache_with_backup, save_cookie_cache
                from .merch_http_ops import AuthSession, fetch_merchandise_list, _fetch_wssid_http
                results = {}
                for name in checked_names:
                    try:
                        profile_dir = ROOT_DIR / "profiles" / name
                        cookies, wssid, _ = load_cookie_cache_with_backup(profile_dir)
                        print(f"[查詢] {name}: cookies={len(cookies)}, wssid={'有' if wssid else '無'}")
                        if not cookies:
                            print(f"[查詢] {name}: cookies 為空")
                            results[name] = None
                            continue

                        # wssid 为空时自动补提取
                        if not wssid:
                            print(f"[查詢] {name}: 補提取 wssid...")
                            wssid = _fetch_wssid_http(cookies, log=None, proxy="")
                            if wssid:
                                save_cookie_cache(profile_dir, cookies, wssid)
                                print(f"[查詢] {name}: wssid 補提取成功")
                            else:
                                print(f"[查詢] {name}: wssid 補提取失敗")
                                results[name] = None
                                continue

                        session = AuthSession(cookies=cookies, wssid=wssid)
                        session.build_http()
                        print(f"[查詢] {name}: 查詢商品列表...")
                        try:
                            _, total = fetch_merchandise_list(session, item_status="shelve", limit=1)
                        except Exception as _first_err:
                            # wssid 过期，尝试刷新后重试
                            if "401" in str(_first_err) or "wssid" in str(_first_err).lower():
                                print(f"[查詢] {name}: wssid 过期，刷新重试...")
                                _new_wssid = _fetch_wssid_http(cookies, log=None, proxy="")
                                if _new_wssid and _new_wssid != wssid:
                                    wssid = _new_wssid
                                    save_cookie_cache(profile_dir, cookies, wssid)
                                    session = AuthSession(cookies=cookies, wssid=wssid)
                                    session.build_http()
                                    _, total = fetch_merchandise_list(session, item_status="shelve", limit=1)
                                else:
                                    raise
                            else:
                                raise
                        print(f"[查詢] {name}: 成功，商品數={total}")
                        results[name] = total
                    except Exception as e:
                        print(f"[查詢] {name} 失敗: {e}")
                        results[name] = None
                try:
                    if dlg.winfo_exists():
                        dlg.after(0, lambda: _on_query_done(results))
                except Exception:
                    pass

            def _on_query_done(results):
                btn_query.config(state="normal", text="查詢勾選商品數")
                btn_average.config(state="normal")
                btn_confirm.config(state="normal")
                account_current_counts.update(results)
                # 更新界面显示
                for bv, name, sv, count_lbl in entries:
                    if name in results:
                        count = results[name]
                        if count is not None:
                            count_lbl.config(text=str(count), foreground="green")
                        else:
                            count_lbl.config(text="失敗", foreground="red")
                failed = [n for n, c in results.items() if c is None]
                if failed:
                    if len(failed) <= 5:
                        failed_list = "\n".join(failed)
                    else:
                        failed_list = "\n".join(failed[:5]) + f"\n... 等 {len(failed)} 個"
                    msg = f"查詢完成。{len(failed)} 個帳號失敗：\n\n{failed_list}"
                    messagebox.showinfo("查詢結果", msg, parent=dlg)
                else:
                    messagebox.showinfo("查詢結果", f"查詢完成。成功查詢 {len(results)} 個帳號。", parent=dlg)

            threading.Thread(target=_run, daemon=True).start()

        def _do_average():
            if total_rows <= 0:
                messagebox.showwarning("平均分配", "test.xlsx 沒有可分配資料。", parent=dlg)
                return
            try:
                limit_total = int(var_limit_total.get().strip())
                if limit_total <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("平均分配", "總數上限必須是正整數。", parent=dlg)
                return
            limit_per_str = var_limit_per.get().strip()
            limit_per = 0
            if limit_per_str:
                try:
                    limit_per = int(limit_per_str)
                    if limit_per <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning("平均分配", "單賬號最多必須是正整數或留空。", parent=dlg)
                    return
            checked_names = [name for bv, name, _, _ in entries if bv.get()]
            if not checked_names:
                messagebox.showwarning("平均分配", "請至少勾選一個帳號。", parent=dlg)
                return
            need_query = [n for n in checked_names if n not in account_current_counts or account_current_counts.get(n) is None]
            if need_query:
                btn_query.config(state="disabled")
                btn_average.config(state="disabled", text="平均分配中...")
                btn_confirm.config(state="disabled")
                dlg.update_idletasks()
                _auto_query_then_distribute(need_query, limit_total, limit_per)
            else:
                _calculate_and_fill(limit_total, limit_per)

        def _auto_query_then_distribute(need_query, limit_total, limit_per):
            def _run():
                from .cookie_store import load_cookie_cache_with_backup, save_cookie_cache
                from .merch_http_ops import AuthSession, fetch_merchandise_list, _fetch_wssid_http
                results = {}
                for name in need_query:
                    try:
                        profile_dir = ROOT_DIR / "profiles" / name
                        cookies, wssid, _ = load_cookie_cache_with_backup(profile_dir)
                        print(f"[查詢] {name}: cookies={len(cookies)}, wssid={'有' if wssid else '無'}")
                        if not cookies:
                            print(f"[查詢] {name}: cookies 為空，跳過")
                            results[name] = None
                            continue

                        # wssid 为空时自动补提取
                        if not wssid:
                            print(f"[查詢] {name}: 補提取 wssid...")
                            wssid = _fetch_wssid_http(cookies, log=None, proxy="")
                            if wssid:
                                save_cookie_cache(profile_dir, cookies, wssid)
                                print(f"[查詢] {name}: wssid 補提取成功")
                            else:
                                print(f"[查詢] {name}: wssid 補提取失敗")
                                results[name] = None
                                continue

                        session = AuthSession(cookies=cookies, wssid=wssid)
                        session.build_http()
                        print(f"[查詢] {name}: 開始查詢商品列表...")
                        _, total = fetch_merchandise_list(session, item_status="shelve", limit=1)
                        print(f"[查詢] {name}: 成功，商品數={total}")
                        results[name] = total
                    except Exception as e:
                        print(f"[查詢] {name} 失敗: {e}")
                        import traceback
                        traceback.print_exc()
                        results[name] = None
                try:
                    if dlg.winfo_exists():
                        dlg.after(0, lambda: _on_query_done(results, limit_total, limit_per))
                except Exception:
                    pass
            def _on_query_done(results, lt, lp):
                btn_query.config(state="normal", text="查詢勾選商品數")
                btn_average.config(state="normal", text="平均分配")
                btn_confirm.config(state="normal")
                account_current_counts.update(results)
                _calculate_and_fill(lt, lp)
            threading.Thread(target=_run, daemon=True).start()

        def _calculate_and_fill(limit_total, limit_per):
            accounts_data = []
            for bv, name, sv, _ in entries:
                current = account_current_counts.get(name)
                is_checked = bv.get()
                accounts_data.append((name, current, is_checked))
            assignments, stats = calculate_auto_distribution(accounts_data, total_rows, limit_total, limit_per)
            assignment_dict = dict(assignments)
            for bv, name, sv, _ in entries:
                if name in assignment_dict:
                    sv.set(str(assignment_dict[name]))
            _update_remain()
            _show_distribution_result(stats)

        def _show_distribution_result(stats):
            lines = []
            if stats["assigned_total"] > 0:
                lines.append("平均分配完成！")
            else:
                lines.append("平均分配完成，但沒有可分配資料。")
            if stats["success"] > 0:
                lines.append(f"✓ 成功填充 {stats['success']} 個帳號")
            if stats["zero"] > 0:
                lines.append(f"• {stats['zero']} 個帳號最終分配為 0")
            if stats["failed"] > 0:
                lines.append(f"✗ 其中 {stats['failed']} 個帳號因查詢失敗而未分配")
            if stats["insufficient"]:
                lines.append("⚠ test.xlsx 資料不足，已按順序優先分配前面的勾選帳號")
            lines.append(f"\n實際分配：{stats['assigned_total']} 條")
            lines.append(f"test.xlsx 剩餘：{stats['remaining_test']} 條")
            messagebox.showinfo("平均分配結果", "\n".join(lines), parent=dlg)

        def _do_split():
            assignments = []
            for bv, name, sv, _ in entries:
                if not bv.get():
                    continue
                try:
                    cnt = int(sv.get())
                except ValueError:
                    cnt = 0
                if cnt > 0:
                    assignments.append((name, cnt))

            if not assignments:
                messagebox.showwarning("拆分", "請至少勾選一個帳號並輸入數量。", parent=dlg)
                return

            total_assigned = sum(c for _, c in assignments)
            if total_assigned > total_rows:
                messagebox.showwarning("拆分",
                    f"分配總數 {total_assigned} 超過可用行數 {total_rows}。",
                    parent=dlg)
                return

            # 禁用按鈕，顯示進度
            btn_confirm.config(state="disabled", text="拆分中...")
            btn_cancel.config(state="disabled")
            dlg.update_idletasks()

            def _run():
                try:
                    result = split_excel(source, assignments, pub_dir)
                except Exception as e:
                    dlg.after(0, lambda: _on_error(str(e)))
                    return
                dlg.after(0, lambda: _on_done(result, total_assigned))

            def _on_error(msg):
                btn_confirm.config(state="normal", text="確認拆分")
                btn_cancel.config(state="normal")
                messagebox.showerror("拆分失敗", msg, parent=dlg)

            def _on_done(result, assigned):
                lines = [f"拆分完成！共分配 {assigned} 條："]
                for acc, path in result.items():
                    lines.append(f"  {acc} → {Path(path).name}")
                lines.append(f"test.xlsx 剩餘 {total_rows - assigned} 條")
                messagebox.showinfo("拆分結果", "\n".join(lines), parent=dlg)
                dlg.destroy()
                self._refresh_files()

            threading.Thread(target=_run, daemon=True).start()

        btn_query = ttk.Button(param_frame, text="查詢勾選商品數", command=_do_query)
        btn_query.pack(side="left")

        btn_cancel = ttk.Button(bottom, text="取消", command=dlg.destroy)
        btn_cancel.pack(side="right")
        btn_confirm = ttk.Button(bottom, style="Accent.TButton", text="確認拆分", command=_do_split)
        btn_confirm.pack(side="right", padx=(0, 6))
        btn_average = ttk.Button(bottom, text="平均分配", command=_do_average)
        btn_average.pack(side="right", padx=(0, 6))

    def _open_dir(self):
        try:
            # Windows
            os.startfile(self.var_dir.get())
        except Exception:
            try:
                import subprocess
                subprocess.Popen(["explorer", self.var_dir.get()])
            except Exception:
                pass

    def _refresh_files(self):
        # 清空 tree
        for i in self.tree.get_children(""):
            self.tree.delete(i)

        self._stats.clear()
        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        d.mkdir(parents=True, exist_ok=True)
        # 自动转换 CSV/XLS → XLSX
        for src in list(d.glob("*.csv")) + list(d.glob("*.xls")):
            if src.suffix.lower() in (".csv", ".xls") and not src.name.startswith("~$"):
                converted = _ensure_xlsx(src)
                if converted:
                    self.app.log(f"[PUBLISH] 已转换: {src.name} → {converted.name}")
        files = sorted([p for p in d.glob("*.xlsx") if not p.name.startswith("~$") and not p.name.startswith("本次") and not p.name.startswith("导出") and not p.stem.endswith("_done") and p.name not in ("成功汇总.xlsx", "失败汇总.xlsx") and p.stem != "test"])
        for p in files:
            acc = p.stem.strip()
            st = PublishStat(account=acc, excel=p.name)
            self._stats[acc] = st
            self.tree.insert("", "end", iid=acc, values=("\u2713", acc, 0, 0, "待机", ""))

        # 恢复上次未清理的刊登记录
        self._restore_publish_stats()

        # 检测是否有上次暫停的狀態 → 恢復顯示（延迟500ms，不阻塞 UI 初始化）
        self.app.after(500, self._restore_pause_display)

        self.app.after(100, self._update_btn_states)

        self.app.log(f"[PUBLISH] 已发现 {len(files)} 个 Excel：{d}")

    def _refresh_files_incremental(self):
        """累计模式：保留已完成的记录，只追加新发现的 xlsx 文件。
        重复账号的上批成绩存入 base_success/base_fail 作为累加基数。"""
        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        d.mkdir(parents=True, exist_ok=True)
        # 自动转换 CSV/XLS → XLSX
        for src in list(d.glob("*.csv")) + list(d.glob("*.xls")):
            if src.suffix.lower() in (".csv", ".xls") and not src.name.startswith("~$"):
                _ensure_xlsx(src)
        files = sorted([p for p in d.glob("*.xlsx") if not p.name.startswith("~$") and not p.name.startswith("本次") and not p.name.startswith("导出") and not p.stem.endswith("_done") and p.name not in ("成功汇总.xlsx", "失败汇总.xlsx") and p.stem != "test"])

        new_count = 0
        for p in files:
            acc = p.stem.strip()
            if acc in self._stats:
                st = self._stats[acc]
                if st.ended_at:
                    # 上批已完成，本批又有同名 xlsx
                    # 把当前成绩存为 base，新一轮在此基础上累加
                    st.base_success = st.success
                    st.base_fail = st.fail
                    st.running = False
                    st.ended_at = ""
                    st.last_error = ""
                    st.excel = p.name
                    try:
                        self.tree.item(acc, values=(
                            "\u2713", acc, st.success, st.fail, "待机", "",
                        ))
                    except Exception:
                        pass
                continue
            st = PublishStat(account=acc, excel=p.name)
            self._stats[acc] = st
            self.tree.insert("", "end", iid=acc, values=("\u2713", acc, 0, 0, "待机", ""))
            new_count += 1

        self.app.log(f"[PUBLISH] 累计模式：新增 {new_count} 个账号，保留 {len(self._stats) - new_count} 个历史记录")

    def _on_sel_click(self, event):
        """点击 sel 列切换单行勾选。"""
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            # 可能点了表头，由 heading command 处理
            return
        try:
            vals = list(self.tree.item(row_id, "values"))
            vals[0] = "" if str(vals[0]) == "\u2713" else "\u2713"
            self.tree.item(row_id, values=vals)
        except Exception:
            pass

    def _toggle_select_all(self):
        """表头全选/全不选切换。"""
        self._all_selected = not self._all_selected
        mark = "\u2713" if self._all_selected else ""
        self.tree.heading("sel", text="\u2611" if self._all_selected else "\u2610")
        for iid in self.tree.get_children(""):
            try:
                vals = list(self.tree.item(iid, "values"))
                vals[0] = mark
                self.tree.item(iid, values=vals)
            except Exception:
                pass

    def _get_summary_row_count(self, filename: str) -> int:
        """获取汇总文件当前行数。"""
        import openpyxl
        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        p = d / filename
        if not p.exists():
            return 0
        try:
            wb = openpyxl.load_workbook(p, read_only=True)
            count = wb.active.max_row
            wb.close()
            return count
        except Exception:
            return 0

    def _open_batch_summary(self, filename: str) -> None:
        """导出汇总记录。从 _batch_summary_start 起始行之后的行导出。
        累计模式下 _batch_summary_start 跨批次不重置，所以自然导出多批次累积数据。"""
        import openpyxl
        import tempfile
        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        src = d / filename
        if not src.exists():
            messagebox.showinfo("提示", f"{filename} 不存在")
            return

        start_row = self._batch_summary_start.get(filename, 0)

        try:
            wb = openpyxl.load_workbook(src)
            ws = wb.active
            total = ws.max_row
            if total <= start_row:
                label = "成功" if "成功" in filename else "失败"
                messagebox.showinfo("提示", f"无{label}记录")
                wb.close()
                return

            # 创建临时文件，只包含表头 + 批次新增行
            new_wb = openpyxl.Workbook()
            new_ws = new_wb.active
            label = "成功" if "成功" in filename else "失败"
            new_ws.title = f"导出{label}"

            # 复制表头（第1行）
            for col in range(1, ws.max_column + 1):
                new_ws.cell(row=1, column=col, value=ws.cell(row=1, column=col).value)

            # 复制批次行
            out_row = 2
            for r in range(start_row + 1, total + 1):
                for col in range(1, ws.max_column + 1):
                    new_ws.cell(row=out_row, column=col, value=ws.cell(row=r, column=col).value)
                out_row += 1

            wb.close()

            # 保存到临时文件并打开（放到系统临时目录，不污染刊登目录）
            tmp = tempfile.NamedTemporaryFile(
                suffix=".xlsx", prefix=f"导出{label}_",
                delete=False,
            )
            tmp.close()
            new_wb.save(tmp.name)
            new_wb.close()
            os.startfile(tmp.name)
        except Exception as e:
            messagebox.showerror("错误", f"打开失败: {e}")

    def _on_cumulative_changed(self, *_args):
        """累计记录 checkbox 变化时持久化到 settings.json。"""
        try:
            if self.app:
                self.app.settings["publish_cumulative"] = self._var_cumulative.get()
                from core.accounts import save_settings
                save_settings(self.app.settings)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 操作记录历史
    # ------------------------------------------------------------------

    _HISTORY_FILE = "publish_history.json"

    def _append_history(self, trigger: str):
        """记录一次刊登操作到历史文件。trigger='手动刊登' 或 '定时刊登 19:30 ...'。"""
        import json
        from datetime import datetime
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            p = d / self._HISTORY_FILE

            # 构建本次记录（只记录本批次新增的，减去累计基数）
            accounts = []
            for acc, st in self._stats.items():
                if st.ended_at:
                    # 使用 _history_base（本批次起始基數），而非 base_success（暫停恢復可能調整過）
                    _hb_s = getattr(st, "_history_base_success", st.base_success)
                    _hb_f = getattr(st, "_history_base_fail", st.base_fail)
                    batch_succ = st.success - _hb_s
                    batch_fail = st.fail - _hb_f
                    if batch_succ == 0 and batch_fail == 0:
                        continue  # 本批次没有执行的账号不记录
                    accounts.append({
                        "account": st.account,
                        "success": batch_succ,
                        "fail": batch_fail,
                        "error": st.last_error or "",
                    })
            if not accounts:
                return

            total_success = sum(a["success"] for a in accounts)
            total_fail = sum(a["fail"] for a in accounts)
            record = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": trigger,
                "accounts": accounts,
                "total_success": total_success,
                "total_fail": total_fail,
            }

            # 追加到历史文件
            history = []
            if p.exists():
                try:
                    history = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
            history.append(record)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _show_history(self):
        """弹窗显示操作历史记录。"""
        import json
        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        p = d / self._HISTORY_FILE

        history = []
        if p.exists():
            try:
                history = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 倒序：最新的在上面
        history_rev = list(reversed(history))

        win = tk.Toplevel(self.frame)
        win.title("刊登操作记录")
        win.geometry("750x480")
        win.transient(self.frame.winfo_toplevel())

        # 顶部工具栏
        toolbar = ttk.Frame(win)
        toolbar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(toolbar, text=f"共 {len(history)} 条记录").pack(side="left")
        ttk.Label(toolbar, text="💡 右键点击可查看账号明细",
                  foreground="gray").pack(side="left", padx=12)
        ttk.Button(toolbar, text="清空记录", command=lambda: self._clear_history(win, tree)).pack(side="right", padx=4)

        # Treeview
        cols = ("time", "trigger", "accounts", "success", "fail")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=18)
        tree.heading("time", text="时间")
        tree.heading("trigger", text="触发方式")
        tree.heading("accounts", text="账号明细")
        tree.heading("success", text="成功")
        tree.heading("fail", text="失败")
        tree.column("time", width=140, stretch=False)
        tree.column("trigger", width=100, stretch=False)
        tree.column("accounts", width=320, minwidth=150, stretch=True)
        tree.column("success", width=55, anchor="center", stretch=False)
        tree.column("fail", width=55, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        vsb.pack(side="right", fill="y", padx=(0, 6), pady=(0, 6))

        # 填充数据
        _iid_map = {}  # iid -> history record index
        for idx, rec in enumerate(history_rev):
            t = rec.get("time", "")
            trigger = rec.get("trigger", "")
            accs = rec.get("accounts", [])
            details = ", ".join(
                f"{a['account'].split('@')[0]}:{a['success']}/{a['fail']}"
                for a in accs
            )
            iid = tree.insert("", "end", values=(
                t, trigger, details,
                rec.get("total_success", 0),
                rec.get("total_fail", 0),
            ))
            _iid_map[iid] = idx

        # 右键菜单
        ctx_menu = tk.Menu(tree, tearoff=0)
        ctx_menu.add_command(label="查看账号明细",
                             command=lambda: self._show_history_detail(win, history_rev, tree, _iid_map))

        def _on_right_click(event):
            row_id = tree.identify_row(event.y)
            if row_id:
                tree.selection_set(row_id)
                ctx_menu.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", _on_right_click)

    def _clear_history(self, win, tree):
        """清空操作历史。"""
        import json
        if not messagebox.askyesno("确认", "确定要清空所有操作记录吗？", parent=win):
            return
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            p = d / self._HISTORY_FILE
            if p.exists():
                p.unlink()
        except Exception:
            pass
        for i in tree.get_children(""):
            tree.delete(i)

    def _show_history_detail(self, parent_win, history_rev, tree, iid_map):
        """右键查看单条记录的账号明细。"""
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        idx = iid_map.get(iid)
        if idx is None:
            return
        rec = history_rev[idx]

        t = rec.get("time", "")
        trigger = rec.get("trigger", "")
        accs = rec.get("accounts", [])

        detail_win = tk.Toplevel(parent_win)
        detail_win.title(f"账号明细 — {t}")
        detail_win.geometry("520x360")
        detail_win.transient(parent_win)

        # 头部信息
        hdr = ttk.Frame(detail_win)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(hdr, text=f"时间: {t}    触发: {trigger}    "
                       f"总计: 成功 {rec.get('total_success', 0)}  "
                       f"失败 {rec.get('total_fail', 0)}").pack(anchor="w")

        # 账号明细表格
        cols2 = ("account", "success", "fail", "error")
        dt = ttk.Treeview(detail_win, columns=cols2, show="headings", height=12)
        dt.heading("account", text="账号")
        dt.heading("success", text="成功")
        dt.heading("fail", text="失败")
        dt.heading("error", text="错误信息")
        dt.column("account", width=220, stretch=True)
        dt.column("success", width=60, anchor="center", stretch=False)
        dt.column("fail", width=60, anchor="center", stretch=False)
        dt.column("error", width=150, stretch=True)

        vsb2 = ttk.Scrollbar(detail_win, orient="vertical", command=dt.yview)
        dt.configure(yscrollcommand=vsb2.set)
        dt.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        vsb2.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))

        for a in accs:
            dt.insert("", "end", values=(
                a.get("account", ""),
                a.get("success", 0),
                a.get("fail", 0),
                a.get("error", ""),
            ))

    def _ui_update_stat(self, acc: str):
        st = self._stats.get(acc)
        if not st:
            return
        # 说明：勾选「自动清理已完成」或手动删除行后，仍可能有 after() 回调在队列里；
        # 这时 Treeview 里已经没有该 iid 了，会抛 TclError（你日志里看到的 Item xian678 not found）。
        if not getattr(self, "tree", None):
            return
        try:
            if hasattr(self.tree, "exists") and (not self.tree.exists(acc)):
                return
        except Exception:
            # 某些环境下 exists() 不可用/异常，继续走下面的 try/except
            pass
        try:
            # 保持 sel 列的当前勾选状态
            cur_sel = ""
            try:
                cur_vals = self.tree.item(acc, "values")
                if cur_vals:
                    cur_sel = str(cur_vals[0])
            except Exception:
                cur_sel = "\u2713"
            # 判斷狀態文字
            _pd = getattr(st, "_paused_display", False)
            if _pd == "waiting" and st.running:
                _status_txt = "暫停中..."
            elif _pd and st.running:
                _status_txt = "已暫停"
            elif st.running:
                _status_txt = "刊登中"
            elif st.ended_at:
                _status_txt = "完成"
            else:
                _status_txt = "待机"
            self.tree.item(acc, values=(
                cur_sel,
                st.account,
                st.success,
                st.fail,
                _status_txt,
                st.last_error or "",
            ))
        except Exception:
            # TclError 或其他 UI 竞态 -> 忽略
            return
    def _remove_selected_excels(self):
        """把列表中勾选的 Excel(以及对应 _done) 移到 publish_excels/_removed，方便清理误放/误选。
        不会永久删除。
        """
        import tkinter.messagebox as messagebox
        sel = [iid for iid in self.tree.get_children("")
               if str(self.tree.item(iid, "values")[0]) == "\u2713"]
        if not sel:
            messagebox.showinfo('提示', '請先勾選要刪除的帳號')
            return
        d = Path(self.var_dir.get().strip() or PUBLISH_DIR)
        removed_dir = d / '_removed'
        removed_dir.mkdir(parents=True, exist_ok=True)

        moved = 0
        for iid in sel:
            vals = self.tree.item(iid, 'values')
            if not vals:
                continue
            excel_name = str(vals[1]).strip() + ".xlsx"
            if not excel_name:
                continue
            src = d / excel_name
            done = d / f'{src.stem}_done{src.suffix}'

            for p in [src, done]:
                try:
                    if p.exists():
                        dst = removed_dir / p.name
                        if dst.exists():
                            # 避免覆盖
                            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                            dst = removed_dir / f'{p.stem}_{ts}{p.suffix}'
                        shutil.move(str(p), str(dst))
                except Exception as e:
                    self.app.log(f'[PUBLISH] 刪除失敗: {p} -> {e}')

            # 从界面移除
            try:
                self.tree.delete(iid)
            except Exception:
                pass

            # 从内存移除
            acc = None
            for k, st in list(self._stats.items()):
                if st.excel == excel_name:
                    acc = k
                    break
            if acc and acc in self._stats:
                self._stats.pop(acc, None)

            moved += 1

        self.app.log(f'[PUBLISH] 已移除 {moved} 个 Excel（移动到 {removed_dir}）')

        # 同步更新持久化文件（防止重啟後恢復已刪除的記錄）
        if moved > 0:
            self._save_publish_stats()
            # 如果有暫停狀態，也要同步清理已刪除的帳號
            _ps = self._load_pause_state()
            if _ps:
                _ps_accounts = _ps.get("accounts", {})
                _changed = False
                for iid in sel:
                    _vals = None
                    try:
                        _vals = self.tree.item(iid, "values")
                    except Exception:
                        pass
                    _acc_name = str(iid)
                    if _acc_name in _ps_accounts:
                        del _ps_accounts[_acc_name]
                        _changed = True
                if _changed:
                    if _ps_accounts:
                        _ps["accounts"] = _ps_accounts
                        try:
                            import json as _json
                            fp = d / self._PAUSE_STATE_FILE
                            fp.write_text(_json.dumps(_ps, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                    else:
                        self._delete_pause_state()
            self.app.after(0, self._update_btn_states)

    def _after_finish_account(self, acc_name: str):
        """單帳號跑完後：更新 UI（保留记录供回顾）+ 持久化。

        v6.1.62-fix:這個函數其實在 publish 開始 (L5152) + 結束 (L5307) 都被呼叫,
        是 UI update + persist 用,不該在這釋放 hold(否則 publish 開始時就 release)。
        改成只看 st.ended_at:有值 = 真結束,才 release hold。
        """
        self._ui_update_stat(acc_name)
        self._save_publish_stats()
        # v6.1.62-fix:只在「真結束」(st.ended_at 有值)時釋放 monitor hold
        try:
            st = self._stats.get(acc_name)
            if st is None or not getattr(st, "ended_at", ""):
                # publish 還沒真結束(只是中間 UI update) → 不釋放 hold
                return

            mon = getattr(self.app, "mon", None)
            if mon is None:
                return
            # 透過 acc_name 找到 profile_id
            state = None
            for s in self.app.states.values():
                if (s.name or "").strip() == acc_name:
                    state = s
                    break
            if state is None:
                return
            pid = state.profile_id
            # set_hold 是 async,從 sync context 排到 app 的 loop 上跑(monitor 也跑在 app.loop)
            try:
                _loop = getattr(self.app, "loop", None)
                if _loop is not None and _loop.is_running():
                    import asyncio as _asyncio
                    _asyncio.run_coroutine_threadsafe(
                        mon.set_hold(pid, False, reason="publish_running"),
                        _loop,
                    )
                    self.app.log(f"[PUBLISH] {acc_name}: 已釋放 monitor hold(publish 結束)")
            except Exception as _e_async:
                self.app.log(f"[PUBLISH] {acc_name}: 釋放 monitor hold 失敗(忽略): {_e_async}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 刊登记录持久化（JSON 文件保存/恢复）
    # ------------------------------------------------------------------

    _STATS_FILE = "publish_stats.json"

    def _save_publish_stats(self):
        """将当前刊登记录保存到 JSON 文件。"""
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            p = d / self._STATS_FILE
            records = []
            for acc, st in self._stats.items():
                if st.ended_at:  # 只保存已完成的
                    records.append({
                        "account": st.account,
                        "success": st.success,
                        "fail": st.fail,
                        "ended_at": st.ended_at,
                        "last_error": st.last_error or "",
                    })
            if records:
                import json
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(p)
            else:
                # 沒有記錄了 → 刪除舊文件（否則重啟後會恢復已刪除的帳號）
                if p.exists():
                    p.unlink()
        except Exception:
            pass

    def _restore_publish_stats(self):
        """从 JSON 文件恢复上次的刊登记录（已完成的账号）。"""
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            p = d / self._STATS_FILE
            if not p.exists():
                return
            import json
            records = json.loads(p.read_text(encoding="utf-8"))
            for rec in records:
                acc = rec.get("account", "")
                if not acc or acc in self._stats:
                    continue  # 磁盘上有 xlsx 的优先（已在 _refresh_files 中加载）
                st = PublishStat(
                    account=acc,
                    excel=acc + ".xlsx",
                    success=rec.get("success", 0),
                    fail=rec.get("fail", 0),
                    ended_at=rec.get("ended_at", "done"),
                    last_error=rec.get("last_error", ""),
                )
                self._stats[acc] = st
                self.tree.insert("", "end", iid=acc, values=(
                    "\u2713", acc, st.success, st.fail, "完成", st.last_error or "",
                ))
        except Exception:
            pass

    def _restore_pause_display(self):
        """啟動時若存在 publish_pause_state.json，在表格中恢復暫停進度顯示。
        先用 saved_succ 快速显示 UI，后台线程读取 Excel 计算准确值再更新。"""
        try:
            ps = self._load_pause_state()
            if not ps:
                return
            accounts = ps.get("accounts", {})
            if not accounts:
                return
            paused_at = ps.get("paused_at", "")

            # 软件刚启动时tree未初始化，跳过UI恢复但保留暂停记录
            if not hasattr(self, 'tree') or self.tree is None:
                self.app.log(f"[PUBLISH] 检测到上次暂停记录但tree未初始化，跳过UI恢复")
                return

            _d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            self.app.log(f"[PUBLISH] 檢測到上次暫停記錄 ({paused_at})，恢復顯示...")

            # ── 第一阶段：立即用 saved_succ 显示 UI（不读 Excel，不阻塞）──
            for acc, info in accounts.items():
                _saved_succ = info.get("success", 0)
                fail = info.get("fail", 0)
                if acc in self._stats:
                    st = self._stats[acc]
                    st.success = _saved_succ
                    st.fail = fail
                    st.base_success = info.get("base_success", _saved_succ)
                    st.base_fail = 0
                    try:
                        self.tree.item(acc, values=(
                            "\u2713", acc, _saved_succ, fail, "已暫停", "",
                        ))
                    except Exception:
                        pass
                else:
                    st = PublishStat(account=acc, excel=acc + ".xlsx")
                    st.success = _saved_succ
                    st.fail = fail
                    st.base_success = info.get("base_success", _saved_succ)
                    st.base_fail = 0
                    self._stats[acc] = st
                    try:
                        self.tree.insert("", "end", iid=acc, values=(
                            "\u2713", acc, _saved_succ, fail, "已暫停", "",
                        ))
                    except Exception:
                        pass

            # ── 第二阶段：后台线程读取 Excel，计算准确完成数后更新 UI ──
            def _recount_in_background():
                try:
                    for acc, info in accounts.items():
                        _saved_succ = info.get("success", 0)
                        fail = info.get("fail", 0)
                        _excel_count = self._count_completed_in_excel(_d, acc)
                        if _excel_count <= _saved_succ:
                            continue  # saved 已经更大，无需更新
                        succ = _excel_count
                        # 通过 app.after 在 UI 线程更新
                        def _update(a=acc, s=succ, f=fail):
                            try:
                                if a in self._stats:
                                    self._stats[a].success = s
                                    self._stats[a].base_success = s
                                self.tree.item(a, values=(
                                    "\u2713", a, s, f, "已暫停", "",
                                ))
                            except Exception:
                                pass
                        try:
                            self.app.after(0, _update)
                        except Exception:
                            pass
                except Exception as e:
                    try:
                        self.app.log(f"[PUBLISH] 后台重算 Excel 完成数失败: {e}")
                    except Exception:
                        pass

            import threading
            threading.Thread(target=_recount_in_background, daemon=True).start()
        except Exception:
            pass

    def _clear_publish_stats_file(self):
        """清除持久化文件（新批次开始时调用）。"""
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            p = d / self._STATS_FILE
            if p.exists():
                p.unlink()
        except Exception:
            pass

    @staticmethod
    def _count_completed_in_excel(excel_dir: Path, acc: str) -> int:
        """計算 Excel 中已有商品編號的行數（實際已完成刊登數）。"""
        try:
            import openpyxl as _xl
            # 優先讀 _done.xlsx（刊登過程中寫入的工作副本）
            _done = excel_dir / f"{acc}_done.xlsx"
            _orig = excel_dir / f"{acc}.xlsx"
            _path = _done if _done.exists() else _orig
            if not _path.exists():
                return 0
            _wb = _xl.load_workbook(_path, read_only=True)
            _ws = _wb.active
            _headers = {_norm_str(_ws.cell(row=1, column=c).value): c
                        for c in range(1, (_ws.max_column or 0) + 1)
                        if _ws.cell(row=1, column=c).value}
            _col = _headers.get("商品編號", 0)
            if not _col:
                _wb.close()
                return 0
            _count = 0
            for _r in range(2, (_ws.max_row or 1) + 1):
                _v = _ws.cell(row=_r, column=_col).value
                if _v and str(_v).strip():
                    _count += 1
            _wb.close()
            return _count
        except Exception:
            return 0


    def start(self):
        if self._future is not None and not self._future.done():
            return

        # ── 檢測是否從暫停狀態恢復 ──
        _resume_state = self._load_pause_state()
        if _resume_state:
            _resume_dir = _resume_state.get("excel_dir", "")
            if _resume_dir:
                self.var_dir.set(_resume_dir)
            self._trigger_label = _resume_state.get("trigger_label", "恢復刊登")
            self.app.log(f"[PUBLISH] 從暫停狀態恢復 (暫停於 {_resume_state.get('paused_at', '?')})")
            # 恢復帳號統計
            # v6.0.70 修補:Excel 實際 count 改背景 thread,避免慢電腦 UI freeze
            #   - Phase 1(同步):用 saved success 立刻設 base,UI 馬上回應
            #   - Phase 2(背景):算 Excel 實際完成數 → 若 > saved 才校準 base_success
            #   不影響刊登邏輯(刊登靠 Excel 行的「商品編號」是否填過跳過,base 只影響顯示)
            _d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            _acc_saved_map = {}  # 給背景 thread 用
            for acc, info in _resume_state.get("accounts", {}).items():
                _saved_success = info.get("success", 0)
                _history_base_s = info.get("history_base_success", info.get("base_success", 0))
                _history_base_f = info.get("history_base_fail", info.get("base_fail", 0))
                _acc_saved_map[acc] = _saved_success
                # Phase 1:用 saved success 立刻當 base(快)
                if acc in self._stats:
                    st = self._stats[acc]
                    st.base_success = _saved_success
                    st.base_fail = 0  # ★ 失敗行無商品編號會被重跑，base_fail=0 防止重複計數
                    st._history_base_success = _history_base_s
                    st._history_base_fail = _history_base_f
                else:
                    _st = PublishStat(
                        account=acc, excel=acc + ".xlsx",
                        base_success=_saved_success,
                        base_fail=0,  # ★ 同上
                    )
                    _st._history_base_success = _history_base_s
                    _st._history_base_fail = _history_base_f
                    self._stats[acc] = _st
            self._delete_pause_state()

            # Phase 2:背景 thread 算 Excel 實際完成數,有需要才校準
            # 慢電腦 N 個賬號 × 數千行 Excel iterate 在主 thread 會 freeze app,丟背景跑
            def _bg_correct_base_success(_dir=_d, _saved_map=dict(_acc_saved_map)):
                import threading as _th
                _corrected = 0
                for _acc, _saved in _saved_map.items():
                    try:
                        _actual = self._count_completed_in_excel(_dir, _acc)
                        if _actual > _saved:
                            # 必須在 UI thread 改 stats(避免跟 ui_update 競態)
                            def _apply(_a=_acc, _new=_actual, _old=_saved):
                                try:
                                    if _a in self._stats:
                                        self._stats[_a].base_success = _new
                                        self.app.log(f"[PUBLISH] 校準 {_a}: base_success {_old} → {_new}")
                                        # 觸發 UI 重繪(若有對應 method)
                                        try:
                                            self._ui_update_stat(_a)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            try:
                                self.app.after(0, _apply)
                                _corrected += 1
                            except Exception:
                                pass
                    except Exception:
                        pass
                if _corrected:
                    try:
                        self.app.after(0, lambda c=_corrected: self.app.log(
                            f"[PUBLISH] 背景校準完成,校正了 {c} 個賬號的 base_success"))
                    except Exception:
                        pass

            try:
                import threading as _th_mod
                _th_mod.Thread(target=_bg_correct_base_success, daemon=True,
                               name="publish-resume-count").start()
            except Exception as _e:
                # 起 thread 失敗就退回同步(罕見)
                self.app.log(f"[PUBLISH] 背景 count thread 啟動失敗,退回同步: {_e}")
                _bg_correct_base_success()

        # 触发来源标记（定时刊登会在调用前设置 _trigger_label）
        if not hasattr(self, "_trigger_label") or not self._trigger_label:
            self._trigger_label = "手动刊登"

        d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
        if not d.exists():
            messagebox.showerror("错误", f"资料夹不存在：{d}")
            return

        try:
            conc = int(self.var_conc.get().strip() or "3")
            conc = max(1, min(10, conc))
        except Exception:
            messagebox.showerror("错误", "并发账号必须是数字")
            return
        # 持久化到 settings.json
        if self.app:
            self.app.settings["publish_concurrency"] = conc
            from core.accounts import save_settings
            save_settings(self.app.settings)

        chrome = (getattr(self.app, 'var_browser', None).get().strip() if getattr(self.app, 'var_browser', None) else '') or self.app.settings.get('browser_path','') or ''
        if not chrome or (not os.path.exists(chrome)):
            messagebox.showerror("错误", f"找不到 chrome.exe：{chrome}\n请在『监控』页填写正确 Chrome 路径。")
            return

        # persist
        self.app.settings["browser_path"] = chrome
        try:
            from core.accounts import save_settings
            save_settings(self.app.settings)
        except Exception:
            pass

        self._stop.clear()
        self._pause.set()  # 確保不處於暫停狀態
        # 清掉上一批次留下的 403037 熔断标记（用户重启就当 VPN 已修好）
        clear_vpn_down()
        self.app.after(0, self._update_btn_states)
        headless = True
        step_delay = 0.4
        upload_wait_s = 6.0
        retry_pending_review = True
        debug_steps = False
        save_html = False
        humanize_cfg = HumanizeConfig(
            enabled=True,
            step_jitter=0.25,
            action_pre_delay_min=0.18,
            action_pre_delay_max=0.85,
            action_post_delay_min=0.10,
            action_post_delay_max=0.55,
            click_hold_ms_min=35,
            click_hold_ms_max=120,
            type_delay_ms_min=35,
            type_delay_ms_max=95,
            long_pause_prob=0.08,
            long_pause_min=1.30,
            long_pause_max=4.80,
            micro_move_prob=0.25,
            micro_scroll_prob=0.0,
            type_max_chars=120,
        )

        # 记录汇总表当前行数（用于导出成功/失败按钮）
        cumulative = self._var_cumulative.get()
        # 從暫停恢復時強制使用累計模式（保留上次進度）
        if _resume_state:
            cumulative = True
        if cumulative and self._batch_summary_start:
            # 累计模式且已有起始行号：保留之前的起始点，不重置
            pass
        else:
            self._batch_summary_start = {
                "成功汇总.xlsx": self._get_summary_row_count("成功汇总.xlsx"),
                "失败汇总.xlsx": self._get_summary_row_count("失败汇总.xlsx"),
            }

        if cumulative:
            # 累计模式：保留已完成的记录，只追加新的 xlsx 账号
            self._refresh_files_incremental()
        else:
            # 非累计：清除旧记录，重新扫描
            self._clear_publish_stats_file()
            self._refresh_files()

        # 記錄本批次的起始 base（用於操作歷史計算，不受暫停恢復影響）
        for _acc, _st in self._stats.items():
            if not hasattr(_st, "_history_base_success"):
                _st._history_base_success = _st.base_success
                _st._history_base_fail = _st.base_fail

        # 文件收集（在 refresh 之后，确保新增账号已在 tree 中）
        _is_xlsx = lambda p: p.exists() and not p.name.startswith("~$") and not p.name.startswith("本次") and not p.name.startswith("导出") and not p.stem.endswith("_done") and p.name not in ("成功汇总.xlsx", "失败汇总.xlsx") and p.stem != "test"
        files = []
        try:
            if getattr(self, "tree", None):
                for iid in self.tree.get_children(""):
                    try:
                        vals = self.tree.item(iid, "values")
                        if not vals or str(vals[0]) != "\u2713":
                            continue
                        acc_name = str(vals[1]) if len(vals) > 1 else str(iid)
                    except Exception:
                        acc_name = str(iid)
                    p = d / (acc_name + ".xlsx")
                    if _is_xlsx(p):
                        files.append(p)
        except Exception:
            pass
        if not files:
            files = sorted([p for p in d.glob("*.xlsx") if _is_xlsx(p)])
        if not files:
            messagebox.showinfo("提示", "资料夹没有 xlsx：" + str(d))
            return

        # ── Cookie 注入检查：仅记录日志，不阻拦 ──
        _no_cookie_accounts = []
        for _f in files:
            _acc = _f.stem.strip()
            _st = None
            for _s in self.app.states.values():
                if (_s.name or "").strip() == _acc:
                    _st = _s
                    break
            if _st:
                _pdir = ROOT_DIR / "profiles" / _st.profile_id
                _rc = load_raw_cookies(_pdir, max_age=2592000)
                # v6.2:cache 沒 raw_cookies 但 SQLite 有 → 不算「無 cookie 帳號」(後續會自動 fallback)
                if not _rc:
                    try:
                        _flat_sql_chk, _ = load_from_chrome_sqlite_yahoo(_pdir)
                        if _flat_sql_chk and len(_flat_sql_chk) >= 5:
                            _rc = True  # 標記有(避免誤報)
                    except Exception:
                        pass
                if not _rc:
                    _no_cookie_accounts.append(_acc)
        if _no_cookie_accounts:
            self.app.log(f"[PUBLISH] {len(_no_cookie_accounts)} 个账号无 Cookie 缓存，将使用 Profile 模式: {', '.join(_no_cookie_accounts[:10])}")

        async def _runner():
            sem = asyncio.Semaphore(conc)
            _goto_lock = asyncio.Lock()  # 全局鎖：同一時間只有一個帳號在 goto Yahoo，避免 upstream error
            _launch_lock = asyncio.Lock()  # Chrome 启动交错锁：确保两次启动间隔 >=8-15 秒（随机抖动）
            _last_launch_ts = 0.0  # 上一次 Chrome 启动的时间戳

            # 共享一个 Playwright 实例（所有账号共用一个 node.js 进程，节省大量内存）
            _shared_pw = await async_playwright().start()

            async def _run_one(xlsx: Path):
                acc_name = xlsx.stem.strip()
                self.app.log(f"[PUBLISH] {acc_name}: 任务启动")
                st = self._stats.get(acc_name)
                if not st:
                    st = PublishStat(account=acc_name, excel=xlsx.name)
                    self._stats[acc_name] = st

                # 用檔名匹配帳號
                state = None
                for s in self.app.states.values():
                    if (s.name or "").strip() == acc_name:
                        state = s
                        break
                if state is None:
                    st.last_error = "找不到对应账号（Excel 檔名需等于账号名）"
                    self.app.after(0, lambda: self._after_finish_account(acc_name))
                    self.app.log(f"[PUBLISH] {acc_name}: 找不到账号，跳过")
                    return

                pid = state.profile_id
                proxy = getattr(state, "proxy", "") or ""
                profile_dir = ROOT_DIR / "profiles" / pid
                profile_dir.mkdir(parents=True, exist_ok=True)
                debug_dir = profile_dir / "debug"

                # v6.1.17:純 HTTP 模式 cookie_cache.json 原子寫,監控+刊登可並行讀
                # 保留 set_hold(告訴監控暫停下輪 poll,避免持續寫 cache),但去掉 wait_idle 不等
                mon = getattr(self.app, "mon", None)
                if mon:
                    await mon.set_hold(pid, True, reason="publish_cookie")
                    # v6.1.61:整個 publish 期間 hold monitor 該帳號 — 避免 publish 高頻打 Yahoo
                    # 同時 monitor 也打 myauc → Yahoo 看到「同 cookies + 同 fingerprint」過熱 →
                    # monitor poll myauc 撞 5xx 被標「異常」
                    # publish 結束(_after_finish_account)會釋放 publish_running hold
                    await mon.set_hold(pid, True, reason="publish_running")

                # 清理该账号残留的临时 profile
                for _old in ROOT_DIR.joinpath("profiles").glob(f"{pid}_pub_*"):
                    try:
                        shutil.rmtree(_old, ignore_errors=True)
                    except Exception:
                        pass

                # ── 纯 HTTP 模式：从 cookie cache 读取 cookie ──
                _raw_cookies = load_raw_cookies(profile_dir, max_age=2592000)
                # v6.2:cookie_cache.json 不存在(剛遠程登錄完還沒寫 cache)→ 直接從真實 profile 的
                # Chrome SQLite 強讀 yahoo cookies。raw 已經是 [{name,value,domain,path,...}] 格式,
                # 不需轉換。避免「新登錄帳號被傳空 tmp_profile 進 publish 必失敗」死鎖。
                if not _raw_cookies:
                    try:
                        _flat_sql, _raw_sql = load_from_chrome_sqlite_yahoo(profile_dir)
                        if _raw_sql and _flat_sql and len(_flat_sql) >= 5:
                            _raw_cookies = _raw_sql
                            # 順手把 SQLite 讀到的寫進 cookie_cache.json,下次直接走 fast path
                            try:
                                save_cookie_cache(profile_dir, _flat_sql, "", raw_cookies=_raw_sql)
                                self.app.log(f"[PUBLISH] {acc_name}: SQLite 強讀 {len(_flat_sql)} cookies 並寫入 cache")
                            except Exception:
                                self.app.log(f"[PUBLISH] {acc_name}: SQLite 強讀 {len(_flat_sql)} cookies(cache 寫入失敗)")
                    except Exception as _e_sql:
                        self.app.log(f"[PUBLISH] {acc_name}: SQLite fallback 異常: {_e_sql}")

                tmp_profile = ROOT_DIR / "profiles" / f"{pid}_pub_{int(time.time())}"
                tmp_profile.mkdir(parents=True, exist_ok=True)

                if _raw_cookies:
                    _yahoo_n = len([c for c in _raw_cookies if "yahoo" in c.get("domain", "").lower()])
                    self.app.log(f"[PUBLISH] {acc_name}: cookie注入模式 (yahoo_cookies={_yahoo_n})")
                else:
                    # 无 inject_cookies，_run_publish_for_excel 会用 profile_dir 的 cookie_cache.json
                    self.app.log(f"[PUBLISH] {acc_name}: 无 raw cookies，将尝试 profile cookie_cache")

                # 读取完成，立即恢复该账号的监控调度
                if mon:
                    try:
                        await mon.set_hold(pid, False, reason="publish_cookie")
                    except Exception:
                        pass

                async with sem:
                    # ── Chrome 启动交错：两次启动间隔 8-15 秒随机抖动，避免 CPU 瞬间飙高导致空白页 ──
                    nonlocal _last_launch_ts
                    async with _launch_lock:
                        _min_gap = random.uniform(8, 15)
                        _gap = time.time() - _last_launch_ts
                        if _gap < _min_gap:
                            _wait = _min_gap - _gap
                            self.app.log(f"[PUBLISH] {acc_name}: 等待 {_wait:.0f}s（交错启动）")
                            await asyncio.sleep(_wait)
                        _last_launch_ts = time.time()

                    st.running = True
                    st.started_at = datetime.now().strftime("%m-%d %H:%M:%S")
                    st.ended_at = ""
                    st.last_error = ""
                    self.app.after(0, lambda: self._after_finish_account(acc_name))

                    try:
                        for _profile_try in range(5):
                            if _profile_try > 0:
                                self.app.log(f"[PUBLISH {_ts()}] {acc_name}: === 第{_profile_try+1}/5次重试 ===")
                            _try_start = time.time()
                            try:
                                await _run_publish_for_excel(
                                    app_log=self.app.log,
                                    stop_flag=self._stop,
                                    chrome_path=chrome,
                                    profile_dir=tmp_profile,
                                    xlsx_path=xlsx,
                                    account_name=acc_name,
                                    headless=headless,
                                    step_delay=step_delay,
                                    debug_steps=debug_steps,
                                    save_html=save_html,
                                    upload_wait_s=upload_wait_s,
                                    humanize_cfg=humanize_cfg,
                                    retry_pending_review=retry_pending_review,
                                    proxy=proxy,
                                    on_progress=lambda succ, fail, row, err="": self._on_progress(acc_name, succ, fail, row, err),
                                    debug_dir=debug_dir,
                                    goto_lock=_goto_lock,
                                    inject_cookies=_raw_cookies,
                                    pw_instance=_shared_pw,
                                    pause_event=self._pause,
                                    _pause_notify=self._on_account_paused,
                                )
                                break
                            except _BlankPageRetry as _bpr:
                                _try_elapsed = time.time() - _try_start
                                self.app.log(f"[PUBLISH {_ts()}] {acc_name}: 第{_profile_try+1}次空白，重建profile重试...")
                                if _profile_try >= 4:
                                    st.last_error = "页面空白，重试5次均失败"
                                    self.app.log(f"[PUBLISH {_ts()}] {acc_name}: {st.last_error}")
                                    break

                                # 每次都杀Chrome + 清理锁 + 重建profile
                                try:
                                    _pd = str(tmp_profile).replace("\\", "\\\\")
                                    _kr = subprocess.run(
                                        f'wmic process where "commandline like \'%{_pd}%\'" call terminate',
                                        shell=True, timeout=10, capture_output=True,
                                    )
                                except Exception as _ke:
                                    pass
                                await asyncio.sleep(3)
                                from .profile_lock import force_clear_all as _fclear
                                _fclear(tmp_profile)
                                # 重建profile
                                try:
                                    shutil.rmtree(tmp_profile, ignore_errors=True)
                                except Exception:
                                    pass
                                tmp_profile = ROOT_DIR / "profiles" / f"{pid}_pub_{int(time.time())}"
                                try:
                                    if _raw_cookies:
                                        # cookie注入模式：创建空profile即可
                                        tmp_profile.mkdir(parents=True, exist_ok=True)
                                        (tmp_profile / "Default").mkdir(exist_ok=True)
                                        (tmp_profile / "First Run").touch()
                                    else:
                                        # fallback: 复制profile（此时旧Chrome已被杀掉+清锁）
                                        if mon:
                                            # v6.1.17:不等 wait_idle,profile_lock.py 已 cover SQLite 衝突
                                            await mon.set_hold(pid, True, reason="publish_rebuild")
                                        loop = asyncio.get_event_loop()
                                        await loop.run_in_executor(None, _copy_login_profile, profile_dir, tmp_profile)
                                        _yahoo_n = await loop.run_in_executor(None, _verify_cookies, tmp_profile)
                                        if _yahoo_n < 10:
                                            raise RuntimeError(f"重建profile cookie不完整(yahoo={_yahoo_n})")
                                except Exception as _re_err:
                                    self.app.log(f"[PUBLISH {_ts()}] {acc_name}: 重建profile失败: {_re_err}")
                                    st.last_error = f"重建profile失败: {str(_re_err)[:100]}"
                                    break  # 不再重试，避免用损坏的profile启动Chrome导致OOM
                                # hold会在外层finally统一释放
                            except Exception as _pub_err:
                                _err_str = str(_pub_err)
                                # WinError 1455 = 内存/页面文件不足，可恢复（等其他Chrome释放内存）
                                _is_mem_err = "1455" in _err_str or "页面文件太小" in _err_str
                                if _is_mem_err and _profile_try < 4:
                                    self.app.log(f"[PUBLISH] {acc_name}: 内存不足(WinError 1455)，等待30s后重试(第{_profile_try+1}次)...")
                                    await asyncio.sleep(30)
                                    continue
                                st.last_error = _err_str[:200]
                                self.app.log(f"[PUBLISH] {acc_name}: 异常: {_pub_err}")
                                break
                    finally:
                        # 释放重建期间的监控hold（如果有）
                        if mon:
                            try:
                                await mon.set_hold(pid, False, reason="publish_rebuild")
                            except Exception:
                                pass
                        # 清理临时 profile
                        try:
                            shutil.rmtree(tmp_profile, ignore_errors=True)
                        except Exception:
                            pass
                        st.running = False
                        st.ended_at = datetime.now().strftime("%m-%d %H:%M:%S")

                        # 直接在异步线程做文件清理（不依赖 GUI 线程）
                        done_path = PUBLISH_DIR / f"{acc_name}_done.xlsx"
                        orig_path = PUBLISH_DIR / f"{acc_name}.xlsx"
                        if done_path.exists():
                            try:
                                _move_success_to_summary(done_path, acc_name, self.app.log)
                            except Exception as _se:
                                self.app.log(f"[PUBLISH] {acc_name}: 成功汇总异常: {_se}")
                            try:
                                _move_failure_to_summary(done_path, acc_name, self.app.log)
                            except Exception as _fe:
                                self.app.log(f"[PUBLISH] {acc_name}: 失败汇总异常: {_fe}")

                            # v6.0.48 fix: 检查 done 文件是否还有未刊登行
                            # 之前只在 self._stop.is_set()(user 主動停)時檢查 → 認證熔斷/403037 熔斷
                            # 也會走到這裡但 _stop 是 False → 直接 unlink → 未跑 row 永久丟失
                            # 修法:不論觸發原因都檢查,有 row 一律保留
                            _has_remaining = False
                            _remaining_count = 0
                            try:
                                _dwb = openpyxl.load_workbook(done_path)
                                _dws = _dwb.active
                                _has_remaining = _dws.max_row > 1
                                _remaining_count = max(0, _dws.max_row - 1) if _has_remaining else 0
                                _dwb.close()
                            except Exception:
                                # 讀不出來保守當成有資料,不刪
                                _has_remaining = True

                            if _has_remaining:
                                _stop_reason = "主动停止" if self._stop.is_set() else "异常停止/熔断"
                                self.app.log(f"[PUBLISH] {acc_name}: {_stop_reason} — 原文件保留(剩余 {_remaining_count} 行未刊登,可重新刊登)")
                            else:
                                try:
                                    done_path.unlink()
                                    self.app.log(f"[PUBLISH] 已删除: {done_path.name}")
                                except Exception as _de:
                                    self.app.log(f"[PUBLISH] 删除失败: {done_path.name} -> {_de}")
                        if orig_path.exists():
                            # v6.0.48 fix: orig 跟 done 一起保留判斷
                            # 只要 done 還有未跑 row 或 user 主動停,orig 都保留(讓 user 能重跑)
                            if _has_remaining or self._stop.is_set():
                                pass
                            else:
                                try:
                                    orig_path.unlink()
                                    self.app.log(f"[PUBLISH] 已删除: {orig_path.name}")
                                except Exception as _oe:
                                    self.app.log(f"[PUBLISH] 删除失败: {orig_path.name} -> {_oe}")

                        self.app.after(0, lambda: self._after_finish_account(acc_name))

            try:
                tasks = []
                for p in files:
                    tasks.append(asyncio.create_task(_run_one(p)))
                    await asyncio.sleep(6)  # 间隔6秒启动，避免同时请求Yahoo导致upstream error + 缓解内存压力
                if tasks:
                    # 后台任务：检测到全局 VPN 熔断 → TG 告警（一次性）
                    async def _vpn_watcher():
                        notified = False
                        while True:
                            await asyncio.sleep(2)
                            done_all = all(t.done() for t in tasks)
                            if is_vpn_down() and not notified:
                                notified = True
                                reason = get_vpn_down_reason()
                                self.app.log(f"[PUBLISH] ⚠ 全局 VPN 熔断：{reason}")
                                try:
                                    ops_bot = getattr(self.app, "_ops_tg_bot", None)
                                    if ops_bot:
                                        cid = str(getattr(self.app, "settings", {}).get("tg_chat_id", "")).strip()
                                        if cid:
                                            ops_bot.send_to(cid,
                                                f"⚠️【自动刊登熔断】\n"
                                                f"原因：{reason}\n"
                                                f"已停止所有账号刊登 — 请检查 VPN / IP 是否台湾\n"
                                                f"恢复后重新点「开始刊登」即可")
                                except Exception:
                                    pass
                            if done_all:
                                return
                    watcher_task = asyncio.create_task(_vpn_watcher())
                    try:
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                    finally:
                        watcher_task.cancel()
                        try:
                            await watcher_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    for r in results:
                        if isinstance(r, Exception):
                            self.app.log(f"[PUBLISH] ERROR: {r}")
            finally:
                await _shared_pw.stop()

        self._future = asyncio.run_coroutine_threadsafe(_runner(), self.app.loop)

        def _done(fut):
            try:
                fut.result()
                self.app.after(0, lambda: self.app.log("[PUBLISH] 全部任务已完成"))
            except Exception as e:
                self.app.after(0, lambda: self.app.log(f"[PUBLISH] ERROR: {e}"))
            # 记录操作历史
            try:
                trigger = getattr(self, "_trigger_label", "手动刊登") or "手动刊登"
                self._append_history(trigger)
                self._trigger_label = ""  # 重置
            except Exception:
                pass
            self._future = None
            self._pause.set()             # ★ 重置暫停狀態，防止殘留
            self._delete_pause_state()    # ★ 清理暫停文件，防止下次排程誤判
            self.app.after(0, self._update_btn_states)

        self._future.add_done_callback(_done)

    def _on_progress(self, acc: str, succ: int, fail: int, row: int, err: str = ""):
        st = self._stats.get(acc)
        if not st:
            return
        prev_succ = st.success
        st.success = st.base_success + succ
        st.fail = st.base_fail + fail
        st.current_row = int(row or 0)
        if err:
            st.last_error = err
        self.app.after(0, lambda: self._ui_update_stat(acc))
        # 每 100 條成功推送一次進度到 TG
        if succ > 0 and succ != prev_succ and succ % 100 == 0:
            self.app.log(f"[PUBLISH] {acc}: 進度通知 — 已成功 {succ} 條，失敗 {fail} 條")

    # ── API server 用:刊登進度快照(2026-04-29 v6.0.46)──
    def get_progress_snapshot(self) -> Dict[str, Any]:
        """純讀快照,給 /api/publish/progress 用。

        從現有 self._stats 拷字段,絕不修改。
        """
        import time as _time
        try:
            now = _time.time()
            rows = []
            running_count = 0
            grand_succ = 0
            grand_fail = 0
            for acc, st in list(self._stats.items()):
                if getattr(st, "running", False):
                    running_count += 1
                succ = int(getattr(st, "success", 0) or 0)
                fail = int(getattr(st, "fail", 0) or 0)
                grand_succ += succ
                grand_fail += fail
                rows.append({
                    "account": acc,
                    "excel": getattr(st, "excel", "") or "",
                    "success": succ,
                    "fail": fail,
                    "current_row": int(getattr(st, "current_row", 0) or 0),
                    "running": bool(getattr(st, "running", False)),
                    "started_at": getattr(st, "started_at", "") or "",
                    "ended_at": getattr(st, "ended_at", "") or "",
                    "last_error": str(getattr(st, "last_error", "") or "")[:300],
                    "base_success": int(getattr(st, "base_success", 0) or 0),
                    "base_fail": int(getattr(st, "base_fail", 0) or 0),
                })
            try:
                paused = not self._pause.is_set()
            except Exception:
                paused = False
            try:
                stopping = self._stop.is_set()
            except Exception:
                stopping = False
            return {
                "ts": now,
                "paused": paused,
                "stopping": stopping,
                "running_count": running_count,
                "total_accounts": len(rows),
                "grand_success": grand_succ,
                "grand_fail": grand_fail,
                "accounts": rows,
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "accounts": []}

    def stop(self):
        self._pause.set()  # 先解除暫停（讓循環跑到 stop 檢查點）
        self._stop.set()
        self._delete_pause_state()  # 用戶主動停止=放棄恢復
        self.app.log("[PUBLISH] stop requested")
        self.app.after(0, self._update_btn_states)

    # ── 暫停 / 恢復 ─────────────────────────────────────

    def _toggle_pause(self):
        if self._pause.is_set():
            self._do_pause()
        else:
            self._do_resume()

    def _do_pause(self):
        """暫停刊登（當前進行中的那一件會完成後才停）"""
        self._pause.clear()
        self.app.log("[PUBLISH] 暫停中 — 等待當前項完成...")
        # 立即顯示「暫停中」（等待各帳號當前項完成後才變「已暫停」）
        self.btn_pause.config(text="恢復")
        for acc, st in self._stats.items():
            if st.running:
                st._paused_display = "waiting"  # waiting=暫停中, True=已暫停
        self.app.after(0, self._refresh_all_ui_stats)
        # 延遲保存暫停狀態（等待各帳號 on_progress 回調更新 st.success 後再保存，更準確）
        self.app.after(1000, self._save_pause_state_if_paused)

    def _do_resume(self):
        """恢復刊登"""
        self._pause.set()
        self._delete_pause_state()
        self.app.log("[PUBLISH] 已恢復刊登")
        self.btn_pause.config(text="暫停")
        for acc, st in self._stats.items():
            if getattr(st, "_paused_display", False):
                st._paused_display = False
        self.app.after(0, self._refresh_all_ui_stats)

    def _save_pause_state_if_paused(self):
        """僅在仍處於暫停狀態時保存（供延遲調用）"""
        if not self._pause.is_set():
            self._save_pause_state()

    def _refresh_all_ui_stats(self):
        for acc in self._stats:
            self._ui_update_stat(acc)

    def _on_account_paused(self, acc_name: str):
        """帳號循環真正進入暫停等待時回調（從 async 線程調用）。"""
        st = self._stats.get(acc_name)
        if st and getattr(st, "_paused_display", False) == "waiting":
            st._paused_display = True  # 從「暫停中...」變為「已暫停」
        self.app.after(0, lambda: self._ui_update_stat(acc_name))
        # 帳號已真正暫停，重新保存暫停狀態（此時 st.success 已是最新）
        self.app.after(200, self._save_pause_state_if_paused)

    def _update_btn_states(self):
        """根據當前狀態更新按鈕啟用/禁用"""
        running = self._future is not None and not self._future.done()
        paused = not self._pause.is_set()
        has_pause_state = self._load_pause_state() is not None

        if running:
            self.btn_start.config(state="disabled")
            self.btn_pause.config(state="normal", text="恢復" if paused else "暫停")
            self.btn_stop.config(state="normal")
        elif has_pause_state:
            self.btn_start.config(state="normal", text="恢復刊登", style="Accent.TButton")
            self.btn_pause.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.btn_start.config(state="normal", text="开始刊登", style="Accent.TButton")
            self.btn_pause.config(state="disabled")
            self.btn_stop.config(state="disabled")

    # ── 暫停狀態持久化 ──────────────────────────────────

    def _save_pause_state(self):
        """保存暫停狀態到 JSON（跨會話恢復用）"""
        try:
            import json as _json
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            accounts = {}
            for acc, st in self._stats.items():
                if st.running or not st.ended_at:
                    accounts[acc] = {
                        "success": st.success,
                        "fail": st.fail,
                        "current_row": st.current_row,
                        "base_success": st.base_success,
                        "base_fail": st.base_fail,
                        "history_base_success": getattr(st, "_history_base_success", st.base_success),
                        "history_base_fail": getattr(st, "_history_base_fail", st.base_fail),
                    }
            state = {
                "paused_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "excel_dir": str(d),
                "trigger_label": getattr(self, "_trigger_label", ""),
                "accounts": accounts,
            }
            fp = d / self._PAUSE_STATE_FILE
            fp.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.app.log(f"[PUBLISH] 保存暫停狀態失敗: {e}")

    def _load_pause_state(self) -> dict | None:
        """讀取暫停狀態（返回 None 表示沒有）"""
        try:
            import json as _json
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            fp = d / self._PAUSE_STATE_FILE
            if not fp.exists():
                return None
            return _json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _delete_pause_state(self):
        """刪除暫停狀態文件"""
        try:
            d = Path(self.var_dir.get().strip() or str(PUBLISH_DIR))
            fp = d / self._PAUSE_STATE_FILE
            if fp.exists():
                fp.unlink()
        except Exception:
            pass



def _ensure_done_excel(src_xlsx: Path) -> Path:
    """確保存在 *_done.xlsx：
    - 若已存在：直接返回
    - 若不存在：复制一份并返回（原文件不修改）
    """
    done = src_xlsx.with_name(f"{src_xlsx.stem}_done{src_xlsx.suffix}")
    try:
        if not done.exists():
            shutil.copy2(str(src_xlsx), str(done))
    except Exception:
        # 如果复制失败，就退回使用原文件（但我们仍尽量不移动/删除）
        return src_xlsx
    return done


_summary_lock = threading.Lock()


def _auto_upload_codes(rows, app_log):
    """刊登成功后自动上传编码数据到云端（追加模式）。"""
    try:
        from core.tg_kv_poller import load_relay_config
        relay = load_relay_config()
        owner = str(relay.get("user_id", "") or "").strip()
        if not owner:
            app_log("[DOC] 自动上传跳过：未绑定 TG 用户")
            return
        url = "https://product-query.<PHONE_REDACTED>.workers.dev/api/upload-batch"
        token = "<D1_UPLOAD_TOKEN_REDACTED>"
        resp = requests.post(url, json={
            "token": token, "owner": owner, "rows": rows, "clear": False,
        }, timeout=30)
        r = resp.json()
        if r.get("ok"):
            app_log(f"[DOC] 自动上传编码: {r.get('inserted', 0)} 条")
        else:
            app_log(f"[DOC] 自动上传失败: {r.get('error', '未知')}")
    except Exception as e:
        app_log(f"[DOC] 自动上传异常: {e}")


def _move_success_to_summary(
    xlsx_path: Path,
    account_name: str,
    app_log: Callable[[str], None],
) -> None:
    """刊登完成后：把成功的行移到汇总表，从原文档删除。失败的留在原文档。

    汇总表路径：publish_excels/成功汇总.xlsx
    汇总表比原表多一列「账号」，放在第一列。
    """
    with _summary_lock:
        try:
            wb = openpyxl.load_workbook(xlsx_path)
            ws = wb.active
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 无法打开 Excel 做汇总: {e}")
            return

        # 读取表头
        headers: Dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            v = _norm_str(ws.cell(row=1, column=c).value)
            if v:
                headers[v] = c

        status_col = headers.get("刊登狀態")
        if not status_col:
            return

        # 收集成功行（从后往前，方便删除）
        success_rows: List[int] = []
        for r in range(2, ws.max_row + 1):
            st = _norm_str(ws.cell(row=r, column=status_col).value)
            if st == "成功":
                success_rows.append(r)

        if not success_rows:
            return

        # 收集成功行的标题，用于稍后从 test.xlsx 中移除
        title_col = headers.get("標題", 1)
        _success_titles: List[str] = []
        for r in success_rows:
            tv = _norm_str(ws.cell(row=r, column=title_col).value)
            if tv:
                _success_titles.append(tv)

        # 准备汇总表
        summary_path = xlsx_path.parent / "成功汇总.xlsx"
        if summary_path.exists():
            try:
                swb = openpyxl.load_workbook(summary_path)
                sws = swb.active
            except Exception:
                swb = openpyxl.Workbook()
                sws = swb.active
                sws.title = "成功汇总"
        else:
            swb = openpyxl.Workbook()
            sws = swb.active
            sws.title = "成功汇总"

        # 如果汇总表是空的，写表头（第一列=账号，后面=原表头）
        if sws.max_row <= 1 and not _norm_str(sws.cell(row=1, column=1).value):
            sws.cell(row=1, column=1, value="账号")
            for col_name, col_idx in sorted(headers.items(), key=lambda x: x[1]):
                sws.cell(row=1, column=col_idx + 1, value=col_name)

        # 追加成功行到汇总表
        for r in success_rows:
            new_row = sws.max_row + 1
            sws.cell(row=new_row, column=1, value=account_name)
            for col_name, col_idx in headers.items():
                val = ws.cell(row=r, column=col_idx).value
                sws.cell(row=new_row, column=col_idx + 1, value=val)

        # 保存汇总表
        try:
            _save_wb(swb, summary_path)
            app_log(f"[PUBLISH] {account_name}: {len(success_rows)} 条成功记录已汇总到 成功汇总.xlsx")
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 汇总表保存失败: {e}")
            return

        # 自动上传编码数据到云端（追加模式）
        try:
            bc_col = None
            for k in ("商品條碼", "商品条码", "條碼", "条码"):
                if k in headers:
                    bc_col = headers[k]
                    break
            pc_col = headers.get("商品編號")
            if bc_col or pc_col:
                upload_rows = []
                for r in success_rows:
                    bc = str(ws.cell(row=r, column=bc_col).value or "").strip() if bc_col else ""
                    pc = str(ws.cell(row=r, column=pc_col).value or "").strip() if pc_col else ""
                    if bc or pc:
                        upload_rows.append({"barcode": bc, "product_code": pc, "account": account_name})
                if upload_rows:
                    _auto_upload_codes(upload_rows, app_log)
                else:
                    app_log(f"[PUBLISH] {account_name}: 成功行无条码/编号数据，跳过自动上传")
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 自动上传编码异常(不影响刊登): {e}")

        # 从原文档删除成功行（从后往前删，避免行号偏移）
        for r in sorted(success_rows, reverse=True):
            ws.delete_rows(r, 1)

        # 清理幽灵行：标题列为空但其他列有残留数据的行
        title_col = headers.get("標題", 1)
        for r in range(ws.max_row, 1, -1):
            title = ws.cell(row=r, column=title_col).value
            if title:
                continue
            has_any = any(ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1))
            if has_any or r > ws.max_row:
                ws.delete_rows(r, 1)

        try:
            _save_wb(wb, xlsx_path)
            app_log(f"[PUBLISH] {account_name}: 已从原文档移除 {len(success_rows)} 条成功记录")
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 原文档保存失败: {e}")

        # ── 同步从 test.xlsx 移除已成功刊登的行 ──
        if _success_titles:
            test_path = xlsx_path.parent / "test.xlsx"
            if test_path.exists() and test_path != xlsx_path:
                try:
                    twb = openpyxl.load_workbook(test_path)
                    tws = twb.active
                    # 找 test.xlsx 的标题列
                    t_title_col = None
                    for c in range(1, tws.max_column + 1):
                        if _norm_str(tws.cell(row=1, column=c).value) == "標題":
                            t_title_col = c
                            break
                    if t_title_col:
                        remaining_titles = list(_success_titles)
                        del_rows = []
                        for r in range(2, tws.max_row + 1):
                            tv = _norm_str(tws.cell(row=r, column=t_title_col).value)
                            if tv and tv in remaining_titles:
                                del_rows.append(r)
                                remaining_titles.remove(tv)  # 每个标题只移除一次
                        if del_rows:
                            for r in sorted(del_rows, reverse=True):
                                tws.delete_rows(r, 1)
                            _save_wb(twb, test_path)
                            app_log(f"[PUBLISH] {account_name}: 已从 test.xlsx 移除 {len(del_rows)} 条成功记录")
                except Exception as e:
                    app_log(f"[PUBLISH] {account_name}: 同步 test.xlsx 失败(不影响刊登): {e}")


def _move_failure_to_summary(
    xlsx_path: Path,
    account_name: str,
    app_log: Callable[[str], None],
) -> None:
    """把失败/待人工核对的行追加到 失败汇总.xlsx。"""
    with _summary_lock:
        try:
            wb = openpyxl.load_workbook(xlsx_path)
            ws = wb.active
        except Exception:
            return

        headers: Dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            v = _norm_str(ws.cell(row=1, column=c).value)
            if v:
                headers[v] = c

        status_col = headers.get("刊登狀態")
        if not status_col:
            return

        fail_rows = []
        for r in range(2, ws.max_row + 1):
            st = _norm_str(ws.cell(row=r, column=status_col).value)
            if st in ("失败", "待人工核對", "跳過"):
                fail_rows.append(r)

        if not fail_rows:
            return

        # 收集失败行标题，稍后同步清理 test.xlsx
        title_col = headers.get("標題", 1)
        _fail_titles: List[str] = []
        for r in fail_rows:
            tv = _norm_str(ws.cell(row=r, column=title_col).value)
            if tv:
                _fail_titles.append(tv)

        summary_path = xlsx_path.parent / "失败汇总.xlsx"
        if summary_path.exists():
            try:
                swb = openpyxl.load_workbook(summary_path)
                sws = swb.active
            except Exception:
                swb = openpyxl.Workbook()
                sws = swb.active
        else:
            swb = openpyxl.Workbook()
            sws = swb.active

        if sws.max_row <= 1 and not _norm_str(sws.cell(row=1, column=1).value):
            sws.cell(row=1, column=1, value="账号")
            for col_name, col_idx in sorted(headers.items(), key=lambda x: x[1]):
                sws.cell(row=1, column=col_idx + 1, value=col_name)

        for r in fail_rows:
            new_row = sws.max_row + 1
            sws.cell(row=new_row, column=1, value=account_name)
            for col_name, col_idx in headers.items():
                sws.cell(row=new_row, column=col_idx + 1, value=ws.cell(row=r, column=col_idx).value)

        try:
            _save_wb(swb, summary_path)
            app_log(f"[PUBLISH] {account_name}: {len(fail_rows)} 条失败记录 → 失败汇总.xlsx")
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 失败汇总保存失败: {e}")
            # 寫 summary 失敗就不從原檔刪,保險(避免資料丟失)
            return

        # v6.1.52:只把「永久性失敗」(分類禁止)從原檔刪除,跟成功一樣
        # 修「分類白名單失敗永久保留 done 文件」bug — 重試也沒用,等於完成
        # 網路錯誤/timeout 之類 transient 失敗不刪,讓用戶可以選擇重跑
        err_col = headers.get("刊登錯誤")
        permanent_fail_rows = []
        if err_col:
            for r in fail_rows:
                _err_txt = _norm_str(ws.cell(row=r, column=err_col).value)
                # 分類白名單禁止 / 不在允許的分類 → 永久性失敗
                if ("被禁止刊登" in _err_txt
                        or "分類白名單" in _err_txt
                        or "白名單" in _err_txt
                        or "不在允許的分類" in _err_txt
                        or "不在允许的分类" in _err_txt):
                    permanent_fail_rows.append(r)
        if not permanent_fail_rows:
            return  # 沒有永久性失敗,原檔保留(讓用戶可以重跑 transient 失敗)
        for r in sorted(permanent_fail_rows, reverse=True):
            try:
                ws.delete_rows(r, 1)
            except Exception:
                pass
        try:
            _save_wb(wb, xlsx_path)
            app_log(f"[PUBLISH] {account_name}: 已從原文檔移除 {len(permanent_fail_rows)} 條永久性失敗(分類禁止)")
        except Exception as e:
            app_log(f"[PUBLISH] {account_name}: 原文檔(失敗刪除)保存失敗: {e}")

        # ── 同步从 test.xlsx 移除已处理（失败/跳过）的行 ──
        if _fail_titles:
            test_path = xlsx_path.parent / "test.xlsx"
            if test_path.exists() and test_path != xlsx_path:
                try:
                    twb = openpyxl.load_workbook(test_path)
                    tws = twb.active
                    t_title_col = None
                    for c in range(1, tws.max_column + 1):
                        if _norm_str(tws.cell(row=1, column=c).value) == "標題":
                            t_title_col = c
                            break
                    if t_title_col:
                        remaining_titles = list(_fail_titles)
                        del_rows = []
                        for r in range(2, tws.max_row + 1):
                            tv = _norm_str(tws.cell(row=r, column=t_title_col).value)
                            if tv and tv in remaining_titles:
                                del_rows.append(r)
                                remaining_titles.remove(tv)
                        if del_rows:
                            for r in sorted(del_rows, reverse=True):
                                tws.delete_rows(r, 1)
                            _save_wb(twb, test_path)
                            app_log(f"[PUBLISH] {account_name}: 已从 test.xlsx 移除 {len(del_rows)} 条失败记录")
                except Exception as e:
                    app_log(f"[PUBLISH] {account_name}: 同步 test.xlsx(失败) 失败(不影响刊登): {e}")


class _ImageUploadingRetry(Exception):
    """圖片尚在上傳中，需要刷新頁面重跑當前行"""

class _BlankPageRetry(Exception):
    """页面空白/表单未渲染，需要重建临时 profile 重试"""


# =====================================================================
# Redux Dispatch 快速刊登（替代路径 UI 填表，直接 dispatch Redux action）
# =====================================================================

_REDUX_DRY_RUN = False          # True = 只构建 payload 打日志，不实际 dispatch
_MAX_REDUX_FAILURES = 5         # 连续系统性失败 N 次后禁用 Redux，全程走 UI

# ── JS: 解析 isoredux-data ──
_JS_PARSE_ISOREDUX = """() => {
    const el = document.getElementById('isoredux-data');
    if (!el) return null;
    try { return JSON.parse(el.textContent); }
    catch (e) { return null; }
}"""

# ── JS: 页面加载前注入，拦截 Redux store 创建 ──
_JS_INIT_STORE_HOOK = """(() => {
    window.__PUB_STORE__ = null;
    // 拦截 React-Redux Provider: store 通常通过 context 传递
    // 我们 hook Object.defineProperty 来捕获 _currentValue 设置
    const _isStore = (obj) =>
        obj && typeof obj === 'object'
        && typeof obj.dispatch === 'function'
        && typeof obj.getState === 'function'
        && typeof obj.subscribe === 'function';

    // 方法1: 定期扫描 React fiber 树
    const _scan = () => {
        if (window.__PUB_STORE__) return;
        try {
            const root = document.getElementById('isoredux-root');
            if (!root) return;
            // 尝试所有可能的 React 内部属性前缀
            const key = Object.keys(root).find(k =>
                k.startsWith('__reactContainer$') ||
                k.startsWith('__reactFiber$') ||
                k.startsWith('__reactInternalInstance$'));
            if (!key) return;

            let fiber = root[key];
            // 遍历 fiber 树（BFS）
            const visited = new Set();
            const queue = [fiber];
            while (queue.length > 0 && queue.length < 50000) {
                const node = queue.shift();
                if (!node || visited.has(node)) continue;
                visited.add(node);
                // stateNode.store（Class Component with Provider）
                try {
                    if (node.stateNode && _isStore(node.stateNode.store)) {
                        window.__PUB_STORE__ = node.stateNode.store;
                        return;
                    }
                } catch(e) {}
                // pendingProps.store（Provider element props）
                try {
                    if (node.pendingProps && _isStore(node.pendingProps.store)) {
                        window.__PUB_STORE__ = node.pendingProps.store;
                        return;
                    }
                    if (node.memoizedProps && _isStore(node.memoizedProps.store)) {
                        window.__PUB_STORE__ = node.memoizedProps.store;
                        return;
                    }
                } catch(e) {}
                // memoizedState 链表（hooks）
                try {
                    let hook = node.memoizedState;
                    let hops = 0;
                    while (hook && hops < 300) {
                        const val = hook.memoizedState;
                        if (val && typeof val === 'object') {
                            if (_isStore(val)) { window.__PUB_STORE__ = val; return; }
                            // React Context: _currentValue
                            try {
                                if (val._currentValue && _isStore(val._currentValue)) {
                                    window.__PUB_STORE__ = val._currentValue; return;
                                }
                            } catch(e2) {}
                            // useReducer dispatch (array pattern [state, dispatch])
                            try {
                                if (hook.queue && hook.queue.lastRenderedReducer) {
                                    const q = hook.queue;
                                    if (q.dispatch && typeof q.dispatch === 'function') {
                                        // Check if reducer handles FETCH_PUBLISH
                                        // Not reliable, skip
                                    }
                                }
                            } catch(e2) {}
                        }
                        hook = hook.next;
                        hops++;
                    }
                } catch(e) {}
                if (node.child) queue.push(node.child);
                if (node.sibling) queue.push(node.sibling);
            }
        } catch(e) {}
    };

    // 多次扫描：React hydration 可能延迟
    let _attempts = 0;
    const _interval = setInterval(() => {
        _scan();
        _attempts++;
        if (window.__PUB_STORE__ || _attempts >= 20) clearInterval(_interval);
    }, 500);
    // 也在 DOMContentLoaded 和 load 时扫描
    document.addEventListener('DOMContentLoaded', () => setTimeout(_scan, 500));
    window.addEventListener('load', () => setTimeout(_scan, 1000));
})();"""

# ── JS: 检查 store 是否已缓存（配合 init hook 使用）──
_JS_FIND_STORE = """() => {
    // 1) init hook 已捕获
    if (window.__PUB_STORE__) return 'hook:captured';
    // 2) 常见全局
    for (const name of ['__REDUX_STORE__','__store__','store']) {
        try {
            const s = window[name];
            if (s && typeof s.dispatch==='function' && typeof s.getState==='function'
                && typeof s.subscribe==='function') {
                window.__PUB_STORE__ = s;
                return 'global:' + name;
            }
        } catch(e) {}
    }
    // 3) 手动扫描 fiber（兜底）
    try {
        const root = document.getElementById('isoredux-root');
        if (!root) return null;
        const key = Object.keys(root).find(k =>
            k.startsWith('__reactContainer$') ||
            k.startsWith('__reactFiber$') ||
            k.startsWith('__reactInternalInstance$'));
        if (!key) return null;
        let fiber = root[key];
        const visited = new Set();
        const queue = [fiber];
        while (queue.length > 0 && queue.length < 50000) {
            const node = queue.shift();
            if (!node || visited.has(node)) continue;
            visited.add(node);
            try {
                if (node.stateNode && node.stateNode.store
                    && typeof node.stateNode.store.dispatch==='function') {
                    window.__PUB_STORE__ = node.stateNode.store;
                    return 'fiber:stateNode.store';
                }
            } catch(e) {}
            try {
                if (node.pendingProps && node.pendingProps.store
                    && typeof node.pendingProps.store.dispatch==='function') {
                    window.__PUB_STORE__ = node.pendingProps.store;
                    return 'fiber:pendingProps.store';
                }
                if (node.memoizedProps && node.memoizedProps.store
                    && typeof node.memoizedProps.store.dispatch==='function') {
                    window.__PUB_STORE__ = node.memoizedProps.store;
                    return 'fiber:memoizedProps.store';
                }
            } catch(e) {}
            try {
                let hook = node.memoizedState;
                let hops = 0;
                while (hook && hops < 300) {
                    const val = hook.memoizedState;
                    if (val && typeof val === 'object') {
                        if (typeof val.dispatch==='function' && typeof val.getState==='function') {
                            window.__PUB_STORE__ = val;
                            return 'fiber:hook';
                        }
                        if (val._currentValue && typeof val._currentValue.dispatch==='function'
                            && typeof val._currentValue.getState==='function') {
                            window.__PUB_STORE__ = val._currentValue;
                            return 'fiber:context';
                        }
                    }
                    hook = hook.next;
                    hops++;
                }
            } catch(e) {}
            if (node.child) queue.push(node.child);
            if (node.sibling) queue.push(node.sibling);
        }
        // 返回诊断信息
        return null;
    } catch(e) { return null; }
}"""

# ── JS: 提取上传后的图片 CDN URL ──
_JS_EXTRACT_IMG_URLS = """() => {
    const found = [];
    // 优先原始URL (/images/ 路径)，过滤含时效token的变换URL (/cl/api/res/)
    const _isRaw = (u) => u && u.includes('img.yec.tw') && u.includes('/images/') && !u.includes('/cl/api/res/');
    const _add = (u) => { if (u && typeof u === 'string' && u.includes('img.yec.tw') && !found.includes(u)) found.push(u); };
    const _addArr = (a) => {
        if (!Array.isArray(a)) return;
        // 先提取原始URL
        const raw = [];
        a.forEach(x => {
            if (typeof x === 'string') { if (_isRaw(x)) raw.push(x); }
            else if (x && x.url && _isRaw(x.url)) raw.push(x.url);
            else if (x && x.src && _isRaw(x.src)) raw.push(x.src);
        });
        if (raw.length > 0) { raw.forEach(_add); return; }
        // 没有原始URL时退回到所有URL
        a.forEach(x => { if (typeof x === 'string') _add(x); else if (x && x.url) _add(x.url); else if (x && x.src) _add(x.src); });
    };

    // 策略1: Redux store — 搜索多条路径
    try {
        const s = window.__PUB_STORE__;
        if (s) {
            const st = s.getState() || {};
            const ms = st.merchandiseSubmit || {};
            // 路径 a: merchandiseSubmit.merchandise.images
            _addArr((ms.merchandise || {}).images);
            // 路径 b: merchandiseSubmit.images
            _addArr(ms.images);
            // 路径 c: merchandiseSubmit.uploadImages / uploadedImages
            _addArr(ms.uploadImages);
            _addArr(ms.uploadedImages);
            // 路径 d: 遍历 merchandiseSubmit 所有 array 值
            if (found.length === 0) {
                for (const k of Object.keys(ms)) {
                    const v = ms[k];
                    if (Array.isArray(v) && v.length > 0 && v.length <= 20) {
                        for (const item of v) {
                            if (typeof item === 'string' && _isRaw(item)) _add(item);
                            else if (item && typeof item === 'object') {
                                // 优先 src.url (奇摩軟件格式)
                                if (item.src && item.src.url && _isRaw(item.src.url)) { _add(item.src.url); continue; }
                                for (const vv of Object.values(item)) {
                                    if (typeof vv === 'string' && vv.includes('img.yec.tw')) _add(vv);
                                }
                            }
                        }
                    }
                }
            }
        }
    } catch(e) {}
    if (found.length > 0) return found;

    // 策略2: 拦截到的 URL（由 Python 端注入，已做原始URL优先过滤）
    try {
        const intercepted = window.__PUB_IMG_URLS__;
        if (Array.isArray(intercepted) && intercepted.length > 0) return intercepted;
    } catch(e) {}

    // 策略3: 页面 img 标签 src
    const imgEls = document.querySelectorAll('img[src]');
    for (const img of imgEls) {
        const s = img.src || '';
        if (s.includes('img.yec.tw') && s.includes('/images/')) _add(s);
    }
    if (found.length > 0) return found;

    // 策略4: background-image
    const divs = document.querySelectorAll('[style*="img.yec.tw"]');
    for (const d of divs) {
        const m = (d.getAttribute('style')||'').match(/url\\(['"]?(https?:\\/\\/img\\.yec\\.tw[^'"\\)]+)/);
        if (m) _add(m[1]);
    }
    return found;
}"""

# ── JS: dispatch action 并等待结果 ──
_JS_DISPATCH_AND_WAIT = """async (actionJson) => {
    const action = JSON.parse(actionJson);
    const store = window.__PUB_STORE__;
    if (!store) return {error: true, message: 'Redux store not found'};

    // 记录 dispatch 前的 result
    const prevId = (store.getState().merchandiseSubmit || {}).result
                   ? store.getState().merchandiseSubmit.result.id : '';

    store.dispatch(action);

    // 轮询结果（最长 25s）
    const t0 = Date.now();
    while (Date.now() - t0 < 25000) {
        await new Promise(r => setTimeout(r, 500));
        const st = store.getState().merchandiseSubmit || {};
        // 成功：result.id 出现且不同于旧值
        if (st.result && st.result.id && st.result.id !== prevId) {
            return st.result;
        }
        // 检查 isFetching 变为 false + 有 errors
        if (!st.isFetching && st.errors && Object.keys(st.errors).length > 0) {
            return {error: true, errors: st.errors};
        }
    }
    // 最后读一次
    const final = (store.getState().merchandiseSubmit || {}).result;
    if (final && final.id && final.id !== prevId) return final;
    return {error: true, message: 'dispatch timeout (25s)'};
}"""


async def _redux_extract_initial_state(page) -> dict:
    """从 isoredux-data 脚本标签解析 Redux 初始状态。"""
    state = await page.evaluate(_JS_PARSE_ISOREDUX)
    if not state:
        raise RuntimeError("isoredux-data not found or not parseable")
    # 校验登录
    user = (state.get("page") or {}).get("user") or {}
    if not user.get("isLogin"):
        raise RuntimeError("Cookies 已失效 (isLogin=false)")
    return state


async def _redux_find_store(page) -> str:
    """运行时发现 Redux store 并缓存到 window.__PUB_STORE__，返回发现方式。"""
    method = await page.evaluate(_JS_FIND_STORE)
    if not method:
        raise RuntimeError("Redux store not found via any strategy")
    return method


_REDUX_CAT_BY_ID: Dict[str, dict] = {}   # {cat_id: {"name":..., "parent":...}}

def _redux_build_category_lookup(category_tree: dict) -> Dict[str, str]:
    """从 categoryTree dict 构建 {分类路径: cat_id} 映射表。"""
    global _REDUX_CAT_BY_ID
    by_id: Dict[str, dict] = {}
    for cid_key, entry in category_tree.items():
        cid = str(entry.get("cat_id", cid_key))
        pid = str(entry.get("parent_cat_id", "0"))
        name = entry.get("name", "")
        if cid and name:
            by_id[cid] = {"name": name, "parent": pid}

    lookup: Dict[str, str] = {}

    def _path_for(cid: str) -> str:
        parts = []
        cur = cid
        seen = set()
        while cur and cur in by_id and cur not in seen and cur != "0":
            seen.add(cur)
            _n = re.sub(r"[＠@#＃]+$", "", by_id[cur]["name"])
            parts.append(_n)
            cur = by_id[cur]["parent"]
        parts.reverse()
        return " > ".join(parts)

    _at_count = 0
    for cid in by_id:
        path = _path_for(cid)
        if path:
            lookup[path] = cid
            norm = re.sub(r"\s*[>＞]\s*", ">", path)
            if norm != path:
                lookup[norm] = cid
            if "@" in path or "＠" in path:
                _at_count += 1
                if _at_count <= 3:
                    _plog(f"[DEBUG] lookup仍含@: '{path[:80]}'")

    if _at_count:
        _plog(f"[DEBUG] lookup中含@的路径共{_at_count}条（应为0）")
    _REDUX_CAT_BY_ID = by_id
    return lookup


def _tree_find_best(seg: str, pool: list) -> Optional[str]:
    """在兄弟节点 pool [(cat_id, name), ...] 中找 seg 的最佳匹配。
    逐步放宽条件：精确→包含→斜杠段→字符重叠。"""
    seg = seg.strip()
    if not seg or not pool:
        return None

    # 1) 精确匹配
    for cid, name in pool:
        if name == seg:
            return cid

    # 2) 包含匹配 (双向子串，至少2字符)
    hits = [(cid, name) for cid, name in pool
            if (seg in name or name in seg) and min(len(seg), len(name)) >= 2]
    if len(hits) == 1:
        return hits[0][0]

    # 3) 斜杠段匹配：Yahoo 叶子含 "/" 时逐段比对
    for cid, name in pool:
        if "/" in name and (cid, name) not in hits:
            for s in name.split("/"):
                s = s.strip()
                if len(s) >= 2 and (s in seg or seg in s):
                    hits.append((cid, name))
                    break
    if len(hits) == 1:
        return hits[0][0]
    if hits:
        # 多个候选时取字符重叠度最高的
        def _overlap(name):
            sa, sb = set(seg), set(name)
            return len(sa & sb) / max(len(sa | sb), 1)
        return max(hits, key=lambda x: _overlap(x[1]))[0]

    return None


def _redux_tree_resolve(parts: list) -> Optional[str]:
    """逐层树遍历匹配分类。每层只在兄弟节点(10~30个)中搜索，大幅减少误匹配。"""
    if not parts or not _REDUX_CAT_BY_ID:
        return None

    # 构建 children 索引: parent_id -> [(cat_id, name), ...]
    children: Dict[str, list] = {}
    for cid, info in _REDUX_CAT_BY_ID.items():
        children.setdefault(info["parent"], []).append((cid, info["name"]))

    cur_parents = ["0"]
    final_id = None

    for seg in parts:
        pool = []
        for pid in cur_parents:
            pool.extend(children.get(pid, []))
        if not pool:
            return None
        matched = _tree_find_best(seg, pool)
        if not matched:
            return None
        final_id = matched
        cur_parents = [matched]

    return final_id


# ── 分类属性缓存 ──
# 缓存文件: ROOT_DIR/cat_attrs_cache.json（所有账号共享，分类属性与账号无关）
# 格式: {"catId": [{"title": "...", "values": ["..."], "fail": false}], ...}
_CAT_ATTRS_CACHE: Dict[str, list] = {}
_CAT_ATTRS_CACHE_PATH: Optional[Path] = ROOT_DIR / "cat_attrs_cache.json"


def _load_cat_attrs_cache(profile_dir: str = "") -> None:
    """加载分类属性缓存。"""
    global _CAT_ATTRS_CACHE
    if _CAT_ATTRS_CACHE_PATH.exists():
        try:
            _CAT_ATTRS_CACHE = json.loads(_CAT_ATTRS_CACHE_PATH.read_text(encoding="utf-8"))
            _plog(f"[CatAttrs] 已加载缓存: {len(_CAT_ATTRS_CACHE)} 个分类有属性")
        except Exception:
            _CAT_ATTRS_CACHE = {}
    else:
        _CAT_ATTRS_CACHE = {}


def _save_cat_attrs_cache(cat_id: str, attrs: list) -> None:
    """保存分类属性到缓存。"""
    global _CAT_ATTRS_CACHE
    if not attrs:
        return
    _CAT_ATTRS_CACHE[str(cat_id)] = attrs
    try:
        _CAT_ATTRS_CACHE_PATH.write_text(
            json.dumps(_CAT_ATTRS_CACHE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_cached_cat_attrs(cat_id: str) -> list:
    """从缓存获取分类属性。返回 [] 表示未缓存。"""
    return _CAT_ATTRS_CACHE.get(str(cat_id), [])


# ── 分类别名映射（Yahoo 改版后旧分类→新分类）──
_CATEGORY_ALIASES: Dict[str, str] = {
    # 2026-03: 胸針/別針 从女裝移到手錶與飾品配件
    "女裝與服飾配件->女性服飾配件->胸針/別針": "手錶與飾品配件->其他首飾配件->別針",
    "女裝與服飾配件>女性服飾配件>胸針/別針": "手錶與飾品配件>其他首飾配件>別針",
}

# ── 分类 ID 直接覆盖（Redux 分类树缺失时，手动指定正确 ID）──
_CATEGORY_ID_OVERRIDES: Dict[str, str] = {
    # 居家、家具與園藝 > 寢具/家飾 > 收藏擺飾@ > 其他 = 20452
    "居家、家具與園藝>寢具/家飾>收藏擺飾>其他": "20452",
    # 偶像、球員卡與郵幣 > 錢幣/古錢幣 > 日本銀幣 = 2092074529（Redux 分類樹缺失）
    "偶像、球員卡與郵幣>錢幣/古錢幣>日本銀幣": "2092074529",
}


def _apply_category_alias(cat_path: str) -> str:
    """将已知的旧分类路径映射为新路径。"""
    norm = re.sub(r"\s*[>＞]\s*", ">", cat_path).strip()
    for old, new in _CATEGORY_ALIASES.items():
        old_norm = re.sub(r"\s*[>＞]\s*", ">", old)
        if norm == old_norm:
            _plog(f"[CatAlias] '{cat_path}' -> '{new}'")
            return new
    return cat_path


def _redux_resolve_category_id(cat_path: str, lookup: Dict[str, str]) -> str:
    """将 Excel 分类字段解析为 Yahoo 分类 ID。只接受纯数字 ID。"""
    raw = cat_path  # 保留原始值用于诊断
    cat_path = (cat_path or "").strip()
    if not cat_path:
        raise RuntimeError("分類欄位為空")

    # ── 数字化归一化：处理 Excel 文本型数字/脏数据 ──
    cat_path = cat_path.lstrip("'")                    # 去前导单引号
    cat_path = cat_path.replace("\u200b", "")           # 去零宽空格
    cat_path = cat_path.replace("\ufeff", "")            # 去 BOM
    cat_path = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", "", cat_path)  # 去所有不可见 Unicode
    cat_path = cat_path.strip()

    # 支持 Excel 数字格式（openpyxl 读出来可能带 .0）
    if "." in cat_path:
        try:
            cat_path = str(int(float(cat_path)))
        except (ValueError, OverflowError):
            pass

    if cat_path.isdigit():
        # 白名单检查
        if _ALLOWED_CAT_IDS and cat_path not in _ALLOWED_CAT_IDS:
            raise RuntimeError(f"分類 {cat_path} 被禁止刊登（不在允許的分類白名單中）")
        _plog(f"[CatResolve] ID直通: {cat_path}")
        return cat_path

    raise RuntimeError(f"分類欄位必須是數字ID，收到: {cat_path[:60]} (raw={repr(raw)[:80]})")


def _redux_get_active_payments(redux_state: dict) -> List[str]:
    """从 Redux 初始状态提取账号已勾选的付款方式。
    优先用 checked 字段（表单 checkbox 实际状态），fallback 到 active。
    """
    accept_payment = redux_state.get("acceptPayment") or {}
    payments_data = accept_payment.get("payments") or {}
    # checked = 表单上 checkbox 的真实状态（文檔是什麽就上傳什麽）
    checked = [k for k, v in payments_data.items()
               if isinstance(v, dict) and v.get("checked")]
    if checked:
        _plog(f"[Redux] acceptPayment checked={checked}")
        return checked
    # fallback: 旧版页面可能没有 checked 字段
    active = [k for k, v in payments_data.items()
              if isinstance(v, dict) and v.get("active")]
    _plog(f"[Redux] acceptPayment active(fallback)={active}")
    return active or ["c2cCash"]


# JS: 从 live store 读取 Yahoo 表单实际渲染后的 payments（checkbox 状态）
# Yahoo 刊登页的收款 checkbox 是 React 组件，不是标准 <input type=checkbox>
# 需要通过 DOM 结构和文本内容来映射
_JS_GET_FORM_PAYMENTS = """() => {
    // 付款方式文本 → Redux key 映射
    const TEXT_TO_KEY = {
        '萊爾富取貨付款': 'c2cHilifeCvs',
        '7-ELEVEN取貨付款': 'c2cSevenCvs',
        '全家取貨付款': 'c2cFamilyCvs',
        '郵局便利包': 'c2cPostOffice',
        'ATM轉帳': 'c2cAtm',
        'FamiPort': 'c2cAtm',
        '輕鬆付帳戶餘額': 'c2cAtm',
        '面交': 'c2cCash',
        '貨到付款': 'c2cCash',
        '信用卡一次付清': 'c2cCreditCard',
    };

    // 方法1: 找「收款方式」区块，检查哪些 checkbox 是 checked
    const found = [];
    // Yahoo 用 label 包裹 checkbox，checked 时有 svg 或特定 class
    const labels = document.querySelectorAll('label');
    for (const label of labels) {
        const text = label.textContent || '';
        // 检查 label 内是否有 checked 的 input
        const input = label.querySelector('input[type="checkbox"]');
        if (input && input.checked) {
            for (const [txt, key] of Object.entries(TEXT_TO_KEY)) {
                if (text.includes(txt) && !found.includes(key)) {
                    found.push(key);
                }
            }
        }
        // 也检查 aria-checked 或 svg checkmark
        const checkedEl = label.querySelector('[aria-checked="true"], [data-checked="true"]');
        if (checkedEl) {
            for (const [txt, key] of Object.entries(TEXT_TO_KEY)) {
                if (text.includes(txt) && !found.includes(key)) {
                    found.push(key);
                }
            }
        }
    }
    if (found.length > 0) return { source: 'dom_label', payments: found };

    // 方法2: 运送与运费区块的文本提取有效的付款方式
    // 截图显示: "套用全店運費設定（7-ELEVEN取貨付款、萊爾富取貨付款，修改）"
    const allText = document.body.innerText || '';
    const shipMatch = allText.match(/套用全店運費設定[（(]([^)）]+)[)）]/);
    if (shipMatch) {
        const shipText = shipMatch[1];
        const shipPayments = [];
        for (const [txt, key] of Object.entries(TEXT_TO_KEY)) {
            if (shipText.includes(txt) && !shipPayments.includes(key)) {
                shipPayments.push(key);
            }
        }
        if (shipPayments.length > 0) {
            return { source: 'shipping_text', payments: shipPayments, _shipText: shipText };
        }
    }

    // 方法3: 从 Redux store 的 merchandiseSubmit 读
    const s = window.__PUB_STORE__;
    if (s) {
        const st = s.getState();
        const ms = st.merchandiseSubmit || {};
        const msMerch = ms.merchandise || {};
        if (Array.isArray(msMerch.payments) && msMerch.payments.length > 0) {
            return { source: 'ms.merchandise.payments', payments: msMerch.payments };
        }
        if (Array.isArray(ms.payments) && ms.payments.length > 0) {
            return { source: 'ms.payments', payments: ms.payments };
        }
    }

    return null;
}"""


def _redux_get_shipments_config(redux_state: dict) -> dict:
    """从 Redux 状态提取账号的运费规则配置。
    默认 isApplyShippingRule=True（绝大多数卖家都有配运费规则）。
    只有在明确检测到没有运费规则时才返回 False。
    """
    ms = redux_state.get("merchandiseSubmit") or {}
    # 如果 merchandiseSubmit 里已有 shipments 配置，直接用
    ms_shipments = ms.get("shipments")
    if isinstance(ms_shipments, dict) and ms_shipments:
        _plog(f"[Redux] shipments from merchandiseSubmit: {ms_shipments}")
        return ms_shipments
    # 默认 True — isoredux-data 可能不含 shippingRule 字段
    _plog(f"[Redux] shipments: 使用默认值 isApplyShippingRule=true")
    return {"isApplyShippingRule": True}


# JS: 从 live Redux store 提取当前表单的 merchandise 默认状态
_JS_GET_STORE_MERCHANDISE_DEFAULTS = """() => {
    const s = window.__PUB_STORE__;
    if (!s) return null;
    const st = s.getState();
    const ms = st.merchandiseSubmit || {};
    // 提取关键默认字段
    return {
        payments: ms.payments || null,
        shipments: ms.shipments || null,
        merchandise: ms.merchandise || null,
        // 完整的 top-level keys（诊断用）
        _ms_keys: Object.keys(ms),
        _ap_keys: Object.keys(st.acceptPayment || {}),
        _ship_keys: Object.keys(st.shippingRule || st.acceptShipment || {}),
        // 直接抓 shippingRule state
        _shippingRule: st.shippingRule || null,
        _acceptShipment: st.acceptShipment || null,
    };
}"""


async def _redux_get_store_defaults(page) -> dict:
    """从 live Redux store 提取表单默认值（payments, shipments 等）。"""
    try:
        defaults = await page.evaluate(_JS_GET_STORE_MERCHANDISE_DEFAULTS)
        if defaults:
            _plog(f"[Redux] store defaults: ms_keys={defaults.get('_ms_keys')}, "
                  f"payments={defaults.get('payments')}, "
                  f"shipments={defaults.get('shipments')}, "
                  f"shippingRule={json.dumps(defaults.get('_shippingRule'), ensure_ascii=False)[:200] if defaults.get('_shippingRule') else 'null'}, "
                  f"acceptShipment={json.dumps(defaults.get('_acceptShipment'), ensure_ascii=False)[:200] if defaults.get('_acceptShipment') else 'null'}")
        return defaults or {}
    except Exception as e:
        _plog(f"[Redux] get store defaults failed: {e}")
        return {}


async def _redux_get_form_payments(page) -> List[str]:
    """从 live store/DOM 读取表单实际的付款方式（和用户看到的 checkbox 一致）。
    返回 payments list，如果读不到返回空 list。
    """
    try:
        result = await page.evaluate(_JS_GET_FORM_PAYMENTS)
        if result and result.get("payments"):
            _plog(f"[Redux] form payments: source={result['source']}, "
                  f"payments={result['payments']}"
                  + (f", debug={result.get('_debug')}" if result.get('_debug') else ""))
            return result["payments"]
        _plog(f"[Redux] form payments: 未能读取 (result={result})")
        return []
    except Exception as e:
        _plog(f"[Redux] get form payments failed: {e}")
        return []


async def _redux_extract_image_urls(page, expected_count: int,
                                     timeout_s: float = 30.0) -> List[str]:
    """上传图片后，轮询提取 CDN URL（img.yec.tw）。"""
    t0 = time.time()
    _last_log = 0  # 每5秒记录一次诊断
    while time.time() - t0 < timeout_s:
        urls = await page.evaluate(_JS_EXTRACT_IMG_URLS)
        if urls and len(urls) >= expected_count:
            return urls[:expected_count]
        elapsed = time.time() - t0
        if elapsed - _last_log >= 5:
            # 诊断: 检查拦截器和 DOM 状态
            _diag = await page.evaluate("""() => {
                const intercepted = (window.__PUB_IMG_URLS__ || []).length;
                const store = window.__PUB_STORE__;
                let storeImgs = 0;
                try {
                    const ms = store ? store.getState().merchandiseSubmit || {} : {};
                    storeImgs = ((ms.merchandise || {}).images || ms.images || []).length;
                } catch(e) {}
                const domImgs = document.querySelectorAll('img[src*="img.yec.tw"]').length;
                const bgImgs = document.querySelectorAll('[style*="img.yec.tw"]').length;
                return {intercepted, storeImgs, domImgs, bgImgs};
            }""")
            _plog(f"[Redux] CDN URL 等待中 ({elapsed:.0f}s/{timeout_s}s): "
                  f"已提取={len(urls) if urls else 0}/{expected_count}, "
                  f"拦截器={_diag.get('intercepted',0)}, "
                  f"store={_diag.get('storeImgs',0)}, "
                  f"DOM_img={_diag.get('domImgs',0)}, "
                  f"bg_img={_diag.get('bgImgs',0)}")
            _last_log = elapsed
        await page.wait_for_timeout(1000)
    # 最后一次
    urls = await page.evaluate(_JS_EXTRACT_IMG_URLS) or []
    if urls:
        _plog(f"[Redux] 圖片URL不足(預期{expected_count}, 實際{len(urls)}), 但仍使用已有的")
        return urls
    raise RuntimeError(f"圖片上傳後未能取得CDN URL (等待{timeout_s}s, 預期{expected_count}張)")


async def _redux_setup_image_interceptor(page) -> None:
    """注入网络拦截器，在图片上传成功后自动收集 CDN URL 到 window.__PUB_IMG_URLS__。"""
    await page.evaluate("""() => {
        window.__PUB_IMG_URLS__ = [];
        // 辅助: 判断是否为原始(稳定)图片URL，排除含时效token的变换URL
        const _isRawUrl = (u) => u && u.includes('img.yec.tw') && u.includes('/images/') && !u.includes('/cl/api/res/');
        const _addUrl = (u) => {
            if (u && typeof u === 'string' && u.includes('img.yec.tw')
                && !window.__PUB_IMG_URLS__.includes(u)) {
                window.__PUB_IMG_URLS__.push(u);
            }
        };
        // 从上传响应中智能提取: 优先 src.url (原始URL)，其次递归提取
        const _extractFromResponse = (j) => {
            if (!j) return;
            // 策略A: 结构化响应 {src: {url: "..."}} — 奇摩軟件使用此格式
            if (j.src && j.src.url && _isRawUrl(j.src.url)) {
                _addUrl(j.src.url);
                return;
            }
            // 策略B: 递归提取，优先原始URL (/images/ 路径)
            const rawUrls = [];
            const otherUrls = [];
            const collect = (o) => {
                if (!o) return;
                if (typeof o === 'string' && o.includes('img.yec.tw')) {
                    if (_isRawUrl(o)) rawUrls.push(o);
                    else otherUrls.push(o);
                } else if (Array.isArray(o)) { o.forEach(collect); }
                else if (typeof o === 'object') { Object.values(o).forEach(collect); }
            };
            collect(j);
            // 优先用原始URL，没有才用其他URL
            const urls = rawUrls.length > 0 ? rawUrls : otherUrls;
            urls.forEach(_addUrl);
        };
        // 拦截 XHR
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m, u) {
            this.__url = u;
            return origOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function() {
            this.addEventListener('load', function() {
                try {
                    if (this.responseText && this.responseText.includes('img.yec.tw')) {
                        _extractFromResponse(JSON.parse(this.responseText));
                    }
                } catch(e) {}
            });
            return origSend.apply(this, arguments);
        };
        // 拦截 fetch
        const origFetch = window.fetch;
        window.fetch = async function() {
            const resp = await origFetch.apply(this, arguments);
            try {
                const clone = resp.clone();
                clone.text().then(t => {
                    if (t && t.includes('img.yec.tw')) {
                        try { _extractFromResponse(JSON.parse(t)); } catch(e2) {}
                    }
                }).catch(() => {});
            } catch(e) {}
            return resp;
        };
    }""")


# JS: 获取分类属性 — 从 Redux state 的 customAttributes 转换为 API 提交格式
# API 正确格式: [{title: "流通發行形式狀況", values: ["非現行流通貨幣"], fail: false}]
# Redux state customAttributes 格式:
#   [{id, title, required, select, options: [{value, name, sn}, ...]}]
_JS_FETCH_CAT_ATTRS = """(catId) => {
    const store = window.__PUB_STORE__;
    if (!store) return [];
    const ms = store.getState().merchandiseSubmit || {};
    const ca = ms.customAttributes || [];
    if (ca.length === 0) {
        console.log('[CAT_ATTRS] customAttributes is empty (category not yet selected via UI)');
        return [];
    }
    console.log('[CAT_ATTRS] customAttributes found:', JSON.stringify(ca).substring(0, 1000));
    // 转换为 API 提交格式: {title, values: [第一个选项], fail: false}
    const result = ca.map(a => {
        const opts = a.options || [];
        let val = '';
        if (opts.length > 0) {
            const o = opts[0];
            val = (typeof o === 'object') ? (o.value || o.name || '') : String(o);
        }
        if (!val && a.title) val = '';  // 有标题但无选项，仍然保留
        return { title: a.title || '', values: val ? [val] : [], fail: false };
    }).filter(a => a.title && a.values.length > 0);
    console.log('[CAT_ATTRS] converted to API format:', JSON.stringify(result));
    return result;
}"""


async def _redux_fetch_category_attrs(page, cat_id: str) -> list:
    """通过 Yahoo API 或 Redux state 获取分类属性默认值。"""
    # 临时监听 console 输出（抓取 JS 的诊断日志）
    _console_msgs = []
    def _on_console(msg):
        text = msg.text
        if '[CAT_ATTRS]' in text:
            _console_msgs.append(text)
    page.on("console", _on_console)
    try:
        attrs = await asyncio.wait_for(
            page.evaluate(_JS_FETCH_CAT_ATTRS, str(cat_id)),
            timeout=8,
        )
        # 记录 console 输出到日志
        for cm in _console_msgs:
            _plog(f"[Redux] console: {cm[:300]}")
        if isinstance(attrs, list) and attrs:
            _plog(f"[Redux] cat_id={cat_id} 需要属性: {json.dumps(attrs, ensure_ascii=False)}")
            return attrs
        _plog(f"[Redux] cat_id={cat_id} Redux state 无属性，尝试 API 获取...")
    except Exception as _e:
        for cm in _console_msgs:
            _plog(f"[Redux] console: {cm[:300]}")
        _plog(f"[Redux] cat_id={cat_id} 属性获取异常: {_e}，尝试 API 获取...")
    finally:
        try:
            page.remove_listener("console", _on_console)
        except Exception:
            pass

    # ── 后备: 通过 /fe/api/categoryChildren API 获取属性 ──
    try:
        api_attrs = await _fetch_attrs_via_api(page, cat_id)
        if api_attrs:
            _plog(f"[Redux] cat_id={cat_id} API 获取属性成功: {json.dumps(api_attrs, ensure_ascii=False)}")
            _save_cat_attrs_cache(cat_id, api_attrs)
            return api_attrs
    except Exception as _api_err:
        _plog(f"[Redux] cat_id={cat_id} API 属性获取失败: {_api_err}")
    return []


# JS: 通过 /fe/api/categoryChildren 获取某分类的属性
_JS_FETCH_ATTRS_API = """(catId) => {
    // 先查该分类在 categoryTree 中的 parent_cat_id
    const store = window.__PUB_STORE__;
    let parentId = null;
    if (store) {
        const ct = (store.getState().merchandiseSubmit || {}).categoryTree || {};
        const entry = ct[catId] || ct[String(catId)];
        if (entry) parentId = String(entry.parent_cat_id || '0');
    }
    if (!parentId) return {error: 'no parent found for ' + catId};

    return fetch('/fe/api/categoryChildren?id=' + parentId, {credentials: 'include'})
        .then(r => r.json())
        .then(data => {
            const children = data.response_data || [];
            for (const ch of children) {
                if (String(ch.cat_id) === String(catId)) {
                    let attrs = ch.attributes || '';
                    if (typeof attrs === 'string' && attrs.trim()) {
                        try { attrs = JSON.parse(attrs); }
                        catch(e) { return {raw: attrs}; }
                    }
                    if (attrs && typeof attrs === 'object' && !Array.isArray(attrs)) {
                        // 转换 {title: {type, value, required}} -> [{title, values, fail}]
                        const result = [];
                        for (const [title, info] of Object.entries(attrs)) {
                            if (!info || typeof info !== 'object') continue;
                            const vals = info.value;
                            let firstVal = '';
                            if (Array.isArray(vals) && vals.length > 0) {
                                firstVal = String(vals[0]);
                            }
                            if (firstVal) {
                                result.push({title: title, values: [firstVal], fail: false});
                            }
                        }
                        return result;
                    }
                    return [];  // 该分类没有属性
                }
            }
            return {error: 'cat_id not found in parent children'};
        })
        .catch(e => ({error: e.message}));
}"""


async def _fetch_attrs_via_api(page, cat_id: str) -> list:
    """通过 Yahoo /fe/api/categoryChildren API 获取分类属性。"""
    result = await asyncio.wait_for(
        page.evaluate(_JS_FETCH_ATTRS_API, str(cat_id)),
        timeout=10,
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and result.get("error"):
        _plog(f"[CatAttrsAPI] cat_id={cat_id} error: {result['error']}")
    return []


def _redux_build_merchandise(
    *, title: str, brief: str, desc: str, image_urls: List[str],
    category_id: str, price: str, qty: str, condition: str,
    location: str, payments: List[str], category_attrs: list = None,
    shipments: dict = None, cat_kw: str = "",
    hashtags: Optional[List[str]] = None,
) -> dict:
    """从 Excel 行数据构建 Redux dispatch 所需的 merchandise payload。

    cat_kw(v6.0.50): Excel「拍賣類別名稱」原文,用於判斷大類自動加「收藏品」標籤。
    hashtags: Excel「標籤」欄解析後的字串列表(已符合 Yahoo 4 條規則)。
    """
    # 状态映射
    cond_text = _map_condition_to_radio_text(condition)
    use_status = "used" if "二手" in cond_text else "new"

    # brief 清理：Yahoo 簡述是单行 input，不接受换行符
    _clean_brief = (brief or "").replace("\n", " ").replace("\r", " ").strip()

    # location 归一化 — 台↔臺 / 市↔縣 匹配
    _matched_loc = _http_match_location(location)

    # 价格标准化
    try:
        price_val = f"{float(price.replace(',', '')):.2f}"
    except (ValueError, AttributeError):
        price_val = price or "0"

    # 数量标准化
    try:
        qty_val = str(int(float(qty.replace(",", ""))))
    except (ValueError, AttributeError):
        qty_val = qty or "1"

    # v6.0.50: 大類為古董/偶像 + 二手品 → 自動填「收藏品」標籤
    _labels = _build_item_labels(cat_kw, use_status)

    return {
        "type": "basic",
        "title": title,
        "description": {"brief": _clean_brief, "detail": desc or ""},
        "hashtags": list(hashtags) if hashtags else [],
        "labels": _labels,
        "images": image_urls,
        "location": _matched_loc,
        "video": {},
        "useStatus": use_status,
        "category": {"id": str(category_id), "attributes": category_attrs or []},
        "payments": payments,
        "purchaseLimit": {"minQuantity": "", "maxQuantity": ""},
        "presale": {},
        "shipments": shipments if shipments is not None else {"isApplyShippingRule": True},
        "product": {
            "models": [{
                "quantity": qty_val,
                "price": {"selling": price_val},
                "partNumber": {"first": "", "second": ""},
                "barcode": "",
            }],
        },
        "buyMorePromotions": [],
        "saveLocation": True,
    }


async def _redux_dispatch_publish(page, wssid: str, merchandise: dict,
                                   timeout_s: float = 30.0) -> dict:
    """dispatch FETCH_PUBLISH_MERCHANDISE 并等待结果。返回 {id, title, ...} 或抛异常。"""
    action = {
        "type": "CALL_RESERVICE",
        "payload": {
            "wssid": wssid,
            "merchandise": merchandise,
        },
        "reservice": {
            "name": "FETCH_PUBLISH_MERCHANDISE",
            "start": "FETCH_PUBLISH_MERCHANDISE_START",
            "state": "BEGIN",
        },
        "rtk2": True,
    }

    if _REDUX_DRY_RUN:
        _plog(f"[Redux DRY-RUN] payload={json.dumps(action, ensure_ascii=False)[:500]}")
        return {"id": "DRY_RUN_0000", "title": merchandise.get("title", "")}

    result = await page.evaluate(_JS_DISPATCH_AND_WAIT, json.dumps(action, ensure_ascii=False))
    if not result:
        raise RuntimeError("Redux dispatch returned null")

    if isinstance(result, dict) and result.get("error"):
        errors = result.get("errors") or {}
        if errors:
            detail = json.dumps(errors, ensure_ascii=False)[:200]
        else:
            detail = result.get("message", str(result))[:200]
        _plog(f"[Redux] dispatch 错误详情: errors={json.dumps(errors, ensure_ascii=False)}, "
              f"full_result={json.dumps(result, ensure_ascii=False)[:500]}")
        raise RuntimeError(f"Yahoo API: {detail}")

    if isinstance(result, dict) and result.get("id"):
        return result

    raise RuntimeError(f"Redux dispatch 結果異常: {str(result)[:200]}")


async def _publish_one_item_redux(
    page, *, wssid: str, title: str, brief: str, desc: str,
    pics: List[str], cat_kw: str, price: str, qty: str,
    condition: str, location: str, category_lookup: Dict[str, str],
    payments: List[str], shipments: dict = None,
    upload_wait_s: float, account_name: str,
    tracer, app_log: Callable[[str], None],
    hashtags: Optional[List[str]] = None,
) -> str:
    """单件商品 Redux 快速刊登。返回商品编号 str，失败抛异常。"""
    _ts = lambda: time.strftime("%H:%M:%S")

    # 0) 必须先选「直購品」，否则 type-picker overlay 挡住整个表单
    app_log(f"[REDUX {_ts()}] {account_name}: [1/5] 選擇「直購品」...")
    await _ensure_direct_buy(page)

    # 1) 解析分类
    cat_id = _redux_resolve_category_id(cat_kw, category_lookup)
    app_log(f"[REDUX {_ts()}] {account_name}: [2/5] 分類匹配 '{cat_kw[:30]}' -> id={cat_id}")

    # 1.5) 获取分类属性：优先从缓存，否则从 Redux state
    cat_attrs = _get_cached_cat_attrs(cat_id)
    if cat_attrs:
        _plog(f"[Redux] cat_id={cat_id} 使用缓存属性: {json.dumps(cat_attrs, ensure_ascii=False)[:200]}")
    else:
        cat_attrs = await _redux_fetch_category_attrs(page, cat_id)

    # 2) 上传图片（仍走 file input）
    image_urls = []
    if pics:
        app_log(f"[REDUX {_ts()}] {account_name}: [3/5] 上傳 {len(pics)} 張圖片 + 等待CDN...")
        # 注入网络拦截器，在图片上传过程中自动抓取 CDN URL
        try:
            await _redux_setup_image_interceptor(page)
        except Exception:
            pass
        await _upload_images(page, pics, upload_wait_s=upload_wait_s)
        try:
            await tracer.snap("redux_after_upload")
        except Exception:
            pass
        # 3) 提取 CDN URL
        image_urls = await _redux_extract_image_urls(page, len(pics))
        app_log(f"[REDUX {_ts()}] {account_name}: [3/5] 取得 {len(image_urls)} 張CDN URL ✓")
    else:
        app_log(f"[REDUX {_ts()}] {account_name}: [3/5] 無圖片，跳過")

    # 4) 构建 payload
    app_log(f"[REDUX {_ts()}] {account_name}: [4/5] 構建 Redux payload (標題={title[:20]}...)")
    merchandise = _redux_build_merchandise(
        title=title, brief=brief, desc=desc,
        image_urls=image_urls, category_id=cat_id,
        price=price, qty=qty, condition=condition,
        location=location, payments=payments,
        category_attrs=cat_attrs, shipments=shipments,
        cat_kw=cat_kw,  # v6.0.50: 透傳給「收藏品」自動填邏輯
        hashtags=hashtags,
    )

    # 5) dispatch（如果 item-attributes / item-payments 失败，重试一次）
    app_log(f"[REDUX {_ts()}] {account_name}: [5/5] store.dispatch() 發送刊登請求...")
    try:
        result = await _redux_dispatch_publish(page, wssid, merchandise)
    except RuntimeError as _attr_err:
        _err_msg = str(_attr_err)
        if "item-payments" in _err_msg:
            # 记录诊断信息后直接抛出
            _plog(f"[Redux] item-payments 错误(payments={merchandise['payments']}, "
                  f"shipments={merchandise.get('shipments')})")
            raise
        elif "item-attributes" in _err_msg and not cat_attrs:
            # 第一次因缺属性失败 → 尝试获取属性后重试
            _plog(f"[Redux] item-attributes 错误，尝试获取分类属性后重试...")
            app_log(f"[REDUX {_ts()}] {account_name}: 属性缺失，获取默认属性后重试...")
            cat_attrs_retry = await _redux_fetch_category_attrs(page, cat_id)
            if cat_attrs_retry:
                merchandise["category"]["attributes"] = cat_attrs_retry
                _plog(f"[Redux] 重试 attrs={json.dumps(cat_attrs_retry, ensure_ascii=False)}")
                result = await _redux_dispatch_publish(page, wssid, merchandise)
                # 重试成功，缓存属性供下次直接使用
                _save_cat_attrs_cache(cat_id, cat_attrs_retry)
            else:
                # 诊断：dump merchandiseSubmit 的 key 结构帮助调试
                try:
                    _ms_dump = await page.evaluate("""() => {
                        const s = window.__PUB_STORE__;
                        if (!s) return 'no store';
                        const st = s.getState();
                        const ms = st.merchandiseSubmit || {};
                        const keys = Object.keys(ms);
                        // 找出哪些 key 包含 array
                        const detail = {};
                        for (const k of keys) {
                            const v = ms[k];
                            if (Array.isArray(v)) detail[k] = 'Array(' + v.length + ')';
                            else if (v && typeof v === 'object') detail[k] = 'Object(' + Object.keys(v).length + 'keys)';
                            else detail[k] = typeof v;
                        }
                        return JSON.stringify(detail);
                    }""")
                    _plog(f"[Redux] 属性获取失败，merchandiseSubmit 结构: {_ms_dump}")
                except Exception:
                    pass
                raise  # 拿不到属性，回退 UI
        else:
            raise

    code = str(result.get("id", ""))
    if not code:
        raise RuntimeError("dispatch 成功但返回無商品編號")
    app_log(f"[REDUX {_ts()}] {account_name}: 刊登成功! 商品編號={code} ✓")
    return code


async def _run_publish_for_excel(
    *,
    app_log: Callable[[str], None],
    stop_flag: threading.Event,
    chrome_path: str,
    profile_dir: Path,
    xlsx_path: Path,
    account_name: str,
    headless: bool,
    step_delay: float,
    proxy: str,
    on_progress: Callable[[int, int, int, str], None],
    debug_dir: Path,
    debug_steps: bool,
    save_html: bool,
    upload_wait_s: float,
    humanize_cfg: HumanizeConfig,
    retry_pending_review: bool,
    goto_lock: asyncio.Lock | None = None,
    inject_cookies: list | None = None,
    pw_instance=None,
    pause_event: asyncio.Event | None = None,
    _pause_notify: Callable[[str], None] | None = None,
) -> None:
    """單帳號：讀 Excel -> 逐行刊登 -> 回寫商品編號"""

    _plog(f"[{account_name}] load excel -> {xlsx_path.name}")

    # 每個並發任務獨立套用「人類化節奏」設定（不互相污染）
    try:
        _HUMANIZE_CFG.set(humanize_cfg or HumanizeConfig())
    except Exception:
        pass

    orig_xlsx = xlsx_path
    xlsx_path = _ensure_done_excel(xlsx_path)
    if xlsx_path != orig_xlsx:
        _plog(f"[{account_name}] write excel -> {xlsx_path.name}（保留原檔 {orig_xlsx.name}）")

    wb, ws, headers, todo_rows = _load_rows_from_excel(xlsx_path, retry_pending_review=bool(retry_pending_review))
    if not todo_rows:
        app_log(f"[PUBLISH {_ts()}] {account_name}: 无待刊登商品（商品編號已存在或標題空）")
        return

    success = 0
    fail = 0

    # ═══════════════════════════════════════════════════════════════
    # 纯 HTTP 模式（完全不开浏览器）
    # 有 inject_cookies → 从中提取 Yahoo cookies
    # 无 inject_cookies → 从 profile_dir 的 cookie_cache.json 读取
    # ═══════════════════════════════════════════════════════════════
    try:
        _yahoo_ck = None
        if inject_cookies:
            _yahoo_ck = {
                c["name"]: c["value"]
                for c in inject_cookies
                if "yahoo" in c.get("domain", "").lower()
            }
            if not _yahoo_ck:
                raise RuntimeError("inject_cookies 无 Yahoo 域 cookie")

        # 1) 创建 HTTP session
        if _yahoo_ck:
            _ho_sess, _ho_wssid, _, _ho_err = _http_create_session(
                None, cookies_override=_yahoo_ck,
            )
        else:
            _ho_sess, _ho_wssid, _, _ho_err = _http_create_session(profile_dir, max_age=2592000)
        if _ho_err:
            raise RuntimeError(f"session: {_ho_err}")

        # 2) HTTP GET 发布页 → 解析 isoredux-data → 获取配置
        _ho_state, _ho_err = await asyncio.to_thread(_http_fetch_page, _ho_sess)
        if _ho_err:
            raise RuntimeError(f"publish page: {_ho_err}")

        _ho_cfg = _http_extract_config(_ho_state)
        _ho_wssid = _ho_cfg["wssid"] or _ho_wssid
        if not _ho_wssid:
            raise RuntimeError("无 wssid")

        _ho_payments = _ho_cfg["payments"]
        _ho_shipments = {"isApplyShippingRule": True}
        _ho_loc_options = _ho_cfg.get("location_options") or []

        # 分类查找表
        _ho_cat_tree = (_ho_state.get("merchandiseSubmit") or {}).get("categoryTree") or {}
        if not isinstance(_ho_cat_tree, dict) or not _ho_cat_tree:
            _ho_cat_tree = _ho_cfg.get("category_tree") or {}
        _ho_cat_lookup = _redux_build_category_lookup(_ho_cat_tree) if _ho_cat_tree else {}
        if not _ho_cat_lookup:
            raise RuntimeError("分类树为空")

        _load_cat_attrs_cache(str(profile_dir))
        app_log(f"[PUBLISH {_ts()}] {account_name}: HTTP-only 模式 "
                f"(wssid={_ho_wssid[:8]}... cats={len(_ho_cat_lookup)} payments={_ho_payments})")

        # v6.0.72:記下 session 起始時間,401 診斷時用來算 wssid 已存活多久
        import time as _t_mod
        _ho_session_start_ts = _t_mod.time()
        _ho_session_start_iso = _t_mod.strftime("%Y-%m-%d %H:%M:%S", _t_mod.localtime(_ho_session_start_ts))
        _auth_err_count_total = 0  # 全程累計,給診斷 log 用

        # 3) 逐行处理（图片使用直传 API，不需要 S3 凭证）
        _consecutive_fail = 0
        for idx, r in enumerate(todo_rows, start=1):
            # v6.1.45 診斷:進入 for loop 下一輪(用來抓「sleep 結束 → 真正進入 row 邏輯」之間的延遲)
            _t_row_enter = _t_mod.time()
            app_log(f"[HTTP-PUB-DIAG {_ts()}] {account_name}: 進入 row={r} 迭代")

            if stop_flag.is_set():
                break
            # 全局 VPN 熔断：其它账号收到 403037 → 本账号也停（避免继续浪费 + 加重限流）
            if is_vpn_down():
                app_log(f"[HTTP-PUB {_ts()}] {account_name}: 检测到全局 VPN 熔断 ({get_vpn_down_reason()})，停止")
                _save_wb(wb, xlsx_path)
                on_progress(success, fail, r, "全局 VPN 熔断，已停止")
                return

            # ★ 暫停檢查：如果暫停了，先保存 Excel，然後等待恢復
            if pause_event is not None and not pause_event.is_set():
                _save_wb(wb, xlsx_path)
                on_progress(success, fail, r, "")
                # 通知 UI：此帳號已真正暫停（從「暫停中...」變為「已暫停」）
                if _pause_notify:
                    _pause_notify(account_name)
                app_log(f"[HTTP-PUB] {account_name}: 已暫停，等待恢復...")
                await pause_event.wait()
                if stop_flag.is_set():
                    break
                app_log(f"[HTTP-PUB] {account_name}: 已恢復，繼續刊登")

            on_progress(success, fail, r, "")

            def g(col: str) -> str:
                c = headers.get(col)
                if not c:
                    return ""
                return _norm_str(ws.cell(row=r, column=c).value)

            title = g("標題")
            brief = g("商品簡述")
            price = g("起標價")
            qty = g("數量")
            loc = g("所在地")
            condition = g("商品狀況")
            desc = g("說明")
            cat_kw = g("拍賣類別")
            pics = _split_picture_paths(g("圖片"))
            hashtags = _parse_hashtags(g("標籤"))

            if '?' in title and pics:
                fixed_title = _fix_title_from_pics(title, pics)
                if fixed_title != title:
                    title = fixed_title

            app_log(f"[HTTP-PUB {_ts()}] {account_name}: row={r} ({idx}/{len(todo_rows)}) {title[:30]}")

            _row_err = ""
            _row_code = ""
            try:
                # 分类解析
                _ho_cat_id = _redux_resolve_category_id(cat_kw, _ho_cat_lookup)
                _ho_cat_attrs = _get_cached_cat_attrs(_ho_cat_id)

                # 上传图片（直传 API，不需要 S3 凭证）
                cdn_urls = []
                if pics:
                    # v6.1.45 診斷:上傳開始
                    _t_upload_start = _t_mod.time()
                    app_log(f"[HTTP-PUB-DIAG {_ts()}] {account_name}: 準備上傳 {len(pics)} 張圖 row={r}")
                    cdn_urls, _ie = await asyncio.to_thread(
                        _http_upload_images, _ho_sess, pics, None, _plog,
                        _ho_wssid,
                    )
                    if _ie:
                        raise RuntimeError(f"图片: {_ie}")
                    # v6.1.45 診斷:上傳完成,可分辨 thread pool 排隊 vs 純圖多/慢
                    _upload_dt = _t_mod.time() - _t_upload_start
                    app_log(
                        f"[HTTP-PUB-DIAG {_ts()}] {account_name}: 圖片上傳完成,"
                        f"耗時 {_upload_dt:.1f}s({len(pics)} 張 = 平均 {_upload_dt/max(1, len(pics)):.1f}s/張) row={r}"
                    )

                # 构建 payload（location 归一化：台↔臺/市↔縣 匹配）
                _matched_loc = _http_match_location(loc, _ho_loc_options or None)
                if _matched_loc != loc and loc:
                    app_log(f"[HTTP-PUB] {account_name}: 所在地自动修正 '{loc}' → '{_matched_loc}'")
                _m = _http_build_merchandise(
                    title=title, brief=brief, desc=desc,
                    image_urls=cdn_urls, category_id=_ho_cat_id,
                    price=price, qty=qty, condition=condition,
                    location=_matched_loc, payments=_ho_payments,
                    category_attrs=_ho_cat_attrs,
                    shipments=_ho_shipments,
                    location_options=_ho_loc_options,
                    cat_kw=cat_kw,  # v6.0.50: 透傳給「收藏品」自動填邏輯
                    hashtags=hashtags,
                )

                # 限流延迟(v6.1.48: 1.5-4.0s → 1.0-2.5s,單筆提速 ~1s)
                _delay = random.uniform(1.0, 2.5)
                await asyncio.sleep(_delay)

                # 提交
                _res, _se = await asyncio.to_thread(
                    _http_submit_merchandise, _ho_sess, _ho_wssid, _m,
                )
                # v6.1.47:Yahoo 網關錯誤(502/503/504)自動重試一次,Yahoo UDB backend 短暫故障常見
                # 修「sjgxjcjfuffhkssv/tiojky7/cakrawijayaa09 撞 UDB validation unavailable 直接 FAIL」bug
                if _se and any(_tag in _se for _tag in ("503", "504", "502", "Gateway", "BadGateway", "UDB validation")):
                    app_log(
                        f"[HTTP-PUB {_ts()}] {account_name}: row={r} Yahoo 網關錯誤({_se[:80]}),"
                        f"10s 後重試一次"
                    )
                    await asyncio.sleep(10)
                    _res, _se = await asyncio.to_thread(
                        _http_submit_merchandise, _ho_sess, _ho_wssid, _m,
                    )
                    if not _se:
                        app_log(f"[HTTP-PUB {_ts()}] {account_name}: row={r} 網關錯誤重試成功")
                if _se:
                    raise RuntimeError(_se)
                _row_code = str(_res.get("id", ""))
                if not _row_code:
                    raise RuntimeError("响应无商品编号")

            except Exception as _re:
                _row_err = str(_re)

            # 写回 Excel
            if _row_code and not _row_err:
                ws.cell(row=r, column=headers["商品編號"], value=_row_code)
                ws.cell(row=r, column=headers["刊登狀態"], value="成功")
                ws.cell(row=r, column=headers["刊登錯誤"], value="")
                if cat_kw:
                    ws.cell(row=r, column=headers.get("實際分類", headers.get("拍賣類別")), value=cat_kw)
                success += 1
                _consecutive_fail = 0  # 成功则重置连续失败计数
                app_log(f"[HTTP-PUB {_ts()}] {account_name}: OK row={r} code={_row_code}")
            else:
                ws.cell(row=r, column=headers["刊登狀態"], value="失败")
                ws.cell(row=r, column=headers["刊登錯誤"], value=_row_err[:200])
                fail += 1
                # 401/凭证类错误连续计数（其他错误不累计，避免偶发错误误触熔断）
                # v6.0.72:移除 "Missing" 死碼(實測 185 筆 error log 中 0 次命中,純佔位)
                #   加 "Unauthorized" / "ExpiredToken" 大小寫匹配,涵蓋更多真實 auth 錯誤模式
                #   精確 "missing required header" / "missing wssid" 取代廣義 "Missing"
                _err_lower = _row_err.lower()
                _is_auth_err = (
                    "401" in _row_err
                    or "expiredtoken" in _err_lower
                    or "unauthorized" in _err_lower
                    or "missing required header" in _err_lower
                    or "missing wssid" in _err_lower
                )
                if _is_auth_err:
                    _consecutive_fail += 1
                    _auth_err_count_total += 1
                    # v6.0.72:每次 auth error 都 dump 詳細 JSON 診斷檔(下次出現可以直接定位根因)
                    try:
                        import json as _json
                        from datetime import datetime as _dt
                        _diag_dir = Path("publish_logs") / "auth_diagnostics"
                        _diag_dir.mkdir(parents=True, exist_ok=True)
                        _ts_str = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
                        _safe_acc = (account_name or "unknown").replace("@", "_at_").replace("/", "_")
                        _diag_path = _diag_dir / f"{_safe_acc}_{_ts_str}.json"
                        _elapsed = _t_mod.time() - _ho_session_start_ts
                        _diag_data = {
                            "timestamp": _dt.now().isoformat(),
                            "account": account_name,
                            "row": r,
                            "row_index_in_batch": idx,
                            "consecutive_fail": _consecutive_fail,
                            "auth_err_count_total": _auth_err_count_total,
                            "session_start": _ho_session_start_iso,
                            "wssid_age_seconds": round(_elapsed, 1),
                            "wssid_age_minutes": round(_elapsed / 60, 1),
                            "wssid_prefix": (_ho_wssid or "")[:16],
                            "wssid_length": len(_ho_wssid or ""),
                            "successes_so_far": success,
                            "failures_so_far": fail,
                            "rows_processed": idx,
                            "error_message": _row_err[:1000],
                            "error_lower_match": {
                                "has_401": "401" in _row_err,
                                "has_expiredtoken": "expiredtoken" in _err_lower,
                                "has_unauthorized": "unauthorized" in _err_lower,
                                "has_missing_required_header": "missing required header" in _err_lower,
                                "has_missing_wssid": "missing wssid" in _err_lower,
                            },
                            "row_data": {
                                "title": title[:100] if title else "",
                                "category_kw": cat_kw,
                                "image_count": len(pics),
                                "hashtag_count": len(hashtags) if hashtags else 0,
                            },
                        }
                        _diag_path.write_text(
                            _json.dumps(_diag_data, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        app_log(f"[HTTP-PUB {_ts()}] {account_name}: ★ 認證失敗診斷已記錄 → "
                                f"publish_logs/auth_diagnostics/{_diag_path.name} "
                                f"(wssid 已用 {_diag_data['wssid_age_minutes']:.1f} 分鐘,本批已成功 {success} 筆)")
                    except Exception as _diag_e:
                        app_log(f"[HTTP-PUB {_ts()}] {account_name}: 診斷 dump 失敗: {_diag_e}")
                else:
                    _consecutive_fail = 0
                app_log(f"[HTTP-PUB {_ts()}] {account_name}: FAIL row={r} err={_row_err[:80]}")

                # 熔断 1：连续 5 次认证失败 → 停止该账号
                if _consecutive_fail >= 5:
                    app_log(f"[HTTP-PUB {_ts()}] {account_name}: 连续 {_consecutive_fail} 次认证失败(401/ExpiredToken/Unauthorized),停止刊登")
                    app_log(f"[HTTP-PUB {_ts()}] {account_name}: ★ 完整診斷檔在 publish_logs/auth_diagnostics/{_safe_acc}_*.json,可分析根因")
                    _save_wb(wb, xlsx_path)
                    on_progress(success, fail, r, f"连续认证失败{_consecutive_fail}次,已停止(诊断已记录)")
                    return

                # 熔断 2：403037 (Yahoo Operation is not allowed) — 多半是 VPN 异常 / IP 不是台湾
                # 单账号遇 1 次就停（再发只会浪费），同时记录跨账号事件
                if "403037" in _row_err or "Operation is not allowed" in _row_err:
                    triggered_global, reason = report_vpn_error(account_name, _row_err)
                    app_log(f"[HTTP-PUB {_ts()}] {account_name}: 检测到 403037 (Yahoo 拒绝刊登，多半 VPN 异常)，停止该账号")
                    if triggered_global:
                        app_log(f"[HTTP-PUB {_ts()}] ⚠ 全局熔断：{reason} — 所有账号停止刊登")
                    _save_wb(wb, xlsx_path)
                    on_progress(success, fail, r, "403037: VPN/IP 异常，已停止")
                    return

            # 每 5 行保存一次
            if idx % 5 == 0:
                _save_wb(wb, xlsx_path)

            on_progress(success, fail, r, _row_err)

            # 行间限流延迟(v6.1.48: 2.0-5.0s → 1.5-3.0s,行間提速 ~1.5s)
            if idx < len(todo_rows):
                _gap = random.uniform(1.5, 3.0)
                app_log(f"[HTTP-PUB {_ts()}] {account_name}: 等待 {_gap:.1f}s...")
                # v6.1.45 診斷:記錄 sleep 開始時間,sleep 結束後算實際耗時
                _t_sleep_start = _t_mod.time()
                await asyncio.sleep(_gap)
                _t_sleep_actual = _t_mod.time() - _t_sleep_start
                # 實際 sleep 跟預期差 > 1s = event loop 被阻塞
                if abs(_t_sleep_actual - _gap) > 1.0:
                    app_log(
                        f"[HTTP-PUB-DIAG {_ts()}] {account_name}: ⚠ 行間 sleep 預期 {_gap:.1f}s 實際 {_t_sleep_actual:.1f}s "
                        f"(差 {_t_sleep_actual - _gap:+.1f}s,event loop 被阻塞)"
                    )

        _save_wb(wb, xlsx_path)
        app_log(f"[HTTP-PUB {_ts()}] {account_name}: 完成 成功={success} 失败={fail}")

    except Exception as _ho_init_err:
        app_log(f"[PUBLISH {_ts()}] {account_name}: HTTP-only 初始化失败({_ho_init_err})，标记全部失败")
        _plog(f"[{account_name}] HTTP-only init failed: {_ho_init_err}")
        # 不回退浏览器，直接标记所有行为失败
        status_col = headers.get("刊登狀態")
        err_col = headers.get("錯誤信息")
        for r in todo_rows:
            fail += 1
            if status_col:
                ws.cell(row=r, column=status_col, value="失败")
            if err_col:
                ws.cell(row=r, column=err_col, value=f"HTTP初始化失败: {_ho_init_err}")
        try:
            _save_wb(wb, xlsx_path)
        except Exception:
            pass
        on_progress(success, fail, todo_rows[-1] if todo_rows else 0, str(_ho_init_err))

    on_progress(success, fail, todo_rows[-1] if todo_rows else 0, "")
    return
