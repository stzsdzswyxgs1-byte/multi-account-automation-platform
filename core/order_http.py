"""純 HTTP 抓取 Yahoo 拍賣訂單列表 — 取代 Playwright(v6.0.83+)

從 `https://tw.bid.yahoo.com/partner/order/list` 解析 SSR inline JSON
(`<script id="isoredux-data">{...}</script>`),拿到每筆訂單:
- orderId / 訂單編號
- buyer.id / 買家 Y-ID (用來路由到對應 forum topic)
- buyer.chatUrl / 點到 IM 對話的 URL
- price.orderAmount / 金額
- status / statusText.status / payment.status / shipping.status
- items[] / 商品列表
- urls.detail / 訂單詳情頁
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client_runtime_compat import get_html_headers
from .im_http_ops import _build_session

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

_ORDER_LIST_URL = "https://tw.bid.yahoo.com/partner/order/list"
_ISOREDUX_RE = re.compile(
    r'<script id="isoredux-data"[^>]*>(.+?)</script>', re.DOTALL,
)

# Yahoo 內部 status code → 中文友好標籤
# v6.1 實機驗證(chen749/kinhuaw168 訂單):buyerPickup 拼字不是 buyerPickedUp;
# 取消狀態實際細分為 buyerCancel / sellerCancel(不是統一 canceled)
_STATUS_LABEL = {
    "waitForDelivery": "待出貨",
    "delivered": "已出貨",
    "completed": "已完成",
    "canceled": "已取消",
    "buyerCancel": "買家取消",
    "sellerCancel": "賣家取消",
    "refunded": "已退款",
    "buyerPickup": "已取貨",  # v6.1 修拼字
    "buyerPickedUp": "已取貨",  # 保留舊拼字 backward compat
    "deliveryOverdue": "出貨逾期",
}

_PAY_LABEL = {
    "paid": "已付款",
    "notPay": "未付款",
    "refund": "退款中",
    "canceled": "已取消",
}


# v6.1:統一訂單分類 helper(所有統計地方都用這,確保口徑一致)
def classify_order(status: str, payment: str = "", status_label: str = "", status_extra: str = "") -> str:
    """把 Yahoo (status, payment) 組合 → 統一分類 key。

    v6.1:加 status_label / status_extra 參數,解決「deliveryOverdue 但 Yahoo 已取消」假告警:
    - 超商未取貨 → Yahoo 系統取消,但 status code 留 deliveryOverdue
    - 這時 status_label="已取消" 或 status_extra 含「取消」
    - 應該歸 canceled,不是 overdue

    Returns:
      'overdue' / 'waiting_paid' / 'waiting_unpaid' / 'shipped' / 'picked_up'
      / 'completed' / 'canceled' / 'refunded' / 'unknown'

    v6.2 業務修正:waitForDelivery + notPay (取貨付款 COD) Yahoo 自己標 label="待出貨",
    對中間商來說都是「賣家要寄」,不分付款方式。因此 waitForDelivery 任何 payment
    都歸 waiting_paid(待出貨)。waiting_unpaid 保留 enum 但實機不會被觸發。
    """
    # v6.1:status_label / status_extra 含「取消」→ 強制 canceled(防 deliveryOverdue 假告警)
    if status_label and "取消" in status_label:
        return "canceled"
    if status_extra and "取消" in status_extra:
        return "canceled"

    # refund 優先(退款中不算待付款,業務意義不同)
    if payment == "refund":
        return "refunded"
    if status == "deliveryOverdue":
        return "overdue"
    if status in ("waitForDelivery", "getShippingIdForDelivery"):
        # v6.2:COD(notPay)也是賣家要出貨,跟 paid 等同待出貨
        # v6.1.18:getShippingIdForDelivery = 取得物流單號等待出貨(transitional 狀態,同樣待我們行動)
        return "waiting_paid"
    if status in ("delivered",):
        return "shipped"
    if status in ("buyerPickup", "buyerPickedUp"):
        return "picked_up"
    if status == "completed":
        return "completed"
    if status in ("canceled", "buyerCancel", "sellerCancel"):
        return "canceled"
    if status == "refunded":
        return "refunded"
    return "unknown"

# v6.1:狀態 icon 顏色化(訂單卡開頭一眼看出狀態)
def _status_icon(status: str, payment: str, status_label: str = "", status_extra: str = "") -> str:
    """根據 status + payment 組合給 icon。
    🟡 未付款待出貨 / 🔴 已付款待出貨(要我們行動) / 🔵 已出貨/已取貨 / 🟢 已完成 / ⚫ 已取消 / 🚨 逾期
    """
    cls = classify_order(status, payment, status_label, status_extra)
    return {
        "overdue": "🚨",
        "waiting_paid": "🔴",
        "waiting_unpaid": "🟡",
        "shipped": "🔵",
        "picked_up": "🔵",
        "completed": "🟢",
        "canceled": "⚫",
        "refunded": "⚫",
        "unknown": "📋",
    }.get(cls, "📋")


# v6.1:從 ISO 時間戳轉台灣時間友善格式
# 防 naive ISO 在非 TW 機器(Docker UTC)會多加 8h 變 +16h
def _fmt_dt(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        TW_TZ = timezone(timedelta(hours=8))
        _dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # 沒帶 tzinfo 的 ISO 視為台灣時間(Yahoo 給 TW 賣家通常本來就 TW 時間)
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=TW_TZ)
        # 統一轉成台灣時間顯示
        _tw = _dt.astimezone(TW_TZ)
        return _tw.strftime("%m/%d %H:%M")
    except Exception:
        return iso_str[:16]


def fetch_orders(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
    timeout: int = 20,
) -> Tuple[List[Dict[str, Any]], str]:
    """純 HTTP 拿單一帳號的訂單列表(預設拉最近 50 筆,server cap)。

    Returns:
        (orders_list, error)
        orders_list 元素 = 簡化版訂單 dict:
            {
              "order_id": "...",
              "buyer_id": "Y...",
              "buyer_chat_url": "...",
              "amount": 1234,
              "status": "waitForDelivery",
              "status_label": "待出貨",
              "payment_status": "notPay",
              "payment_label": "未付款",
              "shipping_status": "wait",
              "items": [{"title":..., "image":..., "url":...}],
              "detail_url": "...",
            }
    """
    on_log = on_log or (lambda *_: None)

    session, _wssid, err = _build_session(profile_dir)
    if not session:
        return [], f"session 不可用: {err}"

    try:
        r = session.get(_ORDER_LIST_URL, headers=get_html_headers(), timeout=timeout)
        if r.status_code != 200:
            return [], f"order list HTTP {r.status_code}"
        m = _ISOREDUX_RE.search(r.text)
        if not m:
            return [], "isoredux-data 不存在(可能未登入)"
        try:
            data = json.loads(m.group(1))
        except Exception as e:
            return [], f"isoredux-data parse fail: {e}"

        raw_listings = (data.get("orderList") or {}).get("listings") or []
        out: List[Dict[str, Any]] = []
        for l in raw_listings:
            try:
                buyer = l.get("buyer") or {}
                seller = l.get("seller") or {}
                price = l.get("price") or {}
                pay = l.get("payment") or {}
                ship = l.get("shipping") or l.get("shipment") or {}
                status_text_obj = l.get("statusText") or {}
                urls = l.get("urls") or {}
                items = []
                # v6.1:解每件商品的物流資訊(shippingId / deliverDateTime / method)
                for it in (l.get("items") or [])[:5]:
                    it_ship = it.get("shipping") or {}
                    items.append({
                        "title": it.get("title", "")[:120],
                        "image": it.get("image", ""),
                        "url": it.get("url", ""),
                        "quantity": it.get("quantity", 1),
                        "unit_price": it.get("unitPrice", 0),
                        # v6.1 新增:商品物流資訊
                        "shipping_id": it_ship.get("shippingId", ""),  # 物流單號
                        "shipping_method": it_ship.get("method", ""),  # homeDelivery / cvs / 7-11 等
                        "deliver_datetime": it_ship.get("deliverDateTime", ""),  # ISO 出貨時間
                        "is_delivered": bool(it_ship.get("isDelivered", False)),
                    })
                status = l.get("status", "")
                status_label = status_text_obj.get("status") or _STATUS_LABEL.get(status, status)
                out.append({
                    "order_id": l.get("orderId", ""),
                    # v6.1 新增:買家暱稱(之前只有 Y-ID)
                    "buyer_id": buyer.get("id", ""),
                    "buyer_name": buyer.get("name", ""),
                    "buyer_chat_url": buyer.get("chatUrl", ""),
                    # v6.1 新增:賣家(自己)店鋪資訊
                    "seller_id": seller.get("id", ""),
                    "seller_name": seller.get("name", ""),
                    "amount": price.get("orderAmount", 0),
                    "status": status,
                    "status_label": status_label,
                    # v6.1 新增:狀態完整描述(『已於2026/05/12 18:21:04出貨...』)
                    "status_extra": status_text_obj.get("extra", ""),
                    "status_shipping_label": status_text_obj.get("shipping", ""),
                    "payment_status": pay.get("status", ""),
                    "payment_label": _PAY_LABEL.get(pay.get("status", ""), pay.get("status", "")),
                    "shipping_status": ship.get("status", ""),
                    "items": items,
                    # v6.1 新增:多個 action URL(評價/查款/取消)
                    "detail_url": urls.get("detail", ""),
                    "rating_url": urls.get("rating", ""),
                    "escrow_url": urls.get("escrow", ""),
                    "cancel_url": urls.get("cancel", ""),
                })
            except Exception as e:
                on_log(f"[ORDER-HTTP] parse 1 listing 失敗: {e}")
        return out, ""
    except Exception as e:
        return [], f"fetch_orders 異常: {e}"
    finally:
        try:
            session.close()
        except Exception:
            pass


def format_order_progress(order: Dict[str, Any], age_seconds: float = 0.0) -> str:
    """v6.1:訂單流程進度條(4 階段:付款 → 出貨 → 收貨 → 完成)。

    HTML mode 輸出。當前 ⏳ 步驟若 age_seconds > 0 顯示「已 2d 14h」(中間商 KPI 關鍵指標)。
    """
    status = order.get("status", "")
    payment = order.get("payment_status", "")
    items = order.get("items") or []
    deliver_dt = items[0].get("deliver_datetime", "") if items else ""
    ship_id = items[0].get("shipping_id", "") if items else ""

    # v6.1:用 classify_order 統一判斷,canceled/refunded/buyerCancel/sellerCancel 都當已關閉
    status_label = order.get("status_label", "")
    status_extra = order.get("status_extra", "")
    cls = classify_order(status, payment, status_label, status_extra)
    if cls in ("canceled", "refunded"):
        return f"❌ <b>{_esc(_STATUS_LABEL.get(status, status))}</b>\n(此訂單已關閉)"

    steps = {
        "pay": {"icon": "⬜", "label": "等付款", "ts": ""},
        "ship": {"icon": "⬜", "label": "等出貨", "ts": ""},
        "receive": {"icon": "⬜", "label": "等收貨", "ts": ""},
        "complete": {"icon": "⬜", "label": "完成", "ts": ""},
    }

    if payment == "paid":
        steps["pay"]["icon"] = "✅"
        steps["pay"]["label"] = "已付款"
    elif payment in ("notPay", ""):
        steps["pay"]["icon"] = "⏳"

    # v6.1:支援 buyerPickup(無 ed)正確拼字
    if deliver_dt or status in ("delivered", "buyerPickup", "buyerPickedUp", "completed"):
        steps["ship"]["icon"] = "✅"
        steps["ship"]["label"] = "已出貨"
        if deliver_dt:
            steps["ship"]["ts"] = _fmt_dt(deliver_dt)
    elif status == "waitForDelivery" and payment == "paid":
        steps["ship"]["icon"] = "⏳"
        steps["ship"]["label"] = "等出貨"
    elif status == "deliveryOverdue":
        steps["ship"]["icon"] = "🚨"
        steps["ship"]["label"] = "出貨逾期!"

    if status in ("buyerPickup", "buyerPickedUp", "completed"):
        steps["receive"]["icon"] = "✅"
        steps["receive"]["label"] = "已收貨"
    elif status == "delivered":
        steps["receive"]["icon"] = "⏳"
        steps["receive"]["label"] = "等收貨"

    if status == "completed":
        steps["complete"]["icon"] = "✅"
        steps["complete"]["label"] = "完成"

    # v6.1:當前 ⏳ 步驟加「已等多久」— 中間商最在意的 KPI
    age_suffix = ""
    if age_seconds > 60:
        age_suffix = f" (已 {_humanize_age(age_seconds)})"

    lines = []
    for k in ("pay", "ship", "receive", "complete"):
        s = steps[k]
        ts = f" ({s['ts']})" if s["ts"] else ""
        extra = ""
        if k == "ship" and ship_id and s["icon"] == "✅":
            extra = f" 單號 <code>{_esc(ship_id)}</code>"
        # 當前 ⏳ 步驟才加 age
        age_part = age_suffix if s["icon"] == "⏳" else ""
        lines.append(f"  {s['icon']} {_esc(s['label'])}{ts}{extra}{age_part}")
    return "\n".join(lines)


_METHOD_LABEL = {
    "homeDelivery": "宅配/黑貓",
    "cvs": "超商取貨",
    "sevenEleven": "7-11 取貨",
    "familyMart": "全家取貨",
    "hilife": "萊爾富取貨",
    "okMart": "OK 超商取貨",
}


def _esc(s: Any) -> str:
    """v6.1:HTML escape,parse_mode=HTML 必須,防商品標題含 `<>&` 破訊息。"""
    if s is None:
        return ""
    import html as _html
    return _html.escape(str(s))


def _safe_html_truncate(text: str, limit: int = 4000) -> str:
    """v6.1:HTML mode safe truncate,在 `<tag>` 邊界外切,防 TG parse 400 error。

    策略:從 limit 位置往前找最近的 `>`(tag 結束)或 `\\n`,在那邊切。
    如果都找不到 → 至少避免切在 `<` 之後(不完整 tag),退到上一個換行。
    """
    if len(text) <= limit:
        return text
    # 從 limit 往前找安全切點(`>` 或 `\n`,優先 `\n`)
    cut = text.rfind("\n", 0, limit)
    if cut < limit - 200:  # \n 太遠 → 退而求其次找 `>`
        gt = text.rfind(">", 0, limit)
        lt = text.rfind("<", 0, limit)
        if gt > lt:  # 最後一個 `>` 在 `<` 後 → tag 完整 → 切點安全
            cut = gt + 1
        else:
            cut = lt  # 切點在 `<` 之前(把不完整的 tag 砍掉)
    if cut <= 0:
        cut = limit  # 沒有 tag 結構,硬切就硬切
    return text[:cut] + "\n<i>...(訊息過長已截斷)</i>"


def _humanize_age(seconds: float) -> str:
    """秒數 → 「2d 14h」「6h 30m」「45m」等。"""
    if seconds < 60:
        return "剛剛"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def format_order_card_compact(order: Dict[str, Any], age_seconds: float = 0.0) -> str:
    """v6.1:已出貨/已完成訂單的簡潔卡片(4 行,只給狀態更新 reply)。"""
    items = order.get("items") or []
    oid = (order.get("order_id", "") or "")[-10:]
    buyer = order.get("buyer_name", "") or (order.get("buyer_id", "")[:8] if order.get("buyer_id") else "")
    title = (items[0].get("title", "")[:30] if items else "")
    ship_id = items[0].get("shipping_id", "") if items else ""
    deliver_dt = items[0].get("deliver_datetime", "") if items else ""
    primary_method = items[0].get("shipping_method", "") if items else ""
    method = _METHOD_LABEL.get(primary_method, primary_method) or "宅配"
    icon = _status_icon(
        order.get("status", ""), order.get("payment_status", ""),
        order.get("status_label", ""), order.get("status_extra", ""),
    )
    age_str = f" · 已 {_humanize_age(age_seconds)}" if age_seconds > 60 else ""
    lines = [
        f"{icon} <b>#{_esc(oid)}</b> NT${order.get('amount', 0):,} · {_esc(buyer)}",
        f"🎁 {_esc(title)}",
    ]
    ship_line = f"🚚 {_esc(method)}"
    if ship_id:
        ship_line += f" <code>{_esc(ship_id)}</code>"
    ship_line += f" · {_esc(order.get('status_label',''))}{age_str}"
    lines.append(ship_line)
    if deliver_dt:
        lines.append(f"<i>{_fmt_dt(deliver_dt)} 出貨</i>")
    return "\n".join(lines)


def format_order_card_alert(order: Dict[str, Any], age_seconds: float = 0.0) -> str:
    """v6.1:逾期訂單高警報卡片。"""
    items = order.get("items") or []
    oid = (order.get("order_id", "") or "")[-10:]
    buyer_name = order.get("buyer_name", "")
    buyer_id = order.get("buyer_id", "")
    title = (items[0].get("title", "")[:40] if items else "")
    age_str = _humanize_age(age_seconds) if age_seconds > 60 else "剛剛"
    lines = [
        "🚨🚨🚨 <b>出貨逾期警告!</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"<b>訂單 #{_esc(oid)}</b> · NT${order.get('amount', 0):,}",
    ]
    if buyer_name:
        lines.append(f"👤 {_esc(buyer_name)} (<code>{_esc(buyer_id)}</code>)")
    elif buyer_id:
        lines.append(f"👤 <code>{_esc(buyer_id)}</code>")
    if title:
        lines.append(f"🎁 {_esc(title)}")
    lines.append("")
    lines.append(f"⚠️ <b>已逾期 {age_str},Yahoo 後台會降權,請立刻處理!</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_order_for_tg(
    order: Dict[str, Any],
    with_progress: bool = True,
    age_seconds: float = 0.0,
) -> str:
    """v6.1:訂單卡 HTML 格式(parse_mode=HTML,商品標題含 `*_[]<>&` 都安全)。

    v6.1 UX 分級:
    - overdue(逾期)→ alert 高警報版本
    - shipped/picked_up/completed(已出貨/完成)→ compact 簡潔版本
    - 其他(待出貨/待付款)→ 完整版本(原邏輯)

    age_seconds:當前狀態已停留秒數(從 last_updated_ts 算)
    """
    # v6.1:分級渲染
    cls = classify_order(
        order.get("status", ""), order.get("payment_status", ""),
        order.get("status_label", ""), order.get("status_extra", ""),
    )
    if cls == "overdue":
        return format_order_card_alert(order, age_seconds=age_seconds)
    if cls in ("shipped", "picked_up", "completed") and with_progress:
        # 已出貨類用 compact(降低資訊密度)— 但用戶若明確 with_progress=False 就走完整版
        return format_order_card_compact(order, age_seconds=age_seconds)
    # 其他用完整版(舊邏輯)
    oid = order.get("order_id", "")
    amount = order.get("amount", 0)
    status = order.get("status", "")
    payment = order.get("payment_status", "")
    status_label = order.get("status_label", "")
    status_extra = order.get("status_extra", "")
    pay_label = order.get("payment_label", "")
    buyer_name = order.get("buyer_name", "")
    buyer_id = order.get("buyer_id", "")
    items = order.get("items") or []

    icon = _status_icon(status, payment, status_label, status_extra)
    primary_method = items[0].get("shipping_method", "") if items else ""
    method_friendly = _METHOD_LABEL.get(primary_method, primary_method)

    lines = [
        f"{icon} <b>訂單 #{_esc(oid)}</b>",
        f"💰 NT${amount} | 📦 {_esc(status_label)}",
    ]
    if buyer_name:
        lines.append(f"👤 {_esc(buyer_name)} (<code>{_esc(buyer_id)}</code>)")
    elif buyer_id:
        lines.append(f"👤 <code>{_esc(buyer_id)}</code>")
    # 商品(多件全部展示,不只第 1 件;每件可能有獨立物流單號)
    # v6.1.44:每件商品下面顯示 D1 貨源資訊(閒魚/煤炉 + barcode + 連結),方便處理訂單
    def _d1_source_line(it: Dict[str, Any], indent: str = "") -> str:
        """為單一 item 生成 D1 貨源行(空字串 = 沒貨源資訊,該行 skip)。"""
        src = it.get("d1_source", "")
        if src not in ("xianyu", "mercari"):
            return ""
        src_label = "閒魚" if src == "xianyu" else "煤炉"
        barcode = (it.get("d1_barcode") or "").strip()
        src_url = (it.get("d1_source_url") or "").strip()
        parts = [f"{indent}📦 {src_label}"]
        if barcode and src == "xianyu":
            # 閒魚 barcode 是純數字 ID,顯示 code
            parts.append(f"<code>{_esc(barcode[:20])}</code>")
        if src_url:
            parts.append(f"<a href=\"{_esc(src_url)}\">[連結]</a>")
        return " · ".join(parts)

    if items:
        if len(items) == 1:
            it = items[0]
            title = _esc(it.get("title", "")[:60])
            lines.append(f"🎁 {title} x{it.get('quantity', 1)}")
            _d1 = _d1_source_line(it, indent="   ")
            if _d1:
                lines.append(_d1)
        else:
            lines.append(f"🎁 共 {len(items)} 件商品:")
            for it in items[:5]:
                title = _esc(it.get("title", "")[:40])
                ship_id = it.get("shipping_id", "")
                line = f"  • {title} x{it.get('quantity', 1)}"
                if ship_id:
                    line += f" <code>{_esc(ship_id)}</code>"
                lines.append(line)
                _d1 = _d1_source_line(it, indent="    ")
                if _d1:
                    lines.append(_d1)
            if len(items) > 5:
                lines.append(f"  ...還有 {len(items) - 5} 件")
    if method_friendly:
        lines.append(f"🚚 {_esc(method_friendly)}")

    # 進度條
    if with_progress:
        lines.append("")
        lines.append("<b>進度:</b>")
        lines.append(format_order_progress(order, age_seconds=age_seconds))

    # 完整狀態描述
    if status_extra and status not in ("waitForDelivery",):
        lines.append("")
        lines.append(f"<i>{_esc(status_extra)}</i>")
    lines.append("")
    lines.append(f"<i>更新: {__import__('time').strftime('%m/%d %H:%M')}</i>")
    return "\n".join(lines)


def format_order_buttons(order: Dict[str, Any], buyer_topic_id: int = 0,
                          group_chat_id: str = "", profile_id: str = "") -> list:
    """v6.2:訂單卡的 inline keyboard — 全改 callback_data 動作按鈕。

    舊版用 url 跳轉 Yahoo 後台 / Yahoo IM 網頁 → 需要對應 Chrome profile 登入才能用,
    對中間商(27 帳號)無意義。改成:
    - 🧾 詳情      → callback,後端 HTTP 抓最新訂單詳情 reply 到 topic
    - 💬 找買家    → callback,後端找/建該買家 TG topic + 回 deeplink(用戶在 TG 直接聊)

    callback_data 格式(64 byte 上限):
      oc:det:{profile_id}:{order_id}   — 詳情
      oc:buy:{profile_id}:{order_id}   — 聯繫買家(從訂單拿 buyer_id + 商品 + D1 貨源)

    Returns: [[button1, button2]] inline_keyboard 格式
    """
    oid = (order.get("order_id") or "").strip()
    buyer_id = (order.get("buyer_id") or "").strip()
    row1 = []
    if oid and profile_id:
        cb = f"oc:det:{profile_id}:{oid}"
        if len(cb.encode("utf-8")) <= 64:
            row1.append({"text": "🧾 詳情", "callback_data": cb})
    # v6.2:聯繫買家也用 order_id(handler 從訂單拉 buyer_id + 商品 + 貨源鏈接)
    if oid and buyer_id and profile_id:
        cb = f"oc:buy:{profile_id}:{oid}"
        if len(cb.encode("utf-8")) <= 64:
            row1.append({"text": "💬 找買家", "callback_data": cb})
    return [row1] if row1 else []


# v6.1:關鍵狀態變更時 reply 主卡的短摘要
def format_order_status_summary(order: Dict[str, Any], prev_status: str = "") -> str:
    """關鍵狀態變更摘要(reply 到主卡讓 user 感知變化)。"""
    status = order.get("status", "")
    payment = order.get("payment_status", "")
    items = order.get("items") or []
    deliver_dt = items[0].get("deliver_datetime", "") if items else ""
    ship_id = items[0].get("shipping_id", "") if items else ""

    icon = _status_icon(status, payment)
    label = order.get("status_label", "")
    summary = f"{icon} <b>狀態變更: {_esc(label)}</b>"
    if status == "delivered" and ship_id:
        summary += f"\n單號 <code>{_esc(ship_id)}</code>"
        if deliver_dt:
            summary += f" / {_fmt_dt(deliver_dt)}"
    elif status == "completed":
        summary += " ✓"
    elif status == "canceled":
        summary += "\n(訂單已取消)"
    return summary
