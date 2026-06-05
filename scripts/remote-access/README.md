# 远程接入执行套件 (Tailscale + 自建 DERP)

把 [`../../REMOTE_ACCESS_RUNBOOK.md`](../../REMOTE_ACCESS_RUNBOOK.md) 的步骤落成可直接运行的脚本/配置。
在每台真机上跑对应那个文件即可，不用手敲。

| 顺序 | 文件 | 在哪台机器跑 | 作用 |
|------|------|--------------|------|
| 1 | `01-vps-derper-setup.sh` | 香港 VPS (Ubuntu/Debian) | 装 Tailscale+Go、编译 derper、入网、写 systemd、起带 `-verify-clients` 的 DERP |
| 2 | `02-tailscale-acl.hujson` | Tailscale 后台 (浏览器) | 合并 ACL：只用香港 DERP + 收紧手机→电脑 3389 |
| 3 | `03-windows-rdp-setup.ps1` | 日本电脑 (Win10 Pro，管理员 PS) | 开 RDP、永不休眠、打印 100.x.x.x |
| 4 | 手机 | 中国手机 | 装 Tailscale + RDP 客户端，连 100.x.x.x |

## 执行前必改的占位符

- `01-...sh` 顶部 `DOMAIN`（必改）；如要无人值守入网可填 `TAILSCALE_AUTHKEY`。
- `02-...hujson` 里的 `derp.yourdomain.com` 和 `your-login-email@example.com`。

## 红线（别碰）

- **不开 Exit Node**：开了会改变日本电脑业务出口 IP，破坏 Taobao/Xianyu 风控行为一致性。
- VPS 必须先 `tailscale up` 入网，`-verify-clients` 才生效（脚本已包含）；否则 CN2 线路会被白嫖。

## 出发前

务必在日本本地用手机流量（关 Wi-Fi）整链路测通再走。国内现场调试很被动。

## Win10 家庭版

家庭版无 RDP 服务端，`03` 脚本会自动检测并提示改用 RustDesk（见 runbook 附录 A）。
