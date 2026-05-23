"""v6.1:本機 instance_id 識別。

供:
- updater.pyw — check_update 時帶 user_id 讓 Worker 過濾 target_users
- app.py — window title 顯示給用戶看(同事能直接告訴主管自己 ID)

優先順序:
1. settings.json `instance_id` 欄位(明確指定最優先)
2. settings.json `tg_chat_id`(若 != 主管預設 <SUPERVISOR_CHAT_ID>)
3. 機器 hostname(Windows COMPUTERNAME / 其他 socket.gethostname())
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path


_SUPERVISOR_CHAT_ID = "<SUPERVISOR_CHAT_ID>"  # 主管預設 ID,settings.tg_chat_id 等於這值代表是「接收主管通知」不是自己 ID


def get_instance_id(base_dir: str | Path = ".") -> tuple[str, str]:
    """取本機 instance_id 給 Worker 識別 / GUI 顯示。

    Args:
        base_dir: 包含 settings.json 的目錄(預設當前目錄)

    Returns:
        (instance_id, source) — source 是 'instance_id' / 'tg_chat_id' / 'hostname'
    """
    base_dir = Path(base_dir)
    try:
        fp = base_dir / "settings.json"
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                settings = json.load(f)
            iid = str(settings.get("instance_id", "") or "").strip()
            if iid:
                return iid, "instance_id"
            tcid = str(settings.get("tg_chat_id", "") or "").strip()
            if tcid and tcid != _SUPERVISOR_CHAT_ID:
                return tcid, "tg_chat_id"
    except Exception:
        pass

    # fallback hostname
    try:
        hn = (os.environ.get("COMPUTERNAME") or "").strip()
        if hn:
            return hn, "hostname"
        return socket.gethostname(), "hostname"
    except Exception:
        return "", "none"
