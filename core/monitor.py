from __future__ import annotations
import asyncio
import json as _json
import os
import subprocess
import time
import random
import hashlib
import traceback as _tb
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Callable, Optional, List

from .client_runtime_compat import async_playwright, apply_runtime_normalization_async
from .scraper import scrape_myauc

# 文件级崩溃日志
_CRASH_LOG = Path(__file__).parent.parent / "crash.log"

def _crash_log(msg: str):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [MON] {msg}\n")
            f.flush()
    except Exception:
        pass

from .yahoo_im_fulltext import capture_yahoo_im_fulltext, capture_yahoo_im_unread_previews, enrich_previews_with_fulltext
from .profile_lock import try_acquire, release, detect_chrome_profile_in_use, CHROME_SINGLETON_FILES, force_clear_all
from .cookie_store import save_cookie_cache, load_cookie_cache, migrate_raw_cookies_for_all
from .human import human_interval_sec, human_jitter_ms, maybe_extra_think_ms
from .accounts import load_settings
# v6.0.83:移除 Playwright BOSH JWT 攔截 — 改用 yahoo_im_jwt.ensure_bosh_jwt (純 HTTP AES decrypt)


_PUSHED_FILE = Path(__file__).parent.parent / "pushed_orders.json"

def _load_pushed() -> set:
    try:
        return set(_json.loads(_PUSHED_FILE.read_text("utf-8")))
    except Exception:
        return set()

def _save_pushed(s: set):
    # 只保留最近 2000 条，防止无限增长
    items = sorted(s)[-2000:]
    _PUSHED_FILE.write_text(_json.dumps(items, ensure_ascii=False), "utf-8")

def _get_system_chrome_path(prefer: str = "") -> str:
    """Return system Chrome/Edge executable path. Best effort.

    If prefer is provided and exists, use it. Otherwise try settings.json (browser_path),
    then common Windows install paths.
    """
    def _ok(p: str) -> str:
        p = (p or "").strip()
        if not p:
            return ""
        try:
            if Path(p).exists():
                return p
        except Exception:
            pass
        return ""

    p = _ok(prefer)
    if p:
        return p

    try:
        s = load_settings() or {}
        p = _ok(s.get("browser_path", ""))
        if p:
            return p
    except Exception:
        pass

    candidates = [
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    ]
    for c in candidates:
        p = _ok(c)
        if p:
            return p
    return ""

@dataclass
class AccountState:
    name: str
    profile_id: str
    start_url: str
    refresh_sec: int = 300
    proxy: str = ""
    note: str = ""
    selected: bool = True

    # --- monitor selection (decoupled from UI selection) ---
    # v4.3.5+: UI 的"勾选"也用于批量上下架/删除等操作目标选择。
    # 为避免"为了执行操作临时取消勾选 -> 其他账号监控被停掉"的问题，
    # 监控调度改为使用 monitor_selected（在开始监控时对当前勾选做一次快照）。
    # 这样：
    # - 监控中的账号集合稳定（除非停止监控后重新开始）。
    # - UI 勾选可自由用于批量操作目标选择，而不会影响正在运行的监控。
    monitor_selected: bool = True

    # runtime
    status: str = "离线"
    last_values: Dict[str, int] = field(default_factory=lambda: {"paid_to_ship":0, "cod":0, "im":0})
    last_change_ts: float = 0.0
    next_run_ts: float = 0.0
    running: bool = False
    last_error: str = ""

    # account restriction flags
    suspended: bool = False

    # IM persistence reminder
    # If IM badge stays >0 for multiple consecutive monitor rounds, we will re-notify.
    # This mitigates the "reply -> customer replies instantly" case where IM count may stay the same.
    im_persist_rounds: int = 0

    # last successful monitor tick ts(供 API server /api/state/accounts 用,純讀,非主流程)
    last_check_ts: float = 0.0


def _profile_dir(base_dir: Path, profile_id: str) -> Path:
    p = base_dir / "profiles" / profile_id
    p.mkdir(parents=True, exist_ok=True)
    return p

def detect_change(oldv: Dict[str,int], newv: Dict[str,int]) -> bool:
    # 只在"增加"时触发（你说的：发生改变需要通知，重点是新增）
    for k in ("paid_to_ship","cod","im"):
        if int(newv.get(k,0)) > int(oldv.get(k,0)):
            return True
    return False

def detect_order_change(oldv: Dict[str,int], newv: Dict[str,int]) -> bool:
    """只检测订单变化（paid_to_ship / cod 增加）。"""
    for k in ("paid_to_ship","cod"):
        if int(newv.get(k,0)) > int(oldv.get(k,0)):
            return True
    return False

def detect_im_change(oldv: Dict[str,int], newv: Dict[str,int]) -> bool:
    """只检测 IM 变化。"""
    return int(newv.get("im",0)) > int(oldv.get("im",0))

async def _scrape_new_orders_and_notify(page, acc_name, oldv, newv, conv_manager, on_log):
    """检测到新订单时，进入订单列表页抓取商品信息，查采购链接，推送到主管TG。

    Yahoo 订单列表页结构：每个订单是一个卡片，卡片上有商品标题链接和金额。
    点"明细"会弹 modal（不是导航到新页面）。
    策略：先尝试从卡片直接提取商品链接；如果没有，再逐个点明细从 modal 提取。
    """
    from core.tg_conversation import _query_product_d1, _classify_source
    import re as _re

    delta = (int(newv.get("paid_to_ship", 0)) - int(oldv.get("paid_to_ship", 0))) \
          + (int(newv.get("cod", 0)) - int(oldv.get("cod", 0)))
    if delta <= 0:
        return
    n = min(delta, 10)
    original_url = page.url

    try:
        # 导航到订单列表页
        base = original_url.split("/myauc")[0]
        order_list_url = base + "/partner/order/list"
        await page.goto(order_list_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        # 方案A：直接从订单列表页提取所有商品链接（卡片上的）
        # v6.0.48: 優先讀 isoredux-data 結構化 JSON(完整訂單資料 — buyer/order_id/payment 等),
        # 失敗 fallback 既有 regex 路徑(行為跟舊版完全一致)。
        products = await page.evaluate("""(n) => {
            // ── 優先方案:isoredux-data 結構化讀取(2026-04-30 加) ──
            try {
                const el = document.getElementById('isoredux-data');
                if (el && el.textContent) {
                    const data = JSON.parse(el.textContent);
                    const listings = (data && data.orderList && data.orderList.listings) || [];
                    if (listings.length > 0) {
                        const out = [];
                        for (const o of listings) {
                            if (!o.items || o.items.length === 0) continue;
                            // 一筆訂單可能多商品,各算一個 product(跟既有邏輯一致)
                            for (const it of o.items) {
                                const url = (it.url || '').split('?')[0];
                                if (!url) continue;
                                out.push({
                                    title: (it.title || '').slice(0, 100),
                                    yahoo_url: url,
                                    amount: String((o.price && o.price.orderAmount) || ''),
                                    order_id: o.orderId || '',
                                    buyer_id: (o.buyer && o.buyer.id) || '',
                                    buyer_label: (o.buyer && o.buyer.name) || '',
                                    shipping_method: (o.shipping && o.shipping.type) || '',
                                    shipping_status: (o.shipping && o.shipping.status) || '',
                                    payment_status: (o.payment && o.payment.status) || '',
                                    payment_type: (o.payment && o.payment.type) || '',
                                    order_status: o.status || '',
                                });
                                if (out.length >= n) break;
                            }
                            if (out.length >= n) break;
                        }
                        if (out.length > 0) return out;
                    }
                }
            } catch (e) { /* fall through to regex 既有路徑 */ }

            // ── Fallback 既有方案:DOM regex(原邏輯,完全不變)──
            const results = [];
            const seen = new Set();
            const links = Array.from(document.querySelectorAll('a[href*="tw.bid.yahoo.com/item/"]'));
            for (const a of links) {
                const href = (a.href || '').split('?')[0];
                if (seen.has(href)) continue;
                seen.add(href);
                let title = (a.textContent || '').trim();
                // 去掉标题末尾粘连的价格（如 "商品名-$1,910" 或 "商品名-1910"）
                title = title.replace(/[-\\s]*\\$[\\d,]+\\s*$/, '').replace(/-[\\d,]{3,}\\s*$/, '').trim();
                if (!title || title.length < 3) {
                    const card = a.closest('div,tr,li,section');
                    if (card) {
                        const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(s => s.length > 2);
                        title = lines[0] || '';
                        title = title.replace(/[-\\s]*\\$[\\d,]+\\s*$/, '').replace(/-[\\d,]{3,}\\s*$/, '').trim();
                    }
                }
                // 找同卡片内的金额
                let amount = '';
                const card = a.closest('div,tr,li,section');
                if (card) {
                    const t = card.innerText || '';
                    const m = t.match(/(?:訂單金額|订单金额|總計|总计|合計|合计)[^\\d]*(\\$?[\\d,]+)/);
                    if (m) amount = m[1];
                    if (!amount) {
                        const m2 = t.match(/\\$\\s*([\\d,]+)/);
                        if (m2) amount = m2[1];
                    }
                }
                results.push({ title: title.slice(0, 100), yahoo_url: href, amount: amount });
                if (results.length >= n) break;
            }
            return results;
        }""", n)

        # 方案B：如果卡片上没找到商品链接，尝试点明细 modal
        if not products:
            products = await _scrape_via_detail_modals(page, n, on_log)

        if not products:
            on_log(f"[ORDER-SCRAPE] {acc_name}: no products found on order list")
            return

        pushed = _load_pushed()
        for prod in products:
            try:
                yahoo_url = prod.get("yahoo_url", "")

                # 去重：同一个 item 不重复推送
                _item_m = _re.search(r'/item/(\d+)', yahoo_url)
                _item_key = _item_m.group(1) if _item_m else yahoo_url
                if _item_key in pushed:
                    on_log(f"[ORDER-SCRAPE] skip duplicate: {_item_key}")
                    continue

                title = prod.get("title", "")
                amount = prod.get("amount", "")

                source_line = ""
                m = _re.search(r'/item/(\d+)', yahoo_url)
                if m:
                    item_id = m.group(1)
                    on_log(f"[ORDER-SCRAPE] querying D1: {item_id}")
                    result = _query_product_d1(item_id, on_log=on_log)
                    on_log(f"[ORDER-SCRAPE] D1 result: {result}")
                    if result:
                        barcode = str(result.get("barcode", "") or "").strip()
                        on_log(f"[ORDER-SCRAPE] barcode={barcode}")
                        _, source_url = _classify_source(barcode)
                        if source_url:
                            source_line = f"\n货源：{source_url}"
                        else:
                            on_log(f"[ORDER-SCRAPE] classify_source returned empty url for barcode={barcode}")
                    else:
                        on_log(f"[ORDER-SCRAPE] gennyou1 not found for {item_id}")

                msg = f"🛒 【新订单】{acc_name}"
                if title:
                    msg += f"\n商品：{title}"
                if amount:
                    msg += f"\n金额：${amount}"
                if yahoo_url:
                    msg += f"\nYahoo：{yahoo_url}"
                msg += source_line

                conv_manager._supervisor_send(msg)
                pushed.add(_item_key)
                _save_pushed(pushed)
                on_log(f"[ORDER-SCRAPE] pushed: {acc_name} - {title[:30]}")
                # 写盘 hook A：runtime/orders.jsonl（给 daemon @example_daemon_bot 当事实源）
                # v6.0.48: 從 isoredux-data 路徑可拿到完整 buyer/order_id/payment/shipping
                # 既有 regex fallback 路徑只有 title/amount/yahoo_url,新欄位空字串
                try:
                    from .runtime_hooks import log_order_pushed
                    log_order_pushed(
                        account=acc_name,
                        item_id=str(_re.search(r'/item/(\d+)', yahoo_url).group(1)) if _re.search(r'/item/(\d+)', yahoo_url) else "",
                        title=title,
                        price=amount,
                        buyer=str(prod.get("buyer_id", "") or ""),
                        yahoo_url=yahoo_url,
                        order_id=str(prod.get("order_id", "") or ""),
                        buyer_label=str(prod.get("buyer_label", "") or ""),
                        payment_status=str(prod.get("payment_status", "") or ""),
                        payment_type=str(prod.get("payment_type", "") or ""),
                        shipping_status=str(prod.get("shipping_status", "") or ""),
                        shipping_method=str(prod.get("shipping_method", "") or ""),
                        order_status=str(prod.get("order_status", "") or ""),
                    )
                except Exception:
                    pass
            except Exception as _e_prod:
                on_log(f"[ORDER-SCRAPE] product error: {_e_prod}")
    finally:
        try:
            await page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
        except Exception:
            pass


async def _scrape_via_detail_modals(page, n, on_log):
    """备用方案：逐个点击订单卡片的"明细"按钮，从 modal 中提取商品链接。"""
    results = []
    btns = await page.query_selector_all("button:has-text('明細')")
    if not btns:
        btns = await page.query_selector_all("button:has-text('明细')")
    for i, btn in enumerate(btns[:n]):
        try:
            await btn.click()
            await page.wait_for_timeout(2000)

            items = await page.evaluate("""() => {
                const results = [];
                const seen = new Set();
                // modal 内找商品链接
                const links = Array.from(document.querySelectorAll('a[href*="tw.bid.yahoo.com/item/"]'));
                for (const a of links) {
                    const href = (a.href || '').split('?')[0];
                    if (seen.has(href)) continue;
                    seen.add(href);
                    let title = (a.textContent || '').trim();
                    title = title.replace(/[-\\s]*\\$[\\d,]+\\s*$/, '').replace(/-[\\d,]{3,}\\s*$/, '').trim();
                    results.push({ title: title.slice(0, 100), yahoo_url: href });
                }
                let amount = '';
                const allText = document.body.innerText || '';
                const m = allText.match(/(?:訂單金額|订单金额|總計|总计|合計|合计)[^\\d]*(\\$?[\\d,]+)/);
                if (m) amount = m[1];
                for (const r of results) r.amount = amount;
                return results;
            }""")
            results.extend(items)

            # 关闭 modal
            for sel in ["button:has-text('×')", "button[aria-label='Close']"]:
                try:
                    b = await page.query_selector(sel)
                    if b:
                        await b.click()
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    continue
            else:
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
        except Exception as _e:
            on_log(f"[ORDER-SCRAPE] modal {i} error: {_e}")
            continue
    return results


def _hash_text(s: str) -> str:
    return hashlib.md5((s or '').encode('utf-8', errors='ignore')).hexdigest()

class MonitorManager:
    def __init__(
        self,
        base_dir: Path,
        concurrency: int,
        timeout_sec: int,
        headless: bool,
        browser_path: str,
        on_update: Callable[[str, Dict[str, Any]], None],
        on_log: Callable[[str], None],
        conv_manager=None,
        purchase_cmd=None,
        manage_bot=None,
        owner_chat_id: str = "",
    ):
        self.base_dir = base_dir
        self.concurrency = max(1, int(concurrency or 1))
        self.timeout_sec = int(timeout_sec or 45)
        self.headless = bool(headless)
        self.browser_path = str(browser_path or "").strip()
        self.on_update = on_update
        self.on_log = on_log
        self.conv_manager = conv_manager  # TG AI 客服对话管理器（可选）
        self.purchase_cmd = purchase_cmd  # TG 采购指令处理器（可选）
        self.manage_bot = manage_bot      # TG 管理 Bot（可选）
        self.owner_chat_id = (owner_chat_id or "").strip()  # 当前使用者 TG chat_id

        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(self.concurrency)
        self._task: Optional[asyncio.Task] = None
        self._states: List[AccountState] = []

        # hold map: profile_id -> set(reasons)
        self._holds: Dict[str, set[str]] = {}

        # v6.0.73:每個帳號最近 N 次 cycle 的歷史 buffer(供未登錄診斷追溯用)
        # profile_id → deque,每個 element 是一次 cycle 的摘要
        from collections import deque as _deque
        self._diag_cycle_history: Dict[str, "_deque"] = {}

    def _get_diag_history(self, profile_id: str):
        """取得指定 profile 的 cycle 歷史 buffer(maxlen=10)。"""
        from collections import deque as _deque
        if profile_id not in self._diag_cycle_history:
            self._diag_cycle_history[profile_id] = _deque(maxlen=10)
        return self._diag_cycle_history[profile_id]

    @staticmethod
    def _cycle_history_path(profile_id: str) -> Path:
        """jsonl 持久化路徑 — publish_logs/monitor_cycle_history/{safe_profile_id}.jsonl"""
        _safe = (profile_id or "unknown").replace("@", "_at_").replace("/", "_").replace("\\", "_").replace(":", "_")
        return Path("publish_logs") / "monitor_cycle_history" / f"{_safe}.jsonl"

    def _persist_cycle_history(self, profile_id: str, entry: dict) -> None:
        """append-only 寫入 jsonl — 跨 app 重啟保留 cycle 歷史。
        v6.0.73 強化診斷:單一 jsonl 文件記錄每帳號完整 cycle 歷史
        (成功/失敗都記),用於分析「未登錄前發生了什麼」pattern。
        """
        try:
            _path = self._cycle_history_path(profile_id)
            _path.parent.mkdir(parents=True, exist_ok=True)
            with open(_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
            # 若文件超大(> 2MB),保留最後 500 行(rolling)
            try:
                if _path.stat().st_size > 2 * 1024 * 1024:
                    _lines = _path.read_text(encoding="utf-8").splitlines()
                    if len(_lines) > 500:
                        _path.write_text("\n".join(_lines[-500:]) + "\n", encoding="utf-8")
            except Exception:
                pass
        except Exception:
            pass

    def _load_recent_cycle_history(self, profile_id: str, n: int = 20) -> list:
        """讀回 jsonl 最後 N 條 cycle 摘要(供未登錄診斷時對照「之前都做了什麼」)。"""
        try:
            _path = self._cycle_history_path(profile_id)
            if not _path.exists():
                return []
            with open(_path, "r", encoding="utf-8") as f:
                _lines = f.readlines()
            _recent = _lines[-n:]
            return [_json.loads(l) for l in _recent if l.strip()]
        except Exception:
            return []

    def reset_im_count(self, profile_id: str) -> None:
        """自动发送后重置 IM 计数为 0，让下次买家新消息能触发检测。"""
        for acc in self._states:
            if acc.profile_id == profile_id:
                acc.last_values["im"] = 0
                acc.im_persist_rounds = 0
                break

    def set_accounts(self, states: List[AccountState]) -> None:
        self._states = states
        now = time.time()
        # 启动抖动：避免所有账号在同一秒同时发起请求（更稳定，且不会明显变慢）
        # 这里固定 2~15 秒随机错开（每个账号不同）
        for s in self._states:
            s.next_run_ts = now + random.uniform(2.0, 15.0)

    def get_snapshot(self) -> Dict[str, Any]:
        """API server 用:當前所有監控帳號的實時狀態快照。

        純讀,不上 lock,不修改任何狀態。dataclass 字段都是 GIL 安全的原語類型。
        若需要絕對一致性的批次讀,daemon 那邊隔 5 秒呼叫即可。
        """
        try:
            now = time.time()
            rows = []
            for a in list(self._states):  # 拷一份 list 避免迭代中被 set_accounts 換掉
                lv = a.last_values or {}
                last_ts = float(getattr(a, "last_check_ts", 0.0) or 0.0)
                rows.append({
                    "name": a.name,
                    "profile_id": a.profile_id,
                    "selected": bool(a.selected),
                    "monitor_selected": bool(getattr(a, "monitor_selected", a.selected)),
                    "running": bool(a.running),
                    "status": a.status or "",
                    "suspended": bool(getattr(a, "suspended", False)),
                    "item_count": int(lv.get("item_count", 0) or 0),
                    "paid_to_ship": int(lv.get("paid_to_ship", 0) or 0),
                    "cod": int(lv.get("cod", 0) or 0),
                    "im": int(lv.get("im", 0) or 0),
                    "im_persist_rounds": int(getattr(a, "im_persist_rounds", 0) or 0),
                    "last_error": str(a.last_error or "")[:200],
                    "last_check_ts": last_ts,
                    "last_check_age_sec": int(now - last_ts) if last_ts > 0 else None,
                    "next_run_ts": float(a.next_run_ts or 0.0),
                    "held_reasons": sorted(self._holds.get(a.profile_id, set())),
                })
            return {
                "ts": now,
                "concurrency": self.concurrency,
                "running_count": sum(1 for r in rows if r["running"]),
                "total_count": len(rows),
                "accounts": rows,
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "accounts": []}

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task

    async def set_hold(self, profile_id: str, enabled: bool = True, reason: str = "manual") -> None:
        """Hold/resume a single profile inside the monitor scheduler.

        When held, the monitor will NOT schedule new runs for that profile, so you can open a visible Chrome window
        on the same user-data-dir without having to stop all monitoring.
        """
        pid = (profile_id or "").strip()
        if not pid:
            return

        # normalize reason
        r = (reason or "manual").strip() or "manual"

        # add/remove hold token
        if enabled:
            s = self._holds.setdefault(pid, set())
            s.add(r)
        else:
            if pid in self._holds:
                self._holds[pid].discard(r)
                if not self._holds[pid]:
                    del self._holds[pid]

        # update UI status for that account (best-effort)
        acc = next((a for a in self._states if a.profile_id == pid), None)
        if acc is None:
            return

        if pid in self._holds:
            acc.status = "手动中"
            acc.last_error = "接管中：" + ",".join(sorted(self._holds[pid]))
            self.on_update(pid, {"status": acc.status, "last_error": acc.last_error})
        else:
            # resume soon (do not force status)
            acc.last_error = ""
            acc.next_run_ts = time.time() + random.uniform(2.0, 5.0)
            self.on_update(pid, {"last_error": acc.last_error})

    async def wait_idle(self, profile_id: str, timeout_sec: float = 20.0) -> bool:
        """Wait until the given account is not running a monitor check."""
        pid = (profile_id or "").strip()
        if not pid:
            return True
        t0 = time.time()
        while time.time() - t0 < float(timeout_sec):
            acc = next((a for a in self._states if a.profile_id == pid), None)
            if acc is None:
                return True
            if not acc.running:
                return True
            await asyncio.sleep(0.2)
        return False

    async def run_forever(self):
        self._stop.clear()
        _cleared_accs = []
        for acc in self._states:
            pd = _profile_dir(self.base_dir, acc.profile_id)
            _c = force_clear_all(pd)
            if _c:
                _cleared_accs.append(f"{acc.name}({_c})")
        if _cleared_accs:
            self.on_log(f"[MON] 启动清理残留锁: {', '.join(_cleared_accs)}")
        self.on_log(f"[MON] started, concurrency={self.concurrency}, headless={self.headless}")

        # 一次性迁移：为缺少 raw_cookies 的账号提取完整 cookie（首次启动时执行）
        try:
            _all_pids = [acc.profile_id for acc in self._states]
            _profiles_dir = self.base_dir / "profiles"
            _chrome = self.browser_path or _get_system_chrome_path("")
            if _chrome:
                _migrated = await migrate_raw_cookies_for_all(
                    _profiles_dir, _all_pids, _chrome, on_log=self.on_log,
                )
                if _migrated > 0:
                    self.on_log(f"[MON] Cookie 迁移完成: {_migrated} 个账号")
        except Exception as _e_mig:
            self.on_log(f"[MON] Cookie 迁移异常(不影响监控): {str(_e_mig)[:120]}")

        # 自动重启：Playwright 连接崩溃后自动重建，而不是停止整个监控
        _restart_count = 0
        _MAX_RESTARTS = 50  # 防止无限重启
        while not self._stop.is_set() and _restart_count < _MAX_RESTARTS:
            try:
                _crash_log(f"run_forever: launching Playwright (restart={_restart_count})")
                async with async_playwright() as p:
                    if _restart_count > 0:
                        self.on_log(f"[MON] Playwright 连接已重建（第{_restart_count}次重启）")
                        # 重置所有账号的 running 状态（防止上次崩溃残留）
                        for acc in self._states:
                            acc.running = False
                    # Playwright 启动成功，重置连续失败计数
                    _restart_count = 0
                    self._task = asyncio.create_task(self._loop(p))
                    await self._task
            except asyncio.CancelledError:
                self.on_log("[MON] cancelled")
                return
            except Exception as e:
                _restart_count += 1
                _emsg = str(e)[:200]
                self.on_log(f"[MON] Playwright 异常（第{_restart_count}次，自动重启中）: {_emsg}")
                # 等待一段时间再重启，避免快速循环
                _wait = min(10 + _restart_count * 5, 60)
                for _ in range(int(_wait)):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(1.0)

        if _restart_count >= _MAX_RESTARTS:
            self.on_log(f"[MON] 已达最大重启次数({_MAX_RESTARTS})，监控停止")

    async def _loop(self, p):
        _rr_idx = 0  # round-robin 起始索引，避免前面的账号总是优先调度
        while not self._stop.is_set():
            try:
                now = time.time()
                _running_now = sum(1 for a in self._states if a.running)
                n = len(self._states)
                if n > 0:
                    _scheduled = 0
                    for _i in range(n):
                        if self._stop.is_set():
                            break
                        if _running_now >= self.concurrency:
                            break
                        idx = (_rr_idx + _i) % n
                        acc = self._states[idx]
                        if not getattr(acc, "monitor_selected", getattr(acc, "selected", True)):
                            continue
                        if acc.running:
                            continue
                        if acc.profile_id in self._holds:
                            continue
                        if now >= acc.next_run_ts:
                            acc.running = True
                            _running_now += 1
                            _scheduled += 1
                            task = asyncio.create_task(self._run_one(p, acc))
                            task.add_done_callback(lambda t, a=acc: self._task_done_cb(t, a))
                    # 下次从上次停下的位置继续
                    if _scheduled > 0:
                        _rr_idx = (_rr_idx + _scheduled) % n
            except Exception as _loop_err:
                try:
                    self.on_log(f"[MON] loop iteration error (已捕获): {str(_loop_err)[:160]}")
                except Exception:
                    pass
            await asyncio.sleep(1.0)

        self.on_log("[MON] stopping...")

    def _task_done_cb(self, task: asyncio.Task, acc: AccountState = None):
        """防止 create_task 的未处理异常导致事件循环崩溃，并确保 acc.running 被重置。"""
        try:
            exc = task.exception()
            if exc is not None:
                self.on_log(f"[MON] task exception (已捕获，不影响监控): {str(exc)[:160]}")
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass
        except Exception:
            pass
        # 安全网：确保 acc.running 一定被重置（防止 task cancel 时 finally 未执行）
        if acc is not None and acc.running:
            acc.running = False

    async def _run_one(self, p, acc: AccountState):
        async with self._sem:
            try:
                ctx = None
                profile_dir = None
                lock_ok = False

                # if this profile is held (handoff), abort quickly
                if acc.profile_id in self._holds:
                    acc.status = "手动中"
                    self.on_update(acc.profile_id, {"status": acc.status})
                    return

                acc.status = "巡检中"
                self.on_update(acc.profile_id, {"status": acc.status})
                profile_dir = _profile_dir(self.base_dir, acc.profile_id)
                in_use, reason = detect_chrome_profile_in_use(profile_dir)
                if in_use:
                    acc.status = '占用'
                    acc.last_error = reason
                    self.on_update(acc.profile_id, {'status': acc.status, 'last_error': acc.last_error})
                    self.on_log(f'[LOCK] {acc.name}: {acc.last_error}')
                    return
                # 轻量锁：避免同一 Profile 被监控/批量同时占用（防止 Cookie/会话被写乱）
                ok, reason = try_acquire(profile_dir, owner="monitor")
                if not ok:
                    acc.status = "占用"
                    acc.last_error = reason
                    self.on_update(acc.profile_id, {"status": acc.status, "last_error": acc.last_error})
                    self.on_log(f"[LOCK] {acc.name}: {acc.last_error}")
                    return
                lock_ok = True

                # 代理（可选，格式示例：http://user:pass@host:port）
                proxy = None
                if acc.proxy:
                    proxy = {"server": acc.proxy}

                # 固定使用系统 Chrome（executable_path），不再回退 Playwright 自带 Chromium（会导致 Cookie/会话不兼容 -> 未登录）
                _hl = bool(self.headless)
                from .client_runtime_compat import get_launch_args, get_ignore_default_args
                _args = get_launch_args(headless=_hl)
                if _hl:
                    _args.append("--window-size=1280,800")
                _ignore_args = get_ignore_default_args(headless=_hl)
                launch_kw = dict(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    proxy=proxy,
                    args=_args,
                    ignore_default_args=_ignore_args,
                )
                if _hl:
                    pass   # headless 大小由 --window-size 控制（get_launch_args 默认 1280x860）
                else:
                    pass   # 非 headless 使用浏览器默认窗口大小
                launch_kw["no_viewport"] = True   # Patchright 不支持 viewport（headless=False 参数下会 getWindowForTarget）
                exe_path = self.browser_path or _get_system_chrome_path("")
                if exe_path:
                    launch_kw["executable_path"] = exe_path

                try:
                    ctx = await p.chromium.launch_persistent_context(**launch_kw)
                except Exception as e:
                    _emsg = str(e)
                    _crash_log(f"{acc.name}: launch_persistent_context FAILED: {_emsg[:200]}")
                    acc.status = "占用"
                    acc.last_error = f"Chrome 启动失败（请关闭该账号相关Chrome窗口后重试）：{_emsg[:160]}"
                    self.on_update(acc.profile_id, {"status": acc.status, "last_error": acc.last_error})
                    self.on_log(f"[LOCK] {acc.name}: {acc.last_error}")
                    return

                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await apply_runtime_normalization_async(ctx)

                # v6.0.73:強化診斷 — 追蹤本次 cycle 的事件 / 網路請求 / 重定向鏈
                _cycle_events = []
                _cycle_requests = []
                _cycle_responses = []
                _cycle_start_ts = time.time()

                def _log_evt(etype: str, **kw):
                    _cycle_events.append({"ts": round(time.time() - _cycle_start_ts, 3),
                                          "type": etype, **kw})

                _log_evt("ctx_created", headless=_hl)

                # 註冊網路事件 listener(只記元資料,不存 body 防爆量 + 隱私)
                def _on_req(req):
                    try:
                        if len(_cycle_requests) < 60:
                            _cycle_requests.append({
                                "ts": round(time.time() - _cycle_start_ts, 3),
                                "method": req.method,
                                "url": req.url[:250],
                                "resource_type": req.resource_type,
                                "is_navigation": req.is_navigation_request(),
                            })
                    except Exception:
                        pass

                def _on_resp(resp):
                    try:
                        if len(_cycle_responses) < 60:
                            _h = {}
                            try:
                                _hd = resp.headers
                                # 只記 auth/redirect 相關 header,不爆量
                                for _hk in ("location", "set-cookie", "x-frame-options", "content-type"):
                                    _hv = _hd.get(_hk, "")
                                    if _hv:
                                        # set-cookie 只記 cookie 名稱,不記 value(隱私)
                                        if _hk == "set-cookie":
                                            _names = []
                                            for _seg in _hv.split(","):
                                                _n = _seg.split("=")[0].strip()
                                                if _n and _n not in _names:
                                                    _names.append(_n[:40])
                                            _h["set_cookie_names"] = _names[:20]
                                        elif _hk == "location":
                                            _h["location"] = _hv[:250]
                                        else:
                                            _h[_hk] = _hv[:120]
                            except Exception:
                                pass
                            _cycle_responses.append({
                                "ts": round(time.time() - _cycle_start_ts, 3),
                                "status": resp.status,
                                "url": resp.url[:250],
                                **_h,
                            })
                    except Exception:
                        pass

                try:
                    page.on("request", _on_req)
                    page.on("response", _on_resp)
                    _log_evt("listeners_installed")
                except Exception as _e_lst:
                    _log_evt("listeners_install_failed", err=str(_e_lst)[:100])

                # cookies snapshot 進入 navigation 前(供「之前」對照)
                # v6.0.73 強化:Yahoo 域 cookie 完整保留,non-yahoo 取樣即可
                _cookies_before = None
                try:
                    _ck_before_raw = await ctx.cookies()
                    _yh_before = [c for c in _ck_before_raw
                                  if "yahoo" in (c.get("domain", "") or "").lower()]
                    _non_yh_before = [c for c in _ck_before_raw
                                      if "yahoo" not in (c.get("domain", "") or "").lower()]
                    _cookies_before = {
                        "count": len(_ck_before_raw),
                        "yahoo_count": len(_yh_before),
                        # Yahoo 域 cookie 完整保留(不截斷,因為這是診斷關鍵)
                        "yahoo_names": sorted({c.get("name", "") for c in _yh_before if c.get("name")}),
                        # non-yahoo 取樣 30 個給概念,實際上不太重要
                        "non_yahoo_names_sample": sorted({c.get("name", "") for c in _non_yh_before if c.get("name")})[:30],
                    }
                    _log_evt("cookies_snapshot_before",
                             count=_cookies_before["count"],
                             yahoo_count=_cookies_before["yahoo_count"])
                except Exception as _ce:
                    _log_evt("cookies_snapshot_before_failed", err=str(_ce)[:100])

                # v6.0.83:純 HTTP 路徑替代 Playwright 攔截 — yahoo_im_jwt.decrypt_api_token(AES-128-CBC)
                # 直接 GET /fe/api/im/user → AES-CBC decrypt → 拿 plain JWT(683 字)
                # bosh_mark_read 內已自動 fallback 到 ensure_bosh_jwt(cache miss 時)
                # Playwright 攔截 BOSH SASL 取得 JWT 的方式已不需要,移除以加速 page load
                _log_evt("bosh_jwt_capture_skipped", reason="pure_http_decrypt_active")

                _log_evt("goto_start", url=acc.start_url)
                await page.goto(acc.start_url, wait_until="domcontentloaded", timeout=self.timeout_sec * 1000)
                _log_evt("goto_done", final_url=page.url)

                # 尝试等待网络空闲（Yahoo 有时有长连接，超时就算了）
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # 等待异步徽标/红点渲染（避免抓到 0）
                for _sel in ("text=已付款待出貨訂單", "text=取貨付款訂單", "text=即時通"):
                    try:
                        await page.wait_for_selector(_sel, timeout=8000)
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(human_jitter_ms(2500, low=1.0, high=1.4, min_ms=2500) + maybe_extra_think_ms(chance=0.04, extra_min_ms=200, extra_max_ms=700))

                url = (page.url or "").lower()
                _log_evt("login_check", url_lower=url[:200])
                # 如果被重定向到登录页，视为未登录
                if ("login.yahoo" in url) or ("signin" in url) or ("/login" in url):
                    # v6.0.73:連續未登錄計數 — 避免反覆巡檢觸發 Yahoo 紅標記
                    _fc = getattr(acc, "_login_fail_count", 0) + 1
                    acc._login_fail_count = _fc
                    acc.status = "未登录"
                    _log_evt("login_detected_redirected", consecutive_count=_fc)
                    if _fc >= 3:
                        # 達到 3 次 → 完全停巡檢,等使用者手動處理(避免越測越糟)
                        acc.last_error = (
                            f"連續 {_fc} 次被重定向到登入頁,自動停止巡檢避免限流加重。"
                            f"請用【打開登入窗口】手動登入後再恢復巡檢。最後 URL: {page.url[:200]}"
                        )
                    else:
                        acc.last_error = (
                            f"被重定向到登入頁(第 {_fc}/3 次):{page.url[:150]}"
                            f"(請用【打開登入窗口】在該賬號 Profile 裡登入一次,然後關閉該窗口)"
                        )
                    self.on_update(acc.profile_id, {"status": acc.status, "last_error": acc.last_error})
                    self.on_log(f"[LOGIN] {acc.name} -> {page.url} (連續第 {_fc} 次)")
                    if _fc >= 3:
                        self.on_log(f"[LOGIN] ★ {acc.name} 已達連續 3 次未登入,停止巡檢避免 Yahoo 限流加重,請手動恢復")

                    # v6.0.73 強化診斷 — 把整個 cycle 流水帳 + 網路 + cookies + 歷史 cycle 全部 dump
                    try:
                        import json as _json
                        from datetime import datetime as _dt
                        from pathlib import Path as _Path
                        from urllib.parse import urlparse as _urlparse, parse_qs as _pqs

                        _diag_dir = _Path("publish_logs") / "monitor_login_diagnostics"
                        _diag_dir.mkdir(parents=True, exist_ok=True)
                        _ts_str = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
                        _safe_acc = (acc.name or "unknown").replace("@", "_at_").replace("/", "_")
                        _diag_path = _diag_dir / f"{_safe_acc}_{_ts_str}.json"

                        # 重定向 URL query 解析(看 Yahoo 給的 reason)
                        _u = _urlparse(page.url or "")
                        _qs = {k: v[0] if v else "" for k, v in _pqs(_u.query).items()}

                        # 重定向鏈:從 responses 中過濾 3xx
                        _redirects = [r for r in _cycle_responses
                                      if 300 <= int(r.get("status", 0) or 0) < 400]

                        # 失敗/異常 response(4xx/5xx)
                        _err_resps = [r for r in _cycle_responses
                                      if int(r.get("status", 0) or 0) >= 400]

                        # 主要 navigation 請求(is_navigation_request)
                        _nav_reqs = [r for r in _cycle_requests if r.get("is_navigation")]

                        # cookies 之前 vs 現在 對比
                        # v6.0.73 強化:Yahoo cookie 完整保留 + key cookie 詳細元資料
                        # (expires/secure/httpOnly/value_len) → 排查「cookie 真過期 vs server 端拒絕」
                        _cookies_after = {"count": 0, "yahoo_count": 0, "yahoo_names": []}
                        try:
                            _ck_after_raw = await ctx.cookies()
                            _yh_after = [c for c in _ck_after_raw
                                         if "yahoo" in (c.get("domain", "") or "").lower()]
                            _non_yh_after = [c for c in _ck_after_raw
                                             if "yahoo" not in (c.get("domain", "") or "").lower()]
                            _cookies_after["count"] = len(_ck_after_raw)
                            _cookies_after["yahoo_count"] = len(_yh_after)
                            _cookies_after["yahoo_names"] = sorted({c.get("name", "") for c in _yh_after if c.get("name")})
                            _cookies_after["non_yahoo_names_sample"] = sorted({c.get("name", "") for c in _non_yh_after if c.get("name")})[:30]
                            # 關鍵 Yahoo auth cookie 詳細元資料
                            _key_names = ("Y", "T", "SSL", "B", "AS", "BX", "GUC", "PH",
                                          "F", "A1", "A1S", "A3", "APC")
                            _now_ts = time.time()
                            _key_meta = {}
                            for kn in _key_names:
                                _matched = [c for c in _ck_after_raw if c.get("name") == kn]
                                if not _matched:
                                    _key_meta[kn] = {"present": False}
                                    continue
                                c = _matched[0]
                                _exp = c.get("expires", -1) or -1
                                _key_meta[kn] = {
                                    "present": True,
                                    "domain": c.get("domain", ""),
                                    "path": c.get("path", "/"),
                                    "secure": bool(c.get("secure", False)),
                                    "httpOnly": bool(c.get("httpOnly", False)),
                                    "sameSite": c.get("sameSite", "") or "",
                                    "expires": _exp,
                                    # session cookie 的 expires = -1
                                    "ttl_sec": int(_exp - _now_ts) if _exp and _exp > 0 else -1,
                                    "value_len": len(str(c.get("value", "") or "")),
                                }
                            _cookies_after["key_cookies_present"] = {
                                kn: _key_meta[kn].get("present", False) for kn in _key_names
                            }
                            _cookies_after["key_cookie_details"] = _key_meta
                            # 計算 cookies 變化(用 yahoo_names,因為 non-yahoo 是取樣不完整)
                            if _cookies_before:
                                _before_y = set(_cookies_before.get("yahoo_names", []))
                                _after_y = set(_cookies_after["yahoo_names"])
                                _cookies_after["added"] = sorted(_after_y - _before_y)
                                _cookies_after["removed"] = sorted(_before_y - _after_y)
                        except Exception as _ck_e:
                            _cookies_after["error"] = str(_ck_e)

                        _title = ""
                        try:
                            _title = await page.title()
                        except Exception:
                            pass

                        # v6.0.73 強化:抓 login page 顯示的錯誤訊息 + captcha/challenge 檢測 + body snippet
                        # → 看 Yahoo 在頁面上具體告訴用戶什麼(reason 是 query 給的,error_text 是頁面實際顯示)
                        _login_page = {}
                        try:
                            # Yahoo login 頁可能的錯誤位置 (按優先級嘗試)
                            _err_text = ""
                            for _sel in ('.error-msg', '#error-msg', '[role="alert"]',
                                         '.notice', '.alert', '.alert-error',
                                         '[class*="error-message"]', '[class*="ErrorMessage"]'):
                                try:
                                    _loc = page.locator(_sel).first
                                    if await _loc.count() > 0:
                                        _txt = (await _loc.inner_text(timeout=1500)).strip()
                                        if _txt and len(_txt) < 500:
                                            _err_text = _txt[:300]
                                            break
                                except Exception:
                                    continue
                            _login_page["error_text"] = _err_text

                            # captcha / challenge / device verification 檢測
                            _captcha_total = 0
                            _captcha_matches = {}
                            for _sel in ('iframe[src*="captcha"]', '[class*="captcha" i]', '#captcha',
                                         'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
                                         '[class*="challenge" i]', '[id*="challenge" i]',
                                         'input[name*="captcha" i]'):
                                try:
                                    _cnt = await page.locator(_sel).count()
                                    if _cnt:
                                        _captcha_matches[_sel] = _cnt
                                        _captcha_total += _cnt
                                except Exception:
                                    pass
                            _login_page["has_captcha_or_challenge"] = _captcha_total > 0
                            _login_page["captcha_matches"] = _captcha_matches

                            # 頁面可見文字 snippet(壓縮空白 + 截斷)
                            try:
                                _body_text = await page.evaluate(
                                    "() => document.body ? document.body.innerText.substring(0, 1500) : ''"
                                )
                                _login_page["body_visible_text"] = (_body_text or "").strip()[:1500]
                            except Exception as _bve:
                                _login_page["body_visible_text_err"] = str(_bve)[:100]

                            # 偵測 Yahoo 常見的訊號詞(限流指紋)
                            _bt_lower = (_login_page.get("body_visible_text", "") or "").lower()
                            _signals = []
                            for _kw in ("notrusted", "not trusted", "trusted device", "verify",
                                        "verification", "captcha", "challenge", "unusual",
                                        "suspicious", "blocked", "suspended", "locked",
                                        "您的帐户", "您的帳戶", "异常", "異常", "暫停", "暂停"):
                                if _kw in _bt_lower or _kw in (_body_text or ""):
                                    _signals.append(_kw)
                            _login_page["risk_keywords_in_body"] = _signals
                        except Exception as _lp_e:
                            _login_page["error"] = str(_lp_e)[:150]

                        # v6.0.73 強化:browser 環境快照 — 確認 runtime compat 注入有效,排除指紋被識破
                        _browser_env = {}
                        try:
                            _be_data = await page.evaluate("""() => {
                                try {
                                    return {
                                        userAgent: navigator.userAgent || '',
                                        language: navigator.language || '',
                                        languages: Array.isArray(navigator.languages) ? navigator.languages.slice(0, 5) : [],
                                        platform: navigator.platform || '',
                                        webdriver: navigator.webdriver === undefined ? 'undefined' : String(navigator.webdriver),
                                        plugins_count: (navigator.plugins && navigator.plugins.length) || 0,
                                        mimeTypes_count: (navigator.mimeTypes && navigator.mimeTypes.length) || 0,
                                        cookieEnabled: navigator.cookieEnabled,
                                        doNotTrack: navigator.doNotTrack || null,
                                        hardwareConcurrency: navigator.hardwareConcurrency || -1,
                                        deviceMemory: navigator.deviceMemory || -1,
                                        timezone: (Intl && Intl.DateTimeFormat) ? Intl.DateTimeFormat().resolvedOptions().timeZone : 'unknown',
                                        screen_width: screen.width,
                                        screen_height: screen.height,
                                        screen_colorDepth: screen.colorDepth,
                                        window_chrome_present: (typeof window.chrome === 'object' && window.chrome.runtime) ? true : false,
                                        // 反檢測有效性指標
                                        webdriver_undefined: navigator.webdriver === undefined,
                                        plugins_nonempty: (navigator.plugins && navigator.plugins.length) > 0,
                                    };
                                } catch(e) { return { _eval_err: String(e).substring(0, 200) }; }
                            }""")
                            _browser_env = _be_data or {}
                        except Exception as _be_e:
                            _browser_env = {"error": str(_be_e)[:150]}

                        # 取歷史 cycle 摘要(看這帳號最近 N 次 cycle 都做了什麼)
                        # v6.0.73 強化:從 jsonl 讀回最近 20 條(跨 session 持久化),內存 buffer 作 fallback
                        _hist_persisted = self._load_recent_cycle_history(acc.profile_id, n=20)
                        _hist_memory = list(self._get_diag_history(acc.profile_id))
                        # 持久化的優先,記憶體的補充(去重靠 ts)
                        _seen_ts = {h.get("ts", 0) for h in _hist_persisted}
                        _hist = _hist_persisted + [h for h in _hist_memory if h.get("ts", 0) not in _seen_ts]
                        _hist = sorted(_hist, key=lambda h: h.get("ts", 0))[-20:]

                        # v6.0.73 強化:算「距上次 cycle 成功多久」— 看是不是真的長期沒登
                        _last_ok_ts = 0
                        for h in reversed(_hist):
                            if h.get("result") == "ok":
                                _last_ok_ts = float(h.get("ts", 0) or 0)
                                break
                        _time_since_last_ok = round(time.time() - _last_ok_ts) if _last_ok_ts > 0 else -1

                        _diag_data = {
                            "timestamp": _dt.now().isoformat(),
                            "diag_version": "v6.0.73",
                            "account": acc.name,
                            "profile_id": acc.profile_id,
                            "consecutive_login_fail": _fc,
                            "previous_status": getattr(acc, "_prev_status", "?"),
                            "last_change_ts": getattr(acc, "last_change_ts", 0),
                            # v6.0.73 新增:距上次成功 cycle 多久(秒) → 看是不是真的長期沒登
                            "time_since_last_ok_sec": _time_since_last_ok,
                            "last_ok_ts": _last_ok_ts,

                            # 重定向的最終 URL 跟 query
                            "redirected_url": page.url,
                            "url_host": _u.netloc,
                            "url_path": _u.path,
                            "url_query_params": _qs,
                            "page_title": _title,

                            # v6.0.73 新增:login page 上 Yahoo 實際顯示的訊息 + captcha 檢測
                            "login_page": _login_page,
                            # v6.0.73 新增:browser env 快照 → 確認 runtime compat 注入沒被識破
                            "browser_env": _browser_env,

                            # cookies 對比(關鍵:看是否 Y/T/SSL 還在 + value_len/expires/secure)
                            "cookies_before": _cookies_before,
                            "cookies_after": _cookies_after,

                            # 本次 cycle 完整事件流水帳(時序)
                            "cycle_events": _cycle_events,
                            "cycle_duration_sec": round(time.time() - _cycle_start_ts, 2),

                            # 本次 cycle 的 navigation 請求
                            "navigation_requests": _nav_reqs[:20],
                            # 本次 cycle 的重定向鏈(3xx responses)
                            "redirects_chain": _redirects[:20],
                            # 本次 cycle 的失敗 response(4xx/5xx)
                            "error_responses": _err_resps[:20],
                            # 統計
                            "total_requests": len(_cycle_requests),
                            "total_responses": len(_cycle_responses),

                            # 該帳號最近 N 次 cycle 的摘要(跨 session 持久化,看 pattern)
                            "recent_cycles_history": _hist,
                            "recent_cycles_count": len(_hist),

                            "hint": (
                                "v6.0.73 強化診斷對照重點:\n"
                                "1) time_since_last_ok_sec — 距上次成功多久(秒)\n"
                                "   → 86400+ 多半是長期沒登的 server 端 session TTL 過期\n"
                                "   → 但若 < 3600(1小時內剛成功過)突然 fail 就要查限流\n"
                                "2) url_query_params.reason — Yahoo 給的原因:\n"
                                "   notloggedin = 純 session 過期(走【打開登入窗口】恢復)\n"
                                "   notrusted/devicechallenge = 限流触发,需要驗證設備\n"
                                "3) login_page.error_text / risk_keywords_in_body / has_captcha_or_challenge\n"
                                "   → 看 Yahoo 在頁面上具體說什麼(超過 query reason 的資訊)\n"
                                "4) cookies_after.key_cookie_details — 每個關鍵 cookie 的:\n"
                                "   present + domain + secure + expires + ttl_sec + value_len\n"
                                "   → value_len=0 → cookie 被清空,server 端主動讓 session 失效\n"
                                "   → ttl_sec<0 → cookie 真本地過期\n"
                                "   → present=true 但 ttl_sec 很大 → cookie 還在但 server 拒絕(notrusted 限流)\n"
                                "5) cookies_after.added / removed — Yahoo 這次 cycle 加了/刪了哪些 yahoo 域 cookie\n"
                                "6) redirects_chain[0] — 第一跳是哪個 host:\n"
                                "   tw.bid.yahoo.com → auction server 自己拒絕(session 校驗)\n"
                                "   login.yahoo.com → 已經在登入頁了\n"
                                "7) error_responses — 是否有 401/403(API 直接拒絕,通常表示帳號異常)\n"
                                "8) browser_env.webdriver_undefined / plugins_count / window_chrome_present\n"
                                "   → false/0/false 表示 runtime compat 失效,Yahoo 識破了無頭瀏覽器\n"
                                "9) recent_cycles_history — 跨 session 看這帳號最近 20 次 cycle 結果\n"
                                "   → 看是「突然壞」還是「慢慢累積壞」"
                            ),
                        }
                        _diag_path.write_text(
                            _json.dumps(_diag_data, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        # log 摘要 — 把幾個關鍵欄位露出來方便快速看
                        _key_present = _cookies_after.get("key_cookies_present", {}) or {}
                        _key_str = "/".join([
                            f"{k}={'1' if _key_present.get(k) else '0'}"
                            for k in ("Y", "T", "SSL", "B")
                        ])
                        _reason = _qs.get("reason", "?")
                        _risk_kw = (_login_page.get("risk_keywords_in_body") or [])
                        _captcha = _login_page.get("has_captcha_or_challenge", False)
                        _wd_ok = _browser_env.get("webdriver_undefined", None)
                        self.on_log(
                            f"[LOGIN-DIAG] {acc.name}: dump→{_diag_path.name} "
                            f"reason={_reason} cookies({_key_str}) "
                            f"risk_kw={len(_risk_kw)} captcha={_captcha} "
                            f"runtime_ok={_wd_ok} "
                            f"last_ok={_time_since_last_ok}s ago "
                            f"(events={len(_cycle_events)} reqs={len(_cycle_requests)} resps={len(_cycle_responses)} hist={len(_hist)})"
                        )
                    except Exception as _diag_e:
                        try:
                            self.on_log(f"[LOGIN-DIAG] {acc.name}: 診斷 dump 失敗: {_diag_e}")
                        except Exception:
                            pass

                    # 將本次失敗 cycle 摘要存進歷史 buffer + 持久化到 jsonl
                    try:
                        _fail_entry = {
                            "ts": time.time(),
                            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                            "result": "login_fail",
                            "consecutive_fail": _fc,
                            "redirected_url_host": _urlparse(page.url or "").netloc,
                            "url_reason": _qs.get("reason", ""),
                            "events_count": len(_cycle_events),
                            "duration_sec": round(time.time() - _cycle_start_ts, 2),
                            "diag_file": _diag_path.name,
                        }
                        self._get_diag_history(acc.profile_id).append(_fail_entry)
                        self._persist_cycle_history(acc.profile_id, _fail_entry)
                    except Exception:
                        pass

                    # ctx close in finally
                    return

                # 等一下页面徽标渲染
                await page.wait_for_timeout(human_jitter_ms(2500, low=1.0, high=1.4, min_ms=2500) + maybe_extra_think_ms(chance=0.04, extra_min_ms=200, extra_max_ms=700))
                newv = await scrape_myauc(page)

                # 停权/受限检测：出现【您的帳號已被停權】等提示时，状态置为【停權】并推送一次通知
                try:
                    is_susp = bool(newv.get("suspended", False))
                except Exception:
                    is_susp = False
                if is_susp:
                    reason = str(newv.get("suspend_msg", "") or "")
                    was_susp = bool(getattr(acc, "suspended", False))
                    acc.suspended = True
                    acc.status = "停權"
                    # counts 仍然保留（若页面还能显示订单红点）
                    acc.last_values = {
                        "paid_to_ship": int(newv.get("paid_to_ship", 0) or 0),
                        "cod": int(newv.get("cod", 0) or 0),
                        "im": int(newv.get("im", 0) or 0),
                        "item_count": int(newv.get("item_count", 0) or 0),
                    }
                    acc.last_error = reason or "检测到停权提示"
                    self.on_update(acc.profile_id, {
                        "status": acc.status,
                        "last_error": acc.last_error,
                        "paid_to_ship": acc.last_values["paid_to_ship"],
                        "cod": acc.last_values["cod"],
                        "im": acc.last_values["im"],
                        "item_count": acc.last_values["item_count"],
                    })
                    self.on_log(f"[SUSP] {acc.name}: {acc.last_error}")

                    # 只在首次检测到停权时推送到管理 Bot（仅通知当前使用者）
                    if not was_susp:
                        self.on_log(f"[SUSP] {acc.name} 停权通知")
                        if self.manage_bot and self.owner_chat_id:
                            try:
                                _ts = time.strftime("%Y-%m-%d %H:%M:%S")
                                _msg = (
                                    f"⚠️【账号停权】{acc.name}\n"
                                    f"原因：{reason or '检测到停权提示'}\n"
                                    f"时间：{_ts}"
                                )
                                self.manage_bot.send_to(self.owner_chat_id, _msg)
                            except Exception as _e:
                                self.on_log(f"[TG-MANAGE] 停权通知推送失败: {_e}")
                    return
                else:
                    # 若之前停权，现已恢复到正常页（best-effort）
                    if bool(getattr(acc, "suspended", False)):
                        acc.suspended = False
                

                # IM 红点可能会晚 1~3 秒才渲染；若首次抓不到（=0），稍等再抓一次。
                # 以前这里依赖 loggedIn 字段，但 scraper 并不返回 loggedIn，导致永远不二次抓取。
                try:
                    for _im_retry in range(3):
                        if int(newv.get("im", 0) or 0) > 0:
                            break
                        await page.wait_for_timeout(5000)
                        newv2 = await scrape_myauc(page)
                        if int(newv2.get("im", 0) or 0) > 0:
                            newv = newv2
                            break
                except Exception:
                    pass

                oldv = dict(acc.last_values)
                acc.last_values = {
                    "paid_to_ship": int(newv.get("paid_to_ship",0)),
                    "cod": int(newv.get("cod",0)),
                    "im": int(newv.get("im",0)),
                    "item_count": int(newv.get("item_count",0)),
                }

                # IM persistence reminder:
                # - When IM badge stays >0 for multiple consecutive monitor rounds,
                #   re-notify every 5 rounds if the IM count is unchanged.
                # - This reduces missed alerts when customer replies instantly but the badge count remains the same.
                new_im = int(acc.last_values.get("im", 0) or 0)
                old_im = int(oldv.get("im", 0) or 0)
                if new_im > 0:
                    acc.im_persist_rounds += 1
                else:
                    acc.im_persist_rounds = 0
                acc.status = "在线"
                acc.last_error = ""
                self.on_update(acc.profile_id, {
                    "status": acc.status,
                    "paid_to_ship": acc.last_values["paid_to_ship"],
                    "cod": acc.last_values["cod"],
                    "im": acc.last_values["im"],
                    "item_count": acc.last_values["item_count"],
                })
                self.on_log(f"[OK] {acc.name} paid_to_ship={acc.last_values['paid_to_ship']} cod={acc.last_values['cod']} im={acc.last_values['im']}")
                # 写盘 hook B：dump runtime/account_stats.json（60s 节流，给 daemon 当事实源）
                # 同时记录 last_check_ts 给 API /api/state/accounts 用
                try:
                    acc.last_check_ts = time.time()
                except Exception:
                    pass
                try:
                    from .runtime_hooks import dump_account_stats
                    snap: Dict[str, Any] = {}
                    for _a in self._states:
                        snap[_a.profile_id] = {
                            "name": _a.name,
                            "item_count": int(_a.last_values.get("item_count", 0) or 0),
                            "paid_to_ship": int(_a.last_values.get("paid_to_ship", 0) or 0),
                            "cod": int(_a.last_values.get("cod", 0) or 0),
                            "im": int(_a.last_values.get("im", 0) or 0),
                            "status": _a.status,
                            "updated_ts": time.time(),
                        }
                    dump_account_stats(snap)
                except Exception:
                    pass

                # ── 保存 cookies 到缓存（供 HTTP 批量操作免浏览器使用） ──
                try:
                    _raw_cookies = await ctx.cookies()
                    _yahoo_cookies = {
                        c["name"]: c["value"]
                        for c in _raw_cookies
                        if "yahoo" in c.get("domain", "").lower()
                    }
                    if _yahoo_cookies:
                        # 尝试从页面提取 wssid
                        _wssid = ""
                        try:
                            _iso = await page.evaluate("""() => {
                                const el = document.getElementById('isoredux-data');
                                if (el) try { return JSON.parse(el.textContent); } catch(e) {}
                                if (window.__ISOREDUX_DATA__) return window.__ISOREDUX_DATA__;
                                return null;
                            }""")
                            if _iso:
                                _wssid = (_iso.get("page") or {}).get("wssid", "")
                        except Exception:
                            pass
                        # 提取失败时保留旧 wssid，防止空值覆盖有效值
                        if not _wssid:
                            try:
                                _old = load_cookie_cache(profile_dir)
                                if _old[1]:  # _old = (cookies, wssid, saved_at)
                                    _wssid = _old[1]
                            except Exception:
                                pass
                        save_cookie_cache(
                            profile_dir, _yahoo_cookies, _wssid,
                            nickname=acc.name,
                            raw_cookies=_raw_cookies,
                        )
                except Exception as _ce:
                    _crash_log(f"{acc.name}: cookie cache save failed: {_ce}")

                if newv.get("im_diag"):
                    self.on_log(f"[IM-DIAG] {acc.name}: im_diag={newv['im_diag']}")

                changed = detect_change(oldv, acc.last_values)
                order_changed = detect_order_change(oldv, acc.last_values)
                im_changed = detect_im_change(oldv, acc.last_values)
                _im_preview_items = None
                if changed:
                    _crash_log(f"{acc.name}: change detected, order={order_changed}, im={im_changed}")
                    acc.last_change_ts = time.time()
                    self.on_update(acc.profile_id, {"last_change_ts": acc.last_change_ts})
                    # TG 推送新订单通知（仅订单变化时）
                    if order_changed and self.purchase_cmd:
                        try:
                            _crash_log(f"{acc.name}: before notify_new_order")
                            self.purchase_cmd.notify_new_order(
                                acc_name=acc.name,
                                profile_id=acc.profile_id,
                                newv=acc.last_values,
                                oldv=oldv,
                            )
                            _crash_log(f"{acc.name}: after notify_new_order OK")
                        except Exception as _e_pc:
                            _crash_log(f"{acc.name}: notify_new_order EXCEPTION: {_e_pc}")
                            self.on_log(f"[TG] 订单推送失败: {_e_pc}")
                    # 新订单详情抓取 + 主管静默推送（仅配置了 supervisor 时执行）
                    if order_changed and self.conv_manager and getattr(self.conv_manager, '_sv_token', ''):
                        try:
                            _crash_log(f"{acc.name}: before _scrape_new_orders_and_notify")
                            await _scrape_new_orders_and_notify(
                                page, acc.name, oldv, acc.last_values,
                                self.conv_manager, self.on_log,
                            )
                            _crash_log(f"{acc.name}: after _scrape_new_orders_and_notify OK")
                        except Exception as _e_os:
                            _crash_log(f"{acc.name}: _scrape_new_orders_and_notify EXCEPTION: {_e_os}")
                            self.on_log(f"[ORDER-SCRAPE] failed: {_e_os}")
                    # If IM increased, capture unread previews from left list, then enrich with full text.
                    try:
                        if new_im > old_im:
                            self.on_log(f"[IM-DIAG] {acc.name}: 检测到新IM ({old_im}→{new_im})，开始采集预览")
                            _im_preview_items = await capture_yahoo_im_unread_previews(
                                page,
                                acc.start_url,
                                max_items=10,
                                scan_rounds=30,
                                on_log=self.on_log,
                            )
                            self.on_log(f"[IM-DIAG] {acc.name}: 预览采集结果={len(_im_preview_items)}条")
                            # Enrich with full conversation text via API (keeps red dot)
                            if _im_preview_items:
                                try:
                                    _im_preview_items = await enrich_previews_with_fulltext(
                                        page,
                                        acc.start_url,
                                        _im_preview_items,
                                        max_items=10,
                                        on_log=self.on_log,
                                        profile_dir=profile_dir,
                                        account_name=acc.name,
                                    )
                                except Exception as _e_ft:
                                    self.on_log(f"[IM] fulltext enrich failed (preview kept): {_e_ft}")
                    except Exception as _e_im:
                        try:
                            self.on_log(f"[IM] preview capture failed: {_e_im}")
                        except Exception:
                            pass

                    # TG 推送 IM 新消息通知到订单管理助手
                    try:
                        if im_changed and self.purchase_cmd:
                            self.purchase_cmd.notify_new_im(
                                acc_name=acc.name,
                                new_im=new_im,
                                old_im=old_im,
                                preview_items=_im_preview_items,
                            )
                    except Exception as _e_im:
                        try:
                            self.on_log(f"[TG] IM推送失败: {_e_im}")
                        except Exception:
                            pass
# ctx close in finally

                # TG AI 客服触发：IM 增加时，将未读预览传给 ConversationManager
                try:
                    if self.conv_manager and (new_im > old_im) and _im_preview_items:
                        self.on_log(f"[IM-DIAG] {acc.name}: 触发AI客服, items={len(_im_preview_items)}, chat_id={getattr(self.conv_manager, 'tg', None) and self.conv_manager.tg.chat_id or '(无)'}")
                        self.conv_manager.on_new_im(
                            acc.profile_id, acc.name, _im_preview_items,
                        )
                        self.on_log(f"[TG] conv trigger: {acc.name}, {len(_im_preview_items)} items")
                    elif (new_im > old_im) and not _im_preview_items:
                        # 2026-04-30 v6.0.48 fix: DOM scrape 偶爾失敗
                        # (contact_list_container=NOT FOUND) → 6 個 IM 永久卡住。
                        # 加 1 次重試 + missed_im.jsonl fallback,**不改既有成功路徑**。
                        self.on_log(f"[IM-DIAG] {acc.name}: IM增加但预览为空,3 秒後重試 1 次")
                        _retry_items = []
                        try:
                            await asyncio.sleep(3)
                            _retry_items = await capture_yahoo_im_unread_previews(
                                page,
                                acc.start_url,
                                max_items=10,
                                scan_rounds=30,
                                on_log=self.on_log,
                            )
                        except Exception as _e_retry:
                            self.on_log(f"[IM-DIAG] {acc.name}: 重試 capture_previews 失敗: {_e_retry}")

                        if _retry_items:
                            # 重試拿到 → 走跟成功路徑一樣的處理
                            try:
                                _retry_items = await enrich_previews_with_fulltext(
                                    page,
                                    acc.start_url,
                                    _retry_items,
                                    max_items=10,
                                    on_log=self.on_log,
                                    profile_dir=profile_dir,
                                    account_name=acc.name,
                                )
                            except Exception as _e_ft2:
                                self.on_log(f"[IM] retry fulltext enrich failed: {_e_ft2}")
                            if self.conv_manager:
                                self.conv_manager.on_new_im(
                                    acc.profile_id, acc.name, _retry_items,
                                )
                            self.on_log(f"[IM-DIAG] {acc.name}: 重試成功 {len(_retry_items)} 條,已交 AI 客服")
                        else:
                            # 重試也空 → 寫 runtime/missed_im.jsonl 給 daemon fallback 處理
                            try:
                                _miss_path = Path(__file__).resolve().parent.parent / "runtime" / "missed_im.jsonl"
                                _miss_path.parent.mkdir(parents=True, exist_ok=True)
                                with _miss_path.open("a", encoding="utf-8") as _mf:
                                    _mf.write(_json.dumps({
                                        "ts": time.time(),
                                        "account": acc.name,
                                        "profile_id": acc.profile_id,
                                        "new_im": int(new_im),
                                        "old_im": int(old_im),
                                        "reason": "capture_previews_empty_after_retry",
                                    }, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
                            self.on_log(f"[IM-DIAG] {acc.name}: IM增加但预览空(重試也空),已記 missed_im.jsonl")
                    elif (new_im > old_im) and not self.conv_manager:
                        self.on_log(f"[IM-DIAG] {acc.name}: conv_manager未初始化，跳过AI客服")
                except Exception as _e_tg:
                    try:
                        self.on_log(f"[TG] conv trigger failed: {_e_tg}")
                    except Exception:
                        pass

                # IM persists (unchanged) -> reminder every 5 rounds
                try:
                    if (new_im > 0) and (new_im == old_im) and (acc.im_persist_rounds >= 5) and (acc.im_persist_rounds % 5 == 0):
                        if self.purchase_cmd:
                            self.purchase_cmd.notify_new_im(
                                acc_name=acc.name,
                                new_im=new_im,
                                old_im=old_im,
                                preview_items=None,
                            )
                except Exception:
                    pass
            except Exception as e:
                _crash_log(f"{acc.name}: _run_one OUTER EXCEPTION: {_tb.format_exc()[:500]}")
                acc.status = "错误"
                acc.last_error = str(e)[:160]
                try:
                    self.on_update(acc.profile_id, {"status": acc.status, "last_error": acc.last_error})
                    self.on_log(f"[ERR] {acc.name}: {acc.last_error}")
                except Exception:
                    pass
            finally:
                _crash_log(f"{acc.name}: entering finally block, ctx={'yes' if ctx else 'no'}")
                try:
                    if ctx is not None:
                        await asyncio.wait_for(ctx.close(), timeout=15)
                        _crash_log(f"{acc.name}: ctx.close() OK")
                except asyncio.TimeoutError:
                    self.on_log(f"[WARN] {acc.name}: ctx.close() 超时")
                except Exception:
                    pass

                try:
                    if lock_ok and profile_dir is not None:
                        release(profile_dir)
                except Exception:
                    pass

                acc.running = False
                # v6.0.73:把本次 cycle 結果存進記憶體 buffer + 持久化到 jsonl
                # (成功/失敗都記,給 pattern 分析用,跨 app 重啟保留)
                # 注意:login_fail 已在前面分支單獨記過,這裡只記非 login_fail 的結果
                try:
                    if '_cycle_events' in dir() or '_cycle_events' in locals():
                        # 防止 _cycle_events 未定義(極早期 ctx 創建失敗的場景)
                        _evt_count = len(locals().get('_cycle_events', []) or [])
                        _req_count = len(locals().get('_cycle_requests', []) or [])
                        _resp_count = len(locals().get('_cycle_responses', []) or [])
                        _dur = round(time.time() - locals().get('_cycle_start_ts', time.time()), 2)
                        # 避免重複記 login_fail(那邊已經單獨記過)
                        if acc.status != "未登录":
                            _result = "ok" if acc.status in ("在线", "繁忙") else f"status:{acc.status}"
                            _entry = {
                                "ts": time.time(),
                                "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                "result": _result,
                                "events_count": _evt_count,
                                "requests_count": _req_count,
                                "responses_count": _resp_count,
                                "duration_sec": _dur,
                            }
                            self._get_diag_history(acc.profile_id).append(_entry)
                            self._persist_cycle_history(acc.profile_id, _entry)
                except Exception:
                    pass

                # 下次巡检（繁忙时已设置短间隔，不覆盖）
                if acc.status != "繁忙":
                    if acc.status in ("未登录", "停權", "错误"):
                        # v6.0.73:指數退避 — 第 1 次 30 分,第 2 次 2 小時,第 3 次起 24 小時
                        # 避免反覆訪問觸發 Yahoo 限流紅標
                        _fc = int(getattr(acc, "_login_fail_count", 0) or 0)
                        if _fc <= 1:
                            delay = 1800       # 30 分鐘
                        elif _fc == 2:
                            delay = 7200       # 2 小時
                        else:
                            delay = 86400      # 24 小時(基本上等使用者手動處理)
                    else:
                        # v6.0.73:正常巡檢成功 → reset 未登錄計數
                        if getattr(acc, "_login_fail_count", 0) > 0:
                            acc._login_fail_count = 0
                        base_sec = max(15, int(acc.refresh_sec or 300))
                        delay = human_interval_sec(base_sec, factor=2.0, min_sec=15)
                    acc.next_run_ts = time.time() + delay
                try:
                    _remaining = max(0, acc.next_run_ts - time.time())
                    self.on_log(f"[MON] {acc.name}: next run in {_remaining:.1f}s")
                    _crash_log(f"{acc.name}: _run_one DONE, status={acc.status}, next={_remaining:.0f}s")
                except Exception:
                    pass