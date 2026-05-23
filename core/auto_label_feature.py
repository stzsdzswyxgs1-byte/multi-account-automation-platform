"""
自动列印面单 + 宅配查询码填写

触发流程：
  采购出货完成 -> register_task() 注册任务
  -> SYB发货监控检测到目标状态 -> on_shipped() 触发执行

v2 (HTTP-first): 纯 HTTP API 出货，不再需要 Playwright 点按钮
  店配：HTTP 执行出货 -> 获取面单URL -> 短暂浏览器下载PDF -> 上传SYB
  宅配：HTTP 执行出货(含查询码) -> 完成
"""
from __future__ import annotations

import json, os, re, queue, threading, time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
TASKS_FILE = ROOT_DIR / "output" / "label_tasks.json"
DEBUG_DIR = ROOT_DIR / "output" / "label_debug"

_QUERY_CODE_COL_TEXTS = ("查詢碼", "查询码")


def _today_str() -> str:
    return date.today().strftime("%Y%m%d")


def _get_browser_path(app) -> Optional[str]:
    try:
        return (app.var_browser.get() or "").strip() or None
    except Exception:
        return str(getattr(app, "settings", {}).get("browser_path", "") or "") or None


# --------------- 任务数据 ---------------

@dataclass
class LabelTask:
    order_no: str
    profile_id: str
    account_name: str
    channel: str
    status: str = "pending"
    error: str = ""
    created_at: str = ""
    finished_at: str = ""
    pdf_path: str = ""
    query_code: str = ""

    def is_home(self) -> bool:
        return self.channel in ("黑貓", "黑猫")


def _load_tasks() -> Dict[str, LabelTask]:
    if not TASKS_FILE.exists():
        return {}
    try:
        raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return {k: LabelTask(**{f: v.get(f, "") for f in LabelTask.__dataclass_fields__})
                for k, v in raw.items()}
    except Exception:
        return {}


def _save_tasks(tasks: Dict[str, LabelTask],
                log_fn: Optional[Callable[[str], None]] = None) -> None:
    """保存 tasks 到 JSON。
    诊断:写入后立即检查文件 mtime/size,若没变化(被外部还原/同步)就告警。
    """
    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(f"[AUTO-LABEL] {msg}")
            except Exception:
                pass
    try:
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _payload = json.dumps({k: asdict(v) for k, v in tasks.items()},
                              ensure_ascii=False, indent=2).encode("utf-8")
        # 用 write_bytes 避免 Windows 上 write_text 的 \n→\r\n 转换破坏 size 校对
        TASKS_FILE.write_bytes(_payload)
        # 写入后立即检查持久化效果(诊断 mtime 不更新的问题)
        try:
            _stat = TASKS_FILE.stat()
            _mtime_age = time.time() - _stat.st_mtime
            if _stat.st_size != len(_payload) or _mtime_age > 5:
                _log(f"_save_tasks 异常: 写入后 size={_stat.st_size}(预期 {len(_payload)}) "
                     f"mtime_age={_mtime_age:.1f}s — 文件可能被外部进程还原")
        except Exception as _e_stat:
            _log(f"_save_tasks stat 失败: {_e_stat}")
    except Exception as e:
        _log(f"_save_tasks 写入失败: {type(e).__name__}: {e}")
        # 不再抛 — 让调用者继续(in-memory 状态仍正确)


# --------------- 核心 Worker ---------------

class AutoLabelWorker:

    def __init__(self, app: Any, log_fn: Callable[[str], None]):
        self.app = app
        self._log_fn = log_fn
        self.enabled_store = True
        self.enabled_home = True
        self.debug = False
        self.pdf_root = str(ROOT_DIR / "output" / "面单")
        self.trigger_level = "已发货"  # "已揽收"/"已发货"/"转运中"
        self._tasks: Dict[str, LabelTask] = _load_tasks() or {}
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(f"[AUTO-LABEL] {msg}")
        except Exception:
            print(f"[AUTO-LABEL] {msg}")

    def _notify_tg(self, text: str) -> None:
        """通过运营 TG Bot 发送出货通知（仅通知当前使用者）。"""
        try:
            ops_bot = getattr(self.app, "_ops_tg_bot", None)
            if ops_bot:
                _cid = str(self.app.settings.get("tg_chat_id", "")).strip()
                if _cid:
                    ops_bot.send_to(_cid, text)
        except Exception:
            pass

    def _dbg_sync(self, page: Any, step: str) -> None:
        if not self.debug:
            return
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%H%M%S")
            p = DEBUG_DIR / f"{ts}_{step}.png"
            page.screenshot(path=str(p), full_page=True)
            self._log(f"[DBG] {step} -> {p.name}")
        except Exception as e:
            self._log(f"[DBG] {step} -> 截图失败: {e}")

    def register_task(self, order_no: str, profile_id: str,
                      account_name: str, channel: str) -> None:
        with self._lock:
            if order_no in self._tasks:
                if self._tasks[order_no].status in ("done", "processing"):
                    return
            is_home = channel in ("黑貓", "黑猫")
            if is_home and not self.enabled_home:
                return
            if not is_home and not self.enabled_store:
                return
            self._tasks[order_no] = LabelTask(
                order_no=order_no, profile_id=profile_id,
                account_name=account_name, channel=channel,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            _save_tasks(self._tasks, self._log_fn)
            self._log(f"已注册: {order_no} ({account_name}) 渠道={channel}")

    def on_shipped(self, order_no: str, status_text: str) -> None:
        # 判断当前货物状态（SYB HTTP 返回的状态可能是「已揽收」「已打包」等，不仅仅是「已发货」）
        _picked_keywords = ("已揽收", "已攬收", "已打包", "待揽收")
        is_picked = any(kw in status_text for kw in _picked_keywords)
        _shipped_keywords = ("已发货", "已發貨", "已出仓", "已寄出")
        is_shipped = any(kw in status_text for kw in _shipped_keywords)
        is_transit = "转运" in status_text or "轉運" in status_text or "配送" in status_text

        # 级联单选：选择的是触发阈值
        # 已揽收 → 已揽收/已发货/转运中 都触发
        # 已发货 → 已发货/转运中 触发
        # 转运中 → 只有转运中 触发
        lv = self.trigger_level
        should = False
        if lv == "已揽收" and (is_picked or is_shipped or is_transit):
            should = True
        elif lv == "已发货" and (is_shipped or is_transit):
            should = True
        elif lv == "转运中" and is_transit:
            should = True

        self._log(f"on_shipped: {order_no} status='{status_text}' trigger_level='{lv}' should={should}")

        if not should:
            return
        with self._lock:
            task = self._tasks.get(order_no)
            if not task:
                self._log(f"on_shipped 跳过: {order_no} task=无")
                return
            if task.status in ("processing", "done"):
                self._log(f"on_shipped 跳过: {order_no} task=有 status={task.status}")
                return
            if task.status == "failed":
                # 允许 failed 重试 (例如 SYB-MON 重新检测/手动重绑定后)
                _prev_err = (task.error or "")[:80]
                self._log(f"重试失败任务: {order_no} (上次错误: {_prev_err})")
                task.status = "pending"
                task.error = ""
                _save_tasks(self._tasks, self._log_fn)
            # 此时 status 为 pending → 入队
            self._log(f"触发: {order_no} (状态={status_text})")
            self._queue.put(order_no)
        self._ensure_worker()

    def get_stats(self) -> Dict[str, int]:
        with self._lock:
            s = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
            for t in self._tasks.values():
                s[t.status] = s.get(t.status, 0) + 1
            return s

    # --- Worker ---

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="AutoLabelWorker", daemon=True)
        self._thread.start()

    def _worker_loop(self) -> None:
        self._log("Worker 启动")
        while not self._stop.is_set():
            try:
                order_no = self._queue.get(timeout=5)
            except queue.Empty:
                with self._lock:
                    has = any(t.status == "pending" for t in self._tasks.values())
                if not has:
                    break
                continue
            with self._lock:
                task = self._tasks.get(order_no)
                if not task or task.status != "pending":
                    continue
                task.status = "processing"
                _save_tasks(self._tasks, self._log_fn)
            try:
                if task.is_home():
                    self._do_home(task)
                else:
                    self._do_store(task)
            except Exception as e:
                import traceback as _tb_mod
                _tb_str = _tb_mod.format_exc()
                self._log(f"异常: {order_no} -> {e}")
                # 完整 traceback 写到日志,下次失败能定位到具体步骤(ship_order_http/PDF下载/SYB上传 哪一步)
                for _line in _tb_str.rstrip().split("\n"):
                    self._log(f"  TB: {_line}")
                with self._lock:
                    task.status = "failed"
                    task.error = str(e)[:200]
                    task.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    _save_tasks(self._tasks, self._log_fn)
                self._notify_tg(
                    f"⚠️【自动出货失败】\n"
                    f"账号：{task.account_name}\n"
                    f"订单：{order_no}\n"
                    f"错误：{str(e)[:100]}")
        self._log("Worker 退出")

    # --- 店配 (HTTP-first) ---

    def _do_store(self, task: LabelTask) -> None:
        """店配出货: HTTP API 执行出货 + 浏览器下载面单 PDF。

        不需要 profile lock (HTTP 用 cookie cache, 面单用临时 profile)。
        """
        import asyncio
        from core.ship_http_ops import ship_order_http, download_store_label_pdf
        # v6.0.68 ★:task.order_no 可能是帶 +N 後綴的 SYB code(若曾撞「已存在」)
        # Yahoo API 需要純 14 位數,SYB 端跟 PDF 命名要保留帶後綴版本
        from core.syb_http_ops import strip_dup_suffix

        order_no = task.order_no              # 帶後綴(SYB code,用於 PDF 命名)
        yahoo_code = strip_dup_suffix(order_no)  # 砍尾(用於 Yahoo API)
        profile_dir = ROOT_DIR / "profiles" / task.profile_id
        chrome = _get_browser_path(self.app)

        # 1. HTTP 出货 — 用純 Yahoo 號叫 Yahoo API
        if yahoo_code != order_no:
            self._log(f"店配HTTP出货: SYB code={order_no}, Yahoo code={yahoo_code}")
        else:
            self._log(f"店配HTTP出货: {order_no}")
        result = ship_order_http(
            profile_dir, yahoo_code,
            channel=task.channel,
            chrome_path=chrome or "",
            log=self._log,
        )

        if not result.success:
            raise RuntimeError(f"HTTP出货失败: {result.error}")

        self._log(f"出货成功: {order_no} shippingId={result.shipping_id}")

        # 2. 下载面单 PDF — 檔名用 SYB code(帶後綴),供後續上傳對應 SYB 紀錄
        pdf_path = None
        if result.print_delivery_url:
            pdf_dir = Path(self.pdf_root) / _today_str()
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"{order_no}.pdf"

            self._log(f"下载面单: {order_no}")
            ok = asyncio.run(download_store_label_pdf(
                result.print_delivery_url,
                pdf_path,
                profile_dir,
                chrome or "",
                log=self._log,
            ))
            if not ok:
                self._log(f"面单PDF下载失败: {order_no} (出货已成功)")
                pdf_path = None

        with self._lock:
            task.status = "done"
            task.pdf_path = str(pdf_path) if pdf_path else ""
            task.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_tasks(self._tasks, self._log_fn)
        self._log(f"店配完成: {order_no} -> {pdf_path or '无PDF'}")
        self._notify_tg(
            f"📦【自动出货-店配】\n"
            f"账号：{task.account_name}\n"
            f"订单：{order_no}\n"
            f"物流编号：{result.shipping_id}\n"
            f"面单：{'已下载' if pdf_path else '无'}")

        if pdf_path:
            self._upload_pdf_to_syb(pdf_path)

    # --- 宅配 (HTTP-first) ---

    def _do_home(self, task: LabelTask) -> None:
        """宅配出货: SYB 获取查询码 + HTTP API 执行出货。

        不需要 profile lock (纯 HTTP)。
        """
        from core.ship_http_ops import ship_order_http
        # v6.0.68 ★:task.order_no 可能帶 +N 後綴
        from core.syb_http_ops import strip_dup_suffix

        order_no = task.order_no               # 帶後綴(SYB 端)
        yahoo_code = strip_dup_suffix(order_no)  # 純 Yahoo 號(Yahoo API 端)

        # 1. 获取查询码 — 用 SYB code(帶後綴)直接查;get_query_code 內部也支援純號自動解析
        qc = self._syb_get_query_code(order_no)
        if not qc:
            raise RuntimeError(f"SYB 未找到查询码: {order_no}")
        self._log(f"查询码: {order_no} -> {qc}")
        task.query_code = qc

        # 2. HTTP 出货 (含查询码) — Yahoo 端需要純 14 位
        profile_dir = ROOT_DIR / "profiles" / task.profile_id
        chrome = _get_browser_path(self.app)

        result = ship_order_http(
            profile_dir, yahoo_code,
            tracking_code=qc,
            channel=task.channel,
            chrome_path=chrome or "",
            log=self._log,
        )

        if not result.success:
            raise RuntimeError(f"HTTP出货失败: {result.error}")

        with self._lock:
            task.status = "done"
            task.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_tasks(self._tasks, self._log_fn)
        self._log(f"宅配完成: {order_no} 查询码={qc}")
        self._notify_tg(
            f"📦【自动出货-宅配】\n"
            f"账号：{task.account_name}\n"
            f"订单：{order_no}\n"
            f"查询码：{qc}")

    # --- SYB 查询码获取 ---

    def _syb_get_query_code(self, order_no: str) -> str:
        """获取查询码。HTTP-first，失败回退 Playwright。"""
        self._log(f"SYB: 获取查询码 {order_no}")

        # --- HTTP-first (自动登录) ---
        try:
            from core.syb_http_ops import ensure_stoken, get_query_code
            stoken = ensure_stoken(log=self._log)
            qc = get_query_code(stoken, order_no, log=self._log)
            if qc:
                return qc
            self._log(f"SYB HTTP 未找到查询码，回退 Playwright")
        except Exception as e:
            self._log(f"SYB HTTP 查询码异常({e})，回退 Playwright")

        # --- Playwright 回退 ---
        syb_tab = getattr(self.app, "syb_upload_tab", None)
        if not syb_tab or not getattr(syb_tab, "_agent", None):
            raise RuntimeError("SYB WebAgent 未初始化")

        result = {"code": "", "error": ""}
        done_event = threading.Event()

        def task(page):
            try:
                from core.shunyunbao_upload_feature import SYB_STOCK_URL
                page.goto(SYB_STOCK_URL, wait_until="domcontentloaded", timeout=45_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                page.wait_for_selector("div.ctrl-left", state="visible", timeout=30_000)

                # 展开高级搜索
                try:
                    lab = page.locator("label", has_text="订单编号").first
                    if not lab.is_visible():
                        raise Exception("not visible")
                except Exception:
                    root = page.locator("div.ctrl-left").first
                    spans = root.locator("span.txt")
                    for i in range(min(spans.count(), 50)):
                        s = spans.nth(i)
                        if s.is_visible() and s.inner_text().strip() == "高级搜索":
                            s.click(timeout=3000)
                            break
                    page.locator("label", has_text="订单编号").first.wait_for(
                        state="visible", timeout=10_000)

                # 填订单号
                lab = page.locator("label", has_text="订单编号").first
                item = lab.locator(
                    "xpath=ancestor::div[contains(@class,'el-form-item')][1]").first
                inp = item.locator("textarea, input").first
                inp.wait_for(state="visible", timeout=3000)
                inp.click(timeout=3000)
                inp.fill(order_no)

                # 搜索
                page.locator("button").filter(has_text="搜索").first.click(timeout=5000)

                # 等结果行
                deadline = time.time() + 20
                row = None
                while time.time() < deadline:
                    r1 = page.locator(
                        ".vxe-table--body-wrapper .vxe-body--row"
                    ).filter(has_text=order_no).first
                    r2 = page.locator(
                        ".el-table__body-wrapper tbody tr"
                    ).filter(has_text=order_no).first
                    if r1.count() > 0:
                        row = r1
                        break
                    if r2.count() > 0:
                        row = r2
                        break
                    page.wait_for_timeout(300)

                if not row:
                    result["error"] = f"SYB未找到订单: {order_no}"
                    return

                self._dbg_sync(page, f"{order_no}_syb_row")

                qc = self._extract_query_code_from_row(page, row)
                result["code"] = qc

            except Exception as e:
                result["error"] = str(e)[:200]
            finally:
                done_event.set()

        syb_tab._agent.submit(task, f"获取查询码({order_no})")
        done_event.wait(timeout=60)
        if result["error"]:
            self._log(f"SYB查询码失败: {result['error']}")
            return ""
        return result["code"]

    def _extract_query_code_from_row(self, page, row) -> str:
        """从SYB结果行提取查询码。"""
        for col_text in _QUERY_CODE_COL_TEXTS:
            headers = page.locator(
                ".vxe-header--column, thead th")
            for i in range(headers.count()):
                try:
                    ht = headers.nth(i).inner_text().strip()
                    if col_text in ht:
                        cells = row.locator(
                            ".vxe-body--column, td")
                        if cells.count() > i:
                            val = cells.nth(i).inner_text().strip()
                            if val and val != "-":
                                return val
                except Exception:
                    continue
        txt = row.inner_text()
        m = re.search(r'P\d{8,}', txt)
        if m:
            return m.group(0)
        return ""

    # --- PDF上传到SYB ---

    def _upload_pdf_to_syb(self, pdf_path) -> None:
        """上传面单PDF到SYB（纯HTTP）。"""
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            self._log(f"PDF不存在，跳过上传: {pdf_path}")
            return

        # --- HTTP 上传(最多重试 3 次,401 时自动清 cache 重新登录)---
        from core.syb_http_ops import ensure_stoken, upload_label_pdf, SYBAuthError, _TOKEN_CACHE
        last_err = ""
        for attempt in range(1, 4):
            try:
                stoken = ensure_stoken(log=self._log)
                r = upload_label_pdf(stoken, pdf_path, log=self._log)
                if r.get("uploaded"):
                    if r.get("status"):
                        self._log(f"面单HTTP上传成功: {pdf_path.name}")
                    else:
                        # v6.0.49: 解析異常時通知 user
                        _msg = r.get('msg', '') or '未知'
                        self._log(f"面单HTTP已上传(解析异常): {pdf_path.name} - {_msg}")
                        self._notify_tg(
                            f"⚠️【面單解析異常】\n"
                            f"檔名:{pdf_path.name}\n"
                            f"原因:{_msg}\n"
                            f"請手動到順雲寶確認")
                    return
                else:
                    last_err = r.get('msg', '未知错误')
                    self._log(f"面单HTTP上传失败(第{attempt}次): {last_err}")
            except SYBAuthError as e:
                # v6.0.62: cache 内 stoken 被 server 拒 → 清 cache → 下次 ensure_stoken 自动 auto_login
                last_err = str(e)
                try: _TOKEN_CACHE.unlink(missing_ok=True)
                except Exception: pass
                self._log(f"面单HTTP上传第{attempt}次:stoken 被 server 拒,清缓存重新登录后重试...")
            except Exception as e:
                last_err = str(e)
                self._log(f"面单HTTP上传异常(第{attempt}次): {last_err}")
            if attempt < 3:
                import time as _t
                _t.sleep(5)

        # v6.0.61: 删除 Playwright 回退 — 与「上传出货资料」一致策略「不允许后退」
        # 之前 HTTP 3 次失败 → 自动跑 Playwright 上传(浏览器打开顺云宝、点按钮、填表),
        # 但 Playwright 路径有 2 个固有问题:
        #   1) WebAgent 抓到的 stoken cookie 会污染 HTTP cache(已在 v6.0.61 A 改动里删掉了同步逻辑)
        #   2) task.fn 即使失败也不抛,日志「完成」误导用户(C 改动修了但不彻底)
        # 现在 HTTP 3 次失败 → 直接通知 TG,主管手动处理。失败结果清晰不被遮蔽。
        self._log(f"面单上传失败(3 次 HTTP 均失败): {pdf_path.name} - {last_err}")
        self._notify_tg(
            f"⚠️【面單自動上傳失敗】\n"
            f"檔名:{pdf_path.name}\n"
            f"原因:{last_err[:120]}\n"
            f"請手動到順雲寶後台檢查/補傳"
        )
