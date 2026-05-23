from __future__ import annotations

import queue
import threading
import time
import re
import json
from dataclasses import dataclass
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import ttk, filedialog

import openpyxl


# ======================================================
# 物流系统：没有 API 权限时的"网页 UI 自动化"方案
# ======================================================
# 目标：
# - 账号/密码写死在代码里，界面不暴露
# - 浏览器长期挂着，尽量复用 session（减少验证码出现频率）
# - 上传资料：店配 / 线下(宅配)
# - 上传面单：批量上传 PDF（文件名=订单编号.pdf）
# - 查询物流记录：判断是否出现「已发货」
#
# 重要：不做"自动识别验证码/替代路径验证码"。验证码属于网站的安全校验，
#       自动破解/识别通常违反服务条款且极不稳定。
#       这里采用"长期保持 session + 必要时人工输入一次验证码"的最稳方案。


BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = (BASE_DIR / "output")


SYB_BASE_URL = "https://shunyunbaoerp.com"
SYB_LOGIN_URLS = [
    f"{SYB_BASE_URL}/sys/login",
    f"{SYB_BASE_URL}/login",
]

SYB_STOCK_URL = f"{SYB_BASE_URL}/sys/admin/stock"
SYB_IMPORT_URL = f"{SYB_BASE_URL}/sys/admin/stock/import"

SYB_HEADLESS = True  # True=不弹出浏览器窗口；若被站点拦截，可改 False 并配合"隐藏窗口"参数
SYB_GOTO_TIMEOUT_MS = 45_000
SYB_WAIT_SELECTOR_MS = 15_000


SYB_REDACT_URL_IN_LOG = True  # 隐藏日志中的完整 URL（避免泄露站点地址/页面参数）
SYB_DEBUG_FULLPAGE_CAPTURE = False  # 是否保存"整页诊断截图"（默认关闭，避免泄露页面内容）


# 你指定的固定账号密码（界面不展示）
SYB_USERNAME = "<SYB_USER_REDACTED>"
SYB_PASSWORD = "<SYB_PASSWORD_REDACTED>"

# 你的模板 sheet 名称（来自 latest_template_builder.py）
SHEET_STORE = "线上贴单资料"   # 店配
SHEET_HOME = "宅配打包资料"    # 线下(宅配)


def _now_ts() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


_URL_RE = re.compile(r"https?://[^\s\]>)'\"，,;]+", re.IGNORECASE)

def _sanitize_log_text(s: str) -> str:
    """日志脱敏：默认把完整 URL 替换为 <URL_REDACTED>。"""
    try:
        if not isinstance(s, str):
            s = str(s)
    except Exception:
        return ""
    if SYB_REDACT_URL_IN_LOG:
        try:
            s = _URL_RE.sub("<URL_REDACTED>", s)
        except Exception:
            pass
        try:
            # 兜底：把 base url 的裸字符串也替换掉
            s = s.replace(SYB_BASE_URL, "<URL_REDACTED>")
        except Exception:
            pass
    return s


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _copy_single_sheet(src_xlsx: Path, sheet_name: str, dst_xlsx: Path) -> Tuple[int, int]:
    """把 src_xlsx 的指定 sheet 复制成一个"只有 1 张 sheet"的新文件。

    说明：
    - 之前直接用 ws.max_column / ws.max_row 可能会被"模板样式范围"误伤（例如 max_column=16383），
      导致生成的导入文件巨大、且后续网页端上传选择器变慢甚至超时。
    - 这里用"前几行表头/数据的实际非空内容"推断有效列数，再扫描有效行数。

    返回：(rows, cols) 便于写日志。
    """
    wb = openpyxl.load_workbook(src_xlsx, read_only=True, data_only=False)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"Excel 不存在 sheet：{sheet_name}，现有：{wb.sheetnames}")
        ws = wb[sheet_name]

        # --- 1) 推断有效列数（优先看表头/前几行内容）
        MAX_SCAN_COL = 512  # 够用；避免模板把整行样式刷到 16383 列导致爆炸
        scan_rows = min(5, ws.max_row or 1)
        eff_max_col = 0

        for row in ws.iter_rows(min_row=1, max_row=scan_rows, max_col=MAX_SCAN_COL, values_only=True):
            for c_idx, v in enumerate(row, start=1):
                if v not in (None, ""):
                    if c_idx > eff_max_col:
                        eff_max_col = c_idx

        if eff_max_col <= 0:
            # 兜底：至少取一个合理上限
            eff_max_col = min(ws.max_column or 0, MAX_SCAN_COL) or min(200, MAX_SCAN_COL)

        # --- 2) 推断有效行数（只扫前 N 行，避免 ws.max_row 被样式撑爆）
        MAX_SCAN_ROW = 20000  # 足够覆盖日常批量导入
        max_row_scan = min(ws.max_row or 0, MAX_SCAN_ROW)
        eff_max_row = 0

        for r_idx, row in enumerate(
            ws.iter_rows(min_row=1, max_row=max_row_scan, max_col=eff_max_col, values_only=True),
            start=1,
        ):
            if any(v not in (None, "") for v in row):
                eff_max_row = r_idx

        if eff_max_row <= 0:
            eff_max_row = min(ws.max_row or 1, 1)

        # --- 3) 写出单 sheet 文件（只写值，更稳、更快）
        out = openpyxl.Workbook()
        out_ws = out.active
        out_ws.title = sheet_name

        for r_idx, row in enumerate(
            ws.iter_rows(min_row=1, max_row=eff_max_row, max_col=eff_max_col, values_only=True),
            start=1,
        ):
            for c_idx, v in enumerate(row, start=1):
                if v in (None, ""):
                    continue
                out_ws.cell(row=r_idx, column=c_idx, value=v)

        dst_xlsx.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst_xlsx)
        return eff_max_row, eff_max_col
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _has_data_rows(xlsx: Path, sheet_name: str) -> bool:
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    if sheet_name not in wb.sheetnames:
        return False
    ws = wb[sheet_name]
    # 认为第 1 行是表头：只要第 2 行开始有任意一个单元格有值，就视为有数据
    if ws.max_row and ws.max_row >= 2:
        for row in ws.iter_rows(min_row=2, max_row=2, values_only=True):
            if any(v not in (None, "") for v in row):
                return True
    return False




# ---------------- SYB login helpers ----------------

def _syb_try_goto_login(page, log: Callable[[str], None]) -> None:
    """尽量稳地打开登录页。优先 /sys/login，其次 /login。"""
    last_err: Optional[Exception] = None
    for url in SYB_LOGIN_URLS:
        for attempt in range(1, 4):
            try:
                log(f"[{_now_ts()}] [SYB] 导航：登录页（尝试 {attempt}/3）")
                page.goto(url, wait_until="commit", timeout=SYB_GOTO_TIMEOUT_MS)
                # 页面可能没触发 domcontentloaded，这里直接等关键 input 出现
                page.wait_for_selector('#app input, input[placeholder], form input', timeout=SYB_WAIT_SELECTOR_MS)
                return
            except Exception as e:
                last_err = e
                # 小等一下再试
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                continue
    raise last_err or RuntimeError("goto login failed")


def _syb_fill_credentials(page) -> None:
    # 账号/密码：优先用 placeholder 定位（更稳），再回退到 form input 顺序
    u = page.locator('input[placeholder*="用户"]')
    p = page.locator('input[placeholder*="密码"]')
    if u.count() >= 1:
        u.first.fill(SYB_USERNAME)
    if p.count() >= 1:
        p.first.fill(SYB_PASSWORD)

    # 仅在用户名和密码都无法通过 placeholder 定位时，才退回到顺序填充（避免把账号/密码误填到验证码输入框）
    if u.count() == 0 and p.count() == 0:
        inputs = page.locator("form input")
        if inputs.count() >= 2:
            inputs.nth(0).fill(SYB_USERNAME)
            inputs.nth(1).fill(SYB_PASSWORD)


def _syb_locate_captcha_img(page):
    # 尽量稳地定位验证码图片元素（只截小块）
    # 兼容：
    # - src 相对路径：/api/pcode...
    # - src 绝对路径：https://.../api/pcode...
    # - 少数情况下 src 可能是 data:（用"验证码输入框附近"兜底）

    # 1) 最稳：src 包含 pcode（相对/绝对都能命中）
    for sel in (
        "xpath=//img[contains(@src,'pcode')]",
        'img[src*="pcode"]',
        '#app img[src*="pcode"]',
        'img[src^="/api/pcode"]',
        'img[src*="/api/pcode"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() >= 1:
                return loc.first
        except Exception:
            pass

    # 2) 兜底：从"验证码输入框"附近找同一行的 img
    try:
        cap = page.locator('input[placeholder*="验证码"]')
        if cap.count() >= 1:
            # 你 DevTools 里这一行通常是 style="...; display: flex;"
            row = cap.first.locator(
                "xpath=ancestor::*[self::div and contains(@style,'display') and contains(@style,'flex')][1]"
            )
            imgs = row.locator("img")
            if imgs.count() >= 1:
                return imgs.first

            # 再兜底：DOM 中紧跟着的 img
            near = cap.first.locator("xpath=following::img[1]")
            if near.count() >= 1:
                return near.first
    except Exception:
        pass

    # 3) 最后兜底：挑一个"尺寸像验证码"的小图（避免选到背景大图）
    try:
        imgs = page.locator("img")
        try:
            n = min(imgs.count(), 30)
        except Exception:
            n = 0
        for i in range(n):
            el = imgs.nth(i)
            try:
                box = el.bounding_box()
                if not box:
                    continue
                w = float(box.get("width", 0) or 0)
                h = float(box.get("height", 0) or 0)
                if 60 <= w <= 220 and 20 <= h <= 90:
                    return el
            except Exception:
                continue
    except Exception:
        pass

    return None


def _syb_capture_captcha_png(page, out_path: Path, log: Callable[[str], None]) -> bool:
    # 截取验证码小图到 out_path。成功返回 True。
    img = _syb_locate_captcha_img(page)
    if img is None:
        # 轻量诊断（不涉及账号密码）
        try:
            srcs = page.eval_on_selector_all(
                "img",
                "els => els.map(e => e.getAttribute('src') || '').slice(0, 12)"
            )
            log(f"[{_now_ts()}] [SYB] 未找到验证码img（已省略页面内容），请确认登录页已加载且验证码已显示。")
        except Exception:
            pass
        return False

    try:
        img.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass

    try:
        # 等它可见，避免刚插入 DOM 但未渲染
        try:
            img.wait_for(state="visible", timeout=10_000)
        except Exception:
            pass

        _safe_mkdir(out_path.parent)
        img.screenshot(path=str(out_path))

        # 粗检：文件过小通常说明截空
        try:
            if out_path.exists() and out_path.stat().st_size > 200:
                return True
        except Exception:
            pass
        return out_path.exists()
    except Exception as e:
        log(f"[{_now_ts()}] [SYB] 验证码截图失败：{e}")
        return False
    try:
        img.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass
    try:
        _safe_mkdir(out_path.parent)
        img.screenshot(path=str(out_path))
        return True
    except Exception as e:
        log(f"[{_now_ts()}] [SYB] 验证码截图失败：{e}")
        return False

def _syb_is_login_page(page) -> bool:
    """粗判断：当前是否仍在登录页（含"账号输入框被隐藏"的情况）。"""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    u = url.lower()
    if "/sys/login" in u or u.rstrip("/").endswith("/login"):
        return True

    # 某些情况下账号输入框可能被站点隐藏（只显示"密码+验证码"）。
    # 这时不能再依赖"用户/密码"同时存在来判断。
    try:
        pwd = page.locator('input[placeholder*="密码"]')
        cap = page.locator('input[placeholder*="验证"], input[placeholder*="证码"]')
        reg = page.locator("text=注册会员")
        if reg.count() > 0:
            return True
        if pwd.count() > 0 and cap.count() > 0:
            return True
    except Exception:
        pass

    try:
        uu = page.locator('input[placeholder*="用户"]')
        pp = page.locator('input[placeholder*="密码"]')
        if uu.count() >= 1 and pp.count() >= 1:
            return True
    except Exception:
        pass

    return False


def _syb_collect_login_error(page) -> str:
    """收集页面上可能出现的登录错误/提示（ElementUI toast、表单错误等）。"""
    parts: List[str] = []
    sels = [
        ".el-message__content",
        ".el-notification__content",
        ".el-alert__content",
        ".el-form-item__error",
        "[role='alert']",
    ]
    for sel in sels:
        try:
            loc = page.locator(sel)
            n = 0
            try:
                n = min(loc.count(), 3)
            except Exception:
                n = 0
            for i in range(n):
                try:
                    t = (loc.nth(i).inner_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    parts.append(t)
        except Exception:
            continue

    # 去重并拼接
    out: List[str] = []
    for p in parts:
        if p not in out:
            out.append(p)
    return " / ".join(out)


@dataclass
class _Task:
    fn: Callable[[Any], Any]
    desc: str




@dataclass
class _ShipMonitorItem:
    """发货监控条目（本地持久化）。"""
    profile_id: str
    account_name: str
    order_no: str
    added_at: float
    next_check_at: float
    active: bool = True
    last_status: str = ""
    last_checked_at: float = 0.0
    source: str = ""


class SYBWebAgent:
    """Playwright 常驻线程：保持 session，串行执行网页操作。"""

    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._q: "queue.Queue[_Task]" = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._pw = None
        self._ctx = None
        self._page = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SYBWebAgent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self._stop.set()
        except Exception:
            pass

    def submit(self, fn: Callable[[Any], Any], desc: str) -> None:
        self.start()
        self._q.put(_Task(fn=fn, desc=desc))

    def _run(self) -> None:
        try:
            from .client_runtime_compat import sync_playwright, apply_runtime_normalization_sync, get_launch_args, get_ignore_default_args, CHROME_UA

            user_data_dir = (BASE_DIR / "profiles" / "_syb_web_session").resolve()
            _safe_mkdir(user_data_dir)

            with sync_playwright() as pw:
                self._pw = pw

                def launch_ctx():
                    ctx = pw.chromium.launch_persistent_context(
                        user_data_dir=str(user_data_dir),
                        headless=SYB_HEADLESS,
                        args=get_launch_args(headless=SYB_HEADLESS, lang="zh-CN", extra=[
                            "--disable-features=IsolateOrigins,site-per-process",
                            "--window-position=-32000,-32000",
                            "--window-size=900,700",
                            "--start-minimized",
                        ]),
                        ignore_default_args=get_ignore_default_args(headless=SYB_HEADLESS),
                        locale="zh-CN",
                        user_agent=CHROME_UA,
                        no_viewport=True,
                    )

                    self._ctx = ctx
                    apply_runtime_normalization_sync(ctx)
                    self._ready.set()
                    self._log(f"[{_now_ts()}] [SYB] WebAgent 已启动（session dir: {user_data_dir}）")
                    return ctx

                def get_live_page(ctx):
                    # 优先拿现有"活着"的 page（倒序取最后一个更像当前活跃页）
                    try:
                        pages = list(ctx.pages)
                    except Exception:
                        pages = []
                    for p in reversed(pages):
                        try:
                            if not p.is_closed():
                                return p
                        except Exception:
                            continue
                    # 没有就新建
                    return ctx.new_page()

                ctx = launch_ctx()

                last_ping = 0.0
                while not self._stop.is_set():
                    # 保活:每 8 分钟轻触一次(用"活 page",别用旧引用)
                    now = time.time()
                    if now - last_ping >= 8 * 60:
                        last_ping = now
                        try:
                            page = get_live_page(ctx)
                            cur_url = page.url or ""
                            if cur_url.startswith(SYB_BASE_URL):
                                page.evaluate("() => Date.now()")
                                # v6.0.61: 删除「定期刷新 stoken 缓存」逻辑 —
                                # 之前从 Playwright 抓 cookie 写 stoken_cache.json,
                                # 但 Playwright 的 stoken 跟 HTTP API 用的 jwt 可能不同源,
                                # 写进去会污染 HTTP 路径的 cache,导致 HTTP 401 但 cache 看似有效。
                                # 现在让 cache 只由 HTTP auto_login 写入,纯净不互相污染。
                        except Exception:
                            pass

                    try:
                        task = self._q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    self._log(f"[{_now_ts()}] [SYB] 开始：{task.desc}")

                    # 失败时：如果是 page/context/browser 被关，自动重启并重试一次
                    for attempt in (1, 2):
                        try:
                            page = get_live_page(ctx)
                            task.fn(page)
                            self._log(f"[{_now_ts()}] [SYB] 完成：{task.desc}")
                            break
                        except Exception as e:
                            msg = str(e) if e is not None else ""
                            closed = (
                                "Target page, context or browser has been closed" in msg
                                or "has been closed" in msg
                            )
                            if attempt == 1 and closed:
                                self._log(f"[{_now_ts()}] [SYB] 检测到浏览器/页面已关闭，正在重启会话窗口…")
                                try:
                                    ctx.close()
                                except Exception:
                                    pass
                                ctx = launch_ctx()
                                continue

                            self._log(f"[{_now_ts()}] [SYB] 失败：{task.desc} -> {e}")
                            break

                try:
                    ctx.close()
                except Exception:
                    pass

        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB] WebAgent 启动失败：{e}")

class ShunyunbaoUploadFeatureTab:
    """物流系统：网页 UI 自动化（无 API 权限的替代方案）。"""

    def __init__(self, app: Any, frame: tk.Widget):
        self.app = app
        self.frame = frame

        # 常驻浏览器 agent
        self._agent = SYBWebAgent(self._log)
        self._check_results: dict[str, bool] = {}

        # UI
        self.var_status = tk.StringVar(value="会话：未启动")
        self.var_pdf_dir = tk.StringVar(
            value=str(getattr(app, "settings", {}).get("label_pdf_dir", "") or ""))
        self.var_orders = tk.StringVar(value="")
        self.var_captcha = tk.StringVar(value="")

        # 验证码图片（只展示小块验证码）
        self._captcha_path = (BASE_DIR / "output" / "syb_captcha.png").resolve()
        self._captcha_ts = 0.0  # 验证码抓取时间戳（避免过期）
        self._captcha_session_token = ""  # HTTP 获取验证码时返回的 session token
        self._debug_page_path = (BASE_DIR / "output" / "syb_debug_page.png").resolve()
        self._captcha_photo = None  # 需要保持引用，否则 Tk 会清掉图片
        self._lbl_captcha_img = None

        # ---------------- 自动列印面单 ----------------
        self.var_auto_label_store = tk.BooleanVar(value=True)
        self.var_auto_label_home = tk.BooleanVar(value=True)
        self.var_label_trigger = tk.StringVar(value="已发货")  # 已揽收/已发货/转运中
        self.lbl_label_stats = None

        # ---------------- 发货监控（自动轮询） ----------------
        # 默认：3 小时检查一次；每次检查"监控列表全部订单"（逐单查询，轮着查）
        self.var_mon_enabled = tk.BooleanVar(value=True)
        self.var_mon_interval_hours = tk.StringVar(value="3")
        self.var_mon_acc = tk.StringVar(value="")
        self.var_mon_order = tk.StringVar(value="")
        self.var_mon_status = tk.StringVar(value="")

        # key=order_no
        self._mon_items: dict[str, _ShipMonitorItem] = {}
        self._mon_order_ring = deque()
        self._mon_running = False
        self._mon_tree = None
        self._mon_state_path = (BASE_DIR / "output" / "syb_ship_monitor.json").resolve()
        self._mon_acc_combo = None
        self._mon_acc_display_map: dict[str, tuple[str, str]] = {}  # display -> (profile_id, account_name)
        self._mon_tick_started = False


    # ---------------- logging ----------------

    def _ensure_agent(self) -> None:
        """确保后台 WebAgent 已启动（headless 模式也可用）。
        这个方法只做"兜底"，避免 UI 回调因缺少方法直接崩溃。
        """
        try:
            if getattr(self, "_agent", None) is None:
                raise RuntimeError("WebAgent 尚未初始化")
            # start() 内部是幂等的：已启动会直接返回
            self._agent.start()
        except Exception as e:
            try:
                self._log(f"[SYB] 初始化失败：{e}")
            except Exception:
                pass
            raise

    def _log(self, msg: str) -> None:
        msg = _sanitize_log_text(msg)
        try:
            if hasattr(self.app, "log"):
                self.app.log(msg)
        except Exception:
            pass
        try:
            if hasattr(self, "txt") and self.txt:
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
        except Exception:
            pass

    
    def _ui_call(self, fn: Callable[[], None]) -> None:
        """确保在 Tk 主线程更新 UI。"""
        try:
            self.frame.after(0, fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _ui_update_captcha_image(self) -> None:
        def do():
            if not self._lbl_captcha_img:
                return
            if not self._captcha_path.exists():
                return
            try:
                img = tk.PhotoImage(file=str(self._captcha_path))
                self._captcha_photo = img
                self._lbl_captcha_img.configure(image=img)
            except Exception as e:
                self._log(f"[{_now_ts()}] [SYB] 更新验证码图片失败：{e}")
        self._ui_call(do)

    def _toggle_login_ui(self, logged_in: bool) -> None:
        """已登录时隐藏验证码/登录行，未登录时显示。"""
        def do():
            row = getattr(self, "_login_row", None)
            if not row:
                return
            if logged_in:
                row.grid_remove()
            else:
                row.grid()
        self._ui_call(do)

# ---------------- UI ----------------

    def build(self):
        root = self.frame
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        lf = ttk.Labelframe(root, text="物流系统（网页自动化）")
        lf.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 3))
        lf.columnconfigure(1, weight=1)

        # 会话控制（说明 + 状态合并为一行）
        row0 = ttk.Frame(lf)
        row0.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=(4, 1))
        ttk.Label(row0, text="流程：获取验证码→登录→上传资料/面单→自动监控",
                  foreground="gray").pack(side="left")
        ttk.Label(row0, textvariable=self.var_status, foreground="#555").pack(side="right")

        btns = ttk.Frame(lf)
        btns.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 3))
        self._login_row = btns  # 保存引用，用于登录后隐藏

        left = ttk.Frame(btns)
        left.pack(side="left", fill="x", expand=True)

        right = ttk.Frame(btns)
        right.pack(side="right")

        # 验证码小图（只显示这一小块）
        self._lbl_captcha_img = ttk.Label(right)
        self._lbl_captcha_img.pack(side="right", padx=(8, 0))

        ttk.Button(left, text="获取验证码", command=self._ui_get_captcha).pack(side="left")
        ttk.Label(left, text="验证码：").pack(side="left", padx=(8, 2))
        ttk.Entry(left, textvariable=self.var_captcha, width=10).pack(side="left")
        ttk.Button(left, text="登录", command=self._ui_login).pack(side="left", padx=(8, 0))
        ttk.Button(left, text="刷新验证码", command=self._ui_refresh_captcha).pack(side="left", padx=(8, 0))

        # 上传资料 + 面单上传
        up = ttk.Frame(lf)
        up.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 3))
        ttk.Button(up, text="上传店配资料", command=self._ui_upload_store).pack(side="left")
        ttk.Button(up, text="上传宅配资料", command=self._ui_upload_home).pack(side="left", padx=(8, 0))
        ttk.Button(up, text="上传面单", command=self._ui_upload_labels).pack(side="left", padx=(8, 0))

        # 自动列印面单
        label_row = ttk.Frame(lf)
        label_row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 3))
        ttk.Checkbutton(label_row, text="自动列印(店配)", variable=self.var_auto_label_store,
                        command=self._sync_label_settings).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(label_row, text="自动查询码(宅配)", variable=self.var_auto_label_home,
                        command=self._sync_label_settings).pack(side="left", padx=(0, 6))
        ttk.Separator(label_row, orient="vertical").pack(side="left", fill="y", padx=(4, 8), pady=2)
        ttk.Label(label_row, text="触发:").pack(side="left")
        ttk.Radiobutton(label_row, text="已揽收", variable=self.var_label_trigger, value="已揽收",
                        command=self._sync_label_settings).pack(side="left", padx=(2, 4))
        ttk.Radiobutton(label_row, text="已发货", variable=self.var_label_trigger, value="已发货",
                        command=self._sync_label_settings).pack(side="left", padx=(0, 4))
        ttk.Radiobutton(label_row, text="转运中", variable=self.var_label_trigger, value="转运中",
                        command=self._sync_label_settings).pack(side="left", padx=(0, 6))
        ttk.Separator(label_row, orient="vertical").pack(side="left", fill="y", padx=(4, 8), pady=2)
        self.lbl_label_stats = ttk.Label(label_row, text="待:0 中:0 完:0 败:0")
        self.lbl_label_stats.pack(side="left", padx=(0, 0))

        # 日志 + 发货监控（左右分栏）
        paned = ttk.Panedwindow(root, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        loglf = ttk.Labelframe(paned, text="物流系统日志")
        loglf.rowconfigure(0, weight=1)
        loglf.columnconfigure(0, weight=1)

        monlf = ttk.Labelframe(paned, text="发货监控（自动轮询）")
        monlf.columnconfigure(0, weight=1)

        paned.add(loglf, weight=1)
        paned.add(monlf, weight=2)

        # 日志文本框（缩窄：让右侧监控有更多空间）
        self.txt = tk.Text(loglf, height=10, width=25)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(loglf, orient="vertical", command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=sb.set)

        # 监控面板
        topbar = ttk.Frame(monlf)
        topbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        topbar.columnconfigure(3, weight=1)

        ttk.Checkbutton(topbar, text="启用", variable=self.var_mon_enabled, command=self._mon_on_toggle).grid(row=0, column=0, sticky="w")
        ttk.Label(topbar, text="间隔(h):").grid(row=0, column=1, sticky="w", padx=(6, 2))
        ttk.Entry(topbar, textvariable=self.var_mon_interval_hours, width=3).grid(row=0, column=2, sticky="w")
        ttk.Label(topbar, textvariable=self.var_mon_status, foreground="#555").grid(row=0, column=3, sticky="e", padx=(6, 0))
        ttk.Button(topbar, text="立即检查", command=self._mon_check_all_now).grid(row=0, column=4, sticky="e", padx=(6, 0))

        addbar = ttk.Frame(monlf)
        addbar.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))
        addbar.columnconfigure(1, weight=1)

        ttk.Label(addbar, text="账号:").grid(row=0, column=0, sticky="w")
        self._mon_acc_combo = ttk.Combobox(addbar, textvariable=self.var_mon_acc, width=12, state="readonly")
        self._mon_acc_combo.grid(row=0, column=1, sticky="ew", padx=(2, 4))
        ttk.Label(addbar, text="单号:").grid(row=0, column=2, sticky="w")
        ttk.Entry(addbar, textvariable=self.var_mon_order, width=10).grid(row=0, column=3, sticky="w", padx=(2, 4))
        ttk.Button(addbar, text="绑定", command=self._mon_add_manual).grid(row=0, column=4, sticky="w")
        ttk.Button(addbar, text="移除", command=self._mon_remove_selected).grid(row=0, column=5, sticky="w", padx=(4, 0))

        cols = ("order_no", "account", "next", "status")
        tree = ttk.Treeview(monlf, columns=cols, show="headings", height=10, selectmode="extended")
        tree.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        monlf.rowconfigure(2, weight=1)
        self._mon_tree = tree
        tree.heading("order_no", text="订单号")
        tree.heading("account", text="账号")
        tree.heading("next", text="下次检查")
        tree.heading("status", text="状态")
        tree.column("order_no", width=100, anchor="w", stretch=True)
        tree.column("account", width=100, anchor="w", stretch=True)
        tree.column("next", width=100, anchor="w", stretch=True)
        tree.column("status", width=100, anchor="w", stretch=True)

        sb2 = ttk.Scrollbar(monlf, orient="vertical", command=tree.yview)
        sb2.grid(row=2, column=1, sticky="ns", pady=(0, 6))
        tree.configure(yscrollcommand=sb2.set)

        # 初始化监控状态/任务
        self._mon_refresh_accounts()
        self._mon_load_state()
        self._mon_refresh_tree()
        self._mon_start_tick()

        # 启动时自动检查 stoken 有效性（后台线程，不阻塞 UI）
        self._check_stoken_on_start()

    # ---------------- 自动列印面单 UI 回调 ----------------

    def _sync_label_settings(self) -> None:
        alw = getattr(self.app, "auto_label_worker", None)
        if not alw:
            return
        alw.enabled_store = self.var_auto_label_store.get()
        alw.enabled_home = self.var_auto_label_home.get()
        alw.trigger_level = self.var_label_trigger.get()  # "已揽收"/"已发货"/"转运中"
        pdf_dir = self.var_pdf_dir.get().strip()
        if pdf_dir:
            alw.pdf_root = pdf_dir

    def _update_label_stats(self) -> None:
        alw = getattr(self.app, "auto_label_worker", None)
        if not alw:
            return
        s = alw.get_stats()
        try:
            if self.lbl_label_stats:
                self.lbl_label_stats.config(
                    text=f"待:{s['pending']} 中:{s['processing']} 完:{s['done']} 败:{s['failed']}")
        except Exception:
            pass


    # ---------------- 发货监控（自动轮询） ----------------

    def _mon_update_status(self, msg: str) -> None:
        try:
            self.var_mon_status.set(msg)
        except Exception:
            pass

    def _mon_on_toggle(self) -> None:
        # 仅保存开关状态
        try:
            self._mon_save_state()
            self._mon_update_status("已启用" if self.var_mon_enabled.get() else "已停用")
        except Exception:
            pass

    def _mon_refresh_accounts(self) -> None:
        """刷新账号下拉框：显示为 'name (profile_id)'。"""
        mp: dict[str, tuple[str, str]] = {}
        vals: List[str] = []

        # 优先使用 app.accounts（更完整）
        accs = []
        try:
            accs = list(getattr(self.app, "accounts", []) or [])
        except Exception:
            accs = []

        if accs:
            for a in accs:
                try:
                    pid = str(a.get("profile_id") or "").strip()
                    name = str(a.get("name") or pid or "").strip()
                    if not pid:
                        continue
                    disp = f"{name} ({pid})" if name and name != pid else pid
                    mp[disp] = (pid, name or pid)
                    vals.append(disp)
                except Exception:
                    continue
        else:
            # 兜底：用 states
            try:
                stmap = getattr(self.app, "states", {}) or {}
                for pid, st in stmap.items():
                    try:
                        pid2 = str(getattr(st, "profile_id", pid) or "").strip()
                        name = str(getattr(st, "name", pid2) or "").strip()
                        if not pid2:
                            continue
                        disp = f"{name} ({pid2})" if name and name != pid2 else pid2
                        mp[disp] = (pid2, name or pid2)
                        vals.append(disp)
                    except Exception:
                        continue
            except Exception:
                pass

        self._mon_acc_display_map = mp

        try:
            if self._mon_acc_combo is not None:
                self._mon_acc_combo["values"] = vals
        except Exception:
            pass

        cur = (self.var_mon_acc.get() or "").strip()
        if (not cur) or (cur not in mp and cur not in vals):
            if vals:
                try:
                    self.var_mon_acc.set(vals[0])
                except Exception:
                    pass

    def _mon_get_interval_sec(self) -> float:
        raw = (self.var_mon_interval_hours.get() or "").strip()
        try:
            h = float(raw)
        except Exception:
            h = 3.0
        if h <= 0:
            h = 3.0
        return h * 3600.0

    def _mon_save_state(self) -> None:
        try:
            _safe_mkdir(self._mon_state_path.parent)
            data = {
                "enabled": bool(self.var_mon_enabled.get()),
                "interval_hours": str(self.var_mon_interval_hours.get() or "3"),
                "ring": list(self._mon_order_ring),
                "items": {},
            }
            for ono, it in (self._mon_items or {}).items():
                data["items"][ono] = {
                    "profile_id": it.profile_id,
                    "account_name": it.account_name,
                    "order_no": it.order_no,
                    "added_at": float(it.added_at or 0),
                    "next_check_at": float(it.next_check_at or 0),
                    "active": bool(it.active),
                    "last_status": str(it.last_status or ""),
                    "last_checked_at": float(it.last_checked_at or 0),
                    "source": str(it.source or ""),
                }
            self._mon_state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            try:
                self._log(f"[{_now_ts()}] [SYB-MON] 保存监控状态失败：{e}")
            except Exception:
                pass

    def _mon_load_state(self) -> None:
        try:
            if not self._mon_state_path.exists():
                return
            data = json.loads(self._mon_state_path.read_text(encoding="utf-8") or "{}")

            try:
                self.var_mon_enabled.set(bool(data.get("enabled", True)))
            except Exception:
                pass
            try:
                self.var_mon_interval_hours.set(str(data.get("interval_hours", "3")))
            except Exception:
                pass

            items: dict[str, _ShipMonitorItem] = {}
            raw_items = data.get("items", {}) or {}
            for ono, v in raw_items.items():
                try:
                    ono2 = str(v.get("order_no") or ono or "").strip()
                    if not ono2:
                        continue
                    items[ono2] = _ShipMonitorItem(
                        profile_id=str(v.get("profile_id") or "").strip(),
                        account_name=str(v.get("account_name") or "").strip(),
                        order_no=ono2,
                        added_at=float(v.get("added_at") or 0),
                        next_check_at=float(v.get("next_check_at") or 0),
                        active=bool(v.get("active", True)),
                        last_status=str(v.get("last_status") or ""),
                        last_checked_at=float(v.get("last_checked_at") or 0),
                        source=str(v.get("source") or ""),
                    )
                except Exception:
                    continue
            self._mon_items = items

            ring = []
            try:
                ring = list(data.get("ring") or [])
            except Exception:
                ring = []
            # 只保留 ring 中仍存在的订单，并补齐缺失
            ring2 = [str(x).strip() for x in ring if str(x).strip() in items]
            for ono in items.keys():
                if ono not in ring2:
                    ring2.append(ono)
            self._mon_order_ring = deque(ring2)

        except Exception as e:
            try:
                self._log(f"[{_now_ts()}] [SYB-MON] 加载监控状态失败：{e}")
            except Exception:
                pass

    def _mon_refresh_tree(self) -> None:
        tree = self._mon_tree
        if tree is None:
            return
        try:
            for iid in tree.get_children():
                tree.delete(iid)
        except Exception:
            pass

        def _fmt(ts: float) -> str:
            if not ts:
                return "-"
            try:
                return time.strftime("%m-%d %H:%M", time.localtime(ts))
            except Exception:
                return "-"

        # 按 ring 顺序展示
        for ono in list(self._mon_order_ring):
            it = (self._mon_items or {}).get(ono)
            if not it or not it.active:
                continue
            acc = f"{it.account_name} ({it.profile_id})" if it.account_name and it.profile_id else (it.account_name or it.profile_id or "")
            nxt = _fmt(it.next_check_at)
            st = it.last_status or ""
            try:
                tree.insert("", "end", iid=ono, values=(ono, acc, nxt, st))
            except Exception:
                try:
                    tree.insert("", "end", values=(ono, acc, nxt, st))
                except Exception:
                    pass

        # 状态行提示
        try:
            n = sum(1 for it in (self._mon_items or {}).values() if it and it.active)
            self._mon_update_status(f"监控中：{n} 单")
        except Exception:
            pass

    def _mon_get_next_cycle_at(self) -> float:
        """取当前监控队列的"统一下次检查时间"（让新加入的单跟着大部队一起查）。"""
        next_at = 0.0
        try:
            cands = [float(it.next_check_at or 0) for it in (self._mon_items or {}).values() if it and it.active and float(it.next_check_at or 0) > 0]
            if cands:
                next_at = min(cands)
        except Exception:
            next_at = 0.0
        return next_at

    def _mon_add_items_bulk(self, profile_id: str, account_name: str, order_nos: Iterable[str], source: str = "") -> int:
        """批量加入监控（幂等去重）。返回新增数量。"""
        now = time.time()
        interval = self._mon_get_interval_sec()
        next_at = self._mon_get_next_cycle_at()
        if next_at <= 0:
            next_at = now + interval

        added = 0
        for raw in (order_nos or []):
            ono = str(raw or "").strip()
            if not ono:
                continue

            it = (self._mon_items or {}).get(ono)
            if it is None:
                self._mon_items[ono] = _ShipMonitorItem(
                    profile_id=str(profile_id or "").strip(),
                    account_name=str(account_name or "").strip(),
                    order_no=ono,
                    added_at=now,
                    next_check_at=next_at,
                    active=True,
                    last_status="",
                    last_checked_at=0.0,
                    source=source or "",
                )
                if ono not in self._mon_order_ring:
                    self._mon_order_ring.append(ono)
                added += 1
            else:
                # 重新启用 / 对齐下一轮时间
                it.profile_id = str(profile_id or it.profile_id or "").strip()
                it.account_name = str(account_name or it.account_name or "").strip()
                it.active = True
                it.next_check_at = next_at
                if source:
                    it.source = source
                if ono not in self._mon_order_ring:
                    self._mon_order_ring.append(ono)

        if added > 0:
            self._mon_save_state()
            self._mon_refresh_tree()
        return added

    def _mon_add_item(self, profile_id: str, account_name: str, order_no: str, source: str = "") -> None:
        self._mon_add_items_bulk(profile_id, account_name, [order_no], source=source)

    def _mon_add_manual(self) -> None:
        disp = (self.var_mon_acc.get() or "").strip()
        order_raw = (self.var_mon_order.get() or "").strip()
        if not disp or not order_raw:
            self._mon_update_status("请选择账号并输入订单号")
            return

        pid, name = self._mon_acc_display_map.get(disp, ("", ""))
        if not pid:
            # 兼容：用户直接输入了 profile_id
            pid = disp
            name = disp

        # 支持多行/逗号
        parts = [x.strip() for x in order_raw.replace(",", "\n").splitlines() if x.strip()]
        if not parts:
            self._mon_update_status("订单号为空")
            return

        added = self._mon_add_items_bulk(pid, name, parts, source="manual")
        try:
            self.var_mon_order.set("")
        except Exception:
            pass
        if added > 0:
            self._mon_update_status(f"已加入监控：{added} 单")
        else:
            self._mon_update_status("已在监控列表中")

    def _mon_remove_one(self, order_no: str) -> bool:
        """v6.0.75:程式可呼叫的單筆移除 — 給「一鍵作廢」callback 用。

        作廢後原始訂單號不需要再監控發貨(實際生效的是 +N 修正版本)。
        Returns: True 表示有移除,False 表示原本就不在監控內。
        """
        ono = str(order_no or "").strip()
        if not ono:
            return False
        removed = False
        try:
            if ono in (self._mon_items or {}):
                try:
                    del self._mon_items[ono]
                    removed = True
                except Exception:
                    pass
            if ono in self._mon_order_ring:
                while ono in self._mon_order_ring:
                    try:
                        self._mon_order_ring.remove(ono)
                    except Exception:
                        break
                removed = True
        except Exception:
            pass
        if removed:
            try:
                self._mon_save_state()
            except Exception:
                pass
            try:
                self._ui_call(self._mon_refresh_tree)
            except Exception:
                pass
        return removed

    def _mon_remove_selected(self) -> None:
        tree = self._mon_tree
        if tree is None:
            return
        try:
            sel = list(tree.selection() or [])
        except Exception:
            sel = []
        if not sel:
            self._mon_update_status("未选择任何订单")
            return

        removed = 0
        for iid in sel:
            ono = str(iid)
            if ono in (self._mon_items or {}):
                try:
                    del self._mon_items[ono]
                except Exception:
                    pass
                removed += 1
            try:
                if ono in self._mon_order_ring:
                    # 可能重复，循环清理
                    while ono in self._mon_order_ring:
                        self._mon_order_ring.remove(ono)
            except Exception:
                pass

        if removed > 0:
            self._mon_save_state()
            self._mon_refresh_tree()
            self._mon_update_status(f"已移除：{removed} 单")

    def _mon_check_all_now(self) -> None:
        # 将所有订单的 next_check_at 置为现在，下一个 tick 立即执行
        now = time.time()
        for it in (self._mon_items or {}).values():
            try:
                if it and it.active:
                    it.next_check_at = now
            except Exception:
                continue
        self._mon_save_state()
        self._mon_refresh_tree()
        self._mon_update_status("已触发：立即检查全部")
        self._mon_tick()

    def _mon_start_tick(self) -> None:
        if self._mon_tick_started:
            return
        self._mon_tick_started = True
        self.frame.after(60_000, self._mon_tick)

    def _mon_pick_due_all(self) -> List[str]:
        """本轮应检查的所有订单（你要求：有多少单就查多少单）。"""
        if not self._mon_order_ring:
            return []
        now = time.time()

        # 为了保持"轮着查"的感觉：每一轮把起点向后挪一格
        try:
            self._mon_order_ring.rotate(-1)
        except Exception:
            pass

        picked: List[str] = []
        # 注意：逐单查询，但本轮查全量
        for ono in list(self._mon_order_ring):
            it = (self._mon_items or {}).get(ono)
            if not it or not it.active:
                continue
            if float(it.next_check_at or 0) <= now:
                picked.append(ono)
        return picked

    def _mon_tick(self) -> None:
        # 每分钟 tick 一次：到点就检查"本轮全部订单"
        try:
            # reschedule first (avoid stopping on exceptions)
            self.frame.after(60_000, self._mon_tick)
        except Exception:
            pass

        try:
            if not self.var_mon_enabled.get():
                return
        except Exception:
            return

        if self._mon_running:
            return

        order_nos = self._mon_pick_due_all()
        if not order_nos:
            # 更新下次时间提示
            try:
                nxt = self._mon_get_next_cycle_at()
                if nxt > 0:
                    s = time.strftime("%m-%d %H:%M", time.localtime(nxt))
                    self._mon_update_status(f"下次检查：{s}")
            except Exception:
                pass
            return

        self._mon_running = True
        self._mon_update_status(f"检查中：{len(order_nos)} 单")

        # --- HTTP 路径 (自动登录 + 重试) ---
        http_done = False
        from .syb_http_ops import ensure_stoken, check_shipped_batch, SYBAuthError, _TOKEN_CACHE
        for _http_attempt in range(3):
            try:
                stoken = ensure_stoken(log=self._log)
                results = check_shipped_batch(stoken, order_nos, log=self._log)
                if results:
                    http_done = True
                    self._log(f"[{_now_ts()}] [SYB-MON] HTTP 查状态完成: {len(results)} 单")
                    self._ui_call(lambda: self._mon_on_check_done(order_nos, results))
                    break
            except SYBAuthError as e:
                # v6.0.61 修复:server 拒了 cache 里的 stoken → 清掉 cache,
                # 下次 ensure_stoken 看到 cache 没了 → 自动跑 auto_login (AI 验证码)
                try:
                    _TOKEN_CACHE.unlink(missing_ok=True)
                except Exception:
                    pass
                self._log(f"[{_now_ts()}] [SYB-MON] 第{_http_attempt+1}次 stoken 被 server 拒,已清缓存,下次重试将走 auto_login(AI 验证码)")
                if _http_attempt < 2:
                    time.sleep(2)  # 短间隔,让下次重试快速触发 auto_login
            except Exception as e:
                self._log(f"[{_now_ts()}] [SYB-MON] HTTP 查状态第{_http_attempt+1}次失败({e})")
                if _http_attempt < 2:
                    time.sleep(5)

        if not http_done:
            self._log(f"[{_now_ts()}] [SYB-MON] HTTP 3次均失败,等待下一轮监控")
            self._mon_running = False
            return

    def _mon_on_check_done(self, order_nos: List[str], results: dict) -> None:
        self._mon_running = False
        now = time.time()
        interval = self._mon_get_interval_sec()

        shipped_list: List[str] = []
        for ono in order_nos:
            r = results.get(ono) if isinstance(results, dict) else None
            shipped = False
            status = ""
            try:
                if isinstance(r, dict):
                    shipped = bool(r.get("shipped", False))
                    status = str(r.get("status", "") or "")
            except Exception:
                shipped = False
                status = ""

            it = (self._mon_items or {}).get(ono)
            if it:
                it.last_checked_at = now
                it.last_status = status or ("已发货" if shipped else "未发货")

            if shipped:
                shipped_list.append(ono)

        # 已发货：通知 + 移除监控
        if shipped_list:
            for ono in shipped_list:
                it = (self._mon_items or {}).get(ono)
                acc = ""
                pid = ""
                if it:
                    acc = it.account_name
                    pid = it.profile_id

                # 通知 TG 运营 Bot（仅通知当前使用者）
                try:
                    ops_bot = getattr(self.app, "_ops_tg_bot", None)
                    if ops_bot:
                        _cid = str(self.app.settings.get("tg_chat_id", "")).strip()
                        if _cid:
                            ops_bot.send_to(_cid,
                                f"📦【发货提醒】\n"
                                f"账号：{acc}\n"
                                f"订单：{ono}\n"
                                f"状态：✅ 已发货\n"
                                f"时间：{_now_ts()}")
                except Exception:
                    pass

                # 回调自动列印面单
                _should_remove = False
                try:
                    alw = getattr(self.app, "auto_label_worker", None)
                    if alw:
                        _r_ono = results.get(ono) if isinstance(results, dict) else None
                        _st_ono = str(_r_ono.get("status", "")) if isinstance(_r_ono, dict) else ""
                        self._log(f"[{_now_ts()}] [SYB-MON] 回调列印: {ono} status_text='{_st_ono or '已发货'}' alw.enabled_store={alw.enabled_store}")

                        # 手动绑定的订单 _tasks 里没有 → 用 SYB API 返回的 expCompany 自动补注册
                        # 自动上传的订单 register_task 已在 purchase_ship_feature.py:1082 调过，这里跳过
                        try:
                            with alw._lock:
                                _has_task = ono in alw._tasks
                        except Exception:
                            _has_task = False
                        if not _has_task:
                            _auto_ch = ""
                            if isinstance(_r_ono, dict):
                                _auto_ch = str(_r_ono.get("exp_company") or "").strip()
                            if _auto_ch:
                                alw.register_task(
                                    ono,
                                    it.profile_id if it else "",
                                    it.account_name if it else "",
                                    _auto_ch,
                                )
                                self._log(f"[{_now_ts()}] [SYB-MON] 手动绑定订单自动补注册列印: {ono} channel={_auto_ch}")
                                # register_task 内部会按 enabled_store/home 决定是否真注册
                                # 若用户没勾选对应开关，task 不会被加入 → 后续 on_shipped 不触发列印
                            else:
                                self._log(f"[{_now_ts()}] [SYB-MON] {ono} 暂无 channel 信息（API 未返回 expCompany），保留监控等下一轮")
                                # channel 缺失 → 跳过本轮列印 + 不移除监控
                                continue

                        # 检查 on_shipped 是否真正会触发列印（根据 trigger_level 判断）
                        _lv = getattr(alw, 'trigger_level', '已发货')
                        _picked_kw = ("已揽收", "已攬收", "已打包", "待揽收")
                        _shipped_kw = ("已发货", "已發貨", "已出仓", "已寄出")
                        _is_picked = any(k in _st_ono for k in _picked_kw)
                        _is_shipped = any(k in _st_ono for k in _shipped_kw)
                        _is_transit = "转运" in _st_ono or "轉運" in _st_ono or "配送" in _st_ono
                        _will_trigger = (
                            (_lv == "已揽收" and (_is_picked or _is_shipped or _is_transit)) or
                            (_lv == "已发货" and (_is_shipped or _is_transit)) or
                            (_lv == "转运中" and _is_transit)
                        )
                        alw.on_shipped(ono, _st_ono or "已发货")
                        _should_remove = _will_trigger
                        if not _will_trigger:
                            self._log(f"[{_now_ts()}] [SYB-MON] 状态'{_st_ono}'未达触发级别'{_lv}'，保留监控")
                    else:
                        self._log(f"[{_now_ts()}] [SYB-MON] 列印回调跳过: auto_label_worker 未初始化")
                        _should_remove = True  # 无列印功能时直接移除
                except Exception as _label_err:
                    self._log(f"[{_now_ts()}] [SYB-MON] 列印回调异常: {ono} -> {_label_err}")
                    _should_remove = True

                # 只在真正触发列印后才从监控移除（否则保留，等状态升级后再触发）
                if _should_remove:
                    try:
                        if ono in self._mon_items:
                            del self._mon_items[ono]
                    except Exception:
                        pass
                    try:
                        while ono in self._mon_order_ring:
                            self._mon_order_ring.remove(ono)
                    except Exception:
                        pass

        # 未发货：统一推迟到下一轮（全量轮询）
        next_at = now + interval
        for it in (self._mon_items or {}).values():
            try:
                if it and it.active:
                    it.next_check_at = next_at
            except Exception:
                pass

        self._mon_save_state()
        self._mon_refresh_tree()

        if shipped_list:
            self._mon_update_status(f"本轮已发货：{len(shipped_list)} 单（已移除监控）")
        else:
            try:
                s = time.strftime("%m-%d %H:%M", time.localtime(next_at))
            except Exception:
                s = "-"
            self._mon_update_status(f"本轮完成：0 已发货；下次：{s}")

        # TG 汇总通知
        try:
            ops_bot = getattr(self.app, "_ops_tg_bot", None)
            if ops_bot:
                total = len(order_nos)
                shipped_cnt = len(shipped_list)
                not_found_cnt = 0
                fail_cnt = 0
                for ono in order_nos:
                    r = results.get(ono) if isinstance(results, dict) else None
                    st = str(r.get("status", "")) if isinstance(r, dict) else ""
                    if "未找到" in st:
                        not_found_cnt += 1
                    elif "失败" in st:
                        fail_cnt += 1
                not_shipped_cnt = total - shipped_cnt - not_found_cnt - fail_cnt
                remaining = len([it for it in (self._mon_items or {}).values()
                                 if it and it.active])
                lines = ["📊【发货监控检查完成】"]
                lines.append(f"检查：{total} 单")
                if shipped_cnt:
                    lines.append(f"已发货：{shipped_cnt} 单")
                if not_shipped_cnt > 0:
                    lines.append(f"未发货：{not_shipped_cnt} 单")
                if not_found_cnt:
                    lines.append(f"未找到：{not_found_cnt} 单")
                if fail_cnt:
                    lines.append(f"查询失败：{fail_cnt} 单")
                if remaining > 0:
                    try:
                        ns = time.strftime("%H:%M", time.localtime(next_at))
                    except Exception:
                        ns = "-"
                    lines.append(f"剩余监控：{remaining} 单，下次检查：{ns}")
                _cid = str(self.app.settings.get("tg_chat_id", "")).strip()
                if _cid:
                    ops_bot.send_to(_cid, "\n".join(lines))
        except Exception:
            pass

    def _extract_order_nos_from_template(self, xlsx_path: Path) -> List[str]:
        """从最新模板里提取订单号，用于自动加入发货监控。"""
        out: set[str] = set()
        try:
            import openpyxl

            wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
            sheet_names = [
                "线上贴单资料",
                "線上貼單資料",
                "宅配打包资料",
                "宅配打包資料",
            ]
            col_candidates = {
                "订单编号",
                "訂單編號",
                "订单編號",
                "订单号",
                "訂單號",
            }

            for sn in sheet_names:
                if sn not in wb.sheetnames:
                    continue
                ws = wb[sn]
                rows = ws.iter_rows(min_row=1, max_row=1, values_only=True)
                header = next(rows, None)
                if not header:
                    continue
                idx = None
                for i, h in enumerate(header):
                    hh = str(h or "").strip()
                    if hh in col_candidates:
                        idx = i
                        break
                if idx is None:
                    continue

                for r in ws.iter_rows(min_row=2, values_only=True):
                    try:
                        v = r[idx] if idx < len(r) else None
                    except Exception:
                        v = None
                    if v is None:
                        continue
                    s = str(v).strip()
                    if not s or s.lower() == "none":
                        continue
                    # 数字型订单号（避免 123.0）
                    if isinstance(v, float):
                        try:
                            if abs(v - int(v)) < 1e-6:
                                s = str(int(v))
                        except Exception:
                            pass
                    out.add(s)
            try:
                wb.close()
            except Exception:
                pass
        except Exception:
            return []

        return sorted(out)

    def _syb_check_shipped_batch(self, page, order_nos: List[str], verbose: bool = True) -> dict:
        """检查一批订单是否已发货。
        返回：{order_no: {"shipped": bool, "status": str}}
        """
        results: dict[str, dict] = {}

        def _wait_stock_ready(timeout: int = 30_000) -> None:
            page.wait_for_selector("div.ctrl-left", state="visible", timeout=timeout)
            end = time.time() + timeout / 1000
            while time.time() < end:
                if page.locator(".vxe-table").count() > 0 or page.locator(".el-table").count() > 0:
                    return
                page.wait_for_timeout(200)

        def _click_toolbar(label: str, timeout: int = 10_000) -> None:
            _wait_stock_ready(timeout=max(timeout, 30_000))
            root = page.locator("div.ctrl-left").first
            last_err = None
            end = time.time() + timeout / 1000

            while time.time() < end:
                try:
                    spans = root.locator("span.txt")
                    for i in range(min(spans.count(), 50)):
                        s = spans.nth(i)
                        if not s.is_visible():
                            continue
                        try:
                            if s.inner_text().strip() != label:
                                continue
                        except Exception:
                            continue

                        clickable = s.locator("xpath=ancestor::*[self::a or self::button][1]")
                        if clickable.count() == 0:
                            clickable = s

                        clickable.scroll_into_view_if_needed()
                        try:
                            clickable.click(timeout=2000)
                        except Exception:
                            clickable.click(timeout=2000, force=True)
                        return

                    ok = page.evaluate(
                        """(label) => {
                            const root = document.querySelector('div.ctrl-left');
                            if (!root) return false;
                            const txts = Array.from(root.querySelectorAll('span.txt'));
                            const hit = txts.find(x => (x.textContent || '').trim() === label);
                            const el = hit ? (hit.closest('a,button') || hit) : null;
                            if (!el) return false;
                            el.click();
                            return true;
                        }""",
                        label,
                    )
                    if ok:
                        return
                except Exception as e:
                    last_err = e

                page.wait_for_timeout(250)

            if last_err:
                raise last_err
            raise RuntimeError(f"找不到可点击的工具栏按钮：{label}")

        def _ensure_adv_search_panel() -> None:
            try:
                lab = page.locator("label", has_text="订单编号").first
                if lab.count() > 0 and lab.is_visible():
                    return
            except Exception:
                pass
            _click_toolbar("高级搜索", timeout=10_000)
            page.locator("label", has_text="订单编号").first.wait_for(state="visible", timeout=20_000)

        def _fill_order_no(order_no: str) -> None:
            lab = page.locator("label", has_text="订单编号").first
            item = lab.locator("xpath=ancestor::div[contains(@class,'el-form-item')][1]").first
            candidates = [
                item.locator("textarea").first,
                item.locator("input").first,
                lab.locator("xpath=following::textarea[1]").first,
                lab.locator("xpath=following::input[1]").first,
            ]
            last_err = None
            for cand in candidates:
                try:
                    cand.wait_for(state="visible", timeout=3000)
                    cand.scroll_into_view_if_needed()
                    cand.click(timeout=3000)
                    cand.fill(order_no)
                    return
                except Exception as e:
                    last_err = e
                    continue
            raise RuntimeError(f"找不到『订单编号』输入框：{last_err}")

        def _click_btn(text: str) -> None:
            btn = page.locator("button").filter(has_text=text).first
            btn.scroll_into_view_if_needed()
            btn.click(timeout=5000)

        def _find_row(order_no: str):
            row_vxe = page.locator(".vxe-table--body-wrapper .vxe-body--row").filter(has_text=order_no).first
            row_el = page.locator(".el-table__body-wrapper tbody tr").filter(has_text=order_no).first
            return row_vxe, row_el

        # 1) 打开物流查询页（SPA 需要等 networkidle 确保工具栏渲染）
        page.goto(SYB_STOCK_URL, wait_until="domcontentloaded", timeout=SYB_GOTO_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        cur_url = page.url or ""
        if "/login" in cur_url and "/sys/admin/" not in cur_url:
            raise RuntimeError("物流查询页被重定向到登录页（可能未登录/会话失效）")
        _wait_stock_ready(timeout=30_000)
        _ensure_adv_search_panel()

        for order_no in order_nos:
            try:
                modal_txt = ""
                if verbose:
                    self._log(f"[SYB] 检查订单：{order_no}")

                try:
                    _click_btn("重置")
                except Exception:
                    pass

                _fill_order_no(order_no)
                _click_btn("搜索")

                row_vxe, row_el = _find_row(order_no)
                deadline = time.time() + 20
                while time.time() < deadline:
                    if row_vxe.count() > 0 or row_el.count() > 0:
                        break
                    page.wait_for_timeout(250)

                row = row_vxe if row_vxe.count() > 0 else row_el
                if row.count() == 0:
                    results[order_no] = {"shipped": False, "status": "未找到"}
                    continue

                txt = row.inner_text()
                shipped = ("已发货" in txt) or ("已發貨" in txt)

                if not shipped:
                    try:
                        selected = False
                        if row_vxe.count() > 0:
                            rowid = row_vxe.get_attribute("data-rowid") or ""
                            if rowid:
                                cb = page.locator(
                                    f".vxe-table--fixed-left-wrapper .vxe-body--row[data-rowid='{rowid}'] "
                                    f".vxe-cell--checkbox"
                                ).first
                                if cb.count() > 0:
                                    try:
                                        cb.click(timeout=2000)
                                        selected = True
                                    except Exception:
                                        selected = False
                            if not selected:
                                cb2 = page.locator(
                                    ".vxe-table--fixed-left-wrapper .vxe-table--body-wrapper "
                                    ".vxe-body--row .vxe-cell--checkbox"
                                ).first
                                if cb2.count() > 0:
                                    try:
                                        cb2.click(timeout=2000)
                                        selected = True
                                    except Exception:
                                        selected = False
                            if not selected:
                                try:
                                    row_vxe.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False
                        else:
                            cb = row_el.locator("td .el-checkbox__input").first
                            if cb.count() > 0:
                                try:
                                    cb.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False
                            if not selected:
                                try:
                                    row_el.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False

                        opened = False
                        dlg = None
                        try:
                            _click_toolbar("物流记录")
                            dlg = page.locator(".el-dialog__wrapper:visible").filter(
                                has=page.locator(".el-dialog__title", has_text="物流")
                            ).first
                            dlg.wait_for(state="visible", timeout=6000)
                            opened = True
                        except Exception:
                            try:
                                _click_toolbar("物流記錄")
                                dlg = page.locator(".el-dialog__wrapper:visible").filter(
                                    has=page.locator(".el-dialog__title", has_text="物流")
                                ).first
                                dlg.wait_for(state="visible", timeout=6000)
                                opened = True
                            except Exception:
                                opened = False

                        if opened and dlg is not None:
                            try:
                                dlg.locator(".el-timeline-item__content").first.wait_for(timeout=5000)
                            except Exception:
                                pass

                            modal_txt = dlg.inner_text()
                            shipped = bool(re.search(r"已\s*发\s*货", modal_txt)) or bool(re.search(r"已\s*發\s*貨", modal_txt))

                            try:
                                dlg.locator(".el-dialog__headerbtn").click(timeout=2000)
                            except Exception:
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass

                            try:
                                dlg.wait_for(state="hidden", timeout=5000)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # 提取更精确的物流状态（用于自动列印触发）
                detail_status = "未发货"
                if shipped:
                    if re.search(r"转\s*运|轉\s*運", modal_txt):
                        detail_status = "转运中"
                    elif re.search(r"已\s*揽\s*收|已\s*攬\s*收", modal_txt):
                        detail_status = "已揽收"
                    else:
                        detail_status = "已发货"
                results[order_no] = {"shipped": shipped, "status": detail_status}

            except Exception as e:
                results[order_no] = {"shipped": False, "status": f"失败：{e}"}

        return results


# ---------------- helpers ----------------

    def _find_latest_template(self) -> Optional[Path]:
        """自动找最新的 最新模板_YYYYMMDD.xlsx；找不到则尝试 最新模板.xlsx。"""
        # 1) 优先用 ship_output_dir
        outdir = None
        try:
            outdir = str(getattr(self.app, "settings", {}).get("ship_outdir", "") or "").strip()
        except Exception:
            outdir = None

        cand_dirs: List[Path] = []
        if outdir:
            cand_dirs.append(Path(outdir))
        cand_dirs.append(BASE_DIR)
        cand_dirs.append((BASE_DIR / "output"))

        # 找最新的最新模板_*.xlsx
        best: Optional[Path] = None
        best_mtime = -1.0
        for d in cand_dirs:
            try:
                if not d.exists():
                    continue
                for p in d.glob("最新模板_*.xlsx"):
                    try:
                        mt = p.stat().st_mtime
                        if mt > best_mtime:
                            best_mtime = mt
                            best = p
                    except Exception:
                        pass
            except Exception:
                pass

        if best:
            return best

        # 兜底：最新模板.xlsx（底稿/或你手动放的）
        for d in cand_dirs:
            p = d / "最新模板.xlsx"
            if p.exists():
                return p

        return None

    # ---------------- UI callbacks ----------------

    def _ui_open_login(self):
        self.var_status.set("会话：启动中...")

        def task(page):
            _syb_try_goto_login(page, self._log)
            # 登录表单：通常是 3 个 input（账号/密码/验证码）
            inputs = page.locator("form input")
            if inputs.count() >= 2:
                inputs.nth(0).fill(SYB_USERNAME)
                inputs.nth(1).fill(SYB_PASSWORD)
                if inputs.count() >= 3:
                    inputs.nth(2).click()
            self._log(f"[{_now_ts()}] [SYB] 已自动填入账号/密码，请在浏览器输入验证码并点击『登录』")
            # 等待登录成功（不阻塞太久）
            try:
                page.wait_for_url("**/sys/admin/**", timeout=120_000)
            except Exception:
                # 可能还没点登录，正常
                pass
            # 更新状态
            try:
                cur = page.url
                if "/sys/admin/" in cur:
                    self.var_status.set("会话：已登录")
                    self._toggle_login_ui(True)
                else:
                    self.var_status.set("会话：等待验证码")
            except Exception:
                self.var_status.set("会话：已启动")

        self._agent.submit(task, "打开登录页并等待验证码")

    # ---- 新：不弹出浏览器，只显示验证码小图 ----

    def _ui_get_captcha(self):
        """HTTP 获取验证码图片（不需要 Playwright）。"""
        self.var_status.set("会话：获取验证码中...")

        def _do():
            try:
                from .syb_http_ops import fetch_captcha_image
                img_bytes, session_token = fetch_captcha_image(log=self._log)
                if not img_bytes:
                    self.var_status.set("会话：获取验证码失败")
                    return

                self._captcha_session_token = session_token

                # 保存验证码图片（转为 PNG，tk.PhotoImage 不支持 JPEG）
                _safe_mkdir(self._captcha_path.parent)
                try:
                    from PIL import Image
                    import io
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    pil_img.save(str(self._captcha_path), format="PNG")
                except ImportError:
                    # 没有 PIL，直接保存原始 JPEG（tk.PhotoImage 可能无法显示）
                    self._captcha_path.write_bytes(img_bytes)

                self._captcha_ts = time.time()
                self._ui_update_captcha_image()
                self.var_status.set("会话：请输入验证码")
                self._log(f"[{_now_ts()}] [SYB] 验证码已获取（HTTP），请输入后点击登录")
            except Exception as e:
                self.var_status.set("会话：获取验证码失败")
                self._log(f"[{_now_ts()}] [SYB] HTTP获取验证码失败：{e}")

        threading.Thread(target=_do, daemon=True).start()

    def _ui_refresh_captcha(self):
        """刷新验证码（HTTP，不需要 Playwright）。"""
        self._ui_get_captcha()

    def _ui_login(self):
        """HTTP 登录（不需要 Playwright 浏览器）。"""
        code = (self.var_captcha.get() or "").strip()
        if not code:
            self._log("[SYB] 请先输入验证码")
            self.var_status.set("会话：请先输入验证码")
            return

        # 提醒：验证码通常很快过期
        try:
            if self._captcha_ts and (time.time() - float(self._captcha_ts) > 120):
                self._log("[SYB] 提示：你获取验证码已超过 2 分钟，建议点击『刷新验证码』再登录")
        except Exception:
            pass

        session_token = getattr(self, "_captcha_session_token", "")
        if not session_token:
            self._log("[SYB] 请先点击『获取验证码』")
            self.var_status.set("会话：请先获取验证码")
            return

        def _do():
            try:
                from .syb_http_ops import http_login, save_stoken, verify_stoken

                self.var_status.set("会话：登录中...")
                success, jwt_token, err_msg = http_login(session_token, code, log=self._log)

                if success:
                    # 验证 token 是否真的可用
                    if jwt_token and verify_stoken(jwt_token, log=self._log):
                        save_stoken(jwt_token)
                        self.var_status.set("会话：已登录")
                        self._log(f"[{_now_ts()}] [SYB] HTTP 登录成功，stoken 已缓存 (len={len(jwt_token)})")
                        self.var_captcha.set("")
                        self._toggle_login_ui(True)
                    elif jwt_token:
                        save_stoken(jwt_token)
                        self.var_status.set("会话：已登录")
                        self._log(f"[{_now_ts()}] [SYB] HTTP 登录成功（token 验证跳过），stoken 已缓存")
                        self.var_captcha.set("")
                        self._toggle_login_ui(True)
                    else:
                        self.var_status.set("会话：登录成功但未获取到 token")
                        self._log(f"[{_now_ts()}] [SYB] 登录成功但 stoken 为空")
                else:
                    self.var_status.set(f"会话：登录失败（{err_msg}）")
                    self._log(f"[{_now_ts()}] [SYB] HTTP 登录失败：{err_msg}")
                    # 自动刷新验证码
                    self._ui_get_captcha()
            except Exception as e:
                self.var_status.set("会话：登录失败（异常）")
                self._log(f"[{_now_ts()}] [SYB] HTTP 登录异常：{e}")

        threading.Thread(target=_do, daemon=True).start()

    def _check_stoken_on_start(self):
        """启动时检查 stoken，过期则自动登录，失败才显示手动登录 UI。

        thread 內所有 var.set 都用 self._ui_call 切回主執行緒,避免
        「main thread is not in main loop」啟動時序錯誤。
        """
        def _set_status(msg):
            """thread-safe 更新狀態"""
            self._ui_call(lambda: self.var_status.set(msg))

        def _do():
            try:
                from .syb_http_ops import load_stoken, verify_stoken, auto_login, save_stoken

                # 1. 检查缓存
                token = load_stoken()
                if token:
                    self._log(f"[{_now_ts()}] [SYB] 检查缓存 stoken 有效性...")
                    if verify_stoken(token, log=self._log):
                        _set_status("会话：已登录")
                        self._log(f"[{_now_ts()}] [SYB] 缓存 stoken 有效，自动恢复登录状态")
                        self._toggle_login_ui(True)
                        return

                # 2. 过期或无缓存 → 自动登录
                _set_status("会话：自动登录中...")
                self._log(f"[{_now_ts()}] [SYB] token 无效/过期，正在自动登录...")
                try:
                    new_token = auto_login(max_attempts=5, log=self._log)
                    _set_status("会话：已登录（自动）")
                    self._log(f"[{_now_ts()}] [SYB] 自动登录成功")
                    self._toggle_login_ui(True)
                    return
                except Exception as login_err:
                    self._log(f"[{_now_ts()}] [SYB] 自动登录失败: {login_err}")

                # 3. 自动登录失败 → 显示手动登录 UI
                _set_status("会话：自动登录失败（请手动登录）")
                self._toggle_login_ui(False)

            except Exception as e:
                self._log(f"[{_now_ts()}] [SYB] stoken 检查异常：{e}")
                self._toggle_login_ui(False)
        threading.Thread(target=_do, daemon=True).start()

    def _ui_open_stock(self):
        def task(page):
            page.goto(SYB_STOCK_URL, wait_until="domcontentloaded")
            self.var_status.set("会话：已打开货运管理")

        self._agent.submit(task, "打开货运管理页面")

    def _ui_upload_store(self):
        tpl = self._pick_template("店配")
        if not tpl:
            return
        self._upload_data(tpl, kind="store")
        # v6.0.62: 手动上传成功后也自动加入发货监控(让 SYB-MON 看「已发货」自动列印面单)
        self._auto_register_monitor_from_template(tpl)

    def _ui_upload_home(self):
        tpl = self._pick_template("宅配")
        if not tpl:
            return
        self._upload_data(tpl, kind="home")
        self._auto_register_monitor_from_template(tpl)

    def _auto_register_monitor_from_template(self, tpl: Path) -> None:
        """v6.0.62: 从模板提取订单号自动加入 SYB-MON 监控。

        手动上传场景下没有 yahoo 帐号信息(profile_id/account_name 留空),
        SYB-MON 后续会从 SYB API 返回的 expCompany 自动补充(line 1348-1366 已处理)。
        """
        try:
            order_nos = self._extract_order_nos_from_template(tpl)
            if not order_nos:
                return
            added = self._mon_add_items_bulk("", "", sorted(order_nos), source="manual_upload")
            if added > 0:
                self._log(f"[{_now_ts()}] [SYB-MON] 已加入发货监控:{added} 单(来源:手动上传)")
        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB-MON] 自动加入监控失败:{e}")

    def _pick_template(self, label: str) -> Optional[Path]:
        """弹出文件选择对话框让用户选择模板，自动定位到最近的模板目录。"""
        # 用自动查找结果决定初始目录和默认文件
        auto = self._find_latest_template()
        if auto:
            init_dir = str(auto.parent)
            init_file = auto.name
        else:
            init_dir = str(BASE_DIR / "output")
            init_file = ""
        p = filedialog.askopenfilename(
            title=f"选择{label}模板文件",
            filetypes=[("Excel", "*.xlsx"), ("All", "*.*")],
            initialdir=init_dir,
            initialfile=init_file,
        )
        if not p:
            return None
        return Path(p)

    def _upload_data(self, tpl: Path, kind: str, force_voided_orders: Optional[Set[str]] = None):
        """kind: store(店配) | home(线下/宅配)

        HTTP-first: 解析 Excel → 调 HTTP API 导入 → 成功则跳过 Playwright。
        失败时回退到 Playwright 网页上传。

        v6.0.75:force_voided_orders 跳過 D1 作廢檢查
        (一鍵作廢 + 重上傳 callback 場景:剛 set_void_status 但 D1 還沒同步)
        """
        sheet = SHEET_STORE if kind == "store" else SHEET_HOME
        option = "店配" if kind == "store" else "线下"

        # 如果该 sheet 没数据，直接跳过（避免误传空表）
        try:
            if not _has_data_rows(tpl, sheet):
                self._log(f"[{_now_ts()}] [SYB] {kind}：模板 {tpl.name} 的『{sheet}』没有数据行，跳过")
                return
        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB] 读取模板失败：{e}")
            return

        # --- HTTP-first 路径 (自动登录 + 401 自动恢复) ---
        try:
            from .syb_http_ops import (
                ensure_stoken, parse_template_to_import_rows, import_shipment_data,
                SYBAuthError, _TOKEN_CACHE, make_dup_code,
            )
            import_rows = parse_template_to_import_rows(tpl, sheet, log=self._log)
            if import_rows:
                origin = 1 if kind == "store" else 2  # 店配=1, 线下=2
                # v6.0.62: cache 内 token 被 server 拒时,清 cache → 重试 → ensure_stoken 自动 auto_login (AI 验证码)
                results = None
                for _attempt in (1, 2):
                    try:
                        stoken = ensure_stoken(log=self._log)
                        results = import_shipment_data(stoken, import_rows, origin=origin, log=self._log)
                        break  # 成功
                    except SYBAuthError as _e:
                        if _attempt == 1:
                            try: _TOKEN_CACHE.unlink(missing_ok=True)
                            except Exception: pass
                            self._log(f"[{_now_ts()}] [SYB] stoken 被服务器拒绝,清缓存重新登录后重试...")
                            continue
                        raise
                if results is None:
                    raise RuntimeError("import_shipment_data 未返回结果")

                # v6.0.75:撞「已存在」時,查 D1 業績作廢狀態決定是否 +N
                # 設計意圖(用戶澄清):
                #   - 業績「已作廢」 = 用戶在業績匯總申請作廢過,要重新上傳修正版本 → +N 上傳(原 v6.0.68 行為)
                #   - 業績「未作廢」 = 訂單已上傳但用戶忘了作廢 → 跳過 + 通知,避免重複 SYB 訂單
                # 之前 v6.0.68 不分作廢狀態,無腦 +N → 造成 +1 雪球
                exist_idx = [i for i, r in enumerate(results) if "已存在" in str(r.get("msg", ""))]
                _retry_ok = 0
                _retry_fail: List[Tuple[Any, str]] = []

                # v6.1.45:成功 import 的訂單立刻寫本機 cache,下一輪自動上傳同模板時這些訂單不再誤判
                # 修「採購監控觸發第 N 輪自動出貨時,模板累積前 N-1 筆撞已存在誤推申請作廢」bug
                try:
                    success_codes = [
                        import_rows[i].code for i, r in enumerate(results)
                        if "成功" in str(r.get("msg", ""))
                    ]
                    if success_codes:
                        self._mark_syb_uploaded(success_codes)
                except Exception:
                    pass

                if exist_idx:
                    # v6.0.75:bypass — callback 觸發時帶 force_voided_orders,跳過 D1 查詢避免延遲
                    if force_voided_orders:
                        _voided_pks = None  # 不用 D1 結果,用 force list 判斷
                        self._log(f"[{_now_ts()}] [SYB] 一鍵作廢觸發 — bypass D1 檢查,force list: {sorted(force_voided_orders)[:3]}...")
                    else:
                        # 查 D1 業績作廢清單(近 30 天)
                        try:
                            from core.latest_template_builder import _query_d1_voided_pks
                            _voided_pks = _query_d1_voided_pks(log_fn=self._log)
                        except Exception as _e_v:
                            self._log(f"[{_now_ts()}] [SYB] ⚠️ 查 D1 作廢清單失敗: {_e_v},保守跳過(不 +N)")
                            _voided_pks = None  # None 表示查不到 → 保守跳過

                    for idx in exist_idx:
                        base_code = import_rows[idx].code
                        perf_code = import_rows[idx].shop_name or ""  # 業績編碼(店铺名欄)
                        pk = f"{perf_code}|{base_code}"

                        # v6.1.45:本機 cache 命中 → 訂單之前就成功上傳過,這次是模板累積重傳
                        # 靜默跳過,不查 D1 不推 TG,避免「第一次採購綁定」用戶被誤導
                        if (not force_voided_orders) and self._is_syb_uploaded(base_code):
                            self._log(
                                f"[{_now_ts()}] [SYB] 訂單 {base_code} 本機 cache 已上傳 → 靜默跳過"
                                f"(模板累積重傳,不是作廢場景)"
                            )
                            continue

                        # 優先看 force list(callback 場景);否則用 D1 結果
                        if force_voided_orders and base_code in force_voided_orders:
                            is_voided = True
                        else:
                            is_voided = (_voided_pks is not None) and (pk in _voided_pks)

                        if not is_voided:
                            warn_msg = (
                                f"訂單 {base_code} 撞「已存在」但業績「{perf_code}」"
                                f"{'未作廢' if _voided_pks is not None else '無法查 D1 作廢狀態(保守跳過)'}"
                                f" — 請先去【業績匯總】申請作廢,才能重新上傳此訂單"
                            )
                            self._log(f"[{_now_ts()}] [SYB] ⚠️ {warn_msg}")
                            # TG 通知主管含一鍵按鈕(只通知一次,避免刷屏)
                            try:
                                self._notify_need_void(perf_code, base_code, warn_msg, tpl_path=str(tpl))
                            except Exception:
                                pass
                            continue

                        # 業績已作廢 → +N 重試(原 v6.0.68 行為,用戶有意修正)
                        self._log(f"[{_now_ts()}] [SYB] 訂單 {base_code} 業績「{perf_code}」已作廢,進行 +N 重試上傳(修正版本)")
                        last_msg = "已存在"
                        for n in range(1, 10):
                            new_code = make_dup_code(base_code, n)
                            try:
                                from copy import copy as _copy
                                retry_row = _copy(import_rows[idx])
                                retry_row.code = new_code
                                retry_results = import_shipment_data(stoken, [retry_row], origin=origin, log=self._log)
                                rmsg = str(retry_results[0].get("msg", ""))
                                if "成功" in rmsg:
                                    results[idx] = retry_results[0]
                                    import_rows[idx].code = new_code
                                    self._log(f"[{_now_ts()}] [SYB] {base_code} 改 {new_code} 上傳成功 (n={n}) — 業績作廢已確認")
                                    _retry_ok += 1
                                    last_msg = rmsg
                                    # v6.1.45:+N 重試成功也要寫 cache(以新 code 記),下次不會誤判
                                    try:
                                        self._mark_syb_uploaded([new_code])
                                    except Exception:
                                        pass
                                    # v6.0.75:原始訂單號已作廢 → 從 SYB-MON 移除(避免查無效訂單)
                                    try:
                                        if self._mon_remove_one(base_code):
                                            self._log(f"[{_now_ts()}] [SYB-MON] 已移除原始作廢訂單號 {base_code}(改監控 +N 版本 {new_code})")
                                    except Exception as _e_rm:
                                        self._log(f"[{_now_ts()}] [SYB-MON] 移除原始訂單號異常: {_e_rm}")
                                    break
                                elif "已存在" in rmsg:
                                    last_msg = rmsg
                                    continue
                                else:
                                    last_msg = rmsg
                                    break
                            except Exception as e:
                                last_msg = f"重試異常: {e}"
                                break
                        if "成功" not in last_msg:
                            _retry_fail.append((import_rows[idx], last_msg))

                ok_cnt = sum(1 for r in results if "成功" in str(r.get("msg", "")))
                exist_cnt = sum(1 for r in results if "已存在" in str(r.get("msg", "")))
                real_fail = len(results) - ok_cnt - exist_cnt
                self._log(f"[{_now_ts()}] [SYB] HTTP導入{kind}: {ok_cnt}成功 {exist_cnt}仍已存在 {real_fail}失敗 (共{len(results)},其中 +N 重試成功 {_retry_ok})")

                # v6.0.68 ★:重試成功的把新 code 寫回最新模板 file 的「订单编号」欄
                # 這樣下游(發貨監控/面單上傳/業績核對)讀 file 拿到的就是 SYB 實際 code
                # 不用再「猜」+N 後綴。業績 D1 跟 業績Excel 是另一條軸不會被影響。
                if _retry_ok > 0:
                    try:
                        import openpyxl as _opx
                        _wb = _opx.load_workbook(tpl, data_only=False)
                        if sheet in _wb.sheetnames:
                            _ws = _wb[sheet]
                            # 讀表頭找「订单编号」欄
                            _hdr_row = next(_ws.iter_rows(min_row=1, max_row=1, values_only=False))
                            _idx_order = -1
                            for _i, _c in enumerate(_hdr_row):
                                _v = str(_c.value or "").strip()
                                if _v in ("订单编号", "訂單編號", "订单編號", "訂單編码"):
                                    _idx_order = _i
                                    break
                            if _idx_order >= 0:
                                # 對 import_rows 跟 file 的資料行 1-1 對應(builder 寫入順序)
                                # 過濾出當前 sheet 對應的 import_rows
                                _data_rows = list(_ws.iter_rows(min_row=2, values_only=False))
                                _updated = 0
                                for _i, _row in enumerate(_data_rows):
                                    if _i >= len(import_rows):
                                        break
                                    _cur_code = str(_row[_idx_order].value or "").strip()
                                    _new_code = import_rows[_i].code
                                    if _cur_code and _new_code and _cur_code != _new_code:
                                        _row[_idx_order].value = _new_code
                                        _updated += 1
                                if _updated > 0:
                                    _wb.save(tpl)
                                    self._log(f"[{_now_ts()}] [SYB] 把 {_updated} 個重試成功的 code 寫回 {tpl.name} 的「订单编号」欄")
                        _wb.close()
                    except Exception as _e:
                        self._log(f"[{_now_ts()}] [SYB] ⚠️ 寫回最新模板 file 失敗: {_e}(SYB 上傳已完成,只是 file 沒更新,下游會 auto-resolve 補回)")

                # 真失敗(網路/auth/500) = 需要作廢業績(殭屍業績防護)
                # 「已存在」= SYB 已有訂單,業績 D1 也有 → 兩邊正常,不作廢
                #   (之前 bug:批次上傳模板累積,前單變「已存在」被誤作廢)
                # _retry_fail 內可能含「9 次 retry 都撞已存在」case,也要過濾掉
                blocked_pairs: List[Tuple[Any, str]] = [
                    (row, msg) for row, msg in _retry_fail
                    if "已存在" not in msg  # 已存在不作廢,SYB 端已有訂單
                ]
                for idx, r in enumerate(results):
                    msg = str(r.get('msg', ''))
                    if "成功" in msg or "已存在" in msg:
                        continue  # ⚠️ 「已存在」也跳過 — 不作廢
                    self._log(f"  - {r.get('code')}: {msg}")
                    if idx < len(import_rows):
                        if not any(b[0] is import_rows[idx] for b in blocked_pairs):
                            blocked_pairs.append((import_rows[idx], msg))

                if not blocked_pairs:
                    return
                self._log(f"[{_now_ts()}] [SYB] HTTP導入有 {len(blocked_pairs)} 條真失敗,自動作廢對應業績(避免殭屍業績)")
                self._auto_void_failed_perf(blocked_pairs)
                return
            else:
                self._log(f"[{_now_ts()}] [SYB] 模板解析无数据，跳过")
                return
        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB] HTTP导入异常({e})，不回退Playwright")
            return

        # ── Playwright fallback 已删除 (v6.0.58) ─────────
        # 之前 HTTP 失败时会跳到 Playwright 网页上传作为兜底,
        # 但实际上前面所有 try/except 路径都已 return,这段是 dead code,
        # 且用户明确要求「不允许后退」,直接 return,失败就失败。
        return

    # v6.1.45:本機 SYB 上傳記錄 cache
    # 解決「同日訂單累積在最新模板,第 N 輪自動上傳時前 N-1 個 row 撞已存在,誤推申請作廢」bug
    # cache 結構:{order_code: timestamp},90 天 TTL(超過刪除避免無限長大)
    @staticmethod
    def _syb_uploaded_cache_path():
        p = BASE_DIR / "runtime" / "syb_uploaded_orders.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def _load_syb_uploaded_cache(cls) -> dict:
        try:
            fp = cls._syb_uploaded_cache_path()
            if not fp.exists():
                return {}
            d = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                return {}
            return d
        except Exception:
            return {}

    @classmethod
    def _is_syb_uploaded(cls, order_code: str) -> bool:
        """訂單之前在本機是否已成功 SYB 上傳過。"""
        if not order_code:
            return False
        return str(order_code) in cls._load_syb_uploaded_cache()

    @classmethod
    def _mark_syb_uploaded(cls, order_codes) -> None:
        """SYB import 返回成功 → 把訂單號寫進本機 cache。"""
        try:
            import time as _t
            codes = [str(c) for c in (order_codes or []) if c]
            if not codes:
                return
            fp = cls._syb_uploaded_cache_path()
            d = cls._load_syb_uploaded_cache()
            now_ts = _t.time()
            for c in codes:
                d[c] = now_ts
            # 清理 > 90 天的舊 entry,避免 cache 無限長大
            cutoff = now_ts - 90 * 86400
            d = {k: v for k, v in d.items() if float(v or 0) >= cutoff}
            tmp = fp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            import os as _os
            _os.replace(str(tmp), str(fp))
        except Exception:
            pass

    def _notify_need_void(self, perf_code: str, order_code: str, msg: str,
                          tpl_path: str = "") -> None:
        """v6.0.75:訂單撞「已存在」但業績未作廢時,通知主管去業績匯總申請作廢。

        v6.0.75 新增:TG 通知含 [✅ 一鍵作廢並重上傳] 按鈕,主管手機點按就完成。
        防刷屏:同一 (perf_code, order_code) 1 小時內只通知一次。
        """
        try:
            import time as _t
            import hashlib as _hl
            cache_fp = BASE_DIR / "runtime" / "syb_need_void_notified.json"
            cache_fp.parent.mkdir(parents=True, exist_ok=True)
            try:
                cache = json.loads(cache_fp.read_text(encoding="utf-8")) if cache_fp.exists() else {}
            except Exception:
                cache = {}
            key = f"{perf_code}|{order_code}"
            now_ts = _t.time()
            last_ts = float(cache.get(key, 0) or 0)
            # 1 小時內已通知過 → 跳過
            if now_ts - last_ts < 3600:
                return
            cache[key] = now_ts
            # 清理過期項(>24h)
            cache = {k: v for k, v in cache.items() if now_ts - float(v or 0) < 86400}
            try:
                cache_fp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            # v6.0.75:寫 pending action token cache,callback_data 只帶短 token(避免 64 字元限制)
            token = _hl.md5(f"{key}|{now_ts}".encode()).hexdigest()[:10]
            pending_fp = BASE_DIR / "runtime" / "syb_void_pending.json"
            try:
                if pending_fp.exists():
                    pending_cache = json.loads(pending_fp.read_text(encoding="utf-8"))
                else:
                    pending_cache = {}
            except Exception:
                pending_cache = {}
            pending_cache[token] = {
                "perf_code": perf_code,
                "order_code": order_code,
                "tpl_path": tpl_path,
                "created_at": now_ts,
            }
            # 清理過期(>24h)
            pending_cache = {
                k: v for k, v in pending_cache.items()
                if now_ts - float((v or {}).get("created_at", 0) or 0) < 86400
            }
            try:
                pending_fp.write_text(json.dumps(pending_cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            # TG 通知 + 一鍵按鈕(用 ops bot)
            try:
                ops_bot = getattr(self.app, "_ops_tg_bot", None)
                if ops_bot:
                    _cid = str(self.app.settings.get("tg_chat_id", "")).strip()
                    if _cid:
                        text = (
                            f"⚠️ 【SYB 上傳跳過】\n"
                            f"訂單號:{order_code}\n"
                            f"業績編碼:{perf_code}\n\n"
                            f"原因:SYB 已有此訂單但業績未作廢。\n"
                            f"點下方按鈕一鍵完成「作廢 + 重上傳」,或去【業績匯總】手動處理。"
                        )
                        kb = ops_bot.make_keyboard([
                            [{"text": "✅ 一鍵作廢並重上傳", "callback_data": f"sybv:ok:{token}"}],
                            [{"text": "❌ 駁回(不處理)", "callback_data": f"sybv:no:{token}"}],
                        ])
                        ops_bot.send_to(_cid, text, reply_markup=kb)
            except Exception as _e_send:
                self._log(f"[{_now_ts()}] [SYB] TG 通知異常: {_e_send}")
        except Exception as _e:
            self._log(f"[{_now_ts()}] [SYB] _notify_need_void 異常: {_e}")

    def _auto_void_failed_perf(self, failed_pairs: List[Tuple[Any, str]]) -> None:
        """SYB 上传失败时,自动作废对应的业绩条目(避免殭屍业绩堆积)。

        ImportRow.shop_name 字段实际是 perf_code (白050401 这种),
        ImportRow.code 是订单编号 (10121179210513);
        D1 业绩 PK = '{perf_code}|{order_code}'。

        业绩同步是写 Excel 后 2 秒触发(order_export.py),upload 通常 < 5 秒;
        我们等 15 秒确保业绩已落地 D1,再调 set_void_status 标记作废。
        """
        try:
            from .performance_feature import set_void_status
        except ImportError:
            self._log(f"[作废] performance_feature 不可用,跳过自动作废")
            return

        import threading as _th
        import time as _t

        def _run():
            _t.sleep(15)  # 等业绩同步落地
            voided = 0
            for im_row, fail_msg in failed_pairs:
                perf_code = (getattr(im_row, "shop_name", "") or "").strip()
                order_code = (getattr(im_row, "code", "") or "").strip()
                if not perf_code or not order_code:
                    continue
                pk = f"{perf_code}|{order_code}"
                note = f"SYB 物流上传失败:{fail_msg[:80]}"
                try:
                    ok, err = set_void_status(pk, "final", current_note=note)
                    if ok:
                        voided += 1
                        self._log(f"[作废] 自动作废 PK={pk} (SYB 失败)")
                    else:
                        self._log(f"[作废] 作废失败 PK={pk}: {err[:80]}")
                except Exception as e:
                    self._log(f"[作废] 作废异常 PK={pk}: {e}")
            if voided:
                self._log(f"[作废] 共 {voided}/{len(failed_pairs)} 条业绩已自动作废 (SYB 上传失败)")

        _th.Thread(target=_run, daemon=True).start()


    def _ui_upload_labels(self):
        # 弹出文件多选对话框，默认打开面单目录
        init_dir = (self.var_pdf_dir.get() or "").strip() or None
        files = filedialog.askopenfilenames(
            title="选择要上传的面单 PDF（可多选）",
            initialdir=init_dir,
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
            parent=self.app,
        )
        if not files:
            return
        pdfs = [Path(f) for f in files]
        self._log(f"[{_now_ts()}] [SYB] 手动选择 {len(pdfs)} 个面单")

        # --- HTTP-first 路径 (自动登录 + 401 自动恢复) ---
        try:
            from .syb_http_ops import (
                ensure_stoken, upload_label_pdfs_batch, SYBAuthError, _TOKEN_CACHE,
            )
            self._log(f"[{_now_ts()}] [SYB] HTTP批量上传面单: {len(pdfs)} 个")
            # v6.0.62: cache 内 token 被 server 拒时,清 cache → 重试 → ensure_stoken 自动 auto_login (AI 验证码)
            results = None
            for _attempt in (1, 2):
                try:
                    stoken = ensure_stoken(log=self._log)
                    results = upload_label_pdfs_batch(stoken, pdfs, log=self._log)
                    break  # 成功
                except SYBAuthError as _e:
                    if _attempt == 1:
                        try: _TOKEN_CACHE.unlink(missing_ok=True)
                        except Exception: pass
                        self._log(f"[{_now_ts()}] [SYB] stoken 被服务器拒绝,清缓存重新登录后重试...")
                        continue
                    raise
            if results is None:
                raise RuntimeError("upload_label_pdfs_batch 未返回结果")
            ok = sum(1 for r in results if r.get("status"))
            uploaded = sum(1 for r in results if r.get("uploaded"))
            fail = len(results) - uploaded
            self._log(f"[{_now_ts()}] [SYB] HTTP上传结果: {ok}成功 {uploaded-ok}解析异常 {fail}失败")
            for r in results:
                if not r.get("uploaded"):
                    self._log(f"  - {r.get('filename')}: {r.get('msg')}")
            return
        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB] HTTP 上传异常: {e}")
            try:
                messagebox.showerror("失败", f"面单上传异常: {e}\n请重新登录或稍后重试")
            except Exception:
                pass
            return


    def _ui_check_shipped(self):
        raw = (self.var_orders.get() or "").strip()
        if not raw:
            self._log(f"[{_now_ts()}] [SYB] 请输入订单号")
            return
        order_nos = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
        order_nos = order_nos[:50]

        def task(page):
            self._log("[SYB] 打开：物流查询页（准备检查已发货）")

            # 兜底：旧版本若没初始化 _check_results，避免直接炸
            if not hasattr(self, "_check_results") or self._check_results is None:
                self._check_results = {}

            def _wait_stock_ready(timeout: int = 30_000) -> None:
                page.wait_for_selector("div.ctrl-left", state="visible", timeout=timeout)
                end = time.time() + timeout / 1000
                while time.time() < end:
                    if page.locator(".vxe-table").count() > 0 or page.locator(".el-table").count() > 0:
                        return
                    page.wait_for_timeout(200)

            def _click_toolbar(label: str, timeout: int = 10_000) -> None:
                _wait_stock_ready(timeout=max(timeout, 30_000))
                root = page.locator("div.ctrl-left").first
                last_err = None
                end = time.time() + timeout / 1000

                while time.time() < end:
                    try:
                        spans = root.locator("span.txt")
                        for i in range(min(spans.count(), 50)):
                            s = spans.nth(i)
                            if not s.is_visible():
                                continue
                            try:
                                if s.inner_text().strip() != label:
                                    continue
                            except Exception:
                                continue

                            clickable = s.locator("xpath=ancestor::*[self::a or self::button][1]")
                            if clickable.count() == 0:
                                clickable = s

                            clickable.scroll_into_view_if_needed()
                            try:
                                clickable.click(timeout=2000)
                            except Exception:
                                clickable.click(timeout=2000, force=True)
                            return

                        ok = page.evaluate(
                            """(label) => {
                                const root = document.querySelector('div.ctrl-left');
                                if (!root) return false;
                                const txts = Array.from(root.querySelectorAll('span.txt'));
                                const hit = txts.find(x => (x.textContent || '').trim() === label);
                                const el = hit ? (hit.closest('a,button') || hit) : null;
                                if (!el) return false;
                                el.click();
                                return true;
                            }""",
                            label,
                        )
                        if ok:
                            return
                    except Exception as e:
                        last_err = e

                    page.wait_for_timeout(250)

                if last_err:
                    raise last_err
                raise RuntimeError(f"找不到可点击的工具栏按钮：{label}")

            def _ensure_adv_search_panel() -> None:
                # 高级搜索面板不一定默认展开：没看到"订单编号"就点一下高级搜索
                try:
                    lab = page.locator("label", has_text="订单编号").first
                    if lab.count() > 0 and lab.is_visible():
                        return
                except Exception:
                    pass

                _click_toolbar("高级搜索", timeout=10_000)
                page.locator("label", has_text="订单编号").first.wait_for(state="visible", timeout=20_000)

            def _fill_order_no(order_no: str) -> None:
                # ⚠️此页有多个"单号"输入框（快递单号/订单编号/面单号...），必须精准定位"订单编号"这一格
                lab = page.locator("label", has_text="订单编号").first
                # 优先：同一个 el-form-item 内的输入框/多行输入框
                item = lab.locator("xpath=ancestor::div[contains(@class,'el-form-item')][1]").first

                candidates = [
                    item.locator("textarea").first,
                    item.locator("input").first,
                    # 兜底：如果 DOM 结构变化，再用 label 后第一个输入框
                    lab.locator("xpath=following::textarea[1]").first,
                    lab.locator("xpath=following::input[1]").first,
                ]

                last_err: Exception | None = None
                for cand in candidates:
                    try:
                        # count() 在 strict mode 下可能抛错，所以用 try 包住
                        if cand is None:
                            continue
                        cand.wait_for(state="visible", timeout=3000)
                        cand.scroll_into_view_if_needed()
                        cand.click(timeout=3000)
                        cand.fill(order_no)
                        return
                    except Exception as e:
                        last_err = e
                        continue

                raise RuntimeError(f"找不到『订单编号』输入框（可能页面结构变更）：{last_err}")

            def _click_btn(text: str) -> None:
                # 页面上一般只有一组"搜索/重置"
                btn = page.locator("button").filter(has_text=text).first
                btn.scroll_into_view_if_needed()
                btn.click(timeout=5000)

            def _find_row(order_no: str):
                row_vxe = page.locator(".vxe-table--body-wrapper .vxe-body--row").filter(has_text=order_no).first
                row_el = page.locator(".el-table__body-wrapper tbody tr").filter(has_text=order_no).first
                return row_vxe, row_el

            # 1) 打开物流查询页（SPA 需要等 networkidle 确保工具栏渲染）
            page.goto(SYB_STOCK_URL, wait_until="domcontentloaded", timeout=SYB_GOTO_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            cur_url = page.url or ""
            if "/login" in cur_url and "/sys/admin/" not in cur_url:
                raise RuntimeError("物流查询页被重定向到登录页（可能未登录/会话失效）")
            _wait_stock_ready(timeout=30_000)

            # 2) 展开高级搜索
            _ensure_adv_search_panel()

            for order_no in order_nos:
                self._log(f"[SYB] 检查订单：{order_no}")

                # 重置（避免上一次条件残留）
                try:
                    _click_btn("重置")
                except Exception:
                    pass

                _fill_order_no(order_no)

                # 搜索
                _click_btn("搜索")

                # 等结果行出现（vxe / el-table 任一）
                row_vxe, row_el = _find_row(order_no)
                deadline = time.time() + 20
                while time.time() < deadline:
                    if row_vxe.count() > 0 or row_el.count() > 0:
                        break
                    page.wait_for_timeout(250)

                row = row_vxe if row_vxe.count() > 0 else row_el
                if row.count() == 0:
                    self._log(f"[SYB] 结果：未找到订单 {order_no}")
                    self._check_results[order_no] = False
                    continue

                txt = row.inner_text()
                shipped = ("已发货" in txt) or ("已發貨" in txt)

                # 这一页的列表"发货"列经常只显示"转运中/打印中…"，
                # 真正的"已发货"是在【物流记录】弹窗的时间线里。
                if not shipped:
                    try:
                        # 1) 先选中这一行（否则点【物流记录】可能不会打开/或提示未选择）
                        selected = False
                        if row_vxe.count() > 0:
                            rowid = row_vxe.get_attribute("data-rowid") or ""
                            if rowid:
                                cb = page.locator(
                                    f".vxe-table--fixed-left-wrapper .vxe-body--row[data-rowid='{rowid}'] "
                                    f".vxe-cell--checkbox"
                                ).first
                                if cb.count() > 0:
                                    try:
                                        cb.click(timeout=2000)
                                        selected = True
                                    except Exception:
                                        selected = False
                            if not selected:
                                # 兜底：点固定列第一行的 checkbox
                                cb2 = page.locator(
                                    ".vxe-table--fixed-left-wrapper .vxe-table--body-wrapper "
                                    ".vxe-body--row .vxe-cell--checkbox"
                                ).first
                                if cb2.count() > 0:
                                    try:
                                        cb2.click(timeout=2000)
                                        selected = True
                                    except Exception:
                                        selected = False
                            if not selected:
                                # 再兜底：点整行（有的版本会把它当"当前行"）
                                try:
                                    row_vxe.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False
                        else:
                            # el-table 版本
                            cb = row_el.locator("td .el-checkbox__input").first
                            if cb.count() > 0:
                                try:
                                    cb.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False
                            if not selected:
                                try:
                                    row_el.click(timeout=2000)
                                    selected = True
                                except Exception:
                                    selected = False

                        # 2) 打开【物流记录】弹窗并从弹窗内容判断"已发货"
                        #    （不管结果如何，都要自动关闭弹窗）
                        opened = False
                        dlg = None
                        try:
                            _click_toolbar("物流记录")
                            dlg = page.locator(".el-dialog__wrapper:visible").filter(
                                has=page.locator(".el-dialog__title", has_text="物流")
                            ).first
                            dlg.wait_for(state="visible", timeout=6000)
                            opened = True
                        except Exception:
                            # 少量账号/语言会是"物流記錄"
                            try:
                                _click_toolbar("物流記錄")
                                dlg = page.locator(".el-dialog__wrapper:visible").filter(
                                    has=page.locator(".el-dialog__title", has_text="物流")
                                ).first
                                dlg.wait_for(state="visible", timeout=6000)
                                opened = True
                            except Exception:
                                opened = False

                        if opened and dlg is not None:
                            # 等待弹窗内容（时间线）渲染出来，避免"读太快"
                            try:
                                dlg.locator(".el-timeline-item__content").first.wait_for(timeout=5000)
                            except Exception:
                                pass

                            # 取弹窗文本：用正则容忍「已 发 货」被拆成多段/带空白的情况
                            modal_txt = dlg.inner_text()
                            shipped = bool(re.search(r"已\s*发\s*货", modal_txt)) or bool(re.search(r"已\s*發\s*貨", modal_txt))

                            # 自动关闭弹窗
                            try:
                                dlg.locator(".el-dialog__headerbtn").click(timeout=2000)
                            except Exception:
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass

                            # 等弹窗彻底消失，避免下一单点击被遮挡
                            try:
                                dlg.wait_for(state="hidden", timeout=5000)
                            except Exception:
                                pass
                    except Exception as e:
                        self._log(f"[SYB] 警告：检查「物流记录」时出错（将退回列表文本判断）：{e}")

                self._check_results[order_no] = shipped

                if shipped:
                    self._log(f"[SYB] 结果：{order_no} -> 已发货")
                else:
                    # 你说"只要看到已发货就行"，所以这里统一当作未发货/未显示已发货
                    self._log(f"[SYB] 结果：{order_no} -> 未显示『已发货』（视作未发货）")


        self._agent.submit(task, f"检查已发货（{len(order_nos)} 单）")

    # ---------------- app hook ----------------

    def on_ship_results(self, profile_id: str, account_name: str, orders: List[dict], results: List[dict],
                        force_voided_orders: Optional[Set[str]] = None):
        """订单获取/出货结束后的回调。

        app.py 会把从『采购监控 + 闲鱼』来的订单标记为 syb_auto_upload 并写入 syb_latest_template_path。
        这里在"浏览器已保持登录"的前提下，尝试自动上传资料。

        v6.0.75:多 trigger 重複觸發保護移到 _upload_data 內(用「D1 作廢狀態」判斷該不該 +N),
                這裡不再做 order_no 緩存(避免攔下用戶有意作廢後的重新上傳)。
        v6.0.75:force_voided_orders 給「一鍵作廢 + 重上傳」callback 用 — bypass D1 同步延遲。
        """
        # 收集需要上传的模板路径（去重）
        paths: List[Path] = []
        for o in (orders or []):
            try:
                if not o.get("syb_auto_upload"):
                    continue
                p = str(o.get("syb_latest_template_path") or "").strip()
                if p:
                    paths.append(Path(p))
            except Exception:
                pass
        uniq: List[Path] = []
        seen = set()
        for p in paths:
            sp = str(p)
            if sp in seen:
                continue
            seen.add(sp)
            uniq.append(p)
        if not uniq:
            return

        for tpl in uniq:
            if not tpl.exists():
                self._log(f"[{_now_ts()}] [SYB] 自动上传跳过：模板不存在 {tpl}")
                continue
            # 同一个模板里可能同时有 店配+线下，两种都尝试（各自 sheet 有数据才会上传）
            self._log(f"[{_now_ts()}] [SYB] 自动上传：使用模板 {tpl.name}")
            try:
                self._upload_data(tpl, kind="store", force_voided_orders=force_voided_orders)
            except Exception as _e_store:
                self._log(f"[{_now_ts()}] [SYB] store 上傳異常: {_e_store}")
            try:
                self._upload_data(tpl, kind="home", force_voided_orders=force_voided_orders)
            except Exception as _e_home:
                self._log(f"[{_now_ts()}] [SYB] home 上傳異常: {_e_home}")

        # 自动加入发货监控（从上传的最新模板提取订单号）
        try:
            all_order_nos = set()
            for tpl in uniq:
                try:
                    all_order_nos.update(self._extract_order_nos_from_template(tpl))
                except Exception:
                    continue

            if all_order_nos:
                def _do_add():
                    added = self._mon_add_items_bulk(profile_id, account_name, sorted(all_order_nos), source="auto_upload")
                    if added > 0:
                        self._log(f"[{_now_ts()}] [SYB-MON] 已加入发货监控：{added} 单（来源：自动上传）")
                self._ui_call(_do_add)
        except Exception as e:
            self._log(f"[{_now_ts()}] [SYB-MON] 自动加入监控失败：{e}")
