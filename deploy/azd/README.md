# 用 `azd up` 部署 FA 到自己的訂閱

設計與取捨見 [docs/azd-new-environment.md](../../docs/azd-new-environment.md)。本頁只寫怎麼跑。
這是新環境的**唯一**標準流程；[deploy/README.md 的 17 步](../README.md#new-environment-standard-flow)
保留為逐步排錯對照，兩邊呼叫同一批腳本。

> 狀態（2026-10-11）：作者在自己的訂閱（westus3）從頭跑完一次 `azd up`，並在工作台實際使用過。
> 區域與模型選擇（下方「區域與模型」）也在 eastus2（只選 gpt-6-luna）實跑到 `SUCCESS`，
> 部署與設定讀回符合選擇，**那次的工作台驗收還沒做**。
> 2026-10-21 又用公開 repo 第 34 版在 westus3 從頭跑一次，前 10 分鐘驗收通過（[紀錄](../../docs/progress.md#azd-e2e-public-1021)）。

<a id="prerequisites"></a>
## 事前準備（不是 Entra 管理員的人請先看）

先跑唯讀檢查，它會列出你已具備什麼、還缺什麼、該找誰（本頁的指令都在 repo 根目錄執行）。
**步驟順序只有一份**，在下方[「第一次部署」](#first-deploy)的指令區塊：先照它 clone 到 repo 根目錄、設好
`AZURE_CONFIG_DIR`、完成 `az login` 與 `azd auth login`（`prereq` 要用同一個 `AZURE_CONFIG_DIR` 和登入），再跑：

```bash
azd env new <name> --subscription <subscription-id> --location <region>
python deploy/azd/fa_azd.py prereq
```

不知道訂閱 ID 或租用戶 ID 時，`az login` 後執行 `az account list --query "[].{name:name, id:id, tenant:tenantId}" -o table`。訂閱在 `azd env new --subscription` 時就固定在該 azd 環境，不使用 az CLI 的預設訂閱；hook 會檢查 az 與 azd 登入的是同一個租用戶，不同就停止。

兩條路擇一，檢查內容相同：

- **先檢查再部署（上面）**：`prereq` 單獨執行，只需要已選好的 azd 環境和訂閱 ID，不讀區域，所以 `azd env new`
  有沒有 `--location` 都能跑。帶了 `--location`，區域就存在環境裡，之後的 `azd up` 不再出區域選單；沒帶，`azd up` 才出選單。
- **直接部署（下方[「第一次部署」](#first-deploy)）**：不另外跑 `prereq` 指令，直接 `azd up`；沒帶 `--location` 時會出區域選單，
  它的 preprovision 會跑同一份檢查。

標 `blocked` 的項目會在**任何計費資源建立前**停下。

**A. 你自己（部署的人）**

| 項目 | 為什麼 | 怎麼確認 |
|---|---|---|
| azd ≥ 1.32.0、Azure CLI、Python ≥ 3.11、git、Docker（引擎要在跑）；Windows 另需 PowerShell 7 | hook 用 Python；`azd deploy` 在本機 build 映像 | `prereq` |
| `az login --tenant <tenant>` 與 `azd auth login --tenant-id <tenant>` 用同一個**使用者**帳號 | Entra 物件與首次安裝批准都以這個人為準；service principal 登入不支援 | `prereq` |
| 你就是 Team 主人（建議），或 `azd env set FA_TEAM_OWNER_OID <主人 object ID>` | 首次安裝批准與第一次 session 準備都要主人本人登入執行。主人不是你時，主人接手跑 `azd hooks run postdeploy`，所以主人也要有 B 的訂閱權限，以及 project 的 Foundry User／OpenAI User（Bicep 只自動給部署者） | |

**B. 訂閱 Owner（不是你的話請他先給權限或代做）**

| 項目 | 為什麼 |
|---|---|
| 你在訂閱上有 **Owner**，或 **Contributor＋User Access Administrator** | Bicep 會建角色指派（包括給 API 身分的 Contributor 與受條件限制的 RBAC Administrator，兩者都是特權管理角色；你的 Owner／User Access Administrator 指派若帶有排除特權角色的條件，provision 預期會失敗（推論，未實測）），還會在訂閱層建一個自訂備份角色（需要 `roleDefinitions/write`，RBAC Administrator 不夠） |
| 已註冊 feature `Microsoft.Network/AllowBringYourOwnPublicIpAddress` 與 9 個 resource provider（[清單](#register-providers)） | 少了 feature，Container Apps 環境會「成功」但沒有平台網路；preprovision 只唯讀檢查，沒過就停下，**不會替你註冊**（註冊會改訂閱）；名稱與指令見下方表後的區塊 |
| 區域有你要的模型與 quota（azd 選區域、hook 選模型時都會顯示；可用 `FA_MODEL_CAPACITY_JSON` 調小容量；`azd up` 前怎麼先查見[「區域與模型」](#region-and-models)） | 同訂閱同區域的帳號共用 quota |
| 區域支援 Foundry Hosted Agents（azd 的區域選單只列這些）與 Container Apps workload profiles | |

<a id="register-providers"></a>
要註冊的 9 個 resource provider：`Microsoft.App`、`Microsoft.CognitiveServices`、`Microsoft.ContainerRegistry`、`Microsoft.Insights`、
`Microsoft.ManagedIdentity`、`Microsoft.Network`、`Microsoft.OperationalInsights`、`Microsoft.SignalRService`、`Microsoft.Storage`
（`Microsoft.AlertsManagement` 沒註冊只警告，FA 不需要）。查狀態與註冊的 az 指令（由有權限的人（Owner 或 Contributor）執行；`<provider>` 逐一換成上面的名稱）：

```bash
az provider show --namespace <provider> --subscription <subscription-id> --query registrationState -o tsv
az provider register --namespace <provider> --subscription <subscription-id>
# 一次註冊 9 個：
for p in Microsoft.App Microsoft.CognitiveServices Microsoft.ContainerRegistry Microsoft.Insights Microsoft.ManagedIdentity Microsoft.Network Microsoft.OperationalInsights Microsoft.SignalRService Microsoft.Storage; do az provider register --namespace $p --subscription <subscription-id>; done
az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --subscription <subscription-id>
az feature show --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --subscription <subscription-id> --query properties.state -o tsv
```

feature 的狀態變成 `Registered` 後，再執行一次 `az provider register --namespace Microsoft.Network` 讓它生效，然後才跑 `azd up`（或重跑 `prereq`）。
註冊是非同步的，狀態可能要等幾分鐘；若 feature 長時間停在 `Pending`，要開 Azure support request。新開終端機要先設同一個 `AZURE_CONFIG_DIR`。逐項的用途與已知問題在 [deploy/README.md](../README.md#new-environment-provider-precheck)。

**C. Entra 管理員（一次性；你若是管理員可跳過）**

| 項目 | 需要的角色 | 什麼時候 |
|---|---|---|
| 允許你註冊應用程式：租用戶預設允許；若關閉，給你 **Application Developer** | Privileged Role Administrator 指派 | `azd up` 前（否則 preprovision 停下） |
| 租用戶裡要有 Azure CLI 的 service principal（大多已存在）：`az ad sp create --id 04b07795-8ddb-461a-bbee-02f9e1bf7b46` | Cloud Application Administrator 以上 | `azd up` 前；`prereq` 會顯示是否缺 |
| 替 **Team 主人一人** 建 3 筆 delegated grant（Azure CLI→FA API、FA Portal→FA API、FA Portal→登入範圍；`FA Portal` 是工作台的 Entra app） | Cloud Application Administrator、Application Administrator 或 Global Administrator | `azd provision` 之後。部署的人自己能建 grant（有左欄任一角色）時，hook 直接建好，不需要找管理員；不能建時，hook 才把 3 個請求 body 與指令寫到 `.azure/<name>/fa/admin-consent/`（只有 object ID，沒有秘密），交給管理員照 `README.txt` 執行即可，不需要 repo 或 azd |

grant 只給主人一人（`consentType=Principal`），不是全租用戶同意；FA API 也只接受該主人。
要走管理員檔案、而管理員還沒做 C 的第 3 項時，`azd up` 仍會把資源、Agent、API 與工作台部署完，停在第一次 session 準備前；
管理員做完後，你再跑 `azd hooks run postdeploy` 接續。

<a id="first-deploy"></a>
## 第一次部署

```bash
git clone https://github.com/neallee2012/fa-agent.git && cd fa-agent   # 已經 clone 就跳過；不要用 ZIP
export AZURE_CONFIG_DIR="$HOME/.azure-fa"   # 每個 shell 都設；PowerShell 用 $env:AZURE_CONFIG_DIR = "$HOME\.azure-fa"；Git Bash 見下方
export PYTHONUTF8=1                         # Windows 的預防設定（預設編碼 cp1252 曾造成 UnicodeEncodeError，見 deploy/README.md H2）；PowerShell 用 $env:PYTHONUTF8 = "1"
export AZD_NON_INTERACTIVE=false            # 選用：只有 azd 自動進入 no-prompt 的終端機才需要（見下方）；PowerShell 用 $env:AZD_NON_INTERACTIVE = 'false'
az login --tenant <tenant-id>
azd auth login --tenant-id <tenant-id>
azd env new <name> --subscription <subscription-id>   # 已知區域就加 --location <region>，azd up 就不再出區域選單
azd env set FA_TEAM_OWNER_OID <owner-object-id>       # Team 主人不是你時才需要（見下方）
python deploy/azd/fa_azd.py prereq                    # 選用、唯讀；不讀區域，上面的 azd env new 有沒有 --location 都能跑
azd up
```

這是新環境唯一的一組步驟；根目錄 [README](../../README.md#deploy-to-your-azure) 的 PowerShell 版是同一個順序（它預設帶
`--location`、`FA_TEAM_OWNER_OID` 和 `prereq`），[docs/azd-new-environment.md](../../docs/azd-new-environment.md) 只講設計、不列指令。
訂閱與租用戶 ID 的查法見上方[「事前準備」](#prerequisites)；訂閱固定在這個 azd 環境，不使用 az CLI 的預設訂閱。

- **要多久**：依區域與當時平台狀況差很多。2026-10-21 的新使用者實測（第 34 版 2026.10.21-34）：provision 約 10 分鐘
  （其中 Container Apps 環境約 3 分鐘），另外還要等首次安裝的 Job log（見下一點）；整體總時間那次沒量。2026-10-15 那次
  （`fa-azdtest-1015`）第一次 `azd up` 約 1.5～2 小時（光 Container Apps 環境就約 20 分鐘）。
  之後的 `azd up` 約 15 分鐘（例如加一個模型那次 13 分 25 秒）。
- **等 Job log 是正常的**：首次安裝由 Azure 上的 Container Apps Job 執行（寫入資料表的 apply，以及事後核對的 reconcile），它的 Job log 第一次寫進新的 Log Analytics workspace 要 5～15 分鐘
  （2026-10-21 實測 8 分鐘）；批准後的 apply 與 reconcile 兩個 Job 還會各等一次（那次各約 6 分鐘，合計約 20 分鐘）。
  這段時間終端機看起來像停住。不要中斷它；真的中斷了，照[「中斷後接續」](#resume)重跑即可。
- **`AZD_NON_INTERACTIVE=false`**：有些終端機（例如 Claude 桌面 app 裡的終端機分頁）會讓 azd 自動進入
  no-prompt 模式，`azd up` 一開始就停在「Provision cannot continue (interactive prompts disabled) … missing location」。
  設成 `false` 才會照常出區域選單與確認；一般終端機設了也無妨。azd 何時自動開 no-prompt 見下方[「區域與模型」](#region-and-models)。
- **Windows 登入挑不到帳號**：換了新的 `AZURE_CONFIG_DIR` 後，`az login` 會用 Windows 的帳號挑選視窗（WAM），
  裡面可能沒有你要用的帳號。先 `az config set core.enable_broker_on_windows=false` 再 `az login --tenant <tenant-id>`
  （改用瀏覽器登入；這個設定只寫在目前的 `AZURE_CONFIG_DIR`），或直接 `az login --tenant <tenant-id> --use-device-code`。
- **Git Bash 的路徑（曾觀察到，不保證每台都會發生）**：在 Windows 的 Git Bash 裡設 `export AZURE_CONFIG_DIR=$HOME/.azure-fa`
  這類路徑時，曾被 MSYS 的路徑轉換改寫，結果多出一個 `C:\c\Users\<你>\.azure-fa` 資料夾，登入沒有寫進預期的目錄。
  建議照文件用 PowerShell；一定要用 Git Bash 時，改用 Windows 形式的路徑，例如
  `export AZURE_CONFIG_DIR=C:/Users/<你>/.azure-fa`，設完先 `echo $AZURE_CONFIG_DIR` 確認是預期的路徑再 `az login`。
- `<name>`：2–20 個小寫英數字或 `-`，是資源名稱前綴，RG 預設 `rg-<name>`。
- 區域與模型：見下一節。跳過區域選單的 `--location` 見上方 `azd env new` 那行。
- 沒設 `FA_TEAM_OWNER_OID` 時部署的人就是 Team 主人，終端機會要你輸入 `y` 確認。主人不是自己時，先
  `azd env set FA_TEAM_OWNER_OID <object-id>`（查法：`az ad user show --id <主人 UPN> --query id -o tsv`，主人自己登入時 `az ad signed-in-user show --query id -o tsv` 也行）；首次安裝的批准與 session 準備都要由主人本人登入執行。
- 中途會停下來要你做一件事：**首次安裝批准**。終端機會顯示這次的 request、installation、configuration 與 plan hash，
  再問 `[y/N]`；主人輸入 `y`（或 `yes`）才寫入 Table，批准檔綁定 configuration 與 plan 這兩個 hash、10 分鐘內有效。輸入其他內容（包括直接按
  Enter）就停，什麼都不寫。
- **預先批准首次安裝（不必在終端機輸入 `y`）**：主人在 `azd up`／`azd hooks run postdeploy` 前設
  `azd env set FA_APPROVE_INITIALIZATION <環境名稱>`。值和這次的 `AZURE_ENV_NAME` 完全相同（大小寫也要一樣）時，
  上面那個 `[y/N]` 就算已批准，不讀終端機輸入，有沒有終端機都一樣。其他檢查照舊：登入 az 的必須是 `FA_TEAM_OWNER_OID`
  那位主人（不是就停、不寫入）、plan 摘要照樣印出、批准檔欄位與 10 分鐘窗口不變；批准檔的 `decision_id` 會是
  `<環境>-fresh-preapproved-<時間>`（打字批准是 `<環境>-fresh-<時間>`），並多印一行說明這次是預先批准。值寫成別的環境名稱就停下，
  什麼都不寫；沒設（或空值）時和以前一樣：有終端機就問，沒有就停。它只涵蓋首次安裝這一題，不涵蓋 WebIQ 金鑰、窗口 IP
  （`FA_CONFIRM_DEPLOY_WINDOW`）或刪除確認（`FA_CONFIRM_DELETE`）。取捨：少了當場打字確認，但仍要主人本人登入，加上明確寫出環境名稱才生效。
  只用 `azd env set` 設（不要設在 shell 環境變數，postdown 只清得掉 azd 環境裡的值）；`azd down` 的 postdown 會把它清空，同名環境重建時要重新設。（Neal 2026-10-24 同意此做法：「好」。）
- 部署完後，工作台「支援明細」的「目錄中未列出的助理」會列出 c、d 並標 `AGENT_SCOPE_MISSING`：這是預期的，
  見[「4 個 Hosted Agent」](#hosted-agents)。
<a id="region-and-models"></a>
### 區域與模型

不是每個訂閱都能用同一個區域或模型，所以兩者都在 `azd up` 開頭選：

1. **區域**：azd 自己問。選單只列 Foundry Hosted Agents 支援的區域，並隱藏沒有 embedding quota 的區域。
   選單的預設是**第一項**（例如 Australia East），不是推薦區域；直接按 Enter 就會選它。請挑有你要的模型與 quota
   的區域（作者用 westus3）。
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

**先查 quota，必要時預先選好模型。** 同訂閱同區域的帳號共用 quota，各區域、各模型分開算。2026-10-21 在 westus3 就遇過訂閱的
gpt-6 系列（`gpt-6-luna`、`gpt-6.1-sol`）quota 是 0，要改選 `gpt-5.4-mini` 這類有 quota 的模型。

- **查法（唯讀）**：用 [`az cognitiveservices usage list`](https://learn.microsoft.com/cli/azure/cognitiveservices/usage#az-cognitiveservices-usage-list)
  列出該區域每個 quota 的已用量與上限（查 quota 需要訂閱層的 Cognitive Services Usages Reader 或 Reader 角色，見官方
  [Foundry 模型 quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota)），或在 Foundry 入口網站的 **Management > Quota** 看：

  ```bash
  az cognitiveservices usage list --location <region> --subscription <subscription-id> \
    --query "[?contains(name.value, 'GlobalStandard')].{name:name.value, used:currentValue, limit:limit}" -o table
  ```

  FA 部署用 GlobalStandard，名稱是 `OpenAI.GlobalStandard.<模型>`（`gpt-` 開頭的模型 hook 兩種寫法都試，例如 `gpt-4.1-mini` 的 quota 名稱是 `gpt4.1-mini`）。剩餘量是 `limit` 減 `used`，
  單位與 [`model-catalog.json`](../../src/fa-shared-context-poc/model-catalog.json) 的 `capacity` 相同（官方範例的說明是 Tokens Per Minute (thousands)）。
  每個模型需要的量：`gpt-4.1-mini` 500、`gpt-5.4-mini` 100、`gpt-6-luna` 500、`gpt-6.1-sol` 500，記憶開啟時另需 `text-embedding-3-small` 10。
  上面的「選單」用的是同一份 `limit` 減 `used` 與同一份需要量，但只列 chat 模型，也不扣掉環境裡已有的同名部署；
  之後的訂閱預檢（`model-quota`）則會扣掉同名、同模型、同 SKU 的現有部署已佔的量，並把 `text-embedding-3-small` 的 10 一起算（記憶開啟時）。
  所以選單的數字是估計；先查是為了在 `azd up` 前就知道要選哪個區域和模型。
- **quota 是 0 或不夠時 hook 的行為**：選單會把該模型標成 `quota free 0, needs 500 (not enough)`，預設建議的編號只包含「有提供且夠」的模型；
  一個都不夠就停下（`no catalog chat model fits …`），什麼都不改。手動輸入一個標了 `(not enough)` 的編號，選單不會擋，
  但 preprovision 接著的訂閱預檢（`model-quota`）會在建立任何計費資源前停下。`FA_MODEL_CAPACITY_JSON` 通常用來調小需要量（程式接受任何正整數），剩餘是 0 時沒有用。
- **預先選好**：`azd env set FA_MODELS gpt-5.4-mini`，要多個就逗號分隔；`azd env set FA_DEFAULT_MODEL gpt-5.4-mini`（必須在 `FA_MODELS` 裡；
  沒設時有 `gpt-6.1-sol` 就用它，否則用清單第一個）。已設 `FA_MODELS` 時 hook 不再出選單，也不再列 quota，只做上面那個預檢。
  預設模型也會影響記憶庫，見下方「選好之後」。
- **都不夠時**：換一個有 quota 的區域（`azd env new` 時改 `--location`），或向 Azure 申請提高 quota（官方[申請表單](https://aka.ms/oai/stuquotarequest)，申請會改訂閱，由有權限的人送）。

**選好之後**（選單選的或預先設的都一樣）：

- 選好的值存成 `FA_MODELS`／`FA_DEFAULT_MODEL`，之後的 `azd up` 不再問；工作台只列這些模型。
- 沒有終端機（程式以 `sys.stdin.isatty()` 判斷，不看 azd 的 `--no-prompt`，例如 CI）時預設 catalog 全部模型；要指定就先 `azd env set FA_MODELS gpt-5.4-mini,gpt-6-luna`。
- azd 1.32 以上偵測到 CI 或 AI agent 環境（環境變數如 `CLAUDE_CODE_ENTRYPOINT`、`AI_AGENT`）時，會自動開啟 no-prompt 模式，
  略過 azd 自己的區域選單並以「missing required inputs」失敗；要照常互動就設 `AZD_NON_INTERACTIVE=false`
  （來源：azd 文件 `cli/azd/docs/environment-variables.md`，tag azure-dev-cli_1.32.0，2026-10-11 查證）。
- 記憶開啟時（預設），新環境第一次建記憶庫用你選的預設模型（`FA_MEMORY_CHAT_MODEL` 可另外指定，但也必須在 `FA_MODELS` 裡），之後固定，
  所以預設模型要選你會一直留著的；另外只會多部署 `text-embedding-3-small`（記憶需要）。`FA_ENABLE_MEMORY=false` 則不建記憶庫、也不部署它。沒有任何模型是固定必裝的。
- 之後要加模型：`azd env set FA_MODELS …` 再 `azd up`。拿掉的模型只會從工作台消失，部署本身不刪；
  記憶庫建立時用的模型要留在清單裡。

- 選用設定（`azd env set`，在 `azd up` 前）：

| 名稱 | 預設 | 說明 |
|---|---|---|
| `FA_MODELS` | 終端機：依區域選；否則 catalog 全部 | 要部署、工作台要列的 chat 模型，逗號分隔 |
| `FA_DEFAULT_MODEL` | `gpt-6.1-sol`（有選時），否則清單第一個 | 新對話預設模型；第一次建記憶庫也用它 |
| `FA_MODEL_CAPACITY_JSON` | catalog 容量 | 例 `{"gpt-6-luna":50,"gpt-6.1-sol":50}` |
| `FA_MODEL_VERSION_UPGRADE_OPTION` | `NoAutoUpgrade` | |
| `FA_ENABLE_WEBIQ` | `false` | `true`：connection 還不存在時，deploy 以隱藏輸入詢問 WebIQ 金鑰（需終端機）並建 connection 與 toolbox `fa-org-tools`；已存在就只讀回（要換過期的金鑰時，在終端機執行 `python deploy/stage/new_webiq_connection.py --config .azure/<env>/fa/profile.json --rotate`；連線必須已存在，不存在就停下且不寫入。要用 azd 環境的 profile，不加 `--config` 會用 `FA_DEPLOY_CONFIG` 或 `deploy/stage/stage.json`（也就是 stage）。腳本需要 `httpx` 和 `azure-identity`：用 deploy 建好的 `.azure/<env>/fa/venv`（Windows 是 `Scripts\python.exe`，其他是 `bin/python`），或在自己的 venv 執行 `pip install -r src/fa-shared-context-poc/requirements.txt`；見 [deploy/README.md 第 16 關](../README.md#webiq-key-rotation)）。這條 connection 是這個環境唯一的 WebIQ key（2026-10-22 起 API 不再有 WebIQ secret）。 |
| `FA_ENABLE_AGENT_ONBOARDING` | `true` | 「＋」新 Agent 自動補角色：API 身分取得受限 RBAC Admin（只能把 Foundry User／OpenAI User 指派給 service principal、不能刪除；風險見 [ADR-0006](../../docs/adr/0006-team-trust-boundary-grokbot-mode.md)），API 開 `FA_ONBOARDING_*`（stage 同）。不要就在 `azd up` 前 `azd env set FA_ENABLE_AGENT_ONBOARDING false`。已建好、沒設過這個值的環境，下一次 provision（`azd up`，或 `azd provision` 再 `azd deploy`）就會打開；只跑 `azd deploy` 不會，因為它沿用上次 provision 的輸出 |
| `FA_ENABLE_MEMORY` | `true` | 私人記憶與 Team 記憶：deploy 建 Memory Store `poc-team-memory`（`FA_DEFAULT_MODEL`＋text-embedding-3-small，preview，依用量計費）並開旗標；同 stage。私人記憶與 Team note 都由助理 VM 直接讀寫自己 Team 的 Memory Store scope，是 [ADR-0006](../../docs/adr/0006-team-trust-boundary-grokbot-mode.md) 記錄的 Team 隔離已知例外。要關請在第一次 `azd up` 前設 `false`：旗標隨 Agent 第一版發布 |
| `FA_ENABLE_TEAM_MCP_CONNECTIONS` | `true` | 給 API 身分 project 範圍的 Contributor，Team MCP 設定頁才能新增含金鑰的 connection（stage 同） |
| `FA_TIMEZONE` | `UTC` | Hosted 的 `TZ` |
| `FA_HOSTED_IDLE_TIMEOUT_SECONDS` | `3600` | 120–3600 |
| `FA_API_SUBNET_NAT_EGRESS` | `false` | 同 `prepare_environment.py` |
| `FA_NETWORK_MODE` | `public` | `public`：維持現狀。`private`：Foundry Hosted Agent 注入這個環境 VNet 的 subnet、Foundry 帳號關閉公網（[ADR-0010](../../docs/adr/0010-azd-private-network-option.md)）。**只能在建立環境時選，之後不能切換**；`private` 的現況與限制見[「網路模式」](#network-mode) |
| `FA_VNET_RESOURCE_ID` | 空 | 只用於 `private`：改用你自己的既有 VNet（完整資源 ID），FA 不建 VNet、subnet、NSG、NAT。見[「帶入既有 VNet」](#byo-vnet) |
| `FA_API_SUBNET_NAME`／`FA_AGENT_SUBNET_NAME`／`FA_PRIVATE_ENDPOINT_SUBNET_NAME` | `snet-api`／`snet-agents`／`snet-private-endpoints` | 只在設了 `FA_VNET_RESOURCE_ID` 時可改：你 VNet 裡的三個 subnet |
| `FA_DNS_MODE` | `azd` | 只在設了 `FA_VNET_RESOURCE_ID` 時可改成 `existing`：DNS 由你管，FA 不建 private DNS zone |
| `FA_DEPLOY_UPGRADE_SNAPSHOTS` | `false` | `true`：另建「升級時保留助理 VM 工作檔快照」（ADR-0009 phase 5，一般用不到）專用的儲存帳號（`stsnap<token>`、私有端點、30 天刪除規則、帳號刪除鎖、API 身分在該 container 的備份讀寫自訂角色，不含刪除），API 取得 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`；與 `no-auto-gc` 備份帳號分開。打開會多建一個儲存帳號並指派角色，會產生費用；預設關，不需要就維持 `false` |

<a id="network-mode"></a>
### 網路模式（`FA_NETWORK_MODE`）

**`private` 的核心流程已在 2026-10-24 雲端驗收**（[紀錄](../../docs/progress.md#private-e2e-1024)）：新環境 `azd up`、[部署期間的暫時窗口](#private-deploy-window)開關、「前 10 分鐘」、長期記憶（Cosmos DB，[ADR-0011](../../docs/adr/0011-private-mode-cosmos-memory.md)）的寫入、新對話回想、更正與 Team note 都通過，[`private` 的刪除](#private-down)第一次停在傳播延遲、重跑完成且無殘留（競態由 #974 處理）（[ADR-0010](../../docs/adr/0010-azd-private-network-option.md) 第 3 階段）。同日兩輪乾淨重跑也通過（[紀錄](../../docs/progress.md#private-reruns-1024)）：azd 建的 VNet 與[帶入既有 VNet](#byo-vnet)（`FA_DNS_MODE=azd`）都跑完 `azd up`、前 10 分鐘、記憶與「＋」Agent 的記憶，涵蓋 #972、#975、#976、#978 的事先批准與 #974 的刪除重送；#973（記憶 log 匯出）有部署，沒有特別測。**使用上**：azd 建的 VNet 已於 2026-10-24 驗收（含 `azd down`）；帶入既有 VNet 搭配 `FA_DNS_MODE=azd` 的核心流程與 `azd down` 已驗收（拆除後客戶 VNet 三個 subnet 都在、兩個委派 subnet 的委派仍在，不留 FA 的 service association link 或 private endpoint），只測過一種網路拓撲（見[帶入既有 VNet](#byo-vnet)）；`FA_DNS_MODE=existing` 還沒在雲端測過。

- `public`（預設）：資源屬性和加入這個選項之前相同；preprovision 多一次只讀檢查，且只接受 `public`／`private`；`tests/infra/test_network_mode.py` 把每個資源的屬性（含 dev 與 stage 共用的 Foundry 帳號）釘死在舊版。
- `private`：第一次 `azd up` 前執行 `azd env set FA_NETWORK_MODE private`。

`private` 比較適合有高資安、金融監理（金管）或其他法遵要求的企業用戶（Foundry 的資料面（模型與 agent endpoint）不經公網、要自己控管網路出入）；它提供網路隔離的基礎元件，是否合規仍取決於組織自己的控管與評估。一般使用或試用請維持預設的 `public`。

`private` 多建或改變的資源：

- VNet 多一段 192.168.97.0/24 的 `snet-agents`：委派給 `Microsoft.App/environments`，只給 Foundry Hosted Agent 用；有自己的 NSG（只有預設規則，要限制出站時自己加規則）；
- NAT Gateway 與它的固定公網 IP：`snet-api` 和 `snet-agents` 都經它出站（不論 `FA_API_SUBNET_NAT_EGRESS`）；
- Foundry 帳號：Hosted Agent 注入 `snet-agents`；關閉公網（`publicNetworkAccess: Disabled`，`networkAcls` 為 `Deny`＋`AzureServices`）；private endpoint 放在 `snet-private-endpoints`；三個 private DNS zone（`privatelink.cognitiveservices.azure.com`、`privatelink.openai.azure.com`、`privatelink.services.ai.azure.com`）連到 VNet；
- 不建 project capability host（平台會自動建帳號層級的，ADR-0010 第 0 階段實測）；
- 記憶開啟時（預設）：Cosmos DB for NoSQL serverless 帳號 `cosmos-<token>`（`EnableNoSQLVectorSearch`、關閉金鑰驗證與公網），`Sql` private endpoint 放在 `snet-private-endpoints`，private DNS zone `privatelink.documents.azure.com` 連到 VNet；資料庫 `fa-memory`、container `memories`（partition key `/scope`，1536 維 cosine `quantizedFlat`）；只有 API 身分有一個自訂資料角色（5 個 dataActions，不能刪除），範圍是這個 container（[ADR-0011](../../docs/adr/0011-private-mode-cosmos-memory.md) 第 5 節）。每個 Agent 身分除了 project 的 Foundry User／OpenAI User，另給 Foundry 帳號範圍的 Cognitive Services OpenAI User（embedding 走帳號根路徑；隨 RG 刪除）：seed Agent 由 postdeploy 指派；用「＋」新增的 Agent 由 API 指派，為此 API 身分在帳號上另有一筆受限 RBAC Admin，只能把這一個角色指派給 service principal、不能刪除（`FA_ENABLE_AGENT_ONBOARDING=false` 時不建，[ADR-0006](../../docs/adr/0006-team-trust-boundary-grokbot-mode.md)）。API 只在 Bicep 輸出 `FA_AGENT_ONBOARDING_ACCOUNT_ROLE=true`（provision 真的建了這筆委派）時才送 account 那筆指派（`FA_ONBOARDING_ACCOUNT_ROLE`）。這版之前建立的 `private` 環境要先重跑 `azd provision`；沒重跑、只跑 `azd deploy` 時，deploy 會印警告，「＋」照樣完成（只有兩個 project 角色），但新 Agent 的記憶不能用（抽取會略過、什麼都不寫）。重跑後新增的 Agent 才有這個角色，之前新增的要手動補。不建 Memory Store。Cosmos 是 `private` 部署裡最慢的一段（探針第一次約 11 分鐘）。記憶抽取用 `FA_MEMORY_CHAT_MODEL`（沒設時是預設模型）：預設模型較大時，建議在第一次 `azd up` 前把 `FA_MEMORY_CHAT_MODEL` 設成 `FA_MODELS` 裡的小模型（例如 `gpt-5.4-mini` 或 `gpt-4.1-mini`），抽取的延遲與費用較低。

azd 先單獨部署 VNet 再建 Foundry 帳號，所以傳給 `foundation.bicep` 的 `createNetwork` 是 `false`。在 `private` 環境手動單獨重跑 `foundation.bicep` 時必須傳 `networkMode=private`，否則 VNet 會被更新成沒有 `snet-agents`（見 [deploy/README.md](../README.md)「先嘗試原地 reconcile」）。

`private` 做得到的：Foundry 帳號／project 的資料面與 agent endpoint 只能從 VNet 內進入；Hosted Agent 在你的 subnet，可以自己加 NSG、UDR、防火牆；Table、Blob 照舊只走 private endpoint。

`private` 做不到的（它不是「全私有」）：

- Hosted Agent 到 FA API 的 Team 資料仍走公網（TLS＋Entra），因為 API 的入口是公網；工作台與 API 也留在公網＋Entra。全內網是之後另案的 `isolated`。
- 沒有出站過濾：agent 經 NAT 出站，來源 IP 固定，但目的地不限（WebIQ 等 MCP 要連外部）。
- 不能用 Foundry Memory Store；`private` 的長期記憶由 FA 自建（Cosmos DB serverless，ADR-0011）。記憶經 FA API 轉送（Hosted 不直連 Cosmos），所以 Hosted 到 API 這一段同樣走公網 TLS＋Entra。preprovision 依網路模式寫入 `FA_MEMORY_BACKEND`（`private` 且開記憶是 `cosmos`，其他是 `foundry`），不要自己設；之後兩種後端不能互換、也沒有遷移。
- 不支援 Work IQ，也不支援入口網站的 Teams 一鍵發布。部署時 API 會取得 `FA_NETWORK_MODE=private`；若手動開了 Connectors（`FA_TEAM_CONNECTORS_ENABLED`，azd 預設沒開），加入或重新啟用 Microsoft Teams（Work IQ）會回 422 `TEAM_CONNECTOR_PRIVATE_NETWORK`，只能停用。
- 主人不能再從公網用 Foundry 入口網站看 Agent 與資料，要在 VNet 內或經 VPN。
- 刪除很慢而且要照順序（10-24 的三次測試中從開始拆除算起約 50～60 分鐘；刪帳號遇到「project 還列著」的傳播延遲時，hook 會自動等一段時間再送一次），由 `predown` 處理，見[「刪除」](#private-down)。

<a id="private-deploy-window"></a>
#### 部署期間的暫時窗口（`private`）

Foundry 帳號關閉公網後，postdeploy 從你的電腦呼叫 Foundry 資料面的步驟會被擋（403 "Public access is disabled"）。這些步驟只有三個：WebIQ connection 與 toolbox（`FA_ENABLE_WEBIQ=true` 時）、Memory Store（只有 `public` 有；`private` 的記憶是 Cosmos DB，不需要窗口）、第一次部署建 Hosted Agent 版本。其餘步驟走 ARM、VNet 裡的 Container Apps Job，或 FA API 的公網入口，不受影響。所以只在這幾步要跑時，postdeploy 才暫時開一個只允許你這台電腦的窗口（ADR-0010 第 0 階段實測可行）：

0. 一開始先讀 Foundry 帳號：它不是注入 VNet 的帳號（`FA_NETWORK_MODE=private` 設錯在 `public` 環境）就停下，和 preprovision 同一句說明，什麼都不寫。
1. 在建 image 之前就決定要不要開窗口，要的話向 `https://api.ipify.org` 讀你這台電腦目前的公網 IPv4。讀不到或不是公網 IPv4（含 multicast）就停下，什麼都不寫。
2. 印出要允許的 IP 和窗口會開多久，請你在終端機輸入同一個 IP 確認。沒有終端機時設 `FA_CONFIRM_DEPLOY_WINDOW=<IP>`（必須等於這次讀到的 IP，換了網路就要重設）。不符就停下，什麼都不寫；因為在建 image 之前問，不會白等一次建置。
3. 建完 image、要跑這幾步之前才開窗口：先再讀一次 IP，和確認的不同（建置期間換了網路）就停下、不開。再用 ARM PATCH 只改 `publicNetworkAccess: Enabled` 和 `networkAcls` 的三個欄位（`defaultAction: Deny`、`bypass: AzureServices`、`ipRules` 只有你的 IP）；`networkAcls` 的其他欄位（例如 `virtualNetworkRules`）照讀到的原樣送回，`networkInjections` 等其他屬性都不送。等資料面回應（實測約 4 秒，最多等 3 分鐘）。
4. 跑完這幾步（不論成功、失敗或按 Ctrl+C），一定 PATCH 回 `publicNetworkAccess: Disabled`、`ipRules` 清空，並讀回確認（輪詢最多 2 分鐘；每次 `az` 呼叫最多等 90 秒，逾時視為關不回去）；關的這段時間 Ctrl+C 會被忽略。關不回去時印出大段警告與要你自己執行的 `az rest --method patch …` 指令（body 檔在 `.azure/<env>/fa/foundry-window-close.json`），hook 以非零結束。
5. 上次執行在窗口打開時被中斷（關掉終端機等），下一次 `azd deploy`／`azd hooks run postdeploy` 一開始會讀帳號，發現還開著就先關、讀回，再繼續；`azd down` 的 `predown` 也會在問問題之前先關。`azd provision` 也會照 Bicep 把它設回 `Disabled`。

注意：
- **共用 NAT 的 IP 會把窗口開得更寬。** 公司、校園或 VPN 的出口 IP 是很多人共用的，窗口開著的這段時間，同一出口的所有人都連得到這個帳號的資料面（仍要 Entra 身分與 Foundry 角色）。在意的話，換一個只有你自己用的網路再部署。
- 窗口開多久：只在這幾步執行時開著，通常幾分鐘；第一次部署建 Hosted Agent 版本時，每個版本等它啟用最多約 10 分鐘。
- WebIQ 第一次設定會在窗口打開時問金鑰；問的這段時間窗口一直開著，請盡快輸入。
- **開了 WebIQ（`FA_ENABLE_WEBIQ=true`）時，之後每次 `private` 升級都會開一次窗口**：connection 與 toolbox 要從資料面讀回確認（toolbox 沒有 ARM 讀法）。沒開 WebIQ 的升級不讀 IP、不開窗口。
- `public` 不做以上任何一步。

**只能在建立環境時選。** Foundry 的網路注入只能在建立帳號時設定，之後不能加也不能移除。preprovision 會用 Bicep 的命名規則（訂閱、環境名稱、區域）算出這個環境的 Foundry 帳號名稱並讀它：帳號已存在而模式不符，就停下、什麼都不寫；帳號不存在（新環境）就略過。要換模式請另建新環境。

`private` 時 preprovision 另外檢查下面幾項，都只讀、不寫，不符就停下並說明：

- 區域在 Agent Service 的區域表裡。`ukwest` 雖然在 azd 的區域選單裡，卻不在這張表上，`private` 會擋下；`public` 照舊允許（這是既有的不一致，這一版不改 `public`）。
- resource provider `Microsoft.ContainerService` 與 `Microsoft.DocumentDB`（ADR-0011 的 Cosmos DB，只在記憶開啟時檢查；預設開啟）已註冊。hook 不會替你註冊（註冊會改訂閱），請有權限的人對沒註冊的那個執行：`az provider register --namespace <namespace> --subscription <subscription-id>`。

<a id="byo-vnet"></a>
#### 帶入既有 VNet（`FA_VNET_RESOURCE_ID`，`private` 才有）

預設的 `private` 每個環境都建一樣位址的 `vnet-<env>`（192.168.96.0/24、192.168.97.0/24），接到公司的 hub 會重疊。已經有自己的網路時，改用你的 VNet（[ADR-0010 第 2b 階段設計](../../docs/adr/0010-azd-private-network-option.md#第-2b-階段設計帶入既有-vnet2026-10-24)）。**2026-10-24 已在 Azure 驗收 `FA_DNS_MODE=azd` 的核心流程與 `azd down`（[紀錄](../../docs/progress.md#private-reruns-1024)）；`FA_DNS_MODE=existing` 還沒在雲端測過。** 那次只測過一種拓撲：客戶 VNet 事先建在同一訂閱、同一區域的另一個 RG；兩個委派 subnet 都掛 NAT gateway；沒有 hub、防火牆、route table 或自訂 DNS；`FA_DNS_MODE=azd`。其他拓撲（例如經 hub 防火牆出站、Virtual WAN）還沒在雲端跑過。

```bash
azd env set FA_NETWORK_MODE private
azd env set FA_VNET_RESOURCE_ID /subscriptions/<sub>/resourceGroups/<網路的 RG>/providers/Microsoft.Network/virtualNetworks/<vnet>
# subnet 名稱和預設不同時才要設
azd env set FA_API_SUBNET_NAME <Container Apps 的 subnet>
azd env set FA_AGENT_SUBNET_NAME <Foundry agent 的 subnet>
azd env set FA_PRIVATE_ENDPOINT_SUBNET_NAME <private endpoint 的 subnet>
# DNS 由你的 hub 管時
azd env set FA_DNS_MODE existing
azd up
```

你要先準備好（preprovision 只讀檢查，不符就停下、什麼都不寫）：

| 項目 | 要求 |
|---|---|
| VNet | 和這個環境同一個 subscription、同一個區域，**放在另一個 resource group**（`azd down` 會刪掉整個 `rg-<env>`，所以放在裡面會被拒絕）；位址空間不能和下面「三個 subnet」列的保留位址重疊（FA 對 VNet 套用同一份聯集；Foundry 也要求 peer 的 VNet 不重疊，FA 不讀 peer，請自己確認） |
| API subnet | 委派給 `Microsoft.App/environments`，至少 /27，沒有被別的 Container Apps 環境用（有 service association link 而且不是這個環境的 `cae-<env>` 就停下） |
| agent subnet | 委派給 `Microsoft.App/environments`，至少 /27（建議 /24，每個 Hosted session 一個 IP），RFC 1918 位址，名稱不超過 63 bytes，只給這個環境的 Foundry 帳號用（已經有 service association link 就停下；重跑時帳號必須就是注入在這個 subnet）。帳號建立後注入不能改，所以之後清掉 `FA_VNET_RESOURCE_ID` 也會在寫入前停下 |
| PE subnet | 沒有委派，至少 /28（FA 最多約 10 個 private endpoint IP） |
| 三個 subnet | 互不相同；API 與 agent subnet 不能和保留位址重疊：Container Apps workload profiles 保留的 169.254.0.0/16、172.30.0.0/16、172.31.0.0/16、192.0.2.0/24、100.100.0.0/17、100.100.128.0/19、100.100.160.0/19、100.100.192.0/19（[Container Apps 自訂 VNet](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks?tabs=workload-profiles-env)），加上 Foundry 另列的 100.100.224.0/19、0.0.0.0/8、127.0.0.0/8（[virtual-networks，Limitations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks)） |
| 出站 | 由你提供：API 與 agent subnet 要能連到公網（FA API、Entra ID、ACR、App Insights、外部 MCP），例如 subnet 上的 NAT Gateway，或 route table 到防火牆。防火牆要放行 `control-<region>.identity.azure.net`、`<region>.login.microsoft.com`、`login.microsoftonline.com`（或 `AzureActiveDirectory` service tag），NSG 也要允許這些出站。subnet 上沒有 NAT 或 route table 時只會警告（Virtual WAN 可以不掛 route table 就把流量送到 hub）。`FA_API_SUBNET_NAT_EGRESS=true` 不能和這個選項一起用 |
| 權限 | 部署者在這個 VNet 上要能 join subnet 與連結 DNS（一般是 VNet 上的 Network Contributor） |

FA 不會改你的 VNet：不建、不改 subnet、NSG、NAT、route table。落在你 VNet 上的只有 Azure 自動產生的 Container Apps 與 Foundry 的 service association link、private endpoint 的網卡，以及 `FA_DNS_MODE=azd` 時 DNS zone 對 VNet 的連結（zone 在 `rg-<env>`）。訂閱的治理 Policy 可能自動替你的 subnet 掛 NSG（推論：10-24 驗收時客戶 RG 出現三個不是 FA 建的 NSG，當時有 Policy 評估在跑；預設規則沒有擋到流量），NSG 規則要允許上表的出站。10-24 那一次帶入既有 VNet 時，Foundry 帳號與 project 各花約 20 分鐘（azd 建的 VNet 約 4–6 分鐘）；這只是一次觀察，原因未知，不代表每次都會這樣。

**DNS：**
- `FA_DNS_MODE=azd`（預設）：FA 在 `rg-<env>` 建 private DNS zone 並連到你的 VNet。一個 VNet 不能連兩個同名的 zone：VNet 已經連了任何一個同名的 `privatelink.*` zone（hub-spoke、由 Azure Policy 管的中央 zone 都常見）時，FA 建連結的 PUT 會失敗，provision 停在中途。這種 VNet 一開始就用 `existing`。VNet 設了自訂 DNS 伺服器時 preprovision 會停下（自訂 DNS 不會查連到 VNet 的 zone），也請改用 `existing`。preprovision 不掃描 VNet 已有的 zone 連結，這一項要你自己確認。

  provision 中途失敗時留下的東西：
  - 順序是先建三個 Foundry zone（`privatelink.cognitiveservices.azure.com`、`privatelink.openai.azure.com`、`privatelink.services.ai.azure.com`）與連結，再建 Foundry 帳號；Table、Blob、Cosmos DB 的 zone 都在帳號之後。
  - 衝突在 Foundry zone：帳號還沒建，注入還沒定下來；留下的是 FA 已建好的 Foundry zone 與連結。
  - 只有 Blob 衝突（例如 hub 只管 Blob）：帳號與 Table 等都已建好，FA 的 zone、連結，以及已建 private endpoint 上的 zone group 都會留著。
  - 改成 `FA_DNS_MODE=existing` 再跑 `azd up` **不會**移除它們：azd 用 incremental 部署，不刪範本裡沒有的資源。留著的連結會繼續占住這些名稱，你的 hub zone 連不上來。請在 `rg-<env>` 手動清掉，順序如下：
    1. 每個 private endpoint 的 zone group：`az network private-endpoint dns-zone-group list --resource-group rg-<env> --endpoint-name <pe>` 找出來，再 `az network private-endpoint dns-zone-group delete --resource-group rg-<env> --endpoint-name <pe> --name <group>`；
    2. 每個 FA zone 上連到你 VNet 的連結：`az network private-dns link vnet list --resource-group rg-<env> --zone-name <zone>`，再 `az network private-dns link vnet delete --resource-group rg-<env> --zone-name <zone> --name <link>`；
    3. `rg-<env>` 裡的 `privatelink.*` zone：`az network private-dns zone delete --resource-group rg-<env> --name <zone>`。

    清完再設 `FA_DNS_MODE=existing` 跑 `azd up`。另一條路是 `azd down` 後從頭來，但帳號已建時這會連帳號與資料一起刪掉。
- `FA_DNS_MODE=existing`：FA 不建 zone、不建連結，private endpoint 也不帶 zone group。你要讓 VNet 內把下列名稱解析到 private endpoint 的 IP：常見做法是 hub 的中央 zone 加 Azure Policy 自動掛 zone group，或自己建 A 紀錄；自訂 DNS 伺服器要對公網 zone（`cognitiveservices.azure.com` 等）設條件轉送到 168.63.129.16。

  | Private DNS zone | 名稱 |
  |---|---|
  | `privatelink.cognitiveservices.azure.com`、`privatelink.openai.azure.com`、`privatelink.services.ai.azure.com` | `cog-<token>` |
  | `privatelink.table.core.windows.net` | `st<token>` |
  | `privatelink.blob.core.windows.net` | `stbk<token>`、`stcon<token>`（工作台）、`stsnap<token>`（`FA_DEPLOY_UPGRADE_SNAPSHOTS=true`） |
  | `privatelink.documents.azure.com` | `cosmos-<token>`、`cosmos-<token>-<region>`（記憶開啟時） |

  IP 要等 private endpoint 建好才知道，所以 postprovision 會讀回並印出每個名稱與 IP，然後停下；名稱都能解析之後執行 `azd env set FA_DNS_RECORDS_READY true` 再跑一次 `azd up`。工作台的 private endpoint 在 deploy 時才建，deploy 結束時會再印一次清單。`azd down` 之後 `FA_DNS_RECORDS_READY` 會被清掉（重建時 IP 會變），你自己建的紀錄也要自己清。

  `FA_DNS_RECORDS_READY` 設了之後就一直有效，閘門只在 postprovision 檢查這個值，不會核對新的名稱。之後才打開記憶（Cosmos DB）或 `FA_DEPLOY_UPGRADE_SNAPSHOTS` 時，`azd up` 會多建 private endpoint（`cosmos-<token>` 與 `cosmos-<token>-<region>`、`stsnap<token>`），閘門不會再停下，這些名稱的紀錄要你自己補上。每次 `azd up` 的 postprovision 都會印出完整清單，新名稱從那裡找。

**刪除：** 你的 VNet 與 subnet 在另一個 resource group，`azd down` 不會刪它們。`predown` 照原本順序刪 Foundry 帳號，最後讀回**你的** agent subnet 的 service association link 清空。API subnet 的 Container Apps link 要等 azd 刪掉 Container Apps 環境（約 27 分鐘）之後才釋放，`azd down` 剛結束時那個 subnet 可能還不能給別的環境用（這段時間沒有實測）。

### 升級專用：版本開關與相容閘門（第一次部署可跳過）

這一節寫給**已經用舊版 azd 建過環境、現在要升級**的人；第一次部署可以直接跳到[「跑完就能用」](#跑完就能用)。
名詞：2d-i、2d-ii 是 [ADR-0009](../../docs/adr/0009-version-model-g-actual-version-idle-upgrade.md) 的兩個階段代號，意思是助理 session 的綁定記錄從舊格式（v1）換成新格式（`foundry-native-v2`）：2d-i 先讓新舊格式並存，2d-ii 讓 API 只講新格式；
「相容閘門」是 azd deploy 的 hook 在改動任何東西之前先做的唯讀檢查，確認沒有還記著舊格式的助理；全新環境沒有舊記錄，這兩項都已經預設好。

ADR-0009 2d-i 一律打開，不是選用設定：`profile.defaults.json` 寫 `FA_NATIVE_SESSION_GUARD_ENFORCE="1"`，部署時兩位助理的定義宣告
`foundry-native-v2`。2d-ii 起 API 只送 v2，所以開關不能關、不能退版；以前的 `FA_NATIVE_2DI` 已不再讀取。第一次安裝還沒有 API
在服務，沒有舊版本要收斂，所以打開的前提（服務中的 API 已有 P5＋P6）不適用；之後的 `azd deploy` 照 stage 的規則檢查
（見 [2d-i 開關](../../src/fa-shared-context-poc/README.md#native-2di-switch)）。2d-ii（#891）合併前用 azd 建、開關還是 `"0"` 的環境，
只有在還沒有任何綁定列（從沒用過助理）時，下一次 `azd deploy` 才會一併打開 2d-i（服務中的 API 要已有 P5＋P6，而且只有一個 active revision）；已經有 v1 列的環境會被下面的相容閘門擋下，照[升級路徑](#upgrade-2dii)走。

**相容閘門一律武裝。** 2d-ii 的 API 只服務 v2，還記著 v1 的綁定列遇到它會換新 SID、丟工作檔，或一直回 503。所以 azd 的
profile 一律寫 `compatibility_gate_armed: true`（`FA_COMPAT_GATE_ARMED` 不再讀取）。已經有 API 在服務時（之後的 `azd deploy`，
或 API app 已建好、中途停下的首次安裝續跑），hook 在 `check` 之後、sync 和 API 更新之前跑閘門；只要有 v1 列，部署就停下，
訊息指向下面的[升級路徑](#upgrade-2dii)。azd 環境沒有 release 檔；手寫 `.azure/<env>/fa/release.json` 可以讓 `gate_settings` 通過，
但不受支援，要離開閘門請照升級路徑，或退役那位 Agent。第一次安裝還沒有綁定列，不跑閘門
（[相容閘門](../README.md#compat-gate)）。

<a id="upgrade-2dii"></a>
**已用過助理的公開 azd 使用者，升級到 2d-ii 的路徑。** 一直沒用過任何 Agent（沒有綁定列）的人可以直接升級，不會碰到閘門；
萬一閘門還是擋下來，照下面做：

1. `git checkout v2026.10.20-33`（公開 repo `neallee2012/fa-agent` 的第 33 版，最後一版仍會讀 `FA_NATIVE_2DI`）。
2. `azd env set FA_NATIVE_2DI true`
3. `azd deploy`
4. 對閘門訊息列出的每個 Agent 傳一則訊息，等它回覆；它的綁定列就會記成 `foundry-native-v2`。
5. 換回 2d-ii 的 release（`git checkout main`，或其後的版本 tag），再跑 `azd deploy`。
6. 閘門若還是點名某位 Agent，就再傳訊息給它，或[退役它](../README.md#retire-agent)。

## 跑完就能用

首次安裝最後一步，hook 以 Team 主人身分替兩位助理做「新對話選版」：
Responses 通道、伺服器提供的全部能力（桌面、技能、Shell、瀏覽器、交辦、協作、圖片等；`WebIQ-MCP` 已於 2026-10-22 退場，排除設定留著給還提供它的舊 API image），
以及 Team 已批准的技能（最多 4 個）。模板由 `select_all_capabilities.py` 依伺服器自己的選項算出，不從別的 Agent 複製；
之後的 `azd deploy` 不會改它，主人在工作台助理 ⚙ 設定頁的調整都保留。

<a id="model-switch"></a>
兩位助理也打開**對話中換模型**（已有完成回合的對話也能改用另一個模型，換的那一回合會慢一次）：Hosted overlay
`FA_NATIVE_MODEL_SWITCH=1`，和 stage 相同。它寫在
[`profile.defaults.json`](profile.defaults.json) 的 `assistant_hosted_environment`，每次 `azd deploy` 都重新帶上。
要關：把那裡的值改成 `"0"` 再 `azd deploy`。不要只刪掉這個 key：sync 會沿用目前版本的值，等於沒關。
更早用 azd 建的環境，下一次 `azd deploy` 起也會打開。

剩下一件只有真人能做的事：主人登入 hook 印出的工作台網址（之後用 `azd env get-value FA_CONSOLE_URL` 也查得到）、開新對話、送一則短訊息；以 receipt
`completed`、`failure_code=null` 為準。逐步操作見根目錄 README 的[「前 10 分鐘」](../../README.md#first-10-minutes)。

<a id="hosted-agents"></a>
### 4 個 Hosted Agent

`azd up` 在 Foundry project 建 4 個 Hosted Agent（[`profile.py`](profile.py) 的 `AGENT_SUFFIXES`）：

| Agent | 是什麼 | 工作台看得到嗎 |
|---|---|---|
| `fa-<name>-a`、`fa-<name>-b` | 兩位助理（kind `assistant`），聊天、交辦都在這兩位 | 看得到，就是左側的助理清單 |
| `fa-<name>-c`、`fa-<name>-d` | 伺服器設定固定要 4 個 target，這兩個只為補滿（kind `executor`，描述寫明「never invoked」）；不會被呼叫，也沒有在 Team 登錄 | 不在助理清單，只列在「支援明細」的「目錄中未列出的助理」 |

- **費用**：Hosted Agent 的運算依 active session 的 CPU 與記憶體計費（[官方說明](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)，
  見 [ADR-0005](../../docs/adr/0005-long-running-conversation-worker.md)）。c、d 從不被呼叫，首次 session 準備也只做 a、b（`prepare_native_sessions.py`），所以 c、d 不會有 session；
  a、b 的 session 在最後一個請求後閒置 `FA_HOSTED_IDLE_TIMEOUT_SECONDS`（預設 3600 秒）才釋放。實際帳單沒有另外量過。
- **啟動時 c、d 的 `AGENT_SCOPE_MISSING` 是預期的**：它們沒有 Team 登錄，sync 會以這個原因略過（`skipped`），
  助理目錄也把它們標成 `unavailable`。只有 a 或 b 出現這個錯誤才需要處理。

<a id="resume"></a>
## 中斷後接續

- 每個寫入都有 intent 檔（`.azure/<name>/fa/`，不進 Git）。停下來時先照訊息讀回，再
  `azd hooks run postprovision` 或 `azd hooks run postdeploy` 接續；不要刪 intent 重來。
- **在新的終端機接續**：先設同一個 `AZURE_CONFIG_DIR`（[第一次部署](#first-deploy)那行；沒設會用到別的 az 登入），
  再 `cd` 到 repo 根目錄（azd 從目前目錄找 `azure.yaml` 與 `.azure/<name>`），然後才 `azd hooks run postdeploy`：
  ```bash
  export AZURE_CONFIG_DIR="$HOME/.azure-fa"   # PowerShell：$env:AZURE_CONFIG_DIR = "$HOME\.azure-fa"；Git Bash 的路徑寫法見「第一次部署」
  cd <repo 路徑>
  azd hooks run postdeploy
  ```
- 等你回答問題時 azd 結束或按了 Ctrl+C，hook 會停下並印出 `no answer was read`：沒有批准任何東西，
  這個回答也沒有保存；照它印的指令（例如 `azd hooks run postdeploy`）重跑再回答。
- 首次安裝的批准檔有 10 分鐘窗口；過期但 apply 還沒開始時，hook 會停下，請自己把那份檔案移到旁邊保存，
  再重跑一次批准。設了 `FA_APPROVE_INITIALIZATION=<環境名稱>`（見[第一次部署](#first-deploy)）時，重跑會直接寫新的批准檔，不再問。
- `azd provision --preview` 可以先看 what-if（重跑既有環境前先看 what-if，確認不會拿掉 Bicep 沒宣告的現場資源（例如 tenant 治理自動化掛上的 NSG）；見 [deploy/README.md](../README.md#new-environment-standard-flow) 第 5 步）。
- 首次安裝時 Agent 不做 follow（從沒準備過的 Agent 沒有舊環境可跟）。若中斷後**改了程式**才接續，已準備好的 Agent 會停在舊版，直到下一次 `azd deploy` 之後它在回合結束時於背景換上新版；資料不受影響。
- WebIQ 腳本（`deploy/stage/new_webiq_connection.py`）不自己設登入目錄，執行前在同一個 shell 設好 `AZURE_CONFIG_DIR`。

## 之後更新

再跑 `azd deploy`：重 build 映像，然後執行和 stage CI 相同的步驟
（`ci_deploy.py check` → WebIQ（有開才做；連線已存在就只讀回，不需終端機）→ 下面五步），只改映像與 profile 宣告的 ENV；
已安裝的 runtime 設定不會被改。閘門擋下時見[升級路徑](#upgrade-2dii)。

- `guard`：檢查 commit 順序。
- `sync`：發布 Hosted Agent 新版。
- `api`：更新 API。
- `console`：更新工作台。
- `follow`：只回報並讀回各助理的版本與環境，不會替助理換版；助理在各自的回合結束後，才在背景換到新版。

- 映像由 `src/fa-shared-context-poc` 的工作目錄 build，卻以 commit 命名、同一 commit 與 core 再跑時直接沿用。所以這個目錄有**未提交的變更**（含未追蹤檔）時，`azd deploy` 會停下並列出第一個檔案：先 commit。確定要部署工作目錄原樣時，這一次加 `FA_ALLOW_DIRTY_BUILD=true`（例如 `azd env set FA_ALLOW_DIRTY_BUILD true`，用完 `azd env set FA_ALLOW_DIRTY_BUILD false`）；這樣 build 的映像不會被之後的部署沿用。
- 上面的檢查只看會進映像的檔案：三個 Dockerfile（api、hosted、console）、`.dockerignore`，以及它們 `COPY` 自 build context 的路徑。`src/fa-shared-context-poc` 底下只改文件（README 等）不會擋住 `azd deploy`。
- `guard` 要求這次的 commit 包含正在服務的 API revision 記錄的 commit（不能用較舊或另一條分支的 commit 蓋過去，要部署就先合併）；revision 名稱不是部署腳本取的，只警告、不停。
- 正在服務的 revision 記錄的 commit 已不在這個 checkout 的歷史裡（部署過的 feature branch 被 rebase 或丟掉）時，`guard` 也會停下，之後每次 `azd deploy` 都一樣。確定要部署時，這一次加 `FA_ALLOW_ROLLBACK=true`（例如 `azd env set FA_ALLOW_ROLLBACK true`，用完 `azd env set FA_ALLOW_ROLLBACK false`）：停止改成一行 `::warning::`，並略過回退檢查與變更報告，只在這一次執行生效。commit 還在歷史裡、只是這次的 commit 不包含它時，仍然停下，這個開關不放行。stage CI（deploy-staging）**不認也不設** `FA_ALLOW_ROLLBACK`，維持嚴格；stage 上遇到這種停止，由維護者決定怎麼處理，不靠 workflow 繞過。
## 刪除

```bash
azd down --purge
```

- 刪掉整個 RG，**包含 Table、Blob 備份、Memory Store 與 Foundry project**，無法復原。
- 確認：備份帳號的刪除鎖還在、而且沒設 `FA_CONFIRM_DELETE=<環境名稱>` 時，`predown` hook 在終端機要你輸入環境名稱
  （沒有終端機就停下，請你設這個值；見下一點）；azd 自己也會問一次 `y/N`。鎖已經不在（例如上次 down 中途停下）
  就不問環境名稱，只剩 azd 的 `y/N`（`private` 一律會問，見[下一節](#private-down)）。`predown` 移除鎖是在 azd 的 `y/N` 之前，所以移除後會印出原樣放回去的
  `az lock create --name … --lock-type CanNotDelete --resource <備份帳號 ID>`；在 `y/N` 答 N 時照著跑一次。
- 確認前 `predown` 先唯讀列出 RG 以外會刪的東西：兩個 Entra app 與備份自訂角色。備份角色用 RG 算出的固定 ID 讀
  （`guid(<RG id>, 'fa-m1-backup-data-read-write-v1')`，和 `backup-role.bicep` 相同），不比對名稱，所以不會碰到
  別的同名環境的角色；讀不到時印 `could not be read`，`azd down` 照常繼續。
- `postdown` 對備份角色印其中一行：`<角色名稱> (<GUID>): deleted`、`<GUID>: already gone`；azd 環境記的
  `FA_BACKUP_ROLE_ID` 和算出的 ID 不同時什麼都不刪、保留這個值，請你自己核對。
- 約 30 分鐘（2026-10-15 實測 33 分 30 秒）。
- `predown` hook 先移除 FA 給備份帳號加的刪除鎖（`m1-backup-account-delete-protection`），要先輸入環境名稱確認；
  沒有終端機時用 `FA_CONFIRM_DELETE=<環境名稱>`。資源群組裡有別人加的鎖就停下，不替你移除。
- `--purge` 同時清除 soft-delete 的 Foundry account：不清的話它仍占模型 quota（同訂閱同區域共用），
  同名環境也無法重建。
- `postdown` hook 接著刪 RG 外、這個環境自己建的東西：Entra API／Portal app（30 天內可還原）與訂閱層自訂備份角色；
  本機 `.azure/<name>/fa` 改名為 `fa-down-<時間>` 保留紀錄，hook 寫入的值清掉。同名環境能否馬上從頭再裝**尚未驗證**：
  soft-delete 的 Entra app 在 30 天內仍保有 identifierUris／uniqueName，可能與重建衝突；要換新環境名稱最保險。

<a id="private-down"></a>
### `private` 的刪除（ADR-0010 第 7 節）

注入 VNet 的 Foundry 帳號要照順序刪，azd 自己刪 RG 時不會照這個順序，所以 `FA_NETWORK_MODE=private` 的 `predown` 先把它刪完，再交給 azd。上面 `public` 的行為不變。

- **一律問一次環境名稱**，不論備份鎖在不在；和備份鎖的確認合併成同一次詢問，問句本身就寫「permanently delete and purge」（永久刪除並 purge）。沒有終端機時設 `FA_CONFIRM_DELETE=<環境名稱>`；**這個值也同意替這次讀到的 IP 開窗口刪 Agent**（說明裡會印出那個 IP）。輸入不符就停下，什麼都不刪。
- 問之前先唯讀讀出目前狀態：帳號不是注入 VNet 的帳號（`FA_NETWORK_MODE=private` 設錯）就停下，什麼都不刪；資源群組已經不在就說明一切都已刪除，直接結束（soft-delete 的帳號不隨群組消失：還讀得到就印出 purge 指令讓你自己執行，因為這條路沒有問過確認）；上次部署中斷留下的窗口先關掉並讀回（這是回到 Bicep 的狀態，不需要確認，所以就算之後輸入不符、沒有終端機或讀 IP 失敗也會先關）。接著用短短幾行說明：Foundry 帳號（模型、Hosted Agent）會被**永久刪除並 purge**；要刪 Agent 時會暫時對哪個 IP 開窗口；確認後 azd 還會問自己的 `y/N` 處理其餘資源。**在 azd 的 `y/N` 答 N 時，Foundry 帳號已經刪了、其他資源還在**；之後再跑 `azd up` 會建一個新的注入帳號。
- 確認後依序執行（第 0／0b 階段實測的順序）：
  1. project 還在時：開[部署窗口](#private-deploy-window)（同一個 IP，由上面那一次確認涵蓋，不再另外問），用 `force=true` 刪掉所有 Hosted Agent（有 session 時不加 `force` 會回 409；回 404 視為已刪），關窗口並讀回。project 已不在就略過，Agent 已隨 project 刪除；project 已在 `Deleting`（上次執行刪到一半）就只輪詢，不再開窗口。
  2. 刪 project（要先刪 project，帳號才刪得掉，否則回 `CannotDeleteResource`），只送一次（沒有下一步的例外），讀回直到不見；CLI 回錯誤時和帳號一樣先讀狀態。
  3. 刪 Foundry 帳號，只送一次。CLI 回錯誤（特別是 `GatewayTimeout`）時，帳號常常其實已經在 `Deleting`：hook 讀狀態並輪詢，**不重送**。唯一例外：project 剛讀回不見，帳號那邊還列著它，刪帳號回 `CannotDeleteResource` 且列出的巢狀資源只有這個 project（實測 2026-10-24），表示什麼都沒被接受，hook 每 15 秒重送一次、最多 5 分鐘，到上限仍被拒就停下、帳號還在；列到更深的資源（例如 project 底下的 connections）、帳號的 `capabilityHosts` 或別的 project 時不重送，讀一次狀態後停下。`GatewayTimeout` 後約 90 秒內還讀到 `Succeeded`，就表示刪除沒有開始，hook 停下說明；重跑 `azd down` 會先讀狀態再送一次。
  4. 輪詢帳號直到消失（實測約 22 分鐘，最多等 45 分鐘，約每分鐘印一行進度）。
  5. 帳號消失後才 purge（`Deleting` 時 purge 不會結束），讀回直到 soft-delete 的帳號也不見。
  6. 讀回 `snet-agents`（[帶入既有 VNet](#byo-vnet) 時是你的 agent subnet，只讀不刪）的 service association link 直到清空（最多等 20 分鐘）。
  7. 最後移除備份鎖（印出放回去的指令），交給 azd 刪 RG（實測約 27 分鐘，卡在 Container Apps 環境）。10-24 的三次測試中，從開始拆除算起約 50～60 分鐘（`fa-priv-1024` 第一次停在傳播延遲的競態、重跑才完成；`fa-priv2-1024` 61 分，azd 印 29 分 7 秒；`fa-byo-1024` 51 分，azd 印 23 分 59 秒）。
- **可以重跑。** 每一步都先讀目前狀態再接著做：Agent 還在？project 還在？帳號還在、已在 `Deleting`、已 soft-delete、已 purge？SAL 還在？中途停下（等太久、網路斷）就再跑一次 `azd down`。
- 為什麼刪 Agent 要開窗口：Agent 只能從 Foundry 資料面刪，`Disabled` 時筆電連不到。直接刪 project 會不會連帶刪掉 Agent 沒有實測，第 0 階段是先 force 刪 Agent 才刪得掉帳號，所以照實測的順序做。
- 拆除後的檢查項：建立 Hosted 版本時，平台會自動建 Entra agent 身分（instance identity 與 blueprint）。第 0b 階段 force 刪 Agent 後讀回 404，推定由平台自動清除，但沒有證明「無殘留」；有 Graph 讀取權限的人可以再查一次。
- `azd down --purge` 照常可用：帳號已由 hook purge，azd 不會再找到它。2026-10-24 已在 azd 建的 VNet 上驗證（`fa-priv2-1024`：exit 0、29 分 7 秒，途中跑過 #974 的帳號刪除重送，[紀錄](../../docs/progress.md#private-reruns-1024)）；帶入既有 VNet 也已驗證（`fa-byo-1024`：exit 0，拆除後客戶 VNet 不留 FA 的 service association link 或 private endpoint）。
