# PR #721 — AVM 盤點與離線契約驗證

2026-10-07；基準為 PR branch `neallee2012-fa-agent-v18-deployment` 的
`e05324c9` 加本次 AVM 修補。這是程式／公開來源／離線編譯結果，
**不是 Azure 部署或現場資源狀態**；沒有執行 Azure 資源查詢、部署、IAM 或設定寫入。

## 結果

- 原有主要資源已採用下表 13 種固定版本 AVM；不為盤點而換版或重寫既有資源。
- `backup-foundation.bicep` 的 Blob DNS VNet link 改用官方 child AVM
  `br/public:avm/res/network/private-dns-zone/virtual-network-link:0.1.0`。
  保留同一 zone、link name、VNet ID、`global`、tags、`registrationEnabled=false`，
  顯式 `enableTelemetry=false`。新 child 只宣告 parent zone 為 `existing`，不 PUT parent。
- `createBlobPrivateDnsZone` 與 `createBlobDnsVnetLink` 仍獨立控制兩個操作；
  link 保留原 zone dependency。既有 DNS zone 不因新增 link 而重新部署。
  沒有新增 `NxDomainRedirect`；AVM 的可省略 `resolutionPolicy` 未傳入。
- M3 additive container 保留 raw child resource，因候選 AVM 的
  `metadata={}` 預設會新增原程式沒有的 metadata 寫入，不能當成零契約變更。
- 19 個 Shared Context entrypoint 全部離線編譯成功。29 個 infra compile／契約／
  workflow 檢查，加上原 seed／metadata／profile 的 134 個測試：**163 passed**。
- **2026-10-07 新環境教訓追加：** `private-state-network.bicep` 新增 opt-in
  `apiSubnetNatEgress`（預設 `false`），只有開啟時才用官方
  `network/public-ip-address:0.13.0` 與 `network/nat-gateway:2.1.1` 建 egress
  Public IP／NAT，並把兩個 subnet 設 `defaultOutboundAccess=false`；預設 subnets
  與原值相同，stage／dev 不受影響。`foundry.bicep` 的模型部署改成
  `modelDeployments` 參數，預設仍是原兩筆。固定 pin 共 16 個；離線契約見
  `tests/infra/test_shared_context_avm.py`。

## 固定版本盤點

各版本均核過官方 `Azure/bicep-registry-modules` 版本標籤與公開
`mcr.microsoft.com/v2/bicep/avm/res/<path>/tags/list`；本機編譯也實際還原其 registry artifact。
來源入口：[AVM 官方目錄](https://azure.github.io/Azure-Verified-Modules/)、
[官方模組 repository](https://github.com/Azure/bicep-registry-modules)。
表中連結指向核對的版本來源，不把最新版能力直接套到舊 pin。

| 資源／官方 `br/public:avm/res/` path | 固定版本／來源 | 本機入口 | 本次處理 |
|---|---|---|---|
| `app/container-app` | [0.23.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/app/container-app/0.23.0/avm/res/app/container-app) | `api`、`behavior-api`、`behavior-controller` | 已用 AVM，保留 |
| `app/job` | [0.7.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/app/job/0.7.0/avm/res/app/job) | `behavior-job` | 已用 AVM，保留 Manual／retry 0 |
| `app/managed-environment` | [0.16.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/app/managed-environment/0.16.0/avm/res/app/managed-environment) | `foundation` | 已用 AVM，保留網路／logging |
| `managed-identity/user-assigned-identity` | [0.6.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/managed-identity/user-assigned-identity/0.6.0/avm/res/managed-identity/user-assigned-identity) | `foundation`、`behavior-recovery-identity`、`stage-ci-identity` | 已用 AVM，保留 OIDC／identity |
| `storage/storage-account` | [0.33.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/storage/storage-account/0.33.0/avm/res/storage/storage-account) | `foundation`、`backup-foundation` | 已用 AVM，保留 keyless／private／backup |
| `container-registry/registry` | [0.13.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/container-registry/registry/0.13.0/avm/res/container-registry/registry) | `foundation` | 已用 AVM，保留 Basic／admin off |
| `network/virtual-network` | [0.10.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/virtual-network/0.10.2/avm/res/network/virtual-network) | `private-state-network` | 已用 AVM，保留 CIDR／subnets |
| `network/public-ip-address` | [0.13.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/public-ip-address/0.13.0/avm/res/network/public-ip-address) | `private-state-network` | **新增**，只在 `apiSubnetNatEgress=true` 時部署 |
| `network/nat-gateway` | [2.1.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/nat-gateway/2.1.1/avm/res/network/nat-gateway) | `private-state-network` | **新增**，只在 `apiSubnetNatEgress=true` 時部署 |
| `network/private-endpoint` | [0.12.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/private-endpoint/0.12.1/avm/res/network/private-endpoint) | `private-state-network`、`backup-foundation` | 已用 AVM，保留 Table／Blob wiring |
| `network/private-dns-zone` | [0.8.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/private-dns-zone/0.8.1/avm/res/network/private-dns-zone) | `private-state-network`、`backup-foundation` | 已用 AVM，保留 zone 建立條件 |
| `network/private-dns-zone/virtual-network-link` | [0.1.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/private-dns-zone/virtual-network-link/0.1.0/avm/res/network/private-dns-zone/virtual-network-link) | `backup-foundation` | **本次改用 AVM** |
| `operational-insights/workspace` | [0.16.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/operational-insights/workspace/0.16.1/avm/res/operational-insights/workspace) | `stage-monitoring`（dev 共用） | 已用 AVM，保留 retention／quota |
| `insights/component` | [0.8.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/insights/component/0.8.0/avm/res/insights/component) | `stage-monitoring` | 已用 AVM，保留 workspace／connection |
| `signal-r-service/web-pub-sub` | [0.7.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/signal-r-service/web-pub-sub/0.7.2/avm/res/signal-r-service/web-pub-sub) | `realtime` | 已用 AVM，保留 Entra-only／capacity 1 |
| `cognitive-services/account` | [0.19.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/cognitive-services/account/0.19.0/avm/res/cognitive-services/account) | `foundry` | 已用 AVM，保留 account；模型部署改由 `modelDeployments` 參數傳入（預設不變） |

## 保留 local modules／raw resource 的原因

| 入口 | 保留內容／具體理由 |
|---|---|
| `foundation.bicep`、`foundry.bicep` | FA orchestration、參數／outputs、跨 RG deployment prefix、registry connection；主要資源已在 AVM leaf，不換另一套 orchestration |
| `private-state-network.bicep` | 固定 API delegation、private-endpoint subnet、Table DNS／group wiring；leaf 已是 AVM，維持地址／依賴 |
| `backup-foundation.bicep` | 新備份 storage／lock／container RBAC、可建立或沿用 zone 的獨立 flags；只替換可獨立覆蓋的 DNS link leaf |
| `m3-artifacts.bicep` | additive container 不重新 PUT M1 account／blob service；原 container body 只含 `publicAccess: None`。候選 [container AVM 0.3.4](https://github.com/Azure/bicep-registry-modules/blob/avm/res/storage/storage-account/blob-service/container/0.3.4/avm/res/storage/storage-account/blob-service/container/main.bicep) 雖使用 existing parent，但 `metadata` 非 nullable、預設 `{}` 並直接寫入 resource；會新增清空 metadata 的可能性，因此不替換 |
| `stage-project.bicep`、Foundry project declaration | 保留指定 account 下 project 的身份、API version、display/description、跨 RG 與 ADR-0008 account/project 分工；不為改 AVM 而改 account 或模型容量 |
| `account-access.bicep`、`project-service-access.bicep`、`infra/vnet/project-access.bicep` | 原 project/account scope、role IDs、principal type 與 GUID 種子是授權契約，不改成另一套角色生成方式 |
| `backup-role.bicep`、`behavior-recovery-identity.bicep` 的 access | FA 限定 read/write、不含刪除的 custom role，以及 recovery read-only scope；身份 leaf 已用 AVM，權限不擴大 |
| `stage-ci-identity.bicep` 的 access、`stage-onboarding-delegation.bicep` | 原 OIDC subject/audience、app/job 限定 RBAC、ABAC condition 與穩定 GUID；identity leaf 已用 AVM，不重建批准／擴權 |
| `project-appinsights-connection.bicep`、`infra/vnet/registry-connection.bicep` | Foundry-specific connection category、auth type、target／metadata／scope；保留特殊接線 |
| 各 `existing` declaration | 只是引用既有資源，不是需要另用 AVM 建立的資源；改成建立型 module 反而會增加 parent PUT |

DNS child AVM 的
[固定版 README／契約](https://github.com/Azure/bicep-registry-modules/blob/avm/res/network/private-dns-zone/virtual-network-link/0.1.0/avm/res/network/private-dns-zone/virtual-network-link/README.md)
與 source 已核對：同一 child API `2024-06-01`、zone name only／existing parent、
registration false、global 與 nullable resolution policy。不是因為「child modules 不存在」而保留 M3；
兩個候選都已確認公開發布，取捨由實際參數／write body 決定。

## WAF／default 取捨

固定 AVM 的安全 default 不等於這次可以改現場：目前 wrapper 已明確傳入
`enableTelemetry=false`、Storage private/keyless/infrastructure encryption、
Managed Environment public access／zoneRedundant、API HTTPS ingress、
Web PubSub Entra-only。這些直接接點與官方固定版 default 已交叉核對，並由編譯契約測試鎖住；
不加 blanket upgrade、額外 IAM 或新的部署 gate。

Storage 的 infrastructure encryption
[建立後不可切換](https://learn.microsoft.com/en-us/azure/storage/common/infrastructure-encryption-enable)；
本次保留既有明示 `true`，沒有查或變更現場值。AVM Web PubSub `0.7.2` upstream
有 `ORPHANED.md`（security/bug fixes only）；已記錄維護限制，不因此換服務或改現場設定。

## Verified against

- 已讀／編譯：`infra/shared-context` 全部 19 個 `.bicep`，包含其 `infra/vnet` local dependencies；
  `prepare_environment.resolve` 的 8 組 generated parameter names 均符合編譯入口。
- Bicep `0.48.1`（`cec7951cf6`）；Windows executable SHA256
  `398af294cf16ac4becdbd008a77d148d0e8e91492309a6b60ec911b7e1cab082`。
  新 CI 固定同版 Linux executable，依 GitHub release asset digest 校驗
  `b09ec25a9d376c1f8e33ede6ed22b587f915ad68488d5db77a6f9541748c7f6e`。
- Baseline 19 個入口先全部 build；最終 JSON 與 `.build-avm/baseline` 做 equality 比對：
  18 個模板完全相同；backup 的 root parameters／outputs 與非 link 內容完全相同，
  只更換 DNS link leaf／相應 template hash。新增 tests 的初批為 24 PASS／4 FAIL：
  兩項為尚未遷移 DNS link，兩項為測試 oracle（nullable parameter expression／example UUID placeholder）。
  修正 oracle 並遷移後，下一批 27 PASS／1 FAIL 為 nullable 參數的表示法預期；
  對齊 `nullable: true` 後，最終 combined run **163 PASS／1 Starlette/AnyIO warning**。
  原中間結果不回填為 PASS。
- 實際最終命令（Windows、原 pinned tokenizer cache，編譯及 module cache 均留在 worktree）：

  ```powershell
  $env:PYTHONUTF8 = '1'
  $env:PYTHONDONTWRITEBYTECODE = '1'
  $env:TEMP = Join-Path $PWD '.build-avm'
  $env:TMP = $env:TEMP
  $env:BICEP_CLI = Join-Path $PWD '.build-avm\bicep.exe'
  $env:BICEP_MODULE_CACHE = Join-Path $PWD '.build-avm\cache'
  $env:TIKTOKEN_CACHE_DIR = Join-Path $PWD 'src\fa-shared-context-poc\.venv\fa-tokenizer-cache'
  & .\src\fa-shared-context-poc\.venv\Scripts\python.exe -X utf8 -m pytest `
    .\tests\infra\test_shared_context_avm.py `
    .\tests\shared_context\test_stage_version_publication.py `
    .\tests\shared_context\test_endpoint_versions.py `
    .\tests\shared_context\test_agent_versions.py `
    .\tests\shared_context\test_deployment_config.py `
    -q -p no:cacheprovider --basetemp .\.build-avm\verified --tb=short
  git diff --check
  ```

- 新 `.github/workflows/validate-infrastructure.yml` 只做 public module restore／compile／tests；
  `contents: read`、無 Azure login／OIDC／environment／deployment command。YAML 與這些限制已離線測試。
  **尚未執行 GitHub-hosted workflow，也沒有設定 required check／branch protection**；
  不把新增檔案寫成已強制執行。
- 另外依部署 README 建立 `.build-avm/venv`，只安裝 `tests/infra/requirements.txt`
  （pytest 9.0.2、PyYAML 6.0.3），不安裝 API／Hosted SDK。
  同一 compiler/cache 下執行
  `.\.build-avm\venv\Scripts\python.exe -m pytest .\tests\infra -q -p no:cacheprovider --basetemp .\.build-avm\infra-tests --tb=short`：
  **29 passed**，再次確認 infra 檢查不依賴完整 backend 開發環境。
- 沒有 deploy/what-if、模型執行、Hosted 建立重試、舊 metadata 修改、雲端設定或權限寫入。
  後續 Azure 套用仍需另外批准，seed／metadata 修補的操作接續見 [部署說明](../deploy/README.md)。
