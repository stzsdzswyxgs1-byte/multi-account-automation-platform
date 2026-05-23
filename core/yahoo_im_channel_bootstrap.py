"""Yahoo IM channel bootstrap — 對「全新買家」初始化 channel.

⭐ 實機 deep-trace 結論(2026-05-20):
Yahoo Juiker IM 對 channel 創建有 session 隔離.同一個 Y-ID 的兩個 session
(用戶日常 Chrome vs 我們 Python BOSH)互相看不到對方建的 channel.

唯一可靠方案: **用軟件 profile dir 直接 launch Chrome subprocess** → 用戶在這個 Chrome
內 click 即時通 button + 發第一句 → channel 在「我們 profile session」內建立 →
之後 Python BOSH 用同 profile 的 cookies 就能 channel_user_active rc=0 → send 正常 work.

關鍵函數:
- `open_profile_chrome_first_contact()` — ⭐ 主路徑,subprocess Chrome 開 chat URL
- `bootstrap_via_http()` — 純 HTTP(對 NEW channel 證實不會建)
- `bootstrap_channel_via_order_page()` — Playwright(對 NEW channel 也不行,Yahoo 拒絕)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


def _kill_chrome_with_profile(profile_dir_name: str, on_log: LogFn) -> int:
    """Kill all chrome.exe processes whose CommandLine contains the profile dir name.

    Returns count of killed processes.
    """
    try:
        import subprocess
        ps_cmd = (
            f"Get-WmiObject Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ ($_.CommandLine -ne $null) "
            f"-and ($_.CommandLine.Contains('{profile_dir_name}')) }} | "
            f"ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "
            f"Write-Output $_.ProcessId }} catch {{}} }}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        killed = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        if killed:
            on_log(f"[CH-BOOT] killed {len(killed)} chrome procs for profile: {killed}")
        return len(killed)
    except Exception as e:
        on_log(f"[CH-BOOT] kill chrome 異常: {e}")
        return 0


def _find_free_port(start: int = 9222, tries: int = 20) -> int:
    """Find an available TCP port for Chrome remote debugging."""
    import socket
    for offset in range(tries):
        port = start + offset
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("no free port for chrome debug")


def open_profile_chrome_first_contact(
    profile_dir: Path,
    buyer_y_id: str,
    order_id: str,
    *,
    on_log: Optional[LogFn] = None,
    auto_send_text: str = "1",
    wait_after_send_sec: float = 20.0,  # 預設 20s,慢網路最多 poll 此時間
) -> Tuple[bool, str]:
    """⭐ 全自動 first-contact:headless Chrome + CDP click(用軟件 profile).

    實機驗證關鍵(2026-05-20):
    - 舊 --headless → Yahoo 偵測 automation 拒絕
    - off-screen visible Chrome(--window-position=-32000)→ 沒 focus,Yahoo 拒絕
    - **--headless=new(Chrome 113+ modern headless)→ ✅ Yahoo SDK 接受,完全不開窗**

    所以這個函數:
    1. Launch Chrome subprocess **--headless=new**(完全背景跑,不影響用戶桌面)
    2. CDP `connect_over_cdp` 接管控制
    3. 自動 click 即時通 button → navigate /chat URL
    4. 自動 fill textarea + click button[class*=sendBtn] 紙飛機
    5. Poll verify channel(每 2s 檢查,慢網路最多等 wait_after_send_sec 秒)
    6. 關閉 Chrome

    用戶**完全不會看到任何視窗**,後台靜默執行.慢網路自動 retry verify.

    Args:
        profile_dir: 軟件 profile 目錄
        buyer_y_id: 買家 Y-ID
        order_id: 訂單號
        auto_send_text: 自動 send 的文字(實際 dispatch 用 reply_text 真實用戶輸入)
        wait_after_send_sec: poll verify channel 最大等待時間 (預設 20s)

    Returns:
        (True, info) 表 channel 真實已建立; (False, info) 表創建失敗.
    """
    on_log = on_log or (lambda *_: None)
    profile_dir = Path(profile_dir).resolve()
    if not profile_dir.exists():
        return False, f"profile_dir 不存在: {profile_dir}"

    buyer = buyer_y_id.upper()
    if not buyer.startswith("Y"):
        buyer = "Y" + buyer.lstrip("y")
    if not order_id:
        return False, "order_id 空"

    try:
        from .accounts import load_settings
        settings = load_settings()
        exe = (settings.get("browser_path") or "").strip()
    except Exception as e:
        return False, f"load_settings 失敗: {e}"
    if not exe or not Path(exe).exists():
        return False, f"browser_path 未配置: {exe!r}"

    import time as _t
    import subprocess
    import urllib.request

    # 先清掉這個 profile 的既有 Chrome process(避免衝突)
    _kill_chrome_with_profile(profile_dir.name, on_log)
    _t.sleep(0.5)

    # 找 free port for CDP
    try:
        debug_port = _find_free_port(9300, tries=30)
    except Exception as e:
        return False, f"找不到 free port: {e}"

    # ⭐ Launch Chrome **--headless=new**(Chrome 113+ 新 headless,Yahoo SDK 不偵測)
    # 不影響用戶桌面,完全背景跑(舊 --headless 會被 Yahoo 拒絕)
    target_url = f"https://tw.bid.yahoo.com/partner/order/detail?orderId={order_id}"
    args = [
        exe,
        f"--user-data-dir={str(profile_dir)}",
        f"--remote-debugging-port={debug_port}",
        "--remote-allow-origins=*",
        "--headless=new",                      # ⭐ 新 headless 模式,不開窗
        "--window-size=1280,800",              # virtual viewport
        "--no-first-run",
        "--no-default-browser-check",
        target_url,
    ]
    on_log(
        f"[CH-BOOT-CDP] launch headless Chrome port={debug_port} "
        f"order={order_id[-6:]}"
    )

    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            args,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        return False, f"launch Chrome 失敗: {e}"

    # 等 CDP port ready(慢網路最多 40s)
    cdp_ready = False
    for _attempt in range(40):
        _t.sleep(1.0)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=2) as r:
                if r.status == 200:
                    cdp_ready = True
                    break
        except Exception:
            continue

    if not cdp_ready:
        on_log("[CH-BOOT-CDP] CDP 沒 ready,放棄")
        _kill_chrome_with_profile(profile_dir.name, on_log)
        return False, f"CDP port {debug_port} 沒 ready"

    on_log("[CH-BOOT-CDP] CDP ready, Playwright connect")

    # Playwright connect_over_cdp
    try:
        from .client_runtime_compat import sync_playwright
    except Exception:
        from playwright.sync_api import sync_playwright  # type: ignore

    p = None
    browser = None
    try:
        p = sync_playwright().start()
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
        ctx = browser.contexts[0]
        page = ctx.pages[0]

        # 等訂單頁載入(慢網路 — 給更長 timeout)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        _t.sleep(2.0)
        on_log(f"[CH-BOOT-CDP] order page loaded")

        # 自動接受 TOS popup(如果有)
        try:
            tos = page.evaluate("""() => {
                const dialogs = document.querySelectorAll('[role="dialog"]');
                for (const d of dialogs) {
                    if ((d.textContent||'').includes('使用條款') || (d.textContent||'').includes('PAYUNi')) {
                        d.querySelectorAll('*').forEach(s => {
                            if (s.scrollHeight > s.clientHeight) s.scrollTop = s.scrollHeight;
                        });
                        return true;
                    }
                }
                return false;
            }""")
            if tos:
                on_log("[CH-BOOT-CDP] TOS popup → 自動滑底 + click 我同意")
                _t.sleep(1.5)
                try:
                    btn = page.get_by_role("button", name="我同意")
                    if btn.count() > 0:
                        btn.first.click()
                        _t.sleep(2.0)
                except Exception:
                    pass
        except Exception:
            pass

        # Click 即時通 button(從訂單頁進到 chat 頁,觸發 Yahoo wrapper SDK init 跟 order context)
        im_locators = page.locator('a[href*="/chat/Y"]')
        if im_locators.count() == 0:
            im_locators = page.locator('a[href*="/chat/y"]')
        if im_locators.count() == 0:
            return False, "找不到 即時通 button"
        on_log(f"[CH-BOOT-CDP] click 即時通 button")

        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                im_locators.first.click()
        except Exception as _e_n:
            on_log(f"[CH-BOOT-CDP] navigation 異常: {_e_n}")

        cur_url = page.url
        if "/chat/" not in cur_url.lower():
            return False, f"click 後 URL 沒進 /chat: {cur_url}"
        on_log(f"[CH-BOOT-CDP] now on chat URL")

        # 等 SDK 完整 init(BOSH connect + 全部 HTTP setup calls)
        _t.sleep(4.0)

        # ⭐ 找 chat composer 的真實 send button
        # Chrome MCP 實機抓到:button[class*='sendBtn'] (class='sendBtn__XXXXX' Yahoo 用 CSS modules)
        # 在 textarea 右邊 (x=1217, y=764, w=72)
        try:
            # 等 textarea + send button 真實 render(慢網路最多 30s)
            ta_btn_ready = False
            for _i in range(30):
                ta_ready = page.evaluate(
                    "!!document.querySelector('textarea[placeholder*=\"給對方\"]')"
                )
                btn_ready = page.evaluate(
                    "!!document.querySelector('button[class*=\"sendBtn\"]')"
                )
                if ta_ready and btn_ready:
                    ta_btn_ready = True
                    on_log(f"[CH-BOOT-CDP] textarea + sendBtn ready @ {_i}s")
                    break
                _t.sleep(1.0)
            if not ta_btn_ready:
                return False, "textarea or sendBtn 30s 內沒 render (網路太慢?)"

            # Fill textarea
            ta = page.locator('textarea[placeholder*="給對方"]')
            ta.click(timeout=5000)
            ta.fill(auto_send_text)
            _t.sleep(0.5)
            on_log(f"[CH-BOOT-CDP] filled textarea with {auto_send_text!r}")

            # 拿 send button 的真實 bounding box
            send_bbox = page.evaluate(r"""() => {
                const btn = document.querySelector('button[class*="sendBtn"]');
                if (!btn) return null;
                const r = btn.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""")
            if not send_bbox or send_bbox["w"] < 5:
                return False, f"找不到 sendBtn class button or bbox 太小: {send_bbox}"
            on_log(
                f"[CH-BOOT-CDP] sendBtn bbox: "
                f"x={send_bbox['x']:.0f} y={send_bbox['y']:.0f} "
                f"w={send_bbox['w']:.0f} h={send_bbox['h']:.0f}"
            )

            # Click 中心點(用 mouse event,trusted)
            cx = send_bbox["x"] + send_bbox["w"] / 2
            cy = send_bbox["y"] + send_bbox["h"] / 2
            page.mouse.move(cx, cy, steps=5)
            _t.sleep(0.2)
            page.mouse.click(cx, cy, delay=100)
            on_log(f"[CH-BOOT-CDP] click sendBtn at ({cx:.0f},{cy:.0f}) — message sent!")
        except Exception as _e_s:
            on_log(f"[CH-BOOT-CDP] send 異常: {_e_s}")
            return False, f"send 失敗: {_e_s}"

        # ⭐ Poll verify(網路慢適應):每 2s 檢查 channel,直到 rc=0 或 max_wait_sec timeout
        # max_wait 等同於 wait_after_send_sec(由 caller 傳入,通常 8-30s)
        try:
            from .yahoo_im_bosh_ext import BOSHSession
            from .im_http_ops import build_channel_id
            from .yahoo_im_jwt import get_plain_jwt
            _, my_user, _ = get_plain_jwt(profile_dir, on_log=lambda m: None)
            chid_fwd = build_channel_id(my_user, buyer) if my_user else ""
            if not chid_fwd:
                return False, "拿不到 my_user 構不出 chID"

            _parts = chid_fwd.split(":")
            chid_rev = (
                f"{_parts[0]}:{_parts[2]}:{_parts[1]}"
                if len(_parts) == 3 else ""
            )

            max_wait = max(wait_after_send_sec, 5.0)
            poll_interval = 2.0
            elapsed = 0.0
            on_log(
                f"[CH-BOOT-CDP] poll verify channel"
                f"(max {max_wait}s, every {poll_interval}s)"
            )
            while elapsed < max_wait:
                _t.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    with BOSHSession(profile_dir, on_log=lambda m: None) as s:
                        # try forward
                        resp_f, _ = s.channel_user_active(chid_fwd, is_active=True)
                        rc_f = resp_f.get("returnCode") if isinstance(resp_f, dict) else None
                        if rc_f == 0:
                            on_log(f"[CH-BOOT-CDP] @ {elapsed:.0f}s verify forward rc=0 ✓")
                            return True, "Channel auto-bootstrap OK (forward)"
                        # try reversed
                        if chid_rev:
                            resp_r, _ = s.channel_user_active(chid_rev, is_active=True)
                            rc_r = resp_r.get("returnCode") if isinstance(resp_r, dict) else None
                            if rc_r == 0:
                                on_log(f"[CH-BOOT-CDP] @ {elapsed:.0f}s verify reversed rc=0 ✓")
                                return True, "Channel auto-bootstrap OK (reversed)"
                        on_log(f"[CH-BOOT-CDP] poll @ {elapsed:.0f}s rc_f={rc_f} rc_r={rc_r if chid_rev else 'n/a'}")
                except Exception as _e_p:
                    on_log(f"[CH-BOOT-CDP] poll @ {elapsed:.0f}s 異常: {_e_p}")

            return False, f"poll {max_wait}s 後 channel 仍 rc=1106 (網路慢? 或 send fail?)"
        except Exception as e_v:
            on_log(f"[CH-BOOT-CDP] 驗證異常: {e_v}")
            return False, f"verify 異常: {e_v}"
    except Exception as e:
        on_log(f"[CH-BOOT-CDP] 整體異常: {e}")
        return False, f"CDP 流程異常: {e}"
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass
        try:
            _kill_chrome_with_profile(profile_dir.name, on_log)
        except Exception:
            pass


# 舊 CDP 自動化路徑(實機驗證 Yahoo 拒絕 automation-event)
# 留著供參考,但不再使用
def _DEPRECATED_open_profile_chrome_via_cdp(
    profile_dir: Path,
    buyer_y_id: str,
    order_id: str,
    *,
    on_log: Optional[LogFn] = None,
    auto_send_text: str = "1",
    wait_after_send_sec: float = 8.0,
) -> Tuple[bool, str]:
    """⚠️ DEPRECATED — automation 不能觸發 Yahoo channel 建立.

    保留代碼供未來探索,但不再從 dispatch_topic_reply 呼叫.
    """
    on_log = on_log or (lambda *_: None)
    profile_dir = Path(profile_dir).resolve()
    buyer = buyer_y_id.upper()
    if not buyer.startswith("Y"):
        buyer = "Y" + buyer.lstrip("y")

    try:
        from .accounts import load_settings
        settings = load_settings()
        exe = (settings.get("browser_path") or "").strip()
    except Exception as e:
        return False, f"load_settings 失敗: {e}"

    _kill_chrome_with_profile(profile_dir.name, on_log)
    import time as _t
    _t.sleep(1.0)

    try:
        debug_port = _find_free_port(9300, tries=30)
    except Exception as e:
        return False, f"找不到 free port: {e}"

    target_url = f"https://tw.bid.yahoo.com/partner/order/detail?orderId={order_id}"
    args = [
        exe,
        f"--user-data-dir={str(profile_dir)}",
        f"--remote-debugging-port={debug_port}",
        "--remote-allow-origins=*",
        "--window-position=-32000,-32000",
        "--window-size=1280,800",
        "--no-first-run",
        target_url,
    ]
    import subprocess
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            args,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        return False, f"launch Chrome 失敗: {e}"

    import urllib.request
    cdp_ready = False
    for _attempt in range(20):
        _t.sleep(1.0)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=2) as r:
                if r.status == 200:
                    cdp_ready = True
                    break
        except Exception:
            continue

    if not cdp_ready:
        _kill_chrome_with_profile(profile_dir.name, on_log)
        return False, f"CDP port {debug_port} 沒 ready"

    try:
        from .client_runtime_compat import sync_playwright
    except Exception:
        from playwright.sync_api import sync_playwright  # type: ignore

    p = None
    browser = None
    try:
        p = sync_playwright().start()
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
        contexts = browser.contexts
        if not contexts:
            return False, "no contexts"
        ctx = contexts[0]
        pages = ctx.pages
        if not pages:
            return False, "no pages"
        page = pages[0]

        # 等訂單頁載入
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        _t.sleep(2.0)

        on_log(f"[CH-BOOT-CDP] page loaded: {page.url[-80:]}")

        # ⭐ 處理 TOS popup(如果有)— 用戶授權 auto-accept(他們明確要求 headless 全自動)
        try:
            tos_handled = page.evaluate("""() => {
                // 滑到 modal 底,點 我同意
                const dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"]');
                for (const d of dialogs) {
                    const txt = (d.textContent || '');
                    if (txt.includes('使用條款') || txt.includes('PAYUNi')) {
                        // 找 scrollable container 滾到底
                        const scrolls = d.querySelectorAll('*');
                        scrolls.forEach(s => {
                            if (s.scrollHeight > s.clientHeight) {
                                s.scrollTop = s.scrollHeight;
                            }
                        });
                        return true;
                    }
                }
                return false;
            }""")
            if tos_handled:
                on_log("[CH-BOOT-CDP] 偵測到 TOS popup,滾到底...")
                _t.sleep(1.5)
                # click 我同意 button
                try:
                    agree_btn = page.get_by_role("button", name="我同意")
                    if agree_btn.count() > 0:
                        agree_btn.first.click()
                        on_log("[CH-BOOT-CDP] 已 click 我同意")
                        _t.sleep(2.0)
                except Exception as _e_a:
                    on_log(f"[CH-BOOT-CDP] click 我同意 異常: {_e_a}")
        except Exception as _e_tos:
            on_log(f"[CH-BOOT-CDP] TOS 處理異常(忽略): {_e_tos}")

        # 找 即時通 button
        try:
            im_locators = page.locator('a[href*="/chat/Y"]')
            count = im_locators.count()
            if count == 0:
                im_locators = page.locator('a[href*="/chat/y"]')
                count = im_locators.count()
            if count == 0:
                return False, "找不到 即時通 button"
            on_log(f"[CH-BOOT-CDP] found {count} 即時通 buttons → click first")

            # Click → navigate to /chat URL
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                im_locators.first.click()
        except Exception as e_click:
            on_log(f"[CH-BOOT-CDP] click 即時通 異常: {e_click}")
            return False, f"click 即時通 button 失敗: {e_click}"

        cur_url = page.url
        if "/chat/" not in cur_url.lower():
            return False, f"click 後 URL 沒進 /chat: {cur_url}"

        on_log(f"[CH-BOOT-CDP] 進入 chat 頁: {cur_url[-60:]}")

        # 等 SDK init 完整(BOSH connect + listChannels + setup)
        _t.sleep(5.0)

        # 找輸入框 + 真實 send button(airplane icon)
        try:
            input_box = page.get_by_placeholder("給對方的訊息...")
            input_box.click(timeout=5000)
            input_box.fill(auto_send_text)
            on_log(f"[CH-BOOT-CDP] 已輸入訊息: {auto_send_text!r}")
            _t.sleep(0.5)

            # 找 send button — airplane icon,通常是 input 框旁邊的 button
            # 嘗試多種定位策略
            send_clicked = False
            for selector in [
                # send button 通常 placeholder input 旁邊
                'button[aria-label="送出"]',
                'button[aria-label="Send"]',
                'button[title*="送出"]',
                'button[type="submit"]',
            ]:
                try:
                    btn = page.locator(selector)
                    if btn.count() > 0:
                        btn.first.click(timeout=3000)
                        on_log(f"[CH-BOOT-CDP] click send button via {selector}")
                        send_clicked = True
                        break
                except Exception:
                    continue
            if not send_clicked:
                # fallback: 找 input 後面的 button
                try:
                    sibling_btn = page.evaluate_handle("""() => {
                        const inp = document.querySelector('input[placeholder*="給對方的訊息"], textarea[placeholder*="給對方的訊息"]');
                        if (!inp) return null;
                        // 在同個 parent / sibling 內找 button
                        let p = inp.parentElement;
                        for (let depth = 0; depth < 4 && p; depth++) {
                            const btns = p.querySelectorAll('button');
                            for (const b of btns) {
                                // skip emoji / image / video buttons,挑最右邊的(通常 send)
                                if (b.textContent.trim() === '' && (b.querySelector('svg') || b.querySelector('img'))) {
                                    return b;
                                }
                            }
                            p = p.parentElement;
                        }
                        return null;
                    }""")
                    if sibling_btn:
                        sibling_btn.as_element().click(timeout=3000)
                        on_log("[CH-BOOT-CDP] click sibling send button")
                        send_clicked = True
                except Exception as _e_sb:
                    on_log(f"[CH-BOOT-CDP] sibling button 找不到: {_e_sb}")

            if not send_clicked:
                # 最後 fallback: press Enter
                page.keyboard.press("Enter")
                on_log("[CH-BOOT-CDP] fallback: press Enter")
        except Exception as e_send:
            on_log(f"[CH-BOOT-CDP] send 異常: {e_send}")
            return False, f"send 失敗: {e_send}"

        # 等 SDK 完成 channel 創建 + 訊息實際 deliver
        on_log(f"[CH-BOOT-CDP] 等 SDK 完成 channel 註冊 {wait_after_send_sec}s")
        _t.sleep(wait_after_send_sec)

        # 驗證 channel 已可被 Python BOSH access
        try:
            from .yahoo_im_bosh_ext import BOSHSession
            from .im_http_ops import build_channel_id
            from .yahoo_im_jwt import get_plain_jwt
            _, my_user, _ = get_plain_jwt(profile_dir, on_log=on_log)
            if my_user:
                chid = build_channel_id(my_user, buyer)
                with BOSHSession(profile_dir, on_log=lambda m: None) as s:
                    resp, _ = s.channel_user_active(chid, is_active=True)
                    rc = resp.get("returnCode") if isinstance(resp, dict) else None
                    on_log(f"[CH-BOOT-CDP] 驗證 BOSH channel_user_active rc={rc}")
                    if rc == 0:
                        return True, (
                            f"全自動 first-contact 完成 + 驗證通過 "
                            f"(channel {chid[-30:]} 可被軟件 access)"
                        )
                    return False, (
                        f"first-contact send 完成但 BOSH 仍 rc={rc} "
                        f"(SDK 可能還沒完成註冊,可試 manual)"
                    )
        except Exception as e_v:
            on_log(f"[CH-BOOT-CDP] 驗證異常: {e_v}")
            return True, f"first-contact 已嘗試(驗證異常: {e_v})"

        return True, "first-contact 已嘗試"
    except Exception as e:
        on_log(f"[CH-BOOT-CDP] 整體異常: {e}")
        return False, f"CDP 流程異常: {e}"
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass
        # 關閉 subprocess Chrome
        try:
            _kill_chrome_with_profile(profile_dir.name, on_log)
        except Exception:
            pass


def bootstrap_via_http(
    profile_dir: Path,
    buyer_y_id: str,
    order_id: str,
    *,
    on_log: Optional[LogFn] = None,
    timeout_sec: float = 10.0,
) -> Tuple[bool, str]:
    """純 HTTP bootstrap — 複製 Yahoo SDK 載入 /chat 頁時的完整 HTTP 呼叫序列.

    比 Playwright 快很多(~3s vs ~12s),且不佔用 Chrome profile.

    流程:
    1. 從 profile cookie cache 拉 cookies + wssid
    2. 依序送 Yahoo SDK 載入 /chat URL 時的 HTTP calls
    3. (parallel)開 BOSH session,fire channel_user_active 觸發 server 端建 channel
    4. 驗證 channel 是否 isSubscribed=true

    Returns:
        (success, info)
    """
    on_log = on_log or (lambda *_: None)
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return False, f"profile_dir 不存在: {profile_dir}"

    buyer = buyer_y_id.upper()
    if not buyer.startswith("Y"):
        buyer = "Y" + buyer.lstrip("y")
    if not order_id:
        return False, "order_id 空"

    from .im_http_ops import _build_session, _IM_API_BASE, build_channel_id

    session, wssid, err = _build_session(profile_dir)
    if not session:
        return False, f"session 不可用: {err}"

    # 拿 my_id(seller Y-ID)從 /fe/api/im/user
    my_id = ""
    try:
        r = session.get(f"{_IM_API_BASE}/user", timeout=timeout_sec)
        if r.status_code == 200:
            data = r.json()
            user = data.get("user") or {}
            my_id = user.get("id", "")
    except Exception as e:
        on_log(f"[CH-BOOT-HTTP] /user fail: {e}")
        return False, f"/user fail: {e}"

    if not my_id:
        return False, "拿不到 my_id"

    channel_id = build_channel_id(my_id, buyer)
    on_log(f"[CH-BOOT-HTTP] channel={channel_id}")

    session.headers["Referer"] = f"https://tw.bid.yahoo.com/chat/{buyer}"

    # 序列複製 SDK 呼叫
    on_log("[CH-BOOT-HTTP] sequence: orders → putLastAccessedTs → users → channelRules → channelSubscription")
    try:
        # 1. GET order info
        session.get(f"{_IM_API_BASE}/orders?orderId={order_id}", timeout=timeout_sec)
        # 2. PUT putLastAccessedTs(全局時間戳,SDK 載入時必呼叫)
        session.put(
            f"{_IM_API_BASE}/putLastAccessedTs?from=messageHome",
            json={"wssid": wssid}, timeout=timeout_sec,
        )
        # 3. GET buyer info
        session.get(f"{_IM_API_BASE}/users?userIds={buyer}", timeout=timeout_sec)
        # 4. GET imUserQnas
        session.get(f"{_IM_API_BASE}/imUserQnas?userId={buyer}", timeout=timeout_sec)
        # 5. GET relationship
        session.get(
            f"{_IM_API_BASE}/relationship?userId={buyer}&property=auction2",
            timeout=timeout_sec,
        )
        # 6. GET channelRules(關鍵:用 chID 觸發 server 端認知)
        session.get(
            f"{_IM_API_BASE}/getChannelRules?channelId={channel_id}&property=auction2",
            timeout=timeout_sec,
        )
        # 7. POST channelSubscription(可能是真正觸發訂閱建立的)
        session.post(
            f"{_IM_API_BASE}/channelSubscription?channelId={channel_id}",
            json={}, timeout=timeout_sec,
        )
        # 8. GET channelSubscription 確認
        r_sub = session.get(
            f"{_IM_API_BASE}/channelSubscription?channelId={channel_id}",
            timeout=timeout_sec,
        )
        is_sub = False
        if r_sub.status_code == 200:
            try:
                is_sub = bool(r_sub.json().get("isSubscribed"))
            except Exception:
                pass
        on_log(f"[CH-BOOT-HTTP] channelSubscription.isSubscribed={is_sub}")
    except Exception as e:
        return False, f"HTTP 序列失敗: {e}"

    # 開 BOSH session + channel_user_active(觸發 + 驗證)
    try:
        from .yahoo_im_bosh_ext import BOSHSession
        with BOSHSession(profile_dir, on_log=on_log) as s:
            resp, _ = s.channel_user_active(channel_id, is_active=True)
            rc = resp.get("returnCode") if isinstance(resp, dict) else None
            on_log(f"[CH-BOOT-HTTP] BOSH channel_user_active rc={rc}")
            if rc == 0:
                # 確認可拉 channel profile
                resp_p, _ = s.get_channel_profile(channel_id)
                rc_p = resp_p.get("returnCode") if isinstance(resp_p, dict) else None
                subject = resp_p.get("subject", "") if isinstance(resp_p, dict) else ""
                on_log(f"[CH-BOOT-HTTP] BOSH get_channel_profile rc={rc_p} subject={subject!r}")
                if rc_p == 0:
                    return True, f"HTTP bootstrap OK (subject={subject!r})"
            return False, f"channel_user_active rc={rc}(channel 未建立)"
    except Exception as e:
        return False, f"BOSH 驗證失敗: {e}"


def bootstrap_channel_via_order_page(
    profile_dir: Path,
    order_id: str,
    *,
    on_log: Optional[LogFn] = None,
    wait_after_click_sec: float = 10.0,
    timeout_ms: int = 25000,
) -> Tuple[bool, str]:
    """Playwright 自動點訂單頁 即時通 button 建 channel(慢路徑兜底).

    用真 profile launch_persistent_context.profile 被 Chrome 佔用會 fail,
    這時要 fallback 通知用戶手動點.

    Args:
        profile_dir: 賣家 Yahoo Chrome profile 目錄
        order_id: 訂單號
        wait_after_click_sec: button 點完等多久讓 SDK 完成 setup
        timeout_ms: page.goto / 元素等待 超時

    Returns:
        (success, info)
    """
    on_log = on_log or (lambda *_: None)
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return False, f"profile_dir 不存在: {profile_dir}"
    if not order_id:
        return False, "order_id 空"

    try:
        from .accounts import load_settings
        settings = load_settings()
        exe = (settings.get("browser_path") or "").strip()
    except Exception as e:
        return False, f"load_settings 失敗: {e}"
    if not exe or not Path(exe).exists():
        return False, f"browser_path 未配置或不存在: {exe!r}"

    from .profile_lock import acquire_or_clear, release
    lock_ok, lock_reason = acquire_or_clear(profile_dir, owner="ch_boot", log_fn=on_log)
    if not lock_ok:
        return False, f"profile lock 取不到: {lock_reason}"

    p = None
    ctx = None
    try:
        from .client_runtime_compat import (
            sync_playwright,
            apply_runtime_normalization_sync,
            get_launch_args,
            get_ignore_default_args,
        )

        p = sync_playwright().start()

        _kw = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,
            locale="zh-TW",
            accept_downloads=False,
            args=get_launch_args(
                headless=False,
                lang="zh-TW",
                extra=[
                    "--disable-features=TranslateUI",
                    "--window-position=-32000,-32000",
                    "--window-size=400,300",
                ],
            ),
            ignore_default_args=get_ignore_default_args(headless=False),
            executable_path=exe,
        )

        try:
            ctx = p.chromium.launch_persistent_context(**_kw)
        except TypeError:
            _kw.pop("no_viewport", None)
            _kw["viewport"] = {"width": 1280, "height": 800}
            ctx = p.chromium.launch_persistent_context(**_kw)

        try:
            apply_runtime_normalization_sync(ctx)
        except Exception:
            pass

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        order_url = f"https://tw.bid.yahoo.com/partner/order/detail?orderId={order_id}"
        on_log(f"[CH-BOOT-PW] goto order detail orderId={order_id[-6:]}")
        try:
            page.goto(order_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e_goto:
            return False, f"order page goto fail: {e_goto}"

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(1.5)

        # ⭐ TOS popup 偵測:Yahoo 拍賣 + PAYUNi 使用條款 modal 會擋住所有 button click
        # 我們的軟件 profile 跟用戶日常 Chrome 是獨立 cookie 庫,用戶在日常 Chrome 接受了
        # 不代表我們 profile 接受了 — 必須讓用戶在我們 profile 內手動接受
        try:
            tos_detected = page.evaluate("""() => {
                // 找 role=dialog 或 text 含「使用條款」「同意條款」的 modal
                const dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"]');
                for (const d of dialogs) {
                    const txt = (d.textContent || '').substring(0, 500);
                    if (txt.includes('使用條款') || txt.includes('PAYUNi') || txt.includes('我同意')) {
                        return { found: true, snippet: txt.substring(0, 80) };
                    }
                }
                // 也找頁面任何 button 有「我同意」text 而且看起來不可點
                const agreeBtns = Array.from(document.querySelectorAll('button')).filter(b =>
                    (b.textContent || '').trim() === '我同意'
                );
                if (agreeBtns.length > 0) return { found: true, snippet: '我同意 button visible' };
                return { found: false };
            }""")
            if tos_detected and tos_detected.get("found"):
                on_log(f"[CH-BOOT-PW] ⚠️ 偵測到 TOS popup 擋住頁面: {tos_detected.get('snippet','')[:80]}")
                return False, (
                    f"TOS popup 擋住 Playwright 操作.我們的軟件 profile(profiles/{profile_dir.name})"
                    f"獨立於用戶日常 Chrome,需要在軟件的登入瀏覽器內手動接受一次 Yahoo+PAYUNi 條款."
                )
        except Exception as _e_tos:
            on_log(f"[CH-BOOT-PW] TOS 偵測異常(忽略繼續): {_e_tos}")

        on_log("[CH-BOOT-PW] locate 即時通 button")
        im_link = None
        try:
            locators = page.locator('a[href*="/chat/Y"]')
            count = locators.count()
            if count == 0:
                locators = page.locator('a[href*="/chat/y"]')
                count = locators.count()
            if count == 0:
                return False, "找不到訂單頁的 即時通 button"
            im_link = locators.first
        except Exception as e_loc:
            return False, f"locate 即時通 button 失敗: {e_loc}"

        on_log("[CH-BOOT-PW] click 即時通 button")
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                im_link.click()
        except Exception as e_click:
            on_log(f"[CH-BOOT-PW] click+navigate 異常(忽略繼續等): {str(e_click)[:120]}")
            try:
                im_link.click()
            except Exception:
                pass

        cur_url = ""
        try:
            cur_url = page.url
        except Exception:
            pass
        on_log(f"[CH-BOOT-PW] post-click URL: {cur_url[:80]}")
        if "/chat/" not in cur_url.lower():
            return False, f"click 後 URL 沒進 /chat: {cur_url}"

        # 分多次短 wait + 中間驗證,而不是一次等死
        from .yahoo_im_bosh_ext import BOSHSession
        from .im_http_ops import build_channel_id
        from .yahoo_im_jwt import get_plain_jwt
        import re

        m = re.search(r"/chat/(Y\d+)", cur_url)
        buyer_y = m.group(1) if m else ""
        chid = ""
        if buyer_y:
            _, my_user, _ = get_plain_jwt(profile_dir, on_log=on_log)
            if my_user:
                chid = build_channel_id(my_user, buyer_y)

        # 不能 verify(沒 chid)→ 退回固定 wait
        if not chid:
            on_log(f"[CH-BOOT-PW] 沒 chid → fixed wait {wait_after_click_sec}s")
            time.sleep(wait_after_click_sec)
            return True, f"Playwright bootstrap (no verify) URL={cur_url[-60:]}"

        # ⭐ 真實驗證:每 3s 檢查一次 BOSH channel_user_active,最多等 30s
        # SDK 跑完 channel 建立後 rc 會變 0
        max_wait = max(wait_after_click_sec, 30.0)
        check_interval = 3.0
        elapsed = 0.0
        on_log(f"[CH-BOOT-PW] poll verify (max {max_wait}s, every {check_interval}s)")
        while elapsed < max_wait:
            time.sleep(check_interval)
            elapsed += check_interval
            try:
                # 偵測 TOS popup,如果中途出現也算 fail
                tos = page.evaluate("""() => {
                    const dialogs = document.querySelectorAll('[role="dialog"]');
                    for (const d of dialogs) {
                        const txt = (d.textContent || '').substring(0, 300);
                        if (txt.includes('使用條款') || txt.includes('PAYUNi')) {
                            return true;
                        }
                    }
                    return false;
                }""")
                if tos:
                    on_log("[CH-BOOT-PW] ⚠️ chat 頁出現 TOS popup,擋住 SDK")
                    return False, (
                        f"chat 頁 TOS popup 擋住 SDK 初始化.我們的軟件 profile "
                        f"({profile_dir.name})需要手動登入 + 接受條款一次."
                    )
            except Exception:
                pass

            # BOSH 驗證
            try:
                with BOSHSession(profile_dir, on_log=lambda m: None) as s:
                    resp, _ = s.channel_user_active(chid, is_active=True)
                    rc = resp.get("returnCode") if isinstance(resp, dict) else None
                    on_log(f"[CH-BOOT-PW] poll @ {elapsed:.0f}s: rc={rc}")
                    if rc == 0:
                        return True, f"Playwright bootstrap OK,channel 已建立 @{elapsed:.0f}s"
            except Exception as _e_check:
                on_log(f"[CH-BOOT-PW] poll @ {elapsed:.0f}s 異常: {_e_check}")

        return False, (
            f"Playwright 等了 {max_wait}s channel 還沒建立(rc 一直 1106)."
            f"可能 Yahoo 偵測 Playwright 自動化拒絕,或 SDK 在 Playwright 環境跑不起來."
        )

    except Exception as e:
        on_log(f"[CH-BOOT-PW] 異常: {e}")
        return False, f"bootstrap fail: {e}"
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass
        try:
            release(profile_dir)
        except Exception:
            pass


def bootstrap_channel(
    profile_dir: Path,
    buyer_y_id: str,
    order_id: str,
    *,
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """頂層:優先用純 HTTP bootstrap,失敗才 fallback Playwright.

    Args:
        profile_dir: 賣家 profile 目錄
        buyer_y_id: 買家 Y-ID
        order_id: 訂單號

    Returns:
        (success, info)
    """
    on_log = on_log or (lambda *_: None)

    # 1. 先試純 HTTP(~3s,無 Chrome 占用)
    on_log("[CH-BOOT] try HTTP-only first (fast, no Chrome)")
    ok, info = bootstrap_via_http(profile_dir, buyer_y_id, order_id, on_log=on_log)
    if ok:
        return True, f"HTTP {info}"
    on_log(f"[CH-BOOT] HTTP fail ({info[:80]}) → fallback Playwright")

    # 2. fallback Playwright(慢,可能跟 Chrome 衝突)
    ok2, info2 = bootstrap_channel_via_order_page(
        profile_dir, order_id, on_log=on_log,
    )
    if ok2:
        return True, f"PW {info2}"
    return False, f"HTTP fail: {info} | PW fail: {info2}"
