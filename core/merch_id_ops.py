from __future__ import annotations

import re
import os
import asyncio
import time
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .client_runtime_compat import async_playwright, PwTimeoutError, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

from .profile_lock import try_acquire, release, detect_chrome_profile_in_use, acquire_or_clear
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



@dataclass
class MerchIdOpsConfig:
    """根据『商品编号』批量：下架 -> 删除

    interval_sec: 每批(最多10个编号)完成后等待提醒的秒数，之后 reload 再继续下一批
    headless: 是否无头
    batch_size: 每次搜索最多几个编号（Yahoo 限制 10）
    """

    interval_sec: float = 0.0
    headless: bool = False
    keep_open_on_done: bool = False
    batch_size: int = 10


LIST_URL = (
    "https://tw.bid.yahoo.com/partner/merchandise/list_merchandise"
    "?qType=keyword&itemStatus=shelve&categoryCustomId=all&isApplyShippingRule=all&type=all&sortBy=%2BonTime"
)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg, flush=True)


async def _save_debug_screenshot(page, tag: str, log: Optional[LogFn]) -> None:
    """保存调试截图到项目根目录 debug/ 下（失败不影响主流程）。"""
    try:
        root = Path(__file__).resolve().parent.parent
        out_dir = root / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"merch_id_{tag}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        _log(log, f"[MERCH-ID {_ts()}] debug screenshot saved: {path}")
    except Exception:
        return


async def _locate_manage_search_input(page, log: Optional[LogFn] = None):
    """定位『管理商品頁面』的搜尋輸入框（避免誤命中頂部黃色欄的站內搜尋）。"""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(human_jitter_ms(120) + maybe_extra_think_ms())

    # 優先用 placeholder 的關鍵字鎖定管理頁那條輸入框
    selectors = [
        "input[placeholder*='商品編號']",
        "input[placeholder*='商品编号']",
        "input[placeholder*='請輸入商品編號']",
        "input[placeholder*='请输入商品编号']",
        "input[placeholder*='多筆']",
        "input[placeholder*='多笔']",
        "input[placeholder*='最多']",
    ]

    best = None
    best_bb = None
    best_score = 1e18

    for sel in selectors:
        loc = page.locator(sel)
        try:
            cnt = min(await loc.count(), 8)
        except Exception:
            cnt = 0
        for i in range(cnt):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                bb = await el.bounding_box()
                if not bb:
                    continue
                # 排除頂部 header 區域（站內搜尋通常在很上面）
                if bb["y"] < 120:
                    continue
                # 期望在主內容區靠上位置
                score = abs((bb["y"] + bb["height"] / 2) - 260)
                if score < best_score:
                    best_score = score
                    best = el
                    best_bb = bb
            except Exception:
                continue

        if best is not None:
            break

    # 最後兜底：用所有可見 input 里，挑 placeholder 含『請輸入』且 y>120 的那個
    if best is None:
        loc = page.locator("input[placeholder*='請輸入'], input[placeholder*='请输入']")
        try:
            cnt = min(await loc.count(), 20)
        except Exception:
            cnt = 0
        for i in range(cnt):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                bb = await el.bounding_box()
                if not bb or bb["y"] < 120:
                    continue
                ph = ""
                try:
                    ph = (await el.get_attribute("placeholder")) or ""
                except Exception:
                    ph = ""
                # 仍盡量排除太泛的輸入框
                if ("商品" in ph) or ("編號" in ph) or ("编号" in ph) or ("最多" in ph) or ("多筆" in ph) or ("多笔" in ph):
                    score = abs((bb["y"] + bb["height"] / 2) - 260)
                else:
                    score = abs((bb["y"] + bb["height"] / 2) - 260) + 200
                if score < best_score:
                    best_score = score
                    best = el
                    best_bb = bb
            except Exception:
                continue

    if best is None:
        await _save_debug_screenshot(page, "manage_search_input_not_found", log)
        raise RuntimeError("cannot locate manage-page search input")

    # 確保可見
    await best.wait_for(state="visible", timeout=15000)
    return best, best_bb


def _read_ids_txt(path: Path) -> List[str]:
    out: List[str] = []
    seen = set()
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = (raw or "").strip()
        if not s:
            continue
        # 允许纯数字；若混入空格/逗号/全角逗号，也尽量清洗
        s = s.replace("，", ",")
        # 如果一行里用户手动放了多个，用逗号切开
        parts = [p.strip() for p in s.split(",") if p.strip()]
        for p in parts:
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


def resolve_ids_file(base_dir: Path, account_name: str, profile_id: str) -> Optional[Path]:
    """ids/ 下按 profile_id 优先，其次账号名。兼容：无扩展名 / .txt / .TXT"""
    ids_dir = Path(base_dir) / "ids"
    candidates: List[Path] = []
    for stem in [profile_id, account_name]:
        if not stem:
            continue
        candidates += [
            ids_dir / f"{stem}.txt",
            ids_dir / f"{stem}.TXT",
            ids_dir / stem,
        ]
        # 兼容用户可能写成 stem.任意扩展
        try:
            candidates += list(ids_dir.glob(f"{stem}.*"))
        except Exception:
            pass

    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            continue
    return None


def _chunk(seq: Sequence[str], n: int) -> List[List[str]]:
    if n <= 0:
        n = 10
    out: List[List[str]] = []
    cur: List[str] = []
    for x in seq:
        cur.append(x)
        if len(cur) >= n:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out



async def run_merch_id_batches_ops(
    *,
    base_dir: Path,
    profile_dir: Path,
    chrome_path: str,
    account_name: str,
    profile_id: str,
    batches: List[List[str]],
    cfg: MerchIdOpsConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> None:
    """与 run_merch_id_ops 相同，但批次由外部传入（用于“冻结批次/进度续跑”）。"""

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    if not batches:
        _log(log, f"[MERCH-ID {_ts()}] {account_name}: batches empty -> skip")
        return

    ok, reason = acquire_or_clear(profile_dir, owner=f"merch-id-ops:{account_name}",
                                  log_fn=lambda msg: _log(log, f"[MERCH-ID {_ts()}] {msg}"))
    if not ok:
        _log(log, f"[MERCH-ID {_ts()}] LOCKED: {reason}")
        return

    async with async_playwright() as p:
        context = None
        proxy_kw = {"server": proxy} if proxy else None

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
        except Exception as e:
            _log(log, f"[MERCH-ID {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:160]}")
            return  # finally 会统一 release

        try:
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(LIST_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(human_jitter_ms(450) + maybe_extra_think_ms())

            await _dismiss_swipe_tip(page, log)
            await _set_qtype_merch_id(page, log)
            # 切換到「商品編號」後，等頁面穩定（避免第一批搜索讀到舊 total 被 SAFETY 跳過）
            await page.wait_for_timeout(human_jitter_ms(800) + maybe_extra_think_ms())

            async def _wait_if_paused() -> bool:
                if is_pause and is_pause():
                    _log(log, f"[MERCH-ID {_ts()}] PAUSE requested -> waiting...")
                while is_pause and is_pause():
                    if is_stop and is_stop():
                        _log(log, f"[MERCH-ID {_ts()}] STOP requested (during pause)")
                        return False
                    await asyncio.sleep(0.25)
                try:
                    if page.url and ("/partner/merchandise/list_merchandise" not in page.url):
                        await page.goto(LIST_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
                    await _set_qtype_merch_id(page, log)
                except Exception:
                    pass
                return True

            async def _sleep_interruptible(seconds: float) -> bool:
                end_t = time.time() + float(seconds)
                while time.time() < end_t:
                    if is_stop and is_stop():
                        return False
                    if is_pause and is_pause():
                        ok2 = await _wait_if_paused()
                        if not ok2:
                            return False
                        end_t = time.time() + float(seconds)
                        continue
                    await asyncio.sleep(min(0.5, max(0.0, end_t - time.time())))
                return True

            for i, ids in enumerate(batches, start=1):
                if is_stop and is_stop():
                    _log(log, f"[MERCH-ID {_ts()}] STOP requested")
                    return
                if is_pause and is_pause():
                    okp = await _wait_if_paused()
                    if not okp:
                        return

                _log(log, f"[MERCH-ID {_ts()}] {account_name}: batch {i}/{len(batches)}")
                await _fill_and_search(page, ids, log)

                total = await _parse_total_count(page)
                # SAFETY: 若搜尋未切到『商品編號』導致命中太多結果，禁止批量操作（避免全店被下架/刪除）
                if total is not None and total > len(ids):
                    # NOTE: 这里经常会被“旧 DOM/旧统计数”误触发（例如页面还没刷新完，total 读到上一次的结果）。
                    # 正确行为：retry 后如果 total2 已经合理（<= len(ids)），就继续执行，不要无条件 skip。
                    await _save_debug_screenshot(page, "unsafe_total_too_large", log)
                    _log(log, f"[MERCH-ID {_ts()}] WARN: total={total} > ids_in_batch={len(ids)} (filter maybe failed / stale dom) -> retry once")
                    try:
                        await _set_qtype_merch_id(page, log)
                    except Exception:
                        pass
                    try:
                        await _fill_and_search(page, ids, log)
                    except Exception:
                        pass
                    total2 = await _parse_total_count(page)
                    if total2 is not None and total2 <= len(ids):
                        total = total2
                        _log(log, f"[MERCH-ID {_ts()}] retry OK: total={total2} <= ids_in_batch={len(ids)} -> continue")
                    else:
                        await _save_debug_screenshot(page, "unsafe_total_too_large_after_retry", log)
                        _log(log, f"[MERCH-ID {_ts()}] SAFETY: total={total2} > ids_in_batch={len(ids)} (filter failed) -> skip")
                        continue
                if total is None:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot parse total count, continue")
                elif total <= 0:
                    _log(log, f"[MERCH-ID {_ts()}] not found (0) -> skip")
                    continue

                sel_ok = await _select_some_item(page, log, total_expected=total, require_all=True)
                if not sel_ok:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot select item -> skip batch")
                    continue

                if not await _safe_click_action(page, "下架", log):
                    _log(log, f"[MERCH-ID {_ts()}] WARN: 下架 button not clickable")
                else:
                    await _confirm_submit(page, log)

                try:
                    await page.get_by_text("已下架").first.wait_for(state="visible", timeout=6000)
                except Exception:
                    try:
                        await page.wait_for_timeout(human_jitter_ms(600) + maybe_extra_think_ms())
                    except Exception:
                        pass

                sel_ok2 = await _select_some_item(page, log, total_expected=total, require_all=True)
                if not sel_ok2:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot re-select for delete")
                else:
                    if await _safe_click_action(page, "刪除", log):
                        await _confirm_submit(page, log)
                    else:
                        _log(log, f"[MERCH-ID {_ts()}] WARN: 刪除 button not clickable")

                try:
                    await page.get_by_text(re.compile(r"刪除\s*\d+\s*筆商品成功")).first.wait_for(timeout=5000)
                except Exception:
                    pass

                delay_s = human_interval_sec(max(0.0, float(cfg.interval_sec)))
                _log(log, f"[MERCH-ID {_ts()}] interval wait: {delay_s:.1f}s (base={cfg.interval_sec}s)")
                ok_sleep = await _sleep_interruptible(delay_s)
                if not ok_sleep:
                    _log(log, f"[MERCH-ID {_ts()}] STOP requested (during interval)")
                    return

                try:
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
                    await _set_qtype_merch_id(page, log)
                except Exception as e:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: reload failed: {e}")

            _log(log, f"[MERCH-ID {_ts()}] {account_name}: DONE")
        finally:
            try:
                if context is not None and (not bool(getattr(cfg, 'keep_open_on_done', False))):
                    await context.close()
            except Exception:
                pass
            release(profile_dir)


def load_merch_id_batches(
    *,
    base_dir: Path,
    account_name: str,
    profile_id: str,
    batch_size: int = 10,
    log: Optional[LogFn] = None,
) -> tuple[List[List[str]], Path]:
    """读取 ids/ 下的商品编号文件，并按 batch_size 分批。

    这是“可恢复队列/进度续跑”用的：启动时先冻结批次，后续按批次索引续跑。
    """
    base_dir = Path(base_dir)
    ids_path = resolve_ids_file(base_dir, account_name, profile_id)
    if not ids_path:
        raise FileNotFoundError(f"ids file not found for {account_name}/{profile_id}")
    ids_all = _read_ids_txt(ids_path)
    if not ids_all:
        raise ValueError(f"ids file empty: {ids_path}")
    bs = max(1, int(batch_size or 10))
    batches = _chunk(ids_all, bs)
    _log(log, f"[MERCH-ID {_ts()}] {account_name}: loaded {len(ids_all)} ids from {ids_path.name} (batches={len(batches)})")
    return batches, ids_path


async def _dismiss_swipe_tip(page, log: Optional[LogFn]) -> None:
    """图五那个『左滑查看更多』提示，偶尔会挡住点击。"""
    try:
        tip = page.get_by_text("左滑查看更多")
        if await tip.first.is_visible():
            try:
                await tip.first.click(timeout=800, force=True)
            except Exception:
                pass
            # 勾『不再顯示』也行（尽力）
            try:
                await page.get_by_text("不再顯示").first.click(timeout=800, force=True)
            except Exception:
                pass
            # 最后兜底点一下空白
            try:
                await page.mouse.click(20, 20)
            except Exception:
                pass
            _log(log, f"[MERCH-ID {_ts()}] dismiss tip")
    except Exception:
        return


async def _set_qtype_merch_id(page, log: Optional[LogFn]):
    """
    Yahoo 商品管理頁面的 qType 下拉（商品關鍵字/商品編號）偶爾會因為 UI 文案/結構變動而找不到。
    這個版本更「寬容」：
    1) 先嘗試原生 <select>（若存在）
    2) 再用「搜尋列容器」內的角色/屬性（combobox / aria-haspopup）與「包含匹配」文字（商品關鍵字/商品編號）
    3) 仍用幾何位置確保選到『搜尋輸入框右側』那個下拉
    """
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())
    try:
        await page.evaluate("window.scrollTo(0,0)")
    except Exception:
        pass

    # 0) 如果頁面其實是 <select>，直接 select_option（最穩）
    try:
        sel = page.locator(
            "select:has(option:has-text('商品編號')), "
            "select:has(option:has-text('商品编号'))"
        ).first
        if await sel.count() > 0:
            try:
                await sel.select_option(label="商品編號")
            except Exception:
                await sel.select_option(label="商品编号")
            await page.wait_for_timeout(human_jitter_ms(200) + maybe_extra_think_ms())
            return True
    except Exception:
        pass

    # 1) 找搜尋輸入框（管理商品頁面那個，不是頂部站內搜尋）
    inp, inp_box = await _locate_manage_search_input(page, log)
    if not inp_box:
        inp_box = await inp.bounding_box()
    if not inp_box:
        raise RuntimeError("cannot get search input bbox")

    # 2) 盡量把搜尋列容器縮小：找包含『搜尋』按鈕的最近祖先
    row = inp.locator(
        "xpath=ancestor::*[.//button[normalize-space()='搜尋' or normalize-space()='搜索' or normalize-space()='Search']][1]"
    )
    if await row.count() == 0:
        row = page.locator("body")

    # 3) 找同一行的『搜尋』按鈕（用來限制 x 範圍）
    btn_box = None
    for btn_name in ("搜尋", "搜索", "Search"):
        try:
            btns = row.get_by_role("button", name=btn_name)
            best_btn = None
            best_btn_score = 1e18
            for i in range(min(await btns.count(), 8)):
                b = btns.nth(i)
                bb = await b.bounding_box()
                if not bb:
                    continue
                score = abs((bb["y"] + bb["height"]/2) - (inp_box["y"] + inp_box["height"]/2))
                if score < best_btn_score:
                    best_btn_score = score
                    best_btn = b
                    btn_box = bb
            if btn_box:
                break
        except Exception:
            pass

    # 4) 找 qType 顯示元素：改為「包含匹配」(避免 UI 變成『商品關鍵字／商品編號』或多空白)
    q_candidates = row.locator(
        "xpath=.//*[contains(normalize-space(),'商品關鍵字') "
        "or contains(normalize-space(),'商品关键字') "
        "or contains(normalize-space(),'商品編號') "
        "or contains(normalize-space(),'商品编号')]"
    )

    best = None
    best_score = 1e18
    inp_right = inp_box["x"] + inp_box["width"]
    x_max = (btn_box["x"] if btn_box else (inp_right + 520))

    # 幾何挑選：只挑『輸入框右側、搜尋按鈕左側、y 高度接近』的那個
    for i in range(min(await q_candidates.count(), 60)):
        el = q_candidates.nth(i)
        bb = await el.bounding_box()
        if not bb:
            continue
        cx = bb["x"] + bb["width"]/2
        cy = bb["y"] + bb["height"]/2

        if abs(cy - (inp_box["y"] + inp_box["height"]/2)) > 80:
            continue
        if cx < inp_right - 10:
            continue
        if cx > x_max + 30:
            continue

        score = abs(cy - (inp_box["y"] + inp_box["height"]/2)) * 5 + abs(cx - (inp_right + 80))
        if score < best_score:
            best_score = score
            best = el

    # 5) fallback：在搜尋列容器內找 combobox / aria-haspopup 控制
    if best is None:
        try:
            aria_dd = row.locator(
                "[role='combobox'], [aria-haspopup='listbox'], [aria-haspopup='menu']"
            ).first
            if await aria_dd.count() > 0:
                best = aria_dd
        except Exception:
            pass

    if best is None:
        await _save_debug_screenshot(page, "merch_id_qtype_dropdown_not_found", log)
        raise RuntimeError("cannot find qType dropdown (商品關鍵字/商品編號)")

    # 6) 開 dropdown
    try:
        await best.click(timeout=3500)
    except Exception:
        try:
            await best.locator("xpath=..").click(timeout=3500)
        except Exception:
            await _save_debug_screenshot(page, "merch_id_qtype_dropdown_click_fail", log)
            raise

    await page.wait_for_timeout(human_jitter_ms(200) + maybe_extra_think_ms())

    # 7) 選「商品編號」：優先在可見 listbox/menu 裡點（避免點到頁面其他『商品編號』）
    target_labels = ("商品編號", "商品编号")
    clicked = False
    for label in target_labels:
        # a) role option/menuitem
        for role_name in ("option", "menuitem"):
            try:
                # 先找 popup 容器（若存在）
                popup = page.locator("[role='listbox'], [role='menu']").filter(has_text=label)
                if await popup.count() > 0:
                    opt = popup.first.get_by_role(role_name, name=label)
                    if await opt.count() > 0:
                        await opt.first.click(timeout=3500)
                        clicked = True
                        break
                # 再全局嘗試（某些 UI 沒有標準 role）
                opt2 = page.get_by_text(label).first
                if await opt2.is_visible():
                    await opt2.click(timeout=3500)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break

    if not clicked:
        await _save_debug_screenshot(page, "merch_id_qtype_option_not_found", log)
        raise RuntimeError("cannot find qType option: 商品編號")

    await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())

    # 8) 輕量驗證：找搜尋列附近是否已顯示『商品編號』，不硬 fail（留圖）
    try:
        ok = False
        q_verify = row.locator(
            "xpath=.//*[contains(normalize-space(),'商品編號') or contains(normalize-space(),'商品编号')]"
        )
        for i in range(min(await q_verify.count(), 20)):
            el = q_verify.nth(i)
            bb = await el.bounding_box()
            if not bb:
                continue
            cx = bb["x"] + bb["width"]/2
            cy = bb["y"] + bb["height"]/2
            if abs(cy - (inp_box["y"] + inp_box["height"]/2)) > 90:
                continue
            if cx < inp_right - 10:
                continue
            if btn_box and cx > btn_box["x"] + 30:
                continue
            txt = (await el.inner_text()).strip()
            if "商品編號" in txt or "商品编号" in txt:
                ok = True
                break
        if not ok:
            await _save_debug_screenshot(page, "merch_id_qtype_verify_failed", log)
            _log(log, f"[MERCH-ID {_ts()}] WARN: qType verify not sure (label not 商品編號)")
    except Exception:
        pass

    return True



async def _human_like_type_into(inp, text: str, log: Optional[LogFn]) -> bool:
    """尽量像手动输入：分段输入 + 字符延时 + 轻微停顿。
    不改变功能：若输入失败/不完整，自动回退到 fill() 确保流程继续。
    返回 True 表示已通过“像手动输入”方式完成且内容一致。
    """
    # 这些数值偏保守：既不“瞬间填值”，也避免太慢导致超时
    per_char_min = int(os.getenv("MERCH_HUMAN_TYPE_DELAY_MIN_MS", "45") or 45)
    per_char_max = int(os.getenv("MERCH_HUMAN_TYPE_DELAY_MAX_MS", "110") or 110)
    chunk_min = int(os.getenv("MERCH_HUMAN_TYPE_CHUNK_MIN", "3") or 3)
    chunk_max = int(os.getenv("MERCH_HUMAN_TYPE_CHUNK_MAX", "8") or 8)
    pause_min = int(os.getenv("MERCH_HUMAN_TYPE_PAUSE_MIN_MS", "120") or 120)
    pause_max = int(os.getenv("MERCH_HUMAN_TYPE_PAUSE_MAX_MS", "420") or 420)

    # 兜底，避免配置错误导致异常
    per_char_min = max(0, min(per_char_min, 2000))
    per_char_max = max(per_char_min, min(per_char_max, 2000))
    chunk_min = max(1, min(chunk_min, 64))
    chunk_max = max(chunk_min, min(chunk_max, 128))
    pause_min = max(0, min(pause_min, 6000))
    pause_max = max(pause_min, min(pause_max, 6000))

    try:
        # 分段输入（比逐字 loop 更高效，但仍像人在打字）
        i = 0
        n = len(text)
        while i < n:
            k = random.randint(chunk_min, chunk_max)
            seg = text[i : i + k]
            delay = random.randint(per_char_min, per_char_max)
            await inp.type(seg, delay=delay)
            i += k
            if i < n:
                await asyncio.sleep(random.randint(pause_min, pause_max) / 1000.0)

        # 校验内容是否一致
        try:
            v = await inp.input_value()
        except Exception:
            v = None
        if (v or "") == text:
            return True

        # 有些页面会自动 trim 或插入空格，放宽一次校验（仅去空白）
        if v is not None:
            vn = re.sub(r"\s+", "", v)
            tn = re.sub(r"\s+", "", text)
            if vn == tn:
                return True

        # 不一致则回退 fill（确保功能不受影响）
        await inp.fill(text)
        if log:
            _log(log, f"[MERCH-ID {_ts()}] WARN: human typing mismatch, fallback to fill()")
        return False
    except Exception:
        try:
            await inp.fill(text)
        except Exception:
            pass
        if log:
            _log(log, f"[MERCH-ID {_ts()}] WARN: human typing failed, fallback to fill()")
        return False

async def _fill_and_search(page, ids: List[str], log: Optional[LogFn]) -> None:
    """把最多10个编号填入『管理商品頁面』输入框，然后点『搜尋』。"""
    query = ",".join(ids)

    inp, inp_box = await _locate_manage_search_input(page, log)
    try:
        await inp.scroll_into_view_if_needed()
    except Exception:
        pass

    # 清空再填入
    try:
        await inp.click(timeout=3000)
    except Exception:
        await inp.click(timeout=3000, force=True)

    try:
        await inp.fill("")
    except Exception:
        pass
    try:
        await inp.press("Control+A")
        await inp.press("Backspace")
    except Exception:
        pass
    # 像手动输入：分段打字（失败自动回退 fill，确保不影响功能）

    await _human_like_type_into(inp, query, log)

    if inp_box is None:
        try:
            inp_box = await inp.bounding_box()
        except Exception:
            inp_box = None

    # 找同一個搜尋列（包含『搜尋』按鈕的最近祖先），避免點到頂部黃色欄『搜尋商品』
    row = inp.locator("xpath=ancestor::*[.//button[normalize-space()='搜尋']][1]")
    if await row.count() == 0:
        row = page.locator("body")

    # 在 row 內找『搜尋』按鈕（注意：必須是純『搜尋』，不是『搜尋商品』）
    best_btn = None
    best_score = 1e18
    try:
        btns = row.locator("xpath=.//button[normalize-space()='搜尋']")
        cnt = min(await btns.count(), 10)
        for i in range(cnt):
            b = btns.nth(i)
            bb = await b.bounding_box()
            if not bb:
                continue
            # 避免 header 區域
            if bb["y"] < 120:
                continue
            if inp_box:
                score = abs((bb["y"] + bb["height"]/2) - (inp_box["y"] + inp_box["height"]/2))
            else:
                score = abs((bb["y"] + bb["height"]/2) - 260)
            if score < best_score:
                best_score = score
                best_btn = b
    except Exception:
        best_btn = None

    if best_btn is None:
        # fallback：全局找 name='搜尋' 的 button，再按 y 距离挑
        btns = page.get_by_role("button", name="搜尋")
        cnt = min(await btns.count(), 12)
        for i in range(cnt):
            b = btns.nth(i)
            bb = await b.bounding_box()
            if not bb or bb["y"] < 120:
                continue
            if inp_box:
                score = abs((bb["y"] + bb["height"]/2) - (inp_box["y"] + inp_box["height"]/2))
            else:
                score = abs((bb["y"] + bb["height"]/2) - 260)
            if score < best_score:
                best_score = score
                best_btn = b

    if best_btn is None:
        await _save_debug_screenshot(page, "merch_id_search_button_not_found", log)
        raise RuntimeError("cannot find 搜尋 button in merch manage page")

    await best_btn.click()
    await page.wait_for_timeout(human_jitter_ms(600) + maybe_extra_think_ms())

    # 有时会误导航到首页（tw.bid.yahoo.com/?），这里自动回退一次
    if "partner/merchandise/list_merchandise" not in (page.url or ""):
        _log(log, f"[MERCH-ID {_ts()}] WARN: unexpected navigation after search: {page.url}")
        await _save_debug_screenshot(page, "merch_id_unexpected_nav_after_search", log)
        try:
            await page.go_back()
            await page.wait_for_timeout(human_jitter_ms(800) + maybe_extra_think_ms())
        except Exception:
            pass

    if "partner/merchandise/list_merchandise" not in (page.url or ""):
        raise RuntimeError(f"unexpected navigation after search (now at {page.url})")


async def _fill_ids_and_search(page, ids: List[str], log: Optional[LogFn]) -> None:
    """兼容旧名字：等价于 _fill_and_search。"""
    await _fill_and_search(page, ids, log)


async def _parse_total_count(page) -> Optional[int]:
    """解析『共 X 筆』。"""
    try:
        loc = page.get_by_text(re.compile(r"共\s*\d+\s*筆")).first
        await loc.wait_for(state="visible", timeout=15000)
        txt = (await loc.inner_text()).strip()
        m = re.search(r"共\s*(\d+)\s*筆", txt)
        if m:
            return int(m.group(1))
    except Exception:
        return None
    return None


async def _safe_click_action(page, action_name: str, log: Optional[LogFn]) -> bool:
    """点击『下架/刪除』按钮（要求先勾选商品）。"""
    names = [action_name]
    if action_name in ("刪除", "删除"):
        names = ["刪除", "删除"]

    for n in names:
        try:
            btn = page.get_by_role("button", name=n).first
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=6000, force=True)
                _log(log, f"[MERCH-ID {_ts()}] CLICK ACTION: {n}")
                return True
        except Exception:
            pass
        try:
            btn = page.locator(f"button:has-text('{n}')").first
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=6000, force=True)
                _log(log, f"[MERCH-ID {_ts()}] CLICK ACTION: {n}")
                return True
        except Exception:
            pass
    return False


async def _confirm_submit(page, log: Optional[LogFn]) -> None:
    """点弹窗『送出/確定/確認』。"""
    # 等一下弹窗
    try:
        await page.locator("[role='dialog']").first.wait_for(state="visible", timeout=2500)
    except Exception:
        pass

    for text in ["送出", "確定", "確認", "Submit", "OK"]:
        try:
            btn = page.get_by_role("button", name=text).first
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=6000, force=True)
                _log(log, f"[MERCH-ID {_ts()}] CLICK CONFIRM: {text}")
                await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())
                return
        except Exception:
            pass
        try:
            btn = page.locator(f"button:has-text('{text}')").first
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=6000, force=True)
                _log(log, f"[MERCH-ID {_ts()}] CLICK CONFIRM: {text}")
                await page.wait_for_timeout(human_jitter_ms(250) + maybe_extra_think_ms())
                return
        except Exception:
            pass


async def _select_some_item(page, log: Optional[LogFn], total_expected: Optional[int] = None, require_all: bool = True) -> bool:
    """
    勾选『商品』旁边那颗（表头全选），用于批量下架/删除。

    旧版逻辑会在“表头全选找不到”时退化到只勾第一条，导致『刪除』只删第一件。
    这里改成：优先用“几何/可访问性”找到『表头全选』；只有在 total_expected<=1 或 require_all=False 时才允许退化到第一条。
    """
    # 先判定是否 0 结果（避免一直等 checkbox）
    try:
        no1 = page.get_by_text("目前搜尋條件沒有符合的商品")
        if await no1.count() > 0 and await no1.first.is_visible(timeout=800):
            _log(log, f"[MERCH-ID {_ts()}] no results -> skip")
            return False
    except Exception:
        pass

    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(human_jitter_ms(120) + maybe_extra_think_ms())

    # 读取“共 X 筆”（若没传进来）
    if total_expected is None:
        try:
            total_expected = await _parse_total_count(page)
        except Exception:
            total_expected = None

    # helper: 读取“已選擇 X 筆”
    async def _read_selected_count() -> Optional[int]:
        try:
            loc = page.get_by_text(re.compile(r"已選擇\s*\d+\s*筆|已选择\s*\d+\s*笔")).first
            if await loc.count() == 0:
                return None
            if not await loc.is_visible():
                return None
            txt = (await loc.inner_text()).strip()
            m = re.search(r"已(?:選擇|选择)\s*(\d+)\s*(?:筆|笔)", txt)
            if m:
                return int(m.group(1))
        except Exception:
            return None
        return None

    # 计算列表区域的 y_min：优先用动作按钮（上架/下架/刪除）定位
    y_min = 0.0
    try:
        btns = page.get_by_role("button").filter(
            has_text=re.compile(r"(上架|下架|刪除|删除)")
        )
        best = None
        best_y = 1e18
        for i in range(min(await btns.count(), 12)):
            b = btns.nth(i)
            bb = await b.bounding_box()
            if not bb:
                continue
            # 取更靠上的那一排按钮
            if bb["y"] < best_y:
                best_y = bb["y"]
                best = bb
        if best:
            y_min = best["y"] + best["height"] + 10
    except Exception:
        pass

    # fallback：用搜尋輸入框定位
    if y_min <= 1:
        try:
            inp = page.locator(
                "input[placeholder*='請輸入商品'], input[placeholder*='请输入商品'], "
                "input[placeholder*='請輸入'], input[placeholder*='请输入']"
            ).first
            if await inp.count() > 0:
                await inp.wait_for(state="visible", timeout=8000)
                bb = await inp.bounding_box()
                if bb:
                    y_min = bb["y"] + bb["height"] + 15
        except Exception:
            pass

    # 在列表区域里找“checkbox-like”元素（尽量宽松）
    cand_locator = page.locator(
        "[role='checkbox'], input[type='checkbox'], [aria-checked], "
        "[class*='checkbox'], [data-testid*='checkbox']"
    )

    cands = []
    try:
        n = min(await cand_locator.count(), 120)
        for i in range(n):
            el = cand_locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                bb = await el.bounding_box()
                if not bb:
                    continue
                # 过滤掉太大/太小的，避免误点按钮/容器
                if bb["width"] < 10 or bb["height"] < 10 or bb["width"] > 60 or bb["height"] > 60:
                    continue
                # 只考虑主内容区域左侧那一列（避免误点别处）
                if bb["x"] < 120 or bb["x"] > 650:
                    continue
                # 列表区域：在 y_min 以下
                if bb["y"] + bb["height"] < y_min:
                    continue
                cands.append((bb["y"], bb["x"], el, bb))
            except Exception:
                continue
    except Exception:
        cands = []

    cands.sort(key=lambda t: (t[0], t[1]))

    # 尝试点击最靠上的几个候选（表头全选通常是“左侧列里 y 最小的那个”）
    want_all = (require_all and (total_expected is None or total_expected > 1))
    clicked_any = False

    for idx, (_, _, el, bb) in enumerate(cands[:10]):
        try:
            await el.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await el.click(timeout=1800)
            clicked_any = True
        except Exception:
            # 有些 checkbox 的可点击点在父级
            try:
                await el.locator("xpath=..").click(timeout=1800)
                clicked_any = True
            except Exception:
                continue

        await page.wait_for_timeout(human_jitter_ms(180) + maybe_extra_think_ms())

        sel_cnt = await _read_selected_count()

        # 没有“已選擇”也别急，继续用期望值判断
        if total_expected is not None and total_expected <= 1:
            return True

        if want_all:
            # 期望批量：至少要 >1（或等于 total_expected）
            if sel_cnt is None:
                # 有些 UI 不显示“已選擇”，那就尝试再点下一候选
                continue
            if total_expected is not None:
                if sel_cnt >= total_expected:
                    return True
                # 有时分页只选到当前页，也要确保不是只选 1 个
                if sel_cnt > 1:
                    return True
            else:
                if sel_cnt > 1:
                    return True
            # 选到 1 个，说明点到“第一条”不是表头 -> 继续换候选
            continue
        else:
            # 不强制全选：任何勾选成功都算
            if sel_cnt is None or sel_cnt >= 1:
                return True


    # 额外兜底：有时“表头全选”不是标准 checkbox（匹配不到），但它通常在“第一条 checkbox”正上方一点点。
    # 这里用坐标点几次试一下（只在 require_all=True 且预期>1 时启用）。
    if want_all and cands:
        try:
            row0_bb = cands[0][3]
            cx = row0_bb["x"] + row0_bb["width"] / 2
            for dy in (-40, -28, -18, -10):
                y = row0_bb["y"] + dy
                # 不要点到列表上方太远
                if y < y_min + 2:
                    continue
                try:
                    await page.mouse.click(cx, y)
                except Exception:
                    continue
                await page.wait_for_timeout(human_jitter_ms(180) + maybe_extra_think_ms())
                sel_cnt = await _read_selected_count()
                if sel_cnt is None:
                    continue
                if total_expected is not None:
                    if total_expected <= 1:
                        return True
                    if sel_cnt >= total_expected or sel_cnt > 1:
                        return True
                else:
                    if sel_cnt > 1:
                        return True
        except Exception:
            pass

    # 如果表头没点到，最后才允许退化：只勾第一条（仅当允许或 total_expected<=1）
    if not require_all or (total_expected is not None and total_expected <= 1):
        try:
            # 更像“第一条行内 checkbox”的定位：在候选列表里取 y 更靠下的第一个
            for _, _, el, _ in cands[0:25]:
                try:
                    await el.click(timeout=1500)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

    await _save_debug_screenshot(page, "merch_id_select_all_failed", log)
    if clicked_any:
        _log(log, f"[MERCH-ID {_ts()}] ERROR: cannot select header(all) checkbox")
    else:
        _log(log, f"[MERCH-ID {_ts()}] ERROR: no checkbox candidates")
    return False



async def run_merch_id_step(
    *,
    base_dir: Path,
    profile_dir: Path,
    chrome_path: str,
    account_name: str,
    profile_id: str,
    ids: List[str],
    batch_no: int,
    batch_total: int,
    cfg: MerchIdOpsConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> str:
    """执行 1 个批次（<=10 个商品编号）：下架 -> 删除。

    返回：
      - 'ok' / 'paused' / 'stopped' / 'locked' / 'error'
    """
    base_dir = Path(base_dir)
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    if is_stop and is_stop():
        return "stopped"
    if is_pause and is_pause():
        return "paused"

    ok, reason = acquire_or_clear(profile_dir, owner=f"merch-id-step:{account_name}",
                                  log_fn=lambda msg: _log(log, f"[MERCH-ID {_ts()}] {msg}"))
    if not ok:
        _log(log, f"[MERCH-ID {_ts()}] LOCKED: {reason}")
        return "locked"

    async with async_playwright() as p:
        context = None
        proxy_kw = {"server": proxy} if proxy else None
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
                context = await p.chromium.launch_persistent_context(**_lkw)
            except Exception as e:
                _log(log, f"[MERCH-ID {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:180]}")
                return "error"
            await apply_runtime_normalization_async(context)

            page = context.pages[0] if context.pages else await context.new_page()

            _log(log, f"[MERCH-ID {_ts()}] {account_name}: batch {batch_no}/{batch_total} ids={','.join(ids)}")
            _log(log, f"[MERCH-ID {_ts()}] GOTO: {LIST_URL}")
            await page.goto(LIST_URL, wait_until="domcontentloaded")

            if is_stop and is_stop():
                return "stopped"
            if is_pause and is_pause():
                return "paused"

            await _set_qtype_merch_id(page, log)

            async def _wait_if_paused() -> bool:
                if is_pause and is_pause():
                    _log(log, f"[MERCH-ID {_ts()}] PAUSE requested -> waiting...")
                while is_pause and is_pause():
                    if is_stop and is_stop():
                        _log(log, f"[MERCH-ID {_ts()}] STOP requested (during pause)")
                        return False
                    await asyncio.sleep(0.25)
                # 恢复后尽量回到正确页面/状态
                try:
                    if page.url and ("/partner/merchandise/list_merchandise" not in page.url):
                        await page.goto(LIST_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
                    await _set_qtype_merch_id(page, log)
                except Exception:
                    pass
                return True

            async def _sleep_interruptible(seconds: float) -> bool:
                end_t = time.time() + float(seconds)
                while time.time() < end_t:
                    if is_stop and is_stop():
                        return False
                    if is_pause and is_pause():
                        ok2 = await _wait_if_paused()
                        if not ok2:
                            return False
                        end_t = time.time() + float(seconds)
                        continue
                    await asyncio.sleep(min(0.5, max(0.0, end_t - time.time())))
                return True

            # 搜索
            await _fill_and_search(page, ids, log)

            if is_stop and is_stop():
                return "stopped"
            if is_pause and is_pause():
                return "paused"

            total = await _parse_total_count(page)
            # SAFETY: 若搜尋未切到『商品編號』導致命中太多結果，禁止批量操作（避免全店被下架/刪除）
            if total is not None and total > len(ids):
                # NOTE: 这里经常会被“旧 DOM/旧统计数”误触发；retry 后若 total2 合理应继续执行。
                await _save_debug_screenshot(page, "unsafe_total_too_large", log)
                _log(log, f"[MERCH-ID {_ts()}] WARN: total={total} > ids_in_batch={len(ids)} (filter maybe failed / stale dom) -> retry once")
                try:
                    await _set_qtype_merch_id(page, log)
                except Exception:
                    pass
                try:
                    await _fill_and_search(page, ids, log)
                except Exception:
                    pass
                total2 = await _parse_total_count(page)
                if total2 is not None and total2 <= len(ids):
                    total = total2
                    _log(log, f"[MERCH-ID {_ts()}] retry OK: total={total2} <= ids_in_batch={len(ids)} -> continue")
                else:
                    await _save_debug_screenshot(page, "unsafe_total_too_large_after_retry", log)
                    _log(log, f"[MERCH-ID {_ts()}] SAFETY: total={total2} > ids_in_batch={len(ids)} (filter failed) -> abort this step")
                    return "error"
            if total is not None and total <= 0:
                _log(log, f"[MERCH-ID {_ts()}] {account_name}: no result -> skip")
                return "ok"

            # 1) 下架
            sel_ok = await _select_some_item(page, log, total_expected=total, require_all=True)
            if not sel_ok:
                await _save_debug_screenshot(page, "select_failed", log)
                return "error"

            if is_pause and is_pause():
                return "paused"
            if is_stop and is_stop():
                return "stopped"

            if not await _safe_click_action(page, "下架", log):
                _log(log, f"[MERCH-ID {_ts()}] WARN: 下架 button not clickable")
            else:
                await _confirm_submit(page, log)

            # 等待 UI 更新到『已下架』（不给太久）
            try:
                await page.get_by_text("已下架").first.wait_for(state="visible", timeout=6000)
            except Exception:
                try:
                    await page.wait_for_timeout(human_jitter_ms(600) + maybe_extra_think_ms())
                except Exception:
                    pass

            # 2) 删除：动作后会清空勾选 -> 重新勾选一次
            sel_ok2 = await _select_some_item(page, log, total_expected=total, require_all=True)
            if not sel_ok2:
                _log(log, f"[MERCH-ID {_ts()}] WARN: cannot re-select for delete")
            else:
                if await _safe_click_action(page, "刪除", log):
                    await _confirm_submit(page, log)
                else:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: 刪除 button not clickable")

            try:
                await page.get_by_text(re.compile(r"刪除\s*\d+\s*筆商品成功")).first.wait_for(timeout=5000)
            except Exception:
                pass

            return "ok"
        finally:
            try:
                if context is not None:
                    if not bool(getattr(cfg, 'keep_open_on_done', False)):
                        await context.close()
            except Exception:
                pass
            release(profile_dir)


async def run_merch_id_ops(
    *,
    base_dir: Path,
    profile_dir: Path,
    chrome_path: str,
    account_name: str,
    profile_id: str,
    cfg: MerchIdOpsConfig,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
) -> None:
    """核心：读取 ids/<账号名>.txt，每批(<=10)搜索 -> 下架 -> 删除 -> 等待/刷新 -> 下一批。

    - 没搜到（共0笔）会跳过，不影响后续
    - 文件不存在会直接跳过该账号
    """

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    ids_path = resolve_ids_file(base_dir, account_name=account_name, profile_id=profile_id)
    if not ids_path:
        _log(log, f"[MERCH-ID {_ts()}] {account_name}: ids file not found under ids/ (try {profile_id}.txt or {account_name}.txt)")
        return

    ids_all = _read_ids_txt(ids_path)
    if not ids_all:
        _log(log, f"[MERCH-ID {_ts()}] {account_name}: ids file empty -> skip")
        return

    batches = _chunk(ids_all, max(1, int(cfg.batch_size or 10)))
    _log(log, f"[MERCH-ID {_ts()}] {account_name}: loaded {len(ids_all)} ids from {ids_path.name} (batches={len(batches)})")

    # lock profile (avoid monitor/batch conflict)
    ok, reason = acquire_or_clear(profile_dir, owner="merch-id",
                                  log_fn=lambda msg: _log(log, f"[MERCH-ID {_ts()}] {msg}"))
    if not ok:
        _log(log, f"[MERCH-ID {_ts()}] LOCKED: {reason}")
        return

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
                context = await p.chromium.launch_persistent_context(**_lkw)
            await apply_runtime_normalization_async(context)
        except Exception as e:
            _log(log, f"[MERCH-ID {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:160]}")
            return  # finally 会统一 release
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # goto list page
            cur = page.url or ""
            if "/partner/merchandise/list_merchandise" not in cur:
                _log(log, f"[MERCH-ID {_ts()}] GOTO: {LIST_URL}")
                await page.goto(LIST_URL, wait_until="domcontentloaded")
            else:
                # ensure page is fresh
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass

            # ensure qType is 商品编号
            await _set_qtype_merch_id(page, log)

            async def _wait_if_paused() -> bool:
                if is_pause and is_pause():
                    _log(log, f"[MERCH-ID {_ts()}] PAUSE requested -> waiting...")
                while is_pause and is_pause():
                    if is_stop and is_stop():
                        _log(log, f"[MERCH-ID {_ts()}] STOP requested (during pause)")
                        return False
                    await asyncio.sleep(0.25)
                try:
                    if page.url and ("/partner/merchandise/list_merchandise" not in page.url):
                        await page.goto(LIST_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
                    await _set_qtype_merch_id(page, log)
                except Exception:
                    pass
                return True

            async def _sleep_interruptible(seconds: float) -> bool:
                end_t = time.time() + float(seconds)
                while time.time() < end_t:
                    if is_stop and is_stop():
                        return False
                    if is_pause and is_pause():
                        ok2 = await _wait_if_paused()
                        if not ok2:
                            return False
                        end_t = time.time() + float(seconds)
                        continue
                    await asyncio.sleep(min(0.5, max(0.0, end_t - time.time())))
                return True

            for i, ids in enumerate(batches, start=1):
                if is_stop and is_stop():
                    _log(log, f"[MERCH-ID {_ts()}] STOP requested")
                    return
                if is_pause and is_pause():
                    okp = await _wait_if_paused()
                    if not okp:
                        return

                _log(log, f"[MERCH-ID {_ts()}] {account_name}: batch {i}/{len(batches)}")
                await _fill_and_search(page, ids, log)

                total = await _parse_total_count(page)
                # SAFETY: 若搜尋未切到『商品編號』導致命中太多結果，禁止批量操作（避免全店被下架/刪除）
                if total is not None and total > len(ids):
                    # NOTE: 这里经常会被“旧 DOM/旧统计数”误触发；retry 后若 total2 合理应继续执行。
                    await _save_debug_screenshot(page, "unsafe_total_too_large", log)
                    _log(log, f"[MERCH-ID {_ts()}] WARN: total={total} > ids_in_batch={len(ids)} (filter maybe failed / stale dom) -> retry once")
                    try:
                        await _set_qtype_merch_id(page, log)
                    except Exception:
                        pass
                    try:
                        await _fill_and_search(page, ids, log)
                    except Exception:
                        pass
                    total2 = await _parse_total_count(page)
                    if total2 is not None and total2 <= len(ids):
                        total = total2
                        _log(log, f"[MERCH-ID {_ts()}] retry OK: total={total2} <= ids_in_batch={len(ids)} -> continue")
                    else:
                        await _save_debug_screenshot(page, "unsafe_total_too_large_after_retry", log)
                        _log(log, f"[MERCH-ID {_ts()}] SAFETY: total={total2} > ids_in_batch={len(ids)} (filter failed) -> skip")
                        continue
                if total is None:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot parse total count, continue")
                elif total <= 0:
                    _log(log, f"[MERCH-ID {_ts()}] not found (0) -> skip")
                    continue

                # 1) 下架
                sel_ok = await _select_some_item(page, log, total_expected=total, require_all=True)
                if not sel_ok:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot select item -> skip batch")
                    continue

                if not await _safe_click_action(page, "下架", log):
                    _log(log, f"[MERCH-ID {_ts()}] WARN: 下架 button not clickable")
                else:
                    await _confirm_submit(page, log)

                # 等 UI 更新到『已下架』（不给太久）
                try:
                    await page.get_by_text("已下架").first.wait_for(state="visible", timeout=6000)
                except Exception:
                    await page.wait_for_timeout(human_jitter_ms(600) + maybe_extra_think_ms())

                # 2) 删除（图八）
                # 重新勾选一次（通常动作后会清空勾选）
                sel_ok2 = await _select_some_item(page, log, total_expected=total, require_all=True)
                if not sel_ok2:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: cannot re-select for delete")
                else:
                    if await _safe_click_action(page, "刪除", log):
                        await _confirm_submit(page, log)
                    else:
                        _log(log, f"[MERCH-ID {_ts()}] WARN: 刪除 button not clickable")

                # 等待 toast（尽力，不阻塞）
                try:
                    await page.get_by_text(re.compile(r"刪除\s*\d+\s*筆商品成功")).first.wait_for(timeout=5000)
                except Exception:
                    pass

                # interval + reload -> next batch
                delay_s = human_interval_sec(max(0.0, float(cfg.interval_sec)))
                _log(log, f"[MERCH-ID {_ts()}] interval wait: {delay_s:.1f}s (base={cfg.interval_sec}s)")
                ok_sleep = await _sleep_interruptible(delay_s)
                if not ok_sleep:
                    _log(log, f"[MERCH-ID {_ts()}] STOP requested (during interval)")
                    return
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(human_jitter_ms(350) + maybe_extra_think_ms())
                    await _set_qtype_merch_id(page, log)
                except Exception as e:
                    _log(log, f"[MERCH-ID {_ts()}] WARN: reload failed: {e}")

            _log(log, f"[MERCH-ID {_ts()}] {account_name}: DONE")
        finally:
            try:
                await context.close()
            except Exception:
                pass
            release(profile_dir)
