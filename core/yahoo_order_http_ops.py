"""Yahoo 賣家後台訂單 HTTP API 客戶端 — 靜默核對訂單狀態。

複用 core/cookie_store 的緩存(monitor 每輪寫入,24h 有效),
不開瀏覽器、不搶 profile 鎖,純 HTTP 調用 reservice API。

用途:業績核對 — 確認訂單實際狀態(已退款/已取消/已撥款/已給評 等),
異常狀態(退款/取消/爭議)的訂單不算業績。

API endpoint(瀏覽器抓的完整 schema):
  POST https://tw.bid.yahoo.com/fe/_reservice_/
  body: {type, payload, reservice, rtk2, params}
  reservice.name: FETCH_ORDERLIST_LISTINGS / FETCH_ORDER_DETAIL

實測:Playwright 5-8s/order → HTTP 200-500ms/50 orders,~10x 快。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .cookie_store import load_cookie_cache, invalidate_cookie_cache

YAHOO_RESERVICE_URL = "https://tw.bid.yahoo.com/fe/_reservice_/"
YAHOO_ORDER_LIST_URL = "https://tw.bid.yahoo.com/partner/order/list"


class YahooAuthError(Exception):
    """Yahoo cookie/wssid 過期或不存在。"""


class YahooAPIError(Exception):
    """Yahoo API 業務/網路錯誤。"""


class YahooOrderAPI:
    """Yahoo 賣家後台訂單 API 客戶端(靜默,從 cookie_cache 載入認證)。

    Args:
        profile_dir: 該 Yahoo 帳號 profile 目錄(profiles/<profile_id>/)
        log: 日誌函數(可選)
        max_age: cookie cache 最大有效期(秒,默認 24h)

    Raises:
        YahooAuthError: cookie cache 不存在或已過期
    """

    def __init__(self, profile_dir: Path, log: Optional[Callable] = None,
                 max_age: float = 86400):
        self.profile_dir = Path(profile_dir)
        self.log = log or (lambda *_: None)
        cookies, wssid, saved_at = load_cookie_cache(self.profile_dir, max_age=max_age)
        if not cookies or not wssid:
            raise YahooAuthError(
                f"無 cookie cache 或 wssid:{self.profile_dir.name}(需先跑監控刷新)"
            )
        self.cookies = cookies
        self.wssid = wssid
        self.cookie_saved_at = saved_at
        self.s = requests.Session()
        self.s.cookies.update(cookies)
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/130.0.0.0 Safari/537.36"),
            "Referer": YAHOO_ORDER_LIST_URL,
        })

    def list_orders(
        self,
        start_iso: str,
        end_iso: str,
        time_range: str,
        *,
        limit: int = 50,
        offset: int = 0,
        archive: bool = False,
        shipping_status: Optional[str] = None,
        payment_status: Optional[str] = None,
        escrow_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """抓訂單列表。

        start_iso/end_iso: ISO8601 UTC,例 "2026-02-01T16:00:00Z"
        time_range: UI 顯示用,例 "2026/02/02 ~ 2026/05/02"
        return: {totalCount, listings, nextParams, ...}
        """
        payload = {
            "wssid": self.wssid,
            "limit": limit,
            "role": "seller",
            "reset": offset == 0,
            "queryType": "keyword",
            "sortBy": "-createTime",
            "startTime": start_iso,
            "endTime": end_iso,
            "archive": archive,
            "timeRange": time_range,
        }
        if offset > 0:
            payload["offset"] = offset
        if shipping_status:
            payload["shippingStatus"] = shipping_status
        if payment_status:
            payload["paymentStatus"] = payment_status
        if escrow_status:
            payload["escrowStatus"] = escrow_status

        body = {
            "type": "CALL_RESERVICE",
            "payload": payload,
            "reservice": {"name": "FETCH_ORDERLIST_LISTINGS", "state": "CREATED"},
            "rtk2": True,
            "params": payload,
        }
        return self._post(body)

    def get_detail(self, order_id: str, archive: bool = False) -> Dict[str, Any]:
        payload = {
            "wssid": self.wssid,
            "orderId": order_id,
            "archive": archive,
            "role": "seller",
        }
        body = {
            "type": "CALL_RESERVICE",
            "payload": payload,
            "reservice": {"name": "FETCH_ORDER_DETAIL", "state": "CREATED"},
            "rtk2": True,
            "params": payload,
        }
        return self._post(body)

    def _post(self, body: dict, timeout: int = 25) -> Dict[str, Any]:
        try:
            r = self.s.post(YAHOO_RESERVICE_URL, json=body, timeout=timeout)
        except Exception as e:
            raise YahooAPIError(f"network: {e}")
        if r.status_code in (401, 403):
            invalidate_cookie_cache(self.profile_dir)
            raise YahooAuthError(f"HTTP {r.status_code}(已標記 cookie 失效)")
        if r.status_code != 200:
            raise YahooAPIError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:
            raise YahooAPIError(f"non-json response: {r.text[:200]}")
        # reservice 回傳結構:{ payload: {...}, reservice: {state: "FETCHED"|"FAILED"} }
        rs = data.get("reservice") or {}
        if rs.get("state") == "FAILED":
            raise YahooAPIError(f"reservice failed: {data}")
        return data.get("payload") or {}


# ── 訂單狀態識別 ───────────────────────────────────────────

def identify_order_status(order: Dict[str, Any]) -> Tuple[str, bool, str]:
    """根據 order JSON 識別「正常 vs 異常」狀態。

    回傳 (label, is_valid_perf, abnormal_reason):
      - label: 中文狀態,例「已退款+待出貨」「已撥款」
      - is_valid_perf: True=算業績,False=不算
      - abnormal_reason: 不算業績時的原因(算業績時為空)

    判斷規則(對應瀏覽器 Claude 抓的 9 種狀態):
      payment.status='canceled' → 已取消(不算)
      payment.status='refund' → 已退款(不算)
      escrow.filterId='apply' → 退款中(不算,可能逆轉)
      escrow.filterId='dispute' → 爭議中(不算)
      ship='buyerPickedUp' + pay='paid' + escrow.statusId∈(N00*,close) → 已撥款(算)
      ship='buyerPickedUp' + pay='paid' + escrow.statusId∈(N08*) → 等撥款(算)
      ship in (delivered, delivering, inBuyerStore) + pay='paid' → 運送中(算)
      ship in (wait, getShippingIdForDelivery) + pay='paid' → 待出貨已付款(算)
      payment.status='notPay' → 未付款(不算,通常 7 天自動取消)
      rated=True → 已給評(算)
    """
    pay = ((order.get("payment") or {}).get("status") or "")
    ship = ((order.get("shipping") or {}).get("status") or "")
    escrow = order.get("escrow") or {}
    escrow_id = str(escrow.get("statusId") or "")
    filter_id = str(escrow.get("filterId") or "")
    rated = bool(order.get("rated"))

    # 1. 取消(優先,不算)
    if pay == "canceled":
        return ("已取消", False, "訂單已取消")

    # 2. 退款族群(不算)
    if pay == "refund":
        if ship == "wait":
            return ("已退款+待出貨", False, "同意退款,訂單未出貨")
        if ship == "buyerPickedUp":
            return ("已退款+已取貨", False, "取貨後退款")
        return (f"已退款+{ship or '未知'}", False, "退款狀態")

    # 3. 退款處理中(不算,可能逆轉)
    if filter_id == "apply":
        return ("退款中", False, "買家申請退款處理中")
    if filter_id == "dispute":
        return ("價金保管中(爭議)", False, "Yahoo 留款爭議中")

    # 4. 已撥款 / 已取貨(算)
    if ship == "buyerPickedUp" and pay == "paid":
        if escrow_id.startswith("N00"):
            return ("已撥款/結束保管", True, "")
        if escrow_id.startswith("N08"):
            return ("已取貨+已付款(等撥款)", True, "")
        if filter_id == "close":
            return ("已撥款", True, "")
        return ("已取貨+已付款", True, "")

    # 5. 出貨運送中(算)
    if ship in ("delivered", "delivering", "inBuyerStore", "deliveringAgain") and pay == "paid":
        return ("已出貨+運送中", True, "")

    # 6. 待出貨已付款(算)
    if ship in ("wait", "getShippingIdForDelivery", "inSellerStore", "sellerPickup") and pay == "paid":
        return ("待出貨+已付款", True, "")

    # 7. 已給評(算)— 通常不會獨立判定,常見是已撥款後評價
    if rated:
        return ("已給評", True, "")

    # 8. 未付款(不算)
    if pay == "notPay":
        return ("未付款", False, "未付款(通常 7 天自動取消)")

    # 9. 其他未知
    return (f"{ship or '?'}/{pay or '?'}", False, "未知狀態")


def is_valid_perf(order: Dict[str, Any]) -> bool:
    """快捷:是否算業績。"""
    return identify_order_status(order)[1]
