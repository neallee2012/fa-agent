# 擴充 FA：MCP、skills、plugin 與 connector

給第一次部署 FA、想加自己工具的人。每一段先說在哪裡改，再說要不要改程式或重新部署。程式路徑都相對於 repository 根目錄，`shared_context/` 指 `src/fa-shared-context-poc/shared_context/`，`index.html` 指 `src/fa-shared-context-poc/web_test_console/index.html`。

## 先看結論

| 想做的事 | 做法 | 要改程式或重新部署嗎 |
|---|---|---|
| 接一個用金鑰（header）驗證的遠端 MCP server | 工作台「Agent Team 設定」→ Team MCP | 不用 |
| 在助理 VM 裡跑一個本機（stdio）MCP server | 加 `tool_catalog/*.json`，並把執行檔裝進 Hosted 映像 | 要，重建映像並發布新的 Hosted 版本 |
| 寫一個 skill（流程說明） | 「Agent Team 設定」→ Team Skills → 新增，再由 Team 主人批准 | 不用 |
| 裝 plugin | FA 沒有 plugin 機制，見下方「Plugin」 | — |
| 接一個用 Entra OAuth 登入的服務（像 Teams） | 在 connector registry 加一筆 | 要改程式，也要 Entra app |

`azd up` 不需要 Microsoft Teams、Work IQ 或 GitHub。部署完成時 Team MCP 是開的（還沒有任何 server），connector 是關的。

## `azd up` 完成後的預設狀態

| 項目 | 預設 | 依據 |
|---|---|---|
| Team MCP | 開，沒有 server | `deploy/azd/profile.defaults.json:56-57`；`azd env set FA_ENABLE_TEAM_MCP_CONNECTIONS false` 會拿掉（`deploy/azd/profile.py:106-109`），Bicep 也就不給 API 寫 connection 的權限 |
| Team connector（Teams 等） | 關 | profile 從不寫 `FA_TEAM_CONNECTORS_ENABLED`、`FA_CONNECTOR_*`（`deploy/azd/profile.defaults.json`、`deploy/azd/profile.py:105-128`；測試 `tests/shared_context/test_azd_profile.py::test_a_new_environment_has_no_team_connector_and_needs_no_teams_or_github_app`） |
| WebIQ 網頁搜尋 | 關 | 要 `azd env set FA_ENABLE_WEBIQ true` 才建 toolbox `fa-org-tools`（`deploy/azd/fa_azd.py:835-840`、`deploy/azd/profile.py:121-122`） |
| Feed reader MCP | 不部署 | `deploy/azd/profile.defaults.json:2`；沒有簽章金鑰時，`x-feed-url` 的 Team MCP 會回 `TEAM_MCP_FEED_UNAVAILABLE` |
| Playwright、桌面、shell、skills、交辦等內建能力 | 第一次安裝時替兩位助理選好 | `deploy/azd/fa_azd.py:870-881`（只在第一次；之後的部署不會改你在設定頁的選擇） |
| Skills | 沒有 | repo 裡沒有預設的 skill 或 SOP |

connector 關著時，API 不安裝 connector 路由（`shared_context/team_connector_api.py:24-25`），回合也不帶任何 connector（`shared_context/team_connector_narrowing.py:114`），所以不會出現「需要連線」卡。工作台的 Connectors 分頁顯示「這個環境還沒開放 Connectors」（`src/fa-shared-context-poc/web_test_console/team_connectors.py:105-106`）。Hosted 不讀這個旗標。azd 建的 Entra app 只有 API 和 Portal 兩個（`deploy/azd/entra.py`），沒有 Teams、Work IQ 或 GitHub 的 app。

## MCP

### 金鑰型遠端 MCP：Team MCP（不用改程式）

1. Team 主人開左下 ⚙「Agent Team 設定」→「Team MCP」。
2. 填名稱、https 網址、允許的工具（至少 1 個、最多 32 個），以及「每次呼叫都要我批准」（預設）或「不用批准，直接執行」。
3. 驗證選既有的 connection，或建一條新的：填 header 名稱（預設 `x-apikey`）和金鑰。金鑰只在送出的那一次傳給 API，由 API 建成 Foundry 的 `CustomKeys` connection（`shared_context/team_mcp.py:252-255`）。
4. 送出後，API 在 Team 自己的 Foundry toolbox `fa-team-{team}`（`shared_context/team_mcp_wire.py:66-70`）建新版本並設為預設（`shared_context/team_mcp.py:206-217` 的 `create_version`、`promote`；流程在 `:494-534` 的 `submit`、`_finish`）。FA 自己不另存一份。
5. 到每位要用的助理列 ⚙ →「MCP」打開它，按「套用到新對話」。只影響之後的新對話。

限制：網址必須是 https，不能帶帳密或 `#`（`shared_context/team_mcp_wire.py:79-84`）；一個 Team 最多 8 個 server（`shared_context/team_mcp_wire.py:26`）；最後一個 server 不能停用，要先加另一個。只支援 header 金鑰；需要 OAuth 登入的 MCP 走下面的 connector。停用後，所有對話從下一回合起都不能再用它。

### 本機 MCP 或組織預裝的 MCP：`tool_catalog/`（要重新部署）

組織層級的工具定義在 [`src/fa-shared-context-poc/tool_catalog/`](../src/fa-shared-context-poc/tool_catalog)，一個 JSON 檔放一個 server，用標準 `mcpServers` 格式。API 映像把這個目錄複製到 `/opt/fa-catalog`，建映像時就檢查格式（`src/fa-shared-context-poc/Dockerfile.behavior-api:10,17`），任何一個檔案不合格整份都不載入。

- **stdio**：參考 [`playwright.json`](../src/fa-shared-context-poc/tool_catalog/playwright.json)。`command` 必須是 Hosted 映像裡 `/opt/fa-tools/` 底下的絕對路徑（`shared_context/tool_catalog.py:36`），所以要同時改 [`Dockerfile.behavior-assistant-desktop`](../src/fa-shared-context-poc/Dockerfile.behavior-assistant-desktop) 把執行檔裝進去。`npx`、`uvx`、`docker`、shell 和 `@latest` 都會被拒絕，不在執行時下載。
- **toolbox**：參考 [`webiq-toolbox.json`](../src/fa-shared-context-poc/tool_catalog/webiq-toolbox.json)。另外要讓 API 的 `FA_CAPABILITY_TOOLBOX_ENTRIES` 列出它；azd 目前只會寫入 `WebIQ`（`deploy/azd/profile.py:121-122`）。
- 每個檔案都要列出允許的工具；不接受萬用字元。格式細節見 [API 與 Hosted 說明的組織 MCP catalog](../src/fa-shared-context-poc/README.md#組織-mcp-catalog-載入local)。

改完後重建 API 與 Hosted 映像，用 `azd deploy` 發布，再到助理 ⚙ →「MCP」打開。VM 裡沒有能在執行時加 MCP 的設定檔：Copilot SDK 的 session 以 `mcp_servers={}`、`enable_config_discovery=False` 建立，只放進 catalog 選到的 server（`shared_context/m2_copilot.py:1459`、`:1486-1487`）。

### 批准

- **Toolbox 裡的 MCP**（Team MCP、WebIQ、connector）只看每個 server 的「每次批准／不用批准」。
- **「Agent Team 設定」→「批准規則」**（最多 20 條，「先問我」或「自動允許」）管 shell、助理桌面、交辦、助理之間傳話，以及不在 Toolbox 裡的 MCP（`index.html:479`）。

## Skills、SOP 與角色

**Skill。** Team 主人在「Agent Team 設定」→「Team Skills」→「新增 skill」寫名稱（64 字內）、用途（512 bytes 內）、SKILL.md 本文（8 KiB 內），可加最多 4 個純文字附檔。送出後是**未批准草稿**，不會生效。主人查看內容後批准；批准會釘住那一版的 SHA-256，已決定的版本不能再改（`shared_context/m3_artifacts.py:340-371`）。要改內容就寫一份新的，用「批准並取代」換掉舊的；停用會保留原內容與紀錄。助理也能在對話裡把做法存成草稿（內建能力 `fa-skillify-v1`），一樣要主人批准。

批准後，到助理 ⚙ →「Skills」選它（每位助理最多 4 個，`shared_context/behavior_agent_template.py:91`），按「套用到新對話」。回合裡助理用 `read_skill` 工具按需讀取已批准的版本（內建能力 `fa-skills-v1`，`shared_context/behavior_capabilities.py:148`），不是 Copilot SDK 的 skill 目錄（`skill_directories=[]`，`shared_context/m2_copilot.py:1460`）。

**SOP。** 資料模型有 SOP 類型，但工作台沒有寫 SOP 的入口；目前把流程寫成 skill 即可。

**角色。** 在工作台按「＋」新增助理時填「角色說明」，上限 2048 bytes（`shared_context/behavior_agent_role_prompt.py:22`），存檔就生效，存成一個不會改寫的版本（`shared_context/behavior_agent_role_prompt_versions.py:89-90`）。目前只有新增助理時會寫角色，工作台沒有修改既有助理角色的入口。`azd up` 建的兩位助理沒有角色說明。

「＋」在 azd 環境預設就能用：`FA_ENABLE_AGENT_ONBOARDING` 預設 `true`（Neal 2026-10-15 決定；`infra/azd/main.parameters.json:29-30`、[azd 新環境](azd-new-environment.md)）。Bicep 給 API 身分一個受條件限制的 RBAC 管理角色，只能把 project 上的 Foundry User 和 Cognitive Services OpenAI User 指派給 service principal、不能刪除（`infra/azd/main.bicep:252-261`），再輸出 `FA_AGENT_ONBOARDING`；deploy 依這個輸出把 `FA_ONBOARDING_*` 寫進 API（`deploy/azd/profile.py:118-121`）。新增助理時 API 就替它打開 endpoint 協定並補上這兩個角色（`shared_context/behavior_agent_setup.py:153-184`）。這等於讓 API 身分能替同一個 project 上的任何 service principal 加這兩個角色（風險見 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md) 與 #469）。這條路沒有在 azd 環境實跑過。

不要這筆權限就在 `azd up` 前關掉：

```powershell
azd env set FA_ENABLE_AGENT_ONBOARDING false
azd up
```

關掉時「＋」照樣看得到，也會建出助理、存下角色，但上面兩件事要人手動補；沒補，新助理的回合預期會失敗（推論，沒有在 azd 環境實測）。已建好、沒設過這個值的環境，下一次 provision（`azd up`，或先 `azd provision` 再 `azd deploy`）就會打開；只跑 `azd deploy` 不會，因為 deploy 沿用上次 provision 寫下的 `FA_AGENT_ONBOARDING=false`。

## Plugin

FA 沒有 plugin 機制。Copilot SDK 的 plugin 目錄在 FA 是關的（`plugin_directories=[]`，`shared_context/m2_copilot.py:1460`）。工作台助理 ⚙ 裡的「Plugins」分頁，列的是 FA 內建能力（桌面、shell、交辦、skills 等，`index.html:1831`；清單在 `shared_context/behavior_capabilities.py:141-155`，部署時由 `FA_CAPABILITY_BUILTINS` 決定提供哪些），只能開或關，不能安裝。新增內建能力要改程式。想擴充助理能做的事，用上面的 MCP、skill 或下面的 connector。

## Connector

connector 是用 Entra OAuth 代 Team 主人登入外部服務的 MCP。每個 Team 有自己的 toolbox `fa-connector-{team}-{connector}`，讀和寫分成兩個 entry，各自設批准（`shared_context/team_connector_wire.py:1-7`）。目前 registry 只有 Microsoft Teams（經 Work IQ MCP，`shared_context/connector_registry.py:73-100`）。GitHub connector 還在設計（[設計](connector-design.md)），程式裡沒有。

### 開啟 Teams connector

需要：一個能申請 Work IQ 權限的 Entra app（client ID 和 secret）、管理員同意，以及 Microsoft 365 管理中心的 Work IQ 原則允許要用的動作。然後：

1. API 準備好四件事：
   - `FA_TEAM_CONNECTORS_ENABLED=1`。只接受 `1`、`0` 或不設；寫成 `true` 這類其他值，API 安裝路由時就會丟出錯誤（`shared_context/team_connector_wire.py:30-31` 用 `team_mcp_wire.py:52-58` 的檢查，未實跑）。
   - `FA_CONNECTOR_TEAMS_CLIENT_ID=<app 的 client ID>`。
   - `FA_TEAM_MCP_PROJECT_RESOURCE_ID=<Foundry project 的完整 ARM ID>`，必須是 API 本身服務的那個 project（`shared_context/team_mcp_api.py:105-121`）。沒設或對不上時，Connectors 頁和加入都回 503 `TEAM_MCP_UNAVAILABLE`（`shared_context/team_connector.py:49-50`）。
   - API 的身分在這個 project 上有 Contributor，才能建 connection。沒有時 Foundry 拒絕寫入，API 回 403 `TEAM_MCP_FORBIDDEN`（`shared_context/team_mcp.py:123-127`）。

   後兩件和 Team MCP 新增金鑰 connection 用的是同一套。azd 環境預設 `FA_ENABLE_TEAM_MCP_CONNECTIONS=true`，已經有了（`deploy/azd/profile.py:106-111`、`infra/azd/main.bicep:82-83,262`）。
2. Team 主人在「Agent Team 設定」→「Connectors」加入 Teams，貼一次 client secret。secret 只經過送往 API 的那一個請求，由 API 寫進 Foundry 的 OAuth connection。沒設 client ID 時，加入會回 `TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE`（`shared_context/team_connector.py:100-103`）。
3. 把這個 connection 的 redirect URI 加進 Entra app。redirect URI 由 Foundry 指定（`shared_context/team_mcp.py:260-261`），格式像 `https://global.consent.azure-apim.net/redirect/<id>`，每條 connection 的 `<id>` 不同。connection 名稱每個 Team 固定一個（`shared_context/team_connector.py:36-38`），所以每個 Team 第一次加入時做一次。這是改 Entra app 的租用戶寫入，要有該 app 的擁有者或管理員權限。依據是 dev 10-10 的實跑：新 connection 的 redirect 不在 app 裡，要另外加進去（[紀錄](progress.md#connector-m1-dev-1010)）；FA 程式不會替你加，其他租用戶是否一樣沒有驗證過。
4. 加入後，Team 的每位助理預設都帶這個 connector（`shared_context/team_connector_narrowing.py:1-7`）；不想讓某位助理用，到它的 ⚙ →「MCP」關掉，只影響之後的新對話。同意是每位助理各自的（`shared_context/team_connector.py:5-6`）：
   - 還沒同意的助理，在工作台由真人送出的回合裡，回合一開始、模型還沒動之前列工具時就會出「需要連線」卡，不管這則訊息要不要用 Teams（`shared_context/native_toolbox.py:680-693`）。回合停在卡上等主人。主人在卡上的連結同意後按「我連好了，繼續」，同一回合接著跑；之後這位助理的對話不再問。
   - 按「先不用」，這個對話之後的回合不再出卡，Teams 工具只回報尚未連線，其他工具照常。這個決定隨對話存在 API 的 `consent-declined` 紀錄裡（Agent、對話、toolbox 為 key），換版本或 VM 重啟後仍有效；同一個對話內不能撤回；主人在其他對話連好 Teams 後，這個對話的 Teams 工具也會變成可用（連線每位 Agent 一份）；若仍未連線，新對話會再出卡。VM 程序內另有 `_DECLINED` 只省一趟來回（`shared_context/native_toolbox.py`）。
   - 已同意的連線被撤銷時，會出同一種卡，要求重新連線（呼叫工具時見 `shared_context/native_toolbox.py:809-818`）。
   - 助理之間交辦、排程，以及從 Teams 轉來的回合（request ID 以 `tr-` 開頭，含它的自動接續）不出卡、不等待，直接當成尚未連線繼續（`shared_context/native_toolbox.py:539-543,629-647`；#778）。

**azd 環境目前沒有對應的設定。** `.azure/<env>/fa/profile.json` 每次 hook 執行都由 `deploy/azd/profile.defaults.json` 和 azd 環境變數重建（`deploy/azd/fa_azd.py:567-570`），直接改檔會被蓋掉，也沒有 azd 變數對應 `FA_TEAM_CONNECTORS_ENABLED` 和 `FA_CONNECTOR_TEAMS_CLIENT_ID`。要在 azd 環境開，得仿照 `FA_ENABLE_WEBIQ`（`deploy/azd/profile.py:121-122`）擴充 `profile.py`。這條路沒有實作，也沒有在 azd 環境驗證過；Entra app 與 Work IQ 原則的細節見 [connector 設計](connector-design.md)。

<a id="connector-acceptance"></a>
### Teams connector 驗收到哪裡

- **測試環境（dev）**：10-10 真人加入 Teams、在 Connect 卡同意，並讀到指定 chat 的最新訊息（[紀錄](progress.md#connector-m1-dev-1010)）。10-11 工作台允許送出與拒絕送出都通過，從 Teams 轉來的回合也通過（[紀錄](progress.md#dev-deploy-8415-1011)）。上面第 4 步「非真人回合不出卡」的修正（#778）在 dev 還沒部署。
- **預備環境（stage）**：隨第 24 版部署（`deploy/stage/release.json`，#760），這一版已包含 #778。stage 的部署工作流程 10-11 成功；stage 上的真人驗收還沒有紀錄。

### 加一種新的 connector

- **服務用金鑰驗證**：不需要 connector，用上面的 Team MCP。
- **服務用 Entra OAuth**：在 [`connector_registry.py`](../src/fa-shared-context-poc/shared_context/connector_registry.py) 仿照 `TEAMS` 加一個 `ConnectorRef`，放進最後一行的 `CONNECTORS`：
  - `id`：小寫英數，16 字內；`server_url`：https。
  - `authentication`：`CustomOAuth2`，`app_setting` 是 API 環境變數名稱，格式 `FA_CONNECTOR_<名稱>`，以及 scopes。這是目前唯一的驗證方式（`connector_registry.py:37-43`）；Foundry 代管 OAuth（例如 GitHub 的 `foundrygithubmcp`）還不支援。
  - `read`、`write`：各一個 toolbox entry，列出工具與預設批准。一個工具只能是讀或寫；兩邊都沒列的工具不開放。
  - `revoke_url`、`disclosure`：工作台卡片顯示的撤銷入口與資料範圍說明。

  Connectors 頁會列出 registry 的每一項（`shared_context/team_connector.py` 的 `view`），撤銷提示的文字沒有對應時用通用說明。接著照「開啟 Teams connector」設環境變數，並為新的 connector 補測試（參考 `tests/shared_context/test_team_connector_api.py`）。只改設定不能新增 connector：API 會拒絕 registry 裡沒有的 `connector_id`（`shared_context/team_connector_wire.py:62-67`）。新 connector 還沒有人實際做過。

## Verified against

- 程式基準：`origin/main` `aeea27c3`，加上本文件所在分支的測試提交。
- 已讀程式：`deploy/azd/profile.defaults.json`、`profile.py`、`fa_azd.py`、`entra.py`、`azure.yaml`；`shared_context/` 的 `connector_registry.py`、`team_connector.py`、`team_connector_api.py`、`team_connector_wire.py`、`team_connector_narrowing.py`、`team_mcp.py`、`team_mcp_wire.py`、`team_mcp_api.py`、`tool_catalog.py`、`behavior_capabilities.py`、`behavior_agent_template.py`、`behavior_agent_role_prompt.py`、`m3_artifacts.py`、`team_skills_api.py`、`m2_copilot.py`；`web_test_console/index.html`、`team_connectors.py`；`tool_catalog/*.json` 與兩個 Dockerfile。
- Review round 1 補讀：`shared_context/` 的 `native_toolbox.py`（C3 Connect 卡的開卡條件與 `_DECLINED`，#778 `a935b97a`、`cfd18d92`，已在基準內）、`native_hitl_runtime.py` 的 `connect`、`behavior_native_turn_wire.py` 的 `RELAY_PREFIX`、`behavior_agent_setup.py`、`behavior_agent_onboarding.py` 的 `default_setup`；`infra/azd/main.bicep`、`main.parameters.json`、`infra/shared-context/stage-onboarding-delegation.bicep`；`deploy/stage/release.json`、`stage.json`；`docs/progress.md` 10-10、10-11 的 connector 紀錄。這一輪只改文件，沒有重跑測試。
- 實際驗證（本機 Windows，離線）：`test_azd_hooks.py`、`test_azd_profile.py`、`test_connector_consent.py`、`test_team_connector_api.py`、`test_team_mcp_api.py`、`test_native_toolbox_unavailable.py` 通過；`test_team_connector_narrowing.py` 7 則在 setup 停在 `the pinned offline tokenizer cache is required`（需要 `TIKTOKEN_CACHE_DIR`，見 [API 與 Hosted 說明](../src/fa-shared-context-poc/README.md)），本次沒有執行。
- 限制：本文沒有在雲端實跑。`azd up` 不帶 connector 的結論來自程式與離線測試；作者實跑過的 `azd up` 本來就沒有 connector。自訂 connector、在 azd 環境開 Teams connector、新增 stdio MCP 都沒有人實際做過。
