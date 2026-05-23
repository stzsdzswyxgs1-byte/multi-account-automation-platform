"""采购出货图片管理 — 用户为每个采购订单附 1 张图，自动出货后上传到顺云宝供仓库核对打包。

工作流：
  1. 用户在 采购出货 UI 选图 → ingest_image() 复制到 internal cache
  2. 自动出货完成（doImport 成功）后，调 upload_images_for_yahoo_order()
     - 按 yahoo_order_no 查 顺云宝 stock 的所有 detail
     - 用 PurchaseLink.tracking_no 匹配 detail.innerExpCode 找到 detailId
     - upload_attachment + bind_detail_thumb
     - 失败发 TG 通知

匹配规则：tracking_no ↔ innerExpCode（唯一可靠，无 fallback）
"""
from __future__ import annotations

import shutil
import threading
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional, Tuple

LogFn = Callable[[str], None]

BASE_DIR = Path(__file__).resolve().parent.parent
IMAGE_CACHE_DIR = BASE_DIR / "output" / "采购图片"

# 物流系统支持的图片格式（jpg/png/webp 是 web 通用，gif/bmp 也支持）
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 压缩阈值：超过 1.5MB 或长边超过 1920px 才压缩，否则原样上传
_MAX_RAW_SIZE = 1_500_000
_MAX_LONG_SIDE = 1920
_JPEG_QUALITY = 85


def is_supported_image(path: Path) -> bool:
    return path.suffix.lower() in ALLOWED_EXTS


def ingest_image(
    src_path: Path,
    yahoo_order: str,
    purchase_order: str,
    log: Optional[LogFn] = None,
) -> Tuple[Path, str]:
    """把用户选的图片复制到 internal cache，必要时压缩。
    返回 (final_internal_path, error_msg)。error_msg == "" 表示成功。
    """
    src_path = Path(src_path)
    if not src_path.exists():
        return Path(), f"文件不存在: {src_path}"
    if not is_supported_image(src_path):
        return Path(), f"格式不支持({src_path.suffix})，仅支持 {', '.join(sorted(ALLOWED_EXTS))}"

    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yahoo_safe = _sanitize(yahoo_order)
    purchase_safe = _sanitize(purchase_order) or "main"
    ext = src_path.suffix.lower()
    # 大图压缩成 jpg；小图原样保留扩展名
    raw_size = src_path.stat().st_size
    needs_compress = raw_size > _MAX_RAW_SIZE or ext in (".png", ".webp", ".bmp")
    if needs_compress:
        ext = ".jpg"
    dst = IMAGE_CACHE_DIR / f"{yahoo_safe}_{purchase_safe}{ext}"

    try:
        if needs_compress:
            _compress_to_jpg(src_path, dst)
        else:
            shutil.copy2(src_path, dst)
        if log:
            log(f"[采购图片] 已缓存: {src_path.name} → {dst.name} ({dst.stat().st_size//1024}KB)")
        return dst, ""
    except Exception as e:
        return Path(), f"复制/压缩异常: {e}"


def remove_image(path: Path) -> bool:
    """删 internal cache 里的图（用户从 UI 删除时调）。"""
    try:
        p = Path(path)
        if p.exists() and IMAGE_CACHE_DIR in p.parents:
            p.unlink()
            return True
    except Exception:
        pass
    return False


def _sanitize(s: str) -> str:
    """把订单号里非法的字符替换掉，避免文件名问题"""
    bad = '\\/:*?"<>|'
    out = "".join("_" if c in bad else c for c in str(s or "").strip())
    return out[:80]  # 截断防超长


def _compress_to_jpg(src: Path, dst: Path) -> None:
    """用 PIL 压缩到 1920px 长边 + JPEG q=85，保证清晰度但减小体积。"""
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)  # 修正 EXIF 旋转
        if im.mode in ("RGBA", "LA", "P"):
            # JPEG 不支持透明，转白底
            bg = Image.new("RGB", im.size, (255, 255, 255))
            try:
                bg.paste(im, mask=im.split()[-1] if im.mode != "P" else None)
            except Exception:
                bg.paste(im.convert("RGB"))
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        # 等比缩放到长边 1920
        w, h = im.size
        long_side = max(w, h)
        if long_side > _MAX_LONG_SIDE:
            ratio = _MAX_LONG_SIDE / long_side
            im = im.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=_JPEG_QUALITY, optimize=True)


# ────────────────────────────────────────────────────────────
# 上传到顺云宝（自动出货完成后调）
# ────────────────────────────────────────────────────────────

def upload_images_for_yahoo_order(
    yahoo_order_no: str,
    purchase_links: List,  # List[PurchaseLink]，避免循环 import 不写类型
    log: Optional[LogFn] = None,
    tg_notify: Optional[Callable[[str], None]] = None,
) -> Tuple[int, int, List[str]]:
    """同一个 yahoo_order 下的所有 PurchaseLink，逐个上传图片到顺云宝。

    返回 (成功数, 失败数, 错误列表)。

    匹配规则：PurchaseLink.tracking_no == 顺云宝 detail.innerExpCode
    """
    from .syb_http_ops import (
        ensure_stoken, query_stock_details_by_code,
        upload_attachment, bind_detail_thumb,
    )

    # 过滤出有图且未上传的
    pending = [
        x for x in purchase_links
        if (x.image_path or "").strip()
        and not getattr(x, "image_uploaded", False)
        and Path(x.image_path).exists()
    ]
    if not pending:
        return 0, 0, []

    if log: log(f"[采购图片] 准备上传 {len(pending)} 张图（yahoo_order={yahoo_order_no}）")

    try:
        stoken = ensure_stoken(log=log)
    except Exception as e:
        msg = f"取 stoken 失败: {e}"
        if log: log(f"[采购图片] {msg}")
        if tg_notify:
            tg_notify(f"⚠️ 图片上传失败 — {yahoo_order_no}\n原因: {msg}\n（订单本身已上传，仅图片缺失）")
        return 0, len(pending), [msg]

    # 拿这个 stock 的所有 detail — 带重试（doImport 在 顺云宝 后端落库可能要 10-30 秒）
    import time as _time
    details = []
    last_err = ""
    for attempt, wait_sec in enumerate([0, 8, 15, 25, 40], start=1):  # 累计最多等 ~88 秒
        if wait_sec > 0:
            if log: log(f"[采购图片] 第 {attempt} 次查 detail（等 {wait_sec}s 让 doImport 落库）...")
            _time.sleep(wait_sec)
        try:
            details = query_stock_details_by_code(stoken, yahoo_order_no, log=log)
            if details:
                break
        except Exception as e:
            last_err = str(e)
            if log: log(f"[采购图片] 查 detail 异常（第 {attempt} 次）: {e}")
    if not details:
        msg = f"顺云宝查不到 stock detail (code={yahoo_order_no})，重试 5 次共 ~88 秒未果。{('异常: ' + last_err) if last_err else 'doImport 可能没真正成功'}"
        if log: log(f"[采购图片] {msg}")
        if tg_notify:
            tg_notify(f"⚠️ 图片上传失败 — {yahoo_order_no}\n原因: {msg}\n请稍后手动重试")
        return 0, len(pending), [msg]

    # tracking_no → detailId 索引
    track_to_detail: dict = {}
    for d in details:
        inner = str(d.get("innerExpCode") or "").strip()
        did = d.get("id")
        if inner and did:
            track_to_detail[inner] = int(did)

    ok_n = 0
    fail_n = 0
    errs: List[str] = []
    skipped_no_track = 0
    for link in pending:
        track = (link.tracking_no or "").strip()
        if not track:
            # 多采购场景：部分采购还没拿到 tracking — 不算失败，等下一轮监控
            skipped_no_track += 1
            if log: log(f"[采购图片] {link.purchase_order_id} 暂无 tracking_no，跳过（等下一轮）")
            continue
        detail_id = track_to_detail.get(track)
        if not detail_id:
            errs.append(f"{link.purchase_order_id}: tracking={track} 在顺云宝 detail 里找不到")
            fail_n += 1
            continue

        thumb_id, err = upload_attachment(stoken, Path(link.image_path), log=log)
        if err:
            errs.append(f"{link.purchase_order_id}: 上传图失败 — {err}")
            fail_n += 1
            continue
        ok_b, err2 = bind_detail_thumb(stoken, detail_id, thumb_id, log=log)
        if not ok_b:
            errs.append(f"{link.purchase_order_id}: 绑定失败 — {err2}")
            fail_n += 1
            continue

        # 标记已上传
        link.image_uploaded = True
        ok_n += 1

    if log:
        msg = f"[采购图片] 完成: 成功 {ok_n}, 失败 {fail_n}"
        if skipped_no_track:
            msg += f", 暂跳过(无 tracking) {skipped_no_track}"
        log(msg)

    if fail_n > 0 and tg_notify:
        lines = [f"⚠️ 图片上传部分失败 — {yahoo_order_no}",
                 f"成功 {ok_n} / 失败 {fail_n}", ""]
        lines.extend("- " + e for e in errs[:5])
        if len(errs) > 5:
            lines.append(f"... 还有 {len(errs)-5} 条")
        tg_notify("\n".join(lines))

    return ok_n, fail_n, errs
