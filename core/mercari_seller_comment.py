"""煤炉(Mercari)卖家留言 -- Playwright 操作封装

职责：
- 打开 Mercari 商品页面（jp.mercari.com/item/mXXX）
- 点击「コメントする」按钮
- 在弹出的输入框中输入问题并发送
- 轮询刷新页面等待卖家回复
- 返回卖家回复文本

依赖：
- purchase_feature.py 中的 PURCHASE_PROFILE_DIR
- profile_lock.py 中的锁机制

注意：
- 煤炉留言是公开评论，不是即时聊天
- 卖家回复时间不确定（几分钟到几小时）
- 默认轮询5分钟，超时后降级手动
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.profile_lock import try_acquire, release, detect_chrome_profile_in_use


# ---------- 常量 ----------

PROFILE_WAIT_SEC = 60          # 等待 profile 锁的最长时间
SELLER_REPLY_TIMEOUT = 300     # 等待卖家回复的最长时间（5分钟）
POLL_INTERVAL_SEC = 15         # 轮询间隔（比闲鱼长，因为是刷新页面）
PAGE_LOAD_TIMEOUT = 30000      # 页面加载超时 30s


# ---------- 返回结构 ----------

@dataclass
class MercariCommentResult:
    success: bool
    seller_reply: str = ""
    error: str = ""
    need_login: bool = False
    timed_out: bool = False
    mercari_url: str = ""              # 商品页 URL，用于后续检查
    comment_count_after_send: int = 0  # 发送后的评论数
    sent_question: str = ""            # 实际发送的问题


# ---------- 同步入口 ----------

def comment_mercari_seller_sync(
    *,
    mercari_url: str,
    question: str,
    on_log: Optional[Callable] = None,
    chrome_path: str = "",
    poll_timeout_sec: int = SELLER_REPLY_TIMEOUT,
) -> MercariCommentResult:
    """同步入口：获取 profile 锁 → 打开浏览器留言 → 释放锁。"""
    from core.purchase_feature import PURCHASE_PROFILE_DIR

    waited = 0
    acquired = False
    while waited < PROFILE_WAIT_SEC:
        in_use, _ = detect_chrome_profile_in_use(PURCHASE_PROFILE_DIR)
        if not in_use:
            ok, reason = try_acquire(PURCHASE_PROFILE_DIR, owner="mercari-comment")
            if ok:
                acquired = True
                break
        time.sleep(5)
        waited += 5

    if not acquired:
        return MercariCommentResult(
            success=False,
            error=f"采购 Profile 被占用，等待{PROFILE_WAIT_SEC}秒后仍无法获取",
        )

    try:
        return _comment_inner(
            mercari_url=mercari_url,
            question=question,
            on_log=on_log,
            chrome_path=chrome_path,
            poll_timeout_sec=poll_timeout_sec,
        )
    except Exception as e:
        return MercariCommentResult(success=False, error=str(e)[:200])
    finally:
        release(PURCHASE_PROFILE_DIR)


# ---------- 辅助：提取评论列表 ----------

def _get_comments(page) -> List[str]:
    r"""提取 Mercari 商品页面评论区的所有评论文本。

    策略：
    1. 先用 CSS 选择器精确匹配（如果 Mercari 有 data-testid）
    2. 兜底：从「コメント (N)」标题提取期望数量 N，
       然后用时间戳「X分前」「X時間前」等作为锚点，
       取每个时间戳前面紧邻的文本块作为评论内容。
    """
    return page.evaluate(r"""
        () => {
            const msgs = [];

            // ---- 方法1：CSS 选择器精确匹配 ----
            const sels = [
                '[data-testid="comment-content"]',
                '[class*="comment"] [class*="body"]',
                '[class*="comment"] [class*="content"]',
                '[class*="Comment"] [class*="Body"]',
                '[class*="comment-text"]',
            ];
            for (const sel of sels) {
                const els = document.querySelectorAll(sel);
                if (els.length > 0) {
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (t) msgs.push(t);
                    }
                    return msgs;
                }
            }

            // ---- 方法2：用时间戳作为锚点 ----
            // Mercari 评论结构：用户名 → 评论文本 → 时间（如「3分前」）
            // 删除的评论：「出品者がコメントを削除しました」→ 时间
            // 思路：找到所有时间戳元素，往前找同级或父级中的评论文本

            // 时间戳正则：「X分前」「X時間前」「X日前」「X秒前」等
            const TIME_RE = /^\d+[分時秒日週月年]+(間)?前$/;
            // 删除消息
            const DEL_RE = /^出品者がコメントを削除しました$/;

            // 收集评论区内所有叶子文本节点
            // 先找到评论区的范围
            const allEls = document.querySelectorAll('*');
            let commentStart = -1;
            let commentEnd = allEls.length;

            for (let i = 0; i < allEls.length; i++) {
                const el = allEls[i];
                const tc = (el.textContent || '').trim();
                if (commentStart === -1
                    && /^コメント\s*[\(（]\d+[\)）]$/.test(tc)
                    && el.children.length <= 3) {
                    commentStart = i;
                    continue;
                }
                if (commentStart >= 0) {
                    if (tc === '商品へのコメント'
                        || el.tagName === 'TEXTAREA') {
                        commentEnd = i;
                        break;
                    }
                }
            }

            if (commentStart === -1) return msgs;

            // 在评论区范围内，找所有时间戳叶子节点
            const timeNodes = [];
            for (let i = commentStart; i < commentEnd; i++) {
                const el = allEls[i];
                if (el.children.length > 0) continue;
                const t = (el.innerText || '').trim();
                if (TIME_RE.test(t)) {
                    timeNodes.push({ idx: i, el: el });
                }
            }

            // 对每个时间戳，往前找评论文本
            // 评论文本 = 时间戳之前、上一个时间戳之后的
            //            非用户名、非系统文本的最长文本块
            for (let ti = 0; ti < timeNodes.length; ti++) {
                const tEnd = timeNodes[ti].idx;
                const tStart = ti > 0
                    ? timeNodes[ti - 1].idx + 1
                    : commentStart + 1;

                // 收集这个区间内的叶子文本（按 DOM 顺序）
                // 结构：用户名(第1个) → 评论文本(第2个) → 可能还有其他
                const texts = [];
                let isDeleted = false;

                for (let i = tStart; i < tEnd; i++) {
                    const el = allEls[i];
                    if (el.children.length > 0) continue;
                    const t = (el.innerText || '').trim();
                    if (!t || t.length < 1) continue;

                    if (DEL_RE.test(t)) {
                        isDeleted = true;
                        break;
                    }

                    texts.push(t);
                }

                if (isDeleted || texts.length === 0) continue;

                // 评论结构：[用户名, 评论文本段1, 评论文本段2, ...]
                // 如果只有1个文本 → 就是评论本身（可能用户名被跳过了）
                // 如果有2个以上 → 第1个是用户名，其余全部是评论文本
                let comment = '';
                if (texts.length === 1) {
                    comment = texts[0];
                } else {
                    // 跳过第1个（用户名），拼接其余所有文本
                    comment = texts.slice(1).join('\n');
                }

                if (comment) {
                    msgs.push(comment);
                }
            }

            return msgs;
        }
    """) or []


# ---------- 内部实现 ----------

def _comment_inner(
    *,
    mercari_url: str,
    question: str,
    on_log: Optional[Callable] = None,
    chrome_path: str = "",
    poll_timeout_sec: int = SELLER_REPLY_TIMEOUT,
) -> MercariCommentResult:
    """打开煤炉商品页 → 留言 → 轮询等待回复。"""
    from .client_runtime_compat import sync_playwright, apply_runtime_normalization_sync, get_launch_args, get_ignore_default_args
    from core.purchase_feature import (
        PURCHASE_PROFILE_DIR,
        _get_system_chrome_path,
        _ensure_cookie_persistence_hint,
    )

    def log(msg: str):
        if on_log:
            on_log(msg)

    _ensure_cookie_persistence_hint(PURCHASE_PROFILE_DIR)
    exe = _get_system_chrome_path(chrome_path)

    p = sync_playwright().start()
    ctx = None
    try:
        _launch_kw = dict(
            user_data_dir=str(PURCHASE_PROFILE_DIR),
            headless=False,
            no_viewport=True,   # 非 headless 不设 viewport（Patchright 兼容）
            locale="ja-JP",
            accept_downloads=False,
            args=get_launch_args(headless=False, lang="ja", extra=[
                "--window-position=50,50",
                "--window-size=1280,860",
            ]),
            ignore_default_args=get_ignore_default_args(headless=False),
            **({"executable_path": exe} if exe else {}),
        )
        try:
            ctx = p.chromium.launch_persistent_context(**_launch_kw)
        except TypeError:
            _launch_kw.pop("no_viewport", None)
            _launch_kw["viewport"] = {"width": 1280, "height": 860}
            ctx = p.chromium.launch_persistent_context(**_launch_kw)
        apply_runtime_normalization_sync(ctx)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ---- Step 1: 打开商品页 ----
        log(f"[MERCARI-COMMENT] 打开商品页: {mercari_url}")
        page.goto(mercari_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_timeout(3000)

        # ---- Step 2: 检查登录 ----
        if _check_need_login(page):
            return MercariCommentResult(success=False, need_login=True, error="煤炉需要登录")

        # ---- Step 3: 记录已有评论 ----
        baseline = _get_comments(page)
        log(f"[MERCARI-COMMENT] 已有评论数: {len(baseline)}")

        # ---- Step 4: 点击留言按钮 ----
        log("[MERCARI-COMMENT] 查找留言按钮...")
        clicked = _click_comment_button(page)
        if not clicked:
            return MercariCommentResult(success=False, error="找不到留言按钮")

        # ---- Step 5: 查找输入框并发送 ----
        log("[MERCARI-COMMENT] 查找输入框...")
        page.wait_for_timeout(1500)
        input_el = _find_comment_input(page)
        if not input_el:
            return MercariCommentResult(success=False, error="找不到留言输入框")

        log(f"[MERCARI-COMMENT] 发送留言: {question[:60]}")
        input_el.click()
        input_el.fill(question)
        page.wait_for_timeout(500)

        # 点击发送按钮
        sent = _click_send_button(page)
        if not sent:
            return MercariCommentResult(success=False, error="找不到发送按钮")
        page.wait_for_timeout(2000)
        log("[MERCARI-COMMENT] 留言已发送")

        # ---- Step 6: 发送后重新记录 baseline，立刻返回 ----
        post_send = _get_comments(page)
        baseline_count = len(post_send)
        log(f"[MERCARI-COMMENT] 发送后评论数: {baseline_count}")

        return MercariCommentResult(
            success=True,
            mercari_url=mercari_url,
            comment_count_after_send=baseline_count,
            sent_question=question,
        )

    except Exception as e:
        return MercariCommentResult(success=False, error=str(e)[:200])
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        try:
            p.stop()
        except Exception:
            pass


# ---------- 辅助：检查登录 ----------

def _check_need_login(page) -> bool:
    """检查是否需要登录。"""
    text = page.evaluate("document.body.innerText || ''")
    if "ログインしてコメントする" in text:
        return True
    if "ログイン" in page.url:
        return True
    return False


# ---------- 辅助：关闭模板提示弹窗 ----------

def _dismiss_template_popup(page) -> None:
    """关闭 Mercari 的「テンプレートを利用してみましょう」弹窗。"""
    try:
        close_btn = page.locator('button:near(:text("テンプレートを利用"))').first
        if close_btn.is_visible(timeout=1500):
            close_btn.click()
            page.wait_for_timeout(500)
            return
    except Exception:
        pass
    # 用 X 按钮关闭
    for sel in [
        ':text("テンプレートを利用") >> .. >> button',
        '[class*="close"]:near(:text("テンプレート"))',
        'svg:near(:text("テンプレート"))',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue
    # 兜底：按 Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


# ---------- 辅助：点击留言按钮 ----------

def _click_comment_button(page) -> bool:
    """点击煤炉商品页的「コメント」按钮，滚动到评论区。

    注意：有评论后按钮变成图标+数字（无文字），需要用 svg/icon 选择器。
    """
    # 1) 有文字的按钮（首次无评论时）
    for sel in [
        'button:has-text("コメント")',
        '[data-testid="comment-button"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # 2) 图标按钮（有评论后，只有💬图标+数字）
    #    找包含评论图标 svg 的按钮，通常在 ♡ 按钮旁边
    try:
        # Mercari 评论按钮通常是第二个 action button（第一个是 ♡）
        comment_btn = page.locator('button:near(button:has-text("値下げ依頼"))').first
        if comment_btn.is_visible(timeout=2000):
            comment_btn.scroll_into_view_if_needed()
            comment_btn.click()
            return True
    except Exception:
        pass

    # 3) 用 aria-label 或 svg 找评论按钮
    for sel in [
        'button[aria-label*="コメント"]',
        'button[aria-label*="comment"]',
        '[class*="comment"] button',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # 4) 兜底：直接滚动到评论区标题或 textarea
    for sel in [
        'text=コメント (', 'text=商品へのコメント',
        'textarea[placeholder*="コメント"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.scroll_into_view_if_needed()
                return True
        except Exception:
            continue

    # 5) 最终兜底：JS 滚动到页面底部（评论区在底部）
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        return True
    except Exception:
        pass

    return False


# ---------- 辅助：查找留言输入框 ----------

def _find_comment_input(page):
    """查找留言输入框。"""
    for sel in [
        'textarea[placeholder*="コメント"]',
        'textarea[placeholder*="comment"]',
        'textarea',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=5000):
                return el
        except Exception:
            continue
    return None


# ---------- 辅助：点击发送按钮 ----------

def _click_send_button(page) -> bool:
    """点击留言发送按钮。"""
    for sel in [
        'button:has-text("コメントを送信する")',
        'button:has-text("送信する")',
        'button:has-text("コメントする")',
        'button:has-text("送信")',
        'button[type="submit"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                return True
        except Exception:
            continue
    return False


# ---------- 检查回复结构 ----------

@dataclass
class MercariCheckResult:
    has_reply: bool = False
    seller_reply: str = ""
    error: str = ""
    need_login: bool = False


# ---------- 检查回复：同步入口 ----------

def check_mercari_reply_sync(
    *,
    mercari_url: str,
    comment_count_after_send: int,
    sent_question: str,
    on_log: Optional[Callable] = None,
    chrome_path: str = "",
) -> MercariCheckResult:
    """同步入口：获取 profile 锁 → 打开商品页检查新评论 → 释放锁。"""
    from core.purchase_feature import PURCHASE_PROFILE_DIR

    waited = 0
    acquired = False
    while waited < PROFILE_WAIT_SEC:
        in_use, _ = detect_chrome_profile_in_use(PURCHASE_PROFILE_DIR)
        if not in_use:
            ok, reason = try_acquire(PURCHASE_PROFILE_DIR, owner="mercari-check")
            if ok:
                acquired = True
                break
        time.sleep(5)
        waited += 5

    if not acquired:
        return MercariCheckResult(error="采购 Profile 被占用")

    try:
        return _check_mercari_reply_inner(
            mercari_url=mercari_url,
            comment_count_after_send=comment_count_after_send,
            sent_question=sent_question,
            on_log=on_log,
            chrome_path=chrome_path,
        )
    except Exception as e:
        return MercariCheckResult(error=str(e)[:200])
    finally:
        release(PURCHASE_PROFILE_DIR)


# ---------- 检查回复：内部实现 ----------

def _check_mercari_reply_inner(
    *,
    mercari_url: str,
    comment_count_after_send: int,
    sent_question: str,
    on_log: Optional[Callable] = None,
    chrome_path: str = "",
) -> MercariCheckResult:
    """打开商品页 → 检查评论区是否有新回复 → 关闭浏览器。"""
    from .client_runtime_compat import sync_playwright, apply_runtime_normalization_sync, get_launch_args, get_ignore_default_args
    from core.purchase_feature import (
        PURCHASE_PROFILE_DIR,
        _get_system_chrome_path,
        _ensure_cookie_persistence_hint,
    )

    def log(msg: str):
        if on_log:
            on_log(msg)

    _ensure_cookie_persistence_hint(PURCHASE_PROFILE_DIR)
    exe = _get_system_chrome_path(chrome_path)

    p = sync_playwright().start()
    ctx = None
    try:
        _launch_kw2 = dict(
            user_data_dir=str(PURCHASE_PROFILE_DIR),
            headless=False,
            no_viewport=True,   # 非 headless 不设 viewport（Patchright 兼容）
            locale="ja-JP",
            accept_downloads=False,
            args=get_launch_args(headless=False, lang="ja", extra=[
                "--window-position=50,50",
                "--window-size=1280,860",
            ]),
            ignore_default_args=get_ignore_default_args(headless=False),
            **({"executable_path": exe} if exe else {}),
        )
        try:
            ctx = p.chromium.launch_persistent_context(**_launch_kw2)
        except TypeError:
            _launch_kw2.pop("no_viewport", None)
            _launch_kw2["viewport"] = {"width": 1280, "height": 860}
            ctx = p.chromium.launch_persistent_context(**_launch_kw2)
        apply_runtime_normalization_sync(ctx)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ---- 打开商品页 ----
        log(f"[MERCARI-CHECK] 打开商品页: {mercari_url}")
        page.goto(mercari_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_timeout(3000)

        # ---- 检查登录 ----
        if _check_need_login(page):
            return MercariCheckResult(need_login=True, error="煤炉需要登录")

        # ---- 滚动到评论区 ----
        log("[MERCARI-CHECK] 滚动到评论区...")
        _click_comment_button(page)
        page.wait_for_timeout(2000)
        _dismiss_template_popup(page)
        page.wait_for_timeout(500)

        # ---- 获取当前评论 ----
        current = _get_comments(page)
        log(f"[MERCARI-CHECK] 当前评论数: {len(current)}, 发送时: {comment_count_after_send}")

        if len(current) <= comment_count_after_send:
            return MercariCheckResult(has_reply=False)

        # ---- 基于问题文本定位，找到问题之后的新评论 ----
        q = sent_question.strip()
        q_pos = -1
        for i in range(len(current) - 1, -1, -1):
            if q in current[i] or current[i].strip() == q:
                q_pos = i
                break

        if q_pos >= 0:
            new_msgs = current[q_pos + 1:]
            log(f"[MERCARI-CHECK] 问题在位置 {q_pos}，之后有 {len(new_msgs)} 条评论")
        else:
            new_msgs = current[comment_count_after_send:]
            log(f"[MERCARI-CHECK] 找不到问题文本，用数量兜底")

        if not new_msgs:
            return MercariCheckResult(has_reply=False)

        real_new = [m for m in new_msgs if q not in m and m.strip() != q]

        if not real_new:
            return MercariCheckResult(has_reply=False)

        reply = "\n".join(real_new)
        log(f"[MERCARI-CHECK] 卖家回复: {reply[:100]}")
        return MercariCheckResult(has_reply=True, seller_reply=reply)

    except Exception as e:
        return MercariCheckResult(error=str(e)[:200])
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        try:
            p.stop()
        except Exception:
            pass
