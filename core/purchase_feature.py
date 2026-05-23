# PATCH_SUB_ORDER_UI_V1 2026-01-16 (sub-order UI + no-merge duplicate guard)
from __future__ import annotations
"""
采购订单绑定 / 监控（闲鱼 + Mercari）

目标：
- 你手动把「采购平台订单号」绑定到「Yahoo账号 + Yahoo订单号」
- 系统自动抓取：采购金额、代付/购买日期、（可选）物流单号
- 轮询监控：一旦出现物流单号 => 企业微信提醒 + 可自动生成「订单获取/出货」任务

说明：
- 采购平台登录依赖 Playwright 持久化浏览器 Profile（只需登录一次，掉线会提醒）
- 本文件是独立功能模块，后续维护只改这里即可；主程序仅做最小接入。
"""
import json
import re
import threading
import subprocess
import time
import queue
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from .client_runtime_compat import sync_playwright, PWTimeoutError, apply_runtime_normalization_sync, get_launch_args, get_ignore_default_args, CHROME_UA, GOOFISH_PROXY_BYPASS

# 复用项目已有工具
from core.accounts import load_settings
from core.profile_lock import detect_chrome_profile_in_use, try_acquire, release


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT_DIR / "purchase_links.json"
PURCHASE_PROFILE_DIR = ROOT_DIR / "profiles" / "purchase_monitor"  # 闲鱼 + Mercari 共用一个 Profile
PURCHASE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _recover_stale_panel_lock(app_log=None, max_age_sec: int = 180) -> bool:
    """清理旧版本遗留的 .panel_lock（不影响账号数据）。

    旧版本在 try_acquire / release 调用错误时，会留下锁文件，导致后续一直提示"本软件占用"。
    本函数仅在：1) 锁文件存在；2) 未检测到 Chrome 正在使用该 Profile 时，才会清理。
    """
    lp = PURCHASE_PROFILE_DIR / ".panel_lock"
    if not lp.exists():
        return False

    # Chrome/Chromium 正在使用时不要清理（避免误删有效锁）
    in_use, _ = detect_chrome_profile_in_use(PURCHASE_PROFILE_DIR)
    if in_use:
        return False

    age = None
    try:
        data = json.loads(lp.read_text(encoding="utf-8"))
        ts = int(data.get("ts", 0) or 0)
        if ts:
            age = int(time.time()) - ts
            if age < 0:
                age = 0
    except Exception:
        age = None

    # 无法解析时也按"可清理"处理（因为已确认没被 Chrome 占用）
    if age is None or age >= int(max_age_sec):
        try:
            lp.unlink()
            if app_log:
                if age is None:
                    app_log("[采购] 检测到遗留锁文件（无法解析），已自动清理。")
                else:
                    app_log(f"[采购] 检测到遗留锁文件（{age}s），已自动清理。")
            return True
        except Exception:
            return False
    return False



def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically to avoid corrupting Chrome preference files."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _ensure_cookie_persistence_hint(user_data_dir: Path) -> None:
    """Best-effort tweak for Xianyu(闲鱼)/Taobao login persistence.

    闲鱼登录通常依赖阿里系跨域 Cookie（goofish.com <-> taobao.com）。
    新建 Profile 的默认隐私设置有时会阻止第三方 Cookie，导致"关掉再开就像没登录"。

    这里做两件事：
    1) 尝试 patch Preferences（若存在）以允许第三方 Cookie / 关闭清理站点数据
    2) 若 Preferences 不存在（第一次启动前），不强行创建（避免破坏 Chrome）。
    """
    pref_path = user_data_dir / "Default" / "Preferences"
    if not pref_path.exists():
        return

    try:
        data = json.loads(pref_path.read_text(encoding="utf-8"))
    except Exception:
        return

    # --- 1) allow 3p cookies (best-effort, keys may differ by Chrome versions) ---
    profile = data.setdefault("profile", {})
    # 常见字段：cookie_controls_mode / block_third_party_cookies
    profile["cookie_controls_mode"] = 0
    profile["block_third_party_cookies"] = False
    profile["third_party_cookie_phaseout"] = 0

    # --- 2) keep site data on exit (best-effort) ---
    browser = data.setdefault("browser", {})
    # 某些版本会把"退出时清理"放在这里（不同版本字段不同，尽量不乱加太深）
    browser.setdefault("clear_data", {}).setdefault("on_exit", {}).setdefault("cookies", False)

    # --- 3) explicit allowlist for goofish/taobao domains (best-effort structure) ---
    cs = profile.setdefault("content_settings", {})
    ex = cs.setdefault("exceptions", {})
    cookies = ex.setdefault("cookies", {})

    def _allow(pattern: str) -> None:
        if pattern in cookies:
            # 若用户自己设置过，别覆盖
            return
        cookies[pattern] = {
            "setting": 1,
            "last_modified": "13273526400000000",  # 任意占位，Chrome 会重写
        }

    _allow("https://[*.]goofish.com,*")
    _allow("https://[*.]taobao.com,*")
    _allow("https://[*.]login.taobao.com,*")

    try:
        _atomic_write_text(pref_path, json.dumps(data, ensure_ascii=False))
    except Exception:
        return

def _get_system_chrome_path(prefer: str = "") -> str:
    """Return system Chrome executable path.

    prefer: optional user-specified path (e.g. from settings UI).
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

    # 1) prefer (UI / caller)
    p = _ok(prefer)
    if p:
        return p

    # 2) settings.json
    try:
        s = load_settings()
        p = _ok(s.get("browser_path", ""))
        if p:
            return p
    except Exception:
        pass

    # 3) common install locations
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




# -----------------------------
# 数据模型
# -----------------------------
@dataclass
class PurchaseLink:
    # 绑定关系（你手动填）
    yahoo_profile_id: str
    yahoo_acc_name: str
    yahoo_order_no: str

    # 副订单编号（最多 5 个）。仅用于显示/备注拼接；实际抓取/检测仍以 yahoo_order_no（主单号）为准。
    sub_order_nos: List[str] = field(default_factory=list)

    # 说明：dataclass 要求「无默认值字段」必须全部放在「有默认值字段」之前。
    # 你当前工程里这几个字段的顺序/默认值在多次补丁合并后可能出现变化，
    # 导致导入时报：TypeError: non-default argument 'platform' follows default argument。
    # 这里给 platform / purchase_order_id 设置默认值，只修复该报错，不改变功能逻辑。
    platform: str = "xianyu"  # "xianyu" | "mercari"
    purchase_order_id: str = ""

    product_name: str = ""
    spec: str = ""
    remark: str = ""        # 备注（订单级/商品级均可用；最终会合并到出货任务的备注）


    # 自动抓取字段
    pay_amount: str = ""      # 代付金额（字符串，保持原始显示更稳）
    pay_dt_raw: str = ""      # 原始时间文本
    pay_mmdd: str = ""        # 4位 mmdd（用于出货任务）

    tracking_no: str = ""     # 物流单号
    last_checked_at: str = "" # ISO

    # 状态
    status: str = "待监控"      # 待监控 / 需登录 / 等待出货 / 已获取单号
    notified_login: bool = False
    notified_tracking: bool = False
    created_ship_task: bool = False

    # 勾选：是否参与监控（你可自由选择）
    watch: bool = True

    error: str = ""

    # 商品图片（每个采购订单 1 张图，自动出货后上传到顺云宝供仓库核对）
    # 路径是 internal cache 路径（output/采购图片/<yahoo_order>_<purchase_order>.<ext>）
    image_path: str = ""
    image_uploaded: bool = False  # True = 已上传到顺云宝（避免重复上传）


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_links_lock = threading.Lock()


def load_links() -> List[PurchaseLink]:
    with _links_lock:
        if not DATA_FILE.exists():
            return []
        try:
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            out: List[PurchaseLink] = []
            fields = set(PurchaseLink.__dataclass_fields__.keys())
            for x in raw if isinstance(raw, list) else []:
                if not isinstance(x, dict):
                    continue
                kwargs = {k: x.get(k) for k in fields if k in x}
                out.append(PurchaseLink(**kwargs))
            return out
        except Exception:
            return []


def save_links(links: List[PurchaseLink]) -> None:
    with _links_lock:
        _atomic_write_json(DATA_FILE, [asdict(x) for x in links])



# -----------------------------
# 解析工具
# -----------------------------
def _to_mmdd(dt_raw: str) -> str:
    """
    支持：
    - 2025-11-30 14:35:26
    - 2025/11/30 14:35
    - 2025年12月15日 21:23
    """
    s = (dt_raw or "").strip()
    if not s:
        return ""
    # 2025-11-30 ...
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        mm = int(m.group(2))
        dd = int(m.group(3))
        return f"{mm:02d}{dd:02d}"
    # 2025年12月15日
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        mm = int(m.group(2))
        dd = int(m.group(3))
        return f"{mm:02d}{dd:02d}"
    return ""


def _clean_amount(s: str) -> str:
    """Normalize amount to a plain number string.
    - Removes commas/Chinese commas.
    - Preserves decimals (e.g., '28.88').
    - Returns '' if nothing numeric is found.
    """
    s = (s or "").strip().replace("，", ",")
    if not s:
        return ""
    m = re.search(r"([0-9][0-9,]*)(\.[0-9]+)?", s)
    if not m:
        return ""
    int_part = m.group(1).replace(",", "")
    dec_part = m.group(2) or ""
    return int_part + dec_part
def _detect_login(platform: str, url: str, body_text: str) -> bool:
    t = (body_text or "")
    u = (url or "")
    if platform == "xianyu":
        # 闲鱼/阿里系登录页常见关键词
        if ("login" in u) or ("passport" in u) or ("taobao.com" in u and "login" in u):
            return True
        if ("登录" in t and ("手机号" in t or "验证码" in t or "短信" in t)) or ("请登录" in t):
            return True
        return False
    if platform == "mercari":
        if "/login" in u or "signin" in u:
            return True
        if ("ログイン" in t and ("メールアドレス" in t or "パスワード" in t)):
            return True
        return False
    return False


def _extract_tracking(text: str) -> str:
    """
    物流单号极其多样，这里做"尽量抓"：
    - 优先 10~20 位数字
    - 其次 字母数字组合 8~30（常见：YTxxxx、SFxxxx、JP 追踪号）
    """
    if not text:
        return ""
    # 数字单号
    m = re.search(r"\b\d{10,20}\b", text)
    if m:
        return m.group(0)
    # 字母数字组合
    m = re.search(r"\b[A-Z0-9]{8,30}\b", text)
    if m:
        return m.group(0)
    return ""


# 闲鱼：只在「收货信息」区块出现时才尝试抓物流单号（避免在"交易成功/已评价/未发货"等页面误抓手机号/订单号）
_XIANYU_COURIERS = [
    "申通", "顺丰", "圆通", "中通", "韵达", "极兔", "京东", "EMS", "邮政", "德邦", "天天", "百世",
    "宅急送", "优速", "安能", "跨越", "DHL", "UPS", "FedEx", "TNT"
]

def _extract_xianyu_tracking(body_text: str) -> str:
    """从闲鱼订单详情页文本中提取物流单号。

    关键约束：
    - **必须**存在「收货信息/收貨信息」区块，才认为页面有物流信息；否则直接返回空字符串。
      这样可以避免在"交易成功/已评价/未发货"等没有物流的页面，把手机号/订单号误当成物流单号。
    - 提取范围只看「收货信息」附近，避免误抓其它数字。
    """
    t = (body_text or "")
    if not t:
        return ""

    key = ""
    if "收货信息" in t:
        key = "收货信息"
    elif "收貨信息" in t:
        key = "收貨信息"
    else:
        return ""

    idx = t.find(key)
    seg = t[idx: idx + 1800]  # 只看收货信息附近，避免误抓其它数字

    # 如果连"物流/快递/运单/复制/快递公司关键词"都没有，就不要继续
    if (("物流" not in seg) and ("快递" not in seg) and ("运单" not in seg) and ("复制" not in seg)
        and not any(k in seg for k in _XIANYU_COURIERS)):
        return ""

    cour_pat = "(?:" + "|".join(map(re.escape, _XIANYU_COURIERS)) + ")"

    # 0) 最稳：常见展示「申通快递 773... 复制」/「申通 773... 复制」
    m = re.search(rf"{cour_pat}(?:快递|物流|速运|快運|快运)?\s*([A-Za-z0-9\-]{{8,40}})\s*复制", seg, flags=re.IGNORECASE)
    if m:
        v = m.group(1).strip().replace(" ", "")
        if not re.fullmatch(r"\d{11}", v):  # 11 位极可能是手机号
            return v

    # 1) 标签直取：物流单号/运单号/快递单号
    for pat in (
        r"(?:物流单号|运单号|快递单号)\s*[:：]?\s*([A-Za-z0-9\-]{8,40})",
        r"(?:物流编号|运单编号)\s*[:：]?\s*([A-Za-z0-9\-]{8,40})",
    ):
        m = re.search(pat, seg)
        if m:
            v = m.group(1).strip().replace(" ", "")
            if not re.fullmatch(r"\d{11}", v):
                return v

    # 2) 按行匹配：只要该行含快递公司关键词（最好带"复制/快递/物流/运单"）
    for line in seg.splitlines():
        line = (line or "").strip()
        if not line:
            continue
        if not any(k in line for k in _XIANYU_COURIERS):
            continue
        # 降低误抓：没有任何"复制/快递/物流/运单"提示的行，跳过（防止地址里出现"EMS"之类极端情况）
        if ("复制" not in line) and ("快递" not in line) and ("物流" not in line) and ("运单" not in line):
            continue
        m = re.search(r"([A-Za-z0-9\-]{8,40})", line)
        if m:
            v = m.group(1).strip().replace(" ", "")
            if not re.fullmatch(r"\d{11}", v):
                return v

    # 3) 兜底：在 seg 内找"快递公司 + 单号"
    m = re.search(rf"{cour_pat}(?:快递|物流|速运|快運|快运)?\s*[:： ]+\s*([A-Za-z0-9\-]{{8,40}})", seg, flags=re.IGNORECASE)
    if m:
        v = m.group(1).strip().replace(" ", "")
        if not re.fullmatch(r"\d{11}", v):
            return v

    return ""



# -----------------------------
# Playwright 抓取
# -----------------------------
def _pw_open_context(headless: bool = True, chrome_path: str = "", no_proxy: bool = False):
    """Playwright 打开 persistent context（用于抓取），复用 PURCHASE_PROFILE_DIR 登录态。"""
    import logging as _logging
    _log = _logging.getLogger("purchase")
    _log.info("[PW] _pw_open_context called: headless=%s", headless)

    # 1) 若用户还开着"登录浏览器"，Chrome 会占用资料目录，Playwright 会失败
    in_use, reason = detect_chrome_profile_in_use(PURCHASE_PROFILE_DIR)
    if in_use:
        raise RuntimeError(f"采购登录资料被占用：{reason}（请先关闭登录浏览器/关闭占用该资料的Chrome）")
    _log.info("[PW] profile not in use, acquiring lock...")

    # 2) 防止本软件内部并发抢占同一资料目录
    ok, reason2 = try_acquire(PURCHASE_PROFILE_DIR, owner="purchase")
    lock_ok = bool(ok)
    if not lock_ok:
        if _recover_stale_panel_lock(max_age_sec=120):
            ok, reason2 = try_acquire(PURCHASE_PROFILE_DIR, owner="purchase")
            lock_ok = bool(ok)

    if not lock_ok:
        raise RuntimeError(
            f"采购登录资料正被本软件占用：{reason2}（如确认未运行采购监控/抓取，请关闭软件后删除：{PURCHASE_PROFILE_DIR / '.panel_lock'}）"
        )
    _log.info("[PW] lock acquired, getting chrome path...")
    exe = _get_system_chrome_path(chrome_path)
    _log.info("[PW] chrome exe=%s, starting playwright...", exe)

    p = sync_playwright().start()
    _log.info("[PW] playwright started, launching context...")
    try:
        _extra_args = [
            "--disable-features=TranslateUI,ThirdPartyCookiesDeprecation,TrackingProtection3pcd,PrivacySandboxSettings4",
            "--window-size=1280,860",
        ]
        if no_proxy:
            _extra_args.append(f"--proxy-bypass-list={GOOFISH_PROXY_BYPASS}")
        _args = get_launch_args(headless=headless, lang="zh-CN", extra=_extra_args)
        _kw_headless = False if headless else False
        kwargs = dict(
            user_data_dir=str(PURCHASE_PROFILE_DIR),
            headless=_kw_headless,
            no_viewport=True,   # 不设 viewport（Patchright 兼容）
            locale="zh-CN",
            accept_downloads=False,
            args=_args,
            ignore_default_args=get_ignore_default_args(headless=headless),
            timeout=30000,
        )
        kwargs["user_agent"] = CHROME_UA
        # 只有在找到系统Chrome时才指定 executable_path
        if exe:
            kwargs["executable_path"] = exe

        ctx = p.chromium.launch_persistent_context(**kwargs)
        _log.info("[PW] context launched OK")

        # 运行时兼容：隐藏 webdriver / headless 特征
        apply_runtime_normalization_sync(ctx)

        return p, ctx, lock_ok
    except Exception:
        # 失败时释放 lock
        try:
            p.stop()
        except Exception:
            pass
        try:
            release(PURCHASE_PROFILE_DIR)
        except Exception:
            pass
        raise



def _ensure_chrome_session_persistence(profile_dir: Path, app_log=None) -> None:
    """修改 Chrome Preferences 让 session cookie 持久化。

    闲鱼的关键登录 cookie (cookie2/_tb_token_/csg/sgcookie 等) 是 session cookie，
    Chrome 默认退出时清除 → 下次打开就要重新登录。

    解决方案：设置 session.restore_on_startup=5（继续上次结束的位置），
    Chrome 会自动保留 session cookie。
    同时启用 third-party cookies（闲鱼登录依赖淘宝跨域 cookie）。

    注意 Chrome restore_on_startup 值含义：
    - 1 = 打开新标签页（不保留 session cookie）
    - 4 = 打开特定 URL
    - 5 = 继续上次结束的位置（保留 session cookie）← 我们要的
    """
    def _diag(msg):
        if app_log:
            try:
                app_log(f"[采购][cookie诊断] {msg}")
            except Exception:
                pass
    import json as _json
    pref_path = profile_dir / "Default" / "Preferences"

    # 诊断：检查 cookie 数据库是否存在 + 大小
    _cookies_db = profile_dir / "Default" / "Network" / "Cookies"
    if _cookies_db.exists():
        _diag(f"Cookies DB 存在: {_cookies_db.stat().st_size} bytes")
        # 数 SQLite 里 goofish/taobao cookie 数量
        try:
            import sqlite3, shutil, tempfile, os as _os
            _tmp_fd, _tmp = tempfile.mkstemp(suffix=".db")
            _os.close(_tmp_fd)
            shutil.copy2(_cookies_db, _tmp)
            _conn = sqlite3.connect(_tmp)
            _cnt = _conn.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%goofish%' OR host_key LIKE '%taobao%'"
            ).fetchone()[0]
            _conn.close()
            _os.unlink(_tmp)
            _diag(f"SQLite 中闲鱼/淘宝 cookie 数量: {_cnt}")
        except Exception as _e:
            _diag(f"读取 SQLite 失败: {_e}")
    else:
        _diag(f"Cookies DB 不存在: {_cookies_db}")

    # 检查 Local State 文件（Chrome 存放加密密钥的地方）
    _local_state = profile_dir / "Local State"
    if _local_state.exists():
        _diag(f"Local State 存在: {_local_state.stat().st_size} bytes")
    else:
        _diag(f"Local State 不存在 - profile 损坏或第一次启动")

    if not pref_path.exists():
        _diag(f"Preferences 不存在，将创建初始配置")
        try:
            pref_path.parent.mkdir(parents=True, exist_ok=True)
            initial = {
                "session": {"restore_on_startup": 5, "startup_urls": []},
                "profile": {
                    "exit_type": "Normal",
                    "exited_cleanly": True,
                    "cookie_controls_mode": 0,
                    "default_content_setting_values": {"cookies": 1},
                },
                "intl": {"accept_languages": "zh-CN,zh,en"},
            }
            with open(pref_path, "w", encoding="utf-8") as f:
                _json.dump(initial, f, ensure_ascii=False)
            _diag("已创建初始 Preferences (restore_on_startup=5)")
            return
        except Exception as _e:
            _diag(f"创建 Preferences 失败: {_e}")
            return

    try:
        with open(pref_path, "r", encoding="utf-8") as f:
            pref = _json.load(f)
    except Exception as _e:
        _diag(f"读取 Preferences 失败（文件损坏？）: {_e}")
        return

    # 诊断：当前关键设置
    _curr_restore = pref.get("session", {}).get("restore_on_startup", "未设置")
    _curr_exit_type = pref.get("profile", {}).get("exit_type", "未设置")
    _curr_exited_cleanly = pref.get("profile", {}).get("exited_cleanly", "未设置")
    _curr_cookie_mode = pref.get("profile", {}).get("cookie_controls_mode", "未设置")
    _diag(f"修改前 Preferences: restore_on_startup={_curr_restore}, exit_type={_curr_exit_type}, exited_cleanly={_curr_exited_cleanly}, cookie_controls_mode={_curr_cookie_mode}")

    changed = False
    sess = pref.setdefault("session", {})
    if sess.get("restore_on_startup") != 5:
        sess["restore_on_startup"] = 5
        changed = True
    # 强制清空 startup_urls：如果有任何 URL 在这里，
    # Chrome 会优先打开这些 URL 而跳过 session restore，session cookie 必丢
    if sess.get("startup_urls") != []:
        sess["startup_urls"] = []
        changed = True

    prof = pref.setdefault("profile", {})
    if prof.get("cookie_controls_mode") != 0:
        prof["cookie_controls_mode"] = 0
        changed = True
    cs = prof.setdefault("default_content_setting_values", {})
    if cs.get("cookies") != 1:
        cs["cookies"] = 1
        changed = True
    if prof.get("exit_type") != "Normal":
        prof["exit_type"] = "Normal"
        changed = True
    if prof.get("exited_cleanly") is not True:
        prof["exited_cleanly"] = True
        changed = True

    if changed:
        try:
            with open(pref_path, "w", encoding="utf-8") as f:
                _json.dump(pref, f, ensure_ascii=False)
            _diag("Preferences 已修复 (restore_on_startup=5)")
        except Exception as _e:
            _diag(f"写入 Preferences 失败: {_e}")
            pass


def open_login_browser(app_log, chrome_path: str = ""):
    """打开登录浏览器：单浏览器（Playwright + 系统 Chrome 内核），同时处理闲鱼+煤炉。

    用 Playwright 指向原 purchase_monitor profile 目录，所以：
    - 打开就能看到上次登录的账号（cookies 还在的话）
    - 闲鱼+煤炉各开一个 tab
    - 闲鱼 cookies 持续自动写到 goofish_cookie_cache.json（不丢 session cookie）
    - 煤炉 LevelDB 由同一个 Chromium 写入（机制不变，token_store 正常工作）
    """
    try:
        from core.purchase_login_playwright import launch_login_browser_threaded as _pw_login
        _pw_login(app_log)
    except Exception as _e:
        app_log(f"[采购登录] ⚠ Playwright 启动失败：{_e}")
        app_log(f"[采购登录] 请确认 playwright 已装：pip install playwright + playwright install chromium")
    return  # 不再走系统 Chrome subprocess

    # ↓↓↓ 以下旧 system Chrome 流程已废弃（保留代码方便回滚），不会执行 ↓↓↓
    app_log("[采购][cookie诊断 v5.0.82] open_login_browser(Chrome 模式) 开始执行")

    # 诊断：列出 profile 目录下所有子目录（看有没有 Default 之外的）
    try:
        if PURCHASE_PROFILE_DIR.exists():
            _subdirs = [p.name for p in PURCHASE_PROFILE_DIR.iterdir() if p.is_dir()]
            app_log(f"[采购][cookie诊断] profile 子目录: {_subdirs}")
            # 检查 Local State 里的 last_used profile
            _ls = PURCHASE_PROFILE_DIR / "Local State"
            if _ls.exists():
                try:
                    import json as _json
                    _ls_data = _json.loads(_ls.read_text(encoding="utf-8", errors="replace"))
                    _last_used = _ls_data.get("profile", {}).get("last_used", "未知")
                    app_log(f"[采购][cookie诊断] Local State last_used profile: {_last_used}")
                    _info_cache = _ls_data.get("profile", {}).get("info_cache", {})
                    if _info_cache:
                        app_log(f"[采购][cookie诊断] Local State 已知 profile: {list(_info_cache.keys())}")
                except Exception as _e:
                    app_log(f"[采购][cookie诊断] 解析 Local State 失败: {_e}")
    except Exception as _e:
        app_log(f"[采购][cookie诊断] 列目录异常: {_e}")

    try:
        PURCHASE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 诊断：profile 目录是否可写
    try:
        _test = PURCHASE_PROFILE_DIR / ".write_test"
        _test.write_text("test")
        _test.unlink()
        app_log(f"[采购][cookie诊断] profile 目录可写: {PURCHASE_PROFILE_DIR}")
    except Exception as _e:
        app_log(f"[采购][cookie诊断] ⚠ profile 目录不可写: {_e}")
        app_log(f"[采购][cookie诊断] ⚠ 可能原因: 防毒软件锁定/磁盘空间不足/权限不足")

    # 诊断：profile 父目录是否在 OneDrive 或类似云同步目录
    _profile_str = str(PURCHASE_PROFILE_DIR).lower()
    for _bad_keyword in ["onedrive", "dropbox", "icloud", "google drive", "百度网盘", "坚果云"]:
        if _bad_keyword in _profile_str:
            app_log(f"[采购][cookie诊断] ⚠ profile 在云同步目录 ({_bad_keyword})，会导致 cookie 文件被锁/同步失败")
            break

    exe = _get_system_chrome_path(chrome_path)
    if not exe:
        app_log("[采购] 未找到系统Chrome路径。请在【设置】里配置固定系统Chrome路径。")
        return

    # ⚠️ 关键：如果同一个 user-data-dir 仍被 Playwright/Chrome 占用，
    # 你再次"打开登录浏览器"时，Chrome 会复用既有进程（往往带着 --enable-automation），
    # 闲鱼会更容易出现登录态不持久/反复要求扫码。
    in_use, detail = detect_chrome_profile_in_use(PURCHASE_PROFILE_DIR)
    if in_use:
        app_log(
            "[采购] 检测到【采购Profile正在被占用】（可能你正在跑采购监控/后台浏览器未退出）。\n"
            "请先停止采购监控，并在任务管理器里确认没有残留的 Chrome 进程后，再点【打开登录浏览器】。\n"
            f"详情：{detail}"
        )
        return

    # 修改 Preferences：让 session cookie 持久化，避免下次打开要重新登录
    try:
        _ensure_chrome_session_persistence(PURCHASE_PROFILE_DIR, app_log=app_log)
    except Exception as _e:
        app_log(f"[采购] Preferences 修改跳过：{_e}")

    # 诊断：清理残留的 SingletonLock 文件（上次 Chrome 没正常退出会留下）
    # 如果 SingletonLock 存在，Chrome 会用「Crashed」模式重新打开，可能丢失 session cookie
    _residual_locks = []
    for _lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        _lock_file = PURCHASE_PROFILE_DIR / _lock_name
        if _lock_file.exists():
            _residual_locks.append(_lock_name)
            try:
                _lock_file.unlink()
            except Exception:
                pass
    if _residual_locks:
        app_log(f"[采购][cookie诊断] ⚠ 发现残留锁文件 {_residual_locks}，已清理。这表示上次 Chrome 没正常退出，session cookie 可能已丢失。")
        app_log(f"[采购][cookie诊断] ⚠ 请确保下次【手动点 X 关闭】Chrome，不要直接关闭整个 cmd 窗口或任务管理器结束")

    # 打开两个站点（按你实际需要可自行改成订单页/登录页）
    urls = [
        # 闲鱼已用 Playwright 处理（开头那段），这里不再打开 goofish
        "https://jp.mercari.com/",
    ]

    # 用同一份 user-data-dir，确保登录态持久化
    cmd = [
        exe,
        f"--user-data-dir={str(PURCHASE_PROFILE_DIR)}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-features=TranslateUI,ThirdPartyCookiesDeprecation,TrackingProtection3pcd,PrivacySandboxSettings4",
        f"--proxy-bypass-list={GOOFISH_PROXY_BYPASS}",
        "--lang=zh-CN",
        "--disable-background-mode",
        "--disable-background-networking",
        "--new-window",
        *urls,
    ]

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        app_log("[采购] 系统 Chrome 已打开（煤炉登录）。闲鱼请在另一个 Playwright 窗口扫码。")
    except Exception as e:
        app_log(f"[采购] 打开登录浏览器失败：{e}")


def scrape_xianyu(order_id: str, headless: bool, timeout_ms: int = 45000) -> Tuple[Dict[str, str], bool, str]:
    """纯 HTTP 闲鱼订单抓取（不使用浏览器）。

    返回：(fields, need_login, error_detail)
    fields: pay_amount, pay_dt_raw, tracking_no
    error_detail: 空字符串=成功, 否则为错误原因
    """
    import logging as _logging
    _log = _logging.getLogger("purchase")
    empty = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    # ── 同轮次防重试：如果上次重试也失败了，直接跳过（避免 5 个订单做 5 次无用重试）
    _last_fail_ts = getattr(scrape_xianyu, "_last_retry_fail_ts", 0.0)
    if _last_fail_ts and (time.time() - _last_fail_ts) < 120:
        return empty, True, "session_dead_skip"

    try:
        from .goofish_order_http import fetch_order_detail_http
        fields, error = fetch_order_detail_http(
            order_id, PURCHASE_PROFILE_DIR, log_fn=_log.info
        )
        if not error:
            _log.info("[采购] HTTP 抓取成功: %s -> %s", order_id, fields)
            scrape_xianyu._last_retry_fail_ts = 0.0  # 成功了，清除标记
            return fields, False, ""

        if error == "rate_limit":
            _log.info("[采购] HTTP 频率限制, 跳过本次")
            return empty, False, "rate_limit"

        if error in ("session_expired", "no_cookies", "token_expired"):
            # 三种情况都需要 invalidate 缓存 + 重新提取:
            # - session_expired: 服务端 session 过期, 缓存 cookie 已无效
            # - no_cookies: 缓存不存在或已失效
            # - token_expired: token 过期, 需要新 cookie
            _log.info("[采购] 闲鱼 HTTP: %s — invalidate 缓存后重新提取 cookie 重试", error)
            retry_err = error
            try:
                from .goofish_cookie_store import extract_cookies_from_profile, invalidate_goofish_cookies
                invalidate_goofish_cookies(PURCHASE_PROFILE_DIR)
                if extract_cookies_from_profile(PURCHASE_PROFILE_DIR):
                    fields2, error2 = fetch_order_detail_http(
                        order_id, PURCHASE_PROFILE_DIR, log_fn=_log.info
                    )
                    if not error2:
                        _log.info("[采购] 重提取后 HTTP 成功: %s -> %s", order_id, fields2)
                        scrape_xianyu._last_retry_fail_ts = 0.0
                        return fields2, False, ""
                    retry_err = f"{error}→重试→{error2}"
                    _log.info("[采购] 重提取后仍失败: %s", error2)
                else:
                    retry_err = f"{error}→cookie提取失败"
            except Exception as re_err:
                retry_err = f"{error}→重提取异常:{re_err}"
                _log.warning("[采购] cookie 重提取异常: %s", re_err)
            # 重试也失败了 → 标记，同轮次后续订单不再重试
            scrape_xianyu._last_retry_fail_ts = time.time()
            return empty, True, retry_err

        _log.warning("[采购] HTTP 抓取失败: %s", error)
        return empty, False, error

    except Exception as e:
        _log.error("[采购] HTTP 抓取异常: %s", e)
        return empty, False, f"exception:{e}"



def _scrape_xianyu_playwright(order_id: str, headless: bool, timeout_ms: int = 45000) -> Tuple[Dict[str, str], bool]:
    """
    返回：(fields, need_login)
    fields: pay_amount, pay_dt_raw, tracking_no

    规则：
    - 代付日期：对应「付款时间」
    - 代付金额：对应「成交价」里的金额（支持小数，如 28.88）
    - 物流单号：先留空/尽力提取（后续你再补抓取逻辑）
    """
    url = f"https://www.goofish.com/order-detail?orderId={order_id}"
    p, ctx, lock_ok = _pw_open_context(headless=headless, chrome_path="", no_proxy=True)
    try:
        page = ctx.new_page()
        # 先访问闲鱼首页让 cookie 生效，处理"快速进入"弹窗
        try:
            page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            # 检测并点击"快速进入"按钮
            for _btn_text in ("快速进入", "快速進入"):
                try:
                    btn = page.locator(f"text={_btn_text}").first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue
        except Exception:
            pass
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # 闲鱼订单详情是强动态页面：多等一会儿，让「付款时间/成交价」渲染出来
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            page.locator("text=付款时间").first.wait_for(timeout=8000)
        except Exception:
            pass

        # headless 诊断截图
        if headless:
            try:
                _diag = Path(__file__).resolve().parent.parent / "output" / "headless_diag_xianyu.png"
                _diag.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(_diag), full_page=True)
            except Exception:
                pass

        # 若订单已发货/有物流，"收货信息"区块会在稍后渲染（部分情况下需滚动触发懒加载）
        has_ship = False
        for k in ("收货信息", "收貨信息"):
            try:
                loc = page.locator(f"text={k}").first
                loc.wait_for(timeout=2500)
                has_ship = True
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                break
            except Exception:
                continue

        time.sleep(0.6)
        body_text = page.inner_text("body")
        need_login = _detect_login("xianyu", page.url, body_text)
        if need_login:
            return {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}, True

        # --- 提取：付款时间 ---
        pay_dt = ""
        lines = [l.strip() for l in body_text.splitlines() if l.strip()]
        dt_pat = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}:\d{2})")
        for i, line in enumerate(lines):
            if ("付款时间" in line) or ("付款時間" in line):
                m = dt_pat.search(line)
                if not m:
                    look = " ".join(lines[i:i+4])
                    m = dt_pat.search(look)
                if m:
                    pay_dt = m.group(1)
                    break
        if not pay_dt:
            m = re.search(r"(付款时间|付款時間)[\s\S]{0,80}?(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}:\d{2})", body_text)
            if m:
                pay_dt = m.group(2)

        # --- 提取：成交价金额（支持 '成交价（...） ¥28.88' 这种结构） ---
        pay_amount = ""
        amt_pat = re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)")
        for i, line in enumerate(lines):
            if ("成交价" in line) or ("成交價" in line):
                m = amt_pat.search(line)
                if not m:
                    look = " ".join(lines[i:i+6])
                    m = amt_pat.search(look)
                if m:
                    pay_amount = m.group(1)
                    break
        if not pay_amount:
            m = re.search(r"(成交价|成交價)[\s\S]{0,120}?[¥￥]\s*([0-9]+(?:\.[0-9]+)?)", body_text)
            if m:
                pay_amount = m.group(2)

        # 检测订单是否尚未发货（页面含"等待卖家发货"等关键词时，不应提取物流单号）
        _not_shipped_keywords = ("等待卖家发货", "等待賣家發貨", "待发货", "待發貨", "未发货", "未發貨")
        _order_not_shipped = any(kw in (body_text or "") for kw in _not_shipped_keywords)

        # 物流单号：只在「收货信息」区块出现 且 订单已发货 时才抓取
        tracking = "" if _order_not_shipped else _extract_xianyu_tracking(body_text)

        ship_present = (not _order_not_shipped) and (has_ship or ("收货信息" in (body_text or "")) or ("收貨信息" in (body_text or "")))

        # 若页面有「收货信息」，但 body.innerText 暂时没包含单号（强动态/懒加载/复制按钮用属性存值），做更稳的提取：
        if (not tracking) and ship_present:
            try:
                ship_loc = None
                for k in ("收货信息", "收貨信息"):
                    try:
                        loc = page.locator(f"text={k}").first
                        if loc.count() > 0:
                            ship_loc = loc
                            break
                    except Exception:
                        continue

                if ship_loc is not None:
                    # 先尝试从"复制"相关元素的 data-clipboard-text / data-copy 等属性取值；
                    # 若取不到，再回退为该卡片的 innerText，让 _extract_xianyu_tracking 去解析。
                    blob = ship_loc.evaluate("""(el) => {
                        let root = el;
                        for (let i = 0; i < 8 && root; i++) root = root.parentElement;
                        if (!root) return '';
                        const pickAttr = (node) => node && (node.getAttribute('data-clipboard-text') || node.getAttribute('data-copy') || node.getAttribute('data-text') || '');
                        // 1) 直接找带复制属性的节点
                        const nodes = Array.from(root.querySelectorAll('[data-clipboard-text],[data-copy],[data-text]'));
                        for (const n of nodes) {
                            const v = pickAttr(n) || '';
                            if (v) return v;
                        }
                        // 2) 找"复制"按钮附近
                        const copyEls = Array.from(root.querySelectorAll('button,a,span,div')).filter(x => (x.innerText || '').trim() === '复制');
                        for (const ce of copyEls) {
                            const v = pickAttr(ce) || '';
                            if (v) return v;
                            const t = (ce.parentElement && (ce.parentElement.innerText || '')) || '';
                            if (t) return t;
                        }
                        return root.innerText || '';
                    }""")
                    blob = str(blob or "").strip()

                    # 如果 blob 本身就像一个单号（多半来自 data-clipboard-text），直接用
                    m = re.search(r"\b[A-Za-z0-9\-]{8,40}\b", blob)
                    if m and (not re.fullmatch(r"\d{11}", m.group(0))):
                        tracking = m.group(0)

                    if not tracking:
                        tracking = _extract_xianyu_tracking(blob)
            except Exception:
                pass

        # 最后兜底：触发一次懒加载滚动后再抓 body（不会误抓，因为仍受 _extract_xianyu_tracking 的「收货信息」约束）
        if (not tracking) and ship_present:
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(600)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(200)
                body_text2 = page.inner_text("body")
                tracking = _extract_xianyu_tracking(body_text2)
            except Exception:
                pass

        return {"pay_amount": _clean_amount(pay_amount), "pay_dt_raw": pay_dt, "tracking_no": tracking}, False

    finally:
        # 导出闲鱼 cookie 供 HTTP 路径使用 (best-effort)
        try:
            from .goofish_cookie_store import save_goofish_cookies
            _raw = ctx.cookies()
            save_goofish_cookies(PURCHASE_PROFILE_DIR, _raw)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass
        try:
            release(PURCHASE_PROFILE_DIR)
        except Exception:
            pass
def scrape_mercari(item_id: str, headless: bool, timeout_ms: int = 45000) -> Tuple[Dict[str, str], bool]:
    """纯HTTP煤炉交易查询：订单详情 + 物流API获取单号。

    item_id 形如：m90000000001
    """
    import logging as _logging
    _log = _logging.getLogger("purchase")
    empty = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    try:
        from .mercari_order_http import fetch_mercari_transaction_http
        fields, error = fetch_mercari_transaction_http(
            item_id, PURCHASE_PROFILE_DIR, log_fn=_log.info
        )
        if error:
            if error in ("auth_expired", "no_token"):
                _log.info("[采购] 煤炉 token 不可用 (%s), 需重新登录", error)
                return empty, True
            if error == "not_found":
                _log.warning("[采购] 煤炉交易不存在: %s", item_id)
                return empty, False
            _log.warning("[采购] 煤炉 HTTP 抓取失败: %s", error)
            return empty, False

        _log.info("[采购] 煤炉 HTTP 抓取成功: %s -> %s", item_id, fields)
        return fields, False

    except Exception as e:
        _log.error("[采购] 煤炉抓取异常: %s", e)
        return empty, False


def scrape(platform: str, purchase_order_id: str, headless: bool) -> Tuple[Dict[str, str], bool, str]:
    platform = (platform or "").strip().lower()
    if platform == "xianyu":
        return scrape_xianyu(purchase_order_id, headless=headless)
    if platform == "mercari":
        f, nl = scrape_mercari(purchase_order_id, headless=headless)
        return f, nl, ""
    return {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}, False, ""


# -----------------------------
# Tkinter UI + 监控控制器
# -----------------------------
class PurchaseFeatureTab:
    """
    采购绑定/监控页签

    你要的逻辑：
    1) 采购绑定/监控：只负责把「采购订单号 + 商品名称 + 规格 + 代付日期/金额 + 物流单号」整理完整
       - 闲鱼/煤炉都可抓取物流单号；抓不到时仍允许手填（抓取不会覆盖你手填的单号）
       - "抓取一次"只补齐日期/金额（能抓到的话）
       - 监控是可选的：只有勾选"监控"的记录才会定期检查（并且可以全选/全不选）
    2) 数据整理完整后：勾选记录 -> "发送到订单获取/出货"
       - 会合并到【订单获取/出货】任务列表
       - 采购绑定/监控里的记录不会自动删除（你手动删）
    """

    def __init__(
        self,
        app: Any,
        frame: ttk.Frame,
        *,
        on_create_ship_task: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.app = app
        self.frame = frame
        self.on_create_ship_task = on_create_ship_task
        self.purchase_cmd = None  # TG 采购指令处理器（由 app.py 设置）

        self.links: List[PurchaseLink] = load_links()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

        # --- 绑定录入区 ---
        self.var_account = tk.StringVar()
        self.var_yahoo_order = tk.StringVar()
        self.var_platform = tk.StringVar(value="xianyu")

        # 副订单（最多 5 个）：用于显示/拼接，不参与抓取/检测逻辑
        self.sub_order_vars: List[tk.StringVar] = []
        self._sub_orders_holder: Optional[ttk.Frame] = None

        # 动态行：采购订单号/商品名称/规格（可多条）
        self.purchase_rows: List[Dict[str, tk.StringVar]] = []

        # --- 监控参数 ---
        self.var_interval = tk.IntVar(value=int(getattr(app, "settings", {}).get("purchase_interval_sec", 600)))
        self.var_headless = tk.BooleanVar(value=bool(getattr(app, "settings", {}).get("purchase_headless", True)))
        self.var_auto_create_ship = tk.BooleanVar(value=bool(getattr(app, "settings", {}).get("purchase_auto_create_ship", False)))

        # --- 手动编辑（选中行） ---
        self.var_edit_purchase_id = tk.StringVar()
        self.var_edit_product = tk.StringVar()
        self.var_edit_spec = tk.StringVar()
        self.var_edit_remark = tk.StringVar()
        self.var_edit_pay_dt = tk.StringVar()
        self.var_edit_pay_amount = tk.StringVar()
        self.var_edit_tracking = tk.StringVar()

        # tree
        self.tree: Optional[ttk.Treeview] = None

    # -----------------
    # helpers
    # -----------------
    def log(self, s: str) -> None:
        try:
            self.app.log(s)
        except Exception:
            print(s)

    def _save_settings_patch(self) -> None:
        try:
            st = getattr(self.app, "settings", {})
            st["purchase_interval_sec"] = int(self.var_interval.get())
            st["purchase_headless"] = bool(self.var_headless.get())
            st["purchase_auto_create_ship"] = bool(self.var_auto_create_ship.get())
            if hasattr(self.app, "_save_settings"):
                self.app._save_settings()
            else:
                try:
                    from core.accounts import save_settings as _save
                    _save(st)
                except Exception:
                    pass
        except Exception:
            pass

    def _open_monitor_settings(self):
        w = tk.Toplevel(self.app)
        w.title("监控设置")
        w.resizable(False, False)
        w.grab_set()
        g = ttk.LabelFrame(w, text="监控参数")
        g.pack(fill="x", padx=10, pady=10)
        ttk.Label(g, text="间隔(秒)").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(g, textvariable=self.var_interval, width=10).grid(row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Checkbutton(g, text="后台无头(headless)", variable=self.var_headless, command=self._save_settings_patch).grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        ttk.Checkbutton(g, text="自动生成出货任务（闲鱼+煤炉）", variable=self.var_auto_create_ship, command=self._save_settings_patch).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        g2 = ttk.LabelFrame(w, text="批量操作")
        g2.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(g2, text="选中→监控", command=lambda: self.action_watch_selected(True)).pack(anchor="w", padx=6, pady=3)
        ttk.Button(g2, text="选中→不监控", command=lambda: self.action_watch_selected(False)).pack(anchor="w", padx=6, pady=(0, 6))
        ttk.Button(w, text="关闭", command=w.destroy).pack(pady=(0, 10))

    def _open_edit_dialog(self):
        self.action_load_selected_to_editor()
        if not (self.var_edit_purchase_id.get() or self.var_edit_product.get()):
            from tkinter import messagebox
            messagebox.showinfo("提示", "请先在列表中选中一行")
            return
        w = tk.Toplevel(self.app)
        w.title("编辑选中记录")
        w.resizable(False, False)
        w.grab_set()
        g = ttk.Frame(w)
        g.pack(fill="x", padx=10, pady=10)
        fields = [
            ("采购订单号", self.var_edit_purchase_id),
            ("商品名称", self.var_edit_product),
            ("规格", self.var_edit_spec),
            ("代付日期", self.var_edit_pay_dt),
            ("代付金额", self.var_edit_pay_amount),
            ("物流单号", self.var_edit_tracking),
            ("备注", self.var_edit_remark),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(g, textvariable=var, width=40).grid(row=i, column=1, sticky="ew", padx=6, pady=3)
        g.columnconfigure(1, weight=1)
        def _save_and_close():
            self.action_apply_editor_to_selected()
            w.destroy()
        bf = ttk.Frame(w)
        bf.pack(pady=(0, 10))
        ttk.Button(bf, style="Accent.TButton", text="保存", command=_save_and_close).pack(side="left", padx=6)
        ttk.Button(bf, text="取消", command=w.destroy).pack(side="left", padx=6)

    def _refresh_accounts(self) -> None:
        accs = getattr(self.app, "accounts", []) or []
        vals = []
        for a in accs:
            name = str(a.get("name", "") or "").strip()
            pid = str(a.get("profile_id", "") or "").strip()
            if not pid:
                continue
            if name:
                vals.append(f"{name} ({pid})")
            else:
                vals.append(pid)
        vals = sorted(set(vals))
        if hasattr(self, "cmb_account"):
            self.cmb_account["values"] = vals
        if vals and not (self.var_account.get() or "").strip():
            self.var_account.set(vals[0])

    def _parse_account_display(self, disp: str):
        s = (disp or "").strip()
        m = re.search(r"\(([^()]+)\)\s*$", s)
        if m:
            pid = m.group(1).strip()
            name = s[: m.start()].strip()
            return pid, name
        return s, s

    # -----------------
    # sub-order helpers
    # -----------------
    def _normalize_sub_order(self, s: str) -> str:
        s = (s or "").strip()
        # 允许用户粘贴含有 + 的字符串：只取第一个片段放入单格
        if "+" in s:
            s = s.split("+", 1)[0].strip()
        return s

    def _get_sub_orders(self) -> List[str]:
        out: List[str] = []
        seen = set()
        for v in (self.sub_order_vars or []):
            try:
                s = self._normalize_sub_order(v.get())
            except Exception:
                s = ""
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        # 最多 5 个
        return out[:5]

    def _ensure_sub_order_rows(self) -> None:
        """确保副订单输入框至少存在 1 个。"""
        if self.sub_order_vars:
            return
        self._add_sub_order_row()

    def _add_sub_order_row(self) -> None:
        if len(self.sub_order_vars) >= 5:
            try:
                messagebox.showinfo("提示", "副订单编号最多 5 个")
            except Exception:
                pass
            return
        v = tk.StringVar()
        self.sub_order_vars.append(v)
        self._rebuild_sub_order_rows()

    def _clear_sub_orders(self) -> None:
        for v in (self.sub_order_vars or []):
            try:
                v.set("")
            except Exception:
                pass
        # 保留 1 个输入框（不让布局跳变）
        if len(self.sub_order_vars) > 1:
            self.sub_order_vars = self.sub_order_vars[:1]
        self._rebuild_sub_order_rows()

    def _rebuild_sub_order_rows(self) -> None:
        holder = getattr(self, "_sub_orders_holder", None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        # 每个输入框一个小 Entry，横向排列
        for i, v in enumerate(self.sub_order_vars or []):
            e = ttk.Entry(holder, textvariable=v, width=16)
            e.grid(row=0, column=i, sticky="w", padx=(0, 6))
        # 让 holder 可扩展
        for i in range(5):
            holder.columnconfigure(i, weight=0)

    def _persist(self) -> None:
        save_links(self.links)

    def _merge_and_persist(self) -> None:
        """从文件重新加载 links，合并抓取结果后保存。

        解决竞态：_loop 抓取期间 TG 可能修改了 watch 等字段，
        直接 _persist 会覆盖 TG 的改动。此方法只把抓取产生的
        字段（status/tracking_no/pay_amount 等）写回，保留文件
        中其他字段（watch 等）的最新值。
        """
        fresh = load_links()
        # 按 purchase_order_id 建索引，用于匹配
        fresh_map = {}
        for fx in fresh:
            key = (fx.yahoo_order_no, fx.purchase_order_id)
            fresh_map[key] = fx

        for mem in self.links:
            key = (mem.yahoo_order_no, mem.purchase_order_id)
            fx = fresh_map.get(key)
            if not fx:
                continue
            # 只合并抓取产生的字段
            fx.status = mem.status
            fx.tracking_no = mem.tracking_no
            fx.pay_amount = mem.pay_amount
            fx.pay_dt_raw = mem.pay_dt_raw
            fx.pay_mmdd = mem.pay_mmdd
            fx.error = mem.error
            fx.last_checked_at = mem.last_checked_at
            fx.notified_login = mem.notified_login
            fx.notified_tracking = mem.notified_tracking
            fx.created_ship_task = mem.created_ship_task
            # watch: 如果抓取过程中把 watch 从 True 改为 False
            # （获取到物流单号后自动关闭），需要同步
            if not mem.watch and fx.watch:
                # 抓取逻辑主动关闭了监控（获取到单号）
                fx.watch = False

        self.links = fresh
        save_links(fresh)

    def _mmdd_to_show(self, mmdd: str) -> str:
        mmdd = (mmdd or "").strip()
        if re.fullmatch(r"\d{4}", mmdd):
            mm = int(mmdd[:2])
            dd = int(mmdd[2:])
            return f"{mm}月{dd}日"
        return ""

    def _norm_mmdd_input(self, s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        m = re.fullmatch(r"(\d{1,2})[/-]?(\d{2})", s)
        if m:
            mm = int(m.group(1))
            dd = int(m.group(2))
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{mm:02d}{dd:02d}"
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
        if m:
            mm = int(m.group(1))
            dd = int(m.group(2))
            return f"{mm:02d}{dd:02d}"
        mmdd = _to_mmdd(s)
        if mmdd:
            return mmdd
        return ""

    # -----------------
    # tree render / interaction
    # -----------------
    def _render_tree(self) -> None:
        if not self.tree:
            return
        self.tree.delete(*self.tree.get_children())
        for idx, x in enumerate(self.links):
            y_disp = x.yahoo_order_no
            try:
                subs = getattr(x, "sub_order_nos", None) or []
                subs = [str(s).strip() for s in subs if str(s).strip()]
                if subs:
                    y_disp = f"{x.yahoo_order_no}+" + "+".join(subs)
            except Exception:
                pass
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    "☑" if x.watch else "☐",
                    x.yahoo_acc_name,
                    y_disp,
                    x.platform,
                    x.purchase_order_id,
                    x.product_name,
                    x.spec,
                    x.remark,
                    self._mmdd_to_show(x.pay_mmdd) or x.pay_mmdd,
                    x.pay_amount,
                    x.tracking_no,
                    x.status,
                    x.last_checked_at,
                    (x.error or "")[:60],
                ),
            )

    def _tree_toggle_watch_by_iid(self, iid: str) -> None:
        try:
            idx = int(iid)
        except Exception:
            return
        # 先从文件重新加载，避免覆盖 TG 端的修改
        self.links = load_links()
        if not (0 <= idx < len(self.links)):
            return
        self.links[idx].watch = not bool(self.links[idx].watch)
        self._persist()
        self._render_tree()

    def _on_tree_click(self, event):
        if not self.tree:
            return
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and col == "#1":
            self._tree_toggle_watch_by_iid(row)
            return "break"
        return

    # -----------------
    # purchase rows UI
    # -----------------
    def _add_purchase_row(self, init=None) -> None:
        init = init or {}
        row = {
            "purchase_order_id": tk.StringVar(value=init.get("purchase_order_id", "")),
            "product_name": tk.StringVar(value=init.get("product_name", "")),
            "spec": tk.StringVar(value=init.get("spec", "")),
            "remark": tk.StringVar(value=init.get("remark", "")),
        }
        self.purchase_rows.append(row)
        self._rebuild_purchase_rows()

    def _remove_purchase_row(self, idx: int) -> None:
        if len(self.purchase_rows) <= 1:
            for k in ("purchase_order_id", "product_name", "spec", "remark"):
                self.purchase_rows[0][k].set("")
            self._rebuild_purchase_rows()
            return
        if 0 <= idx < len(self.purchase_rows):
            self.purchase_rows.pop(idx)
        self._rebuild_purchase_rows()

    def _rebuild_purchase_rows(self) -> None:
        if not hasattr(self, "purchase_rows_holder"):
            return
        holder = self.purchase_rows_holder
        for w in holder.winfo_children():
            w.destroy()

        ttk.Label(holder, text="采购订单号").grid(row=0, column=0, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="商品名称").grid(row=0, column=1, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="规格").grid(row=0, column=2, sticky="w", padx=3, pady=(2, 1))
        ttk.Label(holder, text="备注").grid(row=0, column=3, sticky="w", padx=3, pady=(2, 1))
        ttk.Button(holder, text="＋", width=3, command=lambda: self._add_purchase_row()).grid(row=0, column=4, sticky="w", padx=3, pady=(2, 1))

        holder.columnconfigure(0, weight=0)
        holder.columnconfigure(1, weight=1)
        holder.columnconfigure(2, weight=1)
        holder.columnconfigure(3, weight=1)
        holder.columnconfigure(4, weight=0)

        for i, r in enumerate(self.purchase_rows, start=1):
            ttk.Entry(holder, textvariable=r["purchase_order_id"], width=28).grid(row=i, column=0, sticky="w", padx=3, pady=2)
            ttk.Entry(holder, textvariable=r["product_name"]).grid(row=i, column=1, sticky="ew", padx=3, pady=2)
            ttk.Entry(holder, textvariable=r["spec"]).grid(row=i, column=2, sticky="ew", padx=3, pady=2)
            ttk.Entry(holder, textvariable=r["remark"]).grid(row=i, column=3, sticky="ew", padx=3, pady=2)
            ttk.Button(holder, text="－", width=3, command=lambda ii=i-1: self._remove_purchase_row(ii)).grid(row=i, column=4, sticky="w", padx=3, pady=2)

    # -----------------
    # actions
    # -----------------
    def action_add_bindings(self) -> None:
        disp = (self.var_account.get() or "").strip()
        pid, name = self._parse_account_display(disp)
        y_order = (self.var_yahoo_order.get() or "").strip()
        sub_orders = self._get_sub_orders()
        platform = (self.var_platform.get() or "").strip().lower()
        if not pid or not y_order:
            messagebox.showerror("错误", "请填写：Yahoo账号、Yahoo订单号")
            return
        if platform not in ("xianyu", "mercari"):
            messagebox.showerror("错误", "平台只支持：xianyu / mercari")
            return

        # 从文件重新加载，避免覆盖 TG 端的修改
        self.links = load_links()

        created = 0
        existed = 0

        for r in self.purchase_rows:
            p_order = (r["purchase_order_id"].get() or "").strip()
            if not p_order:
                continue
            product_name = (r["product_name"].get() or "").strip()
            spec = (r["spec"].get() or "").strip()
            remark = (r.get("remark").get() if isinstance(r.get("remark"), tk.StringVar) else (r.get("remark") or ""))
            remark = (remark or "").strip()

            dup = False
            for ex in self.links:
                if ex.yahoo_profile_id == pid and ex.yahoo_order_no == y_order and ex.platform == platform and ex.purchase_order_id == p_order:
                    dup = True
                    break
            if dup:
                existed += 1
                continue

            link = PurchaseLink(
                yahoo_profile_id=pid,
                yahoo_acc_name=name,
                yahoo_order_no=y_order,
                sub_order_nos=sub_orders,
                platform=platform,
                purchase_order_id=p_order,
                product_name=product_name,
                spec=spec,
                remark=remark,
                watch=True,
                status="待监控",
            )
            self.links.append(link)
            created += 1

        if created == 0 and existed == 0:
            messagebox.showerror("错误", "请至少填写 1 条采购订单号")
            return

        self._persist()
        self._render_tree()
        self.log(f"[PURCHASE] 已新增绑定：{created} 条（重复跳过：{existed}）")

    def action_delete_selected(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            return
        self.links = load_links()
        idxs = sorted([int(x) for x in sel], reverse=True)
        for i in idxs:
            if 0 <= i < len(self.links):
                del self.links[i]
        self._persist()
        self._render_tree()

    def action_watch_all(self, value: bool) -> None:
        self.links = load_links()
        for x in self.links:
            x.watch = bool(value)
        self._persist()
        self._render_tree()

    def action_watch_selected(self, value: bool) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            return
        self.links = load_links()
        for iid in sel:
            try:
                idx = int(iid)
            except Exception:
                continue
            if 0 <= idx < len(self.links):
                self.links[idx].watch = bool(value)
        self._persist()
        self._render_tree()

    def action_open_login_browser(self) -> None:
        self._save_settings_patch()
        chrome_path = ""
        try:
            chrome_path = self.app.var_browser.get()
        except Exception:
            pass
        t = threading.Thread(
            target=open_login_browser,
            kwargs={"app_log": self.log, "chrome_path": chrome_path},
            daemon=True,
        )
        t.start()

    def _scrape_indices(self, indices, *, notify: bool, headless=None) -> None:
        changed = False
        for idx in indices:
            changed = self._scrape_and_update(idx, notify=notify, auto_create=False, headless=headless) or changed
        if changed:
            self._merge_and_persist()
        self._ui_queue.put(("refresh", None))

    def action_scrape_selected_once(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择记录")
            return
        indices = []
        for iid in sel:
            try:
                indices.append(int(iid))
            except Exception:
                pass

        t = threading.Thread(
            target=self._scrape_indices,
            kwargs={"indices": indices, "notify": True, "headless": bool(self.var_headless.get())},
            daemon=True,
        )
        t.start()

    def action_send_selected_to_ship(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择要发送的记录")
            return

        indices = []
        for iid in sel:
            try:
                indices.append(int(iid))
            except Exception:
                pass
        self.links = load_links()
        indices = [i for i in indices if 0 <= i < len(self.links)]
        if not indices:
            return

        missing = []
        for i in indices:
            if not (self.links[i].tracking_no or "").strip():
                missing.append(f"{self.links[i].platform}:{self.links[i].purchase_order_id}")
        if missing:
            messagebox.showerror("错误", "以下记录缺少物流单号（请先手动填写）\n\n" + "\n".join(missing[:20]))
            return

        groups = {}
        for i in indices:
            x = self.links[i]
            key = (x.yahoo_profile_id, x.yahoo_order_no)
            groups.setdefault(key, []).append(x)

        sent = 0
        for (pid, order_no), items in groups.items():
            acc_name = items[0].yahoo_acc_name
            shipments = []
            for x in items:
                shipments.append({
                    "tracking_no": (x.tracking_no or "").strip(),
                    "purchase_order_id": (x.purchase_order_id or "").strip(),
                    "product_name": (x.product_name or "").strip(),
                    "spec": (x.spec or "").strip(),
                    "remark": (getattr(x, "remark", "") or "").strip(),
                    "pay_mmdd": (x.pay_mmdd or "").strip(),
                    "pay_amount": _clean_amount(x.pay_amount),
                })
                x.created_ship_task = True

            # 合并副订单编号（去重、保序）
            sub_order_nos = []
            for _x in items:
                for _s in (getattr(_x, "sub_order_nos", None) or []):
                    _s = str(_s).strip()
                    if not _s or _s == order_no:
                        continue
                    if _s not in sub_order_nos:
                        sub_order_nos.append(_s)
            order_no_display = order_no + ("+" + "+".join(sub_order_nos) if sub_order_nos else "")

            # 订单级备注：一笔Yahoo订单对应一个备注；若选中多条采购记录，则合并去重
            _rs = []
            for _x in items:
                _r = (getattr(_x, "remark", "") or "").strip()
                if _r and _r not in _rs:
                    _rs.append(_r)
            order_remark = " / ".join(_rs).strip()

            payload = {
                "profile_id": pid,
                "acc_name": acc_name,
                "order_no": order_no,
                "order_no_display": order_no_display,
                "sub_order_nos": sub_order_nos,
                "remark": order_remark,
                "shipments": shipments,
                # 标记：来自【采购绑定/监控】推送（用于后续生成【最新模板】与顺运宝上传）
                "from_purchase_monitor": True,
                # 平台：xianyu / mercari（PurchaseLink.platform）
                "purchase_platform": (link.platform or "").strip(),
            }

            ok = False
            try:
                # 1) 先走原有回调（如果存在）以保持原功能/日志/刷新
                if self.on_create_ship_task:
                    try:
                        self.on_create_ship_task(payload)
                        ok = True
                    except Exception:
                        ok = False

                # 备注/副单/快递合并已统一在 app._ship_add_task_payload() 内处理，
                # 这里不再直接写 app.ship_tasks，避免"先本地写入 -> after 回调再写入"导致的重复提示。
            except Exception as e:
                ok = False
                self.log(f"[PURCHASE] 发送到出货失败：{e}")

            if ok:
                sent += 1

        self._persist()
        self._render_tree()
        self.log(f"[PURCHASE] 已发送到【订单获取/出货】：{sent} 个任务（采购数据不删除，你可手动删）")

    def action_load_selected_to_editor(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            return
        try:
            idx = int(sel[0])
        except Exception:
            return
        if not (0 <= idx < len(self.links)):
            return
        x = self.links[idx]
        self.var_edit_purchase_id.set(x.purchase_order_id)
        self.var_edit_product.set(x.product_name)
        self.var_edit_spec.set(x.spec)
        self.var_edit_remark.set(getattr(x, "remark", ""))
        self.var_edit_pay_dt.set(x.pay_dt_raw or x.pay_mmdd)
        self.var_edit_pay_amount.set(x.pay_amount)
        self.var_edit_tracking.set(x.tracking_no)

    def action_apply_editor_to_selected(self) -> None:
        if not self.tree:
            return
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择要写入的记录")
            return

        purchase_id = (self.var_edit_purchase_id.get() or "").strip()
        product = (self.var_edit_product.get() or "").strip()
        spec = (self.var_edit_spec.get() or "").strip()
        remark = (self.var_edit_remark.get() or "").strip()
        pay_dt = (self.var_edit_pay_dt.get() or "").strip()
        pay_amount = (self.var_edit_pay_amount.get() or "").strip()
        tracking = (self.var_edit_tracking.get() or "").strip()

        mmdd = self._norm_mmdd_input(pay_dt)
        self.links = load_links()
        changed = False

        for iid in sel:
            try:
                idx = int(iid)
            except Exception:
                continue
            if not (0 <= idx < len(self.links)):
                continue
            x = self.links[idx]

            if purchase_id and purchase_id != x.purchase_order_id:
                x.purchase_order_id = purchase_id
                changed = True
            if product != x.product_name:
                x.product_name = product
                changed = True
            if spec != x.spec:
                x.spec = spec
                changed = True
            if remark != getattr(x, "remark", ""):
                x.remark = remark
                changed = True
            if pay_amount != x.pay_amount:
                x.pay_amount = pay_amount
                changed = True
            if tracking != x.tracking_no:
                x.tracking_no = tracking
                changed = True

            if pay_dt:
                x.pay_dt_raw = pay_dt
                if mmdd:
                    x.pay_mmdd = mmdd
                changed = True

            x.status = "已获取单号" if x.tracking_no else "等待出货"

        if changed:
            self._persist()
            self._render_tree()
            self.log("[PURCHASE] 已写入选中记录（采购绑定/监控数据不会自动删除）")

    # -----------------
    # monitoring
    # -----------------
    def action_start(self) -> None:
        if self._thread and self._thread.is_alive():
            messagebox.showinfo("提示", "监控已在运行")
            return
        self._stop_event.clear()
        self._save_settings_patch()
        interval = max(30, int(self.var_interval.get() or 600))
        headless = bool(self.var_headless.get())
        auto_create = bool(self.var_auto_create_ship.get())

        self._thread = threading.Thread(
            target=self._loop,
            kwargs={"interval": interval, "headless": headless, "auto_create": auto_create},
            daemon=True,
        )
        self._thread.start()
        self.log(f"[PURCHASE] 监控已启动（间隔={interval}s，headless={headless}；仅勾选『监控』的记录会执行）")

    def action_stop(self) -> None:
        self._stop_event.set()
        self.log("[PURCHASE] 已发送停止指令（会在当前抓取结束后停止）")

    def _loop(self, interval: int, headless: bool, auto_create: bool) -> None:
        while not self._stop_event.is_set():
            try:
                # ── 每轮前刷新闲鱼 session（防止服务端 cookie 过期） ──
                _need_xianyu = any(
                    getattr(x, "platform", "xianyu") == "xianyu"
                    for x in load_links() if bool(getattr(x, "watch", True))
                )
                if _need_xianyu:
                    try:
                        from .goofish_cookie_store import refresh_goofish_session
                        _chrome = _get_system_chrome_path()
                        self.log("[采购] 后台无头刷新闲鱼 session...")
                        if refresh_goofish_session(PURCHASE_PROFILE_DIR, chrome_path=_chrome):
                            self.log("[采购] 闲鱼 session 刷新成功")
                        else:
                            self.log("[采购] ⚠ 闲鱼 session 刷新失败，请使用「打开登录浏览器」重新登录闲鱼")
                    except Exception as _ref_err:
                        self.log(f"[采购] 闲鱼 session 刷新跳过: {_ref_err}")

                self.links = load_links()

                changed = False
                _watch_count = sum(1 for x in self.links if bool(getattr(x, "watch", True)))
                _has_track = sum(1 for x in self.links if bool(getattr(x, "watch", True)) and (x.tracking_no or "").strip())
                _pending = _watch_count - _has_track
                if _pending > 0 or _has_track > 0:
                    self.log(f"[采购出货] 开始第{'一' if _pending == _watch_count else ''}轮监控（共 {_watch_count} 条待监控记录，{_has_track} 条已有单号将跳过）")
                for i, x in enumerate(self.links):
                    if self._stop_event.is_set():
                        break
                    if not bool(getattr(x, "watch", True)):
                        continue
                    changed = self._scrape_and_update(i, notify=True, auto_create=auto_create, headless=headless) or changed

                if changed:
                    self._merge_and_persist()
                self._ui_queue.put(("refresh", None))
            except Exception as e:
                self.log(f"[PURCHASE] 监控循环异常：{e}")

            for _ in range(int(interval * 10)):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _scrape_and_update(self, idx: int, notify: bool, auto_create: bool, headless=None) -> bool:
        if idx < 0 or idx >= len(self.links):
            return False

        link = self.links[idx]
        prev_tracking = (link.tracking_no or "").strip()

        # 已有物流单号的记录无需再抓取（避免重复出货生成）
        if prev_tracking:
            # 确保 watch 标记关闭（防止下轮再进来）
            if link.watch:
                link.watch = False
                return True  # changed
            return False
        link.error = ""
        link.last_checked_at = _now_iso()

        try:
            use_headless = bool(self.var_headless.get()) if headless is None else bool(headless)
            fields, need_login, _err_detail = scrape(link.platform, link.purchase_order_id, headless=use_headless)

            if need_login:
                link.status = "需登录"
                if notify and (not link.notified_login):
                    link.notified_login = True
                    # TG 推送掉登提醒
                    if hasattr(self, 'purchase_cmd') and self.purchase_cmd:
                        try:
                            self.purchase_cmd.notify_login_required(
                                platform=link.platform,
                                order_id=link.purchase_order_id,
                            )
                        except Exception:
                            pass
                return True

            if link.status == "需登录":
                link.notified_login = False

            if fields.get("pay_amount"):
                link.pay_amount = str(fields["pay_amount"]).strip()
            if fields.get("pay_dt_raw"):
                link.pay_dt_raw = str(fields["pay_dt_raw"]).strip()
                mmdd = _to_mmdd(link.pay_dt_raw)
                if mmdd:
                    link.pay_mmdd = mmdd

            new_tracking = str(fields.get("tracking_no") or "").strip()
            if new_tracking:
                # 只在抓到非空单号时写入，避免覆盖你手填的单号
                link.tracking_no = new_tracking
            link.status = "已获取单号" if link.tracking_no else "等待出货"

            # v6.1.27:訓練 hook — 採購抓取結果(訂單生命週期的關鍵 trigger 信號)
            # 訓練端用 yahoo_order_no 跟對應的對話 trajectory join(用 buyer 或 yahoo order)
            try:
                from core import training_collector as _TC_HOOK
                _yahoo_order = getattr(link, "yahoo_order_no", "") or ""
                _account = getattr(link, "account", "") or ""
                _TC_HOOK.record_event(
                    "order:purchase_scrape",
                    conv=None,
                    conv_key_override=f"order|{_account}|{_yahoo_order or link.purchase_order_id}",
                    profile_id_override=_account,
                    input={
                        "platform": link.platform,
                        "purchase_order_id": link.purchase_order_id,
                        "yahoo_order_no": _yahoo_order,
                    },
                    output={
                        "pay_amount": link.pay_amount,
                        "pay_dt_raw": link.pay_dt_raw,
                        "tracking_no": link.tracking_no,
                        "status": link.status,
                        "has_tracking": bool(link.tracking_no),
                    },
                    metadata={
                        "channel": "purchase_scrape",
                        "checked_at": link.last_checked_at,
                    },
                )
            except Exception:
                pass

            # 一旦信息完整（抓到物流单号），该订单不再需要继续监控：取消勾选 watch，但保留在列表里
            if link.tracking_no:
                if link.watch:
                    link.watch = False
                # 采购已出货（只通知一次）
                if notify and (not link.notified_tracking) and (not prev_tracking):
                    link.notified_tracking = True
                    # TG 推送物流通知
                    if hasattr(self, 'purchase_cmd') and self.purchase_cmd:
                        try:
                            self.purchase_cmd.notify_tracking_found(link)
                        except Exception:
                            pass

            # 自动生成出货任务（并支持：用户手动清空了【订单获取/出货】任务后，可再次由监控推送重建任务）
            # 多采购单号场景：同一个 Yahoo 订单绑了多个采购单号时，
            # 必须等所有采购单号都有物流单号才触发出货生成，避免部分发货就去做资料。
            _all_siblings_have_tracking = False
            if auto_create and link.tracking_no:
                _siblings = [x for x in self.links
                             if x.yahoo_profile_id == link.yahoo_profile_id
                             and x.yahoo_order_no == link.yahoo_order_no]
                _all_siblings_have_tracking = all((s.tracking_no or "").strip() for s in _siblings)
                if not _all_siblings_have_tracking:
                    _missing = [s.purchase_order_id for s in _siblings if not (s.tracking_no or "").strip()]
                    self.log(f"[PURCHASE] {link.yahoo_acc_name}/{link.yahoo_order_no}: 等待其他采购单号发货 (缺{len(_missing)}个)")
            if auto_create and link.tracking_no and _all_siblings_have_tracking:
                # 需要创建的条件：
                # 1) 从未创建过；或
                # 2) 之前创建过，但出货任务被用户手动删除/清空（ship_tasks 里已不存在）
                need_create = (not link.created_ship_task)
                if (not need_create):
                    try:
                        if hasattr(self, '_ship_task_exists') and (not self._ship_task_exists(link.yahoo_profile_id, link.yahoo_order_no)):
                            need_create = True
                    except Exception:
                        pass

                did_create = False
                if need_create:
                    payload = self._make_ship_task_payload(link)
                    ok = False
                    try:
                        # 走原本的回调（保留原有日志/刷新行为）
                        if self.on_create_ship_task:
                            try:
                                self.on_create_ship_task(payload)
                                ok = True
                            except Exception:
                                ok = False
                    except Exception:
                        ok = False
                    if ok:
                        link.created_ship_task = True
                        did_create = True
                        self.log(f"[PURCHASE] 已自动生成出货任务：{link.yahoo_acc_name} / {link.yahoo_order_no}")

                # 自动出货：只有在"任务已存在或已触发创建"时才入队，避免出现"找不到任务，已放弃"
                if auto_create and hasattr(self.app, 'ship_auto_enqueue'):
                    try:
                        exists_now = False
                        try:
                            exists_now = self._ship_task_exists(link.yahoo_profile_id, link.yahoo_order_no)
                        except Exception:
                            exists_now = False
                        if did_create or exists_now:
                            self.app.after(300, lambda pid=link.yahoo_profile_id, ono=link.yahoo_order_no: self.app.ship_auto_enqueue(pid, ono, reason="采购监控获取到物流单号"))
                    except Exception:
                        pass

            return True

        except Exception as e:
            link.error = str(e)
            link.status = "异常"
            return True
    def _ship_task_exists(self, profile_id: str, order_no: str) -> bool:
        """检查【订单获取/出货】任务列表中是否存在该(账号+主订单)。

        用途：当用户手动删除/清空出货任务后，采购监控需要能够再次推送并重建任务，
        避免 created_ship_task=True 导致不再创建，从而出现自动出货"找不到任务"。
        """
        pid = str(profile_id or '').strip()
        ono = str(order_no or '').strip()
        if not pid or not ono:
            return False
        try:
            tasks = getattr(self.app, 'ship_tasks', None)
            if not isinstance(tasks, list):
                return False
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                if str(t.get('profile_id','')).strip() == pid and str(t.get('order_no','')).strip() == ono:
                    return True
        except Exception:
            return False
        return False



    def _make_ship_task_payload(self, link: PurchaseLink) -> dict:
        main_no = (link.yahoo_order_no or "").strip()
        subs = getattr(link, "sub_order_nos", None) or []
        subs = [str(s).strip() for s in subs if str(s).strip()]
        order_disp = main_no
        if subs:
            order_disp = f"{main_no}+" + "+".join(subs)
        shipments = [{
            "tracking_no": (link.tracking_no or "").strip(),
            "purchase_order_id": (link.purchase_order_id or "").strip(),
            "product_name": (link.product_name or "").strip(),
            "spec": (link.spec or "").strip(),
            "remark": (getattr(link, "remark", "") or "").strip(),
            "pay_mmdd": (link.pay_mmdd or "").strip(),
            "pay_amount": _clean_amount(link.pay_amount),
        }]
        return {
            "profile_id": link.yahoo_profile_id,
            "acc_name": link.yahoo_acc_name,
            # 注意：抓取/检测仍以主单号 order_no 为准
            "order_no": main_no,
            # 仅用于 UI 显示（例如：123+234）
            "order_no_display": order_disp,
            "sub_order_nos": subs,
            "remark": (getattr(link, "remark", "") or "").strip(),
            "shipments": shipments,
            # 标记：来自【采购绑定/监控】推送（用于后续生成【最新模板】与顺运宝上传）
            "from_purchase_monitor": True,
            "purchase_platform": str(getattr(link, "platform", "") or "").strip(),
        }

    def _create_ship_task(self, payload: dict) -> bool:
        try:
            app = self.app
            if not hasattr(app, "ship_tasks"):
                return False
            existing = {(t.get("profile_id"), t.get("order_no")) for t in getattr(app, "ship_tasks", [])}
            key = (payload.get("profile_id"), payload.get("order_no"))
            if key in existing:
                # 已存在：合并快递并同步备注（避免备注丢失/导出时落回默认备注）
                try:
                    pr = str(payload.get("remark") or "").strip()
                    od = str(payload.get("order_no_display") or "").strip()
                    subs = payload.get("sub_order_nos") or []
                    try:
                        subs = [str(s).strip() for s in subs if str(s).strip()]
                    except Exception:
                        subs = []
                    new_ships = payload.get("shipments") or []
                    for t in getattr(app, "ship_tasks", []):
                        if (t.get("profile_id"), t.get("order_no")) != key:
                            continue
                        t.setdefault("shipments", [])
                        exist_tr = {str(s.get("tracking_no") or "").strip() for s in (t.get("shipments") or []) if isinstance(s, dict)}
                        for s in new_ships:
                            s = s or {}
                            tr = str(s.get("tracking_no") or "").strip()
                            if tr and tr not in exist_tr:
                                t["shipments"].append(dict(s))
                                exist_tr.add(tr)
                        if pr:
                            t["remark"] = pr
                        # 同步副单显示（不影响实际抓取）
                        if od:
                            t["order_no_display"] = od
                        t["sub_order_nos"] = subs
                        if hasattr(app, "_ship_render_tasks"):
                            app._ship_render_tasks()
                        if hasattr(app, "log"):
                            app.log(f"[SHIP] 已更新 1 条任务（账号：{t.get('acc_name','')}）")
                        return True
                except Exception:
                    return True
                return True
            seq = int(getattr(app, "_ship_task_seq", 0)) + 1
            setattr(app, "_ship_task_seq", seq)

            task = {
                "id": seq,
                "acc_name": payload.get("acc_name", ""),
                "profile_id": payload.get("profile_id", ""),
                "order_no": payload.get("order_no", ""),
                "order_no_display": payload.get("order_no_display", ""),
                "sub_order_nos": payload.get("sub_order_nos", []),
                "remark": str(payload.get("remark") or "").strip(),
                "shipments": [dict(x) for x in payload.get("shipments", [])],
                "status": "待执行",
                "amount": "",
                "channel": "",
                "exec_code": "",
                "error": "",
            }
            app.ship_tasks.append(task)
            if hasattr(app, "_ship_render_tasks"):
                app._ship_render_tasks()
            if hasattr(app, "log"):
                app.log(f"[SHIP] 已新增 1 条任务（账号：{task['acc_name']}）")
            return True
        except Exception:
            return False

    # -----------------
    # build UI
    # -----------------
    def build(self) -> None:
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        lf = ttk.Labelframe(self.frame, text="采购订单绑定")
        lf.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for c in range(7):
            lf.columnconfigure(c, weight=0)
        lf.columnconfigure(1, weight=1)
        lf.columnconfigure(4, weight=1)

        ttk.Label(lf, text="使用方式：选Yahoo账号 → 输入Yahoo订单号 → 选平台 → 填采购订单号 → 点新增绑定 → 开启监控后系统自动抓取物流单号",
                  foreground="gray", wraplength=700, justify="left").grid(row=0, column=0, columnspan=7, sticky="w", padx=4, pady=(4, 2))

        ttk.Label(lf, text="Yahoo账号").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.cmb_account = ttk.Combobox(lf, textvariable=self.var_account, width=18)
        self.cmb_account.grid(row=1, column=1, sticky="ew", padx=4, pady=3)
        ttk.Button(lf, text="刷新账号", command=self._refresh_accounts).grid(row=1, column=2, sticky="w", padx=4, pady=3)

        ttk.Label(lf, text="Yahoo订单号").grid(row=1, column=3, sticky="w", padx=4, pady=3)
        ttk.Entry(lf, textvariable=self.var_yahoo_order).grid(row=1, column=4, sticky="ew", padx=4, pady=3)

        ttk.Label(lf, text="平台").grid(row=1, column=5, sticky="w", padx=4, pady=3)
        self.cmb_platform = ttk.Combobox(lf, textvariable=self.var_platform, values=["xianyu", "mercari"], width=10, state="readonly")
        self.cmb_platform.grid(row=1, column=6, sticky="w", padx=4, pady=3)

        # --- 副订单编号（可多个，最多 5 个）---
        ttk.Label(lf, text="副订单编号").grid(row=2, column=3, sticky="w", padx=4, pady=(0, 3))
        self._sub_orders_holder = ttk.Frame(lf)
        self._sub_orders_holder.grid(row=2, column=4, sticky="ew", padx=4, pady=(0, 3))
        ttk.Button(lf, text="副订单编号＋", command=self._add_sub_order_row).grid(row=2, column=5, sticky="w", padx=4, pady=(0, 3))
        ttk.Button(lf, text="清空副单", command=self._clear_sub_orders).grid(row=2, column=6, sticky="w", padx=4, pady=(0, 3))
        try:
            self._ensure_sub_order_rows()
            self._rebuild_sub_order_rows()
        except Exception:
            pass

        self.purchase_rows_holder = ttk.Frame(lf)
        self.purchase_rows_holder.grid(row=3, column=0, columnspan=7, sticky="ew", padx=4, pady=(2, 4))
        self.purchase_rows_holder.columnconfigure(1, weight=1)
        self.purchase_rows_holder.columnconfigure(2, weight=1)
        self.purchase_rows_holder.columnconfigure(3, weight=1)

        if not self.purchase_rows:
            self.purchase_rows = []
            self._add_purchase_row()

        btnrow = ttk.Frame(lf)
        btnrow.grid(row=4, column=0, columnspan=7, sticky="ew", padx=4, pady=(0, 6))
        ttk.Button(btnrow, style="Accent.TButton", text="新增绑定（多条）", command=self.action_add_bindings).pack(side="left", padx=(0, 8))
        ttk.Button(btnrow, text="打开登录浏览器（闲鱼+煤炉）", command=self.action_open_login_browser).pack(side="left")

        lf_table = ttk.Labelframe(self.frame, text="绑定列表（点击第一列可勾选/取消监控）")
        lf_table.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        lf_table.columnconfigure(0, weight=1)
        lf_table.rowconfigure(0, weight=1)

        cols = ('watch', 'acc', 'yahoo_order', 'platform', 'purchase_order', 'product_name', 'spec', 'remark', 'pay_date', 'pay_amount', 'tracking', 'status', 'last_checked', 'error')
        self.tree = ttk.Treeview(lf_table, columns=cols, show="headings")
        self.tree.bind("<Button-1>", self._on_tree_click)

        headings = {
            "watch": "监控",
            "acc": "账号",
            "yahoo_order": "Yahoo订单",
            "platform": "平台",
            "purchase_order": "采购订单",
            "product_name": "商品名称",
            "spec": "规格",
            "remark": "备注",
            "pay_date": "代付日期",
            "pay_amount": "代付金额",
            "tracking": "物流单号",
            "status": "状态",
            "last_checked": "上次检查",
            "error": "错误",
        }
        widths = {
            "watch": 54,
            "acc": 120,
            "yahoo_order": 110,
            "platform": 70,
            "purchase_order": 150,
            "product_name": 180,
            "spec": 140,
            "remark": 220,
            "pay_date": 90,
            "pay_amount": 90,
            "tracking": 140,
            "status": 90,
            "last_checked": 140,
            "error": 160,
        }
        for c in cols:
            self.tree.heading(c, text=headings.get(c, c))
            self.tree.column(c, width=widths.get(c, 120), anchor="w", stretch=True)
        self.tree.column("watch", anchor="center", stretch=False)
        self.tree.column("platform", anchor="center", stretch=False)
        self.tree.column("pay_date", anchor="center", stretch=False)
        self.tree.column("pay_amount", anchor="e", stretch=False)

        vsb = ttk.Scrollbar(lf_table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(lf_table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        lf_ctl = ttk.Labelframe(self.frame, text="操作")
        lf_ctl.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        lf_ctl.columnconfigure(0, weight=1)

        row1 = ttk.Frame(lf_ctl)
        row1.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        ttk.Button(row1, text="抓取一次（选中）", command=self.action_scrape_selected_once).pack(side="left", padx=(0, 8))
        ttk.Button(row1, style="Accent.TButton", text="发送到订单获取/出货（选中）", command=self.action_send_selected_to_ship).pack(side="left", padx=(0, 8))
        ttk.Button(row1, text="删除选中", command=self.action_delete_selected).pack(side="left", padx=(0, 8))
        ttk.Button(row1, text="编辑选中", command=self._open_edit_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(row1, text="全选监控", command=lambda: self.action_watch_all(True)).pack(side="left", padx=(0, 6))
        ttk.Button(row1, text="全不选", command=lambda: self.action_watch_all(False)).pack(side="left", padx=(0, 8))
        ttk.Button(row1, text="监控设置 ⚙", command=self._open_monitor_settings).pack(side="right", padx=(0, 6))
        ttk.Button(row1, text="开始监控", command=self.action_start).pack(side="right", padx=(0, 6))
        ttk.Button(row1, text="停止监控", command=self.action_stop).pack(side="right", padx=(0, 6))

        self._refresh_accounts()
        self._render_tree()

        self.frame.after(400, self._poll_ui_queue)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "refresh":
                    self.links = load_links()
                    self._render_tree()
        except Exception:
            pass
        finally:
            self.frame.after(400, self._poll_ui_queue)
