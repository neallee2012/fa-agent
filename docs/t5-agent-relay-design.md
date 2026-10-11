# T5 — 同團隊 Agent 轉送設計草稿

狀態：草稿，2026-10-01。已唯讀核對 ADR-0006 §3／§5（#267，`395851b0`），併入 c7bb 的 B 節、1b60 的穩定身分接點及總協調批准的上限變更。程式核對基準仍為 `8a192e712a198a182c0f77518eb2140eb68daafe`，不宣稱新設計已實作、已驗收或已部署。

**最新方向：用已驗 Agent 身分與原入口票轉送；訊息、root 計數、不可變 Agent registration，以及 queued job／pool 同筆 CAS。** 這是不可遺失的待開始收件意圖，並非最終執行票；成功後才由原 `WorkerCore.start` 保存真 binding／attempt，再按原 RID 發票。票不另接單、不新增 RID，`begin` 一次、unknown不重送（2026-10-01總協調確認，依 [ADR-0006 §4／§5](adr/0006-team-trust-boundary-grokbot-mode.md)）。此決定取代下文舊草稿中「訊息同CAS建立B票」的表述；T1純builder只準備接單，T2負責開始、終局和交付接合。

> 請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。

## 1. 發送者由已驗身分決定

既有外部接點是 `agent-message-send`／`agent-message-read`，不是模型直接呼叫 ledger。`BehaviorJwtVerifier` 先驗 token，再由可信 actor 資料得到 `BehaviorPrincipal`；assistant route 將它連同原 assignment 交給 handler。T5 沿用這個 Seam，不新增另一套 JWT 驗證。

| 材料 | 可信來源及用法 |
|---|---|
| sender oid | 已驗 JWT 的 `oid`；不接受 body、工具參數或模型文字自稱。 |
| sender Agent／Team | `Depends(identity)` 提供 `kind=="agent"` 的 `BehaviorPrincipal`；以已驗 oid、tenant、team、actor 對上原入口票的 Agent，不由模型或 config 捏造一個已驗 principal。 |
| recipient Agent／Team | caller 只能選 server 提供的 Agent ID；現階段沿 `config.agent_scope(recipient_agent_id)` 取 Team／identity／target，PR1 日後替換同一解析 Interface。公開名稱不當作 ID。 |
| root、hop、chain、deadline、conversation、in_reply_to | 由原入口票與原訊息回條推導；轉送點發 B 票時寫入，不接受模型擴張。 |
| 本文與 refs | 都是資料，不形成 Team 成員資格、角色、批准或執行權。 |

首次轉送只接受可信解析所得、同 tenant／Team 的兩個不同 Agent。未知 oid、未知收件人、跨 Team、token Agent 與原票不符，都要在發 B 票／入隊前拒絕；即使名稱、內容或 `refs.team_id` 看起來相同也不能通過。不為 T5 重複核整份目前選版、source scope 或收件端讀權。

**B 的結果 sender 綁開始回條的 `caller_oid`。** 79fa 已向 c7bb 確認：只記消耗 B 入口票時，經 JWT／控制連線驗證的 principal，不收 body，也不由 config 推定。它必須對應原 request 的 recipient；結果本文只取同一 B RID 的結束回條。重建時讀原開始／結束回條，不保存 raw token，不沿用 `_controller_principal` 冒稱收到 B JWT。

B 未消耗票、已取消或失聯時，API 可保存來源明確為 `server` 的 `blocked`，不帶 B 本文。它只表示「沒有可確認的 B 結果」，不是 B 已停止的證明，也不是重送許可；原 unknown、claim 與 #253／#263 的停止證據規則保持。

## 2. 同團隊，不再綁死設定中的 A／B

`config.targets[0]`／`[1]` 目前同時被 `assignment_recipient`、`_new_root` 與 history reader 當成固定 A／B。T5 的新 root 應改由已驗 sender 與 registry 中選定的同 Team recipient 建立，不再以陣列位置、名稱或預設 Agent 判斷成員。

新訊息各自保存**本次 sender／recipient**、kind、原票及 `in_reply_to`，不把 `PublicAgentDirectory.max_length=2` 當作全 Team 容量。新 root 只留計數／回覆名額、期限與票鏈，不沿用舊單 handoff／two-peer root。A→B→C 可以是同 root 的兩層委派，不能只放寬公開 wire 卻讓 ledger／restart 仍只認 A/B。

chain 防互等只限制會建立新回合的 `request/handoff`：收件者不能是同鏈上正在等結果的 Agent。`result/blocked` 回原 A 照常保存，由 A 的 JWT 讀回；`fyi/receipt` 不發票，也不算等待鏈。不能把防迴圈規則套到結果回傳而擋死 A。

公開目錄與工具 schema 沿 CAP-06 PR1 的 server 資料接線，和 1b60 對齊，不在 T5 另建一份目錄。保留 #256「給模型真正 ID，而非叫模型猜名稱」的修正；目錄／schema只是選擇資料，正式轉送仍須核身分與 Team。原 assignment 缺收件人資料時，不用目前目錄偷偷補寫原件。

## 3. 訊息上限與模型上限分開

**2026-10-01緊急決定優先：** live真人選collaboration但未交接即被第9次429擋住。
總協調確認現行2跳／8則訊息、唯一handoff／回覆，加peer100足以防互叫，
直接移除`model_allowance`及A/B合計8次，不等完整新票鏈；下面「先保留舊模型合計8」是先前交付決策，
已由本段取代。訊息限制、一般root預算、once／原回條與peer100不刪。

| 限制 | 最新設計的接點 | 必須保留的效果 |
|---|---|---|
| 最多 2 層委派 | 真人root深度為0；只有request/handoff建新回合時，計算「sender票的hop＋1」，在轉送CAS核對。 | A→B request=1、B→C request=2；C→B result不再加1。result/blocked/FYI不增加委派深度，receipt不發票；新wire不沿舊「回覆也算hop」顯示。 |
| 最多 8 則新工作／FYI | 同 root 的 CAS 只計 request、handoff、FYI；request/handoff 同時保存 B 票、排他 queue 及唯一回覆名額。 | 每個已接受工作另保留1個result/blocked，回覆不占這8則；非receipt總數最多16，不會把必要回信擠掉。 |
| 轉送產生的 Agent 回合之模型呼叫上限 | 入口票帶可信上限，VM 在實際送出模型請求前計數。 | 若該類一般回合已有**實際生效**的上限就沿用，沒有則首版 **100 次**；超限明確停止這一回合，不重送。 |
| 舊 A／B root 合計 8 次 intent | 現行 `model_allowance`＋`AssistantAuthority.reserve_model` 的 `peer_limit`。 | 替代機制完成且上線前仍保留。不能先刪計數或用「訊息最多8則」假裝已補上模型保護。 |

總協調已於 2026-10-01 明確批准上述兩種計數的語意變更。100 是防失控上限，不是預算保證，未來才接 Team policy 調整；不先新增 policy CRUD。只依**票的可信來源**限制轉送發票的 Agent 回合，不依 VM 的 `principal.kind` 或模型自稱判斷；真人 instruction 回合維持現況。現行 `reserve_model` 的一般 profile 限制在 `not instruction` 分支，不能因欄位存在就誤認它已限制一般 instruction 回合。

VM 計數按原 RID，不因模型錯誤或未知結果退回名額；同請求 digest 不可再送。沿既有 per-turn gateway，在真正送出模型請求前核對，不數任意HTTP讀取、工具次數或WebSocket開連線。常駐 `_ResidentGateway` 會轉到當前 `turn.gateway`，不能把不同 RID 的額度混在 resident 本體。VM失聯不當新RID從零續跑，用量unknown不歸零。delegation／compaction 的模型呼叫和T4共用計數，不另開旁路；T4直連須等替代完成並上線。

**回覆名額**：`in_reply_to` 必須指向已接受的 request/handoff；一般result的sender等於該request的recipient，收件者等於原sender。以 `in_reply_to` 為唯一鍵，只能建立一次result或blocked，server blocked也用同一個預留名額。模型不能只憑 `kind=result`／`blocked` 就消耗名額或宣稱完成；正文與來源仍由原結束回條或明確server通知產生。

計數可直接核對：新工作／FYI數 `i≤8`，可回覆工作數 `p≤i`，已保存回覆數 `r≤p`，因此非receipt總數 `i+r≤16`。原「所有非receipt合計8」與「root模型合計8」都保留到替代機制完成且上線，不提前移除。

## 4. 原 ID、原回條與防重送

| 識別 | 沿用的推導 |
|---|---|
| root RID | 原入口票上的真人 root RID，不因轉送另建 human root。 |
| 新工具訊息 ID | 轉送點計算 `H(發送票的 RID, SDK callback 的 tool_call_id)`；call id 是 SDK metadata，不是模型參數。 |
| 新訊息 key／內容 | 以原 message_id 只能建立一次，記完整內容 digest；不含到達時間或重試次數。 |
| B 回合 RID | `H(message_id)`；訊息、原 root 計數、B 入口票與 queue 同筆 CAS 成立。 |
| 結果轉送 | 原 request→B RID→開始回條 caller_oid→結束回條；同一結果只能保存一次，回條連回這條原關聯。 |

送出順序是「核已驗身分／原票／Team與關聯 → 查同 ID 原回條 → 同一 CAS 寫訊息、計數、B票及queue」。同 ID 同內容回原件及原 B RID；不同內容回409，不換 ID、覆寫 digest 或再次入隊。FYI只保存、不發票；receipt不計數、不喚醒。不能先回訊息成功，再用另一個獨立步驟補發 B 票。

傳輸回應遺失只沿同一 SDK call／原 RID／message_id 核對；若重試轉送請求，也只能是相同輸入和同一 ID，不能產生第二張 B 票。這不是重送模型或重新執行 B。B 開始未知時保留原開始回條／claim，不能靠再次POST把它當未執行。A不把自己的計算冒稱B結果，也不把系統blocked當停止證明。

交易沿既有 `ControlRequest/ControlChanges` 與回條；明確CAS衝突先核原回條，提交已成立就讀回，不另建重試框架。B入口票由一般worker最多消耗一次，queue消費沿原排他接點，不再由A observer另推一套B consumer。

B 先保存完整結束回條，再由API代轉；A final、B queue消費與真人ACK仍分開。A用自己的JWT讀轉送點等待，不喚醒一個新的A回合。逾期或unknown不丟棄原保存材料；取消與換版仍沿原明確接續，不自動換環境。新ID算法只用於新票鏈，不能重算或改寫舊CAP訊息ID。

## 5. 刪除 refs 分享門禁，不刪來源及原件核對

| 現行判斷 | 草稿處理 |
|---|---|
| `send`／ledger 要求 ref 必須在 `root.spec.allowed_refs` | 新轉送移除此分享白名單；保留 refs 的同 tenant／Team 及完整固定版本資料格式。 |
| sender `read_source`＋B `source_ids`／visibility／purpose preflight＋B registered read | 移除 relay 的這組雙端資料讀取及政策授權鏈，包含 send、read、dispatch、result 路徑，不能只改 send。 |
| `_original_root`／`_current`／`_result_current` 重查兩端當下 policy/read | 由 T1 已驗工作身分及本次同 Team 轉送核對取代；原 root、取消、期限、claim、完成結果關聯仍核。 |
| 版本、hash、request digest、result／reply 對應 | 保留。這些用來防止拿錯原件，不是同 Team 再授權。 |
| caller 自稱 `ref.team_id` | 不當作資源讀權。真正讀資料時，由可信 Team 選取儲存範圍，不能靠 body 選另一個 Team 的容器／資料表。 |

`PeerTeamReference` 的 source／artifact 類型、標準版本格式、最多 8 份且不重複，以及完整訊息大小限制保持。relay 不再為了傳 ref 下載兩遍本文；實際使用資源的 reader 仍須核版本與 hash。傳送一個 ref 不是「已確認資源可讀」的回條。

依新 ADR §3，T3 已改成**API 薄轉送**，不是等待 VM 直連儲存或新增 RBAC：API 從已驗 oid 選 Team，再組固定用途的 key；不收任意 table／partition／key，不提供刪除，Agent 對已發布內容只能讀。T5 refs 沿這個接點，不建立容器／資料表、不加 IAM、不開匿名或 Blob fallback，也不恢復已被取代的雙端逐份政策 gate。本文只記契約，不宣稱薄轉送已部署。

## 6. 修改範圍與恢復路徑

優先在既有 Interface 內改動，不加通用「Team relay framework」或第二層 agent loop。

| 接點 | 預計工作 |
|---|---|
| `behavior_agent_message_api` | 沿已驗principal＋原入口票；收件者沿穩定scope解析，移除固定A/B及雙端refs/policy門禁。新訊息交易要同時發B票，不再無job先登錄。 |
| ledger／T1票producer／T2排他queue | 共用ControlChanges組成一筆CAS，保存原ID、payload及root計數；不把舊單handoff索引硬套到新票鏈。79fa的producer只產生ControlWrite／回條資料、不自行transact，正式函式與shape仍待固定交付。 |
| `behavior_agent_message_runtime`、`behavior_agent_peer_work` | 新路徑移除PeerWorkReference、B專用consumer、A observer代推及config principal；沿一般回合的開始／結束回條取B結果，server blocked明確另標來源。 |
| history／restart | 舊原件沿舊ID與原回條讀回；新票鏈不受targets[0]/[1]、舊whitelist或restart只掃前兩個queue的假設限制。不為新格式破壞舊材料。 |
| `behavior_agent_message_tools`／純wire | 保留窄模型參數；把SDK call id作為工具metadata交server計ID。新wire需區分Agent結果與server通知；型別與reader一併接，API不載Hosted SDK。 |
| VM模型接點／T4 | 從原票取得每回合上限，在真正送出前計數；與T4共用同一接點。新的100次保護先完成並上線，之後才移除舊peer_limit。 |
| refs helper／測試 | 若不再有正式 caller，同片移除只服務舊分享政策的 helper與相應斷言；保留跨 Team、格式及原件完整性案例，不靠新增旁路留下舊 gate。 |

舊 root 的 `allowed_refs`、directory、policy digest 及原 message bytes 不重寫、不清空。優先沿既有 wire shape 解碼原資料；舊欄位可保留作歷史材料，但不再是新轉送的分享批准。新舊紀錄都核自身原 digest／回條，不拿現在的 registry 或選版重算舊批准。這是保護現有資料可讀，不是準備舊 image 回退或兩階段部署。

## 7. 待實作的直接驗證

以下是測項，不是本稿已執行的結果；沿既有 API／ledger／工具接點測，不另建內部專用測試框架。

| 情境 | 必須核到的結果 |
|---|---|
| 同Team不同Agent，包含非targets[1]的成員 | A→B→C可到深度2，C回B及B回A不增加深度；C再發新回合的深度3拒絕。不漏掉restart或工具schema。 |
| 真正簽章 token 的 oid、body 自稱、跨 Team／未知 Agent | 只採已驗 oid；拒絕不得留下 peer work、訊息或模型 intent。 |
| B結果代轉／未開始即取消／失聯 | sender只來自原開始回條caller_oid；server blocked不帶B內容、不冒稱B成功、不清unknown或放行重送。 |
| 同 Team 固定 ref 不在舊白名單，及跨 Team／錯格式 ref | 前者可轉送且不呼叫雙端 source readers；後者拒絕，資源 body 不形成讀權。 |
| 同SDK call並行／改內容／CAS或HTTP回應遺失 | 最多一筆message及一張B票，同次CAS的root/queue一致；改內容409，原B開始未知不重新執行。 |
| 第2／3層、8則新工作／第9則、最多16則含回覆 | 第9則新工作拒絕，但8個既有工作的預留回覆仍可保存；每個工作第二個result/blocked拒絕。 |
| chain互等、系統通知與FYI/receipt | 新request/handoff不能排到正等結果的祖先；result/blocked可讀回，FYI/receipt不發票；偽造模型result不能占回覆名額。 |
| Agent回合模型第100／101次與既有較小上限 | 第101次不送出且明確停止；真正既有上限優先；未知不退計數，同digest不再送，真人回合不被順手新增上限。 |
| API／runtime 重建、原保存結果回讀、舊 root／新 refs root | 原 ID、digest、計數、queue 與結果不變；移除 whitelist 後不能讓新合法紀錄在 startup 被拒。 |

優先延用 `test_behavior_agent_messages.py`、`test_agent_message_api.py`、`test_agent_message_continuation.py`、`test_agent_message_tools.py` 與其現有固定 SDK 接線案例；將 `test_agent_message_refs.py` 中已被新規格取代的雙端政策斷言改成上述直接契約。

產品切片完成後跑 Claude Opus 5.5 Standards＋Spec，prompt 原文附最小機制原則；P1／P2 修清後只由原軸核 delta，GitHub CLEAN＋固定 head 再正常合併。本草稿只改文件，不先跑產品 review、模型或 live。

## 8. 依賴與交接基線

**已先做的獨立切片（2026-10-01）**：總協調確認不等B票即可交付peer的本機模型counter。
固定實作`ec165327`沿真per-turn gateway完成100／101界線、並行共用、原請求不重送、
新回合reset與真人不變，並接上peer failed保存及固定blocked原因；[37案結果](agent-runtime-evaluation-baseline.md#2026-10-01t5-peer-回合模型上限)已保存。
舊peer_limit與單handoff結構未刪，B票與完整新轉送仍未接線；Opus5.5兩軸在`25be1558`均0 P1／0 P2。
此片已由#279合併`48223e99`，未部署；
T1票值後續接`run_copilot(model_call_limit=...)`同一個gateway計數，不另造T4計數器。

- **79fa／T1**：新的human registry不是T5前置。已確認producer純產生ControlWrite／回條資料、不自行transact，`behavior_turn_wire.py`／`behavior_turn_receipts.py`由79fa持有；caller_oid取verified JWT context。尚待固定wire/head/函式及同RID結束回條接點，不沿舊claim票接熱執行。
- **c7bb／B 節**：全文、chain／server blocked、hop與8則口徑已核清；已唯讀核對#267的固定ADR `395851b0`，不重開研究。
- **1b60／CAP-06**：已確認PR3之後維持同一身分Interface，T5不直接讀oid registry、不等PR3。收件者目前用config.agent_scope，PR1提供同Interface的目錄解析；T5不重做onboarding。
- **T2／T4／T3**：一般worker／排他queue與T2共用；模型counter和T4對齊，替代上線後T4再前進；refs沿83d4的T3薄轉送，不等待或新增IAM。不因本稿啟用WAKE-05或操作live。

**目前onboarding的實際限制**：完全未配置的新oid/target不能只插Table就生效。`for_agent`先經config.agent_scope；registry核原config sha與回條，factory再取其native target。已預配四個identity/targets內的C可沿合法登錄與懶載入，但新對話還要`FA_INSTRUCTION_HOSTED_VERSIONS[C]`明設；不能因此宣稱任意Agent可免改ENV／部署。這是現行程式判讀，未實測live C，不把它另列成T5的registry等待條件。

WAKE-05 #259 已於台灣 2026-10-01 09:48:31 合併為 `8a192e71`，兩原軸清零；預設關閉，沒有建立 live 排程。其測試結果仍在[原 evaluation](agent-runtime-evaluation-baseline.md#2026-10-01wake-05-owner-routine-producer)，不是 T5 結果。

總協調另於 2026-10-01 交接：部署線在 `fec04a8e` 的 09:05 新回合 `text-6b449d4c` 已完成 request A→B／result B→A，A明示「B 核對的結果是 530」；4afa核來源鏈判②③ PASS。原 `3969` FAIL 保留，完整紀錄 PR 由 fdbb負責；此處只記交接，不冒稱本線重跑或已讀其完整原件，也不把舊 CAP 路徑的 PASS 算作 T5 驗收。

## Verified against

- 決策：ADR-0006固定版本 §3薄轉送、§5B及模型上限；透過GitHub API唯讀核對，沒有執行git fetch或改動本機產品基準。
- 補充契約：c7bb提供的`adr0006-section-a-b.md`與hop／chain／blocked澄清、1b60的身分Interface確認、79fa的純producer與caller_oid交接，以及總協調對100次與8＋預留回覆的明確批准。
- 身分／入口：[behavior_auth.py](../src/fa-shared-context-poc/shared_context/behavior_auth.py)、[behavior_assistant_api.py](../src/fa-shared-context-poc/shared_context/behavior_assistant_api.py) 的原 token principal、private ticket 與 handler 接點。
- 轉送：[behavior_agent_message_api.py](../src/fa-shared-context-poc/shared_context/behavior_agent_message_api.py) 的 `_new_root`／`send`／`read`；[behavior_agent_messages.py](../src/fa-shared-context-poc/shared_context/behavior_agent_messages.py) 的 `authenticate`／`send_recorded`／`_require_allowance`。
- 保存／額度：[behavior_agent_message_runtime.py](../src/fa-shared-context-poc/shared_context/behavior_agent_message_runtime.py) 的 `model_allowance`／`forward_result`、[behavior_assistant.py](../src/fa-shared-context-poc/shared_context/behavior_assistant.py) 的 `reserve_model`，以及 [history reader](../src/fa-shared-context-poc/shared_context/behavior_agent_message_history.py)。
- 工具／wire：[behavior_agent_message_tools.py](../src/fa-shared-context-poc/shared_context/behavior_agent_message_tools.py)、[behavior_agent_message_wire.py](../src/fa-shared-context-poc/shared_context/behavior_agent_message_wire.py)；程式均只讀核對於 `8a192e71`。尚未改產品或執行 T5 測試。
- VM接點：[m2_copilot.py](../src/fa-shared-context-poc/shared_context/m2_copilot.py) 的 `_TurnCallbacks`／`_ResidentGateway`；已核它轉到當前回合gateway，尚未實作新的模型計數。
- Foundry：唯讀閱讀 [Microsoft 官方 `microsoft-foundry` skill](https://github.com/microsoft/azure-skills/blob/main/skills/microsoft-foundry/SKILL.md)；本機未載其工具，未執行 dependency setup、azd、Foundry MCP 或雲端操作。
