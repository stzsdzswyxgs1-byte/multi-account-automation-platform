"""閒魚語音/視頻訊息 → 文字理解(v6.0.80 新增,v6.0.81 擴充 video)

閒魚 IM 語音(contentType=3)/視頻(contentType=4)server 不提供 AI 解析,
要客戶端下載 + 呼叫 AI 多模態 API 轉文字描述。

策略(v6.0.81 分流):
- **語音 → Gemini**(cli-proxy 包裝)
  * 預設 `gemini-2.5-pro`,遇 429 自動降 `gemini-2.5-flash`
  * 用 `image_url + data:audio/wav;base64,...` trick(cli-proxy 不認 input_audio)
- **視頻 → GPT vision**(OpenAI 規定 image_url 只接受 image MIME,GPT 不認 video)
  * 用 imageio 從視頻抽 6 個均勻關鍵幀 → 每幀 JPEG/base64 → 一次塞給 GPT vision
  * 預設 `gpt-5.5`,遇失敗降 `gpt-5.4-mini`

調用:
    text = voice_to_text(url, on_log=...)     # 語音轉文字 (Gemini)
    desc = video_to_text(url, on_log=...)     # 視頻轉場景描述 (GPT vision keyframes)
    失敗皆返回空字串
"""
from __future__ import annotations

import base64
import time
import threading
from typing import Callable, Optional, Dict, Tuple

import requests


# 結果 cache:url → (text, cached_at) — 語音、視頻分別 cache
_STT_CACHE: Dict[str, Tuple[str, float]] = {}
_VIDEO_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 7 * 24 * 3600  # 7 天

# Gemini 對短/靜音/雜訊回的「無內容」字串(全部視為失敗,讓上層 fallback)
_NOISE_REPLIES = {
    "silent", "silence", "no audio", "empty",
    "no speech", "inaudible", "unintelligible",
}


def _get_stt_config() -> Tuple[str, str, str, str]:
    """從 settings 取 STT (語音 → Gemini) 配置。

    Returns: (api_url_chat, api_key, primary_model, fallback_model)
        api_url_chat 為 /v1/chat/completions 端點(用 Gemini chat 跑 STT)

    優先級:
    1. settings.voice_stt_api_url / voice_stt_api_key / voice_stt_model(用戶自訂)
    2. 硬編碼 gennyou1 + gemini-2.5-pro(最強)+ flash(fallback,quota 寬鬆)
    """
    DEFAULT_URL = "https://<AI_PROXY_HOST>/v1/chat/completions"
    DEFAULT_KEY = "<AI_API_KEY_REDACTED>"
    DEFAULT_MODEL = "gemini-2.5-pro"           # 最強,但 quota 緊
    FALLBACK_MODEL = "gemini-2.5-flash"        # 兜底,實測穩定
    try:
        from core.accounts import load_settings
        st = load_settings() or {}
        api_url = str(st.get("voice_stt_api_url", "") or "").strip() or DEFAULT_URL
        api_key = str(st.get("voice_stt_api_key", "") or "").strip() or DEFAULT_KEY
        model = str(st.get("voice_stt_model", "") or "").strip() or DEFAULT_MODEL
        fallback = str(st.get("voice_stt_fallback_model", "") or "").strip() or FALLBACK_MODEL
        return api_url, api_key, model, fallback
    except Exception:
        return DEFAULT_URL, DEFAULT_KEY, DEFAULT_MODEL, FALLBACK_MODEL


def _get_video_config() -> Tuple[str, str, str, str]:
    """從 settings 取視頻理解 (GPT vision) 配置。

    Returns: (api_url_chat, api_key, primary_model, fallback_model)

    GPT 不接受 video MIME,要抽 keyframes 用 image_url 傳。
    優先級:
    1. settings.video_understand_*(用戶自訂)
    2. 硬編碼 gennyou1 + gpt-5.5(最強 vision)+ gpt-5.4-mini(便宜兜底)
    """
    DEFAULT_URL = "https://<AI_PROXY_HOST>/v1/chat/completions"
    DEFAULT_KEY = "<AI_API_KEY_REDACTED>"
    DEFAULT_MODEL = "gpt-5.5"                  # 主線最強
    FALLBACK_MODEL = "gpt-5.4-mini"            # 兜底,便宜快
    try:
        from core.accounts import load_settings
        st = load_settings() or {}
        api_url = str(st.get("video_understand_api_url", "") or "").strip() or DEFAULT_URL
        api_key = str(st.get("video_understand_api_key", "") or "").strip() or DEFAULT_KEY
        model = str(st.get("video_understand_model", "") or "").strip() or DEFAULT_MODEL
        fallback = str(st.get("video_understand_fallback_model", "") or "").strip() or FALLBACK_MODEL
        return api_url, api_key, model, fallback
    except Exception:
        return DEFAULT_URL, DEFAULT_KEY, DEFAULT_MODEL, FALLBACK_MODEL


def _download_media(url: str, timeout: int = 30) -> bytes:
    """下載媒體檔案(語音/視頻)。失敗返回空 bytes。"""
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return b""


def _guess_audio_mime(url: str, audio_bytes: bytes) -> str:
    """從 URL 副檔名或 magic bytes 推測音訊 MIME type。

    注意:m4a 容器要用 audio/mp4(不是 audio/aac),cli-proxy 看 mime 字段判斷。
    """
    lower = url.lower()
    for ext, mime in [
        (".mp3", "audio/mp3"),
        (".m4a", "audio/mp4"),   # m4a = MPEG-4 container,mime 為 audio/mp4
        (".mp4", "audio/mp4"),
        (".aac", "audio/aac"),
        (".wav", "audio/wav"),
        (".ogg", "audio/ogg"),
        (".flac", "audio/flac"),
        (".webm", "audio/webm"),
        (".amr", "audio/amr"),
    ]:
        if ext in lower:
            return mime
    # Magic bytes
    if audio_bytes.startswith(b"RIFF"):
        return "audio/wav"
    if audio_bytes.startswith(b"ID3") or audio_bytes[:2] == b"\xff\xfb":
        return "audio/mp3"
    if audio_bytes.startswith(b"OggS"):
        return "audio/ogg"
    if audio_bytes.startswith(b"#!AMR"):
        return "audio/amr"
    # ftyp box (MP4 family)
    if len(audio_bytes) >= 12 and audio_bytes[4:8] == b"ftyp":
        return "audio/mp4"
    # 兜底:當 mp3(Gemini 接受度高)
    return "audio/mp3"


def _guess_video_mime(url: str, video_bytes: bytes) -> str:
    """從 URL 副檔名或 magic bytes 推測視頻 MIME type。"""
    lower = url.lower()
    for ext, mime in [
        (".mp4", "video/mp4"),
        (".mov", "video/quicktime"),
        (".webm", "video/webm"),
        (".mkv", "video/x-matroska"),
        (".avi", "video/x-msvideo"),
        (".m4v", "video/mp4"),
        (".3gp", "video/3gpp"),
    ]:
        if ext in lower:
            return mime
    # ftyp box (MP4 family) — 最常見
    if len(video_bytes) >= 12 and video_bytes[4:8] == b"ftyp":
        return "video/mp4"
    # webm
    if video_bytes.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    # 兜底
    return "video/mp4"


def _is_capacity_exhausted(status: int, body_text: str) -> bool:
    """判斷是否為 429/RESOURCE_EXHAUSTED 類錯誤(該降級 fallback model)。"""
    if status == 429:
        return True
    low = (body_text or "").lower()
    return ("resource_exhausted" in low
            or "model_capacity_exhausted" in low
            or "no capacity available" in low
            or "rate_limit" in low)


def _call_gemini_multimodal(
    *,
    api_url: str,
    api_key: str,
    model: str,
    mime: str,
    media_b64: str,
    prompt: str,
    log: Callable[[str], None],
    timeout: int = 45,
) -> Tuple[str, str]:
    """呼叫 Gemini chat/completions 多模態。

    v6.0.82:timeout 120→45,失敗快不浪費 worker。
    Gemini 不穩定,寧可誤判失敗也不卡 AI 整合主流程。

    Returns: (text, error_kind)
        text: 成功時的文字內容(空字串 = 失敗)
        error_kind: "" | "CAPACITY_EXHAUSTED" | "HTTP_ERROR" | "PARSE_ERROR" | "EMPTY"
    """
    data_uri = f"data:{mime};base64,{media_b64}"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "stream": False,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        log(f"[GEMINI] {model} 請求異常: {str(e)[:200]}")
        return "", "HTTP_ERROR"

    # 503/504 → 短退避 retry 一次(同 model)— v6.0.82:30s→10s
    if r.status_code in (503, 504):
        log(f"[GEMINI] {model} HTTP {r.status_code},10s 後重試...")
        time.sleep(10)
        try:
            r = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            log(f"[GEMINI] {model} retry 異常: {str(e)[:200]}")
            return "", "HTTP_ERROR"

    # 429 / capacity → 通知上層切 fallback
    if _is_capacity_exhausted(r.status_code, r.text):
        log(f"[GEMINI] {model} 額度耗盡 (HTTP {r.status_code})")
        return "", "CAPACITY_EXHAUSTED"

    if r.status_code != 200:
        log(f"[GEMINI] {model} HTTP {r.status_code}: {r.text[:300]}")
        return "", "HTTP_ERROR"

    try:
        d = r.json()
    except Exception:
        log(f"[GEMINI] {model} 非 JSON: {r.text[:200]}")
        return "", "PARSE_ERROR"

    # 200 但 body 含 error 字段(上游處理失敗 / 中間件分類)
    if isinstance(d, dict) and d.get("error"):
        err_obj = d.get("error") or {}
        err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
        err_code = err_obj.get("code") if isinstance(err_obj, dict) else ""
        # 中間件分類為 rate_limit / 內含 RESOURCE_EXHAUSTED → 降級
        if _is_capacity_exhausted(0, str(err_msg) + " " + str(err_code)):
            log(f"[GEMINI] {model} body 級額度耗盡: {str(err_msg)[:200]}")
            return "", "CAPACITY_EXHAUSTED"
        log(f"[GEMINI] {model} body error: {str(err_msg)[:300]}")
        return "", "HTTP_ERROR"

    text = (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    if not text:
        log(f"[GEMINI] {model} 回空文字")
        return "", "EMPTY"

    return text, ""


def _run_with_fallback(
    *,
    media_bytes: bytes,
    mime: str,
    prompt: str,
    log: Callable[[str], None],
    deadline: Optional[float] = None,
) -> str:
    """主 model 嘗試 → 失敗時降級 fallback model。

    v6.0.82:
    - 加 deadline 控制(若提供):剩餘時間不足 5s 直接放棄,不浪費呼叫
    - fallback 觸發條件擴充:CAPACITY_EXHAUSTED / HTTP_ERROR / EMPTY 都試 flash
      (Gemini 不穩定,pro 503 時 flash 偶爾還能跑;1 次嘗試,timeout 短不會拖很久)
    """
    api_url, api_key, primary, fallback = _get_stt_config()
    if not api_url or not api_key:
        log("[GEMINI] 配置缺失(api_url/api_key)")
        return ""

    # base64 size 檢查
    media_b64 = base64.b64encode(media_bytes).decode("ascii")
    b64_mb = len(media_b64) / (1024 * 1024)
    if b64_mb > 15:
        log(f"[GEMINI] ⚠️ base64 size={b64_mb:.1f}MB 超過 15MB 建議上限,可能 504")

    def _remaining() -> float:
        if deadline is None:
            return 1e9  # 無限制
        return max(0.0, deadline - time.time())

    # 1. 主 model
    rem = _remaining()
    if rem < 5:
        log(f"[GEMINI] budget 已耗盡(剩 {rem:.1f}s),放棄主 model")
        return ""
    primary_timeout = int(min(45, rem))
    log(f"[GEMINI] 嘗試主 model={primary} ({b64_mb:.1f}MB, timeout={primary_timeout}s)...")
    text, err = _call_gemini_multimodal(
        api_url=api_url, api_key=api_key, model=primary,
        mime=mime, media_b64=media_b64, prompt=prompt, log=log,
        timeout=primary_timeout,
    )
    if text:
        log(f"[GEMINI] ✓ 主 model {primary} 成功")
        return text

    # 2. fallback — Gemini 不穩定,擴大 fallback 觸發條件
    if (err in ("CAPACITY_EXHAUSTED", "HTTP_ERROR", "EMPTY")
            and fallback and fallback != primary):
        rem = _remaining()
        if rem < 5:
            log(f"[GEMINI] budget 已耗盡(剩 {rem:.1f}s),不切 fallback")
            return ""
        fb_timeout = int(min(30, rem))
        log(f"[GEMINI] 主 model err={err},降級 fallback={fallback} (timeout={fb_timeout}s)...")
        text, err = _call_gemini_multimodal(
            api_url=api_url, api_key=api_key, model=fallback,
            mime=mime, media_b64=media_b64, prompt=prompt, log=log,
            timeout=fb_timeout,
        )
        if text:
            log(f"[GEMINI] ✓ fallback {fallback} 成功")
            return text

    return ""


def _post_process_text(text: str, log: Callable[[str], None]) -> str:
    """文字後處理:清理 markdown/引號 + 過濾 noise reply。

    Returns: 處理後文字(空字串 = 失敗/無內容)
    """
    if not text:
        return ""

    # 清理 markdown / 引號 / 前後綴
    text = text.strip().strip('"').strip("'").strip("「」")
    text = text.replace("「", "").replace("」", "")

    # 走錯 model 徵兆
    if any(kw in text for kw in ("請上傳音訊", "请上传音频", "请上传语音", "no audio file")):
        log(f"[GEMINI] 疑似走錯 model:{text[:80]!r}")
        return ""

    # 過濾 noise reply
    lower = text.lower()
    if any(noise in lower for noise in _NOISE_REPLIES) or text == "[聽不清]":
        log(f"[GEMINI] 判定為無內容:{text!r}")
        return ""

    return text


def voice_to_text(
    url: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    use_cache: bool = True,
    prompt: str = "請逐字轉寫這段音訊,只輸出轉寫文字,不要加任何說明或標題。如果是中英混合請保留原語言。完全聽不到內容請回「[聽不清]」。",
    budget_sec: float = 60.0,
) -> str:
    """把語音 URL 轉文字(用 Gemini 多模態,pro 優先 + flash fallback)。

    v6.0.82:加整體 budget(默認 60s)— Gemini 不穩定,寧可誤判失敗也不卡 AI 整合主流程。

    Args:
        url: 閒魚語音檔案 URL
        on_log: log 回調
        use_cache: 是否查 cache(同 URL 不重複轉)
        prompt: 轉寫提示(默認要求逐字稿,聽不清回 [聽不清])
        budget_sec: 整個函數最多執行多久(秒);超過直接返回空字串

    Returns:
        轉文字結果。失敗回空字串。
    """
    log = on_log or (lambda *_: None)

    if not url or not url.startswith(("http://", "https://")):
        return ""

    start_ts = time.time()
    deadline = start_ts + budget_sec

    # 1. cache(極快,不計入 budget)
    if use_cache:
        with _CACHE_LOCK:
            hit = _STT_CACHE.get(url)
            if hit and start_ts - hit[1] < _CACHE_TTL:
                log(f"[STT] cache 命中:{hit[0][:40]!r}")
                return hit[0]

    # 2. 下載(預算內,timeout 取 min(15, 剩餘時間))
    dl_remain = max(0.0, deadline - time.time())
    if dl_remain < 3:
        log(f"[STT] budget 已耗盡(剩 {dl_remain:.1f}s),放棄下載")
        return ""
    log(f"[STT] 下載語音: {url[:80]} (timeout={int(min(15, dl_remain))}s)")
    audio_bytes = _download_media(url, timeout=int(min(15, dl_remain)))
    if not audio_bytes:
        log("[STT] 下載失敗")
        return ""
    log(f"[STT] 下載 {len(audio_bytes)} bytes (累計 {time.time()-start_ts:.1f}s)")

    # 3. 推 mime + 呼叫(主 model + fallback,帶 deadline)
    mime = _guess_audio_mime(url, audio_bytes)
    raw_text = _run_with_fallback(
        media_bytes=audio_bytes, mime=mime, prompt=prompt, log=log,
        deadline=deadline,
    )
    text = _post_process_text(raw_text, log)
    if not text:
        log(f"[STT] 失敗(累計 {time.time()-start_ts:.1f}s)")
        return ""

    # 4. cache
    if use_cache:
        with _CACHE_LOCK:
            _STT_CACHE[url] = (text, time.time())
            if len(_STT_CACHE) > 100:
                now = time.time()
                to_drop = [k for k, (_, ts) in _STT_CACHE.items() if now - ts > _CACHE_TTL]
                for k in to_drop:
                    _STT_CACHE.pop(k, None)

    log(f"[STT] ✓ 轉文字成功({len(text)} 字, 耗時 {time.time()-start_ts:.1f}s):{text[:60]!r}")
    return text


def _extract_video_keyframes(
    video_bytes: bytes,
    n: int = 6,
    *,
    max_dim: int = 512,
    jpeg_quality: int = 80,
    log: Callable[[str], None] = lambda *_: None,
) -> list:
    """從視頻 bytes 抽 N 個均勻分佈的關鍵幀,每幀返回 JPEG bytes(已縮放)。

    Returns: list[bytes] — 失敗回空 list

    Args:
        n: 要抽的幀數(默認 6,平衡精度和 token 消耗)
        max_dim: 每幀縮放到此最長邊像素(節省 token)
        jpeg_quality: JPEG 壓縮品質(80 平衡大小和清晰度)
    """
    try:
        import imageio.v3 as iio
        from PIL import Image
        import io as _io

        # imageio.imiter 一次讀完所有幀(視頻很短時 OK,大檔可能吃 RAM)
        all_frames = list(iio.imiter(_io.BytesIO(video_bytes), plugin="pyav"))
        total = len(all_frames)
        if total == 0:
            log("[VIDEO] 視頻 0 幀,放棄")
            return []
        log(f"[VIDEO] 共 {total} 幀,抽 {min(n, total)} 個關鍵幀")

        # 均勻抽 n 幀(包含首尾)
        if total <= n:
            idxs = list(range(total))
        else:
            idxs = [int(i * (total - 1) / (n - 1)) for i in range(n)]

        jpeg_list = []
        for idx in idxs:
            frame = all_frames[idx]
            img = Image.fromarray(frame.astype("uint8") if hasattr(frame, "astype") else frame)
            # 等比縮放最長邊到 max_dim
            w, h = img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            # RGB 模式 + JPEG
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=jpeg_quality)
            jpeg_list.append(buf.getvalue())

        return jpeg_list
    except Exception as e:
        log(f"[VIDEO] 抽幀異常: {e}")
        return []


def _call_gpt_vision_frames(
    *,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    frames_jpeg: list,
    log: Callable[[str], None],
    timeout: int = 45,
) -> Tuple[str, str]:
    """用 GPT vision 看一組 keyframes,回傳描述。

    v6.0.82:timeout 120→45,sleep 30→10,失敗快不卡 AI 整合。

    Returns: (text, error_kind)
        error_kind: "" | "CAPACITY_EXHAUSTED" | "HTTP_ERROR" | "PARSE_ERROR" | "EMPTY"
    """
    if not frames_jpeg:
        return "", "EMPTY"

    contents = [{"type": "text", "text": prompt}]
    for jpeg in frames_jpeg:
        b64 = base64.b64encode(jpeg).decode("ascii")
        contents.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": contents}],
        "stream": False,
        "max_tokens": 1000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        log(f"[GPT-V] {model} 請求異常: {str(e)[:200]}")
        return "", "HTTP_ERROR"

    if r.status_code in (503, 504):
        log(f"[GPT-V] {model} HTTP {r.status_code},10s 後重試...")
        time.sleep(10)
        try:
            r = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            log(f"[GPT-V] {model} retry 異常: {str(e)[:200]}")
            return "", "HTTP_ERROR"

    if _is_capacity_exhausted(r.status_code, r.text):
        log(f"[GPT-V] {model} 額度耗盡 (HTTP {r.status_code})")
        return "", "CAPACITY_EXHAUSTED"

    if r.status_code != 200:
        log(f"[GPT-V] {model} HTTP {r.status_code}: {r.text[:300]}")
        return "", "HTTP_ERROR"

    try:
        d = r.json()
    except Exception:
        log(f"[GPT-V] {model} 非 JSON: {r.text[:200]}")
        return "", "PARSE_ERROR"

    if isinstance(d, dict) and d.get("error"):
        err = d.get("error") or {}
        em = err.get("message") if isinstance(err, dict) else str(err)
        if _is_capacity_exhausted(0, str(em)):
            log(f"[GPT-V] {model} body 級額度耗盡")
            return "", "CAPACITY_EXHAUSTED"
        log(f"[GPT-V] {model} body error: {str(em)[:300]}")
        return "", "HTTP_ERROR"

    text = (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    if not text:
        log(f"[GPT-V] {model} 回空文字")
        return "", "EMPTY"

    return text, ""


def video_to_text(
    url: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    use_cache: bool = True,
    prompt: str = "這是按時序抽出的視頻關鍵幀(由前到後)。請用繁體中文 1-3 句描述視頻內容(物品/外觀/動作/瑕疵等),只輸出描述文字不要加說明或標題。商品展示請特別注意外觀細節。",
    n_frames: int = 6,
    budget_sec: float = 90.0,
) -> str:
    """把視頻 URL 轉場景描述(用 GPT vision 看 keyframes)。

    v6.0.82:加整體 budget(默認 90s,視頻比語音多 30s 因為要抽幀)。
    流程:下載 → imageio 抽 6 個均勻幀 → JPEG 壓縮 → GPT vision 一次性看
    OpenAI 不接受 video MIME,所以走 keyframe 方式。

    Args:
        url: 閒魚視頻檔案 URL
        on_log: log 回調
        use_cache: 是否查 cache
        prompt: 描述提示
        n_frames: 抽幀數量(默認 6)
        budget_sec: 整個函數最多執行多久(秒);超過直接返回空字串

    Returns:
        視頻內容描述。失敗回空字串。
    """
    log = on_log or (lambda *_: None)

    if not url or not url.startswith(("http://", "https://")):
        return ""

    start_ts = time.time()
    deadline = start_ts + budget_sec

    # 1. cache
    if use_cache:
        with _CACHE_LOCK:
            hit = _VIDEO_CACHE.get(url)
            if hit and start_ts - hit[1] < _CACHE_TTL:
                log(f"[VIDEO] cache 命中:{hit[0][:40]!r}")
                return hit[0]

    # 2. 下載(預算內,timeout 取 min(45, 剩餘時間))— 視頻 30MB 可能 30s 下完
    dl_remain = max(0.0, deadline - time.time())
    if dl_remain < 5:
        log(f"[VIDEO] budget 已耗盡(剩 {dl_remain:.1f}s),放棄")
        return ""
    log(f"[VIDEO] 下載視頻: {url[:80]} (timeout={int(min(45, dl_remain))}s)")
    video_bytes = _download_media(url, timeout=int(min(45, dl_remain)))
    if not video_bytes:
        log("[VIDEO] 下載失敗")
        return ""

    size_mb = len(video_bytes) / (1024 * 1024)
    log(f"[VIDEO] 下載 {size_mb:.1f}MB (累計 {time.time()-start_ts:.1f}s)")

    # 視頻太大 → 不抽幀
    if size_mb > 50:
        log(f"[VIDEO] ⚠️ 視頻過大({size_mb:.1f}MB > 50MB),放棄 AI 解析")
        return ""

    # 3. 抽 keyframes(本機操作,快)
    frames_jpeg = _extract_video_keyframes(video_bytes, n=n_frames, log=log)
    if not frames_jpeg:
        log("[VIDEO] 抽幀失敗,放棄")
        return ""

    # 4. GPT vision(主 model + fallback,帶 deadline)
    api_url, api_key, primary, fallback = _get_video_config()
    if not api_url or not api_key:
        log("[VIDEO] GPT 配置缺失")
        return ""

    def _remaining() -> float:
        return max(0.0, deadline - time.time())

    rem = _remaining()
    if rem < 5:
        log(f"[VIDEO] budget 已耗盡(剩 {rem:.1f}s),放棄主 model")
        return ""

    primary_timeout = int(min(45, rem))
    log(f"[VIDEO] GPT vision: {len(frames_jpeg)} 幀 → 主 model={primary} (timeout={primary_timeout}s)")
    raw_text, err = _call_gpt_vision_frames(
        api_url=api_url, api_key=api_key, model=primary,
        prompt=prompt, frames_jpeg=frames_jpeg, log=log,
        timeout=primary_timeout,
    )

    # fallback 條件擴充:CAPACITY_EXHAUSTED / HTTP_ERROR / EMPTY 都試
    if ((not raw_text) and err in ("CAPACITY_EXHAUSTED", "HTTP_ERROR", "EMPTY")
            and fallback and fallback != primary):
        rem = _remaining()
        if rem >= 5:
            fb_timeout = int(min(30, rem))
            log(f"[VIDEO] 主 model err={err},降級 fallback={fallback} (timeout={fb_timeout}s)...")
            raw_text, err = _call_gpt_vision_frames(
                api_url=api_url, api_key=api_key, model=fallback,
                prompt=prompt, frames_jpeg=frames_jpeg, log=log,
                timeout=fb_timeout,
            )
        else:
            log(f"[VIDEO] budget 已耗盡(剩 {rem:.1f}s),不切 fallback")

    text = _post_process_text(raw_text, log)
    if not text:
        log(f"[VIDEO] 失敗(累計 {time.time()-start_ts:.1f}s)")
        return ""

    # 5. cache
    if use_cache:
        with _CACHE_LOCK:
            _VIDEO_CACHE[url] = (text, time.time())
            if len(_VIDEO_CACHE) > 50:
                now = time.time()
                to_drop = [k for k, (_, ts) in _VIDEO_CACHE.items() if now - ts > _CACHE_TTL]
                for k in to_drop:
                    _VIDEO_CACHE.pop(k, None)

    log(f"[VIDEO] ✓ 描述成功({len(text)} 字, 耗時 {time.time()-start_ts:.1f}s):{text[:80]!r}")
    return text
