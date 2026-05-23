"""TG Bot Registry — 跨 bot 跳轉的 username 解析 + cache

v6.0.76:首次新增。
- 從每個 bot 的 token 呼叫 Telegram getMe API 拿 username
- 記憶體 cache 24 小時(token → username),避免每次按主菜單都呼叫 API
- 提供統一介面 get_username(bot_kind) 給各 bot 主菜單建 url 按鈕用
- 支援 4 個 bot:purchase / manage / ops / ai_cs

接口:
    init_registry(app) — 啟動時呼叫一次,在背景 thread 拉所有 username
    get_username(bot_kind) -> Optional[str] — 取 cache 中的 username
    get_jump_url(bot_kind) -> Optional[str] — 取 https://t.me/<username>
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple
import requests


_TG_API = "https://api.telegram.org/bot{token}/getMe"
_CACHE_TTL_SEC = 86400  # 24h

# 全域 cache:bot_kind -> (username, fetched_ts)
_cache: Dict[str, Tuple[str, float]] = {}
_cache_lock = threading.Lock()

# 全域 app reference(初始化時設置)
_app = None


def _fetch_username_via_getMe(token: str, timeout: int = 10) -> Optional[str]:
    """直接呼叫 Telegram /getMe 拿 bot username。失敗回 None。"""
    if not token:
        return None
    try:
        r = requests.get(
            _TG_API.format(token=token),
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        result = data.get("result") or {}
        username = (result.get("username") or "").strip()
        return username or None
    except Exception:
        return None


def _bot_kind_to_attr() -> Dict[str, str]:
    """bot_kind → app 屬性名。"""
    return {
        "purchase": "_purchase_tg_bot",
        "manage":   "_manage_tg_bot",
        "ops":      "_ops_tg_bot",
        "ai_cs":    "_tg_bot",  # AI 客服 bot
    }


def _get_token_for(bot_kind: str) -> Optional[str]:
    """從 app 取對應 bot 的 token。"""
    if not _app:
        return None
    attr = _bot_kind_to_attr().get(bot_kind)
    if not attr:
        return None
    bot = getattr(_app, attr, None)
    if not bot:
        return None
    token = getattr(bot, "token", "")
    return token or None


def init_registry(app, log=None) -> None:
    """啟動時呼叫:在背景 thread 抓所有 bot 的 username。"""
    global _app
    _app = app

    def _bg():
        for kind in _bot_kind_to_attr().keys():
            try:
                token = _get_token_for(kind)
                if not token:
                    continue
                username = _fetch_username_via_getMe(token)
                if username:
                    with _cache_lock:
                        _cache[kind] = (username, time.time())
                    if log:
                        log(f"[BOT-REG] {kind} → @{username}")
                else:
                    if log:
                        log(f"[BOT-REG] {kind} username 取得失敗")
            except Exception as e:
                if log:
                    log(f"[BOT-REG] {kind} 取得 username 例外:{e}")

    threading.Thread(target=_bg, daemon=True, name="tg-bot-registry").start()


def get_username(bot_kind: str) -> Optional[str]:
    """取 cache 中的 username,過期/未命中時 lazy 重新抓一次。"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(bot_kind)
        if hit:
            username, ts = hit
            if now - ts < _CACHE_TTL_SEC:
                return username

    # cache miss / 過期:同步抓一次(會慢一些,但 1 次就好)
    token = _get_token_for(bot_kind)
    if not token:
        return None
    username = _fetch_username_via_getMe(token)
    if username:
        with _cache_lock:
            _cache[bot_kind] = (username, now)
    return username


def get_jump_url(bot_kind: str) -> Optional[str]:
    """取 https://t.me/<username> 形式的跳轉 URL。"""
    u = get_username(bot_kind)
    if not u:
        return None
    return f"https://t.me/{u}"


def get_all_jump_buttons(exclude_kind: str = "") -> list:
    """取所有可用的跨 bot 跳轉按鈕(已排除自己)。
    回 List[Dict] 形式,可直接放到 make_keyboard 的 row 裡。
    """
    labels = {
        "purchase": "📦 採購",
        "manage":   "🔧 管理",
        "ops":      "💼 運營",
        "ai_cs":    "🤖 AI 客服",
    }
    btns = []
    for kind, label in labels.items():
        if kind == exclude_kind:
            continue
        url = get_jump_url(kind)
        if url:
            btns.append({"text": f"→ {label}", "url": url})
    return btns
