"""多 TG 用戶 ↔ Yahoo 帳號 權限隔離 (v6.0.83)

多個同事綁定同一個 bot,但每人只能看自己負責的 Yahoo 帳號 IM。

設定檔: tg_user_accounts.json (項目根目錄)
{
  "<SUPERVISOR_CHAT_ID>": {
    "name": "主管",
    "accounts": ["xian678", "zheng2414", "kinhuaw168"],
    "is_admin": true       # admin 可看所有帳號 + 管理 acl
  },
  "12345678": {
    "name": "客服小張",
    "accounts": ["xiao567"]
  }
}

啟動時讀,動態變更需重啟。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

log = logging.getLogger(__name__)


class TGUserACL:
    """TG user_id → 負責的 Yahoo profile_id 列表。"""

    def __init__(self, base_dir: Path):
        self.path = Path(base_dir) / "tg_user_accounts.json"
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._loaded_at = 0.0
        self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = raw if isinstance(raw, dict) else {}
                else:
                    self._data = {}
                self._loaded_at = time.time()
            except Exception as e:
                log.warning("TGUserACL reload failed: %s", e)
                self._data = {}

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("TGUserACL save failed: %s", e)

    def get_user_entry(self, user_id: str) -> Dict[str, Any]:
        """返回 user_id 對應 entry {name, accounts: [...], is_admin: bool}。"""
        with self._lock:
            return dict(self._data.get(str(user_id)) or {})

    def get_allowed_accounts(self, user_id: str, all_accounts: List[str]) -> List[str]:
        """返回該 user 允許訪問的 profile_id 列表。

        - admin → 所有 all_accounts
        - 一般 user → entry.accounts 與 all_accounts 交集
        - 未在 ACL 中的 user → 空清單
        """
        entry = self.get_user_entry(user_id)
        if not entry:
            return []
        if entry.get("is_admin"):
            return list(all_accounts)
        allowed = entry.get("accounts") or []
        if not isinstance(allowed, list):
            return []
        # 取交集
        all_set = set(all_accounts)
        return [a for a in allowed if a in all_set]

    def is_admin(self, user_id: str) -> bool:
        return bool(self.get_user_entry(user_id).get("is_admin"))

    def can_access(self, user_id: str, profile_id: str) -> bool:
        """這個 TG user 是否能訪問該 profile_id。"""
        entry = self.get_user_entry(user_id)
        if not entry:
            return False
        if entry.get("is_admin"):
            return True
        return profile_id in (entry.get("accounts") or [])

    def list_known_users(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())

    # ─── admin API:管理 ACL ───

    def set_user_accounts(
        self,
        user_id: str,
        accounts: List[str],
        *,
        name: str = "",
        is_admin: bool = False,
    ) -> None:
        with self._lock:
            entry = self._data.get(str(user_id)) or {}
            entry["accounts"] = list(accounts)
            if name:
                entry["name"] = name
            if is_admin:
                entry["is_admin"] = True
            self._data[str(user_id)] = entry
            self._save()

    def remove_user(self, user_id: str) -> bool:
        with self._lock:
            if str(user_id) in self._data:
                del self._data[str(user_id)]
                self._save()
                return True
            return False
