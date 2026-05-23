# -*- coding: utf-8 -*-
"""Yahoo IM: capture full text for current conversation + unread conversations.

This module reads the Yahoo IM page in your own logged-in browser session.
It does not use any private API; it only collects what the page already shows.

Returned format
  [{"chat_id": "Y...", "label": "...", "url": "...", "text": "..."}, ...]

Behavior
- First capture the currently open conversation after entering /chat/<shop_code>
- Then capture up to max_unread unread conversations (red dot on the left list)
- For each conversation, scroll the message container to the top multiple times
  to load older history.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

_RE_YID = re.compile(r"\bY\d{6,12}\b")


async def _extract_shop_code_from_myauc(page) -> str:
    """Best-effort: extract seller shop code like Y9000000010 from /myauc."""
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    text = text or ""
    m = _RE_YID.search(text)
    return m.group(0) if m else ""


def _abs_url(origin: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return origin.rstrip("/") + href



async def _wait_for_chat_loaded(page, timeout_ms: int = 20000):
    """Wait until Yahoo 即时通聊天页主要 DOM 就绪。

    现版本的左侧会话列表通常不再是 <a href="/chat/...">，
    而是 div.channel__*（CSS module 形式），所以不能只等 a[href*="/chat/"]。
    """
    candidates = [
        'aside [class*="channel__"]',
        '[class*="channels__"] [class*="channel__"]',
        '[class*="channelList"] [class*="channel__"]',
        # message area fallback
        'div[class*="message__"], div[class*="messages__"], div[class*="chat__"]',
        '#im-message-home, #im-root, [data-property="auction"]',
    ]
    per = max(1500, int(timeout_ms / max(1, len(candidates))))
    for sel in candidates:
        try:
            await page.wait_for_selector(sel, timeout=per)
            # give React / CSS module a short breath
            await page.wait_for_timeout(250)
            return
        except Exception:
            continue
    # last fallback: don't crash
    try:
        await page.wait_for_timeout(300)
    except Exception:
        pass

async def _get_active_chat_id(page) -> str:
    url = page.url or ""
    m = re.search(r"/chat/(Y\d{6,})", url)
    return m.group(1) if m else ""


async def _find_message_container(page):
    """Find the right-side message scroll container (best-effort)."""
    js = r"""
() => {
  const divs = Array.from(document.querySelectorAll('div'));
  const w = window.innerWidth || 1200;
  const h = window.innerHeight || 800;
  const cands = [];
  for (const el of divs) {
    const st = getComputedStyle(el);
    if (!(st.overflowY === 'auto' || st.overflowY === 'scroll')) continue;
    if (el.scrollHeight <= el.clientHeight + 80) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 300 || rect.height < 220) continue;
    // left list is usually on the left side
    if (rect.right < w * 0.45) continue;
    const txt = (el.innerText || '').trim();
    if (txt.length < 80) continue;
    let score = txt.length;
    if (rect.left > w * 0.35) score += 5000;
    if (rect.width > w * 0.45) score += 2000;
    if (rect.height > h * 0.55) score += 1000;
    cands.push({ el, score });
  }
  cands.sort((a,b)=>b.score-a.score);
  return cands.length ? cands[0].el : null;
}
"""
    h = await page.evaluate_handle(js)
    return h.as_element() if h else None


async def _find_contact_list_container(page: Page) -> Optional[ElementHandle]:
    """Find the scrollable sidebar container that holds chat/channel rows.

    Yahoo IM 的左侧列表 DOM 经常变动：
    - 旧版：每个会话是 <a href="/chat/Yxxxx">...
    - 新版：会话行是纯 <div class="channel__..."> 点击后用 history 跳转

    这里做两套探测：优先找含 /chat/ anchor 的滚动容器；找不到就退化为含 channel__ 行的容器。
    """
    js = r"""
() => {
  // Prefer the contact list inside the left sidebar (aside.channelList...).
  const aside = document.querySelector('aside[class*="channelList"]');
  if (aside) {
    let best = aside;
    let bestCount = aside.querySelectorAll('div[class*="channel__"]').length;
    // Some builds wrap the list in div.channels__...
    const inner = aside.querySelector('div[class*="channels__"]');
    if (inner) {
      const c = inner.querySelectorAll('div[class*="channel__"]').length;
      if (c >= bestCount) {
        best = inner;
        bestCount = c;
      }
    }
    // If there is a deeper scroll container, pick the one with the most channel rows.
    const candidates = Array.from(aside.querySelectorAll('div,section'));
    for (const el of candidates) {
      const c = el.querySelectorAll('div[class*="channel__"]').length;
      if (c > bestCount) {
        best = el;
        bestCount = c;
      }
    }
    if (bestCount >= 1) return best;
  }

  // Fallback: pick the element that contains the most channel rows, with a bias to narrow sidebars.
  const vpW = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
  const nodes = Array.from(document.querySelectorAll('aside,div,section'));
  let best = null;
  let bestScore = 0;
  for (const el of nodes) {
    const channelCount = el.querySelectorAll('div[class*="channel__"]').length;
    const linkCount = el.querySelectorAll('a[href*="/chat/"]').length;
    if (channelCount < 2 && linkCount < 2) continue;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) continue;
    // Skip huge containers that are likely the main chat area.
    if (rect.width > vpW * 0.8) continue;

    let score = 0;
    score += channelCount * 200;
    score += linkCount * 250;
    score += Math.min(rect.height, 900);
    if (rect.width < vpW * 0.6) score += 1000;
    if (el.closest('aside')) score += 200;

    if (score > bestScore) {
      bestScore = score;
      best = el;
    }
  }
  return best;
}
"""

    handle = await page.evaluate_handle(js)
    try:
        el = handle.as_element()
    except Exception:
        el = None
    return el
async def _scroll_to_top(page, el_handle, max_rounds: int = 30, sleep_ms: int = 650):
    """Scroll message container to the top repeatedly to trigger lazy-load."""
    last_h = None
    stable = 0
    for _ in range(max_rounds):
        try:
            await el_handle.evaluate(
                r"""(el) => {
  try {
    el.scrollTop = 0;
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
  } catch (e) {}
}"""
            )
        except Exception:
            break
        try:
            await page.wait_for_timeout(sleep_ms)
        except Exception:
            pass
        try:
            cur_h = await el_handle.evaluate("(el) => el.scrollHeight")
        except Exception:
            break
        if last_h is not None and abs(cur_h - last_h) < 5:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_h = cur_h



async def _capture_current_conversation_sliced(
    page,
    unread_count: Optional[int] = None,
    last_n: int = 12,
) -> str:
    """Capture conversation as text, optionally slicing the last N *incoming* messages.

    - If unread_count is provided, we try to return the last `unread_count` messages from the other side.
    - Otherwise, return the last `last_n` messages (with ME/THEM prefix when possible).
    """
    js = r"""
() => {
  const nodes = Array.from(document.querySelectorAll('div[class*="messageComponentWrap"]'));
  const items = [];
  for (const n of nodes) {
    const t = (n.innerText || '').trim();
    if (!t) continue;

    // Heuristic: incoming messages usually have an avatar image near them.
    const wrap = n.closest('div[class*="msgContentWrapper"]') || n.parentElement;
    const hasAvatar = wrap && wrap.querySelector && wrap.querySelector('img[class*="avatar"], img[class*="userImage"], img[alt]');
    let dir = hasAvatar ? "THEM" : "ME";

    // Another heuristic: some builds include "self" / "me" in class
    const cls = (wrap && wrap.className) ? String(wrap.className) : "";
    if (/self|me|mine|right/i.test(cls)) dir = "ME";
    if (/other|left/i.test(cls)) dir = "THEM";

    items.push({dir, text: t});
  }
  return items;
}
"""
    try:
        items = await page.evaluate(js)
    except Exception:
        items = []

    if not items:
        # Fallback: old method
        try:
            d = await _capture_current_conversation(page, scroll_rounds=0)
            return (d.get("text") if isinstance(d, dict) else str(d))
        except Exception:
            return ""

    def _normalize(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
        return s.strip()

    items2 = [{"dir": it.get("dir", ""), "text": _normalize(it.get("text", ""))} for it in items if _normalize(it.get("text", ""))]

    if not items2:
        return ""

    if unread_count and isinstance(unread_count, int) and unread_count > 0:
        them = [it["text"] for it in items2 if it["dir"] == "THEM"]
        if them:
            return "\n".join(them[-unread_count:]).strip()
        # fallback: last N messages
        return "\n".join([it["text"] for it in items2[-unread_count:]]).strip()

    last = items2[-last_n:]
    out_lines = [f'{it["dir"]}: {it["text"]}' for it in last]
    return "\n".join(out_lines).strip()


async def _capture_current_conversation(page, scroll_rounds: int, on_log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Capture full text of the currently open conversation."""
    chat_id = await _get_active_chat_id(page)

    # wait input box to improve success rate
    try:
        await page.wait_for_selector(
            'textarea, input[placeholder*="訊息"], textarea[placeholder*="訊息"], input[placeholder*="消息"], textarea[placeholder*="消息"]',
            timeout=12000,
        )
    except Exception:
        pass

    el = await _find_message_container(page)
    if el:
        try:
            await _scroll_to_top(page, el, max_rounds=max(1, int(scroll_rounds or 30)))
        except Exception:
            pass
        try:
            text = (await el.evaluate("(e) => (e.innerText || '').trim()")) or ""
        except Exception:
            text = ""
    else:
        # fallback
        try:
            text = (await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText.trim() : ''")) or ""
        except Exception:
            text = ""

    label = ""
    try:
        m = _RE_YID.search(text or "")
        if m:
            label = m.group(0)
    except Exception:
        label = ""
    if not label:
        label = chat_id or ""

    if on_log:
        try:
            on_log(f"[IM] capture current chat_id={chat_id} text_len={len(text or '')}")
        except Exception:
            pass

    return {
        "chat_id": chat_id or "",
        "label": label or chat_id or "",
        "url": page.url or "",
        "text": text or "",
    }


async def _list_chat_links_with_unread(page, container_el=None) -> List[Dict[str, Any]]:
    """Return chat list entries that have an unread red-dot (conversation-level), with their preview snippet.

    ⚠️ 注意：只抓「联系人列表里」每条对话的红点（dot），不会去读页面顶部 IM 图标的红点。
    """
    # Try to ensure the IM list is rendered
    try:
        await page.wait_for_selector("aside[class*='channelList__'], div[class*='channels__'], div[class*='channel__']",
                                     timeout=10_000)
    except Exception:
        pass

    js = r"""
(root) => {
  const scope = root || document;

  // Prefer scanning inside the left contact list to avoid picking other "dot" on the page.
  const listRoot =
    scope.querySelector('aside[class*="channelList__"]') ||
    scope.querySelector('div[class*="channels__"]') ||
    scope;

  const rows = Array.from(listRoot.querySelectorAll('div[class*="channel__"]'));

  const pickText = (el) => (el && (el.innerText || el.textContent || '').trim()) || '';

  const out = [];
  for (const row of rows) {
    // Conversation unread dot
    const dot = row.querySelector('div[class*="dot__"]');
    if (!dot) continue;

    const unreadText = pickText(dot);
    const unread = parseInt((unreadText || '').replace(/[^\d]/g, ''), 10);
    if (!Number.isFinite(unread) || unread <= 0) continue;

    // ── 提取店铺 ID（用于区分卖家/买家） ──
    const _shopMatch = (location.pathname || '').match(/\/chat\/(Y?\d+)/i);
    const _shopId = _shopMatch ? _shopMatch[1].toLowerCase() : '';
    let cid = '';

    // ── 策略 A（最优先）：React fiber 提取买家专属 ID ──
    // channel.id 格式: "yahoo-bid-logbot1:y{卖家}:y{买家}"
    // 取不等于店铺 ID 的 Y 号 = 真正的买家 ID
    try {
      const rk = Object.keys(row).find(k => k.startsWith('__reactFiber'));
      if (rk) {
        let fiber = row[rk];
        for (let d = 0; d < 5 && fiber && !cid; d++) {
          const p = fiber.memoizedProps || fiber.pendingProps || {};
          const ch = p.channel || p.data || p.item || null;
          if (ch && typeof ch === 'object') {
            const chId = ch.id || ch.channelId || ch.chatId || '';
            if (chId && typeof chId === 'string') {
              const parts = chId.split(':');
              for (const pt of parts) {
                if (/^y\d{5,}$/i.test(pt) && pt.toLowerCase() !== _shopId) {
                  cid = pt.replace(/^y/, 'Y');
                  break;
                }
              }
            }
          }
          fiber = fiber.return;
        }
      }
    } catch(e) {}

    // ── 策略 B（回退）：<a href="/chat/XXX"> ──
    if (!cid) {
      let href = '';
      const a = row.querySelector('a[href*="/chat/"]');
      if (a) href = a.getAttribute('href') || '';
      if (!href) {
        const anyA = row.querySelector('a[href]');
        if (anyA) href = anyA.getAttribute('href') || '';
      }
      if (!href) {
        href = row.getAttribute('data-href') || row.getAttribute('data-url') || '';
      }
      if (href) {
        const m = href.match(/\/chat\/([^\/\?#]+)/);
        if (m && m[1]) cid = m[1];
      }
    }

    // ── 策略 C（最后回退）：data 属性 / HTML 扫描 ──
    if (!cid) {
      cid = row.getAttribute('data-cid') || row.getAttribute('data-id') ||
            row.getAttribute('data-channel-id') || row.getAttribute('data-chat-id') || '';
    }
    if (!cid) {
      const html = row.outerHTML || '';
      const m2 = html.match(/\/chat\/(Y\d{5,})/);
      if (m2 && m2[1]) cid = m2[1];
    }

    const labelEl =
      row.querySelector('div[class*="userName__"]') ||
      row.querySelector('div[class*="userName"]') ||
      row.querySelector('div[class*="nick"]') ||
      null;

    const previewEl =
      row.querySelector('div[class*="message__"]') ||
      row.querySelector('div[class*="message"]') ||
      null;

    const timeEl =
      row.querySelector('div[class*="lastMsgTime__"]') ||
      row.querySelector('div[class*="lastMsgTime"]') ||
      null;

    const label = pickText(labelEl);
    const preview = pickText(previewEl);
    const timeText = pickText(timeEl);

    out.push({ href, cid, label, preview, timeText, unread });
  }

  // De-dupe by (cid || href || label+preview)
  const seen = new Set();
  const dedup = [];
  for (const it of out) {
    const key = it.cid || it.href || (it.label + '|' + it.preview);
    if (seen.has(key)) continue;
    seen.add(key);
    dedup.push(it);
  }
  return dedup;
}
"""

    try:
        if container_el is not None:
            return await container_el.evaluate(js, container_el)  # root=container_el
        return await page.evaluate(js, None)
    except Exception:
        # Fallback to the original narrow selectors (best effort)
        out: List[Dict[str, Any]] = []
        roots = [container_el] if container_el is not None else [page]
        row_sel_list = [
            "div.channel__EHI_P",
            "div[class*='channel__']",
        ]
        for root in roots:
            rows = []
            for row_sel in row_sel_list:
                try:
                    rows = await root.query_selector_all(row_sel)
                except Exception:
                    rows = []
                if rows:
                    break
            for row in rows:
                try:
                    dot_el = await row.query_selector("div.dot__ghI5M, div[class*='dot__']")
                    if not dot_el:
                        continue
                    dot_text = (await dot_el.inner_text()).strip()
                    m = re.search(r"\d+", dot_text)
                    if not m:
                        continue
                    unread = int(m.group(0))
                    if unread <= 0:
                        continue
                    label_el = await row.query_selector("div.userName__a9YPt, div[class*='userName__']")
                    preview_el = await row.query_selector("div.message__yHs8, div[class*='message__']")
                    time_el = await row.query_selector("div.lastMsgTime__SuUPy, div[class*='lastMsgTime__']")
                    a_el = await row.query_selector("a[href*='/chat/']")
                    href = (await a_el.get_attribute("href")) if a_el else ""
                    cid = ""
                    if href:
                        mm = re.search(r"/chat/([^/?#]+)", href)
                        if mm:
                            cid = mm.group(1)
                    out.append({
                        "href": href,
                        "cid": cid,
                        "label": (await label_el.inner_text()).strip() if label_el else "",
                        "preview": (await preview_el.inner_text()).strip() if preview_el else "",
                        "timeText": (await time_el.inner_text()).strip() if time_el else "",
                        "unread": unread,
                    })
                except Exception:
                    continue
        # Dedup
        seen=set()
        ded=[]
        for it in out:
            key=it.get("cid") or it.get("href") or (it.get("label","")+"|"+it.get("preview",""))
            if key in seen: 
                continue
            seen.add(key)
            ded.append(it)
        return ded
async def _collect_unread_previews_in_dom(page) -> List[Dict[str, Any]]:
    """Collect unread red-dot preview snippets directly from the left conversation list (no opening / no marking read).

    This is the "最初那样" version: only grab the small preview text next to the red-dot.
    """
    # Ensure IM page loaded
    await _wait_for_chat_loaded(page)

    # 1) First try: scan the whole page (most robust; avoids wrong container selection)
    try:
        raw = await _list_chat_links_with_unread(page, None)
        if raw:
            out: List[Dict[str, Any]] = []
            for it in raw:
                out.append({
                    "cid": it.get("cid") or "",
                    "href": it.get("href") or "",
                    "label": it.get("label") or "",
                    "preview": it.get("preview") or "",
                    "unread": int(it.get("unread") or 0),
                    "timeText": it.get("timeText") or "",
                })
            return out
    except Exception:
        pass

    # 2) Second try: scroll the actual list container and rescan
    list_container = None
    try:
        list_container = await page.query_selector("aside[class*='channelList__'] div[class*='channels__'], div.channels__aXmZg")
    except Exception:
        list_container = None

    if list_container is None:
        try:
            list_container = await _find_contact_list_container(page)
        except Exception:
            list_container = None

    if list_container is None:
        return []

    # Scroll sweep to load more items (virtualized list safe)
    seen_keys = set()
    out_map: Dict[str, Dict[str, Any]] = {}

    # Try bring to top
    try:
        await _scroll_to_top(list_container)
    except Exception:
        pass

    for _ in range(25):
        try:
            items = await _list_chat_links_with_unread(page, list_container)
        except Exception:
            items = []

        for it in items or []:
            cid = it.get("cid") or ""
            href = it.get("href") or ""
            label = it.get("label") or ""
            preview = it.get("preview") or ""
            unread = int(it.get("unread") or 0)
            key = cid or href or (label + "|" + preview)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out_map[key] = {
                "cid": cid,
                "href": href,
                "label": label,
                "preview": preview,
                "unread": unread,
                "timeText": it.get("timeText") or "",
            }

        # Determine whether can scroll further
        try:
            metrics = await list_container.evaluate(
                """(el) => ({top: el.scrollTop, h: el.scrollHeight, ch: el.clientHeight})"""
            )
            top = float(metrics.get("top", 0))
            h = float(metrics.get("h", 0))
            ch = float(metrics.get("ch", 0))
            if top + ch >= h - 5:
                break
            await list_container.evaluate(
                """(el) => { el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + el.clientHeight * 0.85); el.dispatchEvent(new Event('scroll', {bubbles:true})); }"""
            )
            await page.wait_for_timeout(250)
        except Exception:
            break

    return list(out_map.values())
async def _scan_chat_links(page, scan_rounds: int = 30, sleep_ms: int = 450, on_log: Optional[Callable[[str], None]] = None) -> List[Dict[str, str]]:
    """Scroll the left contact list to collect more chat links."""
    el = await _find_contact_list_container(page)
    if not el:
        return await _list_chat_links_with_unread(page)

    merged: Dict[str, Dict[str, str]] = {}

    # start from top
    try:
        await el.evaluate("(e) => { try { e.scrollTop = 0; } catch (x) {} }")
        await page.wait_for_timeout(sleep_ms)
    except Exception:
        pass

    last_top = -1
    stable = 0
    for i in range(max(1, int(scan_rounds or 30))):
        # collect current DOM
        try:
            items = await _list_chat_links_with_unread(page)
        except Exception:
            items = []

        for it in items:
            # _list_chat_links_with_unread returns `id` (chat_id), not `chat_id`.
            cid = (it.get("id") or it.get("chat_id") or "").strip()
            if not cid:
                url = (it.get("url") or "").strip()
                m = re.search(r"/chat/(Y[0-9A-Za-z]+)", url)
                if m:
                    cid = m.group(1)
            if not cid:
                continue

            prev = merged.get(cid)
            if not prev:
                prev = dict(it)
                prev["chat_id"] = cid
                # prefer unread_count if present
                uc = str(it.get("unread_count") or it.get("unread") or "1").strip()
                prev["unread"] = uc if uc.isdigit() else "1"
                merged[cid] = prev
            else:
                # keep max unread_count
                try:
                    prev_uc = int(str(prev.get("unread") or "0").strip() or "0")
                except Exception:
                    prev_uc = 0
                try:
                    cur_uc = int(str(it.get("unread_count") or it.get("unread") or "0").strip() or "0")
                except Exception:
                    cur_uc = 0
                if cur_uc > prev_uc:
                    prev["unread"] = str(cur_uc)
                if not prev.get("preview") and it.get("preview"):
                    prev["preview"] = it.get("preview")
                if not prev.get("label") and it.get("label"):
                    prev["label"] = it.get("label")
                if not prev.get("text") and it.get("text"):
                    prev["text"] = it.get("text")

        # scroll down
        try:
            cur_top = await el.evaluate("(e) => e.scrollTop")
            max_top = await el.evaluate("(e) => e.scrollHeight - e.clientHeight")
            step = await el.evaluate("(e) => Math.max(220, Math.floor(e.clientHeight * 0.85))")
            next_top = min(int(max_top or 0), int(cur_top or 0) + int(step or 260))
            await el.evaluate("(e, v) => { try { e.scrollTop = v; e.dispatchEvent(new Event('scroll', {bubbles:true})); } catch (x) {} }", next_top)
            await page.wait_for_timeout(sleep_ms)
        except Exception:
            break

        # stop condition
        try:
            new_top = await el.evaluate("(e) => e.scrollTop")
        except Exception:
            break
        if int(new_top or 0) == int(last_top or 0):
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_top = int(new_top or 0)

    # optionally reset to top (do not depend on it)
    try:
        await el.evaluate("(e) => { try { e.scrollTop = 0; } catch (x) {} }")
    except Exception:
        pass

    out = list(merged.values())
    if on_log:
        try:
            unread_cnt = sum(1 for x in out if int(str(x.get("unread", "0") or "0").strip() or "0") > 0)
            on_log(f"[IM] scanned links total={len(out)} unread={unread_cnt}")
        except Exception:
            pass
    return out


async def capture_yahoo_im_fulltext(
    page,
    start_url: str,
    max_unread: int = 10,
    scroll_rounds: int = 30,
    on_log: Optional[Callable[[str], None]] = None,
    keep_unread: bool = True,
    unread_only: bool = True,
) -> List[Dict[str, str]]:
    """Capture Yahoo IM chat content.

    Key behaviors:
    - keep_unread=True: intercept "mark as read" network calls (e.g. /fe/api/im/putReadInfo)
      so the server-side unread red-dot will NOT be consumed by our scraper.
    - unread_only=True: for each unread conversation, only return the last `unread` incoming
      messages (fast, good for monitoring).
    - unread_only=False: scroll and capture more full text (slow).
    """
    if on_log is None:
        on_log = lambda *_: None

    chat_url = (start_url or "").strip()
    if not chat_url:
        raise ValueError("start_url is empty")

    # Install no-read routes BEFORE we enter any chat (otherwise the first open may consume the red dot).
    if keep_unread:
        await _install_no_read_marking_routes(page, on_log=on_log)

    on_log(f"[IM] 打开：{chat_url}")
    await page.goto(chat_url, wait_until="domcontentloaded")
    await _wait_for_chat_loaded(page, timeout_ms=20000)

    # 自动关闭"小心假買家"弹窗
    try:
        cb = page.locator("text=我已詳閱相關資訊").first
        if await cb.is_visible(timeout=3000):
            await cb.click()
            await page.wait_for_timeout(300)
            btn = page.locator("text=我知道了").first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(500)
                on_log("[IM] 已自动关闭假買家弹窗")
    except Exception:
        pass

    # Prefer collecting unread badge counts from the LEFT list (no clicking).
    previews = await _collect_unread_previews_in_dom(page)

    if previews:
        unread_links = []
        for p in previews:
            try:
                n = int((p.get("unread") or "0").strip() or "0")
            except Exception:
                n = 0
            if n <= 0:
                continue
            if not p.get("url"):
                continue
            unread_links.append(p)
    else:
        # Fallback: older method (may be less accurate on some UI versions).
        unread_links = await _list_chat_links_with_unread(page, max_scroll_rounds=max_unread, on_log=on_log)

    if not unread_links:
        on_log("[IM] 没找到未读会话")
        return []

    results: List[Dict[str, str]] = []
    total = min(max_unread, len(unread_links))

    for i, item in enumerate(unread_links[:max_unread], start=1):
        url = str(item.get("url", "") or "")
        if not url:
            continue

        cid = str(item.get("chat_id", "") or "")
        label = str(item.get("label", "") or "")
        preview = str(item.get("preview", "") or "")

        unread_n = None
        try:
            unread_n = int(str(item.get("unread", "") or item.get("badge", "0") or "0").strip() or "0")
        except Exception:
            unread_n = None

        on_log(f"[IM] 打开未读({i}/{total}): {label or cid or url} unread={unread_n or 0}")

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await _wait_for_chat_loaded(page, timeout_ms=20000)

            if unread_only:
                text = await _capture_current_conversation_sliced(page, unread_count=unread_n)
                results.append({
                    "chat_id": cid,
                    "label": label,
                    "url": url,
                    "text": text,
                    "unread": str(unread_n or 0),
                    "preview": preview,
                })
            else:
                d = await _capture_current_conversation(page, scroll_rounds=scroll_rounds)
                if isinstance(d, dict):
                    if unread_n is not None:
                        d["unread"] = str(unread_n)
                    if preview:
                        d["preview"] = preview
                    results.append(d)
                else:
                    results.append({
                        "chat_id": cid,
                        "label": label,
                        "url": url,
                        "text": str(d),
                        "unread": str(unread_n or 0),
                        "preview": preview,
                    })
        except Exception as e:
            on_log(f"[IM] 捕获失败: {label or cid or url} err={e}")
        finally:
            # Go back to the list page to keep DOM stable.
            try:
                await page.goto(chat_url, wait_until="domcontentloaded")
                await _wait_for_chat_loaded(page, timeout_ms=20000)
            except Exception:
                pass

    return results


async def capture_yahoo_im_unread_previews(
    page,
    start_url: str,
    max_items: int = 10,
    scan_rounds: int = 30,
    sleep_ms: int = 450,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, str]]:
    """
    Capture unread IM previews (badge count + last preview line) without clicking into chats.

    Note: preview may be ellipsized (UI truncation). For unread>1, usually only the last preview line is available
    unless you open the conversation (which will clear the badge).
    """
    if not start_url:
        return []

    # ensure on /myauc so we can extract shop code
    try:
        if "/myauc" not in (page.url or ""):
            await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    shop_code = await _extract_shop_code_from_myauc(page)
    if not shop_code:
        if on_log:
            on_log("[IM] cannot find shop_code on myauc")
        return []

    origin = "https://tw.bid.yahoo.com"
    chat_url = f"{origin}/chat/{shop_code}"
    if on_log:
        on_log(f"[IM-DIAG] capture_previews: shop_code={shop_code}, chat_url={chat_url}")

    # 在进入即时通页面前安装已读拦截，防止红点消失
    try:
        await _install_no_read_marking_routes(page, on_log=on_log)
    except Exception as _e_route:
        if on_log:
            on_log(f"[IM-DIAG] capture_previews: 已读拦截安装失败: {_e_route}")

    chat_loaded = False
    for _try in range(2):
        try:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
            chat_loaded = True
            if on_log:
                on_log(f"[IM-DIAG] capture_previews: chat页面已加载, url={page.url}")
            break
        except Exception as _e_goto:
            if on_log:
                on_log(f"[IM-DIAG] goto chat页面失败(try={_try+1}): {_e_goto}")
            if _try == 0:
                await page.wait_for_timeout(3000)
    if not chat_loaded:
        return []

    # 自动关闭"小心假買家"弹窗（勾选 checkbox + 点"我知道了"）
    try:
        cb = page.locator("text=我已詳閱相關資訊").first
        if await cb.is_visible(timeout=3000):
            await cb.click()
            await page.wait_for_timeout(300)
            btn = page.locator("text=我知道了").first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(500)
                if on_log:
                    on_log("[IM] 已自动关闭假買家弹窗")
    except Exception:
        pass

    merged: Dict[str, Dict[str, str]] = {}

    # 等待 React SPA 渲染联系人列表（domcontentloaded 不够，需等 channel 行出现）
    await _wait_for_chat_loaded(page, timeout_ms=20000)

    el = await _find_contact_list_container(page)
    # 慢网络下 channel 行可能还没渲染，重试2次
    if not el:
        for _retry in range(2):
            await page.wait_for_timeout(2000)
            el = await _find_contact_list_container(page)
            if el:
                break
    if on_log:
        on_log(f"[IM-DIAG] capture_previews: contact_list_container={'found' if el else 'NOT FOUND'}")
    # 诊断：dump容器内DOM结构
    if el and on_log:
        try:
            dom_diag = await page.evaluate(r"""() => {
              const root = document.querySelector('aside[class*="channelList"]') || document;
              const channels = root.querySelectorAll('div[class*="channel__"]');
              const dots = root.querySelectorAll('div[class*="dot__"]');
              // 采样第一个channel行的子元素class
              let sample = '';
              if (channels.length > 0) {
                const kids = Array.from(channels[0].querySelectorAll('*')).slice(0, 15);
                sample = kids.map(k => k.tagName + '.' + (k.className || '').toString().substring(0, 40)).join(' | ');
              }
              // 采样所有包含数字的小元素（可能是badge）
              let badgeSample = '';
              const smalls = root.querySelectorAll('span,em,i,b,div');
              const badges = [];
              for (const s of smalls) {
                const t = (s.textContent || '').trim();
                if (/^\d{1,3}$/.test(t)) {
                  const r = s.getBoundingClientRect();
                  if (r.width < 40 && r.height < 40 && r.width > 0) {
                    badges.push(t + '@' + s.tagName + '.' + (s.className || '').toString().substring(0, 30));
                  }
                }
              }
              badgeSample = badges.slice(0, 10).join(', ');
              return 'channels=' + channels.length + ',dots=' + dots.length + ',badges=[' + badgeSample + '],sample=' + sample.substring(0, 200);
            }""")
            on_log(f"[IM-DIAG] container_dom: {dom_diag}")
        except Exception as _e_diag:
            on_log(f"[IM-DIAG] container_dom_err: {_e_diag}")
    if el:
        # start from top
        try:
            await el.evaluate("(e) => { try { e.scrollTop = 0; } catch (x) {} }")
            await page.wait_for_timeout(sleep_ms)
        except Exception:
            pass

        last_top = -1
        stable = 0
        for _i in range(max(1, int(scan_rounds or 30))):
            try:
                items = await _collect_unread_previews_in_dom(page)
            except Exception:
                items = []

            for it in (items or []):
                cid = (it.get("cid") or it.get("chat_id") or "").strip()
                # cid 为空时用 label 作为临时 key（fulltext enrichment 阶段会通过 React fiber 补全）
                key = cid or (it.get("label") or "").strip()
                if not key:
                    continue
                it["chat_id"] = cid
                if not it.get("url") and it.get("href"):
                    it["url"] = "https://tw.bid.yahoo.com" + it["href"] if it["href"].startswith("/") else it["href"]
                prev = merged.get(key)
                if not prev:
                    merged[key] = dict(it)
                else:
                    # merge: keep larger unread count, keep preview/url if missing
                    try:
                        a = int(prev.get("unread") or "0")
                        b = int(it.get("unread") or "0")
                        if b > a:
                            prev["unread"] = str(b)
                    except Exception:
                        pass
                    if not prev.get("preview") and it.get("preview"):
                        prev["preview"] = it.get("preview")
                    if not prev.get("url") and it.get("url"):
                        prev["url"] = it.get("url")
                    if not prev.get("label") and it.get("label"):
                        prev["label"] = it.get("label")

            # scroll down
            try:
                cur_top = await el.evaluate("(e) => e.scrollTop")
                max_top = await el.evaluate("(e) => e.scrollHeight - e.clientHeight")
                step = await el.evaluate("(e) => Math.max(220, Math.floor(e.clientHeight * 0.85))")
                next_top = min(int(max_top or 0), int(cur_top or 0) + int(step or 260))
                await el.evaluate("(e, v) => { try { e.scrollTop = v; e.dispatchEvent(new Event('scroll', {bubbles:true})); } catch (x) {} }", next_top)
                await page.wait_for_timeout(sleep_ms)
            except Exception:
                break

            # stop if cannot scroll further
            try:
                new_top = await el.evaluate("(e) => e.scrollTop")
            except Exception:
                break
            if int(new_top or 0) == int(last_top or 0):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_top = int(new_top or 0)

        # reset scroll (optional)
        try:
            await el.evaluate("(e) => { try { e.scrollTop = 0; } catch (x) {} }")
        except Exception:
            pass
    else:
        # fallback: just collect what exists in DOM
        try:
            items = await _collect_unread_previews_in_dom(page)
        except Exception:
            items = []
        for it in (items or []):
            cid = (it.get("chat_id") or it.get("label") or "").strip()
            if cid:
                merged[cid] = dict(it)

    # return to /myauc so later scraping isn't affected
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    out = list(merged.values())
    # sort: more unread first
    def _key(x):
        try:
            return -int(str(x.get("unread") or "0"))
        except Exception:
            return 0
    out.sort(key=_key)

    if max_items and len(out) > int(max_items):
        out = out[: int(max_items)]

    if on_log:
        try:
            on_log(f"[IM] unread preview items={len(out)}")
        except Exception:
            pass

    # normalize fields
    norm: List[Dict[str, str]] = []
    for it in out:
        norm.append({
            "chat_id": (it.get("chat_id") or "").strip(),
            "label": (it.get("label") or it.get("chat_id") or "").strip(),
            "unread": str(it.get("unread") or "").strip() or "1",
            "preview": (it.get("preview") or "").strip(),
            "url": (it.get("url") or "").strip(),
        })
    return norm


def _enrich_via_http_only(
    preview_items: List[Dict[str, str]],
    shop_code: str,
    profile_dir,
    max_items: int,
    on_log,
    account_name: str = "",
) -> List[Dict[str, str]]:
    """纯 HTTP 获取全文，不需要浏览器导航到 chat 页面。

    当所有 preview_items 都有 chat_id 时使用此快速路径，
    跳过整个 chat 页面导航（节省 5-10 秒）。

    account_name(2026-04-30 v6.0.47):向下傳給 im_read_messages → hook C
    寫 runtime/im_metadata.jsonl 才有 account 欄位,daemon 才能 follow up。
    """
    from .im_http_ops import im_read_messages, build_channel_id

    enriched = []
    total = min(max_items, len(preview_items))

    for idx, it in enumerate(preview_items[:max_items], start=1):
        new_item = dict(it)
        cid = (it.get("chat_id") or "").strip()
        label = (it.get("label") or cid).strip()

        on_log(f"[IM-full] http({idx}/{total}): {label} cid={cid}")

        text = ""
        try:
            channel_id = build_channel_id(shop_code, cid)
            text = im_read_messages(
                profile_dir, channel_id,
                shop_id=shop_code,
                on_log=on_log,
                account_name=account_name,
            )
        except Exception as e:
            on_log(f"[IM-full] {label}: HTTP read failed: {e}")

        new_item["text"] = text or ""
        if not new_item.get("url"):
            new_item["url"] = f"https://tw.bid.yahoo.com/chat/{shop_code}"
        enriched.append(new_item)

    # 加回超出 max_items 的 items
    for it in preview_items[max_items:]:
        enriched.append(dict(it))

    return enriched


async def enrich_previews_with_fulltext(
    page,
    start_url: str,
    preview_items: List[Dict[str, str]],
    max_items: int = 10,
    on_log: Optional[Callable[[str], None]] = None,
    profile_dir=None,
    account_name: str = "",
) -> List[Dict[str, str]]:
    """点击左侧有红点的联系人行，抓取完整对话内容。

    不依赖 chat_id（Yahoo 新版 UI 可能没有 <a> 标签），
    而是直接点击行元素，从 URL 变化中获取对话内容。
    已读拦截在 capture_yahoo_im_unread_previews 中已安装，红点不会消失。

    Returns the same items with an added "text" field.
    """
    if on_log is None:
        on_log = lambda *_: None

    if not preview_items:
        return []

    origin = "https://tw.bid.yahoo.com"

    # 1) 提取 shop_code，构造 IM 首页 URL
    shop_code = ""
    try:
        if "/myauc" not in (page.url or ""):
            await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        shop_code = await _extract_shop_code_from_myauc(page)
    except Exception:
        pass

    if not shop_code:
        on_log("[IM-full] cannot find shop_code, skip fulltext")
        return preview_items

    # ── 快速路径：所有 preview_items 都有 chat_id → 纯 HTTP 获取全文，无需导航到 chat 页 ──
    all_have_cid = profile_dir and all(
        (it.get("chat_id") or "").strip() for it in preview_items
    )
    if all_have_cid:
        on_log(f"[IM-full] fast path: {len(preview_items)} items all have chat_id, HTTP-only")
        enriched = _enrich_via_http_only(
            preview_items, shop_code, profile_dir, max_items, on_log,
            account_name=account_name,
        )
        for it in enriched:
            it["shop_code"] = shop_code
        return enriched

    # ── 慢路径：需要导航到 chat 页面用 JS 提取 cid ──
    chat_home = f"{origin}/chat/{shop_code}"
    on_log(f"[IM-full] slow path: chat_home={chat_home}")

    # 2) 安装已读拦截（可能已安装，幂等）
    try:
        await _install_no_read_marking_routes(page, on_log=on_log)
    except Exception as _e_r2:
        if on_log:
            on_log(f"[IM-DIAG] enrich: 已读拦截安装失败: {_e_r2}")

    # 3) 导航到 IM 首页
    try:
        await page.goto(chat_home, wait_until="domcontentloaded", timeout=60000)
        await _wait_for_chat_loaded(page, timeout_ms=20000)
    except Exception as e:
        on_log(f"[IM-full] goto chat home failed: {e}")
        return preview_items

    # 4) 关闭弹窗
    try:
        cb = page.locator("text=我已詳閱相關資訊").first
        if await cb.is_visible(timeout=3000):
            await cb.click()
            await page.wait_for_timeout(300)
            btn = page.locator("text=我知道了").first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(500)
                on_log("[IM-full] 已自动关闭假買家弹窗")
    except Exception:
        pass

    # 5) 用 JS 找到所有有红点的行元素，点击进入对话
    enriched = await _click_unread_rows_and_capture(
        page, chat_home, preview_items, max_items, on_log,
        profile_dir=profile_dir, shop_code=shop_code,
    )

    # 5.5) 把 shop_code 写入每个 item，供下游发送时构造 channelId
    for it in enriched:
        it["shop_code"] = shop_code

    # 6) 返回 myauc
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    return enriched


async def _click_unread_rows_and_capture(
    page, chat_home: str, preview_items: List[Dict[str, str]],
    max_items: int, on_log,
    profile_dir=None, shop_code: str = "",
) -> List[Dict[str, str]]:
    """在 IM 页面，逐个点击有红点的联系人行，抓取完整对话。"""

    # 找到所有有红点的行
    rows_js = r"""
() => {
  const listRoot =
    document.querySelector('aside[class*="channelList__"]') ||
    document.querySelector('div[class*="channels__"]') ||
    document;
  const rows = Array.from(listRoot.querySelectorAll('div[class*="channel__"]'));
  const results = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const dot = row.querySelector('div[class*="dot__"]');
    if (!dot) continue;
    const txt = (dot.innerText || '').replace(/[^\d]/g, '');
    const n = parseInt(txt, 10);
    if (!Number.isFinite(n) || n <= 0) continue;
    const labelEl = row.querySelector('div[class*="userName__"]') ||
                    row.querySelector('div[class*="userName"]');
    const label = labelEl ? (labelEl.innerText || '').trim() : '';

    // 尝试多种方式提取 cid
    let cid = '';

    // ── 策略 A（最优先）：React fiber 提取买家专属 ID ──
    // channel.id 格式: "yahoo-bid-logbot1:y{卖家}:y{买家}"
    // 取不等于店铺 ID 的 Y 号 = 真正的买家 ID
    try {
      const shopMatch = (location.pathname || '').match(/\/chat\/(Y?\d+)/i);
      const shopId = shopMatch ? shopMatch[1].toLowerCase() : '';
      const rk = Object.keys(row).find(k => k.startsWith('__reactFiber'));
      if (rk) {
        let fiber = row[rk];
        for (let d = 0; d < 5 && fiber && !cid; d++) {
          const p = fiber.memoizedProps || fiber.pendingProps || {};
          const ch = p.channel || p.data || p.item || null;
          if (ch && typeof ch === 'object') {
            const chId = ch.id || ch.channelId || ch.chatId || '';
            if (chId && typeof chId === 'string') {
              const parts = chId.split(':');
              for (const pt of parts) {
                if (/^y\d{5,}$/i.test(pt) && pt.toLowerCase() !== shopId) {
                  cid = pt.replace(/^y/, 'Y');
                  break;
                }
              }
            }
          }
          fiber = fiber.return;
        }
      }
    } catch(e) {}

    // ── 策略 B（回退）：<a href="/chat/XXX"> ──
    if (!cid) {
      const a = row.querySelector('a[href*="/chat/"]');
      if (a) {
        const m = (a.getAttribute('href') || '').match(/\/chat\/([^\/\?#]+)/);
        if (m && m[1]) cid = m[1];
      }
    }

    // ── 策略 C（最后回退）：data 属性 / HTML 扫描 ──
    if (!cid) {
      cid = row.getAttribute('data-cid') || row.getAttribute('data-id') ||
            row.getAttribute('data-channel-id') || '';
    }
    if (!cid) {
      const html = row.innerHTML || '';
      const m2 = html.match(/Y\d{7,}/);
      if (m2) cid = m2[0];
    }

    results.push({ index: i, label: label, unread: n, cid: cid });
  }
  return results;
}
"""
    try:
        unread_rows = await page.evaluate(rows_js)
    except Exception as e:
        on_log(f"[IM-full] JS scan failed: {e}")
        return preview_items

    if not unread_rows:
        on_log("[IM-full] no unread rows found in DOM")
        return preview_items

    on_log(f"[IM-full] found {len(unread_rows)} unread rows")

    # 建立 label -> preview_item 的映射，用于匹配
    label_map: Dict[str, Dict[str, str]] = {}
    for it in preview_items:
        lb = (it.get("label") or "").strip()
        if lb:
            label_map[lb] = it

    enriched = []
    done_labels = set()
    total = min(max_items, len(unread_rows))

    # === 直接用 page.goto 逐个进入对话抓取（不点击行，红点不消失） ===
    for idx, row_info in enumerate(unread_rows[:max_items], start=1):
        row_label = row_info.get("label", "")
        row_unread = row_info.get("unread", 1)
        row_cid = row_info.get("cid", "")

        if not row_cid:
            on_log(f"[IM-full] ({idx}/{total}): {row_label} no cid, skip")
            matched = label_map.get(row_label)
            if matched:
                enriched.append(dict(matched))
                done_labels.add(row_label)
            continue

        chat_url = f"https://tw.bid.yahoo.com/chat/{shop_code}" if shop_code else f"https://tw.bid.yahoo.com/chat/{row_cid}"
        on_log(f"[IM-full] api({idx}/{total}): {row_label} cid={row_cid}")

        # 不打开对话页面，直接用 fetch API 获取消息（不触发已读）
        text = ""
        try:
            text = await _fetch_chat_messages_via_api(
                page, row_cid, on_log=on_log,
                profile_dir=profile_dir, shop_code=shop_code,
            )
        except Exception as e:
            on_log(f"[IM-full] {row_label}: api fetch failed: {e}")

        on_log(f"[IM-full] {row_label}: cid={row_cid} text_len={len(text or '')}")

        matched = label_map.get(row_label)
        if matched:
            new_item = dict(matched)
        else:
            new_item = {"label": row_label, "unread": str(row_unread), "preview": ""}

        new_item["text"] = text or ""
        new_item["chat_id"] = row_cid
        new_item["url"] = chat_url
        enriched.append(new_item)
        done_labels.add(row_label)

    # 把没有被点击到的 preview_items 也加回去
    for it in preview_items:
        lb = (it.get("label") or "").strip()
        if lb not in done_labels:
            enriched.append(dict(it))

    return enriched


async def _fetch_chat_messages_via_api(
    page, buyer_cid: str,
    on_log: Optional[Callable[[str], None]] = None,
    profile_dir=None,
    shop_code: str = "",
) -> str:
    """用 fetch API 直接获取对话消息，不打开对话页面，不触发已读。

    优先使用纯 HTTP（im_http_ops），失败时回退到浏览器内 fetch。
    """
    if on_log is None:
        on_log = lambda *_: None

    # 从当前 URL 提取店铺 ID
    shop_match = re.search(r'/chat/(Y?\d+)', page.url or '', re.IGNORECASE)
    shop_id = shop_match.group(1).lower() if shop_match else ""
    buyer_id = buyer_cid.lower()

    if not shop_id or not buyer_id:
        on_log(f"[IM-api] missing ids: shop={shop_id} buyer={buyer_id}")
        return ""

    # ── 优先：纯 HTTP（不需要浏览器，异常处理更好） ──
    if profile_dir:
        try:
            from .im_http_ops import im_read_messages, build_channel_id
            channel_id = build_channel_id(shop_code or shop_id, buyer_cid)
            text = im_read_messages(
                profile_dir, channel_id,
                shop_id=shop_code or shop_id,
                on_log=on_log,
            )
            if text:
                on_log(f"[IM-api] HTTP read OK: {len(text)} chars")
                return text
            on_log("[IM-api] HTTP read empty, fallback to browser fetch")
        except Exception as e:
            on_log(f"[IM-api] HTTP read failed, fallback: {e}")

    # ── 回退：浏览器内 fetch（原有逻辑） ──
    channel_id = f"yahoo-bid-logbot1:{shop_id}:{buyer_id}"
    on_log(f"[IM-api] browser fetch channel={channel_id}")

    # 尝试多个可能的 API 端点
    fetch_js = r"""
async (channelId) => {
  const base = 'https://tw.bid.yahoo.com/fe/api/im';
  const endpoints = [
    `${base}/getMessages?channelId=${encodeURIComponent(channelId)}&limit=30`,
    `${base}/getChannelMessages?channelId=${encodeURIComponent(channelId)}&limit=30`,
    `${base}/messages?channelId=${encodeURIComponent(channelId)}&limit=30`,
  ];

  for (const url of endpoints) {
    try {
      const r = await fetch(url, {credentials: 'include'});
      if (!r.ok) continue;
      const data = await r.json();
      // 返回原始 JSON 让 Python 解析
      return JSON.stringify({ok: true, url: url, data: data});
    } catch(e) {
      continue;
    }
  }

  // 如果标准端点都失败，尝试从页面的 __NEXT_DATA__ 或 window 对象找 API 信息
  try {
    const nd = window.__NEXT_DATA__;
    if (nd) {
      return JSON.stringify({ok: false, hint: 'has_next_data', keys: Object.keys(nd)});
    }
  } catch(e) {}

  return JSON.stringify({ok: false, hint: 'all_endpoints_failed'});
}
"""
    try:
        result_str = await page.evaluate(fetch_js, channel_id)
        result = __import__('json').loads(result_str or '{}')
    except Exception as e:
        on_log(f"[IM-api] fetch failed: {e}")
        return ""

    if not result.get("ok"):
        on_log(f"[IM-api] no working endpoint: {result.get('hint', '')}")
        # Fallback: 尝试从 React store/state 获取已缓存的消息
        return await _extract_messages_from_react_store(page, channel_id, on_log)

    # 解析消息数据
    data = result.get("data", {})
    on_log(f"[IM-api] got response from {result.get('url', '')}")
    text = _parse_im_api_messages(data, on_log, shop_id=shop_id)

    # 如果 messages 为空，尝试反转 shop/buyer 顺序
    if not text and shop_id and buyer_id:
        alt_channel = f"yahoo-bid-logbot1:{buyer_id}:{shop_id}"
        on_log(f"[IM-api] retry with reversed channel: {alt_channel}")
        try:
            result_str2 = await page.evaluate(fetch_js, alt_channel)
            result2 = __import__('json').loads(result_str2 or '{}')
            if result2.get("ok"):
                data2 = result2.get("data", {})
                text = _parse_im_api_messages(data2, on_log, shop_id=shop_id)
        except Exception:
            pass

    return text


def _parse_im_api_messages(data, on_log, shop_id: str = "") -> str:
    """解析 Yahoo IM API 返回的消息数据。用 sender 区分买家/卖家。"""
    import json as _json
    messages = []

    # 尝试多种可能的数据结构
    if isinstance(data, dict):
        msgs = data.get("messages")
        if msgs is None:
            msgs = data.get("data") or data.get("result") or []
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("items") or msgs.get("list") or []
        messages = msgs if isinstance(msgs, list) else []

    if not messages:
        on_log(f"[IM-api] cannot parse messages, keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
        return ""

    # 标准化 shop_id 用于比较
    shop_lower = shop_id.lower().lstrip("y") if shop_id else ""

    # IM API 返回的消息是从新到旧排列的，反转为时间正序（旧→新）
    messages = list(reversed(messages))

    lines = []
    for msg in messages[-50:]:
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type", "")
        # 跳过贴图和撤回消息
        if msg_type == "sticker":
            continue
        if msg_type == "recalled" or msg.get("recalled"):
            continue

        # 判断发送者：sender 包含 shop_id 则为卖家
        sender = str(msg.get("sender") or "").lower()
        is_seller = bool(shop_lower and shop_lower in sender)
        prefix = "【卖家】" if is_seller else "【买家】"

        value = msg.get("value")

        if isinstance(value, dict):
            text = value.get("content") or ""

            # listing 类型（商品卡片）：提取商品编号、标题、价格
            if msg_type == "listing":
                item_id = value.get("id") or ""
                title = value.get("title") or ""
                price = value.get("price") or ""
                listing_line = ""
                if item_id:
                    listing_line = f"https://tw.bid.yahoo.com/item/{item_id}"
                if title:
                    listing_line += f" {title}"
                if price:
                    listing_line += f" ${price}"
                if listing_line:
                    lines.append(f"{prefix}{listing_line.strip()}")
                continue
            if text:
                text = str(text).strip()
                if text and text.lower() != "recalled":
                    lines.append(f"{prefix}{text}")
        else:
            text = msg.get("text") or msg.get("content") or msg.get("body") or ""
            if text:
                text = str(text).strip()
                if text and text.lower() != "recalled":
                    lines.append(f"{prefix}{text}")

    return "\n".join(lines)


async def _extract_messages_from_react_store(
    page, channel_id: str,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    """从 React store/Redux state 中提取已缓存的消息。

    Yahoo IM SPA 在加载 IM 首页时可能已经预加载了部分消息数据。
    """
    if on_log is None:
        on_log = lambda *_: None

    store_js = r"""
(channelId) => {
  // 搜索 Redux store 或 React context 中的消息数据
  const results = [];

  // 方法1: 从 __NEXT_DATA__ 提取
  try {
    const nd = window.__NEXT_DATA__;
    if (nd && nd.props && nd.props.pageProps) {
      const pp = nd.props.pageProps;
      const str = JSON.stringify(pp);
      if (str.includes(channelId)) {
        results.push({source: 'next_data', found: true});
      }
    }
  } catch(e) {}

  // 方法2: 从 Redux store 提取
  try {
    const root = document.querySelector('#__next') || document.querySelector('#root');
    if (root) {
      const rk = Object.keys(root).find(k => k.startsWith('__reactFiber'));
      if (rk) {
        let fiber = root[rk];
        for (let d = 0; d < 30 && fiber; d++) {
          const s = fiber.memoizedState;
          if (s && s.memoizedState && typeof s.memoizedState === 'object') {
            try {
              const str = JSON.stringify(s.memoizedState).substring(0, 200);
              if (str.includes('message') || str.includes('channel')) {
                results.push({source: 'fiber_state', depth: d, preview: str});
              }
            } catch(e2) {}
          }
          fiber = fiber.return;
        }
      }
    }
  } catch(e) {}

  return JSON.stringify(results);
}
"""
    try:
        result_str = await page.evaluate(store_js, channel_id)
        on_log(f"[IM-api] store search: {(result_str or '')[:300]}")
    except Exception as e:
        on_log(f"[IM-api] store search failed: {e}")

    return ""


async def _extract_cid_by_hook_click(page, row_index: int) -> str:
    """Hook history.pushState，点击行提取 cid，然后撤销导航。

    Yahoo IM 是 SPA，点击左侧联系人行时会调用 history.pushState
    把 URL 从 /chat/SHOP_CODE 改成 /chat/BUYER_CID。
    我们拦截这个调用，记录 cid，然后 history.back() 恢复。
    """
    # 1) 安装 pushState hook
    hook_js = r"""
() => {
  window.__ym_captured_cid = '';
  window.__ym_orig_pushState = history.pushState.bind(history);
  history.pushState = function(state, title, url) {
    const s = String(url || '');
    const m = s.match(/\/chat\/([^\/\?#]+)/);
    if (m && m[1]) {
      window.__ym_captured_cid = m[1];
    }
    // 仍然执行原始 pushState（否则 SPA 可能报错）
    return window.__ym_orig_pushState(state, title, url);
  };
}
"""
    await page.evaluate(hook_js)

    # 2) 点击行
    click_js = r"""
(idx) => {
  const listRoot =
    document.querySelector('aside[class*="channelList__"]') ||
    document.querySelector('div[class*="channels__"]') ||
    document;
  const rows = Array.from(
    listRoot.querySelectorAll('div[class*="channel__"]')
  );
  if (idx < 0 || idx >= rows.length) return false;
  rows[idx].click();
  return true;
}
"""
    clicked = await page.evaluate(click_js, row_index)
    if not clicked:
        await _restore_push_state(page)
        return ""

    # 3) 等待 pushState 被触发
    await page.wait_for_timeout(600)

    # 4) 读取捕获的 cid
    cid = await page.evaluate("() => window.__ym_captured_cid || ''")

    # 5) 还原 pushState
    await _restore_push_state(page)

    # 6) history.back() 恢复到 IM 首页（撤销点击产生的导航）
    if cid:
        try:
            await page.evaluate("() => history.back()")
            await page.wait_for_timeout(800)
        except Exception:
            pass

    return cid


async def _restore_push_state(page):
    """还原被 hook 的 history.pushState。"""
    try:
        await page.evaluate(r"""
() => {
  if (window.__ym_orig_pushState) {
    history.pushState = window.__ym_orig_pushState;
    delete window.__ym_orig_pushState;
  }
  delete window.__ym_captured_cid;
}
""")
    except Exception:
        pass


async def _click_row_by_index(page, row_index: int) -> bool:
    """点击左侧联系人列表中第 row_index 个 channel 行。"""
    click_js = r"""
(idx) => {
  const listRoot =
    document.querySelector('aside[class*="channelList__"]') ||
    document.querySelector('div[class*="channels__"]') ||
    document;
  const rows = Array.from(listRoot.querySelectorAll('div[class*="channel__"]'));
  if (idx < 0 || idx >= rows.length) return false;
  rows[idx].click();
  return true;
}
"""
    try:
        return await page.evaluate(click_js, row_index)
    except Exception:
        return False


async def _install_no_read_marking_routes(page, on_log: Optional[Callable[[str], None]] = None) -> None:
    """Prevent Yahoo IM from marking conversations as 'read' while we scrape.

    Yahoo IM typically sends requests like:
      - /fe/api/im/putReadInfo
      - /fe/api/im/putLastAccessedTs
    when a conversation is opened. We intercept and *fulfill* them locally so:
      - Page JS thinks it succeeded (no noisy errors)
      - Server never receives the call (unread badge will remain on server)
    """
    if getattr(page, "_yahoo_im_no_read_routes", False):
        return

    patterns = [
        "**/fe/api/im/putReadInfo*",
        "**/fe/api/im/putLastAccessedTs*",
        "**/fe/api/im/*putRead*",
        "**/fe/api/im/*LastAccessed*",
    ]

    async def _fake_ok(route, request):
        try:
            if on_log:
                on_log(f"[IM] 拦截已读上报: {request.method} {request.url}")
            await route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body='{"ok":true}',
            )
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    for pat in patterns:
        try:
            await page.route(pat, _fake_ok)
        except Exception:
            # ignore; routing may fail if page already closed
            pass

    setattr(page, "_yahoo_im_no_read_routes", True)

