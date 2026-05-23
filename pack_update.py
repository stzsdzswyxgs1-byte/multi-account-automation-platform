"""一键打包 + 上传更新到 Cloudflare Worker

用法：
    python pack_update.py 1.0.2 "修复了AI回复问题"
    python pack_update.py 1.0.3           # changelog 可省略
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile

import requests
import core.ssl_helper  # noqa: F401  — SSL 容错

# ---------- 配置 ----------

# 需要打包的代码文件/目录（相对于项目根目录）
CODE_ITEMS = [
    "app.py",
    "requirements.txt",
    "run.bat",
    "install.bat",
    "start_updater.bat",
    "updater.pyw",
    "rollback.py",
    "core",                   # 整个 core 目录
    "fonts",                  # 字体文件 (Noto Sans TC)
    "cat_attrs_cache.json",   # 分类属性缓存（所有机器共享）
    "allowed_categories.json", # 分类白名单（刊登必需）
]

# 不打包的文件后缀/目录
SKIP_PATTERNS = [
    "__pycache__",
    ".pyc",
    ".pyo",
    ".wrangler",
    "node_modules",
    "remark_templates.json",   # 用户自定义备注模板，不覆盖
]

# 从 tg_relay_config.json 读取 worker_url
CONFIG_FILE = "tg_relay_config.json"


def load_config():
    """读取 worker URL 和 API key。"""
    cfg_path = os.path.join(os.path.dirname(__file__), CONFIG_FILE)
    if not os.path.exists(cfg_path):
        print(f"[ERR] 找不到 {cfg_path}")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    worker_url = cfg.get("worker_url", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    if not worker_url:
        print("[ERR] tg_relay_config.json 中缺少 worker_url")
        sys.exit(1)
    if not api_key:
        print("[ERR] tg_relay_config.json 中缺少 api_key")
        sys.exit(1)
    return worker_url, api_key


def _should_skip(path: str) -> bool:
    """检查文件/目录是否应该跳过。"""
    for pat in SKIP_PATTERNS:
        if pat in path:
            return True
    return False


def build_zip(root_dir: str) -> bytes:
    """将代码文件打包为 zip，返回字节。"""
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in CODE_ITEMS:
            full = os.path.join(root_dir, item)
            if not os.path.exists(full):
                print(f"  [WARN] 跳过不存在的: {item}")
                continue

            if os.path.isfile(full):
                if not _should_skip(item):
                    zf.write(full, item)
                    count += 1
            elif os.path.isdir(full):
                for dirpath, dirnames, filenames in os.walk(full):
                    # 过滤目录
                    dirnames[:] = [
                        d for d in dirnames if not _should_skip(d)
                    ]
                    for fn in filenames:
                        if _should_skip(fn):
                            continue
                        abs_path = os.path.join(dirpath, fn)
                        rel_path = os.path.relpath(abs_path, root_dir)
                        zf.write(abs_path, rel_path)
                        count += 1

    print(f"[OK] 打包完成: {count} 个文件, {buf.tell() / 1024:.1f} KB")
    return buf.getvalue()


def upload(worker_url: str, api_key: str, data: bytes,
           version: str, changelog: str, target_users: str = "") -> bool:
    """上传 zip 到 Worker R2。

    v6.1:target_users 可指定逗號分隔的 user_id 列表(只推給這些 user_id 的客戶端)。
    空值 = 全推(舊行為,backward compatible)。
    """
    url = (
        f"{worker_url}/update/upload"
        f"?key={api_key}&version={version}"
        f"&changelog={requests.utils.quote(changelog)}"
    )
    if target_users:
        url += f"&target_users={requests.utils.quote(target_users)}"
    print(f"[...] 上传到 {worker_url} ...")
    if target_users:
        print(f"[...] 限定 user_id: {target_users}")
    r = requests.put(url, data=data, timeout=120,
                     headers={"Content-Type": "application/zip"})
    if r.status_code == 200:
        resp = r.json()
        if resp.get("ok"):
            print(f"[OK] 上传成功! 版本: {version}")
            return True
    print(f"[ERR] 上传失败: {r.status_code} {r.text[:200]}")
    return False


def _notify_update_tg(root_dir: str, version: str, changelog: str):
    """推送成功后发一次 TG 通知给当前使用者（settings.json 的 tg_chat_id）。"""
    try:
        tokens_path = os.path.join(root_dir, "tg_tokens.json")
        if not os.path.exists(tokens_path):
            print("[WARN] 找不到 tg_tokens.json，跳过 TG 通知")
            return
        with open(tokens_path, "r", encoding="utf-8") as f:
            tokens = json.load(f)
        bot_token = tokens.get("manage_bot_token", "")
        if not bot_token:
            print("[WARN] manage_bot_token 为空，跳过 TG 通知")
            return

        settings_path = os.path.join(root_dir, "settings.json")
        if not os.path.exists(settings_path):
            print("[WARN] 找不到 settings.json，跳过 TG 通知")
            return
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        owner_chat_id = str(settings.get("tg_chat_id", "")).strip()
        if not owner_chat_id:
            print("[WARN] settings.json 中无 tg_chat_id，跳过 TG 通知")
            return

        msg = f"🔄【软件已自动更新】\n新版本：{version}\n"
        if changelog:
            msg += f"更新内容：{changelog}\n"
        msg += f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n请双击 run.bat 重新启动软件"

        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": owner_chat_id, "text": msg},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            print(f"[OK] TG 通知已发送给 {owner_chat_id}")
        else:
            print(f"[WARN] TG 通知发送失败: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[WARN] TG 通知异常: {e}")


def main():
    if len(sys.argv) < 2:
        print("用法: python pack_update.py <版本号> [更新说明] [--target=ID1,ID2,...]")
        print("例如: python pack_update.py 1.0.2 \"修复了AI回复问题\"")
        print("      python pack_update.py 1.0.3 \"灰度测试\" --target=6679449205")
        sys.exit(1)

    # 解析 args:positional [version, changelog] + optional --target=ID1,ID2
    pos_args = []
    target_users = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--target="):
            target_users = arg.split("=", 1)[1].strip()
        elif arg.startswith("--target"):
            print("[ERR] --target 需要等號:--target=6679449205")
            sys.exit(1)
        else:
            pos_args.append(arg)
    if not pos_args:
        print("[ERR] 缺少版本號")
        sys.exit(1)

    version = pos_args[0]
    changelog = pos_args[1] if len(pos_args) > 1 else ""

    root_dir = os.path.dirname(os.path.abspath(__file__))
    worker_url, api_key = load_config()

    print(f"[INFO] 版本: {version}")
    print(f"[INFO] 更新说明: {changelog or '(无)'}")
    if target_users:
        print(f"[INFO] 限定推送: user_id ∈ [{target_users}]")
    else:
        print(f"[INFO] 推送範圍: 全推(所有客戶端)")
    print(f"[INFO] 项目目录: {root_dir}")
    print()

    data = build_zip(root_dir)
    if not data:
        print("[ERR] 打包失败")
        sys.exit(1)

    ok = upload(worker_url, api_key, data, version, changelog, target_users=target_users)
    if not ok:
        sys.exit(1)

    # 单独上传 updater.pyw（供旧版 updater 小文件自修复）
    updater_path = os.path.join(root_dir, "updater.pyw")
    if os.path.exists(updater_path):
        with open(updater_path, "rb") as f:
            udata = f.read()
        try:
            r = requests.put(
                f"{worker_url}/update/updater?key={api_key}",
                data=udata, timeout=30,
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            if r.status_code == 200:
                print(f"[OK] updater.pyw 单独上传成功 ({len(udata)/1024:.1f} KB)")
            else:
                print(f"[WARN] updater.pyw 上传失败: {r.status_code}")
        except Exception as e:
            print(f"[WARN] updater.pyw 上传异常: {e}")

    # 推送成功后发一次 TG 通知（取代 updater.pyw 的多机器重复通知）
    _notify_update_tg(root_dir, version, changelog)

    sys.exit(0)


if __name__ == "__main__":
    main()
