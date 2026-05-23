"""業績核對引擎 — 員工 PC 自動掃 Yahoo 訂單,異常標記存本地 sqlite。

業務規則:
  1. 業績匯總 D1 上的訂單,出貨後 30 天內狀態還可能變動(7天退款期 + 收貨後可能爭議)
  2. 出貨 ≥ 30 天的訂單,狀態應該已穩定(已撥款 / 已給評 / 已退款 都定了)
  3. 對這些訂單抓 Yahoo 後台 API,識別「正常 vs 異常」
  4. 異常(已退款/已取消/退款中/爭議)→ 不算業績 → 顯示時 patch qty=0/total_twd=0

設計:
  - 本地 sqlite `output/perf_verify.db` 存核對結果
  - 員工 PC 啟動 90s 後背景跑(12h 節流)
  - 主管 PC 也跑(自己的訂單也要核對)
  - 結果不推 D1(MVP 第一版),顯示時 client 端 patch
  - 認證複用 cookie_store(monitor 已寫,24h 有效,不開瀏覽器)

員工 vs 主管:
  - Yahoo 帳號隔離(每個 ecid 獨立 cookie)
  - 員工 PC 對自己 PC 上所有 profile 跑核對
  - 主管 PC 對自己 PC 上所有 profile 跑核對
  - 不能互看(必要時靠 D1 worker 整合,後續 phase)
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

LogFn = Callable[[str], None]

BASE_DIR = Path(__file__).resolve().parent.parent
_VERIFY_DB = BASE_DIR / "output" / "perf_verify.db"
_LAST_AUTO_VERIFY_FILE = BASE_DIR / "output" / ".perf_verify_last_auto"

# 訂單出貨後 N 天才開始核對(7 天退款期 + 緩衝)
VERIFY_AFTER_DAYS = 30
# 核對範圍上限:出貨 N 天以前的不再核對(已超過 Yahoo 撥款 deadline)
VERIFY_UNTIL_DAYS = 90
# 啟動自動核對節流(秒)
AUTO_VERIFY_THROTTLE = 12 * 3600


def _verify_db():
    _VERIFY_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_VERIFY_DB), timeout=10.0)
    db.execute("""CREATE TABLE IF NOT EXISTS perf_verify (
        order_code TEXT PRIMARY KEY,
        profile_id TEXT,
        yahoo_status TEXT,
        is_valid INTEGER NOT NULL DEFAULT 1,
        abnormal_reason TEXT,
        yahoo_amount INTEGER,
        raw_payment_status TEXT,
        raw_shipping_status TEXT,
        raw_escrow_status_id TEXT,
        raw_escrow_filter_id TEXT,
        verified_at REAL
    )""")
    return db


def _read_last_auto_verify() -> float:
    try:
        return float(_LAST_AUTO_VERIFY_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0.0


def _save_last_auto_verify(ts: float) -> None:
    try:
        _LAST_AUTO_VERIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_AUTO_VERIFY_FILE.write_text(str(ts), encoding="utf-8")
    except Exception:
        pass


def lookup_verify(order_code: str) -> Optional[Dict[str, Any]]:
    """查單筆訂單核對結果。沒記錄返 None。"""
    code = (order_code or "").strip()
    if not code:
        return None
    try:
        db = _verify_db()
        row = db.execute(
            "SELECT yahoo_status, is_valid, abnormal_reason, yahoo_amount, verified_at "
            "FROM perf_verify WHERE order_code = ?", (code,)
        ).fetchone()
        db.close()
    except Exception:
        return None
    if not row:
        return None
    return {
        "yahoo_status": row[0],
        "is_valid": bool(row[1]),
        "abnormal_reason": row[2] or "",
        "yahoo_amount": row[3] or 0,
        "verified_at": row[4] or 0.0,
    }


def apply_verify_to_rows(rows: List[dict], log: LogFn = None) -> int:
    """對 rows in-place 加 verify 獨立欄(不動 r["note"],避免污染 D1)。

    優先從 D1 拉(主管側看員工的核對),fallback 本地 sqlite(離線/D1 失敗)。
    加的欄位:
      - verify_status_label: 中文狀態,例「已撥款」「已退款+待出貨」
      - verify_invalid: True=異常,False=正常
      - verify_reason: 異常原因
      - verify_amount: Yahoo 上的訂單金額
    _render_rows 顯示時拼接到備註欄,update_note 仍寫純 r["note"](D1 乾淨)。

    回傳異常訂單數。
    """
    if not rows:
        return 0
    if log is None:
        log = lambda *_: None

    # 拿所有 order_codes,先試 D1 查(批量),失敗 fallback 本地
    codes = [str(r.get("order_code") or "").strip() for r in rows
             if isinstance(r, dict) and r.get("order_code")]
    codes = [c for c in codes if c]
    if not codes:
        return 0

    verify_map: Dict[str, dict] = {}
    try:
        verify_map = query_verify_from_d1(codes, log=log)
    except Exception as e:
        log(f"[VERIFY] D1 query 失敗,fallback 本地 sqlite:{e}")
        verify_map = {}
    # fallback / 補洞:D1 沒拿到的用本地查
    missing = [c for c in codes if c not in verify_map]
    for c in missing:
        v = lookup_verify(c)
        if v:
            verify_map[c] = {
                "yahoo_status": v["yahoo_status"],
                "is_valid": v["is_valid"],
                "abnormal_reason": v["abnormal_reason"],
                "yahoo_amount": v["yahoo_amount"],
            }

    n_invalid = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        oc = str(r.get("order_code") or "").strip()
        v = verify_map.get(oc)
        if not v:
            continue
        r["verify_status_label"] = v["yahoo_status"]
        r["verify_invalid"] = not v["is_valid"]
        r["verify_reason"] = v["abnormal_reason"]
        r["verify_amount"] = v.get("yahoo_amount", 0)
        if not v["is_valid"]:
            n_invalid += 1
    return n_invalid


def query_verify_from_d1(order_codes: List[str], log: LogFn = None) -> Dict[str, dict]:
    """從 D1 worker 批量拉 verify 結果。

    回傳 {order_code: {yahoo_status, is_valid, abnormal_reason, yahoo_amount}}。
    """
    if log is None:
        log = lambda *_: None
    try:
        from .performance_feature import WORKER_URL, _get_chat_id, _get_my_owner
    except Exception as e:
        log(f"[VERIFY] D1 query: 載入 helper 失敗:{e}")
        return {}
    chat_id = _get_chat_id()
    owner = _get_my_owner()
    if not chat_id:
        return {}
    codes = [str(c).strip() for c in (order_codes or []) if c]
    if not codes:
        return {}

    import requests as _rq
    out: Dict[str, dict] = {}
    # 批量打,一次最多 5000(worker 端限制)
    BATCH = 1000
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            r = _rq.post(f"{WORKER_URL}/api/perf_verify/by_codes",
                         json={"chat_id": chat_id, "owner": owner, "codes": chunk},
                         timeout=30)
            if r.status_code != 200:
                log(f"[VERIFY] D1 query HTTP {r.status_code}")
                continue
            d = r.json()
            if not d.get("ok"):
                log(f"[VERIFY] D1 query 業務失敗:{d.get('error','')}")
                continue
            for row in (d.get("rows") or []):
                oc = str(row.get("order_code") or "").strip()
                if not oc:
                    continue
                out[oc] = {
                    "yahoo_status": row.get("yahoo_status") or "",
                    "is_valid": bool(row.get("is_valid")),
                    "abnormal_reason": row.get("abnormal_reason") or "",
                    "yahoo_amount": int(row.get("yahoo_amount") or 0),
                }
        except Exception as e:
            log(f"[VERIFY] D1 query 異常:{e}")
    return out


def upload_verify_records_to_d1(records: List[dict], log: LogFn = None) -> Tuple[int, int]:
    """把本地 verify 結果推 D1 worker(員工各自推自己 owner 的)。

    records 每筆:{order_code, profile_id, yahoo_status, is_valid, abnormal_reason,
                  yahoo_amount, raw_payment_status, raw_shipping_status,
                  raw_escrow_status_id, raw_escrow_filter_id}
    回傳 (upserted, rejected)。
    """
    if log is None:
        log = lambda *_: None
    if not records:
        return 0, 0
    try:
        from .performance_feature import WORKER_URL, _get_chat_id, _get_my_owner
    except Exception as e:
        log(f"[VERIFY] D1 upload: 載入 helper 失敗:{e}")
        return 0, 0
    chat_id = _get_chat_id()
    owner = _get_my_owner()
    if not chat_id:
        log("[VERIFY] D1 upload: 無 chat_id,跳過")
        return 0, 0

    import requests as _rq
    upserted_total = 0
    rejected_total = 0
    BATCH = 200
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        try:
            r = _rq.post(f"{WORKER_URL}/api/perf_verify/upload",
                         json={"chat_id": chat_id, "owner_check": owner, "records": chunk},
                         timeout=60)
            if r.status_code != 200:
                log(f"[VERIFY] D1 upload HTTP {r.status_code}: {r.text[:200]}")
                rejected_total += len(chunk)
                continue
            d = r.json()
            if not d.get("ok"):
                log(f"[VERIFY] D1 upload 業務失敗:{d.get('error','')}")
                rejected_total += len(chunk)
                continue
            upserted_total += d.get("upserted", 0)
            rejected_total += d.get("rejected", 0)
        except Exception as e:
            log(f"[VERIFY] D1 upload 異常:{e}")
            rejected_total += len(chunk)
    return upserted_total, rejected_total


def get_orders_needing_verify(log: LogFn = None) -> List[dict]:
    """從 D1 拿過去 30~90 天內、自己 owner、還沒 verified 的業績訂單。

    過期(>90 天)或太新(<30 天)不抓。
    返回 [{order_code, owner, ship_date, total_twd, ...}, ...]
    """
    if log is None:
        log = lambda *_: None
    try:
        from .performance_feature import query_records, _get_my_owner
    except Exception as e:
        log(f"[VERIFY] 載入 query_records 失敗:{e}")
        return []

    today = datetime.now()
    from_date = (today - timedelta(days=VERIFY_UNTIL_DAYS)).strftime("%Y-%m-%d")
    to_date = (today - timedelta(days=VERIFY_AFTER_DAYS)).strftime("%Y-%m-%d")
    owner = _get_my_owner()
    log(f"[VERIFY] 拉 D1 業績 ship_date {from_date} ~ {to_date}(出貨 {VERIFY_AFTER_DAYS}-{VERIFY_UNTIL_DAYS} 天前),owner={owner or '全部'}")

    try:
        rows = query_records(owner=owner, from_date=from_date, to_date=to_date, limit=5000)
    except Exception as e:
        log(f"[VERIFY] query_records 失敗:{e}")
        return []
    if not rows:
        log("[VERIFY] D1 無符合條件的業績")
        return []

    # filter:還沒 verified
    try:
        db = _verify_db()
        verified = {row[0] for row in db.execute(
            "SELECT order_code FROM perf_verify WHERE verified_at > 0"
        ).fetchall()}
        db.close()
    except Exception:
        verified = set()

    # 跳过作废 / 待审作废的条目(它们不需要再核对)
    try:
        from .performance_feature import is_voided_note
    except Exception:
        is_voided_note = lambda _n: False
    n_pre = len(rows)
    rows = [r for r in rows if not is_voided_note(r.get("note", ""))]
    n_voided_skipped = n_pre - len(rows)

    targets = [r for r in rows if str(r.get("order_code") or "").strip() not in verified]
    log(f"[VERIFY] 共 {n_pre} 筆,已 verified {n_pre - len(targets) - n_voided_skipped} 筆"
        + (f",作废跳过 {n_voided_skipped} 筆" if n_voided_skipped else "")
        + f",待核對 {len(targets)} 筆")
    return targets


def verify_for_one_profile(
    profile_id: str,
    target_codes: Set[str],
    *,
    log: LogFn = None,
) -> Tuple[int, int]:
    """對一個 Yahoo 帳號 profile 跑核對。

    從 cookie_cache 拿 cookie + wssid → list_orders 抓近 90 天 → 比對 target_codes →
    識別狀態 → upsert sqlite。

    回傳 (核對到的訂單數, 異常訂單數)。
    """
    if log is None:
        log = lambda *_: None
    if not target_codes:
        return 0, 0
    try:
        from .yahoo_order_http_ops import (
            YahooOrderAPI, YahooAuthError, YahooAPIError,
            identify_order_status,
        )
    except Exception as e:
        log(f"[VERIFY] 載入 yahoo_order_http_ops 失敗:{e}")
        return 0, 0

    profile_dir = BASE_DIR / "profiles" / profile_id
    if not profile_dir.exists():
        log(f"[VERIFY] {profile_id}: profile 目錄不存在,跳過")
        return 0, 0

    try:
        api = YahooOrderAPI(profile_dir, log=log)
    except YahooAuthError as e:
        log(f"[VERIFY] {profile_id}: {e}(跳過,等監控刷新 cookie)")
        return 0, 0
    except Exception as e:
        log(f"[VERIFY] {profile_id}: 建立 API 失敗:{e}")
        return 0, 0

    today = datetime.now()
    end_dt = today
    # 抓覆蓋 30~90 天前的訂單,API 用 createTime;業績的 ship_date 略晚於 createTime,留 7 天緩衝
    start_dt = (today - timedelta(days=VERIFY_UNTIL_DAYS + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_iso = start_dt.strftime("%Y-%m-%dT00:00:00Z")
    end_iso = end_dt.strftime("%Y-%m-%dT23:59:59Z")
    time_range = f"{start_dt.strftime('%Y/%m/%d')} ~ {end_dt.strftime('%Y/%m/%d')}"

    # 翻頁拉所有訂單
    all_orders: List[dict] = []
    offset = 0
    page = 1
    while True:
        try:
            payload = api.list_orders(
                start_iso=start_iso, end_iso=end_iso, time_range=time_range,
                limit=50, offset=offset,
            )
        except YahooAuthError as e:
            log(f"[VERIFY] {profile_id} page {page}: auth 失敗 {e},中止此 profile")
            return 0, 0
        except YahooAPIError as e:
            log(f"[VERIFY] {profile_id} page {page}: {e}")
            break
        listings = payload.get("listings") or []
        if not listings:
            break
        all_orders.extend(listings)
        total = payload.get("totalCount") or 0
        if len(all_orders) >= total or len(listings) < 50:
            break
        offset = len(all_orders)
        page += 1
        if page > 30:
            log(f"[VERIFY] {profile_id}: 抓到 page {page} 強制停(>1500 筆,異常)")
            break
        time.sleep(0.3)  # 避免太頻繁

    # 比對 + upsert sqlite + 推 D1
    n_verified = 0
    n_invalid = 0
    pending_d1: List[dict] = []  # 同步 push D1 用
    try:
        db = _verify_db()
        cur = db.cursor()
        ts = time.time()
        for o in all_orders:
            oc = str(o.get("orderId") or "").strip()
            if oc not in target_codes:
                continue
            label, is_valid, reason = identify_order_status(o)
            pay = (o.get("payment") or {})
            ship = (o.get("shipping") or {})
            escrow = (o.get("escrow") or {})
            try:
                yahoo_amt = int(float((o.get("price") or {}).get("orderAmount", 0) or 0))
            except Exception:
                yahoo_amt = 0
            cur.execute(
                "INSERT INTO perf_verify (order_code, profile_id, yahoo_status, is_valid, "
                "  abnormal_reason, yahoo_amount, raw_payment_status, raw_shipping_status, "
                "  raw_escrow_status_id, raw_escrow_filter_id, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(order_code) DO UPDATE SET "
                "  profile_id = excluded.profile_id, "
                "  yahoo_status = excluded.yahoo_status, "
                "  is_valid = excluded.is_valid, "
                "  abnormal_reason = excluded.abnormal_reason, "
                "  yahoo_amount = excluded.yahoo_amount, "
                "  raw_payment_status = excluded.raw_payment_status, "
                "  raw_shipping_status = excluded.raw_shipping_status, "
                "  raw_escrow_status_id = excluded.raw_escrow_status_id, "
                "  raw_escrow_filter_id = excluded.raw_escrow_filter_id, "
                "  verified_at = excluded.verified_at",
                (oc, profile_id, label, 1 if is_valid else 0, reason, yahoo_amt,
                 pay.get("status", ""), ship.get("status", ""),
                 str(escrow.get("statusId", "")), str(escrow.get("filterId", "")),
                 ts),
            )
            pending_d1.append({
                "order_code": oc,
                "profile_id": profile_id,
                "yahoo_status": label,
                "is_valid": is_valid,
                "abnormal_reason": reason,
                "yahoo_amount": yahoo_amt,
                "raw_payment_status": pay.get("status", ""),
                "raw_shipping_status": ship.get("status", ""),
                "raw_escrow_status_id": str(escrow.get("statusId", "")),
                "raw_escrow_filter_id": str(escrow.get("filterId", "")),
            })
            n_verified += 1
            if not is_valid:
                n_invalid += 1
        db.commit()
        db.close()
    except Exception as e:
        log(f"[VERIFY] {profile_id}: sqlite upsert 失敗:{e}")
        return 0, 0

    # 推 D1(主管也能看員工的核對結果)
    if pending_d1:
        try:
            up, rej = upload_verify_records_to_d1(pending_d1, log=log)
            if up:
                log(f"[VERIFY] {profile_id}: D1 推送 {up} 筆,拒收 {rej} 筆")
        except Exception as e:
            log(f"[VERIFY] {profile_id}: D1 推送異常(本地仍存,可重試):{e}")

    if n_verified:
        log(f"[VERIFY] {profile_id}: 核對 {n_verified} 筆,異常 {n_invalid} 筆")
    return n_verified, n_invalid


def run_verify_all_profiles(log: LogFn = None) -> Dict[str, int]:
    """掃 PC 上所有 Yahoo 帳號,對 D1 業績「需要 verify」的訂單跑核對。

    回傳 {profile_id_count, total_verified, total_invalid}。
    """
    if log is None:
        log = lambda *_: None

    targets = get_orders_needing_verify(log=log)
    if not targets:
        return {"profiles": 0, "verified": 0, "invalid": 0}

    target_codes = {str(r.get("order_code") or "").strip() for r in targets if r.get("order_code")}

    try:
        from .accounts import load_accounts
        accounts = load_accounts() or []
    except Exception as e:
        log(f"[VERIFY] 載入 accounts 失敗:{e}")
        return {"profiles": 0, "verified": 0, "invalid": 0}

    n_profiles_skipped = 0
    n_profiles = 0
    total_verified = 0
    total_invalid = 0
    verified_codes_set: Set[str] = set()
    for acc in accounts:
        pid = str(acc.get("profile_id") or "").strip()
        if not pid:
            continue
        nv, ni = verify_for_one_profile(pid, target_codes, log=log)
        if nv > 0:
            n_profiles += 1
            total_verified += nv
            total_invalid += ni
        else:
            n_profiles_skipped += 1
    # 統計沒抓到任何 profile 的訂單(可能 cookie 過期或訂單在別 PC)
    try:
        db_q = _verify_db()
        verified_codes_set = {row[0] for row in db_q.execute(
            "SELECT order_code FROM perf_verify WHERE verified_at > 0"
        ).fetchall()}
        db_q.close()
    except Exception:
        pass
    unmatched = target_codes - verified_codes_set
    if unmatched:
        log(f"[VERIFY] ⚠ {len(unmatched)} 筆訂單核對不到(cookie 過期 / 訂單不在本 PC),"
            f"範例:{list(unmatched)[:3]}")

    log(f"[VERIFY] 完成:跑 {n_profiles} 個 profile(跳過 {n_profiles_skipped}),"
        f"核對 {total_verified} 筆,異常 {total_invalid} 筆")
    return {"profiles": n_profiles, "verified": total_verified, "invalid": total_invalid,
            "skipped_profiles": n_profiles_skipped, "unmatched_codes": len(unmatched)}


def startup_auto_verify(log: LogFn = None, throttle_seconds: int = AUTO_VERIFY_THROTTLE):
    """GUI 啟動時呼叫的自動核對 — 12h 節流,失敗不擋。"""
    if log is None:
        log = lambda *_: None
    try:
        now = time.time()
        last_at = _read_last_auto_verify()
        if now - last_at < throttle_seconds:
            log(f"[VERIFY] {throttle_seconds//3600}h 內已自動核對過(距上次 {int((now-last_at)/60)} 分鐘),跳過")
            return
        run_verify_all_profiles(log=log)
        _save_last_auto_verify(now)
    except Exception as e:
        log(f"[VERIFY] 啟動自動核對異常:{e}")
