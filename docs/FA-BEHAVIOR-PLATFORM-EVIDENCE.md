# FA 行為設計：官方平台契約與證據

> **查證日期：2026-09-15。這是公開官方文件研究，不是本次 Azure 實測。**
>
> 本文不盤點 repository 程式，不表示 FA 已接上、部署或驗收任何能力。既有 Context API、私人 sandbox、Chromium profile 與 Blob 備份結果由主代理另外判讀。沒有執行 Azure live resource 查詢、模型呼叫、部署、套件或技能安裝、routine CRUD、外部訊息傳送、commit 或 push。

## 0. 怎麼讀這份文件

- **官方文字**：官方文件直接陳述的行為，或指定 commit 中確實存在的介面。附來源 ID、短引文或原始欄位。
- **設計推論**：從契約推出的 FA 設計責任，不是假裝平台已提供。
- **未驗收**：官方有文件，但本次沒有執行驗證；`NOT_CONFIRMED` 另表示本次有界來源不足以確認該能力或介面，應停止推定。
- **產品預覽能力需版本固定**：至少固定 container protocol、AgentServer adapter/Core、Copilot SDK 與配套 CLI/runtime、Memory API version、模型與 deployment 類型。不能用今天的 `main` 文件證明舊版套件可用。

本次先 discovery 並使用 Microsoft Learn MCP 搜尋及擷取，再以同一批官方網頁與 GitHub 官方來源做精確查證。未讀取工具產生的暫存輸出檔。下列 **14 份主要證據**為引用範圍；導覽索引不作產品承諾。

### 來源清單

| ID | 官方來源、精確 URL | 本次版本線索 |
| --- | --- | --- |
| F1 | [Hosted agents in Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) | 頁面 `ms.date: 2026-09-11` |
| F2 | [Hosted agent runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract) | `ms.date: 2026-07-30`；部分 headers 明限 protocol `2.0.0` |
| F3 | [Resilience for long-running hosted agents (preview)](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/long-running-agent-resilience) | `ms.date: 2026-07-30`；public preview、無 SLA |
| S1 | [Copilot SDK：Session resume and persistence](https://github.com/github/copilot-sdk/blob/a675b55531a9dfc647ee015e32d74279568550f3/docs/features/session-persistence.md) | 固定官方 source commit |
| S2 | [Copilot SDK：Streaming session events](https://github.com/github/copilot-sdk/blob/a675b55531a9dfc647ee015e32d74279568550f3/docs/features/streaming-events.md) | 同上 |
| S3 | [Copilot SDK：Node.js type definitions](https://github.com/github/copilot-sdk/blob/a675b55531a9dfc647ee015e32d74279568550f3/nodejs/src/types.ts) | 同上；僅確認此版本 TypeScript 介面 |
| M1 | [Create and use memory in Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage) | `ms.date: 2026-08-05`；REST 範例 `2025-11-15-preview` |
| T1 | [Performing entity group transactions](https://learn.microsoft.com/en-us/rest/api/storageservices/performing-entity-group-transactions) | Azure Table Storage REST 契約 |
| T2 | [Update Entity](https://learn.microsoft.com/en-us/rest/api/storageservices/update-entity2) | Azure Table Storage REST；`If-Match`／`ETag` |
| Q1 | [Azure Service Bus sessions](https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-sessions) | `ms.date: 2026-08-20` |
| Q2 | [Azure Service Bus duplicate detection](https://learn.microsoft.com/en-us/azure/service-bus-messaging/duplicate-detection) | `ms.date: 2026-06-03` |
| Q3 | [Message transfers, locks, and settlement](https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-transfers-locks-settlement) | `ms.date: 2026-07-15` |
| C1 | [Azure OpenAI prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching) | `ms.date: 2026-08-11`；行為依模型家族及 deployment 而異 |
| R1 | [Routines in Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/routines) | `ms.date: 2026-08-27` |

Copilot SDK 查證時的官方 `main` commit 為 `a675b55531a9dfc647ee015e32d74279568550f3`。固定 source commit 不等於確認所有介面已在某個公開套件版本發布；本文不指定未查證的套件版本。

## 1. Foundry：session、compute、history 是不同東西

### 官方文字

- F1「Sessions」：`$HOME` 與 `/files` 內容跨 turns、idle periods 保留；同一 session 再被引用時，平台按需配置 compute 並還原狀態。各 session 彼此隔離。
- F1「Session lifetime」：idle timeout 可設 **2–60 分鐘，預設 15 分鐘**；**30 天沒有活動後永久刪除 session**。F1 另明說：「Compute follows the session, not the individual request.」
- F1 區分 conversation 與 session：conversation history 在 Foundry 保存，獨立於 compute；Responses protocol 會關聯 conversation 與 session。
- F2「Invocations protocol」：`Conversation history | Not managed. Your code handles state if needed.` Responses adapter 則在 `conversation.id` 存在時處理 history hydration。
- F2 runtime 基本要求：port `8088`、`GET /readiness` 回 `200 OK`、至少實作 `POST /responses` 或 `POST /invocations`。

### 官方支持範圍／不能推論的範圍

這支持「有身分的私人 session 狀態，可在 idle 後恢復」，**不支持共享 POSIX filesystem、跨 agent mount、同一記憶體或原 shell PID 存活**。F1 的隔離單位是 **session**，不是「每個 agent 永遠只有一台 Linux」；一個 agent 可以有多個 session sandbox。

同樣不能把 Copilot SDK 的 conversation history 與 Foundry Responses history 當成自然同步。走 Invocations bridge 時，Foundry 明確不替 bridge 管 history。[F1, F2, S1]

### 對 FA 的設計推論

維持明確映射：logical conversation → owner → Foundry session → SDK session/checkpoint。資料保留、handoff、重建與過期策略應由應用層明訂；不要讓一個短期 compute ID 成為業務資料的唯一身分。共享知識仍走有授權的 Context API／資料服務，不靠私人 `$HOME` 假裝共享磁碟。

**未驗收**：FA 容器中的真實還原路徑、SDK checkpoint 與 profile 的一致性、30 天過期後重建、跨版本 session 相容性。本文不重判既有 M1 結果。

## 2. 背景執行、resilience、heartbeat 與喚醒不可混為一談

### 官方文字

- F2「Graceful shutdown」：平台送 `SIGTERM` 後，容器停止接新 request、完成 in-flight request、flush `$HOME` 寫入並正常退出；官方 adapters 處理這個流程。
- F3：「Background execution lets work continue without keeping the original HTTP connection open.」但 background execution 本身不提供擁有工作之 process 結束後的 recovery。
- F3 原生 **resilient execution 已有 preview**：執行前保存 input、取得 work lease；handler 執行時 runtime 續租；process 消失後，後續 process 可 reclaim 並以同一 work/input identity 重入 handler。
- F3：「Recovery reenters the handler from its beginning.」它不是 deterministic replay，也不還原 local variables 或 in-memory call stack。
- F3 Responses 的完整 crash recovery 只適用於 **stored background responses，且 server 另行 opt in**。Foreground responses 不會在 process 中斷後自動 reinvoke。
- F2/F3 Invocations 要使用 AgentServer **resilient task primitive**；request、status、polling、streaming 對外契約仍由應用定義。
- F3 stream replay 提供 retained events 與 cursor；它不是 agent workflow checkpoint。Recovered Responses 的後續 `response.in_progress` 是 snapshot reset，client 要替換舊的累積輸出。
- F3 cancellation/shutdown 是 cooperative；來不及完成時，應 defer 未完成工作，不要寫成 terminal。Durable timers、fan-out/fan-in、child workflows 不是 resilient tasks 的承諾。

### 已確認的存活訊號與 `NOT_CONFIRMED`

| 機制 | 已確認範圍 | 不能推論 |
| --- | --- | --- |
| `GET /readiness` [F2] | container readiness／restart 判斷 | 不是 job durable receipt、queue lease、定時喚醒 |
| 新 request 重設 idle timer [F1] | 活動期間的 compute lifecycle | 不代表沒有 request 時任意 shell job 會被平台追蹤 |
| Resilient work 的 lease renewal [F3] | runtime 對已註冊 resilient handler 的 process-loss detection 與 recovery | 不是所有 subprocess 的 heartbeat、進度 checkpoint 或 exactly-once |
| 任意 background task 計數／手動 heartbeat API | **NOT_CONFIRMED**：本次 F1–F3 未提供可供 FA 回報任意 shell job 的精確公開方法或 header | 不虛構 `backgroundTaskCount`、heartbeat endpoint 或保活時限 |
| Zero-compute 後自主 wake 的 deadline／SLA | **NOT_CONFIRMED**：F3 說 later process 可 reclaim，未承諾任意工作在某個期限內自行被喚醒 | 不把 lease 或長 HTTP connection 當 durable timer |
| 固定 SIGTERM 寬限秒數、SIGKILL/OOM 最後寫入完整性 | **NOT_CONFIRMED** | graceful shutdown 的描述不是所有非正常結束都能 flush |

### 對 FA 的設計推論

可以把原生 resilient execution 列為候選，不應再一概說 Foundry「完全沒有 durable work primitive」。但必須把工作包成受管理 handler，讓 handler 持有 checkpoint reference、side-effect watermark 與 idempotency key。**任意 shell process 不會因為外面包了 background response 就自動耐久。**

恢復應重新檢查外部操作是否已完成；遇到「可能送出，但回執遺失」要 reconciliation，而非盲目重跑。Stream cursor 是交付進度，checkpoint 是工作進度，兩者要分開。[F3]

**未驗收**：FA 是否使用上述 primitive、實際 adapter/Core 版本、process loss 後恢復、lease 失效時 stale executor 停止、斷線重播、重啟後通知。不能以文件存在代替故障注入驗收。

## 3. Copilot SDK：可用介面與事件，不憑想像補 API

以下是 S1/S2 官方文件及 S3 固定 TypeScript source 的查證。**不直接套成 Python 欄位，也不保證舊版 CLI/runtime 支援。**

| 能力 | 官方介面／欄位與支持範圍 | FA 不能直接推論 |
| --- | --- | --- |
| Session continuity | `client.createSession({ sessionId })`、`client.resumeSession(sessionId, config)`；`listSessions`、`deleteSession`；`session.disconnect()` 保留磁碟資料 [S1] | resume 不是恢復先前 shell PID，也不是 worker 自動喚醒 |
| 磁碟 state | S1 列 conversation history、tool results、planning state、session artifacts；**in-memory tool state 不保留**；BYOK provider/API keys 要在 resume 重供 | checkpoint 路徑存在不代表 Hosted 支援文件示範的 Azure Files mount |
| Infinite sessions | `infiniteSessions.enabled`；`backgroundCompactionThreshold` 預設 `0.80`，`bufferExhaustionThreshold` 預設 `0.95`，都是 context 使用比例 [S1, S3] | infinite 指 context 管理，不是 compute 永不關、資料永不過期或摘要零遺漏 |
| Session events | `session.on(callback)`／`session.on("assistant.message_delta", callback)`；envelope 有 `id`、`timestamp`、`parentId`、`agentId?`、`ephemeral?` [S2] | event stream 不是自帶交易 inbox 或可靠外部通知管線 |
| Completion | `assistant.message` 是一次 LLM call 的完整訊息，仍可能含 `toolRequests`；`session.idle` 表示可接受下一訊息，**ephemeral、不會 resume replay** [S2] | 收到 assistant text 或 idle，不等於業務成果驗證、發布、送達使用者 |
| Compaction observability | `session.compaction_start` 的 `data` 為 `{}`；`session.compaction_complete` 有 `success`，及可選 `summaryContent`、`checkpointNumber`、`checkpointPath`、前後 token 統計 [S2] | 摘要不是簽核紀錄或精確交易 ledger |
| Pending work resume | `ResumeSessionConfig.continuePendingWork` 預設 `false`；設 `true` 可繼續 suspended 時未決的 tool calls／permission prompts；permission 會重新 emit，external tool result 需 consumer 經對應 RPC 提供 [S3] | 它不替外部工具重建執行程序；本文沒有確認那個低階 RPC 的方法簽名 |
| Tool definitions | `tools?: Tool[]`；`Tool` 有 `name`、`description`、`parameters`、`handler`；`availableTools`／`excludedTools` 過濾，exclude 優先 [S3] | tool schema 本身不是授權、證據或低風險認證 |
| MCP | `mcpServers?: Record<string, MCPServerConfig>`；stdio/local 有 `command`／`args`，http/sse 有 `url`；每 server `tools`：未指定或 `["*"]` 全收，`[]` 不收 [S3] | discover 得到的 server／tool 自述不能自動升為可信 policy |
| 動態 tool discovery | `Tool.defer?: "auto" \| "never"`；`toolSearch.enabled`／`deferThreshold`；`tool_search_tool` override 可在 `ToolInvocation.availableTools` 取得已初始化 catalog；其他 tool invocation 此欄位為 `undefined` [S3] | **NOT_CONFIRMED**：任意 hot-add/hot-remove 都不影響正在執行的 turn，或對所有 backend 都維持相同 prompt cache |

### Hooks、permissions 與 runtime provenance

**官方文字**

- S3 的 `SessionHooks` 確有 `onPreToolUse`、`onPreMcpToolCall`、`onPostToolUse`、`onPostToolUseFailure`、`onUserPromptSubmitted`、`onUserPromptTransformed`、`onSessionStart`、`onSessionEnd`、`onErrorOccurred`、`onAgentStop`。
- `PreToolUseHookOutput.permissionDecision` 為 `"allow" | "deny" | "ask"`，另有 `permissionDecisionReason`、`modifiedArgs`。這是 hook output，**不是** `onPermissionRequest` 的回傳型別。
- `onPermissionRequest?: PermissionHandler` 可處理 permission；此 source 若省略，requests 以事件呈現並保持 pending，待 consumer 解決。不要默認「省略就是全部允許」。
- `onPostToolUse` 只處理成功結果；失敗另用 `onPostToolUseFailure`。`Tool.skipPermission: true` 表示可略過 permission prompt，不是平台判定它低風險。
- `createAttributedPermissionResult` 的 context 是「informational only and never changes permission behavior」；`managedApprovalRequired` 出現時，host 應停止 auto-approve 並呈現確認介面。[S3]
- S2 `tool.execution_complete` 有 `toolCallId`、`success`、`result`／`error`。`assistant.usage` 可有 `model`、`apiEndpoint`、`providerCallId`、`serviceRequestId` 等，但 usage 是 ephemeral、許多欄位可省略。
- F2 protocol `2.0.0` 的 `x-agent-user-id`、`x-agent-foundry-call-id` 由平台從 verified identity 產生；call ID 必須原樣向 Foundry 下游傳遞，user ID 不往外傳。`x-client-*` 則是 caller-supplied pass-through，兩者信任等級不同。

**對 FA 的設計推論**

低風險自主行動應由執行邊界的 allowlist、資源範圍、budget 與可逆性規則判定。模型說「我已獲批准」、MCP 的 `readOnly` 自述、記憶內容、或 callback 回 `"allow"`，都不能代替應用持久保存的人類批准證據。

Provenance 應由 runtime 收集：選定與實際觀測到的 model/provider、SDK/CLI/adapter/container 版本、tool identity、caller identity、approval decision 與 effect receipt；可選欄位缺失就標 unknown，不讓模型補值。Trace/correlation ID 是定位線索，不自動等於授權證據。[S2, S3, F2]

**未驗收**：S3 新介面的套件發布版本、CLI 支援矩陣、FA permission adapter、hook 在取消與失敗時是否完整記錄、事件重播去重、相同 SDK session 的互斥。S1 明列 **“No session locking”**，並說 concurrent access undefined；F3 的 conversation locking 只涵蓋其受管理 work，不能外推成所有 SDK clients 共用鎖。

## 4. Memory Store：可學習、可管理，但不是精確交易資料

### 官方文字與語意邊界

- M1 明標 Memory 與 Memory Store API 為 **preview**，有 item CRUD、TTL、direct remember/forget。
- `scope` 是 partition key／選取命名空間；低階 APIs 要自己給 `scope`，沒有從 caller identity 自動擷取。Memory search tool 的 `scope: "{{$userId}}"` 才會從 `x-memory-user-id`，或 fallback Entra TID/OID 解析。
- M1 的 best practices 要開發者 **“Implement per-user access controls”**，並要求自己記錄 tamper-evident deletion audit trail。
- 更新會 extraction/consolidation；`begin_update_memories` 回 poller，`.result()` 等完成。REST 是 `:update_memories`，再用 `update_id` 查 `/updates/{update_id}`；可用 `previous_update_id` 串接。文件說這個 LRO 可能約一分鐘。
- Prompt agent 的自動更新受 `update_delay` debounce；「The update is scheduled and only completes after the configured period of inactivity.」
- **不要把 extract 與 search 一律寫成同一種非同步 job。** M1 中 extraction/update 是 LRO；`search_memories` 是 request/response 搜尋，Python 範例直接回 `search_response`，TypeScript 的 `await` 不代表 server 端有耐久 search job。**獨立 durable search job／search completion event：NOT_CONFIRMED。**
- `search_memories` 的 request 可帶 `scope`、`items`、`options.max_memories`，回 `memories`。文件說：「Often, you can't retrieve user profile memories based on semantic similarity to a user's message.」不帶 `items`／`previous_search_id` 可取 static user-profile memories；帶最新 `items` 取 contextual memories。
- 使用者明確要求 remember/forget 時，工具可 **立即** 套用並回 `memory_command_call`；它不取消 TTL，也不代表所有一般 extraction 都同步完成。

### 不能推論的範圍

**`scope` 不是 caller-to-scope ACL 的證明。** 字串命名、`x-memory-user-id` header、語意檢索，都不能取代服務端授權。M1 的 identity mapping 是特定 tool 的機制，不應外推成所有 item CRUD 自動驗證 end-user ownership。這是安全邊界的設計判讀，不是宣稱做過跨 scope 滲透實測。

語意記憶適合找偏好、經驗與摘要；M1 不承諾完整回憶、精確版本比較、owner lock、交易計數、不可變 ledger，或「記憶中的事已發生」。也沒有承諾 FA 所需的 **proposal → audit → evidence check → human approval** 流程。

### 對 FA 的設計推論

保留三層：精確業務事實／批准／handoff／published work 由可追溯資料層掌管；原始產物與證據保留不可變 reference/hash；Memory Store 放可撤回的衍生摘要和 retrieval hint。Memory 命中後，要回精確來源驗證再決策。

寫入管線可先保存 proposal 與原始來源，檢查 evidence、版本、敏感資料及衝突，依政策取得人類批准，再發布可用記憶。這是 **FA 自建流程**，不是 Memory API 現成狀態機。會自動寫入的 memory tool／direct commands 不能繞過此閘門；必要時改由受控 API 寫入。

**未驗收**：FA 採用的 API schema、`memories` 與 legacy `results` 差異、更新失敗與延遲、scope 授權、記憶中毒／衝突、TTL、撤回與再抽取後是否復活。`update_delay=0` 只取消等待，不是精確即時可見性的通用 SLA。[M1]

## 5. Azure Table：同 partition 原子提交的硬邊界

### 官方文字

- T1：同一 transaction 的 entities 必須在 **同一 table、同一 `PartitionKey`**；最多 **100 entities／operations、4 MiB**；同 entity 只能出現一次、做一個 operation。
- T1：「all operations in the change set either succeed or fail」。Query 不可與 insert/update/delete 混在同一 batch；每個操作可帶自己的 `If-Match`。
- T2：`If-Match: <ETag>` 只有與 server ETag 相符才更新；不符回 **`412 (Precondition Failed)`**。`If-Match: *` 明確取消 optimistic concurrency。
- T2 `Update Entity` 是整筆 replace，不是 patch；漏掉 properties 可能移除它們。ETag opaque，不應自行解析。

### 對 FA 的設計推論

「job 狀態＋outbox event」若在同 table／partition，可用一個 EGT 原子提交；inbox 去重紀錄與其對應狀態改動也應選擇能同 partition 提交的邊界。固定 `event_id`／`job_id`，重試時不能產生新的業務身分。

跨 partition 的 owner、handoff、全域 admission、不同 table 的 audit，不會因為每筆都帶 ETag 就形成同一交易。若現有 key layout 不能同 partition 提交，要明訂多階段恢復及 reconciliation，不能只在設計圖寫「transactional outbox」。

**不能推論**：Table EGT 不能把 Service Bus send/complete、Blob 寫入、Memory 寫入、外部工具副作用放進同一原子交易。ETag 也不會讓已失去 owner 的舊 process 停止執行。[T1, T2]

**未驗收**：FA 的真正 partition layout、100-operation／4 MiB 上限、412 衝突重讀、失去回應後重送、舊 owner fencing、outbox dispatcher 的恢復掃描。

## 6. Service Bus：有 ordered delivery 與 settlement，不是端到端 exactly-once

| 能力 | 官方文字／支持範圍 | 不能推論 |
| --- | --- | --- |
| Sessions [Q1] | 同 `SessionId` 在 queue/subscription 中只由持有 exclusive session lock 的 receiver 接收；跨機器 service-side lock；有序收取 | 不鎖所有 session、不限制全系統 active 數；handler 自行平行執行仍須管順序 |
| Session state [Q1] | Broker 保存 opaque application state，接手 receiver 可讀；由應用清理 | 不是 SDK history、共享 filesystem、任意 job 的執行狀態 |
| Duplicate detection [Q2] | 指定時間窗內檢查 application-controlled `MessageId`；重複 send 仍回成功，但新副本被丟棄 | 不去除 consumer redelivery，不檢查 payload 語意，不保證外部 side effect 一次 |
| 去重範圍 [Q2] | 預設 **10 分鐘**，可設 **20 秒–7 天**；partitioning 開啟看 `MessageId+PartitionKey`；sessions 與 partitioning 並用時 partition key 要等於 session ID | 超窗重試不保證去重；改 ID 就可能是新訊息 |
| Send settlement [Q3] | send 完成表示 broker acceptance；不可 fire-and-forget 而不取結果 | 不是 receiver 已完成，更不是使用者已看到成果 |
| `PeekLock` [Q3] | 處理後 `Complete` 移除；`Abandon`／lock expiry 可 redeliver；`DeadLetter` 處理不可恢復訊息 | `Complete` 本身可能失敗；side effect 已做但 settlement 不明，仍可能收到同一訊息 |
| `ReceiveAndDelete` [Q3] | broker 傳送即算 settled | 傳輸失敗可能直接遺失，不適合可靠工作交付 |

Q1/Q2 的 sessions、duplicate detection 不支援 Basic tier。Q3 明說 PeekLock／SessionLock 是 **volatile**；斷線、更新等情況可以失去鎖。Q1 也提醒 DLQ 訊息重新送回後，原先相對順序不再保證。

### 對 FA 的設計推論

採「可重送的 delivery＋idempotent processing＋持久 inbox/outbox」模型，不宣稱端到端 exactly-once。Broker acceptance、worker start、effect commit、成果發布、通知 channel acceptance、使用者可見回執是不同狀態。

若目標是 **全域最多 4 active**：

1. 先定義 active 指 admission、真正執行中的 job，還是仍在計費的 Hosted compute。工作完成後 sandbox 還可能等 idle timeout 才釋放。[F1]
2. `SessionId=agent_id` 只能序列化該 agent 的訊息，不能證明所有 agents 加總 ≤4。[Q1]
3. 可考慮共用 admission ledger／四個 lease slots，加 ETag、owner epoch/fencing；但跨 partition 領 slot 與 claim job 不是單一 EGT，要有補償與回收。[T1, T2]
4. **lease 到期不等於舊 executor 已停。** 嚴格執行上限還需 executor 在 effect 邊界驗證 fencing，或可靠停止／確認舊工作；否則只是「帳面上最多 4」。

這些是待選的應用協定，不是「用了 sessions 就自然達成」。本文沒有建議立刻加一套 Service Bus 基礎設施。

## 7. Reply-first、silence 與防 ping-pong：平台不會替 FA 定義

**官方支持**：S2 提供文字、tool lifecycle、idle/error events；F3 提供某些受管理工作的恢復；Q3 提供 transport settlement。它們沒有承諾 FA 的產品交付語意。

**設計推論**

- 先將工作及 outbox 原子保存，才回「已接受／正在處理」；收到 request 不等於接單成功。**ack ≠ delivery**。[T1, Q3]
- 低風險工作可以自主執行，但成果要經 evidence check／發布／通知管線，不能用 assistant text 當 effect receipt。[S2, S3]
- 無需打擾時可以 silence；silence 是對外通知政策，不是丟棄 task terminal state、失敗、批准要求或 delivery retry。
- Agent-to-agent 事件帶穩定 correlation、origin、causation、dedupe key、最大 hop／回覆 budget；不要因「收到 agent 訊息」就必定再回一封。這些是 FA 自建欄位，不是本文確認的 SDK schema。
- Model/SDK 生成的摘要不能自行升級批准、擴張工具權限，或把未送達工作改成已完成。

**未驗收**：重複事件、循環 delegation、idle event 遺失、通知 channel timeout、使用者重問時能否回讀真正工作狀態。沒有來源支持「提示詞寫了不要 ping-pong」即可形成硬保證。

## 8. Stable prefix／dynamic tools：cache 是特定 provider 契約

### 官方文字

C1 要求 cacheable prompt 至少 **1,024 tokens**，且開頭 **1,024 tokens 完全相同**；單一字元差異就可能 miss。建議 stable/repeated content 在前、dynamic content 在後。

該頁區分 model families：較早模型與 GPT-5.6 以後的 cache write、breakpoint、retention 行為不同；GPT-5.6 的 explicit breakpoints 只支援 Standard pay-as-you-go，不支援 PTU-M。較早模型傳入 `prompt_cache_options`／`prompt_cache_breakpoint` 會回 `400`。Chat Completions 的 cache 命中可從 `usage.prompt_tokens_details.cached_tokens` 觀測。[C1]

### 對 FA 的設計推論／未驗收

可維持固定 policy prefix、可追蹤的 prompt version，把動態 evidence 與按需工具放後面；S3 的 deferred tool discovery 是相關可用介面。但 **Copilot SDK 支援某種 backend，不代表必然傳遞相同 cache 參數或享有同樣命中保證**。

**NOT_CONFIRMED**：FA 選定 SDK/runtime/provider 路徑是否保留所需 prefix、cache option passthrough、實際命中率與成本。切換模型、tool schema、system-message construction 或 compaction 之後都需重新量測，不把 cache 當持久 memory 或正確性機制。[C1, S3]

## 9. 官方 wake/event 選項：只列候選，不盲目新增基礎設施

### 官方文字

R1 確認 Foundry **routines 存在**：一個 trigger、一個 action，可由 timer、schedule、支援的 event 去 invoke 一個 prompt/hosted agent，並保存 run history。支援事件包括 GitHub issue 與 Teams channel message；不是任意 event bus。

- Recurring 最短 **5 分鐘**。
- 下游 agent request 預設每次 attempt **30 秒 timeout、共 3 次 delivery attempts**。
- 預設用 agent identity；creator identity 是建立時 opt in，不是任意 end-user impersonation。
- R1 另提 built-in reminder tool 經 toolbox 讓 hosted agent 稍後在同一 conversation 再被 invoke；本文**只確認概念存在**，未確認精確 tool name／schema，不給 CRUD。
- Routines 不替代 branching、人類批准、multiple-agent orchestration；有區域與 CMK 限制。[R1]

### FA 候選比較

| 候選 | 適合解決 | 仍須 FA 自己解決 |
| --- | --- | --- |
| FA 自建持久 job/outbox＋recovery scanner | 接單、重試、故障回收的應用契約；不是聲稱 repo 已實作 | scanner 自身可用性、真正 wake 來源、fencing |
| Foundry resilient execution [F3] | 註冊 handler 的 process-loss recovery、可重播 stream | domain checkpoint、side-effect idempotency、通知與批准 |
| Foundry routine/reminder [R1] | 平台支援的輕量定時／事件喚醒 | delivery attempts 耗盡、長工作 handoff、同 conversation 對應與去重 |
| Service Bus [Q1–Q3] | 需 broker buffer、consumer coordination、DLQ 時 | dispatcher/waker、inbox/outbox、全域 admission 與外部效果一致性 |

**設計推論**：先驗證現有方案缺哪一個可靠性環節，再選補件。不能因官方列出 Functions／Logic Apps／queues，就把整套都加進來；也不能把 routine 的 run status 當成果已可靠交付。**以上候選均未在本次實測。**

## 10. 給主代理的十大契約陷阱

1. **Invocations 沒有 platform-managed history**；logical conversation 要有自己的 owner、SDK session/checkpoint 映射。[F1, F2, S1]
2. **私人 session persistence ≠ shared filesystem，也 ≠ process 存活**；30 天 inactivity expiration 與 compute idle 是兩種生命周期。[F1]
3. **原生 resilience preview 已存在，但需 opt in／受管理 handler**；background response、任意 shell job、durable timer 不能畫等號。Recovery 從 handler 開頭重入。[F2, F3]
4. **SDK resume/infinite sessions ≠ durable executor**；in-memory tool state 不保存、SDK 沒有內建 session locking、`session.idle` 不會 replay。[S1, S2, S3]
5. **Memory `scope` 不是完整 ACL，semantic recall 不是精確事實**；一般 update 是 LRO，search 不能因 `await` 就稱 durable async job；direct remember/forget 又是即時例外。[M1]
6. **Hooks 是實作政策的接點，不是自動人類批准**；成功與失敗 hooks 分開，provenance 可選欄位缺失不可讓模型補造。[S2, S3, F2]
7. **Table 原子性只在同 table／PartitionKey／100 operations／4 MiB 內**；ETag 不能讓跨服務副作用變成同一交易。[T1, T2]
8. **Service Bus sessions＋duplicate detection ≠ exactly-once，也 ≠ 全域 4 active**；去重有時間窗，lock 會失去，Complete 可能在 effect 已完成後失敗。[Q1, Q2, Q3]
9. **ack ≠ 成果發布 ≠ 通知送達**；silence 與避免 ping-pong 是 FA 的產品與事件協定，不是 event stream 的天然保證。[S2, Q3]
10. **不要把 cache 或 routine 外推成普遍保證**：cache 綁 model/deployment/backend；routine 有 timeout/retry/trigger 限制。先固定版本、列故障驗收，再決定是否新增基礎設施。[C1, R1]
