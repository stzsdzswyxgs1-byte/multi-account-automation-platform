"""純 HTTP Monitor — 取代 Playwright scraper(v6.0.83+)

跟 MonitorManager 同接口(set_accounts / run_forever / stop / on_update / on_log),
但內部走純 HTTP:
- myauc 統計 → core.myauc_http.fetch_myauc_stats
- IM 未讀 → core.myauc_http.fetch_unread_im_total (BOSH)
- 新訊息 diff push → forum_bridge.forward_yahoo_inbound

特性:
- 每帳號 60s ± 20s jitter,不規律避免 fingerprint
- 錯峰啟動:N 個帳號分散在 60s 內,不是同一秒一起
- 偵測 channel unread 變化 → 拉 diff → 單條 forward(像正常聊天)
- 指數退避:失敗時暫停 5-10 分鐘
- HTTP 層 fingerprint 等同 Chrome(TLS / client hints / cookies)

不取代:Playwright 路徑保留當 fallback,settings.use_pure_http_monitor 開關控制。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .myauc_http import fetch_myauc_stats, fetch_unread_im_total
from .order_http import fetch_orders, format_order_for_tg

# AccountState 定義在 monitor.py 內,避免 import cycle 用 TYPE_CHECKING
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .monitor import AccountState as _AccountState

LogFn = Callable[[str], None]
UpdateFn = Callable[[str, Dict[str, Any]], None]

# ── 行為調整參數 ──
# v6.1:輪詢間隔降 60→90s(myauc 訂單數延遲 0.5 分鐘無感),server 看到的請求頻率減 33%
_INTERVAL_BASE_SEC = 120         # v6.1.14:每帳號基準輪詢間隔(中位數)從 90 → 120,降全局負載 25%

# v6.1.45:工作時段模擬 — 模擬「真人台灣賣家」行為,Yahoo 限流看不出 24h 等速 bot
# 用 Asia/Taipei 固定時區(不管 user 在 JP/CN 哪裡,Yahoo 看的是台灣賣家正常作息)
# - 0:00-7:00 TPE → ×3 base(模擬睡覺)
# - 7:00-22:00 TPE → ×1 base(正常工作)
# - 22:00-24:00 TPE → ×1.5 base(夜間少看)
def _now_ts() -> float:
    import time as _t
    return _t.time()


def _tpe_work_hour_multiplier() -> float:
    """根據台灣時區當前小時返回 interval 倍率。"""
    try:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            _h = datetime.now(ZoneInfo("Asia/Taipei")).hour
        except Exception:
            # zoneinfo 不可用(Python 3.8?)— fallback UTC+8 算
            import time as _t
            _h = (int(_t.time() / 3600) + 8) % 24
        if 0 <= _h < 7:
            return 3.0   # 凌晨睡覺,polling 慢 3 倍
        if 22 <= _h < 24:
            return 1.5   # 夜間少看
        return 1.0       # 正常工作時段
    except Exception:
        return 1.0       # 異常時保守用正常速度,不影響業務
# v6.1:jitter 改對數正態分布(真人查看本身就接近這分布)
# multiplier ~ lognormvariate(0, 0.35) → median=1.0, 25%-75% range ≈ 0.78-1.27
# clip 到 [_INTERVAL_MIN_SEC, _INTERVAL_MAX_SEC] 防極端值
# server 端 FFT 分析看不到 60s/90s 主頻(舊版均勻 ±20s 會留 60s peak)
_INTERVAL_MIN_SEC = 60           # 最短間隔(防 polling 太密)
_INTERVAL_MAX_SEC = 300          # 最長間隔(v6.1.45:240→300,配合 sigma 擴大)
_INTERVAL_SIGMA = 0.5            # v6.1.45:對數正態 sigma 從 0.35→0.5,範圍 [0.61×, 1.65×]
                                 # = [73s, 198s](120s base),server 端 FFT 完全看不到主頻
_STAGGER_WINDOW_SEC = 60         # 啟動錯開窗口下限(實際 max(此值, N × 1.2))
# v6.1.10:目標全局負載 ≤ 1.5 帳號 poll/秒(舊版,每帳號 3-4 HTTP → 5-6 HTTP/秒)
# v6.1.45:降到 0.5/秒,大規模(200+)帳號降低單一 IP 壓力,避免被 Yahoo 視為 abuse
# - 45 帳號:auto-scale min cap=120s 所以無感(45/0.5=90 < 120,還是 120s)
# - 100 帳號:100/0.5=200s polling 間隔(每 100 帳號分散 200 秒內)
# - 200 帳號:200/0.5=400s polling 間隔(7 分鐘一次,訂單延遲 trade-off)
_TARGET_POLL_RATE_PER_SEC = 0.5  # 全局每秒帳號 poll 上限(動態 scale 用)
# v6.1.14:停權確認 3 次 → 寫 flag 永久 skip(避免持續打停權帳號浪費請求)
# flag 7 天後過期重試一次(萬一 Yahoo 解封會自動偵測到)
_SUSPEND_CONFIRM_THRESHOLD = 3
_SUSPEND_FLAG_TTL_SEC = 7 * 86400  # 7 天
_FAIL_BACKOFF_MIN_SEC = 60       # 失敗時最少暫停
_FAIL_BACKOFF_MAX_SEC = 600      # 失敗時最多暫停(10 分鐘)
_FAIL_THRESHOLD = 3              # 連續失敗 N 次觸發長 backoff
# 異常狀態:完全暫停輪詢,等 cookie cache 變化(handoff/user 手動登入後寫入)再恢復
# 防止持續訪問 Yahoo 觸發限流
_ABNORMAL_WATCH_INTERVAL_SEC = 60   # 每 60s 檢查 cookie cache mtime 是否變化
_ABNORMAL_MAX_WAIT_SEC = 1800      # 強制醒來上限(30 分鐘),避免漏 trigger

# v6.1.18:官方頻道靜音名單 — 收到訊息只 mark_read 清紅點,不推 TG forum
# Yahoo 後台「官方客服」「廣告」這類無用通知,推進 TG 只會干擾客服流程
# 用戶可在 settings.json 加 "im_silent_buyers" 擴充(全 Y-ID 字串)
_OFFICIAL_BUYER_IDS = {
    "y9000000006",  # Y拍官方客服頻道
    "y9000000007",  # 廣告小舖 專售短效期回饋金
}


def _is_silent_channel(channel_id: str, extra_ids: Optional[List[str]] = None) -> bool:
    """檢查 channel_id 內的 buyer 是否在靜音名單。channel_id 格式 yahoo-bid-logbot1:y{a}:y{b}"""
    cid_lower = channel_id.lower()
    blacklist = set(_OFFICIAL_BUYER_IDS)
    if extra_ids:
        for b in extra_ids:
            b_clean = str(b).lower().lstrip("y").strip()
            if b_clean:
                blacklist.add(f"y{b_clean}")
    for bid in blacklist:
        if f":{bid}:" in cid_lower or cid_lower.endswith(f":{bid}"):
            return True
    return False


class PureHTTPMonitor:
    """純 HTTP 版 monitor。

    建構簽名跟 MonitorManager 對齊(便於 app.py 切換):
        mon = PureHTTPMonitor(
            base_dir, concurrency, timeout_sec,
            headless, browser_path,    # 純 HTTP 不需要這 2 個,保留為兼容
            on_update, on_log,
            conv_manager=None, purchase_cmd=None, manage_bot=None,
            owner_chat_id="",
        )
        mon.set_accounts(["AccountState", ...])
        await mon.run_forever()
        await mon.stop()
    """

    def __init__(
        self,
        base_dir: Path,
        concurrency: int = 5,
        timeout_sec: int = 15,
        headless: bool = True,
        browser_path: str = "",
        on_update: Optional[UpdateFn] = None,
        on_log: Optional[LogFn] = None,
        conv_manager=None,
        purchase_cmd=None,
        manage_bot=None,
        owner_chat_id: str = "",
    ):
        self.base_dir = Path(base_dir)
        self.concurrency = max(1, int(concurrency or 5))
        self.timeout_sec = int(timeout_sec or 15)
        self.on_update = on_update or (lambda pid, patch: None)
        self.on_log = on_log or (lambda *_: None)
        self.conv_manager = conv_manager
        self.purchase_cmd = purchase_cmd
        self.manage_bot = manage_bot
        self.owner_chat_id = (owner_chat_id or "").strip()

        self._stop_event = asyncio.Event()
        # v6.1:startup_scan 完成 snapshot rebuild 後 set;_account_loop poll 前 await
        # 防 startup_scan 慢時 _account_loop 已開跑 → snapshot 空 → 誤推所有訂單
        self._snapshot_rebuilt = asyncio.Event()
        self._states: List[Any] = []
        self._holds: Dict[str, set] = {}
        # 每帳號的 channel unread snapshot(用來算 diff)
        # v6.1:跨重啟持久化,避免「重啟後 prev=0 → 所有 channel 都被當新訊息推一次」
        self._unread_snapshot: Dict[str, Dict[str, int]] = {}
        self._last_snapshot_save_ts: Dict[str, float] = {}
        # v6.1.26:每帳號「首輪 poll 是否做過 catch-up」flag。
        # in-memory,跨重啟重置。確保軟件重啟後第一輪 poll 會把所有 unread channel
        # 強制當「新訊息」觸發 AI + topic 推送(即使 snapshot=Yahoo 沒 diff)
        # 修「重啟後有未讀但不推到 topic」bug。
        self._first_poll_done: Set[str] = set()
        # 每帳號連續失敗次數
        self._fail_count: Dict[str, int] = {}
        # 每帳號異常狀態(需登入/停權)連續次數 — 用來漸進 cool-down
        self._abnormal_count: Dict[str, int] = {}
        # v6.1.8:myauc 不明錯誤連續失敗計數,>=2 次才判「離線」,防 100 帳號 rate limit 瞬間誤判
        self._myauc_fail_count: Dict[str, int] = {}
        # 每帳號是否正在 _poll_once 中(供 wait_idle 用)
        self._in_poll: Dict[str, bool] = {}
        # v6.1.48:外部 mark_read 的時間戳(race condition 用)
        # key=profile_id, val={channel_id: ts}
        # 修「買家秒回漏訊息」bug — 若 mark_read_ts >= poll fetch_ts,
        # 代表 mark_read 在 BOSH 拉取後才完成 → 拉到的 by_channel 是 stale,
        # 強制 override 該 cid 為 0,讓買家秒回的新訊息能被 diff 偵測到
        self._mark_read_ts: Dict[str, Dict[str, float]] = {}
        # 每帳號的訂單 ID set snapshot(偵測新訂單 diff push 用)
        # profile_id → {order_id: status}
        self._order_snapshot: Dict[str, Dict[str, str]] = {}
        # 啟動時跳過首次 diff(避免一次性 push 既有所有訂單)
        self._order_first_run: Dict[str, bool] = {}
        # buyer Y-id → nickname cache(per profile),減少重複呼叫 /fe/api/im/users
        # profile_id → {buyer_y_upper: nickname}
        self._nickname_cache: Dict[str, Dict[str, str]] = {}
        # 每帳號最近一次 cookie refresh 時間(cooldown 防爆)
        self._last_cookie_refresh: Dict[str, float] = {}
        # v6.1.45:全局健康監測 — 近 5 分鐘 myauc poll 成功率
        # ring buffer 存 (timestamp, success_bool) tuples,old entries 自動 pop
        from collections import deque as _deque
        self._global_health: _deque = _deque(maxlen=500)  # 最近 500 次 poll
        self._global_slow_mode: bool = False  # 成功率 < 80% 觸發,> 95% 恢復
        self._global_slow_logged_ts: float = 0  # log throttle

    # ─── 兼容 MonitorManager 接口 ───

    def set_accounts(self, states: List[Any]) -> None:
        self._states = list(states or [])

    async def set_hold(self, profile_id: str, hold: bool, *, reason: str = "manual") -> None:
        self._holds.setdefault(profile_id, set())
        if hold:
            self._holds[profile_id].add(reason)
        else:
            self._holds[profile_id].discard(reason)

    def reset_im_count(self, profile_id: str) -> None:
        """conv_manager 用:某 profile 收到回覆後重置 IM 計數。"""
        # 純 HTTP 模式下 IM 計數由下次 BOSH 拉取自動更新,這裡無需 action
        pass

    def notify_channel_marked_read(self, profile_id: str, channel_id: str) -> None:
        """v6.1.48:外部呼叫 im_mark_read 後通知 monitor 同步 reset snapshot[cid]→0。

        修「買家秒回漏訊息」bug:
        - 使用者點 OK 回覆 → _auto_send_to_yahoo 呼叫 im_mark_read → Yahoo unread=0
        - 但 monitor 的 _unread_snapshot[cid] 還是舊值(例如 1,從上次 poll 留下)
        - 買家秒回 → Yahoo unread 升回 1
        - 下次 poll: by_channel[cid]=1, prev=1, diff=0 → forum 不推、AI 不觸發
        - 直到買家再發 1 條(unread→2)才被偵測,但 _forward_to_forum 只拉 n_new=1 條
          最新的,中間秒回那條永久丟失

        修法:
        1. 記錄 mark_read_ts(用於下次 poll 偵測 race condition)
        2. 立刻把 snapshot[cid] 清 0
        下次 poll: by_channel[cid]=1, prev=0, diff=+1 → 推送買家秒回的訊息 ✅

        Race 處理:
        若 mark_read 發生在 poll 的 BOSH 拉取之後,poll 的 by_channel 還是舊值,
        在 _poll_once 內會用 mark_read_ts >= fetch_ts 判斷後強制 override by_channel[cid]=0

        呼叫處:
        - tg_conversation._auto_send_to_yahoo (使用者點 OK / edit / reply 後)
        - tg_conversation._mark_read_yahoo (使用者點 read 或 skip)
        """
        if not profile_id or not channel_id:
            return
        try:
            # 1. 記時間戳給下次 poll 比對 race condition
            self._mark_read_ts.setdefault(profile_id, {})[channel_id] = time.time()

            # 2. 立刻 reset snapshot,讓下次 poll diff 能偵測到買家秒回
            snap = self._unread_snapshot.get(profile_id)
            if snap is None:
                return  # 還沒 poll 過,留 ts 等下次 poll 處理
            old_val = snap.get(channel_id, 0)
            if old_val <= 0:
                return  # 本來就 0,沒事做
            snap[channel_id] = 0
            # 強寫 disk(避開 30s throttle)— 防止重啟期間 snapshot 沒同步
            self._last_snapshot_save_ts.pop(profile_id, None)
            self._save_unread_snapshot(profile_id, dict(snap))
            try:
                self.on_log(
                    f"[PUREHTTP-MON] snapshot reset cid=...{channel_id[-30:]} "
                    f"({old_val}→0,外部 mark_read 同步)"
                )
            except Exception:
                pass
        except Exception:
            pass

    async def wait_idle(self, profile_id: str, timeout_sec: float = 20.0) -> bool:
        """等待某帳號 monitor 進入 idle(不在 _poll_once 內)。

        被批量上下架/採購出貨/自動刊登/業績核對 切換 profile 占用前呼叫,
        確保 monitor 不會跟功能搶 cookie/session。
        """
        pid = (profile_id or "").strip()
        if not pid:
            return True
        t0 = time.time()
        while time.time() - t0 < float(timeout_sec):
            if not self._in_poll.get(pid, False):
                return True
            await asyncio.sleep(0.2)
        return False

    def get_snapshot(self) -> Dict[str, Any]:
        """API server 用:當前所有監控帳號的實時狀態快照(對齊 MonitorManager)。"""
        now = time.time()
        rows = []
        for a in list(self._states):
            lv = getattr(a, "last_values", {}) or {}
            last_ts = float(getattr(a, "last_check_ts", 0.0) or 0.0)
            rows.append({
                "name": getattr(a, "name", ""),
                "profile_id": getattr(a, "profile_id", ""),
                "selected": bool(getattr(a, "selected", True)),
                "monitor_selected": bool(getattr(a, "monitor_selected", True)),
                "running": bool(getattr(a, "running", False)),
                "status": getattr(a, "status", "") or "",
                "suspended": bool(getattr(a, "suspended", False)),
                "item_count": int(lv.get("item_count", 0) or 0),
                "paid_to_ship": int(lv.get("paid_to_ship", 0) or 0),
                "cod": int(lv.get("cod", 0) or 0),
                "im": int(lv.get("im", 0) or 0),
                "im_persist_rounds": int(getattr(a, "im_persist_rounds", 0) or 0),
                "last_error": str(getattr(a, "last_error", "") or "")[:200],
                "last_check_ts": last_ts,
                "last_check_age_sec": int(now - last_ts) if last_ts > 0 else None,
                "next_run_ts": float(getattr(a, "next_run_ts", 0.0) or 0.0),
                "held_reasons": sorted(self._holds.get(getattr(a, "profile_id", ""), set())),
                "fail_count": self._fail_count.get(getattr(a, "profile_id", ""), 0),
            })
        return {"updated_at": now, "accounts": rows}

    async def stop(self):
        self._stop_event.set()

    def reset_account_state(self, profile_id: str) -> dict:
        """v6.1.58:清掉單一帳號的所有 in-memory poll state,模擬「單獨重啟」效果。

        用於 stuck 帳號(連續 Yahoo 5xx 但其他帳號正常)— 強制下一輪 poll 用全新
        connection + 全新 fail counter,不再被 backoff / 異常退避影響。

        清的內容:
          - _yahoo_5xx_fail_count[pid]      → 0
          - _myauc_fail_count[pid]          → 0
          - _abnormal_count[pid]            → 0
          - _fail_count[pid]                → 0(連續失敗 backoff)
          - _mark_read_ts[pid]              → 不動(BOSH 路徑用的)
          - _nickname_cache[pid]            → 不動(跨重啟也保留)
          - status                          → "在线"(由 on_update 廣播,讓 UI 立刻更新,
                                                下輪 poll 失敗會再正確 patch)

        不清的內容:
          - cookies(JSON cache + Chrome SQLite)— user 已驗證重啟也沒清這個,所以不動
          - profile lock / Chrome session
          - 帳號 monitor_selected 狀態

        Returns: 摘要 dict(供 UI log 顯示)
        """
        cleared = {}
        for attr_name in ("_yahoo_5xx_fail_count", "_myauc_fail_count",
                          "_abnormal_count", "_fail_count"):
            attr = getattr(self, attr_name, None)
            if isinstance(attr, dict) and profile_id in attr:
                cleared[attr_name] = attr.pop(profile_id, 0)

        # 同步推 status="在线" 讓 UI 立刻反應
        # 下一輪 poll 如果還是失敗會再正確 patch 回「異常」
        try:
            self.on_update(profile_id, {"status": "在线", "last_error": ""})
        except Exception:
            pass

        # 喚醒該帳號 poll loop(如果在 sleep)— 用 broadcast event,讓它立刻醒
        try:
            ev = getattr(self, "_wakeup_events", {}).get(profile_id)
            if ev is not None:
                ev.set()
        except Exception:
            pass

        return cleared

    # ─── 主迴圈 ───

    async def run_forever(self):
        """錯峰啟動每帳號的 polling task。"""
        selected = [s for s in self._states if getattr(s, "monitor_selected", True)]
        total = len(self._states)
        active = len(selected)
        skipped = [getattr(s, "name", "?") for s in self._states if not getattr(s, "monitor_selected", True)]
        self.on_log(
            f"[PUREHTTP-MON] 啟動 {active}/{total} 個帳號"
            + (f" (跳過未勾選: {', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''})" if skipped else "")
        )
        # v6.1.10:啟動 stagger 動態 scale — N 帳號分散在 max(60s, N/1.5) 視窗
        # 確保啟動瞬間每秒 ≤ 1.5 帳號 poll(配合每帳號 3 HTTP = ~5 HTTP/秒,Yahoo 舒適)
        active_stagger = max(_STAGGER_WINDOW_SEC, int(active / _TARGET_POLL_RATE_PER_SEC))
        # v6.1.45:存 active_count 供「異常」狀態慢速退避用
        self._active_count = active
        # v6.1.45:記錄啟動時間,用於 global slow mode 啟動 10 分鐘內不啟用判斷
        self._start_ts = _now_ts()
        # 同步動態 scale 每帳號 poll interval 基準(讓常態負載也不超過 1.5/秒)
        self._dyn_interval_base = max(
            _INTERVAL_BASE_SEC,
            int(active / _TARGET_POLL_RATE_PER_SEC),
        )
        self.on_log(
            f"[PUREHTTP-MON] 動態調整:{active} 帳號 → 啟動 stagger {active_stagger}s, "
            f"輪詢中位 {self._dyn_interval_base}s(目標 ≤ {_TARGET_POLL_RATE_PER_SEC}/秒)"
        )
        tasks = []
        for st in selected:
            stagger = random.uniform(0, active_stagger)
            tasks.append(asyncio.create_task(self._account_loop(st, stagger)))

        # ✅ daily cleanup task — 每 24h 刪除 >N 天無活動的 topic 釋放 1000 配額
        tasks.append(asyncio.create_task(self._cleanup_loop()))

        # ✅ orphan backfill task — 掃 store 沒 backfilled_ts 的 topic 主動補拉
        tasks.append(asyncio.create_task(self._backfill_orphans_on_start()))

        # ✅ v6.1:訂單中心啟動掃描 — ensure topic + push 所有現有訂單摘要
        tasks.append(asyncio.create_task(self._startup_order_scan(selected)))

        # ✅ v6.1:每日 21:00 訂單中心業績總結 cron
        tasks.append(asyncio.create_task(self._daily_summary_loop(selected)))

        try:
            # 等所有 task 完成或 stop 觸發
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.on_log("[PUREHTTP-MON] 已停止")

    async def _startup_order_scan(self, selected: List[Any]) -> None:
        """v6.1:軟件啟動時 ensure 訂單中心 topic + 重建 _order_snapshot + 推摘要。

        改動:
        - HTML mode + dedupe(同日重啟不重推摘要)
        - 重建 _order_snapshot 從 store last_status(防漏推遺漏狀態變更)
        - 對「重啟期間狀態變了的訂單」自動補 push(_push_order_to_center is_new=False)
        """
        await asyncio.sleep(5)
        forum_bridge = getattr(self.conv_manager, "forum_bridge", None) if self.conv_manager else None
        if not forum_bridge:
            self.on_log("[PUREHTTP-MON] forum 未啟用,跳過訂單中心啟動掃描")
            self._snapshot_rebuilt.set()  # 沒啟用也要 unblock _account_loop
            return

        from .order_http import fetch_orders
        import time as _t
        import html as _html

        # v6.2:同日 dedupe 改為「數字一致才 skip」 — 防止舊摘要殘留誤導
        # 把上次摘要數字寫進 .done 檔(json),這次跟它比對,有差異就強制重推
        scan_mark_path = self.base_dir / "runtime" / "order_center" / f"scan_{_t.strftime('%Y%m%d')}.done"
        prev_summary_digest = ""
        if scan_mark_path.exists():
            try:
                prev_summary_digest = scan_mark_path.read_text(encoding="utf-8").strip()
            except Exception:
                prev_summary_digest = ""

        try:
            stats_by_chat: Dict[str, Dict[str, Any]] = {}
            for st in selected:
                profile_dir = self.base_dir / "profiles" / st.profile_id
                if not profile_dir.exists():
                    continue
                try:
                    orders, err = await asyncio.get_event_loop().run_in_executor(
                        None, fetch_orders, profile_dir,
                    )
                    if err or not orders:
                        continue
                except Exception:
                    continue

                # v6.1:重建 _order_snapshot + 自動補推「重啟期間變過狀態」的訂單
                # v6.1.18:比 classify_order() 輸出而不是 raw status,
                #          防 buyerCancel↔canceled 同類字串切換觸發 spam push
                from .order_http import classify_order as _cls_o
                pid = st.profile_id
                snap = self._order_snapshot.setdefault(pid, {})
                catch_up_count = 0
                for o in orders:
                    oid = o.get("order_id", "")
                    cur_status = o.get("status", "")
                    cur_payment = o.get("payment_status", "")
                    if not oid:
                        continue
                    # 從 store 取上次記錄的 status + payment
                    prev_msg = forum_bridge.store.get_order_msg(oid)
                    store_last_status = prev_msg.get("last_status", "") if prev_msg else ""
                    store_last_payment = prev_msg.get("last_payment", "") if prev_msg else ""
                    # 寫進 _order_snapshot(改存 tuple,完整 status+payment 給 diff 用)
                    snap[oid] = (cur_status, cur_payment)
                    # 自動補推:store 有紀錄 + 分類變化才補推(同類視為無變化)
                    if store_last_status:
                        prev_cls = _cls_o(store_last_status, store_last_payment)
                        cur_cls = _cls_o(cur_status, cur_payment)
                        if prev_cls != cur_cls:
                            buyer_id = o.get("buyer_id", "")
                            if buyer_id:
                                try:
                                    await asyncio.get_event_loop().run_in_executor(
                                        None, self._push_order_to_center,
                                        forum_bridge, st, buyer_id, o, "", False, store_last_status,
                                    )
                                    catch_up_count += 1
                                except Exception as _e_cu:
                                    self.on_log(f"[PUREHTTP-MON] 補推 {oid} 異常: {_e_cu}")
                self._order_first_run[pid] = True  # 標記 first_run done(restored)
                if catch_up_count > 0:
                    self.on_log(f"[PUREHTTP-MON] {st.name} 啟動補推 {catch_up_count} 筆狀態變更")

                # v6.1:統一 classify_order 統計 + 加金額 KPI + 收集行動項訂單詳情
                from .order_http import classify_order
                waiting_paid = waiting_unpaid = shipped = completed = overdue = 0
                wp_amt = wu_amt = sh_amt = od_amt = 0
                action_items = []  # 收集 (cls, order) for 行動項詳情
                for o in orders:
                    cls = classify_order(
                        o.get("status", ""), o.get("payment_status", ""),
                        o.get("status_label", ""), o.get("status_extra", ""),
                    )
                    amt = o.get("amount", 0) or 0
                    if cls == "overdue":
                        overdue += 1
                        od_amt += amt
                        action_items.append(("overdue", o))
                    elif cls == "waiting_paid":
                        waiting_paid += 1
                        wp_amt += amt
                        action_items.append(("waiting_paid", o))
                    elif cls == "waiting_unpaid":
                        waiting_unpaid += 1
                        wu_amt += amt
                    elif cls in ("shipped", "picked_up"):
                        shipped += 1
                        sh_amt += amt
                    elif cls == "completed":
                        completed += 1
                    # canceled / refunded / unknown 不算進主統計(已關閉)

                try:
                    group_chat = forum_bridge._resolve_chat_id_for_profile(st.profile_id)
                except Exception:
                    group_chat = str(forum_bridge.bot.forum_chat_id)

                entry = stats_by_chat.setdefault(group_chat, {
                    "wp": 0, "wu": 0, "sh": 0, "cp": 0, "od": 0,
                    "wp_amt": 0, "wu_amt": 0, "sh_amt": 0, "od_amt": 0,
                    "action_items": [],  # [(cls, acc, order), ...]
                })
                entry["wp"] += waiting_paid
                entry["wu"] += waiting_unpaid
                entry["sh"] += shipped
                entry["cp"] += completed
                entry["od"] += overdue
                entry["wp_amt"] += wp_amt
                entry["wu_amt"] += wu_amt
                entry["sh_amt"] += sh_amt
                entry["od_amt"] += od_amt
                # 行動項詳情(逾期 + 待出貨)收進 entry,優先在訊息頂部展示
                for cls, o in action_items:
                    entry["action_items"].append((cls, st.name, o))

            # v6.1:snapshot rebuild 完成 → unblock _account_loop poll
            self._snapshot_rebuilt.set()

            # v6.2:摘要 digest 比對 — 數字跟上次一樣才 skip,有變化強制重推讓用戶看新數字
            cur_digest_parts = []
            for gc in sorted(stats_by_chat.keys()):
                e = stats_by_chat[gc]
                cur_digest_parts.append(f"{gc}:wp{e['wp']}wu{e['wu']}sh{e['sh']}od{e['od']}")
            cur_digest = "|".join(cur_digest_parts)
            if prev_summary_digest and prev_summary_digest == cur_digest:
                self.on_log(f"[PUREHTTP-MON] 訂單中心 snapshot 重建完成 (數字無變化,跳過摘要 push)")
                return
            if prev_summary_digest:
                self.on_log(f"[PUREHTTP-MON] 訂單中心:數字有變化,強制重推摘要 (舊={prev_summary_digest} 新={cur_digest})")

            # v6.1:每個 group push 「行動項優先」儀表板訊息
            for group_chat, entry in stats_by_chat.items():
                try:
                    topic_id, err = await asyncio.get_event_loop().run_in_executor(
                        None, forum_bridge.ensure_order_center_topic, group_chat,
                    )
                    if not topic_id:
                        self.on_log(f"[PUREHTTP-MON] 訂單中心 topic 建失敗 {group_chat}: {err}")
                        continue

                    from .order_http import _esc
                    lines = [
                        f"📋 <b>訂單儀表板</b> · {_t.strftime('%m/%d %H:%M')}",
                        "━━━━━━━━━━━━━━━━━━━",
                    ]

                    # ── Section 1:必須行動(逾期 + 待出貨)── 最頂、最突出
                    has_action = entry["od"] > 0 or entry["wp"] > 0
                    if has_action:
                        lines.append("⚠️ <b>需要立刻處理</b>")
                        if entry["od"] > 0:
                            lines.append(f"🚨 出貨逾期 <b>{entry['od']} 筆</b> · NT${entry['od_amt']:,}")
                            # 列前 3 筆逾期詳情(訂單號 + 帳號 + 商品)
                            od_items = [(a, o) for cls, a, o in entry["action_items"] if cls == "overdue"]
                            for acc, o in od_items[:3]:
                                oid = (o.get("order_id", "") or "")[-10:]
                                its = o.get("items") or []
                                title = (its[0].get("title", "")[:22] if its else "")
                                lines.append(f"   • [{_esc(acc)}] <code>{_esc(oid)}</code> · {_esc(title)}")
                            if len(od_items) > 3:
                                lines.append(f"   <i>...還有 {len(od_items) - 3} 筆</i>")
                        if entry["wp"] > 0:
                            lines.append(f"🔴 待出貨 <b>{entry['wp']} 筆</b> · NT${entry['wp_amt']:,}")
                            wp_items = [(a, o) for cls, a, o in entry["action_items"] if cls == "waiting_paid"]
                            for acc, o in wp_items[:3]:
                                oid = (o.get("order_id", "") or "")[-10:]
                                its = o.get("items") or []
                                title = (its[0].get("title", "")[:22] if its else "")
                                lines.append(f"   • [{_esc(acc)}] <code>{_esc(oid)}</code> · {_esc(title)}")
                            if len(wp_items) > 3:
                                lines.append(f"   <i>...還有 {len(wp_items) - 3} 筆</i>")
                    else:
                        lines.append("✅ <b>沒有需要處理的訂單!</b>")

                    # ── Section 2:進行中(等買家,informational)── 低調
                    if entry["wu"] > 0 or entry["sh"] > 0:
                        lines.append("")
                        lines.append("💤 <i>進行中(等買家,不需處理)</i>")
                        if entry["wu"] > 0:
                            lines.append(f"   🟡 {entry['wu']} 筆等付款 · 潛在 NT${entry['wu_amt']:,}")
                        if entry["sh"] > 0:
                            lines.append(f"   🔵 {entry['sh']} 筆已出貨等收貨 · 已成交 NT${entry['sh_amt']:,}")

                    lines.append("")
                    lines.append("━━━━━━━━━━━━━━━━━━━")
                    lines.append("<i>按下面 button 查詳細 / 打 /orders 用指令</i>")
                    summary = "\n".join(lines)

                    _gc = group_chat  # 變數 capture for lambda
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda gc=_gc, txt=summary: forum_bridge.push_to_order_center(
                            account_name="🏁 啟動掃描",
                            html_text=txt,
                            group_chat_id=gc,
                        ),
                    )
                except Exception as e:
                    self.on_log(f"[PUREHTTP-MON] 啟動掃描 push 異常 group={group_chat}: {e}")

            # 標記同日已掃
            try:
                scan_mark_path.parent.mkdir(parents=True, exist_ok=True)
                # v6.2:寫入 digest(數字快照)而非單純 timestamp,讓下次能比對差異
                scan_mark_path.write_text(cur_digest, encoding="utf-8")
            except Exception:
                pass

            self.on_log(f"[PUREHTTP-MON] 訂單中心啟動掃描完成 ({len(stats_by_chat)} group)")
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] _startup_order_scan 異常: {e}")
        finally:
            # v6.1:不管成功失敗都 set,避免 _account_loop 卡死等不到
            self._snapshot_rebuilt.set()

    async def _daily_summary_loop(self, selected: List[Any]) -> None:
        """v6.1:每天 21:00(台灣時間)push 業績總結到訂單中心。

        - 自動排程,不需 user 主動觸發
        - dedupe:同日只 push 一次(寫 runtime 標記)
        """
        from datetime import datetime, timezone, timedelta
        TW_TZ = timezone(timedelta(hours=8))
        TARGET_HOUR = 21  # 每天 21:00 push

        # 等 30 秒讓 monitor 啟動穩定
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            return  # 真 stop 才結束
        except asyncio.TimeoutError:
            pass  # timeout 表示要繼續

        while not self._stop_event.is_set():
            try:
                now = datetime.now(TW_TZ)
                target = now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
                if target <= now:
                    # 今天 21:00 已過 → 明天 21:00
                    target = target + timedelta(days=1)
                wait_sec = (target - now).total_seconds()
                self.on_log(f"[PUREHTTP-MON] 訂單中心:下次業績總結 {target.strftime('%m/%d %H:%M')}({int(wait_sec/60)} 分後)")
                # 等到目標時間 或 stop
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                    return  # stop 觸發
                except asyncio.TimeoutError:
                    pass

                # 21:00 到 — push 業績總結到所有 group
                forum_bridge = getattr(self.conv_manager, "forum_bridge", None) if self.conv_manager else None
                if not forum_bridge:
                    continue

                # 同日 dedupe
                today_str = datetime.now(TW_TZ).strftime("%Y%m%d")
                mark_path = self.base_dir / "runtime" / "order_center" / f"daily_summary_{today_str}.done"
                if mark_path.exists():
                    self.on_log("[PUREHTTP-MON] 訂單中心:今日業績總結已 push 過,跳過")
                    continue

                await self._push_daily_summary(selected, forum_bridge)
                try:
                    mark_path.parent.mkdir(parents=True, exist_ok=True)
                    mark_path.write_text(str(int(datetime.now(TW_TZ).timestamp())), encoding="utf-8")
                except Exception:
                    pass
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] _daily_summary_loop 異常(繼續): {e}")
                # v6.1 修:異常後等 1 小時 continue 而非 return,否則 cron 永久死掉
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=3600)
                    return  # 真 stop 才退
                except asyncio.TimeoutError:
                    continue  # timeout 表示 1h 過了,繼續下一輪

    async def _push_daily_summary(self, selected: List[Any], forum_bridge) -> None:
        """執行業績總結 push 到每個 group。"""
        from .order_http import fetch_orders, _esc
        from datetime import datetime, timezone, timedelta
        import html as _html

        TW_TZ = timezone(timedelta(hours=8))
        today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        today_start = datetime.now(TW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

        stats_by_chat: Dict[str, Dict[str, Any]] = {}
        for st in selected:
            profile_dir = self.base_dir / "profiles" / st.profile_id
            if not profile_dir.exists():
                continue
            try:
                orders, err = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_orders, profile_dir,
                )
                if err or not orders:
                    continue
            except Exception:
                continue

            # v6.1:用統一 classify_order
            from .order_http import classify_order
            today_shipped = 0
            today_shipped_amount = 0
            cur_wp = cur_wu = cur_sh = cur_od = 0
            for o in orders:
                status = o.get("status", "")
                payment = o.get("payment_status", "")
                items = o.get("items") or []
                amount = o.get("amount", 0)
                deliver_dt = items[0].get("deliver_datetime", "") if items else ""
                if deliver_dt:
                    try:
                        _dt = datetime.fromisoformat(deliver_dt.replace("Z", "+00:00"))
                        if _dt.tzinfo is None:
                            _dt = _dt.replace(tzinfo=TW_TZ)
                        if _dt.astimezone(TW_TZ) >= today_start:
                            today_shipped += 1
                            today_shipped_amount += amount
                    except Exception:
                        pass
                cls = classify_order(
                    status, payment,
                    o.get("status_label", ""), o.get("status_extra", ""),
                )
                if cls == "waiting_paid":
                    cur_wp += 1
                elif cls == "waiting_unpaid":
                    cur_wu += 1
                elif cls in ("shipped", "picked_up"):
                    cur_sh += 1
                elif cls == "overdue":
                    cur_od += 1

            try:
                group_chat = forum_bridge._resolve_chat_id_for_profile(st.profile_id)
            except Exception:
                group_chat = str(forum_bridge.bot.forum_chat_id)
            entry = stats_by_chat.setdefault(group_chat, {
                "ts": 0, "ta": 0, "wp": 0, "wu": 0, "sh": 0, "od": 0, "lines": [],
            })
            entry["ts"] += today_shipped
            entry["ta"] += today_shipped_amount
            entry["wp"] += cur_wp
            entry["wu"] += cur_wu
            entry["sh"] += cur_sh
            entry["od"] += cur_od
            parts = []
            if today_shipped > 0:
                parts.append(f"✅出貨 {today_shipped}")
            if cur_wp > 0:
                parts.append(f"🔴待處理 {cur_wp}")
            if parts:
                entry["lines"].append(f"  • [{_html.escape(st.name)}] " + " · ".join(parts))

        for group_chat, entry in stats_by_chat.items():
            try:
                topic_id, _ = await asyncio.get_event_loop().run_in_executor(
                    None, forum_bridge.ensure_order_center_topic, group_chat,
                )
                if not topic_id:
                    continue
                lines = [
                    f"📊 <b>今日業績總結</b>",
                    f"<i>{today_str} 21:00</i>",
                    "━━━━━━━━━━━━━━━━━━━",
                    f"<b>✅ 今日完成:</b>",
                    f"  📦 已出貨: <b>{entry['ts']}</b> 筆 ({entry['ta']:,} NT$)",
                    "",
                    f"<b>📋 當前狀態:</b>",
                    f"  🔴 待出貨(已付款): <b>{entry['wp']}</b> 筆",
                    f"  🟡 待付款: {entry['wu']} 筆",
                    f"  🔵 已出貨等收貨: {entry['sh']} 筆",
                ]
                if entry['od'] > 0:
                    lines.append(f"  🚨 出貨逾期: <b>{entry['od']}</b> 筆 ⚠️")
                if entry["lines"]:
                    lines.append("")
                    lines.append("<b>各帳號:</b>")
                    lines.extend(entry["lines"])
                lines.append("━━━━━━━━━━━━━━━━━━━")
                if entry['wp'] > 10:
                    lines.append("⚠️ 待處理較多,明天記得優先出貨")
                elif entry['wp'] == 0:
                    lines.append("🎉 全部清空了,辛苦!")
                summary = "\n".join(lines)

                _gc = group_chat
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda gc=_gc, txt=summary: forum_bridge.push_to_order_center(
                        account_name="📊 每日總結",
                        html_text=txt,
                        group_chat_id=gc,
                    ),
                )
                self.on_log(f"[PUREHTTP-MON] 每日業績總結 push 完成 group={group_chat}")
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] 每日總結 push 異常 group={group_chat}: {e}")

    async def _backfill_orphans_on_start(self) -> None:
        """軟件啟動後掃 forum store,找沒 backfilled_ts 的 topic 主動補 backfill。

        覆蓋場景:
        - 過去 skip_backfill=True 建的空白 topic
        - 舊版軟件殘留(packed 模式之前)
        - sync 範圍外但已有 topic 的 buyer

        跑一次寫 mark,跨重啟不會重複跑。
        """
        # 等主 sync 跑完一輪(避免搶 TG rate limit)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=180)
            return  # stop
        except asyncio.TimeoutError:
            pass

        forum_bridge = (
            getattr(self.conv_manager, "forum_bridge", None)
            if self.conv_manager else None
        )
        if not forum_bridge:
            return

        # 找 orphans(沒 backfilled_ts 的 entry)
        orphans = []
        try:
            for k, v in dict(forum_bridge.store._data).items():
                if not v.get("backfilled_ts"):
                    orphans.append((k, dict(v)))
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] orphan 掃描異常: {e}")
            return

        if not orphans:
            self.on_log("[PUREHTTP-MON] 啟動掃描:所有 topic 都有 backfill 紀錄 ✓")
            return

        self.on_log(
            f"[PUREHTTP-MON] 啟動掃描:{len(orphans)} 個 topic 沒 backfill,"
            f"預估 {len(orphans) * 4 / 60:.1f} 分鐘補拉"
        )

        from .accounts import load_settings as _ls_bf
        try:
            _bf_n = int(_ls_bf().get("tg_forum_backfill_count", 200) or 200)
        except Exception:
            _bf_n = 200

        loop = asyncio.get_event_loop()
        success = 0
        fail = 0
        for k, entry in orphans:
            if self._stop_event.is_set():
                break
            profile_id = entry.get("profile_id", "")
            yahoo_chat_id = entry.get("chat_id", "")
            topic_id = entry.get("topic_id", 0)
            title = entry.get("title", "")
            if not (profile_id and yahoo_chat_id and topic_id):
                continue
            # 從 title "[acc] buyer_label" 解 buyer_label
            buyer_label = yahoo_chat_id
            if "] " in title:
                try:
                    buyer_label = title.split("] ", 1)[1] or yahoo_chat_id
                except Exception:
                    pass

            def _do_backfill(k=k, profile_id=profile_id, yahoo_chat_id=yahoo_chat_id,
                              topic_id=topic_id, buyer_label=buyer_label):
                import time as _t
                try:
                    forum_bridge._backfill_history(
                        topic_id=topic_id,
                        profile_id=profile_id,
                        yahoo_chat_id=yahoo_chat_id,
                        buyer_label=buyer_label,
                        max_total=_bf_n,
                    )
                    with forum_bridge.store._lock:
                        if k in forum_bridge.store._data:
                            forum_bridge.store._data[k]["backfilled_ts"] = _t.time()
                            forum_bridge.store._save()
                    return True
                except Exception as e:
                    self.on_log(f"[PUREHTTP-MON] orphan backfill {k} 失敗: {e}")
                    return False

            ok = await loop.run_in_executor(None, _do_backfill)
            if ok:
                success += 1
            else:
                fail += 1
            # backfill 內已有 push_delay,額外間隔避免連續 push 撞 rate
            await asyncio.sleep(1.0)

        self.on_log(
            f"[PUREHTTP-MON] orphan backfill 完成:成功 {success} / 失敗 {fail}"
        )

    async def _cleanup_loop(self) -> None:
        """每日掃一次 TG forum 過期 topic + delete 釋放 1000 配額。

        settings:
            tg_forum_auto_delete_days = 730 (預設 2 年)
            tg_forum_auto_delete_enabled = True
        """
        # 啟動延遲 30 分鐘(讓首次同步先跑完),之後每 24h 一次
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=1800)
            return  # stop 觸發
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            try:
                from .accounts import load_settings as _ls
                st = _ls() or {}
                enabled = bool(st.get("tg_forum_auto_delete_enabled", True))
                days = int(st.get("tg_forum_auto_delete_days", 730) or 730)
                forum_bridge = (
                    getattr(self.conv_manager, "forum_bridge", None)
                    if self.conv_manager else None
                )
                if enabled and forum_bridge:
                    loop = asyncio.get_event_loop()
                    ok, fail = await loop.run_in_executor(
                        None, forum_bridge.cleanup_old_topics, days,
                    )
                    if ok or fail:
                        self.on_log(
                            f"[PUREHTTP-MON] daily cleanup:刪 {ok} 個,失敗 {fail} 個"
                        )
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] cleanup_loop 異常: {e}")
            # 等 24h or stop
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=86400)
                return
            except asyncio.TimeoutError:
                pass

    async def _account_loop(self, st: "AccountState", initial_delay: float) -> None:
        """單一帳號的 polling loop。"""
        if initial_delay > 0:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=initial_delay)
                return  # stop 觸發
            except asyncio.TimeoutError:
                pass

        self.on_log(f"[PUREHTTP-MON] {st.name} 開始輪詢 (delay={initial_delay:.1f}s)")

        # v6.1:等 _startup_order_scan 完成 snapshot rebuild
        # 防 startup 慢時 poll 已開跑 → snapshot 空 → _diff_push_orders 誤推所有訂單
        # 最多等 120s 後強制繼續(避免 startup 死掉時 poll 永卡)
        try:
            await asyncio.wait_for(self._snapshot_rebuilt.wait(), timeout=120)
        except asyncio.TimeoutError:
            self.on_log(f"[PUREHTTP-MON] {st.name} 等 snapshot rebuild 超時 120s,繼續 poll")

        # v6.0.83:啟動同步 — 拉所有 active channels 主動建 topic(不只等新訊息)
        # 對「全部遷移到 TG」訴求,user 啟動軟件立刻看到所有 buyer 的 topic
        try:
            await self._sync_all_channels_on_start(st)
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] {st.name} 啟動同步異常: {e}")

        while not self._stop_event.is_set():
            # 檢查 hold
            if self._holds.get(st.profile_id):
                await self._sleep_or_stop(30)
                continue

            # v6.1.14:永久停權 flag → 完全 skip(避免持續打死掉的帳號)
            # flag 7 天後自動過期重試一次,Yahoo 解封會自動偵測到
            if self._is_suspended_permanent(st.profile_id):
                if self._abnormal_count.get(st.profile_id, 0) % 360 == 0:
                    # 每 6 小時 log 一次(360 × 60s),避免完全 silent
                    self.on_log(
                        f"[PUREHTTP-MON] {st.name} 永久停權 flag 存在,完全 skip"
                        f"(7 天後自動重試,刪 profiles/{st.profile_id}/.suspended.flag 可手動重置)"
                    )
                self._abnormal_count[st.profile_id] = self._abnormal_count.get(st.profile_id, 0) + 1
                await self._sleep_or_stop(60)  # 1 分鐘 check 一次 stop_event,不浪費 myauc
                continue

            try:
                await self._poll_once(st)
                self._fail_count[st.profile_id] = 0
            except Exception as e:
                self._fail_count[st.profile_id] = self._fail_count.get(st.profile_id, 0) + 1
                self.on_log(f"[PUREHTTP-MON] {st.name} 輪詢異常({self._fail_count[st.profile_id]}): {e}")
                # 連續失敗 backoff
                if self._fail_count[st.profile_id] >= _FAIL_THRESHOLD:
                    backoff = min(_FAIL_BACKOFF_MIN_SEC * 2 ** (self._fail_count[st.profile_id] - _FAIL_THRESHOLD), _FAIL_BACKOFF_MAX_SEC)
                    self.on_log(f"[PUREHTTP-MON] {st.name} 連續失敗,backoff {backoff}s")
                    await self._sleep_or_stop(backoff)
                    continue

            # 異常狀態處理:
            # - 需登入: BOSH 也用不了 → 完全暫停,等 cookie 更新
            # - 停權: BOSH 端可用,IM monitor 照跑(myauc/訂單會在 _do_poll 內 skip)
            cur_status = getattr(st, "status", "") or ""
            if cur_status == "需登入":
                if self._abnormal_count.get(st.profile_id, 0) == 0:
                    # v6.1:把 last_error 一起 log,user 能直接看出是「cookie 過期」還是「wssid 失效」
                    last_err = (getattr(st, "last_error", "") or "")[:160]
                    self.on_log(
                        f"[PUREHTTP-MON] {st.name} 狀態=需登入,"
                        f"暫停輪詢,等 cookie 更新或最多 {_ABNORMAL_MAX_WAIT_SEC//60} 分鐘 "
                        f"| err={last_err!r}"
                    )
                self._abnormal_count[st.profile_id] = self._abnormal_count.get(st.profile_id, 0) + 1
                await self._wait_for_cookie_change(st)
                continue  # 跳過下面的 sleep,直接進下一輪 poll
            else:
                # 在線 / 停權 / 異常
                if cur_status == "停權":
                    cnt = self._abnormal_count.get(st.profile_id, 0)
                    # v6.1.14:連續確認 3 次停權 → 寫永久 flag,下輪起完全 skip
                    if cnt + 1 == _SUSPEND_CONFIRM_THRESHOLD:
                        self._mark_suspended_permanent(st.profile_id)
                        self.on_log(
                            f"[PUREHTTP-MON] {st.name} 確認停權 {_SUSPEND_CONFIRM_THRESHOLD} 次,"
                            f"標記永久 skip(7 天後自動重試 / 刪 .suspended.flag 可手動重置)"
                        )
                    elif cnt == 0 or cnt % 30 == 0:
                        self.on_log(
                            f"[PUREHTTP-MON] {st.name} 狀態=停權(連續 {cnt+1}/{_SUSPEND_CONFIRM_THRESHOLD} 次,確認後永久 skip)"
                        )
                    self._abnormal_count[st.profile_id] = cnt + 1
                else:
                    # 真正在線 / 異常:重置 abnormal_count
                    if self._abnormal_count.get(st.profile_id, 0) > 0:
                        # 之前是停權異常,現在恢復 → 清 flag
                        try:
                            fp = self._suspend_flag_path(st.profile_id)
                            if fp.exists():
                                fp.unlink()
                                self.on_log(f"[PUREHTTP-MON] {st.name} 已從停權恢復,清永久 flag")
                        except Exception:
                            pass
                    self._abnormal_count[st.profile_id] = 0

                # v6.1.45:「異常」(Yahoo server 端 5xx)狀態 → 慢速退避 5 分鐘
                # 避免 Yahoo 維護期間 200 帳號狂打加重 Yahoo 負擔(也省 user 機器資源)
                # 任何一個成功 poll → status 回「在線」,自動恢復正常 interval
                if cur_status == "異常":
                    # 異常專用 base 至少 300s(5 min),200 帳號 → base = max(300, 200/1.5*2.5) = 333s
                    # 配合 lognormvariate jitter 約 [180, 800]s
                    _act = getattr(self, "_active_count", 0)
                    base = max(300, int(_act / _TARGET_POLL_RATE_PER_SEC * 2.5)) if _act else 300
                else:
                    # v6.1.10:對數正態 jitter,base 用動態 scale 過的(配合帳號數)
                    base = getattr(self, "_dyn_interval_base", _INTERVAL_BASE_SEC)
                # v6.1.45:工作時段模擬 — 套用 TPE 時區倍率(凌晨慢 3 倍 / 夜間慢 1.5 倍)
                # 凌晨 base × 3 + 異常 base × 3 = 最壞 base × 9 但 dyn_max cap 住
                _hour_mul = _tpe_work_hour_multiplier()
                base = base * _hour_mul
                # v6.1.45:Global slow mode(全局成功率 < 80%)→ 額外 × 2.5 倍
                # 跟個別「異常」狀態獨立(個別異常可能跟全局異常同時觸發,雙倍 slow)
                if getattr(self, "_global_slow_mode", False) and cur_status != "異常":
                    base = base * 2.5
                multiplier = random.lognormvariate(0.0, _INTERVAL_SIGMA)
                interval = base * multiplier
                # 最長 cap 也跟 base 動態 scale,但不低於原 MAX
                dyn_max = max(_INTERVAL_MAX_SEC, int(base * 2.5))
                interval = max(_INTERVAL_MIN_SEC, min(dyn_max, interval))
                await self._sleep_or_stop(interval)

    def _sync_done_path(self, profile_id: str) -> Path:
        return self.base_dir / "runtime" / "sync_state" / f"{profile_id}.done"

    def _unread_snapshot_path(self, profile_id: str) -> Path:
        """v6.1:unread snapshot 跨重啟持久化路徑。

        修「重啟後重複推送」bug:_unread_snapshot 是內存 dict,重啟後是空 {},
        prev_cnt=0 → 任何 channel 有 unread 都被當「新訊息」推一次。
        持久化後 prev_cnt 是上次運行的真實值,只 push 真增量。
        """
        return self.base_dir / "runtime" / "im_snapshot" / f"{profile_id}.json"

    def _load_unread_snapshot(self, profile_id: str) -> Dict[str, int]:
        """v6.1:從 disk load 上次運行最後的 unread snapshot。"""
        try:
            import json
            fp = self._unread_snapshot_path(profile_id)
            if fp.exists():
                d = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return {str(k): int(v) for k, v in d.items()}
        except Exception:
            pass
        return {}

    def _save_unread_snapshot(self, profile_id: str, snapshot: Dict[str, int]) -> None:
        """v6.1:持久化當前 snapshot 到 disk(atomic + throttle 30s)。"""
        try:
            now = time.time()
            last = self._last_snapshot_save_ts.get(profile_id, 0)
            if now - last < 30:
                return
            self._last_snapshot_save_ts[profile_id] = now
            import json
            fp = self._unread_snapshot_path(profile_id)
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            import os
            os.replace(str(tmp), str(fp))
        except Exception:
            pass

    # v6.1.14:永久停權 flag 路徑(profile_dir 內的 .suspended.flag)
    def _suspend_flag_path(self, profile_id: str) -> Path:
        return self.base_dir / "profiles" / profile_id / ".suspended.flag"

    def _is_suspended_permanent(self, profile_id: str) -> bool:
        """檢查該帳號是否被標永久停權(< 7 天前標記)。"""
        fp = self._suspend_flag_path(profile_id)
        if not fp.exists():
            return False
        try:
            ts = int(fp.read_text(encoding="utf-8").strip())
            import time as _t
            age = _t.time() - ts
            if age > _SUSPEND_FLAG_TTL_SEC:
                # 7 天過期 → 重試一次,清 flag
                try:
                    fp.unlink()
                except Exception:
                    pass
                return False
            return True
        except Exception:
            return False

    def _mark_suspended_permanent(self, profile_id: str) -> None:
        """連續 3 次確認停權 → 寫 flag 永久 skip。"""
        fp = self._suspend_flag_path(profile_id)
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            import time as _t
            fp.write_text(str(int(_t.time())), encoding="utf-8")
        except Exception:
            pass

    # v6.1.11:版本 bump 讓 v6.1.5 silent mark(synced=0 也 mark)的舊標記自動失效
    # 過往 bug:_sync_all_channels_on_start 跑完不管 synced=0 還是 N 都永久 mark → 同事看不到 buyer topic
    _SYNC_DONE_VERSION = "v6.1.11"
    _SYNC_PENDING_RETRY_SEC = 86400  # pending 24h 後再試

    # v6.1.24:專屬 mark 給「BOSH ok 但 filter 後 0 channels」的合理空狀態
    # auto-heal 看到此 mark 不會 heal(因為 0 topic 是預期結果,非異常)
    _SYNC_EMPTY_PREFIX = "v6.1.24-empty"

    def _is_first_sync_done(self, profile_id: str) -> bool:
        """已完整 sync 過(拉到 ≥1 對話)— 後續靠 60s poll 增量,不再重掃。

        v6.1.5:
        - 舊版 mark(無版本標記)→ 視為未完成
        - pending mark(BOSH 成功但拉到 0)→ 24h 內 skip,超過 24h 重試
        - 永久 mark(v6.1.5\\n...)→ skip

        v6.1.24:
        - 新增「empty」mark(`v6.1.24-empty\\n{ts}`)— filter 後合理為 0 channels,
          auto-heal 看到此 mark 直接 return True 不觸發 heal(避免無謂重做 BOSH 拉取)。
        - 新增 auto-heal — 偵測「forum 啟用 + 一般 done mark 存在 + store 無該 profile
          任何 buyer topic」的異常狀態(過去 forum_bridge race 條件造成),自動清 mark 重 sync。
        """
        path = self._sync_done_path(profile_id)
        if not path.exists():
            return False
        try:
            content = path.read_text(encoding="utf-8").strip()
            # v6.1.24:empty mark(合理空狀態,不要 heal)
            if content.startswith(self._SYNC_EMPTY_PREFIX):
                return True
            # pending:格式 "pending\n{ts}"
            if content.startswith("pending"):
                import time as _t
                try:
                    pending_ts = int(content.split("\n", 1)[1].strip())
                except Exception:
                    pending_ts = 0
                age = _t.time() - pending_ts
                if age < self._SYNC_PENDING_RETRY_SEC:
                    self.on_log(
                        f"[PUREHTTP-MON] {profile_id} sync pending {int(age/3600)}h 前 "
                        f"(< 24h),skip 本次 sync"
                    )
                    return True
                self.on_log(
                    f"[PUREHTTP-MON] {profile_id} sync pending {int(age/3600)}h 前 "
                    f"(>= 24h),重試 sync"
                )
                return False
            # 舊版誤 mark(無版本標記 / 版本不對)→ 視為未完成
            if not content.startswith(self._SYNC_DONE_VERSION):
                self.on_log(
                    f"[PUREHTTP-MON] {profile_id} 偵測到舊版 sync mark,"
                    f"清除重 sync"
                )
                return False

            # v6.1.24 auto-heal:偵測異常 mark
            # 條件:forum 啟用 + 此 profile 在 forum store 無任何 buyer topic entry
            # 推斷此 mark 是過去 forum_bridge race condition 寫死的(正常 sync 完
            # 至少應該有 ≥1 個 "{profile_id}|*" entry)
            # 對正常使用者零影響:他們的 store 都有 buyer topic,此分支不觸發
            try:
                forum_bridge = (
                    getattr(self.conv_manager, "forum_bridge", None)
                    if self.conv_manager else None
                )
                if forum_bridge:
                    prefix = f"{profile_id}|"
                    # v6.1.24:snapshot 一份 keys 避免 race(其他 thread 可能正在 add/del)
                    # 跟 _backfill_orphans_on_start L711 用同樣的安全 pattern
                    try:
                        keys_snap = list(forum_bridge.store._data.keys())
                    except RuntimeError:
                        # iter 過程中 dict 變動:跳過此次 auto-heal,下次重啟再試
                        keys_snap = None
                    has_buyer_topic = (
                        keys_snap is None  # snapshot 失敗 → 保守:不觸發 heal
                        or any(
                            isinstance(k, str) and k.startswith(prefix)
                            for k in keys_snap
                        )
                    )
                    if not has_buyer_topic:
                        self.on_log(
                            f"[PUREHTTP-MON] {profile_id} 偵測異常 mark "
                            f"(forum 啟用但 store 無 buyer topic),清除重 sync"
                        )
                        try:
                            path.unlink()
                        except Exception:
                            pass
                        return False
            except Exception as _e_heal:
                # auto-heal 失敗不影響原 mark 判定(回退到原行為)
                self.on_log(
                    f"[PUREHTTP-MON] {profile_id} auto-heal 檢查異常: {_e_heal}"
                )
        except Exception:
            return False
        return True

    def _mark_first_sync_done(self, profile_id: str) -> None:
        """永久 mark:拉到 ≥1 對話且成功處理完。"""
        try:
            path = self._sync_done_path(profile_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            import time as _t
            path.write_text(
                f"{self._SYNC_DONE_VERSION}\n{int(_t.time())}",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _mark_first_sync_empty(self, profile_id: str) -> None:
        """v6.1.24:標記「BOSH 拉到 channels 但 filter 90 天後 0 個」的合理空狀態。

        跟 _mark_first_sync_done 不同:
        - done mark 表示「sync 跑完,有建 topic」→ auto-heal 看到 store 0 topic 會誤 heal
        - empty mark 表示「sync 跑完,合理沒建 topic(都是老對話)」→ auto-heal 不碰

        效果:全是 >90 天老對話的帳號每次重啟不會重複跑 BOSH(省 ~3 秒/次)。
        """
        try:
            path = self._sync_done_path(profile_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            import time as _t
            path.write_text(
                f"{self._SYNC_EMPTY_PREFIX}\n{int(_t.time())}",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _mark_sync_pending(self, profile_id: str) -> None:
        """v6.1.5:暫存 mark — BOSH 成功但拉到 0,24h 後重試。

        防範:剛登入帳號 session 還沒 warm up 第一次拉空,不該永久放棄。
        """
        try:
            path = self._sync_done_path(profile_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            import time as _t
            path.write_text(
                f"pending\n{int(_t.time())}",
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _sync_all_channels_on_start(self, st: "AccountState") -> None:
        """首次啟動:拉該帳號所有 active channels → 主動建 topic + backfill。

        關鍵設計(業界 helpdesk 同模式):
        - 首次跑完整掃描(分頁拉所有歷史 active 對話,不只前 30 個)
        - 持久化 mark 檔 runtime/sync_state/{profile_id}.done
        - 後續啟動偵測 mark 檔存在 → skip(只靠 60s poll diff 增量)
        - TG 本身是 archive,訊息不會丟,不需要重複 fetch
        """
        # 已 sync 過就跳過(後續訊息靠 60s poll diff push)
        if self._is_first_sync_done(st.profile_id):
            return

        # forum 沒啟用就跳過(不寫 mark,下次啟動 forum 已啟用會自動 sync)
        forum_bridge = getattr(self.conv_manager, "forum_bridge", None) if self.conv_manager else None
        if not forum_bridge:
            # v6.1.24:不再 _mark_first_sync_done — 過去這裡寫死 mark,如果啟動瞬間
            # forum_bridge 還沒 ready(race condition),會永久 skip sync 即使後來
            # forum 啟用了,導致同事「軟件跑 2 天卻沒同步 buyer topic」的 bug
            return

        profile_dir = self.base_dir / "profiles" / st.profile_id
        if not profile_dir.exists():
            return

        def _do():
            """分頁拉所有 active channels(用 lastMsgTime markTS 往前拉)。

            v6.1.3:返回 (channels, bosh_ok)。bosh_ok=False 表示 BOSH session 完全失敗
            (例:JWT 未 cache / cookies 過期 / 網路斷)→ 上層不要 mark sync done,下次重試。
            """
            all_chs = []
            bosh_ok = False
            try:
                from .yahoo_im_bosh_ext import BOSHSession
                with BOSHSession(profile_dir, on_log=self.on_log) as s:
                    bosh_ok = True  # session 建起來了
                    mark_ts = 0
                    for _page in range(20):  # 最多 20 頁 × 100 = 2000 對話
                        body = {
                            "ascSort": False,
                            "unread": 0,  # 0 = all
                            "count": 100,
                        }
                        if mark_ts:
                            body["lastMsgTime"] = mark_ts
                        resp, err = s.iq(
                            "list_channels_by_lastmsgtime", body, iq_type="get",
                        )
                        if err or not isinstance(resp, dict):
                            # iq 失敗:標 bosh_ok=False 讓上層下次重試
                            self.on_log(
                                f"[PUREHTTP-MON] {st.name} list_channels iq 失敗 page={_page}: {err}"
                            )
                            bosh_ok = (_page > 0)  # 第 0 頁失敗算徹底失敗;後續頁失敗保留已拉到的
                            break
                        page_chs = resp.get("channels", resp.get("result", [])) or []
                        # v6.1.5:診斷 log — 看 BOSH 真的回什麼(找出「拉到 0」根因)
                        if _page == 0:
                            resp_keys = list(resp.keys()) if isinstance(resp, dict) else []
                            self.on_log(
                                f"[PUREHTTP-MON] {st.name} BOSH page=0 channels={len(page_chs)} "
                                f"resp_keys={resp_keys}"
                            )
                        if not page_chs:
                            break
                        all_chs.extend(page_chs)
                        if len(page_chs) < 100:
                            break  # 沒更多
                        # 下一頁 markTS = 本頁最舊 channel 的 lastMsgTime
                        oldest = min(
                            (c.get("lastMsgTime", 0) or 0) for c in page_chs
                        )
                        if oldest <= 0 or oldest == mark_ts:
                            break
                        mark_ts = oldest
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] {st.name} list_channels 異常: {e}")
                bosh_ok = False
            return all_chs, bosh_ok

        loop = asyncio.get_event_loop()
        channels, bosh_ok = await loop.run_in_executor(None, _do)
        # v6.1.3:BOSH session 完全失敗時不 mark done,下次重啟再試
        # (修「第一次啟動 JWT/BOSH 未準備好 → 拉到空 → mark done → 永遠 skip」bug)
        if not bosh_ok:
            self.on_log(
                f"[PUREHTTP-MON] {st.name} BOSH list_channels 完全失敗,"
                f"不標記 sync done,下次重啟會重試"
            )
            return
        if not channels:
            # v6.1.5:BOSH 成功但拉到 0 對話 — 可能是:
            # a) 真的沒 IM 歷史(新登入帳號常見)
            # b) Yahoo session 還沒 warm up(剛登入第一次拉返空)
            # c) Yahoo 對該帳號 channel list 有問題
            # 不再立刻永久 mark done。改成寫「pending」標記,24h 後重試一次。
            # 真的沒歷史的帳號每天最多嘗試 1 次(可接受),有歷史的能自愈。
            self.on_log(
                f"[PUREHTTP-MON] {st.name} BOSH 成功但拉到 0 對話 — "
                f"標記 pending,24h 後重試"
            )
            self._mark_sync_pending(st.profile_id)
            return

        # ✅ 過濾:預設只同步「近 N 天」有訊息的對話 topic,避免冷對話佔滿 TG 1000 上限
        # settings: tg_forum_sync_days(預設 90 = 3 個月, 設 0 = 全部建)
        # 冷對話不會丟失:新訊息來時 _forward_to_forum → ensure_topic 會自動建
        try:
            from .accounts import load_settings as _ls
            sync_days = int(_ls().get("tg_forum_sync_days", 90) or 90)
        except Exception:
            sync_days = 90
        if sync_days > 0:
            import time as _t
            cutoff_ms = int((_t.time() - sync_days * 86400) * 1000)
            before = len(channels)
            channels = [
                c for c in channels
                if int(c.get("lastMsgTime", 0) or 0) >= cutoff_ms
            ]
            self.on_log(
                f"[PUREHTTP-MON] {st.name} 過濾近 {sync_days} 天活躍 → "
                f"{before} → {len(channels)} 個對話"
            )

        if not channels:
            # v6.1.24:寫 empty mark 不寫 done mark
            # done mark 會被 auto-heal 誤觸(store 0 topic 看似異常)
            # empty mark 表示「sync 跑完,合理 0 channel(都過 90 天)」→ auto-heal 不碰
            self._mark_first_sync_empty(st.profile_id)
            self.on_log(
                f"[PUREHTTP-MON] {st.name} 首次同步:filter 後 0 channels(都 >90 天)"
                f"→ 標 empty mark,下次重啟 skip"
            )
            return

        self.on_log(
            f"[PUREHTTP-MON] {st.name} 首次完整同步 {len(channels)} 個對話到 forum"
            "(完成後不再重掃,後續靠 60s 增量 poll)"
        )

        # ✅ 排序修正:按 lastMsgTime ASC(最舊先建)→ 最新對話最後建
        # 因 TG forum 列表按 topic top_message.date 排序,先建的 topic 資訊卡時間最早 → 排最下
        # 倒序建立後,最新對話排最上,跟 Yahoo IM list 順序一致
        try:
            channels = sorted(
                channels, key=lambda c: int(c.get("lastMsgTime", 0) or 0),
            )
        except Exception:
            pass

        # 對每個 channel 建 topic(ensure_topic 內會 backfill 歷史)
        def _resolve_my_id():
            try:
                from .yahoo_im_jwt import ensure_bosh_jwt
                _, user, _ = ensure_bosh_jwt(profile_dir)
                return user
            except Exception:
                return ""

        my_user = await loop.run_in_executor(None, _resolve_my_id)
        my_id_l = (my_user or "").lower()

        synced = 0
        for ch in channels:
            cid = ch.get("chID") or ch.get("channelId") or ""
            if not cid:
                continue
            # 從 channel_id 解出 buyer Y-id
            parts = cid.split(":")
            if len(parts) != 3:
                continue
            buyer = ""
            for p in parts[1:]:
                if p and p.lower() != my_id_l:
                    buyer = p
                    break
            if not buyer:
                continue
            buyer_y = buyer.upper() if not buyer.startswith("Y") else buyer

            # backfill 策略:packed 模式 1 對話 ~1-3 個 TG msg,所有 topic 都 backfill
            # settings: tg_forum_backfill_recent_days(預設 0 = 全部 backfill)
            try:
                from .accounts import load_settings as _ls2
                _bf_days = int(_ls2().get("tg_forum_backfill_recent_days", 0) or 0)
            except Exception:
                _bf_days = 0
            if _bf_days > 0:
                import time as _t_bf
                _cutoff_bf = int((_t_bf.time() - _bf_days * 86400) * 1000)
                should_backfill = int(ch.get("lastMsgTime", 0) or 0) >= _cutoff_bf
            else:
                should_backfill = True  # 0 = 全部 backfill

            # 拿暱稱 + 建 topic(reuse 既有 topic 不會重建)
            try:
                def _build():
                    cache = self._nickname_cache.setdefault(st.profile_id, {})
                    label = cache.get(buyer_y, "") or buyer_y
                    if label == buyer_y:
                        try:
                            from .im_http_ops import _build_session
                            from .client_runtime_compat import get_api_headers
                            sess, _, _ = _build_session(profile_dir, buyer_cid=buyer_y)
                            if sess:
                                r = sess.get(
                                    f"https://tw.bid.yahoo.com/fe/api/im/users?userIds={buyer_y}",
                                    headers=get_api_headers(), timeout=8,
                                )
                                if r.status_code == 200:
                                    users = r.json().get("users") or []
                                    if users:
                                        nick = users[0].get("nickname") or users[0].get("displayName") or ""
                                        if nick:
                                            label = nick
                                            cache[buyer_y] = nick
                        except Exception:
                            pass
                    # backfill 策略:有未讀的對話拉歷史(讓使用者看到上下文),
                    # 0 未讀的老對話只建空 topic + 資訊卡(節省 TG API 配額)
                    forum_bridge.ensure_topic(
                        profile_id=st.profile_id,
                        yahoo_chat_id=buyer_y,
                        buyer_label=label,
                        account_name=st.name,
                        skip_backfill=not should_backfill,
                    )
                await loop.run_in_executor(None, _build)
                synced += 1
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] {st.name} ensure {buyer_y} 異常: {e}")

            # 每建 1 個 topic 之間小 sleep(避免 TG createForumTopic flood)
            await asyncio.sleep(0.5)

        # v6.1.11:成功建立 >= 1 個 topic 才永久 mark;synced=0 改 pending(24h 重試)
        # 修同事「TG 群組沒看到 buyer topic 但 sync 已 mark done」bug
        if synced == 0:
            self.on_log(
                f"[PUREHTTP-MON] {st.name} 首次同步:{len(channels)} 個 channels 但 ensure_topic "
                f"全失敗 → 標記 pending,24h 後重試(看上方有無「ensure XXX 異常」log)"
            )
            self._mark_sync_pending(st.profile_id)
        else:
            self.on_log(
                f"[PUREHTTP-MON] {st.name} 首次同步完成 {synced}/{len(channels)} 個 topics"
                " → 下次重啟自動 skip,只靠增量 poll"
            )
            self._mark_first_sync_done(st.profile_id)

    def _chrome_cookies_newer_than_cache(self, profile_dir: Path) -> bool:
        """偵測:Chrome SQLite Cookies(user 瀏覽器登入後寫的)是否比軟件 cache 新?

        是的話表示 user 在 Chrome 內登入過,軟件 cache 沒同步。
        應該觸發 Playwright extract 把 fresh cookies 寫進 cache。
        """
        cache_fp = profile_dir / "cookie_cache.json"
        chrome_fp = profile_dir / "Default" / "Network" / "Cookies"
        if not chrome_fp.exists():
            chrome_fp = profile_dir / "Default" / "Cookies"
        if not chrome_fp.exists():
            return False
        try:
            cache_mt = cache_fp.stat().st_mtime if cache_fp.exists() else 0
            chrome_mt = chrome_fp.stat().st_mtime
            return chrome_mt > cache_mt + 60  # 加 60s buffer 避免 noise
        except Exception:
            return False

    async def _attempt_cookie_refresh(self, st: "AccountState", profile_dir: Path) -> bool:
        """用 Playwright 開 profile 訪問 myauc → 提取 fresh cookies + wssid → 寫 cache。

        - 15 分鐘 cooldown 防爆(每帳號最多 4 次/小時)
        - 完成後 cookie_cache.json mtime 變化 → 下次 poll 用新 cookies
        - 返 True = refresh 成功且寫了 cache
        """
        import time as _t
        last = self._last_cookie_refresh.get(st.profile_id, 0)
        if _t.time() - last < 900:
            return False
        self._last_cookie_refresh[st.profile_id] = _t.time()

        try:
            from .accounts import load_settings
            from .merch_http_ops import extract_auth_session
            from .cookie_store import save_cookie_cache
            chrome_path = (load_settings() or {}).get("browser_path", "") or "chrome"
            self.on_log(f"[PUREHTTP-MON] {st.name} Chrome SQLite 比 cache 新,跑 Playwright 同步 cookie...")
            session = await extract_auth_session(
                profile_dir=profile_dir,
                chrome_path=chrome_path,
                headless=True,
                log=self.on_log,
            )
            if not session or not session.cookies:
                self.on_log(f"[PUREHTTP-MON] {st.name} cookie 同步失敗(Playwright 拿不到 session)")
                return False
            # v6.1.45:區分「明確 isLogin=False」vs「解析失敗 unknown」
            # 修「Yahoo 維護期間 isoredux 解析失敗被當登出,軟件誤推 user 重登」bug
            _is_login = getattr(session, "is_login", False)
            _login_unknown = getattr(session, "is_login_unknown", False)
            if _login_unknown:
                # 解析失敗 = Yahoo 服務異常(維護/降級),不誤判為登出,
                # 也不假裝在線 — 標「Yahoo異常」讓 user 看到真實狀況
                try:
                    self.on_update(st.profile_id, {"status": "異常",
                                                    "last_error": "Yahoo 頁面解析失敗(疑似維護)"})
                except Exception:
                    pass
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} Yahoo 頁面解析失敗(可能維護中)→ 標 Yahoo異常,"
                    f"保留現有 cookie cache(不需重登)"
                )
                return False
            if not _is_login:
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} Chrome 內 cookies 也無效"
                    f"(isLogin=False)— **需要 user 手動登入**,不寫 cache"
                )
                return False
            save_cookie_cache(
                profile_dir, session.cookies, session.wssid,
                raw_cookies=getattr(session, "raw_cookies", None),
            )
            self.on_log(f"[PUREHTTP-MON] {st.name} cookie 同步完成({len(session.cookies)} cookies, isLogin=True)")
            return True
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] {st.name} cookie refresh 異常: {e}")
        return False

    async def _wait_for_cookie_change(self, st: "AccountState") -> None:
        """異常狀態下等 cookie cache 變化(handoff 完成 / user 手動登入後寫入新 cookies)。

        每 60s 檢查一次 mtime,變化就 break 立刻 retry poll。
        最多等 30 分鐘強制醒(防漏 trigger)。
        """
        from pathlib import Path as _Path
        cookie_path = _Path(self.base_dir) / "profiles" / st.profile_id / "cookie_cache.json"
        try:
            last_mtime = cookie_path.stat().st_mtime if cookie_path.exists() else 0
        except Exception:
            last_mtime = 0

        import time as _t
        t0 = _t.time()
        while not self._stop_event.is_set():
            if _t.time() - t0 >= _ABNORMAL_MAX_WAIT_SEC:
                self.on_log(f"[PUREHTTP-MON] {st.name} 異常等待達 {_ABNORMAL_MAX_WAIT_SEC//60} 分鐘,強制 retry")
                return
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=_ABNORMAL_WATCH_INTERVAL_SEC,
                )
                return  # stop 觸發
            except asyncio.TimeoutError:
                pass
            try:
                cur_mtime = cookie_path.stat().st_mtime if cookie_path.exists() else 0
                if cur_mtime > last_mtime + 0.5:  # +0.5s 防止小數誤差
                    self.on_log(f"[PUREHTTP-MON] {st.name} cookie cache 更新,恢復輪詢")
                    return
            except Exception:
                pass

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ─── v6.1.45:全局健康監測 + global slow mode ───

    def _record_poll_result(self, success: bool) -> None:
        """每次 myauc poll 完成記錄結果(success/fail),維護近 500 次的 ring buffer。"""
        try:
            self._global_health.append((_now_ts(), bool(success)))
        except Exception:
            pass

    def _check_global_health(self) -> None:
        """檢查近 5 分鐘成功率,< 80% → 觸發 global slow mode,> 95% → 恢復。
        啟動前 10 分鐘不啟用(樣本太少防誤判)。"""
        try:
            now = _now_ts()
            start_ts = getattr(self, "_start_ts", now)
            if now - start_ts < 600:  # 啟動 10 分鐘內不啟用
                return
            # 過濾近 5 分鐘的樣本
            cutoff = now - 300
            recent = [s for (t, s) in self._global_health if t >= cutoff]
            if len(recent) < 10:  # 樣本太少(< 10),不判定
                return
            success_rate = sum(1 for s in recent if s) / len(recent)
            # 進入 / 退出 global slow mode 用滯後閾值避免閃爍
            if not self._global_slow_mode and success_rate < 0.80:
                self._global_slow_mode = True
                if now - self._global_slow_logged_ts >= 60:
                    self.on_log(
                        f"[PUREHTTP-MON] ⚠️ 全局成功率 {success_rate:.0%} < 80% "
                        f"({sum(1 for s in recent if s)}/{len(recent)} 樣本)→ "
                        f"啟用 global slow mode(所有帳號降速)"
                    )
                    self._global_slow_logged_ts = now
            elif self._global_slow_mode and success_rate > 0.95:
                self._global_slow_mode = False
                self.on_log(
                    f"[PUREHTTP-MON] ✓ 全局成功率 {success_rate:.0%} > 95% → 退出 slow mode,恢復正常"
                )
        except Exception:
            pass

    # ─── 單次輪詢 ───

    async def _poll_once(self, st: "AccountState") -> None:
        """單帳號一輪:抓 myauc stats + IM unread + diff push 新訊息。"""
        profile_dir = self.base_dir / "profiles" / st.profile_id
        if not profile_dir.exists():
            return
        # 標記 in_poll 給 wait_idle 看
        self._in_poll[st.profile_id] = True
        try:
            await self._do_poll(st, profile_dir)
        finally:
            self._in_poll[st.profile_id] = False

    async def _do_poll(self, st: "AccountState", profile_dir: Path) -> None:

        # 1. myauc HTTP stats
        # v6.1.13:傳 on_log 讓 myauc 內部診斷 log(SQLite fallback 觸發/失敗等)顯示到主 log
        loop = asyncio.get_event_loop()
        stats, err = await loop.run_in_executor(
            None, lambda: fetch_myauc_stats(profile_dir, on_log=self.on_log),
        )

        # v6.1.49 (A):myauc 5xx 立即原地重試一次(3-5s 後)
        # 修「Yahoo myauc API 偶爾 transient 500 → UI 閃紫」體驗
        # 75% 單次 5xx 抖動可以被內部吸收,不會走到「下輪重試」邏輯
        def _is_yahoo_5xx(_err: str) -> bool:
            if not _err:
                return False
            return (
                "HTTP 5" in _err or "Gateway" in _err or "BadGateway" in _err.lower()
                or "BAD_GATEWAY" in _err or "503" in _err or "502" in _err
                or "504" in _err or "500" in _err or "維護" in _err or "维护" in _err
            )

        if (not stats) and _is_yahoo_5xx(err or ""):
            _retry_wait = 3.0 + random.uniform(0, 2.0)
            self.on_log(
                f"[PUREHTTP-MON] {st.name} myauc 5xx 原地重試(等 {_retry_wait:.1f}s): "
                f"{(err or '')[:60]}"
            )
            await asyncio.sleep(_retry_wait)
            stats, err = await loop.run_in_executor(
                None, lambda: fetch_myauc_stats(profile_dir, on_log=self.on_log),
            )
            if stats:
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} myauc 5xx 重試成功(吸收單次抖動)"
                )

        # 失敗 → 嘗試 cookie refresh(條件:SQLite 比 cache 新 / cache 過 1 天 / cache 不存在 / err 含「需登入」)
        # v6.1.13:擴大 should_refresh 條件,涵蓋「cache 不存在」+「err 含需登入」
        # 修同事 cookie 同意頁/login redirect 場景 — 即使 SQLite 沒比 cache 新也試 Playwright refresh
        if not stats:
            should_refresh = self._chrome_cookies_newer_than_cache(profile_dir)
            refresh_reason = "SQLite > cache" if should_refresh else ""
            if not should_refresh:
                try:
                    cache_fp = profile_dir / "cookie_cache.json"
                    if not cache_fp.exists():
                        should_refresh = True
                        refresh_reason = "cache 不存在"
                    else:
                        import time as _t_chk
                        age = _t_chk.time() - cache_fp.stat().st_mtime
                        if age > 86400:
                            should_refresh = True
                            refresh_reason = f"cache 已 {int(age/3600)}h 老"
                        elif err and ("需登入" in err or "cookie" in err.lower() or "consent" in err.lower() or "同意" in err):
                            should_refresh = True
                            refresh_reason = f"err 含需登入/consent({err[:40]})"
                except Exception:
                    pass
            if should_refresh:
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} 觸發 Playwright cookie refresh "
                    f"(原因: {refresh_reason})"
                )
                refreshed = await self._attempt_cookie_refresh(st, profile_dir)
                if refreshed:
                    stats, err = await loop.run_in_executor(
                        None, lambda: fetch_myauc_stats(profile_dir, on_log=self.on_log),
                    )
        patch: Dict[str, Any] = {}
        skip_myauc_orders = False  # 停權帳號 myauc/訂單 skip,但 BOSH IM 照跑
        if stats:
            patch.update(stats)
            patch["status"] = "在线"
            # v6.1.8:成功 → 清失敗計數(避免歷史失敗影響後續判定)
            try:
                self._myauc_fail_count.pop(st.profile_id, None)
            except Exception:
                pass
            # v6.1.45:成功 → 也清 Yahoo 5xx 計數(Yahoo 恢復後 status 自動回「在線」)
            try:
                if hasattr(self, "_yahoo_5xx_fail_count"):
                    self._yahoo_5xx_fail_count.pop(st.profile_id, None)
            except Exception:
                pass
            # v6.1.45:記錄成功到全局健康 ring buffer
            self._record_poll_result(True)
            self._check_global_health()
        else:
            # 細分失敗原因
            err_low = (err or "").lower()
            is_clear_err = False  # 「明確錯誤」(需登入/停權)直接 patch 不 retry
            # v6.1.45:任何 myauc 失敗都記錄到全局健康(用於 global slow mode 判定)
            self._record_poll_result(False)
            self._check_global_health()
            if "cookie" in err_low or "wssid" in err_low or "未登入" in err or "未登錄" in err or "未登录" in err:
                patch["status"] = "需登入"
                is_clear_err = True
            elif "停權" in err or "停权" in err or "停用" in err or "凍結" in err or "冻结" in err:
                patch["status"] = "停權"
                skip_myauc_orders = True
                is_clear_err = True
            elif "session 不可用" in err_low or "cache 不存在" in err or "cache 已过期" in err or "cache 已過期" in err:
                patch["status"] = "需登入"
                is_clear_err = True
            elif err.startswith("RATE_LIMITED:"):
                # v6.1.45:Yahoo 主動限流限速(HTTP 429 + Retry-After)
                # 業界標準處理:該帳號退避指定秒數 + 觸發全局 slow mode(待 6.3 整合)
                try:
                    _ra_sec = int(err.split(":", 1)[1])
                except Exception:
                    _ra_sec = 60
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} ⚠️ Yahoo 主動限速(HTTP 429),"
                    f"退避 {_ra_sec}s 後重試"
                )
                # 標「異常」狀態 + 記錄 last_error
                patch["status"] = "異常"
                patch["last_error"] = f"Yahoo 限速 ({_ra_sec}s)"
                self.on_update(st.profile_id, patch)
                # 標記全局 limited(待 6.3 全局 slow mode 使用)
                try:
                    self._rate_limited_until = max(
                        getattr(self, "_rate_limited_until", 0),
                        __import__("time").time() + _ra_sec
                    )
                except Exception:
                    pass
                await self._sleep_or_stop(_ra_sec)
                return
            else:
                # v6.1.8:不明錯誤(timeout/5xx/network 等)→ 不立刻判離線
                # 連續 2 次以上才判離線,避免 100 帳號同時啟動觸發 rate limit 瞬間誤判
                # v6.1.45:HTTP 5xx / Gateway / 維護 = Yahoo server 端問題,獨立計數 + 獨立狀態
                # 修「Yahoo 維護期間越來越多帳號被誤標離線」bug,改顯示為「Yahoo異常」
                # 「離線」= 帳號本身問題(cookie 失效),user 要處理
                # 「Yahoo異常」= Yahoo server 端問題(5xx/維護),user 不用動
                _is_server_side = (
                    "HTTP 5" in err or "Gateway" in err or "BadGateway" in err.lower()
                    or "BAD_GATEWAY" in err or "503" in err or "502" in err
                    or "504" in err or "500" in err or "維護" in err or "维护" in err
                )
                if _is_server_side:
                    # Yahoo 端問題 — 獨立計數,連續 3 次才標「Yahoo異常」
                    # v6.1.49 (B):threshold 2→3 — A 的原地重試已吸收單次抖動,
                    # 連續 3 次才表示真的 Yahoo 端故障,UI 上少很多誤閃紫
                    # 真實反映 server 端狀態讓 user 看到「不是我的帳號問題」
                    if not hasattr(self, "_yahoo_5xx_fail_count"):
                        self._yahoo_5xx_fail_count = {}
                    yfn = self._yahoo_5xx_fail_count.get(st.profile_id, 0) + 1
                    self._yahoo_5xx_fail_count[st.profile_id] = yfn
                    if yfn < 3:
                        self.on_log(
                            f"[PUREHTTP-MON] {st.name} Yahoo 5xx 第 {yfn} 次({err[:60] if err else ''}),"
                            f"下輪重試"
                        )
                        if not skip_myauc_orders:
                            return
                    # 累計 3 次:標 Yahoo異常,讓 user 知道是 server 端問題而不是帳號問題
                    patch["status"] = "異常"
                    patch["last_error"] = err
                    self.on_update(st.profile_id, patch)
                    self.on_log(
                        f"[PUREHTTP-MON] {st.name} 連續 {yfn} 次 Yahoo 5xx → 標 Yahoo異常"
                        f"(等 Yahoo 恢復自動清除,不需重新登入)"
                    )
                    # v6.1.57:dump cookies + 對比一個健康帳號,診斷污染源
                    # 只在首次標異常時 dump(yfn=3 那次),後續同帳號重標不重複
                    if yfn == 3:
                        try:
                            from .cookie_store import dump_cookie_diff_for_abnormal
                            # 找一個「在線」帳號當對照
                            _healthy_dirs = []
                            for _pid, _other_st in (getattr(self, "states", {}) or {}).items():
                                if _pid == st.profile_id:
                                    continue
                                if str(getattr(_other_st, "status", "")) in ("在线", "在線"):
                                    _healthy_dirs.append(self.base_dir / "profiles" / _pid)
                                    if len(_healthy_dirs) >= 5:
                                        break
                            dump_cookie_diff_for_abnormal(
                                self.base_dir / "profiles" / st.profile_id,
                                _healthy_dirs,
                                base_dir=self.base_dir,
                                on_log=self.on_log,
                                error_context=f"連續 3 次 5xx: {err[:120] if err else ''}",
                            )
                        except Exception as _e_dump:
                            self.on_log(f"[PUREHTTP-MON] cookie-diff dump 異常(忽略): {_e_dump}")
                    if not skip_myauc_orders:
                        return

                if not hasattr(self, "_myauc_fail_count"):
                    self._myauc_fail_count = {}
                fail_n = self._myauc_fail_count.get(st.profile_id, 0) + 1
                self._myauc_fail_count[st.profile_id] = fail_n
                if fail_n < 2:
                    # 第 1 次失敗 → 不 patch status(維持原狀),下輪重試
                    self.on_log(
                        f"[PUREHTTP-MON] {st.name} myauc 第 {fail_n} 次失敗,下輪重試: {err[:80] if err else ''}"
                    )
                    if not skip_myauc_orders:
                        return  # 不 patch,下輪再試
                patch["status"] = "离线"
            patch["last_error"] = err
            self.on_update(st.profile_id, patch)
            if not skip_myauc_orders:
                return  # 需登入/離線:全 skip
            # 停權繼續往下跑 IM monitor

        # 2. BOSH IM unread
        # v6.1.48:記錄 fetch 開始時間,給 race condition 偵測用
        _fetch_started_ts = time.time()
        total, by_channel, err = await loop.run_in_executor(
            None, fetch_unread_im_total, profile_dir,
        )
        if not err:
            patch["im"] = total

        self.on_update(st.profile_id, patch)

        # v6.1.48:race condition 處理 — 若 mark_read 發生在 fetch 之後,
        # by_channel 還是 stale 舊值,強制 override 該 cid 為 0,讓 diff 正確偵測買家秒回
        # 修「買家秒回漏訊息」bug
        _mark_ts_map = self._mark_read_ts.get(st.profile_id, {})
        if _mark_ts_map and not err:
            _now_ts = time.time()
            for _cid in list(_mark_ts_map.keys()):
                _mts = _mark_ts_map.get(_cid, 0)
                if _mts >= _fetch_started_ts:
                    # mark_read 在 fetch 之後 → by_channel 是 stale → override
                    if _cid in by_channel and by_channel[_cid] > 0:
                        try:
                            self.on_log(
                                f"[PUREHTTP-MON] {st.name} race override cid=...{_cid[-30:]} "
                                f"by_channel={by_channel[_cid]}→0(mark_read 在 fetch 後 "
                                f"{_mts - _fetch_started_ts:.2f}s)"
                            )
                        except Exception:
                            pass
                        by_channel[_cid] = 0
                    # 已處理,移除 ts
                    _mark_ts_map.pop(_cid, None)
                elif _now_ts - _mts > 600:
                    # 10 分鐘前的 mark_ts 過期清掉(避免無限累積)
                    _mark_ts_map.pop(_cid, None)
                # else: mark_read 在 fetch 前 → by_channel 已反映,不 override

        # 3. diff: 偵測 channel unread 變化 → 拉新訊息 → forward
        # v6.1:snapshot 跨重啟持久化,避免重啟後重複推送
        # - disk 有 snapshot → 上次運行真實狀態,正常 diff push 增量
        # - disk 沒 snapshot → 全新冷啟動,寫 snapshot 但不 push(防一次推全部 unread)
        is_cold_start = False
        if st.profile_id not in self._unread_snapshot:
            disk_snap = self._load_unread_snapshot(st.profile_id)
            self._unread_snapshot[st.profile_id] = disk_snap
            if disk_snap:
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} 從 disk 還原 {len(disk_snap)} 個 channel snapshot"
                    f"(避免重啟後重複推送)"
                )
            else:
                # 第一次啟動沒任何記憶 → 把當前 unread 寫 disk 但不 push
                is_cold_start = True
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} IM snapshot 冷啟動 "
                    f"({len(by_channel)} 個 channel,首輪不 push,只建 snapshot)"
                )
        prev = self._unread_snapshot.get(st.profile_id, {})
        changed_channels = []
        if not is_cold_start:
            for cid, cnt in by_channel.items():
                prev_cnt = prev.get(cid, 0)
                if cnt > prev_cnt:
                    changed_channels.append((cid, prev_cnt, cnt))

        # v6.1.26:第一輪 poll catch-up — 重啟後若有 unread channel 但 diff=0(snapshot
        # 跟 Yahoo 一樣,例如 stuck case),強制當新訊息觸發 AI + topic 推送。
        # 修「重啟後有未讀但不推到 topic」bug。
        # in-memory flag,每次重啟都會做一次。is_cold_start 不做(避免首裝洗版)。
        if not is_cold_start and st.profile_id not in self._first_poll_done:
            self._first_poll_done.add(st.profile_id)
            in_changed = {c[0] for c in changed_channels}
            catch_up_count = 0
            for cid, cnt in by_channel.items():
                if cnt > 0 and cid not in in_changed:
                    changed_channels.append((cid, 0, cnt))
                    catch_up_count += 1
            if catch_up_count > 0:
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} 重啟首輪 catch-up:"
                    f"{catch_up_count} 個 unread channel(snapshot 跟 Yahoo 一致沒 diff)"
                    f"強制當新訊息觸發 AI + topic 推送"
                )

        # snapshot 更新(包含 unread=0 的 channel 也記)
        # 注意:by_channel 已在前面被 v6.1.48 race override 處理過(若有 mark_read race)
        self._unread_snapshot[st.profile_id] = dict(by_channel)
        # 持久化:
        #   - 冷啟動 → 強制寫(避免重啟反覆推)
        #   - 有 channel 變化(將要 push)→ 強制寫(v6.1.20.4 fix:防 crash loop 期間
        #       30s throttle 擋住 save,disk 沒記「已收到」,重啟後重複推同訊息)
        #   - 沒變化 → 走原本 30s throttle
        if is_cold_start or changed_channels:
            self._last_snapshot_save_ts.pop(st.profile_id, None)  # 清 throttle 強寫
        self._save_unread_snapshot(st.profile_id, dict(by_channel))

        # 4. 對每個 unread 變化的 channel 拉新訊息 + forward
        # v6.1.18:官方頻道(Y拍官方客服 / 廣告小舖)→ 只 mark_read,不推 TG
        try:
            from .accounts import load_settings as _ls_silent
            _extra_silent = (_ls_silent() or {}).get("im_silent_buyers") or []
        except Exception:
            _extra_silent = []
        for cid, prev_cnt, new_cnt in changed_channels:
            if _is_silent_channel(cid, _extra_silent):
                await self._silent_mark_read(st, cid)
                continue
            await self._forward_new_messages(st, cid, prev_cnt, new_cnt)

        # 5. 訂單 diff push(新訂單 → 對應 buyer forum topic)
        #    停權帳號跳過(訂單頁存取會被擋)
        if not skip_myauc_orders:
            orders, ord_err = await loop.run_in_executor(
                None, fetch_orders, profile_dir,
            )
            if not ord_err:
                await self._diff_push_orders(st, orders)

    async def _diff_push_orders(self, st: "AccountState", orders: List[Dict[str, Any]]) -> None:
        """偵測新訂單(snapshot diff)→ push 訂單卡到對應 buyer forum topic。

        - 啟動首輪不 push(只建 snapshot),避免一次性 push 既有 N 條老訂單
        - 後續發現新 orderId 或狀態變化 → push
        """
        # v6.1.18:snapshot 改存 (status, payment) tuple,比較走 classify_order() 同類視為無變化
        from .order_http import classify_order as _cls_o
        pid = st.profile_id
        prev = self._order_snapshot.get(pid, {})
        first_run = not self._order_first_run.get(pid, False)
        # 建本輪 snapshot — tuple 格式
        cur = {o["order_id"]: (o.get("status", ""), o.get("payment_status", "")) for o in orders if o.get("order_id")}
        self._order_snapshot[pid] = cur
        self._order_first_run[pid] = True
        if first_run:
            self.on_log(f"[PUREHTTP-MON] {st.name} 訂單 snapshot 首輪 ({len(cur)} 筆),不 push")
            return

        # 找新訂單 + 狀態變化的訂單(forum 有就推 topic,沒就推私聊)
        forum_bridge = getattr(self.conv_manager, "forum_bridge", None) if self.conv_manager else None

        for o in orders:
            oid = o.get("order_id")
            if not oid:
                continue
            # backward compat:舊 snapshot 可能是 dict[oid]=str
            prev_entry = prev.get(oid)
            if isinstance(prev_entry, tuple):
                prev_status, prev_payment = prev_entry
            elif prev_entry:
                prev_status, prev_payment = str(prev_entry), ""
            else:
                prev_status, prev_payment = "", ""
            buyer_id = o.get("buyer_id", "")
            if not buyer_id:
                continue

            is_new = oid not in prev
            if is_new:
                status_changed = False
            else:
                # 比 classify_order() 輸出,同類(例 buyerCancel↔canceled)視為無變化
                prev_cls = _cls_o(prev_status, prev_payment)
                cur_cls = _cls_o(o.get("status", ""), o.get("payment_status", ""))
                status_changed = (prev_cls != cur_cls)
            if not (is_new or status_changed):
                continue

            # 推到對應 buyer forum topic(reuse 既有 topic,沒就建新)
            # forum 未啟用 fallback 到 yahookefu_bot 私聊
            try:
                msg = format_order_for_tg(o)
                if status_changed:
                    # v6.2:format_order_for_tg 是 HTML,prepend 改用 <b> 不能用 *(衝突顯示亂碼)
                    import html as _html_esc
                    msg = (
                        f"🔄 <b>訂單狀態變更</b>: {_html_esc.escape(o.get('status_label','') or '')}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                    ) + msg
                # 訂單卡優先用 sendPhoto 帶第一個商品縮圖(enrich)
                items_img = ""
                items_list = o.get("items") or []
                if items_list:
                    items_img = (items_list[0] or {}).get("image", "") or ""
                if forum_bridge:
                    # 1) 推到該買家 topic(維持既有行為 — 每變更發新訊息)
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._push_order_card,
                        forum_bridge, st, buyer_id, msg, items_img,
                    )
                    # 2) v6.1:訂單中心邏輯(is_new → push 主卡 / status_changed → edit + reply)
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._push_order_to_center,
                            forum_bridge, st, buyer_id, o, items_img,
                            is_new, prev_status or "",
                        )
                    except Exception as _e_oc:
                        self.on_log(f"[PUREHTTP-MON] 訂單中心 push 異常(忽略): {_e_oc}")
                elif self.conv_manager and hasattr(self.conv_manager, "tg"):
                    # fallback:私聊推送(prepend 帳號+買家標識)
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.conv_manager.tg.send,
                        f"[{st.name}] {buyer_id}\n{msg}",
                    )
                self.on_log(
                    f"[PUREHTTP-MON] {st.name} push 訂單 {oid} → {buyer_id} "
                    f"({'新' if is_new else '狀態變更'}) via {'forum' if forum_bridge else '私聊'}"
                )
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] push 訂單 {oid} 異常: {e}")

    def _enrich_order_with_d1(self, order: Dict[str, Any]) -> None:
        """v6.1.44:訂單推送前查 D1,把每個 item 的貨源資訊填到 item["d1_*"] 欄位。

        item["url"] → extract yahoo_id → query D1 worker → classify source
        填入: d1_yahoo_id / d1_source (xianyu/mercari) / d1_source_url / d1_barcode

        失敗 silent skip(訂單卡仍 push,只是少 D1 行)。
        多商品場景 sync 查每個 yahoo_id(~500ms/件,5 件 < 2.5s 可接受)。
        """
        try:
            from .tg_conversation import (
                extract_yahoo_item_ids,
                _query_product_d1,
                _classify_source,
            )
        except Exception as _e:
            self.on_log(f"[ORDER-D1] import 失敗,跳過 enrich: {_e}")
            return

        items = order.get("items") or []
        for it in items:
            try:
                # 從商品 URL 抽 yahoo_id(也可能是 order_id 後綴抽,但 url 最穩定)
                url = str(it.get("url") or "")
                yids = extract_yahoo_item_ids(url)
                if not yids:
                    continue
                yid = yids[0]
                it["d1_yahoo_id"] = yid
                d1_row = _query_product_d1(yid, on_log=self.on_log)
                if not d1_row:
                    continue
                barcode = str(d1_row.get("barcode", "") or "").strip()
                if not barcode:
                    continue
                source, source_url = _classify_source(barcode)
                if source not in ("xianyu", "mercari"):
                    continue
                it["d1_source"] = source
                it["d1_source_url"] = source_url
                it["d1_barcode"] = barcode
                self.on_log(
                    f"[ORDER-D1] {yid} → source={source}, barcode={barcode[:30]}"
                )
            except Exception as _e:
                self.on_log(f"[ORDER-D1] enrich item exception(忽略): {_e}")
                continue

    def _push_order_to_center(
        self,
        forum_bridge,
        st: "AccountState",
        buyer_id: str,
        order: Dict[str, Any],
        items_img: str = "",  # v6.1:不再用(改純文字),保留 signature 不破其他 caller
        is_new: bool = True,
        prev_status: str = "",
    ) -> None:
        """v6.1:訂單中心推送邏輯(HTML mode + edit / push + reply 摘要 + age)。

        is_new=True:第一次出現 → push 主卡 + 記 msg_id
        is_new=False:狀態變更 → edit 主卡(進度條更新)+ reply 摘要(關鍵事件)
        """
        import time as _t
        try:
            from .order_http import (
                format_order_for_tg, format_order_buttons,
                format_order_status_summary,
            )

            try:
                target_chat = forum_bridge._resolve_chat_id_for_profile(st.profile_id)
            except Exception:
                target_chat = str(forum_bridge.bot.forum_chat_id)

            buyer_topic = 0
            try:
                conv_key = forum_bridge._conv_key(st.profile_id, buyer_id)
                buyer_topic = forum_bridge.store.get_topic_id(conv_key) or 0
            except Exception:
                pass

            oid = order.get("order_id", "")
            # v6.1:計算「當前狀態已等多久」— 從 store 上次更新時間算
            prev_msg = forum_bridge.store.get_order_msg(oid)
            age_seconds = 0.0
            if prev_msg and prev_msg.get("last_updated_ts"):
                age_seconds = _t.time() - float(prev_msg["last_updated_ts"])

            # v6.1.44:新訂單(is_new=True)第一次推送時查 D1 enrich 貨源資訊
            # 之後 edit / reply 摘要不重複查(item dict 已有 d1_* 欄位)
            if is_new:
                try:
                    self._enrich_order_with_d1(order)
                except Exception as _e_d1:
                    self.on_log(f"[ORDER-D1] enrich 異常(忽略,繼續推訂單): {_e_d1}")

            html_text = format_order_for_tg(order, with_progress=True, age_seconds=age_seconds)
            buttons = format_order_buttons(
                order,
                buyer_topic_id=buyer_topic,
                group_chat_id=target_chat,
                profile_id=st.profile_id,  # v6.2:callback handler 用來反查 profile
            )

            if is_new:
                msg_id = forum_bridge.push_to_order_center(
                    account_name=st.name,
                    html_text=html_text,
                    buttons=buttons,
                    group_chat_id=target_chat,
                )
                if msg_id:
                    topic_id = forum_bridge.store.get_order_center_topic(target_chat) or 0
                    forum_bridge.store.set_order_msg(
                        oid, topic_id=topic_id, msg_id=msg_id,
                        group_chat_id=target_chat,
                        status=order.get("status", ""),
                        payment=order.get("payment_status", ""),
                    )
                return

            # 狀態變更 → edit 主卡
            edit_ok = forum_bridge.edit_order_card_in_center(
                order_id=oid, html_text=html_text, buttons=buttons,
            )
            if not edit_ok:
                # edit 失敗 → fallback push 新訊息
                msg_id = forum_bridge.push_to_order_center(
                    account_name=st.name,
                    html_text=html_text,
                    buttons=buttons,
                    group_chat_id=target_chat,
                )
                if msg_id:
                    topic_id = forum_bridge.store.get_order_center_topic(target_chat) or 0
                    forum_bridge.store.set_order_msg(
                        oid, topic_id=topic_id, msg_id=msg_id,
                        group_chat_id=target_chat,
                        status=order.get("status", ""),
                        payment=order.get("payment_status", ""),
                    )

            # v6.1:reply 摘要(關鍵事件)— completed 預設不 reply(太頻繁,改 settings 開關)
            status = order.get("status", "")
            payment = order.get("payment_status", "")
            try:
                from .accounts import load_settings as _ls
                completed_reply = bool(_ls().get("order_center_completed_reply", False))
            except Exception:
                completed_reply = False
            critical_events = {"delivered", "canceled", "refunded", "deliveryOverdue"}
            if completed_reply:
                critical_events.add("completed")
            is_critical = (
                status in critical_events
                or (status == "waitForDelivery" and payment == "paid")
            )
            if is_critical:
                summary = format_order_status_summary(order, prev_status=prev_status)
                forum_bridge.reply_in_order_center(
                    order_id=oid, html_text=summary,
                )

            # 更新 store last_status + last_updated_ts(下次算 age 用)
            forum_bridge.store.set_order_msg(
                oid,
                topic_id=prev_msg.get("topic_id", 0) if prev_msg else 0,
                msg_id=prev_msg.get("msg_id", 0) if prev_msg else 0,
                group_chat_id=target_chat,
                status=status,
                payment=payment,
            )
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] _push_order_to_center 異常: {e}")

    def _push_order_card(
        self,
        forum_bridge,
        st: "AccountState",
        buyer_id: str,
        markdown_text: str,
        items_img: str = "",
    ) -> None:
        """同步(executor 內):ensure_topic → 訂單卡。

        有商品圖優先用 sendPhoto + caption(視覺更突出);沒圖 fallback send_text。
        """
        try:
            topic_id, err = forum_bridge.ensure_topic(
                profile_id=st.profile_id,
                yahoo_chat_id=buyer_id,
                buyer_label=buyer_id,
                account_name=st.name,
            )
            if not topic_id:
                self.on_log(f"[PUREHTTP-MON] ensure_topic 失敗 {buyer_id}: {err}")
                return
            # v6.1:用 conv 對應 group 的 chat_id
            conv_key = forum_bridge._conv_key(st.profile_id, buyer_id)
            target_chat = forum_bridge._resolve_chat_id_for_conv(conv_key)
            if items_img:
                # v6.2:format_order_for_tg 返回 HTML(<b>/<code>),改 HTML mode 避免亂碼
                # caption 上限 1024 字
                caption = markdown_text[:1024]
                ok = forum_bridge.bot._post("sendPhoto", {
                    "chat_id": target_chat,
                    "message_thread_id": topic_id,
                    "photo": items_img,
                    "caption": caption,
                    "parse_mode": "HTML",
                })
                # sendPhoto 失敗就 fallback sendMessage(同 HTML mode)
                if not (ok and ok[0] and not ok[1]):
                    forum_bridge.bot.send_text(topic_id, markdown_text, parse_mode="HTML", chat_id=target_chat)
            else:
                forum_bridge.bot.send_text(topic_id, markdown_text, parse_mode="HTML", chat_id=target_chat)
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] _push_order_card 異常: {e}")

    async def _silent_mark_read(self, st: "AccountState", channel_id: str) -> None:
        """v6.1.18:官方頻道靜音 — 只清 Yahoo 紅點,不推 TG forum。

        對應 _is_silent_channel 命中的 channel(Y拍官方客服 / 廣告小舖 等)。
        BOSH channel_user_active 把紅點清掉,不觸發 AI / forum / topic 創建。
        """
        from .im_http_ops import im_mark_read
        profile_dir = self.base_dir / "profiles" / st.profile_id
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, im_mark_read,
                profile_dir, channel_id, "", self.on_log,
            )
            self.on_log(
                f"[PUREHTTP-MON] {st.name} 官方頻道 mark_read 跳過 TG forward: "
                f"...{channel_id[-25:]}"
            )
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] {st.name} 官方頻道 mark_read 異常: {e}")

    async def _forward_new_messages(
        self,
        st: "AccountState",
        channel_id: str,
        prev_unread: int,
        new_unread: int,
    ) -> None:
        """某 channel 未讀變多 → 拉 BOSH 新訊息 → 構造 im_preview_items 結構
        → 同時觸發:
          1. AI 客服流程 — conv_manager.on_new_im(profile_id, name, items)
             (走 commander_decide + writer 生草稿到 TG 預覽,跟舊 monitor 同接口)
          2. Forum forward — push 訊息到對應 buyer topic
        """
        # 構造 im_preview_items(舊 monitor 餵給 conv_manager 的格式)
        items = await self._build_im_preview_items(st, channel_id, new_unread - prev_unread)

        # 1. 觸發 AI 流程(這條是核心 — 客戶訊息來時生成 AI 草稿給 TG 預覽)
        if items and self.conv_manager and hasattr(self.conv_manager, "on_new_im"):
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.conv_manager.on_new_im,
                    st.profile_id, st.name, items,
                )
                self.on_log(
                    f"[PUREHTTP-MON] 觸發 AI 客服:{st.name} {len(items)} 條 channel={channel_id[-30:]}"
                )
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] AI 觸發異常: {e}")

        # 2. forum forward(BOSH 拉訊息 push 到對應 topic)
        try:
            await self._forward_to_forum(st, channel_id, new_unread - prev_unread)
        except Exception as e:
            self.on_log(f"[PUREHTTP-MON] forum forward {channel_id} 異常: {e}")

    async def _build_im_preview_items(
        self,
        st: "AccountState",
        channel_id: str,
        n_new: int,
    ) -> List[Dict[str, Any]]:
        """從 BOSH 拉訊息,構造 conv_manager.on_new_im 接受的 items 結構。

        items 元素參考 capture_yahoo_im_unread_previews 輸出:
          {
            "chat_id": "Y...",       # 買家 Y-ID(賣家視角的對話 ID)
            "label": "買家標籤",
            "preview": "預覽訊息",
            "text": "全文(雙向對話拼接)",
            "url": "/chat/Y...",
            "shop_code": "Y..."(我們自己的店鋪 ID),
            "unread": N,
          }
        """
        from .yahoo_im_jwt import ensure_bosh_jwt
        profile_dir = self.base_dir / "profiles" / st.profile_id
        loop = asyncio.get_event_loop()

        def _do():
            try:
                _, my_user, _ = ensure_bosh_jwt(profile_dir)
                my_id_l = (my_user or "").lower()
                parts = channel_id.split(":")
                if len(parts) != 3:
                    return []
                buyer = ""
                for p in parts[1:]:
                    if p and p.lower() != my_id_l:
                        buyer = p
                        break
                if not buyer:
                    return []
                buyer_y = buyer.upper() if not buyer.startswith("Y") else buyer
                shop_y = my_user.upper() if not my_user.startswith("Y") else my_user

                # 用 REST API 拉最近 100 條訊息(含雙向對話,給 AI 完整上下文)
                # v6.1.35:30 → 100。長對話時 yahoo_id 訊息易被擠出 30 條範圍,
                # 導致 extract_yahoo_item_ids 抓不到 → NEED_SELLER 走 manual fallback。
                # 100 條足夠涵蓋大多數對話(平均每對話 < 80 條,57 條已是 user case)
                from urllib.parse import quote
                from .im_http_ops import _build_session
                from .client_runtime_compat import get_api_headers
                session, _, _ = _build_session(profile_dir, buyer_cid=buyer_y)
                if not session:
                    return []
                url = (
                    f"https://tw.bid.yahoo.com/fe/api/im/messages"
                    f"?property=auction2&channelId={quote(channel_id)}"
                    f"&sortBy=-createdTs&limit=100"
                )
                r = session.get(url, timeout=20)
                if r.status_code != 200:
                    return []
                msgs = r.json().get("messages", []) or []
                # 排序 asc(舊→新)
                msgs.sort(key=lambda m: m.get("createdUts", 0) or 0)

                # 構造全文:【買家】xxx \n 【賣家】xxx(供 AI 看上下文)
                lines = []
                preview_txt = ""
                # v6.1.54:買家發來的圖片 URL list(用於中轉給閒魚賣家比對)
                # 保留最近 5 張(由舊到新),避免 NEED_SELLER 時刷屏
                buyer_image_urls: list = []
                # v6.1.55:完整對話媒體(買家+賣家、圖+視頻、時間戳、訊息位置)
                # 每個元素: {url, role, ts, msg_idx, kind}
                conversation_media: list = []
                for _msg_idx, m in enumerate(msgs, 1):
                    sender = (m.get("sender", "") or "").lower()
                    is_buyer = (sender != my_id_l)
                    role = "【買家】" if is_buyer else "【賣家】"
                    role_key = "buyer" if is_buyer else "seller"
                    msg_ts = int(m.get("createdUts", 0) or 0)
                    val = m.get("value") or {}
                    mtype = m.get("type", "")
                    body = ""
                    media_url = ""
                    media_kind = ""

                    if mtype == "text":
                        body = val.get("content", "") or ""
                    elif mtype == "image":
                        body = "[圖片]"
                        media_kind = "image"
                        try:
                            media_url = (val.get("src") or {}).get("url") or (val.get("origin") or {}).get("url") or ""
                        except Exception:
                            media_url = ""
                        # v6.1.54:買家圖也存到 buyer_image_urls(向後相容,用於閒魚中轉)
                        if is_buyer and media_url and media_url.startswith("http") and media_url not in buyer_image_urls:
                            buyer_image_urls.append(media_url)
                    elif mtype == "video":
                        body = "[視頻]"
                        media_kind = "video"
                        try:
                            # 取 mp4 URL(優先 resizeVideos,fallback src)
                            resized = val.get("resizeVideos") or []
                            for rv in resized:
                                u = (rv or {}).get("url", "") or ""
                                if u.lower().endswith(".mp4"):
                                    media_url = u
                                    break
                            if not media_url:
                                media_url = (val.get("src") or {}).get("url") or ""
                        except Exception:
                            media_url = ""
                    elif mtype == "sticker":
                        body = "[貼圖]"
                        # 貼圖也是圖,但通常表情/小圖,可選擇納入
                        media_kind = "image"
                        media_url = val.get("url", "") or ""
                    elif mtype == "listing":
                        # 必須含 yahoo_id 讓 extract_yahoo_item_ids 抓得到,
                        # 否則 _process_new_conv_inner 抓不到 product_urls,走手動分支
                        _yid = val.get("id", "") or ""
                        _title = (val.get("title", "") or "")[:60]
                        _url = f"https://tw.bid.yahoo.com/item/{_yid}" if _yid else ""
                        body = f"[商品] {_title} {_url}".strip()
                    elif mtype == "order":
                        body = f"[訂單 #{val.get('id','')}]"

                    if body:
                        lines.append(f"{role} {body}")
                        if is_buyer:
                            preview_txt = body[:80]

                    # v6.1.55:加進 conversation_media(含買家+賣家所有圖/視頻)
                    if media_url and media_url.startswith("http") and media_kind:
                        # 去重(同 URL 不重複)
                        if not any(m2.get("url") == media_url for m2 in conversation_media):
                            conversation_media.append({
                                "url": media_url,
                                "role": role_key,
                                "ts": msg_ts,
                                "msg_idx": _msg_idx,
                                "kind": media_kind,
                            })

                # 只保留最近 5 張買家圖(向後相容欄位)
                if len(buyer_image_urls) > 5:
                    buyer_image_urls = buyer_image_urls[-5:]
                # 對話媒體 cap 15(賣家圖+買家圖+視頻總共)
                if len(conversation_media) > 15:
                    conversation_media = conversation_media[-15:]

                # v6.1.35:30 → 100 拼全文,給 AI 完整對話上下文
                text = "\n".join(lines[-100:])
                # 嘗試從 Yahoo IM users API 拿買家暱稱(label)
                buyer_label = buyer_y
                try:
                    r2 = session.get(
                        f"https://tw.bid.yahoo.com/fe/api/im/users?userIds={buyer_y}",
                        headers=get_api_headers(),
                        timeout=10,
                    )
                    if r2.status_code == 200:
                        users = r2.json().get("users") or []
                        if users:
                            nick = users[0].get("nickname") or users[0].get("displayName") or ""
                            if nick:
                                buyer_label = nick
                except Exception:
                    pass

                return [{
                    "chat_id": buyer_y,
                    "label": buyer_label,
                    "preview": preview_txt or "[無預覽]",
                    "text": text,
                    "url": f"/chat/{buyer_y}",
                    "shop_code": shop_y,
                    "unread": n_new,
                    # v6.1.54:傳給 conv_manager,賣家提問時可中轉
                    "buyer_image_urls": buyer_image_urls,
                    # v6.1.55:完整對話媒體(買家+賣家、圖+視頻、時間戳)— AI 按權重看
                    "conversation_media": conversation_media,
                }]
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] _build_im_preview_items 異常: {e}")
                return []

        return await loop.run_in_executor(None, _do)

    async def _forward_to_forum(
        self,
        st: "AccountState",
        channel_id: str,
        n_new: int,
    ) -> None:
        """從 channel_id 拉最近 N 條訊息,逐條 push 到對應 TG forum topic。"""
        forum_bridge = getattr(self.conv_manager, "forum_bridge", None) if self.conv_manager else None
        if not forum_bridge:
            return  # forum 未啟用

        # 從 channel_id 解出 buyer Y-id (yahoo-bid-logbot1:y{shop}:y{buyer})
        parts = channel_id.split(":")
        if len(parts) != 3:
            return
        from .yahoo_im_jwt import ensure_bosh_jwt
        profile_dir = self.base_dir / "profiles" / st.profile_id
        loop = asyncio.get_event_loop()

        def _do():
            _, my_user, _ = ensure_bosh_jwt(profile_dir)
            my_id_l = (my_user or "").lower()
            buyer = ""
            for p in parts[1:]:
                if p and p.lower() != my_id_l:
                    buyer = p
                    break
            if not buyer:
                return
            buyer_y = buyer.upper() if not buyer.startswith("Y") else buyer
            yahoo_chat_id = buyer_y

            # 拉買家暱稱(用 nickname 當 buyer_label,比 Y-ID 友好)— 有 cache 直接用
            cache = self._nickname_cache.setdefault(st.profile_id, {})
            buyer_label = cache.get(buyer_y, "") or buyer_y
            if buyer_label == buyer_y:
                try:
                    from .im_http_ops import _build_session
                    from .client_runtime_compat import get_api_headers
                    _sess, _, _ = _build_session(profile_dir, buyer_cid=buyer_y)
                    if _sess:
                        _r = _sess.get(
                            f"https://tw.bid.yahoo.com/fe/api/im/users?userIds={buyer_y}",
                            headers=get_api_headers(), timeout=10,
                        )
                        if _r.status_code == 200:
                            _users = _r.json().get("users") or []
                            if _users:
                                _nick = _users[0].get("nickname") or _users[0].get("displayName") or ""
                                if _nick:
                                    buyer_label = _nick
                                    cache[buyer_y] = _nick
                except Exception:
                    pass

            # 拉最近 N 條訊息(N + 1 補一個確保拿到全新的)
            from .yahoo_im_bosh_ext import BOSHSession
            try:
                with BOSHSession(profile_dir, on_log=self.on_log) as s:
                    resp, _ = s.query_message(channel_id, after_n=-(n_new + 1))
                    msgs = resp.get("messages", []) or []
                    # asc 排序
                    msgs.sort(key=lambda m: m.get("sendTime", 0) or 0)
                    # 只取 buyer 發的訊息(避免推送我們自己發的)
                    buyer_msgs = [m for m in msgs if (m.get("senderID","") or "").lower() == buyer.lower()]
                    if not buyer_msgs:
                        return
                    # 取最近 n_new 條
                    for m in buyer_msgs[-n_new:]:
                        try:
                            content_raw = m.get("msgContent","") or ""
                            try:
                                content = json.loads(content_raw)
                            except Exception:
                                content = {}
                            mtype = content.get("type", "")
                            value = content.get("value") or {}
                            text = ""
                            media_url = ""
                            media_kind = ""
                            if mtype == "text":
                                text = value.get("content", "") or ""
                            elif mtype == "image":
                                media_url = (value.get("src") or {}).get("url") or value.get("origin", {}).get("url") or ""
                                media_kind = "image"
                            elif mtype == "video":
                                resized = value.get("resizeVideos") or []
                                for rv in resized:
                                    u = (rv or {}).get("url", "")
                                    if u.lower().endswith(".mp4"):
                                        media_url = u
                                        break
                                if not media_url:
                                    media_url = (value.get("src") or {}).get("url") or ""
                                media_kind = "video"
                            elif mtype == "sticker":
                                media_url = value.get("url", "")
                                media_kind = "image"
                            elif mtype == "listing":
                                yid = value.get("id", "")
                                title = (value.get("title", "") or "")[:120]
                                price = value.get("price", "")
                                text = f"🏷 [商品] {title}\n💰 NT${price}\nhttps://tw.bid.yahoo.com/item/{yid}"
                            elif mtype == "order":
                                # ⭐ Yahoo 訂單 attach 卡 — 跟 backfill 顯示對齊
                                oid = value.get("id", "")
                                text = f"📋 [訂單卡] #{oid}"
                            elif mtype:
                                text = f"[{mtype}] {str(value)[:200]}"
                            forum_bridge.forward_yahoo_inbound(
                                profile_id=st.profile_id,
                                yahoo_chat_id=yahoo_chat_id,
                                buyer_label=buyer_label,  # 暱稱(gifery)而非 Y-ID
                                account_name=st.name,
                                text=text,
                                media_url=media_url,
                                media_kind=media_kind,
                            )
                        except Exception as e:
                            self.on_log(f"[PUREHTTP-MON] forward msg 異常: {e}")
            except Exception as e:
                self.on_log(f"[PUREHTTP-MON] BOSH query_message {channel_id} 異常: {e}")

        await loop.run_in_executor(None, _do)
