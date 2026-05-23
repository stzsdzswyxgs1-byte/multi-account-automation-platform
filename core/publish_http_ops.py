"""纯 HTTP 刊登 Yahoo 拍賣商品（不需要浏览器）。

完整流程：
1. HTTP GET 发布页 → 解析 isoredux-data → 提取 wssid/payments/categories/shipments
2. HTTP GET S3 凭证 → HTTP POST 图片上传到 Pixelframe → 获得 CDN URL
3. HTTP POST /fe/_reservice_/ → CALL_RESERVICE FETCH_PUBLISH_MERCHANDISE

依赖 cookie_store 提供的 cookies + wssid（由监控模块每 300s 刷新）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import CURL_CFFI_IMPERSONATE, get_api_headers
from .cookie_store import (
    load_cookie_cache, invalidate_cookie_cache, DEFAULT_MAX_AGE,
    load_from_chrome_sqlite_yahoo, save_cookie_cache,
)
from .human import human_jitter_ms

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# ── 超时与重试（兼容 VPN 慢速网络）─────────────────────────
HTTP_TIMEOUT_SHORT = 30       # S3 credentials 等轻量请求
HTTP_TIMEOUT_LONG = 90        # 发布页 / Reservice 提交商品
HTTP_TIMEOUT_IMAGE = 180      # v6.0.68:图片上传独立超时(慢网络友好,3min 上限)
HTTP_MAX_RETRIES = 2          # 网络错误最多重试 2 次（共 3 次尝试）
HTTP_RETRY_DELAY = 3          # 重试间隔（秒）

# ── 常量 ──────────────────────────────────────────────

PUBLISH_PAGE_URL = "https://tw.bid.yahoo.com/partner/merchandise/publish?"
RESERVICE_URL = "https://tw.bid.yahoo.com/fe/_reservice_/"
S3_CRED_URL = "https://trendr-apac.media.yahoo.com/api/pixelframe/v1/aws/resources/s3/credentials?role=content-upload"
IMG_UPLOAD_URL = "https://trendr-apac.media.yahoo.com/api/pixelframe/v1/images/upload"
IMG_UPLOAD_PARAMS = {
    "targetType": "item",
    "targetId": "auction2",
    "appName": "auction",
    "resizingProfile": "auction",
}

# Yahoo Bid Image 直传 API（不需要 S3 凭证，用 wssid 认证）
BID_IMAGE_URL = "https://api.bid.yahoo.com/api/item/v1/bid/image"
BID_IMAGE_PARAMS = {"appId": "bid", "imageIntent": "listing"}

# ── 代理检测 ──────────────────────────────────────────

def _detect_system_proxy() -> str:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    s = server.strip()
                    if not s.startswith(("http://", "https://", "socks")):
                        s = "http://" + s
                    return s
    except Exception:
        pass
    return ""


# ── Session 创建 ──────────────────────────────────────

def create_publish_session(
    profile_dir: Path,
    max_age: float = 2592000,  # 30天
    cookies_override: Dict[str, str] = None,
    wssid_override: str = "",
) -> Tuple[Optional[CffiSession], str, Dict[str, str], str]:
    """从 cookie_cache 创建 curl_cffi session。

    cookies_override: 直接提供 cookies dict（跳过文件读取，用于 inject_cookies 模式）。
    返回 (session, wssid, cookies_dict, error_msg)。
    session 为 None 表示失败。
    """
    if cookies_override:
        cookies = cookies_override
        wssid = wssid_override
    else:
        profile_dir = Path(profile_dir)
        cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
        # v6.2:cache 過期/不存在 → 從 Chrome SQLite 強讀(跟 im_http_ops / myauc_http 一致)
        # 解決:用戶剛遠程登錄完還沒寫 cookie_cache.json,publish 立刻就跑失敗的問題
        if not cookies:
            try:
                flat, raw = load_from_chrome_sqlite_yahoo(profile_dir)
                if flat and len(flat) >= 5:
                    save_cookie_cache(profile_dir, flat, "", raw_cookies=raw)
                    cookies, wssid, saved_at = load_cookie_cache(profile_dir, max_age=max_age)
            except Exception:
                pass
    if not cookies:
        return None, "", {}, "cookie cache 不存在或已过期"

    kw = dict(impersonate=CURL_CFFI_IMPERSONATE)
    proxy = _detect_system_proxy()
    if proxy:
        kw["proxy"] = proxy

    s = CffiSession(**kw)
    # 基础头
    s.headers.update(get_api_headers(
        referer=PUBLISH_PAGE_URL,
        origin="https://tw.bid.yahoo.com",
    ))
    # cookies
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".yahoo.com")

    return s, wssid, cookies, ""


# ── 1. 解析发布页 isoredux-data ──────────────────────

def fetch_publish_page(
    session: CffiSession,
) -> Tuple[dict, str]:
    """HTTP GET 发布页，解析 isoredux-data 提取 Redux 初始状态。

    返回 (state_dict, error_msg)。
    state_dict 包含: page.wssid, paymentsList, categoryTree, shipmentsList, booth 等。

    重试策略：
    - 网络错误/HTTP 非 200/空内容：重试（可能是流中断）
    - HTTP 200 但缺 isoredux-data 且没重定向到登录：也重试（部分页面）
    - 重定向到登录：立即失败（cookie 真的挂了）
    - 重试全部失败：保存 HTML 到 output/ 供诊断
    """
    # v6.1.47:5xx retry 3→5 次 + 更長 backoff,救 Yahoo backend 短暫故障(~30s 內恢復)
    # 修「mondalgobindo84gs 撞 publish page HTTP 500 3 次都中,但 10 秒後監控就恢復」bug
    _log = logging.getLogger("publish_http")
    last_err = ""
    html = ""
    raw = ""
    for attempt in range(1, 6):  # 1, 2, 3, 4, 5
        html = ""
        final_url = ""
        try:
            r = session.get(
                PUBLISH_PAGE_URL,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=HTTP_TIMEOUT_LONG,
            )
            final_url = str(getattr(r, "url", "") or "")
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} url={final_url[:100]}"
            else:
                html = r.text or ""
                if not html:
                    last_err = "返回空内容"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:150]}"

        if html:
            # 权威 cookie 失效判断：看最终 URL 是否被重定向到登录页
            # （不用 "登入" 关键词，因为发布页 header 本就有「登入」链接，会误判）
            _url_lower = final_url.lower()
            if _url_lower and any(k in _url_lower for k in (
                "login.yahoo", "//login.", "/signin", "/signin?", "/login?",
                "open_login_"
            )):
                return {}, f"Cookie 已失效（已重定向到登录页: {final_url[:120]}）"

            # 解析 isoredux-data（正则放宽：允许 id 不是第一个属性、允许属性顺序变化）
            m = re.search(
                r'<script\b[^>]*?\bid=["\']isoredux-data["\'][^>]*>(.*?)</script>',
                html, re.DOTALL,
            )
            if m:
                if attempt > 1:
                    _log.info("[fetch_publish_page] 第 %d 次重试成功 (html=%d bytes)",
                              attempt, len(html))
                raw = m.group(1).strip()
                break

            # 200 但没找到 isoredux-data：多半是流中断导致的部分页面，值得重试
            last_err = f"no isoredux-data (html={len(html)} bytes, url={final_url[:80]})"

        if attempt < 5:
            # v6.1.47:retry backoff 表 3s, 7s, 15s, 30s(總 ~55s 給 Yahoo backend 恢復)
            _waits = [3, 7, 15, 30]
            _wait = _waits[min(attempt - 1, len(_waits) - 1)]
            _log.warning("[fetch_publish_page] 第 %d/5 次失败: %s;%ds 后重试",
                         attempt, last_err, _wait)
            time.sleep(_wait)

    if not raw:
        # 全部失败：保存最后一次 HTML 用于诊断
        if html:
            try:
                diag = Path("output") / f"publish_page_fail_{int(time.time())}.html"
                diag.parent.mkdir(parents=True, exist_ok=True)
                diag.write_text(html[:300000], encoding="utf-8", errors="replace")
                _log.error("[fetch_publish_page] 最后一次 HTML 已保存: %s (size=%d bytes)",
                           diag, len(html))
            except Exception:
                pass
        return {}, f"HTTP GET publish page 失败(5次重试): {last_err}"
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"isoredux-data JSON 解析失败: {e}"

    # 校验登录
    user = (state.get("page") or {}).get("user") or {}
    if not user.get("isLogin"):
        return {}, "Cookie 已失效 (isLogin=false)"

    # 从 HTML 提取所在地 <select> 的有效选项列表
    # 格式: <option value="台北市">台北市</option> 或 <option value="xxx">Label</option>
    _loc_options = []
    try:
        # 找到所在地 <select> 区域（通常在 "所在地區" 标签附近）
        sel_m = re.search(
            r'<select[^>]*name=["\'](?:location|area)["\'][^>]*>(.*?)</select>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if not sel_m:
            # 宽松匹配：任何靠近 "所在地" 的 <select>
            sel_m = re.search(
                r'所在地[區区]?\s*</(?:label|div|span|th|td)>\s*(?:<[^>]*>\s*)*<select[^>]*>(.*?)</select>',
                html, re.DOTALL | re.IGNORECASE,
            )
        if sel_m:
            # 提取所有 <option value="...">...</option>
            for opt_m in re.finditer(
                r'<option\s+value=["\']([^"\']*)["\'][^>]*>([^<]*)</option>',
                sel_m.group(1), re.IGNORECASE,
            ):
                val = opt_m.group(1).strip()
                label = opt_m.group(2).strip()
                if val and val not in ("", "0", "-1", "default"):
                    _loc_options.append(val)
    except Exception:
        pass
    state["_html_location_options"] = _loc_options

    return state, ""


def extract_publish_config(state: dict) -> dict:
    """从 isoredux-data state 提取刊登所需的配置。

    返回 {wssid, payments, category_tree, shipments, booth_code, ...}。
    """
    page = state.get("page") or {}
    ms = state.get("merchandiseSubmit") or {}

    wssid = page.get("wssid", "")

    # payments: 从 acceptPayment.payments 提取 checked=True 的付款方式
    # （与 auto_publish_feature._redux_get_active_payments 逻辑一致）
    accept_payment = state.get("acceptPayment") or {}
    payments_data = accept_payment.get("payments") or {}
    payment_ids = []
    if isinstance(payments_data, dict):
        # 优先用 checked 状态（表单 checkbox 实际勾选状态）
        checked = [pid for pid, info in payments_data.items()
                   if isinstance(info, dict) and info.get("checked")]
        if checked:
            payment_ids = checked
        else:
            # fallback: 用 active 状态
            payment_ids = [pid for pid, info in payments_data.items()
                          if isinstance(info, dict) and info.get("active")]

    # category tree
    category_tree = ms.get("categoryTree") or state.get("categoryTree") or {}

    # shipments
    shipments_list = ms.get("shipmentsList") or state.get("shipmentsList") or []

    # booth code (店铺代码)
    booth = page.get("booth") or {}
    booth_code = booth.get("boothCode", "")

    return {
        "wssid": wssid,
        "payments": payment_ids,
        "category_tree": category_tree,
        "shipments_list": shipments_list,
        "booth_code": booth_code,
        "booth": booth,
        "user": page.get("user", {}),
        "location_options": state.get("_html_location_options") or [],
    }


# ── Yahoo 台灣有效所在地列表（硬编码兜底）──────────────
# 当 HTML 提取失败时使用此列表做匹配
_YAHOO_TW_LOCATIONS = [
    # 六都
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市",
    # 市
    "基隆市", "新竹市", "嘉義市",
    # 縣
    "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "台東縣",
    "澎湖縣", "金門縣", "連江縣",
    # 海外
    "中國大陸", "香港", "日本", "韓國", "美加地區",
    "東南亞", "其他亞洲地區", "歐洲", "紐澳", "其他",
]


def match_location(user_loc: str, valid_list: List[str] = None) -> str:
    """将用户输入的所在地归一化为 Yahoo 接受的有效值。

    匹配策略:
    1. 精确匹配
    2. 台↔臺 变体匹配
    3. 去掉「市」「縣」模糊匹配
    4. 包含匹配（如 "台北" 匹配 "台北市"）
    找不到则返回原始值并记录警告。
    """
    loc = (user_loc or "").strip()
    if not loc:
        return loc

    pool = valid_list if valid_list else _YAHOO_TW_LOCATIONS

    # 1) 精确匹配
    if loc in pool:
        return loc

    # 2) 台↔臺 变体
    variants = [loc]
    if "臺" in loc:
        variants.append(loc.replace("臺", "台"))
    if "台" in loc:
        variants.append(loc.replace("台", "臺"))
    for v in variants:
        if v in pool:
            log.info("[LOCATION] 自动修正所在地: '%s' → '%s' (台↔臺)", loc, v)
            return v

    # 3) 去掉「市」「縣」+ 台↔臺
    for v in variants:
        bare = v.rstrip("市縣")
        for p in pool:
            if p.rstrip("市縣") == bare:
                log.info("[LOCATION] 自动修正所在地: '%s' → '%s' (模糊匹配)", loc, p)
                return p

    # 4) 包含匹配（如 "台北" 匹配 "台北市"）
    for v in variants:
        for p in pool:
            if v in p or p in v:
                log.info("[LOCATION] 自动修正所在地: '%s' → '%s' (包含匹配)", loc, p)
                return p

    # 找不到，返回原始值并记录警告
    log.warning("[LOCATION] 无法匹配所在地: '%s'，Yahoo 可能拒绝。"
                "有效值: %s", loc, ", ".join(pool[:10]) + " ...")
    return loc

def fetch_s3_credentials(session: CffiSession) -> Tuple[dict, str]:
    """获取 S3 临时凭证（用于图片上传鉴权）。

    返回 (creds_dict, error_msg)。
    """
    # v6.1.19:curl_cffi TLS lib bug retry
    from .ssl_helper import cffi_retry_call
    try:
        r = cffi_retry_call(
            session.get,
            S3_CRED_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "Origin": "https://tw.bid.yahoo.com",
                "Referer": "https://tw.bid.yahoo.com/",
            },
            timeout=HTTP_TIMEOUT_SHORT,
            max_retries=2,
        )
    except Exception as e:
        return {}, f"S3 credentials 请求失败: {e}"

    if r.status_code != 200:
        return {}, f"S3 credentials HTTP {r.status_code}: {r.text[:200]}"

    try:
        return r.json(), ""
    except Exception as e:
        return {}, f"S3 credentials JSON 解析失败: {e}"


def upload_image_direct(
    session: CffiSession,
    image_path: str,
    wssid: str,
) -> Tuple[str, str]:
    """直传图片到 Yahoo Bid Image API（不需要 S3 凭证）。

    返回 (cdn_url, error_msg)。
    """
    if not os.path.isfile(image_path):
        return "", f"图片文件不存在: {image_path}"
    if not wssid:
        return "", "缺少 wssid"

    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }
    content_type = mime_map.get(ext, "image/jpeg")

    try:
        from curl_cffi import CurlMime
        with open(image_path, "rb") as f:
            img_data = f.read()
        mp = CurlMime()
        mp.addpart(name="image", content_type=content_type,
                   filename=os.path.basename(image_path), data=img_data)
        # 移除 session 全局的 Content-Type: application/json，让 curl_cffi 自动设 multipart boundary
        _saved_ct = session.headers.pop("Content-Type", None)
        # v6.1.19:curl_cffi TLS lib bug retry(圖片上傳)
        from .ssl_helper import cffi_retry_call as _cffi_retry
        try:
            r = _cffi_retry(
                session.post,
                BID_IMAGE_URL,
                params=BID_IMAGE_PARAMS,
                multipart=mp,
                headers={
                    "X-YahooWSSID-Authorization": wssid,
                    "Origin": "https://tw.bid.yahoo.com",
                    "Referer": "https://tw.bid.yahoo.com/",
                },
                timeout=HTTP_TIMEOUT_IMAGE,  # v6.0.68:图片上传独立超时(180s)
                max_retries=2,
            )
        finally:
            # 恢复 Content-Type（其他请求还需要 application/json）
            if _saved_ct:
                session.headers["Content-Type"] = _saved_ct
    except Exception as e:
        return "", f"直传请求失败: {e}"

    if r.status_code not in (200, 201):
        return "", f"直传 HTTP {r.status_code}: {r.text[:300]}"

    try:
        data = r.json()
    except Exception:
        return "", f"直传响应非 JSON: {r.text[:200]}"

    # 提取 CDN URL
    src = data.get("src") or {}
    cdn_url = src.get("url", "")
    if cdn_url and "img.yec.tw" in cdn_url:
        return cdn_url, ""

    cdn_url = _find_cdn_url(data)
    if cdn_url:
        return cdn_url, ""

    return "", f"直传成功但未找到 CDN URL: {json.dumps(data)[:300]}"


def upload_image(
    session: CffiSession,
    image_path: str,
    s3_creds: dict = None,
) -> Tuple[str, str]:
    """上传单张图片到 Yahoo Pixelframe CDN（S3 直传 + Pixelframe 处理）。

    流程:
    1. PUT 图片到 S3（使用 Pixelframe 提供的临时 AWS 凭证）
    2. POST S3 URL 到 Pixelframe 处理接口 → 返回 CDN URL

    返回 (cdn_url, error_msg)。cdn_url 格式: https://img.yec.tw/...
    """
    if not os.path.isfile(image_path):
        return "", f"图片文件不存在: {image_path}"

    if not s3_creds:
        return "", "缺少 S3 凭证"

    cred = s3_creds.get("credentials") or {}
    ak = cred.get("accessKeyId", "")
    sk = cred.get("secretAccessKey", "")
    token = cred.get("sessionToken", "")
    bucket = s3_creds.get("bucketName", "")
    path_prefix = s3_creds.get("path", "")
    region = s3_creds.get("region", "")

    if not all([ak, sk, token, bucket, path_prefix, region]):
        return "", "S3 凭证不完整"

    # MIME 类型推断
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }
    content_type = mime_map.get(ext, "image/jpeg")

    # ── Step 1: 上传到 S3 ──
    file_uuid = str(uuid.uuid4())
    s3_ext = ext if ext else ".jpg"
    s3_key = f"{path_prefix}/{file_uuid}{s3_ext}"

    try:
        import boto3
        s3_client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            aws_session_token=token,
        )
        with open(image_path, "rb") as f:
            s3_client.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=f,
                ContentType=content_type,
            )
    except Exception as e:
        return "", f"S3 上传失败: {e}"

    # S3 URL
    s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

    # ── Step 2: 调用 Pixelframe 处理 → 获得 CDN URL ──
    payload = {
        "url": s3_url,
        "appName": IMG_UPLOAD_PARAMS["appName"],
        "targetType": IMG_UPLOAD_PARAMS["targetType"],
        "targetId": IMG_UPLOAD_PARAMS["targetId"],
        "resizingProfile": IMG_UPLOAD_PARAMS["resizingProfile"],
    }

    # v6.1.19:curl_cffi TLS lib bug retry
    from .ssl_helper import cffi_retry_call as _cffi_retry2
    try:
        r = _cffi_retry2(
            session.post,
            IMG_UPLOAD_URL,
            params=IMG_UPLOAD_PARAMS,
            json=payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://tw.bid.yahoo.com",
                "Referer": "https://tw.bid.yahoo.com/",
            },
            timeout=HTTP_TIMEOUT_LONG,
            max_retries=2,
        )
    except Exception as e:
        return "", f"Pixelframe 处理请求失败: {e}"

    if r.status_code != 200:
        return "", f"Pixelframe HTTP {r.status_code}: {r.text[:300]}"

    try:
        data = r.json()
    except Exception:
        return "", f"Pixelframe 响应非 JSON: {r.text[:200]}"

    # 提取 CDN URL: 优先顶层 url 字段（原始稳定 URL，/images/ 路径）
    cdn_url = data.get("url", "")
    if cdn_url and "img.yec.tw" in cdn_url and "/images/" in cdn_url:
        return cdn_url, ""

    # fallback: 递归搜索
    cdn_url = _find_cdn_url(data)
    if cdn_url:
        return cdn_url, ""

    return "", f"Pixelframe 成功但未找到 CDN URL: {json.dumps(data)[:300]}"


def _find_cdn_url(obj) -> str:
    """递归搜索 dict/list 中的 img.yec.tw URL。"""
    if isinstance(obj, str):
        if "img.yec.tw" in obj and "/images/" in obj:
            return obj
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_cdn_url(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_cdn_url(v)
            if r:
                return r
    return ""


def upload_images(
    session: CffiSession,
    image_paths: List[str],
    s3_creds: dict = None,
    on_log: LogFn = None,
    wssid: str = "",
) -> Tuple[List[str], str]:
    """批量上传图片。优先直传 API，失败回退 S3。返回 (cdn_urls, error_msg)。"""
    _log = on_log or (lambda m: None)

    if not image_paths:
        return [], ""

    if not wssid:
        return [], "缺少 wssid，无法上传图片"

    _log(f"[HTTP-PUB] 直传模式（Yahoo Bid Image API）")

    urls = []
    for i, path in enumerate(image_paths):
        _log(f"[HTTP-PUB] 上传图片 {i+1}/{len(image_paths)}: {os.path.basename(path)}")

        url, err = "", ""
        for attempt in range(1, HTTP_MAX_RETRIES + 2):
            url, err = upload_image_direct(session, path, wssid)
            if not err:
                break
            if attempt <= HTTP_MAX_RETRIES:
                _log(f"[HTTP-PUB] 图片 {i+1} 上传失败(第{attempt}次)，{HTTP_RETRY_DELAY}s后重试: {err}")
                time.sleep(HTTP_RETRY_DELAY)

        if err:
            return urls, f"第 {i+1} 张图片上传失败: {err}"

        urls.append(url)
        _log(f"[HTTP-PUB] 图片 {i+1} CDN URL: {url[:80]}...")

        # 图片间延迟（模拟人工）
        if i < len(image_paths) - 1:
            delay_ms = human_jitter_ms(800, low=0.6, high=1.2)
            time.sleep(delay_ms / 1000)

    return urls, ""


# ── 3. 提交商品 (Reservice HTTP POST) ────────────────

# Yahoo API 常见错误码 → 中文友好说明
_ERROR_HINTS = {
    "no match location":
        "所在地不匹配 — Excel 的「所在地」欄位值不在 Yahoo 允許的列表中。"
        "請檢查是否用了「臺」(應該是「台」)，或者拼寫是否與 Yahoo 下拉選項完全一致"
        "（例：台北市、新北市、桃園市、日本 等）",
    "title":
        "標題格式問題 — 標題可能含有不允許的字元、太長或太短",
    "description":
        "商品描述問題 — 描述可能含有不允許的字元或 HTML 標籤",
    "image":
        "圖片問題 — 圖片 URL 無效或數量不符合要求（至少1張，最多10張）",
    "category":
        "分類問題 — 所選的拍賣分類 ID 無效或已停用",
    "price":
        "價格問題 — 價格格式不正確或超出允許範圍",
    "payment":
        "付款方式問題 — 選擇的付款方式無效或未啟用",
    "quantity":
        "數量問題 — 數量格式不正確或超出允許範圍",
    "shipment":
        "運送方式問題 — 運送設定無效",
    "wssid":
        "Session 過期 — 登入狀態已失效，需要重新登入或刷新 Cookie",
    "unauthorized":
        "未授權 — Cookie 已失效，請重新登入 Yahoo",
    "forbidden":
        "被禁止 — Yahoo 帳號可能被限制刊登，請檢查帳號狀態",
    "rate limit":
        "限流 — Yahoo 偵測到頻繁操作，請降低刊登速度或稍後重試",
    "duplicate":
        "重複刊登 — 相同的商品可能已經刊登過",
}


def _friendly_error(raw_detail: str, merchandise: dict = None) -> str:
    """将 Yahoo API 原始错误转换为中文友好说明。"""
    lower = raw_detail.lower()

    # 尝试匹配已知错误关键词
    hints = []
    for keyword, explanation in _ERROR_HINTS.items():
        if keyword in lower:
            hints.append(explanation)

    if hints:
        loc_val = (merchandise or {}).get("location", "")
        loc_note = f"（当前值: '{loc_val}'）" if loc_val and "location" in lower else ""
        return f"Yahoo API 错误: {raw_detail}\n  原因: {hints[0]}{loc_note}"

    # 未知错误码，保留原文
    return f"Yahoo API 错误: {raw_detail}"

def submit_merchandise(
    session: CffiSession,
    wssid: str,
    merchandise: dict,
) -> Tuple[dict, str]:
    """HTTP POST 提交商品到 Yahoo。

    返回 (result_dict, error_msg)。
    成功时 result_dict 包含 {id: "商品编号", title: "..."} 等。
    """
    action = {
        "type": "CALL_RESERVICE",
        "payload": {
            "wssid": wssid,
            "merchandise": merchandise,
        },
        "reservice": {
            "name": "FETCH_PUBLISH_MERCHANDISE",
            "start": "FETCH_PUBLISH_MERCHANDISE_START",
            "state": "BEGIN",
        },
        "rtk2": True,
    }

    body = json.dumps(action, ensure_ascii=False)

    # v6.1.19:curl_cffi TLS library bug retry(error:00000000:invalid library)
    from .ssl_helper import cffi_retry_call
    try:
        r = cffi_retry_call(
            session.post,
            RESERVICE_URL,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Origin": "https://tw.bid.yahoo.com",
                "Referer": PUBLISH_PAGE_URL,
            },
            timeout=HTTP_TIMEOUT_LONG,
            max_retries=2,
        )
    except Exception as e:
        return {}, f"Reservice 请求失败: {e}"

    if r.status_code == 429:
        return {}, "HTTP 429 — Yahoo 限流，请稍后重试"

    if r.status_code != 200:
        return {}, f"Reservice HTTP {r.status_code}: {r.text[:300]}"

    try:
        result = r.json()
    except Exception:
        return {}, f"Reservice 响应非 JSON: {r.text[:200]}"

    # 检查错误
    if result.get("error"):
        payload = result.get("payload") or {}
        error_data = payload.get("errorData") or {}
        errors = error_data.get("error") or []
        if errors:
            detail = "; ".join(
                e.get("message", e.get("detail", str(e)))[:100]
                for e in errors[:3]
            )
        else:
            detail = payload.get("message", str(result))[:200]

        # ── 诊断: 把完整 error response 写到 publish_logs 方便后续查根因 ──
        try:
            from datetime import datetime
            log_dir = Path("publish_logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            diag_path = log_dir / f"submit_error_{ts}.json"
            diag_path.write_text(
                json.dumps({
                    "request_merchandise": merchandise,
                    "yahoo_response": result,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

        # ── 常见错误码中文说明 ──
        friendly = _friendly_error(detail, merchandise)

        # 错误中带上 errors 列表的 dataKey/code 等关键字段(方便日志直接看清)
        if errors:
            verbose = []
            for e in errors[:3]:
                parts = []
                for k in ("code", "dataKey", "message", "detail"):
                    v = e.get(k)
                    if v is not None:
                        parts.append(f"{k}={v!r}")
                verbose.append("{" + " ".join(parts) + "}")
            friendly += "\n  RAW: " + " | ".join(verbose)

        return result, friendly

    # 成功：提取商品 payload
    payload = result.get("payload") or result
    merch_id = payload.get("id", "")
    if merch_id:
        return payload, ""

    # 可能成功但响应格式不同
    return result, ""


# ── 4. 一键刊登 ──────────────────────────────────────

def publish_item_http(
    profile_dir: Path,
    *,
    title: str,
    brief: str,
    desc: str,
    image_paths: List[str],
    category_id: str,
    price: str,
    qty: str,
    condition: str,
    location: str,
    payments: List[str],
    category_attrs: list = None,
    shipments: dict = None,
    on_log: LogFn = None,
    max_age: float = DEFAULT_MAX_AGE,
) -> Tuple[str, str]:
    """纯 HTTP 刊登单件商品。

    返回 (merchandise_id, error_msg)。
    如果 error_msg 非空表示失败（调用方可 fallback 到浏览器）。
    """
    _log = on_log or (lambda m: None)
    _ts = lambda: time.strftime("%H:%M:%S")

    # ── Step 0: 创建 session ──
    _log(f"[HTTP-PUB {_ts()}] 创建 HTTP session...")
    session, cached_wssid, cookies, err = create_publish_session(profile_dir, max_age)
    if err:
        return "", f"Session 创建失败: {err}"

    # ── Step 1: 获取发布页初始状态 ──
    _log(f"[HTTP-PUB {_ts()}] 获取发布页 isoredux-data...")
    state, err = fetch_publish_page(session)
    if err:
        invalidate_cookie_cache(profile_dir)
        return "", f"发布页获取失败: {err}"

    config = extract_publish_config(state)
    wssid = config["wssid"] or cached_wssid
    if not wssid:
        return "", "无法获取 wssid"

    _log(f"[HTTP-PUB {_ts()}] wssid={wssid[:8]}... payments={len(config['payments'])} "
         f"categories={len(config['category_tree'])}")

    # ── Step 2: 上传图片 ──
    cdn_urls = []
    if image_paths:
        _log(f"[HTTP-PUB {_ts()}] 上传 {len(image_paths)} 张图片...")
        # 图片上传前延迟（模拟人工浏览表单）
        time.sleep(human_jitter_ms(1500, low=0.8, high=1.5) / 1000)

        cdn_urls, err = upload_images(session, image_paths, on_log=_log, wssid=wssid)
        if err:
            return "", f"图片上传失败: {err}"
        _log(f"[HTTP-PUB {_ts()}] {len(cdn_urls)} 张图片上传完成")

    # ── Step 3: 使用调用方提供的 payments（如果发布页的为空则用配置的）──
    if not payments and config["payments"]:
        payments = config["payments"]

    # ── Step 4: 构建 merchandise payload ──
    _log(f"[HTTP-PUB {_ts()}] 构建 merchandise payload (title={title[:20]}...)")
    _loc_opts = config.get("location_options") or []
    merchandise = _build_merchandise(
        title=title, brief=brief, desc=desc,
        image_urls=cdn_urls, category_id=category_id,
        price=price, qty=qty, condition=condition,
        location=location, payments=payments,
        category_attrs=category_attrs, shipments=shipments,
        location_options=_loc_opts,
    )

    # ── Step 5: 提交 ──
    # 提交前延迟（模拟人工检查表单）
    delay = human_jitter_ms(2000, low=0.8, high=2.0)
    _log(f"[HTTP-PUB {_ts()}] 提交前延迟 {delay}ms...")
    time.sleep(delay / 1000)

    _log(f"[HTTP-PUB {_ts()}] 提交商品到 Yahoo...")
    result, err = {}, ""
    # 504/502/503 (网关 / 服务器临时不可用) 单独处理 — 用更长退避，Yahoo 服务器过载时 3s 不够缓过来
    GATEWAY_ERROR_TAGS = ("HTTP 504", "HTTP 502", "HTTP 503", "Gateway", "BAD_GATEWAY", "BadGateway")
    GATEWAY_DELAYS = [10, 20]  # 2 次重试，总耗时 ~30 秒
    NORMAL_MAX_TRIES = HTTP_MAX_RETRIES + 1  # 现有逻辑：3 次（含首次）
    GATEWAY_MAX_TRIES = NORMAL_MAX_TRIES + len(GATEWAY_DELAYS)  # 504 走更长路径
    attempt = 0
    while True:
        attempt += 1
        result, err = submit_merchandise(session, wssid, merchandise)
        if not err:
            break
        # 429 限流或 auth 错误不重试
        if "429" in err or "401" in err or "403" in err:
            break
        is_gateway_err = any(tag in err for tag in GATEWAY_ERROR_TAGS)
        if is_gateway_err:
            if attempt > GATEWAY_MAX_TRIES:
                break
            # 用专用退避表
            idx = min(attempt - 1, len(GATEWAY_DELAYS) - 1)
            wait = GATEWAY_DELAYS[idx]
            _log(f"[HTTP-PUB {_ts()}] Yahoo 网关错误(第{attempt}次)，{wait}s 后重试: {err[:80]}")
            time.sleep(wait)
            continue
        # 其它错误：维持原 3 次 / 3s 行为
        if attempt > NORMAL_MAX_TRIES:
            break
        _log(f"[HTTP-PUB {_ts()}] 提交失败(第{attempt}次)，{HTTP_RETRY_DELAY}s后重试: {err}")
        time.sleep(HTTP_RETRY_DELAY)
    if err:
        _log(f"[HTTP-PUB {_ts()}] 提交失败: {err}")
        # 如果是 auth 错误，清除 cookie cache
        if "401" in err or "403" in err or "cookie" in err.lower() or "wssid" in err.lower():
            invalidate_cookie_cache(profile_dir)
        return "", err

    merch_id = str(result.get("id", ""))
    _log(f"[HTTP-PUB {_ts()}] 刊登成功! 商品编号={merch_id}")

    return merch_id, ""


# ── 辅助: 构建 merchandise payload ────────────────────

def _build_merchandise(
    *, title: str, brief: str, desc: str, image_urls: List[str],
    category_id: str, price: str, qty: str, condition: str,
    location: str, payments: List[str], category_attrs: list = None,
    shipments: dict = None, location_options: List[str] = None,
    cat_kw: str = "",
    hashtags: List[str] = None,
) -> dict:
    """构建 Redux dispatch 所需的 merchandise payload。

    与 auto_publish_feature._redux_build_merchandise 保持一致。
    location_options: 从发布页提取的有效所在地列表（用于归一化匹配）。
    cat_kw(v6.0.50): 用於判斷大類自動加「收藏品」標籤。
    hashtags: Excel「標籤」欄解析後的字串列表(已符合 Yahoo 4 條規則)。
    """
    # 状态映射
    use_status = "used" if "二手" in condition else "new"

    # brief 清理
    _clean_brief = (brief or "").replace("\n", " ").replace("\r", " ").strip()

    # location 归一化 — 台↔臺 / 市↔縣 匹配
    matched_loc = match_location(location, location_options or None)

    # 价格标准化
    try:
        price_val = f"{float(str(price).replace(',', '')):.2f}"
    except (ValueError, AttributeError):
        price_val = price or "0"

    # 数量标准化
    try:
        qty_val = str(int(float(str(qty).replace(",", ""))))
    except (ValueError, AttributeError):
        qty_val = qty or "1"

    # v6.0.50: 大類為古董/偶像 + 二手品 → 自動填「收藏品」標籤
    # 從 auto_publish_feature import,失敗就 fallback 空 list(不影響原本)
    _labels = []
    try:
        from .auto_publish_feature import _build_item_labels
        _labels = _build_item_labels(cat_kw, use_status)
    except Exception:
        pass

    return {
        "type": "basic",
        "title": title,
        "description": {"brief": _clean_brief, "detail": desc or ""},
        "hashtags": list(hashtags) if hashtags else [],
        "labels": _labels,
        "images": image_urls,
        "location": matched_loc,
        "video": {},
        "useStatus": use_status,
        "category": {"id": str(category_id), "attributes": category_attrs or []},
        "payments": payments,
        "purchaseLimit": {"minQuantity": "", "maxQuantity": ""},
        "presale": {},
        "shipments": shipments if shipments is not None else {"isApplyShippingRule": True},
        "product": {
            "models": [{
                "quantity": qty_val,
                "price": {"selling": price_val},
                "partNumber": {"first": "", "second": ""},
                "barcode": "",
            }],
        },
        "buyMorePromotions": [],
        "saveLocation": True,
    }
