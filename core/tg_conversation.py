"""对话状态机 — 管理 买家→AI→卖家→买家 的完整链路

职责：
- 为每个新 IM 消息创建对话
- 调用已有的 commander_decide() 判断下一步
- 调用已有的 call_openai() 生成回复
- 通过 TelegramBot 发送通知和接收用户操作
- 管理对话生命周期（超时清理等）
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.ai_forwarder_feature import (
    call_openai,
    commander_decide,
    redact_sensitive,
    infer_source_platform_from_url,
    source_label,
    count_arrival_questions,
    guess_fragile,
    infer_can_buy,
    format_commander_banner,
    extract_latest_buyer_message,
)
from core.profile_lock import try_acquire, release, detect_chrome_profile_in_use
# v6.1.55:對話媒體權重 + 視頻首幀(賣家恆 1.0,買家衰減半衰期 10 分鐘)
from core.conversation_media import build_media_for_ai

# v6.1.27:訓練數據收集器(蒸餾使用者行為)
# 沒初始化也安全 — record_event() 內部會檢查 collector 是否存在
try:
    from core import training_collector as _TC
except Exception:
    _TC = None


def _tc_record(action_type: str, **kwargs) -> str:
    """訓練 event 紀錄. 任何異常吞掉,不影響主流程."""
    if _TC is None:
        return ""
    try:
        return _TC.record_event(action_type, **kwargs)
    except Exception:
        return ""


def _tc_supersede(target: str, by: str, reason: str = ""):
    if _TC is None or not target or not by:
        return
    try:
        _TC.mark_superseded(target, by, reason)
    except Exception:
        pass


# ---------- 商品链接/编号提取 ----------

# Yahoo 拍卖商品 URL → 提取编号（支持多种格式）
_RE_YAHOO_ITEM_URL = re.compile(
    r'https?://tw\.bid\.yahoo\.com/item/(\d+)', re.IGNORECASE,
)
# Yahoo 商品页其他可能的 URL 格式
_RE_YAHOO_ITEM_URL2 = re.compile(
    r'https?://tw\.bid\.yahoo\.com/[^\s]*[?&]id=(\d+)', re.IGNORECASE,
)
# 从纯文本中提取 10-12 位纯数字（Yahoo 商品编号格式）
_RE_YAHOO_ITEM_BARE = re.compile(r'\b(10\d{10})\b')
# 货源查询 API（D1 云端数据库）
_PRODUCT_QUERY_API = "https://product-query.<PHONE_REDACTED>.workers.dev/api/query"


def extract_yahoo_item_ids(text: str) -> List[str]:
    """从买家对话文本中提取 Yahoo 商品编号。去重，最多 3 个。

    支持：
    - 完整 URL: https://tw.bid.yahoo.com/item/880000000001
    - 带参数 URL: ...?id=880000000001
    - 纯数字编号: 880000000001（10开头的12位数字）
    """
    ids: List[str] = []
    seen: set = set()

    # 先从 URL 中提取
    for pattern in [_RE_YAHOO_ITEM_URL, _RE_YAHOO_ITEM_URL2]:
        for m in pattern.finditer(text or ""):
            item_id = m.group(1)
            if item_id not in seen:
                seen.add(item_id)
                ids.append(item_id)

    # 再从纯文本中提取 10 开头的 12 位数字
    for m in _RE_YAHOO_ITEM_BARE.finditer(text or ""):
        item_id = m.group(1)
        if item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)

    return ids[:3]


def _query_product_d1(item_code: str, on_log=None) -> Optional[Dict[str, str]]:
    """调用 D1 云端数据库查询货源信息。返回第一条结果或 None。

    最多重试 2 次，每次超时 15 秒。
    D1 返回字段: barcode, product_code, account, owner
    """
    import requests
    for attempt in range(2):
        try:
            resp = requests.get(
                _PRODUCT_QUERY_API, params={"code": item_code}, timeout=15,
            )
            data = resp.json()
            if data.get("found") and data.get("data"):
                return data["data"][0]
            return None
        except requests.exceptions.Timeout:
            if on_log:
                on_log(f"[TG] D1 query timeout (attempt {attempt+1})")
            if attempt == 0:
                continue
        except Exception as e:
            if on_log:
                on_log(f"[TG] D1 query error: {str(e)[:80]}")
            break
    return None


def _classify_source(barcode: str) -> Tuple[str, str]:
    """根据商品条码判断货源类型。返回 (source, source_url)。
    - Mercari: barcode 是 https://jp.mercari.com/... 链接
    - 闲鱼: barcode 是纯数字 ID
    注意:闲鱼返回桌面版 URL (www.goofish.com),供内部 Playwright 自动化(聊天/状态检测)使用。
    给用户在 TG 显示的链接请用 _to_mobile_xianyu_url() 转成移动版。
    """
    barcode = (barcode or "").strip()
    if barcode.lower().startswith("http") and "mercari.com" in barcode.lower():
        return "mercari", barcode
    if barcode.isdigit() and len(barcode) > 5:
        goofish_url = f"https://www.goofish.com/item?id={barcode}"
        return "xianyu", goofish_url
    return "unknown", ""


def _to_mobile_xianyu_url(url: str) -> str:
    """把桌面版闲鱼 URL 转成 h5.m 移动版 (手机和电脑都可打开)。

    桌面版 `https://www.goofish.com/item?id=X` 在手机浏览器打不开,
    必须用 `https://h5.m.goofish.com/item?forceFlush=1&id=X` 才支持两端。
    其它平台 URL (mercari/未知) 原样返回。
    """
    if not url or "goofish.com" not in url:
        return url
    m = re.search(r'[?&]id=(\d+)', url)
    if not m:
        return url
    return f"https://h5.m.goofish.com/item?forceFlush=1&id={m.group(1)}"


# ---------- 货源价格过滤 ----------

# 煤炉(Mercari) 价格：¥3,500 / ¥ 1,200 / 3,500円
_RE_MERCARI_PRICE = re.compile(
    r'[¥￥]\s?[\d,]+(?:\s*(?:円|税込|税抜|送料込))?'
    r'|[\d,]+\s*円(?:\s*(?:税込|税抜|送料込))?',
)
# 闲鱼价格：¥128 / ¥128.00
_RE_XIANYU_PRICE = re.compile(r'[¥￥]\s?[\d,.]+(?:\s*元)?')
# 通用标签价格：価格：¥5,000 / 售价：¥99 / 原价¥200
_RE_LABELED_PRICE = re.compile(
    r'(?:価格|售价|原价|現在価格|即決価格|出品価格|单价|單價)'
    r'\s*[:：]?\s*[¥￥$]?[\d,.]+'
)


def _strip_source_prices(text: str, source: str) -> str:
    """移除货源页面文字中的价格信息，避免 AI 看到进价。"""
    if not text:
        return ""
    s = text
    if source == "mercari":
        s = _RE_MERCARI_PRICE.sub("[价格已隐藏]", s)
    elif source == "xianyu":
        s = _RE_XIANYU_PRICE.sub("[价格已隐藏]", s)
    s = _RE_LABELED_PRICE.sub("[价格已隐藏]", s)
    return s


# ---------- 统一商品信息摘要 ----------

def _build_product_summary(conv) -> str:
    """构建统一的商品信息摘要，供 AI 判断和回复共用。

    数据规则：
    - 价格/运费 → 以 Yahoo 页面为准
    - 商品规格（尺寸、材质等）→ 以货源页面为准（价格已过滤）
    - 货源平台 → 仅内部参考

    v6.0.75:多商品場景 — 如果 conv.all_products 有多條,把每個商品的規格都列出來,
    AI 看得到所有商品的描述,自己判斷買家在問哪個。
    """
    parts: List[str] = []
    src = conv.product_urls[0]["source"] if conv.product_urls else ""

    # Section 1: Yahoo 页面信息(只有第一個 yahoo_id 的,目前未做多 yahoo page 抓取)
    if conv.yahoo_page_info:
        yp = conv.yahoo_page_info
        yahoo_lines = []
        price = yp.get("price", "")
        if price:
            yahoo_lines.append(f"Yahoo定價：{price}")
        ship = yp.get("shipping", "")
        if ship:
            yahoo_lines.append(f"Yahoo運費規則：{ship}")
        promo = yp.get("shipping_promo", "")
        if promo:
            yahoo_lines.append(f"Yahoo運費活動：{promo}")
        cond = yp.get("condition", "")
        if cond:
            yahoo_lines.append(f"Yahoo商品狀況：{cond}")
        if yahoo_lines:
            parts.append("【Yahoo頁面資訊（價格/運費以此為準）】")
            parts.extend(yahoo_lines)
    else:
        parts.append("【Yahoo頁面資訊】")
        parts.append("無法取得Yahoo頁面資訊。沒有定價資料。運費部分：我們商品默認免運（包含運費）。")

    # Section 2: 多商品 — 把所有商品的規格都列出來(AI 能判斷買家在問哪個)
    all_products = getattr(conv, "all_products", []) or []
    if len(all_products) > 1:
        parts.append("")
        parts.append(f"【共識別到 {len(all_products)} 個商品的規格/描述】")
        for idx, prod in enumerate(all_products, 1):
            yid = prod.get("yahoo_id", "")
            psrc = prod.get("source", "")
            ptext = prod.get("product_text", "") or ""
            can_buy = prod.get("can_buy", "未知")
            barcode = prod.get("barcode", "")
            parts.append("")
            parts.append(f"--- 商品 {idx} (Yahoo編號: {yid}) ---")
            parts.append(f"  貨源平台：{source_label(psrc)},頁面可購買：{can_buy}")
            if barcode:
                parts.append(f"  商品標題：{barcode}")
            if ptext:
                cleaned = _strip_source_prices(ptext, psrc)[:2500]
                parts.append(f"  規格/描述：{cleaned}")
            else:
                parts.append("  (此商品無詳細描述)")
    else:
        # 單商品 — 保持原行為
        product_text = conv.product_text or ""
        if product_text:
            cleaned = _strip_source_prices(product_text, src)[:4000]
            parts.append("")
            parts.append("【商品規格/描述（僅供參考商品本身資訊，價格已移除）】")
            parts.append(cleaned)

    # Section 3: 内部参考 — 多商品時也標出主商品
    meta = []
    if len(all_products) > 1:
        primary_yid = conv.product_urls[0].get("yahoo_id", "") if conv.product_urls else ""
        meta.append(f"主商品(買家最後問的)：Yahoo編號 {primary_yid},貨源 {source_label(src)}")
        meta.append("（如 AI 判斷買家問的是其他商品,reason 內請寫明「問商品 X 的賣家」)")
    else:
        meta.append(f"貨源平台：{source_label(src)}（僅供內部參考，絕對不可告知買家）")
    meta.append(f"頁面可購買：{conv.product_can_buy}")
    if conv.product_title and len(all_products) <= 1:
        meta.append(f"商品標題：{conv.product_title}")
    parts.append("")
    parts.append("【內部參考資訊】")
    parts.extend(meta)

    return "\n".join(parts)


# ---------- 安全的 Playwright route handler ----------

async def _safe_route_handler(route, request, block_types: set):
    """攔截不必要的資源，加快加載速度。
    用 try/except 吃掉 page/browser 關閉後殘留的 TargetClosedError。
    """
    try:
        if request.resource_type in block_types:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


# ---------- Playwright 残留异常静默处理 ----------

_PW_SILENCE_KWS = (
    "TargetClosedError", "Target page, context or browser has been closed",
    "Browser window not found", "Protocol error", "browser has been closed",
    "Target closed", "Session closed", "_closed_error",
)

def _install_pw_silence_handler():
    """在当前 event loop 上安装静默处理器，吞掉 Playwright 关闭时的残留异常噪音。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _handler(loop, context):
        exc = context.get("exception")
        if exc:
            s = f"{type(exc).__name__}: {exc}"[:300]
            for kw in _PW_SILENCE_KWS:
                if kw in s:
                    return
        msg = context.get("message", "")
        for kw in _PW_SILENCE_KWS:
            if kw in msg:
                return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# ---------- Yahoo 商品页面抓取（公开页面，匿名浏览器） ----------

def _get_browser_exe_path() -> str:
    """v6.1.27 修復:從 settings.json 拿系統 Chrome 路徑.

    bundled Python(v6.1.22)沒裝 playwright-chromium,
    chromium.launch 不帶 executable_path 會崩潰
    'BrowserType.launch: Executable doesn\\'t exist'.
    """
    try:
        from .accounts import load_settings
        p = str(load_settings().get("browser_path", "") or "").strip()
        return p
    except Exception:
        return ""


def _fetch_yahoo_item_page_http(item_id: str) -> Dict[str, Any]:
    """v6.1.27 純 HTTP 抓 Yahoo 商品頁(取代 Playwright,~300-500ms vs ~3-5s).

    Yahoo 把完整商品資料嵌在一個 `<script>{"item":{...}}</script>`,
    直接 regex+json.loads 即可,完全不需要 chromium.
    """
    import json as _json
    import re as _re_y
    url = f"https://tw.bid.yahoo.com/item/{item_id}"
    result: Dict[str, Any] = {}
    try:
        from curl_cffi import requests as _cfreq
        r = _cfreq.get(url, impersonate="chrome120", timeout=15, allow_redirects=True)
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}"
            return result
        text = r.text or ""
        # 抓內嵌商品 JSON
        m = _re_y.search(
            r'<script[^>]*>(\{"item":\{.+?\})</script>',
            text, _re_y.DOTALL,
        )
        if not m:
            result["error"] = "embedded json not found"
            return result
        d = _json.loads(m.group(1))
        item = d.get("item") or {}

        # ---- 抽欄位 ----
        title = item.get("title", "") or ""
        price = item.get("price")
        desc = item.get("description", "") or ""
        condition_int = item.get("condition", 0)
        # Yahoo condition 內部 int → 文字(實機 mapping)
        _COND_MAP = {1: "全新", 2: "九成新", 3: "八成新", 4: "七成新",
                     5: "六成新", 6: "五成新以下"}
        condition_str = _COND_MAP.get(condition_int, f"狀況代碼{condition_int}")

        # 運費:取 shippings 第一個 outline(常見「免運費」或「NT$60」等)
        shippings = item.get("shippings") or []
        shipping_lines = []
        for sh in shippings[:5]:
            if isinstance(sh, dict):
                nm = sh.get("name", "")
                ol = sh.get("outline", "")
                if nm and ol:
                    shipping_lines.append(f"{nm}: {ol}")
        shipping_text = " / ".join(shipping_lines) if shipping_lines else ""

        # 圖片:取每張 image 的 lg.src(高解析度);lg/md/sm 都是 {src, w, h} dict
        image_urls = []
        for im in (item.get("images") or [])[:8]:
            if not isinstance(im, dict):
                continue
            _u = ""
            for key in ("lg", "md", "sm"):
                v = im.get(key)
                if isinstance(v, dict):
                    _u = v.get("src", "")
                elif isinstance(v, str):
                    _u = v
                if _u:
                    break
            if _u and isinstance(_u, str) and _u.startswith("http") and _u not in image_urls:
                image_urls.append(_u)

        # 組 page_text 供 AI 看(替代 Playwright 拿到的 innerText)
        page_text_parts = []
        if title:
            page_text_parts.append(f"商品標題: {title}")
        if price is not None:
            page_text_parts.append(f"定價: ${price}")
        if shipping_text:
            page_text_parts.append(f"運費規則: {shipping_text}")
        page_text_parts.append(f"商品狀況: {condition_str}")
        if desc:
            page_text_parts.append(f"商品描述: {desc[:1500]}")
        page_text = "\n".join(page_text_parts)

        # 填 result(欄位對應 _fetch_product_info 的下游使用)
        result["page_text"] = page_text[:4000]
        if price is not None:
            result["price"] = f"${price}"
        if shipping_text:
            result["shipping"] = shipping_text[:300]
        result["condition"] = condition_str
        if title:
            result["title"] = title
        if image_urls:
            result["image_urls"] = image_urls
    except Exception as e:
        result["error"] = str(e)[:100]
    return result


async def _fetch_yahoo_item_page_async(item_id: str) -> Dict[str, Any]:
    """v6.1.27 改純 HTTP(取代 Playwright)— 跑在 thread 不阻塞 asyncio loop."""
    # 直接 thread 跑同步 HTTP(curl_cffi 是 blocking,~500ms)
    return await asyncio.to_thread(_fetch_yahoo_item_page_http, item_id)


def _next_nonempty(lines: List[str], start: int, max_skip: int = 3) -> str:
    """从 start 开始找下一个非空行，最多跳过 max_skip 行。"""
    for j in range(start, min(start + max_skip, len(lines))):
        s = lines[j].strip()
        if s:
            return s
    return ""


def _parse_yahoo_item_text(text: str, out: Dict[str, str]) -> None:
    """从 Yahoo 商品页面纯文本中提取关键字段。"""
    lines = text.splitlines()

    for i, line in enumerate(lines):
        ln = line.strip()

        if ln.startswith("運費規則"):
            val = _next_nonempty(lines, i + 1)
            if val:
                out["shipping"] = val[:300]

        if ln == "定價":
            val = _next_nonempty(lines, i + 1)
            if val.startswith("$"):
                out["price"] = val

        if ln == "商品狀況":
            val = _next_nonempty(lines, i + 1)
            if val:
                out["condition"] = val

        if ln == "運費活動":
            val = _next_nonempty(lines, i + 1)
            if val:
                out["shipping_promo"] = val[:200]


# ---------- 状态枚举 ----------

class ConvPhase(Enum):
    PENDING_AI = "PENDING_AI"              # AI 分析中
    PREVIEW_SENT = "PREVIEW_SENT"          # AI 草稿已发 TG，等用户操作
    WAIT_SELLER = "WAIT_SELLER"            # 需要卖家回复
    PREVIEW_SELLER = "PREVIEW_SELLER"      # AI 整合卖家回复后，等用户确认
    PREVIEW_SELLER_QUESTION = "PREVIEW_SELLER_QUESTION"  # AI 生成了问卖家的问题，等用户确认/修改
    AUTO_ASKING_SELLER = "AUTO_ASKING_SELLER"  # 正在自动向闲鱼卖家提问
    DONE = "DONE"                          # 完成
    EXPIRED = "EXPIRED"                    # 超时/跳过
    ERROR = "ERROR"                        # 出错


# ---------- 对话数据 ----------

@dataclass
class ConversationState:
    conv_id: str
    profile_id: str
    account_name: str
    chat_id: str           # Yahoo IM chat_id
    chat_url: str
    buyer_label: str
    buyer_text: str
    unread_count: int = 1

    phase: ConvPhase = ConvPhase.PENDING_AI
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    # AI 结果
    ai_action: str = ""          # AUTO_REPLY / NEED_SELLER 等
    ai_draft: str = ""           # AI 生成的草稿
    ai_internal_note: str = ""   # 内部备注
    # v6.1:議價結構化 hint(Commander 算好的,Writer 直接套對應 tier 話術)
    # {bid_amount, list_price, bid_ratio, suggested_tier}
    ai_pricing_hint: dict = field(default_factory=dict)
    # v6.1:Commander CoT(debug 用,看 Commander 怎麼推理路由)
    ai_thought_steps: List[str] = field(default_factory=list)

    # TG 消息追踪（记录所有发给 TG 的消息 ID，用于引用回复定位）
    tg_msg_ids: Set[int] = field(default_factory=set)

    # 卖家回复
    seller_answer: str = ""
    ai_integrated_draft: str = ""

    # 最终
    final_reply: str = ""
    error_msg: str = ""

    # 商品信息（从买家消息中提取的链接）
    product_urls: List[Dict[str, str]] = field(default_factory=list)
    product_text: str = ""
    product_can_buy: str = "未知"
    product_title: str = ""
    product_image_urls: List[str] = field(default_factory=list)

    # v6.1.54:買家在 Yahoo IM 發給賣家的圖片 URL list(用於 NEED_SELLER 場景中轉給閒魚賣家比對)
    # 由 _build_im_preview_items / BOSH 訊息解析填入,最多保留最近 5 張
    buyer_image_urls: List[str] = field(default_factory=list)

    # v6.1.55:完整對話媒體(含買家+賣家、圖+視頻、時間戳、訊息位置 — 供 AI 按權重看)
    # 每個 dict: {"url", "role" ("buyer"/"seller"), "ts" (createdUts ms), "msg_idx", "kind" ("image"/"video")}
    # weight 是 build_media_for_ai() 時動態算的(賣家恆 1.0,買家按時間衰減半衰期 10 分鐘)
    conversation_media: List[Dict[str, Any]] = field(default_factory=list)

    # 提取到的 Yahoo IDs(即使 D1 query 失敗也保留,供 _ensure_product_urls 重抓)
    pending_yahoo_ids: List[str] = field(default_factory=list)

    # Yahoo 商品页面信息（运费等）
    yahoo_page_info: Dict[str, str] = field(default_factory=dict)

    # v6.0.75:多商品場景 — 所有 yahoo_ids 對應的完整資訊
    # 每個元素含: {yahoo_id, source, source_url, can_buy, product_text, barcode, yahoo_page_info}
    # AI commander 能看到所有商品的描述,自己判斷要問哪一個
    all_products: List[Dict[str, Any]] = field(default_factory=list)

    # 卖家店铺 ID（用于构造 channelId 发送消息）
    shop_code: str = ""

    # 自动向闲鱼卖家提问
    auto_ask_question: str = ""        # AI 生成的问题
    auto_ask_fallback: bool = False    # 是否已降级为手动
    # v6.1.56:AI 判斷該附給賣家的買家圖 URL list(可被 user 預覽時切換)
    # AI commander 算出 indices 後,caller 從 buyer_image_urls 對應取 URL 存進來
    # 發送時用這個 list,不直接用 conv.buyer_image_urls(讓 user 有控制權)
    seller_question_images: List[str] = field(default_factory=list)
    seller_question_images_reason: str = ""  # AI 為什麼這樣選(供 user 預覽)

    # send-and-check 模式（发送后关闭浏览器，定期检查回复）
    seller_chat_url: str = ""              # 聊天页 URL（闲鱼）
    seller_msg_count: int = 0              # 发送后的消息数
    seller_sent_question: str = ""         # 实际发送的问题（简体）
    seller_check_count: int = 0            # 已检查次数
    seller_read_detected: bool = False     # 是否检测到已读（用于第二次通知）
    seller_check_timer: Optional[object] = None  # 定时检查 timer

    # v6.0.75 新增:HTTP 监控用 (替代 Playwright,15s 轮询不卡 profile 锁)
    seller_peer_user_id: str = ""          # 闲鱼对方 userId (从 chat_url 取)
    seller_session_id: str = ""            # 闲鱼对话 sessionId (session.sync 初次拿到)
    seller_baseline_version: int = 0       # 发送问题后的 version 基线
    seller_baseline_ts: int = 0            # 发送问题后的 ts 基线(毫秒)
    seller_http_mode: bool = False         # 是否走 HTTP 模式 (initial 用 Playwright 拿 baseline 后切 True)
    seller_http_fail_count: int = 0        # HTTP 连续失败次数(达到阈值降级回 Playwright)

    # v6.0.75 並發保護:訊息級去重 + 連續訊息累積 + 整合中標記
    seller_processed_msg_ids: set = field(default_factory=set)  # 已處理過的 messageId(防 WS/HTTP 雙路雙重整合)
    seller_extra_msgs: List[str] = field(default_factory=list)  # PREVIEW_SELLER 期間賣家補發的訊息(供用戶看到)
    seller_integrating: bool = False                            # AI 整合中標記(防同一 conv race condition)
    # v6.1.51:AI 自動回覆計數(改名拿掉底線,加進 persist)
    # 修「重啟後 AI 二次回覆又通知一次,私聊轟炸」bug
    seller_ai_count: int = 0

    # v6.0.83 賣家分多句發 debounce:第一條進 buffer 後啟 N 秒 timer,期間又發就重置 timer
    # 直到賣家停打字 N 秒才合併整批訊息給 AI 整合,避免「發一句就立刻出草稿」漏掉後續關鍵資訊
    seller_reply_buffer: List[str] = field(default_factory=list)
    seller_reply_debounce_timer: Optional[object] = None

    # v6.1.65 Fix A:進入 DONE 的時間戳,用於 reattach 24h window
    # 賣家在 DONE 後 24h 內補訊息/圖/視頻,reattach 回此 conv 走 integration
    # 由 _set_phase 統一寫入(DONE 才 set,EXPIRED/其他 phase 不 set)
    done_at_ts: float = 0.0


EXPIRE_SEC = 28800  # 8 小时超时
REATTACH_WINDOW_SEC = 86400  # v6.1.65:DONE 後 24h 內賣家補訊息可 reattach
DEDUP_SEC = 60     # 同一 chat_id 60 秒内去重（防止监控下一轮重复触发）

# 持久化(重啟後恢復 PREVIEW phase)— 跳過 set/timer 等運行時欄位
_CONV_PERSIST_FIELDS = (
    "conv_id", "profile_id", "account_name", "chat_id", "chat_url",
    "buyer_label", "buyer_text", "unread_count", "shop_code",
    "phase", "created_ts", "updated_ts",
    "ai_action", "ai_draft", "ai_internal_note",
    "seller_answer", "ai_integrated_draft", "final_reply", "error_msg",
    "product_urls", "product_text", "product_can_buy", "product_title",
    "product_image_urls", "yahoo_page_info", "all_products",
    # v6.1.54:買家發來的圖片 URL list,賣家提問時自動中轉
    "buyer_image_urls",
    # v6.1.55:完整對話媒體(買家+賣家、圖+視頻、時間戳)— 給 AI 按權重看
    "conversation_media",
    "auto_ask_question", "auto_ask_fallback",
    # v6.1.56:AI 判斷+user 切換的賣家附圖
    "seller_question_images", "seller_question_images_reason",
    "seller_chat_url", "seller_msg_count", "seller_sent_question",
    "seller_check_count", "seller_read_detected",
    "seller_peer_user_id", "seller_session_id",
    "seller_baseline_version", "seller_baseline_ts", "seller_http_mode",
    # v6.1.51:加 persist 修「重啟後重複整合 + AI 二次提醒轟炸」bug
    # seller_processed_msg_ids 是 set,需要 _persist_conv 把 set 序列化為 list
    # 還原時 _load_persisted_convs 把 list 還原為 set
    "seller_processed_msg_ids", "seller_ai_count", "seller_extra_msgs",
    # v6.1.65 Fix A:reattach 用的 DONE 時間戳(重啟後仍能套用 24h window)
    "done_at_ts",
)


def _smart_truncate(text: str, limit: int = 800) -> str:
    """截断长文本，保留开头和最新（末尾）的内容。"""
    if not text:
        return text or ""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head - 5
    return text[:head] + "\n...\n" + text[-tail:]


# ---------- AI 回复生成用的 system prompt ----------

_WRITER_SYSTEM_PROMPT = (
    "你是 Yahoo 拍賣的賣家本人,用繁體中文跟買家聊天。口吻像台灣真人主管 — 口語、極簡、直接。\n\n"

    "═══════════════ 1. 風格基線 ═══════════════\n\n"

    "【字數規範】\n"
    "- 一般回覆:8-20 字最理想\n"
    "- 需要解釋:25 字內為佳\n"
    "- 純確認/招呼:2-6 字(在的/不在了/收到/好的)\n"
    "- 寫到 30 字就停 — 超過會像 bot 背稿\n\n"

    "【語氣詞】\n"
    "1-2 個內自然搭配(捏/喲/唷/欸/啦/齁/嘿/喔/~)。\n"
    "真人主管平均 1 個,純確認句省略。\n\n"

    "【Emoji】\n"
    "業務對話 0 個。只有一個例外:道歉場景可用 🙏(例『不好意思讓您等這麼久 🙏』),整段最多 1 個。\n\n"

    "【標點 / 價格寫法】\n"
    "- 數字裸寫:『4800』『4800 塊』(用 $ 或元都不對 — 主管不這樣寫)\n"
    "- 底價尾巴一律加『免運』(避免買家誤會底價外還要付運費)\n\n"

    "═══════════════ 2. 五大鐵律(違反 = 整段廢)═══════════════\n\n"

    "【鐵律 #1 — 沒了 = 沒了(最高優先)】\n\n"
    "**這條是最常踩雷的鐵律,違反等於把買家騙來退款。所以要嚴格按 condition 觸發,不能腦補。**\n\n"
    "**Step 1 — 先判定買家最新一句的「意圖類型」(很重要!不能跳過):**\n"
    "  · 『賣嗎?/能賣?/可以賣嗎?/賣不賣?/可以嗎?/X 元賣嗎/X 元可以嗎』→ **議價追問**(不要走鐵律 #1)\n"
    "  · 『還在嗎?/有貨嗎?/在嗎?/還有嗎?/還能買嗎?/有現貨嗎?』→ **貨況詢問**(可能觸發鐵律 #1)\n"
    "  · 注意:即使買家短句裡含『賣』字(『賣嗎?』),只要前面有過議價脈絡,就是議價追問。看『賣』字 token 就觸發是錯誤的!\n\n"
    "**Step 2 — 議價追問處理(不走鐵律 #1):**\n"
    "  · 看 user_prompt 內的『議價建議檔位』section 套對應 tier 話術\n"
    "  · 沒給檔位 → 用 Yahoo 標價簡短婉拒(『標價 X 喔 真的不好再降了』)\n"
    "  · 絕對不要當作貨況詢問回『沒貨/賣掉了』(這會丟單)\n\n"
    "**Step 3 — 貨況詢問才看以下 condition:**\n"
    "  條件 A:對話歷史內**【賣家】**訊息(由【賣家】發送的那行)含『沒了/沒貨/已售出/賣完/找不到/這件不在了/出掉了/賣掉了』\n"
    "  條件 B:【頁面可購買】明確 = 否(已售出狀態)\n"
    "  條件 C:【頁面可購買】= 未知 → 不可自己編『有現貨』。買家問『還在嗎』可回『還在的』;但對話歷史有售出跡象就回『抱歉沒了』\n"
    "  → 條件 A 或 B 成立 → **買家後續不管問什麼(在嗎/有貨嗎/重新確認/還在嗎)第一句必須『抱歉 這件沒了』到此結束**\n"
    "  → 都不成立 → 回『還在的』\n\n"
    "**Step 4 — 嚴格 source check(必看):**\n"
    "  · 只看【賣家】(由【賣家】tag 開頭的訊息行)\n"
    "  · 買家訊息(【買家】開頭)裡含『沒/賣完』之類的字,**完全不算觸發**\n"
    "  · 商品標題裡的『賣完/已售』(如『絕版已售 X 萬』)**不算觸發**\n"
    "  · 賣家若沒明確說過沒貨 + 頁面可購買=是/未知 → 一律當『還在』\n\n"
    "觸發鐵律 #1 時的話術(避免冷冰冰):\n"
    "  · 『這個賣掉了 真的不好意思』\n"
    "  · 『不好意思 這個剛好被別人下了』\n"
    "  · 『抱歉 這件沒了 您看看其他款』\n"
    "  · 『這個之前賣掉了 忘記下架 不好意思捏』\n\n"

    "【鐵律 #2 — 物流時間照守則處理】\n"
    "守則 A:具體時間只從『賣家對話』或『商品描述』取,沒明說就用模糊話術。\n"
    "守則 B:賣家提到的產地,你跟著賣家立場說,不推翻不改寫:\n"
    "  · 賣家說『日本寄出』→ 你說『在日本寄出 大約一週左右送達』\n"
    "  · 賣家說『清關中』→ 你說『在清關中 這幾天會送達』\n"
    "守則 C:賣家沒提產地時依買家狀態:\n"
    "  · 詢價中『多久到』→『大約一週左右送達』\n"
    "  · 已下單『何時寄』→『已經安排處理 晚點會有貨態更新』\n"
    "  · 第二次以上追問還沒收到 →『我幫你查一下原因 馬上跟你回』\n"
    "(具體幾天到/明天到/今天寄這種數字承諾,只在賣家明說過時才能用)\n\n"

    "【鐵律 #3 — 規格只從來源抽】\n"
    "尺寸/材質/重量/年代/克數/盒子/證書/保固/配件這些細節:\n"
    "  · 答案在【商品規格/描述】或【賣家對話】 → 直接答\n"
    "  · 找不到 → 回『這個我看一下再回你』一句結束\n\n"
    "誠實話術(找不到時用,比硬編更受信任):\n"
    "  · 年代不確定 →『回收得到的 年代沒辦法追溯』『沒鑑定 不能確認』『按圖出貨』\n"
    "  · 真假/材質不確定 →『真銀假幣』『不確定材料』『回收得到 沒有檢測』\n"
    "  · 圈口/尺寸不確定 →『活口的 可調節』或『這個我看一下再回你』\n\n"
    "【✗ 禁止無中生有詞庫】沒有在【商品規格/描述】或【賣家對話】出現的細節,一律不准寫進回覆:\n"
    "  · 配件類:含原盒/含證書/含清潔刷/含鏈子/含掛繩/全套配件\n"
    "  · 尺寸類:具體 cm/直徑 40-50cm/19 圈口/2 公分寬/卡 16 厚 7 等任意尺寸數字\n"
    "  · 重量類:具體克數(8 克/2.84 克/30 多克)\n"
    "  · 狀態類:全新/未使用/全新無瑕/幾成新/二手/有/沒有/帶/不帶 任何盒、套、章、繩\n"
    "  · 工藝類:手繪/印刷/手工/機制/真品/保真/保代\n"
    "另:標題裡的描述詞(如『木紋金彩水波紋』)是花紋名,當花紋處理就好(不是材質,不能當規格答)。\n\n"

    "【鐵律 #4 — 對外口徑統一】\n"
    "對買家的口徑:商品是我們自己的、是現貨。\n"
    "可說的二手來源:『回收得到/舊藏/家中收的』(這是合法的二手描述,不等於透露貨源平台)。\n"
    "貨源平台(煤爐/Mercari/閒魚/淘寶/代購/轉賣/日本購入 等)— 對外永遠當作我們是賣家。\n"
    "系統術語也不適合對買家說:『連結抓不到』『頁面沒有顯示』『系統無法取得』『可購買狀態未知』。\n"
    "編造活動也不行:『有運費券活動』『有優惠券可用』『有滿額活動』。\n\n"

    "【鐵律 #5 — 賣家立場不倒退 + 看對話最底部買家訊息】\n"
    "對話中【賣家】已表態的價格/讓步/連結 — 後續必須延續:\n"
    "  · 賣家已說『最低 4800』→ 買家再問回『最低 4800 免運』\n"
    "  · 賣家已讓『可以給你 1600』→ 不可回到原標價\n"
    "  · 賣家已換連結 → 跟著走\n"
    "  · ✗ 不要回『4800 真的沒辦法』(賣家自己已答應這個價,AI 反悔會精分,客戶會質疑)\n\n"
    "**找到對話最底部的【買家】訊息,只回那一句的關鍵問題**(不要回前面的舊問題)。\n"
    "**買家訊息最後若貼了商品連結可能只是簽名檔重複**,關注文字問題本身,不要被連結干擾。\n\n"

    "═══════════════ 3. 議價檔位(系統會在 user_prompt 給你 tier)═══════════════\n\n"

    "Commander 已算好 bid_ratio + suggested_tier。**照系統給的 tier 寫**,不用自己重算:\n\n"

    "**accept_or_minor_haggle**(R ≥ 0.85)— 接近標價可成交\n"
    "  · 標 1755 買家 1500(R=0.85)→『最低 1600 免運』(讓 100 接近成交)\n"
    "  · 標 12650 買家 11000(R=0.87)→『12000 吧』(讓 5%)\n\n"

    "**counter_offer**(0.70 ≤ R < 0.85)— 反提折中價\n"
    "  · 標 5460 買家 4000(R=0.73)→『最低 4800 免運』\n"
    "  · 標 15962 買家 12000(R=0.75)→『最低 13800 免運』\n\n"

    "**firm_refuse**(R < 0.70)— 軟拒 + 可附底價拉回\n"
    "  · 標 5350 買家 2000(R=0.37)→『2000 沒辦法欸 最低 3500 免運』\n"
    "  · 標 8625 買家 500(R=0.06)→『差太多了捏 抱歉』(極離譜不報底價)\n\n"

    "**general_refuse**(沒具體數字)— 複述標價婉拒\n\n"

    "**議價場景禁忌(無資訊量的空話,不要加)**:\n"
    "  ✗ 運費套話:『超商免運/7-11 都免運/運費都有含』(議價時買家沒問運費)\n"
    "  ✗ 空話尾巴:『價格已經很實在了』『已經是最低了』(沒資訊量)\n"
    "  ✗ 反問:『你想出多少』『你能接受多少』(答不出直接套規則)\n"
    "  ✗ R ≥ 0.85 還回『1500 真的沒辦法』『11000 真的沒辦法』(讓幅合理硬拒會跑單)\n"
    "  ✗ R 0.70-0.85 還回『4000 真的沒辦法 這個價格已經很實在』(沒讓步等於沒回答)\n\n"

    "═══════════════ 4. 系統規則硬知識 ═══════════════\n\n"

    "【超商門市限制】(買家問就答)\n"
    "  · 尺寸:單邊 ≤ 45cm,三邊合計 ≤ 105cm(超過只能黑貓宅配)\n"
    "  · 「取貨不付款」限額 4000 以下\n\n"

    "【物流方式問答】(各一句,不解釋)\n"
    "  · 到付 →『到付沒辦法唷』\n"
    "  · 面交 →『抱歉 沒有面交服務』\n"
    "  · 郵局 →『我們宅配只能黑貓』\n"
    "  · 自取 →『沒有實體店面 沒辦法自取』\n\n"

    "【產地分級話術】\n"
    "  · 商品描述/賣家對話明確說在日本 →『這件在日本倉 大約 10-15 天到台灣』\n"
    "  · 明確說在香港 →『香港倉 7 天左右』\n"
    "  · 未明確或其他海外 →『這件在海外倉 下單後 7-10 天寄達』\n"
    "  · 明確說在台灣才講台灣\n"
    "  · 永遠不說『大陸倉/大陸出貨』(台灣買家敏感詞)\n\n"

    "═══════════════ 5. 邊界場景 ═══════════════\n\n"

    "**多商品連結**(對話內 2+ 個 yahoo/item 連結)→ 回『稍等我看一下再告訴你』。系統只拿到第一個連結資料,挑一個會答錯。\n\n"
    "**合購/多件總價** → 回『我看一下再告訴你』結束。不在 prompt 內算。\n\n"
    "**單個 vs 一組不確定** → 回『這是單個價錢』或『單個』(大多商品默認單價)。\n\n"
    "**投訴/爭議**(假/騙/瑕疵/破損/詐騙)→ 第一句『實在抱歉』或『不好意思』+『馬上幫你查一下原因』結束。\n\n"
    "**跨店連結**(買家貼別家連結比價)→ 回『那件不是我的 我這邊最低 X 免運』維持自己立場。\n\n"
    "**極低議價對已售出商品**(『3000?』對已售出)→ 回『抱歉 議價也沒有這麼誇張的』結束。\n\n"
    "**閒聊/八卦**(買家聊盜圖店/平台活動)→ 只回跟訂單/商品有關的事,不接閒聊。\n\n"
    "**已下標確認**(下單後客套)→ 可說『會幫你包好再出貨』『單號晚點貼給你』(這是已成交客套,跟詢價階段不同)。\n\n"
    "**主動承諾的紅線**:詢價階段不要說『我幫你保留』『我會優先處理』『出貨前拍照』。已下標後才能用客套話。\n\n"
    "**結尾話術**:套話省略(『如有其他問題,隨時告訴我』『感謝您的支持』這類客套對主管口吻違和,別接)。\n\n"
    "**運費提及**:買家沒問運費就不要主動提『7-11/萊爾富/超商/免運』。只在買家明確問『自取/面交/怎麼寄』時才回。\n\n"

    "═══════════════ 6. 媒體訊息標記 ═══════════════\n\n"

    "對話中可能出現這些前綴(AI 自動處理的標記,不是對方原話):\n"
    "  · [語音→文字] xxx — 對方語音 AI 轉寫,可能同音字錯誤,理解大意即可\n"
    "  · [視頻→描述] xxx — 對方視頻 AI 看影像寫的描述\n"
    "  · [圖片] URL — 只有 URL\n"
    "  · [語音 無法轉寫] / [視頻 無法解析] — 解析失敗,若關鍵資訊請建議買家『方便打字補充嗎』\n"
    "處理:轉寫結果當提示用,不當原話引用(別寫『您剛才說 xxx』,語音可能有錯字)。\n\n"

    "═══════════════ 7. Few-shot 範例(嚴格模仿這種簡潔度)═══════════════\n\n"

    "<example><buyer>請問還在嗎?</buyer><reply>還在的</reply></example>\n"
    "<example><buyer>有現貨嗎?(賣家已說沒了)</buyer><reply>抱歉 這件沒了</reply></example>\n"
    "<example><buyer>賣嗎?(前面已議價過 7800,標 8880,屬議價追問非貨況)</buyer><reply>標價 8880 喔 真的不好再降了</reply></example>\n"
    "<example><buyer>能賣嗎?(沒議價脈絡,標 5000)</buyer><reply>還在的 5000 喔</reply></example>\n"
    "<example><buyer>賣嗎?(前面有 3000 出價,系統 tier=firm_refuse,標 6900)</buyer><reply>3000 真的沒辦法欸 最低 5800 免運</reply></example>\n"
    "<example><buyer>X 元賣嗎/X 元可以嗎(議價追問,千萬別回『賣掉了』)</buyer><reply>(按 pricing_hint 給的 tier 寫,或標價婉拒『標 X 喔 不好再降了』)</reply></example>\n"
    "<example><buyer>盒、證書都在嗎?</buyer><reply>全品 帶原木供箱 沒證書</reply></example>\n"
    "<example><buyer>5500 讓藏嗎(標價 9075,tier=counter_offer)</buyer><reply>最低 8000 免運</reply></example>\n"
    "<example><buyer>1500 割愛(標價 1755,tier=accept_or_minor_haggle)</buyer><reply>最低 1600 免運</reply></example>\n"
    "<example><buyer>3000 可以?(標價 6900,tier=firm_refuse)</buyer><reply>3000 真的沒辦法欸 抱歉</reply></example>\n"
    "<example><buyer>最低多少?(賣家已說底價 4800)</buyer><reply>最低 4800 免運</reply></example>\n"
    "<example><buyer>2 件合購多少?</buyer><reply>我看一下再告訴你</reply></example>\n"
    "<example><buyer>是單個還是一組?</buyer><reply>單個</reply></example>\n"
    "<example><buyer>在台灣嗎?(商品描述提日本)</buyer><reply>這件在日本倉 大約 10-15 天到台灣</reply></example>\n"
    "<example><buyer>可以面交嗎?</buyer><reply>抱歉 沒有面交服務</reply></example>\n"
    "<example><buyer>可以寄郵局嗎?</buyer><reply>我們宅配只能黑貓</reply></example>\n"
    "<example><buyer>可以自取嗎?</buyer><reply>沒有實體店面 沒辦法自取</reply></example>\n"
    "<example><buyer>到付可以嗎?</buyer><reply>到付沒辦法唷</reply></example>\n"
    "<example><buyer>何時出貨?/寄了嗎?</buyer><reply>已經安排處理 晚點會有貨態更新</reply></example>\n"
    "<example><buyer>保真嗎?</buyer><reply>保真銀</reply></example>\n"
    "<example><buyer>是真品嗎?(LV 等奢侈品)</buyer><reply>這件沒鑑定 不能確認 是回收得到的</reply></example>\n"
    "<example><buyer>是新疆和闐玉嗎?</buyer><reply>是的 新疆和闐玉籽料 原皮原色</reply></example>\n"
    "<example><buyer>年代?(不確定)</buyer><reply>抱歉 這個沒有確定年代</reply></example>\n"
    "<example><buyer>重量幾克?</buyer><reply>2.84 克(或『重量沒有秤過 不過很重』如果沒數據)</reply></example>\n"
    "<example><buyer>我下標了</buyer><reply>好的 收到</reply></example>\n"
    "<example><buyer>下單了 今天全家繳款</buyer><reply>好的 繳完款系統入帳我就幫你安排出貨 會幫你包好再寄出</reply></example>\n"
    "<example><buyer>謝謝!/收到 謝謝/好的</buyer><reply>不客氣喔</reply></example>\n"
    "<example><buyer>了解/我再想想/我參考看看</buyer><reply>好的 謝謝你唷</reply></example>\n"
    "<example><buyer>不買了/不好意思 掰掰</buyer><reply>好的 有需要再跟我說</reply></example>\n"
    "<example><buyer>不好意思讓您久等(催物流)</buyer><reply>早安 不好意思讓您等這麼久 🙏 今天會安排出貨</reply></example>\n"
    "<example><buyer>麻煩包裝好</buyer><reply>好的 沒問題</reply></example>\n"
    "<example><buyer>他回我說沒貨 哈哈哈(買家聊其他店家八卦)</buyer><reply>是的 我這邊已經安排寄出了 請等待貨態更新</reply></example>\n\n"

    "═══════════════ 8. 最常踩雷的 4 種對比 ═══════════════\n\n"

    "**踩雷 1:賣家已說沒了卻回還在**\n"
    "  ✗ 還在喔 可以直接下標\n"
    "  ✓ 這個賣掉了 真的不好意思\n\n"

    "**踩雷 2:規格找不到卻編配件**\n"
    "  ✗ 對的 一套 5 把 含原盒跟清潔刷唷\n"
    "  ✓ 對的 一套 5 把\n\n"

    "**踩雷 3:物流時間編具體幾天**\n"
    "  ✗ 我會盡快寄出 1-2 個工作天就到\n"
    "  ✓ 已經安排處理 晚點會有貨態更新\n\n"

    "**踩雷 4:賣家已表態的價硬撐標價**\n"
    "  ✗ (賣家已說『最低 4800』)這款目前就是 6875 喔 真的沒辦法再低了\n"
    "  ✓ 最低 4800 免運\n"
)

_INTEGRATE_SYSTEM_PROMPT = (
    "你是Yahoo拍賣賣家的客服助手,角色是『真人主管』口吻(不是 bot)。\n"
    "賣家已私下回覆了你問的問題,現在你要把答案改寫成給 Yahoo 買家的回覆。\n"
    "全程繁體中文,1-4 句,口語化(像跟朋友講話,不用敬語套話)。\n"
    "不出現『賣家說/我確認了/我問過了』 — 直接像自己回答。\n\n"

    "═══════════════ 鐵律(跟 Writer 一致,不准違反)═══════════════\n\n"

    "【鐵律 #1 — 對外口徑統一】\n"
    "對買家的口徑:商品就是我們自己的、是現貨。\n"
    "提到貨源平台/代購/轉賣/日本購入/中國/閒魚/煤爐/淘寶等任一字眼 → 違規\n\n"

    "【鐵律 #2 — 物流時間模糊化】\n"
    "買家沒明確問就不主動提到貨時間。\n"
    "買家問了 → 只說『下單後我們會盡快安排出貨喔』之類模糊話術。\n"
    "賣家就算說了具體日期(如『明日発送』『3 天到』),你也不要寫死數字到回覆裡(賣家也無法保證跨境時間)。\n\n"

    "【鐵律 #3 — 沒了 = 沒了】\n"
    "賣家若說『沒有/沒貨/賣完/沒了/已售/分かりかねます/沒在賣了』,**立刻告知買家沒貨**,不要拖。\n"
    "範例輸出:『不好意思這個剛剛沒了,要不要看看其他的~』\n\n"

    "【鐵律 #4 — 不確認 = 不確認】\n"
    "賣家若說『不清楚/不知道/沒辦法確認/わからない』,如實告知買家『不好意思這個我也沒辦法確認到耶』\n"
    "禁止編造一個確定的答案。\n\n"

    "【鐵律 #5 — 議價不私下加碼】\n"
    "賣家給了一個價(如『8000』),回覆時就用 8000,不要自作主張少 100 或多 100。\n"
    "賣家沒給價但你發現買家在議價 → 用 Yahoo 標價婉拒,不要報任何新數字。\n\n"

    "═══════════════ 賣家回覆預處理 ═══════════════\n\n"

    "從賣家對話抽『關鍵事實』,過濾以下噪音:\n"
    "- 用戶名稱呼(XXX様/XXXさん/XXX 親) → 忽略,這是平台用戶名不是商品資訊\n"
    "- 寒暄客套(您好/感謝詢問/麻煩您) → 忽略,只留事實\n"
    "- 語言:賣家可能用日文或簡中,你輸出繁中\n"
    "- 賣家對話含 [語音→文字]/[視頻→描述] 是 AI 自動轉寫,可能同音字錯誤,理解大意即可\n"
    "- 賣家對話含 [語音 無法轉寫]/[視頻 無法解析] → 解析失敗,如實告知買家『這部分我再確認看看』\n"
    "- 視頻描述提到的瑕疵/細節可以引用,但別當賣家親口承諾,語氣保留『看起來/應該是』\n\n"

    "═══════════════ 輸出範例 ═══════════════\n\n"

    "範例 1(賣家給了尺寸):\n"
    "賣家:『長 35 寬 23.5 高 3.6,一共 2 個』\n"
    "→ 輸出:『尺寸長 35 寬 23.5 高 3.6 公分,共 2 個喔』\n\n"

    "範例 2(賣家說沒了):\n"
    "賣家:『この商品はもう売り切れました』\n"
    "→ 輸出:『不好意思這個已經沒有了耶,要不要看看其他的~』\n\n"

    "範例 3(賣家不確定):\n"
    "賣家:『年代分かりかねます』\n"
    "→ 輸出:『不好意思年代這個我也沒辦法確認到耶』\n\n"

    "範例 4(議價賣家給了價):\n"
    "賣家:『最低 8000』\n"
    "→ 輸出:『最低 8000 喔,可以的話下標就幫您處理~』\n"
)

_SELLER_QUESTION_SYSTEM_PROMPT = (
    "把买家的问题转成闲鱼买家的口吻，直接问卖家。\n"
    "核心原则：忠实于买家原话，只做语气转换和敏感词过滤，不要自己发挥或添加新问题。\n\n"
    "【关键 — 只翻译真正的「问题」】\n"
    "买家对话可能包含:\n"
    "- 对你(我们)的回应/肯定:「可以」「好的」「收到」「谢谢」「OK」「下吧」「下標吧」「不用了」「不要了」「行」「嗯」\n"
    "- 真正要问卖家的问题:「这个产地哪里」「能便宜吗」「还有现货吗」\n"
    "你只翻译【真正要问卖家的问题】部分,**完全忽略肯定/回应**(那是买家回我们,不是问卖家)。\n"
    "例如:输入「可以 你下標吧 這個是產地哪里的」→ 只翻「这个产地哪里的呀?」\n"
    "如果对话里只有肯定/回应没有真问题,输出「请问还在吗?」(兜底,保留对话)\n\n"
    "语气：像真实买家在闲鱼随口问的，口语化、简短。\n"
    "过滤：去掉 Yahoo、奇摩、拍卖、台湾、代购、转卖、超商取货。\n"
    "禁止：不要重复商品标题/价格、不要加前缀/解释/括号、不要自己编问题。\n"
    "只输出一句话。\n"
)

_SELLER_QUESTION_JA_SYSTEM_PROMPT = (
    "買い手の質問を日本語に翻訳して、メルカリの購入者の口調にしてください。\n\n"
    "【関鍵 — 本当の「質問」だけを翻訳】\n"
    "買い手の会話には次のような内容が混在する可能性があります:\n"
    "- 我々への返事/肯定:「可以」「好的」「收到」「OK」「下標吧」「不用了」「不要了」\n"
    "- 売り手に聞きたい本当の質問:「這個產地哪裡」「能便宜嗎」「有現貨嗎」\n"
    "【本当の質問の部分のみ翻訳】、肯定/返事は完全に無視してください(それは買い手が私たちに返事しただけ、売り手への質問ではない)。\n\n"
    "【最重要ルール】\n"
    "- 「買い手が知りたいこと」が提供されている場合、それを忠実に日本語の質問にする。\n"
    "- 例：「內徑尺寸」→「内径のサイズを教えていただけますか？」\n"
    "- 例：「重量」→「重さを教えていただけますか？」\n"
    "- 具体的なキーワード（サイズ、内径、重さ等）は必ず質問に含める。「詳細」等の曖昧な言葉に置き換えない。\n\n"
    "【絶対禁止】\n"
    "- 「購入可能ですか」「まだ購入できますか」は絶対に出力しない。\n"
    "- 「詳細を教えて」等の曖昧な質問にしない。買い手が聞いている具体的な内容を聞く。\n"
    "- 「仕入れ先に確認」「確認していただけると」等は絶対に出力しない。あなたが話しかけている相手が商品の持ち主本人です。\n"
    "- 買い手が聞いていない質問を勝手に作らない。\n\n"
    "口調：メルカリで普通の購入者が聞くような自然な日本語。\n"
    "除外：Yahoo、オークション、台湾、代購、転売。\n"
    "禁止：商品名・価格の繰り返し、前置き・説明・括弧。\n"
    "出力形式：日本語の質問|中国語の翻訳\n"
    "例：内径のサイズを教えていただけますか？|请问内径尺寸是多少？\n"
)


# ---------- ConversationManager ----------

class ConversationManager:
    """管理所有活跃对话，协调 TG Bot ↔ AI ↔ 监控。"""

    def __init__(self, tg_bot, ai_config: Dict[str, Any], on_log: Callable[[str], None],
                 chrome_path: str = "", base_dir: str = "",
                 supervisor_config: Optional[Dict[str, str]] = None):
        """
        Args:
            tg_bot: TelegramBot 实例
            ai_config: {"api_key", "base_url", "endpoint_mode", "model", "redact"}
            on_log: 日志回调
            chrome_path: Chrome 浏览器路径
            base_dir: 项目根目录
            supervisor_config: {"token": "...", "chat_id": "..."} 主管 Bot 配置
        """
        self.tg = tg_bot
        self.ai = ai_config
        self.on_log = on_log
        self._chrome_path = chrome_path
        self._base_dir = base_dir
        self.monitor = None  # 由 app.py 在创建 MonitorManager 后设置

        # 主管 Bot：转发关键消息给主管（用于收集训练数据）
        self._sv_token = ""
        self._sv_chat_id = ""
        if supervisor_config:
            self._sv_token = (supervisor_config.get("token") or "").strip()
            self._sv_chat_id = (supervisor_config.get("chat_id") or "").strip()

        self._lock = threading.Lock()
        self._profile_send_locks: Dict[str, threading.Lock] = {}
        self._profile_send_locks_mu = threading.Lock()
        self._convs: Dict[str, ConversationState] = {}  # conv_id -> state
        # phase_buttons 最新訊息位置 — 區分 forum/私聊,避免 _clear_buttons 用錯 bot edit
        # conv_id -> (kind: "forum"|"private", msg_id: int, topic_id: int)
        self._phase_button_target: Dict[str, tuple] = {}
        self._chat_map: Dict[str, str] = {}              # "profile|chat" -> conv_id
        self._chat_text_hash: Dict[str, str] = {}         # "profile|chat" -> md5(buyer_text)

        # 翻译模式状态: chat_id -> "zh2ja" | "ja2zh" | None
        self._translate_mode: Dict[str, str] = {}

        # v6.0.74 新增:force_reply 输入收集器
        # prompt_msg_id (force_reply 提示消息的 id) -> (conv_id, action)
        # 用户引用回复该提示消息时,根据 action 走对应逻辑(edit/rewrite/reply/edit_q)
        self._pending_inputs: Dict[int, tuple] = {}
        self._pending_inputs_lock = threading.Lock()

        # v6.0.78 新增:chat-level「最近一個 pending」備援
        # TG Desktop 桌面版的 force_reply 經常不會自動觸發引用 → reply_to_msg_id 為空 →
        # 走到通用命令路徑 → 回「help 訊息」讓使用者困惑
        # 解法:在 _pending_inputs 之外多記一份「最近 5 分鐘內某 chat 的 pending」,
        # 收到無 reply_to 但不像命令的訊息時 fallback 消耗該 pending
        # chat_id -> (conv_id, action, ts, prompt_msg_id)
        self._latest_pending_by_chat: Dict[str, tuple] = {}
        self._latest_pending_lock = threading.Lock()
        self._latest_pending_ttl_sec = 300  # 5 分鐘

        # v6.1.20:一鍵轉刊 — chat_id -> RelistSession
        self._relist_sessions: Dict[str, Any] = {}
        self._relist_sessions_lock = threading.Lock()

        # v6.0.75:WebSocket inbound 訊息 dispatcher
        # cid (sessionId@goofish) -> [conv_id, ...]
        # 同一個閒魚商品被多個 Yahoo 買家詢問時會撞 cid,改用 list 廣播給所有 active conv
        self._ws_cid_to_conv: Dict[str, List[str]] = {}
        # peer_uid -> [conv_id, ...](備援:cid 沒命中時用 sender_uid 找)
        self._ws_peer_to_conv: Dict[str, List[str]] = {}
        self._ws_map_lock = threading.Lock()
        self._ws_started = False  # 全局 WS 連線是否啟動過

        # v6.0.75:限流保護 — 同 cid 的 listUserMessages 5s throttle
        # (server 對同個對話 typing/輸入完/已讀 可能短時間推多次 40006)
        self._ws_fetch_last_ts: Dict[str, float] = {}
        self._ws_fetch_throttle_lock = threading.Lock()

        # v6.0.81:媒體解析(STT/視頻 GPT vision)異步 executor
        # WS callback 在 asyncio loop 中被 sync invoke,媒體解析阻塞 5-30s 會卡心跳(15s)→ server 斷連
        # 媒體消息走獨立 thread,WS loop 立即返回繼續心跳/收 push
        # max_workers=2:允許並發處理 2 個媒體,實機極少 1 秒內收 3+ 條媒體
        from concurrent.futures import ThreadPoolExecutor
        self._media_inbound_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ws-media-inbound",
        )

        # 注册 TG 回复回调
        self.tg.on_message = self._on_tg_reply
        self.tg.on_callback = self._on_tg_callback

        # v6.0.83:TG forum 整合 — 每個 Yahoo 對話 ↔ TG forum topic 雙向同步
        # settings.tg_forum_enabled + tg_forum_chat_id 控制
        self.forum_bridge = None
        try:
            from core.tg_forum import build_forum_bridge_from_settings
            self.forum_bridge = build_forum_bridge_from_settings(
                Path(self._base_dir), on_log=self.on_log,
            )
            if self.forum_bridge:
                # 註冊 forum topic 回覆 callback(雙路徑 — KV poller + forum_bot polling)
                # 1. ai_bot KV poller 路徑(若 Worker forward supergroup 訊息)
                self.tg.on_forum_message = self._on_tg_forum_reply
                self.tg.forum_chat_id = str(self.forum_bridge.bot.forum_chat_id)
                # 2. forum_bot 獨立 polling(example_forum_bot,沒設 webhook,直接接 supergroup)
                self.forum_bridge.bot.on_forum_message = self._on_tg_forum_reply
                # callback_query(topic 內 inline button 點擊)走既有 _on_tg_callback handler
                self.forum_bridge.bot.on_callback_query = self._on_tg_callback
                self.forum_bridge.bot.start_polling()
                self.on_log(
                    f"[TG-FORUM] bridge 已啟用 supergroup={self.tg.forum_chat_id}"
                )
                # v6.1.20:啟動時刷新訂單中心置頂訊息(更新按鈕,加上一鍵轉刊提示)
                try:
                    ok = self.forum_bridge.refresh_order_center_pin()
                    if ok:
                        self.on_log("[TG-FORUM] 訂單中心置頂訊息已刷新 (含新按鈕)")
                except Exception as _e_pin:
                    self.on_log(f"[TG-FORUM] refresh pin 異常(忽略): {_e_pin}")
        except Exception as _e_forum:
            self.on_log(f"[TG-FORUM] bridge 建立失敗(forum 功能停用): {_e_forum}")
            self.forum_bridge = None

        # v6.0.83 啟動時還原 PREVIEW phase 對話(重啟不丟)
        try:
            self._load_persisted_convs()
        except Exception as _e_load:
            self.on_log(f"[CONV-RESTORE] load 異常(忽略): {_e_load}")

        # v6.0.83:TG forum menu(/accounts /buyers /history 命令面板)
        # 每個軟件實例獨立綁定一個 user(settings.tg_chat_id),只負責 accounts.json 內的帳號
        # 不需要 ACL — 此實例的所有 accounts.json 帳號預設都對綁定的 user 開放
        # (僅在 tg_user_accounts.json 存在時才啟用多用戶 ACL,供未來「共用軟件」場景)
        self._tg_acl = None
        self._forum_menu = None
        # forum reply force_reply pending: prompt_msg_id → {profile_id, channel_id, kind, ts}
        self._forum_pending_replies: Dict[int, Dict[str, Any]] = {}
        self._forum_pending_lock = threading.Lock()
        try:
            from core.tg_forum_menu import TGForumMenu
            acl = None
            try:
                acl_path = Path(self._base_dir) / "tg_user_accounts.json"
                if acl_path.exists():
                    from core.tg_user_acl import TGUserACL
                    acl = TGUserACL(Path(self._base_dir))
                    self._tg_acl = acl
                    self.on_log(
                        f"[TG-MENU] 多用戶 ACL 啟用 (users: {len(acl.list_known_users())})"
                    )
            except Exception:
                acl = None
            self._forum_menu = TGForumMenu(
                Path(self._base_dir),
                on_log=self.on_log,
                acl=acl,
            )
            mode = "ACL 模式" if acl else "單用戶模式(列所有 accounts.json 帳號)"
            self.on_log(f"[TG-MENU] forum menu 已啟用 — {mode}")
        except Exception as _e_menu:
            self.on_log(f"[TG-MENU] 啟用失敗: {_e_menu}")

        # v6.1 safety net:獨立 watchdog daemon 強制掃所有 AUTO_ASKING_SELLER conv,
        # 不依賴 conv 個別 timer 鏈。timer 死掉、WS reconnect 漏推、asyncio loop 卡住
        # → watchdog 都會兜底。每 60s 跑一輪。連續 5 輪 list 失敗時 force_reconnect WS。
        try:
            self._start_seller_watchdog()
        except Exception as _e_wd:
            self.on_log(f"[WATCHDOG] 啟動失敗(忽略): {_e_wd}")

    def _start_seller_watchdog(self) -> None:
        """v6.1:safety net daemon thread,獨立於 conv 個別 timer 鏈強制 poll 賣家回覆。

        為什麼需要:
        - threading.Timer 是 best-effort,異常/race 可能讓 timer 鏈中斷
        - WS asyncio loop 可能因媒體解析/server stale 連線卡住
        - WS reconnect 後不會自動 catch up 斷線期間漏推的訊息

        本 watchdog:
        - 每 60s 跑,獨立 daemon thread,不會被 conv timer 異常影響
        - 對每個 AUTO_ASKING_SELLER xianyu conv 強制呼叫 list_user_messages
        - 跟現有 timer 並存(_xianyu_check_via_list_messages 內部有 baseline+processed_msg_ids 去重)
        - 連續 5 輪 WS list 失敗 → force_reconnect WS 救 stale 連線
        """
        import threading as _t

        self._watchdog_consecutive_fail = 0  # 連續 list 失敗計數
        WATCHDOG_INTERVAL = 60
        MAX_FAIL_BEFORE_RECONNECT = 5

        def _loop():
            import time as _time
            from core.goofish_ws_client import XianyuWsClient
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            while True:
                try:
                    _time.sleep(WATCHDOG_INTERVAL)

                    # 1. 拿出所有 AUTO_ASKING_SELLER + xianyu 的 conv
                    with self._lock:
                        candidates = [
                            c for c in self._convs.values()
                            if c.phase == ConvPhase.AUTO_ASKING_SELLER
                            and c.seller_session_id
                            and not c.auto_ask_fallback
                        ]
                    xy_convs = []
                    for c in candidates:
                        src = ""
                        if c.product_urls:
                            src = c.product_urls[0].get("source", "")
                        if src == "xianyu":
                            xy_convs.append(c)
                    if not xy_convs:
                        continue

                    # 2. WS 在線才跑(離線時 conv timer 鏈會走 HTTP fallback)
                    try:
                        ws = XianyuWsClient.get_instance(
                            PURCHASE_PROFILE_DIR, on_log=self.on_log,
                        )
                        if not ws.is_connected():
                            self.on_log(
                                f"[WATCHDOG] WS 未連線,跳過本輪 "
                                f"({len(xy_convs)} 個 conv 等)"
                            )
                            continue
                    except Exception as e:
                        self.on_log(f"[WATCHDOG] WS check 異常: {e}")
                        continue

                    self.on_log(
                        f"[WATCHDOG] 強掃 {len(xy_convs)} 個 AUTO_ASKING_SELLER conv"
                    )

                    # 3. 對每個 conv 強拉訊息(內部有 baseline+processed dedupe,
                    #    跟現有 timer 並存不會重複觸發整合)
                    round_fail_count = 0
                    for c in xy_convs:
                        try:
                            # 直接呼叫補漏函數,複用既有過濾邏輯
                            before_processed = len(c.seller_processed_msg_ids)
                            self._xianyu_check_via_list_messages(c, ws)
                            after_processed = len(c.seller_processed_msg_ids)
                            # 持久化,disk 反映 watchdog 真的有跑
                            try:
                                if hasattr(self, "_last_persist_ts"):
                                    self._last_persist_ts.pop(c.conv_id, None)
                                self._persist_conv(c)
                            except Exception:
                                pass
                            # processed set 沒增長 → 可能 list 失敗或沒新訊息
                            # (沒法區分,只在 list 拋 exception 才算 hard fail)
                        except Exception as e:
                            round_fail_count += 1
                            self.on_log(
                                f"[WATCHDOG] check {c.conv_id[:8]} 異常: {e}"
                            )

                    # 4. 連續多輪 fail → 主動 force_reconnect WS
                    if round_fail_count == len(xy_convs):
                        self._watchdog_consecutive_fail += 1
                        if self._watchdog_consecutive_fail >= MAX_FAIL_BEFORE_RECONNECT:
                            self.on_log(
                                f"[WATCHDOG] 連續 {self._watchdog_consecutive_fail} 輪 "
                                f"全 fail,強制 reconnect WS 救 stale 連線"
                            )
                            try:
                                ws.force_reconnect()
                                self._watchdog_consecutive_fail = 0
                            except Exception as _e_rc:
                                self.on_log(f"[WATCHDOG] force_reconnect 失敗: {_e_rc}")
                    else:
                        self._watchdog_consecutive_fail = 0

                except Exception as e:
                    # loop 內任何異常都不能殺掉 daemon
                    self.on_log(f"[WATCHDOG] loop 內未捕獲異常(繼續): {e}")

        t = _t.Thread(target=_loop, daemon=True, name="seller-watchdog")
        t.start()
        self.on_log(
            f"[WATCHDOG] 賣家回覆 watchdog 已啟動 "
            f"({WATCHDOG_INTERVAL}s/輪 + 連續 {MAX_FAIL_BEFORE_RECONNECT} 輪 fail 強制重連)"
        )

    # ---------- 主管 Bot 转发 ----------

    def _get_profile_send_lock(self, profile_id: str) -> threading.Lock:
        with self._profile_send_locks_mu:
            if profile_id not in self._profile_send_locks:
                self._profile_send_locks[profile_id] = threading.Lock()
            return self._profile_send_locks[profile_id]

    # ---------- 主管 Bot 转发（原） ----------

    def _supervisor_send(self, text: str) -> None:
        """转发消息给主管 Bot（静默失败，不影响主流程）。"""
        if not self._sv_token or not self._sv_chat_id:
            return
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{self._sv_token}/sendMessage",
                json={"chat_id": self._sv_chat_id, "text": text},
                timeout=10,
            )
        except Exception:
            pass

    # ---------- API server 用:活躍對話快照(2026-04-29 v6.0.46)----------

    def get_active_convs_snapshot(self) -> Dict[str, Any]:
        """純讀快照,給 /api/state/conversations 用。

        Lock 哲學:進 lock 拷貝引用 + 取原語類型字段值,出 lock 序列化,
        絕不在 lock 內做 I/O。已知 5 秒 cache 在 api_server 層做。
        排除 phase 為 DONE/EXPIRED 的(視為終態)。ERROR 保留,讓 daemon 看到失敗對話可決定 retry。
        """
        import time as _time
        try:
            now = _time.time()
            with self._lock:
                # 進 lock 只拷:把字段值複製到本地 dict,出 lock 再序列化
                copies = []
                for cid, conv in list(self._convs.items()):
                    phase = conv.phase.value if hasattr(conv.phase, "value") else str(conv.phase)
                    if phase in ("DONE", "EXPIRED"):
                        continue
                    copies.append({
                        "conv_id": cid,
                        "profile_id": conv.profile_id,
                        "account_name": conv.account_name,
                        "chat_id": conv.chat_id,
                        "chat_url": conv.chat_url,
                        "buyer_label": conv.buyer_label,
                        "buyer_text_preview": (conv.buyer_text or "")[:200],
                        "buyer_text_len": len(conv.buyer_text or ""),
                        "unread_count": int(conv.unread_count or 0),
                        "phase": phase,
                        "ai_action": conv.ai_action or "",
                        "ai_draft": conv.ai_draft or "",
                        "ai_draft_len": len(conv.ai_draft or ""),
                        "ai_internal_note": (conv.ai_internal_note or "")[:200],
                        "shop_code": conv.shop_code or "",
                        "product_can_buy": conv.product_can_buy or "",
                        "product_title": (conv.product_title or "")[:120],
                        "seller_answer_len": len(conv.seller_answer or ""),
                        "ai_integrated_draft": conv.ai_integrated_draft or "",
                        "final_reply_len": len(conv.final_reply or ""),
                        "error_msg": (conv.error_msg or "")[:200],
                        "created_ts": float(conv.created_ts or 0),
                        "updated_ts": float(conv.updated_ts or 0),
                        "age_sec": int(now - float(conv.created_ts or now)),
                        "draft_age_sec": int(now - float(conv.updated_ts or now)),
                    })
            # 出 lock 之外做計數 + 序列化
            by_phase: Dict[str, int] = {}
            pending_user_ack = 0
            for c in copies:
                p = c["phase"]
                by_phase[p] = by_phase.get(p, 0) + 1
                if p in ("PREVIEW_SENT", "PREVIEW_SELLER_QUESTION", "PREVIEW_SELLER"):
                    pending_user_ack += 1
            return {
                "ts": now,
                "active_count": len(copies),
                "by_phase": by_phase,
                "pending_user_ack": pending_user_ack,
                "conversations": copies,
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "conversations": []}

    # ---------- 监控触发入口 ----------

    def on_new_im(self, profile_id: str, account_name: str, items: List[Dict[str, Any]]) -> None:
        """监控检测到 IM 新消息时调用。items 来自 capture_yahoo_im_unread_previews。"""
        self.on_log(f"[TG] on_new_im called: acc={account_name}, items={len(items or [])}, tg_chat={self.tg.chat_id or '(空)'}")
        self._cleanup_expired()

        for it in (items or []):
            chat_id = str(it.get("chat_id") or "").strip()
            self.on_log(f"[IM-DIAG] on_new_im item: chat_id={chat_id!r}, label={str(it.get('label',''))[:30]!r}, preview={str(it.get('preview',''))[:40]!r}, text_len={len(str(it.get('text','') or ''))}")

            # v6.0.75:chat_id 空時 fallback — 從 url 或 label 補回
            # (Yahoo「傳送了一則商品資訊」這類自動發的對話 DOM 內可能拿不到 cid)
            # v6.0.81 修復:**不再用 shop_code 補 chat_id** — shop_code 是賣家自己的店鋪 ID,
            #   不是買家 chat_id,會跟同買家其他訊息分裂成兩個 conv(重複建)
            # 改成:用 label 找同 profile 已存在的 active conv(同買家剛建過) → 合併跳過
            if not chat_id:
                url = str(it.get("url") or "").strip()
                shop_code = str(it.get("shop_code") or "").strip()
                # 嘗試從 url 提取 chat_id: /chat/Y9000000008 → Y9000000008
                # 注意:Yahoo /chat/{shop_code} 是店鋪頁,提取出來的也可能是 shop_code(非買家 ID)
                import re as _re_cid
                m = _re_cid.search(r"/chat/([A-Za-z0-9]+)", url)
                if m and m.group(1) != shop_code:
                    chat_id = m.group(1)
                    self.on_log(f"[IM-DIAG] {account_name}: chat_id 從 url 補回 = {chat_id}")
                else:
                    # 用 label 找同 profile 內 active conv(同買家的其他訊息已建過 conv)
                    label_for_match = str(it.get("label") or "").strip()
                    matched_conv_id = ""
                    if label_for_match:
                        with self._lock:
                            for _c in self._convs.values():
                                if (_c.profile_id == profile_id
                                    and _c.buyer_label == label_for_match
                                    and _c.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR)):
                                    matched_conv_id = _c.conv_id
                                    break
                    if matched_conv_id:
                        self.on_log(
                            f"[IM-DIAG] {account_name}: chat_id 空 → 找到同 label "
                            f"{label_for_match!r} 的 active conv={matched_conv_id[:8]},"
                            f"跳過(避免拿 shop_code 假冒建出重複 conv)"
                        )
                        continue
                    self.on_log(
                        f"[IM-DIAG] {account_name}: 跳過 — chat_id 空、url 無 buyer ID、"
                        f"label={label_for_match!r} 沒匹配到 active conv "
                        f"(shop_code={shop_code} 是賣家自己,不能當買家 chat_id)"
                    )
                    continue
            label = str(it.get("label") or chat_id).strip()
            fulltext = str(it.get("text") or "").strip()
            preview = str(it.get("preview") or "").strip()
            buyer_text = fulltext or preview
            url = str(it.get("url") or "").strip()
            unread = int(it.get("unread") or 1)
            shop_code = str(it.get("shop_code") or "").strip()
            # v6.1.54:買家發來的圖 URL list(用於 NEED_SELLER 中轉給閒魚賣家)
            buyer_image_urls_in: List[str] = list(it.get("buyer_image_urls") or [])
            # v6.1.55:完整對話媒體(買家+賣家、圖+視頻、ts、msg_idx — 給 AI 看)
            conversation_media_in: List[Dict[str, Any]] = list(it.get("conversation_media") or [])

            if not buyer_text:
                self.on_log(f"[IM-DIAG] {account_name}: 跳过 {label} — buyer_text为空 (chat_id={chat_id})")
                continue

            # Yahoo 官方频道过滤：广告消息不需要 AI 回复，只提供消红点
            _OFFICIAL_LABELS = ("Y拍官方客服頻道", "Y拍官方", "Yahoo拍賣官方")
            if any(kw in label for kw in _OFFICIAL_LABELS):
                self.on_log(f"[TG] {account_name}: 官方频道 {label}，跳过AI，提供消红点")
                _chat_url = url or f"https://tw.bid.yahoo.com/chat/{shop_code or chat_id}"
                with self._lock:
                    conv_id = uuid.uuid4().hex[:12]
                    conv = ConversationState(
                        conv_id=conv_id,
                        profile_id=profile_id,
                        account_name=account_name,
                        chat_id=chat_id,
                        chat_url=_chat_url,
                        buyer_label=label,
                        buyer_text=buyer_text,
                        unread_count=unread,
                        shop_code=shop_code,
                    )
                    conv.ai_draft = ""
                    conv.ai_action = "STICKER_READ"
                    self._convs[conv_id] = conv
                    self._chat_map[key] = conv_id
                # v6.0.74:合并消息 — 内容+按钮一条
                _content = f"「{_smart_truncate(buyer_text, 80)}」\n\nYahoo 官方廣告,無需回覆。"
                self._set_phase(conv_id, ConvPhase.PREVIEW_SENT)
                self._send_phase_buttons(conv, content=_content)
                continue

            # 文本哈希去重：如果提取的文本和上次完全一样（新消息是贴纸/图片被过滤了），跳过
            import hashlib as _hl
            text_hash = _hl.md5(buyer_text.encode("utf-8", errors="ignore")).hexdigest()
            key = f"{profile_id}|{chat_id}"
            if self._chat_text_hash.get(key) == text_hash:
                self.on_log(f"[TG] 跳过 {label}: 文本无变化（可能是贴纸/图片）")
                continue
            self._chat_text_hash[key] = text_hash

            # 卖家是最后发言者检测：如果对话最后一条是【卖家】，
            # 说明买家没有新文字消息（可能只发了贴图/贴纸），跳过
            _lines = buyer_text.strip().splitlines()
            _last_line = ""
            for _ln in reversed(_lines):
                _s = _ln.strip()
                if _s:
                    _last_line = _s
                    break
            if _last_line.startswith("【卖家】"):
                self.on_log(f"[TG] 跳过 {label}: 最后发言是卖家（买家可能只发了贴图）")
                # 创建对话让用户确认是否消红点
                _chat_url = url or f"https://tw.bid.yahoo.com/chat/{shop_code or chat_id}"
                with self._lock:
                    conv_id = uuid.uuid4().hex[:12]
                    conv = ConversationState(
                        conv_id=conv_id,
                        profile_id=profile_id,
                        account_name=account_name,
                        chat_id=chat_id,
                        chat_url=_chat_url,
                        buyer_label=label,
                        buyer_text=buyer_text,
                        unread_count=unread,
                        shop_code=shop_code,
                    )
                    conv.ai_draft = ""  # 无需回复内容
                    conv.ai_action = "STICKER_READ"
                    self._convs[conv_id] = conv
                    self._chat_map[key] = conv_id
                # v6.0.74:合并消息
                _content = "买家发送了贴图/贴纸(无文字),卖家已是最后发言。"
                self._set_phase(conv_id, ConvPhase.PREVIEW_SENT)
                self._send_phase_buttons(conv, content=_content)
                continue

            # 去重：同一 chat DEDUP_SEC 内不重复创建
            with self._lock:
                existing_id = self._chat_map.get(key)
                # v6.1.35:重建時繼承舊 conv 的累積狀態 — 避免 product_urls / all_products
                # / pending_yahoo_ids 在 buyer 連發訊息後被歸零,導致 NEED_SELLER 走 manual fallback
                _inherited: Dict[str, Any] = {}
                _skip_rebuild = False  # v6.1.36:標記「已通知後跳過重建」
                if existing_id and existing_id in self._convs:
                    old = self._convs[existing_id]
                    if old.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR):
                        # v6.1.36:buyer 又發訊息但賣家還沒回覆 → 不重建避免重複問賣家
                        # 修「重啟前已發給閒魚賣家,重啟後 buyer 又催 → 系統重建 conv 重複問」bug
                        # 涵蓋:
                        #   AUTO_ASKING_SELLER:系統已自動發給賣家等回覆
                        #   WAIT_SELLER:手動模式 user 自己在等賣家回
                        if old.phase in (ConvPhase.AUTO_ASKING_SELLER, ConvPhase.WAIT_SELLER):
                            _text_changed = (buyer_text != old.buyer_text)
                            if _text_changed:
                                old.buyer_text = buyer_text
                                old.updated_ts = time.time()
                                # v6.1.54:同步把買家新發的圖累積進 conv(賣家再次提問 / 整合時可用)
                                try:
                                    _old_imgs = list(getattr(old, "buyer_image_urls", None) or [])
                                    for _u in buyer_image_urls_in:
                                        if _u and _u not in _old_imgs:
                                            _old_imgs.append(_u)
                                    old.buyer_image_urls = _old_imgs[-5:]
                                except Exception:
                                    pass
                                # v6.1.55:也累積 conversation_media(賣家補圖、買家追問都會進來)
                                try:
                                    _old_media = list(getattr(old, "conversation_media", None) or [])
                                    _old_urls = {m.get("url") for m in _old_media if m.get("url")}
                                    for _nm in conversation_media_in:
                                        _u = _nm.get("url", "")
                                        if _u and _u not in _old_urls:
                                            _old_media.append(_nm)
                                            _old_urls.add(_u)
                                    if len(_old_media) > 15:
                                        _old_media = _old_media[-15:]
                                    old.conversation_media = _old_media
                                except Exception:
                                    pass
                                try:
                                    if hasattr(self, "_last_persist_ts"):
                                        self._last_persist_ts.pop(old.conv_id, None)
                                    self._persist_conv(old)
                                except Exception:
                                    pass
                            # v6.1.37:不管文本是否新都推一張帶按鈕的 status card
                            # 修「重啟 / catch-up 後 forum 出現 buyer 訊息但沒任何提示/按鈕」UX
                            # throttle:同 conv 60s 內已推過則 skip(避免 spam)
                            try:
                                if not hasattr(self, "_seller_status_throttle"):
                                    self._seller_status_throttle: Dict[str, float] = {}
                                _last_status_ts = self._seller_status_throttle.get(old.conv_id, 0)
                                _now = time.time()
                                if _now - _last_status_ts >= 60:
                                    self._seller_status_throttle[old.conv_id] = _now
                                    # v6.1.44:區分「真的成功問過」vs「AI 有草稿但沒成功送」
                                    # 修「自動發送失敗(WS 連不上)→ seller_sent_question 空,但 auto_ask_question
                                    # AI 草稿有值 → 之前 status_card 顯示「已問過:草稿」誤導 user 以為問成功了」
                                    _q_sent = (old.seller_sent_question or "").strip()
                                    _q_draft = (old.auto_ask_question or "").strip()
                                    _phase_label = (
                                        "閒魚賣家(系統自動問)"
                                        if old.phase == ConvPhase.AUTO_ASKING_SELLER
                                        else "賣家(手動模式)"
                                    )
                                    _wait_min = max(0, int((_now - (old.updated_ts or _now)) // 60))
                                    if _q_sent:
                                        # 真的成功發送過
                                        if _text_changed:
                                            _header = f"✋ 買家又發了訊息,但{_phase_label}還沒回覆"
                                        else:
                                            _header = f"⏳ {_phase_label}還沒回覆,當前還在等"
                                        _content = _header
                                        _content += f"\n已問過:「{_q_sent[:100]}」"
                                        if _wait_min > 0:
                                            _content += f"\n已等待 {_wait_min} 分鐘"
                                        _content += "\n\n選擇下方操作:"
                                    else:
                                        # 沒成功發送(自動 fallback 到 manual 但 user 沒處理)
                                        _content = (
                                            f"⚠️ 賣家還沒被問到(上次自動發送失敗,卡在手動模式)"
                                        )
                                        if _q_draft:
                                            _content += f"\nAI 草稿(未發送):「{_q_draft[:100]}」"
                                        if _wait_min > 0:
                                            _content += f"\n已等待 {_wait_min} 分鐘(從未實際送出)"
                                        _content += (
                                            "\n\n建議按下方:\n"
                                            "  · 「轉自動問」 重試自動發送\n"
                                            "  · 「自己回买家」 不等賣家直接回\n"
                                            "  · 「跳过」 結束等待"
                                        )
                                    self._send_phase_buttons(old, content=_content)
                            except Exception as _e_status:
                                self.on_log(f"[TG] 推 status card 異常(忽略): {_e_status}")
                            self.on_log(
                                f"[TG] {label}: buyer 在 {old.phase.value} 狀態又收訊息"
                                f"(text_changed={_text_changed}),不重建 conv,只推 status card"
                            )
                            _skip_rebuild = True
                            continue  # 跳到 for 下一條 item,不執行後續重建邏輯
                        elif time.time() - old.updated_ts < DEDUP_SEC:
                            # 文本相同 → 真正的重复检测，跳过
                            # 文本不同 → 买家发了新消息，应该重建对话
                            if buyer_text == old.buyer_text:
                                continue
                            self.on_log(f"[TG] {label}: 买家在AI处理中发了新消息，重建对话")
                        # 繼承商品 / 來源 / 累積 yahoo_id 等(避免重新查 D1 + 走 manual fallback)
                        try:
                            # v6.1.54:合併 old.buyer_image_urls + 新抓到的(去重、保持順序、cap 5 張)
                            _merged_imgs = list(getattr(old, "buyer_image_urls", None) or [])
                            for _u in buyer_image_urls_in:
                                if _u and _u not in _merged_imgs:
                                    _merged_imgs.append(_u)
                            _merged_imgs = _merged_imgs[-5:]  # 保留最新 5 張

                            # v6.1.55:合併 conversation_media(old + new,去重、cap 15)
                            _merged_media = list(getattr(old, "conversation_media", None) or [])
                            _existing_urls = {m.get("url") for m in _merged_media if m.get("url")}
                            for _nm in conversation_media_in:
                                _u = _nm.get("url", "")
                                if _u and _u not in _existing_urls:
                                    _merged_media.append(_nm)
                                    _existing_urls.add(_u)
                            if len(_merged_media) > 15:
                                _merged_media = _merged_media[-15:]

                            _inherited = {
                                "product_urls": list(old.product_urls or []),
                                "all_products": list(getattr(old, "all_products", None) or []),
                                "pending_yahoo_ids": list(getattr(old, "pending_yahoo_ids", None) or []),
                                "product_text": old.product_text or "",
                                "product_can_buy": old.product_can_buy or "",
                                "product_title": old.product_title or "",
                                "product_image_urls": list(old.product_image_urls or []),
                                "yahoo_page_info": dict(old.yahoo_page_info or {}),
                                # v6.1.54:繼承並合併買家圖 URL
                                "buyer_image_urls": _merged_imgs,
                                # v6.1.55:繼承並合併完整對話媒體
                                "conversation_media": _merged_media,
                            }
                        except Exception:
                            _inherited = {}
                        # 买家有新消息 → 关掉旧对话，重建新的
                        old.phase = ConvPhase.EXPIRED
                        self._convs.pop(existing_id, None)
                        self._chat_map.pop(key, None)

                # 创建新对话
                conv_id = uuid.uuid4().hex[:12]
                conv = ConversationState(
                    conv_id=conv_id,
                    profile_id=profile_id,
                    account_name=account_name,
                    chat_id=chat_id,
                    chat_url=url,
                    buyer_label=label,
                    buyer_text=buyer_text,
                    unread_count=unread,
                    shop_code=shop_code,
                    # v6.1.54:買家圖 URL(NEED_SELLER 時自動中轉給閒魚賣家)
                    buyer_image_urls=list(buyer_image_urls_in),
                    # v6.1.55:完整對話媒體(給 AI 按權重看)
                    conversation_media=list(conversation_media_in),
                )
                # v6.1.35:套用繼承欄位(僅當舊 conv 有實際值才覆蓋,避免清空已 default 欄位)
                if _inherited:
                    for _k, _v in _inherited.items():
                        if _v:
                            try:
                                setattr(conv, _k, _v)
                            except Exception:
                                pass
                    try:
                        _yids_n = len(_inherited.get("pending_yahoo_ids") or [])
                        _prod_n = len(_inherited.get("product_urls") or [])
                        if _yids_n or _prod_n:
                            self.on_log(
                                f"[TG] {label}: 重建 conv 繼承累積狀態 "
                                f"(yahoo_ids={_yids_n}, product_urls={_prod_n})"
                            )
                    except Exception:
                        pass
                self._convs[conv_id] = conv
                self._chat_map[key] = conv_id

            # v6.0.83:forum push 統一由 PureHTTPMonitor._forward_to_forum 處理
            # (BOSH 拉訊息單條 push,跟原生 Yahoo IM 一致體驗)
            # 這裡不再呼叫 _maybe_forward_yahoo_to_forum(會把整段對話文字塞成一條訊息,
            # 跟 _forward_to_forum 路徑重複,還可能因 label 不同建出第二個 topic)

            # 在后台线程运行 AI 判断
            threading.Thread(
                target=self._process_new_conv,
                args=(conv_id,),
                daemon=True,
            ).start()

    # ---------- AI 判断流程 ----------

    def _process_new_conv(self, conv_id: str) -> None:
        """后台线程：AI 判断 + 发 TG 通知。"""
        try:
            self._process_new_conv_inner(conv_id)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                self.on_log(f"[TG] _process_new_conv 异常: {e}\n{tb}")
            except Exception:
                pass
            # v6.0.83 修:set conv.phase=ERROR(原本卡 PENDING_AI 永遠不結束),
            # 並 push 錯誤到 buyer topic(不是私聊)
            conv = self._get(conv_id)
            try:
                self._set_phase(conv_id, ConvPhase.ERROR, error_msg=str(e)[:200])
            except Exception:
                pass
            err_msg = f"⚠️ AI 處理異常:`{str(e)[:200]}`\n_可 retry 或 reply: 自己回_"
            pushed = False
            if conv and self.forum_bridge:
                try:
                    topic_id = self._find_topic_for_conv(conv)
                    if topic_id:
                        self.forum_bridge.bot._post("sendMessage", {
                            "chat_id": self.forum_bridge.bot.forum_chat_id,
                            "message_thread_id": topic_id,
                            "text": err_msg,
                            "parse_mode": "Markdown",
                        })
                        pushed = True
                except Exception:
                    pass
            if not pushed:
                try:
                    self.tg.send(err_msg)
                except Exception:
                    pass

    def _process_new_conv_inner(self, conv_id: str) -> None:
        conv = self._get(conv_id)
        if not conv:
            self.on_log(f"[IM-DIAG] _process_new_conv_inner: conv_id={conv_id} 不存在，跳过")
            return

        # v6.1.27:訓練數據紀錄 — 新對話開始(訓練端的軌跡起點)
        # 補強:加跨對話歷史 metadata,訓練端能學「老客戶 vs 新客戶」差異
        _tc_prior_meta = {}
        try:
            # 查同 buyer 過去活躍/已結束的 conv 數(在記憶體中能查到的)
            _prior_active = 0
            _prior_done = 0
            with self._lock:
                for _other in self._convs.values():
                    if _other.conv_id == conv_id:
                        continue
                    if (_other.profile_id == conv.profile_id
                            and _other.chat_id == conv.chat_id
                            and _other.buyer_label == conv.buyer_label):
                        if _other.phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
                            _prior_done += 1
                        else:
                            _prior_active += 1
            _tc_prior_meta["prior_active_convs"] = _prior_active
            _tc_prior_meta["prior_done_convs"] = _prior_done
            _tc_prior_meta["is_returning_buyer"] = (_prior_active + _prior_done) > 0
        except Exception:
            pass

        _tc_record(
            "conv:new",
            conv=conv,
            input={"buyer_text": conv.buyer_text[-3000:]},
            output=None,
            metadata={
                "unread_count": conv.unread_count,
                "buyer_text_length": len(conv.buyer_text),
                **_tc_prior_meta,
            },
        )

        self.on_log(f"[IM-DIAG] 开始AI分析: conv={conv_id}, acc={conv.account_name}, buyer={conv.buyer_label}, text_len={len(conv.buyer_text)}")
        buyer_text = conv.buyer_text
        # 脱敏版本只用于发给 AI API，TG 通知用原始文本（发给自己）
        buyer_text_for_ai = redact_sensitive(buyer_text) if self.ai.get("redact", True) else buyer_text

        # --- 提取 Yahoo 商品编号并查询货源 ---
        # v6.0.75:買家對話中可能有多個 Yahoo 商品,**全部**查貨源,
        # 優先用「最後一個」的貨源(買家最後問的通常是最後一個 yahoo_id),
        # 確保 NEED_SELLER 時系統能自動問該商品對應的賣家
        yahoo_ids = extract_yahoo_item_ids(conv.buyer_text)
        product_info: Dict[str, str] = {}

        # v6.1.35:從新訊息 extract 抓不到 yahoo_id 時,fallback 到之前累積的 pending_yahoo_ids
        # 修「對話 > 100 條後,Yahoo URL 訊息被擠出 BOSH 拉的範圍,新訊息來時 extract 抓不到」
        # 之前抓過的 yahoo_id 不該丟,保留讓 AI 能對應商品
        if not yahoo_ids:
            _prior_yids = list(getattr(conv, "pending_yahoo_ids", None) or [])
            if _prior_yids:
                yahoo_ids = _prior_yids
                self.on_log(
                    f"[TG] 新訊息無 yahoo_id,fallback 用 conv.pending_yahoo_ids={_prior_yids}"
                )

        # 把提到的 yahoo_ids 存進 conv 供後續 fallback 用(即使 D1 query 失敗也保留)
        if yahoo_ids:
            conv.pending_yahoo_ids = list(yahoo_ids)

        if yahoo_ids:
            self.on_log(f"[TG] Found Yahoo item ID(s): {yahoo_ids}")
            all_infos = []  # (yahoo_id, info_dict)
            # v6.0.75:全部查,不限數量(每個商品都該被查到)
            for yid in yahoo_ids:
                info = self._fetch_product_info(
                    yahoo_item_id=yid,
                    profile_id=conv.profile_id,
                )
                if info:
                    all_infos.append((yid, info))

            if all_infos:
                # 全部商品資訊存到 all_products(AI commander 看得到所有商品 + 圖)
                conv.all_products = [
                    {
                        "yahoo_id": yid,
                        "source": info.get("source", ""),
                        "source_url": info.get("source_url", ""),
                        "can_buy": info.get("can_buy", "未知"),
                        "product_text": info.get("source_page_text", "") or info.get("status_text", ""),
                        "barcode": info.get("barcode", ""),
                        "image_urls": list(info.get("image_urls") or []),  # v6.0.75:每個商品的圖
                    }
                    for yid, info in all_infos
                ]

                # v6.0.75:primary 直接用「買家最後問的」(all_infos[-1])
                # 不要因為「沒貨源」就 fallback 到第一個 — 買家問哪個就是哪個,
                # 沒貨源就走手動,不要錯誤地問成另一個商品的賣家
                primary_yid, product_info = all_infos[-1]
                conv.product_can_buy = product_info.get("can_buy", "未知")
                conv.product_text = product_info.get("source_page_text", "") or product_info.get("status_text", "")
                conv.product_title = product_info.get("barcode", "")
                conv.product_urls = [{
                    "url": product_info.get("source_url", ""),
                    "source": product_info.get("source", ""),
                    "yahoo_id": primary_yid,
                }]
                for yid, info in all_infos:
                    if yid != primary_yid:
                        conv.product_urls.append({
                            "url": info.get("source_url", ""),
                            "source": info.get("source", ""),
                            "yahoo_id": yid,
                        })
                self.on_log(
                    f"[TG] Product (primary=最後問 yid={primary_yid}): "
                    f"source={product_info.get('source')}, can_buy={conv.product_can_buy}, "
                    f"total_products={len(conv.all_products)}"
                )

                # v6.1.35:product_urls / all_products 設好立刻強制 persist,bypass 5s throttle
                # 修「conv restore 後 product_urls 丟失,NEED_SELLER 走 manual fallback」bug
                # 原因:_persist_conv 5s throttle 可能跳過此次寫盤,導致 disk 上 product_urls 空
                try:
                    if hasattr(self, "_last_persist_ts"):
                        self._last_persist_ts.pop(conv.conv_id, None)
                    self._persist_conv(conv)
                except Exception:
                    pass

        # --- 抓取 Yahoo 商品页面（运费等公开信息，匿名浏览器） ---
        if yahoo_ids:
            self.on_log(f"[TG] Yahoo page scraping start: {yahoo_ids[0]}")
            try:
                ypage = asyncio.run(_fetch_yahoo_item_page_async(yahoo_ids[0]))
                if ypage and not ypage.get("error"):
                    conv.yahoo_page_info = ypage
                    if ypage.get("image_urls"):
                        conv.product_image_urls = list(ypage["image_urls"])
                        self.on_log(f"[TG] Yahoo images: {len(ypage['image_urls'])} found")
                    self.on_log(f"[TG] Yahoo page: shipping={ypage.get('shipping','')[:60]}")
                elif ypage and ypage.get("error"):
                    self.on_log(f"[TG] Yahoo page scrape failed: {ypage['error']}")
                else:
                    self.on_log("[TG] Yahoo page: empty result")
            except Exception as e:
                self.on_log(f"[TG] Yahoo page fetch error: {type(e).__name__}: {e}")

        # v6.0.75:多商品場景 — primary 商品圖優先,其他商品也全帶上(不限數量)
        # 策略:Yahoo 主圖(已在前面)+ primary 閒魚圖 + 其他商品閒魚圖(全部)
        all_prods = getattr(conv, "all_products", []) or []
        if all_prods:
            primary_yid = conv.product_urls[0].get("yahoo_id", "") if conv.product_urls else ""
            primary_imgs = []
            other_imgs = []
            for prod in all_prods:
                imgs = prod.get("image_urls") or []
                if prod.get("yahoo_id") == primary_yid:
                    primary_imgs = imgs
                else:
                    other_imgs.extend(imgs)  # 全部帶上,不限

            # 合併到 conv.product_image_urls(去重 + 保持順序,primary 在前)
            existing = set(conv.product_image_urls)
            for u in primary_imgs + other_imgs:
                if u and u not in existing:
                    conv.product_image_urls.append(u)
                    existing.add(u)
            self.on_log(
                f"[TG] 商品圖合併: Yahoo {len(conv.yahoo_page_info.get('image_urls', []) if conv.yahoo_page_info else [])} 張 + "
                f"閒魚 primary {len(primary_imgs)} + 其他 {len(other_imgs)} = 總 {len(conv.product_image_urls)}"
            )

        # 1) 发 TG 通知：收到新消息
        # v6.1 簡化:forum 場景買家對話已透過 _forward_to_forum 單獨 push 過,
        # 這條訊息不再重複貼 buyer_text;改成單純「分析元數據」卡片
        in_forum = bool(self.forum_bridge and self._find_topic_for_conv(conv))
        if in_forum:
            # forum:簡短卡片(對話已在前面 push 過,不重複)
            notify_text = (
                f"🤖 AI 分析中 [{conv.account_name} / {conv.buyer_label}]\n"
            )
        else:
            # 私聊:保留完整 buyer_text(沒 forum 訊息流)
            notify_text = (
                f"📩 新消息 [{conv.account_name}]\n"
                f"买家 {conv.buyer_label}:\n"
                f"「{_smart_truncate(buyer_text)}」\n"
            )
        if product_info:
            src = product_info.get("source", "")
            src_label = "煤炉(Mercari)" if src == "mercari" else "闲鱼" if src == "xianyu" else src
            notify_text += f"\n🏭 货源：{src_label}"
            if conv.product_can_buy != "未知":
                if conv.product_can_buy == "是":
                    status = "在售可购买"
                elif conv.product_can_buy == "可能":
                    status = "可能在售(需App確認)"
                else:
                    status = "已售出/无货"
                notify_text += f" · {status}"
            notify_text += "\n"
            # 闲鱼链接转移动版 (手机和电脑都能打开),其它平台原样
            notify_text += f"🔗 {_to_mobile_xianyu_url(product_info.get('source_url', ''))}\n"
            st = product_info.get("status_text", "")
            if st:
                notify_text += f"📋 {st}\n"
        # v6.1:運費資訊只供 AI 內部判斷用(prompt 內看),
        # 卡片不再顯示「🚚 7-ELEVEN... 萊爾富... 宅配...」這種冗餘字串(user 反饋沒用)
        # 私聊保留「AI 分析中」(有完整 context 看);forum 已經夠簡短,不需再加
        if not in_forum:
            notify_text += "\n🤖 AI 分析中..."
        self.on_log(f"[TG] 发送通知: chat_id={self.tg.chat_id or '(未绑定)'}, token={'有' if self.tg.token else '无'}")
        msg_id = self._conv_aware_send(conv, notify_text)
        if msg_id is None:
            self.on_log(f"[TG] ⚠️ 初始通知发送失败 — chat_id 可能未绑定，请向 Bot 发送 /start")
        else:
            self.on_log(f"[TG] send result: msg_id={msg_id}")
            conv.tg_msg_ids.add(msg_id)

        # 转发给主管：买家新消息
        self._supervisor_send(
            f"📩 [{conv.account_name}] 买家 {conv.buyer_label}：\n"
            f"「{_smart_truncate(buyer_text)}」"
        )

        # 2) AI 判断（传完整对话，靠 prompt 让 AI 理解对话进展）
        try:
            self.on_log(f"[IM-DIAG] 调用AI判断: conv={conv_id}")
            decision = self._run_ai_judge(conv, buyer_text_for_ai)
            self.on_log(f"[IM-DIAG] AI判断结果: conv={conv_id}, action={decision.action}, confidence={decision.confidence}, reason={decision.reason[:80]}")
        except Exception as e:
            self.on_log(f"[IM-DIAG] ⚠️ AI判断异常: conv={conv_id}, error={e}")
            self._set_phase(conv_id, ConvPhase.ERROR, error_msg=str(e)[:200])
            # v6.0.74:错误内容+按钮合并一条
            with self._lock:
                _err_conv = self._convs.get(conv_id)
            if _err_conv:
                self._send_phase_buttons(_err_conv, content=f"AI 判斷失敗:\n{str(e)[:200]}")
            return

        conv.ai_action = decision.action
        # v6.1:把 Commander 的議價 hint + CoT thought 帶到 conv 給 Writer 用
        conv.ai_pricing_hint = decision.pricing_hint or {}
        conv.ai_thought_steps = decision.thought_steps or []
        if decision.thought_steps:
            self.on_log(
                f"[AI-COT] conv={conv.conv_id[:8]} thought: "
                + " | ".join(decision.thought_steps[:3])[:300]
            )

        # v6.1.27:訓練數據紀錄 — Commander 的決策結果
        _tc_record(
            "ai:commander_decide",
            conv=conv,
            input={"buyer_text": buyer_text[-2000:]},
            output={
                "action": decision.action,
                "reason": (decision.reason or "")[:1000],
                "pricing_hint": decision.pricing_hint or {},
                "thought_steps": (decision.thought_steps or [])[:10],
            },
            chosen_action=decision.action,
            metadata={"phase_before": str(conv.phase)},
        )

        # 3) 根据判断结果分流
        if decision.action == "NO_REPLY":
            self._handle_no_reply(conv, decision, buyer_text)
        elif decision.action == "NEED_SELLER":
            self._handle_need_seller(conv, decision, buyer_text)
        else:
            self._handle_auto_reply(conv, decision, buyer_text)

    # ---------- 分流处理 ----------

    def _handle_no_reply(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """AI 判断买家是纯收尾/感谢语，生成感谢回复等用户确认。"""
        reason = decision.reason or "买家纯感谢/收尾语"
        conv.ai_internal_note = format_commander_banner(decision)

        # AI 生成简短感谢回复作为草稿
        thanks_reply = self._generate_thanks_reply(buyer_text)
        conv.ai_draft = thanks_reply

        # v6.0.74:合并消息 — AI 判断+建议回复+按钮同一条
        _content = f"💬 AI 判断:{reason}\n\n{thanks_reply}"
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SENT)
        self._send_phase_buttons(conv, content=_content)

    def _generate_thanks_reply(self, buyer_text: str) -> str:
        """用 AI 根据对话上下文生成简短感谢收尾回复。"""
        try:
            ok, result = call_openai(
                api_key=self.ai.get("api_key", ""),
                base_url=self.ai.get("base_url", ""),
                endpoint_mode=self.ai.get("endpoint_mode", "responses"),
                model=self.ai.get("model", ""),
                system_prompt=(
                    "你是Yahoo拍賣賣家，買家剛發了感謝/收尾語。\n"
                    "請生成一句簡短的收尾回覆（10字以內最佳，最多15字）。\n\n"
                    "【規則】\n"
                    "- 用繁體中文，口語化，像台灣人聊天\n"
                    "- 語氣溫暖但要讓對話自然結束\n"
                    "- 絕對不要用會讓買家覺得需要再回覆的句子\n"
                    "- 禁止：『有需要再找我』『隨時聯繫』『歡迎再來』這類邀請回覆的話\n"
                    "- 好的範例：『謝謝你～』『不客氣喔～』『好的收到👌』『感謝你唷～』\n"
                    "- 只輸出回覆文字，不要加引號或說明"
                ),
                user_prompt=f"【對話內容】\n{buyer_text[-500:]}\n\n請生成收尾回覆。",
            )
            if ok and result.strip():
                return result.strip()
        except Exception:
            pass
        return "謝謝你～"

    def _handle_reply_override(self) -> None:
        """用户回复 reply，覆盖 NO_REPLY 判断，强制生成 AI 回复。"""
        # 找最近一个 NO_REPLY 的 DONE 对话
        with self._lock:
            candidates = [
                c for c in self._convs.values()
                if c.phase == ConvPhase.DONE and c.ai_action == "NO_REPLY"
            ]
        if not candidates:
            self.tg.send("没有可覆盖的 NO_REPLY 对话。")
            return
        candidates.sort(key=lambda c: c.updated_ts, reverse=True)
        conv = candidates[0]

        self._conv_aware_send(conv, f"🔄 重新生成回覆中...(買家 {conv.buyer_label})")
        conv.ai_action = "AUTO_REPLY"

        # 在后台线程生成回复
        threading.Thread(
            target=self._reply_override_worker,
            args=(conv,),
            daemon=True,
        ).start()

    def _reply_override_worker(self, conv: ConversationState) -> None:
        """后台线程：为 NO_REPLY 覆盖生成 AI 回复。"""
        try:
            buyer_text = conv.buyer_text
            buyer_text_for_ai = (
                redact_sensitive(buyer_text)
                if self.ai.get("redact", True) else buyer_text
            )
            draft = self._run_ai_reply(conv, buyer_text_for_ai)
            conv.ai_draft = draft

            preview_text = (
                f"🤖 AI 建议回复：（下一条可直接复制）"
            )
            msg_id = self._conv_aware_send(conv, preview_text)
            if msg_id:
                conv.tg_msg_ids.add(msg_id)
            draft_msg_id = self._conv_aware_send(conv, draft)
            if draft_msg_id:
                conv.tg_msg_ids.add(draft_msg_id)
            ops_msg_id = self._conv_aware_send(conv, 
                f"回复 ok → 确认并自动发送\n"
                f"回复 mod:指令 → AI根据你的指令修改草稿\n"
                f"回复 edit:内容 → 修改后自动发送\n"
                f"回复 reply:内容 → 忽略AI，直接用你的内容回复买家\n"
                f"回复 ask → 去问采购方卖家\n"
                f"回复 read → 只消红点（不发消息）\n"
                f"回复 skip → 跳过"
            )
            if ops_msg_id:
                conv.tg_msg_ids.add(ops_msg_id)
            self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SENT)
            self._send_phase_buttons(conv)  # v6.0.74 按钮操作面板
        except Exception as e:
            self.on_log(f"[TG] reply override error: {e}")
            self._conv_aware_send(conv, f"⚠️ 生成回复失败：{str(e)[:200]}")

    def _handle_auto_reply(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """AI 能回答：生成草稿 → 发 TG 预览 → 等用户确认。"""
        try:
            draft = self._run_ai_reply(conv, buyer_text)
        except Exception as e:
            self._set_phase(conv.conv_id, ConvPhase.ERROR, error_msg=str(e)[:200])
            self._send_phase_buttons(conv, content=f"AI 生成回復失敗:\n{str(e)[:200]}")
            return

        conv.ai_draft = draft
        conv.ai_internal_note = format_commander_banner(decision)

        # v6.0.74:重构版面 — 商业信息已由 _process_new_conv 发过(消息 A),
        # 这里只发「草稿+按钮」合并消息(消息 B),删除原本的 preview/draft/ops 三条重复消息
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SENT)
        self._send_phase_buttons(conv, content=draft)

        # 转发给主管：AI 建议回复
        self._supervisor_send(
            f"🤖 [{conv.account_name}] 买家 {conv.buyer_label}\n"
            f"AI 建议回复：「{draft}」"
        )

    def _handle_need_seller(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """需要卖家回复：根据货源平台分流。"""
        conv.ai_internal_note = format_commander_banner(decision)
        # Fallback:product_urls 空時主動重抓 D1(常見:_process_new_conv_inner 首輪 D1 timeout)
        self._ensure_product_urls(conv)
        src = conv.product_urls[0]["source"] if conv.product_urls else ""

        # 闲鱼货源 → 自动提问
        if src == "xianyu" and conv.product_urls:
            self._handle_need_seller_auto_xianyu(conv, decision, buyer_text)
            return

        # 煤炉货源 → 自动留言
        if src == "mercari" and conv.product_urls:
            self._handle_need_seller_auto_mercari(conv, decision, buyer_text)
            return

        # v6.1.35:走 manual fallback 前印診斷 log,讓 user 看到「為什麼判定不到貨源」
        try:
            _diag_urls_n = len(conv.product_urls or [])
            _diag_all_n = len(getattr(conv, "all_products", None) or [])
            _diag_pend = list(getattr(conv, "pending_yahoo_ids", None) or [])
            self.on_log(
                f"[TG] _handle_need_seller manual fallback: src={src!r} "
                f"product_urls_n={_diag_urls_n} all_products_n={_diag_all_n} "
                f"pending_yids={_diag_pend} conv={conv.conv_id[:8]}"
            )
        except Exception:
            pass

        # 其他货源 → 手动流程
        self._handle_need_seller_manual(conv, decision, buyer_text)

    def _ensure_product_urls(self, conv: ConversationState) -> None:
        """如果 conv.product_urls 為空,主動拉 Yahoo ID + 查 D1 補上。

        Yahoo ID 來源(逐級 fallback):
        0. conv.all_products(restore 後 product_urls 可能丟但 all_products 還在)
        1. conv.pending_yahoo_ids(_process_new_conv_inner 已提取的)
        2. 從 conv 各文字欄位重新 extract
        3. BOSH 拉 buyer 最近 30 條訊息(包含連發後續訊息的 URL)
        """
        if conv.product_urls:
            return

        # v6.1.35:0. 從 conv.all_products 重建 product_urls
        # 修「conv restore 或新訊息 reset 後 product_urls 空,但 all_products 還在」場景
        try:
            ap = getattr(conv, "all_products", None) or []
            if ap:
                rebuilt: list = []
                for p in ap:
                    src_val = p.get("source", "") if isinstance(p, dict) else ""
                    if src_val in ("xianyu", "mercari"):
                        rebuilt.append({
                            "url": p.get("source_url", ""),
                            "source": src_val,
                            "yahoo_id": p.get("yahoo_id", ""),
                        })
                if rebuilt:
                    conv.product_urls = rebuilt
                    self.on_log(
                        f"[TG] product_urls 空,從 all_products 重建 {len(rebuilt)} 項 "
                        f"(primary source={rebuilt[0]['source']})"
                    )
                    return
        except Exception as _e_ap:
            self.on_log(f"[TG] _ensure_product_urls all_products 重建異常: {_e_ap}")

        try:
            # 1. _process 已提取的 yahoo_ids
            yids: List[str] = list(getattr(conv, "pending_yahoo_ids", None) or [])

            # 2. 從 conv 各文字欄位提取
            if not yids:
                texts = [
                    conv.buyer_text or "",
                    getattr(conv, "original_msg", "") or "",
                    conv.product_text or "",
                    conv.ai_draft or "",
                    conv.ai_internal_note or "",
                ]
                yids = extract_yahoo_item_ids(" ".join(texts))

            # 3. BOSH 拉最近訊息找 URL(處理 buyer 連發、conv.buyer_text 沒更新場景)
            if not yids:
                yids = self._extract_yahoo_ids_from_bosh(conv)

            if not yids:
                self.on_log(f"[TG] _ensure_product_urls: 找不到 Yahoo ID (conv={conv.conv_id[:12]})")
                return

            self.on_log(f"[TG] product_urls 空,fallback 重抓 yids={yids}")
            for yid in yids:
                info = self._fetch_product_info(yid, conv.profile_id)
                if not info:
                    continue
                src_val = info.get("source", "")
                if src_val in ("xianyu", "mercari"):
                    conv.product_urls = [{
                        "url": info.get("source_url", ""),
                        "source": src_val,
                        "yahoo_id": yid,
                    }]
                    conv.product_can_buy = info.get("can_buy", "未知")
                    conv.product_text = info.get("status_text", "") or info.get("source_page_text", "")
                    conv.product_title = info.get("barcode", "")
                    self.on_log(f"[TG] fallback 補上 yid={yid} source={src_val}")
                    return
                else:
                    self.on_log(f"[TG] yid={yid} D1 返回 source={src_val!r}(非 xianyu/mercari)")
        except Exception as e:
            self.on_log(f"[TG] _ensure_product_urls 異常: {e}")

    def _extract_yahoo_ids_from_bosh(self, conv: ConversationState) -> List[str]:
        """BOSH 拉 buyer 最近 30 條訊息,從 msgContent 提取 Yahoo item ID。

        處理場景:buyer 連發 2 條(問句+商品 URL),conv.buyer_text 只記第一條,
        但 BOSH 端有完整歷史。
        """
        try:
            from pathlib import Path
            profile_dir = Path("profiles") / conv.profile_id
            if not profile_dir.exists():
                return []
            from .yahoo_im_bosh_ext import BOSHSession
            from .yahoo_im_jwt import ensure_bosh_jwt
            _, my_user, _ = ensure_bosh_jwt(profile_dir)
            if not my_user:
                return []
            buyer_y = conv.chat_id.upper() if not conv.chat_id.upper().startswith("Y") else conv.chat_id.upper()
            channel = f"yahoo-bid-logbot1:{my_user.lower()}:{buyer_y.lower()}"
            all_texts = []
            with BOSHSession(profile_dir, on_log=lambda *_: None) as s:
                resp, err = s.iq(
                    "juiker:iq:queryMessage",
                    {"chID": channel, "afterN": 30}, iq_type="get",
                )
                if not err and isinstance(resp, dict):
                    msgs = resp.get("messages") or []
                    for m in msgs:
                        content = m.get("msgContent", "") or ""
                        all_texts.append(content)
            yids = extract_yahoo_item_ids(" ".join(all_texts))
            if yids:
                self.on_log(f"[TG] BOSH fallback 找到 yids={yids}")
            return list(yids)
        except Exception as e:
            self.on_log(f"[TG] BOSH yids extract 異常: {e}")
            return []

    def _handle_need_seller_manual(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """手动流程：通知用户去问卖家。"""
        reason = decision.reason or "需要卖家确认"
        missing = "、".join(decision.missing_info) if decision.missing_info else ""

        src = conv.product_urls[0]["source"] if conv.product_urls else ""
        src_hint = "煤炉(Mercari)卖家" if src == "mercari" else "闲鱼卖家" if src == "xianyu" else "卖家"

        _content = f"🏷 需要问{src_hint}:{reason}"
        if missing:
            _content += f"\n缺少信息:{missing}"
        _content += f"\n\n请手动问完{src_hint}后,引用此消息回复卖家的答案。"

        self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
        self._send_phase_buttons(conv, content=_content)

    def _escalate_to_seller(self, conv: ConversationState) -> None:
        """用户在 PREVIEW_SENT 阶段回复 ask，转为问采购方卖家。"""
        # Fallback:product_urls 空時主動補抓 D1(共用 helper)
        self._ensure_product_urls(conv)

        src = conv.product_urls[0]["source"] if conv.product_urls else ""
        buyer_text = conv.buyer_text

        if src == "xianyu" and conv.product_urls:
            self._conv_aware_send(conv, "🔄 轉為自動向閒魚賣家提問...")
            self._escalate_to_seller_auto_xianyu(conv)
            return

        if src == "mercari" and conv.product_urls:
            self._conv_aware_send(conv, "🔄 轉為自動向煤炉賣家留言...")
            self._escalate_to_seller_auto_mercari(conv)
            return

        # v6.1.35:走 manual fallback 前印診斷 log
        try:
            _diag_urls_n = len(conv.product_urls or [])
            _diag_all_n = len(getattr(conv, "all_products", None) or [])
            _diag_pend = list(getattr(conv, "pending_yahoo_ids", None) or [])
            self.on_log(
                f"[TG] _escalate_to_seller manual fallback: src={src!r} "
                f"product_urls_n={_diag_urls_n} all_products_n={_diag_all_n} "
                f"pending_yids={_diag_pend} conv={conv.conv_id[:8]}"
            )
        except Exception:
            pass

        # 其他货源 → 手动
        src_hint = "卖家"
        notify_text = (
            f"🏷 需要问{src_hint}确认\n\n"
            f"请问卖家后，直接回复卖家的答案。\n"
            f"回复 skip → 跳过"
        )
        self._conv_aware_send(conv, notify_text)
        self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
        self._send_phase_buttons(conv)  # v6.0.74

    def _escalate_to_seller_auto_xianyu(self, conv: ConversationState) -> None:
        """从 PREVIEW_SENT 转为问闲鱼卖家，先预览问题。"""
        # v6.1.45 真 root cause 修復:用戶觸發「轉問賣家」=重新走自動流程,清 fallback flag
        conv.auto_ask_fallback = False
        try:
            question = self._generate_seller_question(conv)
            conv.auto_ask_question = question
        except Exception as e:
            self.on_log(f"[TG] 生成卖家问题失败: {e}")
            self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"⚠️ AI 生成问题失败,请手动问闲鱼卖家。\n直接引用此消息回复卖家的答案。")
            return

        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SELLER_QUESTION)
        self._send_phase_buttons(conv,
                                content=f"准备向闲鱼卖家提问:\n{question}")

    def _escalate_to_seller_auto_mercari(self, conv: ConversationState) -> None:
        """从 PREVIEW_SENT 转为问煤炉卖家，先预览问题。"""
        # v6.1.45 真 root cause 修復:用戶觸發「轉問賣家」=重新走自動流程,清 fallback flag
        conv.auto_ask_fallback = False
        try:
            question, zh_hint = self._generate_seller_question_ja(conv)
            conv.auto_ask_question = question
        except Exception as e:
            self.on_log(f"[TG] 生成卖家问题失败: {e}")
            self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"⚠️ AI 生成问题失败,请手动问煤炉卖家。\n直接引用此消息回复卖家的答案。")
            return

        _content = f"准备向煤炉卖家留言(日文):\n{question}"
        if zh_hint:
            _content += f"\n\n(中文意思:{zh_hint})"
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SELLER_QUESTION)
        self._send_phase_buttons(conv, content=_content)

    def _handle_need_seller_auto_xianyu(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """闲鱼货源：AI 生成问题 → 预览等用户确认 → 再发送。"""
        reason = decision.reason or "需要卖家确认"

        # 1) AI 生成问题
        try:
            question = self._generate_seller_question(conv)
            conv.auto_ask_question = question
        except Exception as e:
            self.on_log(f"[TG] 生成卖家问题失败: {e}")
            self._handle_need_seller_manual(conv, decision, buyer_text)
            return

        # v6.1.56:從 commander decision 抓「該附哪幾張買家圖」(AI 判斷)
        # decision.seller_attach_images_indices 是對 conv.buyer_image_urls 的 index list
        # [] = AI 判斷不附圖純文字
        try:
            _all_buyer_imgs = list(getattr(conv, "buyer_image_urls", None) or [])
            _attach_indices = list(getattr(decision, "seller_attach_images_indices", None) or [])
            _attach_reason = str(getattr(decision, "seller_attach_images_reason", "") or "")
            _selected_imgs = [
                _all_buyer_imgs[i] for i in _attach_indices
                if 0 <= i < len(_all_buyer_imgs)
            ][:3]  # cap 3
            conv.seller_question_images = _selected_imgs
            conv.seller_question_images_reason = _attach_reason
            self.on_log(
                f"[TG] AI 判斷附 {len(_selected_imgs)}/{len(_all_buyer_imgs)} 張買家圖給賣家"
                f"(原因: {_attach_reason[:60] or '無'})"
            )
        except Exception as e:
            self.on_log(f"[TG] 解析 seller_attach_images 異常(降級無附圖): {e}")
            conv.seller_question_images = []
            conv.seller_question_images_reason = ""

        # v6.0.74:合并消息(原本 4 条变 1 条)
        _content = (
            f"🏷 需要问卖家:{reason}\n\n"
            f"准备向闲鱼卖家提问:\n{question}"
        )
        # v6.1.56:附圖狀態提示
        _n_attach = len(conv.seller_question_images)
        _n_all = len(getattr(conv, "buyer_image_urls", None) or [])
        if _n_all > 0:
            if _n_attach > 0:
                _content += f"\n\n📎 附 {_n_attach}/{_n_all} 張買家圖一起送"
                if conv.seller_question_images_reason:
                    _content += f"\n   理由:{conv.seller_question_images_reason[:80]}"
            else:
                _content += f"\n\n📝 純文字發送(買家有 {_n_all} 張圖,AI 判斷不需附)"
                if conv.seller_question_images_reason:
                    _content += f"\n   理由:{conv.seller_question_images_reason[:80]}"
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SELLER_QUESTION)
        self._send_phase_buttons(conv, content=_content)

    def _auto_ask_xianyu_worker(self, conv: ConversationState) -> None:
        """后台线程：自动向闲鱼卖家提问并处理结果。"""
        try:
            self._auto_ask_xianyu_inner(conv)
        except Exception as e:
            self.on_log(f"[TG] auto_ask_xianyu error: {e}")
            self._fallback_to_manual(conv, f"异常: {str(e)[:100]}")

    def _auto_ask_xianyu_inner(self, conv: ConversationState) -> None:
        """实际执行闲鱼自动提问逻辑（send-and-check 模式）。

        发送问题后立即关闭浏览器，然后启动定期检查。
        """
        # v6.0.78:純 WebSocket 發送(不開瀏覽器),已移除 Playwright fallback
        from core.xianyu_seller_chat import ask_xianyu_seller_via_ws

        source_url = conv.product_urls[0].get("url", "") if conv.product_urls else ""
        if not source_url:
            self._fallback_to_manual(conv, "缺少闲鱼商品链接")
            return

        if conv.auto_ask_fallback:
            return

        self.on_log(f"[TG] Auto-ask xianyu (WS): url={source_url}")

        # 偵測到滑塊時推 TG(用戶要去屏幕中央拖滑塊)
        _captcha_notified = {"sent": False}
        def _on_captcha():
            if _captcha_notified["sent"]:
                return
            _captcha_notified["sent"] = True
            try:
                self._conv_aware_send(
                    conv,
                    f"⚠️ 閒魚彈出滑塊驗證\n\n"
                    f"系統已把瀏覽器窗口移到屏幕中央,請手動拖動滑塊完成驗證(60 秒內)。\n"
                    f"完成後系統會自動繼續。\n\n"
                    f"💡 如不想再被驗證,可去採購頁「打開閒魚登入瀏覽器」一次養好 /im 端點 cookie。"
                )
            except Exception:
                pass

        # v6.1.56:用 conv.seller_question_images(AI 已選好 + user 可能切換過)
        # 取代 v6.1.54 直接拿 conv.buyer_image_urls 的「無腦傳」邏輯
        _imgs_to_send = list(getattr(conv, "seller_question_images", None) or [])
        if _imgs_to_send:
            self.on_log(f"[TG] 將附 {len(_imgs_to_send)} 張圖給賣家(AI 選 + user 確認後)")
        else:
            _n_buyer = len(getattr(conv, "buyer_image_urls", None) or [])
            if _n_buyer > 0:
                self.on_log(f"[TG] 買家有 {_n_buyer} 張圖,但不附給賣家(AI/user 選擇純文字)")

        result = ask_xianyu_seller_via_ws(
            goofish_url=source_url,
            question=conv.auto_ask_question,
            image_urls=_imgs_to_send if _imgs_to_send else None,
            on_log=self.on_log,
            on_captcha=_on_captcha,
        )

        if conv.auto_ask_fallback:
            return

        if not result.success:
            self.on_log(f"[TG] WS 發送失敗: {result.error}")
            # v6.1.27:訓練 hook — 閒魚賣家發問失敗
            _tc_record(
                "send:xianyu",
                conv=conv,
                input={"question": conv.auto_ask_question, "url": source_url},
                output={"ok": False, "error": str(result.error)[:300]},
                metadata={"channel": "xianyu_ws", "failed": True,
                          "need_login": bool(getattr(result, "need_login", False))},
            )
            # v6.1.45:RGV587 异常码 cooldown 跟「需要登入」分開報,避免用戶被誤導去重登
            _err_str = str(result.error or "")
            _is_rgv587 = "RGV587" in _err_str or "USER_VALIDATE" in _err_str or "被擠爆" in _err_str
            if _is_rgv587:
                self._fallback_to_manual(
                    conv,
                    "閒魚限流冷卻中(RGV587),通常需要等 30 分鐘自動解除\n"
                    "可以稍後重試「轉自動問」,或先手動問賣家"
                )
            elif result.need_login:
                self._fallback_to_manual(conv, "闲鱼需要登录,请先在采购监控中登录闲鱼")
            else:
                self._fallback_to_manual(conv, f"WS 发送失败: {result.error[:120]}")
            return

        # 发送成功 → 保存状态
        conv.seller_chat_url = result.chat_url
        conv.seller_msg_count = result.msg_count_after_send
        conv.seller_sent_question = result.sent_question
        conv.seller_check_count = 0
        conv.seller_read_detected = False
        self.on_log(f"[TG] Xianyu question sent via WS, chat_url={result.chat_url[:80]}")
        # v6.1.27:訓練 hook — 閒魚賣家發問成功
        _tc_record(
            "send:xianyu",
            conv=conv,
            input={"question": conv.auto_ask_question, "url": source_url},
            output={"ok": True, "chat_url": result.chat_url[:200],
                    "sent_question": result.sent_question},
            metadata={"channel": "xianyu_ws"},
        )

        # v6.0.75:WS 已連線,初始化 baseline + 註冊 cid dispatcher(實時推送)
        try:
            from core.xianyu_im_http import (
                extract_peer_user_id, get_baseline_for_peer, get_baseline_for_session,
            )
            from core.purchase_feature import PURCHASE_PROFILE_DIR

            # 優先用 ask_xianyu_seller_via_ws 已返回的 peer_user_id(商品頁 URL 解析會空)
            peer_id = result.peer_user_id or extract_peer_user_id(result.chat_url)
            if not peer_id:
                self.on_log(f"[TG] Xianyu chat_url 无 peerUserId: {result.chat_url[:80]}")
                self._fallback_to_manual(conv, "无法解析 peerUserId,监控不可用")
                return
            conv.seller_peer_user_id = peer_id

            # 優先用 WS 路徑已知的 sessionId,避免再查 session.sync
            sess_id = result.session_id
            version = 0
            ts = 0
            if sess_id:
                # 已有 sessionId,只需取 version/ts baseline
                version, ts, _err = get_baseline_for_session(
                    PURCHASE_PROFILE_DIR, sess_id, on_log=self.on_log,
                )
                if _err:
                    self.on_log(f"[TG] Xianyu baseline (by sid) 警告: {_err}")
            else:
                # 沒 sessionId(舊路徑) → 用 peer 反查
                version, ts, sess_id, err = get_baseline_for_peer(
                    PURCHASE_PROFILE_DIR, peer_id, on_log=self.on_log,
                )
                if err or not sess_id:
                    self.on_log(f"[TG] Xianyu baseline 失败: {err}")
                    self._fallback_to_manual(conv, f"baseline 失败: {err or '未找到对话'}")
                    return

            conv.seller_session_id = sess_id
            conv.seller_baseline_version = version
            # v6.0.79:baseline_ts 取 max(server_ts, 當前時間+1秒)
            # 場景:server 返回的 ts 是「賣家最後訊息時間」(可能是 2 天前),
            #       這會讓「我自己發送的提問 echo」(ts ≈ 當前時間)誤過過濾,
            #       被當成新賣家訊息觸發 _integrate_and_preview(賣家根本沒回!)
            # 修法:baseline_ts 至少設為「發送後 1 秒」,確保自己的 echo ts < baseline_ts 被攔截
            import time as _time
            now_ms = int(_time.time() * 1000)
            conv.seller_baseline_ts = max(ts, now_ms + 1000)
            conv.seller_http_mode = True
            self.on_log(
                f"[TG] Xianyu baseline OK: sid={sess_id} v={version} "
                f"server_ts={ts} baseline_ts={conv.seller_baseline_ts} (取 max 防 echo 誤推)"
            )

            # 註冊 WS dispatcher(對方回覆會即時推送)
            self._ensure_ws_client()
            self._register_ws_cid(conv)
            self.on_log(f"[TG] WS dispatcher 已註冊 cid={sess_id}@goofish peer={peer_id}")
        except Exception as _e_bl:
            self.on_log(f"[TG] Xianyu baseline 异常: {_e_bl}")
            self._fallback_to_manual(conv, f"baseline 异常: {str(_e_bl)[:120]}")
            return

        self._conv_aware_send(
            conv,
            f"✅ 已發送問題給閒魚賣家(WebSocket)\n"
            f"問題:「{conv.auto_ask_question}」\n"
            f"📡 即時推送 + 15s 輪詢備援"
        )

        # v6.1:seller_peer_user_id / session_id / baseline_ts 等已設,手動 persist
        # (重啟後 _load_persisted_convs 還原時要靠這些欄位重啟 timer 檢查賣家)
        try:
            # 暫時清 throttle 記錄,確保這次一定寫入
            if hasattr(self, "_last_persist_ts"):
                self._last_persist_ts.pop(conv.conv_id, None)
            self._persist_conv(conv)
        except Exception:
            pass

        # 啟動 HTTP 輪詢備援(WS 推送是主要,15s HTTP 是備援)
        self._schedule_xianyu_check(conv, delay=15)

    def _schedule_xianyu_check(self, conv: ConversationState, delay: int = 60) -> None:
        """安排下一次闲鱼回复检查。"""
        if conv.auto_ask_fallback:
            return
        if conv.phase not in (ConvPhase.AUTO_ASKING_SELLER,):
            return
        t = threading.Timer(delay, self._xianyu_check_worker, args=(conv,))
        t.daemon = True
        conv.seller_check_timer = t
        t.start()
        self.on_log(f"[TG] Scheduled xianyu check #{conv.seller_check_count + 1} in {delay}s")

    def _xianyu_check_worker(self, conv: ConversationState) -> None:
        """定期检查闲鱼卖家是否回复。"""
        try:
            self._xianyu_check_inner(conv)
        except Exception as e:
            self.on_log(f"[TG] xianyu_check error: {e}")
            self._fallback_to_manual(conv, f"检查回复异常: {str(e)[:100]}")

    def _xianyu_check_inner(self, conv: ConversationState) -> None:
        """实际执行闲鱼回复检查逻辑。
        v6.0.75:纯 HTTP 监控,不再 fallback Playwright。
        失败累计达到 6 次 = 90 秒静默失败,提示用户(只通知一次)但继续轮询。
        """
        if conv.auto_ask_fallback:
            return
        if conv.phase != ConvPhase.AUTO_ASKING_SELLER:
            return

        if not conv.seller_peer_user_id:
            # 没有 peerUserId 无法走 HTTP — 必须切手动
            self._fallback_to_manual(conv, "缺少 peerUserId,无法 HTTP 监控,请手动处理")
            return

        conv.seller_check_count += 1
        self._xianyu_check_http(conv)

    def _xianyu_check_via_list_messages(self, conv: ConversationState, ws) -> None:
        """v6.0.75:WS 在線時用 listUserMessages 主動拉該對話最新訊息(補漏 WS 漏推)。

        listUserMessages 是 LWP 協議 over WS,直接針對該 cid 拉最近 N 條訊息,
        比 session.sync 可靠(session.sync 跟 web 有同步延遲)。

        如果拉到的訊息不在 seller_processed_msg_ids 內 → 觸發整合(走標準 dispatch)。

        v6.1 修復:limit 5→20、加詳細 log、每次跑完 _persist_conv、找不到時也 log 過濾原因
        """
        if not conv.seller_session_id:
            self.on_log(f"[TG] listUserMessages 補漏 skip conv={conv.conv_id[:8]}: 缺 sid")
            return
        try:
            from core.goofish_ws_client import parse_user_message_model
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            from core.xianyu_im_http import get_my_user_id

            cid = f"{conv.seller_session_id}@goofish"
            # v6.1:limit 5→20 防短時間內賣家連發 >5 條被截斷
            # v6.1.51:極端 case 賣家連發 >20 條時自動分頁拉,防中間訊息丟失
            # 用 list_all_user_messages_sync 一次最多拉 100 條(分頁 + baseline_ts 自動切)
            msgs, err = ws.list_all_user_messages_sync(
                cid, max_total=100, page_size=50, per_page_timeout=10,
            )
            if err:
                self.on_log(f"[TG] listUserMessages 補漏失敗 conv={conv.conv_id[:8]} cid={cid}: {err}")
                return
            if not msgs:
                self.on_log(f"[TG] listUserMessages 補漏 conv={conv.conv_id[:8]} cid={cid}: 返回 0 條")
                return

            my_uid = ""
            try:
                my_uid = get_my_user_id(PURCHASE_PROFILE_DIR)
            except Exception:
                pass

            # 倒序找最新「對方發的、尚未處理過的、baseline 之後的」訊息
            target_msg = None
            filter_stats = {"self": 0, "old_baseline": 0, "processed": 0, "parse_fail": 0, "total": len(msgs)}
            for item in msgs:
                parsed = parse_user_message_model(item)
                if not parsed:
                    filter_stats["parse_fail"] += 1
                    continue
                # 跳過自己發的
                if parsed.sender_uid and my_uid and parsed.sender_uid == my_uid:
                    filter_stats["self"] += 1
                    continue
                # v6.0.78:跳過 baseline 之前的舊訊息(關鍵修復)
                # 舊版只看 seller_processed_msg_ids 不夠 — 剛建對話時 set 是空,
                # 所有歷史訊息(包括發送提問前的圖片/卡片)都會被當「新訊息」推送
                if (parsed.created_ts and conv.seller_baseline_ts
                        and parsed.created_ts < conv.seller_baseline_ts):
                    filter_stats["old_baseline"] += 1
                    continue
                # 已處理過 → 跳過
                if parsed.message_id and parsed.message_id in conv.seller_processed_msg_ids:
                    filter_stats["processed"] += 1
                    continue
                target_msg = parsed
                break

            if not target_msg:
                # 拿不到新訊息 — log 過濾統計,讓問題可定位
                self.on_log(
                    f"[TG] listUserMessages 補漏 conv={conv.conv_id[:8]} 沒新訊息 "
                    f"(共 {filter_stats['total']} 條: 自己={filter_stats['self']} "
                    f"舊={filter_stats['old_baseline']} 已處理={filter_stats['processed']} "
                    f"parse_fail={filter_stats['parse_fail']}) baseline_ts={conv.seller_baseline_ts}"
                )
                return

            self.on_log(
                f"[TG] listUserMessages 補漏拿到新訊息 conv={conv.conv_id[:8]}: "
                f"text={target_msg.content_text[:60]!r} msg_id={target_msg.message_id} "
                f"created_ts={target_msg.created_ts}"
            )
            # 走標準 dispatch(含訊息級 dedupe + AI 自動判斷 + 連發累積邏輯)
            target_msg.is_session_event = False
            target_msg.object_type = 40000
            self._on_ws_inbound_msg(target_msg)
        except Exception as e:
            self.on_log(f"[TG] _xianyu_check_via_list_messages 異常 conv={conv.conv_id[:8]}: {e}")

    def _xianyu_check_http(self, conv: ConversationState) -> None:
        """v6.0.75 新增:纯 HTTP 模式检查闲鱼卖家回复(不开浏览器)。

        v6.0.75 設計:WS 推送是主路徑 + listUserMessages 補漏 + session.sync 兜底。
        - WS 在線:每 60s 用 listUserMessages 主動拉該對話最新訊息(server 漏推時補上)
        - WS 斷線:走 session.sync 嘗試找對話
        失败累计到阈值时提示用户,但不切 Playwright,继续 HTTP 重试到 24 小时上限。
        """
        try:
            from core.goofish_ws_client import XianyuWsClient
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            ws = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=self.on_log)
            ws_connected = ws.is_connected()
        except Exception:
            ws_connected = False

        # WS 在線時,**用 listUserMessages 主動補漏**
        # (server 可能漏推訊息,WS 連著不代表訊息一定收到,主動拉一次保險)
        if ws_connected and conv.seller_session_id:
            self.on_log(
                f"[TG] Xianyu check #{conv.seller_check_count} (WS路徑) "
                f"conv={conv.conv_id[:8]} sid={conv.seller_session_id}"
            )
            self._xianyu_check_via_list_messages(conv, ws)
            # v6.1:每次 check 後持久化,讓 disk 反映實際運行狀態(否則重啟看不到 count 變化)
            try:
                if hasattr(self, "_last_persist_ts"):
                    self._last_persist_ts.pop(conv.conv_id, None)
                self._persist_conv(conv)
            except Exception:
                pass
            if conv.seller_check_count >= 2880:
                self._fallback_to_manual(conv, "已等待 24 小時無賣家回覆")
                return
            self._schedule_xianyu_check(conv, delay=60)
            return

        try:
            from core.xianyu_im_http import check_seller_reply_http
        except Exception as e:
            self.on_log(f"[TG] xianyu_im_http import 失败: {e}")
            self._fallback_to_manual(conv, f"HTTP 模块导入失败: {e}")
            return

        self.on_log(
            f"[TG] Xianyu check #{conv.seller_check_count}: peerId={conv.seller_peer_user_id} "
            f"baseline_v={conv.seller_baseline_version}"
        )
        result = check_seller_reply_http(
            PURCHASE_PROFILE_DIR,
            peer_user_id=conv.seller_peer_user_id,
            sent_question=conv.seller_sent_question,
            baseline_version=conv.seller_baseline_version,
            baseline_ts=conv.seller_baseline_ts,
            on_log=self.on_log,
        )
        if conv.auto_ask_fallback:
            return

        if result.need_login:
            # cookie 失效 — 必须用户手动处理,无法纯 HTTP 自动恢复
            self._fallback_to_manual(conv, "闲鱼登录失效,请到采购页重新登录")
            return

        if result.error:
            is_not_found = (not result.found) or "未找到" in result.error
            # v6.1:not_found 不算 fail — 對話可能在 fetch 500 外,但 WS 主路徑還在工作
            # 只有「真失敗」(cookie 失效 / API 500 / 網路) 才累計 fail_count
            if not is_not_found:
                conv.seller_http_fail_count += 1
                self.on_log(
                    f"[TG] Xianyu HTTP error #{conv.seller_http_fail_count}: {result.error}"
                )
                # 失敗 6 次才通知 cookie 過期
                if conv.seller_http_fail_count == 6:
                    self._conv_aware_send(
                        conv,
                        f"⚠️ 閒魚 HTTP 監控連續失敗 {conv.seller_http_fail_count} 次\n"
                        f"錯誤:{result.error[:120]}\n"
                        f"可能 cookie 過期,請去採購頁重新登入閒魚;WS 推送仍在工作。"
                    )
                if conv.seller_http_fail_count >= 20:
                    self._fallback_to_manual(conv, f"HTTP 监控连续失败 {conv.seller_http_fail_count} 次")
                    return
            else:
                # not_found 靜默重試,延長間隔避免無謂呼叫
                self.on_log(
                    f"[TG] Xianyu not_found check #{conv.seller_check_count} "
                    f"peer={conv.seller_peer_user_id}(WS 主路徑工作中,HTTP 靜默重試)"
                )

            if conv.seller_check_count >= 2880:
                self._fallback_to_manual(conv, "HTTP 检查 24 小时仍持续失败")
                return
            # not_found case:延長 interval 到 60s(WS 是主路徑,HTTP 沒必要 15s 高頻 retry)
            if is_not_found:
                delay = 60 if conv.seller_check_count < 60 else 120
            else:
                delay = 15 if conv.seller_check_count < 240 else 30
            self._schedule_xianyu_check(conv, delay=delay)
            return

        # 成功 — 重置失败计数
        conv.seller_http_fail_count = 0

        if result.has_reply:
            # v6.0.75:訊息級 dedupe — 用 (version, ts) 當 key 防 WS/HTTP 雙路雙重整合
            # 如果 WS 已先處理過這條訊息,HTTP 就跳過
            http_msg_key = f"http_v{result.new_version}_ts{result.new_ts}"
            if http_msg_key in conv.seller_processed_msg_ids:
                self.on_log(
                    f"[TG] HTTP 命中 dedupe(WS 已處理過): key={http_msg_key}, "
                    f"text={result.seller_reply[:60]!r}"
                )
                # 仍要更新 baseline 否則下次又會撞到
                conv.seller_baseline_version = result.new_version
                conv.seller_baseline_ts = result.new_ts
                delay = 15 if conv.seller_check_count < 240 else 30
                self._schedule_xianyu_check(conv, delay=delay)
                return
            # 標記已處理
            conv.seller_processed_msg_ids.add(http_msg_key)
            if len(conv.seller_processed_msg_ids) > 20:
                conv.seller_processed_msg_ids = set(
                    list(conv.seller_processed_msg_ids)[-20:]
                )
            # 同步更新 baseline
            conv.seller_baseline_version = result.new_version
            conv.seller_baseline_ts = result.new_ts

            conv.seller_answer = result.seller_reply
            # v6.0.75:疑似賣家 AI 回覆 → 繼續監控,不切手動(等真人來再整合)
            if result.is_suspected_ai:
                self.on_log(f"[TG] Xianyu suspected AI reply: {result.seller_reply[:100]}(繼續輪詢等真人)")
                ai_count = (getattr(conv, 'seller_ai_count', 0) or 0) + 1
                conv.seller_ai_count = ai_count
                if ai_count == 1:
                    self._conv_aware_send(
                        conv,
                        f"🤖 閒魚賣家暫不在,自動回覆\n\n"
                        f"賣家(AI):「{result.seller_reply[:200]}」\n\n"
                        f"⏳ 繼續等賣家本人回覆(輪詢持續監控,無需操作)\n"
                        f"💡 若已等夠,可點 [自己回] 或 reply:<內容> 手動處理"
                    )
                # 不切 phase,維持 AUTO_ASKING_SELLER 繼續輪詢
                delay = 15 if conv.seller_check_count < 240 else 30
                self._schedule_xianyu_check(conv, delay=delay)
                return

            # 賣家連發累積:整合中或已預覽 → 追加並通知,不重複整合
            if conv.seller_integrating or conv.phase == ConvPhase.PREVIEW_SELLER:
                conv.seller_extra_msgs.append(result.seller_reply)
                extra_count = len(conv.seller_extra_msgs)
                self.on_log(
                    f"[TG] HTTP 賣家補發第 {extra_count} 條: {result.seller_reply[:60]!r}"
                )
                self._conv_aware_send(
                    conv,
                    f"➕ 閒魚賣家補發\n\n"
                    f"賣家又說:「{result.seller_reply[:200]}」\n\n"
                    f"💡 已累積 {extra_count} 條補發訊息。可選擇:\n"
                    f"  • 點 [🔄 更新整合] 把新訊息納入合併重整合(等下次預覽刷新)\n"
                    f"  • 或繼續用 ok / edit / reply 處理當前草稿"
                )
                # v6.1.51:重發 PREVIEW_SELLER buttons 帶上新的 [🔄 更新整合]
                # (PREVIEW_SELLER 階段 keyboard 會根據 seller_extra_msgs 長度決定是否顯示)
                try:
                    if conv.phase == ConvPhase.PREVIEW_SELLER:
                        self._send_phase_buttons(
                            conv,
                            content=f"📥 賣家補發 — 待你決定:更新整合 OR 用目前草稿",
                        )
                except Exception:
                    pass
                delay = 15 if conv.seller_check_count < 240 else 30
                self._schedule_xianyu_check(conv, delay=delay)
                return

            self.on_log(f"[TG] Xianyu seller replied: {result.seller_reply[:100]}")
            # v6.1.27:訓練 hook — 賣家真人回覆到達(用於計算等待時間)
            _tc_record(
                "seller:reply_arrived",
                conv=conv,
                input={"check_count": conv.seller_check_count,
                       "channel": "xianyu_http"},
                output={"seller_reply": result.seller_reply[:1000],
                        "is_real_human": True},
                metadata={"wait_until_reply": True},
            )
            conv.seller_integrating = True
            try:
                self._integrate_and_preview(conv, result.seller_reply)
            finally:
                conv.seller_integrating = False
            return

        # 没有回复 — 继续 HTTP 轮询
        # 前 240 次 (~1 小时) 每 15 秒;之后每 30 秒;24 小时上限 (2880 次)
        max_checks = 2880
        if conv.seller_check_count >= max_checks:
            # v6.1.27:訓練 hook — 等到 24 小時超時
            _tc_record(
                "seller:no_reply_timeout",
                conv=conv,
                input={"check_count": conv.seller_check_count,
                       "channel": "xianyu_http"},
                output={"reason": "24h_no_reply"},
                metadata={"max_checks": max_checks},
            )
            self._fallback_to_manual(conv, "HTTP 检查 24 小时卖家仍未回复")
            return

        delay = 15 if conv.seller_check_count < 240 else 30
        self._schedule_xianyu_check(conv, delay=delay)

    def _fallback_to_manual(self, conv: ConversationState, reason: str) -> None:
        """自动提问失败，降级为手动流程。"""
        conv.auto_ask_fallback = True
        # 取消定时检查
        if conv.seller_check_timer:
            try:
                conv.seller_check_timer.cancel()
            except Exception:
                pass
            conv.seller_check_timer = None
        self.on_log(f"[TG] Auto-ask fallback: {reason}")

        src = conv.product_urls[0]["source"] if conv.product_urls else ""
        src_hint = "煤炉(Mercari)卖家" if src == "mercari" else "闲鱼卖家" if src == "xianyu" else "卖家"
        # v6.0.74:合并消息
        _content = (
            f"⚠️ 自动提问失败:{reason}\n\n"
            f"请手动问{src_hint}后,引用此消息回复卖家的答案。"
        )
        # v6.1.45:強制持久化 phase 轉換,確保 disk 一致(關鍵修復)
        # 修「disk 上 phase=AUTO_ASKING_SELLER + auto_ask_fallback=true 內部矛盾」bug
        # 場景:_set_phase(AUTO_ASKING_SELLER) 剛 persist 後 5s 內,_set_phase(WAIT_SELLER)
        # 的 _persist_conv 被 throttle 跳過 → disk 卡在 AUTO_ASKING_SELLER 但 in-memory fallback=true
        # → 重啟 _load_persisted_convs 又把它當 active AUTO_ASKING_SELLER 排 check → 又 fallback 循環
        try:
            if hasattr(self, "_last_persist_ts"):
                self._last_persist_ts.pop(conv.conv_id, None)
        except Exception:
            pass
        self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
        self._send_phase_buttons(conv, content=_content)

    def _retry_auto_ask(self, conv: ConversationState) -> None:
        """重试自动留言（用户登录后）。"""
        conv.auto_ask_fallback = False  # 重置fallback标志
        src = conv.product_urls[0]["source"] if conv.product_urls else ""
        question = conv.auto_ask_question
        if not question:
            self._conv_aware_send(conv, "⚠️ 没有待发送的问题，请手动处理。")
            return
        if src == "mercari":
            self._set_phase(conv.conv_id, ConvPhase.AUTO_ASKING_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"🔄 重试自动留言:\n{question}")
            threading.Thread(
                target=self._auto_ask_mercari_worker,
                args=(conv,),
                daemon=True,
            ).start()
        elif src == "xianyu":
            self._set_phase(conv.conv_id, ConvPhase.AUTO_ASKING_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"🔄 重试自动留言:\n{question}")
            threading.Thread(
                target=self._auto_ask_xianyu_worker,
                args=(conv,),
                daemon=True,
            ).start()
        else:
            self._conv_aware_send(conv, "⚠️ 未知货源,请手动处理。")

    def _handle_need_seller_auto_mercari(self, conv: ConversationState, decision, buyer_text: str) -> None:
        """煤炉货源：AI 生成问题 → 预览等用户确认 → 再留言。"""
        reason = decision.reason or "需要卖家确认"

        # 1) AI 生成问题（日文）
        try:
            question, zh_hint = self._generate_seller_question_ja(conv, reason=reason)
            conv.auto_ask_question = question
        except Exception as e:
            self.on_log(f"[TG] 生成煤炉问题失败: {e}")
            self._handle_need_seller_manual(conv, decision, buyer_text)
            return

        # v6.0.74:合并消息
        _content = (
            f"🏷 需要问卖家:{reason}\n\n"
            f"准备向煤炉卖家留言(日文):\n{question}\n\n"
            f"(中文意思:{zh_hint or reason})"
        )
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SELLER_QUESTION)
        self._send_phase_buttons(conv, content=_content)

    def _auto_ask_mercari_worker(self, conv: ConversationState) -> None:
        """后台线程：自动向煤炉卖家留言并处理结果。"""
        try:
            self._auto_ask_mercari_inner(conv)
        except Exception as e:
            self.on_log(f"[TG] auto_ask_mercari error: {e}")
            self._fallback_to_manual(conv, f"异常: {str(e)[:100]}")

    def _auto_ask_mercari_inner(self, conv: ConversationState) -> None:
        """实际执行煤炉自动留言逻辑（send-and-check 模式）。"""
        from core.mercari_seller_comment import comment_mercari_seller_sync

        source_url = conv.product_urls[0].get("url", "") if conv.product_urls else ""
        if not source_url:
            self._fallback_to_manual(conv, "缺少煤炉商品链接")
            return

        if conv.auto_ask_fallback:
            return

        chrome_path = self._get_chrome_path()
        self.on_log(f"[TG] Auto-ask mercari: url={source_url}")

        result = comment_mercari_seller_sync(
            mercari_url=source_url,
            question=conv.auto_ask_question,
            on_log=self.on_log,
            chrome_path=chrome_path,
        )

        if conv.auto_ask_fallback:
            return

        if result.need_login:
            # v6.1.27:訓練 hook — 煤炉留言失敗(需登入)
            _tc_record(
                "send:mercari",
                conv=conv,
                input={"question": conv.auto_ask_question, "url": source_url},
                output={"ok": False, "error": "need_login"},
                metadata={"channel": "mercari_playwright", "failed": True, "need_login": True},
            )
            self._fallback_to_manual(conv, "煤炉需要登录，请先在采购监控中登录煤炉")
            return

        if not result.success:
            # v6.1.27:訓練 hook — 煤炉留言失敗
            _tc_record(
                "send:mercari",
                conv=conv,
                input={"question": conv.auto_ask_question, "url": source_url},
                output={"ok": False, "error": str(result.error or "")[:300]},
                metadata={"channel": "mercari_playwright", "failed": True},
            )
            self._fallback_to_manual(conv, result.error or "煤炉留言失败")
            return

        # 发送成功 → 保存状态，启动定期检查
        conv.seller_chat_url = result.mercari_url
        conv.seller_msg_count = result.comment_count_after_send
        conv.seller_sent_question = result.sent_question
        conv.seller_check_count = 0
        self.on_log(f"[TG] Mercari comment sent, url={result.mercari_url[:80]}")
        # v6.1.27:訓練 hook — 煤炉留言成功
        _tc_record(
            "send:mercari",
            conv=conv,
            input={"question": conv.auto_ask_question, "url": source_url},
            output={"ok": True, "mercari_url": result.mercari_url[:200],
                    "sent_question": result.sent_question},
            metadata={"channel": "mercari_playwright"},
        )

        self._conv_aware_send(conv,
            f"✅ 已发送留言给煤炉卖家，等待回复中...\n"
            f"留言：「{conv.auto_ask_question}」\n"
            f"系统会自动检查卖家回复（最长持续24小时）\n"
            f"回复 manual → 切换手动模式"
        )

        # v6.1:seller_* 已設,手動 persist 供重啟後還原 timer 用
        try:
            if hasattr(self, "_last_persist_ts"):
                self._last_persist_ts.pop(conv.conv_id, None)
            self._persist_conv(conv)
        except Exception:
            pass

        # 启动第一次检查（90秒后，煤炉留言回复通常较慢）
        self._schedule_mercari_check(conv)

    def _schedule_mercari_check(self, conv: ConversationState, delay: int = 90) -> None:
        """安排下一次煤炉回复检查。"""
        if conv.auto_ask_fallback:
            return
        if conv.phase != ConvPhase.AUTO_ASKING_SELLER:
            return
        t = threading.Timer(delay, self._mercari_check_worker, args=(conv,))
        t.daemon = True
        conv.seller_check_timer = t
        t.start()
        self.on_log(f"[TG] Scheduled mercari check #{conv.seller_check_count + 1} in {delay}s")

    def _mercari_check_worker(self, conv: ConversationState) -> None:
        """定期检查煤炉卖家是否回复。"""
        try:
            self._mercari_check_inner(conv)
        except Exception as e:
            self.on_log(f"[TG] mercari_check error: {e}")
            self._fallback_to_manual(conv, f"检查回复异常: {str(e)[:100]}")

    def _mercari_check_inner(self, conv: ConversationState) -> None:
        """实际执行煤炉回复检查逻辑。"""
        from core.mercari_seller_comment import check_mercari_reply_sync

        if conv.auto_ask_fallback:
            return
        if conv.phase != ConvPhase.AUTO_ASKING_SELLER:
            return

        conv.seller_check_count += 1
        chrome_path = self._get_chrome_path()
        self.on_log(f"[TG] Mercari check #{conv.seller_check_count}: {conv.seller_chat_url[:60]}")

        result = check_mercari_reply_sync(
            mercari_url=conv.seller_chat_url,
            comment_count_after_send=conv.seller_msg_count,
            sent_question=conv.seller_sent_question,
            on_log=self.on_log,
            chrome_path=chrome_path,
        )

        if conv.auto_ask_fallback:
            return

        if result.need_login:
            self._fallback_to_manual(conv, "煤炉需要登录，请先在采购监控中登录煤炉")
            return

        if result.error and not result.has_reply:
            self.on_log(f"[TG] Mercari check error: {result.error}")
            delay = 90 if conv.seller_check_count < 10 else 300
            if conv.seller_check_count < 288:
                self._schedule_mercari_check(conv, delay=delay)
            else:
                self._fallback_to_manual(conv, f"持续检查仍失败: {result.error}")
            return

        if result.has_reply:
            conv.seller_answer = result.seller_reply
            self.on_log(f"[TG] Mercari seller replied: {result.seller_reply[:100]}")
            # v6.1.27:訓練 hook — 煤炉賣家回覆到達
            _tc_record(
                "seller:reply_arrived",
                conv=conv,
                input={"check_count": conv.seller_check_count,
                       "channel": "mercari_playwright"},
                output={"seller_reply": result.seller_reply[:1000]},
                metadata={"wait_until_reply": True},
            )
            self._integrate_and_preview(conv, result.seller_reply)
            return

        # 没有回复，继续检查（前10次每90秒，之后每5分钟，最长24小时）
        if conv.seller_check_count < 288:
            delay = 90 if conv.seller_check_count < 10 else 300
            self._schedule_mercari_check(conv, delay=delay)
        else:
            # v6.1.27:訓練 hook — 煤炉等到 24h 超時
            _tc_record(
                "seller:no_reply_timeout",
                conv=conv,
                input={"check_count": conv.seller_check_count,
                       "channel": "mercari_playwright"},
                output={"reason": "24h_no_reply"},
                metadata={"max_checks": 288},
            )
            self._fallback_to_manual(conv, "检查24小时卖家仍未回复")

    # ---------- TG 用户回复处理 ----------

    def _on_tg_reply(self, text: str, message_id: int, reply_to_msg_id: Optional[int] = None) -> None:
        """TG Bot 收到用户消息时的回调。"""
        text = (text or "").strip()
        # v6.0.83:從 telegram_bot 拿 photo/video file_id(per-message)
        photo_fid = getattr(self.tg, "last_photo_file_id", "") or ""
        video_fid = getattr(self.tg, "last_video_file_id", "") or ""

        # 純媒體訊息(無 text)也要繼續走 forum reply 路徑
        if not text and not photo_fid and not video_fid:
            return

        # v6.0.83:Forum reply pending — 用戶引用回覆 force_reply prompt → dispatch 到 Yahoo
        if reply_to_msg_id:
            with self._forum_pending_lock:
                entry = self._forum_pending_replies.pop(int(reply_to_msg_id), None)
            if entry:
                self._dispatch_forum_reply_to_yahoo(entry, text, photo_fid, video_fid)
                return
        if not text and not photo_fid:
            return  # 沒對應 pending 且無文字無圖片 — 不處理

        # v6.1.20:一鍵轉刊 state machine — 比所有其他命令更早路由,優先生效
        _chat_id_str = (self.tg.chat_id or "").strip()
        if _chat_id_str:
            # 1. /relist 命令
            if text and (
                text.lower().startswith("/relist")
                or text.startswith("/轉刊") or text.startswith("/转刊")
            ):
                if self._relist_handle_command(text, _chat_id_str, topic_id=0):
                    return
            # 2. 進行中 session:文字輸入(await_input)
            _rl_sess = self._relist_get_session(_chat_id_str)
            if _rl_sess:
                if _rl_sess.state == "await_input" and text:
                    if self._relist_handle_text_input(text, _chat_id_str, 0):
                        return
                if _rl_sess.state == "await_photo" and photo_fid:
                    if self._relist_handle_photo(photo_fid, _chat_id_str, 0):
                        return
        if not text:
            return

        # v6.0.83:TG forum menu 命令 — /accounts /buyers /history
        # 以 / 開頭且匹配 menu 命令時優先處理(避免被既有命令解析器吞)
        if text.startswith("/") and self._handle_forum_menu_command(text, message_id):
            return

        # v6.0.74:优先检查 force_reply pending input
        # 用户点了 [修改]/[AI重写]/[自己回] 按钮后,Bot 发了 force_reply 提示
        # 现在用户回复了那条提示,根据 action 走对应逻辑
        if reply_to_msg_id:
            _pending = self._consume_pending_input(reply_to_msg_id)
            if _pending:
                _cid, _action = _pending
                self._handle_pending_input(_cid, _action, text)
                return

        # v6.0.78:TG Desktop 桌面版 force_reply 經常不會自動觸發引用 →
        # reply_to_msg_id 為空 → 走到通用命令路徑 → 回 help 訊息讓使用者困惑。
        # 備援:若 chat 最近 5 分鐘內有 pending(用戶剛點了修改/AI 重寫/自己回),
        # 且當前 text 明顯不是命令(無命令前綴)→ 視為對 pending 的回覆。
        _chat_id = (self.tg.chat_id or "").strip()
        if _chat_id and not reply_to_msg_id:
            # 判斷是否「看起來像命令」(有命令前綴/符號)→ 不算 pending 回覆
            _lower = text.lower().lstrip()
            _norm = _lower.replace("：", ":")  # 全角冒號歸一
            _CMD_PREFIXES = (
                "ok", "skip", "ask", "manual", "retry", "read",
                "mod:", "edit:", "reply:",
                "/", "#",
            )
            _looks_like_cmd = any(_norm.startswith(p) for p in _CMD_PREFIXES)
            if not _looks_like_cmd:
                _pending2 = self._consume_latest_pending(_chat_id)
                if _pending2:
                    _cid, _action = _pending2
                    self.on_log(
                        f"[TG] force_reply fallback 命中:chat={_chat_id} "
                        f"conv={_cid[:8]} action={_action} text={text[:40]!r}"
                    )
                    self._handle_pending_input(_cid, _action, text)
                    return

        # 翻译模式:如果用户处于翻译模式,优先处理翻译
        chat_id = self.tg.chat_id or ""
        tr_dir = self._translate_mode.get(chat_id)
        if tr_dir and text not in ("/status", "/help", "/translate", "/start"):
            if text.lower() == "exit":
                self._translate_mode.pop(chat_id, None)
                self.tg.send("已退出翻譯模式。")
                return
            self._do_translate(chat_id, tr_dir, text)
            return

        # /status 命令：列出活跃对话
        if text == "/status":
            self._handle_status_cmd()
            return

        # reply → 覆盖 NO_REPLY 判断，强制生成回复
        if text.lower() == "reply":
            self._handle_reply_override()
            return

        # ---- 解析 #N 前缀：指定对话编号 ----
        import re as _re
        conv = None
        m = _re.match(r'^#(\d+)\s+(.*)', text, _re.DOTALL)
        if m:
            idx = int(m.group(1))
            text = m.group(2).strip()
            if not text:
                return
            conv = self._find_conv_by_index(idx)
            if not conv:
                total = len(self._get_sorted_active_convs())
                self.tg.send(f"编号 #{idx} 不存在（当前 {total} 个活跃对话，用 /status 查看）。")
                return
        elif reply_to_msg_id:
            # 用户引用回复了某条 Bot 消息 → 通过 tg_msg_id 定位对话
            conv = self._find_conv_by_tg_msg_id(reply_to_msg_id)
            if not conv:
                # 引用的消息不属于任何活跃对话，回退到默认
                conv = self._find_active_conv()
                if not conv:
                    self.tg.send("当前没有活跃的对话。")
                    return
        else:
            conv = self._find_active_conv()
            if not conv:
                self.tg.send("当前没有活跃的对话。")
                return

        cmd = text.lower().replace("：", ":")
        if cmd == "ask" and conv.phase == ConvPhase.PREVIEW_SENT:
            # v6.1.27:訓練 hook — 文字命令 ask
            _tc_record(
                "user:ask",
                conv=conv,
                input={"text": text, "phase": str(conv.phase)},
                output=None,
                chosen_action="ask",
                metadata={"channel": "text"},
            )
            self._escalate_to_seller(conv)
            return

        # skip → 跳过当前对话
        if cmd == "skip":
            # v6.1.27:訓練 hook — 文字命令 skip
            _tc_record(
                "user:skip",
                conv=conv,
                input={"text": text, "phase": str(conv.phase)},
                output=None,
                chosen_action="skip",
                metadata={"channel": "text"},
            )
            self._set_phase(conv.conv_id, ConvPhase.EXPIRED)
            self.tg.send(f"⏭ 已跳过对话（买家 {conv.buyer_label}）。")
            return

        # retry → ERROR 阶段重新触发 AI 处理
        if cmd == "retry" and conv.phase == ConvPhase.ERROR:
            # v6.1.27:訓練 hook — 文字命令 retry
            _tc_record(
                "user:retry",
                conv=conv,
                input={"text": text, "phase": str(conv.phase)},
                output=None,
                chosen_action="retry",
                metadata={"channel": "text"},
            )
            self.tg.send(f"🔄 正在重新处理（买家 {conv.buyer_label}）...")
            self._set_phase(conv.conv_id, ConvPhase.PENDING_AI)
            threading.Thread(
                target=self._process_new_conv_inner,
                args=(conv.conv_id,),
                daemon=True,
            ).start()
            return

        # PREVIEW_SENT 或 PREVIEW_SELLER 阶段：等用户确认草稿
        if conv.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
            # v6.1.27:訓練數據紀錄 — 文字命令(ok/edit:/mod:/reply:)直接打字的路徑
            _tc_record(
                "user:text_command",
                conv=conv,
                input={"text": text, "phase": str(conv.phase)},
                output=text,
                ai_draft=conv.ai_integrated_draft or conv.ai_draft or "",
                chosen_action=text.lower().replace("：", ":")[:20],
                metadata={"channel": "text", "phase_before": str(conv.phase)},
            )
            self._handle_confirm_reply(conv, text)
            return

        # PREVIEW_SELLER_QUESTION 阶段：用户确认/修改问卖家的问题
        if conv.phase == ConvPhase.PREVIEW_SELLER_QUESTION:
            # v6.1.27:訓練數據紀錄 — 卖家问题阶段的文字命令
            _tc_record(
                "user:text_command_seller_q",
                conv=conv,
                input={"text": text, "phase": str(conv.phase)},
                output=text,
                ai_draft=conv.auto_ask_question or "",
                chosen_action=text.lower().replace("：", ":")[:20],
                metadata={"channel": "text", "phase_before": str(conv.phase)},
            )
            self._handle_confirm_seller_question(conv, text)
            return

        # WAIT_SELLER 阶段：用户提供卖家答案
        if conv.phase == ConvPhase.WAIT_SELLER:
            if cmd == "retry":
                # v6.1.27:訓練 hook — WAIT_SELLER retry(重新問賣家)
                _tc_record(
                    "user:retry",
                    conv=conv,
                    input={"text": text, "phase": str(conv.phase)},
                    output=None,
                    chosen_action="retry_ask_seller",
                    metadata={"channel": "text", "context": "wait_seller"},
                )
                self._retry_auto_ask(conv)
                return
            if cmd.startswith("reply:"):
                reply_content = text[6:].strip()
                if not reply_content:
                    self.tg.send("reply: 后面请输入你要回复给买家的内容。")
                    return
                # v6.1.27:訓練 hook — WAIT_SELLER reply:(不等賣家直接回買家)
                _tc_record(
                    "user:reply_text",
                    conv=conv,
                    input={"text": text, "phase": str(conv.phase)},
                    output=reply_content,
                    chosen_action="reply_skip_seller",
                    metadata={"channel": "text", "context": "wait_seller"},
                )
                conv.final_reply = reply_content
                # v6.0.74:跳过中间消息
                conv._skip_remind_on_done = True
                self._set_phase(conv.conv_id, ConvPhase.DONE)
                self._supervisor_send(
                    f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                    f"最终回复(直接回复):「{conv.final_reply}」"
                )
                threading.Thread(
                    target=self._auto_send_to_yahoo,
                    args=(conv,),
                    daemon=True,
                ).start()
                return
            self._handle_seller_answer(conv, text)
            return

        # AUTO_ASKING_SELLER 阶段：支持 manual 和 skip
        if conv.phase == ConvPhase.AUTO_ASKING_SELLER:
            if cmd == "manual":
                # v6.1.27:訓練 hook — 文字命令 manual(自動問賣家失敗 → 切手動)
                _tc_record(
                    "user:manual",
                    conv=conv,
                    input={"text": text, "phase": str(conv.phase)},
                    output=None,
                    chosen_action="manual",
                    metadata={"channel": "text", "context": "auto_asking_seller"},
                )
                # 取消定时检查
                if conv.seller_check_timer:
                    try:
                        conv.seller_check_timer.cancel()
                    except Exception:
                        pass
                    conv.seller_check_timer = None
                conv.auto_ask_fallback = True
                self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
                self.tg.send(
                    f"🔄 已切换手动模式。\n"
                    f"请手动问闲鱼卖家后，直接回复卖家的答案。\n"
                    f"回复 skip → 跳过"
                )
                return
            self.tg.send(
                f"🤖 正在自动等待卖家回复中...\n"
                f"已检查 {conv.seller_check_count} 次\n"
                f"回复 manual → 切换手动模式\n"
                f"回复 skip → 跳过"
            )
            return

        # 其他阶段：提示
        self.tg.send(
            f"当前对话状态：{conv.phase.value}\n"
            f"买家：{conv.buyer_label}\n"
            f"暂时无法处理此回复。"
        )

    # ---------- v6.0.74 Inline Keyboard 操作版面 ----------

    def _build_buttons_for_phase(self, conv: ConversationState) -> List[List[Dict[str, str]]]:
        """根据对话当前阶段,返回 inline_keyboard 按钮配置。

        callback_data 格式: cs:{action}:{conv_id}
        - ok    : 确认发送(AI 草稿/卖家问题/整合回复)
        - edit  : 修改回复(force_reply 收集替换内容)
        - rw    : AI 重写(force_reply 收集指令)
        - rpy   : 自己回买家(force_reply 收集回复内容)
        - ask   : 转为问卖家
        - rd    : 只消红点(不发消息,DONE + mark_read) ← v6.0.75 新增
        - skp   : 跳过(EXPIRED 保留红点)
        - rty   : 重试(ERROR 阶段)
        - mnl   : 切手动(AUTO_ASKING_SELLER 阶段)

        三种结案方式语义对照:
        - 确认发送 → DONE + 发 AI 草稿到买家 + 消红点(顺带)
        - 只消红点 → DONE + 仅消红点,不发消息(看过了,不回)
        - 跳过    → EXPIRED 保留红点,下次刷新会再看到
        """
        cid = conv.conv_id
        phase = conv.phase

        if phase == ConvPhase.PREVIEW_SENT:
            # STICKER_READ 特例:广告/贴图无文字回复,只需消红点/跳过
            if conv.ai_action == "STICKER_READ":
                return [
                    [{"text": "✅  消红点  ✅", "callback_data": f"cs:ok:{cid}"}],
                    [{"text": "跳过", "callback_data": f"cs:skp:{cid}"}],
                ]
            # AI 草稿待用户确认
            rows = [
                [{"text": "✅  确认发送  ✅", "callback_data": f"cs:ok:{cid}"}],
                [
                    {"text": "修改", "callback_data": f"cs:edit:{cid}"},
                    {"text": "AI重写", "callback_data": f"cs:rw:{cid}"},
                    {"text": "问卖家", "callback_data": f"cs:ask:{cid}"},
                ],
                [
                    {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                    {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
                ],
            ]
            # v6.1.1:貨源已售出 → 加「順手下架 Yahoo 商品」按鈕
            # v6.1.37:user 反映成交了也想順手下架(不限「貨源已售出」)→ 永遠顯示
            try:
                has_yids = any((p.get("yahoo_id") or "") for p in (conv.product_urls or []))
                if has_yids:
                    rows.append([
                        {"text": "📤 順手下架 Yahoo 商品", "callback_data": f"cs:tdown:{cid}"},
                    ])
            except Exception:
                pass
            return rows

        if phase == ConvPhase.PREVIEW_SELLER_QUESTION:
            # 要问卖家的问题待用户确认
            rows = [
                [{"text": "✅  发送给卖家  ✅", "callback_data": f"cs:ok:{cid}"}],
                [
                    {"text": "改问题", "callback_data": f"cs:edit:{cid}"},
                    {"text": "改回买家", "callback_data": f"cs:rpy:{cid}"},
                ],
            ]
            # v6.1.56:買家有發圖 → 加切換附圖按鈕
            _n_buyer_imgs = len(getattr(conv, "buyer_image_urls", None) or [])
            _n_attached = len(getattr(conv, "seller_question_images", None) or [])
            if _n_buyer_imgs > 0:
                if _n_attached > 0:
                    # 目前帶圖 → 提供「改純文字」 + 「重選圖」
                    rows.insert(1, [
                        {"text": f"📎 帶 {_n_attached}/{_n_buyer_imgs} 圖",
                         "callback_data": f"cs:imgcyc:{cid}"},  # cycle 切換
                        {"text": "📝 純文字",
                         "callback_data": f"cs:imgoff:{cid}"},
                    ])
                else:
                    # 目前不帶 → 提供「加全部圖」
                    rows.insert(1, [
                        {"text": f"📎 加全部 {_n_buyer_imgs} 張買家圖",
                         "callback_data": f"cs:imgall:{cid}"},
                    ])
            # v6.0.75:多商品場景 → 加切換目標商品按鈕(讓用戶選問哪個賣家)
            # 只列「非當前 primary 且有閒魚/煤炉貨源」的商品
            all_prods = getattr(conv, "all_products", []) or []
            if len(all_prods) > 1:
                current_yid = conv.product_urls[0].get("yahoo_id", "") if conv.product_urls else ""
                switch_btns = []
                for idx, prod in enumerate(all_prods):
                    if prod.get("yahoo_id") == current_yid:
                        continue
                    if prod.get("source") not in ("xianyu", "mercari"):
                        continue
                    if not prod.get("source_url"):
                        continue
                    short_yid = str(prod.get("yahoo_id", ""))[-6:]
                    switch_btns.append({
                        "text": f"改問{short_yid}",
                        "callback_data": f"cs:as{idx}:{cid}",
                    })
                    if len(switch_btns) >= 3:  # 每行最多 3 個按鈕
                        break
                if switch_btns:
                    rows.append(switch_btns)
            rows.append([
                {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
            ])
            return rows

        if phase == ConvPhase.PREVIEW_SELLER:
            # 卖家答完,AI 整合的回复待确认
            rows = [
                [{"text": "✅  确认发送  ✅", "callback_data": f"cs:ok:{cid}"}],
                [
                    {"text": "修改", "callback_data": f"cs:edit:{cid}"},
                    {"text": "AI重写", "callback_data": f"cs:rw:{cid}"},
                ],
                [
                    {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                    {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
                ],
            ]
            # v6.1.51:賣家又補發新訊息 → 加「🔄 更新整合」按鈕
            # 場景:議價時賣家可能半小時後又發訊息(同意/變價/補資訊),需要重新整合
            try:
                extra_n = len(getattr(conv, "seller_extra_msgs", []) or [])
                if extra_n > 0:
                    rows.insert(1, [
                        {"text": f"🔄 更新整合 (賣家又發 {extra_n} 條)",
                         "callback_data": f"cs:reint:{cid}"},
                    ])
            except Exception:
                pass
            # v6.1.37:賣家答完 → 用戶可能要決定下架(成交常見場景)
            try:
                has_yids = any((p.get("yahoo_id") or "") for p in (conv.product_urls or []))
                if has_yids:
                    rows.append([
                        {"text": "📤 順手下架 Yahoo 商品", "callback_data": f"cs:tdown:{cid}"},
                    ])
            except Exception:
                pass
            return rows

        if phase == ConvPhase.WAIT_SELLER:
            # 手动等卖家答案中 + 提供「轉自動問」button 重新走自動流程
            return [
                [
                    {"text": "🔄 轉自動問", "callback_data": f"cs:ask:{cid}"},
                    {"text": "自己回买家", "callback_data": f"cs:rpy:{cid}"},
                ],
                [
                    {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                    {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
                ],
            ]

        if phase == ConvPhase.AUTO_ASKING_SELLER:
            # 自动问卖家中
            return [
                [
                    {"text": "切手动", "callback_data": f"cs:mnl:{cid}"},
                    {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                    {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
                ],
            ]

        if phase == ConvPhase.ERROR:
            # 错误,提供重试/自己回/只消红点/跳过
            return [
                [{"text": "🔄  重试  🔄", "callback_data": f"cs:rty:{cid}"}],
                [
                    {"text": "自己回", "callback_data": f"cs:rpy:{cid}"},
                    {"text": "只消红点", "callback_data": f"cs:rd:{cid}"},
                    {"text": "跳过", "callback_data": f"cs:skp:{cid}"},
                ],
            ]

        return []

    def _send_phase_buttons(self, conv: ConversationState,
                            content: str = "", hint: str = "") -> Optional[int]:
        """v6.0.74 发送阶段操作面板 — 合并核心内容 + 按钮在同一条消息。

        Args:
            content: 核心内容(AI 草稿 / 要问卖家的问题 / 整合回复 / 错误信息 等)
                     用 ━━ 分隔条包裹,方便用户一眼定位
            hint: 额外提示(可选)

        以前主流程会发 5 条消息(主+预览+草稿+操作说明+按钮),现在合并成 2 条:
        - 消息 A(_process_new_conv 已发):完整商业信息(对话/货源/运费)+ "AI 分析中"
        - 消息 B(本方法发):阶段标签 + 核心内容 + 按钮
        """
        buttons = self._build_buttons_for_phase(conv)
        if not buttons:
            return None

        phase_emoji = {
            ConvPhase.PREVIEW_SENT: "📝",
            ConvPhase.PREVIEW_SELLER_QUESTION: "❓",
            ConvPhase.PREVIEW_SELLER: "📥",
            ConvPhase.WAIT_SELLER: "⏳",
            ConvPhase.AUTO_ASKING_SELLER: "🤖",
            ConvPhase.ERROR: "❌",
        }
        phase_label = {
            ConvPhase.PREVIEW_SENT: "AI 草稿待确认",
            ConvPhase.PREVIEW_SELLER_QUESTION: "问卖家的问题待确认",
            ConvPhase.PREVIEW_SELLER: "整合回复待确认",
            ConvPhase.WAIT_SELLER: "等待卖家答案",
            ConvPhase.AUTO_ASKING_SELLER: "自动问卖家中",
            ConvPhase.ERROR: "AI 出错",
        }
        # STICKER_READ 特例标题
        if conv.phase == ConvPhase.PREVIEW_SENT and conv.ai_action == "STICKER_READ":
            label = "无文字消息(贴图/广告)"
            emoji = "📢"
        else:
            emoji = phase_emoji.get(conv.phase, "•")
            label = phase_label.get(conv.phase, conv.phase.value)

        # v6.0.83:Markdown 粗體 + 強化分區 — 在 topic 內快速從滾動條辨識 AI 卡片
        parts = [
            f"━━━━━━━━━━━━━━━━━━━",
            f"{emoji} *{label}*",
            f"`[{conv.account_name} / {conv.buyer_label}]`",
            f"━━━━━━━━━━━━━━━━━━━",
        ]
        if content:
            parts.append(content)
            parts.append("━━━━━━━━━━━━━━━━━━━")
        if hint:
            parts.append(f"_{hint}_")

        prompt = "\n".join(parts)
        # v6.0.83:有對應 forum topic 就 push 到 topic 內(AI 草稿在 buyer topic 顯示)
        # 沒 topic 或 forum 沒啟用 → fallback 私聊
        msg_id = 0
        topic_id = self._find_topic_for_conv(conv) if self.forum_bridge else 0
        if topic_id:
            try:
                # v6.1:multi-tenant 用 conv 對應 group 的 chat_id
                conv_key = self.forum_bridge._conv_key(conv.profile_id, conv.chat_id)
                target_chat = self.forum_bridge._resolve_chat_id_for_conv(conv_key)
                result, err = self.forum_bridge.bot._post("sendMessage", {
                    "chat_id": target_chat,
                    "message_thread_id": topic_id,
                    "text": prompt[:4096],
                    "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": buttons},
                })
                if result and not err:
                    msg_id = int(result.get("message_id") or 0)
            except Exception as e:
                self.on_log(f"[TG-FORUM] _send_phase_buttons push topic 異常: {e}")
                msg_id = 0
        if msg_id:
            # 記下 phase_buttons 在 forum 的位置 + chat_id(multi-tenant 正確 edit)
            self._phase_button_target[conv.conv_id] = ("forum", msg_id, topic_id, target_chat)
        else:
            msg_id = self.tg.send_inline_keyboard(prompt, buttons)
            if msg_id:
                self._phase_button_target[conv.conv_id] = ("private", msg_id, 0, "")
        if msg_id:
            conv.tg_msg_ids.add(msg_id)
        return msg_id

    # ─── 持久化 _convs(v6.0.83)── 重啟不丟 PREVIEW phase ───

    def _conv_persist_path(self, conv_id: str):
        return Path(self._base_dir) / "runtime" / "conv" / f"{conv_id}.json"

    def _persist_conv(self, conv) -> None:
        """寫單一 conv 到 disk(atomic + throttle 5s 避免重複寫)。"""
        try:
            import os as _os
            cid = conv.conv_id
            if not cid:
                return
            # throttle:5 秒內已寫過就跳過
            now = time.time()
            last = getattr(self, "_last_persist_ts", {}).get(cid, 0)
            if now - last < 5:
                return
            if not hasattr(self, "_last_persist_ts"):
                self._last_persist_ts: Dict[str, float] = {}
            self._last_persist_ts[cid] = now

            d = {}
            for f in _CONV_PERSIST_FIELDS:
                v = getattr(conv, f, None)
                if hasattr(v, "value"):  # enum → str
                    v = v.value
                if v is None:
                    continue
                # v6.1.51:set 不能 JSON 序列化,轉 list(限制大小防膨脹)
                if isinstance(v, set):
                    v = list(v)[-50:]  # 只保留最近 50 個,跟 set 內 prune 邏輯一致
                d[f] = v
            path = self._conv_persist_path(cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            _os.replace(str(tmp), str(path))
        except Exception as e:
            self.on_log(f"[CONV-PERSIST] {conv.conv_id[:8] if conv else '?'} 寫入失敗(忽略): {e}")

    def _delete_persisted_conv(self, conv_id: str) -> None:
        """conv 結束(DONE/EXPIRED/ERROR)時刪除持久化檔案。"""
        try:
            path = self._conv_persist_path(conv_id)
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _load_persisted_convs(self) -> None:
        """啟動時 load — 還原 PREVIEW phase 中的 conv,讓 user 看到「待處理對話」沒丟。"""
        root = Path(self._base_dir) / "runtime" / "conv"
        if not root.exists():
            return
        loaded = 0
        dropped = 0
        for fp in root.glob("*.json"):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                # phase enum
                phase_str = d.pop("phase", None)
                phase = ConvPhase.PENDING_AI
                if phase_str:
                    try:
                        phase = ConvPhase(phase_str)
                    except Exception:
                        pass
                # 跳過已結束的 + 超過 24h 的
                if phase in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR):
                    fp.unlink()
                    dropped += 1
                    continue
                if time.time() - (d.get("updated_ts", 0) or 0) > 86400:
                    fp.unlink()
                    dropped += 1
                    continue
                # 構造 ConversationState — 必要欄位先 default
                conv = ConversationState(
                    conv_id=d.get("conv_id", ""),
                    profile_id=d.get("profile_id", ""),
                    account_name=d.get("account_name", ""),
                    chat_id=d.get("chat_id", ""),
                    chat_url=d.get("chat_url", ""),
                    buyer_label=d.get("buyer_label", ""),
                    buyer_text=d.get("buyer_text", ""),
                )
                # 其他可選欄位 setattr
                for f in _CONV_PERSIST_FIELDS:
                    if f in d:
                        try:
                            _val = d[f]
                            # v6.1.51:disk 上是 list,還原為 set(seller_processed_msg_ids 用)
                            if f == "seller_processed_msg_ids" and isinstance(_val, list):
                                _val = set(_val)
                            setattr(conv, f, _val)
                        except Exception:
                            pass
                conv.phase = phase
                with self._lock:
                    self._convs[conv.conv_id] = conv
                    key = f"{conv.profile_id}|{conv.chat_id}"
                    self._chat_map[key] = conv.conv_id
                loaded += 1

                # v6.1:AUTO_ASKING_SELLER phase 還原後要重啟賣家檢查 timer
                # 不然賣家回了沒人偵測,卡在「等待中」永遠
                # 但 conv 太老(>6h)時:鹹魚對話已被新對話擠出 session.sync 200 列表
                # → schedule timer 也是無限重試,直接 fallback 到手動
                if phase == ConvPhase.AUTO_ASKING_SELLER:
                    age_sec = time.time() - (d.get("updated_ts", 0) or 0)
                    # v6.1.45:detect & repair 內部矛盾狀態(關鍵修復)
                    # 場景 A:auto_ask_fallback=true + phase=AUTO_ASKING_SELLER → 上次 fallback 沒 persist 成功
                    #   → 直接修為 WAIT_SELLER + 推 status card
                    # 場景 B:seller_sent_question="" + seller_peer_user_id="" → WS send 從沒成功
                    #   → 用戶看到「正在向闲鱼卖家发送」但其實沒成功,重啟後應該重試,不是直接 fallback
                    _disk_fallback = bool(d.get("auto_ask_fallback", False))
                    _disk_sent_q = (d.get("seller_sent_question", "") or "").strip()
                    _disk_peer = (d.get("seller_peer_user_id", "") or "").strip()
                    if _disk_fallback:
                        # 矛盾狀態:fallback=true 但 phase=AUTO_ASKING_SELLER → 修為 WAIT_SELLER
                        # v6.1.45 真 root cause 修復:同時清 fallback flag,讓下次「轉自動問」
                        # 能正常觸發 WS thread(否則 L2329 早退 → WS 從沒跑)
                        self.on_log(
                            f"[CONV-RESTORE] 矛盾狀態修復 conv={conv.conv_id[:8]} "
                            f"buyer={conv.buyer_label}:disk 有 fallback=true 但 phase=AUTO_ASKING_SELLER,"
                            f"強制修為 WAIT_SELLER + 清 fallback flag"
                        )
                        try:
                            conv.phase = ConvPhase.WAIT_SELLER
                            conv.auto_ask_fallback = False  # ← 真 root cause 修復
                            # 強制 persist
                            if hasattr(self, "_last_persist_ts"):
                                self._last_persist_ts.pop(conv.conv_id, None)
                            self._persist_conv(conv)
                            # 推一張 status card 讓用戶看到「卡在等待」可手動處理
                            try:
                                self._send_phase_buttons(
                                    conv,
                                    content=(
                                        f"⚠️ 上次自動問賣家被中斷(WS 未成功)\n"
                                        f"請手動問賣家或點 [🔄 重試自動問]"
                                    ),
                                )
                            except Exception:
                                pass
                        except Exception as _e_repair:
                            self.on_log(f"[CONV-RESTORE] 矛盾狀態修復失敗: {_e_repair}")
                    elif age_sec > 6 * 3600:
                        self.on_log(
                            f"[CONV-RESTORE] AUTO_ASKING_SELLER 已 {int(age_sec/3600)}h 無更新,"
                            f"直接 fallback 手動 conv={conv.conv_id[:8]} buyer={conv.buyer_label}"
                        )
                        try:
                            self._fallback_to_manual(
                                conv,
                                f"重啟時對話已 {int(age_sec/3600)} 小時無更新,改手動處理"
                            )
                        except Exception:
                            pass
                    elif not _disk_sent_q and not _disk_peer:
                        # 場景 B:從沒成功送過 → 自動重試(不要直接 schedule check 又 fallback)
                        self.on_log(
                            f"[CONV-RESTORE] AUTO_ASKING_SELLER 但 seller_sent_question 跟 peer 都空 "
                            f"→ 上次 WS send 中斷,自動重試 conv={conv.conv_id[:8]} buyer={conv.buyer_label}"
                        )
                        try:
                            src = ""
                            if conv.product_urls:
                                src = conv.product_urls[0].get("source", "")
                            if src == "xianyu":
                                threading.Thread(
                                    target=self._auto_ask_xianyu_worker,
                                    args=(conv,),
                                    daemon=True,
                                ).start()
                            elif src == "mercari":
                                threading.Thread(
                                    target=self._auto_ask_mercari_worker,
                                    args=(conv,),
                                    daemon=True,
                                ).start()
                            else:
                                # 無 source 資訊,fallback 手動
                                self._fallback_to_manual(
                                    conv, "重啟還原時找不到貨源平台,改手動處理"
                                )
                        except Exception as _e_retry:
                            self.on_log(f"[CONV-RESTORE] 自動重試失敗: {_e_retry}")
                    else:
                        try:
                            src = ""
                            if conv.product_urls:
                                src = conv.product_urls[0].get("source", "")
                            if src == "xianyu":
                                # v6.1:重啟還原 + 同步 register WS dispatcher
                                # (WS 是賣家回訊息的主路徑,重啟後沒 register 就收不到)
                                try:
                                    if conv.seller_session_id:
                                        self._ensure_ws_client()
                                        self._register_ws_cid(conv)
                                        self.on_log(
                                            f"[CONV-RESTORE] WS dispatcher 重 register "
                                            f"cid={conv.seller_session_id}@goofish"
                                        )
                                except Exception as _e_ws:
                                    self.on_log(
                                        f"[CONV-RESTORE] WS register 失敗(忽略): {_e_ws}"
                                    )
                                self._schedule_xianyu_check(conv, delay=15)
                                self.on_log(
                                    f"[CONV-RESTORE] 重啟閒魚賣家檢查 conv={conv.conv_id[:8]} "
                                    f"buyer={conv.buyer_label}"
                                )
                            elif src == "mercari":
                                self._schedule_mercari_check(conv, delay=30)
                                self.on_log(
                                    f"[CONV-RESTORE] 重啟煤爐賣家檢查 conv={conv.conv_id[:8]} "
                                    f"buyer={conv.buyer_label}"
                                )
                        except Exception as _e_resume:
                            self.on_log(
                                f"[CONV-RESTORE] 重啟賣家檢查失敗 conv={conv.conv_id[:8]}: {_e_resume}"
                            )
            except Exception as e:
                self.on_log(f"[CONV-RESTORE] load {fp.name} 失敗: {e}")
        if loaded or dropped:
            self.on_log(f"[CONV-RESTORE] 還原 {loaded} 個 PREVIEW 對話 / 清理 {dropped} 個已結束")

        # v6.1.59:重啟後主動推 status card 給所有「等待賣家中」的 conv
        # 修「重啟後 user 不知道系統有沒有在追蹤,擔心要重複操作」UX 缺失
        # - 對所有 AUTO_ASKING_SELLER / WAIT_SELLER 推一張「⏳ 還在等 X 分鐘」card
        # - 帶按鈕(切手動 / 只消紅點 / 跳過 / 自己回买家),user 可以隨時介入
        # - 跳過矛盾狀態 path 已推過的(L3924)避免重複
        try:
            recap_n = 0
            with self._lock:
                _waiting_convs = [
                    c for c in list(self._convs.values())
                    if c.phase in (ConvPhase.AUTO_ASKING_SELLER, ConvPhase.WAIT_SELLER)
                ]
            for c in _waiting_convs:
                try:
                    _q_sent = (c.seller_sent_question or "").strip()
                    _q_draft = (c.auto_ask_question or "").strip()
                    _now = time.time()
                    _wait_min = max(0, int((_now - (c.updated_ts or _now)) // 60))
                    _phase_label = (
                        "閒魚賣家(系統自動問)"
                        if c.phase == ConvPhase.AUTO_ASKING_SELLER
                        else "賣家(手動模式)"
                    )

                    if _q_sent:
                        # 真的成功問過,在等回覆
                        _content = (
                            f"♻️ 軟件已重啟 — {_phase_label}還沒回覆,系統繼續追蹤中\n"
                            f"已問過:「{_q_sent[:100]}」\n"
                        )
                        if _wait_min > 0:
                            _content += f"已等待 {_wait_min} 分鐘\n"
                        _content += (
                            f"\n📡 WS 已重新註冊 + 15s 輪詢備援\n"
                            f"賣家回了會自動整合通知你 — 不用做什麼。"
                        )
                    else:
                        # 沒成功問過(矛盾狀態 path 應該已處理,這裡是後備)
                        _content = (
                            f"♻️ 軟件已重啟 — 此對話卡在「等賣家」但實際沒成功送出\n"
                        )
                        if _q_draft:
                            _content += f"AI 草稿(未發送):「{_q_draft[:100]}」\n"
                        _content += (
                            f"\n建議按下方:\n"
                            f"  · 「轉自動問」 重試自動發送\n"
                            f"  · 「自己回买家」 不等賣家直接回\n"
                            f"  · 「跳过」 結束等待"
                        )

                    self._send_phase_buttons(c, content=_content)
                    # 設 throttle 防止 buyer 又發訊息時 60s 內又推同樣的(在 _process_new_conv_inner 用)
                    if not hasattr(self, "_seller_status_throttle"):
                        self._seller_status_throttle: Dict[str, float] = {}
                    self._seller_status_throttle[c.conv_id] = _now
                    recap_n += 1
                except Exception as _e_each:
                    self.on_log(f"[CONV-RESTORE] recap 單條失敗 conv={c.conv_id[:8]}: {_e_each}")
            if recap_n > 0:
                self.on_log(f"[CONV-RESTORE] 重啟 recap 已推 {recap_n} 張等待中 status card")
        except Exception as _e_recap:
            self.on_log(f"[CONV-RESTORE] recap 異常(忽略): {_e_recap}")

    def _conv_aware_send(self, conv, text: str, *, parse_mode: str = "") -> int:
        """conv 對應的 buyer 有 forum topic → push topic;沒有 → fallback 私聊。

        v6.0.83 新增:讓 conv 相關訊息(錯誤提示、force_reply prompt 等)出現在
        該 buyer 的 topic 內,不再分散到私聊。
        v6.1:multi-tenant 用 store entry 內的 forum_chat_id;遞迴 fallback 改私聊 self.tg.send。
        """
        topic_id = self._find_topic_for_conv(conv) if (conv and self.forum_bridge) else 0
        if topic_id and conv:
            try:
                # 從 store 拿該 conv 對應的 group chat_id(multi-tenant 路由)
                conv_key = self.forum_bridge._conv_key(conv.profile_id, conv.chat_id)
                target_chat = self.forum_bridge._resolve_chat_id_for_conv(conv_key)
                payload = {
                    "chat_id": target_chat,
                    "message_thread_id": topic_id,
                    "text": text[:4096],
                }
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                r, e = self.forum_bridge.bot._post("sendMessage", payload)
                if r and not e:
                    mid = int(r.get("message_id") or 0)
                    if conv and mid:
                        conv.tg_msg_ids.add(mid)
                    return mid
            except Exception as ex:
                self.on_log(f"[TG] _conv_aware_send forum 失敗 fallback 私聊: {ex}")
        # fallback 私聊(非遞迴 — bug fix)
        try:
            return self.tg.send(text)
        except Exception:
            return 0

    def _find_topic_for_conv(self, conv) -> int:
        """從 conv 找對應 TG forum topic_id;沒建過就主動 ensure_topic 建一個。

        修 race condition:AI commander 跑得比 _forward_to_forum 快時,topic 還沒建,
        草稿落到私聊 fallback。改成主動 ensure_topic,保證 AI 草稿一定在 topic 內。
        """
        if not self.forum_bridge:
            return 0
        try:
            key = f"{conv.profile_id}|{conv.chat_id}"
            existing = int(self.forum_bridge.store.get_topic_id(key) or 0)
            if existing:
                return existing
            # 沒 topic 就主動建(會自動 backfill 歷史 + pin info card)
            topic_id, err = self.forum_bridge.ensure_topic(
                profile_id=conv.profile_id,
                yahoo_chat_id=conv.chat_id,
                buyer_label=conv.buyer_label or conv.chat_id,
                account_name=conv.account_name or conv.profile_id,
            )
            if err:
                self.on_log(f"[TG-FORUM] ensure_topic for conv 失敗: {err}")
                return 0
            return int(topic_id or 0)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _find_topic_for_conv 異常: {e}")
            return 0

    def _register_pending_input(
        self, prompt_msg_id: int, conv_id: str, action: str,
        topic_id: int = 0, group_chat_id: str = "",
    ) -> None:
        """注册一个 force_reply 提示消息,等用户回复后走对应 action。

        v6.1:加 topic_id + group_chat_id 參數 — forum mode 內 TG Desktop force_reply 不會
        自動觸發 reply UI,user 在 topic 內直接打字應被當「修改內容」,而不是發給買家。
        """
        with self._pending_inputs_lock:
            self._pending_inputs[prompt_msg_id] = (conv_id, action)
            # 防止积累:只保留最近 50 个 pending
            if len(self._pending_inputs) > 50:
                # 删除最早的 (按 msg_id 升序删头部)
                _to_drop = sorted(self._pending_inputs.keys())[: len(self._pending_inputs) - 50]
                for _k in _to_drop:
                    self._pending_inputs.pop(_k, None)
        # v6.0.78:同時記到 chat-level 備援(TG Desktop force_reply 失效時用)
        # v6.1:forum 場景另開 topic-level key,避免私聊 + forum 場景混雜
        try:
            now_ts = time.time()
            # 私聊 chat-level(舊邏輯保持)
            chat_id = (self.tg.chat_id or "").strip()
            if chat_id:
                with self._latest_pending_lock:
                    self._latest_pending_by_chat[chat_id] = (
                        conv_id, action, now_ts, prompt_msg_id,
                    )
            # v6.1:forum topic-level(新)
            if topic_id and group_chat_id:
                tkey = f"topic:{group_chat_id}:{topic_id}"
                with self._latest_pending_lock:
                    self._latest_pending_by_chat[tkey] = (
                        conv_id, action, now_ts, prompt_msg_id,
                    )
        except Exception:
            pass

    def _consume_pending_input(self, prompt_msg_id: int) -> Optional[tuple]:
        """从 pending 中取出并删除一个 (conv_id, action)。"""
        with self._pending_inputs_lock:
            return self._pending_inputs.pop(prompt_msg_id, None)

    def _consume_latest_pending(self, chat_id: str) -> Optional[tuple]:
        """v6.0.78:消耗 chat-level latest pending(force_reply 未觸發引用時備援)。

        過期(>5 分鐘)的不算。回 (conv_id, action) 或 None。
        ⭐ 多檢 conv state:若 conv 已 DONE/EXPIRED,直接清掉 latest_pending 並返 None
           (避免下條無引用訊息誤觸發 "對話已結束" popup)
        """
        if not chat_id:
            return None
        now = time.time()
        with self._latest_pending_lock:
            hit = self._latest_pending_by_chat.get(chat_id)
            if not hit:
                return None
            conv_id, action, ts, prompt_msg_id = hit
            if now - ts > self._latest_pending_ttl_sec:
                # 過期了,清掉
                self._latest_pending_by_chat.pop(chat_id, None)
                return None
            # ⭐ 檢查 conv state:已結束就不 fallback
            try:
                with self._lock:
                    _conv = self._convs.get(conv_id)
                if _conv and _conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
                    # conv 已結束 → latest_pending 是 stale,清掉並返 None(讓訊息走正常 dispatch)
                    self._latest_pending_by_chat.pop(chat_id, None)
                    with self._pending_inputs_lock:
                        self._pending_inputs.pop(prompt_msg_id, None)
                    return None
            except Exception:
                pass
            # 消耗
            self._latest_pending_by_chat.pop(chat_id, None)
        # 同步清掉 _pending_inputs 中的對應 prompt(避免日後再被消耗)
        with self._pending_inputs_lock:
            self._pending_inputs.pop(prompt_msg_id, None)
        return (conv_id, action)

    def _clear_buttons_with_status(self, conv: ConversationState, status: str) -> None:
        """点完按钮后,把操作面板消息的按钮区清掉。

        v6.0.83 修:用 _phase_button_target 精準找對應 bot/chat 編輯。
        message_id 是 chat-scope 的,ai_bot 私聊 #100 跟 forum #100 是不同訊息,
        不能用 max(tg_msg_ids) 一概而論。
        """
        target = self._phase_button_target.get(conv.conv_id)
        if not target:
            return
        try:
            # v6.1:支援新 4-tuple (kind, msg_id, topic_id, chat_id) 兼容舊 3-tuple
            if len(target) == 4:
                kind, msg_id, topic_id, chat_id = target
            else:
                kind, msg_id, topic_id = target
                chat_id = ""
        except Exception:
            return
        if kind == "forum" and self.forum_bridge:
            try:
                # 用 store 內 conv 對應的 group chat_id(multi-tenant 正確)
                if not chat_id:
                    conv_key = self.forum_bridge._conv_key(conv.profile_id, conv.chat_id)
                    chat_id = self.forum_bridge._resolve_chat_id_for_conv(conv_key)
                self.forum_bridge.bot._post("editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "reply_markup": {"inline_keyboard": []},
                })
            except Exception:
                pass
        elif kind == "private":
            try:
                self.tg.edit_message_reply_markup(msg_id, buttons=None)
            except Exception:
                pass

    # ---------- 翻译功能 ----------

    def _handle_cs_callback(self, data: str, chat_id: str, message_id: int) -> None:
        """v6.0.74 处理 AI 客服操作面板按钮点击。

        data 格式: cs:{action}:{conv_id}
        action: ok / edit / rw / rpy / ask / skp / rty / mnl
        """
        try:
            parts = data.split(":", 2)
            if len(parts) != 3:
                return
            _, action, cid = parts
        except Exception:
            return

        with self._lock:
            conv = self._convs.get(cid)
        if not conv:
            self.tg.send("⚠️ 该对话已不存在或已过期(可能 app 重启了)。")
            return

        # v6.1.19:tdown 是商品層動作(下架),跟對話狀態無關,即使 DONE/EXPIRED 也可執行
        if conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED) and action != "tdown":
            self._conv_aware_send(conv, f"⚠️ 该对话已结束(buyer={conv.buyer_label}),无法再操作。")
            return

        # v6.1.27:訓練數據紀錄 — 在 action 執行前 capture state(這就是「使用者看到草稿/狀態做的決定」)
        _tc_action_map = {
            "ok": "user:ok",
            "edit": "user:edit_start",
            "rw": "user:rewrite_start",
            "rpy": "user:reply_start",
            "ask": "user:ask",
            "skp": "user:skip",
            "rd": "user:read",
            "rty": "user:retry",
            "mnl": "user:manual",
            "tdown": "user:takedown",
            "reint": "user:reintegrate",  # v6.1.51:賣家又補發 → 重新整合
        }
        _tc_at = _tc_action_map.get(action)
        if _tc_at is None and action.startswith("as") and action[2:].isdigit():
            _tc_at = "user:switch_product"
        if _tc_at:
            _tc_record(
                _tc_at,
                conv=conv,
                input={"button": action, "callback_data": data},
                output=None,
                ai_draft=conv.ai_integrated_draft or conv.ai_draft or "",
                chosen_action=action,
                metadata={
                    "phase_before": str(conv.phase),
                    "ai_action": conv.ai_action,
                    "channel": "button",
                },
            )

        # ── 路由 action ──
        if action == "ok":
            self._cs_action_ok(conv, message_id)
        elif action == "edit":
            self._cs_action_force_reply(conv, "edit",
                                       "✏️ 请直接输入要发送的新内容(回复这条消息即可):")
        elif action == "rw":
            self._cs_action_force_reply(conv, "rewrite",
                                       "🔄 请输入修改指令(例:写得亲切点 / 加上免运提示 / 改成可议价):")
        elif action == "rpy":
            self._cs_action_force_reply(conv, "reply",
                                       "💬 请直接输入要回复给买家的内容(忽略 AI 草稿/卖家流程):")
        elif action == "ask":
            self._cs_action_ask(conv)
        elif action == "skp":
            self._cs_action_skip(conv)
        elif action == "rd":
            self._cs_action_read(conv)
        elif action == "rty":
            self._cs_action_retry(conv)
        elif action == "mnl":
            self._cs_action_manual(conv)
        elif action == "tdown":
            self._cs_action_takedown(conv)
        elif action == "reint":
            # v6.1.51:賣家補發了新訊息 → 重新整合所有內容
            self._cs_action_reintegrate(conv)
        elif action.startswith("as") and action[2:].isdigit():
            # v6.0.75:cs:as{idx} 切換到指定商品問賣家
            self._cs_action_switch_product(conv, int(action[2:]))
        elif action == "imgall":
            # v6.1.56:加全部買家圖
            self._cs_action_toggle_seller_images(conv, message_id, mode="all")
        elif action == "imgoff":
            # v6.1.56:純文字(不附任何圖)
            self._cs_action_toggle_seller_images(conv, message_id, mode="off")
        elif action == "imgcyc":
            # v6.1.56:cycle 切換(用於現在已帶圖 → 試其他組合)
            self._cs_action_toggle_seller_images(conv, message_id, mode="cycle")
        else:
            self.tg.send(f"⚠️ 未知操作: {action}")

    # ---- 按钮点击的具体动作(尽量复用旧文字命令逻辑) ----

    def _cs_action_ok(self, conv: ConversationState, btn_msg_id: int) -> None:
        """按钮 [确认发送] → 走旧 ok 逻辑。"""
        # 先清按钮,避免重复点
        try:
            self.tg.edit_message_reply_markup(btn_msg_id, buttons=None)
        except Exception:
            pass
        if conv.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
            self._handle_confirm_reply(conv, "ok")
        elif conv.phase == ConvPhase.PREVIEW_SELLER_QUESTION:
            self._handle_confirm_seller_question(conv, "ok")
        else:
            self._conv_aware_send(conv, f"⚠️ 当前阶段 {conv.phase.value} 不支持「确认发送」。")

    def _cs_action_force_reply(self, conv: ConversationState, action: str, prompt_text: str) -> None:
        """按钮 [修改/AI重写/自己回] → 发 force_reply 提示,登记 pending input。
        v6.0.74:prompt 中包含当前草稿,方便用户长按复制或基于现有内容改写。
        """
        # 阶段校验
        if action == "edit":
            if conv.phase not in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER,
                                  ConvPhase.PREVIEW_SELLER_QUESTION):
                self._conv_aware_send(conv, f"⚠️ 当前阶段 {conv.phase.value} 不支持「修改」。")
                return
        elif action == "rewrite":
            if conv.phase not in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
                self._conv_aware_send(conv, f"⚠️ 当前阶段 {conv.phase.value} 不支持「AI 重写」。")
                return
        elif action == "reply":
            # reply (自己回买家) 在多个阶段都可用
            if conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
                self._conv_aware_send(conv, f"⚠️ 该对话已结束。")
                return

        # 取当前内容供用户参考/复制(edit/rewrite 都需要,reply 不需要)
        _current = ""
        if action in ("edit", "rewrite"):
            if conv.phase == ConvPhase.PREVIEW_SELLER_QUESTION:
                _current = conv.auto_ask_question or ""
                _label = "当前 AI 生成的问题"
            else:
                _current = conv.ai_integrated_draft or conv.ai_draft or ""
                _label = "当前 AI 草稿"

        # 组装 prompt:身份 + 操作说明 + 当前内容(供长按复制)
        full_prompt = (
            f"{prompt_text}\n"
            f"[{conv.account_name} / 买家 {conv.buyer_label}]"
        )
        if _current:
            # 草稿超过 250 字截断,避免 prompt 太长把按钮挤走
            _show = _current if len(_current) <= 250 else _current[:250] + "...(共" + str(len(_current)) + "字)"
            full_prompt += (
                f"\n\n【{_label}(可長按複製):】\n"
                f"{_show}"
            )
        # v6.0.83:有 forum topic 就 push 到 topic(user 在 topic 內 reply 就會接到)
        prompt_id = 0
        topic_id = self._find_topic_for_conv(conv) if self.forum_bridge else 0
        prompt_group_chat = ""
        if topic_id:
            try:
                # v6.1:multi-tenant 用 conv 對應 group 的 chat_id
                conv_key = self.forum_bridge._conv_key(conv.profile_id, conv.chat_id)
                target_chat = self.forum_bridge._resolve_chat_id_for_conv(conv_key)
                # v6.1:forum 內 force_reply 在 desktop 不會自動觸發 reply UI
                # → 加一行提示「直接打字即可,不需引用」
                hint_prompt = full_prompt[:4096]
                if "回复这条消息即可" in hint_prompt or "回復這條消息即可" in hint_prompt:
                    # 把舊提示改成更直白的:「直接輸入即可」
                    hint_prompt = hint_prompt.replace(
                        "回复这条消息即可", "下一條訊息會自動當作修改內容"
                    ).replace(
                        "回復這條消息即可", "下一條訊息會自動當作修改內容"
                    )
                r, e = self.forum_bridge.bot._post("sendMessage", {
                    "chat_id": target_chat,
                    "message_thread_id": topic_id,
                    "text": hint_prompt,
                    "reply_markup": {"force_reply": True, "selective": True},
                })
                if r and not e:
                    prompt_id = int(r.get("message_id") or 0)
                    prompt_group_chat = str(target_chat)
            except Exception:
                prompt_id = 0
        if not prompt_id:
            prompt_id = self.tg.send_force_reply(full_prompt)
        if prompt_id:
            # v6.1:forum 場景把 topic_id + group_chat 一起記,讓 forum reply handler 能 fallback
            self._register_pending_input(
                prompt_id, conv.conv_id, action,
                topic_id=topic_id, group_chat_id=prompt_group_chat,
            )
            conv.tg_msg_ids.add(prompt_id)
        else:
            self._conv_aware_send(conv, "⚠️ 发送提示失败,请直接用文字命令(edit:xxx / mod:xxx / reply:xxx)。")

    def _cs_action_ask(self, conv: ConversationState) -> None:
        """按钮 [问卖家] — 支援 PREVIEW_SENT(轉自動) + WAIT_SELLER(手動轉自動)"""
        if conv.phase not in (ConvPhase.PREVIEW_SENT, ConvPhase.WAIT_SELLER):
            self._conv_aware_send(conv, f"⚠️ 当前阶段 {conv.phase.value} 不支持「问卖家」。")
            return
        self._escalate_to_seller(conv)

    def _cs_action_toggle_seller_images(self, conv: ConversationState, btn_msg_id: int,
                                          mode: str = "cycle") -> None:
        """v6.1.56:切換要附給賣家的買家圖。

        mode:
          - "all"   : 附全部買家圖
          - "off"   : 不附圖純文字
          - "cycle" : 在「全圖 → 純文字 → AI 選 → 全圖」之間循環

        會更新 conv.seller_question_images 然後重新渲染預覽訊息+按鈕。
        """
        if conv.phase != ConvPhase.PREVIEW_SELLER_QUESTION:
            self._conv_aware_send(conv, f"⚠️ 當前階段 {conv.phase.value} 不支持切換附圖")
            return

        all_imgs = list(getattr(conv, "buyer_image_urls", None) or [])
        if not all_imgs:
            self._conv_aware_send(conv, "⚠️ 買家沒發圖,無法切換附圖。")
            return

        current = list(getattr(conv, "seller_question_images", None) or [])

        if mode == "all":
            new_imgs = list(all_imgs[:3])  # cap 3
            mode_label = "全部圖"
        elif mode == "off":
            new_imgs = []
            mode_label = "純文字"
        elif mode == "cycle":
            # cycle: 全圖 → 純文字 → (AI 選/最新一張) → 全圖
            if len(current) >= min(3, len(all_imgs)):
                new_imgs = []
                mode_label = "純文字"
            elif len(current) == 0:
                new_imgs = [all_imgs[-1]]  # 只附最新一張
                mode_label = "只附最新 1 張"
            else:
                new_imgs = list(all_imgs[:3])
                mode_label = "全部圖"
        else:
            return

        conv.seller_question_images = new_imgs
        # mode != 'cycle' 的人工選擇覆蓋 AI 原始 reason
        if mode in ("all", "off"):
            conv.seller_question_images_reason = f"user 切換為:{mode_label}"

        # 訓練 hook
        _tc_record(
            "user:toggle_seller_images",
            conv=conv,
            input={"mode": mode, "before_count": len(current), "after_count": len(new_imgs)},
            output={"new_images": [u[:80] for u in new_imgs]},
            metadata={"mode_label": mode_label},
        )

        # 重新渲染預覽訊息
        reason = conv.ai_internal_note or "需要卖家确认"
        question = conv.auto_ask_question or ""
        _n_attach = len(new_imgs)
        _n_all = len(all_imgs)
        _content = (
            f"🏷 需要问卖家:{reason}\n\n"
            f"准备向闲鱼卖家提问:\n{question}\n\n"
        )
        if _n_attach > 0:
            _content += f"📎 附 {_n_attach}/{_n_all} 張買家圖一起送 ({mode_label})"
        else:
            _content += f"📝 純文字發送(買家有 {_n_all} 張圖,user 切為不附)"

        # 刪舊訊息,重發新預覽 + 按鈕
        try:
            self.tg.edit_message_reply_markup(btn_msg_id, buttons=None)
        except Exception:
            pass
        self._send_phase_buttons(conv, content=_content)

    def _cs_action_skip(self, conv: ConversationState) -> None:
        """按钮 [跳过] → 同舊文字命令 skip:EXPIRED 不处理,保留 Yahoo 红点。

        和「只消红点」语义相反:
        - 只消红点 = _mark_read_yahoo + DONE,Yahoo 红点消失,结案不回
        - 跳过    = EXPIRED,Yahoo 红点保留,下次监控刷新会再次进入对话
        """
        self._set_phase(conv.conv_id, ConvPhase.EXPIRED)
        self._conv_aware_send(conv, 
            f"⏭ 已跳过(保留红点,下次刷新会再看到)。\n"
            f"买家 {conv.buyer_label}"
        )

    def _cs_action_read(self, conv: ConversationState) -> None:
        """v6.0.75 新增:按钮 [只消红点] → 同舊文字命令 read:DONE + mark_read,
        Yahoo 那边红点消失,本条结案,但不发任何消息给买家。

        典型场景:买家发感谢/客套话,你看过了不想回但要消红点。
        """
        if conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
            return
        # 设 skip flag,避免 _set_phase 内自动发排队提醒(我们要合并到这条消息)
        conv._skip_remind_on_done = True
        self._set_phase(conv.conv_id, ConvPhase.DONE)
        # 合并:消红点完成提示 + 排队提醒
        _msg = f"📭 已消红点(不发消息) [{conv.account_name} / {conv.buyer_label}]"
        _merged = self._remind_pending_convs(prefix=_msg)
        self._conv_aware_send(conv, _merged if _merged else _msg)
        threading.Thread(
            target=self._mark_read_yahoo,
            args=(conv.profile_id, conv.chat_url, conv.account_name),
            kwargs={"shop_code": conv.shop_code, "chat_id": conv.chat_id},
            daemon=True,
        ).start()

    def _cs_action_retry(self, conv: ConversationState) -> None:
        """按钮 [重试] → 同 retry 文字命令。"""
        if conv.phase != ConvPhase.ERROR:
            self._conv_aware_send(conv, f"⚠️ 当前阶段 {conv.phase.value} 无需重试。")
            return
        self._conv_aware_send(conv, f"🔄 正在重新处理(买家 {conv.buyer_label})...")
        self._set_phase(conv.conv_id, ConvPhase.PENDING_AI)
        threading.Thread(
            target=self._process_new_conv_inner,
            args=(conv.conv_id,),
            daemon=True,
        ).start()

    def _cs_action_reintegrate(self, conv: ConversationState) -> None:
        """v6.1.51:按钮 [更新整合] → 重新整合「賣家原本答案 + 補發訊息」生成新草稿。

        場景:議價時賣家半小時後又發訊息(同意/改價/補資訊),用戶需要把新訊息納入整合。

        流程:
        1. 把 seller_answer + seller_extra_msgs 合併成新的「賣家答案」
        2. 重跑 _integrate_and_preview(會用 _fetch_seller_full_history 拉完整對話)
        3. clear seller_extra_msgs 避免下次重複(因為已合併)
        4. AI 生成新草稿 → 覆蓋預覽
        """
        if conv.phase != ConvPhase.PREVIEW_SELLER:
            self._conv_aware_send(conv, f"⚠️ 當前階段不支援「更新整合」(只在賣家答完待確認時可用)。")
            return
        extra = getattr(conv, "seller_extra_msgs", []) or []
        if not extra:
            self._conv_aware_send(conv, f"⚠️ 沒有新的賣家補發訊息可以整合。")
            return

        n_extra = len(extra)
        # 合併:原賣家答案 + 補發訊息
        combined_parts = []
        if conv.seller_answer:
            combined_parts.append(conv.seller_answer)
        combined_parts.extend(extra)
        combined = "\n".join(combined_parts)

        self.on_log(
            f"[TG] 用戶點 [更新整合] conv={conv.conv_id[:8]}: "
            f"合併 1 原答案 + {n_extra} 補發訊息"
        )
        self._conv_aware_send(conv,
            f"🔄 重新整合中(納入 {n_extra} 條補發訊息)...\n"
            f"買家 {conv.buyer_label}"
        )
        # clear extra(已併入 combined)
        conv.seller_extra_msgs = []
        # 也清 integrating flag(避免重整合時被自己擋下)
        conv.seller_integrating = False
        # 重跑整合
        try:
            self._integrate_and_preview(conv, combined)
        except Exception as e:
            self.on_log(f"[TG] reintegrate 異常 conv={conv.conv_id[:8]}: {e}")
            self._conv_aware_send(conv, f"⚠️ 重新整合失敗:{str(e)[:150]}")

    def _cs_action_manual(self, conv: ConversationState) -> None:
        """按钮 [切手动] → 同 manual 文字命令。"""
        if conv.phase != ConvPhase.AUTO_ASKING_SELLER:
            self._conv_aware_send(conv, f"⚠️ 当前阶段不支持「切手动」。")
            return
        if conv.seller_check_timer:
            try:
                conv.seller_check_timer.cancel()
            except Exception:
                pass
            conv.seller_check_timer = None
        conv.auto_ask_fallback = True
        self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
        self._conv_aware_send(conv, 
            f"🔄 已切换手动模式(买家 {conv.buyer_label})\n"
            f"请手动问卖家后,引用此消息回复卖家的答案。"
        )
        self._send_phase_buttons(conv)

    def _cs_action_takedown(self, conv: ConversationState) -> None:
        """v6.1.1:按鈕 [📤 順手下架 Yahoo 商品] → 純 HTTP 下架 conv 對應的所有 Yahoo 商品。

        - 從 conv.product_urls 拿 yahoo_id 列表
        - 用 merch_http_ops 的 batch_unshelve_items(純 HTTP,~1-3s)
        - 後台 thread 跑,避免 block AI 客服 polling
        - 完成後 send 訊息回報結果
        """
        item_ids = [
            (p.get("yahoo_id") or "").strip()
            for p in (conv.product_urls or [])
        ]
        item_ids = [x for x in item_ids if x]
        if not item_ids:
            self._conv_aware_send(conv, "⚠️ 沒有可下架的 Yahoo 商品編號")
            return

        profile_dir = Path(self._base_dir) / "profiles" / conv.profile_id
        if not profile_dir.exists():
            self._conv_aware_send(conv, f"⚠️ profile 不存在: {conv.profile_id}")
            return

        # 立刻回 ⏳ 訊息(背景 thread 跑下架)
        self._conv_aware_send(
            conv,
            f"⏳ 下架中... 商品編號: {', '.join(item_ids)}\n"
            f"帳號: {conv.account_name}",
        )

        import threading as _t
        def _bg():
            try:
                from .merch_http_ops import (
                    _try_cached_session, batch_unshelve_items, _parse_batch_result,
                )
                # v6.1.1:cache 沒 wssid/cookies → SQLite fallback(跟 publish 同邏輯)
                session = _try_cached_session(profile_dir, log=self.on_log)
                if session is None:
                    try:
                        from .cookie_store import (
                            load_from_chrome_sqlite_yahoo, save_cookie_cache,
                        )
                        flat, raw = load_from_chrome_sqlite_yahoo(profile_dir)
                        if flat and len(flat) >= 5:
                            save_cookie_cache(profile_dir, flat, "", raw_cookies=raw)
                            session = _try_cached_session(profile_dir, log=self.on_log)
                    except Exception:
                        pass
                if session is None or not session.is_valid:
                    self._conv_aware_send(
                        conv,
                        f"❌ 下架失敗:無法建 session(cookie cache 跟 SQLite 都讀不到)\n"
                        f"帳號 {conv.account_name} 可能需要重新登入"
                    )
                    # 訓練數據紀錄 — 下架失敗(session 建不起來)
                    _tc_record(
                        "send:takedown",
                        conv=conv,
                        input={"item_ids": item_ids},
                        output={"ok": False, "info": "session 建立失敗"},
                        metadata={"channel": "merch_http", "failed": True, "stage": "session"},
                    )
                    return

                resp = batch_unshelve_items(session, item_ids)
                succ, fail, failed_ids = _parse_batch_result(resp)

                lines = [
                    f"📤 <b>下架結果 — {conv.account_name}</b>",
                    f"  ✅ 成功 {succ} 件",
                ]
                if fail:
                    lines.append(f"  ❌ 失敗 {fail} 件:{', '.join(failed_ids)}")
                if succ > 0:
                    lines.append(f"<i>已下架的商品可在 Yahoo 後台「下架中」分類重新上架</i>")
                self._conv_aware_send(conv, "\n".join(lines))
                self.on_log(f"[TG-TDOWN] {conv.account_name} 下架: 成功={succ} 失敗={fail}")
                # ⭐ v6.1.27:訓練數據紀錄 — 下架結果(最終出口)
                # 訓練端能學「user 何時選擇下架」+「下架成功率」
                _tc_record(
                    "send:takedown",
                    conv=conv,
                    input={"item_ids": item_ids},
                    output={
                        "ok": fail == 0,
                        "succ_count": succ,
                        "fail_count": fail,
                        "failed_ids": failed_ids,
                    },
                    metadata={
                        "channel": "merch_http",
                        "total_items": len(item_ids),
                    },
                )
            except Exception as e:
                self.on_log(f"[TG-TDOWN] 異常: {e}")
                try:
                    self._conv_aware_send(conv, f"❌ 下架異常: {str(e)[:200]}")
                except Exception:
                    pass
                # 訓練數據紀錄 — 下架異常
                _tc_record(
                    "send:takedown",
                    conv=conv,
                    input={"item_ids": item_ids},
                    output={"ok": False, "info": str(e)[:200]},
                    metadata={"channel": "merch_http", "failed": True, "stage": "exception"},
                )

        _t.Thread(target=_bg, daemon=True, name=f"cs-tdown-{conv.conv_id[:6]}").start()

    def _cs_action_switch_product(self, conv: ConversationState, target_idx: int) -> None:
        """v6.0.75:cs:as{idx} 切換到 all_products[target_idx] 對應的商品問賣家。

        將 conv.product_urls[0] 換成目標商品,重新生成問題,再次預覽。
        """
        all_prods = getattr(conv, "all_products", []) or []
        if not (0 <= target_idx < len(all_prods)):
            self._conv_aware_send(conv, f"⚠️ 商品索引 {target_idx} 無效(共 {len(all_prods)} 個)")
            return
        target = all_prods[target_idx]
        src = target.get("source", "")
        if src not in ("xianyu", "mercari") or not target.get("source_url"):
            self._conv_aware_send(conv, f"⚠️ 商品 {target.get('yahoo_id', '')} 沒閒魚/煤炉貨源,無法自動問")
            return

        new_yid = target.get("yahoo_id", "")
        self.on_log(f"[TG] 切換問賣家目標: conv={conv.conv_id[:8]} 新 yahoo_id={new_yid}")

        # 把目標商品提到 product_urls[0],原 primary 退到後面
        new_primary = {
            "url": target.get("source_url", ""),
            "source": src,
            "yahoo_id": new_yid,
        }
        new_list = [new_primary]
        for p in conv.product_urls:
            if p.get("yahoo_id") != new_yid:
                new_list.append(p)
        conv.product_urls = new_list
        # 同步 product_* 字段(AI 生成問題會用到 product_text)
        conv.product_text = target.get("product_text", "")
        conv.product_can_buy = target.get("can_buy", "未知")
        conv.product_title = target.get("barcode", "")
        # 清除舊 seller 狀態(避免上一輪殘留干擾新賣家流程)
        conv.seller_chat_url = ""
        conv.seller_session_id = ""
        conv.seller_peer_user_id = ""
        conv.seller_baseline_version = 0
        conv.seller_baseline_ts = 0
        conv.seller_sent_question = ""
        conv.seller_check_count = 0
        conv.seller_processed_msg_ids = set()
        conv.seller_extra_msgs = []
        conv.seller_integrating = False
        conv.seller_ai_count = 0

        # 重新生成問題
        try:
            if src == "xianyu":
                question = self._generate_seller_question(conv)
                conv.auto_ask_question = question
                self._send_phase_buttons(
                    conv,
                    content=f"🔄 改為問商品 {new_yid} 的閒魚賣家:\n{question}",
                )
            elif src == "mercari":
                question, zh_hint = self._generate_seller_question_ja(conv)
                conv.auto_ask_question = question
                _content = f"🔄 改為問商品 {new_yid} 的煤炉賣家(日文):\n{question}"
                if zh_hint:
                    _content += f"\n\n(中文意思:{zh_hint})"
                self._send_phase_buttons(conv, content=_content)
        except Exception as e:
            self.on_log(f"[TG] 切換商品後生成問題失敗: {e}")
            self._conv_aware_send(conv, f"⚠️ 切換到商品 {new_yid} 但生成問題失敗: {str(e)[:120]}")

    def _handle_pending_input(self, cid: str, action: str, text: str) -> None:
        """v6.0.74 用户回复 force_reply 提示后的处理。

        action: edit / rewrite / reply
        把用户输入转换成等价的旧文字命令,复用现有逻辑。
        """
        with self._lock:
            conv = self._convs.get(cid)
        if not conv:
            self.tg.send("⚠️ 对话已不存在或已过期。")
            return
        if conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
            self.tg.send(f"⚠️ 该对话已结束(buyer={conv.buyer_label}),输入无效。")
            return

        # v6.1.27:訓練數據紀錄 — 這是「使用者實際寫出來的最終文字」, 最關鍵的 SFT 訓練信號
        _tc_action_to_label = {
            "edit": "user:edit_text",
            "rewrite": "user:rewrite_instruction",
            "reply": "user:reply_text",
        }
        _tc_label = _tc_action_to_label.get(action)
        if _tc_label:
            _tc_record(
                _tc_label,
                conv=conv,
                input={"force_reply_text": text, "action": action},
                output=text,
                ai_draft=conv.ai_integrated_draft or conv.ai_draft or "",
                chosen_action=action,
                metadata={
                    "phase_before": str(conv.phase),
                    "ai_action": conv.ai_action,
                    "channel": "force_reply",
                    "text_length": len(text or ""),
                },
            )

        if action == "edit":
            # 替换草稿/问题后发送
            if conv.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
                # PREVIEW_SENT / PREVIEW_SELLER:替换 AI 草稿
                self._handle_confirm_reply(conv, f"edit:{text}")
            elif conv.phase == ConvPhase.PREVIEW_SELLER_QUESTION:
                # 替换要问卖家的问题
                self._handle_confirm_seller_question(conv, f"edit:{text}")
            else:
                self.tg.send(f"⚠️ 当前阶段 {conv.phase.value} 不支持修改。")
            return

        if action == "rewrite":
            # AI 重写
            if conv.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
                self._handle_confirm_reply(conv, f"mod:{text}")
            else:
                self.tg.send(f"⚠️ 当前阶段 {conv.phase.value} 不支持 AI 重写。")
            return

        if action == "reply":
            # 直接回买家
            if conv.phase == ConvPhase.PREVIEW_SELLER_QUESTION:
                self._handle_confirm_seller_question(conv, f"reply:{text}")
            elif conv.phase in (ConvPhase.PREVIEW_SENT, ConvPhase.PREVIEW_SELLER):
                self._handle_confirm_reply(conv, f"reply:{text}")
            elif conv.phase == ConvPhase.WAIT_SELLER:
                # v6.0.74:跳过中间消息,让 _auto_send_to_yahoo 统一发完成通知
                conv.final_reply = text
                conv._skip_remind_on_done = True
                self._set_phase(conv.conv_id, ConvPhase.DONE)
                self._supervisor_send(
                    f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                    f"最终回复(直接回复):「{text}」"
                )
                threading.Thread(
                    target=self._auto_send_to_yahoo,
                    args=(conv,),
                    daemon=True,
                ).start()
            elif conv.phase == ConvPhase.ERROR:
                # v6.0.74:ERROR 阶段也跳过中间消息
                conv.final_reply = text
                conv._skip_remind_on_done = True
                self._set_phase(conv.conv_id, ConvPhase.DONE)
                threading.Thread(
                    target=self._auto_send_to_yahoo,
                    args=(conv,),
                    daemon=True,
                ).start()
            else:
                self.tg.send(f"⚠️ 当前阶段 {conv.phase.value} 不支持自己回。")
            return

        self.tg.send(f"⚠️ 未知 pending action: {action}")

    # ────── v6.0.75 WebSocket inbound dispatcher ──────

    def _ensure_ws_client(self):
        """確保 WS 連線已啟動且 callback 已註冊。線程安全,可重複呼叫。"""
        if self._ws_started:
            return True
        try:
            from core.goofish_ws_client import XianyuWsClient
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            ws = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=self.on_log)
            ws.set_callbacks(
                on_inbound_msg=self._on_ws_inbound_msg,
                on_read_receipt=self._on_ws_read_receipt,
            )
            ok = ws.start(wait_ready=False)  # 不等 ready,後台連
            self._ws_started = True
            self.on_log("[TG] WebSocket client 已啟動(後台連線中)")
            return ok
        except Exception as e:
            self.on_log(f"[TG] WS client 啟動異常: {e}")
            return False

    def _register_ws_cid(self, conv: ConversationState) -> None:
        """v6.0.75:把 conv 的 (cid, peer_uid) 註冊到 WS dispatcher。
        同 cid 多 conv 共存(Yahoo 同商品被多個買家問時走同一閒魚 sessionId)。
        """
        with self._ws_map_lock:
            if conv.seller_session_id:
                cid = f"{conv.seller_session_id}@goofish"
                lst = self._ws_cid_to_conv.setdefault(cid, [])
                if conv.conv_id not in lst:
                    lst.append(conv.conv_id)
            if conv.seller_peer_user_id:
                lst2 = self._ws_peer_to_conv.setdefault(conv.seller_peer_user_id, [])
                if conv.conv_id not in lst2:
                    lst2.append(conv.conv_id)

    def _find_active_convs_by_ws_msg(self, msg) -> List["ConversationState"]:
        """從 WS inbound msg 找對應 conv 列表(cid 優先,sender_uid 備援)。

        v6.1.65 Fix A:DONE 24h 內仍 reattach,EXPIRED 永遠不撈
        - active conv(非 DONE/EXPIRED)永遠撈
        - DONE conv 有 done_at_ts 且距今 <= 24h → 也撈進來(賣家補拍/補答)
        - DONE conv 沒 done_at_ts(舊資料/persist 還原) → 視為已關閉不撈(安全 default)
        - EXPIRED 永遠不撈(8h 超時 conv 真結束了)

        返回所有 candidate conv,caller(如 Fix B AI 判斷)決定怎處理多 conv 衝突。
        """
        candidate_ids: List[str] = []
        with self._ws_map_lock:
            if msg.cid and msg.cid in self._ws_cid_to_conv:
                candidate_ids = list(self._ws_cid_to_conv[msg.cid])
            elif msg.sender_uid and msg.sender_uid in self._ws_peer_to_conv:
                candidate_ids = list(self._ws_peer_to_conv[msg.sender_uid])
        if not candidate_ids:
            return []
        result = []
        now_ts = time.time()
        with self._lock:
            for cid in candidate_ids:
                c = self._convs.get(cid)
                if not c:
                    continue
                if c.phase == ConvPhase.EXPIRED:
                    continue  # 真結束,永遠不撈
                if c.phase == ConvPhase.DONE:
                    # v6.1.65:24h 內賣家補訊息可 reattach
                    done_ts = float(getattr(c, "done_at_ts", 0) or 0)
                    if done_ts <= 0:
                        continue  # 舊資料/persist 還原沒紀錄,安全 default 不撈
                    if (now_ts - done_ts) > REATTACH_WINDOW_SEC:
                        continue  # 超過 24h
                    # 24h 內 → 列入 reattach 候選
                result.append(c)
        return result

    def _on_ws_inbound_msg(self, msg) -> None:
        """v6.0.75:WS 收到對方訊息 → 廣播給所有 active conv 觸發 AI 整合。

        並發保護:
        - 多 conv 共用 cid:同閒魚商品被多個 Yahoo 買家問 → 廣播給每一個 conv
        - 訊息級 dedupe:用 message_id 防 WS/HTTP 雙路雙重整合
        - 整合中標記:同 conv 內部 race 也被擋下
        """
        # v6.0.75:objectType=40006 session event → 觸發 HTTP fetch 拉正文
        if getattr(msg, 'is_session_event', False):
            self._on_ws_session_event(msg)
            return

        # 篩掉自己發的訊息
        try:
            from core.xianyu_im_http import get_my_user_id
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            my_uid = get_my_user_id(PURCHASE_PROFILE_DIR)
        except Exception:
            my_uid = ""
        if msg.sender_uid and my_uid and msg.sender_uid == my_uid:
            return  # 自己發的,跳過

        # v6.0.79:LWP 路徑 sender_uid 可能解析不到 → my_uid 過濾失效
        # 額外用「broadcast 前比對所有 active conv 的 seller_sent_question」攔截 echo
        # 任一 conv 的 sent_question 跟訊息內容相同 → 是自己發送的 echo
        msg_text = (msg.content_text or "").strip()
        if msg_text:
            try:
                with self._lock:
                    for c in self._convs.values():
                        sent_q = (getattr(c, "seller_sent_question", "") or "").strip()
                        if sent_q and sent_q == msg_text:
                            self.on_log(
                                f"[TG] WS 跳過自己提問的 echo: text={msg_text[:40]!r} "
                                f"(匹配 conv={c.conv_id[:8]} sent_question)"
                            )
                            return
            except Exception:
                pass

        # 平台/系統消息(sender_type != "0")— 直接跳過(在路由前判,所有 conv 都不需要)
        if msg.sender_type and msg.sender_type != "0":
            self.on_log(f"[TG] WS 跳過系統/活動消息: type={msg.sender_type} text={msg.content_text[:40]!r}")
            return

        # v6.0.77:閒魚官方系統提示卡片(驗貨寶/先驗後買/安全提示等)— 直接跳過
        # 平台會偽裝 senderUserType="0"(看起來像賣家),光靠 sender_type 過濾不掉,
        # parse_user_message_model 內透過 OFFICIAL_TIP_KEYS 識別並標記 is_official_tip
        if getattr(msg, "is_official_tip", False):
            self.on_log(
                f"[TG] WS 跳過閒魚官方提示卡片: "
                f"text={msg.content_text[:40]!r} raw={msg.raw_text[:80]!r}"
            )
            return

        # v6.0.80:語音訊息 → 客戶端 STT 轉文字(閒魚 server 不提供 STT)
        # v6.0.81:視頻訊息 → AI 多模態描述(GPT vision keyframes 看視頻)
        # v6.0.81 修復:媒體解析阻塞 5-30s,以前 sync call 卡 WS asyncio loop(心跳 15s 跳 → 斷連)
        # 改成 thread 異步:媒體消息 submit 到 executor,WS callback 立即返回
        # 整段對話若沒有語音/視頻 → 完全不觸發 AI 多模態(只在 startswith 匹配時才執行)
        # v6.0.83:閒魚賣家發圖 → 自動 stash URL 待中轉到 Yahoo 買家(settings.auto_forward_seller_media)
        _ct = msg.content_text or ""

        # stash 媒體 URL 供後續 forward 用(在改寫前)
        try:
            import re as _re_stash
            if _ct.startswith("[圖片]") and "http" in _ct:
                m = _re_stash.search(r'(https?://[^\s]+)', _ct)
                if m:
                    msg.media_forward_url = m.group(1)
                    msg.media_forward_kind = "image"
            elif _ct.startswith("[視頻]") and "http" in _ct:
                m = _re_stash.search(r'(https?://[^\s]+)', _ct)
                if m:
                    msg.media_forward_url = m.group(1)
                    msg.media_forward_kind = "video"
        except Exception:
            pass

        has_media = (_ct.startswith("[語音") or _ct.startswith("[視頻]")) and "http" in _ct

        if has_media:
            try:
                self._media_inbound_executor.submit(self._process_media_inbound_async, msg)
            except Exception as _e_sub:
                # executor 異常時降級同步處理(會卡 loop 但不會丟訊息)
                self.on_log(f"[TG] 媒體 executor submit 失敗,降級同步: {_e_sub}")
                self._process_media_inbound_async(msg)
            return  # 不阻塞 WS asyncio loop

        # 非媒體 → sync 廣播(快,< 100ms,不會卡 loop)
        self._broadcast_inbound_to_convs(msg)

    def _process_media_inbound_async(self, msg) -> None:
        """worker thread:媒體解析 → 改寫 content_text → broadcast。

        - 失敗時改寫成明確 hint 而非保留裸 URL,讓 AI 知道「解析失敗」可主動建議文字補充
        """
        try:
            import re as _re
            _ct = msg.content_text or ""

            if _ct.startswith("[語音") and "http" in _ct:
                url_m = _re.search(r'(https?://[^\s]+)', _ct)
                if url_m:
                    voice_url = url_m.group(1)
                    self.on_log(f"[TG] WS 收到語音訊息,觸發 STT (async)...")
                    try:
                        from core.xianyu_voice_stt import voice_to_text
                        stt_text = voice_to_text(voice_url, on_log=self.on_log)
                    except Exception as _e_stt:
                        self.on_log(f"[TG] STT 異常: {_e_stt}")
                        stt_text = ""
                    if stt_text:
                        msg.content_text = f"[語音→文字] {stt_text}"
                        self.on_log(f"[TG] ✓ STT 完成,AI 將讀到:{stt_text[:60]!r}")
                    else:
                        # 失敗 → 明確 hint 不保留裸 URL
                        msg.content_text = "[語音 無法轉寫,聽不到內容]"
                        self.on_log(f"[TG] STT 失敗,改寫為明確 hint")

            elif _ct.startswith("[視頻]") and "http" in _ct:
                # 視頻消息格式: "[視頻] URL 封面:封面URL" — 取第一個 URL 即視頻
                url_m = _re.search(r'(https?://[^\s]+)', _ct)
                if url_m:
                    video_url = url_m.group(1)
                    self.on_log(f"[TG] WS 收到視頻訊息,觸發 GPT vision (async)...")
                    try:
                        from core.xianyu_voice_stt import video_to_text
                        vdesc = video_to_text(video_url, on_log=self.on_log)
                    except Exception as _e_video:
                        self.on_log(f"[TG] 視頻解析異常: {_e_video}")
                        vdesc = ""
                    if vdesc:
                        msg.content_text = f"[視頻→描述] {vdesc}"
                        self.on_log(f"[TG] ✓ 視頻描述完成,AI 將讀到:{vdesc[:60]!r}")
                    else:
                        msg.content_text = "[視頻 無法解析,看不到內容]"
                        self.on_log(f"[TG] 視頻解析失敗,改寫為明確 hint")
        except Exception as e:
            self.on_log(f"[TG] _process_media_inbound_async 異常: {e}")

        # 解析完後 broadcast(媒體解析期間其他即時訊息已由 WS loop 正常處理)
        self._broadcast_inbound_to_convs(msg)

    def _broadcast_inbound_to_convs(self, msg) -> None:
        """廣播 inbound 訊息給所有 active conv → dispatch。

        v6.1.65 Fix A:active_convs 可能包含「DONE 24h 內 reattach 候選」
        對於 DONE 候選 → 把 phase 拉回 WAIT_SELLER 走正常 integration 流程
        """
        active_convs = self._find_active_convs_by_ws_msg(msg)
        if not active_convs:
            self.on_log(f"[TG] WS inbound 沒對應 active conv: peer={msg.sender_uid} cid={msg.cid} text={msg.content_text[:40]!r}")
            return

        # v6.1.65 Fix A:DONE 候選 → reattach 變回 WAIT_SELLER
        # 場景:賣家在 DONE 後 24h 內補拍照/補答案,點亮 conv 走正常 integration
        # 注意:phase 改在 dispatch 前,讓 _dispatch_seller_msg_to_conv 看到的是 WAIT_SELLER
        for conv in active_convs:
            if conv.phase == ConvPhase.DONE:
                done_ts = float(getattr(conv, "done_at_ts", 0) or 0)
                if done_ts > 0 and (time.time() - done_ts) <= REATTACH_WINDOW_SEC:
                    self.on_log(
                        f"[TG] reattach DONE conv conv={conv.conv_id[:8]} "
                        f"距 DONE {int(time.time() - done_ts)}s,phase 拉回 WAIT_SELLER"
                    )
                    # 不用 _set_phase(會清 timer),直接改 phase + 更新 ts
                    with self._lock:
                        conv.phase = ConvPhase.WAIT_SELLER
                        conv.updated_ts = time.time()
                        # done_at_ts 保留(讓二次 reattach 仍能算 24h)
                    # 訓練紀錄
                    try:
                        _tc_record(
                            "phase:transition",
                            conv=conv,
                            input={"from": "DONE", "to": "WAIT_SELLER"},
                            output="WAIT_SELLER",
                            metadata={"reattach": True, "trigger": "seller_late_msg",
                                      "msg_kind": getattr(msg, "media_forward_kind", "text") if getattr(msg, "media_forward_url", None) else "text"},
                        )
                    except Exception:
                        pass

        for conv in active_convs:
            try:
                self._dispatch_seller_msg_to_conv(conv, msg)
            except Exception as e:
                self.on_log(f"[TG] WS inbound dispatch 異常 conv={conv.conv_id[:8]}: {e}")

        # v6.0.83:閒魚賣家發圖 → 自動中轉到對應的 Yahoo 買家(純 HTTP 走 yahoo_im_media)
        # v6.1.65 Fix B:加 AI 判斷模式(seller_media_forward_mode = off / always / ai)
        if getattr(msg, "media_forward_url", None):
            try:
                self._maybe_forward_seller_media_to_yahoo(msg, active_convs)
            except Exception as e:
                self.on_log(f"[TG] _maybe_forward 異常: {e}")

    def _maybe_forward_seller_media_to_yahoo(self, msg, active_convs) -> None:
        """v6.0.83 → v6.1.65 Fix B:閒魚賣家發的圖/視頻中轉到 Yahoo 買家(三模式)。

        三模式 (settings.seller_media_forward_mode):
        - "off"   : 不轉,完全關閉(舊 auto_forward_seller_media=false 等價)
        - "always": 全部轉,無 AI 判斷(舊 auto_forward_seller_media=true 等價)
        - "ai" (預設) : per-conv 跑 AI 判斷,相關才轉,fail default 不轉

        向後兼容:settings 無新欄位 → 讀舊 auto_forward_seller_media:
            True → "always"
            False/缺 → "ai"(智能 default,但 AI fail 不轉等價於關閉)
        """
        url = getattr(msg, "media_forward_url", "")
        kind = getattr(msg, "media_forward_kind", "image")
        if not url:
            return
        if kind not in ("image", "video"):
            self.on_log(f"[TG] 媒體中轉:kind={kind} 未支援 url={url[:60]}")
            return

        # ===== 解析模式 =====
        try:
            from core.accounts import load_settings
            st = load_settings() or {}
        except Exception:
            return
        mode = (st.get("seller_media_forward_mode") or "").strip().lower()
        if not mode:
            # 向後兼容:讀舊 bool 欄位
            legacy = bool(st.get("auto_forward_seller_media", False))
            mode = "always" if legacy else "ai"

        if mode == "off":
            return

        # ===== 過濾可用 conv(去重 + 必要欄位齊全) =====
        candidates: List[ConversationState] = []
        mid = getattr(msg, "message_id", "") or url
        for conv in active_convs:
            if not (conv.shop_code and conv.chat_id and conv.profile_id):
                continue
            if conv.phase in (ConvPhase.EXPIRED, ConvPhase.ERROR):
                continue
            if not hasattr(conv, "_forwarded_media_ids"):
                conv._forwarded_media_ids = set()
            if mid in conv._forwarded_media_ids:
                continue
            conv._forwarded_media_ids.add(mid)
            candidates.append(conv)

        if not candidates:
            return

        # ===== always 模式:直接轉 =====
        if mode == "always":
            for conv in candidates:
                threading.Thread(
                    target=self._do_forward_media_to_yahoo,
                    args=(conv, url, kind),
                    daemon=True,
                ).start()
            self.on_log(
                f"[TG] 媒體中轉(always):{len(candidates)} 個 conv {kind} url={url[:60]}"
            )
            return

        # ===== ai 模式:per-conv 跑 AI 判斷 =====
        for conv in candidates:
            threading.Thread(
                target=self._ai_judge_then_forward_media,
                args=(conv, url, kind),
                daemon=True,
            ).start()
        self.on_log(
            f"[TG] 媒體中轉(ai):{len(candidates)} 個 conv 啟動 AI 判斷 {kind} url={url[:60]}"
        )

    def _ai_judge_then_forward_media(
        self, conv: "ConversationState", media_url: str, media_kind: str,
    ) -> None:
        """v6.1.65 Fix B:worker thread:AI 判斷 → 相關才轉 → TG 通知用戶決策結果。"""
        try:
            decision = self._ai_judge_seller_media_relevance(conv, media_url, media_kind)
        except Exception as e:
            self.on_log(f"[TG] AI 判斷異常 conv={conv.conv_id[:8]}: {e},default 不轉")
            decision = {"ok": False, "forward": False, "reason": f"AI 判斷異常: {e}"}

        ai_ok = decision.get("ok", False)
        should_forward = decision.get("forward", False)
        ai_reason = decision.get("reason", "")

        if not ai_ok:
            # AI fail → default 不轉 + TG 告知用戶可手動處理
            self._conv_aware_send(conv,
                f"⚠️ AI 媒體判斷失敗 [{conv.account_name} / {conv.buyer_label}]\n\n"
                f"賣家剛發 {media_kind}:{media_url[:80]}\n"
                f"原因:{ai_reason[:150]}\n\n"
                f"💡 如需轉發請手動處理 (打字回覆或引用回覆)"
            )
            return

        if not should_forward:
            # AI 判斷不相關 → 通知用戶 + log(讓用戶知道有圖被攔下)
            self._conv_aware_send(conv,
                f"📭 AI 判斷此媒體不轉發 [{conv.account_name} / {conv.buyer_label}]\n\n"
                f"來源:閒魚賣家發的 {media_kind}\n"
                f"理由:{ai_reason[:200]}\n\n"
                f"💡 若要強制轉發請打字 (例:reply: 把圖轉發給買家)"
            )
            return

        # AI 判斷相關 → 走實際 forward
        self.on_log(f"[TG] AI 判斷相關 → forward conv={conv.conv_id[:8]} kind={media_kind}")
        self._do_forward_media_to_yahoo(conv, media_url, media_kind, ai_reason=ai_reason)

    def _ai_judge_seller_media_relevance(
        self, conv: "ConversationState", media_url: str, media_kind: str,
    ) -> Dict[str, Any]:
        """v6.1.65 Fix B:AI 判斷賣家剛發的媒體是否該轉給該 conv 的 Yahoo 買家。

        返回 {"ok": bool, "forward": bool, "reason": str}
        - ok=False:AI 呼叫本身失敗(網路/API 掛)→ caller default 不轉
        - ok=True, forward=True:相關,該轉
        - ok=True, forward=False:不相關,不轉
        """
        # 構造上下文(token 預算控制:截斷對話歷史)
        buyer_text_recent = (conv.buyer_text or "")[-500:]
        seller_dialog = (conv.seller_answer or "")[-800:]
        product_title = (conv.product_title or "")[:100]
        product_text = (conv.product_text or "")[:400]

        # 視頻處理:用首幀(若 conversation_media 有 cached 的首幀 b64 data URL 更好)
        # 簡化版:直接把 video URL 傳給 vision API(部分 vision 模型支援 mp4 frame extract)
        # 若不支援,AI 仍可從 URL/上下文判斷
        media_for_vision = [media_url] if media_kind == "image" else []
        # 視頻 fallback:嘗試從 conversation_media 找首幀
        if media_kind == "video":
            for m in (conv.conversation_media or []):
                if m.get("url") == media_url and m.get("first_frame_b64"):
                    media_for_vision = [m["first_frame_b64"]]
                    break

        system_prompt = (
            "你是 Yahoo 拍賣賣家代理 AI。你的真實貨源是中國閒魚(對 Yahoo 買家保密)。\n"
            "閒魚賣家剛發了一張圖/視頻給你。請判斷:這個媒體是否該轉發給 Yahoo 買家?\n\n"
            "【判斷規則】\n"
            "✅ 應該轉發(forward=true)的情境:\n"
            "  - 買家明確要求看更多圖/實拍/細節/瑕疵照片,賣家剛發來補拍\n"
            "  - 買家詢問商品某個特徵(例:18K 刻印、尺寸、瑕疵位置),媒體正是該特徵的展示\n"
            "  - 賣家回應買家具體問題後附上實物圖佐證\n\n"
            "❌ 不應該轉發(forward=false)的情境:\n"
            "  - 媒體跟買家當前對話無關(例:賣家在跟其他人聊,順手發圖)\n"
            "  - 媒體是廣告/不相關促銷圖\n"
            "  - 買家根本沒提及視覺需求,媒體屬於賣家單方面分享\n"
            "  - 媒體可能洩漏貨源平台/賣家身份(例:閒魚 logo、賣家頭像、聊天截圖)\n\n"
            "輸出格式(嚴格 JSON,無多餘文字):\n"
            '{"forward": true 或 false, "reason": "30 字內中文理由"}'
        )

        user_prompt = (
            f"【商品】{product_title}\n"
            f"{product_text}\n\n"
            f"【買家最近對話(節錄)】\n{buyer_text_recent}\n\n"
            f"【賣家對話歷史(節錄)】\n{seller_dialog}\n\n"
            f"【賣家剛發的媒體】類型={media_kind}, URL={media_url}\n\n"
            "請判斷該媒體是否相關該轉發,輸出嚴格 JSON。"
        )

        try:
            ok, result = call_openai(
                api_key=self.ai.get("api_key", ""),
                base_url=self.ai.get("base_url", ""),
                endpoint_mode=self.ai.get("endpoint_mode", "chat"),
                model=self.ai.get("model", ""),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_urls=media_for_vision,
                timeout_sec=30,
            )
        except Exception as e:
            return {"ok": False, "forward": False, "reason": f"AI 呼叫異常: {e}"}

        if not ok:
            return {"ok": False, "forward": False, "reason": f"AI API fail: {str(result)[:150]}"}

        # 解析 JSON 輸出
        try:
            import json as _json
            import re as _re_j
            # AI 可能用 ```json 包,先抽出來
            txt = str(result).strip()
            m = _re_j.search(r'```(?:json)?\s*(\{.*?\})\s*```', txt, _re_j.DOTALL)
            if m:
                txt = m.group(1)
            else:
                # 直接抽第一個 {...}
                m2 = _re_j.search(r'\{[^{}]*"forward"[^{}]*\}', txt, _re_j.DOTALL)
                if m2:
                    txt = m2.group(0)
            parsed = _json.loads(txt)
            # v6.1.65 bug fix:防 AI 返回 "false" 字串被 bool() 誤判成 True
            fv = parsed.get("forward", False)
            if isinstance(fv, str):
                forward = fv.strip().lower() in ("true", "yes", "1", "是")
            elif isinstance(fv, (int, float)):
                forward = bool(fv)
            else:
                forward = bool(fv)
            reason = str(parsed.get("reason", "")).strip()
            return {"ok": True, "forward": forward, "reason": reason or "(無理由)"}
        except Exception as e:
            self.on_log(f"[TG] AI 媒體判斷 JSON 解析失敗 conv={conv.conv_id[:8]}: {e}, raw={str(result)[:200]!r}")
            return {"ok": False, "forward": False, "reason": f"JSON 解析失敗: {e}"}

    def _do_forward_media_to_yahoo(
        self, conv: "ConversationState", media_url: str,
        media_kind: str = "image", ai_reason: str = "",
    ) -> None:
        """v6.1.65 Fix C:worker thread:下載閒魚源媒體 → Yahoo Pixelframe 4-step 上傳。

        - image: send_image_from_url(既有)
        - video: send_video_from_url_autosplit(v6.1.53,>30s 自動切分)
        - video fallback:autosplit fail → video_to_text 取 GPT vision 描述 → 發文字告訴買家
        """
        # 兼容舊呼叫(沒傳 media_kind 預設 image)
        kind = media_kind or "image"
        try:
            from pathlib import Path
            profile_dir = Path(self._base_dir) / "profiles" / conv.profile_id
            if not profile_dir.exists():
                self.on_log(f"[TG] forward 失敗:profile_dir 不存在 {profile_dir}")
                _tc_record(
                    "send:media_to_buyer",
                    conv=conv,
                    input={"source": "xianyu_seller", "media_url": media_url[:200], "kind": kind},
                    output={"ok": False, "error": "profile_dir_missing"},
                    metadata={"channel": "pixelframe_forward", "failed": True},
                )
                return

            ok, info = False, ""
            if kind == "image":
                from core.yahoo_im_media import send_image_from_url
                ok, info = send_image_from_url(
                    profile_dir,
                    shop_id=conv.shop_code,
                    buyer_id=conv.chat_id,
                    image_url=media_url,
                    on_log=self.on_log,
                )
            elif kind == "video":
                # v6.1.65 Fix C:用 v6.1.53 autosplit,>30s 自動切分多段送
                try:
                    from core.yahoo_im_media import send_video_from_url_autosplit
                    ok, info = send_video_from_url_autosplit(
                        profile_dir,
                        shop_id=conv.shop_code,
                        buyer_id=conv.chat_id,
                        video_url=media_url,
                        on_log=self.on_log,
                    )
                except Exception as e:
                    ok, info = False, f"視頻 send 異常: {e}"

                # 視頻 fallback:autosplit fail → 用 GPT vision 描述 → 發文字
                if not ok:
                    self.on_log(f"[TG] 視頻 forward 失敗,嘗試 vision 描述 fallback: {info[:120]}")
                    try:
                        from core.xianyu_voice_stt import video_to_text
                        vdesc = video_to_text(media_url, on_log=self.on_log)
                    except Exception as _e_v:
                        vdesc = ""
                        self.on_log(f"[TG] vision 描述 fallback 異常: {_e_v}")
                    if vdesc:
                        # 發文字告訴買家賣家發了視頻的內容(不洩漏貨源)
                        try:
                            from core.im_http_ops import im_send_message, build_channel_id
                            channel = build_channel_id(conv.shop_code, conv.chat_id)
                            fallback_text = (
                                f"剛收到的補充說明:\n{vdesc[:300]}\n"
                                "(原視頻長度超過上限,以文字描述代替)"
                            )
                            ok_txt, info_txt = im_send_message(
                                profile_dir,
                                channel_id=channel,
                                receiver=conv.chat_id,
                                message=fallback_text,
                                on_log=self.on_log,
                            )
                            if ok_txt:
                                ok, info = True, f"視頻 fallback 文字描述送達: {info_txt[:150]}"
                                self.on_log(f"[TG] 視頻 vision fallback 文字發送成功 conv={conv.conv_id[:8]}")
                        except Exception as _e_t:
                            self.on_log(f"[TG] 視頻 vision fallback 發文字異常: {_e_t}")
            else:
                ok, info = False, f"未支援的 media kind: {kind}"

            # 訓練 hook
            _tc_record(
                "send:media_to_buyer",
                conv=conv,
                input={"source": "xianyu_seller", "media_url": media_url[:200], "kind": kind},
                output={"ok": bool(ok), "info": str(info)[:200]},
                metadata={
                    "channel": "pixelframe_forward",
                    "auto_triggered": True,
                    "ai_reason": ai_reason[:200],
                    "buyer_text_at_trigger": (conv.buyer_text or "")[-300:],
                },
            )

            icon = "📷" if kind == "image" else "🎬"
            if ok:
                msg_text = (
                    f"{icon} 已自動轉發{('實物圖' if kind=='image' else '視頻')}給買家 "
                    f"[{conv.account_name} / {conv.buyer_label}]\n\n"
                    f"來源:閒魚賣家剛發的{kind}\n"
                )
                if ai_reason:
                    msg_text += f"AI 判斷理由:{ai_reason[:150]}\n"
                msg_text += f"{str(info)[:200]}"
                self._conv_aware_send(conv, msg_text)
                self.on_log(f"[TG] ✓ forward {kind} OK conv={conv.conv_id[:8]}")
            else:
                self._conv_aware_send(conv,
                    f"⚠️ {kind} 中轉失敗 [{conv.account_name} / {conv.buyer_label}]\n\n"
                    f"閒魚源:{media_url[:80]}\n"
                    f"原因:{str(info)[:200]}\n"
                    f"請手動處理(可改用 reply: 文字回覆)"
                )
                self.on_log(f"[TG] forward {kind} 失敗 conv={conv.conv_id[:8]}: {info}")
        except Exception as e:
            self.on_log(f"[TG] _do_forward_media_to_yahoo 異常 conv={conv.conv_id[:8]}: {e}")

    # v6.1.65 Fix C:舊名 alias 保留向後兼容(以防別處還有呼叫)
    def _do_forward_image_to_yahoo(self, conv: "ConversationState", image_url: str) -> None:
        """[LEGACY] v6.1.65 起改用 _do_forward_media_to_yahoo,此函數保留向後兼容。"""
        self._do_forward_media_to_yahoo(conv, image_url, media_kind="image")

    # ────────── v6.0.83:TG forum 整合 ──────────

    def _handle_orders_command(
        self,
        args: List[str],
        from_user_id: str,
        reply_fn,
        is_supervisor: bool,
    ) -> None:
        """v6.1:`/orders` 指令處理 — 查當前訂單列表。

        - /orders        → 過濾 status_label == "待出貨"
        - /orders all    → 所有狀態
        - /orders <acc>  → 過濾單一帳號名
        後台 thread 跑(避免 blocking polling loop)。
        """
        import threading as _t

        def _bg():
            try:
                from .accounts import load_accounts as _la
                from .order_http import fetch_orders as _fo, format_order_for_tg as _fmt, classify_order as _cls_o
                try:
                    from .employees import find_employee_by_tg_user_id as _find_emp
                except Exception:
                    _find_emp = None

                # 決定要查哪些帳號(部署模型:每人一個 instance,accounts.json 只有自己的)
                all_accounts = _la() or []
                if is_supervisor:
                    # 本 instance 的 accounts.json 全部
                    target_accs = [a["name"] for a in all_accounts if a.get("monitor_selected", True)]
                elif _find_emp:
                    # legacy 集中部署:從 employees.json 拿綁定帳號
                    me = _find_emp(str(from_user_id))
                    if not me:
                        reply_fn("⚠️ 你還沒綁定。先用 `/bind <名字> <帳號>` 綁定才能查。")
                        return
                    target_accs = list(me.get("accounts") or [])
                else:
                    target_accs = []

                # 過濾參數
                filter_name = ""
                only_waiting = True
                for a in args:
                    al = a.lower()
                    if al == "all":
                        only_waiting = False
                    else:
                        filter_name = a  # 假設是帳號名
                if filter_name:
                    target_accs = [n for n in target_accs if filter_name.lower() in n.lower()]
                if not target_accs:
                    reply_fn(f"⚠️ 沒找到符合的帳號:`{filter_name or '(空)'}`")
                    return

                reply_fn(f"⏳ 查詢中... {len(target_accs)} 個帳號")

                # 跑 fetch_orders 對每個帳號
                results = []  # [(acc_name, [orders])]
                fail = []
                base_dir = self._base_dir if hasattr(self, "_base_dir") else "."
                from pathlib import Path as _P
                profiles_dir = _P(base_dir) / "profiles"
                for acc in target_accs:
                    # acc 在 accounts.json 內 name → profile_id
                    profile_id = next(
                        (a["profile_id"] for a in all_accounts if a.get("name") == acc),
                        acc,  # fallback 假設 name == profile_id
                    )
                    pd = profiles_dir / profile_id
                    if not pd.exists():
                        fail.append(f"{acc}(profile 不存在)")
                        continue
                    try:
                        orders, err = _fo(pd)
                        if err:
                            fail.append(f"{acc}({err[:50]})")
                            continue
                        if only_waiting:
                            # v6.2:用 classify_order 篩,自動排除「已退款/已取消但 status_label 還是『待出貨』」的訂單
                            orders = [o for o in orders if _cls_o(
                                o.get("status",""), o.get("payment_status",""),
                                o.get("status_label",""), o.get("status_extra","")
                            ) == "waiting_paid"]
                        if orders:
                            results.append((acc, orders))
                    except Exception as e:
                        fail.append(f"{acc}({str(e)[:50]})")

                # 整合回覆
                total = sum(len(o) for _, o in results)
                if total == 0:
                    msg = f"📋 *訂單查詢*({'待出貨' if only_waiting else '所有狀態'})\n\n"
                    msg += f"📦 共 {len(target_accs)} 個帳號,目前 *沒有* 符合的訂單"
                    if fail:
                        msg += f"\n\n⚠️ {len(fail)} 個帳號查詢失敗:\n  • " + "\n  • ".join(fail[:5])
                    reply_fn(msg)
                    return

                # v6.2:總覽訊息先發,然後逐筆訂單發獨立卡片(帶「聯繫買家」按鈕)
                summary_lines = [
                    f"📋 *訂單列表*({'真正需要出貨' if only_waiting else '所有狀態'})",
                    f"共 *{total}* 筆 / {len(target_accs)} 個帳號",
                ]
                if only_waiting:
                    summary_lines.append("_💡 已自動排除已退款/已出貨訂單(以實際需要行動為準)_")
                summary_lines.append("━━━━━━━━━━━━━━━━━━━")
                summary_lines.append("📝 下方逐筆顯示 · 按 *💬 聯繫買家* 直接跳對話")
                if fail:
                    summary_lines.append(f"\n⚠️ {len(fail)} 個帳號失敗:")
                    for f in fail[:3]:
                        summary_lines.append(f"  • {f}")
                reply_fn("\n".join(summary_lines))

                # 逐筆訂單獨立訊息 + 聯繫買家按鈕(防洗版 cap 20 筆)
                from html import escape as _h_esc
                MAX_SEND = 20
                sent = 0
                profile_lookup = {a.get("name"): a.get("profile_id", a.get("name"))
                                  for a in all_accounts}
                for acc, orders in results:
                    pid = profile_lookup.get(acc, acc)
                    for o in orders:
                        if sent >= MAX_SEND:
                            break
                        oid = (o.get("order_id", "") or "")
                        amount = o.get("amount", 0)
                        status_lbl = o.get("status_label", "")
                        buyer_name = o.get("buyer_name", "")
                        buyer_id = o.get("buyer_id", "")
                        buyer_show = buyer_name or buyer_id or "?"
                        items = o.get("items") or []
                        title = (items[0].get("title", "") if items else "")
                        ship_id = items[0].get("shipping_id", "") if items else ""
                        # HTML 模式(更穩 escape,避免商品標題的 _* 破 Markdown)
                        text_lines = [
                            f"📦 <b>訂單 #{_h_esc(oid)}</b>",
                            f"💰 NT${amount}  ·  📍 {_h_esc(status_lbl)}",
                            f"👤 帳號 <code>{_h_esc(acc)}</code>  ·  買家 <code>{_h_esc(buyer_show)}</code>",
                        ]
                        if title:
                            text_lines.append(f"🎁 {_h_esc(title)}")
                        if ship_id:
                            text_lines.append(f"🚚 單號 <code>{_h_esc(ship_id)}</code>")
                        # v6.2:聯繫買家用 order_id(handler 拉商品+D1 貨源)
                        cb = f"oc:buy:{pid}:{oid}"
                        buttons = None
                        if oid and buyer_id and len(cb.encode("utf-8")) <= 64:
                            buttons = [[{
                                "text": f"💬 聯繫買家 {buyer_show}",
                                "callback_data": cb,
                            }]]
                        reply_fn("\n".join(text_lines), parse_mode="HTML", buttons=buttons)
                        sent += 1
                    if sent >= MAX_SEND:
                        break

                if total > sent:
                    reply_fn(
                        f"\n_...還有 *{total - sent}* 筆未顯示_\n"
                        f"_可用 `/orders 帳號名` 過濾單一帳號_"
                    )

                # v6.2:結尾推快捷面板,讓用戶接著一鍵切換查詢(不用再打 /orders)
                reply_fn(
                    "🔁 *快捷操作* · 點按鈕直接查詢",
                    buttons=self._QUICK_PANEL_BUTTONS,
                )

            except Exception as e:
                self.on_log(f"[TG-FORUM] /orders 處理異常: {e}")
                try:
                    reply_fn(f"❌ 查詢失敗: {str(e)[:200]}")
                except Exception:
                    pass

        _t.Thread(target=_bg, daemon=True, name="orders-query").start()

    def _handle_forum_admin_command(
        self, text: str, from_user_id: str, thread_id: int, source_chat_id: str = "",
    ) -> bool:
        """處理 forum 內管理指令。返 True 表示已處理(caller 不再 dispatch)。

        支援指令(主管專用,主管 = settings.json tg_chat_id):
        - /bind <name> <account1> <account2> ...
        - /list_bindings
        - /unbind <name>
        - /myaccounts  (任何人皆可查自己負責的帳號)
        - /help        (列指令說明)
        """
        if not self.forum_bridge:
            return False
        try:
            from . import employees as _emp
            # 從 settings 拿主管 TG ID
            try:
                from .accounts import load_settings
                _st = load_settings() or {}
                supervisor_id = str(_st.get("tg_chat_id") or "")
            except Exception:
                supervisor_id = ""
            is_supervisor = str(from_user_id) == supervisor_id

            parts = text.strip().split()
            if not parts:
                return False
            cmd = parts[0].lower().split("@")[0]  # /bind@bot → /bind

            # v6.1.27:訓練 hook — 同事 forum 管理命令(訂單查詢/工作流動作)
            # 不是客服對話,但訓練端能重建「同事在這個時段查了什麼訂單」
            # 跟後續客服回應建立因果鏈(同事查訂單 → 主動聯繫客戶 → 對話)
            try:
                _tc_admin_cmd_map = {
                    "/orders": "admin:query_orders",
                    "/myaccounts": "admin:query_my_accounts",
                    "/list_bindings": "admin:list_bindings",
                    "/myid": "admin:query_myid",
                    "/help": "admin:help",
                    "/bind": "admin:bind_employee",
                    "/unbind": "admin:unbind_employee",
                    "/invite": "admin:invite_employee",
                    "/join": "admin:join_employee",
                    "/relist": "admin:relist_item",
                    "/轉刊": "admin:relist_item",
                    "/转刊": "admin:relist_item",
                    "/recall": "admin:recall_msg",
                    "/r": "admin:recall_msg",
                }
                _tc_admin_action = _tc_admin_cmd_map.get(cmd)
                if _tc_admin_action:
                    # is_supervisor 補強:settings 載入失敗時 supervisor_id="",
                    # 此時所有 user 都會被誤判為 non-supervisor → metadata 加 flag 讓訓練端能識別
                    _settings_loaded = bool(supervisor_id)
                    _tc_record(
                        _tc_admin_action,
                        conv=None,
                        conv_key_override=f"admin|{from_user_id}|{source_chat_id or ''}",
                        profile_id_override="",
                        input={"command": cmd, "args": parts[1:][:10]},
                        output=None,
                        chosen_action=cmd,
                        metadata={
                            "from_user_id": str(from_user_id),
                            "is_supervisor": is_supervisor,
                            "supervisor_settings_loaded": _settings_loaded,
                            "thread_id": thread_id,
                            "channel": "forum_admin",
                        },
                    )
            except Exception:
                pass

            def _reply(msg: str, parse_mode: str = "Markdown", buttons: Optional[list] = None):
                """回覆訊息到當前 group(thread_id 若 0 就 group 主聊天)。

                v6.1:default parse_mode=Markdown,因為 /orders /myaccounts 等回覆內含 *粗體* `code`。
                v6.2:加 buttons 參數支援 inline_keyboard(/orders 每筆訂單帶聯繫買家按鈕)。
                """
                payload: Dict[str, Any] = {
                    "chat_id": source_chat_id or self.forum_bridge.bot.forum_chat_id,
                    "text": msg[:4000],
                }
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                if thread_id:
                    payload["message_thread_id"] = thread_id
                if buttons:
                    payload["reply_markup"] = {"inline_keyboard": buttons}
                self.forum_bridge.bot._post("sendMessage", payload)

            if cmd == "/myid":
                # 同事 onboarding 用:在自己 group 內打 /myid → bot 回 user_id + group_id
                _reply(
                    f"📇 *身份資訊*\n\n"
                    f"  你的 TG user_id: `{from_user_id}`\n"
                    f"  這 group chat_id: `{source_chat_id}`\n\n"
                    f"📌 填到軟件 GUI:\n"
                    f"  settings.json → `tg_chat_id`: `{from_user_id}`\n"
                    f"  settings.json → `tg_forum_chat_id`: `{source_chat_id}`\n"
                    f"  tg_relay_config.json → `user_id`: `{from_user_id}`\n\n"
                    f"重啟軟件後,你的軟件 instance 就會自動 polling 屬於你的訊息。"
                )
                return True

            if cmd == "/help":
                _reply(
                    "📖 *指令說明*\n\n"
                    "*同事 onboarding(每人一個軟件 instance):*\n"
                    "  1. 你建 TG supergroup + 啟用 Topics\n"
                    "  2. 邀 @example_forum_bot 進 group + 設 Admin\n"
                    "  3. 在 group 內打 `/myid` → bot 給你需要的 ID\n"
                    "  4. 把 ID 填到軟件 settings.json + tg_relay_config.json\n"
                    "  5. 軟件 accounts.json 只列你負責的 Yahoo 帳號\n"
                    "  6. 啟動軟件 → KV 模式自動 polling 屬於你的訊息\n\n"
                    "*任何人:*\n"
                    "  /myid             — 拿你的 TG user_id 跟 group chat_id\n"
                    "  /myaccounts       — 看你綁定的帳號列表\n"
                    "  /orders           — 查當前所有待出貨訂單\n"
                    "  /orders all       — 查所有狀態訂單\n"
                    "  /orders 帳號名     — 過濾單一帳號\n"
                    "  /help             — 此說明"
                )
                return True

            if cmd == "/myaccounts":
                me = _emp.find_employee_by_tg_user_id(from_user_id)
                if not me:
                    _reply(f"⚠️ 你還沒綁定。請主管打 `/bind <你的名字> <帳號>`")
                    return True
                accs = me.get("accounts") or []
                _reply(
                    f"👤 你綁定為:*{me.get('name','')}*\n"
                    f"📦 負責帳號 ({len(accs)}):\n  • " + "\n  • ".join(accs or ["(無)"])
                )
                return True

            if cmd == "/orders":
                # v6.1:訂單中心查詢
                #   /orders          — 列當前所有待出貨訂單
                #   /orders all      — 列所有狀態訂單
                #   /orders 帳號名    — 過濾單一帳號
                self._handle_orders_command(
                    args=parts[1:] if len(parts) > 1 else [],
                    from_user_id=from_user_id,
                    reply_fn=_reply,
                    is_supervisor=is_supervisor,
                )
                return True

            if cmd == "/list_bindings":
                all_emp = _emp.list_employees()
                if not all_emp:
                    _reply("ℹ️ 尚無同事綁定")
                    return True
                lines = ["📋 *當前綁定*\n"]
                for name, e in all_emp.items():
                    accs = e.get("accounts") or []
                    lines.append(
                        f"• *{name}* (tg={e.get('tg_user_id','-')})\n"
                        f"  group: `{e.get('forum_chat_id','-')}`\n"
                        f"  帳號 ({len(accs)}): {', '.join(accs) if accs else '(無)'}"
                    )
                _reply("\n".join(lines))
                return True

            if cmd == "/invite":
                if not is_supervisor:
                    _reply("⚠️ 只有主管能用 /invite")
                    return True
                if len(parts) < 3:
                    _reply(
                        "用法:`/invite <同事名> <帳號1> <帳號2> ...`\n"
                        "例:`/invite alice kinhuaw168 chen749`\n"
                        "→ bot 生成 30 分鐘有效的邀請碼,同事打 `/join CODE` 完成"
                    )
                    return True
                emp_name = parts[1]
                accounts_list = parts[2:]
                code = _emp.create_invite(emp_name, accounts_list, created_by=from_user_id)
                _reply(
                    f"✅ *邀請碼已生成*\n\n"
                    f"  同事:*{emp_name}*\n"
                    f"  帳號 ({len(accounts_list)}):{', '.join(accounts_list)}\n"
                    f"  有效期:30 分鐘\n\n"
                    f"━━━ 把下面這段給同事 ━━━\n"
                    f"請依序完成:\n"
                    f"1. 建 TG supergroup(任意名稱)\n"
                    f"2. 群組設定 → 啟用 *Topics*\n"
                    f"3. 加 @example_forum_bot 進 group → 設 *Admin*(權限 Manage Topics)\n"
                    f"4. 在 group 內打:\n"
                    f"   `/join {code}`\n"
                    f"━━━━━━━━━━━━"
                )
                return True

            if cmd == "/join":
                if len(parts) < 2:
                    _reply("用法:`/join <邀請碼>`\n邀請碼由主管 `/invite` 生成")
                    return True
                code = parts[1]
                ok, msg, entry = _emp.consume_invite(
                    code, tg_user_id=from_user_id, forum_chat_id=source_chat_id,
                )
                if ok:
                    accs = entry.get("accounts") or []
                    _reply(
                        f"✅ *綁定成功!*\n\n"
                        f"  同事:*{msg}*(TG: `{from_user_id}`)\n"
                        f"  group:`{source_chat_id}`\n"
                        f"  你負責的帳號 ({len(accs)}):\n  • " + "\n  • ".join(accs) + "\n\n"
                        f"📦 之後這些帳號的新買家訊息會自動建 topic 在這 group。\n"
                        f"💬 你在 topic 內回覆 → 自動 send 給對應買家。"
                    )
                else:
                    _reply(f"❌ {msg}")
                return True

            if cmd == "/list_invites":
                if not is_supervisor:
                    _reply("⚠️ 只有主管能用 /list_invites")
                    return True
                invites = _emp.list_active_invites()
                if not invites:
                    _reply("ℹ️ 沒有待用邀請碼")
                    return True
                import time as _t
                lines = ["📋 *待用邀請碼*\n"]
                for c, v in invites.items():
                    remain = int(v.get("expires_ts", 0) - _t.time())
                    lines.append(
                        f"• `{c}` — *{v.get('name','')}* ({len(v.get('accounts',[]))} 帳號) "
                        f"剩 {remain//60} 分鐘"
                    )
                _reply("\n".join(lines))
                return True

            if cmd == "/bind":
                if not is_supervisor:
                    _reply("⚠️ 只有主管能用 /bind")
                    return True
                if len(parts) < 3:
                    _reply("用法:`/bind <同事名> <帳號1> <帳號2> ...`")
                    return True
                emp_name = parts[1]
                accounts_list = parts[2:]
                # 把當前 group chat_id 綁定為該同事的 forum_chat_id
                ok, err = _emp.bind_employee(
                    emp_name,
                    forum_chat_id=source_chat_id or str(self.forum_bridge.bot.forum_chat_id),
                    accounts=accounts_list,
                )
                if ok:
                    _reply(
                        f"✅ 綁定成功:\n"
                        f"  同事:*{emp_name}*\n"
                        f"  group:`{source_chat_id}`\n"
                        f"  帳號 ({len(accounts_list)}):{', '.join(accounts_list)}\n\n"
                        f"⏭ 同事請打 `/myaccounts` 確認"
                    )
                else:
                    _reply(f"❌ 綁定失敗:{err}")
                return True

            if cmd == "/unbind":
                if not is_supervisor:
                    _reply("⚠️ 只有主管能用 /unbind")
                    return True
                if len(parts) < 2:
                    _reply("用法:`/unbind <同事名>`")
                    return True
                emp_name = parts[1]
                ok, err = _emp.unbind_employee(emp_name)
                _reply(f"{'✅ 已解綁 ' + emp_name if ok else '❌ ' + err}")
                return True

        except Exception as e:
            self.on_log(f"[TG-FORUM] admin command 異常: {e}")
        return False

    def _on_tg_forum_reply(
        self,
        message_thread_id: int,
        text: str,
        message_id: int,
        from_user_id: str,
        photo_file_id: Optional[str],
        video_file_id: Optional[str],
        reply_to_text: str = "",
        reply_to_msg_id: int = 0,
        source_chat_id: str = "",  # v6.1:多同事 group 場景
    ) -> None:
        """TG forum topic 內收到回覆 → 透過 forum_bridge dispatch 到 Yahoo 買家。

        每個 Yahoo 買家對話對應一個 TG topic;在 topic 內任意 reply 自動 send 給該買家。
        若用戶引用了某條客戶訊息(reply_to_text),前綴 `引用「...」\\n` 一起送(階段 4a)。
        """
        if not self.forum_bridge:
            return
        # v6.1:topic=0 是 group 主聊天(非 forum topic 內),通常是 sticker/系統訊息
        # 或同事不小心在 group 主聊天打字 — 不該 dispatch,silent return 不 spam log
        if not message_thread_id and not text and not photo_file_id and not video_file_id:
            return  # 空訊息直接 skip
        if not message_thread_id:
            # group 主聊天非空訊息 — silent skip(沒對應 Yahoo 對話)
            return
        try:
            self.on_log(
                f"[TG-FORUM] 收到 topic={message_thread_id} 回覆 "
                f"text={text[:40]!r} photo={bool(photo_file_id)} video={bool(video_file_id)} "
                f"reply_to={reply_to_text[:30]!r}"
            )

            # v6.1.20:一鍵轉刊(forum 內優先路由,避免被當客戶訊息發出去)
            _fchat_id = source_chat_id or (
                str(self.forum_bridge.bot.forum_chat_id) if self.forum_bridge else ""
            )
            if _fchat_id:
                # 0. 進行中 session 最優先 — 避免 await_input/await_photo 期間被 URL detect 攔走
                #   修 v6.1.20.1 bug:改分類時輸入純數字 catId 被 ^(\d{8,15})$ 當新 URL 觸發
                _rl_sess = self._relist_get_session(_fchat_id)
                if _rl_sess and _rl_sess.topic_id == message_thread_id:
                    if _rl_sess.state == "await_input" and text:
                        if self._relist_handle_text_input(text, _fchat_id, message_thread_id):
                            return
                    if _rl_sess.state == "await_photo" and photo_file_id:
                        if self._relist_handle_photo(photo_file_id, _fchat_id, message_thread_id):
                            return

                # 1. /relist 命令
                if text and (
                    text.lower().startswith("/relist")
                    or text.startswith("/轉刊") or text.startswith("/转刊")
                ):
                    if self._relist_handle_command(text, _fchat_id, topic_id=message_thread_id):
                        return
                # 1b. v6.1.20:訂單中心 topic 內貼 Yahoo URL → 自動當 /relist 處理
                # 其他 topic 內貼 URL 是對話內容,不該攔截
                if text and self.forum_bridge and self.forum_bridge.store:
                    _oc_topic = self.forum_bridge.store.get_order_center_topic(str(_fchat_id)) or 0
                    if _oc_topic and message_thread_id == _oc_topic:
                        from .relist_feature import extract_item_id
                        _item_id = extract_item_id(text)
                        if _item_id and not text.startswith("/"):
                            # 自動加 /relist 前綴轉刊
                            _synth = f"/relist {text.strip()}"
                            try:
                                self._relist_handle_command(
                                    _synth, _fchat_id, topic_id=message_thread_id,
                                )
                            except Exception as _e_rl:
                                self.on_log(
                                    f"[RELIST] _relist_handle_command 異常: "
                                    f"{type(_e_rl).__name__}: {_e_rl}"
                                )
                            # 訂單中心 URL 一定算 relist 意圖,不再 fall through 給 forum dispatch
                            return

            # v6.1:管理指令(主管/同事在 group 內打的指令)
            # /bind <name> <account1> <account2> ...  ← 主管綁定同事
            # /list_bindings  ← 列出所有綁定
            # /unbind <name>  ← 解綁
            # /myaccounts     ← 同事查自己負責的帳號
            if text.startswith("/"):
                if self._handle_forum_admin_command(
                    text, from_user_id, message_thread_id, source_chat_id=source_chat_id,
                ):
                    return  # 指令已處理

            # v6.0.83:Forum 內 reply prompt(force_reply)接 conv action(edit/rewrite/reply)
            # 若 user 是 reply 某個我們之前發的 force_reply prompt → 走 conv pending_input 路徑
            if reply_to_msg_id and text:
                pending = self._consume_pending_input(reply_to_msg_id)
                if pending:
                    _cid, _action = pending
                    # ⭐ 同步清掉 topic-level latest_pending(避免之後無引用訊息誤觸 fallback)
                    try:
                        _tkey_clean = f"topic:{source_chat_id or self.forum_bridge.bot.forum_chat_id}:{message_thread_id}"
                        with self._latest_pending_lock:
                            self._latest_pending_by_chat.pop(_tkey_clean, None)
                    except Exception:
                        pass
                    # ⭐ DONE/EXPIRED 不走 pending_input,fall through 到正常 topic dispatch
                    # (避免用戶引用 stale prompt 結果看到「對話已結束」popup + 訊息丟失)
                    with self._lock:
                        _check_conv = self._convs.get(_cid)
                    if (_check_conv and
                            _check_conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED)):
                        self.on_log(
                            f"[TG-FORUM] pending hit DONE conv {_cid[:8]} ({_check_conv.phase.value})"
                            f" → fall through to dispatch as normal topic reply"
                        )
                        # 不 return,繼續往下走 dispatch_topic_reply
                    else:
                        self._handle_pending_input(_cid, _action, text)
                        return

            # v6.1:forum 內 TG Desktop force_reply 不會自動觸發 reply UI →
            # user 看到 prompt 後直接打字(沒引用) → 走 topic-level latest pending fallback
            # 跟舊版 yahoo 轉發客服私聊體驗一致(舊版私聊 force_reply 會自動引用)
            # 5 分鐘內最後一次 force_reply prompt 被當作目標
            if text and not reply_to_msg_id and not photo_file_id and not video_file_id:
                # 排除指令類
                _t = text.strip()
                if not (_t.startswith("/") or _t in ("ok", "skip", "skp")):
                    tkey = f"topic:{source_chat_id or self.forum_bridge.bot.forum_chat_id}:{message_thread_id}"
                    topic_pending = self._consume_latest_pending(tkey)
                    if topic_pending:
                        _cid, _action = topic_pending
                        self.on_log(
                            f"[TG-FORUM] 命中 topic pending fallback(無引用): "
                            f"conv={_cid[:8]} action={_action}"
                        )
                        self._handle_pending_input(_cid, _action, text)
                        return

            # 撤回:reply 某條訊息 + 打「/recall」或「/r」→ 反查 yahoo_msg_id → BOSH recall
            #       + 在 TG 也 deleteMessage 同步刪掉
            _text_low = (text or "").strip().lower()
            if reply_to_msg_id and _text_low in ("/recall", "/r", "撤回", "/recall@" + (self.forum_bridge.bot.token.split(":")[0] if self.forum_bridge.bot.token else "")):
                self._handle_forum_recall(message_thread_id, reply_to_msg_id, message_id)
                return

            # 階段 4b:有 reply_to_msg_id 就反查 yahoo_msg_id → BOSH send_reply_message
            # 反查失敗(沒記到 mapping)就退到 4a 模式拼引用上下文進 text
            parent_yahoo_info: Dict[str, Any] = {}
            if reply_to_msg_id:
                try:
                    parent_yahoo_info = self.forum_bridge.lookup_parent_yahoo_info(
                        message_thread_id, reply_to_msg_id,
                    )
                except Exception:
                    parent_yahoo_info = {}

            dispatch_text = text
            if reply_to_text and not parent_yahoo_info and not photo_file_id and not video_file_id:
                # ⭐ 偵測 bot 自己發的訊息(force_reply prompt、狀態通知、訂單卡等)
                # 若是 bot 訊息,**不加** quote prefix,只送 user 真實輸入的文字
                # 否則同事 reply 一個 prompt 會把整個 "請直接輸入..." 當引用送 Yahoo(2026-05-20 bug)
                _bot_msg_markers = (
                    "📃", "🛍", "📦", "📩", "🤖", "🔔", "🔍",
                    "✅", "❌", "⚠️", "⛔",
                    "請直接輸入", "请直接输入",
                    "請手動", "请手动",
                    "已發送到", "已发送到",
                    "AI 草稿", "AI 分析中",
                    "刷新狀態", "对话历史", "對話歷史",
                    "本次訂單商品", "本次订单商品",
                    "Yahoo 拍賣 IM",
                    "管理员", "管理員",
                )
                _is_bot_msg = any(
                    reply_to_text.strip().startswith(m) or m in reply_to_text[:80]
                    for m in _bot_msg_markers
                )
                if _is_bot_msg:
                    # bot 自己訊息 → 只送 user typed text,不加 quote prefix
                    self.on_log(
                        f"[TG-FORUM] reply_to 是 bot 訊息(detected via marker),skip quote prefix"
                    )
                    dispatch_text = text
                else:
                    # 真正引用買家訊息(或不確定來源)→ 加 quote prefix 提示對方
                    quoted = reply_to_text[:120].replace("\n", " ")
                    dispatch_text = f"引用「{quoted}」\n{text}"

            # v6.1.27:訓練數據紀錄 — forum topic 內直接回覆
            # 情境分兩種:
            #   A. 有 active conv(買家剛問了東西,同事在 topic 內回應)→ 走正常 conv-based
            #   B. 主動聯繫(沒未讀,同事主動跟客戶聊訂單/招呼)→ 用 forum_topic pseudo key
            #     訓練端能學「同事什麼時候主動聯繫客戶」「主動聯繫怎麼開頭」
            _tc_conv_for_forum = None
            _tc_topic_pid = ""
            _tc_topic_cid = ""
            try:
                if self.forum_bridge and self.forum_bridge.store:
                    _key, _entry = self.forum_bridge.store.find_by_topic_id(message_thread_id)
                    if _entry:
                        _tc_topic_pid = _entry.get("profile_id", "")
                        _tc_topic_cid = _entry.get("chat_id", "")
                        # 找 active conv(buyer 觸發中)
                        for _c in list(self._convs.values()):
                            if _c.profile_id == _tc_topic_pid and _c.chat_id == _tc_topic_cid:
                                _tc_conv_for_forum = _c
                                break
            except Exception:
                _tc_conv_for_forum = None

            _is_proactive = _tc_conv_for_forum is None
            _tc_common_meta = {
                "channel": "forum_topic",
                "has_parent_yahoo_info": bool(parent_yahoo_info),
                "from_user_id": str(from_user_id),
                "is_proactive": _is_proactive,
                "topic_id": message_thread_id,
            }
            if _is_proactive and _TC is not None:
                # 主動聯繫:用 forum_topic|pid|chat|topic_id pseudo key
                try:
                    _proactive_key = _TC.build_forum_topic_key(
                        _tc_topic_pid, _tc_topic_cid, message_thread_id,
                    )
                except Exception:
                    _proactive_key = f"forum_topic|unknown|unknown|{message_thread_id}"
                _tc_record(
                    "user:forum_proactive",
                    conv=None,
                    conv_key_override=_proactive_key,
                    profile_id_override=_tc_topic_pid,
                    input={
                        "text": text,
                        "has_photo": bool(photo_file_id),
                        "has_video": bool(video_file_id),
                        "reply_to_text": (reply_to_text or "")[:200],
                    },
                    output=dispatch_text,
                    metadata=_tc_common_meta,
                )
            else:
                _tc_record(
                    "user:forum_topic_reply",
                    conv=_tc_conv_for_forum,
                    input={
                        "text": text,
                        "has_photo": bool(photo_file_id),
                        "has_video": bool(video_file_id),
                        "reply_to_text": (reply_to_text or "")[:200],
                    },
                    output=dispatch_text,
                    metadata=_tc_common_meta,
                )

            def _bg():
                try:
                    ok, info = self.forum_bridge.dispatch_topic_reply(
                        topic_id=message_thread_id,
                        reply_text=dispatch_text,
                        photo_file_id=photo_file_id or "",
                        video_file_id=video_file_id or "",
                        parent_yahoo_info=parent_yahoo_info,
                        source_topic_msg_id=message_id,
                    )
                    # v6.1.27:訓練 hook — forum dispatch 結果(代表訊息真的送到 Yahoo)
                    # 跟前面記的 user:forum_topic_reply / user:forum_proactive 同 conv_key
                    if _is_proactive and _TC is not None:
                        try:
                            _proactive_key = _TC.build_forum_topic_key(
                                _tc_topic_pid, _tc_topic_cid, message_thread_id,
                            )
                            _tc_record(
                                "send:yahoo_forum",
                                conv=None,
                                conv_key_override=_proactive_key,
                                profile_id_override=_tc_topic_pid,
                                input={"text": dispatch_text[:500]},
                                output={"ok": bool(ok), "info": str(info)[:200]},
                                metadata={"channel": "forum_dispatch",
                                          "is_proactive": True,
                                          "topic_id": message_thread_id},
                            )
                        except Exception:
                            pass
                    else:
                        _tc_record(
                            "send:yahoo_forum",
                            conv=_tc_conv_for_forum,
                            input={"text": dispatch_text[:500]},
                            output={"ok": bool(ok), "info": str(info)[:200]},
                            metadata={"channel": "forum_dispatch",
                                      "is_proactive": False,
                                      "topic_id": message_thread_id},
                        )
                    # 成功 ✅ / 失敗 ❌ 都加 reaction(user 看 user 原訊息上的 emoji 就懂)
                    # 失敗 額外 push 錯誤訊息給細節
                    try:
                        # v6.1:用 source_chat_id(訊息來源 group),multi-tenant 才能對到
                        _reaction_chat = source_chat_id or str(self.forum_bridge.bot.forum_chat_id)
                        self.forum_bridge.bot._post("setMessageReaction", {
                            "chat_id": _reaction_chat,
                            "message_id": message_id,
                            "reaction": [{"type": "emoji", "emoji": "✅" if ok else "❌"}],
                        })
                    except Exception:
                        pass
                    # 統一風格:成功 push「✅ 已發送到 Yahoo IM「內容」」 — 跟 AI 客服路徑一致
                    # BOSH 兜底成功時加說明(讓 user 知道是新 channel)
                    if ok:
                        _content_to_show = dispatch_text or text or ""
                        # v6.1.53:確認訊息掛 inline「撤回」按鈕(改進 C)
                        # callback_data = fr:{topic_id}:{user_msg_id}
                        # TG Bot API 限制 callback_data ≤ 64 bytes,topic_id + user_msg_id 數字夠用
                        _recall_btn_label = "🗑 撤回"
                        # 多段視頻 / 多張圖時改顯示
                        try:
                            _parent = self.forum_bridge.lookup_parent_yahoo_info(
                                message_thread_id, message_id,
                            )
                            _msg_ids_list = (_parent or {}).get("yahoo_msg_ids") or []
                            if len(_msg_ids_list) > 1:
                                _recall_btn_label = f"🗑 撤回 {len(_msg_ids_list)} 段"
                        except Exception:
                            pass
                        _recall_kbd = {
                            "inline_keyboard": [[
                                {"text": _recall_btn_label,
                                 "callback_data": f"fr:{message_thread_id}:{message_id}"},
                            ]],
                        }
                        if _content_to_show:
                            _shown = _content_to_show if len(_content_to_show) <= 200 else _content_to_show[:200] + "…"
                            _hdr = "✅ 已發送到 Yahoo IM"
                            if "BOSH" in str(info):
                                _hdr += "(新對話 + order)"
                            self.forum_bridge.bot.send_text(
                                message_thread_id,
                                f"{_hdr}\n「{_shown}」",
                                chat_id=source_chat_id or None,
                                reply_markup=_recall_kbd,
                            )
                        elif photo_file_id or video_file_id:
                            _kind = "圖片" if photo_file_id else "視頻"
                            self.forum_bridge.bot.send_text(
                                message_thread_id,
                                f"✅ 已發送 {_kind} 到 Yahoo IM",
                                chat_id=source_chat_id or None,
                                reply_markup=_recall_kbd,
                            )
                    if not ok:
                        self.forum_bridge.bot.send_text(
                            message_thread_id,
                            f"❌ {info[:300]}",
                            chat_id=source_chat_id or None,
                        )
                    self.on_log(
                        f"[TG-FORUM] dispatch topic={message_thread_id} "
                        f"{'OK' if ok else 'FAIL'}: {info[:80]}"
                    )
                except Exception as e:
                    self.on_log(f"[TG-FORUM] dispatch_topic_reply 異常: {e}")
            threading.Thread(target=_bg, daemon=True).start()
        except Exception as e:
            self.on_log(f"[TG-FORUM] _on_tg_forum_reply 異常: {e}")

    def _handle_forum_recall(
        self,
        topic_id: int,
        reply_to_msg_id: int,
        command_msg_id: int,
    ) -> None:
        """TG topic 內 reply 某條 + 打 /recall → BOSH recall Yahoo 那條 + TG 也刪。

        - 反查 reply_to_msg_id 對應的 yahoo_msg_id
        - 呼叫 BOSH recall_messages
        - 成功後 TG deleteMessage 同步刪除(包含 /recall 命令自己跟原訊息)
        """
        if not self.forum_bridge:
            return
        try:
            parent = self.forum_bridge.lookup_parent_yahoo_info(topic_id, reply_to_msg_id)
            # v6.1:從 topic 找 conv,拿正確的 group chat_id(multi-tenant)
            key, entry = self.forum_bridge.store.find_by_topic_id(topic_id)
            target_chat = (entry.get("forum_chat_id") if entry else "") or str(self.forum_bridge.bot.forum_chat_id)
            if not parent or (not parent.get("yahoo_msg_id") and not parent.get("yahoo_msg_ids")):
                self.forum_bridge.bot.send_text(
                    topic_id, "⚠️ 找不到對應 Yahoo msg_id,無法撤回(可能訊息不在 mapping 內)",
                    chat_id=target_chat,
                )
                return
            # v6.1.53:優先用 yahoo_msg_ids(list,多段視頻全撤),fallback yahoo_msg_id(single)
            yahoo_msg_ids = parent.get("yahoo_msg_ids") or []
            if not yahoo_msg_ids and parent.get("yahoo_msg_id"):
                yahoo_msg_ids = [parent["yahoo_msg_id"]]
            n_targets = len(yahoo_msg_ids)
            profile_id, chat_id = "", ""
            if entry:
                profile_id = entry.get("profile_id", "")
                chat_id = entry.get("chat_id", "")
            if not profile_id:
                self.forum_bridge.bot.send_text(topic_id, "⚠️ 找不到 profile_id", chat_id=target_chat)
                return

            def _bg():
                try:
                    profile_dir = Path(self._base_dir) / "profiles" / profile_id
                    from core.yahoo_im_bosh_ext import bosh_recall_messages
                    ok, info = bosh_recall_messages(
                        profile_dir, yahoo_msg_ids,
                        on_log=self.on_log,
                    )
                    if ok:
                        # TG 端也刪掉原訊息 + /recall 命令訊息(用對的 group chat_id)
                        for mid in (reply_to_msg_id, command_msg_id):
                            self.forum_bridge.bot._post("deleteMessage", {
                                "chat_id": target_chat,
                                "message_id": mid,
                            })
                        # v6.1.53:多段視頻撤回提示帶段數
                        _summary = (
                            f"視頻 {n_targets} 段全部撤回" if n_targets > 1
                            else f"yahoo_msg_id={yahoo_msg_ids[0][:12]}..."
                        )
                        self.forum_bridge.bot.send_text(
                            topic_id,
                            f"✅ 已撤回 {_summary}",
                            chat_id=target_chat,
                        )
                        self.on_log(f"[TG-FORUM] recall OK ({n_targets} msg) first={yahoo_msg_ids[0][:12]}")
                    else:
                        self.forum_bridge.bot.send_text(
                            topic_id, f"❌ 撤回失敗: {info[:200]}", chat_id=target_chat,
                        )
                except Exception as e:
                    self.on_log(f"[TG-FORUM] recall 異常: {e}")
                    self.forum_bridge.bot.send_text(
                        topic_id, f"❌ 撤回異常: {e}", chat_id=target_chat,
                    )
            threading.Thread(target=_bg, daemon=True).start()
        except Exception as e:
            self.on_log(f"[TG-FORUM] _handle_forum_recall 異常: {e}")

    def _handle_forum_recall_button(
        self,
        topic_id: int,
        user_msg_id: int,
        confirmation_msg_id: int,
        source_chat_id: str = "",
    ) -> None:
        """v6.1.53:confirmation 訊息上 [🗑 撤回] 按鈕的 callback handler。

        - user_msg_id: 用戶當初發的訊息(我們要撤回對應的 Yahoo 那條/那幾條)
        - confirmation_msg_id: bot 自己送的「✅ 已發送」訊息(撤回成功後一起刪掉)

        跟 _handle_forum_recall 邏輯類似,差異:
        - 取 yahoo_msg_ids 直接從 user_msg_id 查 mapping(支援多段視頻)
        - TG 刪除:user 原訊息 + confirmation 訊息
        """
        if not self.forum_bridge:
            return
        try:
            parent = self.forum_bridge.lookup_parent_yahoo_info(topic_id, user_msg_id)
            key, entry = self.forum_bridge.store.find_by_topic_id(topic_id)
            target_chat = (entry.get("forum_chat_id") if entry else "") or source_chat_id or str(self.forum_bridge.bot.forum_chat_id)
            if not parent or (not parent.get("yahoo_msg_id") and not parent.get("yahoo_msg_ids")):
                self.forum_bridge.bot.send_text(
                    topic_id, "⚠️ 找不到對應 Yahoo msg_id,無法撤回",
                    chat_id=target_chat,
                )
                return
            yahoo_msg_ids = parent.get("yahoo_msg_ids") or []
            if not yahoo_msg_ids and parent.get("yahoo_msg_id"):
                yahoo_msg_ids = [parent["yahoo_msg_id"]]
            n_targets = len(yahoo_msg_ids)
            profile_id = ""
            if entry:
                profile_id = entry.get("profile_id", "")
            if not profile_id:
                self.forum_bridge.bot.send_text(topic_id, "⚠️ 找不到 profile_id", chat_id=target_chat)
                return

            def _bg():
                try:
                    profile_dir = Path(self._base_dir) / "profiles" / profile_id
                    from core.yahoo_im_bosh_ext import bosh_recall_messages
                    ok, info = bosh_recall_messages(
                        profile_dir, yahoo_msg_ids,
                        on_log=self.on_log,
                    )
                    if ok:
                        # TG 刪 user 原訊息 + confirmation 訊息
                        for mid in (user_msg_id, confirmation_msg_id):
                            try:
                                self.forum_bridge.bot._post("deleteMessage", {
                                    "chat_id": target_chat,
                                    "message_id": mid,
                                })
                            except Exception:
                                pass
                        _summary = (
                            f"視頻 {n_targets} 段全部撤回" if n_targets > 1
                            else f"yahoo_msg_id={yahoo_msg_ids[0][:12]}..."
                        )
                        self.forum_bridge.bot.send_text(
                            topic_id,
                            f"✅ 已撤回 {_summary}",
                            chat_id=target_chat,
                        )
                        self.on_log(f"[TG-FORUM] recall(button) OK ({n_targets} msg)")
                    else:
                        self.forum_bridge.bot.send_text(
                            topic_id, f"❌ 撤回失敗: {info[:200]}", chat_id=target_chat,
                        )
                except Exception as e:
                    self.on_log(f"[TG-FORUM] recall(button) 異常: {e}")
                    try:
                        self.forum_bridge.bot.send_text(
                            topic_id, f"❌ 撤回異常: {e}", chat_id=target_chat,
                        )
                    except Exception:
                        pass
            threading.Thread(target=_bg, daemon=True).start()
        except Exception as e:
            self.on_log(f"[TG-FORUM] _handle_forum_recall_button 異常: {e}")

    def _maybe_forward_yahoo_to_forum(
        self,
        *,
        profile_id: str,
        yahoo_chat_id: str,
        buyer_label: str,
        account_name: str,
        text: str,
    ) -> None:
        """Yahoo 收到買家訊息 → 同步 forward 到 TG forum topic(如啟用)。"""
        if not self.forum_bridge:
            return
        try:
            def _bg():
                try:
                    ok, info = self.forum_bridge.forward_yahoo_inbound(
                        profile_id=profile_id,
                        yahoo_chat_id=yahoo_chat_id,
                        buyer_label=buyer_label,
                        account_name=account_name,
                        text=text,
                    )
                    if not ok:
                        self.on_log(f"[TG-FORUM] forward 失敗: {info}")
                except Exception as e:
                    self.on_log(f"[TG-FORUM] forward 異常: {e}")
            threading.Thread(target=_bg, daemon=True).start()
        except Exception as e:
            self.on_log(f"[TG-FORUM] _maybe_forward_yahoo_to_forum 異常: {e}")

    # ────────── v6.0.83:TG forum menu(/accounts /buyers /history)──────────

    def _diag_im_handler(self, account_name: str) -> None:
        """v6.1.20:/diag_im 後台執行 — 直接拉 BOSH 看為何沒生 buyer topic。

        步驟:
        1. 解析帳號名 → profile_id → profile_dir
        2. 檢查 sync mark / pending mark / cookie cache
        3. BOSHSession 建立 → list_channels_by_lastmsgtime
        4. 統計 channels 總數 / 90 天內活躍 / lastMsgTime 分布
        5. 結論 + 建議
        """
        target_chat = self.tg.chat_id or ""
        try:
            from .accounts import load_accounts
            accs = load_accounts() or []
            acc = next(
                (a for a in accs
                 if a.get("name") == account_name or a.get("profile_id") == account_name),
                None,
            )
            if not acc:
                self.tg.send(
                    f"❌ 找不到帳號 {account_name!r}\n"
                    f"可選:{[a.get('name') for a in accs[:8]]} ..."
                )
                return
            profile_id = acc.get("profile_id") or account_name
            profile_dir = Path(self._base_dir) / "profiles" / profile_id
            if not profile_dir.exists():
                self.tg.send(f"❌ profile 不存在: {profile_dir}")
                return

            lines = [f"🔍 <b>診斷 {account_name}</b>"]

            # 1. sync mark
            sync_mark = Path(self._base_dir) / "runtime" / "sync_state" / f"{profile_id}.done"
            pending_mark = Path(self._base_dir) / "runtime" / "sync_state" / f"{profile_id}.pending"
            if sync_mark.exists():
                import time as _t
                age_h = (_t.time() - sync_mark.stat().st_mtime) / 3600
                lines.append(f"⚠️ sync mark 存在(age={age_h:.1f}h)→ 不會再 sync")
                lines.append("  建議:打 <code>/resync " + account_name + "</code> 刪除後重啟")
            elif pending_mark.exists():
                lines.append("ℹ️ sync 上次標記 pending(拉到 0 對話),會 24h 後重試")
            else:
                lines.append("✅ 沒 sync mark,下次重啟應該會 sync")

            # 2. cookie cache 狀態
            try:
                import json
                cache_path = profile_dir / "cookie_cache.json"
                if cache_path.exists():
                    data = json.loads(cache_path.read_text(encoding="utf-8"))
                    import time as _t
                    age_min = (_t.time() - data.get("saved_at", 0)) / 60
                    lines.append(
                        f"🍪 cookie cache: cookies={len(data.get('cookies', {}))} "
                        f"wssid={'有' if data.get('wssid') else '無'} age={age_min:.0f} 分"
                    )
                else:
                    lines.append("❌ cookie_cache.json 不存在 → 需登入")
            except Exception as e:
                lines.append(f"⚠️ 讀 cookie cache 失敗: {e}")

            # 3. BOSH list_channels — 真實拉
            lines.append("")
            lines.append("🔄 拉 BOSH list_channels...")
            self.tg.send("\n".join(lines), parse_mode="HTML")
            lines = []

            try:
                from .yahoo_im_bosh_ext import BOSHSession
                with BOSHSession(profile_dir, on_log=self.on_log) as s:
                    body = {"ascSort": False, "unread": 0, "count": 100}
                    resp, err = s.iq("list_channels_by_lastmsgtime", body, iq_type="get")
                    if err:
                        lines.append(f"❌ BOSH iq 失敗: {err[:200]}")
                    elif not isinstance(resp, dict):
                        lines.append(f"❌ BOSH 回應異常,type={type(resp).__name__}")
                    else:
                        chs = resp.get("channels", resp.get("result", [])) or []
                        lines.append(f"📊 <b>BOSH 回:{len(chs)} 個 channel</b>")
                        lines.append(f"resp keys: <code>{list(resp.keys())}</code>")
                        if chs:
                            import time as _t
                            now_ms = int(_t.time() * 1000)
                            cutoff_90d = now_ms - 90 * 86400 * 1000
                            cutoff_30d = now_ms - 30 * 86400 * 1000
                            recent_90d = [c for c in chs if int(c.get("lastMsgTime", 0) or 0) >= cutoff_90d]
                            recent_30d = [c for c in chs if int(c.get("lastMsgTime", 0) or 0) >= cutoff_30d]
                            lines.append(f"📅 30 天內活躍: {len(recent_30d)}")
                            lines.append(f"📅 90 天內活躍: {len(recent_90d)}")
                            lines.append(f"📅 全部: {len(chs)}")
                            # 最新 3 個 channel 樣本
                            chs_sorted = sorted(
                                chs,
                                key=lambda c: int(c.get("lastMsgTime", 0) or 0),
                                reverse=True,
                            )
                            lines.append("最新 3 個 channel:")
                            for c in chs_sorted[:3]:
                                last_ms = int(c.get("lastMsgTime", 0) or 0)
                                last_dt = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(last_ms / 1000)) if last_ms else "?"
                                cid = c.get("chID") or ""
                                lines.append(f"  • {cid[-30:]} @ {last_dt}")
                            lines.append("")
                            if len(recent_90d) > 0:
                                lines.append(f"✅ 有 {len(recent_90d)} 個應該建 topic")
                                lines.append("如果 TG 沒看到 → 可能 sync 還沒跑完,或 /resync 一次")
                            else:
                                lines.append("⚠️ 所有對話都超過 90 天")
                                lines.append("settings.json 加 <code>\"tg_forum_sync_days\": 0</code> 全部同步")
                        else:
                            lines.append("")
                            lines.append("ℹ️ <b>真的沒任何買家對話</b>")
                            lines.append("這帳號可能:")
                            lines.append("  • 還沒接過買家訊息(新登入)")
                            lines.append("  • Yahoo IM 後台真的沒記錄")
                            lines.append(
                                "請登入 https://tw.bid.yahoo.com/mychat 確認是否真的沒對話"
                            )
            except Exception as e:
                lines.append(f"❌ BOSH 連線異常: {type(e).__name__}: {str(e)[:200]}")

            self.tg.send("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            try:
                self.tg.send(f"⚠️ /diag_im 後台異常: {type(e).__name__}: {e}")
            except Exception:
                pass

    def _handle_forum_menu_command(self, text: str, message_id: int) -> bool:
        """處理 /accounts /buyers /history 等 menu 命令。

        Returns True 表示已處理(不應繼續走其他命令分支)。
        """
        if not self._forum_menu:
            return False
        text_low = text.strip().lower()
        user_id = getattr(self.tg, "last_from_user_id", "") or self.tg.chat_id
        if not user_id:
            return False

        # v6.0.83:/pending 命令 — 列出所有待用戶處理的對話(PREVIEW_SENT / PREVIEW_SELLER 等)
        # /resync — 強制重新完整同步該帳號(刪 sync done mark)
        if text_low.startswith("/resync"):
            try:
                rest = text[len("/resync"):].strip()
                if not rest:
                    self.tg.send("⚠️ 用法: /resync <帳號名>(例:/resync kinhuaw168)")
                    return True
                target = rest
                sync_mark = Path(self._base_dir) / "runtime" / "sync_state" / f"{target}.done"
                if sync_mark.exists():
                    sync_mark.unlink()
                    self.tg.send(f"✅ 已刪除 {target} 的 sync mark,下次 monitor 輪詢時會重新完整同步")
                else:
                    self.tg.send(f"⚠️ {target} 沒 sync mark(可能還沒同步過或帳號名錯誤)")
                return True
            except Exception as e:
                self.tg.send(f"⚠️ /resync 異常: {e}")
                return True

        # v6.1.20:/diag_im <帳號名> — 自助診斷 BOSH 拉買家頻道狀況
        if text_low.startswith("/diag_im") or text_low.startswith("/diag"):
            try:
                rest = text.split(None, 1)
                arg = rest[1].strip() if len(rest) >= 2 else ""
                if not arg:
                    self.tg.send(
                        "⚠️ 用法: <code>/diag_im 帳號名</code>(例 <code>/diag_im kinhuaw168</code>)\n"
                        "用途:看為什麼帳號沒生 TG 買家 topic — 直接拉 BOSH 看真實 channel 數。",
                        parse_mode="HTML",
                    )
                    return True
                import threading as _t
                _t.Thread(
                    target=self._diag_im_handler, args=(arg,),
                    daemon=True, name=f"diag_im-{arg[:10]}",
                ).start()
                return True
            except Exception as e:
                self.tg.send(f"⚠️ /diag_im 異常: {e}")
                return True

        if text_low in ("/pending", "/待办", "/待辦"):
            try:
                lines = ["📋 *待處理對話:*", ""]
                pending_phases = (
                    ConvPhase.PREVIEW_SENT,
                    ConvPhase.PREVIEW_SELLER_QUESTION,
                    ConvPhase.PREVIEW_SELLER,
                    ConvPhase.ERROR,
                )
                with self._lock:
                    cands = [c for c in self._convs.values() if c.phase in pending_phases]
                cands.sort(key=lambda c: c.updated_ts or 0)
                if not cands:
                    self.tg.send("✅ 沒有待處理對話")
                    return True
                for c in cands[:30]:
                    phase_emoji = {
                        ConvPhase.PREVIEW_SENT: "📝",
                        ConvPhase.PREVIEW_SELLER_QUESTION: "❓",
                        ConvPhase.PREVIEW_SELLER: "📥",
                        ConvPhase.ERROR: "❌",
                    }.get(c.phase, "•")
                    age = int(time.time() - c.updated_ts) // 60 if c.updated_ts else 0
                    topic_id = self._find_topic_for_conv(c)
                    topic_hint = f"(topic #{topic_id})" if topic_id else ""
                    lines.append(
                        f"{phase_emoji} `[{c.account_name}]` {c.buyer_label} "
                        f"— {age}m {topic_hint}"
                    )
                self.tg.send_inline_keyboard("\n".join(lines), [])
                return True
            except Exception as e:
                self.on_log(f"[TG-MENU] /pending 異常: {e}")
                return True

        if text_low == "/accounts" or text_low.startswith("/accounts "):
            try:
                msg, kb = self._forum_menu.render_accounts_message(user_id)
                self.tg.send_inline_keyboard(msg, kb.get("inline_keyboard", []))
                self.on_log(f"[TG-MENU] /accounts user={user_id}")
                return True
            except Exception as e:
                self.on_log(f"[TG-MENU] /accounts 異常: {e}")
                self.tg.send(f"⚠️ /accounts 處理失敗: {e}")
                return True

        if text_low.startswith("/buyers "):
            profile_id = text[len("/buyers "):].strip()
            if not profile_id:
                self.tg.send("⚠️ 用法: /buyers <帳號名>")
                return True
            try:
                msg, kb = self._forum_menu.list_buyers_for_account(user_id, profile_id)
                self.tg.send_inline_keyboard(msg, kb.get("inline_keyboard", []))
                return True
            except Exception as e:
                self.tg.send(f"⚠️ /buyers 異常: {e}")
                return True

        if text_low.startswith("/history "):
            rest = text[len("/history "):].strip()
            # 格式: /history <profile_id> <channel_id>
            parts = rest.split(None, 1)
            if len(parts) != 2:
                self.tg.send("⚠️ 用法: /history <帳號> <channel_id>")
                return True
            try:
                msg, kb = self._forum_menu.show_history_for_buyer(user_id, parts[0], parts[1])
                self.tg.send_inline_keyboard(msg, kb.get("inline_keyboard", []))
                return True
            except Exception as e:
                self.tg.send(f"⚠️ /history 異常: {e}")
                return True

        # /menu /help_forum 顯示說明
        if text_low in ("/menu", "/help_forum"):
            self.tg.send(
                "🛍 **Yahoo IM 命令面板**\n\n"
                "/pending — 📋 待處理對話清單(AI 草稿等審核)\n"
                "/accounts — 🛍 列出你負責的 Yahoo 帳號\n"
                "/buyers <帳號> — 列該帳號所有 active 對話\n"
                "/history <帳號> <channel> — 看完整聊天記錄\n"
                "\n💡 在 buyer topic 內:\n"
                "  • 直接打字 → 自動 send 給買家\n"
                "  • 傳圖/視頻 → 自動轉發\n"
                "  • 引用某條 reply → Yahoo 原生回覆連結\n"
                "  • 引用 + 打「撤回」 → 雙端撤回"
            )
            return True

        return False

    def _on_forum_menu_callback(self, data: str, chat_id: str, message_id: int) -> bool:
        """處理 forum menu inline keyboard callback(fm:* 開頭)。"""
        if not self._forum_menu or not data.startswith("fm:"):
            return False
        user_id = getattr(self.tg, "last_from_user_id", "") or chat_id
        try:
            result = self._forum_menu.handle_callback(user_id, data)
            if result is None:
                return False
            text, kb = result
            if isinstance(kb, dict) and kb.get("force_reply"):
                # force_reply 模式 — 用 _api_send_message 帶 reply_markup
                prompt_msg_id = self.tg._api_send_message(chat_id, text, reply_markup=kb)
                # 把 prompt_msg_id 跟 token 反查的 (profile_id, channel_id) 註冊
                try:
                    parts = data.split(":", 2)
                    if len(parts) == 3 and parts[1] in ("reply", "reply_img", "reply_vid"):
                        v = self._forum_menu._resolve_token(user_id, parts[2])
                        v_parts = v.split("|", 2)
                        if len(v_parts) == 3 and prompt_msg_id:
                            with self._forum_pending_lock:
                                self._forum_pending_replies[int(prompt_msg_id)] = {
                                    "profile_id": v_parts[1],
                                    "channel_id": v_parts[2],
                                    "kind": v_parts[0],  # "reply" or "reply_img"
                                    "ts": time.time(),
                                }
                            self.on_log(f"[TG-MENU] pending reply 註冊 msg_id={prompt_msg_id} → {v_parts[1]}/{v_parts[2][-30:]}")
                except Exception as _e_reg:
                    self.on_log(f"[TG-MENU] pending reply 註冊失敗: {_e_reg}")
            else:
                self.tg.send_inline_keyboard(text, kb.get("inline_keyboard", []))
            # v6.0.83:如果剛拉了聊天記錄(buyer callback)→ 把媒體訊息拆出來逐個發 TG sendPhoto/sendVideo
            if data.startswith("fm:buyer:"):
                self._expand_history_media(chat_id, user_id)
            return True
        except Exception as e:
            self.on_log(f"[TG-MENU] callback 異常: {e}")
            return False

    def _dispatch_forum_reply_to_yahoo(
        self,
        entry: Dict[str, Any],
        text: str,
        photo_fid: str = "",
        video_fid: str = "",
    ) -> None:
        """force_reply 收到回覆 → send 給對應 Yahoo 對話(text / photo / video)。"""
        profile_id = entry.get("profile_id", "")
        channel_id = entry.get("channel_id", "")
        if not (profile_id and channel_id):
            self.tg.send("⚠️ pending reply 缺資料,無法 dispatch")
            return
        profile_dir = Path(self._base_dir) / "profiles" / profile_id
        if not profile_dir.exists():
            self.tg.send(f"⚠️ profile_dir 不存在: {profile_id}")
            return

        def _bg():
            try:
                # 從 chID 拿 my_id 後識別對方 Y-id 當 receiver
                from core.yahoo_im_jwt import fetch_im_user_info
                me_info, _ = fetch_im_user_info(profile_dir)
                my_id_lower = ((me_info or {}).get("id") or "").lower()
                buyer = ""
                for p in channel_id.split(":")[1:]:
                    if p and p.lower() != my_id_lower:
                        buyer = p
                        break
                if not buyer:
                    self.tg.send(f"⚠️ 無法從 {channel_id} 識別對方 Y-id")
                    return
                receiver = buyer.upper() if buyer.startswith("y") else buyer
                if not receiver.startswith("Y"):
                    receiver = "Y" + receiver

                shop_id = my_id_lower.upper() if my_id_lower else ""

                # 1. 媒體 reply — TG file_id → getFile → URL → 走 yahoo_im_media
                if photo_fid:
                    file_url = self._tg_get_file_url(photo_fid)
                    if not file_url:
                        self.tg.send("⚠️ 拿不到 TG 圖片 URL")
                        return
                    from core.yahoo_im_media import send_image_from_url
                    ok, info = send_image_from_url(
                        profile_dir,
                        shop_id=shop_id, buyer_id=receiver,
                        image_url=file_url, on_log=self.on_log,
                    )
                    if ok:
                        self.tg.send(f"✅ 圖片已發送給 {receiver}\n{info[:200]}")
                    else:
                        self.tg.send(f"❌ 圖片發送失敗\n receiver={receiver}\n {info[:300]}")
                    return

                if video_fid:
                    file_url = self._tg_get_file_url(video_fid)
                    if not file_url:
                        self.tg.send("⚠️ 拿不到 TG 視頻 URL")
                        return
                    # v6.1.53:用 autosplit 版,> 30 秒自動切分 ffmpeg 多段順序送
                    # Yahoo IM 視頻上限 30 秒,超過會被 server transcoding 拒絕
                    from core.yahoo_im_media import send_video_from_url_autosplit
                    ok, info = send_video_from_url_autosplit(
                        profile_dir,
                        shop_id=shop_id, buyer_id=receiver,
                        video_url=file_url, on_log=self.on_log,
                    )
                    if ok:
                        self.tg.send(f"✅ 視頻已發送給 {receiver}\n{info[:200]}")
                    else:
                        self.tg.send(f"❌ 視頻發送失敗\n receiver={receiver}\n {info[:300]}")
                    return

                # 2. 文字 reply
                if text:
                    from core.im_http_ops import im_send_message
                    ok, info = im_send_message(
                        profile_dir,
                        channel_id=channel_id,
                        receiver=receiver,
                        message=text,
                        on_log=self.on_log,
                    )
                    if ok:
                        self.tg.send(f"✅ 已發送給 {receiver}: {text[:80]}\n{info[:200]}")
                    else:
                        self.tg.send(f"❌ 發送失敗 (Yahoo IM)\n receiver={receiver}\n {info[:300]}")
                else:
                    self.tg.send("⚠️ 沒文字也沒媒體,不送")
            except Exception as e:
                self.tg.send(f"❌ dispatch 異常: {e}")
                self.on_log(f"[TG-MENU] dispatch_forum_reply 異常: {e}")
        threading.Thread(target=_bg, daemon=True).start()

    def _tg_get_file_url(self, file_id: str) -> str:
        """TG getFile API → 拿 file_path → 組 download URL。"""
        try:
            import requests as _req
            r = _req.post(
                f"https://api.telegram.org/bot{self.tg.token}/getFile",
                json={"file_id": file_id},
                timeout=10,
            )
            j = r.json()
            if not j.get("ok"):
                return ""
            fp = j.get("result", {}).get("file_path", "")
            if not fp:
                return ""
            return f"https://api.telegram.org/file/bot{self.tg.token}/{fp}"
        except Exception as e:
            self.on_log(f"[TG-MENU] getFile 失敗: {e}")
            return ""

    def _expand_history_media(self, chat_id: str, user_id: str) -> None:
        """把剛拉的聊天記錄內媒體訊息逐個發成 TG sendPhoto/sendVideo,讓用戶看實際內容。"""
        if not self._forum_menu:
            return
        parsed_map = getattr(self._forum_menu, "_last_history_parsed", {}) or {}
        parsed = parsed_map.get(user_id, [])
        if not parsed:
            return

        def _bg():
            import requests as _req
            base_url = f"https://api.telegram.org/bot{self.tg.token}"
            for p in parsed:
                t = p.get("type", "")
                url = p.get("media_url", "")
                if not url:
                    continue
                caption = f"{p['ts_str']} {p['role']}"
                try:
                    if t == "image" or t == "sticker":
                        _req.post(
                            f"{base_url}/sendPhoto",
                            json={"chat_id": chat_id, "photo": url, "caption": caption[:1024]},
                            timeout=20,
                        )
                    elif t == "video":
                        # 視頻可能很大,優先用縮圖顯示 + URL caption
                        thumb = p.get("thumb_url", "")
                        if thumb:
                            _req.post(
                                f"{base_url}/sendPhoto",
                                json={"chat_id": chat_id, "photo": thumb,
                                      "caption": f"{caption}\n🎬 視頻: {url}"[:1024]},
                                timeout=20,
                            )
                        else:
                            # 直接送 sendVideo 試
                            _req.post(
                                f"{base_url}/sendVideo",
                                json={"chat_id": chat_id, "video": url, "caption": caption[:1024]},
                                timeout=30,
                            )
                except Exception as e:
                    self.on_log(f"[TG-MENU] 展開媒體失敗 type={t}: {e}")
            # 清掉 cache 避免下次重複發
            parsed_map.pop(user_id, None)
        threading.Thread(target=_bg, daemon=True).start()

    def _dispatch_seller_msg_to_conv(self, conv: "ConversationState", msg) -> None:
        """分發單條賣家訊息到單個 conv,處理 dedupe/連發累積/AI 整合。"""

        # v6.0.78:跳過 baseline 之前的舊訊息(發送提問前的歷史對話,不該觸發整合)
        # 場景:剛發送提問 → HTTP 補漏拉 listUserMessages → 拿到對話歷史中的舊圖片/卡片 → 誤推
        msg_ts = getattr(msg, "created_ts", 0) or 0
        if (msg_ts and conv.seller_baseline_ts
                and msg_ts < conv.seller_baseline_ts):
            self.on_log(
                f"[TG] 跳過 baseline 之前的舊訊息 conv={conv.conv_id[:8]}: "
                f"msg_ts={msg_ts} < baseline_ts={conv.seller_baseline_ts} "
                f"text={(msg.content_text or '')[:40]!r}"
            )
            return

        # v6.0.79:跳過「自己發送的提問 echo」
        # WS server 會把我發送的訊息也廣播回給我(雖然 sender_uid 過濾應已擋下,
        # 但有些路徑 sender_uid 為空 / 格式不同會漏網)
        # 額外用「內容比對」雙保險:訊息文字 == 剛發送的提問 → 是 echo,跳過
        sent_q = (getattr(conv, "seller_sent_question", "") or "").strip()
        msg_text = (msg.content_text or "").strip()
        if sent_q and msg_text and msg_text == sent_q:
            self.on_log(
                f"[TG] 跳過自己提問的 echo conv={conv.conv_id[:8]}: text={msg_text[:40]!r}"
            )
            return

        # ── 訊息級 dedupe:同 message_id 已處理過 → 跳過(防 WS/HTTP 雙路雙重整合)──
        if msg.message_id:
            if msg.message_id in conv.seller_processed_msg_ids:
                return  # 已處理
            conv.seller_processed_msg_ids.add(msg.message_id)
            # 限制 set 大小,只保留最近 20 條
            if len(conv.seller_processed_msg_ids) > 20:
                conv.seller_processed_msg_ids = set(
                    list(conv.seller_processed_msg_ids)[-20:]
                )

        # ── 精確 AI 自動回覆判定(v6.1.51 提前到非文字檢查前)──
        # v6.0.75:賣家是 AI 自動回覆 → 不整合也不切手動,繼續監控等真人來
        # (避免「AI 互轟」+ 避免用戶要為每條 AI 自動回覆手動處理)
        # v6.1.51:bizTag.taskName 偵測,即使賣家用圖片/卡片做智能回覆也能 catch
        # 修「賣家智能回覆發圖片被誤判為一般非文字訊息切手動」bug
        if msg.is_auto_reply:
            self.on_log(f"[TG] WS 收到賣家 AI 自動回覆 conv={conv.conv_id[:8]}: {msg.content_text[:60]!r}(繼續監控等真人)")
            # 記錄一次 AI 回覆計數,避免反覆同樣的 TG 提示
            ai_count = (getattr(conv, 'seller_ai_count', 0) or 0) + 1
            conv.seller_ai_count = ai_count
            # 只在第一次 AI 回覆時提示用戶,後續同條對話內的 AI 回覆靜默
            if ai_count == 1:
                self._conv_aware_send(conv, 
                    f"🤖 闲鱼卖家暂不在,自动回复 [{conv.account_name} / {conv.buyer_label}]\n\n"
                    f"卖家(AI):「{msg.content_text[:200]}」\n\n"
                    f"⏳ 继续等卖家本人回复(WS 持续监控,无需操作)\n"
                    f"💡 若已等够,可点 [自己回] 或 reply:<内容> 手动处理"
                )
            return  # 不切 phase,不切 fallback,維持 AUTO_ASKING_SELLER 繼續等

        # ── 非文字訊息(WS 路徑解析失敗)→ 通知用戶手動處理 ──
        # v6.1.51:圖片/視頻/貼圖在 parse_inbound_message 已嘗試解析成 [圖片] URL 等文字
        # 此處若 content_text 還是空 → 真的是無法處理的訊息類型
        if not msg.content_text:
            self.on_log(
                f"[TG] WS 收到賣家非文字訊息: conv={conv.conv_id[:8]} "
                f"contentType={msg.content_type} msg_id={msg.message_id} platform={msg.platform}"
            )
            self._conv_aware_send(conv,
                f"📸 闲鱼卖家发送了非文字消息 [{conv.account_name} / {conv.buyer_label}]\n"
                f"(语音/位置/卡片等,contentType={msg.content_type})\n\n"
                f"请到 goofish.com 查看具体内容后:\n"
                f"  • reply:<给买家的回复>  ← 直接回买家\n"
                f"  • 或点 [自己回] 手动处理"
            )
            conv.auto_ask_fallback = True
            self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
            self._send_phase_buttons(
                conv,
                content=f"卖家发送了非文字消息(contentType={msg.content_type})",
            )
            return

        # ── 賣家連發累積:整合中或已進入 PREVIEW_SELLER 時,新訊息追加並 TG 通知 ──
        # 不直接觸發第二次整合(避免覆蓋用戶正在處理的草稿)
        if conv.seller_integrating or conv.phase == ConvPhase.PREVIEW_SELLER:
            conv.seller_extra_msgs.append(msg.content_text)
            extra_count = len(conv.seller_extra_msgs)
            self.on_log(
                f"[TG] 賣家補發第 {extra_count} 條訊息 conv={conv.conv_id[:8]}: "
                f"{msg.content_text[:60]!r}(累積中,不重複整合)"
            )
            self._conv_aware_send(conv,
                f"➕ 闲鱼卖家补发 [{conv.account_name} / {conv.buyer_label}]\n\n"
                f"卖家又说:「{msg.content_text[:200]}」\n\n"
                f"💡 已累積 {extra_count} 條補發訊息。可選擇:\n"
                f"  • 點 [🔄 更新整合] 重新合併所有訊息給 AI 生成新草稿\n"
                f"  • 或繼續用 ok / edit / reply 處理當前草稿"
            )
            # v6.1.51:PREVIEW_SELLER 階段重發按鈕以顯示新加的 [🔄 更新整合]
            try:
                if conv.phase == ConvPhase.PREVIEW_SELLER:
                    self._send_phase_buttons(
                        conv,
                        content=f"📥 賣家補發 — 待你決定:更新整合 OR 用目前草稿",
                    )
            except Exception:
                pass
            return

        # ── 真實賣家回覆 — v6.0.83 改成 debounce 累積後整合 ──
        # 賣家常分多句發(「最低 4800」+「免運費」+「等你下單」),
        # 過去收第一句就立即整合 → 草稿停在「沒辦法再低」,後面 4800/免運就漏掉。
        # 新邏輯:第一條訊息進 buffer 啟 8 秒 timer,期間又發就重置 timer,
        #         停打字 8 秒才合併整批 → AI 整合 → 出完整草稿。
        if conv.phase not in (ConvPhase.AUTO_ASKING_SELLER, ConvPhase.WAIT_SELLER):
            return  # 已切到其他狀態(如 PREVIEW_SELLER/DONE),走後面 PREVIEW_SELLER 補發累積邏輯

        # 取消 HTTP 輪詢 timer(WS 已即時推送了)
        if conv.seller_check_timer:
            try:
                conv.seller_check_timer.cancel()
            except Exception:
                pass
            conv.seller_check_timer = None

        SELLER_DEBOUNCE_SEC = 8.0  # 賣家停打字 N 秒才整合(實測 4-10s 賣家會連發完)

        # 入 buffer
        conv.seller_reply_buffer.append(msg.content_text)
        buf_count = len(conv.seller_reply_buffer)
        self.on_log(
            f"[TG] 賣家回覆 #{buf_count}(debounce 中) conv={conv.conv_id[:8]}: "
            f"{msg.content_text[:60]!r}"
        )

        # 取消舊 debounce timer(如有),重置
        if conv.seller_reply_debounce_timer:
            try:
                conv.seller_reply_debounce_timer.cancel()
            except Exception:
                pass
            conv.seller_reply_debounce_timer = None

        # 啟新 timer:N 秒後合併整批整合
        conv_id_capture = conv.conv_id  # 避免閉包持 conv ref 過久

        def _debounced_integrate():
            try:
                with self._lock:
                    _conv = self._convs.get(conv_id_capture)
                if not _conv:
                    return  # conv 已被清理
                # phase 已切到其他(用戶可能 skip / 已 DONE)→ 放棄整合
                if _conv.phase not in (ConvPhase.AUTO_ASKING_SELLER, ConvPhase.WAIT_SELLER):
                    self.on_log(
                        f"[TG] debounce 到期但 phase={_conv.phase.name},放棄整合 "
                        f"conv={conv_id_capture[:8]}"
                    )
                    _conv.seller_reply_buffer.clear()
                    _conv.seller_reply_debounce_timer = None
                    return

                buffered = list(_conv.seller_reply_buffer)
                _conv.seller_reply_buffer.clear()
                _conv.seller_reply_debounce_timer = None
                if not buffered:
                    return

                # 多條合併成一段:「最低 4800\n免運費\n等你下單」
                combined = "\n".join(buffered)
                _conv.seller_answer = combined
                self.on_log(
                    f"[TG] debounce 到期,整合 {len(buffered)} 條賣家訊息 "
                    f"conv={conv_id_capture[:8]}: {combined[:80]!r}"
                )
                _conv.seller_integrating = True
                try:
                    self._integrate_and_preview(_conv, combined)
                except Exception as e:
                    self.on_log(f"[TG] debounced _integrate_and_preview 異常: {e}")
                finally:
                    _conv.seller_integrating = False
            except Exception as _e:
                self.on_log(f"[TG] _debounced_integrate 異常: {_e}")

        timer = threading.Timer(SELLER_DEBOUNCE_SEC, _debounced_integrate)
        timer.daemon = True
        conv.seller_reply_debounce_timer = timer
        timer.start()

    def _on_ws_read_receipt(self, rr) -> None:
        """v6.0.75:WS 收到已讀回執 → 記錄到所有 active conv(同 cid 多 conv 廣播)。
        應用:可在 N 分鐘已讀但沒回時提示用戶「對方已讀沒回」(暫不實作)。
        """
        if not rr.cid:
            return
        with self._ws_map_lock:
            conv_ids = list(self._ws_cid_to_conv.get(rr.cid, []))
        if not conv_ids:
            return
        with self._lock:
            for cid in conv_ids:
                conv = self._convs.get(cid)
                if conv and conv.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED):
                    conv.seller_read_detected = True
        self.on_log(f"[TG] WS 已讀回執: cid={rr.cid} 廣播 {len(conv_ids)} convs read_count={len(rr.read_message_ids)}")

    def _trigger_all_active_xianyu_convs(self, reason: str) -> None:
        """v6.1:40006 cid 沒命中 dispatcher 時的 fallback。

        對所有 AUTO_ASKING_SELLER xianyu conv 各自觸發一次 _xianyu_check_via_list_messages,
        per-conv 5s throttle 防 spam(用 _ws_fetch_last_ts 共用同個 throttle 池,以 conv_id 為 key)。
        """
        try:
            with self._lock:
                candidates = [
                    c for c in self._convs.values()
                    if c.phase == ConvPhase.AUTO_ASKING_SELLER
                    and c.seller_session_id
                    and not c.auto_ask_fallback
                ]
            # 篩 xianyu 來源
            xy = []
            for c in candidates:
                src = ""
                if c.product_urls:
                    src = c.product_urls[0].get("source", "")
                if src == "xianyu":
                    xy.append(c)
            if not xy:
                return

            now_ts = time.time()
            triggered = 0
            with self._ws_fetch_throttle_lock:
                for c in xy:
                    key = f"conv:{c.conv_id}"
                    if now_ts - self._ws_fetch_last_ts.get(key, 0.0) < 5.0:
                        continue
                    self._ws_fetch_last_ts[key] = now_ts
                    triggered += 1
            if triggered == 0:
                return

            self.on_log(f"[TG] 40006-fallback {reason}: 觸發 {triggered}/{len(xy)} 個 conv check")

            # 拿 WS instance,在後台 thread 跑 check 避免阻塞 WS asyncio loop
            def _bg():
                try:
                    from core.goofish_ws_client import XianyuWsClient
                    from core.purchase_feature import PURCHASE_PROFILE_DIR
                    ws = XianyuWsClient.get_instance(
                        PURCHASE_PROFILE_DIR, on_log=self.on_log,
                    )
                    if not ws.is_connected():
                        return
                    for c in xy:
                        key = f"conv:{c.conv_id}"
                        # 再 check 一次 throttle(其他 thread 可能已 trigger)
                        if (now_ts - self._ws_fetch_last_ts.get(key, 0.0)) > 5.0:
                            continue  # 別人剛 trigger 過,不重複
                        try:
                            self._xianyu_check_via_list_messages(c, ws)
                        except Exception as _e:
                            self.on_log(f"[TG] 40006-fallback check {c.conv_id[:8]} 異常: {_e}")
                except Exception as e:
                    self.on_log(f"[TG] _trigger_all_active_xianyu_convs bg 異常: {e}")

            threading.Thread(target=_bg, daemon=True, name="40006-fallback").start()
        except Exception as e:
            self.on_log(f"[TG] _trigger_all_active_xianyu_convs 異常: {e}")

    def _on_ws_session_event(self, msg) -> None:
        """v6.0.75:WS 收到 40006 session event → 用 listUserMessages 拉訊息。

        多 conv 廣播:同 cid 對應多個 active conv(同閒魚商品被多買家問),
        listUserMessages 只拉一次,訊息透過 _on_ws_inbound_msg 廣播給每個 conv。

        限流保護:同 cid 5 秒內 throttle(server 對 typing/state 可能推多次 40006)。

        v6.1 修復:cid 提取失敗 / 不在 map 內時 fallback 觸發所有 AUTO_ASKING_SELLER xianyu conv 各自 check,
                  避免 server 推了 40006 但 parse_session_event regex 抓錯導致漏訊息
        """
        # v6.1:cid 解析失敗 → fallback 觸發所有 active xianyu AUTO_ASKING_SELLER conv 各自 check
        # (per-conv 5s throttle 防 spam)
        if not msg.cid:
            self.on_log(f"[TG] WS 40006 沒抓到 cid (raw={msg.raw_text[:80]!r}) → fallback 廣播 trigger")
            self._trigger_all_active_xianyu_convs("40006-no-cid")
            return

        # 同 cid 5s throttle(防 server 多次推 40006 觸發重複 fetch)
        now_ts = time.time()
        with self._ws_fetch_throttle_lock:
            last = self._ws_fetch_last_ts.get(msg.cid, 0.0)
            if now_ts - last < 5.0:
                # 5 秒內已 fetch 過,跳過(訊息級 dedupe 也會擋,這層是減少 HTTP 請求)
                return
            self._ws_fetch_last_ts[msg.cid] = now_ts
            # 順手清理太舊的 throttle 記錄(> 5 分鐘)
            if len(self._ws_fetch_last_ts) > 200:
                cutoff = now_ts - 300
                self._ws_fetch_last_ts = {
                    k: v for k, v in self._ws_fetch_last_ts.items() if v > cutoff
                }

        with self._ws_map_lock:
            conv_ids = list(self._ws_cid_to_conv.get(msg.cid, []))
        if not conv_ids:
            # v6.1:cid 不在 dispatcher map → 可能是別人對話的 40006,
            # 但也可能是 parse_session_event regex 抓到別的 ID(my_uid 帶 @goofish 之類)
            # → fallback 觸發所有 active xianyu AUTO_ASKING_SELLER conv check
            self.on_log(f"[TG] WS 40006 cid={msg.cid} 不在 dispatcher → fallback 廣播 trigger")
            self._trigger_all_active_xianyu_convs(f"40006-miss-{msg.cid}")
            return
        # 至少有一個 active conv 才拉訊息(避免無謂的 HTTP 請求)
        with self._lock:
            has_active = any(
                (c := self._convs.get(cid)) and c.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED)
                for cid in conv_ids
            )
        if not has_active:
            return

        self.on_log(f"[TG] WS session event 觸發 listUserMessages: cid={msg.cid} active_convs={len(conv_ids)}")

        def _bg_fetch():
            try:
                from core.goofish_ws_client import XianyuWsClient, parse_user_message_model
                from core.purchase_feature import PURCHASE_PROFILE_DIR
                ws = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=self.on_log)
                msgs, err = ws.list_user_messages_sync(msg.cid, limit=10, timeout=10)
                if err:
                    self.on_log(f"[TG] listUserMessages 失敗: {err},降級 HTTP 對每個 active conv 各跑")
                    # 降級:每個 active conv 各跑一次 HTTP 備援
                    with self._lock:
                        for _cid in conv_ids:
                            _c = self._convs.get(_cid)
                            if _c and _c.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED):
                                try:
                                    self._xianyu_check_http(_c)
                                except Exception:
                                    pass
                    return
                if not msgs:
                    return
                my_uid = ""
                try:
                    from core.xianyu_im_http import get_my_user_id
                    my_uid = get_my_user_id(PURCHASE_PROFILE_DIR)
                except Exception:
                    pass
                # msgs 順序:server 返回新→舊。倒序找最新「對方發」的訊息
                # v6.0.78:同時跳過 baseline 之前的訊息(broadcast 路徑用最早 baseline 過濾)
                # 同 cid 多 conv 時,取最早建立的 conv 的 baseline_ts 作為過濾線
                # (個別 conv 的 baseline 過濾在 _dispatch_seller_msg_to_conv 內二次把關)
                min_baseline_ts = 0
                try:
                    with self._lock:
                        bls = [
                            self._convs[_cid].seller_baseline_ts
                            for _cid in conv_ids
                            if _cid in self._convs
                            and self._convs[_cid].seller_baseline_ts
                        ]
                    if bls:
                        min_baseline_ts = min(bls)
                except Exception:
                    pass

                target_msg = None
                for item in msgs:
                    parsed = parse_user_message_model(item)
                    if not parsed:
                        continue
                    if parsed.sender_uid and my_uid and parsed.sender_uid == my_uid:
                        continue
                    # 跳過 baseline 之前的舊訊息
                    if (parsed.created_ts and min_baseline_ts
                            and parsed.created_ts < min_baseline_ts):
                        continue
                    target_msg = parsed
                    break

                if not target_msg:
                    self.on_log(f"[TG] listUserMessages 拿到 {len(msgs)} 條但沒對方新訊息")
                    return

                self.on_log(
                    f"[TG] listUserMessages 取得對方訊息: text={target_msg.content_text[:60]!r} "
                    f"ai={target_msg.is_auto_reply} qr={target_msg.quick_reply} "
                    f"msg_id={target_msg.message_id}"
                )

                # 餵給 _on_ws_inbound_msg 標準流程(內含廣播到所有 active conv + dedupe)
                target_msg.is_session_event = False
                target_msg.object_type = 40000
                self._on_ws_inbound_msg(target_msg)
            except Exception as e:
                self.on_log(f"[TG] _on_ws_session_event listUserMessages 異常: {e}")
                # 降級到 HTTP — 對每個 active conv 各跑一次
                try:
                    with self._lock:
                        for _cid in conv_ids:
                            _c = self._convs.get(_cid)
                            if _c and _c.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED):
                                try:
                                    self._xianyu_check_http(_c)
                                except Exception:
                                    pass
                except Exception:
                    pass

        threading.Thread(target=_bg_fetch, daemon=True).start()

    # ────── 翻譯模式 / cs callback ──────

    def _on_order_center_callback(self, data: str, chat_id: str, message_id: int) -> None:
        """v6.2:訂單中心 inline button callback dispatcher。

        data 格式:
          oc:filter:wp/wu/sh/od/all          — 過濾狀態
          oc:batch:todo_list                 — 列待出貨清單(帶 Yahoo 後台連結)
          oc:summary:today                   — 今日業績總結
          oc:det:{profile_id}:{order_id}     — v6.2:HTTP 抓訂單詳情 reply
          oc:buy:{profile_id}:{order_id}     — v6.2:找/建買家 topic + 推商品+貨源 + 回 deeplink
        """
        parts = data.split(":", 3)  # 最多切 3 次:oc / action / profile / rest
        if len(parts) < 2:
            return
        action = parts[1]
        arg = parts[2] if len(parts) > 2 else ""
        arg2 = parts[3] if len(parts) > 3 else ""

        # 後台 thread 跑(callback handler 不能 block 太久)
        import threading as _t

        def _bg():
            try:
                if action == "filter":
                    self._oc_filter_handler(arg, chat_id)
                elif action == "batch":
                    if arg == "todo_list":
                        self._oc_todo_list_handler(chat_id)
                elif action == "summary":
                    if arg == "today":
                        self._oc_today_summary_handler(chat_id)
                elif action == "det":
                    # arg=profile_id, arg2=order_id
                    self._oc_detail_handler(arg, arg2, chat_id, message_id)
                elif action == "buy":
                    # v6.2:arg=profile_id, arg2=order_id(handler 從訂單拿 buyer_id + 商品)
                    self._oc_buyer_handler(arg, arg2, chat_id, message_id)
                elif action == "relist":
                    # v6.1.20:訂單中心 → 一鍵轉刊提示
                    if arg == "hint":
                        self._oc_relist_hint_handler(chat_id)
            except Exception as e:
                self.on_log(f"[TG-FORUM] oc handler 異常: {e}")

        _t.Thread(target=_bg, daemon=True, name=f"oc-{action}").start()

    def _oc_get_target_accounts(self, chat_id: str) -> List[str]:
        """從 chat_id 推回該 group 看到的帳號列表。

        部署模型:每人一個軟件 instance,本 instance accounts.json 只有自己的帳號。
        - 本 instance forum group(預設 chat_id)→ accounts.json 全部
        - 同事 group(employees.json 有綁定)→ 該同事綁定的帳號(舊架構支援,通常用不到)
        - 陌生 group → [](防意外洩漏)
        """
        try:
            from .accounts import load_accounts
            all_accs = load_accounts() or []
            default_chat = ""
            if self.forum_bridge:
                default_chat = str(self.forum_bridge.bot.forum_chat_id)
            if str(chat_id) == default_chat or not chat_id:
                return [a["name"] for a in all_accs if a.get("monitor_selected", True)]
            # 同事 group(legacy 集中部署模式)→ 查 employees.json
            try:
                from .employees import find_employee_by_forum_chat_id
                owner = find_employee_by_forum_chat_id(str(chat_id))
                if owner:
                    return list(owner.get("accounts") or [])
            except Exception:
                pass
            # 陌生 group + 未綁定 → 返 [](防意外洩漏)
            return []
        except Exception:
            return []

    def _oc_fetch_all_orders(self, account_names: List[str]) -> Dict[str, List[Dict]]:
        """對 N 個帳號跑 fetch_orders,返回 {acc_name: [orders]}。"""
        from .order_http import fetch_orders
        from .accounts import load_accounts
        all_accs = load_accounts() or []
        result = {}
        for acc in account_names:
            profile_id = next(
                (a["profile_id"] for a in all_accs if a.get("name") == acc),
                acc,
            )
            pd = Path(self._base_dir) / "profiles" / profile_id
            if not pd.exists():
                continue
            try:
                orders, err = fetch_orders(pd)
                if not err and orders:
                    result[acc] = orders
            except Exception:
                pass
        return result

    # v6.2:快捷面板按鈕配置(訂單中心 pin 訊息 + 每次查詢結束都會推一份,避免用戶展開 pin)
    # v6.1.20:加 [📦 一鍵轉刊] 入口
    _QUICK_PANEL_BUTTONS = [
        [
            {"text": "🔴 待出貨", "callback_data": "oc:filter:wp"},
            {"text": "🟡 待付款", "callback_data": "oc:filter:wu"},
        ],
        [
            {"text": "🔵 已出貨", "callback_data": "oc:filter:sh"},
            {"text": "🚨 逾期", "callback_data": "oc:filter:od"},
        ],
        [
            {"text": "📋 待處理清單", "callback_data": "oc:batch:todo_list"},
            {"text": "📊 今日業績", "callback_data": "oc:summary:today"},
        ],
        [
            {"text": "📦 一鍵轉刊(小→大賣場)", "callback_data": "oc:relist:hint"},
        ],
    ]

    def _oc_send_quick_panel(self, chat_id: str, topic_id: int) -> None:
        """v6.2:訊息流尾部推一份快捷面板按鈕,讓用戶看完訂單後一鍵切換查詢。

        避免「pin 折疊看不到按鈕 / 每次都要打 /orders」的麻煩。
        """
        if not self.forum_bridge:
            return
        try:
            payload = {
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "text": "🔁 <b>快捷操作</b> · 點按鈕直接查詢",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": self._QUICK_PANEL_BUTTONS},
            }
            self.forum_bridge.bot._post("sendMessage", payload)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_send_quick_panel 異常: {e}")

    def _oc_send_reply(self, chat_id: str, topic_id: int, html_text: str,
                       buttons: Optional[list] = None) -> None:
        """送結果到訂單中心 topic(reply 不主卡,獨立訊息)。

        v6.1:用 _safe_html_truncate 避免在 <tag> 中間切壞 HTML parse。
        v6.2:加 buttons 支援 inline_keyboard。
        """
        if not self.forum_bridge:
            return
        try:
            from .order_http import _safe_html_truncate
            payload = {
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "text": _safe_html_truncate(html_text, 4000),
                "parse_mode": "HTML",
            }
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            self.forum_bridge.bot._post("sendMessage", payload)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_send_reply 異常: {e}")

    def _oc_chat_owns_profile(self, chat_id: str, profile_id: str) -> bool:
        """v6.2:防意外跨 group 查詢。

        部署模型:每人一個軟件 instance,本 instance accounts.json 只有自己的帳號。
        - 本 instance forum group(預設 forum_chat_id)→ 允許查 accounts.json 內的 profile
        - 陌生 group → 拒絕(防意外被加進別的 group 後資料外洩)
        """
        if not self.forum_bridge:
            return False
        try:
            supervisor_chat = str(self.forum_bridge.bot.forum_chat_id)
            if str(chat_id) == supervisor_chat:
                # 本 instance 的 group:確認該 profile_id 確實在自己 accounts.json
                try:
                    from .accounts import load_accounts
                    own = {a.get("profile_id") for a in (load_accounts() or [])}
                    return profile_id in own
                except Exception:
                    return True  # fallback:讀檔失敗時放行(本 instance group 默認可信)
            # 其他 group(legacy 同事 group 場景)→ 用 owner 路由驗證
            expected_chat = self.forum_bridge._resolve_chat_id_for_profile(profile_id)
            return str(chat_id) == str(expected_chat)
        except Exception:
            return False

    def _oc_detail_handler(self, profile_id: str, order_id: str, chat_id: str, message_id: int) -> None:
        """v6.2:🧾 詳情按鈕 → HTTP 抓最新訂單詳情,reply 到訂單中心 topic。

        替代舊版「Yahoo 後台 URL 跳轉」(中間商沒對應 Chrome profile 開不了)。
        """
        if not self.forum_bridge or not profile_id or not order_id:
            return
        from .order_http import fetch_orders, format_order_for_tg, _esc, _safe_html_truncate
        # v6.2:multi-tenant 防護 — 拒絕跨 group 查詢
        if not self._oc_chat_owns_profile(chat_id, profile_id):
            self.on_log(f"[TG-FORUM] ⚠️ {chat_id} 嘗試查不屬於自己的 profile {profile_id}")
            self._oc_send_reply_to_msg(chat_id, message_id,
                f"⚠️ 此 group 不擁有帳號 <code>{_esc(profile_id)}</code>")
            return
        pd = Path(self._base_dir) / "profiles" / profile_id
        if not pd.exists():
            self._oc_send_reply_to_msg(chat_id, message_id,
                f"❌ profile <code>{_esc(profile_id)}</code> 不存在")
            return
        try:
            orders, err = fetch_orders(pd)
            if err:
                self._oc_send_reply_to_msg(chat_id, message_id, f"❌ 拉訂單失敗: {_esc(err)}")
                return
            target = next((o for o in orders if (o.get("order_id") or "") == order_id), None)
            if not target:
                self._oc_send_reply_to_msg(chat_id, message_id,
                    f"⚠️ 訂單 <code>{_esc(order_id)}</code> 不在最新列表(可能已歸檔/取消)")
                return
            # 完整版顯示(含進度條)
            html = format_order_for_tg(target, with_progress=True, age_seconds=0)
            # 附加原始 Yahoo 後台連結(可選,給有 profile 的用戶)
            detail_url = target.get("detail_url", "")
            if detail_url:
                html += f'\n\n<i>Yahoo 後台原始連結(需登入該帳號 Chrome):</i>\n{_esc(detail_url)}'
            self._oc_send_reply_to_msg(chat_id, message_id, html)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_detail_handler 異常: {e}")

    def _oc_buyer_handler(self, profile_id: str, order_id: str, chat_id: str, message_id: int) -> None:
        """v6.2:💬 聯繫買家按鈕 → 找/建該買家 forum topic + 推訂單商品上下文 + 回 deeplink。

        流程:
        1. fetch_orders 拿訂單 → buyer_id + items(Yahoo 商品 URL)
        2. ensure_topic 建/找該買家 topic
        3. 主動往 topic 推一條商品上下文訊息(訂單號 / 金額 / 商品列表 / Yahoo 鏈接 / D1 貨源鏈接)
        4. 回原訊息 deeplink 按鈕讓用戶跳轉
        """
        if not self.forum_bridge or not profile_id or not order_id:
            return
        from .order_http import _esc, fetch_orders
        # v6.2:multi-tenant 防護 — 拒絕跨 group 查詢
        if not self._oc_chat_owns_profile(chat_id, profile_id):
            self.on_log(f"[TG-FORUM] ⚠️ {chat_id} 嘗試聯繫不屬於自己的 profile {profile_id}")
            self._oc_send_reply_to_msg(chat_id, message_id,
                f"⚠️ 此 group 不擁有帳號 <code>{_esc(profile_id)}</code>")
            return
        try:
            # 1. fetch_orders 拿訂單詳情
            pd = Path(self._base_dir) / "profiles" / profile_id
            if not pd.exists():
                self._oc_send_reply_to_msg(chat_id, message_id,
                    f"❌ profile <code>{_esc(profile_id)}</code> 不存在")
                return
            orders, err = fetch_orders(pd)
            if err:
                self._oc_send_reply_to_msg(chat_id, message_id,
                    f"❌ 拉訂單失敗: {_esc(err)}")
                return
            target = next((o for o in orders if (o.get("order_id") or "") == order_id), None)
            if not target:
                self._oc_send_reply_to_msg(chat_id, message_id,
                    f"⚠️ 訂單 <code>{_esc(order_id)}</code> 不在最新列表")
                return

            buyer_id = target.get("buyer_id") or ""
            buyer_name = target.get("buyer_name") or buyer_id
            amount = target.get("amount", 0)
            status_lbl = target.get("status_label", "")
            items = target.get("items") or []

            # 2. 找 account name
            account_name = profile_id
            try:
                from .accounts import load_accounts
                for a in load_accounts() or []:
                    if a.get("profile_id") == profile_id:
                        account_name = a.get("name") or profile_id
                        break
            except Exception:
                pass

            # 3. 找/建 topic
            topic_id, err = self.forum_bridge.ensure_topic(
                profile_id=profile_id,
                yahoo_chat_id=buyer_id,
                buyer_label=buyer_name or buyer_id,
                account_name=account_name,
            )
            if err or not topic_id:
                self._oc_send_reply_to_msg(chat_id, message_id,
                    f"❌ 找/建買家 topic 失敗: {_esc(err or 'unknown')}")
                return

            # v6.1.27:記下 order_id 到 store entry,後續 BOSH 兜底用來 attach 訂單卡片
            # (BOSH 對全新 channel 必須附 order 才會真正送達 buyer)
            try:
                _conv_key_store = self.forum_bridge._conv_key(profile_id, buyer_id)
                self.forum_bridge.store.set_last_order_id(_conv_key_store, order_id)
            except Exception:
                pass

            # 4. 生成完整商品上下文(D1 查一次)— push 到買家 topic 留底
            target_chat = self.forum_bridge._resolve_chat_id_for_profile(profile_id)
            tc = target_chat[4:] if target_chat.startswith("-100") else target_chat
            jump_url = f"https://t.me/c/{tc}/{topic_id}"
            context_html = self._build_order_context_html(
                target, account_name,
                title_prefix="📦 <b>本次訂單商品資訊</b>",
            )

            # 5a. push 商品上下文到買家 topic(歷史記錄)
            try:
                payload = {
                    "chat_id": target_chat,
                    "message_thread_id": topic_id,
                    "text": context_html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                self.forum_bridge.bot._post("sendMessage", payload)
            except Exception as _pe:
                self.on_log(f"[TG-FORUM] push topic context 異常: {_pe}")

            # 5b. v6.1.27:不需 prime channel — forum dispatch 已改成 BOSH send_message,
            # 對全新 channel 也能 work(server 自動建 channel)

            # 5c. v6.1.27:回原訊息極簡(資訊已在 topic 內,使用者點按鈕直接跳)
            # 移除大段 context_html,只保留跳轉按鈕 → 一鍵體驗
            reply_html = (
                f"✓ 已準備好 <b>{_esc(buyer_name or buyer_id)}</b> 的對話 topic\n"
                f"<i>商品資訊已 push 到 topic 內,點下方按鈕直接跳過去打字</i>"
            )
            buttons = [[{"text": f"💬 → 跳到買家 topic", "url": jump_url}]]
            self._oc_send_reply_to_msg(chat_id, message_id, reply_html, buttons=buttons)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_buyer_handler 異常: {e}")

    def _prime_yahoo_channel(self, profile_id: str, buyer_id: str) -> None:
        """v6.1.27:對「從沒對話過」的買家先 BOSH channel_user_active 啟動 channel.

        訂單中心場景下,賣家從訂單系統主動找買家,該買家在 Yahoo IM 沒對應 channel.
        直接 send_message 會 404 (Channel not found).
        先 channel_user_active(isActive=true) → Yahoo 建立 channel → 之後 send 就 OK.
        """
        if not profile_id or not buyer_id:
            return
        profile_dir = Path(self._base_dir) / "profiles" / profile_id
        if not profile_dir.exists():
            return
        try:
            from .yahoo_im_jwt import ensure_bosh_jwt
            from .yahoo_im_bosh_ext import BOSHSession
            from .im_http_ops import build_channel_id

            # 從 JWT 拿 my_id (shop Y-ID)
            _, user, err = ensure_bosh_jwt(profile_dir, on_log=self.on_log)
            if not user:
                self.on_log(f"[TG-FORUM] prime channel: 拿不到 my_id ({err})")
                return
            my_id = user.upper() if not user.startswith("Y") else user

            # buyer_y 加 Y 前綴(如果還沒有)
            buyer_y = buyer_id if buyer_id.upper().startswith("Y") else f"Y{buyer_id}"
            channel = build_channel_id(my_id, buyer_y)

            with BOSHSession(profile_dir, on_log=self.on_log) as s:
                _, err = s.channel_user_active(channel, is_active=True)
                if err:
                    self.on_log(f"[TG-FORUM] prime channel {channel[-30:]} 失敗: {err}")
                else:
                    self.on_log(f"[TG-FORUM] prime channel {channel[-30:]} OK")
                # 順便 mark_read 確保 channel 完全活化
                try:
                    s.mark_read(channel)
                except Exception:
                    pass
        except Exception as e:
            self.on_log(f"[TG-FORUM] _prime_yahoo_channel 異常: {e}")

    def _build_order_context_html(self, order: Dict[str, Any], account_name: str,
                                   title_prefix: str = "📦 <b>本次訂單商品資訊</b>") -> str:
        """v6.2:生成訂單 + 商品上下文 HTML(含 Yahoo URL + D1 貨源鏈接)。

        兩處共用:
        - _push_order_context_to_topic — 推到買家 topic
        - _oc_buyer_handler 回原訊息 — 訂單中心 reply
        """
        from .order_http import _esc
        oid = order.get("order_id", "")
        amount = order.get("amount", 0)
        status_lbl = order.get("status_label", "")
        buyer_id = order.get("buyer_id", "")
        buyer_name = order.get("buyer_name", "") or buyer_id
        items = order.get("items") or []

        lines = [
            title_prefix,
            f"━━━━━━━━━━━━━━━━━━━",
            f"訂單 <code>#{_esc(oid)}</code> · NT${amount} · 📍 {_esc(status_lbl)}",
            f"帳號 <code>{_esc(account_name)}</code> · 買家 <code>{_esc(buyer_name)}</code>",
            "",
        ]
        for i, it in enumerate(items, 1):
            title = it.get("title", "")
            qty = it.get("quantity", 1)
            up = it.get("unit_price", 0)
            yahoo_url = it.get("url", "")
            # 從 Yahoo URL 抽 item_code(11-12 位數字)
            item_code = ""
            if yahoo_url:
                import re as _re
                m = _re.search(r"/item/(\d{10,12})", yahoo_url)
                if m:
                    item_code = m.group(1)
            lines.append(f"<b>{i}. {_esc(title)}</b>")
            lines.append(f"   數量 {qty} · 單價 NT${up}")
            if yahoo_url:
                lines.append(f'   🟡 <a href="{yahoo_url}">Yahoo 拍賣商品頁</a>')
            # D1 查貨源
            if item_code:
                try:
                    d1_data = _query_product_d1(item_code, on_log=self.on_log)
                    if d1_data:
                        barcode = d1_data.get("barcode", "")
                        src, src_url = _classify_source(barcode)
                        if src == "xianyu":
                            disp_url = _to_mobile_xianyu_url(src_url)
                            lines.append(f'   🔶 <a href="{_esc(disp_url)}">閒魚貨源</a>')
                        elif src == "mercari":
                            lines.append(f'   🔶 <a href="{_esc(src_url)}">煤炉貨源</a>')
                        pc = d1_data.get("product_code", "")
                        if pc:
                            lines.append(f"   <i>商品編碼 {_esc(pc)}</i>")
                    else:
                        lines.append(f"   <i>(D1 無此商品編碼貨源記錄)</i>")
                except Exception as _d1e:
                    self.on_log(f"[TG-FORUM] D1 查 {item_code} 異常: {_d1e}")
            lines.append("")
        return "\n".join(lines)[:4000]

    def _push_order_context_to_topic(self, target_chat: str, topic_id: int,
                                      order: Dict[str, Any], account_name: str) -> None:
        """v6.2:推送訂單商品上下文到指定 topic(讓用戶看到「這買家買了什麼」)。"""
        if not self.forum_bridge:
            return
        try:
            text = self._build_order_context_html(order, account_name)
            payload = {
                "chat_id": target_chat,
                "message_thread_id": topic_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            self.forum_bridge.bot._post("sendMessage", payload)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _push_order_context_to_topic 異常: {e}")

    def _oc_send_reply_to_msg(self, chat_id: str, message_id: int, html_text: str,
                              buttons: Optional[list] = None) -> None:
        """v6.2:reply 到特定 message_id(用戶按 button 的那條主卡),含 inline keyboard 支援。"""
        if not self.forum_bridge:
            return
        try:
            from .order_http import _safe_html_truncate
            payload = {
                "chat_id": chat_id,
                "text": _safe_html_truncate(html_text, 4000),
                "parse_mode": "HTML",
                "reply_to_message_id": message_id,
                # v6.2:商品 URL 多時不展開預覽,訊息更乾淨
                "disable_web_page_preview": True,
            }
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            self.forum_bridge.bot._post("sendMessage", payload)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_send_reply_to_msg 異常: {e}")

    def _oc_filter_handler(self, filter_type: str, chat_id: str) -> None:
        """過濾 wp/wu/sh/od/all → 列出符合條件的訂單。"""
        if not self.forum_bridge:
            return
        topic_id = self.forum_bridge.store.get_order_center_topic(str(chat_id)) or 0
        if not topic_id:
            return

        # v6.1:用 classify_order 統一過濾邏輯(修 buyerPickup + refund + buyerCancel)
        from .order_http import classify_order
        def _cls(o):
            return classify_order(
                o.get("status", ""), o.get("payment_status", ""),
                o.get("status_label", ""), o.get("status_extra", ""),
            )
        FILTERS = {
            "wp": ("🔴 待出貨", lambda o: _cls(o) == "waiting_paid"),
            "wu": ("🟡 待付款", lambda o: _cls(o) == "waiting_unpaid"),
            "sh": ("🔵 已出貨等收貨", lambda o: _cls(o) in ("shipped", "picked_up")),
            "od": ("🚨 逾期", lambda o: _cls(o) == "overdue"),
            "all": ("📋 所有訂單(不含已關閉)", lambda o: _cls(o) not in ("canceled", "refunded", "completed", "unknown")),
        }
        if filter_type not in FILTERS:
            return
        label, predicate = FILTERS[filter_type]

        accs = self._oc_get_target_accounts(chat_id)
        if not accs:
            self._oc_send_reply(chat_id, topic_id, f"⚠️ 沒有可查的帳號")
            return

        # 先回 loading
        self._oc_send_reply(chat_id, topic_id, f"⏳ 查詢 {label} 中... ({len(accs)} 個帳號)")

        # fetch + filter
        from .order_http import _esc
        from .accounts import load_accounts as _la_acc
        _profile_lookup = {a.get("name"): a.get("profile_id", a.get("name"))
                           for a in (_la_acc() or [])}
        all_orders = self._oc_fetch_all_orders(accs)
        # 收集所有匹配訂單,先發總覽,再逐筆獨立卡片
        matched_all: List[Tuple[str, Dict[str, Any]]] = []  # [(acc, order), ...]
        for acc, orders in all_orders.items():
            for o in orders:
                if predicate(o):
                    matched_all.append((acc, o))
        total = len(matched_all)

        if total == 0:
            self._oc_send_reply(chat_id, topic_id, f"📋 {label} 查詢結果:<b>0 筆</b>")
            return

        # 先發總覽
        self._oc_send_reply(
            chat_id, topic_id,
            f"📋 {label} 共 <b>{total}</b> 筆\n📝 下方逐筆顯示 · 按 <b>💬 聯繫買家</b> 直接跳對話",
        )

        # 逐筆訂單獨立卡片 + 聯繫買家按鈕(防洗版 cap 20 筆)
        MAX_SEND = 20
        for acc, o in matched_all[:MAX_SEND]:
            pid = _profile_lookup.get(acc, acc)
            oid = (o.get("order_id", "") or "")
            amount = o.get("amount", 0)
            status_lbl = o.get("status_label", "")
            buyer_name = o.get("buyer_name", "")
            buyer_id = o.get("buyer_id", "")
            buyer_show = buyer_name or buyer_id or "?"
            items = o.get("items") or []
            title = (items[0].get("title", "") if items else "")
            ship_id = items[0].get("shipping_id", "") if items else ""
            text_lines = [
                f"📦 <b>訂單 #{_esc(oid)}</b>",
                f"💰 NT${amount}  ·  📍 {_esc(status_lbl)}",
                f"👤 帳號 <code>{_esc(acc)}</code>  ·  買家 <code>{_esc(buyer_show)}</code>",
            ]
            if title:
                text_lines.append(f"🎁 {_esc(title)}")
            if ship_id:
                text_lines.append(f"🚚 單號 <code>{_esc(ship_id)}</code>")
            # v6.2:聯繫買家用 order_id
            cb = f"oc:buy:{pid}:{oid}"
            btns = None
            if oid and buyer_id and len(cb.encode("utf-8")) <= 64:
                btns = [[{
                    "text": f"💬 聯繫買家 {buyer_show}",
                    "callback_data": cb,
                }]]
            self._oc_send_reply(chat_id, topic_id, "\n".join(text_lines), buttons=btns)

        if total > MAX_SEND:
            self._oc_send_reply(
                chat_id, topic_id,
                f"<i>...還有 <b>{total - MAX_SEND}</b> 筆未顯示</i>",
            )
        # v6.2:訊息流尾部推快捷面板,讓用戶一鍵切換查詢(不用展開 pin / 打 /orders)
        self._oc_send_quick_panel(chat_id, topic_id)

    def _oc_todo_list_handler(self, chat_id: str) -> None:
        """列待出貨清單 + Yahoo 後台跳轉連結(批量操作入口)。"""
        if not self.forum_bridge:
            return
        topic_id = self.forum_bridge.store.get_order_center_topic(str(chat_id)) or 0
        if not topic_id:
            return

        accs = self._oc_get_target_accounts(chat_id)
        self._oc_send_reply(chat_id, topic_id, f"⏳ 查詢待處理清單中...")

        from .order_http import _esc, classify_order
        from .accounts import load_accounts as _la_acc
        _profile_lookup = {a.get("name"): a.get("profile_id", a.get("name"))
                           for a in (_la_acc() or [])}
        all_orders = self._oc_fetch_all_orders(accs)
        # v6.2:收集所有待處理訂單,先發總覽 + 逐筆獨立卡片(含聯繫買家按鈕)
        wp_all: List[Tuple[str, Dict[str, Any]]] = []
        for acc, orders in all_orders.items():
            for o in orders:
                if classify_order(o.get("status",""), o.get("payment_status",""),
                                  o.get("status_label",""), o.get("status_extra","")) == "waiting_paid":
                    wp_all.append((acc, o))
        total = len(wp_all)

        if total == 0:
            self._oc_send_reply(chat_id, topic_id, "🎉 <b>目前沒有待處理訂單!</b>")
            return

        # 總覽
        yahoo_url = "https://tw.bid.yahoo.com/myauc?sellerTab=generalOrder"
        self._oc_send_reply(
            chat_id, topic_id,
            f"📋 <b>待處理清單(已付款待出貨)</b>\n"
            f"共 <b>{total}</b> 筆 · 已自動排除已退款/已出貨\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f'💡 <a href="{yahoo_url}">Yahoo 後台批量操作(列印物流單/標記出貨)</a>\n'
            f"📝 下方逐筆顯示 · 按 <b>💬 聯繫買家</b> 直接跳對話",
        )

        # 逐筆訂單獨立卡片
        MAX_SEND = 20
        for acc, o in wp_all[:MAX_SEND]:
            pid = _profile_lookup.get(acc, acc)
            oid = (o.get("order_id", "") or "")
            amount = o.get("amount", 0)
            buyer_name = o.get("buyer_name", "")
            buyer_id = o.get("buyer_id", "")
            buyer_show = buyer_name or buyer_id or "?"
            items = o.get("items") or []
            title = (items[0].get("title", "") if items else "")
            text_lines = [
                f"📦 <b>訂單 #{_esc(oid)}</b>",
                f"💰 NT${amount}",
                f"👤 帳號 <code>{_esc(acc)}</code>  ·  買家 <code>{_esc(buyer_show)}</code>",
            ]
            if title:
                text_lines.append(f"🎁 {_esc(title)}")
            # v6.2:聯繫買家用 order_id
            cb = f"oc:buy:{pid}:{oid}"
            btns = None
            if oid and buyer_id and len(cb.encode("utf-8")) <= 64:
                btns = [[{
                    "text": f"💬 聯繫買家 {buyer_show}",
                    "callback_data": cb,
                }]]
            self._oc_send_reply(chat_id, topic_id, "\n".join(text_lines), buttons=btns)

        if total > MAX_SEND:
            self._oc_send_reply(
                chat_id, topic_id,
                f"<i>...還有 <b>{total - MAX_SEND}</b> 筆未顯示</i>",
            )
        # v6.2:訊息流尾部推快捷面板
        self._oc_send_quick_panel(chat_id, topic_id)

    def _oc_today_summary_handler(self, chat_id: str) -> None:
        """今日業績總結(統計當日的訂單變化)。"""
        if not self.forum_bridge:
            return
        topic_id = self.forum_bridge.store.get_order_center_topic(str(chat_id)) or 0
        if not topic_id:
            return

        accs = self._oc_get_target_accounts(chat_id)
        self._oc_send_reply(chat_id, topic_id, f"⏳ 統計今日業績中...")

        from .order_http import _esc
        from datetime import datetime, timezone, timedelta
        TW_TZ = timezone(timedelta(hours=8))
        today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        today_start = datetime.now(TW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

        # v6.1:用 classify_order 統一邏輯
        from .order_http import classify_order
        all_orders = self._oc_fetch_all_orders(accs)
        today_shipped = 0
        today_shipped_amount = 0
        today_completed = 0
        current_waiting_paid = 0
        current_waiting_unpaid = 0
        current_shipped_pending = 0

        for acc, orders in all_orders.items():
            for o in orders:
                status = o.get("status", "")
                payment = o.get("payment_status", "")
                items = o.get("items") or []
                amount = o.get("amount", 0)

                # 今日出貨統計
                deliver_dt = items[0].get("deliver_datetime", "") if items else ""
                if deliver_dt:
                    try:
                        _dt = datetime.fromisoformat(deliver_dt.replace("Z", "+00:00"))
                        if _dt.tzinfo is None:
                            _dt = _dt.replace(tzinfo=TW_TZ)
                        _dt_tw = _dt.astimezone(TW_TZ)
                        if _dt_tw >= today_start:
                            today_shipped += 1
                            today_shipped_amount += amount
                    except Exception:
                        pass

                cls = classify_order(
                    status, payment,
                    o.get("status_label", ""), o.get("status_extra", ""),
                )
                if cls == "completed":
                    today_completed += 1
                elif cls == "waiting_paid":
                    current_waiting_paid += 1
                elif cls == "waiting_unpaid":
                    current_waiting_unpaid += 1
                elif cls in ("shipped", "picked_up"):
                    current_shipped_pending += 1

        # 平均出貨時長計算(從 status_extra 內的「已於 X 出貨」+ 對應付款時間,簡化版用「24h 內出貨率」)
        # 簡單版:不算精確平均,只給「今日總出貨」
        lines = [
            f"📊 <b>今日業績總結</b>",
            f"<i>{today_str} · {len(accs)} 個帳號</i>",
            "━━━━━━━━━━━━━━━━━━━",
            f"<b>今日完成事項:</b>",
            f"  ✅ 已出貨: <b>{today_shipped}</b> 筆 ({today_shipped_amount:,} NT$)",
            "",
            f"<b>當前未結案:</b>",
            f"  🔴 待出貨(已付款,要處理): <b>{current_waiting_paid}</b> 筆",
            f"  🟡 待付款(等買家): {current_waiting_unpaid} 筆",
            f"  🔵 已出貨等收貨: {current_shipped_pending} 筆",
            "━━━━━━━━━━━━━━━━━━━",
        ]
        if current_waiting_paid > 10:
            lines.append("⚠️ <b>待出貨積壓較多,建議優先處理</b>")
        elif current_waiting_paid == 0:
            lines.append("🎉 沒有待處理訂單,辛苦了!")
        self._oc_send_reply(chat_id, topic_id, "\n".join(lines))
        # v6.2:訊息流尾部推快捷面板
        self._oc_send_quick_panel(chat_id, topic_id)

    def _oc_relist_hint_handler(self, chat_id: str) -> None:
        """訂單中心 [📦 一鍵轉刊] 按鈕 → 推使用說明。

        使用者點按鈕後,在訂單中心 topic 內回覆 /relist <URL> 啟動。
        """
        if not self.forum_bridge:
            return
        topic_id = self.forum_bridge.store.get_order_center_topic(str(chat_id)) or 0
        if not topic_id:
            return
        text = (
            "📦 <b>一鍵轉刊使用方式</b>\n"
            "━━━━━━━━━━━━━━\n"
            "1. 複製你小賣場(或任何 Yahoo 商品)的連結\n"
            "2. 在<b>任意 topic</b>(包含本訂單中心)發:\n"
            "   <code>/relist 連結</code>\n\n"
            "<b>例:</b>\n"
            "<code>/relist https://tw.bid.yahoo.com/item/101739354873</code>\n\n"
            "Bot 會自動:\n"
            "✅ 拉商品資料(標題/圖/價/分類)\n"
            "✅ 讓你選目標帳號\n"
            "✅ 任何欄位都可改(標題/價/數量/圖/描述/標籤...)\n"
            "✅ 確認後刊登到目標帳號\n\n"
            "<i>(來源跟目標都要是你自己的帳號,1 次 HTTP 拉公開頁無需來源帳號 cookies)</i>"
        )
        try:
            payload = {
                "chat_id": str(chat_id),
                "message_thread_id": topic_id,
                "text": text[:4096],
                "parse_mode": "HTML",
            }
            self.forum_bridge.bot._post("sendMessage", payload)
        except Exception as e:
            self.on_log(f"[TG-FORUM] _oc_relist_hint_handler 發送異常: {e}")

    # ─────────────────────────────────────────────────────
    # v6.1.20:一鍵轉刊(/relist)— TG state machine
    # ─────────────────────────────────────────────────────

    def _relist_get_session(self, chat_id: str):
        """取當前 chat 的 RelistSession,沒就 None。同時清過期(>1h)。"""
        with self._relist_sessions_lock:
            sess = self._relist_sessions.get(chat_id)
            if sess and sess.is_expired():
                self._relist_sessions.pop(chat_id, None)
                return None
            return sess

    def _relist_set_session(self, chat_id: str, sess) -> None:
        with self._relist_sessions_lock:
            self._relist_sessions[chat_id] = sess

    def _relist_clear_session(self, chat_id: str) -> None:
        with self._relist_sessions_lock:
            self._relist_sessions.pop(chat_id, None)

    def _relist_tg_send(
        self, session, text: str, buttons=None, edit_msg_id: int = 0,
    ) -> int:
        """在 session 所在 chat/topic 發訊息(支援 forum + 私聊)。"""
        try:
            # Forum 模式
            if session.topic_id and self.forum_bridge:
                payload = {
                    "chat_id": session.chat_id,
                    "message_thread_id": int(session.topic_id),
                    "text": text[:4096],
                    "parse_mode": "HTML",
                }
                if buttons:
                    payload["reply_markup"] = {"inline_keyboard": buttons}
                if edit_msg_id:
                    payload["message_id"] = int(edit_msg_id)
                    r, e = self.forum_bridge.bot._post("editMessageText", payload)
                else:
                    r, e = self.forum_bridge.bot._post("sendMessage", payload)
                if r and not e:
                    return int(r.get("message_id") or 0)
                return 0
            # 私聊
            if buttons:
                return int(self.tg.send_inline_keyboard(text, buttons, session.chat_id) or 0)
            else:
                return int(self.tg.send(text, session.chat_id) or 0)
        except Exception as e:
            self.on_log(f"[RELIST] _relist_tg_send 異常: {e}")
            return 0

    def _relist_load_accounts(self) -> list:
        """讀 accounts.json,返回 list of dicts。"""
        try:
            from .accounts import load_accounts
            return load_accounts() or []
        except Exception as e:
            self.on_log(f"[RELIST] load_accounts 異常: {e}")
            return []

    def _relist_account_picker_buttons(self) -> list:
        """產生目標帳號 inline button(每行 3 個)。"""
        accs = self._relist_load_accounts()
        buttons: list = []
        row: list = []
        for a in accs:
            name = a.get("name") or a.get("profile_id") or ""
            if not name:
                continue
            # 帳號 name 可能含 @gmail.com,callback_data 不能太長(64 byte 上限),用 profile_id 短
            pid = a.get("profile_id") or name
            disp = name[:18] if len(name) > 18 else name
            row.append({"text": disp, "callback_data": f"rl:pt:{pid}"})
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([{"text": "❌ 取消", "callback_data": "rl:cancel"}])
        return buttons

    def _relist_format_preview(self, session) -> str:
        """格式化 source + edits 預覽。"""
        s = session
        get = s.get_field
        title = get("title", "") or ""
        price = get("price", 0)
        cat_id = get("catId", "") or ""
        cond_raw = str(get("condition", "1") or "1")
        cond_label = {
            "1": "全新", "2": "近全新", "3": "二手", "4": "二手有瑕疵", "5": "二手有保存",
        }.get(cond_raw, f"未知({cond_raw})")
        location = get("location", "") or "台北市"
        desc = get("description", "") or ""
        hashtags = get("hashtags", []) or []
        qty = (s.edits.get("quantity") or 1)
        if not s.edits.get("quantity") and s.source.get("models"):
            try:
                qty = (s.source["models"][0] or {}).get("qty") or 1
            except Exception:
                pass
        # 圖數
        if s.new_image_file_ids:
            img_n = len(s.new_image_file_ids)
            img_src = "新傳"
        else:
            from .relist_feature import _extract_image_urls
            img_n = len(_extract_image_urls(s.source))
            img_src = "原圖"

        from html import escape as _esc
        lines = [
            "📦 <b>轉刊預覽</b>",
            "━━━━━━━━━━━━━━",
            f"<b>標題:</b> {_esc(str(title)[:60])}",
            f"<b>售價:</b> NT$ {price}",
            f"<b>分類:</b> <code>{_esc(str(cat_id))}</code>",
            f"<b>商品狀況:</b> {_esc(cond_label)}",
            f"<b>數量:</b> {qty}",
            f"<b>所在地:</b> {_esc(str(location))}",
            f"<b>圖片:</b> {img_n} 張({img_src})",
            f"<b>標籤:</b> {_esc(', '.join(hashtags) if hashtags else '(無)')}",
            f"<b>描述:</b> {_esc(str(desc)[:200])}{'...' if len(str(desc)) > 200 else ''}",
            "━━━━━━━━━━━━━━",
        ]
        if s.target_account:
            lines.append(f"🎯 <b>目標帳號:</b> {_esc(s.target_account)}")
        return "\n".join(lines)

    def _relist_edit_buttons(self) -> list:
        """編輯選單按鈕。"""
        return [
            [
                {"text": "✏️ 改標題", "callback_data": "rl:ed:title"},
                {"text": "💰 改價", "callback_data": "rl:ed:price"},
                {"text": "📊 改數量", "callback_data": "rl:ed:quantity"},
            ],
            [
                {"text": "📂 改分類", "callback_data": "rl:ed:catId"},
                {"text": "📝 改描述", "callback_data": "rl:ed:description"},
                {"text": "🏷️ 改標籤", "callback_data": "rl:ed:hashtags"},
            ],
            [
                {"text": "📍 改所在地", "callback_data": "rl:ed:location"},
                {"text": "🆕 全新/二手", "callback_data": "rl:cond_menu"},
                {"text": "🖼️ 換圖", "callback_data": "rl:img"},
            ],
            [
                {"text": "✅ 確認刊登", "callback_data": "rl:ok"},
                {"text": "❌ 取消", "callback_data": "rl:cancel"},
            ],
        ]

    def _relist_condition_buttons(self) -> list:
        """商品狀況選擇按鈕(全新/近全新/二手/瑕疵/保存)。"""
        return [
            [
                {"text": "🆕 全新", "callback_data": "rl:cond:1"},
                {"text": "✨ 近全新", "callback_data": "rl:cond:2"},
            ],
            [
                {"text": "📦 二手", "callback_data": "rl:cond:3"},
                {"text": "🔧 二手有瑕疵", "callback_data": "rl:cond:4"},
                {"text": "💎 二手有保存", "callback_data": "rl:cond:5"},
            ],
            [
                {"text": "↩️ 返回", "callback_data": "rl:back_edit"},
            ],
        ]

    def _relist_handle_command(
        self, text: str, chat_id: str, topic_id: int = 0,
    ) -> bool:
        """處理 /relist URL 命令。返回 True 表示已處理。"""
        from .relist_feature import (
            RelistSession, extract_item_id, fetch_source_item,
        )
        t = text.strip()
        # 命令前綴:/relist or /轉刊 or /转刊
        if not (
            t.lower().startswith("/relist")
            or t.startswith("/轉刊")
            or t.startswith("/转刊")
        ):
            return False

        # 抽 URL / item id
        parts = t.split(None, 1)
        if len(parts) < 2:
            self._relist_tg_send(
                RelistSession(chat_id=chat_id, topic_id=topic_id),
                "用法:<code>/relist &lt;Yahoo 商品連結&gt;</code>\n"
                "例:<code>/relist https://tw.bid.yahoo.com/item/101739354873</code>",
            )
            return True
        url_or_id = parts[1].strip()
        item_id = extract_item_id(url_or_id)
        if not item_id:
            self._relist_tg_send(
                RelistSession(chat_id=chat_id, topic_id=topic_id),
                f"❌ 連結格式無法識別:<code>{url_or_id[:80]}</code>\n"
                "請貼完整 <code>tw.bid.yahoo.com/item/...</code> 連結",
            )
            return True

        # 抓 source
        sess = RelistSession(
            chat_id=chat_id,
            topic_id=topic_id,
            item_id=item_id,
            source_url=url_or_id,
        )
        self._relist_tg_send(sess, f"⏳ 拉取商品 <code>{item_id}</code> 資料中...")
        item, err = fetch_source_item(url_or_id, on_log=self.on_log)
        if err or not item:
            self._relist_tg_send(
                sess, f"❌ 拉商品資料失敗:{err or '未知錯誤'}",
            )
            return True

        sess.source = item
        sess.state = "pick_target"
        self._relist_set_session(chat_id, sess)

        # 預覽 + 目標帳號選擇
        preview = self._relist_format_preview(sess)
        buttons = self._relist_account_picker_buttons()
        msg_id = self._relist_tg_send(
            sess, preview + "\n\n👉 <b>請選目標帳號(刊登到):</b>", buttons,
        )
        sess.preview_msg_id = msg_id
        return True

    def _relist_on_callback(
        self, data: str, chat_id: str, message_id: int,
    ) -> None:
        """處理 rl:* callback。"""
        sess = self._relist_get_session(chat_id)
        if not sess:
            try:
                self.tg.send("⚠️ 轉刊 session 已過期或不存在,請重新 /relist", chat_id)
            except Exception:
                pass
            return

        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        action = parts[1]
        arg = parts[2] if len(parts) >= 3 else ""

        if action == "cancel":
            self._relist_clear_session(chat_id)
            self._relist_tg_send(sess, "❌ 已取消轉刊。")
            return

        if action == "pt":  # pick target
            target_pid = arg
            accs = self._relist_load_accounts()
            picked = next((a for a in accs if (a.get("profile_id") or "") == target_pid), None)
            if not picked:
                self._relist_tg_send(sess, f"❌ 找不到目標帳號 profile_id={target_pid}")
                return
            sess.target_account = picked.get("name") or target_pid
            sess.target_profile_id = target_pid
            sess.state = "editing"
            # 顯示編輯選單
            text = self._relist_format_preview(sess)
            buttons = self._relist_edit_buttons()
            new_msg_id = self._relist_tg_send(sess, text, buttons)
            sess.edit_msg_id = new_msg_id
            return

        if action == "ed":  # edit field
            field_name = arg
            sess.pending_field = field_name
            sess.state = "await_input"
            prompts = {
                "title": "請輸入新標題:",
                "price": "請輸入新售價(數字,如 199):",
                "quantity": "請輸入新數量(整數):",
                "catId": "請輸入新分類 ID(數字,例 2092074086):",
                "description": "請輸入新描述(可多行):",
                "hashtags": "請輸入新標籤(逗號分隔,最多 4 個,如:和闐玉,手串):",
                "location": "請輸入新所在地(如 台北市):",
            }
            prompt_text = prompts.get(field_name, f"請輸入新 {field_name}:")
            self._relist_tg_send(
                sess, f"✏️ <b>{prompt_text}</b>\n(直接打字回覆即可,5 分鐘內有效)",
            )
            return

        if action == "cond_menu":  # 顯示商品狀況選擇
            text = self._relist_format_preview(sess) + "\n\n選擇<b>商品狀況</b>:"
            self._relist_tg_send(sess, text, self._relist_condition_buttons())
            return

        if action == "cond":  # 確認商品狀況
            cond = (arg or "1").strip()
            if cond not in ("1", "2", "3", "4", "5"):
                cond = "1"
            sess.edits["condition"] = cond
            cond_label = {
                "1": "全新", "2": "近全新", "3": "二手",
                "4": "二手有瑕疵", "5": "二手有保存",
            }.get(cond)
            text = self._relist_format_preview(sess) + f"\n\n✅ 商品狀況改為:<b>{cond_label}</b>"
            self._relist_tg_send(sess, text, self._relist_edit_buttons())
            return

        if action == "back_edit":  # 從子選單返回
            text = self._relist_format_preview(sess)
            self._relist_tg_send(sess, text, self._relist_edit_buttons())
            return

        if action == "img":  # 換圖模式
            sess.state = "await_photo"
            # v6.1.20.1:不再 reset new_image_file_ids — 讓使用者再按 🖼️ 還能繼續加圖,
            # 不會清空已上傳的。要清空可點 [🗑️ 清空已傳]。
            _n = len(sess.new_image_file_ids)
            _hint = (
                "🖼️ <b>請傳新圖片</b>(可多張,1-10 張)\n"
                "傳完後點下方完成。<i>不換圖直接點 [取消換圖] 用原圖。</i>"
            )
            if _n > 0:
                _hint = (
                    f"🖼️ <b>已有 {_n} 張新圖,繼續傳或完成</b>\n"
                    "繼續傳會加到後面,或點 [🗑️ 清空] 重來,或點 [✅ 完成換圖] 用目前的。"
                )
            buttons = [
                [
                    {"text": "✅ 完成換圖", "callback_data": "rl:done_img"},
                    {"text": "↩️ 取消換圖(用原圖)", "callback_data": "rl:keep_img"},
                ],
            ]
            if _n > 0:
                buttons.insert(0, [
                    {"text": "🗑️ 清空已傳 (重來)", "callback_data": "rl:clear_img"},
                ])
            self._relist_tg_send(sess, _hint, buttons=buttons)
            return

        if action == "clear_img":  # 清空已上傳
            sess.new_image_file_ids = []
            sess.state = "await_photo"
            self._relist_tg_send(
                sess,
                "🗑️ <b>已清空新圖</b>。請重新傳圖。",
                buttons=[
                    [
                        {"text": "↩️ 取消換圖(用原圖)", "callback_data": "rl:keep_img"},
                    ],
                ],
            )
            return

        if action == "done_img":
            n = len(sess.new_image_file_ids)
            if n == 0:
                self._relist_tg_send(sess, "⚠️ 還沒收到任何圖片,請先傳圖再點完成。")
                return
            sess.state = "editing"
            text = self._relist_format_preview(sess) + f"\n\n✅ 已收 {n} 張新圖(刊登時上傳到目標帳號)"
            buttons = self._relist_edit_buttons()
            self._relist_tg_send(sess, text, buttons)
            return

        if action == "keep_img":
            sess.new_image_file_ids = []
            sess.state = "editing"
            text = self._relist_format_preview(sess) + "\n\n(保留原圖)"
            buttons = self._relist_edit_buttons()
            self._relist_tg_send(sess, text, buttons)
            return

        if action == "ok":  # confirm publish
            self._relist_tg_send(
                sess,
                f"🚀 開始刊登到 <b>{sess.target_account}</b>...\n"
                f"(若有新圖會先上傳到該帳號 Pixelframe,約 5-30 秒)",
            )
            sess.state = "publishing"
            import threading as _t
            _t.Thread(
                target=self._relist_do_publish, args=(chat_id,), daemon=True,
                name=f"relist-{sess.item_id}",
            ).start()
            return

        self._relist_tg_send(sess, f"⚠️ 未知操作:{action}")

    def _relist_handle_text_input(
        self, text: str, chat_id: str, topic_id: int = 0,
    ) -> bool:
        """轉刊 session 在 await_input 狀態時的文字輸入。返回 True 表示已處理。"""
        sess = self._relist_get_session(chat_id)
        if not sess or sess.state != "await_input" or not sess.pending_field:
            return False
        field_name = sess.pending_field
        value: Any = text.strip()

        # 型別轉換
        try:
            if field_name == "price":
                value = int(value)
                if value < 0:
                    raise ValueError("價格不能負")
            elif field_name == "quantity":
                value = int(value)
                if value < 1:
                    value = 1
            elif field_name == "hashtags":
                # comma-separated
                parts = [p.strip() for p in re.split(r"[,，、]", value) if p.strip()]
                value = parts[:4]
            elif field_name == "catId":
                value = re.sub(r"[^\d]", "", value)
                if not value:
                    raise ValueError("分類 ID 應為純數字")
        except Exception as e:
            self._relist_tg_send(sess, f"❌ 輸入無效:{e}\n請重新輸入或點別的按鈕。")
            return True

        sess.edits[field_name] = value
        sess.pending_field = ""
        sess.state = "editing"
        text_disp = self._relist_format_preview(sess)
        buttons = self._relist_edit_buttons()
        self._relist_tg_send(sess, text_disp + f"\n\n✅ <b>{field_name}</b> 已更新", buttons)
        return True

    def _relist_handle_photo(
        self, photo_file_id: str, chat_id: str, topic_id: int = 0,
    ) -> bool:
        """轉刊 session 在 await_photo 狀態收到照片。返回 True 表示已處理。"""
        sess = self._relist_get_session(chat_id)
        if not sess or sess.state != "await_photo" or not photo_file_id:
            return False
        if len(sess.new_image_file_ids) >= 10:
            self._relist_tg_send(sess, "⚠️ 已達 10 張上限,點 ✅ 完成換圖 繼續。")
            return True
        sess.new_image_file_ids.append(photo_file_id)
        n = len(sess.new_image_file_ids)
        self._relist_tg_send(sess, f"📸 已收 {n}/10 張。繼續傳或點 ✅ 完成換圖。")
        return True

    def _relist_get_tg_file_url(self, file_id: str, in_forum: bool) -> str:
        """取 TG file_id 對應的下載 URL(支援私聊 + forum)。"""
        if not file_id:
            return ""
        # Forum 場景:用 forum_bridge.bot 的 token
        if in_forum and self.forum_bridge:
            try:
                return self.forum_bridge._tg_file_url(file_id) or ""
            except Exception:
                return ""
        # 私聊:用 self.tg.token 自己組
        token = getattr(self.tg, "token", "") or ""
        if not token:
            return ""
        try:
            import requests as _req
            r = _req.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id}, timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                fp = (data.get("result") or {}).get("file_path", "")
                if fp:
                    return f"https://api.telegram.org/file/bot{token}/{fp}"
        except Exception:
            pass
        return ""

    def _relist_do_publish(self, chat_id: str) -> None:
        """背景 thread:上傳新圖(若有)→ 呼叫 do_relist → 回報。"""
        sess = self._relist_get_session(chat_id)
        if not sess:
            return
        try:
            from .relist_feature import do_relist, upload_replacement_image
            from .merch_http_ops import _try_cached_session
            target_profile = Path(self._base_dir) / "profiles" / sess.target_profile_id

            # 1. 若有新圖,先建目標 session 並上傳
            new_image_urls: list = []
            if sess.new_image_file_ids:
                auth_sess = _try_cached_session(target_profile, log=self.on_log)
                if auth_sess is None or not auth_sess.is_valid:
                    self._relist_tg_send(
                        sess,
                        "❌ 目標帳號 session 無效,無法上傳新圖。請重登或改用原圖。",
                    )
                    sess.state = "editing"
                    return
                self._relist_tg_send(
                    sess, f"📤 上傳 {len(sess.new_image_file_ids)} 張新圖到 Yahoo Pixelframe...",
                )
                _in_forum = bool(sess.topic_id and self.forum_bridge)
                for i, fid in enumerate(sess.new_image_file_ids):
                    file_url = self._relist_get_tg_file_url(fid, in_forum=_in_forum)
                    if not file_url:
                        self._relist_tg_send(
                            sess, f"⚠️ 第 {i+1} 張圖 file_url 拿不到,跳過。",
                        )
                        continue
                    cdn_url, err = upload_replacement_image(
                        auth_sess, image_url=file_url, on_log=self.on_log,
                    )
                    if err or not cdn_url:
                        self._relist_tg_send(
                            sess, f"⚠️ 第 {i+1} 張圖上傳失敗:{err}",
                        )
                        continue
                    new_image_urls.append(cdn_url)
                if not new_image_urls:
                    self._relist_tg_send(
                        sess,
                        "❌ 所有新圖上傳都失敗,刊登中止。可改用原圖再試一次。",
                    )
                    sess.state = "editing"
                    return
                sess.new_image_urls = new_image_urls

            # 2. do_relist
            new_id, err = do_relist(
                target_profile,
                sess.source,
                edits=sess.edits,
                replacement_image_urls=new_image_urls if new_image_urls else None,
                on_log=self.on_log,
            )
            if err:
                self._relist_tg_send(
                    sess,
                    f"❌ <b>刊登失敗</b>\n{err}\n\n"
                    f"可以調整後再點 ✅ 確認刊登。",
                )
                sess.state = "editing"
                # 重新發送編輯選單
                self._relist_tg_send(
                    sess, self._relist_format_preview(sess),
                    self._relist_edit_buttons(),
                )
                return

            # 3. 成功
            item_url = f"https://tw.bid.yahoo.com/item/{new_id}"
            self._relist_tg_send(
                sess,
                f"✅ <b>刊登成功!</b>\n\n"
                f"📦 新商品 ID: <code>{new_id}</code>\n"
                f"🎯 帳號: {sess.target_account}\n"
                f"🔗 <a href=\"{item_url}\">查看商品</a>\n\n"
                f"來源:{sess.source.get('title','')[:40]}",
            )
            self._relist_clear_session(chat_id)
        except Exception as e:
            self.on_log(f"[RELIST] _relist_do_publish 異常: {e}")
            try:
                self._relist_tg_send(sess, f"❌ 刊登流程異常:{str(e)[:200]}")
            except Exception:
                pass
            sess.state = "editing"

    def _on_tg_callback(self, data: str, chat_id: str, message_id: int) -> None:
        """处理 inline keyboard 按钮回调。

        支持三种 prefix:
        - tr:* — 翻译模式
        - cs:* — AI 客服操作面板 (v6.0.74)
        - fm:* — Forum menu(/accounts /buyers /history)(v6.0.83)
        """
        # ---- v6.1.53 撤回按鈕(forum confirmation 訊息上的) ----
        # data 格式:fr:{topic_id}:{user_msg_id}
        # confirmation_msg_id = message_id 參數本身
        if data.startswith("fr:"):
            try:
                _parts = data.split(":", 2)
                if len(_parts) == 3:
                    _topic_id = int(_parts[1])
                    _user_msg_id = int(_parts[2])
                    self._handle_forum_recall_button(
                        topic_id=_topic_id,
                        user_msg_id=_user_msg_id,
                        confirmation_msg_id=message_id,
                        source_chat_id=chat_id,
                    )
            except Exception as e:
                self.on_log(f"[TG-FORUM] fr: callback 異常: {e}")
            return

        # ---- v6.1.20 一鍵轉刊 ----
        if data.startswith("rl:"):
            try:
                self._relist_on_callback(data, chat_id, message_id)
            except Exception as e:
                self.on_log(f"[RELIST] callback 異常: {e}")
            return

        # ---- v6.0.83 Forum menu ----
        if data.startswith("fm:"):
            self._on_forum_menu_callback(data, chat_id, message_id)
            return

        # ---- v6.0.74 AI 客服操作面板 ----
        if data.startswith("cs:"):
            self._handle_cs_callback(data, chat_id, message_id)
            return

        # ---- 🔄 刷新狀態(資訊卡) ----
        if data.startswith("refresh_card:"):
            conv_key = data[len("refresh_card:"):]
            if not self.forum_bridge:
                return
            try:
                ok, err = self.forum_bridge.refresh_info_card(conv_key)
                self.on_log(
                    f"[TG-FORUM] refresh_info_card {conv_key}: ok={ok} err={err}"
                )
            except Exception as e:
                self.on_log(f"[TG-FORUM] refresh_info_card 異常: {e}")
            return

        # ---- v6.1 訂單中心 callback ----
        # oc:filter:wp/wu/sh/od  oc:batch:todo_list  oc:summary:today
        if data.startswith("oc:"):
            try:
                self._on_order_center_callback(data, chat_id, message_id)
            except Exception as e:
                self.on_log(f"[TG-FORUM] oc callback 異常: {e}")
            return

        if not data.startswith("tr:"):
            return

        action = data[3:]  # "zh2ja" / "ja2zh" / "cancel" / "exit" / "switch"

        if action == "cancel":
            self._translate_mode.pop(chat_id, None)
            self.tg.send("已取消。", chat_id)
            return

        if action == "exit":
            self._translate_mode.pop(chat_id, None)
            self.tg.send("已退出翻譯模式。", chat_id)
            return

        if action == "switch":
            cur = self._translate_mode.get(chat_id)
            if cur == "zh2ja":
                new_dir = "ja2zh"
            else:
                new_dir = "zh2ja"
            self._translate_mode[chat_id] = new_dir
            label = "中文 → 日本語" if new_dir == "zh2ja" else "日本語 → 中文"
            buttons = [
                [
                    {"text": "切換方向", "callback_data": "tr:switch"},
                    {"text": "退出翻譯", "callback_data": "tr:exit"},
                ],
            ]
            self.tg.send_inline_keyboard(
                f"已切換翻譯方向：{label}\n\n"
                f"直接發送文字即可翻譯。",
                buttons,
                chat_id,
            )
            return

        if action in ("zh2ja", "ja2zh"):
            self._translate_mode[chat_id] = action
            if action == "zh2ja":
                label = "中文 → 日本語"
            else:
                label = "日本語 → 中文"
            buttons = [
                [
                    {"text": "切換方向", "callback_data": "tr:switch"},
                    {"text": "退出翻譯", "callback_data": "tr:exit"},
                ],
            ]
            self.tg.send_inline_keyboard(
                f"已進入翻譯模式：{label}\n\n"
                f"直接發送文字即可翻譯。",
                buttons,
                chat_id,
            )
            return

    def _do_translate(self, chat_id: str, direction: str, text: str) -> None:
        """在後台線程中執行翻譯。"""
        threading.Thread(
            target=self._do_translate_worker,
            args=(chat_id, direction, text),
            daemon=True,
        ).start()

    def _do_translate_worker(self, chat_id: str, direction: str, text: str) -> None:
        """翻譯工作線程。"""
        if direction == "zh2ja":
            system_prompt = (
                "你是專業的中日翻譯。請將用戶發送的繁體中文（台灣用語）翻譯成自然的日文。\n"
                "要求：\n"
                "- 使用日本人日常用語和表達方式\n"
                "- 保持原文語氣和風格\n"
                "- 只輸出翻譯結果，不要加任何解釋"
            )
        else:
            system_prompt = (
                "你是專業的中日翻譯。請將用戶發送的日文翻譯成繁體中文（台灣用語）。\n"
                "要求：\n"
                "- 使用台灣人日常用語和表達方式\n"
                "- 保持原文語氣和風格\n"
                "- 只輸出翻譯結果，不要加任何解釋"
            )

        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=system_prompt,
            user_prompt=text,
            timeout_sec=30,
        )

        if ok and result:
            self.tg.send(result.strip(), chat_id)
        else:
            self.tg.send(f"翻譯失敗：{result or '未知錯誤'}", chat_id)

    def _handle_confirm_seller_question(self, conv: ConversationState, text: str) -> None:
        """处理用户对问卖家问题的确认/修改（ok / edit:XXX / manual）。"""
        cmd = text.lower().strip().replace("：", ":")
        src = conv.product_urls[0]["source"] if conv.product_urls else ""

        if cmd == "ok":
            self._send_seller_question(conv, conv.auto_ask_question)
            return

        if cmd.startswith("edit:"):
            custom = text[5:].strip()
            if not custom:
                self._conv_aware_send(conv, "edit: 后面请输入你要发送的问题。")
                return
            conv.auto_ask_question = custom
            self._send_seller_question(conv, custom)
            return

        if cmd.startswith("reply:"):
            reply_content = text[6:].strip()
            if not reply_content:
                self._conv_aware_send(conv, "reply: 后面请输入你要回复给买家的内容。")
                return
            conv.final_reply = reply_content
            # v6.0.74:跳过中间消息,让 _auto_send_to_yahoo 统一发
            conv._skip_remind_on_done = True
            self._set_phase(conv.conv_id, ConvPhase.DONE)
            self._supervisor_send(
                f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                f"最终回复(直接回复):「{conv.final_reply}」"
            )
            threading.Thread(
                target=self._auto_send_to_yahoo,
                args=(conv,),
                daemon=True,
            ).start()
            return

        if cmd == "manual":
            conv.auto_ask_fallback = True
            self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
            src_hint = "煤炉(Mercari)卖家" if src == "mercari" else "闲鱼卖家"
            self._conv_aware_send(conv, 
                f"🔄 已切换手动模式。\n"
                f"请手动问{src_hint}后，直接回复卖家的答案。\n"
                f"回复 skip → 跳过"
            )
            return

        # 无法识别
        self._conv_aware_send(conv, 
            f"请回复：\n"
            f"ok → 确认发送问题\n"
            f"edit:内容 → 修改问题后发送\n"
            f"reply:内容 → 不问卖家，直接回复买家\n"
            f"manual → 切换手动模式\n"
            f"skip → 跳过"
        )

    def _send_seller_question(self, conv: ConversationState, question: str) -> None:
        """确认后，启动后台线程发送问题给卖家。"""
        src = conv.product_urls[0]["source"] if conv.product_urls else ""

        # v6.1.45 真 root cause 修復:每次「確認送賣家」=新一輪嘗試,必須清 fallback flag
        # 否則前一輪 fallback 留下的 True 會讓 _auto_ask_xianyu_inner L2329
        # `if conv.auto_ask_fallback: return` 直接早退,WS 從沒跑 → seller_peer_user_id 永遠空
        conv.auto_ask_fallback = False

        if src == "xianyu":
            # 闲鱼必须用简体中文
            from core.xianyu_seller_chat import _to_simplified
            question = _to_simplified(question)
            conv.auto_ask_question = question
            self._set_phase(conv.conv_id, ConvPhase.AUTO_ASKING_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"正在向闲鱼卖家发送:\n{question}")
            threading.Thread(
                target=self._auto_ask_xianyu_worker,
                args=(conv,),
                daemon=True,
            ).start()
        elif src == "mercari":
            self._set_phase(conv.conv_id, ConvPhase.AUTO_ASKING_SELLER)
            self._send_phase_buttons(conv,
                                    content=f"正在向煤炉卖家发送:\n{question}")
            threading.Thread(
                target=self._auto_ask_mercari_worker,
                args=(conv,),
                daemon=True,
            ).start()
        else:
            self._set_phase(conv.conv_id, ConvPhase.WAIT_SELLER)
            self._send_phase_buttons(conv,
                                    content="请手动问卖家后,引用此消息回复卖家的答案。")

    def _handle_confirm_reply(self, conv: ConversationState, text: str) -> None:
        """处理用户对 AI 草稿的确认（ok / edit:XXX）。"""
        cmd = text.lower().strip().replace("：", ":")

        # 确定当前草稿
        draft = conv.ai_integrated_draft or conv.ai_draft

        if cmd == "read":
            self._set_phase(conv.conv_id, ConvPhase.DONE)
            self._conv_aware_send(conv, f"✅ 正在消红点...（买家 {conv.buyer_label}）")
            threading.Thread(
                target=self._mark_read_yahoo,
                args=(conv.profile_id, conv.chat_url, conv.account_name),
                kwargs={"shop_code": conv.shop_code, "chat_id": conv.chat_id},
                daemon=True,
            ).start()
            return

        if cmd == "ok":
            # 贴图消红点：只打开聊天页面，不发消息
            if conv.ai_action == "STICKER_READ":
                self._set_phase(conv.conv_id, ConvPhase.DONE)
                self._conv_aware_send(conv, f"✅ 正在消红点 [{conv.account_name} / {conv.buyer_label}]")
                threading.Thread(
                    target=self._mark_read_yahoo,
                    args=(conv.profile_id, conv.chat_url, conv.account_name),
                    kwargs={"shop_code": conv.shop_code, "chat_id": conv.chat_id},
                    daemon=True,
                ).start()
                return
            conv.final_reply = draft
            # v6.0.74:让 _auto_send_to_yahoo 统一发完成通知,这里不再发中间消息
            conv._skip_remind_on_done = True
            self._set_phase(conv.conv_id, ConvPhase.DONE)
            self._supervisor_send(
                f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                f"最终回复:「{conv.final_reply}」"
            )
            threading.Thread(
                target=self._auto_send_to_yahoo,
                args=(conv,),
                daemon=True,
            ).start()
            return

        if cmd.startswith("edit:"):
            custom = text[5:].strip()
            if not custom:
                self._conv_aware_send(conv, "edit: 后面请输入你要发送的内容。")
                return
            conv.final_reply = custom
            # v6.0.74:让 _auto_send_to_yahoo 统一发完成通知
            conv._skip_remind_on_done = True
            self._set_phase(conv.conv_id, ConvPhase.DONE)
            self._supervisor_send(
                f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                f"最终回复(已修改):「{conv.final_reply}」"
            )
            threading.Thread(
                target=self._auto_send_to_yahoo,
                args=(conv,),
                daemon=True,
            ).start()
            return

        if cmd.startswith("reply:"):
            reply_content = text[6:].strip()
            if not reply_content:
                self._conv_aware_send(conv, "reply: 后面请输入你要回复给买家的内容。")
                return
            conv.final_reply = reply_content
            # v6.0.74:让 _auto_send_to_yahoo 统一发完成通知
            conv._skip_remind_on_done = True
            self._set_phase(conv.conv_id, ConvPhase.DONE)
            self._supervisor_send(
                f"✅ [{conv.account_name}] 买家 {conv.buyer_label}\n"
                f"最终回复(直接回复):「{conv.final_reply}」"
            )
            threading.Thread(
                target=self._auto_send_to_yahoo,
                args=(conv,),
                daemon=True,
            ).start()
            return

        if cmd.startswith("mod:"):
            instruction = text[4:].strip()
            if not instruction:
                self._conv_aware_send(conv, "mod: 后面请输入修改指令，例如：mod:改成可以议价")
                return
            self._conv_aware_send(conv, "🔄 AI 正在根据你的指令修改草稿...")
            # 累积 mod 历史
            if not hasattr(conv, '_mod_history'):
                conv._mod_history = []
            try:
                modified = self._run_ai_modify(conv, draft, instruction)
            except Exception as e:
                self._conv_aware_send(conv, f"⚠️ AI 修改失败：{str(e)[:200]}")
                return
            conv._mod_history.append(instruction)
            conv.ai_draft = modified
            # v6.1.60:同步 update ai_integrated_draft(若有)
            # 修「PREVIEW_SELLER phase 修改草稿後發送舊內容」bug:
            #   L8771 draft 讀取優先 ai_integrated_draft,但 mod 只寫 ai_draft → 拿到舊版發送
            if conv.ai_integrated_draft:
                conv.ai_integrated_draft = modified
            # v6.0.74:合并消息(原本 3 条变 1 条)
            _hist = " → ".join(conv._mod_history[-3:]) if len(conv._mod_history) > 1 else instruction
            _content = (
                f"✏️ AI 已根据指令修改\n"
                f"修改指令:{_hist}\n\n"
                f"新草稿:\n{modified}"
            )
            self._send_phase_buttons(conv, content=_content)
            return

        # 无法识别的指令
        self._conv_aware_send(conv, 
            f"请回复：\n"
            f"ok → 确认使用 AI 草稿\n"
            f"mod:指令 → AI根据指令修改草稿\n"
            f"edit:内容 → 修改后使用\n"
            f"reply:内容 → 忽略AI，直接回复买家\n"
            f"ask → 去问采购方卖家\n"
            f"skip → 跳过"
        )

    def _handle_seller_answer(self, conv: ConversationState, text: str) -> None:
        """用户提供了卖家的答案，AI 整合后生成给买家的回复。"""
        conv.seller_answer = text
        self._conv_aware_send(conv, f"收到卖家答案，AI 整合中...")
        self._integrate_and_preview(conv, text)

    def _integrate_and_preview(self, conv: ConversationState, seller_answer: str) -> None:
        """AI 整合卖家回复 → TG 预览。自动提问和手动流程共用。

        v6.0.75:拉完整賣家對話歷史(含 AI 自動回覆標註),AI 看到完整上下文後整合。
        """
        # 拉完整對話歷史
        full_history = self._fetch_seller_full_history(conv)
        try:
            integrated = self._run_ai_integrate(conv, seller_answer, full_history=full_history)
        except Exception as e:
            self._set_phase(conv.conv_id, ConvPhase.ERROR,
                            error_msg=str(e)[:200])
            self._send_phase_buttons(conv, content=f"AI 整合失败:\n{str(e)[:200]}\n请手动处理。")
            return

        conv.ai_integrated_draft = integrated

        # v6.0.74:合并消息(原本 3 条变 1 条)
        # v6.0.75:預覽時顯示完整歷史(讓用戶看到 AI 看的是什麼)
        preview_history = full_history if full_history else f"卖家答:\n{seller_answer[:300]}"
        _content = (
            f"{preview_history}\n\n"
            f"🤖 整合回复:\n{integrated}"
        )
        self._set_phase(conv.conv_id, ConvPhase.PREVIEW_SELLER)
        self._send_phase_buttons(conv, content=_content)

    def _fetch_seller_full_history(self, conv: ConversationState) -> str:
        """v6.0.75:用 listUserMessages 分頁拉「整段」對話歷史,格式化給 AI 看。

        按時間順序排,標註:
        - [我] 我發的訊息
        - [賣家·AI自動回覆 可忽略] AI 自動回覆
        - [賣家·真人] 真人回覆

        Returns: 格式化字串,空字串表示拉取失敗
        """
        if not conv.seller_session_id:
            return ""
        try:
            from core.goofish_ws_client import XianyuWsClient, parse_user_message_model
            from core.purchase_feature import PURCHASE_PROFILE_DIR
            from core.xianyu_im_http import get_my_user_id

            ws = XianyuWsClient.get_instance(PURCHASE_PROFILE_DIR, on_log=self.on_log)
            if not ws.is_connected():
                return ""
            cid = f"{conv.seller_session_id}@goofish"
            # 分頁拉「整段」對話,上限 500 條(避免極端情況)
            msgs, err = ws.list_all_user_messages_sync(cid, max_total=500, page_size=50)
            if err or not msgs:
                return ""

            my_uid = ""
            try:
                my_uid = get_my_user_id(PURCHASE_PROFILE_DIR)
            except Exception:
                pass

            # 解析 + 帶上 createAt 時間戳排序(server 返新→舊,我們要時間升序)
            import time as _t
            parsed_list = []
            for item in msgs:
                p = parse_user_message_model(item)
                if not p:
                    continue
                # v6.0.77:跳過閒魚官方系統提示(驗貨寶/先驗後買等),不算進對話歷史
                if getattr(p, "is_official_tip", False):
                    continue
                created = 0
                try:
                    created = int((item.get("message", {}) or {}).get("createAt", 0) or 0)
                except Exception:
                    pass
                parsed_list.append((created, p))
            parsed_list.sort(key=lambda x: x[0])  # 舊→新

            if not parsed_list:
                return ""

            # v6.0.81:歷史對話的語音/視頻媒體 — 補一輪 AI 多模態解析
            # WS 即時 push 已在 _on_ws_inbound_msg 處理過,但 HTTP 拉的歷史(尤其首次/重連後)
            # 直接用 content_text 會給 AI 看到裸 [語音 URL]/[視頻 URL],AI 看不到內容無法整合
            # 用 cache 避免重複跑;多個媒體並發處理省時間
            self._resolve_media_in_history(parsed_list)

            lines = [f"【賣家對話完整歷史(共 {len(parsed_list)} 條,時間順序)】"]
            for created, p in parsed_list:
                is_self = bool(p.sender_uid and my_uid and p.sender_uid == my_uid)
                if is_self:
                    role = "我"
                elif p.is_auto_reply:
                    role = "賣家·AI自動回覆 可忽略"
                else:
                    role = "賣家·真人"
                text = p.content_text or "[非文字訊息]"
                ts_str = ""
                if created > 0:
                    try:
                        ts_str = _t.strftime("%m-%d %H:%M", _t.localtime(created / 1000)) + " "
                    except Exception:
                        pass
                lines.append(f"  {ts_str}[{role}] {text[:200]}")

            return "\n".join(lines)
        except Exception as e:
            self.on_log(f"[TG] _fetch_seller_full_history 異常 conv={conv.conv_id[:8]}: {e}")
            return ""

    def _resolve_media_in_history(self, parsed_list) -> None:
        """v6.0.81:歷史對話批次解析語音/視頻 → 改寫 content_text。

        v6.0.82:加整體 90s timeout — Gemini/GPT vision 不穩定,絕不卡住 AI 整合主流程。
        超時後已完成的保留,未完成的不變(下次重整合時會吃 cache 或再試)。

        - parsed_list: [(created_ts, parsed_msg), ...] — 會 in-place 改寫 .content_text
        - 已是 [語音→文字] / [視頻→描述] 的(WS 推送過、或 server 已 STT)直接跳過
        - 命中 voice_to_text / video_to_text 的 7 天 cache 不會重複跑
        - 多個媒體並發處理(最多 3 個同時)+ 整體 90s timeout
        - 失敗時改寫成明確 hint(不保留裸 URL),AI 知道是「無法解析」
        """
        try:
            import re as _re
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
            import time as _time

            tasks = []  # (parsed_msg, kind, url)
            for _, p in parsed_list:
                ct = (p.content_text or "").strip()
                if not ct:
                    continue
                # 已轉過的跳過
                if ct.startswith("[語音→文字]") or ct.startswith("[視頻→描述]"):
                    continue
                # 失敗 hint 也跳過(避免反覆重跑)
                if ct.startswith("[語音 無法") or ct.startswith("[視頻 無法"):
                    continue
                # 待解析:[語音 (Ns)] URL / [視頻] URL ...
                if ct.startswith("[語音") and "http" in ct:
                    url_m = _re.search(r'(https?://[^\s]+)', ct)
                    if url_m:
                        tasks.append((p, "voice", url_m.group(1)))
                elif ct.startswith("[視頻") and "http" in ct:
                    url_m = _re.search(r'(https?://[^\s]+)', ct)
                    if url_m:
                        tasks.append((p, "video", url_m.group(1)))

            if not tasks:
                return

            OVERALL_BUDGET = 90.0  # 整批最多等 90 秒,Gemini 不穩定絕不卡 AI 整合
            self.on_log(f"[TG] 歷史媒體解析:{len(tasks)} 條(吃 cache,整體 budget {int(OVERALL_BUDGET)}s)")
            start_ts = _time.time()

            def _run_one(task):
                p, kind, url = task
                try:
                    # 子任務也設 budget — 多個並發時每個給比例時間,避免單一卡死整批
                    sub_budget = max(20.0, OVERALL_BUDGET / max(1, len(tasks)) * 1.5)
                    if kind == "voice":
                        from core.xianyu_voice_stt import voice_to_text
                        text = voice_to_text(url, on_log=self.on_log, budget_sec=sub_budget)
                        if text:
                            return (p, f"[語音→文字] {text}")
                        return (p, "[語音 無法轉寫,聽不到內容]")
                    else:
                        from core.xianyu_voice_stt import video_to_text
                        desc = video_to_text(url, on_log=self.on_log, budget_sec=sub_budget)
                        if desc:
                            return (p, f"[視頻→描述] {desc}")
                        return (p, "[視頻 無法解析,看不到內容]")
                except Exception as _e:
                    self.on_log(f"[TG] 歷史媒體解析異常 kind={kind} url={url[:60]}: {_e}")
                    return None

            ex = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hist-media")
            try:
                futures = [ex.submit(_run_one, t) for t in tasks]
                done_count = 0
                for fut in futures:
                    remain = max(0.0, OVERALL_BUDGET - (_time.time() - start_ts))
                    if remain <= 0:
                        # 超 budget,放棄等剩下的(留 hint 表示未解析)
                        for p_obj, _kind, _url in [tasks[i] for i in range(len(tasks)) if not futures[i].done()]:
                            if (p_obj.content_text or "").startswith("[語音"):
                                p_obj.content_text = "[語音 無法轉寫,聽不到內容]"
                            elif (p_obj.content_text or "").startswith("[視頻"):
                                p_obj.content_text = "[視頻 無法解析,看不到內容]"
                        self.on_log(f"[TG] 歷史媒體解析超 budget,完成 {done_count}/{len(tasks)},未完成的標記為「無法解析」")
                        break
                    try:
                        result = fut.result(timeout=remain)
                    except FutureTimeout:
                        # 個別任務超剩餘時間,break 後續一起處理
                        self.on_log(f"[TG] 歷史媒體解析整體超時,break")
                        break
                    if result:
                        p_obj, new_text = result
                        p_obj.content_text = new_text
                    done_count += 1
            finally:
                # cancel 未開始的任務,shutdown 不等
                for f in futures:
                    if not f.done():
                        f.cancel()
                ex.shutdown(wait=False)

            elapsed = _time.time() - start_ts
            self.on_log(f"[TG] 歷史媒體解析完成 {done_count}/{len(tasks)},耗時 {elapsed:.1f}s")
        except Exception as e:
            self.on_log(f"[TG] _resolve_media_in_history 異常(降級不改寫): {e}")

    # ---------- /status 命令 ----------

    def _handle_status_cmd(self) -> None:
        sorted_convs = self._get_sorted_active_convs()
        with self._lock:
            all_active = [
                c for c in self._convs.values()
                if c.phase not in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR)
            ]
        if not all_active:
            self.tg.send("当前没有活跃的对话。")
            return
        # 建立 conv_id → 编号 的映射
        idx_map = {c.conv_id: i + 1 for i, c in enumerate(sorted_convs)}
        lines = [f"📋 活跃对话：{len(all_active)} 个\n"]
        for c in all_active:
            age = int(time.time() - c.created_ts)
            num = idx_map.get(c.conv_id, 0)
            tag = f"#{num}" if num else "⏳"
            lines.append(
                f"{tag} [{c.account_name}] {c.buyer_label} "
                f"状态={c.phase.value} {age}秒前"
            )
        lines.append("\n💡 回复 #N 指令 可指定对话，如 #2 ok")
        self.tg.send("\n".join(lines))

    # ---------- AI 调用封装 ----------

    def _run_ai_judge(self, conv: ConversationState, buyer_text: str):
        """调用 commander_decide() 判断下一步。完整对话传给 AI，靠 prompt 理解进展。"""
        # Yahoo 链接（不再传货源链接）
        yahoo_ids = extract_yahoo_item_ids(conv.buyer_text)
        item_url = f"https://tw.bid.yahoo.com/item/{yahoo_ids[0]}" if yahoo_ids else conv.chat_url

        src = conv.product_urls[0]["source"] if conv.product_urls else infer_source_platform_from_url(conv.chat_url)
        raw_product_text = conv.product_text or ""

        # 统一商品摘要（含 Yahoo 运费 + 去价格的货源规格）
        product_summary = _build_product_summary(conv)

        # v6.1.55:用 build_media_for_ai 計算權重(賣家恆 1,買家半衰期 10 分鐘)
        # 視頻自動抓首幀 → base64 給 AI 看
        media_prompt_section, media_urls_for_ai = build_media_for_ai(
            conversation_media=list(getattr(conv, "conversation_media", None) or []),
            product_image_urls=list(conv.product_image_urls or []),
            max_count=10,
            on_log=self.on_log,
        )

        return commander_decide(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            buyer_text=buyer_text,
            latest_buyer_text=extract_latest_buyer_message(buyer_text),
            product_text=product_summary,
            item_url=item_url,
            source_hint=src,
            can_buy=conv.product_can_buy if raw_product_text else infer_can_buy("", src),
            fragile=guess_fragile(conv.product_title or "", raw_product_text),
            repeated_arrival=count_arrival_questions(buyer_text) > 1,
            # v6.1.55:image_urls 直接傳合併好的(已含權重排序、視頻首幀),媒體 prompt 描述走新管道
            image_urls=media_urls_for_ai or None,
            buyer_image_urls=None,  # v6.1.55:廢棄(由 conversation_media 取代),避免重複構造
            media_prompt_section=media_prompt_section or None,
        )

    def _run_ai_modify(self, conv: ConversationState, draft: str, instruction: str) -> str:
        """根据用户指令修改 AI 草稿，带对话上下文避免幻觉。"""
        context = f"【對話內容（僅供理解背景，不要從中自行添加內容）】\n{_smart_truncate(conv.buyer_text, 1000)}\n\n"
        if conv.product_text:
            context += f"【商品資訊（僅供理解背景）】\n{conv.product_text[:500]}\n\n"

        # 累积 mod 历史，让 AI 知道之前改过什么
        mod_history = ""
        if hasattr(conv, '_mod_history') and conv._mod_history:
            mod_history = "【之前的修改記錄】\n"
            for i, h in enumerate(conv._mod_history, 1):
                mod_history += f"{i}. {h}\n"
            mod_history += "\n"

        user_prompt = (
            f"{context}"
            f"{mod_history}"
            f"【當前草稿】\n{draft}\n\n"
            f"【本次修改指令】\n{instruction}\n\n"
            f"請嚴格按照修改指令調整當前草稿。只輸出修改後的回覆文字。"
        )

        system_prompt = (
            "你是草稿修改助手。你的唯一任務是根據用戶的修改指令來調整草稿。\n\n"
            "【重要】修改指令是賣家對你（AI）說的話，不是要發給買家的內容。\n"
            "賣家可能用口語化的方式告訴你他的意思，例如：\n"
            "- 「這個沒有了 被我下架了」→ 意思是要你把草稿改成告訴買家商品已經沒有了\n"
            "- 「跟他說可以便宜一點」→ 意思是要你把草稿改成告訴買家可以議價\n"
            "你要理解賣家的意圖，然後用適合回覆買家的語氣來修改草稿。\n\n"
            "【鐵律 - 必須嚴格遵守】\n"
            "1. 只改用戶要求改的部分，其他部分原封不動保留\n"
            "2. 絕對不要自行添加原始草稿中沒有的內容或資訊\n"
            "3. 用戶說刪除/不要某句話，就徹底刪掉，不要換個說法再加回來\n"
            "4. 不要自作主張補充說明、理由或額外資訊\n"
            "5. 修改幅度要最小化：能改一句就不要重寫整段\n"
            "6. 保持繁體中文、口語化、親切的語氣\n"
            "7. 只輸出修改後的回覆文字，不要加任何說明、標題或引號\n\n"
            "【常見錯誤 - 絕對避免】\n"
            "- ❌ 用戶說「不要提到出貨地」→ 你換個說法又提到出貨地\n"
            "- ❌ 用戶說「加上年後出貨」→ 你自己又加了一堆其他內容\n"
            "- ❌ 用戶只要求改價格 → 你把整段話重寫了\n"
            "- ✅ 正確做法：只動用戶指定的部分，其餘一字不改"
        )

        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if not ok:
            raise RuntimeError(result)
        return result.strip()

    def _run_ai_reply(self, conv: ConversationState, buyer_text: str) -> str:
        """调用 call_openai() 生成 AI 回复草稿。完整对话传入，靠 prompt 理解进展。

        v6.1:Commander 若給了 pricing_hint(已算好 bid_ratio + suggested_tier)
        會結構化傳給 Writer,Writer 直接套對應檔位話術,不用再自己算比例
        """
        product_summary = _build_product_summary(conv)

        user_prompt = (
            f"【對話內容】\n"
            f"{buyer_text}\n\n"
        )
        if product_summary:
            user_prompt += f"{product_summary}\n\n"

        # v6.1:議價結構化 hint(從 Commander 帶下來)
        ph = getattr(conv, "ai_pricing_hint", None) or {}
        if ph and isinstance(ph, dict) and ph.get("suggested_tier"):
            tier_map = {
                "accept_or_minor_haggle": "比例 ≥ 0.85 → **可成交,或讓 50-200 收尾**(寫法:『可以喔,幫您算 XXXX』/『少 100 行嗎,XXXX 給您』,不要硬拒)",
                "counter_offer": "比例 0.70-0.85 → **反提折中價維持利潤**(寫法:『XXXX 真的不行,XXXX 給您可以嗎』,介於買家出價跟標價中間)",
                "firm_refuse": "比例 < 0.70 → **婉拒留議價空間**(寫法:『這個真的不行耶,標價已經很便宜了喔』,語氣客氣)",
                "general_refuse": "沒具體出價數字 → **複述標價婉拒**(寫法:『價格上不太能再讓喔,要不就直接 XXXX 給您吧』)",
            }
            tier_desc = tier_map.get(ph.get("suggested_tier"), "")
            user_prompt += "【💰 議價建議檔位(已由指揮官算好,直接照這個寫)】\n"
            if ph.get("bid_amount"):
                user_prompt += f"買家出價:{ph['bid_amount']}\n"
            if ph.get("list_price"):
                user_prompt += f"Yahoo 標價:{ph['list_price']}\n"
            if ph.get("bid_ratio"):
                user_prompt += f"比例 R = {ph['bid_ratio']:.2f}\n"
            user_prompt += f"建議:{tier_desc}\n\n"

        # v6.1.55:用 build_media_for_ai(賣家恆 1.0,買家半衰期 10 分鐘,視頻抓首幀)
        _media_section_w, _media_urls_w = build_media_for_ai(
            conversation_media=list(getattr(conv, "conversation_media", None) or []),
            product_image_urls=list(conv.product_image_urls or []),
            max_count=10,
            on_log=self.on_log,
        )
        if _media_section_w:
            user_prompt += "\n\n" + _media_section_w
            user_prompt += "\n\n如果買家圖內圈了/指了具體東西,你的回覆要對應那個物件,不要答非所問。"

        user_prompt += "請根據以上資訊生成回覆。"

        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=_WRITER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_urls=_media_urls_w or None,
        )
        if not ok:
            # v6.1.27:訓練數據紀錄 — Writer 失敗也記
            _tc_record(
                "ai:writer_draft",
                conv=conv,
                input={"buyer_text": buyer_text[-2000:], "pricing_hint": ph},
                output={"ok": False, "error": str(result)[:500]},
                metadata={"failed": True},
            )
            raise RuntimeError(result)
        draft = result.strip()
        # v6.1.27:訓練數據紀錄 — Writer 草稿(關鍵 SFT 信號:同樣 state AI 寫什麼 vs 使用者最後寫什麼)
        _tc_record(
            "ai:writer_draft",
            conv=conv,
            input={"buyer_text": buyer_text[-2000:], "pricing_hint": ph},
            output=draft,
            ai_draft=draft,
            metadata={"draft_length": len(draft)},
        )
        return draft

    def _run_ai_integrate(self, conv: ConversationState, seller_answer: str,
                           full_history: str = "") -> str:
        """卖家回复后，AI 整合生成给买家的回复。

        v6.0.75:可選 full_history 完整對話歷史(含 AI 自動回覆標註)。
        AI 看完整對話 → 只關注真人有效回覆 → 整合給買家。
        """
        product_summary = _build_product_summary(conv)

        # 标注货源平台，帮助 AI 理解回复语言
        src = conv.product_urls[0]["source"] if conv.product_urls else ""
        if src == "mercari":
            lang_hint = "（注意:賣家對話可能是日文,請翻譯理解後用繁體中文回復買家）\n"
        else:
            lang_hint = ""

        user_prompt = f"买家原始问题:{conv.buyer_text}\n{lang_hint}"

        # v6.0.75:優先用完整對話歷史(賣家可能多次回覆,只關注真人 + 忽略 AI 自動回覆)
        if full_history:
            user_prompt += (
                f"\n{full_history}\n\n"
                f"【整合規則】\n"
                f"1. 標註「賣家·AI自動回覆 可忽略」的訊息是賣家的自動離線回覆,**完全忽略**\n"
                f"2. 標註「賣家·真人」的訊息才是有效資訊,以此整合回覆給買家\n"
                f"3. 如果只有 AI 自動回覆沒有真人回覆,告訴買家「賣家暫不在,我們會盡快確認」\n"
                f"4. 多條真人回覆都納入考慮(賣家可能補充訊息)\n"
            )
        else:
            user_prompt += f"卖家最新回复:{seller_answer}\n"

        if product_summary:
            user_prompt += f"\n{product_summary}\n"

        # v6.1.55:整合時也用權重 helper(賣家圖恆 1.0,買家衰減)
        _media_section_i, _media_urls_i = build_media_for_ai(
            conversation_media=list(getattr(conv, "conversation_media", None) or []),
            product_image_urls=list(conv.product_image_urls or []),
            max_count=10,
            on_log=self.on_log,
        )
        if _media_section_i:
            user_prompt += "\n\n" + _media_section_i

        user_prompt += "\n请生成给买家的正式回复。"
        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=_INTEGRATE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_urls=_media_urls_i or None,
        )
        if not ok:
            _tc_record(
                "ai:seller_integration",
                conv=conv,
                input={"seller_answer": seller_answer[:1500], "full_history_len": len(full_history)},
                output={"ok": False, "error": str(result)[:500]},
                metadata={"failed": True},
            )
            raise RuntimeError(result)
        integrated = result.strip()
        # v6.1.27:訓練數據紀錄 — AI 整合賣家回覆後生成的草稿
        _tc_record(
            "ai:seller_integration",
            conv=conv,
            input={"seller_answer": seller_answer[:1500], "full_history_len": len(full_history)},
            output=integrated,
            ai_draft=integrated,
            metadata={"draft_length": len(integrated)},
        )
        return integrated

    def _extract_latest_buyer_question(self, buyer_text: str) -> str:
        """从对话历史中提取买家「最近一轮未回答」的问题。

        v6.0.75:只取最後一個【卖家】訊息之後的買家訊息(已答的不重複問)。
        - 如果完全沒「【卖家】」(第一次對話)→ 取所有買家訊息
        - 如果有「【卖家】」訊息 → 只取最後一個賣家訊息之後的買家訊息
        """
        lines = buyer_text.strip().split("\n")

        # 找最後一個「【卖家】」訊息的位置 — 那之後的買家訊息才是「待回答」
        last_seller_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().startswith("【卖家】"):
                last_seller_idx = i
                break

        target_lines = lines[last_seller_idx + 1:] if last_seller_idx >= 0 else lines

        buyer_parts = []
        for line in target_lines:
            stripped = line.strip()
            if stripped.startswith("【买家】"):
                content = stripped.replace("【买家】", "", 1).strip()
                # 跳过纯链接行
                if content.startswith("http") and not any(
                    c >= "\u4e00" and c <= "\u9fff" for c in content.split(" ", 1)[-1] if " " in content
                ):
                    continue
                if content:
                    buyer_parts.append(content)

        # fallback:切片後沒任何買家訊息(極端情況),退回取所有買家訊息
        if not buyer_parts and last_seller_idx >= 0:
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("【买家】"):
                    content = stripped.replace("【买家】", "", 1).strip()
                    if content.startswith("http") and not any(
                        c >= "\u4e00" and c <= "\u9fff" for c in content.split(" ", 1)[-1] if " " in content
                    ):
                        continue
                    if content:
                        buyer_parts.append(content)

        if buyer_parts:
            q = " ".join(buyer_parts)
        else:
            q = buyer_text[-200:].strip()

        # 去掉对话连接词        # 去掉对话连接词（第一次问卖家时这些词不自然）
        for prefix in ["那麼", "那么", "還有", "还有", "另外", "對了", "对了",
                        "忘記問了", "忘记问了", "順便問一下", "顺便问一下"]:
            if q.startswith(prefix):
                q = q[len(prefix):].lstrip("，, ")
                break
        # 去掉无意义的寒暄/废话（卖家已经在卖，不需要问还在不在）
        import re
        filler_patterns = [
            r"這個還在嗎[？\?]?\s*",
            r"这个还在吗[？\?]?\s*",
            r"還在嗎[？\?]?\s*",
            r"还在吗[？\?]?\s*",
            r"請問還有嗎[？\?]?\s*",
            r"请问还有吗[？\?]?\s*",
            r"有貨嗎[？\?]?\s*",
            r"有货吗[？\?]?\s*",
            r"還有貨嗎[？\?]?\s*",
            r"还有货吗[？\?]?\s*",
        ]
        for pat in filler_patterns:
            q = re.sub(pat, "", q).strip()
        return q

    def _generate_seller_question(self, conv: ConversationState) -> str:
        """把买家的问题转成闲鱼买家口吻，忠实翻译不自己发挥。

        v6.0.75:多商品場景時,提示 AI 只挑與當前 primary 商品相關的問題。
        """
        latest_q = self._extract_latest_buyer_question(conv.buyer_text)
        user_prompt = f"买家原话：{latest_q}"

        # 多商品時,告訴 AI 只翻譯與當前商品有關的問題
        all_prods = getattr(conv, "all_products", []) or []
        if len(all_prods) > 1 and conv.product_urls:
            current_yid = conv.product_urls[0].get("yahoo_id", "")
            if current_yid:
                user_prompt += (
                    f"\n\n注意:對話中可能涉及多個商品,本次只翻譯與「Yahoo編號 {current_yid}」相關的問題。"
                    f"買家如果只問了一個,直接翻譯;若問了多個商品,只挑出與當前商品相關的部分。"
                )

        # v6.1.55:問賣家時不需商品圖(賣家本人就是貨源),只給高權重的對話媒體
        # 商品圖不傳 — 賣家自己就在管商品
        _media_section_q, _media_urls_q = build_media_for_ai(
            conversation_media=list(getattr(conv, "conversation_media", None) or []),
            product_image_urls=None,  # 不傳商品圖
            max_count=5,  # 給賣家提問場景,3-5 張即可
            on_log=self.on_log,
        )
        if _media_section_q:
            user_prompt += "\n\n" + _media_section_q
            user_prompt += (
                "\n翻譯時把『這個』『圈起來那個』等指代詞還原成具體物件描述,"
                "讓賣家清楚知道買家在問哪個東西。"
            )

        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=_SELLER_QUESTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_urls=_media_urls_q or None,
        )
        if not ok:
            _tc_record(
                "ai:seller_question",
                conv=conv,
                input={"latest_q": latest_q, "lang": "zh"},
                output={"ok": False, "error": str(result)[:500]},
                metadata={"failed": True},
            )
            raise RuntimeError(result)
        q = result.strip()
        # v6.1.27:訓練數據紀錄 — AI 生成的問賣家問題(閒魚中文)
        _tc_record(
            "ai:seller_question",
            conv=conv,
            input={"latest_q": latest_q, "lang": "zh"},
            output=q,
            metadata={"q_length": len(q)},
        )
        return q

    def _generate_seller_question_ja(self, conv: ConversationState, reason: str = "") -> tuple:
        """把买家的问题翻译成日文，返回 (日文, 中文翻译)。"""
        latest_q = self._extract_latest_buyer_question(conv.buyer_text)
        if reason:
            user_prompt = f"買い手が知りたいこと：{reason}\n買い手の元の質問：{latest_q}"
        else:
            user_prompt = f"買い手の元の質問：{latest_q}"

        # v6.0.75:多商品場景 → 提示 AI 只翻譯與當前 primary 商品相關的問題
        all_prods = getattr(conv, "all_products", []) or []
        if len(all_prods) > 1 and conv.product_urls:
            current_yid = conv.product_urls[0].get("yahoo_id", "")
            if current_yid:
                user_prompt += (
                    f"\n\n注意：対話に複数の商品が含まれる可能性があるため、"
                    f"今回は「Yahoo番号 {current_yid}」に関する質問のみ翻訳してください。"
                )

        # v6.1.55:問日本賣家時也用對話媒體權重(賣家自己有商品所以不傳商品圖)
        _media_section_qj, _media_urls_qj = build_media_for_ai(
            conversation_media=list(getattr(conv, "conversation_media", None) or []),
            product_image_urls=None,
            max_count=5,
            on_log=self.on_log,
        )
        if _media_section_qj:
            user_prompt += "\n\n" + _media_section_qj
            user_prompt += (
                "\n買い手が囲った/指した特定の物体があれば、"
                "『これ』『丸を付けたもの』を具体的な物体に置き換えて翻訳してください。"
            )

        ok, result = call_openai(
            api_key=self.ai.get("api_key", ""),
            base_url=self.ai.get("base_url", ""),
            endpoint_mode=self.ai.get("endpoint_mode", "responses"),
            model=self.ai.get("model", ""),
            system_prompt=_SELLER_QUESTION_JA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_urls=_media_urls_qj or None,
        )
        if not ok:
            _tc_record(
                "ai:seller_question",
                conv=conv,
                input={"latest_q": latest_q, "lang": "ja", "reason": reason},
                output={"ok": False, "error": str(result)[:500]},
                metadata={"failed": True},
            )
            raise RuntimeError(result)
        text = result.strip()
        if "|" in text:
            ja, zh = text.split("|", 1)
            _tc_record(
                "ai:seller_question",
                conv=conv,
                input={"latest_q": latest_q, "lang": "ja", "reason": reason},
                output={"ja": ja.strip(), "zh": zh.strip()},
                metadata={"q_length": len(ja)},
            )
            return ja.strip(), zh.strip()
        _tc_record(
            "ai:seller_question",
            conv=conv,
            input={"latest_q": latest_q, "lang": "ja", "reason": reason},
            output={"ja": text, "zh": ""},
            metadata={"q_length": len(text)},
        )
        return text, ""

    def _get_chrome_path(self) -> str:
        if self._chrome_path:
            return self._chrome_path
        try:
            settings_path = Path(self._base_dir or ".") / "settings.json"
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                return str(s.get("browser_path", "") or "").strip()
        except Exception:
            pass
        return ""

    def _fetch_product_info(self, yahoo_item_id: str, profile_id: str) -> Dict[str, str]:
        """通过 gennyou1 API 查货源，再查 Mercari 状态。

        返回 {"source", "source_url", "barcode", "account", "can_buy", "status_text"} 或空 dict。
        """
        # 1) 调 D1 云端数据库查货源
        self.on_log(f"[TG] querying D1: {yahoo_item_id}")
        row = _query_product_d1(yahoo_item_id, on_log=self.on_log)
        if not row:
            self.on_log(f"[TG] D1: not found for {yahoo_item_id}")
            return {}

        barcode = str(row.get("barcode", "") or "").strip()
        account = str(row.get("account", "") or "").strip()
        source, source_url = _classify_source(barcode)
        self.on_log(f"[TG] D1: source={source}, barcode={barcode[:60]}")

        result: Dict[str, str] = {
            "source": source,
            "source_url": source_url,
            "barcode": barcode,
            "account": account,
            "can_buy": "未知",
            "status_text": "",
        }

        # 2) Mercari → 用 Playwright 查状态（带重试）
        if source == "mercari" and source_url:
            status = self._check_mercari_status(source_url, profile_id)
            # 如果首次检查失败或超时，重试一次
            need_retry = False
            if not status:
                need_retry = True
            elif status.get("can_buy") == "未知" and "检查失败" in status.get("status", ""):
                need_retry = True
            if need_retry:
                self.on_log("[TG] Mercari 首次检查失败，5秒后重试...")
                import time; time.sleep(5)
                status = self._check_mercari_status(source_url, profile_id)
            if status:
                result["can_buy"] = status.get("can_buy", "未知")
                result["status_text"] = status.get("status", "")
                result["source_page_text"] = status.get("page_text", "")

        # 3) 闲鱼 → 用匿名浏览器查状态
        if source == "xianyu" and source_url:
            self.on_log(f"[TG] checking xianyu: {source_url}")
            status = self._check_xianyu_status(source_url)
            if status:
                result["can_buy"] = status.get("can_buy", "未知")
                result["status_text"] = status.get("status", "")
                result["source_page_text"] = status.get("page_text", "")
                if status.get("title"):
                    result["title"] = status["title"]
                # v6.0.75:閒魚商品圖片(用尺子量尺寸/材質特寫圖)
                if status.get("image_urls"):
                    result["image_urls"] = status["image_urls"]
            else:
                result["status_text"] = "闲鱼查询失败"

        return result

    def _check_mercari_status(self, mercari_url: str, profile_id: str) -> Optional[Dict[str, str]]:
        """v6.1.27 改純 HTTP API:Mercari /items/get + DPoP 本地簽名,~100ms 不開 Playwright."""
        try:
            from core.mercari_check_feature import (
                _make_dpop_signer as _mk_dpop,
                _check_mercari_api as _mc_api,
                _extract_mercari_item_id as _extract,
            )
        except Exception:
            try:
                from core.mercari_check_feature import (
                    _make_dpop_signer as _mk_dpop,
                    _check_mercari_api as _mc_api,
                )
                _extract = None
            except Exception:
                # 純 HTTP 模組壞了,fallback skip(不開 Playwright)
                return None

        # 從 URL 抽 item_id (m12345... 或 /item/m12345)
        import re as _re_mc
        item_id = ""
        if _extract:
            try:
                item_id = _extract(mercari_url) or ""
            except Exception:
                item_id = ""
        if not item_id:
            m = _re_mc.search(r'(m\d{8,})', mercari_url or "")
            item_id = m.group(1) if m else ""
        if not item_id:
            return None

        try:
            import requests as _req
            session = _req.Session()
            dpop_sign = _mk_dpop()
            api_result = _mc_api(item_id, session, dpop_sign)
            if not api_result:
                return None
            status_str = api_result.get("status", "未知")
            # 對應 _fetch_product_info 的 can_buy 語意
            if api_result.get("buy_hit"):
                can_buy = "是"
            elif api_result.get("sold_hit") or api_result.get("deleted_hit"):
                can_buy = "否"
            else:
                can_buy = "未知"
            return {
                "status": status_str,
                "can_buy": can_buy,
                # page_text/title/image_urls 在純 HTTP 模式拿不到,留空(訓練資料仍有 yahoo 頁面 info)
                "page_text": "",
                "title": "",
                "image_urls": [],
            }
        except Exception:
            return None

    @staticmethod
    async def _check_mercari_async(chrome_path: str, profile_dir: str, url: str) -> Dict[str, str]:
        """异步打开 Mercari 页面，提取文本判断状态。"""
        _install_pw_silence_handler()
        from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

        ctx = None
        try:
            async with async_playwright() as p:
                _tg_send_lkw = dict(
                    user_data_dir=profile_dir,
                    executable_path=chrome_path,
                    headless=False,
                    no_viewport=True,   # 不设 viewport（Patchright 兼容）
                    args=get_launch_args(headless=True, extra=["--window-size=1280,800"]),
                    ignore_default_args=get_ignore_default_args(headless=True),
                )
                try:
                    ctx = await p.chromium.launch_persistent_context(**_tg_send_lkw)
                except TypeError:
                    _tg_send_lkw.pop("no_viewport", None)
                    _tg_send_lkw["viewport"] = {"width": 1280, "height": 800}
                    ctx = await p.chromium.launch_persistent_context(**_tg_send_lkw)
                await apply_runtime_normalization_async(ctx)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                # 拦截不必要的资源（保留 stylesheet，煤炉 SPA 需要）
                await page.route("**/*", functools.partial(
                    _safe_route_handler, block_types={"image", "media", "font"}
                ))

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # 煤炉是 React SPA，轮询等待关键内容渲染
                text = ""
                for _ in range(8):  # 最多等 8 * 2 = 16秒
                    await page.wait_for_timeout(2000)
                    text = await page.evaluate("document.body.innerText || ''") or ""
                    # 只要出现任何状态关键词就停止等待
                    if any(k in text for k in (
                        "購入手続きへ", "購入する", "購入に進む", "カートに入れる",
                        "売り切れ", "SOLD", "該当する商品は削除",
                        "お探しのページは見つかりませんでした",
                        "商品の説明", "商品の情報",
                    )):
                        break

                # 判断状态
                deleted = ("該当する商品は削除されています" in text
                           or "お探しのページは見つかりませんでした" in text)
                sold = "売り切れ" in text or "この商品は売り切れました" in text
                buy = ("購入手続きへ" in text or "購入する" in text
                       or "購入に進む" in text or "カートに入れる" in text)

                if deleted:
                    # CDN 二次验证：检查图片是否仍在，判断是否真的删除
                    _item_m = re.search(r'/item/(m\d+)', url or "")
                    _item_id = _item_m.group(1) if _item_m else ""
                    if _item_id:
                        try:
                            import urllib.request as _ur
                            _cdn = f"https://static.mercdn.net/item/detail/orig/photos/{_item_id}_1.jpg"
                            _req = _ur.Request(_cdn, method="HEAD",
                                               headers={"Referer": "https://jp.mercari.com/",
                                                        "User-Agent": "Mozilla/5.0"})
                            with _ur.urlopen(_req, timeout=3) as _r:
                                if _r.status == 200:
                                    return {"status": "可能在售(网页不可见)", "can_buy": "可能", "page_text": text[:4000]}
                        except Exception:
                            pass
                    return {"status": "已删除", "can_buy": "否", "page_text": text[:4000]}
                elif sold:
                    return {"status": "已售完", "can_buy": "否", "page_text": text[:4000]}
                elif buy:
                    return {"status": "在售", "can_buy": "是", "page_text": text[:4000]}
                else:
                    # 页面有商品信息但没有购买按钮，可能是页面结构变化
                    if "商品の説明" in text or "商品の情報" in text:
                        return {"status": "在售(推测)", "can_buy": "是", "page_text": text[:4000]}
                    return {"status": "未知", "can_buy": "未知", "page_text": text[:4000]}
        except Exception as e:
            return {"status": f"检查失败: {str(e)[:80]}", "can_buy": "未知"}
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # v6.1.27:閒魚 API checker singleton(純 HTTP,共用 session + 自動刷 token)
    _xianyu_api_checker = None
    _xianyu_api_lock = threading.Lock()

    @classmethod
    def _get_xianyu_api_checker(cls, on_log=None):
        """lazy init GoofishApiChecker(用「闲鱼检测专用登录」的 cookie).

        ConversationManager 多 thread 共用同一個 checker(內部 session 線程安全).
        cookie 過期會自動 HTTP 刷新.
        AI 客服場景 → 傳 noop log,避免 [GF-API] 訊息洩漏到 GUI.
        """
        if cls._xianyu_api_checker is not None:
            return cls._xianyu_api_checker
        with cls._xianyu_api_lock:
            if cls._xianyu_api_checker is not None:
                return cls._xianyu_api_checker
            try:
                from core.goofish_api_check import GoofishApiChecker
                # AI 客服路徑強制靜默(diagnose log 留給 unified_check tab)
                _ck = GoofishApiChecker(log=lambda m: None)
                if _ck.load():
                    cls._xianyu_api_checker = _ck
                    return _ck
            except Exception:
                pass
            return None

    def _check_xianyu_status(self, goofish_url: str) -> Optional[Dict[str, str]]:
        """v6.1.27 改純 HTTP H5 API:用 GoofishApiChecker(quasi-instant ~200ms).

        如果「闲鱼检测专用登录」cookie 過期/沒登入 → fall back Playwright.
        Playwright 是慢路徑 + 需要系統 Chrome(executable_path 已修).
        """
        # 從 URL 抽 item_id
        try:
            from core.goofish_check_feature import extract_item_id
            iid = extract_item_id(goofish_url)
        except Exception:
            import re as _re_g
            m = _re_g.search(r'[?&]id=(\d+)', goofish_url or "")
            iid = m.group(1) if m else ""
        if not iid:
            return None

        # 1) 純 HTTP H5 API 優先
        ck = self._get_xianyu_api_checker(on_log=self.on_log)
        if ck is not None:
            try:
                r = ck.check_item(iid)
                if r:
                    # 對應 _fetch_product_info 的 can_buy 語意
                    status = r.get("status", "未知")
                    if status == "在线":
                        can_buy = "是"
                    elif status in ("卖掉了", "已下架", "已删除", "拍卖"):
                        can_buy = "否"
                    else:
                        can_buy = "未知"
                    return {
                        "status": status,
                        "can_buy": can_buy,
                        "title": r.get("title", ""),
                        "page_text": "",   # H5 API 無 page_text(訓練端從 yahoo 頁面 info 就夠)
                        "image_urls": [],
                    }
            except Exception:
                pass

        # 2) Fallback:Playwright(慢但保底)
        try:
            result = asyncio.run(self._check_xianyu_async(goofish_url))
            return result
        except Exception as e:
            self.on_log(f"[TG] xianyu check error: {e}")
            return None

    @staticmethod
    async def _check_xianyu_async(url: str) -> Dict[str, str]:
        """异步打开闲鱼 PC 页面，提取文本判断状态。"""
        _install_pw_silence_handler()
        from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

        browser = None
        try:
            async with async_playwright() as p:
                # v6.1.27 修復:帶 executable_path = settings.browser_path
                # bundled Python 沒裝 playwright chromium,不帶會 launch 失敗
                _launch_kw = dict(
                    headless=False,
                    args=get_launch_args(headless=False, extra=[
                        "--window-position=-2400,-2400",
                        "--window-size=1280,800",
                    ]),
                    ignore_default_args=get_ignore_default_args(headless=False),
                )
                _exe = _get_browser_exe_path()
                if _exe:
                    _launch_kw["executable_path"] = _exe
                browser = await p.chromium.launch(**_launch_kw)
                page = await browser.new_page()

                # v6.0.75:不再攔截 image — 我們需要從 DOM 拿閒魚商品圖片 URL(尺寸/材質可能在圖內)
                # 只攔 media/font 還能加速
                await page.route("**/*", functools.partial(
                    _safe_route_handler, block_types={"media", "font"}
                ))

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # 关闭登录弹窗（闲鱼经常弹出）
                try:
                    close_btn = page.locator('div.login-dialog-close, .sufei-dialog-close, [class*="close"]').first
                    if await close_btn.is_visible(timeout=2000):
                        await close_btn.click()
                        await page.wait_for_timeout(500)
                except Exception:
                    pass

                # 等待页面内容渲染
                await page.wait_for_timeout(3000)

                text = await page.evaluate("document.body.innerText || ''")
                title = await page.title()
                text = text or ""

                # v6.0.75:抓閒魚商品圖片 URL(全部,不限數量)
                try:
                    img_urls = await page.evaluate("""() => {
                        const urls = new Set();
                        // 商品主圖區(各種閒魚 PC 端結構)
                        const sels = [
                            'div[class*="ImageZoom"] img',
                            'div[class*="image"] img',
                            'img[src*="alicdn"]',
                            'img[src*="taobaocdn"]',
                            '.swiper-slide img',
                            'div[class*="item-detail"] img',
                            'div[class*="banner"] img',
                        ];
                        for (const sel of sels) {
                            for (const im of document.querySelectorAll(sel)) {
                                const u = im.currentSrc || im.src || '';
                                if (!u) continue;
                                // 過濾頭像 / 圖標 / icon
                                if (u.includes('avatar') || u.includes('icon') ||
                                    u.includes('logo') || u.includes('.svg')) continue;
                                if (u.startsWith('http')) urls.add(u.split('?')[0]);
                            }
                        }
                        return Array.from(urls);  // 全部圖,不限數量
                    }""")
                    if not isinstance(img_urls, list):
                        img_urls = []
                except Exception:
                    img_urls = []

                # 提取商品标题（页面 title 格式："商品名_闲鱼"）
                item_title = title.replace("_闲鱼", "").strip() if title else ""

                base = {"title": item_title, "page_text": text[:4000], "image_urls": img_urls}

                if "糟糕！宝贝被删掉了" in text or "页面不存在" in text:
                    return {**base, "status": "已删除", "can_buy": "否"}
                if "已下架" in text:
                    return {**base, "status": "已下架", "can_buy": "否"}
                if "已售出" in text or "已卖出" in text or "卖掉了" in text:
                    return {**base, "status": "已售出", "can_buy": "否"}
                if "¥" in text and item_title:
                    return {**base, "status": "在售", "can_buy": "是"}

                return {**base, "status": "未知", "can_buy": "未知"}
        except Exception as e:
            return {"status": f"检查失败: {str(e)[:80]}", "can_buy": "未知"}
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    def _get(self, conv_id: str) -> Optional[ConversationState]:
        with self._lock:
            return self._convs.get(conv_id)

    def _set_phase(self, conv_id: str, phase: ConvPhase, **kwargs) -> None:
        with self._lock:
            conv = self._convs.get(conv_id)
            if not conv:
                return
            prev_phase = conv.phase
            conv.phase = phase
            conv.updated_ts = time.time()
            # v6.1.65 Fix A:首次進 DONE 紀錄時間戳(reattach 24h window 用)
            # 只在「非 DONE → DONE」這個 transition 寫,避免重複 reattach 後又 set 新值
            if phase == ConvPhase.DONE and prev_phase != ConvPhase.DONE:
                conv.done_at_ts = time.time()
            for k, v in kwargs.items():
                if hasattr(conv, k):
                    setattr(conv, k, v)

        # v6.1.27:訓練數據紀錄 — 階段轉換(訓練端可重建完整狀態軌跡)
        if prev_phase != phase:
            _tc_record(
                "phase:transition",
                conv=conv,
                input={"from": str(prev_phase), "to": str(phase)},
                output=str(phase),
                metadata={"kwargs_keys": list(kwargs.keys())},
            )

        # v6.1.51:進 DONE/EXPIRED/ERROR 統一取消所有賣家相關 timer
        # 修「用戶用 reply: 直接回買家後,賣家 polling timer 還在跑 → race 整合到已結束 conv」bug
        # 也修「賣家補發 debounce timer 在 conv 結束後仍 fire」bug
        if phase in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR):
            if getattr(conv, "seller_check_timer", None):
                try:
                    conv.seller_check_timer.cancel()
                except Exception:
                    pass
                conv.seller_check_timer = None
            if getattr(conv, "seller_reply_debounce_timer", None):
                try:
                    conv.seller_reply_debounce_timer.cancel()
                except Exception:
                    pass
                conv.seller_reply_debounce_timer = None
            # v6.1.51:重置 auto_ask_fallback,避免訓練/persist 看到過期的 flag
            # 修 Mercari `auto_ask_fallback` 在 manual reply 後沒重置 bug
            try:
                conv.auto_ask_fallback = False
            except Exception:
                pass

        # v6.0.83 持久化:結束(DONE/EXPIRED/ERROR)就刪檔,其他狀態寫盤
        if phase in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR):
            self._delete_persisted_conv(conv_id)
            # v6.1.27:對話真正結束(DONE/EXPIRED)→ 打包該 conv 所有 events 上傳成單一 jsonl
            # ERROR 不 finalize(等 retry 或 EXPIRED 觸發),避免切斷 trajectory
            if _TC is not None and prev_phase != phase:
                if phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
                    # v6.1.27 補強:對話結束時記 conv→order_id 對應,供 14 天後 outcome 查詢
                    try:
                        _ck = _TC._build_conv_key(conv)
                        _yahoo_id = ""
                        try:
                            if conv.product_urls:
                                _yahoo_id = conv.product_urls[0].get("yahoo_id", "")
                        except Exception:
                            pass
                        _TC.register_conv_outcome(
                            _ck,
                            yahoo_order_id=_yahoo_id,
                            profile_id=conv.profile_id,
                            buyer_label=conv.buyer_label,
                        )
                    except Exception:
                        pass
                    try:
                        _TC.finalize_conv(conv, reason=phase.value)
                    except Exception:
                        pass
                    try:
                        _TC.clear_conv_cache_by_conv(conv)
                    except Exception:
                        pass
        else:
            # v6.1.45:phase 真的有變 → 強制 persist(bypass throttle),確保 disk 跟 in-memory 一致
            # 修「_set_phase(WAIT_SELLER) 在 _set_phase(AUTO_ASKING_SELLER) 後 <5s 內被 throttle 跳過,
            # 導致 disk 卡在 AUTO_ASKING_SELLER + 內部 fallback=true 矛盾」bug
            if prev_phase != phase:
                try:
                    if hasattr(self, "_last_persist_ts"):
                        self._last_persist_ts.pop(conv_id, None)
                except Exception:
                    pass
            self._persist_conv(conv)

        # 对话结束后,提醒用户还有其他等待中的对话
        # v6.0.74:如果 conv 设了 _skip_remind_on_done (即将跑 _auto_send_to_yahoo),
        # 跳过此处的 remind,改由 _auto_send_to_yahoo 完成后手动合并发送(避免顺序错乱)
        if phase in (ConvPhase.DONE, ConvPhase.EXPIRED):
            # v6.0.75:清理 WS dispatcher 映射(list 中 remove 自己,空了才 pop 整個 key)
            try:
                with self._ws_map_lock:
                    if conv.seller_session_id:
                        cid = f"{conv.seller_session_id}@goofish"
                        lst = self._ws_cid_to_conv.get(cid)
                        if lst and conv_id in lst:
                            lst.remove(conv_id)
                            if not lst:
                                self._ws_cid_to_conv.pop(cid, None)
                    if conv.seller_peer_user_id:
                        lst2 = self._ws_peer_to_conv.get(conv.seller_peer_user_id)
                        if lst2 and conv_id in lst2:
                            lst2.remove(conv_id)
                            if not lst2:
                                self._ws_peer_to_conv.pop(conv.seller_peer_user_id, None)
            except Exception:
                pass
            if conv.seller_check_timer:
                try:
                    conv.seller_check_timer.cancel()
                except Exception:
                    pass
                conv.seller_check_timer = None

            # v6.0.83:phase 切換時也取消賣家回覆 debounce timer
            # (用戶 skip / DONE / EXPIRED 後不應再觸發整合,以免覆蓋已處理的草稿)
            if conv.seller_reply_debounce_timer:
                try:
                    conv.seller_reply_debounce_timer.cancel()
                except Exception:
                    pass
                conv.seller_reply_debounce_timer = None
                # 清 buffer 避免下次又被用到舊資料
                conv.seller_reply_buffer.clear()

            if getattr(conv, '_skip_remind_on_done', False):
                conv._skip_remind_on_done = False  # 清除 flag,避免下次卡住
                return
            self._remind_pending_convs()

    def _remind_pending_convs(self, prefix: str = "") -> str:
        """对话结束后,检查是否还有其他需要用户操作的对话,有则提醒。
        v6.0.74:加 prefix 参数支持合并到其他消息(如发送成功通知),
        若 prefix 非空则返回拼接后的提醒文字而不发送;空 prefix 时直接发送。
        """
        need_action_phases = (
            ConvPhase.PREVIEW_SENT,
            ConvPhase.PREVIEW_SELLER_QUESTION,
            ConvPhase.PREVIEW_SELLER,
            ConvPhase.WAIT_SELLER,
        )
        with self._lock:
            pending = [
                c for c in self._convs.values()
                if c.phase in need_action_phases
            ]
        if not pending:
            return ""
        pending.sort(key=lambda c: c.updated_ts)
        nxt = pending[0]
        phase_hint = {
            ConvPhase.PREVIEW_SENT: "等待确认 AI 回复",
            ConvPhase.PREVIEW_SELLER_QUESTION: "等待确认问卖家的问题",
            ConvPhase.PREVIEW_SELLER: "等待确认整合回复",
            ConvPhase.WAIT_SELLER: "等待提供卖家答案",
        }
        hint = phase_hint.get(nxt.phase, "等待处理")
        total = len(pending)
        msg = f"📋 还有 {total} 个对话等待处理\n"
        msg += f"下一个:买家 {nxt.buyer_label} ({hint})"
        if total > 1:
            msg += f"\n其余 {total - 1} 个排队中"

        # 若 prefix 非空,合并返回(不发);否则直接发
        if prefix:
            return f"{prefix}\n\n{msg}"
        self.tg.send(msg)
        return ""

    def _get_sorted_active_convs(self) -> List[ConversationState]:
        """返回按优先级排序的活跃对话列表。索引+1 即为 #N 编号。"""
        phase_priority = {
            ConvPhase.ERROR: 0,
            ConvPhase.PREVIEW_SELLER_QUESTION: 1,
            ConvPhase.PREVIEW_SENT: 2,
            ConvPhase.PREVIEW_SELLER: 3,
            ConvPhase.WAIT_SELLER: 4,
            ConvPhase.AUTO_ASKING_SELLER: 5,
        }
        with self._lock:
            candidates = [
                c for c in self._convs.values()
                if c.phase in phase_priority
            ]
        if not candidates:
            return []
        candidates.sort(key=lambda c: (phase_priority[c.phase], c.updated_ts))
        return candidates

    def _find_active_conv(self) -> Optional[ConversationState]:
        """找到最需要用户操作的活跃对话（优先级最高的）。"""
        convs = self._get_sorted_active_convs()
        return convs[0] if convs else None

    def _find_conv_by_index(self, idx: int) -> Optional[ConversationState]:
        """按编号找对话。idx 从 1 开始。"""
        convs = self._get_sorted_active_convs()
        if 1 <= idx <= len(convs):
            return convs[idx - 1]
        return None

    def _find_conv_by_tg_msg_id(self, tg_msg_id: int) -> Optional[ConversationState]:
        """通过 TG 消息 ID 找到对应的活跃对话（用于引用回复定位）。"""
        phase_active = {
            ConvPhase.PENDING_AI,
            ConvPhase.ERROR,
            ConvPhase.PREVIEW_SELLER_QUESTION,
            ConvPhase.PREVIEW_SENT,
            ConvPhase.PREVIEW_SELLER,
            ConvPhase.WAIT_SELLER,
            ConvPhase.AUTO_ASKING_SELLER,
        }
        with self._lock:
            for c in self._convs.values():
                if tg_msg_id in c.tg_msg_ids and c.phase in phase_active:
                    return c
        return None

    def _auto_send_to_yahoo(self, conv: ConversationState) -> None:
        """后台线程：纯 HTTP 自动发送回复到 Yahoo IM（不回退浏览器）。

        顺序模拟人的操作: 消红点(=打开聊天窗口) → 短延迟(=阅读消息) → 发送回复
        """

        lock = self._get_profile_send_lock(conv.profile_id)
        with lock:
            try:
                if conv.shop_code and conv.chat_id:
                    from core.im_http_ops import im_send_message, im_mark_read, build_channel_id
                    from core.human import human_jitter_ms
                    profile_dir = Path(self._base_dir) / "profiles" / conv.profile_id
                    channel_id = build_channel_id(conv.shop_code, conv.chat_id)

                    # 1. 先消红点（= 人点进聊天窗口）
                    try:
                        ok_rd, info_rd = im_mark_read(
                            profile_dir, channel_id,
                            buyer_cid=conv.chat_id,
                            on_log=self.on_log,
                        )
                        if ok_rd:
                            self.on_log(f"[TG] HTTP 消红点 OK")
                            # v6.1.48:通知 monitor 同步 reset snapshot,
                            # 修「買家秒回漏訊息」bug(我們 mark_read 後 Yahoo
                            # unread=0,但 monitor snapshot 還是舊值,買家秒回升回
                            # 同樣值 → diff=0 → 永久漏推)
                            try:
                                if self.monitor and hasattr(self.monitor, 'notify_channel_marked_read'):
                                    self.monitor.notify_channel_marked_read(conv.profile_id, channel_id)
                            except Exception:
                                pass
                        else:
                            self.on_log(f"[TG] HTTP 消红点 fail (非致命): {info_rd}")
                    except Exception as exc_rd:
                        self.on_log(f"[TG] HTTP 消红点 error (非致命): {exc_rd}")

                    # 2. 短延迟（= 人阅读消息 + 打字）
                    import time
                    time.sleep(human_jitter_ms(2000) / 1000.0)

                    # 3. 发送消息
                    ok_http, info_http = im_send_message(
                        profile_dir, channel_id,
                        receiver=conv.chat_id,
                        message=conv.final_reply,
                        buyer_cid=conv.chat_id,
                        on_log=self.on_log,
                    )
                    if ok_http:
                        # v6.0.83:成功反饋走 _conv_aware_send,有 topic 就 push 到 topic
                        _success_msg = (
                            f"✅ 已發送到 Yahoo IM\n"
                            f"「{conv.final_reply}」"
                        )
                        self._conv_aware_send(conv, _success_msg)
                        # 排队提醒(其他 pending convs)還是用私聊,不混入 topic
                        try:
                            _reminder = self._remind_pending_convs(prefix="")
                            if _reminder:
                                self._conv_aware_send(conv, _reminder)
                        except Exception:
                            pass
                        self.on_log(f"[TG] Auto-send(HTTP) success: {conv.buyer_label} | {info_http}")
                        # v6.1.27:訓練數據紀錄 — 發送成功(最終出口,確認使用者選擇真的執行)
                        _tc_record(
                            "send:yahoo",
                            conv=conv,
                            input={"final_reply": conv.final_reply},
                            output={"ok": True, "info": str(info_http)[:200]},
                            metadata={"channel": "yahoo_im_http"},
                        )
                        try:
                            if self.monitor and hasattr(self.monitor, 'reset_im_count'):
                                self.monitor.reset_im_count(conv.profile_id)
                                self.on_log(f"[TG] Reset IM count for {conv.profile_id}")
                        except Exception:
                            pass
                    else:
                        self.on_log(f"[TG] HTTP send failed: {info_http}")
                        self._conv_aware_send(conv,
                            f"⚠️ 自动发送失败:{info_http}\n\n"
                            f"请手动复制到 Yahoo IM:\n「{conv.final_reply}」"
                        )
                        # v6.1.27:訓練數據紀錄 — 發送失敗(訓練端可看 retry 模式)
                        _tc_record(
                            "send:yahoo",
                            conv=conv,
                            input={"final_reply": conv.final_reply},
                            output={"ok": False, "info": str(info_http)[:200]},
                            metadata={"channel": "yahoo_im_http", "failed": True},
                        )
                        # 失败也要提醒排队
                        self._remind_pending_convs()
                else:
                    self.on_log(f"[TG] HTTP send skip: shop_code={conv.shop_code}, chat_id={conv.chat_id}")
                    self._conv_aware_send(conv, 
                        f"⚠️ 自动发送失败：缺少 shop_code 或 chat_id\n\n"
                        f"请手动复制到 Yahoo IM：\n「{conv.final_reply}」"
                    )
            except Exception as e:
                self.on_log(f"[TG] Auto-send exception: {e}")
                self._conv_aware_send(conv, 
                    f"⚠️ 自动发送异常：{str(e)[:150]}\n\n"
                    f"请手动复制到 Yahoo IM：\n「{conv.final_reply}」"
                )

    def _mark_read_yahoo(self, profile_id: str, chat_url: str, account_name: str,
                         shop_code: str = "", chat_id: str = "") -> None:
        """后台线程：HTTP 消红点（putReadInfo + putLastAccessedTs）。"""
        try:
            from core.im_http_ops import im_mark_read, build_channel_id
            profile_dir = Path(self._base_dir) / "profiles" / profile_id
            if shop_code and chat_id:
                channel_id = build_channel_id(shop_code, chat_id)
                self.on_log(f"[TG] Mark-read(HTTP): {account_name} ch={channel_id}")
                ok, info = im_mark_read(
                    profile_dir, channel_id,
                    buyer_cid=chat_id,
                    on_log=self.on_log,
                )
                if ok:
                    self.on_log(f"[TG] 已消红点(HTTP): {account_name}")
                    self.tg.send(f"✅ 已消红点：{account_name}")
                    try:
                        if self.monitor and hasattr(self.monitor, 'reset_im_count'):
                            self.monitor.reset_im_count(profile_id)
                    except Exception:
                        pass
                    # v6.1.48:通知 monitor 同步 reset snapshot,
                    # 修「買家秒回漏訊息」bug(我們 mark_read 後 Yahoo unread=0,
                    # 但 monitor snapshot 還是舊值,買家秒回升回同樣值 → diff=0
                    # → 永久漏推)
                    try:
                        if self.monitor and hasattr(self.monitor, 'notify_channel_marked_read'):
                            self.monitor.notify_channel_marked_read(profile_id, channel_id)
                    except Exception:
                        pass
                else:
                    self.on_log(f"[TG] 消红点失败: {info}")
                    self.tg.send(f"⚠️ 消红点失败：{info[:150]}")
            else:
                self.on_log(f"[TG] 消红点跳过: shop_code={shop_code} chat_id={chat_id} 为空")
                self.tg.send("⚠️ 消红点跳过：缺少 shop_code 或 chat_id")
        except Exception as e:
            self.on_log(f"[TG] 消红点异常: {e}")
            self.tg.send(f"⚠️ 消红点异常：{str(e)[:150]}")

    def _cleanup_expired(self) -> None:
        """清理超时的对话。"""
        now = time.time()
        expired_ids = []
        with self._lock:
            for cid, conv in self._convs.items():
                if conv.phase in (ConvPhase.DONE, ConvPhase.EXPIRED, ConvPhase.ERROR):
                    # 已结束的对话保留 10 分钟后清理
                    if now - conv.updated_ts > 600:
                        expired_ids.append(cid)
                else:
                    # PREVIEW phase 跟其他 phase 一樣走 8h(EXPIRE_SEC)
                    # 之前 15min 太短,使用者忙完手上事再回來時草稿已被丟,只能手打
                    # 問賣家 phase 走 24h(賣家可能慢回)
                    if conv.phase == ConvPhase.AUTO_ASKING_SELLER:
                        timeout_sec = 86400  # 24 小時(問賣家)
                    else:
                        timeout_sec = EXPIRE_SEC  # 8 小時

                    if (now - conv.updated_ts) <= timeout_sec:
                        continue

                    conv.phase = ConvPhase.EXPIRED
                    conv.updated_ts = now
                    expired_ids.append(cid)
                    # v6.0.83:超時提示 push 到 buyer topic 而非私聊(如有 forum)
                    expire_msg = (
                        f"⏰ *對話已超時*({timeout_sec // 60} 分鐘未處理)\n"
                        f"買家:{conv.buyer_label}\n"
                        f"帳號:`{conv.account_name}`"
                    )
                    topic_id = self._find_topic_for_conv(conv) if self.forum_bridge else 0
                    if topic_id:
                        try:
                            self.forum_bridge.bot._post("sendMessage", {
                                "chat_id": self.forum_bridge.bot.forum_chat_id,
                                "message_thread_id": topic_id,
                                "text": expire_msg,
                                "parse_mode": "Markdown",
                            })
                        except Exception:
                            pass
                    else:
                        try:
                            self.tg.send(expire_msg)
                        except Exception:
                            pass
            for cid in expired_ids:
                conv = self._convs.pop(cid, None)
                if conv:
                    key = f"{conv.profile_id}|{conv.chat_id}"
                    self._chat_map.pop(key, None)
                    # 順帶清持久化檔
                    try:
                        self._delete_persisted_conv(cid)
                    except Exception:
                        pass


# ---------- Yahoo IM 自动发送 ----------

async def _send_to_yahoo_im_async(
    *,
    chrome_path: str,
    profile_dir: str,
    chat_url: str,
    message: str,
    buyer_id: str = "",
    buyer_label: str = "",
    shop_id: str = "",
    headless: bool = True,
) -> Tuple[bool, str]:
    """发送 Yahoo IM 消息。

    通过 Playwright 打开聊天页面发送消息，发送后导航回 myauc 页面，
    让 IM 连接正常断开，确保后续买家新消息能正常显示红点。
    """
    _install_pw_silence_handler()
    from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

    _hl = bool(headless)
    _launch_args = dict(
        user_data_dir=profile_dir,
        executable_path=chrome_path,
        headless=False,
        no_viewport=True,   # 不设 viewport（Patchright 兼容）
        args=get_launch_args(headless=_hl, extra=["--window-size=1280,800"] if _hl else None),
        ignore_default_args=get_ignore_default_args(headless=_hl),
    )

    def _clean_singleton_files():
        for _f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            for _base in (profile_dir, os.path.join(profile_dir, "Default")):
                _fp = os.path.join(_base, _f)
                try:
                    if os.path.exists(_fp):
                        os.remove(_fp)
                except Exception:
                    pass

    _RETRYABLE_ERRORS = ("window not found", "target page", "has been closed",
                         "target closed", "browser has been closed",
                         "context or browser")
    _MAX_RETRIES = 2

    last_err = None
    for _attempt in range(_MAX_RETRIES + 1):
        ctx = None
        try:
            async with async_playwright() as p:
                if _attempt > 0:
                    _clean_singleton_files()
                    await asyncio.sleep(1.5 + _attempt * 0.5)

                ctx = await p.chromium.launch_persistent_context(**_launch_args)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await apply_runtime_normalization_async(ctx)
                return await _send_via_chat_page(page, chat_url, message,
                                                  buyer_id=buyer_id,
                                                  buyer_label=buyer_label)

        except Exception as e:
            last_err = e
            err_lower = str(e).lower()
            retryable = any(kw in err_lower for kw in _RETRYABLE_ERRORS)
            if retryable and _attempt < _MAX_RETRIES:
                # 可重试错误：清理残留后再来一次
                continue
            return False, str(e)[:200]
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass

    return False, f"重试 {_MAX_RETRIES} 次仍失败：{str(last_err)[:150]}"


async def _select_buyer_in_chat_list(page, buyer_id: str, buyer_label: str) -> Tuple[bool, str]:
    """通过 React fiber 精确定位并选择目标买家对话。

    使用 React fiber 的 channel.id 匹配买家 Y-ID（由 yahoo_im_fulltext.py 提取），
    然后点击对应侧栏行切换到正确对话，解决发错人的根本问题。
    """
    if not buyer_id and not buyer_label:
        return False, "无buyer_id和buyer_label"

    # 等待侧栏列表渲染（SPA 需要时间，Yahoo chat 是重型 SPA）
    _sidebar_found = False
    _sidebar_selectors = [
        'li[class*="list__"]', 'li[class*="chatList"]',
        'li[class*="item__"]', '[class*="conversation"] li',
        'div[class*="channelList"] li', 'ul li',
    ]
    for _sel in _sidebar_selectors:
        try:
            await page.wait_for_selector(_sel, timeout=6000)
            _sidebar_found = True
            break
        except Exception:
            continue

    if not _sidebar_found:
        # SPA 可能还在加载，额外等待
        await page.wait_for_timeout(4000)
    else:
        await page.wait_for_timeout(2000)  # React hydration 额外等待

    # ── 策略 1：React fiber 精确匹配 buyer_id（带重试） ──
    _fiber_rows_n = 0  # 用于诊断日志
    if buyer_id:
        _fiber_js = r"""
(targetBuyerId) => {
  const shopMatch = (location.pathname || '').match(/\/chat\/(Y?\d+)/i);
  const shopId = shopMatch ? shopMatch[1].toLowerCase().replace(/^y/, '') : '';
  const target = targetBuyerId.toLowerCase().replace(/^y/, '');

  if (target === shopId) {
    return { status: 'ambiguous', msg: 'buyer_id equals shop code' };
  }

  // 多种选择器尝试找侧栏行
  let rows = document.querySelectorAll('li[class*="list__"]');
  if (!rows.length) rows = document.querySelectorAll('li[class*="chatList"]');
  if (!rows.length) rows = document.querySelectorAll('li[class*="item__"]');
  if (!rows.length) rows = document.querySelectorAll('[class*="conversation"] li');
  if (!rows.length) rows = document.querySelectorAll('div[class*="channelList"] li');
  // 兜底: 找侧栏中所有 li
  if (!rows.length) {
    const sidebar = document.querySelector('aside') || document.querySelector('nav') ||
                    document.querySelector('[class*="sidebar"]') || document.querySelector('[class*="Sidebar"]');
    if (sidebar) rows = sidebar.querySelectorAll('li');
  }

  let rowCount = rows.length;
  // 收集所有行的用户名（用于诊断）
  let names = [];
  for (const row of rows) {
    const ne = row.querySelector('div[class*="userName"]') ||
               row.querySelector('div[class*="nick"]') ||
               row.querySelector('[class*="name"]');
    if (ne) names.push((ne.innerText || '').trim().substring(0, 20));

    const rk = Object.keys(row).find(k => k.startsWith('__reactFiber'));
    if (!rk) continue;
    let fiber = row[rk];
    let matched = false;
    for (let d = 0; d < 10 && fiber; d++) {
      const p = fiber.memoizedProps || fiber.pendingProps || {};
      const ch = p.channel || p.data || p.item || p.conversation || null;
      if (ch && typeof ch === 'object') {
        const chId = (ch.id || ch.channelId || ch.chatId || '').toLowerCase();
        const parts = chId.split(':');
        for (const pt of parts) {
          if (pt === 'y' + target) { matched = true; break; }
        }
        if (matched) break;
      }
      fiber = fiber.return;
    }
    if (matched) {
      const link = row.querySelector('a') || row;
      link.click();
      return { status: 'clicked', msg: 'fiber matched ' + targetBuyerId, rows: rowCount };
    }
  }
  return { status: 'not_found', msg: 'buyer not found', rows: rowCount,
           url: location.href, names: names.slice(0, 8).join(',') };
}
"""
        for attempt in range(4):
            try:
                result = await page.evaluate(_fiber_js, buyer_id)
                status = (result or {}).get("status", "")
                msg = (result or {}).get("msg", "")
                rows_n = (result or {}).get("rows", 0)
                _fiber_rows_n = rows_n
                if status == "clicked":
                    await page.wait_for_timeout(1500)  # 等待对话切换
                    return True, f"fiber定位成功({msg})"
                if status == "ambiguous":
                    break  # buyer_id=shop_code, 无法区分，跳到 label
                # not_found → 等待后重试（SPA 可能还在渲染）
                log.info("[IM-send] fiber attempt %d/%d: rows=%d, names=%s, url=%s",
                         attempt + 1, 4, rows_n,
                         (result or {}).get("names", ""),
                         (result or {}).get("url", "")[:80])
                if attempt < 3:
                    # 递增等待（SPA 首次可能需要很久）
                    await page.wait_for_timeout(2500 + attempt * 1000)
            except Exception as _fib_err:
                log.warning("[IM-send] fiber attempt %d error: %s", attempt + 1, _fib_err)
                if attempt < 3:
                    await page.wait_for_timeout(2500)

    # ── 策略 2：buyer_label 匹配侧栏用户名（Unicode 归一化） ──
    if buyer_label:
        _label_js = r"""
(targetLabel) => {
  // Unicode 归一化: 全角→半角, 去首尾空白
  function normalize(s) {
    return s.replace(/[\uFF01-\uFF5E]/g, c =>
      String.fromCharCode(c.charCodeAt(0) - 0xFEE0)
    ).replace(/\u3000/g, ' ').trim();
  }
  const targetNorm = normalize(targetLabel);

  let rows = document.querySelectorAll('li[class*="list__"]');
  if (!rows.length) rows = document.querySelectorAll('li[class*="chatList"]');
  if (!rows.length) rows = document.querySelectorAll('li[class*="item__"]');
  if (!rows.length) rows = document.querySelectorAll('[class*="conversation"] li');
  if (!rows.length) rows = document.querySelectorAll('div[class*="channelList"] li');
  // 兜底
  if (!rows.length) {
    const sidebar = document.querySelector('aside') || document.querySelector('nav') ||
                    document.querySelector('[class*="sidebar"]') || document.querySelector('[class*="Sidebar"]');
    if (sidebar) rows = sidebar.querySelectorAll('li');
  }

  let rowCount = rows.length;
  for (const row of rows) {
    // 多种选择器找用户名元素
    const el = row.querySelector('div[class*="userName__"]') ||
               row.querySelector('div[class*="userName"]') ||
               row.querySelector('div[class*="nick"]') ||
               row.querySelector('[class*="name"]');
    if (el) {
      const name = (el.innerText || '').trim();
      // 精确匹配 或 归一化后匹配
      if (name === targetLabel || normalize(name) === targetNorm) {
        const link = row.querySelector('a') || row;
        link.click();
        return { status: 'clicked', name: name, rows: rowCount };
      }
    }
  }
  // 策略 2b: 在整个侧栏中用 textContent 搜索（兜底）
  const allEls = document.querySelectorAll('a, div, span');
  for (const el of allEls) {
    const t = (el.innerText || '').trim();
    if ((t === targetLabel || normalize(t) === targetNorm) && t.length < 30) {
      // 确保不是消息内容（只匹配短文本）
      el.click();
      return { status: 'clicked', name: t, rows: rowCount, method: 'global_text' };
    }
  }
  return { status: 'not_found', rows: rowCount };
}
"""
        try:
            result = await page.evaluate(_label_js, buyer_label)
            if (result or {}).get("status") == "clicked":
                await page.wait_for_timeout(1500)
                _method = (result or {}).get("method", "label")
                return True, f"label定位成功({(result or {}).get('name', '')}, {_method})"
        except Exception:
            pass

    return False, f"无法定位买家: id={buyer_id}, label={buyer_label}, sidebar_rows={_fiber_rows_n}"


async def _send_via_chat_page(page, chat_url: str, message: str,
                              buyer_id: str = "", buyer_label: str = "") -> Tuple[bool, str]:
    """打开聊天页面发送消息。

    尽量缩短在聊天页面的停留时间，因为停留期间买家发的新消息
    会被 Yahoo 服务端自动标记已读，导致红点不出现。
    """
    # 拦截图片/字体/媒体，加速页面加载（不拦截 stylesheet，SPA 渲染需要 CSS）
    await page.route("**/*", functools.partial(
        _safe_route_handler, block_types={"image", "media", "font"}
    ))

    await page.goto(chat_url, wait_until="domcontentloaded", timeout=20000)

    # ── 检测登录重定向（session 过期时 Yahoo 会跳转到 login） ──
    _cur_url = page.url or ""
    if "login" in _cur_url.lower() or "signin" in _cur_url.lower():
        return False, f"session过期(重定向到login): {_cur_url[:100]}"

    input_sel = (
        'textarea[placeholder*="訊息"], input[placeholder*="訊息"], '
        'textarea[placeholder*="消息"], input[placeholder*="消息"], textarea'
    )
    try:
        await page.wait_for_selector(input_sel, timeout=8000)
    except Exception:
        # 输入框未出现 — 再检查一次 URL（可能是延迟跳转）
        _cur_url2 = page.url or ""
        if "login" in _cur_url2.lower() or "signin" in _cur_url2.lower():
            return False, f"session过期(延迟重定向login): {_cur_url2[:100]}"

    # ---- 验证当前对话是目标买家（必须成功才发送） ----
    if buyer_id or buyer_label:
        sel_ok, sel_info = await _select_buyer_in_chat_list(page, buyer_id, buyer_label)
        if not sel_ok:
            # ── 重试：刷新页面后再来一次（SPA 首次加载可能不完整） ──
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
                sel_ok2, sel_info2 = await _select_buyer_in_chat_list(page, buyer_id, buyer_label)
                if sel_ok2:
                    sel_ok, sel_info = sel_ok2, sel_info2
                else:
                    sel_info = f"{sel_info} → reload后仍失败: {sel_info2}"
            except Exception as _reload_err:
                sel_info = f"{sel_info} → reload异常: {_reload_err}"
            if not sel_ok:
                return False, f"无法定位目标买家: {sel_info}"
        # 成功切换，重新等待输入框
        try:
            await page.wait_for_selector(input_sel, timeout=5000)
        except Exception:
            pass

    # 关闭"假买家"弹窗（多种文字匹配）
    for _ in range(3):
        closed = False
        try:
            for cb_text in ["我已詳讀相關資訊", "我已詳閱相關資訊", "我已詳閱", "我已詳讀", "我已閱讀"]:
                cb = page.locator(f"text={cb_text}").first
                if await cb.is_visible(timeout=500):
                    await cb.click()
                    await page.wait_for_timeout(300)
                    closed = True
                    break
            for btn_text in ["我知道了", "確認", "關閉", "確定"]:
                btn = page.locator(f"text={btn_text}").first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    await page.wait_for_timeout(500)
                    closed = True
                    break
        except Exception:
            pass
        if not closed:
            break
        await page.wait_for_timeout(300)

    input_el = await page.query_selector(input_sel)
    if not input_el:
        return False, "找不到输入框"

    try:
        await input_el.click(timeout=5000)
    except Exception:
        try:
            await input_el.click(force=True)
        except Exception:
            # force click 也失败（如 element outside viewport），改用 JS
            try:
                await page.evaluate(
                    "(el) => { el.scrollIntoView({block:'center'}); el.focus(); el.click(); }",
                    input_el,
                )
            except Exception:
                pass  # fill() 下面会自动 focus
    await input_el.fill(message)
    await page.wait_for_timeout(100)
    await input_el.press("Enter")

    # 等待消息发出
    await page.wait_for_timeout(800)

    # 验证：输入框应该被清空（发送成功后）
    try:
        remaining = await input_el.input_value()
        if remaining and remaining.strip():
            return False, "消息未发出（输入框未清空，可能被弹窗阻挡）"
    except Exception:
        return False, "发送验证失败（无法读取输入框状态）"

    # 立即离开，切断 IM 连接
    try:
        await page.goto("about:blank", timeout=3000)
    except Exception:
        pass

    return True, "消息已发送(聊天页面)"


def _send_yahoo_im_sync(
    *,
    chrome_path: str,
    profile_id: str,
    chat_url: str,
    message: str,
    buyer_id: str = "",
    buyer_label: str = "",
    shop_id: str = "",
    base_dir: str = "",
    max_wait_sec: int = 90,
) -> Tuple[bool, str]:
    """同步发送消息到 Yahoo IM，处理 profile 锁。"""
    if not chrome_path:
        return False, "缺少 Chrome 路径"
    if not chat_url:
        return False, "缺少聊天链接"
    if not message:
        return False, "消息为空"

    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    prof_dir = base / "profiles" / (profile_id or "")
    prof_dir.mkdir(parents=True, exist_ok=True)

    # 等待 profile 释放
    waited = 0
    acquired = False
    while waited < max_wait_sec:
        in_use, _ = detect_chrome_profile_in_use(prof_dir)
        if not in_use:
            ok, reason = try_acquire(prof_dir, owner="yahoo-im-send")
            if ok:
                acquired = True
                break
        time.sleep(5)
        waited += 5

    if not acquired:
        return False, f"等待 Profile 释放超时（{max_wait_sec}s）"

    try:
        success, info = asyncio.run(
            _send_to_yahoo_im_async(
                chrome_path=chrome_path,
                profile_dir=str(prof_dir),
                chat_url=chat_url,
                message=message,
                buyer_id=buyer_id,
                buyer_label=buyer_label,
                shop_id=shop_id,
                headless=True,
            )
        )
        return success, info
    except Exception as e:
        return False, str(e)[:200]
    finally:
        release(prof_dir)


def _mark_read_yahoo_im_sync(
    *,
    chrome_path: str,
    profile_id: str,
    chat_url: str,
    base_dir: str = "",
    max_wait_sec: int = 60,
) -> Tuple[bool, str]:
    """只打开聊天页面消红点，不发消息。"""
    if not chrome_path or not chat_url:
        return False, "缺少参数"

    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    prof_dir = base / "profiles" / (profile_id or "")
    prof_dir.mkdir(parents=True, exist_ok=True)

    waited = 0
    acquired = False
    while waited < max_wait_sec:
        in_use, _ = detect_chrome_profile_in_use(prof_dir)
        if not in_use:
            ok, reason = try_acquire(prof_dir, owner="yahoo-im-read")
            if ok:
                acquired = True
                break
        time.sleep(5)
        waited += 5

    if not acquired:
        return False, f"等待 Profile 释放超时（{max_wait_sec}s）"

    try:
        success, info = asyncio.run(
            _open_chat_read_only_async(
                chrome_path=chrome_path,
                profile_dir=str(prof_dir),
                chat_url=chat_url,
            )
        )
        return success, info
    except Exception as e:
        return False, str(e)[:200]
    finally:
        release(prof_dir)


async def _open_chat_read_only_async(
    *,
    chrome_path: str,
    profile_dir: str,
    chat_url: str,
) -> Tuple[bool, str]:
    """打开聊天页面等几秒消红点，不发消息。"""
    _install_pw_silence_handler()
    from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

    _launch_kw = dict(
        user_data_dir=profile_dir,
        executable_path=chrome_path,
        headless=False,
        no_viewport=True,   # 不设 viewport（Patchright 兼容）
        args=get_launch_args(headless=True, extra=["--window-size=1280,800"]),
        ignore_default_args=get_ignore_default_args(headless=True),
    )

    def _clean_singleton():
        for _f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            for _base in (profile_dir, os.path.join(profile_dir, "Default")):
                _fp = os.path.join(_base, _f)
                try:
                    if os.path.exists(_fp):
                        os.remove(_fp)
                except Exception:
                    pass

    _RETRYABLE = ("window not found", "target page", "has been closed",
                  "target closed", "browser has been closed",
                  "context or browser")
    _MAX_RETRIES = 2
    last_err = None

    for _attempt in range(_MAX_RETRIES + 1):
        ctx = None
        try:
            async with async_playwright() as pw:
                if _attempt > 0:
                    _clean_singleton()
                    await asyncio.sleep(1.5 + _attempt * 0.5)

                ctx = await pw.chromium.launch_persistent_context(**_launch_kw)
                await apply_runtime_normalization_async(ctx)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                await page.route("**/*", functools.partial(
                    _safe_route_handler,
                    block_types={"image", "media", "font"},
                ))

                await page.goto(chat_url, wait_until="domcontentloaded", timeout=20000)

                input_sel = (
                    'textarea[placeholder*="訊息"], input[placeholder*="訊息"], '
                    'textarea[placeholder*="消息"], input[placeholder*="消息"], textarea'
                )
                try:
                    await page.wait_for_selector(input_sel, timeout=10000)
                except Exception:
                    pass

                # 关闭"假买家"弹窗
                try:
                    cb = page.locator("text=我已詳閱相關資訊").first
                    if await cb.is_visible(timeout=500):
                        await cb.click()
                        await page.wait_for_timeout(150)
                        btn = page.locator("text=我知道了").first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            await page.wait_for_timeout(200)
                except Exception:
                    pass

                await page.wait_for_timeout(5000)

                try:
                    await page.goto("about:blank", timeout=3000)
                except Exception:
                    pass

                return True, "已消红点"
        except Exception as e:
            last_err = e
            err_lower = str(e).lower()
            retryable = any(kw in err_lower for kw in _RETRYABLE)
            if retryable and _attempt < _MAX_RETRIES:
                continue
            return False, str(e)[:200]
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass

    return False, f"重试 {_MAX_RETRIES} 次仍失败：{str(last_err)[:150]}"