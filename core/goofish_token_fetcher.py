"""閒魚 WebSocket access_token 自動取得(v6.0.75)。

設計理念 — 學採購監控的 refresh_goofish_session(全自動,不需要用戶介入):
- 24h token 過期時,自動跑一次(headless=False + 系統 Chrome + 真實 fingerprint)
- 訪問 /im 頁觸發 login.token API,攔截 response 拿 access_token
- 5-15 秒完成,視窗一閃就關(跟 refresh_goofish_session 體驗一致)
- 失敗了等 30 分鐘再試(避免限流期間反覆刷把瀏覽器 token 踢掉)

跟 "打開登錄瀏覽器" 並列(用戶手動觸發的版本仍然存在,作為「掃碼登入」入口):
- 「打開登錄瀏覽器」= headless=False + 等用戶掃碼 → 用戶看著
- 本模組 auto_refresh_ws_token = headless=False + 自動完成 → 5-10 秒一閃而過

互斥保護:
- _refresh_lock 確保同時只有一個刷 token 操作
- _last_attempt_ts 記錄最後一次嘗試,失敗後 30 分鐘冷卻
"""
from __future__ import annotations
import json
import time
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

LogFn = Optional[Callable[[str], None]]

# 全局互斥
_refresh_lock = threading.Lock()
_last_attempt_ts: float = 0.0
_last_attempt_ok: bool = False
COOLDOWN_AFTER_FAIL_SEC = 180    # 失敗後 3 分鐘不再試(短冷卻,避免 WS 死局)
COOLDOWN_AFTER_OK_SEC = 60       # 成功後 60 秒不重複(避免同個 session 重複觸發)


def _auto_refresh_ws_token_inner(
    profile_dir: Path,
    chrome_path: str = "",
    on_log: LogFn = None,
) -> bool:
    """背景用系統 Chrome 訪問 /im 拿 access_token。

    跟「打開登錄瀏覽器」走同一條路 (系統 Chrome + 真實 fingerprint + 用戶 cookies),
    但自動跑完不等用戶。流程 5-15 秒。

    Returns: True 成功 + 寫緩存 / False 失敗
    """
    def log(m):
        if on_log:
            on_log(m)

    try:
        from core.client_runtime_compat import (
            sync_playwright, get_launch_args, get_ignore_default_args,
            apply_runtime_normalization_sync, GOOFISH_PROXY_BYPASS,
        )
        from core.profile_lock import detect_chrome_profile_in_use, try_acquire, release
        from core.purchase_feature import _get_system_chrome_path
    except Exception as e:
        log(f"[XY-TOKEN] import 失敗: {e}")
        return False

    exe = _get_system_chrome_path(chrome_path) if not chrome_path else chrome_path
    if not exe:
        log("[XY-TOKEN] 找不到系統 Chrome,無法刷新 WS token")
        return False

    # 取 profile 鎖(最多等 30 秒)
    waited = 0
    acquired = False
    while waited < 30:
        in_use, _ = detect_chrome_profile_in_use(profile_dir)
        if not in_use:
            ok, _ = try_acquire(profile_dir, owner="ws-token-refresh")
            if ok:
                acquired = True
                break
        time.sleep(2)
        waited += 2

    if not acquired:
        log("[XY-TOKEN] Profile 鎖被佔用 30 秒,放棄這次刷新")
        return False

    p = None
    ctx = None
    captured = {"access": "", "refresh": "", "exp_ms": 0, "device_id": ""}
    try:
        p = sync_playwright().start()

        # 跟「打開登錄瀏覽器」用同一套參數 — 系統 Chrome (channel/executable_path)
        # 視窗會開但 5-10 秒自動關
        _kw = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,
            locale="zh-CN",
            accept_downloads=False,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            args=get_launch_args(headless=False, lang="zh-CN", extra=[
                "--disable-features=TranslateUI,ThirdPartyCookiesDeprecation,"
                "TrackingProtection3pcd,PrivacySandboxSettings4",
                # v6.0.75:視窗極端屏幕外 + 啟動最小化(用戶幾乎察覺不到閃現)
                "--window-position=-32000,-32000",
                "--window-size=400,300",
                "--start-minimized",
                f"--proxy-bypass-list={GOOFISH_PROXY_BYPASS}",
            ]),
            ignore_default_args=get_ignore_default_args(headless=False),
            executable_path=exe,
        )

        try:
            ctx = p.chromium.launch_persistent_context(**_kw)
        except TypeError:
            _kw.pop("no_viewport", None)
            _kw["viewport"] = {"width": 800, "height": 600}
            ctx = p.chromium.launch_persistent_context(**_kw)
        apply_runtime_normalization_sync(ctx)

        # v6.0.75 關鍵:Chrome 重啟時 session cookie 從記憶體蒸發,只剩 9 個 persistent。
        # 從 cache 注入完整 31 條 cookies(含 cookie2/_tb_token_/sgcookie 等 session cookie),
        # 模擬「上次關閉時的狀態」,避免 BX 認為新會話觸發滑塊驗證
        try:
            from core.goofish_cookie_store import load_goofish_raw_cookies
            cached_raw = load_goofish_raw_cookies(profile_dir)
            if cached_raw:
                # Playwright 對 expires/sameSite 格式敏感,標準化下
                normalized = []
                for c in cached_raw:
                    nc = dict(c)
                    # expires: -1 / 0 / None → 不傳(session cookie)
                    exp = nc.get("expires", -1)
                    if exp is None or exp == -1 or exp == 0:
                        nc.pop("expires", None)
                    # sameSite: 標準化 ("None"/"Lax"/"Strict")
                    ss = nc.get("sameSite", "")
                    if ss and ss not in ("None", "Lax", "Strict"):
                        nc.pop("sameSite", None)
                    # 移除 Playwright 不認的字段
                    for k in ("priority", "session", "storeId", "hostOnly"):
                        nc.pop(k, None)
                    if nc.get("name") and nc.get("domain"):
                        normalized.append(nc)
                if normalized:
                    ctx.add_cookies(normalized)
                    log(f"[XY-TOKEN] ✓ 從 cache 注入 {len(normalized)} 條 cookies (含 session cookie,模擬未關 Chrome)")
        except Exception as _e:
            log(f"[XY-TOKEN] 注入 cache cookies 失敗(繼續): {_e}")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 註冊 login.token 攔截器(含 deviceId 抽取)
        def _on_response(resp):
            try:
                url = resp.url
                if "mtop.taobao.idlemessage.pc.login.token" not in url:
                    return

                # 1. 抽 request 內的 deviceId (跟 token 強綁,Python 後續要用同個)
                # post_data 通常是 URL encoded JSON,先 unquote 再 regex
                try:
                    from urllib.parse import unquote
                    req = resp.request
                    src_url = req.url or ""
                    src_post = req.post_data or ""
                    # URL decode 兩次(post_data 是 form-encoded 的 URL-encoded JSON)
                    src_combined = unquote(unquote(src_url + "|" + src_post))
                    import re as _re2
                    m_did = _re2.search(r'"deviceId"\s*:\s*"([^"]+)"', src_combined)
                    if m_did:
                        captured["device_id"] = m_did.group(1)
                        log(f"[XY-TOKEN] ✓ 抽到 deviceId: {captured['device_id']}")
                    else:
                        log(f"[XY-TOKEN] ⚠ 抽 deviceId 失敗,decoded={src_combined[:200]}")
                except Exception as _ex:
                    log(f"[XY-TOKEN] 抽 deviceId 異常: {_ex}")

                # 2. 抽 response 內的 access_token
                try:
                    body = resp.text()
                except Exception:
                    return
                import re as _re
                m = _re.search(r'\{.*\}', body, _re.DOTALL)
                if not m:
                    return
                j = json.loads(m.group())
                ret = j.get("ret", [])
                if not any("SUCCESS" in str(x) for x in ret):
                    log(f"[XY-TOKEN] login.token 攔到但非 SUCCESS: {ret}")
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
                    log(f"[XY-TOKEN] ✓ 攔到 access_token: {tk[:24]}...{tk[-12:]} (len={len(tk)})")
            except Exception as _e:
                log(f"[XY-TOKEN] response 攔截異常: {_e}")

        page.on("response", _on_response)

        # 訪問首頁 warmup (模擬用戶正常訪問)
        try:
            log("[XY-TOKEN] 訪問首頁 warmup...")
            page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
        except Exception as e:
            log(f"[XY-TOKEN] 首頁訪問異常 (繼續): {e}")

        # 訪問 /im 觸發 login.token
        try:
            log("[XY-TOKEN] 訪問 /im 觸發 login.token...")
            page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            log(f"[XY-TOKEN] /im 訪問異常 (繼續): {e}")

        # 先等 5 秒讓 BX SDK 加載完
        page.wait_for_timeout(5000)

        # 主動在 Chrome JS 上下文呼 login.token(避免前端認為已登入不重呼)
        # 之前測試:訪問 /im 但前端 SDK 不會自動呼 login.token(因為 access_token 還在 sessionStorage)
        # 我們在 JS context 內帶 cookie + BX 簽名 fetch,server 會接受
        if not captured["access"]:
            log("[XY-TOKEN] 主動在 JS context 呼 login.token (BX 已加載)...")
            try:
                # 從 Chrome cookie 拿 _m_h5_tk
                h5tk = page.evaluate("""() => {
                    const m = document.cookie.match(/_m_h5_tk=([^;]+)/);
                    return m ? m[1] : '';
                }""")
                if h5tk and "_" in h5tk:
                    import hashlib as _hl
                    import time as _t
                    import json as _j
                    mtop_token = h5tk.split("_")[0]
                    # 用持久化 deviceId(已存就用,沒就生成)
                    pc_uuid = ""
                    full_device_id = ""
                    try:
                        device_fp_pre = Path(__file__).resolve().parent.parent / "runtime" / "goofish_device.json"
                        if device_fp_pre.exists():
                            _dd = _j.loads(device_fp_pre.read_text(encoding="utf-8"))
                            pc_uuid = _dd.get("device_uuid", "")
                            full_device_id = _dd.get("full_device_id", "")
                    except Exception:
                        pass
                    if not pc_uuid:
                        import uuid as _uuid
                        pc_uuid = str(_uuid.uuid4()).upper()
                    # 從 cookie 拿 unb 拼 device_id
                    unb_val = page.evaluate("""() => {
                        const m = document.cookie.match(/unb=([^;]+)/);
                        return m ? m[1] : '';
                    }""")
                    if not full_device_id and unb_val:
                        full_device_id = f"{pc_uuid}-{unb_val}"

                    WS_APP_KEY = "<XIANYU_WS_APP_KEY_REDACTED>"
                    APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
                    API = "mtop.taobao.idlemessage.pc.login.token"
                    data_obj = {"appKey": WS_APP_KEY, "deviceId": full_device_id}
                    data_str = _j.dumps(data_obj, separators=(",", ":"))
                    t = str(int(_t.time() * 1000))
                    sign = _hl.md5(f"{mtop_token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()
                    qs = (
                        f"jsv=2.7.2&appKey={APP_KEY}&t={t}&sign={sign}&v=1.0"
                        f"&type=originaljson&accountSite=xianyu&dataType=json&timeout=20000"
                        f"&AntiCreep=true&AntiFlool=true&api={API}"
                    )
                    ret = page.evaluate(f"""async () => {{
                        try {{
                            const r = await fetch('https://h5api.m.goofish.com/h5/{API}/1.0/?{qs}', {{
                                method: 'POST',
                                credentials: 'include',
                                headers: {{
                                    'Content-Type': 'application/x-www-form-urlencoded',
                                    'Origin': 'https://www.goofish.com',
                                    'Referer': 'https://www.goofish.com/im',
                                }},
                                body: 'data=' + encodeURIComponent({_j.dumps(data_str)}),
                            }});
                            const txt = await r.text();
                            return {{status: r.status, text: txt}};
                        }} catch(e) {{ return {{error: String(e)}}; }}
                    }}""")
                    if isinstance(ret, dict) and ret.get("text"):
                        try:
                            resp_j = _j.loads(ret["text"])
                            data = resp_j.get("data", {}) or {}
                            access_tk = str(data.get("accessToken", "") or "")
                            refresh_tk = str(data.get("refreshToken", "") or "")
                            exp_ms = int(data.get("accessTokenExpiredTime", 0) or 86400000)
                            if access_tk:
                                captured["access"] = access_tk
                                captured["refresh"] = refresh_tk
                                captured["exp_ms"] = exp_ms
                                captured["device_id"] = full_device_id
                                log(f"[XY-TOKEN] ✓ JS fetch login.token 成功: {access_tk[:24]}...")
                            else:
                                log(f"[XY-TOKEN] JS fetch 沒拿到 accessToken,ret={ret['text'][:120]}")
                        except Exception as _e2:
                            log(f"[XY-TOKEN] JS fetch 解析失敗: {_e2}, raw={ret['text'][:80]}")
                    else:
                        log(f"[XY-TOKEN] JS fetch 異常: {ret}")
                else:
                    log("[XY-TOKEN] Chrome cookie 內無 _m_h5_tk,放棄 JS fetch")
            except Exception as _e:
                log(f"[XY-TOKEN] JS fetch 異常: {_e}")

        # 再等 5 秒看 response 攔截(雙保險:JS fetch + page.on response)
        for _ in range(10):
            page.wait_for_timeout(500)
            if captured["access"]:
                break

        if not captured["access"]:
            # 偵測是不是 RGV587 异常码 / 重定向到登入頁(滑塊) → 把視窗顯示到屏幕中央讓用戶手動處理
            try:
                has_captcha_or_login = page.evaluate("""() => {
                    const url = location.href || '';
                    if (url.includes('mini_login') || url.includes('passport.')) return 'login_page';
                    const t = document.body ? document.body.innerText : '';
                    if (t.includes('请拖动') || t.includes('滑块') || t.includes('完成验证') || t.includes('拖动下方')) return 'captcha';
                    if (document.querySelector('iframe[src*="punish"], iframe[src*="captcha"], .nc_wrapper, .nc-container, #nc_1_wrapper, .baxia-dialog')) return 'captcha';
                    return '';
                }""")
            except Exception:
                has_captcha_or_login = ""
            if has_captcha_or_login:
                log(f"[XY-TOKEN] ⚠ 偵測到限流/滑塊({has_captcha_or_login}),顯示視窗讓用戶手動處理")
                # 把視窗從屏幕外移到中央
                try:
                    cdp = ctx.new_cdp_session(page)
                    window_info = cdp.send("Browser.getWindowForTarget")
                    window_id = window_info.get("windowId")
                    if window_id:
                        cdp.send("Browser.setWindowBounds", {
                            "windowId": window_id,
                            "bounds": {"left": 200, "top": 100, "width": 1000, "height": 750, "windowState": "normal"},
                        })
                        log("[XY-TOKEN] ✓ 視窗已顯示到屏幕中央,請手動完成驗證(60 秒內)")
                except Exception as _e:
                    log(f"[XY-TOKEN] 視窗移動失敗: {_e}")

                # 等用戶處理(60 秒)
                for _ in range(120):  # 60s
                    page.wait_for_timeout(500)
                    if captured["access"]:
                        log("[XY-TOKEN] ✓ 用戶完成驗證,拿到 token")
                        break
                if not captured["access"]:
                    log("[XY-TOKEN] 60 秒內未完成驗證")
                    return False
            else:
                log("[XY-TOKEN] JS fetch + page.on 都未拿到 login.token (沒偵測到滑塊)")
                return False

        # v6.0.75:必須同步 deviceId(跟 token 強綁)
        browser_did = captured.get("device_id", "")
        if browser_did:
            try:
                import json as _json
                import time as _time
                device_fp = Path(__file__).resolve().parent.parent / "runtime" / "goofish_device.json"
                device_fp.parent.mkdir(parents=True, exist_ok=True)
                parts = browser_did.rsplit("-", 1)
                uuid_part = parts[0] if (len(parts) == 2 and parts[1].isdigit()) else browser_did
                device_fp.write_text(_json.dumps({
                    "device_uuid": uuid_part,
                    "full_device_id": browser_did,
                    "created_at": _time.time(),
                    "created_ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "auto_refresh",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"[XY-TOKEN] ✓ device_uuid 同步: {uuid_part}")
            except Exception as e:
                log(f"[XY-TOKEN] device_uuid 同步異常: {e}")

        # 寫緩存
        try:
            from core.xianyu_im_http import save_cached_access_token
            save_cached_access_token(
                profile_dir,
                captured["access"],
                captured["refresh"],
                captured["exp_ms"] or 86400000,
            )
            log("[XY-TOKEN] ✓ access_token 已寫入緩存(24h 有效)")
        except Exception as e:
            log(f"[XY-TOKEN] 寫 access_token 緩存異常: {e}")

        # v6.0.75:Playwright 訪問 /im 期間 server 會 set 新 cookies 到 context,
        # 順手把 Playwright context 的 cookies 同步寫到 goofish_cookie_cache.json
        # 這樣 Python mtop API 也能用新 cookie,避免 ILLEGAL_ACCESS
        try:
            cookies_list = ctx.cookies()
            from core.goofish_cookie_store import save_goofish_cookies
            save_goofish_cookies(
                profile_dir,
                cookies_list,
                account_hint=f"playwright_auto_refresh|unb_from_browser",
            )
            log(f"[XY-TOKEN] ✓ 順手同步 {len(cookies_list)} 條 Playwright cookies 到 cache")
        except Exception as e:
            log(f"[XY-TOKEN] 同步 Playwright cookies 異常: {e}")

        return True

    except Exception as e:
        import traceback
        log(f"[XY-TOKEN] auto_refresh 異常: {e}\n{traceback.format_exc()[:300]}")
        return False
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


def auto_refresh_ws_token(
    profile_dir: Path,
    chrome_path: str = "",
    on_log: LogFn = None,
) -> bool:
    """互斥保護 + 冷卻策略包裝。

    - 多執行緒只允許一個跑(_refresh_lock)
    - 失敗後 30 分鐘內不再試
    - 成功後 60 秒內不重複
    """
    global _last_attempt_ts, _last_attempt_ok

    if not _refresh_lock.acquire(blocking=False):
        if on_log:
            on_log("[XY-TOKEN] 已有另一個 refresh 在跑,跳過")
        return False

    try:
        now = time.time()
        age = now - _last_attempt_ts
        if _last_attempt_ts > 0:
            if _last_attempt_ok and age < COOLDOWN_AFTER_OK_SEC:
                if on_log:
                    on_log(f"[XY-TOKEN] 剛剛成功 ({age:.0f}s 前),跳過")
                return True
            if not _last_attempt_ok and age < COOLDOWN_AFTER_FAIL_SEC:
                if on_log:
                    on_log(f"[XY-TOKEN] 失敗冷卻中 (上次 {age:.0f}s 前失敗,需等 {COOLDOWN_AFTER_FAIL_SEC}s)")
                return False

        _last_attempt_ts = now
        ok = _auto_refresh_ws_token_inner(profile_dir, chrome_path, on_log)
        _last_attempt_ok = ok
        return ok
    finally:
        _refresh_lock.release()


_RGV587_COOLDOWN_SEC = 1800  # 30 分鐘
_rgv587_blocked_until: float = 0.0


def ensure_access_token(
    profile_dir: Path,
    device_id: str,
    on_log: LogFn = None,
    force_refresh: bool = False,
) -> Tuple[str, str]:
    """取 WS access_token(純 HTTP/WS 路徑,v6.0.78 後無 Playwright fallback)。

    流程:
    1. 緩存命中 → 直接返回(0 秒)
    2. 緩存過期 + 不在 RGV587 冷卻 → 試 HTTP get_login_token
    3. RGV587 觸發 → 寫 30 分鐘冷卻 + 提示用戶手動「打開閒魚登入瀏覽器」

    Returns: (access_token, error_msg)
    """
    global _rgv587_blocked_until
    from core.xianyu_im_http import (
        load_cached_access_token, save_cached_access_token, get_login_token,
    )

    # 1. 緩存命中(最常見路徑 — 24h TTL,大多數時候走這條)
    if not force_refresh:
        cached, expires_at = load_cached_access_token(profile_dir)
        if cached:
            return cached, ""

    # v6.0.79:RGV587 冷卻檢查 — 短時間內被限流過 → 不再撞,直接讓上層提示用戶
    import time as _time
    now = _time.time()
    if _rgv587_blocked_until > now:
        remain = int(_rgv587_blocked_until - now)
        if on_log:
            on_log(f"[XY-TOKEN] RGV587 冷卻中(剩餘 {remain}s),跳過 HTTP refresh")
        return "", (
            f"閒魚 RGV587 异常码冷卻中(剩餘 {remain // 60} 分鐘)。"
            "請去採購頁點【打開閒魚登入瀏覽器】手動養 access_token。"
        )

    if not device_id:
        if on_log:
            on_log("[XY-TOKEN] deviceId 為空(首次啟動),需手動初始化")
        return "", "deviceId 缺失:首次啟動需要透過採購頁打開閒魚登入瀏覽器一次"

    # 2. 純 HTTP 嘗試
    if on_log:
        on_log("[XY-TOKEN] 緩存空/過期,試純 HTTP refresh (不開瀏覽器)")
    try:
        access, refresh_tk, exp_ms, err = get_login_token(profile_dir, device_id, on_log)
        if access and not err:
            save_cached_access_token(profile_dir, access, refresh_tk, exp_ms or 86400000)
            if on_log:
                on_log("[XY-TOKEN] ✓ 純 HTTP refresh 成功")
            return access, ""

        # 識別 RGV587 异常码 → 寫冷卻
        if err and ("RGV587" in err or "USER_VALIDATE" in err or "挤爆" in err):
            _rgv587_blocked_until = now + _RGV587_COOLDOWN_SEC
            if on_log:
                on_log(f"[XY-TOKEN] ⚠️ RGV587 异常码觸發,冷卻 {_RGV587_COOLDOWN_SEC // 60} 分鐘: {err[:80]}")
            return "", (
                f"閒魚 RGV587 异常码觸發(server 阻止反爬蟲)。"
                "請去採購頁點【打開閒魚登入瀏覽器】手動養 access_token。"
            )

        if on_log:
            on_log(f"[XY-TOKEN] 純 HTTP refresh 失敗: {err[:80]}")
    except Exception as e:
        if on_log:
            on_log(f"[XY-TOKEN] 純 HTTP refresh 異常: {e}")

    return "", "純 HTTP refresh 失敗:請去採購頁點【打開閒魚登入瀏覽器】重新登入"
