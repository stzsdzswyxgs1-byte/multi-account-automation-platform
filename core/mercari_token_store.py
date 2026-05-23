"""煤炉(Mercari) Token 缓存 — 从 purchase_monitor 浏览器 Profile 提取并管理 Mercari auth token。

架构:
  login_browser (Playwright) 成功后 → save_mercari_token()
  HTTP order fetch → load_mercari_token() → 获得 accessToken

文件位置: profiles/purchase_monitor/mercari_token_cache.json

认证机制 (不同于闲鱼的 cookie+mtop 签名):
  - Authorization header = accessToken (从 localStorage.authTokenData 提取)
  - Dpop header = 每次请求生成的 ES256 JWT (不需要持久化)
  - 不依赖 cookies
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

MERCARI_CACHE_FILE = "mercari_token_cache.json"

# 煤炉登录态持久有效，不主动判定过期。
# 只在 API 返回 401 时才认为 token 失效，此时从 LevelDB 重新提取。
MERCARI_TOKEN_MAX_AGE = 86400 * 365  # 不限时

# ── 工具函数 ──────────────────────────────────────────

def _cache_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / MERCARI_CACHE_FILE


# ── 保存 ──────────────────────────────────────────────

def save_mercari_token(
    profile_dir: Path,
    access_token: str,
    expiration_ms: int = 0,
    user_id: str = "",
) -> bool:
    """保存 Mercari accessToken 到缓存文件。

    Args:
        profile_dir: profile 目录
        access_token: localStorage.authTokenData.accessToken
        expiration_ms: token 过期时间 (Unix ms)
        user_id: mercari user id (e.g. "238492048")
    """
    if not access_token:
        return False

    fp = _cache_path(Path(profile_dir))
    data = {
        "access_token": access_token,
        "expiration_ms": expiration_ms,
        "user_id": user_id,
        "saved_at": time.time(),
        "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[mercari_token] 保存成功: user=%s, token=%s...",
                 user_id, access_token[:20])
        return True
    except Exception as e:
        log.error("[mercari_token] 保存失败: %s", e)
        return False


# ── 加载 ──────────────────────────────────────────────

def load_mercari_token(
    profile_dir: Path,
    max_age: float = MERCARI_TOKEN_MAX_AGE,
) -> Tuple[str, str]:
    """从缓存加载 Mercari accessToken。

    Returns:
        (access_token, user_id)
        access_token 为 "" 表示不可用。
    """
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return "", ""

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return "", ""

    access_token = data.get("access_token", "")
    if not access_token:
        return "", ""

    user_id = data.get("user_id", "")
    return access_token, user_id


# ── 失效 ──────────────────────────────────────────────

def invalidate_mercari_token(profile_dir: Path) -> None:
    """标记 Mercari token 缓存失效。"""
    fp = _cache_path(Path(profile_dir))
    if not fp.exists():
        return
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        data["expiration_ms"] = 0
        data["saved_at"] = 0
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── 从 Profile 提取 token ─────────────────────────────

def extract_token_from_profile(
    profile_dir: Path,
    chrome_path: str = "",
) -> bool:
    """直读 LevelDB 文件提取 Mercari accessToken 并保存到缓存。

    不需要打开浏览器，直接从 Chrome Profile 的 Local Storage 读取。

    从 localStorage.authTokenData 提取:
      - accessToken
      - expiration (ms)
      - idToken.sub → user_id
    """
    profile_dir = Path(profile_dir)
    ls_dir = profile_dir / "Default" / "Local Storage" / "leveldb"
    if not ls_dir.exists():
        log.warning("[mercari_token] LevelDB 目录不存在: %s", ls_dir)
        return False

    for ldb in sorted(ls_dir.glob("*.ldb"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            raw = ldb.read_bytes()
            # LevelDB 中 key 格式: _https://jp.mercari.com\x00\x01authTokenData
            # value 紧跟在 key 后面
            marker = b'\x01authTokenData'
            idx = raw.find(marker)
            if idx < 0:
                continue

            # 跳过 LevelDB 内部编码字节，找到 JSON 起始 {"
            search_start = idx + len(marker)
            json_start = raw.find(b'{"', search_start, search_start + 50)
            if json_start < 0:
                continue

            # 提取完整 JSON（匹配花括号深度）
            depth = 0
            json_end = json_start
            for i in range(json_start, min(json_start + 5000, len(raw))):
                if raw[i:i+1] == b'{':
                    depth += 1
                elif raw[i:i+1] == b'}':
                    depth -= 1
                    if depth == 0:
                        json_end = i + 1
                        break

            if json_end <= json_start:
                continue

            json_str = raw[json_start:json_end].decode("utf-8", errors="ignore")
            log.info("[mercari_token] LevelDB 提取到 authTokenData (%d bytes)", len(json_str))
            return _parse_and_save(profile_dir, json_str)

        except Exception as e:
            log.debug("[mercari_token] LevelDB 读取 %s 失败: %s", ldb.name, e)
            continue

    log.warning("[mercari_token] LevelDB 中未找到 authTokenData")
    return False


def _parse_and_save(profile_dir: Path, raw_json: str) -> bool:
    """解析 authTokenData JSON 并保存到缓存。"""
    try:
        auth_data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception as e:
        log.error("[mercari_token] JSON 解析失败: %s", e)
        return False

    access_token = auth_data.get("accessToken", "")
    if not access_token:
        log.warning("[mercari_token] accessToken 为空")
        return False

    expiration = auth_data.get("expiration", 0)
    id_token = auth_data.get("idToken", {})
    if isinstance(id_token, dict):
        sub = id_token.get("sub", "")
    else:
        sub = ""
    user_id = sub.replace("mercari:", "") if sub.startswith("mercari:") else sub

    return save_mercari_token(profile_dir, access_token, expiration, user_id)
