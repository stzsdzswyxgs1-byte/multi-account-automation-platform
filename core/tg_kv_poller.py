"""Cloudflare Worker KV 轮询模块

替代原来的 Telegram getUpdates 长轮询。
每个 Bot 实例调用 Worker 的 /poll/{botType}/{userId} 接口，
获取属于自己的消息队列，格式与 Telegram update 完全一致。

配置文件: tg_relay_config.json
{
    "user_id": "123456789"
}

worker_url 和 api_key 在下方默认值中统一配置（部署后改一次即可）。
每个同事只需要在 tg_relay_config.json 填自己的 TG user_id。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
RELAY_CONFIG_FILE = ROOT_DIR / "tg_relay_config.json"

# ========== 默认值（部署 Worker 后在这里改一次） ==========
# 部署完成后，把下面两个值改成你实际的 Worker URL 和 API Key
DEFAULT_WORKER_URL = "https://square-river-tg.<PHONE_REDACTED>.workers.dev"
DEFAULT_API_KEY = "<WORKER_API_KEY_REDACTED>"


def load_relay_config() -> Dict[str, str]:
    """加载中转配置。

    优先级：tg_relay_config.json > settings.json 的 tg_chat_id > 代码默认值
    每个同事只需要在软件界面填 Chat ID 即可，无需手动编辑 json。
    """
    cfg: Dict[str, str] = {
        "worker_url": DEFAULT_WORKER_URL,
        "api_key": DEFAULT_API_KEY,
        "user_id": "",
    }
    # 先尝试从 settings.json 读取 tg_chat_id 作为 user_id 备选
    settings_file = ROOT_DIR / "settings.json"
    try:
        if settings_file.exists():
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            chat_id = str(settings.get("tg_chat_id", "") or "").strip()
            if chat_id:
                cfg["user_id"] = chat_id
    except Exception:
        pass
    # tg_relay_config.json 的值优先级更高，会覆盖上面的
    try:
        if RELAY_CONFIG_FILE.exists():
            with open(RELAY_CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            for k in ("worker_url", "api_key", "user_id"):
                v = str(file_cfg.get(k, "") or "").strip()
                if v:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


class KvPoller:
    """从 Cloudflare Worker KV 轮询消息，替代 getUpdates。

    用法:
        poller = KvPoller("ops", config, on_log)
        updates = poller.poll()  # 返回 List[Dict]，格式同 Telegram update
    """

    def __init__(
        self,
        bot_type: str,
        config: Dict[str, str],
        on_log: Callable[[str], None],
    ):
        self.bot_type = bot_type
        self.worker_url = (config.get("worker_url") or "").rstrip("/")
        self.api_key = config.get("api_key") or ""
        self.user_id = config.get("user_id") or ""
        self.on_log = on_log
        self._backoff: float = 1.0

    @property
    def is_configured(self) -> bool:
        return bool(self.worker_url and self.api_key and self.user_id)

    def poll(self, raise_on_error: bool = False) -> List[Dict[str, Any]]:
        """轮询一次 KV，返回 updates 列表。

        v6.2:加 raise_on_error 參數(向後兼容 — 預設 False 沿用舊行為)。
        - raise_on_error=False(舊):異常吞掉 + log + 返回 [](caller 無法區分失敗/空)
        - raise_on_error=True(新):異常 raise(由 run_kv_poll_loop 統一處理 backoff/降噪)
        """
        if not self.is_configured:
            return []
        url = f"{self.worker_url}/poll/{self.bot_type}/{self.user_id}"
        try:
            r = requests.get(
                url,
                params={"key": self.api_key},
                timeout=15,
            )
            data = r.json()
            if data.get("ok"):
                self._backoff = 1.0
                return data.get("updates", [])
            else:
                err = data.get("error", "unknown")
                if raise_on_error:
                    raise RuntimeError(f"server: {err}")
                self.on_log(
                    f"[KV-{self.bot_type.upper()}] 轮询失败: {err}"
                )
                return []
        except Exception as e:
            if raise_on_error:
                raise
            self.on_log(f"[KV-{self.bot_type.upper()}] 轮询异常: {e}")
            return []

    def get_backoff(self) -> float:
        """返回当前退避时间（秒）。"""
        return self._backoff

    def increase_backoff(self) -> None:
        """增加退避时间（指数退避，最大 60 秒）。"""
        self._backoff = min(self._backoff * 2, 60.0)

    def reset_backoff(self) -> None:
        self._backoff = 1.0
