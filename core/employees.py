"""多同事(multi-tenant)管理 — 每個同事一個 TG group。

employees.json 結構:
{
  "alice": {
    "name": "Alice 中文名",
    "tg_user_id": "111111",         # 同事 TG user_id (用來識別他自己 DM)
    "forum_chat_id": "-1001111",    # 同事自己的 supergroup chat_id
    "dm_chat_id": "111111",         # 同事自己的 DM chat_id (= tg_user_id)
    "accounts": ["kinhuaw168", "chen749"],  # 負責的 Yahoo 帳號 (profile_id)
    "created_ts": 1778900000
  },
  ...
}

路由邏輯:
- 新訊息 conv → 看 conv.profile_id → 找 owner employee → push 到 owner.forum_chat_id
- 沒綁定的 profile_id → fallback 到主管 supergroup(設定 supervisor_forum_chat_id)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STORE: Optional[Dict[str, Any]] = None
# 用絕對路徑(從 module 位置回推),避開 cwd 變化造成的讀寫不一致
_PATH: Path = Path(__file__).resolve().parent.parent / "employees.json"

# 邀請碼:主管生成,30 分鐘有效,一次性消費
# code → {name, accounts, expires_ts, created_by}
_INVITES: Dict[str, Dict[str, Any]] = {}
_INVITES_LOCK = threading.Lock()


def _load() -> Dict[str, Any]:
    global _STORE
    if _STORE is not None:
        return _STORE
    try:
        if _PATH.exists():
            _STORE = json.loads(_PATH.read_text(encoding="utf-8"))
        else:
            _STORE = {}
    except Exception:
        _STORE = {}
    return _STORE


def _save() -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_STORE, ensure_ascii=False, indent=2), encoding="utf-8")
        import os
        os.replace(str(tmp), str(_PATH))
    except Exception as e:
        log.warning("save employees.json failed: %s", e)


def bind_employee(
    name: str,
    *,
    tg_user_id: str = "",
    forum_chat_id: str = "",
    accounts: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """綁定一個同事(新建 or update)。"""
    with _LOCK:
        data = _load()
        existing = data.get(name) or {}
        entry = dict(existing)
        if tg_user_id:
            entry["tg_user_id"] = str(tg_user_id)
            entry["dm_chat_id"] = str(tg_user_id)
        if forum_chat_id:
            entry["forum_chat_id"] = str(forum_chat_id)
        if accounts is not None:
            entry["accounts"] = list(accounts)
        entry.setdefault("name", name)
        entry.setdefault("created_ts", time.time())
        data[name] = entry
        _save()
        return True, ""


def unbind_employee(name: str) -> Tuple[bool, str]:
    with _LOCK:
        data = _load()
        if name not in data:
            return False, f"{name} 不在綁定列表"
        del data[name]
        _save()
        return True, ""


def list_employees() -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        return dict(_load())


def find_owner_by_account(profile_id: str) -> Optional[Dict[str, Any]]:
    """根據 Yahoo 帳號 profile_id 找對應同事 entry。沒綁定返 None。"""
    if not profile_id:
        return None
    with _LOCK:
        data = _load()
        for name, entry in data.items():
            if profile_id in (entry.get("accounts") or []):
                return {"name": name, **entry}
    return None


def find_employee_by_tg_user_id(tg_user_id: str) -> Optional[Dict[str, Any]]:
    """根據 TG user_id 找同事 entry。用於辨識「指令發起者是哪個同事」。"""
    tg_user_id = str(tg_user_id or "")
    if not tg_user_id:
        return None
    with _LOCK:
        data = _load()
        for name, entry in data.items():
            if str(entry.get("tg_user_id", "")) == tg_user_id:
                return {"name": name, **entry}
    return None


def find_employee_by_forum_chat_id(forum_chat_id: str) -> Optional[Dict[str, Any]]:
    """根據 supergroup chat_id 找同事 entry。bot 收到 group 訊息時用。"""
    forum_chat_id = str(forum_chat_id or "")
    if not forum_chat_id:
        return None
    with _LOCK:
        data = _load()
        for name, entry in data.items():
            if str(entry.get("forum_chat_id", "")) == forum_chat_id:
                return {"name": name, **entry}
    return None


def reload() -> None:
    """強制重新從磁碟讀取(供 manual 編輯 json 後即時生效)。"""
    global _STORE
    with _LOCK:
        _STORE = None
        _load()


# ── 邀請碼 onboarding ──

def create_invite(
    name: str,
    accounts: List[str],
    *,
    created_by: str = "",
    expires_in_sec: int = 1800,
) -> str:
    """主管生成邀請碼。同事在 group 內打 /join CODE 完成綁定。

    Args:
        name: 同事識別名(如 alice / bob)
        accounts: 該同事負責的 Yahoo profile_id 列表
        created_by: 主管 TG user_id(audit)
        expires_in_sec: 有效期(預設 30 分鐘)

    Returns:
        邀請碼字串(8 字元易讀格式)
    """
    import random
    # 避開易混淆字 0/O/1/I/L
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    code = "".join(random.choice(alphabet) for _ in range(8))
    with _INVITES_LOCK:
        _INVITES[code] = {
            "name": name,
            "accounts": list(accounts),
            "created_by": created_by,
            "expires_ts": time.time() + expires_in_sec,
            "created_ts": time.time(),
        }
        # 清過期 invites(順手)
        now = time.time()
        expired = [c for c, v in _INVITES.items() if v.get("expires_ts", 0) < now]
        for c in expired:
            del _INVITES[c]
    return code


def consume_invite(
    code: str,
    *,
    tg_user_id: str,
    forum_chat_id: str,
) -> Tuple[bool, str, Dict[str, Any]]:
    """同事在 group 內打 /join CODE 觸發。

    驗證邀請碼 + 寫綁定 + 消費(一次性)。

    Returns:
        (success, message, employee_entry)
        - success=False:message 是錯誤原因
        - success=True:message 是同事名,entry 是完整綁定資訊
    """
    if not code:
        return False, "邀請碼為空", {}
    code = code.strip().upper()
    with _INVITES_LOCK:
        inv = _INVITES.get(code)
        if not inv:
            return False, "邀請碼無效或已被使用", {}
        if time.time() > inv.get("expires_ts", 0):
            del _INVITES[code]
            return False, "邀請碼已過期(30 分鐘有效)", {}
        # 消費(刪掉)
        name = inv["name"]
        accounts = inv["accounts"]
        del _INVITES[code]

    ok, err = bind_employee(
        name, tg_user_id=tg_user_id,
        forum_chat_id=forum_chat_id, accounts=accounts,
    )
    if not ok:
        return False, f"綁定失敗: {err}", {}
    return True, name, {
        "name": name,
        "tg_user_id": tg_user_id,
        "forum_chat_id": forum_chat_id,
        "accounts": accounts,
    }


def list_active_invites() -> Dict[str, Dict[str, Any]]:
    """列出未過期 invites(主管查看用)。"""
    now = time.time()
    with _INVITES_LOCK:
        return {
            c: dict(v) for c, v in _INVITES.items()
            if v.get("expires_ts", 0) > now
        }
