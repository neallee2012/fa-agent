# FA deployment profiles

The deployment scripts use one environment profile instead of embedding a
subscription, resource group, or endpoint in each script. The checked-in
`deploy/stage/stage.json` remains the default, while another environment can be
selected with `--config` or `FA_DEPLOY_CONFIG`.

<a id="deploying-dev"></a>
## Deploying dev

Neal 2026-10-19 決定：dev（`faexample`，RG `rg-faexample`）改走
`deploy/stage/ci_deploy.py` 加 `deploy/dev/dev.json`，用同一套檢查，取代部署線的 Git 外工具。
**每一個命令都要帶 `--config deploy/dev/dev.json`**：不帶時用 `FA_DEPLOY_CONFIG`，再沒有就是
`deploy/stage/stage.json`，也就是對 stage 寫入。`deploy/dev/` 不進公開匯出（[public-sync](../docs/public-sync.md#不會同步的檔案)），
所以這裡的 `deploy/dev/...` 路徑只寫成文字，不做連結。

**一次性轉換（W1、W2）先做完才能跑 `guard` 以後的步驟**（Neal 2026-10-19 批准，只限 dev）。共同設定：
`$env:AZURE_CONFIG_DIR = "$HOME\.azure-foundry-agents"`。這份隔離設定的預設帳號在另一個 tenant，所以
**每條 `az` 都要帶 `--subscription <dev 訂用帳戶>`、`-g rg-faexample` 和資源的 `-n`**（`az rest` 打 ARM 時，
訂用帳戶寫在 URL 路徑裡）。Foundry 資料面 token 也要 `az account get-access-token --resource https://ai.azure.com --subscription <dev 訂用帳戶>`：
不帶時拿到預設帳號另一個 tenant 的 token，`GET /agents/...` 回 404 `WorkspaceNotFound`；改帶 `--tenant` 會得到 AADSTS90072。

- **W1：建 sync Job `job-fa-agent-sync-dev`。** 只部署
  [`behavior-job.bicep`](../infra/shared-context/behavior-job.bicep) 這一個模板到 `rg-faexample`
  （`az deployment group create -g rg-faexample -n <部署紀錄名稱> --template-file infra/shared-context/behavior-job.bicep --parameters @<私密參數檔> --subscription <dev 訂用帳戶>`），
  不跑 `foundation.bicep`／`behavior-api.bicep`。參數檔放在 repo 外。stage 的
  `behavior-job.stage.parameters.json` 沒有 `commandArguments`、`environmentVariables`、`imageSha256`，不能直接照抄；
  dev 要給的是下面這些（全是資源名稱或讀回值，沒有秘密）：

  | 參數 | dev 的值與來源 |
  |---|---|
  | `jobName` | `job-fa-agent-sync-dev`（和 profile 的 `sync_job` 相同） |
  | `environmentName` | `cae-facontext-private`（profile 的 `container_apps_environment`，也是模板預設） |
  | `identityName` | `id-facontext-api`（profile 的 `api_identity`，也是模板預設） |
  | `registryName` | `crfacontextpocdemo`（profile 的 `registry_name`，也是模板預設） |
  | `imageRepository` | `fa-behavior-api` |
  | `imageSha256` | 目前 dev API image 的 64 位 digest：`az containerapp show -g rg-faexample -n ca-fa-behavior-poc --subscription <dev 訂用帳戶> --query properties.template.containers[0].image` 讀回，取 `@sha256:` 後面那段 |
  | `commandArguments` | `["-B","-c","print('job-fa-agent-sync-dev placeholder', flush=True)"]`（佔位；`ci_deploy` 每次 start 都會換掉 image、args 和 env） |
  | `environmentVariables` | `[{"name":"PYTHONUTF8","value":"1"},{"name":"PYTHONDONTWRITEBYTECODE","value":"1"}]`（和 stage 讀回相同） |
  | `timeoutSeconds` | `1800` |
  | `containerResources` | `{"cpu":"0.5","memory":"1Gi"}` |
  | `tags` | `{"workload":"hosted-sandbox-poc","environment":"dev"}` |

  寫入前先確認 `az containerapp job show -g rg-faexample -n job-fa-agent-sync-dev --subscription <dev 訂用帳戶>` 是 NotFound，
  再 `az deployment group what-if`（同樣的 `-g`、模板與參數檔），只能有 1 筆 Create；寫入後用同一條 `job show` 讀回
  （Manual、1800、retry 0、UAMI 只有 `id-facontext-api`、`Consumption`），不 start。
  不需要新的 IAM（Job 用 API 的 UAMI，它已有 AcrPull）。
  **退回**：Job 是新建的，閒置不計費，所以預設是留著。真要退，只刪這一個新建的 Job
  （`az containerapp job delete`，要 Neal 先批准刪除），而且 Job 重新存在之前**不能再用 `ci_deploy` 部署 dev**：
  `sync_job` 是 profile 必填欄位（[`deployment_config.py`](shared-context/deployment_config.py)），把它從 profile 拿掉，
  每個命令連 profile 都載入失敗。
- **W2：dev API `ca-fa-behavior-poc` 改用 `serve` 啟動。**
  **W2-0 先決條件**（任何一項不成立就不寫入）：(1) `az containerapp revision list -g rg-faexample -n ca-fa-behavior-poc --subscription <dev 訂用帳戶> --all`
  只有 `ca-fa-behavior-poc--ae3d-v157-nofault` 是 active；(2) 沒有回合在跑，挑安靜的時段（先看工作台和 routine 的時間）；
  (3) PATCH 前的完整 GET JSON 已存到 repo 外（`az rest --method get --url https://management.azure.com/subscriptions/<dev 訂用帳戶>/resourceGroups/rg-faexample/providers/Microsoft.App/containerApps/ca-fa-behavior-poc?api-version=2025-01-01`）。
  image 不換，只把 GET 回來的 template 改成
  `command ["python"]`、`args ["-B","-m","shared_context.behavior_server","serve"]`，加一項
  `BEHAVIOR_SERVER_CONFIG_JSON`＝`deploy/dev/runtime/config.json` 的內容（和原內嵌配置逐位元組相同），
  拿掉 `imageType`、`ephemeralStorage`，`revisionSuffix` 用**正在服務的 commit** 前 7 碼加 `-r0-1`
  （目前是 `ae3d30e-r0-1`；不是 checkout 的 HEAD，這樣 `guard` 讀得出服務中的 commit），再對同一個 URL `az rest --method patch`。
  寫入後讀回 revision Healthy、traffic 100%、`/healthz` 200、ENV 等於 `runtime/config.json`。
  **退回**：`az containerapp revision activate -g rg-faexample -n ca-fa-behavior-poc --subscription <dev 訂用帳戶> --revision ca-fa-behavior-poc--ae3d-v157-nofault`；流量沒回去時用
  `az containerapp revision copy -g rg-faexample -n ca-fa-behavior-poc --subscription <dev 訂用帳戶> --from-revision ca-fa-behavior-poc--ae3d-v157-nofault --revision-suffix <新後綴>`（舊 image 留在 ACR）。
  **切換後的行為差異**：原啟動腳本另帶的成本觀測檔（`FA_WORK_COST_OBSERVATION_FILE`）不再存在，所以舊的 job-based
  指令 lane（沒有 VM ticket）回 403 `WORK_POLICY_COST_OBSERVATION_SCOPE`，「失敗回合移到新環境」回 403
  `CONVERSATION_TRANSFER_BUDGET`。這和 stage 相同，是總協調 2026-10-19 的決定；native（VM 內）回合、follow 不受影響。

**`gate`／`sync` 之前的人工檢查**（`ci_deploy` 不檢查 Job 是否存在或是否正在跑，Job 不存在時 `build` 已經把 image 推上 ACR 才會失敗）：
`az containerapp job show -g rg-faexample -n job-fa-agent-sync-dev --subscription <dev 訂用帳戶>` 要讀得到，
`az containerapp job execution list -g rg-faexample -n job-fa-agent-sync-dev --subscription <dev 訂用帳戶>` 不能有 `Running` 的 execution。
這是文件步驟，沒有加進 `ci_deploy`（`guard` 也是 stage CI 在用，多一次讀取要另外評估）。

就緒後的順序（PowerShell，完整 checkout 的根目錄；dev 部署照 [ADR-0008](../docs/adr/0008-environment-tiers-staging-cicd.md) 的 dev 規則批准）：

```powershell
$env:AZURE_CONFIG_DIR = "$HOME\.azure-foundry-agents"; $env:PYTHONUTF8 = '1'
# 本機沒有 GitHub 的這三個值：tag 與 revision 名稱 <commit>-r<run>-<attempt> 由它們組成，run 每次要不同
$env:GITHUB_SHA = git rev-parse HEAD; $env:GITHUB_RUN_NUMBER = '<這次的編號>'; $env:GITHUB_RUN_ATTEMPT = '1'
$cfg = 'deploy/dev/dev.json'
python -X utf8 deploy/stage/ci_deploy.py --config $cfg check
python -X utf8 deploy/stage/ci_deploy.py --config $cfg guard
$api    = python -X utf8 deploy/stage/ci_deploy.py --config $cfg build api    | Select-Object -Last 1
$hosted = python -X utf8 deploy/stage/ci_deploy.py --config $cfg build hosted | Select-Object -Last 1
python -X utf8 deploy/stage/ci_deploy.py --config $cfg gate $api
python -X utf8 deploy/stage/ci_deploy.py --config $cfg sync $api $hosted
python -X utf8 deploy/stage/ci_deploy.py --config $cfg api $api
python -X utf8 deploy/stage/ci_deploy.py --config $cfg follow $api $hosted
```

- `build` 用本機 Docker（`linux/amd64`）build 後 push，不是部署線的 `az acr build`。
- `--verify-follow` 不用自己加：profile 的 `FA_NATIVE_SESSION_GUARD_ENFORCE` 是 `"1"`，`sync` 自動帶
  `--defer-follow`，`follow` 自動帶 `--verify-follow`（只回報各 Agent 版本與環境，不 follow）。
- Teams relay（`src/fa-teams-relay`）**不在** `ci_deploy` 裡（`build` 只有 api／hosted／console／feed），照
  [relay README](../src/fa-teams-relay/README.md) 另外做。
- dev 沒有遠端工作台（`console_app` 是 `null`），不跑 `console`；`feed` 視需要在 `follow` 之後跑。
- 最後重啟本機 8765 工作台：停掉原 bridge，用**原本的**配置、journal 與 SHA 重開
  （[`serve` 用法](../src/fa-shared-context-poc/web_test_console/README.md#多-agent-配置與交接codeoffline)，
  多 Agent 時加原 `--agents` manifest），再按「連線」。

## Prepare a new environment

**New subscription or new environment: use `azd up`** ([deploy/azd/README.md](azd/README.md),
design in [docs/azd-new-environment.md](../docs/azd-new-environment.md)). It runs the steps below through
the same scripts and Bicep. The rendering in this section stays for re-running an environment that was
deployed by hand before azd, whose names follow `prepare_environment.py` (`rg-fa-agent-v18` was one; it was
deleted on 2026-10-10).

Create a small input file from
`deploy/shared-context/environment.example.json`, then render the Bicep
parameter bundle without contacting Azure:

```powershell
python .\deploy\shared-context\prepare_environment.py `
  --input C:\fa-inputs\v18.json `
  --out C:\fa-inputs\v18
```

The command is deterministic and refuses to overwrite different bytes. It
creates `deployment.json` plus parameter files for Foundry, the shared
monitoring, foundation, API, sync job, Web PubSub, backup, and artifacts. It
does not log in, create resources, assign roles, or read secrets.

Before infrastructure deployment, complete the subscription checks below.
Deploy the Foundry account/project first, then deploy monitoring and read back
the workspace, Application Insights component, and project connection before
deploying the foundation that references the workspace. After the foundation
creates the environment, verify ACA platform readiness before deploying API/Job
resources or starting executions; offline parameter rendering does not check
Azure prerequisites. Use the subscription and resource group from `deployment.json`.
After Azure returns the project endpoint,
managed-identity client ID, API FQDN, and Hosted Agent IDs, assemble the complete
runtime profile. The profile is the only input needed by the operational scripts.

The input also carries three options learned from the 2026-10-07 deployment:

- `api_subnet_nat_egress` (default `false`): stage and dev have no NAT on the
  API subnet. `true` adds an AVM NAT gateway with its own Public IP and sets
  `defaultOutboundAccess=false` on both subnets. An environment that already
  has this NAT must keep `true`; a foundation rerun with `false` detaches it.
- `model_capacity`: the Foundry account now deploys every entry of
  `src/fa-shared-context-poc/model-catalog.json` (the Portal model menu plus the
  embedding model). Capacity defaults to the catalog value; this map
  (`{"gpt-5.4-mini": 500}`) overrides it per deployment. Names outside the
  catalog are rejected.
- `model_version_upgrade_option`: `NoAutoUpgrade` (default) for all deployments,
  another option for all, or a deployment-name map that keeps an existing
  environment's per-deployment setting.

<a id="new-environment-standard-flow"></a>
## 新環境標準流程與完整問題清單（2026-10-07 部署，10-08 盤點）

**2026-10-10 起，新環境改用 [`azd up`](azd/README.md)**：下面 17 步已對應到 azd 的 hook 與 Bicep
（[對照表](../docs/azd-new-environment.md#4-原-17-步對應到哪裡)），hook 呼叫的就是這裡列的腳本。
本節保留為逐步排錯與既有手動環境重跑的對照，不再當成第二套新環境流程維護。

新訂閱首部署照下列順序做；每一關讀回通過才進下一關，任何未知結果先讀原紀錄，
不換 ID 重送。標「批准」的步驟是雲端寫入，每次都要先取得涵蓋目標與後果的批准。
括號內代號對應下方[完整問題清單](#new-environment-problem-inventory)。

1. **登入與目標核對（唯讀）**：每個 shell 設隔離的 `AZURE_CONFIG_DIR`，
   `az login --tenant <tenant>`，每個資源命令都帶 `--subscription`。用 Graph `/me`
   讀 UPN 與 OID，和 runtime 的 Team 主人 OID 比對；不要相信口頭帳號拼字（A1、A2）。
   Windows 上設 `PYTHONUTF8=1`，腳本以 CLI 自帶的 `python.exe -X utf8 -IBm azure.cli`
   呼叫 Azure CLI（H1、H2）。
2. **Render（離線）**：`prepare_environment.py --input … --out …`。重跑既有環境時，
   input 要寫明現場名稱、NAT 與模型設定（見本節最後的重跑輸入）。
3. **Subscription 前置（唯讀）**：
   `python .\deploy\shared-context\preflight_subscription.py subscription --deployment <out>\deployment.json`。
   檢查 tenant／subscription、`Microsoft.Network/AllowBringYourOwnPublicIpAddress`、
   9 個必要 resource provider（[清單](#new-environment-provider-precheck)）、每個模型部署在該區域的
   quota（同訂閱同區域的帳號共用）；缺 quota row 且仍需增加容量時會阻擋，已部署容量足夠、
   `needed=0` 時不阻擋；`Microsoft.AlertsManagement` 未註冊只列警告（A3、A4、B1）。
   缺 feature 時另批註冊，註冊後再跑一次 `az provider register -n Microsoft.Network`，
   然後重跑這個檢查；**全部通過才建第一個 CAE**，CAE 建失敗後不要直接重跑 deployment（B1、B2）。
4. **Foundry 與監控（批准；先於 foundation）**：先用 `foundry.parameters.json` 部署
   Foundry account/project，再用 `monitoring.parameters.json` 部署
   `infra/shared-context/stage-monitoring.bicep`。以唯讀 ARM GET 讀回目標 RG 的
   Log Analytics workspace 與 Application Insights component，以及 Foundry RG 內
   project connection；名稱、location、connection target 都須與產生的 parameters 相符。
   三項都確認存在且成功後才能部署 foundation。監控 workspace 是 foundation
   診斷設定的實際目的地，不能只因 parameters 已產生就略過讀回。
5. **Foundation（批准）**：用 `foundation.parameters.json` 先 what-if 再部署。
   what-if 會拿掉 Bicep 沒宣告的現場資源（例如 NSG 關聯）時先停下（B5）。
6. **ACA 平台就緒（唯讀）**：`preflight_subscription.py aca-ready --deployment …`。
   CAE 必須 `Succeeded` 且有 `staticIp`，infrastructure RG 必須有平台 LB 和同一個 IP 的
   Public IP。不通過就不要部署 API／Job。補 feature 後同設定原地 reconcile 本次無效；
   改成只刪失敗的 App／Job／CAE、直接 GET 確認 `ResourceNotFound`、subnet 的
   service association link 已釋放，再用新名稱重建 CAE（B1–B3）。
7. **Entra API app（批准）**：每個 tenant 各建自己的 API app／audience
   （v1 token，只公開 `Behavior.Read`／`Behavior.Approve`）。目標 tenant 沒有 Azure CLI
   第一方 client 的 SP 時補建，只對 Team 主人做 `Principal` consent；用真 token 核
   tenant／oid／aud／scp，token 不保存（D1、D2）。
8. **映像、API、sync Job（批准）**：image 用 digest；`--core` 用 code identity（C4）。
   Windows 打包要先把檔案模式改成 0644／0755（C2）；本機 `az acr build` 的結束碼
   不可信，以 ACR run 讀回為準（C3）。Job identity 只走 Bicep／AVM（C1）。
   API 前先跑一次無模型 Job smoke：image pull、container start、exit 0（C6）。
9. **Profile 讀回（唯讀）**：private profile 的每個 ARM ID（API、Job、CAE、
   Log Analytics）都 GET 一次；Agent ID／OID 換成實際讀回值，不留 placeholder。
   只改 profile，不改 runtime 原始 bytes（C7）。
10. **首次安裝（批准）**：`run_fresh_job.py plan` 後再 `apply`。本機有配置檔不代表
   Table 已初始化（D4）。
11. **Seed、sync、route、follow**：用既有腳本，未知結果不重送。從沒準備過的 Agent 不要 follow
    （沒有舊環境可跟，回 `NATIVE_FILES_CONFIRMATION_REQUIRED`，2026-10-10 azd 實跑）；先做第 13 步的首次準備，
    之後換版才 follow。Hosted definition
    內含 API URL 與 audience；任一項改變都要發布新的 immutable 版本並把 route 設為
    100%，舊版本保留（D3、D5）。
12. **Agent project 角色（批准）**：每個 Agent identity 用 `project-service-access.bicep`
    給 project 的 Foundry User 和 OpenAI User，兩個都給（F1）。API MI 的「＋」委派用
    `stage-onboarding-delegation.bicep`（ABAC 受限 RBAC Admin）；stage 另有 project
    Contributor，目前不在 Bicep 中，要對齊時另批。
13. **首次 session 準備（批准）**：
    `prepare_native_sessions.py --config <profile> --out <私人目錄> --request-id <新ID>`，
    先 `--dry-run`。讀回 `state=ready`、`model_started=false` 才算完成（E2）。
14. **新對話選版（批准）**：azd 路徑自 2026-10-10 起由 postdeploy 以主人身分自動做「全開」選版（Neal 決定，見
    [azd 設計 §5](../docs/azd-new-environment.md#5-跑完就能用與仍然手動的部分)）；以下是手動對照。
    `select_all_capabilities.py --config <profile> --keep-capabilities --out … --request-id …`，
    先 `--dry-run`，把已選版 Agent 的新對話設定對齊該 profile 的
    `conversation_defaults`（Responses lane）。Fresh scoped Agent 尚未選版時，不從
    `/agent-template` 隱式匯出預設；Team 主人先明確挑選／檢視一份
    `fa-agent-template-v1` JSON，再於 dry-run 和獲准的實際 `/select` 呼叫都指定
    `--initial-template <owner-reviewed-template.json>`。既有 endpoint 仍驗證目前 owner
    的批准權限及精確 capability／skill source refs；模板檔與 dry-run 都不代替批准，
    也不會自動複製其他 Agent 的設定。未知回應沿原 request ID reconcile，不換 ID 重送。
    選版只影響新對話，舊對話不會搬（F2–F4）。
15. **Portal app（批准）**：目前 repo 沒有建立 Portal app 的 Bicep；`ci_deploy.py console`
    只更新已存在的 app。照[19:00 讀回](#v20-portal-readback)逐項建立：Easy Auth 只允許
    Team 主人、UAMI FIC（沒有 client secret）、UAMI 只有 `AcrPull` 和 `journal`／
    `easyauth-tokens` containers 的 Blob Data Contributor、API 加
    `FA_REMOTE_CONSOLE_CLIENT_ID`。讀回匿名瀏覽器 request 302 到目標 tenant 登入（E1）。
16. **WebIQ toolbox（選用，批准）**：`new_webiq_connection.py`（2026-10-10 取代同格式的
    `new-webiq-connection.ps1`）＋`new_toolbox.py`，兩支都接受 `--config`；
    金鑰由 Neal 隱藏輸入。連線已存在時只讀回、不再問金鑰；要換過期或外洩的金鑰，
    在終端機執行 `python deploy/stage/new_webiq_connection.py --rotate`（azd 環境加
    `--config .azure/<env>/fa/profile.json`；不加 `--config` 會用 `FA_DEPLOY_CONFIG` 或 `deploy/stage/stage.json`，也就是 stage；
    需要 `httpx` 與 `azure-identity`，可用 deploy 建好的 `.azure/<env>/fa/venv`，或 `pip install -r src/fa-shared-context-poc/requirements.txt`）：
    先唯讀確認連線已存在（不存在就停下、什麼都不寫；第一次建立走不加 `--rotate` 的正常流程），
    再隱藏輸入新金鑰、對同一條連線做一次 PUT、讀回要 200。同一條連線的 PUT 是冪等的，結果不明時重跑 `--rotate` 是安全的。project 沒有 `fa-org-tools` 時，不要讓 Agent 選 WebIQ toolbox（F6）。
17. **Portal 驗收**：真人登入、開新對話、送短訊息；以 receipt `completed`、
    `failure_code=null`、用量已計量為準。HTTP 200、「執行中」或畫面有字都不算。

<a id="new-environment-provider-precheck"></a>
### 部署前要先檢查的 resource provider／feature

範圍是 `infra/shared-context` Bicep 實際部署的 namespace，以及部署時 Azure 自動附帶建立的資源。
10-07 狀態取自目標訂閱的 Activity Log（10-08 唯讀重讀，時間為 UTC）。
`preflight_subscription.py subscription` 會逐項唯讀檢查；註冊會改訂閱，要另外批准。

| Provider／feature | FA 用在哪裡 | 10-07 新訂閱 | preflight |
|---|---|---|---|
| feature `Microsoft.Network/AllowBringYourOwnPublicIpAddress` | ACA 平台為 CAE 建 LB 和 Public IP `capp-svc-lb-ip`（也就是 `staticIp`） | **未註冊**。01:19:44–01:20:33 平台 Public IP 寫入 6 次 `SubscriptionNotRegisteredForFeature`；01:20:40 第一次建 CAE `Failed`；01:21:10 才註冊；約 23 秒後重跑同名 foundation deployment（ARM 只留最後一次，失敗那次的紀錄被覆蓋），01:23:50 同一個 CAE 變成 `Succeeded`，但平台沒有再寫 Public IP，所以 `staticIp=null`（B1、B2） | 不通過就失敗 |
| `Microsoft.Network` | VNet、subnet、private endpoint、private DNS；opt-in 時加 NAT 和 Public IP | 已註冊；為了讓 feature 生效重新註冊兩次（01:21:20；07:01:09 經批准的修復），之後舊 CAE 仍要刪掉重建 | 不通過就失敗 |
| `Microsoft.SignalRService` | Web PubSub（`realtime.bicep`） | **未註冊**；01:28:43 由 operator 註冊 | 不通過就失敗 |
| `Microsoft.Insights` | Application Insights（`stage-monitoring.bicep`）、各資源的 diagnostic settings | 已註冊（07-17 由同訂閱其他使用者註冊） | 不通過就失敗（10-08 補進清單） |
| `Microsoft.App` | CAE、API／Portal app、sync Job | 已註冊 | 不通過就失敗 |
| `Microsoft.CognitiveServices` | Foundry account／project、模型部署、connection | 已註冊 | 不通過就失敗 |
| `Microsoft.ContainerRegistry` | ACR、`az acr build` | 已註冊 | 不通過就失敗 |
| `Microsoft.Storage` | Table、Blob（狀態、備份、artifact） | 已註冊 | 不通過就失敗 |
| `Microsoft.ManagedIdentity` | 各個 UAMI | 已註冊 | 不通過就失敗 |
| `Microsoft.OperationalInsights` | Log Analytics workspace | 已註冊 | 不通過就失敗 |
| `Microsoft.AlertsManagement` | App Insights 自動建的「Failure Anomalies」smart detector；不在 Bicep 中 | 未註冊，維持原狀；該附帶步驟 `MissingSubscriptionRegistration`，FA 部署本身成功（A4） | 只警告；要那個告警再另批註冊 |

不用檢查：`Microsoft.Authorization`、`Microsoft.Resources` 不需要註冊；
`Microsoft.MachineLearningServices` 在這個訂閱維持 `NotRegistered`，Foundry project 和 Hosted Agent
都正常，FA 不需要它。Activity Log 每 12 小時出現一次的 `Microsoft.EventGrid` 註冊來自 tenant 自動化
（appid `00000000…`），不是 FA。

<a id="new-environment-problem-inventory"></a>
### 完整問題清單

來源：本次工作階段紀錄、[progress 10-07](../docs/progress.md#fa-new-subscription-v20-1007)、
目標 RG 的 Activity Log（10-07 全天、307 筆失敗事件，10-08 唯讀重讀）。
「已修」指已寫進 repo；「流程」指靠上方步驟避免；「非 FA」指查明不是 FA 造成。

**A. 帳號、訂閱與 CLI**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| A1 | 指定帳號 `owner@contoso.net`，目標又寫 `contsoo.com` | 實際 UPN 是 `owner@contoso.com`，OID 與 runtime owner 相同 | 流程 1：先讀 `/me`，拼字差異不是加帳號或放寬 allowlist 的理由 |
| A2 | Memory Store／project 回 `Workspace not found`／404 | CLI 預設訂閱錯，或 token audience 不是 Foundry | 流程 1：隔離 `AZURE_CONFIG_DIR`、帶 `--subscription`，Foundry data-plane 用 `az rest --resource https://ai.azure.com` |
| A3 | quota 查不到 gpt-4.1 | usage 名稱是 `OpenAI.GlobalStandard.gpt4.1-mini`（沒有連字號） | 已修：preflight 以多種名稱比對 |
| A4 | Activity Log `MissingSubscriptionRegistration`（`Microsoft.AlertsManagement`） | App Insights 建「Failure Anomalies」smart detector 時需要這個 provider；FA 本身不需要 | 已修：preflight 只警告；要那個告警時另批註冊 |
| A5 | 10-07 有 282 次 `listKeys` 因 `disableLocalAuth=true` 被拒 | 全部來自第一方 appid `00000000…`（tenant 內沒有這個 SP、不是 az CLI），時段與 operator 瀏覽 portal 吻合；判定是 Azure／Foundry portal 介面輪詢 | 非 FA：FA 全程 Entra，不開金鑰；14:38 的一次 `RequestConflict` 是同時在改模型部署 |

**B. ACA 平台與網路**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| B1 | CAE `Succeeded` 但 `staticIp=null`、infrastructure RG 空；API 兩次 `Failed to provision revision … Error details: .`（每次約 20 分鐘）、零 revision；v20 sync Job pod `BackoffLimitExceeded`／`ContainerNotFound`；診斷 image 更新卡 `InProgress` | 訂閱沒註冊 `AllowBringYourOwnPublicIpAddress`；平台 Public IP `capp-svc-lb-ip` 寫入 6 次 `SubscriptionNotRegisteredForFeature`，第一次建 CAE `Failed`；feature 在失敗後才註冊，隨即重跑同名 foundation deployment，把同一個 CAE 標成 `Succeeded` 卻沒補建平台網路（時間線見[前置清單](#new-environment-provider-precheck)） | 已修：preflight `subscription`／`aca-ready` 兩關 |
| B2 | 補 feature 後原地 reconcile `fa-v20-aca-reconcile-20261007` 成功，`staticIp` 仍 null；匯出 template 的 what-if 還想改 diagnostic settings 與 peer authentication | 已建壞的 CAE 不會因同設定更新而補建平台資源 | 流程 5：不重複 reconcile，改刪失敗運算後重建；what-if 有非預期變更就保留現場值 |
| B3 | 清理時：整個 RG 刪除會連帶刪 Foundry、Table／Blob、ACR；CAE `ScheduledForDelete` 約 20 分鐘；命令尾端 JMESPath 經 `az.cmd` 解析失敗 | Windows `az.cmd` 的引號處理；CAE 刪除是非同步 | 流程 5：只刪失敗的 App／Job／CAE，用直接 GET 確認；service association link 釋放後原 subnet 可重用，新 CAE 換名稱，App／Job 沿用名稱、ARM ID 與 UAMI |
| B4 | 懷疑 NAT／`defaultOutboundAccess=false` | 不是主因；stage／dev 都沒有 NAT | 已修：NAT 改成 opt-in `api_subnet_nat_egress`，預設關 |
| B5 | 兩個 subnet 掛著 Bicep 沒宣告的 NSG；15:05 一度讀到「未綁 NSG」 | 10-07 03:14／03:15 UTC 由 tenant 治理自動化（appid `00000000…`）建立並掛上；06:11 operator 改 VNet（NAT 實驗）時關聯被拿掉，07:13 同一自動化又掛回 | 流程 4：重跑前 what-if；要拿掉 NSG 就停下，不和治理自動化互相覆寫；stage／dev 也有同名 NSG |

**C. 映像、Job、診斷與 profile**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| C1 | 手動 PUT Job 回 `InvalidIdentityValues` | `userAssignedIdentities` 的值必須是 `{}`，不能回送 GET 的 `clientId`／`principalId` | 流程 7：一律用 Bicep／AVM |
| C2 | desktop image build 的「UID10001 不可寫」檢查失敗 | Windows `az acr build` 打包成 0666／0777 | 流程 7：打包前檔案改 0644、目錄改 0755（見 [Shared Context README](../src/fa-shared-context-poc/README.md) 的「stage 環境與 CI/CD」）；Linux CI 用 docker，不受影響 |
| C3 | 本機 `az acr build` exit 1，但遠端 run 已 `Succeeded`；`listLogSasUrl` 回 `NoRegisteredProviderFound`，log 串流遇 cp1252 中斷 | 本機 CLI 讀 log 失敗，不是 build 失敗 | 流程 7：用 `az acr task list-runs`／`show-run` 讀回，不盲目重建 |
| C4 | `fa_build` 填了 OCI digest；Hosted seed 把 `container_protocol_versions: []` 和省略當成不同 | 腳本缺陷 | 已修（#721）：`--core` 用 code identity；seed 把空清單視同省略 |
| C5 | 診斷 image `busybox` 404／`MANIFEST_UNKNOWN`（`InvalidParameterValueInContainerTemplate`）；CLI `--args` 把多個參數併成一個；ACR `--cmd` 接在 entrypoint 後面 | MCR 路徑不對；CLI 行為 | 診斷用自己 ACR 的 digest image，Job 參數用 ARM `start` body |
| C6 | Job 結束後 console log 讀不到，system log 有 | console log 進 Log Analytics 較慢，replica 已消失 | 等 Log Analytics，不重跑 smoke、不改 logging |
| C7 | profile 指向尚未建立的 Job、已刪除的 App；Agent ID／OID 與 Log Analytics workspace 是 placeholder | 初版 profile 在 API 失敗前產生，沒讀回 | 流程 8：每個 ARM ID 讀回，填真值 |

**D. Entra、API 與首次安裝**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| D1 | `AADSTS500011` | 新 tenant 沒有自己的 API app／audience | 流程 6 |
| D2 | 目標 tenant 用 Azure CLI 取 FA API token 失敗 | 新 tenant 沒有 Azure CLI 第一方 client 的 SP，也沒有 consent | 流程 6：補 SP，只對 Team 主人做 `Principal` consent |
| D3 | Hosted 仍呼叫舊 API URL／舊 audience | Hosted definition 帶這兩個值，換 API 或 audience 後舊版本不會變 | 流程 10：發布新版本（本次 v2 改 URL、v3 改 audience），route 100%，v1／v2 保留 |
| D4 | API `CrashLoopBackOff`、`/healthz` timeout，factory 回 `BEHAVIOR_NOT_INITIALIZED`（早期 v18 是 `BEHAVIOR_READ_FAILED`） | Table 從未首次安裝；本機有 runtime 配置不等於已安裝 | 流程 9：`plan` 核全表為空，主人批准精確 plan 後 `apply` 一次 |
| D5 | `ci_deploy.py guard` 無法用在首次部署 | 它要從既有 ready revision 推導 serving commit | 首次部署走 operator 路徑；有 ready revision 後才用 CI 升級流程 |
| D5a | `guard` 說 serving commit 不在這個 checkout 的歷史裡 | 正在服務的 revision 來自後來被 rebase 或丟掉的分支 | `azd deploy` 可在那一次加 `FA_ALLOW_ROLLBACK=true`（[azd README](azd/README.md#之後更新)）。**stage CI 不認這個開關**，維持嚴格停止，由 Neal 決定 |

**E. Portal 與首次使用**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| E1 | Portal 需要手動拼裝 | 建立 Portal app 不在 repo 的 Bicep／腳本 | 流程 14；列為待補 |
| E2 | 第一則訊息 409 `NATIVE_SESSION_CHANGED`；原訊息後來讀回 404 `NATIVE_RID_MISSING` | seed 的 A／B 沒做首次 `/prepare`；送訊息不會隱式準備 | 已修：`prepare_native_sessions.py`（流程 12）；原訊息不補造、不重送 |
| E3 | 串流先回 409 `NATIVE_STREAM_NOT_STARTED` | 設計中的暫時狀態 | 非問題：console 自動重試 |
| E4 | C／D 顯示 `AGENT_SCOPE_MISSING` | Portal 只載入已批准的 A／B | 非問題：不為此擴權 |

**F. 模型與對話**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| F1 | 對話回 401 `PermissionDenied`；只補 OpenAI User 後約 2 分鐘內仍失敗 | 真正根因是 F2 的 completions lane（同身分改走 Responses 就成功），不是缺 IAM | 流程 11 仍給兩個角色，以對齊 stage 基線；401 不要先當成 IAM 問題 |
| F2 | gpt-6.1-sol 400 `reasoning_effort` | A 從未選版、B 的手寫選版是 completions，新對話走 completions lane | 已修：`select_all_capabilities.py --config --keep-capabilities`；舊對話仍失敗，要開新對話 |
| F3 | B 的 template export 409 `AGENT_TEMPLATE_PROFILE_UNSUPPORTED` | registered B 刻意不套 server default，必須由主人明確選版 | 流程 14：fresh Agent 要求 owner 明確提供 template，再由既有 `/select` 驗權限與 refs；不隱式繼承或放寬預設 |
| F4 | 用非部署 commit 跑全能力選版回 422 | role digest 由本機 checkout 計算 | 用 `--keep-capabilities`，或從部署的 commit 執行 |
| F5 | Portal 選單有模型但沒有部署 | Bicep 只部署 gpt-4.1-mini 和 embedding | 已修：模型部署由 `model-catalog.json` 自動產生，可覆寫容量／升版選項 |
| F6 | `fa-org-tools` toolbox GET 404 | 新 project 沒建 WebIQ connection／toolbox | 流程 15（選用）；目前 role 仍 v1、capabilities 空 |
| F7 | 失敗回合留下「未量測」請求 | 模型被拒前已送出請求 | 照實保留，不清零、不重送 |

**G. 多使用者**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| G1 | 第二位使用者（另一位測試使用者）403；只放寬 Easy Auth 會變成 API 409 `TEAM_RUNTIME_NOT_SERVED` | Easy Auth 只允許 Team 主人；一個 Team 只有一位主人（ADR-0006，程式強制） | 另一位使用者需要自己的 Team 與獨立 runtime（或未部署的 T6）；使用者決定先不處理 |

**H. 本機工具（Windows operator）**

| # | 症狀 | 根因 | 處理 |
|---|---|---|---|
| H1 | `ci_deploy.py` 在 Windows 起不了 `az`；request body 寫到 `/tmp`；從部分 checkout 匯入就失敗 | `az` 是 `.cmd` shim；硬寫 `/tmp`；模組載入時就讀 profile | 已修前兩項（CLI module、系統 temp）；請從完整 checkout 執行並用 `--config`／`FA_DEPLOY_CONFIG` |
| H2 | Bicep、`print`、ACR log 出現 cp1252 `UnicodeEncodeError` | Windows 預設編碼 | `PYTHONUTF8=1`、`-X utf8` |
| H3 | SDK log stream `ServiceResponseTimeoutError` | 一次讀太大 | 改小段串流讀取 |
| H4 | Git 外 helper：log 解析 regex 假設欄位相鄰、把 steady-v2 當 `StandingConsoleConfig`、讀 raw dict 遇 `KeyError` | helper 自己的錯誤 | 改用欄位白名單與 `AgentTemplate` DTO；正式流程改用 repo 腳本 |
| H5 | `gh api` 讀 AVM repo 回 SAML 403 | 組織 SSO | 用公開 tag／MCR 核對版本 pin |

**仍未確認（不影響本次結果）：**

- 診斷 image `ds5` 失敗且沒有 log，`ds6` 成功；原因未查（只用於診斷，未進 production）。
- 治理自動化 appid `00000000…` 在 tenant 查不到 SP、也不是 subscription policy identity；
  只能判定不是 FA、Bicep 或 operator。
- API 兩次 revision 失敗的 `Error details` 是空的；判定 B1 根因靠平台 Public IP 寫入失敗，
  不是 App 本身的錯誤訊息。
- 角色指派要多久生效沒有量測；F1 的 2 分鐘只說明當時未轉綠。
- gpt-5.4-mini、gpt-6-luna 尚未在 Portal 實測；gpt-6-luna 在 Responses 有已知平台問題（stage 相同）。

**尚未寫進 repo（下次仍要人工做或另批）：** Portal app 建立（E1）；Windows 打包的
檔案模式正規化腳本（C2）；本次 sync／audience 改版／factory 診斷用的 Git 外 recovery Job
helper（D3、D5）；API MI 的 project Contributor（流程 11）；WebIQ toolbox 與 role v4（F6）；
第二位使用者的 Team（G1）；`Microsoft.AlertsManagement` 註冊（A4，選用）。

**本環境（`rg-fa-agent-v18`）重跑的必要輸入（22:5x 唯讀核對現場）：**

- `api_subnet_nat_egress: true`：現場 `snet-api` 掛著 `vnet-facontext-api-nat`，兩個 subnet 都是
  `defaultOutboundAccess=false`，與 `natEgressSubnets` 相同。
- `model_capacity: {"gpt-5.4-mini": 500}`；`model_version_upgrade_option` 用 map，
  `gpt-5.4-mini`、`gpt-6-luna`、`gpt-6.1-sol` 為 `OnceNewDefaultVersionAvailable`，其餘 `NoAutoUpgrade`。
- 名稱跟預設不同，要明確寫在 `names`：`vnet: "vnet-facontext-api"`（原 foundation 沒傳
  `vnetName`，用了 Bicep 預設）、`backup_private_endpoint: "pe-fa-v18-backup-blob"`、
  `container_apps_environment: "cae-fa-agent-v20"`。
- 兩個 subnet 各掛一個 `vnet-facontext-api-<subnet>-nsg-westus3`，Bicep 沒有宣告，
  由 tenant 治理自動化建立並維持（B5）。重跑前先 what-if，確認 VNet 更新不會拿掉它們。

少了任何一項，foundation／Foundry 重跑都會改到現場或另建資源；重跑前先 render，逐一對照現場名稱。

<a id="aca-platform-recovery-plan"></a>
## 新訂閱 v20 部署修訂：先恢復 ACA 平台

**2026-10-07：Portal 目標已達成。** 22:2x 把 A／B 的新對話改走 Responses lane 後，Neal 在真 Portal 的新對話正常回覆，receipt 都是 `completed`。部署教訓與流程改動見[新環境標準流程](#new-environment-standard-flow)；以下保留當時的修復紀錄。目標仍是 subscription
`00000000-0000-0000-0000-000000000018`、tenant
`00000000-0000-0000-0000-000000000019`、West US 3、
`rg-fa-agent-v18`；資源名稱的 `v18` 不代表必須執行 v18 映像。
沿用現有 Foundry project、Agent identity、Storage、UAMI、已保存的 runtime
配置與原操作紀錄。16:42 +08 的實際啟動診斷發現這份配置的 authority guard
尚未初始化；**本機配置檔存在不是已完成安裝的證據**，不能跳過首次安裝，
也不能據此假定整個 Table 為空或覆寫既有資料。既有 Agent 不重 seed。
本節是人工操作順序，**不是已接線的自動 preflight，也不是後續 Azure 寫入批准**。

### 首次安裝與接續紀錄

使用者 16:01 +08 以「go」批准接續前述新環境、無模型檢查與 v20 部署。
確認舊 `snet-api` 的 service association／navigation links 已釋放後，
用原已部署、固定版本的 AVM `managed-environment:0.16.0` ARM template
建立 `cae-fa-agent-v20`，只新增 environment 及其 keyless Azure Monitor
diagnostic setting。沿用原 subnet／NAT，不再建 subnet、VNet、NAT、Foundry、
Storage、ACR 或 Azure RBAC。CAE `Succeeded`，平台 LB／Public IP 均
`Succeeded`，`staticIp=203.0.113.10` 與平台 Public IP 一致。

App／Job 已刪除並釋放名稱，因此在新 CAE 下重建原名稱
`ca-fa-behavior-v18`／`job-fa-agent-sync-v18`，保留配置中的 ARM bindings，
不是把存活 App 原地換 environment。Job AVM 為 `0.7.0`，API AVM 為
`0.23.0`。一次無模型 Job 檢查已 `Succeeded`，原 execution logs 讀到
`FA_ACA_PLATFORM_READY`、精確 core `1e7ab3bd…` 與 exit 0；console ingestion
比 system logs 慢，沒有為取得 marker 重送 smoke。

以既有兩個 v20 digest 發布四位 Agent 的 immutable version **2**，並讀回
active／100% selector、原 GUID／principal、同 core 與新 API URL：
`https://ca-fa-behavior-v18.example-00000001.westus3.azurecontainerapps.io`。
這是已核對的目標 FQDN，**不是可用性證明**。Operator 遷移 callback 沿
`image_source`／`desired_source`／`sync_agent_version`／`route` 接點，只替換
新 definition 的 API URL；不改 v1 metadata 或原身份，不把 URL 開放成
一般模型可選的 ENV flag。

API 保留原配置 bytes、ENV、`serve` 命令、2 CPU／4Gi、`minReplicas=1`；
ARM deployment 已成功，但 replica `CrashLoopBackOff`、未通過 `/healthz`。
同 image／配置／ENV 的一次無模型 production factory 診斷回
`BEHAVIOR_NOT_INITIALIZED`：配置的 Table authority guard 不存在。
另一方面，原 API audience
`api://00000000-0000-0000-0000-000000000013/fa-context-poc-api`
在目標 tenant 取得 token 回 `AADSTS500011`，resource principal 未建立。
**這是兩個獨立阻塞，不是 ACA 平台、image pull 或缺少 NAT。**

17:09 +08 Neal 批准先查既有身分、沒有才新增，並準備首次安裝 plan。
完整 Entra app inventory 未發現可沿用的 FA API 應用；GPT-6 Luna 已於
17:21 +08 建立單租戶 `fa-context-api-v20`，新 URI 為
`api://00000000-0000-0000-0000-00000000001a`，access token version 1。
只公開 `Behavior.Read`／`Behavior.Approve`；API resource SP 不要求 app role，
不照抄早期 stage 的 `Behavior.Agent` role。Azure CLI 的本 tenant client SP
也補齊，consent 僅為 Neal 個人的 `Principal`，不做全租戶同意、
不加 Graph 權限、Azure RBAC、secret 或其他 client 的授權。
真實取得 token 的 tenant／oid／URI audience／issuer／client／兩個 scopes
已核對；token 不保存，這不是 API HTTP 可用性證明。

原 runtime 不覆寫，新私有配置只改 `identity.audience`，其餘 typed 語義、
身份及 resource bindings 相同；新 canonical digest `1768911b…`，
fresh inputs digest `2f6e766d…`。API／Hosted 的新 audience 已備妥，
正由 GPT-6 Luna 接續部署；尚未取得新版 API readiness／HTTP 授權結果。
以固定 v20 API image 的既有 fresh operator **先唯讀核對首次安裝 plan**；
核整個 Table 為空而非只有 authority partition，保留精確 plan／配置 digest，
再取得 Table 寫入批准。有既有或部分安裝資料就停下核對，
不清空 Table、不重寫批准或原件。API 真正 ready 後才 follow／最小 live 驗收。
17:24 +08 原 execution `job-fa-agent-sync-v18-mjnh11r` 已完成空表 plan，
`table_writes=0`、`approval_required=true`，plan digest `c7bed584…`；
本機以相同固定版本模型重建並核對完整 digest 相同；17:24 時尚未 apply。
Table 寫入只接受這份精確 plan 的 operator approval，有限窗口最多 15 分鐘；
apply 前會再核全表為空，建立 guard／回條與初始狀態的一筆 create-only EGT，
不把先前一般部署批准改寫成已批准的 plan。
Neal 後續選擇「批准這份精確計畫，執行一次首次初始化」；保存的窗口為
18:08:02–18:23:02 +08，沒有自動續期。GPT-6 Luna 的唯一 apply
`job-fa-agent-sync-v18-4jrou21` 已在窗口內完成，原 reconcile
`job-fa-agent-sync-v18-k99esad` 讀回 `initialized`、同一 receipt `5527b046…`。
request／installation IDs 保持，`budget_reset=false`；不清資料或重送 apply。
**首次安裝完成不等於 API／Hosted 已可用**；接續只改新 API revision 的
配置 audience 與四位 Agent 的新 immutable definition audience，
保留映像、原身份、ENV、min 1／compute／probes、v1／v2 metadata 及其他資源。
沿固定 AVM 更新 API、核 production factory 與真人 HTTP 授權後，才 follow；
不把 `/healthz` 的 liveness、token claims 或 active version 當完整功能驗收。
首次安裝後尚未 follow、未呼叫模型；API 仍設定 min 1，停止或降為 0 需另行批准。
詳細結果見 [16:01 接續紀錄](../docs/progress.md#fa-new-subscription-v20-1007)。

<a id="v20-portal-readback"></a>
#### 2026-10-07 19:00 +08：v20 API／Hosted audience／Portal 最新讀回

- `fa-v20-api-console-20261007` 已 `Succeeded`。API revision
  `ca-fa-behavior-v18--v20-1e7ab3bd-console1` Ready，只有新增已批准的
  `FA_REMOTE_CONSOLE_CLIENT_ID`；image digest、2 CPU／4 Gi、min／max 1 保持。
  `/healthz` 200（`EVIDENCE_GATED`）、匿名 accounting 401、Neal delegated token
  accounting 200；未保存 JWT，未呼叫模型。
- production factory 無模型檢查 `job-fa-agent-sync-v18-8g6ar35` 成功；
  四位 Agent audience 修訂 `job-fa-agent-sync-v18-kq0s5jo` 成功。
  四位新 version 3 active、100% route；core、Hosted image、API URL、新 audience
  已讀回一致，GUID／principal 不變，舊 v1／v2 未修改。尚未 follow、開 session 或呼叫模型。
- Portal 部署 `fa-v20-portal-20261007` 已 `Succeeded`：
  `https://ca-fa-console-v20.example-00000001.westus3.azurecontainerapps.io`。
  `ca-fa-console-v20--v20-20261007-r1` 的 `web-console` 與 `http-auth` 均 Ready、
  restart 0，min／max replica 1。內建 Entra 僅允許 Neal；UAMI FIC 已讀回吻合，
  無 client secret。瀏覽器型匿名 GET `/`、`/api/state` 與 `/novnc/core/rfb.js`
  均 302 至目標 tenant 的 Entra authorize；非瀏覽器 `Accept: */*` request 回 401。
- UAMI 僅有 registry `AcrPull` 及 `journal`／`easyauth-tokens` containers 的
  Blob Data Contributor；storage public network／shared key 關閉，private endpoint
  `Approved`、Blob private DNS link 已讀回。Portal 僅載入已批准的 A／B 設定。
  Endpoint 已可供 Neal 登入；尚未做互動登入、登入後 A／B 畫面／操作或模型驗收，
  不宣稱真人 Portal 對話端到端通過。

#### 2026-10-07 19:18 +08：登入成功，但 A／B 尚未首次準備

19:07 的真人畫面已顯示 Neal 登入且 A／B 可見，但 `Hi` 被拒；
API 記 `NATIVE_SESSION_CHANGED`。目標 tenant 的唯讀 Graph 核對確認帳號是
`owner@contoso.com`，OID 與原 owner 一致；`contsoo.com` 是輸入拼字差異，
不是需要新增帳號或放寬 allowlist 的理由。
19:15、19:18 兩次正式 API GET `/environment` 都讀到 A／B 的 version 3、
`state=needs_follow`、`observed_sessions=[]`。**版本發布與 Table 首次安裝均未完成
Hosted session 的首次 preparation**；不是再次修 Entra、重裝 Table 或重建 Agent。
原 `Hi` 的 receipt read 仍回 409，不能把缺少 reader 的拒絕當作原 RID 已不存在。

**修訂後的首次部署收尾（待個別批准，尚未執行）：**

1. 保持 v3、固定 images／core、現有 Agent 身份與原件。取得啟動 A／B Hosted
   session 及有界模型驗收的批准；fresh Table apply 與一般部署批准不取代這個範圍。
2. 每位 Agent 保存唯一 preparation intent／`request_id`，沿已驗證 owner 身份，
   POST `/agents/<agent_id>/work-policy/v1/native-turns/prepare`，body 只有 `request_id`。
   核真正 preparation 回條的 `state=ready`、該 Agent 的確定 SID、
   `hosted_agent_version=3`、`model_started=false`。準備 VM 本身仍可能計費。
   若失 ACK／unknown，沿原 `/prepare/reconcile` 查回，不換 ID 重送、
   不改舊版 metadata，也不以只讀到 active version 宣告成功。
3. 先查原 `Hi` 的 RID／完整 command／receipt，結果未決不重送。
   然後在明確批准的 scope 內做 A／B 最小回合，核接單、終態、實際回覆與入帳；
   最後核 Portal 登入後能顯示結果。session ready 或 HTTP 200 均不是完整對話驗收。

**不要用 recovery Job 的 `follow` action 代替首次 preparation：**
該 Git 外 helper 仍會呼叫 `_sync_script(..., follow=True)` 重做 definition sync，
而保存的 targets 尚有 v1 placeholder；未核對新 audience overlay 前重播可能產生
額外版本或錯誤設定。本輪使用已存在的正式 `/prepare` 接點，不新增 agent loop。
C／D 的 `AGENT_SCOPE_MISSING` 與 A／B 本次 session 缺口分開，不因此擴權。
目前 preparation／模型驗收未獲真人回應；桌面亦為 locked／inactive，
未繞過登入或擷取既有 browser session。登入成功不等於 Agent 已正常可用。

#### 2026-10-07 19:38 +08：首次準備完成，接續模型授權診斷

接手時重新查核同工作階段的原問答：16:01 的「go」已批准部署順序中的
follow／最小功能驗收。19:18 將這一段再次視為待批准是過度保守；
**這份既有批准不涵蓋新增模型角色**，兩個操作範圍分開處理。
19:23／19:24 對 A／B 各送一次正式 `/prepare`，保存原 intent／RID；
兩份回條均 `ready`、version 3、確定 SID、`model_started=false`、
`original_work_replayed=false`、previous SID 為 null，原 `/prepare/reconcile`
已確認一致。沒有 sync、新版、IAM、重建或工作重播。
原 `Hi` 的 receipt read 現在回 404 `NATIVE_RID_MISSING`；未替它補造 command 或重送。
原缺 reader loop 已轉綠；metadata 仍是 `unverified`，以真正 preparation 回條確認準備，
不把 metadata 或 session 啟動當成模型驗收。

19:27 沿固定 v20 Portal 的 `StandingConsoleConfig`／`NativeAdapter`，
對 A 提交首個短測試 `fa-v20-smoke-a-20261007`，現行模板選 `foundry-native-v1`、
模型 `gpt-6.1-sol`，不是自行改模型或 template。回條
`accepted → started → unknown`，failure `NATIVE_MODEL_REFUSED`；
已保留一個未量測的模型 request，未重送、未改成零用量。
官方 session log 的固定去敏行證實：

```text
FA_MODEL_DIRECT_HTTP_FAILURE status=401 wire_api=completions model=gpt-6.1-sol error_code=PermissionDenied attempts=1 before_output=True
```

目標模型 deployment 是 `Succeeded`；固定 runtime 與 SDK 2.3.0 的 token scope
均為 `https://ai.azure.com/.default`。project MI 已有 account 範圍的 `Foundry User`，
不是要再給 project MI 或 API 權限。A／B 的直接角色清單均為空。
這將下一個診斷收斂到**現行聊天模型路徑的 Agent 存取權**，但補角色的效果尚未實測。
官方說明 Hosted Agent 對自己 project 的標準推論有 implicit access；
**不得把本次 Chat Completions 的拒絕外推成所有 Hosted Agent 都必須加角色**。
依現有 setup 接點，候選最小修復為只給 A／B 在這個 project 的
`Cognitive Services OpenAI User`，不擴到 account／RG／subscription，
不加 `Foundry User`、管理角色或 C／D 的存取權。

已要求上述精確權限與修復後最小驗收的批准，但未取得真人回應；
私有 plan 的兩個 assignment 名稱沿 `behavior_agent_setup.assignment_name`，
`approval_received=false`、`cloud_writes_performed=0`。**尚未新增角色**。
B 的模型測試未送，避免重複已知的授權拒絕。
修復後用新、固定且批准內的驗收 RID；原 A unknown／用量及原 `Hi` 保持，
不以補權限為由重播它們。真人 Portal 登入已觀測，修復後互動畫面仍未重驗；
只有模型實際回覆與 Portal 顯示均成功才能完成原目標。
參考 [Hosted permissions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions)。

#### 2026-10-07 20:38 +08：B 缺主人選版，不以程式預設繞過

20:29 唯讀 API 核對 A／B `/agent-template/selection` 都為 null；
A export 200、`fa-instruction-v1`，B export 409 `AGENT_TEMPLATE_PROFILE_UNSUPPORTED`。
完整 consumer／既有操作說明確認：registered B 刻意不套 server default，且兩份
Portal sidecar 明確走 native，所以不能假稱 B 可保留舊 lane。
本輪曾起草 scoped native default 放寬，發現它與既有主人明選契約衝突後**完全撤回**；
三個 runtime modules 已無差異，沒有 build、部署或雲端寫入。

下一步是 B 的既有 `/agent-template/select`，不是換 API／Hosted core 或改 fresh plan。
待主人批准的最小提案：`fa-instruction-v1`（prefix `8e24b529…`）、
`foundry-native-v1`、`auto-no-custom-v1`／`linux-x64`／`openai-completions`，
零額外 capabilities／skills；Git 外 v2 proposal template digest `06a3f399…`。
本機 Windows default 的先前 v1 proposal 未套用，v2 明確取代它；不得送錯 checkpoint platform。
主人的精確選版批准仍未取得，**B selection 尚未寫入**。
選版只影響 B 新對話，不能修 A 的模型 401，也不授予模型 IAM。
既有 A unknown／未量測用量與原 `Hi` 保留，C／D 不在修復範圍。

本機回歸證明原拒絕符合契約，且主人沿既有 API 明選後 B export／capture 一致；
缺 `Behavior.Approve` 的請求 403 且 Table 零寫入。未新增 runtime 行為，
不以本機通過代替 Azure 選版、模型或 Portal 畫面驗收。

#### 2026-10-07 21:04 +08：現行存取對照仍失敗，停止重複模型測試

沿同工作階段已批准的最小功能驗收，20:57 用新 RID
`fa-v20-smoke-a-current-access-20261007` 對 A 提交一次獨立回合；
仍 `accepted → started → unknown`、`NATIVE_MODEL_REFUSED`。21:01 原件讀回
確認兩個 A RID 均 unknown，各保留一筆未量測請求、token counts=null，
不是重送首個回合或零用量。B 尚未提交模型回合。

限時官方 session log 唯讀未取得第二回合的安全 provider failure 行，
因此它的 HTTP status／provider code 尚未核實；首回合的 `401 PermissionDenied`
證據不外推到新回合。未新增 IAM、B selection、部署或 runtime 修改。
不再盲發同類測試；先取得相應精確批准、單一變數修復，再用新 RID 驗回覆與
真人 Portal 顯示。完整用量與讀取限制見
[接續紀錄](../docs/progress.md#fa-new-subscription-v20-1007)。

#### 2026-10-07 21:18 +08：修正操作端擷取，補核原 session 的 401

上輪「尚未取到第二回合 HTTP/code」來自操作端 regex 漏掉 logger 的中間安全欄位，
不是來源沒有拒絕證據。用既有去敏樣本重現、七案回歸通過後，
21:14 唯讀同 A v3 SID，取得新 intent 之後 **20:58:03 +08** 的
`401 PermissionDenied`／completions／`gpt-6.1-sol`／attempts=1／before_output=True。
關聯依同 SID 與時間窗，沒有 provider request-ID 證明；原 log／provider message 未保存。
21:18 正式原件 read 仍 unknown、原用量不變，新增模型請求為零。

Graph 唯讀確認 A／B 是已啟用的 `ServiceIdentity`、project 是已啟用的
`ManagedIdentity`，不能混用或從 metadata 推定 VM token claims。
操作端修正不改 FA runtime、IAM、B selection 或部署；模型角色的候選效果仍未驗，
不得以擷取器 GREEN 代替模型／Portal PASS。批准與下一步沿上節，詳細證據見接續紀錄。

#### 2026-10-07 21:57 +08：staging 權限基線比較，只讀未套用

staging 的 Foundry account 在共用 `rg-faexample`，project 是
`cog-fasample-memory-demo/faexample-stage`。沿兩個精確 tenant／subscription
核 account 與 project 的角色，包含上層指派並完整分頁；SDK 核 A／B 的真 principal：

- **staging A／B 各有 project Foundry User ＋ Cognitive Services OpenAI User；
  新 A／B 未找到這兩項角色的本層或上層指派。**
- 兩邊 project MI 都是 account Foundry User，API MI／Team 主人都有 project
  雙角色；兩個主人 account permissions 的 dataActions 都為空。
  account 的 `disableLocalAuth=true`、public network／default Allow 也相同；
  不以停用 API key 解釋差異，不把真人 account Playground 與 Agent project 呼叫混用。
- staging API MI 另有 project Contributor 與 ABAC 受限 RBAC Administrator。
  新 API MI 沒有這兩項角色，但兩邊都已開
  `FA_ONBOARDING_ENDPOINT_PROTOCOLS=true`／`FA_ONBOARDING_ROLE_SCOPE`。
  ENV 不代表有指派權，onboarding 接點也不會替已存在的 A／B 自動補角色。
  這是「＋」配權的獨立缺口，不是修本次既有 Agent 推論須照抄管理角色的證明。

這份正常環境基線支持先在 **Agent project scope** 修復，不先擴到 account。
最小 A OpenAI User 候選與 B 精確選版仍待批准，補角色效果仍須新 RID 驗回覆；
完整記憶／onboarding 對齊另列雙角色、精確 ABAC 與批准，不把它們變成此次推論前置。
比較本身沒有模型呼叫、IAM／ENV／B selection 寫入或重新部署，原 unknown 保留。
詳見[權限比較紀錄](../docs/progress.md#fa-stage-new-permissions-1007)。

#### 2026-10-07 22:19 +08：已批准並對齊 project 權限，等待模型驗收

22:06 同 project／同 Chat Completions／同 `gpt-6.1-sol` 的 Neal 身分對照，
單次取得 200 與精確回覆，用量為 input 12／output 6／reasoning 0；
這證明該 caller 的現有 project 路徑可用，**不是 A 或 Portal 已修復**。
獨立意圖／安全結果保留，不保存憑證或 raw provider body。

使用者先批准 A／B 的 project OpenAI User、B Linux v2 範本與有界驗收；
兩筆角色已新增並讀回。A 的 22:12 新 Portal 回合仍 401／unknown，
距 A 指派僅約兩分鐘，不把它當成永久失敗或改 account scope 的證據。
之後使用者明確批准 Foundry User，並說「只要是對齊staging環境，我都批准」；
22:18 再沿剛核實的 stage 基線新增四筆精確 project 角色：

- A／B 各 `Foundry User`，與自己的 OpenAI User 組成 staging 雙角色。
- API MI 的 `Contributor` 及 ABAC v2 受限 `Role Based Access Control Administrator`；
  condition 原樣保留，並且只在新 project。管理角色不給 A／B。

22:19 完整角色讀回確認對齊，API key 仍停用；沒有改 staging、擴到 account／RG／
subscription、複製舊租戶 principal、換 provider／core／image 或重做初始化。
B 的正式單次 `/select`、原 command reconcile、current selection 與 export 均同
Linux v2 digest `06a3f399…`；不是改原 policy 或使用 Windows 提案。

**新環境收尾須分開核對：** 初始 A／B 的 project 模型資料角色、API「＋」精確
委派權、owner 範本選版、正式 session preparation，以及真模型／Portal 結果。
`FA_ONBOARDING_ROLE_SCOPE` 只接 onboarding，不會替既有 seed Agent 補權限；
ENV、角色存在、選版或計時完成都不是模型成功。新租戶首次安裝缺哪項就依
該項明確批准補齊，不能照抄 staging 的 tenant／human／resource ID 或整套角色。
本次等最後 IAM 讀回滿 10 分鐘才重新驗收；舊三個 A RID unknown／用量保持，
不重送或改判。詳細批准、回條與結果沿[接續紀錄](../docs/progress.md#fa-new-subscription-v20-1007)。

### 15:46 已完成的清理：只刪失敗 ACA，不刪 RG

使用者後續先批准新 v20 environment 備案，再要求節省費用；經刪除影響確認，
最後選擇「只刪除失敗的 ACA 運算，保留其他資源與資料」。
實際清理範圍僅為 `ca-fa-behavior-v18`、`job-fa-agent-sync-v18`、
`cae-fa-agent-v18`；**不是刪除 `rg-fa-agent-v18`**。
15:29 +08 CAE 尚為 `ScheduledForDelete`，當時不能宣稱刪除完成。
15:43:44 +08 原刪除命令結束；15:46 +08 對三個資源的直接 ARM GET
均回 `ResourceNotFound`，受管 `ME_cae-fa-agent-v18` RG 也已不存在，
主 RG 仍存在，**指定清理已完成**。沒有重送刪除或手動刪除共用 VNet。

Foundry project／Agent、Table／Blob、ACR（含兩個 v20 digest）、UAMI、NAT、
Public IP、VNet／private endpoints、監控與 Web PubSub 均保留。
清理前已把 App／Job／environment 配置及 15 筆非 active Job execution
metadata 保存到本工作階段 Git 外；這不是可還原的運算或整份資料備份。
NAT、ACR、Storage 等保留項目仍可能計費，不能宣稱整個 RG 費用已停止。

15:46 清理這一輪沒有建立替代運算，也沒有重 seed、sync 或模型呼叫。
下方原地修復步驟保留作歷史與診斷參考，**不得重播原 template 把已刪資源建回**。
舊 private profile 的 App／Job 名稱與 API URL 已不能直接執行；
後續接續須先核新 environment 與 App／Job、endpoint／resource
bindings 的 operator 遷移，不直接覆寫配置或重做已有安裝；
是否需要首次安裝，另依實際 authority guard 與原件判讀。

### Findings 改變了什麼

Activity Log 已確認 ACA 的平台 Public IP `capp-svc-lb-ip` 建立失敗於
`SubscriptionNotRegisteredForFeature`，指定
`Microsoft.Network/AllowBringYourOwnPublicIpAddress`。
2026-10-07 14:50 +08 唯讀讀回：該 feature、`Microsoft.Network`、
`Microsoft.App` 都是 `Registered`，但 `cae-fa-agent-v18` 仍然
`staticIp=null`、`ME_cae-fa-agent-v18` 沒有資源，API 沒有任何 revision。
因此現在不是「再註冊 feature 就算完成」，而是確認 feature 傳播後，
修復半完成的 ACA infrastructure；是否能原地恢復仍須實測。
完整時間線與映像基準見 [本輪紀錄](../docs/progress.md#fa-new-subscription-v20-1007)。

使用者另行批准第一階段後，已完成 Network provider re-registration，並在
15:03–15:05 +08 套用一次原 environment-only AVM template。
Deployment `fa-v20-aca-reconcile-20261007` 為 `Succeeded`，但 15:06 +08
readback 的 `staticIp` 仍為 `null`，平台 RG 仍無資源。
**本次相同設定的原地更新沒有達成恢復條件**；沒有因此重送更新、啟動
Job smoke／sync 或建立 Hosted 版本。新 environment 備案後來已取得批准，
但尚未建立；最新操作範圍以上方清理決定為準。

| 舊方向 | 修訂後方案 |
|---|---|
| ARM `Succeeded` 後開始 sync／API 部署 | 先核對 environment 的 `staticIp` 與實際平台資源，再用一次無模型 Job smoke 確認能啟動 container |
| 把新增 NAT／`defaultOutboundAccess=false` 當修復 | NAT 不是已證明的主因；本次保留現場 NAT、不加 NSG、不改 subnet，不再同時改網路變數。之後 NAT 改成 opt-in 參數 `api_subnet_nat_egress`（預設關），本環境重跑須設 `true` |
| 反覆換 image、identity 或重送 Job | environment 未恢復時不再跑 image／UAMI probes；未知操作先讀原 execution／intent |
| 重跑整份 foundation／重建環境 | 先評估 environment-only、保留現況的 reconcile；失敗才另批新 environment，不自動刪除或搬移 |

### 1. Subscription 前置檢查：只讀，先於 resource deployment

每個 shell process 都設定隔離的 `AZURE_CONFIG_DIR`，所有資源命令指定目標
subscription；先確認 account 的 tenant／subscription，不能沿用 CLI 預設值。
以下只讀命令也適用於後續新訂閱的首部署：

```powershell
$env:AZURE_CONFIG_DIR = Join-Path $HOME '.azure-foundry-agents'
$subscription = '00000000-0000-0000-0000-000000000018'

az.cmd account show --subscription $subscription `
  --query '{subscription:id,tenant:tenantId}' -o json
az.cmd feature registration show --subscription $subscription `
  --provider-namespace Microsoft.Network `
  --name AllowBringYourOwnPublicIpAddress `
  --query '{state:properties.state,registrationDate:properties.registrationDate}' -o json
az.cmd provider show --subscription $subscription `
  --namespace Microsoft.Network --query registrationState -o tsv
az.cmd provider show --subscription $subscription `
  --namespace Microsoft.App --query registrationState -o tsv
```

任何查詢失敗、身份不符、`Pending`／`Registering`／`NotRegistered` 或未知狀態
都停在此階段，不能把它們當成 ready。Feature 名稱來自本次 Azure 錯誤，
不泛化為所有 Foundry 或無 VNet 的 ACA environment 都必須啟用的產品要求。

目前 feature 已 `Registered`，**不重送 feature registration**。
若其他新訂閱缺少同一 feature，先取得 subscription-level 設定變更批准，
再註冊並讀回 `Registered`；`registrationDate`／`requestedTime` 不等於已完成
傳播的時間。依 [官方 feature registration 說明](https://learn.microsoft.com/azure/azure-resource-manager/management/preview-features#register-preview-feature)，
feature 完成後可能需重新
`az provider register --subscription $subscription --namespace Microsoft.Network`
以傳播變更。對本次半完成環境，將這個 provider re-registration 納入待批准的
修復範圍，之後讀回 provider；**provider 的 `Registered` 本身不能證明 feature
已被 ACA／Network 的實際建置路徑採用**。不重註冊已正常的其他 provider，
不要求固定等待時間後就自動宣告成功。

### 2. 先嘗試原地 reconcile，不重跑全套資源

每次執行前需批准：目標 subscription 的 Network feature 傳播，以及
`cae-fa-agent-v18` 的 environment-only 更新；不含刪除、IAM 擴權或新增 NAT。
先保存受控、Git 外的 environment／subnet／diagnostic 設定與原部署錯誤，
核對仍然是本次批准的資源。

沿用 `foundation.bicep` 已固定的
`br/public:avm/res/app/managed-environment:0.16.0`，從原 environment-only
nested deployment 與實際 readback 整理保留現況的輸入，先評估最小 what-if，
再執行一次受控更新。保留原 subnet、Consumption workload profile、
external／PNA、監控與現有設定；若 what-if 要修改其他資源或不可變網路屬性，
先停下重新確認。**目前沒有獨立的 reconcile 腳本／entrypoint**；
不得把重跑 `foundation.bicep` 冒充 environment-only 修復，因它還會寫
Storage、ACR、VNet、RBAC 與 Foundry connection。
單純更新 tag 或 no-op PUT 能否觸發平台恢復未經驗證，不把它寫成保證有效的修復。

本次實際使用 `az deployment group export` 匯出
`shared-context-api-environment`，並讀取原 deployment parameters。
重播輸入只保留有 `value` 的參數，不回送 readback 的 `type` 或缺少值的 secure
參數。第一次 what-if 顯示 peer authentication 與 diagnostic settings 會改變，
因此補入現場 `peerAuthentication`，不帶 `diagnosticSettings` 參數，
讓原診斷設定保持不動；第二次 what-if 對 CAE 為 `NoChange`，其他資源為
`Ignore`／`NoChange`。只送出一次 Incremental deployment，並保留唯一 intent、
模板／參數 hash、operation 與結果於 Git 外；這套已執行步驟仍不是自動修復腳本。

更新 timeout／結果不明時，先核原 deployment／operation，再讀回，不換 deployment
名稱反覆重送。ACA 的受管 infrastructure RG 由平台管理，不手動建立
`capp-svc-lb`／`capp-svc-lb-ip`，也不手動刪除殘留資源。

### 3. 平台就緒後才啟動 v20

```powershell
$env:AZURE_CONFIG_DIR = Join-Path $HOME '.azure-foundry-agents'
$subscription = '00000000-0000-0000-0000-000000000018'

az.cmd containerapp env show --subscription $subscription `
  --resource-group rg-fa-agent-v18 --name cae-fa-agent-v20 `
  --query '{state:properties.provisioningState,staticIp:properties.staticIp,infrastructureResourceGroup:properties.infrastructureResourceGroup,error:properties.deploymentErrors}' -o json
az.cmd resource list --subscription $subscription `
  --resource-group ME_cae-fa-agent-v20 `
  --query '[].{name:name,type:type}' -o json
```

對本次 external、custom-VNet workload-profile environment，必須同時看到
`provisioningState=Succeeded`、非空 `staticIp`，並在 environment 指向的
infrastructure RG 確認平台 Load Balancer／Public IP 已建立且其 IP 與
`staticIp` 一致。`capp-svc-lb*` 是本次錯誤指出的診斷目標，不當作所有
environment 的永久命名契約。**NAT 的 Public IP 不能代替 ACA 的 `staticIp`**。
Readback 失敗或仍然只有外層 `Succeeded`，都不進入 sync。

之後另在執行批准內，用現有 sync Job／v20 API image／原 UAMI／registry 做
**一次不呼叫模型的 container smoke**：只以 start override 執行
`python -B -c "print('FA_ACA_PLATFORM_READY')"`，不修改 Job identity／原模板。
保存唯一 start intent、execution 名稱與結果；須有 container/log，
execution `Succeeded` 且印出 marker 才往下。這個 smoke 是待執行的人工步驟，
不是既有 `run_fresh_job.py plan`（後者會讀 fresh installation 狀態）。
`containers=[]`、`ContainerNotFound` 或未知 start 結果都先查原 execution。
Smoke 只能證明平台與該 image 可以啟動，不代表 FA 功能驗收。

| v20 操作順序 | 保留的契約／完成條件 |
|---|---|
| 核對既有 v20 API／Hosted digest 與實際 installed core | 使用同一組含 PR #721 normalization 修補的 build；OCI digest 不當 `fa_build`，不拿本分支或另一份 checkout 的 core 代替 |
| 用保存的 runtime 執行 version sync，先 `--route --defer-follow` | 先核清舊 Job／create intents；保留 v1 與其 immutable metadata，新版本成功後讀回四個 Agent 的 active definition／selector，不預定服務一定分配 v2；sync 不證明 Table 已初始化 |
| 更新 API image，等本次 revision Ready | 先確認該 runtime 的安裝原件及此 tenant 的 Entra audience 存在；保留身份、命令、scale 與其他 ENV。初次安裝另走 operator plan／批准，不覆寫舊記錄；取得 `/healthz=200` 並核對所服務 core |
| 完成既有 follow／route verification | API 已服務同 core 才 follow；最後才跑獲批准的最小 live 功能驗收，單獨記錄結果與用量 |

`run_fresh_job.py` 的 `sync` 可沿用原 runtime，不重做 `plan`／`apply` 或 seed。
`ci_deploy.py` 的 `_sync_script` 會比對目前 checkout 的 core；使用它時必須在
與選定 images 相符的建置來源執行，不刪除 identity assertion。
`ci_deploy.py guard` 需要既有 ready revision 才能推導 serving commit；本次 API
沒有任何 revision，**不能直接套正常 CI 升級流程或假造 serving baseline**。
Operator 必須在批准內核對 image／已安裝 config 後決定首次恢復更新；
若實際 config 缺少或不同，屬於另行確認的安裝／設定操作，不是 image-only 更新。

兩個次要問題分開處理：Job 的 AVM `userAssignedResourceIds` 已沿用原身份；
若人工 ARM PUT 必須帶 identity，只傳 `{ "<uami-resource-id>": {} }`，
不回送 GET 的 `clientId`／`principalId`。Foundry 維持 `disableLocalAuth=true`，
不呼叫 `listKeys`、不為部署重新開金鑰驗證。
Temporary restore 的 `InvalidIdentityValues` 不等於 AVM Job 宣告已有同一缺陷。
10-08 盤點確認 `listKeys` 失敗全部來自 Azure／Foundry portal 介面的第一方 appid，
不是 FA 腳本或 Job（見[問題清單 A5](#new-environment-problem-inventory)）。

### 4. 原地修復仍失敗時的備案

先帶原 operation／Activity Log 與 feature readback 進一步查 ACA 平台問題；
只有在原操作已核清、再次批准新增資源與費用後，才建立新的 environment。
舊 environment 尚存時，新 environment 使用同一固定 AVM、同 VNet 中**另一個未佔用且符合 delegation／
容量要求的專用 subnet**，不把已被舊 environment 使用的 `snet-api` 再綁一次。
從 feature 前置檢查、平台 readback 到 container smoke 完整重走。
若舊 environment 已完整刪除，先核實原 subnet 關聯已釋放，可在批准範圍內重用；
本次 16:01 接續採此路徑，不再建立先前候選的新 subnet。

Environment 綁定不是 image update；若需替代 App／Job，使用新資源名稱，
不得假設既有 App 可以原地換 environment。API URL／runtime bindings／Agent
endpoint 如需變更，另外列明 operator 設定遷移與批准，再經原 sync／follow
接點接續；Foundry identity、Table／Blob、記憶、舊版本與原件不刪不重建。
舊 CAE、App、Job、NAT 先保留；刪除與清理是另一項批准，不自動回退或切流量。

本次 findings **沒有證明 NAT 必須移除**，也沒有證明「有 NAT Public IP」
等於所有 required egress 都正常。[官方網路說明](https://learn.microsoft.com/azure/container-apps/networking#environment-selection)
區分支援 NAT 的 workload-profile environment 與不支援的 legacy
Consumption-only；本次是前者。部署方案不把 NAT 當普遍前提：
`private-state-network.bicep` 的 NAT 現在是 opt-in 參數 `apiSubnetNatEgress`
（profile 的 `api_subnet_nat_egress`，預設 `false`），開啟時用 AVM
Public IP／NAT modules 並把 subnet 設 `defaultOutboundAccess=false`；
預設與 stage／dev 原本的 subnet 相同。需要固定 egress 的環境另核成本後再開啟。

## Code identity and seed recovery

`--image` identifies the immutable container manifest (`registry/repo@sha256:...`).
`--core` identifies the installed Python code: use the **lowercase 64-hex**
value returned by `shared_context.behavior_code_identity.code_sha256()` in the
matching API/Hosted images, also printed as `FA_DEPLOYED_CORE` by the API build.
`ci_deploy.py build api` reads it from the built image (`docker run --entrypoint python`),
not from the build log: a Docker layer reused from the cache prints no such line.
Do not strip `@sha256:` from an image reference and pass that digest as core.
The two values have the same shape but different meanings; `fa_build` must be
the code identity for `HostedVersions.current` to accept the served version.

Seed rejects malformed core values and a core equal to the Hosted image digest
before credentials, directories or intents are created. Seed cannot prove an
otherwise well-formed value belongs to the image: the operator must verify it
against the matching image's code identity, not a different checkout.
Version sync derives `fa_build` from its installed `code_sha256()`; its optional
`--core` checks that exact value before configuration/credentials are loaded.
CI passes the checkout code identity explicitly and keeps its existing runtime
identity assertion. The default stage profile is unchanged.

For definition readback, only the service-added top-level
`container_protocol_versions: []` is equivalent to an omitted field.
Nonempty protocols, null/other shapes, unknown fields, image, CPU/memory,
environment, session settings and protocol changes remain significant.
The original `definition_sha256` and seed body hashes are unchanged; this is a
comparison rule, not a migration of saved fingerprints. Both list and version
GET are checked. An existing current or legacy seed intent still prevents its
version create from being resent; the intent itself is never rewritten.

If an already-active v1 has an image digest in `fa_build`, **do not edit that
version's metadata, replace the seed intent, or seed again with another output
directory**. After separate deployment approval, reconcile the original create
records read-only, then use the existing version sync from the matching API
image/runtime to create a new immutable version with the correct code identity:

```powershell
python -B -m shared_context.behavior_agent_versions sync `
  --config $runtimeConfig --image $hostedImage --core $core `
  --route --defer-follow
```

`--defer-follow` is only for the preparation phase before the matching API is
ready; afterward complete the existing `sync --route`/CI follow step without
that flag. Sync preserves v1 and its metadata, verifies the new active
definition/identity, and publishes the new selector through the existing gates.
Do not blindly repeat an unknown create, job start or route write.

The reported original v18 image code identity is
`86f7b85ccf276941cc9e665504b25cfa66e837eef8fff28164e4352bbc1c9c07`;
it is not a constant for future builds. These shared-code fixes change core,
so rebuilt API/Hosted images need their newly printed code identity. Images
predating this CLI option still derive core automatically during version sync;
do not add unsupported arguments or substitute the manifest digest.
These are operator instructions, not evidence of a completed Azure remediation.

## Run the operational steps

```powershell
$env:FA_DEPLOY_CONFIG = 'C:\fa-inputs\v18\profile.json'

python .\deploy\stage\seed_agents.py `
  --config $env:FA_DEPLOY_CONFIG `
  --out C:\fa-inputs\v18\seed `
  --image $hostedImage `
  --core $core

python .\deploy\stage\make_runtime.py `
  --config $env:FA_DEPLOY_CONFIG `
  --out C:\fa-inputs\v18\runtime

python .\deploy\stage\run_fresh_job.py `
  --config $env:FA_DEPLOY_CONFIG `
  --runtime-dir C:\fa-inputs\v18\runtime `
  --work C:\fa-inputs\v18\fresh `
  plan --image $apiImage
```

`ci_deploy.py`, `seed_agents.py`, `make_runtime.py`, and
`run_fresh_job.py` all accept the same profile selector. The profile loader
validates identifiers, URLs, agent records, and runtime bindings before an
Azure write. The API deployment keeps the existing safety contract: the
installed configuration is guarded, image references are digest-pinned, and
the API template keeps `minReplicas=1` in generated profiles.

The staging GitHub workflow does not change its behavior; it uses the default
stage profile. A different workflow or operator run can pass `--config` to
the same commands without copying or editing Python scripts.

<a id="compat-gate"></a>
## 部署前相容閘門（ADR-0009 phase 3b）

依 [ADR-0009](../docs/adr/0009-version-model-g-actual-version-idle-upgrade.md) 第 7 點與 M1（先擴充、再收斂）：
API 只有在**沒有任何綁定列還記著舊 profile、也沒有 session 還跑舊 image** 時，才可以不再說舊 wire。閘門就是這個收斂的檢查。

- **在哪裡跑：** `ci_deploy.py gate <api-ref>`。stage workflow 排在「Build and push API image」之後、其他 image build
  與 sync／API 更新／console／follow 之前（在它之前只有 ACR push，不寫環境）；azd 的 `upgrade()` 排在 `check` 之後、WebIQ／Memory
  Store／sync 之前。它以**新的 API image** 啟動一次 sync job（API identity；GitHub 部署身份沒有 Table 與 Foundry
  資料面權限），所以「新 API 的支援集合」就是那個 image 裡的 `native_binding.SUPPORTED_STATE_PROFILES`，沒有第二份。
  第一次安裝（azd 首次 `deploy`）還沒有綁定列，不跑。
- **讀什麼：** 只讀這個 Team 的全部綁定列（`NativeBindingStore.rows()`，分頁讀到底）和這些列引用的版本 definition
  （`get_version`）。**零 session 呼叫**：不 list／get／invoke／stop session、不讀 Session Files、不寫任何東西
  （`shared_context/native_deploy_gate.py`；測試以只答 `get_version` 的替身確認）。
- **什麼算不相容：** 每個服務中的 Agent，(a) 綁定列的 `state_profile` 不在新 API 的支援集合；或 (b) 紀錄的實際版本
  （`observed.agent_version`）的 definition 宣告的 `FA_NATIVE_TURNS` 不在支援集合；或 (c) 讀不到 definition、
  讀不到綁定列。(c) 證明不了收斂條件，所以閘門打開時一律算拒絕（fail closed）。已不在服務清單的 Agent 留下的列只檢查 (a)。
  退回較舊的 API image 也算：例如 phase 2c 之前的 image 讀不了還帶著未送出 `switch_notice` 的列，打開時就以 `rows_unread`
  拒絕（[共用 README](../src/fa-shared-context-poc/README.md#g-phase2c-clear)）。
- **還沒有綁定列的 Agent（`unclaimed`）只報告、不拒絕：** ADR 第 7 點檢查的是綁定列**紀錄的** profile，沒有列就沒有
  紀錄可查（剛 onboarding 的 Agent、stage C／D 這類還沒被 API 觀察過的 Agent）；否則每次打開後的部署都會被它們擋住。
  它們第一次被觀察時才認領綁定列（`NativeBindingStore.claim`）。閘門不能判斷它的 session 是不是所選版本推導出的
  第 0 代 SID（`behavior_native_sessions.native_session_id` 也推導不帶版本的 SID，要分辨得讀 `get_session`，閘門不呼叫
  session），所以 2d-ii 打開前，[ADR-0009 M1 閘門清單](../docs/adr/0009-version-model-g-actual-version-idle-upgrade.md)
  要先把報告的 `unclaimed` 認領或退役。
- **報告：** job log 一行 `DEPLOY_COMPAT_GATE {...}`（Log Analytics），含每個 Agent 的判定、`decision`
  （`pass`／`observe`／`refuse`／`override`）與保護清單（見下節）。CI 只看得到 job 成功或失敗（ARM 的 execution 沒有 exit code 或輸出），
  step summary 只寫狀態能證明的判定與這一行的位置；拒絕筆數與詳細原因讀這一行。
- **時間：** 閘門有自己的等待上限 `GATE_POLL_ATTEMPTS`（18 次 × 10 秒＝**3 分鐘**），不沿用 sync 的 30 分鐘（sync job
  的 `replicaTimeout` 最長可到 3600 秒）。超過就對該 execution 送 Jobs - Stop Execution
  （`POST .../jobs/{job}/executions/{name}/stop`），不打開時只留 `::warning::`、部署照常；打開時算拒絕。ARM 回應是空的或
  格式不對（`None`、缺欄位、不是 JSON）同樣處理：不打開時是 warning，打開時是拒絕。
- **每次部署多花的時間（估計，未量測）：** 離線量不到，以下是依 job 規格推估：Consumption、0.25 vCPU／0.5 GiB 的 sync job
  冷啟動並**拉一次新的 API image**（python 3.11 slim＋behavior 相依套件，約數百 MB；sync 之後仍可能在別的節點再拉一次，
  不保證有快取）約 30～90 秒，Python 匯入與 `build_server` 約 10～30 秒，讀綁定列與每個 Agent 數次 `get_version` 幾秒，
  加上 10 秒輪詢的粒度。一般約 **1～2 分鐘**，最多約 3 分鐘再加一次 stop 請求。第一次 dev 部署後以 job execution 的
  開始／結束時間核對，再改成量測值。

**預設不打開（unarmed）：** ADR 第 7 點要求閘門在 v1→v2 遷移完成後才接上，否則第一次 v2 部署一定被擋。所以閘門
每次都會跑、報告「如果打開會拒絕什麼」（`decision=observe`），但 job 結果不影響部署（失敗只留 `::warning::`）。
2d-i 本身也不受影響（2d-i 的 API 仍支援 v1）。

**打開（arming）：** 部署 profile 加 `"compatibility_gate_armed": true`（stage 是 `deploy/stage/stage.json`；azd 環境
`azd env set FA_COMPAT_GATE_ARMED true`）。時點是觀察報告已經沒有 v1 的列之後、2d-ii 之前，由 2d-ii 的 release PR
（或它之前的 PR）一併改，經 PR 審查留紀錄。打開之後，拒絕或讀不到都讓部署在任何環境寫入之前停下。

**一次性例外（override）：** 打開後真的必須先部署（例如要退役的 Agent 還綁在舊版本），在該環境的 release 檔
（stage 是 `deploy/stage/release.json`）加：

```json
"compatibility_gate_override": {"release": "<這次的 version>", "approved_by": "Neal", "approved_at": "<時間>", "reason": "<原因>"}
```

- 只對 `release` 等於這次 `version` 的那一個 release 有效；下一個 release 還留著就在 `meta`（任何 build 之前）停下，
  要求拿掉，所以不會悄悄延續。沒有 release 檔的環境（`release()` 是 `none`）不能用 override，只能改回不打開。
- 批准：stage 的 release PR 由 Neal 決定合併，override 隨它一起經批准；其他環境照該環境的批准規則。
- 紀錄：git 歷史、run 的 `::warning::` 與 step summary（`compatibility gate: OVERRIDE (...)`）、job log 的
  `decision=override` 與 override 內容。閘門仍照常讀一次，把被放行的 Agent 寫進報告。

**2d-i（P3）起綁定列寫版本宣告的 profile：** 2d-ii 的 API 只支援 v2，所以三個寫入點都寫該 session 版本 definition
實際宣告的 `FA_NATIVE_TURNS`（`behavior_native_sessions.NativeSessions._row_profile`，經 `HostedVersions.profile` 讀
`get_version`，每個版本每個 process 只讀一次），否則已搬到 H2d 的 Agent 也會被 (a) 擋下：

- `NativeBindingStore.claim()`：第一次認領列，寫被認領 SID 的版本宣告的 profile。只有真的要新增列時才讀（已有列的觀察，
  例如送出路徑的 drift 紀錄，不多讀 `get_version`）；讀不到 definition 就不寫（記 `NATIVE_BINDING_UNRECORDED`，下一次觀察
  再認領）：認領不會覆寫已有的列，寫錯了沒有補救。
- 換環境的 commit `NativeBindingStore.record_switch()`：寫目的 session 版本宣告的 profile。follow 讀不到就不寫，由重跑補寫；
  送出路徑的 M3 修補（`_repair`）讀不到則寫 v1 並記 `NATIVE_BINDING_SWITCH_PROFILE_UNREAD`，照 2d-i 以前一樣寫列並重送，
  不讓一次暫時的 Foundry 失敗變成拒絕；版本實際宣告別的 profile 時由部署閘門報出。
- `NativeBindingStore.decide_abandon()` 在沒有列時連同棄用決定一起認領：讀得到就寫宣告的 profile；讀不到時，棄用理由是
  `incompatible` 寫 `incompatible`（`INCOMPATIBLE_STATE_PROFILE`，不在任何支援集合內，其他 process 的強制 guard 不會再呼叫
  這個 SID），其他理由寫 v1（之後執行棄用的 `record_switch` 會整列換掉）。
- 離線測試沒有 `HostedVersions`（`hosted_versions=None`）時照舊寫 v1。

<a id="version-retention"></a>
## 綁定列引用的版本與 image 不刪（ADR-0009 phase 3b）

- **規則：** 每個綁定列的目前 SID 建立時的版本、紀錄的實際版本，以及之前 SID 清單最後一個（前一個 SID）的版本，
  連同這些版本 definition 裡的 ACR image（以 digest 比對），不刪、不讓保留政策清掉。刪除任何版本、image 或 session
  仍需另外批准。
- **保護清單：** 閘門報告的 `protected`（`versions`、`images`）每次部署都會算一次。任何清除清單都要先經
  `native_deploy_gate.retention_candidates(candidates, protected)`，被引用的一律留在 `kept`。
- **現況（2026-10-15 核對）：** repo 沒有任何刪除單一 Agent 版本或 ACR image 的腳本（測試掃描 `deploy`、`scripts`、
  `infra`、`.github` 與 `src/fa-shared-context-poc` 確認），所以目前沒有要加守門的清除清單；之後新增的要走上面的函式。
  唯一會刪 Foundry 物件的是下面的[退役指令](#retire-agent)：它整個刪掉一位**已退役**的 Agent，或一個 onboarding
  失敗後留下的 Foundry agent（`force=true`），不是挑版本清除，也不碰其他 Agent。
  `infra/shared-context/foundation.bicep` 的 registry 是 `retentionPolicyStatus: 'disabled'`（測試鎖住）。唯讀
  `az acr` 讀回：dev `crfacontextpocdemo`、stage `crfacontextstagedemo` 都是 Basic、retention `disabled`、沒有
  ACR task（也就沒有排程的 `acr purge`）。不需要改 infra。
- `azd down` 刪的是整個環境（含 Table 裡的綁定列本身），不在這條規則內；它另有確認步驟與批准。

<a id="retire-agent"></a>
## 退役一位 Agent（tombstone；Foundry 清除另外選）

用在「＋」建立、之後不要了的 Agent（例如 dev 測試 Agent）。設定檔裡的 Agent（A 與 config targets）不能退役。
依 ADR-0006 補充「退役是 tombstone，不是刪除」：Team 的 Table 資料一筆都不刪。

1. **寫 tombstone（API 正常寫入路徑）。** `TeamRegistry.retire_agent` 在同一筆 CAS 建立 create-only 的
   `team_agent_retired` 列（誰、何時、原因、request ID），並把這位 Agent 的身分索引改成停用（原 `deactivation_id`
   就是這個 request ID）。roster、註冊列、onboarding、綁定列、routine 定義與對話紀錄都原樣保留。之後：
   工作台目錄不列它（`/work-policy/v1/agents`）、API 啟動不再為它建 peer 或記目錄錯誤、部署的 `agent_versions sync`
   與 `follow_all` 跳過它、routine 時鐘不觸發它的排程（只記一次 `FA_NATIVE_ROUTINE_CLOCK_RETIRED`）、相容性閘門把它的
   綁定列列在 `retired_rows`（不擋部署，版本仍列在 `protected`）；任何送給它的訊息（真人或其他 Agent）回 410
   `AGENT_RETIRED`「這位助理已退役」，什麼都沒送出（API 重啟後它的 binding 路徑也一樣；Teams 轉送改回一句
   「這個助理已經退役，這則訊息沒有送給它。」，`follow_all` 回 `AGENT_RETIRED`）；群組活動跳過它的回合、只記
   `NATIVE_GROUP_ACTIVITY_RETIRED_MEMBER`，其他成員的回合照常可讀；它自己的 Agent token 在入口被拒（身分已停用）。
   重跑（同一或新的 request ID、或 ack 遺失後）回原 tombstone，不寫第二次。
2. **（選用）刪 Foundry 端。** 加 `--delete-foundry`：刪掉 Foundry agent 本身（`agents.delete(name, force=True)`），
   以及這位 Agent 自己身分在 onboarding role scope 上的 FA 兩個角色指派（Foundry User、Cognitive Services OpenAI User）。
   **409 說明：** 不帶 `force` 時，只要有任何版本還有 session，服務就回 409（deletion-blocking sessions）；
   `force=true` 會連同所有版本與 session 一起刪（2026-10-15 dev 的 `fa-agent-cc96…` 就是這樣刪掉 85 個版本、56 個 session）。
   目標只取自這位已退役 Agent 的原註冊列（agent name、agent oid），不讀其他 Agent。
   API 的受控身分依 ADR-0006 §1 的 ABAC 條件**不能刪除角色指派**，所以這一步通常回 `refused status=403`：
   preview 已列出確切的指派 ID，由有權限的真人刪（`az role assignment delete --ids <id>`），再重跑同一個指令讀回收斂。

**operator token 怎麼取：** 在 operator 自己的電腦上用自己的帳號取，一定要帶 `--subscription`（API 所在 tenant 的訂閱）。
不帶的話 `az account get-access-token` 可能用預設帳號的另一個 tenant，回 `AADSTS500011`。`<api-audience>` 是該環境的
`CONTEXT_API_AUDIENCE`（dev 與 stage 不同）。輸出直接進剪貼簿，不顯示在畫面上：

```powershell
az account get-access-token --subscription <subscription-id> --resource <api-audience> --query accessToken --output tsv | Set-Clipboard
```

再由 operator 自己把它貼到下面指令的隱藏提示「Original operator bearer token (hidden):」。token 不放在指令參數、檔案、
聊天或 log 裡，也不經過任何工具或 agent 轉手。
貼完立刻清掉剪貼簿：`Set-Clipboard -Value $null`；有開 Windows 剪貼簿歷程（Win+V）或雲端剪貼簿同步的，也要從歷程刪掉這一筆。

```powershell
# 在 API 容器內（受控身分與現行 config）；token 用隱藏輸入或 FA_TEAM_OPERATOR_TOKEN，只有原組織 operator 可以。
python -m shared_context.behavior_agent_retire preview --config <current-config> --agent-id <agent-id> [--delete-foundry] [--role-scope <scope>] [--stuck-onboarding]
python -m shared_context.behavior_agent_retire retire --config <current-config> --agent-id <agent-id> `
  --request-id <retire-id> --reason "<原因>" --confirm <agent-id> [--delete-foundry] [--role-scope <scope>] [--stuck-onboarding]
```

- `preview` 只讀：tombstone 是否已在、Foundry agent 的版本與 session 清單、會刪的角色指派，以及同一身分其他（不刪的）指派。
- `retire` 一定要 `--confirm` 再打一次同一個 Agent ID；先寫 tombstone，再刪 Foundry。每一部分都先讀、刪完再讀回：
  已經不在的回 `already_retired`／`already_deleted`，不是錯誤；還有剩下的回 `state=partial`（exit 1），重跑就接著做完。
- Foundry 已經手動刪掉的 Agent（2026-10-15 dev 的情況）一樣可以只補 tombstone；加 `--delete-foundry` 會回報
  `already_deleted`，沒有角色指派就是空清單。
- tombstone 寫入後還會繼續跑的只有一種：寫入**之前**就已開始的那一輪 instruction observer（連同它自己的 session
  keepalive），會觀察到那一輪結束或 API 重啟（下一次部署或 revision 重啟）為止。它不接新工作；其他 Agent 對它的
  keepalive 會跳過（只記一次 `HOSTED_SESSION_KEEPALIVE_PEER_RETIRED`），新的請求立刻回 `AGENT_RETIRED`。
- `--delete-foundry`（含 preview）遇到 roster 裡另一位 Agent（不論是否已退役）有相同 project endpoint 與 agent name，
  回 409 `TEAM_AGENT_CONFLICT`，不寫 tombstone、不刪任何東西。
- **卡在 `registered` 的 onboarding。** 例如選擇時釘住 skill X v1，之後 Team 核准了 X v2：同一個 request ID 每次重試都回
  `AGENT_ONBOARDING_SELECTION_STALE`（cause `PROFILE_SELECTION_SOURCES_CHANGED`），永遠到不了 `ready`，還占 2 個名額
  （roster＋`pending`）。這種 Agent 在下面任一條件成立時可以退役：它進入 `registered` 已超過 1 小時
  （`STUCK_ONBOARDING_SECONDS`，看 onboarding 列的 `registered_at`，和進入 `registered` 同一筆 CAS 寫入；本 PR 之前註冊、
  沒有這個欄位的舊列改看最後一次 create attempt 的 `claimed_at`），或 operator 明確加 `--stuck-onboarding`。
  preview 會多列 `stuck_onboarding`（request ID 與狀態）；`retire` 照樣要 `--confirm <agent-id>`。寫 tombstone 的同一筆 CAS
  把這個 request ID 從 `pending` 名單拿掉（tombstone 記 `onboarding_request_id`），所以還回 1 個名額；roster 名額與顯示
  名稱照樣不釋出，onboarding 列維持 `registered`、不改寫。之後用同一個 request ID 重送會回 410 `AGENT_RETIRED`。
  還沒有 tombstone 的 Agent，若進入 `registered` 不到 1 小時又沒加旗標，或還在 `admitted`／`creating`（加旗標也一樣；
  直接讀 onboarding 列，所以 `register_agent` 已寫、還沒進 `registered` 就中斷的也算），回 409
  `AGENT_ONBOARDING_IN_PROGRESS`、`pending` 不動；後者由 owner 用同一個 request ID 重送接著完成。
  **已經有 tombstone 的不一樣**：舊版 CLI 一般退役過（tombstone 沒有 `onboarding_request_id`），onboarding 停在
  `admitted`、`creating` 或 `registered`。這時重送只會回 410 `AGENT_RETIRED`，不會完成 onboarding，名額會一直被占住。
  對這種 Agent，preview 與 `retire` 都不再拒絕（不必加旗標、不看 1 小時），回原 tombstone（`already_retired`），
  preview 列出 `stuck_onboarding`；`retire` 再用一筆獨立 CAS（`team-agent-retire-release`，只寫 `pending`，寫前重核
  tombstone 與 onboarding 列沒變）把它從 `pending` 拿掉；重跑發現已拿掉就什麼都不寫。onboarding 列維持原狀態、不改寫。
- **不能退回本 PR 之前的 image。** 卡住退役之後，舊 image 的啟動核對看到 `pending` 少了一筆仍開著的 onboarding，回 409
  `TEAM_REGISTRY_INVALID`；舊 model 又是 `extra="forbid"`，讀到本 PR 之後進入 `registered` 的 onboarding 列（多了
  `registered_at`）或卡住退役的 tombstone（`onboarding_request_id`）也會啟動失敗（`MIGRATION_RECORD_INVALID`）。所以用本 PR
  的 image 註冊過 Agent 或做過卡住退役之後，該環境不能退版到本 PR 之前（依程式判讀，未在舊 image 上實測）。
- **onboarding 失敗留下的 Foundry agent。** 失敗（例如 `FOUNDRY_VERSION_FAILED`）的 onboarding 沒有註冊 Agent，所以沒有
  tombstone 可寫，但 Foundry 端可能留著那個 agent。用 `--failed-agent-name <agent-name>`（取代 `--agent-id`）：
  preview 只讀，列出 Foundry agent 的版本與 session，以及它屬於哪個 onboarding；`retire` 要 `--confirm <agent-name>`，
  用 `force=true` 刪掉它再讀回，不寫 Team 的 Table（不需要 `--request-id`／`--reason`）；除了 JSON 輸出，另記一行
  `FA_RETIRE_FAILED_LEFTOVER agent_name=… request_id=… outcome=…`。只接受：每個版本的 metadata 都指向
  同一個本 Team 的 onboarding、名稱正是那個 request ID 推出來的名稱、該 onboarding 是 `failed`；設定檔或 roster 裡任何
  Agent 的名稱回 409 `TEAM_AGENT_CONFLICT`，其他不符回 409 `FAILED_ONBOARDING_MISMATCH`／`AGENT_ONBOARDING_NOT_FAILED`。
  FA 只對 active 的版本指派角色，失敗的 onboarding 沒有角色指派要清。

```powershell
python -m shared_context.behavior_agent_retire preview --config <current-config> --failed-agent-name <fa-agent-...>
python -m shared_context.behavior_agent_retire retire --config <current-config> --failed-agent-name <fa-agent-...> --confirm <fa-agent-...>
```

- 每次執行都是雲端寫入，需要該環境的批准（stage 照 ADR-0008）。退役不釋出 roster 名額，也不釋出顯示名稱。

## AVM and local infrastructure checks

The Shared Context templates already use pinned official AVM resources for
Container Apps/Jobs, managed identities, Storage, ACR, VNet/private networking,
monitoring, Web PubSub and the Foundry account. PR #721 additionally uses
`br/public:avm/res/network/private-dns-zone/virtual-network-link:0.1.0` for the
backup Blob DNS link; it keeps the independent zone/link flags and does not
PUT an existing parent zone.

The [version inventory and contract audit](../docs/avm-pr721-source-verification.md)
documents all 16 pins (including the opt-in NAT gateway and its Public IP)
and retained FA orchestration, Foundry project/access,
custom RBAC/ABAC and special network wiring. The additive M3 container remains
raw because the candidate AVM would also write `metadata: {}`, unlike the
existing no-metadata-write contract. Existing stage defaults are unchanged.

For credential-free checks, place the verified
[Bicep 0.48.1 Windows binary](https://github.com/Azure/bicep/releases/download/v0.48.1/bicep-win-x64.exe)
at `.build-avm\bicep.exe` (the audit lists its SHA256), then use an isolated test
environment. No Azure CLI, login, API/Hosted packages or deployment is needed:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = Join-Path $PWD '.build-avm'
$env:TMP = $env:TEMP
$env:BICEP_CLI = Join-Path $PWD '.build-avm\bicep.exe'
$env:BICEP_MODULE_CACHE = Join-Path $PWD '.build-avm\cache'
python -m venv .\.build-avm\venv
& .\.build-avm\venv\Scripts\python.exe -m pip install --quiet --no-cache-dir `
  -r .\tests\infra\requirements.txt
& .\.build-avm\venv\Scripts\python.exe -m pytest .\tests\infra -q `
  -p no:cacheprovider --basetemp .\.build-avm\infra-tests
git diff --check
```

The new `validate-infrastructure.yml` workflow checks the same 19 entrypoints
and deployment contracts, with pinned Bicep and only `contents: read`.
Its YAML/credential-free contract was tested locally; the GitHub-hosted run
and required-check configuration have **not** been verified or enabled here.
The final combined local run was **163 passed**, one Starlette/AnyIO warning;
the exact command and baseline comparison are in the audit. Public registry
restores and local compilation are not Azure deployment evidence.

## Verified against

- PR #721 follow-up, baseline `2944a7eb9b62ff099725d3052d7e44fad3da70f6`
  (`neallee2012-pr721-p2-fixes`): checked `prepare_environment.resolve`／
  `_parameters`, `stage-monitoring.bicep`, subscription and ACA preflights, the
  scoped template export policy, selection API, and `select_all_capabilities.py`.
  The three direct offline test files passed (**42 passed**). The broader
  requested test set reported **183 passed, 37 setup errors** because the
  worktree lacks its pinned offline tokenizer cache; no Azure/cloud validation
  or deployment was performed.
- 22:40 之後的部署教訓與流程修改（本分支 `a881a8ff` 加本次未提交修改）：
  已讀 `prepare_environment.resolve`／`_parameters`、`foundry.bicep`、
  `private-state-network.bicep`／`foundation.bicep`、`model-catalog.json`、
  `select_all_capabilities.py`，以及 v20 的 native-turns environment／prepare／
  reconcile routes。離線測試一次跑
  `tests/infra/test_shared_context_avm.py`、`test_deployment_config.py`、
  `test_stage_select_all_capabilities.py`、`test_stage_prepare_native_sessions.py`、
  `test_preflight_subscription.py`，結果 **65 passed**。另對本環境唯讀跑
  `preflight_subscription.py` 兩種模式，都是 `ok=true`（staticIp `203.0.113.10`、
  平台 LB／Public IP 存在，quota 不需新增）。新增的兩個 AVM 版本已核官方
  repository tag 與 MCR tag list。這一輪沒有雲端寫入。
- 22:2x Portal 修復：A／B 各 POST `/select` 一次，只套用 stage
  `conversation_defaults` 的 settings（Responses lane），role 與 capabilities 不變；
  真 Portal 新對話三個 receipt 都是 `completed`、`failure_code=null`、
  `foundry-responses-v1`，usage 已計量。另依「對齊 staging」批准補上
  `gpt-5.4-mini`、`gpt-6-luna` 部署，並把 `gpt-4.1-mini` 容量從 10 調到 500。
  舊的 completions lane 對話不會搬遷，仍會失敗。
- 20:38 接續：已核 `configured_prompt_override`、registered peer policy、
  `export_template`、`NativeInstructionControl.refresh_routing`、native capture 與
  原 `select_profile`；目前三個 runtime modules 與 HEAD 無 diff。
  新 signed fresh API 回歸覆蓋 v1／v2 defaults、原拒絕、缺批准零寫入、
  B 主人精確選版、export／capture 一致、原 policy 不變、無模型／source／backup。
  設定 source `PYTHONPATH` 後，`test_fresh_installation.py`、
  `test_agent_scope_registry.py`、`test_conversation_profile_selection.py` 一次 **72 passed**。
  20:29 真 API 唯讀兩份 selection／template 狀態與原拒絕相符；未選版或部署。
  固定 SDK 2.3.0 的 project OpenAI URL／scope 與 runtime 一致，未找到須補 transport header
  的證據；官方 Foundry skill／troubleshoot 只唯讀參考，本機未安裝或執行 setup。
- 19:38 接續：兩份正式 prepare／原 reconcile 均通過，缺 reader loop 已綠；
  A 的真模型回合為 FAIL／unknown，不能回填成對話 PASS。已讀固定
  `NativeSdkExecutor` 的 provider／token scope、`DirectCompletions` 的去敏 refusal log、
  Portal `NativeInstructionControl` 的 route／submit 與真正 `NativeAdapter`。
  SDK 2.3.0 `get_session_log_stream` 取得上述固定安全行；只保存 identifiers／counts，
  不保存 provider message、prompt、token 或 reasoning。ARM 直接核模型部署、
  project MI 既有角色、A／B 各自角色；未拿 A 的角色讀回代替 B。
  原 native usage 為一個 unmeasured request；accounting read 的 model pending／metered
  為 null、`all_fields_present=false`，不宣稱模型免費、帳本齊全或已清除未知。
  最小兩筆 project-role plan 沿既有 helper 產生，未執行。官方 permissions 與
  固定 SDK scope 已核對，權限變更的 green、B 模型與修復後真人畫面尚未取得。
- 21:04 現行存取對照：本分支 `bcdb3001` 上追加紀錄，部署 core／images 未變。
  新 A RID 的正式 submit／read 仍 unknown；21:01 唯讀兩份原件，各一筆未量測請求，
  tokens=null。已讀固定 SDK 2.3.0 `get_session_log_stream` 的 byte stream 接點；
  限時官方 log 沒有取得第二回合固定安全錯誤行，不外推首則 HTTP 401。
  本輪沒有 IAM、B 選版或 runtime 修改；文檔同步不重跑無關離線測試或模型。
- 21:18 診斷補證：本分支 `e82603f1` 上追加紀錄。已對照
  `behavior_model_direct._error_fields`／HTTP failure logger 及原安全樣本，
  修正 Git 外擷取器並跑七個離線測項；再唯讀官方原 session，
  取得新 intent 時間窗的 401。真 request read 仍 RED，沒有新增模型回合。
  Graph 三個精確物件與官方 Hosted permissions／Agent identity 文件已核，
  未取得 VM token claims、未授權 IAM 或 B 選版，未改部署或 runtime。
- 21:57 staging 比較：沿 `deploy/stage/stage.json` 核實共用 account 與 project，
  SDK A／B principal、兩租戶 ARM 本層／繼承角色與實際 role definitions／ABAC，
  及兩個 API 的實際 onboarding ENV；安全原件在 Git 外，交叉核對通過。
  已讀 `behavior_agent_setup.NewAgentSetup.pending`／`roles_pending` 與 README #468，
  不把 ENV 當成已配權。此輪模型 0、雲端寫入 0，沒有修復後回覆／Portal PASS；
  原先準備的真人模型對照未執行。官方 Foundry／RBAC skill 僅唯讀，未安裝或跑 setup。
- 22:19 接續：真人 project 模型對照已執行一次且預期回覆／用量均保存；
  角色單次 PUT＋完整 ARM 分頁讀回證明 project 基線與 staging 相同，
  API MI 的 ABAC 原樣保留、API key 仍停用。B 正式 DTO select／checked receipt、
  原 command reconcile、selection／template hash 及 export 均核同 Linux 提案。
  A 單一角色的新 Portal 回合仍 RED／unknown，沒有回填或重送；IAM 對齊後
  模型／Portal 重驗待傳播窗口，沒有以 ARM／選版成功宣告目標完成。
- 19:18 首次 session 阻塞：本分支 `a923ff55`，部署 core `1e7ab3bd…`。
  已讀固定 v20 `NativeSessions._platform_reader`／`environment_view`／`prepare`、
  `NativeTurnGateway.submit`／`_prepared_before`、正式 prepare／reconcile routes，
  以及工作台 onboarding `_prepare`；提交入口不會替從未準備的 Agent 隱式 bootstrap。
  兩次真 API GET、原 RID read 與去敏 API log 均重現缺 reader／`NATIVE_SESSION_CHANGED`；
  私有 `probe_native_runtime.py --require-ready` exit 1，明確 RED，不記為 PASS。
  既有 `test_native_session_prepare_api.py` 的 unprepared、same-MI preparation、
  unknown create／no implicit preparation 六案通過；相關三個 runtime modules 與
  固定部署 source 的 AST 相同，但離線替身不是 Hosted 或模型驗收。
  本輪沒有 cloud 寫入、session 建立、模型呼叫或原 `Hi` 重送。
- 首次安裝：沿 `run_fresh_job` 與固定 v20 fresh operator，一次精確批准 apply；
  GPT-6 Luna 核 Azure 身分與有效窗口，回報原 apply／reconcile 均 `Succeeded`，
  兩者同 receipt `5527b046…`、`initialized`、`budget_reset=false`。
  原 intents、authorization、execution logs 及結果保留私有目錄；未重跑 plan、
  apply 或 smoke。Parent 已讀 `image_source`／`desired_source`／`sync_agent_version`／
  `route` 與固定 AVM API 參數，準備 audience-only 接線；尚無該段雲端終局。
- 17:09 批准後：已讀固定 v20 `behavior_auth`／`auth` 的 human scopes、
  exact tenant／audience、configured Agent oid 門禁及
  `FreshInitializer.plan`／`BehaviorStore.require_empty_table` 的全表唯讀契約；
  `workstate` ARM resource 已讀回存在。GPT-6 Luna 執行後回報 API manifest、
  SP、個人 consent 及去敏 token claim 核對；私有新配置以同 core 的
  `ServerConfiguration`／`make_runtime.inputs`／`_settings` 核對，只改 audience。
  單次 cloud plan 取得 core、plan／config／input digest、全表空狀態與零寫入結果；
  依原執行 scalars 重建 `FreshInstallationPlan`，完整 digest 相同，未重送 Job。
  官方依據：[token claims](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference)、
  [個人 delegated grant](https://learn.microsoft.com/en-us/graph/api/oauth2permissiongrant-post?view=graph-rest-1.0)。
  這些結果不代表 Table 已寫入或 API／Hosted 已切新設定。
- 16:01 接續：文件前一版 `901470d2`；固定 v20 source 加 #721 overlay 的 core
  `1e7ab3bd…`，兩個 ACR manifest 已重讀。CAE／平台 LB／Public IP、
  smoke execution／marker／core、四個 Hosted version／selector／身份／API URL
  已核對；API ARM 成功但 replica crash、health timeout，不當部署完成。
  已讀固定 image source 的 `create_production_app`／`ServerComposition.installation`、
  `installed_origin`／`BehaviorStore._guard`，並以同 image／配置的無模型 Job
  取得 `BEHAVIOR_NOT_INITIALIZED`；API audience token precheck 為 `AADSTS500011`。
  原始配置、intents、readbacks、去敏診斷與 operator callback 留在 Git 外。
  未建立 Entra app／SP，未初始化 Table、未 follow 或做模型驗收。
- 本次方案與執行回饋（2026-10-07）：程式基準為本分支 `3cee4303`，另有先前 NAT Bicep／infra
  tests WIP。已讀 `foundation.bicep`／`private-state-network.bicep`、
  `behavior-job.bicep`、`prepare_environment.resolve`、
  `ci_deploy.guard`／`_sync_script`／`sync`／`api`、
  `run_fresh_job.start`／`script`；本次提交範圍只含文件，不採用 NAT 診斷 WIP。
- Azure 14:50 +08 唯讀核對 feature、兩個 RP、CAE、infrastructure RG 與 API；
  另讀 01:15–01:25 UTC 平台 Public IP 失敗 Activity Log，結果見本輪 progress。
  官方 feature／ACA networking 文件查閱日期：2026-10-07。
  本輪未使用本機 `microsoft-foundry` skill／Foundry MCP，只唯讀閱讀官方 skill，
  未安裝 skill 或執行 dependency setup。使用者明確批准後已執行 Network
  provider re-registration、environment-only what-if 與一次 reconcile；
  15:06 +08 平台仍未恢復，監控／subnet／NAT 維持。未執行 Job smoke／sync、
  Hosted 建版、API 更新、模型或 live 功能驗收。後續經刪除影響確認，已刪
  指定失敗 App／Job／CAE；15:46 +08 直接 ARM GET 全為 `ResourceNotFound`，
  受管 infrastructure RG 已移除，主 RG 保留。
  清理後 resource inventory 確認另外 22 個資源仍存在，資料不在刪除範圍；
  不用下列歷史離線結果當作本次雲端修復證據。
- Code baseline: PR #721 branch `neallee2012-fa-agent-v18-deployment`,
  `1f692bda` plus this blocker fix (2026-10-07).
- Read: `seed_agents.seed`/`_of_definition`/`main`,
  `behavior_agent_versions.definition_sha256`/`definition_matches`/`sync_all`/`main`/
  `HostedVersions.current`, `behavior_code_identity.code_sha256`, and
  `ci_deploy.core`/`_sync_script`.
- Windows local validation, using the pinned test dependencies and verified
  `tiktoken==0.11.0` cache:

  ```powershell
  $env:PYTHONUTF8 = '1'
  $env:PYTHONDONTWRITEBYTECODE = '1'
  New-Item -ItemType Directory -Force .\.build-v18 | Out-Null
  $env:TEMP = Join-Path $PWD '.build-v18'
  $env:TMP = $env:TEMP
  $env:TIKTOKEN_CACHE_DIR = Join-Path $PWD 'src\fa-shared-context-poc\.venv\fa-tokenizer-cache'
  & .\src\fa-shared-context-poc\.venv\Scripts\python.exe -X utf8 -m pytest `
    .\tests\shared_context\test_stage_version_publication.py `
    .\tests\shared_context\test_endpoint_versions.py `
    .\tests\shared_context\test_agent_versions.py `
    .\tests\shared_context\test_deployment_config.py `
    -q -p no:cacheprovider --basetemp .\.build-v18\verified --tb=short
  git diff --check
  ```

  Result: **134 passed**, one Starlette/AnyIO deprecation warning; diff check
  passed. The pre-fix focused regressions failed 16 cases, including the exact
  `fa-v18-a: version not settled` symptom and invalid core reaching credentials.
  The first broader run had 125 passes and one missing-tokenizer setup error;
  the pinned prerequisite was restored, not bypassed.
- The SDK readback and v18 metadata fixtures are synthetic and offline. No Azure
  deployment, Hosted creation retry, cloud configuration/resource/permission
  change, model run, or live acceptance was performed.
