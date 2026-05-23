"""買家深度智能分析 — 從 BOSH 對話歷史挖出多維度指標。

從 BOSH `queryMessage` 拉最近 200 條訊息分析:
- 對方未讀我訊息數(我發了但對方 lastReadTime 之後的)
- 議價傾向(關鍵字+次數+平均砍價幅度)
- 平均回應時間
- 活躍時段(對方常發訊息的小時範圍)
- 首次互動日

+ LLM 客戶優質度評分(評價+歷史+訊息語氣綜合)

整合到 forum topic 資訊卡顯示。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# v6.1:中文路徑 curl_cffi cert workaround(同 yahoo_im_read_status)
def _ensure_ssl_cert_env() -> None:
    import os
    if os.environ.get("CURL_CA_BUNDLE"):
        return
    try:
        import certifi, shutil, tempfile
        src = certifi.where()
        if src and any(ord(c) > 127 for c in src):
            dst = os.path.join(tempfile.gettempdir(), "cacert_ascii.pem")
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            os.environ["SSL_CERT_FILE"] = dst
            os.environ["CURL_CA_BUNDLE"] = dst
            os.environ["REQUESTS_CA_BUNDLE"] = dst
    except Exception:
        pass

_ensure_ssl_cert_env()

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

# 議價關鍵字(中文常用)
_HAGGLE_KEYWORDS = [
    "便宜", "議價", "再便宜", "可以議", "可議",
    "殺價", "砍價", "降價", "優惠", "折",
    "最低", "底價", "出 ", "出價", "成本",
    "可以.*?嗎", "便宜.*?嗎", "便宜.*?賣",
]
# 議價數字(NT$xxx / xxx 元 / xxx 塊 / xxx 賣)
_HAGGLE_PRICE_RE = re.compile(r"(\d{2,5})\s*(?:元|塊|nt|NT)?")

# 緩存:每個 buyer 24h cache(BOSH 拉 200 條訊息很貴)
_CACHE: Dict[str, Tuple[Dict, float]] = {}
_CACHE_TTL_SEC = 86400


def _classify_haggle_tendency(haggle_count: int, total_msgs: int) -> str:
    """根據議價次數判定傾向。"""
    if haggle_count == 0:
        return "無"
    if total_msgs == 0:
        return "未知"
    ratio = haggle_count / max(1, total_msgs)
    if ratio < 0.05:
        return "偶爾"
    if ratio < 0.15:
        return "常"
    return "總是"


def _detect_haggle_from_msgs(msgs: List[Dict], my_id_l: str) -> Dict[str, Any]:
    """掃 buyer 訊息找議價模式。

    Returns: { count, tendency, avg_discount_pct (or None) }
    """
    buyer_texts = []
    for m in msgs:
        sender = (m.get("senderID") or m.get("sender") or "").lower()
        if sender == my_id_l:
            continue
        if int(m.get("msgType", 0) or 0) != 1:
            continue
        content = m.get("msgContent", "") or ""
        # msgContent 可能是 JSON string,試 parse
        try:
            obj = json.loads(content)
            text = (obj.get("value") or {}).get("content", "") or ""
        except Exception:
            text = content
        if text:
            buyer_texts.append(text)

    haggle_count = 0
    haggle_prices = []
    for text in buyer_texts:
        matched = False
        for kw in _HAGGLE_KEYWORDS:
            if re.search(kw, text):
                matched = True
                break
        if matched:
            haggle_count += 1
            # 嘗試提取砍價金額
            for m in _HAGGLE_PRICE_RE.finditer(text):
                try:
                    price = int(m.group(1))
                    if 50 <= price <= 99999:  # 合理範圍
                        haggle_prices.append(price)
                except Exception:
                    continue

    tendency = _classify_haggle_tendency(haggle_count, len(buyer_texts))
    return {
        "count": haggle_count,
        "tendency": tendency,
        "sample_prices": haggle_prices[:3],
    }


def _calc_response_stats(msgs: List[Dict], my_id_l: str) -> Dict[str, Any]:
    """算對方的平均回應時間 + 活躍時段(對方訊息常發的小時)。"""
    # 對於每對「我發 → 對方回」算時間差
    response_deltas = []
    last_my_ts = 0
    for m in msgs:
        sender = (m.get("senderID") or m.get("sender") or "").lower()
        ts = int(m.get("sendTime", 0) or m.get("createdUts", 0) or 0)
        if not ts:
            continue
        if sender == my_id_l:
            last_my_ts = ts
        elif last_my_ts > 0:
            delta_min = (ts - last_my_ts) / 1000 / 60
            if 0 < delta_min < 1440:  # 1 天內才算
                response_deltas.append(delta_min)
            last_my_ts = 0  # reset

    avg_resp = sum(response_deltas) / len(response_deltas) if response_deltas else 0

    # 活躍時段(對方訊息的 hour 統計)
    hour_counts: Dict[int, int] = {}
    for m in msgs:
        sender = (m.get("senderID") or m.get("sender") or "").lower()
        if sender == my_id_l:
            continue
        ts = int(m.get("sendTime", 0) or 0)
        if not ts:
            continue
        hour = time.localtime(ts / 1000).tm_hour
        hour_counts[hour] = hour_counts.get(hour, 0) + 1

    active_hours = ""
    if hour_counts:
        # 找最多訊息的 1-3 個 hour
        sorted_h = sorted(hour_counts.items(), key=lambda x: -x[1])
        top_hours = sorted([h for h, _ in sorted_h[:3]])
        if len(top_hours) == 1:
            active_hours = f"{top_hours[0]:02d}:00"
        else:
            active_hours = f"{top_hours[0]:02d}:00-{top_hours[-1]:02d}:00"

    return {
        "avg_response_min": int(round(avg_resp)) if avg_resp else 0,
        "response_samples": len(response_deltas),
        "active_hours": active_hours,
    }


def _extract_buyer_viewed_items(msgs: List[Dict], my_id_l: str) -> List[Dict]:
    """從訊息歷史抽「對方發的 listing card」= 對方看過/詢問過的商品。

    Yahoo IM 機制:買家點商品頁的「向賣家發問」會自動 attach listing card。
    所以對方發的 listing = 確定看過 + 主動詢問過該商品。
    最新一條 ≈「對方剛在看 / 最近關注」。

    Returns: [{yahoo_id, title, price, ts}, ...] 按 ts desc
    """
    items = []
    seen_ids = set()
    for m in msgs:
        sender = (m.get("senderID") or m.get("sender") or "").lower()
        if sender == my_id_l:
            continue
        if int(m.get("msgType", 0) or 0) != 1:
            continue
        content = m.get("msgContent", "") or ""
        try:
            obj = json.loads(content)
            if obj.get("type") != "listing":
                continue
            value = obj.get("value") or {}
            yid = str(value.get("id", "") or "")
            if not yid or yid in seen_ids:
                continue
            seen_ids.add(yid)
            items.append({
                "yahoo_id": yid,
                "title": (value.get("title", "") or "")[:80],
                "price": value.get("price", ""),
                "ts": int(m.get("sendTime", 0) or 0),
            })
        except Exception:
            continue
    items.sort(key=lambda x: -x["ts"])
    return items


def _calc_unread_my_count(msgs: List[Dict], my_id_l: str, other_last_read_ts: int) -> int:
    """對方還沒讀我的訊息數 = 我發出後 ts > other_last_read_ts。"""
    if not other_last_read_ts:
        return 0
    count = 0
    for m in msgs:
        sender = (m.get("senderID") or m.get("sender") or "").lower()
        if sender != my_id_l:
            continue
        ts = int(m.get("sendTime", 0) or 0)
        if ts > other_last_read_ts and int(m.get("msgType", 0) or 0) == 1:
            count += 1
    return count


def analyze_buyer_conversation(
    profile_dir: Path,
    *,
    channel_id: str,
    my_id: str,
    on_log: Optional[LogFn] = None,
    use_cache: bool = True,
) -> Tuple[Optional[Dict], str]:
    """從 BOSH 拉 200 條訊息 + 統計分析。

    返回 dict 含:
    - haggle (議價統計)
    - response (回應時間 + 活躍時段)
    - unread_my_count (對方沒讀我訊息數)
    - first_interaction_ts (ms)
    - days_since_first
    - total_msgs / buyer_msgs / my_msgs
    """
    on_log = on_log or (lambda *_: None)
    cache_key = f"{profile_dir.name}|{channel_id}"
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached and (time.time() - cached[1]) < _CACHE_TTL_SEC:
            return cached[0], ""

    try:
        from .yahoo_im_bosh_ext import BOSHSession
        with BOSHSession(profile_dir, on_log=on_log) as s:
            # 1. queryMessage 拉 200 條
            resp, err = s.iq(
                "juiker:iq:queryMessage",
                {"chID": channel_id, "afterN": 200},
                iq_type="get",
            )
            msgs = []
            if not err and isinstance(resp, dict):
                msgs = resp.get("messages") or []
            # 方向 fallback
            if not msgs:
                parts = channel_id.split(":")
                if len(parts) == 3:
                    rev = f"{parts[0]}:{parts[2]}:{parts[1]}"
                    resp2, err2 = s.iq(
                        "juiker:iq:queryMessage",
                        {"chID": rev, "afterN": 200}, iq_type="get",
                    )
                    if not err2 and isinstance(resp2, dict):
                        msgs = resp2.get("messages") or []

            # 2. 對方 lastReadTime(算 unread_my_count 用)
            resp3, _ = s.iq(
                "juiker:iq:queryChannelReadInfo",
                {"chID": channel_id}, iq_type="get",
            )
            other_last_read = 0
            my_id_l = my_id.lower()
            if isinstance(resp3, dict):
                for u in (resp3.get("readInfo") or []):
                    if (u.get("userID") or "").lower() != my_id_l:
                        other_last_read = int(u.get("lastReadTime", 0) or 0)
                        break

    except Exception as e:
        return None, f"analyze 異常: {e}"

    if not msgs:
        return None, "無對話歷史"

    my_id_l = my_id.lower()
    buyer_msgs = [m for m in msgs if (m.get("senderID") or "").lower() != my_id_l]
    my_msgs = [m for m in msgs if (m.get("senderID") or "").lower() == my_id_l]

    # 首次互動 = 訊息最早的 ts
    sorted_msgs = sorted(msgs, key=lambda m: int(m.get("sendTime", 0) or 0))
    first_ts = int(sorted_msgs[0].get("sendTime", 0) or 0) if sorted_msgs else 0

    haggle = _detect_haggle_from_msgs(msgs, my_id_l)
    response = _calc_response_stats(msgs, my_id_l)
    unread_my = _calc_unread_my_count(msgs, my_id_l, other_last_read)
    viewed = _extract_buyer_viewed_items(msgs, my_id_l)

    result = {
        "haggle": haggle,
        "response": response,
        "unread_my_count": unread_my,
        "viewed_items": viewed,  # v6.1:對方詢問過的商品列表(按時間 desc)
        "first_interaction_ts": first_ts,
        "days_since_first": int((time.time() - first_ts / 1000) / 86400) if first_ts else 0,
        "total_msgs": len(msgs),
        "buyer_msgs": len(buyer_msgs),
        "my_msgs": len(my_msgs),
    }

    if use_cache:
        _CACHE[cache_key] = (result, time.time())

    return result, ""


def evaluate_buyer_quality_with_ai(
    rating: Optional[Dict],
    intel: Optional[Dict],
    buyer_label: str = "",
    on_log: Optional[LogFn] = None,
) -> Dict[str, Any]:
    """LLM 看評價 + 歷史互動 → 給「客戶優質度」評分 1-5 + 一句理由。

    Returns:
        {
            "score": 4,                   # 1-5 星
            "label": "良好",               # 差/一般/良好/優質/極優
            "reason": "正評率高,議價偶爾,回應快"
        }
    """
    on_log = on_log or (lambda *_: None)
    try:
        # 1. 規則打分(若 AI 失敗 fallback)
        score = 3  # 預設一般
        reasons = []

        if rating:
            pos = rating.get("positive", 0)
            neg = rating.get("negative", 0)
            ratio = rating.get("positive_ratio", 0)
            total = pos + neg + rating.get("neutral", 0)
            if total >= 100 and ratio >= 99:
                score += 2
                reasons.append(f"{total} 筆 99%+ 正評")
            elif total >= 20 and ratio >= 95:
                score += 1
                reasons.append(f"{total} 筆 95%+ 正評")
            elif total >= 20 and ratio < 90:
                score -= 2
                reasons.append(f"⚠️ {total} 筆但 {ratio:.0f}% 正評偏低")
            elif total < 5:
                score -= 1
                reasons.append("評價過少 (新買家)")

        if intel:
            haggle = intel.get("haggle") or {}
            if haggle.get("tendency") == "總是":
                score -= 1
                reasons.append("常砍價")
            elif haggle.get("tendency") == "無":
                score += 1
                reasons.append("不還價")

            avg_resp = intel.get("response", {}).get("avg_response_min", 0)
            if avg_resp and avg_resp < 30:
                reasons.append(f"回應快 (~{avg_resp} 分)")
            elif avg_resp and avg_resp > 360:
                reasons.append(f"回應慢 (~{avg_resp//60} 小時)")

            days = intel.get("days_since_first", 0)
            if days >= 365:
                reasons.append(f"老客戶 ({days // 30} 個月互動)")

        score = max(1, min(5, score))
        labels = {1: "差", 2: "一般", 3: "中等", 4: "良好", 5: "優質"}
        return {
            "score": score,
            "label": labels.get(score, "中等"),
            "reason": " / ".join(reasons[:3]) if reasons else "資料不足",
        }
    except Exception as e:
        on_log(f"[BUYER-INTEL] AI 評分異常: {e}")
        return {"score": 3, "label": "中等", "reason": "評分失敗"}


def format_intel_for_card(intel: Optional[Dict], quality: Optional[Dict]) -> str:
    """格式化深度情報成資訊卡 markdown 區塊。"""
    if not intel and not quality:
        return ""
    lines = []

    # 優質度評分(置頂)
    if quality:
        stars = "⭐" * quality.get("score", 3)
        lines.append(f"💎 客戶優質度:{stars} *{quality.get('label','')}*")
        if quality.get("reason"):
            lines.append(f"   _{quality['reason']}_")

    # 對方未讀
    if intel:
        u = intel.get("unread_my_count", 0)
        if u > 0:
            lines.append(f"📬 對方還有 *{u}* 條未讀我的訊息")

        # 議價
        haggle = intel.get("haggle") or {}
        h_tend = haggle.get("tendency", "")
        h_cnt = haggle.get("count", 0)
        if h_tend and h_tend != "無":
            sample = haggle.get("sample_prices") or []
            extra = f" (出價樣本:{','.join(map(str, sample))})" if sample else ""
            lines.append(f"💬 議價傾向:*{h_tend}* ({h_cnt} 次){extra}")

        # 回應時間
        resp = intel.get("response") or {}
        avg = resp.get("avg_response_min", 0)
        hours = resp.get("active_hours", "")
        if avg:
            line = f"⏱ 平均回應 *{avg}* 分鐘"
            if hours:
                line += f"  /  常活躍 {hours}"
            lines.append(line)

        # 首次互動
        first_ts = intel.get("first_interaction_ts", 0)
        days = intel.get("days_since_first", 0)
        if first_ts:
            from_date = time.strftime("%Y/%m/%d", time.localtime(first_ts / 1000))
            if days < 30:
                rel = f"{days} 天前"
            elif days < 365:
                rel = f"{days // 30} 個月前"
            else:
                rel = f"{days // 365} 年前"
            lines.append(f"📅 首次互動 {from_date} ({rel})")

        # v6.1:對方詢問過的商品(最新 = 他剛在看的)
        viewed = intel.get("viewed_items") or []
        if viewed:
            lines.append("")
            lines.append(f"🔍 對方詢問過 *{len(viewed)}* 件商品 (最新→最舊):")
            for i, it in enumerate(viewed[:5]):  # 顯示前 5 件
                yid = it.get("yahoo_id", "")
                title = (it.get("title", "") or "")[:50]
                price = it.get("price", "")
                ts = it.get("ts", 0)
                rel_time = ""
                if ts:
                    delta_min = int((time.time() - ts / 1000) / 60)
                    if delta_min < 60:
                        rel_time = f"{delta_min}分前" if delta_min > 0 else "剛剛"
                    elif delta_min < 1440:
                        rel_time = f"{delta_min // 60}小時前"
                    elif delta_min < 43200:
                        rel_time = f"{delta_min // 1440}天前"
                    else:
                        rel_time = f"{delta_min // 43200}月前"
                marker = "🆕" if i == 0 else "  "
                price_str = f" NT${price}" if price else ""
                lines.append(
                    f"{marker} {title}{price_str} ({rel_time})\n"
                    f"     [→ 商品頁](https://tw.bid.yahoo.com/item/{yid})"
                )

    return "\n".join(lines)
