# FA Teams v1.2 外部契約研究

研究日期：2026-09-16。唯一規範仍為 [FA Agent 行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)；本文不是部署批准或 Teams PASS。BR-06 的身分、路由與回條必須獨立取證，不能沿用 PullInbox 的 `client_received`。

## SDK 與可重用的 Module

建議採官方 Teams SDK Python `microsoft-teams-apps==2.0.15` 作通道外殼，保留既有 FastAPI、Copilot SDK／AssistantEngine、ArtifactService 與 FA 控制帳本。官方版本支援將 `FastAPIAdapter(app=existing_app)` 接入既有應用程式。套件要求 Python `>=3.11,<4.0`；現有 Python 3.12 符合下限，但尚未安裝或執行 Teams 整合驗證。依賴也需固定，不使用漂移的最低版本。

來源：[官方 package metadata](https://pypi.org/pypi/microsoft-teams-apps/2.0.15/json)、[固定版本的 FastAPI 範例](https://github.com/microsoft/teams.py/blob/f01b31f5f224cc2def45e41d9d472b9f2b3400ca/examples/http-adapters/src/fastapi_non_managed.py)。

Microsoft 365 Agents SDK Python 1.5.0 也有 FastAPI hosting；但固定版本的預設 Connector client 會把 conversation ID 截至150字元。FA 不可接受目的地被默默改寫，必須拒絕或另用經驗證的精確路由 Adapter。Bot Framework SDK／Emulator 已退休，不新增 `botbuilder-*`、TeamsFx 或 Emulator 依賴；使用仍有效的 Azure Bot Service／Connector REST 契約不等於使用退休 SDK。

來源：[Agents Python 固定 client](https://github.com/microsoft/Agents-for-python/blob/d3f427d81b6d5fdd40bcb8ad16b78dbeb4530631/libraries/microsoft-agents-hosting-core/microsoft_agents/hosting/core/connector/client/connector_client.py)、[退休工具與替代品](https://learn.microsoft.com/microsoftteams/platform/legacy-tools-sdks)。

## 已讀到的 POC 前置 metadata

研究代理透過指定隔離 Azure CLI／Graph 執行唯讀檢查，未使用其他 tenant 的 WorkIQ、未讀私人聊天或其他人的資料、未安裝、傳訊、呼叫模型或變更資源。以下是其回報，不把 metadata 當成通道實測。

| 檢查 | 結果與界線 |
| --- | --- |
| Azure 身分 | subscription `00000000-0000-0000-0000-000000000012`、tenant `00000000-0000-0000-0000-000000000013`、指定 admin 均吻合。 |
| Bot resource | `rg-faexample` 的 `Microsoft.BotService/botServices` 查詢成功且為空；沒有擴大搜尋其他 RG。 |
| 指定 admin license | `MICROSOFT_365_E7` 的 `TEAMS1` 為 `Success`，另有 Teams Premium service plans；不是「沒有 Teams license」，也不證明 app policy、安裝或實際傳訊可用。 |
| 現有 API app | `00000000-0000-0000-0000-00000000002b` 為 `AzureADMyOrg`，有 Behavior scopes；不是 Bot／Teams channel 註冊。 |
| 測試目的地 | 尚無具體 Teams manifest、合法安裝或 conversation reference 證據；不能自行選別人的群組。 |
| SDK | 當時未安裝 Teams／Agents hosting 套件；沒有整合 PASS。 |

使用者檢查僅針對既定 OID `00000000-0000-0000-0000-00000000001b` 的 `accountEnabled`、`assignedPlans` 與 `licenseDetails`。CLI 能讀到這些欄位，不代表 backend MI 或 bot 已獲 Graph 安裝／傳訊權限。

## Incoming：服務身分與使用者身分分開

Connector service JWT 不是使用者 delegated token。公有雲 profile 必須驗證受信任 JWKS、RS256、精確 bot audience、issuer、期限與 service URL。不能拿現有 FA API audience 代替 bot audience，也不能強求 service JWT 帶 POC 使用者的 `oid`／`tid`。

服務認證完成後，Teams asserted `from.aadObjectId` 仍須經 server-owned tenant／actor map 核對。它可形成受限的人類來源事件，不能自行產生 `Behavior.Approve`、delegated `scp` 或 Graph token。操作批准沿用現有真使用者驗證流程，或另行完整實作 Teams SSO/action 身分契約。

每次 admission 固定並保存：

- `channelId == "msteams"`、`channelData.tenant.id`；若其他受信任 tenant 欄位並存，必須一致。
- bot `recipient.id`、bot app/account、`from.id` 與 `from.aadObjectId` 的授權關係。
- 完整 opaque `conversation.id`、conversation type、channel thread root／`replyToId`，不得截斷。
- 受驗證的完整 `serviceUrl`、cloud、route version。
- provider activity ID、來源 namespace、防重播條件與 payload digest；不是按文字內容去重。

UPN／email 不是 proactive messaging address；Teams user ID 具有 bot/user 範圍。未建立可靠 edit/delete change identity 前，不把修改後相同 provider ID 包成新事件。

來源：[Connector authentication](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-authentication)、[Teams activities](https://learn.microsoft.com/microsoftteams/platform/bots/build-conversational-capability)、[Proactive messages](https://learn.microsoft.com/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)。規格對應：BC-01、BC-04、B05、B09、B12。

## serviceUrl 與送出目的地

Teams SDK 在 body service URL 存在時，會檢查 token `serviceurl` 與完整 URL（去掉尾斜線後）。FA 必須額外要求欄位存在，不能依賴 optional branch。

送出還須核對 HTTPS、核准 cloud/host/port/path；拒絕 userinfo、未核准 URL、loopback/link-local/private network 與 redirect。不能把任意 Microsoft 網域當成本次授權的 Teams endpoint。目的地只能來自受驗證的 activity 或正式核准路由，不能由模型提供 callback URL。

來源：[Teams SDK token validator](https://github.com/microsoft/teams.py/blob/f01b31f5f224cc2def45e41d9d472b9f2b3400ca/packages/apps/src/microsoft_teams/apps/auth/token_validator.py)、[安全指引](https://learn.microsoft.com/microsoft-365/agents-sdk/secure-your-agent)。規格對應：BC-04、B09、B12。

## Outgoing：一次送出與可證明的回條

Connector outgoing 使用 bot 自己的 service-to-service token，公有雲 scope 為 `https://api.botframework.com/.default`，不是 FA API token 或使用者 Graph token。現有 MI 不因存在就自動成為合法 bot identity，需符合 Bot resource 的註冊。

首次送出前須保存固定內容／成果版本，核對目前收件人與路由授權，原子預留 operation/attempt/budget，並讓 executor 消費同一份核准 method/URL/body。換 route／recipient 是新授權及 delivery，不得借用舊 receipt。

**不要把 FA delivery ID 放進 `Activity.id` 假裝冪等。** Teams SDK 有 `activity.id` 時走 update，沒有時才 create。

Connector 的 `ResourceResponse` 只有 provider resource ID，不證明 FA payload digest、UI render 或真人已讀。FA Adapter 必須把原始固定 request、route、dispatch authorization、真成功 HTTP response 及非空 provider ID，綁成版本化 `VerifiedDeliveryReceipt`；不能接受 caller 自報 `{id: ...}`。2xx 但缺 ID／完整 receipt 仍未確認。

來源：[Bot resource/identity 前提](https://learn.microsoft.com/microsoft-365/agents-sdk/provision-azure-bot-service-manually)、[Connector API reference](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference)、[Teams SDK ActivitySender](https://github.com/microsoft/teams.py/blob/f01b31f5f224cc2def45e41d9d472b9f2b3400ca/packages/apps/src/microsoft_teams/apps/activity_sender.py)。規格對應：BC-02、BC-04、BC-08、B09、B12。

## Unknown、重試與確認層級

所查官方 send/reply/update 契約沒有一般 client-provided idempotency key、以 FA delivery ID 查原 send 的 API 或 exactly-once 保證；不推論所有未查 API 都不存在。

| 情況 | 處理 |
| --- | --- |
| 未dispatch | 尚未交付；重新驗目前授權才可首次送出。 |
| provider可能接受但response丟失／timeout／process消失 | 保留 delivery/operation unknown、原attempt、材料與預留；不能盲目再POST。 |
| provider receipt已保存但控制提交失敗／未知 | 只核對原receipt及控制提交，不重送訊息。 |
| 2xx缺有效provider ID | 不製造ID或declared delivered。 |
| 暫時查不到訊息 | 不證明原attempt不會稍後生效，不據此退額或發B。 |
| blocked/uninstalled或配額耗盡 | 停止新送出、自動重試按已驗證能力限制；保留未交付責任，不倒推原unknown未生效。 |

官方 backoff／429 建議不是 v1.2 的終局性證據。SDK／proxy 的 retry 也必須受共同帳本限制；無所需證據的非冪等自動再試保持停用。

首版可選並獨立驗證 `provider_accepted`，不能暗降現有 obligation 的要求。Teams read receipts 另有限制：個人 user-to-agent chat、RSC `ChatMessageReadReceipt.Read.Chat` 及 admin/user設定；未獲授權與取證前不支持 `human_read`。reaction、receipt、安裝事件不觸發模型互答。

來源：[Rate limits](https://learn.microsoft.com/microsoftteams/platform/bots/how-to/rate-limit)、[read receipts](https://learn.microsoft.com/microsoftteams/platform/bots/build-conversational-capability#receive-a-read-receipt)。規格對應：B02、B04、B05-terminality、B09-terminality、B10、B11、B12。

## 接線與驗收界線

一般入口與 Teams 各自驗證來源後，匯入既有 admission/work/obligation；沿用 AssistantEngine、固定成果與受保護 outbox，再由各自 Adapter 送出及驗證 receipt。研究時 `DeliveryCore.send` 尚無一般 outbound sender，runtime也固定 PullInbox；不能只換 channel 字串。

2026-09-16 離線實作核對：新增 `teams_identity.py`、`teams_runtime.py`、`teams_delivery.py`，`DeliveryCore.send`／receipt 只對精確註冊的 Teams Adapter 接線。此切片採上述有效的 Connector REST 契約與既有固定版本 `httpx==0.28.1`、`PyJWT[crypto]==2.13.0`，沒有安裝退休 SDK，也沒有宣稱已驗 Teams SDK。真 HTTPX 測試 transport、RS256 與既有 BehaviorStore／ToolBrokerCore／DeliveryCore 已串起 incoming→admission→trusted work callback→prepare→guarded POST→receipt；transport 與 Table 均為離線替身，`actual_environment=NOT_RUN`。

本切片只開 `provider_accepted`；未知結果不重送、不退預留，retained receipt 只作原控制提交核對。採完整 route 雜湊與不可變紀錄，沒有截斷 conversation／thread／provider activity ID；完整編碼 URL 超過既有 operation 的 1024 字元上限時明確拒絕。來源 body 32 KiB、純文字 8 KiB、每 entry 最多 32 筆保留 source plan；無自動 GC、無計數重置。只接受明確註冊的 `smba.trafficmanager.net` 公有雲 HTTPS 路徑，其他 cloud／endpoint profile 未開放。成果連結須符合既有 M3 固定版本下載路徑、當前 route audience 的成果讀取範圍與獨立人工 grant；下載端仍須重新驗當前讀權。

必要反例包括錯/過期JWT、服務token冒充人類批准、tenant/bot/user/thread錯綁、同ID換digest、過長或被截斷的conversation ID、service URL／redirect攻擊、批准後改route/body、撤權、2xx空ID、錯receipt、provider接受後response丟失、持久consumer重啟與full quota下的收尾。需分列文件、offline與真正Teams安裝／activity／response證據。

Teams訊息大小約100KB，官方建議內容少於80KB；不得截斷必要成果湊成功，應引用既有受權固定成果版本，下載時再檢查當前讀權。來源：[訊息格式與大小](https://learn.microsoft.com/microsoftteams/platform/bots/how-to/format-your-bot-messages)。

09/17 更新：BotService／Teams channel、組織發布、指定 admin 個人安裝及真 `connector_http` route 已各自取證；參考 `docs/evidence/teams-personal-route-20260917.json`，不再把它們列成未知前提。sender 尚未真正送出，也沒有 provider receipt。

CODE-ONLY 後續接線加入 `TeamsNativeRegistration`／`TeamsNativeEvidence`：未明確註冊仍回 `TEAMS_NATIVE_DISABLED`；註冊後必須讀回同 authority 的原 signed route／capture binding、目前 server installation，以及獨立驗證、未過期的 sender/readiness 證據。`publish_installation()` 只保存證據，不開 admission、不建 quota／pool。`enable_admission()`、incoming staging、prepare、consume 與 wire send 都核對目前安裝；原 NativeGuard 由 parent 的 `require_current_native_evidence` 沿用，不能換成永遠成功的 callback。Offline registration 仍只允許精確 `httpx.MockTransport`，native 不接受注入 transport 或測試 credential。

Bot UAMI token 改在 `_consume` 前預熱；之後仍重新核對 route/access/owner/epoch/policy/grant，沒有放大原 2 秒 wire gate。token 的實際期限另在送出前核對；一次 POST、exact URL/body、無 redirect／retry 不變。新 native receipt 將 `connector_http`、原 installation evidence ID/digest 與 consumed gate、provider response 綁定，只能確認 `provider_accepted`。已保存 receipt 的核對不需重新取得 token，也不因 installation 過期而重送；缺 ACK／缺 receipt 保留 unknown。舊 offline plan／gate／receipt 的序列化內容保持相容。

**目前安裝的 SDK 設定仍不相容，這不是 Azure 權限 blocker。** `868a0560e741ac886649bdfbd65fbb152d8a9946870e13b01fff098f14d4056f` 只有 `authenticated-pull`／`client_received`。`DeliveryCore.configuration_digest()` 把單一 channel 算入 digest，quota/index/obligation 又各自綁該 digest；直接換 channel、建第二套 root 或把 pull ACK 換成 Teams receipt 都不合法。`compose_installed_personal_channel()` 明確回 `TEAMS_PARENT_CHANNEL_INCOMPATIBLE`，未改 installed config 或任何 rotation/migration gate。下一步所需的最小同 authority、多通道配置變更及 operator 接線方法見 `deploy/teams/README.md` 的 native 章節。本批只有離線回歸，不能當成真 Teams／UI 或整個 BR-06 PASS。

## 09/18 多通道與固定來源串接：CODE／OFFLINE

`ServerConfigurationV2`／`InstalledChannels` 已提供明確的兩通道 registry。原 `authenticated-pull` contract 與 quota/index digest 保留，v2 installation 另外固定 Teams channel／rule hash；不是任意 caller 可指定的 digest alias。Teams 的 required/supported/max 固定 `provider_accepted`，與精確 route 分開，錯組合在載入配置時拒絕。v1 序列化與原件比較不變；此相容性結果不冒充真實 `868a...` installation 已遷移。

`ChannelMigration` 只接受原 v1 的完整非 channel 欄位不變，核對 direct pin-v1 或 rotated v1 的 typed history，再以單一條件式 EGT 保存 v2。Direct pin 路徑重用既有 `_without_pin_migration`：先驗 capture 的完整同交易材料／回條，再建立不可變的 typed view；rotated 路徑保留完整原 catalog 並沿用原 verifier。未知新增 row、缺/錯 receipt、mixed capture transaction、guard revision 不符、舊 pending/capacity unknown 均拒絕。原 rows、receipts、已耗額度與 retired unknown 不刪、不退、不改判成功；尚未完成第一次 SDK 退役核對的中間配置不在可遷移範圍。

`TeamsMetrics` 已把真驗簽入口接到既有 `AssistantRuntime`／`AssistantAuthority` 的固定 source/claim/profile、原 root budget、C1、M3 固定成果與 sender。Activity 只能提供固定命令，不能供應批准、來源、agent、SDK 參數或目的地。新 work/owner 的授權來自另外由 delegated 管理者批准的有限 rule，保留 activity body digest 與原 ingress evidence；Connector token 沒有變成人類 delegated token。每次讀取／模型 dispatch／保存仍核對來源版本與當前 review。

原 admission EGT 及 send consumption 內都有 channel rule／撤權條件；管理者的 `revoke` 只追加不可變紀錄，不重寫原批准，也沒有 renew/unrevoke 入口。撤權／到期不關掉必要的 receipt-only 核對入口：已存 provider receipt 可完成原控制提交，但不能再次取 token 或 POST。原 pull binding 的歷史狀態／回條可讀，不讓過期 source/context 或工作權限復活。具體命令、call chain 與 migrate/enable 前置條件見 [Teams README](../deploy/teams/README.md)。

本輪重讀官方 [Connector authentication](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-authentication)、[Connector REST reference](https://learn.microsoft.com/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference) 及 [Table entity group transactions](https://learn.microsoft.com/rest/api/storageservices/performing-entity-group-transactions)：服務 JWT 必須核對 issuer/audience/signature/期限/serviceUrl；outgoing 用 bot credential；ResourceResponse 的 provider ID 不是 client/UI/human-read 證據；Table EGT 限同 partition、最多 100 operations／4 MiB，且同一 entity 不能在同批重複操作。此切片沿用既有更保守的 store 限制，沒有假設 Blob/SDK/Connector 與 Table 可跨服務原子提交。

**`actual_environment=NOT_RUN`。** 測試只以離線 Table、HTTPX、SDK/model/business-effect 替身實跑；沒有新增握手、Azure 登入／部署、模型呼叫或 Teams POST。原成功握手後剩餘兩次仍作廢。真 outgoing credential、v2 部署／獨立 current readiness、受批准的新 activity、provider receipt、client/UI/human-read 仍須各自取證，不能由本輪測試推出。
