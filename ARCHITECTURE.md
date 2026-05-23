# XDZHGL Architecture（脱敏作品集版）

> 这份文件描述系统架构、模块拆分、AI 决策链路、状态机和外部能力依赖。**不包含**真实账号、token、内部服务名、部署路径、密钥文件名、生产数据样本。

---

## 1. 系统定位

桌面端 Multi-Agent / Harness 平台，面向 Yahoo TW 拍卖业务的全链路自动化：

| 业务模块 | Agent 职责 |
|---------|----------|
| 账号监控 | 监控 Agent 周期性拉取账号状态（订单 / IM / 异常） |
| IM 客服 | 5 层 AI Subagent 协作：Commander 路由 → Writer 撰写 → Integration 整合 → Seller Q&A → Media Relevance |
| 自动刊登 | 读 Excel → 模板组合 → 多步表单提交 |
| 采购绑定 | 商品 ↔ 货源跨平台映射，缺货状态实时回流 |
| 物流上传 | 第三方跨境物流 ERP 自动出货 |
| 运营通知 | Telegram 多 Bot 拆分通知 + 主管审计 |

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│              Tkinter Desktop GUI  (app.py)                           │
│  monitor / AI customer service / auto-publish / batch ops / shipping │
└────────────────────────────────────┬─────────────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
┌──────────────┐         ┌──────────────────────┐         ┌────────────────┐
│  Tool Layer  │         │  Multi-Agent Layer   │         │ External Sinks │
│ (HTTP/WS 工 │         │  (5 Subagents)       │         │ (5 个独立 TG   │
│  具层抽象)   │◄────────│ Commander / Writer  │◄────────│  Bot + Cloud   │
│             │         │ Integration / SellerQ│         │  Worker bus)   │
│             │         │ / MediaRelevance     │         │                │
└──────┬───────┘         └──────────┬───────────┘         └────────────────┘
       │                            │
       │                            ▼
       │                 ┌──────────────────────┐
       │                 │ Training Pipeline    │
       │                 │ (per-conv JSONL)     │
       │                 └──────────────────────┘
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Conversation State Machine (per-buyer, per-account, persistent)     │
│  PENDING_AI → PREVIEW_SENT → DONE                                    │
│  PENDING_AI → NEED_SELLER → AUTO_ASKING → WAIT_SELLER → INTEGRATE    │
│           → DONE  ←─ 24h reattach window ─→ WAIT_SELLER              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 文件架构（脱敏）

### 根目录
| 文件 | 作用 |
|------|------|
| `app.py` | 主程序入口，Tkinter GUI |
| `pack_update.py` | 打包代码 zip 上传 Cloudflare Worker |
| `updater.pyw` | 后台自动更新守护进程（atomic two-phase commit） |
| `rollback.py` | 版本回滚工具 |

### 配置文件（公开 repo **不含真实值**）
| 用途 | 说明 |
|------|------|
| 账号列表配置 | 运行时密钥，**未上传** |
| 全局设置 | AI 模型 / 并发数 / 浏览器路径等，**已脱敏** |
| Telegram Bot 配置 | 5 个 bot token，**未上传** |
| Worker 配置 | KV / D1 / R2 endpoints，**未上传** |

### core/ — 按功能分组

#### 监控 & 账号
- `monitor.py` — 并发账号监控
- `scraper.py` — 页面 JSON 状态提取
- `accounts.py` — 账号 / 设置 JSON 读写（原子写、线程安全）
- `profile_lock.py` — 浏览器 Profile 文件锁

#### AI 客服（核心 Agent 协作）
- `tg_conversation.py` (~10k 行) — **最重要的文件**：5 个 Subagent 协作的对话状态机
- `ai_forwarder_feature.py` — Commander Agent + AI 调用层 + fallback chain
- `yahoo_im_fulltext.py` — 历史消息加载
- `mercari_seller_comment.py` / `xianyu_seller_chat.py` — 卖家询问 Tool

#### Telegram Bot（5 个独立功能拆分）
| Bot | 用途 |
|-----|------|
| AI客服 Bot | 买家消息通知 + AI 草稿审核 + 9 类 HITL 指令 |
| 管理 Bot | 版本管理、日志查看、广播 |
| 采购 Bot | 采购订单通知 |
| 运营 Bot | 运营团队通知 |
| 主管 Bot | 主管审计（含训练数据静默上传） |

#### Tool Layer（HTTP/WebSocket 工具层抽象）
- `yahoo_im_*.py` — Yahoo IM 协议适配（BOSH/XMPP + JWT + 媒体上传）
- `goofish_*.py` — 闲鱼 WebSocket + HTTP 协议适配
- `mercari_*.py` — Mercari 协议适配
- `myauc_http.py` — Yahoo 商品页 HTTP 客户端
- `merch_http_ops.py` / `publish_http_ops.py` / `order_http.py` — Yahoo 商品/订单 HTTP 操作
- `跨境物流 ERP 上传 feature` — 第三方物流系统对接（OCR 验证码登录）

#### Harness 工程
- `training_collector.py` — 训练数据 trajectory 沉淀（SFT / DPO ready）
- `client_runtime_compat.py` — 客户端运行时兼容层（公开版为 Stub）
- `network_health.py` — 网络容错与重试
- `d1_reconcile.py` — Cloudflare D1 同步（货源数据库）
- `api_server.py` — 本地 API 服务

---

## 4. AI 客服对话流程（核心 Agent Loop）

```
买家新消息
  │
  ├─ 1. 提取商品编号 → 查 D1 货源数据库（含采购链接 / 可购买状态）
  │
  ├─ 2. 抓取商品页（运费 / 定价 / 商品状况）
  │
  ├─ 3. TG 通知用户："📩 新消息... 🤖 AI 分析中..."
  │
  ├─ 4. Commander Agent 决策
  │     输入: dialog + product spec + Yahoo state + sourcing avail + images
  │     输出: {action, confidence, reason, seller_attach_images_indices, pricing_hint}
  │     action ∈ {AUTO_REPLY, NEED_SELLER, NO_REPLY, NEED_PRODUCT_DATA, NEED_CLARIFY}
  │
  ├─ 5a. AUTO_REPLY → Writer Agent 生成回复
  │       → TG 预览 → 用户 ok / edit / mod / skip
  │       → 确认后 Yahoo IM 发送
  │
  ├─ 5b. NEED_SELLER → Seller Question Agent 生成提问
  │       ├─ Mercari: 日文 → 自动留言 → 定时检查回复
  │       ├─ 闲鱼: 简中 → 自动发送 → 定时检查回复
  │       └─ 卖家回复 → Integration Agent 整合 → 给买家最终回复
  │
  └─ 5c. NO_REPLY → 简短感谢 → 用户确认
```

### ConvPhase 状态机
```
PENDING_AI → PREVIEW_SENT → DONE                              (自动回复)
PENDING_AI → PREVIEW_SELLER_QUESTION → AUTO_ASKING_SELLER
           → WAIT_SELLER → PREVIEW_SELLER → DONE              (问卖家流程)
PENDING_AI → ERROR (可 retry)
任何阶段   → EXPIRED (skip / 超时)
DONE       ← 24h reattach window → WAIT_SELLER                (卖家迟到补发)
```

### TG 用户 HITL 指令（9 类）
| 指令 | 阶段 | 作用 |
|------|------|------|
| `ok` | PREVIEW_*  | 确认发送 |
| `edit:内容` | PREVIEW_* | 替换内容后发送 |
| `reply:内容` | 任何阶段 | 直接回买家 |
| `mod:指令` | PREVIEW_SENT | AI 根据指令修改草稿 |
| `ask` | PREVIEW_SENT | 转问采购方卖家 |
| `skip` | 任何阶段 | 跳过不回复 |
| `manual` | PREVIEW_SELLER_QUESTION | 切换手动模式 |
| `retry` | ERROR | 重试 |
| `read` | PREVIEW_SENT | 只消红点不回复 |

**注意**：全角冒号 `：` 与半角 `:` 都支持（已 normalize 处理）。

---

## 5. AI 决策架构（5 层 Subagent）

所有消息路由决策由 AI LLM 完成（v4.7.29 起移除了关键词快速规则）。

| Layer | 角色 | 输入 | 输出 |
|-------|------|------|------|
| Commander | 路由判断 | dialog + product spec + Yahoo state + sourcing + images | strict JSON 含 5 类 action 之一 |
| Writer | 撰写买家回复 | Commander 输出 + dialog + images | 繁体口语化回复 |
| Seller Question | 生成给卖家的问题 | Commander rationale + product spec | 日文/简中（按平台） |
| Integration | 整合卖家回复 | 买家原问 + 卖家回复 + seller dialog | 买家可见最终回复 |
| Media Relevance | 判断卖家发的图要不要转 | per-conv 买家 context + seller dialog + 媒体 URL | `{forward: bool, reason}` |

**关键 prompt 规则**（节选）：
- `可購買=否` → 必须 AUTO_REPLY（告知买家没货），最高优先级
- 议价 / 出价 → AUTO_REPLY（用 Yahoo 定价婉拒）
- 运费 → AUTO_REPLY（默认免运）
- 退换 / 投诉 / 物流查询 → NEED_SELLER
- 商品描述中找不到的信息 → NEED_SELLER

---

## 6. 训练数据 Pipeline（蒸馏用户行为）

每对话独立 JSONL trajectory，记录 30+ action types：

```
conv:new → ai:commander_decide → ai:writer_draft → user:edit_text → user:ok
        → send:yahoo → conv:end
                              ↑
                       DPO signal:
                       (ai_draft, user_edit) pair
```

**设计特性：**
- per-conv 独立文件，对话结束才提交完整 trajectory（避免污染）
- state delta 避免 O(N²) context 膨胀
- restart recovery 从断点恢复
- 12h sweep 强制 finalize 卡死对话
- TG supervisor bot 静默上传（不刷主管聊天窗）
- Ready for SFT (state→action) 或 DPO ((ai_draft, user_edit) pair)

**长期目标**：2 周累积 → fine-tune 路由分类器 + 文本 DPO → 逐步收敛 7 按钮 UI 到「1 按钮 + 按需展开」。

---

## 7. 客户端自动更新（Atomic Two-Phase Commit）

为根治线上用户 AV 干扰导致的 partial-apply 永久卡死 case 而建：

```
Phase 0: zip CRC verify              → bad zip = fail-fast
Phase 1: 全写到 {file}.update_tmp    → 原文件不动
Phase 2: 一次性 os.replace()         → 原子换名，全新或全旧
Failure: cleanup tmps + 不写 current_version → 下轮自动重试
Startup: 扫 .update_tmp 残骸 → 自动清理
```

**对比朴素 `for f in files: write(f)`：**
- 任一文件失败不会留下半生不熟状态
- 进程中途 crash 不会污染文件树
- AV scanner 干扰不会导致永久卡死

---

## 8. Harness 工程其他亮点

### 客户端运行时兼容层
统一不同机器、不同浏览器版本、不同网络环境下的运行参数，减少部署差异导致的任务失败。**公开仓库为接口 Stub，实现细节未上传**。

### 网络容错语义
- 3s / 8s / 18s 渐进 backoff + 完整 payload replay
- 区分 network blip vs 真 fail（不同 cascade pause counter）
- 服务端 5xx 维护窗口降级为 slow-poll，200 恢复时自动退出

### 资源锁原子隔离
- 发布锁 vs 监控锁：publish 流持有 monitor lock，防止 60s 监控 poll 在 publish 中段抢同一 account
- Profile 锁：避免同账号多客户端冲突

---

## 9. Tech Stack

| Layer | Tech |
|-------|------|
| Language | Python 3.12.7（embedded runtime） |
| GUI | Tkinter + customtkinter |
| HTTP | HTTP / WebSocket 工具层 |
| LLM | GPT-5.5 / 5.4 fallback chain，OpenAI-compatible endpoint |
| Telegram | 5 bot + Cloudflare Worker KV-relay |
| Cloud | Cloudflare Workers + R2 + D1 + KV |
| Crypto | pycryptodome（IM JWT 用 AES-CBC） |
| OCR | 本地 OCR + GPT-vision 兜底（验证码登录） |
| Media | imageio-ffmpeg + AWS Sig V4 |

---

## 10. 脱敏说明

**已替换为 placeholder：**
- API keys, Token, Bot token, D1 token
- 手机、email、真名、chat ID、IP、webhook GUID
- 真实账号 ID（统一用 `ACCOUNT_NN` 形式）

**完全未上传：**
- 运行时配置 JSON（accounts / settings / tokens / relay config）
- Profile 数据、对话历史、训练 dump、生产日志
- 客户端运行时兼容层的真实实现（公开版为 Stub）

**保留：**
- 完整的 Agent 协作逻辑
- 状态机定义
- Tool Layer 接口抽象
- Harness 架构

---

## 11. 生产规模

- **实盘维护 20+ Yahoo 账号**，系统按 50+ / 100+ 多账号并发场景设计
- 5 个独立 Telegram Bot 拆分（功能隔离）
- 11 用户日活，单日 ~30 万 hub API calls
- v6.1.65（自 v6.0 起 65+ 次小版本迭代，持续 6+ 个月）
