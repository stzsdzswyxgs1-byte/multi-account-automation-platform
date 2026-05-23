"""远程登录自动化模块

通过 Playwright 打开 Chrome，自动填充账号密码，
截图验证码发送到 TG，等待用户回复验证码后填入。
"""
from __future__ import annotations

import asyncio
import json
import time
import tempfile
import threading
import concurrent.futures
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Dict, Optional, Awaitable

# ---------- 常量 ----------

YAHOO_LOGIN_URL = "https://login.yahoo.com/"
YAHOO_MYAUC_URL = "https://tw.bid.yahoo.com/myauc"

# Yahoo 登录页选择器
SEL_USERNAME = 'input[name="username"]'
SEL_USERNAME_NEXT = 'input[name="signin"], button[name="signin"], #login-signin'
SEL_PASSWORD = 'input[name="password"]'
SEL_PASSWORD_NEXT = 'button[name="verifyPassword"], #login-signin'

# 验证码相关选择器
SEL_CAPTCHA_IMG = 'img#captchaV2Img, img[alt*="captcha"], img[src*="captcha"]'
SEL_CAPTCHA_INPUT = 'input[name="captchaAnswer"], input#captchaV2Answer'
SEL_CAPTCHA_SUBMIT = 'button[type="submit"]'

# 二次验证 / 手机验证
SEL_CHALLENGE_CODE = 'input[name="code"], input#verification-code-field'
SEL_CHALLENGE_SUBMIT = 'button[type="submit"], button[data-testid="verify-code-button"]'

# 登录成功判断（只匹配 host+path，不匹配 query 参数）
SUCCESS_URL_PATTERNS = ("tw.bid.yahoo.com", "myauc", "partner")
LOGIN_URL_PATTERNS = ("login.yahoo", "signin", "/login")

# Yahoo 中间页面（密码正确后、真正登录成功前）
TSV_URL_PATTERN = "tsv-authenticator"


def _url_host_path(url: str) -> str:
    """提取 URL 的 host+path 部分（小写），排除 query 参数。"""
    try:
        p = urlparse(url)
        return (p.netloc + p.path).lower()
    except Exception:
        return url.lower()


class RemoteLoginSession:
    """一次远程登录会话，由 TG 指令触发。"""

    def __init__(
        self,
        profile_dir: str,
        browser_path: str,
        on_log: Callable[[str], None],
        proxy: str = "",
    ):
        self.profile_dir = profile_dir
        self.browser_path = browser_path
        self.on_log = on_log
        self.proxy = proxy

        self._pw = None
        self._ctx = None
        self._page = None
        self._closed = False

        # 专属事件循环 + 线程，确保所有 Playwright 操作在同一线程
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _start_loop(self) -> None:
        """启动专属后台线程和事件循环。"""
        if self._loop and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def run_async(self, coro, timeout: float = 120) -> Any:
        """从任意线程安全地提交协程到专属循环并等待结果。"""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("登录会话的事件循环未运行")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_chrome_prefs(profile: Path) -> None:
        """在启动前修改 Chrome Preferences，禁用密码管理器弹窗。"""
        prefs_dir = profile / "Default"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        prefs_file = prefs_dir / "Preferences"

        prefs = {}
        if prefs_file.exists():
            try:
                prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
            except Exception:
                prefs = {}

        # 禁用密码保存提示
        creds = prefs.setdefault("credentials_enable_service", False)
        if creds is not False:
            prefs["credentials_enable_service"] = False

        pwd_mgr = prefs.setdefault("password_manager", {})
        if not isinstance(pwd_mgr, dict):
            pwd_mgr = {}
            prefs["password_manager"] = pwd_mgr
        pwd_mgr["enabled"] = False
        pwd_mgr["leak_detection"] = False

        profile_prefs = prefs.setdefault("profile", {})
        if not isinstance(profile_prefs, dict):
            profile_prefs = {}
            prefs["profile"] = profile_prefs
        profile_prefs["password_manager_enabled"] = False

        try:
            prefs_file.write_text(
                json.dumps(prefs, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _async_start(self) -> None:
        """内部：在专属线程的事件循环中启动浏览器。"""
        from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

        self._pw = await async_playwright().start()

        profile = Path(self.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)

        # 禁用 Chrome 密码管理器弹窗
        self._patch_chrome_prefs(profile)

        launch_kw: Dict[str, Any] = dict(
            user_data_dir=str(profile),
            headless=False,
            no_viewport=True,   # 非 headless 不设 viewport（Patchright 兼容）
            args=get_launch_args(headless=False, lang="zh-TW", extra=[
                "--disable-save-password-bubble",
                "--password-store=basic",
            ]),
            ignore_default_args=get_ignore_default_args(headless=False),
        )
        if self.browser_path:
            launch_kw["executable_path"] = self.browser_path
        if self.proxy:
            launch_kw["proxy"] = {"server": self.proxy}

        self._ctx = await self._pw.chromium.launch_persistent_context(
            **launch_kw
        )
        await apply_runtime_normalization_async(self._ctx)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.on_log("[REMOTE-LOGIN] 浏览器已启动")

    def start_sync(self) -> None:
        """同步启动：创建专属线程 + 事件循环，在其中启动浏览器。"""
        self._start_loop()
        self.run_async(self._async_start())

    async def _async_close(self) -> None:
        """内部：在专属线程中关闭浏览器。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.on_log("[REMOTE-LOGIN] 浏览器已关闭")

    def close_sync(self) -> None:
        """同步关闭：关闭浏览器并停止专属事件循环。"""
        if self._closed:
            return
        try:
            if self._loop and self._loop.is_running():
                self.run_async(self._async_close(), timeout=15)
        except Exception:
            pass
        # 停止事件循环
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------
    # 步骤 1：导航到目标页，检查是否需要登录
    # ------------------------------------------------------------------

    async def navigate_and_check(self, start_url: str = "") -> str:
        """导航到目标页面，返回状态。

        Returns:
            "already_logged_in" - 已登录，无需操作
            "need_login"        - 需要登录
            "error"             - 出错
        """
        url = start_url or YAHOO_MYAUC_URL
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            hp = _url_host_path(self._page.url or "")
            if any(p in hp for p in LOGIN_URL_PATTERNS):
                self.on_log(f"[REMOTE-LOGIN] 需要登录: {self._page.url}")
                return "need_login"

            if any(p in hp for p in SUCCESS_URL_PATTERNS):
                self.on_log("[REMOTE-LOGIN] 已登录，无需操作")
                return "already_logged_in"

            # 不确定，可能是登录页的变体
            self.on_log(f"[REMOTE-LOGIN] 未知页面: {self._page.url}")
            return "need_login"
        except Exception as e:
            self.on_log(f"[REMOTE-LOGIN] 导航失败: {e}")
            return "error"

    # ------------------------------------------------------------------
    # 步骤 2：填入用户名
    # ------------------------------------------------------------------

    async def fill_username(self, username: str) -> str:
        """填入用户名并点击下一步。

        Returns:
            "ok"       - 成功，等待密码
            "captcha"  - 出现验证码
            "error"    - 出错
        """
        try:
            # 等待用户名输入框
            await self._page.wait_for_selector(
                SEL_USERNAME, state="visible", timeout=10000
            )
            await self._page.fill(SEL_USERNAME, username)
            await asyncio.sleep(0.5)

            # 点击下一步
            clicked = False
            for sel in (SEL_USERNAME_NEXT, 'button[type="submit"]'):
                try:
                    btn = self._page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                # 尝试按 Enter
                await self._page.press(SEL_USERNAME, "Enter")

            await asyncio.sleep(2)
            return await self._detect_after_username()
        except Exception as e:
            self.on_log(f"[REMOTE-LOGIN] 填入用户名失败: {e}")
            return "error"

    async def _detect_after_username(self) -> str:
        """用户名提交后检测页面状态。"""
        # 检查是否出现密码框
        try:
            await self._page.wait_for_selector(
                SEL_PASSWORD, state="visible", timeout=8000
            )
            return "ok"
        except Exception:
            pass

        # 检查是否出现验证码
        if await self._has_captcha():
            return "captcha"

        # 检查是否有错误提示
        return "error"

    # ------------------------------------------------------------------
    # 步骤 3：填入密码
    # ------------------------------------------------------------------

    async def fill_password(self, password: str) -> str:
        """填入密码并提交。

        Returns:
            "success"    - 登录成功
            "captcha"    - 出现验证码
            "challenge"  - 出现二次验证
            "error"      - 出错
        """
        try:
            await self._page.wait_for_selector(
                SEL_PASSWORD, state="visible", timeout=8000
            )
            await self._page.fill(SEL_PASSWORD, password)
            await asyncio.sleep(0.5)

            # 点击登录
            clicked = False
            for sel in (SEL_PASSWORD_NEXT, 'button[type="submit"]'):
                try:
                    btn = self._page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                await self._page.press(SEL_PASSWORD, "Enter")

            await asyncio.sleep(3)
            return await self._detect_after_password()
        except Exception as e:
            self.on_log(f"[REMOTE-LOGIN] 填入密码失败: {e}")
            return "error"

    async def _detect_after_password(self) -> str:
        """密码提交后检测页面状态。"""
        hp = _url_host_path(self._page.url or "")

        # 登录成功（只检查 host+path，不检查 query 参数）
        if any(p in hp for p in SUCCESS_URL_PATTERNS):
            self.on_log("[REMOTE-LOGIN] 登录成功")
            return "success"

        # 检查是否在 tsv-authenticator 中间页（保持通過驗證狀態）
        if TSV_URL_PATTERN in hp:
            return await self._handle_tsv_page()

        # 检查验证码
        if await self._has_captcha():
            return "captcha"

        # 检查二次验证
        if await self._has_challenge():
            return "challenge"

        # 检查密码错误提示（Yahoo 页面红字）
        if await self._has_password_error():
            self.on_log("[REMOTE-LOGIN] 密码错误")
            return "wrong_password"

        # 仍在登录页 → 可能密码错误
        if any(p in hp for p in LOGIN_URL_PATTERNS):
            self.on_log("[REMOTE-LOGIN] 仍在登录页，可能密码错误")
            return "wrong_password"

        # 等一下再检查（页面可能还在跳转）
        await asyncio.sleep(3)
        hp = _url_host_path(self._page.url or "")
        if any(p in hp for p in SUCCESS_URL_PATTERNS):
            self.on_log("[REMOTE-LOGIN] 登录成功")
            return "success"
        if TSV_URL_PATTERN in hp:
            return await self._handle_tsv_page()

        return "error"

    # ------------------------------------------------------------------
    # 步骤 4：处理验证码
    # ------------------------------------------------------------------

    async def fill_captcha(self, code: str) -> str:
        """填入验证码并提交。

        Returns:
            "success"   - 登录成功
            "challenge" - 出现二次验证
            "retry"     - 验证码错误，需重试
            "error"     - 出错
        """
        try:
            # 尝试填入验证码输入框
            filled = False
            for sel in (SEL_CAPTCHA_INPUT, SEL_CHALLENGE_CODE):
                try:
                    el = self._page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        await el.fill(code)
                        filled = True
                        break
                except Exception:
                    continue

            if not filled:
                self.on_log("[REMOTE-LOGIN] 找不到验证码输入框")
                return "error"

            await asyncio.sleep(0.5)

            # 点击提交
            for sel in (SEL_CAPTCHA_SUBMIT, SEL_CHALLENGE_SUBMIT):
                try:
                    btn = self._page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(3)
            return await self._detect_after_password()
        except Exception as e:
            self.on_log(f"[REMOTE-LOGIN] 填入验证码失败: {e}")
            return "error"

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    async def screenshot(self) -> Optional[str]:
        """截取当前页面，返回临时文件路径。"""
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", prefix="tg_login_", delete=False
            )
            tmp.close()
            await self._page.screenshot(path=tmp.name, full_page=False)
            self.on_log(f"[REMOTE-LOGIN] 截图: {tmp.name}")
            return tmp.name
        except Exception as e:
            self.on_log(f"[REMOTE-LOGIN] 截图失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 内部检测
    # ------------------------------------------------------------------

    async def _handle_tsv_page(self) -> str:
        """处理 Yahoo tsv-authenticator 中间页（保持通過驗證狀態）。

        自动点击「保持通過驗證狀態」按钮，然后等待跳转。
        如果跳转到目标页 → success
        如果出现验证码/二次验证 → 返回对应状态
        """
        self.on_log("[REMOTE-LOGIN] 检测到中间验证页，尝试自动跳过...")

        # 用 force=True 点击，替代路径 Chrome 保存密码弹窗的遮挡
        clicked = await self._click_tsv_button()

        if not clicked:
            # 后备：用 JavaScript 直接点击
            clicked = await self._click_tsv_button_js()

        if not clicked:
            self.on_log("[REMOTE-LOGIN] 未找到可点击的按钮，等待用户操作")
            return "challenge"

        # 等待页面跳转
        await asyncio.sleep(5)
        return await self._check_after_tsv()

    async def _click_tsv_button(self) -> bool:
        """尝试用 Playwright 点击 tsv 页面按钮（只匹配按钮元素）。"""
        # 用 get_by_role("button") 限定只匹配按钮，避免点到标题文字
        for text_pattern in [
            "保持通過驗證狀態",
            "保持通过验证状态",
            "Stay signed in",
            "現在不要",
            "现在不要",
            "Not now",
        ]:
            try:
                btn = self._page.get_by_role(
                    "button", name=text_pattern, exact=False
                ).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(force=True)
                    self.on_log(f"[REMOTE-LOGIN] 已点击按钮「{text_pattern}」")
                    return True
            except Exception:
                continue

        # 也尝试 link role（有些按钮是 <a> 标签）
        for text_pattern in [
            "保持通過驗證狀態",
            "現在不要",
        ]:
            try:
                btn = self._page.get_by_role(
                    "link", name=text_pattern, exact=False
                ).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(force=True)
                    self.on_log(f"[REMOTE-LOGIN] 已点击链接「{text_pattern}」")
                    return True
            except Exception:
                continue

        # 最后尝试通用 submit 按钮
        try:
            btn = self._page.locator('button[type="submit"]').first
            if await btn.is_visible(timeout=2000):
                await btn.click(force=True)
                self.on_log("[REMOTE-LOGIN] 已点击提交按钮")
                return True
        except Exception:
            pass
        return False

    async def _click_tsv_button_js(self) -> bool:
        """后备方案：用 JavaScript 直接点击 tsv 页面按钮。"""
        try:
            result = await self._page.evaluate("""() => {
                const keywords = [
                    '保持通過驗證狀態', '保持通过验证状态',
                    'Stay signed in', '現在不要', '现在不要', 'Not now'
                ];
                const buttons = document.querySelectorAll(
                    'button, a[role="button"], input[type="submit"]'
                );
                for (const btn of buttons) {
                    const txt = (btn.textContent || btn.value || '').trim();
                    for (const kw of keywords) {
                        if (txt.includes(kw)) {
                            btn.click();
                            return kw;
                        }
                    }
                }
                // 最后尝试第一个 submit 按钮
                const sub = document.querySelector('button[type="submit"]');
                if (sub) { sub.click(); return 'submit'; }
                return null;
            }""")
            if result:
                self.on_log(f"[REMOTE-LOGIN] JS点击「{result}」")
                return True
        except Exception:
            pass
        return False

    async def _check_after_tsv(self) -> str:
        """tsv 按钮点击后检查页面状态。"""
        hp = _url_host_path(self._page.url or "")

        if any(p in hp for p in SUCCESS_URL_PATTERNS):
            self.on_log("[REMOTE-LOGIN] 登录成功")
            return "success"

        # 可能跳到了验证码或二次验证
        if await self._has_captcha():
            return "captcha"
        if await self._has_challenge():
            return "challenge"

        # 再等一下
        await asyncio.sleep(3)
        hp = _url_host_path(self._page.url or "")
        if any(p in hp for p in SUCCESS_URL_PATTERNS):
            self.on_log("[REMOTE-LOGIN] 登录成功")
            return "success"

        self.on_log(f"[REMOTE-LOGIN] 中间页处理后仍未成功: {self._page.url}")
        return "challenge"

    async def _has_captcha(self) -> bool:
        """检测页面是否有验证码图片。"""
        try:
            el = self._page.locator(SEL_CAPTCHA_IMG).first
            return await el.is_visible(timeout=2000)
        except Exception:
            return False

    async def _has_challenge(self) -> bool:
        """检测页面是否有二次验证输入框。"""
        try:
            el = self._page.locator(SEL_CHALLENGE_CODE).first
            return await el.is_visible(timeout=2000)
        except Exception:
            return False

    async def _has_password_error(self) -> bool:
        """检测页面是否显示密码错误提示。"""
        try:
            # Yahoo 密码错误时显示的红字关键词
            error_texts = [
                "無效密碼", "无效密码", "Invalid password",
                "密碼不正確", "密码不正确", "incorrect password",
                "請再試一次", "请再试一次",
            ]
            body = await self._page.inner_text("body", timeout=3000)
            return any(t.lower() in body.lower() for t in error_texts)
        except Exception:
            return False

    async def get_page_url(self) -> str:
        """获取当前页面 URL。"""
        try:
            return self._page.url or ""
        except Exception:
            return ""
