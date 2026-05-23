"""
闲鱼检测专用：Playwright 登录 + JSON cookie 持久化 + HTTP token 刷新

参考闲鱼采集 0329 的可靠方案：
- Playwright launch_persistent_context 启动 Chrome
- 登录成功后立即调用 browser.cookies() 主动导出全部 cookie 到 JSON
  （包括 Chrome session restore 不会保存的 session cookie：
   cookie2 / _tb_token_ / _samesite_flag_ / XSRF-TOKEN）
- 检测时直接从 JSON 读，不再依赖 Chrome SQLite + session restore
- token 过期纯 HTTP 调牺牲接口刷新，不需要重开浏览器

只服务于 check_goofish 检测路径，不影响 purchase_monitor。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

LogFn = Callable[[str], None]

# 检测专用 profile（Playwright persistent_context 格式）
BASE_DIR = Path(__file__).resolve().parent.parent
CHECK_PROFILE_DIR = BASE_DIR / "profiles" / "check_goofish"
COOKIE_JSON_PATH = CHECK_PROFILE_DIR / "goofish_cookies.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
TOKEN_REFRESH_API = "mtop.taobao.idle.item.web.recommend.list"
TOKEN_REFRESH_URL = f"https://h5api.m.goofish.com/h5/{TOKEN_REFRESH_API}/1.0/"

# 隐藏自动化痕迹（抄 0329）
RUNTIME_COMPAT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
try { delete navigator.__proto__.webdriver; } catch(e) {}
if (!window.chrome || !window.chrome.runtime) {
    window.chrome = {
        runtime: { onMessage: { addListener: function(){} }, onConnect: { addListener: function(){} } },
        loadTimes: function(){ return {}; },
        csi: function(){ return {}; },
        app: { isInstalled: false }
    };
}
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(parameters);
['callPhantom','_phantom','__nightmare','domAutomation','domAutomationController',
 '_Selenium_IDE_Recorder','_selenium','__webdriver_evaluate','__driver_evaluate'
].forEach(p => {
    try { Object.defineProperty(window, p, { get: () => undefined }); } catch(e) {}
});
"""


# ────────────────────────────── JSON 读写 ──────────────────────────────

def cookie_json_exists() -> bool:
    return COOKIE_JSON_PATH.exists()


def load_cookies_from_json() -> List[dict]:
    """读 cookies.json，返回 Playwright 格式 cookie list"""
    if not COOKIE_JSON_PATH.exists():
        return []
    try:
        with open(COOKIE_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "cookies" in data:
            return data["cookies"]
    except Exception:
        pass
    return []


_PUNISH_COOKIE_NAMES = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}


def _is_punish_cookie(c: dict) -> bool:
    """判断是否是闲鱼限流标记 cookie（保存到 JSON 前要过滤掉，
    否则下次登录带着历史 punish 标记会立刻触发限流）"""
    name = c.get("name", "")
    path = c.get("path", "") or ""
    if name in _PUNISH_COOKIE_NAMES:
        return True
    if "_____tmd_____" in path or "punish" in path.lower():
        return True
    return False


def save_cookies_to_json(cookies: List[dict]) -> None:
    """保存 cookies 到 JSON 文件，过滤掉 punish 标记 cookie"""
    COOKIE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _filtered = [c for c in cookies if not _is_punish_cookie(c)]
    _skipped = len(cookies) - len(_filtered)
    with open(COOKIE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(_filtered, f, ensure_ascii=False, indent=2)
    if _skipped:
        # 用 print 是因为这里调用点可能没 log_fn，让用户在控制台看得到
        try:
            print(f"[闲鱼登录] 已过滤 {_skipped} 条 punish 标记 cookie")
        except Exception:
            pass


def get_m_h5_tk(cookies: List[dict]) -> str:
    for c in cookies:
        if c.get("name") == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
            return c.get("value", "")
    return ""


def is_logged_in(cookies: List[dict]) -> bool:
    """unb 是登录后写入的持久 cookie，作为登录态判断依据"""
    for c in cookies:
        if c.get("name") == "unb" and c.get("value"):
            return True
    return False


# ────────────────────────────── HTTP token 刷新 ──────────────────────────────

def refresh_token_http(log: Optional[LogFn] = None) -> bool:
    """通过 HTTP POST 牺牲接口刷新 _m_h5_tk，刷新成功后写回 JSON。

    Returns: True 刷新成功，False 失败（需要重新登录）
    """
    _log = log or (lambda m: None)
    cookies = load_cookies_from_json()
    if not cookies:
        _log("[闲鱼登录] cookies.json 不存在，无法刷新 token")
        return False

    try:
        from curl_cffi.requests import Session
    except Exception as e:
        _log(f"[闲鱼登录] curl_cffi 不可用: {e}")
        return False

    session = Session(impersonate="chrome142")
    for c in cookies:
        try:
            session.cookies.set(
                c.get("name", ""), c.get("value", ""),
                domain=c.get("domain", ".goofish.com"),
                path=c.get("path", "/"),
            )
        except Exception:
            pass

    payload = json.dumps(
        {"itemId": "0", "pageSize": 1, "pageNum": 1},
        separators=(",", ":"), ensure_ascii=False,
    )
    t = str(int(time.time() * 1000))
    sign = hashlib.md5(f"&{t}&{APP_KEY}&{payload}".encode()).hexdigest()
    params = {
        "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign,
        "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000",
        "AntiCreep": "true", "AntiFlool": "true", "api": TOKEN_REFRESH_API,
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.goofish.com/",
        "Origin": "https://www.goofish.com",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        resp = session.post(
            TOKEN_REFRESH_URL, params=params, data={"data": payload},
            headers=headers, timeout=15,
        )
    except Exception as e:
        _log(f"[闲鱼登录] token 刷新请求失败: {e}")
        return False

    set_cookie = resp.headers.get("set-cookie", "")
    m_tk = re.search(r'_m_h5_tk=([^;]+)', set_cookie)
    m_enc = re.search(r'_m_h5_tk_enc=([^;]+)', set_cookie)
    new_tk = m_tk.group(1) if m_tk else session.cookies.get("_m_h5_tk")
    new_enc = m_enc.group(1) if m_enc else session.cookies.get("_m_h5_tk_enc")

    if not new_tk or "_" not in new_tk:
        _log(f"[闲鱼登录] token 刷新无新值, status={resp.status_code}")
        return False

    # 写回 JSON
    updated = False
    for c in cookies:
        name = c.get("name", "")
        if name == "_m_h5_tk" and ".goofish.com" in c.get("domain", ""):
            c["value"] = new_tk
            updated = True
        elif new_enc and name == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
            c["value"] = new_enc
    if not updated:
        # 旧 JSON 没有这两条，追加
        cookies.append({"name": "_m_h5_tk", "value": new_tk, "domain": ".goofish.com", "path": "/"})
        if new_enc:
            cookies.append({"name": "_m_h5_tk_enc", "value": new_enc, "domain": ".goofish.com", "path": "/"})

    save_cookies_to_json(cookies)
    _log(f"[闲鱼登录] token 刷新成功: {new_tk.split('_')[0][:12]}...")
    return True


# 商品浏览样本（让 BX 限流建立「真实用户」fingerprint）
# 用通用的「我的闲鱼」+ 几个真实可访问的商品 ID
_BROWSE_SAMPLE_IDS = [
    "990000000001",
    "990000000002",
    "990000000003",
]


async def _auto_click_quick_login(page, log: LogFn) -> bool:
    """检测并自动点击「快速进入」按钮（goofish 记住登录的快捷入口）。

    当浏览器有上次的登录痕迹但 session 失效时，goofish 会弹这个对话框。
    用户必须点击它才会真正激活 session 并下发完整的登录 cookie。
    若不点击，cookie 是半登录态，BX 限流会把所有 API 请求判定为可疑流量。
    """
    import asyncio
    # 等对话框出现（最多 5 秒）
    for _ in range(10):
        try:
            has_dialog = await page.evaluate("""() => {
                const t = document.body ? document.body.innerText : '';
                return t.includes('快速进入') || t.includes('其他账号登录');
            }""")
        except Exception:
            has_dialog = False
        if has_dialog:
            break
        await asyncio.sleep(0.5)
    else:
        return False  # 没弹对话框，可能本来就完全登录了

    log("[闲鱼登录] 检测到「快速进入」对话框，自动点击激活登录态")
    # 找到并点击「快速进入」按钮
    try:
        clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button, div, span, a');
            for (const b of btns) {
                const txt = (b.innerText || b.textContent || '').trim();
                if (txt === '快速进入' || txt === '快速進入') {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
    except Exception as e:
        log(f"[闲鱼登录] 点击「快速进入」失败（忽略）: {e}")
        clicked = False

    if not clicked:
        # 兜底：用 Playwright 的文本定位再试
        try:
            await page.click("text=快速进入", timeout=3000)
            clicked = True
        except Exception:
            try:
                await page.click("text=快速進入", timeout=3000)
                clicked = True
            except Exception:
                pass

    if clicked:
        log("[闲鱼登录] ✓ 已点击「快速进入」，等待登录态激活...")
        await asyncio.sleep(3)
        # 跳一次首页让新 cookie 落地
        try:
            await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        except Exception:
            pass
        return True
    else:
        log("[闲鱼登录] ⚠ 未能自动点击「快速进入」，请手动点击")
        return False


async def _enrich_cookies_via_browse(page, log: LogFn) -> None:
    """登录成功后让浏览器在 JS 上下文里实际调用 awesome.detail.unit 端点几次。

    关键原理：BX 的 x5sec cookie 是 **端点绑定** 的——对 endpoint A 有效的 x5sec
    不能用在 endpoint B 上。所以仅仅 goto 商品页（页面 JS 调用的可能是别的 mtop 端点）
    无法换来对 awesome.detail.unit 的信任。
    必须用 page.evaluate 在 JS 上下文里直接 fetch awesome.detail.unit，
    让 BX SDK（已在页面里加载）拦截响应、自动解限流验证，最终颁发对这个端点有效的 x5sec。

    若出现滑块验证，会等用户手动拖完（每条最多等 60 秒）。
    """
    import asyncio
    import hashlib as _hl
    import json as _json
    import time as _time

    APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
    DETAIL_API = "mtop.taobao.idle.awesome.detail.unit"

    log("[闲鱼登录] 开始浏览商品 + JS fetch 让 BX 信任 awesome.detail.unit 端点...")
    log("[闲鱼登录] ⚠ 如出现滑块验证，请手动拖动完成（每条最多等 60 秒）")

    # ── 第一步：先访问 goofish.com 让 BX SDK 在页面加载 ──
    try:
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
    except Exception as e:
        log(f"[闲鱼登录] 打开首页异常（忽略）: {e}")

    # ── 第二步：访问 3 个商品页（让 BX SDK 累积 fingerprint 信号）──
    for idx, iid in enumerate(_BROWSE_SAMPLE_IDS, 1):
        try:
            url = f"https://www.goofish.com/item?id={iid}"
            log(f"[闲鱼登录] 浏览第 {idx}/3 条：{iid}")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            # 检测滑块验证
            for wait_round in range(60):
                try:
                    has_captcha = await page.evaluate("""() => {
                        const t = document.body ? document.body.innerText : '';
                        if (t.includes('请拖动') || t.includes('滑块') || t.includes('完成验证') || t.includes('拖动下方')) return true;
                        if (document.querySelector('iframe[src*="punish"]')) return true;
                        if (document.querySelector('iframe[src*="captcha"]')) return true;
                        if (document.querySelector('.nc_wrapper, .nc-container, #nc_1_wrapper')) return true;
                        return false;
                    }""")
                except Exception:
                    has_captcha = False
                if not has_captcha:
                    break
                if wait_round == 0:
                    log(f"[闲鱼登录] ⚠ 检测到滑块验证！请手动拖动滑块（最多等 60 秒）")
                elif wait_round % 10 == 0:
                    log(f"[闲鱼登录] 仍在等待验证完成... ({wait_round}s)")
                await asyncio.sleep(1)
            await asyncio.sleep(1)
            try:
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.6)
            except Exception:
                pass
        except Exception as e:
            log(f"[闲鱼登录] 浏览 {iid} 异常（忽略）: {e}")

    # ── 第三步：在 JS 上下文里 fetch awesome.detail.unit，让 BX 解挑战 + 颁发端点 x5sec ──
    log("[闲鱼登录] 在浏览器 JS 里调用 awesome.detail.unit 让 BX 信任此端点...")
    # 拿浏览器里的 _m_h5_tk
    try:
        h5tk = await page.evaluate("""() => {
            const m = document.cookie.match(/_m_h5_tk=([^;]+)/);
            return m ? m[1] : '';
        }""")
    except Exception:
        h5tk = ""
    if not h5tk or "_" not in h5tk:
        log("[闲鱼登录] ⚠ 浏览器内 _m_h5_tk 为空，跳过 JS fetch（cookie 仍可能有效）")
        return
    token = h5tk.split("_")[0]
    log(f"[闲鱼登录] 用浏览器 token={token[:12]}... 发起 JS fetch")

    # 在 JS 里循环调用 awesome.detail.unit，最多等 BX 自动解 3 轮
    for round_idx in range(3):
        success_count = 0
        for iid in _BROWSE_SAMPLE_IDS:
            data_obj = {
                "commerceAdPlanId": "",
                "extra": '{"labelIds":"36,35,9,12"}',
                "fishAdCode": "440902",
                "flowVersion": "6.0",
                "gps": "0,0",
                "isOld": False,
                "itemId": str(iid),
                "latitude": "",
                "longitude": "",
                "needSimpleDetail": False,
            }
            data_str = _json.dumps(data_obj, separators=(",", ":"))
            t = str(int(_time.time() * 1000))
            sign = _hl.md5(f"{token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()
            qs = (
                f"jsv=2.7.2&appKey={APP_KEY}&t={t}&sign={sign}&v=1.0"
                f"&type=originaljson&accountSite=xianyu&dataType=json&timeout=20000"
                f"&AntiCreep=true&AntiFlool=true&api={DETAIL_API}"
                f"&spm_cnt=a21ybx.item.0.0&spm_pre=widle.12011849.0.0&sessionOption=AutoLoginOnly"
            )
            try:
                # JS fetch — BX SDK 在页面里 hook 了 fetch/XHR，会自动解挑战
                ret = await page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('https://h5api.m.goofish.com/h5/{DETAIL_API}/1.0/?{qs}', {{
                            method: 'POST',
                            credentials: 'include',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                                'Origin': 'https://www.goofish.com',
                            }},
                            body: 'data=' + encodeURIComponent({_json.dumps(data_str)}),
                        }});
                        const txt = await r.text();
                        return {{ok: txt.indexOf('SUCCESS') >= 0, status: r.status, snippet: txt.substring(0, 80)}};
                    }} catch(e) {{ return {{error: String(e)}}; }}
                }}""")
                if isinstance(ret, dict) and ret.get("ok"):
                    success_count += 1
            except Exception:
                pass
            await asyncio.sleep(0.4)  # 给 BX SDK 时间
        log(f"[闲鱼登录] JS fetch 第 {round_idx + 1}/3 轮：{success_count}/{len(_BROWSE_SAMPLE_IDS)} 成功")
        if success_count == len(_BROWSE_SAMPLE_IDS):
            log("[闲鱼登录] ✓ awesome.detail.unit 端点已被 BX 完全信任")
            break
        # 等 BX SDK 解限流验证
        await asyncio.sleep(3)

    # ── 第四步：让 BX 也信任 mtop.idle.web.trade.order.detail（采购监控用的端点！）──
    # 重要：BX x5sec 是端点绑定的。awesome.detail.unit 信任不能用在 order.detail。
    # 不做这步，cookie 即使保存了，监控调 order.detail 时仍被 BX 拒绝 → session_expired。
    ORDER_API = "mtop.idle.web.trade.order.detail"
    log("[闲鱼登录] 让 BX 信任 order.detail 端点（采购监控用的）...")
    for round_idx in range(3):
        passed_count = 0
        for tid in _BROWSE_SAMPLE_IDS:
            data_obj = {"tid": str(tid)}
            data_str = _json.dumps(data_obj, separators=(",", ":"))
            t = str(int(_time.time() * 1000))
            sign = _hl.md5(f"{token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()
            qs = (
                f"jsv=2.7.2&appKey={APP_KEY}&t={t}&sign={sign}&v=1.0"
                f"&type=originaljson&accountSite=xianyu&dataType=json&timeout=20000"
                f"&AntiCreep=true&AntiFlool=true&api={ORDER_API}"
            )
            try:
                ret = await page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('https://h5api.m.goofish.com/h5/{ORDER_API}/1.0/?{qs}', {{
                            method: 'POST',
                            credentials: 'include',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                                'Origin': 'https://www.goofish.com',
                                'Referer': 'https://www.goofish.com/order-detail?orderId={tid}',
                            }},
                            body: 'data=' + encodeURIComponent({_json.dumps(data_str)}),
                        }});
                        const txt = await r.text();
                        // tid 不是真订单会返回业务 error（"非本人订单"等），但只要不含 punish/validate 字眼
                        // 就说明 BX 已信任该端点（业务错误不影响 BX 信任）
                        const blockedByBx = txt.includes('FAIL_SYS_USER_VALIDATE') || txt.includes('punish') || txt.includes('TOKEN_EMPTY');
                        return {{passedBx: !blockedByBx && txt.length > 0, snippet: txt.substring(0, 100)}};
                    }} catch(e) {{ return {{error: String(e)}}; }}
                }}""")
                if isinstance(ret, dict) and ret.get("passedBx"):
                    passed_count += 1
            except Exception:
                pass
            await asyncio.sleep(0.4)
        log(f"[闲鱼登录] order.detail 第 {round_idx + 1}/3 轮：{passed_count}/{len(_BROWSE_SAMPLE_IDS)} 通过 BX")
        if passed_count == len(_BROWSE_SAMPLE_IDS):
            log("[闲鱼登录] ✓ order.detail 端点已被 BX 信任")
            break
        await asyncio.sleep(3)

    # ── 第五步:養 /im 端點 x5sec(WS 客服 fetch_session_id_via_playwright_lite 用) ──
    # 不做這步,客服第一次「問賣家」訪問 /im 會彈滑塊
    log("[闲鱼登录] 让 BX 信任 /im 端点(WS 客服用)...")
    try:
        await page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        # 偵測滑塊(用戶手動拖)
        for wait_round in range(60):
            try:
                has_captcha = await page.evaluate("""() => {
                    const t = document.body ? document.body.innerText : '';
                    if (t.includes('请拖动') || t.includes('滑块') || t.includes('完成验证') || t.includes('拖动下方')) return true;
                    if (document.querySelector('iframe[src*="punish"]')) return true;
                    if (document.querySelector('iframe[src*="captcha"]')) return true;
                    if (document.querySelector('.nc_wrapper, .nc-container, #nc_1_wrapper, .baxia-dialog')) return true;
                    return false;
                }""")
            except Exception:
                has_captcha = False
            if not has_captcha:
                break
            if wait_round == 0:
                log("[闲鱼登录] ⚠ /im 端点彈滑塊!請手動拖滑塊(60秒內)")
            elif wait_round % 10 == 0:
                log(f"[闲鱼登录] 仍在等 /im 滑塊驗證... ({wait_round}s)")
            await asyncio.sleep(1)
        # 拖過後等 2 秒讓 BX 寫入 x5sec
        await asyncio.sleep(2)
        log("[闲鱼登录] ✓ /im 端点已被 BX 信任")
    except Exception as e:
        log(f"[闲鱼登录] /im 端点養護異常(繼續): {e}")

    log("[闲鱼登录] ✓ BX 信任建立完成(awesome.detail.unit + order.detail + /im)")


# ────────────────────────────── Playwright 登录 ──────────────────────────────

async def _async_launch_login(log: LogFn) -> bool:
    """异步：启动 Playwright 浏览器供用户扫码登录，关闭浏览器时导出 cookie"""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log(f"[闲鱼登录] Playwright 未安装: {e}")
        return False

    CHECK_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    browser = None
    saved_count = 0
    try:
        browser = await pw.chromium.launch_persistent_context(
            str(CHECK_PROFILE_DIR),
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
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script(RUNTIME_COMPAT_JS)

        log("[闲鱼登录] 打开 goofish.com，请扫码登录...")
        try:
            await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"[闲鱼登录] 打开首页失败（继续等待）: {e}")

        # 关键：检测到「快速进入」对话框时自动点击，激活登录态
        # 若不点击，cookie 是半登录态，BX 限流会判定「未完全登录用户」→ 弹滑块
        await _auto_click_quick_login(page, log)

        # 检查是否已登录
        try:
            already = await page.evaluate(
                "() => document.cookie.includes('unb=') || document.cookie.includes('sid=')"
            )
        except Exception:
            already = False

        if already:
            log("[闲鱼登录] 检测到已登录状态")
            await asyncio.sleep(2)
            await _enrich_cookies_via_browse(page, log)
            cookies = await browser.cookies()
            save_cookies_to_json(cookies)
            saved_count = len(cookies)
            log(f"[闲鱼登录] ✓ 已保存 {saved_count} 条 cookie 到 goofish_cookies.json")
            log("[闲鱼登录] 自动关闭浏览器")
            return True

        # 等待扫码登录（最多 10 分钟），登录成功立即保存 cookie 并自动关闭
        log("[闲鱼登录] 等待扫码登录...")
        for attempt in range(600):
            try:
                logged = await page.evaluate(
                    "() => document.cookie.includes('unb=') || document.cookie.includes('sid=')"
                )
            except Exception:
                break
            if logged:
                log(f"[闲鱼登录] ✓ 登录成功（耗时 {attempt+1}s）")
                await asyncio.sleep(2)
                # 关键：登录后必须浏览几个商品让 BX 限流 fingerprint，否则后续 API 必被 bxpunish
                await _enrich_cookies_via_browse(page, log)
                cookies = await browser.cookies()
                save_cookies_to_json(cookies)
                saved_count = len(cookies)
                log(f"[闲鱼登录] ✓ 已保存 {saved_count} 条 cookie 到 goofish_cookies.json")
                log("[闲鱼登录] 自动关闭浏览器")
                return True
            await asyncio.sleep(1)
            if attempt % 30 == 29:
                log(f"[闲鱼登录] 等待扫码中... ({attempt+1}s)")

        log("[闲鱼登录] 等待登录超时（10 分钟），未保存 cookie")

    except Exception as e:
        log(f"[闲鱼登录] 异常: {e}")
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

    return saved_count > 0


def launch_login_browser_threaded(log: LogFn) -> None:
    """从 tkinter 线程调用：起一个独立线程跑 asyncio Playwright 登录流程。
    立即返回，不阻塞 UI。
    """
    import threading

    def _runner():
        try:
            asyncio.run(_async_launch_login(log))
        except Exception as e:
            log(f"[闲鱼登录] 启动失败: {e}")

    threading.Thread(target=_runner, daemon=True).start()


# ────────────────────────────── 清除 ──────────────────────────────

def clear_all(log: Optional[LogFn] = None) -> None:
    """彻底清除检测专用 cookie：JSON + Playwright profile 目录"""
    import shutil
    _log = log or (lambda m: None)
    cleared = []

    if COOKIE_JSON_PATH.exists():
        try:
            COOKIE_JSON_PATH.unlink()
            cleared.append("goofish_cookies.json")
        except Exception as e:
            _log(f"[闲鱼登录] 删 JSON 失败: {e}")

    if CHECK_PROFILE_DIR.exists():
        # 彻底删整个 profile 目录
        try:
            shutil.rmtree(CHECK_PROFILE_DIR)
            cleared.append("check_goofish/ (整个 profile 目录)")
        except Exception as e:
            _log(f"[闲鱼登录] 删 profile 目录失败: {e}")

    if cleared:
        _log(f"[闲鱼登录] ✓ 已清除: {', '.join(cleared)}")
    else:
        _log("[闲鱼登录] 没有需要清除的数据")
