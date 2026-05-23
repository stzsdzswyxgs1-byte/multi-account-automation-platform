from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args

def _visual_slow_mo_ms() -> int:
    """视觉化慢动作（ms）。通过环境变量 MERCH_VISUAL_SLOW_MO_MS 控制。"""
    try:
        v = int(os.environ.get("MERCH_VISUAL_SLOW_MO_MS", "250"))
    except Exception:
        v = 250
    if v < 0:
        v = 0
    if v > 3000:
        v = 3000
    return v


from .profile_lock import try_acquire, release, detect_chrome_profile_in_use, acquire_or_clear
from .human import human_interval_sec, human_jitter_ms, maybe_extra_think_ms

# Reuse the proven selectors/behaviors from merch_batch.py
from .merch_batch import (
    LIST_URLS,
    _norm_mode,
    _wait_list_ready,
    _select_header_then_row_if_needed,
    _click_action,
    _confirm_submit_if_any,
    _save_debug_screenshot,
    _log,
    _ts,
    LogFn,
)


async def run_batch_persistent_ops(
    *,
    profile_dir: Path,
    chrome_path: str,
    mode: str,
    repeat: int,
    interval_sec: float,
    headless: bool = False,
    proxy: str = "",
    log: Optional[LogFn] = None,
    is_stop: Optional[Callable[[], bool]] = None,
    is_pause: Optional[Callable[[], bool]] = None,
    start_round: int = 0,
) -> Tuple[str, int]:
    """批量任务（上架/下架/删除）- 可恢复队列/进度续跑。

    关键点：
    - 在同一个 Chrome 会话里跑多轮（避免每一轮都关闭浏览器）
    - 支持接管（pause）/停止（stop）及时退出并释放 profile lock

    返回： (status, new_done_round)
      status: ok/paused/stopped/locked/error
      new_done_round: 已完成轮数（供调用方续跑）
    """

    mode = _norm_mode(mode)
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    repeat = int(repeat or 0)
    done = max(0, int(start_round or 0))

    if done >= repeat:
        return "ok", done

    if is_stop and is_stop():
        return "stopped", done
    if is_pause and is_pause():
        return "paused", done

    ok, reason = acquire_or_clear(profile_dir, owner=f"batch-persist:{mode}",
                                  log_fn=lambda msg: _log(log, f"[BATCH {_ts()}] {msg}"))
    if not ok:
        _log(log, f"[BATCH {_ts()}] LOCKED: {reason}")
        return "locked", done

    async with async_playwright() as p:
        ctx = None
        proxy_kw = {"server": proxy} if proxy else None
        try:
            _hl = bool(headless)
            _args = get_launch_args(headless=_hl, lang="zh-TW")
            if _hl:
                _args.append("--window-size=1280,860")
            _lkw = dict(
                slow_mo=_visual_slow_mo_ms(),
                user_data_dir=str(profile_dir),
                executable_path=chrome_path,
                headless=False,
                proxy=proxy_kw,
                args=_args,
                ignore_default_args=get_ignore_default_args(headless=_hl),
            )
            _lkw["no_viewport"] = True   # Patchright: headless=False 下 viewport 会 getWindowForTarget
            try:
                ctx = await p.chromium.launch_persistent_context(**_lkw)
            except TypeError:
                _lkw.pop("no_viewport", None)
                ctx = await p.chromium.launch_persistent_context(**_lkw)
            await apply_runtime_normalization_async(ctx)
        except Exception as e:
            _log(log, f"[BATCH {_ts()}] Chrome 启动失败（请关闭该账号相关 Chrome 窗口后重试）：{str(e)[:180]}")
            release(profile_dir)
            return "error", done

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            url = LIST_URLS.get(mode)
            if not url:
                return "error", done

            # Ensure we're on the correct list page.
            cur = page.url or ""
            if "/partner/merchandise/list_merchandise" not in cur:
                _log(log, f"[BATCH {_ts()}] GOTO: {url}")
                await page.goto(url, wait_until="domcontentloaded")
            else:
                # Also enforce correct itemStatus (下架=shelve; 上架/刪除=close)
                if (mode == "下架" and "itemStatus=shelve" not in cur) or (mode != "下架" and "itemStatus=close" not in cur):
                    _log(log, f"[BATCH {_ts()}] GOTO: {url}")
                    await page.goto(url, wait_until="domcontentloaded")

            for r in range(done, repeat):
                if is_stop and is_stop():
                    return "stopped", done
                if is_pause and is_pause():
                    return "paused", done

                _log(log, f"[BATCH {_ts()}] ROUND {r+1}/{repeat} -> {mode}")

                try:
                    y_min = await _wait_list_ready(page, mode, log)
                except Exception as e:
                    _log(log, f"[BATCH {_ts()}] ERROR: 清单未就绪（可能未登入或页面改版）。url={page.url}")
                    _log(log, f"[BATCH {_ts()}] DETAIL: {e}")
                    await _save_debug_screenshot(page, "list_not_ready", log, out_dir=profile_dir / "debug")
                    return "error", done

                ok_sel = await _select_header_then_row_if_needed(page, y_min, mode, log)
                if not ok_sel:
                    await _save_debug_screenshot(page, "select_failed", log, out_dir=profile_dir / "debug")
                    return "error", done

                if not await _click_action(page, mode, log):
                    await _save_debug_screenshot(page, f"action_{mode}_click_failed", log, out_dir=profile_dir / "debug")
                    return "error", done

                await _confirm_submit_if_any(page, log)
                done = r + 1

                # Wait + reload between rounds (interruptible).
                delay_s = human_interval_sec(max(0.0, float(interval_sec or 0.0)))
                _log(log, f"[BATCH {_ts()}] interval wait: {delay_s:.1f}s (base={interval_sec}s)")
                t_end = time.time() + float(delay_s)
                while time.time() < t_end:
                    if is_stop and is_stop():
                        return "stopped", done
                    if is_pause and is_pause():
                        return "paused", done
                    await asyncio.sleep(min(0.5, max(0.0, t_end - time.time())))

                try:
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(human_jitter_ms(500) + maybe_extra_think_ms())
                except Exception as e:
                    _log(log, f"[BATCH {_ts()}] WARN: reload failed: {e}")

            return "ok", done
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            except Exception:
                pass
            release(profile_dir)