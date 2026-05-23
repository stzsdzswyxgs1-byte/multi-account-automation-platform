"""速达集运(suda68.com) HTTP API 模块 — 纯 HTTP 操作，不需要浏览器。

认证方式：
  POST /Account/Login 获取 ASP.NET Session cookie。
  所有后续请求携带 session cookie。
  Session 过期时自动重新登录。

覆盖功能：
  1. 登录 / Session 管理
  2. 包裹列表查询（检查入库状态）
  3. 收货地址管理（查询/新增）
  4. 运费试算 (SumOrder)
  5. 提交集运订单 (SubmitOrder)
  6. 订单列表查询
  7. 取消订单

关键常量 (从 suda68.com 分析确认):
  WareHouse=20344 (日本大阪仓), Timer=10538 (空运),
  Carrier=21667 (日本空运), Country=11962 (台湾)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
import core.ssl_helper  # noqa: F401 — SSL 容错

_log_mod = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════════════════════╗
# ║  常量                                                       ║
# ╚══════════════════════════════════════════════════════════════╝

SUDA_BASE = "http://www.suda68.com"

# 默认参数（可通过 settings.json 覆盖）
DEFAULT_WAREHOUSE = 20344      # 日本大阪仓
DEFAULT_TIMER = 10538          # 空运时效
DEFAULT_CARRIER = 21667        # 日本空运承运商
DEFAULT_COUNTRY = 11962        # 台湾
DEFAULT_CARD = "93379696"      # 固定证件号码
DEFAULT_IS_TAX = True          # 包税
DEFAULT_GOODS_TYPE = 10212     # 普货

# Session 缓存
_SESSION_CACHE = (
    Path(__file__).resolve().parent.parent
    / "profiles" / "_suda68" / "session_cache.json"
)
_SESSION_MAX_AGE = 3600  # 1 小时（保守，ASP.NET Session 通常20分钟）

LogFn = Callable[[str], None]


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据结构                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

@dataclass
class SudaPackage:
    """速达仓库中的一个包裹。"""
    package_id: str = ""           # 速达包裹 ID (data-id)
    bill_code: str = ""            # 快递单号 (tracking number)
    warehouse: str = ""            # 仓库名称
    warehouse_id: int = 0          # 仓库 ID
    status_name: str = ""          # 状态名称 (可申请/已打包/...)
    goods_name: str = ""           # 品名
    goods_type: int = 0            # 货物类型 ID
    goods_type_name: str = ""      # 货物类型名称
    goods_count: int = 1           # 数量
    goods_price: float = 0.0       # 单价
    goods_money: float = 0.0       # 总价
    weight: float = 0.0            # 实际重量 (kg)
    volume_weight: float = 0.0     # 体积重量 (kg)
    remark: str = ""               # 备注
    storage_time: str = ""         # 入仓时间


@dataclass
class SudaAddress:
    """速达收货地址。"""
    address_id: int = 0
    person: str = ""               # 收件人
    phone: str = ""                # 电话
    address: str = ""              # 完整地址
    country_id: int = 0
    province_id: int = 0
    city_id: int = 0
    area_id: int = 0
    agent_id: int = 0              # 自提点


@dataclass
class SudaOrderResult:
    """提交集运的结果。"""
    success: bool = False
    order_id: str = ""
    order_code: str = ""           # 主单号
    receivables: float = 0.0       # 应收金额
    error: str = ""


@dataclass
class SudaFreightInfo:
    """运费试算结果。"""
    freight: float = 0.0           # 运费
    weight: float = 0.0            # 计费重量
    unit: str = ""                 # 重量单位
    storage_charges: float = 0.0   # 仓租费
    length_charges: float = 0.0    # 超长费
    tax: float = 0.0               # 包税费
    surcharge: float = 0.0         # 附加费
    coupon: float = 0.0            # 优惠券抵扣
    total: float = 0.0             # 总计


@dataclass
class SudaTrackingEvent:
    """物流时间线中的一条事件。"""
    status: str = ""               # 原始状态 ("在途"/"揽件"/"派件"/"签收")
    detail: str = ""               # 完整描述 (e.g. "已核重·集运仓发货")
    timestamp: str = ""            # 时间


@dataclass
class SudaTrackingResult:
    """物流跟踪查询结果。"""
    order_id: str = ""
    order_code: str = ""           # 主单号 / 发货主号
    latest_status: str = ""        # 归一化: 已发货/已集货/配送中/已签收
    tracking_code: str = ""        # 发货主号 (台湾配送单号)
    events: List[SudaTrackingEvent] = field(default_factory=list)
    error: str = ""


# ╔══════════════════════════════════════════════════════════════╗
# ║  内部辅助                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(f"[JP] {msg}")
    else:
        _log_mod.info("[JP] %s", msg)


def _save_session_cookies(cookies: dict) -> bool:
    """保存 session cookies 到本地缓存。"""
    try:
        _SESSION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cookies": cookies,
            "saved_at": time.time(),
            "saved_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _SESSION_CACHE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception:
        return False


def _load_session_cookies(max_age: float = _SESSION_MAX_AGE) -> dict:
    """从缓存加载 session cookies。返回空 dict 表示无效/过期。"""
    if not _SESSION_CACHE.exists():
        return {}
    try:
        data = json.loads(_SESSION_CACHE.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at", 0)
        if time.time() - saved_at > max_age:
            return {}
        return data.get("cookies", {})
    except Exception:
        return {}


def _invalidate_session():
    """标记 session 缓存失效。"""
    try:
        if _SESSION_CACHE.exists():
            _SESSION_CACHE.unlink()
    except Exception:
        pass


# ╔══════════════════════════════════════════════════════════════╗
# ║  Session 管理                                               ║
# ╚══════════════════════════════════════════════════════════════╝

def _create_session() -> requests.Session:
    """创建 requests Session，带基本 headers。"""
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    })
    return s


def login(
    username: str,
    password: str,
    log: Optional[LogFn] = None,
) -> Tuple[requests.Session, str]:
    """登录速达集运，返回 (session, error)。

    成功: (session, "")
    失败: (None, error_message)
    """
    s = _create_session()
    try:
        # Step 1: 获取登录页 CSRF token
        r = s.get(f"{SUDA_BASE}/Account/Login", timeout=30)
        tokens = re.findall(
            r'name="__RequestVerificationToken"[^>]*value="([^"]*)"', r.text
        )
        if not tokens:
            tokens = re.findall(
                r'value="([^"]*)"[^>]*name="__RequestVerificationToken"', r.text
            )
        csrf_token = tokens[0] if tokens else ""
        if not csrf_token:
            return None, "无法获取 CSRF token"

        # Step 2: POST 登录
        r = s.post(
            f"{SUDA_BASE}/Account/Login",
            data={
                "__RequestVerificationToken": csrf_token,
                "Uid": username,
                "Pwd": password,
            },
            allow_redirects=True,
            timeout=30,
        )

        # 检查是否成功（登录失败会停在 /Account/Login）
        if "Login" in r.url and "Account" in r.url:
            return None, "登录失败：用户名或密码错误"

        # Step 3: 缓存 session cookies
        cookie_dict = dict(s.cookies)
        _save_session_cookies(cookie_dict)
        _log(log, f"登录成功 (user={username})")
        return s, ""

    except Exception as e:
        return None, f"登录异常: {e}"


def ensure_session(
    username: str,
    password: str,
    log: Optional[LogFn] = None,
) -> Tuple[requests.Session, str]:
    """确保有效 session — 先尝试缓存，失败则重新登录。

    返回 (session, error)。
    """
    # 尝试加载缓存
    cached = _load_session_cookies()
    if cached:
        s = _create_session()
        s.cookies.update(cached)
        # 验证 session 有效性（访问任意页面看是否跳转登录）
        try:
            r = s.get(
                f"{SUDA_BASE}/Home/Package",
                allow_redirects=False,
                timeout=20,
            )
            if r.status_code == 200:
                _log(log, "使用缓存 session")
                return s, ""
            # 302 跳转到登录页 = session 过期
        except Exception:
            pass
        _invalidate_session()

    # 重新登录
    return login(username, password, log)


# ╔══════════════════════════════════════════════════════════════╗
# ║  包裹列表 — 检查入库                                        ║
# ╚══════════════════════════════════════════════════════════════╝

def get_package_list(
    session: requests.Session,
    warehouse_id: int = 0,
    log: Optional[LogFn] = None,
) -> Tuple[List[SudaPackage], str]:
    """获取包裹列表（通过解析 HTML 页面）。

    Args:
        session: 已登录的 session
        warehouse_id: 筛选仓库 ID，0 = 全部

    Returns:
        (packages, error)
    """
    try:
        url = f"{SUDA_BASE}/Home/Package"
        if warehouse_id:
            url += f"?warehouseId={warehouse_id}"
        r = session.get(url, timeout=30)
        if "Login" in r.url and "Account" in r.url:
            return [], "session 已过期"
        return _parse_package_html(r.text), ""
    except Exception as e:
        return [], f"获取包裹列表失败: {e}"


def _parse_package_html(html: str) -> List[SudaPackage]:
    """从 /Home/Package HTML 解析包裹列表。"""
    packages = []

    # 用 my_edit li 提取结构化数据（包含所有关键字段）
    pattern = (
        r'class="my_edit cur"[^>]*'
        r'data-id="(\d+)"[^>]*'
        r'data-StorageName="([^"]*)"[^>]*'
        r'data-billcode="([^"]*)"[^>]*'
        r'data-GoodsName="([^"]*)"[^>]*'
        r'data-GoodsType="(\d+)"[^>]*'
        r'data-GoodsTypeName="([^"]*)"[^>]*'
        r'data-GoodsCount="(\d+)"[^>]*'
        r'data-GoodsPrice="([^"]*)"[^>]*'
        r'data-GoodsMoney="([^"]*)"[^>]*'
        r'data-Rem="([^"]*)"'
    )
    for m in re.finditer(pattern, html):
        pkg = SudaPackage(
            package_id=m.group(1),
            bill_code=m.group(3),
            status_name=m.group(2),
            goods_name=m.group(4),
            goods_type=int(m.group(5)),
            goods_type_name=m.group(6),
            goods_count=int(m.group(7)),
            goods_price=float(m.group(8) or 0),
            goods_money=float(m.group(9) or 0),
            remark=m.group(10),
        )

        # 从 checkbox 提取 weight 和 warehouse
        chk_pattern = (
            rf'data-id="{pkg.package_id}"[^>]*'
            r'data-weight="([^"]*)"[^>]*'
            r'data-ware="(\d+)"'
        )
        chk_match = re.search(chk_pattern, html)
        if not chk_match:
            # 尝试反序
            chk_pattern2 = (
                rf'data-weight="([^"]*)"[^>]*'
                rf'data-ware="(\d+)"[^>]*'
                rf'data-id="{pkg.package_id}"'
            )
            chk_match = re.search(chk_pattern2, html)
        if chk_match:
            pkg.weight = float(chk_match.group(1) or 0)
            pkg.warehouse_id = int(chk_match.group(2))

        # VolumeWeight
        vol_pattern = rf'data-id="{pkg.package_id}"[^>]*data-VolumeWeight="([^"]*)"'
        vol_match = re.search(vol_pattern, html)
        if vol_match:
            pkg.volume_weight = float(vol_match.group(1) or 0)

        # 入仓时间
        time_pattern = rf'\[.*?\]\s*入仓时间:(\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})'
        # 找到该包裹 ID 附近的入仓时间
        pkg_section_pattern = rf'data-id="{pkg.package_id}".*?入仓时间:(\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})'
        time_match = re.search(pkg_section_pattern, html, re.DOTALL)
        if time_match:
            pkg.storage_time = time_match.group(1)

        # 仓库名称
        ware_name_pattern = rf'data-id="{pkg.package_id}".*?title="([^"]*仓)"'
        ware_match = re.search(ware_name_pattern, html, re.DOTALL)
        if ware_match:
            pkg.warehouse = ware_match.group(1)

        packages.append(pkg)

    return packages


def find_package_by_tracking(
    session: requests.Session,
    tracking_no: str,
    warehouse_id: int = 0,
    log: Optional[LogFn] = None,
) -> Tuple[Optional[SudaPackage], str]:
    """用快递单号在速达仓库中查找包裹。

    Returns:
        (package, error) — package 为 None 表示未找到
    """
    packages, err = get_package_list(session, warehouse_id, log)
    if err:
        return None, err
    for pkg in packages:
        if pkg.bill_code == tracking_no:
            _log(log, f"找到包裹: {tracking_no} → ID={pkg.package_id} 状态={pkg.status_name}")
            return pkg, ""
    return None, ""


# ╔══════════════════════════════════════════════════════════════╗
# ║  收货地址管理                                               ║
# ╚══════════════════════════════════════════════════════════════╝

def get_address_list(
    session: requests.Session,
    log: Optional[LogFn] = None,
) -> Tuple[List[SudaAddress], str]:
    """获取收货地址列表（从 Me/Address 页面解析）。"""
    try:
        r = session.get(f"{SUDA_BASE}/Me/Address", timeout=30)
        if "Login" in r.url and "Account" in r.url:
            return [], "session 已过期"
        return _parse_address_page_html(r.text), ""
    except Exception as e:
        return [], f"获取地址列表失败: {e}"


def _parse_address_page_html(html: str) -> List[SudaAddress]:
    """从 Me/Address 页面 HTML 解析地址列表。

    HTML 结构: 每个地址块有 edit/del span with data-id,
    以及多个 li_group div 包含联系人、手机、地址等信息。
    """
    addresses = []

    # 每个地址有 edit 和 del buttons with data-id
    # 找到所有 edit data-id
    for m in re.finditer(
        r"class='edit'\s+data-id=\"(\d+)\"", html
    ):
        addr_id = int(m.group(1))
        # 向前搜索地址块内容 (在当前 match 前面最近的 addres_group)
        block_end = m.start()
        # 找这个地址块的开头
        block_start = html.rfind('class="addres_group', 0, block_end)
        if block_start < 0:
            continue
        block = html[block_start:block_end + 200]

        addr = SudaAddress(address_id=addr_id)

        # 提取 title 属性中的值
        titles = re.findall(r'title="([^"]*)"', block)
        # 典型的 title 顺序:
        # [full_address, receiver_name, phone, surcharge, card, postal_code]
        for t in titles:
            t = t.strip()
            if not t:
                continue
            # 识别字段类型
            if re.match(r'^0\d{8,10}$', t):
                addr.phone = t
            elif re.match(r'^\d{3,6}$', t) and len(t) <= 6:
                pass  # postal code or surcharge
            elif re.match(r'^\d+\.\d{2}$', t):
                pass  # surcharge
            elif re.match(r'^\d{6,}$', t):
                pass  # card number
            elif len(t) > 8 and ('市' in t or '區' in t or '路' in t
                                 or '號' in t or '号' in t or '街' in t):
                addr.address = t
            elif not addr.person and len(t) >= 2 and len(t) <= 10:
                addr.person = t

        addresses.append(addr)

    return addresses


def add_address(
    session: requests.Session,
    person: str,
    phone: str,
    country_id: int,
    province_id: int,
    city_id: int,
    area_id: int,
    address: str,
    card: str = DEFAULT_CARD,
    post_code: str = "",
    log: Optional[LogFn] = None,
) -> Tuple[int, str]:
    """新增收货地址。返回 (address_id, error)。"""
    try:
        r = session.post(
            f"{SUDA_BASE}/Data/AddClientAddress",
            data={
                "ID": 0,
                "Type": 0,
                "Countryid": country_id,
                "Provinceid": province_id,
                "Cityid": city_id,
                "Areaid": area_id,
                "Address": address,
                "PostCode": post_code,
                "LinkName": person,
                "LinkPhone": phone,
                "LinkTel": "",
                "LinkCardId": card,
                "Agentid": 0,
                "LinkEmeli": "",
            },
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            addr_id = data.get("Data", 0)
            _log(log, f"新增地址成功: ID={addr_id} {person}")
            return addr_id, ""
        return 0, data.get("Msg", "新增地址失败")
    except Exception as e:
        return 0, f"新增地址异常: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  运费试算                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

def sum_order(
    session: requests.Session,
    package_ids: List[str],
    address_id: int,
    warehouse_id: int = DEFAULT_WAREHOUSE,
    timer: int = DEFAULT_TIMER,
    carrier: int = DEFAULT_CARRIER,
    country_id: int = DEFAULT_COUNTRY,
    province_id: int = 0,
    city_id: int = 0,
    agent_id: int = 0,
    goods_type: int = DEFAULT_GOODS_TYPE,
    is_tax: bool = DEFAULT_IS_TAX,
    pay_type: int = 2,
    log: Optional[LogFn] = None,
) -> Tuple[Optional[SudaFreightInfo], str]:
    """运费试算。

    Args:
        package_ids: 包裹 ID 列表
        address_id: 收货地址 ID
        其他参数: 速达系统配置

    Returns:
        (freight_info, error)
    """
    # 注意: 调用 SumOrder 前必须先加载 Step 页面 (在 submit_order 中完成)
    # 如果独立调用, 需要先 GET /Home/Step?WareHouse=X&BillCodeList=Y
    try:
        r = session.post(
            f"{SUDA_BASE}/Data/SumOrder",
            data={
                "WareHouse": warehouse_id,
                "Timer": timer,
                "Carrier": carrier,
                "AddressID": address_id,
                "Agent": agent_id,
                "Country": country_id,
                "Province": province_id,
                "City": city_id,
                "Baojia": 0,
                "DSHK": 0,
                "IsTax": "true" if is_tax else "false",
                "Integral": 0,
                "Coupon": 0,
                "GoodsType": goods_type,
                "PayType": pay_type,
                "AllMoney": 0,
                "ServiceIDs": "",
            },
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            calc = json.loads(data.get("ReturnJson", "{}"))
            fare = calc.get("CalculatedPrice", {}).get("ShopFare", {})
            info = SudaFreightInfo(
                freight=fare.get("Freight", 0),
                weight=fare.get("Weight", 0),
                unit=fare.get("Unit", ""),
                storage_charges=fare.get("StorageCharges", 0),
                length_charges=fare.get("LengthCharges", 0),
                tax=fare.get("Tax", 0),
                surcharge=fare.get("Surcharge", 0),
                coupon=fare.get("Coupon", 0),
                total=fare.get("Freight", 0),
            )
            # 总计 = 运费 + 仓租 + 超长 + 包税 + 附加 - 优惠券
            info.total = (
                info.freight + info.storage_charges + info.length_charges
                + info.tax + info.surcharge - info.coupon
            )
            _log(log, f"运费试算: {info.freight} + 附加={info.surcharge} 总计={info.total}")
            return info, ""
        return None, data.get("Msg", "运费试算失败")
    except Exception as e:
        return None, f"运费试算异常: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  提交集运订单                                               ║
# ╚══════════════════════════════════════════════════════════════╝

def submit_order(
    session: requests.Session,
    package_ids: List[str],
    address_id: int,
    receiver_name: str = "",
    receiver_phone: str = "",
    receiver_address: str = "",
    country_id: int = DEFAULT_COUNTRY,
    province_id: int = 0,
    city_id: int = 0,
    agent_id: int = 0,
    warehouse_id: int = DEFAULT_WAREHOUSE,
    timer: int = DEFAULT_TIMER,
    carrier: int = DEFAULT_CARRIER,
    goods_type: int = DEFAULT_GOODS_TYPE,
    is_tax: bool = DEFAULT_IS_TAX,
    pay_type: int = 2,
    goods_name: str = "",
    goods_name_en: str = "",
    account: int = 1,
    price: float = 10.0,
    all_money: float = 10.0,
    remark: str = "",
    country_name: str = "台湾",
    card: str = DEFAULT_CARD,
    store_number: str = "",
    store_name: str = "",
    insured: int = 0,
    collection: int = 0,
    integral: int = 0,
    coupon: int = 0,
    log: Optional[LogFn] = None,
) -> SudaOrderResult:
    """提交集运订单。

    重要: 必须先加载 Step 页面（服务端session绑定包裹），
    然后调用 SumOrder 试算运费，最后 SubmitOrder 提交。

    Args:
        package_ids: 要出货的包裹 ID 列表
        address_id: 收货地址 ID
        receiver_name: 收件人姓名 (用于 packaddress)
        receiver_phone: 收件人电话 (用于 packaddress)
        receiver_address: 收件人地址文字 (用于 packaddress)
        其他参数: 速达系统需要的各种字段

    Returns:
        SudaOrderResult
    """
    result = SudaOrderResult()

    try:
        # 1. 加载 Step 页面 — 服务端在 session 中绑定包裹列表
        bill_code_list = ",".join(package_ids) + ","
        step_url = (
            f"{SUDA_BASE}/Home/Step"
            f"?WareHouse={warehouse_id}&BillCodeList={bill_code_list}"
        )
        r_step = session.get(step_url, timeout=30)
        if "Login" in r_step.url and "Account" in r_step.url:
            result.error = "session 已过期"
            return result
        if r_step.status_code != 200 or len(r_step.text) < 500:
            result.error = "加载 Step 页面失败"
            return result

        # 2. SumOrder 试算运费 (服务端需要先 SumOrder 才能 SubmitOrder)
        #    注意: 不含 ServiceIDs/ServiceRems — 空字符串会导致服务端 NullReferenceException
        session.post(
            f"{SUDA_BASE}/Data/SumOrder",
            data={
                "WareHouse": warehouse_id,
                "Timer": timer,
                "Carrier": carrier,
                "AddressID": address_id,
                "Agent": agent_id,
                "Country": country_id,
                "Province": province_id,
                "City": city_id,
                "Baojia": 0,
                "DSHK": 0,
                "IsTax": "true" if is_tax else "false",
                "Integral": 0,
                "Coupon": 0,
                "GoodsType": goods_type,
                "PayType": pay_type,
                "AllMoney": 0,
            },
            timeout=30,
        )

        # 3. 构造 packaddress JSON (收件人信息)
        pack_array = [{
            "PackageNo": 1,
            "ReceiveName": receiver_name,
            "ReceivePhoneNo": receiver_phone,
            "ReceiveAddress": receiver_address,
            "ReceiveAddresss": country_name,
            "Collection": collection,
        }]

        # 4. 提交订单 (注意: 不能包含 ServiceIDs/ServiceRems 空字段)
        post_data = {
            "WareHouse": warehouse_id,
            "timer": timer,
            "carrier": carrier,
            "address": address_id,
            "Country": country_id,
            "Province": province_id,
            "City": city_id,
            "agent": agent_id,
            "Insured": insured,
            "Collection": collection,
            "IsTax": is_tax,
            "Integral": integral,
            "Coupon": coupon,
            "PayType": pay_type,
            "packaddress": json.dumps(pack_array),
            "GoodsType": goods_type,
            "Account": account,
            "Price": price,
            "AllMoney": all_money,
            "GoodsName": goods_name,
            "GoodsName_En": goods_name_en,
            "StoreNumber": store_number,
            "StoreName": store_name,
            "Rem": remark,
            "CountryName": country_name,
            "Card": card,
        }

        r = session.post(
            f"{SUDA_BASE}/Data/SubmitOrder",
            data=post_data,
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            order_data = json.loads(data.get("ReturnJson", "{}"))
            result.success = True
            result.order_id = str(order_data.get("ID", ""))
            result.order_code = order_data.get("MainBillCode", "")
            result.receivables = order_data.get("Receivables", 0)
            _log(log, f"提交成功: 订单号={result.order_code} ID={result.order_id}")
        else:
            result.error = data.get("MsgText", "") or data.get("Msg", "提交集运失败")
            _log(log, f"提交失败: {result.error}")
    except Exception as e:
        result.error = f"提交异常: {e}"
        _log(log, result.error)

    return result


# ╔══════════════════════════════════════════════════════════════╗
# ║  订单列表查询                                               ║
# ╚══════════════════════════════════════════════════════════════╝

@dataclass
class SudaOrder:
    """速达集运订单。"""
    order_id: str = ""             # 内部编号 (7位数字, e.g. "7700000")
    order_code: str = ""           # 主单号 (e.g. "D770000000001")
    tracking_number: str = ""      # 发货单号 (物流单号, e.g. "770000000002")
    delivery_status: str = ""      # 物流状态原始文本 (在途/签收/未发货...)
    status: str = ""               # 付款状态
    create_time: str = ""
    receiver_name: str = ""
    receiver_phone: str = ""
    receiver_address: str = ""
    goods_name: str = ""
    total_weight: float = 0.0
    freight: float = 0.0


def get_order_list(
    session: requests.Session,
    search: str = "",
    search_type: int = 0,
    page: int = 0,
    log: Optional[LogFn] = None,
) -> Tuple[List[SudaOrder], str]:
    """获取订单列表（通过解析 /Home/Order HTML 页面）。

    search: 搜索关键词 (订单号 或 发货单号)
    search_type: 0=订单号, 3=发货单号
    page: 页码 (0=全部页, 1-N=指定页)
    """
    try:
        params = {"AddDays": "0", "PageCount": "10"}
        if search:
            params["OrderCode"] = search
            params["Condition"] = str(search_type)
        if page > 0:
            params["Page"] = str(page)

        r = session.get(f"{SUDA_BASE}/Home/Order", params=params, timeout=30)
        if "Login" in r.url and "Account" in r.url:
            return [], "session 已过期"

        orders = _parse_order_html(r.text)

        # 如果 page=0 且有多页, 加载所有页
        if page == 0 and not search:
            total_pages = _extract_total_pages(r.text)
            for p in range(2, total_pages + 1):
                params["Page"] = str(p)
                try:
                    r2 = session.get(
                        f"{SUDA_BASE}/Home/Order", params=params, timeout=30
                    )
                    orders.extend(_parse_order_html(r2.text))
                except Exception:
                    break

        return orders, ""
    except Exception as e:
        return [], f"获取订单列表失败: {e}"


def _extract_total_pages(html: str) -> int:
    """从订单页 HTML 中提取总页数。"""
    m = re.search(r'var\s+h\s*=\s*(\d+)', html)
    return int(m.group(1)) if m else 1


def _parse_order_html(html: str) -> List[SudaOrder]:
    """从 /Home/Order HTML 解析订单列表。

    HTML 结构: <tbody> 下的 <tr> 行, 每行 30+ 个 <td>, 包含:
    - td[2]: 内部编号 (e.g. 7008346)
    - td[3]: 订单号码 (e.g. D770000000001)
    - td[10]: 发货单号 (含 logisticspost span)
    - td[12]: 收货人
    - td[13]: 手机
    - td[15]: 地址
    - 倒数第3个可见 td: 物流状态 (含 logisticspost span)
    """
    orders = []
    seen_ids = set()

    # 提取 logisticspost 信息: data-OrderDetail(内部ID) + data-MainOrderCode(订单号)
    # 每个订单有2个 logisticspost span: 第1个=发货单号, 第2个=物流状态
    logistics_spans = re.findall(
        r'class="logisticspost\s+cur"[^>]*'
        r'data-OrderDetail="(\d+)"[^>]*'
        r'data-MainOrderCode="([^"]*)"[^>]*>'
        r'([^<]*)</span>',
        html,
    )

    # 按 internal_id 分组 (有tracking的订单出现2次, 无tracking的仅1次)
    order_map: Dict[str, dict] = {}
    for internal_id, order_code, text in logistics_spans:
        text = text.strip()
        if internal_id not in order_map:
            order_map[internal_id] = {
                "order_id": internal_id,
                "order_code": order_code.strip(),
                "tracking_number": text,
                "delivery_status": "",
            }
        else:
            # 第二次出现 = 物流状态列
            order_map[internal_id]["delivery_status"] = text

    # 修复: 仅有1个 logisticspost span 时, 判断是tracking还是delivery_status
    # tracking number = 纯数字/字母 (e.g. "770000000002")
    # delivery_status = 含中文 (e.g. "在途", "签收")
    for info in order_map.values():
        if not info["delivery_status"] and info["tracking_number"]:
            tn = info["tracking_number"]
            if any('\u4e00' <= c <= '\u9fff' for c in tn):
                info["delivery_status"] = tn
                info["tracking_number"] = ""

    # 从 <tr> 行中提取收件人等信息 (匹配 title="{internal_id}" 的 td)
    for internal_id, info in order_map.items():
        if internal_id in seen_ids:
            continue
        seen_ids.add(internal_id)

        order = SudaOrder(
            order_id=internal_id,
            order_code=info["order_code"],
            tracking_number=info["tracking_number"],
            delivery_status=info["delivery_status"],
        )

        # 提取该行的所有 <td> 内容
        # 查找包含此 internal_id 的 <tr> 块
        tr_pattern = re.compile(
            r'<tr[^>]*>.*?title="' + re.escape(internal_id)
            + r'">' + re.escape(internal_id)
            + r'</td>(.*?)</tr>',
            re.DOTALL,
        )
        tr_match = tr_pattern.search(html)
        if tr_match:
            row_html = tr_match.group(1)
            # 提取所有 td 的纯文本
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
            # 清理 HTML 标签
            def strip_tags(s: str) -> str:
                return re.sub(r'<[^>]+>', '', s).strip()

            # tds 偏移 (从 internal_id 之后):
            # [0] = order_code, [1] = destination, [2] = goods_type
            # [3] = warehouse, [4] = timing, [5] = carrier
            # [6] = goods_name, [7] = tracking_number (logisticspost)
            # [8] = goods_name2, [9] = receiver_name
            # [10] = phone, [11] = tel, [12] = address
            if len(tds) > 9:
                order.receiver_name = strip_tags(tds[9])
            if len(tds) > 10:
                order.receiver_phone = strip_tags(tds[10])
            if len(tds) > 12:
                order.receiver_address = strip_tags(tds[12])
            if len(tds) > 6:
                order.goods_name = strip_tags(tds[6])
            # 时间在 tr_match 之前, 用另一种方式获取
            # 提取 create_time (第二个 td 是完整时间)
            full_tr = re.search(
                r'<tr[^>]*>\s*<td[^>]*>[^<]*</td>\s*'
                r'<td[^>]*>(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*</td>\s*'
                r'<td[^>]*>' + re.escape(internal_id),
                html,
            )
            if full_tr:
                order.create_time = full_tr.group(1).strip()

            # 重量和费用 — 从可见 td (靠近末尾)
            # 寻找数字类的 td 值
            for td in reversed(tds):
                val = strip_tags(td)
                try:
                    f = float(val)
                    if f > 100 and order.freight == 0:
                        order.freight = f
                    elif f < 100 and order.total_weight == 0:
                        order.total_weight = f
                        break
                except ValueError:
                    continue

        orders.append(order)

    return orders


# ╔══════════════════════════════════════════════════════════════╗
# ║  取消订单                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

def cancel_order(
    session: requests.Session,
    order_id: str,
    log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """取消集运订单。"""
    try:
        r = session.post(
            f"{SUDA_BASE}/Data/CancelOrder",
            data={"id": order_id},
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            _log(log, f"取消订单成功: {order_id}")
            return True, ""
        msg = data.get("Msg", "取消失败")
        _log(log, f"取消订单失败: {msg}")
        return False, msg
    except Exception as e:
        return False, f"取消订单异常: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  余额支付                                                   ║
# ╚══════════════════════════════════════════════════════════════╝

def pay_with_balance(
    session: requests.Session,
    order_id: str,
    log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """用余额支付集运订单。"""
    try:
        r = session.post(
            f"{SUDA_BASE}/Data/PaymentBalance",
            data={"id": order_id},
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            _log(log, f"余额支付成功: {order_id}")
            return True, ""
        msg = data.get("Msg", "支付失败")
        _log(log, f"余额支付失败: {msg}")
        return False, msg
    except Exception as e:
        return False, f"支付异常: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  包裹预报 (入库前报关)                                      ║
# ╚══════════════════════════════════════════════════════════════╝

def add_prediction(
    session: requests.Session,
    bill_code: str,
    warehouse_id: int = DEFAULT_WAREHOUSE,
    goods_name: str = "",
    goods_type: int = DEFAULT_GOODS_TYPE,
    remark: str = "",
    log: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """添加包裹预报（让仓库知道即将有快递到达）。"""
    try:
        data_array = json.dumps([{
            "BillCode": bill_code,
            "WareHouseID": warehouse_id,
            "GoodsName": goods_name,
            "GoodsType": goods_type,
            "Rem": remark,
        }])
        r = session.post(
            f"{SUDA_BASE}/Data/AddPrediction",
            data={"Data": data_array},
            timeout=30,
        )
        data = r.json()
        if data.get("State"):
            _log(log, f"预报成功: {bill_code}")
            return True, ""
        msg = data.get("Msg", "预报失败")
        return False, msg
    except Exception as e:
        return False, f"预报异常: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  下拉列表数据                                               ║
# ╚══════════════════════════════════════════════════════════════╝

def get_dropdown(
    session: requests.Session,
    entity: str,
    parent_id: int = 0,
    log: Optional[LogFn] = None,
) -> Tuple[List[dict], str]:
    """获取下拉列表数据。

    entity: Country/Province/City/Area/Timer/Carrier 等
    parent_id: 父级 ID（省份的 parent 是国家 ID）
    """
    try:
        r = session.get(
            f"{SUDA_BASE}/GetDropDownList/Get{entity}List",
            params={"parentId": parent_id} if parent_id else {},
            timeout=20,
        )
        return r.json(), ""
    except Exception as e:
        return [], f"获取{entity}列表失败: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║  高级入口 — 一键提交集运                                     ║
# ╚══════════════════════════════════════════════════════════════╝

def submit_shipment(
    username: str,
    password: str,
    package_ids: List[str],
    address_id: int,
    country_id: int = DEFAULT_COUNTRY,
    province_id: int = 0,
    city_id: int = 0,
    agent_id: int = 0,
    goods_type: int = DEFAULT_GOODS_TYPE,
    goods_name: str = "",
    remark: str = "",
    card: str = DEFAULT_CARD,
    is_tax: bool = DEFAULT_IS_TAX,
    pay_type: int = 2,
    log: Optional[LogFn] = None,
) -> SudaOrderResult:
    """一键提交集运 — 自动登录 + 提交订单。

    最高级别的入口函数，适合从 UI 层直接调用。
    """
    result = SudaOrderResult()

    # 1. 确保 session
    session, err = ensure_session(username, password, log)
    if err:
        result.error = err
        return result

    # 2. 提交订单
    return submit_order(
        session=session,
        package_ids=package_ids,
        address_id=address_id,
        country_id=country_id,
        province_id=province_id,
        city_id=city_id,
        agent_id=agent_id,
        goods_type=goods_type,
        goods_name=goods_name,
        remark=remark,
        card=card,
        is_tax=is_tax,
        pay_type=pay_type,
        log=log,
    )


def check_warehouse_arrival(
    username: str,
    password: str,
    tracking_no: str,
    warehouse_id: int = DEFAULT_WAREHOUSE,
    log: Optional[LogFn] = None,
) -> Tuple[Optional[SudaPackage], str]:
    """检查包裹是否已入库 — 自动登录 + 查询。

    返回 (package, error)。package 为 None 表示尚未入库。
    """
    session, err = ensure_session(username, password, log)
    if err:
        return None, err
    return find_package_by_tracking(session, tracking_no, warehouse_id, log)


# ╔══════════════════════════════════════════════════════════════╗
# ║  物流跟踪查询                                               ║
# ╚══════════════════════════════════════════════════════════════╝

def _normalize_delivery_status(raw: str) -> str:
    """将原始物流描述归一化为触发级别。

    返回: 已下单 / 已发货 / 已集货 / 配送中 / 已签收
    """
    s = raw.strip()
    if not s:
        return ""
    # 签收 / 送達
    if "签收" in s or "送達" in s or "送达" in s:
        return "已签收"
    # 配送中
    if "配送中" in s:
        return "配送中"
    # 已集货 / 转运中 (台湾段)
    if "已集貨" in s or "已集货" in s or "轉運中" in s or "转运中" in s:
        return "已集货"
    # 已发货 / 在途 / 集运仓发货 / 航班
    if ("已发货" in s or "已發貨" in s or "在途" in s
            or "发货" in s or "發貨" in s or "航班" in s
            or "已核重" in s):
        return "已发货"
    # 已下单
    if "下单" in s or "下單" in s:
        return "已下单"
    return s


def get_order_tracking(
    session: requests.Session,
    order_id: str,
    log: Optional[LogFn] = None,
) -> Tuple[Optional[SudaTrackingResult], str]:
    """查询集运订单的物流跟踪信息。

    order_id: 可以是内部编号 (7位数字) 或订单号 (D5417...)。
              如果传入订单号，先从订单列表查内部ID。

    物流跟踪 URL: /home/Track?OrderDetail={internal_id}

    Returns:
        (SudaTrackingResult, error)
    """
    result = SudaTrackingResult(order_id=order_id)

    internal_id = order_id
    order_code_found = ""
    tracking_number_found = ""
    delivery_status_raw = ""

    # 如果不是纯数字 或 长度>8, 可能是订单号, 先从订单列表搜索
    if not order_id.isdigit() or len(order_id) > 8:
        _log(log, f"搜索订单: {order_id}")
        # 尝试按订单号搜索
        orders, err = get_order_list(session, search=order_id, search_type=0, page=1, log=log)
        if not orders:
            # 尝试按发货单号搜索
            orders, err = get_order_list(session, search=order_id, search_type=3, page=1, log=log)
        if not orders:
            # 尝试不搜索, 遍历全部页找
            orders, err = get_order_list(session, page=0, log=log)
            orders = [o for o in orders if order_id in (o.order_id, o.order_code, o.tracking_number)]
        if orders:
            o = orders[0]
            internal_id = o.order_id
            order_code_found = o.order_code
            tracking_number_found = o.tracking_number
            delivery_status_raw = o.delivery_status
            _log(log, f"找到订单: 内部ID={internal_id} 单号={order_code_found} "
                       f"发货号={tracking_number_found} 状态={delivery_status_raw}")
        else:
            return None, f"未找到订单 {order_id}"

    # 先用订单列表中的信息填充基本结果
    if order_code_found:
        result.order_code = order_code_found
    if tracking_number_found:
        result.tracking_code = tracking_number_found
    if delivery_status_raw:
        result.latest_status = _normalize_delivery_status(delivery_status_raw)

    # 尝试加载 Track 页面获取详细时间线
    track_url = f"{SUDA_BASE}/home/Track?OrderDetail={internal_id}"
    try:
        r = session.get(track_url, timeout=30)
        if r.status_code == 200 and len(r.text) > 200:
            track_html = r.text

            # 尝试 JSON
            try:
                data = json.loads(track_html)
                if isinstance(data, dict):
                    detail = _parse_tracking_json(data, order_id, log)
                    if detail.events:
                        result.events = detail.events
                        result.latest_status = detail.latest_status
                    if detail.tracking_code and not result.tracking_code:
                        result.tracking_code = detail.tracking_code
                    return result, ""
            except (json.JSONDecodeError, ValueError):
                pass

            # HTML 解析
            detail = _parse_tracking_html(track_html, order_id, log)
            if detail.events:
                result.events = detail.events
                result.latest_status = detail.latest_status
            if detail.tracking_code and not result.tracking_code:
                result.tracking_code = detail.tracking_code
    except Exception as e:
        _log(log, f"Track 页面加载失败: {e}")

    # 即使 Track 页面没有详细事件, 只要有 delivery_status 也算成功
    if result.latest_status:
        return result, ""

    return result, "" if delivery_status_raw else f"无法获取订单 {order_id} 的物流信息"


def _parse_tracking_json(
    data: dict,
    order_id: str,
    log: Optional[LogFn] = None,
) -> SudaTrackingResult:
    """解析 JSON 格式的物流响应。"""
    result = SudaTrackingResult(order_id=order_id)

    # 尝试常见 JSON 结构
    logistics = data.get("Data") or data.get("data") or data
    if isinstance(logistics, str):
        try:
            logistics = json.loads(logistics)
        except Exception:
            pass

    if isinstance(logistics, dict):
        result.order_code = str(logistics.get("MainBillCode", "")
                                or logistics.get("BillCode", ""))
        result.tracking_code = str(logistics.get("MainBillCode", "")
                                   or logistics.get("TrackingCode", ""))
        traces = logistics.get("Traces") or logistics.get("traces") or []
        if isinstance(traces, list):
            for t in traces:
                evt = SudaTrackingEvent(
                    status=str(t.get("Status", "") or t.get("status", "")),
                    detail=str(t.get("Desc", "") or t.get("desc", "")
                               or t.get("Description", "")),
                    timestamp=str(t.get("Time", "") or t.get("time", "")),
                )
                result.events.append(evt)

    elif isinstance(logistics, list):
        for t in logistics:
            if not isinstance(t, dict):
                continue
            evt = SudaTrackingEvent(
                status=str(t.get("Status", "") or t.get("status", "")),
                detail=str(t.get("Desc", "") or t.get("desc", "")
                           or t.get("Description", "")),
                timestamp=str(t.get("Time", "") or t.get("time", "")),
            )
            result.events.append(evt)

    # 归一化最新状态
    if result.events:
        last_evt = result.events[-1]
        combined = f"{last_evt.status} {last_evt.detail}"
        result.latest_status = _normalize_delivery_status(combined)

    return result


def _parse_tracking_html(
    html: str,
    order_id: str,
    log: Optional[LogFn] = None,
) -> SudaTrackingResult:
    """从 HTML 页面解析物流跟踪信息。"""
    result = SudaTrackingResult(order_id=order_id)

    # 提取发货主号 / 主单号
    main_bill = re.search(
        r'(?:主单号|发货主号|MainBillCode)[：:\s]*(\d{10,20})', html
    )
    if main_bill:
        result.order_code = main_bill.group(1)
        result.tracking_code = main_bill.group(1)

    # 提取物流事件时间线
    # 模式 1: 表格行 <tr>...<td>状态</td><td>时间</td><td>描述</td>...</tr>
    row_pattern = (
        r'<tr[^>]*>\s*<td[^>]*>([^<]*)</td>\s*'
        r'<td[^>]*>(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})</td>\s*'
        r'<td[^>]*>([^<]*)</td>'
    )
    for m in re.finditer(row_pattern, html, re.DOTALL):
        evt = SudaTrackingEvent(
            status=m.group(1).strip(),
            timestamp=m.group(2).strip(),
            detail=m.group(3).strip(),
        )
        result.events.append(evt)

    # 模式 2: 时间线 div 结构
    if not result.events:
        timeline_pattern = (
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*'
            r'(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*'
            r'([^<]{2,80})'
        )
        for m in re.finditer(timeline_pattern, html):
            detail = m.group(2).strip()
            if not detail or len(detail) < 2:
                continue
            # 从描述中推断状态
            status = ""
            for kw in ["签收", "派件", "揽件", "在途", "已下单"]:
                if kw in detail:
                    status = kw
                    break
            evt = SudaTrackingEvent(
                status=status,
                timestamp=m.group(1).strip(),
                detail=detail,
            )
            result.events.append(evt)

    # 归一化最新状态
    if result.events:
        last_evt = result.events[-1]
        combined = f"{last_evt.status} {last_evt.detail}"
        result.latest_status = _normalize_delivery_status(combined)

    # 调试: 如果没解析到事件，记录 HTML 长度便于排查
    if not result.events and log:
        _log(log, f"物流查询: 订单 {order_id} 未解析到事件 (HTML长度={len(html)})")

    return result


def get_order_tracking_by_login(
    username: str,
    password: str,
    order_id: str,
    log: Optional[LogFn] = None,
) -> Tuple[Optional[SudaTrackingResult], str]:
    """查询物流跟踪 — 自动登录 + 查询。"""
    session, err = ensure_session(username, password, log)
    if err:
        return None, err
    return get_order_tracking(session, order_id, log)
