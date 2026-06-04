# 远程控制日本电脑施工方案（Tailscale + 自建 DERP 中继）

> **架构**：中国手机 → Tailscale 虚拟内网 → 日本电脑 RDP 桌面
> **中继**：自建 DERP 放在香港 CN2 GIA VPS（解决 Tailscale 官方中继在国内不稳的问题）
> **核心前提**：日本电脑业务流量的对外出口 IP **必须保持不变**（Taobao / Xianyu 风控行为一致性），所以全程**不开 Exit Node**。

> 本文档是经过技术审查 + 加固后的版本。相对原始方案，新增了 4 处关键修订（已在文中用 🔒/⚠️ 标出）：
> 1. DERP 开启 `-verify-clients`，防止付费 CN2 线路被白嫖
> 2. 单点故障（SPOF）提示 + 存活监控
> 3. RDP 在 tailnet 内的访问控制（ACL + 强密码）
> 4. 国区 App Store 装不了 Tailscale 的应对

---

## 施工前确认

1. 香港 VPS 系统为 Debian / Ubuntu，且**已绑定域名**（DERP 中继**必须**有域名 + 证书，不能用纯 IP）。
2. 日本电脑 Windows 10 是否为**专业版 Pro**？
   - **Pro**：自带 RDP 服务端，按下方主方案走。
   - **Home**：无 RDP 服务端，改用附录 A 的 **RustDesk** 兜底方案。

下方主方案默认「VPS = Ubuntu + 有域名」「Win10 = Pro」。

---

## 第一部分：香港 VPS —— 自建 DERP 中继

DERP 是 Tailscale 的中继节点。国内连这个走你自己的香港线路，稳定。

### 1. 装 Go 和 Tailscale

```bash
# 装 Tailscale 本体
curl -fsSL https://tailscale.com/install.sh | sh

# 装 Go（编译 derper 用）
wget https://go.dev/dl/go1.22.0.linux-amd64.tar.gz
tar -C /usr/local -xzf go1.22.0.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
source ~/.bashrc

# 编译 derper
go install tailscale.com/cmd/derper@latest
```

### 🔒 2. 让 VPS 加入 tailnet（开启 `-verify-clients` 的前提，必做）

> **为什么必做**：`derper` 默认**不校验客户端**——只要别人知道 `derp.yourdomain.com`，就能拿你的香港 CN2 GIA 带宽当免费中继。CN2 GIA 流量很贵，这个洞必须堵。
> `-verify-clients` 会让 derper 通过**本机 tailscaled** 校验「连进来的是不是你 tailnet 的成员」，非成员直接拒。**前提是 VPS 本身也在 tailnet 里**。

```bash
# VPS 加入你的 tailnet（按提示在浏览器授权）
tailscale up
```

### 3. 准备域名和证书

把域名（假设 `derp.yourdomain.com`）A 记录解析到香港 VPS 的 IP。然后开放端口：

```bash
# derper 自动申请 Let's Encrypt 证书（TLS-ALPN-01 走 443，无需额外开 80）
ufw allow 443/tcp
ufw allow 3478/udp
```

### 🔒 4. 启动 derper（systemd 常驻，带 `-verify-clients`）

创建 `/etc/systemd/system/derper.service`：

```ini
[Unit]
Description=Tailscale DERP Relay
After=network.target tailscaled.service
Requires=tailscaled.service

[Service]
# 注意末尾的 -verify-clients：只给自己 tailnet 的成员中继，防止线路被白嫖
ExecStart=/root/go/bin/derper -hostname derp.yourdomain.com -certmode letsencrypt -a :443 -stun-port 3478 -verify-clients
Restart=always

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
systemctl daemon-reload
systemctl enable --now derper
systemctl status derper   # 确认 running
```

### 5. 验证

浏览器访问 `https://derp.yourdomain.com`，看到 DERP 文字页面即成功。

### ⚠️ 6. 给 DERP 加存活监控（SPOF 防护）

下方第二部分会设 `OmitDefaultRegions: true`，届时**整条链路只剩香港这一个中继**。国内→日本的 P2P UDP 直连大概率被打掉，所以你**基本全程吃这个中继**，它不是 fallback，是主干道。一旦 VPS 挂了 / 被打 / 证书过期，你人在国内现场就彻底连不上日本电脑。

- 用 UptimeRobot（或任意探活）监控 `https://derp.yourdomain.com`，VPS 宕机/证书过期前有告警。
- 心里清楚这是单点故障（SPOF）。

---

## 第二部分：让 Tailscale 使用你的自建 DERP

登录 Tailscale 管理后台（login.tailscale.com）→ **Access Controls**，在 JSON 配置中加入：

```json
"derpMap": {
  "OmitDefaultRegions": true,
  "Regions": {
    "900": {
      "RegionID": 900,
      "RegionCode": "hk",
      "Nodes": [
        {
          "Name": "hk1",
          "RegionID": 900,
          "HostName": "derp.yourdomain.com"
        }
      ]
    }
  }
}
```

> `OmitDefaultRegions: true` 表示**只用你的香港中继**，不再尝试连官方 DERP（官方那些在国内不稳）。
> 自建 RegionID 用 **900 及以上**是 Tailscale 的约定，别和官方 region 冲突。

### 🔒 同时配 ACL：只允许手机访问电脑的 RDP 端口

开了 RDP 后，tailnet 里**任何设备**默认都能敲日本电脑的 3389。在 ACL 里收紧（示例，按你实际 tag / 设备名改）：

```json
"acls": [
  {
    "action": "accept",
    "src":    ["tag:phone"],
    "dst":    ["tag:jp-pc:3389"]
  }
]
```

---

## 第三部分：日本电脑（Windows 10 Pro）

### 1. 装 Tailscale

下载 <https://tailscale.com/download/windows> 安装，登录**同一个账号**。

### 2. 开启 RDP 服务端

```powershell
# 管理员 PowerShell
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name "fDenyTSConnections" -Value 0
Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
```

### 🔒 强密码（必做）

RDP 在 tailnet 内可达，Windows 登录密码务必用强密码，**别用 4 位 PIN**。配合第二部分的 ACL，把暴露面收到最小。

### 3. 设置永不休眠（关键，否则断线无法唤醒）

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

### 4. 记下这台电脑的 Tailscale IP

```powershell
tailscale ip -4
# 形如 100.x.x.x，记下来
```

### ⚠️ 绝对不要做的操作

- **不要**在这台电脑启用 Tailscale 的「Exit Node / Use exit node」。
- 一旦启用，电脑所有外网流量会被导向 VPN，**会改变你的业务出口 IP**，破坏 Taobao / Xianyu 风控的行为一致性。
- 保持默认「仅内网互通」即可，你对外的日本本地 IP 纹丝不动。
- 补充澄清：`tailscale up --advertise-exit-node`（把电脑**宣告成**出口节点）本身**不改变**电脑自己的出口 IP，真正改 IP 的是某台设备去**「使用」**出口节点。你这个场景两个都不需要开。

---

## 第四部分：中国手机

1. 装 **Tailscale App**，登录同一账号。
   - ⚠️ **国区 Apple ID 通常搜不到 / 装不了 Tailscale**。出发前确认：要么用非国区 Apple ID 装好，要么 Android 直接 APK 侧载。这个现场才发现就抓瞎。
2. 装 RDP 客户端：iOS 用「Microsoft Remote Desktop」，Android 用同名或「RD Client」。
3. Tailscale 连上后，RDP 客户端新建连接，地址填日本电脑的 **100.x.x.x**（第三部分记下的那个），账号密码用 Windows 登录凭据。
4. 连上即可看到桌面。

---

## 执行顺序总结

1. **VPS**：装 Tailscale + Go → 编译 derper → 🔒 `tailscale up` 入网 → 配域名证书 → 🔒 systemd 常驻（带 `-verify-clients`）→ 验证 443 页面 → ⚠️ 加存活监控。
2. **Tailscale 后台**：改 ACL，加自建 derpMap（`OmitDefaultRegions`）+ 🔒 收紧 RDP 端口访问。
3. **日本电脑**：装 Tailscale → 开 RDP → 🔒 强密码 → 设永不休眠 → 记录 100.x.x.x → ⚠️ 确认没开 Exit Node。
4. **手机**：⚠️ 确认能装上 Tailscale → 装 RDP 客户端 → 连内网 IP。

---

## 出发前必做测试

**务必在日本本地先用手机流量（关 Wi-Fi）整链路测一遍**，确认能连通再出发。国内现场调试很被动。

---

## 关键提醒速查

| 项目 | 说明 |
|------|------|
| 不会改变电脑 IP | Tailscale 是新增虚拟网卡，原内网 IP 和公网出口 IP 都不变 |
| 出口 IP 保持日本本地 | 只要不开 Exit Node，业务流量出口完全不受影响 |
| 🔒 防白嫖 | derper 必须带 `-verify-clients`，VPS 须先 `tailscale up` 入网 |
| ⚠️ 单点故障 | `OmitDefaultRegions: true` 后香港中继是唯一通道，必须加存活监控 |
| 🔒 RDP 访问控制 | ACL 只放手机 → 电脑 3389 + Windows 强密码 |
| ⚠️ 客户端可装性 | 国区 App Store 可能装不了 Tailscale，提前用非国区 ID / APK 解决 |
| 永不休眠 | 必做，否则远程断线后无法唤醒电脑 |
| DERP 必须有域名 | 中继节点需域名 + Let's Encrypt 证书（TLS-ALPN-01 走 443），不能用纯 IP |
| 只用香港中继 | `OmitDefaultRegions: true`，避开国内不稳的官方 DERP |

---

## 附录 A：Win10 家庭版兜底 —— RustDesk

如果日本电脑是 **Win10 Home（无 RDP 服务端）**，把第三部分换成 RustDesk，其余（Tailscale + DERP）不变：

1. 日本电脑装 RustDesk 服务端，设置固定密码、开机自启。
2. （可选，更稳）RustDesk 也支持自建 ID/中继服务器（`hbbs`/`hbbr`），可一并放到香港 VPS，进一步摆脱公共服务器。
3. 手机装 RustDesk 客户端，通过 Tailscale 内网 IP（100.x.x.x）或 RustDesk ID 连入。
4. 同样**不开 Exit Node**，出口 IP 不变的前提不受影响。

> 注意：RustDesk 走的是它自己的协议，不依赖 Windows RDP；但 Tailscale 内网互通 + 不开 Exit Node 这套前提与主方案完全一致。
