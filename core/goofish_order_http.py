"""纯 HTTP 查询闲鱼订单详情（不需要浏览器）。

替代 purchase_feature.py 的 Playwright scrape_xianyu() 方案。
使用 mtop H5 API + 买家 cookie 认证。

流程:
  1. goofish_cookie_store 加载 cookies + _m_h5_tk token
  2. 签名 + POST mtop API → 解析 JSON 响应
  3. 提取: pay_amount, pay_dt_raw, tracking_no
  4. token 过期 → 自动通过牺牲 API 刷新 → 重试

API (2026-03 discover_order_api.py 发现):
  订单详情: mtop.idle.web.trade.order.detail v1.0  payload={"tid":"<order_id>"}
  物流信息: mtop.cainiao.ld.detail.tradeid.ordercode.mailno.rescode.get.xy v1.0
            payload={"tradeId":"<order_id>"}

依赖: curl_cffi, goofish_cookie_store
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import CURL_CFFI_IMPERSONATE, CHROME_HTTP_UA
from .goofish_cookie_store import (
    load_goofish_cookies,
    load_goofish_raw_cookies,
    invalidate_goofish_cookies,
    update_goofish_token,
    GOOFISH_SESSION_MAX_AGE,
)

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# ── 常量 ──────────────────────────────────────────────

# APP_KEY: <XIANYU_APP_KEY_REDACTED> = PC web 版 (需要认证 cookie)
# <GOOFISH_APP_KEY_REDACTED> = H5 mobile (公开 API, 不需登录, 用于 goofish_check_feature)
# 最终以 discover_order_api.py 发现结果为准
APP_KEY = "<XIANYU_APP_KEY_REDACTED>"

API_BASE = "https://h5api.m.goofish.com/h5"

# Token 刷新 "牺牲 API" (轻量级, 只为获取 Set-Cookie 里的新 _m_h5_tk)
TOKEN_REFRESH_API = "mtop.taobao.idle.item.web.recommend.list"
TOKEN_REFRESH_VER = "1.0"

# ── 订单详情 API (discover_order_api.py 2026-03 确认) ──
ORDER_DETAIL_API = "mtop.idle.web.trade.order.detail"
ORDER_DETAIL_VER = "1.0"

# ── 物流详情 API ──
LOGISTICS_API = "mtop.cainiao.ld.detail.tradeid.ordercode.mailno.rescode.get.xy"
LOGISTICS_VER = "1.0"

# ── 国内中转服务器 (session_expired 时通过国内 IP 重试) ──
RELAY_MTOP_URL = "http://<RELAY_IP_REDACTED>:18899/proxy_mtop"

HEADERS_BASE = {
    "User-Agent": CHROME_HTTP_UA,  # v6.1:對齊 curl_cffi chrome136 HTTP 客户端 profile
    "Referer": "https://www.goofish.com/",
    "Origin": "https://www.goofish.com",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ── 签名 ──────────────────────────────────────────────

def _sign(token: str, t: str, data: str) -> str:
    """MD5(token & t & appKey & data)"""
    s = f"{token}&{t}&{APP_KEY}&{data}"
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _ts_ms() -> str:
    """当前时间戳 (毫秒)"""
    return str(int(time.time() * 1000))


# ── HTTP 会话 ─────────────────────────────────────────

def _build_goofish_session(
    profile_dir: Path,
) -> Tuple[Optional[CffiSession], str, str]:
    """从缓存 cookie 构建 curl_cffi session。

    Returns:
        (session, token_hex, error_msg)
        session 为 None 表示失败。
    """
    cookies, token_hex, saved_at = load_goofish_cookies(
        profile_dir, max_age=GOOFISH_SESSION_MAX_AGE
    )
    if not cookies:
        return None, "", "no_cookies"

    # 历史遗留 cache 可能含有煤炉/广告追踪等数百条非 Alibaba cookie,
    # 全部塞 .goofish.com 会导致 Cookie header > 8KB → 闲鱼网关回 431。
    # 这里按 raw_cookies 的 domain 过滤,只保留 Alibaba 系真正需要的 cookie。
    raw_cookies = load_goofish_raw_cookies(profile_dir, max_age=GOOFISH_SESSION_MAX_AGE)
    if raw_cookies:
        # 用 raw 域名信息过滤,精确
        from core.goofish_cookie_store import _is_goofish_domain
        ali_names = {c["name"] for c in raw_cookies
                     if c.get("name") and _is_goofish_domain(c.get("domain", ""))}
        if ali_names:
            cookies = {k: v for k, v in cookies.items() if k in ali_names}

    s = CffiSession(impersonate=CURL_CFFI_IMPERSONATE, proxy="")  # proxy="" → 替代路径系统代理(VPN)

    # 设置 cookie (闲鱼 cookie 跨多个域名)
    for name, value in cookies.items():
        # _m_h5_tk / unb 等在 .goofish.com, sid 等在 .taobao.com
        # 简单起见，对两个域都设置
        s.cookies.set(name, value, domain=".goofish.com")
        s.cookies.set(name, value, domain=".taobao.com")

    return s, token_hex, ""


# ── Token 刷新 ────────────────────────────────────────

def _refresh_token(
    session: CffiSession,
    profile_dir: Path,
) -> Tuple[str, bool]:
    """通过牺牲 API 刷新 _m_h5_tk token (纯 HTTP, 不需要浏览器)。

    Returns:
        (new_token_hex, success)
    """
    payload = json.dumps(
        {"itemId": "0", "pageSize": 1, "pageNum": 1},
        separators=(",", ":"), ensure_ascii=False,
    )
    t = _ts_ms()
    # 空 token 签名 (首次获取 / token 过期)
    sign = _sign("", t, payload)

    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": t,
        "sign": sign,
        "v": TOKEN_REFRESH_VER,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "AntiCreep": "true",
        "AntiFlool": "true",
        "api": TOKEN_REFRESH_API,
    }

    url = f"{API_BASE}/{TOKEN_REFRESH_API}/{TOKEN_REFRESH_VER}/"

    try:
        resp = session.post(
            url,
            params=params,
            data={"data": payload},
            headers=HEADERS_BASE,
            timeout=15,
        )

        # 从 Set-Cookie 头提取新 token
        set_cookie = resp.headers.get("set-cookie", "")
        m = re.search(r'_m_h5_tk=([^;]+)', set_cookie)
        new_tk = m.group(1) if m else None
        m_enc = re.search(r'_m_h5_tk_enc=([^;]+)', set_cookie)
        new_enc = m_enc.group(1) if m_enc else None

        # 备用: 从 session cookie jar 读取
        if not new_tk:
            new_tk = session.cookies.get("_m_h5_tk")
        if not new_enc:
            new_enc = session.cookies.get("_m_h5_tk_enc")

        if new_tk and "_" in new_tk:
            new_token = new_tk.split("_")[0]
            # 更新 session cookie jar
            session.cookies.set("_m_h5_tk", new_tk, domain=".goofish.com")
            if new_enc:
                session.cookies.set("_m_h5_tk_enc", new_enc, domain=".goofish.com")
            # 回写缓存
            update_goofish_token(profile_dir, new_tk, new_enc or "")
            log.info("[goofish_http] token 刷新成功: %s...", new_token[:12])
            return new_token, True

        log.warning("[goofish_http] token 刷新未获得新值, status=%d", resp.status_code)
        return "", False

    except Exception as e:
        log.error("[goofish_http] token 刷新异常: %s", e)
        return "", False


# ── mtop API 调用 ─────────────────────────────────────

def _call_mtop(
    session: CffiSession,
    token: str,
    api: str,
    version: str,
    data: dict,
    *,
    referer: str = "",
) -> Tuple[dict, str]:
    """调用一次 mtop API。

    Returns:
        (response_data, error_code)
        error_code: "" 表示成功
    """
    data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    t = _ts_ms()
    sign = _sign(token, t, data_str)

    url = f"{API_BASE}/{api}/{version}/"

    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": t,
        "sign": sign,
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "AntiCreep": "true",
        "AntiFlool": "true",
        "api": api,
        "sessionOption": "AutoLoginOnly",
    }

    headers = dict(HEADERS_BASE)
    if referer:
        headers["Referer"] = referer

    try:
        resp = session.post(
            url, params=params,
            data={"data": data_str},
            headers=headers,
            timeout=20,
        )
    except Exception as e:
        return {}, f"network:{e}"

    try:
        result = resp.json()
    except Exception:
        return {}, f"parse:status={resp.status_code}"

    ret = result.get("ret", [])
    ret_str = str(ret[0]) if ret else ""

    if "SUCCESS" in ret_str:
        return result.get("data", {}), ""

    if "FAIL_SYS_TOKEN_EXOIRED" in ret_str or "TOKEN_EXOIRED" in ret_str:
        return {}, "token_expired"

    if "RGV587" in ret_str:
        return {}, "rate_limit"

    if "SESSION_EXPIRED" in ret_str:
        return {}, "session_expired"

    if "FAIL_SYS_ILLEGAL_ACCESS" in ret_str:
        return {}, "illegal_access"

    # 有些 API 虽然 ret 不含 SUCCESS 但有 data
    data_part = result.get("data", {})
    if data_part:
        return data_part, ""

    return {}, f"unknown:{ret_str[:100]}"


def _call_with_retry(
    session: CffiSession,
    token: str,
    api: str,
    version: str,
    data: dict,
    profile_dir: Path,
    *,
    log_fn: Optional[LogFn] = None,
) -> Tuple[dict, str, str]:
    """调用 mtop API，token 过期时自动刷新重试。

    Returns:
        (response_data, error_code, current_token)
    """
    result, err = _call_mtop(session, token, api, version, data)
    if not err:
        return result, "", token

    if err == "token_expired":
        if log_fn:
            log_fn("[采购HTTP] token 过期, 自动刷新...")
        new_token, ok = _refresh_token(session, profile_dir)
        if ok:
            result2, err2 = _call_mtop(session, new_token, api, version, data)
            return result2, err2, new_token
        return {}, "token_expired", token

    return {}, err, token


# ── 国内中转调用 ─────────────────────────────────────

def _call_via_relay(
    cookies: dict,
    token: str,
    api: str,
    version: str,
    data: dict,
    profile_dir: Path,
    *,
    log_fn: Optional[LogFn] = None,
) -> Tuple[dict, str]:
    """通过国内中转服务器调用 mtop API（解决 VPN 全局模式下 session_expired）。

    Returns:
        (response_data, error_code)
    """
    payload = json.dumps({
        "api": api,
        "version": version,
        "data": data,
        "cookies": cookies,
        "token": token,
        "app_key": APP_KEY,
    }, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        RELAY_MTOP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        if log_fn:
            log_fn(f"[采购HTTP] 中转请求失败: {e}")
        return {}, f"relay_error:{e}"

    if not body.get("ok"):
        relay_err = body.get("error", "unknown")
        if log_fn:
            log_fn(f"[采购HTTP] 中转返回错误: {relay_err}")
        return {}, f"relay_error:{relay_err}"

    result = body.get("result", {})

    # 如果中转返回了新 token, 更新本地缓存
    new_tk = body.get("new_m_h5_tk")
    if new_tk and "_" in new_tk:
        update_goofish_token(profile_dir, new_tk, "")
        if log_fn:
            log_fn(f"[采购HTTP] 中转返回新 token: {new_tk[:16]}...")

    # 解析 ret
    ret = result.get("ret", [])
    ret_str = str(ret[0]) if ret else ""

    if "SUCCESS" in ret_str:
        return result.get("data", {}), ""

    if "FAIL_SYS_TOKEN_EXOIRED" in ret_str or "TOKEN_EXOIRED" in ret_str:
        return {}, "token_expired"

    if "SESSION_EXPIRED" in ret_str:
        return {}, "session_expired"

    # 有些 API 虽然 ret 不含 SUCCESS 但有 data
    data_part = result.get("data", {})
    if data_part:
        return data_part, ""

    return {}, f"relay_unknown:{ret_str[:100]}"


# ── 响应解析 ──────────────────────────────────────────

def _parse_order_response(data: dict) -> Dict[str, str]:
    """从 mtop.idle.web.trade.order.detail 响应中提取订单信息。

    响应结构 (2026-03 discover 确认):
    {
      "orderId": "...",
      "status": 3,
      "components": [
        ...,
        { "data": {
            "priceInfo": {
              "amount": { "title": "成交价", "value": "1199.00" },
              "billList": [...]
            },
            "orderInfoList": [
              { "title": "订单编号", "value": "..." },
              { "title": "付款时间", "value": "2026-03-06 00:42:05" },
              { "title": "发货时间", "value": "2026-03-06 10:26:30" },
              ...
            ],
            "itemInfo": { "price": "1200.00", "buyAmount": "1" }
        }},
        ...
      ]
    }

    Returns:
        {"pay_amount": "...", "pay_dt_raw": "...", "tracking_no": ""}
        注: tracking_no 需要单独调用物流 API 获取
    """
    fields = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    if not data:
        return fields

    # ── 从 components 中找到含 priceInfo + orderInfoList 的组件 ──
    order_comp_data = None
    for comp in data.get("components", []):
        cd = comp if isinstance(comp, dict) and "priceInfo" in comp else None
        if not cd:
            cd = comp.get("data", {}) if isinstance(comp, dict) else {}
        if isinstance(cd, dict) and ("priceInfo" in cd or "orderInfoList" in cd):
            order_comp_data = cd
            break

    if order_comp_data:
        # 成交价
        price_info = order_comp_data.get("priceInfo", {})
        amount = price_info.get("amount", {})
        if amount.get("value"):
            fields["pay_amount"] = str(amount["value"])

        # 从 orderInfoList 提取付款时间
        for item in order_comp_data.get("orderInfoList", []):
            title = item.get("title", "")
            value = item.get("value", "")
            if "付款" in title and "时间" in title and value:
                fields["pay_dt_raw"] = value
                break

    # ── 备用: 通用深度搜索 (万一 components 结构变了) ──
    if not fields["pay_amount"] or not fields["pay_dt_raw"]:
        flat = _flatten_json(data)

        if not fields["pay_dt_raw"]:
            for key in ("payTime", "pay_time", "gmtPay", "payDate", "paidTime",
                        "gmtPayTime", "paymentTime"):
                if key in flat and flat[key]:
                    val = str(flat[key])
                    if val.isdigit() and len(val) >= 13:
                        ts = int(val) / 1000
                        fields["pay_dt_raw"] = time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    else:
                        fields["pay_dt_raw"] = val
                    break

        if not fields["pay_amount"]:
            for key in ("realPayFee", "actualFee", "payFee", "realTotalFee",
                        "actualTotalFee", "buyerPayAmount"):
                if key in flat and flat[key]:
                    val = str(flat[key]).replace("¥", "").replace("￥", "").strip()
                    fields["pay_amount"] = val
                    break

    return fields


def _parse_logistics_response(data: dict) -> str:
    """从物流 API 响应中提取运单号。

    响应结构:
    {
      "detailViewList": [{
        "companyList": [{
          "mailNo": "SF0258022451838",
          "companyName": "顺丰速运",
          "resCode": "SF"
        }]
      }]
    }

    Returns:
        运单号字符串, 无则返回 ""
    """
    if not data:
        return ""
    for dv in data.get("detailViewList", []):
        for co in dv.get("companyList", []):
            mail_no = co.get("mailNo", "")
            if mail_no and len(mail_no) >= 8:
                return mail_no
    return ""


def _flatten_json(obj, prefix="", result=None) -> Dict[str, str]:
    """将嵌套 JSON 扁平化为 {key: value} 字典（只取叶子节点最后一层 key）。"""
    if result is None:
        result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten_json(v, k, result)
            else:
                result[k] = v
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json(item, prefix, result)
    return result


# ── 主入口 ────────────────────────────────────────────

def fetch_order_detail_http(
    order_id: str,
    profile_dir: Path,
    *,
    log_fn: Optional[LogFn] = None,
) -> Tuple[Dict[str, str], str]:
    """HTTP 查询闲鱼订单详情。

    Args:
        order_id: 闲鱼订单号
        profile_dir: purchase_monitor profile 目录

    Returns:
        (fields, error)
        fields: {"pay_amount": "...", "pay_dt_raw": "...", "tracking_no": "..."}
        error: "" 表示成功, 否则为错误码
    """
    empty = {"pay_amount": "", "pay_dt_raw": "", "tracking_no": ""}

    session, token, err = _build_goofish_session(profile_dir)
    if not session:
        return empty, err

    if not token:
        if log_fn:
            log_fn("[采购HTTP] token 为空, 尝试刷新...")
        token, ok = _refresh_token(session, profile_dir)
        if not ok:
            return empty, "token_expired"

    # ── 1. 查询订单详情 (payload 用 tid 不是 orderId) ──
    order_payload = {"tid": str(order_id)}
    referer = f"https://www.goofish.com/order-detail?orderId={order_id}"

    if log_fn:
        log_fn(f"[采购HTTP] 查询订单 {order_id}...")

    result, err, token = _call_with_retry(
        session, token,
        ORDER_DETAIL_API, ORDER_DETAIL_VER,
        order_payload, profile_dir,
        log_fn=log_fn,
    )

    if err:
        if err == "session_expired":
            # VPN 全局模式下可能因非国内 IP 导致 session_expired
            # 尝试通过国内中转服务器重试
            if log_fn:
                log_fn(f"[采购HTTP] 订单查询 session_expired, 尝试国内中转...")
            cookies_dict, _, _ = load_goofish_cookies(
                profile_dir, max_age=GOOFISH_SESSION_MAX_AGE
            )
            if cookies_dict:
                relay_result, relay_err = _call_via_relay(
                    cookies_dict, token,
                    ORDER_DETAIL_API, ORDER_DETAIL_VER,
                    order_payload, profile_dir,
                    log_fn=log_fn,
                )
                if not relay_err:
                    if log_fn:
                        log_fn("[采购HTTP] 国内中转成功!")
                    fields = _parse_order_response(relay_result)
                    # 物流也走中转
                    logi_relay, logi_relay_err = _call_via_relay(
                        cookies_dict, token,
                        LOGISTICS_API, LOGISTICS_VER,
                        {"tradeId": str(order_id)}, profile_dir,
                        log_fn=log_fn,
                    )
                    if not logi_relay_err:
                        tracking = _parse_logistics_response(logi_relay)
                        if tracking:
                            fields["tracking_no"] = tracking
                    if log_fn:
                        log_fn(f"[采购HTTP] 中转解析结果: {fields}")
                    return fields, ""
                else:
                    if log_fn:
                        log_fn(f"[采购HTTP] 中转也失败: {relay_err}")
                    # 只有中转(国内IP)也返回 session_expired 才 invalidate
                    # 如果是中转网络错误等，不 invalidate（cookies 本身可能没问题）
                    if "session_expired" in relay_err:
                        invalidate_goofish_cookies(profile_dir)
                    return empty, relay_err
            # 没有 cookies_dict → 缓存确实无效
            invalidate_goofish_cookies(profile_dir)
        if log_fn:
            log_fn(f"[采购HTTP] 订单查询失败: {err}")
        return empty, err

    fields = _parse_order_response(result)

    # ── 2. 查询物流信息 (获取运单号) ──
    logistics_payload = {"tradeId": str(order_id)}

    logi_result, logi_err, token = _call_with_retry(
        session, token,
        LOGISTICS_API, LOGISTICS_VER,
        logistics_payload, profile_dir,
        log_fn=log_fn,
    )

    if not logi_err:
        tracking = _parse_logistics_response(logi_result)
        if tracking:
            fields["tracking_no"] = tracking
    elif log_fn:
        log_fn(f"[采购HTTP] 物流查询失败 (非致命): {logi_err}")

    if log_fn:
        log_fn(f"[采购HTTP] 解析结果: {fields}")

    return fields, ""
