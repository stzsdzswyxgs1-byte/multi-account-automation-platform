"""版本回滚工具

用法：
    python rollback.py              # 列出可用历史版本
    python rollback.py 4.7.12       # 回滚到指定版本
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import requests
import core.ssl_helper  # noqa: F401

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = "tg_relay_config.json"
VERSION_FILE = "current_version.txt"


def load_config():
    cfg_path = os.path.join(ROOT_DIR, CONFIG_FILE)
    if not os.path.exists(cfg_path):
        print(f"[ERR] 找不到 {cfg_path}")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    worker_url = cfg.get("worker_url", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    if not worker_url or not api_key:
        print("[ERR] tg_relay_config.json 中缺少 worker_url 或 api_key")
        sys.exit(1)
    return worker_url, api_key


def list_versions(worker_url, api_key):
    """列出云端保存的历史版本。"""
    r = requests.get(
        f"{worker_url}/update/versions",
        params={"key": api_key},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"[ERR] 请求失败: {r.status_code}")
        return []
    data = r.json()
    versions = data.get("versions", [])
    if not versions:
        print("暂无历史版本记录")
        return []

    # 读取本地当前版本
    local_ver = ""
    vf = os.path.join(ROOT_DIR, VERSION_FILE)
    if os.path.exists(vf):
        local_ver = Path(vf).read_text(encoding="utf-8").strip()

    print(f"\n当前版本: {local_ver or '未知'}")
    print(f"可用历史版本 (共 {len(versions)} 个):\n")
    print(f"  {'版本':<12} {'时间':<22} {'大小':>8}  说明")
    print(f"  {'-'*12} {'-'*22} {'-'*8}  {'-'*20}")
    for v in versions:
        ver = v.get("version", "?")
        ts = v.get("uploaded_at", "")[:19].replace("T", " ")
        size = f"{v.get('size', 0) / 1024:.0f}KB"
        cl = v.get("changelog", "")[:40]
        marker = " <-- 当前" if ver == local_ver else ""
        print(f"  {ver:<12} {ts:<22} {size:>8}  {cl}{marker}")
    print(f"\n用法: python rollback.py <版本号>")
    return versions


def rollback(worker_url, api_key, target_version):
    """下载指定版本并覆盖本地文件。"""
    print(f"[INFO] 正在下载版本 {target_version} ...")
    r = requests.get(
        f"{worker_url}/update/download",
        params={"key": api_key, "version": target_version},
        timeout=120,
    )
    if r.status_code == 404:
        print(f"[ERR] 版本 {target_version} 不存在")
        sys.exit(1)
    if r.status_code != 200:
        print(f"[ERR] 下载失败: {r.status_code}")
        sys.exit(1)

    zip_data = r.content
    print(f"[OK] 下载完成: {len(zip_data) / 1024:.1f} KB")

    # 解压覆盖
    print("[INFO] 正在解压覆盖 ...")
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    count = 0
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        if name == "updater.pyw":
            continue
        dest = os.path.join(ROOT_DIR, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(name) as src, open(dest, "wb") as dst:
            dst.write(src.read())
        count += 1

    # 更新本地版本号
    vf = os.path.join(ROOT_DIR, VERSION_FILE)
    Path(vf).write_text(target_version, encoding="utf-8")

    print(f"[OK] 回滚完成: {count} 个文件已覆盖，版本 -> {target_version}")
    print("[INFO] 请重启软件 (双击 run.bat)")


def main():
    worker_url, api_key = load_config()

    if len(sys.argv) < 2:
        list_versions(worker_url, api_key)
        return

    target = sys.argv[1].strip()
    rollback(worker_url, api_key, target)


if __name__ == "__main__":
    main()
