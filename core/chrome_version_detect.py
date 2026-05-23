"""v6.1.62:偵測本機 Chrome 版本 → 動態生成 Yahoo HTTP 訪問用的 UA / Sec-Ch-Ua。

修法目的:
  之前 curl_cffi `impersonate='chrome136'` 預設用 macOS UA,
  軟件 `get_html_headers()` 沒設 User-Agent → curl_cffi 預設 Mac UA 直送出去,
  但軟件業務 headers 設 `Sec-Ch-Ua-Platform: "Windows"`。
  Yahoo 收到「Mac UA + Windows hint」三重矛盾 → spoofed bot → 給 500。

修法:
  - 啟動偵測本機 Chrome 版本(每個 user 機器版本不同 — Chrome 自動更新)
  - 動態組 Windows UA + 對齊本機 major 版本的 Sec-Ch-Ua
  - 軟件業務 headers 強制設 User-Agent 覆蓋 curl_cffi 預設 Mac UA
  - 偵測失敗 → fallback 較新版本(避免「過時 Chrome」標 bot)

只影響 client_runtime_compat 內的 get_html_headers / get_api_headers,scope 限定到 Yahoo HTTP。
閒魚 / 煤炉 / Playwright 全部不受影響。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Fallback 版本 — 偵測失敗時用,選擇較新版本避免「過時 Chrome」相關問題
# 2026-05 真實 Chrome stable 主要在 148-150 之間
_FALLBACK_CHROME_MAJOR = "148"
_FALLBACK_CHROME_FULL = "148.0.7778.168"

# 模組級 cache(避免每次 HTTP request 都跑 subprocess)
_cached_version: Optional[Tuple[str, str]] = None


def _candidate_chrome_paths(extra_path: str = "") -> list[str]:
    """返回 Chrome 可執行檔候選路徑(優先順序)。"""
    paths: list[str] = []
    if extra_path:
        paths.append(extra_path)
    # 嘗試從 settings.json 抓 browser_path
    try:
        from .accounts import load_settings
        bp = (load_settings() or {}).get("browser_path", "") or ""
        if bp and bp not in paths:
            paths.append(bp)
    except Exception:
        pass
    # 常見 Windows Chrome 路徑
    paths.extend([
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ])
    # macOS / Linux fallback(就算不會碰到)
    paths.extend([
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
    ])
    return paths


def _detect_via_version_dir(chrome_exe: str) -> Optional[Tuple[str, str]]:
    """從 Chrome 安裝目錄找版本子資料夾(最安全,不 spawn process)。

    Chrome 安裝目錄結構:
      C:\\Program Files\\Google\\Chrome\\Application\\
      ├── chrome.exe
      ├── 148.0.7778.168\\        ← 版本資料夾!
      ├── 148.0.7780.5\\          ← 可能多個(舊版本沒清)
      └── ...
    """
    try:
        app_dir = Path(chrome_exe).parent
        if not app_dir.exists():
            return None
        # 找符合 X.Y.Z.W pattern 的資料夾,取最新(版本號最大)
        version_pat = re.compile(r'^(\d+)\.(\d+)\.(\d+)\.(\d+)$')
        candidates = []
        for sub in app_dir.iterdir():
            if not sub.is_dir():
                continue
            m = version_pat.match(sub.name)
            if m:
                # 用 tuple of int 排序
                candidates.append((
                    (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))),
                    sub.name,
                ))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        full = candidates[0][1]
        major = str(candidates[0][0][0])
        return (major, full)
    except Exception:
        return None


def _detect_via_registry() -> Optional[Tuple[str, str]]:
    """Windows registry fallback — 讀 HKCU\\Software\\Google\\Chrome\\BLBeacon\\version。"""
    try:
        import sys
        if sys.platform != "win32":
            return None
        try:
            import winreg
        except ImportError:
            return None
        # HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon
        for hive, path in [
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
        ]:
            try:
                k = winreg.OpenKey(hive, path)
                version, _ = winreg.QueryValueEx(k, "version")
                winreg.CloseKey(k)
                m = re.match(r'^(\d+)\.(\d+)\.(\d+)\.(\d+)$', str(version))
                if m:
                    return (m.group(1), str(version))
            except FileNotFoundError:
                continue
            except Exception:
                continue
    except Exception:
        pass
    return None


def detect_local_chrome_version(extra_path: str = "") -> Tuple[str, str]:
    """偵測本機 Chrome 版本(完全不 spawn chrome.exe process)。

    嘗試順序:
      1. Chrome 安裝目錄找版本資料夾(最快最安全)
      2. Windows registry(HKCU/HKLM Software\\Google\\Chrome\\BLBeacon)
      3. fallback hardcode

    ❌ 不使用 chrome.exe --version(會撞 Chrome SingletonLock 開新 Chrome 窗口)

    Returns:
      (major, full_version) e.g. ("148", "148.0.7778.168")
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version

    # ── 方法 1:從 Chrome 安裝目錄找版本子資料夾 ──
    for c in _candidate_chrome_paths(extra_path):
        if c and Path(c).exists():
            result = _detect_via_version_dir(c)
            if result:
                _cached_version = result
                log.info(
                    "[chrome-version] 從版本目錄偵測到 Chrome v%s (%s) → 用此版本對齊 Yahoo UA",
                    result[0], result[1],
                )
                return _cached_version

    # ── 方法 2:Windows registry ──
    reg_result = _detect_via_registry()
    if reg_result:
        _cached_version = reg_result
        log.info(
            "[chrome-version] 從 registry 偵測到 Chrome v%s (%s)",
            reg_result[0], reg_result[1],
        )
        return _cached_version

    # ── 方法 3:fallback ──
    log.warning(
        "[chrome-version] 偵測失敗(找不到 Chrome 安裝目錄/registry),使用 fallback v%s",
        _FALLBACK_CHROME_MAJOR,
    )
    _cached_version = (_FALLBACK_CHROME_MAJOR, _FALLBACK_CHROME_FULL)
    return _cached_version


def get_yahoo_ua() -> str:
    """Yahoo HTTP 用的 User-Agent:Windows + 本機 Chrome 版本。

    跟本機真實 Chrome 對齊,避免「軟件 Mac UA + Sec-Ch-Ua Windows」spoofed bot 標誌。
    """
    _, full = detect_local_chrome_version()
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full} Safari/537.36"
    )


def get_yahoo_sec_ch_ua() -> str:
    """Sec-Ch-Ua:跟本機 Chrome major 版本一致。"""
    major, _ = detect_local_chrome_version()
    return (
        f'"Chromium";v="{major}", '
        f'"Google Chrome";v="{major}", '
        f'"Not.A/Brand";v="99"'
    )


def get_yahoo_major() -> str:
    """只回 major,供需要時用(像 priority hint v= 值)。"""
    major, _ = detect_local_chrome_version()
    return major
