from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import webbrowser
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests

from core.accounts import load_settings, save_settings, load_accounts
from core.profile_lock import try_acquire, release, detect_chrome_profile_in_use

BASE_DIR = Path(__file__).resolve().parent.parent

YAHOO_ORDER_LIST_URL = "https://tw.bid.yahoo.com/partner/order/list"





# 云端业绩核对（Cloudflare Worker + D1）
PERF_CLOUD_SERVER = "https://product-query.<PHONE_REDACTED>.workers.dev"
PERF_BUILTIN_TOKEN = "<PERF_TOKEN_REDACTED>"
# ------------------------ fallback logger ------------------------
# Some call-sites (agent wrapper / exception paths) may reference a module-level `_log`.
# Keep it extremely simple and safe.
def _log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:
        pass

# ------------------------ utils ------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _profile_dir(profile_id: str) -> Path:
    return (BASE_DIR / "profiles" / (profile_id or "")).resolve()


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if not s:
            return default
        s = s.replace(",", "")
        return int(float(s))
    except Exception:
        return default


def _pick_header_map(headers: List[str]) -> Dict[str, int]:
    """Try map required fields by header names (Traditional/ Simplified)."""
    norm = [str(h or "").strip() for h in headers]

    def find(*cands: str) -> int:
        for c in cands:
            for i, h in enumerate(norm):
                if h == c:
                    return i
        # fuzzy contains
        for c in cands:
            for i, h in enumerate(norm):
                if c in h and h:
                    return i
        return -1

    return {
        "account": find("账号", "帳號", "賬號"),
        "owner": find("所属人", "所屬人", "归属人", "歸屬人"),
        "order_no": find("订单编号", "訂單編號", "訂單編碼", "订单編號", "系统订单编号", "系統訂單編號"),
        "amount_twd": find("商品总额（台币）", "商品總額（台幣）", "商品總額(台幣)", "商品總額（台幣）", "商品总额(台币)", "商品總額(台幣)", "商品总额", "商品總額"),
    }


def _http_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            return False, "响应不是JSON对象", {}
        if data.get("ok") is False:
            return False, str(data.get("error") or "server error"), data
        return True, "", data
    except Exception as e:
        return False, str(e), {}



PERF_CHECK_FEATURE_VERSION = "v10_patch_fix16_headless_defaults_2026-01-23"
try:
    print(f"[PERF] performance_check_feature loaded: {__file__} version={PERF_CHECK_FEATURE_VERSION}")
except Exception:
    pass

def _http_get_json(url: str, params: Dict[str, Any] | None = None, timeout: int = 20):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", {}
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            return False, "响应不是JSON对象", {}
        if data.get("ok") is False:
            return False, str(data.get("error") or "server error"), data
        return True, "", data
    except Exception as e:
        return False, str(e), {}


# ------------------------ yahoo extraction ------------------------


def _remove_zero_width(s: str) -> str:
    return s.replace("\u200b", "").replace("\ufeff", "")


def _parse_modal_text(txt: str) -> Dict[str, Any]:
    txt = _remove_zero_width(txt or "")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]

    # status badge (top-right)
    badge_candidates = [
        "已退款", "退款", "已取消", "已取貨", "已取货", "已取件", "已完成訂單", "已完成订单", "已給評", "已给评",
    ]
    badge = ""
    for k in badge_candidates:
        if k in txt:
            badge = k
            break

    # pay/refund line (your red box text)
    pay_line = ""
    for l in lines:
        if any(x in l for x in ["已付款", "保管", "退款", "已退款", "结\u675f保管", "結束保管", "相殺", "相杀"]):
            pay_line = l
            break

    # order amount
    amount = 0
    m = re.search(r"訂單金額\s*[:：]?\s*\$\s*([0-9,]+)", txt, flags=re.S)
    if not m:
        m = re.search(r"订单金额\s*[:：]?\s*\$\s*([0-9,]+)", txt, flags=re.S)
    if m:
        amount = _safe_int(m.group(1), 0)
    else:
        # fallback: last $xxx in dialog
        ms = re.findall(r"\$\s*([0-9,]+)", txt)
        if ms:
            amount = _safe_int(ms[-1], 0)

    return {
        "badge": badge,
        "pay_line": pay_line,
        "amount": amount,
        "raw_lines_preview": "\n".join(lines[:40]),
    }


async def _fetch_perf_one_order_async(
    *,
    chrome_path: str,
    profile_dir: Path,
    headless: bool,
    proxy: str,
    order_no: str,
    slow_mo_ms: int,
    timeout_ms: int,
) -> Dict[str, Any]:
    from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

    proxy = (proxy or "").strip()
    proxy_kw = {"server": proxy} if proxy else None

    async with async_playwright() as p:
        ctx = None
        try:
            _hl = bool(headless)
            _args = get_launch_args(headless=_hl)
            if _hl:
                _args.append("--window-size=1600,960")
            else:
                _args.append("--window-size=1600,960")
            _perf_lkw = dict(
                user_data_dir=str(profile_dir),
                executable_path=chrome_path,
                headless=False,
                proxy=proxy_kw,
                slow_mo=int(slow_mo_ms or 0),
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=_hl),
            )
            _perf_lkw["no_viewport"] = True   # Patchright: headless=False 下 viewport 会 getWindowForTarget
            try:
                ctx = await p.chromium.launch_persistent_context(**_perf_lkw)
            except TypeError:
                _perf_lkw.pop("no_viewport", None)
                ctx = await p.chromium.launch_persistent_context(**_perf_lkw)
            await apply_runtime_normalization_async(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            await page.goto(YAHOO_ORDER_LIST_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_timeout(800)
            except Exception:
                pass

            # search input (top of order list)
            q = str(order_no).strip()
            if not q:
                return {"ok": False, "error": "订单号为空"}

            # 订单列表的搜索框（不是顶部全站搜索框）
            async def _find_order_list_search_box():
                candidates = [
                    "form#search-form input[name='queryContext']",
                    "input[name='queryContext']",
                    "input[placeholder*='請輸入訂單編號']",
                    "input[placeholder*='请输入订单编号']",
                    "input[placeholder*='訂單編號']",
                    "input[placeholder*='订单编号']",
                    "input[placeholder*='請輸入商品關鍵字']",
                    "input[placeholder*='请输入商品关键字']",
                    "input[placeholder*='商品關鍵字']",
                    "input[placeholder*='商品关键字']",
                ]
                for sel in candidates:
                    loc = page.locator(sel).first
                    try:
                        if await loc.count() > 0 and await loc.is_visible():
                            return loc
                    except Exception:
                        pass

                # 兜底：从「銷售訂單管理」标题往后找第一个输入框
                fallback = page.locator(
                    "xpath=//*[contains(normalize-space(.),'銷售訂單管理') or contains(normalize-space(.),'销售订单管理')]/following::input[1]"
                ).first
                try:
                    if await fallback.count() > 0 and await fallback.is_visible():
                        return fallback
                except Exception:
                    pass
                return None

            box = await _find_order_list_search_box()
            if box is None:
                return {"ok": False, "error": "找不到订单列表搜尋框（请确认已登入且在【銷售訂單管理】页）"}
            async def _dbg_async(tag: str, *args, error=None, **_kw):
                # Debug screenshots / debug spam are disabled by design (privacy & less noise).
                return

            def _dbg(tag: str, *args, **kwargs):
                # no-op
                return

            async def _click_basic_options_toggle():
                # 这个按钮是你圈起来的「三条滑杆」图标（基本选项/筛选）
                await box.scroll_into_view_if_needed()
                bb = await box.bounding_box()
                if not bb:
                    raise RuntimeError("无法获取订单列表搜尋框位置")

                cx = bb["x"] + bb["width"] / 2
                cy = bb["y"] + bb["height"] / 2
                x_right = bb["x"] + bb["width"]

                # 收集所有 button / role=button，然后按几何位置筛选出「搜尋栏右侧」那一排按钮
                btns = page.locator("button, [role='button']")
                n = await btns.count()

                near = []
                for i in range(min(n, 260)):
                    el = btns.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        b = await el.bounding_box()
                        if not b:
                            continue
                        ex = b["x"] + b["width"] / 2
                        ey = b["y"] + b["height"] / 2
                        if abs(ey - cy) < 65 and (ex > x_right - 10) and (ex < x_right + 520):
                            near.append((b, el))
                    except Exception:
                        continue

                if not near:
                    # 兜底：尝试用 aria-label/title 的关键字去抓
                    alt = page.locator(
                        "xpath=//*[self::button or @role='button'][contains(@aria-label,'篩') or contains(@aria-label,'筛') or contains(@title,'篩') or contains(@title,'筛') or contains(@aria-label,'基本') or contains(@title,'基本')]"
                    ).first
                    if await alt.count() > 0 and await alt.is_visible():
                        await alt.click()
                        return
                    raise RuntimeError("找不到『基本選項/篩選』按鈕（页面可能还没渲染完）")

                # 识别「搜尋」按钮（有文字的那个）。如果识别不到，就用“最宽的按钮”当作搜尋按钮。
                search_el = None
                search_bb = None
                max_w = -1
                widest = None
                widest_bb = None
                for b, el in near:
                    if b["width"] > max_w:
                        max_w = b["width"]
                        widest = el
                        widest_bb = b
                    try:
                        txt = (await el.inner_text()).strip()
                    except Exception:
                        txt = ""
                    if ("搜尋" in txt) or ("搜索" in txt):
                        search_el = el
                        search_bb = b
                        break

                if search_el is None:
                    search_el = widest
                    search_bb = widest_bb

                # 滑杆按钮通常在「搜尋」按钮左边最近的位置
                sx = search_bb["x"]
                toggle_el = None
                toggle_gap = 10**9
                for b, el in near:
                    if b["x"] < sx:
                        gap = sx - b["x"]
                        if gap < toggle_gap:
                            toggle_gap = gap
                            toggle_el = el

                if toggle_el is None:
                    # 实在找不到就点一下搜尋按钮左侧 80px 位置（最后兜底）
                    await page.mouse.click(sx - 80, cy)
                    return

                await toggle_el.click()

            async def _open_basic_options_panel():
                title = page.locator(
                    "xpath=//*[contains(normalize-space(.),'基本選項') or contains(normalize-space(.),'基本选项')]"
                ).first
                if await title.count() > 0 and await title.is_visible():
                    return

                await _click_basic_options_toggle()
                await title.wait_for(state="visible", timeout=timeout_ms)

            async def _is_search_type_order_no() -> bool:
                # 1) select 的值（最可靠）
                try:
                    sel_all = page.locator("select[name='queryType']")
                    if await sel_all.count():
                        val = await sel_all.first.evaluate("el => el.value")
                        if (val or "").strip() == "orderIds":
                            return True
                except Exception:
                    pass

                # 2) 输入框 placeholder
                try:
                    inp_all = page.locator("form#search-form input[name='queryContext'], input[name='queryContext']")
                    if await inp_all.count():
                        ph = (await inp_all.first.get_attribute("placeholder")) or ""
                        if ("訂單編號" in ph) or ("订单编号" in ph):
                            return True
                except Exception:
                    pass

                return False

            async def _ensure_search_type_order_no():
                # 已经是订单编号模式就别折腾（避免误报）
                if await _is_search_type_order_no():
                    _dbg("perf_switch_search_type_already_orderids")
                    return

                # A) 先尝试直接 select_option（不依赖面板是否展开）
                try:
                    sel_all = page.locator("select[name='queryType']")
                    if await sel_all.count():
                        try:
                            await sel_all.first.select_option("orderIds")
                        except Exception:
                            # label 兜底
                            await sel_all.first.select_option(label="訂單編號")
                except Exception:
                    pass

                if await _is_search_type_order_no():
                    _dbg("perf_switch_search_type_ok_direct")
                    return

                # B) 打开 tune 面板再选（更贴近真实操作）
                try:
                    tune_btn = page.locator(
                        "button[class*='search-field__tune__button'], button.search-field__tune__button__x3JLR, button[aria-label='tune']"
                    ).first
                    if await tune_btn.count():
                        await tune_btn.click()
                except Exception:
                    pass

                # 等待 select 出现/可用后再选
                try:
                    sel_all = page.locator("select[name='queryType']")
                    if await sel_all.count():
                        try:
                            await sel_all.first.wait_for(state="visible", timeout=2000)
                        except Exception:
                            # 有些情况下 select 在 DOM 但不 visible，后面用 evaluate 强制触发
                            pass

                        try:
                            await sel_all.first.select_option("orderIds")
                        except Exception:
                            try:
                                await sel_all.first.select_option(label="訂單編號")
                            except Exception:
                                # C) JS 强制（最后兜底）
                                await page.evaluate(
                                    """() => {
                                        const el = document.querySelector('select[name="queryType"]');
                                        if (!el) return false;
                                        el.value = 'orderIds';
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        return true;
                                    }"""
                                )
                except Exception as e:
                    _dbg("perf_switch_search_type_select_failed", error=e)
                    # 继续往下走，再做一次最终判断

                # 关掉面板，避免挡住后续点击
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

                # 最终确认（给一点点时间让页面反应）
                for _ in range(12):
                    if await _is_search_type_order_no():
                        _dbg("perf_switch_search_type_ok")
                        return
                    try:
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass

                raise RuntimeError("切换失败：无法确认已进入『訂單編號』模式")
            try:
                await _ensure_search_type_order_no()
            except Exception as e:
                # 如果其实已经是订单编号模式，就继续（避免误判）
                if await _is_search_type_order_no():
                    _log(f"[PERF] 警告：切换报错但已处于『訂單編號』模式，继续。err={e}")
                else:
                    _dbg("switch_search_type_failed", error=e)
                    return {"ok": False, "error": f"无法切换到『訂單編號』搜索：{e}"}

            # 3) 输入订单号并搜索（必须确保结果卡片匹配该订单号，否则宁可失败也不要误读默认订单）
            await box.click()
            await box.fill("")
            await box.fill(q)
            # 有些情况下需要 Enter + 点击『搜尋』双保险
            try:
                await box.press("Enter")
            except Exception:
                pass

            # 等待结果出现：优先等 data-order-id 或 copy button；若没出现，再点一次『搜尋/搜索』
            loc_by_orderid = page.locator(f'[data-order-id="{order_no}"]').first
            loc_by_copybtn = page.locator(f'yec-copy-button[copycontent="{order_no}"]').first

            async def _wait_result_once(timeout_ms: int) -> bool:
                try:
                    await loc_by_orderid.wait_for(state="visible", timeout=timeout_ms)
                    return True
                except Exception:
                    pass
                try:
                    await loc_by_copybtn.wait_for(state="visible", timeout=timeout_ms)
                    return True
                except Exception:
                    return False

            ok_found = await _wait_result_once(6500)
            if not ok_found:
                try:
                    btn = page.locator("button:has-text('搜尋'), button:has-text('搜索'), button:has-text('Search')").first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                except Exception:
                    pass
                ok_found = await _wait_result_once(6500)

            if not ok_found:
                _log(f"[PERF] 失败：搜索后仍找不到订单 {order_no}（页面可能没刷新/账号无权限/订单不存在）")
                return {
                    "ok": False,
                    "order_no": str(order_no),
                    "amount": 0,
                    "badge": "",
                    "pay_line": "",
                    "status_paid": "",
                    "status_ship": "",
                    "err": "order_not_found",
                }

# 订单卡片定位：必须命中指定订单号，否则宁可失败也不要误读第一页默认订单
            qid = str(order_no).strip()
            card = None
            found_order_no = ""

            async def _pick_card(base):
                try:
                    if await base.count() <= 0:
                        return None
                except Exception:
                    return None
                # 优先用 data-order-index 的外层卡片（包含 actions 区域）
                try:
                    c = base.locator('xpath=ancestor::div[@data-order-index][1]').first
                    if await c.count() > 0:
                        return c
                except Exception:
                    pass
                # 再兜底用 order-cell 外层
                try:
                    c = base.locator('xpath=ancestor::div[contains(@class,"order-cell")][1]').first
                    if await c.count() > 0:
                        return c
                except Exception:
                    return None
                return None

            # 1) 用 data-order-id（最稳）
            card = await _pick_card(page.locator(f'[data-order-id="{qid}"]').first)
            if card:
                found_order_no = qid

            # 2) 用 copy button（copycontent=订单号）
            if not card:
                card = await _pick_card(page.locator(f'yec-copy-button[copycontent="{qid}"]').first)
                if card:
                    found_order_no = qid

            # 3) 文本“編號/编号：xxxx”（兜底）
            if not card:
                loc_marker = page.locator(
                    f'xpath=//*[contains(., "{qid}") and (contains(normalize-space(.), "編號") or contains(normalize-space(.), "编号"))]'
                ).first
                card = await _pick_card(loc_marker)
                if card:
                    found_order_no = qid

            if not card:
                raise RuntimeError(f"找不到订单卡片（order_no={qid}）")

            # 保险：确认这张卡片确实包含目标订单号（避免误读到买家 Y 开头编号等）
            try:
                loc_oid = card.locator(f'[data-order-id="{qid}"]').first
                if await loc_oid.count() == 0:
                    real_oid = ""
                    try:
                        real_oid = (await card.locator('[data-order-id]').first.get_attribute("data-order-id")) or ""
                    except Exception:
                        pass
                    # 进一步兜底：看看是否存在 copy 按钮的 copycontent
                    try:
                        if not real_oid:
                            real_oid = (await card.locator('yec-copy-button[copycontent]').first.get_attribute("copycontent")) or ""
                    except Exception:
                        pass
                    raise RuntimeError(f"订单卡片不匹配：期望 {qid} 实际 {real_oid or '?'}")
            except Exception:
                # 校验失败也不阻断解析（有些页面结构可能变化）；后续仍会尽力解析金额/状态
                pass

            found_order_no = qid

            # 让卡片进视口，等一下渲染（否则 actions 区域可能还没挂载）
            try:
                await card.scroll_into_view_if_needed()
            except Exception:
                pass
            await page.wait_for_timeout(250)

            # 等 actions 区域渲染出来（金额/底部信息都在这里）
            try:
                await card.locator('p[class*="actions_total"]').first.wait_for(timeout=2500)
            except Exception:
                pass


            # 取一份 card_text 做兜底解析
            card_text = ""
            try:
                await card.wait_for(timeout=7000)
                card_text = ((await card.text_content()) or "").strip()
            except Exception:
                card_text = ""

# 从卡片 DOM 读取四个关键信息
            paid_status = ""
            ship_status = ""
            badge = ""
            pay_line = ""
            amt = 0

            def _parse_amount_from_text(s: str) -> int:
                """从卡片文本里解析订单金额（TWD）。兼容：有/无“訂單金額”字样、$ 是 ::before、全角数字/逗号、各种空白分隔。"""
                if not s:
                    return 0

                s = str(s)

                # 归一化：全角数字/逗号、NBSP 等
                trans = {
                    ord("０"): "0", ord("１"): "1", ord("２"): "2", ord("３"): "3", ord("４"): "4",
                    ord("５"): "5", ord("６"): "6", ord("７"): "7", ord("８"): "8", ord("９"): "9",
                    ord("，"): ",", ord("﹐"): ",", ord("､"): ",",
                    0x00A0: " ", 0x202F: " ", 0x2009: " ",
                }
                s = s.translate(trans)
                s = re.sub(r"\s+", " ", s).strip()

                def _to_int(raw: str) -> int:
                    digits = re.sub(r"[^0-9]", "", raw or "")
                    if not digits:
                        return 0
                    # 过滤订单号/流水号（通常 10+ 位）
                    if len(digits) > 9:
                        return 0
                    v = _safe_int(digits, 0)
                    return v if 0 <= v <= 100_000_000 else 0

                # 1) 优先：订单金额附近
                for pat in [
                    r"(?:訂單金額|订单金额)\s*[:：]?\s*[$￥¥]?\s*([0-9][0-9, ]{0,})",
                    r"(?:訂單金額|订单金额)[^0-9]{0,24}([0-9][0-9, ]{0,})",
                ]:
                    m = re.search(pat, s)
                    if m:
                        v = _to_int(m.group(1))
                        if v:
                            return v

                # 2) 次优：直接金额（有些页面 $ 在伪元素，不在 innerText）
                for pat in [
                    r"[$￥¥]\s*([0-9][0-9, ]{0,})",
                    r"^\s*([0-9][0-9, ]{0,})\s*$",
                ]:
                    m = re.search(pat, s)
                    if m:
                        v = _to_int(m.group(1))
                        if v:
                            return v

                # 3) 兜底：抓所有数字片段，过滤并取最大
                nums = []
                for mm in re.finditer(r"([0-9][0-9, ]{0,})", s):
                    v = _to_int(mm.group(1))
                    if v:
                        nums.append(v)

                return max(nums) if nums else 0
            try:
                if card is not None:
                    # 状态（右上角）- 用更稳定的 class 片段
                    loc_paid = card.locator('p[class*="orderstatus"] span[class*="orderstatus__shipping"]').first
                    loc_ship = card.locator('p[class*="orderstatus"] span[class*="orderstatus__status"]').first
                    if await loc_paid.count() > 0:
                        paid_status = ((await loc_paid.inner_text()) or "").strip()
                    if await loc_ship.count() > 0:
                        ship_status = ((await loc_ship.inner_text()) or "").strip()
                    paid_s = (paid_status or "").strip()
                    ship_s = (ship_status or "").strip()
                    # badge 不要拼太多：优先用“出货/取货”等物流状态；若出现“已退款”，以退款为准
                    if paid_s and ("退款" in paid_s):
                        badge = paid_s
                    elif ship_s or paid_s:
                        badge = (ship_s or paid_s)

                    # 金额（右侧“訂單金額”）
                    # 说明：Yahoo 拍卖这个区域的 class 常变（CSS module），
                    # 直接用「包含文字」抓 <p> 最稳，innerText 会同时包含“訂單金額”和金额。
                    amt_text = ""

                    # 1) 最稳：包含“訂單金額/订单金额”的 <p>
                    loc_amt_p = card.locator('p:has-text("訂單金額"), p:has-text("订单金额")').first
                    if await loc_amt_p.count() > 0:
                        try:
                            amt_text = ((await loc_amt_p.text_content()) or "").strip()
                        except Exception:
                            amt_text = ""

                    # 2) 兜底：按 class 片段抓到 actions_total 的 <p>
                    if not amt_text:
                        loc_amt_p2 = card.locator('p[class*="actions_total"], p[class*="actions__total"]').first
                        if await loc_amt_p2.count() > 0:
                            try:
                                amt_text = ((await loc_amt_p2.text_content()) or "").strip()
                            except Exception:
                                amt_text = ""

                    # 3) 兜底：抓真正金额的 span（避免拿到“訂單金額：”那个 span）
                    if not amt_text:
                        loc_amt_span = card.locator('span[class*="total_span"], span[class*="actions_total_span"], span[class*="actions__total__span"]').first
                        if await loc_amt_span.count() > 0:
                            amt_text = ((await loc_amt_span.text_content()) or "").strip()

                    # 4) 兜底：actions_total 里的最后一个 span 往往就是金额
                    if not amt_text:
                        loc_amt_span2 = card.locator('p[class*="actions_total"] span').last
                        loc_amt_span3 = card.locator('span[class*="actions_total_span"]').last
                        if await loc_amt_span2.count() > 0:
                            amt_text = ((await loc_amt_span2.text_content()) or "").strip()

                    # 5) 最后：从整卡片文本解析
                    if not amt_text and card_text:
                        amt_text = card_text

                    amt = _parse_amount_from_text(amt_text)
                    if amt == 0:
                        _log(f"[PERF][DBG] 金额=0 | amt_text={amt_text[:120]!r} | card_text_head={card_text[:120]!r}")

                    # 底部信息（已出货/已付款等描述）
                    pay_line = (pay_line or '').strip()
                    if not pay_line:
                        # 先抓最精确的 span（避免整卡片 text 混入 icon/隐藏文字）
                        for sel in [
                            'span[class*="actions_infos_extra_span"]',
                            'span[class*="actions__infos__extra__span"]',
                            'span[class*="extra_span"]',
                            'p[class*="actions_infos_extra"]',
                            'p[class*="actions__infos__extra"]',
                            'p[class*="infos_extra"]',
                            'p[class*="infos__extra"]',
                        ]:
                            loc_extra = card.locator(sel).first
                            if await loc_extra.count() > 0:
                                t = ((await loc_extra.inner_text()) or '').strip()
                                if t:
                                    pay_line = t
                                    break

                    # fallback：补齐 badge / 金额 / pay_line
                    if (not badge or not amt or not pay_line) and card_text:
                        parsed_fb = _parse_modal_text(card_text) or {}
                        if not isinstance(parsed_fb, dict):
                            parsed_fb = {}
                        if not badge:
                            badge = (str(parsed_fb.get("badge") or "").strip()) or badge
                        if not amt:
                            amt = _safe_int(parsed_fb.get("amount") or 0, 0) or amt
                        if not pay_line:
                            fb_pay = (str(parsed_fb.get("pay_line") or "").strip())
                            if fb_pay:
                                # 兜底：如果只能从整卡片文本里拿到，先去掉 icon/颜色标识串，再只保留『付款说明』类句子
                                fb_pay = re.sub(r'(?:flag(?:red|green|blue|yellow|gray)+)+', '', fb_pay, flags=re.I).strip()
                                if any(k in fb_pay for k in ("已於", "已于", "保管期", "將撥款", "将拨款", "撥款", "拨款")):
                                    pay_line = fb_pay
            except Exception as e:
                _dbg("perf_dom_parse_failed", str(e))
                if card_text:
                    parsed_fb = _parse_modal_text(card_text) or {}
                    if not isinstance(parsed_fb, dict):
                        parsed_fb = {}
                    badge = (str(parsed_fb.get("badge") or "").strip())
                    amt = _safe_int(parsed_fb.get("amount") or 0, 0)
                    fb_pay = (str(parsed_fb.get("pay_line") or "").strip())
                    if fb_pay:
                        fb_pay = re.sub(r'(?:flag(?:red|green|blue|yellow|gray)+)+', '', fb_pay, flags=re.I).strip()
                        if any(k in fb_pay for k in ("已於", "已于", "保管期", "將撥款", "将拨款", "撥款", "拨款")):
                            pay_line = fb_pay
            # 截图：已禁用（隐私 / 不落地）
            screenshot_path = ""
            screenshot_b64 = ""
            screenshot_url = ""

            return {
                "ok": True,
                "paid_status": paid_status,
                "ship_status": ship_status,
                "order_no": order_no,
                "found_order_no": found_order_no or order_no,
                "amount": amt,
                "paid_status": paid_status,
                "ship_status": ship_status,
                "badge": badge,
                "pay_line": pay_line,
                "screenshot_path": screenshot_path,
                "screenshot_b64": screenshot_b64,
                "screenshot_url": screenshot_url,
                "raw_text": (card_text or "")[:1200],
            }
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            except Exception:
                pass


def fetch_perf_one_order(
    *,
    chrome_path: str,
    profile_id: str,
    headless: bool,
    proxy: str,
    order_no: str,
    slow_mo_ms: int = 3000,
    timeout_sec: int = 45,
) -> Tuple[bool, Dict[str, Any]]:
    import asyncio

    chrome_path = (chrome_path or "").strip()
    if not chrome_path:
        return False, {"error": "缺少Chrome路径"}

    profile_dir = _profile_dir(profile_id)

    # lock
    in_use, reason = detect_chrome_profile_in_use(profile_dir)
    if in_use:
        return False, {"error": reason}

    ok_lock, reason = try_acquire(profile_dir, owner="perf_check")
    if not ok_lock:
        return False, {"error": reason}

    try:
        res = asyncio.run(
            _fetch_perf_one_order_async(
                chrome_path=chrome_path,
                profile_dir=profile_dir,
                headless=bool(headless),
                proxy=proxy,
                order_no=order_no,
                slow_mo_ms=int(slow_mo_ms or 0),
                timeout_ms=int((timeout_sec or 45) * 1000),
            )
        )
        if not isinstance(res, dict):
            return False, {"error": "未知返回"}
        if not res.get("ok"):
            return False, res
        return True, res
    except Exception as e:
        return False, {"error": str(e)}
    finally:
        release(profile_dir)


# ------------------------ Feature Tab ------------------------


@dataclass
class PerfTask:
    owner: str
    account: str
    order_no: str
    expected_amount: int


class PerformanceCheckFeatureTab:
    """业绩核对：

    - 控制端（你的电脑）：读取业绩Excel -> 按『所属人』分组 -> 派发到对应同事电脑（Agent）
    - 执行端（同事电脑）：用本机 cookie 打开Yahoo订单 -> 搜索订单 -> 打开明细 -> 抓『已付款/退款/保管』行 + 金额 + 弹窗截图 -> 回传

    云端任务队列（Cloudflare Worker + D1）：/api/perf/push_tasks, /api/perf/pull_tasks, /api/perf/post_result, /api/perf/pull_results
    """

    def __init__(self, *, app: Any, frame: ttk.Frame):
        self.app = app
        self.frame = frame

        self._worker_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._results: List[Dict[str, Any]] = []
        self._last_result_id = 0
        # 结果拉取游标持久化：避免重启/清空导致重复拉取
        self._cursor_file = os.path.join(getattr(self.app, "workdir", os.getcwd()), "perf_pull_cursor.json")
        self._last_result_id = self._load_pull_cursor(self._cursor_file, default=self._last_result_id)
        self._seen_result_ids: set[int] = set()
        # 导出游标：避免第二次导出把之前已导出的结果再写入
        self._export_cursor_file = os.path.join(getattr(self.app, "workdir", os.getcwd()), "perf_export_cursor.json")
        self._last_export_result_id = self._load_export_cursor(self._export_cursor_file, default=0)
        # perf 的“抢占”标记（避免误把别的暂停清掉）
        self._perf_batch_pause_set: Dict[str, bool] = {}
        # 自动接单：打开即启动；但若手动点过“停止”，则不再自动重启
        self._agent_manual_stopped = False
        self._auto_agent_started = False
        self._auto_agent_warned_missing = False

    @staticmethod
    def _load_pull_cursor(path: str, default: int = 0) -> int:
        try:
            if not path:
                return int(default)
            if not os.path.exists(path):
                return int(default)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("last_result_id", default) if isinstance(data, dict) else default
            return int(v) if str(v).strip() != "" else int(default)
        except Exception:
            return int(default)

    @staticmethod
    def _save_pull_cursor(path: str, last_id: int) -> None:
        try:
            if not path:
                return
            tmp = path + ".tmp"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_result_id": int(last_id)}, f, ensure_ascii=False)
            try:
                os.replace(tmp, path)
            except Exception:
                # fallback
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"last_result_id": int(last_id)}, f, ensure_ascii=False)
        except Exception:
            pass



    @staticmethod
    def _load_export_cursor(path: str, default: int = 0) -> int:
        try:
            if not path or not os.path.exists(path):
                return int(default)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            v = data.get("last_export_result_id", default)
            return int(v) if v is not None else int(default)
        except Exception:
            return int(default)

    @staticmethod
    def _save_export_cursor(path: str, last_id: int) -> None:
        try:
            if not path:
                return
            tmp = path + ".tmp"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_export_result_id": int(last_id)}, f, ensure_ascii=False)
            try:
                os.replace(tmp, path)
            except Exception:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"last_export_result_id": int(last_id)}, f, ensure_ascii=False)
        except Exception:
            pass


    def _http_json(self, method: str, endpoint: str, payload: dict | None = None, params: dict | None = None, timeout: int = 30):
        """派送端 HTTP JSON 封装：兼容历史代码里调用 self._http_json。
        - endpoint 可以是完整 URL（http://...）或相对路径（/api/xxx）
        - payload/params 二选一：GET 会用 params，POST 会用 payload
        """
        server = PERF_CLOUD_SERVER

        ep = str(endpoint or "")
        if ep.startswith("http://") or ep.startswith("https://"):
            url = ep
        else:
            url = server.rstrip("/") + ep

        method_u = (method or "GET").upper().strip()
        if method_u == "GET":
            ok, err, data = _http_get_json(url, params=(params if params is not None else (payload or {})), timeout=timeout)
        else:
            ok, err, data = _http_post_json(url, payload=(payload if payload is not None else (params or {})), timeout=timeout)

        if not ok:
            raise RuntimeError(err or "HTTP 请求失败")
        return data

    def build(self) -> None:
        f = self.frame
        f.columnconfigure(0, weight=1)

        # Load settings (shared)
        st = load_settings()

        cfg = st.get("perf_check", {}) if isinstance(st, dict) else {}

        # 默认值迁移：旧版常见默认是 slowmo=80 / poll=6，这里自动升级到新默认
        try:
            changed = False
            if _safe_int(cfg.get("slow_mo_ms", 80), 80) == 80:
                cfg["slow_mo_ms"] = 3000
                changed = True
            if _safe_int(cfg.get("poll_sec", 6), 6) == 6:
                cfg["poll_sec"] = 21600
                changed = True
            if changed and isinstance(st, dict):
                st.setdefault("perf_check", {})
                st["perf_check"].update(cfg)
                try:
                    save_settings(st)
                except Exception:
                    pass
        except Exception:
            pass


        self.var_mode = tk.StringVar(value=str(cfg.get("mode", "manager")))
        self.var_agent_id = tk.StringVar(value=str(cfg.get("agent_id", "")).strip())
        self.var_headless = tk.BooleanVar(value=bool(cfg.get("headless", True)))
        self.var_slowmo = tk.StringVar(value=str(cfg.get("slow_mo_ms", 3000)))
        self.var_poll = tk.StringVar(value=str(cfg.get("poll_sec", 21600)))

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="模式").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Radiobutton(top, text="控制端(派发)", value="manager", variable=self.var_mode, command=self._render_mode).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(top, text="执行端(接单)", value="agent", variable=self.var_mode, command=self._render_mode).grid(row=0, column=2, sticky="w", padx=(12, 0))

        self.frm_mode = ttk.Frame(f)
        self.frm_mode.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        f.rowconfigure(1, weight=3)

        # log
        frm_log = ttk.Labelframe(f, text="日志")
        frm_log.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        f.rowconfigure(2, weight=1)
        frm_log.columnconfigure(0, weight=1)
        frm_log.rowconfigure(0, weight=1)

        self.txt = tk.Text(frm_log, height=6, wrap="word")
        self.txt.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frm_log, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        self._render_mode()

    # ---------------- mode rendering ----------------

    def _render_mode(self) -> None:
        for w in list(self.frm_mode.winfo_children()):
            w.destroy()

        if self.var_mode.get() == "agent":
            self._build_agent_ui(self.frm_mode)
        else:
            self._build_manager_ui(self.frm_mode)


        # 执行端：切到接单模式后自动启动（若手动停止过，则不再自动重启）
        if self.var_mode.get() == "agent":
            try:
                self.frame.after(200, self._auto_start_agent_if_ready)
            except Exception:
                pass

    # ---------------- settings ----------------

    def _save_cfg(self) -> None:
        st = load_settings()
        if not isinstance(st, dict):
            st = {}
        st.setdefault("perf_check", {})
        st["perf_check"] = {
            "mode": self.var_mode.get(),
            "agent_id": self.var_agent_id.get().strip(),
            "headless": bool(self.var_headless.get()),
            "slow_mo_ms": _safe_int(self.var_slowmo.get(), 3000),
            "poll_sec": _safe_int(self.var_poll.get(), 21600),
        }
        try:
            save_settings(st)
            # keep in app cache too
            try:
                self.app.settings = st
            except Exception:
                pass
        except Exception as e:
            self._log(f"[PERF] 保存设置失败：{e}")

    def _log(self, msg: str) -> None:
        try:
            self.txt.insert("end", msg + "\n")
            self.txt.see("end")
        except Exception:
            pass

    # ---------------- manager ui ----------------

    def _build_manager_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        hint = ttk.Label(
            parent,
            text=(
                "控制端用法：\n"
                "1) 选业绩Excel -> 解析『账号/所属人/订单编号/商品总额(台币)』\n"
                "2) 按『所属人』派发给同事（同事的 AgentID 建议直接=所属人）\n"
                "3) 拉取结果 -> 自动对比金额/状态，必要时点截图复核\n\n"
                "注意：你只是派发任务，网页访问/台湾香港VPN 都还是同事电脑本机网络，不会被你改变。"
            ),
            justify="left",
        )
        hint.grid(row=0, column=0, sticky="w", pady=(0, 8))

        frm = ttk.Labelframe(parent, text="派发")
        frm.grid(row=1, column=0, sticky="ew")
        frm.columnconfigure(1, weight=1)

        self.var_excel_path = tk.StringVar(value="")
        self.var_sheet = tk.StringVar(value="")

        ttk.Label(frm, text="业绩Excel").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_excel_path).grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm, text="选择...", command=self._pick_excel).grid(row=0, column=2, padx=6, pady=4)

        ttk.Label(frm, text="Sheet").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.cbo_sheet = ttk.Combobox(frm, textvariable=self.var_sheet, values=[], state="readonly")
        self.cbo_sheet.grid(row=1, column=1, sticky="w", padx=6, pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 8))
        btns.columnconfigure((0, 1, 2), weight=1)
        ttk.Button(btns, text="解析任务", command=self._parse_excel_preview).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(btns, style="Accent.TButton", text="派发任务", command=self._dispatch_tasks).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(btns, text="保存设置", command=self._save_cfg).grid(row=0, column=2, sticky="ew")

        frm_res = ttk.Labelframe(parent, text="结果")
        frm_res.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        frm_res.columnconfigure(0, weight=1)
        frm_res.rowconfigure(0, weight=1)

        # screenshot_path: 服务器保存的截图路径（隐藏列），双击行可打开截图
        cols = ("id", "owner", "account", "order_no", "exp", "amt", "badge", "pay_line", "ok", "screenshot_path", "screenshot_url")
        self.tree = ttk.Treeview(frm_res, columns=cols, show="headings", height=10)
        col_names = {
            "id": "ResultID",
            "owner": "所属人",
            "account": "账号",
            "order_no": "订单编号",
            "exp": "预期金额",
            "amt": "抓取金额",
            "badge": "状态",
            "pay_line": "付款行",
            "ok": "OK",
            "screenshot_path": "截图文件",
            "screenshot_url": "截图URL",
        }
        for c, w in [
            ("id", 70), ("owner", 80), ("account", 90), ("order_no", 130),
            ("exp", 80), ("amt", 80), ("badge", 80), ("pay_line", 360), ("ok", 60),
            ("screenshot_path", 0), ("screenshot_url", 260),
        ]:
            self.tree.heading(c, text=col_names.get(c, c))
            self.tree.column(c, width=w, stretch=True)
        # hide screenshot_path
        try:
            self.tree.column("screenshot_path", width=0, minwidth=0, stretch=False)
        except Exception:
            pass
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frm_res, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        # 双击打开截图（如果有）
        self.tree.bind("<Double-1>", self._open_selected_screenshot)

        ctl = ttk.Frame(parent)
        ctl.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ctl.columnconfigure((0, 1, 2, 3), weight=1)
        ttk.Button(ctl, text="拉取结果", command=self._pull_results).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(ctl, text="导出Excel", command=self._export_results_excel).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(ctl, text="清空列表", command=self._clear_results_view).grid(row=0, column=2, sticky="ew", padx=(0, 6))
        ttk.Button(ctl, text="保存设置", command=self._save_cfg).grid(row=0, column=3, sticky="ew")

        self._tasks_preview: List[PerfTask] = []

    def _pick_excel(self) -> None:
        p = filedialog.askopenfilename(title="选择业绩Excel", filetypes=[("Excel", "*.xlsx;*.xlsm;*.xltx;*.xltm"), ("All", "*.*")])
        if not p:
            return
        self.var_excel_path.set(p)
        try:
            import openpyxl
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            names = wb.sheetnames
            wb.close()
            self.cbo_sheet.configure(values=names)
            if names:
                self.var_sheet.set(names[0])
        except Exception as e:
            self._log(f"[PERF] 读取sheet失败：{e}")

    def _parse_excel_preview(self) -> None:
        p = self.var_excel_path.get().strip()
        sh = self.var_sheet.get().strip()
        if not p:
            messagebox.showerror("错误", "请先选择Excel")
            return
        try:
            import openpyxl
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            ws = wb[sh] if sh and sh in wb.sheetnames else wb.active

            rows = ws.iter_rows(values_only=True)
            headers = [str(x or "").strip() for x in next(rows)]
            mp = _pick_header_map(headers)
            missing = [k for k, idx in mp.items() if idx < 0]
            if missing:
                wb.close()
                messagebox.showerror("错误", f"Excel缺少列：{', '.join(missing)}\n当前表头：{headers}")
                return

            # v6.0.68 ★:防禦性 strip — 萬一業績 Excel 來源帶了 +N 後綴(理論上 D1 export 不該有,
            # 但若操作員手工貼資料可能帶到),這裡先砍尾還原成純 Yahoo 號再派發
            try:
                from .syb_http_ops import strip_dup_suffix
            except Exception:
                strip_dup_suffix = lambda x: x  # noqa: E731

            tasks: List[PerfTask] = []
            for r in rows:
                if not r:
                    continue
                account = str(r[mp["account"]] or "").strip()
                owner = str(r[mp["owner"]] or "").strip()
                order_no = str(r[mp["order_no"]] or "").strip()
                exp_amt = _safe_int(r[mp["amount_twd"]], 0)
                if not order_no or not account:
                    continue
                # 砍 +N 後綴(若有)
                stripped = strip_dup_suffix(order_no)
                if stripped != order_no:
                    self._log(f"[PERF] order_no={order_no} → strip 後綴 → {stripped}")
                    order_no = stripped
                tasks.append(PerfTask(owner=owner, account=account, order_no=order_no, expected_amount=exp_amt))

            wb.close()

            self._tasks_preview = tasks
            by_owner: Dict[str, int] = {}
            for t in tasks:
                by_owner[t.owner or "(空)"] = by_owner.get(t.owner or "(空)", 0) + 1

            self._log(f"[PERF] 解析到 {len(tasks)} 条任务：" + ", ".join([f"{k}:{v}" for k, v in by_owner.items()]))
        except Exception as e:
            self._log(f"[PERF] 解析Excel失败：{e}")

    def _dispatch_tasks(self) -> None:
        self._save_cfg()
        server = PERF_CLOUD_SERVER
        token = PERF_BUILTIN_TOKEN
        if not getattr(self, "_tasks_preview", None):
            messagebox.showerror("错误", "请先点『解析任务』")
            return

        # group by owner -> agent_id. 默认 agent_id=owner
        grouped: Dict[str, List[PerfTask]] = {}
        for t in self._tasks_preview:
            agent_id = (t.owner or "").strip()
            if not agent_id:
                # 没所属人就跳过，避免派错
                continue
            grouped.setdefault(agent_id, []).append(t)

        if not grouped:
            messagebox.showerror("错误", "没有可派发的任务（可能所属人为空）")
            return

        total = 0
        for agent_id, tasks in grouped.items():
            payload = {
                "token": token,
                "agent_id": agent_id,
                "tasks": [
                    {
                        "owner": t.owner,
                        "account": t.account,
                        "order_no": t.order_no,
                        "expected_amount": t.expected_amount,
                        # 主管派发的核对任务默认高优先级：会插队到同事本机队列最前
                        "priority": 100,
                    }
                    for t in tasks
                ],
            }
            ok, err, data = _http_post_json(server + "/api/perf/push_tasks", payload, timeout=20)
            if ok:
                n = int(data.get("count") or len(tasks))
                total += n
                self._log(f"[PERF] 派发 {agent_id}：{n} 条")
            else:
                self._log(f"[PERF] 派发 {agent_id} 失败：{err}")

        self._log(f"[PERF] 派发完成，总计 {total} 条")

    def _pull_results(self) -> None:
        server = PERF_CLOUD_SERVER

        try:
            payload = self._http_json("GET", f"{server}/api/perf/pull_results", params={"token": PERF_BUILTIN_TOKEN, "after_id": self._last_result_id})
        except Exception as e:
            self._log(f"[PERF] 拉取失败：{e}")
            return

        items = payload.get("results") or payload.get("items") or []
        if not isinstance(items, list):
            items = []

        if not items:
            self._log(f"[PERF] 拉取完成：无新结果（after_id={self._last_result_id}）")
            return

        max_id = int(self._last_result_id or 0)

        for it in items:
            try:
                if not isinstance(it, dict):
                    continue

                rid = _safe_int(it.get("result_id") or it.get("id") or it.get("rid") or it.get("task_id"), 0)
                if rid in self._seen_result_ids:
                    continue
                if rid <= int(self._last_result_id or 0):
                    # 保险：如果 server 偶尔返回重复项，直接跳过
                    continue
                self._seen_result_ids.add(rid)
                if rid > max_id:
                    max_id = rid

                owner = str(it.get("owner") or it.get("who") or "")
                account = str(it.get("account") or it.get("acc") or "")
                order_no = str(it.get("order_no") or it.get("orderNo") or it.get("order") or "")
                exp_amt = _safe_int(it.get("expected_amount") or it.get("expected") or it.get("expectedAmount"), 0)
                amt = _safe_int(it.get("amount") or it.get("paid_amount") or it.get("paid") or it.get("actual_amount"), 0)
                badge = str(it.get("badge") or it.get("status") or "")
                pay_line = str(it.get("pay_line") or it.get("payLine") or it.get("pay") or "")

                shot_path = str(
                    it.get("screenshot_path")
                    or it.get("screenshot_file")
                    or it.get("shot_path")
                    or it.get("screenshot")
                    or ""
                )
                shot_url = str(
                    it.get("screenshot_url")
                    or it.get("shot_url")
                    or it.get("screenshotUrl")
                    or ""
                ).strip()

                shot_b64 = str(
                    it.get("screenshot_b64")
                    or it.get("shot_b64")
                    or it.get("screenshotB64")
                    or it.get("shotB64")
                    or ""
                ).strip()
                if shot_b64 and not shot_path:
                    try:
                        out_dir = os.path.join(os.getcwd(), "perf_check_shots_pulled")
                        os.makedirs(out_dir, exist_ok=True)
                        fn = f"{rid or it.get('task_id') or '0'}_{order_no or 'order'}.png"
                        local_path = os.path.join(out_dir, fn)
                        with open(local_path, "wb") as f:
                            f.write(base64.b64decode(shot_b64))
                        shot_path = local_path
                    except Exception:
                        pass

                if not shot_url and shot_path and (not shot_path.startswith("http://") and not shot_path.startswith("https://")):
                    shot_url = shot_path

                # 兜底：有 filename 就拼成可点击 URL；否则猜测 rid.png
                if not shot_url:
                    if shot_path.startswith("http://") or shot_path.startswith("https://"):
                        shot_url = shot_path
                    else:
                        fn = os.path.basename(shot_path).strip()
                        if fn:
                            shot_url = f"{server}/shots/{fn}"
                        elif rid:
                            shot_url = f"{server}/shots/{rid}.png"

                ok_flag = "OK" if (exp_amt == amt) else "DIFF"

                self.tree.insert(
                    "",
                    "end",
                    values=(rid, owner, account, order_no, exp_amt, amt, badge, pay_line, ok_flag, shot_path, shot_url),
                )
            except Exception:
                continue

        if max_id > int(self._last_result_id or 0):
            self._last_result_id = max_id
            self._save_pull_cursor(getattr(self, "_cursor_file", ""), self._last_result_id)

        self._log(f"[PERF] 拉取到 {len(items)} 条结果，last_id={self._last_result_id}")


    def _export_results_excel(self) -> None:
        """把当前列表里的结果导出成 Excel（或 CSV 兜底）。

        - 默认输出到“业绩Excel”同目录；如果没选业绩Excel，则输出到程序目录 output/。
        - 文件名：业绩核对结果_YYYYMMDD_HHMMSS.xlsx
        """
        try:
            items = list(self.tree.get_children())
            if not items:
                messagebox.showinfo("导出结果", "当前没有可导出的结果。")
                return

            rows = []
            max_rid = getattr(self, "_last_export_result_id", 0)

            for item in items:
                vals = self.tree.item(item, "values") or ()
                if len(vals) < 9:
                    continue

                task_id = vals[0]
                owner = vals[1]
                account = vals[2]
                order_no = vals[3]
                exp_amt = vals[4]
                got_amt = vals[5]
                badge = vals[6]
                pay_line = vals[7]
                ok_flag = vals[8]
                shot_path = vals[9] if len(vals) > 9 else ""
                shot_url = vals[10] if len(vals) > 10 else ""

                rid_int = None
                try:
                    rid_int = int(str(task_id).strip())
                except Exception:
                    m = re.search(r"(\d+)", str(task_id))
                    rid_int = int(m.group(1)) if m else None

                # 默认：只导出「上次导出之后」的新结果，避免第二次导出把旧数据也写进去
                if rid_int is not None:
                    last_export = getattr(self, "_last_export_result_id", 0)
                    if rid_int <= last_export:
                        continue
                    if rid_int > max_rid:
                        max_rid = rid_int

                rows.append([
                    task_id,
                    owner,
                    account,
                    order_no,
                    exp_amt,
                    got_amt,
                    "",  # 差额，写完后再补公式/计算
                    badge,
                    pay_line,
                    ok_flag,
                    shot_path,
                    shot_url,
                ])

            if not rows:
                messagebox.showinfo("导出结果", f"没有新结果可导出（已导出到 {getattr(self, '_last_export_result_id', 0)}）。")
                return

# 输出目录
            from pathlib import Path
            excel_in = (self.var_excel_path.get() or "").strip()
            if excel_in:
                out_dir = Path(excel_in).expanduser().resolve().parent
            else:
                out_dir = (BASE_DIR / "output").resolve()
            out_dir.mkdir(parents=True, exist_ok=True)

            ts = time.strftime("%Y%m%d_%H%M%S")
            xlsx_path = out_dir / f"业绩核对结果_{ts}.xlsx"

            # 先尝试写 xlsx
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Font, Alignment, PatternFill

                wb = Workbook()
                ws = wb.active
                ws.title = "results"

                headers = [
                    "TaskID",
                    "所属人",
                    "账号",
                    "订单编号",
                    "预期金额",
                    "抓取金额",
                    "差额(抓-预)",
                    "状态",
                    "付款行",
                    "OK",
                    "截图文件",
                    "截图URL",
                ]
                ws.append(headers)

                header_font = Font(bold=True)
                header_fill = PatternFill("solid", fgColor="DDDDDD")
                for c in range(1, len(headers) + 1):
                    cell = ws.cell(row=1, column=c)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal="center", vertical="center")

                # data rows
                red_fill = PatternFill("solid", fgColor="FFDDDD")
                ok_fill = PatternFill("solid", fgColor="DDFFDD")

                for r_idx, row in enumerate(rows, start=2):
                    ws.append(row)
                    # 简单标色：DIFF/金额不一致 -> 红；OK -> 绿
                    try:
                        exp_i = int(row[4])
                        amt_i = int(row[5])
                        ok_flag = str(row[9]).upper()
                        if exp_i != 0 and amt_i != 0 and exp_i != amt_i:
                            for c in range(1, len(headers) + 1):
                                ws.cell(row=r_idx, column=c).fill = red_fill
                        elif ok_flag == "OK":
                            for c in range(1, len(headers) + 1):
                                ws.cell(row=r_idx, column=c).fill = ok_fill
                    except Exception:
                        pass

                ws.freeze_panes = "A2"

                # 列宽（粗略）
                col_widths = [10, 10, 14, 18, 10, 10, 12, 10, 32, 6, 26, 50]
                for i, w in enumerate(col_widths, start=1):
                    ws.column_dimensions[chr(64 + i)].width = w

                wb.save(xlsx_path)

                # 记录本次已导出的最大结果ID，避免下次导出把旧结果再写进去
                try:
                    self._last_export_result_id = int(max_rid)
                    self._save_export_cursor(self._export_cursor_file, self._last_export_result_id)
                except Exception:
                    pass
                self._log(f"[PERF] 已导出Excel：{xlsx_path}")
                messagebox.showinfo("完成", f"已导出：\n{xlsx_path}")
                return

            except Exception as e_xlsx:
                # openpyxl 不存在或写入失败 -> CSV 兜底
                import csv
                csv_path = out_dir / f"业绩核对结果_{ts}.csv"
                with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow([
                        "TaskID", "所属人", "账号", "订单编号", "预期金额", "抓取金额", "差额(抓-预)", "状态", "付款行", "OK", "截图文件", "截图URL"
                    ])
                    w.writerows(rows)
                self._log(f"[PERF] openpyxl不可用或写xlsx失败，已导出CSV：{csv_path}；原因：{e_xlsx}")
                messagebox.showinfo("完成", f"openpyxl不可用或写xlsx失败，已导出CSV：\n{csv_path}")
                return

        except Exception as e:
            self._log(f"[PERF] 导出失败：{e}")
            messagebox.showerror("错误", f"导出失败：{e}")

    def _clear_results_view(self) -> None:
        try:
            for i in self.tree.get_children():
                self.tree.delete(i)
        except Exception:
            pass
        # NOTE: 清空列表只清空显示，不重置拉取游标（避免重复出现历史结果）

    
    def _open_selected_screenshot(self) -> None:
        try:
            item_id = self.tree.focus()
            if not item_id:
                return
            vals = self.tree.item(item_id, "values") or ()
            if not isinstance(vals, (list, tuple)):
                return

            # values: ... , screenshot_path, screenshot_url
            shot_path = str(vals[-2] if len(vals) >= 2 else "").strip()
            shot_url = str(vals[-1] if len(vals) >= 1 else "").strip()

            server = PERF_CLOUD_SERVER
            token = PERF_BUILTIN_TOKEN

            # 优先用 server 回传的 screenshot_url（可直接点开）
            if shot_url:
                if shot_url.startswith("http://") or shot_url.startswith("https://"):
                    url = shot_url
                elif shot_url.startswith("/"):
                    url = f"{server}{shot_url}"
                else:
                    url = f"{server}/{shot_url.lstrip('/')}" if server else shot_url

                # 只有 /shots/ 才追加 token（避免污染外部 URL）
                if token and server and url.startswith(f"{server}/shots/") and "token=" not in url:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}token={requests.utils.quote(token)}"

                webbrowser.open(url)
                return

            # 兜底：只有 screenshot_path 时，按旧逻辑拼 /shots/filename
            fn = os.path.basename(shot_path) if shot_path else ""
            if server and token and fn:
                url = f"{server}/shots/{fn}?token={requests.utils.quote(token)}"
                webbrowser.open(url)
        except Exception:
            pass


    def _build_agent_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        hint = ttk.Label(
            parent,
            text=(
                "执行端用法：\n"
                "1) AgentID 建议直接填『所属人』(Excel里的所属人要一致)\n"
                "2) 点『开始接单』后，本机会轮询服务器拿任务\n"
                "3) 每条任务会用本机 cookie 打开Yahoo订单 -> 明细 -> 抓状态/金额/截图 -> 回传\n\n"
                "注意：跑任务会占用该账号Profile；请不要同时用同账号开登录窗口或跑监控/批量。"
            ),
            justify="left",
        )
        hint.grid(row=0, column=0, sticky="w", pady=(0, 8))

        frm = ttk.Labelframe(parent, text="执行端设置")
        frm.grid(row=1, column=0, sticky="ew")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="AgentID").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_agent_id).grid(row=0, column=1, sticky="ew", padx=6, pady=4)

        ttk.Label(frm, text="Headless").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(frm, text="无头", variable=self.var_headless).grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(frm, text="SlowMo(ms)").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_slowmo, width=10).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(frm, text="轮询间隔(s)").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_poll, width=10).grid(row=3, column=1, sticky="w", padx=6, pady=4)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=(8, 10))
        btns.columnconfigure((0, 1, 2), weight=1)

        ttk.Button(btns, style="Accent.TButton", text="开始接单", command=self._agent_start).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(btns, style="Danger.TButton", text="停止", command=self._agent_stop).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(btns, text="保存设置", command=self._save_cfg).grid(row=0, column=2, sticky="ew")

        # 状态（内联显示，不再占独立区块）
        stat_row = ttk.Frame(parent)
        stat_row.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        self.var_stat = tk.StringVar(value="idle")
        ttk.Label(stat_row, text="状态：", foreground="#6E6E73").pack(side="left")
        ttk.Label(stat_row, textvariable=self.var_stat).pack(side="left")

    # ---------------- agent worker ----------------

    def _auto_start_agent_if_ready(self) -> None:
        """
        打开/切换到执行端后自动接单：
        - 不弹窗打断用户
        - 若手动点过“停止”，则不自动重启
        """
        try:
            if self.var_mode.get() != "agent":
                return
        except Exception:
            return

        if getattr(self, "_agent_manual_stopped", False):
            return

        # 已在运行则跳过
        if self._worker_thread and self._worker_thread.is_alive():
            return

        agent_id = (self.var_agent_id.get() or "").strip()
        if not agent_id:
            if not getattr(self, "_auto_agent_warned_missing", False):
                self._auto_agent_warned_missing = True
                try:
                    self._log("[PERF] 自动接单未启动：请先填写 AgentID")
                except Exception:
                    pass
            return

        # 保存配置（不落盘 token）
        try:
            self._save_cfg()
        except Exception:
            pass

        try:
            self._stop_evt.clear()
            self._worker_thread = threading.Thread(target=self._agent_loop, daemon=True)
            self._worker_thread.start()
            try:
                self.var_stat.set("running")
            except Exception:
                pass
            if not getattr(self, "_auto_agent_started", False):
                self._auto_agent_started = True
                try:
                    self._log("[PERF] 自动接单：已启动")
                except Exception:
                    pass
        except Exception:
            # 静默失败，避免打断
            return



    def _agent_start(self) -> None:
        self._save_cfg()
        # 手动启动：视为允许再次运行
        self._agent_manual_stopped = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._log("[PERF] Agent 已在运行")
            return

        agent_id = self.var_agent_id.get().strip()
        if not agent_id:
            messagebox.showerror("错误", "请填写 AgentID")
            return

        self._stop_evt.clear()
        self._worker_thread = threading.Thread(target=self._agent_loop, daemon=True)
        self._worker_thread.start()
        self.var_stat.set("running")
        self._log("[PERF] Agent 开始接单")

    def _agent_stop(self) -> None:
        # 手动停止后，默认不再自动重启
        self._agent_manual_stopped = True
        self._stop_evt.set()
        self.var_stat.set("stopping")
        self._log("[PERF] Agent 停止中...")

    def _agent_loop(self) -> None:
        server = PERF_CLOUD_SERVER
        token = PERF_BUILTIN_TOKEN
        agent_id = self.var_agent_id.get().strip()

        while not self._stop_evt.is_set():
            # 运行中允许随时改设置：无头/慢动作/轮询间隔会在下一轮立即生效
            poll_sec = max(2, _safe_int(self.var_poll.get(), 21600))
            slow_mo = max(0, _safe_int(self.var_slowmo.get(), 3000))
            headless = bool(self.var_headless.get())
            try:
                # pull tasks
                url = server + f"/api/perf/pull_tasks?token={requests.utils.quote(token)}&agent_id={requests.utils.quote(agent_id)}&max=3"
                ok, err, data = _http_get_json(url, timeout=20)
                if not ok:
                    self._log(f"[PERF] 拉取任务失败：{err}")
                    time.sleep(poll_sec)
                    continue

                tasks = data.get("tasks") or []
                if not tasks:
                    time.sleep(poll_sec)
                    continue

                for t in tasks:
                    if self._stop_evt.is_set():
                        break
                    # 每条任务再读一次，避免你在处理过程中切换无头/slowmo
                    headless_now = bool(self.var_headless.get())
                    slow_mo_now = max(0, _safe_int(self.var_slowmo.get(), 3000))
                    self._handle_one_task(server, token, agent_id, t, headless=headless_now, slow_mo=slow_mo_now)

            except Exception as e:
                self._log(f"[PERF] Agent loop error: {e}")

            time.sleep(1)

        self.var_stat.set("stopped")
        self._log("[PERF] Agent 已停止")

    # ---------------- preempt (priority) ----------------

    def _preempt_for_profile(self, profile_id: str, *, reason: str = "perf", wait_idle_sec: int = 25) -> None:
        """让路给『业绩核对』：暂停同账号的监控/批量（仅暂停自动化，不强杀人工窗口）。"""
        pid = (profile_id or "").strip()
        if not pid:
            return

        # 1) 暂停监控（只暂停这个账号）
        try:
            mon = getattr(self.app, "mon", None)
            loop = getattr(self.app, "loop", None)
            if getattr(self.app, "monitoring", False) and mon is not None and loop is not None:
                fut = asyncio.run_coroutine_threadsafe(mon.set_hold(pid, True, reason=reason), loop)
                fut.result(timeout=3)

                # 等待该账号当前轮次跑完（避免抢锁死等）
                fut2 = asyncio.run_coroutine_threadsafe(mon.wait_idle(pid, timeout_sec=wait_idle_sec), loop)
                fut2.result(timeout=wait_idle_sec + 5)
        except Exception:
            pass

        # 2) 暂停批量（只暂停这个账号）
        try:
            pause_ev = getattr(self.app, "_ensure_merch_pause_event", None)
            if callable(pause_ev):
                ev = pause_ev(pid)
                prev = bool(ev.is_set())
                if not prev:
                    ev.set()
                    self._perf_batch_pause_set[pid] = True
        except Exception:
            pass

    def _release_preempt_for_profile(self, profile_id: str, *, reason: str = "perf") -> None:
        """恢复监控/批量（只恢复本功能自己加的暂停）。"""
        pid = (profile_id or "").strip()
        if not pid:
            return

        # 1) 恢复监控（只移除 perf reason 的 hold）
        try:
            mon = getattr(self.app, "mon", None)
            loop = getattr(self.app, "loop", None)
            if mon is not None and loop is not None:
                fut = asyncio.run_coroutine_threadsafe(mon.set_hold(pid, False, reason=reason), loop)
                fut.result(timeout=3)
        except Exception:
            pass

        # 2) 恢复批量（只有 perf 自己 set 过才 clear）
        try:
            if self._perf_batch_pause_set.pop(pid, False):
                pause_ev = getattr(self.app, "_ensure_merch_pause_event", None)
                if callable(pause_ev):
                    ev = pause_ev(pid)
                    try:
                        ev.clear()
                    except Exception:
                        pass
        except Exception:
            pass

    def _wait_profile_free(self, profile_id: str, *, max_wait_sec: int = 180) -> Tuple[bool, str]:
        """等待同事关闭『人工接管/登录窗口』占用（不强制结束对方进程）。"""
        pid = (profile_id or "").strip()
        if not pid:
            return True, ""
        prof_dir = _profile_dir(pid)
        end_t = time.time() + float(max_wait_sec)
        last_reason = ""
        while time.time() < end_t and not self._stop_evt.is_set():
            in_use, reason = detect_chrome_profile_in_use(prof_dir)
            if not in_use:
                return True, ""
            if reason and reason != last_reason:
                self._log(f"[PERF] {pid}: 正在被人工窗口占用，等待释放... {reason}")
                last_reason = reason
            time.sleep(3)
        return False, last_reason or "Chrome profile in use"

    def _handle_one_task(self, server: str, token: str, agent_id: str, task: Dict[str, Any], *, headless: bool, slow_mo: int) -> None:
        task_id = int(task.get("task_id") or 0)
        owner = str(task.get("owner") or "")
        account = str(task.get("account") or "")
        order_no = str(task.get("order_no") or "")
        exp_amt = _safe_int(task.get("expected_amount"), 0)

        self._log(f"[PERF] 开始：{account} / {order_no} (task_id={task_id})")

        # find account profile_id & proxy
        try:
            accs = load_accounts()
        except Exception:
            accs = []

        profile_id = ""
        proxy = ""
        for a in accs:
            if str(a.get("name") or "").strip() == account:
                profile_id = str(a.get("profile_id") or "").strip() or account
                proxy = str(a.get("proxy") or "").strip()
                break

        if not profile_id:
            self._post_result(server, token, agent_id, {
                "task_id": task_id,
                "owner": owner,
                "account": account,
                "order_no": order_no,
                "expected_amount": exp_amt,
                "ok": False,
                "error": "本机accounts.json里找不到该账号",
            })
            self._log(f"[PERF] 失败：找不到账号 {account}")
            return

        chrome_path = ""
        try:
            chrome_path = str(getattr(self.app, "var_browser").get() or "").strip()
        except Exception:
            try:
                chrome_path = str(getattr(self.app, "settings", {}).get("browser_path", "")).strip()
            except Exception:
                chrome_path = ""

        # ✅ 抢占：暂停该账号监控/批量，让本任务优先执行（只暂停自动化，不强杀人工窗口）
        self._preempt_for_profile(profile_id, reason="perf", wait_idle_sec=25)

        try:
            # 如果同事正在手动开着该账号的Chrome窗口：不强抢，只等待一段时间让对方关闭
            free_ok, free_reason = self._wait_profile_free(profile_id, max_wait_sec=180)
            if not free_ok:
                payload = {
                    "task_id": task_id,
                    "owner": owner,
                    "account": account,
                    "order_no": order_no,
                    "expected_amount": exp_amt,
                    "ok": False,
                    "error": f"账号窗口占用，未能在等待时间内释放：{free_reason}",
                }
                self._post_result(server, token, agent_id, payload)
                self._log(f"[PERF] 失败：{payload['error']}")
                return

            ok, res = fetch_perf_one_order(
                chrome_path=chrome_path,
                profile_id=profile_id,
                headless=headless,
                proxy=proxy,
                order_no=order_no,
                slow_mo_ms=slow_mo,
                timeout_sec=45,
            )
        finally:
            # 无论成功/失败，都恢复（只恢复 perf 自己加的暂停）
            self._release_preempt_for_profile(profile_id, reason="perf")

        if not ok:
            payload = {
                "task_id": task_id,
                "owner": owner,
                "account": account,
                "order_no": order_no,
                "expected_amount": exp_amt,
                "ok": False,
                "error": str(res.get("error") or "unknown"),
            }
            self._post_result(server, token, agent_id, payload)
            self._log(f"[PERF] 失败：{payload['error']}")
            return

        payload = {
            "task_id": task_id,
            "owner": owner,
            "account": account,
            "order_no": order_no,
            "expected_amount": exp_amt,
            "ok": True,
            "badge": res.get("badge", ""),
            "pay_line": res.get("pay_line", ""),
            "amount": _safe_int(res.get("amount"), 0),
            "screenshot_b64": res.get("screenshot_b64", ""),
            "screenshot_path": res.get("screenshot_path", ""),
            "screenshot_filename": os.path.basename(res.get("screenshot_path", "") or ""),
            "preview": res.get("preview", ""),
        }
        self._post_result(server, token, agent_id, payload)

        self._log(f"[PERF] 完成：{account}/{order_no} 金额={payload.get('amount')} badge={payload.get('badge')}")

    def _post_result(self, server: str, token: str, agent_id: str, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload["token"] = token
        payload["agent_id"] = agent_id
        ok, err, _ = _http_post_json(server + "/api/perf/post_result", payload, timeout=25)
        if not ok:
            self._log(f"[PERF] 回传失败：{err}")