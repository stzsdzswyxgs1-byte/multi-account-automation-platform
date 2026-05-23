"""D1 對齊功能(v6.1.25)

把 D1 database 跟奇摩帳號真實上架狀態對齊:
- account 不在 accounts.json → 保留(非奇摩,例 rosa9855)
- account 在 accounts.json + product_code 在 Yahoo 上架 → 保留
- account 在 accounts.json + product_code 已下架 → 刪除

設計重點:
1. **永遠先備份** — 任何 reconcile 動作前先 dump 全量 D1 snapshot 到本機,
   萬一刪錯了可以用 /api/upload-batch 回滾。
2. Dry-run = fetch + 算 stale + save plan,**不刪**。
3. fetch 中途任何 account 失敗 → 整個 account skip(不部分刪),下次再試。
4. 用 cookie cache,不 acquire Profile lock,不打擾 monitor。
5. 並發 5(中等負載,_post_reservice 已內建 429 retry)。
"""
from __future__ import annotations

# v6.1.25:中文路徑 + curl_cffi 修復(跟 app.py 第 6-19 行同邏輯)
# 如果 d1_reconcile.py 被 standalone import(沒走 app.py 入口),env var 沒設
# 會導致 curl_cffi 拿不到 cacert.pem err 77。提前在 import time 設一份。
# Idempotent — 如果 app.py 已設過,setdefault 不會覆蓋。
import os as _os_init
try:
    import certifi as _certifi_init
    _src_cert = _certifi_init.where()
    if _src_cert and any(ord(c) > 127 for c in _src_cert):
        import shutil as _shutil_init, tempfile as _tempfile_init
        _dst_cert = _os_init.path.join(
            _tempfile_init.gettempdir(), "cacert_ascii.pem",
        )
        if not _os_init.path.exists(_dst_cert):
            _shutil_init.copy(_src_cert, _dst_cert)
        _os_init.environ.setdefault("SSL_CERT_FILE", _dst_cert)
        _os_init.environ.setdefault("CURL_CA_BUNDLE", _dst_cert)
        _os_init.environ.setdefault("REQUESTS_CA_BUNDLE", _dst_cert)
except Exception:
    pass

import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests as _requests


# ── D1 Worker 配置(跟 doc_upload_feature.py / merch_http_ops.py 一致)──
_D1_WORKER_URL = "https://product-query.<PHONE_REDACTED>.workers.dev"
_D1_TOKEN = "<D1_UPLOAD_TOKEN_REDACTED>"

# ── Yahoo listing fetch 配置 ──
_YAHOO_PAGE_SIZE = 40          # FETCH_MERCHANDISE_LIST 上限
_DEFAULT_CONCURRENCY = 5       # 並發帳號數(调研结论)
_INTER_PAGE_DELAY_MS = 250     # 同帳號連續分頁間 delay,降低 burst

LogFn = Callable[[str], None]


# ─────────────────────────────────────────────────────────
# 數據結構
# ─────────────────────────────────────────────────────────

@dataclass
class ReconcileResult:
    """對齊結果統計。"""
    total_d1_records: int = 0
    kept_non_yahoo: int = 0           # account 不在 accounts.json 保留的條數
    accounts_checked: int = 0          # 真正去 Yahoo 查過的帳號數
    accounts_skipped: int = 0          # fetch 失敗 / 無 cookie / profile 不存在 跳過的帳號數
    skipped_account_names: List[str] = field(default_factory=list)
    per_account_summary: List[Dict[str, Any]] = field(default_factory=list)
    stale_codes_total: int = 0         # 算出的 stale product_code 數
    deleted_count: int = 0             # 真實刪除數(dry-run = 0)
    backup_path: str = ""              # 備份檔案路徑
    plan_path: str = ""                # 刪除 plan 路徑
    missing_plan_path: str = ""        # v6.1.25: Yahoo 有 D1 沒的清單 xlsx 路徑
    dry_run: bool = True
    elapsed_sec: float = 0.0
    # v6.1.25:給 UI dialog 用的詳細資料(stale records + missing records)
    stale_records: List[Dict[str, str]] = field(default_factory=list)
    stale_codes: List[str] = field(default_factory=list)
    missing_records: List[Dict[str, str]] = field(default_factory=list)
    missing_total: int = 0
    owner: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────
# D1 全量拉取(分頁)+ 備份
# ─────────────────────────────────────────────────────────

def fetch_all_d1_records(
    owner: str, *, log: Optional[LogFn] = None,
    page_size: int = 5000,
) -> List[Dict[str, str]]:
    """分頁拉 D1 owner 全部 records。

    回傳每條:`{"barcode": ..., "product_code": ..., "account": ...}`
    """
    all_records: List[Dict[str, str]] = []
    offset = 0
    while True:
        url = (
            f"{_D1_WORKER_URL}/api/barcodes"
            f"?owner={owner}&limit={page_size}&offset={offset}"
        )
        try:
            r = _requests.get(
                url,
                headers={"User-Agent": "curl/8.0.1"},  # CF bot block bypass
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if log:
                log(f"[D1對齊] 拉 D1 分頁失敗 offset={offset}: {e}")
            raise
        rows = data.get("data") or []
        if not rows:
            break
        all_records.extend(rows)
        if log and (offset == 0 or offset % 25000 == 0):
            total = data.get("total", "?")
            log(f"[D1對齊]   拉 D1 進度 {len(all_records)}/{total}")
        if len(rows) < page_size:
            break
        offset += page_size
    return all_records


def _write_records_xlsx(records: List[Dict[str, str]], path: Path) -> None:
    """寫成 Excel,欄位 商品條碼 / 商品編號 / 帳號 — 跟既有上傳格式相容。

    用 write_only mode 對 160k+ 行也能很快寫完(~10-20 秒)。
    用戶要恢復時直接到「編碼數據更新」tab → 選擇文件 → 開始上傳(追加更新)即可。
    """
    import openpyxl
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("data")
    ws.append(["商品條碼", "商品編號", "帳號"])
    for r in records:
        ws.append([
            r.get("barcode", "") or "",
            r.get("product_code", "") or "",
            r.get("account", "") or "",
        ])
    wb.save(str(path))


def _write_readme(backup_root: Path, owner: str, record_count: int, stale_count: int,
                   dry_run: bool, missing_count: int = 0) -> None:
    """寫人類可讀的恢復說明 README.txt(中文)。"""
    missing_section = ""
    if missing_count > 0:
        missing_section = f"""
missing_plan.xlsx — Yahoo 有 D1 沒的清單(共 {missing_count} 條)
─────────────────────────────────────────
這些商品 Yahoo 帳號上有在上架,但 D1 沒對應記錄。軟件無法自動補,
因為 D1 一條 record 需要「barcode」(閒魚/煤炉 URL),Yahoo API 拿不到。

如要補進 D1:
1. 打開 missing_plan.xlsx
2. barcode 欄留空,你逐條補上對應的閒魚/煤炉 URL
3. 存檔後 → 軟件「上傳編碼數據」→ 選此檔案 → 追加更新
"""
    content = f"""D1 對齊備份目錄
═══════════════════════════════════════════

時間戳    : {backup_root.name}
擁有者    : {owner}
全量記錄  : {record_count} 條(對齊前 D1 全量)
要刪/已刪 : {stale_count} 條(stale = D1 有但 Yahoo 已下架)
Yahoo 有 D1 沒: {missing_count} 條(missing = 需手動補上傳)
模式      : {'預覽模式(只算不刪,沒有真實刪除)' if dry_run else '實際執行(已刪除)'}

檔案列表
─────────────────────────────────────────
• d1_backup.xlsx    — D1 全量備份(對齊前所有 records)
                      ↑ 「全量回滾」用,慎用,會重複上傳所有資料
• delete_plan.xlsx  — 只列要刪/已刪的 records(stale)
                      ↑ 「精準回滾」用,只把刪掉的 records 重新上傳
{("• missing_plan.xlsx — Yahoo 有 D1 沒的清單(barcode 欄留空,給你填)" + chr(10)) if missing_count > 0 else ""}• metadata.json     — 元資料(per-account 統計)
• README.txt        — 本檔案

如何恢復刪掉的 stale(推薦做法 — 用 delete_plan.xlsx)
─────────────────────────────────────────
1. 打開軟件 → 切到「編碼數據更新」tab
2. 上方「上傳編碼數據」區塊 → 點「選擇文件」
3. 選 delete_plan.xlsx
4. 模式選「追加更新」(預設)
5. 點「開始上傳」

→ 剛剛被刪掉的 {stale_count} 條 records 會回到 D1。

⚠️ 不要選「覆蓋更新」!那會把你 D1 全部清掉再上傳,
   等於只剩 stale 那些 records,其他所有資料會丟。
{missing_section}
如要全量回滾(不推薦)
─────────────────────────────────────────
用 d1_backup.xlsx + 「覆蓋更新」模式上傳 →
D1 會完全變回對齊前的狀態(包含 stale)。

僅在執行完對齊後完全反悔 + 不想留任何痕跡時使用。
"""
    (backup_root / "README.txt").write_text(content, encoding="utf-8")


def save_d1_backup(
    owner: str, records: List[Dict[str, str]],
    base_dir: Path, *, log: Optional[LogFn] = None,
) -> Path:
    """把當前 D1 snapshot 存到本機,作為 reconcile 前備份。

    產出檔案(`{base_dir}/runtime/d1_backups/reconcile_{YYYYMMDD_HHMMSS}/`):
    - `d1_backup.xlsx`  全量 D1 snapshot(可直接走「上傳編碼數據」恢復)
    - `metadata.json`   元資料
    - `README.txt`      人類可讀恢復說明

    delete_plan.xlsx 由 save_delete_plan 補寫(後面才知道 stale 是哪些)。

    為什麼用 Excel 不用 JSON:
    - 跟既有「編碼數據更新」上傳功能直接相容,點兩下就能恢復
    - JSON 用戶看不懂 + 沒法直接 POST API
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_root = Path(base_dir) / "runtime" / "d1_backups" / f"reconcile_{ts}"
    backup_root.mkdir(parents=True, exist_ok=True)

    # 1. Excel 全量備份
    xlsx_path = backup_root / "d1_backup.xlsx"
    if log:
        log(f"[D1對齊]   寫全量備份 xlsx ({len(records)} 條)...")
    _write_records_xlsx(records, xlsx_path)

    # 2. metadata 元資料(輕量,給程式讀)
    meta_path = backup_root / "metadata.json"
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump({
            "owner": owner,
            "timestamp": int(time.time()),
            "ts_human": ts,
            "full_record_count": len(records),
        }, f, ensure_ascii=False, indent=2)

    if log:
        size_mb = xlsx_path.stat().st_size / 1024 / 1024
        log(
            f"[D1對齊] ✅ 備份 D1 → {xlsx_path} "
            f"({len(records)} 條 / {size_mb:.1f} MB)"
        )
    return backup_root


def save_delete_plan(
    backup_root: Path, stale_records: List[Dict[str, str]],
    per_account_summary: List[Dict[str, Any]],
    owner: str, dry_run: bool, full_record_count: int, *,
    missing_records: Optional[List[Dict[str, str]]] = None,
    log: Optional[LogFn] = None,
) -> Tuple[Path, Optional[Path]]:
    """把要刪掉的 records 存到備份目錄。

    產出:
    - delete_plan.xlsx: 跟上傳格式相容,用戶可直接「上傳編碼數據 → 追加更新」恢復
    - missing_plan.xlsx (v6.1.25): Yahoo 有 D1 沒的清單,barcode 欄留空給用戶填
    - delete_plan_summary.json: per-account 統計(audit 用)
    - README.txt: 中文恢復步驟說明

    Returns: (delete_plan_xlsx_path, missing_plan_xlsx_path or None)
    """
    # 1. delete_plan.xlsx(可直接 upload-batch 恢復)
    xlsx_path = backup_root / "delete_plan.xlsx"
    _write_records_xlsx(stale_records, xlsx_path)

    # 2. missing_plan.xlsx — v6.1.25:Yahoo 有 D1 沒的清單,barcode 留空給用戶填
    missing_xlsx_path = None
    if missing_records:
        missing_xlsx_path = backup_root / "missing_plan.xlsx"
        # barcode 欄留空(因為 Yahoo 沒提供),用戶填好後可直接上傳
        _write_records_xlsx(missing_records, missing_xlsx_path)

    # 3. per-account 統計
    summary_path = backup_root / "delete_plan_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump({
            "timestamp": int(time.time()),
            "stale_count": len(stale_records),
            "missing_count": len(missing_records or []),
            "per_account_summary": per_account_summary,
        }, f, ensure_ascii=False, indent=2)

    # 4. README.txt
    _write_readme(
        backup_root, owner,
        record_count=full_record_count,
        stale_count=len(stale_records),
        dry_run=dry_run,
        missing_count=len(missing_records or []),
    )

    if log:
        log(
            f"[D1對齊] ✅ 寫 delete_plan → {xlsx_path.name} "
            f"({len(stale_records)} 條 stale)"
        )
        if missing_xlsx_path:
            log(
                f"[D1對齊] ✅ 寫 missing_plan → {missing_xlsx_path.name} "
                f"({len(missing_records)} 條 Yahoo 有 D1 沒,barcode 欄留空)"
            )
        log(f"[D1對齊] ✅ 寫 README → {backup_root / 'README.txt'}")
    return xlsx_path, missing_xlsx_path


def _legacy_save_delete_plan(
    backup_root: Path, stale_records: List[Dict[str, str]],
    per_account_summary: List[Dict[str, Any]], *,
    log: Optional[LogFn] = None,
) -> Path:
    """deprecated:舊 JSON-only 版本,留著免得有人 import."""
    plan_path = backup_root / "delete_plan.json"
    payload = {
        "timestamp": int(time.time()),
        "stale_count": len(stale_records),
        "per_account_summary": per_account_summary,
        "stale_records": stale_records,
    }
    with plan_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if log:
        log(f"[D1對齊] ✅ 寫 delete_plan → {plan_path}")
    return plan_path


# ─────────────────────────────────────────────────────────
# Yahoo on-shelf product_codes 拉取(per profile)
# ─────────────────────────────────────────────────────────

def fetch_all_onshelf_codes(
    profile_dir: Path, *, log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
) -> Optional[Set[str]]:
    """同步函數:用 cookie cache + HTTP 拉該 profile 所有 on-shelf 的 product_codes。

    回傳:
    - 成功:`set` of product_codes(可能為空 set = 真實 0 件上架)
    - 失敗(cookie 過期 / 網路斷 / Profile 不存在):`None`(調用方應 skip 此 account)

    為什麼 None != empty set:**很重要的區別**
    - empty set:Yahoo 真實回 0 件,D1 該帳號所有 records 都是 stale
    - None:fetch 失敗,**絕對不能** 把 D1 該帳號所有 records 當 stale 刪掉
    """
    from .merch_http_ops import (
        AuthExpiredError, _try_cached_session,
        _update_seller_timestamp, fetch_merchandise_list,
    )

    if not profile_dir.exists():
        if log:
            log(f"[D1對齊] {profile_dir.name}: profile_dir 不存在,skip")
        return None

    try:
        session = _try_cached_session(profile_dir, proxy="", log=None)
    except Exception as e:
        if log:
            log(f"[D1對齊] {profile_dir.name}: cookie cache 異常: {e}")
        return None
    if session is None:
        if log:
            log(f"[D1對齊] {profile_dir.name}: 無 cookie cache,skip")
        return None
    if not session.is_valid:
        if log:
            log(f"[D1對齊] {profile_dir.name}: cookies/wssid 失效,skip")
        return None

    try:
        _update_seller_timestamp(session)
    except Exception:
        pass

    onshelf: Set[str] = set()
    offset = 0
    _wssid_refreshed = False  # 每帳號只 retry 一次,防無限 loop
    while True:
        if is_stop and is_stop():
            return None  # user 中斷,當失敗處理避免誤刪
        try:
            items, total = fetch_merchandise_list(
                session, item_status="shelve",
                sort_by="-createTime",
                offset=offset, limit=_YAHOO_PAGE_SIZE,
            )
        except AuthExpiredError as e:
            # v6.1.34:cookies 還有效但 wssid 過期場景 — 用既有 cookies HTTP 補刷新 wssid 重試
            # 修「GUI 顯示在線 + myauc 正常 2804 件,但 D1 對齊 401001」誤判
            # 參考 merch_http_ops.py:661 AUTH 過期重新提取 pattern,但只動 wssid 不動 cookies
            if not _wssid_refreshed:
                _wssid_refreshed = True
                try:
                    from .merch_http_ops import _fetch_wssid_http
                    from .cookie_store import save_cookie_cache
                    new_wssid = _fetch_wssid_http(session.cookies, proxy="")
                    if new_wssid and new_wssid != session.wssid:
                        if log:
                            log(f"[D1對齊] {profile_dir.name}: wssid 過期,HTTP 補刷成功,重試 offset={offset}")
                        session.wssid = new_wssid
                        save_cookie_cache(
                            profile_dir, session.cookies, new_wssid,
                            raw_cookies=getattr(session, "raw_cookies", None),
                        )
                        continue  # 不 offset++ 重試當前頁
                except Exception as _e_refresh:
                    if log:
                        log(f"[D1對齊] {profile_dir.name}: wssid 補刷異常: {_e_refresh}")
            if log:
                log(f"[D1對齊] {profile_dir.name}: AUTH 過期 offset={offset}: {e}")
            return None
        except Exception as e:
            if log:
                log(f"[D1對齊] {profile_dir.name}: fetch 異常 offset={offset}: {e}")
            return None

        if not items:
            break
        onshelf.update(str(it.get("id", "")).strip() for it in items if it.get("id"))

        if offset + len(items) >= (total or 0):
            break
        offset += _YAHOO_PAGE_SIZE
        # 同帳號連續分頁小 sleep(避免 burst)
        time.sleep(_INTER_PAGE_DELAY_MS / 1000.0)

    if log:
        log(f"[D1對齊] {profile_dir.name}: Yahoo on-shelf = {len(onshelf)} 件")
    return onshelf


# ─────────────────────────────────────────────────────────
# 批量 delete(用既有 /api/delete-by-product-codes)
# ─────────────────────────────────────────────────────────

def batch_delete_stale_codes(
    owner: str, product_codes: List[str],
    *, log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """v6.1.25:public 入口供 UI dialog「確認刪除」直接 call,
    跳過重複 analysis,只執行 delete 階段。
    """
    return _batch_delete_d1(owner, product_codes, log=log, is_stop=is_stop)


async def cleanup_yahoo_extras(
    *,
    base_dir: Path,
    chrome_path: str,
    missing_records: List[Dict[str, str]],
    headless: bool = True,
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    concurrency: int = 3,
    batch_size: int = 10,
) -> Dict[str, Any]:
    """v6.1.25:對「Yahoo 有但 D1 沒有」的商品執行批量下架+刪除。

    把 missing_records(由 reconcile 算出來)按 account 分組,
    每帳號分批 10 件,用既有 `run_http_merch_id_ops`(純 HTTP)處理。
    跟「批量上下架 → 根據商品編號下架刪除」mode 走同一條流程,只是 IDs 來源不同。

    Returns: {
        accounts_total, accounts_done, accounts_failed,
        accounts_done_list, accounts_failed_list, total_codes
    }
    """
    from collections import defaultdict
    from .merch_http_ops import HttpBatchConfig, run_http_merch_id_ops

    _log = log or (lambda _: None)

    if not missing_records:
        _log("[Yahoo清理] 沒有 missing records 可處理")
        return {
            "accounts_total": 0, "accounts_done": 0, "accounts_failed": 0,
            "accounts_done_list": [], "accounts_failed_list": [],
            "total_codes": 0,
        }

    # 按 account 分組
    by_account: Dict[str, List[str]] = defaultdict(list)
    for r in missing_records:
        acc = (r.get("account") or "").strip()
        pc = (r.get("product_code") or "").strip()
        if acc and pc:
            by_account[acc].append(pc)

    if not by_account:
        _log("[Yahoo清理] missing records 缺有效欄位")
        return {
            "accounts_total": 0, "accounts_done": 0, "accounts_failed": 0,
            "accounts_done_list": [], "accounts_failed_list": [],
            "total_codes": 0,
        }

    total_codes = sum(len(c) for c in by_account.values())
    _log(
        f"[Yahoo清理] 開始清理 {len(by_account)} 個帳號共 {total_codes} 件"
        f"(並發 {concurrency},每批 {batch_size} 件)"
    )

    sem = asyncio.Semaphore(concurrency)
    base_dir = Path(base_dir)

    async def _process_account(account_name: str, codes: List[str]):
        async with sem:
            if is_stop and is_stop():
                return account_name, "stopped", len(codes)
            profile_dir = base_dir / "profiles" / account_name
            if not profile_dir.exists():
                _log(f"[Yahoo清理] {account_name}: profile 不存在,skip")
                return account_name, "no_profile", len(codes)

            batches = [
                codes[i:i + batch_size]
                for i in range(0, len(codes), batch_size)
            ]
            cfg = HttpBatchConfig(
                mode="根據商品編號下架刪除",
                interval_sec=0.0,
                headless=headless,
                batch_size=batch_size,
            )
            try:
                status = await run_http_merch_id_ops(
                    base_dir=base_dir,
                    profile_dir=profile_dir,
                    chrome_path=chrome_path,
                    account_name=account_name,
                    profile_id=account_name,  # account 通常等於 profile_id
                    batches=batches,
                    cfg=cfg,
                    proxy="",
                    log=log,
                    is_stop=is_stop,
                )
                _log(
                    f"[Yahoo清理] {account_name}: {status} "
                    f"({len(codes)} 件 / {len(batches)} 批)"
                )
                return account_name, status, len(codes)
            except Exception as e:
                _log(f"[Yahoo清理] {account_name}: 異常: {e}")
                return account_name, "error", len(codes)

    tasks = [_process_account(acc, codes) for acc, codes in by_account.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    summary: Dict[str, Any] = {
        "accounts_total": len(by_account),
        "accounts_done": 0,
        "accounts_failed": 0,
        "accounts_done_list": [],
        "accounts_failed_list": [],
        "total_codes": total_codes,
    }
    for r in results:
        if isinstance(r, Exception):
            _log(f"[Yahoo清理] 帳號處理時拋異常: {r}")
            continue
        acc, status, n = r
        if status == "done":
            summary["accounts_done"] += 1
            summary["accounts_done_list"].append((acc, n))
        else:
            summary["accounts_failed"] += 1
            summary["accounts_failed_list"].append((acc, status, n))

    _log(
        f"[Yahoo清理] 完成 — 成功 {summary['accounts_done']} 帳號 / "
        f"失敗 {summary['accounts_failed']} 帳號 / 共 {total_codes} 件"
    )
    return summary


def _batch_delete_d1(
    owner: str, product_codes: List[str],
    *, log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """批量刪 D1 records。回傳真實刪除數。"""
    if not product_codes:
        return 0
    BATCH = 200
    deleted = 0
    for i in range(0, len(product_codes), BATCH):
        if is_stop and is_stop():
            if log:
                log(f"[D1對齊] 用戶中斷,已刪 {deleted}/{len(product_codes)}")
            break
        chunk = product_codes[i:i + BATCH]
        try:
            r = _requests.post(
                f"{_D1_WORKER_URL}/api/delete-by-product-codes",
                json={
                    "token": _D1_TOKEN,
                    "product_codes": chunk,
                    "owner": owner,  # 雙保險:owner 過濾防跨帳號誤刪
                },
                headers={"User-Agent": "curl/8.0.1"},
                timeout=30,
            )
            data = r.json()
        except Exception as e:
            if log:
                log(f"[D1對齊] 刪除批次 {i // BATCH + 1} 異常: {e}")
            continue
        if data.get("ok"):
            d = int(data.get("deleted", 0) or 0)
            deleted += d
            if log:
                log(
                    f"[D1對齊]   批次 {i // BATCH + 1}/"
                    f"{(len(product_codes) + BATCH - 1) // BATCH}: 刪除 {d}/{len(chunk)} 條"
                )
        else:
            if log:
                log(f"[D1對齊] 刪除批次 {i // BATCH + 1} 失敗: {data}")
    return deleted


# ─────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────

async def reconcile_d1_with_yahoo(
    *,
    base_dir: Path,
    owner: str,
    accounts: List[Dict[str, Any]],       # accounts.json 的 list
    dry_run: bool = True,
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ReconcileResult:
    """主入口:對齊 D1 跟 Yahoo 真實上架狀態。

    流程:
    1. 拉 D1 全量 + 存 backup snapshot(無條件)
    2. 按 account 分組
    3. account 不在 accounts.json → 全保留(non-Yahoo)
    4. account 在 accounts.json → 並發 fetch on-shelf
    5. 計算 stale per account
    6. 存 delete_plan
    7. dry_run=True → 停在這一步
    8. 否則:批量 delete
    """
    result = ReconcileResult(dry_run=dry_run)
    t_start = time.time()
    base_dir = Path(base_dir)

    if not owner:
        if log:
            log("[D1對齊] ❌ 無 owner(tg_chat_id 空)— 取消")
        return result

    _log = log or (lambda _: None)

    # ── 1. 拉 D1 全量 + 備份 ─────────────────────────────
    _mode_zh = "預覽模式(只算不刪)" if dry_run else "實際執行(會真的刪除)"
    _log(f"[D1對齊] 開始 — 模式:{_mode_zh},擁有者={owner}")
    _log("[D1對齊] 拉 D1 全量 records...")
    try:
        all_d1 = fetch_all_d1_records(owner, log=log)
    except Exception as e:
        _log(f"[D1對齊] ❌ 拉 D1 失敗: {e}")
        return result
    result.total_d1_records = len(all_d1)
    _log(f"[D1對齊] 拿到 {len(all_d1)} 條 D1 records")

    backup_root = save_d1_backup(owner, all_d1, base_dir, log=log)
    result.backup_path = str(backup_root / "d1_backup.xlsx")

    if is_stop and is_stop():
        _log("[D1對齊] 用戶中斷")
        return result

    # ── 2. 按 account 分組 ──────────────────────────────
    by_account: Dict[str, List[Dict[str, str]]] = {}
    for r in all_d1:
        acc = (r.get("account") or "").strip()
        if not acc:
            # account 為空:當非奇摩保留
            by_account.setdefault("", []).append(r)
            continue
        by_account.setdefault(acc, []).append(r)

    # ── 3. 跟 accounts.json 對照 ──────────────────────
    # 用小寫 strip 做 case-insensitive 比對
    profile_set = {
        (a.get("profile_id") or "").strip().lower()
        for a in accounts if a.get("profile_id")
    }
    profile_set.discard("")

    to_check: List[Tuple[str, List[Dict[str, str]]]] = []  # (account_name, d1_records)
    orphan_records: List[Dict[str, str]] = []

    for acc, records in by_account.items():
        if acc.lower() in profile_set:
            to_check.append((acc, records))
        else:
            orphan_records.extend(records)

    result.kept_non_yahoo = len(orphan_records)
    _log(
        f"[D1對齊] 分組完成:"
        f"{len(orphan_records)} 條保留(非奇摩 account,例 rosa9855),"
        f"{len(to_check)} 個奇摩帳號要查 Yahoo"
    )

    if not to_check:
        _log("[D1對齊] 沒有奇摩帳號的 D1 records — 全部保留")
        result.elapsed_sec = time.time() - t_start
        return result

    # ── 4. 並發 fetch on-shelf codes ────────────────────
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(concurrency)

    async def check_one(acc_name: str, d1_records: List[Dict[str, str]]):
        async with sem:
            if is_stop and is_stop():
                return acc_name, None, d1_records
            profile_dir = base_dir / "profiles" / acc_name
            # cookie cache 同步操作,run in executor
            onshelf = await loop.run_in_executor(
                None,
                lambda pd=profile_dir: fetch_all_onshelf_codes(
                    pd, log=log, is_stop=is_stop,
                ),
            )
            return acc_name, onshelf, d1_records

    _log(
        f"[D1對齊] 並發 {concurrency} 開始查 Yahoo on-shelf "
        f"(預估 {(len(to_check) + concurrency - 1) // concurrency * 20} 秒)..."
    )
    tasks = [check_one(acc, recs) for acc, recs in to_check]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    # ── 5. 計算 stale + missing per account ──────────────
    stale_records: List[Dict[str, str]] = []
    stale_codes: List[str] = []
    missing_records: List[Dict[str, str]] = []  # v6.1.25: Yahoo 有 D1 沒的

    for outcome in fetched:
        if isinstance(outcome, Exception):
            _log(f"[D1對齊] 帳號處理時拋異常: {outcome}")
            continue
        acc_name, onshelf, d1_records = outcome
        if onshelf is None:
            # fetch 失敗 → 整個帳號 skip,不刪任何東西
            result.accounts_skipped += 1
            result.skipped_account_names.append(acc_name)
            result.per_account_summary.append({
                "account": acc_name,
                "status": "skipped (fetch failed)",
                "d1_count": len(d1_records),
                "onshelf_count": None,
                "stale_count": 0,
            })
            continue

        result.accounts_checked += 1
        # 算 stale(D1 有但 Yahoo 沒有 → 要刪)
        acc_stale = [r for r in d1_records if (r.get("product_code") or "") not in onshelf]
        acc_kept = len(d1_records) - len(acc_stale)
        stale_records.extend(acc_stale)
        stale_codes.extend(r.get("product_code") or "" for r in acc_stale)

        # v6.1.25:也算 missing(Yahoo 有但 D1 沒有 → 用戶需要補上傳 Excel,軟件無法自動補)
        # 原因:D1 一條 record 需要 barcode(閒魚/煤炉 URL),Yahoo API 拿不到此欄位
        d1_codes_set = {(r.get("product_code") or "").strip() for r in d1_records}
        d1_codes_set.discard("")
        missing_in_d1 = onshelf - d1_codes_set
        # 累積 missing_records,barcode 留空給用戶填
        for code in sorted(missing_in_d1):
            missing_records.append({
                "barcode": "",  # ← 留空,Yahoo API 不知道
                "product_code": code,
                "account": acc_name,
            })

        result.per_account_summary.append({
            "account": acc_name,
            "status": "ok",
            "d1_count": len(d1_records),
            "onshelf_count": len(onshelf),
            "stale_count": len(acc_stale),
            "kept_count": acc_kept,
            "missing_in_d1_count": len(missing_in_d1),  # Yahoo 有但 D1 沒有(需手動補)
        })
        missing_note = (
            f" + Yahoo 有但 D1 沒有 {len(missing_in_d1)} 條(需手動補上傳 Excel)"
            if missing_in_d1 else ""
        )
        _log(
            f"[D1對齊]   {acc_name}: D1 {len(d1_records)} 條 / "
            f"Yahoo on-shelf {len(onshelf)} 件 → stale {len(acc_stale)} 條"
            f"{missing_note}"
        )

    # 過濾掉空 product_code(防垃圾資料)
    stale_codes = [c for c in stale_codes if c and c.strip()]
    result.stale_codes_total = len(stale_codes)

    # ── 6. 寫 delete_plan + missing_plan + README ─────────
    plan_path, missing_path = save_delete_plan(
        backup_root, stale_records, result.per_account_summary,
        owner=owner, dry_run=dry_run,
        full_record_count=result.total_d1_records,
        missing_records=missing_records,
        log=log,
    )
    result.plan_path = str(plan_path)
    result.missing_plan_path = str(missing_path) if missing_path else ""
    # v6.1.25:把詳細 records 也填到 result 給 UI dialog 用
    result.stale_records = stale_records
    result.stale_codes = list(stale_codes)
    result.missing_records = missing_records
    result.missing_total = len(missing_records)
    result.owner = owner

    # ── 7. 彙總 ─────────────────────────────────────────
    _log("[D1對齊] ═════ 對齊統計 ═════")
    _log(f"  D1 總 records         : {result.total_d1_records}")
    _log(f"  非奇摩 account 保留   : {result.kept_non_yahoo}")
    _log(f"  奇摩帳號查過 / skip   : {result.accounts_checked} / {result.accounts_skipped}")
    if result.skipped_account_names:
        _log(f"  skip 的帳號           : {', '.join(result.skipped_account_names)}")
    _log(f"  待刪 stale(D1 有 Yahoo 沒): {result.stale_codes_total}")

    # v6.1.25:總和 Yahoo 有 D1 沒有的數量(用戶需手動補上傳 Excel)
    total_missing = sum(
        int(s.get("missing_in_d1_count", 0) or 0)
        for s in result.per_account_summary
    )
    if total_missing > 0:
        miss_accounts = [
            f"{s['account']}={s['missing_in_d1_count']}"
            for s in result.per_account_summary
            if s.get("missing_in_d1_count", 0) > 0
        ]
        _log(
            f"  Yahoo 有 D1 沒(需手動補上傳): {total_missing} 條 "
            f"[ {', '.join(miss_accounts)} ]"
        )
        _log(
            "  → 這些是你 Yahoo 帳號上有但 D1 沒對應記錄的商品,"
            "本工具無法自動補(因為缺 barcode/閒魚 URL)。"
            "請整理對應的 Excel 後用「上傳編碼數據 → 追加更新」補上去。"
        )
    _log(f"  備份               → {result.backup_path}")
    _log(f"  delete_plan        → {result.plan_path}")

    # ── 8. 預覽模式停在這 ─────────────────────────────
    if dry_run:
        _log(
            "[D1對齊] ✅ 預覽完成 — 沒有任何刪除動作。"
            "確認上面 stale 列表後請按「實際執行對齊」。"
        )
        result.elapsed_sec = time.time() - t_start
        return result

    # ── 9. 真實刪除 ────────────────────────────────────
    if not stale_codes:
        _log("[D1對齊] ✅ 完成 — 沒有需要刪除的 stale records")
        result.elapsed_sec = time.time() - t_start
        return result

    _log(f"[D1對齊] 開始刪除 {len(stale_codes)} 條 stale...")
    deleted = _batch_delete_d1(owner, stale_codes, log=log, is_stop=is_stop)
    result.deleted_count = deleted

    _log(
        f"[D1對齊] ✅ 完成 — 刪除 {deleted}/{len(stale_codes)} 條 stale "
        f"(保留 {result.total_d1_records - deleted} 條)"
    )
    if deleted < len(stale_codes):
        _log(
            f"[D1對齊] ⚠️ 有 {len(stale_codes) - deleted} 條沒刪掉"
            "(可能 worker 回傳的 deleted < 預期,可重跑檢查)"
        )

    result.elapsed_sec = time.time() - t_start
    _log(f"[D1對齊] 總耗時 {result.elapsed_sec:.1f} 秒")
    return result
