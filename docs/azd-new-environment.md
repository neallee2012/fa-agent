# 新環境 `azd up` 設計（2026-10-10）

> 範圍：用 `azd up` 把 FA 部署到使用者自己的 Azure 訂閱。本文是設計與實作說明，**不是部署批准**，
> 也不代表已在雲端跑通。公開版只在包含這條路徑的 stage release 之後同步出去（stage 是作者自己維運、正式使用的環境）。
> 操作步驟見 [deploy/azd/README.md](../deploy/azd/README.md)；舊的手動 17 步保留為逐步排錯用的對照
> （[deploy/README.md](../deploy/README.md#new-environment-standard-flow)）。

## 1. 結論

- **用 azd 本體（Bicep provider＋hooks），不用 Foundry agent extension 的 `azure.ai.*` service host。**
  `azd provision` 部署現有的 `infra/shared-context` Bicep；Entra、首次安裝、Hosted Agent 這幾段交給
  hook 呼叫 repo 現有的腳本。stage CI（`deploy-staging.yml`→`ci_deploy.py`）和 azd 共用同一套腳本，
  不另寫第二套部署邏輯。
- 原本根目錄的 `azure.yaml` 是過時的 SMB 探針，這次改寫成新環境入口。

## 2. 為什麼不用 Foundry extension 的 service host（已查官方文件）

| 官方行為 | 跟 FA 的衝突 | 來源 |
|---|---|---|
| `azure.ai.agent`：「Reusing a name creates a new version」，`azd deploy` 每次建新版 | FA 的版本由 `seed_agents.py`（先寫 intent，不重送）與 `behavior_agent_versions sync` 管：新版本複製上一個 active 版本、只換 image，metadata 帶 `fa_build`／定義 hash，API 的 `HostedVersions.current` 靠它判斷 serving 版本。azd 另建版本會繞過這些檢查 | [azure.yaml reference](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)、[shared README](../src/fa-shared-context-poc/README.md)「官方工具只處理 azure.yaml 寫死的 agent」 |
| `azure.ai.connection` 的 `credentials` 從 azd env（`.azure/<env>/.env`）取值 | WebIQ 金鑰會以明文寫在磁碟上。azd 官方自己也說「Never store secrets in an azd .env file」 | [author azure.yaml](https://learn.microsoft.com/azure/foundry/agents/how-to/author-azure-yaml)、[azd 環境變數](https://learn.microsoft.com/azure/developer/azure-developer-cli/manage-environment-variables) |
| `azure.ai.toolbox` 的 tool 只有 `type`／`connection` | FA 的 `fa-org-tools` 需要 `server_label`、`allowed_tools`、`require_approval: never`；文件沒有這些欄位，不猜 | 同上 |
| 用既有 project（`endpoint`／`--project-id`）時「skips the automatic role assignments」 | FA 本來就要自己給每個 Agent 身分 project 角色 | [init-agent-project](https://learn.microsoft.com/azure/foundry/agents/how-to/init-agent-project) |
| 全部是 preview、無 SLA（`azure.ai.agents` ≥1.0.0-beta.8、`azure.ai.projects` ≥1.0.0-beta.4） | 不想讓新環境的基礎依賴 preview 的 provider | 同上 |
| `image` 可直接部署預先 build 的映像 | 映像本身相容：FA 的 Hosted 映像（`Dockerfile.behavior-assistant-desktop`，固定 `github-copilot-sdk==1.0.13`、Copilot runtime 1.0.83、`azure-ai-agentserver-*`）已經由 `ci_deploy.py build` 以 digest 推進 ACR，換用 azd 不會換 SDK 版本。不合的是上面那一列的版本語意，不是映像或 SDK | [azure.yaml reference](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)、`src/fa-shared-context-poc/requirements-hosted.txt` |

azd 本體的已證實行為（官方文件；第 3 點另外在本機 azd 1.32.0 實測）：

1. `services` 可以省略（schema 只要求 `name`）；`infra.provider: bicep` 讀 `infra/azd/main.bicep`。
2. Hook 有 `preprovision`／`postprovision`／`predeploy`／`postdeploy`；hook 看得到 azd env，Bicep output 自動寫回 azd env；
   `interactive: true` 會把 stdin 接到 console（隱藏輸入用）。
3. **沒有 service 時，project 層的 `postdeploy` 仍會執行，hook 內 `azd env set` 會寫回**（scratchpad 探針，`azd deploy --no-prompt`，無雲端呼叫）。
4. `azd hooks run <name>` 可單獨重跑某個 hook，用於批准中斷後接續。
5. 分層 provision（`infra.layers`）還是 beta，而且每層都會跑一次 hook；這次不用。
6. Graph Bicep extension 能宣告 application／servicePrincipal／oauth2PermissionGrant，但 extensible resource **不支援 what-if**，
   會讓 `azd provision --preview` 失效；所以 Entra 改由 hook 呼叫 Graph v1.0 的 upsert：
   `PATCH /applications(uniqueName='…')`＋`Prefer: create-if-missing`，重跑是同一個物件，不會再建一個
   （[upsert](https://learn.microsoft.com/graph/api/application-upsert?view=graph-rest-1.0)、[限制](https://learn.microsoft.com/graph/templates/limitations)）。

## 3. azd environment 的資料形狀

只有一個來源：`.azure/<env>/.env`（azd 管理、已在 `.gitignore`）。分三類：

**使用者輸入（`azd env set`；都有預設值）**

| 名稱 | 預設 | 用途 |
|---|---|---|
| `AZURE_ENV_NAME`、`AZURE_LOCATION`、`AZURE_SUBSCRIPTION_ID` | azd 詢問 | 名稱前綴、區域、訂閱；區域選單只列 Hosted Agents 區域（見下方「模型與區域」） |
| `FA_MODELS` | 終端機：hook 依區域列出可用模型讓人選；沒有終端機：catalog 全部 4 個 chat 模型 | 要部署、工作台要列的 chat 模型（逗號分隔，只接受 catalog 內名稱） |
| `FA_DEFAULT_MODEL` | `gpt-6.1-sol`（有選時），否則 `FA_MODELS` 第一個 | 新對話預設模型；也是第一次建記憶庫用的 chat 模型 |
| `AZURE_RESOURCE_GROUP` | `rg-<env>` | 唯一的 RG（Foundry 與 FA 資源放一起） |
| `FA_TEAM_OWNER_OID` | 執行者自己的 OID（Graph `/me`） | Team 主人；Easy Auth allowlist、consent、runtime `human_oid` |
| `FA_MODEL_CAPACITY_JSON` | 空（用 model catalog 的容量） | 例 `{"gpt-5.4-mini":50}`；只接受 catalog 內名稱 |
| `FA_MODEL_VERSION_UPGRADE_OPTION` | `NoAutoUpgrade` | 所有模型部署一致 |
| `FA_API_SUBNET_NAT_EGRESS` | `false` | 同 `prepare_environment.py` |
| `FA_DEPLOY_UPGRADE_SNAPSHOTS` | `false` | `true`：另建 ADR-0009 phase 5 工作檔快照專用的儲存帳號（`stsnap<token>`、私有端點、30 天刪除規則、帳號刪除鎖、API 身分在該 container 的備份讀寫自訂角色，不含刪除），API 取得 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`；與 `no-auto-gc` 備份帳號分開。打開會多建一個儲存帳號並指派角色，會產生費用；預設關，不需要就維持 `false` |
| `FA_ENABLE_AGENT_ONBOARDING` | `true` | 「＋」新 Agent 需要的受限 RBAC Admin（Team 隔離的已知例外，風險見 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md)；預設開）與 API 的 `FA_ONBOARDING_*`（同 stage）；設 `false` 就兩者都不給 |
| `FA_ENABLE_WEBIQ` | `false` | `true` 時 deploy 階段以隱藏輸入要金鑰並建 toolbox |
| `FA_ENABLE_MEMORY` | `true` | 建 Memory Store 並開私人／Team 記憶旗標（同 stage）；私人記憶與 Team note 由助理 VM 直接讀寫（[ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md) 的已知例外） |
| `FA_ENABLE_TEAM_MCP_CONNECTIONS` | `true` | API 身分 project 範圍 Contributor，Team MCP 可新增含金鑰 connection（同 stage） |
| `FA_HOSTED_IDLE_TIMEOUT_SECONDS` | `3600` | 同 stage |

**Bicep output（`azd provision` 自動寫回）**：RG、Foundry account／project 名稱、project endpoint 與 ARM ID、
ACR、CAE、API UAMI（名稱／client ID／principal ID）、Table／備份 storage、Web PubSub endpoint、Log Analytics，
以及 API／Job／Portal app 的名稱。全球唯一的名稱（storage、ACR、Foundry 子網域、Web PubSub）用
`uniqueString(subscription, env, location)`，不會撞到別人。

**Hook 寫回的結果**：`FA_MODEL_DEPLOYMENTS`（Bicep 要建的部署＝`FA_MODELS`＋記憶開啟時的 embedding）、
`FA_MEMORY_CHAT_MODEL`（記憶庫建立時用的 chat 模型，之後固定）、`FA_API_AUDIENCE`、`FA_API_APP_ID`、`FA_CONSOLE_APP_ID`、各 Agent 的 ID／OID、
各步驟完成標記與已部署映像。完整 runtime profile 每次由 `deploy/azd/profile.py` 重新組出（`.azure/<env>/fa/profile.json`），
沿用 `deployment_config.validate_config` 驗證後才給 `make_runtime.py`、`seed_agents.py` 等腳本使用。
所有 intent、plan、批准檔、image ref 都放 `.azure/<env>/fa/`（私有、不進 Git）。

<a id="models-and-region"></a>
**模型與區域（不是每個人都能用 westus3 與 gpt-6.1-sol）**

- **區域由 azd 自己問。** azd 1.32 在 preprovision hook 之前就解析 `location`（本機探針：沒有
  `AZURE_LOCATION` 時停在「Initialize bicep provider」、hook 沒跑），所以選單放在 Bicep：`location` 參數的
  `@allowed` 是 [Hosted Agents 區域](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability)
  （2026-09-29 版，31 區），`@metadata({azd:{type:'location', usageName:[…text-embedding-3-small,10]}})` 讓 azd
  隱藏沒有 embedding quota 的區域（[azd metadata](https://learn.microsoft.com/azure/developer/azure-developer-cli/metadata)；
  azd 1.32 原始碼確認 `parametersMappedToAzureLocation` 會套用 allowedValues 與 location metadata）。
  `usageName` 是編譯時常數，只能放每個環境都要的模型，所以 chat 模型的 quota 交給 hook。
- **模型由 preprovision hook 問。** 第一次在終端機跑且沒設 `FA_MODELS` 時，hook 唯讀讀
  `az cognitiveservices model list`／`usage list`，列出每個 catalog chat 模型在這個區域有沒有提供、quota 剩多少、
  需要多少，建議「有提供且 quota 夠」的那些；選了沒提供的模型就停，什麼都不改。之後再跑只驗證，不再問。
  沒有終端機時預設 catalog 全部，quota 不夠由既有 preflight 擋下。
- **一個決定，三處一致。** `deploy/azd/models.py` 把選擇變成 `FA_MODEL_DEPLOYMENTS`（Bicep 只建這些）、
  API 的 `FA_INSTRUCTION_MODELS`／`FA_INSTRUCTION_DEFAULT_MODEL`（工作台只列這些，選其他模型回 422），
  以及記憶庫的 chat 模型。`FA_INSTRUCTION_MODELS` 是 API 新增的選用 ENV：沒設時照舊列整份 catalog，stage 不受影響。
  azd 1.32 在每個 hook 後重新載入 env，`provision` 的部署階段才重新解析參數，所以同一次 `azd up` 就讀得到 hook 寫的值。
- **不再強制 gpt-4.1-mini。** 依 [Foundry 記憶文件](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)，
  記憶庫只需要「一個 chat 部署＋一個 embedding 部署」（範例用 gpt-5.2），沒有指定型號；所以記憶庫改用
  `FA_DEFAULT_MODEL`，只有 `text-embedding-3-small` 隨記憶一起部署。記憶庫建立後 chat 模型不變（`new_memory_store.py`
  讀回比對），所以 `FA_MEMORY_CHAT_MODEL` 第一次寫入後固定，且必須留在 `FA_MODELS`；這個選擇之前就 provision 過的環境（有 Bicep output、沒有 `FA_MODEL_DEPLOYMENTS`）不再詢問，沿用全部模型與 gpt-4.1-mini 記憶庫。
  程式裡仍綁 gpt-4.1-mini 的只有歷史驗收工具：固定內容的 standing 任務（`AssistantSubmit`，runtime 設定
  `AssistantSettings.model`）與 M2／M3／M4 harness；一般聊天、委派、技能與記憶回憶都用對話選的模型。
  沒部署 gpt-4.1-mini 的環境不能跑那個 standing 任務，工作台一般使用不受影響。
- **加減模型。** 之後 `azd env set FA_MODELS …` 再 `azd up`：新增的會建部署並出現在工作台；拿掉的只會從工作台消失，
  Bicep 增量部署不刪既有部署（要刪請在 Foundry 入口網站自己刪）。

功能旗標（HITL、groups、routines、Responses lane、對話中換模型 `FA_NATIVE_MODEL_SWITCH`、`conversation_defaults` 等）放在
[`deploy/azd/profile.defaults.json`](../deploy/azd/profile.defaults.json)。裡面沒有 tenant、訂閱、OID 或資源名稱；
測試會檢查每個 key 都是 stage profile 已有的 key，避免自己發明旗標。

## 4. 原 17 步對應到哪裡

| 原步驟 | azd 階段 | 做法 |
|---|---|---|
| 1 登入與目標核對 | `preprovision` | az 與 azd 都要登入同一個 tenant／訂閱；Graph `/me` 取 OID，未設 owner 時預設自己 |
| 2 Render | （取消） | 名稱與模型清單改在 `main.bicep` 內算（`loadJsonContent` 讀同一份 model catalog）；`prepare_environment.py` 只留給既有手動環境重跑 |
| 3 Subscription 前置 | `preprovision` | 原 `preflight_subscription.py subscription`，唯讀；不通過就停並印出要另外批准的註冊指令 |
| 4 Foundry＋監控、5 Foundation、W4 Blob、Web PubSub | `azd provision` | `main.bicep`（subscription scope）依序呼叫原 module，監控 workspace 以 output 直接接到 foundation，取代人工讀回 |
| 6 ACA 平台就緒 | `postprovision` | 原 `preflight_subscription.py aca-ready`；不通過就停，不往下部署 app |
| 7 Entra API app | `postprovision` | Graph upsert：API app（v1 token、`Behavior.Read`／`Behavior.Approve`）、SP、Azure CLI 第一方 SP、只對 owner 的 `Principal` consent |
| 8 映像 | `postdeploy` | `ci_deploy.py build api|hosted|console`（本機 Docker），只用 digest |
| 11 Seed Hosted | `postdeploy` | `seed_agents.py`（intent 不重送） |
| 12 Agent project 角色 | `postdeploy` | 每個 Agent 部署 `project-service-access.bicep` |
| 9 Profile | `postdeploy` | `profile.py` 以讀回值組 profile，沒有 placeholder |
| 10 首次安裝 | `postdeploy` | `make_runtime.py`→部署 Job→`run_fresh_job.py plan`→本機重建 plan 並核 hash→**owner 在終端機打字批准**→`apply`→`reconcile` |
| 8 API | `postdeploy` | `behavior-api.bicep`（首次安裝後才建，避免 D4 的 crash loop） |
| 11 Sync／route（首次不 follow） | `postdeploy` | `ci_deploy.py sync`；首次安裝**不跑 follow**：從沒準備過的 Agent 沒有舊環境可跟，伺服器回 `NATIVE_FILES_CONFIRMATION_REQUIRED`（10-10 實跑）。第一次 session 準備直接綁定已路由的版本；之後的 `azd deploy` 才 follow |
| 15 Portal app | `postdeploy` | `fresh_export`→`remote-bundle`→`web-console.bicep`→補 redirect URI 與 FIC→consent |
| 13 首次 session 準備 | `postdeploy` | `prepare_native_sessions.py`（owner token） |
| 16 WebIQ（選用） | `postdeploy` | `FA_ENABLE_WEBIQ=true` 時：`new_webiq_connection.py`（getpass）＋`new_toolbox.py` |
| 再次 `azd deploy` | `postdeploy` | 首次安裝已完成（API 存在且 `FA_SESSIONS_PREPARED=true`）才走升級：`ci_deploy.py check`→WebIQ（若開）→`guard/sync/api/console/follow`，跟 stage CI 相同；首次安裝中斷時則照首次路徑接續（每步先讀回） |

## 5. 「跑完就能用」與仍然手動的部分

專案規則：`azd up` 結束就能直接用，預設和作者自己維運的 stage 環境相同。

- **新對話選版改為自動**（原 14、F3）：首次安裝最後由主人身分執行 `select_all_capabilities.py`，模板依伺服器選項與
  `conversation_defaults` 算出（Responses、全部能力、已批准技能），不從其他 Agent 匯出；只做一次，之後不覆蓋主人的調整。
- **私人／Team 記憶**：`new_memory_store.py` 建 `poc-team-memory`（選項同 dev／stage，chat 模型用 `FA_DEFAULT_MODEL`），旗標隨 Agent 第一版發布。
- **Team MCP 含金鑰 connection**：API 身分 project 範圍 Contributor（`infra/azd/api-project-contributor.bicep`）。
- **`azd down --purge`＋`postdown`**：連 RG 外的 Entra app 與自訂角色一起清，quota 釋放。確認是 `predown` 的環境名稱（備份鎖還在、
  沒設 `FA_CONFIRM_DELETE` 時）和 azd 自己的 `y/N`，2026-10-15 實測約 33 分鐘；見 [README 刪除](../deploy/azd/README.md#刪除)。

仍然手動或選用：

| 項目 | 原因 |
|---|---|
| 工作台真人驗收（原 17） | 要真人登入送訊息，以 receipt `completed` 為準；HTTP 200 不算 |
| Provider／feature 註冊、quota 申請 | 這些會改訂閱，preflight 只列出缺什麼和指令 |
| 沒有管理員權限時的 consent | `oauth2PermissionGrant` 要 `DelegatedPermissionGrant.ReadWrite.All`（管理員角色）。部署者有這類角色時 hook 直接建，不需要這一步；沒有時 hook 寫出 3 個只給主人的 grant body 與指令（`.azure/<env>/fa/admin-consent/`），管理員照做，部署停在第一次 session 前等它。完整的事前準備與分工見 [README 事前準備](../deploy/azd/README.md#prerequisites)，`fa_azd.py prereq` 唯讀檢查 |
| Feed reader MCP | 需要另一把簽章金鑰；新環境預設不建 |
| 第二位使用者 | ADR-0006 一 Team 一主人（G1） |

## 6. 批准與安全邊界

- `azd up` 本身就是使用者對自己環境的部署動作；對 FA 自己的 dev／stage 仍照 AGENTS.md 每次批准。
  azd 路徑**不碰** `deploy/stage/*.json` 的任何值，stage CI 不變。
- 兩個寫入點在 hook 裡要 owner 當場輸入確認：首次安裝 apply（plan hash 前 12 碼）與 WebIQ 金鑰。
  非互動（CI、`--no-prompt`）時直接停，不自動批准。
- 每個雲端寫入都先寫 intent（`open("x")`）；有 intent 只讀回，不重送；結果不明就停。
- WebIQ 金鑰只在記憶體，PUT 一次後丟棄；不寫檔、不印、不放命令列或 azd env。

## Verified against

- 程式：`deploy/azd/fa_azd.py`（`preprovision`／`postprovision`／`deploy`／`upgrade`／`fresh_install`）、`entra.py`、`profile.py`、
  `infra/azd/main.bicep`；沿用的 `ci_deploy.py`、`seed_agents.py`、`make_runtime.py`、`run_fresh_job.py`、
  `prepare_native_sessions.py`、`behavior_native_sessions.follow／advance`（首次不 follow 的依據）。
- 官方文件（2026-10-10 讀）：azd [Foundry extension](https://learn.microsoft.com/azure/developer/azure-developer-cli/extensions/azure-ai-foundry-extension)、
  [init-agent-project](https://learn.microsoft.com/azure/foundry/agents/how-to/init-agent-project)、[azure.yaml reference](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)、
  [hooks](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-extensibility)、[layered provisioning](https://learn.microsoft.com/azure/developer/azure-developer-cli/layered-provisioning)、
  [Graph upsert](https://learn.microsoft.com/graph/api/application-upsert?view=graph-rest-1.0)、[Graph Bicep 限制](https://learn.microsoft.com/graph/templates/limitations)、
  [Container Apps Entra 驗證](https://learn.microsoft.com/azure/container-apps/authentication-entra)。
- 官方文件（2026-10-11 讀）：[Hosted Agents 區域](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability)、
  [Agent Service 區域與限制](https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions)、
  [azd Bicep metadata](https://learn.microsoft.com/azure/developer/azure-developer-cli/metadata)、
  [記憶庫](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)；範本對照 Azure-Samples `get-started-with-ai-agents`
  的 `location` 參數（同樣 `@allowed`＋`usageName`＋`${AZURE_LOCATION}`）。
- azd 1.32.0 原始碼（tag `azure-dev-cli_1.32.0`）：`pkg/ext/hooks_runner.go` 每個 hook 前後 `envManager.Reload`；
  `bicep_provider.go` 的 `plan()` 在部署時重新 `ensureParameters`。
- 本機：azd 1.32.0 探針（無 service 時 `postdeploy` 仍執行；`pwsh` hook 的非零 exit 會讓 azd 失敗；沒有 `AZURE_LOCATION`
  時在 hook 之前就停在 provider 初始化）；唯讀 `az cognitiveservices model list -l westus3`（catalog 5 個模型都有
  GlobalStandard；gpt-4.1-mini 標 `Legacy`）。離線測試在 `tests/shared_context/test_azd_hooks.py` 與 `test_azd_profile.py`。
- 雲端：2026-10-10 在作者的訂閱用 `azd up` 建新環境實跑（[progress](progress.md#azd-new-environment-1010)）；10-11 第二個新環境（westus3）從頭跑完，含新對話選版，作者在工作台實際使用回報通過（[progress](progress.md#azd-region-models-1011)）；10-11 第三個新環境（eastus2，區域與模型選擇，只選 gpt-6-luna）`azd up` SUCCESS、部署與設定讀回符合，工作台未驗收（[progress](progress.md#azd-eastus2-1011)）。三組事後都已刪除。
