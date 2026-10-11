# Teams personal-chat 部署前置作業

**13:17 更新：** 已依使用者最新批准，以1／3次重測取得真 `connector_http` 個人路由；獨立 Azure 程序核對 tenant／指定 admin／Bot／personal conversation 相符。成功後停止剩餘2次，沒有 Bot 回覆、一般工作或模型執行。見 `docs\evidence\teams-personal-route-20260917.json`；這不是 native sender／provider receipt PASS。

**09/17 實際進度：** BotService F0／MsTeamsChannel 與獨立 capture-only callback 已部署，真 ARM metadata 及公開 callback／notice 網址已核對；沒有建立新 identity／secret／role。使用者後續明確批准組織發布與指定 admin 個人安裝，已用同一個核對過的 package 完成，Teams UI 顯示 `成功新增!` 並開啟個人 App。未傳訊；設定握手仍待另行批准，native sender 與一般工作未開啟。受控原件在 `.azure\teams-personal-0917\capture-native-prerequisite.json`、`bot-metadata-receipt.json`、`personal-installation-receipt.json`；下面是工具與操作契約，不把 metadata 或安裝通過等同 Teams 端到端驗收。

這是**可重複的離線設定／package 建置與唯讀核對工具**，不是部署或 Teams Native PASS。工具沒有 create/update/delete、登入、role assignment、Graph、安裝、傳訊或列印 token 的路徑。Azure Bot resource／Teams channel 由父工作階段依授權另行部署；只有真實 Connector 簽章驗證成功的指定使用者 personal handshake，才能建立精確 route。即使建立 route，仍不自動開啟 business admission 或 sender。

## 固定範圍與官方契約

| 項目 | 固定值／含義 |
| --- | --- |
| Subscription | `00000000-0000-0000-0000-000000000012` |
| Tenant | `00000000-0000-0000-0000-000000000013` |
| Resource group | `rg-faexample` |
| 唯一預定 Teams 測試使用者 | OID `00000000-0000-0000-0000-00000000001b` |
| 現有 UAMI | `id-facontext-api`，不是新身分 |
| 現有 callback host | `Microsoft.App/containerApps/ca-fa-behavior-poc`，本範本不改其部署 |
| Bot 身分 | `msaAppType=UserAssignedMSI`；`msaAppId` 使用 UAMI **client ID**，不是 principal/object ID |
| UAMI 綁定 | `msaAppMSIResourceId` 使用既有 UAMI 的完整 ARM resource ID |
| Teams app package ID | 本機穩定 GUID；不是新 Entra app，也不取代 bot client ID |
| 範圍 | manifest 只有 `personal`；入口另強制 tenant／admin OID，不能只靠 manifest 限制收件人 |

2026-09-17 核對的官方來源：

- [Azure Bot resource／UAMI](https://learn.microsoft.com/azure/bot-service/abs-quickstart?view=azure-bot-service-4.0)：允許選既有 UAMI，`MicrosoftAppPassword` 不適用。本文使用現行 Bot Service REST／ARM 契約，沒有安裝退休 SDK。
- [BotService 2022-09-15](https://learn.microsoft.com/azure/templates/microsoft.botservice/2022-09-15/botservices)：`msaAppId`、`msaAppMSIResourceId`、`msaAppTenantId`、`UserAssignedMSI`。
- [MsTeamsChannel 2022-09-15](https://learn.microsoft.com/azure/templates/microsoft.botservice/2022-09-15/botservices/channels)：`channelName`、`isEnabled`、`enableCalling`。
- [Connector authentication](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-authentication)：服務 JWT 與 outgoing bot credential；公有雲 scope `https://api.botframework.com/.default`。
- [Teams Activity 範例](https://learn.microsoft.com/microsoftteams/platform/bots/build-conversational-capability?pivots=teams-sdk-python)：本公有雲 personal profile 只接受 `28:<bot client ID>` 的 bot account 格式。保存的仍是驗證後原始值；若實際 provider 使用不同格式，拒絕並另核對契約，不猜替代 ID。
- [Package 格式](https://learn.microsoft.com/microsoftteams/platform/concepts/build-and-test/apps-package)、[上傳 custom app](https://learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/apps-upload)、[custom app policy](https://learn.microsoft.com/microsoftteams/teams-custom-app-policies-and-settings)。
- 固定 [manifest v1.19 schema](https://developer.microsoft.com/json-schemas/teams/v1.19/MicrosoftTeams.schema.json)；這是本 personal-only POC 所選 GA 版本，不宣稱是最新版。

官方 schema 的本次完整 bytes SHA-256：
`cd5815382f0810ff947f94debd4d03a3a3f62a2a671b090ef2b8c12e761de961`。
`--schema-file` 只接受這個本機快照；不自行連網抓 schema、不接受較寬鬆的替代 schema。若官方檔案更新，須另核對，不能只換 hash 通過。

**AVM 選型核對（2026-09-17）：** 已讀取完整[官方 Bicep resource module index](https://azure.github.io/Azure-Verified-Modules/indexes/bicep/bicep-resource-modules/)，`avm/res/bot-service/bot-service` 當時列為 `Proposed`、版本 `N/A`，不是可固定版本使用的已發布模組。因此本切片依官方 ARM `2022-09-15` 契約直接宣告 BotService 與 MsTeamsChannel，不改採功能更廣的 customer-chatbot pattern。與通用模組的差異是只引用既有 UAMI、使用 F0、停用 local auth／calling／streaming，不建立 App Service、secret、role、診斷目的地或其他產品能力；Container App 繼續沿用專案已固定版本的 AVM。

## 1. 取得公開設定值，不取得 secret

使用**既有、專用且經授權的** Azure CLI context。工具要求 `-AzConfigDirectory`，先核對 `AzureCloud` 公有雲及 tenant／subscription，再讀其他資源；不執行 `az login` 或 `az account set`。沒有可用登入時由使用者自行完成正確 tenant 的登入，不能借用其他 WorkIQ tenant。

選一個已具備 `src\fa-shared-context-poc\requirements-teams-operator.txt` 相依套件的 Python。PowerShell wrapper 的 `-Python` 可指定其完整路徑；缺套件時先依這份固定 manifest 安裝，工具不自動安裝。

```powershell
.\deploy\teams\Invoke-TeamsPersonal.ps1 `
  -Action identity `
  -AzConfigDirectory C:\approved-azure-context `
  -Python C:\approved-python\python.exe
```

只輸出經核對的 UAMI resource/client/principal/tenant ID；不輸出 access token、account username 或原始 Azure CLI 錯誤。若身分不存在／欄位不同，明確停止，不建立替代身分。

複製 `personal.example.json` 為操作者自己的設定檔，填入：

- 唯讀核對取得的 `uami_client_id`、`uami_principal_id`。
- 父工作階段確認的 callback **origin**，例如 `https://<實際容器應用程式 FQDN>`。不可填 localhost、IP、HTTP、port、path、query 或 credential；工具固定附加 `/behavior/v1/teams/activities`。這不是 incoming 的 `serviceUrl`。
- 真正可供測試者閱讀的 `website_url`、`privacy_url`、`terms_url`；工具不假造已存在的隱私／條款頁。
- 經父工作階段核對的 FA `team_id`。

範例中的 `null` 刻意無法通過驗證。`route` 必須保持 `null`，`native_admission_enabled` 必須保持 `false`；不得由操作者填 conversation ID、Teams user ID 或 service URL。現有 FA API app ID 不是 bot 身分，不拿它代替 UAMI client ID。

## 2. 離線產生範本參數與 Teams package

```powershell
.\deploy\teams\Invoke-TeamsPersonal.ps1 `
  -Action build `
  -Config C:\poc-inputs\personal.json `
  -OutputDirectory C:\poc-output\teams-personal-v1 `
  -SchemaFile C:\poc-inputs\teams-v1.19.schema.json `
  -Python C:\approved-python\python.exe
```

輸出目錄必須尚不存在。產物包含 `manifest.json`、原創 192×192 color／32×32 transparent white outline PNG、只含這三個檔案的 `fa-teams-personal.zip`、`bot.parameters.json`、`personal.config.json`、`bot.environment.json`。相同輸入產生相同 zip；不覆寫已有檔案。

`bot.environment.json` 只有公開的 UAMI 身分設定，不含 password。沒有 schema 快照時可省略 `-SchemaFile`，但結果會明記 `official_schema_validated=false`，不能當作完整官方 schema 核對。

## 3. 父工作階段／既有授權 operator 的最少部署動作

本節是**待執行的明確人工／父工作階段動作**；工具不會執行。

1. 確認 `Microsoft.BotService` provider 已註冊。若未註冊，只有具有該 subscription provider-register 權限的既有 operator 可處理；唯讀工具只報 blocker。
2. 確認父端已在核准的 HTTPS host 掛上下面的 capture-only router。確認 UAMI 已附加到該 host；缺少時由父工作階段按原部署流程補上。本範本不改共享 host／server／migration。
3. 審核 `infra\teams\personal-bot.bicep` 與產生的 parameters，在**固定 subscription／RG** 以 incremental deployment 建立 `azurebot` F0 resource 及 `MsTeamsChannel`。若同名 bot 已存在但綁定不同身分或 endpoint，不覆寫，先核對。Bicep 不建立 identity、Entra app、secret、federation 或 role assignment。

父工作階段審核後的命令形狀如下；這不是工具自動部署入口：

```powershell
az deployment group create `
  --subscription 00000000-0000-0000-0000-000000000012 `
  --resource-group rg-faexample `
  --name fa-teams-personal-prerequisites `
  --mode Incremental `
  --template-file .\infra\teams\personal-bot.bicep `
  --parameters '@C:\poc-output\teams-personal-v1\bot.parameters.json'
```

最少 Azure 操作權限由既有 operator 持有：讀取既有 UAMI，固定 RG 的 `Microsoft.Resources/deployments` 與 `Microsoft.BotService/botServices`、`botServices/channels` 必要 read/write。只有 host 尚未附加 UAMI 時才涉及 host write 與該 UAMI 的 `assign/action`。不要求新增 Global Administrator、Graph application permission 或 tenant-wide consent。

F0 是 Bot resource SKU，不代表所有 host／儲存／流量均零費用；所有支出仍屬原 US$100 POC 總額，tag 不是帳單硬上限。若 Teams channel 要求額外 terms 接受，由有權的人另行處理；範本不替使用者填 `acceptedTerms=true`。

## 4. 唯讀核對部署 metadata

```powershell
.\deploy\teams\Invoke-TeamsPersonal.ps1 `
  -Action verify `
  -Config C:\poc-output\teams-personal-v1\personal.config.json `
  -AzConfigDirectory C:\approved-azure-context `
  -Python C:\approved-python\python.exe
```

只執行固定範圍的 `az account show`、`az identity show`、`az provider show`、`az resource show`。核對 UAMI、BotService identity／endpoint／F0／停用 local auth、已啟用且無 calling 的 Teams channel、host 的 UAMI attachment 與 HTTPS ingress metadata。

成功只代表 `prerequisite_metadata_matches`，並保留觀察時間、明確 resource IDs 及已核對 metadata 的 SHA-256。這些讀取不是跨資源原子快照，也不是 signed native readiness。仍明記 installation、callback reachability、credential acquisition、signed Connector route 為 `NOT_RUN`，native gate 為 closed。HTTP reachability、路由存在與權限不能從 ARM metadata 推定。不得執行 `az account get-access-token` 作為這份工具的輸出。

## 5. 指定使用者／Teams administrator 的最少動作

只有 OID `00000000-0000-0000-0000-00000000001b` 在 tenant `00000000-0000-0000-0000-000000000013` 的 personal chat 是預定目的地。

1. 有權的 Teams administrator 確認這個**特定 app 與指定使用者**的 app availability／custom app upload policy。若 custom upload 被停用，依該 tenant 實際管理方式核准此測試者，或由 admin 將同一 package 放入組織 catalog 並只提供給此使用者；不默認開放全租戶或所有 app。現有 `TEAMS1=Success` metadata 不等於 policy 或安裝通過。
2. 指定使用者在正確 tenant 的 Teams client：**Apps → Manage your apps → Upload an app → Upload a custom app**，選產生的 zip，再 **Add → Open** personal app。若該入口被 policy 隱藏，不繞過；由有權 administrator 處理。不可選 team、group chat 或其他人。也不改用公開 Store 發布。
3. 在父端 capture-only endpoint 已就緒、安裝及一次 handshake 訊息另獲授權後，**由該使用者本人**送出精確文字 `FA Teams route enrollment`。這是設定控制訊息，不是交辦工作；目前不發 bot 回覆。

保留的最少證據：實際 package ID／version／hash、指定使用者 personal 安裝的 UI／管理端證據、父端以真 Connector JWT 驗證的 body digest／provider activity ID／精確 bot-user-conversation-serviceURL route receipt。不得保存 bearer token；不能拿手填的 activity JSON、Graph／Storage 可讀、license、app catalog 上傳成功或 HTTP 200 代替 signed Connector route。

這個 app 不要求 `webApplicationInfo`、SSO、Graph scopes、RSC、file consent、calling 或 `Behavior.Approve`。如果政策／安裝畫面出現額外 permission 要求，先停止核對 package，而不是一併授權。

## 父端的具體 composition interface

先部署 capture-only，沿用同一受保護 `BehaviorStore`，不改共享 server 檔：

```python
from shared_context.teams_deployment import load_config
from shared_context.teams_personal import PersonalRouteEnrollment, PersonalTeamsEntry

config = load_config(config_path)
enrollment = PersonalRouteEnrollment(store, config, clock=trusted_utc)
entry = PersonalTeamsEntry(enrollment)
app.include_router(entry.router())
```

只掛**一個** `/behavior/v1/teams/activities` router。未掛 runtime 時，一般訊息明確拒絕；只有指定 personal handshake 會驗簽並保存一筆不可變 enrollment。`capture` 不建立 work／obligation、不呼叫模型或 sender。注入的 JWKS test transport 永遠標成 `offline_contract`，重建物件也不能升成 live 證據。

真正的 work 仍由父工作階段既有流程提供：

```python
from shared_context.teams_personal import (
    TeamsParentSeams, UamiBotToken, compose_personal_channel,
)

seams = TeamsParentSeams(
    resolve_admission=server_admission_resolver,
    resolve_context=current_execution_owner_resolver,
    process_work=existing_durable_assistant_job_consumer,
    resolve_send=current_sender_and_human_grant_resolver,
)
channel = compose_personal_channel(
    enrollment,
    parent_runtime_registration,
    seams,
    bot_token=UamiBotToken(config, clock=trusted_utc),
)
entry.attach_runtime(channel.runtime)
```

`resolve_admission` 回 `TeamsAdmission(context, root_request_id, work)`；`resolve_send` 回 `TeamsSendContext(context, grant_id)`。Work callback 必須取得既有持久 owner/job claim，不能每次 callback 直接重跑模型。人工 grant、readiness／result verifier、quota、sender owner／epoch、route audience／artifact ACL 仍由父端既有 authority 管理。UAMI credential 是 lazy server dependency，只接受 Connector scope；不使用 `DefaultAzureCredential`、FA API token 或 delegated user token fallback。

若工作承諾固定成果，父端另傳 `artifact_links` 與 `artifact_origins` 給 `compose_personal_channel`；沿用原 Teams Adapter 的 M3 固定版本 URL／hash／scope gate。未提供就明確拒絕 artifact delivery，不偷偷刪掉成果。這不會開啟 Teams file-upload／OneDrive consent 能力。

**native 預設仍關閉。** 上面未傳 `native_registration` 的範例不會開 native。已實作的 current-installation gate 與 sender 接法如下；不是把 `offline_contract` 配真 network transport。缺 live enrollment 仍回 `TEAMS_PERSONAL_LIVE_ROUTE_REQUIRED`／`TEAMS_PERSONAL_ROUTE_REQUIRED`，缺註冊回 `TEAMS_NATIVE_DISABLED`，目前安裝的 pull-only parent 則回 `TEAMS_PARENT_CHANNEL_INCOMPATIBLE`。

## Native sender／工作入口：已完成的程式與尚未接上的配置

這一批只做 CODE／OFFLINE，沒有 Azure、Bot、瀏覽器、模型動作或部署，也未改目前安裝設定。原個人 route `efda177a6522a4463af0aeea5c2fbe71795c9244408c1a2cd892bd05f8591c7d` 直接讀同 authority 的 signed witness 與 capture binding；operator JSON **不能**補 conversation/service URL 當證據。已完成的組織發布、個人安裝及 `acceptedTerms=true` 不重做；握手成功後作廢的 2 次不復活。接上工作 runtime 的 personal entry 明確拒絕再做 enrollment。

### 真正部署時的接線順序

以下只有在 **parent 已合法支援 Teams delivery contract** 時可用；目前 SDK pull-only 設定會在 composition 第一關拒絕，不會先取得 Bot token 或建立工作。

```python
from shared_context.teams_native import compose_installed_personal_channel

# parent 是原 server composition；enrollment.store 必須就是 parent.runtime.store。
# seams 使用前節原 worker/job/result/owner/grant callbacks，不建立替代 authority。
channel = compose_installed_personal_channel(
    parent, enrollment, seams,
    evidence_id=approved_native_evidence_id,
    verify_current=independent_current_teams_observer,
    artifact_links=authorized_fixed_artifact_links,
    artifact_origins=approved_artifact_origins,
)
app.include_router(channel.entry.router())  # 同一路徑只能掛這一個 router

# API 與受監督 consumer 先啟動，admission 保持 false。
# 獨立 observer 真正核對下列前提後，才保存既有證據：
channel.adapter.publish_installation()
channel.runtime.enable_admission()

# 既有 durable consumer 在同 root 下交付指定 obligation：
channel.runtime.consume(obligation_id)
# 或由既有 timer/consumer 執行有上限的 drain，不自動建立新模型工作：
channel.runtime.drain(original_root_budget_id, limit=4)
```

這個 wrapper 自動綁 `parent.require_current_native_evidence`，沿用其 installation/history/protection/worker/readiness 檢查，不另造 NativeGuard。低階 `compose_personal_channel(..., native_registration=TeamsNativeRegistration(...))` 也存在，但註冊只能由受信任 server 組成，不能從 activity、HTTP enable 參數或模型文字建立。`verify_current(evidence_id)` 必須每次重新讀目前獨立觀察，不能用啟動時的常數當永久證據。

兩個 composition 都使用 `TeamsDeliveryCore`：原 admission／quota 轉移不改，只把 native current pointer、installation、route/capture 加進原 admission EGT 的 read-set，並在該交易的 quota 檢查內再驗一次。readiness 通過後才撤權也不能接單。唯有更新獨立觀察、沒有改配置時，原 admission request ID/read-set 與固定 payload 仍可沿用；不因換 evidence ID 重建工作。直接用普通 `DeliveryCore` 啟用 native Teams 會被拒絕。

`TeamsNativeEvidence` 必須有以下真實輸入；hash 是固定原件的 SHA-256，不是以 hash 取代觀察：

- 同一 installation row 的完整 `data` digest、目前 server configuration SHA、原 `control_binding`，以及 Teams `DeliveryCore.configuration_digest()`。
- 原 `PersonalTeamsConfig`／`TeamsSettings` digest；原 signed `_RouteWitness` 的 material SHA，與原 `teams-capture-route-binding` 的 data SHA。capture 可以是先前 installation 的歷史來源，但原 material／route／authority 不得改寫或偽裝成新的 activity。
- 已核對 Bot resource／MsTeamsChannel metadata 的 `bot_registration_sha256`、指定 admin 個人安裝證據 `personal_installation_sha256`、原 package SHA、callback URL。Bot app/client `00000000-0000-0000-0000-000000000027` 與 principal `00000000-0000-0000-0000-000000000028` 必須吻合原 UAMI。
- 目前有效的 `observed_at`／`expires_at`，及 **Teams consumer** 真實的 `ReadinessEvidence`：同 configuration/control binding、protection/consumer/timer versions，完整 `atomic_control`、`inline_retention`、`durable_consumer`、`timer_redrive`、`bounded_scan`、`ownership_recovery` coverage。舊 SDK 的 readiness 不可改 channel/digest 後冒充。
- 當前 `TeamsRouteAccess`：指定收件人、有效 sender owner IDs、全部固定成果版本的讀取範圍；另走既有人工批准，取得 exact `TeamsSendPlan.description_digest`／adapter digest 對應的 grant。收到 Teams 訊息不會自動給 `Behavior.Approve`。

`publish_installation()` 只在既有 authority 新增 `teams-native-current` 與不可變 `teams-native-evidence`，不 bootstrap／reset root、pool、budget。較新的獨立證據可更新 active pointer；被撤銷的 pointer 不會因重播舊 publication receipt 或換 evidence ID 自動復活。publication 也不會自動打開 admission。

預設 Bot credential 使用原 UAMI 的 `ManagedIdentityCredential(client_id=...)`，沒有新 secret／permission、Graph 或使用者 token fallback。native 不接受 injected `MockTransport` 或測試 credential。第一次 token 冷啟動在 `_consume` 前完成，之後再核對當前批准；wire window 仍是 2 秒，亦不得超過 token／grant／native evidence 的期限。HTTP 層一次 exact POST，禁 redirect／retry，不用 `Activity.id` 假裝冪等。consumer 必須保留原 delivery ID，不能失敗就另造 ID。

完整 observer／NativeGuard 核對放在 consume 交易決策內、2 秒視窗開始前；wire 前只重新讀已發布的 current pointer 與 installation，避免在短視窗內重跑整份歷史或冷啟動 credential。若這些最小讀取／提交仍超過 2 秒，保持 unknown／不送，不能自行放大時限。consume 之後的其他撤權仍屬 in-flight 界線，不宣稱能追回已發生的效果。

### Receipt 與失敗核對

`channel.adapter.inspect(delivery_id)` 只讀原不可變 plan、consumed gate 及保存的 HTTP witness；`reconcile(delivery_id)` 只用這份原始證據核對／完成原控制提交，不做 HTTP。`send(delivery_id)` 遇到既有 claim 也只走 reconcile。provider 已接受但回應丟失、gate ACK 丟失、receipt ACK 丟失或寫入失敗，都不重 POST、不退原預留。installation 過期或被撤銷，不抹掉過去 provider receipt；查歷史不重新取 token、不把歷史 witness 當新的發送許可。

真 sender 回條可具有 `evidence_mode=connector_http`、`native_evidence_id`、`native_evidence_digest`，但只支持 `provider_accepted`，不等於 UI render／`client_received`／`human_read`。本批測試雖覆蓋 native 分支，其 network、MI、JWT、Table 都是本機替身；測試及既有結果欄位仍維持 `actual_environment=NOT_RUN`，不能把它們填進真部署證據。

### 目前 SDK r2 的具體不相容，以及最小後續工作

唯讀核對 `.azure\v12-sse-current-0917\new-config.json`：原安裝 SHA `868a0560e741ac886649bdfbd65fbb152d8a9946870e13b01fff098f14d4056f`，authority/workspace/partition 都是 `behavior-c1-0916`，entry `behavior-c1-api`，只有 `authenticated-pull`／`fa-pull-inbox-v1`／`client_received`。這是程式契約限制，不是 Azure 許可或安裝失敗。

`RuntimeContract.channel` 只有一個值；其 digest 綁住共用 quota/index/obligation。直接 `replace(channel=Teams)` 會得到另一個 digest，原 `_Bound` row 會回 `DELIVERY_CONFIGURATION_MISMATCH`。所以本批 **未** 建第二套 root、未改 installed config、未套用任何 rotation、未拿 pull receipt 當 Teams 交付。

下一階段最小合法方案需要一個**另外審核的同 authority 多通道設定擴充／變更接口**，不是放寬現有 SDK rotation：

1. 原 authority、root `c1-validation-0916`、quota/index、worker pool、所有舊 unknown/cancelled/sealed 與 configuration/history hash 原樣保留；新增明確的 Teams channel binding，讓新 obligation/outbox 取用自己的 channel/version，但共用原 root 計數。現有讀取必須繼續按原 configuration 驗舊 rows，不能批次改標記。
2. 配置變更以獨立、可核對的一次 control transition 安裝這個 binding；parent 的 NativeGuard／current installation 驗證明確認得它。現有 `sdk-sse-0917`、`sdk-0917` 不重 apply，r2 的 policy/owner/job 不借用成新批准。
3. 固定 metrics 流程先就足夠：server-owned mapping 把 **真 Teams activity 的 event/body digest/route** 綁到明確的新 work/job/source claim；沿用現有 durable SDK ingress/results verifier。只能用通過原 verifier 的固定成果版本建立 Teams payload，再走獨立 Teams obligation/grant/send。不能以 operator 填的成果文字、job 完成字串或 pull ACK 取代成果證據。
4. 明確定義 Teams 新 obligation 的交付責任、同 root 次數與 artifact ACL；舊已結案的 `sdk-job-0917` 不重播，r2 尚未確認的成果不預設成功。Teams 送出只消費這份新批准，不重跑已完成 SDK 工作。

以上第一輪缺口已由下節的 v2 CODE／OFFLINE 接線補上；**目前真實安裝未遷移、sender 未送出，`actual_environment=NOT_RUN`**。不能直接拿原 v1 設定改 channel 或加 boolean 啟用。

### 09/18 CODE-ONLY：同 authority 的 v2 多通道與固定 metrics

`ServerConfigurationV2` 明確使用 `schema_version=fa-behavior-server-v2`。原 v1 全部欄位、canonical bytes/hash、`authenticated-pull` contract 與已耗用 counter 不變；新增 `channels` 只允許指定 personal Teams 路由。`channel` 的 supported/max 都固定為 `provider_accepted`，`required_confirmation_level` 也只能是該值；route ID 與這些確認層級分開保存。原 pull 的 `client_received` 回條不能履行 Teams 責任，也不會被改寫。

本機可用的 operator 入口如下。`schema`、`propose` 不連網，`propose` 只建立尚不存在的 JSON；以下**不表示本次已執行 serve／apply／啟用**。

```powershell
$env:PYTHONPATH = 'C:\approved-worktree\src\fa-shared-context-poc'
python -m shared_context.behavior_channels_api schema channels
python -m shared_context.behavior_channels_api schema retirement
python -m shared_context.behavior_channels_api propose `
  --old-config C:\poc-inputs\current-installed-v1.json `
  --channels C:\poc-inputs\reviewed-teams-metrics.json `
  --output C:\poc-output\proposed-v2.json
```

`channels` 必須包含原 `PersonalTeamsConfig`、既有 signed `route_id`、唯一 `template_binding_id`、`max_requests`（1–16）、`max_model_requests`（不得超過原 profile）、`native_max_generations`（不得超過原 worker）、有限 `valid_until`、新配置對應的 `native_evidence_id/path` 與固定 HTTPS `artifact_origin`。命令只接受精確 bytes `FA metrics`；大小寫、空白、附加批准文字均不是同一命令。來源、已審 claim、agent、目的地、模型與成果規則不能由 activity 選擇。

遷移使用同一個 operator store，依序呼叫 `/operator/v2/channels/{transition_id}/plan`、`authorize`、`apply`、`reconcile`、`verify`。服務可用 `python -m shared_context.behavior_channels_api serve` 加上 `--old-config`、`--new-config`、`--sources`、`--backups`、`--retirement` 啟動；**預設 review-only**，`--allow-apply` 須另有人工批准。每個呼叫都需要原租戶指定管理者的 delegated `Behavior.Read`／`Behavior.Approve`；Connector JWT 不適用。

`plan`／`apply` 前必須同時成立：

- 目前 installation 的 v1 hash 與完整 typed history 已核對；支援 direct pin-v1 及已驗證的 rotated v1。只有尚未結案的第一次 SDK transition、未知新增 row 或來歷不明的 capture 都拒絕，不補假 schema。Capture 的兩個原 rows、材料、精確 request/write-set/result、receipt/hash/guard revision 與 tenant/bot/user/route 全部核對；不接受 capture 與別的 control row 混在同一交易。
- 全部舊 obligation 已有原通道合法回條、pending index/quota 為零、C1 的 pending/completion/slot/lease 已排空、原 native target 無 active launch。未知 model 使用量仍按原歷史保存，不填零、不退額；未知 execution capacity 或 r2 未結責任不能繞過。
- 新鮮、獨立的原 API/controller/native target 退役觀察綁定同一 old/new config 與原 installed code；source/backup 保護及完整 inventory 仍有效。新 rule 的 `valid_until` 必須另外明確批准，最多在當下四小時內；套用批准本身最多十五分鐘且不得超過 plan/evidence 期限。這些上限不是本次獲准的執行時間。

套用僅以一個有 guard 條件的 EGT 更新 installation 並新增批准／歷史／catalog／必要初始 ACL，不建立新 root、pool 或 quota。`verify` 核對每個原 row/receipt 與 counter；回 `activation_performed=false`。apply ACK 遺失只核對原 transition/request，不重送、不重建 migration ID。

實際 call chain：

`POST /behavior/v1/teams/activities` → Connector RS256／tenant/bot/user/serviceUrl／原 personal route → `TeamsMetrics.register`（同 authority 的有限 registry）→ 原 `DeliveryCore.admit`（同 EGT 再查通道撤權及路由 ACL）→ `AssistantRuntime` delegation／原 `WorkerCore` job／C1 → `AssistantAuthority` 讀固定來源與當前人工 review、預留並消費同 root 的 model allowance → 固定 M3 成果與 provenance → 原 work/result verifier → 固定 outbox → exact send grant／sender gate → **一次** Connector POST → 綁定原 digest、route、attempt 的 `provider_accepted`。

SDK、模型及 business effects 在本輪全是離線替身；執行的是實際 authority／來源審核／預算／成果／outbox 邏輯，不是 Windows 真 SDK 或原生 Teams。相同活動只回原 receipt，已完成後也不重跑 job；新 ID 不重置批准次數。SDK／send outcome 不明則保留原責任及預留，不發替代工作或第二次 POST。

`build_server(v2)` 已接上 `TeamsMetrics`，原 server/controller 使用相同 authority，禁止對 v2 bootstrap。真實切換仍要新部署與獨立 readiness 核對；`POST /behavior/v2/channels/teams/native` 只保存目前 observer 證據，不補成人工授權。新 activity 會重新核對已發布 native 證據與有限 rule；缺任何前提都不接單。

原指定管理者可透過 `POST /behavior/v2/channels/teams/revoke` 追加不可變撤權紀錄；此入口也需要 delegated Read+Approve，不支持解除撤權或延長期限。不抹除已發生效果、材料、counter 或原批准。撤權／到期後禁止新工作和新 send，已保存 provider receipt 可由原 controller 走 receipt-only 核對，不重新取得 token／POST；這不是宣稱追回在途效果。原 pull binding 僅保留狀態與專用回條讀取，不重新活化舊 source/context／執行 scope。

`GET /behavior/v2/channels/{channel_id}/receipts/{evidence_id}` 分通道驗回條；固定成果連結走 `/teams/{team}/artifacts/{artifact_id}/versions/{version_id}/content`，仍需原使用者 delegated 身分及目前 route/artifact ACL。`provider_accepted` 不代表 `client_received`、UI 或 `human_read`。

本輪 targeted tests 覆蓋多組 metrics 數值／來源 hash、字面命令變化、重播、錯 scope/JWT、撤權競態、原 root 撤權、錯答案／source withdrawal、request 上限、send ACKloss、不重送、direct pin/rotated capture 歷史及未知 row。真 installation 的 `868a...` 原件未複製到此工作樹，該 optional 本機測項保持 skip；另以白名單原始程式核對合成 v1 canonical bytes/hash 完全相同。這些結果都不是 native PASS。

### 本次離線驗證

結果：**188 passed**，其中 native 分支 44 項；只有既有 Starlette/AnyIO deprecation warning。這不是新增完整 eval suite，也沒有執行 Windows 真 SDK。驗證涵蓋 native default-off、缺證據／過期／錯配置、原 admission EGT 內撤權、token 冷啟動與期限、wire 前撤銷、原 2 秒視窗、exact POST 一次、drop ACK／重啟不重送、歷史 receipt/provenance、證據更新不重建 admission，以及既有 identity／personal／capture 行為。

```powershell
& .\src\fa-shared-context-poc\.venv\Scripts\python.exe -m pytest `
  tests\shared_context\test_teams_native.py `
  tests\shared_context\test_teams_delivery.py `
  tests\shared_context\test_teams_runtime.py `
  tests\shared_context\test_teams_personal.py `
  tests\shared_context\test_teams_identity.py `
  tests\shared_context\test_teams_capture_server.py `
  -q --tb=short -p no:cacheprovider --basetemp=.nat\teams-native-current
```

## 獨立 capture-only 服務

`shared_context.teams_capture_server` 只建立 UAMI／Table client，不建構模型、一般 runtime、批准或 sender。它要求**已套用**的 migration，不接受只有 plan／authorization 或舊 installation；啟動、health、收到活動及最後寫入前都核對同一 installation／history。路由與 installation binding 在同一 EGT 建立，過程不重設原帳本。

```powershell
$env:BEHAVIOR_SERVER_CONFIG = 'C:\poc-inputs\migrated-server.json'
$env:TEAMS_PERSONAL_CONFIG = 'C:\poc-inputs\personal.json'
python -m shared_context.teams_capture_server serve --port 8080
```

兩份設定也各自支援對應的 `_JSON` 環境變數，但同一設定不可同時提供 path 與 JSON。`TEAMS_NATIVE_ENABLED`／`BEHAVIOR_ADMISSION_ENABLED` 不會開啟此服務的工作能力。health 必須回 `mode=capture_only`，且 `work_enabled`、`approval_enabled`、`model_enabled`、`sender_enabled` 全為 `false`。

`/teams/poc`、`/teams/privacy`、`/teams/terms` 提供此 capture-only 版本的公開純文字說明，可作 package 的三個 metadata URL；部署後仍須真正 GET 核對，不能僅因本機有 route 就稱網址已可用。這些頁面不讀取帳本、不顯示租戶或私人 route、不新增具拘束力的服務條款。一般助理功能啟用前必須更新說明，不把 capture-only 的資料處理範圍外推。

## 本批限制

- 沒有 cloud write、bot/app 安裝、message send、token probe 或 model call。
- 沒有新 account、secret、license、role、permission grant 或 Graph consent。
- 固定 personal-only；不支援其他 tenant／user、group/channel、自動改 route、附件、read receipts 或 Teams 內人工批准。
- App package／BotService metadata、Connector 身分驗證、工作成功及 provider/client/UI/human-read 交付證據各自分開；不互相冒充。
- Bootstrap route 的完整 URL 仍受既有 1024 字元 operation 上限約束，超出會拒絕而非截斷。未知／失敗控制提交仍保留原 request 供核對，不能因此開 gate。
