# Hosted Agents 冷啟動延遲官方研究

調查日期：2026-10-05。範圍：Microsoft Foundry **Hosted Agents**，FA 目前採用 `invocations` protocol。本文只引用 Microsoft Learn、官方 REST/SDK 文件與 `Azure/azure-sdk-for-python` 官方原始碼；沒有使用部落格或第三方文章。

## 一句話結論

官方文件明確說：Hosted agent 是「每個 session 一個 VM-isolated sandbox」，session 第一次被使用或 idle 後再次被引用時，平台會 on demand provision compute、恢復 `$HOME` 和 `/files`，再把請求送進容器的 `/invocations` 或 `/responses` handler。**但官方沒有公布 cold start 各段秒數、最大等待時間、SLA、readiness probe 間隔，或 request timeout。** 官方唯一給的接近時間數字，是「建立 agent version 到 `active` 通常少於 1 分鐘，視 image size 而定」；那是部署／版本 provisioning，不是每次 session cold start。

## 證據等級

- **官方文件明寫**：Microsoft Learn 直接描述 Hosted Agents、session、lifecycle、limits、endpoint、idle timeout。
- **官方 SDK／範例原始碼可推得**：`azure-ai-projects==2.3.0`、`azure-ai-agentserver-core==2.2.0`、`azure-ai-agentserver-invocations==1.2.0`、`azure-ai-agentserver-responses==2.2.0` 的 enum、host server、poll 範例、CHANGELOG。
- **FA 實測**：本文件先留空，等總協調補數字；不得拿 FA 實測當官方數字。

## 名詞先對齊

- **Agent version**：agent 設定的不可變 snapshot，包含 container image、CPU/memory、environment variables、protocol configuration。每次 create version 都是新版本；endpoint 一次只服務一個版本，沒有 traffic splitting。[S1][S6]
- **Agent endpoint / selector**：agent 有穩定 endpoint。`version_selector` 決定 endpoint 路由到 latest 或指定版本，100% traffic 到該版本。[S6]
- **Session**：一個 `agent_session_id` 對應一個 logical workload 的 stateful isolated sandbox；保存 `$HOME` 和 `/files`，compute 可 scale-to-zero 後再恢復。[S1][S2]
- **Conversation**：Responses protocol 的對話歷史；不是 sandbox。Responses 用 `previous_response_id` 或 `conversation` 串歷史；Invocations 不管歷史，FA 要自己管。[S2]
- **Compute / sandbox**：官方用語是 per-session VM-isolated sandbox。每個 session 需求時建立一個 sandbox；idle timeout 到了 deprovision compute 並保存 state。[S1]
- **Files API**：session-scoped 檔案 API，檔案與 container 寫到 `$HOME` 的內容共享同一份 session storage。官方沒有說 Files API 本身是否會啟動 compute。[S2]

## 官方 session lifecycle

| 狀態／動作 | 官方說法 | 官方數字 | 來源 |
|---|---|---:|---|
| first use / create session | session 可在第一次 invoke 自動建立；也可先 `POST .../endpoint/sessions` 預先建立，常見用途是先上傳檔案、預先拿 session ID、或用 `version_indicator` 綁定版本。 | 官方文件沒有給 session 建立耗時 | [S2] |
| `creating` | SDK enum：`Session initialization is in progress.` | 官方文件沒有給數字 | [K1] |
| `active` | SDK enum：`Session is running and operational.` 文件狀態表：Active 時 compute running、requests routed to it、`$HOME` 和 `/files` 可用。 | 官方文件沒有給數字 | [S1][K1] |
| `idle` | 沒有 request 達 idle timeout 後，平台 deprovision compute 並保存 session state；同 session 再被引用時會 provision compute 並恢復 state。SDK enum 補一句：idle sandbox resumes on next invocation。 | idle timeout 可設 120–3600 秒；預設 900 秒 / 15 分鐘 | [S1][S2][K1] |
| `resumed` | 同一 session ID 再被引用，平台 provision 新 compute 並恢復 `$HOME` / `/files`。 | 官方文件沒有給 resume 耗時 | [S1][S2] |
| `stop` | `:stop` terminates running compute，但保留 persistent filesystem volume；已 stopped 再 stop 也成功。停止不是 cancel turn。 | 官方文件沒有給 stop/resume 耗時 | [S2] |
| `expired` | SDK enum：30 天 inactivity 後 session TTL exceeded；文件說 session 30 天未活動會永久刪除。 | 30 天 inactivity | [S1][S2][K1] |
| `failed` | SDK enum：session initialization 或 lifecycle handling failed，需要 delete recover。 | 官方文件沒有給數字 | [K1] |

### `create_session` 和第一個 invocation 是否都 provision compute？

官方文件沒有用同一句話回答「兩者是否都 provision compute」。可支持的結論是：

- 沒有 `agent_session_id` 的第一個 invoke 會建立新 session；Invocations 的 session ID 放在 query string `?agent_session_id=<id>`，沒有放就由平台建立並在 sample 的 terminal `done` event 回傳 `session_id`。[S2]
- explicit `create_session` 的官方用途包含「Preallocate a session」和「Pin the session to a specific agent version with `version_indicator`」。C# 範例在 create 後每 1 秒 poll，直到 `Active` 或 `Failed`。[S2]
- 因此，若 FA 先 `create_session` 並等到 `active`，後續第一個 invoke 應是 route 到同一個 session；官方沒有說這能完全消除第一次 handler 前的 latency，也沒有說 create_session 和 first invocation 會重複啟兩次 VM。[S2]

### stopped / idle / Files access 的恢復界線

- **idle**：文件寫「when the session is referenced again」會 provision compute 並 restore saved state；SDK enum 比較窄，寫「resumes on next invocation」。這支持「invoke 一定是恢復點」，但 Files API 是否算「referenced again」並啟 compute，官方文件沒有講清楚。[S1][S2][K1]
- **stopped**：文件說 stop 後 session retained and you can resume it later；但對 in-flight work，又警告 stopped session 的 work 不保證自己重啟，需看 hosting platform 如何 explicit resume。官方沒有列出獨立 `resume` API，也沒有說 Files access 會 resume stopped compute。[S2]

## 冷啟動步驟時間表（client request 到 handler 收到訊息）

> 這張表只放官方來源可支持的步驟。FA 自己在 handler 裡啟動 Copilot SDK/runtime 的時間，放到下一節，不算 Microsoft 平台 cold start。

| 步驟 | 誰負責 | 官方說法 | 官方數字 | 來源 |
|---|---|---|---|---|
| 0. 前置：agent version 已部署且 `active` | Microsoft + deployer | 部署流程是 build/push image → create agent version → poll 到 `active` → invoke。建立 version 時平台 pulls image、建立 dedicated Entra agent identity、建立 endpoint。 | version provisioning 通常少於 1 分鐘，視 image size 而定。這不是 session cold start。 | [S1][S3] |
| 1. FA portal / client 發 `POST` 到 dedicated agent endpoint | FA client + Microsoft gateway | Hosted agent 有 dedicated endpoint。Invocations endpoint 是 `{project_endpoint}/agents/{name}/endpoint/protocols/invocations?api-version=v1`；caller 用 Microsoft Entra/RBAC。 | 官方文件沒有給 gateway/auth/routing 耗時 | [S1][S2][S6] |
| 2. endpoint 用 selector 或 session binding 找版本 | Microsoft gateway | endpoint 的 `version_selector` 可 latest 或 pinned；explicit session 建立時可用 `version_indicator` 綁 concrete version，之後該 session 綁單一版本。 | 官方文件沒有給 routing 耗時 | [S2][S6] |
| 3. 建立或查找 `agent_session_id` | Microsoft session plane | 沒帶 `agent_session_id` 的第一個 Invocations call 會建立 session；帶 query string 則重用該 sandbox。Invocations 只看 query string，body/header 的同名欄位不影響 routing。 | 官方文件沒有給 session lookup/create 耗時；C# explicit create sample 每 1 秒 poll 狀態，是範例 polling，不是 SLA | [S2] |
| 4. provision per-session VM-isolated sandbox | Microsoft platform | Hosted agents run in per-session VM-isolated sandboxes；平台 on demand 建 compute，session idle 後 deprovision，resume 時重建 compute。 | 官方文件沒有給 VM/container cold-start 秒數、最大等待、SLA | [S1][S2] |
| 5. image/container 準備 | Microsoft platform | 官方明寫 deploy/create version 時會 pull image/build container snapshot；session cold start 是否再次從 ACR pull image，文件沒有說。image size 只被明確連到 version provisioning 時間。 | session cold start：官方文件沒有給數字；version provisioning：通常 < 1 分鐘，依 image size | [S1][S3] |
| 6. 恢復 persistent filesystem | Microsoft platform | `$HOME` 和 `/files` persisted across turns and idle periods；resume 時 restore。session storage at 1 vCPU+ up to 20 GiB，約 20% reserved；其餘由 image、`$HOME`、writable locations 共用。 | 官方文件沒有給 restore 耗時 | [S1][S2] |
| 7. 注入環境與 connections | Microsoft platform | startup 注入 `FOUNDRY_PROJECT_ENDPOINT`、`FOUNDRY_AGENT_NAME`、`FOUNDRY_AGENT_VERSION`、`FOUNDRY_AGENT_SESSION_ID`、`APPLICATIONINSIGHTS_CONNECTION_STRING` 等；connection placeholder 在 sandbox start 前解析，missing 則 env var 空值。 | 官方文件沒有給 env/secret injection 耗時 | [S3][S4] |
| 8. container process 啟動，agentserver 開 HTTP server | FA container + SDK | runtime contract：listen port 8088、serve `GET /readiness`、serve `POST /invocations` 或 `POST /responses`、consume env vars、SIGTERM graceful shutdown。SDK `AgentServerHost.run()` 用 Hypercorn，default bind `0.0.0.0`，port 來自 `PORT` 或 8088。 | 官方 SDK default graceful shutdown timeout 30 秒；startup 到可接 request 的秒數沒有數字 | [S4][K2][K3] |
| 9. readiness check | Microsoft platform + SDK | 平台送 `GET /readiness` 判斷 container 是否 ready；SDK adapter 自動註冊 endpoint，回 `200 OK` / `{"status":"healthy"}`。 | 官方文件沒有給 probe interval、timeout、失敗重試次數 | [S3][S4][K3] |
| 10. gateway forward request to `/invocations` | Microsoft gateway + SDK | Invocations protocol 是 pass-through：你定義 JSON payload，平台 route 到 session 並 forward body；SDK route 是 `POST /invocations`。 | 官方文件沒有給 forward 耗時 | [S2][S4][K4] |
| 11. SDK 解析 IDs/context 後呼叫 FA handler | SDK + FA code | `azure-ai-agentserver-invocations` 1.2.0 會從 query `agent_session_id`、`FOUNDRY_AGENT_SESSION_ID` 或 fallback UUID 解析 session，設定 `request.state.session_id`、`FoundryAgentRequestContext`，再 dispatch `@app.invoke_handler`。這一刻 FA handler 收到 request。 | 官方 SDK 沒有給 handler dispatch 耗時 | [K4][K5] |

## 暖啟動 vs 冷啟動

官方文件沒有把 `warm start` / `cold start` 當正式狀態名；正式狀態是 `active`、`idle`、`resumed` 等。本文用官方 lifecycle 做操作性定義：[S1][S2]

- **暖啟動**：同一個 session 的 compute 仍在 **Active**，也就是還在 idle timeout 內再次被呼叫。官方說 Active 時 compute running、requests routed to it；每個 request 會 reset idle timer，平台 keeps compute active while requests continue。[S1]
- **冷啟動**：沒有可用 running compute，需要平台 provision sandbox。包含新 session 第一次使用、session 已進入 **Idle** 後再次被引用、或 session 被 `:stop` 終止 compute 後再使用。[S1][S2]
- **idle 被喚醒算什麼**：官方稱 **Resumed**，不是 warm。因為 idle 已 deprovision compute；再次 reference session 時，平台 provisions new compute 並 restores persisted state。[S1][S2]
- **stopped 被喚醒算什麼**：不是 warm。`stop` 會 terminate running compute、保留 filesystem volume；官方沒有保證 stopped session 的 in-flight work 會自動重啟，也沒有列獨立 resume API。[S2]

時間證據仍分三層：官方文件明寫的數字只有 idle timeout、TTL、version provisioning 通常 <1 分鐘等；SDK 原始碼只補出 server default port、graceful shutdown timeout、handler dispatch 路徑；FA 實測等總協調補到「FA 實測對照」。

| 步驟 | 冷啟動 / idle resume / stopped resume | 暖啟動（Active 內下一回合） | 每回合都會做或 FA-side | 官方數字 |
|---|---|---|---|---|
| 1. client 呼叫 dedicated endpoint | 需要 | 需要 | FA portal/client 每回合都發 request | 官方文件沒有給數字 |
| 2. auth / endpoint routing / selector | 需要；若未帶 existing session，endpoint 用 `version_selector` 找版本 | 需要；既有 session 已綁版本，但 gateway 仍要 route session | 平台-owned | 官方文件沒有給數字 |
| 3. session create / lookup | 新 session 會 create；idle/stopped 用既有 `agent_session_id` lookup | 用既有 `agent_session_id` lookup | Invocations 只看 query string `agent_session_id` | 官方文件沒有給數字 |
| 4. provision per-session VM-isolated sandbox | 需要；idle resume 明寫 provision new compute；stopped 因 compute 已 terminated，也需重新有 compute 才能服務 | 不需要；compute already running | 平台-owned | 官方文件沒有給數字 |
| 5. image/container 準備 | version 建立時已 pull/build snapshot；session resume 是否再從 ACR 拉 image，官方文件沒有說 | 不需要重新啟動容器 | 平台-owned | session 層沒有數字；version provisioning 通常 <1 分鐘、依 image size |
| 6. restore `$HOME` / `/files` | idle/resume 需要 restore persisted state；新 session 沒舊 state | 不需要 restore；state 已掛在 running sandbox | 大量檔案是否拖慢，官方沒有數字 | 官方文件沒有給數字 |
| 7. env vars / connections injection | sandbox start 時做；connection placeholder 在 sandbox start 前解析 | 不應每回合重新注入；仍可在 process 內讀 env | 平台-owned | 官方文件沒有給數字 |
| 8. container process / agentserver startup | 需要啟動 process，SDK 開 Hypercorn、註冊 `/readiness`、protocol routes | 不需要重新啟動 process | FA container + SDK | SDK graceful shutdown default 30 秒；startup 無數字 |
| 9. readiness check | 需要等 container ready 才 forward request | 不需要重新 readiness gate（compute 已 active） | 平台 + SDK | 官方文件沒有 probe interval/timeout |
| 10. forward to `/invocations` | 需要 | 需要 | SDK route 每回合接 request | 官方文件沒有給數字 |
| 11. SDK 解析 session/context 並 dispatch handler | 需要 | 需要 | `azure-ai-agentserver-invocations` 每回合都解析 IDs、設定 request context、呼叫 handler | 官方 SDK 沒有給數字 |
| 12. FA handler 內 Copilot SDK/runtime | 冷啟動時可能第一次啟動；若 FA 每回合都重啟 process，就每回合都有 | 暖啟動也可能有，取決於 FA 是否保留 runtime process | FA-side，不是 Microsoft 平台 | 等 FA 實測；官方文件不會給 FA 數字 |

### 換版對既有 session 的影響

- **Agent version 是不可變 snapshot**：container image、resource allocation、environment variables、protocol configuration 都在版本裡；更新 agent 要 create/deploy 新 version。[S1][S6]
- **Stable endpoint 的 selector 只影響新路由決策**：預設 latest，或 pin 到指定 version；traffic splitting 不支援，100% traffic 到一個版本。[S1][S6]
- **Session 建立時綁單一版本**：explicit `create_session` 可用 `version_indicator` 綁 concrete version；官方寫「Each session is bound to a single version at creation time」，之後 later turns keep using that version even if you publish a newer one。[S2]
- **對 FA 發版的含意**：既有 active/idle session 理論上繼續綁原 version；新的 session 或沒有固定版本的入口，會依 endpoint `version_selector` 走新版本。若舊 session idle 後 resume，官方說會 provision compute 並 restore state，但沒有說換版後舊 session 是否重用舊 image snapshot 的耗時，也沒有給數字。[S1][S2][S6]

## FA-side：handler 收到後才開始的事

以下不是 Microsoft Hosted Agents 平台冷啟動；是 FA 自己要量的段落。

- FA 目前用 Invocations protocol，呼叫形狀是 `POST {project}/agents/{agent}/endpoint/protocols/invocations?api-version=v1&agent_session_id=...`，並使用 explicit `create_session`、`:stop`、Files API、`version_indicator` / selector routing、`hosted_idle_timeout_seconds=900`。這些是本次 Neal 任務提供的 FA 現況，不是 Microsoft 官方聲明。
- handler 之後，FA 可能還要啟動 Copilot SDK/runtime process、讀 FA checkpoint、還原 workspace、載入 tools、建立本地 bridge。這些都應在 FA trace 裡另切 span，不要混到「Microsoft VM 啟動」時間。

## 限制、timeout、quota、官方建議

| 類別 | 官方明寫 | 對 cold start 的意義 | 來源 |
|---|---|---|---|
| idle timeout | `idle_timeout_seconds` 可設 120–3600 秒；省略時 server default 900 秒。 | FA 當時設 900 秒（stage 2026-10-07 改 3600），代表閒置逾時內連續請求通常維持 active；拉長到最多 60 分鐘可少冷啟動，但增加 active session 成本。 | [S1][S2] |
| session TTL | 30 天 inactivity 後永久刪除。 | idle resume 只在 session 還沒過期時成立。 | [S1][S2][K1] |
| CPU/memory | sandbox 支援 0.5 vCPU/1 GiB、1 vCPU/2 GiB、2 vCPU/4 GiB。 | 資源越大不等於官方承諾 cold start 更快；官方建議用 App Insights 看 CPU/memory/request duration 後 right-size。 | [S1] |
| session storage | 1 vCPU+ up to 20 GiB，約 20% 系統保留；image、`$HOME`、writable locations 共用。 | 大 image 會吃同一份 budget；官方沒有說它如何影響 session cold start，只說 version provisioning 受 image size 影響。 | [S1][S3] |
| file upload | Session Files API 單檔上傳最大 50 MB。 | 可先 create session 上傳檔案，但官方沒有說上傳會 warm compute。 | [S2] |
| request timeout | 官方 Hosted Agents 文件沒有公布 foreground invoke request timeout。 | FA 不能把任何固定秒數當平台保證。 | [S1][S2][S4] |
| max cold-start wait / SLA | 官方文件沒有給 VM/container cold start 上限、percentile 或 SLA。 | 只能靠 FA 實測分段觀察。 | [S1][S2][S3][S4] |
| readiness probe | container 必須 `GET /readiness` 回 200；SDK 自動提供。 | 可以量 container log 到 readiness 的時間，但官方沒有 probe 間隔/timeout。 | [S3][S4][K3] |
| graceful shutdown | SDK default graceful shutdown timeout 30 秒；SIGTERM 時停止接新 request、完成 in-flight、flush `$HOME` writes。 | 這是停機/scale-in 的 drain，不是冷啟動等待時間。 | [S4][K2][K3] |
| resilient task preview | Long-running resilience 是 preview；recovery 會從 handler 開頭 reenter，不是 deterministic replay。core 2.0.0b11 起 per-turn task timeout default 1 day、可到 7 days。 | 只影響長工作恢復，不提供 cold start 秒數。 | [S5][K2] |
| concurrent sessions | Hosted agents scale per session；每個 session 一個 sandbox；沒有 replica count 或 warm pool 可設。官方沒有列 concurrent session quota。 | FA 能控制「同 Agent 排隊」產品策略，但平台計費/scale 是 active sessions 累計。 | [S1] |
| regions | Hosted agents region list 包含 **West US 3**。 | FA westus3 在官方 supported list 內。 | [S1] |
| Container Apps / min replicas | Foundry Hosted Agents 官方文件沒有說它跑在 Azure Container Apps，也明寫「no replica count to configure and no warm pool to size」。 | 不引用 ACA cold-start/min-replica 文件；不能假設可設 min replicas。 | [S1] |

### 官方可用的「減少冷啟動」槓桿

- **調整 idle timeout**：官方支援 2–60 分鐘。這是唯一明確能減少 idle 後冷啟動機率的 Hosted Agents 設定，但會讓 session compute 維持 active 較久、增加成本。[S1][S2]
- **先 create session**：官方說可 preallocate session、先上傳檔案、或 pin version；C# sample 可 poll 到 `Active`。這可把 session initialization 移到使用者真正送訊息前，但官方沒有承諾消除全部 cold-start latency。[S2]
- **image size 控制**：官方只明確說 version provisioning 通常 <1 分鐘且依 image size；沒有說 session cold start 的 image pull 時間。仍應避免超大 image，因 image 也吃 session disk budget。[S1][S3]
- **沒有官方 warm pool / min replicas**：文件明寫 no replica count / no warm pool to size。若 FA 想 keep-warm，只能在產品層用合法 request/session 策略評估成本與行為，不能套用 ACA min replicas。[S1]

## FA 實測對照（總協調補充）

以下都是 **FA 實測或總協調判讀**，不是 Microsoft 官方數字，也不是 SDK 契約。用途是對照官方 lifecycle，不能混進前面的「官方文件明寫」或「SDK 原始碼可推得」。

### A. stage 平台 `invoke_agent` span（2026-10-02～10-05）

來源：Log Analytics workspace `log-fa-stage`，`AppRequests` 表，`AppRoleName="agentsv2"`、`Name="invoke_agent"`，共 9183 筆。這是 Foundry 平台自己送的 span，涵蓋一次 invocation 從進平台到回應；限制是它沒有拆出 `create_session`、image pull、container start、`/readiness`，也不等於使用者完整回合時間。

| 分組（依同一 `azure.ai.agentserver.session_id` 距上次呼叫間隔） | 筆數 | p50 | p90 | p99 | 最大 | 判讀限制 |
|---|---:|---:|---:|---:|---:|---|
| 同一個 session 的第一次呼叫 | 32 | 0.51 秒 | 0.79 秒 | 1.3 秒 | 1.3 秒 | stage API 沒記 `create_session` 耗時；推測 compute 可能已在 FA 先 `create_session` 時開好，未核對。 |
| 距上次超過 900 秒（閒置後） | 38 | 0.25 秒 | 6.9 秒 | 133 秒 | 133 秒 | 混到不同版本與 idle timeout；133 秒單點原因未查。 |
| 距上次 60～900 秒 | 97 | 0.43 秒 | 1.0 秒 | — | 487 秒 | 可能包含長工作或異常回合；不是純 warm path。 |
| 距上次不到 60 秒（暖） | 9016 | 0.24 秒 | 0.76 秒 | 8.0 秒 | 1800 秒 | 最大 1800 秒是即時桌面 `invocations_ws` 長連線，不是一般回合。 |

細節判讀：閒置超過 900 秒後的第一次呼叫，版本 64、65 多半落在 4～8 秒（8.4、7.5、6.9、6.4、6.4、6.2、4.3 秒）；有一次 133 秒（2026-10-05 01:49，間隔 19.5 分鐘，原因未查）。版本 57、58、63 間隔 15～96 分鐘仍只要 0.5～0.8 秒。總協調推測各版本 idle timeout 不同：當時 `stage.json` 的 `hosted_idle_timeout_seconds=900`，但 ADR-0006 記錄 Neal 2026-10-01 曾決定 3600（2026-10-07 已改成 3600，見 [progress](progress.md#dev-rtt-ab-1007)，下次 stage release 生效）。**推論：compute 被回收後，第一次 invoke 的冷恢復約多 4～8 秒。這是 FA 實測推論，不是官方數字。**

### B. FA 回合內部分段（stage A 暖回合，2026-10-04 21:53）

來源：Hosted stdout `FA_TIMING`，`azd ai agent monitor fa-stage-a --session-id fa-native-615cfb1c…`。限制：單一暖回合、單一 agent，不能代表 cold start，也不能代表全部模型或工具情境。

| 分段 | 秒數 | 判讀 |
|---|---:|---|
| `team_memory_recall` | 0.72 | FA-side，每回合可能發生。 |
| `private_memory_recall` | 0.91 | FA-side，每回合可能發生。 |
| `sdk_process_start` | 0.013 | 該暖回合幾乎沒有 SDK process start 成本。 |
| `sdk_process_stop` | 2.3 | 回合結束後成本，不在使用者看到首包前。 |
| `private_memory_update` | 0.73 | 回合結束後成本。 |

### C. 舊架構歷史對照（已被 ADR-0006 / ADR-0007 取代）

來源：`docs/adr/0005-long-running-conversation-worker.md`「背景」；live job `4db84cb8`，2026-09-30 10:03 UTC，main `57d269e`，Hosted 56，新對話。限制：當時是「每個對話一個 Hosted session」的舊架構，已被 ADR-0006（每個 Agent 一台 VM）和 ADR-0007（native）取代，只能當歷史對照。

- 第一個處理步驟出現在第 15.6 秒；第一個工具在第 28.8 秒。
- 分段：接單 3.4 秒、`native_prepare` 0.14 秒、`hosted_create`（只在第 1 回合；容器冷啟動）3.2 秒、`hosted_invocation`（含兩次 claim，未拆開）4.9 秒、acquire＋SDK 啟動前 catalog／授權約 6 秒、SDK 啟動＋session＋MCP 小於 1 秒。
- 第 2 回合起沒有 `hosted_create`，但仍有 Foundry invocation、兩次 claim、archive restore、SDK/MCP 重啟、SDK stop 和 archive write 成本。

### D. 換版後接續（近似強制冷啟動）

來源：總協調提供的 stage/dev 發版驗收紀錄與 dev 退版演練報告（`fdbbfbeb…/files/i-p1/d62/v14-dev-test-report.md`）。限制：這些數字包含 FA 自己的接續掃描、工作台狀態、歷史檢查和部署流程，不是純 Microsoft sandbox cold start。

- stage 第 14 版（2026-10-05）：CI 部署約 10 分鐘；部署後打開 A，A 約 2.5 分鐘接上新版。
- B 因工作台在空白對話有程式錯誤，卡住 20 分鐘以上；切到有內容的對話後 55 秒接上。
- dev 退版演練：A、B 自動接續各約 4～6 分鐘。接續時間大多花在「不搬檔接續前，逐頁掃描歷史確認沒有工作檔」（`files_check=scan_pending`）。
- 伺服器端自動接續 PR #652 尚在開發中；上面數字不應外推成新版接續的長期目標。

### E. 使用者感受到的暖回合時間

來源：總協調人工觀察工作台顯示。限制：這是端到端體感，包含模型、工具、UI 與 FA-side 工作；不是平台 span。

- `gpt-6.1-sol` 簡單暖回合：工作台顯示約 2～4 秒。
- `run_command` 加表格的回合：約 11 秒。

### 下一步要量的項目

目前沒量到：stage 的 `create_session` 耗時、image pull 時間、container process start 到 `/readiness` 的時間、平台 forward 到 `@app.invoke_handler` 的單獨耗時。下一輪應分開記錄：FA portal request start → Foundry first byte、`create_session` start → `Active`、invoke request start → container first log / handler entry、handler entry → Copilot runtime ready、warm active 下一回合、idle resume、stopped resume、fresh session、換版後舊 session resume。

## 對 FA 的意義

- **平台負責、FA 只能量不能控制**：gateway auth/routing、session lookup、VM-isolated sandbox provisioning、filesystem restore、env/connection injection、readiness probe cadence。
- **FA 可控制**：是否先 `create_session`、是否把檔案上傳前移、idle timeout（2–60 分鐘內）、image 大小與啟動腳本、handler 內 Copilot runtime 啟動方式、FA trace 分段。
- **不要做大機制**：官方沒有 warm pool/min replica 設定；依 FA 原則，先用 Hosted-first / SDK-first 和最小策略：拉清楚 trace、確認 cold/idle/stopped 三種情境、必要時只調 idle timeout 或 pre-create session。
- **下一步最重要**：把「平台到 handler」和「handler 後 FA runtime」分開量。Neal 問的是 client → Microsoft VM → session → agent receive message；所以最關鍵是能在 container startup、`/readiness`、`@app.invoke_handler` 入口打去敏 timestamp。

## 官方文件沒有給數字的步驟

- client → Foundry endpoint 的 auth/APIM/gateway/routing。
- endpoint selector / `version_indicator` routing。
- session lookup / auto-create / explicit `create_session` 到 active 的平台耗時。
- per-session VM/container/sandbox provisioning 的 cold-start 秒數、上限、percentile 或 SLA。
- idle resume / stopped resume 的耗時。
- `$HOME` / `/files` restore 耗時。
- environment variables / connection placeholder injection 耗時。
- container process start 到 `/readiness` ready 的平台 timeout、probe interval、重試策略。
- request forward 到 `/invocations` handler 的耗時。
- foreground invoke request timeout。
- Files API 是否會啟動 idle/stopped compute。

## 官方來源清單

- [S1] Microsoft Learn, **Hosted agents in Foundry Agent Service**，sections: How it works; Key concepts > Sessions; Session compute lifecycle; Platform details; Sandbox sizes; Session storage; Scaling and right-sizing; Limits, pricing, and availability。URL: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents 。Learn git commit: `d14789af400edaeb68bbb8607db16c73738d850e`。
- [S2] Microsoft Learn, **Manage hosted agent sessions**，sections: Sessions versus conversations; Manage session idleness; Invoke an agent and let the platform create the session; Create a session explicitly; Stop a session; Session file operations。URL: https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions 。Learn git commit: `df285a821771fdf003146ef635daba25dc6924f8`。
- [S3] Microsoft Learn, **Deploy a hosted agent**，sections: Deployment lifecycle; Container requirements; Platform-injected environment variables; Reference project connections; Poll for version status。URL: https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent 。Learn git commit: `45491a5e4027a948ea0c10471face36331215fdf`。
- [S4] Microsoft Learn, **Hosted agent runtime contract**，sections: Contract requirements; SDK adapter packages; Health probe; Network and transport; Graceful shutdown; Platform environment variables; Platform request headers。URL: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract 。Learn git commit: `69a226754528e8d4d032ec652eba7d82e584f648`。
- [S5] Microsoft Learn, **Resilience for long-running Microsoft Foundry hosted agents (preview)**，sections: Background execution and resilience; Resilient work model; Handle cancellation and shutdown。URL: https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience 。Learn git commit: `dfed7238aabf96ccce6940f64d0b3ef6cd6f8235`。
- [S6] Microsoft Learn, **Configure and share your Microsoft Foundry agent**，sections: Understand the agent object model; Traffic routing; Protocols; Authorization schemes。URL: https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent 。Learn git commit: `b3d4e01e629f99924e98bdbfa805412886d231ad`。
- [S7] Microsoft Learn, **Migrate hosted agents to the latest version**，section: What changed。URL: https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview 。Learn git commit: `8a8a94badd890e475a9b0b8eb334676dd2873eea`。
- [K1] `Azure/azure-sdk-for-python` tag `azure-ai-projects_2.3.0` commit `4378d83c503e963aa06ff6ea214df7a67939c4fb`，file `sdk/ai/azure-ai-projects/azure/ai/projects/models/_enums.py`，`AgentSessionStatus` / `AgentVersionStatus` enums。URL: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-projects_2.3.0/sdk/ai/azure-ai-projects/azure/ai/projects/models/_enums.py
- [K2] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-core_2.2.0` commit `18cfd6846557a64cfb1b3cc6d79c136f87960566`，file `sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md`，2.0.0b10–2.2.0 notes。URL: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md
- [K3] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-core_2.2.0` commit `18cfd6846557a64cfb1b3cc6d79c136f87960566`，file `sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_base.py` and `_config.py`，`AgentServerHost.run()`、`/readiness`、default port、graceful shutdown。URLs: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_base.py and https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_config.py
- [K4] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-invocations_1.2.0` commit `913745781a953c351d2f1e07e3ee57346a06d98d`，file `sdk/agentserver/azure-ai-agentserver-invocations/azure/ai/agentserver/invocations/_invocation.py`，routes、`agent_session_id` resolution、handler dispatch。URL: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-invocations_1.2.0/sdk/agentserver/azure-ai-agentserver-invocations/azure/ai/agentserver/invocations/_invocation.py
- [K5] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-core_2.2.0` commit `18cfd6846557a64cfb1b3cc6d79c136f87960566`，files `_platform_headers.py` and `_request_context.py`，`x-agent-user-id`、`x-agent-foundry-call-id`、`x-agent-session-id`、`get_request_context()`。URLs: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_platform_headers.py and https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_request_context.py
- [K6] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-invocations_1.2.0` commit `913745781a953c351d2f1e07e3ee57346a06d98d`，file `sdk/agentserver/azure-ai-agentserver-invocations/CHANGELOG.md`，1.2.0 and startup-latency related 1.0.0b8 note。URL: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-invocations_1.2.0/sdk/agentserver/azure-ai-agentserver-invocations/CHANGELOG.md
- [K7] `Azure/azure-sdk-for-python` tag `azure-ai-agentserver-responses_2.2.0` commit `9d59217589dc37bc3b6517fdfdba334590258202`，file `sdk/agentserver/azure-ai-agentserver-responses/CHANGELOG.md`，2.2.0 and history/resilient/background notes。URL: https://raw.githubusercontent.com/Azure/azure-sdk-for-python/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/CHANGELOG.md
