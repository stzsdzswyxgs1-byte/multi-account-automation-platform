"""
闲鱼检测 — 手机辅助签名模式（最强方案，零限流）

架构：
  1. 局域网手机（192.168.0.105:10102）跑 LSPosed + 修改版闲鱼 APP
  2. PC 调手机 /sign → 拿到 APP 真实签名 (x-sign / x-mini-wua / x-sgext / x-umt)
  3. PC 用 APP 签名直接 GET acs.m.goofish.com（acs.m 完全无 BX 限流）
  4. 速度：~0.4 秒/件，并行 16，零限流

需要手机端配套：
  - 工具包3.0/手机端/LSPosed_v1.9.2_zygisk.zip
  - 工具包3.0/手机端/appsign_patched_v5.apk
  - 一键配置脚本：工具包3.0/手机端/setup_phone.py

只复用 awesome.detail.unit 端点，专做状态检测，不涉及商品采集。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import re
from typing import Callable, Dict, Optional

import requests as _req  # 普通 requests 用于打手机内网

try:
    from curl_cffi import requests as _curl_req
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False
    _curl_req = None

LogFn = Callable[[str], None]

# ── 默认配置（可由 settings.json 覆盖）──
PHONE_IP_DEFAULT = "192.168.0.105"
SIGN_PORT = 10102
APP_DETAIL_API = "mtop.taobao.idle.awesome.detail.unit"
APP_API_VERSION = "1.0"
ACS_BASE = "https://acs.m.goofish.com/gw"
TMPL_TTL = 60  # 模板缓存 60 秒（tianya Form1 行为）

_STATUS_RE = re.compile(r'"itemStatusStr"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]{0,100})"')
_ITEM_STATUS_RE = re.compile(r'"itemStatus"\s*:\s*(\d+)')

# 拍卖检测正则（参考 闲鱼采集0420 的过滤逻辑）
_AUCTION_RES_PHONE = (
    re.compile(r'"itemType"\s*:\s*"detailAuction"'),
    re.compile(r'"auctionDO"\s*:\s*\{[^}]*"auctionId"'),
    re.compile(r'"auctionType"\s*:\s*"(?!b")[^"]+"'),
)


def _is_auction_text_phone(text: str) -> bool:
    if not text:
        return False
    for pat in _AUCTION_RES_PHONE:
        if pat.search(text):
            return True
    return False


def _up_encode(v) -> str:
    """模仿 tianya UrlEncodeToUpper：全字符 URL-encode + uppercase hex"""
    if v is None:
        return ""
    return urllib.parse.quote(str(v), safe="").replace("%20", "+")


def _build_detail_payload(item_id: str) -> str:
    """awesome.detail.unit 极简 payload — 检测专用：needSimpleDetail=True 让 acs.m
    返回最少字段（仅 itemDO.itemStatus + itemStatusStr + title），下载量减半，速度更快。
    """
    return json.dumps({
        "commerceAdPlanId": "",
        "extra": '{"labelIds":"36,35,9,12"}',
        "fishAdCode": "440902",
        "flowVersion": "6.0",
        "gps": "0,0",
        "isOld": False,
        "itemId": str(item_id),
        "latitude": "",
        "longitude": "",
        "needSimpleDetail": True,  # 检测只看状态，不要图片/描述/sellerDO 等大字段
    }, separators=(",", ":"))


def _scan_paths_for(filename: str, max_depth: int = 3) -> Optional[str]:
    """跨盤掃描指定檔名(最多深度 3 層),返回找到的絕對路徑

    v6.0.71:多個版本資料夾(如「工具包4.0」「工具包3.0」)同時存在時,
    自動選版本號最高的(4.0 > 3.0 > ...)。新版本工具包 deploy 時不用手動清舊資料夾。
    """
    import os
    import string
    import re
    # 候選根目錄:所有盤符 + 用戶 Desktop / Downloads
    roots = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if os.path.exists(d):
            roots.append(d)
    try:
        from pathlib import Path
        home = Path.home()
        roots.append(str(home / "Desktop"))
        roots.append(str(home / "Downloads"))
        roots.append(str(home / "Documents"))
    except Exception:
        pass

    matches = []  # v6.0.71:收集所有命中,後面按版本排序

    def _walk(root, depth):
        if depth > max_depth:
            return
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if name.lower() == filename.lower() and os.path.isfile(full):
                    matches.append(full)
                    continue
                if os.path.isdir(full):
                    # 只下鑽有可能含工具的目錄
                    if any(k in name.lower() for k in ("工具", "tool", "platform", "闲鱼", "采集", "android", "sdk")):
                        _walk(full, depth + 1)
        except (PermissionError, OSError):
            pass

    for root in roots:
        _walk(root, 0)

    if not matches:
        return None

    # v6.0.71:按路徑中版本號(X.Y 格式)取最大,讓 4.0 > 3.0
    def _version_score(path: str) -> float:
        # 找路徑裡所有 "X.Y" 數字模式,取最大
        nums = re.findall(r'(\d+\.\d+)', path)
        return max((float(v) for v in nums), default=0.0)

    matches.sort(key=_version_score, reverse=True)
    return matches[0]


def _find_adb() -> Optional[str]:
    """找系统里能用的 adb.exe（必须真的存在，不是 PATH 占位）"""
    import os
    import shutil
    from pathlib import Path
    # 1) settings.json
    try:
        from core.accounts import load_settings
        s = load_settings() or {}
        p = (s.get("adb_path") or "").strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    # 2) PATH 里真的有 adb（用 shutil.which 而不是字符串占位）
    p = shutil.which("adb")
    if p:
        return p
    p = shutil.which("adb.exe")
    if p:
        return p
    # 3) 常见路径
    for c in [
        r"C:/Users/<USER>/Downloads/platform-tools/adb.exe",
        r"C:/platform-tools/adb.exe",
        r"D:/platform-tools/adb.exe",
        r"E:/platform-tools/adb.exe",
        r"C:/Program Files/Android/platform-tools/adb.exe",
        r"C:/Android/platform-tools/adb.exe",
    ]:
        if os.path.exists(c):
            return c
    # 4) 跨盘扫描 platform-tools/adb.exe（同事可能装在 D:/E: 任意位置）
    p = _scan_paths_for("adb.exe", max_depth=4)
    if p:
        return p
    return None


def _adb_discover_phone_ip(log: Optional[LogFn] = None) -> Optional[str]:
    """通过 ADB 自动发现手机 WiFi IP（兼容 USB 连接和 WiFi adb）"""
    import subprocess
    _log = log or (lambda m: None)
    adb = _find_adb()
    if not adb:
        _log("[GF-PHONE] 找不到 adb.exe")
        return None
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                           timeout=8, encoding="utf-8", errors="replace")
        lines = [l for l in r.stdout.split("\n") if "\t" in l and "device" in l]
        devices = [l.split("\t")[0] for l in lines if not l.startswith("List")]
        if not devices:
            _log("[GF-PHONE] adb devices 没找到设备（手机没连/没开 USB 调试）")
            return None
        dev = devices[0]
        _log(f"[GF-PHONE] adb 检测到设备：{dev}")
        # 从 wlan0 取 IPv4
        for cmd in [
            [adb, "-s", dev, "shell", "ip", "-4", "addr", "show", "wlan0"],
            [adb, "-s", dev, "shell", "ifconfig", "wlan0"],
        ]:
            try:
                r2 = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=8, encoding="utf-8", errors="replace")
                for line in r2.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("inet "):
                        ip = line.split("inet ")[1].split("/")[0].split(" ")[0].strip()
                        if ip and ip.count(".") == 3 and not ip.startswith("127."):
                            return ip
            except Exception:
                continue
    except Exception as e:
        _log(f"[GF-PHONE] adb 调用异常：{e}")
    return None


def _adb_run(adb: str, dev: str, *args, timeout: int = 8) -> str:
    """跑一个 adb 命令，返回 stdout（utf-8 解码）"""
    import subprocess
    try:
        r = subprocess.run(
            [adb, "-s", dev] + list(args),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _adb_check_port_listening(adb: str, dev: str, port: int) -> bool:
    """从手机自己 telnet 127.0.0.1:port，确认 AndServer 在跑"""
    out = _adb_run(adb, dev, "shell",
                   f"netstat -tln 2>/dev/null | grep -E ':{port}\\s'")
    return str(port) in out


def _adb_pick_best_device(adb: str, log: LogFn) -> Optional[str]:
    """选最合适的 device：优先选 10102 端口已经在监听的"""
    import subprocess
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                           timeout=8, encoding="utf-8", errors="replace")
        lines = [l for l in r.stdout.split("\n") if "\t" in l and "device" in l]
        devices = [l.split("\t")[0] for l in lines if not l.startswith("List")]
        if not devices:
            return None
        if len(devices) == 1:
            return devices[0]
        # 多设备：优先选 10102 已监听的
        log(f"[GF-PHONE] 多设备检测到：{devices}")
        for dev in devices:
            if _adb_check_port_listening(adb, dev, SIGN_PORT):
                log(f"[GF-PHONE] 选 {dev}（10102 端口在监听）")
                return dev
        # 都没监听：优先选有闲鱼进程的
        for dev in devices:
            pid = _adb_run(adb, dev, "shell", "pidof", "com.taobao.idlefish")
            if pid and pid.isdigit():
                log(f"[GF-PHONE] 选 {dev}（闲鱼 APP 在跑 pid={pid}）")
                return dev
        log(f"[GF-PHONE] 多设备都没 AndServer，默认选第一个 {devices[0]}")
        return devices[0]
    except Exception as e:
        log(f"[GF-PHONE] adb devices 异常：{e}")
        return None


def _adb_setup_port_forward(log: Optional[LogFn] = None) -> Optional[str]:
    """用 adb forward 把手机的 10102 端口转发到 PC 本地。
    返回选中的 device serial（成功）或 None（失败）。
    """
    import subprocess
    _log = log or (lambda m: None)
    adb = _find_adb()
    if not adb:
        return None
    dev = _adb_pick_best_device(adb, _log)
    if not dev:
        return None

    # 设置 forward
    try:
        r2 = subprocess.run(
            [adb, "-s", dev, "forward", f"tcp:{SIGN_PORT}", f"tcp:{SIGN_PORT}"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace")
        if r2.returncode != 0:
            _log(f"[GF-PHONE] adb forward 失败：{r2.stderr or r2.stdout}")
            return None
        _log(f"[GF-PHONE] adb forward 设置：127.0.0.1:{SIGN_PORT} → {dev}:{SIGN_PORT}")
        # 启动 APP（如果没跑）
        _ensure_app_running(adb, dev, _log)
        return dev
    except Exception as e:
        _log(f"[GF-PHONE] adb forward 异常：{e}")
        return None


def _adb_diagnose(adb: str, dev: str, log: LogFn) -> None:
    """连接失败后的全方位诊断"""
    log(f"[GF-PHONE] ─── 手机端诊断 (device={dev}) ───")
    # 1. 闲鱼 APP 进程
    pid = _adb_run(adb, dev, "shell", "pidof", "com.taobao.idlefish")
    log(f"[GF-PHONE] 闲鱼 APP pid: {pid or '没在跑'}")
    # 2. tianya 模块是否装
    pkgs = _adb_run(adb, dev, "shell", "pm", "list", "packages", "com.tianya")
    if "tianya" in pkgs:
        log(f"[GF-PHONE] tianya 模块已装：{pkgs[:200]}")
    else:
        log(f"[GF-PHONE] ✗ tianya 模块没装 (com.tianya.idlefish7920)！")
    # 3. LSPosed 模块状态（如果 root）
    lsp = _adb_run(adb, dev, "shell",
                   "su -c \"sqlite3 /data/adb/lspd/config/modules_config.db "
                   "'SELECT module_pkg_name, enabled FROM modules WHERE module_pkg_name LIKE \\\"%tianya%\\\"'\" 2>/dev/null")
    if lsp:
        log(f"[GF-PHONE] LSPosed 模块状态：{lsp}")
    # 4. ss/netstat 看 10102（不可靠但参考）
    for cmd in [
        ["shell", "ss", "-tln"],
        ["shell", "netstat", "-tln"],
    ]:
        out = _adb_run(adb, dev, *cmd, timeout=5)
        if out:
            for line in out.split("\n"):
                if "10102" in line:
                    log(f"[GF-PHONE] {cmd[1]} 找到 10102：{line.strip()}")
                    break
            else:
                log(f"[GF-PHONE] {cmd[1]} 没找到 10102 端口监听")
            break
    # 5. 给修复指引
    log(f"[GF-PHONE] ─── 排错步骤 ───")
    if not pid or not pid.isdigit():
        log(f"[GF-PHONE] 1) 手机上点开闲鱼 APP")
    if "tianya" not in pkgs:
        log(f"[GF-PHONE] 2) 装 appsign_patched_v5.apk（在工具包3.0/手机端/）")
    log(f"[GF-PHONE] 3) 打开手机 LSPosed → 「模块」→ 启用 com.tianya.idlefish7920")
    log(f"[GF-PHONE] 4) 进模块详情 → 「作用域」→ 勾选「闲鱼」")
    log(f"[GF-PHONE] 5) 强制停止闲鱼 APP（设置 → 应用 → 闲鱼 → 强制停止）→ 重新打开")
    log(f"[GF-PHONE] 6) 在闲鱼 APP 里下拉刷新首页")
    log(f"[GF-PHONE] 7) 验证：用同事的「闲鱼采集器」工具试一下能不能连，能连说明 AndServer 活")


def _ensure_app_running(adb: str, dev: str, log: LogFn) -> None:
    """检查闲鱼 APP 是否运行；没在跑就 monkey 启动它。
    AndServer 是 Xposed 模块在 APP 进程里跑的，APP 不开 → 端口不开。
    """
    import subprocess
    import time
    try:
        r = subprocess.run(
            [adb, "-s", dev, "shell", "pidof", "com.taobao.idlefish"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace")
        pid = (r.stdout or "").strip()
        if pid and pid.isdigit():
            log(f"[GF-PHONE] 闲鱼 APP 已在跑 (pid={pid})")
            return
        log(f"[GF-PHONE] 闲鱼 APP 未运行，自动启动...")
        subprocess.run(
            [adb, "-s", dev, "shell", "monkey", "-p", "com.taobao.idlefish",
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        # 等 APP 起来 + AndServer 启动（一般 3-5 秒）
        log(f"[GF-PHONE] 等 APP 启动 + AndServer 拉起...")
        time.sleep(5)
    except Exception as e:
        log(f"[GF-PHONE] 启动 APP 异常（忽略）：{e}")


def _gather_candidate_ips(log: LogFn) -> list:
    """收集所有候选手机 IP（按优先级）。返回 [(ip, source_desc), ...]"""
    candidates = []
    # 1) settings.json
    try:
        from core.accounts import load_settings
        s = load_settings() or {}
        ip = (s.get("goofish_phone_ip") or "").strip()
        if ip:
            candidates.append((ip, "settings.json"))
    except Exception:
        pass
    # 2) 同事采集工具的 phone_config.py（重要！他们已验证可用的 IP）
    try:
        cfg_path = _scan_paths_for("phone_config.py", max_depth=4)
        if cfg_path:
            from pathlib import Path
            txt = Path(cfg_path).read_text(encoding="utf-8", errors="replace")
            for line in txt.split("\n"):
                if "PHONE_IP" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip("\"'").strip("\"")
                    if " " in val:
                        val = val.split()[0].strip("\"'")
                    if val and val.count(".") == 3:
                        candidates.append((val, f"采集工具 phone_config.py ({cfg_path})"))
                        break
    except Exception:
        pass
    # 3) adb forward → 127.0.0.1
    dev = _adb_setup_port_forward(log)
    if dev:
        candidates.append(("127.0.0.1", f"adb forward → {dev}"))
    # 4) ADB 发现的 WiFi IP
    ip = _adb_discover_phone_ip(lambda m: None)  # 不打日志（已经被 _adb_setup_port_forward 打了）
    if ip:
        candidates.append((ip, "adb 发现手机 WiFi IP"))
    # 5) 默认
    candidates.append((PHONE_IP_DEFAULT, "默认"))
    # 去重（保持顺序）
    seen = set()
    uniq = []
    for ip, src in candidates:
        if ip not in seen:
            seen.add(ip)
            uniq.append((ip, src))
    return uniq


def _test_phone_ip(ip: str, timeout: int = 4):
    """快速测试一个 IP 的 AndServer 是否响应（试 /request）。

    回傳 (ok: bool, reason: str)。失敗時 reason 說明具體原因，
    讓員工自己看到「網段不通 / port 沒開 / AndServer 沒跑」一目了然。
    """
    import requests as _r
    url = f"http://{ip}:{SIGN_PORT}/request?count=1"
    try:
        r = _r.get(url, timeout=timeout)
    except _r.exceptions.ConnectTimeout:
        return False, f"連線超時 {timeout}s（手機關了/休眠?或不在同網段）"
    except _r.exceptions.ReadTimeout:
        return False, f"讀超時 {timeout}s（AndServer 卡住?）"
    except _r.exceptions.ConnectionError as e:
        msg = str(e)
        if "10061" in msg or "Connection refused" in msg or "拒絕" in msg or "拒绝" in msg:
            return False, f"連線被拒（port {SIGN_PORT} 沒開,AndServer 沒在跑?）"
        if "10060" in msg or "Network is unreachable" in msg or "目標電腦拒絕" in msg:
            return False, "網路不通（IP 不在同網段?）"
        if "getaddrinfo failed" in msg or "Name or service not known" in msg:
            return False, "DNS/IP 解析失敗"
        return False, f"連線錯誤:{msg[:80]}"
    except Exception as e:
        return False, f"其他異常:{type(e).__name__}:{str(e)[:60]}"

    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        r.json()
        return True, "OK"
    except Exception:
        body_preview = (r.text or "")[:60].replace("\n", " ")
        return False, f"非 JSON 響應（可能 port 被別的服務佔了）:{body_preview!r}"


def _resolve_phone_ip(log: Optional[LogFn] = None) -> str:
    """实测每个候选 IP，选第一个响应的"""
    _log = log or (lambda m: None)
    candidates = _gather_candidate_ips(_log)
    _log(f"[GF-PHONE] 候选 IP（{len(candidates)} 个）：{[ip for ip, _ in candidates]}")
    for ip, src in candidates:
        ok, reason = _test_phone_ip(ip)
        if ok:
            _log(f"[GF-PHONE] ✓ {ip} 响应正常（来源：{src}）")
            return ip
        else:
            _log(f"[GF-PHONE] ✗ {ip} 不响应（来源：{src}）— {reason}")
    # 全部不响应：返回最高优先级的，让 load() 走完整诊断
    fallback = candidates[0][0] if candidates else PHONE_IP_DEFAULT
    _log(f"[GF-PHONE] ⚠ 所有候选 IP 都没响应，用 {fallback}（让后续诊断告诉你哪台手机有问题）")
    return fallback


class PhoneApiChecker:
    """闲鱼检测器 — 手机辅助签名模式

    与 GoofishApiChecker 相同接口：
      load() -> bool           启动时检查手机连接
      check_item(item_id) -> {status, title} | None
      get_error_summary() -> str
    """

    def __init__(self, log: LogFn = None):
        self._log = log or (lambda m: None)
        self._loaded = False
        self._tmpl: Optional[Dict[str, str]] = None
        self._tmpl_time: float = 0
        self._tmpl_lock = threading.Lock()
        # 主 session 用于打手机 /sign：大连接池，keep-alive 复用 TCP
        self._main_sess = self._mk_session(pool_size=64, for_acs=False)
        self._tl = threading.local()
        self._err_stats: Dict[str, int] = {}
        self._err_lock = threading.Lock()

        self._phone_ip = _resolve_phone_ip(self._log)
        self._base_url = f"http://{self._phone_ip}:{SIGN_PORT}"

    @staticmethod
    def _mk_session(pool_size: int = 16, for_acs: bool = False):
        """for_acs=True 用 curl_cffi（Chrome HTTP 客户端 profile）；False 用 requests（手机内网，HTTP keep-alive）"""
        if for_acs and HAVE_CURL_CFFI:
            return _curl_req.Session(impersonate="chrome131")
        s = _req.Session()
        a = _req.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        s.mount("http://", a)
        s.mount("https://", a)
        # 强制 keep-alive，复用 TCP 连接（手机内网延迟 ms 级，每次握手太亏）
        s.headers.update({"Connection": "keep-alive"})
        return s

    def _acs_sess(self):
        s = getattr(self._tl, "s", None)
        if s is None:
            s = self._mk_session(pool_size=4, for_acs=True)
            self._tl.s = s
        return s

    # ── 加载：直接尝试拿模板（跳过 /test 健康检查）──
    def load(self) -> bool:
        """直接尝试 /request 拿模板。能拿到 = AndServer 活着，否则报错。
        多次重试 + 自动等待，应对 APP 启动慢 / AndServer 还没起来的情况。
        """
        import time
        last_err = None
        # 最多 5 次尝试（含初始 0 秒）
        wait_seq = [0, 3, 5, 8, 12]  # 总共最多等 28 秒
        for attempt, wait in enumerate(wait_seq, 1):
            if wait > 0:
                self._log(f"[GF-PHONE] 第 {attempt}/{len(wait_seq)} 次：等 {wait}s 后重试（上次：{last_err[:80] if last_err else '?'}）")
                time.sleep(wait)
            try:
                self._fetch_tmpl(force=True)
                self._loaded = True
                tmpl = self._tmpl or {}
                self._log(f"[GF-PHONE] ✓ AndServer OK，APP utdid={tmpl.get('x-utdid','')[:20]}... ver={tmpl.get('x-app-ver','')}")
                return True
            except Exception as e:
                last_err = str(e)

        # 全失败：诊断信息
        self._log(f"[GF-PHONE] ✗ 拿不到 APP 模板 ({self._base_url}/request)")
        self._log(f"[GF-PHONE] 最后错误：{last_err}")
        # 用 adb 做全方位诊断
        try:
            adb = _find_adb()
            if adb:
                # 找设备
                import subprocess
                r = subprocess.run([adb, "devices"], capture_output=True,
                                   text=True, timeout=5,
                                   encoding="utf-8", errors="replace")
                lines = [l for l in r.stdout.split("\n") if "\t" in l and "device" in l]
                devices = [l.split("\t")[0] for l in lines if not l.startswith("List")]
                if devices:
                    _adb_diagnose(adb, devices[0], self._log)
        except Exception as e:
            self._log(f"[GF-PHONE] 诊断脚本异常：{e}")
        return False

    def _fetch_tmpl(self, force: bool = False) -> Dict[str, str]:
        """从手机 /request 拿最新 APP header 模板"""
        with self._tmpl_lock:
            if self._tmpl and not force and (time.time() - self._tmpl_time) < TMPL_TTL:
                return self._tmpl
            r = self._main_sess.get(f"{self._base_url}/request?count=3", timeout=6)
            data = r.json()
            raws = data.get("req", [])
            if not raws:
                raise RuntimeError("/request 无 APP 模板，请在手机闲鱼 APP 首页刷新一下")
            raw = raws[0]
            tmpl: Dict[str, str] = {}
            for kv in raw.split(", "):
                i = kv.find("=")
                if i > 0:
                    tmpl[kv[:i]] = urllib.parse.unquote(kv[i + 1:])
            for key in ("x-utdid", "x-devid", "x-appkey", "x-ttid",
                        "x-extdata", "x-app-ver", "x-bx-version",
                        "x-features", "user-agent"):
                if key not in tmpl:
                    raise RuntimeError(f"模板缺字段 {key}")
            self._tmpl = tmpl
            self._tmpl_time = time.time()
            return tmpl

    def _sign(self, api: str, version: str, data_str: str,
              tmpl: Dict[str, str], t: int) -> Dict[str, str]:
        """POST 手机 /sign 拿 x-sign/x-umt/x-sgext/x-mini-wua"""
        fields = [
            ("deviceId", tmpl["x-devid"]),
            ("appKey", tmpl["x-appkey"]),
            ("extdata", tmpl["x-extdata"]),
            ("utdid", tmpl["x-utdid"]),
            ("t", str(t)),
            ("xFeatures", tmpl["x-features"]),
            ("ttid", tmpl["x-ttid"]),
            ("api", api),
            ("v", version),
            ("data", data_str),
            ("lng", "0"),
            ("lat", "0"),
            ("pageName", ""),
            ("pageId", ""),
        ]
        if tmpl.get("x-sid"):
            fields.append(("sid", tmpl["x-sid"]))
        if tmpl.get("x-uid"):
            fields.append(("uid", tmpl["x-uid"]))
        body = "&".join(k + "=" + _up_encode(v) for k, v in fields)
        r = self._main_sess.post(
            f"{self._base_url}/sign", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=8)
        return r.json()

    def _fetch_acs(self, app_api: str, version: str, data_str: str,
                   tmpl: Dict[str, str], signed: Dict[str, str], t: int):
        """GET acs.m.goofish.com（每个 header value URL-encode upper —— tianya 关键）"""
        url = f"{ACS_BASE}/{app_api}/{version}/?data={_up_encode(data_str)}"
        raw_headers = {
            "x-features": tmpl["x-features"],
            "x-extdata": tmpl["x-extdata"],
            "x-sgext": signed["x-sgext"],
            "umid": signed["x-umt"],
            "x-location": "0,0",
            "user-agent": tmpl["user-agent"],
            "x-ttid": tmpl["x-ttid"],
            "x-appkey": tmpl["x-appkey"],
            "x-mini-wua": signed["x-mini-wua"],
            "x-c-traceid": tmpl["x-utdid"] + str(t) + "000000000000",
            "x-app-conf-v": "0",
            "x-app-ver": tmpl["x-app-ver"],
            "x-t": str(t),
            "x-pv": "6.3",
            "x-bx-version": tmpl["x-bx-version"],
            "f-refer": "mtop",
            "x-utdid": tmpl["x-utdid"],
            "x-umt": signed["x-umt"],
            "x-devid": tmpl["x-devid"],
            "x-sign": signed["x-sign"],
        }
        if tmpl.get("x-sid"):
            raw_headers["x-sid"] = tmpl["x-sid"]
        if tmpl.get("x-uid"):
            raw_headers["x-uid"] = tmpl["x-uid"]
        enc_headers = {k: _up_encode(v) for k, v in raw_headers.items()}
        return self._acs_sess().get(url, headers=enc_headers, timeout=15)

    def _record_err(self, kind: str) -> None:
        with self._err_lock:
            self._err_stats[kind] = self._err_stats.get(kind, 0) + 1

    def get_error_summary(self) -> str:
        if not self._err_stats:
            return "无错误"
        items = sorted(self._err_stats.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}={v}" for k, v in items[:8])

    # ── 主入口：检测单个商品 ──
    def check_item(self, item_id: str) -> Optional[Dict]:
        """返回 {status: 在线|卖掉了|已下架|已删除, title: ...} 或 None"""
        if not self._loaded:
            return None

        data_str = _build_detail_payload(item_id)
        try:
            tmpl = self._fetch_tmpl()
        except Exception as e:
            self._record_err(f"tmpl_err:{type(e).__name__}")
            return None

        t = int(time.time())
        try:
            signed = self._sign(APP_DETAIL_API, APP_API_VERSION, data_str, tmpl, t)
        except Exception as e:
            self._record_err(f"sign_err:{type(e).__name__}")
            return None
        if "x-sign" not in signed:
            self._record_err("sign_no_xsign")
            return None

        try:
            r = self._fetch_acs(APP_DETAIL_API, APP_API_VERSION, data_str, tmpl, signed, t)
        except Exception as e:
            self._record_err(f"acs_err:{type(e).__name__}")
            return None

        try:
            txt = r.text
            result = r.json()
        except Exception:
            self._record_err("parse_err")
            return None

        ret = result.get("ret", [])
        ret_str = " ".join(str(x) for x in ret) if ret else ""

        # 已删除
        if "FAIL_BIZ_ITEM_DEL" in ret_str or "ITEM_NOT_FOUND" in ret_str or "NOT_FOUND" in ret_str:
            return {"status": "已删除", "title": ""}

        # 审核中（ITEM_CC）→ 当已下架处理
        if "ITEM_CC" in ret_str:
            return {"status": "已下架", "title": ""}

        # session 过期 → 强制刷新模板，下次重试
        if "SESSION_EXPIRED" in ret_str:
            self._tmpl = None
            self._record_err("session_expired")
            return None

        # 验证码（acs.m 极少出现，但 tianya 有处理）
        if "USER_VALIDATE" in ret_str:
            self._record_err("user_validate")
            return None

        # 成功：从结构化 itemDO 取（不用全文正则——会抓到页面按钮文本）
        if "SUCCESS" in ret_str:
            item_do = (result.get("data") or {}).get("itemDO") or {}
            status_str = str(item_do.get("itemStatusStr") or "")
            title = str(item_do.get("title") or "")[:50]
            try:
                item_status_num = int(item_do.get("itemStatus") or 0)
            except Exception:
                item_status_num = 0
            # 状态归一：itemStatus≠0 强制视为非在售
            if item_status_num != 0 and "已" not in status_str and "卖" not in status_str:
                status_str = "已下架"
            if not status_str:
                status_str = "在线" if item_status_num == 0 else "已下架"
            # 拍卖检测：itemDO 字段直查 + 全文兜底（acs.m 的 needSimpleDetail 可能裁掉部分字段）
            is_auction = (
                str(item_do.get("itemType", "")) == "detailAuction"
                or bool((item_do.get("auctionDO") or {}).get("auctionId") if isinstance(item_do.get("auctionDO"), dict) else False)
                or (str(item_do.get("auctionType", "")) and str(item_do.get("auctionType", "")) != "b")
            )
            if not is_auction:
                # 兜底：用全文正则（appendDO 可能在外层 data 而非 itemDO）
                try:
                    raw_txt = json.dumps(result, ensure_ascii=False)
                except Exception:
                    raw_txt = ""
                is_auction = _is_auction_text_phone(raw_txt)
            if is_auction:
                status_str = "拍卖"
            return {"status": status_str, "title": title}

        # 其他错误
        self._record_err(f"OTHER:{ret_str[:50]}")
        return None
