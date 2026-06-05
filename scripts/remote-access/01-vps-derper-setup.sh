#!/usr/bin/env bash
#
# 01-vps-derper-setup.sh
# 在香港 CN2 GIA VPS (Ubuntu/Debian) 上一键部署 Tailscale 自建 DERP 中继。
#
# 用法:
#   1. 先把域名 A 记录解析到本机公网 IP。
#   2. 编辑下面的 CONFIG 段 (至少改 DOMAIN)。
#   3. sudo bash 01-vps-derper-setup.sh
#
# 关键加固: derper 带 -verify-clients，且本机先加入 tailnet，
#           避免昂贵的 CN2 线路被全网当免费中继白嫖。
#
set -euo pipefail

# ===================== CONFIG (改这里) =====================
DOMAIN="derp.yourdomain.com"      # 必改：你解析好的 DERP 域名
GO_VERSION="1.22.0"                # 编译 derper 用的 Go 版本
TAILSCALE_AUTHKEY=""              # 可选：填了就无人值守入网 (tskey-auth-...)；留空则交互式浏览器授权
STUN_PORT="3478"
# ==========================================================

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行：sudo bash $0" >&2
  exit 1
fi

if [[ "$DOMAIN" == "derp.yourdomain.com" ]]; then
  echo "❌ 还没改 CONFIG 里的 DOMAIN，先改成你真实的域名。" >&2
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) GOARCH="amd64" ;;
  aarch64|arm64) GOARCH="arm64" ;;
  *) echo "❌ 未适配的架构: $ARCH" >&2; exit 1 ;;
esac

echo "==> [1/6] 安装 Tailscale 本体"
curl -fsSL https://tailscale.com/install.sh | sh

echo "==> [2/6] 安装 Go ${GO_VERSION} (${GOARCH})"
GO_TARBALL="go${GO_VERSION}.linux-${GOARCH}.tar.gz"
cd /tmp
wget -q "https://go.dev/dl/${GO_TARBALL}"
rm -rf /usr/local/go
tar -C /usr/local -xzf "${GO_TARBALL}"
export PATH="$PATH:/usr/local/go/bin"
if ! grep -q '/usr/local/go/bin' /root/.bashrc 2>/dev/null; then
  echo 'export PATH=$PATH:/usr/local/go/bin' >> /root/.bashrc
fi

echo "==> [3/6] 编译 derper"
/usr/local/go/bin/go install tailscale.com/cmd/derper@latest
DERPER_BIN="/root/go/bin/derper"
[[ -x "$DERPER_BIN" ]] || { echo "❌ derper 编译失败，未找到 $DERPER_BIN" >&2; exit 1; }

echo "==> [4/6] 本机加入 tailnet (verify-clients 的前提)"
if tailscale status >/dev/null 2>&1; then
  echo "    已在 tailnet 中，跳过。"
elif [[ -n "$TAILSCALE_AUTHKEY" ]]; then
  tailscale up --authkey "$TAILSCALE_AUTHKEY"
else
  echo "    未提供 authkey，启动交互式登录——请在浏览器打开下面的链接授权:"
  tailscale up
fi

echo "==> [5/6] 开放端口 (若装了 ufw)"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 443/tcp || true
  ufw allow "${STUN_PORT}/udp" || true
else
  echo "    未检测到 ufw，请自行确认安全组放行 443/tcp 和 ${STUN_PORT}/udp。"
fi

echo "==> [6/6] 写入 systemd 单元并启动 derper (带 -verify-clients)"
cat > /etc/systemd/system/derper.service <<EOF
[Unit]
Description=Tailscale DERP Relay
After=network.target tailscaled.service
Requires=tailscaled.service

[Service]
ExecStart=${DERPER_BIN} -hostname ${DOMAIN} -certmode letsencrypt -a :443 -stun-port ${STUN_PORT} -verify-clients
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now derper
sleep 2
systemctl --no-pager status derper || true

echo
echo "✅ 部署完成。验证："
echo "   浏览器打开 https://${DOMAIN} 应看到 DERP 文字页面 (首次签发证书可能要等几十秒)。"
echo "   下一步：把 02-tailscale-acl.hujson 合并进 Tailscale 后台的 Access Controls。"
