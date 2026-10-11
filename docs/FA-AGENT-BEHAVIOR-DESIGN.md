# FA Agent 行為設計背景：少等待、少打擾、少失憶、少卡住

日期：2026-09-15。狀態：**設計與研究背景；已由 [行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)承接，不再作開發／驗收規範。**

使用者已要求規格化並對齊目前 M4。最新已發布基準是 `f097fa6` 的 M2／M3，M4 已批准實作中；本文件原先以 M1 `3392c8e` 所作的現況盤點及 M2-A／M2-B 排程不是最新狀態。開發按[規格](FA-AGENT-BEHAVIOR-SPEC.md)、[ADR-0004](adr/0004-agent-behavior-control.md)與[BR-01–BR-06](POC-ANALYSIS-AND-PLAN.md#m4-之後的行為開發切片)，新增行為在本批 M4 之後落地，未授權部署或額外用量。

## 1. 結論與設計範圍

**建議保留 Foundry Hosted、Copilot SDK 與既有共享 Context，由 Hosted 內的 Agent Harness 接回合行為，由 FA 耐久控制管理跨程序的工作、授權與交付責任。模型決定怎麼做，但不能自行宣布授權、恢復或交付已成立。**

要對標的是「不用一直照顧助理」的體驗，不是複製 Grok Bot 的 prompt、每個內部步驟或電腦共享方式。文章是需求輸入；使用者後續提供的靜態研究及有界補充報告，補上函式與呼叫路徑依據，但本工作樹沒有原始 bundle、模型端封裝或工作負載追蹤。研究可用性與未確認項目見第 4.4 節。

本提案不改變 [需求基線](requirements.md) 與 [ADR-0003](adr/0003-hosted-environment-continuity.md)：每 agent 屬於一個 Team、私人 Hosted 環境隔離、可更換 compute、共享 Context 取代共同掛載、四 agent 執行容量與人類批准仍保留。跨 Team 共用使用者記憶、額外執行槽、外部 VM 與新增基礎設施都不是既定決策。

證據分四類：

- **現況**：本工作樹 `3392c8e420200964035484abd94d2e92cba34c05` 的程式、測項與已保存報告。本次沒有重新執行 Azure POC。
- **官方契約**：[平台查證](FA-BEHAVIOR-PLATFORM-EVIDENCE.md) 記錄來源與適用界線；SDK／平台能力不等於已接入 FA。
- **使用者提供的 Grok 研究**：2026-09-15 的 `REPORT.md`、`REPORT-ADDENDUM.md` 與索引／manifest 貼文。引用研究者的靜態證據，不宣稱本工作樹重讀原程式或完成動態驗證；未把私人對話、設定與原始碼副本加入 repo。
- **提案**：下文的 Interface、資料欄位、預設值與驗收門檻，均需在實作前確認，不直接升格為新的 FR 或已批准 ADR。

## 2. 原 M1 快照的盤點（不是目前進度）

本節保留原 `3392c8e` 分析。M2 SDK 文字／恢復／交接與 M3 成果／真人批准現已有結果；目前差距以[規格第 1 節](FA-AGENT-BEHAVIOR-SPEC.md#1-最新基準與適用範圍)為準，不依本表重做已完成工作。

| 面向 | 目前可沿用的基礎 | 本提案要補的缺口 |
|---|---|---|
| Hosted 入口 | `hosted_main.invoke` 呼叫 `HostedClient.dispatch`，執行明確指定的 API／GUI／environment 探針 | Copilot SDK 自主工作循環、結構化工作事件與完成交付 |
| 精確工作狀態 | `ContextService` 有 owner、revision、handoff、published 不可變；`TableRepository.replace` 使用 ETag | 工作執行狀態、durable inbox／outbox、交付責任、取消與不確定結果 |
| Context | `assemble` 讀權威工作，以 goal 前 2048 字依序搜尋 Team／私人記憶各最多 10 筆 | Token 預算、長歷史壓縮、相關性、來源與權限版本管理 |
| 記憶 | `FoundryMemory` 有非同步抽取、scope search；operation receipt 不依賴 process memory | 一般記憶提案、證據稽核、矛盾／到期／撤銷與 User Model |
| 身分與資料隔離 | Context API 驗證身份後選 actor／Team／scope；Hosted 沒有原生 Table／Memory 資料權限 | 人類委託與 agent 轉交的授權鏈、所有工具共用的副作用批准 |
| 私人環境 | M1 報告記錄指定檔案、工具設定、Chromium profile、替代 session 與相容設定版本恢復 | SDK 對話與業務 checkpoint 恢復、未知副作用核對；不能以 M1 代替 |
| 多工與可靠性 | 有 POC controller、CAS 衝突與明確 stop/resume 案例 | 正式 mailbox、背景 job registry、事件喚醒、容量控制、重複事件去重與交付補償 |

程式依據：[入口](../src/fa-shared-context-poc/hosted_main.py)、[dispatcher](../src/fa-shared-context-poc/shared_context/hosted.py)、[ContextService](../src/fa-shared-context-poc/shared_context/service.py)、[TableRepository](../src/fa-shared-context-poc/shared_context/storage.py)、[FoundryMemory](../src/fa-shared-context-poc/shared_context/memory.py)。

現有測項已表達應保留的契約：[owner／ETag／發布保護](../tests/shared_context/test_api.py)、[私人 scope／記憶失敗不偽裝成功](../tests/shared_context/test_memory_context.py)。它們不驗證本提案的新能力。M1 的較新狀態以 [M1 結果](M1-PRIVATE-ENVIRONMENT-POC.md) 為準，不沿用早期規格中「HOME 尚未驗收」的歷史快照，也不擴張 M1 的成功範圍。

## 3. 文章九個層面如何落到 FA

| 文章目標 | FA 建議行為 | 不能只留在 prompt 的規則 |
|---|---|---|
| 回應體驗 | 接單後立即有狀態；重要變化才更新；最後交付答案或可取回的成果 | 每個需要回答的請求建立 `DeliveryObligation`；ack、模型結束、job 完成均不等於已交付 |
| 自主性 | 低風險且已授權的選擇自己決定；真正阻塞才問 | `PolicyDecision` 區分允許、需批准、拒絕；換成 shell／browser 不得繞過 |
| 對話連續性 | 使用者感覺是同一位助理；每個 work 有可接續的脈絡 | Logical conversation／work 與 SDK session／Hosted session 分開，事件可重播，不無限塞滿模型 context |
| 記憶 | 維護有來源、可更正、會過期的偏好與知識 | 提案、audit、evidence validation、scope 授權與版本生命週期；批准與操作 receipt 不進語意記憶 |
| 背景多工 | 長工作可交付背景執行，入口仍可接收訊息、查進度與取消 | Durable job、completion event、execution lease；一般 thread／subprocess 不算耐久工作 |
| Context／tools | 穩定指令、按需載入 skills／schemas、有界檢索 | Context manifest、catalog version、token budget；cache 不得保留已撤銷權限 |
| 安全 | 分清誰說的、可代表誰、可以做什麼 | 伺服器建立 provenance、執行時驗證 capability；外部文字不升格為委託 |
| 安靜 | 沒新資訊的 routine／FYI 不通知，不互傳「收到」 | `Silence` 是有理由的結果；不能免除使用者正在等待的交付 |
| Recovery | 中斷接續、重複事件不重做、結果不明先核對 | Inbox／outbox、operation ledger、fencing、有限重試與人工處理出口 |

最重要的取捨：**不預設每件事都多 agent 化，也不承諾所有外部操作 exactly-once。** 前者可能增加等待與 tokens；後者取決於目的端是否支援冪等與結果查詢。

## 4. 架構選擇：回合行為靠近 agent，耐久控制跨程序延續

### 4.1 三種做法的取捨

| 做法 | 優點 | 判斷 |
|---|---|---|
| 只改 system prompt | 最快看到回覆風格改變 | 不採作完整方案；模型仍可能忘記交付、重試副作用或遺失背景工作 |
| 所有狀態與喚醒都放 Hosted process | 接線簡單、接近單機 harness | 不採作權威控制層；不能靠會中止的 process 保證自己的恢復與通知 |
| Hosted Agent Harness＋FA 耐久控制 | 回合 hooks、context 與工具接線靠近 SDK；權威工作、授權與交付跨 compute 延續 | **建議**；先做小型 durable 閉環，不把所有控制移到遠端，也不拆成大量獨立部署 |

資料流是：使用者入口 → 驗證與持久接單 → mailbox／排程 → 指定 Hosted 的 SDK／工具 → 工作與成果確認 → outbox → 原請求的授權回覆入口。

模型產生的內容先是候選輸出，不自動公開。內部工具結果、稽核 metadata 與使用者訊息分開；不要求儲存或顯示模型的私有推理過程。

### 4.2 建議的 deep modules

下列是 **FA 自訂 Interface 概念**，不是聲稱 Foundry 或 Copilot SDK 有同名方法。

| Module | 小型 Interface | 藏在 Implementation 裡的責任 |
|---|---|---|
| `AgentRuntime` | `accept(event)`、`read(cursor)` | 驗證來源、durable inbox、conversation 單寫者、交付責任、outbox 與有界 nudge |
| `JobRuntime` | `start(spec)`、`observe(job_id)`、`cancel(job_id)` | 排程、全域容量、lease／fence、Hosted binding、checkpoint、completion、reconciliation |
| `ToolBroker` | `execute(intent, execution_context)` | 即時授權、批准綁定、操作去重、目的端核對、credentials 與工具路由 |
| `ContextBuilder` | `build(turn_ref, budget)` | 權威快照、近期原話、摘要、檢索、catalog、版本與 token manifest |
| `MemoryGovernor` | `propose(candidate)`、`resolve(query, access_context)` | 抽取與稽核、證據驗證、衝突、scope、到期及撤銷；不取代權威資料庫 |

`Delivery` 可先留在 `AgentRuntime` 內部，不讓每個呼叫者重做「已送出？」判斷。既有 Table 與 Memory Adapter 可沿用；不再疊一個只轉呼叫、沒有新增責任的包裝層。

真正會變動的 Seam 是執行位置與回覆通道：離線受控 executor／Hosted executor、驗收入口／Teams。Adapter 需有契約測項；不先建立十種沒有實際使用者的抽象。Hosted 映像只載入 SDK、純傳輸與工具契約，不因共用程式碼而引入控制層的 JWT／Table server 依賴。

Module 是邏輯責任，不等於一個獨立部署。建議的放置方式：

| 執行位置 | 責任 | 不應自行承諾 |
|---|---|---|
| Hosted Agent Harness | 接 SDK events／hooks、組裝 context、工具前後處理、產生候選訊息、消費背景完成事件 | 不用 process memory 取代持久工作；不自行擴權或領取另一執行端的工作 |
| FA 耐久控制 | 持久接單、execution owner、容量、批准、inbox／outbox、成果與交付責任、失聯核對 | 不重造 SDK 的推理迴圈；不把所有背景完成直接變成使用者通知 |
| 平台／工具執行端 | Foundry lifecycle、適用的原生 resilient handler、shell／browser／connector 執行 | 執行回條不等於業務成功或人類已收到；不同運輸與執行位置分開記錄 |

即使「需要補回覆」的即時判斷在 Hosted 內，未完成的交付責任仍須持久保存。控制層不需要另一個模型逐步監督每個 token；正常排程、去重及有效批准核對是一般程式責任。

### 4.3 官方已有什麼，不必全部重造

以下來源 ID 對應 [完整平台查證](FA-BEHAVIOR-PLATFORM-EVIDENCE.md)，均為文件／原始碼證據，不是本輪 FA 實測。

| 官方能力 | 可以沿用 | 仍由 FA 負責 |
|---|---|---|
| [Hosted runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)（F2） | 私人 session 的 runtime 與終止契約 | 目前 Invocations 不管理 history；維護 FA／Hosted／SDK 映射 |
| [Resilient execution preview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/long-running-agent-resilience)（F3） | 已註冊 handler 的 input persistence、work lease 與 process-loss recovery | 從 handler 開頭重入；FA 保存 checkpoint、工具結果與副作用紀錄，不能重做已完成效果 |
| Copilot SDK session persistence／events（S1–S3） | Resume、infinite sessions／compaction、tool lifecycle、permission hooks | SDK 官方說沒有 session locking；`session.idle` 不會 resume replay，更不等於 final 已交付 |
| Copilot SDK deferred tools（S3） | 固定 TypeScript source 已有 `Tool.defer`、`toolSearch` 等 Interface | 核對發布版本及所選語言；不能直接把 TypeScript 欄位寫進目前 Python Hosted |
| [Memory Store](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage)（M1） | 語意記憶、非同步 update、scope search、管理能力 | 先授權、記憶 audit／來源治理；search 本身不是 durable background job |
| [Table transactions](https://learn.microsoft.com/en-us/rest/api/storageservices/performing-entity-group-transactions)（T1–T2）與 Service Bus（Q1–Q3） | 有界交易、條件更新、訊息 buffer／settlement | 跨服務一致性、全域容量、結果未知與使用者交付 |

實作優先使用固定 SDK 版本已有的 hooks、compaction 與 tool discovery，不另做第二套一模一樣的模型迴圈。FA 補的是這些能力外圍的產品契約，不是取代 Copilot SDK。現有 AgentServer 依賴版本不能僅憑最新版文件就視為已支援全部 resilience 行為。

### 4.4 Grok 有界補證據：可結案，不是運行保證

使用者提供的補充報告自述保留 E01–E34、追加 E35–E52；host 回報已解析 52 筆且 ID 唯一。此工作樹收到的 JSON／manifest 附件仍是折疊貼文，未獨立取得原 JSON bytes、核算產物 hash 或逐筆重驗。來源 bundle 的 `294fba1` 只是研究中記錄的版本 marker；來源 fingerprint 由 host preflight 提供，不是本工作樹重新計算。

以下 E 編號指該 Grok 研究，不是第 4.3 節的官方來源 ID：

| 問題 | 研究支持的結論 | 仍須保留的未知 |
|---|---|---|
| 執行歸屬 | profile／proxy／local route 與 Temporal local-wake 排除可追到程式；E36 | 特定 request 的實際 owner 未取得事件證據；E37 |
| Shell 首次通知 | spawn 回傳、start 診斷、registry／watch consumer、父代理 revival 可見；E38–E41 | spawn 成功到首次 subscribe／arm 的 producer 接點仍未連起；不是已知缺陷，也不是端到端通知保證 |
| 交付語義 | admission、ack obligation、可見 send、channel attempt、transcript／SSE sync 有靜態路徑；E42–E47 | UI receipt／render、人類已讀均未建立證據；suppressed send 也可能回 tool success |
| 有效批准 | resolver、surface wiring、批准失效與獨立 local permission 可見；E48–E49 | 特定工具呼叫的 off／shadow／enforce 模式未知；E50 |

**補充報告的「第 1–5 層」重新定義了分層，不能直接套回原交辦的六個收件階段。** 其第 4 層是 channel delivery attempt，第 5 層是 transcript／SSE synchronization，並不是 client 已收到與 UI 已顯示；其第 6 層一起保留 human read／實際 UI receipt 的未知。資料已進 SSE write 不證明 UI render，語義分層也不是一筆已發生的完整事件序列。

FA 可採用 execution owner、持久 obligation、背景結果先入 inbox、批准 fingerprint／epoch，以及記憶／摘要失效檢查等原則。不要照抄非耐久 admission fallback、只靠 send count／reaction 判斷任務交付、或認為一般背景 process 會自動恢復。也不因 Grok 有 Temporal 分支就新增 FA 的 Temporal 選型。

**研究停止點已達到：** 足以繼續 FA 架構與狀態模型；不再為填滿未知而掃描受限 daemon、私人 transcript 或製造新工作。憑證在進入研究工具輸出／模型前是否已遮罩，另列為環境負責人的待確認事項，不由最終報告省略值來推定。

## 5. 接單、執行、交付是三件事

### 5.1 事件與狀態

每個事件至少帶 `event_id`、`schema_version`、`conversation_id`、`work_id`（若適用）、`correlation_id`、`causation_id`、`occurred_at`、payload reference／hash。`tenant_id`、Team、actor、`source_kind`、授權鏈由入口驗證後寫入，不能接受模型自報。

`source_kind` 區分 `human`、`routine`、`agent`、`tool_result`、`system`。每一種都有獨立驗證方式，不把字面上的 `"role": "user"` 當人類批准。

| 狀態軸 | 建議狀態與意義 |
|---|---|
| 執行 | `accepted → queued → running → waiting_dependency / waiting_approval → ready_to_finalize → succeeded / failed / cancelled`；等待解除回 queued |
| 回覆 | `pending → acked → final_prepared → delivered`；另有 `delivery_unknown`、`delivery_failed` |
| 外部操作 | `prepared → dispatching → confirmed / failed / outcome_unknown` |

三條軸不能混用。`succeeded + acked` 仍是未交付；任務失敗也需要清楚交付失敗原因及已保存成果。正在核對外部操作時不能把工作當作完全成功，也不能假稱取消已回滾操作。

每個 conversation 只允許一個狀態寫入者。長工作不持有 conversation 鎖：提交 job 後結束這次模型 turn，由 completion event 開新 turn。工作中的追加指示帶目標 work／revision，排入安全接續點；只合併尚未開始、同一來源與同一 work 的普通補充訊息，保留原 event IDs，不合併批准、撤銷、取消或不同來源。

### 5.2 `DeliveryObligation`

人類交辦會建立一筆交付責任，記錄接收人、通道、應回覆內容類型、對應 work／輸入事件、期限或服務目標、目前交付版本。原請求授權的回覆範圍與「對其他人傳訊」分開；未有批准的外部訊息不能因使用 `SendToUser` 名稱而放行。

接單回覆應由 runtime 根據持久狀態產生，不必每次多呼叫模型。保存接單失敗就回明確失敗，不先說「已安排」。非常短的回答可直接交付 final，避免多送一則 ack。

完成需同時滿足：

1. 所需工作結果已保存；若有 artifact，已驗版本／hash，接收人確實有權取得。
2. 最終訊息已保存為不可變 delivery payload，與正確請求及成果版本連結。
3. 通道回條已確認其可證明的送達層級；`provider_accepted` 不等於人類已閱讀。FA 工作台可另外記錄 client acknowledgement。

候選訊息另帶 `message_kind`（`ack`／`progress`／`final`／`failure`）及 `send_outcome`（`emitted`／`suppressed`／`failed`／`unknown`）。這是 FA 提案欄位，不是 Grok schema。只有與正確請求及成果版本相符的 final／failure，達到該通道事先定義的確認層級後，才能清除最終交付責任；ack、reaction、批准卡、widget、被抑制的訊息及內部 revival 完成都不能代替。

`delivered` 必須附具體的 `confirmation_level` 與 receipt reference；資料保存、通道接受、client 接收、UI 顯示及人類已讀不可折成一個 success。不是所有通道都有每一層回條；未提供的能力保持 unknown，不以模型推斷補值，也不把人類已讀設成所有工作的必要條件。

Watchdog 對「模型回合結束但沒有 final／合法等待狀態」發出有界 `delivery_due` 事件，優先從已保存成果補交，不重跑完整工作。建議每個 obligation 最多兩次模型補交嘗試；再失敗改成確定性的失敗通知與待處理記錄。這是提案上限，不是 Grok 或平台既有行為。

通知節流：開始一次；阻塞、需批准、關鍵成果或完成才更新。UI 可顯示機器 heartbeat；不要把每個 heartbeat 都變成聊天訊息。不能捏造百分比或 ETA。

## 6. 耐久多工、容量與恢復

### 6.1 長工作不等於一定要再開一個 agent

短、連續的工作由目前 agent 直接完成。只有可獨立交付、需要不同角色／能力或預期會長時間占用工具的工作，才建立 executor job。純等待外部結果可登記 receipt，釋放模型回合，完成後再接續。

暫存 subagent 不是新的 FA Agent 身份。它繼承父任務的最小授權與預算，只能回傳結果／草稿；不得自行建立 agent principal、跨 Team 私人環境、批准副作用或發布 SOP。

**入口可回應不等於模型隨時有空。** 四個執行槽滿時，Web／Teams 的接單、狀態查詢及取消受理仍由非模型控制層處理；新的推理要求明確排隊。不能承諾第五位助理會立刻執行。

建議首版採保守配置：每個 active agent 最多一個有實際工作在跑的 executor；所有 SDK 平行 subagents 也需申請容量，不留隱藏平行旁路。等待批准／依賴只有在已 checkpoint 且沒有 shell／browser 等工作繼續跑時才釋放槽位。背景 shell 正在執行就仍占容量；HTTP 回覆結束不能當作已 idle。

是否允許同一 agent 同時推理與背景 shell，留到量測後再決定；初版不靠額外隱形 executor 換取體感。

若產品要求長工作期間仍優先取得模型回答，可選「一槽給互動 Lead、最多三槽背景」模式，總數仍是四；代價是背景吞吐下降。另一種是四槽全投入工作、新推理排隊。這是明確的容量政策選項，不把接單 ack 假裝成模型保持空閒，也不暗加第五槽。無論模式，desktop／browser profile 與同一成果仍維持單一操作／編輯者。

### 6.2 Durable job 與 wake

Job 保存負責 actor、父 work、輸入 hash、狀態、attempt、checkpoint reference、Hosted／SDK binding、`execution_owner`、`execution_epoch`、lease、deadline、批准與預算 reference。`execution_owner` 表示哪個執行端有權啟動／恢復本次工作，與既有 work `owner` 的成果編輯權分開；由權威控制層指派，不接受模型自報。

讀到工作或私人檔案不等於取得執行權。每次 dispatch／resume 都核對 owner、epoch 與 binding；不相符的執行端不得自行接手。FA 端的 ownership transition 與平台原生 handler attempt 必須對映，不能兩邊各自因 timeout 建立替身。

Hosted 的完成事件是正常路徑；控制層另以有限頻率核對逾時 lease、未送 outbox 與已登記的外部 receipts。這種機器 reconciliation 不需要 LLM sleep/poll，也不能因為採事件架構就完全取消失聯偵測。

一般 `asyncio.to_thread`、記憶體 task registry 或 background subprocess，不提供跨 Hosted process 的 job 持久性。優先驗證 F3 的 Invocations resilient task primitive，以受管理 handler 執行可恢復步驟；同時持續保存 checkpoint，不只等收到 SIGTERM 才保存重要結果。

FA job ID 對應固定的原生 work identity；原生 lease 管理 handler attempt，FA 管理業務狀態、容量及交付。原生工作仍在執行或恢復時，FA 不另外建立第二個 job 重跑；透過 receipt 核對後才決定接續。兩層不能各自看到 timeout 就重試同一副作用。

**任意 shell job 的手動 background count／heartbeat 公開 Interface，本次為 `NOT_CONFIRMED`。** 不虛構 endpoint、header 或保活參數。FA 自己的 job heartbeat 是失聯觀察，不是平台必定保留 compute 的證據；F3 也不承諾任意工作在 zero-compute 後於固定期限內自行醒來。

控制層不能只藏在會 scale-to-zero 的 API process 裡。正式 wake 需有可持續的 queue consumer／durable timer 宿主；先以明確受控的測試 worker 驗 Interface，再依平台查證選部署方式。不得把排程表寫入 Table 就宣稱會自行喚醒。

FA 必須自行接完背景 shell 的 producer／consumer 契約：先保存 job、attempt 與觀察意圖，再啟動 executor，關聯返回的 execution ID，確認監聽／核對程序已接上。分開記錄「程序已啟動」與「觀察已啟用」，不能拿 start 日誌、PID 或設定旗標代替後者。跨程序步驟不假裝同一交易；先到的 completion 依既知 job／attempt 進持久 inbox，再核對關聯，不因回條尚未寫完就丟棄或重做工作。spawn 結果不明時維持 `outcome_unknown`，不盲目重啟。

### 6.3 四個槽位與 stale worker

全域容量建議用單一容量登記區的四筆 slot records，CAS 取得 slot 與遞增 epoch。只靠各 Team 自己數工作或 Service Bus session lock，不能保證跨 Team 的四 agent 上限。

Job 與全域 slot 不在同一 Table partition 時，採明確的「保留 slot → 綁定 job → 確認 → 執行」握手及 reconciliation；不聲稱跨 partition 原子交易。只有已確認配對、epoch 相符的 worker 才能領工作。

Lease 過期是「失聯」，不是「舊程序已停止」。工具提交與狀態寫入都檢查最新 fencing epoch；shell／browser 的舊程序另需有終止／失效證據。無法核對實際執行者時凍結該槽，不啟動替身而冒險超量。代價是可用容量暫時降低，應明示 `capacity_uncertain`。

UI 的四個名稱、queue consumer 數量或某次 inventory 快照，都不能單獨驗證全程實際並行數。

### 6.4 恢復分類

| 失敗情況 | 正確接續 |
|---|---|
| Job 完成，通知遺失 | 讀已保存結果補送 outbox，不重做 job |
| Hosted process 消失 | 核對 binding／epoch，恢復私人環境與 SDK checkpoint，再載入權威 work |
| Token 到期／OAuth 尚未完成 | `waiting_auth` 原因碼；同 work／operation 恢復，不另建第二次執行 |
| 外部 API timeout，可能已成功 | `outcome_unknown`；按原 operation ID 查詢，不重新生成 ID 重送 |
| ETag 衝突 | 讀新 revision、重新協調；不能無條件覆寫 |
| 使用者取消時已有操作在途 | 停止新操作，保存已完成／未知清單；不假稱取消會撤銷既有效果 |
| Agent 互傳時對方忙碌 | 持久 mailbox 排隊；不因忙碌丟棄，也不另外建立不受控的 SDK session |
| 超出重試／期限 | 保存原始錯誤 code 與來源，交付阻塞／失敗；不形成無限 nudge 循環 |

`waiting_auth`、`capacity_uncertain` 是等待／阻塞原因，不另外創造第二套互相衝突的工作狀態機。

## 7. 可靠交付與外部副作用：不做假的 exactly-once

建議在相同 Team partition 中，將「inbox 去重記錄＋工作轉移＋outbox」用一次有界交易保存。T1 的硬限制是同一 table／`PartitionKey`、最多 100 operations／4 MiB，同一 entity 每批只能操作一次。採獨立 row namespace，不往現有 48 KiB work payload 塞入全部事件。現有 `Repository` 只有 get／create／replace，**尚無這個交易 Interface**，需要新增且驗證，不能拿目前的單筆 ETag 當替代。

Queue 是喚醒運輸，Table 記錄才是權威。Table 與 queue 不組成同一交易：dispatcher 可以重送，consumer 必須去重。Service Bus duplicate detection 只能作第二層防護，不能取代 application inbox、也不能保證外部 API 的唯一副作用。

| 識別值 | 用途 |
|---|---|
| `event_id` | 同一輸入重送仍是同一事件；去重命中後回原 receipt |
| `operation_id` | 在第一次外部呼叫前持久建立；所有 retry 沿用 |
| `args_digest` | 綁定正規化後的目標與內容，防止同 ID 偷換操作；digest 本身不去重 |
| `delivery_id` | 對應同一接收範圍的同一最終訊息版本；不是每 retry 新建 |
| `provider_receipt` | 目的端原生結果，可用來核對是否已接受或完成 |

「相同內容 hash」不代表使用者只要求一次；兩次合法交辦可有不同 operation IDs。反之 nonce 必須在 crash 後可找回，臨時記憶體 nonce 沒有耐久去重效果。

同 ID 卻出現不同 digest 必須拒絕，不能覆蓋原 receipt。Inbox／operation ledger 的保留期至少涵蓋允許的重試、離線重連與 redrive 期間；過期事件不得在去重資料已移除後被當作全新操作。具體保留與刪除政策另批准。

可承諾的層級：

- 在 FA 的交易／去重範圍內，同一事件只套用一次狀態變更。
- 目的端有冪等 key 或明確查詢能力時，按其正式契約做到 effectively-once 的可見效果。
- 目的端兩者皆無時，timeout 後標 `outcome_unknown`，停止自動重送，讓人核對。不承諾任意 Teams 傳訊、browser click 或 Azure 操作 end-to-end exactly-once。

外部訊息送出成功、FA 尚未寫回 receipt 就中斷，是必要的故障測項。無法確定時不能先標 delivered；也不能因擔心漏訊就無限重送。重連的 FA client 用 cursor／delivery ID 取回持久紀錄，未知狀態也必須可見。

## 8. 「同一位助理」不等於一份無限聊天

### 8.1 Context 的四層

| 層 | 內容與權威 |
|---|---|
| 已批准指令 | Context release、角色、skills catalog、工具政策；版本固定，授權另於執行時查最新值 |
| 精確工作快照 | 目標、owner、revision、批准、成果、未完成 job／未知操作；來自權威 store，不從摘要猜 |
| 本輪脈絡 | 最近使用者原話、relevant events、帶來源的摘要與檢索結果 |
| 長期語意記憶 | 有 scope 與來源的偏好／知識；只作資料，不產生批准 |

Conversation log 是受存取與保留期控制的工作事件資料，不是 durable memory。原始聊天、工具日誌、heartbeat、credentials、目前執行狀態不送進長期記憶。

保留一個穩定的 logical conversation 視角，但模型使用有界片段。不同 work 的敏感內容、不同參與者的 conversation 不因為「同一個 bot」就混在一起。

Compaction 的結構需包含：工作目標、已作決定、限制、未完成事項、待回覆責任、成果 references、source event IDs 及 covered sequence range。精確批准與已完成副作用只保留參照，重載時回權威 store 核對；摘要不是唯一副本。

SDK 原生 compaction 可處理模型 session 壓力，FA checkpoint 則保存產品必要狀態。兩者只各做自己的事，不在同一回合額外重複摘要全部歷史。重要摘要以 schema／來源檢查保護；不把 SDK 自動摘要當作批准資訊必定無損的保證。

### 8.2 Token 預算與 prefix

先設定經所選模型核對的總輸入預算 `B`，另保留輸出及工具往返空間。按「批准指令與必要 work → 最近原話 → 摘要 → 檢索」順序分配。超出時刪低相關片段或擷取有來源的範圍，不靜默截掉批准與約束。

每次記錄 `ContextManifest`：各區塊 tokens、來源、work revision、release／catalog／memory 版本、遺漏原因。現有 10＋10 筆搜尋上限不能當作 token 預算。

穩定 prefix 放已批准的較穩定內容；時間、job heartbeat、工作差異與檢索放後段。批准的新行為版本可在安全 turn 邊界切換；**撤銷權限立即生效，不等 prefix 換版或下一次 compaction**。硬政策更新必要時重建 SDK session，不為 cache 命中犧牲正確性。

工具先提供有權看到的 catalog，再按需取得 schema；schema 要版本化、限定大小與驗證來源。Tool descriptions 也可能有不可信內容。若固定 SDK 版本不能 turn 中安全更新工具集合，就採受控 session 重建或固定 broker Interface，不發明不存在的 SDK 功能。

目前查證的 deferred tools／hooks 部分來自固定 commit 的 TypeScript source，尚未確認對應公開套件與 Python Interface。M2 先核對實際 SDK／CLI 組合，不為使用最新欄位而直接更換語言或追浮動 `main`。

Stable prefix 只有在 provider／model／請求格式支援時才可能命中 cache；實際記錄 cached tokens 與延遲。不能把 Azure OpenAI 的 cache 規則直接套到所有 Copilot-backed 或 BYOK 配置。

記憶服務失敗時，必要知識工作停止並明示原因；明確不依賴該記憶的工作才可在可見的 degraded 狀態繼續。不得破壞現有「錯誤不回空成功」契約。

## 9. User Model 與記憶治理

首版先維持既有私人／Team scope。User Model 的「使用者」是人類，不等於 FA Agent actor；資料還需明確的 subject、owner 與 reader grants。

不照抄文章的全域跨 bot 記憶：先做同 Team、經同意共享的低敏感偏好；跨 Team 取用必須另批准具體用途與讀者，不取消 FR-06 的私人隔離。不得以記憶內容暗示的人名推定可讀權限。

| 記憶類型 | 建議內容 | 生命週期 |
|---|---|---|
| `profile` | 明確、長期的語言／格式偏好 | 保留來源，可更正；使用者新陳述可 supersede 舊值 |
| `episode` | 一段有意義工作的短摘要 | 與工作／證據連結，不逐字複製聊天；問候／感謝不觸發 |
| `note` | 有限用途、較短期的知識 | 到期／降權，查詢時檢查有效性 |
| 精確工作紀錄 | 版本、hash、receipt、批准、進度 | **不是語意記憶**；保存在權威 store |

建議 pipeline：

1. 在工作完成或重要決策後，產生候選 `{subject, scope, type, claim, evidence_refs, valid_from, expires_at}`。
2. 獨立 audit 檢查是否值得保留、是否包含敏感或行為變更、是否與已知資料矛盾。高風險候選不能只靠第二個模型放行。
3. 程式驗 evidence 確實存在、hash／範圍正確、來源身份與 scope 相符；audit 另驗引用是否真的支持 claim，不能只看「有引用」。
4. 只將通過的知識候選送入 memory pipeline；記錄 native operation receipt，不把 202 當成功。
5. 抽取完成後核對實際輸出的記憶。保留 FA 端已審核 claim ledger，Memory Store 作檢索投影；只把能映射至有效 claim 的結果送進 context。無法核對 lineage 的輸出不自動生效。

現有 `SyntheticMemory.chat()` 是伺服器產生的合成偏好 fixture，不能直接開放為任意外部 chat 的原始輸入。新 pipeline 是產品能力，不把 POC 路由放寬就當完成。

變更 SOP／skills／角色／批准規則者，轉入 FR-18 的人類批准與 Context release，而非 profile 更新。模型、其他 agent 或網頁不能藉「記住這條規則」改變行為。

撤銷／更正先在 claim ledger 生效並失效快取，檢索層即刻過濾舊資料；底層索引刪除需依已批准保留／刪除政策執行。無此批准先禁止使用，不擅自刪使用者資料。Memory Store 的 scope 不是 ACL，private receipts 與查詢快取也需同樣隔離。

## 10. 少確認的前提：工具不能走旁路

授權必須由控制層驗證，模型只提交意圖。`ToolBroker` 每次核對組織／Team grant、agent 身份、父工作委託、當前政策、具體 target／args 與有效批准。

| 操作 | 建議處理 |
|---|---|
| 已授權讀取、局部草稿、合理檔名、格式與工具順序 | 自主決定，可撤回；不為小事打斷使用者 |
| 缺少只有人類知道的資訊、真正改變目標的歧義 | 問一個能解除阻塞的問題，其他獨立工作可繼續 |
| 對外傳訊、支出、刪除、權限變更、排程建立／修改、發布 | 按使用者與組織政策取得精確批准；不能以「default act」推翻 |
| 未授權來源、跨 Team、超出批准範圍 | 拒絕並記錄；不改用另一工具繞過 |

批准綁定 approver identity、work／operation、目標、args digest、版本、有效期、次數與支出範圍。原始批准與 revoke 是權威事件；subagent 的「Lead 說可以」只是資料，除非附可驗證的委託鏈。有效批准已涵蓋的低風險步驟不反覆詢問；越界才重新問。

每次工具決策保存有效政策版本、所需 grant／批准 reference、操作 digest 與允許／拒絕原因；若另用模型分類器，分開記錄其有效 review mode 與分類結果。enabled 設定不是該次決策；模型審核的 off／shadow 模式也不能停用 FA 必須執行的人類批准與 scope gate。指示、操作、generation 或有效期不再符合時，舊批准失效。

同一 Hosted 中的 shell／browser 與 SDK tools 也必須受約束。只有 SDK permission callback、command string parsing 或 `ToolBroker` prompt，不足以阻止已有廣泛 credentials 的 shell／已登入 browser 執行外部操作。正式執行需最小 credentials、連線範圍、工具 sandbox 與不可繞過的執行政策；無法管住的操作不開放自動執行。

Browser profile 尤其不能當作全權委託。沿用 M1 的合成 profile 證據，不把它升格成真實 SaaS 免批准。Secrets 不放模型 context、event payload 或 Memory；政策 gate／身份服務不可用時，副作用 fail closed。

## 11. Silence、routine 與協作

每個處理結果需是 `reply`、`work_started`、`waiting` 或 `silence(reason)`；Silence 必須可稽核，但不向使用者刷一則「我選擇不說話」。

允許 silence：routine 無新事件、重複已處理事件、agent FYI 無需動作、group discussion 已無新增觀點。禁止 silence：使用者直接在等答案、需要批准、工作終止或結果不明而尚未告知。

內部訊息使用 `request`、`result`、`handoff`、`fyi`、`blocked` 與 `in_reply_to`。Transport receipt 不產生新的 agent 對話。每輪協作建議設定 hop／訊息／token／時間上限；只有新增證據、決策或阻塞才繼續。首個沒有新工作及新內容的回合即結束。

每個 routine 有 owner、批准版本、時區、去重 occurrence ID、下一次執行與通知政策。到點只建立一般工作事件，同樣排隊與經過工具批准；不能把 routine 身份當永久人類委託。排程變更需批准；無變化不通知，連續失敗仍需合併告警，不能以 silence 隱藏。

可評估官方 [Foundry routines](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/routines)（R1），但 recurring 最短五分鐘，預設下游每 attempt 30 秒、共三次 delivery attempts，不能當成秒級 delivery watchdog 或任意事件匯流排。長工作先回持久 receipt；routine 的 delivery retry 仍需經過相同 inbox 去重。

首版不先引入第二套多 agent 排程器。既有計畫的 Group Chat 方向若採用，也應經相同 mailbox／job／政策契約，不另開無上限的討論迴圈。

## 12. 資料與部署建議

### 12.1 先沿用哪些，哪些需另批准

| 資料 | 建議位置與限制 |
|---|---|
| 精確 work、inbox／outbox、receipts、批准 | 沿用 Table 技術；新版本 schema／row namespaces，不改舊 published work 的不可變契約 |
| 長事件內容、SDK checkpoint、成果檔 | 私有 Blob 的獨立容器／權限與固定版本 reference；**不是**直接擴用 M1 私人備份容器 |
| 私人環境 | Hosted 指定持久位置＋M1 備份契約；SDK 必要狀態另列保存清單 |
| 語意記憶 | Foundry Memory Store 作經審核知識的檢索投影；preview 風險與網路限制另行接受 |
| 工作喚醒 | 選定耐久運輸與 consumer 後實作；Service Bus 為候選，不是已存在的 FA 基礎設施 |
| UI 狀態與遙測 | 權威 projection／metrics；不是用 UI 的 running 字樣推定工作已開始 |

同 partition 的交易亦需符合 Azure Table 的操作數／大小限制；檔案先 create-only 保存、核對 hash，再交易提交 reference。失敗留下的未引用 blob 依批准保留政策處理，不用跨服務假交易或直接刪檔補償。

SDK session 檔與 event logs 設加密、讀者範圍、保存期及 data minimization。每個讀取都驗 Team／actor；不能僅因 RowKey、Memory scope 或 Blob 路徑難猜就當隔離。

### 12.2 實作落點

建議保留 `src/fa-shared-context-poc` 作回歸探針，新增正式 `src/fa-runtime-control` 與 `src/fa-hosted-agent`，採用現有權威狀態／記憶能力而非複製另一套。

先固定共享資料契約，再將真正需要共用的純型別／傳輸程式抽成 package；機械搬移與行為變更分開。新的批次交易／產品路由版本化，不改 `ContextService.publish` 的舊語義。正式 artifact 發布、使用者訊息交付與 Context release 三種「發布」各用自己的狀態與批准，不共用一個 `published` 欄位。

本輪不建立上述目錄或雲端資源。若後續新增 Azure IaC，先選適用 AVM／WAF 範例、固定版本並記錄不採用原因；評估 queue tier、常駐 consumer、儲存與模型費用後另取得批准。

## 13. 推進順序已由最新開發計畫承接

原提案的 M2-A／M2-B 不再作執行順序。保留已完成的 M2 文字／恢復與 M3 核心閉環，先按原批准完成本批 M4，再依[BR-01–BR-06](POC-ANALYSIS-AND-PLAN.md#m4-之後的行為開發切片)增量補可靠交付、背景與 ownership、Context／記憶治理及 M5 四 agent／Teams。M2 自主 UI 仍是獨立缺口，不以其他階段通過代替。

建議第一個 demonstration：交辦有明確輸入的研究／整理工作 → 保存接單 → 執行工具 → 中途補充限制 → 保存成果 → 人為中斷在「成果已保存、final 尚未送」 → 恢復後只補交一次 final。這比先展示五個 agent 同時聊天更能驗證核心差異。

## 14. 驗收：量體驗，也量真正完成

**B01–B12 已移入[規格第 11 節](FA-AGENT-BEHAVIOR-SPEC.md#11-新增行為驗收-b01b12)，只在該處維護。** 以下成本與服務目標保留為原提案的討論材料，不是新增的 M4 gate 或已批准 SLA。先以必要故障案例驗新增切片，不重跑已完成階段。

首輪建議服務目標：暖入口 `durable_ack` p95 ≤ 2 秒，已完成工作到 FA 工作台 final 可取得 p95 ≤ 5 秒；Hosted 冷啟動、模型完成時間與 Teams 外部送達分開計。這些數字是驗收提案，不代表立即模型回覆或平台承諾。

模型補交、Memory audit、compaction、背景 workers、重試及 idle retention 都納入成本：

`每件成功交付成本 = (模型 + 工具／檢索 + 記憶 + 執行／控制層 + 儲存／運輸 + 失敗重試分攤) / 成功交付數`

另記錄首次有效進展時間、端到端完成時間、無必要詢問率、未交付率、重複副作用數、記憶有來源命中率及通知數。不要用 ack 延遲或 cache 命中率取代端到端成果。

10 倍目標沿用既定 OpenClaw 公平比較：固定版本、模型／推理設定、工具、任務、初始資料、資源與品質，冷／暖／恢復分組，使用相同 p50／p95 統計量比較。工作集與樣本數先批准；品質不達門檻的快回覆不算改善。

每 job／Team 有 token、工具次數、並行量與期限控制；Memory audit 可批次、不因每句「謝謝」啟動。估計費用與 Azure 延遲帳務不是硬上限，超範圍停止並要求批准。

## 15. 需要批准的設計選項與本輪交付界線

架構方向已由 ADR-0004 承接；尚待選型的權威清單見[規格第 12 節](FA-AGENT-BEHAVIOR-SPEC.md#12-尚待選型不阻擋既定-m4)。下表保留原先取捨，不再要求回頭批准或重開 M2-A：

| 選項 | 建議起點 | 不先承諾的部分 |
|---|---|---|
| 執行容量 | 保守的一 agent 一 executor、全域四槽；可選一槽互動＋三槽背景；未知活躍者不釋放 | 同 agent 多路模型／shell 同跑、預留第五個互動槽 |
| 交付通道 | 先做薄驗收入口與持久成果，再接 Teams | 外部通道 exactly-once 或人類已閱讀 |
| Durable wake 宿主 | 先驗 queue／worker 契約，再選具體 Azure 配置 | 免費常駐、scale-to-zero 自動喚醒、沿用舊 POC 額度 |
| User Model | 明確同意、同 Team 的低敏感偏好先行 | 自動跨 Team 共享、推測性敏感 profile |
| Preview／provider | 固定 SDK、CLI、model、Memory 與協定版本 | 各 provider 支援相同 cache、compaction 或 background 行為 |
| 保留與刪除 | 訂定事件／checkpoint／memory／artifact 各自政策 | 永久保存全部聊天，或未批准自動刪除資料 |

此設計工作只整理文件，未另外部署、呼叫模型或變更排程；M2／M3 的實作及發布另以 `f097fa6` 報告為準。規格化不替代 M0 成本收尾，也不把既有 M4 授權延伸到本文的新能力。
