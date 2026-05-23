"""
client_runtime_compat

Public portfolio stub. Production implementation is omitted.

This module provides the **客户端运行时兼容层 (Client Runtime Compatibility Layer)** —
a Harness component that normalizes browser/runtime/network parameters across
heterogeneous user environments (different OS versions, browser versions, VPN setups),
so that Agent-issued Tool calls have consistent behavior regardless of the host machine.

Why this matters as Harness Engineering:
- Reduces deployment-environment-induced task failures.
- Provides a single configuration surface for runtime tuning.
- Decouples Agent decision logic from host machine quirks.

Public surface kept; implementation intentionally stripped to a minimal stub
to avoid disclosing platform-specific compatibility details.
"""

from __future__ import annotations

from typing import Iterable, Optional


def get_launch_args(
    headless: bool = True,
    lang: str = "zh-TW",
    extra: Optional[Iterable[str]] = None,
) -> list:
    """Return a list of normalized browser launch arguments for the local runtime.

    Production version handles per-user version detection and request-stack alignment.
    This stub returns a generic minimal set sufficient for portfolio review.
    """
    args = [
        "--no-first-run",
        "--no-default-browser-check",
        f"--lang={lang}",
    ]
    if headless:
        args.append("--headless=new")
    if extra:
        args.extend(extra)
    return args


def get_http_client_profile(scope: str = "default") -> dict:
    """Return a normalized HTTP client profile for the given scope.

    Each scope (e.g. "yahoo", "mercari", "xianyu") maps to a self-consistent
    set of client parameters so that a single host can serve multiple
    backend platforms without cross-contamination.

    Stub returns an empty profile.
    """
    return {}


def apply_runtime_normalization(session, scope: str = "default") -> None:
    """Apply runtime normalization to a session object in-place.

    Production version reads local environment metadata and aligns the session
    parameters to whatever the target platform expects. Stub is a no-op.
    """
    return None
