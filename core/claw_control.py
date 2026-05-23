from __future__ import annotations
import json
import time
import traceback
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

# =========================
# ClawDBot Control (File Queue)
# =========================
#
# Design goals:
# - ZERO impact on existing features (only add a controller)
# - No open ports, no firewall issues: file-based inbox/out
# - "Cover whole software": provide universal UI operations:
#       - ui.snapshot: enumerate widgets (buttons/entries/combos/checks/text/treeviews) with tk paths
#       - ui.invoke / ui.invoke_by_text: invoke any button
#       - ui.set / ui.get: set/get widget values
#       - ui.select_tab: switch notebook tabs
#       - log.tail: read last N lines from the log Text widget
#
# ClawDBot writes JSON into inbox/ (write *.tmp then rename to *.json).
# XDZHGL reads, executes on UI thread (tk main thread), and writes JSON response to out/.
#
# IMPORTANT:
# - This is intended for local/controlled environment. Do NOT share inbox to untrusted parties.

def _now_ts() -> float:
    return time.time()

def _safe_read_json(p: Path) -> Dict[str, Any]:
    raw = p.read_bytes()
    # utf-8-sig handles BOM
    txt = raw.decode("utf-8-sig", errors="replace")
    return json.loads(txt)

def _atomic_write_json(p: Path, obj: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(p)

def _widget_text(w: tk.Widget) -> str:
    try:
        return str(w.cget("text") or "")
    except Exception:
        return ""

def _widget_value_get(app: tk.Tk, w: tk.Widget) -> Any:
    # Return best-effort value for common widgets
    if isinstance(w, (tk.Entry, ttk.Entry)):
        try:
            return w.get()
        except Exception:
            return None
    if isinstance(w, ttk.Combobox):
        try:
            return w.get()
        except Exception:
            return None
    if isinstance(w, (tk.Text,)):
        try:
            return w.get("1.0", "end-1c")
        except Exception:
            return None
    if isinstance(w, (ttk.Checkbutton, tk.Checkbutton)):
        # prefer variable if present
        try:
            var_name = w.cget("variable")
            if var_name:
                v = app.getvar(var_name)
                # tk uses "0"/"1" sometimes
                if str(v) in ("0", "1"):
                    return str(v) == "1"
                return v
        except Exception:
            pass
        # fallback to state
        try:
            if isinstance(w, ttk.Checkbutton):
                return bool(w.instate(["selected"]))
        except Exception:
            pass
        return None
    if isinstance(w, ttk.Treeview):
        try:
            sel = w.selection()
            return {"selection": list(sel)}
        except Exception:
            return None
    return None

def _widget_value_set(app: tk.Tk, w: tk.Widget, value: Any) -> None:
    # Best-effort setter for common widgets
    if isinstance(w, (tk.Entry, ttk.Entry)):
        w.delete(0, "end")
        w.insert(0, "" if value is None else str(value))
        return
    if isinstance(w, ttk.Combobox):
        w.set("" if value is None else str(value))
        return
    if isinstance(w, tk.Text):
        w.delete("1.0", "end")
        w.insert("1.0", "" if value is None else str(value))
        return
    if isinstance(w, (ttk.Checkbutton, tk.Checkbutton)):
        want = bool(value)
        # try variable first
        try:
            var_name = w.cget("variable")
            if var_name:
                app.setvar(var_name, "1" if want else "0")
                return
        except Exception:
            pass
        # fallback: invoke to reach desired state
        try:
            cur = None
            if isinstance(w, ttk.Checkbutton):
                cur = bool(w.instate(["selected"]))
            if cur is None:
                # try read current via get
                cur = bool(_widget_value_get(app, w))
            if cur != want:
                w.invoke()
            return
        except Exception:
            return
    raise ValueError(f"Unsupported widget type for set(): {type(w)}")

def _is_descendant(widget_path: str, ancestor_path: str) -> bool:
    return widget_path == ancestor_path or widget_path.startswith(ancestor_path + ".")

class ClawControlManager:
    """
    File-queue controller to let ClawDBot control XDZHGL's UI / features.
    """
    def __init__(self, app: tk.Tk, base_dir: Optional[Path] = None):
        self.app = app
        self.base_dir = Path(base_dir) if base_dir else Path(getattr(app, "BASE_DIR", None) or getattr(app, "base_dir", None) or Path.cwd())
        # Default cmd directory under project root
        _claw_dir = (getattr(app, "settings", {}) or {}).get("claw_cmd_dir") if isinstance(getattr(app, "settings", None), dict) else None
        self.cmd_root = Path(_claw_dir) if _claw_dir else None
        if self.cmd_root:
            self.cmd_root = Path(self.cmd_root)
        else:
            self.cmd_root = self.base_dir / "claw_cmd"

        self.inbox = self.cmd_root / "inbox"
        self.out = self.cmd_root / "out"
        self.done = self.cmd_root / "done"
        self.bad = self.cmd_root / "bad"

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_ids: set[str] = set()

        # runtime cache
        self._last_snapshot: Optional[Dict[str, Any]] = None

    def log(self, msg: str) -> None:
        try:
            if hasattr(self.app, "log"):
                self.app.log(msg)
        except Exception:
            pass

    def start(self) -> None:
        """Start background polling thread."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)
        self.done.mkdir(parents=True, exist_ok=True)
        self.bad.mkdir(parents=True, exist_ok=True)

        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._loop, name="claw_control", daemon=True)
        self._thread.start()

        # write initial snapshot (best effort)
        # NOTE: start() is called via app.after() so we are already on the UI thread.
        # Calling _call_ui() here would deadlock (it posts to UI thread and waits).
        try:
            snap = self._snapshot_widgets()
            self._last_snapshot = snap
            _atomic_write_json(self.cmd_root / "ui_snapshot.json", snap)
        except Exception:
            pass

        self.log(f"[CLAW] 控制器已启动：{self.inbox}")

    def stop(self) -> None:
        self._stop.set()

    # -------------------------
    # UI thread bridge
    # -------------------------
    def _call_ui(self, fn, timeout: float = 30.0):
        ev = threading.Event()
        box: Dict[str, Any] = {}

        def _run():
            try:
                box["res"] = fn()
            except Exception:
                box["err"] = traceback.format_exc()
            finally:
                ev.set()

        try:
            self.app.after(0, _run)
        except Exception:
            # if app not ready
            raise

        if not ev.wait(timeout):
            raise TimeoutError(f"UI call timeout after {timeout}s")
        if "err" in box:
            raise RuntimeError(box["err"])
        return box.get("res")

    # -------------------------
    # Widget snapshot & helpers
    # -------------------------
    def _snapshot_widgets(self) -> Dict[str, Any]:
        """
        Enumerate widgets for universal control.
        Returns a dict with tabs + common widgets.
        """
        snap: Dict[str, Any] = {
            "ts": _now_ts(),
            "tabs": [],
            "widgets": [],
        }

        nb = getattr(self.app, "nb", None)
        tab_frames = getattr(self.app, "_tab_frames", None) or {}

        # tabs
        if nb is not None:
            try:
                for i in range(nb.index("end")):
                    tab_id = nb.tabs()[i]
                    tab_text = nb.tab(tab_id, "text")
                    snap["tabs"].append({"index": i, "text": tab_text, "tab_id": tab_id})
            except Exception:
                pass

        # walk widget tree from root
        root = self.app
        all_widgets: List[tk.Widget] = [root]
        idx = 0
        while idx < len(all_widgets):
            w = all_widgets[idx]
            idx += 1
            try:
                for c in w.winfo_children():
                    all_widgets.append(c)
            except Exception:
                continue

        for w in all_widgets:
            try:
                path = str(w)
            except Exception:
                continue

            # determine tab (best effort)
            tab_name = ""
            try:
                for tname, tframe in tab_frames.items():
                    if _is_descendant(path, str(tframe)):
                        tab_name = tname
                        break
            except Exception:
                pass

            wcls = ""
            try:
                wcls = w.winfo_class()
            except Exception:
                wcls = type(w).__name__

            record: Dict[str, Any] = {
                "path": path,
                "class": wcls,
                "py_type": type(w).__name__,
                "tab": tab_name,
            }

            # add common attributes
            txt = _widget_text(w)
            if txt:
                record["text"] = txt

            # keep only useful widgets (buttons/entries/combos/checks/text/treeview)
            is_useful = isinstance(w, (tk.Button, ttk.Button, tk.Entry, ttk.Entry, ttk.Combobox, ttk.Checkbutton, tk.Checkbutton, tk.Text, ttk.Treeview))
            if not is_useful:
                continue

            # widget kind
            if isinstance(w, (tk.Button, ttk.Button)):
                record["kind"] = "button"
            elif isinstance(w, (tk.Entry, ttk.Entry)):
                record["kind"] = "entry"
            elif isinstance(w, ttk.Combobox):
                record["kind"] = "combobox"
            elif isinstance(w, (ttk.Checkbutton, tk.Checkbutton)):
                record["kind"] = "checkbutton"
            elif isinstance(w, tk.Text):
                record["kind"] = "text"
            elif isinstance(w, ttk.Treeview):
                record["kind"] = "treeview"
            else:
                record["kind"] = "widget"

            # grid info (optional, helps identify)
            try:
                gi = w.grid_info()
                if gi:
                    record["grid"] = {k: gi.get(k) for k in ("row", "column", "rowspan", "columnspan", "sticky")}
            except Exception:
                pass

            snap["widgets"].append(record)

        return snap

    def _find_buttons_by_text(self, text: str, tab: str = "", contains: bool = False) -> List[Dict[str, Any]]:
        snap = self._snapshot_widgets()
        targets = []
        for w in snap.get("widgets", []):
            if w.get("kind") != "button":
                continue
            if tab and w.get("tab") != tab:
                continue
            t = w.get("text", "")
            if contains:
                if text in t:
                    targets.append(w)
            else:
                if t == text:
                    targets.append(w)
        return targets

    def _get_widget(self, path: str) -> tk.Widget:
        return self.app.nametowidget(path)

    # -------------------------
    # Command handlers
    # -------------------------
    def _handle(self, cmd: str, args: Dict[str, Any]) -> Dict[str, Any]:
        # meta
        if cmd in ("ping", "health"):
            return {"pong": True, "ts": _now_ts()}
        if cmd == "help":
            return {"cmds": self._cmd_catalog()}
        if cmd == "ui.snapshot":
            snap = self._snapshot_widgets()
            self._last_snapshot = snap
            # also update ui_snapshot.json
            try:
                _atomic_write_json(self.cmd_root / "ui_snapshot.json", snap)
            except Exception:
                pass
            return snap
        if cmd == "ui.select_tab":
            tab_text = str(args.get("tab", "")).strip()
            if not tab_text:
                raise ValueError("args.tab required")
            nb = getattr(self.app, "nb", None)
            tab_frames = getattr(self.app, "_tab_frames", None) or {}
            if nb is None:
                raise ValueError("App has no notebook (self.nb)")
            if tab_text not in tab_frames:
                raise ValueError(f"Unknown tab: {tab_text}. Known: {list(tab_frames.keys())}")
            nb.select(tab_frames[tab_text])
            return {"selected": tab_text}
        if cmd == "ui.invoke":
            path = str(args.get("path", "")).strip()
            if not path:
                raise ValueError("args.path required")
            w = self._get_widget(path)
            # invoke if possible
            if hasattr(w, "invoke"):
                w.invoke()
                return {"invoked": path, "text": _widget_text(w)}
            raise ValueError(f"Widget has no invoke(): {path}")
        if cmd == "ui.invoke_by_text":
            text = str(args.get("text", "")).strip()
            if not text:
                raise ValueError("args.text required")
            tab = str(args.get("tab", "")).strip()
            contains = bool(args.get("contains", False))
            matches = self._find_buttons_by_text(text=text, tab=tab, contains=contains)
            if not matches:
                return {"ok": False, "error": "no matches", "matches": []}
            if len(matches) > 1 and not args.get("path"):
                # ambiguous
                return {"ok": False, "error": "ambiguous", "matches": matches[:30]}
            # choose
            target = None
            if args.get("path"):
                p = str(args.get("path"))
                for m in matches:
                    if m.get("path") == p:
                        target = m
                        break
                if target is None:
                    return {"ok": False, "error": "path not in matches", "matches": matches[:30]}
            else:
                target = matches[0]
            w = self._get_widget(target["path"])
            if hasattr(w, "invoke"):
                w.invoke()
                return {"invoked": target["path"], "text": target.get("text",""), "tab": target.get("tab","")}
            raise ValueError(f"Widget has no invoke(): {target['path']}")
        if cmd == "ui.set":
            path = str(args.get("path", "")).strip()
            if not path:
                raise ValueError("args.path required")
            value = args.get("value", "")
            w = self._get_widget(path)
            _widget_value_set(self.app, w, value)
            return {"set": path, "value": value}
        if cmd == "ui.get":
            path = str(args.get("path", "")).strip()
            if not path:
                raise ValueError("args.path required")
            w = self._get_widget(path)
            return {"path": path, "value": _widget_value_get(self.app, w), "text": _widget_text(w), "type": type(w).__name__}
        if cmd == "ui.find":
            # find widgets by text substring (best effort)
            q = str(args.get("q","")).strip()
            tab = str(args.get("tab","")).strip()
            kind = str(args.get("kind","")).strip()  # button/entry/...
            if not q:
                raise ValueError("args.q required")
            snap = self._snapshot_widgets()
            out = []
            for w in snap.get("widgets", []):
                if tab and w.get("tab") != tab:
                    continue
                if kind and w.get("kind") != kind:
                    continue
                t = (w.get("text","") or "")
                if q in t:
                    out.append(w)
            return {"q": q, "matches": out[:200]}
        if cmd == "log.tail":
            n = int(args.get("n", 200))
            # read from app's log Text widget if present
            txt_log = getattr(self.app, "txt_log", None)
            if txt_log is None:
                return {"lines": [], "n": n}
            content = txt_log.get("1.0", "end-1c")
            lines = content.splitlines()
            return {"n": n, "lines": lines[-n:]}
        if cmd == "app.quit":
            try:
                self.app.after(0, self.app.destroy)
            except Exception:
                pass
            return {"quit": True}

        raise ValueError(f"Unknown cmd: {cmd}")

    def _cmd_catalog(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "ping", "args": {}, "desc": "health check"},
            {"cmd": "help", "args": {}, "desc": "list supported commands"},
            {"cmd": "ui.snapshot", "args": {}, "desc": "dump all control widgets (also writes claw_cmd/ui_snapshot.json)"},
            {"cmd": "ui.find", "args": {"q":"", "tab":"", "kind":""}, "desc": "find widgets by text substring"},
            {"cmd": "ui.select_tab", "args": {"tab":"操作|监控|..."} , "desc": "switch notebook tab"},
            {"cmd": "ui.invoke", "args": {"path":".!frame...."} , "desc": "invoke any button by tk path"},
            {"cmd": "ui.invoke_by_text", "args": {"text":"开始监控", "tab":"监控", "contains":False}, "desc": "invoke a button by visible text (auto resolve)"},
            {"cmd": "ui.set", "args": {"path":"", "value":""}, "desc": "set widget value (Entry/Combobox/Text/Checkbutton)"},
            {"cmd": "ui.get", "args": {"path":""}, "desc": "get widget value"},
            {"cmd": "log.tail", "args": {"n":200}, "desc": "read last N log lines from UI log box"},
            {"cmd": "app.quit", "args": {}, "desc": "close XDZHGL app"},
        ]

    def _loop(self) -> None:
        # poll-based (works with SMB shares as well)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                # keep alive
                pass
            time.sleep(0.25)

    def _poll_once(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        files = sorted([p for p in self.inbox.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime)
        for p in files:
            name = p.name
            if name.endswith(".tmp") or name.startswith("~") or name.startswith("."):
                continue
            if not name.lower().endswith(".json"):
                continue
            # basic "file complete" check: size stable across 2 reads
            try:
                s1 = p.stat().st_size
                t1 = p.stat().st_mtime
                time.sleep(0.03)
                s2 = p.stat().st_size
                t2 = p.stat().st_mtime
                if s1 != s2 or t1 != t2:
                    continue
            except Exception:
                continue

            self._process_file(p)

    def _process_file(self, p: Path) -> None:
        cmd_id = ""
        try:
            data = _safe_read_json(p)
            cmd_id = str(data.get("id","")).strip() or p.stem
            cmd = str(data.get("cmd","")).strip()
            args = data.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args must be object/dict")
            if not cmd:
                raise ValueError("cmd required")

            if cmd_id in self._seen_ids:
                # duplicate, archive
                self._archive(p, ok=True, reason="duplicate")
                return

            # run handler on UI thread
            res = self._call_ui(lambda: self._handle(cmd, args), timeout=float(data.get("timeout", 180) or 180))

            self._seen_ids.add(cmd_id)
            self._write_response(cmd_id, ok=True, cmd=cmd, data=res)
            self._archive(p, ok=True)

        except Exception as e:
            err = traceback.format_exc()
            self._write_response(cmd_id or p.stem, ok=False, cmd=str(data.get("cmd","") if isinstance(locals().get("data",None), dict) else ""), error=err)
            self._archive(p, ok=False)

    def _write_response(self, cmd_id: str, ok: bool, cmd: str, data: Any = None, error: str = "") -> None:
        resp = {
            "id": cmd_id,
            "ok": bool(ok),
            "cmd": cmd,
            "ts": _now_ts(),
        }
        if ok:
            resp["data"] = data
        else:
            resp["error"] = error or "unknown error"
        outp = self.out / f"res_{cmd_id}.json"
        try:
            _atomic_write_json(outp, resp)
        except Exception:
            # best-effort fallback
            try:
                (self.out / f"res_{cmd_id}.txt").write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _archive(self, p: Path, ok: bool, reason: str = "") -> None:
        try:
            target_dir = self.done if ok else self.bad
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / p.name
            # avoid overwrite
            if target.exists():
                target = target_dir / f"{p.stem}_{int(_now_ts()*1000)}{p.suffix}"
            p.replace(target)
        except Exception:
            pass
