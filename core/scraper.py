from __future__ import annotations
from typing import Dict, Any
import re

# 说明：
# Yahoo 页面会改版，所以这里尽量用“文本+正则”做高兼容提取。
# 如果你发现提取不到数字，你把 myauc 页面的 HTML 片段/截图发我，我只改这个文件即可。

JS_SCRAPE = r"""(() => {
  const LABELS_PAID = ["已付款待出貨訂單", "已付款待出货订单"];
  const LABELS_COD  = ["取貨付款訂單", "取货付款订单"];
  const LABELS_IM   = ["即時通", "即时通"];

  const norm = (s) => (s || "").replace(/\s+/g, "").trim();

  const isVisible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (!st) return false;
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity || "1") === 0) return false;
    const r = el.getBoundingClientRect();
    return !!r && r.width > 0 && r.height > 0;
  };

  const digitText = (el) => {
    if (!el) return null;
    const t = (el.textContent || "").trim();
    if (/^\d{1,3}$/.test(t)) return parseInt(t, 10);
    return null;
  };

  const findBadgeInside = (root) => {
    if (!root) return 0;

    // 1) digits-only badges (best)
    const nodes = Array.from(root.querySelectorAll("span,em,i,b,strong,div,a,button"));
    const cands = [];
    for (const n of nodes) {
      const v = digitText(n);
      if (v === null) continue;
      if (v > 999) continue;
      if (!isVisible(n)) continue;
      const r = n.getBoundingClientRect();
      // badges are small; filter out large numbers/blocks
      if (r.width > 80 || r.height > 80) continue;
      cands.push({ v, area: r.width * r.height, top: r.top, left: r.left });
    }
    if (cands.length) {
      cands.sort((a, b) => (a.area - b.area) || (a.top - b.top) || (a.left - b.left));
      return cands[0].v;
    }

    // 2) red-dot badges without digits (treat as 1)
    const dots = Array.from(root.querySelectorAll("span,div,i,em,b,strong"));
    for (const n of dots) {
      if (!isVisible(n)) continue;
      const r = n.getBoundingClientRect();
      if (r.width < 6 || r.height < 6 || r.width > 20 || r.height > 20) continue;
      const st = window.getComputedStyle(n);
      const bg = st.backgroundColor || "";
      if (bg === "transparent" || bg === "rgba(0, 0, 0, 0)") continue;
      // round-ish badge
      const br = parseFloat(st.borderRadius || "0");
      if (br < 4) continue;
      return 1;
    }
    return 0;
  };

  const pickBestCandidate = (els) => {
    let best = null;
    for (const el of els) {
      if (!el) continue;
      if (!isVisible(el)) continue;
      const r = el.getBoundingClientRect();
      // avoid whole-page containers
      if (r.width > 1200 && r.height > 200) continue;
      if (r.height > 140) continue;
      const role = el.getAttribute("role") || "";
      const score = (role === "tab" ? -50 : 0) + r.height + (r.width / 200) + r.top / 50;
      if (!best || score < best.score) best = { el, score };
    }
    return best ? best.el : null;
  };

  const findTabCount = (labels) => {
    for (const label of labels) {
      const nl = norm(label);
      if (!nl) continue;
      // prefer role=tab first
      const roleTabs = Array.from(document.querySelectorAll('[role="tab"]'))
        .filter(el => norm(el.textContent).includes(nl));
      let target = pickBestCandidate(roleTabs);
      if (!target) {
        // fallback: links/buttons containing the label
        const candidates = Array.from(document.querySelectorAll("a,button,li,div,span"))
          .filter(el => norm(el.textContent).includes(nl));
        target = pickBestCandidate(candidates);
      }
      if (!target) continue;

      // Search badge within the tab node or its parent
      let v = findBadgeInside(target);
      if (!v && target.parentElement) v = findBadgeInside(target.parentElement);
      if (v) return v;

      // Sometimes digit is appended into textContent (e.g. label + "1")
      const txt = norm(target.textContent);
      const m = txt.match(new RegExp(nl + "(\\d{1,3})"));
      if (m) return parseInt(m[1], 10) || 0;
    }
    return 0;
  };

  const findIM = () => {
    const diag = [];
    // 1) known ids
    const knownIds = [
      "im-unread-channels-count",
      "imUnreadCount",
      "im_unread_count",
      "unreadCount",
      "unread-count"
    ];
    for (const id of knownIds) {
      const el = document.getElementById(id);
      if (el) {
        const v = digitText(el);
        const vis = isVisible(el);
        diag.push("id:" + id + "=" + v + ",vis=" + vis);
        if (v !== null && vis) return { v, diag: diag.join("|") };
      }
    }

    // 2) element containing "即時通"
    for (const label of LABELS_IM) {
      const nl = norm(label);
      const cands = Array.from(document.querySelectorAll("a,button,div,span,li"))
        .filter(el => norm(el.textContent).includes(nl));
      diag.push("label:" + label + ",cands=" + cands.length);
      const target = pickBestCandidate(cands);
      if (target) {
        const r = target.getBoundingClientRect();
        diag.push("target:" + Math.round(r.width) + "x" + Math.round(r.height) + "@" + Math.round(r.top));
        let v = findBadgeInside(target);
        if (!v && target.parentElement) v = findBadgeInside(target.parentElement);
        diag.push("badge=" + v);
        if (v) return { v, diag: diag.join("|") };
      }
    }

    // 3) no fallback — if we can't find IM through known IDs or label, return 0
    diag.push("nothing_found");
    return { v: 0, diag: diag.join("|") };
  };

  const findItemCount = () => {
    // "目前您共有：直購商品 13792 件" — 完整数字，不会被截断
    const LABELS = ["直購商品", "直购商品"];
    const body = document.body;
    if (!body) return 0;
    const bodyText = body.innerText || "";
    for (const label of LABELS) {
      const pattern = new RegExp(label + "\\s*(\\d{1,7})\\s*件");
      const m = bodyText.match(pattern);
      if (m) return parseInt(m[1], 10) || 0;
    }
    return 0;
  };

  const detectSuspended = () => {
    // 常见停权提示（繁/简）。尽量用明确短语，避免误判。
    const PHRASES_STRICT = [
      "您的帳號已被停權",
      "您的账号已被停权",
      "帳號已被停權",
      "账号已被停权",
    ];
    const PHRASES_HINT = [
      "停權原因",
      "停权原因",
    ];

    const body = document.body;
    const bodyText = norm(body ? (body.innerText || "") : "");

    for (const p of PHRASES_STRICT) {
      const np = norm(p);
      if (np && bodyText.includes(np)) {
        return { suspended: true, msg: p };
      }
    }

    // 次严格：找可见节点包含停权原因（配合可见性过滤）
    const nodes = Array.from(document.querySelectorAll("div,span,p,li,section,header"));
    for (const el of nodes) {
      if (!el) continue;
      if (!isVisible(el)) continue;
      const t = norm(el.textContent || "");
      if (!t) continue;
      for (const p of PHRASES_STRICT) {
        const np = norm(p);
        if (np && t.includes(np)) {
          const raw = (el.textContent || "").trim();
          return { suspended: true, msg: raw.slice(0, 160) };
        }
      }
      for (const p of PHRASES_HINT) {
        const np = norm(p);
        if (np && t.includes(np)) {
          const raw = (el.textContent || "").trim();
          // 仅当页面顶部区域出现提示时才算（减少误判）
          const r = el.getBoundingClientRect();
          if (r && r.top >= 0 && r.top < 200) {
            return { suspended: true, msg: raw.slice(0, 160) };
          }
        }
      }
    }

    return { suspended: false, msg: "" };
  };

  const susp = detectSuspended();
  const imResult = findIM();

  return {
    paid_to_ship: findTabCount(LABELS_PAID),
    cod: findTabCount(LABELS_COD),
    im: imResult.v,
    im_diag: imResult.diag,
    item_count: findItemCount(),
    suspended: susp.suspended,
    suspend_msg: susp.msg,
    url: location.href,
    ts: Date.now()
  };
})();"""


async def scrape_myauc(page) -> Dict[str, Any]:
    """Return dict: paid_to_ship, cod, im, item_count"""
    data = await page.evaluate(JS_SCRAPE)
    # 规范化
    def _int(x):
        try:
            return int(x)
        except Exception:
            return 0
    return {
        "paid_to_ship": _int(data.get("paid_to_ship", 0)),
        "cod": _int(data.get("cod", 0)),
        "im": _int(data.get("im", 0)),
        "im_diag": str(data.get("im_diag", "") or ""),
        "item_count": _int(data.get("item_count", 0)),
        "suspended": bool(data.get("suspended", False)),
        "suspend_msg": str(data.get("suspend_msg", "") or "")[:200],
        "url": data.get("url", ""),
        "ts": data.get("ts", 0),
    }
