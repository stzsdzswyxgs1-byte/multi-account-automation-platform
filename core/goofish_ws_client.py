"""閒魚 WebSocket IM 客戶端(v6.0.75)— 純 Python,完全不開瀏覽器。

協議:阿里 LWP (Lightweight Protocol) over WebSocket,純 JSON
URL:wss://wss-goofish.dingtalk.com:443

支援功能:
- 發送文字訊息 (send_text)
- 接收賣家訊息推送 (objectType=40000)
- 接收已讀回執 (objectType=40103)
- 精確 AI 自動回覆判定 (intellectTags / quickReply)
- 心跳 + 自動重連 + 多對話 dispatcher

執行模型:
- 單例 WS 客戶端,在後台 thread 內跑 asyncio event loop
- 主 thread 通過 thread-safe API 提交 send / 註冊 callback
- 連線斷掉自動重連(指數退避)
"""
from __future__ import annotations
import asyncio
import base64
import json
import random
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError as _e:
    raise ImportError(
        "需要 websockets 套件: pip install websockets"
    ) from _e


# v6.1.35:跨版本 websockets .closed 相容檢查
# - websockets < 12: WebSocketClientProtocol 有 .closed bool property
# - websockets >= 12: ClientConnection 改用 .state(沒 .closed),
#                    狀態變數對應 protocol.State.CLOSED
# Python 3.12.7 bundle 帶的 websockets 是 12+,直接 .closed 會炸:
#   AttributeError: 'ClientConnection' object has no attribute 'closed'
# 修「閒魚自動問賣家 WS 建立對話失敗」bug
def _ws_is_closed(ws) -> bool:
    """跨版本檢查 websockets 連線是否已關閉,失敗時保守當已關閉。"""
    if ws is None:
        return True
    # websockets >= 12 新 API:檢查 .state
    try:
        _state = getattr(ws, "state", None)
        if _state is not None:
            try:
                from websockets.protocol import State as _WSState
                return _state == _WSState.CLOSED
            except Exception:
                # 文字比對 fallback:State enum 的 repr 含 "CLOSED"
                return "CLOSED" in str(_state)
    except Exception:
        pass
    # websockets < 12 舊 API:.closed bool property
    try:
        return bool(getattr(ws, "closed", True))
    except Exception:
        return True


# ─────────────────────────── 常量 ───────────────────────────

WS_URL = "wss://wss-goofish.dingtalk.com:443"
WS_APP_KEY = "<XIANYU_WS_APP_KEY_REDACTED>"
WS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 "
    "DingTalk(2.2.0) OS(Windows/10) Browser(Chrome/147.0.0.0) "
    "DingWeb/2.2.0 IMPaaS DingWeb/2.2.0"
)
HEARTBEAT_INTERVAL = 15
RECONNECT_BACKOFF = (5, 10, 20, 40, 60)


# ─────────────────────────── 工具函數 ───────────────────────────

def make_mid(seq: int = 0) -> str:
    """生成 mid: f"{rand 2-3 digit}{ts_ms} {seq}"."""
    rand = random.randint(10, 999)
    ts_ms = int(time.time() * 1000)
    return f"{rand}{ts_ms} {seq}"


def make_uuid(serial: int = 0) -> str:
    """生成客戶端 uuid: f"-{ts_ms*10+serial}"."""
    ts_ms = int(time.time() * 1000)
    return f"-{ts_ms * 10 + serial}"


def encode_text_content(text: str) -> str:
    """編碼 send body 內 content.custom.data (base64 of inner JSON)。
    使用緊湊 JSON 格式(無空格)跟瀏覽器抓包完全一致。
    """
    inner = json.dumps(
        {"contentType": 1, "text": {"text": text}},
        ensure_ascii=False,
        separators=(",", ":"),  # 緊湊版,無空格
    )
    return base64.b64encode(inner.encode("utf-8")).decode("ascii")


# ─────────────────────────── inbound 解析 ───────────────────────────

class InboundMessage:
    """objectType=40000 或 40006 訊息結構。"""
    __slots__ = (
        "content_text", "content_type", "message_id", "sender_uid",
        "sender_type", "cid", "quick_reply", "intellect_tags",
        "is_auto_reply", "platform", "raw_text",
        "object_type", "is_session_event",  # v6.0.75 新加
        "is_official_tip",  # v6.0.77 新加:閒魚官方系統提示卡片(驗貨寶/安全提示等)
        "created_ts",  # v6.0.78 新加:訊息建立時間(毫秒),用於 baseline_ts 過濾
    )

    def __init__(self):
        self.content_text = ""
        self.content_type = 0
        self.message_id = ""
        self.sender_uid = ""
        self.sender_type = "0"
        self.cid = ""
        self.quick_reply = False
        self.intellect_tags: List[str] = []
        self.is_auto_reply = False  # 賣家 AI 自動回覆精確判定
        self.platform = ""
        self.raw_text = ""
        # v6.0.75:objectType=40006 是輕量 session event (typing/state change),
        # 不含訊息正文,只當 "有新事件" 信號,上層觸發 HTTP session.sync 拉正文
        self.object_type = 0
        self.is_session_event = False
        # v6.0.77:閒魚官方系統提示卡片(驗貨寶/先驗後買/平台提示等)
        # 這類訊息不是賣家發的,不應推送 TG 也不應觸發整合
        self.is_official_tip = False
        # v6.0.78:訊息建立時間戳(毫秒),HTTP 補漏拉時用於和 baseline_ts 比較
        # 防止把「發送提問前的舊訊息」誤判為新訊息
        self.created_ts = 0

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


class ReadReceipt:
    """objectType=40103 已讀回執。"""
    __slots__ = ("read_message_ids", "cid", "ts")

    def __init__(self):
        self.read_message_ids: List[str] = []
        self.cid = ""
        self.ts = 0


def parse_inbound_message(b64_data: str) -> Optional[InboundMessage]:
    """從 inbound /s/sync push 的 data (base64) 解析訊息內容。

    不用解 protobuf — 直接 regex 抓 ASCII 區段(可靠且簡單)。
    """
    if not b64_data:
        return None
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return None
    text = raw.decode("utf-8", errors="ignore")

    msg = InboundMessage()
    msg.raw_text = text[:500]  # 留 raw 給診斷用

    # 1. 訊息正文(關鍵)
    m = re.search(r'\{"atUsers":\[[^\]]*\],"contentType":(\d+),"text":\{[^}]*"text":"([^"]*)"[^}]*\}\}', text)
    if m:
        try:
            msg.content_type = int(m.group(1))
            msg.content_text = m.group(2)
        except Exception:
            pass

    # v6.1.51:WS 直推路徑沒解析圖片/視頻 URL → 落到「非文字訊息」分支切手動,
    # AI 整合時也看不到內容。補上跟 parse_user_message_model 對齊的圖片/視頻 URL 抓取。
    # 修「賣家發圖片/視頻被當無內容訊息」bug
    if not msg.content_text:
        # 圖片:image.pics[].url 或 origin.url
        m_img = re.search(r'"image"[^}]*?"pics"\s*:\s*\[[^\]]*?"url"\s*:\s*"([^"]+)"', text)
        if m_img:
            msg.content_text = f"[圖片] {m_img.group(1)}"
            msg.content_type = msg.content_type or 2
        else:
            # 視頻:resizeVideos[].url 或 video.src.url
            m_vid = re.search(r'"resizeVideos"\s*:\s*\[[^\]]*?"url"\s*:\s*"([^"]+\.mp4[^"]*)"', text)
            if not m_vid:
                m_vid = re.search(r'"video"[^}]*?"src"[^}]*?"url"\s*:\s*"([^"]+)"', text)
            if m_vid:
                msg.content_text = f"[視頻] {m_vid.group(1)}"
                msg.content_type = msg.content_type or 4
            else:
                # 貼圖:sticker.url
                m_st = re.search(r'"sticker"[^}]*?"url"\s*:\s*"([^"]+)"', text)
                if m_st:
                    msg.content_text = f"[貼圖] {m_st.group(1)}"
                    msg.content_type = msg.content_type or 5

    # 2. messageId
    m = re.search(r'(\d{10,16}\.PNM)', text)
    if m:
        msg.message_id = m.group(1)

    # v6.0.79:解析 createAt 時間戳(13 位毫秒),用於 baseline_ts 過濾
    # 之前 LWP 路徑漏解析,造成「自己發送的訊息 echo」過不了 baseline 過濾
    m = re.search(r'createAt[^\d]*(\d{13})', text)
    if m:
        try:
            msg.created_ts = int(m.group(1))
        except Exception:
            pass

    # 3. 發送方 userId
    m = re.search(r'senderUserId.{0,5}(\d{10,16})', text)
    if m:
        msg.sender_uid = m.group(1)

    # 4. 發送方類型
    m = re.search(r'senderUserType.{0,3}(\d+)', text)
    if m:
        msg.sender_type = m.group(1)

    # 5. cid (sessionId@goofish)
    m = re.search(r'(\d{8,12}@goofish)', text)
    if m:
        msg.cid = m.group(1)

    # 6. AI 訊號 — 精確判定
    # intellectTags 可能在 extJson 內(嵌套 JSON 字串),需要兩種匹配
    m_tags = re.search(r'intellectTags[^\[]*\[([^\]]+)\]', text)
    if m_tags:
        tag_str = m_tags.group(1)
        # 簡單 split,因為標籤是字串 list
        msg.intellect_tags = [t.strip(' "\'') for t in tag_str.split(",") if t.strip(' "\'')]
    m_qr = re.search(r'quickReply[^\d]*"?(\d)', text)
    if m_qr:
        msg.quick_reply = (m_qr.group(1) == "1")

    # v6.1.50:抓 extension.bizTag(server 端精確標識,跟 parse_user_message_model 對齊)
    # 真人/普通訊息: {"sourceId":"S:1","messageId":"..."}
    # 智能回復:     {"sourceId":"IM:XXX","taskName":"不在线自动回复2.0_买家","taskId":"XXX"}
    # 實機驗證:taskName / taskId 存在 → 100% 業務任務自動觸發,不是真人手打
    _is_biz_task_reply = False
    _biz_task_name = ""
    m_biztag = re.search(r'bizTag[^{]*\{([^}]+)\}', text)
    if m_biztag:
        biz_blob = m_biztag.group(1)
        # 簡單 regex 抓 taskName / taskId / sourceId
        _mtn = re.search(r'taskName["\s:]+([^",}]+)', biz_blob)
        if _mtn:
            _biz_task_name = _mtn.group(1).strip(' "\'')
            if _biz_task_name:
                _is_biz_task_reply = True
        _mti = re.search(r'taskId["\s:]+([^",}]+)', biz_blob)
        if _mti and _mti.group(1).strip(' "\''):
            _is_biz_task_reply = True
        _msi = re.search(r'sourceId["\s:]+"?(IM:[^",}\s]+)', biz_blob)
        if _msi:
            _is_biz_task_reply = True

    msg.is_auto_reply = (
        _is_biz_task_reply                           # v6.1.50 最可靠的 server 標識
        or "openOfflineReplyConfig" in msg.intellect_tags
        or msg.quick_reply
    )
    if _is_biz_task_reply:
        msg.raw_text = (msg.raw_text or "") + f" [bizTag_task='{_biz_task_name[:40]}']"

    # 7. platform
    m = re.search(r'_platform.{0,2}([a-z]+)', text)
    if m:
        msg.platform = m.group(1)

    # 沒抓到正文 → 視為無效訊息
    if not msg.content_text and not msg.message_id:
        return None
    return msg


def parse_user_message_model(item: Dict) -> Optional[InboundMessage]:
    """v6.0.75:解析 /r/MessageManager/listUserMessages 返回的單條訊息。

    結構:
    {
      "message": {
        "messageId": "4108237320615.PNM",
        "content": {"contentType": 101, "custom": {"type": 1, "data": "<base64>"}},
        "extension": {
          "senderUserId": "<userId>",
          "senderUserType": "0",
          "extJson": '{"quickReply":"1","msgAttachedTip":"卖家不在线，AI正在回复你",...}',
          "sessionType": "1",
          "_platform": "web|ios|android"
        }
      },
      "readStatus": 0,
      ...
    }
    """
    if not isinstance(item, dict):
        return None
    message = item.get("message", {}) or {}
    if not message:
        return None

    msg = InboundMessage()
    msg.object_type = 40000  # 視為「完整訊息」型

    msg.message_id = str(message.get("messageId", ""))
    # v6.0.78:訊息建立時間戳(毫秒),用於 baseline_ts 過濾
    try:
        msg.created_ts = int(message.get("createAt", 0) or 0)
    except Exception:
        msg.created_ts = 0
    content = message.get("content", {}) or {}
    custom = content.get("custom", {}) or {}
    msg.content_type = int(content.get("contentType", 0) or 0)

    # 1) 解碼正文 (custom.data base64) + v6.0.77 識別閒魚官方系統提示卡片
    data_b64 = str(custom.get("data", "") or "")
    custom_type = 0
    try:
        custom_type = int(custom.get("type", 0) or 0)
    except Exception:
        pass
    if data_b64:
        try:
            raw = base64.b64decode(data_b64)
            inner = raw.decode("utf-8", errors="ignore")
            msg.raw_text = inner[:400]

            # v6.0.80:完整 contentType schema 解析(适配自實機抓取)
            # 閒魚 IM 訊息類型:
            #   1=文字  2=圖片  3=語音  4=視頻
            #   6=textCard(系統HTML卡)  7=itemCard(商品卡)  14=tip(系統提示)
            #   25/26/32=dxCard(訂單動態卡)
            # 不同類型有不同結構,先看 inner.contentType,再用對應 schema
            inner_ct_m = re.search(r'"contentType"\s*:\s*(\d+)', inner)
            inner_ct = int(inner_ct_m.group(1)) if inner_ct_m else 0

            # 1) 普通文字訊息:{"text":{"text":"<actual>"}}
            text_m = re.search(r'"text"\s*:\s*\{"text"\s*:\s*"([^"]*)"', inner)
            if text_m and text_m.group(1):
                msg.content_text = text_m.group(1)

            # 2) 各類媒體/卡片訊息 — 沒抓到 text 才看
            if not msg.content_text:
                if inner_ct == 2:
                    # 圖片:{"image":{"pics":[{"url":"https://img.alicdn.com/..."}]}}
                    pic_m = re.search(r'"image"[^}]*?"pics"\s*:\s*\[[^}]*?"url"\s*:\s*"([^"]+)"', inner)
                    if pic_m:
                        msg.content_text = f"[圖片] {pic_m.group(1)}"
                        msg.raw_text = inner[:400] + " [PARSED:image]"

                elif inner_ct == 3:
                    # 語音:推測結構 {"audio":{"url":"...","duration":N}} 或含 asrText/voiceText
                    # 優先取 server STT (asrText/voiceText) — 閒魚可能 server 端轉文字
                    stt_m = re.search(r'"(?:asrText|voiceText|audioText|transcribeText)"\s*:\s*"([^"]+)"', inner)
                    if stt_m:
                        msg.content_text = f"[語音→文字] {stt_m.group(1)}"
                        msg.raw_text = inner[:400] + " [PARSED:audio+stt]"
                    else:
                        aud_m = re.search(r'"audio"[^}]*?"url"\s*:\s*"([^"]+)"', inner)
                        if not aud_m:
                            aud_m = re.search(r'"(?:audioUrl|voiceUrl)"\s*:\s*"([^"]+)"', inner)
                        if aud_m:
                            dur_m = re.search(r'"duration"\s*:\s*(\d+)', inner)
                            dur = f" ({dur_m.group(1)}秒)" if dur_m else ""
                            msg.content_text = f"[語音{dur}] {aud_m.group(1)}"
                            msg.raw_text = inner[:400] + " [PARSED:audio]"

                elif inner_ct == 4:
                    # 視頻:{"video":{"url":"...","snapshot":"...","duration":N}}
                    vid_url_m = re.search(r'"video"[^}]*?"url"\s*:\s*"([^"]+)"', inner)
                    snap_m = re.search(r'"snapshot"\s*:\s*"([^"]+)"', inner)
                    if vid_url_m:
                        snap_part = f" 封面:{snap_m.group(1)}" if snap_m else ""
                        msg.content_text = f"[視頻] {vid_url_m.group(1)}{snap_part}"
                        msg.raw_text = inner[:400] + " [PARSED:video]"

                elif inner_ct == 7:
                    # 商品卡:{"itemCard":{"item":{"itemId":...,"title":"...","price":"..."}}}
                    title_m = re.search(r'"title"\s*:\s*"([^"]+)"', inner)
                    price_m = re.search(r'"price"\s*:\s*"([^"]+)"', inner)
                    itid_m = re.search(r'"itemId"\s*:\s*(\d+)', inner)
                    if title_m:
                        price_part = f" ¥{price_m.group(1)}" if price_m else ""
                        id_part = f" itemId={itid_m.group(1)}" if itid_m else ""
                        msg.content_text = f"[商品卡] {title_m.group(1)}{price_part}{id_part}"
                        msg.raw_text = inner[:400] + " [PARSED:itemCard]"

                elif inner_ct in (6, 14):
                    # 系統提示文字卡(物流/訂單狀態等)— 一般不影響 AI 判斷,但要識別
                    tip_m = re.search(r'"tip"\s*:\s*"([^"]+)"', inner)
                    if tip_m:
                        msg.content_text = f"[系統] {tip_m.group(1)}"
                        msg.raw_text = inner[:400] + " [PARSED:tip]"

                elif inner_ct in (25, 26, 32):
                    # 動態卡片(訂單交易流程提示,如「快評價」「等發貨」)
                    # 這些卡片 AI 不該回覆,標記為系統訊息
                    title_m = re.search(r'"title"\s*:\s*"([^"]+)"', inner)
                    if title_m:
                        msg.content_text = f"[訂單系統卡] {title_m.group(1)}"
                        msg.raw_text = inner[:400] + " [PARSED:dxCard]"

            # v6.0.77:識別閒魚官方系統提示(非用戶/賣家訊息)
            # 例:「可要求賣家走「驗貨寶」,先驗後買,查看介紹」
            # 平台會偽裝 senderUserType="0"(看起來像賣家發),所以光靠 sender_type 過濾不掉
            #
            # 雙層判斷防誤殺(用戶/賣家可能也會說「驗貨寶」3 個字):
            # 1. 結構字段名命中(強信號,普通訊息 JSON 沒這些字段)→ 確定是系統卡片
            # 2. 文案關鍵字命中 **且** content_text 為空(不是普通 text:text 結構)→ 系統卡片
            #    若 content_text 有值,代表是用戶/賣家在訊息中提到關鍵字,不是系統卡片
            STRUCT_FIELDS = (
                "tipType", "officialTip", "safeTip", "systemTip",
                "buttonText", "linkUrl", "actionUrl",
            )
            TEXT_KEYS = (
                "验货宝", "驗貨寶",
                "先验后买", "先驗後買",
                "查看介绍", "查看介紹",
                "官方平台", "保障服务", "保障服務",
                "系统提示", "系統提示", "平台提示", "平台公告",
                "安全提示", "風險提示", "风险提示",
            )
            # Layer 1:結構字段(強信號,不會誤殺)
            if any(f in inner for f in STRUCT_FIELDS):
                msg.is_official_tip = True
            # Layer 2:文案關鍵字 + 抓不到 content_text(系統卡片走特殊渲染)
            elif not msg.content_text and any(k in inner for k in TEXT_KEYS):
                msg.is_official_tip = True
        except Exception:
            pass

    # 2) extension 內取 sender + 平台 + AI 判定
    ext = message.get("extension", {}) or {}
    msg.sender_uid = str(ext.get("senderUserId", "") or "")
    msg.sender_type = str(ext.get("senderUserType", "0") or "0")
    msg.platform = str(ext.get("_platform", "") or "")

    ai_signals = []

    # v6.1.50 Layer 0(最可靠):extension.bizTag 是 server 端 per-訊息業務標識
    # 实机适配(2026-05-22)PC 閒魚 React fiber 抓到的 3 條訊息對比:
    #   - 我們發的訊息    : {"sourceId":"S:1", "messageId":"..."}
    #   - 智能回復(離線) : {"sourceId":"IM:XXX", "taskName":"不在线自动回复2.0_买家",
    #                       "taskId":"XXX", "materialId":"XXX"}
    #   - 真人手動回覆     : {"sourceId":"S:1", "messageId":"..."}
    # 結論:taskName / taskId 存在,或 sourceId 用 "IM:" prefix → 業務任務自動觸發
    # 這是 server 端的精確標識,完全不依賴文字匹配,100% 可靠
    ext_biz_tag_str = ext.get("bizTag", "") or ""
    if ext_biz_tag_str:
        try:
            import json as _json
            ebt = _json.loads(ext_biz_tag_str)
            _task_name = str(ebt.get("taskName", "") or "")
            _task_id = str(ebt.get("taskId", "") or "")
            _source_id = str(ebt.get("sourceId", "") or "")
            if _task_name or _task_id:
                ai_signals.append(f"bizTag_taskName='{_task_name[:50]}'")
            elif _source_id.startswith("IM:"):
                # sourceId 用 IM: 開頭(非 S:),也是業務任務自動觸發
                ai_signals.append(f"bizTag_sourceId='{_source_id[:40]}'")
        except Exception:
            pass

    # 3) extJson 內取 AI 信號(精確判定 + 啟發式 fallback)
    # v6.0.75 修正:之前誤判真人回覆為 AI(因為賣家開啟 AI 託管後,
    # intellectTags / userAIMarker 是「per 對話」的配置 → 所有訊息都帶,真人也誤判)
    # 唯一可靠的「per 訊息」AI 標識是 msgAttachedTip 內含「AI正在回复」這類動態字串
    ext_json_str = ext.get("extJson", "")
    if ext_json_str:
        try:
            import json as _json
            ej = _json.loads(ext_json_str)
            qr = str(ej.get("quickReply", "") or "")
            tip = str(ej.get("msgAttachedTip", "") or "")
            msg.quick_reply = (qr == "1")

            # Layer 1:只信 msgAttachedTip 含「AI正在回复」這種動態字串
            # (這個 tip 是 server per-訊息設的,只有真 AI 自動回覆才帶)
            tip_lower = tip.lower()
            ai_tip_keywords = (
                "ai正在回复", "ai 正在回复", "ai正在回覆",
                "智能回复", "智能回覆", "智能助理",
                "自动回复您", "自動回覆您", "自动回复你", "自動回覆你",
            )
            if any(k in tip for k in ai_tip_keywords) or any(k in tip_lower for k in ai_tip_keywords):
                ai_signals.append(f"msgAttachedTip='{tip[:40]}'")

            # 保留 intellect_tags 字段方便診斷,但不用於 AI 判定
            tags = ej.get("intellectTags", [])
            if isinstance(tags, list):
                msg.intellect_tags = [str(t) for t in tags]
        except Exception:
            pass

    # Layer 2 啟發式 fallback (對方訊息 + quickReply=1 + 內含 template 關鍵字)
    # 真人快回「你好/在/好的/可以」這種短句 quickReply 不算 AI
    # 但長 template(離線設定 / 店鋪政策模板)有典型 template 用語
    if not ai_signals and msg.quick_reply and msg.content_text and len(msg.content_text) >= 10:
        TEMPLATE_PHRASES = (
            # ── 離線回覆類(老的) ──
            "我现在不在", "我現在不在", "我不在线", "我不在線",
            "我现在不在线", "我現在不在線",
            "稍后回复", "稍後回覆", "稍后回您", "稍後回您",
            "会尽快回复", "會盡快回覆", "尽快回您", "盡快回您",
            "有问题留言", "有問題留言", "留言哦",
            "感谢你这么酷", "感謝你這麼酷",
            "请稍等", "請稍等",
            "亲，我", "親,我",
            "看到会回复", "看到會回覆",
            "AI回复", "AI回覆", "AI正在",
            "智能客服", "智能小蜜",
            # ── v6.1.50:店鋪政策模板類(新) ──
            # 修「店鋪政策快捷回覆被誤判為真人」bug
            # 用戶實測 case:「能直接購買的商品都在,不二價,本店均不還價,保真包老...」
            # 賣家點"快捷回覆"按鈕發出店鋪政策模板,mobile 顯示「智能回復」badge
            # 但 PC 不顯示 badge — server 是用 quickReply=1 標的,我們關鍵字沒涵蓋
            "不二价", "不二價",
            "不还价", "不還價",
            "本店均", "本店不",
            "保真包老",
            "全场包邮", "全場包郵",
            "全店包邮", "全店包郵",
            "可回收",
            "还价绕路", "還價繞路",
            "没赠品", "沒贈品",
            "不送东西", "不送東西",
            "售出不退", "售出不換",
            "不退不换", "不退不換",
            "先验货", "先驗貨",
            "在签收", "在簽收",
            "签收前", "簽收前",
            "签收后", "簽收後",
            "拒收的话", "拒收的話",
            "+先验货", "+先驗貨",
            "本店均不",
            "友情提示",
            "包真包老",
        )
        hit = [p for p in TEMPLATE_PHRASES if p in msg.content_text]
        if hit:
            ai_signals.append(f"heuristic_template={hit[:2]}")

    # v6.1.50 Layer 3:quickReply=1 + 訊息長度 >= 50 → 幾乎肯定是模板
    # 真人「快捷回覆」短句一般 < 30 chars(「好的」「在」「可以」「不還價喔」)
    # 50+ chars 的 quickReply 必然是預設的模板/店鋪政策
    # 修「店鋪政策模板未匹配關鍵字也被誤判為真人」case(防止未來新模板)
    if not ai_signals and msg.quick_reply and msg.content_text and len(msg.content_text) >= 50:
        ai_signals.append(f"heuristic_long_quickreply={len(msg.content_text)}chars")

    msg.is_auto_reply = bool(ai_signals)
    if ai_signals:
        msg.raw_text = (msg.raw_text or "") + f" [AI_signals={ai_signals}]"
    else:
        # v6.1.50:沒匹配上 AI 信號的賣家訊息 — 把關鍵 extJson 欄位寫入 raw_text
        # 供日後追蹤「真人 vs AI 誤判」case(用戶反饋時可以看 raw 數據)
        # extJson 完整保留前 200 chars + intellectTags 列表
        if ext_json_str:
            try:
                _diag_tags = msg.intellect_tags[:5] if msg.intellect_tags else []
                _diag_tip = ""
                try:
                    import json as _json_diag
                    _ej_diag = _json_diag.loads(ext_json_str)
                    _diag_tip = str(_ej_diag.get("msgAttachedTip", "") or "")
                except Exception:
                    pass
                msg.raw_text = (msg.raw_text or "") + (
                    f" [NO_AI_diag qr={msg.quick_reply} "
                    f"len={len(msg.content_text)} "
                    f"tags={_diag_tags} "
                    f"tip='{_diag_tip[:40]}']"
                )
            except Exception:
                pass

    if not msg.content_text and not msg.message_id:
        return None
    return msg


def parse_session_event(b64_data: str) -> Optional[InboundMessage]:
    """v6.0.75:解析 objectType=40006 輕量 session event。
    結構(49 bytes):'\\x01\\x01' + cid + '\\x02\\x01\\x03[\\x00|\\x01]\\x04' + my_uid@goofish
    我們只抽出 cid 當 trigger,正文走 HTTP 拉。
    """
    if not b64_data:
        return None
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return None
    text = raw.decode("utf-8", errors="ignore")
    m = re.search(r'(\d{8,12}@goofish)', text)
    if not m:
        return None
    msg = InboundMessage()
    msg.object_type = 40006
    msg.is_session_event = True
    msg.cid = m.group(1)
    msg.raw_text = text[:200]
    return msg


def parse_read_receipt(b64_data: str) -> Optional[ReadReceipt]:
    """從 inbound /s/sync push 解析已讀回執 (objectType=40103)。"""
    if not b64_data:
        return None
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return None
    text = raw.decode("utf-8", errors="ignore")

    rr = ReadReceipt()
    rr.read_message_ids = re.findall(r'(\d{10,16}\.PNM)', text)
    m = re.search(r'(\d{8,12}@goofish)', text)
    if m:
        rr.cid = m.group(1)
    rr.ts = int(time.time() * 1000)
    if not rr.read_message_ids and not rr.cid:
        return None
    return rr


# ─────────────────────────── 主類別 ───────────────────────────

class XianyuWsClient:
    """閒魚 WebSocket 客戶端(單例,跑在後台 thread)。"""

    _instance: Optional["XianyuWsClient"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, profile_dir: Path, on_log: Callable[[str], None]) -> "XianyuWsClient":
        with cls._instance_lock:
            if cls._instance is None or cls._instance._stopped:
                cls._instance = cls(profile_dir, on_log)
            return cls._instance

    def __init__(self, profile_dir: Path, on_log: Callable[[str], None]):
        self.profile_dir = Path(profile_dir)
        self.on_log = on_log

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[WebSocketClientProtocol] = None

        self._mid_seq = 0
        self._uuid_serial = 0

        self._stopped = False
        self._connected = threading.Event()

        # callbacks:
        # on_inbound_msg(InboundMessage) — 收到對方訊息
        # on_read_receipt(ReadReceipt) — 收到已讀回執
        self._on_inbound_msg: Optional[Callable[[InboundMessage], None]] = None
        self._on_read_receipt: Optional[Callable[[ReadReceipt], None]] = None

        # 重連狀態
        self._reconnect_count = 0
        self._access_token = ""
        self._device_id = ""
        self._my_user_id = ""

        # v6.0.75:send ACK 等待表 (mid → asyncio.Future)
        # 用於 send_text_coro 等 server 回 code=200 same mid 才返回 ok=True
        self._pending_acks: Dict[str, asyncio.Future] = {}
        # token 401 標記:重連時 force_refresh token
        self._token_invalid = False
        # v6.0.75 production:401 後等多久才嘗試 refresh(避免踢瀏覽器 token)
        self._token_invalid_wait_until: float = 0.0
        # 不可用 callback (token 失效時切 HTTP fallback)
        self._on_unavailable_callback: Optional[Callable[[str], None]] = None
        # v6.0.79:最後一次連線失敗的詳細錯誤(讓上層拿到具體原因,如 RGV587)
        self.last_connect_error: str = ""

    # ── 公開介面 ──

    def set_callbacks(
        self,
        on_inbound_msg: Optional[Callable[[InboundMessage], None]] = None,
        on_read_receipt: Optional[Callable[[ReadReceipt], None]] = None,
        on_unavailable: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_inbound_msg = on_inbound_msg
        self._on_read_receipt = on_read_receipt
        # v6.0.75:on_unavailable("reason") — token 失效時通知上層切 HTTP fallback
        self._on_unavailable_callback = on_unavailable

    def start(self, wait_ready: bool = True, timeout: float = 20.0) -> bool:
        """啟動後台 event loop 並建立 WS 連線。

        v6.0.75:thread 已 alive 但 _connected 沒 set 時(重連中),wait_ready 仍要等。
        否則 caller 收到 True 就去 send,實際連線未就緒 → 失敗。
        """
        if self._thread and self._thread.is_alive():
            if wait_ready:
                # thread 在跑(可能重連中),也要等 _connected 就緒
                return self._connected.wait(timeout=timeout)
            return True

        self._stopped = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="XianyuWS")
        self._thread.start()

        if wait_ready:
            return self._connected.wait(timeout=timeout)
        return True

    def stop(self) -> None:
        self._stopped = True
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def force_reconnect(self) -> None:
        """v6.1:強制斷開現有 ws,觸發主循環走重連分支。
        watchdog 偵測到「ws.is_connected()=True 但 list 連續超時」時呼叫,救 stale 連線。"""
        if not self._loop:
            return
        try:
            self._connected.clear()
            if self._ws and not _ws_is_closed(self._ws):
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        except Exception:
            pass

    def list_user_messages_sync(
        self,
        cid: str,
        limit: int = 20,
        timeout: float = 10.0,
        cursor: int = 9223372036854775807,
    ) -> Tuple[List[Dict], str]:
        """v6.0.75 呼叫 WS `/r/MessageManager/listUserMessages` 拉訊息歷史。

        body: [cid, false, cursor_ts, limit, false]
        cursor 是 timestamp(毫秒),從這個 ts 開始往「更舊」方向拉 limit 條;
        傳 MAX_INT 表示「從最新開始」。

        Returns: (messages_list, error_msg) — messages 是新→舊順序
        """
        if not self._connected.is_set() or not self._loop:
            return [], "WS 未連線"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._list_user_messages_coro(cid, limit, cursor),
                self._loop,
            )
            return fut.result(timeout=timeout)
        except Exception as e:
            return [], f"list_user_messages 異常: {e}"

    def list_all_user_messages_sync(
        self,
        cid: str,
        max_total: int = 500,
        page_size: int = 50,
        per_page_timeout: float = 10.0,
    ) -> Tuple[List[Dict], str]:
        """v6.0.75 分頁拉「整段對話歷史」直到沒更多或達到 max_total 上限。

        分頁邏輯:每頁用上一頁最舊一條的 createAt 當 cursor 往下拉。

        Returns: (all_messages, error_msg) — 新→舊順序
        """
        all_msgs: List[Dict] = []
        cursor = 9223372036854775807  # 從最新開始
        seen_ids = set()  # 防 cursor 邊界重複

        for _ in range(20):  # 最多 20 頁防止無限
            page, err = self.list_user_messages_sync(
                cid, limit=page_size, cursor=cursor, timeout=per_page_timeout,
            )
            if err:
                # 已拉到部分就返回部分,error 設空(避免覆蓋已有結果)
                if all_msgs:
                    return all_msgs, ""
                return [], err
            if not page:
                break

            new_this_page = 0
            oldest_ts = cursor
            for item in page:
                msg = item.get("message", {}) or {}
                mid = str(msg.get("messageId", ""))
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                all_msgs.append(item)
                new_this_page += 1
                try:
                    ts = int(msg.get("createAt", 0) or 0)
                    if ts and ts < oldest_ts:
                        oldest_ts = ts
                except Exception:
                    pass

            if len(all_msgs) >= max_total:
                break
            # server 返不滿頁 = 沒更多了
            if new_this_page < page_size:
                break
            # 下一頁從本頁最舊的訊息開始(往更舊方向)
            if oldest_ts >= cursor or oldest_ts <= 0:
                break  # 沒進展,防無限
            cursor = oldest_ts

        return all_msgs, ""

    async def _list_user_messages_coro(
        self,
        cid: str,
        limit: int,
        cursor: int = 9223372036854775807,
    ) -> Tuple[List[Dict], str]:
        """asyncio coroutine 內部實作。"""
        if not self._ws or _ws_is_closed(self._ws):
            return [], "WS 已關閉"
        mid = self._next_mid()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[mid] = fut

        frame = {
            "lwp": "/r/MessageManager/listUserMessages",
            "headers": {"mid": mid},
            "body": [cid, False, int(cursor), int(limit), False],
        }
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            self._pending_acks.pop(mid, None)
            return [], f"WS send 失敗: {e}"

        try:
            code, resp = await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending_acks.pop(mid, None)
            return [], "listUserMessages 等 ACK 超時"

        if code != 200:
            return [], f"listUserMessages server 拒絕 code={code}"

        body = (resp or {}).get("body", {}) or {}
        msgs = body.get("userMessageModels", []) or []
        return msgs, ""

    def send_text_sync(
        self,
        peer_user_id: str,
        session_id: str,
        text: str,
        timeout: float = 10.0,
    ) -> Tuple[bool, str]:
        """主 thread 同步發送一條文字訊息。Returns (success, error_msg)。"""
        if not self._connected.is_set():
            return False, "WebSocket 未連線"
        if not self._loop:
            return False, "event loop 未啟動"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._send_text_coro(peer_user_id, session_id, text),
                self._loop,
            )
            return fut.result(timeout=timeout)
        except Exception as e:
            return False, f"send_text 異常: {e}"

    def send_image_sync(
        self,
        peer_user_id: str,
        session_id: str,
        image_bytes: bytes,
        timeout: float = 30.0,
    ) -> Tuple[bool, str]:
        """v6.1.54:同步發送圖片給閒魚賣家。

        Step 1: 上傳到閒魚 IM CDN(stream-upload.goofish.com)
        Step 2: 拿到 CDN URL + size + pix(WxH)
        Step 3: 構造 image WS frame 發送(contentType=2,custom.type=2)

        Returns: (success, info_or_error_msg)
        """
        if not image_bytes or len(image_bytes) < 100:
            return False, "image_bytes 為空或過小"
        if not self._connected.is_set():
            return False, "WebSocket 未連線"
        if not self._loop:
            return False, "event loop 未啟動"

        # Step 1+2: 上傳圖片拿 CDN URL
        cdn_url, w, h, err = _upload_image_to_goofish_cdn(
            self.profile_dir, image_bytes, on_log=self.on_log,
        )
        if not cdn_url:
            return False, f"上傳圖片失敗: {err}"

        # Step 3: WS 發送 image message
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._send_image_coro(peer_user_id, session_id, cdn_url, w, h),
                self._loop,
            )
            return fut.result(timeout=timeout)
        except Exception as e:
            return False, f"send_image WS 異常: {e}"

    def send_image_from_url_sync(
        self,
        peer_user_id: str,
        session_id: str,
        image_url: str,
        timeout: float = 30.0,
        download_timeout: int = 30,
    ) -> Tuple[bool, str]:
        """v6.1.54:從 URL 下載圖片 → 上傳閒魚 CDN → 發送給賣家。

        用於把買家在 Yahoo IM 發的圖片中轉給閒魚賣家(讓賣家看實物比對)。
        """
        try:
            import requests as _req
            r = _req.get(image_url, timeout=download_timeout, allow_redirects=True)
            if r.status_code != 200:
                return False, f"下載源圖 {r.status_code}"
            image_bytes = r.content
            if not image_bytes or len(image_bytes) < 100:
                return False, "源圖空檔/過小"
        except Exception as e:
            return False, f"下載源圖異常: {e}"
        return self.send_image_sync(peer_user_id, session_id, image_bytes, timeout=timeout)

    def create_chat_sync(
        self,
        peer_user_id: str,
        item_id: str,
        timeout: float = 10.0,
    ) -> Tuple[str, str]:
        """v6.0.80:純 WebSocket 建立新對話,拿到 sessionId(cid)。

        對應閒魚 LWP `/r/SingleChatConversation/create`,
        靈感來自 fancyboi999/goofish-cli 的协议实现。

        Returns: (session_id, error_msg) — session_id 為空時 error_msg 說明原因。
        """
        if not self._connected.is_set():
            return "", "WebSocket 未連線"
        if not self._loop:
            return "", "event loop 未啟動"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._create_chat_coro(peer_user_id, item_id, timeout),
                self._loop,
            )
            return fut.result(timeout=timeout + 2)
        except Exception as e:
            return "", f"create_chat 異常: {e}"

    # ── 內部:event loop ──

    def _run_loop(self):
        """後台 thread 的 event loop。"""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main_async())
        except Exception as e:
            self.on_log(f"[XY-WS] event loop 異常: {e}\n{traceback.format_exc()[:500]}")
        finally:
            try:
                if self._loop and not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass

    async def _main_async(self):
        """主循環:連線 → 收發 → 斷線 → 重連(指數退避)。

        v6.0.75 token 401 處理:
        - 立即觸發 auto_refresh_ws_token (背景開 Chrome 一閃而過拿新 token)
        - 內部已有互斥鎖 + 冷卻 (失敗 30 分鐘內不重試,避免限流反复)
        - auto_refresh 成功 → 下次重連用新 token
        - auto_refresh 失敗 → 通知上層切 HTTP fallback
        """
        while not self._stopped:
            try:
                ok = await self._connect_and_handshake()
                if not ok:
                    if self._token_invalid:
                        # 立即觸發 auto_refresh(內部有冷卻保護)
                        self.on_log("[XY-WS] token 失效 → 觸發 auto_refresh_ws_token")
                        self._token_invalid = False  # 清標記,下次走正常 cache 路徑
                        refresh_ok = await self._trigger_auto_refresh()
                        if not refresh_ok:
                            # auto_refresh 失敗 → 通知上層切 HTTP fallback,等冷卻過再試
                            if self._on_unavailable_callback:
                                try:
                                    self._on_unavailable_callback("token_refresh_failed")
                                except Exception:
                                    pass
                            await asyncio.sleep(60)  # 等 1 分鐘再進下次循環
                            continue
                        # auto_refresh 成功 → 立即下次循環重連
                        await asyncio.sleep(2)
                        continue
                    raise RuntimeError("握手失敗")
                self._reconnect_count = 0
                await self._receive_loop()
            except Exception as e:
                self._connected.clear()
                self.on_log(f"[XY-WS] 連線斷掉: {e}")
                if self._stopped:
                    return
                delay = RECONNECT_BACKOFF[min(self._reconnect_count, len(RECONNECT_BACKOFF) - 1)]
                self._reconnect_count += 1
                self.on_log(f"[XY-WS] {delay}s 後重連 (第 {self._reconnect_count} 次)")
                await asyncio.sleep(delay)

    async def _trigger_auto_refresh(self) -> bool:
        """在 thread pool 內跑 auto_refresh_ws_token(避免阻塞 event loop)。"""
        try:
            from core.goofish_token_fetcher import auto_refresh_ws_token
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(
                None,
                auto_refresh_ws_token,
                self.profile_dir,
                "",
                self.on_log,
            )
            return ok
        except Exception as e:
            self.on_log(f"[XY-WS] _trigger_auto_refresh 異常: {e}")
            return False

    async def _connect_and_handshake(self) -> bool:
        """建立 WS + 執行 /reg 握手。"""
        # 1. 取 access_token + device_id + my_user_id
        # v6.0.75:用 goofish_token_fetcher (Playwright 24h 緩存),繞 login.token API RGV587 异常码
        from core.xianyu_im_http import get_my_user_id
        from core.goofish_device import get_device_id
        from core.goofish_token_fetcher import ensure_access_token

        self._my_user_id = get_my_user_id(self.profile_dir)
        if not self._my_user_id:
            self.on_log("[XY-WS] 缺少 cookie unb,無法連線(請先在採購頁登入閒魚)")
            return False
        self._device_id = get_device_id(self._my_user_id)
        # v6.0.75:如果上次因 401 被標記 token invalid → 這次 force_refresh
        force_refresh = self._token_invalid
        if force_refresh:
            self.on_log("[XY-WS] 上次 401 標記 token invalid,強制刷新...")
            self._token_invalid = False  # 清標記
        # 在 thread pool 內跑 Playwright(避免阻塞 event loop)
        loop = asyncio.get_event_loop()
        access_token, err = await loop.run_in_executor(
            None,
            ensure_access_token,
            self.profile_dir,
            self._device_id,
            self.on_log,
            force_refresh,
        )
        if err or not access_token:
            self.on_log(f"[XY-WS] 取 access_token 失敗: {err}")
            # v6.0.79:保存錯誤訊息,讓上層 ws.start 失敗時能拿到具體原因
            self.last_connect_error = err or "access_token 取得失敗"
            return False
        self._access_token = access_token

        # 2. 建 WebSocket 連線
        self.on_log(f"[XY-WS] 連線 {WS_URL}...")
        self._ws = await websockets.connect(
            WS_URL,
            user_agent_header=WS_UA,
            ping_interval=None,  # 不用 WS 層 ping,LWP 有自己心跳
            max_size=2 * 1024 * 1024,  # 2MB 訊息上限
        )
        self.on_log(f"[XY-WS] WS 連線建立,發送 /reg ...")

        # 3. /reg 註冊
        reg_msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": WS_APP_KEY,
                "token": self._access_token,
                "ua": WS_UA,
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self._device_id,
                "mid": self._next_mid(),
            },
        }
        await self._ws.send(json.dumps(reg_msg))

        # 4. 等 server ack(預期 code=200)+ 初始 /s/sync
        reg_acked = False
        sync_acked = False
        for _i in range(5):
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                # v6.1.43:補設 last_connect_error,讓上層拿到具體原因(原本 default 是 "未知錯誤")
                _msg = f"/reg 等 server 回應超時 (第 {_i+1} 次, 10s timeout)"
                self.on_log(f"[XY-WS] {_msg}")
                self.last_connect_error = _msg
                return False
            except Exception as _e_recv:
                # 連線可能被 server 斷掉,印 close code
                _code = getattr(self._ws, 'close_code', None)
                _reason = getattr(self._ws, 'close_reason', None)
                _msg = (
                    f"recv 異常 (server 主動斷線?): {_e_recv} "
                    f"close_code={_code} close_reason={_reason!r}"
                )
                self.on_log(f"[XY-WS] {_msg}")
                self.last_connect_error = _msg
                return False
            self.on_log(f"[XY-WS] /reg 收到 frame #{_i+1}: {raw[:200]!s}")
            try:
                j = json.loads(raw)
            except Exception:
                continue
            code = j.get("code")
            lwp = j.get("lwp", "")
            # v6.0.75 + v6.1.43:401 token 過期 — 標記 + 補設 last_connect_error
            # token cache 還在 24h 內但 server invalidate(罕見但會發生),需要 force_refresh
            if code == 401:
                body = j.get("body") or {}
                reason = body.get("reason", "")
                _msg = f"/reg 401 token 失效: reason={reason!r}"
                self.on_log(f"[XY-WS] {_msg} → 標記 token invalid,下次連線會 force_refresh")
                self._token_invalid = True
                self.last_connect_error = _msg
                return False
            if code == 200 and not reg_acked:
                reg_acked = True
                self.on_log("[XY-WS] /reg 註冊成功 (code=200)")
            elif lwp == "/s/sync" and not sync_acked:
                # ACK initial /s/sync
                mid_hdr = (j.get("headers") or {}).get("mid", "")
                if mid_hdr:
                    await self._ws.send(json.dumps({"code": 200, "headers": {"mid": mid_hdr}}))
                sync_acked = True
                self.on_log("[XY-WS] 初始 /s/sync 已 ACK")
            if reg_acked:
                break

        if not reg_acked:
            _msg = "/reg 5 個 frame 內沒收到 200 ACK"
            self.on_log(f"[XY-WS] {_msg}")
            self.last_connect_error = _msg
            return False

        # 標記連線就緒
        self._connected.set()
        self.on_log("[XY-WS] [OK] 握手完成,進入接收循環")

        # 啟動心跳協程
        asyncio.create_task(self._heartbeat_loop())
        return True

    async def _receive_loop(self):
        """接收主循環。"""
        async for raw in self._ws:
            try:
                j = json.loads(raw)
            except Exception:
                continue
            await self._handle_inbound(j)

    async def _heartbeat_loop(self):
        """每 15 秒發送 /! ping。"""
        try:
            while not self._stopped and self._ws and not _ws_is_closed(self._ws):
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._stopped or not self._ws or _ws_is_closed(self._ws):
                    return
                try:
                    await self._ws.send(json.dumps({
                        "lwp": "/!",
                        "headers": {"mid": self._next_mid()},
                    }))
                except Exception as e:
                    self.on_log(f"[XY-WS] 心跳發送失敗: {e}")
                    return
        except Exception:
            return

    async def _handle_inbound(self, j: Dict[str, Any]):
        """處理收到的訊息 — dispatch 給 callback。"""
        lwp = j.get("lwp", "")
        code = j.get("code")
        headers = j.get("headers") or {}
        mid_hdr = headers.get("mid", "")

        # v6.0.75:診斷所有 inbound 類型(過濾雜訊後印精簡 log)
        if lwp and lwp not in ("/!",):
            self.on_log(f"[XY-WS] <- recv lwp={lwp} mid={mid_hdr[:24]} code={code}")
        elif code is not None and code != 200:
            self.on_log(f"[XY-WS] <- recv code={code} mid={mid_hdr[:24]}")

        # v6.0.75:除了 /s/sync, /s/para 還要處理 /s/vulcan(積壓批次推送)
        # 全部都包含 syncPushPackage 結構,直接統一處理
        if lwp in ("/s/sync", "/s/para", "/s/vulcan"):
            # ACK 推送
            if mid_hdr:
                try:
                    await self._ws.send(json.dumps({"code": 200, "headers": {"mid": mid_hdr}}))
                except Exception:
                    pass

            # 解析 syncPushPackage.data[]
            body = j.get("body") or {}
            pkg = body.get("syncPushPackage") or {}
            data_list = pkg.get("data", []) or []
            if data_list:
                self.on_log(f"[XY-WS] {lwp} push 含 {len(data_list)} 條 data item")
            for item in data_list:
                try:
                    obj_type = int(item.get("objectType", 0))
                except Exception:
                    obj_type = 0
                b64 = item.get("data", "")
                self.on_log(f"[XY-WS]   item objectType={obj_type} data_len={len(b64)}")

                # v6.0.80:_create_chat_coro 改從 ACK body 抓 sessionId,
                # 不再依賴 /s/vulcan push 解析(舊邏輯誤抓率高)

                if obj_type == 40000:
                    msg = parse_inbound_message(b64)
                    if msg and self._on_inbound_msg:
                        msg.object_type = 40000
                        try:
                            self._on_inbound_msg(msg)
                        except Exception as e:
                            self.on_log(f"[XY-WS] on_inbound_msg 異常: {e}")
                elif obj_type == 40006:
                    # v6.0.75:session event trigger - 抽 cid,讓上層 HTTP 拉正文
                    msg = parse_session_event(b64)
                    if msg and self._on_inbound_msg:
                        try:
                            self._on_inbound_msg(msg)
                        except Exception as e:
                            self.on_log(f"[XY-WS] on_inbound_msg (40006) 異常: {e}")
                elif obj_type == 40103:
                    rr = parse_read_receipt(b64)
                    if rr and self._on_read_receipt:
                        try:
                            self._on_read_receipt(rr)
                        except Exception as e:
                            self.on_log(f"[XY-WS] on_read_receipt 異常: {e}")
        elif code is not None:
            # 業務操作 ACK (code=200 成功 / 4xx 失敗)
            # v6.0.75:喚醒 send_text_coro 內等 ACK 的 future
            if mid_hdr and mid_hdr in self._pending_acks:
                fut = self._pending_acks.pop(mid_hdr)
                if not fut.done():
                    fut.set_result((code, j))

    async def _send_text_coro(
        self,
        peer_user_id: str,
        session_id: str,
        text: str,
    ) -> Tuple[bool, str]:
        """asyncio coroutine:發送訊息並等待 ACK。"""
        if not self._ws or _ws_is_closed(self._ws):
            return False, "WS 已關閉"
        if not self._my_user_id:
            return False, "缺少 my_user_id"
        if not session_id:
            return False, "缺少 session_id"

        cid = f"{session_id}@goofish"
        uuid_str = self._next_uuid()
        data_b64 = encode_text_content(text)
        msg_mid = self._next_mid()

        frame = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": msg_mid},
            "body": [
                {
                    "uuid": uuid_str,
                    "cid": cid,
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": 1, "data": data_b64},
                    },
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1,
                },
                {
                    "actualReceivers": [
                        f"{self._my_user_id}@goofish",
                        f"{peer_user_id}@goofish",
                    ],
                },
            ],
        }
        # v6.0.75:登記 ACK future,等 server 回 code=200 same mid
        ack_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[msg_mid] = ack_future

        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            self._pending_acks.pop(msg_mid, None)
            return False, f"WS send 失敗: {e}"

        # 等 server ACK (最多 5 秒)
        try:
            code, ack_frame = await asyncio.wait_for(ack_future, timeout=5)
        except asyncio.TimeoutError:
            self._pending_acks.pop(msg_mid, None)
            self.on_log(f"[XY-WS] 發送但 5 秒沒收到 ACK (peer={peer_user_id})")
            return False, "send 後 5 秒沒收到 ACK"

        if code == 200:
            self.on_log(f"[XY-WS] 已發送 ACK 確認: peer={peer_user_id} sid={session_id} text={text[:40]!r}")
            return True, ""
        else:
            body = (ack_frame or {}).get("body") or {}
            reason = body.get("reason") or body.get("developerMessage") or ""
            self.on_log(f"[XY-WS] 發送被 server 拒絕: code={code} reason={reason}")
            # token 過期 → 標記 invalid
            if code == 401:
                self._token_invalid = True
            return False, f"server 拒絕 code={code} reason={reason}"

    async def _send_image_coro(
        self,
        peer_user_id: str,
        session_id: str,
        cdn_url: str,
        width: int,
        height: int,
    ) -> Tuple[bool, str]:
        """v6.1.54:asyncio coroutine 發送圖片訊息(WS frame contentType=2,custom.type=2)。

        Frame 結構(适配自閒魚 PC web 2026-05-23):
        - inner JSON: {"contentType": 2, "image": {"pics": [{"url": CDN, "width": W, "height": H}]}}
        - inner JSON base64 → custom.data
        - custom.type = 2(對應 inner.contentType=2)
        - 其餘 frame 結構跟 send_text 一致
        """
        if not self._ws or _ws_is_closed(self._ws):
            return False, "WS 已關閉"
        if not self._my_user_id:
            return False, "缺少 my_user_id"
        if not session_id:
            return False, "缺少 session_id"
        if not cdn_url:
            return False, "缺少 cdn_url"

        cid = f"{session_id}@goofish"
        uuid_str = self._next_uuid()
        msg_mid = self._next_mid()

        # 構造 inner image content + base64 encode(對齊既有 encode_text_content 邏輯)
        inner = json.dumps(
            {
                "contentType": 2,
                "image": {
                    "pics": [{
                        "url": cdn_url,
                        "width": int(width or 0),
                        "height": int(height or 0),
                    }],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data_b64 = base64.b64encode(inner.encode("utf-8")).decode("ascii")

        frame = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": msg_mid},
            "body": [
                {
                    "uuid": uuid_str,
                    "cid": cid,
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": 2, "data": data_b64},  # type=2 = image
                    },
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1,
                },
                {
                    "actualReceivers": [
                        f"{self._my_user_id}@goofish",
                        f"{peer_user_id}@goofish",
                    ],
                },
            ],
        }
        ack_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[msg_mid] = ack_future

        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            self._pending_acks.pop(msg_mid, None)
            return False, f"WS send 失敗: {e}"

        try:
            code, ack_frame = await asyncio.wait_for(ack_future, timeout=5)
        except asyncio.TimeoutError:
            self._pending_acks.pop(msg_mid, None)
            self.on_log(f"[XY-WS] 發圖但 5 秒沒收到 ACK (peer={peer_user_id})")
            return False, "send_image 後 5 秒沒收到 ACK"

        if code == 200:
            self.on_log(f"[XY-WS] 已發圖 ACK: peer={peer_user_id} sid={session_id} url={cdn_url[:60]}")
            return True, f"圖片已發送 cdn={cdn_url[:80]}"
        else:
            body = (ack_frame or {}).get("body") or {}
            reason = body.get("reason") or body.get("developerMessage") or ""
            self.on_log(f"[XY-WS] 發圖被 server 拒絕: code={code} reason={reason}")
            if code == 401:
                self._token_invalid = True
            return False, f"server 拒絕 code={code} reason={reason}"

    async def _create_chat_coro(
        self,
        peer_user_id: str,
        item_id: str,
        timeout: float = 10.0,
    ) -> Tuple[str, str]:
        """v6.0.80:LWP /r/SingleChatConversation/create 建立新對話,拿到 sessionId。

        流程(v6.0.80 重構,直接從 ACK body 抓 sessionId,不依賴 push):
        1. 發 create frame(pairFirst=peer, pairSecond=my, bizType=1, extension.itemId)
        2. 等 server ACK(code=200,same mid 配對)
        3. ACK body 內含 lastMessage.message.extension.reminderUrl,
           裡面有 sid=<sessionId>(這是 server 對該 peer+item 分配的真實 sessionId)
        4. regex 抽 sid → 完成

        實測:對「已對話過」的 peer+item,server 直接返回現有 sessionId(idempotent);
              對「全新對話」,server 建新 session 並在 ACK 中返回。

        Returns: (session_id, error_msg)
        """
        if not self._ws or _ws_is_closed(self._ws):
            return "", "WS 已關閉"
        if not self._my_user_id:
            return "", "缺少 my_user_id"

        mid = self._next_mid()
        frame = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {"mid": mid},
            "body": [
                {
                    "pairFirst": f"{peer_user_id}@goofish",
                    "pairSecond": f"{self._my_user_id}@goofish",
                    "bizType": "1",
                    "extension": {"itemId": str(item_id)},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                }
            ],
        }

        # 登記 ACK future
        ack_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[mid] = ack_future

        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
            self.on_log(f"[XY-WS] 發 create_chat: peer={peer_user_id} itemId={item_id} mid={mid}")
        except Exception as e:
            self._pending_acks.pop(mid, None)
            return "", f"WS send 失敗: {e}"

        # 等 ACK(5 秒)
        try:
            code, ack_frame = await asyncio.wait_for(ack_future, timeout=5)
        except asyncio.TimeoutError:
            self._pending_acks.pop(mid, None)
            return "", "create_chat ACK 超時"

        if code != 200:
            body = (ack_frame or {}).get("body") or {}
            reason = body.get("reason") or body.get("developerMessage") or ""
            return "", f"create_chat 被拒 code={code} reason={reason}"

        # v6.0.80/81:從 ACK body 中抽 sessionId — 多策略(server 結構偶爾變)
        ack_body_str = ""
        try:
            ack_body_str = json.dumps(ack_frame, ensure_ascii=False)

            # 策略 1:reminderUrl 內 sid (最常見)— sid 可能是純數字也可能含字母,放寬 charset
            m = re.search(r'[?&]sid=([A-Za-z0-9_-]+)', ack_body_str)
            if m and m.group(1).isdigit() and len(m.group(1)) >= 6:
                session_id = m.group(1)
                self.on_log(f"[XY-WS] [OK] create_chat ACK sessionId={session_id} (from reminderUrl sid)")
                return session_id, ""

            # 策略 2:sessionId 欄位
            m = re.search(r'"sessionId"\s*:\s*"?(\d{6,})"?', ack_body_str)
            if m:
                session_id = m.group(1)
                self.on_log(f"[XY-WS] [OK] create_chat ACK sessionId={session_id} (from sessionId field)")
                return session_id, ""

            # 策略 3:cid 字段 "<sid>@goofish"
            m = re.search(r'"cid"\s*:\s*"(\d{6,})@goofish"', ack_body_str)
            if m:
                session_id = m.group(1)
                self.on_log(f"[XY-WS] [OK] create_chat ACK sessionId={session_id} (from cid)")
                return session_id, ""

            # 策略 4:conversationId / convId 欄位
            m = re.search(r'"(?:conversationId|convId|conv_id)"\s*:\s*"?(\d{6,})"?', ack_body_str)
            if m:
                session_id = m.group(1)
                self.on_log(f"[XY-WS] [OK] create_chat ACK sessionId={session_id} (from convId)")
                return session_id, ""

            # 策略 5:任何長數字串內含 @goofish 後綴(雙人對話 cid 格式)
            m = re.search(r'(\d{10,})@goofish', ack_body_str)
            if m:
                session_id = m.group(1)
                self.on_log(f"[XY-WS] [OK] create_chat ACK sessionId={session_id} (from @goofish suffix)")
                return session_id, ""
        except Exception as _e:
            self.on_log(f"[XY-WS] create_chat ACK 解析異常: {_e}")

        # ACK 成功但沒抓到 sessionId — 印 body preview 供後續适配(剪短到 1500 字避免 log 爆炸)
        body_preview = ack_body_str[:1500] if ack_body_str else "(empty ack_frame)"
        self.on_log(f"[XY-WS] [WARN] ACK code=200 但 5 種策略全失效,fallback HTTP. body preview: {body_preview}")
        return "", "WAIT_HTTP_FALLBACK"

    def _next_mid(self) -> str:
        self._mid_seq += 1
        return make_mid(self._mid_seq)

    def _next_uuid(self) -> str:
        self._uuid_serial += 1
        return make_uuid(self._uuid_serial)


# ─────────────────────────── 圖片上傳到閒魚 IM CDN ───────────────────────────
# v6.1.54:适配自閒魚 PC web 2026-05-23 抓的 XHR
#   endpoint: POST https://stream-upload.goofish.com/api/upload.api
#   query:    ?floderId=0&appkey=xy_chat&_input_charset=utf-8
#   body:     multipart/form-data with key "file"
#   response: {"object":{"fileId","folderId","url","fileName","size","pix","quality"},
#              "success":true,"status":0,...}
#   pix 是 "WxH" 字串

_GOOFISH_IM_UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api"


def _upload_image_to_goofish_cdn(
    profile_dir: Path,
    image_bytes: bytes,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[str, int, int, str]:
    """v6.1.54:上傳圖片到閒魚 IM CDN(xy_chat appkey)。

    Returns: (cdn_url, width, height, error_msg)
      成功:cdn_url 非空,error 空
      失敗:cdn_url 空,error 說明原因
    """
    _log = on_log or (lambda *_: None)
    try:
        from .xianyu_im_http import _build_session
    except Exception as e:
        return "", 0, 0, f"import _build_session 失敗: {e}"

    sess, _token, err = _build_session(profile_dir, on_log=_log)
    if not sess:
        return "", 0, 0, f"session 不可用: {err}"

    try:
        # 自動偵測圖片格式(PNG vs JPEG)從 magic bytes
        _mime = "image/png"
        _fname = "upload.png"
        if image_bytes[:3] == b"\xff\xd8\xff":  # JPEG SOI
            _mime = "image/jpeg"
            _fname = "upload.jpg"
        elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":  # PNG magic
            _mime = "image/png"
            _fname = "upload.png"
        elif image_bytes[:4] in (b"GIF8",):
            _mime = "image/gif"
            _fname = "upload.gif"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            _mime = "image/webp"
            _fname = "upload.webp"

        params = {
            "floderId": "0",
            "appkey": "xy_chat",
            "_input_charset": "utf-8",
        }
        # v6.1.54-fix:curl_cffi 不支持 requests 風格的 `files=`,改用 CurlMime multipart
        try:
            from curl_cffi import CurlMime
            mp = CurlMime()
            mp.addpart(
                name="file",
                content_type=_mime,
                filename=_fname,
                data=image_bytes,
            )
            r = sess.post(
                _GOOFISH_IM_UPLOAD_URL,
                params=params,
                multipart=mp,
                timeout=30,
            )
        except ImportError:
            # 沒裝 curl_cffi,fallback 試 requests-style(舊版環境)
            files = {"file": (_fname, image_bytes, _mime)}
            r = sess.post(
                _GOOFISH_IM_UPLOAD_URL,
                params=params,
                files=files,
                timeout=30,
            )
        if r.status_code != 200:
            return "", 0, 0, f"上傳 HTTP {r.status_code}: {r.text[:200]}"
        try:
            data = r.json()
        except Exception as e:
            return "", 0, 0, f"upload response 不是 JSON: {e} text={r.text[:200]}"

        if not data.get("success"):
            return "", 0, 0, f"upload success=false: {str(data)[:200]}"
        obj = data.get("object") or {}
        if not isinstance(obj, dict):
            return "", 0, 0, f"upload object 不是 dict: {type(obj)}"

        cdn_url = str(obj.get("url", "") or "")
        if not cdn_url:
            return "", 0, 0, f"upload 缺 url: {list(obj.keys())}"

        # pix = "WxH"
        pix = str(obj.get("pix", "") or "")
        w, h = 0, 0
        try:
            if "x" in pix:
                _w, _h = pix.lower().split("x", 1)
                w = int(_w)
                h = int(_h)
        except Exception:
            pass

        _log(f"[XY-WS] upload image OK url={cdn_url[:80]} {w}x{h} size={obj.get('size','?')}")
        return cdn_url, w, h, ""
    except Exception as e:
        return "", 0, 0, f"上傳異常: {e}"
