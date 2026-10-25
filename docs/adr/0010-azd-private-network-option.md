# azd 私有網路選項（Foundry Hosted Agent 注入 VNet）

狀態：**accepted（方向；Neal 2026-10-23「可以接受，先照這個範圍做」）；第 0 階段探針已完成（2026-10-23）；`private` 的長期記憶採記憶選項 B（FA 自建）；後端由 Neal 比較 Azure AI Search 與 Cosmos DB 之後改選 Cosmos DB serverless＋private endpoint（Neal 2026-10-23），詳細設計見 [ADR-0011](0011-private-mode-cosmos-memory.md)（accepted 2026-10-23）**｜提出日期：2026-10-22 +08｜第 4 版（第 2 版依對抗式審查修正 3 個 P1、9 個 P2；第 3 版再修第二輪審查的 1 個 P1、8 個 P2；第 4 版併入第 0 階段結果與 Neal 的記憶決定，並依 Neal 同日改選，把記憶選項 B 的後端由 Azure AI Search 改為 Cosmos DB serverless）。

本文決定方向。第 0 階段探針已獲批准（2026-10-23），只在 dev 訂閱的新 RG 執行；其他雲端資源、權限與費用仍照 AGENTS 另外批准。探針已執行完畢，結果見「[第 0 階段結果](#第-0-階段結果2026-10-23)」與「[第 0b 階段結果](#第-0b-階段結果2026-10-23)」；下文凡被探針證實或推翻的 [推論]，已依結果改寫。第 0b 階段（同日，新 RG `rg-fa-netprobe0b-1023`）也已執行並刪除。

## 背景

Neal 2026-10-22 的需求原文：「azd的部分，我想要多一個選項，讓使用者決定是否要把FA Agent佈署成私有化網路的架構，ex. Foundry hosted Agent」。他指定的是官方的 BYO virtual network 選項：agent 注入使用者委派、管理的 subnet，並經 private endpoint 存取資料。

今天 azd 路徑的狀態：
- **已經是私有的**：
  - `vnet-<env>`（192.168.96.0/24）；
  - Table 和 Blob 都是 `publicNetworkAccess: Disabled`，只能經 private endpoint 存取；
  - Container Apps 環境已整合 VNet。
- **公開的**：Foundry 帳號、ACR（Basic）、Container Apps 的 ingress、Web PubSub（Free_F1，不支援 private endpoint）、Log Analytics、App Insights。
- Hosted Agent 跑在 Microsoft 管理的網路，從公網呼叫 FA API。依 ADR-0006，Team 資料都經 API 轉送。

## 結論

**新增選項 `FA_NETWORK_MODE`，值為 `public`（預設，維持現狀）或 `private`。**

`private` 能做到：
- **Foundry 帳號／project 資料面入站私有**：關掉公網；模型呼叫走 private endpoint（第 0b 階段實測：Hosted 內模型主機解析到 private endpoint 的 IP，模型呼叫回 200；部署者 IP 窗口內與 `Disabled` 兩種狀態都成功；`Disabled` 時是從 VNet 內的 Job 呼叫 agent，Job 直接呼叫模型是第 0 階段的結果）；toolbox 依官方文件經 project private endpoint（未實測）。
- **agent endpoint 跟著帳號的公網設定走**：帳號 `publicNetworkAccess: Disabled` 時，從公網呼叫 agent endpoint（`/responses`、`/invocations`）回 403 "Public access is disabled. Please configure private endpoint."。第 0 階段先由探針回報，第 0b 階段已用保留的原始 log 重驗（T4）。
- **Hosted Agent 放進你的 subnet**：之後可以自己加 NSG、UDR、防火牆來控制出站。
- Table、Blob 維持現在的 private endpoint。

`private` 做不到的，要先說清楚：
- **Hosted 到 FA API 的 Team 資料仍走公網**（TLS＋Entra），因為 API 的入口是公網。這段要私有，就要讓 API 走內網，列在之後的 `isolated`。
- **agent endpoint 只在帳號 `Disabled` 時私有**。官方 azd 文件寫 agent endpoint URL 在 preview 期間仍是公開位址，探針在本設計的設定（BYO VNet 注入）下沒有看到這個行為，那段文件談的是 azd 擴充套件情境，另論。部署期間暫開部署者 IPv4 的窗口內，endpoint 的可達範圍沒有單獨測，只有旁證：selected IPs 窗口內，允許的筆電 IP 呼叫 agent endpoint（`/responses` 與 invocations）都回 200。
- **第一版沒有出站過濾**。agent 經 NAT 出站，來源 IP 固定，但目的地不限，因為 WebIQ 等 MCP 要連外部。要過濾，請自己在 subnet 加 NSG 規則或防火牆。
- **Foundry Memory Store 不能用，長期記憶改走記憶選項 B**。官方文件寫明「Virtual network (VNet) integration isn't supported for memory stores.」（[What is memory，Limitations](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-memory#limitations)）。探針實測：帳號入站只要是 `defaultAction: Deny`（含只開特定 IP，不論是否注入）記憶背景作業就失敗、搜尋逾時；（未注入帳號測得）只有 `Allow` 正常，而 `Allow` 與 `private` 的前提互相矛盾。Neal 2026-10-23 決定 `private` 採記憶選項 B：FA 自建長期記憶後端。B 原本寫的是 Azure AI Search＋private endpoint；總協調比較 AI Search 與 Cosmos DB 之後，Neal 同日改選 Cosmos DB serverless（「用Cosmos DB serverless」），詳細設計見 [ADR-0011](0011-private-mode-cosmos-memory.md)。ADR-0011 的實作完成前，`private` 一律搭配 `FA_ENABLE_MEMORY=false`（FA 在記憶關閉時已經會正常降級，回 `not_configured`）。`public` 不變，維持 Foundry Memory Store。
- **只能在建立新環境時選，之後不能切換**。官方：「Network injection is part of the create-resource flow and can't be added」（[networking-options](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/networking-options)）。
- **主人無法再從公網用 Foundry 入口網站看 Agent 與資料**，要在 VNet 內或經 VPN。
- 探針基礎設施部署約 5–7 分鐘（不含 FA 應用與 azd hook；探針：第 0 階段 4 分 38 秒，第 0b 階段約 6 分鐘；第 0 階段帳號加兩個模型部署 3 分 20 秒，平台自動建帳號層級 capability host 約 3 分鐘，與帳號同時完成）。刪除要照順序，而且慢很多：`azd down` 預期 45 分鐘以上（見第 7 節）。

**已確認：BYO 為硬需求**（見「Neal 的決定」）。以下 D 的比較留作參考：官方另有 managed VNet（選項 D），「now supports Prompt and Hosted Agent services」。它省掉 subnet、NAT 和刪除順序的麻煩，入站同樣私有，但 agent 不在你自己的網路裡。

## 已確認的事實（官方文件）

標示：[文件] 是文件明寫；[推論] 是依文件推得、尚未實測；[探針] 是第 0 階段（2026-10-23）實測結果。

| 事實 | 來源 |
|---|---|
| [文件] Hosted Agent 支援網路隔離的 Foundry 資源，出站可走客戶的 VNet（configure-private-link 把 hosted 標為 preview） | [Hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)、[configure-private-link](https://learn.microsoft.com/en-us/azure/foundry/how-to/configure-private-link) |
| [文件] 注入只能在建立帳號時設定，之後不能改 subnet、不能移除 | [networking-options](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/networking-options)、[virtual-networks](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks) |
| [文件] agent subnet 委派給 `Microsoft.App/environments`，最小 /27，production 建議 /24；不能和其他 Foundry 資源共用 | 同上、[deep-dive](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agents-networking-deep-dive) |
| [文件] 每個 Hosted session 占一個 IP；/24 約 250 個 session；用盡時回 429 `subnet_exhausted`，平台不預警 | deep-dive |
| [文件] Micro VM 和每個 project 自己的 data proxy 都在委派的 subnet；agent 自己的出站直接走 subnet 上的網卡，工具呼叫走 data proxy | deep-dive |
| [文件] Memory Store 不支援 VNet integration。[探針] 實測：帳號入站只要是 `Deny`（含 selected IPs）就壞，（未注入帳號測得）只有 `Allow` 正常，見「第 0 階段結果」 | [What is memory](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-memory#limitations) |
| [文件] Work IQ 在網路隔離的 project 不支援；MCP、OpenAPI 走 VNet subnet；toolbox 本身沒有網路資源，agent 經 project private endpoint 存取 | [toolbox network isolation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox-network-isolation)、[configure-private-link 工具表](https://learn.microsoft.com/en-us/azure/foundry/how-to/configure-private-link#agent-tools-with-network-isolation) |
| [文件] 關掉公網後，不能從公網工作站跑 `azd up`／`azd deploy`；azd 沒有內建 VNet 旗標 | virtual-networks（azd pivot） |
| [文件] 入站有三種：Enabled、Enabled from selected IPs、Disabled | configure-private-link |
| [文件] 需要註冊 `Microsoft.App` 和 `Microsoft.ContainerService` | virtual-networks |
| [文件] 刪除時 subnet 會被 service association link 卡住，要先刪 capability host、purge 帳號；文件寫 capability host 建立約 30–35 分鐘（[探針] 實測不成立：帳號層級的由平台自動建，約 3 分鐘，與帳號同時完成） | virtual-networks 疑難排解、[範本 15 README](https://github.com/microsoft-foundry/foundry-samples/blob/main/infrastructure/infrastructure-setup-bicep/15-private-network-standard-agent-setup/README.md) |
| [文件] 範本 11（BYO VNet＋平台管理的資料資源）明確建立 project capability host（`capabilityHostKind: 'Agents'`），帳號 `networkAcls` 用 `Deny`＋`AzureServices`（[探針] 實測不建 project capability host，Hosted Agent 也能跑，FA 維持不建） | [foundry-samples 範本 11](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep) |
| [文件] AVM `cognitive-services/account` 0.19 的 `networkInjections` 是單一物件 `{scenario, subnetResourceId, useMicrosoftManagedNetwork}`，內部轉成原生陣列 | [AVM README](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/cognitive-services/account) |
| [文件] managed VNet 支援 Hosted；刪 Foundry 資源會一起刪 managed VNet | [managed-virtual-network](https://learn.microsoft.com/en-us/azure/foundry/how-to/managed-virtual-network) |

文件之間有矛盾，第 0 階段實測的結果如下：
- 一處說 private endpoint 涵蓋 "data-plane and ARM calls"，另一處說 ARM 可達。[探針] 帳號 `Disabled` 時筆電的 ARM 部署與之後的寫入都成功，ARM 可達。
- CGNAT 一處可用、一處不支援。本設計只用 RFC 1918。
- azd pivot 說 agent endpoint 在 preview 期間仍是公開位址。[探針] 本設計的設定下，`Disabled` 時公網呼叫回 403，agent endpoint 跟著帳號的公網設定走；azd 擴充套件情境另論。
- managed VNet 的區域：一處列 18 個，一處說和 custom VNet 相同。

## 選項比較

| 選項 | 入站 | agent 位置 | Hosted→API 的 Team 資料 | 筆電能 `azd up` | 每月加價（約） | 主要麻煩 |
|---|---|---|---|---|---|---|
| **A（建議）BYO VNet** | 私有 | 你的 subnet | 公網（TLS＋Entra） | 能（部署期間暫開部署者 IP，見「部署步驟」） | USD 50 | subnet、NAT、刪除順序、建立順序 |
| D managed VNet | 私有 | Microsoft 管理的 VNet | 公網（TLS＋Entra） | 同 A | 約 0（核准式出站要 Firewall Basic 約 USD 288） | 不能用你的 NSG／UDR；區域說法矛盾 |
| C 全內網（`isolated`） | 私有 | 你的 subnet | 內網 | 不能（要 VPN 或 runner） | USD 50＋你的 VPN | API 與工作台內網化、私有 ACR |
| B 全內網＋Front Door Premium | 私有 | 你的 subnet | 內網 | 不能 | USD 510 起 [推論] | 貴；Easy Auth 在代理後面未驗證 |
| E 只開 selected networks | 限 IP | Microsoft 管理 | 公網 | 能 | 0 | agent 不在你的網路；入站只限 IP；Foundry Memory Store 也壞（selected networks 屬 `Deny`，見第 0 階段結果） |

A 和 D 的差別只在「agent 在不在你自己的網路裡」：
- 在你的網路裡（A），你可以加 NSG、UDR、防火牆，也能和企業網路 peering。
- 不在你的網路裡（D），這些都省了。

兩者的入站、Team 資料路徑、Memory 限制都一樣。Neal 指定 BYO，所以建議 A；D 列為正式替代。

## 建議設計（`private`，選項 A）

### 1. 使用者怎麼選

```bash
azd env set FA_NETWORK_MODE private
azd up
```

- 參數接線：
  - `infra/azd/main.parameters.json` 加 `"networkMode": { "value": "${FA_NETWORK_MODE=public}" }`。
  - `fa_azd.py` 的 `USER_INPUTS` 加 `FA_NETWORK_MODE`。
- preprovision 檢查，任何一項不符就停下並說明，不寫任何東西：
  - **模式不能改**：直接讀 ARM 上 Foundry 帳號的 `properties.networkInjections` 與模式比對。不能只看 azd env 的 `FA_FOUNDRY_ACCOUNT`，因為第一次 provision 失敗時，帳號可能已建立、輸出卻沒寫回。第一次 provision 之前 env 還沒有帳號名稱，改用 Bicep 的 token（`main.bicep:90`，由 subscription、環境名稱、區域算出）推出的固定帳號名稱去讀；該帳號不存在就略過這項檢查（全新環境）。
  - `private` 時 `FA_ENABLE_MEMORY` 必須是 `false`（Foundry Memory Store 在 `private` 不可用；記憶選項 B 的記憶後端由 [ADR-0011](0011-private-mode-cosmos-memory.md) 定義，它的第 4 階段實作後再放寬）。
  - 區域在 Hosted Agent 的區域表裡。`ukwest` 在現有 `@allowed` 清單（`main.bicep:44`），但不在 agents 區域表，`private` 時擋下。這是既有的不一致，不是 `private` 才有：`main.bicep:58` 說區域必須支援 Hosted Agents，卻允許 `ukwest`。這裡是拿 Agent Service 的區域表去對，`public` 不改。
  - **Teams／Work IQ 不在 preprovision 檢查**：`USER_INPUTS` 沒有 Teams 旗標，Work IQ 是之後在 UI 才加，preprovision 沒有可檢查的輸入。改成執行期防護：`FA_NETWORK_MODE` 傳給 API，`private` 時 API 拒絕新增 Work IQ connector grant。這是第 2 階段工作，另外在文件警告「`private` 不支援 Work IQ」。
  - `preflight_subscription.py` 的 provider 清單，只在 `FA_NETWORK_MODE=private` 時檢查 `Microsoft.ContainerService`，`public` 不變。Cosmos DB 用的 `Microsoft.DocumentDB` 由 [ADR-0011](0011-private-mode-cosmos-memory.md) 的實作加入（在那之前 `private` 不能開記憶，用不到）。

### 2. Bicep：先建網路，再建 Foundry

現在的順序是 `foundry` → `foundation`，VNet 在 `foundation` 裡面才建立（`infra/azd/main.bicep:180-200`、`foundation.bicep:126-135`）。注入要求建立帳號時 subnet 已存在，所以：

- 只把 VNet、NAT 和 subnets（`infra/shared-context/private-state-network.bicep`）拆成獨立 module，放在 `foundry` 之前。
  - Table private endpoint 與 DNS（同檔第 5 行 `storageResourceId`、第 106–135 行）需要儲存體資源 ID，留在 foundation；foundation 改成用參數接收 subnet ID。整個搬走會形成循環相依（網路要儲存體 ID，儲存體在 foundation 之後）。
  - AVM VNet module 每次以完整的 subnets 陣列 PUT，所以 `snet-agents` 必須放在同一個 VNet module 的 subnets 裡，不能另外用 module 補。
  - foundation 裡用到 project 身分的部分（AcrPull、registry connection）維持在 `foundry` 之後。
- `public` 時同樣先建網路，只是不帶 `snet-agents`、不帶注入。資源的屬性不變，只有部署順序和 module 名稱改變。要用測試把 `public` 下每個資源（含共用的 Foundry 帳號；stage project 也在這個帳號裡）的屬性釘死，證明 dev 與 stage 不受影響。

### 3. 網路

| subnet | 位址 | 用途 |
|---|---|---|
| `snet-api`（既有） | 192.168.96.0/26 | Container Apps 環境 |
| `snet-private-endpoints`（既有） | 192.168.96.64/27 | Table、Blob，新增 Foundry 的 private endpoint |
| `snet-agents`（`private` 才有） | 192.168.97.0/24 | 委派 `Microsoft.App/environments`，只給 Foundry；`defaultOutboundAccess: false`；NSG（預設規則，留給使用者加出站規則）；NAT |

- `private` 時 VNet 位址空間多加 `192.168.97.0/24`。
- NAT Gateway：
  - 現在只在 `FA_API_SUBNET_NAT_EGRESS=true`（`apiSubnetNatEgress`）時建立。要改三處條件，都改成「NAT egress 開或 `private`」任一成立：
    1. 公網 IP（`private-state-network.bicep:40`）；
    2. NAT Gateway（同檔 `:54`）；
    3. subnets 陣列（同檔 `:83`，現為 `apiSubnetNatEgress ? natEgressSubnets : baseSubnets`），`private` 時要多放 `snet-agents`。
  - 同一個 NAT 可以接兩個 subnet。
- Foundry private endpoint（group `account`）放 `snet-private-endpoints`。新增三個 DNS zone 並連到 VNet：`privatelink.cognitiveservices.azure.com`、`privatelink.openai.azure.com`、`privatelink.services.ai.azure.com`。
- 帶入既有 VNet（真正的 BYO：`FA_VNET_RESOURCE_ID` 加上 subnet 名稱，DNS zone 由使用者的 hub 提供）列為第 2b 階段。原因是每個 FA 環境固定用同一段位址（`public` 用 192.168.96.0/24，`private` 多加 192.168.97.0/24），接到企業 hub 做 peering 時會重疊。設計見「[第 2b 階段設計](#第-2b-階段設計帶入既有-vnet2026-10-24)」。

### 4. Foundry 帳號

`private` 時，在 `foundry.bicep` 用既有的 AVM 0.19 傳入：
- `networkInjections: { scenario: 'agent', subnetResourceId: snet-agents, useMicrosoftManagedNetwork: false }`；
- `publicNetworkAccess` 與 `networkAcls`：`defaultAction: 'Deny'`、`bypass: 'AzureServices'`。部署期間暫時允許部署者 IP（第 0 階段第 2 題已通過），見下一節；
- `privateEndpoints`；
- **不建 project capability host**：探針實測，平台會自動建帳號層級的 capability host（約 3 分鐘），Hosted Agent 正常運作。範本 11 有建立；FA 的 `foundry.bicep` 從來沒建過，維持不變。

`public` 時這些參數都不傳。

### 5. 部署步驟：筆電呼叫 Foundry 資料面的問題

今天 postdeploy 從筆電直接打 Foundry 資料面的步驟有：
- 建立 Hosted Agent 版本（`seed_agents.py`）；
- Memory Store（`new_memory_store.py`）；
- toolbox 與 WebIQ（`new_toolbox.py`、`new_webiq_connection.py` 的讀取段）。

關掉公網後，這些都會失敗。第 0 階段兩條路都實測過、都可行，**主路徑是暫開部署者 IP**，Job 是備案：

- **主路徑（[探針] 已驗證）：部署期間只允許部署者 IPv4。**
  - 做法：postdeploy 開始時用 ARM PATCH 把 Foundry 設成 Enabled＋`defaultAction: Deny`＋`AzureServices`＋一條部署者目前公網 IPv4 的 ipRule；跑完資料面步驟，再 PATCH 回 `Disabled`，並**讀回**確認。
  - PATCH 只送 `publicNetworkAccess` 與 `networkAcls`，不送其他欄位；實測 `networkInjections` 不變。
  - 時間：PATCH 約 15 秒套用完成，資料面約 4 秒後生效；關回 `Disabled` 約 3 秒生效。關回後資料面回 403 "Public access is disabled"。
  - 授權：hook 會先明示將暫時開放的 IP 並取得使用者確認，再寫 ARM。共用 NAT（公司或辦公室出口）的 IP 會一併開給同一出口的所有人，開得太寬，提示要寫明。
  - 中斷時 hook 下次執行會先讀回狀態再收尾。結束時印出目前的入站狀態。
  - 實作（第 2 階段，#963，合併時只有離線替身測試；10-24 已在 `fa-priv-1024` 實跑窗口與刪除）：只在 WebIQ、Memory Store、第一次建 Agent 版本要跑時才開；IP 由 `https://api.ipify.org`（只回 IPv4）讀取；終端機要輸入同一個 IP，無終端機用 `FA_CONFIRM_DEPLOY_WINDOW=<IP>`（須等於這次讀到的 IP）；關回後讀回 `Disabled`、無 ipRule、`Succeeded`，失敗時印出關閉指令並以非零結束；每次 deploy 一開始先關掉中斷留下的窗口。第一輪修正：帳號沒有注入（`FA_NETWORK_MODE` 設錯）時 deploy 與 predown 都在寫入前停下；IP 在建 image 之前確認，開窗口前再核一次；PATCH 保留 `networkAcls` 的其他欄位；Ctrl+C 不會殺掉子程序，關窗口與讀回期間忽略 Ctrl+C。
  - 優點：所有腳本不用改，public 和 stage 完全不動。
  - 先前擔心的反證（官方 networking-options 表格沒有列 BYO VNet 搭配 selected IPs）以實測為準，不成立。
- **備案（[探針] 已驗證可行）：只在 `private` 把這些步驟移進 VNet 裡的 Job。** 探針從 VNet 內的 Job 呼叫資料面成功。範圍比看起來大：
  - API image 不含 `deploy/stage/*.py`，要像 sync job 一樣內嵌腳本；
  - 認證從 `AzureCliCredential` 改成受控身分；
  - 防重送的 intent 檔從本機檔案改存 Table 或 Blob；
  - 建立結果（agent_guid、principal_id）要經 Log Analytics 傳回。
  - API 身分的權限足夠：它在 project 上有 Foundry User＋OpenAI User，sync job 已用它 `update_route`，onboarding 已用它 `create_version`。

留在筆電、`private` 下仍可達的步驟：
- ARM 與 Graph；
- 本機 `docker push`（ACR 維持 Basic、公網、只允許 Entra，不含使用者資料）；
- 呼叫 API 公網入口：`healthz`，以及用主人本人 token 的 `prepare_native_sessions`、`select_all_capabilities`；
- Log Analytics 查詢。

### 6. 執行時的路徑

| 路徑 | `public`（今天） | `private` |
|---|---|---|
| 瀏覽器 → 工作台 → API | 公網＋Entra | 不變 |
| Hosted → FA API（Team 資料） | Microsoft 管理的出站 → API 公網入口 | `snet-agents` → NAT → API 公網入口（TLS＋Entra）。[探針] NAT 出站已驗證（來源 IP＝NAT IP，探針回報 68–292 ms）；第 0b 階段 Hosted → FA dev API 公網 `/healthz` 可達（回 200，19–54 ms，約 14 次觀察；「經 NAT」是推論，log 的出站 IP 已去敏）；帶身分驗證的 Hosted→API Team 資料呼叫未測 |
| Hosted → 模型、toolbox | 平台內部 | project private endpoint。模型：[探針] Hosted 內解析到 private endpoint 的 IP，呼叫回 200（部署者 IP 窗口內與 `Disabled` 都成功，第 0b 階段）；toolbox 依官方文件，未實測 |
| 外部 → agent endpoint | 公網＋身分驗證 | 跟著帳號的公網設定走：`Disabled` 時 `/responses` 與 `/invocations` 公網呼叫回 403（[探針]，第 0b 階段有保留的原始 log）；窗口內允許的筆電 IP 呼叫 agent endpoint 可達（T1／T2 回 200）；窗口內非允許 IP 是否被擋沒有測 |
| Hosted → 長期記憶 | Memory Store | 記憶選項 B：Cosmos DB serverless＋private endpoint，FA 自建（[ADR-0011](0011-private-mode-cosmos-memory.md)）；Memory Store 在 `private` 不用 |
| API → Foundry（agentsv2 等） | 公網 | Container Apps 環境 → Foundry private endpoint（要靠 DNS zone 連到 VNet） |
| API → Table／Blob | private endpoint | 不變 |
| 工具 → 外部 MCP（WebIQ 等） | 平台出站 | data proxy（`snet-agents`）→ NAT（WebIQ MCP 出站未測） |
| Hosted → App Insights、身分端點 | 平台出站 | App Insights：[探針] app 讀環境變數 `APPLICATIONINSIGHTS_CONNECTION_STRING`，12 筆 trace 全部數秒內到達；app 用環境變數中的連線字串（推定為平台注入）直接 POST 到公網 ingestion，此路徑不需要 AMPLS。身分：Hosted 的 token 來自平台提供的 IP 端點（HTTP），不是 `*.identity.azure.net`；`login.microsoftonline.com` 實測可達（回 200，50–292 ms，約 14 次觀察）；是否必要未測（token 走平台 IP 端點） |
| 主人 → Foundry 入口網站 | 公網 | 需要在 VNet 內或經 VPN |
| Teams relay（入口網站一鍵發布） | 公網 | 官方說仍可發布，但有額外設定；第一版不支援。Work IQ 由 API 執行期防護拒絕新增（見第 1 節，第 2 階段） |

### 7. 刪除

azd 的流程是先跑 predown hook，hook 跑完 azd 才問自己的 y/N。所以不可逆的動作不能在使用者確認之前發生。設計如下：

1. **`private` 時 predown 一律自己要求輸入環境名稱。** 不管有沒有備份鎖。沿用既有的確認路徑：互動時輸入環境名稱，無終端機時設 `FA_CONFIRM_DELETE=<環境名稱>`（`fa_azd.py` 約 1114–1121 的備份鎖確認）。和備份鎖的確認合併成同一次詢問，使用者只被問一次，不是兩次。提示要說清楚：「接下來會永久刪除 Foundry 帳號（模型、Agent）和備份；確認後 azd 還會問一次 y/N，處理其餘資源」。輸入不符就停，什麼都不刪。
2. 確認後依序執行（[探針] 驗證過的順序與坑）：
   1. 刪 Hosted agents。**有 session 時要用 `force=true`，否則回 409。**
   2. 刪 project。**要先刪 project 才能刪帳號，否則回 `CannotDeleteResource`。**
   3. 刪 Foundry 帳號（先不 purge）。FA 不建 project capability host。探針沒有單獨刪帳號層級 capability host：刪 project、刪帳號、等帳號消失後 purge 成功。該 capability host 的存在只有探針回報，保留的原始 log 沒有。**CLI 刪帳號可能回 `GatewayTimeout`，此時帳號其實已在 `Deleting`**（第 0b 階段實測）：讀狀態、輪詢，不要重送。唯一例外：project 已讀回不見，刪帳號卻回 `CannotDeleteResource`、列出的巢狀資源只有本帳號自己的 project（2026-10-24 實測），表示什麼都沒被接受，每 15 秒重送一次、最多 5 分鐘。
   4. **輪詢帳號的 `provisioningState` 直到帳號消失**（探針約 22 分鐘）。`snet-agents` 的 service association link 比帳號早清空約 6.5 分鐘，所以 SAL 清空不代表帳號已刪完，不能只看 SAL。
   5. **帳號消失後才 purge。** 探針的 purge 在帳號仍是 `Deleting` 時沒有結束碼，帳號消失後 exit 0。
   6. **讀回** SAL 已清空。
   7. 最後才交給 azd 刪 resource group（約 27 分鐘，卡在 Container Apps 環境）。
3. 可重試：每一步先讀回目前狀態（agent 存在？project 存在？帳號存在、已在 `Deleting`、已 soft-delete、已 purge？SAL 還在？），從那一步接著做。**CLI 回報錯誤時，帳號可能已在 `Deleting`，先讀狀態再重試，不要直接重送。** 例外只有第 2 點第 3 步那一種 `CannotDeleteResource`。
   - 時間：`azd down` 預期 45 分鐘以上（帳號約 22 分鐘＋resource group／Container Apps 環境約 27 分鐘；第 0 階段約 49 分鐘，第 0b 階段整套拆除 50 分鐘：帳號約 22 分鐘、RG 約 26 分鐘）。hook 要印出進度，不能讓使用者以為卡住。
   - **可能的殘留，要在拆除文件中列為檢查項：** 建立 Hosted 版本時，平台會自動建 Entra agent 身分（instance identity 與 blueprint）。第 0b 階段有 Graph 讀取權限：force 刪 agent 後，3 個 instance identity 與 3 個 blueprint principal 的讀取都回 404，很可能由平台自動清除。但刪除前是否讀得到沒有確認，所以 404 不是「已證明有清除」；拆除文件寫「0b 讀回 404，推定自動清除」，不宣稱「無殘留」，並保留檢查項。
4. 使用者在 azd 的 y/N 回 N 時，Foundry 已經刪除，其他資源還在。提示要事先講明；重跑 `azd up` 會建一個新的注入帳號。
5. 實作（第 2 階段，#963，合併時只有離線替身測試；10-24 已在 `fa-priv-1024` 實跑窗口與刪除）的選擇：
   - 刪 agent 是資料面呼叫，`Disabled` 時筆電連不到。**選擇開第 5 節的部署窗口**，不靠刪 project 或帳號連帶清除：探針是先 force 刪 agent 才刪得掉帳號，刪 project 會不會連帶刪 agent 沒有測。project 已不在時略過這一步。窗口的 IP 寫進第 1 點那一次確認的說明，由同一次確認涵蓋，不另外問。
   - 刪帳號與刪 project 都只送一次，只有一個例外：刪帳號回 `CannotDeleteResource`，而列出的巢狀資源 ID 只有本帳號自己的 project（到 project 名稱為止，沒有更深的路徑），表示什麼都沒被接受；predown 每 15 秒重送一次、最多 5 分鐘，到上限仍被拒就停下，帳號還在。列到更深的資源（例如 `projects/<p>/connections/...`）、`capabilityHosts` 或別的 project 時不重送，讀一次狀態後停下；刪 project 沒有這個例外。其餘情況不論 CLI 回什麼都先讀狀態，`Deleting` 或已消失就輪詢（帳號最多 45 分鐘）；回 `GatewayTimeout` 後約 90 秒內仍不是 `Deleting` 就停下（刪除沒開始，重跑時再讀狀態）；其他錯誤且仍在原狀態才停下。重跑時 project 已在 `Deleting` 就只輪詢，不再開窗口。
   - 備份鎖在 Foundry 刪完、SAL 讀回清空之後才移除，中途停下時備份仍有鎖。

### 8. 測試

- `tests/infra`：
  - `public` 下每個資源的屬性和今天相同（含共用的 Foundry 帳號）；
  - `private` 下要有 `networkInjections`、`networkAcls: Deny`、三個 DNS zone、`snet-agents` 的委派，以及 NAT 接到 `snet-agents`；
  - VNet module 在 Foundry 之前。
- preprovision：每種被拒絕的組合各一個測試。模式不能改的檢查要讀 ARM 的假回應。
- predown：每個中斷狀態都能接續，輸入不符時零寫入。

### 9. 營運注意（第 0b 階段）

- **對話與 session 依 Entra 呼叫者隔離**（`isolation_key_source: Entra`）。另一個身分讀取會得到 403 `session_not_accessible` 或 404。`private` 下 API 的受控身分是唯一呼叫者，所以和設計一致；但營運人員用自己的身分從筆電看，**看不到 API 建立的對話**。除錯要經 API，或用同一個身分。`Disabled` 時筆電完全連不到 endpoint（T4 403），要看需要 VNet 或 VPN；實際觀察到的是 Job 與筆電互相看不到對方的對話。
- **換版後要明確建立新版 session。** 只切換 endpoint 的版本選擇器時，既有 session 仍在舊版 container，回應的 `agent_reference` 卻顯示新版（容易誤判）。要用 `POST /agents/{name}/endpoint/sessions` 帶 `version_ref` 明確建立新版 session，再帶 `agent_session_id` 呼叫，新 container 才會讀到完整歷史。這和 ADR-0003／ADR-0009 的既有做法一致，與網路無關。
- **每個新 agent 會得到新的 instance identity**，需要在 project 範圍有 Foundry User 與 Cognitive Services OpenAI User。探針已授予這兩個角色；ARM 寫入不受 `publicNetworkAccess` 影響是推論（`Disabled` 狀態下未測該寫入）；兩個角色是否都必要，未測。`private` 開記憶（Cosmos，ADR-0011）時，agent 身分另需 account 範圍的 Cognitive Services OpenAI User 才能做 embedding（2026-10-24 端到端實測）：seed Agent 由 azd postdeploy 指派，「＋」新增的 Agent 由 API 經 account 範圍的受限委派指派（ADR-0006 2026-10-24 修訂）。
- 第一次呼叫（含冷啟動）約 20 秒，之後同一 session 的回合約 3 秒。這不是 first-token 量測。

## 第 2b 階段設計：帶入既有 VNet（2026-10-24）

Neal 2026-10-24 決定第 2b 階段隨下一個 staging release 出貨。目的：企業已有 hub-spoke 網路時，FA 不再自建固定位址的 `vnet-<env>`（第 3 節：每個環境都是 192.168.96.0/24，接 hub 會重疊），改用使用者自己的 VNet 與 subnet。

### 輸入

| azd env 值 | 預設 | 意思 |
|---|---|---|
| `FA_VNET_RESOURCE_ID` | 空（FA 自建 VNet，今天的行為） | 既有 VNet 的完整資源 ID。只能搭配 `FA_NETWORK_MODE=private` |
| `FA_API_SUBNET_NAME` | `snet-api` | Container Apps 環境的 subnet |
| `FA_AGENT_SUBNET_NAME` | `snet-agents` | Foundry 注入的 agent subnet |
| `FA_PRIVATE_ENDPOINT_SUBNET_NAME` | `snet-private-endpoints` | 所有 private endpoint 的 subnet |
| `FA_DNS_MODE` | `azd` | `azd`：FA 建 private DNS zone 並連到 VNet（今天的行為）；`existing`：DNS 由使用者管，FA 不建 zone、不建連結、private endpoint 不帶 zone group |

三個 subnet 名稱與 `FA_DNS_MODE=existing` 都只在設了 `FA_VNET_RESOURCE_ID` 時有意義；沒設時 preprovision 拒絕非預設值，FA 自建的 VNet 名稱固定。

**VNet 的位置規則：**
- **同一個 subscription。** FA 的 Bicep 以 subscription 為 target scope，跨 subscription 要另外處理權限與 provider 註冊，第一版不做。
- **可以、而且必須在另一個 resource group。** 官方 FAQ：VNet 與 Foundry 資源「don't need to be in the same resource group, but they must be in the same region」（[virtual-networks](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks)）。反過來，`azd down` 刪的是整個 `rg-<env>`；VNet 若在裡面就會被刪掉。所以 preprovision 拒絕放在 `rg-<env>` 的 VNet。
- **同一個區域**（同上，「The Foundry resource must be deployed in the same region as the virtual network」）。

### FA 碰使用者 VNet 的範圍

FA 不建 VNet、subnet、NSG、NAT、公網 IP、route table，也不 PUT 任何 subnet。會落在使用者 VNet 上的只有這四樣，都是 Azure 服務在建立 FA 自己的資源（在 `rg-<env>`）時自動產生的：

1. Container Apps 環境在 API subnet 上的 service association link（SAL）；
2. Foundry 帳號注入在 agent subnet 上的 SAL（`legionservicelink`）；
3. private endpoint 在 PE subnet 上的網卡（private endpoint 本身在 `rg-<env>`）；
4. `FA_DNS_MODE=azd` 時，`rg-<env>` 裡的 private DNS zone 對這個 VNet 的 virtual network link。

部署者需要這個 VNet 的 `Microsoft.Network/virtualNetworks/subnets/join/action`（建 Container Apps 環境、注入、private endpoint）與 `Microsoft.Network/virtualNetworks/join/action`（DNS 連結）；一般是 VNet 上的 Network Contributor。

### 出站：由使用者提供

FA 不把自己的 NAT 掛到使用者的 subnet（那是改 subnet 的寫入），出站由使用者負責：
- agent subnet：Hosted 要連 FA API 公網入口、`login.microsoftonline.com`、App Insights ingestion，工具要連外部 MCP。官方要求防火牆允許 `control-{region}.identity.azure.net`、`{region}.login.microsoft.com`、`login.microsoftonline.com`（或 `AzureActiveDirectory` service tag），NSG 也要有對應的出站允許規則（[virtual-networks，Limitations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks)）。
- API subnet：Container Apps 要能拉 ACR、取 Entra token、呼叫 Foundry（經 private endpoint）與 Web PubSub。Container Apps 支援在 subnet 上接 NAT Gateway（StandardV2 不支援，[Container Apps 自訂 VNet](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks?tabs=workload-profiles-env)）。
- preprovision 只看 subnet 上有沒有 `natGateway` 或 `routeTable`，兩者都沒有時**警告、不停下**：Virtual WAN 的路由意圖可以不在 subnet 上掛 route table 就把流量送到 hub 防火牆，讀 subnet 判斷不出來。
- `FA_API_SUBNET_NAT_EGRESS=true` 搭配 `FA_VNET_RESOURCE_ID` 會被拒絕（它是 FA 自建 NAT 的開關）。

### DNS

`FA_DNS_MODE=azd`（預設）：和今天一樣，FA 在 `rg-<env>` 建下表的 zone，並連到使用者的 VNet。適合專給 FA 用、還沒有中央 DNS 的 VNet。一個 VNet 不能連兩個同名的 private DNS zone：VNet 已經連了任何一個同名的 `privatelink.*` zone（hub-spoke、由 Azure Policy 管的中央 zone）時，FA 建 virtual network link 的 PUT 會失敗（審查依 AKS 疑難排解與 Q&A 回報的錯誤推得；Learn 的 private DNS 文件沒有明文，未實測），provision 停在中途。這種 VNet 請用 `existing`。preprovision 不另外掃描 VNet 已有的 zone 連結（只寫進文件）。

中途失敗留下的東西：main.bicep 先建三個 Foundry zone 與連結（`network` module），Foundry 帳號依賴它的輸出，Table（`foundation`）、Blob（`backupFoundation`）、Cosmos DB（`memoryCosmos`）的 zone 都在帳號之後。所以衝突在 Foundry zone 時帳號還沒建、注入還沒定下來，只留下 FA 已建的 Foundry zone 與連結；只有 Blob 衝突時，帳號、Table 等已建好，FA 的 zone、連結與已建 private endpoint 上的 zone group 都留著。改成 `existing` 再部署不會移除它們（azd 是 incremental 部署，不刪範本裡沒有的資源），留著的連結繼續占住這些名稱。使用者要在 `rg-<env>` 依序手動刪：private endpoint 的 zone group、FA zone 對 VNet 的連結、FA 的 `privatelink.*` zone，指令列在 [deploy/azd/README.md](../../deploy/azd/README.md#byo-vnet)；或 `azd down` 後從頭來（帳號已建時連帳號與資料一起刪）。

VNet 設了自訂 DNS 伺服器（`dhcpOptions.dnsServers`）時，連到它的 private DNS zone 不會被自動查詢（「if you define a custom DNS server in a virtual network, the system doesn't automatically query private DNS zones linked to that virtual network」，[private-dns-privatednszone](https://learn.microsoft.com/en-us/azure/dns/private-dns-privatednszone)），所以 `azd` 搭配自訂 DNS 伺服器會被 preprovision 拒絕，請用 `existing`。

`FA_DNS_MODE=existing`：FA 不建 zone、不建連結，private endpoint 也不帶 zone group。使用者要讓這個 VNet（含 Hosted 與 Container Apps）把下表的名稱解析到 private endpoint 的 IP：常見做法是 hub 的中央 zone 加 Azure Policy 自動掛 zone group，或自己建 A 紀錄；自訂 DNS 伺服器要對下表的公網 zone 設條件轉送到 168.63.129.16。

| Private DNS zone | 名稱（`<token>` 是 main.bicep 的 `uniqueString(subscription, env, region)`） | 何時 |
|---|---|---|
| `privatelink.cognitiveservices.azure.com`、`privatelink.openai.azure.com`、`privatelink.services.ai.azure.com` | `cog-<token>` | 一律（Foundry 帳號，官方三個 zone） |
| `privatelink.table.core.windows.net` | `st<token>` | 一律（工作狀態 Table） |
| `privatelink.blob.core.windows.net` | `stbk<token>`（備份與 M3）、`stcon<token>`（工作台，由 deploy hook 建）、`stsnap<token>`（`FA_DEPLOY_UPGRADE_SNAPSHOTS=true`） | 一律／見左 |
| `privatelink.documents.azure.com` | `cosmos-<token>` 與 `cosmos-<token>-<region>` | 記憶開啟（ADR-0011 Cosmos DB） |

IP 要等 private endpoint 建好才知道，所以 `existing` 時 postprovision 讀回 `rg-<env>` 每個 private endpoint 的 `customDnsConfigs`（FQDN 與 IP）印出來，並在 `FA_DNS_RECORDS_READY=true` 之前停下，不進 deploy（否則 sync job 連不到 Table，錯誤很難看懂）。工作台的 private endpoint 是 deploy 期間才建的，deploy 結束時再印一次。`FA_DNS_RECORDS_READY` 由使用者設，但不列入 `USER_INPUTS`：postdown 會清掉它，重建環境時 IP 會變，要重新確認。它設了之後就一直有效，閘門只在 postprovision 看這個值：之後才打開記憶（Cosmos DB）或 `FA_DEPLOY_UPGRADE_SNAPSHOTS` 新增的 private endpoint 不會再被擋下，使用者要從每次 postprovision 印出的清單補上新名稱（寫在 README）。

### preprovision 檢查（唯讀，任何一項不符就停下，不寫任何東西）

在 `check_network_mode` 之後、任何 `azd env set` 之前，讀 VNet（一次 GET）與三個 subnet：

- `FA_DNS_MODE` 只能是 `azd` 或 `existing`；沒設 `FA_VNET_RESOURCE_ID` 時只能是 `azd`，subnet 名稱也不能設。
- `FA_VNET_RESOURCE_ID` 要是 `FA_NETWORK_MODE=private`、格式正確、同一個 subscription、不在 `rg-<env>`、存在、區域等於 `AZURE_LOCATION`。
- 三個 subnet 名稱互不相同（agent subnet 不能和 Container Apps 共用：官方「Each Foundry resource must use a dedicated agent subnet」，Container Apps 也要求專用 subnet），agent subnet 名稱不超過 63 bytes（官方限制），三個都存在。
- agent subnet：委派給 `Microsoft.App/environments`、至少 /27（官方最小值；小於 /24 時警告，官方 production 建議 /24，[deep-dive](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agents-networking-deep-dive)）、位址在 RFC 1918 內（本設計刻意不支援 CGNAT：[how-to Limitations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks) 允許 RFC 6598 的 100.64.0.0/10 扣除保留段，deep-dive 只列 RFC 1918，取較窄的一份），且不和保留位址重疊：Container Apps workload profiles 保留的 169.254.0.0/16、172.30.0.0/16、172.31.0.0/16、192.0.2.0/24、100.100.0.0/17、100.100.128.0/19、100.100.160.0/19、100.100.192.0/19（[Container Apps 自訂 VNet](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks?tabs=workload-profiles-env)），加上 Foundry 另列的 100.100.224.0/19、0.0.0.0/8、127.0.0.0/8（[virtual-networks，Limitations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks)）。兩份清單不同，取聯集（`APP_RESERVED`）。
- VNet 位址空間（`addressSpace.addressPrefixes`，已讀到的 VNet，不多一次 GET）：不和上述保留位址重疊（Foundry 要求整個 VNet 位址空間都不重疊；peer 的 VNet 不讀）。
- API subnet：委派給 `Microsoft.App/environments`、至少 /27（workload profiles 環境的規則；FA 的環境是 workload profiles，所以不是 consumption-only 的 /23、不委派），不和上述保留位址重疊。
- **subnet 被別人用了**：agent subnet 有 SAL，而這個環境的 Foundry 帳號（main.bicep 的固定名稱）還不存在，就是別的 Foundry 帳號在用，停下；帳號已存在時，它的 `networkInjections[].subnetArmId` 必須就是這個 subnet（注入建立後不能改，換了 `FA_VNET_RESOURCE_ID` 或 subnet 名稱就停下）。沒設 `FA_VNET_RESOURCE_ID` 而帳號已存在時，帳號的注入 subnet 必須是 FA 自己 `vnet-<env>` 的 `snet-agents`，否則在寫入前停下（清掉 BYO 設定的環境原本會在 deploy 中途的帳號 PUT 失敗）。API subnet 有 SAL 時讀 `cae-<env>`：不存在或用的是別的 subnet 就停下。
- PE subnet：不能有委派（private endpoint 不能放在委派的 subnet），至少 /28（FA 最多約 10 個 private endpoint IP）。`privateEndpointNetworkPolicies` 開或關都可以，不檢查。
- `FA_DNS_MODE=azd` 而 VNet 有自訂 DNS 伺服器：停下（見上）。
- 出站：見上，只警告。

### Bicep

- `private-state-network.bicep` 加 `vnetResourceId`：有值時不建 VNet、NSG、NAT、公網 IP；`FA_DNS_MODE=azd` 時仍建三個 Foundry zone 並連到使用者的 VNet。
- 其他 module（`foundation`、`backup-foundation`、`upgrade-snapshots`、`memory-cosmos`、`foundry`、`web-console`）各加「subnet／VNet 資源 ID 覆寫，空字串保持今天的運算式」與 `privateDnsZones`（false 不建 zone、連結與 zone group）。
- 預設值保持今天的 ARM 運算式原樣，所以 `public` 與沒帶 VNet 的 `private` 部署出來的資源與今天相同（正規化 leaf 比對）：`tests/infra` 把編譯結果正規化成 leaf，public 和 baseline 比，private 預設值和 main 的 leaf 的 hash 比。
- 輸出 `FA_VNET` 在帶入時是使用者 VNet 的名稱。

### 刪除（predown／postdown）

- 使用者的 VNet 與 subnet 在另一個 resource group，`azd down` 刪 `rg-<env>` 時不會碰到；preprovision 已拒絕放在 `rg-<env>` 的 VNet。
- 刪除順序不變（第 7 節）。predown 最後等的 SAL 改讀使用者的 agent subnet（`FA_VNET_RESOURCE_ID` 加 `FA_AGENT_SUBNET_NAME`）。agent subnet 的 SAL 在帳號刪除過程中清空（探針：比帳號消失早約 6.5 分鐘），predown 讀回清空才交給 azd。
- API subnet 的 Container Apps SAL 在 azd 刪掉 Container Apps 環境（約 27 分鐘，第 7 節）之後才會釋放；`azd down` 結束後短時間內那個 subnet 仍可能顯示 SAL，不能馬上給另一個環境用。這段時間沒有實測。
- `FA_DNS_MODE=azd` 的 zone 與 VNet 連結都在 `rg-<env>`，跟著 resource group 一起刪；`existing` 時使用者自己建的紀錄要自己清。委派、NSG、NAT、route table 都是使用者的，FA 不動。

### 驗收需要的雲端前置（第 3 階段，另經 Neal 批准）

一個預先建好的 VNet：與 FA 同 subscription、同區域、在另一個 resource group；RFC 1918 位址，不和保留位址重疊；API subnet 至少 /27、agent subnet 至少 /27（建議 /24），兩者都委派給 `Microsoft.App/environments` 且沒有 SAL；一個沒有委派、至少 /28 的 PE subnet；兩個委派 subnet 上的 NAT Gateway（或 route table 到防火牆，並放行上述網域）；部署者在 VNet 上有 Network Contributor。`existing` 另需一組使用者管理的 zone 或 DNS 伺服器。

## WAF 五大支柱

- **可靠性**：
  - subnet 建好後不能改大小，直接用 /24。
  - 用盡時回 429、平台不預警。FA 每位助理一個 session，個人或小團隊遠低於上限。
  - 刪除順序寫進 hook，並且可重試。
- **安全性**：
  - Foundry 帳號／project 資料面與 agent endpoint 入站私有（[探針] `Disabled` 時公網呼叫 agent endpoint 回 403，第 0b 階段有保留的原始 log）；只有部署窗口暫開的部署者 IP 例外。
  - 對話與 session 依 Entra 呼叫者隔離；`private` 下 API 受控身分是唯一呼叫者（見第 9 節）。
  - agent 在你的 subnet，可自行加 NSG、UDR、防火牆。
  - 公網入口只剩工作台與 API，兩個都要 Entra。
  - **Team 資料仍經公網 TLS 到 API**，第一版沒有出站過濾。這兩點寫進文件，不宣稱「全私有」。
- **成本**（[推論]，牌價）：
  - NAT Gateway 約 USD 33＋流量、它的 Standard 公網 IP 約 USD 3.65、Foundry private endpoint 約 USD 7.3、三個 DNS zone 約 USD 1.5，合計每月約 USD 50＋資料處理費。
  - D 約 0（核准式出站加 Firewall Basic 約 USD 288）。B 約 USD 510 起 [推論]（這裡的 B 是上方選項比較表的 Front Door 方案，不是長期記憶的選項 B）。
  - 長期記憶選項 B（Cosmos DB serverless＋private endpoint）的費用不含在上列，由 [ADR-0011](0011-private-mode-cosmos-memory.md) 估算。
- **營運卓越**：
  - 一個選項、兩個值。不能事後切換，preprovision 讀 ARM 擋下。
  - 探針基礎設施部署約 5–7 分鐘（不含 FA 應用與 azd hook；[探針]）；刪除慢，`azd down` 預期 45 分鐘以上。
  - 第 0 階段第 2 題已通過，部署主路徑不改任何腳本。
- **效能**：
  - Hosted 出站多一段 NAT：[探針回報] 68–292 ms；Hosted → FA dev API 公網 `/healthz` 19–54 ms，`login.microsoftonline.com` 50–292 ms（第 0b 階段，約 14 次觀察）。
  - 模型改走 private endpoint（[探針] Hosted 內呼叫回 200）；toolbox 未實測；first-token 延遲未量測。

## 分階段

| 階段 | 內容 | 需要誰 |
|---|---|---|
| 0 探針 | **已完成（2026-10-23）**：dev 訂閱新 RG：注入的 Foundry、private endpoint、NAT、一個 Hosted Agent。已刪除並 purge，費用估計 USD 1–3（實際金額待查）。結果見「第 0 階段結果」 | 已批准（2026-10-23）（新資源；預估 USD 5–10） |
| 0b 平台對話接續與未測項目 | **已完成（2026-10-23）**：Hosted 內呼叫模型、平台對話儲存的多輪與換版接續、agent endpoint 403 的原始證據、Hosted→FA dev API 公網 `/healthz` 可達（經 NAT 為推論）、App Insights 擷取、身分端點。RG `rg-fa-netprobe0b-1023` 已刪，費用估計 USD 1–2（實際金額待查）。結果見「第 0b 階段結果」 | 已批准（2026-10-23，Neal：「好」）（新資源與費用） |
| 0c 剩餘未測項目 | 見「第 0b 階段結果」的「仍未測」。**建議**併入第 3 階段驗收（需要完整 FA 環境：WebIQ MCP 出站、toolbox、重跑 `azd provision`、FA API app 呼叫 agent、desktop 完整啟動、帶身分的 Hosted→API 呼叫），不再另開探針；是否另開由 Neal 決定 | 併入各階段，無新增批准 |
| 1 Bicep | 網路 module 移到 Foundry 前（兩種模式，用測試證明 public 不變）、`snet-agents`、注入、private endpoint、NAT 條件拆開、preprovision 檢查、provider 清單。不建 project capability host。**已實作於 #961（10-24 隨 `fa-priv-1024` 部署，見第 3 階段）**；單有這一階段 `private` 不能用 | 一般 PR |
| 2 部署與刪除（azd hook） | 部署期間只允許部署者 IP（第 2 題已通過，Job 為備案）；predown 的確認、刪除順序與重試（force 刪 agent、先刪 project、輪詢帳號 provisioningState）；拆除文件列出 Entra agent 身分殘留檢查；文件警告。**已實作於 #963（10-24 在 `fa-priv-1024` 實際跑過窗口與刪除，見第 3 階段）** | 一般 PR |
| 2 其餘：Work IQ 防護 | API 在 `private` 拒絕新增 Work IQ connector grant（`FA_NETWORK_MODE` 傳給 API）。**已實作於 #970（離線測試；10-24 隨 `fa-priv-1024` 部署，見第 3 階段）**：azd profile 只在 `private` 把 `FA_NETWORK_MODE=private` 放進 API 環境；加入或重新啟用 Work IQ 回 422 `TEAM_CONNECTOR_PRIVATE_NETWORK`，停用仍可，不刪除既有 grant；停用時不能帶 client secret 由 #974 加上（已合併；這項拒絕沒有在雲端觸發過。#974 的帳號刪除重送已在 10-24 `fa-priv2-1024` 拆除時實際跑過：`CannotDeleteResource` → 重送 → GatewayTimeout → 之後只輪詢，[紀錄](../progress.md#private-reruns-1024)）。防護已部署但沒有實際觸發過：azd 預設不安裝 Connectors 路由 | 一般 PR |
| 2b 帶入既有 VNet | `FA_VNET_RESOURCE_ID`、subnet 名稱、使用者提供 DNS（`FA_DNS_MODE`）。設計見「[第 2b 階段設計](#第-2b-階段設計帶入既有-vnet2026-10-24)」；Neal 2026-10-24 決定隨下一個 staging release 出貨。**已實作於 #976；`FA_DNS_MODE=azd` 已在雲端驗證（2026-10-24 `fa-byo-1024`，[紀錄](../progress.md#private-reruns-1024)），`existing` 未在雲端測** | 一般 PR |
| 2c 長期記憶後端 B | 先寫 [ADR-0011](0011-private-mode-cosmos-memory.md)：`private` 用 Cosmos DB serverless＋private endpoint 作 FA 自建長期記憶後端；再實作。**在第 3 階段驗收之前完成** | ADR-0011 先經 Neal 接受；實作為一般 PR（雲端資源另批准） |
| 3 驗收 | 新 azd 環境以 `private` 跑 `azd up` 到「前 10 分鐘」通過（含 0b 與 2c 的結論，並覆蓋 0c 的未測項目），再 `azd down`；文件。**已通過（2026-10-24）**：`fa-priv-1024` 核心流程（`azd up`、窗口開關、前 10 分鐘、2c 記憶的寫入／回想／更正／Team note、`azd down` 無殘留，[紀錄](../progress.md#private-e2e-1024)）；兩輪乾淨重跑 `fa-priv2-1024`（azd 建的 VNet）與 `fa-byo-1024`（2b 帶入既有 VNet，DNS azd 模式）都通過，驗證 #972、#975、#976、#978 的事先批准與 #974 的刪除重送（[紀錄](../progress.md#private-reruns-1024)）。未收：`FA_DNS_MODE=existing`；#973 有部署未特別測；Work IQ 防護未實際觸發；費用；0c 其餘項目未逐項核對 | **Neal 批准**（新資源與費用） |
| 之後 | `isolated`（API 與工作台內網化、私有 ACR、AMPLS）；Memory Store 若平台支援帳號入站限制，再評估接回 | 另案 |

第 0 階段要實測的問題（結果見下一節；探針自己的題號和這份清單不同，下一節的表有對照）：
1. `publicNetworkAccess: Disabled` 時，筆電的 `azd provision`（ARM）是否成功。
2. **注入的帳號能否暫時設成 selected IPs，讓筆電的資料面步驟成功。** 這一項決定第 2 階段走首選還是備案。
3. 要不要明確建立 project capability host；沒建時 Hosted Agent 能否運作。同時記錄實際存在哪些 capability host（account 層、project 層），供刪除步驟使用。
4. Memory Store API 在注入的帳號上：完全不能用、只能從 VNet 用，還是照常可用。
5. Container Apps 上的 API 能否把 `<account>.services.ai.azure.com` 解析到 private endpoint 的 IP，並成功呼叫 agentsv2 invocations。
6. Hosted 經 NAT 呼叫 API 公網入口、WebIQ toolbox 的 MCP 出站、App Insights 擷取、身分端點是否都成功，以及延遲。第 0b 階段修正：`*.identity.azure.net` 不適用，Hosted 的 token 來自平台提供的 IP 端點（HTTP）；`login.microsoftonline.com` 實測可達（50–292 ms），是否必要未測。結果見「第 0b 階段結果」。
7. Hosted 從公網 Basic ACR 拉 image 是否成功。
8. agent endpoint 是否仍公開可達（azd pivot 的說法）。
9. 192.168.97.0/24 在所選區域可用。
10. 刪除：SAL 清空要多久，`azd down` 的總時間。
11. 同一個環境連跑兩次 `azd provision`，含 postdeploy 中斷後再跑：帶著不變的 `networkInjections` 重新 PUT 是否成功；若上次留下暫時的 selected IPs 狀態，Bicep 是否把 `publicNetworkAccess` 還原成 Disabled；private endpoint 與 NSG 是否冪等。
12. 從 Hosted 內部，`<account>.services.ai.azure.com` 是否解析到 private endpoint 的 IP，模型與 toolbox 呼叫是否成功。（第 0b 階段：解析與模型呼叫通過，toolbox 未測。）

## 第 0 階段結果（2026-10-23）

範圍：dev 訂閱新 RG，westus3，約兩小時。RG 已刪除，兩個 Foundry 帳號已 purge。費用估計 USD 1–3，實際金額約 8–24 小時後才查得到（RG `rg-fa-netprobe-1023`，資源 tag `workload=fa-netprobe-1023`，可用來查費用），待補。原始紀錄留在總協調的本機，不進 repo；下表只留去敏的結論，沒有任何資源 ID。

**證據範圍：** 下表來自探針回報，不是全部都在保留的原始 log 裡。只有探針回報、原始 log 未保留的項目：帳號層級 capability host 約 3 分鐘、版本約 1 分鐘 active；ARM PATCH 回 200 與資料面 4 秒／3 秒生效（保留的 log 只見 15 秒的 PATCH）；記憶的 `ResourceError`／模型 500；刪帳號前的 `CannotDeleteResource`；RG 卡在 Container Apps 環境；NAT 延遲 68–292 ms（保留的原始 log 只見 69／84／132／147 ms）。agent endpoint 403 已在第 0b 階段用保留的原始 log 重驗（見「第 0b 階段結果」T4），不再是只有探針回報的項目。

| 探針題 | 對應上文題號 | 結果 | 重點證據 |
|---|---|---|---|
| Q1 `Disabled` 時筆電 ARM 部署 | 1 | PASS | 部署 4 分 38 秒；之後的 ARM 寫入都成功 |
| Q2 部署期間只允許部署者 IP | 2 | PASS | ARM PATCH 回 200，15 秒套用完成，`networkInjections` 不變；資料面約 4 秒生效，關回約 3 秒生效；`Disabled` 時資料面 403 "Public access is disabled" |
| Q3 Hosted Agent，不建 project capability host | 3 | PASS | 平台自動建帳號層級 capability host（約 3 分鐘，與帳號同時）；版本約 1 分鐘 active；`/responses` 200 |
| Q4 Memory Store | 4 | **FAIL（不能用）** | 帳號入站只要是 `defaultAction: Deny`，記憶背景作業失敗（`ResourceError`、模型 500）、搜尋 408，與是否注入無關；同一個帳號改成只開 IP 後同樣壞；經 private endpoint 也一樣。對照：未注入、全開（`Allow`）的帳號正常 |
| Q5 VNet 內 DNS 與 private endpoint | 5、12 | PASS | 三個 zone 解析到 private endpoint 的 IP（Job 與 Hosted 兩邊都是）。同一個 Container Apps 環境內的 Job 代替 API app，呼叫 agents-list 與 agent 的 `/responses` 都 200，Job 內呼叫模型也 200；Hosted 內沒有呼叫模型。API app 本身與 invocations 沒有單獨測 |
| Q6 出站與 agent endpoint | 6、8 | 出站 PASS；agent endpoint 私有 | Hosted 經 NAT 出站（來源 IP＝NAT IP，探針回報 68–292 ms）；`Disabled` 時公網呼叫 agent endpoint 回 403，隨帳號的公網設定走（探針回報）。旁證：selected IPs 窗口內，允許的筆電 IP 呼叫 agent endpoint（`/responses`、invocations）都回 200；同批留存紀錄另有一筆 HTTP 500（`server_error`，沒有細節，log 沒標是哪個呼叫），原因不明。第 0b 階段 T4 重驗 403 |
| Q7 公網 Basic ACR 拉 image | 7 | PASS（拉取） | FA desktop image 已從公網 Basic ACR 拉取，Python app 也啟動了，但隨即自行結束：探針沒給 API audience 設定（回 424 `session_not_ready`，app log 為 exact API audience is required），不是網路問題。完整的 desktop 啟動沒有測。拉取的證據是探針自己的 agent image 在同一個 ACR，`/responses` 回 200 |
| Q8 時間 | （無） | 資訊 | 帳號加兩個模型部署 3 分 20 秒。探針沒建 project capability host，因此沒有文件所說的 30–35 分鐘那段；帳號層級的由平台自動建（探針回報約 3 分鐘）。不表示文件的數字有誤 |
| Q9 192.168.97.0/24 | 9 | PASS | 帳號建立即出現 service association link；agents 與 Container Apps subnet 分開、共用 NAT |
| Q10 刪除 | 10 | PASS（有坑） | 有 session 時刪 agent 回 409，要 `force=true`；先刪 project 才能刪帳號；注入帳號刪除約 22 分鐘，SAL 比帳號早清空；resource group 刪除約 27 分鐘，卡在 Container Apps 環境；`azd down` 預期 45 分鐘以上 |

### 沒有測的項目

第 0 階段當時未測的項目，已由第 0b 階段處理的有：Hosted 內呼叫模型、Hosted → FA API 經 NAT（`/healthz`）、App Insights 擷取、身分端點（`*.identity.azure.net` 不適用）、平台對話儲存的多輪與換版接續（FA checkpoint 未測）。仍未測的項目見「第 0b 階段結果」的「仍未測」。

### 對本文的影響

- 部署走主路徑（暫開部署者 IPv4，PATCH 只送 `publicNetworkAccess` 與 `networkAcls`，結束讀回 `Disabled`）；Job 備案也實測可行。
- 不建 project capability host；探針基礎設施部署時間改為約 5–7 分鐘，不含 FA 應用與 azd hook（第 0b 階段修正）。
- agent endpoint 在本設計的設定下隨帳號 `Disabled` 為私有。
- Foundry Memory Store 在 `private` 不可用（任何 `Deny` 都壞）。`private` 的長期記憶採記憶選項 B，見「長期記憶的選項」。
- 刪除流程補上 force 刪 agent、先刪 project、輪詢帳號 `provisioningState`、CLI 報錯時先讀狀態、`azd down` 預期 45 分鐘以上。
- 可能的殘留：建立 Hosted 版本時平台自動建的 Entra agent instance identity 與 blueprint。探針沒有 Graph 權限，無法確認刪除後是否清除，需有權限者查。

## 第 0b 階段結果（2026-10-23）

範圍：dev 訂閱新 RG `rg-fa-netprobe0b-1023`（資源 tag `workload=fa-netprobe0b-1023`，可用來查費用），沿用第 0 階段的設定（BYO VNet 注入、private endpoint、NAT、公網 Basic ACR），約 1.5 小時。RG 已刪、帳號已 purge。費用估計 USD 1–2，實際金額約 8–24 小時後用 tag 與 RG 名稱查，待補。原始紀錄（evidence log 與時間線）留在總協調的本機，不進 repo；下表只留去敏的結論，沒有任何資源 ID、身分或 IP。與第 0 階段不同，下表各項都有保留的原始 log。

| 探針題 | 結果 | 重點證據 |
|---|---|---|
| T1 Hosted 內呼叫模型 | PASS | Hosted 內模型主機解析到 private endpoint 的 IP，模型呼叫回 200。部署者 IP 窗口內與帳號 `Disabled` 兩種狀態都成功（`Disabled` 時從 VNet 內的 Job 呼叫 agent，Job 沒有直接呼叫模型）。這補上了 ADR-0006「模型由 VM 直連」的缺口 |
| T2 多輪對話接續 | PASS | 測的是平台 Responses 對話儲存加 `get_history()`，不是 FA checkpoint：回傳第 1 回合的 items，模型答對第 2 回合的問題。`Disabled` 時從 VNet 內的 Job 以 ASCII 版（t7c）通過。T2 第一次嘗試與 t7b 因中文經 `az rest` 遺失而無效，有效結果是 T2 重試與 t7c |
| T3 換版後接續 | PASS，有條件 | 只切換 endpoint 的版本選擇器：既有 session 仍留在舊版 container，回應的 `agent_reference` 卻顯示新版（會誤導）。明確建立新版 session（`POST /agents/{name}/endpoint/sessions` 帶 `version_ref`）並帶 `agent_session_id`：新 container 讀到完整歷史並答對。與 FA 既有做法（ADR-0003／0009）一致，不是網路問題。換版成本：建立新版 session 約 4.0 秒，其首回合約 8.1 秒（對 G 方案有影響） |
| T4 公網呼叫 agent endpoint | 403，有保留證據 | `/responses` 與 `/invocations` 都回 `HTTP/1.1 403 Forbidden`，訊息 "Public access is disabled. Please configure private endpoint."（同一時段 agents-list 也是 403）。取代第 0 階段「探針回報，原始 log 未全數保留」的說明 |
| T5 Hosted → FA dev API 與身分端點 | PASS | Hosted 呼叫 FA dev API 公網 `/healthz` 可達（回 200，19–54 ms；「經 NAT」是推論，log 的出站 IP 已去敏）；`login.microsoftonline.com` 回 200，50–292 ms（約 14 次觀察）。這只證明出站網路，不是帶身分驗證的 Team 資料呼叫 |
| T5b `*.identity.azure.net` | 不適用 | Hosted 的 token 來自平台提供的 IP 端點（HTTP），不經這個網域。`login.microsoftonline.com` 實測可達，是否必要未測 |
| T6 App Insights 擷取 | PASS | 結論限於網路與 ingestion 可達：app 用環境變數 `APPLICATIONINSIGHTS_CONNECTION_STRING`（推定為平台注入）直接 POST 到公網 ingestion；12 筆 trace 全部數秒內到達；此路徑不需要 AMPLS |
| 部署時間 | 資訊 | 探針基礎設施約 6 分鐘，不含 FA 應用與 azd hook（第 0 階段 4 分 38 秒），改為「探針基礎設施部署約 5–7 分鐘（不含 FA 應用與 azd hook）」 |
| 拆除 | 資訊 | 依第 0 階段順序，共 50 分鐘：帳號約 22 分鐘（CLI 回 `GatewayTimeout` 時帳號其實已在 `Deleting`，要讀狀態與輪詢，不要重送）、purge 約 70 秒、RG 約 26 分鐘 |
| Entra agent 身分殘留 | 讀回 404 | force 刪 agent 後，3 個 instance identity 與 3 個 blueprint principal 經 Graph 讀取都回 404，很可能由平台自動清除。刪除前是否讀得到沒有確認；對照組（Job 的受控身分）讀取成功，表示讀取權限有效 |

另有三個營運事實，已寫入第 9 節：對話與 session 依 Entra 呼叫者隔離（另一個身分得 403／404）；換版要明確建立新版 session；每個新 agent 得到新的 instance identity，需要 project 範圍的 Foundry User 與 Cognitive Services OpenAI User（探針已授予這兩個角色；ARM 寫入不受 `publicNetworkAccess` 影響是推論，`Disabled` 下未測；兩個角色是否都必要，未測）。

### 仍未測

- WebIQ MCP 出站；
- toolbox 經 private endpoint；
- 重跑 `azd provision`（上文第 11 題）；
- FA API app 本身（Container Apps）呼叫 agent；
- FA desktop image 的完整啟動；
- 帶身分驗證的 Hosted → FA API 呼叫；
- FA checkpoint 經 API 保存與恢復（`private` 下）未測；儲存路徑（API→Table／Blob 經 PE）與 public 相同，只有 Hosted→API 改經 NAT（已測可達），推論不受影響；
- Foundry User 與 Cognitive Services OpenAI User 兩個角色是否都必要；
- first-token 延遲；
- idle 停止的 session 在版本選擇器切換後，喚醒時用舊版還是新版；
- 注入的帳號開 `Allow` 後的記憶（對 `private` 沒有用，不列入計畫）。

### 對本文的影響（第 0b 階段）

- 移除「Hosted 內呼叫模型未測」：模型由 VM 直連經 private endpoint 可用。
- 平台對話儲存的多輪與換版接續可用，條件是換版時明確建立新版 session。FA 的短期記憶是 SDK checkpoint 經 API 存 Table／Blob（ADR-0006、CTX-03 契約），`private` 下未測。
- agent endpoint 403 有保留證據，原本的證據範圍說明已移除。
- 身分端點的必要網域是 `login.microsoftonline.com`；App Insights 不需要 AMPLS。
- 探針基礎設施部署時間改為約 5–7 分鐘（不含 FA 應用與 azd hook）；Entra 殘留檢查項保留，但改為「0b 讀回 404，推定自動清除」。

## Neal 的決定（2026-10-23）

- **BYO VNet 是硬需求**：走選項 A，D（managed VNet）只留作參考。
- **第 0 階段探針已批准**（預估 USD 5–10），在 dev 訂閱的新 RG 執行，用完照順序刪除並 purge。
- **範圍已接受**（Neal 原文：「可以接受，先照這個範圍做」）：入站私有、agent 在你的 subnet；Team 資料仍經公網 TLS 到 API；第一版無出站過濾；工作台與 API 留公網＋Entra；只能在建立新環境時選。全內網另案為 `isolated`。
- **長期記憶（Neal 2026-10-23，探針結果出來後）**：
  - 「長期記憶是一個重要的元素，不能沒有。」
  - 「私有化的部署方案可以直接選 B。預設的部署方案維持不變」
  - 意思：`private` 採記憶選項 B（FA 自建長期記憶後端）；預設的 `public` 維持 Foundry Memory Store，不變。當時 B 寫的是 Azure AI Search＋private endpoint；總協調比較 AI Search 與 Cosmos DB 之後，Neal 同日改選：「用Cosmos DB serverless」。詳細設計見 [ADR-0011](0011-private-mode-cosmos-memory.md)。
- **審查補充（2026-10-23，已回報 Neal）**：Neal 接受範圍之後，審查才發現 agent endpoint 在 preview 期間可能仍是公開位址（官方 azd 文件），已回報 Neal。本文因此把「入站私有」限縮為 Foundry 帳號／project 資料面，並在第 0 階段第 8 題實測；Neal 的決定原文不變。**探針結果：`Disabled` 時 agent endpoint 回 403，這個疑慮對本設計不成立，限縮的文字保留作紀錄。**

## 長期記憶的選項

探針的結論：Foundry Memory Store 只在帳號入站 `Allow` 時能用；任何 `Deny`（包含只開特定 IP）都會讓背景作業失敗，所以 `private` 不能用它。下表的代號是 Neal 做決定時的選項清單，和探針前的舊表不同。

| 選項 | 做法 | 結果 |
|---|---|---|
| A1. `private` 不提供長期記憶 | FA 在記憶關閉時已會正常降級（回 `not_configured`） | 沒有長期記憶；Neal 不接受 |
| A2. 另開一個完全公開的記憶帳號 | `Allow`、只靠 Entra | 記憶內容經公網到一個沒有網路限制的帳號，和 `private` 的目的衝突；未採用 |
| A3. 等平台 | 等 Memory Store 支援帳號入站限制 | 沒有時程；未採用 |
| **B. FA 自建長期記憶後端（Neal 選定）** | 只在 `private` 使用。後端原寫 Azure AI Search＋private endpoint；Neal 2026-10-23 比較後改選 Cosmos DB serverless＋private endpoint | **採用（後端：Cosmos DB serverless）**。詳細設計見 [ADR-0011](0011-private-mode-cosmos-memory.md) |
| ~~只允許兩個 IP 的記憶帳號~~（探針前的舊記憶選項 B） | 另開不注入的帳號，只允許 NAT 與 API 的出站 IP | **已被推翻**：selected IPs 也屬 `Deny`，記憶一樣壞 |

Neal 2026-10-23 的決定：「長期記憶是一個重要的元素，不能沒有。」「私有化的部署方案可以直接選 B。預設的部署方案維持不變」。所以：

- `private`：長期記憶走記憶選項 B（後端 Cosmos DB serverless，[ADR-0011](0011-private-mode-cosmos-memory.md)）。ADR-0011 的實作完成前，`private` 仍須搭配 `FA_ENABLE_MEMORY=false`。
- `public`（預設）：不變，維持 Foundry Memory Store。
- 記憶選項 B 是 FA 自建，和 AGENTS 的 SDK-first 原則有張力。探針已提供具體缺口（官方 Memory Store 在 `Deny` 帳號上壞掉）；ADR-0011 要引用探針結果，並寫明介面、資料所有權（以 Team 為邊界，ADR-0006）、費用與遷移。

## 原本要 Neal 決定的事（保留紀錄）

1. **BYO VNet 是不是硬需求？** 是的話走 A；不是的話，D（managed VNet）比較省事。**已回答：是，走 A。**
2. **接受 `private` 的範圍嗎？** Foundry 入站私有、agent 在你的 subnet；但 Team 資料仍經公網 TLS 到 API、第一版沒有出站過濾、工作台與 API 留公網（Entra）。全內網列為之後的 `isolated`。**已回答：接受（agent endpoint 公開的限制是之後才發現的，見「Neal 的決定」）。**
3. **`private` 先不提供長期記憶**，接受嗎？**已回答（2026-10-23）：不接受，長期記憶不能沒有；`private` 採記憶選項 B（FA 自建；後端由 AI Search 改選 Cosmos DB serverless，見 [ADR-0011](0011-private-mode-cosmos-memory.md)）。**
4. **第 0 階段探針**：在 dev 訂閱建立並刪除一組新資源，預估 USD 5–10。**已回答：批准（2026-10-23）。**

## 參考

- https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/networking-options
- https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agents-networking-deep-dive
- https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks
- https://learn.microsoft.com/en-us/azure/foundry/how-to/configure-private-link
- https://learn.microsoft.com/en-us/azure/foundry/how-to/managed-virtual-network
- https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents
- https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-memory#limitations
- https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox-network-isolation
- https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-private-azure-container-registry
- https://learn.microsoft.com/en-us/azure/container-apps/networking
- https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks?tabs=workload-profiles-env
- https://learn.microsoft.com/en-us/azure/dns/private-dns-privatednszone
- https://learn.microsoft.com/en-us/azure/container-registry/container-registry-private-link
- https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns
- https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep
- https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/cognitive-services/account
- 本 repo：
  - [ADR-0002](0002-shared-context-without-file-share.md)：Memory Store 與 VNet 的取捨。本 ADR 不取代 ADR-0002；`private` 時不用 Memory Store，改記憶選項 B（[ADR-0011](0011-private-mode-cosmos-memory.md)）；
  - [ADR-0006](0006-team-trust-boundary-grokbot-mode.md)：Team 信任邊界與 API 轉送；
  - [ADR-0008](0008-environment-tiers-staging-cicd.md)：遠端工作台 Entra；
  - [azd 新環境設計](../azd-new-environment.md)。
