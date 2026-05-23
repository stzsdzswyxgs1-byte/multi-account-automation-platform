# XDZHGL — 桌面端 Multi-Agent / Harness 平台（Yahoo 拍卖业务实战）

> 一套**生产环境运行至今**的桌面端 Agent 系统。Model + Harness = Agent — 本 repo 是 Harness 部分的完整实现：从架构、Multi-Agent 协作、Tool Layer 抽象、Memory / Context Engineering，到自动更新、可观测性、SFT/DPO 训练数据回流的全栈基础设施。

> **现状**: v6.1.65，实盘维护 20+ Yahoo 账号、按 50+ / 100+ 多账号并发场景设计，5 层 AI Agent 协作，11 个用户日活，单日 ~30 万 hub API calls。从 2024 年起持续迭代生产中。

---

## 这个项目跟「Agent Harness 研发工程师」岗位的对应关系

| 岗位要求 | 在这个项目里的对应实现 | 关键代码 |
|---------|---------------------|---------|
| **桌面端 Agent 产品全流程** | Tkinter GUI (~150KB `app.py`) → 调度层 → Agent 决策 → Tool 调用 → 状态持久化，端到端自研，没有用任何 Agent 框架 | `app.py`, `core/monitor.py` |
| **架构设计与技术选型** | 自己定义了 Agent / Tool / Memory / Context 各层抽象。从早期 Playwright + 浏览器自动化，逐步重构为 HTTP / WebSocket 工具层以提升多账号并发下的资源效率 | `core/im_http_ops.py`, `core/yahoo_im_*.py` |
| **Multi-Agent / Subagent** | **5 层独立 AI Agent**，每个 agent 有独立 prompt + 结构化输出 schema + 自己的 context window：<br>① Commander（路由决策）<br>② Writer（撰写买家回复）<br>③ Seller Question Generator（生成问卖家的问题，按平台分中/日文）<br>④ Integration（卖家回复后整合再发给买家）<br>⑤ Media Relevance Judge（判断卖家发来的图要不要转发给买家） | `core/ai_forwarder_feature.py` (Commander)<br>`core/tg_conversation.py` (其余 4 个 Subagent) |
| **Agent Loop** | 完整对话状态机：`PENDING_AI → PREVIEW_SENT → DONE`、`PENDING_AI → NEED_SELLER → AUTO_ASKING → WAIT_SELLER → INTEGRATE → DONE`，每个状态可重启恢复，24h reattach 窗口处理迟到的卖家回复 | `core/tg_conversation.py` (~10k 行) |
| **Tool Use** | 将多平台业务接口（Yahoo IM、Mercari、闲鱼、第三方跨境物流 ERP、Cloudflare D1、5 个独立 Telegram Bot + forum supergroup、本地 API）统一封装为 **Tool Layer**：每个 Tool 自带独立的认证配置、重试策略、幂等保护、错误分类、日志审计和人工审核入口；模型只产出结构化 action，执行层负责所有副作用与稳定性保障 | `core/yahoo_im_*.py`<br>`core/im_http_ops.py`<br>`core/mercari_*.py` |
| **Memory** | • 对话级 Memory: 每对话独立 `state.json`，跨进程重启不丢<br>• 训练数据 trajectory JSONL（state→action 序列）每对话一份<br>• 24h reattach 窗口处理跨日卖家回复<br>• 8h 对话过期 + 60s dedup 窗口 + 1h 训练数据 flush | `core/training_collector.py`<br>`core/tg_conversation.py:_load_state` |
| **Context Engineering** | 5 个 Agent 各自有不同 context 拼接策略：<br>• Commander: dialog + product spec + Yahoo 页面状态 + 货源状态 + 图片<br>• Writer: Commander 输出 + 完整对话 + 图片<br>• Seller Question: Commander 的 rationale + 商品规格<br>• Integration: 买家原问 + 卖家回复 + 完整卖家 dialog<br>• Media Relevance: per-conv 买家 context + 卖家 dialog + 媒体 URL（多对话隔离） | `core/ai_forwarder_feature.py`<br>`core/tg_conversation.py:_build_*_context` |
| **Prompt Engineering** | 5 套独立 prompt，每套带 JSON schema 输出约束。每条 prompt 都是从真实失败案例迭代出来（如 `bool("false") == True` 这种 LLM 输出陷阱，靠 property-based test 抓到） | 各 Agent 文件 + `tg_conversation.py` |
| **Harness Engineering** | • **Atomic 两阶段提交自动更新器**（zip CRC verify → `.update_tmp` → `os.replace` 全原子，failure 不留半推状态）<br>• **客户端运行时兼容层**（统一不同机器、浏览器版本、网络环境下的运行参数，减少部署差异导致的任务失败）<br>• **网络容错重试** (3s/8s/18s backoff + full payload replay)<br>• **服务端 5xx 维护窗口异常处理**：不 crash，降级为 slow-poll，恢复时自动退出<br>• **发布锁 vs 监控锁原子隔离**（防止 publish 流被 60s 监控 poll 抢资源） | `updater.pyw`<br>`core/needs_relogin.py` |
| **MCP（外部能力接入）** | 实质上做了 MCP 等价抽象 — 把 Cloudflare Worker (D1 / KV / R2) 作为统一外部能力总线，让 Agent 通过单一 API surface 访问 product DB / 训练数据存储 / 更新分发 | `core/d1_reconcile.py`<br>`core/api_server.py` |
| **真实任务反馈持续迭代** | 130+ 个 tracked task，每个版本 bump 都有 audit data 支持。例如 `4.0.74 PauseException 不吞` 是从 5/11 cascade 生产事故反推出来的根因 | git 历史 + 版本变更注释 |
| **对模型行为有品味** | • 不同 Agent 用不同 prompt 形态（Commander = strict JSON schema + reasoning trace；Writer = few-shot 口语化；Seller Question = bilingual instruction tuning）<br>• Commander rationale 字段专门给 Writer 看，作为 hand-off context<br>• AI 输出失败时不 retry 同 prompt，而是 fallback model + 降级简化（gpt-5.5 → gpt-5.4 → 直连降级通道）<br>• 训练数据回流：每个 user edit 都作为 DPO pair 记录 | `ai_forwarder_feature.py:_FALLBACK_CHAIN`<br>`training_collector.py` |
| **自己是 Agent 重度用户** | 这整个项目就是用 AI Agent 工具开发出来的 — 包括 Claude Code 的 plan-then-execute 流、tracked task 编号、edge-case 审计 30+ 场景的工程纪律，都是从 Agent 实战里磨出来的 | 整个 repo 的工程组织 |

---

## 1. 架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                  Tkinter Desktop GUI  (app.py ~150KB)                    │
│       monitor / AI customer service / auto-publish / batch ops          │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
┌──────────────┐         ┌──────────────────────┐         ┌────────────────┐
│  Tool Layer  │         │  Multi-Agent Layer   │         │ External Sinks │
│ (HTTP / WS)  │         │  (5 Subagents)       │         │                │
├──────────────┤         ├──────────────────────┤         ├────────────────┤
│ • Yahoo IM   │◄────────│ Commander  (路由)    │◄────────│ Telegram (5    │
│ • 媒体 CDN   │         │ Writer     (撰写)    │         │   bots split   │
│ • Xianyu     │         │ Integration(整合)    │         │   by function) │
│ • Mercari    │         │ SellerQ   (问卖家)   │         │ Cloudflare:    │
│ • myauc HTTP │         │ MediaRelv (媒体判断) │         │   R2 / D1 / KV │
│ • 跨境物流ERP │        └──────────┬───────────┘         │ Xianyu / Mercari│
│ • merch HTTP │                    │                     │ 物流 ERP        │
└──────┬───────┘                    │                     │ Google Sheets   │
       │                            ▼                     │ 企业微信 webhook│
       │                 ┌──────────────────────┐         └────────────────┘
       │                 │ Training Pipeline    │
       │                 │ (per-conv JSONL,     │
       │                 │  SFT/DPO ready)      │
       │                 └──────────────────────┘
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Conversation State Machine (per-buyer, per-account, persistent)     │
│  PENDING_AI → PREVIEW_SENT → DONE                                    │
│  PENDING_AI → NEED_SELLER → AUTO_ASKING → WAIT_SELLER → INTEGRATE    │
│           → DONE  ←─ 24h reattach window ─→ WAIT_SELLER  (v6.1.65)   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 推荐阅读顺序（30 分钟摸完核心）

| # | 文件 | 你会看到什么 |
|---|------|-------------|
| 1 | `ARCHITECTURE.md` | 完整架构、模块地图、对话状态机、外部服务清单 — **从这开始** |
| 2 | `core/tg_conversation.py` (~10k 行) | 状态机大脑：5 个 Subagent 协作、24h reattach、训练数据 hook |
| 3 | `core/ai_forwarder_feature.py` | Commander Agent — Multi-modal routing + fallback chain |
| 4 | `core/training_collector.py` | 训练数据 trajectory pipeline（SFT / DPO ready） |
| 5 | `updater.pyw` | Atomic two-phase commit updater |

---

## 3. 技术亮点

### 3.1 Multi-Agent 协作设计

每个 Agent 不是简单的 "function call" — 是有独立 context、独立 prompt、独立失败恢复策略的实体：

| Agent | 输入 | 输出 | 失败处理 |
|-------|-----|------|---------|
| **Commander** | dialog + product spec + Yahoo state + sourcing avail + images | `{action, confidence, reason, seller_attach_images_indices}` | JSON parse fail → fallback model + simplified prompt |
| **Writer** | Commander 输出 (作为 hand-off context) + dialog + images | 买家可见回复 (繁体, tone-matched) | 草稿太短/含 placeholder → 自动 regenerate |
| **Seller Question** | Commander rationale + product spec | 给卖家的提问（闲鱼 = 简中, Mercari = 日文） | 语言 detection fail → 默认走该平台原生语言 |
| **Integration** | 买家原问 + 卖家回复 + 完整 seller dialog | 回买家的最终回复 | 整合失败 → 转人工审核（TG ask） |
| **Media Relevance** | per-conv 买家 context + 卖家 dialog + 媒体 URL | `{forward: bool, reason}` | LLM 不确定 → 默认不 forward 保守处理 |

**多对话隔离**：同一个卖家被多个买家询问时，每个买家的 Media Relevance 判断都独立（不能广播）。

### 3.2 Tool Layer 抽象

把所有外部业务接口统一封装为 Tool，让 Agent 通过结构化 action 调用：

| Tool | 协议层 | 工程价值 |
|------|--------|---------|
| **Yahoo IM** | BOSH/XMPP + AES-128-CBC JWT 解密、52 类 IQ protocol、19 类 stanza method | 取代 200MB Playwright + Chrome session，10× 速度，无 profile 锁争用 |
| **媒体 CDN** | S3 STS 凭证 → AWS Sig V4 → finalize → send 四步链 | 卖家图片/视频 → 买家全 HTTP 流转，>30s 视频自动切段 |
| **Xianyu** | H5 WebSocket 协议、token rotation、smart-reply via `bizTag.taskName` | 实时卖家消息摄入 + 转发买家 |
| **Yahoo myauc** | HTML → JSON 状态提取、5xx retry 语义、ATS 负载均衡兼容 | 监控完全脱离 Chrome，纯 HTTP 客户端 |
| **跨境物流 ERP** | 验证码登录（OCR + AI 兜底）、session token 管理 | 100+ 单/日自动出货 |

**每个 Tool 都有：**
- 独立认证配置（避免 cross-tool 污染）
- 重试策略（指数 backoff + 完整 payload replay）
- 幂等保护（dedup window + idempotency key）
- 错误分类（区分 network blip vs 真 fail）
- 日志审计（cookie diff / 500 响应分离 dump）
- HITL 入口（任何阶段都可人工 override）

### 3.3 Harness Engineering — 这是岗位标题里的关键字

写完核心 Agent 后，**剩下 60% 工作都是 Harness**：

1. **Atomic 两阶段提交更新器**（因为同事机器上观察到 partial-apply 事故才建的）
   - Phase 0: zip CRC verify → bad zip fail-fast
   - Phase 1: 全写到 `.update_tmp`（原文件不动）
   - Phase 2: `os.replace()` 原子换名（要么全新要么全旧）
   - 启动时清理 `.update_tmp` 残骸（防 crash-during-update）

2. **客户端运行时兼容层**
   - 统一不同机器、不同浏览器版本、不同网络环境下的运行参数
   - 动态读取本机 Chrome 版本信息，按需配置请求 header
   - 减少因部署差异导致的任务失败

3. **网络容错语义**
   - 3s / 8s / 18s 渐进 backoff，**带完整 payload replay**
   - 区分 network blip vs 真 fail，blip 不计入 cascade pause counter

4. **训练数据 Pipeline**（蒸馏用户行为，最强 Harness 工程）
   - 每对话独立 JSONL trajectory：`conv:new → ai:commander → ai:writer → user:edit → user:ok → send → conv:end`
   - `(ai_draft, user_edit)` 自然形成 DPO pair
   - 通过 TG supervisor bot `sendDocument + disable_notification` 静默上传
   - 设计目标：2 周累积数据 → fine-tune 一个路由分类器 + 文本 DPO，逐步把 7 按钮 UI 收敛到「1 按钮 + 按需展开」

### 3.4 客户端稳定性工程

- **per-account 异常状态机**：服务端 5xx 维护窗口不 crash，降级为 slow-poll 模式，200 恢复时自动退出
- **发布锁 / 监控锁原子隔离**：publish 流持有 monitor lock，防止 60s 监控 poll 在 publish 中段抢同一 account
- **AI 输出健壮性**：`bool("false") == True` 这种 LLM 输出陷阱用 property-based test 抓出来

---

## 4. 生产规模数字

| 指标 | 数字 |
|------|-----|
| 实盘维护 Yahoo 账号 | **20+ 个** |
| 系统支持并发规模 | **设计支持 50+ / 100+ 多账号** |
| 监控目标 RPS | ≤0.5 req/sec（动态 stagger） |
| Telegram Bot 拆分 | **5 个**（AI客服 / 采购 / 管理 / 运营 / 主管） |
| 对话状态过期 | 8h |
| 卖家回复 reattach 窗口 | 24h |
| Dedup 窗口 | 60s |
| 训练数据 flush | 1h |
| Updater 轮询 | 5min（连续失败 → 30min backoff） |
| 视频自动切段 | 30s 安全边界 |
| 跨用户部署 | 11 user，多个不稳定 VPN 环境 |

---

## 5. 工程纪律（写给招聘方）

招聘要求里 "**对模型行为有品味**" + "**真实任务反馈持续迭代**" — 这部分不容易在 README 里 demo，但留了痕迹：

1. **不在没有 explicit approval 时推生产** — AI 协作 layer 上是硬规则，refuse 任何 unproven hypothesis
2. **每个非平凡变更都做 30+ edge-case 审计** — 包括 AV scanner 干扰、部分文件系统写入这种 hostile case
3. **向后兼容偏执** — 每次 settings schema 变更都迁移老字段；老函数名保留 alias；持久化状态不破跨版本
4. **单元测试覆盖关键逻辑** — atomic updater、reattach window 边界、AI JSON 解析
5. **幂等设计** — D1 同步、batch unshelve、训练上传都设计成可重复执行
6. **多层 logging + 诊断 dump** — cookie diff、500 响应、训练事件 各自独立可寻址

---

## 6. Tech Stack

| 层 | 技术 |
|---|------|
| Language | Python 3.12.7（embedded runtime，从 R2 自举） |
| GUI | Tkinter + customtkinter |
| HTTP | HTTP/WebSocket 工具层，curl_cffi + requests |
| Browser auto (legacy paths) | Playwright（大部分 path 已被 HTTP 工具层替换） |
| LLM | GPT-5.5 + GPT-5.4 fallback chain，自定义 OpenAI-compatible endpoint |
| Telegram | 5 bot + Cloudflare Worker KV-relay |
| Cloud | Cloudflare Workers + R2（更新分发 + Python runtime）+ D1（商品 DB）+ KV |
| Crypto | pycryptodome（Yahoo IM JWT 用 AES-CBC） |
| OCR | 本地 OCR + GPT-vision 兜底 |
| Media | imageio-ffmpeg（视频探测 + 切段）+ AWS Sig V4（S3 上传） |

---

## 7. Sanitization 说明

这是脱敏快照：

- API keys / Token → 全部 placeholder 化
- Telegram bot tokens / chat ID → 全部 placeholder
- 个人手机 / email / 真名 / webhook GUID → 全部 placeholder
- Config JSON (运行时 secrets / 账号列表 / token files) → 完全排除
- Profile 数据 / 对话历史 / 训练 dump → 完全排除
- 客户端运行时兼容层的真实实现 → 替换为接口 Stub

**代码结构、Agent 协作逻辑、Harness 架构 100% 保留** — 招聘方可以直接 review。

---

## 8. 如果你只看 5 分钟

读这两段就够：
- 上面的「**这个项目跟岗位的对应关系**」表
- `core/tg_conversation.py` 里随便挑一个 Agent 方法（建议 `_run_ai_writer` 或 `_ai_judge_seller_media_relevance`）

如果还有 30 分钟，按 §2 的推荐阅读顺序走一遍。
