# Foundry Hosted 原生優先：新對話狀態改由 Agent 的 State store 保存

狀態：accepted。
- 方向由 Neal 2026-10-01 16:00 決定。
- 第 9 節 ①③ 由總協調 16:24 暫定，Neal 可以推翻；② 由 Neal 在 16:37 選 (b)（issue #306）。
- **① 已由 Neal 2026-10-02 08:16 改為「改成 Responses」**（#321）。遷移設計見 [N6 遷移設計](../n6-responses-migration-design.md)，總協調 08:57 已核准，第 4、7、9 節同步更新。
- 本 ADR 只決定方向，不授權部署、新 Azure 資源或支出。第 9 節 ② 的兩個角色，由 fdbb 在部署時指派。
- N0 live smoke 已由總協調另外批准，只限 test agent `00000000`。

決策日期：2026-10-01。

基準：
- main `be2daf3b`
- 固定版本：`azure-ai-agentserver-core==2.1.0`、`azure-ai-agentserver-invocations==1.1.0`、`github-copilot-sdk==1.0.13`
- 官方文件查閱日期：2026-10-01

## 背景

Neal 原話：

> 「目前這個POC階段，可以儘量用hosted agent的能力，沒有GA也沒關係。」
>
> 「權威資料留在 FA <-- 這個不需要」

這兩句話推翻了先前「權威資料留在 FA，只用 `$HOME` 熱恢復」的評估方向。

先前的做法：
- FA 用 Azure Table／Blob 自己保存對話歷史、checkpoint、工作登記和回條（[ADR-0002](0002-shared-context-without-file-share.md)、[ADR-0006 §4](0006-team-trust-boundary-grokbot-mode.md#4-入口票vm-內執行回條費用紀錄的最小分工)）。
- 2026-10-01 曾因讀取全部工作登記超過 4 MiB 而回 413；#300 已改為只讀需要的資料。

Foundry Hosted agents 已內建以下能力：
- Sessions（`$HOME`、`/files`）
- Conversations（只適用 Responses 協定）
- State store
- resilient tasks（long-running resilience）

## 實作原則（沿用 ADR-0006，所有切片適用）

1. 原話：「請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。」
2. 原話：「「SDK-first／Hosted 優先」原則，在 FA 自己建一層之前，應該先確認官方功能跟最佳實踐，確認是否真的不足。」

## 決策摘要

1. **新對話的狀態放在 Agent 自己的 Foundry State store。**
   - 範圍包括接單回條、回合結果、對話資訊和 checkpoint。
   - 固定版 core 2.1.0 已內建 `azure.ai.agentserver.core.storage.FoundryStateStore`（`@experimental`）。
2. **每件事使用一個 key，每次寫入只針對單一 key 使用 ETag。**
   - 官方只提供單一 key 的 `create_item`（「fail on duplicate keys」）和 `set_item(if_match=…)`。
   - 沒有跨 key 交易。
   - 每筆最多 1 MB。
   - `list_keys` 只能依 tag 篩選，不能依前綴列出。
3. **排隊、單一回合和重新進入，交給官方 `@multi_turn_task`。**
   - 每個 Agent 使用一條 steerable chain：`task_id=<agent>`、`input_id=<RID>`。
   - 官方的 `input_id` 不會去重，所以「只執行一次」由回條的 CAS 保證。
   - 結果不明的回合標成 `unknown`，不重新執行模型或外部效果。
4. **API 只保留官方確實缺少的能力。**
   - owner→Team 入口驗證。
   - Team 設定和用量帳。
   - 跨 Agent 共享的團隊資料。
   - 團隊限定 connector。
   - 跨 Agent 傳話（第 5 節）。
5. **舊資料原樣保留，仍可讀回，不搬移也不刪除。**
   - 現有 CID 繼續使用原本的 Table 路徑。
   - 只有新建立的對話改用新路徑。

## 決策

### 1. State store：每件事一個 key

**Store 配置**
- 每個 Agent 使用自己的 store，名稱前綴固定為 `fa/v1/`。key 長度不超過 128 字元。
- store 的範圍是 `[project_id, agent_guid]`，見固定版 `storage/_generated/_models.py` 的 `CreateStateStoreRequest`。
  - 不可以刪除後重建同名 Agent。
  - 換 Agent 版本後是否仍讀得到同一個 store，列入 N0 確認。
- `turns`、`conv`、`effects` 三個 store 建立時，必須設定 `item_ttl_seconds=-1`，也就是永不過期。
  - 預設 TTL 是 30 天，以寫入時間滑動計算，而且建立後不能修改（`_models.py:129-131`、`storage/_state.py:49`）。
  - 如果沿用預設值，回條 30 天後就會消失，同一個 RID 可能被當成新請求再次接受。
- `get_or_create` 遇到已存在的 store 時，會忽略 TTL 參數（`_state.py:272-274`、`:303-310`）。
  - 每次啟動都要用 `store.get()` 讀回實際 TTL。
  - TTL 不是 `-1` 時，停止寫入並明確報錯。
  - 若要換 store，必須使用新名稱。
- task chain 被清理後，回條不會跟著消失。查不到 task，不代表這個 RID 可以重用。

| Store | Key | 寫法 | 取代的 FA 自建資料（新對話） |
|---|---|---|---|
| `fa/v1/turns` | `rid:<RID>` | 用 `create_item` 建立回條，建立成功才算接單。狀態轉換為 `accepted → started → completed｜unknown｜busy_refused`；每次更新都用 `set_item(if_match)`。回條要保存原始請求內容的 digest | 工作登記、入口票、開始和結束回條（`behavior_work_policy.py:1474-1513` 的接單 EGT；`behavior_turn_ticket.py`） |
| `fa/v1/conv/<CID>` | `meta` | 用 `set_item(if_match)` 分配回合號，記錄 `{n, rid, base_ckpt}` | conversation 列（`behavior_instruction.py:27-48`） |
| 同上 | `ckpt:<sha>:<i>` | 用 `create_item` 建立以內容雜湊命名的分段，不使用可變的 head | SDK checkpoint Blob（`behavior_sdk_checkpoint.py:46-100`） |
| 同上 | `turn:<n>` | 用 `create_item` 保存回合內容和 checkpoint 清單（sha、分段數）；超過 1 MB 時拆分 | 回覆 Table 儲存，以及 `behavior_instruction.py:326-331` 的 32 KiB 壓縮上限 |
| `fa/v1/effects` | `fx:<RID>:<tool_call_id>` | 先保存 pending token，完成後再保存 done 和 receipt（官方 at-most-once 寫法，[tasks-guide §6.2](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.1.0/sdk/agentserver/azure-ai-agentserver-core/docs/tasks-guide.md)） | 工具效果回條 |
| `fa/v1/notes` | 依用途決定 | Agent 私人資料 | T3 中只屬於單一 Agent 的部分 |

**接單：原本跨列的 EGT 改成單一 `rid` 回條**

原本的登記、預留、回條、queued job 和 pool 會放在同一筆 EGT。新寫法如下：
- 目前 `budget_enforced=false`，所以不需要預留額度。用量在回合結束時入帳（[ADR-0006 §5](0006-team-trust-boundary-grokbot-mode.md#5-組織資源與跨-vm-傳話c7bb-研究2026-10-01只做設計未實作) A2）。
- 先建立回條，再呼叫 `start(task_id=agent, input_id=RID, input={…, accepted_etag})`。
- 同一個 RID 如果帶來不同的 digest，直接拒絕，不視為重送。這只是防止送錯單，不另外增加控制層。
- 如果建立回條和 start 之間發生 crash，同一個 RID 重送時會收到 409，並讀回原回條。
  - 只有在原回條仍是 `accepted`，而且 digest 相同時，才可以用同一個 `input_id` 再 start 一次。
  - 只要狀態已經是 `started` 或之後的狀態，即使 `get_active_run` 回傳 None，也不能據此重送。
- 官方的 `input_id` 不會去重。
  - 只帶 `input_id` 時，官方行為是「Always succeeds; no precondition check」（`tasks/_decorator.py:650-654`）。
  - 重複放入的輸入會以 `entry_mode="resumed"` 進入（`tasks/_manager.py:2493`）。
  - 因此 handler 的第一步必須是 `set_item(started, if_match=accepted_etag)`，而且只有成功時才能執行模型。
  - 寫入 `started` 時，要附上 handler 自己產生的 `run_token`。
  - 如果收到 409 或 412，可能是自己的寫入其實已經成功，但沒有收到 ACK，所以要先讀回原 `rid` 再判斷。
    - 讀回的 `started` 帶的是自己的 `run_token`：代表剛才的寫入已成功，繼續執行。
    - 讀回的是 `completed`，而且 refs 是自己剛寫入的：視為已完成。
    - 其他情況：依下方的重新進入規則處理。判斷時不看 `entry_mode`。
  - 單一 item 的寫入雖然是原子的，仍可能遺失 ACK，所以不能把它說成「不會有 unknown」。

**回合結束**
- 寫入順序依序為：checkpoint 分段 → `turn:<n>`（含 checkpoint 清單）→ `rid` 改為 `completed`。
- 只要 `turn:<n>` 存在，就表示 checkpoint 已經完整。
- `completed` 要和結果引用一起寫入同一個 `rid` item，並使用同一次 CAS。結果引用包括 `turn` key、內容 digest 和 checkpoint sha。即使 `.result()` 的回應遺失，仍然可以讀回結果。
- 下一回合要接續的 checkpoint：
  - `meta.rid` 指向的回條是 `completed` 時，使用該回條記錄的 checkpoint。
  - 其他情況使用 `meta.base_ckpt`，保留上一個合法狀態。
- 每一步只寫一個 key，新內容都只能建立、不能覆蓋。中途中斷時不會覆蓋舊資料，最多只會留下沒有被引用的分段。
- handler 先把 `rid` CAS 成 `started`，再用 `meta` 的 `if_match` 分配回合號。同一個 Agent 一次只執行一個回合，所以不會跳號或重號。

**重新進入規則**

**2026-10-03 核准④補充：** 下表仍描述原RID，不改其UNKNOWN或重做原執行。
只有官方process-loss的`recovered`／Responses `is_recovery`可另存N1意圖，
以原CID／合法checkpoint及效果水位派生最多2次的新RID；`resumed`、普通重述與歷史UNKNOWN不適用。
已完成效果不重做、未明外部效果不自動重送，舊once不轉移。這不是新的偵測／lease／排程器；
接點、限制及本機結果見[④契約](../FA-AGENT-BEHAVIOR-SPEC.md)與
[評測](../agent-runtime-evaluation-baseline.md#native-continuation-evaluation)。

判斷依據是 `rid` 狀態，不是 `entry_mode`。`recovered` 和 `resumed` 適用同一套規則。

| `rid` 狀態和其他條件 | 處理方式 |
|---|---|
| `completed`、`unknown`、`busy_refused` | 直接回傳原本的結果或狀態 |
| `started`，而且同時符合：`meta.rid` 指向這個 RID；`turn:<n>` 存在；清單中的分段在 store 中全部存在並通過 hash 驗證（N3 `verify_checkpoint`，不讀 `$HOME` cache） | 補上 `completed`，不重新執行 |
| 其他 `started` | 該 RID 已有開始紀錄，但無法確認是否完成：標成 `unknown` 並結束，不重新執行模型。lease 或排他 chain 不保證舊程序或外部效果已經停止；舊程序之後再寫入時，會被原本的 `accepted`／`started` ETag 擋下（412），它的結果也不會被引用 |
| `accepted` | 用 `accepted_etag` CAS 成 `started`，再照一般流程執行 |

**外部效果的三個 crash 邊界**（`fx:<RID>:<tool_call_id>`）
1. pending token 寫入前中斷：外部效果尚未送出，可以照常執行。
2. pending 已保存，但不確定外部效果是否已送出：
   - 只有目的端明確支援用同一個 token 做冪等處理時，才能用原 token 再送一次。
   - 其他情況都維持 `unknown`。
   - 不能因為 `recovered`，或因為 `get_active_run=None`，就重新送出。
3. 外部效果已完成，但 done 或 receipt 還沒保存：照第 2 點處理，用原 token 查回結果。不能因為缺少結果，就判斷外部效果沒有執行。

官方原文是「effects are your responsibility」。task 去重只保證同一個 run 不會重複執行，不代表所有外部效果都只會發生一次。

### 2. API 還要讀什麼（Grokbot 模式）

官方說明 State store 在 preview 期間只能由 hosted agent 使用（[agent-state-store](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store)）。所以在新路徑上，API 不保存對話或回合狀態。

**送出回合**
1. 8765 把請求送到 API。
2. API 先核對 owner→Team（T1），必要時讀 Team 設定和帳本（T6）。
3. API 用 API MI 以 Invocations POST 呼叫 Agent，並帶上 RID 和能力清單。API 端已有 `behavior_foundry.py:146` 的 `FoundryHttpClient`。
4. Agent 回覆 `accepted`，或回傳原回條的狀態。

如果 API 沒有收到回覆，只能用同一個 RID 重送或查詢。只有在 Agent 明確回覆 `busy_refused` 時，客戶端才能使用新的 RID。

**進度和歷史**
- API 只負責轉送，由 Agent 讀取自己的 store 後回覆。
- 進度使用 Invocations GET（`behavior_hosted.py:825` 的 `@app.get_invocation_handler`）。
- 歷史使用唯讀 invocation 讀取。
- 代價是讀取時會喚醒閒置的 VM；POC 階段可以接受。

**交付回條**
- 8765 確認後，經 API 交給 Agent。
- Agent 用 `set_item(if_match)` 在 `rid` 上標記 `delivered`。

**用量**
- VM 在回合結束時把用量送給 API。
- API 用 `behavior_instruction_cost.py:78` 的 `record_turn_usage` 入帳。
- 入帳 key 是 `(agent, RID)`，同一筆只能建立一次。

### 3. Resilient tasks

**啟用方式**
- 在 Hosted 進入點，於 lifespan 開始前呼叫 `set_resilient_tasks_enabled(True)`。
- 固定版 `InvocationAgentServerHost` 已會建立和停止 TaskManager，但必須先開啟這個開關。71c7 已唯讀核實以下位置：
  - `invocations/_invocation.py:172`、`:298`
  - `core/_base.py:331-350`、`:409-416`、`:428-430`
  - 開關預設為 False：`core/tasks/_enablement.py:30-54`
- N0 的 `behavior_hosted.create_host` 已加測試 Agent 限定、預設關閉的明確開關；
  一般回合仍走原路徑，不在此切片啟用 N1 chain。
  [操作與離線結果](../../src/fa-shared-context-poc/README.md#n0foundry-原生啟用與-state-store-探針codeoffline未部署)
  不代表真正 Hosted 啟動、State store 授權或跨版保存已通過；live 由 fdbb 排入下一次 main 同 build。

**取消和 timeout 的判斷**

steering 只用來排隊。官方說明 `ctx.cancel` 可能有多種原因，要用「observe `timeout_exceeded`, `cancel_requested`, `pending_input_count` to disambiguate」判斷（`tasks/_context.py:83-84`）。

| 條件 | 處理方式 |
|---|---|
| `cancel_requested`（只由 `TaskRun.cancel()` 設定，`tasks/_run.py:146-164`），或 `rid` 上已有 FA 指定 CID-RID 的取消 | 停止執行。和排隊同時發生時，**取消優先**，不會因為有人排隊就忽略取消 |
| `timeout_exceeded` | 停止執行，並把 `rid` 標成 `unknown`，避免卡住佇列（`tasks/_manager.py:1613-1652`）。和排隊同時發生時，**timeout 優先** |
| `pending_input_count > 0`，而且同時**沒有** `cancel_requested` 和 `timeout_exceeded` | 後面有回合在排隊，繼續執行目前回合 |
| 已送出外部效果 | 依第 1 節的三個邊界處理 |

**其他規則**
- queue 最多 9 筆，滿了會出現 `SteeringQueueFull`。這時把 `rid` 標成 `busy_refused`，並明確回覆忙碌。
- `call_id` 放在 task input 中，重新進入後才能還原 request context。
- 關機時，由 `ctx.shutdown` 呼叫 `ctx.exit_for_recovery()`。
- handler 的輸出不會被平台保存，所以結果一律寫進 store。

### 4. Conversations／Responses 協定：改用 Responses（N6，Neal 2026-10-02 08:16）

原本第 9 節 ① 暫定「先不改」，已由 Neal 改為「改成Responses」（#321）。完整設計、切片和實測項目見 [N6 遷移設計](../n6-responses-migration-design.md)，官方查證見 [N6 官方查證](../n6-responses-official-research.md)。重點如下：

| 項目 | 內容 |
|---|---|
| 協定 | 同一個容器同時提供兩種協定（官方支援的 mixin）。Responses 負責對話回合，包括 Foundry 代管歷史、stored background 和 crash 重入；Invocations 留作 FA 控制面，負責接單、讀回條、取消、交付和 session 操作 |
| 版本 | `azure-ai-agentserver-responses==2.2.0`、`core==2.2.0`、`invocations==1.2.0` 必須一起升；invocations 1.1.0 要求 `core<2.2.0` |
| 生命週期 | 新 CID 由 Responses resilient task 負責（`background`＋`store`＋`resilient_background`），只執行一次仍靠本節 §1 的 `rid` 回條 CAS；新 CID 不再使用 Agent chain |
| 官方缺口 | 官方只依 `conversation_chain_id` 序列化，沒有 Agent 層級互斥，所以 FA 補行程內 Agent 鎖。`previous_response_id` 模式使用 one-shot task，不會回 409，所以由 `meta.inflight_rid` 單 key CAS 保證每個 CID 同時只有一個未結束的 RID |
| Copilot SDK | 仍使用自己的 session，N3 checkpoint 保留；handler 用 `get_input_text()`，和官方 Copilot SDK＋Responses 範例相同 |
| 重入 | 官方原文「Recovery reenters the handler from its beginning. It isn't deterministic replay」，因此 §1 的重入規則和 #364 照舊，不重跑 unknown |

### 5. 留在 Foundry 外的能力和官方缺口

| 項目 | 官方缺口 | 處理方式 |
|---|---|---|
| 入口 owner→Team | Endpoint 可以依 Agent 授權 Foundry Agent Consumer（[hosted-agent-permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)）。容器協定 2.0.0 會注入平台驗證過的 `x-agent-user-id`，但官方要求「Treat their values as opaque」（[hosted-agent-contract](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract)），對不到 Entra oid，官方也沒有 Team 模型 | 繼續使用 API 和 T1 |
| 組織和 Team 用量帳 | 官方只有部署層級的 TPM／RPM，以及 Agent token 儀表板（preview）；沒有每個 Agent 或每個 Team 的花費上限（[limits](https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions)、[monitor dashboard](https://learn.microsoft.com/azure/foundry/observability/how-to/how-to-monitor-agents-dashboard)） | 繼續使用 API 帳本（T4、T6） |
| 跨 Agent 共享的團隊資料（T3） | store 名稱只在 `[project_id, agent_guid]` 範圍內唯一，官方沒有說明跨 Agent 共用方式 | 團隊共享資料繼續走 T3 relay；Agent 私人資料改放 State store |
| 跨 Agent 傳話（T5） | 送出端可以透過 toolbox 使用 A2A（GA），接收端必須使用 Responses；收件端只拿得到不透明的 `x-agent-user-id`，官方也沒有說明 hop 上限或去重方式。State store 的 API 沒有指定目標 Agent 的參數，跨 Agent 可見性也沒有說明 | N6 後 FA 內部仍走 T5 relay，只把 B 的回合和 wake 改成 Responses（[N6 設計 §5](../n6-responses-migration-design.md#5-t5-和官方-a2a)）。語意依 fcd 的 [T5 收件匣研究](../foundry-inbox-resilience-research.md)，摘要見第 10 節 |
| 團隊限定 connector | toolbox 是 project 層級，沒有每個 Team 的 ACL（[toolbox-overview](https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview)） | 組織共用 connector 可改用官方能力。Team 自己的 MCP 放在 Team toolbox `fa-team-{team}`：設定頁經 API 薄轉送官方 Toolbox REST，API 只核 owner、固定 Team 名稱、以官方版本 `metadata` 防重送；官方版本與 `default_version` 是唯一紀錄（Neal 2026-10-06「改走官方」）。含金鑰的 Team connection 由 API 以 ARM 建立（Neal 10-06 10:41 批准 API 身分 project 範圍 Contributor），細節見 [Shared Context README](../../src/fa-shared-context-poc/README.md#team-mcp-official-toolbox) |

### 6. 進度表 #303 列出的六項官方能力

| 能力 | 判斷 | 依據 |
|---|---|---|
| Routines（WAKE-05／PA-09／E-07） | **本批用官方Job薄轉送（§9⑥）；Foundry Routines延後**。**2026-10-07 起改為 API 兼任時鐘，Job 路徑已移除；Foundry Routines 經 dev 探針實測不採用（[ADR-0006 補充](0006-team-trust-boundary-grokbot-mode.md#api-routine-clock)）** | 固定2.3.0的routine action無FA逐次headers／N1握手接點；先用[Scheduled Jobs](https://learn.microsoft.com/azure/container-apps/jobs)。原`H(dispatch_id)`提案不採，根RID改依scope／routine／排定UTC，重試不換ID；[差額與實作](../../src/fa-shared-context-poc/README.md#native-routines)。新資源／IAM／live另批 |
| A2A tool（CAP-02／SP-22） | **部分改用** | 送出端可以經 toolbox MCP 使用（`a2a` 1.0 GA）；接收端需要 Responses，見第 4 節（[agent-to-agent](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent)） |
| Toolbox 和 project connection（SP-10／24／29、TOOL-02） | **組織共用 connector 直接改用（N5）；批准照工具自己的 `require_approval`（2026-10-06）** | 官方有 Copilot SDK bridge 範例；官方原文是「credential injection…consuming agents don't manage per-tool credentials」，表示秘密不會進入 VM（[toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox)）。官方也寫明 toolbox 不擋 `tools/call`，`always` 要由 runtime 每次呼叫前列出工具與參數請真人批准（[enforce tool approval](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent#enforce-tool-approval)），所以 FA 原生 HITL 依這個值暫停、批准、恢復，Team 規則不再管 Toolbox 工具 |
| Browser Automation（CAP-03／SP-11） | **保留 FA Playwright（第 9 節 ③）** | 改用官方能力需要新的 Playwright Workspaces 資源和 connection，也會增加費用，而且仍是 preview（[browser-automation](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/browser-automation)）。FA image 已內建 `@playwright/mcp` 0.0.82 |
| Computer Use（CAP-05） | **保留 FA 桌面** | 官方原文是「The tool doesn't directly control a device. Your application executes each requested action」，所以桌面仍要由 FA 提供。模型也需要另外申請，且只開放部分區域（[computer-use](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/computer-use)） |
| Memory store | **已部分採用；Agent 私人記憶已改直連（#315，預設關閉）** | 依官方 `FoundryMemoryProvider` 模式：真人回合前查詢，成功保存後更新一次（[hosted memory quickstart](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-memory-hosted-agent)）。agent identity 需要 Foundry User 和 Cognitive Services OpenAI User，見第 9 節 ②。scope 不是 ACL，所以團隊記憶仍經 API（[memory-usage](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)） |

### 7. 最小遷移切片

- 舊對話（現有 CID）的讀取和追問，都維持原本的 Table 路徑，不搬移也不刪除。
- B23 等原本是 `unknown` 的回合維持原狀。
- 新對話由模板旗標切到新路徑，例如 `state_profile="foundry-native-v1"`。

| 切片 | 內容 | 負責／依賴 |
|---|---|---|
| N0 | 部署一次 live smoke，確認三件事：① 開啟 resilient tasks 後，TaskManager 能啟動；② agent identity 能讀寫 State store；③ 換 Agent 版本後，仍讀得到同一個 store。限制：使用正常 main build，API 和 Hosted 必須同版；只在 test agent `00000000`（fa-agent-cc96）執行，不動 A／B；不新增資源或 IAM。若需要新的 RBAC，就停下並交給 Neal 決定 | 71c7（子 session 905fb181）撰寫 probe，CODE／offline 已合併（#313）；fdbb 部署；總協調已批准 |
| N1 | VM 端實作 `rid`、`conv`、`turn`、resilient chain 和重新進入規則，並用官方 local backend 做離線測試。N3 前 checkpoint 仍使用 CTX-03 Blob，但必須先保存 checkpoint，再寫入 `turn:<n>` | 71c7；N0 |
| N2 | API 新路徑：新 CID 不再寫登記列，改為「驗證 owner → 呼叫 Agent」；8765 經 Agent 讀取進度和歷史；帳本不變 | 79fa，等 N1 介面確定後開始；N1、T1、T6 |
| N3 | checkpoint 分段放進 State store，並用 `$HOME` 熱恢復（介面見下方） | 83d4；N0 確認前不合併 |
| 私人記憶直連 | Hosted 用自己的 agent identity 查詢和更新私人 memory scope | 83d4，#315 已合併、預設關閉；部署前由 fdbb 指派角色 |
| N4 | 官方時鐘＋原N1 occurrence入口；不在FA內建timer／agent loop | N1；§9⑥已重新核准S1／S3和S2 code/offline，預設關。Job／MI／app role／pull權限、部署與live仍須Neal另批。**2026-10-07 起改由 API 兼任時鐘（#728，dev PASS），Job 路徑已移除，不需要新 MI／app role／pull；見 [ADR-0006 補充](0006-team-trust-boundary-grokbot-mode.md#api-routine-clock)** |
| N5 | 用 Toolbox 取代組織共用 connector broker | 第 9 節 ②已允許；排在 N1～N3 之後。**2026-10-22**：WebIQ 收斂到只走 toolbox（Neal「只留 toolbox」），舊的 API broker entry `WebIQ-MCP` 與 API secret 退場，見 [README](../../src/fa-shared-context-poc/README.md#webiq-toolbox-only) |
| N6 | 改用 Responses：Foundry 代管歷史、stored background 和重入；T5 內部仍走 relay，由 N6-4 改用 Responses。對外 A2A（N6-6）不做（Neal 2026-10-02 15:08，#396） | Neal 2026-10-02 08:16 決定實作，總協調 08:57 核准設計。切片 N6-0～N6-6 與 owner 見 [N6 設計 §8](../n6-responses-migration-design.md#8-切片依賴owner工作量)；N6-0 先合併，其他片才能合併 |

**N1／N3 checkpoint 介面**（71c7 已同意，N1 的 turn DTO 直接引用，不另定分片格式）
- `NativeCheckpointRef` 就是 manifest，整個寫進 `turn:<n>`。它的 codec 是 `native-session-fs-v1` 或 `native-session-fs-responses-v1`，和 Blob 的 `StoredSdkArchive` 互不冒充。
- 每段原始資料 512 KiB，最多 16 段。key 由 ref 推出，格式是 `ckpt:<sha256>:<i>`。
- 函式：
  - `save_checkpoint`：全部分段讀回並驗證後，才回傳 ref。
  - `load_checkpoint`：可以先使用已核過 hash 的 `$HOME` 熱副本。
  - `verify_checkpoint`：一定逐段核對 store，不讀 cache；這是補 `completed` 前必須使用的檢查。
  - `open_durable_store(name, create=)`：純讀路徑不建立 store；store 不存在時回 404。
- 只有原本已知的一般或 Responses codec 走 native；diag 和 compaction 保留 Blob。store 回 503 或結果不明時，不換 provider 重存。

### 8. IAM 和 Azure 資源

- **N0～N3**：預期不需要新增。State store 使用 agent identity，以及平台注入的 project endpoint；官方未列出額外 RBAC，N0 會實際確認。
- **N4**：本批Job／專用MI／API app role與pull權限均需另批；Foundry原生routine亦是控制面操作，不由程式合併推定批准。2026-10-07 起改由 API 兼任時鐘，以上 Job／MI／app role／pull 都不再需要（[ADR-0006 補充](0006-team-trust-boundary-grokbot-mode.md#api-routine-clock)）。
- **N5、私人記憶直連**：每個 Hosted Agent 的 agent identity 都需要 project 範圍的 **Foundry User**，以及 **Cognitive Services OpenAI User**。Neal 已選 (b)，見第 9 節 ②。
  - 角色名稱：Foundry User 就是改名後的 Azure AI User，role ID 相同（[rbac-foundry](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry#built-in-roles)）。
  - 需要 OpenAI User 的原因：官方 hosted memory quickstart 要把兩個角色都給「the deployed agent's runtime identity」，否則記憶寫入會回 401。
  - 指派方式：每個 Hosted agent 都有自己的 blueprint 和 identity，而「Agent identity blueprints can't be assigned Azure RBAC roles」（[agent-service-principals](https://learn.microsoft.com/entra/agent-id/agent-service-principals#key-differences)），所以必須逐一指派給各個 agent identity。
  - 用「＋」建立的新 Agent，由 fdbb 手動指派。API MI **不新增** Foundry Project Manager；官方說明自動指派需要建立者有 role assignment 權限（[hosted-agent-permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions#explicit-project-level-access)）。
- **N6 的 A2A**：對外 A2A 需要呼叫端擁有目標 Agent 或 project 的 **Foundry Agent Consumer**（新 IAM）。N6-6 不做（#396），所以不新增。
- **Team MCP 的 connection**：設定頁建立含金鑰的 Team connection 是 ARM 控制面寫入，Foundry User 沒有。Neal 2026-10-06 10:41 決定「assign project 範圍的 Contributor」，總協調已指派並讀回：dev `id-facontext-api` 在 project `faexample`、stage `id-facontext-api-stage` 在 project `faexample-stage`，範圍都是 project 本身。程式只寫自己 Team 推算出的 connection 名稱，不刪除。
- **Browser Automation**：需要新的 Playwright Workspaces 資源；依第 9 節 ③，不採用。
- **Computer Use**：需要申請模型存取，且有區域限制；目前不採用。

### 9. 決定狀態

1. **① Neal 2026-10-01 18:03 同意**：先完成 N1～N3 和 T5 收件匣，N6 之後再評估（重要不緊急，待辦 #321）。**已由 Neal 2026-10-02 08:16 改為「改成Responses」**，N6 改為正式工作，見第 4 節。
2. **② Neal 於 2026-10-01 16:37 選 (b)：接受風險，照加**（issue #306）。16:30 那次同意是依錯誤說明做出的，已作廢。
   - 每個 Hosted Agent 的 agent identity 都加上 project 範圍的 Foundry User 和 Cognitive Services OpenAI User，用於私人記憶直連和 N5。
   - **已知例外**：Foundry User 的權限涵蓋整個 project 的 data-plane，包括所有 memory store、所有 scope，以及 `agents/*/write`。所以任何 Agent，包括遭提示注入的 Agent，都可能讀寫別的 Team 的記憶，或修改其他 Agent。POC 期間接受這個風險，並在 [ADR-0006](0006-team-trust-boundary-grokbot-mode.md) 記為隔離原則的例外。
   - 用「＋」建立的新 Agent 由 fdbb 手動指派；API MI 不新增 Foundry Project Manager。
   - 記憶服務的用量（查詢 embedding、非同步萃取）目前只寫在 log。總協調決定部署後就開啟，不等 T1b；T1b／T4 接通用量回報時，再一起入帳。
3. **③ 由總協調暫定，Neal 可以推翻**：保留 FA 自己的 Playwright，不新增 Playwright Workspaces 資源。
4. **④ N4 排序（Neal 2026-10-02 15:26，15:30／15:33／15:37 調整）**：15:26 原文「這個項目排到跟 B1/B2 target state 一起做」「我是說N4」。
   - 15:26 先把 N4 排到和 B1／B2 target state 同一層，排在「ADR-0006＝ADR-0007＝多 Agent 協同合作」之後，不再跟著 ADR-0007 其他切片一起排。
   - 15:30 原文「對話內表單、排程相關提示、回覆／未完成工作提醒、沉默回合，語音通話 <--- B1/B2 target 完成後再做」。所以 PA-09、PA-10 從 N4 這組移出，延後到 B1／B2 target 完成後。SP-22 群組聊天照原計畫，仍和 B1／B2 同層做（Neal 15:33 原文「群組聊天 <--- 這個可以照原來計畫做」）。E-07 依賴已延後的 PA-09～12，所以實際上也跟著延後。
   - 15:37 原文「B1/B2 target 完成後再做」，指 B2 的 WAKE-05、WAKE-07、TURN-07、MEM-02。這 4 項和上一點同批延後。
   - **N4 隨 WAKE-05 延後（總協調判讀，Neal 可改）**：N4 是 WAKE-05 與 PA-09 的計時器實作，兩者都已延後，單獨先做 N4 沒有用處。這取代 15:26 後「N4＋WAKE-05 排在 B1／B2 同層」的寫法。
   - 在 Foundry 建立 routine 屬於控制面寫入，live 前仍要另外批准。排程不等於批准建立。
   - 這些都只是排程，不改帳本分數、B2 定義，也不改 76 項的定義。
5. **⑤ 探針標註、SP-27、改程式規則（Neal 2026-10-03 13:16）**：原文「都同意」，是 Neal 對總協調三項建議的回覆。
   - **探針標註**：探針由總協調判讀就算數，有爭議才找 Neal。
     - 帳本的 run 只能追加。所以原本標「總協調初判、待 Neal 確認」的 6 筆 SP 探針 run 不改，另各追加一筆審查紀錄 run，`human_review` 記為總協調判讀、Neal 13:16 授權。
     - 這只表示這些標註成立，不等於 target 已 accepted。各 ID 仍照帳本規則判斷：要 `implemented`、已知 baseline，以及 full_target 的 target_acceptance。探針都是單次樣本、`subset`，所以這次沒有任何 ID 升到 accepted。舊的 FAIL 照留。
   - **SP-27（購買）**：FA 沒有購買或付款工具，先標成暫不適用；等真的要接購物或付款功能時再做。帳本沒有「不適用」狀態，所以只在 SP-27 的 blockers 和進度表的待核欄寫明，不改計分規則。
   - **改程式規則**：長期只保留 R1（覆寫、刪除或丟棄未提交的改動前先問），R2（唯讀 git 與測試先問）不加。R1 由 fdbb 在 dev 以 Team 規則加上，時間在 SP-16 的暫時規則刪除、讀回 0 條之後。
   - 和 ④ 一樣，不改 76 項的定義，也不改 B2 定義。

6. **⑥ routines／reminders重新啟動（總協調核准，Neal可推翻；2026-10-03／04）**：
   核准短計畫`20c11b363afb428d38602eaee38f94d76dd49df3dfd0bbcc61a19974cd5e1e68`，先做S1／S3與S2的
   default-off code/offline。選Container Apps Scheduled Job（GA）作薄時鐘；Foundry Routines／reminder preview不啟用。
   這只取代④中PA-09／PA-10／WAKE-05與N4本片的延期，不推進其餘延後項目；
   專用MI、app role／assignment、Job、ACR pull、部署及S4 live仍須Neal本人另批。

### 10. T5 收件匣：原生語意與接線界線（fcd，2026-10-01）

完整研究見 [foundry-inbox-resilience-research.md](../foundry-inbox-resilience-research.md)，SHA `b005c3a7…`。這份資料只有研究，尚未部署，也沒有 live 驗證。重點如下：

- **本階段**：N1～N3 使用 Invocations 和 State store；T5-I/W與其並行。跨VM傳話及完成後即時wake沿既有API relay，不等N4；Routines只用後續排程，A2A／Responses留到N6。
- **接收回條**：A 要收到持久接收回條，才可以說「已交給 B」；B 忙碌是排隊，不是失敗。B 完成後，結果要持久送回 A，A 在原 CID 以新的 RID 和新回合處理。
- **去重**：使用帶來源 scope 的穩定 message ID。`create_item` 衝突時，先讀原 payload hash、回條或 outcome：
  - 同一個 ID、內容不同：拒絕。
  - 已完成：回傳原結果。
  - 已接收但尚未啟動：沿用原工作。
  - 結果不明：不重做外部效果。
- **result 綁定**：每個 result 綁定那一則已接受的 request message key，不是讓整個 root RID 共用一個 result。
- **不外推的地方**：
  - State store 沒有指定目標 Agent 的參數，不能據此判斷跨 Agent 的可見性。
  - 把 `task_id` 設為 Agent，是否就能跨 CID 排他並維持 FIFO，要由 N0 確認。
  - 單一 item 的 CAS 不能說成跨 item 或跨 Agent 的原子交易。
- **跨 Agent 寫入**：收件端沿N1寫自己的store。總協調已選現有API relay的Team control store保存root名額，一個小item只記message ID／hash；不記內容／結果／回條、不做queue。獨立Team partition不進舊authority的完整inventory。
- **native限制（Neal18:46／18:47）**：root新交辦預設10，一處常數；不加2hop或每turn100。保留host綁sender、同Team／非self、message ID去重及每request唯一result，舊lane不改。
- **結果與 WAKE**：先保存B的結果／completed原件，再用原`in_reply_to`作A store的唯一result及wake RID；A原CID開新turn。回調重入回同原件，不重跑SDK，read/history不發通知。
- **舊資料**：B23 舊 Table 工作只依原停止和 claim-fence 證明收尾，不搬移、不刪除、不重送，也不是新架構的前置條件。

## 取代與影響

**只適用新對話，`state_profile="foundry-native-v1"`**
- [ADR-0002](0002-shared-context-without-file-share.md) 原本由 Table 保存精確工作狀態；新對話改由 Agent 的 State store 保存。
- [ADR-0006 §4](0006-team-trust-boundary-grokbot-mode.md#4-入口票vm-內執行回條費用紀錄的最小分工) 原本由 API Table 保存入口票、開始回條和結束回條；新對話改用單一 `rid` 回條。
- [ADR-0006 §2](0006-team-trust-boundary-grokbot-mode.md#2-每個-agent-一台-vm取代每個對話一台) 的每個 Agent 排他佇列，改用官方 `@multi_turn_task` 實作。
- 團隊共享資料（§3）、用量帳（§4 費用紀錄、§5）和 T5 跨 Agent 傳話不變。

**不變的部分**
- 舊對話和舊資料繼續使用原路徑。
- 費用、批准、權限的界線不變。

**相關工作線**
- T2 的 FA 自建排隊和回條帳本不再擴充；#284 維持 draft。
- 79fa 只收尾現有 Table admission。
- fcd 的 T5 收件匣語意已收錄在第 10 節。

**AGENTS 第 3 條**
- 目前內容是「依 ADR-0002，Table 保存精確工作狀態」。
- N1 合併時再同步修改；在新路徑上線前，現況仍以原路徑為準。

## 官方依據

- Hosted agents 概念與協定：https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- State store：https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store
- Hosted sessions：https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions
- Long-running agent resilience：https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience
- Resilient tasks 指南（core 2.1.0）：https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.1.0/sdk/agentserver/azure-ai-agentserver-core/docs/tasks-guide.md
- Hosted agent 權限：https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions
- 限制與配額：https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions
- Agent 監控儀表板：https://learn.microsoft.com/azure/foundry/observability/how-to/how-to-monitor-agents-dashboard
- A2A 工具：https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent
- 啟用 A2A endpoint：https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
- Routines：https://learn.microsoft.com/azure/foundry/agents/how-to/use-routines 、https://learn.microsoft.com/azure/foundry/agents/concepts/routines
- Toolbox：https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox 、https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview
- Browser Automation：https://learn.microsoft.com/azure/foundry/agents/how-to/tools/browser-automation
- Computer Use：https://learn.microsoft.com/azure/foundry/agents/how-to/tools/computer-use
- Memory：https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage
- Hosted memory quickstart：https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-memory-hosted-agent
- Foundry RBAC 角色：https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry
- Agent identity 與 blueprint：https://learn.microsoft.com/entra/agent-id/agent-service-principals
- Task state 與 checkpoint：https://learn.microsoft.com/azure/foundry/agents/how-to/manage-task-state
- N6（Responses）：
  - Hosted agent runtime contract（`x-agent-user-id`、只轉送 `x-client-*`）：https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract
  - 在一個 session 中多工多位使用者（明示 `agent_session_id`）：https://learn.microsoft.com/azure/foundry/agents/how-to/multiplex-session-users
  - crash 後恢復長時間工作：https://learn.microsoft.com/azure/foundry/agents/how-to/recover-long-running-work
  - 遷移 Hosted agents（容器協定 2.0.0）：https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview
  - Responses resilience 契約（responses 2.2.0）：https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/resilience-contract.md
  - 多協定 host（responses 2.2.0）：https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/handler-implementation-guide.md
  - 完整清單見 [N6 官方查證](../n6-responses-official-research.md)。
