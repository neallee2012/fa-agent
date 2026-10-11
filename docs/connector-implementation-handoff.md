# FA connector 設計與實作交接計畫

本文件交給下一位總負責 Agent 使用。它接續 [connector-design.md](connector-design.md) 的產品方向，先收斂 P0a 與 P0b，再以 Microsoft Teams 完成第一波 connector，最後才做 GitHub 第二波。工作只補 Foundry 沒有替 FA 完成的 Team 範圍、連線卡、批准接點與工作台設定，不另做 OAuth broker，也不另做 Teams 或 GitHub connector。

計畫基準是 2026-10-09 台灣時間。#741、#742、#743、#744 已合併。P0a 與 P0b 的雲端設定已準備完成，但真人 OAuth 同意與後續探針尚未完成。舊工作階段的批准、授權連結與操作時窗不轉移給新 Agent。

## 如何使用這份交接

一個勾選框是一個可驗收單位。只有在指定證據存在時才勾選。證據可以是 commit、PR、測試輸出、去敏報告、工作台畫面或雲端讀回。每個 PR 只完成一個可獨立核對的改變。

下一位 Agent 以單一總協調者負責整個計畫。若本機有 Orchestrate skill，可用它管理多個 PR owner。沒有時直接照本文件逐段執行。不要把不同 PR 的未提交修改放在同一個 worktree。

程式 PR 在 exact head 完成兩軸 review。直到首次部署完成，review prompt 必須原文帶入：

> 請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。

只把會造成資料損失、重送、secret 外洩或明顯執行錯誤的 P1、P2 當阻擋。docs-only PR 不跑兩軸 review。程式 PR 兩軸清零且 GitHub 顯示 CLEAN 後，由負責該 PR 的工作階段合併。只有 stage release PR 要附 dev 測試報告並由 Neal 決定是否合併。

## 交接完成後使用者會看到什麼

- Team 主人可在「Agent Team 設定」的 Connectors 分頁加入、連線、停用 Microsoft Teams。
- Teams connector 實際使用 Work IQ MCP。畫面要明白說明它能讀取的不只 Teams，也可能包含使用者的 Microsoft 365 郵件與檔案。
- connector 預設提供給 Team 內所有 Agent。每位 Agent 可在自己的齒輪設定關閉它。
- 第一次使用或授權失效時，工作台顯示 Connect 卡。consent URL 不進模型、不進一般 log、不進 PR。
- 使用者按「我連好了，繼續」後，系統只恢復原回合一次。再次收到 `-32006` 就結束並說尚未連好。
- Teams 讀取預設直接執行。Teams 寫入預設每次出批准卡，卡片顯示目標與完整文字。
- 結果不明的 Teams 寫入不會自動重送，也不會換 ID 當成新操作。
- 第二波 GitHub 重用同一套 connector 核心，只新增 GitHub 參考資料與實際工具分類。

## 不可改變的產品決定

1. 第一波是 Microsoft Teams。第二波是 GitHub。
2. 優先使用 Foundry 已有的 Work IQ MCP 與 `foundrygithubmcp`。
3. OAuth token 只由 Foundry connection 保存與更新。FA、Agent VM、模型與工作台都不能持有 token。
4. connector 是 Team 層。預設所有 Agent 可用，每個 Agent 可個別關閉。
5. 寫入預設每次詢問。Team 主人可在設定中改成不詢問。
6. OAuth connector 在多 Team 正式上線前只支援單一 Team 環境。
7. 一個 OAuth connector 使用自己的 Foundry toolbox。P0a 已證明未同意的 OAuth source 會讓該 toolbox 的整份 `tools/list` 失敗。
8. 不自行建立 connector 目錄。只保存 FA 呼叫所需、Foundry 又不提供的最小 `ConnectorRef`。
9. Team 停用 connector 不刪 connection、toolbox 或舊版本。
10. 新對話可以取得新 connector。既有對話不會因新增 connector 自動擴權。
11. Team 停用 connector 後，所有 Agent 下一個回合都不能再使用它。
12. per-Agent 關閉 connector 只影響該 Agent 的新對話，和現有 Agent 設定行為一致。

## 先讀哪些來源

- [ ] 讀 [AGENTS.md](../AGENTS.md) 與 [CURRENT-STATE](status/CURRENT-STATE.md)。
- [ ] 讀 [CONVENTIONS](CONVENTIONS.md) 的第 1、2、3、5、6、8 節。
- [ ] 讀 [connector-design.md](connector-design.md) 全文。
- [ ] 讀 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md) 的 Team 信任邊界與 connector 段落。
- [ ] 讀 [ADR-0007](adr/0007-foundry-hosted-native-first.md) 的 toolbox 與 Team connector 段落。
- [ ] 讀 [Shared Context README](../src/fa-shared-context-poc/README.md) 的 Team MCP、toolbox、HITL、預熱與 dev 部署段落。
- [ ] 讀 [工作台 README](../src/fa-shared-context-poc/web_test_console/README.md) 的 Agent 設定、Team MCP 與批准卡段落。
- [ ] 執行任何 Foundry workflow 前，讀 Microsoft 官方 `microsoft-foundry` skill。若本機沒有該 skill，只可先讀官方檔案，不得把「已讀」寫成依賴檢查已完成。
- [ ] 執行任何 Azure CLI 命令前設定專案專屬 `AZURE_CONFIG_DIR`，每條命令明確指定 subscription。

完成條件是下一位 Agent 能指出目前程式入口、現有不變量、需要新建的資料形狀、每個 PR 的檔案邊界與需要重新取得的批准。

## 目前可相信的狀態

### Repository

- #741 合併 connector 第一版設計。
- #742 合併第二版設計。Teams 第一波，GitHub 第二波。
- #743 合併 dev-only `FA_TOOLBOX_CONSENT_PROBE`。正式預設仍關閉。
- #744 合併 Contoso 帳號、租戶與 P0b 批准更正。
- P0a 的程式基準是 `3c98f685`。dev API 與 Hosted 當時來自同一 build。
- 目前工作樹可能停在 #744 的 head commit，而不是 GitHub 產生的 merge commit。下一位 Agent 應從最新 `origin/main` 建立新工作階段，不在這個已合併分支繼續累積程式修改。

### P0a GitHub 探針

- dev connection 名稱是 `ghprobe-github`。
- Team toolbox `fa-team-team-one` 的 v4 有 `ghprobe_a` 與 `ghprobe_b`。
- `ghprobe_a` 只允許 `get_me`，批准是 `never`。
- `ghprobe_b` 只允許 `search_repositories`，批准是 `always`。
- 只有 Agent A 曾開 `FA_TOOLBOX_CONSENT_PROBE=1`。
- Agent A 的能力選擇曾暫時拿掉 WebIQ、Playwright，加入兩個 probe server。
- 真人 caller 與 Agent A 都在 `tools/list` 收到 JSON-RPC `-32006`。
- 同一 toolbox 內其他金鑰型 source 雖然成功，整份工具列表仍失敗。
- consent link 約十分鐘過期。舊連結不可重用。
- 尚未完成 GitHub 真人同意、Agent B、排程 caller、user caller 對照與撤銷後行為。

### P0b Teams 探針

- Entra app 名稱是 `fa-dev-workiq-connector`。
- client ID 是 `00000000-0000-0000-0000-000000000049`。
- Foundry connection 名稱是 `fa-dev-workiq`。
- 獨立 toolbox 名稱是 `fa-probe-workiq`，版本與 default 都是 `1`。
- toolbox ID 是 `toolbox_391db3e7d94adac2a12b40d9a020ac999834c932`。
- app redirect、Work IQ scope、connection 與 toolbox 已建立並讀回。
- client secret 有效期 30 天。secret 只進 Foundry，沒有本機副本。
- 真人基準的 initialize 與 initialized 成功。`tools/list` 回 `-32006 CONSENT_REQUIRED`。
- 尚未取得實際工具清單，尚未讀 Teams 內容，也尚未發送 Teams 訊息。

### 正確帳號與環境

- Entra tenant 是 Contoso `00000000-0000-0000-0000-000000000013`。
- 探針與 Teams owner 帳號是 `admin@contoso.onmicrosoft.com`。
- Azure subscription 是 `00000000-0000-0000-0000-000000000012`。
- `owner@contoso.com` 屬於另一個租戶，不能用於 FA connector 探針。
- Graph token 必須用指定 subscription 取得。不要使用未帶 subscription 的 `az rest`。

## 本機私有續作資料

以下檔案只供同一台機器上的接手 Agent 讀取。不要複製它們的 consent URL、token、secret 或原始回應到 repo、PR、一般對話或 log。

- P0a 續作筆記

  `%USERPROFILE%\.copilot\session-state\00000000-0000-0000-0000-00000000004a\files\p0a-resume.md`
- P0b 續作筆記

  `%USERPROFILE%\.copilot\session-state\00000000-0000-0000-0000-00000000004b\files\p0b-resume.md`
- 總協調續作筆記

  `%USERPROFILE%\.copilot\session-state\00000000-0000-0000-0000-00000000004c\files\coordinator-resume-20261009.md`
- P0a 原 Agent A selection

  P0a session files 下的 `select-00000000-before.json`
- P0b 去敏報告

  P0b session files 下的 `p0b-teams-probe.md`

若這些檔案不在接手環境，接手 Agent 只使用 repo 已有資料與雲端唯讀讀回，不猜原本內容，也不重建 secret。

## 核心資料形狀

### `ConnectorRef`

`ConnectorRef` 是 code-owned 的小型 registry。它不是 connector 目錄，也不保存使用者 token。

```text
ConnectorRef
  id                    teams | github
  display_name
  server_url
  authentication
    kind                custom_oauth2 | managed_oauth2
    connector_name      managed OAuth 才需要
    app_setting         custom OAuth 只指向部署設定，不保存 secret
    scopes
  entries
    read
      server_label
      allowed_tools
      default_approval  never
    write
      server_label
      allowed_tools
      default_approval  always
  revoke_url
  disclosure            工作台要顯示的資料範圍說明
```

工具說明、input schema 與參數仍以 `tools/list` 為準。registry 只保存 allowlist 與批准分類。新工具名不在 read 或 write 名單時，先不開放。

### `TeamConnectorState`

Team connector 的權威資料仍放在 Foundry。FA 不建第二份 token 或 connection 資料庫。

```text
TeamConnectorState
  connector_id
  connection_name       穩定，重新啟用不換名
  toolbox_name          fa-connector-{team}-{connector}（前綴和 Team MCP 的 `fa-team-` 不同，任何 Team ID 都拼不出別人的 connector toolbox）
  default_version
  enabled               來自 default version metadata
  read_approval
  write_approval
```

每個 OAuth connector 使用獨立 toolbox。該 toolbox 的 read 與 write entry 可共存，因為它們共用同一個 OAuth connection。停用時建立新版本，保留原工具但把 version metadata 設為 `fa_connector_enabled=false`，再 promote。重新啟用時建立 `true` 的新版本。這樣不需要空 toolbox、不需要刪除，也不需要在 FA Table 另存 enabled 狀態。

### `AgentConnectorNarrowing`

現有 `CapabilityCapture` 是手動選擇的白名單，最多 12 項，而且對話內只能縮小。不要把預設開啟的 connector 硬塞進這個結構。

```text
AgentConnectorNarrowing
  team_id
  agent_id
  disabled_connector_ids
  revision
  etag
```

新對話建立 `ConnectorCapture`。內容是當時 Team 已啟用的 connector，扣掉該 Agent 的 disabled 清單。既有對話不因新增 connector 擴權。Team 停用 connector 或 connector 版本失效時，下一個回合從 capture 縮掉。per-Agent 關閉只影響新對話。

### `ConnectorConsentPending`

consent 不是工具批准，也不是模型工具結果。它要有自己的 pending 形狀。

```text
ConnectorConsentPending
  pending_id
  connector_id
  toolbox_name
  connection_name
  turn
  consent_url
  expires_at
  attempt               只能是 1
  status                pending | resume | expired | failed
  original_call_digest  只有 tools/call 時存在
```

consent URL 只出現在工作台私有 pending response。它不進模型 context、公開 progress、tool output、一般 log、歷史摘要或 PR。若 `-32006` 發生在 `tools/list`，恢復動作是重新 bind 同一回合一次。若發生在 `tools/call`，恢復動作是以相同 tool call id 與相同 arguments 重送一次。第二次仍收到 `-32006` 就結束。

## 程式入口

### Team MCP 與 Foundry 管理

- `src/fa-shared-context-poc/shared_context/team_mcp_wire.py`
- `src/fa-shared-context-poc/shared_context/team_mcp.py`
- `src/fa-shared-context-poc/shared_context/team_mcp_api.py`
- `src/fa-shared-context-poc/shared_context/behavior_capabilities.py`
- `tests/shared_context/test_team_mcp_api.py`

### Native toolbox 與批准

- `src/fa-shared-context-poc/shared_context/native_toolbox.py`
- `src/fa-shared-context-poc/shared_context/native_capabilities.py`
- `src/fa-shared-context-poc/shared_context/native_assembly.py`
- `src/fa-shared-context-poc/shared_context/native_sdk.py`
- `src/fa-shared-context-poc/shared_context/native_hitl_wire.py`
- `src/fa-shared-context-poc/shared_context/native_hitl_runtime.py`
- `src/fa-shared-context-poc/shared_context/native_hitl.py`
- `src/fa-shared-context-poc/shared_context/behavior_assistant_boundary.py`
- `tests/shared_context/test_native_toolbox.py`
- `tests/shared_context/test_native_toolbox_approval.py`
- `tests/shared_context/test_native_hitl.py`

### Agent selection 與對話 capture

- `src/fa-shared-context-poc/shared_context/behavior_profile_selection.py`
- `src/fa-shared-context-poc/shared_context/behavior_capabilities.py`
- `src/fa-shared-context-poc/shared_context/behavior_native_capture.py`
- `src/fa-shared-context-poc/shared_context/behavior_work_policy.py`
- `tests/shared_context/test_conversation_profile_selection.py`
- `tests/shared_context/test_capability_capture.py`
- `tests/shared_context/test_native_capture.py`

### 工作台

- `src/fa-shared-context-poc/web_test_console/team_mcp.py`
- `src/fa-shared-context-poc/web_test_console/native_hitl.py`
- `src/fa-shared-context-poc/web_test_console/bridge.py`
- `src/fa-shared-context-poc/web_test_console/index.html`
- `tests/shared_context/test_web_test_console_team_mcp.py`
- `tests/shared_context/test_web_test_console_hitl.py`
- `tests/shared_context/test_web_test_console_workbench.cjs`

## 依賴順序

```text
P0 probe closure
  -> C1 connector domain and Foundry resources
  -> C2 connector capture and per-Agent narrowing
  -> C3 consent runtime and Connect card
  -> C4 Teams first wave
  -> D1 dev acceptance
  -> R1 stage release
  -> G1 GitHub second wave
```

P0 結論若顯示 app caller 無法使用 owner OAuth，停止 C1 到 G1。另開 user-caller delegation 設計，不在這個計畫內硬繞過。

## P0 收斂 GitHub 與 Work IQ 探針

**目標。** 用真實 dev 行為決定 OAuth token 綁定方式，並取得 Work IQ 實際工具與 Teams 路徑。這一階段不改產品程式。

**狀態（2026-10-10）。** 通過。app caller 可用 Team 主人完成的 OAuth，排程回合也可以；授權每個 Agent 各一份。結果與證據見 [connector-design.md P0 實測結果](connector-design.md#p0-results-1010)。探針本身要先修截斷連結（#746）才跑得完。

### 開始前

- [x] 從最新 `origin/main` 建立新的協調工作階段。
- [x] 讀三份私有續作筆記。
- [x] 用唯讀命令核對 dev API revision、Hosted Agent 版本、A 的 probe 旗標、toolbox default version、connection metadata 與 Agent A selection。
- [x] 對不上時停止寫入，只更新狀態差異，不照舊筆記盲目還原。
- [x] 重新取得 P0a 與 P0b 的 dev 雲端寫入批准。舊工作階段的批准只作歷史紀錄。
- [x] 讓 Neal 親自完成 OAuth 同意。Agent 不代登入、不代按同意。
- [x] 讓 Neal 指定一個無敏感內容的測試 chat 與一個測試 channel。

### P0a GitHub

- [x] 重開 8765 工作台並核對版本。不要假設舊 PID 或舊服務仍存在。
- [x] 在 Agent A 原 probe 對話重新送出 GitHub username 問題，產生新的 consent link。
- [x] Neal 以 Contoso admin 完成 Entra 步驟，以自己的 GitHub 帳號完成 GitHub 步驟。
- [x] Agent A 重問同一題。記錄是否成功與回傳帳號。
- [x] 只在取得新批准後，替 Agent B 開 probe flag 與兩個 probe selection。
- [x] Agent B 重問同一題。記錄是否需要第二次同意。
- [x] Agent B 呼叫 `ghprobe_b`。核對批准卡與拒絕路徑。
- [x] 用 dev 排程執行 Agent A 同一題。記錄排程 caller 能否使用同一授權。
- [x] 用指定 Contoso user token 直接打 toolbox。記錄 user caller 是否共用同意。
- [x] 請 Neal 在 GitHub 設定撤銷授權。再問一次並記錄重新連線行為。

### P0b Work IQ

- [x] 重新建立 MCP session 並執行 `tools/list`。
- [x] 若仍需要同意，把新的 URL 只存私有檔，交 Neal 用 Contoso admin 完成。
- [x] 重新列工具。保存去敏的工具名、description digest 與 input schema digest。
- [x] 將實際工具名和設計的 read、write 名單比較。未知工具保持關閉。
- [x] 只用 `search_paths` 與 `get_schema` 找 Teams chat 與 channel 路徑。
- [x] 只列 metadata。不要在 Neal 指定目標前讀內容。
- [x] 讀指定 chat 最新一則。
- [x] 讀指定 channel 一則。政策擋住時保留原錯誤並標示未通過。
- [ ] NOT_RUN（Neal 10-10 同意留到 C5）：取得新批准後，只在單一測試送出前開 Teams mutation policy。
- [ ] NOT_RUN（Neal 10-10 同意留到 C5）：發送一則固定文字。批准卡要顯示目標與全文。
- [ ] NOT_RUN（Neal 10-10 同意留到 C5）：讀回該訊息並核對 sender 是 Contoso admin。
- [ ] NOT_RUN（Neal 10-10 同意留到 C5）：再提出一次相同類型送出，由 Neal 拒絕。核對沒有新訊息。
- [ ] NOT_RUN（Neal 10-10 同意留到 C5）：關回 Teams mutation policy。

### P0 完成判定

- [x] 寫 `p0a-dev-probe.md` 去敏報告。改以 connector-design 的 P0 實測結果代替，不另開檔。
- [x] 寫 `p0b-teams-probe.md` 最終報告。同上。
- [x] 在 [connector-design.md](connector-design.md) 補實測矩陣。
- [x] 在 [progress.md](progress.md) 記錄版本、時間、caller、結果與限制。
- [x] 還原 Agent A 與 B 的 selection。
- [x] 關閉所有 `FA_TOOLBOX_CONSENT_PROBE` overlay。A v138、B v94 讀回旗標為 0。
- [x] 停用 `ghprobe_a` 與 `ghprobe_b`，保留 connection，不刪資源。
- [x] 核對本線啟動的瀏覽器、Playwright、server 與 watcher 都已停止。

**通過條件。** Agent A、Agent B、排程 caller 與 user caller 的同意關係有明確分類。Work IQ 已取得真實工具清單。指定 chat 讀取通過。channel 與寫入可各自是 PASS、BLOCKED 或 NOT_RUN，但不能用推論寫成通過。

**停止條件。** app caller 在 Neal 完成同意後仍無法使用 owner token，或同意無法被穩定綁定到單一 Team owner。此時停止後續實作，另寫 user-caller delegation ADR 提案。

## C1 建立 connector domain 與 Foundry 資源管理

**Depends on.** P0 通過。

**Files.**

- [x] 新增 `shared_context/connector_registry.py`。
- [x] 新增 `shared_context/team_connector_wire.py`。
- [x] 新增 `shared_context/team_connector.py`。
- [x] 新增 `shared_context/team_connector_api.py`。
- [x] 修改 `shared_context/behavior_capabilities.py`。
- [x] 修改 `shared_context/behavior_server.py` 或現有 API route 安裝入口。
- [x] 新增 `tests/shared_context/test_team_connector_api.py`。

**Build.**

- [x] 實作 code-owned `ConnectorRef` registry。第一版只放 Teams，GitHub 留到 G1。
- [x] 實作 `fa-connector-{team}-{connector}` 的穩定 toolbox 名稱。
- [x] 實作穩定 connection 名稱。相同 Team 與 connector 重新送出不能換名。
- [x] 依 P0b 真實 ARM readback 實作 custom OAuth2 connection PUT。
- [x] client secret 只存在 request memory 與 Foundry ARM PUT。任何 model dump、response、metadata、digest 與 log 都不能含 secret。
- [x] 實作 connector toolbox version metadata。至少包含 Team、connector、enabled、request ID 與 command digest。
- [x] 實作 enable、approval change、disable 與 re-enable。每次都建立 immutable version，再 promote。
- [x] 保留失去 ACK 後以相同 request ID reconcile 的行為。
- [x] 不刪 connection、toolbox 或 version。
- [x] 只有 Team 真人主人可讀寫。
- [x] connector 使用自己的數量上限。兩個內部 entry 算一個 connector，不占 generic Team MCP 的八個 server 名額。
- [x] 未列在 registry 的 connector、tool、auth shape 與 server URL 一律拒絕。

**You see.**

- [ ] API 能列出 Teams connector 的 added、enabled、approval、connection 與 toolbox version，不回 token 或 secret。
- [ ] disable 後新回合的 connector capture 不再含它（C2 起 connector 不在 `runtime_grants`）。
- [ ] re-enable 使用同一 connection 名稱。

**Verify, unit.**

- [x] 同一 request ID 只建立一個 version。
- [x] create 後失去 ACK，再送相同 request ID 不重建 connection 或 version。
- [x] 不同 Team 不可讀寫彼此 OAuth connector。
- [x] secret 不在 response、metadata、digest、log 或 fake Foundry readback。
- [x] disable 與 re-enable 不需要空 toolbox，也不刪資源。
- [x] 只有 allowlisted 工具進入 entry。
- [x] 執行 `python -X utf8 -m pytest tests\shared_context\test_team_connector_api.py tests\shared_context\test_team_mcp_api.py -q`。

**Verify, live.**

- [ ] 在 dev 建一條不含真人資料的測試 connector version，讀回 default 與 metadata。
- [ ] 用相同 request ID 重送一次，讀回仍是同一 version。
- [ ] disable 後新回合 capture 不含該 connector。
- [ ] re-enable 後新回合 capture 使用同一 connection。

**Review gate.** 無工作台互動變更。完成兩軸 review 即可。

**Merge.**

- [x] exact head 兩軸 0 P1、0 P2。
- [x] GitHub 顯示 CLEAN。
- [x] 負責工作階段合併。

## C2 加入 connector capture 與 per-Agent narrowing

**Depends on.** C1。

**Files.**

- [x] 新增 `shared_context/team_connector_narrowing.py` 與 wire model。
- [x] 修改 `shared_context/behavior_native_capture.py`。
- [ ] 修改 `shared_context/behavior_work_policy.py`。 不需要：舊 work 路徑本來就拒絕 toolbox grant（`behavior_assistant_hosted.py`），connector 只走 native capture。
- [x] 修改 turn capture wire 與 history validation。
- [x] 修改 Agent 設定 API。
- [x] 新增 narrowing、capture 與 history 測試。

**Build.**

- [x] 保留現有 `CapabilityCapture` 的白名單與最多 12 項規則。
- [x] 新增獨立 `ConnectorCapture`，不占手動 capability 名額。
- [x] `ConnectorCapture` 只在新對話建立時加入當時已啟用且未被該 Agent 關閉的 connector。
- [x] 對話內 connector capture 只能縮小，不能因 Team 後來新增 connector 而變大。
- [x] Team disable 在下一個回合讓所有對話移除該 connector。
- [x] per-Agent narrowing 以 CAS、revision 與原 owner request 保存。
- [x] per-Agent 關閉只影響新對話。
- [x] 未知或已刪除的 connector ID 明確拒絕，不靜默忽略。
- [x] history reader、migration allowlist 與 current runtime validation 同步支援新 record kind。

**You see.**

- [ ] Team 加入 Teams 後，現有 Agent 的新對話預設看得到 Teams。
- [ ] 在 Agent 齒輪關閉 Teams 後，該 Agent 下一個新對話看不到 Teams。
- [ ] 同 Team 其他 Agent 不受影響。
- [ ] 舊對話不因新增 Teams 自動取得新工具。

**Verify, unit.**

- [x] default-on、per-Agent off、Team disable、re-enable、對話不增權與 history rebuild 都有 literal expected assertions。
- [x] 執行 `python -X utf8 -m pytest tests\shared_context\test_capability_capture.py tests\shared_context\test_native_capture.py tests\shared_context\test_conversation_profile_selection.py tests\shared_context\test_team_connector_narrowing.py -q`。

**Verify, live.**

- [ ] dev 使用兩個 Agent。Team enable 後兩者的新對話都看得到 connector。
- [ ] 只關閉 Agent A。A 的新對話看不到，B 的新對話仍看得到。
- [ ] Team disable 後 A、B 的下一個回合都不能呼叫。

**Review gate.** 行為會改變。合併前在工作台展示 Team enable、Agent A off、Agent B on 三個畫面。

**Merge.**

- [x] exact head 兩軸 0 P1、0 P2。
- [ ] 工作台畫面由 Neal 或總協調確認。
- [x] GitHub 顯示 CLEAN。
- [x] 負責工作階段合併。

## C3 把 `-32006` 做成正式 Connect 卡

**Depends on.** C2。

**Files.**

- [x] 修改 `shared_context/native_toolbox.py`。
- [x] 修改 `shared_context/native_assembly.py`、`native_capabilities.py` 或實際 bind boundary。
- [x] 新增 connector consent wire、API 與 runtime。
- [x] 修改 `shared_context/native_hitl_runtime.py` 或沿用其效果保留與等待機制。
- [x] 修改 `shared_context/behavior_assistant_boundary.py`，避免 consent URL 進模型。
- [x] 修改 `web_test_console/native_hitl.py`、`bridge.py` 與 `index.html`。
- [x] 修改對應 toolbox、HITL 與工作台測試。

**Build.**

- [x] 把 `_ConsentRequired` 從 dev probe 文字改為正式、結構化的內部結果。
- [x] `tools/list` 收到 `-32006` 時，在模型啟動前建立 `ConnectorConsentPending`。
- [x] `tools/call` 收到 `-32006` 時，保留原 call id 與 arguments digest。
- [x] 工作台 Connect 卡顯示 connector 名稱、資料範圍說明、外部連線按鈕與「我連好了，繼續」。
- [x] consent URL 只出現在私有 pending response。
- [x] 「我連好了，繼續」只消耗一次 resume grant。
- [x] list-time resume 重新 bind 一次。call-time resume 重送原 call 一次。
- [x] 第二次仍是 `-32006` 時結束，不再迴圈。
- [x] 過期、拒絕、停止與原回合不再 current 時，不恢復。
- [x] 其他 toolbox 與本機能力不因一個 connector 未同意而消失。
- [x] 正式路徑完成後移除 `FA_TOOLBOX_CONSENT_PROBE`、`TOOLBOX_CONSENT_PROBE` 與 probe-only placeholder 行為。

**You see.**

- [ ] 第一次要求 Teams 時，模型不會把 consent URL 說出來，工作台會直接出 Connect 卡。
- [ ] 按「我連好了，繼續」後，原訊息自動接續，不需要再打一遍。
- [ ] 連線仍未完成時，畫面只說尚未連好，不重試第二次。

**Verify, unit.**

- [x] list-time 與 call-time `-32006` 各有測試。
- [x] URL 不在 caplog、model tool failure、progress、history summary 或一般 API response。
- [x] resume 一次，重複按鈕不會產生第二次外部呼叫。
- [x] expiry、turn closed 與 action changed 都停止。
- [x] 執行 `python -X utf8 -m pytest tests\shared_context\test_native_toolbox.py tests\shared_context\test_native_toolbox_approval.py tests\shared_context\test_native_hitl.py tests\shared_context\test_web_test_console_hitl.py -q`。
- [x] 執行 `node --test tests\shared_context\test_web_test_console_workbench.cjs`。

**Verify, live.**

- [ ] dev 以未同意的 GitHub probe 驗證 list-time Connect 卡。
- [ ] 完成同意後按一次繼續，原回合取得 GitHub username。
- [ ] 使用已撤銷授權再次觸發 Connect 卡。
- [ ] 驗證一般 WebIQ、Playwright 與本機工具仍可用。

**Review gate.** 行為會改變。合併前提供 Connect 卡、成功接續、仍未連好與過期四張畫面。

C3 實作註記（2026-10-10，未部署）：資料形狀與 API 見 [Shared Context README](../src/fa-shared-context-poc/README.md#connector-connect-card)。
`connection_name` 需要 API 的 project scope，Hosted 不知道，所以卡上不放。撤銷（`-32007`／`-32602` 加
「Failed to extract exchanged token」）用同一種卡、`need=reconnect`、沒有 URL，指向 `revoke_url`；只有 owner 按繼續才重試一次。
2026-10-11：非真人來源、Teams relay（`tr-`）回合與 owner 已在同一對話按過「先不用」的 connector 不再開卡，直接綁替身工具（Neal 2026-10-11）。
單元測試另含 `tests/shared_context/test_connector_consent.py` 與 `test_web_test_console_consents.py`。

**Merge.**

- [x] exact head 兩軸 0 P1、0 P2。
- [ ] 工作台畫面由 Neal 或總協調確認。
- [x] GitHub 顯示 CLEAN。
- [x] 負責工作階段合併。

## C4 建立 Connectors 設定頁

**Depends on.** C3。

**Files.**

- [x] 新增 `web_test_console/team_connectors.py`。
- [x] 修改 `web_test_console/bridge.py`。
- [x] 修改 `web_test_console/index.html`。
- [x] 修改 [工作台 README](../src/fa-shared-context-poc/web_test_console/README.md)。
- [x] 新增 `test_web_test_console_team_connectors.py` 與 workbench 行為測試。

**Build.**

- [x] 在「Agent Team 設定」新增 Connectors 分頁。
- [x] Teams 卡片顯示資料範圍、Added、Connected unknown、read approval、write approval、停用與重新啟用。
- [x] Added 只表示 Team 已加入，不表示 OAuth token 現在可用。
- [x] 不做假的 Connected 綠燈。只有實際呼叫成功能證明當次可用。
- [x] approval 變更沿 immutable toolbox version 與 request ID reconcile。
- [x] Team 主人以外只能看不到或得到 owner-only 說明。
- [x] Agent 齒輪的 MCP 區加入 connector 開關。這個開關寫 per-Agent narrowing。
- [x] 變更 connector、approval 或 narrowing 都只送一次。結果不明時只允許用原 request ID reconcile。
- [x] generic Team MCP 頁保持原功能，不混入 OAuth connector 的 raw server 表單。

**You see.**

- [ ] Team 主人能在一個頁面加入 Teams、調整讀寫批准、停用與重新啟用。
- [ ] 每位 Agent 的齒輪能單獨關閉 Teams。
- [ ] 畫面不顯示 client ID、secret、token、raw consent URL 或 connection ARM body。

**Verify, unit.**

- [x] owner-only、read-only、stale version、lost ACK、retry same ID、disable、re-enable 與 Agent narrowing 都有測試。
- [x] 執行 `python -X utf8 -m pytest tests\shared_context\test_web_test_console_team_connectors.py tests\shared_context\test_web_test_console_team_mcp.py tests\shared_context\test_web_test_console_agent_policy.py -q`。
- [x] 執行 `node --test tests\shared_context\test_web_test_console_workbench.cjs`。

**Verify, live.**

- [ ] 8765 工作台在桌面寬度完成 Teams add、approval change、disable、re-enable。
- [ ] 390×844 完成讀取、展開說明與 Agent 開關。
- [ ] 關閉 Team 設定後焦點回到原本齒輪。
- [ ] 切換 Agent 後重新讀取該 Agent narrowing。

**Review gate.** 行為會改變。合併前提供桌面與 390×844 畫面，並由 Neal 或總協調確認文字。

C4 實作註記（2026-10-10，未部署）：bridge 給頁面的資料形狀見[工作台 README](../src/fa-shared-context-poc/web_test_console/README.md#connectors-settings)。
頁面不收 toolbox／connection 名稱。加入的結果不明時，頁面沒有保存用戶端密碼，要再輸入一次才能用原編號重送。
「You see」與 live 項目要 8765 實機畫面，仍未勾。

**Merge.**

- [x] exact head 兩軸 0 P1、0 P2。
- [ ] 工作台畫面確認完成。
- [x] GitHub 顯示 CLEAN。
- [x] 負責工作階段合併。

## C5 完成 Microsoft Teams 第一波

**Depends on.** C4。

**Files.**

- [x] 在 `connector_registry.py` 完成 Teams `ConnectorRef`。
- [x] 修改 Teams 專用批准預覽或參數顯示 helper。
- [x] 修改 prompt guidance，明確禁止 connector 需要重連時改用 VM browser 繞過。
- [x] 新增 Teams registry、approval preview 與 unknown outcome 測試。
- [x] 更新 Shared Context README、工作台 README 與 [connector-design.md](connector-design.md)。（design 的資料形狀沒變，沒改。）

**Build.**

- [x] 使用 P0b 真實 `tools/list` 結果固定 read、write allowlist。
- [x] 未知工具不自動加入。
- [x] read entry 預設 `never`。write entry 預設 `always`。
- [x] `ask` 保持 write，因為 peer agent 可能產生副作用。
- [x] Teams connector 卡片明示 Work IQ 可讀 Microsoft 365 郵件與檔案，不只 Teams。
- [ ] Teams 發送批准卡顯示可理解的 chat 或 channel 名稱、路徑與完整訊息。
- [ ] 只有在無法安全解析名稱時才退回顯示 opaque ID，並明示是 ID。（第一波一律顯示路徑並標「路徑／ID」，沒有解析名稱。）
- [ ] 成功送出後讀回核對 sender 與內容。（屬 live 驗收，程式不自動讀回。）
- [x] timeout、5xx、連線中斷或 malformed response 都保留 UNKNOWN，不自動重送。
- [x] 拒絕批准不呼叫 Work IQ。
- [x] 排程回合遇到 consent 或 write approval 時照 600 秒等待，逾時不執行。

**You see.**

- [ ] 使用者可讀指定 Teams chat 與 channel。
- [ ] 發送前一定看到完整內容。
- [ ] 拒絕後沒有訊息。
- [ ] 結果不明時畫面明說不確定，沒有「再試一次」自動按鈕。

**Verify, unit.**

- [x] read、write、ask、未知工具、批准拒絕、unknown outcome 與排程逾時都有測試。
- [x] 執行 connector、toolbox、HITL 與工作台的 targeted tests。

**Verify, live.**

- [x] dev 讀指定 chat 最新一則。（10-10，給 chat 連結時）
- [ ] dev 讀指定 channel 一則，或保存政策阻擋證據。
- [ ] dev 允許一次固定訊息並讀回。
- [ ] dev 拒絕一次並核對沒有新訊息。
- [ ] dev 排程讀取一次。
- [ ] 撤銷 Work IQ token 後重新出 Connect 卡。

**Review gate.** 行為會改變。合併前提供 read、allow send、deny send 與 reconnect 畫面。不要在畫面中曝光真實私人訊息。

C5 實作註記（2026-10-10，未部署）：說明見 [Shared Context README](../src/fa-shared-context-poc/README.md#connector-teams-writes)。
registry 名單與「未知工具不開放」是 C1 完成的，C5 核對後沒改，測試在 `test_team_connector_api.py`。批准卡不查名稱，
只把 `parentUrl` 等路徑標成「路徑／ID」，並完整顯示 `jsonBody`；所以「顯示可理解的 chat 或 channel 名稱」仍未勾。
結果不明改回 `NATIVE_TOOLBOX_OUTCOME_UNKNOWN`，不重送。按「繼續」後立刻停止回合時，停止優先（C3 P3）。
排程逾時的測試用 Agent 間回合代替，兩者都走 `human_origin=false`。拒絕不呼叫的測試沿用
`test_an_always_toolbox_tool_is_never_called_before_the_owner_approves_that_exact_call`。工作台 README 已更新；
`connector-design.md` 的資料形狀沒變，所以沒改。

**Merge.**

- [x] exact head 兩軸 0 P1、0 P2。
- [ ] dev live 結果已去敏保存。
- [x] GitHub 顯示 CLEAN。
- [x] 負責工作階段合併。

## D1 完成 dev 驗收與文件同步

**Depends on.** C5。

**Files.**

- [ ] 更新 [connector-design.md](connector-design.md) 的實作狀態與最終資料形狀。
- [x] 更新 [progress.md](progress.md)。
- [x] 更新 [CURRENT-STATE](status/CURRENT-STATE.md)。
- [ ] 更新 Shared Context README 與工作台 README 的操作流程。
- [x] 建立去敏 dev 測試報告。

**Build.**

- [x] 將 P0、C1 到 C5 的 exact head、測試命令、dev revision 與 live 結果集中成一份 dev 報告。
- [x] 區分本機測試、dev Hosted、真人 OAuth、Teams 讀取與 Teams 發送。
- [x] 列出尚未測的多 Team、第二位使用者與無 Copilot license 使用者。
- [x] 不把未執行項目寫成通過。
- [x] 核對 probe flag 已完全關閉。
- [ ] 核對 probe selection、toolbox entry、mutation policy 與本線背景程序已收尾。

**You see.**

- [ ] 新 Agent 只讀 dev 報告就能知道哪些行為已通過、哪些未測、stage release 會改什麼。

**Verify, unit.**

- [ ] 文件連結、路徑、PR、commit、環境與命令都存在。
- [x] 文件沒有 consent URL、token、secret、cookie 或私人 Teams 內容。
- [x] docs-only，不重跑無關完整測試。

**Verify, live.**

- [x] 讀回 dev API 與 Hosted 同一 build。
- [ ] 讀回 connector toolbox default、connection metadata 與 Agent capture。
- [x] 工作台以 Contoso admin 完成一次完整 Teams read 流程。（10-10）

**Review gate.** docs-only，不需 Opus 兩軸 review。

D1 註記（2026-10-10）：dev 報告在 [progress](progress.md#connector-m1-dev-1010)。dev 已部署 main `82e02283`；加入 Teams、真人 OAuth、讀取、送出與「工作台以 Contoso admin 完成一次完整 Teams read 流程」要 Neal 貼 secret 並同意，仍未勾。

**Merge.**

- [ ] 文件與 dev 狀態一致。
- [ ] GitHub 顯示 CLEAN。
- [ ] 負責工作階段合併。

## R1 發布 Teams 到 stage

**Depends on.** D1。

**Files.**

- [ ] 只在 release PR 修改 `deploy/stage/release.json` 與必要的 stage 設定。
- [ ] PR body 附完整 dev 測試報告與變更清單。

**Build.**

- [ ] stage 保持單一 Team。
- [ ] API 與 Hosted 使用同一 main build。
- [ ] Teams OAuth app、connection 與 toolbox 的 stage 建立步驟明列，但不把 secret 寫入 repo。
- [ ] stage release PR 不附退版方式，也不做退版演練。
- [ ] 未取得 Neal 對該次 stage release 的批准前，不部署。

**You see.**

- [ ] Neal 能從 release PR 判斷功能、費用、租戶影響、dev 證據與尚未測範圍。

**Verify, unit.**

- [ ] release 設定檔 schema 與既有部署檢查通過。

**Verify, live.**

- [ ] Neal 合併後，部署 workflow 成功。
- [ ] 讀回 API revision、Hosted versions、connector flags 與 toolbox default。
- [ ] 由 Neal 完成 stage OAuth 同意。
- [ ] stage 完成一次 Teams read、一次 allow send、一次 deny send。

**Review gate.** Neal 決定是否合併 release PR。

**Merge.**

- [ ] Neal 明確批准並合併。
- [ ] stage live 結果寫入 progress。

## G1 建立 GitHub 第二波

**Depends on.** R1 完成且 Teams 使用結果沒有要求重改 connector core。

**Files.**

- [ ] 在 `connector_registry.py` 新增 GitHub `ConnectorRef`。
- [ ] 新增 GitHub 工具分類測試。
- [ ] 更新 Connectors 頁與說明。
- [ ] 更新設計、progress 與 README。

**Build.**

- [ ] 使用 `foundrygithubmcp` managed OAuth。
- [ ] 以當時實際 `tools/list` 分類 read 與 write。
- [ ] 不沿用 P0a 的兩個 probe server 名稱。
- [ ] GitHub 寫入預設 `always`。
- [ ] 撤銷連到 GitHub Applications 設定。
- [ ] Teams 與 GitHub 使用不同 toolbox 與 connection。
- [ ] GitHub 未同意不能讓 Teams 工具消失，反之亦同。

**You see.**

- [ ] Team 主人能加入 GitHub。
- [ ] 同一套 Connect 卡與 per-Agent 關閉適用 GitHub。
- [ ] GitHub 未連線不影響 Teams。

**Verify, unit.**

- [ ] registry、tool classification、separate toolbox、consent、approval 與 revoke 文字都有 targeted tests。

**Verify, live.**

- [ ] dev `get_me` 成功。
- [ ] 同 Team 第二 Agent 依 P0a 結論共享或個別同意。
- [ ] 一個 read tool 通過。
- [ ] 一個 write tool 完成 allow 與 deny。
- [ ] 撤銷後重新出 Connect 卡。
- [ ] Teams 在整個流程中仍可用。

**Review gate.** 行為會改變。合併前提供 GitHub add、connect、read、write approval 與 revoke 畫面。

**Merge.**

- [ ] exact head 兩軸 0 P1、0 P2。
- [ ] GitHub 顯示 CLEAN。
- [ ] 負責工作階段合併。
- [ ] 另開 stage release PR，由 Neal 決定。

## 驗證矩陣

| 層級 | 必須證明 | 不能拿來代替 |
|---|---|---|
| Wire 與 unit | 型別、CAS、request replay、secret 不外洩、capture 不增權 | 真 Foundry OAuth |
| Fake Foundry | immutable version、promote、lost ACK、同 ID reconcile | 真 toolbox 或真 Work IQ |
| dev toolbox | `-32006`、caller 綁定、實際工具清單 | Teams 訊息真的讀寫 |
| dev Hosted | Agent A、Agent B、排程回合、Connect 卡、批准卡 | stage |
| dev Teams | 指定 chat／channel 讀取、allow send、deny send | 多 Team 隔離 |
| stage | 真 stage 身分、真 OAuth、真工作台 | dev 報告 |

## 最小效能與穩定性檢查

本計畫不增加商業級 SLO 或長時間 burn-in。只核對會造成明顯變慢或重複工作的路徑。

- [ ] 已連線的 warm 回合，每個 connector toolbox 不重複 `initialize` 或 `tools/list`。
- [ ] consent resume 不產生第二個 model turn 或第二個外部 write。
- [ ] per-Agent narrowing 不讓每次 turn 掃描全 Team 歷史。
- [ ] connector 未使用時，不增加模型前的 OAuth 網路呼叫。
- [ ] dev 記錄 disconnected、connected warm、connected cold 三種第一個可見狀態時間，只作回歸比較，不宣稱新 SLO。

## 雲端與真人批准矩陣

| 動作 | 新 Agent 可以直接做 | 需要 Neal 新批准或親自操作 |
|---|---|---|
| 讀 repo、讀私有續作筆記、跑本機測試 | 可以 | 不需要 |
| Azure 與 Foundry 唯讀核對 | 可以，使用隔離帳號與指定 subscription | 不需要 |
| 重新產生 consent link | 不可以 | 需要涵蓋該次 dev 探針的批准 |
| GitHub OAuth 同意 | 不可以 | Neal 親自操作 |
| Work IQ OAuth 同意 | 不可以 | Neal 以 Contoso admin 親自操作 |
| 暫時改 Agent B flag 或 selection | 不可以 | 需要 dev 雲端寫入批准 |
| 開 Teams mutation policy | 不可以 | 需要租戶設定批准，只限單一測試 |
| 發送 Teams 測試訊息 | 不可以 | 需要指定目標與固定文字的批准 |
| commit、push、開 PR | 不可以 | 需要 Git 操作批准 |
| 合併一般程式 PR | 兩軸清零且 CLEAN 後依專案政策執行 | 不需 Neal 逐一批准 |
| 建立或合併 stage release PR | 不可以自行合併 | 每次由 Neal 決定 |
| 刪除 app、connection、toolbox、版本或資料 | 不可以 | 需要精確刪除批准 |

## 已拒絕的方案

1. FA 自建 OAuth broker。它會讓 FA 自己保存、refresh 與隔離 token，重做 Foundry 已有能力。
2. 把 token 或靜態 header 放進 Agent VM。它會破壞 ADR-0006 的秘密邊界。
3. 用 legacy `mcp_TeamsServer` 做正式主線。Microsoft 不建議新專案使用。
4. 自建 Graph OpenAPI Teams connector。它會重做 Work IQ 已提供的路徑與工具發現。
5. 把多個 OAuth connector 放進同一 toolbox。P0a 已證明一個未同意 source 會讓整份 `tools/list` 失敗。
6. 把 connector 預設開啟硬塞進現有 12 項 capability whitelist。它會改壞既有 capture 不增權規則，也會讓 internal read、write entry 被當成兩個產品名額。
7. 用假 Connected 狀態。只有 Foundry 知道 token 是否仍有效，設定頁只能顯示 Added，實際呼叫才能證明當次可用。
8. 自動重送 Teams write。Teams 發送不是 idempotent，未知結果必須保留 UNKNOWN。

## 主要風險與處置

| 風險 | 最早處理階段 | 處置 |
|---|---|---|
| app caller 無法使用 owner OAuth | P0 | 停止產品實作，另寫 user-caller delegation 設計 |
| OAuth connector 互相拖垮工具列表 | C1 | 每個 connector 獨立 toolbox |
| disable 最後一個 entry 需要空 toolbox | C1 | 用 version metadata 保存 enabled，不建立空版本 |
| 預設全 Agent 可用會破壞 capability whitelist | C2 | 獨立 `ConnectorCapture` 與 per-Agent narrowing |
| consent URL 被模型或 log 看見 | C3 | 結構化 pending，只經工作台私有 response |
| resume 造成重送 | C3 | list-time 重 bind 一次，call-time 相同 call id 重送一次 |
| Work IQ 實際能讀 M365 其他資料 | C4、C5 | UI 明示資料範圍，工具 allowlist，未知工具關閉 |
| Teams send 結果不明 | C5 | 不重送，保留 call id 與 UNKNOWN |
| project 層 toolbox 無多 Team ACL | 全程 | P1、P2 只在單一 Team 環境啟用 |
| Work IQ 計費不透明 | P0、D1 | dev 指定小範圍，保存實際用量，無意外迴圈 |
| custom OAuth secret 到期 | D1、R1 | 部署文件列到期與人工輪替，不把 secret 放 repo |

## Azure Well-Architected Framework 對照

| 支柱 | 本計畫的具體做法 |
|---|---|
| 可靠性 | immutable toolbox version、同 request ID reconcile、unknown write 不重送、Connect resume 最多一次 |
| 安全性 | token 留 Foundry、secret 不進 VM 或模型、Team owner 管理、單一 Team 限制、consent URL 不進 log |
| 成本最佳化 | 優先 Work IQ 與 Foundry connector，不建自有 broker；探針只讀指定資料與單一發送；不做 burn-in |
| 營運卓越 | 每個 PR 有 exact head、targeted tests、dev live 證據、去敏報告與明確收尾 |
| 效能效率 | 每個 connector 獨立快取 toolbox session；已連線 warm 回合不重複列工具；connector 未使用時不做 OAuth 呼叫 |

## 接手 Agent 的第一則執行提示

以下文字可直接交給新的總負責 Agent。它是工作指示，不是雲端操作批准。

```text
接手 FA connector 計畫。先讀 AGENTS.md、docs/status/CURRENT-STATE.md、docs/CONVENTIONS.md、docs/connector-design.md、docs/connector-implementation-handoff.md，並讀 handoff 文件列出的三份本機續作筆記。先做唯讀核對，確認 origin/main、dev revision、P0a/P0b resources、probe flags、toolbox default、Agent A selection 與背景程序。不要重建 secret，不要使用舊 consent URL，不要做雲端寫入、登入、commit、push 或部署。

你的第一個交付是 P0 現況核對表。每個項目標 SAME、CHANGED 或 UNKNOWN，附實際讀回來源。接著依 handoff 的批准矩陣，把 P0a 與 P0b 需要 Neal 新批准及親自操作的內容整理成白話問題。取得批准後才完成 P0。P0 顯示 app caller 可用 owner OAuth 才開始 C1。

實作逐 PR 進行。每個 PR 只做 handoff 指定的一個切片，跑 targeted tests 與必要 dev live。review prompt 原文附上最小機制原則，只回報會造成資料損失、重送、secret 外洩或明顯錯誤的 P1/P2。一般程式 PR 兩軸清零且 GitHub CLEAN 後由該工作階段合併。stage release PR 每次交 Neal 決定。
```

## 計畫完成條件

- [ ] P0 caller 綁定結論明確。
- [ ] Teams connector 已在 dev 完成 read、allow send、deny send 與 reconnect。
- [ ] connector 預設所有 Agent 可用，單一 Agent 可關閉。
- [ ] consent URL、token、secret 與私人內容沒有進 repo、模型或一般 log。
- [ ] Teams write UNKNOWN 不重送。
- [ ] dev 報告完整。
- [ ] stage release 經 Neal 決定並完成 live 驗證。
- [ ] GitHub 第二波重用同一核心，不影響 Teams。
- [ ] connector 設計、README、progress 與 CURRENT-STATE 同步。

## 官方來源

以下來源是本計畫實際使用的官方資料。執行時要重新核對適用版本與頁面更新日期。

- [Microsoft Foundry tool authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/tool-authentication)
- [Microsoft Foundry MCP authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication)
- [Microsoft Foundry hosted agent toolbox](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent)
- [Microsoft Foundry MCP tools 與 managed OAuth connectors](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol)
- [Microsoft Foundry Work IQ tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/work-iq)
- [Work IQ Foundry quickstart](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/quickstart/foundry)
- [Work IQ MCP tool reference](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference)
- [Work IQ policy governance](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/policy-governance-mcp)
- [Work IQ permissions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/permissions)
- [Work IQ usage-based billing](https://learn.microsoft.com/en-us/microsoft-365/copilot/usage-based-billing-overview-copilot-credits)
- [Microsoft Graph send chat message](https://learn.microsoft.com/en-us/graph/api/chat-post-messages?view=graph-rest-1.0)
- [Microsoft Graph send channel message](https://learn.microsoft.com/en-us/graph/api/channel-post-messages?view=graph-rest-1.0)
- [Microsoft Entra application permission management](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/manage-application-permissions)
- [Microsoft Azure Well-Architected Framework](https://learn.microsoft.com/en-us/azure/well-architected/)

## Verified against

- 程式基準：目前工作樹 `7d1f2097`，#744 已於 GitHub 合併；P0a dev 程式基準 `3c98f685`。
- 已讀程式：`team_mcp_wire.py`、`team_mcp.py`、`behavior_capabilities.py`、`behavior_profile_selection.py`、`native_toolbox.py`、`native_hitl_wire.py`、`native_hitl_runtime.py`、工作台 `team_mcp.py` 與 `index.html`。
- 已讀測試：`test_team_mcp_api.py`、`test_web_test_console_team_mcp.py`、`test_native_toolbox.py`、`test_native_toolbox_approval.py`。
- 已讀私有交接：P0a、P0b 與總協調續作筆記。
- 實際驗證：本次只建立文件，未執行 connector、OAuth、Teams、GitHub、Azure 寫入、部署或產品測試。
- 限制：P0a 與 P0b 真人同意後的行為尚未驗證。任何 caller 綁定、Teams channel、write policy 與實際費用結論都必須由 P0 實測更新。
