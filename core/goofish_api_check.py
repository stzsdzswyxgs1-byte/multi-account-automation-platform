"""
闲鱼 HTTP API 检测（不需要浏览器/代理/中转服务器）

使用 mtop.taobao.idle.awesome.detail.unit API（APP 端接口）直接检测商品状态。
此接口未被 RGV587 异常码覆盖（仅 mtop.taobao.idle.pc.detail 被限流）。
需要闲鱼 cookies（从检测专用 Playwright JSON 加载）。

状态映射：
  itemStatusStr="在线"  → 在售
  itemStatusStr="卖掉了" → 已售出
  itemStatusStr="已下架" → 已下架
  API 异常 FAIL_BIZ_ITEM_DEL_NOT_FOUND → 已删除
"""
from __future__ import annotations

import hashlib
import json
import time
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

LogFn = Callable[[str], None]

BASE_DIR = Path(__file__).resolve().parent.parent


APP_KEY = "<XIANYU_APP_KEY_REDACTED>"
API_BASE = "https://h5api.m.goofish.com/h5"
DETAIL_API = "mtop.taobao.idle.awesome.detail.unit"
TOKEN_REFRESH_API = "mtop.taobao.idle.item.web.recommend.list"
IMPERSONATE = "chrome142"

_STATUS_RE = re.compile(r'"itemStatusStr"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]{0,100})"')

# 拍卖检测正则（命中任一即视为拍卖商品 — 我们卖 Yahoo 一口价不要拍卖货源）
# 参考：闲鱼采集0420 工具的拍卖识别逻辑
_AUCTION_RES = (
    re.compile(r'"itemType"\s*:\s*"detailAuction"'),       # itemType=detailAuction
    re.compile(r'"auctionDO"\s*:\s*\{[^}]*"auctionId"'),   # auctionDO.auctionId 存在
    re.compile(r'"auctionType"\s*:\s*"(?!b")[^"]+"'),       # auctionType != "b" (b=固定价)
)


def _is_auction_text(text: str) -> bool:
    """从响应文本判断是否拍卖商品（纯字符串匹配，避免解析嵌套 JSON）"""
    if not text:
        return False
    for pat in _AUCTION_RES:
        if pat.search(text):
            return True
    return False


def _build_detail_payload(item_id: str) -> str:
    """构造 awesome.detail.unit 接口所需的完整 data 串（10 字段）"""
    return json.dumps({
        "commerceAdPlanId": "",
        "extra": '{"labelIds":"36,35,9,12"}',
        "fishAdCode": "440902",
        "flowVersion": "6.0",
        "gps": "0,0",
        "isOld": False,
        "itemId": str(item_id),
        "latitude": "",
        "longitude": "",
        "needSimpleDetail": False,
    }, separators=(",", ":"))


def _extract_status_from_text(text: str) -> Optional[Dict]:
    """从响应文本中正则提取 itemStatusStr 和 title（兼容多种数据结构）。
    若是拍卖商品，覆盖 status 为「拍卖」（视为非在售，会被 D1 清理）。
    """
    m = _STATUS_RE.search(text or "")
    if not m:
        return None
    status = m.group(1)
    title = ""
    tm = _TITLE_RE.search(text)
    if tm:
        title = tm.group(1)[:50]
    # 拍卖判定优先：即使 itemStatusStr=在线，是拍卖也覆盖为「拍卖」
    if _is_auction_text(text):
        status = "拍卖"
    return {"status": status, "title": title}

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Origin": "https://www.goofish.com",
    "Content-Type": "application/x-www-form-urlencoded",
}


def _headers_for_item(item_id: str) -> Dict[str, str]:
    """每个 item 用对应商品页的 Referer，模拟真实浏览来源"""
    h = dict(_BASE_HEADERS)
    h["Referer"] = f"https://www.goofish.com/item?id={item_id}"
    return h


# token 刷新接口用首页 Referer
HEADERS = {**_BASE_HEADERS, "Referer": "https://www.goofish.com/"}


def _sign(token: str, t: str, data_str: str) -> str:
    return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


class GoofishApiChecker:
    """闲鱼 API 检测器（共享 session + token 自动刷新）"""

    def __init__(self, log: LogFn = None):
        self._log = log or (lambda m: None)
        self._session = None
        self._token = ""
        self._m_h5_tk = ""
        self._loaded = False
        # 错误统计（用于诊断个别用户的失败原因）
        self._err_stats = {}
        self._err_lock = None  # lazy init
        self._first_rgv587_logged = False  # 只记录第一次 RGV587 详情
        self._first_detail_logged = False  # 只记录第一次 detail 请求详情
        self._detail_count = 0  # 记录前 3 次 detail 用于对比 token 变化
        # RGV587 限流冷却（触发后所有线程暂停一段时间，让限流自然解除）
        import threading as _th
        self._rgv587_cooldown_until = 0  # 时间戳，所有 check_item 调用前检查
        self._cooldown_lock = _th.Lock()

    def load(self) -> bool:
        """加载闲鱼 cookies — 唯一来源：检测专用 Playwright JSON。

        路径：profiles/check_goofish/goofish_cookies.json
        通过【闲鱼检测专用登录】按钮生成。包含 session cookie，永久持久化。
        token 过期会自动 HTTP 刷新。
        """
        try:
            from core.goofish_login_playwright import (
                COOKIE_JSON_PATH as _CHECK_JSON,
                load_cookies_from_json as _load_check_json,
                refresh_token_http as _refresh_check_token,
                is_logged_in as _check_logged,
                get_m_h5_tk as _get_check_tk,
            )
        except Exception as _e:
            self._log(f"[GF-API] 加载检测模块失败: {_e}")
            return False

        if not _CHECK_JSON.exists():
            self._log("[GF-API] 未找到 goofish_cookies.json")
            self._log("[GF-API] 请点【闲鱼检测专用登录】按钮扫码登录")
            return False

        _raw = _load_check_json()
        if not _raw:
            self._log("[GF-API] goofish_cookies.json 为空，请重新登录")
            return False

        if not _check_logged(_raw):
            self._log("[GF-API] 登录态已失效（缺 unb），请重新点【闲鱼检测专用登录】")
            return False

        self._log(f"[GF-API] 使用检测专用 cookie: {_CHECK_JSON.name}")
        return self._load_from_check_json(_raw, _refresh_check_token, _get_check_tk)

    def _load_from_check_json(self, raw_cookies, refresh_fn, get_tk_fn) -> bool:
        """从检测专用 Playwright JSON 加载 cookie 进 curl_cffi session。

        流程（参考 0329）：
        1) 加载 JSON cookies 到 curl_cffi session
        2) 检查 token 时效（<2h 直接用，>2h HTTP 刷新）
        3) 刷新成功就写回 JSON，下次检测直接用
        """
        from curl_cffi.requests import Session

        self._session = Session(impersonate=IMPERSONATE)

        _PUNISH_NAMES = {"x5secdata", "x5sectag", "tb_xs_id", "bxuuid"}
        _set_count = 0
        _skipped_punish = 0
        _seen = set()
        _flat = {}
        for c in raw_cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            domain = c.get("domain", ".goofish.com")
            path = c.get("path", "/")
            if not name:
                continue
            if "_____tmd_____" in path or "punish" in path.lower():
                _skipped_punish += 1
                continue
            if name in _PUNISH_NAMES:
                _skipped_punish += 1
                continue
            key = (name, domain)
            if key in _seen:
                continue
            _seen.add(key)
            try:
                self._session.cookies.set(
                    name, value, domain=domain, path=path,
                    secure=c.get("secure", False),
                )
                _set_count += 1
                _flat[name] = value
            except Exception:
                try:
                    self._session.cookies.set(name, value)
                    _set_count += 1
                    _flat[name] = value
                except Exception:
                    pass

        self._m_h5_tk = get_tk_fn(raw_cookies)
        self._token = self._m_h5_tk.split("_")[0] if self._m_h5_tk and "_" in self._m_h5_tk else ""

        _unb = _flat.get("unb", "")
        _key_names = ["unb", "cookie2", "_tb_token_", "_m_h5_tk", "sgcookie", "tracknick", "havana_lgc2_77", "cna"]
        _diag = [f"{n}={'有' if _flat.get(n) else '无'}" for n in _key_names]
        self._log(f"[GF-API] [JSON] 共{_set_count}条 cookie: " + ", ".join(_diag))
        if _skipped_punish > 0:
            self._log(f"[GF-API] [JSON] ⚠ 跳过 {_skipped_punish} 条 punish 标记 cookie")

        if not _unb:
            self._log("[GF-API] [JSON] 缺少 unb，请重新点「闲鱼检测专用登录」")
            return False

        # ── token 时效检查（<2h 跳过刷新，节省时间避免限流）──
        _need_refresh = True
        if self._m_h5_tk and "_" in self._m_h5_tk:
            try:
                _tk_time_ms = int(self._m_h5_tk.split("_")[1])
                _age_ms = int(time.time() * 1000) - _tk_time_ms
                if 0 < _age_ms < 7200_000:  # 2 小时内
                    _need_refresh = False
                    self._log(f"[GF-API] [JSON] token 仍新鲜（{_age_ms//60000}分钟），跳过刷新")
            except (ValueError, IndexError):
                pass

        if _need_refresh:
            self._log("[GF-API] [JSON] token 需要刷新，HTTP 调用牺牲接口...")
            if refresh_fn(self._log):
                # 刷新成功 → JSON 已写回，重读新 token
                from core.goofish_login_playwright import load_cookies_from_json as _reload
                _new = _reload()
                _new_tk = get_tk_fn(_new)
                if _new_tk:
                    self._m_h5_tk = _new_tk
                    self._token = _new_tk.split("_")[0]
                    self._session.cookies.set("_m_h5_tk", _new_tk, domain=".goofish.com")
                    # 同步 _m_h5_tk_enc
                    for c in _new:
                        if c.get("name") == "_m_h5_tk_enc" and ".goofish.com" in c.get("domain", ""):
                            self._session.cookies.set("_m_h5_tk_enc", c.get("value", ""), domain=".goofish.com")
                            break
            else:
                self._log("[GF-API] [JSON] HTTP token 刷新失败，请重新点「闲鱼检测专用登录」")
                return False

        if not self._token:
            self._log("[GF-API] [JSON] token 为空，请重新点「闲鱼检测专用登录」")
            return False

        self._loaded = True
        self._log(f"[GF-API] [JSON] ✓ 加载成功 unb={_unb[:8]}... token={self._token[:12]}...")
        return True

    def _try_refresh_token_with_diag(self) -> str:
        """刷新 token，返回 OK / FAIL"""
        if not self._session:
            return "FAIL"

        t = _ts_ms()
        payload = json.dumps({"itemId": "0", "pageSize": 1, "pageNum": 1}, separators=(",", ":"))
        # 优先用 cookie 里已有的 token 签名（避免空 token 触发 ILLEGAL_ACCESS）
        # 如果没有再用空字符串
        _sign_token = self._token or ""
        sign = _sign(_sign_token, t, payload)

        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
            "api": TOKEN_REFRESH_API,
        }

        try:
            r = self._session.post(
                f"{API_BASE}/{TOKEN_REFRESH_API}/1.0/",
                params=params, data={"data": payload},
                headers=HEADERS, timeout=15,
            )
            try:
                _result = r.json()
                _ret = _result.get("ret", [])
                _ret_str = " ".join(str(x) for x in _ret)[:120]
                if _ret and "SUCCESS" not in _ret_str:
                    self._log(f"[GF-API] [诊断] token 刷新返回: {_ret_str}")
            except Exception:
                pass

            # 即使返回 ILLEGAL_ACCESS/TOKEN_EXOIRED，只要 _m_h5_tk 被服务端更新就视为成功
            # （我们本地实测这样能 work，cookie 是被刷新的）
            new_tk = self._session.cookies.get("_m_h5_tk")
            if new_tk and "_" in new_tk:
                self._m_h5_tk = new_tk
                self._token = new_tk.split("_")[0]
                self._session.cookies.set("_m_h5_tk", new_tk, domain=".goofish.com")
                new_enc = self._session.cookies.get("_m_h5_tk_enc")
                if new_enc:
                    self._session.cookies.set("_m_h5_tk_enc", new_enc, domain=".goofish.com")
                return "OK"
        except Exception as _e:
            self._log(f"[GF-API] [诊断] token 刷新异常: {_e}")
        return "FAIL"

    def _refresh_token(self) -> bool:
        """通过牺牲接口刷新 _m_h5_tk"""
        if not self._session:
            return False

        t = _ts_ms()
        payload = json.dumps({"itemId": "0", "pageSize": 1, "pageNum": 1}, separators=(",", ":"))
        # 优先用现有 token 签名（避免空 token 触发 ILLEGAL_ACCESS）
        sign = _sign(self._token or "", t, payload)

        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
            "api": TOKEN_REFRESH_API,
        }

        try:
            r = self._session.post(
                f"{API_BASE}/{TOKEN_REFRESH_API}/1.0/",
                params=params, data={"data": payload},
                headers=HEADERS, timeout=15,
            )
            # 诊断：服务端返回的 ret，看看是不是登录态问题
            try:
                _result = r.json()
                _ret = _result.get("ret", [])
                _ret_str = " ".join(str(x) for x in _ret)[:120]
                if _ret and "SUCCESS" not in _ret_str:
                    self._log(f"[GF-API] [诊断] token 刷新返回: {_ret_str}")
            except Exception:
                pass
            new_tk = self._session.cookies.get("_m_h5_tk")
            if new_tk and "_" in new_tk:
                self._m_h5_tk = new_tk
                self._token = new_tk.split("_")[0]
                self._session.cookies.set("_m_h5_tk", new_tk, domain=".goofish.com")
                new_enc = self._session.cookies.get("_m_h5_tk_enc")
                if new_enc:
                    self._session.cookies.set("_m_h5_tk_enc", new_enc, domain=".goofish.com")
                return True
        except Exception as _e:
            self._log(f"[GF-API] [诊断] token 刷新异常: {_e}")
        return False

    def check_item(self, item_id: str) -> Optional[Dict]:
        """检测单个商品状态。

        返回:
          {"status": "在线|卖掉了|已下架|已删除", "title": "..."}
          None: 检测失败（网络错误等）
        """
        if not self._loaded:
            return None

        t = _ts_ms()
        data_str = _build_detail_payload(item_id)
        # 关键：每次签名都用 jar 里最新的 _m_h5_tk（线程安全）
        # 服务端每次响应都会在 set-cookie 里返回新 _m_h5_tk，curl_cffi 自动更新 jar
        # 必须用最新的 token 签名，否则会被服务端认定为「重放攻击」
        if self._err_lock is None:
            import threading
            self._err_lock = threading.Lock()
        with self._err_lock:
            try:
                _latest_tk = self._session.cookies.get("_m_h5_tk") or ""
                if _latest_tk and "_" in _latest_tk:
                    _latest_token = _latest_tk.split("_")[0]
                    if _latest_token:
                        self._token = _latest_token
            except Exception:
                pass
            _curr_token = self._token
        sign = _sign(_curr_token, t, data_str)

        # 记录前 3 次 detail 请求的 token，用于对比是否真的更新
        self._detail_count += 1
        _seq = self._detail_count
        if _seq <= 3:
            self._log(f"[GF-API] [诊断] detail #{_seq}: item_id={item_id}, token={_curr_token[:16]}, t={t}")

        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
            "api": DETAIL_API,
            # spm 跟踪参数（v5.0.83 老代码就有，BX 限流用来判断是否是从 item 页发起的合法请求）
            "spm_cnt": "a21ybx.item.0.0",
            "spm_pre": "widle.12011849.0.0",
            "sessionOption": "AutoLoginOnly",
        }

        # 第一次 detail 请求：记录完整诊断信息（用于定位个别用户失败原因）
        _is_first = not self._first_detail_logged
        if _is_first:
            self._first_detail_logged = True
            self._log("[GF-API] [诊断] === 首次 detail 请求 ===")
            self._log(f"[GF-API] [诊断] item_id={item_id}, t={t}")
            self._log(f"[GF-API] [诊断] token={self._token}")
            self._log(f"[GF-API] [诊断] sign={sign}")
            try:
                _sess_cookies = dict(self._session.cookies)
                _sess_keys = sorted(_sess_cookies.keys())
                self._log(f"[GF-API] [诊断] session 共{len(_sess_keys)}条 cookie: {','.join(_sess_keys)}")
                # 列出关键 cookie 的值前缀（用于和服务端期望对比）
                for _k in ["unb", "_m_h5_tk", "cookie2", "_tb_token_", "sgcookie", "cna"]:
                    _v = _sess_cookies.get(_k, "")
                    if _v:
                        self._log(f"[GF-API] [诊断] cookie {_k}={_v[:40]}")
            except Exception as _e:
                self._log(f"[GF-API] [诊断] 读取 session cookies 异常: {_e}")

        try:
            r = self._session.post(
                f"{API_BASE}/{DETAIL_API}/1.0/",
                params=params, data={"data": data_str},
                headers=_headers_for_item(item_id), timeout=15,
            )
            # 关键：每次响应后立刻从 jar 删除 punish 标记 cookie
            # 闲鱼 punish 时会 set-cookie: x5secdata=...，curl_cffi 自动加到 jar
            # 下次请求带上任一 punish cookie → 闲鱼立刻继续 punish → 死循环
            # 必须全部删光，漏一个都会触发
            try:
                for _bad in ("x5secdata", "x5sectag", "tb_xs_id", "bxuuid"):
                    try:
                        for _d in (".goofish.com", "goofish.com", "h5api.m.goofish.com",
                                   ".taobao.com", "taobao.com", "passport.goofish.com"):
                            try:
                                self._session.cookies.delete(_bad, domain=_d)
                            except Exception:
                                pass
                        try:
                            self._session.cookies.delete(_bad)
                        except Exception:
                            pass
                    except Exception:
                        pass
                # 额外：jar 里任何 path 含 _____tmd_____ 或 punish 的 cookie 全部删
                try:
                    _all_cookies = list(self._session.cookies.jar) if hasattr(self._session.cookies, "jar") else []
                    for _ck in _all_cookies:
                        _ck_path = getattr(_ck, "path", "") or ""
                        if "_____tmd_____" in _ck_path or "punish" in _ck_path.lower():
                            try:
                                self._session.cookies.jar.clear(
                                    domain=_ck.domain, path=_ck.path, name=_ck.name,
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
            except Exception:
                pass
            # 前 3 次都记录响应 set-cookie 看 token 是否更新
            if _seq <= 3:
                try:
                    _sc = r.headers.get("set-cookie", "") or ""
                    # 从 set-cookie 里提取新 _m_h5_tk
                    _new_tk_raw = ""
                    if "_m_h5_tk=" in _sc:
                        _idx = _sc.find("_m_h5_tk=")
                        _end = _sc.find(";", _idx)
                        _new_tk_raw = _sc[_idx+9:_end if _end > 0 else _idx+50]
                    self._log(f"[GF-API] [诊断] detail #{_seq} 响应: HTTP {r.status_code}, 新_m_h5_tk={_new_tk_raw[:30]}")
                    # 检查 jar 里是否真的更新了
                    _jar_tk = self._session.cookies.get("_m_h5_tk") or ""
                    self._log(f"[GF-API] [诊断] detail #{_seq} jar 里的 _m_h5_tk={_jar_tk[:40]}")
                    # 列出所有响应 header（看服务端返回了什么）
                    _all_headers = []
                    for _hk, _hv in r.headers.items():
                        if _hk.lower() not in ("date", "server", "content-length", "content-type", "vary", "via", "ali-swift-stat-msg", "eagleeye-traceid", "timing-allow-origin"):
                            _all_headers.append(f"{_hk}={str(_hv)[:60]}")
                    if _all_headers:
                        self._log(f"[GF-API] [诊断] detail #{_seq} 响应 headers: {' | '.join(_all_headers[:10])}")
                except Exception as _e:
                    self._log(f"[GF-API] [诊断] detail #{_seq} 响应日志异常: {_e}")
            result = r.json()
        except Exception as _e:
            if _is_first:
                self._log(f"[GF-API] [诊断] 首次 detail 请求异常: {_e}")
            self._record_err(f"EXCEPTION:{type(_e).__name__}")
            return None

        ret = result.get("ret", [])
        # 检查所有 ret 元素（RGV587 等错误可能在 ret[1] 而非 ret[0]）
        ret_str = " ".join(str(r) for r in ret) if ret else ""

        if _is_first:
            self._log(f"[GF-API] [诊断] 首次 detail ret = {ret}")

        # 成功（awesome.detail.unit 结构与 pc.detail 不同，data 里嵌套层级较深，
        # 直接用正则从原始 JSON 文本提取 itemStatusStr 最稳定）
        if "SUCCESS" in ret_str:
            try:
                _txt = r.text if hasattr(r, "text") else json.dumps(result)
            except Exception:
                _txt = json.dumps(result)
            extracted = _extract_status_from_text(_txt)
            if extracted:
                return extracted
            # 兜底：尝试老结构
            item = result.get("data", {}).get("itemDO", {}) or {}
            _status = item.get("itemStatusStr", "未知")
            # 兜底也检查拍卖
            if _is_auction_text(_txt):
                _status = "拍卖"
            return {
                "status": _status,
                "title": (item.get("title", "") or "")[:50],
            }

        # 已删除
        if "NOT_FOUND" in ret_str or "DEL" in ret_str:
            return {"status": "已删除", "title": ""}

        # 审核中（商品被平台审核，非在售状态）
        if "ITEM_CC" in ret_str:
            return {"status": "已下架", "title": ""}

        # Token 过期 → 刷新重试一次
        if "TOKEN_EXPIRED" in ret_str or "TOKEN_EXOIRED" in ret_str:
            self._record_err("TOKEN_EXPIRED")
            if self._refresh_token():
                return self._check_item_retry(item_id)
            return None

        # 限流 / 限流验证（RGV587 可能在 ret[0] 或 ret[1]）
        if "RGV587" in ret_str:
            # 尝试提取 captcha url，区分真实限流 vs 未登录验证
            _captcha_url = ""
            try:
                _data = result.get("data", {})
                if isinstance(_data, dict):
                    _captcha_url = _data.get("url", "")[:200]
            except Exception:
                pass
            # 第一次 RGV587 时记录完整诊断信息
            if not self._first_rgv587_logged:
                self._first_rgv587_logged = True
                self._log(f"[GF-API] [诊断] 首次 RGV587 触发! item_id={item_id}, 这是第 {_seq} 次 detail 请求")
                self._log(f"[GF-API] [诊断] ret = {ret}")
                if _captcha_url:
                    _short_url = _captcha_url
                    if "action=" in _captcha_url:
                        _act_idx = _captcha_url.find("action=")
                        _short_url = _captcha_url[_act_idx:_act_idx+50]
                    self._log(f"[GF-API] [诊断] captcha url 关键参数: {_short_url}")
                # 记录 RGV587 时的 token 和响应头（对比第 1 次成功时）
                try:
                    _curr_jar_tk = self._session.cookies.get("_m_h5_tk") or ""
                    self._log(f"[GF-API] [诊断] RGV587 时 jar 里的 _m_h5_tk={_curr_jar_tk[:40]}")
                    self._log(f"[GF-API] [诊断] RGV587 时 self._token={self._token[:16]}, sign 用的 token={_curr_token[:16]}")
                    # 记录响应 set-cookie
                    _rsc = r.headers.get("set-cookie", "") or ""
                    if _rsc:
                        self._log(f"[GF-API] [诊断] RGV587 响应 set-cookie 前300字: {_rsc[:300]}")
                except Exception as _e:
                    self._log(f"[GF-API] [诊断] RGV587 详情记录异常: {_e}")
            # 子分类：
            # - SM::哎哟喂/被挤爆/稍后重试/频繁 = 服务端临时限流（等几分钟自愈，不是 cookie 失效）
            # - captcha url = 需要过人机验证（cookie 指纹被标记）
            # - 其他 punish url = cookie 失效
            _is_server_rate_limit = (
                "SM::" in ret_str and
                ("挤爆" in ret_str or "稍后" in ret_str or "频" in ret_str or "繁忙" in ret_str)
            )
            if _is_server_rate_limit:
                self._record_err("RGV587_服务端限流(稍后重试)")
            elif "captcha" in _captcha_url.lower():
                self._record_err("RGV587_需验证码(cookie指纹被标记)")
            elif "punish" in _captcha_url.lower():
                self._record_err("RGV587_cookie失效(需重新登录)")
            else:
                self._record_err(f"RGV587_其他:{ret_str[:50]}")

            # bxpunish=1 标志记录但不再 600s 全局冷却（避免误杀并行成功流量）
            # 实测同一 IP 同一时刻部分请求会成功部分会 punish，不该一次失败就全停
            try:
                _bx = r.headers.get("bxpunish", "")
                if _bx == "1":
                    with self._cooldown_lock:
                        if not getattr(self, '_punish_warned', False):
                            self._punish_warned = True
                            self._log(f"[GF-API] ⚠ IP 受 bxpunish 干扰（cookie 未过 BX 信任，建议重新登录让脚本浏览商品建立信任）")
                    return None
            except Exception:
                pass

            return None  # 单条失败，让上层熔断器统计连续失败数

        # 非法访问
        if "ILLEGAL_ACCESS" in ret_str:
            self._record_err("ILLEGAL_ACCESS")
            return None

        # 其他：尝试从原始文本提取
        try:
            _txt = r.text if hasattr(r, "text") else json.dumps(result)
        except Exception:
            _txt = ""
        extracted = _extract_status_from_text(_txt)
        if extracted:
            return extracted
        data_part = result.get("data", {})
        if data_part:
            item = data_part.get("itemDO", {})
            if item:
                return {
                    "status": item.get("itemStatusStr", "未知"),
                    "title": (item.get("title", "") or "")[:50],
                }

        # 未识别的失败：记录前 50 字方便诊断
        self._record_err(f"OTHER:{ret_str[:50]}")
        return None

    def _record_err(self, kind: str) -> None:
        """记录失败类型（线程安全）"""
        if self._err_lock is None:
            import threading
            self._err_lock = threading.Lock()
        with self._err_lock:
            self._err_stats[kind] = self._err_stats.get(kind, 0) + 1

    def get_error_summary(self) -> str:
        """返回失败类型统计字符串（用于诊断日志）"""
        if not self._err_stats:
            return "无错误"
        items = sorted(self._err_stats.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}={v}" for k, v in items[:8])

    def _check_item_retry(self, item_id: str) -> Optional[Dict]:
        """token 刷新后重试"""
        t = _ts_ms()
        data_str = _build_detail_payload(item_id)
        sign = _sign(self._token, t, data_str)

        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign, "v": "1.0",
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "AntiCreep": "true", "AntiFlool": "true",
            "api": DETAIL_API,
            "spm_cnt": "a21ybx.item.0.0",
            "spm_pre": "widle.12011849.0.0",
            "sessionOption": "AutoLoginOnly",
        }

        try:
            r = self._session.post(
                f"{API_BASE}/{DETAIL_API}/1.0/",
                params=params, data={"data": data_str},
                headers=_headers_for_item(item_id), timeout=15,
            )
            result = r.json()
            ret = result.get("ret", [])
            ret_str = " ".join(str(x) for x in ret) if ret else ""

            if "SUCCESS" in ret_str:
                try:
                    _txt = r.text
                except Exception:
                    _txt = json.dumps(result)
                extracted = _extract_status_from_text(_txt)
                if extracted:
                    return extracted
                item = result.get("data", {}).get("itemDO", {}) or {}
                _status = item.get("itemStatusStr", "未知")
                if _is_auction_text(_txt):
                    _status = "拍卖"
                return {
                    "status": _status,
                    "title": (item.get("title", "") or "")[:50],
                }
            if "NOT_FOUND" in ret_str or "DEL" in ret_str:
                return {"status": "已删除", "title": ""}
        except Exception:
            pass
        return None
