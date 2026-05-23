"""业绩汇总功能 — 解析 出货资料.xlsx → 同步到 D1 → 显示

数据流：
  本地 出货资料_YYYYMMDD.xlsx (采购出货生成)
    ↓ 解析两个 sheet：线上贴单资料 / 宅配打包资料
    ↓ 计算业绩字段（人民币/运费/派送费/利润）
    ↓ POST /api/performance/upload (按 PK = code+order_code 去重)
  Cloudflare Worker D1
    ↓ GET /api/performance/query / summary
  本地 UI：
    - 员工：只看自己 (owner = settings.paystatus_name)
    - 主管：看全部 (chat_id = <SUPERVISOR_CHAT_ID> 自动放权)

防篡改：D1 INSERT OR IGNORE，已存在的 PK 不能改。退款/调整走 adjustment 表。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import openpyxl
import requests as _req

LogFn = Callable[[str], None]

# ── 配置 ─────────────────────────────────────────────────────
WORKER_URL = "https://product-query.<PHONE_REDACTED>.workers.dev"
SHIPPING_DIR_DEFAULT = Path(r"C:\Users\<USER>\Desktop\出货资料")
SUPERVISOR_CHAT_ID = "<SUPERVISOR_CHAT_ID>"

# v6.0.51: 阿里雲 Excel 共享 server(各 user 自動上傳出貨資料,主管下載)
SHIPPING_EXCEL_SERVER_URL = "http://<RELAY_IP_REDACTED>:18900"
SHIPPING_EXCEL_TOKEN = "<SHIPPING_TOKEN_REDACTED>"

# 出货资料的两个数据 sheet
SHEET_ONLINE = "线上贴单资料"
SHEET_HOME = "宅配打包资料"

# 业绩计算常量（与现有业绩表公式一致）
TWD_TO_CNY_RATE = 5.54
CNY_PROFIT_FACTOR = 0.9    # 人民币 = 台币 / 5.54 * 0.9
PACK_COST_DEFAULT = 15.0
FREIGHT_PER_QTY = 20.0
DELIVERY_DEFAULT = 21.0


# ── SYB 真實貨物數同步(weight_consign → ceil(kg)) ────────
# 業績原本 qty = 訂單商品數(默認 1),改為 ceil(SYB 托運重量/1000)
# 規則:0.43kg→1, 1.446kg→2, 2.78kg→3 (max(1, math.ceil(g/1000)))
# 來源:順雲寶 ERP /am/stock/list,status=50 已發貨,weight_consign>0
# 緩存:output/syb_weights.db(sqlite,PK = order_code)
# 沒重量(未發貨/未過磅)→ qty 維持 Excel「商品數量」原值(向下兼容)

_SYB_WEIGHTS_DB = Path(__file__).resolve().parent.parent / "output" / "syb_weights.db"


def _weights_db():
    _SYB_WEIGHTS_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_SYB_WEIGHTS_DB), timeout=10.0)
    db.execute("""CREATE TABLE IF NOT EXISTS syb_weights (
        order_code TEXT PRIMARY KEY,
        weight_g INTEGER NOT NULL,
        status INTEGER,
        upload_time TEXT,
        synced_at REAL,
        pushed_to_d1 REAL DEFAULT 0,
        give_up_at REAL DEFAULT 0
    )""")
    # 兼容舊 schema:欄位若不存在就加上
    for col_def in (
        "ALTER TABLE syb_weights ADD COLUMN pushed_to_d1 REAL DEFAULT 0",
        "ALTER TABLE syb_weights ADD COLUMN give_up_at REAL DEFAULT 0",
    ):
        try:
            db.execute(col_def)
        except sqlite3.OperationalError:
            pass
    return db


# 舊孤兒清理門檻:升級前殘留的「不在業績清單」舊記錄,N 天後自然 give_up
# 新邏輯下不會再產生孤兒(sync 時用業績清單過濾),這個門檻只為清理舊資料
SYB_ORPHAN_GIVE_UP_DAYS = 14


# 啟動 auto sync 12h 節流(避免每次重啟都打 SYB API + admin_replace)
_LAST_AUTO_SYNC_FILE = Path(__file__).resolve().parent.parent / "output" / ".syb_last_auto_sync"


def _read_last_auto_sync() -> float:
    try:
        return float(_LAST_AUTO_SYNC_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0.0


def _save_last_auto_sync(ts: float) -> None:
    try:
        _LAST_AUTO_SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_AUTO_SYNC_FILE.write_text(str(ts), encoding="utf-8")
    except Exception:
        pass


def lookup_real_qty(order_code: str) -> Optional[int]:
    """根據 order_code 查 SYB 緩存,返回 ceil(weight_g/1000)。沒有/0 返 None。"""
    code = (order_code or "").strip()
    if not code:
        return None
    try:
        db = _weights_db()
        row = db.execute(
            "SELECT weight_g FROM syb_weights WHERE order_code = ?", (code,)
        ).fetchone()
        db.close()
    except Exception:
        return None
    if not row:
        return None
    w = int(row[0] or 0)
    if w <= 0:
        return None
    return max(1, math.ceil(w / 1000.0))


def sync_syb_weights(log: LogFn = None) -> int:
    """從順雲寶 ERP 抓有重量的訂單,以「業績 D1 清單」為過濾依據:

    業務鏈路:出貨完成 = 上業績 D1 + 上物流系統(SYB)同時發生。
    所以業績 D1 是權威清單,SYB 上不在業績裡的訂單(別來源/別人的/測試)
    跟業績無關,直接跳過,**不入 sqlite,不會產生孤兒**。

    以「訂單狀態」為準(非天數):
    - 已 pushed_to_d1>0 的訂單 → 永久 skip
    - 還沒 pushed 的 → 持續嘗試直到推 D1

    不過濾 SYB status:過磅時機早於「已發貨」(status=50),只看 weight_consign>0。

    SYB API 範圍動態決定:從最早未 push 訂單往前 7 天起算。

    回傳本次新處理(upsert)條數。失敗回 -1。無業績清單回 0。
    """
    if log is None:
        log = lambda *_: None
    try:
        from .syb_http_ops import ensure_stoken, _post as _syb_post, SYBAuthError, _TOKEN_CACHE
    except Exception as e:
        log(f"[SYB-W] 載入 syb_http_ops 失敗:{e}")
        return -1

    # ── 步驟 0:拿 D1 業績訂單清單(過去 90 天)── 權威過濾依據 ──
    # 同時保留 ship_date 用於後續動態 days 計算(覆蓋停跑 sync 多日的場景)
    relevant_codes: set = set()
    oc_to_ship_date: Dict[str, str] = {}
    try:
        d1_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        d1_today = datetime.now().strftime("%Y-%m-%d")
        d1_rows = query_records(owner="", from_date=d1_from, to_date=d1_today, limit=10000)
        if d1_rows:
            for r in d1_rows:
                oc = str(r.get("order_code") or "").strip()
                if not oc:
                    continue
                relevant_codes.add(oc)
                sd = str(r.get("ship_date") or "").strip()
                if sd:
                    oc_to_ship_date[oc] = sd
            log(f"[SYB-W] D1 業績清單(過去 90 天):{len(relevant_codes)} 筆,作為過濾依據")
            if len(d1_rows) >= 10000:
                log(f"[SYB-W] ⚠️ D1 query 達 limit=10000,部分業績可能未進清單")
    except Exception as e:
        log(f"[SYB-W] 取 D1 業績清單失敗:{e},sync 中止避免產生孤兒")
        return -1

    if not relevant_codes:
        log("[SYB-W] D1 業績清單為空,跳過 sync")
        return 0

    # ── 步驟 0.5:立即清理 sqlite 中不在業績清單的記錄(舊邏輯抓進的孤兒)──
    # 既然我們已經知道權威清單,不需要等 14 天 give_up,直接標記
    try:
        db_clean = _weights_db()
        existing_active = {row[0] for row in db_clean.execute(
            "SELECT order_code FROM syb_weights WHERE give_up_at = 0"
        ).fetchall()}
        stale_orphans = existing_active - relevant_codes
        if stale_orphans:
            ts_clean = time.time()
            db_clean.executemany(
                "UPDATE syb_weights SET give_up_at = ? WHERE order_code = ?",
                [(ts_clean, c) for c in stale_orphans],
            )
            db_clean.commit()
            log(f"[SYB-W] 清理 sqlite 舊孤兒:{len(stale_orphans)} 條(舊邏輯抓進的非業績訂單,立即標記放棄)")
        db_clean.close()
    except Exception as e:
        log(f"[SYB-W] 清理舊孤兒失敗:{e}")

    # ── 動態 days:取兩個 source 的最早日期 ────────────────
    # A) sqlite 中還沒 push 的 pending(weight 已抓到但還沒推 D1):min(upload_time)
    # B) D1 業績有但 sqlite 完全沒對應記錄的訂單:min(ship_date)
    #    (停跑 sync N 天的場景:新業績一直在 D1,但 sqlite 沒對應,
    #     若只看 A,sqlite 沒 pending → days=14 → 抓不到 14 天前的業績)
    # 取兩者最早的,確保覆蓋所有需要 sync 的訂單
    days_default = 14
    days = days_default
    candidates: List[datetime] = []
    try:
        db_q = _weights_db()
        # A: sqlite pending
        oldest_sqlite = db_q.execute(
            "SELECT MIN(upload_time) FROM syb_weights "
            "WHERE pushed_to_d1 = 0 AND weight_g > 0 AND give_up_at = 0"
        ).fetchone()
        if oldest_sqlite and oldest_sqlite[0]:
            try:
                d_str = str(oldest_sqlite[0]).split(" ")[0]
                candidates.append(datetime.strptime(d_str, "%Y-%m-%d"))
            except Exception:
                pass
        # B: D1 業績有但 sqlite 完全沒記錄(沒抓到 weight,也沒 give_up)
        sqlite_known = {row[0] for row in db_q.execute(
            "SELECT order_code FROM syb_weights WHERE weight_g > 0 OR give_up_at > 0"
        ).fetchall()}
        db_q.close()
        need_sync = relevant_codes - sqlite_known
        if need_sync:
            ship_dates = [oc_to_ship_date.get(c) for c in need_sync]
            ship_dates = [d for d in ship_dates if d]
            if ship_dates:
                try:
                    candidates.append(datetime.strptime(min(ship_dates), "%Y-%m-%d"))
                except Exception:
                    pass
            log(f"[SYB-W] D1 業績有 {len(need_sync)} 筆 sqlite 還沒對應記錄(需要 sync)")
    except Exception:
        pass

    # 短路:既無 sqlite pending 也無 D1 need_sync → 全部已處理完,不打 SYB API
    if not candidates:
        log("[SYB-W] 業績訂單全部已 sync(D1 全推 + sqlite 無 pending),跳過 SYB API")
        return 0

    earliest = min(candidates)
    days = max(days_default, (datetime.now() - earliest).days + 7)

    today = datetime.now().strftime("%Y-%m-%d")
    from_d = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    log(f"[SYB-W] 同步範圍 {from_d} ~ {today}({days} 天,動態)")

    try:
        stoken = ensure_stoken(log=log)
    except Exception as e:
        log(f"[SYB-W] 取 stoken 失敗:{e}")
        return -1
    if not stoken:
        log("[SYB-W] 無 stoken,跳過")
        return -1

    page_size = 500
    all_rows: List[dict] = []
    page = 1
    while True:
        body = {
            "history": 0,
            "length": page_size,
            "start": (page - 1) * page_size,
            "pageTotal": 0,
            "pageIndex": page,
            "columns": [
                {"tableName":"t_stock","colName":"code",          "fieldName":"code",         "hasAlias":0,"tableAlias":"t"},
                {"tableName":"t_stock","colName":"status",        "fieldName":"status",       "hasAlias":0,"tableAlias":"t"},
                {"tableName":"t_stock","colName":"weight_consign","fieldName":"weightConsign","hasAlias":0,"tableAlias":"t"},
                {"tableName":"t_stock","colName":"upload_time",   "fieldName":"uploadTime",   "hasAlias":0,"tableAlias":"t"},
            ],
            "queries": [
                {"tableName":"t_stock","colName":"created","dvalue":f"{from_d},{today}","op":0,"type":3,"tableAlias":"t","optType":0},
                # 不過濾 status:過磅時機(已有 weight_consign)早於「已發貨」(status=50),
                # 只要 weight_consign>0 就應該抓,本地會 filter 掉 weight=0 的
            ],
        }
        try:
            data = _syb_post(stoken, "/am/stock/list", body, log=lambda *_: None)
        except SYBAuthError as e:
            # v6.0.62: cache 内 stoken 被 server 拒 → 清 cache → 重新 auto_login(AI 验证码) → 重试当前 page
            try: _TOKEN_CACHE.unlink(missing_ok=True)
            except Exception: pass
            log(f"[SYB-W] page {page} stoken 被服务器拒,清缓存重新登录...")
            try:
                stoken = ensure_stoken(log=log)  # 重新走 auto_login
            except Exception as _e2:
                log(f"[SYB-W] 重新登录失败:{_e2},终止 sync")
                break
            continue  # 重试当前 page(不递增 page)
        except Exception as e:
            log(f"[SYB-W] /am/stock/list page {page} 失敗:{e}")
            break
        rows = (data.get("data") or {}).get("list") or []
        if not rows:
            break
        all_rows.extend(rows)
        total = (data.get("data") or {}).get("total", 0)
        if len(all_rows) >= total or len(rows) < page_size:
            break
        page += 1
        if page > 20:  # 安全閥,最多抓 10000 條
            log(f"[SYB-W] ⚠️ 抓到 page {page} 仍未結束(已 {len(all_rows)} 條),強制停 — 可能有訂單漏抓!"
                f"建議檢查孤兒訂單並縮小範圍。")
            break

    n_upsert = 0
    n_skip_done = 0       # 已處理完成(pushed_to_d1>0 且 weight 沒變)
    n_skip_irrelevant = 0  # SYB 訂單但不在業績 D1 清單(別來源/別人的)→ 不入 sqlite
    try:
        # v6.0.68 ★:SYB 撈回的 code 可能帶 +N 後綴(避免重綁 SYB 衝突),
        # 要 strip 後才能對應 D1 純 Yahoo 號的 relevant_codes
        try:
            from .syb_http_ops import strip_dup_suffix
        except Exception:
            strip_dup_suffix = lambda x: x  # noqa: E731

        db = _weights_db()
        cur = db.cursor()
        ts = time.time()
        for r in all_rows:
            raw_code = str(r.get("code", "")).strip()
            # 砍 +N 後綴拿純 Yahoo 號
            code = strip_dup_suffix(raw_code)
            w = int(r.get("weightConsign") or 0)
            if not code or w <= 0:
                continue
            # 不在業績清單 → 跳過,完全不入 sqlite(避免產生孤兒)
            if code not in relevant_codes:
                n_skip_irrelevant += 1
                continue
            # 先看這筆訂單在 sqlite 中的狀態
            existing = cur.execute(
                "SELECT weight_g, pushed_to_d1 FROM syb_weights WHERE order_code = ?", (code,)
            ).fetchone()
            if existing and existing[1] and existing[1] > 0 and existing[0] == w:
                # 已推 D1 且 weight 沒變 → 完全跳過,連 sqlite 都不動
                n_skip_done += 1
                continue
            # 沒處理過 / 沒推 / weight 變了 → upsert(weight 變會 reset pushed=0 觸發重推)
            cur.execute(
                "INSERT INTO syb_weights (order_code, weight_g, status, upload_time, synced_at, pushed_to_d1) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(order_code) DO UPDATE SET "
                "  weight_g = excluded.weight_g, "
                "  status = excluded.status, "
                "  upload_time = excluded.upload_time, "
                "  synced_at = excluded.synced_at, "
                "  pushed_to_d1 = CASE WHEN syb_weights.weight_g != excluded.weight_g THEN 0 ELSE syb_weights.pushed_to_d1 END",
                (code, w, r.get("status"), r.get("uploadTime") or "", ts),
            )
            n_upsert += 1
        db.commit()
        db.close()
    except Exception as e:
        log(f"[SYB-W] sqlite upsert 失敗:{e}")
        return -1

    log(f"[SYB-W] 同步完成:抓 {len(all_rows)} 條,跳過已處理 {n_skip_done} 條,"
        f"非業績訂單(其他來源)忽略 {n_skip_irrelevant} 條,新處理 {n_upsert} 條")
    return n_upsert


def apply_weights_to_rows(rows: List[dict]) -> int:
    """對 D1 query 回的 rows in-place 修正 qty/freight/profit(用 SYB 重量覆蓋)。

    回傳實際被改了 qty 的行數。
    """
    if not rows:
        return 0
    n_changed = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        order_code = str(r.get("order_code") or "").strip()
        real_qty = lookup_real_qty(order_code)
        if real_qty is None:
            continue
        try:
            old_qty = int(float(r.get("qty") or 0))
        except Exception:
            old_qty = 0
        if real_qty == old_qty:
            continue
        r["qty"] = real_qty
        r["freight"] = real_qty * FREIGHT_PER_QTY
        # 重算 profit(若 D1 已給齊各分項)
        try:
            total_cny = float(r.get("total_cny") or 0)
            cost = float(r.get("cost") or 0)
            pack = float(r.get("pack_cost") or PACK_COST_DEFAULT)
            deliv = float(r.get("delivery") or DELIVERY_DEFAULT)
            r["profit"] = total_cny - cost - pack - r["freight"] - deliv
        except Exception:
            pass
        n_changed += 1
    return n_changed


def push_weights_to_d1(log: LogFn = None) -> int:
    """把 sqlite 中 pushed_to_d1=0 的真實貨物數推回 D1(走 admin_replace,主管專屬)。

    流程:
    1. 從 sqlite 拿 pushed_to_d1=0 AND weight_g>0 的 order_codes
    2. 從 D1 query_records 對應記錄(按最早 upload_time-7天 為起始)
    3. 構造 PerfRecord,qty=ceil(weight_g/1000),重算 freight/profit
    4. admin_replace_records 推 D1
    5. 推成功的 update pushed_to_d1=now()

    回傳成功 push 條數。非主管直接返 0。
    """
    if log is None:
        log = lambda *_: None

    chat_id = _get_chat_id()
    if chat_id != SUPERVISOR_CHAT_ID:
        log("[SYB-W] push D1 跳過:非主管權限")
        return 0

    # 1. 拿 sqlite 待推(排除已放棄的孤兒)
    try:
        db = _weights_db()
        pending = db.execute(
            "SELECT order_code, weight_g, upload_time FROM syb_weights "
            "WHERE pushed_to_d1 = 0 AND weight_g > 0 AND give_up_at = 0"
        ).fetchall()
        db.close()
    except Exception as e:
        log(f"[SYB-W] 讀 sqlite 待推失敗:{e}")
        return 0

    if not pending:
        log("[SYB-W] D1 無待推記錄,全部已推過")
        return 0

    pending_codes = {row[0] for row in pending}
    weight_map = {row[0]: row[1] for row in pending}
    upload_time_map = {row[0]: row[2] for row in pending}

    # 2. 推算 D1 query 起始日(最早 upload_time - 7 天緩衝)
    upload_dates = [row[2] for row in pending if row[2]]
    if upload_dates:
        try:
            min_d = min(d.split(" ")[0] for d in upload_dates if d)
            min_dt = datetime.strptime(min_d, "%Y-%m-%d") - timedelta(days=7)
            from_date = min_dt.strftime("%Y-%m-%d")
        except Exception:
            from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    else:
        from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    log(f"[SYB-W] 從 D1 拉 {from_date} ~ {today} 全員記錄,匹配 {len(pending_codes)} 個 order_code...")

    try:
        rows = query_records(owner="", from_date=from_date, to_date=today, limit=10000)
    except Exception as e:
        log(f"[SYB-W] query_records 失敗:{e}")
        return 0

    if rows and len(rows) >= 10000:
        log(f"[SYB-W] ⚠️ D1 query 達 limit=10000(可能截斷),部分匹配可能漏掉。"
            f"建議縮小 from_date 範圍或主管手動分批跑「修正同步」。")

    if not rows:
        log("[SYB-W] D1 無記錄,可能業績還沒上傳")
        # 不直接 return — 後續還要處理 60 天放棄邏輯
        rows = []

    # 3. 構造 PerfRecord,改 qty 並重算
    perf_recs: List[PerfRecord] = []
    matched_no_change: List[str] = []  # qty 已是真值的(不用推但要標記)
    for r in rows:
        oc = str(r.get("order_code") or "").strip()  # 防尾隨空格,跟 sync 那邊對齊
        if oc not in pending_codes:
            continue
        w = weight_map.get(oc, 0)
        if w <= 0:
            continue
        new_qty = max(1, math.ceil(w / 1000.0))
        try:
            old_qty = int(float(r.get("qty") or 0))
        except Exception:
            old_qty = 0
        if new_qty == old_qty:
            matched_no_change.append(oc)
            continue
        try:
            rec = PerfRecord(
                code=str(r.get("code") or ""),
                order_code=oc,
                account=str(r.get("account") or ""),
                ship_date=str(r.get("ship_date") or ""),
                pay_date=str(r.get("pay_date") or ""),
                pay_dates_raw=str(r.get("pay_dates_raw") or ""),
                owner=str(r.get("owner") or ""),
                customer=str(r.get("customer") or ""),
                total_twd_raw=str(r.get("total_twd_raw") or ""),
                total_twd=float(r.get("total_twd") or 0),
                qty=new_qty,
                cost_raw=str(r.get("cost_raw") or ""),
                cost=float(r.get("cost") or 0),
                pack_cost=float(r.get("pack_cost") or PACK_COST_DEFAULT),
                delivery=float(r.get("delivery") or DELIVERY_DEFAULT),
                note=str(r.get("note") or ""),
                sheet_type=str(r.get("sheet_type") or ""),
                source_file=str(r.get("source_file") or ""),
            )
            _calc_perf_fields(rec)  # 重算 total_cny/freight/profit/profit_ratio
            perf_recs.append(rec)
        except Exception as e:
            log(f"[SYB-W] 構造 PerfRecord 失敗 {oc}:{e}")

    # 4. admin_replace per-batch(BATCH=100,跟 admin_replace_records 內部對齊),
    #    精準標記成功的 batch — 避免某批失敗仍把整批當成功
    BATCH = 100
    n_pushed = 0
    success_codes: List[str] = []
    if perf_recs:
        log(f"[SYB-W] admin_replace push {len(perf_recs)} 條 → D1(分 {(len(perf_recs)+BATCH-1)//BATCH} 批)...")
        for i in range(0, len(perf_recs), BATCH):
            chunk = perf_recs[i:i + BATCH]
            try:
                n = admin_replace_records(chunk, reason="syb_weight_sync", log=log)
            except Exception as e:
                log(f"[SYB-W] admin_replace batch {i//BATCH+1} 異常:{e}")
                n = 0
            if n > 0:
                # 此 batch 成功 → 精準標記 chunk 內全部
                success_codes.extend([r.order_code for r in chunk])
                n_pushed += n
            else:
                log(f"[SYB-W] admin_replace batch {i//BATCH+1} 失敗,{len(chunk)} 條保留待重推")

    success_codes.extend(matched_no_change)

    if success_codes:
        try:
            db = _weights_db()
            ts = time.time()
            db.executemany(
                "UPDATE syb_weights SET pushed_to_d1 = ? WHERE order_code = ?",
                [(ts, c) for c in success_codes],
            )
            db.commit()
            db.close()
        except Exception as e:
            log(f"[SYB-W] 標記 pushed_to_d1 失敗:{e}")

    # 5. 60 天放棄孤兒:upload_time 距今 > SYB_ORPHAN_GIVE_UP_DAYS 仍 D1 沒對應業績 → 放棄
    matched_in_d1 = {r.order_code for r in perf_recs} | set(matched_no_change)
    unmatched = pending_codes - matched_in_d1
    if unmatched:
        cutoff_str = (datetime.now() - timedelta(days=SYB_ORPHAN_GIVE_UP_DAYS)).strftime("%Y-%m-%d")
        give_up_codes: List[str] = []
        for code in unmatched:
            ut = upload_time_map.get(code, "")
            if not ut:
                continue
            d_part = str(ut).split(" ")[0]
            if d_part < cutoff_str:  # YYYY-MM-DD lex 比較等同日期比較
                give_up_codes.append(code)
        if give_up_codes:
            try:
                db = _weights_db()
                ts2 = time.time()
                db.executemany(
                    "UPDATE syb_weights SET give_up_at = ? WHERE order_code = ?",
                    [(ts2, c) for c in give_up_codes],
                )
                db.commit()
                db.close()
                log(f"[SYB-W] {len(give_up_codes)} 條孤兒(SYB 有重量但 D1 業績超過 "
                    f"{SYB_ORPHAN_GIVE_UP_DAYS} 天仍未上傳)→ 標記放棄,不再嘗試也不再拉低 days 範圍")
            except Exception as e:
                log(f"[SYB-W] 標記 give_up_at 失敗:{e}")

    log(f"[SYB-W] D1 push 完成:admin_replace {n_pushed} 條,"
        f"已是真值 {len(matched_no_change)} 條,孤兒待匹配 {len(unmatched)} 條")
    return n_pushed


# ── 数据结构 ───────────────────────────────────────────────
@dataclass
class PerfRecord:
    code: str = ""
    order_code: str = ""
    account: str = ""
    ship_date: str = ""
    pay_date: str = ""        # 出货资料主行的「代付日期」（兼容旧逻辑，第 1 个 shipment 的）
    pay_dates_raw: str = ""   # 所有 shipment 的代付日期（"YYYY-MM-DD+YYYY-MM-DD"），跟 cost_raw 按 + 顺序对齐
    owner: str = ""
    customer: str = ""
    total_twd_raw: str = ""
    total_twd: float = 0.0
    total_cny: float = 0.0
    qty: int = 0
    pack_cost: float = PACK_COST_DEFAULT
    freight: float = 0.0
    delivery: float = DELIVERY_DEFAULT
    cost: float = 0.0
    cost_raw: str = ""
    profit: float = 0.0
    profit_ratio: float = 0.0
    note: str = ""
    sheet_type: str = ""
    source_file: str = ""

    @property
    def pk(self) -> str:
        return f"{self.code}|{self.order_code}"

    def to_dict(self) -> dict:
        return {
            "code": self.code, "order_code": self.order_code, "account": self.account,
            "ship_date": self.ship_date, "pay_date": self.pay_date,
            "pay_dates_raw": self.pay_dates_raw,
            "owner": self.owner, "customer": self.customer,
            "total_twd_raw": self.total_twd_raw, "total_twd": self.total_twd,
            "total_cny": self.total_cny, "qty": self.qty,
            "pack_cost": self.pack_cost, "freight": self.freight, "delivery": self.delivery,
            "cost": self.cost, "cost_raw": self.cost_raw,
            "profit": self.profit, "profit_ratio": self.profit_ratio,
            "note": self.note, "sheet_type": self.sheet_type, "source_file": self.source_file,
        }


# ── Excel 解析 ───────────────────────────────────────────
def _eval_simple_formula(s: str) -> float:
    """求值字段：兼容 4 种形式
    - 纯数字: 7623 / 7623.5 / 7,623
    - Excel 公式: =12000-12000 / =210+240+279.44
    - 文本算式（无 = 号）: 20000+20000 / 30000+30000+30000+3000+...
    - 含字母（单元格引用如 G2）: 跳过返回 0

    用户在 Excel 里直接写「20000+20000」（成本明细），我们解析时要算出 40000。
    """
    if s is None or s == "":
        return 0.0
    txt = str(s).strip()
    if not txt:
        return 0.0
    # 1. 直接数字
    try:
        return float(txt.replace(",", ""))
    except Exception:
        pass
    # 2. 去掉 = 号 + 逗号
    expr = txt
    if expr.startswith("="):
        expr = expr[1:]
    expr = expr.replace(",", "").strip()
    if not expr:
        return 0.0
    # 3. 含字母 → 单元格引用，跳过
    if re.search(r"[a-zA-Z]", expr):
        return 0.0
    # 4. 纯数字 + 运算符 → 安全 eval
    if re.fullmatch(r"[\d\+\-\*\/\.\(\)\s]+", expr):
        try:
            return float(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return 0.0
    return 0.0


def _to_date_str(v: Any) -> str:
    """各种日期格式 → YYYY-MM-DD"""
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    # 已是 YYYY/MM/DD 或 YYYY-MM-DD
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 中文格式：2026年4月2日
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def _fmt_ship_date_short(s: str) -> str:
    """YYYY-MM-DD → '4月23日'（与 order_export.py 的 m"月"d"日" 显示格式一致）"""
    if not s:
        return ""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return s
    return f"{int(m.group(2))}月{int(m.group(3))}日"


def _count_split(s: str) -> int:
    """数 cost_raw/pay_dates_raw 用 + 拆分后有多少个有效段（跳过空串/含字母的）"""
    if not s:
        return 0
    parts = [x.strip() for x in str(s).split("+") if x.strip()]
    # 成本字段可能含单元格引用如 "G2"，这种不算有效金额段
    return sum(1 for p in parts if not re.search(r"[a-zA-Z]", p))


def parse_shipping_excel(xlsx_path: Path, log: LogFn = None) -> List[PerfRecord]:
    """解析一个 出货资料_YYYYMMDD.xlsx，返回 PerfRecord 列表。

    线上贴单资料 列: 編碼/賬號/日期/系统編碼/所屬人/.../金额/代付日期/商品成本/商品數量/.../备注
    宅配打包资料 列: 編碼/賬號/日期/訂單編碼/所屬人/.../訂單金額/代付日期/商品成本/商品數量/備注

    多 shipment 订单的写入约定（来自 order_export.py）：
      第 1 行: 编码=A, 商品成本="100+200", 代付日期=4-05  ← 主行
      第 2 行: 编码=空,                  代付日期=4-10  ← 后续行（只填代付日期）
    所以解析时要把后续无编码行的「代付日期」追加到主行的 pay_dates_raw（用 + 分隔），
    用于核销时跟 cost_raw 拆出的金额按顺序 1对1 配对。

    解析完成后校验 cost 笔数 == pay_dates 笔数，不等时通过 log 打警告
    （不影响上传，但核销这单可能对不齐，需要人工检查）。
    """
    records: List[PerfRecord] = []
    src = xlsx_path.name

    try:
        wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=False)
    except Exception:
        return records

    def _parse_sheet(sheet_name: str, builder):
        if sheet_name not in wb.sheetnames:
            return
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return
        headers = [str(h or "").strip() for h in rows[0]]
        idx = {h: i for i, h in enumerate(headers) if h}
        last_main: Optional[PerfRecord] = None
        for r in rows[1:]:
            if not r or not any(r):
                last_main = None
                continue
            rec = builder(r, idx, src)
            if rec and rec.code and rec.order_code:
                # 主行：pay_dates_raw 初始化为 pay_date
                if rec.pay_date:
                    rec.pay_dates_raw = rec.pay_date
                records.append(rec)
                last_main = rec
            elif last_main is not None:
                # 后续无编码行 — 把这一行的代付日期追加到上一个主行
                # v6.0.52 加固:白名單 — 只接受「僅代付日期非空,其餘關鍵欄皆空」的行
                # 防 footer/合計行(編碼空但其他 cell 含「合計」「小計」)被誤當後續行污染 pay_dates_raw
                pd = _to_date_str(_get(r, idx, "代付日期"))
                # 關鍵欄(賬號/系統編碼/訂單編碼/商品名稱/金額/商品成本)應全空才算後續行
                _key_fields = ["賬號", "系统編碼", "訂單編碼", "订单编码",
                                "商品名稱", "商品名称", "金额", "金額",
                                "訂單金額", "订单金额", "商品成本"]
                _has_key_data = any(
                    str(_get(r, idx, k) or "").strip() for k in _key_fields
                )
                if pd and not _has_key_data:
                    if last_main.pay_dates_raw:
                        last_main.pay_dates_raw += "+" + pd
                    else:
                        last_main.pay_dates_raw = pd
                # 否則:可能是 footer/合計/錯誤行,跳過(last_main 保留,讓後面 row 還能 append)

    _parse_sheet(SHEET_ONLINE, _build_record_online)
    _parse_sheet(SHEET_HOME, _build_record_home)

    wb.close()

    # 校验 cost / pay_dates 笔数对齐（多 shipment 订单才会多段，单段的直接跳过）
    if log:
        for rec in records:
            cc = _count_split(rec.cost_raw)
            dc = _count_split(rec.pay_dates_raw)
            if cc > 1 or dc > 1:
                if cc != dc:
                    log(f"[业绩] ⚠ {src} {rec.code}/{rec.order_code}: "
                        f"成本 {cc} 笔 ≠ 代付日期 {dc} 笔 "
                        f"(cost_raw={rec.cost_raw!r}, pay_dates_raw={rec.pay_dates_raw!r}) "
                        f"— 此单核销可能对不齐，请人工检查采购出货表")
    return records


def _get(row: tuple, idx: Dict[str, int], key: str, default="") -> Any:
    i = idx.get(key, -1)
    if i < 0 or i >= len(row):
        return default
    v = row[i]
    return default if v is None else v


def _calc_perf_fields(rec: PerfRecord) -> None:
    """根据原始数据计算业绩字段（与现有业绩表公式一致）"""
    rec.total_cny = (rec.total_twd / TWD_TO_CNY_RATE * CNY_PROFIT_FACTOR) if TWD_TO_CNY_RATE else 0
    rec.freight = rec.qty * FREIGHT_PER_QTY
    rec.profit = rec.total_cny - rec.cost - rec.pack_cost - rec.freight - rec.delivery
    rec.profit_ratio = (rec.total_twd / rec.cost) if rec.cost else 0.0


def _build_record_online(row: tuple, idx: Dict[str, int], src: str) -> Optional[PerfRecord]:
    """解析【线上贴单资料】一行"""
    code = str(_get(row, idx, "編碼") or "").strip()
    order = str(_get(row, idx, "系统編碼") or "").strip()
    if not code or not order:
        return None
    qty_raw = _get(row, idx, "商品數量", 1)
    try:
        qty = int(float(str(qty_raw).replace(",", ""))) if qty_raw else 0
    except Exception:
        qty = 0
    # SYB 真實貨物數覆蓋:有重量就用 ceil(kg),沒就用 Excel 商品數量
    _real_qty = lookup_real_qty(order)
    if _real_qty is not None:
        qty = _real_qty
    cost_raw = str(_get(row, idx, "商品成本") or "")
    twd_raw = str(_get(row, idx, "金额") or "")
    rec = PerfRecord(
        code=code,
        order_code=order,
        account=str(_get(row, idx, "賬號") or ""),
        ship_date=_to_date_str(_get(row, idx, "日期")),
        pay_date=_to_date_str(_get(row, idx, "代付日期")),
        owner=str(_get(row, idx, "所屬人") or "").strip(),
        customer=str(_get(row, idx, "收件人") or ""),
        total_twd_raw=twd_raw,
        total_twd=_eval_simple_formula(twd_raw) or (float(twd_raw) if str(twd_raw).replace(".","").replace("-","").isdigit() else 0),
        qty=qty,
        cost_raw=cost_raw,
        cost=_eval_simple_formula(cost_raw),
        note="",  # 业绩备注留空给用户对账时手填，不导入出货资料的备注
        sheet_type="online",
        source_file=src,
    )
    _calc_perf_fields(rec)
    return rec


def _build_record_home(row: tuple, idx: Dict[str, int], src: str) -> Optional[PerfRecord]:
    """解析【宅配打包资料】一行"""
    code = str(_get(row, idx, "編碼") or "").strip()
    order = str(_get(row, idx, "訂單編碼") or "").strip()
    if not code or not order:
        return None
    qty_raw = _get(row, idx, "商品數量", 1)
    try:
        qty = int(float(str(qty_raw).replace(",", ""))) if qty_raw else 0
    except Exception:
        qty = 0
    # SYB 真實貨物數覆蓋:有重量就用 ceil(kg),沒就用 Excel 商品數量
    _real_qty = lookup_real_qty(order)
    if _real_qty is not None:
        qty = _real_qty
    cost_raw = str(_get(row, idx, "商品成本") or "")
    twd_raw = str(_get(row, idx, "訂單金額") or "")
    rec = PerfRecord(
        code=code,
        order_code=order,
        account=str(_get(row, idx, "賬號") or ""),
        ship_date=_to_date_str(_get(row, idx, "日期")),
        pay_date=_to_date_str(_get(row, idx, "代付日期")),
        owner=str(_get(row, idx, "所屬人") or "").strip(),
        customer=str(_get(row, idx, "收件人") or ""),
        total_twd_raw=twd_raw,
        total_twd=_eval_simple_formula(twd_raw) or (float(twd_raw) if str(twd_raw).replace(".","").replace("-","").isdigit() else 0),
        qty=qty,
        cost_raw=cost_raw,
        cost=_eval_simple_formula(cost_raw),
        note="",  # 业绩备注留空给用户对账时手填
        sheet_type="home_delivery",
        source_file=src,
    )
    if not rec.total_twd:
        try:
            rec.total_twd = float(str(twd_raw).replace(",", "").replace("=", ""))
        except Exception:
            rec.total_twd = 0
    _calc_perf_fields(rec)
    return rec


# ── 同步状态（本地记录已上传过哪些文件 mtime）────────────
def _state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "performance_sync_state.json"


# 失败重传队列：出货后自动同步失败时写入文件路径，下次启动软件时重试
def _retry_queue_path() -> Path:
    return Path(__file__).resolve().parent.parent / "performance_retry_queue.json"


def _load_retry_queue() -> List[str]:
    try:
        p = _retry_queue_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_retry_queue(paths: List[str]) -> None:
    try:
        _retry_queue_path().write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def enqueue_failed_upload(xlsx_path: str) -> None:
    """把上传失败的文件路径加入重试队列（去重）"""
    q = _load_retry_queue()
    p = str(xlsx_path)
    if p not in q:
        q.append(p)
        _save_retry_queue(q)


def retry_failed_uploads(log: LogFn = None) -> Tuple[int, int]:
    """重试失败队列里所有文件，返回 (成功条数, 失败文件数)"""
    q = _load_retry_queue()
    if not q:
        return 0, 0
    still_failed: List[str] = []
    total_ok = 0
    for p in q:
        try:
            fp = Path(p)
            if not fp.exists():
                continue  # 文件被删了，静默跳过
            recs = parse_shipping_excel(fp, log=log)
            if not recs:
                continue  # 空文件也跳过
            ins, skp, rej = upload_records(recs, log=log)
            if ins > 0 or skp > 0:
                total_ok += ins
                if log: log(f"[业绩] 重传 {fp.name} 成功：新增 {ins}，已存在 {skp}")
            else:
                still_failed.append(p)
        except Exception as e:
            still_failed.append(p)
            if log: log(f"[业绩] 重传 {p} 失败：{e}")
    _save_retry_queue(still_failed)
    return total_ok, len(still_failed)


def _load_sync_state() -> dict:
    try:
        p = _state_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"files": {}, "last_full_sync": 0}


def _save_sync_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── HTTP 客户端 ──────────────────────────────────────────
def _get_chat_id() -> str:
    """从 settings 读 TG chat_id"""
    try:
        from core.accounts import load_settings
        s = load_settings() or {}
        return str(s.get("tg_chat_id") or "").strip()
    except Exception:
        return ""


def _get_my_owner() -> str:
    """从 settings 读 paystatus_name = 业绩所属人"""
    try:
        from core.accounts import load_settings
        s = load_settings() or {}
        return str(s.get("paystatus_name") or "").strip()
    except Exception:
        return ""


def _get_ship_owner() -> str:
    """从 settings 读 ship_owner = 採購出貨所屬人(對齊 Excel 內容,可能跟 paystatus_name 不同)。
    若沒設則 fallback 到 paystatus_name。"""
    try:
        from core.accounts import load_settings
        s = load_settings() or {}
        v = str(s.get("ship_owner") or "").strip()
        if v:
            return v
        return str(s.get("paystatus_name") or "").strip()
    except Exception:
        return ""


class UploadError(Exception):
    """upload_records 真实失败（网络/Worker 异常）抛出，便于调用方决定是否入重试队列。
    注意：owner 不匹配导致的 rejected 不算失败 — 它是服务端拒收，重试也没用。
    """


def upload_records(records: List[PerfRecord], log: LogFn = None,
                   raise_on_failure: bool = False) -> Tuple[int, int, int]:
    """批量上传，返回 (inserted, skipped, rejected)。

    raise_on_failure=True 时若任一批次失败（网络/Worker error）抛 UploadError，
    让调用方把文件加入重试队列。
    """
    if not records:
        return 0, 0, 0
    chat_id = _get_chat_id()
    owner = _get_my_owner()
    if not chat_id:
        if log: log("[业绩] 缺少 TG chat_id，无法上传")
        if raise_on_failure:
            raise UploadError("missing chat_id")
        return 0, 0, 0
    body = {
        "records": [r.to_dict() for r in records],
        "chat_id": chat_id,
        "owner_check": owner,
    }
    ins, skp, rej = 0, 0, 0
    any_failed = False
    BATCH = 200
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        body["records"] = [r.to_dict() for r in chunk]
        try:
            r = _req.post(f"{WORKER_URL}/api/performance/upload",
                          json=body, timeout=60)
            d = r.json()
            if not d.get("ok"):
                any_failed = True
                if log: log(f"[业绩] 上传失败：{d.get('error', '')}")
                continue
            ins += d.get("inserted", 0)
            skp += d.get("skipped", 0)
            rej += d.get("rejected", 0)
        except Exception as e:
            any_failed = True
            if log: log(f"[业绩] 上传异常：{e}")
    if any_failed and raise_on_failure:
        raise UploadError(f"partial failure: {ins=} {skp=} {rej=}")
    return ins, skp, rej


# ───────────────────────────────────────────────────────────────────
# v6.0.51: 阿里雲 Excel 共享(各 user 自動上傳,主管下載)
# Server: <RELAY_IP_REDACTED>:18900,純 stdlib http.server
# 路徑:~/shipping_excels/{owner}/{filename}
# ───────────────────────────────────────────────────────────────────

# Retry queue 路徑(失敗 upload 持久化,下次開 GUI 自動補)
_ALIYUN_RETRY_QUEUE = Path(__file__).resolve().parent.parent / "output" / "shipping_aliyun_retry.json"


def _parse_month_from_filename(filename: str) -> str:
    """從 出货资料_20260430.xlsx 解出 2026-04(用於 server month 路徑)"""
    import re as _re
    m = _re.search(r'(\d{4})(\d{2})(\d{2})', filename or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _retry_queue_load() -> List[dict]:
    if not _ALIYUN_RETRY_QUEUE.exists():
        return []
    try:
        return json.loads(_ALIYUN_RETRY_QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _retry_queue_save(items: List[dict]) -> None:
    try:
        _ALIYUN_RETRY_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        _ALIYUN_RETRY_QUEUE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _retry_queue_add(xlsx_path: str, owner: str, error: str) -> None:
    items = _retry_queue_load()
    # 已在隊列就更新時間
    for it in items:
        if it.get("path") == xlsx_path:
            it["last_attempt"] = time.time()
            it["error"] = error[:200]
            it["attempts"] = int(it.get("attempts", 0)) + 1
            _retry_queue_save(items)
            return
    items.append({
        "path": xlsx_path, "owner": owner, "error": error[:200],
        "first_attempt": time.time(), "last_attempt": time.time(), "attempts": 1,
    })
    _retry_queue_save(items)


def _notify_supervisor_tg(text: str) -> None:
    """發 TG 給主管(supervisor_bot,chat_id <SUPERVISOR_CHAT_ID>)"""
    try:
        tokens_path = Path(__file__).resolve().parent.parent / "tg_tokens.json"
        if not tokens_path.exists():
            return
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        sv_token = tokens.get("supervisor_bot_token") or ""
        if not sv_token:
            return
        import requests as _rq
        _rq.post(
            f"https://api.telegram.org/bot{sv_token}/sendMessage",
            json={"chat_id": SUPERVISOR_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def upload_excel_to_aliyun(xlsx_path: Path, owner: str = "",
                            log: LogFn = None) -> dict:
    """上傳出貨資料 Excel 到阿里雲共享 server。

    v6.0.52 改進:
    - 帶 month query param(server 用月份子目錄)
    - 失敗時加進 retry queue + TG 通知主管
    - 回傳 {ok, owner, month, filename, size} 或 {ok=False, error}

    失敗不抛 — 不影響業績 D1 主流程,但 propagate 給 caller(可顯示紅字)。
    """
    if not log:
        log = lambda *_: None
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        return {"ok": False, "error": f"file not found: {xlsx_path}"}
    if not owner:
        owner = _get_my_owner() or "unknown"
    month = _parse_month_from_filename(xlsx_path.name)
    try:
        data = xlsx_path.read_bytes()
        params = {"owner": owner, "filename": xlsx_path.name}
        if month:
            params["month"] = month
        url = f"{SHIPPING_EXCEL_SERVER_URL}/upload"
        r = _req.post(
            url, params=params, data=data,
            headers={
                "X-API-Token": SHIPPING_EXCEL_TOKEN,
                "Content-Type": "application/octet-stream",
            },
            timeout=60,
        )
        d = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {"ok": False, "error": f"HTTP {r.status_code}"}
        if d.get("ok"):
            log(f"[阿里云] 出貨資料上傳成功:{owner}/{month}/{d.get('filename')} ({d.get('size')} bytes)")
            # 從 retry queue 移除(若有)
            items = _retry_queue_load()
            new_items = [it for it in items if it.get("path") != str(xlsx_path)]
            if len(new_items) != len(items):
                _retry_queue_save(new_items)
        else:
            err_msg = d.get("error", "未知")
            log(f"[阿里云] 出貨資料上傳失敗:{err_msg}")
            _retry_queue_add(str(xlsx_path), owner, err_msg)
            _notify_supervisor_tg(
                f"⚠️ 出貨資料上傳失敗\n"
                f"檔:{xlsx_path.name}\n"
                f"owner:{owner}\n"
                f"原因:{err_msg}\n"
                f"已加入 retry queue,下次 GUI 啟動自動重試"
            )
        return d
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        log(f"[阿里云] 出貨資料上傳異常:{err_msg}")
        _retry_queue_add(str(xlsx_path), owner, err_msg)
        _notify_supervisor_tg(
            f"⚠️ 出貨資料上傳異常\n"
            f"檔:{xlsx_path.name}\n"
            f"owner:{owner}\n"
            f"原因:{err_msg[:150]}"
        )
        return {"ok": False, "error": str(e)[:200]}


def retry_pending_uploads(log: LogFn = None) -> int:
    """掃 retry queue,重試失敗的上傳。GUI 啟動時呼叫。回傳成功補傳數量。"""
    if not log:
        log = lambda *_: None
    items = _retry_queue_load()
    if not items:
        return 0
    log(f"[阿里云] retry queue 有 {len(items)} 筆待補傳")
    success = 0
    for it in list(items):
        path = it.get("path", "")
        if not path or not Path(path).is_file():
            # 檔不在了,從 queue 移除
            items.remove(it)
            continue
        r = upload_excel_to_aliyun(Path(path), owner=it.get("owner", ""), log=log)
        if r.get("ok"):
            success += 1
    log(f"[阿里云] retry 結果:成功 {success}/{len(items)}")
    return success


def list_aliyun_excels(owner: str = "", month: str = "",
                        log: LogFn = None) -> List[dict]:
    """列阿里雲共享 server 上的 Excel(主管用)。

    owner 留空 = 列全部。month 格式 YYYY-MM。
    回 [{owner, month, filename, size, mtime, legacy?}, ...]
    """
    if not log:
        log = lambda *_: None
    try:
        params = {}
        if owner:
            params["owner"] = owner
        if month:
            params["month"] = month
        r = _req.get(
            f"{SHIPPING_EXCEL_SERVER_URL}/list", params=params,
            headers={"X-API-Token": SHIPPING_EXCEL_TOKEN},
            timeout=20,
        )
        d = r.json()
        if d.get("ok"):
            return d.get("files", [])
        log(f"[阿里云] list 失敗:{d.get('error', '未知')}")
        return []
    except Exception as e:
        log(f"[阿里云] list 異常:{e}")
        return []


def download_aliyun_excel(owner: str, filename: str,
                           save_path: Path, month: str = "",
                           log: LogFn = None) -> bool:
    """從阿里雲下載指定 Excel 到本地。

    month 留空 = 走 v1 legacy 平鋪路徑(server 自動 fallback)
    """
    if not log:
        log = lambda *_: None
    try:
        params = {"owner": owner, "filename": filename}
        if month:
            params["month"] = month
        r = _req.get(
            f"{SHIPPING_EXCEL_SERVER_URL}/download", params=params,
            headers={"X-API-Token": SHIPPING_EXCEL_TOKEN},
            timeout=120, stream=True,
        )
        if r.status_code != 200:
            log(f"[阿里云] download 失敗:HTTP {r.status_code}")
            return False
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        log(f"[阿里云] 下載成功:{owner}/{filename} → {save_path} ({save_path.stat().st_size} bytes)")
        return True
    except Exception as e:
        log(f"[阿里云] 下載異常:{e}")
        return False


# ───────────────────────────────────────────────────────────────────
# v6.0.52: 月匯總(主管專用)
# 從阿里雲拉該月所有 user 的 Excel,合併 4 sheet 到單一檔
# 對齊 user 過去手動習慣:E:\YYYY年M月出货资料.xlsx
# ───────────────────────────────────────────────────────────────────

# 4 sheet 名(對齊 user E:\歷史檔)
MERGE_SHEET_NAMES = ["线上贴单资料", "宅配打包资料", "线下建单资料", "退貨資料"]


def _filename_date(filename: str) -> str:
    """從 出货资料_20260430__upload_xxx.xlsx 解出 20260430(原始日期)"""
    import re as _re
    m = _re.search(r'_(\d{8})_', filename or "")
    if m:
        return m.group(1)
    m2 = _re.search(r'(\d{8})', filename or "")
    return m2.group(1) if m2 else ""


def _filename_upload_ts(filename: str) -> int:
    """從 ...__upload_HHMMSSfff_xxxx.xlsx 解出 timestamp 整數(用於同日多版本去重)
    若解析不到回 0(legacy 檔)
    """
    import re as _re
    m = _re.search(r'__upload_(\d+)', filename or "")
    return int(m.group(1)) if m else 0


def merge_monthly_shipping_excels(
    year_month: str,           # "2026-05"
    save_path: Path,           # E:\2026年5月出货资料.xlsx
    owner_filter: list = None, # 限定 owner;None=全部
    until_date: str = "",      # 截至某日(YYYYMMDD),"" = 整月
    log: LogFn = None,
    on_progress: Callable[[str, int, int], None] = None,
) -> dict:
    """主管月匯總:下載該月所有 user Excel + 合併 4 sheet。

    回傳 {ok, sheet_stats, source_files, dropped_versions, undelivered_owners, save_path, error?}
    """
    if not log:
        log = lambda *_: None
    if not on_progress:
        on_progress = lambda *_: None

    # 1) list 該月所有檔
    log(f"[月匯總] list 阿里雲 {year_month}...")
    all_files = list_aliyun_excels(month=year_month, log=log)
    # filter 含 legacy(month="")— legacy 只能猜 filename 是否屬該月
    by_owner: Dict[str, List[dict]] = {}
    for f in all_files:
        if owner_filter and f["owner"] not in owner_filter:
            continue
        # legacy 檔(月份空):看 filename 日期是否屬此月
        if not f.get("month"):
            fn_date = _filename_date(f["filename"])
            if not fn_date.startswith(year_month.replace("-", "")[:6]):
                continue
        by_owner.setdefault(f["owner"], []).append(f)

    on_progress("已列出檔案清單", 0, sum(len(v) for v in by_owner.values()))

    # 2) 同 user 同源日期取最新 timestamp 整檔(整檔級 dedupe,不拆 row)
    selected: List[dict] = []
    dropped: List[str] = []  # 被廢棄的版本說明

    for owner, files in by_owner.items():
        by_date: Dict[str, List[dict]] = {}
        for f in files:
            fd = _filename_date(f["filename"]) or "unknown"
            # until_date 過濾
            if until_date and fd > until_date:
                continue
            by_date.setdefault(fd, []).append(f)
        for fd, versions in by_date.items():
            versions.sort(key=lambda x: -_filename_upload_ts(x["filename"]))
            latest = versions[0]
            selected.append(latest)
            for v in versions[1:]:
                dropped.append(f"{owner} {fd}:廢棄 {v['filename']}")

    log(f"[月匯總] 選中 {len(selected)} 個來源檔(廢棄 {len(dropped)} 個舊版)")

    # 3) 並行下載所有選中檔到 temp
    import tempfile, threading
    from concurrent.futures import ThreadPoolExecutor

    tmp_dir = Path(tempfile.mkdtemp(prefix="merge_"))
    download_results: Dict[str, Path] = {}  # filename → tmp local path

    def _dl(f):
        local = tmp_dir / f"{f['owner']}__{f['filename']}"
        ok = download_aliyun_excel(
            f["owner"], f["filename"], local,
            month=f.get("month", ""), log=log,
        )
        return (f, local if ok else None)

    on_progress("開始並行下載...", 0, len(selected))
    completed = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f, local in ex.map(_dl, selected):
            completed += 1
            on_progress(f"下載中 {completed}/{len(selected)}", completed, len(selected))
            if local:
                download_results[f["filename"]] = local

    log(f"[月匯總] 下載完成 {len(download_results)}/{len(selected)}")

    # 4) 用 openpyxl 合併 4 sheet
    on_progress("合併 4 sheet 中...", 0, 0)
    merged_wb = openpyxl.Workbook()
    # 移除預設的 Sheet
    if "Sheet" in merged_wb.sheetnames:
        merged_wb.remove(merged_wb["Sheet"])
    sheet_stats: Dict[str, int] = {sn: 0 for sn in MERGE_SHEET_NAMES}
    sheet_seen_keys: Dict[str, set] = {sn: set() for sn in MERGE_SHEET_NAMES}

    for sheet_name in MERGE_SHEET_NAMES:
        merged_ws = merged_wb.create_sheet(title=sheet_name)
        first_headers_written = False
        # 走每個源檔
        for src_local in download_results.values():
            try:
                src_wb = openpyxl.load_workbook(str(src_local), read_only=True, data_only=True)
                if sheet_name not in src_wb.sheetnames:
                    src_wb.close()
                    continue
                src_ws = src_wb[sheet_name]
                rows = list(src_ws.iter_rows(values_only=True))
                src_wb.close()
                if not rows:
                    continue
                # 第一個源檔寫 headers
                if not first_headers_written:
                    merged_ws.append(rows[0])
                    first_headers_written = True
                # 取編碼欄 idx(用於三元組 dedupe key:編碼+系統編碼/訂單編碼+所屬人)
                hdr = rows[0]
                idx_code = next((i for i, h in enumerate(hdr) if str(h or "").strip() in ("編碼",)), 0)
                idx_order = next((i for i, h in enumerate(hdr) if str(h or "").strip() in ("系统編碼", "訂單編碼", "订单编码")), -1)
                idx_owner = next((i for i, h in enumerate(hdr) if str(h or "").strip() in ("所屬人",)), -1)

                # append data rows;主行+後續行作為 block 處理
                last_main_key = None
                for r in rows[1:]:
                    if not r or not any(v not in (None, "") for v in r):
                        last_main_key = None
                        continue
                    code_val = str(r[idx_code] or "").strip() if idx_code < len(r) else ""
                    if code_val:
                        # 主行
                        order_val = str(r[idx_order] or "").strip() if 0 <= idx_order < len(r) else ""
                        owner_val = str(r[idx_owner] or "").strip() if 0 <= idx_owner < len(r) else ""
                        # 三元組 key:跨 user 同訂單(退貨重派)不誤殺
                        key = (owner_val, order_val or code_val, code_val)
                        if key in sheet_seen_keys[sheet_name]:
                            last_main_key = None  # dup,跳過 block
                            continue
                        sheet_seen_keys[sheet_name].add(key)
                        merged_ws.append(r)
                        sheet_stats[sheet_name] += 1
                        last_main_key = key
                    else:
                        # 後續行(編碼空)— 屬於 last_main 的 block,只在 last_main 沒 dup 時加
                        if last_main_key is not None:
                            merged_ws.append(r)
            except Exception as e:
                log(f"[月匯總] 讀檔 {src_local.name} 異常({type(e).__name__}):{e},跳過")

    # 5) 算未交 owner(該月完全沒檔的)
    all_owners = set()
    for f in all_files:
        all_owners.add(f["owner"])
    undelivered = sorted(all_owners - set(by_owner.keys()))

    # 6) 存檔(若已存在就 backup 並 _v2)
    save_path = Path(save_path)
    if save_path.exists():
        bak = save_path.with_suffix(f".bak_{int(time.time())}.xlsx")
        save_path.rename(bak)
        log(f"[月匯總] 既存 {save_path.name} 已備份成 {bak.name}")
    save_path.parent.mkdir(parents=True, exist_ok=True)
    merged_wb.save(str(save_path))
    merged_wb.close()

    # 7) 清 temp
    try:
        import shutil as _sh
        _sh.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    return {
        "ok": True,
        "save_path": str(save_path),
        "source_files": len(download_results),
        "selected_files": len(selected),
        "sheet_stats": sheet_stats,
        "dropped_versions": dropped,
        "undelivered_owners": undelivered,
    }


def query_records(*, owner: str = "", from_date: str = "", to_date: str = "",
                  account: str = "", limit: int = 1000,
                  raise_on_fail: bool = False) -> List[dict]:
    """查询业绩记录。chat_id 由内部带（员工只能查自己 owner）

    v6.0.80:加 retry(網路抖動 / worker cold start race)。
    raise_on_fail=True 時失敗拋異常(讓上層區分「沒資料」vs「查詢失敗」),
    默認 False 維持向下相容(失敗返回 [])。
    """
    chat_id = _get_chat_id()
    params = {"chat_id": chat_id, "limit": str(limit)}
    if owner: params["owner"] = owner
    if from_date: params["from"] = from_date
    if to_date: params["to"] = to_date
    if account: params["account"] = account

    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            r = _req.get(f"{WORKER_URL}/api/performance/query", params=params, timeout=30)
            d = r.json()
            if d.get("ok"):
                return d.get("rows", [])
            last_err = RuntimeError(str(d.get("error", "unknown api err")))
        except Exception as e:
            last_err = e
        if attempt == 0:
            import time as _t
            _t.sleep(0.5)
    # 兩次都失敗
    if raise_on_fail:
        raise RuntimeError(f"query_records 失敗(retry x2):{last_err}")
    return []


def query_summary(*, owner: str = "", from_date: str = "", to_date: str = "") -> List[dict]:
    chat_id = _get_chat_id()
    params = {"chat_id": chat_id}
    if owner: params["owner"] = owner
    if from_date: params["from"] = from_date
    if to_date: params["to"] = to_date
    try:
        r = _req.get(f"{WORKER_URL}/api/performance/summary", params=params, timeout=30)
        d = r.json()
        return d.get("rows", []) if d.get("ok") else []
    except Exception:
        return []


def admin_replace_records(records: List[PerfRecord], reason: str = "fix_parser_bug",
                           log: LogFn = None) -> int:
    """主管专属：强制覆盖 D1 已有记录（INSERT OR REPLACE，每条记审计）。
    用于修正 parser bug 期间的脏数据。
    """
    chat_id = _get_chat_id()
    if chat_id != SUPERVISOR_CHAT_ID:
        if log: log("[业绩] admin_replace 仅主管可用")
        return 0
    if not records:
        return 0
    body = {
        "chat_id": chat_id,
        "reason": reason,
        "records": [r.to_dict() for r in records],
    }
    BATCH = 100
    total = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        body["records"] = [r.to_dict() for r in chunk]
        try:
            r = _req.post(f"{WORKER_URL}/api/performance/admin_replace",
                          json=body, timeout=60)
            d = r.json()
            if d.get("ok"):
                total += d.get("replaced", 0)
            else:
                if log: log(f"[业绩] admin_replace 失败：{d.get('error', '')}")
        except Exception as e:
            if log: log(f"[业绩] admin_replace 异常：{e}")
    return total


def force_resync_all(shipping_dir: Path, log: LogFn = None) -> int:
    """主管专属：重新解析所有出货资料并强制覆盖 D1。修正 parser bug 用。"""
    if not shipping_dir.exists():
        if log: log(f"[业绩] 目录不存在：{shipping_dir}")
        return 0
    all_records: List[PerfRecord] = []
    for fp in sorted(shipping_dir.glob("出货资料_*.xlsx")):
        try:
            recs = parse_shipping_excel(fp, log=log)
            all_records.extend(recs)
        except Exception as e:
            if log: log(f"[业绩] {fp.name} 解析异常：{e}")
    if not all_records:
        return 0
    if log: log(f"[业绩] 共解析出 {len(all_records)} 条，强制覆盖 D1...")
    n = admin_replace_records(all_records, reason="parser_fix_2026-04-23", log=log)
    # 重置本地同步状态 → 后续 sync 不会跳过
    _save_sync_state({"files": {}, "last_full_sync": int(time.time())})
    if log: log(f"[业绩] 强制覆盖完成：{n} 条")
    return n


def update_note(pk: str, note: str) -> Tuple[bool, str]:
    """更新某条业绩的备注。主管或本人可改。"""
    chat_id = _get_chat_id()
    owner = _get_my_owner()
    if not chat_id:
        return False, "无 chat_id"
    try:
        r = _req.post(f"{WORKER_URL}/api/performance/update_note",
                      json={"pk": pk, "note": note,
                             "chat_id": chat_id, "owner_check": owner},
                      timeout=15)
        d = r.json()
        return bool(d.get("ok")), str(d.get("error", ""))
    except Exception as e:
        return False, str(e)


# ── 作废功能(用 note 前缀实现,不需要 Worker 端 schema 改动) ──────
# 三种状态:
#   none    → 普通业绩(无前缀)
#   pending → 员工提交了作废申请,等主管审核(前缀 [待审作废])
#   final   → 主管已最终作废(前缀 [作废])
VOID_PREFIX = "[作废]"
VOID_PENDING_PREFIX = "[待审作废]"


def get_void_status(note: str) -> str:
    """从 note 解出作废状态。返回 'none' / 'pending' / 'final'。"""
    s = (note or "").lstrip()
    if s.startswith(VOID_PREFIX):
        return "final"
    if s.startswith(VOID_PENDING_PREFIX):
        return "pending"
    return "none"


def is_voided_note(note: str) -> bool:
    """判断业绩是否已作废(final 或 pending 都算 — 业绩核对应跳过)。"""
    return get_void_status(note) != "none"


def is_void_final(note: str) -> bool:
    return get_void_status(note) == "final"


def is_void_pending(note: str) -> bool:
    return get_void_status(note) == "pending"


def strip_void_prefix(note: str) -> str:
    """去掉 note 的作废前缀(任一种),返回纯净 note。"""
    s = (note or "").lstrip()
    for p in (VOID_PREFIX, VOID_PENDING_PREFIX):
        if s.startswith(p):
            s = s[len(p):].lstrip()
            break  # 一行 note 只可能有一种前缀
    return s.strip()


def set_void_status(pk: str, status: str, current_note: str = "") -> Tuple[bool, str]:
    """切换业绩作废状态。status ∈ {'none', 'pending', 'final'}。

    用 update_note 写 note 前缀实现,不需要 Worker 端 schema 改动。
    权限:Worker 端 update_note 已 check (主管 OR 该单 owner_check),
         员工只能改自己的;主管能改任何人的。
    """
    if status not in ("none", "pending", "final"):
        return False, f"unknown status: {status}"
    cleaned = strip_void_prefix(current_note)
    prefix = ""
    if status == "final":
        prefix = VOID_PREFIX
    elif status == "pending":
        prefix = VOID_PENDING_PREFIX
    if prefix:
        new_note = f"{prefix} {cleaned}".strip() if cleaned else prefix
    else:
        new_note = cleaned
    return update_note(pk, new_note)


# 向下兼容:旧 void_record 调用方仍能 work
def void_record(pk: str, voided: bool, current_note: str = "") -> Tuple[bool, str]:
    return set_void_status(pk, "final" if voided else "none", current_note)


# ── 业绩 final 作废时的代付回滚 ──────────────────────────
# 业绩配对的代付:之前在 Google Sheets 写了 E='已出货' / L=perf_code / J=出货日期。
# 业绩作废后,这笔代付不再有合法配对 — 需要把代付表回滚到「未核销」状态,
# 让员工在「代付情况」tab 看到这笔代付重新出现 + 备注「{perf_code} 已取消出貨」可追溯。
SHEETS_NOTE_COL = "M"


def _build_void_revert_note(perf_code: str) -> str:
    """生成代付表 M 列填的备注:'{perf_code} 已取消出貨' 例如 '白050302 已取消出貨'。"""
    code = (perf_code or "").strip()
    return f"{code} 已取消出貨" if code else "已取消出貨"


SHEETS_WHITE_RGB = {"red": 1.0, "green": 1.0, "blue": 1.0}  # 撤销「核销黄」回白底


def _revert_sheets_payment_rows(matches: List[dict], perf_code: str, log: LogFn = None) -> int:
    """对每条代付行:
       - 清空 E/J/L 三列
       - M 列追加「{perf_code} 已取消出貨」(保留原私人备注,以「 | 」分隔)
       - 整行(A:M)背景从「核销黄」涂回白色,视觉一致

    matches: [{row_no, rid, code, status, note, ...}, ...] — 来自 fetch_payment_records_from_sheets
    perf_code: 业绩编码,用于生成 M 列追加内容
    返回成功回滚的行数。
    """
    if not matches:
        return 0
    log = log or (lambda *_: None)
    if not SERVICE_ACCOUNT_PATH.exists():
        log(f"[代付-回滚] service_account.json 不存在,无法回写")
        return 0
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log(f"[代付-回滚] gspread 未装,无法回写")
        return 0

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEETS_ID)
        ws = sh.worksheet(GOOGLE_SHEETS_TAB)
        sheet_id = ws.id
    except Exception as e:
        log(f"[代付-回滚] Sheets 连接失败:{e}")
        return 0

    void_note = _build_void_revert_note(perf_code)
    success = 0
    BATCH = 30
    for i in range(0, len(matches), BATCH):
        chunk = matches[i:i + BATCH]
        data = []
        for m in chunk:
            row_no = m["row_no"]
            # M 列追加(保留原私人备注): "{原 M} | {perf_code} 已取消出貨"
            old_note = (m.get("note") or "").strip()
            # 防止重复追加(同一条多次作废只保留一次标记)
            if void_note in old_note:
                merged_note = old_note  # 已有该标记,不再重复追加
            elif old_note:
                merged_note = f"{old_note} | {void_note}"
            else:
                merged_note = void_note
            # 清 E/J/L,M 追加备注
            data.append({"range": f"{SHEETS_STATUS_COL}{row_no}",   "values": [[""]]})
            data.append({"range": f"{SHEETS_SHIPDATE_COL}{row_no}", "values": [[""]]})
            data.append({"range": f"{SHEETS_CODE_COL}{row_no}",     "values": [[""]]})
            data.append({"range": f"{SHEETS_NOTE_COL}{row_no}",     "values": [[merged_note]]})
        try:
            ws.batch_update(data, value_input_option="USER_ENTERED")
            success += len(chunk)
            log(f"[代付-回滚] 第 {i//BATCH+1} 批写 Sheets 成功 ({len(chunk)} 条)")
        except Exception as e:
            log(f"[代付-回滚] 第 {i//BATCH+1} 批写 Sheets 失败:{e}")
            continue
        # 第二步:整行(A:M)涂白,撤销「核销黄」
        try:
            paint_requests = []
            for m in chunk:
                paint_requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": m["row_no"] - 1,
                            "endRowIndex": m["row_no"],
                            "startColumnIndex": 0,
                            "endColumnIndex": SHEETS_ROW_PAINT_END_COL,  # A-M 13 列
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": SHEETS_WHITE_RGB,
                                "backgroundColorStyle": {"rgbColor": SHEETS_WHITE_RGB},
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle",
                    }
                })
            if paint_requests:
                sh.batch_update({"requests": paint_requests})
        except Exception as e:
            # 涂白失败不影响数据已写入(行仍是黄色但 E/J/L 已清,可接受)
            log(f"[代付-回滚] 第 {i//BATCH+1} 批涂白失败({len(chunk)} 条,但 E/J/L/M 已写):{e}")
    return success


def _mark_payments_unreconciled(rids: List[str], log: LogFn = None) -> int:
    """把 D1 payments 表的这些 rid status 改回空字符串(未核销)。
    用现有 mark_status API,传 status='' 即可。"""
    if not rids:
        return 0
    log = log or (lambda *_: None)
    chat_id = _get_chat_id()
    try:
        r = _req.post(f"{WORKER_URL}/api/payment/mark_status",
                      json={"chat_id": chat_id, "rids": rids, "status": ""},
                      timeout=30)
        d = r.json()
        if d.get("ok"):
            n = d.get("updated", 0)
            log(f"[D1-回滚] mark_status 成功 {n} 条")
            return n
        log(f"[D1-回滚] mark_status 失败:{d.get('error', '')}")
        return 0
    except Exception as e:
        log(f"[D1-回滚] mark_status 异常:{e}")
        return 0


def revert_void_in_payment_sheet(perf_code: str, log: LogFn = None) -> dict:
    """业绩 final 作废时反向回滚:
       - 找 Sheets 上 L 列=perf_code 且 E='已出货' 的代付行
       - E/J/L 清空,M 填「編碼已取消出貨」
       - D1 payments 对应 rid status 改回 ''(未核销)
    Returns: {ok, sheets_reverted, d1_marked, matches, perf_code}
    """
    log = log or (lambda *_: None)
    if not perf_code:
        return {"ok": False, "error": "no perf_code", "matches": 0}

    sheet_recs = fetch_payment_records_from_sheets(log=log)
    if not sheet_recs:
        log(f"[作废-回滚] 代付表无数据,跳过 perf_code={perf_code}")
        return {"ok": True, "sheets_reverted": 0, "d1_marked": 0, "matches": 0,
                "perf_code": perf_code}

    matches = [r for r in sheet_recs
               if str(r.get("code") or "").strip() == perf_code.strip()
               and str(r.get("status") or "").strip() == GOOGLE_SHEETS_STATUS_OK]

    if not matches:
        log(f"[作废-回滚] perf_code={perf_code} 在代付表无对应已核销记录,跳过")
        return {"ok": True, "sheets_reverted": 0, "d1_marked": 0, "matches": 0,
                "perf_code": perf_code}

    log(f"[作废-回滚] perf_code={perf_code} 找到 {len(matches)} 条已核销代付,开始回滚...")
    sheets_n = _revert_sheets_payment_rows(matches, perf_code, log=log)

    # 只对真实 rid (非 _synth_ 合成的) 调 D1 mark_status
    real_rids = [m["rid"] for m in matches
                 if m.get("rid") and not str(m["rid"]).startswith("_synth_")]
    d1_n = _mark_payments_unreconciled(real_rids, log=log) if real_rids else 0

    return {"ok": True, "sheets_reverted": sheets_n, "d1_marked": d1_n,
            "matches": len(matches), "perf_code": perf_code}


# ── 代付表同步（核销用） ──────────────────────────────
GOOGLE_SHEETS_ID = "<GOOGLE_SHEETS_ID_REDACTED>"
GOOGLE_SHEETS_TAB = "代付"
SERVICE_ACCOUNT_PATH = Path(r"C:\Users\<USER>\Desktop\代付識別機器人\service_account.json")


def _eval_payment_amount(s: str) -> float:
    """代付金额：可能是 100+100 这种文本算式，需要算总和"""
    return _eval_simple_formula(s)


def fetch_payment_records_from_sheets(log: LogFn = None) -> List[dict]:
    """从 Google Sheets「代付」表读所有记录，返回 dict 列表。
    需要 service_account.json（代付识别机器人那边的）。
    """
    if not SERVICE_ACCOUNT_PATH.exists():
        if log: log(f"[代付] service_account.json 不存在：{SERVICE_ACCOUNT_PATH}")
        return []
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        if log: log(f"[代付] gspread 未装：{e}（pip install gspread google-auth）")
        return []

    # 读写 scope（核销时要回写 E 列「备注」状态）
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEETS_ID)
        ws = sh.worksheet(GOOGLE_SHEETS_TAB)
        rows = ws.get_all_values()
    except Exception as e:
        if log: log(f"[代付] 读 Sheets 失败：{e}")
        return []

    if len(rows) < 2:
        return []

    headers = [h.strip().lstrip("\ufeff") for h in rows[0]]
    idx = {h: i for i, h in enumerate(headers)}
    # 「备注」是 E 列状态字段（已出货/已退款/异常/空），「備註」M 列是自由文本
    # 注意 headers 里两个字段都叫备注/備註，靠列序区分：第一个出现的是 E 列状态
    status_col_idx = -1
    note_col_idx = -1
    for i, h in enumerate(headers):
        if h in ("备注", "備註"):
            if status_col_idx < 0:
                status_col_idx = i
            else:
                note_col_idx = i
                break
    out = []
    synth_count = 0
    for ri, r in enumerate(rows[1:], start=2):  # ri = Google Sheets 行号（1-based, 含 header 所以从 2 开始）
        if not r or not any(c.strip() for c in r):
            continue
        def _g(k):
            i = idx.get(k, -1)
            if i < 0 or i >= len(r):
                return ""
            return str(r[i] or "").strip()
        def _gi(i):
            if i < 0 or i >= len(r):
                return ""
            return str(r[i] or "").strip()
        rid = _g("RID")
        amt_raw = _g("金額")
        payer = _g("姓名")
        date_str = _g("代付日期")
        # 没 RID 的行（用户手动填的、或代付识别机器人未生成）→ 用稳定哈希合成 RID
        # 必须基于 (date|amount|payer|row_no) — 包含 row_no 让插入新行不会影响其他 RID
        if not rid:
            # 跳过完全没数据的行（避免给空行也生成 RID）
            if not (date_str and amt_raw and payer):
                continue
            import hashlib as _hl
            key = f"{date_str}|{amt_raw}|{payer}|row{ri}"
            rid = "_synth_" + _hl.md5(key.encode("utf-8")).hexdigest()[:16]
            synth_count += 1
        out.append({
            "rid": rid,
            "row_no": ri,                        # Google Sheets 行号，回写时用
            "pay_date": _to_date_str(date_str),
            "amount_raw": amt_raw,
            "amount": _eval_payment_amount(amt_raw),
            "payer": payer,
            "ship_date": _to_date_str(_g("出貨日期")),
            "code": _g("編碼"),
            "note": _gi(note_col_idx),           # M 列自由备注
            "status": _gi(status_col_idx),       # E 列状态：'' / '已出货' / '已退款' / '异常'
        })
    if synth_count and log:
        log(f"[代付] 注：{synth_count} 行 Sheets 没 RID 列，已用「行号+内容」哈希合成 RID（不影响匹配）")
    return out


# Sheets 列字母（回写用）— E 备注/状态、J 出货日期、L 编码、M 备注（不动）
SHEETS_STATUS_COL = "E"
SHEETS_SHIPDATE_COL = "J"
SHEETS_CODE_COL = "L"
SHEETS_ROW_PAINT_END_COL = 13  # A-M 13 列（与用户历史习惯一致）
SHEETS_YELLOW_RGB = {"red": 1.0, "green": 1.0, "blue": 0.0}  # 已出货纯黄（与历史手填行一致）
GOOGLE_SHEETS_STATUS_OK = "已出货"
GOOGLE_SHEETS_STATUSES_SKIP = ("已退款", "异常", "已退款", "異常")  # 这些状态跳过核销


def write_back_matches_to_sheets(matches: List[dict],
                                  log: LogFn = None) -> List[dict]:
    """批量回写 Google Sheets 代付表 — 每个 match 写 E/L/J 三栏（不动 M）。

    matches: [{row_no, status, code, ship_date_short, existing_code}, ...]
      - row_no: Sheets 行号（1-based）
      - status: 'E' 列写入值（通常 '已出货'）
      - code:   'L' 列写入值（perf.code，仅当 existing_code 为空时才写）
      - ship_date_short: 'J' 列写入值（'4月23日' 短格式）

    返回成功写入的 matches 子列表。部分失败只返回成功部分。
    """
    if not matches:
        return []
    if not SERVICE_ACCOUNT_PATH.exists():
        if log: log(f"[代付] service_account.json 不存在，无法回写")
        return []
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        if log: log(f"[代付] gspread 未装，无法回写")
        return []
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(GOOGLE_SHEETS_ID)
        ws = sh.worksheet(GOOGLE_SHEETS_TAB)
    except Exception as e:
        if log: log(f"[代付] Sheets 连接失败：{e}")
        return []
    # 每个 match 最多 3 个 cell update，按 match 分批（每批 ~30 个 match → 90 ranges）
    sheet_id = ws.id
    BATCH = 30
    success: List[dict] = []
    for i in range(0, len(matches), BATCH):
        chunk = matches[i:i + BATCH]
        data = []
        for m in chunk:
            row_no = m["row_no"]
            # E 列：状态
            data.append({"range": f"{SHEETS_STATUS_COL}{row_no}", "values": [[m["status"]]]})
            # L 列：编码（仅当原本为空时才写，保护手填的旧记录）
            if not m.get("existing_code", "").strip() and m.get("code"):
                data.append({"range": f"{SHEETS_CODE_COL}{row_no}", "values": [[m["code"]]]})
            # J 列：出货日期短格式
            if m.get("ship_date_short"):
                data.append({"range": f"{SHEETS_SHIPDATE_COL}{row_no}", "values": [[m["ship_date_short"]]]})
        try:
            ws.batch_update(data, value_input_option="USER_ENTERED")
        except Exception as e:
            if log: log(f"[代付] Sheets 回写第 {i//BATCH+1} 批失败（{len(chunk)} 条）：{e}")
            continue
        # 第二步：把这批已写「已出货」的行整行涂纯黄（A:M），跟历史手填行一致
        try:
            paint_requests = []
            for m in chunk:
                if m.get("status") != GOOGLE_SHEETS_STATUS_OK:
                    continue
                paint_requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": m["row_no"] - 1,
                            "endRowIndex": m["row_no"],
                            "startColumnIndex": 0,
                            "endColumnIndex": SHEETS_ROW_PAINT_END_COL,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": SHEETS_YELLOW_RGB,
                                "backgroundColorStyle": {"rgbColor": SHEETS_YELLOW_RGB},
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle",
                    }
                })
            if paint_requests:
                sh.batch_update({"requests": paint_requests})
        except Exception as e:
            # 涂色失败不影响数据已写入的结果
            if log: log(f"[代付] Sheets 涂黄第 {i//BATCH+1} 批失败（{len(chunk)} 条，但 E/L/J 已写）：{e}")
        success.extend(chunk)
    if log: log(f"[代付] Sheets 回写完成：{len(success)}/{len(matches)} 条")
    return success


def upload_payment_records(records: List[dict], log: LogFn = None) -> int:
    """上传代付记录到 D1（仅主管）"""
    chat_id = _get_chat_id()
    if chat_id != SUPERVISOR_CHAT_ID:
        if log: log("[代付] 仅主管可同步")
        return 0
    if not records:
        return 0
    BATCH = 200
    total = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        try:
            r = _req.post(f"{WORKER_URL}/api/payment/upload",
                          json={"chat_id": chat_id, "records": chunk}, timeout=60)
            d = r.json()
            if d.get("ok"):
                total += d.get("replaced", 0)
            else:
                if log: log(f"[代付] 上传失败：{d.get('error', '')}")
        except Exception as e:
            if log: log(f"[代付] 上传异常：{e}")
    return total


def sync_payments_to_d1(log: LogFn = None) -> int:
    """主管：拉 Google Sheets「代付」→ 写 D1 payment_records"""
    if log: log("[代付] 从 Google Sheets 拉代付表...")
    recs = fetch_payment_records_from_sheets(log=log)
    if log: log(f"[代付] Sheets 共 {len(recs)} 条")
    if not recs:
        return 0
    n = upload_payment_records(recs, log=log)
    if log: log(f"[代付] 已写入 D1 {n} 条")
    return n


def cleanup_orphan_payments(valid_rids: List[str], log: LogFn = None) -> int:
    """主管：清理 D1 里 Sheets 已删除但 D1 残留的 rid（OCR 撤销后遗留的孤儿代付）。
    传入当前 Sheets 的全部 rid，Worker 删掉不在列表里的。返回删除条数。
    """
    chat_id = _get_chat_id()
    if chat_id != SUPERVISOR_CHAT_ID:
        return 0
    if not valid_rids:
        return 0
    try:
        r = _req.post(f"{WORKER_URL}/api/payment/cleanup_orphans",
                      json={"chat_id": chat_id, "valid_rids": valid_rids},
                      timeout=60)
        d = r.json()
        if d.get("ok"):
            return d.get("deleted", 0)
        if log: log(f"[代付] 清理孤儿失败：{d.get('error', '')}")
    except Exception as e:
        if log: log(f"[代付] 清理孤儿异常：{e}")
    return 0


def sync_and_reconcile(log: LogFn = None) -> dict:
    """主管：完整核销流程一站式 —
        1. 拉 Google Sheets 代付表 → 写 D1（含 status）
        2. 跑 Worker 核销算法 → 拿到新核销的 rid 列表
        3. 把那些 rid 对应行的 E 列回写为「已出货」
        4. 调 Worker 同步 D1 status='已出货'

    返回 {synced, matched, sheets_written, d1_marked, errors}
    """
    chat_id = _get_chat_id()
    if chat_id != SUPERVISOR_CHAT_ID:
        if log: log("[核销] 仅主管可执行")
        return {"ok": False, "error": "supervisor only"}

    result = {"ok": True, "synced": 0, "matched": 0, "sheets_written": 0, "d1_marked": 0}

    # Step 1: 拉 Sheets
    if log: log("[核销] 步骤 1/4：拉 Google Sheets 代付表...")
    sheet_recs = fetch_payment_records_from_sheets(log=log)
    if not sheet_recs:
        if log: log("[核销] 代付表无数据，跳过")
        return result
    if log: log(f"[核销]   Sheets 共 {len(sheet_recs)} 条")

    # rid → row_no 索引（回写时找行号用）
    rid_to_row = {r["rid"]: r["row_no"] for r in sheet_recs}

    # v6.0.80 ★ Step 1.5:主動清理「對應作廢業績的誤核銷」+ 取作廢清單給 Step 4 過濾用
    # 場景:主管 final 作廢業績,Sheets 已 revert,但 worker reconcile 下次又把代付匹配回作廢業績
    # 修法:每次 reconcile 開頭都掃一遍 D1 voided perf,主動清掉殘留 + 過濾本次配對
    voided_codes: set = set()
    try:
        from core.latest_template_builder import _query_d1_voided_pks
        voided_pks = _query_d1_voided_pks(log_fn=log)
        voided_codes = {pk.split('|', 1)[0] for pk in voided_pks if '|' in pk and pk.split('|', 1)[0]}
        if log: log(f"[核销] 步骤 1.5/4:D1 作廢業績 {len(voided_codes)} 個 perf_code,檢查代付表殘留...")

        # 找已誤核銷的代付行(status='已出货' 且 code 屬於作廢業績)
        wrongly_reconciled = [r for r in sheet_recs
                              if str(r.get("status") or "").strip() == GOOGLE_SHEETS_STATUS_OK
                              and str(r.get("code") or "").strip() in voided_codes]
        if wrongly_reconciled:
            if log: log(f"[核销]   發現 {len(wrongly_reconciled)} 條誤核銷,逐筆清理(重用 sheet_recs 不重複拉)")
            # 按 perf_code 分組,呼叫底層 _revert_sheets_payment_rows + _mark_payments_unreconciled
            by_code: Dict[str, List[dict]] = {}
            for r in wrongly_reconciled:
                code = str(r.get("code") or "").strip()
                by_code.setdefault(code, []).append(r)

            total_reverted = 0
            for code, matches in by_code.items():
                try:
                    n = _revert_sheets_payment_rows(matches, code, log=log)
                    total_reverted += n
                    # D1 也標回未核銷
                    real_rids = [m.get("rid", "") for m in matches
                                 if m.get("rid") and not str(m["rid"]).startswith("_synth_")]
                    if real_rids:
                        _mark_payments_unreconciled(real_rids, log=log)
                    if log: log(f"[核销]   ✓ {code}: 清 {n} 條代付")
                except Exception as e:
                    if log: log(f"[核销]   ✗ {code}: 清理失敗 {e}")
            result["voided_cleaned"] = total_reverted
            if log: log(f"[核销]   共清理 {total_reverted} 條誤核銷代付")
        else:
            if log: log(f"[核销]   無誤核銷殘留")
    except Exception as e:
        if log: log(f"[核销] 1.5 檢查作廢清單失敗(不阻斷主流程):{e}")
        voided_codes = set()

    # Step 2: 上传 D1 + 清理孤儿（Sheets 已删但 D1 残留的 rid）
    if log: log("[核销] 步骤 2/4：写入 D1...")
    n_synced = upload_payment_records(sheet_recs, log=log)
    result["synced"] = n_synced
    if log: log(f"[核销]   D1 已写入 {n_synced} 条")
    # 清理孤儿 rid（OCR 撤销后的残留）
    valid_rids = [r["rid"] for r in sheet_recs if r.get("rid")]
    orphans_deleted = cleanup_orphan_payments(valid_rids, log=log)
    result["orphans_deleted"] = orphans_deleted
    if log and orphans_deleted > 0:
        log(f"[核销]   清理 D1 孤儿 rid {orphans_deleted} 条")

    # Step 3: 跑核销算法（Worker 端）
    if log: log("[核销] 步骤 3/4：执行配对算法...")
    try:
        r = _req.post(f"{WORKER_URL}/api/payment/reconcile_run",
                      json={"chat_id": chat_id}, timeout=60)
        d = r.json()
        if not d.get("ok"):
            if log: log(f"[核销] 配对失败：{d.get('error', '')}")
            result["ok"] = False
            result["error"] = d.get("error")
            return result
    except Exception as e:
        if log: log(f"[核销] 配对异常：{e}")
        result["ok"] = False
        result["error"] = str(e)
        return result
    matched_list = d.get("rids", [])
    if log: log(f"[核销]   Worker 配對 {len(matched_list)} 筆代付")

    # v6.0.80 ★:過濾掉「對應作廢業績」的配對(worker 端 reconcile 沒過濾,本地把關)
    # 場景:作廢業績不該再被配對代付。如果 worker 沒過濾,本地強制丟掉
    if voided_codes and matched_list:
        filtered = [m for m in matched_list if (m.get("perf_code") or "").strip() not in voided_codes]
        skipped = len(matched_list) - len(filtered)
        if skipped:
            if log: log(f"[核销]   過濾 {skipped} 筆配對(對應業績已作廢,不再核銷)")
        matched_list = filtered

    result["matched"] = len(matched_list)
    if not matched_list:
        if log: log("[核销] 没有新配对(已過濾作廢),流程结束")
        return result

    # Step 4a: 回写 Google Sheets E/L/J 三栏（M 不动 — 用户私人备注）+ 涂黄
    if log: log(f"[核销] 步骤 4/4：回写 Google Sheets E/L/J...")
    matches_to_write: List[dict] = []
    row_to_rid: Dict[int, str] = {}
    for m in matched_list:
        rid = m.get("rid")
        row_no = rid_to_row.get(rid)
        if not row_no or not rid:
            continue
        matches_to_write.append({
            "row_no": row_no,
            "status": GOOGLE_SHEETS_STATUS_OK,
            "code": m.get("perf_code", ""),
            "ship_date_short": _fmt_ship_date_short(m.get("perf_ship_date", "")),
            "existing_code": m.get("existing_code", ""),
        })
        row_to_rid[row_no] = rid
    successful_writes = write_back_matches_to_sheets(matches_to_write, log=log)
    # 只取 Sheets 真的写成功的那部分 rid 去标 D1
    successful_rids = [row_to_rid[m["row_no"]] for m in successful_writes if m["row_no"] in row_to_rid]
    result["sheets_written"] = len(successful_writes)
    if not successful_rids:
        if log: log("[核销] Sheets 回写完全失败，D1 status 不更新（下次重试）")
        result["ok"] = False
        result["error"] = "Sheets 回写失败"
        return result

    # Step 4b: 同步 D1 status — 只标 Sheets 真的写成功的
    try:
        r = _req.post(f"{WORKER_URL}/api/payment/mark_status",
                      json={"chat_id": chat_id, "rids": successful_rids,
                             "status": GOOGLE_SHEETS_STATUS_OK}, timeout=60)
        dd = r.json()
        if dd.get("ok"):
            result["d1_marked"] = dd.get("updated", 0)
            if log: log(f"[核销]   D1 已标记 {result['d1_marked']} 条 status='已出货'")
        else:
            if log: log(f"[核销] D1 标记失败：{dd.get('error', '')}")
    except Exception as e:
        if log: log(f"[核销] D1 标记异常：{e}")

    # 如果有部分 Sheets 写失败 — 告知用户但整体算成功（因为 D1 与 Sheets 已成功部分一致）
    if len(successful_writes) < len(matches_to_write):
        failed_count = len(matches_to_write) - len(successful_writes)
        if log: log(f"[核销] 警告：{failed_count} 笔 Sheets 写失败 — 下次同步会自动重试")

    return result


def query_months(owner: str = "") -> List[str]:
    """拉 D1 里有业绩的月份列表（YYYY-MM，DESC）"""
    chat_id = _get_chat_id()
    params = {"chat_id": chat_id}
    if owner:
        params["owner"] = owner
    try:
        r = _req.get(f"{WORKER_URL}/api/performance/months", params=params, timeout=30)
        d = r.json()
        return d.get("months", []) if d.get("ok") else []
    except Exception:
        return []


def query_owners() -> List[str]:
    """主管专用：列出所有 owner"""
    chat_id = _get_chat_id()
    try:
        r = _req.get(f"{WORKER_URL}/api/performance/owners",
                     params={"chat_id": chat_id}, timeout=30)
        d = r.json()
        return [row["owner"] for row in d.get("rows", [])] if d.get("ok") else []
    except Exception:
        return []


# ── 同步主流程 ────────────────────────────────────────────
def sync_shipping_dir(shipping_dir: Path, log: LogFn = None) -> Tuple[int, int, int]:
    """扫整个出货资料目录，按 mtime 增量上传，返回 (inserted, skipped, rejected)"""
    if not shipping_dir.exists():
        if log: log(f"[业绩] 目录不存在：{shipping_dir}")
        return 0, 0, 0
    state = _load_sync_state()
    file_state = state.get("files", {})

    files = sorted(shipping_dir.glob("出货资料_*.xlsx"))
    if log: log(f"[业绩] 扫到 {len(files)} 个出货资料")

    total_ins, total_skp, total_rej = 0, 0, 0
    for fp in files:
        try:
            mt = int(fp.stat().st_mtime)
            old_mt = file_state.get(fp.name, {}).get("mtime", 0)
            if old_mt and old_mt >= mt:
                continue  # 没变化
            recs = parse_shipping_excel(fp, log=log)
            if not recs:
                file_state[fp.name] = {"mtime": mt, "count": 0}
                continue
            ins, skp, rej = upload_records(recs, log=log)
            total_ins += ins
            total_skp += skp
            total_rej += rej
            file_state[fp.name] = {"mtime": mt, "count": len(recs),
                                    "inserted": ins, "skipped": skp}
            if log:
                log(f"[业绩] {fp.name}: {len(recs)} 条 → 新增 {ins}, 已存在 {skp}, 拒收 {rej}")
            # v6.0.51: 同步 Excel 到阿里雲(失敗不抛)
            if ins > 0 or skp > 0:
                try:
                    upload_excel_to_aliyun(fp, log=log)
                except Exception:
                    pass
        except Exception as e:
            if log: log(f"[业绩] {fp.name} 处理异常：{e}")

    state["files"] = file_state
    state["last_full_sync"] = int(time.time())
    _save_sync_state(state)
    return total_ins, total_skp, total_rej


# ══════════════════════════════════════════════════════════════
# Excel 导出
# ══════════════════════════════════════════════════════════════
_EXPORT_COL_TITLES = [
    "编码", "账号", "出货日期", "订单编码", "所属人", "客户",
    "台币", "人民币", "数量", "包装", "运费", "派送费", "成本",
    "毛利", "利润比", "核销", "备注",
]
_EXPORT_COL_KEYS = [
    "code", "account", "ship_date", "order_code", "owner", "customer",
    "total_twd", "total_cny", "qty", "pack_cost", "freight", "delivery", "cost",
    "profit", "profit_ratio", "reconcile_status", "note",
]
_EXPORT_NUM_FMT = {
    "total_twd": "0", "total_cny": "0.00", "qty": "0",
    "pack_cost": "0", "freight": "0", "delivery": "0", "cost": "0.00",
    "profit": "0.00", "profit_ratio": "0.00",
}


def export_records_to_xlsx(rows: List[dict], out_path: Path,
                            month: str = "", owner_label: str = "") -> None:
    """把业绩 rows 写到 xlsx。
    第 1 行：标题（"业绩 YYYY-MM / 所属人: xxx / 共 N 条"）
    第 2 行：表头
    第 3 行起：数据
    末尾：汇总行（按所属人 / 总计）
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "业绩"

    # 样式
    title_font = Font(name="等线", size=14, bold=True)
    header_font = Font(name="等线", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4F81BD")
    total_font = Font(name="等线", size=11, bold=True)
    total_fill = PatternFill("solid", fgColor="FFF2CC")
    unreconciled_fill = PatternFill("solid", fgColor="FFC7CE")  # 粉红
    mismatch_fill = PatternFill("solid", fgColor="FFEB9C")      # 黄
    border = Border(
        left=Side(style="thin", color="BFBFBF"),
        right=Side(style="thin", color="BFBFBF"),
        top=Side(style="thin", color="BFBFBF"),
        bottom=Side(style="thin", color="BFBFBF"),
    )
    center = Alignment(horizontal="center", vertical="center")

    # 第 1 行：标题
    n_cols = len(_EXPORT_COL_TITLES)
    title_txt = f"业绩 {month}  /  所属人: {owner_label or '(全部)'}  /  共 {len(rows)} 条"
    ws.cell(row=1, column=1, value=title_txt).font = title_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24

    # 第 2 行：表头
    for ci, t in enumerate(_EXPORT_COL_TITLES, start=1):
        c = ws.cell(row=2, column=ci, value=t)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border
    ws.row_dimensions[2].height = 20
    ws.freeze_panes = "A3"  # 冻结表头

    # 第 3 行起：数据
    # 17 列布局：A编码 B账号 C出货日期 D订单编码 E所属人 F客户
    #          G台币 H人民币 I数量 J包装 K运费 L派送费 M成本 N毛利 O利润比 P核销 Q备注
    # 派生列用公式（修改台币/数量/成本会自动重算）：
    #   H 人民币 = G/5.54*0.9
    #   K 运费   = I*20
    #   N 毛利   = H-M-J-K-L
    #   O 利润比 = IF(M=0,0,G/M)
    FORMULA_COLS = {
        "total_cny":    lambda r: f"=G{r}/{TWD_TO_CNY_RATE}*{CNY_PROFIT_FACTOR}",
        "freight":      lambda r: f"=I{r}*{int(FREIGHT_PER_QTY)}",
        "profit":       lambda r: f"=H{r}-M{r}-J{r}-K{r}-L{r}",
        "profit_ratio": lambda r: f"=IF(M{r}=0,0,G{r}/M{r})",
    }
    r = 3
    for row in rows:
        for ci, key in enumerate(_EXPORT_COL_KEYS, start=1):
            if key in FORMULA_COLS:
                cell = ws.cell(row=r, column=ci, value=FORMULA_COLS[key](r))
            elif key in _EXPORT_NUM_FMT:
                try:
                    v = float(row.get(key) or 0)
                except Exception:
                    v = 0
                cell = ws.cell(row=r, column=ci, value=v)
            else:
                cell = ws.cell(row=r, column=ci, value=row.get(key, ""))
            if key in _EXPORT_NUM_FMT:
                cell.number_format = _EXPORT_NUM_FMT[key]
            cell.border = border
        # 染色：未核销粉红、部分核销/金额不匹配黄
        recon = str(row.get("reconcile_status", ""))
        if recon == "未核销":
            for ci in range(1, n_cols + 1):
                ws.cell(row=r, column=ci).fill = unreconciled_fill
        elif recon.startswith("缺") or recon.startswith("多付") or recon == "金额不匹配":
            for ci in range(1, n_cols + 1):
                ws.cell(row=r, column=ci).fill = mismatch_fill
        r += 1

    last_data_row = r - 1
    total_row = r
    ws.cell(row=total_row, column=1, value="合计").font = total_font
    for ci in range(1, n_cols + 1):
        ws.cell(row=total_row, column=ci).fill = total_fill
        ws.cell(row=total_row, column=ci).border = border
        ws.cell(row=total_row, column=ci).font = total_font

    # 合计行：SUM 公式（利润比用 AVERAGEIF 跳过 0 值）
    if last_data_row >= 3:
        SUM_COLS = {
            "total_twd":    "G",
            "total_cny":    "H",
            "qty":          "I",
            "pack_cost":    "J",
            "freight":      "K",
            "delivery":     "L",
            "cost":         "M",
            "profit":       "N",
        }
        for key, col in SUM_COLS.items():
            ci = _EXPORT_COL_KEYS.index(key) + 1
            c = ws.cell(row=total_row, column=ci, value=f"=SUM({col}3:{col}{last_data_row})")
            if key in _EXPORT_NUM_FMT:
                c.number_format = _EXPORT_NUM_FMT[key]
        # 利润比：平均（跳过 0，避免无成本的行拉低均值）
        ci = _EXPORT_COL_KEYS.index("profit_ratio") + 1
        c = ws.cell(row=total_row, column=ci,
                    value=f'=IFERROR(AVERAGEIF(O3:O{last_data_row},">0"),0)')
        c.number_format = _EXPORT_NUM_FMT["profit_ratio"]

    # 列宽（按 UI 顺序微调）
    col_widths = [12, 12, 12, 18, 10, 10, 10, 10, 6, 6, 6, 8, 10, 10, 8, 10, 24]
    for ci, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    # 主管多人导出：加「按所属人汇总」sheet — 用 COUNTIF/SUMIF 公式，改数据会自动重算
    if (not owner_label or owner_label == "全部") and last_data_row >= 3:
        owners = sorted({(row.get("owner", "") or "(空)") for row in rows})
        ws2 = wb.create_sheet("按所属人汇总")
        ws2.append(["所属人", "订单数", "台币合计", "人民币合计", "毛利合计"])
        data_range_E = f"业绩!E3:E{last_data_row}"
        data_range_G = f"业绩!G3:G{last_data_row}"
        data_range_H = f"业绩!H3:H{last_data_row}"
        data_range_N = f"业绩!N3:N{last_data_row}"
        for i, o in enumerate(owners, start=2):
            ws2.cell(row=i, column=1, value=o)
            ws2.cell(row=i, column=2, value=f'=COUNTIF({data_range_E},A{i})')
            c_twd = ws2.cell(row=i, column=3, value=f'=SUMIF({data_range_E},A{i},{data_range_G})')
            c_twd.number_format = "0"
            c_cny = ws2.cell(row=i, column=4, value=f'=SUMIF({data_range_E},A{i},{data_range_H})')
            c_cny.number_format = "0.00"
            c_pft = ws2.cell(row=i, column=5, value=f'=SUMIF({data_range_E},A{i},{data_range_N})')
            c_pft.number_format = "0.00"
        # 合计行
        total_r = len(owners) + 2
        ws2.cell(row=total_r, column=1, value="合计").font = total_font
        for col_letter, num_fmt in [("B", "0"), ("C", "0"), ("D", "0.00"), ("E", "0.00")]:
            ci = ord(col_letter) - ord("A") + 1
            c = ws2.cell(row=total_r, column=ci,
                         value=f'=SUM({col_letter}2:{col_letter}{total_r-1})')
            c.number_format = num_fmt
            c.font = total_font
            c.fill = total_fill
        ws2.cell(row=total_r, column=1).fill = total_fill
        # 表头样式
        for c in ws2[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
        for col_letter, w in zip("ABCDE", [12, 10, 12, 14, 12]):
            ws2.column_dimensions[col_letter].width = w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))


# ══════════════════════════════════════════════════════════════
# UI Tab
# ══════════════════════════════════════════════════════════════
class PerformanceFeatureTab:
    """业绩 Tab（员工/主管自适应）"""

    def __init__(self, *, app: Any, frame: ttk.Frame):
        self.app = app
        self.frame = frame
        self._is_supervisor = (_get_chat_id() == SUPERVISOR_CHAT_ID)
        self._my_owner = _get_my_owner()
        self._sync_running = False

        # UI vars
        self.var_month = tk.StringVar(value=time.strftime("%Y-%m"))
        self.var_owner_filter = tk.StringVar(value="(全部)" if self._is_supervisor else self._my_owner)
        self.var_account_filter = tk.StringVar(value="(全部)")
        self.var_status = tk.StringVar(value="未同步")

        # Tree
        self.tree: Optional[ttk.Treeview] = None
        self.summary_label: Optional[ttk.Label] = None

        # v6.0.52: 啟動背景補傳(retry queue + 歷史檔差集)
        try:
            import threading as _th
            _th.Thread(target=self._startup_retry_aliyun_uploads,
                        daemon=True, name="aliyun_retry").start()
            _th.Thread(target=self._startup_sync_local_history,
                        daemon=True, name="aliyun_history").start()
            # 啟動背景 SYB 重量同步(60s 後跑,失敗不擋)— 僅主管跑
            if self._is_supervisor:
                _th.Thread(target=self._startup_sync_syb_weights,
                            daemon=True, name="syb_weights").start()
            # Yahoo 訂單核對(90s 後跑,失敗不擋)— 員工+主管都跑(各自 PC 對自己 profile)
            _th.Thread(target=self._startup_perf_verify,
                        daemon=True, name="perf_verify").start()
        except Exception:
            pass

    def _startup_perf_verify(self):
        """啟動 90s 後背景:Yahoo 訂單核對(出貨 30 天前的業績驗證最終狀態)。

        12h 節流(內部),失敗不擋。員工+主管都跑(各自 PC 對自己的 profile)。
        異常訂單(已退款/取消等)會標記到本地 sqlite,顯示時 patch。
        """
        try:
            import time as _t
            _t.sleep(90)
            from .perf_verify_engine import startup_auto_verify
            startup_auto_verify(log=self.log)
        except Exception as e:
            self.log(f"[VERIFY] 啟動異常:{e}")

    def _startup_sync_syb_weights(self):
        """啟動 60s 後背景:同步 SYB 重量 + 推回 D1。12h 內已跑過則跳過。"""
        try:
            import time as _t
            _t.sleep(60)
            now = time.time()
            last_at = _read_last_auto_sync()
            if now - last_at < 12 * 3600:
                self.log(f"[SYB-W] 12h 內已自動同步過(距上次 {int((now-last_at)/60)} 分鐘),跳過")
                return
            n_sync = sync_syb_weights(log=self.log)
            n_push = push_weights_to_d1(log=self.log)
            if n_sync > 0 or n_push > 0:
                self.log(f"[SYB-W] 啟動同步:新處理 {n_sync} 條,推 D1 {n_push} 條")
            _save_last_auto_sync(now)
        except Exception as e:
            self.log(f"[SYB-W] 啟動同步異常:{e}")

    def _startup_retry_aliyun_uploads(self):
        """GUI 啟動時補傳上次失敗的阿里雲 upload(retry queue)"""
        try:
            import time as _t
            _t.sleep(15)  # 等 GUI 完整載完再跑
            n = retry_pending_uploads(log=self.log)
            if n > 0:
                self.log(f"[阿里雲] retry queue 啟動補傳成功 {n} 筆")
        except Exception as e:
            self.log(f"[阿里雲] retry queue 啟動補傳異常:{e}")

    def _startup_sync_local_history(self):
        """v6.0.52: 啟動時掃本地出貨資料目錄,跟雲端比對,補傳差集。

        每次啟動都跑,但比對後通常 0 動作(已有的 skip)。
        針對性:只補「本地有 + 雲端沒有」的檔。

        v6.0.52 修:用員工自設的 settings["ship_outdir"](採購出貨輸出目錄)而非寫死路徑
        """
        try:
            import time as _t
            _t.sleep(30)  # 等 retry queue 跑完再啟動
            # v6.0.54: 對齊「Excel 內容所屬人」= ship_owner,不是業績 paystatus_name
            owner = _get_ship_owner() or "unknown"
            if owner == "unknown":
                self.log("[阿里雲] 啟動掃:無 ship_owner / paystatus_name,跳過歷史補傳")
                return

            # v6.0.52: 各員工 ship_outdir 設置不同(採購出貨輸出目錄),
            # 嚴格只掃自設目錄,避免掃到「拷貝來但不是自己的」歷史檔
            settings = getattr(self.app, "settings", None) or {}
            outdir_str = (settings.get("ship_outdir") or "").strip()
            if outdir_str:
                scan_dir = Path(outdir_str)
            elif SHIPPING_DIR_DEFAULT.exists():
                # ship_outdir 沒設:fallback 預設(向下兼容舊版未配置的 user)
                scan_dir = SHIPPING_DIR_DEFAULT
            else:
                # 兩個都沒,fallback XDZHGL/output(order_export 真實 fallback)
                scan_dir = Path(__file__).resolve().parent.parent / "output"

            if not scan_dir.exists():
                self.log(f"[阿里雲] 啟動掃:目錄 {scan_dir} 不存在,跳過")
                return
            local_files = sorted(scan_dir.glob("出货资料_*.xlsx"))
            if not local_files:
                return

            # 2. 列雲端 owner 自己的檔(取 filename prefix 作為 set)
            cloud_files = list_aliyun_excels(owner=owner, log=lambda *_: None)
            # 雲端檔名格式:出货资料_20260418__upload_xxx.xlsx
            # 取 prefix "出货资料_20260418" 比對(不管 timestamp,只看是否有過此源檔)
            cloud_prefixes = set()
            for cf in cloud_files:
                fn = cf.get("filename", "")
                # 切到 __upload_ 之前
                prefix = fn.split("__upload_")[0]
                if prefix:
                    cloud_prefixes.add(prefix)

            # 3. 算差集
            to_upload = []
            for lf in local_files:
                # 本地檔名通常 出货资料_20260418.xlsx → prefix = 出货资料_20260418
                local_prefix = lf.stem  # 不含 .xlsx
                if local_prefix not in cloud_prefixes:
                    to_upload.append(lf)

            if not to_upload:
                self.log(f"[阿里雲] 啟動掃:本地 {len(local_files)} 檔皆已在雲端,0 補傳")
                return

            self.log(f"[阿里雲] 啟動掃:本地 {len(local_files)} 檔,雲端缺 {len(to_upload)} 檔,開始補傳...")
            success = 0
            for lf in to_upload:
                r = upload_excel_to_aliyun(lf, owner=owner, log=self.log)
                if r.get("ok"):
                    success += 1
            self.log(f"[阿里雲] 啟動掃:補傳成功 {success}/{len(to_upload)} 檔")
        except Exception as e:
            self.log(f"[阿里雲] 啟動掃歷史異常:{e}")

    def log(self, s: str) -> None:
        try:
            self.app.log(f"[PERF] {s}")
        except Exception:
            print(s)

    def build(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)  # ← 现在 tree 在 row 3

        # ── 第 1 行：筛选 ──
        bar1 = ttk.Frame(self.frame)
        bar1.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(bar1, text="月份:").pack(side="left")
        ttk.Button(bar1, text="<", command=self._click_prev_month, width=2).pack(side="left", padx=(2, 0))
        self.cmb_month = ttk.Combobox(bar1, textvariable=self.var_month, width=10, values=[time.strftime("%Y-%m")])
        self.cmb_month.pack(side="left", padx=0)
        ttk.Button(bar1, text=">", command=self._click_next_month, width=2).pack(side="left", padx=(0, 8))

        if self._is_supervisor:
            ttk.Label(bar1, text="所属人:").pack(side="left")
            self.cmb_owner = ttk.Combobox(bar1, textvariable=self.var_owner_filter, width=12, state="readonly",
                                           values=["(全部)"])
            self.cmb_owner.pack(side="left", padx=(2, 12))
        else:
            ttk.Label(bar1, text=f"所属人: {self._my_owner}",
                      foreground="#666").pack(side="left", padx=(0, 12))

        ttk.Label(bar1, textvariable=self.var_status, foreground="#888").pack(side="left", padx=(15, 0))

        # settings 缺失时红字警告（业绩无法上传/查询）
        missing = []
        if not _get_chat_id():
            missing.append("TG chat_id")
        if not self._my_owner and not self._is_supervisor:
            missing.append("paystatus_name(所属人)")
        if missing:
            ttk.Label(bar1, text=f"⚠ 缺失 settings: {', '.join(missing)} — 业绩无法上传",
                      foreground="#c00").pack(side="left", padx=(15, 0))

        # ── 第 2 行：操作按钮 ──
        bar2 = ttk.Frame(self.frame)
        bar2.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
        ttk.Button(bar2, text="刷新", command=self._click_refresh, width=12).pack(side="left", padx=2)
        ttk.Button(bar2, text="导入出货资料", command=self._click_import, width=14).pack(side="left", padx=2)
        ttk.Button(bar2, text="导出 Excel", command=self._click_export, width=12).pack(side="left", padx=2)
        if self._is_supervisor:
            ttk.Separator(bar2, orient="vertical").pack(side="left", fill="y", padx=8)
            ttk.Label(bar2, text="主管:", foreground="#888").pack(side="left", padx=(0, 4))
            ttk.Button(bar2, text="同步重量", command=self._click_sync_weights, width=10).pack(side="left", padx=2)
            ttk.Button(bar2, text="同步并核销", command=self._click_sync_payments, width=12).pack(side="left", padx=2)
            ttk.Button(bar2, text="下載月匯總", command=self._click_download_monthly_merge, width=12).pack(side="left", padx=2)
            # v6.0.60: 移除「刷新所属人」按钮(改为每次点刷新时自动跑) + 移除「修正同步」按钮(误用风险高,会抹掉作废标记)

        # 汇总区
        sum_frame = ttk.Labelframe(self.frame, text="汇总")
        sum_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        self.summary_label = ttk.Label(sum_frame, text="(请刷新)")
        self.summary_label.pack(side="left", padx=8, pady=4)

        # 明细 Tree
        tree_frame = ttk.Frame(self.frame)
        tree_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 列顺序与现有 业绩表 一致：编码/账号/出货日期/订单/所属人/客户/台币/人民币/数量/包装/运费/派送费/成本/毛利/比值/核销/备注
        cols = ("code", "account", "ship_date", "order_code", "owner",
                "customer", "total_twd", "total_cny", "qty",
                "pack_cost", "freight", "delivery", "cost",
                "profit", "profit_ratio", "reconcile", "note")
        col_titles = ("编码", "账号", "出货日期", "订单编码", "所属人",
                      "客户", "台币", "人民币", "数量",
                      "包装", "运费", "派送费", "成本",
                      "毛利", "比值", "核销", "备注")
        col_widths = (80, 90, 80, 140, 70, 70, 70, 70, 40,
                      40, 40, 50, 60, 70, 50, 80, 200)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=20,
                                  selectmode="extended")
        for c, t, w in zip(cols, col_titles, col_widths):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # 行内备注：保存 pk 索引（行 iid → pk）
        self._row_pk: Dict[str, str] = {}
        # 備註欄原始值(不含 [Yahoo] / ⚠ 拼接 prefix)— 編輯時用,避免回寫污染 D1
        self._row_orig_note: Dict[str, str] = {}

        # 双击「备注」列弹出编辑框
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # 右键复制菜单（解决编码/订单编码无法选取的问题）
        # 多選時複製所有選中行,單選/沒選中時退回右鍵點擊那行
        self._copy_menu_target_row_id = ""
        self._copy_menu_target_col_idx = -1
        # 右键菜单(动态:每次右键时根据 角色 + 选中行状态 重建作废相关项)
        self._copy_menu = tk.Menu(self.tree, tearoff=0)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        # Ctrl+C 預設複製選中行的訂單編碼(最常用)
        self.tree.bind("<Control-c>", lambda e: self._copy_selected_order_codes())
        self.tree.bind("<Control-C>", lambda e: self._copy_selected_order_codes())
        # Ctrl+A 全選
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)

    def _select_all_rows(self, event=None):
        if not self.tree:
            return "break"
        self.tree.selection_set(self.tree.get_children(""))
        return "break"

    def _on_tree_right_click(self, event) -> None:
        if not self.tree:
            return
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # "#1" "#2" ...
        if not row_id or not col_id:
            return
        try:
            col_idx = int(col_id.lstrip("#")) - 1
        except Exception:
            return
        values = self.tree.item(row_id, "values") or ()
        if col_idx < 0 or col_idx >= len(values):
            return
        self._copy_menu_target_row_id = row_id
        self._copy_menu_target_col_idx = col_idx
        # 右鍵點擊的行不在當前 selection 裡 → 把 selection 設成只含這行(模仿 Excel)
        if row_id not in self.tree.selection():
            self.tree.selection_set(row_id)
        # 重建菜单(根据 角色 + 选中行的作废状态)
        self._rebuild_context_menu()
        try:
            self._copy_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._copy_menu.grab_release()

    def _rebuild_context_menu(self) -> None:
        """根据当前角色 + 选中行的作废状态,动态建右键菜单。"""
        m = self._copy_menu
        m.delete(0, "end")
        m.add_command(label="复制单元格", command=self._copy_cell_value)
        m.add_command(label="复制整行", command=self._copy_row_values)
        m.add_separator()
        m.add_command(label="复制选中订单编码", command=self._copy_selected_order_codes)

        # 选中行作废状态:看第 1 行
        rows = self._get_target_rows()
        if not rows:
            return
        first_note = self._row_orig_note.get(rows[0], "")
        first_state = get_void_status(first_note)

        m.add_separator()
        if self._is_supervisor:
            # 主管视角:可直接作废 / 批准 / 拒绝 / 取消
            if first_state == "none":
                m.add_command(label="✓ 直接作废(主管)",
                              command=lambda: self._click_set_void_state("final"))
            elif first_state == "pending":
                m.add_command(label="✓ 批准作废",
                              command=lambda: self._click_set_void_state("final"))
                m.add_command(label="✗ 拒绝(回普通)",
                              command=lambda: self._click_set_void_state("none"))
            elif first_state == "final":
                m.add_command(label="↺ 取消作废(回普通)",
                              command=lambda: self._click_set_void_state("none"))
        else:
            # 员工视角:只能申请 / 撤回申请,不能直接 final
            if first_state == "none":
                m.add_command(label="📝 申请作废(待主管审核)",
                              command=lambda: self._click_set_void_state("pending"))
            elif first_state == "pending":
                m.add_command(label="↺ 撤回申请",
                              command=lambda: self._click_set_void_state("none"))
            elif first_state == "final":
                m.add_command(label="(已作废 — 仅主管可取消)", state="disabled")

    def _get_target_rows(self) -> List[str]:
        """選中行 fallback 到右鍵那行。"""
        if not self.tree:
            return []
        sel = list(self.tree.selection())
        if sel:
            return sel
        if self._copy_menu_target_row_id:
            return [self._copy_menu_target_row_id]
        return []

    def _copy_cell_value(self) -> None:
        """複製選中所有行的「右鍵點擊那一列」的值。每行一個,換行分隔。"""
        rows = self._get_target_rows()
        col_idx = self._copy_menu_target_col_idx
        if not rows or col_idx < 0:
            return
        values: List[str] = []
        for rid in rows:
            row_values = self.tree.item(rid, "values") or ()
            if 0 <= col_idx < len(row_values):
                values.append(str(row_values[col_idx]))
        if not values:
            return
        text = "\n".join(values)
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
        except Exception:
            pass

    def _copy_row_values(self) -> None:
        """複製選中所有行的整行,行內 tab 分隔,行間換行。"""
        rows = self._get_target_rows()
        if not rows:
            return
        lines: List[str] = []
        for rid in rows:
            values = self.tree.item(rid, "values") or ()
            lines.append("\t".join(str(v) for v in values))
        text = "\n".join(lines)
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
        except Exception:
            pass

    def _copy_selected_order_codes(self) -> None:
        """專門複製選中行的「訂單編碼」欄,每行一個。對應 Ctrl+C 預設動作。"""
        rows = self._get_target_rows()
        if not rows:
            return
        # 在 cols 列表找到 order_code 的索引
        try:
            cols = list(self.tree.cget("columns"))
            oc_idx = cols.index("order_code")
        except Exception:
            return
        codes: List[str] = []
        for rid in rows:
            values = self.tree.item(rid, "values") or ()
            if 0 <= oc_idx < len(values):
                v = str(values[oc_idx]).strip()
                if v:
                    codes.append(v)
        if not codes:
            return
        text = "\n".join(codes)
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
        except Exception:
            pass

    def _click_set_void_state(self, target_status: str) -> None:
        """切换选中行作废状态。target_status ∈ {'none', 'pending', 'final'}。

        权限:
          - 员工只能 pending ↔ none(申请/撤回)
          - 主管可任意切换(直接作废、批准、拒绝、取消作废)
        Worker 端 update_note 也会再做一次 owner_check,client 这层是 UX 提示。
        """
        rows = self._get_target_rows()
        if not rows:
            messagebox.showinfo("提示", "请先选中要操作的行")
            return

        # Client 端权限快检
        if target_status == "final" and not self._is_supervisor:
            messagebox.showerror("权限不足", "只有主管可以最终作废 — 你能做的是「申请作废」")
            return

        # 取出 (pk, note, perf_code) — perf_code 是 tree 第 1 列(用于 final 时反向回滚代付)
        items: List[Tuple[str, str, str]] = []
        for rid in rows:
            pk = self._row_pk.get(rid, "")
            note = self._row_orig_note.get(rid, "")
            values = self.tree.item(rid, "values") or ()
            code = str(values[0]).strip() if values else ""
            if pk:
                items.append((pk, note, code))
        if not items:
            return

        labels = {"final": "作废", "pending": "申请作废", "none": "取消作废/撤回申请"}
        action = labels.get(target_status, target_status)
        # final 时额外提示「会同时回滚代付表」
        confirm_msg = f"确定要对选中的 {len(items)} 笔业绩执行「{action}」吗?"
        if target_status == "final":
            confirm_msg += "\n\n注意:作废会同时清掉代付表上对应的核销记录(E/J/L 清空,M 填「編碼已取消出貨」),并把 D1 代付状态改回未核销。"
        if not messagebox.askyesno("确认", confirm_msg):
            return

        def _run():
            ok_n = 0
            revert_summary = {"sheets": 0, "d1": 0, "matched": 0, "skipped_no_code": 0}
            syb_cancel_n = 0
            syb_cancel_skip = 0  # SYB 找不到/shopName 不對 → 跳過(不算錯誤)
            syb_cancel_err: List[str] = []  # 真正的網路/API 錯誤
            fail_msgs: List[str] = []
            # v6.0.68 ★:作廢時(pending or final)立刻去 SYB 取消對應紀錄,避免物流出錯貨
            # 用 (code, shopName) 雙重比對防誤殺
            _syb_stoken_cache = [None]  # 單次 _run 共享一個 stoken
            def _ensure_stoken():
                if _syb_stoken_cache[0]:
                    return _syb_stoken_cache[0]
                try:
                    from .syb_http_ops import ensure_stoken as _es
                    _syb_stoken_cache[0] = _es(log=self._log)
                except Exception as e:
                    self._log(f"[作废→SYB] ensure_stoken 失敗: {e},SYB 取消功能停用")
                    _syb_stoken_cache[0] = ""
                return _syb_stoken_cache[0]

            for pk, note, code in items:
                ok, err = set_void_status(pk, target_status, current_note=note)
                if not ok:
                    fail_msgs.append(f"{pk[:30]}: {err[:60]}")
                    continue
                ok_n += 1

                # v6.0.68 ★:作廢狀態變 pending 或 final 都觸發 SYB 取消
                # (operator 點待审作廢 = pending,主管確認 = final)
                if target_status in ("pending", "final"):
                    try:
                        from .syb_http_ops import (
                            query_orders_by_code, cancel_stock,
                        )
                        perf_code, order_code = pk.split("|", 1) if "|" in pk else ("", pk)
                        perf_code = (perf_code or "").strip()
                        order_code = (order_code or "").strip()
                        if not perf_code or not order_code:
                            syb_cancel_skip += 1
                        else:
                            stoken = _ensure_stoken()
                            if not stoken:
                                syb_cancel_skip += 1
                            else:
                                # 找 SYB 上 code=order_code 或 +N 後綴 的記錄
                                from .syb_http_ops import make_dup_code as _mdc
                                candidates = [order_code] + [_mdc(order_code, n) for n in range(1, 10)]
                                items_syb = query_orders_by_code(stoken, candidates, log=self._log)
                                # 優先精確比對 shopName(防誤殺別 perf 的紀錄)
                                target = None
                                for it in items_syb:
                                    if str(it.get("shopName") or "") == perf_code:
                                        target = it
                                        break
                                if not target:
                                    self._log(f"[作废→SYB] {pk}: SYB 找不到 shopName={perf_code} 的紀錄(可能未上傳),跳過")
                                    syb_cancel_skip += 1
                                else:
                                    sid = target.get("id")
                                    syb_code = target.get("code", "")
                                    if cancel_stock(stoken, sid, log=self._log):
                                        syb_cancel_n += 1
                                        self._log(f"[作废→SYB] 已取消 SYB id={sid} code={syb_code} ({pk})")
                                    else:
                                        syb_cancel_err.append(f"{pk}: cancel API 回 false")
                    except Exception as e:
                        syb_cancel_err.append(f"{pk[:30]}: {str(e)[:60]}")
                        self._log(f"[作废→SYB] {pk}: 取消失敗 {e}(D1 已作廢,SYB 需手動處理)")

                # final → 反向回滚代付表
                if target_status == "final":
                    if not code:
                        revert_summary["skipped_no_code"] += 1
                        continue
                    try:
                        rr = revert_void_in_payment_sheet(code, log=self._log)
                        if rr.get("ok"):
                            revert_summary["sheets"] += rr.get("sheets_reverted", 0)
                            revert_summary["d1"] += rr.get("d1_marked", 0)
                            revert_summary["matched"] += rr.get("matches", 0)
                        else:
                            fail_msgs.append(f"{code} 代付回滚:{rr.get('error', '')[:60]}")
                    except Exception as e:
                        fail_msgs.append(f"{code} 代付回滚异常:{str(e)[:60]}")

            def _done():
                summary_lines = [f"{action}完成 — 业绩成功 {ok_n}/{len(items)}"]
                if target_status in ("pending", "final"):
                    syb_line = f"SYB 取消:{syb_cancel_n} 條成功"
                    if syb_cancel_skip:
                        syb_line += f" / {syb_cancel_skip} 跳過(無對應紀錄)"
                    if syb_cancel_err:
                        syb_line += f" / {len(syb_cancel_err)} 失敗"
                    summary_lines.append(syb_line)
                if target_status == "final":
                    summary_lines.append(
                        f"代付回滚:Sheets {revert_summary['sheets']} 行 / "
                        f"D1 {revert_summary['d1']} 条 / 匹配 {revert_summary['matched']} 条"
                    )
                if fail_msgs or syb_cancel_err:
                    if syb_cancel_err:
                        fail_msgs.extend([f"SYB:{e}" for e in syb_cancel_err[:3]])
                if fail_msgs:
                    summary_lines.append("失败:")
                    summary_lines.extend(fail_msgs[:5])
                    if len(fail_msgs) > 5:
                        summary_lines.append(f"... 共 {len(fail_msgs)} 个失败")
                    messagebox.showwarning(action, "\n".join(summary_lines))
                else:
                    self._log(f"[业绩] {' | '.join(summary_lines)}")
                try:
                    self._click_refresh()
                except Exception:
                    pass
            try:
                self.frame.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _click_sync_weights(self) -> None:
        """主管:同步 SYB 重量 → 永久寫回 D1(admin_replace)→ 刷新顯示。

        已 push 過的不重推(節省工程量)。weight 變了會自動 reset 重推。
        """
        if getattr(self, "_weights_syncing", False):
            messagebox.showinfo("提示", "正在同步重量中…")
            return
        self._weights_syncing = True
        self.var_status.set("同步重量中…")

        def _runner():
            try:
                n_sync = sync_syb_weights(log=self.log)
                n_push = push_weights_to_d1(log=self.log)
                _save_last_auto_sync(time.time())  # 標記跑過,12h 內 startup 不再跑
                if n_sync >= 0:
                    self.var_status.set(f"重量同步:新處理 {n_sync},推 D1 {n_push} 條")
                    self.frame.after(0, self._click_refresh)
                else:
                    self.var_status.set("重量同步失敗(看日誌)")
            except Exception as e:
                self.var_status.set(f"重量同步異常:{e}")
                self.log(f"[SYB-W] 同步異常:{e}")
            finally:
                self._weights_syncing = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_run_perf_verify(self) -> None:
        """主管:手動觸發 Yahoo 訂單核對(出貨 30 天前的業績)。

        對 PC 上所有 profile 跑(各帳號 cookie 從 cookie_cache 拿,不開瀏覽器)。
        結果存本地 sqlite,完成後自動刷新顯示。
        """
        if getattr(self, "_perf_verify_running", False):
            messagebox.showinfo("提示", "正在 Yahoo 核對中…")
            return
        self._perf_verify_running = True
        self.var_status.set("Yahoo 核對中…")

        def _runner():
            try:
                from .perf_verify_engine import run_verify_all_profiles, _save_last_auto_verify
                result = run_verify_all_profiles(log=self.log)
                _save_last_auto_verify(time.time())  # 12h 內 startup 不再跑
                msg = (f"Yahoo 核對完成:跑 {result.get('profiles', 0)} 個帳號,"
                       f"驗 {result.get('verified', 0)} 筆,異常 {result.get('invalid', 0)} 筆")
                self.var_status.set(msg)
                self.frame.after(0, self._click_refresh)
            except Exception as e:
                self.var_status.set(f"Yahoo 核對異常:{e}")
                self.log(f"[VERIFY] 核對異常:{e}")
            finally:
                self._perf_verify_running = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_export(self) -> None:
        """导出当前筛选条件下的业绩到 xlsx。
        员工：只能导自己的；主管：按当前 owner 筛选（(全部) 则全员）。
        """
        month = self.var_month.get().strip()
        if not re.match(r"\d{4}-\d{1,2}", month):
            messagebox.showerror("错误", "月份格式 YYYY-MM")
            return
        y, m = month.split("-")
        from_date = f"{y}-{int(m):02d}-01"
        to_date = f"{int(y)+1}-01-01" if int(m) == 12 else f"{y}-{int(m)+1:02d}-01"

        # owner 来源
        if not self._is_supervisor:
            owner = self._my_owner
            owner_label = owner or "self"
        else:
            sel = self.var_owner_filter.get()
            owner = "" if sel == "(全部)" else sel
            owner_label = sel if sel != "(全部)" else "全部"

        # 默认文件名
        default_name = f"业绩_{owner_label}_{month}.xlsx"
        path = filedialog.asksaveasfilename(
            title="导出业绩到 Excel",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return

        def _runner():
            try:
                rows = query_records(owner=owner, from_date=from_date, to_date=to_date, limit=5000)
                if not rows:
                    self.frame.after(0, lambda: messagebox.showinfo("提示", "当前筛选没有记录"))
                    return
                # SYB 真實貨物數覆蓋:導出前 patch qty/freight/profit
                try:
                    apply_weights_to_rows(rows)
                except Exception:
                    pass
                # Yahoo 核對:導出前標記異常訂單
                try:
                    from .perf_verify_engine import apply_verify_to_rows
                    apply_verify_to_rows(rows)
                except Exception:
                    pass
                export_records_to_xlsx(rows, Path(path), month=month, owner_label=owner_label)
                self.log(f"[业绩] 导出 {len(rows)} 条 → {path}")
                self.frame.after(0, lambda: messagebox.showinfo(
                    "完成", f"已导出 {len(rows)} 条到\n{path}"))
            except Exception as e:
                self.log(f"[业绩] 导出失败：{e}")
                self.frame.after(0, lambda: messagebox.showerror("错误", f"导出失败：{e}"))

        threading.Thread(target=_runner, daemon=True).start()

    # ── 操作 ──
    def _click_sync(self) -> None:
        if self._sync_running:
            messagebox.showinfo("提示", "正在同步中…")
            return
        self._sync_running = True
        self.var_status.set("同步中…")

        def _runner():
            try:
                ins, skp, rej = sync_shipping_dir(SHIPPING_DIR_DEFAULT, log=self.log)
                msg = f"同步完成：新增 {ins}，已存在 {skp}，拒收 {rej}"
                self.var_status.set(msg)
                self.log(msg)
                self.frame.after(0, self._click_refresh)
            except Exception as e:
                self.var_status.set(f"同步失败：{e}")
                self.log(f"同步异常：{e}")
            finally:
                self._sync_running = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_refresh(self) -> None:
        month = self.var_month.get().strip()
        if not re.match(r"\d{4}-\d{1,2}", month):
            messagebox.showerror("错误", "月份格式 YYYY-MM")
            return
        y, m = month.split("-")
        from_date = f"{y}-{int(m):02d}-01"
        # 月末
        if int(m) == 12:
            to_date = f"{int(y)+1}-01-01"
        else:
            to_date = f"{y}-{int(m)+1:02d}-01"

        owner_sel = self.var_owner_filter.get()
        if not self._is_supervisor:
            owner = self._my_owner
        elif owner_sel == "(全部)":
            owner = ""
        else:
            owner = owner_sel

        def _runner():
            try:
                rows = query_records(owner=owner, from_date=from_date, to_date=to_date)
                # SYB 真實貨物數覆蓋:render 前用本地緩存 patch qty/freight/profit
                try:
                    n = apply_weights_to_rows(rows)
                    if n:
                        self.log(f"[业绩] 用 SYB 重量校正了 {n} 條記錄的數量/運費/利潤")
                except Exception as _e:
                    self.log(f"[业绩] 重量校正失敗(忽略,不影響顯示):{_e}")
                # Yahoo 訂單核對:標記異常訂單(已退款/已取消等)
                try:
                    from .perf_verify_engine import apply_verify_to_rows
                    n_v = apply_verify_to_rows(rows)
                    if n_v:
                        self.log(f"[业绩] Yahoo 核對:{n_v} 筆訂單標記為異常(已退款/取消等)")
                except Exception as _e:
                    self.log(f"[业绩] Yahoo 核對 patch 失敗(忽略):{_e}")
                self.frame.after(0, lambda: self._render_rows(rows))
                summary_rows = query_summary(owner=owner, from_date=from_date, to_date=to_date)
                self.frame.after(0, lambda: self._render_summary(summary_rows))
            except Exception as e:
                self.frame.after(0, lambda: messagebox.showerror("错误", f"查询失败：{e}"))
            # 顺便刷新月份下拉（不阻塞主查询）
            self._refresh_month_values()
            # v6.0.60: 主管侧顺便刷新所属人下拉(替代以前的「刷新所属人」按钮)
            if self._is_supervisor:
                try:
                    owners = query_owners()
                    values = ["(全部)"] + (owners or [])
                    self.frame.after(0, lambda: self.cmb_owner.configure(values=values))
                except Exception:
                    pass

        threading.Thread(target=_runner, daemon=True).start()

    def _click_import(self) -> None:
        """让用户选 1 个或多个 出货资料.xlsx 文件，解析 + 上传 D1。
        员工只能导自己 owner 的（Worker 端会拒收别人 owner）；主管不限。
        """
        if self._sync_running:
            messagebox.showinfo("提示", "正在同步中…")
            return
        files = filedialog.askopenfilenames(
            title="选择出货资料 Excel（可多选）",
            initialdir=str(SHIPPING_DIR_DEFAULT) if SHIPPING_DIR_DEFAULT.exists() else "",
            filetypes=[("Excel", "*.xlsx"), ("All", "*.*")],
        )
        if not files:
            return
        self._sync_running = True
        self.var_status.set(f"导入 {len(files)} 个文件中…")

        def _runner():
            try:
                total_recs = 0
                total_ins = total_skp = total_rej = 0
                for fp in files:
                    try:
                        recs = parse_shipping_excel(Path(fp), log=self.log)
                        if not recs:
                            self.log(f"[业绩] 导入 {Path(fp).name}：无可解析记录")
                            continue
                        total_recs += len(recs)
                        ins, skp, rej = upload_records(recs, log=self.log)
                        total_ins += ins
                        total_skp += skp
                        total_rej += rej
                        self.log(f"[业绩] 导入 {Path(fp).name}：解析 {len(recs)} 条 → 新增 {ins}, 已存在 {skp}, 拒收 {rej}")
                        # v6.0.51: D1 業績上傳成功後,自動把 Excel 原檔同步到阿里雲
                        # 失敗不影響(內部已 try/except),user 不察覺
                        if ins > 0 or skp > 0:
                            try:
                                upload_excel_to_aliyun(Path(fp), log=self.log)
                            except Exception as _ee:
                                self.log(f"[业绩] 阿里雲同步失敗(不影響業績):{_ee}")
                    except Exception as e:
                        self.log(f"[业绩] 导入 {Path(fp).name} 异常：{e}")
                msg = f"导入完成：解析 {total_recs}，新增 {total_ins}，已存在 {total_skp}，拒收 {total_rej}"
                self.var_status.set(msg)
                if total_rej > 0:
                    self.frame.after(0, lambda: messagebox.showwarning(
                        "部分拒收", f"{total_rej} 条因 owner 不匹配被服务器拒收\n（员工只能上传自己 owner 的数据）"
                    ))
                self.frame.after(0, self._click_refresh)
            except Exception as e:
                self.var_status.set(f"导入失败：{e}")
                self.log(f"[业绩] 导入异常：{e}")
            finally:
                self._sync_running = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_download_monthly_merge(self) -> None:
        """主管專屬:下載月匯總(出貨原檔 4 sheet)— v6.0.52。

        對齊 user 過去手動習慣:E:\\YYYY年M月出货资料.xlsx 一檔含 4 個 sheet。
        """
        if not self._is_supervisor:
            return
        win = tk.Toplevel(self.frame)
        win.title("下載月匯總(出貨原檔 4 sheet)")
        win.geometry("680x560")

        # 月份選擇
        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="月份:").pack(side="left", padx=(0, 4))
        var_ym = tk.StringVar(value=time.strftime("%Y-%m"))
        # < > 切換
        def _shift(delta):
            try:
                y, m = var_ym.get().split("-")
                y, m = int(y), int(m)
                m += delta
                if m > 12: y, m = y+1, 1
                if m < 1: y, m = y-1, 12
                var_ym.set(f"{y}-{m:02d}")
                _refresh_owners()
            except Exception:
                pass
        ttk.Button(top, text="<", width=2, command=lambda: _shift(-1)).pack(side="left")
        ttk.Entry(top, textvariable=var_ym, width=10, state="readonly").pack(side="left", padx=2)
        ttk.Button(top, text=">", width=2, command=lambda: _shift(1)).pack(side="left")
        ttk.Label(top, text="  截至:").pack(side="left", padx=(15, 4))
        var_until = tk.StringVar(value="(整月)")
        ttk.Entry(top, textvariable=var_until, width=12).pack(side="left")
        ttk.Label(top, text="(YYYYMMDD,留 (整月) 代表完整一個月)",
                  foreground="#888").pack(side="left", padx=(4, 0))

        # 存到
        save_row = ttk.Frame(win)
        save_row.pack(fill="x", padx=10, pady=4)
        ttk.Label(save_row, text="存到:").pack(side="left", padx=(0, 4))
        def _default_save():
            y, m = var_ym.get().split("-")
            return rf"E:\{int(y)}年{int(m)}月出货资料.xlsx"
        var_save = tk.StringVar(value=_default_save())
        ttk.Entry(save_row, textvariable=var_save).pack(side="left", fill="x", expand=True, padx=4)
        def _browse():
            p = filedialog.asksaveasfilename(
                parent=win, title="存到",
                defaultextension=".xlsx",
                filetypes=[("Excel", "*.xlsx")],
                initialfile=Path(var_save.get()).name,
                initialdir=str(Path(var_save.get()).parent),
            )
            if p:
                var_save.set(p)
        ttk.Button(save_row, text="瀏覽...", command=_browse, width=8).pack(side="left", padx=2)

        # owner 列表(checkbox)
        owner_frame = ttk.Labelframe(win, text="來源 owner(勾選要納入的)")
        owner_frame.pack(fill="both", expand=True, padx=10, pady=8)
        owner_canvas = tk.Canvas(owner_frame, height=200)
        owner_scroll = ttk.Scrollbar(owner_frame, orient="vertical", command=owner_canvas.yview)
        owner_canvas.configure(yscrollcommand=owner_scroll.set)
        owner_inner = ttk.Frame(owner_canvas)
        owner_canvas.create_window((0, 0), window=owner_inner, anchor="nw")
        owner_canvas.pack(side="left", fill="both", expand=True)
        owner_scroll.pack(side="right", fill="y")
        owner_inner.bind("<Configure>",
                          lambda e: owner_canvas.configure(scrollregion=owner_canvas.bbox("all")))
        owner_vars: Dict[str, tk.BooleanVar] = {}

        # 進度顯示
        var_progress = tk.StringVar(value="點「開始」載入並合併")
        progress_label = ttk.Label(win, textvariable=var_progress, foreground="#666")
        progress_label.pack(pady=4)
        from tkinter import ttk as _ttk
        progress_bar = _ttk.Progressbar(win, mode="determinate", length=400)
        progress_bar.pack(pady=2)

        def _refresh_owners():
            for w in owner_inner.winfo_children():
                w.destroy()
            owner_vars.clear()
            var_progress.set("載入 owner 清單...")
            ym = var_ym.get()
            def _load():
                files = list_aliyun_excels(month=ym, log=self.log)
                # 加 legacy(month 空但 filename 屬該月)
                legacy = list_aliyun_excels(log=self.log)
                ym_prefix = ym.replace("-", "")[:6]
                legacy = [f for f in legacy if not f.get("month") and
                          _filename_date(f["filename"]).startswith(ym_prefix)]
                files = files + legacy
                # 按 owner 分組
                by_owner: Dict[str, List[dict]] = {}
                for f in files:
                    by_owner.setdefault(f["owner"], []).append(f)
                # 算未交(該月所有 owner = 全部 owner 的 union)
                all_files_any = list_aliyun_excels(log=self.log)
                all_owners = sorted({f["owner"] for f in all_files_any})

                def _render():
                    for owner in all_owners:
                        files_of = by_owner.get(owner, [])
                        cnt = len(files_of)
                        latest = max((f["mtime"] for f in files_of), default=0)
                        latest_str = (datetime.fromtimestamp(latest).strftime("%m-%d %H:%M")
                                       if latest else "—")
                        var = tk.BooleanVar(value=cnt > 0)
                        owner_vars[owner] = var
                        row = ttk.Frame(owner_inner)
                        row.pack(fill="x", padx=4, pady=1)
                        cb = ttk.Checkbutton(row, text=owner, variable=var)
                        if cnt == 0:
                            cb.state(["disabled"])
                        cb.pack(side="left")
                        info = f"  {cnt} 個檔  最近: {latest_str}"
                        if cnt == 0:
                            info += "  ⚠ 該月未交"
                        ttk.Label(row, text=info, foreground=("#c00" if cnt == 0 else "#666")).pack(side="left")
                    var_progress.set(f"共 {len(all_owners)} 個 owner,{sum(1 for o in all_owners if o in by_owner)} 個有資料")
                self.frame.after(0, _render)
            threading.Thread(target=_load, daemon=True).start()

        # 開始合併
        def _go():
            ym = var_ym.get()
            until = var_until.get().strip()
            if until in ("(整月)", "整月", ""):
                until = ""
            elif not until.isdigit() or len(until) != 8:
                messagebox.showerror("錯誤", "截至日期格式應為 YYYYMMDD(8 位數字)", parent=win)
                return
            sel_owners = [o for o, v in owner_vars.items() if v.get()]
            if not sel_owners:
                messagebox.showinfo("提示", "請至少勾選 1 個 owner", parent=win)
                return
            save_path = Path(var_save.get())
            if save_path.exists():
                if not messagebox.askyesno("覆蓋確認",
                    f"{save_path.name} 已存在,要覆蓋嗎?\n(舊檔會自動備份成 .bak_TIMESTAMP.xlsx)",
                    parent=win):
                    return

            def _on_progress(msg, cur, total):
                def _update():
                    var_progress.set(msg)
                    if total > 0:
                        progress_bar["maximum"] = total
                        progress_bar["value"] = cur
                self.frame.after(0, _update)

            def _runner():
                try:
                    r = merge_monthly_shipping_excels(
                        year_month=ym, save_path=save_path,
                        owner_filter=sel_owners, until_date=until,
                        log=self.log, on_progress=_on_progress,
                    )
                    if r.get("ok"):
                        stats = r.get("sheet_stats", {})
                        msg = (f"✅ 已合併 {r.get('source_files')} 個源檔\n"
                                f"→ {r.get('save_path')}\n\n"
                                f"📊 sheet 統計:\n")
                        for sn, n in stats.items():
                            msg += f"  {sn}: {n} 行\n"
                        dropped = r.get("dropped_versions", [])
                        if dropped:
                            msg += f"\n🔄 同日多版本(取最新),廢棄 {len(dropped)} 個舊版\n"
                            for d in dropped[:5]:
                                msg += f"  • {d}\n"
                            if len(dropped) > 5:
                                msg += f"  ... 等共 {len(dropped)} 個\n"
                        und = r.get("undelivered_owners", [])
                        if und:
                            msg += f"\n⚠️ 該月未交:{', '.join(und)}\n"
                        self.frame.after(0, lambda: messagebox.showinfo("完成", msg, parent=win))
                        self.frame.after(0, lambda: var_progress.set(f"完成 → {save_path.name}"))
                    else:
                        self.frame.after(0, lambda: messagebox.showerror(
                            "失敗", f"合併失敗:{r.get('error', '未知')}", parent=win))
                except Exception as e:
                    err = str(e)[:300]
                    self.frame.after(0, lambda: messagebox.showerror(
                        "錯誤", f"合併異常:{err}", parent=win))
                    self.log(f"[月匯總] 異常:{e}")
            threading.Thread(target=_runner, daemon=True).start()

        # 月份變化重整存檔路徑
        def _on_ym_change(*_):
            try:
                var_save.set(_default_save())
            except Exception:
                pass
        var_ym.trace_add("write", _on_ym_change)

        # 底部按鈕
        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text="重新整理", command=_refresh_owners).pack(side="left", padx=2)
        ttk.Button(btn_row, text="原檔細項", command=lambda: (win.destroy(), self._click_download_excel()),
                   width=10).pack(side="left", padx=2)
        ttk.Button(btn_row, text="關閉", command=win.destroy).pack(side="right", padx=2)
        ttk.Button(btn_row, text="開始下載 + 合併", command=_go,
                   style="Accent.TButton" if "Accent.TButton" in self.frame.tk.call("ttk::style", "element", "names") else "TButton"
                   ).pack(side="right", padx=2)

        # 自動載入
        win.after(100, _refresh_owners)

    def _click_download_excel(self) -> None:
        """主管專屬:從阿里雲下載各 user 上傳的出貨資料 Excel(v6.0.51)。"""
        if not self._is_supervisor:
            return
        # 開個 Toplevel 列檔案
        win = tk.Toplevel(self.frame)
        win.title("阿里雲共享 Excel — 出貨資料")
        win.geometry("760x520")

        # 工具列
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=4)
        var_owner_f = tk.StringVar(value="(全部)")
        ttk.Label(bar, text="所屬人:").pack(side="left", padx=(0, 4))
        owner_combo = ttk.Combobox(bar, textvariable=var_owner_f,
                                     values=["(全部)"], width=15, state="readonly")
        owner_combo.pack(side="left", padx=2)
        var_status = tk.StringVar(value="點「重新整理」載入清單")
        ttk.Label(bar, textvariable=var_status, foreground="#666").pack(side="right", padx=8)

        # 檔案 Tree
        tree_frame = ttk.Frame(win)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        cols = ("owner", "filename", "size", "mtime")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                              selectmode="extended")
        tree.grid(row=0, column=0, sticky="nsew")
        tree.heading("owner", text="所屬人")
        tree.heading("filename", text="檔名")
        tree.heading("size", text="大小")
        tree.heading("mtime", text="上傳時間")
        tree.column("owner", width=100)
        tree.column("filename", width=300)
        tree.column("size", width=100, anchor="e")
        tree.column("mtime", width=160)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        # 底部按鈕列
        btnbar = ttk.Frame(win)
        btnbar.pack(fill="x", padx=8, pady=4)

        all_files: List[dict] = []

        def _refresh():
            owner_filter = var_owner_f.get()
            if owner_filter == "(全部)":
                owner_filter = ""
            var_status.set("載入中...")
            tree.delete(*tree.get_children())

            def _load():
                files = list_aliyun_excels(owner=owner_filter, log=self.log)
                # 更新 owner 下拉
                owners = sorted({f["owner"] for f in files})
                self.frame.after(0, lambda: owner_combo.configure(
                    values=["(全部)"] + owners))
                # 渲染 tree
                def _render():
                    nonlocal all_files
                    all_files = files
                    for f in files:
                        size_mb = f["size"] / 1024
                        size_str = f"{size_mb:.1f} KB" if size_mb < 1024 else f"{size_mb/1024:.1f} MB"
                        mtime_str = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
                        tree.insert("", "end", values=(f["owner"], f["filename"], size_str, mtime_str))
                    var_status.set(f"共 {len(files)} 個檔")
                self.frame.after(0, _render)
            threading.Thread(target=_load, daemon=True).start()

        def _download_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "請先選擇要下載的檔(可多選)", parent=win)
                return
            save_dir = filedialog.askdirectory(parent=win, title="選擇儲存資料夾")
            if not save_dir:
                return
            save_dir = Path(save_dir)
            # 從 tree 選中行取 owner+filename
            picks = []
            for iid in sel:
                vals = tree.item(iid, "values")
                if vals:
                    picks.append((vals[0], vals[1]))  # (owner, filename)
            var_status.set(f"下載 {len(picks)} 個檔中...")

            # v6.0.54 修:從 all_files 找 month(雲端 v2 路徑要帶 month 才找得到)
            file_month_map = {(f["owner"], f["filename"]): f.get("month", "") for f in all_files}

            def _do():
                ok_count = 0
                fail_count = 0
                for owner, filename in picks:
                    sub = save_dir / owner
                    sub.mkdir(parents=True, exist_ok=True)
                    mon = file_month_map.get((owner, filename), "")
                    if download_aliyun_excel(owner, filename, sub / filename,
                                              month=mon, log=self.log):
                        ok_count += 1
                    else:
                        fail_count += 1
                self.frame.after(0, lambda: var_status.set(
                    f"下載完成:成功 {ok_count}, 失敗 {fail_count}"))
                if fail_count == 0:
                    self.frame.after(0, lambda: messagebox.showinfo(
                        "完成", f"成功下載 {ok_count} 個到:\n{save_dir}", parent=win))
            threading.Thread(target=_do, daemon=True).start()

        ttk.Button(btnbar, text="重新整理", command=_refresh).pack(side="left", padx=2)
        ttk.Button(btnbar, text="下載選中", command=_download_selected).pack(side="left", padx=2)
        ttk.Button(btnbar, text="關閉", command=win.destroy).pack(side="right", padx=2)

        # 變更 owner filter 時自動重新整理
        owner_combo.bind("<<ComboboxSelected>>", lambda e: _refresh())
        # 開啟時自動載一次
        win.after(100, _refresh)

    def _click_force_resync(self) -> None:
        """主管专属：重解所有出货资料 + INSERT OR REPLACE 覆盖 D1（解决 parser bug 历史脏数据）"""
        if not self._is_supervisor:
            return
        if not messagebox.askyesno("主管修正同步",
                                    "重新解析所有出货资料，强制覆盖 D1。\n"
                                    "用于 parser 修复后修正旧脏数据。\n\n继续吗？"):
            return
        if self._sync_running:
            messagebox.showinfo("提示", "正在同步中…")
            return
        self._sync_running = True
        self.var_status.set("主管修正中…")

        def _runner():
            try:
                n = force_resync_all(SHIPPING_DIR_DEFAULT, log=self.log)
                msg = f"主管修正完成：覆盖 {n} 条"
                self.var_status.set(msg)
                self.log(msg)
                self.frame.after(0, self._click_refresh)
            except Exception as e:
                self.var_status.set(f"修正失败：{e}")
                self.log(f"主管修正异常：{e}")
            finally:
                self._sync_running = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_sync_payments(self) -> None:
        """主管：一站式 — 拉 Sheets 代付表 → D1 → 跑核销 → 回写 Sheets E 列「已出货」 → 业绩刷新"""
        if not self._is_supervisor:
            return
        if self._sync_running:
            messagebox.showinfo("提示", "正在同步中…")
            return
        self._sync_running = True
        self.var_status.set("同步并核销中…")

        def _runner():
            try:
                res = sync_and_reconcile(log=self.log)
                if res.get("ok"):
                    msg = (f"完成 — 同步 {res.get('synced',0)}，"
                           f"新核销 {res.get('matched',0)}，"
                           f"Sheets 写 {res.get('sheets_written',0)}，"
                           f"D1 标 {res.get('d1_marked',0)}")
                else:
                    msg = f"失败：{res.get('error','')}"
                self.var_status.set(msg)
                self.log(msg)
                self.frame.after(0, self._click_refresh)
            except Exception as e:
                self.var_status.set(f"同步并核销失败：{e}")
                self.log(f"同步并核销异常：{e}")
            finally:
                self._sync_running = False

        threading.Thread(target=_runner, daemon=True).start()

    def _click_refresh_owners(self) -> None:
        if not self._is_supervisor:
            return
        def _runner():
            owners = query_owners()
            values = ["(全部)"] + owners
            self.frame.after(0, lambda: self.cmb_owner.configure(values=values))
        threading.Thread(target=_runner, daemon=True).start()

    def _shift_month(self, delta: int) -> None:
        """当前月 +/- N 个月（delta=+1 next, -1 prev），然后自动刷新"""
        try:
            y, m = self.var_month.get().strip().split("-")
            y, m = int(y), int(m)
        except Exception:
            y, m = int(time.strftime("%Y")), int(time.strftime("%m"))
        m += delta
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        self.var_month.set(f"{y}-{m:02d}")
        self._click_refresh()

    def _click_prev_month(self) -> None:
        self._shift_month(-1)

    def _click_next_month(self) -> None:
        self._shift_month(+1)

    def _refresh_month_values(self) -> None:
        """从 D1 拉所有有业绩的月份，刷新 Combobox 下拉列表"""
        try:
            owner = self._my_owner if not self._is_supervisor else ""
            months = query_months(owner=owner)
            if months:
                # 保留当月（即使没数据也能选）
                cur = time.strftime("%Y-%m")
                if cur not in months:
                    months = [cur] + months
                self.frame.after(0, lambda: self.cmb_month.configure(values=months))
        except Exception:
            pass

    def _render_rows(self, rows: List[dict]) -> None:
        if not self.tree:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._row_pk.clear()
        self._row_orig_note.clear()
        for r in rows:
            recon = r.get("reconcile_status", "—")
            # 備註欄顯示時拼接 verify_status_label(原 r["note"] 不動,update_note 仍寫純 note)
            note_orig = str(r.get("note") or "").strip()
            # 作废识别:note 以 '[作废]' / '[待审作废]' 开头 → 核销列覆盖显示
            void_st = get_void_status(note_orig)
            if void_st == "final":
                recon = "已作废"
            elif void_st == "pending":
                recon = "待审作废"
            verify_lbl = str(r.get("verify_status_label") or "").strip()
            verify_reason = str(r.get("verify_reason") or "").strip()
            if verify_lbl:
                if r.get("verify_invalid"):
                    prefix = f"⚠ {verify_lbl}" + (f":{verify_reason}" if verify_reason else "")
                else:
                    prefix = f"[Yahoo] {verify_lbl}"
                note_display = f"{prefix} | {note_orig}" if note_orig else prefix
            else:
                note_display = note_orig
            iid = self.tree.insert("", "end", values=(
                r.get("code", ""),
                r.get("account", ""),
                r.get("ship_date", ""),
                r.get("order_code", ""),
                r.get("owner", ""),
                r.get("customer", ""),
                f"{r.get('total_twd', 0):.0f}",
                f"{r.get('total_cny', 0):.2f}",
                r.get("qty", 0),
                f"{r.get('pack_cost', 0):.0f}",
                f"{r.get('freight', 0):.0f}",
                f"{r.get('delivery', 0):.0f}",
                f"{r.get('cost', 0):.2f}",
                f"{r.get('profit', 0):.2f}",
                f"{r.get('profit_ratio', 0):.2f}" if r.get("profit_ratio") else "",
                recon,
                note_display,
            ))
            self._row_pk[iid] = r.get("pk") or f"{r.get('code','')}|{r.get('order_code','')}"
            # 記原備註(編輯時用),不含 verify_status_label 拼接 prefix
            self._row_orig_note[iid] = note_orig
            # 染色优先级:已作废 > 待审作废 > Yahoo 核对异常 > 未核销 > 不匹配
            if void_st == "final":
                self.tree.item(iid, tags=("voided",))
            elif void_st == "pending":
                self.tree.item(iid, tags=("void_pending",))
            elif r.get("verify_invalid"):
                self.tree.item(iid, tags=("verify_invalid",))
            elif recon == "未核销":
                self.tree.item(iid, tags=("unreconciled",))
            elif recon.startswith("缺") or recon.startswith("多付") or recon == "金额不匹配":
                self.tree.item(iid, tags=("mismatch",))
        # tag 颜色(首次配置)
        try:
            self.tree.tag_configure("unreconciled", foreground="#c00")
            self.tree.tag_configure("mismatch", foreground="#d80")
            # Yahoo 核對異常:深紅 + 淡紅底,一眼可見「這筆業績無效」
            self.tree.tag_configure("verify_invalid", foreground="#a00", background="#fee")
            # 作废:灰色 + 浅灰背景,视觉上明显「已忽略」
            self.tree.tag_configure("voided", foreground="#888", background="#f0f0f0")
            # 待审作废:橘黄色字 + 浅黄背景,提示主管「有人申请待审」
            self.tree.tag_configure("void_pending", foreground="#b07000", background="#fff5d8")
        except Exception:
            pass

    def _on_tree_double_click(self, event):
        """双击行为：
        - 「备注」列 (#17) → 弹编辑框（仅主管可改）
        - 其它任意列 → 复制单元格到剪贴板（编码/订单编码常用）
        """
        if not self.tree:
            return
        col_id = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id or not col_id:
            return
        # 双击非备注列 → 复制单元格
        if col_id != "#17":
            try:
                col_idx = int(col_id.lstrip("#")) - 1
                values = self.tree.item(row_id, "values") or ()
                if 0 <= col_idx < len(values):
                    val = str(values[col_idx])
                    if val and val != "—":
                        self.frame.clipboard_clear()
                        self.frame.clipboard_append(val)
                        # 状态栏短暂闪一下提示
                        try:
                            self.var_status.set(f"已复制：{val[:50]}")
                            self.frame.after(2000, lambda: self.var_status.set(""))
                        except Exception:
                            pass
            except Exception:
                pass
            return
        # 备注列只有主管能编辑
        if not self._is_supervisor:
            return
        pk = self._row_pk.get(row_id, "")
        if not pk:
            messagebox.showwarning("提示", "无法定位记录 PK")
            return
        cur_values = self.tree.item(row_id, "values")
        # 用原 note(不含 verify_status_label 拼接 prefix)— 防 D1 污染
        cur_note = self._row_orig_note.get(row_id, "")

        # 弹简单输入对话框
        dlg = tk.Toplevel(self.frame)
        dlg.title("编辑备注")
        dlg.transient(self.frame.winfo_toplevel())
        dlg.geometry("500x180")
        ttk.Label(dlg, text=f"PK: {pk}").pack(anchor="w", padx=8, pady=4)
        # 提示有 verify status(顯示用)
        cur_display = cur_values[16] if len(cur_values) > 16 else ""
        if cur_display != cur_note:
            ttk.Label(dlg, text=f"目前顯示:{cur_display}",
                      foreground="#888", font=(self.app._base_family if hasattr(self.app, "_base_family") else "Segoe UI", 9)).pack(anchor="w", padx=8)
        ttk.Label(dlg, text="备注(僅輸入用戶手填部分,自動狀態不必加):").pack(anchor="w", padx=8)
        txt = tk.Text(dlg, height=4, wrap="word")
        txt.pack(fill="both", expand=True, padx=8, pady=4)
        txt.insert("1.0", cur_note)
        txt.focus_set()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=8, pady=4)
        def _save():
            new_note = txt.get("1.0", "end").strip()
            ok, err = update_note(pk, new_note)
            if ok:
                # 更新原 note 記錄 + 重新拼接顯示
                self._row_orig_note[row_id] = new_note
                vlabel = ""
                # 從 tree 顯示推回 verify_status(若有)
                cur_disp = cur_values[16] if len(cur_values) > 16 else ""
                # 顯示用拼接邏輯跟 _render_rows 一致
                if cur_disp.startswith("[Yahoo] ") or cur_disp.startswith("⚠ "):
                    sep_idx = cur_disp.find(" | ")
                    prefix = cur_disp[:sep_idx] if sep_idx > 0 else cur_disp
                    new_display = f"{prefix} | {new_note}" if new_note else prefix
                else:
                    new_display = new_note
                vals = list(cur_values)
                vals[16] = new_display
                self.tree.item(row_id, values=vals)
                dlg.destroy()
            else:
                messagebox.showerror("失败", f"保存失败: {err}")
        ttk.Button(btn_row, text="保存", command=_save).pack(side="right", padx=4)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side="right", padx=4)

    def _render_summary(self, summary_rows: List[dict]) -> None:
        if not summary_rows:
            self.summary_label.configure(text="(无数据)")
            return
        if len(summary_rows) == 1:
            r = summary_rows[0]
            self.summary_label.configure(
                text=f"订单 {r.get('count',0)} 条 / 台币 ¥{r.get('twd_sum',0):.0f}"
                     f" / 人民币 ¥{r.get('cny_sum',0):.0f}"
                     f" / 毛利 ¥{r.get('profit_sum',0):.0f}"
                     f" / 平均利润比 {r.get('profit_ratio_avg',0):.2f}"
            )
        else:
            # 主管多人汇总
            lines = []
            for r in summary_rows:
                lines.append(
                    f"{r.get('owner','?'):<8} 订单 {r.get('count',0):>3} / 台币 ¥{r.get('twd_sum',0):>7.0f}"
                    f" / 毛利 ¥{r.get('profit_sum',0):>7.0f}"
                )
            self.summary_label.configure(text="\n".join(lines))
