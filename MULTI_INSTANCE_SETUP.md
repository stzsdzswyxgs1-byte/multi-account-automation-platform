# 多同事 / 多軟件 Instance 部署指南

## 架構

```
@example_forum_bot (共用)
   ↓
Cloudflare Worker webhook
   ↓  按 from.id 分發 KV
   ├─ alice KV queue
   ├─ bob KV queue
   └─ charlie KV queue

每同事自己電腦:
   - 自己 accounts.json (只列他負責的帳號)
   - 自己 settings.json (tg_chat_id = 自己 TG ID)
   - 自己 tg_relay_config.json (user_id = 自己 TG ID)
   - run.bat → 軟件 polling 自己 KV queue → 只看見自己的事
```

## Worker 端設定（主管做一次）

@example_forum_bot 必須走 webhook → Worker 分發。**Worker 端需新增 `forum` bot type 支援**。

### 1. 設 @example_forum_bot webhook

```bash
curl "https://api.telegram.org/bot<TG_BOT_TOKEN_REDACTED>/setWebhook?url=https://square-river-tg.<PHONE_REDACTED>.workers.dev/webhook/forum&secret_token=<WORKER_API_KEY_REDACTED>"
```

### 2. Worker 端加 forum bot type 分發邏輯

Worker 端原本支援 4 個 bot type (ai_cs, ops, manage, purchase)，要加 `forum`：

**Worker JS（伪代码）**：

```javascript
// /webhook/forum POST handler
async function handleForumWebhook(update, env) {
    // 取 from.id 作為 user_id 路由
    const msg = update.message || update.callback_query?.message;
    if (!msg) return;

    // 對 supergroup 訊息:from.id 是發訊人 TG user_id
    const fromId = String(
        update.message?.from?.id ||
        update.callback_query?.from?.id ||
        ""
    );
    if (!fromId) return;

    // 寫進該 user 的 forum queue
    const key = `forum:${fromId}`;
    const existing = JSON.parse(await env.KV.get(key) || "[]");
    existing.push(update);
    // 保留最近 50 條
    await env.KV.put(key, JSON.stringify(existing.slice(-50)));
}

// /poll/forum/{user_id} 已有的 generic handler 自動處理
```

## 同事 onboarding 流程

### Step 1：主管把軟件交給同事

把整個 `XDZHGL2.0指令版` 資料夾打包 zip 給同事。同事解壓到自己電腦。

### Step 2：同事建 TG group + 邀 bot

1. 建 TG supergroup（隨便命名，如「Alice Yahoo 客服」）
2. 群組設定 → 啟用 **Topics**
3. 加 **@example_forum_bot** 進 group + 設 **Admin**（Manage Topics 權限）

### Step 3：同事打 `/myid` 拿 ID

在自己 group 內打：
```
/myid
```

bot 回：
```
📇 身份資訊
  你的 TG user_id: 111222333
  這 group chat_id: -1001234567890

📌 填到軟件 GUI:
  settings.json → tg_chat_id: 111222333
  settings.json → tg_forum_chat_id: -1001234567890
  tg_relay_config.json → user_id: 111222333
```

### Step 4：填配置檔

**settings.json**:
```json
{
  "tg_chat_id": "111222333",
  "tg_forum_chat_id": "-1001234567890",
  "tg_forum_enabled": true,
  "forum_bot_use_kv": true,    ← 重要,走 KV 模式
  ...
}
```

**tg_relay_config.json**:
```json
{
  "user_id": "111222333"
}
```

**accounts.json** — 只列同事負責的帳號：
```json
[
  {"profile_id": "kinhuaw168", "refresh_sec": 600, ...},
  {"profile_id": "chen749", "refresh_sec": 600, ...}
]
```

### Step 5：啟動

雙擊 `run.bat`，看 log 有：
```
[TG-FORUM] polling 已啟動(KV 中轉模式,綁定 TG ID=111222333)
```

完成。從此 Alice 的軟件只接收 kinhuaw168/chen749 的買家對話。

## 隔離保證

| 層 | 機制 | 結果 |
|---|---|---|
| 1. 軟件層 | accounts.json 各自一份 | Alice 軟件只 monitor 她的帳號 |
| 2. KV 路由 | Worker 按 from.id 分發 | Alice KV queue 只有她的事件 |
| 3. 軟件 polling | 只 poll 自己 user_id | Alice 軟件只拉自己 queue |

任一層失靈都不會 cross-channel — 三層硬隔離。

## 主管權限

主管軟件 instance（你 <SUPERVISOR_CHAT_ID>）跟同事一樣設定，但 accounts.json 列**所有**帳號（或只列你個人負責的）。`tg_forum_chat_id` 是你自己的主管 group。

主管的 group 也只看自己 accounts.json 內的對話 — 跟同事一樣的隔離邏輯。

## 故障排除

**Q: 啟動 log 顯示「polling 已啟動(direct getUpdates)」而非 KV**
A: settings.json 沒設 `forum_bot_use_kv: true`，或 tg_relay_config.json 沒 user_id。

**Q: KV 模式但軟件接不到任何訊息**
A: Worker 端沒部署 forum bot webhook handler。檢查 `https://api.telegram.org/bot{token}/getWebhookInfo` 看 webhook 是否設了。

**Q: 兩個軟件 instance 同時跑 → 撞 409**
A: 表示還在 direct 模式。確認兩邊都設 `forum_bot_use_kv: true` 走 KV。
