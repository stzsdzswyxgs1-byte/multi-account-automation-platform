"""Yahoo IM 媒體發送 — 圖片/視頻/貼圖純 HTTP 路徑 (v6.0.83+)

實機适配自 tw.bid.yahoo.com/chat 完整流程:

【圖片發送 4 步】
Step 0 GET  https://trendr-apac.media.yahoo.com/api/pixelframe/v1/aws/resources/s3/credentials
            → AWS STS 臨時憑證 (~1 小時)
Step 1 PUT  https://{bucket}.s3.{region}.amazonaws.com/{path}/{uuid}
            → 圖片 bytes 上傳到 S3 (sigv4 簽名,用 STS 憑證)
Step 2 POST https://trendr-apac.media.yahoo.com/api/pixelframe/v1/images/upload
            body: {"url": "<s3_temp_url>", "targetType":"property", "targetId":"auction2",
                   "appName":"im_sdk", "resizingProfile":"imsdk"}
            → 返回 {"id":"<UUID>", "url":"<CDN_URL>", "width":N, "height":N,
                    "resizedImages": {...}}
Step 3 POST https://tw.bid.yahoo.com/fe/api/im/message/send
            body: {"onlyValidate":true, "channelId":"yahoo-bid-logbot1:y{shop}:y{buyer}",
                   "type":"image", "value":{thumbnail, id, origin, src}, "receiver":"Y...",
                   "wssid":"..."}
            → 200 + 對方真的收到 (實測對方截圖確認)

【貼圖/視頻同樣走 Step 3,只是 type/value 不同】
sticker:  type="sticker", value={"id":"<sticker_id>", "url":"https://img.yec.tw/ma/auc/stickers/.../<id>.png"}
video:    type="video",   value={"id":..., "url":..., "thumbnail":..., "duration":...} (推測,套同模式)

MessageType enum (from Juiker SDK): TEXT=1, EMOJI=2, PHOTO=3, VIDEO=4, AUDIO=5
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Dict, Any

import requests
from curl_cffi.requests import Session as CffiSession

from .client_runtime_compat import get_api_headers
from .cookie_store import invalidate_cookie_cache, save_cookie_cache
from .im_http_ops import _build_session, _yahoo_cookies_dict, build_channel_id, _CHANNEL_DIR_CACHE, _reverse_channel_id, im_mark_read
from .merch_http_ops import _detect_system_proxy, _fetch_wssid_http

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]

# ── Pixelframe 端點 ──
_PIXELFRAME_BASE = "https://trendr-apac.media.yahoo.com/api/pixelframe/v1"
# 實機适配自 auc-im.js fetchPixelframeCredentials,role 必填,值為 "content-upload"
_PIXELFRAME_CRED = f"{_PIXELFRAME_BASE}/aws/resources/s3/credentials?role=content-upload"
_PIXELFRAME_FINALIZE = f"{_PIXELFRAME_BASE}/images/upload"
_PIXELFRAME_VIDEO_FINALIZE = f"{_PIXELFRAME_BASE}/videos/upload"

_IM_BASE = "https://tw.bid.yahoo.com/fe/api/im"
_IM_SEND = f"{_IM_BASE}/message/send"


def _sigv4_put_s3(
    *,
    bucket: str,
    region: str,
    object_key: str,
    body_bytes: bytes,
    access_key: str,
    secret_key: str,
    session_token: str,
    mime: str = "image/png",
) -> Tuple[bool, str]:
    """用 boto3 把 bytes PUT 到 S3 (跟 webimsdk 一樣走 AWS.S3 SDK)。

    Yahoo IM web client 的 auc-im.js 用 `window.AWS.S3.upload({Key, Body})`,
    所以 server 端期望的簽名/canonical request 完全等同 AWS SDK 標準格式。
    自己手寫 sigv4 容易碰到細節差異(content-type 是否簽名/payload hash/UNSIGNED-PAYLOAD)
    所以直接用 boto3.client('s3').put_object() — 同一個 SDK 同一個簽名邏輯。
    """
    try:
        import boto3  # type: ignore
        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=body_bytes,
            ContentType=mime,
        )
        return True, ""
    except Exception as e:
        return False, f"S3 PUT 異常: {e}"


def _get_pixelframe_credentials(
    session: CffiSession,
    on_log: LogFn,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Step 0: 拿 AWS STS 臨時憑證 + bucket/path。"""
    try:
        r = session.get(_PIXELFRAME_CRED, timeout=15)
        if r.status_code != 200:
            return None, f"pixelframe credentials {r.status_code}: {r.text[:200]}"
        d = r.json()
        cred = d.get("credentials") or {}
        if not all(k in cred for k in ("accessKeyId", "secretAccessKey", "sessionToken")):
            return None, f"credentials 結構異常: {list(d.keys())}"
        return d, ""
    except Exception as e:
        return None, f"pixelframe credentials 異常: {e}"


def _pixelframe_finalize(
    session: CffiSession,
    s3_url: str,
    on_log: LogFn,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Step 2: 通知 Pixelframe 上傳完成,拿到正式 CDN URL + 尺寸。"""
    payload = {
        "url": s3_url,
        "targetType": "property",
        "targetId": "auction2",
        "appName": "im_sdk",
        "resizingProfile": "imsdk",
    }
    try:
        r = session.post(_PIXELFRAME_FINALIZE, json=payload, timeout=30)
        if r.status_code != 200:
            return None, f"finalize {r.status_code}: {r.text[:300]}"
        d = r.json()
        if not d.get("url") or not d.get("id"):
            return None, f"finalize 缺 url/id: {list(d.keys())}"
        return d, ""
    except Exception as e:
        return None, f"finalize 異常: {e}"


def upload_image_to_yahoo(
    profile_dir: Path,
    image_bytes: bytes,
    *,
    mime: str = "image/png",
    on_log: Optional[LogFn] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """完整 3 步上傳:返回 finalize 結構 {id, url, width, height, resizedImages}。

    給 send_image_message 用的中間結果。失敗回 (None, err)。
    """
    on_log = on_log or (lambda *_: None)

    session, _wssid, err = _build_session(profile_dir)
    if not session:
        return None, f"session 不可用: {err}"

    # Step 0
    cred_resp, err = _get_pixelframe_credentials(session, on_log)
    if not cred_resp:
        return None, err
    cred = cred_resp["credentials"]
    bucket = cred_resp["bucketName"]
    region = cred_resp["region"]
    path = cred_resp["path"]
    on_log(f"[YAHOO-MEDIA] Step 0: 拿到 STS 憑證 bucket={bucket} region={region}")

    # Step 1: PUT to S3
    obj_uuid = str(uuid.uuid1())
    object_key = f"{path}/{obj_uuid}"
    ok, err = _sigv4_put_s3(
        bucket=bucket, region=region, object_key=object_key,
        body_bytes=image_bytes, mime=mime,
        access_key=cred["accessKeyId"],
        secret_key=cred["secretAccessKey"],
        session_token=cred["sessionToken"],
    )
    if not ok:
        return None, err
    s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"
    on_log(f"[YAHOO-MEDIA] Step 1: S3 PUT OK uuid={obj_uuid}")

    # Step 2: finalize
    finalize, err = _pixelframe_finalize(session, s3_url, on_log)
    if not finalize:
        return None, err
    on_log(f"[YAHOO-MEDIA] Step 2: finalize OK id={finalize.get('id')} cdn={finalize.get('url','')[:60]}")
    return finalize, ""


def _build_image_value(finalize: Dict[str, Any]) -> Dict[str, Any]:
    """從 Pixelframe finalize 結果構造 message/send value 字段。

    結構參考實機抓的 reqBody:
    {
      "isLoading": false,
      "thumbnail": {url, width, height},
      "id": "<UUID>",
      "origin": {url, width, height},
      "src": {url, width, height}  // 通常等於 thumbnail
    }
    """
    img_id = finalize["id"]
    origin_url = finalize["url"]
    w = finalize.get("width", 0)
    h = finalize.get("height", 0)
    resized = (finalize.get("resizedImages") or {}).get("displayImage") or {}
    display_url = resized.get("url") or origin_url
    display_w = resized.get("width", w)
    display_h = resized.get("height", h)
    return {
        "isLoading": False,
        "thumbnail": {"url": display_url, "width": display_w, "height": display_h},
        "id": img_id,
        "origin": {"url": origin_url, "width": w, "height": h},
        "src": {"url": display_url, "width": display_w, "height": display_h},
    }


def send_image_message(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    image_bytes: bytes,
    mime: str = "image/png",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """純 HTTP 發送圖片給 Yahoo IM 對方。

    流程:
      1. upload_image_to_yahoo (Step 0-2)
      2. POST /fe/api/im/message/send (type=image, onlyValidate=true)

    Args:
        shop_id: 賣家 Y-ID (我們自己,如 Y9000000001)
        buyer_id: 買家 Y-ID (對方,如 Y9000000002)
        image_bytes: 圖片 bytes
        mime: image/png / image/jpeg
    """
    on_log = on_log or (lambda *_: None)

    # Step 0-2: 上傳
    finalize, err = upload_image_to_yahoo(
        profile_dir, image_bytes, mime=mime, on_log=on_log,
    )
    if not finalize:
        return False, err

    # Step 3: send
    channel_id = build_channel_id(shop_id, buyer_id)
    effective_channel = _CHANNEL_DIR_CACHE.get(channel_id, channel_id)
    receiver = f"Y{buyer_id.lower().lstrip('y')}"

    session, wssid, err = _build_session(profile_dir, buyer_cid=receiver)
    if not session:
        return False, f"session 不可用: {err}"

    payload = {
        # ⚠️ onlyValidate=true 只走 badword 驗證,不會真的送(對方看不到)。
        # text path (im_send_message) 用 False 才真實 deliver — image/video 同理。
        "onlyValidate": False,
        "channelId": effective_channel,
        "type": "image",
        "value": _build_image_value(finalize),
        "receiver": receiver,
        "wssid": wssid,
    }

    # v6.1.20.4:跟 text path 同步加 404 容錯
    #   1. 第 1 次 404 → 試反向 channelId(im_http_ops:471 同邏輯)
    #   2. 第 2 次 404 → 呼 mark_read 觸發 BOSH channel_user_active 開通 channel,再 retry
    _tried_reverse = False
    _tried_mark_read = False

    for attempt in range(4):  # 多一次 attempt 給 mark_read retry
        try:
            r = session.post(_IM_SEND, json=payload, timeout=20)
            if r.status_code == 200:
                _CHANNEL_DIR_CACHE[channel_id] = payload["channelId"]
                # v6.1.53:抓 msgId 供撤回 mapping 用
                _msg_id = ""
                try:
                    _body = r.json() if r.text.strip() else {}
                    _msg_id = str(_body.get("validation", {}).get("messageId", "") or "")
                except Exception:
                    pass
                on_log(f"[YAHOO-MEDIA] Step 3: send image OK id={finalize.get('id')} msgId={_msg_id}")
                # v6.0.83:send 後自動 mark_read(紅點消 + 對方看到我已讀)
                try:
                    from .im_bosh_ops import bosh_mark_read
                    import threading as _th
                    _th.Thread(
                        target=lambda: bosh_mark_read(profile_dir, payload["channelId"], on_log=on_log),
                        daemon=True,
                    ).start()
                except Exception as _e_mr:
                    on_log(f"[YAHOO-MEDIA] auto mark_read 排程失敗(不阻塞): {_e_mr}")
                return True, f"圖片已發送 cdn={finalize.get('url','')[:80]} msgId={_msg_id}"
            if r.status_code in (401, 403) and attempt == 0:
                on_log(f"[YAHOO-MEDIA] send {r.status_code}, 嘗試刷新 wssid")
                new_wssid = _fetch_wssid_http(
                    _yahoo_cookies_dict(session), proxy=_detect_system_proxy(),
                )
                if new_wssid and new_wssid != wssid:
                    wssid = new_wssid
                    payload["wssid"] = new_wssid
                    save_cookie_cache(profile_dir, _yahoo_cookies_dict(session), new_wssid)
                    continue
                invalidate_cookie_cache(profile_dir)
                return False, f"auth expired ({r.status_code})"
            if r.status_code == 429 and attempt < 2:
                time.sleep(3 + attempt * 3)
                continue
            # v6.1.20.4:404 / Channel not found 處理
            _err_text = r.text[:400] if r.text else ""
            _is_channel_404 = (
                r.status_code == 404
                or "Channel not found" in _err_text
                or "42240402" in _err_text
                or "Wrong parameters" in _err_text
            )
            if _is_channel_404:
                # Step 1:試反向 channelId
                if not _tried_reverse:
                    _tried_reverse = True
                    alt_id = _reverse_channel_id(payload["channelId"])
                    if alt_id != payload["channelId"]:
                        on_log(f"[YAHOO-MEDIA] 404,試反向 channelId: {alt_id}")
                        payload["channelId"] = alt_id
                        continue
                # Step 2:呼 mark_read 開通 channel(BOSH channel_user_active)再 retry
                if not _tried_mark_read:
                    _tried_mark_read = True
                    on_log(f"[YAHOO-MEDIA] 404 → mark_read 開通 channel 後 retry")
                    try:
                        im_mark_read(
                            profile_dir, payload["channelId"],
                            buyer_cid=receiver, on_log=on_log,
                        )
                    except Exception as _e_mr2:
                        on_log(f"[YAHOO-MEDIA] mark_read 開通異常(繼續 retry): {_e_mr2}")
                    continue
            return False, f"send {r.status_code}: {r.text[:300]}"
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            return False, f"send 異常: {e}"
    return False, "send 重試耗盡"


def send_sticker_message(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    sticker_id: str,
    sticker_set: str = "tunjiang",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """發送貼圖。
    sticker_id 例如 "tunjiang-02" (從 stickerSuites 拿)
    sticker_set 預設 "tunjiang" (狗狗系列)
    """
    on_log = on_log or (lambda *_: None)
    channel_id = build_channel_id(shop_id, buyer_id)
    effective_channel = _CHANNEL_DIR_CACHE.get(channel_id, channel_id)
    receiver = f"Y{buyer_id.lower().lstrip('y')}"

    session, wssid, err = _build_session(profile_dir, buyer_cid=receiver)
    if not session:
        return False, f"session 不可用: {err}"

    sticker_url = f"https://img.yec.tw/ma/auc/stickers/{sticker_set}/{sticker_id}.png"
    payload = {
        "onlyValidate": False,
        "channelId": effective_channel,
        "type": "sticker",
        "value": {"id": sticker_id, "url": sticker_url},
        "receiver": receiver,
        "wssid": wssid,
    }

    try:
        r = session.post(_IM_SEND, json=payload, timeout=15)
        if r.status_code == 200:
            on_log(f"[YAHOO-MEDIA] sticker OK id={sticker_id}")
            # v6.0.83:send 後自動 mark_read
            try:
                from .im_bosh_ops import bosh_mark_read
                import threading as _th
                _th.Thread(
                    target=lambda: bosh_mark_read(profile_dir, effective_channel, on_log=on_log),
                    daemon=True,
                ).start()
            except Exception:
                pass
            return True, f"貼圖已發送 {sticker_id}"
        return False, f"sticker send {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"sticker 異常: {e}"


def _pixelframe_finalize_video(
    session: CffiSession,
    s3_url: str,
    on_log: LogFn,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """video finalize:POST /api/pixelframe/v1/videos/upload(注意是 videos 不是 images)。

    實機驗證結構(2026-05-15):
      req body: {url, targetType:"property", targetId:"auction2", appName:"im_sdk", transcodingProfile:"imsdk"}
      resp:     {id, url, width, height,
                 thumbnail:{url(.jpg), width, height},
                 resizeVideos:[{url(.mp4), width, height}, ...]}

    server 自動從視頻第一幀生成 _thumbnail.jpg,client 不需要單獨上傳縮圖。
    """
    payload = {
        "url": s3_url,
        "targetType": "property",
        "targetId": "auction2",
        "appName": "im_sdk",
        "transcodingProfile": "imsdk",  # ⚠️ video 是 transcodingProfile,image 是 resizingProfile
    }
    try:
        r = session.post(_PIXELFRAME_VIDEO_FINALIZE, json=payload, timeout=60)
        if r.status_code != 200:
            return None, f"video finalize {r.status_code}: {r.text[:300]}"
        d = r.json()
        if not d.get("url") or not d.get("id"):
            return None, f"video finalize 缺 url/id: {list(d.keys())}"
        return d, ""
    except Exception as e:
        return None, f"video finalize 異常: {e}"


def upload_video_to_yahoo(
    profile_dir: Path,
    video_bytes: bytes,
    *,
    mime: str = "video/mp4",
    on_log: Optional[LogFn] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """video 完整 3 步上傳:STS → S3 PUT → pixelframe video finalize。

    Returns: finalize 結構 {id, url, width, height, thumbnail:{}, resizeVideos:[]}
    """
    on_log = on_log or (lambda *_: None)
    session, _wssid, err = _build_session(profile_dir)
    if not session:
        return None, f"session 不可用: {err}"

    # Step 0: STS credentials
    cred_resp, err = _get_pixelframe_credentials(session, on_log)
    if not cred_resp:
        return None, err
    cred = cred_resp["credentials"]
    bucket = cred_resp["bucketName"]
    region = cred_resp["region"]
    path = cred_resp["path"]

    # Step 1: PUT to S3
    obj_uuid = str(uuid.uuid1())
    object_key = f"{path}/{obj_uuid}"
    ok, err = _sigv4_put_s3(
        bucket=bucket, region=region, object_key=object_key,
        body_bytes=video_bytes, mime=mime,
        access_key=cred["accessKeyId"],
        secret_key=cred["secretAccessKey"],
        session_token=cred["sessionToken"],
    )
    if not ok:
        return None, err
    s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"
    on_log(f"[YAHOO-MEDIA] video S3 PUT OK uuid={obj_uuid}")

    # Step 2: video finalize(transcoding)
    finalize, err = _pixelframe_finalize_video(session, s3_url, on_log)
    if not finalize:
        return None, err
    on_log(f"[YAHOO-MEDIA] video finalize OK id={finalize.get('id')}")
    return finalize, ""


def send_video_message(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    video_bytes: bytes,
    mime: str = "video/mp4",
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """純 HTTP 發送視頻給 Yahoo IM 對方。

    ✅ 實機驗證結構(2026-05-15)。
    Step 0:GET STS credentials
    Step 1:PUT video to S3
    Step 2:POST /api/pixelframe/v1/videos/upload (transcodingProfile)
            → server 自動轉碼 + 生成 thumbnail
    Step 3:POST message/send type="video" value 結構 = finalize 結果

    注意:server 自動從視頻第一幀生成 _thumbnail.jpg,client 不需要單獨上傳縮圖。
    """
    on_log = on_log or (lambda *_: None)

    # Step 0-2: 上傳視頻檔
    finalize, err = upload_video_to_yahoo(
        profile_dir, video_bytes, mime=mime, on_log=on_log,
    )
    if not finalize:
        return False, f"視頻上傳失敗: {err}"

    # Step 3: send (type=video) — 從實機抓的真實結構
    channel_id = build_channel_id(shop_id, buyer_id)
    effective_channel = _CHANNEL_DIR_CACHE.get(channel_id, channel_id)
    receiver = f"Y{buyer_id.lower().lstrip('y')}"

    session, wssid, err = _build_session(profile_dir, buyer_cid=receiver)
    if not session:
        return False, f"session 不可用: {err}"

    # 構造 value:從 finalize response 提取 + 加 src 字段(client side 構造)
    fw = finalize.get("width", 0)
    fh = finalize.get("height", 0)
    value: Dict[str, Any] = {
        "id": finalize["id"],
        "src": {
            "url": finalize["url"],
            "width": fw,
            "height": fh,
        },
        "thumbnail": finalize.get("thumbnail") or {},
        "resizeVideos": finalize.get("resizeVideos") or [],
    }

    payload = {
        # ⚠️ 同 image — onlyValidate=true 只驗證不送
        "onlyValidate": False,
        "channelId": effective_channel,
        "type": "video",
        "value": value,
        "receiver": receiver,
        "wssid": wssid,
    }

    # v6.1.20.4:跟 image 同步加 404 容錯(反向 channel + mark_read 開通)
    _tried_reverse = False
    _tried_mark_read = False
    for attempt in range(4):
      try:
        r = session.post(_IM_SEND, json=payload, timeout=20)
        if r.status_code == 200:
            _CHANNEL_DIR_CACHE[channel_id] = payload["channelId"]
            # v6.1.53:抓 msgId 供撤回 mapping 用
            _msg_id = ""
            try:
                _body = r.json() if r.text.strip() else {}
                _msg_id = str(_body.get("validation", {}).get("messageId", "") or "")
            except Exception:
                pass
            on_log(f"[YAHOO-MEDIA] send video OK id={finalize.get('id')} msgId={_msg_id}")
            try:
                from .im_bosh_ops import bosh_mark_read
                import threading as _th
                _th.Thread(
                    target=lambda: bosh_mark_read(profile_dir, payload["channelId"], on_log=on_log),
                    daemon=True,
                ).start()
            except Exception as _e_mr:
                on_log(f"[YAHOO-MEDIA] video auto mark_read 排程失敗: {_e_mr}")
            return True, f"視頻已發送 cdn={finalize.get('url','')[:80]} msgId={_msg_id}"
        _err_text = r.text[:400] if r.text else ""
        _is_channel_404 = (
            r.status_code == 404
            or "Channel not found" in _err_text
            or "42240402" in _err_text
            or "Wrong parameters" in _err_text
        )
        if _is_channel_404:
            if not _tried_reverse:
                _tried_reverse = True
                alt_id = _reverse_channel_id(payload["channelId"])
                if alt_id != payload["channelId"]:
                    on_log(f"[YAHOO-MEDIA] video 404,試反向 channelId: {alt_id}")
                    payload["channelId"] = alt_id
                    continue
            if not _tried_mark_read:
                _tried_mark_read = True
                on_log(f"[YAHOO-MEDIA] video 404 → mark_read 開通 channel 後 retry")
                try:
                    im_mark_read(
                        profile_dir, payload["channelId"],
                        buyer_cid=receiver, on_log=on_log,
                    )
                except Exception as _e_mr2:
                    on_log(f"[YAHOO-MEDIA] video mark_read 開通異常: {_e_mr2}")
                continue
        return False, f"video send {r.status_code}: {r.text[:300]}"
      except Exception as e:
        if attempt < 2:
            time.sleep(2)
            continue
        return False, f"video send 異常: {e}"
    return False, "video send 重試耗盡"


def send_voice_message(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    voice_bytes: bytes,
    mime: str = "audio/mp4",
    duration_sec: int = 0,
    on_log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """純 HTTP 發送語音給 Yahoo IM 對方。

    ⚠️ UNVERIFIED:套圖片 4-step 模板,type="voice"。
    """
    on_log = on_log or (lambda *_: None)

    finalize, err = upload_image_to_yahoo(
        profile_dir, voice_bytes, mime=mime, on_log=on_log,
    )
    if not finalize:
        return False, f"語音上傳失敗: {err}"

    channel_id = build_channel_id(shop_id, buyer_id)
    effective_channel = _CHANNEL_DIR_CACHE.get(channel_id, channel_id)
    receiver = f"Y{buyer_id.lower().lstrip('y')}"

    session, wssid, err = _build_session(profile_dir, buyer_cid=receiver)
    if not session:
        return False, f"session 不可用: {err}"

    payload = {
        "onlyValidate": False,
        "channelId": effective_channel,
        "type": "voice",
        "value": {
            "isLoading": False,
            "id": finalize["id"],
            "url": finalize["url"],
            "duration": duration_sec,
        },
        "receiver": receiver,
        "wssid": wssid,
    }

    try:
        r = session.post(_IM_SEND, json=payload, timeout=20)
        if r.status_code == 200:
            on_log(f"[YAHOO-MEDIA] send voice OK id={finalize.get('id')}")
            # v6.0.83:send 後自動 mark_read
            try:
                from .im_bosh_ops import bosh_mark_read
                import threading as _th
                _th.Thread(
                    target=lambda: bosh_mark_read(profile_dir, effective_channel, on_log=on_log),
                    daemon=True,
                ).start()
            except Exception:
                pass
            return True, f"語音已發送"
        return False, f"voice send {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"voice send 異常: {e}"


def send_video_from_url(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    video_url: str,
    on_log: Optional[LogFn] = None,
    download_timeout: int = 60,
    snapshot_url: str = "",
) -> Tuple[bool, str]:
    """便利函數:從 URL 下載視頻 → 發送(供閒魚→Yahoo 視頻中轉)。"""
    on_log = on_log or (lambda *_: None)
    on_log(f"[YAHOO-MEDIA] 下載源視頻: {video_url[:80]}")
    try:
        r = requests.get(video_url, timeout=download_timeout, allow_redirects=True)
        if r.status_code != 200:
            return False, f"下載源視頻 {r.status_code}"
        video_bytes = r.content
        if not video_bytes or len(video_bytes) < 100:
            return False, f"源視頻空檔/過小 size={len(video_bytes)}"
    except Exception as e:
        return False, f"下載源視頻異常: {e}"

    # 推 mime — Pixelframe finalize 會自動轉碼 + 生成 thumbnail,不需要 client 上傳縮圖
    mime = "video/mp4"
    if video_url.lower().endswith(".webm"):
        mime = "video/webm"
    elif video_url.lower().endswith(".mov"):
        mime = "video/quicktime"

    return send_video_message(
        profile_dir,
        shop_id=shop_id, buyer_id=buyer_id,
        video_bytes=video_bytes, mime=mime,
        on_log=on_log,
    )


# v6.1.53:Yahoo IM 視頻 30 秒硬上限。用 29 秒留 1 秒餘量(避免 Server 拒絕邊界值)
# 2 分鐘視頻 → ceil(120/29)=5 段,每段 24s
YAHOO_IM_VIDEO_MAX_SEC = 29


def _probe_video_duration(video_path: str, on_log: Optional[LogFn] = None) -> float:
    """用 imageio-ffmpeg 拿視頻時長(秒)。失敗回 0。"""
    _log = on_log or (lambda *_: None)
    try:
        import imageio_ffmpeg
        import subprocess
        import re
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run(
            [ff, '-i', video_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=30,
        )
        m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
        if m:
            h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h*3600 + mn*60 + s
        _log(f"[YAHOO-MEDIA] probe duration:正則沒匹配 ffmpeg 輸出")
    except Exception as e:
        _log(f"[YAHOO-MEDIA] probe duration 異常: {e}")
    return 0.0


def _split_video_for_yahoo_im(
    video_path: str,
    max_sec: int = YAHOO_IM_VIDEO_MAX_SEC,
    on_log: Optional[LogFn] = None,
) -> Tuple[List[str], float]:
    """切分視頻成 ≤max_sec 秒多段。

    策略:
    - duration ≤ max_sec → 不切,回 [原檔]
    - 否則 N = ceil(duration/max_sec),平均切 N 段(各段 ≈ duration/N)
    - 用 -c copy(stream copy 不重編碼) — 速度 < 1 秒

    Returns: (segment_paths, original_duration)
    失敗時 segment_paths 為空 list
    """
    import math
    import subprocess
    import tempfile
    _log = on_log or (lambda *_: None)

    duration = _probe_video_duration(video_path, on_log=_log)
    if duration <= 0:
        _log(f"[YAHOO-MEDIA] split:無法取得 duration,當作 ≤max_sec 不切")
        return [video_path], 0.0
    if duration <= max_sec:
        return [video_path], duration

    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        _log(f"[YAHOO-MEDIA] split:imageio_ffmpeg 不可用({e}),只送原檔(可能超時失敗)")
        return [video_path], duration

    n_segments = math.ceil(duration / max_sec)
    seg_dur = duration / n_segments
    _log(f"[YAHOO-MEDIA] split:{duration:.1f}s → 切 {n_segments} 段,每段 {seg_dur:.1f}s")

    tmpdir = Path(tempfile.gettempdir()) / 'yahoo_video_split'
    tmpdir.mkdir(parents=True, exist_ok=True)
    base = Path(video_path).stem

    segments = []
    for i in range(n_segments):
        start = i * seg_dur
        out = tmpdir / f'{base}_seg{i+1}_{int(time.time())}.mp4'
        cmd = [
            ff, '-y', '-i', video_path,
            '-ss', f'{start:.2f}',
            '-t', f'{seg_dur:.2f}',
            '-c', 'copy',
            '-avoid_negative_ts', 'make_zero',
            str(out),
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120,
            )
        except Exception as e:
            _log(f"[YAHOO-MEDIA] split 段 {i+1} subprocess 異常:{e}")
            return [], duration
        if r.returncode != 0:
            _log(f"[YAHOO-MEDIA] split 段 {i+1} 失敗:{r.stderr[-300:]}")
            return [], duration
        if not out.exists() or out.stat().st_size < 1000:
            _log(f"[YAHOO-MEDIA] split 段 {i+1} 輸出太小/不存在")
            return [], duration
        _log(f"[YAHOO-MEDIA] split 段 {i+1} OK:{out.stat().st_size} bytes")
        segments.append(str(out))

    return segments, duration


def send_video_from_url_autosplit(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    video_url: str,
    on_log: Optional[LogFn] = None,
    download_timeout: int = 60,
) -> Tuple[bool, str]:
    """v6.1.53:從 URL 下載視頻 → 若 > 30s 自動切分 → 依序發送。

    用於 TG forum reply / 閒魚→Yahoo 中轉 等場景。
    """
    import tempfile
    _log = on_log or (lambda *_: None)
    _log(f"[YAHOO-MEDIA] autosplit:下載源視頻 {video_url[:80]}")
    try:
        r = requests.get(video_url, timeout=download_timeout, allow_redirects=True)
        if r.status_code != 200:
            return False, f"下載源視頻 {r.status_code}"
        video_bytes = r.content
        if not video_bytes or len(video_bytes) < 100:
            return False, f"源視頻空檔/過小 size={len(video_bytes)}"
    except Exception as e:
        return False, f"下載源視頻異常: {e}"

    # 暫存到 disk 給 ffmpeg 處理(ffmpeg 不接 stdin pipe 太複雜)
    tmpdir = Path(tempfile.gettempdir()) / 'yahoo_video_split'
    tmpdir.mkdir(parents=True, exist_ok=True)
    src_tmp = tmpdir / f'src_{int(time.time())}_{buyer_id}.mp4'
    try:
        src_tmp.write_bytes(video_bytes)
    except Exception as e:
        return False, f"寫源視頻到暫存失敗: {e}"

    try:
        segments, dur = _split_video_for_yahoo_im(
            str(src_tmp), max_sec=YAHOO_IM_VIDEO_MAX_SEC, on_log=_log,
        )
        if not segments:
            return False, f"視頻切分失敗(原 {dur:.1f}s)"

        mime = "video/mp4"
        if video_url.lower().endswith(".webm"):
            mime = "video/webm"
        elif video_url.lower().endswith(".mov"):
            mime = "video/quicktime"

        # v6.1.53:聚合所有段的 msgId,讓撤回功能可以一次撤回所有段
        import re as _re_mid
        sent_msg_ids = []

        if len(segments) == 1:
            # 不需切分,直接送
            with open(segments[0], 'rb') as f:
                seg_bytes = f.read()
            ok, info = send_video_message(
                profile_dir,
                shop_id=shop_id, buyer_id=buyer_id,
                video_bytes=seg_bytes, mime=mime,
                on_log=_log,
            )
            return ok, info  # info 已含 msgId=...

        # 多段順序送 — 收集每段的 msgId
        n = len(segments)
        _log(f"[YAHOO-MEDIA] autosplit:送 {n} 段...")
        for i, seg_path in enumerate(segments, 1):
            try:
                with open(seg_path, 'rb') as f:
                    seg_bytes = f.read()
            except Exception as e:
                # 已送的段不撤回(段間延遲,撤回需要額外 BOSH 呼叫,讓 caller 處理)
                if sent_msg_ids:
                    return False, (f"段 {i}/{n} 讀取失敗(已送 {len(sent_msg_ids)} 段): {e} "
                                   f"msgIds={','.join(sent_msg_ids)}")
                return False, f"段 {i}/{n} 讀取失敗: {e}"
            ok, info = send_video_message(
                profile_dir,
                shop_id=shop_id, buyer_id=buyer_id,
                video_bytes=seg_bytes, mime=mime,
                on_log=_log,
            )
            if not ok:
                if sent_msg_ids:
                    return False, (f"段 {i}/{n} 發送失敗(已送 {len(sent_msg_ids)} 段): {info} "
                                   f"msgIds={','.join(sent_msg_ids)}")
                return False, f"段 {i}/{n} 發送失敗: {info}"
            # 從 info 抽 msgId
            _m_mid = _re_mid.search(r'msgId=([A-Za-z0-9-]+)', info or '')
            if _m_mid:
                sent_msg_ids.append(_m_mid.group(1))
            _log(f"[YAHOO-MEDIA] autosplit:段 {i}/{n} 送出 ✓")
            # 段間短停頓避免 Yahoo 限流
            if i < n:
                time.sleep(1.5)

        # 把所有段 msgId 用逗號分隔放在 info 末尾,供 caller 寫 mapping 撤回用
        if sent_msg_ids:
            return True, f"視頻 {dur:.1f}s 切分 {n} 段全部送達 msgIds={','.join(sent_msg_ids)}"
        return True, f"視頻 {dur:.1f}s 切分 {n} 段全部送達"
    finally:
        # 清理暫存
        try:
            src_tmp.unlink(missing_ok=True)
        except Exception:
            pass


def send_image_from_url(
    profile_dir: Path,
    *,
    shop_id: str,
    buyer_id: str,
    image_url: str,
    on_log: Optional[LogFn] = None,
    download_timeout: int = 30,
) -> Tuple[bool, str]:
    """便利函數:從 URL 下載圖片 → 自動發送給 Yahoo 買家。

    用於「閒魚賣家媒體 → Yahoo 買家中轉直通」場景。
    """
    on_log = on_log or (lambda *_: None)
    on_log(f"[YAHOO-MEDIA] 下載源圖: {image_url[:80]}")
    try:
        r = requests.get(image_url, timeout=download_timeout, allow_redirects=True)
        if r.status_code != 200:
            return False, f"下載源圖 {r.status_code}"
        image_bytes = r.content
        if not image_bytes:
            return False, "源圖空檔"
    except Exception as e:
        return False, f"下載源圖異常: {e}"

    # 推 mime
    mime = "image/jpeg"
    if image_bytes.startswith(b"\x89PNG"):
        mime = "image/png"
    elif image_bytes.startswith(b"GIF8"):
        mime = "image/gif"
    elif image_bytes.startswith(b"\xff\xd8"):
        mime = "image/jpeg"

    on_log(f"[YAHOO-MEDIA] 下載 {len(image_bytes)} bytes mime={mime},轉發給 Yahoo 買家")
    return send_image_message(
        profile_dir,
        shop_id=shop_id, buyer_id=buyer_id,
        image_bytes=image_bytes, mime=mime,
        on_log=on_log,
    )
