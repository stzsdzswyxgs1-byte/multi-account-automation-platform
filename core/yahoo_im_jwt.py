"""Yahoo IM BOSH JWT 純 HTTP 取得 (v6.0.83+) — 取代 Playwright 攔截。

适配自 Yahoo 自家 chunk `im.e6ef795486ee172ccc35.js`:

    yp.decryptApiToken = (r, n) => {
      const a = um(n, sm);  // sm = 16
      const s = CryptoJS.lib.CipherParams.create({ciphertext: CryptoJS.enc.Base64.parse(r)});
      return CryptoJS.AES.decrypt(s, CryptoJS.enc.Utf8.parse(a), {
        mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7,
        iv: CryptoJS.enc.Utf8.parse(a),  // ⚠️ IV == Key
      }).toString(CryptoJS.enc.Utf8);
    };

    const um = (r, n) => {
      let a = "";
      while (a.length < n) a += r;
      return a.substring(0, n);
    };

完整流程:
1. GET /fe/api/im/user → 拿 {id, wssid, token(920 字加密), guid, esid}
2. AES-128-CBC decrypt token (key=IV=(wssid 重複到 16 字)) → plain JWT 683 字
3. 用 plain JWT 做 BOSH SASL PLAIN auth(im_bosh_ops.py 已有實作)

實機驗證 2026-05-15:
  wssid='exampleSSID' → key=b'exampleSSIDexamp'
  encrypted 920 字 → decrypted 683 字 JWT (eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9... RS256)
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Callable, Optional, Tuple, Dict, Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

log = logging.getLogger(__name__)
LogFn = Callable[[str], None]


# ── 解密 ──────────────────────────────────────────


def _derive_key(wssid: str, n: int = 16) -> bytes:
    """重現 JS `um(r, n)`:重複 wssid 直到 ≥n 字,截前 n 字 UTF-8 encode。"""
    if not wssid:
        return b""
    a = ""
    while len(a) < n:
        a += wssid
    return a[:n].encode("utf-8")


def decrypt_api_token(encrypted_b64: str, wssid: str) -> str:
    """重現 `yp.decryptApiToken`:AES-128-CBC + PKCS7 + IV=Key=derive_key(wssid)。

    Returns: plain JWT 字串(RS256 三段式)。失敗返回 ""。
    """
    if not encrypted_b64 or not wssid:
        return ""
    try:
        key = _derive_key(wssid, 16)
        ct = base64.b64decode(encrypted_b64)
        cipher = AES.new(key, AES.MODE_CBC, iv=key)
        pt = unpad(cipher.decrypt(ct), AES.block_size)
        return pt.decode("utf-8")
    except Exception as e:
        log.warning("decrypt_api_token failed: %s", e)
        return ""


# ── 純 HTTP 拿 plain JWT ──────────────────────────


def fetch_im_user_info(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """GET /fe/api/im/user — 拿 user 物件含 encrypted token + wssid。

    用既有 cookie / wssid 機制(im_http_ops._build_session)。
    """
    on_log = on_log or (lambda *_: None)
    try:
        from .im_http_ops import _build_session
    except Exception as e:
        return None, f"import _build_session 失敗: {e}"

    session, _existing_wssid, err = _build_session(profile_dir)
    if not session:
        return None, f"session 不可用: {err}"

    try:
        r = session.get("https://tw.bid.yahoo.com/fe/api/im/user", timeout=15)
        if r.status_code != 200:
            return None, f"im/user GET {r.status_code}: {r.text[:200]}"
        d = r.json()
        user = d.get("user") or {}
        if not user.get("token") or not user.get("wssid") or not user.get("id"):
            return None, f"user 物件缺欄位: keys={list(user.keys())}"
        on_log(f"[YAHOO-IM-JWT] /fe/api/im/user OK user={user.get('id')} token_len={len(user.get('token',''))} wssid_len={len(user.get('wssid',''))}")
        return user, ""
    except Exception as e:
        return None, f"im/user 異常: {e}"


def get_plain_jwt(
    profile_dir: Path,
    *,
    on_log: Optional[LogFn] = None,
) -> Tuple[str, str, str]:
    """純 HTTP 拿 BOSH plain JWT。

    Returns: (plain_jwt, user_id, error)
        plain_jwt: 683 字 RS256 JWT;失敗為 ""
        user_id: e.g. "y9000000001"
        error: 錯誤訊息(成功為 "")
    """
    on_log = on_log or (lambda *_: None)
    user, err = fetch_im_user_info(profile_dir, on_log=on_log)
    if not user:
        return "", "", err

    encrypted = user.get("token", "")
    wssid = user.get("wssid", "")
    user_id = user.get("id", "")

    plain = decrypt_api_token(encrypted, wssid)
    if not plain:
        return "", user_id, "decrypt 失敗(token 或 wssid 異常)"
    if not plain.startswith("eyJ"):
        return "", user_id, f"decrypt 結果不像 JWT(開頭:{plain[:10]!r})"

    on_log(f"[YAHOO-IM-JWT] decrypt OK plain JWT len={len(plain)} user={user_id}")
    return plain, user_id, ""


# ── 整合 im_bosh_ops cache:純 HTTP 自動刷新 JWT ────


def ensure_bosh_jwt(
    profile_dir: Path,
    *,
    max_age_sec: float = 3000,  # 50 分鐘(JWT 一般 1 小時有效)
    on_log: Optional[LogFn] = None,
) -> Tuple[str, str, str]:
    """確保有可用的 BOSH JWT(優先 cache,過期則純 HTTP 刷新)。

    Returns: (jwt, user_id, error)
    """
    on_log = on_log or (lambda *_: None)
    try:
        from .im_bosh_ops import load_bosh_cache, save_bosh_cache
    except Exception:
        load_bosh_cache = None
        save_bosh_cache = None

    # 1. 先看 cache
    if load_bosh_cache:
        try:
            jwt, user, captured_at = load_bosh_cache(profile_dir)
            if jwt and (time.time() - captured_at) < max_age_sec:
                on_log(f"[YAHOO-IM-JWT] cache hit age={int(time.time()-captured_at)}s user={user}")
                return jwt, user, ""
        except Exception:
            pass

    # 2. 純 HTTP 刷新
    jwt, user_id, err = get_plain_jwt(profile_dir, on_log=on_log)
    if not jwt:
        return "", user_id, err

    # 3. 寫 cache(讓既有 bosh_mark_read 等使用)
    if save_bosh_cache:
        try:
            save_bosh_cache(profile_dir, jwt, user_id)
            on_log(f"[YAHOO-IM-JWT] 已寫入 cache")
        except Exception as e:
            on_log(f"[YAHOO-IM-JWT] 寫 cache 失敗(不阻塞): {e}")

    return jwt, user_id, ""
