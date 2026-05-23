"""純 HTTP 抓 Yahoo 拍賣買家(或賣家)評價統計。

适配自 SSR 內 `<script id="isoredux-data">` 的 `booth` 物件。

URL: `https://tw.bid.yahoo.com/booth/{user_id}`

返回欄位:
- id / name / screen_name
- positive / neutral / negative / positive_ratio / total_rating
- fans_count / join_date / last_online
- avg_ship_day / ship_expired
- is_suspended / status / is_premium
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .client_runtime_compat import CURL_CFFI_IMPERSONATE, get_html_headers
from .merch_http_ops import _detect_system_proxy

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

# isoredux-data inline JSON 在頁面 <script id="isoredux-data" type="application/json">{...}</script>
_RE_ISOREDUX = re.compile(
    r'<script[^>]+id="isoredux-data"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# 簡易記憶體快取(24h),避免重複抓同一買家
_CACHE: Dict[str, Tuple[Dict, float]] = {}
_CACHE_TTL_SEC = 86400


def _now() -> float:
    return time.time()


def fetch_buyer_rating(
    buyer_id: str,
    *,
    on_log: Optional[LogFn] = None,
    timeout: int = 15,
    use_cache: bool = True,
) -> Tuple[Optional[Dict], str]:
    """抓 Yahoo 拍賣使用者(買家或賣家)評價統計。

    buyer_id 是 Y-id(例如 Y9000000002),也接受小寫或無前綴。

    不需要 cookie 即可拉(booth 頁是公開 SSR)。但用 curl_cffi 模仿 Chrome
    避免 client compat block。
    """
    on_log = on_log or (lambda *_: None)
    buyer_id = (buyer_id or "").strip()
    if not buyer_id:
        return None, "buyer_id 為空"
    if not buyer_id.upper().startswith("Y"):
        buyer_id = "Y" + buyer_id
    buyer_id = buyer_id.upper()

    # 緩存命中
    if use_cache:
        cached = _CACHE.get(buyer_id)
        if cached and (_now() - cached[1]) < _CACHE_TTL_SEC:
            return cached[0], ""

    url = f"https://tw.bid.yahoo.com/booth/{buyer_id}"
    # 用 requests(純 Python,不撞 C lib 中文路徑問題)。booth 頁公開 SSR,
    # 不需要 cookie 也不需要 chrome impersonate(client compat 對單純 GET HTML 不嚴)
    try:
        import requests
        headers = get_html_headers()
        # 確保 UA 是常見 Chrome,避免 client compat
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        )
        proxies = None
        proxy = _detect_system_proxy()
        if proxy:
            proxies = {"http": proxy, "https": proxy}
        r = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
        if r.status_code == 404:
            return None, "用戶不存在"
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        html = r.text
    except Exception as e:
        return None, f"fetch 異常: {e}"

    m = _RE_ISOREDUX.search(html)
    if not m:
        return None, "isoredux-data 不在頁面"
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        return None, f"isoredux-data JSON 解析失敗: {e}"

    booth = data.get("booth") or {}
    if not booth:
        return None, "booth 物件不在"

    rating = booth.get("rating") or {}
    stats = booth.get("statisticsInfo") or {}

    result = {
        "id": booth.get("id", buyer_id),
        "name": booth.get("name", ""),
        "screen_name": booth.get("screenName", ""),
        "is_suspended": bool(booth.get("isSuspended", False)),
        "status": booth.get("status", ""),
        "is_premium": bool(booth.get("isPremium", False)),
        "type": booth.get("type", ""),

        # 評價統計
        "positive": int(rating.get("positive", 0) or 0),
        "neutral": int(rating.get("neutral", 0) or 0),
        "negative": int(rating.get("negative", 0) or 0),
        "positive_ratio": float(rating.get("positiveRatio", 0) or 0),
        "total_rating": int(rating.get("score", 0) or 0),

        # 其他資訊
        "fans_count": int(booth.get("fansCount", 0) or 0),
        "join_date": booth.get("joinDate", ""),
        "last_online": booth.get("lastAccessedTime", ""),
        "avg_ship_day": float(stats.get("orderAvgShipDay", 0) or 0),
        "ship_expired": int(stats.get("shipExpired", 0) or 0),
    }

    if use_cache:
        _CACHE[buyer_id] = (result, _now())

    return result, ""


def format_rating_for_card(rating: Dict) -> str:
    """把評價資料格式化成 TG 資訊卡用的多行 markdown 文字。

    返回類似:
    ```
    ⭐ 買家評價
    ──────────────
    正評率 *100.0%* (8 筆)
    ✅ 好評 8   💛 中評 0   ❌ 差評 0

    👥 粉絲 7    📅 加入 2025/06/09
    🕐 1 小時前上線
    🚚 平均出貨 0.5 天   未出貨 0
    ```
    """
    if not rating:
        return ""

    lines = []

    # 帳號異常先警告
    if rating.get("is_suspended"):
        lines.append("⚠️ *帳號已停權*")

    # 評價區塊
    pos = rating.get("positive", 0)
    neu = rating.get("neutral", 0)
    neg = rating.get("negative", 0)
    ratio = rating.get("positive_ratio", 0)
    total = pos + neu + neg

    # 風險判定:
    # - 正評率 < 95% + 評價數 > 20 → 高風險
    # - 正評率 < 90% → 嚴重警告
    # - 評價 0 + 加入 < 30 天 → 新帳號提醒
    risk_tag = ""
    if total >= 20:
        if ratio < 90:
            risk_tag = "  🚨 *高風險*"
        elif ratio < 95:
            risk_tag = "  ⚠️ *風險買家*"
    elif total == 0:
        # 新帳號偵測:join_date 在 30 天內
        join = (rating.get("join_date") or "").strip()
        if join:
            try:
                import time as _t
                jt = _t.mktime(_t.strptime(join, "%Y/%m/%d"))
                if (_t.time() - jt) < 30 * 86400:
                    risk_tag = "  🆕 *新買家*"
            except Exception:
                pass

    lines.append(f"⭐ 買家評價{risk_tag}")
    if total == 0:
        lines.append("尚無評價")
    else:
        lines.append(f"正評率 *{ratio:.1f}%* ({total} 筆)")
        lines.append(f"✅ 好評 {pos}   💛 中評 {neu}   ❌ 差評 {neg}")

    # 其他資訊
    fans = rating.get("fans_count", 0)
    join = rating.get("join_date", "")
    online = rating.get("last_online", "")
    avg_ship = rating.get("avg_ship_day", 0)
    ship_exp = rating.get("ship_expired", 0)

    if fans or join:
        info_line = ""
        if fans:
            info_line += f"👥 粉絲 {fans}"
        if join:
            if info_line:
                info_line += "    "
            info_line += f"📅 加入 {join}"
        if info_line:
            lines.append(info_line)

    if online:
        lines.append(f"🕐 {online}")

    if avg_ship or ship_exp:
        ship_line = ""
        if avg_ship:
            ship_line += f"🚚 平均出貨 {avg_ship} 天"
        if ship_exp:
            if ship_line:
                ship_line += "   "
            ship_line += f"未出貨 {ship_exp} 筆"
        lines.append(ship_line)

    return "\n".join(lines)
