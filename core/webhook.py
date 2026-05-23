from __future__ import annotations
import json
import time
import requests
from urllib.parse import urlparse
from typing import Dict, Any, Optional

# 允许的企业微信 Webhook 域名白名单
_ALLOWED_HOSTS = {
    "qyapi.weixin.qq.com",
}

def _validate_webhook_url(url: str) -> Optional[str]:
    """校验 Webhook URL 是否为合法的企业微信域名，防止 SSRF。
    返回 None 表示合法，否则返回错误信息。
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return "Webhook URL 无效（无法解析域名）"
        if host not in _ALLOWED_HOSTS:
            return f"Webhook 域名不在白名单中：{host}（仅允许 {', '.join(_ALLOWED_HOSTS)}）"
        if parsed.scheme not in ("http", "https"):
            return f"Webhook URL 协议不合法：{parsed.scheme}"
    except Exception as e:
        return f"Webhook URL 解析失败：{e}"
    return None

def send_wecom_text(webhook_url: str, text: str, max_retries: int = 2) -> Optional[str]:
    if not webhook_url:
        return "Webhook 未设置"
    # 域名校验
    err = _validate_webhook_url(webhook_url)
    if err:
        return err
    payload = {"msgtype": "text", "text": {"content": text}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(webhook_url, data=data,
                              headers=headers, timeout=10)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                # 5xx 服务端错误才重试，4xx 直接返回
                if r.status_code < 500:
                    return last_err
            else:
                # 企业微信成功一般返回 {"errcode":0,"errmsg":"ok"}
                return None
        except Exception as e:
            last_err = str(e)
        # 重试前等待
        if attempt < max_retries:
            time.sleep(1.0 * (attempt + 1))
    return last_err
