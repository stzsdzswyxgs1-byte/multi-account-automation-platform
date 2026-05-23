from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple, List

from .client_runtime_compat import async_playwright, PwTimeoutError, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args
from .profile_lock import try_acquire, release, detect_chrome_profile_in_use
from .human import human_interval_sec, human_jitter_ms, maybe_extra_think_ms

LogFn = Callable[[str], None]

def _visual_slow_mo_ms() -> int:
    """视觉化慢动作（ms）。通过环境变量 MERCH_VISUAL_SLOW_MO_MS 控制。"""
    try:
        v = int(os.environ.get("MERCH_VISUAL_SLOW_MO_MS", "250"))
    except Exception:
        v = 250
    if v < 0:
        v = 0
    if v > 3000:
        v = 3000
    return v



async def _save_debug_screenshot(page, tag: str, log: Optional[LogFn], out_dir: Optional[Path] = None) -> None:
    """保存调试截图（失败不影响主流程）。

    批量/续跑场景里，偶发页面改版或元素点击失败时，截图能快速定位问题。
    """
    try:
        root = Path(__file__).resolve().parent.parent
        d = Path(out_dir) if out_dir else (root / "debug")
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        p = d / f"merch_batch_{tag}_{ts}.png"
        await page.screenshot(path=str(p), full_page=True)
        _log(log, f"[BATCH {_ts()}] debug screenshot saved: {p}")
    except Exception:
        return


@dataclass
class BatchConfig:
    """Yahoo 拍賣商品批量操作參數

    mode: '上架' / '下架' / '刪除'（亦支援 '删除'）
    repeat: 重複次數
    interval_sec: 每輪完成後等待秒數，之後 reload 再下一輪
    headless: 是否無頭
    """
    mode: str
    repeat: int
    interval_sec: float
    headless: bool = False


# ✅ 你指定的入口（注意：刪除用 close + sortBy=%2BoffTime）
LIST_URLS = {
    "下架": "https://tw.bid.yahoo.com/partner/merchandise/list_merchandise?qType=keyword&itemStatus=shelve&categoryCustomId=all&isApplyShippingRule=all&type=all&sortBy=%2BonTime",
    "上架": "https://tw.bid.yahoo.com/partner/merchandise/list_merchandise?qType=keyword&itemStatus=close&categoryCustomId=all&isApplyShippingRule=all&type=all&sortBy=-offTime",
    "刪除": "https://tw.bid.yahoo.com/partner/merchandise/list_merchandise?qType=keyword&itemStatus=close&categoryCustomId=all&isApplyShippingRule=all&type=all&sortBy=%2BoffTime",
}


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg, flush=True)


def _norm_mode(mode: str) -> str:
    m = (mode or "").strip()
    if m in ("删除", "刪除"):
        return "刪除"
    if m in ("上架", "下架"):
        return m
    raise ValueError(f"Unknown mode: {mode!r}")


async def _safe_click(loc, timeout_ms: int = 4000, force: bool = True) -> bool:
    """点击更像人：先 hover + 轻微停顿，优先非 force 点击；失败再回退 force 点击（不改变功能可靠性）。"""
    try:
        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass

    # 先 hover 一下，像人把鼠标移到按钮上
    try:
        await loc.hover(timeout=timeout_ms)
    except Exception:
        pass
    try:
        await asyncio.sleep((human_jitter_ms(120) + maybe_extra_think_ms()) / 1000.0)
    except Exception:
        pass

    # 优先尝试非 force（更像人）
    try:
        await loc.click(timeout=timeout_ms, force=False)
        return True
    except Exception:
        pass

    # 失败再按原逻辑回退（保证不影响功能）
    if force:
        try:
            await loc.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            return False
    return False


async def _get_bbox(loc):
    try:
        if not await loc.is_visible():
            return None
        return await loc.bounding_box()
    except Exception:
        return None


async def _find_goods_header_bbox(page, y_min: float) -> Optional[dict]:
    """找『商品』欄位標題（你紅框旁邊那個『商品』文字）的 bbox，用於鎖定同一行的 checkbox。"""
    try:
        goods = page.get_by_text("商品", exact=True)
        n = await goods.count()
        best = None
        for i in range(min(n, 30)):
            bb = await _get_bbox(goods.nth(i))
            if not bb:
                continue
            # 鎖定主內容區（排除左側菜單/頁首）
            if bb["y"] >= y_min and bb["y"] <= y_min + 400 and bb["x"] <= 600:
                best = bb
                break
        return best
    except Exception:
        return None


async def _collect_visible_checkboxes(page, y_min: float) -> List[Tuple[object, dict]]:
    """收集主內容區的可見 checkbox（排除頁首 menu toggle 那種）。"""
    cbs = page.locator("input[type='checkbox']")
    n = await cbs.count()
    out: List[Tuple[object, dict]] = []
    for i in range(min(n, 120)):
        loc = cbs.nth(i)
        bb = await _get_bbox(loc)
        if not bb:
            continue

        # 排除：太上面的（頁首/選單 toggle）
        if bb["y"] < y_min:
            continue

        # 排除：很右邊的 checkbox（通常不是商品清單用）
        if bb["x"] > 700:
            continue

        try:
            cid = await loc.get_attribute("id")
            cls = await loc.get_attribute("class")
            if cid and cid.startswith("uh-"):
                continue
            if cls and "UhMenu" in cls:
                continue
        except Exception:
            pass

        out.append((loc, bb))
    return out


async def _compute_y_min(page) -> float:
    """動態算出商品清單開始位置，用來排除頁首 checkbox。"""
    # 先找「第 1 - 40 筆」這行（在清單上方）
    try:
        loc = page.get_by_text(re.compile(r"第\s*\d+\s*-\s*\d+\s*筆"))
        await loc.first.wait_for(state="visible", timeout=20000)
        bb = await _get_bbox(loc.first)
        if bb:
            return bb["y"] + bb["height"] + 10
    except Exception:
        pass

    # fallback：找欄位標題（如 金額/出價），它通常在表頭行
    try:
        loc = page.get_by_text("金額/出價", exact=False)
        await loc.first.wait_for(state="visible", timeout=20000)
        bb = await _get_bbox(loc.first)
        if bb:
            return max(0.0, bb["y"] - 50)
    except Exception:
        pass

    # 最後 fallback：給一個保守的值
    return 250.0


async def _pick_header_checkbox(page, y_min: float, log: Optional[LogFn]) -> Optional[Tuple[object, dict]]:
    """優先挑『商品』同一行（表頭行）左側的 checkbox（你紅框那顆）。"""
    goods_bb = await _find_goods_header_bbox(page, y_min)
    candidates = await _collect_visible_checkboxes(page, y_min)

    if not candidates:
        return None

    # 如果能拿到『商品』欄位的 bbox：找 y 最接近、且在其左側的 checkbox
    if goods_bb:
        gx, gy = goods_bb["x"], goods_bb["y"]
        best = None
        best_score = 10**9
        for loc, bb in candidates:
            # 同一行（容忍 35px）
            if abs((bb["y"] - gy)) > 35:
                continue
            # 要在『商品』文字左側附近
            if bb["x"] > gx:
                continue
            # 越靠近商品文字左側越好
            score = abs((gx - bb["x"])) + abs((gy - bb["y"])) * 2
            if score < best_score:
                best_score = score
                best = (loc, bb)
        if best:
            return best

    # 拿不到『商品』bbox 的話：取主內容區「最靠上的、最靠左」那顆
    candidates.sort(key=lambda t: (t[1]["y"], t[1]["x"]))
    return candidates[0]


async def _pick_first_row_checkbox(page, y_min: float, after_y: float) -> Optional[Tuple[object, dict]]:
    """挑第一筆商品列的 checkbox（保證按鈕會亮）。"""
    candidates = await _collect_visible_checkboxes(page, y_min)
    # 只挑在表頭下方的
    candidates = [(loc, bb) for (loc, bb) in candidates if bb["y"] > after_y + 8]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[1]["y"], t[1]["x"]))
    return candidates[0]


async def _wait_list_ready(page, cfg_mode: str, log: Optional[LogFn], is_stop: Optional[Callable[[], bool]] = None, is_pause: Optional[Callable[[], bool]] = None) -> float:
    """等待清單就緒，返回 y_min（供後續定位 checkbox）。"""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(human_jitter_ms(200) + maybe_extra_think_ms())

    y_min = await _compute_y_min(page)

    # 等到主內容區真的能找到清單 checkbox（不是頁首 menu toggle）
    deadline = time.time() + 35
    last_err = None
    while time.time() < deadline:
        try:
            candidates = await _collect_visible_checkboxes(page, y_min)
            if candidates:
                return y_min
        except Exception as e:
            last_err = e
        await page.wait_for_timeout(human_jitter_ms(300) + maybe_extra_think_ms())

    raise PwTimeoutError(f"list not ready: cannot find list checkbox; y_min={y_min}. last_err={last_err}")


async def _is_action_enabled(page, mode: str) -> bool:
    mode = _norm_mode(mode)
    names = [mode]
    if mode == "刪除":
        names = ["刪除", "删除"]
    # 只要其中一個按鈕 enabled 就算可操作
    for n in names:
        try:
            btn = page.get_by_role("button", name=n).first
            if await btn.is_visible() and await btn.is_enabled():
                return True
        except Exception:
            pass
        try:
            btn = page.locator(f"button:has-text('{n}')").first
            if await btn.is_visible() and await btn.is_enabled():
                return True
        except Exception:
            pass
    return False


async def _click_action(page, mode: str, log: Optional[LogFn]) -> bool:
    mode = _norm_mode(mode)
    names = [mode]
    if mode == "刪除":
        names = ["刪除", "删除"]

    # 優先 role=button
    for n in names:
        try:
            btn = page.get_by_role("button", name=n).first
            if await _safe_click(btn):
                _log(log, f"[BATCH {_ts()}] CLICK ACTION: {n}")
                return True
        except Exception:
            pass

    # fallback：CSS
    for n in names:
        try:
            btn = page.locator(f"button:has-text('{n}')").first
            if await _safe_click(btn):
                _log(log, f"[BATCH {_ts()}] CLICK ACTION: {n}")
                return True
        except Exception:
            pass

    _log(log, f"[BATCH {_ts()}] WARN: 點不到『{mode}』按鈕（可能未勾選成功或頁面改版）")
    return False


async def _confirm_submit_if_any(page, log: Optional[LogFn]) -> None:
    """點彈窗的『送出/確定/確認』。"""
    # 等一下彈窗出現（不出現就算）
    try:
        await page.locator("[role='dialog']").first.wait_for(state="visible", timeout=2500)
    except Exception:
        pass

    # 優先送出
    for text in ["送出", "確定", "確認", "Submit", "OK"]:
        try:
            btn = page.get_by_role("button", name=text).first
            if await _safe_click(btn, timeout_ms=3500):
                _log(log, f"[BATCH {_ts()}] CLICK CONFIRM: {text}")
                await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())
                return
        except Exception:
            pass
        try:
            btn = page.locator(f"button:has-text('{text}')").first
            if await _safe_click(btn, timeout_ms=3500):
                _log(log, f"[BATCH {_ts()}] CLICK CONFIRM: {text}")
                await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())
                return
        except Exception:
            pass


async def _select_header_then_row_if_needed(page, y_min: float, mode: str, log: Optional[LogFn]) -> bool:
    """照你說的流程：
    1) 先點『商品』旁邊那顆（表頭 checkbox）
    2) 若按鈕仍灰，改點第一筆商品列 checkbox（保證按鈕亮）
    """
    picked = await _pick_header_checkbox(page, y_min, log)
    if not picked:
        _log(log, f"[BATCH {_ts()}] ERROR: 找不到表頭 checkbox")
        return False

    header_loc, header_bb = picked
    if await _safe_click(header_loc, timeout_ms=4000):
        _log(log, f"[BATCH {_ts()}] CLICK SELECT: header checkbox (x={header_bb['x']:.1f}, y={header_bb['y']:.1f})")
    else:
        _log(log, f"[BATCH {_ts()}] WARN: header checkbox click failed")

    await page.wait_for_timeout(human_jitter_ms(200) + maybe_extra_think_ms())

    if await _is_action_enabled(page, mode):
        return True

    # fallback：點第一筆商品列
    row = await _pick_first_row_checkbox(page, y_min, after_y=header_bb["y"])
    if not row:
        _log(log, f"[BATCH {_ts()}] ERROR: 找不到第一筆商品列 checkbox")
        return False

    row_loc, row_bb = row
    if await _safe_click(row_loc, timeout_ms=4000):
        _log(log, f"[BATCH {_ts()}] CLICK SELECT: first-row checkbox (x={row_bb['x']:.1f}, y={row_bb['y']:.1f})")
        await page.wait_for_timeout(human_jitter_ms(200) + maybe_extra_think_ms())
        return await _is_action_enabled(page, mode)

    return False



async def run_batch_step(
    *,
    profile_dir: Path,
    chrome_path: str,
    mode: str,
    headless: bool = False,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> str:
    """执行 1 次批量动作（上架/下架/刪除），用于“可恢复队列/进度续跑”。

    返回值：
      - 'ok'      : 本轮成功执行（或已提交）
      - 'paused'  : 接管请求 -> 已退出并释放 profile lock
      - 'stopped' : 停止请求
      - 'locked'  : profile 正被占用（接管/用户打开窗口/其他任务）
      - 'error'   : 发生异常（本轮跳过；调用方可选择重试/继续）
    """
    mode = _norm_mode(mode)
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    if is_stop and is_stop():
        return "stopped"
    if is_pause and is_pause():
        return "paused"

    in_use, reason = detect_chrome_profile_in_use(profile_dir)
    if in_use:
        _log(log, f"[BATCH {_ts()}] LOCKED: {reason}")
        return "locked"

    ok, reason = try_acquire(profile_dir, owner=f"merch-batch-step:{mode}")
    if not ok:
        _log(log, f"[BATCH {_ts()}] LOCKED: {reason}")
        return "locked"

    async with async_playwright() as p:
        context = None
        proxy_kw = {"server": proxy} if proxy else None
        try:
            _hl = bool(headless)
            _args = get_launch_args(headless=_hl)
            if _hl:
                _args.append("--window-size=1280,860")
            _lkw = dict(
                slow_mo=_visual_slow_mo_ms(),
                user_data_dir=str(profile_dir),
                executable_path=chrome_path,
                headless=False,
                proxy=proxy_kw,
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=_hl),
            )
            _lkw["no_viewport"] = True   # Patchright: headless=False 下 viewport 会 getWindowForTarget
            try:
                context = await p.chromium.launch_persistent_context(**_lkw)
            except TypeError:
                _lkw.pop("no_viewport", None)
                context = await p.chromium.launch_persistent_context(**_lkw)
            except Exception as e:
                _log(log, f"[BATCH {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:180]}")
                return "error"
            await apply_runtime_normalization_async(context)
            _log(log, "[BATCH] runtime compat init OK")

            page = context.pages[0] if context.pages else await context.new_page()

            url = LIST_URLS.get(mode)
            if not url:
                return "error"

            cur = page.url or ""
            if "/partner/merchandise/list_merchandise" not in cur:
                _log(log, f"[BATCH {_ts()}] GOTO: {url}")
                await page.goto(url, wait_until="domcontentloaded")
            else:
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass

            if is_stop and is_stop():
                return "stopped"
            if is_pause and is_pause():
                return "paused"

            y_min = await _wait_list_ready(page, mode, log, is_stop=is_stop, is_pause=is_pause)

            if is_pause and is_pause():
                return "paused"

            ok_sel = await _select_header_then_row_if_needed(page, y_min=y_min, mode=mode, log=log)
            if not ok_sel:
                await _save_debug_screenshot(page, "select_failed", log, out_dir=profile_dir / "debug")
                return "error"

            if is_pause and is_pause():
                return "paused"
            if is_stop and is_stop():
                return "stopped"

            ok_btn = await _click_action(page, mode, log)
            if not ok_btn:
                await _save_debug_screenshot(page, f"btn_{mode}_notclick", log, out_dir=profile_dir / "debug")
                return "error"

            # 关键区：从点按钮到送出，避免半途中断
            await _confirm_submit_if_any(page, log)
            try:
                await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
            except Exception:
                pass

            return "ok"
        finally:
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            release(profile_dir)


async def run_batch(
    profile_dir: Path,
    chrome_path: str,
    cfg: BatchConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """使用指定 Profile 在 Yahoo 後台商品列表頁批量操作（上架/下架/刪除）。

    僅操作當前頁（通常 1~40 筆），做 N 次循環：
    勾選（表頭商品 checkbox）-> 按鈕（上/下/刪）-> 送出 -> 等待 interval -> 刷新
    """
    lock_ok = False
    mode = _norm_mode(cfg.mode)
    url = LIST_URLS.get(mode)
    if not url:
        raise ValueError(f"Unknown mode: {cfg.mode}")

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    in_use, reason = detect_chrome_profile_in_use(profile_dir)
    if in_use:
        _log(log, f"[BATCH {_ts()}] LOCKED: {reason}")
        return

    # 轻量锁：避免同一 Profile 被监控/批量同时占用
    ok, reason = try_acquire(profile_dir, owner=f"batch:{mode}")
    if not ok:
        _log(log, f"[BATCH {_ts()}] LOCKED: {reason}")
        return
    lock_ok = True

    _log(log, f"[BATCH {_ts()}] START mode={mode} repeat={cfg.repeat} interval={cfg.interval_sec}s headless={cfg.headless}")

    async with async_playwright() as p:
        context = None
        proxy_kw = None
        if proxy:
            proxy_kw = {"server": proxy}

        try:
            _hl = bool(cfg.headless)
            _args = get_launch_args(headless=_hl)
            if _hl:
                _args.append("--window-size=1280,860")
            _lkw = dict(
                slow_mo=_visual_slow_mo_ms(),
                user_data_dir=str(profile_dir),
                executable_path=chrome_path,
                headless=False,
                proxy=proxy_kw,
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=_hl),
            )
            _lkw["no_viewport"] = True   # Patchright: headless=False 下 viewport 会 getWindowForTarget
            try:
                context = await p.chromium.launch_persistent_context(**_lkw)
            except TypeError:
                _lkw.pop("no_viewport", None)
                context = await p.chromium.launch_persistent_context(**_lkw)
            await apply_runtime_normalization_async(context)
            _log(log, "[BATCH] runtime compat init OK")
        except Exception as e:
            _log(log, f"[BATCH {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:160]}")
            if lock_ok:
                release(profile_dir)
            return
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # 進入目標頁（若不在 list 頁就跳過去）
            cur = page.url or ""
            if "/partner/merchandise/list_merchandise" not in cur:
                _log(log, f"[BATCH {_ts()}] GOTO: {url}")
                await page.goto(url, wait_until="domcontentloaded")
            else:
                # 即使在 list 頁，也強制切到正確 itemStatus（下架=shelve；上架/刪除=close）
                if (mode == "下架" and "itemStatus=shelve" not in cur) or (mode != "下架" and "itemStatus=close" not in cur):
                    _log(log, f"[BATCH {_ts()}] GOTO: {url}")
                    await page.goto(url, wait_until="domcontentloaded")

            for r in range(cfg.repeat):
                if is_stop and is_stop():
                    _log(log, f"[BATCH {_ts()}] STOP requested")
                    return

                _log(log, f"[BATCH {_ts()}] ROUND {r+1}/{cfg.repeat} -> {mode}")

                # 等清單就緒（不要用泛用 input[type=checkbox]，會抓到頁首 menu toggle）
                try:
                    y_min = await _wait_list_ready(page, mode, log)
                except PwTimeoutError as e:
                    _log(log, f"[BATCH {_ts()}] ERROR: 清單未就緒（可能未登入或頁面改版）。url={page.url}")
                    _log(log, f"[BATCH {_ts()}] DETAIL: {e}")
                    break

                # ✅ 先點你紅框那顆，再不行就點第一筆商品列
                ok_sel = await _select_header_then_row_if_needed(page, y_min, mode, log)
                if not ok_sel:
                    _log(log, f"[BATCH {_ts()}] ERROR: 勾選失敗，無法讓按鈕亮起來。")
                    break

                # 點按鈕
                if not await _click_action(page, mode, log):
                    break

                # 送出
                await _confirm_submit_if_any(page, log)

                # 等待 + 刷新
                delay_s = human_interval_sec(max(0.0, float(cfg.interval_sec)))
                _log(log, f"[BATCH {_ts()}] interval wait: {delay_s:.1f}s (base={cfg.interval_sec}s)")
                await page.wait_for_timeout(delay_s * 1000)
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(human_jitter_ms(500) + maybe_extra_think_ms())
                except Exception as e:
                    _log(log, f"[BATCH {_ts()}] WARN: reload failed: {e}")

            _log(log, f"[BATCH {_ts()}] DONE")
        finally:
            try:
                await context.close()
            except Exception:
                pass
            if lock_ok:
                release(profile_dir)
