"""纯 HTTP 执行 Yahoo 出货（店配/宅配）。

替代 auto_label_feature.py 的 Playwright UI 自动化方案。
只需 cookies + wssid，不需要渲染页面、找按钮、点弹窗。

流程:
  1. cookie_store 读取 cookies + wssid
  2. FETCH_ORDER_DETAIL → 获取 itemIds、shippingMethod
  3. FETCH_EXECUTE_SHIPMENT_ALL → 执行出货，返回 printDeliveryUrl
  4. (店配) 短暂浏览器访问 7-11 面单 URL → 保存 PDF

依赖: merch_http_ops 的 AuthSession / _post_reservice / _try_cached_session
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .merch_http_ops import (
    AuthSession,
    AuthExpiredError,
    RESERVICE_URL,
    _build_payload,
    _post_reservice,
    _try_cached_session,
    _extract_and_save,
)
from .cookie_store import invalidate_cookie_cache
from .human import human_jitter_ms

LogFn = Callable[[str], None]

_log_mod = logging.getLogger(__name__)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)
    else:
        _log_mod.info(msg)


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class ShipResult:
    """出货结果"""
    success: bool = False
    order_id: str = ""
    shipping_id: str = ""          # 实际物流编号 (超商=P304..., 宅配=查询码)
    print_delivery_url: str = ""   # 面单 URL
    tracking_url: str = ""         # 物流追踪 URL
    status: str = ""               # 出货后状态
    receiver: dict = None          # 收件人 (宅配=完整, 超商=遮罩)
    error: str = ""
    raw_payload: dict = None       # 完整 API 响应 payload

    def __post_init__(self):
        if self.receiver is None:
            self.receiver = {}
        if self.raw_payload is None:
            self.raw_payload = {}


# ── 获取订单出货所需信息 ──────────────────────────────

def fetch_order_shipment_info(
    session: AuthSession,
    order_id: str,
    log: Optional[LogFn] = None,
) -> Dict:
    """调用 FETCH_ORDER_DETAIL 获取出货所需的 itemIds 和 shippingMethod。

    返回 dict:
      - item_ids: List[str]  e.g. ["880000000003-8800000001-88000000001"]
      - shipping_method: str  e.g. "tCat" / "sevenCvs"
      - status: str  e.g. "waitForDelivery"
      - can_ship: bool  (actions.executeShipment)
      - receiver: dict
    """
    _log(log, f"[SHIP {_ts()}] FETCH_ORDER_DETAIL {order_id}")

    data = _post_reservice(session, "FETCH_ORDER_DETAIL", {
        "wssid": session.wssid,
        "orderId": order_id,
        "archive": False,
        "role": "seller",
    })

    payload = data.get("payload", {})
    items = payload.get("items", [])
    item_ids = [it.get("itemId", "") for it in items if it.get("itemId")]
    shipping = payload.get("shipping", {})
    actions = payload.get("actions", {})

    result = {
        "item_ids": item_ids,
        "shipping_method": shipping.get("type", ""),
        "status": payload.get("status", ""),
        "can_ship": actions.get("executeShipment", False),
        "receiver": payload.get("receiver", {}),
        "shipping": shipping,
    }

    _log(log, f"[SHIP {_ts()}] 订单 {order_id}: "
         f"method={result['shipping_method']}, "
         f"status={result['status']}, "
         f"can_ship={result['can_ship']}, "
         f"items={len(item_ids)}")

    # debug: dump receiver for address diagnosis
    import json as _j
    _log(log, f"[SHIP {_ts()}] receiver 原始: {_j.dumps(result['receiver'], ensure_ascii=False)}")

    return result


# ── 执行出货 ─────────────────────────────────────────

def execute_shipment(
    session: AuthSession,
    order_id: str,
    item_ids: List[str],
    shipping_method: str,
    shipping_id: str = "",
    log: Optional[LogFn] = None,
) -> ShipResult:
    """调用 FETCH_EXECUTE_SHIPMENT_ALL 执行出货。

    Args:
        order_id: Yahoo 订单号
        item_ids: 商品 ID 列表 (从 FETCH_ORDER_DETAIL 获取)
        shipping_method: "tCat" (宅配) 或 "sevenCvs" (7-11) 等
        shipping_id: 宅配=查询码/物流编号, 超商=留空或填 order_id

    Returns:
        ShipResult 包含出货结果、面单 URL 等
    """
    # 超商出货不需要查询码，shippingId 就是 orderId
    if not shipping_id:
        shipping_id = order_id

    _log(log, f"[SHIP {_ts()}] FETCH_EXECUTE_SHIPMENT_ALL {order_id} "
         f"method={shipping_method} shippingId={shipping_id}")

    result = ShipResult(order_id=order_id)

    try:
        data = _post_reservice(session, "FETCH_EXECUTE_SHIPMENT_ALL", {
            "orders": [{
                "id": order_id,
                "itemIds": item_ids,
                "shippingMethod": shipping_method,
                "shippingId": shipping_id,
            }],
            "role": "seller",
            "wssid": session.wssid,
        })
    except AuthExpiredError as e:
        result.error = str(e)
        _log(log, f"[SHIP {_ts()}] 出货失败: {e}")
        return result

    payload = data.get("payload", {})
    orders = payload.get("orders", [])

    if not orders:
        result.error = f"API 响应无订单数据: {json.dumps(payload, ensure_ascii=False)[:200]}"
        _log(log, f"[SHIP {_ts()}] {result.error}")
        return result

    order = orders[0]
    result.success = True
    result.raw_payload = order
    result.status = order.get("status", "")
    result.receiver = order.get("receiver", {})

    # 提取物流编号
    shipping = order.get("shipping", {})
    detail = shipping.get("detail", {})
    deliver = detail.get("deliver", {})
    sender = detail.get("sender", {})

    result.shipping_id = deliver.get("shippingId", shipping_id)
    result.print_delivery_url = sender.get("printDeliveryUrl", "")
    result.tracking_url = ""

    # tracking URL 可能在 items 里
    for item in order.get("items", []):
        item_ship = item.get("shipping", {})
        if item_ship.get("trackingUrl"):
            result.tracking_url = item_ship["trackingUrl"]
            break

    _log(log, f"[SHIP {_ts()}] 出货成功! "
         f"shippingId={result.shipping_id} "
         f"status={result.status} "
         f"label={'有' if result.print_delivery_url else '无'}")

    return result


# ── 一键出货 (整合 auth + 查询 + 执行) ────────────────

def ship_order_http(
    profile_dir: Path,
    order_id: str,
    *,
    tracking_code: str = "",
    channel: str = "",
    chrome_path: str = "",
    headless: bool = True,
    log: Optional[LogFn] = None,
) -> ShipResult:
    """一键 HTTP 出货 (整合完整流程)。

    Args:
        profile_dir: Chrome profile 目录
        order_id: Yahoo 订单号
        tracking_code: 宅配查询码 (超商留空)
        channel: "黑貓"/"黑猫" = 宅配, 其他 = 超商 (或自动检测)
        chrome_path: Chrome 路径 (仅 cookie 失效时需要)
        headless: 提取 cookie 时是否无头

    Returns:
        ShipResult
    """
    import asyncio

    result = ShipResult(order_id=order_id)

    # 1. 获取 auth session (优先 cache)
    _log(log, f"[SHIP {_ts()}] 开始出货 {order_id}")
    session = _try_cached_session(profile_dir, log=log)

    if not session or not session.is_valid:
        _log(log, f"[SHIP {_ts()}] cookie cache 无效，提取新 cookies...")
        if not chrome_path:
            result.error = "cookie cache 无效且未提供 chrome_path"
            return result
        try:
            session = asyncio.run(_extract_and_save(
                profile_dir, chrome_path, headless, "", log))
        except Exception as e:
            result.error = f"提取 cookies 失败: {e}"
            return result
        if not session.is_valid:
            result.error = "提取的 session 无效 (可能登录已过期)"
            return result

    # 2. 获取订单信息
    try:
        info = fetch_order_shipment_info(session, order_id, log)
    except AuthExpiredError:
        _log(log, f"[SHIP {_ts()}] cookie 过期，尝试刷新...")
        invalidate_cookie_cache(profile_dir)
        if not chrome_path:
            result.error = "cookie 过期且未提供 chrome_path"
            return result
        try:
            session = asyncio.run(_extract_and_save(
                profile_dir, chrome_path, headless, "", log))
        except Exception as e:
            result.error = f"刷新 cookies 失败: {e}"
            return result
        if not session.is_valid:
            result.error = "刷新后 session 仍无效"
            return result
        try:
            info = fetch_order_shipment_info(session, order_id, log)
        except Exception as e:
            result.error = f"获取订单信息失败: {e}"
            return result

    if not info["can_ship"]:
        result.error = f"订单不可出货 (status={info['status']})"
        _log(log, f"[SHIP {_ts()}] {result.error}")
        return result

    if not info["item_ids"]:
        result.error = "订单无商品 itemIds"
        return result

    # 3. 人工延迟 (限流: 查看订单 → 操作出货之间应有阅读停顿)
    delay_ms = human_jitter_ms(2500, low=0.8, high=1.6, min_ms=1500, max_ms=5000)
    _log(log, f"[SHIP {_ts()}] 模拟阅读 {delay_ms}ms...")
    time.sleep(delay_ms / 1000.0)

    # 4. 确定出货方式
    shipping_method = info["shipping_method"]
    is_home = channel in ("黑貓", "黑猫") if channel else shipping_method in ("tCat", "homeDelivery")

    if is_home:
        # 宅配需要查询码
        if not tracking_code:
            result.error = "宅配出货需要查询码 (tracking_code)"
            return result
        ship_id = tracking_code
    else:
        # 超商: shippingId = orderId
        ship_id = order_id

    # 5. 执行出货
    result = execute_shipment(
        session, order_id, info["item_ids"],
        shipping_method, ship_id, log)

    return result


# ── 面单 PDF 下载 (超商需要浏览器) ─────────────────────

async def download_store_label_pdf(
    print_delivery_url: str,
    output_path: Path,
    profile_dir: Path,
    chrome_path: str,
    log: Optional[LogFn] = None,
) -> bool:
    """超商面单 PDF 下载。

    printDeliveryUrl 会重定向到 7-11 等超商的面单页面。
    需要短暂浏览器访问（7-11 需要 cookie + 重定向）。

    使用 launch + new_context + 注入 cookies（不用 launch_persistent_context，
    因为 headless 模式下 persistent_context 会崩溃）。
    """
    from .client_runtime_compat import (
        async_playwright, apply_runtime_normalization_async,
        get_launch_args, get_ignore_default_args,
    )
    from .cookie_store import load_cookie_cache

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _log(log, f"[LABEL {_ts()}] 下载面单: {print_delivery_url[:80]}...")

    # v6.0.49: pw/browser 預先宣告,統一 finally 清理避免洩漏
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            executable_path=chrome_path,
            headless=True,
            args=get_launch_args(headless=True, lang="zh-TW"),
            ignore_default_args=get_ignore_default_args(headless=True),
        )
        ctx = await browser.new_context(
            viewport={"width": 800, "height": 600},
        )
        await apply_runtime_normalization_async(ctx)

        # 从 cookie cache 注入 cookies（不需要复制 profile）
        cookies, _, _ = load_cookie_cache(str(profile_dir))
        if cookies:
            cookie_list = []
            for k, v in cookies.items():
                cookie_list.append({
                    "name": k, "value": v,
                    "domain": ".yahoo.com", "path": "/",
                })
            await ctx.add_cookies(cookie_list)

        # ============================================================
        # v6.0.49 萊福爾專屬修補:HTTP-first 拿 PDF binary
        # 萊福爾 ec_ordersprn 頁本身回 PDF binary,Chrome 用內嵌 PDF viewer 顯示,
        # headless page.pdf() 列印 viewer 殼 → 全黑(28KB 廢檔)。
        #
        # 流程:GET auc_redirect → 回 HTML form + JS auto-submit → parse hidden inputs →
        #       POST 萊福爾 ASPX → 拿真 PDF binary
        #
        # 重要:**只對萊福爾走此路徑**(URL 含 store_type=HILIFE),
        # 7-11 / 全家 / OK / 其他物流完全保留原本 page.pdf 行為,確保不影響既有功能。
        # ============================================================
        if "store_type=HILIFE" in print_delivery_url or "store_type=hilife" in print_delivery_url:
            try:
                r = await ctx.request.get(print_delivery_url, max_redirects=10, timeout=30000)
                ctype = (r.headers.get("content-type") or "").lower()
                body = await r.body()

                if "html" in ctype and len(body) < 8000 and b"<form" in body.lower():
                    import re as _re
                    html = body.decode("utf-8", errors="ignore")
                    action_m = _re.search(
                        r'<form[^>]*action=["\']([^"\']+)["\']', html, _re.I)
                    if action_m:
                        action_url = action_m.group(1)
                        inputs = _re.findall(
                            r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                            html, _re.I)
                        form_data = dict(inputs)
                        if form_data and action_url:
                            r2 = await ctx.request.post(
                                action_url, form=form_data, max_redirects=5, timeout=30000)
                            body2 = await r2.body()
                            ctype2 = (r2.headers.get("content-type") or "").lower()
                            if "pdf" in ctype2 and len(body2) > 1024 and body2[:4] == b"%PDF":
                                output_path.write_bytes(body2)
                                _log(log, f"[LABEL {_ts()}] 萊福爾 PDF form-POST: {len(body2)} bytes")
                                return True  # finally 統一清理
                            _log(log, f"[LABEL {_ts()}] 萊福爾 form POST 後非 PDF(ctype={ctype2} size={len(body2)}),降回 page.pdf")
                    else:
                        _log(log, f"[LABEL {_ts()}] 萊福爾 HTML 找不到 form action,降回 page.pdf")
                else:
                    _log(log, f"[LABEL {_ts()}] 萊福爾 HTTP 回應結構非預期(ctype={ctype} size={len(body)}),降回 page.pdf")
            except Exception as _e_http:
                _log(log, f"[LABEL {_ts()}] 萊福爾 HTTP 路徑異常({_e_http}),降回 page.pdf")

        # ── safety net:既有 page.pdf 流程(對 yahoo 改 API 仍可用)──
        page = await ctx.new_page()
        try:
            try:
                await page.goto(print_delivery_url, wait_until="networkidle", timeout=30000)
            except Exception:
                await page.goto(print_delivery_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # 验证是否到达面单页面（而非被重定向到首页）
            final_url = page.url
            if "bid.yahoo.com" in final_url and "/logistics/" not in final_url and "/billing/" not in final_url:
                _log(log, f"[LABEL {_ts()}] 面单URL已过期，被重定向到: {final_url[:80]}")
                return False  # finally 統一清理

            # 保存 PDF
            await page.pdf(
                path=str(output_path),
                format="A4",
                print_background=True,
                display_header_footer=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "5mm", "right": "5mm"},
            )
            _log(log, f"[LABEL {_ts()}] PDF 已保存(page.pdf fallback): {output_path.name}")
            return True  # finally 統一清理

        except Exception as e:
            _log(log, f"[LABEL {_ts()}] 面单下载失败: {e}")
            return False  # finally 統一清理

    except Exception as e:
        _log(log, f"[LABEL {_ts()}] 浏览器启动失败: {e}")
        return False  # finally 統一清理

    finally:
        # v6.0.49: 無論成功/失敗/異常,統一清理 browser + pw,防進程洩漏
        # 順序:browser 先 close 再 pw.stop(否則 pw.stop 可能 hang)
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


# ── 宅配面单 (deliver_print 页面) ─────────────────────

async def download_home_label_pdf(
    order_id: str,
    output_path: Path,
    profile_dir: Path,
    chrome_path: str,
    log: Optional[LogFn] = None,
) -> bool:
    """宅配面单 PDF 下载。

    宅配面单 URL: https://tw.bid.yahoo.com/partner/order/deliver_print?orderId=xxx
    在 Yahoo 域名下，直接用 cookies 访问。
    """
    url = f"https://tw.bid.yahoo.com/partner/order/deliver_print?orderId={order_id}"
    return await download_store_label_pdf(
        url, output_path, profile_dir, chrome_path, log)
