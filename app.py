from __future__ import annotations
import core.ssl_helper  # noqa: F401  — 全局 SSL 容错，必须最早导入

# v6.1:中文路徑 + curl_cffi 修復 — 啟動最早設環境變數讓所有後續 curl_cffi 用 ASCII cert path
# (Python 路徑含中文時 curl C lib 讀 cacert.pem 會 fail with err 77)
import os as _os_init
try:
    import certifi as _certifi_init
    _src_cert = _certifi_init.where()
    if _src_cert and any(ord(c) > 127 for c in _src_cert):
        import shutil as _shutil_init, tempfile as _tempfile_init
        _dst_cert = _os_init.path.join(_tempfile_init.gettempdir(), "cacert_ascii.pem")
        if not _os_init.path.exists(_dst_cert):
            _shutil_init.copy(_src_cert, _dst_cert)
        _os_init.environ.setdefault("SSL_CERT_FILE", _dst_cert)
        _os_init.environ.setdefault("CURL_CA_BUNDLE", _dst_cert)
        _os_init.environ.setdefault("REQUESTS_CA_BUNDLE", _dst_cert)
except Exception:
    pass

import os
import sys
import json
import re
import time
import threading
import asyncio
import traceback as _tb
import faulthandler
import atexit
import signal
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import deque


# ========== 文件级崩溃日志（写磁盘，进程死亡也能保留） ==========
_CRASH_LOG = Path(__file__).parent / "crash.log"
_CRASH_LOG_FD = None

def _crash_log(msg: str):
    """写一行到 crash.log（立即 flush，进程崩溃也能保留）。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
            f.flush()
    except Exception:
        pass

# v6.1.20.7:**全部關掉 crash 診斷工具** — 用戶實證 v6.1.20(無這些)不會 crash,
# v6.1.20.4(加了這些)會 crash。最可能元兇 = faulthandler.enable() 在 Windows 上
# 攔 SEH UnhandledExceptionFilter,把第三方 C lib 內部 handled 異常誤判 fatal → 殺進程。
# 不再 enable,留個 _CRASH_LOG_FD 給 _crash_log 用就好。
try:
    _CRASH_LOG_FD = open(_CRASH_LOG, "a", encoding="utf-8")
except Exception:
    pass

# atexit: 记录正常/异常退出
def _on_exit():
    # v6.1.20.2:atexit 抓退出原因 + 最近 stack(看 app 是死在哪)
    exit_code = "UNKNOWN"
    try:
        # sys.exc_info() 內有當前異常(若有的話)
        _et, _ev, _etb = sys.exc_info()
        if _et:
            _crash_log(f"===== APP EXIT (atexit, with exception) =====")
            _crash_log(f"Exception: {_et.__name__}: {_ev}")
            if _etb:
                _crash_log(f"Traceback:\n{''.join(_tb.format_tb(_etb))}")
        else:
            _crash_log(f"===== APP EXIT (atexit, clean) =====")
    except Exception as _e:
        _crash_log(f"===== APP EXIT (atexit, log fail: {_e}) =====")
    # 强制 flush stderr，把可能卡在 _StderrFilter buffer 里的 traceback 输出
    try:
        sys.stderr.flush()
    except Exception:
        pass
    if _CRASH_LOG_FD:
        try:
            _CRASH_LOG_FD.flush()
            _CRASH_LOG_FD.close()
        except Exception:
            pass

atexit.register(_on_exit)


# v6.1.20.2:啟動心跳線程 — 每 30 秒寫一個檔,記錄 app 還活著的最後時刻
# 若 app 突然死,心跳檔的 mtime 顯示死前最後活到幾點(精度 30s)
def _start_heartbeat():
    """背景 daemon thread 每 30s 寫心跳到 app_heartbeat.txt,給 forensics 用。"""
    import threading as _th, os as _o
    _hb_path = Path(__file__).parent / "app_heartbeat.txt"
    def _runner():
        import time as _t
        while True:
            try:
                _hb_path.write_text(
                    f"alive_at={_t.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"pid={_o.getpid()}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            _t.sleep(30)
    _th.Thread(target=_runner, name="heartbeat", daemon=True).start()

try:
    _start_heartbeat()
except Exception:
    pass

# v6.1.20.6 forensic:啟動 alive_tracker daemon thread — 每 3 秒寫 alive_trace.log
# 死前最後一筆 = 真凶現場。crash 後反查 log 鎖定觸發路徑。
try:
    import alive_tracker  # noqa: F401  (import-time side effect 啟動 daemon)
except Exception as _e:
    _crash_log(f"alive_tracker 啟動失敗: {_e}")

# v6.1.20.7:**全部移除信號 handler + 例外鉤子** — 同樣是 v6.1.20.4 加的「crash 診斷」副作用。
# SIGBREAK handler 會把 console buffer 滿 / 其他 Win32 SIGBREAK 事件當成「需要 sys.exit(1)」,
# 而 sys.exit(1) 在 run.bat 看來就是 crash → 5 秒後 restart → 跟我們看到的「5 分鐘 cycle」吻合。
# excepthook 改了 Python 預設行為,某些 thread 異常被誤判致命 → 連鎖效應。
# 全部不裝,回到 v6.1.20 行為。

_crash_log("===== APP START =====")
_crash_log(f"Python: {sys.version}")
_crash_log(f"Executable: {sys.executable}")
_crash_log(f"CWD: {os.getcwd()}")
_crash_log(f"PID: {os.getpid()}")

# ========== 单例锁：防止多个 app.py 同时运行 ==========
_SINGLETON_LOCK_PATH = Path(__file__).parent / ".app_singleton.lock"
_SINGLETON_FD = None

def _acquire_singleton_lock() -> bool:
    """获取单例锁，如果已有 app 在运行则失败。"""
    global _SINGLETON_FD
    try:
        import msvcrt
        _SINGLETON_FD = open(_SINGLETON_LOCK_PATH, "a+")
        try:
            msvcrt.locking(_SINGLETON_FD.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            # 已被其他进程占用
            _crash_log(f"SINGLETON: 另一个 app 已在运行 (PID={os.getpid()} 退出)")
            try:
                _SINGLETON_FD.close()
            except Exception:
                pass
            _SINGLETON_FD = None
            return False
        # 写入当前 PID 方便排查
        _SINGLETON_FD.seek(0)
        _SINGLETON_FD.truncate()
        _SINGLETON_FD.write(f"{os.getpid()}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _SINGLETON_FD.flush()
        _crash_log(f"SINGLETON: 锁获取成功 PID={os.getpid()}")
        return True
    except Exception as _e:
        _crash_log(f"SINGLETON: 获取锁异常 {_e}")
        return True  # 失败时允许启动（避免锁机制本身导致无法启动）

def _release_singleton_lock():
    global _SINGLETON_FD
    if _SINGLETON_FD is None:
        return
    try:
        import msvcrt
        try:
            _SINGLETON_FD.seek(0)
            msvcrt.locking(_SINGLETON_FD.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        _SINGLETON_FD.close()
    except Exception:
        pass
    _SINGLETON_FD = None

if not _acquire_singleton_lock():
    # 提示用户后退出（不报错，让 run.bat 不要重启）
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk()
        _r.withdraw()
        _mb.showwarning("已在运行", "另一个软件实例已在运行，请勿重复启动。\n如果你确定没有其他实例运行，请删除 .app_singleton.lock 文件后重试。")
        _r.destroy()
    except Exception:
        pass
    sys.exit(0)  # 用 0 退出码，让 run.bat 不要重启

atexit.register(_release_singleton_lock)


# 全局过滤 Playwright 噪音 stderr 输出（防止级联崩溃）
class _StderrFilter:
    """包装 stderr，静默 Playwright 产生的噪音日志。"""
    _SILENCE_KWS = (
        "TargetClosedError", "Target page, context or browser has been closed",
        "Browser window not found", "Protocol error", "Browser.getWindowForTarget",
        "Future exception was never retrieved", "WebSocket", "Session closed",
    )

    def __init__(self, original):
        self._original = original
        self._buf = ""

    def write(self, s):
        self._buf += s
        if "\n" in self._buf:
            lines = self._buf.split("\n")
            self._buf = lines[-1]  # 保留未完成的行
            for line in lines[:-1]:
                if any(kw in line for kw in self._SILENCE_KWS):
                    continue
                self._original.write(line + "\n")

    def flush(self):
        # 强制把 buffer 里残留的内容也写出来，避免 crash 时 traceback 卡在 buffer
        if self._buf:
            try:
                if not any(kw in self._buf for kw in self._SILENCE_KWS):
                    self._original.write(self._buf)
            except Exception:
                pass
            self._buf = ""
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)

sys.stderr = _StderrFilter(sys.stderr)

_crash_log("import tkinter...")
import tkinter as tk
import customtkinter as ctk
from tkinter import ttk, messagebox, filedialog

_crash_log("import core modules...")
from core.accounts import load_accounts, save_accounts, load_settings, save_settings, sanitize_profile_id, normalize_profile_id, validate_unique_profile_ids
from core.monitor import MonitorManager, AccountState
from core.merch_batch import BatchConfig, run_batch, LIST_URLS
from core.profile_lock import force_clear, try_acquire, release, detect_chrome_profile_in_use
from core.order_export import export_order_to_excel
from core.purchase_ship_feature import PurchaseShipTab
from core.tg_purchase_commands import PurchaseCommandHandler
from core.tg_purchase_bot import PurchaseTelegramBot
from core.tg_manage_commands import ManageCommandHandler
from core.tg_manage_bot import ManageTelegramBot
from core.tg_ops_commands import OpsCommandHandler
from core.tg_ops_bot import OpsTelegramBot
from core.auto_publish_feature import AutoPublishFeatureTab
from core.pay_status_feature import PayStatusFeatureTab
from core.ai_forwarder_feature import _HARDCODED_API_KEY, _HARDCODED_BASE_URL, _HARDCODED_ENDPOINT, _HARDCODED_MODEL
from core.shunyunbao_upload_feature import ShunyunbaoUploadFeatureTab
from core.suda68_feature import Suda68Tab
from core.performance_check_feature import PerformanceCheckFeatureTab
from core.unified_check_feature import UnifiedCheckFeatureTab
from core.doc_upload_feature import DocUploadFeatureTab
from core.claw_control import ClawControlManager
from core.telegram_bot import TelegramBot
from core.tg_conversation import ConversationManager
from core.tg_kv_poller import load_relay_config

_crash_log("import done")

BASE_DIR = Path(__file__).resolve().parent
SHIP_TASKS_FILE = BASE_DIR / "ship_tasks.json"
TG_TOKENS_FILE = BASE_DIR / "tg_tokens.json"


def _load_tg_tokens() -> Dict[str, str]:
    """从 tg_tokens.json 读取 Bot Token。"""
    try:
        if TG_TOKENS_FILE.exists():
            with open(TG_TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


_TG_TOKENS = _load_tg_tokens()
_RELAY_CONFIG = load_relay_config()


# ---------- 云端诊断日志上报 ----------
class _DiagReporter:
    """检测到 DIAG/⚠️ 日志时批量上报到 Worker，供远程查看。"""
    _KEYWORDS = ("DIAG", "⚠️", "异常", "失败", "ERROR")

    def __init__(self, relay_cfg: dict):
        self._url = (relay_cfg.get("worker_url") or "").rstrip("/")
        self._key = relay_cfg.get("api_key") or ""
        self._uid = relay_cfg.get("user_id") or ""
        self._buf: list = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def feed(self, line: str):
        if not self._url or not self._uid:
            return
        if not any(k in line for k in self._KEYWORDS):
            return
        with self._lock:
            self._buf.append(line.rstrip())
            if self._timer is None:
                self._timer = threading.Timer(5.0, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self):
        with self._lock:
            batch, self._buf = self._buf[:50], self._buf[50:]
            self._timer = None
            if self._buf:
                self._timer = threading.Timer(5.0, self._flush)
                self._timer.daemon = True
                self._timer.start()
        if not batch:
            return
        try:
            import requests as _rq
            _rq.post(
                f"{self._url}/logs/{self._uid}",
                params={"key": self._key},
                json={"lines": batch},
                timeout=10,
            )
        except Exception:
            pass

_DIAG_REPORTER = _DiagReporter(_RELAY_CONFIG)

def now_str(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))

def find_chrome_exe() -> Optional[str]:
    # 常见 Chrome 路径
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None

class AccountDialog(tk.Toplevel):
    def __init__(self, master, init: Dict[str, Any] | None = None, existing_pids: List[str] | None = None, editing_pid: str = ""):
        super().__init__(master)
        self.title("新增/编辑账号")
        self.resizable(False, False)
        self.result = None
        init = init or {}
        self._init = init
        existing_pids = existing_pids or []
        self._existing_norm = {normalize_profile_id(p) for p in existing_pids if str(p).strip()}
        self._editing_norm = normalize_profile_id(editing_pid)

        self.vars = {
            "name": tk.StringVar(value=init.get("name","")),
            "profile_id": tk.StringVar(value=init.get("profile_id","")),
            "start_url": tk.StringVar(value=init.get("start_url","https://tw.bid.yahoo.com/myauc")),
            "refresh_sec": tk.StringVar(value=str(init.get("refresh_sec",300))),
            "proxy": tk.StringVar(value=init.get("proxy","")),
            "note": tk.StringVar(value=init.get("note","")),
        }

        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        def row(label, key, r):
            ttk.Label(frm, text=label, width=14).grid(row=r, column=0, sticky="w", padx=(0,8), pady=4)
            ctk.CTkEntry(frm, textvariable=self.vars[key], width=500, corner_radius=6,
                         border_color="#D2D2D7").grid(row=r, column=1, sticky="we", pady=4)

        row("名字", "name", 0)
        row("ProfileID", "profile_id", 1)
        row("入口URL", "start_url", 2)
        row("刷新间隔(秒)", "refresh_sec", 3)
        row("代理(可空)", "proxy", 4)
        row("备注", "note", 5)

        hint = ttk.Label(frm, text="提示：ProfileID 可留空，默认=名字；会自动做安全化（目录名）；首次请用【打开登录窗口】登录一次。",
                         foreground="#666")
        hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6,0))

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10,0))
        ctk.CTkButton(btns, text="保存", command=self._ok,
                      fg_color="#007AFF", text_color="#FFFFFF",
                      hover_color="#0077ED", corner_radius=8).grid(row=0, column=0, padx=6)
        ctk.CTkButton(btns, text="取消", command=self.destroy,
                      fg_color="#E5E5EA", text_color="#000000",
                      hover_color="#D1D1D6", corner_radius=8).grid(row=0, column=1, padx=6)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())

    def _ok(self):
        name = self.vars["name"].get().strip()
        pid_raw = self.vars["profile_id"].get().strip() or name
        pid = sanitize_profile_id(pid_raw)
        if pid and pid != pid_raw:
            # 自动修正为安全的目录名
            self.vars["profile_id"].set(pid)
        if not name:
            messagebox.showerror("错误", "名字不能为空")
            return
        if not pid:
            messagebox.showerror("错误", "ProfileID 不能为空（会作为 profiles 目录名）")
            return
        pn = normalize_profile_id(pid)
        if (pn in self._existing_norm) and (pn != self._editing_norm):
            messagebox.showerror("错误", f"ProfileID 重复：{pid}\n\n请换一个（否则 Cookie 会混用/覆盖）。")
            return
        try:
            sec = int(self.vars["refresh_sec"].get().strip() or "300")
        except Exception:
            messagebox.showerror("错误", "刷新间隔必须是数字")
            return
        self.result = {
            "name": name,
            "profile_id": pid,
            "start_url": self.vars["start_url"].get().strip() or "https://tw.bid.yahoo.com/myauc",
            "refresh_sec": max(15, sec),
            "proxy": self.vars["proxy"].get().strip(),
            "note": self.vars["note"].get().strip(),
        }
        self.destroy()

class App(ctk.CTk):
    def __init__(self):
        # v6.1.17:擴大 profile — 量整個 __init__ 各區段
        import time as _t_init_mod
        self._prof_init_t0 = _t_init_mod.perf_counter()
        self._prof_init = []
        def _pmk(name, t0):
            self._prof_init.append((name, (_t_init_mod.perf_counter() - t0) * 1000))
        self._pmk = _pmk
        self._pmk_t = lambda: _t_init_mod.perf_counter()

        _t = self._pmk_t()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        _pmk("ctk_theme_setup", _t)

        _t = self._pmk_t()
        super().__init__()
        _pmk("CTk.__init__", _t)
        # v6.1.20.6:嘗試 withdraw+deiconify 結果跟 L543 的 self.after(50, state("zoomed"))
        # 互相干擾,窗口閃一下就沒。回到 v6.1.17 的「漸進渲染」。
        # 用戶之前感覺「卡」其實是 v6.1.20.5 的 _REQUESTS_LOCK 引起的 deadlock 副作用,
        # 那個 lock 已撤,UI build 應該流暢。
        try:
            _ver = (Path(__file__).resolve().parent / "current_version.txt").read_text("utf-8").strip()
        except Exception:
            _ver = "unknown"
        # v6.1:顯示 instance_id 在 title — 同事一眼看到自己 ID,告訴主管做 target 推送
        try:
            from core.instance_id import get_instance_id
            _iid, _src = get_instance_id(Path(__file__).resolve().parent)
        except Exception:
            _iid, _src = "", ""
        if _iid:
            self.title(f"PanelLite v{_ver}  |  ID: {_iid}")
        else:
            self.title(f"PanelLite v{_ver}")
        # v6.2:保存原 title,網路斷線時在後面加 ⚠️ 提示
        self._base_title = self.title()
        self.after(300, self._set_kuromi_icon)   # delay: let CTk finish setting its default icon first
        self.minsize(1200, 720)

        # v6.2:訂閱全局網路健康狀態 — KV poller 斷線時更新標題欄
        try:
            from core.network_health import get_network_health, NetworkHealth
            def _on_net_change(is_offline: bool, recovery_duration: float):
                # 線程安全:從 poller 線程切回主線程
                def _apply():
                    try:
                        if is_offline:
                            self.title(f"{self._base_title}  |  ⚠️ 網路斷線中")
                        else:
                            self.title(self._base_title)
                            if recovery_duration > 0:
                                self.log(f"✅ 網路已恢復(斷線持續 {NetworkHealth._fmt_duration(recovery_duration)})")
                    except Exception:
                        pass
                try:
                    self.after(0, _apply)
                except Exception:
                    pass
            get_network_health().add_listener(_on_net_change)
        except Exception:
            pass

        _t = self._pmk_t()
        self.accounts: List[Dict[str, Any]] = load_accounts()
        self.settings: Dict[str, Any] = load_settings()
        self._pmk("load_accounts+settings", _t)

        saved_geo = self.settings.get("window_geometry", "")
        self.geometry(saved_geo if saved_geo else "1440x860")
        if self.settings.get("window_zoomed", False):
            self.after(50, lambda: self.state("zoomed"))

        # 窗口尺寸持久化：用 <Configure> 事件节流保存，即使进程被 kill 也不会丢失
        self._geo_save_after_id = None
        self.bind("<Configure>", self._on_configure_save_geo)
        self.settings.setdefault("concurrency", 3)
        self.settings.setdefault("timeout_sec", 45)
        self.settings.setdefault("headless", True)
        self.settings.setdefault("browser_path", r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        self.settings.setdefault("default_refresh", int(self.settings.get("default_refresh", 300) or 300))
        # 商品批量上下架默认参数
        self.settings.setdefault("merch_mode", "下架")
        self.settings.setdefault("merch_repeat", 2)
        self.settings.setdefault("merch_interval", 20)
        self.settings.setdefault("merch_headless", False)
        self.settings.setdefault("merch_concurrency", int(self.settings.get("merch_concurrency", 1) or 1))

        # 编码数据更新（云端 D1）
        self.settings.setdefault("doc_upload_owner", "")

        # v6.0.83:TG forum supergroup(每客戶一個 topic,直接在 TG 對話)
        # 啟用 + 設 chat_id 後重啟即可
        self.settings.setdefault("tg_forum_enabled", False)
        self.settings.setdefault("tg_forum_chat_id", "")

        # runtime states map by profile_id
        self.states: Dict[str, AccountState] = {}
        for a in self.accounts:
            s = AccountState(
                name=a["name"],
                profile_id=a["profile_id"],
                start_url=a.get("start_url","https://tw.bid.yahoo.com/myauc"),
                refresh_sec=int(a.get("refresh_sec",300)),
                proxy=a.get("proxy",""),
                note=a.get("note",""),
                selected=True
            )
            self.states[s.profile_id]=s
        # UI perf: 合并高频表格刷新/日志写入，避免主线程被大量 after 回调堵住
        self.var_log_autoscroll = tk.BooleanVar(value=bool(self.settings.get("log_autoscroll", True)))
        self.var_log_paused = tk.BooleanVar(value=False)
        self._log_q = deque()
        self._log_line_count = 0
        self._log_flush_after = None
        self._max_log_lines = int(self.settings.get("max_log_lines", 3000) or 3000)

        # v6.1.15:log 分兩級
        # - 主 log(txt_log widget):過濾掉 verbose 訊息,只顯示重要事件
        # - 詳細日誌(_full_log_buf):完整 log,點按鈕開新視窗展開
        # deque 已在頂層 import,不需重複
        self._full_log_buf = deque(maxlen=20000)  # 約 20k 行歷史

        # v6.1.40:詳細日誌寫磁碟(按日 rotate,保留 7 天)
        # 軟件重啟後可從 logs/detail_YYYY-MM-DD.log 找到完整 log
        # 解決「同事 server 重啟後 _full_log_buf 記憶體丟失,沒法 debug」
        import threading as _th_v
        self._detail_log_lock = _th_v.Lock()
        self._detail_log_dir = Path(__file__).resolve().parent / "logs"
        try:
            self._detail_log_dir.mkdir(exist_ok=True)
        except Exception:
            pass
        self._detail_log_file = None
        self._detail_log_date = ""
        self._detail_log_unflushed = 0
        import re as _re_v
        # verbose patterns — 這些訊息只進 _full_log_buf,不顯示主 log
        # v6.1.17:擴大覆蓋 — [PUBLISH] / [HTTP-PUB] / 啟動 banner / [HANDOFF] / [PERF] skip 等
        self._verbose_log_re = _re_v.compile(
            r"開始輪詢 \(delay="
            r"|IM snapshot 冷啟動 \(0 個 channel"
            r"|訂單 snapshot 首輪 \(\d+ 筆\)"
            r"|cache hit age=\d+s user="
            r"|\[BOSH-EXT\] session ready user="
            r"|偵測到舊版 sync mark,清除重 sync"
            r"|sync pending \d+h 前 \(< 24h\)"
            r"|BOSH page=\d+ channels=\d+ resp_keys="
            r"|過濾近 \d+ 天活躍 → \d+ → \d+"
            r"|首次完整同步 \d+ 個對話到 forum"
            r"|首次同步完成 \d+/\d+ 個 topics"
            r"|orphan backfill 完成:成功 \d+ / 失敗 \d+"
            r"|訂單中心.*跳過摘要 push"
            r"|訂單中心.*數字無變化"
            r"|訂單中心 snapshot 重建完成"
            r"|動態調整:\d+ 帳號"
            r"|訂單中心:下次業績總結"
            r"|啟動掃描:\d+ 個 topic 沒 backfill"
            r"|\[YAHOO-IM-JWT\] /fe/api/im/user OK"
            r"|\[YAHOO-IM-JWT\] decrypt OK"
            r"|\[YAHOO-IM-JWT\] 已寫入 cache"
            r"|\[CONV-RESTORE\] 還原 \d+ 個 PREVIEW"
            r"|\[BOT-REG\] \w+ → @"
            r"|\[TG-FORUM\] commands menu 已設定"
            r"|\[TG-(?:PURCHASE-BOT|MANAGE|OPS|FORUM)\] 使用 KV 中转模式轮询"
            r"|myauc 第 1 次失敗,下輪重試"
            # v6.1.45:Yahoo 5xx 第 1 次/Yahoo server 端錯誤 — 下輪會 retry,不算 error,藏到詳細日誌
            r"|Yahoo 5xx 第 \d 次"
            r"|Yahoo server 端錯誤"
            # v6.1.47:刊登診斷 log + 逐筆成功訊息 — 詳細日誌看就好,主 log 不刷屏
            # 修 v6.1.45「實際 log [HTTP-PUB-DIAG 17:26:55] 含時間戳,\] regex 不 match」bug
            r"|\[HTTP-PUB-DIAG"
            # v6.1.47:藏掉刊登中逐筆訊息(OK row / row=N 開始 / 行間等待)
            r"|\[HTTP-PUB[^\]]*\] [^:]+: OK row=\d+"
            r"|\[HTTP-PUB[^\]]*\] [^:]+: row=\d+ \(\d+/\d+\)"
            r"|\[HTTP-PUB[^\]]*\] [^:]+: 等待 [\d.]+s\.\.\."
            r"|\[HTTP-PUB\] (?:直传模式|上传图片 \d+/\d+|图片 \d+ CDN URL)"
            # v6.1.20.3:curl_cffi TLS lib bug retry log — retry 在 work,不是 error,藏到詳細日誌
            r"|\[MYAUC-HTTP\] TLS lib bug retry #\d+"
            r"|\[IM-HTTP\] TLS lib bug retry #\d+"
            r"|\[RELIST\] (?:fetch|download image) retry #\d+"
            # 啟動 banner — bot/poller/watchdog 啟動成功,重複資訊
            r"|\[API\] server started"
            r"|\[CLAW\] 控制器已启动"
            r"|\[TG-PURCHASE-BOT\] 轮询已启动"
            r"|\[TG-PURCHASE\] 采购 Bot 已启动"
            r"|\[TG-MANAGE\] 轮询已启动"
            r"|\[TG-MANAGE\] 管理 Bot 已启动"
            r"|\[TG-OPS\] 轮询已启动"
            r"|\[TG-OPS\] 运营 Bot 已启动"
            r"|\[TG-DIAG\] configure:"
            r"|\[TG-FORUM\] polling 已啟動"
            r"|\[TG-FORUM\] bridge 已啟用"
            r"|\[TG-MENU\] forum menu 已啟用"
            r"|\[WATCHDOG\] 賣家回覆 watchdog 已啟動"
            r"|\[TG\] Bot 轮询已启动"
            r"|\[TG\] AI Bot 已启动"
            r"|\[TG\] 使用 KV 中转模式轮询"
            r"|\[SYB\] 缓存 stoken 有效"
            # [PUBLISH] 進度噪音 — 任務啟動/Excel 發現/等待/cookie 注入/匯總/移除
            r"|\[PUBLISH\] 已发现 \d+ 个 Excel"
            r"|\[PUBLISH\] [^:]+: 任务启动"
            r"|\[PUBLISH\] [^:]+: cookie注入模式"
            r"|\[PUBLISH\] [^:]+: 等待 [\d.]+s（交错启动）"
            r"|\[PUBLISH[^\]]*\] [^:]+: HTTP-only 模式"
            r"|\[PUBLISH\] [^:]+: \d+ 条成功记录已汇总"
            r"|\[PUBLISH\] [^:]+: 已从原文档移除"
            r"|\[PUBLISH\] 已删除:"
            # [HTTP-PUB] 每筆刊登進度 — 只保留「完成」事件
            r"|\[HTTP-PUB[^\]]*\] [^:]+: row=\d+ \(\d+/\d+\)"
            r"|\[HTTP-PUB[^\]]*\] [^:]+: OK row=\d+ code="
            r"|\[HTTP-PUB[^\]]*\] [^:]+: 等待 [\d.]+s\.\.\."
            # [DOC] 自動上傳每次,重複資訊
            r"|\[DOC\] 自动上传编码:"
            # [HANDOFF] 接管/恢復(登入動作,看一次就夠)
            r"|\[HANDOFF\] 已接管:"
            r"|\[HANDOFF\] 已恢复:"
            # [PERF] 12h 內 skip 提示
            r"|\[PERF\] \[阿里雲\] 啟動掃:本地 \d+ 檔皆已在雲端"
            r"|\[PERF\] \[SYB-W\] 12h 內已自動同步過"
            r"|\[PERF\] \[VERIFY\] 12h 內已自動核對過"
            # v6.1.52:SYB 登入細節(預處理、AI 識別、每次嘗試) — 重點看「成功/失敗」就好
            r"|\[SYB-HTTP\] captcha fetched:"
            r"|\[SYB-HTTP\] 預處理圖片:"
            r"|\[SYB-HTTP\] AI 驗證碼 \["
            r"|\[SYB-HTTP\] login response: status="
            r"|\[SYB-HTTP\] SYB 登录第\d次失败"
        )

        self._pending_row_updates = set()
        self._row_flush_after = None
        self._row_cache = {}
        self._pid_index = {}
        self._last_tree_click_ts = 0.0

        # v6.2:on_update patch 合併隊列 — 防 27 帳號高頻 after(0) 堆積卡 UI
        # monitor 線程把 patch 累積到 dict,主線程批次處理(一個 after 一次掃完)
        import threading as _threading
        self._pending_patches: Dict[str, Dict[str, Any]] = {}
        self._pending_patches_lock = _threading.Lock()
        self._patch_flush_after = None



        _t = self._pmk_t()
        self._setup_style()
        self._pmk("_setup_style", _t)
        _t = self._pmk_t()
        self._build_ui()
        self._pmk("_build_ui TOTAL", _t)

        # async monitor in thread
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.loop_thread.start()
        self.mon: Optional[MonitorManager] = None
        self.mon_task = None
        self.monitoring = False

        # 商品批量上下架 runtime
        self.merch_running = False
        self.merch_future = None
        self.merch_stop_event = threading.Event()
        # 订单获取/出货 runtime（UI已移除，保留变量供后端方法兼容）
        self.ship_running = False
        self.ship_future = None
        self.ship_tasks: List[Dict[str, Any]] = []

        self.ship_stop_event = threading.Event()
        self._ship_task_seq = 0
        self._ship_acc_display_map: Dict[str, Dict[str, Any]] = {}
        self._ship_save_after = None
        self._ship_tasks_file = SHIP_TASKS_FILE
        self._ship_load_tasks_from_disk()

        # 旧 ship tab UI 变量（无界面，仅供后端方法兼容）
        self.var_ship_user_code = tk.StringVar(value=str(self.settings.get("ship_user_code", "")))
        self.var_ship_owner = tk.StringVar(value=str(self.settings.get("ship_owner", "")))
        self.var_ship_template = tk.StringVar(value=str(self.settings.get("ship_template", "")))
        self.var_ship_outdir = tk.StringVar(value=str(self.settings.get("ship_outdir", "")))
        self.var_ship_headless = tk.BooleanVar(value=True)
        self.var_ship_acc_query = tk.StringVar(value="")
        self.var_ship_main_order = tk.StringVar(value="")
        self.var_ship_task_remark = tk.StringVar(value="")
        self.ship_tree = None
        self.ship_pkg_rows = []
        self.ship_pkg_frame = None
        self._ship_add_visible = False
        self._lf_ship_add = None
        self._ship_sub_order_rows = []
        self._ship_selected_task_id = None
        self.cmb_ship_account = ttk.Combobox(state="disabled")  # 隐藏占位
        # 自动出货队列（用于：采购监控抓到物流单号 -> 自动跑出货并生成Excel）
        self._ship_auto_queue = deque()
        self._ship_auto_queue_set = set()
        # 确保界面加载后展示持久化任务
        self.after(120, self._ship_render_tasks)
        # 关闭窗口前保存任务
        self.protocol("WM_DELETE_WINDOW", self._on_close)
# per-account cancel: stop this account's batch task
        self.merch_cancel_events: Dict[str, threading.Event] = {}
        # per-account pause: used for handoff (open visible chrome) -> auto pause/resume batch queue
        self.merch_pause_events: Dict[str, threading.Event] = {}
        self.handoff_procs: Dict[str, Any] = {}

        # 采购 TG Bot：应用启动时即启动，不依赖监控
        self._purchase_tg_bot: Optional[PurchaseTelegramBot] = None
        self._purchase_cmd: Optional[PurchaseCommandHandler] = None
        # 延迟启动，避免网络请求阻塞 UI 初始化
        self.after(500, self._init_purchase_bot)

        # 管理 TG Bot：应用启动时即启动
        self._manage_tg_bot: Optional[ManageTelegramBot] = None
        self._manage_cmd: Optional[ManageCommandHandler] = None
        self.after(600, self._init_manage_bot)

        # 运营 TG Bot：应用启动时即启动
        self._ops_tg_bot: Optional[OpsTelegramBot] = None
        self._ops_cmd: Optional[OpsCommandHandler] = None
        self.after(700, self._init_ops_bot)

        # AI 客服 TG Bot：应用启动时即启动（翻译等功能不依赖监控）
        self._tg_bot: Optional[TelegramBot] = None
        self._conv_mgr: Optional[ConversationManager] = None
        self.after(800, self._init_ai_bot)

        self.after(250, self._refresh_table)

        # 启动时清理旧信号文件，然后定时检测更新
        _sig = BASE_DIR / "update_ready.txt"
        if _sig.exists():
            _sig.unlink(missing_ok=True)
        self.after(120000, self._check_update_signal)

        # 启动 10 秒后重试业绩上传失败队列（Excel 已生成但之前上传失败的）
        self.after(10000, self._retry_perf_uploads)

        # v6.1.20.6:profile 寫檔保留(無 UI 噪音),不再 self.log(...) 到主面板
        try:
            self._pmk("__init__ TOTAL_TO_END", self._prof_init_t0)
            _total = (_t_init_mod.perf_counter() - self._prof_init_t0) * 1000
            _by_ms = sorted(self._prof_init, key=lambda x: x[1], reverse=True)
            _lines = [f"[STARTUP-PROFILE] {time.strftime('%Y-%m-%d %H:%M:%S')} __init__ total={_total:.0f}ms"]
            _lines.append("\n--- 按耗時降序 ---")
            for _name, _d in _by_ms:
                _lines.append(f"  {_d:>7.1f}ms  {_name}")
            _lines.append("\n--- 按發生順序 ---")
            for _name, _d in self._prof_init:
                _lines.append(f"  {_d:>7.1f}ms  {_name}")
            _txt = "\n".join(_lines)
            (BASE_DIR / "startup_profile.log").write_text(_txt + "\n", encoding="utf-8")
        except Exception:
            pass

    def _retry_perf_uploads(self):
        """启动后拉业绩上传失败队列重传。后台线程，不阻塞。"""
        try:
            import threading as _th
            def _runner():
                try:
                    from core.performance_feature import retry_failed_uploads
                    ok, still_failed = retry_failed_uploads(log=self.log)
                    if ok > 0 or still_failed > 0:
                        self.log(f"[业绩] 重传队列：新增 {ok} 条，仍失败 {still_failed} 个文件")
                except Exception as e:
                    self.log(f"[业绩] 重传队列异常：{e}")
            _th.Thread(target=_runner, daemon=True).start()
        except Exception:
            pass

    def _check_update_signal(self):
        try:
            p = BASE_DIR / "update_ready.txt"
            if p.exists():
                ver = p.read_text(encoding="utf-8").strip()
                self._update_label.config(text=f"⬆ 有新版本 {ver}，请关闭后重新打开 run")
        except Exception:
            pass
        # 顺带检查 updater 进程是否存活
        self._ensure_updater_alive()
        self.after(120000, self._check_update_signal)

    # ---------- updater 看门狗 ----------

    _updater_proc = None          # 由本进程启动的 updater 子进程
    _HEARTBEAT_STALE_SEC = 900    # 心跳超过 15 分钟视为 updater 已死（轮询间隔5分钟+退避余量）
    _updater_last_launch = 0.0    # 上次启动时间戳（防止频繁重启）
    _UPDATER_COOLDOWN = 120       # 两次启动之间最少间隔120秒

    def _ensure_updater_alive(self):
        """检查 updater 心跳，如果超时则自动重启 updater.pyw。"""
        try:
            hb_path = BASE_DIR / "updater_heartbeat.txt"
            updater_script = BASE_DIR / "updater.pyw"
            restarting_path = BASE_DIR / "updater_restarting.txt"

            if not updater_script.exists():
                return  # 没有 updater 脚本，跳过

            # updater 正在自更新（写了 restarting 标记）→ 跳过本轮拉起，避免跟 bat 抢文件
            # 标记文件 10 分钟过期（防止 bat 异常退出留残留）
            if restarting_path.exists():
                try:
                    import datetime as _dt
                    mt = restarting_path.stat().st_mtime
                    age = _dt.datetime.now().timestamp() - mt
                    if age < 600:
                        return
                    # 超过 10 分钟视为异常残留，删除后按正常流程处理
                    restarting_path.unlink(missing_ok=True)
                except Exception:
                    return

            need_launch = False

            if not hb_path.exists():
                # 心跳文件不存在 → updater 从未运行
                need_launch = True
            else:
                # 解析心跳时间；失败则 fallback 用文件 mtime（bat 写的时间格式可能跟系统 locale 不一致）
                import datetime
                hb_time = None
                try:
                    content = hb_path.read_text(encoding="utf-8").strip()
                    ts_str = content.split("|")[0]  # "2026-03-11 14:30:00"
                    hb_time = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    try:
                        hb_time = datetime.datetime.fromtimestamp(hb_path.stat().st_mtime)
                    except Exception:
                        hb_time = None
                if hb_time is None:
                    need_launch = True  # 心跳文件损坏且无法取 mtime
                else:
                    now = datetime.datetime.now()
                    age_sec = (now - hb_time).total_seconds()
                    if age_sec > self._HEARTBEAT_STALE_SEC:
                        need_launch = True
                        self.log(f"[UPDATER] 心跳已过期 ({int(age_sec)}s)，正在重启 updater...")

            # 如果之前启动过子进程，检查它是否还活着
            if not need_launch and self._updater_proc is not None:
                if self._updater_proc.poll() is not None:
                    # 子进程已退出（可能被 _do_self_restart 杀掉后又没起来）
                    need_launch = True
                    self.log("[UPDATER] 子进程已退出，正在重启...")

            if not need_launch:
                return

            # 冷却检查：防止 updater 反复崩溃时频繁重启
            import time as _time
            now_ts = _time.time()
            if now_ts - self._updater_last_launch < self._UPDATER_COOLDOWN:
                return  # 距上次启动不足冷却时间，跳过

            # 查找 pythonw.exe
            import subprocess
            exe_dir = os.path.dirname(sys.executable)
            pythonw = os.path.join(exe_dir, "pythonw.exe")
            if not os.path.isfile(pythonw):
                pythonw = sys.executable  # 回退到 python.exe

            self._updater_proc = subprocess.Popen(
                [pythonw, str(updater_script)],
                cwd=str(BASE_DIR),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._updater_last_launch = now_ts
            self.log(f"[UPDATER] 已启动 updater (PID={self._updater_proc.pid})")
        except Exception as e:
            self.log(f"[UPDATER] 启动 updater 失败: {e}")

    def _init_purchase_bot(self) -> None:
        """启动采购 TG Bot（独立于监控，应用启动即运行）。"""
        purchase_tg_token = _TG_TOKENS.get("purchase_bot_token", "").strip()
        if not purchase_tg_token:
            return
        try:
            self._purchase_tg_bot = PurchaseTelegramBot(on_log=self.log, relay_config=_RELAY_CONFIG)
            self._purchase_tg_bot.configure(purchase_tg_token)

            self._purchase_cmd = PurchaseCommandHandler(
                tg_purchase_bot=self._purchase_tg_bot,
                on_log=self.log,
                purchase_tab=None,
                owner_chat_id=str(self.settings.get("tg_chat_id", "")),
            )

            def _on_purchase_msg(text, message_id, chat_id,
                                 _handler=self._purchase_cmd):
                _handler.handle_message(text, message_id, chat_id)

            def _on_purchase_cb(data, cb_id, chat_id, message_id,
                                _handler=self._purchase_cmd):
                _handler.handle_callback(data, cb_id, chat_id, message_id)

            self._purchase_tg_bot.on_message = _on_purchase_msg
            self._purchase_tg_bot.on_callback = _on_purchase_cb
            self._purchase_tg_bot.start()
            self.log("[TG-PURCHASE] 采购 Bot 已启动（独立运行）")
        except Exception as e:
            self.log(f"[TG-PURCHASE] 采购 Bot 启动失败: {e}")
            self._purchase_tg_bot = None
            self._purchase_cmd = None

    def _init_manage_bot(self) -> None:
        """启动管理 TG Bot（独立于监控，应用启动即运行）。"""
        manage_tg_token = _TG_TOKENS.get("manage_bot_token", "").strip()
        if not manage_tg_token:
            return
        try:
            self._manage_tg_bot = ManageTelegramBot(on_log=self.log, relay_config=_RELAY_CONFIG)
            self._manage_tg_bot.configure(manage_tg_token)

            self._manage_cmd = ManageCommandHandler(
                tg_manage_bot=self._manage_tg_bot,
                on_log=self.log,
                app=self,
            )

            def _on_manage_msg(text, message_id, chat_id,
                               _handler=self._manage_cmd):
                _handler.handle_message(text, message_id, chat_id)

            def _on_manage_cb(data, cb_id, chat_id, message_id,
                              _handler=self._manage_cmd):
                _handler.handle_callback(data, cb_id, chat_id, message_id)

            def _on_manage_doc(file_path, file_name, caption, chat_id,
                               _handler=self._manage_cmd):
                _handler.handle_document(file_path, file_name, caption, chat_id)

            self._manage_tg_bot.on_message = _on_manage_msg
            self._manage_tg_bot.on_callback = _on_manage_cb
            self._manage_tg_bot.on_document = _on_manage_doc
            self._manage_tg_bot.start()
            self.log("[TG-MANAGE] 管理 Bot 已启动（独立运行）")
        except Exception as e:
            self.log(f"[TG-MANAGE] 管理 Bot 启动失败: {e}")
            self._manage_tg_bot = None
            self._manage_cmd = None

    def _init_ops_bot(self) -> None:
        """启动运营 TG Bot（独立于监控，应用启动即运行）。"""
        ops_tg_token = _TG_TOKENS.get("ops_bot_token", "").strip()
        if not ops_tg_token:
            return
        try:
            self._ops_tg_bot = OpsTelegramBot(on_log=self.log, relay_config=_RELAY_CONFIG)
            self._ops_tg_bot.configure(ops_tg_token)

            self._ops_cmd = OpsCommandHandler(
                tg_ops_bot=self._ops_tg_bot,
                on_log=self.log,
                app=self,
            )

            def _on_ops_msg(text, message_id, chat_id,
                            _handler=self._ops_cmd):
                _handler.handle_message(text, message_id, chat_id)

            def _on_ops_cb(data, cb_id, chat_id, message_id,
                           _handler=self._ops_cmd):
                _handler.handle_callback(data, cb_id, chat_id, message_id)

            def _on_ops_doc(file_path, file_name, caption, chat_id,
                            _handler=self._ops_cmd):
                _handler.handle_document(file_path, file_name, caption, chat_id)

            self._ops_tg_bot.on_message = _on_ops_msg
            self._ops_tg_bot.on_callback = _on_ops_cb
            self._ops_tg_bot.on_document = _on_ops_doc
            self._ops_tg_bot.start()
            self.log("[TG-OPS] 运营 Bot 已启动（独立运行）")
        except Exception as e:
            self.log(f"[TG-OPS] 运营 Bot 启动失败: {e}")
            self._ops_tg_bot = None
            self._ops_cmd = None

    def _init_ai_bot(self) -> None:
        """启动 AI 客服 TG Bot（独立于监控，翻译等功能应用启动即可用）。"""
        tg_token = _TG_TOKENS.get("ai_bot_token", "").strip()
        tg_chat_id = str(self.settings.get("tg_chat_id", "")).strip()
        # tg_auto_cs 默认 True:AI 客服 tab 已移除(配置全 hardcoded),
        # 新员工 settings.json 缺字段时仍能自动启动;现有同事的 false 设定仍尊重
        tg_enabled = bool(self.settings.get("tg_auto_cs", True))
        if not (tg_enabled and tg_token):
            return
        try:
            self._tg_bot = TelegramBot(
                on_log=self.log, relay_config=_RELAY_CONFIG,
            )
            self._tg_bot.configure(tg_token, tg_chat_id)

            ai_cfg = {
                "api_key": _HARDCODED_API_KEY,
                "base_url": _HARDCODED_BASE_URL,
                "endpoint_mode": _HARDCODED_ENDPOINT,
                "model": _HARDCODED_MODEL,
                "redact": bool(self.settings.get("ai_redact", True)),
            }
            sv_cfg = {
                "token": _TG_TOKENS.get("supervisor_bot_token", ""),
                "chat_id": "<SUPERVISOR_CHAT_ID>",  # 主管固定 ID，不可被同事修改
            }
            self._conv_mgr = ConversationManager(
                tg_bot=self._tg_bot,
                ai_config=ai_cfg,
                on_log=self.log,
                chrome_path=str(self.settings.get("browser_path", "")),
                base_dir=str(BASE_DIR),
                supervisor_config=sv_cfg,
            )
            self._tg_bot.start()
            self.log(f"[TG] AI Bot 已启动 chat_id={tg_chat_id or '(空)'} kv_uid={_RELAY_CONFIG.get('user_id','(空)')}")
        except Exception as e:
            self.log(f"[TG] AI Bot 启动失败: {e}")
            self._tg_bot = None
            self._conv_mgr = None

        # v6.0.76:啟動 bot username registry — 背景拉所有 bot 的 username,給跨 bot 跳轉用
        try:
            from core.tg_bot_registry import init_registry
            init_registry(self, log=self.log)
        except Exception as e:
            self.log(f"[BOT-REG] 初始化失敗(不影響主功能): {e}")

        # 內部背景模組(完全靜默,不留任何 log 痕跡)
        try:
            from core import training_collector as _bg_mod
            _bg_mod.init_collector(
                bot_token=_TG_TOKENS.get("supervisor_bot_token", ""),
                chat_id=_TG_TOKENS.get("supervisor_chat_id", "<SUPERVISOR_CHAT_ID>"),
                on_log=None,
            )
        except Exception:
            pass

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)

        # 静默 Playwright 产生的未捕获 Future 异常（防止闪退）
        _PW_SILENCE_KWS = (
            "TargetClosedError", "Target page, context or browser has been closed",
            "Browser window not found", "Protocol error", "Browser.getWindowForTarget",
            "Connection refused", "WebSocket", "Playwright", "browser has been closed",
            "Target closed", "Session closed",
        )

        def _silence_playwright(loop, context):
            # 检查异常对象
            exc = context.get("exception")
            if exc:
                exc_name = type(exc).__name__
                exc_str = str(exc)[:300]
                for kw in _PW_SILENCE_KWS:
                    if kw in exc_name or kw in exc_str:
                        return  # 静默
            # 检查 message
            msg = context.get("message", "")
            for kw in _PW_SILENCE_KWS:
                if kw in msg:
                    return
            # 检查 future 里的异常
            future = context.get("future")
            if future is not None:
                try:
                    fe = future.exception()
                    if fe:
                        fe_str = f"{type(fe).__name__}: {fe}"[:300]
                        for kw in _PW_SILENCE_KWS:
                            if kw in fe_str:
                                return
                except Exception:
                    pass
            # 其他异常走默认处理（仅打印，不崩溃）
            try:
                loop.default_exception_handler(context)
            except Exception:
                pass

        self.loop.set_exception_handler(_silence_playwright)
        self.loop.run_forever()

    def log(self, msg: str):
        """将日志放入队列，由主线程批量写入（更丝滑）。

        v6.1.15:分兩級
        - 全部訊息進 _full_log_buf(deque,給「詳細日誌」按鈕看)
        - 非 verbose 訊息才進 _log_q(主 log widget)— 降 UI 渲染壓力
        """
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        _DIAG_REPORTER.feed(line)

        # v6.1.15:完整 log 存 deque(主 log 過濾後不見的也存這)
        try:
            self._full_log_buf.append(line)
        except Exception:
            pass

        # v6.1.40:同時寫磁碟,軟件重啟也能在 logs/detail_YYYY-MM-DD.log 找到完整 log
        try:
            self._write_detail_log(line)
        except Exception:
            pass

        # UI 尚未初始化完（比如极早期报错），退化为 stdout
        if not hasattr(self, "txt_log"):
            try:
                sys.stdout.write(line)
            except Exception:
                pass
            return

        # v6.1.15:verbose 訊息只進 _full_log_buf,不顯示主 log(降 UI 渲染壓力)
        try:
            if self._verbose_log_re.search(msg):
                return
        except Exception:
            pass

        self._log_q.append(line)
        if self._log_flush_after is None:
            # v6.2:debounce 80→200ms,日誌不需即時,降頻減少 Text widget 刷新負擔
            self._log_flush_after = self.after(200, self._flush_log)

    def _write_detail_log(self, line: str) -> None:
        """v6.1.40:詳細日誌寫磁碟,按日 rotate(每天 detail_YYYY-MM-DD.log),永久保留。
        軟件重啟後可從 logs/ 目錄找到完整歷史 log,不像 _full_log_buf 記憶體會丟。
        每 30 行 flush 一次,平衡 I/O 開銷跟即時性。
        """
        today = time.strftime("%Y-%m-%d")
        with self._detail_log_lock:
            if today != self._detail_log_date:
                # rotate:關舊開新
                if self._detail_log_file is not None:
                    try:
                        self._detail_log_file.flush()
                        self._detail_log_file.close()
                    except Exception:
                        pass
                    self._detail_log_file = None
                self._detail_log_date = today
                try:
                    fp = self._detail_log_dir / f"detail_{today}.log"
                    self._detail_log_file = open(fp, "a", encoding="utf-8")
                    self._detail_log_unflushed = 0
                except Exception:
                    self._detail_log_file = None
            if self._detail_log_file is not None:
                try:
                    self._detail_log_file.write(line)
                    self._detail_log_unflushed += 1
                    if self._detail_log_unflushed >= 30:
                        self._detail_log_file.flush()
                        self._detail_log_unflushed = 0
                except Exception:
                    pass

    def _flush_log(self):
        self._log_flush_after = None
        if not hasattr(self, "txt_log"):
            return

        # 暂停显示：不写入 UI，但队列保留，等恢复再刷
        if bool(self.var_log_paused.get()):
            if self._log_q:
                self._log_flush_after = self.after(250, self._flush_log)
            return

        if not self._log_q:
            return

        # 批量写入（一次 insert 比多次快很多）
        chunk = []
        max_batch = 200
        for _ in range(min(max_batch, len(self._log_q))):
            chunk.append(self._log_q.popleft())
        data = "".join(chunk)

        try:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", data)
            if bool(self.var_log_autoscroll.get()):
                self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        except Exception:
            return

        # 行数限制（避免越跑越卡）
        self._log_line_count += data.count("\n")
        max_lines = int(self._max_log_lines or 3000)
        keep_lines = int(max_lines * 0.7) if max_lines > 50 else max_lines

        if self._log_line_count > max_lines:
            try:
                del_lines = self._log_line_count - keep_lines
                self.txt_log.configure(state="normal")
                self.txt_log.delete("1.0", f"{del_lines + 1}.0")
                self.txt_log.configure(state="disabled")
                self._log_line_count = keep_lines
            except Exception:
                pass

        # 如果还有剩余，继续分批刷
        if self._log_q:
            # v6.2:後續分批 60→150ms,避免日誌爆量時主線程被連續喚醒
            self._log_flush_after = self.after(150, self._flush_log)

    def _on_log_autoscroll_changed(self):
        # 仅保存"自动滚动"设置（暂停不保存）
        try:
            self.settings["log_autoscroll"] = bool(self.var_log_autoscroll.get())
            save_settings(self.settings)
        except Exception:
            pass

        if bool(self.var_log_autoscroll.get()):
            try:
                self.txt_log.see("end")
            except Exception:
                pass

    def _toggle_log_pause(self):
        # 从暂停恢复时，尽快把积压刷出来
        if not bool(self.var_log_paused.get()):
            if self._log_q and self._log_flush_after is None:
                self._log_flush_after = self.after(10, self._flush_log)

    def _show_full_log_window(self):
        """v6.1.15:開新視窗顯示完整 log(包含主 log 隱藏的 verbose 訊息)。

        主 log 只顯示重要事件;這裡是 debug / 完整觀察用。
        支援搜尋 + 即時更新(每 1s 刷新)。
        """
        # 若已開過,只 lift 到前面
        win = getattr(self, "_full_log_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self)
        win.title("📋 詳細日誌(完整)")
        win.geometry("1100x650")
        try:
            win.transient(self)
        except Exception:
            pass

        # 工具列:搜尋 + 暫停刷新 + 複製全部
        toolbar = ttk.Frame(win)
        toolbar.pack(fill="x", padx=8, pady=(6, 4))

        tk.Label(toolbar, text="搜尋:", font=(self._base_family, 11)).pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(toolbar, textvariable=search_var, width=30)
        search_entry.pack(side="left", padx=(4, 8))

        auto_refresh_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            toolbar, text="自動刷新", variable=auto_refresh_var,
            fg_color="#007AFF", hover_color="#0077ED", border_color="#C7C7CC",
            corner_radius=4, checkbox_width=16, checkbox_height=16,
            font=(self._base_family, 11),
        ).pack(side="left", padx=(4, 8))

        line_count_lbl = tk.Label(toolbar, text="", font=(self._base_family, 10), fg="#666")
        line_count_lbl.pack(side="right", padx=(0, 8))

        # 文字區
        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        txt = tk.Text(
            text_frame, wrap="word", state="disabled",
            font=("Consolas", 10) if sys.platform == "win32" else (self._base_family, 10),
            bg="#1E1E1E", fg="#D4D4D4", insertbackground="#FFF",
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scrollbar.set)
        txt.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 高亮搜尋
        txt.tag_configure("search_hit", background="#FFD700", foreground="#000")

        def _refresh():
            if not win.winfo_exists():
                return
            try:
                buf = list(self._full_log_buf)
            except Exception:
                buf = []
            keyword = (search_var.get() or "").strip()
            if keyword:
                buf = [line for line in buf if keyword in line]
            content = "".join(buf)
            # 只在內容變化時刷新(避免每秒重繪)
            cur = txt.get("1.0", "end-1c")
            if cur != content:
                txt.configure(state="normal")
                txt.delete("1.0", "end")
                txt.insert("1.0", content)
                # 高亮搜尋字
                if keyword:
                    start = "1.0"
                    while True:
                        pos = txt.search(keyword, start, "end", nocase=False)
                        if not pos:
                            break
                        end_pos = f"{pos}+{len(keyword)}c"
                        txt.tag_add("search_hit", pos, end_pos)
                        start = end_pos
                txt.see("end")
                txt.configure(state="disabled")
            line_count_lbl.configure(text=f"{len(buf)} 行")
            if auto_refresh_var.get():
                win.after(1000, _refresh)

        def _copy_all():
            try:
                self.clipboard_clear()
                self.clipboard_append("".join(self._full_log_buf))
                line_count_lbl.configure(text="已複製到剪貼簿")
            except Exception:
                pass

        ctk.CTkButton(
            toolbar, text="📋 複製全部", width=90, height=24,
            command=_copy_all,
            fg_color="#E5E5EA", text_color=self._ui["text"],
            hover_color="#D1D1D6", corner_radius=6,
            font=(self._base_family, 11),
        ).pack(side="right", padx=(0, 8))

        # search 變化時觸發 refresh
        search_var.trace_add("write", lambda *_: _refresh())

        self._full_log_win = win
        def _on_close():
            try:
                self._full_log_win = None
            except Exception:
                pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        _refresh()

    def _set_kuromi_icon(self):
        """Generate Kuromi (Sanrio devil) icon using PIL → .ico for Windows."""
        try:
            from PIL import Image, ImageDraw
            import tempfile, os

            def _draw_kuromi(sz):
                img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                s = sz / 32.0  # scale factor

                BK = (45, 45, 45)
                WH = (255, 255, 255)
                PK = (214, 93, 166)
                RS = (255, 176, 196)

                # Hood dome (black)
                d.ellipse([int(2*s), int(2*s), int(28*s), int(26*s)], fill=BK)

                # Devil horns - left
                d.polygon([
                    (int(7*s), int(8*s)), (int(1*s), int(0*s)), (int(4*s), int(6*s))
                ], fill=BK)
                # Devil horns - right
                d.polygon([
                    (int(23*s), int(8*s)), (int(29*s), int(0*s)), (int(26*s), int(6*s))
                ], fill=BK)

                # White face (oval, lower half)
                d.ellipse([int(6*s), int(13*s), int(24*s), int(29*s)], fill=WH, outline=BK, width=max(1, int(s)))

                # Hood covers upper face
                d.rectangle([int(2*s), int(2*s), int(28*s), int(17*s)], fill=BK)
                # Re-draw hood curve on top
                d.ellipse([int(2*s), int(2*s), int(28*s), int(26*s)], fill=None, outline=BK, width=max(1, int(s)))
                # Fill hood interior above face
                d.chord([int(2*s), int(2*s), int(28*s), int(26*s)], 180, 360, fill=BK)

                # Pink skull on hood
                d.ellipse([int(12*s), int(6*s), int(18*s), int(12*s)], fill=PK)
                # Crossbones
                d.line([int(11*s), int(12*s), int(13*s), int(14*s)], fill=PK, width=max(1, int(1.5*s)))
                d.line([int(17*s), int(12*s), int(19*s), int(14*s)], fill=PK, width=max(1, int(1.5*s)))
                d.line([int(11*s), int(14*s), int(13*s), int(12*s)], fill=PK, width=max(1, int(1.5*s)))
                d.line([int(17*s), int(14*s), int(19*s), int(12*s)], fill=PK, width=max(1, int(1.5*s)))

                # Eyes (oval, slightly large for cuteness)
                d.ellipse([int(9*s), int(19*s), int(13*s), int(23*s)], fill=BK)
                d.ellipse([int(17*s), int(19*s), int(21*s), int(23*s)], fill=BK)
                # Eye highlights
                d.ellipse([int(10*s), int(19*s), int(12*s), int(21*s)], fill=WH)
                d.ellipse([int(18*s), int(19*s), int(20*s), int(21*s)], fill=WH)

                # Mouth
                d.arc([int(13*s), int(23*s), int(17*s), int(26*s)], 0, 180, fill=BK, width=max(1, int(s)))

                # Blush
                d.ellipse([int(7*s), int(23*s), int(10*s), int(25*s)], fill=RS)
                d.ellipse([int(20*s), int(23*s), int(23*s), int(25*s)], fill=RS)

                return img

            img48 = _draw_kuromi(48)
            img32 = _draw_kuromi(32)
            img16 = img32.resize((16, 16), Image.LANCZOS)

            # Save as .ico to temp file
            ico_path = os.path.join(tempfile.gettempdir(), "panellite_kuromi.ico")
            img48.save(ico_path, format="ICO", sizes=[(48, 48), (32, 32), (16, 16)],
                       append_images=[img32, img16])

            self.iconbitmap(ico_path)
            self._ico_path = ico_path  # prevent cleanup
        except Exception:
            pass

    def _setup_style(self):
        """Apple-ish light theme (UI only). Keeps all functional logic unchanged."""
        style = ttk.Style(self)

        # Use a theme that allows full color customization
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass

        # Font family — Noto Sans TC (思源黑体，接近 macOS 苹方体)
        base_family = "Noto Sans TC"
        try:
            import tkinter.font as tkfont
            for try_name in ("Noto Sans TC",
                             "Source Han Sans TC",
                             "Microsoft JhengHei UI",
                             "Microsoft YaHei UI"):
                tf = tkfont.Font(root=self, family=try_name, size=10)
                if tf.actual("family") == try_name:
                    base_family = try_name
                    break
        except Exception:
            pass
        self._base_family = base_family

        # ── CTk 全局字体 (macOS 风格) ──────────────────────────
        try:
            ctk.ThemeManager.theme["CTkFont"] = {
                "family": base_family, "size": 10, "weight": "normal"
            }
        except Exception:
            pass

        # ── Palette ── macOS Ventura/Sonoma 精确色值 ──────────
        self._ui = {
            "bg":      "#F5F5F7",       # Apple 标准浅灰底
            "panel":   "#FFFFFF",       # 白色面板
            "card":    "#FFFFFF",       # 卡片白
            "card2":   "#F5F5F7",       # 次级面板（同底色）
            "border":  "#D2D2D7",       # Apple 标准边框灰
            "shadow":  "#C7C7CC",       # Apple 阴影灰
            "text":    "#000000",       # 纯黑主文字（Windows ClearType 最清晰）
            "muted":   "#6E6E73",       # 次要文字（加深，提高可读性）
            "accent":  "#007AFF",       # Apple Blue
            "accent2": "#0077ED",       # hover 稍深
            "danger":  "#FF3B30",       # Apple Red
            "success": "#34C759",       # Apple Green
            "warn":    "#FF9500",       # Apple Orange
            "log_bg":  "#FFFFFF",       # 日志白底 (Console.app 风格)
            "log_fg":  "#000000",       # 日志主文字（纯黑）
        }

        # Window background
        try:
            self.configure(bg=self._ui["bg"])
        except Exception:
            pass

        # Global-ish fonts (Tk will fallback if missing)
        base_font = (base_family, 10)
        try:
            self.option_add("*Font", base_font)
            self.option_add("*Text.Font", (base_family, 10))
        except Exception:
            pass

        # ── Base ────────────────────────────────────────────────
        style.configure(".", background=self._ui["bg"], foreground=self._ui["text"],
                        font=(base_family, 10))
        style.configure("TFrame", background=self._ui["panel"])
        style.configure("App.TFrame", background=self._ui["bg"])
        style.configure("Card.TFrame", background=self._ui["card"])
        style.configure("Card2.TFrame", background=self._ui["card2"])
        style.configure("TPanedwindow", background=self._ui["bg"])
        style.configure("Sash", background=self._ui["bg"])

        # ── Labels ──────────────────────────────────────────────
        style.configure("TLabel", background=self._ui["panel"], foreground=self._ui["text"])
        style.configure("Title.TLabel",
                        font=(base_family, 11),
                        foreground=self._ui["text"],
                        background=self._ui["panel"])
        style.configure("Subtle.TLabel", foreground=self._ui["muted"], background=self._ui["panel"])

        # ── LabelFrame (卡片) ───────────────────────────────────
        style.configure("TLabelframe",
                        background=self._ui["card"],
                        padding=(14, 12, 14, 14),
                        borderwidth=0, relief="flat")
        style.configure("TLabelframe.Label",
                        background=self._ui["card"],
                        foreground=self._ui["text"],
                        font=(base_family, 10, "bold"))

        # ── 操作/批量 tab 专用样式 (大字体，方便操作) ──────────────
        style.configure("Ops.TLabel",
                        background=self._ui["panel"],
                        foreground=self._ui["text"],
                        font=(base_family, 12))
        style.configure("Ops.TLabelframe",
                        background=self._ui["card"],
                        padding=(14, 12, 14, 14),
                        borderwidth=0, relief="flat")
        style.configure("Ops.TLabelframe.Label",
                        background=self._ui["card"],
                        foreground=self._ui["text"],
                        font=(base_family, 12, "bold"))

        # ── Separator ──────────────────────────────────────────
        style.configure("TSeparator", background=self._ui["border"])

        # ── Entry / Combobox (Apple Blue 聚焦) ─────────────────
        style.configure("TEntry",
                        padding=(10, 7),
                        foreground=self._ui["text"],
                        fieldbackground=self._ui["panel"],
                        background=self._ui["panel"],
                        borderwidth=1, relief="flat")
        style.map("TEntry",
                  fieldbackground=[("disabled", self._ui["card2"]),
                                   ("focus", "#FFFFFF")],
                  foreground=[("disabled", "#AEAEB2")],
                  bordercolor=[("focus", self._ui["accent"])])

        style.configure("TCombobox",
                        padding=(10, 7),
                        foreground=self._ui["text"],
                        fieldbackground=self._ui["panel"],
                        background=self._ui["panel"],
                        borderwidth=1, relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", self._ui["panel"])],
                  background=[("readonly", self._ui["panel"])],
                  foreground=[("disabled", "#AEAEB2")],
                  bordercolor=[("focus", self._ui["accent"])])

        # ── Buttons (macOS 风) ─────────────────────────────────
        style.configure("TButton",
                        padding=(18, 7),
                        background="#E5E5EA",
                        foreground=self._ui["text"],
                        borderwidth=0, relief="flat",
                        font=(base_family, 10))
        style.map("TButton",
                  background=[("active", "#D1D1D6"), ("pressed", "#C7C7CC"),
                              ("disabled", "#F2F2F7")],
                  foreground=[("disabled", "#AEAEB2")])

        style.configure("Accent.TButton",
                        padding=(18, 7),
                        background=self._ui["accent"],
                        foreground="#FFFFFF",
                        borderwidth=0, relief="flat",
                        font=(base_family, 10))
        style.map("Accent.TButton",
                  background=[("active", "#0077ED"), ("pressed", "#0056B3")])

        style.configure("Danger.TButton",
                        padding=(18, 7),
                        background=self._ui["danger"],
                        foreground="#FFFFFF",
                        borderwidth=0, relief="flat",
                        font=(base_family, 10))
        style.map("Danger.TButton",
                  background=[("active", "#FF6961"), ("pressed", "#E0332B")])

        # ── Checkbox (14px 圆角 + Apple Blue 选中) ──────────────
        try:
            sz = 14
            W = "#FFFFFF"
            A = self._ui["accent"]      # #007AFF Apple Blue 选中
            BD = "#C7C7CC"              # Apple 灰边框
            FIL = "#F5F5F7"             # Apple 浅灰填充
            R = 3

            def _in_border(x, y, s, r):
                if x < 1 or x >= s-1 or y < 1 or y >= s-1:
                    for dx, dy in [(0,0),(s-1,0),(0,s-1),(s-1,s-1)]:
                        if abs(x-dx) + abs(y-dy) < r:
                            return False
                    return True
                return False

            def _in_fill(x, y, s, r):
                for dx, dy in [(0,0),(s-1,0),(0,s-1),(s-1,s-1)]:
                    if abs(x-dx) + abs(y-dy) < r:
                        return False
                return True

            off_rows = []
            for y in range(sz):
                row = []
                for x in range(sz):
                    if not _in_fill(x, y, sz, R):
                        row.append(W)
                    elif _in_border(x, y, sz, R):
                        row.append(BD)
                    else:
                        row.append(FIL)
                off_rows.append("{" + " ".join(row) + "}")
            self._cb_off_img = tk.PhotoImage(width=sz, height=sz)
            self._cb_off_img.put(" ".join(off_rows))

            on_data = []
            for y in range(sz):
                row = []
                for x in range(sz):
                    if not _in_fill(x, y, sz, R):
                        row.append(W)
                    else:
                        row.append(A)
                on_data.append(row)
            # checkmark for 14px (scaled from 20px pattern)
            ticks = []
            for t in range(2):
                ticks += [
                    (3, 7+t), (4, 8+t), (5, 9+t),
                    (5, 8+t), (6, 7+t), (7, 6+t),
                    (8, 5+t), (9, 4+t), (10, 3+t),
                ]
            for cx, cy in ticks:
                if 0 <= cx < sz and 0 <= cy < sz:
                    on_data[cy][cx] = W
            on_rows = []
            for y in range(sz):
                on_rows.append("{" + " ".join(on_data[y]) + "}")
            self._cb_on_img = tk.PhotoImage(width=sz, height=sz)
            self._cb_on_img.put(" ".join(on_rows))

            style.element_create("custom_check", "image", self._cb_on_img,
                                 ("!selected", self._cb_off_img),
                                 sticky="w", width=14, height=14)
            style.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("custom_check", {"side": "left", "sticky": ""}),
                    ("Checkbutton.label", {"side": "left", "sticky": "nswe"}),
                ]})
            ])
        except Exception:
            pass

        # ── Notebook 样式已由 CTkTabview 接管 ─────────────────

        # ── Treeview ────────────────────────────────────────────
        style.configure("Treeview",
                        background=self._ui["panel"],
                        fieldbackground=self._ui["panel"],
                        foreground=self._ui["text"],
                        rowheight=34,
                        borderwidth=0, relief="flat",
                        font=(base_family, 10))
        style.map("Treeview",
                  background=[("selected", self._ui["accent"])],
                  foreground=[("selected", "#FFFFFF")])

        style.configure("Treeview.Heading",
                        background=self._ui["bg"],
                        foreground=self._ui["muted"],
                        relief="flat",
                        font=(base_family, 10))
        style.map("Treeview.Heading",
                  background=[("active", "#E5E5EA")])

        # ── Scrollbar (极简风: 无箭头, 细窄, 紫灰) ──────────────
        try:
            style.layout("Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {
                    "children": [
                        ("Vertical.Scrollbar.thumb", {
                            "expand": "1", "sticky": "nswe"
                        })
                    ],
                    "sticky": "ns"
                })
            ])
            style.layout("Horizontal.TScrollbar", [
                ("Horizontal.Scrollbar.trough", {
                    "children": [
                        ("Horizontal.Scrollbar.thumb", {
                            "expand": "1", "sticky": "nswe"
                        })
                    ],
                    "sticky": "ew"
                })
            ])
        except Exception:
            pass
        style.configure("Vertical.TScrollbar",
                        background="#C7C7CC",
                        troughcolor=self._ui["panel"],
                        borderwidth=0, relief="flat",
                        width=7)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#999999"),
                              ("disabled", self._ui["panel"])])
        style.configure("Horizontal.TScrollbar",
                        background="#C7C7CC",
                        troughcolor=self._ui["panel"],
                        borderwidth=0, relief="flat",
                        width=7)
        style.map("Horizontal.TScrollbar",
                  background=[("active", "#999999"),
                              ("disabled", self._ui["panel"])])
    def _build_ui(self):
        # v6.1.17:_build_ui 各區段 profile
        _t_bs = self._pmk_t()
        # ── CTkButton 样式字典 (Apple 色) ──
        _N = {"fg_color": "#E5E5EA", "text_color": self._ui["text"],
              "hover_color": "#D1D1D6", "corner_radius": 8}
        _A = {"fg_color": self._ui["accent"], "text_color": "#FFFFFF",
              "hover_color": "#0077ED", "corner_radius": 8}
        _D = {"fg_color": self._ui["danger"], "text_color": "#FFFFFF",
              "hover_color": "#FF6961", "corner_radius": 8}
        self._ctk_N, self._ctk_A, self._ctk_D = _N, _A, _D

        # ===== overall layout: content + bottom log =====
        vpan = ttk.PanedWindow(self, orient="vertical")
        vpan.pack(fill="both", expand=True)
        self._vpan = vpan

        content = ttk.Frame(vpan, style="App.TFrame")
        log_area = ttk.Frame(vpan, style="App.TFrame")
        vpan.add(content, weight=4)
        vpan.add(log_area, weight=1)

        self._pmk("_bui:overall_layout", _t_bs)
        try: self.update_idletasks()  # v6.1.17:首次 paint — 視窗框架 + 空殼出現
        except Exception: pass
        _t_bs = self._pmk_t()
        # ===== top: table (left) + controls (right) =====
        hpan = ttk.PanedWindow(content, orient="horizontal")
        hpan.pack(fill="both", expand=True, padx=14, pady=(14, 10))
        self._hpan = hpan
        # --- subtle shadow wrappers for a more "premium" look ---
        left_shadow = tk.Frame(hpan, bg=self._ui["shadow"])
        right_shadow = tk.Frame(hpan, bg=self._ui["shadow"])

        left = ttk.Frame(left_shadow, style="Card.TFrame")
        right = ttk.Frame(right_shadow, style="Card.TFrame")

        left.pack(fill="both", expand=True, padx=1, pady=1)
        right.pack(fill="both", expand=True, padx=1, pady=1)

        # right panel width tuned for tabs (bigger than the list)
        hpan.add(left_shadow, weight=2)
        hpan.add(right_shadow, weight=5)

        # ---- table (直接开始，不要标题) ----
        cols = ("sel", "name", "item_count", "status", "paid_to_ship", "cod", "im", "note")
        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings")
        headings = {
            "sel": "\u2610 \u5168\u9009",
            "name": "名字",
            "item_count": "商品数量",
            "status": "状态",
            "paid_to_ship": "待出货",
            "cod": "取货付款",
            "im": "即时通",
            "note": "备注",
        }
        widths = {
            "sel": 40,
            "name": 260,
            "item_count": 80,
            "status": 60,
            "paid_to_ship": 70,
            "cod": 80,
            "im": 60,
            "note": 200,
        }
        minwidths = {
            "sel": 36,
            "name": 140,
            "item_count": 70,
            "status": 50,
            "paid_to_ship": 60,
            "cod": 60,
            "im": 50,
            "note": 80,
        }
        for c in cols:
            kw = dict(text=headings[c])
            if c == "sel":
                kw["command"] = self._toggle_select_all
            self.tree.heading(c, **kw)
            anchor = "center"
            if c in ("name", "note"):
                anchor = "w"
            self.tree.column(c, width=widths.get(c, 80), minwidth=minwidths.get(c, 40),
                             anchor=anchor, stretch=(c != "sel"))

        # zebra rows — 白/淡紫交替
        self.tree.tag_configure("even", background=self._ui["panel"])
        self.tree.tag_configure("odd", background="#F9F9FB")

        # status colors (foreground only)
        self.tree.tag_configure("status_ok", foreground=self._ui["success"])
        self.tree.tag_configure("status_warn", foreground=self._ui["warn"])
        self.tree.tag_configure("status_error", foreground=self._ui["danger"])
        self.tree.tag_configure("status_offline", foreground=self._ui["muted"])
        # v6.1.45:異常(Yahoo server 端問題,非帳號離線)— 紫色,跟 warn(橙)/danger(紅)/muted(灰)/success(綠) 都不衝突
        self.tree.tag_configure("status_yahoo_warn", foreground="#AF52DE")

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-1>", self._on_tree_click, add=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_click, add=True)
        self.tree.bind("<Button-3>", self._on_tree_right_click)


        # ---- right controls: tabs, no scrolling ----
        nb = ctk.CTkTabview(right, corner_radius=10,
                            fg_color="#FFFFFF",
                            segmented_button_fg_color="#E5E5EA",
                            segmented_button_selected_color="#FFFFFF",
                            segmented_button_selected_hover_color="#F5F5F7",
                            segmented_button_unselected_color="#E5E5EA",
                            segmented_button_unselected_hover_color="#D1D1D6",
                            text_color=self._ui["text"],
                            text_color_disabled=self._ui["muted"])
        nb.pack(fill="both", expand=True)
        # macOS 风格 Tab 字体
        try:
            nb._segmented_button.configure(font=(self._base_family, 13))
        except Exception:
            pass

        _tab_names = ["操作", "批量", "采购出货", "自动刊登", "代付情况",
                      "物流系统", "业绩汇总", "检测", "编码数据更新"]
        for tn in _tab_names:
            nb.add(tn)

        tab_ops = nb.tab("操作")
        tab_batch = nb.tab("批量")
        tab_purchase_ship = nb.tab("采购出货")
        tab_publish = nb.tab("自动刊登")
        tab_pay = nb.tab("代付情况")
        tab_syb = nb.tab("物流系统")
        tab_perf = None  # 已移除「業績核對」tab,改成業績匯總自動核對 + 備註欄顯示狀態
        tab_perf_sum = nb.tab("业绩汇总")
        tab_check = nb.tab("检测")
        tab_doc = nb.tab("编码数据更新")

        # Expose notebook and tab frames (for ClawDBot control / UI snapshot)
        self.nb = nb
        self._tab_frames = {
            "操作": tab_ops,

            "批量": tab_batch,
            "采购出货": tab_purchase_ship,
            "自动刊登": tab_publish,
            "代付情况": tab_pay,
            "物流系统": tab_syb,
            "业绩汇总": tab_perf_sum,
            "检测": tab_check,
            "编码数据更新": tab_doc,
        }

        # 恢復上次打開的 Tab
        _last_tab = self.settings.get("last_tab", "")
        if _last_tab and _last_tab in _tab_names:
            try:
                nb.set(_last_tab)
            except Exception:
                pass

        # Tab 切換時自動保存
        def _on_tab_change():
            try:
                _cur = nb.get()
                if _cur and _cur != self.settings.get("last_tab", ""):
                    self.settings["last_tab"] = _cur
                    save_settings(self.settings)
            except Exception:
                pass
        nb.configure(command=_on_tab_change)

        self._pmk("_bui:tree+nb+tab_alloc", _t_bs)
        try: self.update_idletasks()  # v6.1.17:讓使用者看到表格+Tab 殼,而不是空白
        except Exception: pass
        _t_bs = self._pmk_t()
        # ===== 操作 tab =====
        _ops_font = (self._base_family, 12)
        lf_acc = ttk.Labelframe(tab_ops, text="账号管理", style="Ops.TLabelframe")
        lf_acc.grid(row=0, column=0, sticky="ew")
        lf_acc.columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(lf_acc, text="新增", font=_ops_font, command=self._add, **_N).grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(lf_acc, text="编辑", font=_ops_font, command=self._edit, **_N).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(lf_acc, text="删除", font=_ops_font, command=self._delete, **_D).grid(row=0, column=2, sticky="ew", padx=4, pady=4)

        # 监控/刷新（从监控tab合并过来）
        lf_mon = ttk.Labelframe(tab_ops, text="监控/刷新", style="Ops.TLabelframe")
        lf_mon.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        lf_mon.columnconfigure(1, weight=1)

        # v6.1.52:純 HTTP monitor 自動計算 interval(根據帳號數動態 scale),
        # 「默認刷新(秒)」跟「并发」這兩個欄位都失效了 — 隱藏掉但保留變數防 setattr 報錯
        # 之前的邏輯只剩 Chrome路徑(批量功能/出貨流程的瀏覽器路徑仍需要)
        self.var_refresh = tk.StringVar(value=str(self.settings.get("default_refresh", 300) or 300))

        ttk.Label(lf_mon, text="Chrome路径", style="Ops.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.var_browser = tk.StringVar(value=self.settings.get("browser_path", ""))
        ctk.CTkEntry(lf_mon, textvariable=self.var_browser, corner_radius=6,
                     border_color="#D2D2D7", font=_ops_font).grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=4)

        # v6.1.52:並發欄位也隱藏(純 HTTP monitor 不用)
        self.var_conc = tk.StringVar(value=str(self.settings.get("concurrency", 3)))

        # v6.0.83:監控走純 HTTP,headless 對監控無作用,checkbox 隱藏
        # var_headless 變數保留(出貨流程的 fallback 用,L3547/4047)
        self.var_headless = tk.BooleanVar(value=bool(self.settings.get("headless", True)))

        btn_row = ttk.Frame(lf_mon)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=4, pady=(4, 6))
        btn_row.columnconfigure((0, 1), weight=1)
        # v6.0.69:存 instance attr,_start/_stop_monitor 動態改文字+狀態,讓使用者看得到回饋
        self.btn_start_mon = ctk.CTkButton(btn_row, text="开始监控", font=_ops_font, command=self._start_monitor, **_A)
        self.btn_start_mon.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_stop_mon = ctk.CTkButton(btn_row, text="停止", font=_ops_font, command=self._stop_monitor, **_D)
        self.btn_stop_mon.grid(row=0, column=1, sticky="ew")

        # KV 中转状态显示
        lf_kv = ttk.Labelframe(tab_ops, text="TG 中转状态", style="Ops.TLabelframe")
        lf_kv.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        lf_kv.columnconfigure(1, weight=1)
        _kv_uid = _RELAY_CONFIG.get("user_id", "")
        ttk.Label(lf_kv, text="绑定TG ID:", style="Ops.TLabel").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.var_tg_relay_uid = tk.StringVar(value=_kv_uid)
        ctk.CTkEntry(lf_kv, textvariable=self.var_tg_relay_uid, width=200, corner_radius=6,
                     border_color="#D2D2D7", font=_ops_font).grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ctk.CTkButton(lf_kv, text="保存", font=_ops_font, command=self._save_tg_relay_uid, **_N).grid(row=0, column=2, padx=4, pady=2)

        # v6.1:Forum group 設定(填連結/ID,自動解析+寫 settings)
        ttk.Label(lf_kv, text="Forum 群組:", style="Ops.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=(8, 2))
        self.var_tg_forum_chat = tk.StringVar(value=str(self.settings.get("tg_forum_chat_id", "")))
        ctk.CTkEntry(
            lf_kv, textvariable=self.var_tg_forum_chat, width=200, corner_radius=6,
            border_color="#D2D2D7", font=_ops_font,
            placeholder_text="貼 group 連結或 chat_id"
        ).grid(row=1, column=1, sticky="w", padx=4, pady=(8, 2))
        ctk.CTkButton(
            lf_kv, text="啟用 Forum", font=_ops_font,
            command=self._save_tg_forum_chat, **_N,
        ).grid(row=1, column=2, padx=4, pady=(8, 2))

        ttk.Label(
            lf_kv,
            text=(
                "↑ 步骤(必看):\n"
                "  ① 打开 TG → 新建一个群组(自己建,不是用别人的)\n"
                "  ② 进群组「设置」→ 打开「话题」开关(Topics / 主题)\n"
                "  ③ 把机器人 @example_forum_bot(显示名「即時通客服」)拉进群组 → 设为「管理员」\n"
                "     → 权限勾上「管理话题」和「发送消息」 ※ 只拉这一个就够,不需要其他 bot\n"
                "  ④ 群组里随便一条消息 → 右键(或长按)→ 复制链接 → 粘到上面那一栏 → 点「啟用 Forum」"
            ),
            style="Ops.TLabel",
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 2))

        tab_ops.columnconfigure(0, weight=1)


        self._pmk("_bui:tab_ops", _t_bs)
        try: self.update_idletasks()  # v6.1.17:操作 tab 出現
        except Exception: pass
        _t_bs = self._pmk_t()
        # ===== 批量 tab =====
        lf_batch = ttk.Labelframe(tab_batch, text="商品批量上下架", style="Ops.TLabelframe")
        lf_batch.grid(row=0, column=0, sticky="nsew", pady=(6, 0))
        tab_batch.rowconfigure(0, weight=1)
        tab_batch.columnconfigure(0, weight=1)

        lf_batch.columnconfigure(1, weight=1)

        ttk.Label(lf_batch, text="模式", style="Ops.TLabel").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.var_merch_mode = tk.StringVar(value=str(self.settings.get("merch_mode", "下架")))
        ttk.Combobox(
            lf_batch,
            textvariable=self.var_merch_mode,
            values=("下架", "上架", "刪除", "根據商品編號下架刪除", "複製上新"),
            width=18,
            state="readonly",
            font=(self._base_family, 10),
        ).grid(row=0, column=1, columnspan=2, sticky="w", padx=4, pady=3)

        ttk.Label(lf_batch, text="执行次数", style="Ops.TLabel").grid(row=1, column=0, sticky="w", padx=4, pady=6)
        self.var_merch_repeat = tk.StringVar(value=str(self.settings.get("merch_repeat", 2)))
        ctk.CTkEntry(lf_batch, textvariable=self.var_merch_repeat, width=100, corner_radius=6,
                     border_color="#D2D2D7", font=(self._base_family, 12)).grid(row=1, column=1, sticky="w", padx=4, pady=6)

        ttk.Label(lf_batch, text="间隔秒数", style="Ops.TLabel").grid(row=2, column=0, sticky="w", padx=4, pady=6)
        self.var_merch_interval = tk.StringVar(value=str(self.settings.get("merch_interval", 20)))
        ctk.CTkEntry(lf_batch, textvariable=self.var_merch_interval, width=100, corner_radius=6,
                     border_color="#D2D2D7", font=(self._base_family, 12)).grid(row=2, column=1, sticky="w", padx=4, pady=6)

        ttk.Label(lf_batch, text="并发账号", style="Ops.TLabel").grid(row=3, column=0, sticky="w", padx=4, pady=6)
        self.var_merch_conc = tk.StringVar(value=str(self.settings.get("merch_concurrency", 1)))
        ctk.CTkEntry(lf_batch, textvariable=self.var_merch_conc, width=100, corner_radius=6,
                     border_color="#D2D2D7", font=(self._base_family, 12)).grid(row=3, column=1, sticky="w", padx=4, pady=6)

        # ── 按上架日期过滤区域（仅下架模式） ──
        lf_date = ttk.Labelframe(lf_batch, text="按上架日期筛选（仅下架模式）")
        lf_date.grid(row=5, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 2))
        lf_date.columnconfigure(1, weight=1)

        self.var_date_filter_enabled = tk.BooleanVar(value=False)
        self._date_chk = ttk.Checkbutton(lf_date, text="启用日期过滤", variable=self.var_date_filter_enabled)
        self._date_chk.grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=2)

        # 快捷按钮
        self._date_quick_frame = ttk.Frame(lf_date)
        self._date_quick_frame.grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        self.var_date_cutoff = tk.StringVar(value="")
        self._date_quick_btns = []
        for i, (label, months) in enumerate([("保留1个月", 1), ("保留2个月", 2), ("保留3个月", 3)]):
            btn = ttk.Button(self._date_quick_frame, text=label,
                             command=lambda m=months: self._set_date_cutoff_months(m))
            btn.pack(side="left", padx=2)
            self._date_quick_btns.append(btn)

        ttk.Label(lf_date, text="截止日期").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._date_entry = ctk.CTkEntry(lf_date, textvariable=self.var_date_cutoff, width=120, corner_radius=6,
                     placeholder_text="YYYY/MM/DD",
                     border_color="#D2D2D7", font=(self._base_family, 12))
        self._date_entry.grid(row=2, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(lf_date, text="之前上架的在售商品将被下架", style="Ops.TLabel").grid(row=2, column=2, sticky="w", padx=4, pady=2)

        # 模式切换时禁用/启用日期过滤控件
        def _on_mode_change(*_):
            m = self.var_merch_mode.get()
            is_unshelve = (m == "下架")
            state = "normal" if is_unshelve else "disabled"
            self._date_chk.configure(state=state)
            for b in self._date_quick_btns:
                b.configure(state=state)
            self._date_entry.configure(state=state)
            if not is_unshelve:
                self.var_date_filter_enabled.set(False)
        self.var_merch_mode.trace_add("write", _on_mode_change)
        _on_mode_change()  # 初始化状态

        btn_row2 = ttk.Frame(lf_batch)
        btn_row2.grid(row=6, column=0, columnspan=3, sticky="ew", padx=4, pady=(10, 6))
        btn_row2.columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btn_row2, text="开始批量", font=_ops_font, command=self._start_merch_batch, **_A).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(btn_row2, text="停止批量", font=_ops_font, command=self._stop_merch_batch, **_D).grid(row=0, column=1, sticky="ew")

        self._pmk("_bui:tab_batch", _t_bs)
        try: self.update_idletasks()  # v6.1.17:批量 tab 出現
        except Exception: pass
        _t_bs = self._pmk_t()
        # ===== bottom log =====
        sec_log = ttk.Labelframe(log_area, text="日志")
        sec_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # v6.1.17:每個 feature tab build 計時併進 self._prof_init
        def _prof(name, fn):
            _t0 = self._pmk_t()
            try:
                fn()
            except Exception as e:
                self.log(f"[{name}] 初始化失败：{e}")
            self._pmk(f"tab:{name}", _t0)

        # ===== 采购出货 tab (合并版) =====
        def _bld_purch():
            self.purchase_ship_tab = PurchaseShipTab(app=self, frame=tab_purchase_ship)
            self.purchase_ship_tab.build()
        _prof("采购出货", _bld_purch)

        # ===== 自动刊登 tab =====
        def _bld_pub():
            self.publish_tab = AutoPublishFeatureTab(app=self, frame=tab_publish)
            self.publish_tab.build()
        _prof("自动刊登", _bld_pub)

        # ===== 代付情况 tab =====
        def _bld_pay():
            self.pay_status_tab = PayStatusFeatureTab(app=self, frame=tab_pay)
            self.pay_status_tab.build()
        _prof("代付情况", _bld_pay)

        # ===== 物流系统上传 tab (子页签) =====
        logistics_nb = ttk.Notebook(tab_syb)
        logistics_nb.pack(fill="both", expand=True)
        sub_syb = ttk.Frame(logistics_nb)
        sub_suda = ttk.Frame(logistics_nb)
        logistics_nb.add(sub_syb, text="中台線路")
        logistics_nb.add(sub_suda, text="日台線路")

        def _bld_syb():
            self.syb_upload_tab = ShunyunbaoUploadFeatureTab(app=self, frame=sub_syb)
            self.syb_upload_tab.build()
        _prof("物流-中台", _bld_syb)
        def _bld_suda():
            self.suda68_tab = Suda68Tab(app=self, frame=sub_suda)
        _prof("物流-日台", _bld_suda)
        def _bld_alw():
            from core.auto_label_feature import AutoLabelWorker
            self.auto_label_worker = AutoLabelWorker(app=self, log_fn=self.log)
        _prof("AutoLabel", _bld_alw)

        # ===== 业绩汇总 tab =====
        def _bld_perf():
            from core.performance_feature import PerformanceFeatureTab
            self.perf_sum_tab = PerformanceFeatureTab(app=self, frame=tab_perf_sum)
            self.perf_sum_tab.build()
        _prof("业绩汇总", _bld_perf)

        # ===== 检测 tab =====
        def _bld_chk():
            self.unified_check_tab = UnifiedCheckFeatureTab(app=self, frame=tab_check)
            self.unified_check_tab.build()
        _prof("检测", _bld_chk)

        # ===== 编码数据更新 tab =====
        def _bld_doc():
            self.doc_upload_tab = DocUploadFeatureTab(app=self, frame=tab_doc)
            self.doc_upload_tab.build()
        _prof("编码更新", _bld_doc)

        self._pmk("_bui:feature_tabs_TOTAL", _t_bs)
        try: self.update_idletasks()  # v6.1.17:9 個 feature tab 出現
        except Exception: pass
        _t_bs = self._pmk_t()

        # 日志工具栏（自动滚动 / 暂停显示）
        log_toolbar = ttk.Frame(sec_log)
        log_toolbar.pack(fill="x", padx=6, pady=(6, 0))

        ctk.CTkCheckBox(
            log_toolbar, text="自动滚动", variable=self.var_log_autoscroll, command=self._on_log_autoscroll_changed,
            fg_color="#007AFF", hover_color="#0077ED", border_color="#C7C7CC", corner_radius=4,
            checkbox_width=16, checkbox_height=16,
            font=(self._base_family, 12)
        ).pack(side="left")

        ctk.CTkCheckBox(
            log_toolbar, text="暂停显示", variable=self.var_log_paused, command=self._toggle_log_pause,
            fg_color="#007AFF", hover_color="#0077ED", border_color="#C7C7CC", corner_radius=4,
            checkbox_width=16, checkbox_height=16,
            font=(self._base_family, 12)
        ).pack(side="left", padx=(12, 0))

        # v6.1.15:詳細日誌按鈕 — 主 log 只顯示重要事件,點此看全部
        ctk.CTkButton(
            log_toolbar, text="📋 详细日志", width=90, height=24,
            command=self._show_full_log_window,
            fg_color="#E5E5EA", text_color=self._ui["text"],
            hover_color="#D1D1D6", corner_radius=6,
            font=(self._base_family, 11)
        ).pack(side="left", padx=(12, 0))

        self._update_label = tk.Label(
            log_toolbar, text="", fg="#e53935", bg=self._ui["panel"],
            font=(self._base_family, 10),
        )
        self._update_label.pack(side="right", padx=(0, 8))

        log_wrap = ttk.Frame(sec_log)
        log_wrap.pack(fill="both", expand=True)

        self.txt_log = tk.Text(
            log_wrap,
            height=8,
            wrap="word",
            state="disabled",
            font=(self._base_family, 10),
            bg="#FFFFFF",
            fg="#000000",
            insertbackground="#000000",
            selectbackground=self._ui["accent"],
            selectforeground="#ffffff",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#D2D2D7",
            highlightcolor=self._ui["accent"],
        )
        vsb_log = ttk.Scrollbar(log_wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=vsb_log.set)

        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        vsb_log.grid(row=0, column=1, sticky="ns", pady=6)
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)

        # initial sash positions (restore saved, or default 45%/78%)
        # clamp:saved 值需在 [50, 邊界-餘量] 內,否則 fallback 默認比例
        # 防止舊 sash 值大於當前窗口寬/高時把右側 Tab / 底部日誌擠成 0 px
        def _init_sash():
            try:
                self.update_idletasks()
                w = max(980, self.winfo_width())
                h = max(640, self.winfo_height())
                saved_h = self.settings.get("sash_h", 0)
                saved_v = self.settings.get("sash_v", 0)
                if not (50 < saved_h < w - 200):
                    saved_h = int(w * 0.45)
                if not (50 < saved_v < h - 100):
                    saved_v = int(h * 0.78)
                hpan.sashpos(0, saved_h)
                vpan.sashpos(0, saved_v)
            except Exception:
                pass

        # ===== ClawDBot control (file-queue) =====
        # ClawDBot can control this GUI by writing JSON commands into BASE_DIR/claw_cmd/inbox
        # v6.1.17:ClawControlManager 構造 + start 全部延後到 after(500),不阻 __init__
        # 外部沒人在 __init__ 內存取 self.claw_control,延後安全
        self.claw_control = None
        def _init_claw():
            try:
                self.claw_control = ClawControlManager(app=self, base_dir=BASE_DIR)
                self.claw_control.start()
            except Exception as e:
                try:
                    self.log(f"[CLAW] 初始化失败：{e}")
                except Exception:
                    pass
        self.after(500, _init_claw)

        self.after(80, _init_sash)
        # 再校正一次:等 zoom/maximize 完成後窗口尺寸變了,重新校 sash
        self.after(600, _init_sash)
        self._pmk("_bui:log_toolbar+sash+claw", _t_bs)

    def _refresh_table(self):
        # Update rows in-place to avoid scroll/selection jumping
        existing = set(self.tree.get_children(""))

        desired_order: List[str] = []
        for a in self.accounts:
            pid = a.get("profile_id")
            if pid and pid in self.states:
                desired_order.append(pid)
        for pid in self.states.keys():
            if pid not in desired_order:
                desired_order.append(pid)

        desired_set = set(desired_order)

        self._pid_index = {pid: i for i, pid in enumerate(desired_order)}
        self._row_cache.clear()

        # delete removed
        for pid in list(existing - desired_set):
            try:
                self.tree.delete(pid)
            except Exception:
                pass

        # insert/update
        for idx, pid in enumerate(desired_order):
            s = self.states.get(pid)
            if not s:
                continue
            sel = "☑" if s.selected else "☐"
            vals = (
                sel,
                s.name,
                s.profile_id,
                s.status,
                s.last_values.get("paid_to_ship", 0),
                s.last_values.get("cod", 0),
                s.last_values.get("im", 0),
                (s.note or ""),
            )
            tag_bg = "odd" if (idx % 2 == 1) else "even"
            # Status color tag (UI only)
            status_text = (s.status or "").strip()
            lv = s.last_values or {}
            def _to_int(x):
                try:
                    return int(x or 0)
                except Exception:
                    return 0
            paid_to_ship = _to_int(lv.get("paid_to_ship", 0))
            cod = _to_int(lv.get("cod", 0))
            im = _to_int(lv.get("im", 0))

            st_lower = status_text.lower()
            # v6.1.45:status 完全等於「異常」/「异常」= Yahoo server 端問題 → 紫色
            # 跟「錯誤」「停權」(紅色 status_error)分開,讓 user 看到「不是我帳號的事」
            _st_strip = status_text.strip()
            if _st_strip == "異常" or _st_strip == "异常":
                tag_status = "status_yahoo_warn"
            elif ("离线" in status_text) or ("offline" in st_lower):
                tag_status = "status_offline"
            elif ("错误" in status_text) or ("异常" in status_text) or ("fail" in st_lower) or ("error" in st_lower) or ("停權" in status_text) or ("停权" in status_text):
                tag_status = "status_error"
            elif (paid_to_ship > 0) or (cod > 0) or (im > 0):
                tag_status = "status_warn"
            else:
                tag_status = "status_ok"

            tags = (tag_bg, tag_status)
            if pid in existing:
                self.tree.item(pid, values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=pid, values=vals, tags=tags)

        # keep current selection if possible
        sel = self.tree.selection()
        if sel:
            pid = sel[0]
            if pid in desired_set:
                try:
                    self.tree.see(pid)
                except Exception:
                    pass

        self._sync_sel_heading()

    def _schedule_row_update(self, pid: str):
        """合并多次更新：在短时间窗口内只刷新需要的行。"""
        if not pid:
            return
        if not hasattr(self, "_pending_row_updates"):
            self._pending_row_updates = set()
        self._pending_row_updates.add(pid)
        if getattr(self, "_row_flush_after", None) is None:
            self._row_flush_after = self.after(80, self._flush_row_updates)

    def _flush_pending_patches(self):
        """v6.2:批次處理 monitor patch — 一次 flush 完所有 pending pid,避免大量 after(0) 堵塞主線程。"""
        self._patch_flush_after = None
        with self._pending_patches_lock:
            if not self._pending_patches:
                return
            pending_snapshot = self._pending_patches
            self._pending_patches = {}
        try:
            for pid, patch in pending_snapshot.items():
                s = self.states.get(pid)
                if not s:
                    continue
                try:
                    for k, v in patch.items():
                        if k in ("paid_to_ship", "cod", "im", "item_count"):
                            s.last_values[k] = int(v)
                        elif k == "status":
                            s.status = str(v)
                        elif k == "last_change_ts":
                            s.last_change_ts = float(v)
                        elif k == "last_error":
                            s.last_error = str(v)
                    self._schedule_row_update(pid)
                except Exception:
                    pass
        except Exception:
            pass

    def _flush_row_updates(self):
        self._row_flush_after = None
        if not hasattr(self, "tree") or self.tree is None:
            return

        # 用户刚点了列表：先让 UI 处理选中，再刷新数据（体感更顺）
        try:
            if (time.time() - float(getattr(self, "_last_tree_click_ts", 0.0))) < 0.12:
                self._row_flush_after = self.after(80, self._flush_row_updates)
                return
        except Exception:
            pass

        pending = getattr(self, "_pending_row_updates", None)
        if not pending:
            return

        do_full = False
        for pid in list(pending):
            pending.discard(pid)
            try:
                if not self.tree.exists(pid):
                    do_full = True
                    continue
            except Exception:
                do_full = True
                continue

            self._update_row(pid, self._pid_index.get(pid))

        if do_full:
            self._refresh_table()

        if pending:
            self._row_flush_after = self.after(80, self._flush_row_updates)

    def _update_row(self, pid: str, idx: Optional[int] = None):
        """只更新 Treeview 的一行，避免每次都全表重绘。"""
        s = self.states.get(pid)
        if not s:
            return
        sel = "☑" if s.selected else "☐"
        vals = (
            sel,
            s.name,
            s.last_values.get("item_count", 0),
            s.status,
            s.last_values.get("paid_to_ship", 0),
            s.last_values.get("cod", 0),
            s.last_values.get("im", 0),
            (s.note or ""),
        )

        if idx is None:
            idx = self._pid_index.get(pid)
        if idx is None:
            idx = 0

        tag_bg = "odd" if (idx % 2 == 1) else "even"

        status_text = (s.status or "").strip()
        lv = s.last_values or {}

        def _to_int(x):
            try:
                return int(x or 0)
            except Exception:
                return 0

        paid_to_ship = _to_int(lv.get("paid_to_ship", 0))
        cod = _to_int(lv.get("cod", 0))
        im = _to_int(lv.get("im", 0))

        st_lower = status_text.lower()
        # v6.1.45:單純「異常」/「异常」(Yahoo 端問題)優先匹配,給紫色
        _st_strip = status_text.strip()
        if _st_strip == "異常" or _st_strip == "异常":
            tag_status = "status_yahoo_warn"
        elif ("离线" in status_text) or ("offline" in st_lower):
            tag_status = "status_offline"
        elif ("错误" in status_text) or ("异常" in status_text) or ("fail" in st_lower) or ("error" in st_lower) or ("停權" in status_text) or ("停权" in status_text):
            tag_status = "status_error"
        elif (paid_to_ship > 0) or (cod > 0) or (im > 0):
            tag_status = "status_warn"
        else:
            tag_status = "status_ok"

        new_tags = (tag_bg, tag_status)
        old = self._row_cache.get(pid)
        if old and old[0] == vals and old[1] == new_tags:
            return
        try:
            self.tree.item(pid, values=vals, tags=new_tags)
            self._row_cache[pid] = (vals, new_tags)
        except Exception:
            # 行不存在就交给全量刷新处理
            pass



    def _on_tree_click(self, _evt=None):
        self._last_tree_click_ts = time.time()

    def _selected_pid(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _toggle_selected(self, _evt=None):
        pid = self._selected_pid()
        if not pid: return
        s = self.states.get(pid)
        if not s: return
        s.selected = not s.selected
        self._schedule_row_update(pid)
        self._sync_sel_heading()


    def _on_tree_double_click(self, evt):
        """双击：默认切换勾选；若双击的是『备注』单元格，则就地编辑并写入。"""
        try:
            row_id = self.tree.identify_row(evt.y)
            col_id = self.tree.identify_column(evt.x)  # like '#1'
        except Exception:
            return

        if not row_id:
            return

        # Ensure this row becomes current selection
        try:
            self.tree.selection_set(row_id)
        except Exception:
            pass

        # Map column id ('#n') to column name
        col_name = None
        try:
            if col_id and col_id.startswith('#'):
                idx = int(col_id[1:]) - 1
                cols = list(self.tree['columns'])
                if 0 <= idx < len(cols):
                    col_name = cols[idx]
        except Exception:
            col_name = None

        # Double-click on note cell => inline edit (do NOT toggle checkbox)
        if col_name == 'note':
            self._start_edit_note_cell(row_id, col_id)
            return 'break'

        # Otherwise: keep original behavior (toggle selected)
        self._toggle_selected()


    def _cancel_note_cell_edit(self):
        ent = getattr(self, '_note_cell_editor', None)
        if ent is not None:
            try:
                ent.destroy()
            except Exception:
                pass
        self._note_cell_editor = None
        self._note_cell_edit_pid = None


    def _start_edit_note_cell(self, pid: str, col_id: str):
        # Only allow one editor at a time
        self._cancel_note_cell_edit()

        try:
            self.tree.see(pid)
            self.update_idletasks()
        except Exception:
            pass

        try:
            bbox = self.tree.bbox(pid, col_id)
        except Exception:
            bbox = None

        if not bbox:
            return
        x, y, w, h = bbox

        try:
            cur = self.tree.set(pid, 'note')
        except Exception:
            cur = ''

        ent = tk.Entry(self.tree)
        ent.insert(0, cur)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        try:
            ent.icursor('end')
        except Exception:
            pass

        self._note_cell_editor = ent
        self._note_cell_edit_pid = pid

        def _commit(_evt=None):
            self._commit_note_cell_edit(pid, ent.get())
            return 'break'

        def _cancel(_evt=None):
            self._cancel_note_cell_edit()
            return 'break'

        ent.bind('<Return>', _commit)
        ent.bind('<Escape>', _cancel)
        ent.bind('<FocusOut>', lambda _e: self._commit_note_cell_edit(pid, ent.get()))


    def _commit_note_cell_edit(self, pid: str, new_text: str):
        ent = getattr(self, '_note_cell_editor', None)
        if ent is None:
            return

        # Avoid duplicate commits
        self._note_cell_editor = None
        self._note_cell_edit_pid = None

        try:
            ent.destroy()
        except Exception:
            pass

        # Update state + accounts.json
        s = self.states.get(pid)
        old_note = s.note if s else ''

        # Keep exactly what user typed (no forced strip)
        note = '' if new_text is None else str(new_text)

        # Apply in-memory
        if s is not None:
            s.note = note

        old_acc_note = None
        found = False
        for a in self.accounts:
            if a.get('profile_id') == pid:
                old_acc_note = a.get('note', '')
                a['note'] = note
                found = True
                break

        try:
            save_accounts(self.accounts)
        except Exception as e:
            # Rollback if save failed
            if s is not None:
                s.note = old_note
            if found:
                for a in self.accounts:
                    if a.get('profile_id') == pid:
                        a['note'] = old_acc_note
                        break
            messagebox.showerror('保存失败', str(e))
            return

        # Refresh this row only
        try:
            self._schedule_row_update(pid)
        except Exception:
            self._refresh_table()

    def _sync_sel_heading(self):
        all_sel = all(getattr(s, "selected", False) for s in self.states.values())
        self.tree.heading("sel", text="\u2611 \u5168\u9009" if all_sel else "\u2610 \u5168\u9009")

    def _toggle_select_all(self):
        all_selected = all(getattr(s, "selected", False) for s in self.states.values())
        for s in self.states.values():
            s.selected = not all_selected
        self._sync_sel_heading()
        self._refresh_table()

    def _on_tree_right_click(self, evt):
        row_id = self.tree.identify_row(evt.y)
        if not row_id:
            return
        # v6.1.38:支援多選右擊打開
        # 如果 row_id 在當前多選中 → 保留多選(不清掉)
        # 否則 → 選中該 row(原行為)
        cur_sel = self.tree.selection()
        if row_id not in cur_sel:
            self.tree.selection_set(row_id)
        sel = self.tree.selection()
        n_sel = len(sel)
        st = self.states.get(row_id)
        name = st.name if st else row_id

        # 关闭之前的弹窗
        w = getattr(self, "_ctx_popup", None)
        if w:
            try:
                w.destroy()
            except Exception:
                pass
            self._ctx_popup = None

        # 多選時 menu 顯示「打開 N 個帳號」,單選顯示「打開 {name}」
        if n_sel > 1:
            _open_label = f"打开 {n_sel} 个账号"
            _clear_label = f"释放 {n_sel} 个账号"
            _reset_label = f"🔄 重整 {n_sel} 个账号 (清 stuck 状态)"
        else:
            _open_label = f"打开 {name}"
            _clear_label = f"释放 {name}"
            _reset_label = f"🔄 重整 {name} (清 stuck 状态)"
        items = [
            (_open_label, self._open_login_multi),
            (_clear_label, self._clear_lock_multi),
            # v6.1.58:重整帳號 — 清 monitor 內 fail counter,模擬「單一帳號重啟」
            # 用於 stuck case(連續 5xx 維護頁,其他帳號正常)
            (_reset_label, self._reset_account_multi),
        ]

        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#C7C7CC")
        self._ctx_popup = popup

        # macOS Sonoma context menu
        menu_bg = "#ECECEC"
        outer = tk.Frame(popup, bg="#C7C7CC")
        outer.pack(fill="both", expand=True, padx=0, pady=0)
        inner = tk.Frame(outer, bg=menu_bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # Top padding
        tk.Frame(inner, bg=menu_bg, height=2).pack(fill="x")

        fam = getattr(self, "_base_family", "Noto Sans TC")
        font_menu = (fam, 10)
        clr_fg = "#000000"
        clr_hover_bg = "#007AFF"
        item_bg = menu_bg

        for i, (text, cmd) in enumerate(items):
            item_frame = tk.Frame(inner, bg=menu_bg)
            item_frame.pack(fill="x", padx=4, pady=0)

            lbl = tk.Label(item_frame, text=" " + text, font=font_menu,
                           fg=clr_fg, bg=item_bg,
                           anchor="w", cursor="hand2",
                           padx=8, pady=2)
            lbl.pack(fill="x")

            def _enter(e, w=lbl, f=item_frame):
                w.configure(bg=clr_hover_bg, fg="#FFFFFF")
                f.configure(bg=clr_hover_bg)
            def _leave(e, w=lbl, f=item_frame):
                w.configure(bg=item_bg, fg=clr_fg)
                f.configure(bg=menu_bg)
            def _click(e, c=cmd):
                _dismiss()
                c()

            lbl.bind("<Enter>", _enter)
            lbl.bind("<Leave>", _leave)
            lbl.bind("<Button-1>", _click)

            if i < len(items) - 1:
                sep = tk.Frame(inner, bg="#D2D2D7", height=1)
                sep.pack(fill="x", padx=8, pady=1)

        # Bottom padding
        tk.Frame(inner, bg=menu_bg, height=2).pack(fill="x")

        # Position and show with minimum width
        popup.update_idletasks()
        pw = popup.winfo_reqwidth()
        if pw < 200:
            inner.configure(width=200)
            popup.update_idletasks()
        popup.geometry(f"+{evt.x_root}+{evt.y_root}")
        popup.deiconify()
        popup.focus_set()

        def _dismiss(e=None):
            try:
                popup.destroy()
            except Exception:
                pass
            self._ctx_popup = None

        popup.bind("<FocusOut>", _dismiss)

        def _root_click(e):
            _dismiss()
            try:
                self.unbind("<Button-1>", _root_bind_id)
            except Exception:
                pass
        _root_bind_id = self.bind("<Button-1>", _root_click, add="+")

    def _add(self):
        dlg = AccountDialog(self, existing_pids=[a.get('profile_id','') for a in self.accounts])
        self.wait_window(dlg)
        if not dlg.result:
            return
        a = dlg.result
        # update memory
        self.accounts.append(a)
        try:
            save_accounts(self.accounts)
        except Exception as e:
            # 回滚
            self.accounts.pop()
            messagebox.showerror("保存失败", str(e))
            return
        s = AccountState(
            name=a["name"], profile_id=a["profile_id"], start_url=a["start_url"],
            refresh_sec=int(a["refresh_sec"]), proxy=a.get("proxy",""), note=a.get("note",""), selected=True
        )
        self.states[s.profile_id]=s
        self.log(f"[ADD] {s.name} ({s.profile_id})")
        self._refresh_table()

    def _edit(self):
        pid = self._selected_pid()
        if not pid: return
        s = self.states.get(pid)
        if not s: return
        init = {
            "name": s.name, "profile_id": s.profile_id, "start_url": s.start_url,
            "refresh_sec": s.refresh_sec, "proxy": s.proxy, "note": s.note
        }
        dlg = AccountDialog(self, init=init, existing_pids=[a.get('profile_id','') for a in self.accounts], editing_pid=pid)
        self.wait_window(dlg)
        if not dlg.result:
            return
        a = dlg.result

        # replace in accounts list
        new_accounts = []
        for old in self.accounts:
            if old.get("profile_id")==pid:
                new_accounts.append(a)
            else:
                new_accounts.append(old)
        self.accounts = new_accounts
        try:
            save_accounts(self.accounts)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return

        # update state
        # profile_id may change: handle rename (尽量安全地重命名 profiles 目录，避免丢失已登录 Cookie)
        new_pid = a["profile_id"]
        if new_pid != pid:
            old_dir = BASE_DIR / "profiles" / pid
            new_dir = BASE_DIR / "profiles" / new_pid
            try:
                if old_dir.exists() and (not new_dir.exists()):
                    old_dir.rename(new_dir)
                    self.log(f"[PROFILE] renamed: {pid} -> {new_pid}")
            except Exception as e:
                self.log(f"[PROFILE] rename failed ({pid} -> {new_pid}): {e}")
        if new_pid != pid:
            del self.states[pid]
        s2 = self.states.get(new_pid) or AccountState(name=a["name"], profile_id=new_pid, start_url=a["start_url"])
        s2.name = a["name"]
        s2.profile_id = new_pid
        s2.start_url = a["start_url"]
        s2.refresh_sec = int(a["refresh_sec"])
        s2.proxy = a.get("proxy","")
        s2.note = a.get("note","")
        self.states[new_pid]=s2

        self.log(f"[EDIT] {a['name']} ({new_pid})")
        self._refresh_table()

    def _delete(self):
        pid = self._selected_pid()
        if not pid: return
        s = self.states.get(pid)
        if not s: return
        if not messagebox.askyesno("确认", f"删除账号 {s.name} ?（不会删除 profiles/{pid}，只删配置）"):
            return
        self.accounts = [a for a in self.accounts if a.get("profile_id") != pid]
        save_accounts(self.accounts)
        del self.states[pid]
        self.log(f"[DEL] {s.name} ({pid})")
        self._refresh_table()

    def _apply_refresh(self):
        try:
            sec = int(self.var_refresh.get().strip())
            sec = max(15, sec)
        except Exception:
            messagebox.showerror("错误", "刷新秒数必须是数字")
            return
        for a in self.accounts:
            pid = a.get("profile_id")
            if pid in self.states and self.states[pid].selected:
                a["refresh_sec"] = sec
                self.states[pid].refresh_sec = sec
        save_accounts(self.accounts)
        self.settings["default_refresh"]=sec
        save_settings(self.settings)
        self.log(f"[CFG] set refresh_sec={sec} for selected")
        self._refresh_table()

    def _save_tg_relay_uid(self):
        uid = self.var_tg_relay_uid.get().strip()
        if not uid:
            messagebox.showwarning("提示", "请输入 TG User ID")
            return
        if not uid.isdigit():
            messagebox.showwarning("提示", "TG User ID 必须是纯数字")
            return
        self.settings["tg_chat_id"] = uid
        try:
            from core.accounts import save_settings
            save_settings(self.settings)
            messagebox.showinfo("成功", f"TG ID 已保存为 {uid}\n重启软件后生效")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _save_tg_forum_chat(self):
        """v6.1:解析 Forum group 連結/ID,寫 settings.tg_forum_chat_id + 啟用 forum。

        接受格式:
        - 連結: https://t.me/c/3909189274/...
        - chat_id: -1001234567890
        - 純內部 ID: 3909189274
        """
        import re as _re
        raw = self.var_tg_forum_chat.get().strip()
        if not raw:
            messagebox.showwarning("提示", "請貼上 Forum group 連結或 chat_id")
            return
        # 解析
        chat_id = ""
        m = _re.search(r"t\.me/c/(\d+)", raw)
        if m:
            chat_id = "-100" + m.group(1)
        elif raw.startswith("-100") and raw[1:].isdigit():
            chat_id = raw
        elif raw.isdigit():
            chat_id = "-100" + raw
        else:
            messagebox.showwarning(
                "解析失敗",
                "無法解析。請貼:\n"
                "  - 群組連結:https://t.me/c/XXXX/...\n"
                "  - 或 chat_id:-100XXXX",
            )
            return

        # 寫 settings
        self.settings["tg_forum_chat_id"] = chat_id
        self.settings["tg_forum_enabled"] = True
        self.settings["forum_bot_use_kv"] = True

        # 順便保 tg_tokens.json forum_bot_token(若缺)
        from pathlib import Path
        import json
        tokens_fp = Path(__file__).resolve().parent / "tg_tokens.json"
        try:
            tokens = json.loads(tokens_fp.read_text(encoding="utf-8")) if tokens_fp.exists() else {}
            if not tokens.get("forum_bot_token"):
                tokens["forum_bot_token"] = "<TG_BOT_TOKEN_REDACTED>"
                tmp = tokens_fp.with_suffix(".tmp")
                tmp.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
                import os
                os.replace(str(tmp), str(tokens_fp))
        except Exception:
            pass

        try:
            from core.accounts import save_settings
            save_settings(self.settings)
            self.var_tg_forum_chat.set(chat_id)  # 更新顯示為解析後的完整 ID
            messagebox.showinfo(
                "成功",
                f"✅ Forum 啟用\n\n"
                f"group chat_id: {chat_id}\n"
                f"forum_bot_use_kv: True\n"
                f"forum_bot_token: 已自動填\n\n"
                f"重啟軟件後,該 group 會自動收到 buyer 訊息。"
            )
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _clear_lock(self):
        pid = self._selected_pid()
        if not pid:
            messagebox.showinfo("提示", "先在左侧点选一个账号")
            return
        s = self.states.get(pid)
        if not s:
            return
        profile_dir = BASE_DIR / "profiles" / s.profile_id
        ok, msg = force_clear(profile_dir)
        if ok:
            self.log(f"[LOCK] {s.name} -> {msg}")
            messagebox.showinfo("结果", f"{s.name}: {msg}")
        else:
            self.log(f"[LOCK] {s.name} -> {msg}")
            messagebox.showerror("失败", f"{s.name}: {msg}")

    def _open_login(self):
        pid = self._selected_pid()
        if not pid:
            messagebox.showinfo("提示", "先在左侧点选一个账号")
            return
        st = self.states.get(pid)
        if not st:
            return
        url = (st.start_url or "").strip() or "https://tw.bid.yahoo.com/myauc"
        self._handoff_open_chrome(st, url, "登录")

    def _open_login_multi(self):
        """v6.1.38:右擊菜單支援多選 — 一次打開多個帳號的 Chrome。"""
        try:
            sel = self.tree.selection() or ()
        except Exception:
            sel = ()
        if not sel:
            # fallback to single-selection helper
            self._open_login()
            return
        opened = 0
        for pid in sel:
            st = self.states.get(pid)
            if not st:
                continue
            url = (st.start_url or "").strip() or "https://tw.bid.yahoo.com/myauc"
            try:
                self._handoff_open_chrome(st, url, "登录")
                opened += 1
            except Exception as _e:
                self.log(f"[HANDOFF] {st.name} 打开失败:{_e}")
        if opened > 1:
            self.log(f"[HANDOFF] 批量打開 {opened} 個帳號 Chrome(背景啟動,每個獨立 profile)")

    def _clear_lock_multi(self):
        """v6.1.38:右擊菜單支援多選 — 一次釋放多個帳號的 profile 鎖。"""
        try:
            sel = self.tree.selection() or ()
        except Exception:
            sel = ()
        if not sel:
            self._clear_lock()
            return
        ok_n = 0
        fail_n = 0
        for pid in sel:
            s = self.states.get(pid)
            if not s:
                continue
            profile_dir = BASE_DIR / "profiles" / s.profile_id
            ok, msg = force_clear(profile_dir)
            self.log(f"[LOCK] {s.name} -> {msg}")
            if ok:
                ok_n += 1
            else:
                fail_n += 1
        if (ok_n + fail_n) > 1:
            messagebox.showinfo("批量释放完成", f"成功 {ok_n} / 失败 {fail_n}")

    def _reset_account_multi(self):
        """v6.1.58:重整選中帳號(清 monitor 內 fail counter,等同單獨重啟該帳號)。

        用於 stuck case:該帳號 Yahoo 5xx / 維護頁 持續 20+ 分鐘,但其他帳號正常,
        重啟軟件立刻好 — 表示是 software process per-account state 卡。
        """
        try:
            sel = self.tree.selection() or ()
        except Exception:
            sel = ()
        if not sel:
            messagebox.showinfo("提示", "先在左侧点选至少一个账号")
            return

        # monitor instance 在 app.py 內叫 self.mon(PureHTTPMonitor)
        mon = getattr(self, "mon", None) or getattr(self, "_pure_http_mon", None) or getattr(self, "monitor", None)
        if mon is None or not hasattr(mon, "reset_account_state"):
            messagebox.showwarning("提示", "监控未启动,无法重整帳號(请先启动监控)")
            return

        ok_n = 0
        for pid in sel:
            s = self.states.get(pid)
            if not s:
                continue
            try:
                cleared = mon.reset_account_state(pid)
                self.log(
                    f"[重整] {s.name} 清掉 in-memory state: {cleared} "
                    f"→ 下轮 poll 用 fresh connection"
                )
                ok_n += 1
            except Exception as e:
                self.log(f"[重整] {s.name} 失败: {e}")
        if ok_n > 0:
            self.log(
                f"[重整] 完成 {ok_n}/{len(sel)} — 等 1-3 分钟看是否恢复;"
                f"若立刻恢复 → 证实是 process state stuck 不是 server side"
            )

    def _open_merch_page(self, mode: str):
        pid = self._selected_pid()
        if not pid:
            messagebox.showinfo("提示", "先在左侧点选一个账号")
            return
        st = self.states.get(pid)
        if not st:
            return
        mode = (mode or "").strip()
        url = LIST_URLS.get(mode)
        if not url:
            messagebox.showerror("错误", f"未知模式：{mode}")
            return
        self._handoff_open_chrome(st, url, f"{mode}页")

    def _set_date_cutoff_months(self, months: int):
        """快捷设置截止日期为 N 个月前。"""
        from datetime import datetime, timedelta
        now = datetime.now()
        # 简单减月：月份 - N，处理跨年
        year = now.year
        month = now.month - months
        while month <= 0:
            month += 12
            year -= 1
        day = min(now.day, 28)  # 安全处理月末
        cutoff = f"{year:04d}/{month:02d}/{day:02d}"
        self.var_date_cutoff.set(cutoff)
        self.var_date_filter_enabled.set(True)

    def _start_merch_batch(self):
        if self.merch_running:
            return

        # 使用勾选的账号（可多选）— 打勾了就按打勾的来
        targets = [st for st in self.states.values() if getattr(st, "selected", False)]

        # 兜底：如果一个都没勾选，用左侧当前"选中行"（单账号）
        if not targets:
            pid = self._selected_pid()
            if pid and pid in self.states:
                targets = [self.states[pid]]

        if not targets:
            messagebox.showinfo("提示", "请先勾选至少一个账号（或在左侧点选一个账号）")
            return

        # 批量任务与监控可并行：记录本次涉及的 profile（= 重置，不累加）
        target_pids = {st.profile_id for st in targets}
        self._batch_hold_pids = set(target_pids)

        # 如果监控正在运行：立即把这些账号在监控里暂停（避免同一 profile 冲突）
        batch_hold_pids = set()
        if self.monitoring and self.mon is not None:
            batch_hold_pids = set(target_pids)
            for _pid in batch_hold_pids:
                asyncio.run_coroutine_threadsafe(self.mon.set_hold(_pid, True, reason='batch'), self.loop)
            try:
                self.log('[BATCH] 已自动暂停这些账号的监控（批量任务结束后会自动恢复）')
            except Exception:
                pass


        mode = (self.var_merch_mode.get().strip() or "下架")
        try:
            repeat = int(self.var_merch_repeat.get().strip() or "1")
            repeat = max(1, repeat)
        except Exception:
            messagebox.showerror("错误", "执行次数必须是数字")
            return
        try:
            interval = float(self.var_merch_interval.get().strip() or "0")
            interval = max(0.0, interval)
        except Exception:
            messagebox.showerror("错误", "间隔秒数必须是数字")
            return

        try:
            merch_conc = int(self.var_merch_conc.get().strip() or "1")
            merch_conc = max(1, merch_conc)
        except Exception:
            messagebox.showerror("错误", "并发账号必须是数字")
            return

        headless = True

        chrome = (getattr(self, 'var_browser', None).get().strip() if getattr(self, 'var_browser', None) else '') or self.settings.get('browser_path','') or find_chrome_exe() or ''
        if not chrome or (not os.path.exists(chrome)):
            messagebox.showerror("错误", f"找不到 chrome.exe：{chrome}\n请确认已安装 Chrome，或在右侧填写正确 Chrome 路径。")
            return

        # persist batch settings
        self.settings["browser_path"] = chrome
        self.settings["merch_mode"] = mode
        self.settings["merch_repeat"] = repeat
        self.settings["merch_interval"] = interval
        self.settings["merch_concurrency"] = merch_conc
        save_settings(self.settings)

        self.merch_running = True
        self.merch_stop_event.clear()

        # 日期过滤参数
        date_filter_enabled = self.var_date_filter_enabled.get()
        date_cutoff = self.var_date_cutoff.get().strip()
        if date_filter_enabled and not date_cutoff:
            messagebox.showerror("错误", "启用了日期过滤但未设置截止日期")
            self.merch_running = False
            return
        # 硬拦截：日期过滤仅支持下架模式
        if date_filter_enabled and mode != "下架":
            messagebox.showerror("错误", "日期过滤仅支持「下架」模式，不支持删除/上架。\n请先切换模式为「下架」，或取消勾选日期过滤。")
            self.merch_running = False
            return

        def _log_from_worker(m: str):
            self.log(m)


        async def _runner():
            # ✅ 多账号并发执行（merch_conc 控制同时跑多少个账号）
            sem = asyncio.Semaphore(merch_conc)

            # 预读『根据商品编号下架删除』的批次（做成可恢复队列：接管/暂停后自动续跑）
            merch_id_batches = {}
            if mode == "根據商品編號下架刪除":
                try:
                    from core.merch_id_ops import load_merch_id_batches
                    for st in targets:
                        try:
                            batches, ids_path = load_merch_id_batches(
                                base_dir=BASE_DIR,
                                account_name=st.name,
                                profile_id=st.profile_id,
                                batch_size=10,
                                log=_log_from_worker,
                            )
                            merch_id_batches[st.profile_id] = (batches, ids_path)
                        except Exception as e:
                            _log_from_worker(f"[MERCH-ID] {st.name}: prepare ids failed: {e}")
                except Exception as e:
                    _log_from_worker(f"[MERCH-ID] prepare ERROR: {e}")

            async def _sleep_interruptible(pid: str, seconds: float) -> bool:
                end_t = time.time() + float(seconds)
                while time.time() < end_t:
                    if self._is_merch_stop(pid):
                        return False
                    # 若接管暂停中：不计时，等恢复后再继续计时
                    if self._is_merch_pause(pid):
                        await asyncio.sleep(0.25)
                        end_t = time.time() + float(seconds)  # 恢复后重新计时（更符合"每轮间隔"）
                        continue
                    await asyncio.sleep(min(0.5, max(0.0, end_t - time.time())))
                return True

            async def _run_one(st):
                pid = st.profile_id
                pause_ev = self._ensure_merch_pause_event(pid)
                profile_dir = BASE_DIR / "profiles" / pid
                profile_dir.mkdir(parents=True, exist_ok=True)

                # ✅ 模式1：根据商品编号批量【下架 -> 删除】（纯 HTTP）
                if mode == "根據商品編號下架刪除":
                    from core.merch_http_ops import HttpBatchConfig, run_http_merch_id_ops

                    pair = merch_id_batches.get(pid)
                    if not pair:
                        _log_from_worker(f"[MERCH-ID] {st.name}: ids file missing/empty -> skip")
                        return

                    batches, ids_path = pair
                    total_batches = len(batches)
                    if total_batches <= 0:
                        _log_from_worker(f"[MERCH-ID] {st.name}: ids empty -> skip")
                        return

                    _log_from_worker(f"[HTTP] start {st.name} batches={total_batches} headless={headless}")

                    cfg2 = HttpBatchConfig(mode=mode, interval_sec=0.0,
                                           headless=headless, batch_size=10)

                    async with sem:
                        await run_http_merch_id_ops(
                            base_dir=BASE_DIR,
                            profile_dir=profile_dir,
                            chrome_path=chrome,
                            account_name=st.name,
                            profile_id=pid,
                            batches=batches,
                            cfg=cfg2,
                            proxy=getattr(st, "proxy", "") or "",
                            log=_log_from_worker,
                            is_stop=(lambda _pid=pid: self._is_merch_stop(_pid)),
                            is_pause=(lambda _pid=pid: self._is_merch_pause(_pid)),
                        )

                    _log_from_worker(f"[HTTP] {st.name}: DONE")
                    return

                # ✅ 模式1.5：复制上新（逐条下架 → 抓 clone → 重新发布）
                if mode == "複製上新":
                    from core.merch_http_ops import run_http_batch_clone_and_relist

                    _log_from_worker(f"[CLONE] start {st.name} count={repeat} interval={interval}s")

                    async with sem:
                        if self._is_merch_stop(pid):
                            return
                        status, ok_cnt = await run_http_batch_clone_and_relist(
                            profile_dir=profile_dir,
                            chrome_path=chrome,
                            count=int(repeat),
                            interval_sec=float(interval or 0),
                            headless=headless,
                            proxy=getattr(st, "proxy", "") or "",
                            log=_log_from_worker,
                            is_stop=(lambda _pid=pid: self._is_merch_stop(_pid)),
                            is_pause=(lambda _pid=pid: self._is_merch_pause(_pid)),
                        )
                    _log_from_worker(f"[CLONE] {st.name}: {status} 成功={ok_cnt}")
                    return

                # ✅ 模式2a：按上架日期批量下架（仅下架，不删除）
                if date_filter_enabled and date_cutoff:
                    from core.merch_http_ops import run_http_batch_unshelve_by_date

                    _log_from_worker(f"[HTTP] start {st.name} 按日期下架: {date_cutoff}之前 headless={headless}")

                    async with sem:
                        if self._is_merch_stop(pid):
                            return
                        status, found, ok_cnt = await run_http_batch_unshelve_by_date(
                            profile_dir=profile_dir,
                            chrome_path=chrome,
                            cutoff_date=date_cutoff,
                            headless=headless,
                            proxy=getattr(st, "proxy", "") or "",
                            log=_log_from_worker,
                            is_stop=(lambda _pid=pid: self._is_merch_stop(_pid)),
                            d1_owner=str(self.settings.get("tg_chat_id", "") or "").strip(),
                        )

                    _log_from_worker(f"[HTTP] {st.name}: 按日期下架完成 status={status} found={found} ok={ok_cnt}")
                    return

                # ✅ 模式2b：商品批量（上架/下架/刪除）纯 HTTP

                from core.merch_http_ops import HttpBatchConfig, run_http_batch

                _log_from_worker(f"[HTTP] start {st.name} mode={mode} repeat={repeat} interval={interval}s headless={headless}")

                done_round = 0
                while done_round < repeat:
                    if self._is_merch_stop(pid):
                        _log_from_worker(f"[HTTP] {st.name}: STOP")
                        return
                    if pause_ev.is_set():
                        await asyncio.sleep(0.25)
                        continue

                    async with sem:
                        if self._is_merch_stop(pid):
                            return
                        cfg_http = HttpBatchConfig(
                            mode=mode, repeat=repeat,
                            interval_sec=interval, headless=headless)
                        status, new_done = await run_http_batch(
                            profile_dir=profile_dir,
                            chrome_path=chrome,
                            cfg=cfg_http,
                            proxy=getattr(st, "proxy", "") or "",
                            log=_log_from_worker,
                            is_stop=(lambda _pid=pid: self._is_merch_stop(_pid)),
                            is_pause=(lambda _pid=pid: self._is_merch_pause(_pid)),
                            d1_owner=str(self.settings.get("tg_chat_id", "") or "").strip(),
                        )

                    if status in ("paused",):
                        done_round = new_done
                        continue
                    if status in ("locked",):
                        await asyncio.sleep(0.8)
                        continue
                    if status in ("stopped",):
                        return
                    if status in ("error", "auth_error"):
                        _log_from_worker(f"[HTTP] {st.name}: {status} (progress {new_done}/{repeat})")
                        return
                    # 关键：商品下完了立刻终止外层 while，否则 run_http_batch 一直返回 0 → 死循环
                    if status == "exhausted":
                        done_round = new_done
                        _log_from_worker(f"[HTTP] {st.name}: 商品已操作完毕（实际 {done_round}/{repeat} 轮，无更多可操作）")
                        return

                    done_round = new_done
                    _log_from_worker(f"[HTTP] {st.name}: progress {done_round}/{repeat}")

                _log_from_worker(f"[HTTP] {st.name}: DONE")
                return

            tasks = [asyncio.create_task(_run_one(st)) for st in targets]
            if tasks:
                await asyncio.gather(*tasks)
        fut = asyncio.run_coroutine_threadsafe(_runner(), self.loop)
        self.merch_future = fut

        def _done(_f):
            try:
                _f.result()
                msg = "[BATCH] finished"
            except Exception as e:
                msg = f"[BATCH] ERROR: {e}"


            # 批量任务结束：恢复这些账号的监控（如果仍在监控中）
            try:
                if getattr(self, '_batch_hold_pids', None) is not None and self.mon is not None:
                    for _pid in list(target_pids):
                        asyncio.run_coroutine_threadsafe(self.mon.set_hold(_pid, False, reason='batch'), self.loop)
                        self._batch_hold_pids.discard(_pid)
            except Exception:
                pass

            def _ui():
                self.merch_running = False
                self.log(msg)

            self.after(0, _ui)

        fut.add_done_callback(_done)

    def _stop_merch_batch(self):
        if not self.merch_running:
            return
        self.merch_stop_event.set()
        self.log("[BATCH] stop requested")




    def _ensure_merch_cancel_event(self, pid: str) -> threading.Event:
        """Stop-signal for a single account's batch task (manual cancel)."""
        pid = (pid or "").strip()
        if not pid:
            # fallback global event to avoid None checks
            return self.merch_stop_event
        ev = self.merch_cancel_events.get(pid)
        if ev is None:
            ev = threading.Event()
            self.merch_cancel_events[pid] = ev
        return ev

    def _ensure_merch_pause_event(self, pid: str) -> threading.Event:
        """Pause-signal for a single account (used by HANDOFF)."""
        pid = (pid or "").strip()
        if not pid:
            return threading.Event()
        ev = self.merch_pause_events.get(pid)
        if ev is None:
            ev = threading.Event()
            self.merch_pause_events[pid] = ev
        return ev

    def _is_merch_stop(self, pid: str) -> bool:
        if self.merch_stop_event.is_set():
            return True
        ev = self.merch_cancel_events.get((pid or "").strip())
        return bool(ev and ev.is_set())

    def _is_merch_pause(self, pid: str) -> bool:
        ev = self.merch_pause_events.get((pid or "").strip())
        return bool(ev and ev.is_set())

    def _handoff_open_chrome(self, st: AccountState, url: str, label: str) -> None:
        """打开可视化 Chrome 窗口（接管），关闭后自动恢复监控。

        - 监控：仅暂停当前账号（不需要停止全部监控）
        - 批量任务：仅暂停当前账号（进度自动续跑；其他账号继续跑）
        """
        pid = st.profile_id
        pause_ev = self._ensure_merch_pause_event(pid)

        # already open?
        proc = self.handoff_procs.get(pid)
        if proc is not None:
            try:
                if proc.poll() is None:
                    messagebox.showinfo("提示", f"{st.name} 已经处于接管窗口中，关闭该窗口后会自动恢复。")
                    return
            except Exception:
                pass

        chrome = (getattr(self, 'var_browser', None).get().strip() if hasattr(self, 'var_browser') else '') or self.settings.get('browser_path','') or find_chrome_exe() or ''
        if not chrome or (not os.path.exists(chrome)):
            messagebox.showerror("错误", f"找不到 chrome.exe：{chrome}\n请确认已安装 Chrome，或在右侧填写正确 Chrome 路径。")
            return

        # 保存 Chrome 路径到 settings
        self.settings["browser_path"] = chrome
        save_settings(self.settings)

        profile_dir = BASE_DIR / "profiles" / pid
        profile_dir.mkdir(parents=True, exist_ok=True)

        import subprocess

        async def _runner():
            try:
                # 1) hold monitor for this profile
                if self.mon is not None:
                    try:
                        await self.mon.set_hold(pid, True, reason="handoff")
                    except Exception:
                        pass

                # 2) request stop for batch task on this profile (others keep running)
                try:
                    pause_ev.set()
                except Exception:
                    pass

                # 3) wait monitor idle — 超时后额外等待，让监控有机会关闭浏览器
                if self.mon is not None:
                    try:
                        idle = await self.mon.wait_idle(pid, timeout_sec=25.0)
                        if not idle:
                            self.after(0, lambda: self.log(f"[HANDOFF] {st.name} 等待监控释放中..."))
                            await asyncio.sleep(5)
                    except Exception:
                        pass

                # 4) wait our profile lock AND Chrome Singleton to be free
                _lock_reason = ""
                t0 = time.time()
                while True:
                    in_use, _detail = detect_chrome_profile_in_use(profile_dir)
                    if not in_use:
                        ok, _lock_reason = try_acquire(profile_dir, owner="handoff")
                        if ok:
                            release(profile_dir)
                            break
                    else:
                        _lock_reason = _detail
                    if time.time() - t0 > 30.0:
                        _msg = _lock_reason
                        self.after(0, lambda: messagebox.showwarning(
                            "提示",
                            f"{st.name} Profile 仍被占用：{_msg}\n\n"
                            "你可以：\n- 等几秒再试（任务可能正在收尾）\n"
                            "- 或先停止对应任务（监控/批量）"))
                        return
                    await asyncio.sleep(0.3)

                # 5) 打开 Chrome
                args = [
                    chrome,
                    f"--user-data-dir={profile_dir}",
                    "--profile-directory=Default",
                    "--start-maximized",
                    url,
                ]

                proc = await asyncio.to_thread(
                    lambda: subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                self.handoff_procs[pid] = proc
                self.after(0, lambda: self.log(f"[HANDOFF] 已接管：{st.name} -> {label}（关闭窗口后自动恢复）"))
                try:
                    # 只靠进程是否退出来判断用户是否关闭了窗口
                    # 不再依赖 SingletonLock mtime（Chrome 不更新 mtime，10 分钟后会误判为已关闭）
                    while True:
                        await asyncio.sleep(1.0)
                        try:
                            if proc.poll() is not None:
                                break
                        except Exception:
                            break
                finally:
                    try:
                        if self.handoff_procs.get(pid) is proc:
                            del self.handoff_procs[pid]
                    except Exception:
                        pass
                    try:
                        pause_ev.clear()
                    except Exception:
                        pass
                    if self.mon is not None:
                        try:
                            await self.mon.set_hold(pid, False, reason="handoff")
                        except Exception:
                            pass
                    self.after(0, lambda: self.log(f"[HANDOFF] 已恢复：{st.name}"))
            except Exception as _handoff_err:
                # 外层兜底：确保任何异常都不会导致程序闪退
                _err_msg = str(_handoff_err)
                self.after(0, lambda: self.log(f"[HANDOFF] {st.name} 打开登录窗口异常：{_err_msg}"))
                try:
                    pause_ev.clear()
                except Exception:
                    pass
                if self.mon is not None:
                    try:
                        await self.mon.set_hold(pid, False, reason="handoff")
                    except Exception:
                        pass

        asyncio.run_coroutine_threadsafe(_runner(), self.loop)



    # =========================
    # 订单获取 / 出货（Ship）
    # =========================
    def _ship_pick_template(self):
        p = filedialog.askopenfilename(
            title="选择出货资料模板（.xlsx）",
            filetypes=[("Excel", "*.xlsx"), ("All files", "*.*")]
        )
        if p:
            self.var_ship_template.set(p)

    def _ship_pick_outdir(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.var_ship_outdir.set(p)

    def _ship_refresh_accounts(self, *_):
        """
        根据输入框内容，实时筛选账号（按「名字」或 ProfileID 包含匹配）。
        """
        try:
            q = (self.var_ship_acc_query.get() or "").strip().lower()
        except Exception:
            q = ""

        # 兼容：若属性不存在则初始化
        if not hasattr(self, "_ship_acc_display_map"):
            self._ship_acc_display_map = {}
        if not hasattr(self, "cmb_ship_account"):
            return

        states = list(getattr(self, "states", {}).values())
        states.sort(key=lambda s: (str(getattr(s, "name", "")), str(getattr(s, "profile_id", ""))))

        values = []
        mp: Dict[str, Dict[str, Any]] = {}

        for s in states:
            name = str(getattr(s, "name", "") or "")
            pid = str(getattr(s, "profile_id", "") or "")
            display = f"{name} ({pid})" if pid else name

            if q:
                if (q not in name.lower()) and (q not in pid.lower()):
                    continue

            values.append(display)
            mp[display] = {"name": name, "profile_id": pid}

        self._ship_acc_display_map = mp
        try:
            self.cmb_ship_account["values"] = values
        except Exception:
            pass

    def _ship_on_account_selected(self, *_):
        # 当前直接通过 combobox 取值即可
        return

    # --- 动态快递/商品行 ---
    def _ship_pkg_add_row(self, preset: Optional[Dict[str, str]] = None):
        """新增一行：国内快递单号 + 商品名稱 + 规格 + 代付日期(mmdd) + 代付金額。"""
        preset = preset or {}
        if not hasattr(self, "ship_pkg_rows"):
            self.ship_pkg_rows = []
        self.ship_pkg_rows.append({
            "tracking": tk.StringVar(value=preset.get("tracking", "")),
            "product_name": tk.StringVar(value=preset.get("product_name", "")),
            "spec": tk.StringVar(value=preset.get("spec", "")),
            "pay_mmdd": tk.StringVar(value=preset.get("pay_mmdd", "")),
            "pay_amount": tk.StringVar(value=preset.get("pay_amount", "")),
        })
        self._ship_pkg_render_rows()

    def _ship_pkg_remove_row(self, idx: int):
        try:
            if idx <= 0:
                return  # 保留第一行
            self.ship_pkg_rows.pop(idx)
        except Exception:
            return
        self._ship_pkg_render_rows()

    def _ship_pkg_render_rows(self):
        holder = getattr(self, "ship_pkg_frame", None)
        if holder is None:
            return

        # 清空旧控件
        try:
            for w in list(holder.winfo_children()):
                w.destroy()
        except Exception:
            pass

        # 逐行渲染
        for i, row in enumerate(self.ship_pkg_rows):
            r = i
            ent_tracking = ctk.CTkEntry(holder, textvariable=row["tracking"], width=180, corner_radius=6, border_color="#D2D2D7")
            ent_tracking.grid(row=r, column=0, sticky="ew", padx=4, pady=2)

            btnf = ttk.Frame(holder)
            btnf.grid(row=r, column=1, sticky="w", padx=4, pady=2)
            ctk.CTkButton(btnf, text="+", width=30, command=self._ship_pkg_add_row, **self._ctk_N).pack(side="left")
            if i > 0:
                ctk.CTkButton(btnf, text="-", width=30, command=lambda ii=i: self._ship_pkg_remove_row(ii), **self._ctk_D).pack(side="left", padx=(4, 0))

            ent_name = ctk.CTkEntry(holder, textvariable=row["product_name"], width=300, corner_radius=6, border_color="#D2D2D7")
            ent_name.grid(row=r, column=2, sticky="ew", padx=4, pady=2)

            ent_spec = ctk.CTkEntry(holder, textvariable=row["spec"], width=120, corner_radius=6, border_color="#D2D2D7")
            ent_spec.grid(row=r, column=3, sticky="ew", padx=4, pady=2)

            ent_date = ctk.CTkEntry(holder, textvariable=row["pay_mmdd"], width=100, corner_radius=6, border_color="#D2D2D7")
            ent_date.grid(row=r, column=4, sticky="ew", padx=4, pady=2)

            ent_amt = ctk.CTkEntry(holder, textvariable=row["pay_amount"], width=100, corner_radius=6, border_color="#D2D2D7")
            ent_amt.grid(row=r, column=5, sticky="ew", padx=4, pady=2)

        try:
            holder.columnconfigure(2, weight=1)
        except Exception:
            pass

    def _ship_collect_shipments(self) -> List[Dict[str, str]]:
        """从界面收集快递/商品信息。返回 shipments 列表。"""
        shipments: List[Dict[str, str]] = []
        rows = getattr(self, "ship_pkg_rows", []) or []

        def norm_mmdd(s: str) -> str:
            s2 = re.sub(r"\D", "", s or "")
            if not s2:
                return ""
            if len(s2) == 3:
                s2 = "0" + s2
            if len(s2) != 4:
                raise ValueError("代付日期请填纯数字 mmdd，例如 0107")
            m = int(s2[:2])
            d = int(s2[2:])
            if not (1 <= m <= 12 and 1 <= d <= 31):
                raise ValueError("代付日期不合法，请用 mmdd，例如 0107")
            return s2

        for row in rows:
            tracking = (row["tracking"].get() or "").strip()
            product_name = (row["product_name"].get() or "").strip()
            spec = (row["spec"].get() or "").strip()
            pay_mmdd_raw = (row["pay_mmdd"].get() or "").strip()
            pay_amount = (row["pay_amount"].get() or "").strip()

            # 全空行 -> 忽略
            if not any([tracking, product_name, spec, pay_mmdd_raw, pay_amount]):
                continue

            if not tracking:
                raise ValueError("快递单号不能为空（若不需要该行请清空整行）")

            pay_mmdd = norm_mmdd(pay_mmdd_raw)

            shipments.append({
                "tracking_no": tracking,
                "product_name": product_name,
                "spec": spec,
                "pay_mmdd": pay_mmdd,
                "pay_amount": pay_amount,
            })

        if not shipments:
            raise ValueError("请至少填写 1 条国内快递单号")

        return shipments

    def _ship_build_order_display(self, main_order: str, sub_orders: List[str]) -> str:
        main_order = str(main_order or "").strip()
        subs = [str(x or "").strip() for x in (sub_orders or []) if str(x or "").strip()]
        if not subs:
            return main_order
        return main_order + "+" + "+".join(subs)

    def _ship_get_sub_orders(self) -> List[str]:
        rows = getattr(self, "_ship_sub_order_rows", None) or []
        raw: List[str] = []
        for r in rows:
            try:
                v = (r.get("var").get() if isinstance(r, dict) and r.get("var") is not None else "").strip()
            except Exception:
                v = ""
            if v:
                raw.append(v)
        # 去重保持顺序
        seen = set()
        out: List[str] = []
        for x in raw:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    def _ship_clear_sub_orders(self) -> None:
        rows = getattr(self, "_ship_sub_order_rows", None) or []
        for r in list(rows):
            try:
                ent = r.get("ent")
                if ent is not None:
                    ent.destroy()
            except Exception:
                pass
        self._ship_sub_order_rows = []

    def _ship_add_sub_order_row(self, preset: str = "") -> None:
        holder = getattr(self, "_ship_sub_orders_holder", None)
        if holder is None:
            return
        rows = getattr(self, "_ship_sub_order_rows", None)
        if rows is None:
            rows = []
            self._ship_sub_order_rows = rows

        var = tk.StringVar(value=str(preset or ""))
        ent = ctk.CTkEntry(holder, textvariable=var, corner_radius=6, border_color="#D2D2D7")
        ent.grid(row=len(rows), column=0, sticky="ew", pady=(0, 2))
        holder.columnconfigure(0, weight=1)
        rows.append({"var": var, "ent": ent})
        try:
            ent.focus_set()
        except Exception:
            pass


    def _ship_render_tasks(self):
        tree = getattr(self, "ship_tree", None) or getattr(self, "tree_ship", None)
        if not tree:
            return
        try:
            tree.delete(*tree.get_children())
        except Exception:
            pass

        for i, t in enumerate(self.ship_tasks, start=1):
            iid = str(t.get("id", i))

            # tracking 显示：首个 + 数量
            tracking_show = ""
            ships = t.get("shipments") or []
            if ships:
                try:
                    tracking_show = str(ships[0].get("tracking_no", "") or "")
                except Exception:
                    tracking_show = ""
                if len(ships) > 1:
                    tracking_show = f"{tracking_show} (+{len(ships)-1})"

            # 每笔订单备注（可为空）
            remark_show = str(t.get("remark", "") or "")

            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    i,
                    t.get("acc_name", ""),
                    (t.get("order_no_display") or self._ship_build_order_display(t.get("order_no", ""), t.get("sub_order_nos") or [])),
                    tracking_show,
                    remark_show,
                    t.get("status", ""),
                    t.get("exec_code", ""),
                    t.get("amount", ""),
                    t.get("channel", ""),
                ),
            )

    def _ship_on_task_select(self, event=None):
        """任务列表选中变化：把该订单的备注加载到输入框。"""
        tree = getattr(self, "ship_tree", None) or getattr(self, "tree_ship", None)
        if not tree:
            return
        sel = list(tree.selection())
        if not sel:
            self._ship_selected_task_id = None
            try:
                self.var_ship_task_remark.set("")
            except Exception:
                pass
            return

        iid = sel[0]
        try:
            task_id = int(iid)
        except Exception:
            task_id = None
        self._ship_selected_task_id = task_id

        remark = ""
        if task_id is not None:
            for t in self.ship_tasks:
                try:
                    if int(t.get("id")) == int(task_id):
                        remark = str(t.get("remark", "") or "")
                        break
                except Exception:
                    continue
        try:
            self.var_ship_task_remark.set(remark)
        except Exception:
            pass

    def _ship_save_selected_remark(self):
        """保存当前选中任务的备注（每笔订单一个备注）。"""
        tree = getattr(self, "ship_tree", None) or getattr(self, "tree_ship", None)
        if not tree:
            return
        sel = list(tree.selection())
        if not sel:
            self.log("[SHIP] 未选择任务：备注将用于新增任务")
            return
        iid = sel[0]
        try:
            task_id = int(iid)
        except Exception:
            messagebox.showerror("错误", "无法识别当前选中的任务")
            return

        remark = ""
        try:
            remark = str(self.var_ship_task_remark.get() or "").strip()
        except Exception:
            remark = ""

        self._ship_update_task(task_id, remark=remark)

        # 重新选中，保证保存后备注仍显示
        try:
            tree.selection_set(iid)
        except Exception:
            pass

        self.log(f"[SHIP] 已更新备注：{task_id}")


    # ------------------------------
    # 出货任务持久化（任务列表记忆）
    # ------------------------------
    def _ship_load_tasks_from_disk(self) -> None:
        """从 ship_tasks.json 读取任务列表（若存在）。"""
        try:
            path = getattr(self, "_ship_tasks_file", None) or SHIP_TASKS_FILE
            if not Path(path).exists():
                return
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            tasks = []
            max_id = 0
            for t in raw:
                if not isinstance(t, dict):
                    continue
                profile_id = str(t.get("profile_id", "") or "").strip()
                order_no = str(t.get("order_no", "") or "").strip()
                if not profile_id or not order_no:
                    continue
                _id = int(t.get("id", 0) or 0)
                if _id <= 0:
                    _id = max_id + 1
                max_id = max(max_id, _id)
                shipments = t.get("shipments") or []
                if not isinstance(shipments, list):
                    shipments = []
                tasks.append({
                    "id": _id,
                    "acc_name": str(t.get("acc_name", "") or ""),
                    "profile_id": profile_id,
                    "order_no": order_no,
                    "sub_order_nos": (t.get("sub_order_nos") if isinstance(t.get("sub_order_nos"), list) else []),
                    "order_no_display": str(t.get("order_no_display", "") or "").strip() or self._ship_build_order_display(order_no, (t.get("sub_order_nos") if isinstance(t.get("sub_order_nos"), list) else [])),

                    "shipments": shipments,
                    "remark": str(t.get("remark", "") or ""),
                    # 来源/平台标记：用于【监控推送】生成【最新模板】与物流系统自动上传
                    "from_purchase_monitor": bool(t.get("from_purchase_monitor")),
                    "purchase_platform": str(t.get("purchase_platform", "") or "").strip(),
                    "status": str(t.get("status", "待执行") or "待执行"),
                    "amount": str(t.get("amount", "") or ""),
                    "channel": str(t.get("channel", "") or ""),
                    "exec_code": str(t.get("exec_code", "") or ""),
                    "error": str(t.get("error", "") or ""),
                })
            self.ship_tasks = tasks
            self._ship_task_seq = max_id
            if tasks:
                self.log(f"[SHIP] 已从本地恢复 {len(tasks)} 条任务")
        except Exception as e:
            self.log(f"[SHIP] 读取任务持久化失败：{e}")

    def _ship_save_tasks_to_disk(self) -> None:
        """保存任务列表到 ship_tasks.json（原子写入）。"""
        self._ship_save_after = None
        try:
            path = getattr(self, "_ship_tasks_file", None) or SHIP_TASKS_FILE
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.ship_tasks, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            self.log(f"[SHIP] 保存任务持久化失败：{e}")

    
    def _ship_add_task_payload_threadsafe(self, payload: Dict[str, Any]) -> None:
        """线程安全：采购监控线程里也会调用。只做 after 转发，不改变功能。"""
        try:
            self.after(0, lambda p=payload: self._ship_add_task_payload(p))
        except Exception:
            # 退化：直接调用（极少数早期/无UI场景）
            self._ship_add_task_payload(payload)

    def ship_auto_enqueue(self, profile_id: str, order_no: str, reason: str = "") -> None:
        """
        采购监控抓到物流单号后调用：
        - 把该订单加入自动出货队列
        - 自动运行【订单获取/出货】并生成Excel（暂不做上传物流系统）
        """
        pid = str(profile_id or "").strip()
        ono = str(order_no or "").strip()
        if not pid or not ono:
            return
        key = (pid, ono)
        if key in self._ship_auto_queue_set:
            return
        self._ship_auto_queue_set.add(key)
        self._ship_auto_queue.append({"profile_id": pid, "order_no": ono, "reason": reason, "tries": 0})
        # 触发处理（主线程）
        try:
            self.after(50, self._ship_auto_try_run_next)
        except Exception:
            pass

    def _ship_auto_try_run_next(self) -> None:
        """自动出货队列：一次只跑一单，避免和手动出货并发。"""
        if getattr(self, "ship_running", False):
            return
        if not getattr(self, "_ship_auto_queue", None):
            return
        if not self._ship_auto_queue:
            return

        item = self._ship_auto_queue[0]
        pid = item.get("profile_id")
        ono = item.get("order_no")
        reason = item.get("reason","")
        tries = int(item.get("tries", 0) or 0)

        # 找到对应任务（可能稍后才被 after 加入）
        target = None
        for t in self.ship_tasks:
            if str(t.get("profile_id","")) == str(pid) and str(t.get("order_no","")) == str(ono):
                target = t
                break

        if not target:
            tries += 1
            item["tries"] = tries
            if tries >= 25:
                self.log(f"[SHIP] 自动出货：找不到任务，已放弃（{pid} / {ono}）")
                self._ship_auto_queue.popleft()
                self._ship_auto_queue_set.discard((pid, ono))
            else:
                # 稍后重试
                self.after(300, self._ship_auto_try_run_next)
            return

        # 若任务已完成/执行中：直接跳过
        st = str(target.get("status","") or "")
        if st in ("完成", "执行中"):
            self._ship_auto_queue.popleft()
            self._ship_auto_queue_set.discard((pid, ono))
            self.after(50, self._ship_auto_try_run_next)
            return

        # 只跑这一单
        pending = [target]
        self._ship_run_subset(pending_tasks=pending, auto_ctx={"profile_id": pid, "order_no": ono, "reason": reason})

    def _ship_run_subset(self, pending_tasks: List[Dict[str, Any]], auto_ctx: Optional[Dict[str, Any]] = None) -> None:
        """与 _ship_run 相同核心逻辑，但只执行指定任务列表（用于自动化，不弹窗）。"""
        if getattr(self, "ship_running", False):
            return
        if not pending_tasks:
            return

        template = (getattr(self, "var_ship_template", tk.StringVar()).get() if hasattr(self, "var_ship_template") else "").strip()
        outdir = (getattr(self, "var_ship_outdir", tk.StringVar()).get() if hasattr(self, "var_ship_outdir") else "").strip()
        user_code = (getattr(self, "var_ship_user_code", tk.StringVar()).get() if hasattr(self, "var_ship_user_code") else "").strip()
        owner = (getattr(self, "var_ship_owner", tk.StringVar()).get() if hasattr(self, "var_ship_owner") else "").strip()

        # 模板可留空；若填写了但不存在：自动化时不弹窗，只写日志并放弃本次
        if template and (not Path(template).exists()):
            self.log("[SHIP] 自动出货失败：Excel 模板路径不存在（请到【订单获取/出货】检查模板路径）")
            # 出队并继续下一个
            if auto_ctx:
                pid = auto_ctx.get("profile_id"); ono = auto_ctx.get("order_no")
                try:
                    self._ship_auto_queue.popleft()
                    self._ship_auto_queue_set.discard((pid, ono))
                except Exception:
                    pass
                self.after(50, self._ship_auto_try_run_next)
            return

        if not outdir:
            outdir = str((BASE_DIR / "output").resolve())
            try:
                self.var_ship_outdir.set(outdir)
            except Exception:
                pass
        Path(outdir).mkdir(parents=True, exist_ok=True)

        # 保存路径/编码/所有者（不改功能，只同步设置）
        try:
            self.settings["ship_template_path"] = template
            self.settings["ship_outdir"] = outdir
            self.settings["ship_user_code"] = user_code
            self.settings["ship_owner"] = owner
            from core.accounts import save_settings as _ss
            _ss(self.settings)
        except Exception:
            pass

        self.ship_running = True
        try:
            self.ship_stop_event.clear()
        except Exception:
            pass

        # 分组：同账号一起跑（一个账号打开一次浏览器，顺序执行多个订单）
        pending = [t for t in pending_tasks if t.get("status") in ("待执行", "失败")]
        if not pending:
            self.ship_running = False
            if auto_ctx:
                pid = auto_ctx.get("profile_id"); ono = auto_ctx.get("order_no")
                try:
                    self._ship_auto_queue.popleft()
                    self._ship_auto_queue_set.discard((pid, ono))
                except Exception:
                    pass
                self.after(50, self._ship_auto_try_run_next)
            return

        def worker():
            try:
                self.log(f"[SHIP] 自动出货开始：{len(pending)} 条（{auto_ctx.get('reason','') if auto_ctx else ''}）")
                groups: Dict[str, list] = {}
                for t in pending:
                    key = t["profile_id"]
                    groups.setdefault(key, []).append(t)

                for profile_id, ts in groups.items():
                    if self.ship_stop_event.is_set():
                        break

                    # 标记为执行中
                    for t in ts:
                        self.after(0, lambda tid=t["id"]: self._ship_update_task(tid, status="执行中", error=""))

                    acc_name = ts[0].get("acc_name") or profile_id
                    profile_dir = (BASE_DIR / "profiles" / profile_id).resolve()
                    browser_path = (self.var_browser.get() if hasattr(self, "var_browser") else str(self.settings.get("browser_path","") or ""))
                    browser_path = (browser_path or "").strip() or None

                    headless = bool(getattr(self, "var_ship_headless", self.var_headless).get())
                    self.log(f"[SHIP] 浏览器模式：{'无头' if headless else '有头'}")
                    timeout_sec = int(self.settings.get("timeout_sec", 45) or 45)

                    orders = []
                    for t in ts:
                        orders.append({
                            "order_no": t["order_no"],
                            "order_no_display": str(t.get("order_no_display", "") or "").strip(),
                            "sub_order_nos": (t.get("sub_order_nos") if isinstance(t.get("sub_order_nos"), list) else []),
                            "shipments": t.get("shipments") or [],
                            "remark": t.get("remark", "") or "",
                            # 来源/平台：只有【监控推送】才会为 True / 有值
                            "from_purchase_monitor": bool(t.get("from_purchase_monitor")),
                            "purchase_platform": str(t.get("purchase_platform") or "").strip(),
                            # 物流系统：自动上传需要的字段（仅监控推送+闲鱼会填）
                            "syb_auto_upload": False,
                            "syb_latest_template_path": "",
                        })

                    def on_log_wrap(msg: str):
                        try:
                            self.log(msg)
                        except Exception:
                            pass

                    results = export_order_to_excel(
                        profile_id=profile_id,
                        account_name=acc_name,
                        profile_dir=profile_dir,
                        browser_path=browser_path,
                        headless=headless,
                        timeout_sec=timeout_sec,
                        template_path=Path(template),
                        output_dir=Path(outdir),
                        user_code=user_code,
                        owner_name=owner,
                        orders=orders,
                        on_log=on_log_wrap,
                        stop_event=self.ship_stop_event,
                    )

                    # 仅：【监控推送】 + 【闲鱼】 -> 生成【最新模板_YYYYMMDD.xlsx】（用于物流系统上传）
                    # 前提：至少有一条订单成功抓取到数据（避免 Chrome 崩溃后用空数据生成模板）
                    _any_found = any(r.get("found") for r in (results or []))
                    try:
                        x_need = [o.get("order_no") for o in (orders or []) if o.get("from_purchase_monitor") and (o.get("purchase_platform") == "xianyu") and o.get("order_no")]
                        if x_need and _any_found:
                            from core.latest_template_builder import build_latest_template_from_ship_excel
                            date_str = time.strftime("%Y%m%d", time.localtime())
                            ship_xlsx = Path(outdir) / f"出货资料_{date_str}.xlsx"
                            out_tpl = Path(outdir) / f"最新模板_{date_str}.xlsx"
                            if not ship_xlsx.exists():
                                self.log(f"[SYB] 未找到出货资料：{ship_xlsx}（跳过生成最新模板）")
                            else:
                                build_latest_template_from_ship_excel(
                                    ship_excel_path=ship_xlsx,
                                    output_path=out_tpl,
                                    order_nos=x_need,
                                    log_fn=self.log,
                                )
                                for o in orders:
                                    if o.get("from_purchase_monitor") and (o.get("purchase_platform") == "xianyu"):
                                        o["syb_auto_upload"] = True
                                        o["syb_latest_template_path"] = str(out_tpl)
                                self.log(f"[SYB] 已生成最新模板：{out_tpl}")
                        elif x_need and not _any_found:
                            self.log("[SYB] 所有订单抓取失败，跳过生成最新模板")
                    except Exception as e:
                        self.log(f"[SYB] 生成最新模板失败：{e}")

                    # 物流系统：自动上传资料/面单（t=3/t=5）
                    try:
                        if hasattr(self, "syb_upload_tab") and self.syb_upload_tab:
                            self.syb_upload_tab.on_ship_results(profile_id, acc_name, orders, results)
                    except Exception as e:
                        self.log(f"[SYB] 自动出货后上传异常：{e}")

                    res_by_order = {r.get("order_no"): r for r in (results or [])}
                    for t in ts:
                        r = res_by_order.get(t["order_no"]) or {}
                        if r.get("found"):
                            self.after(0, lambda tid=t["id"], rr=r: self._ship_update_task(
                                tid,
                                status="完成",
                                amount=rr.get("amount", ""),
                                channel=rr.get("channel", ""),
                                exec_code=rr.get("exec_code", ""),
                                error="",
                            ))
                        else:
                            self.after(0, lambda tid=t["id"], rr=r: self._ship_update_task(
                                tid,
                                status="失败",
                                error=rr.get("error", "未知错误"),
                            ))

                if self.ship_stop_event.is_set():
                    self.log("[SHIP] 自动出货已停止")
                else:
                    self.log("[SHIP] 自动出货完成")
                    # TG 推送出货完成通知
                    try:
                        _ok = sum(1 for r in (results or []) if r.get("found"))
                        _fail = len(results or []) - _ok
                        if hasattr(self, '_purchase_tg_cmd') and self._purchase_cmd:
                            self._purchase_cmd.notify_export_done(
                                acc_name=acc_name,
                                order_count=len(results or []),
                                success_count=_ok,
                                fail_count=_fail,
                            )
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"[SHIP] 自动出货异常：{e}")
            finally:
                self.ship_running = False
                # 出队并继续下一个
                if auto_ctx:
                    pid = auto_ctx.get("profile_id"); ono = auto_ctx.get("order_no")
                    try:
                        self._ship_auto_queue.popleft()
                        self._ship_auto_queue_set.discard((pid, ono))
                    except Exception:
                        pass
                    self.after(50, self._ship_auto_try_run_next)

        threading.Thread(target=worker, daemon=True).start()

    def _ship_schedule_save(self) -> None:
        """防抖保存：短时间内多次更新只写一次磁盘。"""
        try:
            if getattr(self, "_ship_save_after", None) is not None:
                return
            self._ship_save_after = self.after(250, self._ship_save_tasks_to_disk)
        except Exception:
            try:
                self._ship_save_tasks_to_disk()
            except Exception:
                pass

    def _on_configure_save_geo(self, event=None):
        """窗口尺寸/位置变化时节流保存到 settings.json（2秒防抖）。"""
        if event and event.widget is not self:
            return
        if self._geo_save_after_id is not None:
            self.after_cancel(self._geo_save_after_id)
        self._geo_save_after_id = self.after(2000, self._do_save_geo)

    def _do_save_geo(self):
        """实际保存窗口几何 + 分栏位置到 settings.json。"""
        self._geo_save_after_id = None
        try:
            self.settings["window_zoomed"] = (self.state() == "zoomed")
            self.settings["window_geometry"] = self.geometry()
            # 保存分栏 sash 位置
            try:
                self.settings["sash_h"] = self._hpan.sashpos(0)
                self.settings["sash_v"] = self._vpan.sashpos(0)
            except Exception:
                pass
            save_settings(self.settings)
        except Exception:
            pass

    def _on_close(self) -> None:
        # 停止采购 TG Bot
        if self._purchase_tg_bot:
            try:
                self._purchase_tg_bot.stop()
            except Exception:
                pass
        # 停止管理 TG Bot
        if self._manage_tg_bot:
            try:
                self._manage_tg_bot.stop()
            except Exception:
                pass
        # 停止运营 TG Bot
        if self._ops_tg_bot:
            try:
                self._ops_tg_bot.stop()
            except Exception:
                pass
        try:
            self._ship_save_tasks_to_disk()
        except Exception:
            pass
        try:
            self.settings["window_zoomed"] = (self.state() == "zoomed")
            self.settings["window_geometry"] = self.geometry()
            try:
                self.settings["sash_h"] = self._hpan.sashpos(0)
                self.settings["sash_v"] = self._vpan.sashpos(0)
            except Exception:
                pass
            save_settings(self.settings)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _ship_add_task_payload(self, payload: Dict[str, Any]) -> None:
        """给『采购绑定/监控』回调用：把采购页整理好的数据合并到出货任务列表。"""
        profile_id = str(payload.get("profile_id", "") or "").strip()
        order_no = str(payload.get("order_no", "") or "").strip()
        remark_text = str(payload.get("remark") or "").strip()
        from_purchase_monitor = bool(payload.get("from_purchase_monitor"))
        purchase_platform = str(payload.get("purchase_platform") or "").strip()
        acc_name = str(payload.get("acc_name", payload.get("acc_name", "")) or "")
        # payload 里我们用 acc_name 字段名为 acc_name/acc_name? 兼容两种
        if not acc_name:
            acc_name = str(payload.get("acc_name", payload.get("acc_name", "")) or "")
        if not acc_name:
            acc_name = str(payload.get("acc_name", payload.get("acc_name", "")) or "")
        # 兼容 purchase_feature 传的 acc_name
        if not acc_name:
            acc_name = str(payload.get("acc_name", payload.get("acc_name", "")) or "")
        # 实际 key 是 acc_name in our payload? purchase_feature uses 'acc_name'
        acc_name = str(payload.get("acc_name", payload.get("acc_name", payload.get("acc_name", ""))) or "")

        # 副订单：仅用于显示/导出，不参与抓取/检索
        sub_orders = payload.get("sub_order_nos") or payload.get("yahoo_sub_order_nos") or []
        if not isinstance(sub_orders, list):
            sub_orders = []
        sub_orders = [str(x).strip() for x in sub_orders if str(x).strip() and str(x).strip() != order_no]
        # 去重保持顺序
        seen = set()
        _subs = []
        for x in sub_orders:
            if x in seen:
                continue
            seen.add(x)
            _subs.append(x)
        sub_orders = _subs

        order_display = str(payload.get("order_no_display") or "").strip()
        if not order_display:
            order_display = self._ship_build_order_display(order_no, sub_orders)

        shipments_in = payload.get("shipments") or []
        if not isinstance(shipments_in, list):
            shipments_in = []

        # 已存在则【合并更新】而不是弹窗报错：
        #  - 采购监控/手动发送可能会对同一主订单重复推送（例如新增了副单/备注/多条采购记录）
        #  - 这里统一做"合并快递 + 同步备注/副单显示"，避免"禁止重复推送"的误弹窗
        for t in self.ship_tasks:
            if str(t.get("profile_id")) == profile_id and str(t.get("order_no")) == order_no:
                try:
                    # 合并快递：按 tracking_no 去重
                    t.setdefault("shipments", [])
                    exist_tr = {
                        str(s.get("tracking_no") or "").strip()
                        for s in (t.get("shipments") or [])
                        if isinstance(s, dict)
                    }
                    for s in shipments_in:
                        if not isinstance(s, dict):
                            continue
                        tr = str(s.get("tracking_no") or "").strip()
                        if tr and tr not in exist_tr:
                            t["shipments"].append(dict(s))
                            exist_tr.add(tr)

                    # 同步备注/副单显示（不影响实际抓取）
                    if remark_text:
                        t["remark"] = remark_text
                    if order_display:
                        t["order_no_display"] = order_display
                    t["sub_order_nos"] = list(sub_orders)

                    # 标记来源/平台：用于后续生成【最新模板】与物流系统上传逻辑
                    if from_purchase_monitor:
                        t["from_purchase_monitor"] = True
                    if purchase_platform:
                        t["purchase_platform"] = purchase_platform

                    self._ship_render_tasks()
                    self._ship_schedule_save()
                    self.log(f"[SHIP] 已更新任务（来自采购页）：{acc_name} / {order_no}")
                except Exception as e:
                    self.log(f"[SHIP] 合并更新任务失败：{acc_name} / {order_no} ({e})")
                return

        # 新增任务（来自采购页）
        self._ship_task_seq += 1
        task_id = self._ship_task_seq
        task = {
            "id": task_id,
            "acc_name": acc_name,
            "profile_id": profile_id,
            "order_no": order_no,
            "sub_order_nos": list(sub_orders),
            "order_no_display": order_display,
            "shipments": [dict(x) for x in shipments_in if isinstance(x, dict)],
            "remark": remark_text,
            # 来源/平台标记（来自采购绑定/监控推送才会有）
            # 手动新增任务：不是监控推送，不生成最新模板/不自动上传
            "from_purchase_monitor": bool(from_purchase_monitor),
            "purchase_platform": purchase_platform,
            "status": "待执行",
            "amount": "",
            "channel": "",
            "exec_code": "",
            "error": "",
        }
        self.ship_tasks.append(task)

        self._ship_render_tasks()
        self._ship_schedule_save()
        self.log(f"[SHIP] 已新增任务（来自采购页）：{acc_name} / {order_no}")


    def _ship_toggle_add_panel(self):
        if self._ship_add_visible:
            self._lf_ship_add.grid_remove()
            self._ship_add_visible = False
        else:
            self._lf_ship_add.grid(row=5, column=0, sticky="ew", padx=4, pady=(0, 4))
            self._ship_add_visible = True

    def _ship_add_tasks(self):
        disp = (self.cmb_ship_account.get() or "").strip()
        if not disp:
            messagebox.showerror("错误", "请先选择账号（名字）")
            return

        acc = None
        mp = getattr(self, "_ship_acc_display_map", {}) or {}
        if disp in mp:
            acc = mp[disp]
        else:
            # 用户可能直接输入 profile_id / 名字
            for s in getattr(self, "states", {}).values():
                if disp.lower() in (str(getattr(s, "profile_id", "")).lower(), str(getattr(s, "name", "")).lower()):
                    acc = {"name": str(getattr(s, "name", "")), "profile_id": str(getattr(s, "profile_id", ""))}
                    break

        if not acc or not acc.get("profile_id"):
            messagebox.showerror("错误", "未能识别账号，请从下拉列表选择")
            return

        try:
            shipments = self._ship_collect_shipments()
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return

        main_order = (getattr(self, "var_ship_main_order", None).get() if getattr(self, "var_ship_main_order", None) else "").strip()
        if not main_order:
            messagebox.showerror("错误", "请填写主订单编号（只能一条）")
            return

        # 副订单：仅用于显示/导出，不参与抓取/检索
        sub_orders = self._ship_get_sub_orders()
        # 生成显示：主+副+副...
        order_display = self._ship_build_order_display(main_order, sub_orders)

        # 禁止重复任务（同账号+同主订单）——不合并
        existing = {(t.get("profile_id"), t.get("order_no")) for t in self.ship_tasks}
        if (acc["profile_id"], main_order) in existing:
            messagebox.showerror("禁止重复", f"该主订单已存在任务：{main_order}\n（账号：{acc['name']}）")
            return

        added = 0
        remark_text = (self.var_ship_task_remark.get() or "").strip()

        # 仅新增 1 条任务（主订单一条；副订单仅显示/导出）
        self._ship_task_seq += 1
        task_id = self._ship_task_seq
        self.ship_tasks.append({
            "id": task_id,
            "acc_name": acc["name"],
            "profile_id": acc["profile_id"],
            "order_no": main_order,
            "sub_order_nos": list(sub_orders),
            "order_no_display": order_display,
            "shipments": [dict(x) for x in shipments],
            "remark": remark_text,
            # 手动在【订单获取/出货】新增的任务：一律不参与【最新模板】生成/物流系统自动上传
            "from_purchase_monitor": False,
            "purchase_platform": "",
            "status": "待执行",
            "amount": "",
            "channel": "",
            "exec_code": "",
            "error": "",
        })
        added = 1

        self._ship_render_tasks()
        self._ship_schedule_save()
        self.log(f"[SHIP] 已新增 {added} 条任务（账号：{acc['name']}）")

    def _ship_remove_selected(self):
        tree = getattr(self, "ship_tree", None) or getattr(self, "tree_ship", None)
        if not tree:
            return
        sel = list(tree.selection())
        if not sel:
            return
        sel_ids = set(sel)
        self.ship_tasks = [t for t in self.ship_tasks if str(t.get("id")) not in sel_ids]
        self._ship_render_tasks()
        self._ship_schedule_save()

    def _ship_clear_tasks(self):
        self.ship_tasks = []
        self._ship_render_tasks()
        self._ship_schedule_save()

    def _ship_stop(self):
        try:
            self.ship_stop_event.set()
        except Exception:
            pass
        self.log("[SHIP] 已发送停止指令（会在当前订单结束后停止）")

    def _ship_update_task(self, task_id: int, **kwargs):
        for t in self.ship_tasks:
            if int(t.get("id")) == int(task_id):
                t.update(kwargs)
                break
        self._ship_render_tasks()
        self._ship_schedule_save()

    def _ship_run(self):
        if self.ship_running:
            return

        template = (self.var_ship_template.get() or "").strip()
        outdir = (self.var_ship_outdir.get() or "").strip()
        user_code = (self.var_ship_user_code.get() or "").strip()
        owner = (self.var_ship_owner.get() or "").strip()
        # 模板已内置：允许留空。若你手动填写了模板路径，则必须存在。
        if template and (not Path(template).exists()):
            messagebox.showerror("错误", "Excel 模板路径不存在（模板可留空，留空则使用内置模板）")
            return
        if not outdir:
            outdir = str((BASE_DIR / "output").resolve())
            self.var_ship_outdir.set(outdir)
        Path(outdir).mkdir(parents=True, exist_ok=True)

        if not user_code:
            messagebox.showerror("错误", "请填写「使用者简称」")
            return
        if not owner:
            messagebox.showerror("错误", "请填写「所属人」")
            return
        if not self.ship_tasks:
            messagebox.showerror("错误", "任务列表为空")
            return

        # 保存设置
        self.settings["ship_template"] = template
        self.settings["ship_outdir"] = outdir
        self.settings["ship_user_code"] = user_code
        self.settings["ship_owner"] = owner
        save_settings(self.settings)

        self.ship_running = True
        try:
            self.ship_stop_event.clear()
        except Exception:
            pass

                # 分组：同账号一起跑（一个账号打开一次浏览器，顺序执行多个订单）
        pending = [t for t in self.ship_tasks if t.get("status") in ("待执行", "失败")]
        if not pending:
            messagebox.showinfo("提示", "没有待执行任务")
            self.ship_running = False
            return

        def worker():
            try:
                self.log(f"[SHIP] 开始执行：共 {len(pending)} 条")
                groups: Dict[str, list] = {}
                for t in pending:
                    key = t["profile_id"]
                    groups.setdefault(key, []).append(t)

                for profile_id, ts in groups.items():
                    if self.ship_stop_event.is_set():
                        break

                    # 标记为执行中
                    for t in ts:
                        self.after(0, lambda tid=t["id"]: self._ship_update_task(tid, status="执行中", error=""))

                    # 账号昵称用于写 Excel；profile_id 用于找 profiles 目录
                    acc_name = ts[0].get("acc_name") or profile_id

                    profile_dir = (BASE_DIR / "profiles" / profile_id).resolve()
                    browser_path = (self.var_browser.get() or "").strip() if hasattr(self, "var_browser") else str(self.settings.get("browser_path","") or "")
                    browser_path = (browser_path or "").strip() or None

                    headless = bool(getattr(self, "var_ship_headless", self.var_headless).get())
                    # 仅用于日志提示，便于你在有头模式下肉眼确认流程
                    self.log(f"[SHIP] 浏览器模式：{'无头' if headless else '有头'}")
                    timeout_sec = int(self.settings.get("timeout_sec", 45) or 45)

                    orders = []
                    for t in ts:
                        orders.append({
                            "order_no": t["order_no"],
                            "order_no_display": str(t.get("order_no_display", "") or "").strip(),
                            "sub_order_nos": (t.get("sub_order_nos") if isinstance(t.get("sub_order_nos"), list) else []),
                            "shipments": t.get("shipments") or [],
                            "remark": t.get("remark", "") or "",
                            # 来源/平台：只有【监控推送】才会为 True / 有值
                            "from_purchase_monitor": bool(t.get("from_purchase_monitor")),
                            "purchase_platform": str(t.get("purchase_platform") or "").strip(),
                            # 物流系统：自动上传需要的字段（仅监控推送+闲鱼会填）
                            "syb_auto_upload": False,
                            "syb_latest_template_path": "",
                        })

                    # 调用核心逻辑
                    results = export_order_to_excel(
                        profile_id=profile_id,
                        account_name=acc_name,
                        profile_dir=profile_dir,
                        browser_path=browser_path,
                        headless=headless,
                        timeout_sec=timeout_sec,
                        template_path=Path(template),
                        output_dir=Path(outdir),
                        user_code=user_code,
                        owner_name=owner,
                        orders=orders,
                        on_log=self.log,
                        stop_event=self.ship_stop_event,
                    )

                    # 仅：【监控推送】 + 【闲鱼】 -> 生成【最新模板_YYYYMMDD.xlsx】（用于物流系统上传）
                    _any_found = any(r.get("found") for r in (results or []))
                    try:
                        x_need = [o.get("order_no") for o in (orders or []) if o.get("from_purchase_monitor") and (o.get("purchase_platform") == "xianyu") and o.get("order_no")]
                        if x_need and _any_found:
                            from core.latest_template_builder import build_latest_template_from_ship_excel
                            date_str = time.strftime("%Y%m%d", time.localtime())
                            ship_xlsx = Path(outdir) / f"出货资料_{date_str}.xlsx"
                            out_tpl = Path(outdir) / f"最新模板_{date_str}.xlsx"
                            if not ship_xlsx.exists():
                                self.log(f"[SYB] 未找到出货资料：{ship_xlsx}（跳过生成最新模板）")
                            else:
                                build_latest_template_from_ship_excel(
                                    ship_excel_path=ship_xlsx,
                                    output_path=out_tpl,
                                    order_nos=x_need,
                                    log_fn=self.log,
                                )
                                for o in orders:
                                    if o.get("from_purchase_monitor") and (o.get("purchase_platform") == "xianyu"):
                                        o["syb_auto_upload"] = True
                                        o["syb_latest_template_path"] = str(out_tpl)
                                self.log(f"[SYB] 已生成最新模板：{out_tpl}")
                        elif x_need and not _any_found:
                            self.log("[SYB] 所有订单抓取失败，跳过生成最新模板")
                    except Exception as e:
                        self.log(f"[SYB] 生成最新模板失败：{e}")

                    # 物流系统：自动上传资料/面单（t=3/t=5）
                    try:
                        if hasattr(self, "syb_upload_tab") and self.syb_upload_tab:
                            self.syb_upload_tab.on_ship_results(profile_id, acc_name, orders, results)
                    except Exception as e:
                        self.log(f"[SYB] 手动出货后上传异常：{e}")

                    # 回写任务状态
                    res_by_order = {r.get("order_no"): r for r in (results or [])}
                    for t in ts:
                        r = res_by_order.get(t["order_no"]) or {}
                        if r.get("found"):
                            self.after(0, lambda tid=t["id"], rr=r: self._ship_update_task(
                                tid,
                                status="完成",
                                amount=rr.get("amount", ""),
                                channel=rr.get("channel", ""),
                                exec_code=rr.get("exec_code", ""),
                                error="",
                            ))
                        else:
                            self.after(0, lambda tid=t["id"], rr=r: self._ship_update_task(
                                tid,
                                status="失败",
                                error=rr.get("error", "未知错误"),
                            ))

                if self.ship_stop_event.is_set():
                    self.log("[SHIP] 已停止")
                else:
                    self.log("[SHIP] 全部执行完成")
                    # TG 推送出货完成通知
                    try:
                        _ok = sum(1 for r in (results or []) if r.get("found"))
                        _fail = len(results or []) - _ok
                        if hasattr(self, '_purchase_tg_cmd') and self._purchase_cmd:
                            self._purchase_cmd.notify_export_done(
                                acc_name=acc_name,
                                order_count=len(results or []),
                                success_count=_ok,
                                fail_count=_fail,
                            )
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"[SHIP] 执行异常：{e}")
            finally:
                self.ship_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _start_monitor(self):
        if self.monitoring:
            return
        try:
            conc = int(self.var_conc.get().strip() or "3")
        except Exception:
            messagebox.showerror("错误", "并发必须是数字")
            return
        headless = bool(self.var_headless.get())
        browser_path = self.var_browser.get().strip() if hasattr(self, "var_browser") else self.settings.get("browser_path","")

        # persist settings
        self.settings["concurrency"]=max(1, conc)
        self.settings["headless"]=headless
        self.settings["browser_path"]=browser_path
        save_settings(self.settings)

        # ✅ 关键修复：监控账号集合在"开始监控"时做一次快照（monitor_selected）。
        # 这样用户后续为了执行批量上下架/删除等功能临时勾选/取消勾选账号，
        # 不会导致其他账号监控被停掉。
        # 2026-04-30 新增 excluded_monitor_accounts:settings 內列出的帳號 monitor 會跳過,
        # daemon 可寫此 list 達成「排除某帳號」需求(對齊 user 23:25 「這幾個不運行」)
        excluded = set(self.settings.get("excluded_monitor_accounts", []) or [])
        for s in self.states.values():
            try:
                if s.name in excluded or s.profile_id in excluded:
                    s.monitor_selected = False
                else:
                    s.monitor_selected = bool(getattr(s, "selected", True))
            except Exception:
                pass

        def on_update(pid: str, patch: Dict[str, Any]):
            # v6.2:patch 合併隊列 — 27 帳號 monitor 高頻時不再每次 after(0)
            # 同 pid 的 patch 合併成最新值,80ms 內最多一次主線程 flush
            if not pid or not patch:
                return
            with self._pending_patches_lock:
                merged = self._pending_patches.setdefault(pid, {})
                merged.update(patch)
                need_schedule = self._patch_flush_after is None
                if need_schedule:
                    self._patch_flush_after = True  # 先佔位,after 在 lock 外排
            if need_schedule:
                try:
                    self._patch_flush_after = self.after(80, self._flush_pending_patches)
                except Exception:
                    self._patch_flush_after = None

        def on_log(msg: str):
            try:
                self.log(msg)
            except Exception:
                pass

        # TG Bot + AI 客服对话管理器：复用已在 __init__ 中启动的实例
        conv_mgr = self._conv_mgr

        # 采购 TG Bot 已在 __init__ 中启动，这里直接复用
        purchase_cmd = self._purchase_cmd
        if purchase_cmd:
            pass  # purchase_tab 已移除，采购功能由 purchase_ship_tab 接管

        # v6.0.83:監控優先走純 HTTP。v6.1:加 Playwright fallback
        # (同事 v6.0.82 cookie cache 不齊全時走純 HTTP 會卡死,fallback 用舊 monitor 安全)
        # settings.use_pure_http_monitor:預設 True;設 False 強制走舊 Playwright
        use_pure_http = bool(self.settings.get("use_pure_http_monitor", True))
        if use_pure_http:
            try:
                from core.pure_http_monitor import PureHTTPMonitor
                self.log("[MON] 使用純 HTTP monitor(60s± 輪詢 + 即時 forum push)")
                self.mon = PureHTTPMonitor(
                    base_dir=BASE_DIR,
                    concurrency=conc,
                    timeout_sec=int(self.settings.get("timeout_sec", 15)),
                    headless=headless,
                    browser_path=browser_path,
                    on_update=on_update,
                    on_log=on_log,
                    conv_manager=conv_mgr,
                    purchase_cmd=purchase_cmd,
                    manage_bot=self._manage_tg_bot,
                    owner_chat_id=str(self.settings.get("tg_chat_id", "")),
                )
            except Exception as _e_purehttp:
                self.log(
                    f"[MON] 純 HTTP monitor 初始化失敗({_e_purehttp}),"
                    f"fallback 用 Playwright MonitorManager"
                )
                self.mon = None  # 走下面 fallback
        else:
            self.mon = None

        if self.mon is None:
            # Fallback:用舊 Playwright MonitorManager(同事 v6.0.82 行為)
            self.log("[MON] 使用 Playwright MonitorManager(舊模式)")
            self.mon = MonitorManager(
                base_dir=BASE_DIR,
                concurrency=conc,
                timeout_sec=int(self.settings.get("timeout_sec", 15)),
                headless=headless,
                browser_path=browser_path,
                on_update=on_update,
                on_log=on_log,
                conv_manager=conv_mgr,
                purchase_cmd=purchase_cmd,
                manage_bot=self._manage_tg_bot,
                owner_chat_id=str(self.settings.get("tg_chat_id", "")),
            )
        self.mon.set_accounts(list(self.states.values()))

        # 让 ConversationManager 能通知 Monitor 重置 IM 计数
        if conv_mgr is not None:
            conv_mgr.monitor = self.mon

        # 采购指令处理器：注入监控账号列表
        if purchase_cmd is not None:
            purchase_cmd._monitor_accounts = self.mon._states

        # 如果批量任务已在运行：自动暂停这些 profile 的监控（避免同一 profile 被同时占用）
        try:
            if self.merch_running and getattr(self, "_batch_hold_pids", None) and self.mon is not None:
                for _pid in list(self._batch_hold_pids):
                    asyncio.run_coroutine_threadsafe(self.mon.set_hold(_pid, True, reason="batch"), self.loop)
                self.log("[MON] 批量任务正在运行：已自动暂停相关账号的监控")
        except Exception:
            pass

        async def _runner():
            await self.mon.run_forever()

        self.monitoring = True
        self.log("[MON] starting...")
        self.mon_task = asyncio.run_coroutine_threadsafe(_runner(), self.loop)
        # v6.0.69:UI 反饋 — 開始監控按鈕變灰「監控中...」,停止按鈕保持紅色可點
        try:
            if hasattr(self, "btn_start_mon"):
                self.btn_start_mon.configure(state="disabled", text="监控中...")
            if hasattr(self, "btn_stop_mon"):
                self.btn_stop_mon.configure(state="normal", text="停止")
        except Exception:
            pass

    def _stop_monitor(self):
        if not self.monitoring:
            return
        self.monitoring = False
        self.log("[MON] stop requested")
        # v6.0.69:UI 反饋 — 停止按鈕立刻顯示「停止中...」(等當前輪詢結束可能要幾秒)
        try:
            if hasattr(self, "btn_stop_mon"):
                self.btn_stop_mon.configure(state="disabled", text="停止中...")
        except Exception:
            pass
        if self.mon:
            async def _stop():
                await self.mon.stop()
            future = asyncio.run_coroutine_threadsafe(_stop(), self.loop)
            # 異步完成後在 UI thread 更新按鈕文字
            def _on_stopped(_fut):
                def _apply():
                    try:
                        if hasattr(self, "btn_start_mon"):
                            self.btn_start_mon.configure(state="normal", text="开始监控")
                        if hasattr(self, "btn_stop_mon"):
                            self.btn_stop_mon.configure(state="disabled", text="已停止 ✓")
                        self.log("[MON] stopped")
                    except Exception:
                        pass
                try:
                    self.after(0, _apply)
                except Exception:
                    pass
            future.add_done_callback(_on_stopped)
        else:
            # 沒 monitor instance 直接更新 UI
            try:
                if hasattr(self, "btn_start_mon"):
                    self.btn_start_mon.configure(state="normal", text="开始监控")
                if hasattr(self, "btn_stop_mon"):
                    self.btn_stop_mon.configure(state="disabled", text="已停止 ✓")
            except Exception:
                pass

if __name__ == "__main__":
    import traceback as _tb

    # 全局 Tkinter 回调异常处理：防止任何未捕获异常导致 mainloop 崩溃（闪退）
    def _tk_exception_handler(exc_type, exc_value, exc_tb):
        try:
            msg = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
            sys.stderr.write(f"[TK-SAFE] 捕获未处理异常（已阻止闪退）:\n{msg}\n")
        except Exception:
            pass

    app = App()

    # ── 啟動本機 API server(2026-04-29 v6.0.46,給 daemon agent 用,127.0.0.1 only) ──
    # 預設 False — 只有用戶手動在 settings.json 改 api_server_enabled=true 才啟動
    # 這樣推送時其他用戶不受影響(他們沒 daemon agent)
    # 三層 kill switch:settings.api_server_enabled / 啟動失敗 try/except / token 不存在
    try:
        if app.settings.get("api_server_enabled", False):
            from core import api_server as _api_srv
            _api_srv.attach(app)
            _api_srv.start_server(port=int(app.settings.get("api_port", 7777)))
            try:
                app.log(f"[API] server started on 127.0.0.1:{app.settings.get('api_port', 7777)}")
            except Exception:
                pass
    except Exception as _api_e:
        try:
            app.log(f"[API] start failed (skipped, GUI unaffected): {_api_e}")
        except Exception:
            pass

    # ── 启动提醒：Cookie 不可用时列出具体账号（跳过未勾选的停权账号） ──
    def _startup_cookie_check():
        try:
            from pathlib import Path
            # v6.1.35:改用 load_cookie_cache 檢查 flat cookies + wssid
            # 修「監控正常但 GUI 永遠提示 cookie 不可用」死循環:
            #   load_raw_cookies 只看 raw_cookies 欄位(Playwright 提取才有),
            #   monitor / D1 對齊只寫 flat cookies,raw_cookies 永遠不會被補上
            #   → GUI 永遠誤判,跟實際監控狀態無關
            from core.cookie_store import load_cookie_cache
            _root = Path(__file__).resolve().parent
            _total = 0
            _missing_names = []
            for _s in app.states.values():
                if not getattr(_s, "selected", True):
                    continue  # 未勾选（停权等）跳过
                _total += 1
                _pdir = _root / "profiles" / _s.profile_id
                cookies, wssid, _saved_at = load_cookie_cache(_pdir, max_age=float("inf"))
                # 有 flat cookies 就算可用(monitor / 自動刊登 / D1 對齊全部走 flat path)
                if not cookies:
                    _missing_names.append(_s.name)
            if _missing_names:
                _list_str = "、".join(_missing_names[:15])
                if len(_missing_names) > 15:
                    _list_str += f"\n...等共 {len(_missing_names)} 个"
                from tkinter import messagebox
                messagebox.showinfo(
                    "监控提醒",
                    f"{len(_missing_names)}/{_total} 个活跃账号的 Cookie 缓存不可用：\n\n"
                    f"{_list_str}\n\n"
                    "请启动监控，等待巡检完成后再使用刊登功能。\n"
                    "（监控巡检成功后自动缓存 Cookie，之后无需额外操作）"
                )
        except Exception:
            pass

    app.after(500, _startup_cookie_check)

    # 2026-05-01 v6.0.50: auto-start 預設改 False(user 要求自己決定何時啟動)。
    # daemon 要主動啟動 → 走 /api/monitor/start 端點(已實作),不靠這個。
    # 想自動 → settings.json 加 "auto_start_monitor": true 即可。
    def _auto_start_monitor():
        try:
            if not app.settings.get("auto_start_monitor", False):
                return
            if getattr(app, "monitoring", False):
                return  # 已啟動,不重複
            if not hasattr(app, "var_conc"):
                return  # GUI tab 還沒 build,跳過(理論上 3 秒夠)
            app._start_monitor()
            try:
                app.log("[auto-start] monitor 已自動啟動(settings.auto_start_monitor=true)")
            except Exception:
                pass
        except Exception as _e:
            try:
                app.log(f"[auto-start] 失敗:{_e}")
            except Exception:
                pass
    app.after(3000, _auto_start_monitor)

    # Tk.report_callback_exception 会在 after() / bind() 回调抛异常时被调用
    # 默认行为是打印到 stderr 然后继续，但某些情况下会导致 mainloop 退出
    app.report_callback_exception = _tk_exception_handler

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    except Exception as _main_err:
        try:
            sys.stderr.write(f"[MAIN] mainloop 异常（已阻止闪退）: {_main_err}\n")
        except Exception:
            pass
