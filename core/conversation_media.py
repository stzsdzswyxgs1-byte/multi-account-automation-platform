"""v6.1.55:對話媒體權重 + 視頻首幀提取 module。

設計:
  - 賣家圖/視頻 weight = 1.0(權威資料,實物實拍,不衰減)
  - 買家圖/視頻 weight = exp(-ln2 · age / half_life),half_life=10min
  - 視頻 → 抓首幀 base64 給 AI 看
  - 動態 cap:按 weight 降序排,過濾 < min_weight,cap 總數

使用:
  from core.conversation_media import (
      build_media_for_ai, extract_video_first_frame_b64, compute_weight,
  )

  # 主要 helper(給 4 個 AI 入口呼叫)
  prompt_section, image_urls_for_ai = build_media_for_ai(
      conversation_media,    # List[Dict]
      product_image_urls,    # List[str](Yahoo + 閒魚商品圖)
      now_ms=None,           # 預設用當前時間
      max_count=10,          # AI 看的最多媒體數
      on_log=None,
  )
"""

from __future__ import annotations

import math
import time
import base64
import hashlib
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 常數 ───────────────────────────────────────────
BUYER_HALF_LIFE_SEC = 600.0       # 買家圖半衰期 10 分鐘
SELLER_BASE_WEIGHT = 1.0          # 賣家圖恆 1.0(權威)
MIN_AI_WEIGHT = 0.05              # weight < 0.05 不給 AI(對話超過 ~45 分鐘前的買家圖)
DEFAULT_MAX_MEDIA_FOR_AI = 10     # 總媒體 cap(避免 token 暴增)
VIDEO_DOWNLOAD_TIMEOUT = 20       # 視頻下載超時
VIDEO_FRAME_CACHE_TTL = 86400     # 視頻首幀 cache 24h
VIDEO_FRAME_CACHE_MAX = 50        # 最多 cache 50 個視頻

# ── 視頻首幀 cache ──────────────────────────────────
# key = sha256(video_url)[:16] → (base64_data_url, cached_at)
_video_frame_cache: Dict[str, Tuple[str, float]] = {}
_video_frame_cache_lock = threading.Lock()


def _safe_log(on_log: Optional[Callable[[str], None]], msg: str) -> None:
    if on_log:
        try:
            on_log(msg)
        except Exception:
            pass


def compute_weight(role: str, ts_ms: int, latest_ts_ms: int,
                   half_life_sec: float = BUYER_HALF_LIFE_SEC) -> float:
    """算單個媒體的權重。

    - 賣家:恆 1.0(實物實拍,不衰減)
    - 買家:exp(-ln2 · age / half_life)
        age=0    → 1.0
        age=10m  → 0.5
        age=30m  → 0.125
        age=1h   → 0.016
    """
    if (role or "").lower() == "seller":
        return SELLER_BASE_WEIGHT

    if not ts_ms or not latest_ts_ms:
        return 0.5  # 沒時間戳資訊,中間值

    age_sec = max(0.0, (latest_ts_ms - ts_ms) / 1000.0)
    return math.exp(-math.log(2) * age_sec / half_life_sec)


def extract_video_first_frame_b64(
    video_url: str,
    on_log: Optional[Callable[[str], None]] = None,
    timeout: int = VIDEO_DOWNLOAD_TIMEOUT,
) -> Optional[str]:
    """下載視頻 → ffmpeg 抓 0 秒首幀 → 回傳 'data:image/jpeg;base64,xxx'。

    cache 24h(同 URL 不重複處理)。失敗回 None。
    """
    if not video_url or not video_url.startswith("http"):
        return None

    cache_key = hashlib.sha256(video_url.encode("utf-8")).hexdigest()[:16]
    now = time.time()

    # cache hit
    with _video_frame_cache_lock:
        if cache_key in _video_frame_cache:
            data_url, cached_at = _video_frame_cache[cache_key]
            if now - cached_at < VIDEO_FRAME_CACHE_TTL:
                return data_url
            # expired
            _video_frame_cache.pop(cache_key, None)

    # 1. 下載視頻到臨時檔
    try:
        import requests
        r = requests.get(video_url, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code != 200:
            _safe_log(on_log, f"[MEDIA] 下載視頻失敗 {r.status_code}: {video_url[:80]}")
            return None
        # 限制大小避免 OOM(>30MB 視頻通常太大)
        max_bytes = 30 * 1024 * 1024
        video_bytes = b""
        for chunk in r.iter_content(chunk_size=64 * 1024):
            video_bytes += chunk
            if len(video_bytes) > max_bytes:
                _safe_log(on_log, f"[MEDIA] 視頻 >30MB,僅取前部分: {video_url[:80]}")
                break
    except Exception as e:
        _safe_log(on_log, f"[MEDIA] 下載視頻異常: {e}")
        return None

    if len(video_bytes) < 1024:
        _safe_log(on_log, f"[MEDIA] 視頻 bytes 過小 {len(video_bytes)}: 跳過")
        return None

    # 2. 寫到臨時檔(ffmpeg 需要 file path)
    tmp_video = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(video_bytes)
            tmp_video = tf.name
    except Exception as e:
        _safe_log(on_log, f"[MEDIA] 寫臨時視頻檔異常: {e}")
        return None

    # 3. ffmpeg 抓首幀到 stdout (-ss 0 -frames:v 1 -f image2 -)
    try:
        import imageio_ffmpeg
        import subprocess
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg,
            "-y",                     # overwrite
            "-i", tmp_video,
            "-ss", "0",               # 從 0 秒
            "-frames:v", "1",         # 只抓 1 幀
            "-q:v", "5",              # JPEG quality(2=best, 31=worst)
            "-f", "image2pipe",       # 輸出 pipe
            "-vcodec", "mjpeg",
            "-",                      # stdout
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            _safe_log(on_log, f"[MEDIA] ffmpeg 抓幀失敗 rc={result.returncode}: {result.stderr[:200]}")
            return None
        jpeg_bytes = result.stdout
        if len(jpeg_bytes) < 100:
            _safe_log(on_log, f"[MEDIA] ffmpeg 輸出 JPEG 過小 {len(jpeg_bytes)}")
            return None
    except subprocess.TimeoutExpired:
        _safe_log(on_log, "[MEDIA] ffmpeg 抓幀 timeout 15s")
        return None
    except Exception as e:
        _safe_log(on_log, f"[MEDIA] ffmpeg 抓幀異常: {e}")
        return None
    finally:
        # 清理臨時視頻檔
        if tmp_video:
            try:
                Path(tmp_video).unlink(missing_ok=True)
            except Exception:
                pass

    # 4. base64 + cache
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"

    with _video_frame_cache_lock:
        # cache 滿了 → 移除最舊
        if len(_video_frame_cache) >= VIDEO_FRAME_CACHE_MAX:
            oldest_key = min(_video_frame_cache.items(), key=lambda kv: kv[1][1])[0]
            _video_frame_cache.pop(oldest_key, None)
        _video_frame_cache[cache_key] = (data_url, now)

    _safe_log(on_log, f"[MEDIA] 視頻首幀提取成功 {len(jpeg_bytes)}B → b64 {len(b64)}: {video_url[:60]}")
    return data_url


def build_media_for_ai(
    conversation_media: List[Dict[str, Any]],
    product_image_urls: Optional[List[str]] = None,
    now_ms: Optional[int] = None,
    max_count: int = DEFAULT_MAX_MEDIA_FOR_AI,
    half_life_sec: float = BUYER_HALF_LIFE_SEC,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, List[str]]:
    """主 helper — 構造給 AI 看的 media 列表 + prompt 描述。

    輸入:
      conversation_media: List[Dict] — 每個 dict: {url, ts, role, msg_idx, kind}
        kind: "image" 或 "video"
        role: "buyer" 或 "seller"
        ts: createdUts (毫秒)
        msg_idx: 對話中第幾條(從 1)
      product_image_urls: List[str] — 商品圖(Yahoo+閒魚),固定中等權重
      max_count: 給 AI 的最多媒體數
      half_life_sec: 買家半衰期

    輸出:
      (prompt_section_text, ordered_url_list_for_ai)
      prompt_section_text:用來 append 到 user_prompt 末尾
      ordered_url_list_for_ai:給 call_openai(image_urls=...) 的 URL list,
                              順序跟 prompt 內描述的編號一致

    處理:
      1. 為每個 conversation_media 算 weight(賣家恆 1,買家衰減)
      2. 過濾 weight < MIN_AI_WEIGHT
      3. 視頻 → 抓首幀 base64(失敗則跳過)
      4. 按 weight 降序排
      5. cap max_count
      6. 商品圖固定 weight=0.3(供參考但不主導)
      7. 構造 prompt 描述 + URL list
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # 1. 算對話媒體 weight + 視頻首幀
    convo_items = []
    if conversation_media:
        latest_ts = max((m.get("ts", 0) or 0) for m in conversation_media) or now_ms
        for m in conversation_media:
            url = (m.get("url") or "").strip()
            if not url:
                continue
            role = (m.get("role") or "buyer").lower()
            kind = (m.get("kind") or "image").lower()
            ts = int(m.get("ts") or 0)
            msg_idx = int(m.get("msg_idx") or 0)
            w = compute_weight(role, ts, latest_ts, half_life_sec)
            if w < MIN_AI_WEIGHT:
                continue

            # 視頻 → 抓首幀
            ai_url = url
            if kind == "video":
                frame_url = extract_video_first_frame_b64(url, on_log=on_log)
                if not frame_url:
                    # 抓不到首幀 → 跳過(寧可不給 AI 看,別把錯誤 URL 餵 LLM)
                    _safe_log(on_log, f"[MEDIA] 視頻 {url[:60]} 抓不到首幀,跳過")
                    continue
                ai_url = frame_url

            convo_items.append({
                "ai_url": ai_url,
                "orig_url": url,
                "role": role,
                "kind": kind,
                "ts": ts,
                "msg_idx": msg_idx,
                "weight": w,
            })

    # 2. 按 weight 降序排 + cap
    convo_items.sort(key=lambda x: x["weight"], reverse=True)
    convo_items = convo_items[:max_count]

    # 3. 商品圖(固定中等權重供參考)
    product_imgs = list(product_image_urls or [])
    product_cap = max(0, 12 - len(convo_items))  # 留 token 空間給對話媒體
    product_imgs = product_imgs[:product_cap]

    # 4. 構造 prompt 描述
    lines = []
    urls_for_ai: List[str] = []

    if convo_items:
        lines.append(f"【對話媒體】共 {len(convo_items)} 條(按權重降序,前面更重要)")
        for i, item in enumerate(convo_items, 1):
            role_zh = "賣家" if item["role"] == "seller" else "買家"
            kind_zh = "視頻首幀" if item["kind"] == "video" else "圖片"
            weight_str = f"w={item['weight']:.2f}"
            extra = ""
            if item["role"] == "seller":
                extra = " ⭐(權威·實物實拍)"
            elif item["weight"] >= 0.7:
                extra = " ⚡(剛發·最新)"
            elif item["weight"] < 0.2:
                extra = " ░(較早·參考用)"
            lines.append(f"  [{i}] {role_zh}·{kind_zh} {weight_str}{extra}")
            urls_for_ai.append(item["ai_url"])
        lines.append(
            "\n⚠️ 權重規則:賣家圖 = 1.0(實物實拍,最權威)/ 買家圖按時間衰減(剛發的最有意義)。\n"
            "判斷買家意圖優先看高權重的買家圖(尤其『這個』『圈起來那個』等指代詞 → 對應最新一張)。"
        )

    if product_imgs:
        if convo_items:
            lines.append(f"\n【商品圖】額外附 {len(product_imgs)} 張(Yahoo 主圖→primary 閒魚圖→其他),供尺寸/材質/狀況參考。")
        else:
            lines.append(f"【商品圖】附 {len(product_imgs)} 張,請仔細觀察圖內可能含:")
            lines.append("- 尺子量出的尺寸數字、材質、磨損狀況、包裝、證書、文字標籤")
        for u in product_imgs:
            urls_for_ai.append(u)

    prompt_text = "\n".join(lines) if lines else ""
    return prompt_text, urls_for_ai


def add_media_to_conversation(
    conv_media_list: List[Dict[str, Any]],
    url: str,
    role: str,
    ts_ms: int,
    msg_idx: int,
    kind: str = "image",
    max_keep: int = 15,
) -> None:
    """在 conv_media_list 末尾加一個新媒體,自動去重 + cap max_keep。

    in-place 修改 conv_media_list。
    """
    if not url or not url.startswith("http"):
        return

    # 去重(同 URL 不重複加)
    for existing in conv_media_list:
        if existing.get("url") == url:
            return

    conv_media_list.append({
        "url": url,
        "role": (role or "buyer").lower(),
        "ts": int(ts_ms or 0),
        "msg_idx": int(msg_idx or 0),
        "kind": (kind or "image").lower(),
    })

    # cap(保留最新 max_keep 個)
    if len(conv_media_list) > max_keep:
        del conv_media_list[0:len(conv_media_list) - max_keep]
