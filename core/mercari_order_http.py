"""纯 HTTP 查询煤炉(Mercari)交易详情（不需要浏览器）。

替代 purchase_feature.py 的 Playwright scrape_mercari() 方案。

认证机制:
  Authorization: <accessToken>  (从 localStorage.authTokenData 提取)
  Dpop: <ES256 JWT>             (每次请求生成)

API (2026-03 discover_mercari_api.py 发现):
  交易详情: GET /transaction_evidences/get?item_id={item_id}&_datetime_format=U
  响应: {data: {paid_price, created, status, shipping_class_carrier, ...}}

依赖: curl_cffi, cryptography (DPOP签名), mercari_token_store
"""
from __future__ import annotations

import json
import logging
import time
import uuid
import base64
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import CURL_CFFI_IMPERSONATE, CHROME_HTTP_UA
from .mercari_token_store import (
    load_mercari_token,
    invalidate_mercari_token,
    MERCARI_TOKEN_MAX_AGE,
)

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# ── 常量 ──────────────────────────────────────────────

MERCARI_API_BASE = "https://api.mercari.jp"

HEADERS_BASE = {
    "User-Agent": CHROME_HTTP_UA,  # v6.1:對齊 curl_cffi chrome136 HTTP 客户端 profile
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "X-Platform": "web",
    "Origin": "https://jp.mercari.com",
    "Referer": "https://jp.mercari.com/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ── DPOP Token 生成 (ES256 JWT) ──────────────────────

# 使用 cryptography 库生成 EC P-256 密钥对 (进程生命周期内复用)
_dpop_private_key = None
_dpop_jwk = None


def _ensure_dpop_keypair():
    """确保 DPOP 密钥对已生成 (惰性初始化)。"""
    global _dpop_private_key, _dpop_jwk
    if _dpop_private_key is not None:
        return

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend

    _dpop_private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = _dpop_private_key.public_key()
    pub_numbers = pub.public_numbers()

    # 导出 JWK 格式的公钥 (x, y 坐标)
    x_bytes = pub_numbers.x.to_bytes(32, byteorder="big")
    y_bytes = pub_numbers.y.to_bytes(32, byteorder="big")

    _dpop_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(x_bytes),
        "y": _b64url(y_bytes),
    }


def _b64url(data: bytes) -> str:
    """Base64url 编码 (无 padding)。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _create_dpop_token(method: str, url: str) -> str:
    """生成 DPOP JWT (ES256 签名)。

    Header: { typ: "dpop+jwt", alg: "ES256", jwk: {kty,crv,x,y} }
    Payload: { iat: <unix_sec>, jti: <uuid>, htu: <url>, htm: <method> }
    Signature: ECDSA-P256 (IEEE P1363 format = r || s, 各32字节)
    """
    _ensure_dpop_keypair()

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": _dpop_jwk,
    }
    payload = {
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "htu": url,
        "htm": method.upper(),
    }

    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())

    signing_input = f"{h_b64}.{p_b64}".encode("ascii")

    # ECDSA 签名 (DER 格式 → 转换为 IEEE P1363 = r || s)
    der_sig = _dpop_private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(
        # decode_dss_signature 接受 DER 格式
        der_sig if isinstance(der_sig, bytes) else der_sig
    )
    # r 和 s 各 32 字节 (P-256)
    r_bytes = r.to_bytes(32, byteorder="big")
    s_bytes = s.to_bytes(32, byteorder="big")
    sig_b64 = _b64url(r_bytes + s_bytes)

    return f"{h_b64}.{p_b64}.{sig_b64}"


# ── HTTP 会话 ─────────────────────────────────────────

def _build_mercari_session(
    profile_dir: Path,
) -> Tuple[Optional[CffiSession], str, str]:
    """构建 Mercari HTTP session。

    Returns:
        (session, access_token, error_msg)
    """
    access_token, user_id = load_mercari_token(
        profile_dir, max_age=MERCARI_TOKEN_MAX_AGE
    )
    if not access_token:
        return None, "", "no_token"

    s = CffiSession(impersonate=CURL_CFFI_IMPERSONATE)
    return s, access_token, ""


# ── API 调用 ─────────────────────────────────────────

def _call_mercari_api(
    session: CffiSession,
    access_token: str,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> Tuple[dict, str]:
    """调用一次 Mercari API。

    Returns:
        (response_data, error_code)
    """
    url = f"{MERCARI_API_BASE}{path}"

    # 生成 DPOP token (url 不含 query string)
    dpop_url = url.split("?")[0]
    dpop_token = _create_dpop_token(method, dpop_url)

    headers = dict(HEADERS_BASE)
    headers["Authorization"] = access_token
    headers["Dpop"] = dpop_token

    try:
        if method.upper() == "GET":
            resp = session.get(url, params=params, headers=headers, timeout=20)
        else:
            resp = session.post(url, params=params, json=json_body or {}, headers=headers, timeout=20)
    except Exception as e:
        return {}, f"network:{e}"

    if resp.status_code == 401:
        return {}, "auth_expired"

    if resp.status_code == 403:
        return {}, "forbidden"

    if resp.status_code == 404:
        return {}, "not_found"

    if resp.status_code >= 500:
        return {}, f"server_error:{resp.status_code}"

    try:
        result = resp.json()
    except Exception:
        return {}, f"parse:status={resp.status_code}"

    # Mercari API 正常响应: {"result": "OK", "data": {...}}
    if result.get("result") == "OK" or "data" in result:
        return result.get("data", result), ""

    # 错误响应
    code = result.get("code", "")
    message = result.get("message", "")
    if code or message:
        return {}, f"api_error:{code}:{message}"

    return result, ""


# ── 响应解析 ──────────────────────────────────────────

def _parse_transaction_response(data: dict) -> Dict[str, str]:
    """从 /transaction_evidences/get 响应中提取订单信息。

    响应结构 (2026-03 discover 确认):
    {
      "item_id": "m90000000005",
      "item_name": "天使の羽 ペンダント ネックレス",
      "price": 949,
      "paid_price": 949,
      "status": "done",
      "created": 1700000000,          // Unix 秒 (购入时间)
      "shipping_class_carrier": "japan_post",
      "is_delivered": false,
      ...
    }

    注: 已完成(done)交易的响应不含物流单号。
    运送中(wait_review)交易可能包含物流单号 — 字段名待确认。
    """
    fields = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    if not data:
        return fields

    # 成交价 (paid_price 优先, 否则 price)
    paid = data.get("paid_price") or data.get("price")
    if paid is not None:
        fields["pay_amount"] = str(paid)

    # 购入时间 (created 是 Unix 秒)
    created = data.get("created")
    if created:
        ts = int(created)
        fields["pay_dt_raw"] = time.strftime("%Y年%m月%d日 %H:%M", time.localtime(ts))

    # 物流单号 — 多种可能的字段名 (Mercari便 在运送中时应该有)
    for key in (
        "tracking_no", "tracking_number", "trackingCode",
        "trackingNumber", "mail_no", "barcode",
        "shipping_tracking_number", "invoice_number",
    ):
        val = str(data.get(key) or "").strip()
        if val:
            fields["tracking_no"] = val
            break

    # 调试: 记录未知字段 (帮助发现运送中交易的物流字段名)
    if not fields["tracking_no"]:
        status = data.get("status", "")
        if status not in ("done", "wait_payment"):
            # 运送中但没找到 tracking — 记录所有非标准字段帮助调试
            _known = {
                "showable_address", "id", "status", "buyer_id",
                "zip_code1", "zip_code2", "family_name", "first_name",
                "family_name_kana", "first_name_kana", "prefecture",
                "city", "address1", "address2", "state_abbreviation",
                "paid_method", "item_id", "item_name", "seller_id",
                "price", "consume_point", "consume_sales", "payment_fee",
                "paid_price", "description", "category_id", "item_condition",
                "size", "brand_name", "shipping_payer", "shipping_method",
                "shipping_from_area", "shipping_duration", "shipping_class",
                "shipping_class_carrier", "seller_shipping_fee",
                "buyer_shipping_fee", "seller_additional_fee", "pager_id",
                "grace_period", "updated", "created",
                "shipping_class_carrier_display_name",
                "is_anonymous_shipping", "can_anonymous_shipping_user",
                "is_shop_item", "require_kyc", "should_update_to_address",
                "is_cancelable_by_buyer", "is_cancelable_by_seller",
                "is_immediate_cancelable_by_buyer", "is_delivered",
                "discount_by_coupon", "auto_review", "should_alert_category",
                "current_status_set_at",
                "is_seller_requested_review_from_buyer",
                "coupon_type", "post_add_point_by_coupon",
                "delivery_facility_type", "receive_at_facility",
                "additional_services", "consume_funds",
                "consume_docomo_point", "consume_crypto",
                "is_long_journey", "early_payout", "ekyc_item_attributes",
                "code_group", "store",
            }
            extra = {k: v for k, v in data.items() if k not in _known}
            if extra:
                log.info("[mercari_order] status=%s 发现未知字段: %s",
                         status, list(extra.keys()))

    return fields


# ── 主入口 ────────────────────────────────────────────

def fetch_mercari_transaction_http(
    item_id: str,
    profile_dir: Path,
    *,
    log_fn: Optional[LogFn] = None,
) -> Tuple[Dict[str, str], str]:
    """HTTP 查询煤炉交易详情。

    Args:
        item_id: 煤炉商品号 (e.g. "m90000000005")
        profile_dir: purchase_monitor profile 目录

    Returns:
        (fields, error)
        fields: {"pay_amount": "...", "pay_dt_raw": "...", "tracking_no": "..."}
        error: "" 表示成功
    """
    empty = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    session, access_token, err = _build_mercari_session(profile_dir)
    if not session:
        return empty, err

    if log_fn:
        log_fn(f"[采购HTTP] 查询煤炉交易 {item_id}...")

    # GET /transaction_evidences/get?item_id={item_id}&_datetime_format=U
    data, err = _call_mercari_api(
        session, access_token, "GET",
        "/transaction_evidences/get",
        params={"item_id": item_id, "_datetime_format": "U"},
    )

    if err:
        if err == "auth_expired":
            # token 可能被浏览器刷新了，从 LevelDB 重新提取最新 token 重试
            if log_fn:
                log_fn("[采购HTTP] token 被拒, 尝试从 LevelDB 重新提取...")
            from .mercari_token_store import extract_token_from_profile
            if extract_token_from_profile(profile_dir):
                session2, access_token2, err2 = _build_mercari_session(profile_dir)
                if session2:
                    data, err = _call_mercari_api(
                        session2, access_token2, "GET",
                        "/transaction_evidences/get",
                        params={"item_id": item_id, "_datetime_format": "U"},
                    )
                    if not err:
                        session, access_token = session2, access_token2
                    else:
                        invalidate_mercari_token(profile_dir)
                        if log_fn:
                            log_fn(f"[采购HTTP] 重试仍失败: {err}")
                        return empty, err
                else:
                    invalidate_mercari_token(profile_dir)
                    return empty, "no_token"
            else:
                invalidate_mercari_token(profile_dir)
                if log_fn:
                    log_fn("[采购HTTP] LevelDB 提取失败, 需重新登录")
                return empty, err
        else:
            if log_fn:
                log_fn(f"[采购HTTP] 煤炉查询失败: {err}")
            return empty, err

    fields = _parse_transaction_response(data)

    # ── 2. 如果没有物流单号，调用物流API获取 ──
    if not fields["tracking_no"]:
        tracking = _fetch_tracking_no(session, access_token, data, log_fn=log_fn)
        if tracking:
            fields["tracking_no"] = tracking

    if log_fn:
        log_fn(f"[采购HTTP] 煤炉解析结果: {fields}")

    return fields, ""


def _fetch_tracking_no(
    session: CffiSession,
    access_token: str,
    transaction_data: dict,
    *,
    log_fn: Optional[LogFn] = None,
) -> str:
    """从物流API获取运单号。

    根据 shipping_class_carrier 调用不同的物流API:
      - japan_post → GET /delivery_japan_post/status  → denpyo_no
      - yamato (らくらくメルカリ便) → GET /shipping/get_info → label_id
      - 其他 → GET /delivery/status → denpyo_no

    参数: transaction_evidence_id (从 transaction_evidences/get 的 id 字段)
    """
    te_id = transaction_data.get("id")
    if not te_id:
        return ""

    carrier = str(transaction_data.get("shipping_class_carrier") or "").lower()
    status = str(transaction_data.get("status") or "")

    # 未发货的订单不查物流
    if status in ("wait_payment", "wait_shipping"):
        return ""

    params = {"transaction_evidence_id": str(te_id)}

    # 按 carrier 类型尝试不同的物流API
    api_attempts = []

    if "japan_post" in carrier:
        api_attempts.append(("/delivery_japan_post/status", "denpyo_no"))
    elif "yamato" in carrier:
        api_attempts.append(("/shipping/get_info", "label_id"))
        api_attempts.append(("/delivery/status", "denpyo_no"))
    else:
        # 未知 carrier — 全部尝试
        api_attempts.append(("/shipping/get_info", "label_id"))
        api_attempts.append(("/delivery/status", "denpyo_no"))
        api_attempts.append(("/delivery_japan_post/status", "denpyo_no"))

    for api_path, tracking_key in api_attempts:
        result, err = _call_mercari_api(
            session, access_token, "GET", api_path, params=params,
        )
        if err:
            if log_fn and err != "not_found":
                log_fn(f"[采购HTTP] 物流API {api_path} 失败: {err}")
            continue

        tracking = str(result.get(tracking_key) or "").strip()
        if tracking:
            if log_fn:
                log_fn(f"[采购HTTP] 物流单号获取成功: {tracking} (via {api_path})")
            return tracking

    return ""
