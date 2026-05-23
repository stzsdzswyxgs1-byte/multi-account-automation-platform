"""全局網路健康狀態 — 跨所有 KV poller 共享。

v6.2:解決 5 個 poller 各自獨立重試 + log 爆量問題。

協作邏輯:
- 任一 poller 連續失敗 >=3 次 → 全局 offline
- 任一 poller 成功 → 重置該 poller 計數;之前是 offline 則切回 online
- log 降噪:offline 期間每 60s 只打一次提醒
- UI listener:訂閱 online/offline 變化(app 標題欄顯示斷線提示)
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple


# 退避表(秒)— index = 失敗次數
BACKOFF_LADDER = [2.0, 5.0, 10.0, 30.0, 60.0]
# offline 觸發閾值(任一 poller 連續失敗次數)
OFFLINE_THRESHOLD = 3
# offline 期間 log 降頻週期(秒)
OFFLINE_LOG_INTERVAL = 60.0
# offline 期間 poll 間隔(秒)— 降頻心跳
OFFLINE_POLL_INTERVAL = 30.0


class NetworkHealth:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._fail_counts: Dict[str, int] = {}
        self._is_offline = False
        self._offline_since: float = 0.0
        self._last_log_ts: float = 0.0
        self._listeners: List[Callable[[bool, float], None]] = []

    def report_success(self, poller_name: str) -> Optional[float]:
        """報告 poll 成功。如果從 offline 恢復,返回斷線秒數(供 caller 打恢復 log);否則 None。"""
        with self._lock:
            self._fail_counts[poller_name] = 0
            if self._is_offline:
                duration = time.time() - self._offline_since
                self._is_offline = False
                self._offline_since = 0.0
                listeners = list(self._listeners)
                # 通知 UI 切回 online
                for cb in listeners:
                    try:
                        cb(False, duration)
                    except Exception:
                        pass
                return duration
        return None

    def report_failure(self, poller_name: str, error_msg: str) -> Tuple[str, str, float]:
        """報告 poll 失敗。

        Returns:
            (log_action, log_msg, sleep_seconds)
            log_action:
              "log"       — 應 log 這條訊息
              "throttled" — 已被降噪,caller 不要 log
            sleep_seconds: 建議 caller sleep 的秒數
        """
        with self._lock:
            self._fail_counts[poller_name] = self._fail_counts.get(poller_name, 0) + 1
            cnt = self._fail_counts[poller_name]
            compact = self._compact_error(error_msg)

            # 退避秒數:指數但封頂
            idx = min(cnt - 1, len(BACKOFF_LADDER) - 1)
            sleep_s = BACKOFF_LADDER[idx]

            now = time.time()
            if not self._is_offline:
                if cnt >= OFFLINE_THRESHOLD:
                    # 第 3 次失敗 → 觸發 offline
                    self._is_offline = True
                    self._offline_since = now
                    self._last_log_ts = now
                    listeners = list(self._listeners)
                    for cb in listeners:
                        try:
                            cb(True, 0.0)
                        except Exception:
                            pass
                    sleep_s = OFFLINE_POLL_INTERVAL
                    return ("log", f"⚠️ 網路斷線(由 {poller_name} 連續失敗 {cnt} 次觸發 / {compact}) — 切心跳模式 {int(sleep_s)}s/次,持續到恢復", sleep_s)
                if cnt == 1:
                    # 第 1 次失敗:log + 短退避(可能只是瞬斷)
                    return ("log", f"[{poller_name}] 輪詢失敗(第 1 次): {compact} — 退避 {sleep_s:.0f}s 重試", sleep_s)
                # 第 2 次:不 log,等第 3 次觸發 offline 統一報
                return ("throttled", "", sleep_s)
            else:
                # 已 offline,降頻 log
                sleep_s = OFFLINE_POLL_INTERVAL
                if now - self._last_log_ts >= OFFLINE_LOG_INTERVAL:
                    self._last_log_ts = now
                    duration = now - self._offline_since
                    return ("log", f"⚠️ 網路仍斷線(已 {self._fmt_duration(duration)},最新錯誤 / {compact})", sleep_s)
                return ("throttled", "", sleep_s)

    def is_offline(self) -> bool:
        with self._lock:
            return self._is_offline

    def offline_duration(self) -> float:
        with self._lock:
            return time.time() - self._offline_since if self._is_offline else 0.0

    def add_listener(self, callback: Callable[[bool, float], None]) -> None:
        """訂閱 online/offline 變化。callback(is_offline: bool, recovery_duration_seconds: float)"""
        with self._lock:
            self._listeners.append(callback)

    @staticmethod
    def _compact_error(msg: str) -> str:
        """提取錯誤訊息最有用的部分。"""
        if not msg:
            return "unknown"
        for key in (
            "getaddrinfo failed",
            "Connection refused",
            "Connection aborted",
            "Connection reset",
            "timed out",
            "Read timed out",
            "Max retries exceeded",
            "Temporary failure in name resolution",
        ):
            if key in msg:
                return key
        return msg[:120]

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m{s % 60}s"
        return f"{s // 3600}h{(s % 3600) // 60}m"


_INSTANCE: Optional[NetworkHealth] = None
_INSTANCE_LOCK = threading.Lock()


def get_network_health() -> NetworkHealth:
    """取得全局 singleton。"""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = NetworkHealth()
    return _INSTANCE


# ────────────────────────────────────────────────────────────────────
# 統一 KV poll loop — 4 個 bot + forum 共用,避免每個地方重複 backoff 邏輯
# ────────────────────────────────────────────────────────────────────

def run_kv_poll_loop(
    poller,
    poller_name: str,
    on_update: Callable[[Dict], None],
    on_log: Callable[[str], None],
    is_running: Callable[[], bool],
    success_sleep: float = 2.0,
) -> None:
    """通用 KV polling loop。

    Args:
        poller: KvPoller 實例(需有 .poll(raise_on_error=True) 方法)
        poller_name: 用於 log + health 追蹤的名字(如 "KV-AI")
        on_update: 收到 update 時的 callback
        on_log: 日誌 callback
        is_running: 返回 bool,False 時退出 loop
        success_sleep: 成功時的 sleep 秒數
    """
    if poller is None:
        on_log(f"[{poller_name}] poller 未初始化,退出 loop")
        return
    health = get_network_health()
    while is_running():
        try:
            updates = poller.poll(raise_on_error=True)
            recovery = health.report_success(poller_name)
            if recovery is not None:
                on_log(f"✅ 網路已恢復(斷線持續 {NetworkHealth._fmt_duration(recovery)})")
            if updates:
                for upd in updates:
                    try:
                        on_update(upd)
                    except Exception as e:
                        on_log(f"[{poller_name}] handle_update 異常: {e}")
            time.sleep(success_sleep)
        except Exception as e:
            action, msg, sleep_s = health.report_failure(poller_name, str(e))
            if action == "log" and msg:
                on_log(msg)
            time.sleep(sleep_s)
