# Team toolbox 的 skill：讓助理用到 Foundry toolbox 上掛的 skill

狀態：**已決定（設計方向，非部署授權）**｜提出日期：2026-11-26 +08｜第 2 版｜Neal 2026-11-26 要求「設計 toolbox skill 支援」，同日決定採 A，見「Neal 的決定」。本文只是設計，尚未實作，也不授權部署或雲端寫入。

標示：[原始碼] 是讀過的本 repo 程式（基準見文末）；[SDK] 是讀過的固定版本 SDK 原始碼；[文件] 是官方文件明寫；[stage 實測] 是 2026-11-26 對 stage 的唯讀呼叫結果；[推論] 是依前述推得、尚未實測。

## 背景

2026-11-26 Neal 在入口網站把 Foundry skill `state-change-authorization-69a08d986ce49282` 掛到 stage Team 的 toolbox `fa-team-team-stage`，stage 的助理讀不到。原因有三層：

1. **預設版本沒變。** 入口網站在 09:42 存了第 6 版（第 5 版＋skill）與第 7 版（再加 `toolbox_search`），兩版都沒有設成預設；`default_version` 仍是第 5 版，沒有 skill [stage 實測]。FA 擷取授權時把 Team toolbox 釘在當下的預設版本（`team_mcp.py:557-579` 的 `team_grants`），每回合照這個版本連線（`native_toolbox.py:11-14`）[原始碼]；當天兩支助理的 App Insights 都只有 `tools/list fa-team-team-stage:5` [stage 實測]。
2. **FA 不讀 skill。** FA 的 toolbox 連線只送 `initialize`、`tools/list`、`tools/call`（`native_toolbox.py:448`、`:499`、`:511`）；`src` 下沒有 `skills/list`、`resources/read` 或 `skill://` [原始碼]。
3. **FA 改 Team MCP 時會把 skill 丟掉。** `team_mcp.py:154-168` 的 `read_default_version` 只取預設版本的 `tools` 與 `metadata`，`:196-204` 的 `create_version` 只送 `metadata`、`tools`，`description` 固定寫成 "FA Team MCP" [原始碼]。第 6、7 版的 skill 在頂層 `skills` 欄位，`toolbox_search` 則是 `tools` 陣列裡的一項 [stage 實測]。新版本是從**預設版本**改出來的（`team_mcp.py:534`）[原始碼]。現在預設是第 5 版，所以在工作台改一次 Team MCP，第 6、7 版加的 skill 和 `toolbox_search` 都不會進新版本。等預設改成第 7 版後再改，會保留 `toolbox_search`、但 skill 還是會掉 [推論]。頂層的 `policies`（內容安全設定 `rai_config`）也一樣會掉 [SDK 欄位＋原始碼]；stage 第 6、7 版沒有設 `policies` [stage 實測]。

### toolbox 怎麼提供 skill

官方做法 [文件]：

- skill 存在專案的 Skills API（`{project}/skills`，`Foundry-Features: Skills=V1Preview`，preview）。每次修改產生新的不可變版本，skill 本身有 `default_version`。
- toolbox 版本的 `skills` 陣列放 `skill_reference`。**沒寫 `version` 就跟著 skill 的 `default_version` 走**，寫了才釘在固定版本。
- 相容的 MCP client 走 MCP Skills extension（`io.modelcontextprotocol/skills`）：確認 server 宣告這個 extension → `skills/list`／`skills/get` → 必要的使用者批准後用 `resources/read` 讀 `SKILL.md`，其他檔案需要時才讀。有檔案清單的項目，client 要核對大小與 digest。「只讀資源不代表啟用 skill」。
- Hosted client 要用部署後的身分驗證讀得到；本機或探索成功不代表部署後讀得到。
- 附掛的 skill 必須在同一個專案。

stage 第 6 版的實際回應 [stage 實測，用操作者的使用者 token，不是助理的 managed identity]：

| 呼叫 | 結果 |
|---|---|
| `initialize` | 不論 client 要 `2024-11-05` 或 `2025-11-25`，server 都回 `2025-11-25`；capabilities 有 `tools`、`resources`、`extensions.io.modelcontextprotocol/skills` |
| `skills/list` | 一筆：`uri`＝`skill://<name>/SKILL.md`、`frontmatter.name`／`description`、`resources[]`（每筆有 `uri`、`digest`＝`sha256:…`、`size`＝999） |
| `skills/get` | 同一筆的單筆版本 |
| `resources/list` | 上面的 `SKILL.md`，另有 `skill://index.json`（SEP-2640 索引） |
| `resources/read` | `SKILL.md` 全文（有 front matter） |

只帶 FA 現在的 header `Foundry-Features: Toolboxes=V1Preview` 也能呼叫 `skills/list`。第 5 版的 `skills/list` 回空陣列，讀那個 `skill://` 回 `-32002 not found`，所以 skill 是跟著 toolbox 版本走的 [stage 實測]。

### 固定版本 SDK 能做什麼

`requirements-hosted.txt` 固定 `github-copilot-sdk==1.0.34`（runtime `1.0.112`）[原始碼]。這一版的 `create_session`／`resume_session` 有 [SDK]：

- `skill_directories`：指定 skill 目錄。
- `skill_provider`（**Experimental**）：session 範圍的 skill 來源，介面只有 `list_skills()` → 名稱與描述清單、`read_skill(name)` → `SKILL.md` 文字；resume 時要重新提供；runtime 單次呼叫上限 30 秒。
- `on_mcp_auth_request`：MCP server 要 OAuth 時由 host 提供 token。

SDK 的 Python 原始碼看不出 runtime 會不會對設定的 MCP server 走 Skills extension（那部分在 runtime 執行檔裡）[SDK]。就算會，FA 的 toolbox 工具目前是經 `native_toolbox.py` 自己的橋接送出，不是設定成 SDK 的 MCP server（`native_toolbox.py:5-7`）[原始碼]，所以 runtime 不會自己去連 toolbox。

### 現行 Team Skills 怎麼運作

FA 已經有一條「主人批准後助理才用得到」的 skill 路徑 [原始碼]：

1. **建草稿。** 主人在工作台送 `POST /team-skills/v1/drafts`（`team_skills_api.py:320` → `_save_owner_draft`，`:212`）。內文必須是可見字元、不超過 8192 bytes 的 UTF-8（`behavior_skill_draft.py:34-41`）。用途說明必須是一行、不超過 512 bytes（`m3_models.py:137-140`）。名稱必須是一行、不超過 64 字元（`team_skill_draft_wire.py:20-30`，工作台 `index.html:2573-2583` 也檢查）。附檔最多 4 個（`native_skill_draft_wire.py:39-41`）。存檔時記下 sha256。
2. **批准。** 主人送 decision，`ApprovalInput` 帶 `version_id`、`sha256`、`metadata_sha256`（`m3_models.py:250-255`）。只有 Team 主人能批准，助理不能。
3. **選用。** 助理範本的 `TemplateSkill` 記名稱、`version_id`、`sha256`、`metadata_sha256`（`behavior_agent_template.py:89-113`）。只對新對話生效。一次能選幾個由 `FA_SKILL_SELECTION_MAX` 決定，程式預設 4（`m3_skills.py:26-28`、`:49-54`），stage 與 dev 設成 32。
4. **載入。** Hosted 的 `approved_skills_provider`（`native_capability_tools.py:229`）向 API 取已批准清單，註冊 `read_skill`／`read_skill_resource` 兩個工具。SDK 的 `skill_directories` 設成空的（`m2_copilot.py:1921`）。

助理讀到的是 FA 自己 Blob 裡那一份，hash 等於批准時的 hash。這已經符合 AGENTS 第 6 條與 [ADR-0004](0004-agent-behavior-control.md) v1.2「批准綁定 executor 真正消費的內容與版本」。

## 設計選項

| | A：匯入成 Team Skill（建議） | B：每回合直接從 toolbox 讀 |
|---|---|---|
| 做法 | 主人在工作台看到 toolbox 上的 skill，一鍵帶進現有的「新增 skill」草稿，再照常批准、選用 | Hosted 每回合對授權的 toolbox 版本呼叫 `skills/list`＋`resources/read`，交給助理 |
| 批准綁定 | 沿用現有 sha256 批准，不用改 | 要另建「主人批准了哪個 digest」的紀錄與畫面，只載入 digest 相符的 skill，否則違反第 6 條 |
| 內容被別人改 | 不受影響。助理讀 FA 的那份，toolbox 上改了要重新匯入、重新批准 | `skill_reference` 沒寫版本時跟著 skill 的 `default_version` 走 [文件]，有專案權限的人一改，digest 就對不上，skill 會默默消失 |
| 新程式 | API 一條唯讀路由（用已固定的 `azure-ai-projects` 讀 skill）＋工作台一顆按鈕 | Hosted 的 skill 讀取、digest 批准紀錄、批准畫面，可能還有 Experimental 的 `skill_provider` |
| 每回合成本 | 0 | 每回合多 2 次以上 MCP 呼叫 |
| 身分 | 只要 API 的身分讀得到 | 要助理的 managed identity 讀得到（未驗） |
| 自動跟上 skill 更新 | 不會，要主人重新匯入 | 也不會，digest 變了就要重新批准 |

B 為了守住第 6 條，最後還是要做 A 的批准那一半，再多付每回合的網路呼叫與 Experimental SDK 介面的風險。A 只多一條讀取路徑，其餘全部沿用。

## 建議：A，匯入成 Team Skill

### 匯入流程

1. **列出。** 新增唯讀路由 `GET /team-mcp/v1/skills`，只有 Team 主人能呼叫，用 Team toolbox 的 `default_version`。路由放在 Team MCP API，不放在 `/team-skills/v1`，原因有兩個：Team toolbox 的 client、主人檢查和 toolbox 名稱都在 Team MCP 服務裡，而且只有開了 Team MCP 的環境才有 Team toolbox。一次最多回 8 個 skill（8 份上限長度的內文跳脫成 JSON 後，還在這條路由的 256 KiB 回應上限內），超過的個數放在 `omitted`。第一版不收 `version` 參數：既有 `_no_body_or_query` 會擋 query；要支援非預設版本，就得另寫驗證，也得設計「主人怎麼知道該填哪一版」的畫面。stage 這次的情況見「Neal 的決定」第 3 項。
2. **讀取。** API 用固定版本 SDK `azure-ai-projects==2.3.0`，步驟如下：
   - `project.toolboxes.get_version(...)` 取該版本的 `skills` [SDK]。
   - 只接受 `type` 是 `skill_reference` 的項目（`ToolboxSkillReference`：`name`，可省的 `version`）。其他種類標成不可匯入並寫明原因（AGENTS 第 8 條：未知 shape 明確拒絕）。
   - 有 `version` 就呼叫 `project.beta.skills.download_version(name, version)`；沒有就先 `get(name)` 取 skill 的 `default_version`，再用 `download_version`，回傳的 skill 版本才確定。兩者都回串流的 `Iterator[bytes]` zip [SDK]。
   - `_call` 在操作回傳後就關閉 client（`team_mcp.py:135-138`），所以要在操作內讀完，邊讀邊設上限（例如 64 KiB），超過就停。
   - 用標準庫 `zipfile` 解開。只接受一個檔案：zip 頂層的 `SKILL.md`，或 `<name>/SKILL.md`。目錄項目略過，其他檔案一律標成不可匯入。
3. **回傳。** 每筆回下列內容：
   - `name`、`description`（取自 SDK 的 `SkillVersion.description`，不解析 front matter，API 沒有 YAML 套件）。
   - `SKILL.md` 全文、sha256、toolbox 版本與 skill 版本。
   - 能不能匯入。判斷**直接呼叫草稿用的同一組驗證**：內文 `skill_draft_bytes`、用途 `SkillDraftToolArgs`／`m3_models.py:137-140`、名稱 `OwnerSkillDraftInput.title`。這樣標成可匯入的，送草稿時不會再被 422 擋下。BOM 和零寬字元會被內文驗證擋下 [本機用專案 venv 實測]。

stage 實測：Skills API zip 裡的 `SKILL.md` 是 999 bytes，sha256 與第 6 版 toolbox MCP `skills/list` 給的 `digest`、`resources/read` 文字的 sha256 三者相同 [stage 實測]。所以走 SDK 讀到的，就是 toolbox 給助理的那一份。
4. **帶入草稿。** 工作台把選中的一筆填進現有「新增 skill」表單：名稱用 `name`，用途用 `description`，內文用 `SKILL.md` 全文。主人確認後送現有的 `POST /team-skills/v1/drafts`。
5. **之後不變。** 照現有流程批准、選用，新對話生效。

這樣不用改儲存格式、批准格式、範本格式或 Hosted。助理讀到的仍是 FA Blob 裡、hash 已批准的那份。

### 放在哪裡

- **SDK-first。** API 的 `requirements.txt` 已固定 `azure-ai-projects==2.3.0` [原始碼]。這一版有 `ToolboxVersionObject.skills`、`beta.skills.download`／`download_version`，`beta` 操作經代理自動帶上 `Foundry-Features: Skills=V1Preview`（`operations/_patch.py:58-80`、`:133-138`）[SDK]，所以不自己寫 MCP client。
- 不能用 `native_toolbox.py`：它經 `m2_copilot.py:37` 引入 `copilot`，API 匯入它會違反 AGENTS 第 9 條「API 保持 SDK-independent」[原始碼]。
- 程式放在 `team_mcp.py` 旁，沿用它已有的 Team toolbox 名稱與 project client，不另立設定。
- **退路。** 第 0 階段若 API 的身分讀不到 Skills API，但讀得到 toolbox 的 MCP 端點，改用 `requirements.txt` 已有的 `httpx==0.28.1` 走 MCP `skills/list`＋`resources/read`，並核對 `size` 與 `digest`。兩個都讀不到，就先處理角色指派，那需要 Neal 同意。

### 第一版不做

- **多檔 skill。** zip 裡除了 `SKILL.md` 還有別的檔案時，標成不可匯入並說明。現有 Team Skill 支援最多 4 個附檔，之後要做可以對應過去。
- **來源紀錄與漂移提醒。** 第一版不記「這份來自哪個 toolbox 版本、哪個 digest」。要提醒「toolbox 上的版本比已匯入的新」時，再加一個新的 `ArtifactSource` 種類。那會動到儲存格式，要先讓讀取端認得新種類再上線（AGENTS 第 8 條：未知 shape 明確拒絕）。
- **組織 toolbox（例如 `fa-org-tools`）上的 skill。** Team Skill 只能由 Team 主人批准，組織層由誰批准還沒定。
- **SDK `skill_provider`。** 現有 `read_skill` 工具已能用；`skill_provider` 是 Experimental，換過去沒有使用者看得到的好處。

### 同名與更新

toolbox skill 的名稱成為 Team Skill 的標題，不是識別碼。草稿的識別碼由主人、標題與內容推導出 digest（`team_skill_draft_wire.py:37-47`）[原始碼]，所以：

- 同一份內容再匯入一次，落在同一個草稿，不會重複。刪過的同內容草稿回 `409 M3_SKILL_DELETED`（`team_skills_api.py:155-168`）[原始碼]。
- toolbox 上的 skill 改了內容再匯入，會產生**另一份同標題的新草稿**，不是 409 [原始碼推論]。

更新的做法是：重新匯入產生新草稿，再用工作台現有的「批准新版、停用舊版」流程（`index.html:2560` 一帶）。不另寫同名規則。

## 順手要修的

1. **Team MCP 改版時保留 FA 不管的欄位（必要）。** 不修的話，主人在工作台改一次 Team MCP，入口網站掛的 skill 和內容安全設定就從新預設版本消失，下次也匯入不到。
   - 要保留的欄位：`skills`、`policies`、`description`。`create_version` 多收一個 `base`（改動前的預設版本），自己按版本號重讀那一版，把這三個欄位原樣帶進新版本。toolbox 版本不可變，所以按版本號重讀拿到的就是當初比對過的那一版。
   - SDK 的 `toolboxes.create_version` 已經收 `skills=`、`policies=`、`description=`（`operations/_operations.py:8000-8009`）[SDK]。2026-11-26 在 dev 的測試 toolbox `fa-carry-test` 實建：第 2 版由 FA 從第 1 版改出，`skills`、`policies`（`Microsoft.DefaultV2`）、`description` 都原樣保留 [dev 實測]。
   - 唯一例外：已刪除的 skill 不帶。Foundry 允許刪除還被 toolbox 引用的 skill，舊版本仍留著那個參照；之後任何引用它的新版本都被拒絕，`400 Skill '<name>' not found in project` [dev 實測]。照原樣帶，主人之後每次改 Team MCP 都會失敗。所以帶過去前逐一問 Skills API：回 404 的 `skill_reference`（有指定版本就問那一版）不帶，並記一行 warning；其他錯誤照常往上拋，不會因為暫時讀不到就拿掉參照。被拿掉的參照本來就沒有內容可保留。dev 實測兩種情況：skill 刪掉後，從引用它的版本改出第 3 版，參照被拿掉，`policies` 和 `description` 保留；重建同名 skill 後再從同一版改出第 4 版，參照照樣保留 [dev 實測]。只刪了某個指定版本、skill 本身還在的情況沒有實測。
   - 這樣 `read_default_version` 的回傳形狀不變，`team_mcp.py:152`、`team_connector.py:67`、`:141` 這些呼叫端都不用改。只有 `TeamMcpService.submit` 要把目前的預設版本傳成 `base`。
   - 範圍外：connector 用的是每個 connector 各自一個、由 FA 從頭建的 toolbox（`team_connector.py:155-159`），不在本案範圍。主人若在那些 toolbox 掛 skill，下次改 connector 仍會被蓋掉，列為已知限制。
   - 這是獨立的行為修正，自己一個 commit／PR，附測試。
2. **入口網站複製 metadata 的版本（記錄，不在本案修）。** 第 6 版的 metadata 抄自第 5 版，包括 `fa_request_id`；第 7 版的 metadata 是空的 [stage 實測]。`request_version`（`team_mcp.py:180-194`）從最新版本往回找，同一個 request ID 重送時會先找到第 6 版。接著 `promote_request_version`（`team_mcp.py:340-350`）通常回假的 `409 TEAM_MCP_CHANGED`。只有原本那次 promote 根本沒成功時，才會把入口網站的版本升成預設 [原始碼推論]。只有重送舊請求才會碰到，影響小。

## 分階段

| 階段 | 內容 | 需要的批准 |
|---|---|---|
| 0 | 在 dev 的 Team toolbox 掛一個測試 skill，用 API 的身分跑 `toolboxes.get_version` 與 `beta.skills.download`；順便用助理的 managed identity 跑一次 MCP `skills/list`，留給 B 參考 | 掛 skill 是雲端設定寫入，要 Neal 同意 |
| 1 | 「順手要修」第 1 項：Team MCP 改版保留 `skills`、`policies`、`description`＋測試，並在 dev 實際建一次版本 | 一般開發 PR；dev 建版本有常設同意 |
| 2 | 唯讀路由（用 SDK 讀 skill）、工作台匯入按鈕＋測試。MCP 讀取程式只在第 0 階段證明 SDK 路徑不通時才做 | 一般開發 PR |
| 3 | dev 端到端：匯入 → 批准 → 選用 → 新對話用 `read_skill` 讀到內容。通過後走 stage release PR | dev 部署有常設同意；stage 照 release 流程由 Neal 決定 |
| 4 | stage：把 `fa-team-team-stage` 的預設版本改成第 6 版，匯入 `state-change-authorization-69a08d986ce49282`，再批准、選用 | 改預設版本由 Neal 操作或核准 |

## 驗收

- 單元測試：
  - toolbox 版本的 `skills` 解析，有版本、沒版本兩種。
  - 不是 `skill_reference` 的項目標成不可匯入。
  - 下列都標成不可匯入：超過 8192 bytes、多檔、zip 過大（串流途中就停）、BOM／零寬字元、用途超過 512 bytes、名稱超過 64 字元。
  - 標成可匯入的項目，原樣送 `POST /drafts` 不會被 422 擋下。
  - 非 Team 主人回 403；toolbox 不存在或沒有 skill 回空清單。
- Team MCP：從有 `skills`、`policies` 的預設版本改出新版本後，這兩個欄位與 `description` 不變；只有 Skills API 回 404 的參照被拿掉，讀取失敗（例如 503）時不建版本。
- dev 端到端：工作台匯入那筆 skill、批准、選用後，新對話的助理用 `read_skill` 讀到的內文 sha256 等於**批准時的 sha256**。原檔是 LF 換行、主人也沒改內文時，它也等於匯入畫面顯示的 sha256。工作台的 textarea 會把 CRLF 統一成 LF，主人也可能改內文，所以不拿 toolbox 的 `digest` 當驗收標準。證據是工作台畫面與 App Insights 的 `read_skill` 紀錄。
  - 2026-11-27 dev 通過（API `5b77ff9-r4918-1`；工作台用 origin/main `5b77ff9d`，開在另一個 port，journal 全新）。做法：
    - 在 `fa-team-team-one` 掛測試 skill `fa-p3-import-test`：第 7 版，工具同第 6 版，設為預設。
    - 工作台列出它、帶入、建草稿、批准；`fa-context-b` 選用後開新對話。助理呼叫一次 `read_skill`，答出 skill 裡的暗號。
    - 下列四個 sha256 都是 `130f28c1…`（212 bytes）：Skills API zip 的 `SKILL.md`、帶入後表單內文、批准的版本、App Insights `appi-fa-dev` 記下的 `read_skill` 回傳 `text` [dev 實測]。
    - `appi-fa-dev` 有 Private Link，公開查詢 API 回 `PrivateLinkValidationFailedError`，要走 ARM 的 `/query?api-version=2018-04-20`。
    - 同一回合開頭先跳出 Microsoft Teams 連線卡，卡上寫「這位助理要使用 Microsoft Teams，但還沒連線」；選「先不用」後回合照常進行。這是 connector 的既有流程，與本案無關 [推論]。
    - 截圖、App Insights 擷取與摘要只存在操作者本機，不進 repo；上面的 sha256 就是紀錄。

## Neal 的決定（2026-11-26）

1. **採 A**：匯入成 Team Skill。
2. **入口網站掛 skill 不算批准。** toolbox 有專案權限的人都能改，FA 也沒有那次操作的身分紀錄，不能證明是 Team 主人批准了這個版本。仍須主人在 FA 批准。
3. **stage 這次的 skill 等匯入功能上線**，不先手動貼進「新增 skill」。第一版只讀 Team toolbox 的預設版本，而 stage 的 skill 在第 6、7 版，預設是第 5 版 [stage 實測]。所以匯入前，要先在入口網站把預設版本改成第 6 版。第 6 版的工具與第 5 版相同，只多了 skill。第 7 版多了 `toolbox_search`，FA 橋接遇到它的行為沒有驗證過，不建議用。改預設版本是 stage 的雲端設定寫入，由 Neal 操作或另行核准。

本文當時的備選做法（主人把 `SKILL.md` 手動貼進「新增 skill」）仍然可行，但這次不採用。只有選用之後開的新對話讀得到，前提是 Team 有 `fa-skills-v1` 能力。帶一般 `---` front matter 的內文能通過草稿的內文檢查 [本機用專案 venv 對範例實測]。

## 參考

- Foundry toolbox 的 skill：https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/skills
- 用 LangChain 連 toolbox（MCP 端點與 header）：https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/langchain-toolbox
- Agent Framework 的 Foundry toolbox 整合：https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/tools/foundry-toolbox

## 核對基準

- 程式：`origin/main` `a4a310db`，worktree `fa-toolbox-skill-design`。
- SDK：`github-copilot-sdk==1.0.34` wheel 原始碼（runtime `1.0.112`）；`azure-ai-projects==2.3.0` wheel 原始碼。
- stage：2026-11-26 用操作者的 az 使用者 token 唯讀呼叫 toolbox `fa-team-team-stage` 第 5、6、7 版的管理 API 與 MCP 端點、Skills API 的 skill 與 zip 內容，以及 App Insights `appi-fa-stage`。沒有用 API 或助理的 managed identity 測過。
