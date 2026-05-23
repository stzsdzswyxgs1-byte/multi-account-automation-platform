"""自动更新器 — 后台常驻，定期检查并应用更新

用法：
    pythonw updater.pyw          # 后台运行（无窗口）
    python  updater.pyw          # 前台运行（可看日志）

工作流程：
    1. 每 5 分钟检查 Worker 上的版本号（网络故障时自动退避到 30 分钟）
    2. 发现新版本 → 下载 zip
    3. 关闭正在运行的 app.py
    4. 解压覆盖代码文件（不动配置文件）
    5. 通过 TG Manage Bot 通知用户
    6. 等待用户手动重启（或自动重启）
"""
from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -- SSL 容错 monkey-patch --
import time as _time
_orig_get = requests.get
_orig_post = requests.post

_RETRY_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
)

def _wrap(orig):
    def wrapper(*a, **kw):
        last_err = None
        for attempt in range(2):          # 2次 (原3次)，减少VPN故障时的挂起时间
            try:
                return orig(*a, **kw)
            except _RETRY_ERRORS as e:
                last_err = e
                if attempt < 1:
                    _time.sleep(3)        # 固定3秒（原3s/6s递增）
        raise last_err
    return wrapper

requests.get = _wrap(_orig_get)
requests.post = _wrap(_orig_post)

# ---------- 日志 ----------

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "updater.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("updater")

# ---------- 常量 ----------

CHECK_INTERVAL = 300         # 检查间隔（秒）— 5分钟轮询一次，节省云端资源
VERSION_FILE = "current_version.txt"
CONFIG_FILE = "tg_relay_config.json"
HEARTBEAT_FILE = "updater_heartbeat.txt"
RESTARTING_FILE = "updater_restarting.txt"  # 自更新期间存在；app.py 见到就跳过拉起，避免抢文件

# 项目根目录
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# pythonw 完整路径（用于 bat 脚本重启自身）
def _get_pythonw_path() -> str:
    """获取 pythonw.exe 的完整路径，确保 bat 重启脚本不依赖 PATH。"""
    exe_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(exe_dir, "pythonw.exe")
    if os.path.isfile(pythonw):
        return pythonw
    # 回退：如果当前就是 pythonw 则直接用
    if "pythonw" in sys.executable.lower():
        return sys.executable
    # 最后回退：用 sys.executable（可能有控制台窗口）
    return sys.executable

PYTHONW_PATH = _get_pythonw_path()


# ---------- 单例锁 ----------

_LOCK_FILE = os.path.join(ROOT_DIR, "updater.lock")

def _acquire_singleton() -> bool:
    """确保只有一个 updater 实例运行。
    使用 O_CREAT|O_EXCL 原子创建 lock 文件防止竞争。
    如果已有 lock 且进程还活着，返回 False。"""
    my_pid = str(os.getpid())

    # 尝试原子创建（O_EXCL：文件已存在则失败，防止 TOCTOU 竞争）
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, my_pid.encode())
        os.close(fd)
        return True
    except (FileExistsError, OSError):
        pass  # lock 文件已存在，检查持有者

    # lock 文件已存在 → 检查持有进程是否还活着
    try:
        old_pid = int(Path(_LOCK_FILE).read_text().strip())
        if old_pid == os.getpid():
            return True  # 是自己
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, old_pid)
        if handle:
            kernel32.CloseHandle(handle)
            log.warning("另一个 updater 已在运行 (PID=%d)，本实例退出", old_pid)
            return False
        # handle=0 说明进程已死，抢锁
    except Exception as e:
        log.warning("检查旧 updater 进程失败 (将尝试接管锁): %s", e)

    # 旧进程已死或 lock 损坏 → 删除后重新原子创建
    try:
        os.remove(_LOCK_FILE)
    except Exception:
        pass
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, my_pid.encode())
        os.close(fd)
        return True
    except (FileExistsError, OSError):
        # 极端情况：另一个实例在我们删除和创建之间抢先创建了
        log.warning("lock 竞争失败，本实例退出")
        return False


def _release_singleton():
    """释放单例锁。"""
    try:
        if os.path.isfile(_LOCK_FILE):
            cur = Path(_LOCK_FILE).read_text().strip()
            if cur == str(os.getpid()):
                os.remove(_LOCK_FILE)
    except Exception:
        pass


# ---------- 心跳 ----------

def _write_heartbeat():
    """写入心跳文件，记录最后一次检查的时间和 PID。"""
    try:
        hb_path = os.path.join(ROOT_DIR, HEARTBEAT_FILE)
        Path(hb_path).write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}|PID={os.getpid()}",
            encoding="utf-8",
        )
    except Exception:
        pass


# ---------- 配置读取 ----------

def load_config() -> dict:
    """读取 tg_relay_config.json。"""
    p = os.path.join(ROOT_DIR, CONFIG_FILE)
    if not os.path.exists(p):
        log.error("找不到 %s", p)
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def get_worker_url(cfg: dict) -> str:
    return cfg.get("worker_url", "").rstrip("/")


def get_api_key(cfg: dict) -> str:
    return cfg.get("api_key", "")


# ---------- 版本管理 ----------

def get_current_version() -> str:
    """读取本地当前版本号。"""
    p = os.path.join(ROOT_DIR, VERSION_FILE)
    if os.path.exists(p):
        return Path(p).read_text(encoding="utf-8").strip()
    return "0.0.0"


def save_current_version(version: str):
    """保存当前版本号到本地。"""
    p = os.path.join(ROOT_DIR, VERSION_FILE)
    Path(p).write_text(version, encoding="utf-8")


# ---------- 检查更新 ----------

_CONN_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
)


_LOGGED_USER_ID_ONCE = False


def _read_self_user_id() -> str:
    """v6.1:取本機 user_id 給 Worker 識別,供 target_users 過濾。共用 core.instance_id 邏輯。"""
    global _LOGGED_USER_ID_ONCE
    try:
        # 嘗試用 core.instance_id 共用模組(跟 app.py title 顯示用同一個)
        import sys as _sys
        if ROOT_DIR not in _sys.path:
            _sys.path.insert(0, ROOT_DIR)
        from core.instance_id import get_instance_id
        uid, source = get_instance_id(ROOT_DIR)
    except Exception:
        # fallback:core 模組不在(可能正在更新中)→ 用 hostname 兜底
        try:
            uid = (os.environ.get("COMPUTERNAME") or "").strip()
            source = "env.COMPUTERNAME"
            if not uid:
                import socket as _sock
                uid = _sock.gethostname()
                source = "socket.gethostname"
        except Exception:
            uid, source = "", "none"

    if not _LOGGED_USER_ID_ONCE and uid:
        log.info("[updater] 本機 user_id = %r (來源: %s) — target 推送用這個 ID", uid, source)
        _LOGGED_USER_ID_ONCE = True

    return uid


def check_update(worker_url: str, api_key: str) -> dict | None:
    """检查 Worker 上是否有新版本。
    返回版本信息 dict、None（无更新）。连接失败抛出异常。

    v6.1:帶 user_id 給 Worker,配合 target_users 機制做選擇性推送。"""
    params = {"key": api_key}
    user_id = _read_self_user_id()
    if user_id:
        params["user_id"] = user_id
    r = requests.get(
        f"{worker_url}/update/check",
        params=params,
        timeout=(5, 10),          # (连接5s, 读取10s)，VPN断时快速失败
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("ok") and data.get("has_update"):
        return data
    return None


# ---------- 下载更新包 ----------

def download_update(worker_url: str, api_key: str) -> bytes | None:
    """下载 zip 更新包，返回字节或 None。"""
    try:
        r = requests.get(
            f"{worker_url}/update/download",
            params={"key": api_key},
            timeout=(10, 120),      # 连接10s, 读取120s（zip包可能较大）
        )
        if r.status_code == 200:
            log.info("下载完成: %.1f KB", len(r.content) / 1024)
            return r.content
        log.error("下载失败: %s", r.status_code)
        return None
    except Exception as e:
        log.error("下载异常: %s", e)
        return None


# ---------- 关闭 app.py 进程 ----------

def kill_app() -> bool:
    """关闭正在运行的 app.py 进程及其 Chrome 子进程。"""
    import subprocess
    try:
        # 1) 关闭运行 app.py 的 python 进程
        r = subprocess.run(
            'wmic process where "commandline like \'%app.py%\' and name like \'%python%\'" get processid /value',
            capture_output=True, text=True, shell=True, timeout=10,
        )
        pids = [line.split("=")[1].strip() for line in r.stdout.splitlines() if "ProcessId=" in line and line.split("=")[1].strip()]
        for pid in pids:
            log.info("关闭进程: PID=%s", pid)
            subprocess.run(f"taskkill /F /PID {pid}", shell=True, timeout=10)

        # 2) 关闭 Playwright 启动的 Chrome 进程（带 --remote-debugging-pipe 参数的）
        try:
            subprocess.run(
                'wmic process where "name like \'%chrome%\' and commandline like \'%remote-debugging-pipe%\'" call terminate',
                shell=True, timeout=10, capture_output=True,
            )
        except Exception:
            pass

        if pids:
            import time; time.sleep(2)
        return bool(pids)
    except Exception as e:
        log.warning("kill_app 失败: %s", e)
        return False


# ---------- 解压覆盖 ----------

_UPDATE_TMP_SUFFIX = ".update_tmp"


def apply_update(zip_data: bytes) -> bool:
    """v6.1.64:Atomic two-phase commit。

    Phase 0: 解析 zip + testzip() CRC 校驗(下載損壞立即偵測)
    Phase 1: 全部寫到 `{final_path}.update_tmp`(原檔不動)
    Phase 2: phase 1 全 OK → 依序 os.replace() 原子覆蓋
    任一步失敗 → cleanup 所有 .update_tmp → return False(下輪重試)

    保證 disk 要嘛全部新版,要嘛全部舊版,不會 partial state。

    特殊處理:
    - updater.pyw 自身跳過(正在運行,由 _self_update 單獨處理)
    - cat_attrs_cache.json 走 merge + atomic(_merge_cat_attrs_cache_atomic)
    """
    # ===== Phase 0: 解析 zip + CRC 校驗 =====
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
    except zipfile.BadZipFile as e:
        log.error("zip 損壞(BadZipFile): %s,本次跳過下輪重試", e)
        return False
    except Exception as e:
        log.error("zip 解析異常: %s,本次跳過下輪重試", e)
        return False

    try:
        bad = zf.testzip()
    except Exception as e:
        log.error("zip CRC 校驗異常: %s,本次跳過下輪重試", e)
        return False
    if bad is not None:
        log.error("zip CRC 校驗失敗(內檔損壞): %s,本次跳過下輪重試", bad)
        return False

    # ===== Phase 1: 全部寫到 .update_tmp =====
    tmp_pairs: list = []  # [(tmp_path, final_path), ...]
    cat_attrs_pending = None  # bytes,merge 模式留到 phase 2

    def _cleanup_tmps():
        for tp, _ in tmp_pairs:
            try:
                if os.path.exists(tp):
                    os.unlink(tp)
            except Exception:
                pass

    try:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            # 跳過 updater 自身(正在運行,由 _self_update 處理)
            if name == "updater.pyw":
                continue
            # cat_attrs_cache.json:merge 模式,先讀 bytes 留到 phase 2
            if name == "cat_attrs_cache.json":
                try:
                    cat_attrs_pending = zf.read(name)
                except Exception as e:
                    log.error("讀取 zip 內 cat_attrs_cache.json 失敗: %s,放棄本次更新", e)
                    _cleanup_tmps()
                    return False
                continue

            final_path = os.path.join(ROOT_DIR, name)
            tmp_path = final_path + _UPDATE_TMP_SUFFIX
            try:
                os.makedirs(os.path.dirname(final_path), exist_ok=True)
            except Exception as e:
                log.error("Phase 1 創建目錄失敗 %s: %s,放棄本次更新",
                          os.path.dirname(final_path), e)
                _cleanup_tmps()
                return False

            # 沿用既有 _write_file_with_retry(寫單檔 + PermissionError retry)
            # dest 傳 tmp_path = 寫到 .update_tmp,原檔完全不動
            if not _write_file_with_retry(zf, name, tmp_path):
                log.error("Phase 1 失敗:%s 寫 .update_tmp 失敗,放棄本次更新(下輪重試)", name)
                _cleanup_tmps()
                return False
            tmp_pairs.append((tmp_path, final_path))

        # ===== Phase 2: phase 1 全 OK → atomic replace =====
        replaced_count = 0
        for tmp_path, final_path in tmp_pairs:
            try:
                os.replace(tmp_path, final_path)  # atomic on Windows/Linux 同分區
                replaced_count += 1
            except Exception as e:
                # Phase 2 fail 極罕見(phase 1 已驗證可寫)
                # 此時 disk 已部分新版部分舊版 — 唯一可能 partial state
                # log critical level 並回 False,下輪 updater 會再嘗試把剩下的覆蓋
                log.error(
                    "[CRITICAL] Phase 2 atomic replace 失敗 %s: %s "
                    "(已 replace %d/%d,剩餘 .update_tmp 保留供下輪重試)",
                    final_path, e, replaced_count, len(tmp_pairs)
                )
                # 不 cleanup 剩餘 tmp(下輪 updater 啟動 cleanup_stale 會處理,
                #  或者下輪 download 新 zip 後 replace 才會成功)
                return False

        # cat_attrs_cache.json merge(走 atomic 變體)
        if cat_attrs_pending is not None:
            _merge_cat_attrs_cache_atomic(cat_attrs_pending)

        log.info("解壓完成(atomic):%d 個檔案", len(tmp_pairs))
        return True
    except Exception as e:
        log.error("apply_update 異常: %s,放棄本次更新", e)
        _cleanup_tmps()
        return False


def _merge_cat_attrs_cache(zf, name: str):
    """[LEGACY] 已被 _merge_cat_attrs_cache_atomic 取代(v6.1.64 起 apply_update 改 atomic)。
    保留只為向後兼容,沒被任何地方呼叫。"""
    dest = os.path.join(ROOT_DIR, name)
    # 读取远程版本
    try:
        remote = json.loads(zf.read(name).decode("utf-8"))
    except Exception as e:
        log.warning("读取远程 %s 失败: %s", name, e)
        return
    # 读取本地版本
    local = {}
    if os.path.exists(dest):
        try:
            with open(dest, "r", encoding="utf-8") as f:
                local = json.load(f)
        except Exception:
            pass
    # 合并: 以本地为基础，远程补充（远程覆盖同 key 的值）
    merged = {**local, **remote}
    # 本地有但远程没有的也保留（已经在 local 里了）
    try:
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        log.info("cat_attrs_cache 合并完成: 本地=%d + 远程=%d -> 合并=%d",
                 len(local), len(remote), len(merged))
    except Exception as e:
        log.warning("写入合并后的 %s 失败: %s", name, e)


def _merge_cat_attrs_cache_atomic(remote_bytes: bytes):
    """v6.1.64:cat_attrs_cache.json atomic merge。

    用 bytes 輸入(不依賴 zf,因為呼叫時 zf 可能已關閉)。
    寫 .update_tmp + os.replace 原子覆蓋,失敗則清 .update_tmp。
    merge fail(JSON decode 等)不影響整體 update(只 log warning)。
    """
    dest = os.path.join(ROOT_DIR, "cat_attrs_cache.json")
    tmp = dest + _UPDATE_TMP_SUFFIX
    try:
        remote = json.loads(remote_bytes.decode("utf-8"))
    except Exception as e:
        log.warning("解析遠端 cat_attrs_cache 失敗: %s,跳過 merge", e)
        return
    local = {}
    if os.path.exists(dest):
        try:
            with open(dest, "r", encoding="utf-8") as f:
                local = json.load(f)
        except Exception:
            pass
    # 合併:本地為基礎,遠端覆蓋同 key
    merged = {**local, **remote}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)  # atomic
        log.info("cat_attrs_cache 合併完成(atomic): 本地=%d + 遠端=%d -> 合併=%d",
                 len(local), len(remote), len(merged))
    except Exception as e:
        log.warning("cat_attrs_cache atomic 寫入失敗: %s", e)
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _write_file_with_retry(zf, name: str, dest: str,
                           retries: int = 3) -> bool:
    """写入单个文件，遇到文件锁重试。

    v6.1.64:apply_update 改 atomic 後,dest 通常傳 `.update_tmp` 路徑,
    原檔不會被觸碰直到 phase 2 os.replace。
    """
    for attempt in range(retries):
        try:
            with zf.open(name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            return True
        except PermissionError:
            log.warning("文件被占用，重试 (%d/%d): %s",
                        attempt + 1, retries, name)
            time.sleep(2)
        except Exception as e:
            log.error("写入失败 %s: %s", name, e)
            return False
    return False


# ---------- v6.1.64 啟動清理 .update_tmp 殘渣 ----------

# 避開不該掃的目錄(節省 IO + 避免清到別人的同名檔)
_CLEANUP_SKIP_DIRS = {
    "python_3.12.7",   # 內嵌 Python runtime,有自己的 tmp
    "profiles",        # Chrome profiles,有自己的 tmp
    "__pycache__",     # Python 編譯 cache
    ".wrangler",       # cloudflare cli
    "node_modules",
    ".git",
}


def _cleanup_stale_update_tmps() -> int:
    """掃 ROOT_DIR 所有 .update_tmp 殘渣,清掉。

    可能殘留情境:
    - Phase 1 寫到一半 updater/系統被 kill → .update_tmp 留下
    - Phase 2 中途 fail → 部分 .update_tmp 留下(下輪 download 新 zip 會重寫)

    回傳清掉幾個。
    """
    cleaned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(ROOT_DIR):
            # 過濾掉不該進的子目錄(in-place 修改 dirnames 讓 os.walk 不下去)
            dirnames[:] = [d for d in dirnames if d not in _CLEANUP_SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(_UPDATE_TMP_SUFFIX):
                    full = os.path.join(dirpath, fn)
                    try:
                        os.unlink(full)
                        cleaned += 1
                    except Exception as e:
                        log.warning("清理 .update_tmp 失敗 %s: %s", full, e)
    except Exception as e:
        log.warning("掃描 .update_tmp 異常: %s", e)
    if cleaned:
        log.info("清理 %d 個 .update_tmp 殘渣(上次 update 中斷遺留)", cleaned)
    return cleaned


# ---------- TG 通知 ----------

def notify_tg(version: str, changelog: str):
    """通过 Manage Bot 通知当前使用者更新完成（仅发送给 settings.json 的 tg_chat_id）。"""
    try:
        # 读取 manage bot token
        tokens_path = os.path.join(ROOT_DIR, "tg_tokens.json")
        if not os.path.exists(tokens_path):
            log.warning("找不到 tg_tokens.json，跳过通知")
            return
        with open(tokens_path, "r", encoding="utf-8") as f:
            tokens = json.load(f)
        bot_token = tokens.get("manage_bot_token", "")
        if not bot_token:
            log.warning("manage bot token 为空，跳过通知")
            return

        # 读取当前使用者的 tg_chat_id
        settings_path = os.path.join(ROOT_DIR, "settings.json")
        owner_chat_id = ""
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                owner_chat_id = str(settings.get("tg_chat_id", "")).strip()
            except Exception:
                pass
        if not owner_chat_id:
            log.warning("settings.json 中无 tg_chat_id，跳过通知")
            return
    except Exception as e:
        log.error("读取 TG 配置失败: %s", e)
        return

    # 构造消息
    msg = (
        f"🔄【软件已自动更新】\n"
        f"新版本：{version}\n"
    )
    if changelog:
        msg += f"更新内容：{changelog}\n"
    msg += (
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n请双击 run.bat 重新启动软件"
    )

    # 只发送给当前使用者
    _send_tg_msg(bot_token, owner_chat_id, msg)


def _send_tg_msg(token: str, chat_id: str, text: str):
    """发送 TG 消息。"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=(5, 10),        # 连接5s, 读取10s（中国VPN断时快速失败）
        )
    except Exception as e:
        log.warning("TG 通知发送失败 (%s): %s", chat_id, e)


# ---------- 主循环 ----------

def main_loop():
    """主循环：定期检查更新。"""
    # 单例检查
    if not _acquire_singleton():
        log.warning("另一个 updater 实例已在运行，本实例退出")
        return

    # v6.1.64:啟動先清 .update_tmp 殘渣(上次 update 中斷遺留)
    try:
        _cleanup_stale_update_tmps()
    except Exception as e:
        log.warning("啟動清理 .update_tmp 異常(忽略,不影響後續): %s", e)

    cfg = load_config()
    worker_url = get_worker_url(cfg)
    api_key = get_api_key(cfg)

    if not worker_url or not api_key:
        log.error("配置不完整，退出")
        _release_singleton()
        return

    log.info("更新器启动 | worker=%s | PID=%d | pythonw=%s",
             worker_url, os.getpid(), PYTHONW_PATH)
    log.info("当前版本: %s", get_current_version())

    _check_count = 0
    _fail_streak = 0          # 连续失败计数，用于退避
    _MAX_BACKOFF = 1800       # 最大退避 30 分钟（VPN长时间断开时不频繁重试）
    _LONG_OUTAGE = 5          # 连续失败 ≥5 次视为长时间断网，直接用最大退避
    try:
        while True:
            try:
                _check_count += 1
                _write_heartbeat()
                ok = _check_once(worker_url, api_key)
                if ok is False:
                    # 连接失败 → 线性退避（比指数温和，但足够）
                    _fail_streak += 1
                    if _fail_streak >= _LONG_OUTAGE:
                        backoff = _MAX_BACKOFF       # 30分钟
                    else:
                        # 5min → 10min → 15min → 20min
                        backoff = min(CHECK_INTERVAL * _fail_streak,
                                      _MAX_BACKOFF)
                    # 减少日志刷屏：只在第1、3次和之后每10次记录
                    if _fail_streak <= 1 or _fail_streak == 3 or _fail_streak % 10 == 0:
                        log.warning("连续 %d 次连接失败，%d 分钟后重试",
                                    _fail_streak, backoff // 60)
                    time.sleep(backoff)
                    continue
                else:
                    if _fail_streak > 0:
                        log.info("连接恢复 (之前连续失败 %d 次)", _fail_streak)
                    _fail_streak = 0
                # 每 10 次检查（约 50 分钟）输出一条存活日志
                if _check_count % 10 == 0:
                    log.info("updater 存活: 已检查 %d 次, 当前版本=%s, PID=%d",
                             _check_count, get_current_version(), os.getpid())
            except Exception as e:
                log.error("检查异常: %s", e, exc_info=True)
            time.sleep(CHECK_INTERVAL)
    finally:
        _release_singleton()


def _check_once(worker_url: str, api_key: str):
    """单次检查更新。返回 False 表示连接失败（触发退避），None 表示正常无更新。"""
    try:
        info = check_update(worker_url, api_key)
    except _CONN_ERRORS as e:
        log.warning("检查更新失败: %s", e)
        return False    # 连接失败 → 触发退避
    except Exception as e:
        log.warning("检查更新异常: %s", e)
        return False

    if not info:
        return None     # 连接正常，无更新

    remote_ver = info.get("version", "")
    local_ver = get_current_version()

    if remote_ver == local_ver:
        return

    changelog = info.get("changelog", "")
    log.info("发现新版本: %s → %s", local_ver, remote_ver)
    if changelog:
        log.info("更新说明: %s", changelog)

    # 下载更新包
    zip_data = download_update(worker_url, api_key)
    if not zip_data:
        log.error("下载完整包失败，尝试先更新 updater 自身...")
        _try_self_heal(worker_url, api_key)
        return

    # 解压覆盖
    log.info("正在解压更新包 ...")
    if not apply_update(zip_data):
        log.error("解压失败，跳过本次更新")
        return

    # 立即保存版本号（在 pip install 之前，防止其他实例在 pip 期间重复检测）
    save_current_version(remote_ver)
    log.info("版本已更新为: %s", remote_ver)

    # 写信号文件，通知正在运行的 app.py 有新版本
    Path(os.path.join(ROOT_DIR, "update_ready.txt")).write_text(remote_ver, encoding="utf-8")

    # 如果 requirements.txt 有变化，自动安装依赖
    _auto_pip_install()

    # TG 通知：只通知本机使用者（settings.json 的 tg_chat_id）
    notify_tg(remote_ver, changelog)
    log.info("更新完成: %s", remote_ver)

    # 检查 updater.pyw 自身是否需要更新
    _self_update(zip_data)


# ---------- 自修复：下载失败时先更新 updater 自身 ----------

def _try_self_heal(worker_url: str, api_key: str):
    """下载完整包失败时，尝试只下载新版 updater.pyw（几KB），替换自身并重启。"""
    try:
        r = requests.get(
            f"{worker_url}/update/updater",
            params={"key": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            log.warning("下载新版 updater 失败: %s", r.status_code)
            return

        new_content = r.content
        self_path = os.path.join(ROOT_DIR, "updater.pyw")

        # 读取当前内容比较
        try:
            with open(self_path, "rb") as f:
                old_content = f.read()
        except Exception:
            old_content = b""

        if new_content == old_content:
            log.info("updater 已是最新版，无法自修复")
            return

        log.info("下载到新版 updater (%.1f KB)，准备替换重启...",
                 len(new_content) / 1024)

        _do_self_restart(self_path, new_content)

    except Exception as e:
        log.warning("自修复失败: %s", e)


# ---------- 自动安装依赖 ----------

def _auto_pip_install():
    """如果 requirements.txt 存在，自动运行 pip install。"""
    req_path = os.path.join(ROOT_DIR, "requirements.txt")
    if not os.path.exists(req_path):
        return
    log.info("正在安装依赖 (pip install -r requirements.txt) ...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_path,
             "--quiet", "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            log.info("依赖安装完成")
        else:
            log.warning("pip install 返回码 %d: %s",
                        result.returncode, result.stderr[:300])
    except Exception as e:
        log.warning("pip install 失败: %s", e)


# ---------- 更新器自身更新 ----------

def _self_update(zip_data: bytes):
    """检查 zip 中是否包含 updater.pyw，如果有则覆盖自身并重启。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        if "updater.pyw" not in zf.namelist():
            return

        # 读取新版本内容
        new_content = zf.read("updater.pyw")

        # 读取当前自身内容
        self_path = os.path.join(ROOT_DIR, "updater.pyw")
        try:
            with open(self_path, "rb") as f:
                old_content = f.read()
        except Exception:
            old_content = b""

        # 内容相同则跳过
        if new_content == old_content:
            return

        log.info("检测到更新器自身有更新，准备重启...")
        _do_self_restart(self_path, new_content)

    except Exception as e:
        log.warning("更新器自身更新失败: %s", e)


def _do_self_restart(self_path: str, new_content: bytes):
    """统一的自重启逻辑：写临时文件 → 写 restarting 标记 → bat 替换 → 重启。

    关键修复：bat 运行期间 (~5-10s) 写 RESTARTING_FILE，防止 app.py 抢文件拉起新进程。
    bat 完成后删除标记 + 提前 touch 一次心跳，app.py 就不会判定 updater 死亡。
    """
    tmp_path = self_path + ".new"
    with open(tmp_path, "wb") as f:
        f.write(new_content)

    # 写自更新标记（app.py 见到就跳过拉起逻辑）
    restarting_path = os.path.join(ROOT_DIR, RESTARTING_FILE)
    heartbeat_path = os.path.join(ROOT_DIR, HEARTBEAT_FILE)
    try:
        Path(restarting_path).write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}|PID={os.getpid()}",
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("写 restarting 标记失败: %s", e)

    # 释放单例锁（即将退出）
    _release_singleton()

    bat_path = os.path.join(ROOT_DIR, "_updater_restart.bat")
    # 关键修复：
    # 1. bat 先 touch 心跳让 app.py 认为 updater 还活着（抢占冷却窗口）
    # 2. move 失败时多重试几次
    # 3. start 新进程后 touch 一次最新心跳 + 删除 restarting 标记
    bat_content = (
        '@echo off\n'
        'chcp 65001 >nul\n'
        # Step 1: 先续一次心跳，让 app.py 的 _ensure_updater_alive 以为 updater 还活着
        f'echo %date% %time% ^|restart_bat > "{heartbeat_path}"\n'
        'timeout /t 3 /nobreak >nul\n'
        # Step 2: 尝试 move，最多 3 次（文件可能被前个 Python 进程持有短暂时间）
        f'move /Y "{tmp_path}" "{self_path}"\n'
        f'if errorlevel 1 (\n'
        f'  timeout /t 2 /nobreak >nul\n'
        f'  move /Y "{tmp_path}" "{self_path}"\n'
        f')\n'
        f'if errorlevel 1 (\n'
        f'  timeout /t 3 /nobreak >nul\n'
        f'  move /Y "{tmp_path}" "{self_path}"\n'
        f')\n'
        # Step 3: 启动新的 updater
        f'start "" "{PYTHONW_PATH}" "{self_path}"\n'
        # Step 4: 再等一下让新进程起来写心跳/抢锁，然后删除标记
        'timeout /t 5 /nobreak >nul\n'
        f'del "{restarting_path}" 2>nul\n'
        f'del "%~f0"\n'
    )
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    log.info("启动重启脚本 (pythonw=%s)，当前进程即将退出", PYTHONW_PATH)
    subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    os._exit(0)


# ---------- 入口 ----------

if __name__ == "__main__":
    main_loop()
