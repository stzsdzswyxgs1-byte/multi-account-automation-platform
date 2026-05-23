from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import tkinter as tk
from tkinter import ttk, messagebox

import requests

from core.accounts import save_settings
from core.webhook import send_wecom_text
from core.profile_lock import try_acquire, release, detect_chrome_profile_in_use


BASE_DIR = Path(__file__).resolve().parent.parent

# --------- 硬编码 AI 配置（同事共用，不需要每人填写） ---------
_HARDCODED_API_KEY = "<AI_API_KEY_REDACTED>"
_HARDCODED_BASE_URL = "https://<AI_PROXY_HOST>/v1"
_HARDCODED_ENDPOINT = "chat"
_HARDCODED_MODEL = "gpt-5.5"

# --------- 备用 AI API 列表（主 API 失败时按顺序尝试） ---------
# 全部 GPT,无感切换。两层 fallback 处理不同故障类型:
#   [1] 主 model 5.5 故障(502/model 不可用) → 同 endpoint 降级到 5.4
#   [2] 限速层本身故障/触发 429 → bypass rate-limiter 直连 cli-proxy-api
_FALLBACK_CHAIN = [
    {
        "api_key": "<AI_API_KEY_REDACTED>",
        "base_url": "https://<AI_PROXY_HOST>/v1",
        "endpoint": "chat",
        "model": "gpt-5.4",
    },
    {
        "api_key": "<AI_API_KEY_REDACTED>",
        "base_url": "https://<AI_FALLBACK_HOST>/v1",
        "endpoint": "chat",
        "model": "gpt-5.5",
    },
]

# TG Token 从 tg_tokens.json 读取（每人独立）
def _load_tg_token() -> str:
    try:
        p = Path(__file__).resolve().parent.parent / "tg_tokens.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f).get("ai_bot_token", "")
    except Exception:
        pass
    return ""

_HARDCODED_TG_TOKEN = _load_tg_token()


def _ts() -> str:
    try:
        return time.strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(s: str, n: int = 1200) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "\n... (已截断)"


# --------- Privacy / Redaction ---------

_PHONE_PATTERNS = [
    # TW mobile 09xxxxxxxx
    re.compile(r"\b09\d{8}\b"),
    # JP mobile 070/080/090-xxxx-xxxx or without dashes
    re.compile(r"\b0(?:70|80|90)[- ]?\d{4}[- ]?\d{4}\b"),
]
# Generic long digits — but skip Yahoo item IDs (10-prefixed 12-digit) and numbers inside URLs
_GENERIC_DIGIT_RE = re.compile(r"(?<!\d)\d{8,14}(?!\d)")
_YAHOO_ITEM_ID_RE = re.compile(r"^10\d{10}$")
_IN_URL_RE = re.compile(r"https?://\S*")


def redact_sensitive(text: str) -> str:
    """Very lightweight redaction to reduce accidental leakage to AI API.

    - Masks obvious phone numbers.
    - Masks blocks following '收件地址' / '收货地址' if present.

    It is intentionally conservative: it won't delete everything.
    """
    if not text:
        return ""

    s = str(text)
    for pat in _PHONE_PATTERNS:
        s = pat.sub("[已脱敏号码]", s)

    # Generic long digits — skip Yahoo item IDs and numbers inside URLs
    url_spans = [(m.start(), m.end()) for m in _IN_URL_RE.finditer(s)]

    def _is_in_url(start: int, end: int) -> bool:
        return any(us <= start and end <= ue for us, ue in url_spans)

    def _generic_repl(m):
        digit = m.group(0)
        if _YAHOO_ITEM_ID_RE.match(digit):
            return digit  # 保留 Yahoo 商品编号
        if _is_in_url(m.start(), m.end()):
            return digit  # 保留 URL 内的数字
        return "[已脱敏号码]"

    s = _GENERIC_DIGIT_RE.sub(_generic_repl, s)

    # Address blocks: if keywords exist, mask next 3-8 lines (heuristic)
    addr_keys = ["收件地址", "收货地址", "收貨地址", "地址"]
    for k in addr_keys:
        if k in s:
            lines = s.splitlines()
            out: List[str] = []
            i = 0
            while i < len(lines):
                line = lines[i]
                out.append(line)
                if k in line:
                    # mask following lines until blank or max lines
                    j = i + 1
                    masked = 0
                    while j < len(lines) and masked < 8:
                        if not lines[j].strip():
                            break
                        out.append("[已脱敏地址信息]")
                        masked += 1
                        j += 1
                    i = j
                    continue
                i += 1
            s = "\n".join(out)

    return s


def extract_size_clues(text: str) -> str:
    """Extract likely size/measurement lines from product text."""
    if not text:
        return ""
    keys = [
        "尺寸", "尺码", "尺碼", "胸围", "胸圍", "肩宽", "肩寬", "袖长", "袖長",
        "衣长", "衣長", "腰围", "腰圍", "臀围", "臀圍", "长度", "長度",
        "サイズ", "身幅", "肩幅", "着丈", "ウエスト", "バスト", "ヒップ", "股下"
    ]
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]

    picked: List[str] = []
    for ln in lines:
        hit = any(k in ln for k in keys)
        num = bool(re.search(r"\d", ln))
        unit = bool(re.search(r"cm|mm|\bS\b|\bM\b|\bL\b|\bXL\b|\bXXL\b", ln, re.IGNORECASE))
        if hit and (num or unit):
            picked.append(ln)

    # Also pick 'size table' nearby lines if found
    if not picked:
        for idx, ln in enumerate(lines):
            if any(k in ln for k in ["尺寸表", "尺码表", "サイズ表", "size", "SIZE"]):
                start = max(0, idx)
                end = min(len(lines), idx + 12)
                picked.extend(lines[start:end])
                break

    # De-duplicate and cap
    uniq: List[str] = []
    for x in picked:
        if x not in uniq:
            uniq.append(x)
    return "\n".join(uniq[:30])




# --- AI转发：客服规则辅助判断 ---
FRAGILE_KEYWORDS = [
    "瓷", "陶瓷", "玻璃", "水晶", "镜", "鏡", "杯", "碗", "盤", "盘", "盘子",
    "花瓶", "摆件", "擺件", "公仔", "手办", "手辦", "模型", "雕像", "灯罩", "燈罩",
    "相框", "畫框", "画框", "茶具", "酒杯",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u3000", " ").strip())


def infer_source_platform_from_url(url: str) -> str:
    u = (url or "").lower()
    if "mercari.com" in u:
        return "mercari"  # 日本
    if "goofish.com" in u or "xianyu" in u or "idlefish" in u:
        return "xianyu"  # 闲鱼
    return ""


def count_arrival_questions(text: str) -> int:
    t = text or ""
    keys = [
        "到货", "到貨", "多久", "幾天", "几天", "什麼時候到", "什么时候到", "什么时候能到", "什麼時候能到",
        "發貨", "发货", "出貨", "出货", "到達", "到达", "送達", "送达", "關稅", "关税",
    ]
    n = 0
    for k in keys:
        n += t.count(k)
    return n


def guess_fragile(title: str, product_text: str) -> bool:
    title_hay = _norm(title)
    for kw in FRAGILE_KEYWORDS:
        if kw and kw in title_hay:
            return True
    hay = _norm((title or "") + " " + (product_text or ""))
    for kw in FRAGILE_KEYWORDS:
        if kw and kw in hay:
            return True
    return False


def infer_can_buy(product_text: str, source: str) -> str:
    """返回：'是'/'否'/'未知'。仅做启发式，不保证 100% 准。"""
    t = product_text or ""
    tn = (t.replace("\u3000", " ").lower())

    if source == "mercari":
        # 可购买按钮（页面文本里通常会出现）
        if ("購入手続きへ" in t) or ("購入する" in t) or ("購入へ" in t) or ("購入に進む" in t):
            return "是"
        if ("sold out" in tn) or ("売り切れ" in t) or ("売切れ" in t) or ("SOLD" in t):
            return "否"
        return "未知"

    if source == "xianyu":
        bad_keys = ["宝贝已下架", "寶貝已下架", "已售出", "已被拍下", "不存在", "已失效", "已下架"]
        for k in bad_keys:
            if k in t:
                return "否"
        good_keys = ["立即购买", "立刻购买", "我想要", "想要", "立即下单", "下单"]
        for k in good_keys:
            if k in t:
                return "是"
        return "未知"

    return "未知"


def source_label(source: str) -> str:
    if source == "mercari":
        return "煤爐/Mercari（日本出貨）"
    if source == "xianyu":
        return "鹹魚（以台灣出貨口徑回覆）"
    return "（未知/未提供）"

# --------- Commander (routing / "指挥") ---------

@dataclass
class CommanderDecision:
    action: str  # AUTO_REPLY | NEED_PRODUCT_DATA | NEED_SELLER | NEED_CLARIFY
    confidence: float = 0.0
    reason: str = ""
    missing_info: List[str] = field(default_factory=list)
    should_fetch_product: bool = False
    # v6.1:強制 CoT — debug 用,看 Commander 怎麼推理
    thought_steps: List[str] = field(default_factory=list)
    # v6.1:議價結構化 hint(Writer 接力時直接用,不用再算 R 比例)
    # 內容 {bid_amount, list_price, bid_ratio, suggested_tier}
    pricing_hint: dict = field(default_factory=dict)
    # v6.1.56:NEED_SELLER 時,AI 判斷該附買家發的哪幾張圖給賣家
    # 對應 conv.buyer_image_urls 的 index list(從 0 起);[] = 不附圖純文字
    # 例:[0, 2] = 附第 0 跟第 2 張買家圖給賣家
    seller_attach_images_indices: List[int] = field(default_factory=list)
    # v6.1.56:AI 為什麼這樣選/不選圖(供 user 預覽時看)
    seller_attach_images_reason: str = ""


_ROUTE_ACTIONS = ("AUTO_REPLY", "NEED_PRODUCT_DATA", "NEED_SELLER", "NEED_CLARIFY", "NO_REPLY")

# v6.1:_route_by_rules + _PRICE_KEYS / _CANCEL_RETURN_KEYS 等 keyword fast-path 已於
# v4.7.29 廢棄(所有決策走 LLM),原本約 200 行死碼已移除。歷史請看 git log。
def extract_latest_buyer_message(text: str) -> str:
    """从带【买家】/【卖家】标记的对话中，提取买家最后一段连续消息。
    用于规则快速匹配，判断最新消息是否为感谢/收尾语。
    """
    lines = (text or "").strip().splitlines()
    # 从后往前找最后一段连续的买家消息
    latest_lines: list = []
    found_buyer = False
    for ln in reversed(lines):
        s = ln.strip()
        if not s:
            if found_buyer:
                break
            continue
        if s.startswith("【卖家】"):
            if found_buyer:
                break
            continue
        content = s[4:] if s.startswith("【买家】") else s
        # 跳过 URL 行和 recalled
        if "http://" in content or "https://" in content:
            continue
        if content.strip().lower() == "recalled":
            continue
        found_buyer = True
        latest_lines.append(content)
    latest_lines.reverse()
    return "\n".join(latest_lines)


def _extract_json_loose(s: str) -> Optional[dict]:
    if not s:
        return None
    # Try code fence first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try first {...} block
    m2 = re.search(r"(\{.*\})", s, flags=re.DOTALL)
    if m2:
        blob = m2.group(1)
        # Trim trailing text after last }
        blob = blob[: blob.rfind("}") + 1]
        try:
            return json.loads(blob)
        except Exception:
            return None
    return None


def commander_decide(
    *,
    api_key: str,
    base_url: str,
    endpoint_mode: str,
    model: str,
    buyer_text: str,
    product_text: str,
    item_url: str,
    source_hint: str,
    can_buy: str,
    fragile: bool,
    repeated_arrival: bool,
    latest_buyer_text: str = "",
    timeout_sec: int = 50,
    image_urls: Optional[List[str]] = None,
    buyer_image_urls: Optional[List[str]] = None,
    media_prompt_section: Optional[str] = None,
) -> CommanderDecision:
    """
    Decide next step like a human dispatcher:
      - AUTO_REPLY: generate reply now
      - NEED_PRODUCT_DATA: fetch product data first
      - NEED_SELLER: escalate to seller/human
      - NEED_CLARIFY: ask a clarifying question / request link
    """
    # 所有决策交给 AI LLM，不再使用关键词快速规则
    sys_p = (
        "你是電商客服『指揮官』，只負責判斷下一步該做什麼，不寫長文。\n"
        "你必須輸出 **JSON**（不要多餘文字）。\n\n"

        "【🚨 最高優先規則 — 已售出/無貨】\n"
        "如果【頁面可購買(啟發式)】=否（已售出/無貨），必須判為 AUTO_REPLY。\n"
        "不管買家問什麼，直接回覆買家沒貨了。reason 填：『貨源已售出，直接告知沒貨』\n\n"

        "【🚨 必須 NEED_SELLER 的 8 種情況 — 來自實戰修正】\n"
        "1. 多商品連結比較/合購：對話中買家貼了 2 個以上不同的 tw.bid.yahoo.com/item/ 連結,且最後一句是『這兩個都要嗎/合購可便宜/總共多少/比較這兩個』等 → NEED_SELLER\n"
        "   注意:**單純前後切換商品不算這種**（買家先問商品 A 後又問商品 B 是常見場景,可正常回覆 — 此時你會收到所有商品的圖+描述,系統已把【買家最後問】的當主商品,你正常用主商品圖回答即可）\n"
        "2. 物流追問：買家最後一句含『出貨了嗎/寄了嗎/單號/到貨時間/物流/到了沒/還沒寄/送了嗎/幾號到/查單』→ NEED_SELLER（AI 不知實際物流狀態，會編造『已寄出/明天到』）\n"
        "3. ⚠️ 議價:**永遠 AUTO_REPLY**(用 Yahoo 標價婉拒)— 絕對不要 NEED_SELLER!原因:\n"
        "   - 我們是中間商,Yahoo 端定價已含利潤,被砍價該自己決策,不能 forward 給上游賣家\n"
        "   - 上游賣家在閒魚(人民幣),Yahoo 是台幣,貨幣不同直接問會鬧笑話\n"
        "   - 議價答案應該由 AI 用 Yahoo 標價婉拒(語氣客氣,留議價空間,如『不好意思這個就是這個價了/再給您算優惠 100/這已經是優惠價了』)\n"
        "   - 真要讓主管定底價,用 NEED_CLARIFY(不是 NEED_SELLER)\n"
        "   ⚡ **議價短句識別**:買家最後一句是『賣嗎?/能賣?/可以賣嗎?/賣不賣?/可以嗎?』這類 2-6 字短句,且**對話前面有過出價/議價脈絡**(如『7800』『最低多少』等)→ 視為議價追問,走 AUTO_REPLY + pricing_hint(用前面提到的數字當 bid_amount)\n"
        "   ❌ 千萬不要把『賣嗎?』當『有貨嗎?』走貨況路徑!\n"
        "4. 產地問題：買家問『在台灣嗎/在大陸嗎/台灣現貨嗎/從哪裡寄/幾天到/日本寄來嗎』且商品描述未明確說在台灣 → NEED_SELLER\n"
        "5. 規格缺資料：買家問尺寸/材質/重量/盒子/證書/保固/年代/說明書/配件，但【商品頁文字】中找不到該關鍵詞 → NEED_SELLER（不能編造）\n"
        "6. 投訴爭議：買家含『假/騙/詐騙/瑕疵/破損/不對/有問題/退款/退貨/換貨』→ NEED_SELLER\n"
        "7. 訂單後續：地址/收件/發票/取消訂單/改地址/改付款 → NEED_SELLER\n"
        "8. 要求實拍/更多照片/細節圖/影片 → NEED_SELLER\n"
        "9. 買家發了語音/視頻但 AI 解析失敗（[語音 無法轉寫]/[視頻 無法解析]）且訊息看起來是新問題 → NEED_CLARIFY（請買家用文字補充）\n"
        "判定後在 reason 寫觸發了哪一條（例如：『觸發規則 3：買家出價 3000 但賣家未報底價』）\n\n"

        "【對話媒體訊息標記】\n"
        "對話中可能出現這些前綴(都是 AI 對媒體做的自動處理,不是對方原話):\n"
        "- [語音→文字] xxx → 對方語音 AI 自動轉寫,可能有同音字錯誤,理解大意即可\n"
        "- [視頻→描述] xxx → 對方視頻 AI 看影像寫的描述(非對方文字),可用來理解買家想要的瑕疵/外觀細節\n"
        "- [圖片] URL → 只有 URL 沒描述\n"
        "- [語音 無法轉寫]/[視頻 無法解析] → 解析失敗,若關鍵資訊在裡面 → NEED_CLARIFY 請對方用文字補充\n\n"

        "【對話持續性】\n"
        "買家訊息包含【買家】和【賣家】完整對話歷史。\n"
        "- 賣家已經回答過的問題就不要重複判斷\n"
        "- 只針對對話最底部的【買家】訊息判斷 action\n"
        "- 如果賣家已報過底價（如『最低 4800』），買家再議價 → AUTO_REPLY（複述底價）\n"
        "- 如果賣家已讓價並等買家確認，買家說『好/我要/下單』→ AUTO_REPLY（簡短配合）\n\n"

        "【多商品場景處理】\n"
        "對話中可能出現多個商品連結(買家先問 A 不合適,又貼了 B)。系統處理規則:\n"
        "- 【商品頁文字】section 會列出所有商品的描述與規格\n"
        "- 【商品圖片】section 包含所有商品的圖(primary 商品圖優先在前)\n"
        "- primary 商品 = 買家最後問的那個(內部已標出)\n"
        "你的職責:\n"
        "1. 根據對話最底部買家訊息,判斷他當下在問哪個商品(通常是 primary)\n"
        "2. 從對應商品的【描述+圖】尋找答案(內徑/材質/狀況等)\n"
        "3. 圖內看得到答案就 AUTO_REPLY,看不到才 NEED_SELLER\n"
        "4. 如果買家問題涉及商品比較/合購,才走規則 1 的 NEED_SELLER\n\n"

        "【NO_REPLY 判定條件 — 收緊】\n"
        "只有同時滿足以下條件才判 NO_REPLY：\n"
        "(a) 對話最底部買家訊息 ≤ 6 個字\n"
        "(b) 只包含純感謝/收尾語（謝謝/感恩/收到/好的/好喔/掰/ok/👌/👍）\n"
        "(c) 沒有任何新問題\n"
        "如果上述不全滿足，往 AUTO_REPLY 或 NEED_SELLER 走。\n\n"

        "可選 action：AUTO_REPLY / NO_REPLY / NEED_PRODUCT_DATA / NEED_SELLER / NEED_CLARIFY\n"
        "- AUTO_REPLY：可直接回覆的簡單情境：\n"
        "  · 『還在嗎/有貨嗎/能買嗎』類確認（且不是多連結情境）\n"
        "  · 商品頁文字裡能找到答案的規格題\n"
        "  · 賣家已報過底價的議價（複述底價）\n"
        "  · 一般禮貌招呼（你好/在嗎）\n"
        "  · 已下標確認（買家『下單了』）\n"
        "  · 運費（默認免運）\n"
        "  · 貨源已售出（直接告知沒貨）\n"
        "- NO_REPLY：見上面 3 個條件全滿足\n"
        "- NEED_PRODUCT_DATA：需先看商品頁/規格才能答\n"
        "- NEED_SELLER：見上面 8 條觸發條件之一\n"
        "- NEED_CLARIFY：訊息太模糊（沒給連結/沒說哪件商品）\n\n"

        "【🧠 強制思考三步驟 — 必須在 thought_steps 內列出】\n"
        "在輸出 action 之前,**先依序回答**這三步:\n"
        "1. 對話最底部買家最新一句是什麼?是新問題還是回應賣家?\n"
        "2. 對話歷史中【賣家】是否已回應/讓價/報過底價?有就略過讓主管判定的鐵律\n"
        "3. 商品頁文字+圖片內能否直接找到答案?能 → AUTO_REPLY,不能 → NEED_SELLER\n\n"

        "【💰 議價場景必填 pricing_hint】\n"
        "若 action=AUTO_REPLY 且觸發議價(買家明確出價或要求降價),**必填 pricing_hint**:\n"
        "  - bid_amount: 買家出價(整數,單位 TWD;沒出明確數字填 null)\n"
        "  - list_price: Yahoo 標價(從商品頁/連結附近抓,沒抓到 null)\n"
        "  - bid_ratio: bid_amount/list_price (兩者都有才算,否則 null)\n"
        "  - suggested_tier: 依比例給 Writer 的建議檔位\n"
        "    · ratio>=0.85 → 'accept_or_minor_haggle'(可成交,或讓 50-200 收尾)\n"
        "    · 0.70<=ratio<0.85 → 'counter_offer'(反提折中價,維持利潤)\n"
        "    · ratio<0.70 → 'firm_refuse'(婉拒,留議價空間)\n"
        "    · ratio=null → 'general_refuse'(沒具體出價,複述標價婉拒)\n\n"

        "【v6.1.56:NEED_SELLER 時是否附買家圖給賣家】\n"
        "若 action=NEED_SELLER 且【附帶圖片】內含買家圖,判斷該附買家的哪幾張給賣家:\n"
        "  - seller_attach_images_indices: 買家圖 index list(0-based,對應上面【對話媒體】列出的買家圖順序)\n"
        "    · 例:[0, 2] = 附第 1 跟第 3 張買家圖(會跟著文字一起送給賣家)\n"
        "    · []  = 不附圖純文字(物流問題、純規格追問等)\n"
        "  - seller_attach_images_reason: 一句話原因,例:「買家圈紅圈在某物件,需給賣家確認指代」\n\n"
        "判斷規則:\n"
        "  ✅ 該附的場景:\n"
        "    · 買家圈了/畫了/指了實物 → 賣家要看才知道指什麼\n"
        "    · 買家發瑕疵特寫/細節照 → 問賣家「這個是不是這樣?」\n"
        "    · 買家發實物對比照(收到的 vs 商品圖) → 需賣家確認\n"
        "  ❌ 不附的場景:\n"
        "    · 物流/取貨單照、單號照 → 跟賣家無關\n"
        "    · 對話截圖/螢幕截圖 → 賣家看不懂上下文\n"
        "    · 純文字問題(運費、付款、退款流程) → 不需圖\n"
        "    · 買家圖內容對賣家判斷無幫助(自拍、無關背景)\n"
        "  ⚠️ 多張時:只挑跟問題直接相關的(不是全傳)\n\n"

        "輸出 JSON schema:\n"
        "{\n"
        '  "thought_steps": ["步驟1的答案", "步驟2的答案", "步驟3的答案"],\n'
        '  "action": "AUTO_REPLY",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "一句話原因(觸發哪條規則)",\n'
        '  "pricing_hint": {"bid_amount": null, "list_price": null, "bid_ratio": null, "suggested_tier": null},\n'
        '  "missing_info": ["可為空"],\n'
        '  "should_fetch_product": false,\n'
        '  "seller_attach_images_indices": [],\n'
        '  "seller_attach_images_reason": ""\n'
        "}\n"
    )

    user_p = (
        "【買家訊息】\n" + (buyer_text or "") + "\n\n"
        f"【商品連結】{item_url or ''}\n"
        f"【貨源推斷】{source_label(source_hint)}\n"
        f"【頁面可購買(啟發式)】{can_buy}\n"
        f"【初步易碎】{'是' if fragile else '否/不確定'}\n"
        f"【連續追問到貨(>=2次)】{'是' if repeated_arrival else '否'}\n\n"
        "【商品頁文字（可能為空）】\n" + _short(product_text or "", 6000)
    )

    # v6.1.55:caller 用 build_media_for_ai 算好權重排序的媒體(賣家恆 1.0,買家衰減半衰期 10 分鐘)
    # media_prompt_section 含完整描述+weight 標記;image_urls 是對應的 URL 列表(含視頻首幀 base64)
    if media_prompt_section:
        user_p += "\n\n" + media_prompt_section
        # image_urls 直接用 caller 傳的(已含權重排序)
    elif buyer_image_urls or image_urls:
        # v6.1.54 fallback:caller 沒走新管道時用舊邏輯
        _buyer_imgs = list(buyer_image_urls or [])[:5]
        _product_imgs = list(image_urls or [])
        _merged_imgs = _buyer_imgs + _product_imgs
        if _merged_imgs:
            if _buyer_imgs and _product_imgs:
                user_p += (
                    f"\n\n【附帶圖片】共 {len(_merged_imgs)} 張(依序):\n"
                    f"  ⚠️ 前 {len(_buyer_imgs)} 張 = 買家剛剛發的圖 — 優先看!\n"
                    f"  後 {len(_product_imgs)} 張 = 商品圖(用於判斷尺寸/材質/狀況)\n"
                )
            elif _buyer_imgs:
                user_p += f"\n\n【買家發的圖】{len(_buyer_imgs)} 張 — 優先看!"
            else:
                user_p += (
                    "\n\n【商品圖片】\n"
                    f"附帶 {len(_product_imgs)} 張商品圖,請觀察尺寸/材質/狀況/文字標籤。"
                )
            image_urls = _merged_imgs

    ok, txt = call_openai(
        api_key=api_key,
        base_url=base_url,
        endpoint_mode=endpoint_mode,
        model=model,
        system_prompt=sys_p,
        user_prompt=user_p,
        timeout_sec=timeout_sec,
        image_urls=image_urls if image_urls else None,
    )
    if not ok:
        # fallback: be conservative
        return CommanderDecision(action="NEED_SELLER", confidence=0.3, reason=f"指揮模型失敗：{_short(txt,120)}")

    data = _extract_json_loose(txt) or {}
    action = str(data.get("action") or "").strip().upper()
    if action not in _ROUTE_ACTIONS:
        # last-resort
        return CommanderDecision(action="AUTO_REPLY", confidence=0.4, reason="指揮輸出不規範，先嘗試生成回覆")

    conf = data.get("confidence")
    try:
        conf_f = float(conf)
    except Exception:
        conf_f = 0.0

    missing = data.get("missing_info") or []
    if not isinstance(missing, list):
        missing = []
    missing = [str(x) for x in missing if str(x).strip()][:8]

    should_fetch = bool(data.get("should_fetch_product")) or (action == "NEED_PRODUCT_DATA" and bool((item_url or "").strip()))

    # v6.1:解析 thought_steps + pricing_hint
    raw_thought = data.get("thought_steps") or []
    if isinstance(raw_thought, list):
        thought_steps = [str(x)[:200] for x in raw_thought if str(x).strip()][:5]
    else:
        thought_steps = []
    raw_pricing = data.get("pricing_hint") or {}
    pricing_hint = {}
    if isinstance(raw_pricing, dict):
        # whitelist 欄位 + 型別檢查,避免 LLM 輸出怪格式炸下游
        for k in ("bid_amount", "list_price"):
            v = raw_pricing.get(k)
            if isinstance(v, (int, float)) and v > 0:
                pricing_hint[k] = int(v)
        v = raw_pricing.get("bid_ratio")
        if isinstance(v, (int, float)) and 0 < v <= 2:
            pricing_hint["bid_ratio"] = float(v)
        tier = raw_pricing.get("suggested_tier")
        if isinstance(tier, str) and tier in (
            "accept_or_minor_haggle", "counter_offer", "firm_refuse", "general_refuse",
        ):
            pricing_hint["suggested_tier"] = tier

    # v6.1.56:解析 seller_attach_images_indices(NEED_SELLER 時 AI 判斷該附買家哪幾張圖)
    _raw_attach = data.get("seller_attach_images_indices") or []
    attach_indices: List[int] = []
    if isinstance(_raw_attach, list):
        for x in _raw_attach[:5]:  # cap 5 張
            try:
                _i = int(x)
                if 0 <= _i < 20 and _i not in attach_indices:  # 防止無效 index
                    attach_indices.append(_i)
            except (ValueError, TypeError):
                pass
    _attach_reason = str(data.get("seller_attach_images_reason") or "").strip()[:200]

    return CommanderDecision(
        action=action,
        confidence=max(0.0, min(1.0, conf_f)),
        reason=str(data.get("reason") or "").strip()[:200],
        missing_info=missing,
        should_fetch_product=should_fetch,
        thought_steps=thought_steps,
        pricing_hint=pricing_hint,
        seller_attach_images_indices=attach_indices,
        seller_attach_images_reason=_attach_reason,
    )


def format_commander_banner(d: Optional[CommanderDecision]) -> str:
    if not d:
        return ""
    parts = [f"【指揮判斷】動作={d.action} 置信={d.confidence:.2f}"]
    if d.reason:
        parts.append(f"原因：{d.reason}")
    if d.missing_info:
        parts.append("缺少/需要確認：" + "、".join(d.missing_info))
    if d.should_fetch_product:
        parts.append("提示：需要/建議先抓商品頁資訊")
    return "\n".join(parts).strip()



# --------- OpenAI API (Responses or Chat Completions) ---------


def _openai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _anthropic_headers(api_key: str) -> Dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


def _post_with_retry(url, headers, payload, timeout_sec, max_retries=2):
    """POST 请求，对 5xx 错误自动重试。"""
    import time as _t
    last_r = None
    for i in range(max_retries + 1):
        last_r = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)
        if last_r.status_code < 500:
            return last_r
        if i < max_retries:
            _t.sleep(3)
    return last_r


def _parse_sse_response(text: str) -> str:
    """解析 SSE (Server-Sent Events) 流式响应，拼接 delta.content。

    备用 API (newcli.com) 即使 stream=false 也返回 SSE 格式：
      data: {"choices":[{"delta":{"content":"你"}}]}
      data: {"choices":[{"delta":{"content":"好"}}]}
      ...
      data: [DONE]
    """
    parts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
            c = delta.get("content")
            if c:
                parts.append(c)
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
    return "".join(parts).strip()


def call_openai(
    *,
    api_key: str,
    base_url: str,
    endpoint_mode: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_urls: Optional[List[str]] = None,
    timeout_sec: int = 60,
) -> Tuple[bool, str]:
    """Call OpenAI API and return (ok, text_or_error).

    主 API 失败 (401/403/429) 时自动无感切换到备用 API。
    """
    ok, text = _call_openai_primary(
        api_key=api_key, base_url=base_url, endpoint_mode=endpoint_mode,
        model=model, system_prompt=system_prompt, user_prompt=user_prompt,
        image_urls=image_urls, timeout_sec=timeout_sec,
    )
    # 主 API 成功 → 直接返回
    if ok:
        return ok, text

    # 主 API 失败 → 按顺序尝试备用 API 链
    errors = [text or "主API失败"]
    for i, fb in enumerate(_FALLBACK_CHAIN):
        fb_ok, fb_text = _call_openai_primary(
            api_key=fb["api_key"], base_url=fb["base_url"],
            endpoint_mode=fb["endpoint"], model=fb["model"],
            system_prompt=system_prompt, user_prompt=user_prompt,
            image_urls=image_urls, timeout_sec=timeout_sec,
        )
        if fb_ok:
            return fb_ok, fb_text
        errors.append(f"[备用{i+1}] {fb_text}")

    return False, " | ".join(errors)


def _call_openai_primary(
    *,
    api_key: str,
    base_url: str,
    endpoint_mode: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_urls: Optional[List[str]] = None,
    timeout_sec: int = 60,
) -> Tuple[bool, str]:
    """Call OpenAI API and return (ok, text_or_error)."""
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "缺少 API Key"

    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        base_url = _HARDCODED_BASE_URL

    # Normalize base URL so it works with both:
    #   - https://api.openai.com
    #   - https://api.openai.com/v1
    #   - https://api2.aigcbest.top/v1
    api_root = base_url
    if not re.search(r"/v1$", api_root, flags=re.IGNORECASE):
        api_root = api_root + "/v1"

    endpoint_mode = (endpoint_mode or "responses").strip().lower()
    model = (model or "").strip() or _HARDCODED_MODEL

    # 构建多模态 user content（全部图片都给 AI，不限数量）
    if image_urls:
        if endpoint_mode == "responses":
            # Responses API: type=input_text / input_image
            user_content = [{"type": "input_text", "text": user_prompt or ""}]
            for img_url in image_urls:
                user_content.append({
                    "type": "input_image",
                    "image_url": img_url,
                })
        else:
            # Chat / Anthropic: type=text / image_url
            user_content = [{"type": "text", "text": user_prompt or ""}]
            for img_url in image_urls:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img_url},
                })
    else:
        user_content = user_prompt or ""

    try:
        if endpoint_mode == "anthropic":
            url = f"{api_root}/messages"
            payload = {
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
            }
            if system_prompt:
                payload["system"] = system_prompt
            r = _post_with_retry(url, _anthropic_headers(api_key), payload, timeout_sec)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}: {r.text[:400]}"
            data = r.json()
            # Anthropic response: {"content":[{"type":"text","text":"..."}], ...}
            content = data.get("content")
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                text = "\n".join(parts).strip()
                if text:
                    return True, text
            return False, f"AI 返回格式异常: {json.dumps(data, ensure_ascii=False)[:300]}"

        if endpoint_mode == "chat":
            url = f"{api_root}/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt or ""},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
            }
            r = _post_with_retry(url, _openai_headers(api_key), payload, timeout_sec)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}: {r.text[:400]}"
            data = r.json()
            text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if not text:
                return False, "AI 返回空内容"
            return True, text

        # default: Responses API
        url = f"{api_root}/responses"
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
        }
        r = _post_with_retry(url, _openai_headers(api_key), payload, timeout_sec)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:400]}"
        data = r.json()

        # Try several common layouts
        if isinstance(data.get("output_text"), str) and data.get("output_text").strip():
            return True, data["output_text"].strip()

        # output: [{type: 'message', content:[{type:'output_text'/'text', text: ...}]}]
        out = data.get("output")
        if isinstance(out, list):
            parts: List[str] = []
            for item in out:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "message":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        t = c.get("text") or c.get("content")
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
            if parts:
                return True, "\n".join(parts).strip()

        # 解析失败：返回错误而非 JSON 原文，避免上层把 JSON 当成 AI 回复发出去
        return False, f"AI 返回格式异常: {json.dumps(data, ensure_ascii=False)[:300]}"

    except Exception as e:
        return False, str(e)


# --------- Optional product page scraping via Playwright ---------


def _profile_dir(profile_id: str) -> Path:
    return (BASE_DIR / "profiles" / (profile_id or "")).resolve()


async def _fetch_page_text_async(
    *,
    chrome_path: str,
    profile_dir: Path,
    url: str,
    headless: bool,
    proxy: str = "",
    save_screenshot: bool = True,
) -> Dict[str, Any]:
    from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

    proxy = (proxy or "").strip()
    proxy_kw = {"server": proxy} if proxy else None

    async with async_playwright() as p:
        ctx = None
        try:
            _hl = bool(headless)
            _args = get_launch_args(headless=_hl)
            if _hl:
                _args.append("--window-size=1280,800")
            _fwd_lkw = dict(
                user_data_dir=str(profile_dir),
                executable_path=chrome_path,
                headless=False,
                proxy=proxy_kw,
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=_hl),
            )
            _fwd_lkw["no_viewport"] = True   # Patchright: headless=False 下 viewport 会 getWindowForTarget
            try:
                ctx = await p.chromium.launch_persistent_context(**_fwd_lkw)
            except TypeError:
                _fwd_lkw.pop("no_viewport", None)
                ctx = await p.chromium.launch_persistent_context(**_fwd_lkw)
            await apply_runtime_normalization_async(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            await page.goto(url, wait_until="domcontentloaded")
            # give the page a short time to render dynamic content
            try:
                await page.wait_for_timeout(600)
            except Exception:
                pass

            title = ""
            try:
                title = await page.title()
            except Exception:
                pass

            text = ""
            try:
                text = await page.evaluate("() => document.body ? (document.body.innerText || '') : ''")
            except Exception:
                try:
                    text = await page.content()
                except Exception:
                    text = ""

            shot_path = ""
            if save_screenshot:
                try:
                    out_dir = profile_dir / "debug"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shot_path = str(out_dir / f"ai_fetch_{_now_ms()}.png")
                    await page.screenshot(path=shot_path, full_page=True)
                except Exception:
                    shot_path = ""

            return {
                "ok": True,
                "final_url": page.url or url,
                "title": title,
                "text": text or "",
                "screenshot": shot_path,
            }
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            except Exception:
                pass


def fetch_page_text(
    *,
    chrome_path: str,
    profile_id: str,
    url: str,
    headless: bool,
    proxy: str = "",
) -> Tuple[bool, Dict[str, Any]]:
    """Run Playwright in a worker thread with profile locking."""
    import asyncio

    url = (url or "").strip()
    if not url:
        return False, {"error": "缺少商品链接"}

    chrome_path = (chrome_path or "").strip()
    if not chrome_path:
        return False, {"error": "缺少 Chrome 路径"}

    prof_dir = _profile_dir(profile_id)

    in_use, reason = detect_chrome_profile_in_use(prof_dir)
    if in_use:
        return False, {"error": reason}

    ok, reason = try_acquire(prof_dir, owner="ai-forwarder")
    if not ok:
        return False, {"error": reason}

    try:
        data = asyncio.run(
            _fetch_page_text_async(
                chrome_path=chrome_path,
                profile_dir=prof_dir,
                url=url,
                headless=bool(headless),
                proxy=proxy,
            )
        )
        return True, data
    except Exception as e:
        return False, {"error": str(e)}
    finally:
        release(prof_dir)


# --------- UI Feature ---------


class AIForwarderFeatureTab:
    """AI 转发客服 Tab — 配置 AI/TG 参数，实际对话通过 TG 自动客服完成。"""

    def __init__(self, app, frame):
        self.app = app
        self.frame = frame

        # 硬编码配置：所有人共用，不从 settings 读取
        self.var_api_key = tk.StringVar(value=_HARDCODED_API_KEY)
        self.var_base_url = tk.StringVar(value=_HARDCODED_BASE_URL)
        self.var_endpoint = tk.StringVar(value=_HARDCODED_ENDPOINT)
        self.var_model = tk.StringVar(value=_HARDCODED_MODEL)
        self.var_redact = tk.BooleanVar(value=bool(self.app.settings.get("ai_redact", True)))
        self.var_headless_fetch = tk.BooleanVar(value=bool(self.app.settings.get("ai_fetch_headless", True)))

        # 指挥（路由决策）
        self.var_commander = tk.BooleanVar(value=bool(self.app.settings.get("ai_commander", True)))
        self.var_commander_model = tk.StringVar(value=str(self.app.settings.get("ai_commander_model", "")))
        self.var_commander_auto_fetch = tk.BooleanVar(value=bool(self.app.settings.get("ai_commander_auto_fetch", True)))

        # Telegram Bot — Token 硬编码，只有 Chat ID 需要用户填
        self.var_tg_token = tk.StringVar(value=_HARDCODED_TG_TOKEN or str(self.app.settings.get("tg_bot_token", "")))
        self.var_tg_chat_id = tk.StringVar(value=str(self.app.settings.get("tg_chat_id", "")))
        self.var_tg_enabled = tk.BooleanVar(value=bool(self.app.settings.get("tg_auto_cs", False)))

        self.var_status = tk.StringVar(value="")

    # --- public ---
    def build(self):
        root = self.frame
        root.columnconfigure(0, weight=1)

        # ===== Settings =====
        lf_cfg = ttk.Labelframe(root, text="AI 设置")
        lf_cfg.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        for c in range(6):
            lf_cfg.columnconfigure(c, weight=1)

        # AI 配置已硬编码，只显示摘要信息
        _model_hint = _HARDCODED_MODEL or "(未设置)"
        ttk.Label(lf_cfg, text=f"AI 模型: {_model_hint}  (已内置，无需修改)").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=4, pady=4)

        ttk.Checkbutton(lf_cfg, text="脱敏后再发给 AI", variable=self.var_redact).grid(row=0, column=4, sticky="w", padx=4, pady=4)
        ttk.Checkbutton(lf_cfg, text="抓商品信息用无头", variable=self.var_headless_fetch).grid(row=0, column=5, sticky="w", padx=4, pady=4)

        # 指挥选项（自动判断下一步）
        optsrow = ttk.Frame(lf_cfg)
        optsrow.grid(row=1, column=0, columnspan=6, sticky="ew", padx=4, pady=(0, 4))
        optsrow.columnconfigure((0, 1, 2, 3), weight=1)

        ttk.Checkbutton(optsrow, text="启用指挥(自动判断)", variable=self.var_commander).grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        ttk.Checkbutton(optsrow, text="需要时自动抓商品信息", variable=self.var_commander_auto_fetch).grid(row=0, column=2, columnspan=2, sticky="w", padx=4, pady=2)

        # Telegram Bot 设置行
        tgrow = ttk.Frame(lf_cfg)
        tgrow.grid(row=2, column=0, columnspan=6, sticky="ew", padx=4, pady=(0, 4))
        tgrow.columnconfigure((2, 4), weight=1)

        ttk.Checkbutton(tgrow, text="TG 自动客服", variable=self.var_tg_enabled).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        # Bot Token：如果已硬编码则隐藏，否则显示让用户填
        if not _HARDCODED_TG_TOKEN:
            ttk.Label(tgrow, text="Bot Token").grid(row=0, column=1, sticky="e", padx=4, pady=2)
            ttk.Entry(tgrow, textvariable=self.var_tg_token, show="*").grid(row=0, column=2, sticky="ew", padx=4, pady=2)
        else:
            ttk.Label(tgrow, text="Bot: 已内置").grid(row=0, column=1, columnspan=2, sticky="w", padx=4, pady=2)
        ttk.Label(tgrow, text="Chat ID").grid(row=0, column=3, sticky="e", padx=4, pady=2)
        ttk.Entry(tgrow, textvariable=self.var_tg_chat_id, width=14).grid(row=0, column=4, sticky="ew", padx=4, pady=2)
        ttk.Button(tgrow, text="测试 TG", command=self._test_tg).grid(row=0, column=5, sticky="ew", padx=4, pady=2)

        btnrow = ttk.Frame(lf_cfg)
        btnrow.grid(row=3, column=0, columnspan=6, sticky="ew", padx=4, pady=(2, 6))
        btnrow.columnconfigure((0, 1), weight=1)

        ttk.Button(btnrow, text="保存设置", command=self._save_settings).grid(row=0, column=0, sticky="ew", padx=4)
        ttk.Button(btnrow, text="测试 API", command=self._test_api).grid(row=0, column=1, sticky="ew", padx=4)

        # status bar
        ttk.Label(root, textvariable=self.var_status).grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))

    # --- helpers ---
    def _log(self, msg: str):
        try:
            self.app.log(f"[AI] {msg}")
        except Exception:
            pass

    def _set_status(self, s: str):
        self.var_status.set(s)

    def _save_settings(self):
        # 硬编码字段：始终写入固定值（确保 settings.json 一致）
        self.app.settings["ai_api_key"] = _HARDCODED_API_KEY
        self.app.settings["ai_base_url"] = _HARDCODED_BASE_URL
        self.app.settings["ai_endpoint_mode"] = _HARDCODED_ENDPOINT
        self.app.settings["ai_model"] = _HARDCODED_MODEL
        # 用户可配置字段
        self.app.settings["ai_redact"] = bool(self.var_redact.get())
        self.app.settings["ai_fetch_headless"] = bool(self.var_headless_fetch.get())
        self.app.settings["ai_commander"] = bool(self.var_commander.get())
        self.app.settings["ai_commander_model"] = ""
        self.app.settings["ai_commander_auto_fetch"] = bool(self.var_commander_auto_fetch.get())
        self.app.settings["tg_bot_token"] = self.var_tg_token.get().strip()
        self.app.settings["tg_chat_id"] = self.var_tg_chat_id.get().strip()
        self.app.settings["tg_auto_cs"] = bool(self.var_tg_enabled.get())
        try:
            save_settings(self.app.settings)
            self._set_status("已保存设置")
            self._log("已保存设置")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _test_tg(self):
        """测试 TG Bot 连通性：发送 getMe 验证 Token，如果已有 Chat ID 则发一条测试消息。"""
        import requests as _req
        token = self.var_tg_token.get().strip()
        if not token:
            messagebox.showwarning("测试 TG", "请先填写 Bot Token")
            return

        self._set_status("测试 TG Bot 中...")
        self._log("[TG] 测试连接...")

        # 1) getMe 验证 Token
        try:
            r = _req.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=15,
            )
            data = r.json()
        except Exception as e:
            messagebox.showerror("测试 TG", f"网络请求失败：{e}")
            self._set_status("TG 测试失败")
            return

        if not data.get("ok"):
            desc = data.get("description", "未知错误")
            messagebox.showerror("测试 TG", f"Token 无效：{desc}")
            self._set_status("TG Token 无效")
            return

        bot_name = data["result"].get("username", "?")
        self._log(f"[TG] Bot 验证成功: @{bot_name}")

        # 2) 如果已有 Chat ID，发一条测试消息
        chat_id = self.var_tg_chat_id.get().strip()
        if chat_id:
            try:
                r2 = _req.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": "测试成功！TG Bot 已连通。"},
                    timeout=15,
                )
                d2 = r2.json()
                if d2.get("ok"):
                    messagebox.showinfo("测试 TG", f"Bot @{bot_name} 连接正常，测试消息已发送。")
                    self._set_status("TG 测试成功")
                else:
                    messagebox.showwarning("测试 TG",
                        f"Bot @{bot_name} Token 有效，但发送消息失败：\n{d2.get('description','')}\n\n"
                        f"请在 TG 中给 Bot 发 /start 后重试。")
                    self._set_status("TG 发送失败")
            except Exception as e:
                messagebox.showwarning("测试 TG", f"Bot 验证成功但发送失败：{e}")
                self._set_status("TG 发送失败")
            return

        # 3) 没有 Chat ID：启动临时轮询等待 /start
        messagebox.showinfo("测试 TG",
            f"Bot @{bot_name} Token 有效！\n\n"
            f"请现在去 TG 给 @{bot_name} 发送 /start\n"
            f"然后点确定，程序将等待绑定 Chat ID。")
        self._set_status("等待 TG /start ...")
        self._log("[TG] 等待用户发送 /start ...")

        self._tg_poll_for_start(token, bot_name)

    def _tg_poll_for_start(self, token: str, bot_name: str):
        """后台线程轮询等待 /start，绑定 Chat ID。"""
        import requests as _req
        import threading

        def _poll():
            offset = 0
            for _ in range(20):  # 最多等 ~60 秒
                try:
                    r = _req.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"offset": offset, "timeout": 3},
                        timeout=15,
                    )
                    data = r.json()
                    for upd in data.get("result", []):
                        offset = max(offset, upd["update_id"] + 1)
                        msg = upd.get("message", {})
                        text = (msg.get("text") or "").strip()
                        if text == "/start":
                            cid = str(msg.get("chat", {}).get("id", ""))
                            if cid:
                                # 保存 Chat ID
                                self.var_tg_chat_id.set(cid)
                                self.app.settings["tg_chat_id"] = cid
                                try:
                                    save_settings(self.app.settings)
                                except Exception:
                                    pass
                                # 发送确认消息
                                name = msg.get("from", {}).get("first_name", "")
                                try:
                                    _req.post(
                                        f"https://api.telegram.org/bot{token}/sendMessage",
                                        json={"chat_id": cid,
                                              "text": f"已绑定！Chat ID: {cid}\n用户: {name}"},
                                        timeout=10,
                                    )
                                except Exception:
                                    pass
                                self._log(f"[TG] 绑定成功 chat_id={cid}")
                                self.app.after(0, lambda: self._set_status(f"TG 绑定成功 Chat ID={cid}"))
                                self.app.after(0, lambda: messagebox.showinfo("TG 绑定",
                                    f"绑定成功！\nChat ID: {cid}\n\n"
                                    f"现在勾选「TG 自动客服」并保存设置，\n"
                                    f"开始监控后 TG Bot 将自动运行。"))
                                return
                except Exception:
                    pass
            # 超时
            self._log("[TG] 等待 /start 超时")
            self.app.after(0, lambda: self._set_status("TG 等待超时"))
            self.app.after(0, lambda: messagebox.showwarning("TG 绑定",
                "等待超时（60秒），未收到 /start。\n请确认你给正确的 Bot 发了 /start。"))

        threading.Thread(target=_poll, daemon=True).start()


    # --- actions ---
    def _test_api(self):
        self._set_status("测试 API...")

        def worker():
            ok, txt = call_openai(
                api_key=self.var_api_key.get(),
                base_url=self.var_base_url.get(),
                endpoint_mode=self.var_endpoint.get(),
                model=self.var_model.get(),
                system_prompt="你是一个助手。",
                user_prompt="回复 'ok' 即可。",
                timeout_sec=40,
            )
            self.app.after(0, lambda: self._on_api_test_done(ok, txt))

        threading.Thread(target=worker, daemon=True).start()

    def _on_api_test_done(self, ok: bool, txt: str):
        if ok:
            self._set_status("API 正常")
            self._log("API 测试成功")
            messagebox.showinfo("API", f"成功：{_short(txt, 240)}")
        else:
            self._set_status("API 失败")
            self._log(f"API 测试失败：{txt}")
            messagebox.showerror("API", txt)

