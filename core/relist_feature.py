"""一鍵轉刊 — 從 Yahoo 公開商品 URL 抓資料,刊登到指定帳號。

v6.1.20:從小賣場 URL → 自動拉資料 → 預覽 + 編輯 → 刊登到大賣場。

完整流程(無需來源帳號 cookies):
  1. fetch_source_item(url)   → 公開頁 isoredux-data 解析 → 完整 item dict
  2. build_relist_merchandise → 轉換成 publish API 接受的 merchandise dict
                                  (支援 edits 覆寫任何欄位 + replacement images)
  3. do_relist(target_profile, source, edits, new_image_paths)
                                → 用目標帳號 session 跑 publish

TG 整合:tg_conversation.py 加 /relist URL 命令 + state machine。
"""
from __future__ import annotations

import html as _html_mod
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ── TG state machine ─────────────────────────────────────


_RELIST_STATE_PICK_TARGET = "pick_target"
_RELIST_STATE_EDITING = "editing"
_RELIST_STATE_AWAIT_INPUT = "await_input"
_RELIST_STATE_AWAIT_PHOTO = "await_photo"
_RELIST_STATE_PUBLISHING = "publishing"


@dataclass
class RelistSession:
    """單一 TG chat 的轉刊工作流狀態。"""

    chat_id: str = ""
    topic_id: int = 0  # 0=私聊, >0=forum topic
    item_id: str = ""
    source_url: str = ""
    source: Dict[str, Any] = field(default_factory=dict)
    target_account: str = ""
    target_profile_id: str = ""
    edits: Dict[str, Any] = field(default_factory=dict)
    new_image_file_ids: List[str] = field(default_factory=list)  # TG file_id 暫存
    new_image_urls: List[str] = field(default_factory=list)      # 上傳後 CDN URL
    state: str = _RELIST_STATE_PICK_TARGET
    pending_field: str = ""
    preview_msg_id: int = 0
    edit_msg_id: int = 0
    created_at: float = field(default_factory=time.time)

    def get_field(self, key: str, default: Any = None) -> Any:
        """取生效值:edits 優先,沒有就拿 source。"""
        if key in self.edits:
            return self.edits[key]
        return self.source.get(key, default)

    def is_expired(self, ttl_sec: float = 3600.0) -> bool:
        return (time.time() - self.created_at) > ttl_sec

from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import CURL_CFFI_IMPERSONATE, get_api_headers
from .ssl_helper import cffi_retry_call

LogFn = Callable[[str], None]


# ── 從 URL 抽 item ID ─────────────────────────────────────

_ID_PATTERNS = [
    re.compile(r"tw\.bid\.yahoo\.com/item/(\d{6,15})"),
    re.compile(r"auction\.yahoo\.com\.tw/item/(\d{6,15})"),
    re.compile(r"^(\d{8,15})$"),  # 純數字 ID
]


def extract_item_id(text: str) -> str:
    """從 URL 或純數字字串抽 item id。失敗回 ""。"""
    if not text:
        return ""
    text = text.strip()
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return ""


# ── Step 1:從公開頁拉 item 資料(無需 auth)─────────────

_PUBLIC_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_html_via_requests(url: str, on_log: LogFn) -> Tuple[str, str]:
    """用 Python stdlib requests 拉公開頁。

    v6.1.20.5:VPN / 中國跨境場景優先路徑。
    requests 用系統 OpenSSL + 自動讀系統代理(WinINET / HTTP_PROXY 環境變數),
    跟瀏覽器同 SSL stack → 瀏覽器能開,requests 大概率也能開。
    ssl_helper 已包裝 requests.get 做 3 次 SSL/連線重試。
    """
    import requests as _stdlib_requests
    headers = dict(_PUBLIC_HEADERS)
    headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )
    last_err = "未嘗試"
    for attempt in range(3):
        try:
            r = _stdlib_requests.get(url, headers=headers, timeout=25)
            if r.status_code == 200:
                return r.text, ""
            last_err = f"HTTP {r.status_code}"
            if r.status_code in (429, 503, 502):
                time.sleep(4 + attempt * 3)
                continue
            return "", last_err  # 4xx / 其他 5xx 不重試
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            if attempt < 2:
                on_log(f"[RELIST] requests retry #{attempt + 1}: {last_err}")
                time.sleep(3 + attempt * 4)  # 3s / 7s
    return "", last_err


def _fetch_html_via_curl_cffi(url: str, on_log: LogFn) -> Tuple[str, str]:
    """用 curl_cffi (Chrome TLS 偽裝) 拉公開頁 — fallback 給 requests 也失敗時。

    場景:Yahoo 加 client compat 阻擋 stdlib requests,但 Chrome 偽裝可替代路径。
    timeout 20s × max_retries 2 = 最多 ~60s + backoff。
    """
    s = CffiSession(impersonate=CURL_CFFI_IMPERSONATE)
    s.headers.update(_PUBLIC_HEADERS)
    try:
        r = cffi_retry_call(
            s.get, url, timeout=20, max_retries=2,
            on_retry=lambda att, e: on_log(
                f"[RELIST] curl_cffi retry #{att}: {type(e).__name__}: {str(e)[:60]}"
            ),
        )
        if r.status_code == 200:
            return r.text, ""
        return "", f"HTTP {r.status_code}"
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        try:
            s.close()
        except Exception:
            pass


def _classify_fetch_error(err1: str, err2: str) -> str:
    """組合 requests + curl_cffi 失敗原因,給友善建議。"""
    combined = (err1 + " | " + err2).lower()
    if any(k in combined for k in ("timed out", "timeout", "readtimeout", "connecttimeout")):
        return (
            "❌ 連線 Yahoo 超時(requests + curl_cffi 都試過)\n"
            "可能原因:\n"
            "  • 瀏覽器若也慢/打不開 → 網路問題,等 1-2 分鐘再試\n"
            "  • 瀏覽器若快 → VPN 沒讓 Python 走代理\n"
            "    解法 1:Windows「Internet 選項」→「連線」→「LAN 設定」設代理\n"
            "    解法 2:VPN 軟件開「TUN 模式」或「全局代理」(讓所有程式走 VPN)"
        )
    if any(k in combined for k in ("could not resolve", "name resolution", "nodename nor servname")):
        return "❌ DNS 解析失敗 — 切 VPN 節點或本機 DNS 設 8.8.8.8 / 1.1.1.1"
    if "connection refused" in combined:
        return "❌ 連線被拒 — VPN 沒開或 Yahoo 被擋"
    if "connection reset" in combined or "forcibly closed" in combined:
        return "❌ 連線被重置 — VPN 中途斷或 ISP 干擾,30 秒後再試"
    return (
        f"❌ 拉資料失敗\n"
        f"  requests: {err1[:80]}\n"
        f"  curl_cffi: {err2[:80]}"
    )


def fetch_source_item(url_or_id: str, on_log: Optional[LogFn] = None) -> Tuple[Dict[str, Any], str]:
    """從 Yahoo 公開商品頁拉資料。

    v6.1.20.5:雙路徑策略 — requests 為主(跟瀏覽器同 path),curl_cffi 為 fallback。
    解 VPN/中國跨境場景 curl_cffi 因 TLS 偽裝被擋 / 不認系統代理問題。

    Args:
        url_or_id: 完整 URL 或純 item id
        on_log: 可選 log fn
    Returns:
        (item_dict, error_msg)
        item_dict 結構參考 isoredux-data["item"](63 個欄位:title/price/images/
        catId/description/payments/shippings/models 等)
    """
    _log = on_log or (lambda m: None)
    item_id = extract_item_id(url_or_id)
    if not item_id:
        return {}, "找不到 Yahoo item ID"

    url = f"https://tw.bid.yahoo.com/item/{item_id}"

    # 路徑 1:stdlib requests (主路徑 — 系統 OpenSSL + 系統代理,跟瀏覽器同 path)
    html_text, err_req = _fetch_html_via_requests(url, _log)

    # 路徑 2:curl_cffi (fallback — 若 requests 被 client compat 擋,Chrome 偽裝可替代路径)
    if not html_text:
        _log(f"[RELIST] requests 失敗 ({err_req[:60]}),改 curl_cffi fallback")
        html_text, err_cffi = _fetch_html_via_curl_cffi(url, _log)
        if not html_text:
            return {}, _classify_fetch_error(err_req, err_cffi)
        _log("[RELIST] curl_cffi fallback OK")

    m = re.search(
        r'<[^>]*\bid=["\']isoredux-data["\'][^>]*>(.*?)</[^>]+>',
        html_text, re.DOTALL,
    )
    if not m:
        return {}, "頁面缺 isoredux-data(可能商品已下架或頁面結構變了)"

    try:
        raw = _html_mod.unescape(m.group(1))
        data = json.loads(raw)
    except Exception as e:
        return {}, f"isoredux-data JSON 解析失敗: {e}"

    item = data.get("item") or {}
    if not item or not item.get("id"):
        return {}, "item 資料為空(可能商品已下架)"

    # Sanity:title + price 必有
    if not item.get("title"):
        return {}, "商品標題缺失,可能無法刊登"
    if not item.get("price"):
        return {}, "商品價格缺失"

    _log(f"[RELIST] fetch_source OK id={item.get('id')} title={item.get('title','')[:30]}...")
    return item, ""


# ── Step 2:轉換 source dict → publish merchandise dict ──

# Yahoo condition 數字 → useStatus 文字
_CONDITION_TO_USE_STATUS = {
    "1": "new",          # 全新
    "2": "used",         # 近全新
    "3": "used",         # 二手
    "4": "used",         # 二手有瑕疵
    "5": "used",         # 二手有保存
}


def _extract_image_urls(source: Dict[str, Any]) -> List[str]:
    """從 source.images 拉原圖 URL(優先 oImage 1400x1400)。"""
    urls: List[str] = []
    # source.oImage 是原圖列表,結構 [{origin:{url,width,height}, resize05:{...}, ...}]
    o_images = source.get("oImage") or []
    if isinstance(o_images, list):
        for img in o_images:
            if isinstance(img, dict):
                u = (img.get("origin") or {}).get("url") or ""
                if u:
                    urls.append(u)
    # fallback: source.images
    if not urls:
        for img in source.get("images") or []:
            if isinstance(img, str):
                urls.append(img)
            elif isinstance(img, dict):
                # 優先 lg(1400),次 md/sm
                u = (
                    (img.get("lg") or {}).get("src")
                    or (img.get("md") or {}).get("src")
                    or (img.get("sm") or {}).get("src")
                    or img.get("src")
                    or img.get("origin")
                )
                if isinstance(u, dict):
                    u = u.get("url") or u.get("src") or ""
                if u:
                    urls.append(u)
    # 去重保序
    seen = set()
    out = []
    for u in urls:
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _extract_payment_ids(source: Dict[str, Any]) -> List[str]:
    """從 source.payments 拉付款 id 字串列表。"""
    out: List[str] = []
    for p in source.get("payments") or []:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            pid = p.get("id") or ""
            if pid:
                out.append(pid)
    return out


def _extract_model_info(source: Dict[str, Any]) -> Dict[str, Any]:
    """從 source.models 拉規格資訊(數量/規格組合)。"""
    models = source.get("models") or []
    if not models:
        return {"qty": 1, "specCombination": ""}
    m0 = models[0] if isinstance(models[0], dict) else {}
    return {
        "qty": int(m0.get("qty") or 1),
        "specCombination": m0.get("specCombination") or "",
    }


def build_relist_merchandise(
    source: Dict[str, Any],
    edits: Optional[Dict[str, Any]] = None,
    replacement_image_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """source dict → publish merchandise dict。

    Args:
        source: fetch_source_item 拿到的 item 字典
        edits: 用戶覆寫欄位(title/price/description/catId/condition/location/
               hashtags/quantity/brief/payments)
        replacement_image_urls: 用戶傳新圖後上傳到目標帳號的 CDN URLs
                                (覆蓋 source 原圖)
    Returns:
        publish API 接受的 merchandise dict
    """
    edits = edits or {}
    title = str(edits.get("title", source.get("title", ""))).strip()
    price = edits.get("price", source.get("price", 0))
    try:
        price_s = f"{float(str(price).replace(',', '')):.2f}"
    except Exception:
        price_s = "0.00"

    description = str(edits.get("description", source.get("description", "")))
    location = str(edits.get("location", source.get("location", "")) or "台北市")
    cat_id = str(edits.get("catId", source.get("catId", "")))
    condition_raw = str(edits.get("condition", source.get("condition", "1")))
    use_status = _CONDITION_TO_USE_STATUS.get(condition_raw, "used")

    # subtitle 在公開頁有,publish 規格放 brief
    brief = edits.get("brief")
    if brief is None:
        brief = source.get("subtitle") or title[:60]
    brief = str(brief).replace("\n", " ").replace("\r", " ").strip()[:200]

    hashtags = edits.get("hashtags")
    if hashtags is None:
        hashtags = source.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = [str(hashtags)]
    hashtags = [str(h).strip() for h in hashtags if str(h).strip()][:4]

    # 圖片:用戶換過就用新的,否則用 source 原圖 URL
    if replacement_image_urls:
        images = list(replacement_image_urls)
    else:
        images = _extract_image_urls(source)
    images = images[:10]  # Yahoo 上限 10 張

    # 付款:用戶覆寫 > source 原本 > 預設安全集
    payments = edits.get("payments")
    if payments is None:
        payments = _extract_payment_ids(source)
    if not payments:
        # 防呆 fallback(7-11 / 萊爾富 取貨付款是大多數帳號都支援的)
        payments = ["c2cSevenCvs", "c2cHilifeCvs"]

    # 數量
    model_info = _extract_model_info(source)
    qty = int(edits.get("quantity", model_info["qty"]) or 1)
    if qty < 1:
        qty = 1

    # 分類 attributes(MVP:空,使用者要在 TG 編輯時手動補)
    cat_attrs = edits.get("category_attrs") or []

    merch = {
        "type": "basic",
        "title": title,
        "description": {
            "brief": brief,
            "detail": description,
        },
        "hashtags": hashtags,
        "labels": [],
        "images": images,
        "location": location,
        "video": {},
        "useStatus": use_status,
        "category": {"id": cat_id, "attributes": cat_attrs},
        "payments": payments,
        "purchaseLimit": {"minQuantity": "", "maxQuantity": ""},
        "presale": {},
        "shipments": {"isApplyShippingRule": True},
        "product": {
            "models": [{
                "quantity": str(qty),
                "price": {"selling": price_s},
                "partNumber": {"first": "", "second": ""},
                "barcode": "",
            }]
        },
        "buyMorePromotions": [],
        "listing": {"type": "afterdays", "afterdays": 0},
        "bid": {},
        "saveLocation": True,
    }
    return merch


# ── Step 3:用戶換的圖 → 下載 → 上傳到目標帳號 Pixelframe ─

def upload_replacement_image(
    auth_session,
    image_url: str = "",
    image_path: str = "",
    on_log: Optional[LogFn] = None,
) -> Tuple[str, str]:
    """把單張圖(URL 或本地路徑)上傳到目標帳號的 Yahoo Pixelframe,返回 CDN URL。

    複用 publish_http_ops 的 upload_image_direct(STS + S3 + finalize 4-step)。
    Args:
        auth_session: target AuthSession (or compatible session with cookies+wssid)
        image_url: 來源 URL(從 TG file URL / Yahoo CDN URL 下載)
        image_path: 本地路徑(若已下載)
    Returns:
        (cdn_url, error_msg)
    """
    _log = on_log or (lambda m: None)
    tmp_path: Optional[Path] = None
    try:
        # 下載到 tmp
        if image_path:
            local_path = Path(image_path)
            if not local_path.exists():
                return "", f"本地圖不存在: {image_path}"
        else:
            if not image_url:
                return "", "需要 image_url 或 image_path"
            import tempfile
            # v6.1.20.5:雙路徑下載 — requests 主、curl_cffi fallback
            #   TG file API / Yahoo CDN 在中國 VPN 場景 curl_cffi 常超時,改 requests 跟瀏覽器同 path
            img_bytes = b""
            content_type = ""
            err_req = ""
            try:
                import requests as _stdlib_requests
                r1 = _stdlib_requests.get(image_url, timeout=25)
                if r1.status_code == 200:
                    img_bytes = r1.content
                    content_type = (r1.headers.get("Content-Type") or "").lower()
                else:
                    err_req = f"HTTP {r1.status_code}"
            except Exception as e:
                err_req = f"{type(e).__name__}: {str(e)[:60]}"

            if not img_bytes:
                # fallback curl_cffi
                _log(f"[RELIST] download image: requests 失敗 ({err_req}),改 curl_cffi")
                s = CffiSession(impersonate=CURL_CFFI_IMPERSONATE)
                try:
                    r = cffi_retry_call(
                        s.get, image_url, timeout=20, max_retries=2,
                        on_retry=lambda att, e: _log(
                            f"[RELIST] download retry #{att}: {str(e)[:60]}"
                        ),
                    )
                    if r.status_code == 200:
                        img_bytes = r.content
                        content_type = (r.headers.get("Content-Type") or "").lower()
                    else:
                        return "", f"下載圖 HTTP {r.status_code}(requests: {err_req})"
                except Exception as e:
                    _msg = str(e)
                    if "timed out" in _msg.lower():
                        return "", f"下載圖超時(VPN 不穩?requests/curl_cffi 都試過 — {err_req})"
                    return "", f"下載圖失敗: {type(e).__name__}: {str(e)[:100]}"
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            # 副檔名從 content-type 或 URL 推
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            elif image_url.lower().endswith((".png", ".webp", ".gif")):
                ext = os.path.splitext(image_url.split("?")[0])[1] or ".jpg"
            tmp_fd, tmp_str = tempfile.mkstemp(suffix=ext, prefix="relist_img_")
            os.close(tmp_fd)
            tmp_path = Path(tmp_str)
            tmp_path.write_bytes(img_bytes)
            local_path = tmp_path

        # 上傳:複用 publish_http_ops.upload_images(走 STS+S3+finalize)
        from .publish_http_ops import upload_images
        # upload_images 接受 session(curl_cffi)+ 本地 path 列表,返回 CDN URL 列表
        cdn_urls, err = upload_images(
            auth_session.http if hasattr(auth_session, "http") else auth_session,
            [str(local_path)],
            on_log=_log,
            wssid=getattr(auth_session, "wssid", ""),
        )
        if err:
            return "", f"上傳失敗: {err}"
        if not cdn_urls:
            return "", "上傳成功但無 CDN URL"
        return cdn_urls[0], ""
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


# ── Step 4:do_relist 完整流程 ────────────────────────────

def do_relist(
    target_profile_dir: Path,
    source: Dict[str, Any],
    edits: Optional[Dict[str, Any]] = None,
    replacement_image_urls: Optional[List[str]] = None,
    on_log: Optional[LogFn] = None,
) -> Tuple[str, str]:
    """執行轉刊到目標帳號。

    v6.1.20:跟 publish_http_ops.publish_item_http 同 flow(純 HTTP):
      1. create_publish_session  → 建 target 帳號 session
      2. fetch_publish_page      → 拉發佈頁
      3. extract_publish_config  → 取 target wssid + payments + 分類樹
      4. build_relist_merchandise → 構造 merch dict(用 target's payments,不用 source 的)
      5. submit_merchandise      → 提交

    Args:
        target_profile_dir: 目標帳號 profile 目錄(讀 cookies + wssid)
        source: fetch_source_item 拿到的 item dict
        edits: 用戶覆寫欄位
        replacement_image_urls: 已上傳到目標帳號的新圖 CDN URLs(若 None 用 source 原圖 URL)
        on_log: log fn
    Returns:
        (new_merch_id, error_msg)
        error_msg 非空 = 失敗
    """
    _log = on_log or (lambda m: None)
    target_profile_dir = Path(target_profile_dir)
    if not target_profile_dir.exists():
        return "", f"目標 profile 不存在: {target_profile_dir}"

    from .publish_http_ops import (
        create_publish_session, fetch_publish_page, extract_publish_config,
        submit_merchandise, invalidate_cookie_cache, DEFAULT_MAX_AGE,
    )

    # 1. 建 publish session(跟 auto_publish 同邏輯)
    session, cached_wssid, cookies, err = create_publish_session(
        target_profile_dir, DEFAULT_MAX_AGE,
    )
    if err:
        return "", f"建 session 失敗: {err}"
    _log("[RELIST] target session OK")

    # 2. 拉 target 的發佈頁取 config(payments / wssid / categories)
    state, err = fetch_publish_page(session)
    if err:
        invalidate_cookie_cache(target_profile_dir)
        return "", f"取目標發佈頁失敗: {err}"

    config = extract_publish_config(state)
    wssid = config["wssid"] or cached_wssid
    if not wssid:
        return "", "目標帳號取不到 wssid"
    target_payments = config.get("payments") or []
    _log(
        f"[RELIST] config OK wssid={wssid[:8]}... "
        f"target_payments={target_payments} (n={len(target_payments)})"
    )

    # 3. 構造 merchandise dict — 用 target's payments(不繼承 source's)
    eff_edits = dict(edits or {})
    # 若 caller 沒明確覆寫 payments,用 target 的(避免 shipments/payments inconsistent)
    if "payments" not in eff_edits:
        eff_edits["payments"] = target_payments
    merch = build_relist_merchandise(
        source, edits=eff_edits,
        replacement_image_urls=replacement_image_urls,
    )
    _log(
        f"[RELIST] merch built: title={merch['title'][:30]}, "
        f"price={merch['product']['models'][0]['price']['selling']}, "
        f"cat={merch['category']['id']}, images={len(merch['images'])}, "
        f"payments={merch['payments']}"
    )

    # 4. 提交(跟 auto_publish 同 path:submit_merchandise → Reservice POST)
    import time as _t
    _t.sleep(0.5)  # 模擬人工檢查表單(輕)
    result, err = submit_merchandise(session, wssid, merch)
    if err:
        if "401" in err or "403" in err or "cookie" in err.lower() or "wssid" in err.lower():
            invalidate_cookie_cache(target_profile_dir)
        return "", err
    new_id = str(result.get("id", "")) if result else ""
    if not new_id:
        return "", "Yahoo 接受但沒回新商品 ID"

    _log(f"[RELIST] OK new_merch_id={new_id}")
    return new_id, ""
