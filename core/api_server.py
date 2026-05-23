"""本機 HTTP API server — 給外部 daemon agent (TG bot @example_daemon_bot) 調用。

設計原則(2026-04-29 v6.0.46 階段 1):
  - bind 127.0.0.1 only,外網打不進
  - 全程旁路:不改既有函數簽名/數據結構/執行順序
  - daemon thread,啟動失敗只記日誌不拋 — GUI 主流程零影響
  - 三層 kill switch:settings.json.api_server_enabled / 啟動參數 / token 不存在
  - Lock 哲學:只進 lock 拷貝,出 lock 序列化,絕不 hold lock 做 I/O
  - 寫類:Idempotency-Key(SQLite TTL 24h)+ token + circuit breaker
  - PII:audit log redact 訊息正文,只記 channel_id+ts

階段 1 端點(8 個 + 1 health):
  GET  /api/version              版本 + api_compat_version
  GET  /api/health               server 自身存活探測
  GET  /api/state/accounts       monitor.get_snapshot()
  GET  /api/state/conversations  conv_manager.get_active_convs_snapshot()(5s cache)
  GET  /api/publish/progress     publish_tab.get_progress_snapshot()
  GET  /api/im/messages          ?account=&channel_id=&shop_id=&limit=
  GET  /api/im/draft/{conv_id}   從 conv_mgr._convs 拷草稿
  POST /api/im/send              body {account, channel_id, receiver, message}
  POST /api/im/mark_read         body {account, channel_id}

寫類強制檢核:H 條 — receiver 必須跟 channel_id 解析的 buyer Y-ID 同源。
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
import re
import http.server
import socketserver
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

# ────────────────────── 常量 ──────────────────────

API_COMPAT_VERSION = "1"   # break change 才 +1
API_VERSION = "1.0"        # 加端點 +1

_BASE_DIR = Path(__file__).resolve().parent.parent
_RUNTIME_DIR = _BASE_DIR / "runtime"
_LOGS_DIR = _BASE_DIR / "logs"
_AUDIT_LOG = _LOGS_DIR / "api_calls.jsonl"
_IDEM_DB = _RUNTIME_DIR / "api_idem.db"
_VERSION_FILE = _BASE_DIR / "current_version.txt"

_IDEM_TTL_SEC = 24 * 3600

# 寫類 circuit breaker:同帳號 60s 內 ≥5 連錯 → 503 + Retry-After 60
_CB_WINDOW_SEC = 60.0
_CB_THRESHOLD = 5
_CB_BACKOFF_SEC = 60

# /api/state/conversations 5 秒 cache(避免 lock 抖動)
_CONV_CACHE_TTL_SEC = 5.0


# ────────────────────── 全局狀態 ──────────────────────

_log = logging.getLogger("xdzhgl.api")
_app_ref = None        # 由 attach(app) 注入,api_server 只讀,不寫
_server_thread: Optional[threading.Thread] = None
_httpd: Optional[socketserver.TCPServer] = None
_started_at: float = 0.0
_api_token: str = ""
_started = False

# 寫類 circuit breaker 狀態:{account: deque[ts]}
import collections as _coll
_cb_history: Dict[str, _coll.deque] = {}
_cb_lock = threading.Lock()

# /api/state/conversations 5s cache
_conv_cache: Tuple[float, str] = (0.0, "")
_conv_cache_lock = threading.Lock()


# ────────────────────── 對外掛載 ──────────────────────

def attach(app) -> None:
    """app.py 啟動時呼叫,把 App 實例引用注入。

    api_server 不持有 app 強引用之外的東西 —
    用到 monitor / conv_mgr / publish_tab 時都從 app_ref 拿,允許後啟動。
    """
    global _app_ref
    _app_ref = app


def start_server(port: int = 7777) -> None:
    """啟動 daemon thread。失敗不拋,只記日誌。"""
    global _server_thread, _httpd, _started_at, _api_token, _started
    if _started:
        return
    try:
        _ensure_dirs()
        _ensure_idem_db()
        _api_token = _ensure_token()
        addr = ("127.0.0.1", int(port))
        # ThreadingTCPServer:每 request 一條 thread,不會卡 GUI
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        _httpd = socketserver.ThreadingTCPServer(addr, _Handler)
        _httpd.daemon_threads = True
        _started_at = time.time()
        _started = True
        _server_thread = threading.Thread(
            target=_httpd.serve_forever, daemon=True, name="xdzhgl-api"
        )
        _server_thread.start()
        _log.info(f"[API] server bound 127.0.0.1:{port}")
    except Exception as e:
        _log.error(f"[API] start failed: {e}")


def stop_server() -> None:
    """TG /api stop 時呼叫,關閉 server。"""
    global _httpd, _started
    try:
        if _httpd is not None:
            _httpd.shutdown()
            _httpd.server_close()
            _httpd = None
        _started = False
    except Exception as e:
        _log.error(f"[API] stop failed: {e}")


# ────────────────────── 內部 helpers ──────────────────────

def _ensure_dirs() -> None:
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _ensure_idem_db() -> None:
    try:
        conn = sqlite3.connect(str(_IDEM_DB), timeout=2.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS idem_keys ("
            "key TEXT PRIMARY KEY, response TEXT, ts REAL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON idem_keys(ts)")
        # 順手 GC 過期
        conn.execute("DELETE FROM idem_keys WHERE ts < ?",
                     (time.time() - _IDEM_TTL_SEC,))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.error(f"[API] idem db init failed: {e}")


def _ensure_token() -> str:
    """從 settings.json 讀 api_token,沒有就生成寫回。

    **race fix(2026-04-29 v6.0.46 hotfix)**:
    GUI 已啟動後若硬碟 settings.json 被外部 edit 加了 api_* key,
    內存 self.settings 不會自動感知,任何後續 save_settings(self.settings)
    都會用 stale 內存覆寫硬碟 → api_* 被抹。

    修法:在這裡也同步 setdefault 到 _app_ref.settings(內存),
    讓內存版本帶著 api_* 三個 key,後續落盤就不會抹。
    setdefault 不覆蓋 user 已改的值。
    """
    try:
        from .accounts import load_settings, save_settings
    except Exception:
        return ""
    try:
        s = load_settings() or {}
        tok = (s.get("api_token") or "").strip()
        port = int(s.get("api_port") or 7777)
        # enabled 預設 False — 推送後其他用戶不會誤啟動,user 自己手動 true 才開
        enabled = bool(s.get("api_server_enabled", False))
        dirty = False
        if not tok:
            tok = secrets.token_urlsafe(32)
            s["api_token"] = tok
            dirty = True
        if "api_port" not in s:
            s["api_port"] = port
            dirty = True
        if "api_server_enabled" not in s:
            s["api_server_enabled"] = enabled
            dirty = True
        if dirty:
            save_settings(s)

        # 關鍵:同步到 GUI 內存,避免後續 save_settings(self.settings) 抹掉
        if _app_ref is not None and hasattr(_app_ref, "settings") \
                and isinstance(_app_ref.settings, dict):
            _app_ref.settings.setdefault("api_token", tok)
            _app_ref.settings.setdefault("api_port", port)
            _app_ref.settings.setdefault("api_server_enabled", enabled)
        return tok
    except Exception as e:
        _log.error(f"[API] token init failed: {e}")
        return ""


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _audit(record: Dict[str, Any]) -> None:
    try:
        record.setdefault("ts", time.time())
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _idem_get(key: str) -> Optional[str]:
    if not key:
        return None
    try:
        conn = sqlite3.connect(str(_IDEM_DB), timeout=2.0)
        cur = conn.execute(
            "SELECT response, ts FROM idem_keys WHERE key=?", (key,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        resp, ts = row
        if time.time() - float(ts) > _IDEM_TTL_SEC:
            return None
        return resp
    except Exception:
        return None


def _idem_put(key: str, response_json: str) -> None:
    if not key:
        return
    try:
        conn = sqlite3.connect(str(_IDEM_DB), timeout=2.0)
        conn.execute(
            "INSERT OR REPLACE INTO idem_keys(key, response, ts) VALUES (?, ?, ?)",
            (key, response_json, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _cb_record_error(account: str) -> None:
    if not account:
        return
    now = time.time()
    with _cb_lock:
        dq = _cb_history.setdefault(account, _coll.deque(maxlen=_CB_THRESHOLD * 2))
        dq.append(now)
        # 清過期
        while dq and dq[0] < now - _CB_WINDOW_SEC:
            dq.popleft()


def _cb_is_open(account: str) -> bool:
    if not account:
        return False
    now = time.time()
    with _cb_lock:
        dq = _cb_history.get(account)
        if not dq:
            return False
        while dq and dq[0] < now - _CB_WINDOW_SEC:
            dq.popleft()
        return len(dq) >= _CB_THRESHOLD


# H 條:channel_id 解析 buyer Y-ID,寫類 receiver 必須相符
_CHID_BUYER_RE = re.compile(r"^[^:]+:y([^:]+):y([^:]+)$", re.I)


def _parse_channel_buyer(channel_id: str, shop_id: str = "") -> Optional[str]:
    """從 channel_id 解析買家 Y-ID(扣掉賣家 shop_id 那邊)。

    格式:yahoo-bid-logbot1:y{seller}:y{buyer}
    若 shop_id 給了,確認哪邊是賣家,另一邊是買家。
    沒給 shop_id → 兩邊都當潛在買家(寬鬆,後段邏輯再篩)。
    """
    m = _CHID_BUYER_RE.match((channel_id or "").strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    sid = (shop_id or "").lstrip("yY")
    if sid:
        if a.lower() == sid.lower():
            return f"Y{b.lstrip('Yy')}"
        if b.lower() == sid.lower():
            return f"Y{a.lstrip('Yy')}"
        return None
    # 沒給 shop_id,回 b(慣例 buyer 在右側,但不保證)
    return f"Y{b.lstrip('Yy')}"


# ────────────────────── HTTP Handler ──────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    # 關閉 BaseHTTPRequestHandler 的 stderr log,改用我們自己的
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # ---- 寫 helper ----
    def _send_json(self, status: int, payload: Any,
                    extra_headers: Optional[Dict[str, str]] = None) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        return body.decode("utf-8", errors="replace")

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > 1_000_000:  # 1MB 上限
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _check_token(self, write_endpoint: bool = False) -> bool:
        """讀類:127.0.0.1 連線豁免 token(本機才能打)。寫類:強制 token。"""
        if not write_endpoint:
            return True
        if not _api_token:
            return False
        provided = (self.headers.get("X-API-Token") or "").strip()
        if not provided:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.startswith("Bearer "):
                provided = auth[7:].strip()
        return secrets.compare_digest(provided, _api_token)

    # ---- 路由 ----
    def do_GET(self) -> None:
        try:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            self._dispatch("GET", url.path, qs, {})
        except Exception as e:
            _log.error(f"[API] GET {self.path} crashed: {e}")
            try:
                self._send_json(500, {"error": "internal", "detail": str(e)[:200]})
            except Exception:
                pass

    def do_POST(self) -> None:
        try:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            body = self._read_body()
            self._dispatch("POST", url.path, qs, body)
        except Exception as e:
            _log.error(f"[API] POST {self.path} crashed: {e}")
            try:
                self._send_json(500, {"error": "internal", "detail": str(e)[:200]})
            except Exception:
                pass

    def _dispatch(self, method: str, path: str, qs: Dict[str, list],
                   body: Dict[str, Any]) -> None:
        ts0 = time.time()
        status = 200
        resp_str = ""

        # ---- 路由表 ----
        try:
            if method == "GET" and path == "/api/version":
                resp = _ep_version()
                resp_str = self._send_json(200, resp)
            elif method == "GET" and path == "/api/health":
                resp = _ep_health()
                resp_str = self._send_json(200, resp)
            elif method == "GET" and path == "/api/state/accounts":
                resp = _ep_state_accounts()
                resp_str = self._send_json(200, resp)
            elif method == "GET" and path == "/api/state/conversations":
                resp = _ep_state_conversations()
                resp_str = self._send_json(200, resp)
            elif method == "GET" and path == "/api/publish/progress":
                resp = _ep_publish_progress()
                resp_str = self._send_json(200, resp)
            elif method == "GET" and path == "/api/im/messages":
                status, resp = _ep_im_messages(qs)
                resp_str = self._send_json(status, resp)
            elif method == "GET" and path.startswith("/api/im/draft/"):
                conv_id = path[len("/api/im/draft/"):]
                status, resp = _ep_im_draft(conv_id)
                resp_str = self._send_json(status, resp)
            elif method == "POST" and path == "/api/im/send":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    status, resp, idem_key, replayed = _ep_im_send(body)
                    extra = {}
                    if status == 503:
                        extra["Retry-After"] = str(_CB_BACKOFF_SEC)
                    if replayed:
                        extra["X-Idempotent-Replay"] = "true"
                    resp_str = self._send_json(status, resp, extra)
                    if status == 200 and idem_key and not replayed:
                        _idem_put(idem_key, resp_str)
            elif method == "POST" and path == "/api/im/mark_read":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    status, resp, idem_key, replayed = _ep_im_mark_read(body)
                    extra = {}
                    if status == 503:
                        extra["Retry-After"] = str(_CB_BACKOFF_SEC)
                    if replayed:
                        extra["X-Idempotent-Replay"] = "true"
                    resp_str = self._send_json(status, resp, extra)
                    if status == 200 and idem_key and not replayed:
                        _idem_put(idem_key, resp_str)
            elif method == "POST" and path == "/api/monitor/start":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    status, resp = _ep_monitor_start()
                    resp_str = self._send_json(status, resp)
            elif method == "POST" and path == "/api/monitor/stop":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    status, resp = _ep_monitor_stop()
                    resp_str = self._send_json(status, resp)
            else:
                status = 404
                resp_str = self._send_json(404, {"error": "not_found", "path": path})
        finally:
            # audit(redact 訊息文本)
            redacted_body = {k: ("<redacted>" if k == "message" else v)
                              for k, v in (body or {}).items()}
            _audit({
                "method": method, "path": path,
                "status": status, "qs": dict(qs),
                "body": redacted_body if method == "POST" else None,
                "client": self.client_address[0] if self.client_address else "",
                "elapsed_ms": int((time.time() - ts0) * 1000),
            })


# ────────────────────── 端點實作 ──────────────────────

def _ep_version() -> Dict[str, Any]:
    return {
        "software_name": "xdzhgl",
        "version": _read_version(),
        "api_version": API_VERSION,
        "api_compat_version": API_COMPAT_VERSION,
        "started_at": _started_at,
    }


def _ep_health() -> Dict[str, Any]:
    app = _app_ref
    return {
        "ok": True,
        "started_at": _started_at,
        "uptime_sec": int(time.time() - _started_at) if _started_at else 0,
        "monitor_attached": app is not None and getattr(app, "mon", None) is not None,
        "conv_mgr_attached": app is not None and getattr(app, "_conv_mgr", None) is not None,
        "publish_tab_attached": app is not None and getattr(app, "publish_tab", None) is not None,
    }


def _ep_state_accounts() -> Dict[str, Any]:
    app = _app_ref
    if app is None or getattr(app, "mon", None) is None:
        return {"accounts": [], "note": "monitor not started"}
    try:
        return app.mon.get_snapshot()
    except Exception as e:
        return {"error": str(e)[:200], "accounts": []}


# ── monitor start/stop 直連端點(2026-04-30 v6.0.48,救 daemon 啟動鏈)──
# 既有 TG /startmon /stopmon 路徑要過 KV worker,中間任何一段斷 daemon 收不到信號。
# 直連端點:daemon → API 7777 → app.after(0, app._start_monitor) Tk 主執行緒。
# 注意:_start_monitor 會碰 Tk 變數(self.var_conc),必須走 app.after schedule 到 Tk thread。

def _ep_monitor_start() -> Tuple[int, Dict[str, Any]]:
    app = _app_ref
    if app is None:
        return 503, {"error": "app_not_attached"}
    if getattr(app, "monitoring", False):
        return 200, {"ok": True, "msg": "already_running"}
    try:
        app.after(0, app._start_monitor)
        return 200, {"ok": True, "msg": "start_scheduled",
                      "note": "Tk after queued; check /api/state/accounts in 3-5s for running_count"}
    except Exception as e:
        return 500, {"error": "start_failed", "detail": str(e)[:200]}


def _ep_monitor_stop() -> Tuple[int, Dict[str, Any]]:
    app = _app_ref
    if app is None:
        return 503, {"error": "app_not_attached"}
    if not getattr(app, "monitoring", False):
        return 200, {"ok": True, "msg": "already_stopped"}
    try:
        app.after(0, app._stop_monitor)
        return 200, {"ok": True, "msg": "stop_scheduled"}
    except Exception as e:
        return 500, {"error": "stop_failed", "detail": str(e)[:200]}


def _ep_state_conversations() -> Dict[str, Any]:
    """5 秒 cache,避免高頻呼叫搶 ConvManager._lock。"""
    global _conv_cache
    now = time.time()
    with _conv_cache_lock:
        cached_ts, cached_str = _conv_cache
        if cached_str and (now - cached_ts) < _CONV_CACHE_TTL_SEC:
            return json.loads(cached_str)

    app = _app_ref
    if app is None or getattr(app, "_conv_mgr", None) is None:
        out = {"conversations": [], "note": "conv manager not started"}
    else:
        try:
            out = app._conv_mgr.get_active_convs_snapshot()
        except Exception as e:
            out = {"error": str(e)[:200], "conversations": []}
    with _conv_cache_lock:
        _conv_cache = (now, json.dumps(out, ensure_ascii=False))
    return out


def _ep_publish_progress() -> Dict[str, Any]:
    app = _app_ref
    pub = getattr(app, "publish_tab", None) if app else None
    if pub is None:
        return {"accounts": [], "note": "publish tab not built"}
    try:
        return pub.get_progress_snapshot()
    except Exception as e:
        return {"error": str(e)[:200], "accounts": []}


def _ep_im_messages(qs: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
    account = (qs.get("account", [""])[0] or "").strip()
    channel_id = (qs.get("channel_id", [""])[0] or "").strip()
    shop_id = (qs.get("shop_id", [""])[0] or "").strip()
    try:
        limit = int(qs.get("limit", ["30"])[0] or 30)
    except Exception:
        limit = 30
    if not account or not channel_id:
        return 400, {"error": "account 與 channel_id 必填"}
    pdir = _BASE_DIR / "profiles" / account
    if not pdir.exists():
        return 404, {"error": f"profile 不存在 {account}"}
    try:
        from .im_http_ops import im_read_messages
        text = im_read_messages(pdir, channel_id, shop_id=shop_id,
                                 limit=limit, account_name=account)
        return 200, {
            "account": account, "channel_id": channel_id,
            "shop_id": shop_id, "limit": limit,
            "text": text or "",
            "len": len(text or ""),
        }
    except Exception as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}


def _ep_im_draft(conv_id: str) -> Tuple[int, Dict[str, Any]]:
    conv_id = (conv_id or "").strip()
    if not conv_id:
        return 400, {"error": "conv_id 必填"}
    app = _app_ref
    if app is None or getattr(app, "_conv_mgr", None) is None:
        return 503, {"error": "conv manager 未啟動"}
    try:
        snap = app._conv_mgr.get_active_convs_snapshot()
        for c in snap.get("conversations", []):
            if c.get("conv_id") == conv_id:
                return 200, c
        return 404, {"error": f"conv {conv_id} 不存在或已過期"}
    except Exception as e:
        return 500, {"error": f"{type(e).__name__}: {e}"}


def _ep_im_send(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str, bool]:
    """回 (status, resp, idem_key, replayed)。replayed=True 時 dispatcher 會加 X-Idempotent-Replay header。"""
    account = (body.get("account") or "").strip()
    channel_id = (body.get("channel_id") or "").strip()
    receiver = (body.get("receiver") or "").strip()
    message = (body.get("message") or "").strip()
    shop_id = (body.get("shop_id") or "").strip()
    idem_key = (body.get("idempotency_key") or "").strip()

    if not account or not channel_id or not receiver or not message:
        return 400, {"error": "account/channel_id/receiver/message 必填"}, "", False

    # idempotency 命中:直接回上次響應(不重發),replayed=True 讓 dispatcher 加 header
    if idem_key:
        cached = _idem_get(idem_key)
        if cached is not None:
            try:
                return 200, json.loads(cached), idem_key, True
            except Exception:
                pass

    # circuit breaker:該帳號連錯太多 → 503 退避
    if _cb_is_open(account):
        return 503, {"error": "circuit_open",
                      "retry_after_sec": _CB_BACKOFF_SEC,
                      "reason": f"{account} 寫類連錯 ≥ {_CB_THRESHOLD} / {int(_CB_WINDOW_SEC)}s"}, idem_key, False

    # H 條:receiver 必須跟 channel_id 解析的買家 Y-ID 同源
    parsed = _parse_channel_buyer(channel_id, shop_id)
    if parsed is None:
        return 400, {"error": "channel_id 格式錯誤,無法解析買家 Y-ID"}, idem_key, False
    rcv_norm = receiver if receiver.startswith("Y") else f"Y{receiver.lstrip('Yy')}"
    if shop_id and parsed.lower() != rcv_norm.lower():
        return 403, {"error": "receiver 與 channel_id 解析的買家 Y-ID 不一致(H 條保護)",
                       "parsed_buyer": parsed, "given_receiver": rcv_norm}, idem_key, False

    pdir = _BASE_DIR / "profiles" / account
    if not pdir.exists():
        return 404, {"error": f"profile 不存在 {account}"}, idem_key, False
    try:
        from .im_http_ops import im_send_message
        ok, msg = im_send_message(pdir, channel_id, receiver, message)
        if not ok:
            _cb_record_error(account)
            return 502, {"ok": False, "msg": msg, "account": account}, idem_key, False
        return 200, {"ok": True, "msg": msg, "account": account,
                      "channel_id": channel_id, "sent_len": len(message)}, idem_key, False
    except Exception as e:
        _cb_record_error(account)
        return 500, {"error": f"{type(e).__name__}: {e}"}, idem_key, False


def _ep_im_mark_read(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str, bool]:
    """回 (status, resp, idem_key, replayed)。"""
    account = (body.get("account") or "").strip()
    channel_id = (body.get("channel_id") or "").strip()
    idem_key = (body.get("idempotency_key") or "").strip()

    if not account or not channel_id:
        return 400, {"error": "account/channel_id 必填"}, "", False

    if idem_key:
        cached = _idem_get(idem_key)
        if cached is not None:
            try:
                return 200, json.loads(cached), idem_key, True
            except Exception:
                pass

    if _cb_is_open(account):
        return 503, {"error": "circuit_open",
                      "retry_after_sec": _CB_BACKOFF_SEC}, idem_key, False

    pdir = _BASE_DIR / "profiles" / account
    if not pdir.exists():
        return 404, {"error": f"profile 不存在 {account}"}, idem_key, False
    try:
        from .im_http_ops import im_mark_read
        ok, msg = im_mark_read(pdir, channel_id)
        if not ok:
            _cb_record_error(account)
            return 502, {"ok": False, "msg": msg}, idem_key, False
        return 200, {"ok": True, "msg": msg}, idem_key, False
    except Exception as e:
        _cb_record_error(account)
        return 500, {"error": f"{type(e).__name__}: {e}"}, idem_key, False
