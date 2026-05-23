from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ------------------------ 输出表格格式（只影响Excel外观，不改功能） ------------------------
FORMAT_FONT_NAME = "等线"
FORMAT_FONT_SIZE = 16
FORMAT_ROW_HEIGHT = 70  # 你要求：行高70
FORMAT_ZOOM_SCALE = 50  # 你模板是50；保持一致
# 日期显示：1月1日 这种（与您修改后的Excel一致）
FORMAT_DATE_NF = 'm"月"d"日";@'
FMT_RED = "FFFF0000"    # 纯红
FMT_GREEN = "FF9BBB59"  # 你模板的绿（155,187,89）

# 线上贴单资料：绿列（其余默认为红）
STORE_HEADER_GREEN = {
    "賬號", "系统編碼", "所屬人", "金额", "代付日期", "商品成本", "商品數量", "聯係電話"
}

# 宅配打包资料：绿列（其余默认为红）
HOME_HEADER_GREEN = {
    "賬號", "訂單編碼", "所屬人", "訂單金額", "代付日期", "商品成本", "商品數量"
}

# 列宽：按您“修改過的版本.xlsx”的列宽固定（避免自适应把表头挤窄）
STORE_COL_WIDTHS = {
    'A': 15.75, 'B': 35.7083333333333, 'C': 16.425, 'D': 31.75, 'E': 18.925, 'F': 28.5666666666667, 'G': 22.25, 'H': 26.425, 'I': 26.75, 'J': 18.5, 'K': 17.25, 'L': 16.25, 'M': 22.0, 'N': 20.25, 'O': 23.25, 'P': 28.25, 'Q': 86.75
}

HOME_COL_WIDTHS = {
    'A': 18.5, 'B': 35.25, 'C': 20.75, 'D': 32.0, 'E': 16.75, 'F': 34.75, 'G': 13.75, 'H': 15.25, 'I': 20.75, 'J': 25.625, 'K': 60.75, 'L': 20.5, 'M': 25.625, 'N': 18.5, 'O': 16.25, 'P': 18.75, 'Q': 18.25, 'R': 46.0
}

def _text_width_hint(s: str) -> int:
    """粗略估算列宽：中文算2，ASCII算1。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 127 else 1
    return w

def _autofit_columns(ws, max_scan: int = 200, min_w: float = 8.0, max_w: float = 60.0) -> None:
    """按内容自适应列宽（扫描表头 + 最近N行，避免大模板太慢）。"""
    if ws is None:
        return
    max_row = ws.max_row or 1
    max_col = ws.max_column or 1

    rows_to_scan = {1, 2}
    start = max(3, max_row - max_scan + 1)
    for r in range(start, max_row + 1):
        rows_to_scan.add(r)

    for col in range(1, max_col + 1):
        max_hint = 0
        for r in rows_to_scan:
            v = ws.cell(row=r, column=col).value
            if v is None:
                continue
            s = str(v)
            if not s:
                continue
            max_hint = max(max_hint, _text_width_hint(s))
        if max_hint <= 0:
            continue
        width = max(min_w, min(max_w, max_hint * 0.9 + 4))
        ws.column_dimensions[get_column_letter(col)].width = width


def _apply_column_widths(ws, widths: dict) -> None:
    if ws is None or not widths:
        return
    try:
        for letter, w in widths.items():
            ws.column_dimensions[str(letter)].width = float(w)
    except Exception:
        pass

def _apply_freeze_and_filter(ws) -> None:
    if ws is None:
        return
    # 凍結首行
    try:
        ws.freeze_panes = "A2"
    except Exception:
        pass
    # 篩選（覆盖到当前数据范围）
    try:
        last_col = get_column_letter(ws.max_column or 1)
        last_row = ws.max_row or 1
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"
    except Exception:
        pass

def _apply_center_and_no_border(ws, max_scan: int = 300) -> None:
    """统一水平/垂直居中，去掉框线。
    为了性能：处理表头+第2行+最后N行。
    """
    if ws is None:
        return
    max_row = ws.max_row or 1
    max_col = ws.max_column or 1

    rows_to_fix = {1, 2}
    start = max(3, max_row - max_scan + 1)
    for r in range(start, max_row + 1):
        rows_to_fix.add(r)

    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    no_border = Border()
    for r in rows_to_fix:
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            try:
                cell.alignment = align
            except Exception:
                pass
            try:
                cell.border = no_border
            except Exception:
                pass

def _apply_header_colors(ws, green_set: set[str]) -> None:
    if ws is None:
        return
    red_fill = PatternFill("solid", fgColor=FMT_RED)
    green_fill = PatternFill("solid", fgColor=FMT_GREEN)
    header_font = Font(name=FORMAT_FONT_NAME, size=FORMAT_FONT_SIZE, bold=True)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    max_col = ws.max_column or 1
    for col in range(1, max_col + 1):
        cell = ws.cell(row=1, column=col)
        key = str(cell.value).strip() if cell.value is not None else ""
        if not key:
            continue
        cell.fill = (green_fill if key in green_set else red_fill)
        cell.font = header_font
        cell.alignment = header_align
    ws.row_dimensions[1].height = float(FORMAT_ROW_HEIGHT)

def _force_font_size(ws, max_scan: int = 300) -> None:
    """统一字体为 等线 16（保留bold等属性）。
    为了不拖慢（你的模板可能有历史很多行），这里只处理：表头+第2行样式行+最后N行。
    """
    if ws is None:
        return
    max_row = ws.max_row or 1
    max_col = ws.max_column or 1

    rows_to_fix = {1, 2}
    start = max(3, max_row - max_scan + 1)
    for r in range(start, max_row + 1):
        rows_to_fix.add(r)

    for r in rows_to_fix:
        for c in range(1, max_col + 1):
            cell = ws.cell(row=r, column=c)
            f = cell.font
            cell.font = Font(
                name=FORMAT_FONT_NAME,
                size=FORMAT_FONT_SIZE,
                bold=bool(getattr(f, "b", False)),
                italic=bool(getattr(f, "i", False)),
                underline=getattr(f, "u", None),
                color=getattr(f, "color", None),
            )

def _apply_sheet_view(ws) -> None:
    if ws is None:
        return
    try:
        ws.sheet_view.zoomScale = int(FORMAT_ZOOM_SCALE)
    except Exception:
        pass
    try:
        ws.sheet_format.defaultRowHeight = float(FORMAT_ROW_HEIGHT)
    except Exception:
        pass

def _apply_output_formatting(ws_store, ws_home) -> None:
    """只做样式，不动业务数据/逻辑。"""
    if ws_store is not None:
        _apply_sheet_view(ws_store)
        _apply_header_colors(ws_store, STORE_HEADER_GREEN)
        _force_font_size(ws_store)
        _apply_column_widths(ws_store, STORE_COL_WIDTHS)
        _apply_freeze_and_filter(ws_store)
        _apply_center_and_no_border(ws_store)

    if ws_home is not None:
        _apply_sheet_view(ws_home)
        _apply_header_colors(ws_home, HOME_HEADER_GREEN)  # 表头加粗/颜色同线上
        _force_font_size(ws_home)
        _apply_column_widths(ws_home, HOME_COL_WIDTHS)
        _apply_freeze_and_filter(ws_home)
        _apply_center_and_no_border(ws_home)

        # 宅配打包资料：行高固定 70（整表）
        try:
            ws_home.sheet_format.defaultRowHeight = float(FORMAT_ROW_HEIGHT)
        except Exception:
            pass

from .client_runtime_compat import async_playwright, apply_runtime_normalization_async, get_launch_args, get_ignore_default_args, CHROME_UA
try:
    from patchright.async_api import Page
except ImportError:
    from playwright.async_api import Page
from .profile_lock import try_acquire, release, detect_chrome_profile_in_use

# ============================================================
# Yahoo!拍卖（台湾） 订单获取 / 出货
# - 进入「销售订单管理」页面，切换搜索类型为「订单编号」
# - 输入订单编号 -> 搜索 -> 打开「明细」弹窗
# - 抓取：订单编号 / 金额 / 渠道(7-11/萊爾富/全家/OK) / 收件人(姓名/电话/门市) / 数量
# - 商品相关（商品名稱/规格/代付日期/代付金額/国内快递单号）由界面输入
# - 支持「一个订单多个国内快递单号」：
#     * 第一行写完整订单信息 + 第1个快递单号
#     * 其余快递单号写到下一行（仅写：国内快递单号/商品名稱/规格/代付日期；商品成本在第一行用“+”串联）
# - 写入用户提供的 Excel 模板（默认工作表：线上贴单资料）
# ============================================================

LIST_URL = "https://tw.bid.yahoo.com/partner/order/list"

DEFAULT_SHEET_NAME = "线上贴单资料"
HOME_SHEET_NAME = "宅配打包资料"
ERROR_SHEET_NAME = "错误订单"
# 允许表头繁简/同义
HEADER_ALIASES = {'編碼': ['編碼', '编码', 'Code', '執行編碼', '执行编码'],
 '賬號': ['賬號', '账号', '帳號', '帐号', 'Account'],
 '日期': ['日期', 'Date'],
 '系统編碼': ['系统編碼', '系統編碼', '系统编码', '系統編號', '系统编号', '訂單編號', '订单编号', '訂單編碼', '订单编码'],
 '所屬人': ['所屬人', '所属人', 'Owner'],
 '国内快递单号': ['国内快递单号', '國內快遞單號', '国内快遞單號', '國內快递单号', '快遞單號', '快递单号'],
 '转单号': ['转单号', '轉單號'],
 '代收货款': ['代收货款', '代收貨款', '货到付款', '貨到付款'],
 '收件人': ['收件人', '收貨人', '收货人', '姓名', '收件人姓名'],
 '门市/店': ['门市/店', '門市/店', '門市', '门市', '店', '門店', '门店', '收件地址'],
 '商品名稱': ['商品名稱', '商品名称', '商品', '品名', '商品名'],
 '规格': ['规格', '規格', 'Spec'],
 '渠道': ['渠道', '通路', '取貨通路', '超商', '寄送方式', '運送', '运送', 'Shipping'],
 '金额': ['金额', '金額', '總額', '订单金额', '訂單金額', '總金額', '订单金額', '訂單金额'],
 '代付日期': ['代付日期', '代付', '付款日期', '支付日期'],
 '商品成本': ['商品成本', '成本', '进货成本', '進貨成本'],
 '商品數量': ['商品數量', '商品数量', '數量', '数量', '件数', '件數'],
 '聯係電話': ['聯係電話', '联系电话', '聯絡電話', '联系电话', '電話', '电话', '手机', '手機', '聯繫電話'],
 '备注': ['备注', '備註', '備注', 'Remark', '備考']}



STORE_HEADERS = ['編碼', '賬號', '日期', '系统編碼', '所屬人', '国内快递单号', '收件人', '门市/店', '商品名稱', '规格', '渠道', '金额', '代付日期', '商品成本', '商品數量', '聯係電話', '备注']
HOME_HEADERS = ['編碼', '賬號', '日期', '訂單編碼', '所屬人', '国内快递单号', '转单号', '代收货款', '收件人', '联系电话', '收件地址', '商品名稱', '規格', '訂單金額', '代付日期', '商品成本', '商品數量', '備注']
ERROR_HEADERS = ['訂單編碼', '賬號', '原因', '時間']

def _ensure_headers(ws, headers: List[str]) -> None:
    """若 sheet 为空白（第1行无表头），写入表头。仅用于模板缺少该 sheet 时的兜底创建。"""
    # 判断第1行是否已有任意非空标题
    max_col = max(ws.max_column, len(headers))
    has_any = False
    for c in range(1, max_col + 1):
        v = ws.cell(row=1, column=c).value
        if v is not None and str(v).strip() != "":
            has_any = True
            break
    if has_any:
        return

    bold = Font(bold=True)
    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c)
        cell.value = h
        cell.font = bold
        cell.alignment = align
        cell.border = border

    ws.freeze_panes = "A2"

def _norm_header(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("：", ":").replace("／", "/")
    return s


def _build_header_map(ws) -> Dict[str, int]:
    """
    返回：标准字段名 -> 列号（1-based）
    通过 HEADER_ALIASES 识别不同表头写法。
    """
    header_row = 1
    raw = {}
    for cell in ws[header_row]:
        if cell.value is None:
            continue
        raw[_norm_header(str(cell.value))] = cell.column

    out: Dict[str, int] = {}
    for std, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if a in raw:
                out[std] = raw[a]
                break
    return out


def _ensure_remark_header(ws, display_header: str) -> None:
    """确保表头里存在“备注”列。

    背景：有些新版模板把「备注/備注」这列删掉了，但导出逻辑仍会尝试写入备注。
    旧逻辑 _ensure_headers 只在整张表“完全没表头”时才写表头，
    所以模板只要有表头但少了「备注」，就会导致你看到“备注不见了”。

    这里做一个最小兜底：如果表头里识别不到标准字段「备注」，就把 display_header
    追加到“最后一个非空表头列”的后面，并尽量复制表头/样式行的样式。
    """
    try:
        hmap = _build_header_map(ws)
        if hmap.get("备注"):
            return

        # 找最后一个非空表头列
        last = 0
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=1, column=c).value
            if v is not None and str(v).strip() != "":
                last = c
        if last <= 0:
            # 没表头，交给 _ensure_headers
            return

        new_col = last + 1
        dst_h = ws.cell(row=1, column=new_col)
        dst_h.value = display_header
        try:
            _copy_cell_style(ws.cell(row=1, column=last), dst_h)
        except Exception:
            pass

        # 复制列宽
        try:
            from openpyxl.utils import get_column_letter
            ws.column_dimensions[get_column_letter(new_col)].width = ws.column_dimensions[get_column_letter(last)].width
        except Exception:
            pass

        # 复制样式行（通常第2行）对应列的样式，保证后续追加行能正常套样式
        try:
            style_row = _find_style_row(ws)
            if style_row and style_row <= ws.max_row and style_row != 1:
                dst_s = ws.cell(row=style_row, column=new_col)
                _copy_cell_style(ws.cell(row=style_row, column=last), dst_s)
                dst_s.value = None
        except Exception:
            pass
    except Exception:
        return



def _get_sheet(
    wb: openpyxl.Workbook,
    name: str,
    create: bool = False,
    fallback_active: bool = False,
):
    """按名称获取 sheet；不存在时可选择创建。fallback_active=False 时不存在会返回 None。"""
    if name in wb.sheetnames:
        return wb[name]
    if create:
        return wb.create_sheet(name)
    return wb.active if fallback_active else None


def _excel_date(d: _dt.date) -> _dt.date:
    # openpyxl 接受 date 对象，会自动保存为 Excel date
    return d


def _is_cell_empty(cell) -> bool:
    v = cell.value
    return v is None or (isinstance(v, str) and not v.strip())


def _find_style_row(ws) -> int:
    # 优先用第 2 行作为样式行（模板通常有示例数据），否则用第 1 行
    if ws.max_row >= 2:
        # 若第2行几乎全空，也可能只是空模板；此时仍可用作样式行
        return 2
    return 1


def _copy_cell_style(src, dst):
    dst.font = src.font.copy()
    dst.fill = src.fill.copy()
    dst.border = src.border.copy()
    dst.alignment = src.alignment.copy()
    dst.number_format = src.number_format
    dst.protection = src.protection.copy()
    dst.comment = None


def _append_row_with_style(ws, style_row: int) -> int:
    new_row = ws.max_row + 1
    for col in range(1, ws.max_column + 1):
        src = ws.cell(row=style_row, column=col)
        dst = ws.cell(row=new_row, column=col)
        _copy_cell_style(src, dst)
        # 清空值（避免复制样例内容）
        dst.value = None
        try:
            dst.hyperlink = None
        except Exception:
            pass
    return new_row


def _next_serial_for_day(ws, col_code: int, user_code: str, mmdd: str) -> int:
    prefix = f"{user_code}{mmdd}"
    max_n = 0
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=col_code).value
        if not v:
            continue
        s = str(v).strip()
        if s.startswith(prefix) and len(s) >= len(prefix) + 2:
            tail = s[len(prefix): len(prefix) + 2]
            if tail.isdigit():
                max_n = max(max_n, int(tail))
    return max_n + 1


def _next_serial_for_day_multi(sheets, col_codes, user_code: str, mmdd: str) -> int:
    """跨多个 sheet 取当天流水号的下一个值，保证同一天不同 sheet 不重复。"""
    prefix = f"{user_code}{mmdd}"
    max_n = 0
    for ws, col_code in zip(sheets, col_codes):
        if not ws or not col_code:
            continue
        for r in range(2, ws.max_row + 1):
            try:
                v = ws.cell(row=r, column=col_code).value
            except Exception:
                continue
            if not v:
                continue
            s = str(v).strip()
            if s.startswith(prefix) and len(s) >= len(prefix) + 2:
                tail = s[len(prefix): len(prefix) + 2]
                if tail.isdigit():
                    max_n = max(max_n, int(tail))
    return max_n + 1


def _parse_mmdd_to_date(mmdd: str, exec_date: _dt.date) -> Optional[_dt.date]:
    """把界面输入的纯数字日期（如 0107）转成 date。

    规则：
    - 允许 3/4 位数字（107 -> 0107）
    - 默认用执行当天年份
    - 若执行月为 1 月且输入月为 12 月，则认为是上一年（处理跨年）
    """
    s = re.sub(r"\D+", "", (mmdd or "").strip())
    if not s:
        return None
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4:
        raise ValueError(f"代付日期格式错误：{mmdd}（应为 4 位数字，如 0107）")
    m = int(s[:2])
    d = int(s[2:])
    y = int(exec_date.year)
    if exec_date.month == 1 and m == 12:
        y -= 1
    try:
        return _dt.date(y, m, d)
    except Exception as e:
        raise ValueError(f"代付日期无效：{mmdd} -> {y:04d}-{m:02d}-{d:02d}") from e


# ------------------------ HTTP API 获取订单详情 -------------------------

# shipping.type → 渠道名
# v6.1.39:加 sevenPickup(7-11 取貨不付款,user 確認 Yahoo 只有 7-11 有此選項)
# 全家/萊爾富/OK 沒有取貨不付款,不要加(避免錯誤 mapping)
_SHIP_TYPE_MAP = {
    "tCat": "黑貓",
    "homeDelivery": "黑貓",
    "sevenCvs": "7-11",       # 7-11 取貨付款
    "sevenPickup": "7-11",    # 7-11 取貨不付款(同流程,同渠道名)
    "familyCvs": "全家",
    "hilifeCvs": "萊爾富",
    "okCvs": "OK",
}


def _fetch_order_detail_http(
    profile_dir: Path,
    order_id: str,
    chrome_path: str = "",
    headless: bool = True,
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """用 HTTP API (FETCH_ORDER_DETAIL) 获取订单详情。

    Returns:
        成功 → {"amount": int, "channel": str, "receiver_name": str,
                "receiver_phone": str, "receiver_store": str,
                "product_name": str, "qty": int}
        失败 → None
    """
    try:
        from .merch_http_ops import _try_cached_session, _post_reservice, AuthExpiredError, _extract_and_save
        from .cookie_store import invalidate_cookie_cache
    except ImportError:
        return None

    session = _try_cached_session(profile_dir, log=on_log)
    if not session or not session.is_valid:
        if chrome_path:
            if on_log:
                on_log(f"[SHIP] HTTP: cookie cache 无效，提取新 cookies...")
            import asyncio
            try:
                session = asyncio.run(_extract_and_save(
                    profile_dir, chrome_path, headless, "", on_log))
            except Exception as e:
                if on_log:
                    on_log(f"[SHIP] HTTP: 提取 cookies 失败: {e}")
                return None
            if not session or not session.is_valid:
                if on_log:
                    on_log(f"[SHIP] HTTP: 提取的 session 无效，跳过 HTTP 路径")
                return None
        else:
            if on_log:
                on_log(f"[SHIP] HTTP: cookie cache 无效，跳过 HTTP 路径")
            return None

    def _do_request(_session):
        return _post_reservice(_session, "FETCH_ORDER_DETAIL", {
            "wssid": _session.wssid,
            "orderId": order_id,
            "archive": False,
            "role": "seller",
        })

    try:
        data = _do_request(session)
    except AuthExpiredError as e:
        # wssid 失效(401001 等) → 清缓存 + 重新提取 cookie + 重试一次
        if on_log:
            on_log(f"[SHIP] HTTP wssid 失效 ({e})，清缓存重新提取 cookie 后重试...")
        invalidate_cookie_cache(profile_dir)
        if not chrome_path:
            if on_log:
                on_log(f"[SHIP] HTTP: 无 chrome_path,无法重新提取,放弃")
            return None
        import asyncio
        try:
            session = asyncio.run(_extract_and_save(
                profile_dir, chrome_path, headless, "", on_log))
        except Exception as e2:
            if on_log:
                on_log(f"[SHIP] HTTP: 重新提取 cookie 异常: {e2}")
            return None
        if not session or not session.is_valid:
            if on_log:
                on_log(f"[SHIP] HTTP: 重新提取后 session 仍无效,放弃")
            return None
        try:
            data = _do_request(session)
        except Exception as e3:
            if on_log:
                on_log(f"[SHIP] HTTP 重试仍失败: {e3}")
            return None
    except Exception as e:
        if on_log:
            on_log(f"[SHIP] HTTP FETCH_ORDER_DETAIL 失败: {e}")
        return None

    payload = data.get("payload", {})
    if not payload:
        if on_log:
            on_log(f"[SHIP] HTTP: payload 为空")
        return None

    # --- 渠道 ---
    shipping = payload.get("shipping", {})
    ship_type = shipping.get("type", "")
    channel = _SHIP_TYPE_MAP.get(ship_type, "")
    if not channel:
        # 尝试从 shipping 文本中匹配
        ship_text = str(shipping)
        channel = _short_channel(ship_text)

    # --- 收件人 ---
    receiver = payload.get("receiver", {})
    if on_log:
        import json as _j
        on_log(f"[SHIP] HTTP receiver 原始数据: {_j.dumps(receiver, ensure_ascii=False)}")
    receiver_name = str(receiver.get("name", "") or "").strip()
    receiver_phone = str(receiver.get("phone", "") or receiver.get("mobile", "") or "").strip()

    # 宅配: 组装完整地址 — API 实际字段: zipcode(小写) + city + town + address
    _zip = str(receiver.get("zipcode", "") or receiver.get("zipCode", "") or receiver.get("postalCode", "") or "").strip()
    _city = str(receiver.get("city", "") or "").strip()
    _district = str(receiver.get("town", "") or receiver.get("district", "") or receiver.get("area", "") or "").strip()
    _street = str(receiver.get("address", "") or receiver.get("street", "") or "").strip()
    # 组装: "247 新北市蘆洲區三民路95號3樓"
    _full_parts = [p for p in [_zip, _city, _district, _street] if p]
    receiver_addr = " ".join(_full_parts) if _full_parts else _street

    # 超商: 门市名 — API 实际字段: pickupStoreName + pickupStoreId
    receiver_store = ""
    _pickup_name = str(receiver.get("pickupStoreName", "") or "").strip()
    _pickup_id = str(receiver.get("pickupStoreId", "") or "").strip()
    if _pickup_name:
        receiver_store = f"{_pickup_name}（{_pickup_id}）" if _pickup_id else _pickup_name
    if not receiver_store:
        for _sk in ("storeName", "store", "shopName", "cvsStoreName"):
            _sv = receiver.get(_sk, "")
            if _sv and isinstance(_sv, str):
                receiver_store = _sv.strip()
                break

    # 统一: 超商用门市名，宅配用地址
    receiver_display = receiver_store or receiver_addr

    # --- 金额 ---
    amount = 0
    # API 实际结构: payload.price.orderAmount
    _price_obj = payload.get("price", {})
    if isinstance(_price_obj, dict):
        for pk in ("orderAmount", "itemAmount", "totalAmount"):
            pv = _price_obj.get(pk)
            if pv is not None:
                try:
                    amount = int(float(str(pv)))
                    if amount > 0:
                        break
                except (ValueError, TypeError):
                    pass
    # 回退: 顶层字段
    if amount == 0:
        for key in ("totalAmount", "amount", "orderAmount", "total"):
            v = payload.get(key)
            if v is not None:
                try:
                    amount = int(float(str(v)))
                    if amount > 0:
                        break
                except (ValueError, TypeError):
                    pass
    # 回退: items 里的 totalPrice
    if amount == 0:
        items = payload.get("items", [])
        for item in items:
            for key in ("price", "amount", "totalPrice"):
                v = item.get(key)
                if v:
                    try:
                        amount += int(float(str(v)))
                    except (ValueError, TypeError):
                        pass

    # --- 商品名 & 数量 ---
    items = payload.get("items", [])
    product_name = ""
    qty = 0
    for item in items:
        pn = str(item.get("title", "") or item.get("name", "") or item.get("productName", "") or "").strip()
        if pn and not product_name:
            product_name = pn
        q = item.get("quantity", 1) or item.get("qty", 1) or 1
        try:
            qty += int(q)
        except (ValueError, TypeError):
            qty += 1
    if qty == 0:
        qty = 1

    result = {
        "amount": amount,
        "channel": channel,
        "receiver_name": receiver_name,
        "receiver_phone": receiver_phone,
        "receiver_store": receiver_display,
        "product_name": product_name,
        "qty": qty,
    }

    if on_log:
        on_log(f"[SHIP] HTTP 订单详情: {order_id} → "
               f"channel={channel}, amount={amount}, "
               f"receiver={receiver_name}, phone={receiver_phone}")

    # 基本验证: 至少有渠道
    if not channel:
        if on_log:
            on_log(f"[SHIP] HTTP: 无法识别渠道 (shipping.type={ship_type})，跳过 HTTP 路径")
        return None

    return result


# ------------------------ 页面抓取 ------------------------

async def _ensure_on_orders_page(page: Page, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None):
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    # 页面文案可能繁简不同：銷售訂單管理 / 销售订单管理
    try:
        await page.wait_for_selector("text=銷售訂單管理", timeout=1500)
    except Exception:
        try:
            await page.wait_for_selector("text=销售订单管理", timeout=1500)
        except Exception:
            # 退化：等待订单管理区的搜索按钮或输入框出现
            await page.wait_for_selector("button:has-text('搜尋'), button:has-text('搜索'), input[type='search'], input", timeout=timeout_ms)
    if on_log:
        on_log("[SHIP] 已进入订单管理页面")


async def _open_search_panel(page: Page, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None):
    """
    打开右侧的『基本選項/基本选项』面板（包含『搜尋類型/搜索类型』下拉）。
    Yahoo 页面偶尔更新，这里做了多重兜底：
    1) 若已展开则直接返回
    2) 优先在『搜尋/搜索』按钮附近寻找无文字的小图标按钮
    3) 退化为遍历页面中小尺寸 svg 按钮，直到面板出现
    """
    # 已展开则直接返回
    try:
        if await page.locator("text=基本選項").count() > 0 or await page.locator("text=基本选项").count() > 0:
            return
        if await page.locator("text=搜尋類型").count() > 0 or await page.locator("text=搜索类型").count() > 0:
            return
    except Exception:
        pass

    async def _panel_visible() -> bool:
        try:
            return (await page.locator("text=基本選項").count() > 0
                    or await page.locator("text=基本选项").count() > 0
                    or await page.locator("text=搜尋類型").count() > 0
                    or await page.locator("text=搜索类型").count() > 0)
        except Exception:
            return False

    # 先在『搜尋/搜索』按钮附近找『滑杆/筛选』按钮
    try:
        # 订单管理区的搜索按钮一般是『搜尋』(非『搜尋商品』)
        btn_search = page.get_by_role("button", name=re.compile(r"^(搜尋|搜索)$"))
        if await btn_search.count() > 0:
            b = btn_search.first
            for k in range(1, 5):
                cont = b.locator(f"xpath=ancestor::div[{k}]")
                icons = cont.locator("button").filter(has=cont.locator("svg"))
                n = await icons.count()
                for i in range(n):
                    ib = icons.nth(i)
                    try:
                        t = (await ib.inner_text()).strip()
                    except Exception:
                        t = ""
                    if t in {"搜尋", "搜索", "Search", "搜尋商品", "搜索商品"}:
                        continue
                    try:
                        box = await ib.bounding_box()
                    except Exception:
                        box = None
                    if box and box.get("width", 999) <= 80 and box.get("height", 999) <= 80:
                        await ib.click()
                        await page.wait_for_timeout(200)
                        if await _panel_visible():
                            if on_log:
                                on_log("[SHIP] 已打开搜索类型面板")
                            return
                        # 不是目标面板，收起再继续
                        try:
                            await page.keyboard.press("Escape")
                        except Exception:
                            pass
    except Exception:
        pass

    # 退化：遍历页面小尺寸 svg button
    try:
        btns = page.locator("button").filter(has=page.locator("svg"))
        total = await btns.count()
        for i in range(min(total, 30)):
            ib = btns.nth(i)
            try:
                t = (await ib.inner_text()).strip()
            except Exception:
                t = ""
            if t in {"搜尋", "搜索", "Search", "搜尋商品", "搜索商品"}:
                continue
            try:
                box = await ib.bounding_box()
            except Exception:
                box = None
            if box and box.get("width", 999) <= 80 and box.get("height", 999) <= 80:
                await ib.click()
                await page.wait_for_timeout(200)
                if await _panel_visible():
                    if on_log:
                        on_log("[SHIP] 已打开搜索类型面板")
                    return
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
    except Exception:
        pass

    if on_log:
        on_log("[SHIP] 未找到筛选按钮，尝试直接操作下拉框")


async def _ensure_search_type_order_no(page: Page, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None):
    """把搜索类型切换到『订单编号』。

    Yahoo 这块 UI 经常改：
    - 有时是原生 <select>
    - 有时是自定义下拉（面板标题『基本選項/基本选项』）

    这里按「能用就行」的思路做多重兜底：
    1) 先打开面板
    2) 若存在 <select> 则直接 select_option
    3) 否则在面板里点击当前选项文字（商品关键字/商品關鍵字/訂單編號），再点『订单编号/訂單編號』
    """
    await _open_search_panel(page, timeout_ms, on_log=on_log)

    # 等待面板元素渲染
    try:
        await page.wait_for_timeout(200)
    except Exception:
        pass

    # 1) 原生 select
    try:
        sel = page.locator("select").first
        if await sel.count() > 0:
            # 尝试找到包含『订单编号』的 option 值
            opts = await sel.locator("option").all_inner_texts()
            target_idx = None
            for i, t in enumerate(opts):
                if "訂單編號" in t or "订单编号" in t:
                    target_idx = i
                    break
            if target_idx is not None:
                val = await sel.locator("option").nth(target_idx).get_attribute("value")
                if val is not None:
                    await sel.select_option(val)
                    if on_log:
                        on_log("[SHIP] 已切换搜索类型：订单编号")
                    return
    except Exception:
        pass

    # 2) 自定义面板下拉
    panel = None
    try:
        t = page.locator("text=基本選項").first
        if await t.count() == 0:
            t = page.locator("text=基本选项").first
        if await t.count() > 0:
            panel = t.locator("xpath=ancestor::div[1]")
    except Exception:
        panel = None

    scope = panel if panel else page

    # 先点开下拉：优先点当前值文字
    opened = False
    for cur in ["商品關鍵字", "商品关键字", "商品關鍵", "商品关键", "訂單編號", "订单编号"]:
        try:
            loc = scope.locator(f"text={cur}").first
            if await loc.count() > 0:
                await loc.click()
                opened = True
                break
        except Exception:
            continue

    # 若没找到当前值文字，就尝试点『搜尋類型/搜索类型』那一行的可点击区域
    if not opened:
        for lab in ["搜尋類型", "搜索类型", "搜索類型"]:
            try:
                l = scope.locator(f"text={lab}").first
                if await l.count() > 0:
                    cont = l.locator("xpath=ancestor::div[1]")
                    # 找一个最像下拉触发器的元素
                    for sel2 in ["[role='combobox']", "[aria-haspopup='listbox']", "button", "div"]:
                        cand = cont.locator(sel2).first
                        if await cand.count() > 0:
                            await cand.click()
                            opened = True
                            break
                if opened:
                    break
            except Exception:
                continue

    # 选择『订单编号/訂單編號』
    selected = False
    for opt in ["訂單編號", "订单编号"]:
        try:
            o = page.locator(f"text={opt}").first
            if await o.count() > 0:
                await o.click()
                selected = True
                break
        except Exception:
            continue

    # 收起下拉，避免挡住输入框
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    # 验证（尽量）
    try:
        chk = scope.locator("text=訂單編號")
        if await chk.count() == 0:
            chk = scope.locator("text=订单编号")
        ok = await chk.count() > 0
    except Exception:
        ok = False

    if on_log:
        if selected and ok:
            on_log("[SHIP] 已切换搜索类型：订单编号")
        else:
            on_log("[SHIP] 警告：未能确认搜索类型已切换为订单编号（可先手动切一次，系统通常会记住）")


async def _search_order(page: Page, order_no: str, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None) -> bool:
    async def _dump_inputs():
        try:
            inputs = page.locator("input")
            n = await inputs.count()
            lines = [f"[SHIP][DBG] 页面共发现 input={n}"]
            for i in range(min(n, 12)):
                el = inputs.nth(i)
                try:
                    ph = await el.get_attribute("placeholder")
                except Exception:
                    ph = None
                try:
                    typ = await el.get_attribute("type")
                except Exception:
                    typ = None
                try:
                    name = await el.get_attribute("name")
                except Exception:
                    name = None
                try:
                    aria = await el.get_attribute("aria-label")
                except Exception:
                    aria = None
                try:
                    _id = await el.get_attribute("id")
                except Exception:
                    _id = None
                try:
                    box = await el.bounding_box()
                except Exception:
                    box = None
                lines.append(f"  - #{i} type={typ} id={_id} name={name} ph={ph} aria={aria} box={box}")
            if on_log:
                for ln in lines:
                    on_log(ln)
        except Exception:
            pass

    async def _locate_search_input() -> Optional[Any]:
        # 1) 以『搜尋/搜索』按钮为锚点，找同一区块里的输入框
        try:
            btn = page.get_by_role("button", name=re.compile(r"^(搜尋|搜索)$"))
            if await btn.count() > 0:
                b = btn.first
                for k in range(1, 6):
                    cont = b.locator(f"xpath=ancestor::div[{k}]")
                    cand = cont.locator("input[type='search'], input[type='text'], input")
                    cn = await cand.count()
                    for i in range(min(cn, 8)):
                        el = cand.nth(i)
                        try:
                            box = await el.bounding_box()
                        except Exception:
                            box = None
                        if box and box.get("width", 0) >= 150:
                            return el
        except Exception:
            pass

        # 2) placeholder（繁简）
        for ph in ["請輸入商品關鍵字", "请输入商品关键字", "請輸入商品關鍵字", "請輸入商品關鍵字"]:
            try:
                loc = page.get_by_placeholder(ph)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue

        # 3) 文字提示可能不是 placeholder，而是独立节点
        for txt in ["請輸入商品關鍵字", "请输入商品关键字"]:
            try:
                t = page.get_by_text(txt).first
                if await t.count() > 0:
                    cont = t.locator("xpath=ancestor::div[1]")
                    cand = cont.locator("input[type='search'], input[type='text'], input")
                    if await cand.count() > 0:
                        return cand.first
            except Exception:
                continue

        # 4) 退化：找页面中第一个可见的大输入框（优先 type=search）
        try:
            cand = page.locator("input[type='search']")
            if await cand.count() > 0:
                return cand.first
        except Exception:
            pass
        try:
            cand = page.locator("input")
            n = await cand.count()
            for i in range(min(n, 12)):
                el = cand.nth(i)
                try:
                    box = await el.bounding_box()
                except Exception:
                    box = None
                if box and box.get("width", 0) >= 150:
                    return el
        except Exception:
            pass
        return None

    # 先确保面板不会遮挡输入框
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    inp = await _locate_search_input()
    if not inp:
        if on_log:
            on_log("[SHIP] 找不到搜索输入框")
        await _dump_inputs()
        return False

    try:
        await inp.click()
    except Exception:
        pass
    try:
        await inp.fill("")
    except Exception:
        # 部分输入框不支持 fill，退化为 ctrl+a
        try:
            await inp.press("Control+A")
            await inp.press("Backspace")
        except Exception:
            pass
    await inp.type(order_no, delay=30)

    # 点击『搜尋/搜索』按钮（订单管理区）
    clicked = False
    for sel in [
        "button:has-text('搜尋')",
        "button:has-text('搜索')",
    ]:
        try:
            b = page.locator(sel)
            if await b.count() > 0:
                # 避免点到顶部『搜尋商品』，优先短文本
                for i in range(min(await b.count(), 6)):
                    bb = b.nth(i)
                    try:
                        t = (await bb.inner_text()).strip()
                    except Exception:
                        t = ""
                    if t in {"搜尋", "搜索"}:
                        await bb.click()
                        clicked = True
                        break
                if clicked:
                    break
        except Exception:
            continue
    if not clicked:
        try:
            await inp.press("Enter")
        except Exception:
            pass

    if on_log:
        on_log(f"[SHIP] 搜索订单：{order_no}")

    # 等待结果卡片出现（包含订单号）
    try:
        await page.wait_for_selector(f"text={order_no}", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _open_detail_modal(page: Page, order_no: str, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None) -> bool:
    # 在卡片中找「明细」按钮
    # 页面上可能有多个明细，先缩小到含订单号的区域
    card = None
    try:
        card = await page.query_selector(f"div:has-text('{order_no}')")
    except Exception:
        card = None

    # 直接找按钮
    btn = None
    selectors = [
        "button:has-text('明細')",
        "button:has-text('明细')",
        "a:has-text('明細')",
        "a:has-text('明细')",
    ]
    if card:
        for sel in selectors:
            try:
                b = await card.query_selector(sel)
                if b:
                    btn = b
                    break
            except Exception:
                continue
    if not btn:
        for sel in selectors:
            try:
                b = await page.query_selector(sel)
                if b:
                    btn = b
                    break
            except Exception:
                continue
    if not btn:
        if on_log:
            on_log("[SHIP] 找不到「明细」按钮")
        return False

    await btn.click()
    if on_log:
        on_log("[SHIP] 已打开明细弹窗")

    # 等待弹窗出现
    try:
        await page.wait_for_selector("text=明細", timeout=timeout_ms)
    except Exception:
        pass
    return True


def _parse_receiver_lines(block_text: str) -> Tuple[str, str, str]:
    """
    从「收件地址」块解析：
    - 姓名
    - 电话（可能被星号遮挡）
    - 门市/店（或地址/门市名称）

    Yahoo 页面有时会把三行压成一行，因此这里做多种分割与启发式提取。
    """
    raw = (block_text or "").strip()
    # 去掉标题
    raw = re.sub(r"(收件地址|收貨地址|收货地址)\s*[:：]?", "", raw).strip()

    # 统一空白
    raw = raw.replace("\u3000", " ")

    # 先按换行分割
    parts = [p.strip() for p in re.split(r"[\r\n]+", raw) if p.strip()]

    # 如果换行不够，再按 2+ 空格分割（常见于 inner_text 压缩）
    if len(parts) < 3:
        parts = [p.strip() for p in re.split(r"\s{2,}", raw) if p.strip()]

    # 还不够就按单空格拆（兜底）
    if len(parts) < 3:
        parts = [p.strip() for p in raw.split(" ") if p.strip()]

    # 去噪
    noise = ["没有物流", "沒有物流", "物流資訊", "物流信息", "查詢", "查询", "訂單", "订单"]
    parts = [p for p in parts if not any(k in p for k in noise)]

    # 电话：优先找“全由数字/星号组成且含数字”的段
    phone = ""
    for p in parts:
        if re.fullmatch(r"[0-9\*]{6,}", p) and re.search(r"\d", p):
            phone = p
            break
    if not phone:
        m = re.search(r"[0-9\*]{6,}", raw)
        if m:
            phone = m.group(0)

    # 门市/店：含 市/縣/門市/门市/( )/店 的段优先
    store = ""
    for p in parts:
        if p == phone:
            continue
        if ("門市" in p) or ("门市" in p) or ("市" in p) or ("縣" in p) or ("(" in p) or ("（" in p) or ("）" in p) or ("店" in p):
            store = p
            break

    # 如果门市未识别出来，但有足够段落，兜底取最后一段（通常是门市/店）
    if not store and len(parts) >= 3:
        for p2 in reversed(parts):
            if p2 == phone:
                continue
            if p2:
                store = p2
                break

    # 姓名：剩余的第一段（避免明显标签）
    name = ""
    for p in parts:
        if p in (phone, store):
            continue
        if any(k in p for k in ["地址", "物流", "運送", "运送", "金额", "金額"]):
            continue
        if len(p) >= 2:
            name = p
            break

    return name, phone, store



def _short_channel(text: str) -> str:
    t = (text or "")
    # 宅配（黑猫）
    if ("黑貓" in t) or ("黑猫" in t) or ("黑貓宅配" in t) or ("黑猫宅配" in t) or ("宅配貨運" in t) or ("宅配货运" in t):
        return "黑貓"
    # 超商
    if "7-ELEVEN" in t or "7-11" in t:
        return "7-11"
    if "萊爾富" in t or "Hi-Life" in t or "HILIFE" in t:
        return "萊爾富"
    if "全家" in t or "FamilyMart" in t:
        return "全家"
    if "OK" in t or "OKmart" in t or "OK Mart" in t:
        return "OK"
    return ""


def _extract_amount(text: str) -> Optional[int]:
    t = (text or "")

    # 1) 先抓明确标签（最可靠）
    m = re.search(r"(?:訂單金額|订单金额)\s*[:：]?\s*(?:NT\$|\$)?\s*([0-9]{1,9})", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    # 2) 再抓所有货币数字，取最大值（通常订单金额最大，避免 $0 运费）
    nums = []
    for x in re.findall(r"(?:NT\$|\$)\s*([0-9]{1,9})", t):
        try:
            nums.append(int(x))
        except Exception:
            continue
    if nums:
        return max(nums)

    return None



def _extract_qty(text: str) -> int:
    m = re.search(r"(\d+)\s*項商品小計", text)
    if not m:
        m = re.search(r"(\d+)\s*项商品小计", text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # fallback: X 1
    m = re.search(r"\bX\s*(\d+)\b", text, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return 1


async def _extract_detail_data(page: Page, timeout_ms: int, on_log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """
    从「明細」弹窗提取：收件人/电话/门店(或地址)、渠道(短格式)、商品名、数量、订单金额。
    关键：不要用 document.body.innerText（会把整页导航抓进来），而是尽量抓取「弹窗容器」的文本。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    # 1) 等待弹窗内容出现（优先收件地址）
    label_candidates = ["收件地址", "收貨地址"]
    label_found = None
    for lab in label_candidates:
        try:
            await page.locator(f"text={lab}").first.wait_for(timeout=timeout_ms)
            label_found = lab
            break
        except Exception:
            pass

    if not label_found:
        # 最后退路：仍然尝试继续（避免直接崩）
        label_found = "收件地址"

    # 2) 取「明細弹窗」文本：
    # 关键：不要用 body.innerText（会把整页导航抓进来）。这里用「收件地址」的祖先链，
    # 找到一个“最小但完整”的容器（同时包含：收件地址 + 金额/价格信息）。
    txt = ""
    try:
        label_loc = page.locator(f"text={label_found}").first
        best_txt: Optional[str] = None
        best_len: Optional[int] = None
        for k in range(1, 11):
            anc = label_loc.locator(f"xpath=ancestor::*[{k}]").first
            try:
                t = await anc.inner_text()
            except Exception:
                continue
            if not t:
                continue
            # 必须包含收件地址，并且包含金额/价格相关信息（避免只抓到左侧地址块）
            if ("收件地址" in t or "收貨地址" in t) and ("訂單金額" in t or "订单金额" in t or "$" in t):
                # 限制长度：越短越可能是弹窗而不是整页
                if len(t) <= 50000:
                    if best_len is None or len(t) < best_len:
                        best_txt, best_len = t, len(t)
                        # 很小基本就命中了，提前退出
                        if best_len <= 6000:
                            break
        if best_txt:
            txt = best_txt
    except Exception:
        txt = ""

    if not txt:
        # 退路：尽量用 dialog/aria-modal/class 这些常见容器
        try:
            locs = [
                page.locator("div[role='dialog']").filter(has_text=label_found),
                page.locator("[aria-modal='true']").filter(has_text=label_found),
                page.locator("xpath=//*[contains(.,'收件地址') or contains(.,'收貨地址')]/ancestor::*[@role='dialog' or @aria-modal='true'][1]"),
                page.locator("xpath=//*[contains(.,'收件地址') or contains(.,'收貨地址')]/ancestor::div[contains(@class,'dialog') or contains(@class,'Dialog') or contains(@class,'modal') or contains(@class,'Modal')][1]"),
            ]
            for loc in locs:
                if await loc.count() > 0:
                    try:
                        txt = await loc.first.inner_text()
                        if txt:
                            break
                    except Exception:
                        continue
        except Exception:
            txt = ""

    if not txt:
        # 最后兜底
        try:
            txt = await page.locator("body").inner_text()
        except Exception:
            txt = ""

    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    log("[DBG] 明细文本预览(前18行)：\n" + "\n".join(lines[:18]))

    # --- 解析：收件地址块（通常三行：姓名 / 电话 / 门店或地址） ---
    receiver_name = receiver_phone = receiver_store = ""
    try:
        idx = next(i for i, l in enumerate(lines) if ("收件地址" in l) or ("收貨地址" in l))
        stop_tokens = ["沒有物流資訊", "没有物流资讯", "訂單金額", "订单金额", "訂單成立", "订单成立", "執行出貨", "执行出货", "手續費", "手续费"]
        block = []
        for j in range(idx + 1, min(idx + 12, len(lines))):
            lj = lines[j]
            if any(t in lj for t in stop_tokens):
                break
            # 跳过明显的空标签
            if lj in ["收件地址", "收貨地址"]:
                continue
            block.append(lj)
        if block:
            # 地址块第一行常常会出现『7-ELEVEN 取貨付款 / 全家 取貨付款 ...』，这是渠道，不是姓名
            block2 = [x for x in block if not (_short_channel(x) or ('取貨付款' in x) or ('取货付款' in x))]
            if not block2:
                block2 = block
            receiver_name, receiver_phone, receiver_store = _parse_receiver_lines('\n'.join(block2))
    except Exception:
        pass

    # --- 渠道：找 7-11 / 全家 / 萊爾富 等关键字（短格式） ---
    channel = _short_channel(txt)

    # --- 数量：匹配 X 1 ---
    qty = 1
    m_qty = re.search(r'\b[xX]\s*(\d+)\b', txt)
    if m_qty:
        try:
            qty = int(m_qty.group(1))
        except Exception:
            qty = 1

    # --- 商品名：通常在「X 1」上一行附近 ---
    product_name = ""
    if lines:
        qidx = None
        for i, l in enumerate(lines):
            if re.fullmatch(r'[xX]\s*\d+', l) or re.match(r'^[xX]\s*\d+', l):
                qidx = i
                break
        if qidx is not None:
            for j in range(qidx - 1, max(-1, qidx - 8), -1):
                cand = lines[j]
                if not cand:
                    continue
                if any(k in cand for k in ["規格", "规格", "收件地址", "收貨地址", "沒有物流", "没有物流", "訂單金額", "订单金额", "運送", "运送"]):
                    continue
                if re.search(r'\$\s*\d', cand):
                    continue
                if re.fullmatch(r'\d+', cand):
                    continue
                product_name = cand
                break


    # 商品名兜底：很多页面没有 'X 1'，可用『沒有物流資訊』后第一条文本
    if not product_name:
        nolog_idx = None
        for i, l in enumerate(lines):
            if ('沒有物流資訊' in l) or ('没有物流资讯' in l) or ('没有物流資訊' in l):
                nolog_idx = i
                break
        if nolog_idx is not None:
            for j in range(nolog_idx + 1, min(nolog_idx + 12, len(lines))):
                cand = lines[j]
                if not cand:
                    continue
                if cand in ['-', '—', '–']:
                    continue
                if re.fullmatch(r'[0-9][0-9,]*', cand):
                    continue
                if ('訂單金額' in cand) or ('订单金额' in cand):
                    break
                if _short_channel(cand) or ('取貨付款' in cand) or ('取货付款' in cand):
                    continue
                # 过滤明显的状态/导航噪声
                if any(k in cand for k in ['訂單成立','订单成立','待出貨','待取件','待完成','待給評','待给评','感謝您使用','感谢您使用','Yahoo拍賣','Yahoo拍卖','首頁','購物中心','帳務中心','帐务中心','信箱','App下載','搜尋商品','我的拍賣']):
                    continue
                product_name = cand
                break
    # --- 金额：优先「訂單金額」行；否则取 $ 金额最大值 ---
    amount = 0
    m_amt = re.search(r'(?:訂單金額|订单金额)\s*\$?\s*([0-9][0-9,]*)', txt)
    if m_amt:
        try:
            amount = int(m_amt.group(1).replace(",", ""))
        except Exception:
            amount = 0
    else:
        money = []
        for s in re.findall(r'\$\s*([0-9][0-9,]*)', txt):
            try:
                money.append(int(s.replace(",", "")))
            except Exception:
                pass
        if money:
            amount = max(money)

    # 金额兜底：有些页面是『1,260\n訂單金額』，数字在标签前
    if amount == 0 and lines:
        for i, l in enumerate(lines):
            if ('訂單金額' in l) or ('订单金额' in l):
                # 优先取上一行
                if i - 1 >= 0 and re.fullmatch(r'[0-9][0-9,]*', lines[i-1]):
                    try:
                        amount = int(lines[i-1].replace(',', ''))
                        break
                    except Exception:
                        pass
                # 其次取下一行
                if i + 1 < len(lines) and re.fullmatch(r'[0-9][0-9,]*', lines[i+1]):
                    try:
                        amount = int(lines[i+1].replace(',', ''))
                        break
                    except Exception:
                        pass

    return {
        "receiver_name": receiver_name,
        "receiver_phone": receiver_phone,
        "receiver_store": receiver_store,
        "channel": channel,
        "product_name": product_name,
        "qty": qty,
        "amount": amount,
    }

async def _close_modal(page: Page):
    # 右上角 X
    for sel in [
        "button:has-text('×')",
        "button[aria-label='Close']",
        "button[aria-label='关闭']",
        "button[aria-label='關閉']",
    ]:
        try:
            b = await page.query_selector(sel)
            if b:
                await b.click()
                await page.wait_for_timeout(120)
                return
        except Exception:
            continue
    # ESC
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
    except Exception:
        pass


# ------------------------ 对外 API（同步） ------------------------

def export_order_to_excel(
    profile_id: str,
    account_name: Optional[str],
    profile_dir: Path,
    browser_path: Optional[str],
    headless: bool,
    timeout_sec: int,
    template_path: Path,
    output_dir: Path,
    user_code: str,
    owner_name: str,
    orders: List[Dict[str, Any]],
    remark: str = "",
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """同步入口：处理一组订单，并写入 Excel。

    参数 orders:
      [
        {
          "order_no": "1012...",
          "shipments": [
              {"tracking_no": "123...", "product_name": "...", "spec": "...", "pay_mmdd": "0107", "pay_amount": "10"},
              ...
          ]
        },
        ...
      ]

    注意：
    - 输出文件按日期命名：出货资料_YYYYMMDD.xlsx
    - 若当天文件已存在，会在原文件基础上【追加写入】（不会覆盖）
    - 自动清除模板第2行的“示例数据”（避免每次生成都带一条模板行）
    """
    import shutil as _shutil

    user_code = (user_code or "").strip()
    owner_name = (owner_name or "").strip()
    if not user_code:
        raise ValueError("使用者简称不能为空")
    if not owner_name:
        raise ValueError("所属人不能为空")
    if not orders:
        raise ValueError("订单编号不能为空")

    # 规范化 orders 结构，并做基础校验
    orders_norm: List[Dict[str, Any]] = []
    for item in (orders or []):
        order_no = str((item or {}).get("order_no") or "").strip()
        if not order_no:
            continue
        # 备注（可选）：支持每笔订单单独备注；若不提供则走全局 remark
        item_remark = str((item or {}).get("remark") or (item or {}).get("备注") or (item or {}).get("備註") or (item or {}).get("備注") or "").strip()
        # 兼容：若上游只把备注放在 shipments 里（每个快递/采购记录），这里自动合并成“订单级备注”
        if not item_remark:
            _rs = []
            for _s in ((item or {}).get("shipments") or []):
                _s = _s or {}
                _r = str(_s.get("remark") or _s.get("备注") or _s.get("備註") or _s.get("備注") or "").strip()
                if _r and _r not in _rs:
                    _rs.append(_r)
            item_remark = " / ".join(_rs).strip()
        ships_in = (item or {}).get("shipments") or []
        ships: List[Dict[str, str]] = []
        for s in ships_in:
            s = s or {}
            tr = str(s.get("tracking_no") or "").strip()
            if not tr:
                continue
            ships.append({
                "tracking_no": tr,
                "product_name": str(s.get("product_name") or "").strip(),
                "spec": str(s.get("spec") or "").strip(),
                "remark": str(s.get("remark") or s.get("备注") or s.get("備註") or s.get("備注") or "").strip(),
                "pay_mmdd": str(s.get("pay_mmdd") or "").strip(),
                "pay_amount": str(s.get("pay_amount") or "").strip(),
            })
        # 副订单（可选）：用于 Excel 显示，不影响网页抓取/检索（检索永远只用主订单 order_no）
        sub_order_nos = (item or {}).get("sub_order_nos") or (item or {}).get("sub_orders") or (item or {}).get("sub_order_no") or (item or {}).get("sub_order") or []
        if isinstance(sub_order_nos, str):
            _parts = [p.strip() for p in re.split(r"[+，,;\s]+", sub_order_nos) if p.strip()]
            sub_order_nos = _parts
        else:
            try:
                sub_order_nos = [str(x).strip() for x in list(sub_order_nos) if str(x).strip()]
            except Exception:
                sub_order_nos = []
        sub_order_nos = [p for p in sub_order_nos if p and p != order_no]
        order_no_display = str((item or {}).get("order_no_display") or "").strip()
        if not order_no_display:
            order_no_display = order_no + ("+" + "+".join(sub_order_nos) if sub_order_nos else "")

        if not ships:
            raise ValueError(f"订单 {order_no} 未填写快递单号")
        orders_norm.append({"order_no": order_no, "order_no_display": order_no_display, "sub_order_nos": sub_order_nos, "shipments": ships, "remark": item_remark})

    if not orders_norm:
        raise ValueError("订单编号不能为空")

    # 模板可选：允许留空/不存在时，直接由代码生成一个“内置模板”（不改业务逻辑）
    template_path = Path(template_path) if (template_path and str(template_path).strip()) else None
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exec_date = _dt.date.today()
    mmdd = exec_date.strftime("%m%d")

    out_name = f"出货资料_{exec_date.strftime('%Y%m%d')}.xlsx"
    out_path = output_dir / out_name

    def _create_builtin_workbook(target_path: Path) -> None:
        wb0 = openpyxl.Workbook()
        # 删除默认 Sheet，改成我们固定的三个 sheet
        try:
            if "Sheet" in wb0.sheetnames and len(wb0.sheetnames) == 1:
                wb0.remove(wb0["Sheet"])
        except Exception:
            pass

        ws_store0 = _get_sheet(wb0, DEFAULT_SHEET_NAME, create=True, fallback_active=False)
        ws_home0 = _get_sheet(wb0, HOME_SHEET_NAME, create=True, fallback_active=False)
        ws_err0 = _get_sheet(wb0, ERROR_SHEET_NAME, create=True, fallback_active=False)

        _ensure_headers(ws_store0, STORE_HEADERS)
        _ensure_headers(ws_home0, HOME_HEADERS)
        _ensure_headers(ws_err0, ERROR_HEADERS)

        # 外观样式（等线/表头颜色/列宽/冻结/筛选/无边框/居中）
        try:
            _apply_output_formatting(ws_store0, ws_home0)
        except Exception:
            pass

        wb0.save(target_path)

    # 若当天输出文件不存在：优先复制模板生成；若无模板则生成内置模板；若已存在：直接打开追加（避免覆盖）
    if not out_path.exists():
        if template_path and template_path.is_file():
            _shutil.copy2(template_path, out_path)
        else:
            _create_builtin_workbook(out_path)

    # 打开“当天输出文件”（而不是模板）
    wb = openpyxl.load_workbook(out_path)


    
    # 两个业务 sheet（超商 / 宅配）
    # ⚠️ 不要用 wb.active 作为兜底，否则会把宅配单写进「线上贴单资料」或反过来。
    # 若模板缺少对应 sheet，则创建并写入标准表头（与用户模板一致）。
    ws_store = _get_sheet(wb, DEFAULT_SHEET_NAME, create=True, fallback_active=False)
    ws_home = _get_sheet(wb, HOME_SHEET_NAME, create=True, fallback_active=False)

    _ensure_headers(ws_store, STORE_HEADERS)
    _ensure_headers(ws_home, HOME_HEADERS)

    # 模板可能“有表头但缺少备注列”，这里补齐，避免备注写不进去/看起来像消失。
    _ensure_remark_header(ws_store, "备注")
    _ensure_remark_header(ws_home, "備注")

    # 清除模板/旧输出里遗留的 mailto 超链接（Excel 会显示成 mailto:xxx）
    # 只扫描表头+样式行+最近写入的行（避免数据量大时遍历全表拖慢）
    def _strip_mailto(ws, max_scan: int = 50):
        try:
            max_row = ws.max_row or 1
            rows_to_check = set(range(1, min(4, max_row + 1)))
            start = max(1, max_row - max_scan + 1)
            for r in range(start, max_row + 1):
                rows_to_check.add(r)
            for r in sorted(rows_to_check):
                for cell in ws[r]:
                    try:
                        if cell.hyperlink and isinstance(cell.hyperlink.target, str) and cell.hyperlink.target.startswith('mailto:'):
                            cell.hyperlink = None
                    except Exception:
                        pass
        except Exception:
            pass

    _strip_mailto(ws_store)
    _strip_mailto(ws_home)


    hmap_store = _build_header_map(ws_store)
    hmap_home = _build_header_map(ws_home)

    style_row_store = _find_style_row(ws_store)
    style_row_home = _find_style_row(ws_home)

    # 错误订单 sheet（不存在则创建）
    ws_err = _get_sheet(wb, ERROR_SHEET_NAME, create=True)
    _ensure_headers(ws_err, ERROR_HEADERS)

    # --- 清除模板的“示例数据行”（通常在第2行） ---
    def _maybe_clear_template_sample_row(ws, hmap):
        if ws.max_row < 2:
            return
        col_remark = hmap.get("备注")
        col_sys = hmap.get("系统編碼")
        col_acc = hmap.get("賬號")
        try:
            remark_v = ws.cell(row=2, column=col_remark).value if col_remark else None
            sys_v = str(ws.cell(row=2, column=col_sys).value or "").strip() if col_sys else ""
            acc_v = str(ws.cell(row=2, column=col_acc).value or "").strip() if col_acc else ""
        except Exception:
            return
        is_remark_empty = (remark_v is None) or (isinstance(remark_v, str) and not str(remark_v).strip())
        looks_like_order = bool(re.fullmatch(r"\d{10,}", sys_v))
        looks_like_email = ("@" in acc_v) and ("." in acc_v)
        if is_remark_empty and looks_like_order and looks_like_email:
            for c in range(1, ws.max_column + 1):
                ws.cell(row=2, column=c).value = None

    _maybe_clear_template_sample_row(ws_store, hmap_store)
    _maybe_clear_template_sample_row(ws_home, hmap_home)

    # 找“编码”列来生成流水号（若不存在则用第1列）
    col_code_store = hmap_store.get("編碼", 1)
    col_code_home = hmap_home.get("編碼", 1)

    # 预先计算当天的起始流水号，并在本次运行中递增（避免同一批/同一天重复）
    # 预先计算当天的起始流水号，并在本次运行中递增（避免同一批/同一天重复）
    # 需要同时参考「线上贴单资料」与「宅配打包资料」两张表，取较大的 next_serial 作为起点。
    serial_counter_store = _next_serial_for_day(ws_store, col_code=col_code_store, user_code=user_code, mmdd=mmdd)
    serial_counter_home = _next_serial_for_day(ws_home, col_code=col_code_home, user_code=user_code, mmdd=mmdd)
    serial_counter = max(serial_counter_store, serial_counter_home)

    def _make_row_helpers(ws, hmap, style_row):
        def _find_next_write_row() -> int:
            # 优先用关键列判断“空行”。
            # ⚠️ 重要：多快递模式下，后续行会只写「国内快递单号/商品信息」，
            # 若这里不把「国内快递单号」纳入判断，会被误判为空行而被覆盖。
            cols = []
            for k in ("系统編碼", "編碼", "賬號", "国内快递单号"):
                c = hmap.get(k)
                if c and c not in cols:
                    cols.append(c)
            if not cols:
                cols = list(range(1, ws.max_column + 1))
            for r in range(2, ws.max_row + 1):
                if all(_is_cell_empty(ws.cell(row=r, column=c)) for c in cols):
                    return r
            return ws.max_row + 1

        def _prepare_row_for_write(r: int) -> int:
            if r > ws.max_row:
                ws.append([None] * ws.max_column)
            # 行高统一
            try:
                ws.row_dimensions[r].height = float(FORMAT_ROW_HEIGHT)
            except Exception:
                pass
            for col in range(1, ws.max_column + 1):
                dst = ws.cell(row=r, column=col)
                if style_row and style_row <= ws.max_row and style_row != r:
                    src = ws.cell(row=style_row, column=col)
                    _copy_cell_style(src, dst)
                dst.value = None
                try:
                    dst.hyperlink = None
                except Exception:
                    pass
            return r

        return _find_next_write_row, _prepare_row_for_write

    _find_next_store_row, _prep_store_row = _make_row_helpers(ws_store, hmap_store, style_row_store)
    _find_next_home_row, _prep_home_row = _make_row_helpers(ws_home, hmap_home, style_row_home)

    results: List[Dict[str, Any]] = []

    # ── 共用写 Excel 函数 ──────────────────────────────────
    def _write_result_to_excel(result: Dict[str, Any], item: Dict[str, Any]) -> None:
        """把一条订单结果写入对应的 Excel sheet。"""
        nonlocal serial_counter

        order_no = result["order_no"]
        order_no_display = result.get("order_no_display", order_no)
        shipments = list(item.get("shipments") or [])
        cur_remark = str(item.get("remark") or "").strip() or str(remark or "").strip()

        exec_code = f"{user_code}{mmdd}{serial_counter:02d}"
        serial_counter += 1
        result["exec_code"] = exec_code

        is_home = (result.get("channel") == "黑貓")
        ws_t = ws_home if is_home else ws_store
        hmap_t = hmap_home if is_home else hmap_store
        find_row = _find_next_home_row if is_home else _find_next_store_row
        prep_row = _prep_home_row if is_home else _prep_store_row
        row = find_row()
        row = prep_row(row)

        def set_if(col_key: str, value):
            c = hmap_t.get(col_key)
            if not c:
                return
            cell = ws_t.cell(row=row, column=c)
            cell.value = value
            if col_key in ("日期", "代付日期"):
                try:
                    cell.number_format = FORMAT_DATE_NF
                except Exception:
                    pass
            try:
                cell.hyperlink = None
            except Exception:
                pass

        set_if("編碼", exec_code)
        set_if("賬號", (account_name or profile_id))
        set_if("日期", _dt.datetime(exec_date.year, exec_date.month, exec_date.day))
        set_if("系统編碼", order_no_display)
        set_if("所屬人", owner_name)
        sh0 = shipments[0]
        set_if("国内快递单号", sh0.get("tracking_no", ""))
        set_if("收件人", result["receiver_name"])
        set_if("门市/店", result["receiver_store"])
        set_if("商品名稱", sh0.get("product_name", ""))
        set_if("规格", sh0.get("spec", ""))
        set_if("渠道", result["channel"])
        set_if("金额", result["amount"])
        pay_mmdd0 = str(sh0.get("pay_mmdd") or "").strip()
        if re.fullmatch(r"\d{3,4}", pay_mmdd0):
            try:
                d0 = _parse_mmdd_to_date(pay_mmdd0, exec_date)
                if d0:
                    set_if("代付日期", _dt.datetime(d0.year, d0.month, d0.day))
            except Exception:
                pass
        parts = []
        for sh in shipments:
            p = str(sh.get("pay_amount") or "").strip()
            if p:
                parts.append(p)
        if parts:
            set_if("商品成本", "+".join(parts))
        set_if("商品數量", result["qty"])
        set_if("聯係電話", result["receiver_phone"])
        set_if("备注", (cur_remark or "(檢查出貨）加强包裝，氣泡氣泡 ,氣泡氣泡"))

        if len(shipments) > 1:
            for sh in shipments[1:]:
                row2 = find_row()
                row2 = prep_row(row2)
                def _set2(col_key: str, value, _r=row2):
                    c2 = hmap_t.get(col_key)
                    if not c2:
                        return
                    cell2 = ws_t.cell(row=_r, column=c2)
                    cell2.value = value
                    if col_key in ("日期", "代付日期"):
                        try:
                            cell2.number_format = FORMAT_DATE_NF
                        except Exception:
                            pass
                    try:
                        cell2.hyperlink = None
                    except Exception:
                        pass
                _set2("国内快递单号", sh.get("tracking_no", ""))
                _set2("商品名稱", sh.get("product_name", ""))
                _set2("规格", sh.get("spec", ""))
                sh_mmdd = str(sh.get("pay_mmdd") or "").strip()
                if re.fullmatch(r"\d{3,4}", sh_mmdd):
                    try:
                        d_sh = _parse_mmdd_to_date(sh_mmdd, exec_date)
                        if d_sh:
                            _set2("代付日期", _dt.datetime(d_sh.year, d_sh.month, d_sh.day))
                    except Exception:
                        pass
                _set2("备注", (cur_remark or "(檢查出貨）加强包裝，氣泡氣泡 ,氣泡氣泡"))

        if on_log:
            on_log(f"[SHIP] 写入完成：{order_no} -> {exec_code} 金额={result['amount']} 渠道={result['channel']}")

    # ── Phase 1: HTTP 优先获取订单数据（不启动浏览器）──────────
    _http_done: set = set()  # 已通过 HTTP 成功的订单号
    if on_log:
        on_log("[SHIP] Phase 1: HTTP API 获取订单数据...")

    for item in orders_norm:
        if stop_event is not None and getattr(stop_event, "is_set", None) and stop_event.is_set():
            break

        order_no = str(item.get("order_no") or "").strip()
        shipments = list(item.get("shipments") or [])
        sub_order_nos = item.get("sub_order_nos") or []
        if isinstance(sub_order_nos, str):
            sub_order_nos = [p.strip() for p in re.split(r"[+，,;\s]+", sub_order_nos) if p.strip()]
        else:
            try:
                sub_order_nos = [str(x).strip() for x in list(sub_order_nos) if str(x).strip()]
            except Exception:
                sub_order_nos = []
        sub_order_nos = [p for p in sub_order_nos if p and p != order_no]
        order_no_display = str(item.get("order_no_display") or "").strip()
        if not order_no_display:
            order_no_display = order_no + ("+" + "+".join(sub_order_nos) if sub_order_nos else "")
        if not order_no or not shipments:
            continue

        http_data = _fetch_order_detail_http(
            profile_dir, order_no,
            chrome_path=browser_path or "",
            headless=headless,
            on_log=on_log,
        )
        if not http_data:
            continue  # HTTP 失败，留给 Phase 2

        result: Dict[str, Any] = {
            "order_no": order_no,
            "order_no_display": order_no_display,
            "sub_order_nos": sub_order_nos,
            "found": True,
            "exec_code": "",
            "amount": 0,
            "channel": str(http_data.get("channel") or ""),
            "receiver_name": str(http_data.get("receiver_name") or ""),
            "receiver_phone": str(http_data.get("receiver_phone") or ""),
            "receiver_store": str(http_data.get("receiver_store") or ""),
            "product_name": str(http_data.get("product_name") or ""),
            "qty": int(http_data.get("qty") or 1),
            "error": "",
        }
        main_amount = int(http_data.get("amount") or 0)

        # 副订单金额
        sub_amount_parts = []
        if sub_order_nos:
            _seen = set()
            _uniq = []
            for _x in sub_order_nos:
                _x = str(_x or "").strip()
                if not _x or _x == order_no or _x in _seen:
                    continue
                _seen.add(_x)
                _uniq.append(_x)
            for sub_no in _uniq:
                if stop_event and stop_event.is_set():
                    break
                sub_data = _fetch_order_detail_http(
                    profile_dir, sub_no,
                    chrome_path=browser_path or "",
                    headless=headless,
                    on_log=on_log,
                )
                if sub_data:
                    sub_amount_parts.append(str(int(sub_data.get("amount") or 0)))
                else:
                    sub_amount_parts.append("?")

        if sub_amount_parts:
            result["amount"] = "+".join([str(main_amount)] + sub_amount_parts)
        else:
            result["amount"] = main_amount

        try:
            _write_result_to_excel(result, item)
            results.append(result)
            _http_done.add(order_no)
            if on_log:
                on_log(f"[SHIP] HTTP 成功: {order_no}")
        except Exception as e:
            if on_log:
                on_log(f"[SHIP] HTTP 路径写 Excel 失败: {order_no} - {e}")

    # ── HTTP 失败的订单标记为失败 (Playwright 退回已移除) ──
    _remaining = [item for item in orders_norm
                  if str(item.get("order_no") or "").strip() not in _http_done]

    if _remaining:
        if on_log:
            on_log(f"[SHIP] HTTP 失败 {len(_remaining)} 单,直接记为失败 (不退回 Playwright)")
        _done_orders = {r.get("order_no") for r in results}
        for _item in _remaining:
            _ono = str(_item.get("order_no") or "").strip()
            if _ono and _ono not in _done_orders:
                results.append({
                    "order_no": _ono,
                    "order_no_display": str(_item.get("order_no_display") or _ono),
                    "sub_order_nos": _item.get("sub_order_nos") or [],
                    "found": False,
                    "exec_code": "",
                    "amount": 0,
                    "channel": "",
                    "receiver_name": "",
                    "receiver_phone": "",
                    "receiver_store": "",
                    "product_name": "",
                    "qty": 1,
                    "error": "HTTP 失败 (wssid 失效或订单不存在)",
                })
    else:
        if on_log:
            on_log(f"[SHIP] 全部 {len(_http_done)} 单通过 HTTP 完成")

    # --- 写入「错误订单」sheet（找不到/失败） ---
    try:
        # 初始化表头
        if ws_err.max_row < 1 or all(_is_cell_empty(ws_err.cell(row=1, column=c)) for c in range(1, 4)):
            ws_err.cell(row=1, column=1).value = "订单编号"
            ws_err.cell(row=1, column=2).value = "账号"
            ws_err.cell(row=1, column=3).value = "错误原因"
            ws_err.cell(row=1, column=4).value = "时间"

        next_r = ws_err.max_row + 1
        for r in results:
            if r.get("found"):
                continue
            ws_err.cell(row=next_r, column=1).value = (r.get("order_no_display") or r.get("order_no") or "")
            ws_err.cell(row=next_r, column=2).value = (account_name or profile_id)
            ws_err.cell(row=next_r, column=3).value = r.get("error", "未知错误")
            ws_err.cell(row=next_r, column=4).value = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            next_r += 1

        if any((not r.get("found")) for r in results):
            if on_log:
                on_log(f"[SHIP] 本次有 {sum(1 for r in results if not r.get('found'))} 条失败/找不到的订单，已写入 Sheet「{ERROR_SHEET_NAME}」")
    except Exception as e:
        if on_log:
            on_log(f"[SHIP] 写入错误订单 sheet 失败：{e}")

# 输出外观：按你要求统一（不改功能/数据）
    try:
        _apply_output_formatting(ws_store, ws_home)
    except Exception:
        pass

# 保存（追加写入）到当天输出文件
    # 如果文件被 Excel 占用，先尝试强制关闭占用进程
    _save_ok = False
    for _attempt in range(3):
        try:
            wb.save(out_path)
            _save_ok = True
            break
        except PermissionError:
            if _attempt == 0:
                # 首次失败：尝试关闭占用该文件的 Excel 进程
                try:
                    import subprocess as _sp
                    _sp.run(
                        'taskkill /F /IM EXCEL.EXE',
                        shell=True, timeout=10,
                        capture_output=True,
                    )
                    if on_log:
                        on_log(f"[SHIP] 文件被占用，已关闭 Excel，重试保存...")
                except Exception:
                    pass
                import time as _t
                _t.sleep(2)
            else:
                if on_log:
                    on_log(f"[SHIP] 文件仍被占用（第{_attempt+1}次），等待3秒后重试...")
                import time as _t
                _t.sleep(3)
        except Exception as _save_err:
            if on_log:
                on_log(f"[SHIP] 保存失败：{_save_err}")
            break

    if _save_ok:
        if on_log:
            on_log(f"[SHIP] 输出文件已生成：{out_path}")
        # 自动同步业绩到 D1（后台线程，不阻塞）
        # 修复：
        #   1. sleep 2 秒让 openpyxl 释放文件锁，避免 parse 时读失败
        #   2. upload 失败的文件加入重试队列（performance_retry_queue.json），下次启动软件补传
        try:
            import threading as _th
            import time as _t_sync
            def _auto_sync_perf():
                try:
                    _t_sync.sleep(2)  # 等 openpyxl 释放文件锁
                    from core.performance_feature import (
                        parse_shipping_excel, upload_records, UploadError, enqueue_failed_upload,
                    )
                    recs = parse_shipping_excel(out_path, log=on_log)
                    if not recs:
                        if on_log:
                            on_log(f"[业绩] {out_path.name} 无记录可同步")
                        return
                    try:
                        ins, skp, rej = upload_records(recs, log=on_log, raise_on_failure=True)
                        if on_log:
                            on_log(f"[业绩] 自动同步 {out_path.name}：新增 {ins}，已存在 {skp}，拒收 {rej}")
                    except UploadError as _ue:
                        # 真实网络/Worker 失败 → 入重试队列
                        enqueue_failed_upload(str(out_path))
                        if on_log:
                            on_log(f"[业绩] 自动同步失败 {out_path.name}：{_ue} — 已加入重试队列")
                except Exception as _e:
                    # parse 失败或其他异常：也入队（parse 失败可能因为文件还被占用）
                    try:
                        from core.performance_feature import enqueue_failed_upload as _eq
                        _eq(str(out_path))
                    except Exception:
                        pass
                    if on_log:
                        on_log(f"[业绩] 自动同步异常 {out_path.name}：{_e} — 已加入重试队列")
            _th.Thread(target=_auto_sync_perf, daemon=True).start()
        except Exception:
            pass
    else:
        if on_log:
            on_log(f"[SHIP] 保存失败，请关闭 Excel 文件 {out_path.name} 后重试")

    return results