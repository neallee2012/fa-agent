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
| 已註冊 feature `Microsoft.Network/AllowBringYourOwnPublicIpAddress` 與 9 個 resource provider（[清單](../README.md#new-environment-provider-precheck)） | 少了 feature，Container Apps 環境會「成功」但沒有平台網路（B1）；preprovision 會唯讀檢查並列出指令 |
| 區域有你要的模型與 quota（azd 選區域、hook 選模型時都會顯示；可用 `FA_MODEL_CAPACITY_JSON` 調小容量；`azd up` 前怎麼先查見[「區域與模型」](#region-and-models)） | 同訂閱同區域的帳號共用 quota |
| 區域支援 Foundry Hosted Agents（azd 的區域選單只列這些）與 Container Apps workload profiles | |

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
- **等 Job log 是正常的**：首次安裝的 Job log 第一次寫進新的 Log Analytics workspace 要 5～15 分鐘
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
| `FA_DEPLOY_UPGRADE_SNAPSHOTS` | `false` | `true`：另建 ADR-0009 phase 5 工作檔快照專用的儲存帳號（`stsnap<token>`、私有端點、30 天刪除規則、帳號刪除鎖、API 身分在該 container 的備份讀寫自訂角色，不含刪除），API 取得 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`；與 `no-auto-gc` 備份帳號分開。打開會多建一個儲存帳號並指派角色，會產生費用；預設關，不需要就維持 `false` |

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
  再重跑一次批准。
- `azd provision --preview` 可以先看 what-if（重跑既有環境前建議先看，B5）。
- 首次安裝時 Agent 不做 follow（從沒準備過的 Agent 沒有舊環境可跟）。若中斷後**改了程式**才接續，已準備好的 Agent 會停在舊版，直到下一次 `azd deploy` 的 follow 把它換上新版；資料不受影響。
- WebIQ 腳本（`deploy/stage/new_webiq_connection.py`）不自己設登入目錄，執行前在同一個 shell 設好 `AZURE_CONFIG_DIR`。

## 之後更新

再跑 `azd deploy`：重 build 映像，然後執行和 stage CI 相同的步驟
（`ci_deploy.py check` → WebIQ（有開才做；連線已存在就只讀回，不需終端機）→ `guard → sync → api → console → follow`），只改映像與 profile 宣告的 ENV；
已安裝的 runtime 設定不會被改。

- 映像由 `src/fa-shared-context-poc` 的工作目錄 build，卻以 commit 命名、同一 commit 與 core 再跑時直接沿用。所以這個目錄有**未提交的變更**（含未追蹤檔）時，`azd deploy` 會停下並列出第一個檔案：先 commit。確定要部署工作目錄原樣時，這一次加 `FA_ALLOW_DIRTY_BUILD=true`（例如 `azd env set FA_ALLOW_DIRTY_BUILD true`，用完 `azd env set FA_ALLOW_DIRTY_BUILD false`）；這樣 build 的映像不會被之後的部署沿用。
- 上面的檢查只看會進映像的檔案：三個 Dockerfile（api、hosted、console）、`.dockerignore`，以及它們 `COPY` 自 build context 的路徑。`src/fa-shared-context-poc` 底下只改文件（README 等）不會擋住 `azd deploy`。
- `guard` 要求這次的 commit 包含正在服務的 API revision 記錄的 commit（不能用較舊或另一條分支的 commit 蓋過去，要部署就先合併）；revision 名稱不是部署腳本取的，只警告、不停。
- 正在服務的 revision 記錄的 commit 已不在這個 checkout 的歷史裡（部署過的 feature branch 被 rebase 或丟掉）時，`guard` 也會停下，之後每次 `azd deploy` 都一樣。確定要部署時，這一次加 `FA_ALLOW_ROLLBACK=true`（例如 `azd env set FA_ALLOW_ROLLBACK true`，用完 `azd env set FA_ALLOW_ROLLBACK false`）：停止改成一行 `::warning::`，並略過回退檢查與變更報告，只在這一次執行生效。commit 還在歷史裡、只是這次的 commit 不包含它時，仍然停下，這個開關不放行。stage CI（deploy-staging）**不認也不設** `FA_ALLOW_ROLLBACK`，維持嚴格；stage 上遇到這種停止，由維護者決定怎麼處理，不靠 workflow 繞過。
## 刪除

```bash
azd down --purge
```

- 刪掉整個 RG，**包含 Table、Blob 備份、Memory Store 與 Foundry project**，無法復原。
- 確認：M1 備份鎖還在、而且沒設 `FA_CONFIRM_DELETE=<環境名稱>` 時，`predown` hook 在終端機要你輸入環境名稱
  （沒有終端機就停下，請你設這個值；見下一點）；azd 自己也會問一次 `y/N`。鎖已經不在（例如上次 down 中途停下）
  就不問環境名稱，只剩 azd 的 `y/N`。`predown` 移除鎖是在 azd 的 `y/N` 之前，所以移除後會印出原樣放回去的
  `az lock create --name … --lock-type CanNotDelete --resource <備份帳號 ID>`；在 `y/N` 答 N 時照著跑一次。
- 確認前 `predown` 先唯讀列出 RG 以外會刪的東西：兩個 Entra app 與備份自訂角色。備份角色用 RG 算出的固定 ID 讀
  （`guid(<RG id>, 'fa-m1-backup-data-read-write-v1')`，和 `backup-role.bicep` 相同），不比對名稱，所以不會碰到
  別的同名環境的角色；讀不到時印 `could not be read`，`azd down` 照常繼續。
- `postdown` 對備份角色印其中一行：`<角色名稱> (<GUID>): deleted`、`<GUID>: already gone`；azd 環境記的
  `FA_BACKUP_ROLE_ID` 和算出的 ID 不同時什麼都不刪、保留這個值，請你自己核對。
- 約 30 分鐘（2026-10-15 實測 33 分 30 秒）。
- `predown` hook 先移除 FA 給 M1 備份帳號加的刪除鎖（`m1-backup-account-delete-protection`），要先輸入環境名稱確認；
  沒有終端機時用 `FA_CONFIRM_DELETE=<環境名稱>`。資源群組裡有別人加的鎖就停下，不替你移除。
- `--purge` 同時清除 soft-delete 的 Foundry account：不清的話它仍占模型 quota（同訂閱同區域共用），
  同名環境也無法重建。
- `postdown` hook 接著刪 RG 外、這個環境自己建的東西：Entra API／Portal app（30 天內可還原）與訂閱層自訂備份角色；
  本機 `.azure/<name>/fa` 改名為 `fa-down-<時間>` 保留紀錄，hook 寫入的值清掉。同名環境能否馬上從頭再裝**尚未驗證**：
  soft-delete 的 Entra app 在 30 天內仍保有 identifierUris／uniqueName，可能與重建衝突；要換新環境名稱最保險。
