# N6 遷移官方事實查核報告(Invocations → Responses)

調查日期:2026-10-02。來源僅限 Microsoft Learn(learn.microsoft.com)、`Azure/azure-sdk-for-python` 的 release tag、PyPI metadata、`microsoft-foundry/foundry-samples`、`github/copilot-sdk`。所有引用皆為逐字引用 + URL;SDK 原始碼附 tag + 路徑 + 行號。凡來源沒有明確說明之處,一律標註「**unknown / not documented**」,不做推測。

> 備註:`gh api` / GitHub MCP 對 `Azure`、`microsoft-foundry` org 有 SAML SSO 限制(`403 Resource protected by organization SAML enforcement`),故原始碼與 samples 改以 `raw.githubusercontent.com` 及未授權的 `api.github.com` contents 端點讀取(皆為公開只讀內容)。

---

## Q1. Versions & compatibility(版本與相依性)

### 1.1 最新已發布版本(2026-10-02 查核)

| Package | 最新版本 | 狀態(PyPI classifier) | requires_python |
|---|---|---|---|
| `azure-ai-agentserver-responses` | **2.2.0**(2026-09-28 GitHub CHANGELOG / 2026-09-29 PyPI upload) | `Development Status :: 5 - Production/Stable` | `>=3.10` |
| `azure-ai-agentserver-core` | **2.2.0**(2026-09-23) | `Development Status :: 5 - Production/Stable` | `>=3.10` |
| `azure-ai-agentserver-invocations` | **1.2.0**(2026-09-29) | `Development Status :: 5 - Production/Stable` | `>=3.10` |

來源:`https://pypi.org/pypi/azure-ai-agentserver-responses/json`、`.../azure-ai-agentserver-core/json`、`.../azure-ai-agentserver-invocations/json`(PyPI JSON API,`info.version` / `info.requires_dist` / `info.classifiers` 欄位,2026-10-02 擷取)。

**風險提示(非建議,僅陳述事實)**:這三個版本距今查核日僅 **3–9 天**,是幾乎剛發布的 GA。`responses` 2.2.0 的上一個穩定版是 2.1.0(2026-08-24),中間經過 2.2.0b1(2026-08-27)、2.2.0b2(2026-09-24)兩個 beta。

### 1.2 精確相依性 pin(`requires_dist`,PyPI JSON API)

```
azure-ai-agentserver-responses   2.2.0  requires: azure-ai-agentserver-core<2.3.0,>=2.2.0
azure-ai-agentserver-invocations 1.2.0  requires: azure-ai-agentserver-core<3.0.0,>=2.1.0
azure-ai-agentserver-invocations 1.1.0  requires: azure-ai-agentserver-core<2.2.0,>=2.1.0   ← FA 現版
azure-ai-agentserver-core        2.2.0  requires: azure-core<2.0.0,>=1.37.0, isodate<1.0.0,>=0.6.1,
                                         starlette<2.0.0,>=0.45.0, hypercorn<1.0.0,>=0.17.0,
                                         opentelemetry-api/sdk<2.0.0,>=1.43.0,
                                         microsoft-opentelemetry<2.0.0,>=1.3.5,
                                         aiohttp<4.0.0a0,>=3.10.0, azure-identity<2.0.0,>=1.17.0
```
來源:同上 PyPI JSON API,`info.requires_dist`(`https://pypi.org/pypi/<pkg>/<ver>/json`,直接以 `Invoke-RestMethod` 取出欄位,非網頁推測)。

**關鍵結論(回答「responses 2.2.0 要求 core ==2.2.0 還是 >=2.2.0」)**:
`azure-ai-agentserver-core<2.3.0,>=2.2.0` —— 是**範圍 pin**(`>=2.2.0,<2.3.0`),不是精確 `==2.2.0`,但因為 core 目前最新就是 2.2.0,實務上等於釘死在 2.2.0.x。

**關鍵結論(回答「invocations 1.1.0 是否與 core 2.2.0 相容」)**:
**不相容**。`azure-ai-agentserver-invocations==1.1.0` 的 `requires_dist` 明確寫 `azure-ai-agentserver-core<2.2.0,>=2.1.0` —— 上界排除了 2.2.0。也就是說,FA 現在用的 `invocations==1.1.0` **無法**與 `responses==2.2.0`(需要 core>=2.2.0)在同一個 Python 環境中共存,除非把 invocations 一併升級到 **1.2.0**(其 `requires_dist` 已放寬為 `azure-ai-agentserver-core<3.0.0,>=2.1.0`,相容 core 2.2.0)。
來源:`https://pypi.org/pypi/azure-ai-agentserver-invocations/1.1.0/json`(`info.requires_dist`)。

### 1.3 core 2.1.0 → 2.2.0 的 Breaking Changes(與 FoundryStateStore / tasks / multi_turn_task / set_resilient_tasks_enabled 相關)

來源:`Azure/azure-sdk-for-python` tag `azure-ai-agentserver-core_2.2.0`,檔案 `sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md`(2.1.0 起始於第 41 行,2.2.0 起始於第 3 行)。

逐字引用(2.2.0 / 2.2.0b2 / 2.2.0b1 / 2.1.0 區段):

> "## 2.2.0 (2026-09-23)\n\n### Other Changes\n\n- Stable release promoting the 2.2.0 preview series. No functional changes since 2.2.0b2."

> "## 2.2.0b1 (2026-09-03)\n\n### Features Added\n\n- Added `AgentConfig.session_guid`, populated from the platform-owned `FOUNDRY_AGENT_SESSION_GUID` environment variable for hosted session incarnation identity.\n\n### Other Changes\n\n- Changed the default Azure Monitor trace sampling rate to 100%. ... \n- Disabled Azure SDK, HTTPX, Requests, urllib, and urllib3 instrumentation by default."

> "## 2.1.0 (2026-08-24)\n\n### Other Changes\n\n- Added compatibility bounds to runtime and development dependencies so installation cannot silently resolve to incompatible releases."

**結論:core 2.1.0 → 2.2.0 之間,CHANGELOG 沒有任何 `### Breaking Changes` 區段**(只有 Features Added / Other Changes)。唯一一次「resilient task 改為嚴格 opt-in」的破壞性變更,是在**更早**的 `2.1.0b1`(2026-08-11),而這段文字明確指出此行為**已經**在 FA 目前所用的 core 2.1.0 系列中生效:

> "## 2.1.0b1 (2026-08-11)\n\n### Breaking Changes\n\n- The resilient task subsystem is now **strictly opt-in**. `AgentServerHost` constructs the `TaskManager` only when resilient tasks are enabled via `set_resilient_tasks_enabled(True)` (or a protocol option that maps to it, e.g. the responses `resilient_background`). Previously the manager was always constructed and a declared `@task` / `@multi_turn_task` implicitly enabled the startup recovery scan. ... Existing apps that rely on `@task` must call `set_resilient_tasks_enabled(True)` ... \n- Removed `TaskMetadata`, `TaskContext.metadata`, and `TaskRun.metadata`. Durable application state now belongs in an explicit `FoundryStateStore`..."

因為 FA 已在用 core 2.1.0(晚於 2.1.0b1),這個變更對 FA **沒有額外影響**——FA 既有的 `set_resilient_tasks_enabled(True)` 呼叫已符合新契約。**2.1.0→2.2.0 這一步對 FoundryStateStore / tasks / multi_turn_task 沒有發現新的破壞性變更**(基於 CHANGELOG 逐字比對;若之後發現行為差異,應視為文件未記載的落差並回報)。

`responses` 2.1.0 → 2.2.0 的對應破壞性變更(影響 Copilot SDK 整合的 history 抓取行為):來源 tag `azure-ai-agentserver-responses_2.2.0`、`CHANGELOG.md` 第 12 行起:

> "## 2.2.0b2 (2026-09-24)\n\n### Bugs Fixed\n\n...\n- Changed the default history fetch limit from 100 to -1 (unlimited), avoiding automatic truncation of conversation history. ..."
> "## 2.2.0b1 (2026-08-27)\n\n### Breaking Changes\n\n- `ResponseUsageInputTokensDetails.cache_write_tokens` is now required by the latest AgentServer contract."

### 1.4 invocations 1.1.0 → 1.2.0 Breaking Changes

來源:tag `azure-ai-agentserver-invocations_1.2.0`,`CHANGELOG.md` 第 3、10、36 行。

> "## 1.2.0 (2026-09-29)\n\n### Other Changes\n\n- Stable release of the 1.2.0 preview series.\n- Generate fallback invocation and session IDs only when no valid supplied ID is available."
> "## 1.2.0b1 (2026-09-03)\n\n### Other Changes\n\n- Expanded supported `azure-ai-agentserver-core` versions to `<3.0.0`."

**沒有 `### Breaking Changes` 區段**。1.1.0→1.2.0 對既有 invocations handler 程式碼應是向後相容的(僅新增 Voice tracing API 與 fallback ID 邏輯調整)。

### 1.5 GA 還是 preview/beta?

- `azure-ai-agentserver-responses` 2.2.0:PyPI classifier `Development Status :: 5 - Production/Stable`(GA),來源同 1.1。
- Microsoft Learn 明確陳述平台面是 GA:「Microsoft Foundry Hosted Agents is generally available and supports agents built with your own code or a preferred agent framework.」—— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- 但「長時執行韌性(resilient_background / multi_turn_task / crash recovery)」整體仍是 **preview**:「Resilience for long-running Microsoft Foundry hosted agents (preview)」標題本身即標 (preview);內文:「Items marked preview in this article are currently in preview. This preview is provided without a service-level agreement...」—— https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience

---

## Q2. Host API(Responses host 類別、handler、streaming、取消、多協定)

### 2.1 Host 類別與 handler 註冊

來源:PyPI `azure-ai-agentserver-responses` README(https://pypi.org/project/azure-ai-agentserver-responses/)+ SDK tag `azure-ai-agentserver-responses_2.2.0`、`docs/handler-implementation-guide.md`。

> "`ResponsesAgentServerHost` is an `AgentServerHost` subclass that adds Responses protocol endpoints. Register your handler with the `@app.response_handler` decorator... Handlers MUST be `async def` and take exactly three positional parameters (`request`, `context`, `cancellation_signal`). Sync handlers and the 2-arg signature `(request, context)` are hard-rejected at decoration time."

```python
@app.response_handler
async def my_handler(request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
    ...
```

### 2.2 ResponseContext 欄位

PyPI README 列表(逐字):

> "| `response_id` | Unique ID for this response |\n| `conversation_id` / `conversation_chain_id` | Conversation identifiers; `conversation_chain_id` is the framework-computed stable id shared by every turn in a chain |\n| `is_shutdown_requested` | Whether the server is draining |\n| `platform_context` | `PlatformContext` with `user_id_key`... and `call_id`... |\n| `client_headers` | ... |\n| `query_parameters` | ... |\n| `shutdown` | `asyncio.Event` set on graceful server shutdown — distinct from the per-request cancellation signal |\n| `client_cancelled` | `bool` set when the cancel cause is `/cancel` endpoint or non-bg POST disconnect |\n| `is_recovery` | `bool` set on a crash-recovered re-entry |\n| `is_steered_turn` | `bool` set on the drain re-entry that follows a steering input |\n| `pending_input_count` | `int` count of queued steering inputs |\n| `exit_for_recovery()` | ... |\n| `get_input_items()` | ... |\n| `get_input_text()` | ... |\n| `get_history()` | Load conversation history items |"

`x-agent-session-id` 不在 `ResponseContext` 屬性清單中直接命名,而是走 core 的 `get_request_context()`:
> "The AgentServer SDK exposes these as constants on `PlatformHeaders` and reads them for you — through ... `get_request_context()` in Python." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract

前一個版本的 Session ID 標頭行為(invocations 的 README,Responses 行為概念相同,由 core 統一處理):`x-agent-session-id` 由 `agent_session_id` query 參數 → `FOUNDRY_AGENT_SESSION_ID` 環境變數 → 自動產生 UUID 的順序解析。

### 2.3 Streaming / SSE 事件

PyPI README + handler-implementation-guide.md(tag `azure-ai-agentserver-responses_2.2.0`,檔案 `docs/handler-implementation-guide.md`):

- `TextResponse(context, request, text=...)`:一行搞定完整 SSE 生命週期(`response.created → response.in_progress → ... → response.completed`),`text` 可傳 `AsyncIterable[str]` 做 token-by-token streaming。
- `ResponseEventStream`:細粒度控制,逐一 `yield` 對應 1:1 SSE event。範例(PyPI README 逐字):
```python
stream = ResponseEventStream(response_id=context.response_id, request=request)
yield stream.emit_created()
yield stream.emit_in_progress()
arguments = json.dumps({"location": "Seattle", "unit": "fahrenheit"})
yield from stream.output_item_function_call("get_weather", "call_001", arguments)
yield stream.emit_completed()
```
- 自訂/進度事件:透過 builder API(`add_output_item_message()` 等)或 reasoning item(`output_item_reasoning_item(...)`),都是走同一 `ResponseEventStream`,沒有另一套「custom progress event」API —— 進度訊息等同用一個 output item/message 表達(voicelive 範例即用「狀態訊息」output item 模擬進度,見 Q7)。

### 2.4 取消(`POST /responses/{id}/cancel`)

Handler 簽章的第三個參數就是取消訊號(PyPI README 逐字):

> "The per-request cancellation signal is delivered as the **3rd positional handler argument** (`cancellation_signal: asyncio.Event`), not via a `ResponseContext` attribute. It fires on client cancel (`/cancel` API or non-bg POST disconnect) or steering pressure; it does NOT fire on server shutdown — `context.shutdown` is the independent surface for that case."

錯誤情境(README「Troubleshooting」逐字):

> "**400 Bad Request** (cancel): The response was not created with `background=true`, or it has already reached a terminal state."
> "**404 Not Found**: The response ID does not exist. In hosted deployments persisted responses live in the Foundry hosted responses store; in local development they live under `${AGENTSERVER_STATE_ROOT:-~/.agentserver}/responses/` by default."

### 2.5 一個 host 能否同時服務 Invocations + Responses?

**可以,官方有明確程式碼範例**,來源:tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/docs/handler-implementation-guide.md:228-238`:

> "### Multi-Protocol Composition\n\nFor agents that serve both Invocations and Responses protocols, use cooperative (mixin) inheritance:\n\n```python\nfrom azure.ai.agentserver.invocations import InvocationAgentServerHost\nfrom azure.ai.agentserver.responses import ResponsesAgentServerHost\n\nclass MyHost(InvocationAgentServerHost, ResponsesAgentServerHost):\n    pass\n\napp = MyHost()\n```"

Microsoft Learn 平台文件同樣確認這是「一個 container、一個協定清單」的設計:

> "Hosted agent containers can expose one or more protocols... **Not sure?** Start with **Responses**. You can always add an Invocations endpoint later—a Hosted agent can support both protocols simultaneously." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents

> "**New protocols** — Invocations, Activity, and A2A protocols join the existing Responses protocol. A single agent can expose multiple protocols simultaneously." —— https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview

**對應 `azure.yaml` / agent version 定義**(foundry-samples 範例,見 Q3):`protocols:` 陣列可以同時放 `invocations` 與 `responses` 兩個條目(各自帶 `version`)。

### 2.6 官方「Invocations → Responses 遷移指南」

**沒有找到名為「Invocations→Responses 遷移」的專文**。最接近的官方文件是「Migrate hosted agents to the latest version」(https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview),但這篇講的是「初版 preview 後端 → 新後端」(`azure-ai-agentserver-agentframework`/`langgraph` → 新 protocol 套件)的遷移,**不是**「同一 host 從 Invocations 協定換成 Responses 協定」的逐步指南。**Unknown / not documented**:目前沒有找到「如何把既有 Invocations handler 改寫成 Responses handler」的官方分步教學;可參考的是「Hosted agent runtime contract」頁面中 Invocations 與 Responses 的對照表(見 Q3)與上方 2.5 的多協定共存範例。

---

## Q3. protocol_versions / endpoints / 版本與 session/conversation 的關係

### 3.1 `protocol_versions` 精確值

**容器協定版本(container protocol version)字串為 `"2.0.0"`**(GA,非套件版本號),Responses 與 Invocations 共用同一個容器協定版本概念。

來源:https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview#update-the-protocol-version(逐字):

> "## Update the protocol version\n\nSet every hosted agent protocol version to `\"2.0.0\"`. Protocol version `\"1.0.0\"` is no longer supported:\n\n```python\n# Initial preview\nProtocolVersionRecord(protocol=AgentEndpointProtocol.RESPONSES, version=\"v1\")\n\n# Supported version\nProtocolVersionRecord(protocol=AgentEndpointProtocol.RESPONSES, version=\"2.0.0\")\n```"

`azd`/`azure.yaml` 層級的對應寫法(官方 sample,見 Q7,`azure.yaml` 逐字):
```yaml
protocols:
  - protocol: responses
    version: 2.0.0
```
所以 FA 若要新增 Responses,`protocol_versions` 應追加 `{"protocol": "responses", "version": "2.0.0"}`,與現有 `{"protocol": "invocations", "version": "2.0.0"}` 並列(同一 agent version 定義可並存多個協定條目,見 2.5)。

容器協定 2.0.0 帶來的行為差異(非只是版本號遞增,而是身份傳遞方式改變):

> "Container protocol version 2.0.0 changes how per-request identity reaches your container and downstream calls. Version 1.0.0 is no longer supported, and the platform blocks requests to agents that still use it." —— 同上頁 `#container-protocol-200`

### 3.2 Container 對外路由(endpoint)

來源:https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents(「Agent identity and endpoint」一節,逐字):

> "- **Responses**: {project\_endpoint}/agents/{name}/endpoint/protocols/openai/responses\n- **Invocations**: {project\_endpoint}/agents/{name}/endpoint/protocols/invocations\n- **Invocations (WebSocket)**: wss://{account}.services.ai.azure.com/api/projects/{project}/agents/{name}/endpoint/protocols/invocations\\_ws?api-version=v1\n- **A2A v1.0 (GA) and v0.3 (preview)**: {project\_endpoint}/agents/{name}/endpoint/protocols/a2a"

容器內部(`azure-ai-agentserver-*` 套件實作的) HTTP route,來源:PyPI README:

- Responses:`POST /responses`、`GET /responses/{response_id}`、`POST /responses/{response_id}/cancel`、`DELETE /responses/{response_id}`、`GET /responses/{response_id}/input_items`。
- Invocations:`POST /invocations`、`GET /invocations/{invocation_id}`、`POST /invocations/{invocation_id}/cancel`、`GET /invocations/docs/openapi.json`、`GET /invocations/docs/asyncapi.{json,yaml}`、WS `/invocations_ws`。
- Health:`GET /readiness`(所有協定共用,core 提供)。

### 3.3 呼叫端如何叫用 hosted agent(含「新舊模式」對照)

**舊模式(initial preview,已廢棄)**:共用 project-level OpenAI 端點 + `agent_reference`:
> "`project.get_openai_client()` with `extra_body={\"agent_reference\": {\"name\": ..., \"type\": \"agent_reference\"}}`" —— migrate-hosted-agent-preview 頁 SDK method changes 表

**目前模式(latest)**:每個 agent 有專屬端點,client 直接「預綁定」agent 名稱,不再需要 `agent_reference`:
> "`project.get_openai_client(agent_name=\"my-agent\")` — client is pre-bound, no `extra_body` needed" —— 同上
> "**Dedicated agent endpoint** — Each agent gets its own endpoint URL (for example, `{project_endpoint}/agents/{name}/endpoint/protocols/openai/responses`). You no longer route through a shared project endpoint with `agent_reference` in the request body." —— 同上頁「What changed」

(註:`agent_reference` + `extra_body` 寫法在**沒有專屬端點的 Prompt Agent 場景**仍然出現在目前文件中,例如 A2A「Connect from another Foundry agent」範例呼叫一個 prompt agent 時依然用 `extra_body={"agent_reference": {...}}`。Hosted Agent 則統一走專屬端點。)

### 3.4 如何指定特定 agent **版本**

- 程式碼參照慣例:「In code, you refer to an agent by `<agent_name>:<version>`. Create each agent once, then persist and reuse that identifier...」—— https://learn.microsoft.com/azure/foundry/agents/concepts/development-lifecycle
- Session 層級可明確指定版本:`project.agents.create_session(agent_name, isolation_key=..., version_indicator=...)` —— migrate-hosted-agent-preview 頁 SDK method changes 表。
- Endpoint 層級(stable endpoint)靠 `version_selector` 決定 100% 流量去哪個版本,兩種路由策略:
  > "1. **Always use latest** (default): 100% of traffic routes to the most recently created agent version... \n2. **Pinned to a specific version**: 100% of traffic routes to the agent version you select, called the *active agent version*. New versions don't change what's served until you update the selector." —— https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent#understand-the-agent-object-model
- REST PATCH 範例(逐字):
  > "PATCH {{endpoint}}/agents/{{agent_name}}?api-version=v1 ... { \"agent_endpoint\": { \"version_selector\": { \"version_selection_rules\": [{\"type\": \"FixedRatio\", \"agent_version\": \"2\", \"traffic_percentage\": 100}] } } }" —— 同上
- **「An agent endpoint serves one version at a time and routes 100% of its traffic to that version. Traffic splitting between versions isn't supported.」** —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents(Platform details → Versioning)

### 3.5 Session ↔ Conversation ↔ 協定的對應關係

來源:https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions(逐字,核心表格):

> "| Aspect | Session | Conversation |\n| --- | --- | --- |\n| What it represents | Sandbox compute and persisted filesystem (`$HOME`, `/files`) | History of messages, tool calls, and responses |\n| Identifier | `agent_session_id` | `previous_response_id` or `conversation` (Responses protocol only) |\n| Used for | File uploads, working state across turns | Threading turns of a chat together |\n| Managed by | The platform, through the `/sessions` API | The platform (Responses); your container code (Invocations) |"

> "**Responses protocol.** Conversation continuity comes from `previous_response_id` or a `conversation` ID—not from the session ID. Reusing the same `agent_session_id` doesn't, on its own, replay prior messages to the model. ... **`conversation`.** Create a conversation object once via `POST .../endpoint/protocols/openai/conversations`, then pass its `id` on every `responses.create` call. The platform stores the message history under that conversation ID, and **a stable `agent_session_id` is automatically associated with the conversation**. Subsequent calls reuse the same sandbox without you having to track the session ID."

即:**conversation 不是「綁定」session,而是 conversation 建立時,平台會自動配一個 session 與它關聯**;之後用該 `conversation.id` 呼叫,就會自動路由到同一個 sandbox。

**版本更換時 conversation 是否能延續?** 逐字搜尋後**沒有找到明確陳述**(官方文件只說 conversation 儲存在 Foundry、「persists independently of compute state」,以及 endpoint 的 `version_selector` 決定流量去哪個版本;但沒有一句話直接回答「把 version_selector 從 v1 切到 v2 之後,舊 conversation id 是否還能被 v2 讀取/延續」)。**標記為 unknown / not documented**,不應假設可以或不行。唯一旁證(間接、非直接回答)是 core 2.2.0b1 CHANGELOG 提到 `FOUNDRY_AGENT_SESSION_GUID`(session 的「incarnation identity」)與 responses 2.2.0b2 CHANGELOG 提到:

> "Scoped durable multi-turn task IDs with `FOUNDRY_AGENT_SESSION_GUID` when available, preventing recreated same-name sessions from colliding with task tombstones. Existing pre-rollout active chains remain resumable through a legacy-ID lookup." —— `azure-ai-agentserver-responses` CHANGELOG.md,`2.2.0b2` 區段(tag `azure-ai-agentserver-responses_2.2.0`)

這段話暗示「同名 session 被重建(例如新的 sandbox incarnation)」是平台已知且處理的情境,但**沒有**明確連結到「agent version 變更」這個觸發條件,因此仍標記 unknown。

### 3.6 Session 生命週期(與 Q8 共用)

> "Session lifetime: You can configure the idle timeout per agent version from 2 through 60 minutes, with a 15-minute default. If no request arrives within that window, the platform deprovisions the compute and persists the session state. The platform permanently deletes a session after 30 days of inactivity." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents

---

## Q4. Foundry Conversations(誰存歷史、能否不喚醒 container 讀取、RBAC、限制)

### 4.1 誰儲存對話歷史

**平台(Foundry Conversations)儲存,不是 container**:

> "A conversation ID is a durable record of conversation history (messages, tool calls, and responses) stored in Foundry... Persistence: Conversation history is stored in Foundry and persists independently of compute state." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents

### 4.2 外部服務能否用 managed identity 直接讀 `/openai/v1/conversations/{id}/items` 而不喚醒 container?

**端點與驗證方式已證實是「資料平面 REST API」、獨立於 agent container**,其驗證走 OAuth2/Entra,不經過 container:

- Endpoint(來源:Azure OpenAI conversations REST 參考頁,https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/conversations):
  ```
  GET {endpoint}/openai/v1/conversations/{conversation_id}/items
  ```
  驗證方式:`ApiKeyAuth`(`authorization` 標頭)或 `OAuth2Auth`(`scope: https://cognitiveservices.azure.com/.default`,implicit flow)。
- 這與「Agent Service data-plane REST 端點」(`https://ai.azure.com` 範圍)是不同的 audience/scope,屬於 Foundry 帳戶層的 Azure OpenAI 相容 API,本質上就是直接打 Foundry 的 conversations 儲存,**不經過 agent 容器**。

**但「不會喚醒 container」這句話本身,沒有在任何頁面被逐字陳述**。我找到的最接近佐證是:

> "Persistence: Conversation history is stored in Foundry and persists independently of compute state." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
> "Cross-channel access: Users can access the same conversation from the playground, API, Teams, or other published channels." —— 同上

這兩句強烈暗示 conversation 讀取與 compute(sandbox)狀態無關,但**沒有一句明確寫「GET conversation items 不會觸發 sandbox 啟動」**。**標記為 unknown / not documented(強烈間接佐證,但非逐字確認)**。建議:若要在生產環境依賴此行為(FA 的「不喚醒 VM 讀歷史」設計),應以實測驗證,而非僅依賴此推論。

### 4.3 讀取 Conversations 需要的 RBAC

沒有找到「conversations API 專屬」的 RBAC 條目;但 OAuth2 scope 是 `https://cognitiveservices.azure.com/.default`,對應的典型角色依官方「Hosted agent permissions reference」頁面的角色總表(https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions),与「資料平面互動」最貼近的兩個角色:

> "| [Foundry Agent Consumer](...) | Interact with agent endpoints (least-privilege role for consumers) |\n| [Foundry User](...) | Create agents, perform model inference, and interact with agents |"
> "| [Cognitive Services OpenAI User](...) | Access account-level OpenAI endpoints directly |"

**Unknown / not documented**:沒有找到專門寫「讀 conversations items 需要角色 X」的句子;上述三個角色(`Foundry Agent Consumer`、`Foundry User`、`Cognitive Services OpenAI User`)都是候選,但哪一個是**官方指定的最小角色**沒有逐字確認,應留待實測或進一步查證(例如 `rbac-foundry` 頁的 permission-to-role 對照表附錄)。

### 4.4 限制(item 大小、保留期、最大筆數)

來源:Azure OpenAI conversations REST 參考(https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/conversations):

> "`limit` | query | No | integer (int32) | A limit on the number of objects to be returned. Limit can range between **1 and 100**, and the default is **20**."(List conversation items)
> "`items` | array of OpenAI.InputItem | **Constraints:** maxItems: 20" (Create conversation items —— 單次 POST 最多 20 筆 item)

**保留期(retention/TTL)**:沒有在這份 REST 參考找到 conversations 專屬的 TTL/保留期陳述。Session(非 conversation)層級的保留期是 30 天(見 Q3.6);**conversation 本身的保留期 unknown / not documented**。

### 4.5 Tool calls / custom items 是否被儲存?Container 能否附加自訂 item(例如 progress)?

- Tool calls 確定會被存:「Conversations store items, which can include messages, tool calls, tool outputs, and other data.」—— https://learn.microsoft.com/azure/foundry/agents/concepts/runtime-components
- Container(容器端程式碼)**可以**用 `POST .../conversations/{id}/items` 主動附加 item(REST 規格本身就有 "Create conversation items" 操作,且 input 類型是通用 `OpenAI.InputItem`,沒有限制呼叫者必須是哪一方)。但**沒有找到**官方文件明確說「Responses handler 應該用這個 API 寫入自訂 progress item」這種用法建議/範例 —— **unknown / not documented** 這是否是官方建議的「自訂進度事件」寫法(2.3 節已說明:官方建議的 progress 表達方式是走 SSE 的 output item,而非寫入 conversation items)。

---

## Q5. Background + resilience(resilient_background、TaskManager、並發、idempotency)

### 5.1 `background=true, store=true` 的語意

> "Recovery applies only to responses that are **stored and run in the background** - that is, requests with `store=true` and `background=true`." —— https://learn.microsoft.com/azure/foundry/agents/how-to/recover-long-running-work

> "**Default**: `resilient_background` defaults to `False`. Without the opt-in, a crash mid-handler leaves the response in the \"crash-failed\" state: the next-lifetime recovery scanner marks it `failed` (`error.code=\"server_error\"`) instead of re-invoking the handler." —— `resilient-responses-developer-guide.md`(tag `azure-ai-agentserver-responses_2.2.0`),約第 9 行起

Configuration matrix(逐字,同檔案):

> "| `store` | `background` | `resilient_background` | Summary |\n|---|---|---|---|\n| `true` | `true` | `True` | **Full recovery.** Handler is re-invoked with `context.is_recovery == True`. Persisted events replay to reconnecting clients. |\n| `true` | `true` | `False` (default) | **Failed marker.** Response is marked `failed` on restart. Handler is NOT re-invoked. ... |\n| `true` | `false` (foreground) | any | **Failed marker.** Response is marked `failed` with `code=server_error`. Handler is NOT re-invoked (the client's HTTP connection is already dead). |\n| `false` | any | any | **Best-effort failed marker** during shutdown grace period only. No persistence. Recovery does not apply. |"

### 5.2 `resilient_background` 在哪裡設定、保證什麼

設定位置:`ResponsesServerOptions(resilient_background=True)`,傳給 `ResponsesAgentServerHost(options=...)`。來源:PyPI README + `recover-long-running-work` 頁。

**保證內容(逐字,`resilient-responses-developer-guide.md` 頂部)**:

> "When `resilient_background=True` (opt-in — the default is `False`), the framework automatically wraps your response handler in a **resilient task**. If the server crashes mid-response:\n\n- Background responses are automatically re-invoked on restart\n- Stream events are preserved for client reconnection\n- Conversation state is maintained across crashes\n\n**Opting in (`resilient_background=True`) gets you the framework half for free**: re-invocation on restart, event replay for reconnecting clients, and conversation continuity — with no handler changes. A naive handler re-invoked this way still produces a correct response (it just re-runs the whole turn)."

**是 at-least-once,不是 at-most-once,也不是確定性 replay**(「handler 從頭重跑」而非重放記憶體狀態):

> "Recovery reenters the handler from its beginning. It isn't deterministic replay, and it doesn't restore local variables or an in-memory call stack. The handler uses durable checkpoints or watermarks to determine which work is already complete." —— https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience

> "A naive recovered handler still produces a correct response - it just reruns the whole turn." —— https://learn.microsoft.com/azure/foundry/agents/how-to/recover-long-running-work

因此语意是:**框架保證「at-least-once 的 handler 重新進入」**,副作用是否重複(即是否「at-most-once」)**由應用層自己用 watermark/idempotency key 保證**,框架不提供。

### 5.3 與 core resilient tasks / `multi_turn_task` / TaskManager 的關係

**Responses host 內部就是用 core 的 TaskManager**(逐字,tag `azure-ai-agentserver-core_2.2.0`,`docs/tasks-guide.md:138`):

> "> **Protocol note:** the responses protocol exposes a `resilient_background` server option that maps to this switch — constructing a `ResponsesAgentServerHost(options=ResponsesServerOptions(resilient_background=True))` enables the subsystem automatically, so responses apps typically don't call `set_resilient_tasks_enabled` directly."

**Conversation locking / per-conversation 序列化**(逐字,`resilient-responses-developer-guide.md:38` 配置表 + `:167`):

> "| Conversation lock | Prevents conflicting concurrent writes |"(What the Framework Provides,Zero Code)
> "> **`conversation_id` chains**: when a request supplies `conversation_id`, sequential turns extend the chain even when `steerable_conversations=False`. Only **concurrent overlap** (a new turn arriving while a prior turn's handler is still in progress) returns 409 `conversation_locked`. This is independent of the `steerable_conversations` option — that option only controls whether mid-turn inputs are queued (steerable) or rejected (non-steerable)."

**Steering(排隊而非分叉)**:`steerable_conversations=True` 時,後續 turn 必須引用前一個 turn 的 `response_id`(`previous_response_id`),分叉會被拒絕(409):

> "The framework rejects forks with HTTP 409 ... `code: \"conversation_fork_not_supported\"` ... There is no soft path through; a steerable conversation cannot be branched." —— `resilient-responses-developer-guide.md`

**底層機制是 core 的輸入前置條件(input-acceptance precondition)**,對應 `tasks-guide.md` §4「Input-acceptance preconditions」/ `if_last_input_id`:

> "**`if_last_input_id=\"<prev>\"`** — an optional precondition on `.start` / `.run`. The framework verifies that the chain's currently-stored last-accepted `input_id` equals `<prev>` before accepting the new input. If a concurrent caller advanced the chain first, the call raises `LastInputIdPreconditionFailed`." —— `azure-ai-agentserver-core` `docs/tasks-guide.md` §4.1

**兩個請求同時打到同一個 conversation 會怎樣**(`task_id` 層級的併發語意,逐字,`tasks-guide.md` 第 110 行表格):

> "| Concurrent `.start` on same `task_id` while in-flight | `TaskConflictError` | If `steerable=True`: queued; else `TaskConflictError` |"

即:非 steerable 的 conversation,第二個併發請求直接拿到錯誤(Responses 層是 HTTP 409 `conversation_locked`;底層 task 層是 `TaskConflictError`);steerable 的 conversation,第二個請求會被排入 steering queue、觸發目前 turn 提早結束(cooperative cancel),而不是兩個 handler 同時跑。

### 5.4 `GET /responses/{id}` 輪詢

端點已在 2.3 列出(`GET /responses/{response_id}`),支援 `?stream=true` 做 SSE replay(PyPI README 逐字:「Get response state (JSON or SSE replay via `?stream=true`)」)。

### 5.5 response id 是否 idempotent / 能否由呼叫端自帶 idempotency key(對應 FA 的 `input_id=<RID>`)

**`response_id` 一律由框架伺服端產生,沒有找到任何「呼叫端可自帶 response_id/idempotency key」的欄位或文件**。原始碼佐證(tag `azure-ai-agentserver-responses_2.2.0`,`azure/ai/agentserver/responses/_id_generator.py:57`):

```python
@staticmethod
def new_response_id(partition_key_hint: str | None = "") -> str:
    """Generate a new response ID with the ``caresp`` prefix. ..."""
    return IdGenerator.new_id("caresp", partition_key_hint)
```

對照 FA 現有的 `task_id=<agent>, input_id=<RID>` 模式,官方對應的「冪等」機制不是「帶自己的 response id」,而是:(a) 在 core 的 **multi_turn_task** 上用 `task_id` + 呼叫端自帶的 `input_id` 做 1:1 對映(逐字,`tasks-guide.md` §4.1):

> "**`input_id`** — the resilient name of one input within the chain. ... Multi-turn: per turn; the framework generates a GUID per turn unless the caller supplies one (callers managing their own per-message ids — e.g. chat clients — pass them through)."

(b) 或應用層自己在 `FoundryStateStore` 存一個 watermark/idempotency token,在重試/恢復時檢查是否已執行過下游副作用,範例(逐字,來自 GitHub code search 命中的 `tasks-guide.md` 與 `task-and-streaming-spec.md`):

> "receipt = await payment_gateway.charge(\n    ctx.input[\"card\"],\n    ctx.input[\"amount\"],\n    idempotency_key=pending_token,\n)" —— `azure-ai-agentserver-core` `docs/tasks-guide.md`(GitHub code search 命中,`Azure/azure-sdk-for-python`)
> "token = uuid4().hex\nstate[\"dedup_token\"] = token\nawait store.set_item(\"effect\", state)\nawait do_side_effect(idempotency_key=token)\n# crash-recovered lifetimes re-issue the call with the SAME token,\n# letting the downstream system de-dupe." —— `docs/task-and-streaming-spec.md`(同上搜尋命中)

即:**idempotency key 是「應用層模式」,不是平台原生欄位**。這與 FA 既有「`task_id=<agent>, input_id=<RID>`」的設計是同一個思路,可以直接沿用、對應到 `multi_turn_task(task_id=..., input_id=RID)`,不需要另造一層。

---

## Q6. A2A(啟用、角色、Responses 映射、hop-limit)

### 6.1 啟用 incoming A2A 的確切步驟

來源:https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint

> "Enabling incoming A2A requires two things: an **agent card** that describes your agent's capabilities, and the **A2A protocol** enabled on the agent endpoint. You can set both in a single PATCH call."

REST 範例(逐字節錄):
```
PATCH $BASE_URL/agents/$AGENT_NAME?api-version=v1
{
  "agent_card": {
    "description": "...", "version": "1.0",
    "skills": [{"id": "general-qa", "name": "General Q&A", "description": "..."}]
  },
  "agent_endpoint": {
    "protocol_configuration": { "responses": {}, "a2a": {} }
  }
}
```
Python SDK 對應:`project.agents.update_details(agent_name=..., agent_endpoint=AgentEndpointConfig(protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration(), a2a=A2AProtocolConfiguration())), agent_card=AgentCard(...))`。

### 6.2 「Incoming A2A requires the responses protocol」——確認原文屬實

逐字確認(與使用者提供的引用一致):

> "## Supported agent types\n\nIncoming A2A requires the responses protocol. Prompt agents support the responses protocol by default, and you can expose them as A2A endpoints." —— https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint

限制區段再次重申:

> "- Incoming A2A requires the responses protocol. Agents that don't use the responses protocol can't be exposed as A2A endpoints." —— 同頁「## Limitations」

**確認屬實,且是強制要求,沒有替代路徑**:FA 若要讓 Hosted Agent 開放 incoming A2A,該 agent 必須具備 Responses 協定端點(可以是多協定並存,見 Q2.5,不必移除 Invocations)。

### 6.3 A2A 訊息如何映射進 Responses handler

官方文件**沒有逐字描述**「A2A message → Responses handler 的欄位映射(conversation/context id、task id、message id dedupe)」的實作細節頁面。可確認的事實:

- A2A 的 task/context 保留期:「For Foundry agent targets, A2A tasks and contexts are retained for 60 days from their most recent write. Each new write to a task or context resets its 60-day retention period.」—— https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent
- A2A v1.0 僅支援 JSON-RPC、純文字模態、不支援 streaming:「For A2A v1.0, only the JSONRPC transport is supported... Only **text** modality is supported... Streaming responses (server-sent events) aren't supported.」—— enable-agent-to-agent-endpoint 頁「Limitations」
**Unknown / not documented**:A2A 的 `task id`/`message id` 如何具體映射到 Responses 的 `conversation_id`/`response_id`/去重機制,沒有找到官方逐字規格頁面(可能在尚未索引到的深層 REST 參考或 A2A SDK 原始碼內,但 Microsoft Learn 搜尋與 foundry-samples 搜尋均未命中明確映射表)。

### 6.4 呼叫端(caller)需要的 RBAC —— 確認角色名稱

**`Foundry Agent Consumer`**,逐字確認多處:

> "All A2A URLs require Microsoft Entra ID authentication. ... The calling identity must have the **Foundry Agent Consumer** role or another Foundry role that grants endpoint access on the Foundry project or agent." —— enable-agent-to-agent-endpoint 頁「Verify the agent card」
> "Incoming A2A requests require Microsoft Entra ID authentication. Key-based authentication and unauthenticated access aren't supported. ... The identity behind that token must have the **Foundry Agent Consumer** role, or another Foundry role that grants endpoint access, on the target Foundry project or target agent." —— 同頁「Configure authentication for incoming requests」
> "For a Foundry agent target, use its A2A base path, enable incoming A2A, and grant the calling identity the **Foundry Agent Consumer** role or higher on the target project." —— https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent「Prerequisites」

角色定義(「least-privilege role for consumers」,來源 hosted-agent-permissions 頁角色總表):

> "| [Foundry Agent Consumer](...) | Interact with agent endpoints (least-privilege role for consumers) |"

角色指派範例(逐字 CLI):
```bash
az role assignment create \
  --assignee-object-id "<calling-principal-object-id>" \
  --assignee-principal-type "$PRINCIPAL_TYPE" \
  --role "eed3b665-ab3a-47b6-8f48-c9382fb1dad6" \
  --scope "<target-project-or-agent-scope>"
```
`PRINCIPAL_TYPE` 可為 `User`/`Group`/`ServicePrincipal`;直接取 token 時用 `https://ai.azure.com/.default` scope。

**注意「Foundry User」是另一組前提**(不是給 A2A caller,而是給建立/測試 agent 的開發者,且不是 A2A 專屬):

> "Required Azure role: **Foundry User** or higher on the Foundry project." —— enable-agent-to-agent-endpoint 頁「Prerequisites」(此為「啟用 incoming A2A 這個動作本身」需要的角色,不是「呼叫 A2A 端點的 caller」需要的角色;caller 要的是 Foundry Agent Consumer,見上)
> RBAC 命名已改版:「Foundry User」、「Foundry Owner」、「Foundry Account Owner」、「Foundry Project Manager」舊名分別是「Azure AI User」、「Azure AI Owner」、「Azure AI Account Owner」、「Azure AI Project Manager」(id 與權限不變)。 —— 同頁 Important 區塊

### 6.5 Outgoing A2A(toolbox A2A tool)GA/preview 狀態與呼叫方式

> "Use the generally available `a2a` tool type for new integrations. The earlier `a2a_preview` tool type remains in preview for existing integrations.\n\n| Tool definition | A2A protocol version | Status |\n| --- | --- | --- |\n| `a2a` (`A2ATool` or `A2AToolboxTool`) | 1.0 | Generally available (GA) |\n| `a2a_preview` (`A2APreviewTool` or `A2APreviewToolboxTool`) | 0.3 | Preview |" —— https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent

呼叫方式(Python SDK,逐字節錄):
```python
tool = A2ATool(a2a_version=A2AProtocolVersion.V1_0, project_connection_id=a2a_connection.id)
agent = project.agents.create_version(
    agent_name=AGENT_NAME,
    definition=PromptAgentDefinition(model="gpt-4.1-mini", instructions="...", tools=[tool]),
)
```
前提角色:「**Foundry Project Manager** to create the A2A project connection. **Foundry User** to create and test the agent.」—— 同頁 Prerequisites

### 6.6 Hop-limit / 迴圈防護指引

**逐字檢索「loop / hop / recursion / depth / cycle」於 enable-agent-to-agent-endpoint 與 tools/agent-to-agent 兩篇全文,均無命中任何 hop-limit 或防迴圈機制的陳述**。**Unknown / not documented**:官方文件目前沒有針對 A2A 多跳(agent 呼叫 agent 呼叫 agent…)提供 hop-limit、遞迴偵測或迴圈防護的指引。FA 若要串接多層 A2A,需自行在應用層設計防護(例如在呼叫鏈的 context 裡帶一個遞減的 hop counter),這不是官方現成功能。

---

## Q7. Copilot SDK 在 Responses 協定下的官方範例/adapter

### 7.1 官方 adapter 套件現況:`azure-ai-agentserver-ghcopilot` —— **已棄用**

這個套件**曾經存在**於 `Azure/azure-sdk-for-python` 原始碼樹(`sdk/agentserver/azure-ai-agentserver-ghcopilot`),歷經三個版本,**但第三版就直接把它清空、標記棄用**:

CHANGELOG(逐字,`main` 分支,`sdk/agentserver/azure-ai-agentserver-ghcopilot/CHANGELOG.md:3-9`):

> "## 1.0.0b3 (2026-06-23)\n\n### Breaking Changes\n\n- **This package has been deprecated.** It will no longer receive updates or bug fixes.\n- All dependencies have been removed. This package is now an empty placeholder to reserve the name on PyPI.\n- The integration test agent has been removed.\n- Users should migrate to `azure-ai-agentserver-core` and `azure-ai-agentserver-responses` directly."

原始碼現狀(逐字,`azure/ai/agentserver/githubcopilot/__init__.py:7-21`,`main` 分支):

```python
"""DEPRECATED: azure-ai-agentserver-ghcopilot is no longer maintained.

This package has been deprecated. Please use azure-ai-agentserver-core
and azure-ai-agentserver-responses directly.
"""

import warnings

from ._version import VERSION

warnings.warn(
    "azure-ai-agentserver-ghcopilot is deprecated and will not receive further updates. "
    "Migrate to azure-ai-agentserver-core and azure-ai-agentserver-responses.",
    DeprecationWarning,
    stacklevel=2,
)

__all__: list[str] = []
__version__ = VERSION
```

PyPI 上的 `azure-ai-agentserver-ghcopilot` 實際**從未發布過上述任何功能版本**,目前只是一個「保留名稱」的佔位套件(`version 0.0.0b1`,`requires_dist: null`,0 個真實檔案):

> "This package will be `azure-ai-agentserver-ghcopilot`\n\nThe `Azure SDK` team is planning to release this package for public usage soon. This is a placeholder on PyPI for planning purposes and do not yet contain any code." —— https://pypi.org/pypi/azure-ai-agentserver-ghcopilot/json,`info.description`

**結論:官方曾經短暫維護一個「GitHub Copilot SDK adapter」,但已明確棄用,且明確指示「請直接用 core + responses」**。這與本次 N6 設計方向(FA 自己在 core+responses 之上薄薄包一層,而不是等一個官方 adapter)完全吻合官方的最終建議。

### 7.2 官方範例(不是套件,是 `foundry-samples` 原始碼範例)—— **存在,且正是 Copilot SDK + Responses**

來源:`microsoft-foundry/foundry-samples`(`main` 分支),目錄 `samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/`。

`AGENTS.md`(逐字開頭):

> "# Coding Agent Instructions - GitHub Copilot Responses VoiceLive\n\nThis sample is a **Microsoft Foundry hosted agent**. It uses the GitHub Copilot SDK with the Responses protocol and a Foundry BYOK model to provide streamed, voice-friendly output for VoiceLive scenarios."

`README.md`(逐字):

> "## How it works\n\nThe hosted agent uses the Foundry project endpoint as an Azure provider for the Copilot SDK. `DefaultAzureCredential` acquires a managed-identity token for `https://ai.azure.com/.default`, so the default deployment does not require a GitHub token or model API key.\n\nEach Responses request is forwarded to a persistent Copilot session. Assistant message deltas become Responses text deltas, tool starts become short status messages, ..."

環境變數表(逐字節錄):

> "| `FOUNDRY_AGENT_SESSION_ID` | No | Session ID used to resume Copilot conversation state. |"

套件版本(`requirements.txt`,pip-compile 鎖定版):
```
azure-ai-agentserver-core==2.1.0
azure-ai-agentserver-responses==1.0.0b9
github-copilot-sdk==1.0.11
```
**⚠️ 注意:這個官方範例鎖定的 `azure-ai-agentserver-responses` 是 `1.0.0b9`(beta),core 是 `2.1.0`,都明顯落後於目前的 GA 2.2.0 系列** —— 代表這份範例尚未隨 2.2.0 更新,引用其程式碼時要留意 API 可能已有差異(例如本報告 Q2 提到的 `TextResponse`/`ResponseEventStream` 介面在 1.0.0bx 與 2.2.0 之間可能有演進)。

### 7.3 SDK session ↔ Responses conversation/history 的映射方式(原始碼逐字)

檔案:`src/github-copilot-responses-voicelive/main.py`(`main` 分支)。

**完全不使用 `context.get_history()` 來重建狀態** —— handler 只呼叫 `context.get_input_text()` 取得這一輪的文字,其餘對話延續完全交給 Copilot SDK 自己的 session:

```python
# main.py:83-87 (節錄)
session_id = os.environ.get("FOUNDRY_AGENT_SESSION_ID") or str(uuid.uuid4())
...
try:
    _session = await _client.resume_session(session_id, **session_options)
    logger.info("Resumed Copilot session id=%s", session_id)
except Exception:
    _session = await _client.create_session(session_id=session_id, **session_options)
    logger.info("Created Copilot session id=%s", session_id)
```

```python
# main.py handler 節錄
app = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=20),
)

@app.response_handler
async def handle_response(request, context, cancellation_signal):
    user_input = await context.get_input_text() or "Hello!"
    ...
    session = await _ensure_session()
    ...
    send_task = asyncio.create_task(session.send(_VOICE_INSTRUCTION + user_input, mode="immediate"))
```

**映射策略**:`agent_session_id`(= 平台 `FOUNDRY_AGENT_SESSION_ID`,hosted sandbox 的 session 身分)直接拿來當 Copilot SDK 的 `session_id`。Session 物件是**process-level 全域單例**(`_client`/`_session` 模組級變數 + `asyncio.Lock`),同一個容器 process 內的所有 Responses 請求共用同一個 Copilot session,而不是每個 request 建一個。working directory 固定為 `$HOME`(`pathlib.Path.home()`),與 FA 現有「Copilot SDK session files 存在 $HOME、由 Foundry sandbox 持久化」的做法一致。

這**直接印證**了分析報告原先的推論:「`context.get_history()` is separate from the Copilot SDK's own session files」—— 官方範例本身就完全繞過 `context.get_history()`,只靠 `resume_session(session_id)` 讀 Copilot SDK 自己的 session 檔案。

### 7.4 是否持久化 SDK session 狀態 / 冷啟動行為

- **沒有使用 `FoundryStateStore`**:整份 `main.py` 沒有 import 或使用 `azure.ai.agentserver.core.storage.FoundryStateStore`。持久化完全依賴 Copilot SDK 自己把 session 檔案寫到 `$HOME`,再靠 Foundry sandbox 的 `$HOME` 持久化機制(見 Q3.6/Q8)跨 idle 週期存活。
- **沒有設定 `resilient_background=True`**:`ResponsesServerOptions(default_fetch_history_count=20)` 只設定了歷史抓取筆數上限,沒有開啟 crash-resilient background 模式。換言之,這個官方範例**沒有**對容器崩潰做 handler 重入復原;它依賴的韌性層級僅止於「sandbox idle→resume 時 `$HOME` 還在,所以 `resume_session()` 能接回去」,不是「process 崩潰中途 handler 自動重跑」。
- **冷啟動 / 真正首次呼叫**:`resume_session(session_id)` 丟例外時,`except Exception:` 直接 fallback 成 `create_session(session_id=...)`(新建)。也就是說:第一次呼叫(或 `$HOME` 內沒有該 session 檔案時)一律視為新 session。

### 7.5 其他協定下的同款官方 Copilot SDK 範例(佐證,非 Q7 主答)

同一個 repo 還有:
- `samples/python/hosted-agents/bring-your-own/invocations/github-copilot/`(Invocations 協定、streaming)
- `samples/python/hosted-agents/bring-your-own/activity/github-copilot/`(Activity 協定,用 `ActivityAgentServerHost`,給 Teams/M365 通路)

這進一步確認:**官方的「GitHub Copilot SDK 整合」策略是「每個協定各自一份 BYO 範例原始碼」,而不是一個跨協定的官方 adapter 套件**(7.1 的 `ghcopilot` 套件已棄用正是因為官方轉向了這種「範例優先、不包裝」的策略)。

---

## Q8. Version change / sessions(session 生命週期、$HOME 跨版本持久化、conversation 是否綁定版本)

### 8.1 Session 生命週期(逐字)

> "Session lifetime: You can configure the idle timeout per agent version from 2 through 60 minutes, with a 15-minute default. If no request arrives within that window, the platform deprovisions the compute and persists the session state. The platform permanently deletes a session after 30 days of inactivity." —— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents

> "A session is a stateful, isolated sandbox tied to a single logical workload (for example, one user's chat). The platform persists the session's filesystem (`$HOME` and uploaded files) across turns and across idle periods, so the agent can resume where it left off. **Sessions persist for up to 30 days.**" —— https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions

### 8.2 `$HOME` 跨版本持久化 —— **沒有找到明確陳述**

官方反覆講的是「`$HOME` 跨 **turns** 與跨 **idle periods**(同一 session)持久化」,但**沒有一句話明確講「$HOME 是否跨 agent version 持久化」**。已知事實:

- 每個 agent version 是「不可變快照(immutable snapshot)」,環境變數在建版時固定:「Each call to create a version produces an **immutable agent version**... Environment variables... are set per version and are immutable once the version is created.」—— https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- Idle timeout 是「per agent version」設定的(上引「per agent version」字樣),暗示 session 的某些屬性是跟著 version 走的,但沒有明說 `$HOME` 內容是否在切版號後被保留或清空。

**標記為 unknown / not documented**:`$HOME` 在「同一 `agent_session_id` 但 agent 已切到新版本」情境下是否仍可讀到舊版本寫入的檔案,官方文件沒有逐字回答。不應假設兩種結果中的任何一種。

### 8.3 Responses conversation 是否綁定 agent version

已於 Q3.5 詳述:**conversation 綁定的是「agent 這個穩定實體」(透過 stable endpoint),不是 immutable 的某個「agent version」**(從「An Agent Application projects one or more agents as a service... independently addressable... A **deployment** is a running instance of an agent version」/「Agent: The stable, consumer-facing representation of an agent... as its underlying versions evolve」等描述可推論 conversation 理論上掛在穩定 agent 身份下,而非某個 version),但**沒有找到逐字句子**明確回答「version_selector 切到新版本後,舊 conversation id 能否繼續對話」。維持 Q3.5 的 unknown 標記。

---

## 來源清單(編號,全部為 2026-10-02 查核;SDK 原始碼附 tag)

1. PyPI JSON API — `azure-ai-agentserver-responses`:https://pypi.org/pypi/azure-ai-agentserver-responses/json
2. PyPI JSON API — `azure-ai-agentserver-core`:https://pypi.org/pypi/azure-ai-agentserver-core/json
3. PyPI JSON API — `azure-ai-agentserver-invocations`:https://pypi.org/pypi/azure-ai-agentserver-invocations/json
4. PyPI JSON API — `azure-ai-agentserver-invocations` 1.1.0:https://pypi.org/pypi/azure-ai-agentserver-invocations/1.1.0/json
5. PyPI JSON API — `azure-ai-agentserver-core` 2.2.0:https://pypi.org/pypi/azure-ai-agentserver-core/2.2.0/json
6. PyPI Simple index — `azure-ai-agentserver-invocations`:https://pypi.org/simple/azure-ai-agentserver-invocations/
7. PyPI Simple index — `azure-ai-agentserver-responses`:https://pypi.org/simple/azure-ai-agentserver-responses/
8. PyPI Simple index — `azure-ai-agentserver-core`:https://pypi.org/simple/azure-ai-agentserver-core/
9. PyPI JSON API — `azure-ai-agentserver-ghcopilot`:https://pypi.org/pypi/azure-ai-agentserver-ghcopilot/json
10. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md`
11. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/CHANGELOG.md`
12. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-invocations_1.2.0`,`sdk/agentserver/azure-ai-agentserver-invocations/CHANGELOG.md`
13. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/docs/tasks-guide.md`
14. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/docs/state-store-guide.md`
15. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/docs/handler-implementation-guide.md`
16. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/docs/resilient-responses-developer-guide.md`
17. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/_id_generator.py`
18. `Azure/azure-sdk-for-python`,`main` 分支,`sdk/agentserver/azure-ai-agentserver-ghcopilot/README.md`
19. `Azure/azure-sdk-for-python`,`main` 分支,`sdk/agentserver/azure-ai-agentserver-ghcopilot/CHANGELOG.md`
20. `Azure/azure-sdk-for-python`,`main` 分支,`sdk/agentserver/azure-ai-agentserver-ghcopilot/INSTRUCTIONS.md`
21. `Azure/azure-sdk-for-python`,`main` 分支,`sdk/agentserver/azure-ai-agentserver-ghcopilot/azure/ai/agentserver/githubcopilot/__init__.py`
22. `Azure/azure-sdk-for-python` 原始碼搜尋命中(`idempotency_key`):`sdk/agentserver/azure-ai-agentserver-core/docs/tasks-guide.md`、`sdk/agentserver/azure-ai-agentserver-core/docs/task-and-streaming-spec.md`
23. Microsoft Learn — 什麼是 hosted agents:https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
24. Microsoft Learn — Hosted agent runtime contract:https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract
25. Microsoft Learn — Build with agents, conversations, and responses:https://learn.microsoft.com/azure/foundry/agents/concepts/runtime-components
26. Microsoft Learn — Resilience for long-running hosted agents (preview):https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience
27. Microsoft Learn — Recover long-running work after a crash (preview):https://learn.microsoft.com/azure/foundry/agents/how-to/recover-long-running-work
28. Microsoft Learn — Migrate hosted agents to the latest version:https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview
29. Microsoft Learn — Manage hosted agent sessions:https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions
30. Microsoft Learn — Enable incoming A2A on a Foundry agent:https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
31. Microsoft Learn — Connect to an A2A agent endpoint from Foundry Agent Service:https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent
32. Microsoft Learn — Hosted agent permissions reference:https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions
33. Microsoft Learn — Role-based access control for Microsoft Foundry:https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry
34. Microsoft Learn — Configure and share your agent:https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent
35. Microsoft Learn — Agent development lifecycle:https://learn.microsoft.com/azure/foundry/agents/concepts/development-lifecycle
36. Microsoft Learn — Publish your agent as an Agent Application:https://learn.microsoft.com/azure/foundry/agents/how-to/agent-applications
37. Microsoft Learn — REST API: Azure OpenAI conversations:https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/conversations
38. Microsoft Learn — Foundry Hosted Agents (Agent Framework hosting):https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent
39. Microsoft Learn — Agent Background Responses (Agent Framework):https://learn.microsoft.com/agent-framework/agents/background-responses
40. `microsoft-foundry/foundry-samples`,`main` 分支,`samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/README.md`
41. `microsoft-foundry/foundry-samples`,`main` 分支,`samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/AGENTS.md`
42. `microsoft-foundry/foundry-samples`,`main` 分支,`samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/src/github-copilot-responses-voicelive/main.py`
43. `microsoft-foundry/foundry-samples`,`main` 分支,`samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/src/github-copilot-responses-voicelive/requirements.txt`
44. `microsoft-foundry/foundry-samples`,`main` 分支,`samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive/azure.yaml`
45. `microsoft-foundry/foundry-samples`(路徑確認,未逐字引用內容)— `samples/python/hosted-agents/bring-your-own/invocations/github-copilot/`、`samples/python/hosted-agents/bring-your-own/activity/github-copilot/`
46. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/docs/responses-resilience-spec.md`
47. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/docs/resilience-contract.md`(機器驗證一致性契約,71c7 claims 驗證的主要依據)
48. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/hosting/_task_id.py`
49. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/hosting/_chain_id.py`
50. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/hosting/_resilient_orchestrator.py`
51. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/hosting/_endpoint_handler.py`
52. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/_options.py`
53. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-responses_2.2.0`,`sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/_response_context.py`
54. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/docs/task-and-streaming-spec.md`
55. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/tasks/_lease.py`
56. `Azure/azure-sdk-for-python`,tag `azure-ai-agentserver-core_2.2.0`,`sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/tasks/_client.py`
57. 本機倉庫(`原私有 repo`),commit `00000000`,`src/fa-shared-context-poc/requirements-hosted.txt`(`git show 00000000:...`)

---

## 71c7 claims verification(針對 71c7 於 2026-10-02 提出的 5 項主張,逐一對照「已發布 tag」原始碼 + 規格文件驗證)

> 驗證方法:所有引用一律取自 **release tag**(`azure-ai-agentserver-responses_2.2.0` / `azure-ai-agentserver-core_2.2.0`),不是 `main` 分支(71c7 讀的是 main 上的 README/guide,未釘版本;以下逐條用發布版 tag 的原始碼與規格文件覆核,包含 71c7 尚未看過的兩份「規範文件」:`docs/responses-resilience-spec.md`(設計總綱)與 `docs/resilience-contract.md`(**機器驗證的一致性契約**,文件自稱「on conflict, `resilience-contract.md` is authoritative」)。檔案路徑一律相對於 `sdk/agentserver/azure-ai-agentserver-responses/`(core 的另行標明)。

### Claim 1 — 完整崩潰復原需要 `resilient_background=True` + `background=true` + `store=true` 三者皆成立;foreground 不會被重新進入;`resilient_background` 預設 `False`

**結論:exact,逐字且逐行確認,來源分三層(PyPI README / 開發指南 / 原始碼 / 機器驗證契約),完全一致。**

- 原始碼層級(最硬的證據——建構子預設值本身):`azure-ai-agentserver-responses_2.2.0:azure/ai/agentserver/responses/_options.py:26`
  ```python
  resilient_background: bool = False,
  ```
- 機器驗證契約(`resilience-contract.md:68-70`,tag `azure-ai-agentserver-responses_2.2.0`):
  > "- **`resilient_background`** *(server option, default `False`)* — whether the framework engages full crash-recovery for `background=true, store=true` requests. When `True`, the supporting providers MUST be present..."
- Dispatch matrix(`resilience-contract.md:144-146`,逐字):
  > "| Row | `store` | `background` | `resilient_background` | Path A (within-grace) | Path B (grace exhausted) | Path C (crash / Path-B failure) |\n|----:|---------|--------------|----------------------|-----------------------|--------------------------|---------------------------------|\n|  1  | `true`  | `true`       | `True`               | natural terminal      | hand the in-flight handler to the resilient-task primitive's recovery; runtime exits; next lifetime re-invokes the handler with `is_recovery=True` | next lifetime re-invokes the handler with `is_recovery=True` |"
- **Foreground(`background=false`)不會被重新進入,且這點與 `resilient_background` 設定無關**(`resilience-contract.md:204-210`,逐字,Row 3):
  > "### Row 3 — Marked failed, foreground (`store=true, background=false`, any `resilient_background`)\n\nA stored response observable over the original (foreground) HTTP connection. `resilient_background` is a free axis — foreground responses do not benefit from resilient handler recovery because the client connection is gone. Path A/B/C have the same shape as Row 2; all failure markers use `code=server_error`..."
- `store=true, background=true` 但 `resilient_background=False`(Row 2,`resilience-contract.md:185`)同樣**不會**重新進入 handler,只標記 `failed`。
- 對應的開發者導向措辭(`resilient-responses-developer-guide.md:9-10`,tag 同上):
  > "When `resilient_background=True` (opt-in — the default is `False`), the framework automatically wraps your response handler in a **resilient task**."
- PyPI README(同版號對應文字,來源 2.2.0 release 的 README,逐字):
  > "Crash recovery is **opt-in** via `ResponsesServerOptions(resilient_background=True)`. When opted in, background responses with `store=True` are crash-recoverable: the handler is re-invoked on restart... Without the opt-in (the default), a crash mid-handler marks the response `failed` instead of re-invoking the handler."

71c7 的措辭完全準確,沒有需要修正之處。

### Claim 2 — Responses host 自行管理生命週期(取消/持久化/stream replay/把 handler 包成 resilient task);`steerable_conversations` 預設 `False`;兩種行為與確切錯誤碼;是否用到 core 的 TaskManager/`multi_turn_task`,用什麼 `task_id`

**結論:exact,且比 71c7 原述更精確——框架內部其實註冊了「兩個」resilient-task primitive(one-shot `@task` + `@multi_turn_task`),依請求形狀動態挑選,而不是只用 `multi_turn_task` 一種。**

- `steerable_conversations` 預設值(原始碼,`_options.py:27`):
  ```python
  steerable_conversations: bool = False,
  ```
- **確認 Responses host 直接 import 並使用 core 的 `multi_turn_task`**(`azure/ai/agentserver/responses/hosting/_resilient_orchestrator.py:25-29`):
  ```python
  from azure.ai.agentserver.core.tasks import (
      ...
      multi_turn_task,
  )
  ```
  且在同檔案 `:548-551` 實際呼叫:
  ```python
  @multi_turn_task(
      name="responses_resilient_multi_turn",
      steerable=self._options.steerable_conversations,
  )
  ```
  同時也註冊了一個一次性(one-shot)`@task` primitive(legacy 名稱 `"responses_resilient_background"`,見同檔案 `:476-479` docstring);每個請求用 `_pick_primitive()`(`:567-607`)依 `conversation_id` 是否存在、`steerable_conversations` 是否開啟來二選一——**`conversation_id` 存在,或 `steerable_conversations=True`,就走 `multi_turn_task`;否則走一次性 `@task`**(`:603-607` 逐字):
  > "if conversation_id is not None:\n    return self._multi_turn_task_fn\nif self._options.steerable_conversations:\n    return self._multi_turn_task_fn\nreturn self._one_shot_task_fn"
- **`task_id` 的確切推導**(`hosting/_task_id.py:44-91`,函式 `derive_task_id`):與 `conversation_chain_id` 用同一套推導函式 `derive_conversation_chain_id`(`hosting/_chain_id.py`),三種情況(逐字,`_chain_id.py:16-21`):
  > "1. `conversation_id` present → the chain is the conversation.\n2. steerable, no `conversation_id` → the chain is the `previous_response_id` linkage (chained response ids share one partition key).\n3. everything else → one-shot; each request stands alone."
  三種 id 形狀(`_chain_id.py:23-26`):`cchain_{partition(conversation_id)}{scope}` / `rchain_{partition(previous_response_id or response_id)}{scope}` / 原樣的 `response_id`。且 `_task_id.py` docstring 明確說「task_id 等於 `conversation_chain_id`,除非另外提供 hosted 專用的 `task_session_id`(用 session GUID 做私有命名空間,公開的 `conversation_chain_id` 不變)」。
- **`steerable_conversations=False` 時,同一 conversation 上的併發重疊請求 → HTTP 409 `conversation_locked`**(確切原始碼,`hosting/_endpoint_handler.py:1013-1024`):
  ```python
  except TaskConflictError as exc:
      ...
      err_body = {
          "error": {
              "message": f"Conversation is locked — task is {exc.current_status}",
              "type": "conflict",
              "code": "conversation_locked",
              "param": None,
          }
      }
      return JSONResponse(err_body, status_code=409, ...)
  ```
  上層有清楚的因果註解(`_resilient_orchestrator.py:1304-1307`):
  > "`TaskConflictError` from the underlying primitive ALWAYS signals a real conflict (concurrent overlap on a multi-turn-non-steerable chain, OR a duplicate task_id collision). It propagates up to the endpoint handler which maps it to HTTP 409 `conversation_locked`."
- **`steerable_conversations=True` 時,請求被排隊/導向(steered),且 `previous_response_id` 必須緊接最新一輪,否則視為分叉 → HTTP 409 `conversation_fork_not_supported`**(確切原始碼,`_endpoint_handler.py:987-1004`):
  ```python
  except LastInputIdPreconditionFailed as exc:
      ...
      err_body = {
          "error": {
              "message": (
                  "This agent does not support conversation forking. "
                  "previous_response_id must reference the most recent "
                  "response in the conversation."
              ),
              "type": "conflict",
              "code": "conversation_fork_not_supported",
              "param": "previous_response_id",
          }
      }
      return JSONResponse(err_body, status_code=409, ...)
  ```
  排隊而非分叉的行為(`_resilient_orchestrator.py:547-551` 註解,逐字):
  > "Used for rows where the request has a conversation_id OR a steerable previous_response_id... The steerable= flag gates whether mid-turn input is queued (steerable=True) or rejected with TaskConflictError(in_progress) (steerable=False)."

71c7 的兩個行為描述與確切錯誤碼(`conversation_locked` / 隱含的 fork 拒絕,71c7 沒寫出 `conversation_fork_not_supported` 這個確切字串,但方向正確)**均確認無誤**;"host 內部用 TaskManager / multi_turn_task" 的判斷**方向正確**,但精確地說是「**一次性 `@task` 與 `@multi_turn_task` 兩個 primitive,依請求動態二選一**」,task_id 用的是與 `conversation_chain_id` 相同推導邏輯所得的值(hosted 環境下可能再疊加 session-GUID 私有命名空間,但對外仍等同 `conversation_chain_id`)。

### Claim 3 — 序列化範圍是 `context.conversation_chain_id`,每個 conversation 一把鎖;有沒有「整個 VM / 整個 agent instance 跨 conversation 全局互斥」的官方選項?

**結論:確認「每 conversation 一把鎖」屬實;官方沒有提供「跨 conversation 的 agent-wide/VM-wide 全局互斥」選項 —— `not provided`。**

- 逐字確認鎖的範圍是 conversation 層級(`resilient-responses-developer-guide.md:276-284`):
  > "`ResponseContext.conversation_chain_id: str` is a **derived, stable chain identifier**: the framework computes it so that **every turn of the same conversation resolves to the same value**... It is the same value the framework uses internally to partition resilient tasks."
- `_chain_id.py` 的 `_scope()`(agent_name + session_id 的雜湊)**只是 task_id 字串裡的一段「entropy/命名空間」尾綴,不是鎖的範圍**——真正決定「是否共用同一把鎖」的是 `_partition_key()` 從 `conversation_id`/`previous_response_id`/`response_id` 萃取出的 partition key,**不同 conversation 的 partition key 不同,因此 task_id 不同,因此鎖不同**(`_chain_id.py` 全文邏輯,`:23-26` 的三種 case 定義已證明這點:每個 case 的種子都是「這個請求自己的」`conversation_id`/`previous_response_id`/`response_id`,沒有任何一個 case 把同一 agent/session 下的「所有 conversation」摺疊成同一個 id)。
- 對「agent-wide / VM-wide / instance-wide / process-wide / global lock / across conversations」等字樣,在三份最權威的文件(`responses-resilience-spec.md`、`resilience-contract.md`、`resilient-responses-developer-guide.md`,皆為 tag `azure-ai-agentserver-responses_2.2.0`)**逐字全文檢索,完全沒有命中**。
- **結論:not provided。** 官方沒有「一個 agent instance/VM 內,跨所有 conversation 的全局互斥鎖」這種選項;序列化單位就是(且僅是)`conversation_chain_id`。如果 FA 需要 VM-wide 互斥(例如保護共享的 Copilot SDK session 檔案不被兩個不同 conversation 同時寫),**必須自己在應用層加一層鎖**(例如用 `FoundryStateStore` 或 process 內的 `asyncio.Lock`,這正是 7.3 節官方 voicelive 範例採用的模式:`_session_lock = asyncio.Lock()`)。

### Claim 4 — Recovery 從頭重新進入、不是精確重放副作用;state store 寫入與 task/lease PATCH 互相獨立;復原是否用同一條「task lease/claim PATCH」路徑(可能 502);lease/claim PATCH 失敗時的行為

**結論:前兩句 exact;「同一條 PATCH 路徑」的推論正確(lease renewal 與 lease claim 都是走同一個 `TaskProvider.update()` → `PATCH /tasks/{id}`);502 的行為官方有明確分類與重試/逾時後放棄鎖的機制,細節如下。**

- 「從頭重新進入、非精確重放」(`responses-resilience-spec.md`,§ Recovered entry,與 Learn 頁逐字一致,見本報告 Q5.2/§long-running-agent-resilience):
  > "Recovery reenters the handler from its beginning. It isn't deterministic replay, and it doesn't restore local variables or an in-memory call stack."
- State store 寫入與 task lease/PATCH **互相獨立**(`resilience-contract.md` 多處 + PyPI README 逐字,先前已引用):
  > "State Store writes are independent from task lifecycle PATCHes and lease renewal." —— `resilient-responses-developer-guide.md`(2.2.0 README 對應段落同義重複)
- **Lease 的「claim/acquire」與「renew」走的是同一個底層 PATCH 端點**(原始碼證據,`azure-ai-agentserver-core_2.2.0:azure/ai/agentserver/core/tasks/_client.py:572-651`):
  ```python
  async def update(self, task_id: str, patch: TaskPatchRequest) -> TaskInfo:
      """Update a task via PATCH /tasks/{id}. ..."""
      ...
      http_request = HttpRequest("PATCH", url, params=params, content=json.dumps(body), ...)
      ...
      if response.status_code == 404:
          raise TaskNotFound(task_id)
      if response.status_code >= 400:
          _raise_classified(response, method="PATCH", url=url)
  ```
  `_lease.py` 的 `lease_renewal_loop()`(`_lease.py:155-163`)與 recovery 掃描器「重新取得(claim)一個被放棄的 lease」都是透過同一個 `TaskProvider.update(task_id, TaskPatchRequest(lease_owner=..., lease_instance_id=..., lease_duration_seconds=...))` → 同一支 `PATCH /tasks/{id}`,只是帶的 `lease_owner`/`If-Match` 不同。**沒有找到另一支獨立的「claim 專用」端點** —— claim 本質上就是「用新的 lease_owner 去 PATCH 一筆 lease 已過期(或從未持有)的 task」。
- **502 的分類**(`azure-ai-agentserver-core_2.2.0:docs/task-and-streaming-spec.md:1680-1710`,§28 Hosted provider (HTTP),機器可驗證分類矩陣,逐字節錄):
  > "| HTTP 409 with body `error.code == \"binding_mismatch\"` | `evicted` | ... Triggers the local-cleanup sequence. |\n| HTTP 409 with any other body (or malformed body) | `conflict` | Generic lifecycle conflict. |\n| HTTP 412 | `conflict` | Precondition / ETag mismatch. |\n| HTTP 408, 429 | `transient` | Request timeout / rate limited — retryable. |\n| **HTTP 5xx** | **`transient`** | **Server-side error — retryable.** |\n| Network failure, socket timeout, connection reset | `transient` | Transport-level errors. |"
  即:**502 被分類為 `transient`,官方文件明確標註「retryable」**。
- **傳輸層先自動重試**(`_client.py:410-415`,逐字):
  ```python
  AsyncRetryPolicy(
      retry_total=3,
      retry_on_status_codes=[408, 429, 500, 502, 503, 504],
      retry_backoff_factor=0.5,
  ),
  ```
  即先由 `azure-core` 的 `AsyncRetryPolicy` 對 502 自動重試最多 3 次(`backoff_factor=0.5`)。
- **若重試後仍失敗,逃逸到應用層的 `lease_renewal_loop`,其處理方式是「累計連續失敗次數,預設 3 次後放棄本地執行權」**(`_lease.py:87` 預設值 + `:237-253` 分類後的 fallback 路徑,逐字):
  ```python
  on_failure_count: int = 3,
  ...
  # Non-eviction classified errors fall through to the generic
  # failure-counter path (e.g. transient 503 → retry).
  consecutive_failures += 1
  logger.warning("Lease renewal failed for task %s (attempt %d/%d): %s", ...)
  if consecutive_failures >= on_failure_count and on_cancel_callback is not None:
      logger.error("Lease renewal failed %d times for task %s — signalling cancellation", ...)
      on_cancel_callback.set()
      break
  ```
  也就是說:**502 本身不會立刻讓 task 失敗**——它先被傳輸層重試(最多 3 次、指數退避),若仍持續失敗,`lease_renewal_loop` 累計到 `on_failure_count`(預設 3)次「連續」失敗後,觸發 `on_cancel_callback.set()`:**放棄這個 process 對該 task 的本地執行(取消本地 handler),而不是把 task 標記為終態失敗** —— task 記錄仍在 store 裡,lease 過期後由另一個 process(或下一次 recovery scan)重新 claim、重新進入 handler。這與「412(ETag 衝突,分類為 `conflict`)導致 `_HostedConflict` → 若 `current_status == "in_progress"` 則立即判定失去所有權、馬上 `on_cancel_callback.set()`(不等 3 次)」(`_lease.py:191-201`)是兩條不同的分支:**真正的「所有權被搶走」(409/412 conflict)是立即放棄;純傳輸層 5xx/逾時是計次後才放棄**。

**官方文件沒有使用「502」這個具體字眼**(文件只講「HTTP 5xx」這個類別),所以嚴格說「lease/claim PATCH 會不會回 502」這件事本身是**由 Foundry Task Storage 服務端決定、SDK 文件不承諾**,但「SDK 收到 5xx 時怎麼反應」這件事**有明確、可驗證的規格**(上述分類矩陣 + 重試政策 + lease loop 計次放棄邏輯),可視為對 71c7 問題的完整回答。

### Claim 5 — 取消訊號是 handler 第三參數 `cancellation_signal`;shutdown 另有獨立的 `context.shutdown`;`client_cancelled`/`is_steered_turn`/`pending_input_count` 三個欄位;復原後 `response.in_progress` 是「快照重置」,client 不可把 replay 的 SSE 跟先前答案串接

**結論:全部 exact,且在原始碼層級(不只是文件層級)逐一確認。**

- 三個欄位的型別宣告與初始值(原始碼,`azure-ai-agentserver-responses_2.2.0:azure/ai/agentserver/responses/_response_context.py`):
  ```python
  # :245-249 (annotations)
  is_recovery: bool
  is_steered_turn: bool
  pending_input_count: int
  ...
  client_cancelled: bool
  # :321-323, :342 (初始化)
  self.is_recovery: bool = False
  self.is_steered_turn: bool = False
  self.pending_input_count: int = 0
  ...
  self.client_cancelled: bool = False
  ```
- `cancellation_signal` 是 handler 的**第三個位置參數**,且與 `context.shutdown` 是兩個獨立訊號(PyPI 2.2.0 README,逐字,行號對應下載檔 `responses-README.md:27,121`):
  > "Cancellation is observed via the `cancellation_signal` event (set on client cancel, `/cancel` API, or steering pressure). Server shutdown is a **distinct** signal observed via `context.shutdown` — shutdown does NOT fire the cancellation signal; handlers that care about both must inspect each independently."
  原始碼佐證(`_response_context.py:329-342` 註解,逐字):
  > "# Composing cancellation surface. ``_cancellation_signal`` is ... the positional ``cancellation_signal`` parameter, not the private ... # ``client_cancelled`` is a cause flag stamped by the /cancel ..."
- **`response.in_progress` 是「快照重置」且 client 必須重設累積器(不可串接)——機器驗證契約的明文規則**(`resilience-contract.md:302-322`,§ Streaming sub-contract,逐字):
  > "3. **`response.in_progress` reset event.** On re-invocation the recovered handler MUST emit a `response.in_progress` event as its first **client-visible** event, carrying the corrected output items. ...\n\n**Client-side rule.** A streaming client MUST reset its accumulator on every `response.in_progress` event after the first."
  這比 Learn 文件(`long-running-agent-resilience`)的對應陳述更強制(用 RFC2119 的 `MUST`,而非一般敘述句),可視為最權威的版本。

### 小結:71c7 的 5 項主張是否準確

全部 **exact / 準確無誤**,僅 Claim 2 有一處可以更精確的補充:「用到 core 的 TaskManager/`multi_turn_task`」方向正確,但實際是**一次性 `@task` 與 `@multi_turn_task` 兩個 primitive 依請求動態二選一**,不是單純「永遠用 `multi_turn_task`」。

---

## 版本釘選確認(00000000 / requirements-hosted.txt)

**確認「00000000 requirements-hosted pins only core 2.1.0 + invocations 1.1.0」屬實**,逐字核對 commit `00000000` 的 `src/fa-shared-context-poc/requirements-hosted.txt`(本機 git 倉庫,`git show 00000000:src/fa-shared-context-poc/requirements-hosted.txt`):

```
azure-ai-agentserver-invocations==1.1.0
azure-ai-agentserver-core==2.1.0
starlette==1.6.0
azure-identity==1.25.3
httpx==0.28.1
pydantic==2.12.5
github-copilot-sdk==1.0.13
jsonschema==4.25.1
```

**確認:此檔案裡沒有 `azure-ai-agentserver-responses` 這一行**(這與 FA 目前只用 Invocations 協定的現況一致),且與工作目錄現版(非該 commit)完全相同——即 HEAD 尚未變動此檔。

### 最小候選 pin 組合(回答「responses X, core Y,invocations 能否共存」)

基於本報告 Q1 的 PyPI `requires_dist` 逐一核對(見上方 Q1.2 表):

| Package | 建議最小釘選 | 理由 |
|---|---|---|
| `azure-ai-agentserver-responses` | **`==2.2.0`**(或 `>=2.2.0,<2.3.0`) | 目前唯一 GA stable 版;其 `requires_dist` 本身要求 `azure-ai-agentserver-core<2.3.0,>=2.2.0`,所以 responses 這一行會把 core 的版本範圍**鎖定**在 `[2.2.0, 2.3.0)` |
| `azure-ai-agentserver-core` | **`==2.2.0`** | responses 2.2.0 的硬性要求;目前也是最新 GA |
| `azure-ai-agentserver-invocations` | **`==1.2.0`**(必須從 1.1.0 升級) | 1.1.0 的 `requires_dist` 是 `azure-ai-agentserver-core<2.2.0,>=2.1.0`——**明確排除 2.2.0**,在同一個 Python 環境裡會與 `responses==2.2.0` 的 `core>=2.2.0` 要求直接衝突、`pip` 會解析失敗。1.2.0 的 `requires_dist` 放寬為 `azure-ai-agentserver-core<3.0.0,>=2.1.0`,相容 core 2.2.0。 |

**Invocations 能否與 Responses 留在同一個環境/容器裡:可以,但前提是把 `invocations` 一併升到 1.2.0**。也就是說,`requirements-hosted.txt` 若要加入 Responses,不能只加一行 `azure-ai-agentserver-responses==2.2.0`,必須同時把 `azure-ai-agentserver-core==2.1.0` 改成 `==2.2.0`、把 `azure-ai-agentserver-invocations==1.1.0` 改成 `==1.2.0`,三者缺一都會在 `pip install` 階段就因版本範圍衝突而失敗(可用 `pip install --dry-run` 或 `pip-compile` 驗證,不需實際安裝即可確認相依解析結果)。

---

## 未能確認 / 標記 unknown 的事項總表(避免遺漏)

1. **Q3/Q8**:Responses conversation 是否綁定特定 agent version;version_selector 切版後舊 conversation 能否延續 —— **unknown**。
2. **Q4**:讀取 `GET /openai/v1/conversations/{id}/items` 是否確定「不會喚醒/啟動 agent sandbox」—— 有強烈間接佐證(conversation 儲存獨立於 compute 狀態),但**沒有逐字確認句**。
3. **Q4**:讀取 conversations 的「官方指定最小 RBAC 角色」是哪一個(`Foundry Agent Consumer` vs `Foundry User` vs `Cognitive Services OpenAI User`)—— **unknown,僅能列出候選**。
4. **Q4**:Conversation(而非 session)的保留期/TTL —— **unknown**(只查到 session 30 天、A2A task/context 60 天、state store item 預設 30 天,獨漏 conversation 本身)。
5. **Q6**:A2A message → Responses handler 的欄位級映射(task id / message id dedupe 規則)—— **unknown**。
6. **Q6**:A2A 多跳 hop-limit / 迴圈防護官方指引 —— **unknown(逐字檢索無命中,應視為「目前沒有」)**。
7. **Q2**:是否存在官方「Invocations → Responses 逐步遷移」專文(區別於「preview 後端 → 新後端」的遷移文)—— **unknown(未找到)**。
8. **Q8**:`$HOME` 內容是否跨 agent version 持久化 —— **unknown**。

以上 8 項在設計文件中應明確寫成「待驗證假設」,不可當作已證實的平台保證。
