"""顺云宝 HTTP API 模块 — 替代 Playwright 网页自动化。

认证方式：
  Playwright 登录后从浏览器提取 stoken (JWT)，保存到本地缓存文件。
  所有 HTTP API 调用携带 stoken header。
  JWT 有效期 2 天，过期后需要重新 Playwright 登录。

覆盖功能：
  1. 出货资料导入 (doImport t=3) — 替代 Excel 上传网页
  2. 查询码导入 (doImport t=1) — 替代 Excel 更新
  3. 快递单号导入 (doImport t=5) — 替代 Excel 更新详情
  4. 库存列表查询 (stock/list) — 替代 Playwright 搜索
  5. 快速建单 (offlineCreateOld) — 替代 Playwright 按钮点击
  6. 编辑订单 (stock/update) — 替代 Playwright 弹窗编辑
  7. 获取详情 (stock/detail) — 替代 Playwright 读取

面单上传: HTTP /am/stock/pageImport (multipart, file+code+pwd)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_log_mod = logging.getLogger(__name__)

SYB_BASE_URL = "https://www.shunyunbaoerp.com"
SYB_USERNAME = "<SYB_USER_REDACTED>"
SYB_PASSWORD = "<SYB_PASSWORD_REDACTED>"

# stoken 缓存文件
_TOKEN_CACHE = Path(__file__).resolve().parent.parent / "profiles" / "_syb_web_session" / "stoken_cache.json"
# JWT 有效期 24 小时，缓存保留 23 小时（留 1 小时余量）
_TOKEN_MAX_AGE = 23 * 3600  # 23 小时

LogFn = Callable[[str], None]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(f"[SYB-HTTP] {msg}")
    else:
        _log_mod.info(f"[SYB-HTTP] {msg}")


# ── stoken 缓存管理 ─────────────────────────────────────

def save_stoken(token: str) -> bool:
    """保存 stoken 到本地缓存。"""
    try:
        _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "stoken": token,
            "saved_at": time.time(),
            "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _TOKEN_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_stoken(max_age: float = _TOKEN_MAX_AGE) -> str:
    """从缓存加载 stoken。返回空字符串表示无效/过期。"""
    if not _TOKEN_CACHE.exists():
        return ""
    try:
        data = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at", 0)
        if time.time() - saved_at > max_age:
            return ""
        return data.get("stoken", "")
    except Exception:
        return ""


def extract_stoken_from_browser(page) -> str:
    """从 Playwright page 提取 stoken。

    SYB 的 JWT token 可能在:
    1. cookie 名为 stoken
    2. localStorage 键为 stoken
    3. sessionStorage 键为 stoken
    """
    # 1. 尝试从 cookies 提取
    try:
        ctx = page.context
        cookies = ctx.cookies(SYB_BASE_URL)
        for c in cookies:
            if c.get("name") == "stoken":
                val = c.get("value", "")
                if val:
                    return val
    except Exception:
        pass

    # 2. 尝试从 localStorage 提取
    try:
        val = page.evaluate("() => localStorage.getItem('stoken') || ''")
        if val:
            return val
    except Exception:
        pass

    # 3. 尝试从 sessionStorage 提取
    try:
        val = page.evaluate("() => sessionStorage.getItem('stoken') || ''")
        if val:
            return val
    except Exception:
        pass

    # 4. 尝试所有 cookies 和 storage 中包含 jwt/token 的
    try:
        cookies = ctx.cookies(SYB_BASE_URL)
        for c in cookies:
            val = c.get("value", "")
            # JWT 通常是 xxx.yyy.zzz 格式
            if val and val.count(".") == 2 and len(val) > 50:
                return val
    except Exception:
        pass

    return ""


# ── HTTP 请求基础 ────────────────────────────────────────

def _build_headers(stoken: str) -> Dict[str, str]:
    """构建 API 请求头。"""
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "stoken": stoken,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{SYB_BASE_URL}/sys/admin/stock",
    }


def _post(stoken: str, url: str, payload: Any,
          log: Optional[LogFn] = None, timeout: int = 30) -> Dict:
    """发送 POST 请求到 SYB API。

    返回 JSON dict。失败抛出异常。
    """
    import requests

    headers = _build_headers(stoken)
    full_url = url if url.startswith("http") else f"{SYB_BASE_URL}{url}"

    _log(log, f"POST {url}")

    resp = requests.post(
        full_url,
        json=payload,
        headers=headers,
        cookies={"stoken": stoken},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("status", True):
        msg = data.get("msg", "")
        code = data.get("code")
        if code in (401, 403) or "登录" in msg or "认证" in msg or "权限" in msg:
            raise SYBAuthError(f"SYB 认证失败: {msg}")
        raise SYBAPIError(f"SYB API 失败: {msg}")

    return data


def _get(stoken: str, url: str,
         log: Optional[LogFn] = None, timeout: int = 30) -> Dict:
    """发送 GET 请求到 SYB API。"""
    import requests

    headers = _build_headers(stoken)
    full_url = url if url.startswith("http") else f"{SYB_BASE_URL}{url}"

    resp = requests.get(
        full_url,
        headers=headers,
        cookies={"stoken": stoken},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


class SYBAuthError(Exception):
    """SYB 认证失败/token 过期。"""
    pass


class SYBAPIError(Exception):
    """SYB API 业务错误。"""
    pass


# ── 取消訂單 API + 訂單編碼後綴 helpers ──────────────────
# v6.0.68:支援「作廢 → SYB 取消」+「上傳碰已存在 → 加數字後綴重試」

def _post_raw_text(stoken: str, path: str, raw_body: str,
                   log: Optional[LogFn] = None, timeout: int = 30) -> Dict:
    """SYB 的 column/cancel 等 API 用 text/plain raw body(不是 JSON)。

    body 直接是字串本身(例如 stock_id),不會被 json.dumps 包起來。
    """
    import requests

    headers = _build_headers(stoken)
    headers["Content-Type"] = "text/plain"
    full_url = path if path.startswith("http") else f"{SYB_BASE_URL}{path}"

    _log(log, f"POST(text) {path}")

    resp = requests.post(
        full_url,
        data=raw_body,
        headers=headers,
        cookies={"stoken": stoken},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("status", True):
        msg = data.get("msg", "")
        code = data.get("code")
        if code in (401, 403) or "登录" in msg or "认证" in msg or "权限" in msg:
            raise SYBAuthError(f"SYB 认证失败: {msg}")
        raise SYBAPIError(f"SYB API 失败: {msg}")

    return data


def cancel_stock(stoken: str, stock_id: int,
                 log: Optional[LogFn] = None) -> bool:
    """取消 SYB 訂單(等同 UI:勾訂單 → 取消恢復 → 取消)。

    取消後 status=已取消,物流不再處理該紀錄。
    PK 仍佔著 — 想再用同 code 必須加數字後綴(見 make_dup_code)。

    Endpoint:POST /am/column/cancel?value=2  body=stock_id (raw text)
    Response:{"status":true,"msg":"更新成功","data":true,"code":null}
    """
    _log(log, f"取消訂單: id={stock_id}")
    data = _post_raw_text(stoken, "/am/column/cancel?value=2", str(stock_id), log)
    return data.get("status") is True


# Yahoo 拍賣訂單編碼固定 14 位數;超出代表是避撞後綴版本
YAHOO_CODE_LEN = 14


def strip_dup_suffix(code: str) -> str:
    """從可能帶數字後綴的 code 還原成純 Yahoo 號。

    我們只會加 1 位數後綴(make_dup_code 限制 n ∈ 1..9),所以只在「長度正好 = 14+1」時砍尾 1 字。
    14 位以下不動(純 Yahoo 號,可能尾數本來就是 1)。
    16 位以上也不動(異常,留給人工處理,不冒險自動砍)。

    用於業績核對等下游流程,確保用純 Yahoo 號搜 Yahoo 後台。

      10121897540714  → 10121897540714 (14位純號 結尾 4,不動)
      10121897540711  → 10121897540711 (14位純號 結尾 1,不動 ★ 不會誤砍)
      101218975407141 → 10121897540714 (15位 = 14位+1位後綴,砍 1)
      101218975407112 → 10121897540711 (15位 = 結尾1的 base + 後綴2,砍 1)
      1012189754071142→ 1012189754071142 (16位 異常,不動)
    """
    if not code:
        return code
    s = str(code).strip()
    # 只在剛好多 1 位數時砍尾(我們的後綴永遠是 1 位數 1..9)
    if len(s) == YAHOO_CODE_LEN + 1:
        return s[:-1]
    return s


def make_dup_code(base_code: str, suffix_n: int) -> str:
    """造避撞後綴 code(SYB 上傳「已存在」時用)。

      make_dup_code('10121897540714', 0) → '10121897540714'  (不加)
      make_dup_code('10121897540714', 1) → '101218975407141'
      make_dup_code('10121897540714', 2) → '101218975407142'
    """
    if not base_code:
        return base_code
    base = str(base_code).strip()
    if suffix_n <= 0:
        return base
    return f"{base}{suffix_n}"


def find_active_syb_code(stoken: str, base_code: str,
                         max_n: int = 9,
                         log: Optional[LogFn] = None) -> Optional[str]:
    """找到 base_code(純 Yahoo 號)對應在 SYB 上 active 的實際 code。

    返回值:
      - 若 SYB 上有 base_code 的 active 紀錄 → 返回 base_code 本身
      - 若沒有但有後綴 1/2/.../max_n 的 active 紀錄 → 返回那個 code(優先小的)
      - 都找不到 → None

    用於面單 PDF 上傳:PDF 檔名通常是純 Yahoo 號,但 SYB 上實際 code 可能帶後綴,
    要先解析才能上傳對的 code。

    實作:1 個 query 一次查所有 candidate(IN clause),只 1 次 API call。
    """
    base = str(base_code or "").strip()
    if not base:
        return None
    candidates = [base] + [make_dup_code(base, n) for n in range(1, max_n + 1)]
    try:
        items = query_orders_by_code(stoken, candidates, log=log)
    except Exception:
        return None
    found_codes = {str(it.get("code", "")) for it in items if it.get("code")}
    # 優先 base,然後依序 +1, +2, ...
    for c in candidates:
        if c in found_codes:
            return c
    return None


# ── AI 验证码识别 + 全自动登录 ────────────────────────

# v6.1.52:雙線路 + 三模型 fallback 架構
# 主線路:gennyou1(我們自己,gpt-5.4-mini,準確率高但偶爾不穩)
# 備線路:aigcbest(gpt-4o / claude-3-5-sonnet,穩定但稍慢)
_AI_PRIMARY_BASE = "https://<AI_PROXY_HOST>/v1"
_AI_PRIMARY_KEY = "<AI_API_KEY_REDACTED>"
_AI_PRIMARY_MODEL = "gpt-5.4-mini"

# 備線:既有 aigcbest 線路(用戶原始備援),用同樣的 gpt-5.4-mini
# 用同 model 保證行為一致,差異只在 endpoint(主線不通才走備線)
_AI_BACKUP_BASE = "https://api2.aigcbest.top/v1"
_AI_BACKUP_KEY = "<AI_API_KEY_REDACTED>"
_AI_BACKUP_MODEL = "gpt-5.4-mini"


def _preprocess_for_ocr(image_bytes: bytes, log: Optional[LogFn] = None) -> bytes:
    """v6.1.52:預處理圖片讓它不像 captcha,降低 vision 模型 refusal 機率。

    步驟:
    1. 轉灰階(去顏色干擾)
    2. 放大 3x(小圖容易被 vision 認成 captcha)
    3. 中值濾波(去干擾線/雜點)
    4. 對比增強(讓主要字符更鮮明)
    5. 二值化(只保留字符,純黑白看起來像普通 OCR 場景)
    """
    try:
        from PIL import Image, ImageFilter, ImageEnhance, ImageOps
        import io as _io
        img = Image.open(_io.BytesIO(image_bytes))

        # 1. 轉灰階
        img = img.convert("L")

        # 2. 放大 3x (LANCZOS 高質量上採樣)
        new_size = (img.size[0] * 3, img.size[1] * 3)
        img = img.resize(new_size, Image.LANCZOS)

        # 3. 中值濾波 — 去干擾線/雜點(對線狀干擾特別有效)
        img = img.filter(ImageFilter.MedianFilter(size=3))

        # 4. 對比增強
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)

        # 5. 自動對比 + invert? — 暫不二值化,保留灰度(避免過度處理丟字)
        img = ImageOps.autocontrast(img, cutoff=2)

        # 輸出 PNG
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        if log:
            log(f"[SYB-HTTP] 預處理圖片:{len(image_bytes)} bytes → {len(out)} bytes(灰階+3x+去噪+對比)")
        return out
    except Exception as e:
        # PIL 不在或處理失敗 → fallback 原圖
        if log:
            log(f"[SYB-HTTP] 預處理失敗(用原圖):{e}")
        return image_bytes


# v6.1.52:ddddocr 全局 singleton(初始化要 1-2 秒,只跑一次)
_DDDD_OCR_INSTANCE = None
_DDDD_OCR_INIT_FAILED = False


def _get_ddddocr():
    """lazy init ddddocr,返回 instance 或 None(套件沒裝/初始化失敗)。"""
    global _DDDD_OCR_INSTANCE, _DDDD_OCR_INIT_FAILED
    if _DDDD_OCR_INSTANCE is not None:
        return _DDDD_OCR_INSTANCE
    if _DDDD_OCR_INIT_FAILED:
        return None
    try:
        import ddddocr
        _DDDD_OCR_INSTANCE = ddddocr.DdddOcr(show_ad=False)
        return _DDDD_OCR_INSTANCE
    except Exception:
        _DDDD_OCR_INIT_FAILED = True
        return None


def _ai_recognize_captcha(image_bytes: bytes, log: Optional[LogFn] = None) -> str:
    """识别验证码图片,返回 4 位字母数字。

    v6.1.52 四階 fallback(從免費/快/可靠 到 慢/付費/有 refusal 風險):
      Stage 0:ddddocr 本地(離線、~25ms、95%+ 準確率,專為 captcha 設計)
      Stage 1:gennyou1 主線路 gpt-5.4-mini
      Stage 2:aigcbest 備線路 gpt-5.4-mini
    """
    # ── Stage 0:ddddocr 本地 OCR(優先) ──
    # 95%+ 準確率,~25ms,免費,離線,無 refusal 風險
    ocr = _get_ddddocr()
    if ocr is not None:
        try:
            raw = ocr.classification(image_bytes)
            # 過濾非 ASCII alnum 字符
            code = "".join(c for c in (raw or "") if c.isascii() and c.isalnum())[:4]
            if len(code) == 4:
                _log(log, f"AI 驗證碼 [ddddocr 本地] (離線) → {code!r}")
                return code
            # 長度不對 → fallback 到 AI
            _log(log, f"AI 驗證碼 [ddddocr 本地] 長度不對({len(code)}≠4):{raw!r} → fallback AI")
        except Exception as e:
            _log(log, f"AI 驗證碼 [ddddocr 本地] 失敗(fallback AI):{e}")

    # ── Stage 1+2:AI 線路 fallback ──
    import requests as _req

    # v6.1.52:圖片預處理 — 讓 vision 模型不要把它當 captcha
    # 模型 vision 端會「看圖」識別 captcha 形狀 → 觸發 refusal
    # 預處理:放大 3x + 去干擾線(中值濾波)+ 加強對比 + 轉灰階
    # 處理後看起來像「普通文字圖」而不是「captcha」
    processed_bytes = _preprocess_for_ocr(image_bytes, log=log)
    b64 = base64.b64encode(processed_bytes).decode()
    # 極簡 prompt — 引導模型直接回答案不要解釋(觸發 Step 2 純 4 字回應)
    _prompt_text = "Read this image. Reply with only the text, nothing else."

    def _build_body(model_name: str) -> dict:
        return {
            "model": model_name,
            "max_tokens": 20,  # 留點餘量防尾巴解釋,後續 .isalnum() 過濾
            "temperature": 0,  # 識別任務 deterministic
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
        }

    # v6.1.52 修:.isalnum() 對中文也回 True,改 ASCII 嚴格白名單
    _ASCII_ALNUM = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    # 模型拒絕辨識的回應特徵詞(萬一新 prompt 還是觸發拒絕,fallback log 友善訊息)
    _REFUSAL_KEYWORDS = (
        "抱歉", "不能", "无法", "無法", "拒絕", "拒绝",
        "sorry", "cannot", "can't", "unable", "refuse", "decline",
        "i'm not", "i am not", "not able",
    )

    # 常見 4 字英文 stop word,**絕對不可能**是真實 captcha 答案 — 全部過濾掉
    # (模型拒絕話 / 解釋話常包含這些詞,長度 4 又是純 ASCII,容易誤抓)
    _STOPWORD_4 = {
        "them", "they", "this", "that", "with", "from", "into", "your", "have",
        "been", "what", "when", "where", "will", "want", "like", "help", "sorry",
        "text", "code", "char", "look", "show", "send", "data", "info", "user",
        "back", "skip", "next", "stop", "fail", "okay", "wait", "test",
        "read", "find", "give", "tell", "type", "char", "good", "best",
        "post", "page", "site", "form", "task", "rule",
    }

    def _try(line_label: str, base: str, key: str, model: str, timeout: int) -> Optional[str]:
        """跑一次識別。

        v6.1.52 嚴格策略 — **只接受明確標識的答案**,避免從敘述句抽 stop word:
        1. 拒絕關鍵字 → fallback
        2. 純 4 字回應(模型直接給答案) → 信任
        3. markdown bold `**XXXX**` 或 `*XXXX*` 或 `` `XXXX` `` 包裝 → 信任
        4. markdown bold 內含分隔符(`**X, Y, Z, 9**`)normalize → 信任
        5. 都沒有 → fallback(不嘗試從段落抽 plain run,因為英文 4 字單詞太多)
        """
        import re as _re
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        resp = _req.post(f"{base}/chat/completions",
                          headers=headers, json=_build_body(model), timeout=timeout)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()

        # ── Step 1:拒絕檢查 ──
        text_lower = text.lower()
        is_refusal = any(kw.lower() in text_lower for kw in _REFUSAL_KEYWORDS)
        if is_refusal:
            _log(log, f"AI 驗證碼 [{line_label} / {model}] 模型拒絕辨識:{text[:80]!r} → fallback")
            return None

        # ── Step 2:純 4 字回應 — 模型只回答案沒解釋(最可靠的形式)──
        # 處理 markdown / quote / punctuation 包裝
        stripped = text.strip().strip('*').strip('`').strip('"').strip("'").strip('.').strip(',').strip(':').strip()
        if len(stripped) == 4 and all(c in _ASCII_ALNUM for c in stripped):
            _log(log, f"AI 驗證碼 [{line_label} / {model}] (timeout={timeout}s) 純 4 字:{text!r} → {stripped!r}")
            return stripped

        # ── Step 3:markdown bold(**XXXX**)──
        bold_match = _re.findall(r'\*\*([A-Za-z0-9]{4})\*\*', text)
        if bold_match:
            code = bold_match[-1]
            _log(log, f"AI 驗證碼 [{line_label} / {model}] (timeout={timeout}s) markdown bold:{text[:80]!r} → {code!r}")
            return code

        # ── Step 4:** 內含分隔符的字符,normalize 後 4 字 ──
        # 例:**8, V, K, 7** 或 **8 V K 7** → 8VK7
        bold_groups = _re.findall(r'\*\*([^*]+?)\*\*', text)
        for grp in bold_groups:
            normalized = "".join(c for c in grp if c in _ASCII_ALNUM)
            if len(normalized) == 4:
                _log(log, f"AI 驗證碼 [{line_label} / {model}] (timeout={timeout}s) markdown bold normalize:{grp!r} → {normalized!r}")
                return normalized

        # ── Step 5:單 * / 反引號 包裝 ──
        marked_match = _re.findall(r'[*`]([A-Za-z0-9]{4})[*`]', text)
        if marked_match:
            code = marked_match[-1]
            _log(log, f"AI 驗證碼 [{line_label} / {model}] (timeout={timeout}s) marked:{text[:80]!r} → {code!r}")
            return code

        # 沒抓到明確答案 — 不從敘述句抽 plain 4-char run(會誤抓 make/show/help 等英文單詞)
        _log(log, f"AI 驗證碼 [{line_label} / {model}] 找不到明確標識的答案(無 ** 也非純 4 字):{text[:80]!r} → fallback")
        return None

    last_err = None

    # Stage 1:主線路 gennyou1 gpt-5.4-mini,2 次嘗試
    for attempt, timeout in enumerate([15, 30], 1):
        try:
            code = _try("主線", _AI_PRIMARY_BASE, _AI_PRIMARY_KEY,
                        _AI_PRIMARY_MODEL, timeout)
            if code:
                return code
        except Exception as e:
            last_err = e
            _log(log, f"AI 驗證碼 [主線 / {_AI_PRIMARY_MODEL}] 第{attempt}次失敗:{e}")
        if attempt < 2:
            time.sleep(0.5)

    # Stage 2:備線路 aigcbest gpt-5.4-mini(主線 2 次都不行才走)
    # 同樣 model,差異只在 endpoint(主線 gennyou1 不通就改走 aigcbest)
    for attempt, timeout in enumerate([15, 30], 1):
        _log(log, f"AI 驗證碼 主線失敗 → 切備線路 {_AI_BACKUP_MODEL} (第{attempt}次)")
        try:
            code = _try("備線", _AI_BACKUP_BASE, _AI_BACKUP_KEY,
                        _AI_BACKUP_MODEL, timeout)
            if code:
                return code
        except Exception as e:
            last_err = e
            _log(log, f"AI 驗證碼 [備線 / {_AI_BACKUP_MODEL}] 第{attempt}次失敗:{e}")
        if attempt < 2:
            time.sleep(0.5)

    raise RuntimeError(f"AI 驗證碼所有線路/模型都失敗:{last_err}")


def auto_login(max_attempts: int = 5, log: Optional[LogFn] = None) -> str:
    """全自动 SYB 登录：获取验证码 → AI 识别 → HTTP 登录 → 保存 token。

    Returns:
        有效的 stoken，失败抛出异常。
    """
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            # 1. 获取验证码
            img_bytes, session_token = fetch_captcha_image(log=log)
            if not session_token:
                last_err = "获取验证码失败：无 session_token"
                continue

            # 2. AI 识别
            code = _ai_recognize_captcha(img_bytes, log=log)
            if len(code) != 4:
                _log(log, f"AI 识别结果长度异常({len(code)})，跳过")
                last_err = f"识别结果异常: {code}"
                continue

            # 3. HTTP 登录
            ok, token, err = http_login(session_token, code, log=log)
            if ok:
                save_stoken(token)
                _log(log, f"SYB 自动登录成功 (第{attempt}次)")
                return token
            else:
                last_err = err or "登录失败"
                _log(log, f"SYB 登录第{attempt}次失败: {last_err}")

        except Exception as e:
            last_err = str(e)
            _log(log, f"SYB 自动登录第{attempt}次异常: {last_err}")

    raise SYBAuthError(f"SYB 自动登录失败({max_attempts}次): {last_err}")


def ensure_stoken(log: Optional[LogFn] = None) -> str:
    """确保有有效的 stoken。已缓存则直接返回，过期则自动登录。"""
    token = load_stoken()
    if token:
        return token
    _log(log, "SYB stoken 过期，自动登录...")
    return auto_login(log=log)

def fetch_captcha_image(log: Optional[LogFn] = None) -> Tuple[bytes, str]:
    """HTTP 获取验证码图片。

    Returns:
        (image_bytes, session_token) — image_bytes 是 JPEG，
        session_token 是 GET 请求后 cookie 里返回的 stoken（会话标识）。
    Raises:
        Exception on failure.
    """
    import requests

    r = requests.get(
        f"{SYB_BASE_URL}/api/p/code1",
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    r.raise_for_status()

    ct = r.headers.get("content-type", "")
    if not ct.startswith("image"):
        raise RuntimeError(f"captcha response not image: {ct}, len={len(r.content)}")

    # 服务器在 GET /api/p/code1 时通过 Set-Cookie 返回 stoken（会话 ID）
    session_token = ""
    for c in r.cookies:
        if c.name == "stoken":
            session_token = c.value
            break

    _log(log, f"captcha fetched: {len(r.content)} bytes, session_token={'yes' if session_token else 'no'}")
    return r.content, session_token


def http_login(session_token: str, code: str,
               log: Optional[LogFn] = None) -> Tuple[bool, str, str]:
    """HTTP POST 登录。

    Args:
        session_token: fetch_captcha_image() 返回的 stoken cookie 值
        code: 用户输入的验证码

    Returns:
        (success, stoken, error_msg)
        - success=True 时 stoken 是可用的认证 token（cookie session ID）
        - success=False 时 error_msg 包含错误信息
    """
    import requests

    payload = {
        "username": SYB_USERNAME,
        "password": SYB_PASSWORD,
        "code": code,
    }

    r = requests.post(
        f"{SYB_BASE_URL}/am/auth/login",
        json=payload,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        cookies={"stoken": session_token},
        timeout=15,
    )
    r.raise_for_status()

    data = r.json()
    _log(log, f"login response: status={data.get('status')}, code={data.get('code')}")

    if data.get("status"):
        # 登录成功：
        # 1. 服务端把 session_token (cookie stoken) 关联到用户 → 这个 session ID 就能认证了
        # 2. response body 的 data.token 是 JWT（前端存 localStorage 用），API 认证走 cookie
        # 3. response 可能返回新的 stoken cookie，也可能沿用原来的
        final_token = session_token  # 默认沿用发送登录请求时的 session ID
        for c in r.cookies:
            if c.name == "stoken":
                final_token = c.value  # 服务器返回了新的 session ID
                break
        return True, final_token, ""
    else:
        msg = data.get("msg", "登录失败")
        return False, "", msg


def verify_stoken(stoken: str, log: Optional[LogFn] = None) -> bool:
    """验证 stoken 是否仍然有效（向 SYB API 发一个轻量请求）。"""
    import requests

    try:
        r = requests.get(
            f"{SYB_BASE_URL}/am/user/setting/list",
            headers=_build_headers(stoken),
            cookies={"stoken": stoken},
            timeout=10,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        # status=false + code=-2 表示未登录
        if not data.get("status", True):
            code = str(data.get("code", ""))
            msg = data.get("msg", "")
            if code in ("-2", "401", "403") or "未登录" in msg or "登录" in msg:
                return False
        return True
    except Exception as e:
        _log(log, f"verify_stoken failed: {e}")
        return False


# ── 1. 出货资料导入 (COD Excel) ─────────────────────────

@dataclass
class ImportRow:
    """对应 doImport t=3 的一行数据。"""
    code: str              # 订单编号 (必填)
    product_name: str      # 货物品名
    product_spec: str = "" # 货物规格
    product_price: int = 0 # 价格 (整数)
    product_qty: int = 1   # 数量
    exp_company: str = ""  # 物流公司: 7-11/全家/莱尔富/OK/黑貓/...
    receiver: str = ""     # 收件人 (必填)
    receiver_tel: str = "" # 收件电话 (必填)
    receiver_addr: str = ""     # 收件地址 (宅配必填)
    receiver_shop_code: str = "" # 收件门市编 (超商必填)
    receiver_shop_name: str = "" # 收件门市名
    shop_name: str = ""    # 店铺名 (必填)
    remark2: str = ""      # 货单备注
    remark1: str = ""      # 报单备注
    exp_code: str = ""     # 查询码
    inner_exp_code: str = ""  # 快递单号（国内快递单号）


_IMPORT_COLS = [
    "code", "productName", "productSpec", "productPrice", "productQty",
    "expCompany", "receiver", "receiverTel", "receiverAddr",
    "receiverShopCode", "receiverShopName", "shopName",
    "remark2", "remark1", "expCode", "innerExpCode",
]


# 顺云宝 API 接受的渠道名 — 经实测(2026-05-04 探针 doImport):
#   server 实际接受「萊爾富」(全繁)、「黑貓」(繁),不接受 API 文档里写的「莱尔富」(简)。
#   API 文档过时,server 改了校验。这里 normalize 必须用 server 当前真实接受的形式。
# 我们其它地方(Yahoo / 闲鱼 / 列印)本来就用繁体「萊爾富」,所以萊爾富根本不需要 normalize。
# 历史原因 _SYB_CHANNEL_MAP 把「萊爾富」误归一化为「莱尔富」导致所有萊爾富订单上传失败,
# 已累积大量「殭屍业绩」(已写 D1 但物流没成功)。
_SYB_CHANNEL_MAP = {
    # 萊爾富 → 保持繁体(server 只认这个)
    "莱尔富": "萊爾富",   # 旧代码可能写成简体,反向归一化
    "莱爾富": "萊爾富",
    "萊爾福": "萊爾富",   # 错别字纠正
    "莱尔福": "萊爾富",
    "莱爾福": "萊爾富",
    # 黑貓 → 繁体
    "黑猫": "黑貓",
    "黑猫宅急便": "黑貓宅急便",
    "黑猫宅配": "黑貓宅急便",
    # 7-11、全家、OK 繁简同形，无需映射
}


def _normalize_channel_for_syb(name: str) -> str:
    """归一化渠道名到顺云宝 API 期望的形式（修莱尔富繁简差异导致的上传失败）"""
    s = (name or "").strip()
    return _SYB_CHANNEL_MAP.get(s, s)


def _row_to_list(r: ImportRow) -> list:
    """ImportRow → 与 _IMPORT_COLS 对应的值列表。"""
    return [
        r.code,
        r.product_name,
        r.product_spec or None,
        r.product_price or None,
        r.product_qty or 1,
        _normalize_channel_for_syb(r.exp_company) or None,
        r.receiver,
        r.receiver_tel,
        r.receiver_addr or None,
        r.receiver_shop_code or None,
        r.receiver_shop_name or None,
        r.shop_name,
        r.remark2 or None,
        r.remark1 or None,
        r.exp_code or None,
        r.inner_exp_code or None,
    ]


def import_shipment_data(
    stoken: str,
    rows: List[ImportRow],
    origin: int = 2,
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """导入出货资料 (新增)。

    对应 doImport?t=3 — "新增出货资料"。
    origin: 1=店配(贴面单出货), 2=线下(宅配)
    返回每行的结果列表: [{"code": "xxx", "codeType": "货单号", "msg": "创建成功"}, ...]
    """
    if not rows:
        return []

    payload = {
        "cols": _IMPORT_COLS,
        "datas": [_row_to_list(r) for r in rows],
    }

    _log(log, f"导入出货资料: {len(rows)} 条 (origin={origin})")
    data = _post(stoken, f"/am/stock/import/doImport?t=3&store=&origin={origin}", payload, log)
    results = data.get("data", [])

    ok_cnt = sum(1 for r in results if "成功" in str(r.get("msg", "")))
    _log(log, f"导入结果: {ok_cnt}/{len(results)} 成功")

    return results


# ── 2. 查询码导入 ────────────────────────────────────────

def import_query_codes(
    stoken: str,
    code_pairs: List[Tuple[str, str]],
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """导入查询码 (更新订单资料)。

    code_pairs: [(order_no, query_code), ...]
    对应 doImport?t=1。
    """
    if not code_pairs:
        return []

    payload = {
        "cols": ["code", "expCode"],
        "datas": [[code, exp_code] for code, exp_code in code_pairs],
    }

    _log(log, f"导入查询码: {len(code_pairs)} 条")
    data = _post(stoken, "/am/stock/import/doImport?t=1&store=&origin=", payload, log)
    return data.get("data", [])


# ── 3. 快递单号导入 ──────────────────────────────────────

def import_express_codes(
    stoken: str,
    rows: List[Dict],
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """导入快递单号 (更新详情信息)。

    rows: [{"code": "xxx", "productSpec": "A15", "innerExpCode": "YT123", "purchaseCode": "TB456", "purchasePlatform": "淘宝"}, ...]
    对应 doImport?t=5。
    """
    if not rows:
        return []

    cols = ["code", "productSpec", "innerExpCode", "purchaseCode", "purchasePlatform"]
    datas = [[r.get(c) for c in cols] for r in rows]

    payload = {"cols": cols, "datas": datas}
    _log(log, f"导入快递单号: {len(rows)} 条")
    data = _post(stoken, "/am/stock/import/doImport?t=5", payload, log)
    return data.get("data", [])


# ── 4. 库存列表查询 ─────────────────────────────────────

# 默认查询字段
_DEFAULT_COLUMNS = [
    {"tableName": "t_stock", "colName": "created", "fieldName": "created", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "receiver", "fieldName": "receiver", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "product_name", "fieldName": "productName", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "code", "fieldName": "code", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "exp_code", "fieldName": "expCode", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "page_id", "fieldName": "pageId", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "amt_order", "fieldName": "amtOrder", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "exp_cod", "fieldName": "expCod", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "purchase_status", "fieldName": "purchaseStatus", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "exp_out_type", "fieldName": "expOutType", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "status", "fieldName": "status", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "exp_company", "fieldName": "expCompany", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "shop_name", "fieldName": "shopName", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "detail_qty", "fieldName": "detailQty", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "order_qty", "fieldName": "orderQty", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "remark1", "fieldName": "remark1", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "remark2", "fieldName": "remark2", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "remark9", "fieldName": "remark9", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "receiver_tel", "fieldName": "receiverTel", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "receiver_addr", "fieldName": "receiverAddr", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "receiver_shop_name", "fieldName": "receiverShopName", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "t_stock", "colName": "receiver_shop_code", "fieldName": "receiverShopCode", "hasAlias": 0, "tableAlias": "t"},
    {"tableName": "sys_user", "colName": "fullname", "fieldName": "sufullname", "hasAlias": 1, "tableAlias": "t3"},
]

# status 值映射
STATUS_MAP = {
    0: "待入仓", 5: "待认领", 10: "已入仓", 13: "待备货",
    15: "备货中", 20: "待打包", 23: "已打单", 25: "打包中",
    30: "待出仓", 35: "已打包", 40: "待揽收", 45: "已揽收",
    50: "已发货", 55: "转运中", 60: "待验中", 65: "已清关",
    70: "已提货", 75: "转配送", 80: "截货中", 85: "已拆包",
    90: "待转寄", 95: "转寄中", 100: "已寄出", 105: "待寄回",
    110: "下架中", 115: "寄回中", 120: "已寄回", 125: "待弃件",
    130: "弃件中", 135: "已弃件",
}


def query_stock_list(
    stoken: str,
    *,
    status_values: str = "13,15,20",
    order_codes: Optional[List[str]] = None,
    start: int = 0,
    length: int = 100,
    log: Optional[LogFn] = None,
) -> Tuple[int, List[Dict]]:
    """查询库存列表。

    Args:
        status_values: 逗号分隔的状态码 (默认 "13,15,20" = 待备货+备货中+待打包)
        order_codes: 按订单号过滤 (可选)
        start: 分页起始
        length: 分页长度

    Returns:
        (total, list_of_items)
    """
    queries = []
    if status_values:
        queries.append({
            "dvalue": status_values,
            "tableName": "t_stock",
            "colName": "status",
            "op": 6,
            "type": 2,
            "tableAlias": "t",
        })
    if order_codes:
        queries.append({
            "dvalue": ",".join(order_codes),
            "tableName": "t_stock",
            "colName": "code",
            "op": 6,
            "type": 2,
            "tableAlias": "t",
        })

    payload = {
        "history": 0,
        "length": length,
        "start": start,
        "columns": _DEFAULT_COLUMNS,
        "queries": queries,
    }

    data = _post(stoken, "/am/stock/list", payload, log)
    result = data.get("data", {})
    total = result.get("total", 0)
    items = result.get("list", [])
    return total, items


def query_orders_by_code(
    stoken: str,
    order_codes: List[str],
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """按订单号查询（不限状态）。

    返回匹配的订单列表。
    """
    if not order_codes:
        return []

    # 查所有状态: 0~135
    all_statuses = ",".join(str(v) for v in sorted(STATUS_MAP.keys()))
    _, items = query_stock_list(
        stoken,
        status_values=all_statuses,
        order_codes=order_codes,
        length=len(order_codes) + 10,
        log=log,
    )
    return items


def get_query_code(
    stoken: str,
    order_code: str,
    log: Optional[LogFn] = None,
) -> str:
    """查询单个订单的查询码 (expCode)。

    v6.0.68 ★:輸入是純 Yahoo 號,但 SYB 上實際 code 可能有 +N 後綴。
    試 base + +1...+9 的所有候選,任一個 active 找到 expCode 就返回。
    返回查询码字符串,未找到返回空字符串。
    """
    candidates = [order_code] + [make_dup_code(order_code, n) for n in range(1, 10)]
    items = query_orders_by_code(stoken, candidates, log)
    # 優先 base,然後 +1, +2, ...
    by_code = {str(it.get("code", "")): it for it in items if it.get("code")}
    for c in candidates:
        item = by_code.get(c)
        if not item:
            continue
        exp_code = str(item.get("expCode") or "").strip()
        if exp_code:
            if c != order_code:
                _log(log, f"查询码: {order_code} (SYB 實際 {c}) -> {exp_code}")
            else:
                _log(log, f"查询码: {order_code} -> {exp_code}")
            return exp_code
    _log(log, f"查询码: {order_code} -> 未找到")
    return ""


def check_shipped_batch(
    stoken: str,
    order_codes: List[str],
    log: Optional[LogFn] = None,
) -> Dict[str, Dict]:
    """批量检查发货状态。

    v6.0.68 ★:輸入是純 Yahoo 號,但 SYB 上實際 code 可能因「已存在」自動 +N 後綴。
    所以對每個輸入 code,同時查 base + base+1 + ... + base+9,任一個 active 就算找到,
    結果用「原始輸入 code」當 key 回傳(發貨監控不用知道有後綴存在)。

    返回: {input_order_code: {"shipped": bool, "status": str, "status_code": int, "exp_code": str, "actual_code": str}}
        actual_code 是 SYB 上實際匹配的 code(可能帶後綴),供後續流程使用。
    """
    if not order_codes:
        return {}

    # 為每個輸入 code 準備 base + +1...+9 候選,1 query 全撈
    all_candidates = set()
    for c in order_codes:
        all_candidates.add(c)
        for n in range(1, 10):
            all_candidates.add(make_dup_code(c, n))

    items = query_orders_by_code(stoken, list(all_candidates), log)

    # 按 actual code 索引
    by_actual_code = {}
    for item in items:
        ac = str(item.get("code", ""))
        if ac:
            by_actual_code[ac] = item

    results = {}
    for input_code in order_codes:
        # 優先 base,然後 +1, +2, ...
        target_item = None
        actual = ""
        if input_code in by_actual_code:
            target_item = by_actual_code[input_code]
            actual = input_code
        else:
            for n in range(1, 10):
                c = make_dup_code(input_code, n)
                if c in by_actual_code:
                    target_item = by_actual_code[c]
                    actual = c
                    break

        if not target_item:
            results[input_code] = {
                "shipped": False, "status": "未找到", "status_code": -1,
                "exp_code": "", "exp_company": "", "actual_code": "",
            }
            continue

        status_code = target_item.get("status", 0)
        status_text = STATUS_MAP.get(status_code, f"未知({status_code})")
        exp_code = str(target_item.get("expCode") or "").strip()
        exp_company = str(target_item.get("expCompany") or "").strip()

        # status >= 45 (已揽收) 算已发货
        shipped = status_code >= 45
        results[input_code] = {
            "shipped": shipped,
            "status": status_text,
            "status_code": status_code,
            "exp_code": exp_code,
            "exp_company": exp_company,
            "actual_code": actual,  # SYB 上實際的 code(可能帶後綴),供下游 PDF 列印等使用
        }
        if actual != input_code:
            _log(log, f"  {input_code}: SYB 實際 code = {actual}(後綴版本)")

    _log(log, f"批量查状态: {len(order_codes)} 单, "
         f"已发货 {sum(1 for r in results.values() if r['shipped'])}")

    return results


# ── 5. 快速建单 ─────────────────────────────────────────

def create_orders(
    stoken: str,
    stock_ids: List[int],
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """快速建单。

    stock_ids: 库存 ID 列表 (从 stock/list 的 item["id"] 获取)
    返回: [{"code": "xxx", "errMsg": "建单成功/失败原因", "id": "123"}, ...]
    """
    if not stock_ids:
        return []

    _log(log, f"快速建单: {len(stock_ids)} 条")
    data = _post(stoken, "/am/stock/offlineCreateOld", stock_ids, log)
    return data.get("data", [])


# ── 6. 编辑订单 ─────────────────────────────────────────

def update_stock(
    stoken: str,
    stock_id: int,
    fields: Dict[str, Any],
    log: Optional[LogFn] = None,
) -> Dict:
    """编辑库存订单。

    stock_id: 库存 ID
    fields: 要更新的字段 (见 API 文档)
    """
    # 防呆：expCompany 用繁体「萊爾富」会被 顺云宝 拒绝，统一映射成简体「莱尔富」
    if isinstance(fields, dict) and fields.get("expCompany"):
        fields = dict(fields)
        fields["expCompany"] = _normalize_channel_for_syb(fields["expCompany"])
    payload = {"id": stock_id, **fields}
    _log(log, f"编辑订单: id={stock_id}")
    return _post(stoken, f"/am/stock/update?id={stock_id}", payload, log)


# ── 7. 获取详情 ─────────────────────────────────────────

def get_stock_detail(
    stoken: str,
    stock_id: int,
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """获取库存详情。

    返回详情列表。
    """
    data = _get(stoken, f"/am/stock/detail?id={stock_id}", log)
    return data.get("data", [])


def query_stock_details_by_code(
    stoken: str,
    order_code: str,
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """按订单 code 查 stock 的所有 detail（含 detailId / innerExpCode 等）。
    用于图片上传 — 需要先拿到 detailId 才能 bind thumbnail。

    实现：先用 query_orders_by_code 拿 stock id，再 get_stock_detail 拿 details。
    """
    items = query_orders_by_code(stoken, [order_code], log=log)
    if not items:
        return []
    # 找到匹配的 stock
    stock_id = None
    for item in items:
        if str(item.get("code", "")) == str(order_code):
            stock_id = item.get("id")
            break
    if not stock_id:
        return []
    return get_stock_detail(stoken, int(stock_id), log=log)


# ── 7b. 图片上传 + 绑定（让物流仓核对图打包用）────────────
# 需要 session cookie (stoken) 鉴权。Endpoint：
#   POST /am/attachment/upload?type=product&auth=1  (multipart, field=file) → {data: thumbId}
#   POST /am/stock/detailThumb?detailId=X&thumbId=Y                          → {data: true}

def _compress_image_for_upload(
    file_path: Path,
    max_bytes: int = 200_000,
    log: Optional[LogFn] = None,
) -> Tuple[Path, bytes, str]:
    """大檔(> max_bytes)PIL 自動壓縮到 < max_bytes,返回 (虛擬 path, bytes, mime)。
    小檔直接讀原檔返回。
    跨境網路對大 multipart upload 不穩,壓縮到 200KB 內穩定度大幅提升。
    物流核對圖不需要原始解析度,1280px JPEG q70 已足。
    """
    file_path = Path(file_path)
    size = file_path.stat().st_size
    if size <= max_bytes:
        with open(file_path, "rb") as f:
            return file_path, f.read(), _guess_image_mime(file_path)
    # 需要壓縮
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(file_path)
        # RGBA → RGB(JPEG 不支援 alpha,白底)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            try:
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            except Exception:
                bg.paste(img.convert("RGB"))
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # 縮邊到 max 1280
        max_dim = 1280
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        # 階梯式降質找到 < max_bytes 的版本
        for quality in (85, 75, 65, 55, 45):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            out_bytes = buf.getvalue()
            if len(out_bytes) <= max_bytes:
                # 用原 stem + .jpg 作虛擬名(server 看 filename + content)
                virtual_name = file_path.stem + f".compressed_q{quality}.jpg"
                _log(log, f"图片壓縮: {file_path.name} {size}→{len(out_bytes)} bytes (PIL JPEG q{quality})")
                return Path(virtual_name), out_bytes, "image/jpeg"
        # 5 階都壓不下來(原圖極大)→ 用 q45 結果繼續
        virtual_name = file_path.stem + ".compressed_q45.jpg"
        _log(log, f"图片壓縮: {file_path.name} {size}→{len(out_bytes)} bytes (PIL JPEG q45,仍偏大)")
        return Path(virtual_name), out_bytes, "image/jpeg"
    except Exception as e:
        _log(log, f"图片壓縮失敗(用原檔上傳): {e}")
        with open(file_path, "rb") as f:
            return file_path, f.read(), _guess_image_mime(file_path)


def upload_attachment(
    stoken: str,
    file_path: Path,
    log: Optional[LogFn] = None,
    timeout: int = 60,
) -> Tuple[int, str]:
    """上传图片到顺云宝，返回 (thumbId, error)。
    error == "" 表示成功。

    v6.1.38:加診斷 log(MD5 + raw response)+ 完整 Chrome headers + 大檔 retry
            + 大檔(>200KB)自動壓縮(PIL JPEG q85-45 階梯)
    修「Python 3.12.7 升級後大檔 multipart upload 在跨境 VPN 場景 SSL EOF / write timeout」
    實機驗證:7KB jpg 直傳 / 462KB png 壓縮後皆正常顯示
    """
    import requests
    import hashlib
    import time as _t
    file_path = Path(file_path)
    if not file_path.exists():
        return 0, f"文件不存在: {file_path}"

    url = f"{SYB_BASE_URL}/am/attachment/upload?type=product&auth=1"
    headers = {
        "x-requested-with": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{SYB_BASE_URL}/sys/admin/stock",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": SYB_BASE_URL,
    }

    # v6.1.38:大檔自動壓縮到 200KB 內(避免跨境 VPN write timeout)+ 算 MD5 給 diagnostic
    try:
        virtual_path, file_bytes, mime = _compress_image_for_upload(file_path, log=log)
    except Exception as e:
        return 0, f"read/compress file failed: {e}"
    file_size = len(file_bytes)
    file_md5 = hashlib.md5(file_bytes).hexdigest()
    upload_name = virtual_path.name

    # v6.1.38:大檔(> 100KB)write timeout retry 最多 3 次
    # 跨境 VPN 場景常見 socket write 超時,給 server 接收緩衝時間
    last_err = ""
    for attempt in range(3):
        try:
            files = {"file": (upload_name, file_bytes, mime)}
            resp = requests.post(
                url, headers=headers, cookies={"stoken": stoken},
                files=files, timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            _log(log, f"图片上传 raw response: {data}")
            if not data.get("status"):
                return 0, f"upload failed: {data.get('msg', '')}"
            thumb_id = data.get("data")
            if not isinstance(thumb_id, int) or thumb_id <= 0:
                return 0, f"unexpected response: {data}"
            _log(log, f"图片上传成功: {upload_name} ({file_size} bytes, md5={file_md5[:8]}) → thumbId={thumb_id}")
            return thumb_id, ""
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_err = str(e)
            if attempt < 2:
                wait = 5 + attempt * 5  # 5s, 10s
                _log(log, f"图片上传第 {attempt+1}/3 次失敗(網路/timeout),{wait}s 後重試: {str(e)[:80]}")
                _t.sleep(wait)
                continue
        except Exception as e:
            return 0, f"upload exception: {e}"
    return 0, f"upload exception (retries exhausted): {last_err}"


def bind_detail_thumb(
    stoken: str,
    detail_id: int,
    thumb_id: int,
    log: Optional[LogFn] = None,
    timeout: int = 30,
) -> Tuple[bool, str]:
    """把 thumbId 绑定到指定 detailId。返回 (success, error)。

    v6.1.38:加完整 Chrome headers,跟 upload_attachment 一致
    """
    import requests
    url = f"{SYB_BASE_URL}/am/stock/detailThumb?detailId={detail_id}&thumbId={thumb_id}"
    headers = {
        "x-requested-with": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": f"{SYB_BASE_URL}/sys/admin/stock",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": SYB_BASE_URL,
    }
    try:
        resp = requests.post(
            url, headers=headers, cookies={"stoken": stoken}, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status"):
            return False, f"bind failed: {data.get('msg', '')}"
        _log(log, f"图片绑定成功: detail={detail_id}, thumb={thumb_id}")
        return True, ""
    except Exception as e:
        return False, f"bind exception: {e}"


def _guess_image_mime(file_path: Path) -> str:
    ext = file_path.suffix.lower().lstrip(".")
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
        "gif": "image/gif", "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")


# ── 8. 面单 PDF 上传 ─────────────────────────────────────

def upload_label_pdf(
    stoken: str,
    pdf_path: Path,
    order_code: str = "",
    log: Optional[LogFn] = None,
) -> Dict:
    """上传面单 PDF 到 SYB。

    对应 /am/stock/pageImport — "批量上传面单"。
    文件名格式: {订单号}.pdf 或 {订单号}+{密码}.pdf

    Args:
        pdf_path: PDF 文件路径
        order_code: 订单号 (留空则从文件名提取)

    Returns:
        {"status": bool, "msg": str, "ordercode": str, "filename": str}
    """
    import requests

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 不存在: {pdf_path}")

    # 从文件名提取订单号
    if not order_code:
        stem = pdf_path.stem  # 去掉 .pdf
        order_code = stem.split("+")[0].strip()

    if not order_code:
        raise ValueError("无法确定订单号 (文件名或 order_code 参数)")

    _log(log, f"[SYB-HTTP] 上传面单: {pdf_path.name} -> {order_code}")

    s = requests.Session()
    s.headers.update({
        "stoken": stoken,
        "x-requested-with": "XMLHttpRequest",
        "Referer": f"{SYB_BASE_URL}/sys/admin/stock",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "keep-alive",
    })
    s.cookies.set("stoken", stoken, domain="www.shunyunbaoerp.com")

    # 预热连接（建立 SSL keep-alive，避免大文件上传时握手超时）
    try:
        s.get(f"{SYB_BASE_URL}/am/stock/list",
              params={"status": "13,15,20", "pageNo": "1", "pageSize": "1"},
              timeout=15)
    except Exception:
        pass

    with open(pdf_path, "rb") as f:
        files = {"file": (pdf_path.name, f, "application/pdf")}
        form_data = {"code": order_code, "pwd": ""}

        resp = s.post(
            f"{SYB_BASE_URL}/am/stock/pageImport",
            files=files,
            data=form_data,
            timeout=(60, 180),
        )
        resp.raise_for_status()
        result = resp.json()

    outer_ok = result.get("status", False)
    inner = result.get("data", {})
    inner_ok = inner.get("status", False) if isinstance(inner, dict) else False
    msg = inner.get("msg", "") if isinstance(inner, dict) else result.get("msg", "")
    ordercode = inner.get("ordercode", order_code) if isinstance(inner, dict) else order_code

    if outer_ok and inner_ok:
        _log(log, f"面单上传成功: {ordercode}")
    elif outer_ok:
        _log(log, f"面单已上传但解析异常: {ordercode} - {msg}")
    else:
        _log(log, f"面单上传失败: {ordercode} - {result.get('msg', '')}")

    return {
        "status": outer_ok and inner_ok,
        "uploaded": outer_ok,
        "msg": msg,
        "ordercode": ordercode,
        "filename": pdf_path.name,
    }


def upload_label_pdfs_batch(
    stoken: str,
    pdf_paths: List[Path],
    log: Optional[LogFn] = None,
) -> List[Dict]:
    """批量上传面单 PDF。

    v6.0.68 ★:對每個 PDF,自動解析 SYB 上實際的 active code(可能加 1/2/...後綴),
    避免 PDF 檔名是純 Yahoo 號但 SYB 上 code 是 +1 後綴版本導致 mismatch。

    Returns:
        [{status, uploaded, msg, ordercode, filename}, ...]
    """
    # 先批次撈所有 PDF 對應的 active SYB code(1 個 query 拿全部)
    base_codes = []
    for p in pdf_paths:
        stem = Path(p).stem
        base = stem.split("+")[0].strip()
        base_codes.append(base)

    # 為每個 base_code 準備候選(base + base+1 + ... + base+9)
    all_candidates = set()
    for base in base_codes:
        all_candidates.add(base)
        for n in range(1, 10):
            all_candidates.add(make_dup_code(base, n))

    code_to_active = {}  # base_code → actual SYB code
    if all_candidates:
        try:
            items = query_orders_by_code(stoken, list(all_candidates), log=log)
            existing_set = {str(it.get("code", "")) for it in items if it.get("code")}
            for base in base_codes:
                # 優先 base,然後 +1, +2, ...
                if base in existing_set:
                    code_to_active[base] = base
                else:
                    for n in range(1, 10):
                        c = make_dup_code(base, n)
                        if c in existing_set:
                            code_to_active[base] = c
                            break
        except Exception as e:
            _log(log, f"⚠️ 批次解析 SYB code 失敗: {e},退回直接用檔名")

    results = []
    for i, p in enumerate(pdf_paths):
        base = base_codes[i]
        # 用解析後的 active code(若有);沒有就退回用 base
        actual = code_to_active.get(base, base)
        if actual != base:
            _log(log, f"PDF {Path(p).name}: 檔名 {base},SYB 實際 code {actual}(後綴版本)")
        try:
            r = upload_label_pdf(stoken, p, order_code=actual, log=log)
            results.append(r)
        except Exception as e:
            results.append({
                "status": False,
                "uploaded": False,
                "msg": str(e),
                "ordercode": "",
                "filename": Path(p).name,
            })

    ok = sum(1 for r in results if r["status"])
    uploaded = sum(1 for r in results if r.get("uploaded"))
    _log(log, f"批量上传面单: {ok}成功 {uploaded - ok}解析异常 {len(results) - uploaded}失败 (共{len(results)})")
    return results


# ── Excel 解析辅助 ──────────────────────────────────────

def parse_template_to_import_rows(
    template_path: Path,
    sheet_name: str,
    shop_name: str = "",
    log: Optional[LogFn] = None,
) -> List[ImportRow]:
    """解析现有出货模板 Excel 为 ImportRow 列表。

    sheet_name: "线上贴单资料" (店配) 或 "宅配打包资料" (宅配)
    """
    import openpyxl

    template_path = Path(template_path)
    if not template_path.exists():
        return []

    try:
        wb = openpyxl.load_workbook(template_path, read_only=True, data_only=True)
    except Exception as e:
        _log(log, f"打开模板失败: {e}")
        return []

    if sheet_name not in wb.sheetnames:
        wb.close()
        return []

    ws = wb[sheet_name]
    rows = []

    # 读取表头 (第一行)
    header = []
    for cell in next(ws.iter_rows(min_row=1, max_row=1), []):
        header.append(str(cell.value or "").strip())

    if not header:
        wb.close()
        return []

    # 建立列名到索引的映射
    col_map = {}
    for i, h in enumerate(header):
        col_map[h] = i

    # 读数据行
    for row_cells in ws.iter_rows(min_row=2, values_only=True):
        vals = list(row_cells)
        if not vals or all(v is None or str(v).strip() == "" for v in vals[:3]):
            continue

        def _get(name: str, default="") -> str:
            idx = col_map.get(name)
            if idx is None or idx >= len(vals):
                return default
            v = vals[idx]
            return str(v).strip() if v is not None else default

        def _get_int(name: str, default=0) -> int:
            v = _get(name)
            try:
                return int(float(v)) if v else default
            except Exception:
                return default

        # 映射字段 — 适配常见的中文表头
        code = _get("订单编号") or _get("单号") or _get("code")
        if not code:
            continue

        row = ImportRow(
            code=code,
            product_name=_get("品名") or _get("货物品名") or _get("productName") or "商品",
            product_spec=_get("规格") or _get("货物规格") or _get("productSpec"),
            product_price=_get_int("价格") or _get_int("金额") or _get_int("productPrice"),
            product_qty=_get_int("数量") or _get_int("productQty") or 1,
            exp_company=_get("物流公司") or _get("配送方式") or _get("expCompany"),
            receiver=_get("收件人") or _get("receiver"),
            receiver_tel=_get("收件人电话") or _get("收件电话") or _get("电话") or _get("receiverTel"),
            receiver_addr=_get("收件人地址") or _get("收件地址") or _get("地址") or _get("receiverAddr"),
            receiver_shop_code=_get("收件门市编") or _get("门市编号") or _get("receiverShopCode"),
            receiver_shop_name=_get("收件门市名") or _get("门市名称") or _get("receiverShopName"),
            shop_name=_get("店铺名") or _get("shopName") or shop_name,
            remark2=_get("货单备注") or _get("remark2"),
            remark1=_get("报单备注") or _get("remark1"),
            exp_code=_get("查询码") or _get("expCode"),
            inner_exp_code=_get("快递单号") or _get("国内快递单号") or _get("innerExpCode"),
        )
        rows.append(row)

    wb.close()
    _log(log, f"解析模板: {template_path.name} / {sheet_name} -> {len(rows)} 行")
    return rows
