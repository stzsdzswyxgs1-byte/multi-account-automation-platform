"""閒魚 WebSocket deviceId 持久化(v6.0.75)。

deviceId 格式: <UUIDv4>-<my_user_id>
- UUIDv4 部分:首次啟動時生成,寫到本地檔,之後永遠用同一個
  (server 把 deviceId 視為「同一台 PC」識別,不要每次變)
- my_user_id 部分:從閒魚 cookie unb 取(每次都拼接最新值)

範例:8211F4B0-4F37-4AFC-B260-B0DA9F25BF31-2248961152
"""
from __future__ import annotations
import json
import time
import uuid as _uuid
from pathlib import Path
from typing import Optional

# 持久化檔位置(避免污染 profile)
_DEVICE_FILE = Path(__file__).resolve().parent.parent / "runtime" / "goofish_device.json"


def _get_device_uuid() -> str:
    """取得或生成「裝置 UUIDv4」(整個 PC 共用)。"""
    try:
        if _DEVICE_FILE.exists():
            data = json.loads(_DEVICE_FILE.read_text(encoding="utf-8"))
            v = data.get("device_uuid", "")
            if v and len(v) == 36:
                return v
    except Exception:
        pass

    # 生成新的並寫回
    new_uuid = str(_uuid.uuid4()).upper()
    try:
        _DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEVICE_FILE.write_text(
            json.dumps({
                "device_uuid": new_uuid,
                "created_at": time.time(),
                "created_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return new_uuid


def get_device_id(my_user_id: str) -> str:
    """取得完整 deviceId: <PC-UUID>-<user_id>。

    Args:
        my_user_id: 從 cookie unb 取得的當前登入閒魚 userId

    Returns:
        例如 "8211F4B0-4F37-4AFC-B260-B0DA9F25BF31-2248961152"

    v6.0.75:優先用「打開登錄瀏覽器」期間從瀏覽器抓到的完整 device_id
    (跟 token 強綁,server 401 'device id or appkey is not equal' 修復)
    """
    if not my_user_id:
        raise ValueError("my_user_id 不能为空(从 cookie unb 取)")

    # 優先讀 full_device_id (瀏覽器同步的)
    try:
        if _DEVICE_FILE.exists():
            data = json.loads(_DEVICE_FILE.read_text(encoding="utf-8"))
            full = data.get("full_device_id", "")
            # 驗證 full_device_id 是 <UUID>-<user_id> 格式且 user_id 跟當前一致
            if full and full.endswith(f"-{my_user_id}"):
                return full
    except Exception:
        pass

    # fallback: 用 PC-UUID + my_user_id 拼接
    pc_uuid = _get_device_uuid()
    return f"{pc_uuid}-{my_user_id}"


def reset_device_uuid() -> str:
    """強制重新生成裝置 UUID(僅在登入態徹底失效時呼叫)。"""
    try:
        if _DEVICE_FILE.exists():
            _DEVICE_FILE.unlink()
    except Exception:
        pass
    return _get_device_uuid()
