"""
采购监控用 — 闲鱼 + 煤炉 登录浏览器（单浏览器版）

唯一改动点：用 Playwright 替代 system Chrome subprocess。
为什么改：Chrome SQLite 不持久化 session cookies (cookie2/_tb_token_)，
        关闭时序错就丢登录态。Playwright 通过 browser.cookies() 直接拿到
        进程内全部 cookies，写到 JSON，不再依赖 Chrome 怎么关。

不变点：
- 用同一个 PURCHASE_PROFILE_DIR (profiles/purchase_monitor)
  → 打开就能看到上次登录的账号
- 单浏览器，goofish + mercari 各开一个 tab
- 煤炉的 LevelDB 由同一个 Chromium 写入，token_store 不受影响
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Callable, List

LogFn = Callable[[str], None]

BASE_DIR = Path(__file__).resolve().parent.parent
# ★ 关键：用同一个 profile 目录，跟 system Chrome 之前用的一样
#   这样打开浏览器就能看到上次登录的账号（如果 cookies 还在）
PURCHASE_PROFILE_DIR = BASE_DIR / "profiles" / "purchase_monitor"
CACHE_JSON_PATH = PURCHASE_PROFILE_DIR / "goofish_cookie_cache.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

RUNTIME_COMPAT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
try { delete navigator.__proto__.webdriver; } catch(e) {}
if (!window.chrome || !window.chrome.runtime) {
    window.chrome = { runtime: {}, loadTimes: function(){return{};}, csi: function(){return{};}, app: {} };
}
"""

_PUNISH_NAMES = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}


def _save_cookies_to_cache(raw_cookies: List[dict], log: LogFn) -> bool:
    """Playwright cookies → goofish_cookie_cache.json (HTTP 抓取期望的格式)

    只保存 Alibaba 系(闲鱼/淘宝)域名的 cookie。煤炉、广告追踪等其它域名的 cookie 由
    Chrome SQLite profile 保存,不进入此 cache,避免 Cookie header 超过 8KB 触发 431。
    """
    # 复用 goofish_cookie_store 的 domain 白名单,保持一致
    from .goofish_cookie_store import _is_goofish_domain

    filtered = [
        c for c in raw_cookies
        if c.get("name") not in _PUNISH_NAMES
        and "_____tmd_____" not in (c.get("path") or "")
        and "punish" not in (c.get("path") or "").lower()
        and _is_goofish_domain(c.get("domain", ""))
    ]
    cookie_dict = {c["name"]: c.get("value", "") for c in filtered if c.get("name")}

    unb = cookie_dict.get("unb", "")
    m_h5_tk = cookie_dict.get("_m_h5_tk", "")
    token_hex = m_h5_tk.split("_")[0] if m_h5_tk and "_" in m_h5_tk else ""

    # 闲鱼没登录时跳过保存（避免覆盖之前的好 cookie）
    if not unb:
        return False
    # 关键 cookie 残缺时也跳过 — 闲鱼登录是分批下发的（unb 先到, cookie2 后到 几百ms）
    # 残缺 cookie 集会被服务端判定未登录，注入回去浏览器还是弹登录框
    missing = [k for k in _REQUIRED_COOKIES if not cookie_dict.get(k, "").strip()]
    if missing:
        return False

    data = {
        "cookies": cookie_dict,
        "m_h5_tk": m_h5_tk,
        "m_h5_tk_enc": cookie_dict.get("_m_h5_tk_enc", ""),
        "token_hex": token_hex,
        "account_hint": f"playwright|unb={unb[:12]}",
        "saved_at": time.time(),
        "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookie_count": len(cookie_dict),
        "raw_cookies": filtered,
    }
    CACHE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"[采购登录] 已保存 {len(cookie_dict)} 条 cookie (unb={unb[:8]})")
    return True


# 闲鱼完整登录态需要的关键 cookie（缺一就会被服务端判定未登录）
_REQUIRED_COOKIES = ("unb", "cookie2", "_tb_token_", "sgcookie")


def _has_valid_cached_unb() -> bool:
    """启动时检测：cache.json 是否已有完整登录态（不只是 unb，还要 cookie2 等关键 cookie）。
    残缺的 cookie set 会被闲鱼判定为未登录 → 浏览器仍然弹登录框。
    """
    try:
        if not CACHE_JSON_PATH.exists():
            return False
        with open(CACHE_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies") or {}
        missing = [k for k in _REQUIRED_COOKIES if not cookies.get(k, "").strip()]
        if missing:
            return False
        return True
    except Exception:
        return False


def wipe_cookie_cache() -> bool:
    """彻底清空闲鱼登录痕迹：
    - cache.json
    - Chrome Cookies SQLite
    - Local Storage / Session Storage / IndexedDB / Service Worker 里 闲鱼 域的文件
    （只清这些，不动 Chrome profile 的其他东西，避免影响 煤炉 登录）
    """
    import shutil
    ok = True
    try:
        if CACHE_JSON_PATH.exists():
            CACHE_JSON_PATH.unlink()
    except Exception:
        ok = False
    # 删 Chrome Cookies SQLite
    for cookie_file in ("Cookies", "Cookies-journal"):
        for base in (PURCHASE_PROFILE_DIR / "Default", PURCHASE_PROFILE_DIR):
            f = base / cookie_file
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass
    # 删 闲鱼 域的 Local Storage / IndexedDB / Service Worker 文件
    # Chrome 这些是按 origin 分目录或文件命名的，含 "goofish" 字样的全删
    storage_dirs = [
        PURCHASE_PROFILE_DIR / "Default" / "Local Storage" / "leveldb",
        PURCHASE_PROFILE_DIR / "Default" / "Session Storage",
        PURCHASE_PROFILE_DIR / "Default" / "IndexedDB",
        PURCHASE_PROFILE_DIR / "Default" / "Service Worker" / "ScriptCache",
        PURCHASE_PROFILE_DIR / "Default" / "Service Worker" / "Database",
    ]
    for sd in storage_dirs:
        if not sd.exists():
            continue
        try:
            for child in sd.iterdir():
                # leveldb 文件没有 origin 名字，直接全删（影响整个 leveldb 但 煤炉 cookie 不在里面）
                # 子目录形式（IndexedDB 等）只删名字含 goofish/taobao 的
                if child.is_file():
                    if sd.name == "leveldb":
                        # leveldb 是共享的，删全部 — 不删 goofish 进不来
                        try: child.unlink()
                        except Exception: pass
                    elif "goofish" in child.name.lower() or "taobao" in child.name.lower():
                        try: child.unlink()
                        except Exception: pass
                elif child.is_dir():
                    if "goofish" in child.name.lower() or "taobao" in child.name.lower():
                        try: shutil.rmtree(child, ignore_errors=True)
                        except Exception: pass
        except Exception:
            pass
    return ok


def _aggressive_clean_locks(profile_dir: Path) -> None:
    """清理 Chrome 残留锁文件，包括根目录和 Default 子目录"""
    for base in (profile_dir, profile_dir / "Default"):
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "SingletonPort", "LOCK"):
            f = base / lock_name
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass


def _kill_chrome_using_profile(profile_dir: Path, log: LogFn) -> int:
    """杀掉所有 chrome.exe 进程，命令行里包含 --user-data-dir=<profile_dir> 的。
    用于解决「Browser window not found」错误（Chrome 检测到同 profile 已有实例 → exit(0) → Playwright 失败）。
    返回杀掉的进程数。
    """
    try:
        import psutil
    except ImportError:
        log("[采购登录] ⚠ psutil 未装，无法清理残留 Chrome 进程")
        return 0
    target = str(profile_dir).replace("/", "\\").lower()
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chrome" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            cmd_lower = " ".join(cmdline).lower()
            if target in cmd_lower:
                proc.kill()
                killed += 1
        except Exception:
            continue
    if killed:
        log(f"[采购登录] 已清理 {killed} 个残留 Chrome 进程，等待文件锁释放...")
        time.sleep(2)
        # 顺便清 SingletonLock，刚被 kill 的 Chrome 可能没机会清理
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            f = profile_dir / lock_name
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass
    return killed


def _load_raw_cookies_from_cache() -> List[dict]:
    """加载 cache.json 里的 raw Playwright cookie 列表，用于打开浏览器时注入。
    Chrome SQLite 不持久化 session cookie（闲鱼的 unb/_m_h5_tk 全是），
    所以每次打开浏览器要从 JSON 主动 add_cookies 才能让 UI 显示已登录。
    """
    try:
        if not CACHE_JSON_PATH.exists():
            return []
        with open(CACHE_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("raw_cookies", [])
        if isinstance(raw, list):
            # Playwright add_cookies 要求 sameSite ∈ {Strict, Lax, None}，无值或不合法时去掉
            cleaned = []
            for c in raw:
                if not isinstance(c, dict):
                    continue
                cc = dict(c)
                ss = cc.get("sameSite")
                if ss and str(ss).capitalize() not in ("Strict", "Lax", "None"):
                    cc.pop("sameSite", None)
                elif ss:
                    cc["sameSite"] = str(ss).capitalize()
                cleaned.append(cc)
            return cleaned
    except Exception:
        pass
    return []


async def _polling_save_loop(browser, log: LogFn) -> int:
    """有 cookie 路径（兼容现有同事）：4 秒轮询保存，浏览器关闭才退出。
    维持 v6.0.x 之前的行为，0 影响已正常工作的用户。
    """
    save_count = 0
    last_unb = ""
    while True:
        try:
            await asyncio.sleep(4)
            cookies = await browser.cookies()
            unb = ""
            for c in cookies:
                if c.get("name") == "unb":
                    unb = c.get("value", "")
                    break
            if unb:
                if unb != last_unb:
                    if _save_cookies_to_cache(cookies, log):
                        save_count += 1
                        last_unb = unb
                else:
                    _save_cookies_to_cache(cookies, log=lambda m: None)
                    save_count += 1
        except Exception:
            break
    return save_count


async def _wait_login_then_close(browser, page1, log: LogFn) -> bool:
    """无 cookie 路径：1 秒轮询等 unb 出现 → BX 限流指纹采集 → 保存 → 自动关。
    返回 True = 成功保存，False = 超时未登录。
    最多等 10 分钟（600 秒）。
    """
    # 防呆：浏览器一开就有 unb（来自 Chrome 残留 + localStorage/IndexedDB 自动恢复），
    # 闲鱼会用 localStorage 里的 deviceId / loginToken 等自动重建 session，
    # 即使我们清了 cookie，3 秒内 unb 又会被 闲鱼 JS 自动写回来 → 跑 BX 是死 session。
    # 解决：先清 cookie + clear localStorage + sessionStorage + IndexedDB（仅 goofish 域），
    # 再 reload 页面，让 闲鱼 真正认为是第一次访问。
    try:
        existing = await browser.cookies()
        had_unb = any(c.get("name") == "unb" for c in existing)
        if had_unb:
            log("[采购登录] 检测到残留 unb（非真登录），清空浏览器 cookie + localStorage + IndexedDB...")
            await browser.clear_cookies()
            # 在 闲鱼 域内执行 JS 清掉它的本地存储（防止 闲鱼 用 deviceId 自动恢复 session）
            try:
                await page1.evaluate("""async () => {
                    try { localStorage.clear(); } catch(e) {}
                    try { sessionStorage.clear(); } catch(e) {}
                    try {
                        if (indexedDB.databases) {
                            const dbs = await indexedDB.databases();
                            for (const db of dbs) {
                                try { indexedDB.deleteDatabase(db.name); } catch(e) {}
                            }
                        }
                    } catch(e) {}
                }""")
                log("[采购登录] ✓ 已清 localStorage / sessionStorage / IndexedDB")
            except Exception as e:
                log(f"[采购登录] 清本地存储异常（忽略）：{e}")
            try:
                await page1.goto("https://www.goofish.com/",
                                 wait_until="domcontentloaded", timeout=20000)
                # 再等一秒看 unb 是否又被自动恢复（确认彻底清干净）
                await asyncio.sleep(2)
                cookies_after = await browser.cookies()
                if any(c.get("name") == "unb" for c in cookies_after):
                    log("[采购登录] ⚠ unb 又被自动恢复了（闲鱼可能用了其他存储），再清一次 cookie")
                    await browser.clear_cookies()
                    await page1.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
    except Exception as e:
        log(f"[采购登录] 清残留 cookie 异常（忽略）：{e}")

    log("[采购登录] 等待你登录闲鱼...（10 分钟超时）")
    for attempt in range(600):
        try:
            cookies = await browser.cookies()
        except Exception:
            return False
        unb = ""
        for c in cookies:
            if c.get("name") == "unb":
                unb = c.get("value", "")
                break
        if unb:
            log(f"[采购登录] ✓ 检测到登录成功（unb={unb[:8]}），开始 BX 限流指纹采集...")
            await asyncio.sleep(2)
            # 关键步骤：让 BX SDK 给 awesome.detail.unit 端点颁发 x5sec
            # 不做这步保存的 cookie 服务端会拒绝
            try:
                from core.goofish_login_playwright import _enrich_cookies_via_browse
                await _enrich_cookies_via_browse(page1, log)
            except Exception as e:
                log(f"[采购登录] ⚠ BX 指纹采集异常（继续保存）：{e}")
            # 二次取 cookie（BX 期间会刷新 cookie）
            try:
                cookies = await browser.cookies()
            except Exception:
                return False
            if _save_cookies_to_cache(cookies, log):
                log("[采购登录] ✓ cookie 已保存，3 秒后自动关闭浏览器")
                await asyncio.sleep(3)
                return True
            log("[采购登录] ⚠ 保存失败")
            return False
        await asyncio.sleep(1)
        if attempt > 0 and attempt % 30 == 0:
            log(f"[采购登录] 等待登录中... ({attempt}s)")
    log("[采购登录] ⚠ 等待登录超时（10 分钟）")
    return False


async def _async_open_browser(log: LogFn) -> bool:
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log(f"[采购登录] Playwright 未安装：{e}")
        return False

    PURCHASE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # 主动 kill 残留 Chrome 进程（上次没正常关 / 监控 headless 还在跑）
    _kill_chrome_using_profile(PURCHASE_PROFILE_DIR, log)
    # 清残留锁（包括 Default 子目录）
    _aggressive_clean_locks(PURCHASE_PROFILE_DIR)

    # 启动时判断：决定走「无 cookie 自动关」还是「有 cookie 不自动关」
    had_cookie_at_start = _has_valid_cached_unb()

    pw = await async_playwright().start()
    browser = None
    _launch_kw = dict(
        user_data_dir=str(PURCHASE_PROFILE_DIR),
        headless=False,
        channel="chrome",
        viewport={"width": 1280, "height": 800},
        user_agent=UA,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
        ],
        ignore_default_args=["--enable-automation"],
    )
    try:
        try:
            browser = await pw.chromium.launch_persistent_context(**_launch_kw)
        except Exception as e:
            err_msg = str(e)
            # Chrome 检测到同 profile 已有实例 → exit(0) → 这种特征错误下杀残留进程后重试
            if "Browser window not found" in err_msg or "process did exit: exitCode=0" in err_msg:
                log(f"[采购登录] ⚠ Chrome 启动冲突（profile 被占用），尝试清理残留进程...")
                _kill_chrome_using_profile(PURCHASE_PROFILE_DIR, log)
                await asyncio.sleep(1)
                browser = await pw.chromium.launch_persistent_context(**_launch_kw)
                log(f"[采购登录] ✓ 清理后重试成功")
            else:
                raise

        # 注入 cache.json 里的 cookie（Chrome SQLite 不持久化 session cookie，每次都要重新塞）
        if had_cookie_at_start:
            saved_raw = _load_raw_cookies_from_cache()
            if saved_raw:
                try:
                    await browser.add_cookies(saved_raw)
                    log(f"[采购登录] 已注入 {len(saved_raw)} 条 cookie 到浏览器（恢复登录态）")
                except Exception as e:
                    log(f"[采购登录] cookie 注入失败（继续）：{e}")

        # tab 1: 闲鱼
        page1 = browser.pages[0] if browser.pages else await browser.new_page()
        try:
            await page1.add_init_script(RUNTIME_COMPAT_JS)
        except Exception:
            pass

        # v6.0.75:在 page1 上註冊 login.token 攔截器(整個瀏覽器生命週期都監聽)
        ws_token_captured = await _setup_ws_token_capture(page1, log)

        try:
            await page1.goto("https://www.goofish.com/",
                             wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"[采购登录] 打开闲鱼失败（继续）: {e}")

        # 关键：闲鱼对部分账号/设备会弹「快速进入」对话框，必须点击才能激活完整 session
        # 不点的话 cookie 是「半登录态」，BX 限流判可疑，下次重开依然弹同一个对话框
        try:
            from core.goofish_login_playwright import _auto_click_quick_login
            await _auto_click_quick_login(page1, log)
        except Exception as e:
            log(f"[采购登录] 「快速进入」检测异常（忽略）：{e}")

        # tab 2: 煤炉
        page2 = await browser.new_page()
        try:
            await page2.goto("https://jp.mercari.com/",
                             wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"[采购登录] 打开煤炉失败（继续）: {e}")

        if had_cookie_at_start:
            # 已登录路径：轮询保存，不自动关，让用户看订单
            log("[采购登录] 浏览器已打开（检测到已有 cookie）")
            log("[采购登录] cookies 持续自动保存中，看完订单关闭浏览器即可")
            # v6.0.75:已登錄就先去 /im 拿 WS access_token (24h 緩存)
            await _navigate_to_im_for_token(browser, ws_token_captured, log)
            save_count = await _polling_save_loop(browser, log)
            log(f"[采购登录] 浏览器已关闭，cookies 累计保存 {save_count} 次")
        else:
            # 首次登录路径：等登录 → BX 指纹 → 保存 → 自动关
            log("[采购登录] 浏览器已打开 [闲鱼+煤炉]，未检测到 cookie，请扫码登录闲鱼")
            ok = await _wait_login_then_close(browser, page1, log)
            if ok:
                # v6.0.75:首次登錄成功後也順手拿 WS access_token
                await _navigate_to_im_for_token(browser, ws_token_captured, log)
            else:
                log("[采购登录] 登录流程未完成，退化为持续保存模式（请手动关闭浏览器）")
                save_count = await _polling_save_loop(browser, log)
                log(f"[采购登录] 浏览器已关闭，cookies 累计保存 {save_count} 次")
    except Exception as e:
        log(f"[采购登录] 异常：{e}")
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
    return True


async def _setup_ws_token_capture(page, log: LogFn) -> dict:
    """v6.0.75:在「打開登錄瀏覽器」流程內,順手攔截 login.token 響應拿 access_token。

    重要:同時抓 request 內的 deviceId,同步給 Python (token 跟 deviceId 強綁,
    必須一致才能用 — 不然 server 401 'device id or appkey is not equal')。
    """
    captured = {"access": "", "refresh": "", "exp_ms": 0, "device_id": ""}

    async def _on_response(resp):
        try:
            url = resp.url
            if "mtop.taobao.idlemessage.pc.login.token" not in url:
                return

            # 1. 從 request 抽 deviceId(URL-encoded JSON,先 unquote 再 regex)
            try:
                from urllib.parse import unquote
                req = resp.request
                src_combined = unquote(unquote((req.url or "") + "|" + (req.post_data or "")))
                import re as _re2
                m_did = _re2.search(r'"deviceId"\s*:\s*"([^"]+)"', src_combined)
                if m_did:
                    captured["device_id"] = m_did.group(1)
                    log(f"[采购登录] ✓ 從 request 抽到 deviceId: {captured['device_id']}")
            except Exception as _e_req:
                log(f"[采购登录] 抽 deviceId 異常 (繼續): {_e_req}")

            # 2. 從 response body 抽 access_token
            try:
                body = await resp.text()
            except Exception:
                return
            import re as _re
            m = _re.search(r'\{.*\}', body, _re.DOTALL)
            if not m:
                return
            j = json.loads(m.group())
            ret = j.get("ret", [])
            if not any("SUCCESS" in str(x) for x in ret):
                log(f"[采购登录] login.token 攔截到但非 SUCCESS: {ret}")
                return
            data = j.get("data", {}) or {}
            tk = str(data.get("accessToken", "") or "")
            if tk:
                captured["access"] = tk
                captured["refresh"] = str(data.get("refreshToken", "") or "")
                try:
                    captured["exp_ms"] = int(data.get("accessTokenExpiredTime", 0) or 0)
                except Exception:
                    captured["exp_ms"] = 86400000
                log(f"[采购登录] ✓ 攔截到 WS access_token: {tk[:24]}...{tk[-12:]} (len={len(tk)})")
        except Exception as e:
            log(f"[采购登录] login.token 攔截異常: {e}")

    page.on("response", _on_response)
    return captured


async def _navigate_to_im_for_token(browser, captured: dict, log: LogFn) -> bool:
    """v6.0.75:cookie 保存完成後,navigate /im 觸發 login.token API 拿 access_token。

    瀏覽器是用戶剛剛掃碼互動過的真實 session,fingerprint 健康,
    server RGV587 异常码不會擋,login.token 應該成功。
    """
    try:
        # 用既有 page (頁面已有 fingerprint warmup),不要開新 tab
        page = browser.pages[0] if browser.pages else None
        if not page:
            log("[采购登录] 無可用 page,無法取 WS token")
            return False

        log("[采购登录] 訪問 /im 觸發 login.token API (順手拿 WS access_token)...")
        try:
            await page.goto("https://www.goofish.com/im",
                           wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            log(f"[采购登录] /im 訪問異常 (繼續等 login.token): {e}")

        # 等最多 15 秒讓頁面初始化呼 login.token
        for _ in range(30):
            await asyncio.sleep(0.5)
            if captured.get("access"):
                break

        if captured.get("access"):
            # v6.0.75:必須先把瀏覽器的 deviceId 同步寫入,Python 後續用它連 WS 才不會被 server 401
            browser_did = captured.get("device_id", "")
            if browser_did:
                try:
                    import json as _json
                    import time as _time
                    device_fp = Path(__file__).resolve().parent.parent / "runtime" / "goofish_device.json"
                    device_fp.parent.mkdir(parents=True, exist_ok=True)
                    # 從 deviceId 抽出 UUID 部分(去掉 -userId suffix)
                    parts = browser_did.rsplit("-", 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        uuid_part = parts[0]
                    else:
                        uuid_part = browser_did  # fallback
                    device_fp.write_text(_json.dumps({
                        "device_uuid": uuid_part,
                        "full_device_id": browser_did,
                        "created_at": _time.time(),
                        "created_ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": "browser_capture",  # 標記來自瀏覽器
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    log(f"[采购登录] ✓ 瀏覽器 device_uuid 同步寫入: {uuid_part}")
                except Exception as e:
                    log(f"[采购登录] device_uuid 同步異常: {e}")

            # 寫入 access_token 緩存
            try:
                from core.xianyu_im_http import save_cached_access_token
                save_cached_access_token(
                    PURCHASE_PROFILE_DIR,
                    captured["access"],
                    captured.get("refresh", ""),
                    captured.get("exp_ms", 0) or 86400000,
                )
                log(f"[采购登录] ✓ WS access_token 已寫入緩存(24h 有效)")
                return True
            except Exception as e:
                log(f"[采购登录] 寫緩存異常: {e}")
                return False
        else:
            log("[采购登录] /im 訪問完仍未攔到 login.token,WS 將降級 HTTP 模式")
            return False
    except Exception as e:
        log(f"[采购登录] _navigate_to_im_for_token 異常: {e}")
        return False


def launch_login_browser_threaded(log: LogFn) -> None:
    """主线程入口：后台线程跑 Playwright 登录"""
    def _runner():
        try:
            asyncio.run(_async_open_browser(log))
        except Exception as e:
            log(f"[采购登录] 线程异常：{e}")
    threading.Thread(target=_runner, daemon=True).start()
