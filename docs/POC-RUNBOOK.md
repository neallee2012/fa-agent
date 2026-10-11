# Hosted Sandbox POC 操作手冊

<a id="fresh-native-initialization"></a>
## 全新原生環境初始化（W9 CODE/offline入口）

這一節與下方歷史 SMB／VNet 探針無關。產品入口為
`shared_context.behavior_fresh_operator`，只在**已存在而整張皆空的專用 Table**安裝原生設定。
本片只交付 code／offline，不執行 W9；合併後由 b281 依原 stage 批准執行，
部署／總協調交接仍由 71c7 持有，不能因此重送任何 dev 工作。
完整契約及官方限制見 [Shared README](../src/fa-shared-context-poc/README.md#fresh-native-initialization)。

### 固定輸入

執行環境沿 W9 的同 build API image、API managed identity、既有 VNet／Table 存取；
不用真人 JWT、不執行登入／azd、不建立雲端資源。先保留以下檔案於 operator 的私密可持續目錄，
不要把整份內容送到 stdout、Job log 或 Log Analytics：

| 檔案 | 型別與內容 |
|---|---|
| `stage-config.json` | `ServerConfiguration` v1／assistant-v1.2、capacity=1、1 binding／1 root、無 channels／memory projections；stage 自己的 A/B/C/D、tenant／owner／scope／來源與備份配置。 |
| `stage-fresh-inputs.json` | `FreshInstallationInputs`：`policy` 是完整 `SteadyWorkPolicy`；`peer_agent_id` 為 config.targets[1].agent_id；`peer_registration_id` 是保留的新 ID；`initial_accounted_microusd` 與 `budget_enforced` 都要明填。 |
| `stage-fresh-plan.json` | `plan` 真查空後產生。含 exact config/model digest、原始 code bytes hash、inputs digest、A/B 完整 policy、原 input hash／RID／installation ID。檔案已存在不覆寫。 |
| `stage-fresh-authorization.json` | `FreshInstallationAuthorization`，由有權 operator 依真人批准保存；不是 JWT 或簽章驗證。綁 plan/config hash、指定 human、decision ID 及最多 15 分鐘窗口。 |

`policy.template/profile/legacy_template_policy` 必須分別來自同一份 config 的
`bindings[0]`、`assistant.profiles[0]`、`policies[0].policy`；
`configuration_sha256` 用 `config.sha256`、`authority_domain/control_binding` 用 config 原值。
`cost_observer_oid` 同 `config.evidence.observer_oid`；`cost_scope_sha256` 是本次批准的成本 scope，
不是任意換一個 ID 清空帳本。`minimum_observed_microusd` 不高於期初值；新帳可明設兩者為零。
policy 的 active／read_enabled、用量估計／時效／queue 上限與 profiles 都進完整 plan 審核，
不從 dev 的 policy、receipt 或 private evidence 複製。

### 正式命令與批准檔

下列命令在有 API 套件及該 build tokenizer 的環境執行；本機設定
`PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH` 指向自己的 `src\fa-shared-context-poc`。
schema 命令完全離線；plan 是 Table 唯讀，apply 才是寫入，皆無模型／Hosted 呼叫：

```text
python -m shared_context.behavior_fresh_operator schema inputs
python -m shared_context.behavior_fresh_operator schema plan
python -m shared_context.behavior_fresh_operator schema authorization
python -m shared_context.behavior_fresh_operator plan --config stage-config.json --inputs stage-fresh-inputs.json --request-id stage-fresh-001 --installation-id stage-native-v1 --output stage-fresh-plan.json
python -m shared_context.behavior_fresh_operator apply --config stage-config.json --plan stage-fresh-plan.json --authorization stage-fresh-authorization.json
python -m shared_context.behavior_fresh_operator reconcile --config stage-config.json --plan stage-fresh-plan.json --authorization stage-fresh-authorization.json
```

批准檔的**結構示意**如下，尖括號必須由這一批真實值替換；不預填過期日期或 dev ID：

```json
{
  "schema_version": "fa-fresh-installation-authorization-v1",
  "purpose": "initialize-empty-native-authority",
  "approval_source": "operator-approved-file",
  "decision_id": "<original-human-decision-id>",
  "authorizing_human_oid": "<config.identity.human_oid>",
  "configuration_sha256": "<plan-output.configuration_sha256>",
  "plan_sha256": "<plan-output.plan_sha256>",
  "not_before": "<approved-RFC3339-UTC-time>",
  "expires_at": "<approved-RFC3339-UTC-time-at-most-15-minutes-later>"
}
```

plan 成功的一行 JSON 回 `state=planned`、原 RID／installation ID、config／plan／input hash、
`table_writes=0`、`approval_required=true`。apply／reconcile 成功只回原結果摘要與
`receipt_sha256`，包括 `state=initialized`、A/B policy hash，
`native_runtime_prepared=false`、`legacy_readiness_published=false`、`budget_reset=false`；
不回完整 config、policy 或登入資料。失敗回非零 exit code，stderr 為錯誤 code／status，
unknown 另保留原 request ID／digest；不要只看 Job 外層執行狀態。

### 停止與接續

1. 任何 existing entity、讀取錯誤、錯 config／principal／target／input、批准未生效或過期：
   停在原件，不清表、不補舊 handoff／retirement、不換 ID 重送。若尚未有任何 apply，
   才可在重新取得批准後產生新的 plan；已提交或 unknown 必須先查原回條。
2. apply 失 ACK／503／409：保留原三份檔案及 RID，用原 `reconcile`。
   找不到仍維持 unknown，不宣稱未寫入；查到則不重建帳本／registry。
   同一個成功 apply 命令再次執行也只讀回原件；批准期限只約束首次寫入，不使原結果失效。
3. 不跑舊 `bootstrap --stage records|worker|activate`、舊 standing installer 或 B register 來補 fresh；
   它們不是本入口的後半段。所有必要 Table 安裝列已在同一筆交易，B 可沿既有 operator `show/reconcile` 查回。
4. 啟動仍用原 `behavior_server_main.py --config stage-config.json serve --port 8080`；
   只保留已批准的環境旗標。Native `prepare`／同 build target 核對／真正回合測試是 W9 後續批准範圍，
   不是本 CLI 自動副作用；`healthz` 的 alive 不能代替那些結果。

**NativeEvidenceBundle 此處不需要。** 沒有舊 worker／inbox／model execution 要證明已退休；
只存真正原子安裝回條，不造 probe/readiness/retirement。
原生使用 Blob/source/backup 或 Hosted target 的保護仍在其實際 consumer，
不能拿空 bundle、空 proof 或這次初始化摘要當作 ready。
新帳期初值只描述 FA 的記帳起點，不能當 Azure 已花費總額；
保留後續 model unknown／usage、共用 root 與所有原回條。

<a id="fresh-console-export"></a>
### A/B 工作台設定的離線匯出

在**目標工作台的檔案系統**或有 repo／工作台 package 的準備環境執行；
API image 沒有 `web_test_console`，不要臨時替 API 安裝 UI／Hosted 相依。
此命令不讀 Table、不登入、不改任何遠端設定，也不宣稱 plan 已成功安裝：

```text
python -m web_test_console.fresh_export --config stage-config.json --plan stage-fresh-plan.json --backend-origin https://approved-api.example --client-id approved-delegated-client-id --output-directory private-console
```

輸出 A/B 各自的 `config.json`（standing-v3）、`instructions.json`（native sidecar），
及一份 `agents.json`；使用既有 DTO、各自 policy／hash／journal 路徑，
不呼叫只支援 onboarded Agent 的 `console-connection` endpoint。
`--client-id` 必須是另外已批准並在 API 設定的 delegated client，匯出本身不授權該 client。
manifest 使用**匯出環境的絕對路徑**，若要搬到另一個作業系統／目錄，應在目的地重新匯出；
既有不同 bytes 的檔案拒絕覆寫。匯出只回
`connections-exported, agents=2, installation_verified=false, table_writes=0`；
由 71c7 把原 W9 回條與準備檔交接 3ef6，不由本片自行啟動工作台或呼叫 Agent。

### Verified against（fresh operator）

可重跑的最小**離線**入口（正常 test requirements＋固定 tokenizer cache；output 目錄必須不存在）：

```text
python tests\shared_context\fresh_operator_local.py --output-directory fresh-offline-output
python -m pytest tests\shared_context\test_fresh_installation.py -q -p no:cacheprovider
```

runner 以 synthetic config／本機 Table transport 執行真正 `-m shared_context.behavior_fresh_operator`
入口的 plan、apply、reconcile、同 apply 查回，最後確認只有一筆 batch；阻擋所有 socket。
輸出檔都是測試資料，**不能拿去 stage**，也不能把它的批准檔或回條用在 W9。

原始 base `3241d435`／整合 `56399a71`，本 PR WIP 的正式
`behavior_fresh_operator.main`、`web_test_console.fresh_export.main` 與
`tests\shared_context\test_fresh_installation.py`。
整合前直接 129 案通過；整合後 fresh 35 案及原相干 94 案通過，新增 reader 的首跑 FAIL 原件保留。
使用 Table SDK 12.7.0／本機 HTTP transports、真 JWT verifier，
CLI 的 plan→apply→reconcile→同 apply 回讀與既有工作台 DTO 讀回均納入。
本片沒有 Azure／Hosted／真模型操作，W9 的實際結果須另留紀錄，不回填本機 PASS 為 live PASS。

## stage 完整測試的操作經驗（2026-10-04）

來源：2026-10-04 stage 完整測試，見 [progress](progress.md#2026-10-04stage-發布-20261004-2-3-與完整測試4-項未過修理中)。

### 受控身分補 app role 後不會立刻生效

- **現象**：stage 設定檔裡的 A、B 補上 `Behavior.Agent` app role 後，呼叫 API 時好時壞。
- **原因**：受控身分的角色是 access token 裡的 claim。官方文件說，平台會依 resource URI 把受控身分的 token 快取約 24 小時，而且無法強制刷新，所以角色變更可能要等好幾個小時才生效（[Limitation of using managed identities for authorization](https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations#limitation-of-using-managed-identities-for-authorization)）。
- **現行做法**：#599 起，API 對設定檔 Agent 也只核 token 的 oid（[ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md) 門禁 C），不靠角色判斷。新環境不必先指派角色，也不用等快取；已指派的角色保留。
- 以後遇到「剛指派的角色沒生效」，先想到 token 快取，不要反覆重新指派或刪掉重建。

### 讀 Hosted 容器的 stdout／stderr

官方的 [`azd ai agent monitor`](https://learn.microsoft.com/azure/foundry/agents/how-to/monitor-hosted-agent-logs) 可以讀 Hosted 容器的 console log：

```powershell
# 在 azd 專案目錄「外」執行
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<account>.services.ai.azure.com/api/projects/<project>"
azd ai agent monitor <agent> --session-id <sid>
```

- 要在 azd 專案目錄外執行，並設定 `FOUNDRY_PROJECT_ENDPOINT` 指向要讀的 project（dev 或 stage）。這是 2026-10-04 實測可用的做法。
- `--session-id` 填 Hosted session ID；加 `--follow` 可以持續串流，加 `--type system` 可以看容器啟動、重啟這類系統事件。
- 這是唯讀操作。log 可能含私人對話或路徑，轉貼前要先去敏，不要貼憑證。

## 歷史 Hosted Sandbox POC

**現行方案已完成：** 依 [ADR-0002](adr/0002-shared-context-without-file-share.md)，共享 Context API、
Non-VNet Foundry 與 Memory Store 已部署，17 項 Hosted 功能驗收通過；執行控制缺口與費用未知
見 [POC-RESULTS.md](../POC-RESULTS.md)。新方案入口為 [controller 手冊](../tests/shared_controller/README.md)，
必須使用已驗證的 endpoint 與 session。以下 SMB／VNet 命令只供歷史重現，不是後續預設流程。

**後續主線：** 依 [ADR-0003](adr/0003-hosted-environment-continuity.md) 維持 Hosted，驗收完整必要環境／工作恢復，不要求固定 VM。下一階段依 [分析與計畫](POC-ANALYSIS-AND-PLAN.md) 的 M0–M5 另行批准；不要為閱讀或整理本手冊重跑以下付費命令。

本手冊只執行 `FA-Hosted-Sandbox-POC-Spec.md` 的 G0-G3。它不把本地 Docker、Storage REST 或 session 私有檔案視為共享掛載成功。

**本輪結果：G3 PASS；G0 BLOCKED；G1/G2 未執行。**
先讀 [POC-RESULTS.md](../POC-RESULTS.md)。目前組織政策不允許原測試配置，
ACR 實際為 Premium；最新帳單查詢已回報約 US$0.98，仍可能有未入帳費用。
下列部署／執行命令是重現說明，不代表已批准新的計費工作。

**2026-09-13 10:34 +08 接續決定：固定 VM 列待驗證，先完整執行目前 Hosted 的 G0–G3。**
[DP-02](adr/0001-persistent-agent-computer.md) 與標準 idle／resume 的文件契約落差仍保留，
但不再阻擋本次獨立能力探索；VNet injection 不代表已驗證固定 VM。
雲端部署仍須完成精確資源／權限計畫批准與 Azure validation，且不提高原 US$100 預算。
`fasample-vnet-20260913-1` 已建立 Foundry／網路／四個 Approved PE；
`fasample-vnet-complete-20260913-1` 已修復 registry connection 並完成私有 controller。
新 project v1（相同舊 v5 digest）的 G0 已通過 secret lookup，Hosted Azure Files DNS／TCP 445 成功，
但真正 mount 回 `mount error(13): Permission denied`；根因尚未釐清，不能直接判定為 kernel／capability 限制。
追加批准的診斷 image 已由 `ds8` 建置並在新 project v2 執行，仍為 error 13。
已確認 credential 來源為 Key Vault、格式與檔案 mode 正確；kernel 探針卻回
`dmesg: unknown time format: raw`，這是探針參數錯誤，不是 Hosted 拒絕讀 kernel log。
該探針已在另行批准的 `ds9`／新 project v3 修正並執行，實際 kernel 回
`0xc0000022 STATUS_ACCESS_DENIED`／`Send error in SessSetup = -13`。
先前 account-key 路徑是 SMB session setup 被拒絕，並非單純缺 root；仍未完成 mount。
`allowSharedKeyAccess=false` 是主要設定疑點，尚未以政策對照／現行 key 比對證明唯一根因。
不要再重跑相同診斷期待通過，也不要未獲批准就改政策、加 Files 角色或切換 Kerberos 架構。
當時新 project 七個 sessions 均 idle，控制 VM 已 deallocate；後續 MI 診斷見下節。

## 本地控制測試

```powershell
$env:PYTHONUTF8="1"
python -X utf8 -m unittest discover -s tests -v
docker build --platform linux/amd64 -t fa-hosted-sandbox-poc:local .\src\fa-hosted-sandbox-poc
```

本地 container 可驗 handler、路徑限制、輸出格式與 GUI 套件；本地 mount 成功也不能替代 Hosted G0。

## Azure CLI 隔離

所有 Azure 命令使用專案專屬設定：

```powershell
$env:AZURE_CONFIG_DIR="$HOME\.azure-foundry-agents"
$env:AZURE_DEV_USER_AGENT="microsoft_foundry_skill"
```

登入、建立或修改雲端資源、角色指派、付費 invoke、commit、push 與清理都要另有明確批准。
每個新 PowerShell process 都要重設這些 process-local 變數。
所有 `az` 資源命令還要加 `--subscription "<approved-subscription>"`。

## 私有 VNet 接續流程

使用者已批准新 VNet／私有控制 VM 與窄 scope RBAC；仍須先完成
`.azure/deployment-plan.md` 的 Azure validation，不能直接部署未通過的模板。
控制 VM 僅使用 ARM Run Command 和 managed identity，不傳使用者 token、不代跑 mount／browser。

若 account／project／VNet 已成功而只剩 connection／controller 失敗，
使用 `infra\vnet\completion.bicep` 與獨立參數檔重新 validation；不要重跑完整 `main.bicep` 或 purge account。
RegistryIdentity credentials 依官方 azd ACR 模板映射 project principal 與 ACR resource ID；
不能由 GET 省略 credentials 推論 CREATE 不需要它，也不要把這個特殊映射泛化為所有 MI 的 clientId。

從成功的 Bicep deployment outputs 取得 `controllerVmName`、`projectEndpoint`，再執行：

```powershell
$vm = "<controllerVmName>"
$endpoint = "<projectEndpoint>"
.\scripts\invoke-private-controller.ps1 -Action Bootstrap -VmName $vm -ProjectEndpoint $endpoint
.\scripts\invoke-private-controller.ps1 -Action Deploy -VmName $vm -ProjectEndpoint $endpoint
```

`Deploy` 使用已記錄的 v5 immutable digest，不新增 build。取得回傳的版本及
`instance_identity.principal_id` 後，只對該 Hosted principal 授予原 vault 的 `Key Vault Secrets User`；
控制 VM 不取得 vault secret read。先核對 RBAC，再使用新 run ID 執行 `-Action G0 -AgentVersion "<version>"`。

G0 受阻時，桌面 G3 可用同一個已驗證 image/version 獨立執行，不必另建 image：

```powershell
.\scripts\invoke-private-controller.ps1 -Action G3 -VmName $vm -ProjectEndpoint $endpoint `
  -AgentVersion "<active-version>" -RunId "<new-g3-run-id>"
```

此模式只建立一個 Hosted session，重用 VM managed identity 的控制 client，
執行 environment probe／desktop smoke／Files REST 取證後 stop。
不讀 Key Vault、不做 mount、不發布 `/team`；`shared_publish` 仍為 `NOT_RUN_DEPENDENCY`。
G3 runner 的失敗或 cleanup 失敗不會被 private adapter 當成成功。

原計畫在 G0 通過後才 build；使用者現已另外批准一個 G0 診斷版本。
`Dockerfile.diagnostics` 從舊 v5 immutable image 繼承依賴，只 COPY `poc_core.py` 並啟用診斷旗標，
不重新 apt/pip 安裝。`.dockerignore` 僅放行兩個 Dockerfile 與四個 runtime/dependency 檔案，
不把 `.env`、cache、`.foundry` 或 evidence 上傳。Base image 已固定到 v5 記錄的 digest，
一般 Dockerfile 的 apt/transitive dependencies 仍非完整 snapshot lock；
診斷 Dockerfile 則直接繼承已部署映像中的版本。新 image 的實際 digest 必須重新讀回。
修正版還在 build 內以真正 `dmesg` 驗 argv 與 synthetic log；這不等於 Hosted mount 驗證。
已部署 v3 digest 為 `sha256:c3861e70477956cd341547ad5d75fc8c84a8bf7f6da36aa7fd4d771104563bfe`。

```powershell
$image = "<registry>/<repository>@sha256:<new-verified-digest>"
.\scripts\invoke-private-controller.ps1 -Action Bootstrap -VmName $vm -ProjectEndpoint $endpoint
.\scripts\invoke-private-controller.ps1 -Action Deploy -VmName $vm -ProjectEndpoint $endpoint -Image $image
.\scripts\invoke-private-controller.ps1 -Action All -VmName $vm -ProjectEndpoint $endpoint `
  -Image $image -AgentVersion "<new-version>" -RunId "<new-unique-run-id>"
.\scripts\invoke-private-controller.ps1 -Action Collect -VmName $vm -ProjectEndpoint $endpoint `
  -RunId "<new-collection-id>" -CollectRunId "<exact-run-id>"
```

`All` 依序驗 G0、G1、G2、G3；G2 等待 330 秒，要求平台直接回傳
`x-ms-debug-latency-session-start-type: resume`，不以等待或私人檔案存活代替。
Files REST 僅下載 GUI 的 session-private 證據；共享存取仍由 A/B 的真實 CIFS mount 驗證。
`Collect` 將 VM 上已保存的證據分段取回並核對 archive SHA-256，不會呼叫／喚醒 Hosted session。
`-CollectRunId` 只收指定 run 與根層 metadata，缺少該 run 會失敗；省略它則保留整個 evidence root 的舊行為。
收證 label `-RunId` 與遠端 run selector 是不同用途，不能以 label 推定已篩選。
Run Command 可能把 `[stdout]`／`[stderr]` 放在同一個 `ProvisioningState/succeeded` message，
而不是分開的 `StdOut`／`StdErr` entries。Transport 支援兩種格式，且必須讀到 `FA_POC_EXIT=0`；
單純 ARM `Succeeded` 不算 controller 成功。可用 `tests\test_private_controller_transport.ps1` 重現解析回歸。
不論成功或失敗，先確認 A/B 已停止，再 deallocate controller VM；保留的磁碟／網路仍可能計費。

## MI／SMB OAuth 實驗流程

這是獨立於既有 account-key 診斷的新路徑。原始 build 加上後續批准的三次修正均已執行；
`dsd` 成功；使用者之後批准最多 100 輪必要的修正／建置／部署／測試，原預算與權限範圍不變。
不開放 Shared Key 或公開網路、不部署 AD，也不把 REST 或控制 VM 當成掛載證據。
目前 Debian 12／Foundry Hosted 未在官方 client 清單中，整條路徑仍屬待實測。

`Dockerfile.smb-oauth` 沿用 v3 immutable image，固定官方 `azfilesauth 1.0-12` 套件的 SHA256，
只新增明確的依賴。未修補 stock `cifs.upcall 7.0` 會拒絕 service-ticket-only cache，
因此同一次 build 的 builder stage 編譯固定 source commit 的 `cifs.upcall 7.4`，
只複製該 binary 到 `/usr/local/sbin/cifs.upcall`，不替換原 `mount.cifs`、OS／kernel 或 browser。
Build 必須實際載入 native library，確認版本與 CLI/configuration；
這些檢查不含 Azure token／mount，也不是 Hosted G0。
若 library、CIFS upcall 或相依性不相容，只在目前批准輪數內做必要診斷；
不自動換 OS、kernel、資源或擴大權限，也不湊滿 100 次。

後續 `Dockerfile.smb-diagnostics` 從 v4 immutable digest 繼承相同套件，
只增加有界診斷。`POC_MI_MOUNT_DIAGNOSTICS` 記錄 metadata、upcall entry／exit 和白名單 kernel 分類；
`POC_MI_NETWORK_DIAGNOSTICS` 只在 native 失敗後比較 root／effective UID10002 的未認證連線；
`POC_MI_KEY_DIAGNOSTICS` 在隔離子程序的匿名 keyring 中比較 dummy key 本機建立與 kernel callout。
不得開啟原始 native HTTP、Kerberos ticket、packet dump 或 CIFS verbose credential 記錄。

已完成六輪。最新 v7 能取票，卻同時重現 mount errno2 與 generic `request_key()` 的 ENOENT，
而本機 dummy key 建立成功。此時應停止盲調 Azure RBAC，交由 Foundry 團隊確認 helper 呼叫環境。
首次呼叫前等待 20 秒只作診斷對照，不能當作 readiness 保證；
票證／HEAD HTTP400／本機 key 成功均不是 G0 PASS。完整狀態見 `POC-RESULTS.md`。

通過映像檢查後，才可依批准範圍使用 CLI `--enable-smb-oauth true` 啟用 `stfaexampledemo` 的 SMB OAuth，
並在該 account 指派既有 runtime `00000000-0000-0000-0000-000000000011` 的
`Storage File Data SMB MI Admin`。此角色含檔案管理員權限，不是普通 Share Contributor，
不可擴大為 RG／subscription scope，也不授予 controller。
Storage ARM read-back 必須確認
`properties.azureFilesIdentityBasedAuthentication.smbOAuthSettings.isSmbOAuthEnabled=true`、
Shared Key 仍 disabled、PNA 仍 Disabled。CLI 參數名稱不是 ARM 欄位名稱，不能讀 `properties.enableSmbOAuth`；
新版本的 runtime identity 也必須再次讀回，不能直接推定未變。

更新 controller 後，在同一個隔離 Azure CLI profile 啟動原私有 controller，
先 `Bootstrap` 上傳目前 controller，再以明確的 `-Image "<immutable digest>"`、
`-SmbAuthMode managed_identity` 執行 `Deploy`／`G0`／`All`。
省略 mode 時仍是 `account_key`，保持 v1–v3 的既有定義可重現；
MI mode 不傳 Key Vault／key secret 設定，沒有自動 key fallback。
G0 另要求 mount 回應的 `auth_mode=managed_identity`，不能只看 CIFS source。
通過 G0 才用同一 image／version 繼續 G1–G3；仍需收證、停止 sessions 並 deallocate controller。

官方依據：[MI 主文件](https://learn.microsoft.com/azure/storage/files/files-managed-identities)、
[Linux helper](https://github.com/Azure/AzFilesAuthenticator)；
唯讀前提與限制見 `docs\AZURE-FILES-AUTH-OPTIONS.md`。

## 原 public 環境的 azd 操作（歷史流程）

取得批准後，在 repository root 設定：

```powershell
azd env set AZURE_SUBSCRIPTION_ID "<approved-subscription>"
azd env set AZURE_TENANT_ID "<approved-tenant>"
azd env set AZURE_LOCATION "westus3"
azd env set POC_STORAGE_ACCOUNT_NAME "<approved-storage-account>"
azd env set POC_FILE_SHARE_NAME "<approved-share>"
azd env set POC_KEY_VAULT_URL "https://<approved-vault>.vault.azure.net"
azd env set POC_STORAGE_KEY_SECRET_NAME "azure-files-account-key"
```

Storage account key 只保存在批准的 Key Vault；Hosted agent identity 只取得該 vault 的 `Key Vault Secrets User`。Key 不可進 Git、image、azd env、命令參數、一般日誌或證據。

## 部署與執行

```powershell
.\scripts\deploy-poc-infra.ps1 `
  -SubscriptionId "<approved-subscription>" `
  -EnvironmentName "<azd-environment>" `
  -Location "westus3"
azd provision --preview --no-prompt
azd provision --no-prompt
azd deploy --no-prompt
azd ai agent show --output json
```

`azure.ai.agents 1.0.0-beta.13` 的 CLI request validation 拒絕 120 秒，
300 秒則部署成功；不能由這個 CLI 錯誤推論 live service 的最小值。
本輪保持 300 秒設定，active test window 上限為 60 分鐘。

部署後從 `azd ai agent show fa-hosted-sandbox-poc --output json` 取得 runtime identity，
只在批准的 dedicated vault 授予 secret read：

```powershell
.\scripts\grant-agent-key-vault-access.ps1 `
  -SubscriptionId "<approved-subscription>" `
  -ResourceGroupName "<poc-resource-group>" `
  -KeyVaultName "<poc-key-vault>" `
  -AgentPrincipalId "<hosted-agent-principal-id>"
```

部署回報成功後，**先讀回實際 SKU、網路、local-auth、secret metadata 與 RBAC**。
`az policy assignment list` 不足以排除 inherited modify policy；
應對 Key Vault／Storage 的精確 resource ID 查 `az policy state list --resource ...`。
若 read-back 與批准範圍不同，停止新增 build／invoke，不嘗試覆蓋組織政策。

## 單獨重現 G3

控制端使用同一個 `AZURE_CONFIG_DIR`，固定 active version 與 ACR 查得的 digest。
`g3_only.py` 只建一個 session，不讀 Key Vault、不掛載或寫入 `/team`。
它記錄每次 invocation、下載前後畫面、核對下載內容/hash，並在 `finally` 停止 session。

```powershell
python -m pip install -r .\controller\requirements.txt
python -X utf8 .\controller\g3_only.py `
  --project-endpoint "<project-endpoint>" `
  --agent-version "<active-version>" `
  --image-digest "sha256:<verified-digest>" `
  --run-id "<new-unique-run-id>"
```

人工檢查 `evidence\<run-id>\after.png` 確認整個 display 顯示該 run 的 nonce 與
`GUI_COMPLETE`。這不是 headless、遠端 CDP 或透過 JavaScript 直接填值；
`xdotool` 輸入後用 Tab／Return 觸發按鈕 click。`type` 必須是獨立 process，
否則會把後面的 `key` arguments 當成輸入文字。

Browser 使用 non-root UID 10001，沒有 `--no-sandbox`。測試頁只有 synthetic loopback 資料。
下載到本機的截圖來源為 session-private `$HOME`，不能拿來證明共享掛載。

## 共享路徑的限制

`controller\run_poc.py` 在失敗時保存 manifest 並停止已建立的 A/B；
會核對 session routing、指定 CIFS source、文字／bytes／hash、owner 交接與私有 marker。
但本輪 G0 前提被擋住，**G1/G2 不是已驗收的功能**。
本地已補齊 A 對 rename 新舊檔名的獨立 `inspect`、G3 Files REST 證據核對，
以及 G2 直接平台 resume header 與重掛模式判定；仍須在新 image/version 的 Hosted session 實測。
既有 v5 第一輪並未執行這些後續步驟，不能用新增程式碼回改既有結果。

只有在已批准的 VNet／Private Endpoint 路徑完成驗證，才重開共享測試。
不新增未批准資源、不申請政策豁免，也不換成 Storage REST 同步。

成功步驟支援同 `run_id + step_id` 重播；**失敗步驟的自動重試不在本版保證範圍**。
Controller 不自動重送。排除環境問題後，使用新 run ID，不讓舊 browser profile／artifact 造成假通過。

## 停止與清理

達到預算、60 分鐘 active window、兩個 active sessions、quota 錯誤、平台 mount 權限拒絕或不可修正的 image 問題時，停止新增付費操作。刪除任何 Hosted version、ACR、Storage、share、secret、role assignment 或 resource group 前，先列出精確清單並再次取得批准。

使用官方 `azd ai agent sessions stop <session-id>` 停 compute，保留 filesystem；
再用 `sessions list --agent-name fa-hosted-sandbox-poc --limit 100 --output json` 讀回。
下載／invoke 可能改變 session 狀態，所以檔案擷取必須在最後一次 stop 之前。
本輪保留所有雲端資源，沒有執行 delete、commit、push、G4 或自動排程。
