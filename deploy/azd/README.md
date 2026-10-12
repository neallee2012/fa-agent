# 用 `azd up` 部署 FA 到自己的訂閱

設計與取捨見 [docs/azd-new-environment.md](../../docs/azd-new-environment.md)。本頁只寫怎麼跑。
這是新環境的**唯一**標準流程；[deploy/README.md 的 17 步](../README.md#new-environment-standard-flow)
保留為逐步排錯對照，兩邊呼叫同一批腳本。

> 狀態（2026-10-11）：`azd up -e fazd1011`（Neal 的訂閱，westus3）從頭一次跑完，Neal 在 Portal 實際使用回報通過。
> 區域與模型選擇（下方「區域與模型」）在 `azd up -e fazd1012`（eastus2，只選 gpt-6-luna）實跑到 `SUCCESS`；
> 部署與設定讀回符合選擇，**Portal 尚未驗收**；該環境已依 Neal 10-11 決定刪除（[紀錄](../../docs/progress.md#azd-eastus2-1011)）。

<a id="prerequisites"></a>
## 事前準備（不是 Entra 管理員的人請先看）

先跑唯讀檢查，它會列出你已具備什麼、還缺什麼、該找誰：

```bash
azd env new <name> --subscription <subscription-id> --location <region>
python deploy/azd/fa_azd.py prereq
```

不知道訂閱 ID 或租用戶 ID 時，`az login` 後執行 `az account list --query "[].{name:name, id:id, tenant:tenantId}" -o table`。訂閱在 `azd env new --subscription` 時就固定在該 azd 環境，不使用 az CLI 的預設訂閱；hook 會檢查 az 與 azd 登入的是同一個租用戶，不同就停止。

（這裡先指定區域，是因為 `prereq` 不經過 azd 的區域選單。）

`azd up` 的 preprovision 也會跑同一份檢查；標 `blocked` 的項目會在**任何計費資源建立前**停下。

**A. 你自己（部署的人）**

| 項目 | 為什麼 | 怎麼確認 |
|---|---|---|
| azd ≥ 1.32.0、Azure CLI、Python ≥ 3.11、git、Docker（引擎要在跑）；Windows 另需 PowerShell 7 | hook 用 Python；`azd deploy` 在本機 build 映像 | `prereq` |
| `az login --tenant <tenant>` 與 `azd auth login --tenant-id <tenant>` 用同一個**使用者**帳號 | Entra 物件與首次安裝批准都以這個人為準；service principal 登入不支援 | `prereq` |
| 你就是 Team 主人（建議），或 `azd env set FA_TEAM_OWNER_OID <主人 object ID>` | 首次安裝批准與第一次 session 準備都要主人本人登入執行。主人不是你時，主人接手跑 `azd hooks run postdeploy`，所以主人也要有 B 的訂閱權限，以及 project 的 Foundry User／OpenAI User（Bicep 只自動給部署者） | |

**B. 訂閱 Owner（不是你的話請他先給權限或代做）**

| 項目 | 為什麼 |
|---|---|
| 你在訂閱上有 **Owner**，或 **Contributor＋User Access Administrator** | Bicep 會建角色指派，還會在訂閱層建一個自訂備份角色（需要 `roleDefinitions/write`，RBAC Administrator 不夠） |
| 已註冊 feature `Microsoft.Network/AllowBringYourOwnPublicIpAddress` 與 9 個 resource provider（[清單](../README.md#new-environment-provider-precheck)） | 少了 feature，Container Apps 環境會「成功」但沒有平台網路（B1）；preprovision 會唯讀檢查並列出指令 |
| 區域有你要的模型與 quota（azd 選區域、hook 選模型時都會顯示；可用 `FA_MODEL_CAPACITY_JSON` 調小容量） | 同訂閱同區域的帳號共用 quota |
| 區域支援 Foundry Hosted Agents（azd 的區域選單只列這些）與 Container Apps workload profiles | |

**C. Entra 管理員（一次性；你若是管理員可跳過）**

| 項目 | 需要的角色 | 什麼時候 |
|---|---|---|
| 允許你註冊應用程式：租戶預設允許；若關閉，給你 **Application Developer** | Privileged Role Administrator 指派 | `azd up` 前（否則 preprovision 停下） |
| 租戶裡要有 Azure CLI 的 service principal（大多已存在）：`az ad sp create --id 04b07795-8ddb-461a-bbee-02f9e1bf7b46` | Cloud Application Administrator 以上 | `azd up` 前；`prereq` 會顯示是否缺 |
| 替 **Team 主人一人** 建 3 筆 delegated grant（Azure CLI→FA API、Portal→FA API、Portal→登入範圍） | Cloud Application Administrator、Application Administrator 或 Global Administrator | `azd provision` 之後。hook 會把 3 個請求 body 與指令寫到 `.azure/<name>/fa/admin-consent/`（只有 object ID，沒有秘密），交給管理員照 `README.txt` 執行即可，不需要 repo 或 azd |

grant 只給主人一人（`consentType=Principal`），不是全租戶同意；FA API 也只接受該主人。
管理員還沒做 C 的第 3 項時，`azd up` 仍會把資源、Agent、API 與 Portal 部署完，停在第一次 session 準備前；
管理員做完後，你再跑 `azd hooks run postdeploy` 接續。

## 第一次部署

```bash
export AZURE_CONFIG_DIR="$HOME/.azure-fa"   # 每個 shell 都設；PowerShell 用 $env:AZURE_CONFIG_DIR = "$HOME\.azure-fa"
az login --tenant <tenant-id>
azd auth login --tenant-id <tenant-id>
azd env new <name> --subscription <subscription-id>
azd up
```

訂閱與租用戶 ID 的查法見上方[「事前準備」](#prerequisites)；訂閱固定在這個 azd 環境，不使用 az CLI 的預設訂閱。

- `<name>`：2–20 個小寫英數字或 `-`，是資源名稱前綴，RG 預設 `rg-<name>`。
- 區域與模型：見下一節。已知區域時也可以 `azd env new … --location <region>` 跳過選單。
- 沒設 `FA_TEAM_OWNER_OID` 時部署的人就是 Team 主人，終端機會要你輸入 `y` 確認。主人不是自己時，先
  `azd env set FA_TEAM_OWNER_OID <object-id>`（查法：`az ad user show --id <主人 UPN> --query id -o tsv`，主人自己登入時 `az ad signed-in-user show --query id -o tsv` 也行）；首次安裝的批准與 session 準備都要由主人本人登入執行。
- 中途會停下來要你做一件事：**首次安裝批准**。終端機會顯示 plan hash，主人輸入前 12 碼才寫入
  Table；輸入其他內容就停，什麼都不寫。
<a id="region-and-models"></a>
### 區域與模型

不是每個訂閱都能用同一個區域或模型，所以兩者都在 `azd up` 開頭選：

1. **區域**：azd 自己問。選單只列 Foundry Hosted Agents 支援的區域，並隱藏沒有 embedding quota 的區域。
2. **模型**：preprovision 列出 catalog 的 chat 模型在這個區域有沒有提供、quota 剩多少，建議「有提供且夠」的那些；
   輸入編號（例 `2,4`），再選新對話預設模型。選了這個區域沒有的模型就停下，什麼都不改。

示意（數字依訂閱與區域而定）：

```text
FA: catalog chat models in eastus2 (GlobalStandard; ...)
  1. gpt-4.1-mini: quota free 1000, needs 500
  2. gpt-5.4-mini: quota free 1000, needs 100
  3. gpt-6-luna: quota free 100, needs 500 (not enough)
  4. gpt-6.1-sol: not offered in this region
Models to deploy (numbers separated by commas) [1,2]:
```

- 選好的值存成 `FA_MODELS`／`FA_DEFAULT_MODEL`，之後的 `azd up` 不再問；Portal 只列這些模型。
- 沒有終端機（程式以 `sys.stdin.isatty()` 判斷，不看 azd 的 `--no-prompt`，例如 CI）時預設 catalog 全部模型；要指定就先 `azd env set FA_MODELS gpt-5.4-mini,gpt-6-luna`。
- azd 1.32 以上偵測到 CI 或 AI agent 環境（環境變數如 `CLAUDE_CODE_ENTRYPOINT`、`AI_AGENT`）時，會自動開啟 no-prompt 模式，
  略過 azd 自己的區域選單並以「missing required inputs」失敗；要照常互動就設 `AZD_NON_INTERACTIVE=false`
  （來源：azd 文件 `cli/azd/docs/environment-variables.md`，tag azure-dev-cli_1.32.0，2026-10-11 查證）。
- 記憶庫用你選的預設模型，另外只會多部署 `text-embedding-3-small`（記憶需要）。沒有任何模型是固定必裝的。
- 之後要加模型：`azd env set FA_MODELS …` 再 `azd up`。拿掉的模型只會從 Portal 消失，部署本身不刪；
  記憶庫建立時用的模型要留在清單裡。

- 選用設定（`azd env set`，在 `azd up` 前）：

| 名稱 | 預設 | 說明 |
|---|---|---|
| `FA_MODELS` | 終端機：依區域選；否則 catalog 全部 | 要部署、Portal 要列的 chat 模型，逗號分隔 |
| `FA_DEFAULT_MODEL` | `gpt-6.1-sol`（有選時），否則清單第一個 | 新對話預設模型；第一次建記憶庫也用它 |
| `FA_MODEL_CAPACITY_JSON` | catalog 容量 | 例 `{"gpt-6-luna":50,"gpt-6.1-sol":50}` |
| `FA_MODEL_VERSION_UPGRADE_OPTION` | `NoAutoUpgrade` | |
| `FA_ENABLE_WEBIQ` | `false` | `true`：connection 還不存在時，deploy 以隱藏輸入詢問 WebIQ 金鑰（需終端機）並建 connection 與 toolbox `fa-org-tools`；已存在就只讀回（要換過期的金鑰時，在終端機執行 `python deploy/stage/new_webiq_connection.py --config .azure/<env>/fa/profile.json --rotate`；連線必須已存在，不存在就停下且不寫入。要用 azd 環境的 profile，不加 `--config` 會用 `FA_DEPLOY_CONFIG` 或 `deploy/stage/stage.json`（也就是 stage）。腳本需要 `httpx` 和 `azure-identity`：用 deploy 建好的 `.azure/<env>/fa/venv`（Windows 是 `Scripts\python.exe`，其他是 `bin/python`），或在自己的 venv 執行 `pip install -r src/fa-shared-context-poc/requirements.txt`；見 [deploy/README.md](../README.md#new-environment-standard-flow) 第 16 關） |
| `FA_ENABLE_AGENT_ONBOARDING` | `false` | 「＋」新 Agent 自動補角色（API 身分取得受限 RBAC Admin） |
| `FA_ENABLE_MEMORY` | `true` | 私人記憶與 Team 記憶：deploy 建 Memory Store `poc-team-memory`（`FA_DEFAULT_MODEL`＋text-embedding-3-small，preview，依用量計費）並開旗標；同 stage（#306 例外）。要關請在第一次 `azd up` 前設 `false`：旗標隨 Agent 第一版發布 |
| `FA_ENABLE_TEAM_MCP_CONNECTIONS` | `true` | 給 API 身分 project 範圍的 Contributor，Team MCP 設定頁才能新增含金鑰的 connection（stage 同） |
| `FA_TIMEZONE` | `UTC` | Hosted 的 `TZ` |
| `FA_HOSTED_IDLE_TIMEOUT_SECONDS` | `3600` | 120–3600 |
| `FA_API_SUBNET_NAT_EGRESS` | `false` | 同 `prepare_environment.py` |

## 跑完就能用

首次安裝最後一步，hook 以 Team 主人身分替兩位助理做「新對話選版」（Neal 2026-10-10 決定）：
Responses 通道、伺服器提供的全部能力（桌面、技能、Shell、瀏覽器、交辦、協作、圖片等，WebIQ-MCP 除外），
以及 Team 已批准的技能（最多 4 個）。模板由 `select_all_capabilities.py` 依伺服器自己的選項算出，不從別的 Agent 複製；
之後的 `azd deploy` 不會改它，主人在 Portal 助理 ⚙ 設定頁的調整都保留。

剩下一件只有真人能做的事：主人登入 hook 印出的網址、開新對話、送一則短訊息；以 receipt
`completed`、`failure_code=null` 為準。

## 中斷後接續

- 每個寫入都有 intent 檔（`.azure/<name>/fa/`，不進 Git）。停下來時先照訊息讀回，再
  `azd hooks run postprovision` 或 `azd hooks run postdeploy` 接續；不要刪 intent 重來。
- 首次安裝的批准檔有 10 分鐘窗口；過期但 apply 還沒開始時，hook 會停下，請自己把那份檔案移到旁邊保存，
  再重跑一次批准。
- `azd provision --preview` 可以先看 what-if（重跑既有環境前建議先看，B5）。
- 首次安裝時 Agent 不做 follow（從沒準備過的 Agent 沒有舊環境可跟）。若中斷後**改了程式**才接續，已準備好的 Agent 會停在舊版，直到下一次 `azd deploy` 的 follow 把它換上新版；資料不受影響。
- WebIQ 腳本（`deploy/stage/new_webiq_connection.py`）不自己設登入目錄，執行前在同一個 shell 設好 `AZURE_CONFIG_DIR`。

## 之後更新

再跑 `azd deploy`：重 build 映像，然後執行和 stage CI 相同的步驟
（`ci_deploy.py check` → WebIQ（有開才做；連線已存在就只讀回，不需終端機）→ `guard → sync → api → console → follow`），只改映像與 profile 宣告的 ENV；
已安裝的 runtime 設定不會被改。

## 刪除

```bash
azd down --purge
```

- 刪掉整個 RG，**包含 Table、Blob 備份、Memory Store 與 Foundry project**，無法復原。
- `predown` hook 先移除 FA 給 M1 備份帳號加的刪除鎖（`m1-backup-account-delete-protection`），要先輸入環境名稱確認；
  沒有終端機時用 `FA_CONFIRM_DELETE=<環境名稱>`。資源群組裡有別人加的鎖就停下，不替你移除。
- `--purge` 同時清除 soft-delete 的 Foundry account：不清的話它仍占模型 quota（同訂閱同區域共用），
  同名環境也無法重建。
- `postdown` hook 接著刪 RG 外、這個環境自己建的東西：Entra API／Portal app（30 天內可還原）與訂閱層自訂備份角色；
  本機 `.azure/<name>/fa` 改名為 `fa-down-<時間>` 保留紀錄，hook 寫入的值清掉。同名環境能否馬上從頭再裝**尚未驗證**：
  soft-delete 的 Entra app 在 30 天內仍保有 identifierUris／uniqueName，可能與重建衝突；要換新環境名稱最保險。
