"""判定買家是否已讀我訊息 + 對方最後上線時間(BOSH `queryChannelReadInfo` + 訊息對比)。

實機驗證返回:
{
    "returnCode": 0,
    "chID": "yahoo-bid-logbot1:y9000000001:y9000000002",
    "readInfo": [
        {"userID": "y9000000002", "lastReadTime": 1700000000000, "userReadTime": ...},  # 買家讀我訊息到這時間
        {"userID": "y9000000001", "lastReadTime": 1700000000001, "userReadTime": ...},  # 我讀對方訊息到這時間
    ]
}

判定邏輯:
- 取我發給對方的最後一條訊息 ts
- 對比對方的 lastReadTime
- lastReadTime >= my_last_msg_ts → 已讀,< → 未讀
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


def _ensure_ssl_cert_env() -> None:
    """避開 certifi cacert.pem 中文路徑導致 curl SSL fail。

    把 cacert.pem 複製到 ASCII-only 臨時路徑 + 設 SSL_CERT_FILE/CURL_CA_BUNDLE。
    一次性,後續所有 curl_cffi 操作都用這路徑。
    """
    import os
    if os.environ.get("CURL_CA_BUNDLE"):
        return  # 已設過
    try:
        import certifi, shutil, tempfile
        src = certifi.where()
        if not src or any(ord(c) > 127 for c in src):
            # 路徑含非 ASCII 才複製
            dst = os.path.join(tempfile.gettempdir(), "cacert_ascii.pem")
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            os.environ["SSL_CERT_FILE"] = dst
            os.environ["CURL_CA_BUNDLE"] = dst
            os.environ["REQUESTS_CA_BUNDLE"] = dst
    except Exception:
        pass


# 模組初始化時設一次
_ensure_ssl_cert_env()


def fetch_read_status(
    profile_dir: Path,
    *,
    channel_id: str,
    my_id: str,
    on_log: Optional[LogFn] = None,
) -> Tuple[Optional[Dict], str]:
    """BOSH 拉 channel 的雙方 read ts + 我最後訊息 ts → 判定買家已讀。

    Args:
        profile_dir: Chrome profile 目錄
        channel_id: 如 "yahoo-bid-logbot1:y9000000001:y9000000002"
        my_id: 如 "y9000000001"(小寫)

    Returns:
        (status_dict, error)

    status_dict 含:
        - other_user_id: 對方 Y-id (小寫)
        - other_last_read_ts: 對方最後 read 我訊息的 ts (ms)
        - my_last_msg_ts: 我最後一條訊息的 ts (ms),0 表示我沒發過
        - is_read: 對方是否已讀我最後訊息 (True/False/None — None = 我沒發過訊息)
        - friendly: 人類可讀字串如「已讀 (5 分鐘前)」/「未讀」/「對方還沒看過」
    """
    on_log = on_log or (lambda *_: None)

    try:
        from .yahoo_im_bosh_ext import BOSHSession
    except Exception as e:
        return None, f"BOSHSession import 失敗: {e}"

    try:
        with BOSHSession(profile_dir, on_log=on_log) as s:
            # v6.1 Note: channel_user_active 是 set IQ(fire-and-forget),不是 query
            # → 要 query 對方此刻 active 需要 BOSH 長連線監聽 presence stream
            # → 跨 instance 重構成本太高,暫不做。當前用 lastReadTime 推斷「剛在線」已最接近
            other_in_chat = None

            # 1. 拿雙方 read ts(channel 對方向敏感,正向沒 readInfo 就試反向)
            my_id_l = my_id.lower()

            def _query_read_info(ch):
                resp, err = s.iq(
                    "juiker:iq:queryChannelReadInfo",
                    {"chID": ch}, iq_type="get",
                )
                if err or not isinstance(resp, dict):
                    return [], err
                return resp.get("readInfo") or [], ""

            read_info, err = _query_read_info(channel_id)
            if not read_info:
                # 試反向
                parts = channel_id.split(":")
                if len(parts) == 3:
                    rev_ch = f"{parts[0]}:{parts[2]}:{parts[1]}"
                    read_info, _ = _query_read_info(rev_ch)
                    if read_info:
                        on_log(f"[READ-STATUS] queryReadInfo 反向命中: {rev_ch[-50:]}")
            if not read_info:
                return None, f"queryChannelReadInfo 兩向都空: {err or ''}"

            other_last_read = 0
            other_user_id = ""
            for u in read_info:
                uid = (u.get("userID") or "").lower()
                if uid and uid != my_id_l:
                    other_user_id = uid
                    other_last_read = int(u.get("lastReadTime", 0) or 0)
                    break

            # 2. 拿我最後一條訊息的 ts(BOSH queryMessage 排序 asc 舊→新)
            #    欄位是 senderID / sendTime / msgType(1=normal)
            #    BOSH 對 channel 方向敏感:shop:buyer 可能 0 條,buyer:shop 才有
            def _query_my_last_ts(ch):
                resp2, err2 = s.iq(
                    "juiker:iq:queryMessage",
                    {"chID": ch, "afterN": 30},
                    iq_type="get",
                )
                if err2 or not isinstance(resp2, dict):
                    return 0, 0
                msgs = resp2.get("messages") or resp2.get("result") or []
                total = len(msgs)
                for m in reversed(msgs):
                    sender = (m.get("senderID") or m.get("sender") or "").lower()
                    if sender == my_id_l and int(m.get("msgType", 0) or 0) == 1:
                        return int(
                            m.get("sendTime", 0)
                            or m.get("createdUts", 0) or 0
                        ), total
                return 0, total

            my_last_msg_ts, total_msgs = _query_my_last_ts(channel_id)
            # 沒找到我訊息 + 訊息總數少 → 試反向 channel
            if my_last_msg_ts == 0:
                parts = channel_id.split(":")
                if len(parts) == 3:
                    rev_ch = f"{parts[0]}:{parts[2]}:{parts[1]}"
                    my_last_msg_ts, _ = _query_my_last_ts(rev_ch)
                    if my_last_msg_ts:
                        on_log(f"[READ-STATUS] 反向 channel 命中: {rev_ch[-50:]}")

    except Exception as e:
        return None, f"fetch_read_status 異常: {e}"

    # 3. 判定 is_read — 顯示「絕對時間 + 相對時間」
    def _fmt_abs(ts_ms: int) -> str:
        """ms ts → 「MM/DD HH:MM」(同年省略 year);跨年才顯示完整日期。"""
        if not ts_ms:
            return ""
        lt = time.localtime(ts_ms / 1000)
        now_yr = time.localtime().tm_year
        if lt.tm_year == now_yr:
            return time.strftime("%m/%d %H:%M", lt)
        return time.strftime("%Y/%m/%d %H:%M", lt)

    def _rel_delta(delta_sec: float) -> str:
        if delta_sec < 60: return "秒讀"
        if delta_sec < 3600: return f"{int(delta_sec/60)} 分鐘後"
        if delta_sec < 86400: return f"{int(delta_sec/3600)} 小時後"
        return f"{int(delta_sec/86400)} 天後"

    if my_last_msg_ts == 0:
        is_read = None
        friendly = "對方還沒收過我的訊息"
    elif other_last_read >= my_last_msg_ts:
        is_read = True
        delta_sec = max(0, (other_last_read - my_last_msg_ts) / 1000)
        friendly = f"✅ {_fmt_abs(other_last_read)} 已讀 ({_rel_delta(delta_sec)}讀)"
    else:
        is_read = False
        delta_sec = max(0, time.time() - my_last_msg_ts / 1000)
        if delta_sec < 60:
            friendly = f"⭕ 未讀 (剛發送 {_fmt_abs(my_last_msg_ts)})"
        else:
            friendly = f"⭕ 未讀 (送出 {_fmt_abs(my_last_msg_ts)},已 {_rel_delta(delta_sec)[:-1]})"

    # 4. 對方最後活動時間(lastReadTime 即近似「對方上次開 IM 看訊息」的時間)
    if other_last_read > 0:
        ago_sec = max(0, time.time() - other_last_read / 1000)
        if ago_sec < 60:
            online_str = f"🟢 剛剛在線 ({_fmt_abs(other_last_read)})"
        elif ago_sec < 300:
            online_str = f"🟢 {_fmt_abs(other_last_read)} 在線 (幾分鐘前)"
        elif ago_sec < 3600:
            online_str = f"🕐 {_fmt_abs(other_last_read)} 在線 ({int(ago_sec/60)} 分鐘前)"
        elif ago_sec < 86400:
            online_str = f"🕐 {_fmt_abs(other_last_read)} 在線 ({int(ago_sec/3600)} 小時前)"
        else:
            online_str = f"🕐 {_fmt_abs(other_last_read)} 在線 ({int(ago_sec/86400)} 天前)"
    else:
        online_str = ""

    return {
        "other_user_id": other_user_id,
        "other_last_read_ts": other_last_read,
        "my_last_msg_ts": my_last_msg_ts,
        "is_read": is_read,
        "friendly": friendly,
        "online_friendly": online_str,
        "other_in_chat": other_in_chat,  # v6.1:對方此刻是否在 channel 內(等效 typing)
    }, ""


def format_read_status_for_card(status: Dict) -> str:
    """格式化:
    🟢 5 分鐘前在線
    ✅ 已讀 (3 分鐘後讀)
    """
    if not status:
        return ""
    lines = []
    if status.get("online_friendly"):
        lines.append(status["online_friendly"])
    # v6.1:對方此刻是否打開對話視窗
    other_in = status.get("other_in_chat")
    if other_in is True:
        lines.append("👀 *對方此刻在這對話視窗*")
    if status.get("friendly"):
        lines.append(status["friendly"])
    return "\n".join(lines)
