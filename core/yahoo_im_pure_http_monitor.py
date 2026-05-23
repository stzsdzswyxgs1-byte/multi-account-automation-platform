"""純 HTTP Yahoo IM 監控 — 取代 Playwright DOM 提取紅點/未讀數 (v6.0.83)

用 BOSH long-polling 即時接收訊息 push + 定期 poll 未讀數,完全去 Playwright:

組合:
  BOSHListener         即時 server push(新訊息/已讀/recall)
  BOSHSession + get_user_unread_channels  定期拿未讀數(備援,主要靠 push)
  im_http_ops.read_im_messages            拿訊息歷史(GET 不觸發已讀)

啟動方式(per Yahoo 帳號):
    mon = YahooIMPureHTTPMonitor(
        profile_dir,
        account_name="xian678",
        on_new_message=lambda msg: dispatch_to_ai(msg),
        on_unread_change=lambda total, by_chan: update_red_dot(total),
        on_log=log,
    )
    mon.start()
    ...
    mon.stop()

紅點/已讀同步機制(攔截已讀):
- 監控期間「不主動」呼叫 mark_read/channel_user_active(避免讓對方看到我已讀)
- 用戶在 TG 端 reply 後 send_im_message/send_image_message 內部會 auto mark_read
- 因此「對方看到已讀」與「我們的 reply 動作」綁定,跟既有 Playwright 行為一致
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


class YahooIMPureHTTPMonitor:
    """單一 Yahoo 帳號的純 HTTP 監控。

    內部由 BOSHListener(即時)+ BOSHSession(定期 poll 未讀備援)組合。
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        account_name: str = "",
        on_log: Optional[LogFn] = None,
        on_new_message: Optional[Callable[[Any], None]] = None,
        on_unread_change: Optional[Callable[[int, Dict[str, int]], None]] = None,
        on_mark_read_received: Optional[Callable[[str, int], None]] = None,
        on_recall_received: Optional[Callable[[List[str]], None]] = None,
        unread_poll_interval_sec: int = 300,  # 5 分鐘 fallback poll
        enable_unread_poll: bool = True,
    ):
        self.profile_dir = Path(profile_dir)
        self.account_name = account_name
        self.on_log = on_log or (lambda *_: None)
        self.on_new_message = on_new_message
        self.on_unread_change = on_unread_change
        self.on_mark_read_received = on_mark_read_received
        self.on_recall_received = on_recall_received
        self.unread_poll_interval_sec = unread_poll_interval_sec
        self.enable_unread_poll = enable_unread_poll

        self._listener = None  # BOSHListener
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_unread_summary: Dict[str, int] = {}  # chID → unread count

    def start(self) -> bool:
        """啟動 BOSH listener + (optional) 定期 unread poll。"""
        if self._running:
            return True
        try:
            from .yahoo_im_bosh_listener import BOSHListener
        except ImportError as e:
            self.on_log(f"[YH-MON] BOSHListener import 失敗: {e}")
            return False

        self._running = True
        # 1. BOSH listener (即時接收新訊息/已讀/recall)
        self._listener = BOSHListener(
            self.profile_dir,
            on_log=lambda m: self.on_log(f"[YH-MON:{self.account_name}] {m}"),
            on_message=self._on_listener_message,
            on_mark_read=self._on_listener_mark_read,
            on_recall=self._on_listener_recall,
            on_disconnect=lambda err: self.on_log(
                f"[YH-MON:{self.account_name}] BOSH 斷線: {err}"
            ),
        )
        self._listener.start()

        # 2. 定期 unread poll (備援,即時 push 漏掉時兜底)
        if self.enable_unread_poll:
            self._poll_thread = threading.Thread(
                target=self._unread_poll_loop, daemon=True,
                name=f"yh-unread-{self.account_name}",
            )
            self._poll_thread.start()

        self.on_log(f"[YH-MON:{self.account_name}] 啟動完成(listener + unread poll)")
        return True

    def stop(self) -> None:
        self._running = False
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    # ─── BOSHListener 回調 ───

    def _on_listener_message(self, msg) -> None:
        """server 推來新訊息。"""
        try:
            if self.on_new_message:
                self.on_new_message(msg)
        except Exception as e:
            self.on_log(f"[YH-MON:{self.account_name}] on_new_message 異常: {e}")

    def _on_listener_mark_read(self, channel_id: str, mark_ts: int) -> None:
        try:
            if self.on_mark_read_received:
                self.on_mark_read_received(channel_id, mark_ts)
        except Exception as e:
            self.on_log(f"[YH-MON:{self.account_name}] on_mark_read 異常: {e}")

    def _on_listener_recall(self, msg_ids: List[str]) -> None:
        try:
            if self.on_recall_received:
                self.on_recall_received(msg_ids)
        except Exception as e:
            self.on_log(f"[YH-MON:{self.account_name}] on_recall 異常: {e}")

    # ─── 定期 unread poll ───

    def _unread_poll_loop(self) -> None:
        """定期拉未讀數作為備援(主要靠 BOSH push)。"""
        while self._running:
            try:
                self._poll_once_unread()
            except Exception as e:
                self.on_log(f"[YH-MON:{self.account_name}] unread poll 異常: {e}")
            # sleep with check
            for _ in range(self.unread_poll_interval_sec):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_once_unread(self) -> None:
        try:
            from .yahoo_im_bosh_ext import BOSHSession
            with BOSHSession(self.profile_dir, on_log=lambda m: None) as sess:
                resp, err = sess.get_user_unread_channels()
                if err:
                    self.on_log(f"[YH-MON:{self.account_name}] unread poll {err}")
                    return
                if not isinstance(resp, dict):
                    return
                total = int(resp.get("totalUnread") or 0)
                result_list = resp.get("result") or []
                summary: Dict[str, int] = {}
                if isinstance(result_list, list):
                    for entry in result_list:
                        if isinstance(entry, dict):
                            cid = str(entry.get("chID") or "")
                            unread = int(entry.get("unread") or entry.get("unreadCount") or 0)
                            if cid and unread:
                                summary[cid] = unread
                # 變化才 callback
                if summary != self._last_unread_summary:
                    self._last_unread_summary = summary
                    if self.on_unread_change:
                        try:
                            self.on_unread_change(total, summary)
                        except Exception as e:
                            self.on_log(f"[YH-MON:{self.account_name}] on_unread_change 異常: {e}")
        except Exception as e:
            self.on_log(f"[YH-MON:{self.account_name}] _poll_once_unread 異常: {e}")

    # ─── 公開方法 ───

    def get_current_unread(self) -> Dict[str, int]:
        """返回最近 poll 拿到的 unread summary(chID → count)。"""
        return dict(self._last_unread_summary)

    def fetch_message_history(
        self,
        channel_id: str,
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """純 HTTP 拉訊息歷史(不觸發已讀)。"""
        try:
            from .yahoo_im_bosh_ext import BOSHSession
            with BOSHSession(self.profile_dir, on_log=lambda m: None) as sess:
                resp, err = sess.query_message(channel_id, after_n=-limit)
                if err:
                    return []
                msgs = resp.get("messages") if isinstance(resp, dict) else None
                return msgs if isinstance(msgs, list) else []
        except Exception as e:
            self.on_log(f"[YH-MON:{self.account_name}] fetch_history 異常: {e}")
            return []


# ─── 多帳號管理(供 monitor.py 整合用)───


class MultiAccountPureHTTPMonitor:
    """多 Yahoo 帳號的純 HTTP 監控 — 每帳號一個 YahooIMPureHTTPMonitor。

    用於取代既有 monitor.py 的 Playwright DOM 提取路徑。
    """

    def __init__(
        self,
        base_dir: Path,
        accounts: List[Dict[str, Any]],
        *,
        on_log: Optional[LogFn] = None,
        on_new_message: Optional[Callable[[str, Any], None]] = None,  # (account_name, msg)
        on_unread_change: Optional[Callable[[str, int, Dict[str, int]], None]] = None,  # (account_name, total, by_chan)
    ):
        self.base_dir = Path(base_dir)
        self.accounts = accounts
        self.on_log = on_log or (lambda *_: None)
        self.on_new_message = on_new_message
        self.on_unread_change = on_unread_change
        self._monitors: Dict[str, YahooIMPureHTTPMonitor] = {}

    def start_all(self) -> None:
        for acc in self.accounts:
            name = acc.get("profile_id") or acc.get("name") or ""
            if not name or name in self._monitors:
                continue
            profile_dir = self.base_dir / "profiles" / name
            if not profile_dir.exists():
                continue
            mon = YahooIMPureHTTPMonitor(
                profile_dir,
                account_name=name,
                on_log=self.on_log,
                on_new_message=lambda m, _n=name: (
                    self.on_new_message(_n, m) if self.on_new_message else None
                ),
                on_unread_change=lambda total, by_chan, _n=name: (
                    self.on_unread_change(_n, total, by_chan) if self.on_unread_change else None
                ),
            )
            if mon.start():
                self._monitors[name] = mon
        self.on_log(f"[YH-MULTI] 啟動 {len(self._monitors)}/{len(self.accounts)} 個帳號 listener")

    def stop_all(self) -> None:
        for name, mon in list(self._monitors.items()):
            try:
                mon.stop()
            except Exception:
                pass
        self._monitors.clear()
