# Hosted agent 經企業 AI Gateway 呼叫模型：官方能力與 dev 實測設計

> 狀態：**dev 已實測**（2026-10-29，結果見最後一節）。本文回答客戶的問題，並記錄 dev 實測的設計、費用、批准點與結果。

## 問題

客戶問：hosted agent 能不能使用企業 AI Gateway 上的模型？Gateway 後面可能是 Foundry 模型，也可能是地端的 OpenAI 相容模型。

## 結論（依官方文件；dev 實測結果見最後一節）

**可以，但要由 agent 程式自己呼叫 gateway，不能用 Foundry 內建的「BYOM」連線。**

| 做法 | 適用對象 | Hosted agent |
|---|---|---|
| Foundry 內建 BYOM：在 Admin-connected models 註冊 APIM 或其他 gateway，agent 選那個模型 | 只有 prompt agent | 不支援。文件寫明只支援 prompt agent、不支援 Responses API，Copilot harness 也不支援 |
| Agent 程式直接呼叫 gateway 的網址 | hosted agent | 技術上可行：模型呼叫是容器裡的程式發的，平台不攔 |

依據：

- Hosted agent 的程式會自己呼叫模型、工具與下游服務：「Your agent code … can call Foundry models, Toolbox tools, and downstream Azure services」。
- BYO VNet 下，agent 自己的對外流量「Direct, through the Micro VM's dedicated NIC in the delegated subnet」；只有工具呼叫走 Data proxy。
- 私網部署可以連到「private resources such as databases or internal APIs」。
- 文件**沒有**明說可接非 Foundry 模型，只有免責：接第三方或非 Azure 模型風險自負，內容安全也要自己做。

兩種情境：

- **APIM 前面接 Foundry 模型**：APIM 放私網，agent 用自己的 Entra 身分取 token，APIM 用 `validate-azure-ad-token` 驗證，再用 APIM 的受控身分呼叫 Foundry，全程不用金鑰。也能在 APIM 對每個 Team 設 token 上限（ADR-0006 已列為之後的項目）。
- **地端 OpenAI 相容模型**：agent 子網要能經 hub、ExpressRoute 或 VPN 路由到地端；peering 不可重疊、只能用 RFC 1918；DNS 與防火牆要放行。金鑰放 Key Vault 或 connection，不放環境變數。模型必須支援 tool calling。

**文件沒寫、要實測的**：委派子網上的 UDR 會不會套用到 micro-VM 的流量（能否強制走企業防火牆）；agent 身分能不能取得自訂 audience 的 token，以及 token 的 `appid`／`azp` 是哪個 ID。2026-10-29 dev 實測：路由表會套用（黑洞路由），自訂 audience 的 token 可取得且 `appid` 等於 `oid`，見[實測結果](#實測結果)。

## FA 目前的狀況

FA 的模型網址寫死為 `{FOUNDRY_PROJECT_ENDPOINT}/openai/v1`，用 agent 受控身分取 `https://ai.azure.com` 的 token；已支援 Responses 與 Chat Completions 兩種格式。要接企業 gateway，FA 需要讓模型網址、驗證方式（受控身分加自訂 audience，或從 Key Vault 取金鑰）與模型名稱可以設定，並確認串流回應帶 usage，否則用量入帳缺資料。這是產品變更，等實測確定驗證方式後另提設計，本文不改 FA 程式。

## dev 實測設計

### 架構

- **VNet A `10.251.0.0/16`**：官方 foundry-samples template 15（commit `ce52cffdbd91716723b06eb1bce39f964a23ed45`），部署 gpt-5-mini GlobalStandard 容量 10，參數見 [template15.bicepparam](../infra/model-gateway-probe/template15.bicepparam)。範本沒有 SKU 參數，clone 後要手動改 `modules-network-secured/standard-dependent-resources.bicep` 兩處，費用表以改過的版本計算：Cosmos DB 帳號的 `properties` 加 `capabilities: [{ name: 'EnableServerless' }]`；AI Search 的 `sku.name` 由 `standard` 改為 `basic`。
- **APIM 子網 `10.251.2.0/27`**：APIM Developer，internal 模式注入；子網不委派、要掛 NSG。Internal 模式自 2024-05 起不需要公開 IP。
- **VNet B `10.252.0.0/16`，模擬地端**：與 VNet A 互相 peering；一台 D2ls_v7 Ubuntu（參數 `vmSize`；2026-10-29 westus3 的 B1s、B2ats_v2、B2als_v2、D2als_v6、D2as_v5、D2s_v5 都回 `SkuNotAvailable`，D2ls_v7 才成功；preflight `validate` 偵測不到容量不足）、沒有公開 IP，跑 [mock_openai.py](../scripts/model-gateway-probe/mock_openai.py)（只用標準函式庫），記錄每個請求的來源 IP，回傳串流、tool call 與 usage。
- **私有 DNS**：`<apim 名稱>.azure-api.net`（只建這個 FQDN 的 zone，**不建** `azure-api.net` 整個 zone，官方明文禁止）連到 VNet A 與 B；`llm.onprem-sim.test` 指向 VM，連到 VNet A。
- **路由表** `rt-agent-blackhole`（`10.252.0.0/16` → None）先建好不掛，T6 才用 CLI 掛到 agent 子網；不在 Bicep 重新宣告 template 15 的子網，以免蓋掉委派與 NSG。
- **探針 agent**：Responses protocol，輸入是測試編號或 `ALL`；測試目標寫在環境變數 `PROBE_TARGETS`，不接受請求裡的任意網址。每項回報狀態碼、延遲、DNS 解析、micro-VM 的來源 IP、tool call 與 usage 是否出現、容器有沒有設 proxy 環境變數（只記名稱，不記值；有設就表示量到的可能是 proxy 路徑）；受控身分的項目另外回報 token 的 `aud`、`appid`、`azp`、`oid`、`tid`、`exp`，不回傳 token 本身。

部署與設定：[addon.bicep](../infra/model-gateway-probe/addon.bicep)；探針與腳本：[scripts/model-gateway-probe](../scripts/model-gateway-probe/)。

### Bicep 與 AVM

addon.bicep 全部用 Azure Verified Modules，版本固定；repo 已用到的模組沿用同一版：

| 用途 | 模組與版本 |
|---|---|
| APIM 子網與 VNet B 的 NSG | `avm/res/network/network-security-group:0.5.3` |
| 在 template 15 的 VNet A 加 APIM 子網（不重新宣告整個 VNet） | `avm/res/network/virtual-network/subnet:0.2.0` |
| APIM、API、兩個 operation、rawxml 政策 | `avm/res/api-management/service:0.15.0` |
| APIM 受控身分對 Foundry 帳號的角色 | `avm/ptn/authorization/resource-role-assignment:0.1.2` |
| VNet B 與 B→A peering | `avm/res/network/virtual-network:0.10.2` |
| A→B peering（等 APIM 建完） | `avm/res/network/virtual-network/virtual-network-peering:0.2.0` |
| 模擬地端 VM 與選用的 NVA | `avm/res/compute/virtual-machine:0.22.3` |
| APIM 與 `onprem-sim.test` 的私有 DNS zone | `avm/res/network/private-dns-zone:0.8.1` |
| 黑洞路由表 | `avm/res/network/route-table:0.5.0` |

所有模組都設 `enableTelemetry: false`，沒有保留 native resource。與先前 native 版本的差異（以 `bicep snapshot` 比對）：

- 每個模組是一個巢狀部署（`gwprobe-*`），模組內部還會再拆出 API、operation、政策、子網、peering、DNS 記錄與連結的巢狀部署。不加 lock 或 tag。
- APIM 模組多送了加密套件強化（停用 8 個弱 cipher）、`minApiVersion 2021-08-01`、關閉 developer portal；`publicNetworkAccess` 明確送 `Enabled`（internal 模式仍只在 VNet 內可達）。
- APIM 模組沒有 gateway URL 與私有 IP 的 output，所以用一個 `existing` 讀回來填 DNS 記錄與 output，不建立任何資源。
- VM 模組的預設值另外設定：關閉 accelerated networking（B1s 不支援，換大小後維持關閉）、不指定可用區、OS 磁碟 `ReadWrite` 快取、磁碟與 NIC 隨 VM 刪除；不加 extension、開機診斷、自動關機或備份。OS 磁碟名稱改為 `<vm>-disk-os-01`。
- VNet B 的子網改成獨立的子資源；VNet 的 PUT 不帶 `subnets`。重新部署時是否保留已掛 NIC 的子網未在 Azure 實測，所以步驟 3 改用 `az rest` 更新政策，不重新部署。

### 測試項目

| # | 測什麼 | 從哪裡發 | 預期 |
|---|---|---|---|
| T0 | 直接呼叫 Foundry project endpoint 的模型（基準）；同時取得 agent 身分 token 的 `appid`／`azp`，填進 APIM 政策 | 探針 agent | 200 |
| T1 | 經 APIM 呼叫 Foundry，Chat Completions，agent 身分 token，audience `https://cognitiveservices.azure.com`。回 401 時先比對結果的 `token_claims`（`aud`、`appid`／`azp`）與政策的值，並看 `body_snippet` 是 APIM 的 `Unauthorized` 還是 Foundry 的錯誤（例如 RBAC 尚未生效），再下結論 | 探針 agent | 200 |
| T2 | 同 T1，改用 Responses API（FA 用的格式） | 探針 agent | 200 |
| T3 | 反向：不帶 token；同 audience 但不同身分（VM 自己的受控身分）。APIM 只在 VNet 內可達，所以在模擬地端 VM 上執行：`az vm run-command invoke -g <rg> -n vm-llm-mock --command-id RunShellScript --scripts "python3 /opt/probe/probe.py --targets /opt/probe/targets.t3.json --imds"`（targets 檔由 addon 的 cloud-init 寫好） | 模擬地端 VM | 兩者都 401 |
| T5 | 呼叫模擬地端模型，先用 IP 再用 DNS 名稱，帶 API key，串流加 tool call | 探針 agent | 200，mock 記錄的來源 IP 落在 `10.251.0.0/24` |
| T6 | agent 子網掛上 `rt-agent-blackhole` 後，現有 session 與新 session 各測一次；拿掉後再測。「現有 session」要帶上次回應的 `agent_session_id`，並在上次呼叫後 600 秒（idle timeout）內測完，否則底下可能已換成新的 micro-VM；兩次結果都要比對輸出的 `sent_agent_session_id` 與 `agent_session_id`。順序固定：先測現有 session，再用另一個 `--state` 檔加 `--new-session` 測新 session（state 檔每次呼叫都會覆寫） | 探針 agent | 掛上時失敗、拿掉後恢復即證明 UDR 有效；若委派子網不接受路由表，這本身就是結論 |
| T7（選做） | UDR 指向 NVA VM（開 IP 轉送）並抓封包；只有 T6 證明 UDR 有效才建 NVA | 探針 agent | 流量可被導向企業防火牆或 NVA |

APIM 的 token 上限政策是 APIM 本身的功能，與呼叫端是不是 hosted agent 無關，不列入測試。

### 步驟

1. **本機準備（不花錢）**：探針、mock、Bicep 與參數檔；離線測試與 `az bicep build`。
2. **部署（需批准，約 50 分鐘）**：template 15（約 25 分鐘）與 addon.bicep（APIM 30～50 分鐘）。
3. **設定（約 30 分鐘）**：打開 Foundry IP 通道；建置並推送探針映像；建立 agent；跑 T0 取得 `appid`，**不重新部署 addon**，直接更新 APIM 政策：`az rest --method put --uri "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<apimName>/apis/foundry-openai-v1/policies/policy?api-version=2024-05-01" --body @policy.json`，`policy.json` 為 `{"properties": {"format": "rawxml", "value": "<policies>…</policies>"}}`（先用 `az rest --method get` 讀同一個 URI 並加 `&format=rawxml`，只改 `application-id` 後存成 policy.json，不要手抄 XML）。重新部署會碰到 VM customData 不能改、VNet B 子網保留未實測這兩個風險，所以不用。VM 上的 `/opt/probe/probe.py` 是部署當時的版本；若之後改的部分會影響 T3，用 run-command 把新檔寫進 VM。
4. **測試（約 1 小時）**：T0～T7，輸出與 `invoke.py` 的 `--state` 檔都放在不進 Git 的 scratchpad（不要在 repo 目錄執行），只把去敏摘要寫回本文。
5. **拆除（另外詢問，約 1 小時）**：agent（force）→ project → 帳號 → purge；刪 APIM 並 purge 軟刪除的 APIM；最後刪 RG。

### Foundry IP 通道

Foundry 帳號的公開存取關閉。從建立 agent 一路開到 T7 跑完，只開放操作者的單一 IP，測完立刻關，並把開著的期間寫進結果。

ACR 也對同一個 IP 開放：template 15 設了 `developerIpCidr` 時，ACR 從部署起就允許這個 IP（仍要 Entra 驗證），直到刪 RG；推完映像後可以先關。

### 費用（westus3 牌價，USD）

以 2026-10-28 第二次探針 `rg-fa-stdpriv2-1028` 的實際帳單校正：約 3.2 小時共 0.78（Search Basic 0.404＝4 個起算小時 × 0.101；ACR 0.18 按日攤；VNet／PE 0.18；其他不到 0.02），約每小時 0.25。帳單資料可能仍在入帳。

| 項目 | 單價 | 這次估計 |
|---|---|---|
| Template 15 基礎資源 | 約 0.25／小時 × 6 小時 | 約 1.5 |
| APIM Developer | 0.0658／小時 | 約 0.4 |
| VM D2ls_v7（NVA 只在 T7 才建）＋磁碟 | 0.117／小時／台（westus3 Linux 牌價，不含磁碟） | 約 0.7 |
| Hosted agent，0.5 vCPU／1 GiB | 0.0994／vCPU‑小時＋0.0118／GiB‑小時（約 0.06／小時），只算 session 活著的時間 | 約 0.2 |
| 模型 token、peering、DNS | — | 不到 0.1 |
| **合計（6 小時內拆完）** | | **約 3** |

忘了拆、放一整天約 USD 12。dev 訂閱 westus3 的 gpt-5-mini GlobalStandard 配額 2026-10-29 讀回為 220/1000。

### 批准點

1. 在 dev 訂閱建立上述資源。
2. IAM 寫入：APIM 受控身分取得 `Cognitive Services OpenAI User`，範圍只到測試用的 Foundry 帳號；template 15 本身也替 project MI 指派 Storage、Cosmos、Search 的角色。刪 RG 時一起移除。不寫入 Entra tenant（不建 app 註冊）。
3. 打開 Foundry IP 通道，以及 ACR 對同一 IP 的開放（期間見上）。
4. 拆除，另外詢問。
5. 實測當天另經 Neal 批准：操作者身分在測試 project 取得 Foundry User（建立 agent 需要），刪 RG 時一起移除。

### 已查的部署前提與風險

- MCAPS 政策（2026-10-29 唯讀讀回）：deny initiative 只有「Block Azure OpenAI Provisioned Capacity」可能相關，本測試用 GlobalStandard 不受影響；modify 政策會關閉 Cognitive Services 的本機驗證，所以 APIM 只能用受控身分呼叫 Foundry，沒有金鑰備案。
- 若 APIM Developer 部署失敗，備案是 Standard v2（入站 PE、出站 VNet 整合），每小時 0.96，總額約多 USD 5。
- 委派子網不一定能掛路由表；若不行，T6、T7 直接得到「不行」的結論。

### 測不到的

- 真正的 ExpressRoute 與地端防火牆：peering 只模擬私網路由。
- 客戶實際 gateway 產品的行為與真實地端模型的 tool calling 品質（mock 是自製的）。
- FA 本身的程式路徑：要先改程式才能測。

## 官方來源（2026-10-29 查閱）

- [Bring your own model to Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/ai-gateway)（2026-10-25 更新）
- [Hosted agents in Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)（2026-10-19）
- [Deep dive into Foundry Agent Service networking](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agents-networking-deep-dive)（2026-09-08）
- [Deploy Azure API Management instance to internal VNet](https://learn.microsoft.com/en-us/azure/api-management/api-management-using-with-internal-vnet)（2026-08-24）
- [Configure AI Gateway in your Foundry resources](https://learn.microsoft.com/en-us/azure/foundry/configuration/enable-ai-api-management-gateway-portal)（2026-08-06）：文件沒說它會管到 hosted agent 的模型呼叫

## Verified against

- 程式基準：設計與程式寫於 `origin/main` `cf8ece21`；dev 實測用的是 `19704fd4`（AVM 版 addon、D2ls_v7）。
- 已讀程式：`src/fa-shared-context-poc/shared_context/native_sdk.py` 的 `native_runtime_from_environment`（模型網址與 token）與 sender 選擇（`openai-responses` 走 `/responses`，其他走 `/chat/completions`）。
- 實際驗證：探針與 mock 的離線測試見 `tests/infra/test_model_gateway_probe.py`；T0～T3、T5、T6 已於 2026-10-29 在 dev 執行（見[實測結果](#實測結果)），T7 未執行。
- 證據與限制：費用與政策讀回來自 dev 訂閱的唯讀查詢；官方能力只引用上列文件，不代替實測。

## 實測結果

2026-10-29（UTC）在 dev 訂閱 westus3 的暫時 RG 執行，main `19704fd4`（addon 為 AVM 版，VM 用 D2ls_v7）。原始輸出只留在不進 Git 的 scratchpad；以下是去敏摘要。

| # | 結果 | 觀察 |
|---|---|---|
| T0 | **401** | Foundry 名稱解析到 private endpoint（`10.251.*`），網路通；但 agent 身分沒有 `.../OpenAI/deployments/chat/completions/action`。本次沒有替 agent 身分加角色（不在批准範圍）。[Hosted agents 文件](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)寫「The agent identity can access model inferencing through the project endpoint … by default」，這個 project 實際上不行；FA 本來就替 Agent ID 指派 Foundry User 與 OpenAI User |
| T1 | **200** | 經 APIM（私有 IP）呼叫 Foundry，Chat Completions 串流，有 tool call 與 usage。agent 身分可以取得 `https://cognitiveservices.azure.com` audience 的 token，`appid` 等於它的 `oid`，`azp` 為空 |
| T2 | **200** | 同 T1，Responses API 串流，有 tool call 與 usage |
| T3 | **401／401** | 從模擬地端 VM 打 APIM：VM 自己的受控身分（同 audience、不同 `appid`）與不帶 token 都被 APIM 擋下 |
| T5 | **200** | 以 IP 與 DNS 名稱（`llm.onprem-sim.test`，私有 DNS）呼叫模擬地端模型都成功。mock 記錄的來源 IP 是 agent 子網內的 `10.251.0.58`；容器內看到的本機 IP 則是 `169.254.0.2`，推測 micro-VM 內有一層位址轉換；對外的來源仍是委派子網的位址 |
| T6 | **UDR 有效** | 委派子網接受路由表。掛上 `10.252.0.0/16 → None` 後，現有 session 與新 session 呼叫模擬地端都逾時（30 秒）；同時 APIM（不在黑洞範圍）仍回 200。拿掉後兩個 session 都恢復 200；每次回應的 `agent_session_id` 與送出的一致；現有 session 在上一次呼叫（04:47:08）後約 2 分鐘測試，在 600 秒 idle timeout 內 |
| T7 | 未執行 | 只驗證了黑洞路由（下一跳 None）。下一跳改指 NVA／防火牆時流量能否轉過去、回程是否正常，本次沒有測 |

其他觀察：

- 容器內沒有 proxy 環境變數。
- westus3 的 B1s、B2ats_v2、B2als_v2、D2als_v6、D2as_v5、D2s_v5 都回 `SkuNotAvailable`，D2ls_v7 才建成；preflight `validate` 偵測不到。
- 第一次建立 agent 回 403（錯誤訊息：操作者身分沒有 `Microsoft.CognitiveServices/accounts/AIServices/agents/write`；經 Neal 批准在 project 指派 Foundry User 後重試），第二次回 408，讀回確認未建立後重送成功。
- Foundry IP 通道開放約 92 分鐘（03:19～04:51），只限操作者單一 IP；ACR 在推完映像後於 04:52 關閉公開存取。

**對客戶問題的結論**：hosted agent 可以在私網內經企業 APIM 呼叫 Foundry 模型，這條路徑全程用 Entra 身分、不用金鑰，Chat Completions 與 Responses 都以串流回傳 tool call 與 usage；也能經 peering 呼叫地端 OpenAI 相容模型（這次用 API key）。委派子網上的路由表會套用到 micro-VM 的流量（黑洞路由已驗證，現有與新 session 都生效），這是用 UDR 把流量導向企業防火牆的前提；實際導向 NVA／防火牆與回程沒有測（T7 略過）。真正的 ExpressRoute／地端防火牆與客戶的 gateway 產品仍未測。
