# 版本模型 G：照 session 實際的版本執行，助理閒置時才升級

狀態：**「決定」部分已確認**（Neal 2026-10-12 19:53（+08）「都同意」）；**「草案：閒置升級機制」仍是草案**，要等「協調物件生命週期」表的「未定」格子與草案的已知未解問題都解決才定稿。兩部分都尚未實作｜日期：2026-10-12。

本 ADR 合併後只是設計契約；程式、測試、部署流程另走實作 PR，不因本文合併而生效，也不授權任何部署、雲端寫入或刪除。

## 本 ADR 分兩部分

| 部分 | 內容 | 地位 |
|---|---|---|
| **決定（已確認）** | 第 1～4、7～11 點與 phase 0～3b：版本是執行期事實、相容規則、漂移偵測與處理、v1→v2 遷移、不叫醒已回收 session、通知句子、保留舊版本、AGENTS 界線 2 措辭 | Neal 2026-10-12 19:53 確認；單獨成立，**不依賴**閒置升級機制 |
| **草案：閒置升級機制** | 第 5、6、12 點與 phase 4～6：U0～U9、派送意圖紀錄、租約、Hosted `pending` 的新用法、reconciler、否決與自動退回、Blob 快照、觸發 | **不屬於已確認的決定**。Neal 已定方向（「Neal 已決定」第 2～5、7 項，以及第 8 項的一般機制），但生命週期表的「未定」格子與已知未解問題解決前不定稿、不實作 |

**Neal 決定 8 在兩部分的切法（全文一致）：** 決定＝無法接續的 RID 不改寫回條、不換 RID、通知主人以新訊息重送，以及來源碰不到時這些 RID 不擋棄用來源換新環境、讀到時以「之前的 SID 清單」判斷是否回通知的規則（第 7a 點、第 9 點）；草案＝同一 SID 漂移時無法接續的 RID 怎麼判定（已知未解問題 9；SID 換過的情況用之前的 SID 清單判斷，不需要標記）、派送意圖紀錄怎麼收尾、閒置升級時「不擋升級」的一般機制。

點的編號沿用上一版，沒有重排：第 5、6 點移到草案，第 7 點拆成決定（部署規則）與草案的第 12 點（觸發）。所有協調物件的建立、結束、過期與收拾方式見「協調物件生命週期」。

## 一句話

**版本是「正在跑的那台 VM 自己是哪一版」這個事實，不是 FA 堅持它該是哪一版。** 相容就照跑並記下來；不相容才明確拒絕。換到新版本是一件有紀錄、有通知的事，做完從對話歷史與 checkpoint 接續；「只在助理閒置時換」靠草案的閒置升級機制，它上線前換環境照第 7 點（入口是現行 follow；來源碰不到時是棄用來源換新環境）。

## 取代範圍

- 取代 [ADR-0003 的 2026-10-12「Neal選A」一節](0003-hosted-environment-continuity.md#platform-version-drift-replacement)。Neal 2026-10-12 改為「我比較prefer G」，**選項 A 不會實作**。A 節保留原文作為歷史；A 裡可以重用的零件（API 端 Agent 綁定列、可重算的 SID、先預留再建立、CAS 寫入、平台 session 狀態表）由本 ADR 直接引用，不另抄一份。
- 不改 ADR-0003 的 DP-02 目標（每個 Agent 有可持續恢復的 Hosted 環境）、2026-10-04 的系統政策（無法安全搬檔時自動不搬檔接續並明說）。[CTX-03](../ctx03-checkpoint-contract.md) 的拒絕規則不變（精確固定 SDK `1.0.13`／runtime `1.0.83`、不認識的 codec 一律拒絕、保留最後合法狀態），但 G 讓 API 與 Hosted 可能是不同 build，所以本 ADR 把 CTX-03 的固定版本與 codec 清單**納入 profile 的定義**（第 3 點）：profile 在每次送出前檢查；個別對話的 codec 由 Hosted 載入時檢查。
- 改變的是現行程式的兩個前提：「API 預期的版本＝endpoint 目前選定的版本」，以及「selector 一改，所有 Agent 立刻 follow」（後者的替代做法在草案）。

## 背景：現在怎麼做，哪裡出問題

### 現行程式（基準 `origin/main` `201fd3a8`）

| 做法 | 位置 |
|---|---|
| API 每次都把「目前選定版本」當成預期版本：`selected_version()` 讀 endpoint selector，`_platform_reader` 要求 session 回報的版本等於它 | `shared_context/behavior_native_sessions.py` `selected_version`（259）、`_platform_reader`（369–379） |
| SID 由版本算出；同一 SID 只認那一個版本 | `native_session_id`（37–45）、`_scoped`（264–273）、`_latest_version_reader`（123–146） |
| 選定版本必須是**這個 API build** 的版本（`fa_build` 完全相等），否則回 503 `AGENT_VERSION_FOLLOWING` | `behavior_agent_versions.py` `HostedVersions.current`（148–165） |
| 呼叫 Hosted 時帶 `x-client-fa-expected-version`＝API 認為的版本；VM 內版本不同就 409 `NATIVE_SESSION_CHANGED` | `behavior_foundry.py`（360–362）；`native_turns.py` `native_response`（1996–2000）、`behavior_native_turn_wire.py` `require_expected_version`（55–63） |
| 上面這個 409 在 `native_response` 開頭就丟出，**沒有**帶 `not_dispatched`；只有 `_submit_locked` 的接單檢查會加（1059–1060） | `native_turns.py` |
| Hosted 用「版本號較大」判斷自己是否被取代 | `native_turns.py` `replaced()`（558–567） |
| 部署：發布版本、改 selector 後立刻 `follow_all` 把所有 Agent 換到新版 | `behavior_agent_versions.py` `main`（538–552）；`.github/workflows/deploy-staging.yml` 的 follow 步驟 |
| 送出時遇到版本不同，`_follow_before_admission` 在同一請求裡推進一次 follow | `behavior_native_turn_api.py`（595–655、672–700） |
| FA 永遠用 `create_session`＋`VersionRefIndicator` 明確建立 session；呼叫平台時只帶 agent 名稱與 `agent_session_id`，不帶版本 | `behavior_foundry.py` `create_session`（208–240）、invoke（344–362） |

### 平台實際行為（dev 觀察，不是官方保證）

- 閒置被回收的 session 再被叫醒時，平台綁的版本不一定是原來的。總協調唯讀整理 dev 2026-10-12 的紀錄：8 次叫醒中，3 次等於當下的 selector，4 次既不等於 selector、也不等於 FA 紀錄（例如 B 的 `db0386d0` 紀錄 103、醒來是 97；A 的 `8d9a3d14` 建立時 145、醒來是 141）。**2026-10-13 更正**：這份整理當時以 `list_sessions` 的 `created_at` 變了來數「重建」，但那個值其實是最後存取時間（見下一條），所以這 8 次是叫醒，不是平台換了 session 物件；曾寫的「B 的 `created_at` 每次重建都會重設」也是同一個假象，session 物件的建立時間沒有變。
- **dev 證據（唯讀，2026-10-13）：** ① `list_sessions` 回的 `created_at` 等於 `last_accessed_at`（列表的怪癖）；`get_session` 回的才是真正的建立時間，**叫醒不會重設它**。所以先前「`created_at` 重設＝平台重建」的觀察，實際看到的是最後存取時間。② **同一個 session 物件（建立時間沒變）上確認有版本漂移：** `db0386d0` 於 10-12 14:56:10 由 v103 的 follow 建立，16:20:37 叫醒後回報 v97；`8d9a3d14`（A）於 10-11 22:10:10 以 v145 建立，現在回報 v141；`95b9ee1b` 於 10-12 16:19:42 由一次 v104 的 follow 建立（哪個 Agent 本文未核），17:33:33 存取後回報 v105。因此偵測「同一 session 換了版本」靠 `version_indicator` 與建立時綁的版本比對（漂移旗標，第 2 點；第一次設定時同時設通知待送位，由主人本人的回應交付，第 4 點），不靠 `created_at`；任何以 `created_at` 偵測重建的邏輯一律只讀 `get_session`，不讀 `list_sessions`。
- FA 自己會叫醒舊 SID，每叫一次就多一次漂移機會與費用：①follow 的 `advance` 經 `FollowFiles._source()` 檢查舊來源（`behavior_session_files.py` 122–136 接受 `idle` 後就呼叫 `inspect`）；②`prepare` 經 `_latest_version_reader`；③API 記憶體裡的舊 reader 快取（②③由 #813 修）；④舊版 `fa-chat-…` 對話先 `verify_session` 再呼叫原 SID（`behavior_work_policy.py` 1404–1411，`verify_session` 在 1411）。
- 結果：現行設計把「版本≠選定版本」一律當錯誤，A 被卡死不收新回合（[ADR-0003 A 節背景](0003-hosted-environment-continuity.md#platform-version-drift-replacement)）。

### Grok Bot 怎麼做（研究摘要，未另行核對原始碼）

以下來自總協調提供的研究摘要，只當設計參考：

- harness 版本存在機器上，從本機版本檔讀出，**由 runtime 回報給伺服器**；伺服器不強迫 client 是哪一版。
- 新 bundle 先放好，舊的照跑；supervisor 等閒置才切換：`pauseTurnsForUpgrade`、`suspendWakes`、有回合在跑就拒絕切換、最多延後 6 小時、重新準備最多重試 2 次、會 crash-loop 的版本被否決。
- 重啟後重讀逐字稿、接回工作；檔案留在同一顆磁碟。整台機器換掉是另一條由使用者觸發的路，會先警告「裝過的軟體不會帶過去」。

### Foundry 的限制（官方文件，2026-10-12 重讀）

- 每個 session 在**建立時**綁定一個版本；不帶 `version_indicator` 時由 endpoint 的 `version_selector` 決定；帶了就固定該版本，之後發布新版也不變。（[manage-hosted-sessions：Create a session explicitly](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#create-a-session-explicitly-advanced)）
- 改 endpoint selector **不會**搬移既有 session；只要還有 session 或回退需求依賴舊版本與 image，就要保留它們。（[manage-hosted-agent：Release a version without changing production](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#release-a-version-without-changing-production)）
- session 在第一次使用時建立；閒置逾時後平台回收 compute、保存 `$HOME` 與 `/files`；**同一個 session ID** 再被引用時配新 compute 並還原；30 天無活動永久刪除。（[hosted-agents：Sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions)、[Session compute lifecycle](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#session-compute-lifecycle)）
- 閒置或停止不等於沒有進行中的回合：處理器在關機寬限期內停在安全點並 defer 時，response 或 task 保持進行中，留給之後的 lifetime 接手；自己 stop 的 session 不保證會自行重啟。（[How stopping a session affects an in-flight turn](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#how-stopping-a-session-affects-an-in-flight-turn)）
- 文件**沒有寫**：叫醒時用哪個版本；dev 看到的那種叫醒後版本變了、但 `get_session` 的 `created_at` 不變的情況，是否就是官方說的 resume（官方說 compute 回來時，不論在「新的或既有的基礎設施」上，`$HOME` 與 `/files` 都會自動還原）；`get_session` 會不會喚醒 compute。本文把這三項都當作「不知道」處理（見「平台未知」）。

**結論：在 Foundry 上，一個 session 不能就地換版本。** 所以 Grok 的「同一台機器換 bundle」在 FA 只能對應成「建立一個綁新版本的新 session，接續對話，必要時把工作檔帶過去」；「在閒置時做」是草案的部分。

## 決定（已確認）

Neal 2026-10-12 19:53（+08）「都同意」：本節第 1～4、7～11 點與 phase 0～3b 已確認（phase 3b 是審查第 3 輪把第 7 點已確認的部署閘門與版本保留從草案 phase 6 拆出來，不是新內容）。本節單獨成立，不依賴後面的草案；草案上線前，不相容的 Agent 照第 7 點換環境（入口是現行 follow；來源碰不到時是棄用來源換新環境）。

### 1. 四種「版本」分清楚：誰是事實來源

| 名稱 | 怎麼得到 | 用途 |
|---|---|---|
| **VM 自報版本** | VM 內 `FOUNDRY_AGENT_VERSION`（`behavior_hosted.py` 662、`behavior_assistant_hosted.py` 1812）。**現行 image 沒有任何回應欄位帶它**：`NativeSessionState` 沒有版本欄位，只有 `FA_NATIVE_HTTP` log 的 `agent_version`，以及 expected-version 檢查「相等就收、不同就 409」這個二選一結果。新 image（phase 2）在 expected-version 的 409 本文與 `NativeSessionState` 加 `actual_version` | **最終事實**：這一回合實際由哪一版程式執行 |
| **metadata 版本** | `get_session` 讀回的 `version_indicator.agent_version`（不 invoke） | 不呼叫 session 的觀察；對 `idle`（compute 已回收）的 session 只是「上次綁的版本」，**醒來前不知道實際會是哪一版**；對 `active` 的 session 是**假設**，要由 VM 接受 expected＝該值（或新 image 的 `actual_version`）確認後才算事實 |
| **紀錄的實際版本** | API 端 Agent 綁定列（第 2 點）上次觀察到的值，附觀察時間與來源（metadata／VM 自報） | 定位與相容判斷的起點；每次觀察到不同值就更新並留紀錄 |
| **目標版本** | endpoint selector 目前選的版本 | **只**用來決定「下一個新 session 綁哪一版」（草案上線前是現行 follow 的目標，之後是閒置升級的目標），不再是送出回合的前提 |

FA 不再推導「預期版本＝最新選定版本」。

### 2. Agent 綁定列：只記位置與實際版本

- 沿用 A 節第 2 點的位置與寫法：API 既有 Context Table 每個 Agent 一列，寫入走 owner／ETag／CAS（[AGENTS](../../AGENTS.md) 界線 5）。讀這一列不呼叫任何 session。
- **本節的欄位**：目前 SID、這個 SID 建立時綁的版本（世代 0 就是算出 SID 的那個版本；換環境建立的新 SID 是 commit 結果的 `hosted_agent_version`。這個值寫得出來：`create_session` 以 `VersionRefIndicator(agent_version=target.agent_version)` 釘版本並核對讀回，`behavior_foundry.py` 208–238）、**同一 SID 漂移旗標**（第一次觀察到的實際版本不同於這個 SID 建立時綁的版本就設定；之後版本又變回建立時的版本也不清除，只在目前 SID 換掉時清除；第 7a 點適用條件 (iii) 靠它判斷。不能只比「建立時的版本≠紀錄的實際版本」：版本號不單調，建立時 145、醒來 141、再醒來 145 時這個比較會說沒漂移，但釘在 141 的回條仍永遠卡住）、**漂移通知待送位**（與漂移旗標在同一次 CAS 設定；第一個主人本人的回應帶上第 8 點換版通知、寫出後以 CAS 清除，非主人呼叫者不清；目前 SID 換掉時隨漂移旗標一起清除；第 4 點末段）、紀錄的實際版本（含觀察時間、來源；另記最近一次讀到的 `created_at`，只取自 `get_session`、不取 `list_sessions`，只在既有流程本來就讀 `get_session` 時順便記下〔認領時讀了 metadata、第 4 點第 6 步舊 image 409「SID 沒變」分支〕，不為它另加讀取，所以可能是空的）、對應的協定 profile、之前的 SID 清單（各附版本；每次換環境 commit 後加入來源 SID，第 7a 點 (e)(f) 靠它判斷）、最近一次換環境的理由與 `files_migrated`。升級狀態、租約、預留 SID、否決清單、派送意圖紀錄都**不在**本節，只在草案（第 5 點）。
- 世代 0＝現有由版本算出的 SID，照舊可讀；沒有綁定列的既有 Agent 先「認領」它目前的 SID（phase 1）。
- 只有 API 寫這一列，時點只有四種：認領（同時寫 SID 建立時綁的版本；紀錄的實際版本也寫成這個版本，認領時若已讀到不同的版本〔例如 metadata〕，就把讀到的版本寫成紀錄的實際版本，並在同一次寫入設定漂移旗標與通知待送位；認領在請求之外讀 metadata、沒有 VM 可確認，沿用這個規則，通知位留著等下一個主人回應。否則世代 0 的 SID 建立時 145、認領時已回報 141，之後每次觀察都等於紀錄的 141、不算「觀察到不同版本」，旗標永遠不會設定）；送出時觀察到不同版本（第 4 點第 6 步的漂移 CAS；觀察到的版本不同於 SID 建立時綁的版本時同時設定漂移旗標與通知待送位；舊 image 要等 VM 接受 expected＝V_obs 之後才寫，第 4 點第 6 步），或同一分支讀 `get_session` 時順便讀到的 `created_at` 不同於紀錄（保底訊號，沒有保證的讀取點，見第 4 點末段）；換環境完成（草案上線前＝第 7 點換環境回傳的 commit 結果，phase 1 起就寫：目前 SID＝新 SID、**新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`**、清除漂移旗標與通知待送位、來源 SID 加進之前的 SID 清單；之後＝草案 U8）；通知清除（主人本人的回應帶上通知且已寫出後，單列 CAS 把通知待送位清掉，第 4 點末段）。建立、結束與崩潰後收拾見「協調物件生命週期」。
- 三個假設「SID↔版本」的地方改查綁定列：`_scoped`、`_latest_version_reader`、`_metadata_sessions`。
- **兩份「目前綁定」紀錄**：API 綁定列是定位的權威（零 session 呼叫）；Hosted state store 的 `agent` 紀錄仍是 VM 內的柵欄，照現行由 retarget／follow commit 指令寫。兩者不一致（例如 follow 已 commit、API 還沒更新綁定列就崩潰）由第 4 點第 6 步的「不一致」分支處理，不當成漂移；來源不相容時送出在第 4 點第 1 步就被拒絕、到不了第 6 步，改由重跑切換補寫（第 7a 點「commit 後、綁定列 CAS 前崩潰」）。
- `replaced()` 改成比 SID，不再比版本號大小：平台漂移已經讓版本號不單調（A 的 SID 建立時 145、醒來 141），不必等草案。被取代＝「Hosted `agent` 紀錄的目前 SID 不是我，**而且**我也不是 `environment.state="pending"` 的目標 SID」。只比目前 SID 不行：follow 進行中，`hosted_session_id` 要到 commit 才換成新 SID（`native_environment.py` 744），新 SID 在這段期間收到的 `_RECORD_READS` 會被誤拒成 409（`native_turns.py` 2027–2029；現行在 `pending` 時改比 `previous_agent_version`，558–567）。

### 3. API 與 Hosted 的相容規則

**協定 ID 沿用 state profile（`FA_NATIVE_TURNS`），不新增握手；但要從 `foundry-native-v2` 起，profile 相同才代表相容。**

- **`foundry-native-v1` 不能當相容依據。** 這個字串從定義以來沒換過（`native_responses_wire.py` 10；`behavior_agent_versions.py` 133、338、350 與 `behavior_native_sessions.py` 119 還有寫死的字面比較），但 `extra="forbid"`、`strict=True` 的 wire DTO 一直在變：2026-09-15 起 `behavior_native_turn_wire.py`、`behavior_wire.py` 在 `origin/main` 有 81 個 commit。已部署的 v97、v103、v141、v145 等都報 `foundry-native-v1`，schema 卻各不相同；只比字串的話，API 會把它們當相容，結果是 422 或解不開的 202 回條（界線 7）。以前安全，是因為 `fa_build` 必須完全相等。
- **規則：一個 profile 要先在 repo 裡有 schema 快照測試，才可以放進支援集合。** 快照測試出現之前就存在的 profile（目前只有 `foundry-native-v1`）**永遠不相容**。phase 2 把 profile 換成 `foundry-native-v2` 並加快照測試；第一個 G API 的支援集合只有 `{"foundry-native-v2"}`，所以**所有 G 之前的版本都不相容**：送出一律明確拒絕，換環境走第 7 點的一次性遷移（不 invoke 舊 VM、不搬檔）。
- **profile 字串涵蓋四件事，任一件變了就換字串：** (a) `NATIVE_REQUEST` 與回應 DTO 的 JSON schema；(b) Hosted state store 項目的 schema；(c) CTX-03 的固定 SDK／runtime 版本；(d) 這個 image 讀得懂的 checkpoint codec 清單（對應對話設定的 `checkpoint_profile`，例如 `auto-no-custom-v1`）。快照測試把四件事一起存成快照，任一件變了但 profile 沒換就失敗。
- 每個版本的 definition 已經帶 `FA_NATIVE_TURNS`，API 用 `get_version` 讀（`_require_native_version`、`_responses_enabled` 已在讀），不呼叫 session。上面那些寫死 `"foundry-native-v1"` 的比較都改成「在支援集合內」。
- **送出前的相容判斷：** 版本 Y 相容 ⇔ Y 的 profile 在 API 的支援集合內，而且 Y 的 definition 身份（agent GUID、instance identity）相符、狀態 `active`。**每次送出前**都檢查，只讀 definition，不呼叫 session。
- **checkpoint codec 在 Hosted 載入時檢查，不在送出前檢查。** native checkpoint 只存在 Hosted state store 的 `fa/v1/conv/<CID>`（`native_checkpoint.py` 1–8）；API 沒有持久的 codec 標記：回合 API 只把對話與 capture 放在記憶體 LRU（`_kept_conversations`，`behavior_native_turn_api.py` 138–141），capture 裡的 `template.settings.checkpoint_profile` 是對話**設定**的 codec，不是最後合法 checkpoint 實際用的 codec。所以 codec 由 Hosted 在載入 checkpoint 時依 [CTX-03](../ctx03-checkpoint-contract.md) 第 4 節檢查，不認識就明確拒絕、保留最後合法狀態（同文件第 9 節），不能當成空對話。日後若要改成送出前檢查，前提是 API 在接單時把 codec 標記持久化到每個對話、並為既有對話回填；做到之前不宣稱送出前檢查得到。
- `HostedVersions.current` 的 `fa_build` 完全相等檢查**拿掉**，換成上面的判斷；`fa_build` 只留作資訊。
- Hosted 端照舊要求 `x-client-fa-state-profile` 等於自己的 profile（`native_turns.py` `native_response` 開頭）。API 照現行送**自己目前的** profile（`behavior_foundry.py` 361 一律送常數）；支援集合只有這一個 profile（Neal 決定 6），所以相容的 session 的 profile 就是它，profile 不同的 session 在第 4 點第 1 步就不送。這個檢查排在 expected-version 檢查**之前**、也在讀 request body 之前，不符時回 **403 `NATIVE_TURNS_DISABLED`**，不是 409；所以「醒來綁到不同 profile 的版本」看到的是 403（第 4 點第 5 步、第 9 點）。
- 功能層級的 profile（`files_profile`、`files_omission_profile`、Responses profile）維持「缺功能就那個功能明確拒絕」（例如 `NATIVE_FILES_UNSUPPORTED`），不讓整個回合不相容。
- **支援集合只放目前的 profile，不寫跨 profile 轉接**（Neal 決定 6）。換 profile 時，舊 profile 的 session 一律不相容，等換環境。
- 大部分發版（提示、工具、只改 Hosted 的行為）不動上面四件事，所以**相容、不必強迫升級**。
- **state store 項目一律寫明 profile。** 現行 `_Meta`（`native_turns.py` 173–174）與 `NativeSubmit`、`NativeAgentReceive`、`NativeRoutineSubmit`（`behavior_native_turn_wire.py` 197、725、747）在 profile 等於預設值時不寫這個欄位（`NativeReceipt` 等其他項目一律寫）。只換常數的話，v2 會把沒標的 v1 時代項目讀成 v2，v1 image 也會把沒標的 v2 項目讀成 v1。所以從 v2 起每個項目都明寫 profile；沒標的項目一定是 v1 時代寫的，v2 照現行 follow 的做法讀它們（遷移掃描與接續既有對話都要讀）。
- 從 v2 起，較舊 image 讀到較新 profile 寫的項目必須明確拒絕（同 CTX-03「不認識的格式不猜」），不能解碼失敗後當成空狀態。已凍結的 v1 image 改不了：`NativeStateProfile` 是嚴格的 `Literal`（`native_responses_wire.py` 12），讀到明寫的 `foundry-native-v2` 會驗證失敗，但失敗後怎麼處理無法再改。所以遷移後 FA **永不再 invoke v1 的 session**（第 4 點第 1 步不送、第 7 點遷移不碰、第 10 點不叫醒）。
- **已接的工作仍釘在接單時的版本。** Hosted 照現行拒絕 `execution_version` 不同的排隊輸入與已接受輸入（`native_turns.py` 463–464、1595–1596），G **不放寬**：同一個 profile 只保證 wire 與存檔格式相同，不保證另一版會把做到一半的工作接對。「相容就照跑」只適用**新接的單**；漂移時已有釘在舊版本的工作怎麼辦，見第 9 點。舊版 `fa-chat-…` 對話的 `verify_session`／`_check_session_version`（`behavior_foundry.py` 195–205、258–278）同樣照舊釘版本：版本不同就照現有規則明確 reset 或開新對話。

### 4. 送出一個回合：怎麼處理實際版本與漂移

`require_expected_version` 保留「完全相等」，但角色從「相容閘門」改成「**漂移偵測器**」：

1. API 直接讀綁定列（不用記憶體快取），得到目前 SID 與紀錄的實際版本 V_rec，用第 3 點判斷 V_rec 相容。不相容：不送，明確拒絕（`not_dispatched`），照第 9 點處理。草案上線前，保留的送出時 follow（`_follow_before_admission`）在這一步的拒絕**之前**先跑；第 7a 點 (j) 的補寫依賴這個順序（否則來源不相容時每次送出都停在這裡，重跑切換永遠不會發生）。
2. （保留編號）草案的派送意圖紀錄若上線，在這一步寫；本節**沒有**這一步，見第 5 點與草案已知未解問題 1、3。
3. 呼叫時 `x-client-fa-expected-version`＝**V_rec**（不是 selector），`x-client-fa-state-profile`＝API 目前的 profile（V_rec 相容，所以就是 V_rec 的 profile；第 3 點）。
4. VM 版本＝V_rec：照常接單。
5. 回 403 `NATIVE_TURNS_DISABLED`：現行程式在讀 body 前就丟出，所以確定沒派送。先重讀綁定列；SID 已換就照第 6 步「SID 已換」處理；SID 沒變，就當「醒來綁到 profile 不同的版本」，這一回合明確拒絕（第 9 點）。
6. 回 409 `NATIVE_SESSION_CHANGED`：這個碼現在有兩種原因，不能直接當漂移——(a) Hosted 的 `agent` 紀錄已指向別的 SID（`native_turns.py` `_original_session` 673–676、讀取前的 `replaced()` 2027–2029）；(b) VM 版本≠expected（`require_expected_version`）。新 image 在 409 本文加 `reason`（`replaced`／`version`）與 `actual_version`；舊 image 沒有，所以一律先重讀綁定列：
   - **SID 已換。** 確定沒派送（403，或 409 帶 `not_dispatched`）才同 RID、同 body 改送新 SID。舊 SID 可能已接單時（舊 image 的 409 沒有 `not_dispatched`）不能推定新 SID 可以接手：`rid:<RID>` 雖然存在同一個 Agent 的 `fa/v1/turns`、新舊 SID 讀同一份，但新 SID 接不了舊 SID 的工作：舊 SID 接下、仍是 `accepted` 的 RID 若改送到新 SID，會走 `_require_original_session` 被 409 拒絕（`native_turns.py` 1093–1095 → 673–676）。所以先經新 SID 以 `NativeRead` 讀這個 RID（讀取不會 409，回條照原樣回來，839–844）：讀到終態回條就照原回條；`NATIVE_RID_MISSING` 才確定沒接單、再送；讀到 `accepted`／`started` 且 `hosted_session_id`＝舊 SID，就明確失敗，原 RID 保持未決（界線 7），**不可改送**、不換 RID；舊 SID 在綁定列的之前的 SID 清單時，失敗回應帶第 8 點「這則訊息是較早的版本接下的……」（第 7a 點 (e)）。
   - **SID 沒變。** 取實際版本 V_obs：新 image 用 409 本文的 `actual_version`；舊 image 只能用 `get_session` metadata（session 剛被叫醒，此時是 `active`）。metadata 的值只是假設，下一步 VM 接受 expected＝V_obs 才算確認（第 1 點）；漂移 CAS 與通知待送位也等這個確認才寫（下一條）。
   - **V_obs＝V_rec：不一致，不是漂移。** VM 就是紀錄的版本卻仍 409，原因只能是 (a)：Hosted `agent` 紀錄已指向別的 SID，綁定列還沒跟上（例如換環境的兩處寫入只完成一半）。不記漂移、不重送；這一回合明確失敗（舊 image 沒有 `not_dispatched` 時，RID 照界線 7 保持未決），留 `NATIVE_BINDING_DIVERGED` log。修復：草案上線前交給保留的送出時 follow（`_follow_before_admission`）收斂，再依它回傳的 commit 結果 CAS 更新綁定列；草案的 U8 另要求兩處寫入可重入（草案已知未解問題 6）。這條只在第 1 步通過（V_rec 相容）時走得到；綁定列仍指向不相容的來源時，修復靠重跑切換（第 7a 點）。
   - **V_obs≠V_rec：漂移。** 要寫的是綁定列 CAS：記下 V_obs 與來源（`vm`／`metadata-active`）；V_obs≠SID 建立時綁的版本時，同一次寫入設定漂移旗標與通知待送位（第 2 點），並留 `NATIVE_VERSION_OBSERVED` log。**什麼時候寫，看 V_obs 從哪來：** 新 image 用 409 本文的 `actual_version`，是 VM 自報的事實，立刻寫，再依下面處理；舊 image 的 V_obs 只是 metadata 的假設（第 1 點），**要等 VM 在同一個請求內接受 expected＝V_obs 之後才寫**：下面「讀這個 RID」或「再送一次」，任何一次以 expected＝V_obs 對同一 SID 的呼叫沒有被 409／403 拒絕，才算確認；沒確認（讀不到、再送仍是 409）就什麼都不寫，V_rec、旗標、通知位都維持原樣，下一個請求重新觀察。這樣 metadata 讀錯時不會設旗標，也不會發出錯誤的通知；代價是確認前崩潰或失敗的那次漂移不留紀錄、下次再觀察（多一次 409）。通知不再只附在這個請求的回應上，由通知待送位交付（本點末段）。V_obs 不相容：舊 image 沒有 VM 可確認（不相容的 VM 不 invoke），不寫旗標，這一回合照第 9 點拒絕；新 image 照上面寫入，再照第 9 點拒絕。V_obs 相容：409 **帶** `not_dispatched: true`（新 image）就同 RID、同 body、expected＝V_obs 再送一次；**沒有** `not_dispatched`（舊 image）就不推定沒送出，依界線 7 先以 expected＝V_obs 對同一 SID 讀這個 RID（`NativeRead`；session 此時 `active` 且相容，不會為此叫醒已回收的 session）：`NATIVE_RID_MISSING` 才確定沒接單，再送一次；讀到原回條就照原回條；讀不到就明確失敗（`dispatch_unknown`），不換 SID、不換 RID 重送。
   - 再送仍是 409：不再試，明確失敗，原 RID 保持未決（第 9 點）；舊 image 此時沒有確認，不寫漂移 CAS。
7. 逾時、5xx、回應遺失：照現行界線 7，不重送。

「每個 RID 最多再送一次」只是**重試上限**，防止漂移時繞圈；**不重複執行靠的是 Hosted 的 `rid:<RID>` create-or-read 去重**：同 RID、同 body 送幾次都只建立一次，body 不同就回 `NATIVE_RID_CONFLICT`。

Hosted 端配合（新 image 才有）：`native_response` 在 dispatch 前的所有拒絕（expected version、profile、`replaced()`）都加 `not_dispatched: true`、`reason` 與 `actual_version`。舊 image 沒有，所以上面「先讀綁定列、再核回條」是必要的。

**平台在同一個 session 換了版本：用綁定列的「通知待送」位交付一次通知（2026-10-13 更正；PR #816 審查第 2 輪改交付方式；總協調決定，通知句待 Neal 確認）。** dev 實際看過的平台自行變動就是這一種：同一個 session 物件（`get_session` 的 `created_at` 沒變）叫醒後回報的版本不同於建立時綁的版本（見「平台實際行為」）。FA 第一次觀察到它，就是同一 SID 漂移旗標從「未設定」變成「設定」的那次寫入（第 2 點：認領時讀到不同版本，或本點第 6 步的漂移 CAS；舊 image 要等 VM 接受 expected＝V_obs 才寫）；**同一次 CAS 一併把綁定列的通知待送位設成「待送」**。通知不只附在做這次寫入的那個請求的回應上，因為那樣會在三種情況永遠漏掉，而旗標之後不再清除、不會有第二次機會：(a) 認領在請求之外發生，沒有回應可附；(b) 先看到的是非主人呼叫者（routine、Agent 間交辦、群組、HITL 提醒，也就是第 7a 點 (k) 的入口中不是主人本人的）；(c) 回應遺失（逾時、5xx、CAS 之後崩潰）。交付規則：

- **帶上：** 每個**主人本人的回應**（主人經工作台，或 Teams 主人回覆）在 API 直接讀到的綁定列（第 1 步）通知待送位是「待送」時，附第 8 點「平台把助理的執行環境換成了另一個版本（vX→vY）……」；vX＝這個 SID 建立時綁的版本，vY＝紀錄的實際版本。非主人呼叫者不帶、也不清。
- **清除：** 通知已隨回應寫出（工作台 HTTP 回應已完整寫出、Teams 回覆送出被接受）之後，以 CAS（If-Match 剛讀的 ETag）把待送位清掉；衝突就重讀，仍是「待送」就再清一次，否則放著。順序是先送、後清：寫出前崩潰，位元還在，下一個主人回應再顯示；清除前崩潰或清除失敗，同樣下次再顯示，最壞重複一次，所以是**至少一次**，清除遺失後的重複顯示接受。殘餘：回應已寫出卻在網路上遺失，API 無從得知，這則通知會漏掉（界線 7：寫出不等於真人已收到）。
- **不過期：** 待送位沒有時限；目前 SID 換掉（換環境 commit，第 7a 點 (f)）時隨漂移旗標一起清除，換環境另有自己的通知（第 8 點）。旗標已設定後再觀察到其他版本（含漂移後漂回）不再設待送位。
- **範圍：** 待送位每個 Agent 一個，被第一個主人本人的回應清掉，其他對話不再顯示；「每個對話各顯示一次」的標記與補發仍屬草案（已知未解問題 8）。
- **phase 1 的限制：** 工作台目前不顯示任何第 8 點通知（第 8 點待辦，顯示屬 phase 2），所以 phase 1 只設待送位、回應欄位帶通知，清除要等 phase 2 的顯示上線才啟用，否則通知會在沒人看到時被清掉；Teams 主人回覆路徑同理，要有顯示才清。

句子保持中性、不說檔案不見：官方說叫醒時 `$HOME` 與 `/files` 會還原，同一物件換版時 session 的檔案區照理保留。不相容的漂移另照第 9 點拒絕並附「版本不相容」句。這不是 FA 換環境，但 FA 照實說；AGENTS 界線 2「平台自行重建環境時也照實記錄與告知」靠這一句兌現。

**`get_session` 的 `created_at` 改變：只是順便讀到的保底訊號（2026-10-13 更正）。** 先前的推論（B 每次以不同版本被叫醒時 `created_at` 都重設）被 dev 證據推翻：`list_sessions` 的 `created_at` 等於最後存取時間，`get_session` 的 `created_at` 在叫醒時不變。所以 `created_at` **不是**版本漂移的偵測器。它只在既有流程本來就讀 `get_session` 時順便比對（認領時讀了 metadata、本點第 6 步舊 image 409「SID 沒變」分支；新 image 的 409 帶 `actual_version`，送出時不讀 `get_session`），FA 不為它另加讀取，**沒有保證的讀取點，可能完全漏掉**。讀到與綁定列記錄的值不同（記錄是空的不算），才當成「換了一個 session 物件」，記下來並把第 8 點「平台重建了……」通知附在這個請求的回應上（保底訊號本來就可能漏，不做待送位）。dev 目前沒看過這種改變，官方也沒有定義 `created_at` 何時會變；真的換了物件時，FA 不假設 `$HOME` 還在。

### 7. 部署、`foundry-native-v1` → v2 遷移與保留舊版本

- **`foundry-native-v1` → v2 的一次性遷移：入口沿用現行 follow，但來源碰不到時不是 follow。** phase 2 換成 v2 的那次部署，所有 Agent 都還是 v1、全部不相容；換環境的入口仍是部署時的 `follow_all` 與送出時的 `_follow_before_admission`。現行程式在這次遷移有三個缺口：
  - **來源 403 不算來源拒絕。** v2 的 API 帶 `foundry-native-v2` profile 呼叫 v1 的 VM，會在讀 body 前被 403 `NATIVE_TURNS_DISABLED` 拒絕；`advance`（`behavior_native_sessions.py` 632–643）只吸收 `FILES_ERRORS ∪ {NATIVE_SESSION_CHANGED, NATIVE_ENVIRONMENT_CONFLICT}`，`NATIVE_TURNS_DISABLED` 不在 `FILES_ERRORS`（`native_files_wire.py` 16–21），所以直接往上丟；`copy_step` 只接 409（`behavior_session_files.py` 148–160）。
  - **送出時不 follow。** `origin/main` 的 `_prepared_before`（`behavior_native_turn_api.py` 659–670）讀 v1 session 的 state 會拿到 403，把 Agent 當成「沒準備過」。#813（phase 0）之後更早停下：v2 還沒有 session 時，`_require_selected_version` 在讀任何 v1 state 之前就回 503 `AGENT_VERSION_FOLLOWING`，送出直接拒絕、不 follow（#813 `5464a2f2` 的 `behavior_native_turn_api.py` 609–614、687）。所以真正的缺口是「v2 session 還沒建立 → 不算準備過 → 送出時不 follow」，只能等部署時的 `follow_all`。
  - **現行 follow 處處假設來源碰得到。** 部署時與送出時的 follow 都用 `idle_only=True`（`native_environment_sync.py` 17–20）。審查第 3、4 輪找到的等待點，每一個在來源碰不到時都永遠結束不了：`_scan_page`（`native_environment.py` 363–377）讀到同一個 Agent `fa/v1/turns` 的任何 `accepted`／`started` 回條就回 `accepted_or_started`；Hosted `agent` 紀錄的 `admitting` 不是空的就在掃描前回 `admission_pending`（554–555、658–661、708–709），commit 時又原樣帶進新紀錄（743–745），之後每個 v2 送出都被 503 `NATIVE_ADMISSION_PENDING` 擋下（`native_turns.py` 677–679），`_settle_admission` 也清不掉它（回條的 `hosted_session_id` 不是新 SID，726–733 回 409）；`_tasks_pending` 經 `quiet()`（`native_task_observer.py` 75–107）看到 chain task `in_progress`、有 lease 或還有 steering 輸入就回 `task_active`；來源 metadata 404 時 API 的 `_source_session_status` 丟 503（`behavior_native_sessions.py` 91–95），follow 回 `source_unavailable`、永遠不 commit（586–598），閒置超過 30 天被平台刪掉的 Agent 就永遠換不了。v1 來源上的這些東西，v2 碰不到（403 在讀 body 前），第 10 點也不准為此叫醒 `idle` 的來源；同時 Hosted `pending` 讓新工作一律 503（`native_turns.py` 670–672），第 4 點第 1 步也拒絕送出，Agent 卡死。逐一在 follow 裡加例外，每一輪都會再找到下一個，所以下面第 3 項（第 7a 點）改前提。

  所以 **phase 2 的必要範圍**（不是「用測試確認」）：
  1. 任何仍會 invoke 來源的路徑，來源回 403 `NATIVE_TURNS_DISABLED` 一律當來源拒絕：走既有 `source_unavailable` 階段寫 `source_invocation_refused`（`native_environment_files.py` 56–67），之後照 2026-10-04 不搬檔路徑；`advance` 與 `copy_step` 的接受集合加上它。第 7a 點的棄用來源換新環境本身不 invoke 來源，這一項是其他路徑的防線。
  2. phase 1 之後，送出時的「準備過」改成「這個 Agent 有綁定列」：只讀綁定列，不呼叫任何 session；v2 session 還沒建立也照樣換環境。
  3. **來源碰不到時不是 follow，而是「棄用來源換新環境」**（Neal 決定 8 套用到這次遷移），見第 7a 點。

  三項做不到，phase 2 就不單獨上線，改成與草案機制一起上線。切換完成前，Agent 照第 9 點明確拒絕並通知。
- **停用送出時 follow 與部署時 `follow_all` 屬草案**（第 12 點、phase 4、6）。草案上線前，這兩條現行路徑一律保留，它們是觸發換環境（含第 7a 點的棄用來源換新環境）的唯一入口。
- **部署前的相容閘門：** CI 部署新 API 前，讀所有綁定列的 profile；新 API 的支援集合必須涵蓋所有被紀錄中的 profile，否則拒絕部署（先讓舊 profile 的 Agent 換到新 profile）。閘門在 v1→v2 遷移完成後接上（phase 3b），否則第一次 v2 部署一定被擋。
- **保留舊版本：** repo 目前沒有任何 `delete_version` 呼叫；本 ADR 把它定成規則：綁定列目前 SID 與之前的 SID 清單最後一個（前一個）引用的版本與其 ACR image 不刪、不讓保留政策清掉（phase 3b）。刪除任何版本、image 或 session 仍需另外批准。

### 7a. 棄用來源換新環境

原為第 7 點第 3 項，審查第 5 輪升成獨立一節；文中「第 7a 點」與舊稱「第 7 點第 3 項」指同一件事。**本節不只用於 v1→v2 的一次性遷移**：任何換環境遇到來源碰不到（第 9 點的不相容漂移、404 或 `failed`／`deleted`／`expired`），或下面適用條件 (iii) 的同一 SID 漂移，都照本節處理。

#### 適用情況

API 讀來源 SID 的 metadata（`get_session`，不 invoke），而且下列任一成立：

- **適用條件 (i)** 來源版本不相容（例如 v2 API 之下任何 `foundry-native-v1` 版本），metadata 是 `idle`、`failed`、`deleted`、`expired` 或 404。
- **適用條件 (ii)** 不論相容與否，metadata 是 `failed`、`deleted`、`expired` 或 404（來源已經不存在，上面的東西永遠不會結束）。**來源相容時要再讀確認一次**：隔一段有界的間隔再讀一次 metadata（間隔由實作 PR 訂，例如數秒到一分鐘），兩次都是 404 或 `failed`／`deleted`／`expired`，才寫下「棄用來源」的決定（(g)）；第二次讀到其他狀態就不棄用，照現行 follow。任一次讀取是 5xx 或逾時，都不算 404 或終態（照原錯誤處理，第 9 點），不寫決定。理由：這個決定寫入後不翻轉、會放棄搬檔，而相容的來源本來可以照常 follow，單次 404 可能只是暫時讀錯。來源不相容時適用條件 (i) 不變，一次就算（反正碰不到它）。
- **適用條件 (iii)** 來源就是綁定列的目前 SID，綁定列這個 SID 的**同一 SID 漂移旗標**已設定（第 2 點：曾觀察到不同於建立時綁的版本；之後版本變回來也不清除；換環境新建、從沒漂移過的 SID 不適用），metadata 是 `idle` 或適用條件 (ii) 的狀態，而且換環境只剩這個 SID 上的非終態回條在等。這些回條可能釘在漂移前的版本、Hosted 永遠拒絕執行（`native_turns.py` 463–464、1595–1596），永遠到不了終態；判定它們是哪一種屬草案已知未解問題 9，**換環境不依賴這個判定**。代價要說明白：漂移之後才在目前版本接下、本來下一次叫醒會照常接續的回條，也一併放棄——回條原樣保留，換完讀到時同樣回第 8 點「這則訊息是較早的版本接下的……」通知（(e)），主人以新訊息重送。來源是相容的，所以 (ii) 的狀態（404 或 `failed`／`deleted`／`expired`）照兩次讀取規則（(m)）確認後才算。限制同 (d)：來源若正是 selector 版本算出的 SID，現行 `follow_session` 無法為同一版本另建 session（`native_environment.py` 603 新舊同 SID、609 要求較新的版本），要等發布新版本或草案機制；期間這些回條照一般未決狀態回報（第 9 點）。metadata 是 `active` 時照現行等待，不棄用（相容的 SID 還在用，等它閒置）。

**來源不相容但仍是 `active`（或 `creating`／`updating`／`deleting`）：只等 metadata。** 每次換環境的嘗試只重讀來源 metadata，不 invoke、不 stop、不切換；變成適用條件 (i) 的狀態後才棄用來源換新環境。理由：(1) v2 API 本來就 invoke 不了 v1（403 在讀 body 前），沒有別的事可做；(2) 不能 stop：現行 follow 對 `active` 的來源會 `stop_session`（`behavior_native_sessions.py` 589–590），但那是在 Hosted 掃描確認回條都已終態之後，棄用來源沒有這個確認，stop 會強制結束還沒停在安全點的回合（官方 stop 說明，第 10 點）；不 stop，v1 VM 還能自己把回條寫到終態，主人看到的是正常結果；(3) 平台在該版本的 `idle_timeout_seconds`（官方 120～3600 秒、stage 3600）沒有請求後回收 compute，**前提是 FA 完全不再呼叫這個 SID**。這個前提在 phase 2 不會自然成立，要靠 #813、第 10 點、第 7 點第 1 項，加上下面的零呼叫需求一起保證。

- **零呼叫需求：** 綁定列紀錄的 profile 不在支援集合內的 SID，API 的**任何路徑**都不 invoke 它、也不對它發 Files API 呼叫；在送出請求之前只讀綁定列判斷，不靠對方回 403（被拒絕的請求也是一次請求，可能重設閒置計時）。`origin/main` `1d2d5f91` 有約 51 處呼叫、分在 10 個檔：回合、串流與 Responses API、session 與換環境（含搬檔）、Agent 間交辦、群組、routine、HITL 提醒、skill 草稿；全部經 `NativeTurnGateway._invoke`（`behavior_native_turn_api.py` 273–274）呼叫 `invoke_native`（`behavior_foundry.py` 476），這是 invoke 的檢查點；Files API 呼叫全部經 `SessionFiles`（`behavior_session_files.py` 34–75），這是 Files 的檢查點。零呼叫的範圍另含 `stop_session`（現行 follow 在 `behavior_native_sessions.py` 589–590 對非 `idle` 來源呼叫它）：只有下面「逾時回報」經批准的操作者決定可以例外。讀這種 SID 上的 RID（例如工作台輪詢進行中的 RID）時，API 不經該 SID 讀回條，照 (e) 回報；現行 API 沒有持久的開放 RID 紀錄（第 10 點），所以這時只能回一般未決狀態。
- **假設：** 重讀 metadata（`get_session`）不延長閒置。這是 dev 觀察、不是官方保證，第 10 點已列為實作 PR 要用 log 確認的事。
- **逾時回報：** 從第一次嘗試起超過 `idle_timeout_seconds` 仍是 `active`（可能是長回合還在跑，也可能有東西還在呼叫 v1 SID，例如滾動部署中的舊 API 副本）：留專用 log `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`（不和一般不相容拒絕共用）；之後的送出改顯示第 8 點「舊環境仍在運作」句，不再用「不相容」句（那句承諾換完再送即可，這時已不會自己好）；並升級成**需批准的操作者決定**：繼續等，或批准對來源 `stop_session`（接受可能強制結束還沒停在安全點的回合）後照適用條件 (i) 換環境。FA 不自動 stop、不強制。**回報管道**就是這兩樣：`NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE` log，以及主人看得到的狀態（第 8 點「舊環境仍在運作」句）；由總協調／操作者讀這個 log 處理；FA 不另外發訊息或開工單，任何 `stop_session` 都要先有經批准的操作者決定。

逾時前，等待期間的送出照第 9 點明確拒絕並顯示「不相容」句；來源變成 `idle` 後，下一次送出時的 follow（或部署時 `follow_all`）就完成切換。

#### 做法

FA 不 invoke 來源、不經來源讀檔或讀狀態、不 stop 它；來源上進行中的任何東西（非終態回條、`admitting`、chain task、steering 輸入）都不接續、不複製、不擋切換，回條原樣保留；不搬檔（④，`files_migrated=false`）；新的 session 從它讀得懂的共用對話歷史與 checkpoint 接續（第 3 點：沒標 profile 的項目當 v1 時代讀；codec 不認識就照 CTX-03 明確拒絕、保留最後合法狀態）；通知主人：來源還在（`idle`）用第 8 點 ④ 原句，來源已失效或被平台刪除（`failed`／`deleted`／`expired`／確認過的 404）用第 8 點「舊環境已失效」句。切換後來源 SID 進之前的 SID 清單，讀到那些非終態回條時照 (e) 回「較早的版本接下的」通知。

`idle` 不代表沒有進行中的回合（第 10 點）；適用條件 (i) 與 (iii) 都接受這個風險：切換後 FA 不再 invoke 來源 SID；新 session 也接不了舊 SID 接下的工作（`_original_session`，`native_turns.py` 673–676；第 4 點第 6 步），排隊輸入另受 `execution_version` 釘選拒絕（463–464、1595–1596），所以 deferred 的回合本來就不會被接手。

**commit 後、綁定列 CAS 前崩潰：修復靠重跑切換，不靠第 4 點第 6 步。** 這時綁定列的目前 SID 仍是來源；來源不相容時第 4 點第 1 步在送出前就拒絕，到不了第 6 步。下一次送出時的 follow 或部署時 `follow_all` 以同一個來源重跑切換時，必須冪等地回傳已 commit 的結果（現行 `follow_session` 對同一來源、同一目標、已 `ready` 的環境原樣回傳，`native_environment.py` 587–592），API 依它 CAS 補寫綁定列；不建立第二個新 session、不重做切換、不重新判斷 metadata（以 (g) 寫下的決定為準）。重跑前 selector 若已換到更新的版本，重跑是一次新的 follow（從已 commit 的新 SID 換到更新的版本），它的 commit 結果的之前 SID（`previous_hosted_session_id`）是那個新 SID、不是原來源；所以 API 寫綁定列時，commit 結果的之前 SID≠綁定列目前 SID，就把綁定列目前 SID 也加進之前的 SID 清單，原來源才不會漏掉（漏掉也不會誤叫人重送：(e) 判定不了就回一般未決狀態）。崩潰到補寫之間的保險：(e) 看到 `hosted_session_id`＝綁定列目前 SID（仍是來源），照進行中回報，不叫任何人重送。

**實作方式不限：** 實作 PR 可以另寫一條切換路徑，不必把這些例外穿進 `follow_session`；另寫路徑而不寫 Hosted `pending` 時，照實更新生命週期表第二列，而且同樣要滿足上一段的冪等。實作 PR 必須重新核 `follow_session` 與 API 端 `follow`／`advance` 的每一個等待原因與拒絕，把來源碰不到時結束不了的補進下表。審查第 4 輪看到、尚未逐一判斷的：`_tasks_pending` 的 `task_access_denied`（`native_environment.py` 435–453）；不搬檔路徑 `require_complete` 的 `NATIVE_FILES_CONFIRMATION_REQUIRED` 與 `check_latest_checkpoint`（`_scan_page` 371–392）；Responses 回條的 `responses_environment_blocker`（356–358）；`files_unsupported`（631–632）；Hosted 端的 `source_unavailable`／`source_active`（698–705）。另外，Hosted 端 `NativeBootstrapSource` 接受的狀態（`native_sdk.py` 131）沒有 `deleting`，API 端 `_source_session_status`（`behavior_native_sessions.py` 94）與上面的等待清單有；只有切換路徑重用 Hosted 的檢查時才有影響，屆時兩邊對 `deleting` 的處理要一致。

#### 驗收表

實作 PR 的需求與驗收測試（離線替身；除非另註，來源不相容、metadata `idle`）：

| # | 情況 | 驗收結果 |
|---|---|---|
| (a) | 同一 Agent 的 `fa/v1/turns` 有 `hosted_session_id`＝來源 SID 的 `accepted`／`started` 回條 | 切換完成；回條逐位元組不變（不改寫、不換 RID）；經新 session 以 `NativeRead` 讀回原回條 |
| (b) | Hosted `agent` 紀錄的 `admitting` 不是空的（來源留下的） | 不回 `admission_pending`，切換完成；新紀錄 `admitting` 為空、`chain_task_id`＝`binding_task_id(agent, 新 SID)`；切換後同一 Agent 第一次送出新 RID→202 接單，不是 503 `NATIVE_ADMISSION_PENDING` 或 `AGENT_VERSION_FOLLOWING`；那個 `admitting` 的 RID：有非終態回條照 (e)，沒有 `rid:` 回條→`NATIVE_RID_MISSING`，同 RID 可照常送到新 session |
| (c) | 來源的 chain task `in_progress` 或有 lease，或有 steering `pending_inputs`／`active_input` | 不回 `task_active`／`task_unconfirmed`，切換完成；留 log（task ID 與筆數，不含內容）；對應的 RID 照 (e) |
| (d) | 來源 metadata 404（API 端與 Hosted 端 `NativeBootstrapSource`，`native_sdk.py` 121–123，都讀到） | 與 `deleted` 相同，切換完成；不回 `source_unavailable`；相容的來源 404 要兩次讀到才算（(m)）。限制同第 9 點：相容的來源若正是 selector 版本算出的 SID，現行 `follow_session` 無法為同一版本另建 session（`native_environment.py` 603 新舊同 SID、609 要求較新的版本），要等發布新版本或草案機制；期間送出照第 9 點明確拒絕 |
| (e) | 讀一個非終態的 RID | 由 API 判斷（Hosted 沒有綁定列）：`receipt.hosted_session_id` 在綁定列的「之前的 SID」清單→回第 8 點「這則訊息是較早的版本接下的……」；等於綁定列目前 SID→照一般進行中回報，**不**叫主人重送（界線 7：不叫主人重送還在跑的工作）；兩者都不是→無法判定，照一般未決狀態回報，不發這句 |
| (f) | 切換 commit 成功 | API 依 commit 結果以 CAS 更新綁定列：目前 SID＝新 SID、新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`、同一 SID 漂移旗標與通知待送位清除、來源 SID 加進「之前的 SID」清單（phase 1 起每次換環境 commit 都這樣寫，第 2 點）。「第一次棄用來源之前，不存在屬於已換掉 SID 的非終態回條」（現行 follow 每次 commit 前都要求同一 Agent 的所有回條已終態，`_scan_page` 不分 SID）是對既有資料的推論，測試證明不了：實作 PR 上線後第一次換環境時，對該 Agent 既有的回條做一次唯讀讀回（經新 session 讀同一份 `fa/v1/turns`，不叫醒任何 `idle` session，第 10 點），`hosted_session_id` 不是目前 SID 的非終態回條，把那個 SID 補進之前的 SID 清單（版本不明也可以，(e) 只看在不在清單）；讀不到就照實記成未驗證 |
| (g) | 同一次切換重試時 `idle_only` 或呼叫路徑不同（部署時 `follow_all`、送出時 follow、`advance` 的不搬檔路徑） | 「棄用來源」的決定由 API 依 metadata 做出、放在切換指令裡，寫進 Hosted 的進度紀錄；重試以紀錄為準，不回 409 `NATIVE_ENVIRONMENT_CONFLICT`（現行 `native_environment.py` 595–599 比 `record.idle_only != command.idle_only`）；決定寫入後不因之後的 metadata 改變而翻轉 |
| (h) | 來源不相容、metadata `active` | 不 invoke、不 stop、不切換，只重讀 metadata；Agent 的送出照第 9 點明確拒絕 |
| (i) | 之後的換環境（v2→v3 或漂移）遇到 `hosted_session_id` 在「之前的 SID」清單的非終態回條 | 不等它（同 (e) 的判斷）；`hosted_session_id`＝目前 SID 的非終態回條照現行等待，除非符合適用條件 (iii)（驗收見 (n)(p)(q)） |
| (j) | commit 後、綁定列 CAS 前崩潰 | 下一次送出時的 follow 或 `follow_all` 重跑切換：拿到已 `ready` 的同一結果，綁定列寫成目前 SID＝新 SID、之前的 SID 清單含來源；不建立第二個新 session；補寫前讀來源上的 RID→照進行中回報，不叫重送。變化：重跑前 selector 已換到更新的版本→重跑是新的 follow，commit 結果的之前 SID≠綁定列目前 SID→之前的 SID 清單同時含原來源與那次 commit 的之前 SID；之後讀原來源上的非終態 RID→照 (e) 回「較早的版本接下的」通知 |
| (k) | 來源 `active`、綁定列紀錄的 profile 不在支援集合；對每一個 API 入口各打一次（回合、串流、Responses、Agent 間交辦、群組、routine、HITL 提醒、skill 草稿、session 與換環境、工作台輪詢 RID） | 離線替身在兩個檢查點記錄：對該 SID 的 `invoke_native`（經 `NativeTurnGateway._invoke`）與 `SessionFiles` 的 Files API 呼叫都是 0；`stop_session` 也是 0；每個入口回明確拒絕或一般未決，不透傳 403 |
| (l) | (h) 持續超過 `idle_timeout_seconds` | 留 `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`；送出顯示「舊環境仍在運作」句，不是「不相容」句；不 stop、不切換，直到操作者經批准的決定 |
| (m) | 相容的來源：第一次讀 404，再讀是 `active`；或任一次讀取是 5xx／逾時 | 不寫棄用決定、不切換到棄用路徑，照現行 follow（5xx／逾時照原錯誤處理）；兩次都 404 才照 (d) 切換 |
| (n) | 相容的來源是目前 SID、綁定列這個 SID 的漂移旗標已設定、metadata `idle`，換環境只剩這個 SID 上的非終態回條在等（測試同時放 Hosted 拒絕執行的釘選回條，與漂移後才在目前版本接下的回條） | 換環境不等它們，切換完成；兩種回條都逐位元組不變；換完讀這些 RID→照 (e) 回「較早的版本接下的」通知。同樣情況但 metadata `active`→照現行等待，不棄用；metadata 第一次 404、再讀 `idle`→照 `idle` 處理，不當成已失效 |
| (o) | 通知 | 來源 `idle`→④ 原句；來源 `failed`／`deleted`／`expired`／確認過的 404→「舊環境已失效」句 |
| (p) | 換環境 commit 後的新 SID 從沒漂移過（漂移旗標未設定）、metadata `idle`，換環境只剩它上面的非終態回條在等 | 適用條件 (iii) **不適用**：照現行等待，不棄用、不通知 |
| (q) | 同一 SID 漂移後又漂回（例如建立時 145，觀察到 141，再觀察到 145），之後 metadata `idle`，只剩它上面的非終態回條在等 | 漂移旗標仍是設定的；適用條件 (iii) 適用，照 (n) 切換 |

### 8. 使用者看到的通知

| 情況 | 句子（工作台頁尾與狀態欄位，每個對話顯示一次；「平台在同一個 session 換了版本」列例外，見該列） |
|---|---|
| 換到新版本、工作檔已帶過去 | 「助理已更新到新版本，工作檔也一起帶過來了。」 |
| 換到新版本、沒帶工作檔（④），舊環境還在 | 沿用 2026-10-04 原句：「已換到新版環境；這次沒有搬工作檔，舊環境與檔案都保留。」 |
| 換到新版本、沒帶工作檔，舊環境已失效或被刪（第 7a 點：`failed`／`deleted`／`expired`／確認過的 404） | 「已換到新的執行環境；舊環境已失效或被平台刪除，這次沒有搬工作檔，之前 VM 裡的工作檔可能已經不在了。」（總協調 2026-10-12 決定，待 Neal 確認） |
| 平台在同一個 session 換了版本（同一 SID 漂移旗標與通知待送位一起設定，第 4 點；dev 實際看過的就是這種） | 「平台把助理的執行環境換成了另一個版本（vX→vY）；FA 已記錄，相容就照常執行。」（vX＝SID 建立時綁的版本、vY＝觀察到的版本；總協調 2026-10-13 決定，待 Neal 確認）。由綁定列的通知待送位交付：每個 Agent 由第一個主人本人的回應顯示一次，不分對話，非主人呼叫者不顯示也不清除（第 4 點末段） |
| 平台重建了環境（`get_session` 的 `created_at` 改變；只是順便讀到的保底訊號，沒有保證的讀取點，可能漏掉；dev 叫醒不會改，目前沒看過） | 「平台重建了助理的執行環境，之前 VM 裡的工作檔可能不在了。」 |
| 版本不相容、這則訊息沒送出 | 「助理目前的執行環境和服務版本不相容，這則訊息沒有送出；助理換到新的執行環境後，再送一次即可。」 |
| 不相容的舊環境逾時仍在運作（第 7a 點逾時回報） | 「舊環境仍在運作，暫時無法換到新版本；這則訊息沒有送出，需要管理者處理。」（審查第 5 輪新增、第 6 輪改：FA 只留 log 與這個狀態，沒有另外轉交，所以不說「已轉請」；文案待 Neal 確認） |
| 釘在舊版本的工作無法接續（第 9 點） | 「這則訊息是較早的版本接下的，目前環境無法接續；請用新訊息再送一次。」 |
| 升級中，這則訊息先暫停（草案上線後） | 沿用現有「換版中」的同 RID 重送提示；補一句「助理正在閒置更新，訊息稍後自動送出」 |
| 有新版本在等（草案上線後） | 狀態欄位：「有新版本 vN，會在助理閒置時更新。」 |

工作台不顯示版本號以外的內部識別；API 回條帶前一個 SID、新 SID、理由、`files_migrated`，供稽核。

**待辦（追蹤）：本節的通知都要由工作台顯示，包括不搬檔那句；目前都不顯示。** 2026-10-06（`f56020a5`）工作台不再處理換環境，「ready 後顯示原句」隨之移除（`bridge.py` 的 P06 註解；`index.html` 的 `agent.environment` 只叫醒等待中的請求）。現在只有 API 回條帶 `files_migrated`／`files_omission_reason`。補回顯示列在 phase 2（遷移計畫表）；phase 0 不改 UI。

「每個對話顯示一次」需要記住哪個對話已經通知過，存在哪裡未定（生命週期表、草案已知未解問題 8）。決定部分只要求通知會出現，不依賴這個標記：換環境的通知由上面的待辦補回（工作台目前不顯示）；平台在同一個 session 換了版本，靠綁定列的通知待送位，由第一個主人本人的回應帶上、寫出後清除，不分對話（第 4 點末段）；平台重建（`get_session` 的 `created_at` 改變）只在順便讀到時附在那個請求的回應上，沒有保證會偵測到（第 4 點）；SID 換過之後無法接續的 RID，讀到時以之前的 SID 清單判斷、回通知句子，不需要另存標記（第 7a 點 (e)）；同一 SID 漂移後釘在舊版本的 RID 見第 9 點與已知未解問題 9。

2026-10-04 原句的「舊環境與檔案都保留」只到平台 30 天無活動後永久刪除 session 為止（來源已失效或被刪時改用上表「舊環境已失效」句）；草案 ② 上線後，長期副本是保留 30 天的 Blob 快照（Neal 決定 2）。實作 PR 調整這句時另請 Neal 確認文案。

### 9. 漂移怎麼處理（整理）

| 觀察 | 做法 |
|---|---|
| 醒來的版本≠紀錄、但相容 | 照跑（第 4 點第 6 步），記錄新版本；同一次 CAS 設通知待送位，由第一個主人本人的回應帶上第 8 點「換成了另一個版本」通知（第 4 點末段）。換到目標版本是另一件事（草案第 5 點），這裡不做 |
| 醒來的版本不相容（409 帶 `actual_version`，或舊 image 的 metadata；或 403 `NATIVE_TURNS_DISABLED` 且綁定列的 SID 沒變＝profile 不同） | 這一回合明確拒絕（確定沒派送，同 RID 稍後可重送）並通知；不相容的 VM 不 invoke、不搬檔。換環境：草案上線前靠第 7a 點（不相容的來源碰不到時棄用來源換新環境）。限制：若漂移的正是 selector 版本算出的 SID（A 的情況），現行 follow 沒辦法為同一版本另建 session，只能等發布新版本後的 follow 或草案機制；期間每個回合都明確拒絕，不進 409 迴圈 |
| 409 `NATIVE_SESSION_CHANGED`，綁定列的 SID 已換 | 不是漂移：第 4 點第 6 步「SID 已換」——只有確定沒派送才改送新 SID；舊 SID 上非終態的 RID 不可改送 |
| 409，SID 沒變，V_obs＝V_rec | 不是漂移，是綁定不一致：第 4 點第 6 步「不一致」分支 |
| 漂移，而舊 SID 上還有釘在舊版本的工作（排隊或已接受，`execution_version`≠V_obs） | Hosted 照舊拒絕執行（第 3 點）。決定（Neal 決定 8）：這些 RID 不改寫、不換 RID 重送，原 Hosted 回條保持原樣，請主人以新訊息重送（第 8 點）。**SID 換過之後**（換環境或棄用來源換新環境，第 7a 點）：回條的 `hosted_session_id` 在之前的 SID 清單，讀到時回「版本已變、無法接續」的通知，不需要另存標記，也不擋換環境。**同一 SID 相容的漂移**（V_obs≠V_rec、SID 不變、不換環境）：回條的 `hosted_session_id` 就是目前 SID，回條也沒有時間或執行版本欄位，API 分不出哪個 RID 釘在舊版本；判定方式屬草案（已知未解問題 9），判定出來之前這些 RID 照界線 7 以一般未決狀態回報，不叫主人重送。這些 RID **不擋換環境**：漂移過的 SID 變成 `idle` 或已失效後，換環境照第 7a 點適用條件 (iii) 不等它們，換完 SID 進之前的 SID 清單，讀到時照前一種回通知；換環境不依賴已知未解問題 9。不相容的漂移會換環境，換完就屬前一種。草案另有：怎麼結束對應的派送意圖紀錄、閒置升級時「不擋升級」的一般機制（已知未解問題 1） |
| `get_session` 的 `created_at` 改變（只在既有的 `get_session` 讀取順便比對，沒有保證的讀取點；叫醒不會改；漂移不靠它） | 記錄，並把通知附在讀到它的那個請求的回應上（第 4 點、第 8 點）；每個對話各一次的去重屬草案（已知未解問題 8）；照相容規則決定跑或拒絕 |
| metadata 讀不到、401／403、逾時、5xx | 照原錯誤處理，不推定漂移 |
| 404、`failed`／`deleted`／`expired` | 舊 SID 已不能用：不呼叫、不刪除；這一回合明確拒絕並通知。換新 session：草案上線前靠第 7a 點的棄用來源換新環境（適用條件 (ii)，不論相容與否；相容的來源要再讀確認一次；限制同上），之後由草案機制（④ 不搬檔）；通知用第 8 點「舊環境已失效」句 |

### 10. 不叫醒已回收 session 的規則（立即適用，phase 0）

- 任何「只為了檢查」的讀取，先用 `get_session`（不 invoke），只有 `active` 才可再呼叫 session 或讀它的檔案。
- `FollowFiles._source()` 與 `require_source_version()` 原本接受 `idle`：改成只接受 `active`；`idle` 記 `source_unavailable`，走 2026-10-04 的不搬檔路徑（API 回條記 `files_migrated=false`、`files_omission_reason`；工作台目前不顯示通知，見第 8 點待辦）。stop 前的 `_source_session_status` 讀取同理：stop 只送給 `active`。
- **窄例外：FA 自己 stop 之後的停止後核對。** 搬檔的最後一步要確認 stop 前後來源檔案沒變，只能在 stop 之後經 Files API 讀來源（`stopped_step`）。只在 **FA 自己的 stop 已被平台接受並記下**（`stop_started`，在 `stop_session` 成功之後才寫；`session_already_stopped` 不算）、而且這次讀回 metadata 是 inactive 時才讀；Hosted 在每一步 `source_stopped`／`list_stopped` 也再核 inactive。來源沒有這個紀錄就變 inactive（例如 FA 的 stop 失敗後被平台回收）＝被回收：不讀檔，以 `source_not_migratable` 不搬檔完成。依據：官方說 stop 結束 compute、保留檔案系統（[Manage hosted agent sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)）；Files 呼叫會不會重新配置 compute，官方沒有寫。代價：stop 已被接受、紀錄沒寫成（崩潰或寫入失敗）時，下一次當成被回收，不搬檔，但不叫醒。
- 舊版 `fa-chat-…` 對話（`behavior_work_policy.py` admission 的 `verify_session`）：`verify_session` 只讀 metadata，之後若原 SID 不是 `active`，不再為了驗證去呼叫它，回 409 `CONVERSATION_SESSION_UNAVAILABLE`；如何接續這些舊對話照現有明確 reset／新對話規則。接受的使用者代價：閒置超過逾時的舊對話要明確 reset（reset 用 `stop_session`，不 invoke）或開新對話。
- **metadata 的 `idle` 不代表沒有未完成的回合。** 官方說明：處理器在關機寬限期內停在安全點並 defer 時，response 或 task 會保持進行中，留給之後的 lifetime 接手；自己 stop 的 session 不保證會自行重啟（[How stopping a session affects an in-flight turn](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#how-stopping-a-session-affects-an-in-flight-turn)）。所以 `idle` 只代表「compute 不在、現在不能碰」；有沒有未完成的工作，只能看 API 端的開放 RID／回條紀錄。現行 API 沒有持久的開放 RID 紀錄（只有記憶體 LRU，第 3 點），這是草案已知未解問題 1 的起點。
- 注意：`get_session` 本身不會喚醒 compute 是 dev 觀察，官方文件沒有寫。**dev 驗證項**：① **PASS（2026-10-13，release 28 dev 部署前；[紀錄](../progress.md#adr0009-phase0-merged-1013)）：** 相隔約 50 秒取兩次快照，對 24 個 session 各做 list＋get，沒有任何欄位改變，`get_session` 不喚醒、不改 `last_accessed_at`／`created_at`（仍只是 dev 觀察，官方沒寫）；② **未做：** 對一個 FA 自己 stop 的 session，在一次 Files GET 前後各讀 `status`／`last_accessed_at`／`created_at`，確認上面的窄例外不會重新配置 compute。
- **已接受的殘餘風險（phase 0）：** (a) 部署前已在途的 follow：舊程式在送 stop 之前就寫了 `stop_started`，之後 stop 失敗、來源又被回收，新程式分不出這筆紀錄是舊程式寫的，`stopped_step` 會憑它讀一次該來源的檔案（共用 README「§10 的窄例外與已接受風險」同樣記載）。(b) 窄時窗：平台已接受 FA 的 stop，但 `stop_started` 還沒寫成（崩潰或寫入失敗），重試時平台仍回報 `active`，`before_stop` 會對一個 FA 剛 stop 的來源呼叫 `verify_source`（main 不會，因為 main 是先記錄再送 stop）。(a)(b) 都接受為風險；若 dev 證據顯示真的有被叫醒，已知的修法是送 stop 之前先寫 stop 意圖標記。(c) 讀與呼叫之間的時窗：metadata 讀到 `active` 之後、真正呼叫或讀檔之前，session 剛好閒置逾時變 `idle`，這次呼叫就會叫醒它（#815 只在讀 metadata 時檢查，未涵蓋這個時窗）。接受，範圍有限：時窗只有一次讀取到一次呼叫之間，每次最多多叫醒一次，代價和一般回合叫醒相同。
- **phase 0 的完成條件：沒有任何路徑叫醒非 `active` 的 session**（上面的窄例外只讀 FA 自己 stop 的來源）。實作是 #813（已合併，`8732d7b7`）**加上**本點「follow 與舊對話只碰 `active`」的 #815（已合併，`01e0fdb2`），**兩者都已合併，phase 0 的程式部分完成（2026-10-13）**。只合併 #813 不算完成：#813 只處理 prepare 定位與 API 舊 reader 快取，follow 檢查舊來源時仍會叫醒 `idle` 的 session；#815 補上這一塊。上面的 dev 驗證項 ① 已 PASS，只剩 ② 未做，是待補的 dev 證據；合併不代表已部署或已在 dev 驗證。第 8 點的通知顯示不屬 phase 0（見第 8 點待辦）。

### 11. AGENTS 界線 2 的措辭（Neal 已同意，本 PR 已改 AGENTS.md）

原文：「……VM 回收或換版時從 checkpoint 明確接續，不偷偷換新環境。」

改成：

> 每個 Agent 一個 Hosted session（一台 VM）……compute 可更換。**版本以該 session 實際執行的版本為準，相容就照跑；換到新版本只在助理閒置時做，事前暫停新回合、有工作在跑就延後，並記錄、通知，從對話歷史與 checkpoint 明確接續**；平台自行重建環境時也照實記錄與告知，不偷偷換新環境。

「只在閒置時換」的做法就是草案的閒置升級機制；它定稿上線前，換環境仍走現行 follow（第 7 點）。AGENTS 括號裡的「現行 follow」指第 7 點的換環境入口，包括來源碰不到時的棄用來源換新環境。為了不讓讀者把 AGENTS 這句當成已上線的行為，AGENTS.md 在上面那句最後（句號前）加了括號「（閒置升級機制定稿前，換環境仍走現行 follow；見 ADR-0009 §7、§11）」，Neal 同意的句子本身不改。

「平台自行重建環境時也照實記錄與告知」在決定部分的兌現方式（2026-10-13 更正後）：dev 實際看過的平台自行變動是同一個 session 換了版本，靠漂移 CAS 同時設的綁定列通知待送位，由第一個主人本人的回應帶上換版通知、寫出後清除（第 4 點末段、第 8 點）；`get_session` 的 `created_at` 改變只是順便讀到的保底訊號，沒有保證會偵測到。對 AGENTS 界線 2 這句的意義：告知保證的是「主人本人至少看到一次」（網路上遺失的回應除外），不是每個入口、每個對話都告知；非主人入口與跨對話的補發屬草案（已知未解問題 8）。

## 遷移計畫（每一步一個小 PR，各附測試）

| Phase | 內容 | 主要測試（離線替身；真平台另驗） |
|---|---|---|
| **0（立即）** | #813（不再呼叫 selector 已不選的舊 SID；版本定位器取選定版本否則最高版本）＋第 10 點「follow 與舊對話只碰 `active`」（#815）；兩者都合併才算完成（#813 已合併 `8732d7b7`，#815 已合併 `01e0fdb2`；程式完成，dev 驗證項剩 ②） | 舊 SID `idle` 時 follow 零呼叫、零 Files 讀取、走不搬檔（API 回條記 `files_migrated=false`）；FA 的 stop 失敗後被回收的來源不讀檔；`active` 時照舊搬檔 |
| 1 | API 綁定列：認領現有 SID（同時寫這個 SID 建立時綁的版本，第 2 點；不多一次寫入）、記錄觀察到的實際版本（不同於建立時綁的版本時同一次寫入設定漂移旗標與通知待送位；舊 image 要等 VM 接受 expected＝V_obs 才寫；主人本人的第一個回應帶上第 8 點換版通知、寫出後 CAS 清除，工作台顯示屬 phase 2，所以清除要等 phase 2 才啟用）；`get_session` 的 `created_at` 只在既有流程本來就讀 `get_session` 時順便記下（第 2 點），不另加讀取、沒有保證的讀取點。不改任何回合的結果；新增的 Table 寫入只有每個 Agent 認領一次、觀察到不同版本或順便讀到不同的 `created_at` 時各一次，每次換環境 commit 後一次（目前 SID、新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`、清除漂移旗標與通知待送位、之前的 SID 清單，第 7a 點 (f)），以及（phase 2 起）通知清除一次，送出路徑本身不多寫 | 讀列不呼叫 session；CAS 衝突重讀；漂移只產生紀錄；旗標與通知待送位同一次 CAS 設定，舊 image 在 VM 接受 expected＝V_obs 之前兩者都不寫；非主人呼叫者（routine、交辦、群組、HITL）不帶不清，主人回應帶上並在寫出後清除，寫出前或清除前崩潰→下一個主人回應再帶；認領在請求外設定、第一個主人回應帶上；旗標已設定後不再設待送位；漂移後漂回，旗標仍在；follow commit 後綁定列目前 SID＝新 SID、建立時綁的版本＝commit 結果的 `hosted_agent_version`、旗標與待送位清除、來源 SID 在之前的 SID 清單 |
| 2 | 工作台補回第 8 點的通知顯示（含不搬檔那句；2026-10-06 起不顯示，見第 8 點待辦）。相容規則：profile 換成 `foundry-native-v2`，`HostedVersions.current` 與各處寫死的 `"foundry-native-v1"` 比較改集合判斷；schema 快照測試（wire、state store、SDK／runtime 固定版本、checkpoint codec 清單）；Hosted 在 dispatch 前的拒絕加 `not_dispatched`、`reason`、`actual_version`，`NativeSessionState` 加 `actual_version`；`replaced()` 改比 SID（不是目前 SID、也不是 `pending` 的目標 SID）；每個 state store 項目明寫 profile，舊 image（v2 起）讀新 profile 項目明確拒絕；v1→v2 遷移的三項必要範圍（第 7 點）：來源 403 `NATIVE_TURNS_DISABLED` 當來源拒絕、有綁定列就算準備過、來源碰不到時棄用來源換新環境（第 7a 點，實作 PR 可另寫路徑並重核 `follow_session` 的等待原因） | v1 一律不相容；schema 變但 profile 沒變→測試失敗；follow 進行中，`pending` 目標 SID 的 `_RECORD_READS` 照常讀、不 409；v2 寫的項目都帶 profile，沒標的項目當 v1 時代讀；較舊版本 session 相容時照跑；`execution_version` 不同的排隊／已接受輸入照舊拒絕；v1 來源回 403→寫 `source_invocation_refused`、不搬檔換到 v2；還沒有 v2 session、有綁定列→送出時照樣 follow、不呼叫任何 session；**第 7a 點驗收表 (a)～(q) 全部列為測試**（含讀來源上的 RID→以之前的 SID 清單判斷、得到「版本已變、無法接續」的通知；新 session 上進行中的 RID 不得到這句；commit 後 CAS 前崩潰→重跑切換補寫；不相容 SID 在每個 API 入口零呼叫；相容來源暫時 404 不棄用；同一 SID 漂移（含漂移後漂回）的 `idle` SID 不卡換環境、從沒漂移過的新 SID 照現行等待）；v1 來源 `active`→只重讀 metadata，不 invoke、不 stop、不切換，逾時留專用 log 並轉操作者決定 |
| 3 | 送出流程改用綁定列與 V_rec（第 4 點），含漂移重送一次、核回條與「不一致」分支；`_same_session`（`behavior_native_turn_api.py` 257–261，#813 刻意保留 409）與 `NativeConversation.execution_hosted_session_id` 改以綁定列的目前 SID／之前的 SID 清單判斷；#813 的 selector 快取判斷改以綁定列為準。**保留** `_follow_before_admission` 與 `follow_all`（停用屬草案 phase 4、6） | 醒來版本不同但相容→同 RID 只派送一次；409 無 `not_dispatched`→先讀綁定列再讀 RID；舊 SID 上非終態的 RID→不改送新 SID、明確失敗；409 且 V_obs＝V_rec→不記漂移、交給 follow；403 `NATIVE_TURNS_DISABLED`＋SID 沒變→明確拒絕；follow 換環境後既有對話在新 SID 接續 |
| 3b | 第 7 點的部署規則：CI 部署前相容閘門（v1→v2 遷移完成後接上）、綁定列引用的版本與 ACR image 不刪、不讓保留政策清掉 | 支援集合缺被紀錄的 profile→部署拒絕；閘門只讀綁定列與 definition，不呼叫任何 session；綁定列引用的版本不在任何清除清單 |

\#813 涵蓋 phase 0 的「API 舊 reader 快取」與「版本定位器」兩項，屬**過渡做法**：它用 selector 判斷快取是否過期，版本定位器在選定版本沒有 session 時退回「版本號最大的」。G 之下版本號不單調（第 2 點），這個退路只在 phase 3 之前使用；phase 3 起定位一律查綁定列，selector 只當換環境的目標。A 節的世代上限 3、`pending_work` 立即失敗等規則不實作；替代做法（延後、否決）屬草案。

## 協調物件生命週期

只列本 ADR 用到的協調物件。依據只採 `origin/main` 程式或 Microsoft Learn；填不出來的格子寫「未定」，有「未定」的物件只能放在草案。「決定」部分新增的是第一列與第三列「漂移通知待送位」；第二列的 Hosted `pending` 是第 7 點的 v1→v2 遷移照現行 follow 寫的，決定部分原樣沿用、不新增用法；實作 PR 若為棄用來源換新環境另寫路徑而不寫 `pending`，照實更新這一列。其餘各列的「未定」格子都解決，草案才定稿。

| 物件 | 所在 | 誰建立 | 誰結束 | 何時過期 | API 崩潰後誰收拾 | SID 是 `idle` 或 404 時 |
|---|---|---|---|---|---|---|
| 綁定列的位置與版本欄位（第 2 點） | 決定 | API：phase 1 認領（Table Insert Entity，已存在時回 409 就讀回，不覆寫）；之後只以 CAS 更新（含每次換環境 commit 後寫目前 SID、新 SID 建立時綁的版本、清除漂移旗標與通知待送位、之前的 SID 清單）。同一 SID 漂移旗標只在兩處設定：認領時讀到的版本不同於建立時綁的版本，或第 4 點第 6 步的漂移 CAS（與 V_obs 同一次寫入）；設定是冪等的，已設定再寫仍是設定；從未設定變成設定的那次寫入同時設通知待送位（見第三列） | 不結束；FA 不刪（界線 3 不提供刪除） | 不過期 | 每次寫入都是單列 CAS，寫成或沒寫，沒有半套狀態。漂移 CAS 之前崩潰：V_rec 與漂移旗標都沒變，下一次送出重新觀察、再寫同一個 CAS。follow 已 commit、綁定列沒跟上時：下一次送出時的 follow 或 `follow_all` 以同一來源重跑切換，拿到已 `ready` 的同一結果（`native_environment.py` 587–592）後補寫（第 7a 點）；V_rec 相容時，送出也可能先走第 4 點第 6 步「不一致」分支，同樣交給送出時 follow 收斂 | 讀列不呼叫 session；`idle` 時不寫、不叫醒；404 只在送出時得知，記理由、明確拒絕，不刪列 |
| Hosted `agent` 紀錄的 `environment.state="pending"`（現行 follow 已在用；草案 U2 另用） | 現行 follow 已在用，「決定」不新增用法；新用法（U2）只在草案 | Hosted，經 retarget／follow commit 指令（`native_environment*.py`） | 現行：commit 成 `ready`，或下一次 follow 以 retarget 改指新目標（`behavior_native_sessions.py` 645–660）。延後或失敗時撤回：**未定**（沒有撤回指令） | 未定（現行沒有過期） | 未定 | 寫入要 invoke session，草案規定 `idle` 不寫；SID 404：未定 |
| 漂移通知待送位（綁定列欄位，第 2、4、8 點） | 決定 | API：與同一 SID 漂移旗標同一次 CAS 設定（第 4 點第 6 步的漂移 CAS；認領時讀到不同版本則與認領同一次寫入）；舊 image 要等 VM 接受 expected＝V_obs 才寫 | API：第一個主人本人的回應（工作台或 Teams 主人回覆）帶上通知、寫出後以 CAS 清除；非主人呼叫者（routine、Agent 間交辦、群組、HITL 提醒）不清 | 不過期；SID 換掉時隨漂移旗標一起清除 | 位元留著，下一個主人回應再顯示（至少一次）；清除遺失後可能重複顯示一次，接受；回應寫出後在網路上遺失則漏掉 | 不呼叫 session；`idle` 時不寫、不叫醒；404 不受影響（位元留著，SID 換掉時清除） |
| 派送意圖紀錄 | 草案 | API，送出前 | API，拿到終態回條時；寫入有歧義、逾時／5xx、API 崩潰、沒有 poller 時：**未定** | 未定 | 未定 | 未定（`NativeRead` 不能用在 `idle` 或不相容的 SID） |
| 綁定列的升級欄位、租約與預留的新 SID（`upgrading`、目標、預留 SID、嘗試次數、延後起點） | 草案 | API，U2 CAS | API，U9 釋放；U3／U5／U6 失敗時 CAS 釋放 | 租約建議 15 分鐘；到期後誰接手：未定（reconciler 預設關） | 未定 | 未定 |
| Blob 工作檔快照（②） | 草案 | API，U4 | 保留 30 天（Neal 決定 2）；到期由誰、用什麼機制刪：未定（刪除另需批准） | 30 天 | 做到一半的快照由誰標成不完整並清理：未定 | U4 途中變 `idle`／404：立刻停、標不完整、不發布、改走 ④（第 6 點） |
| 「每個對話通知一次」的標記（第 8 點；跨對話去重與補發，以及第 4 點的 `created_at` 保底通知。漂移換版通知由上面的「漂移通知待送位」涵蓋） | 草案（決定部分只要求通知會出現，見第 8 點） | 未定（現行沒有每個對話的通知紀錄；工作台目前不顯示換環境原句，見第 8 點待辦；補回時再決定記在哪） | 未定 | 未定 | 未定 | 不呼叫 session，不受影響 |
| 否決版本標記（綁定列＋專案層否決表） | 草案 | API，U6 累計失敗或 crash-loop 判定 | 發布更新的版本時解除；由誰寫：未定 | 不過期 | 未定 | 不呼叫 session，不受影響 |

## 草案：閒置升級機制（未定稿）

**本節不是已確認的決定。** Neal 2026-10-12 已定方向（「Neal 已決定」第 2～5、7 項，以及第 8 項的一般機制），但「協調物件生命週期」表裡本節物件的「未定」格子、以及本節最後的「已知未解問題」解決之前，本節不定稿、不實作；在那之前換環境照現行 follow（第 7 點）。審查第 2 輪指出的機制問題都列在已知未解問題，不在本文宣稱已解。

### 5. 閒置時升級（Grok 換 bundle 的 Foundry 版）

**閒置的定義**（沿用 A 節第 5 點 `pending_work` 的清單，只看 API 端紀錄，**不呼叫 session**）：目前 SID 上沒有尚未結束的派送意圖紀錄（現行 API 沒有任何持久的開放 RID 紀錄，所以這是判斷「有沒有尚無終態回條的 RID」的唯一 API 端依據；它的結束與過期未定，見已知未解問題 1）、沒有未決的 HITL／提問、沒有 pending 的 `environment`／`following`、沒有 `task_active`／`task_unconfirmed`、沒有正在送達的 routine 或 Agent 間交辦；紀錄不足以判斷時當作「不閒置」。**metadata 是 `idle` 不算閒置的證據**：deferred 的回合可能仍在進行中，留給之後的 lifetime 接手（第 10 點）。

| 步驟 | 做什麼 | 對應 Grok |
|---|---|---|
| U0 觸發 | 綁定列的 V_rec ≠ 目標版本、目標版本相容且 `active`、沒被否決 | 新 bundle 已 staged |
| U1 檢查閒置 | 不閒置就延後，記下延後起點，下次觸發再看 | 有回合在跑就拒絕切換 |
| U2 取租約、暫停 | CAS 把綁定列改成 `upgrading`（目標、預留的新世代 SID、嘗試次數、租約到期，建議 15 分鐘）。從此任何 API 副本都寫不進派送意圖紀錄（寫入方式見已知未解問題 3），新回合、routine、交辦都收到既有的 503 `AGENT_VERSION_FOLLOWING`＋`not_dispatched`；工作台與各來源照既有方式**同一個 RID** 稍後重送，API 不自己開重送迴圈。現行的暫停（`behavior_native_sessions.py` `admission`）只在單一程序記憶體裡，擋不住其他副本，所以不能單靠它。另外，**只在舊 SID 是 `active` 且相容時**，經既有 retarget／follow commit 指令把 Hosted `agent` 紀錄的 `environment.state` 寫成 `pending`（目標＝預留的新 SID），讓 Hosted 自己拒收新工作（`native_turns.py` 670–672），擋住還沒讀綁定列的舊 API 副本；舊 SID 是 `idle` 時不寫（寫就會叫醒它，違反第 10 點）。這個 `pending` 何時寫、怎麼撤回未定（現行沒有撤回指令），見已知未解問題 2 | `pauseTurnsForUpgrade`、`suspendWakes` |
| U3 再確認閒置 | 取得租約後再檢查一次（防 U1～U2 之間剛寫入的意圖紀錄）；不閒置就 CAS 釋放租約、延後 | — |
| U4 工作檔 | 見第 6 點：只有舊 SID 的 metadata 狀態是 `active` 時才搬 | 檔案留在同一顆磁碟 |
| U5 建立新 session | 新世代 SID：`hash("fa-native-session-v2", 範圍材料, g)`，仍以 `fa-native-` 開頭，不再把版本算進去（理由同 A：可重算，建立後沒收到確認時能用同一 SID 讀回；同一 endpoint 內呼叫端 ID 唯一，兩個程序同時建立只會成功一個）。`create_session`＋`version_indicator`＝目標版本；先 CAS 預留世代再建立，沒收到確認時用同一 SID 讀回（A 節第 2 點的預留／收尾規則照用） | stage 新 bundle |
| U6 健康檢查 | 帶 expected＝目標版本送 `NativeSessionRead`（VM 版本不同就 409；新 image 另回 `actual_version`）、profile 在支援集合、`prepare` 成功。各開放對話 checkpoint 的 codec 怎麼檢查未定：API 沒有 codec 標記（第 3 點），見已知未解問題 7 | 新 bundle 起得來 |
| U7 還原工作檔 | 有快照才做；依 manifest 與 hash 核對 | — |
| U8 切換 | CAS 更新綁定列（新 SID、V_rec＝目標、舊 SID 加進之前的 SID 清單、`files_migrated`、升級 ID），並經既有 commit 指令寫 Hosted 的 `agent` 紀錄。這兩處寫入不是同一個交易，必須可重入、可從 `upgrading` 中斷處接續（已知未解問題 6）。**stop 前最後確認**：重讀意圖紀錄與舊 SID metadata；舊 SID 是 `active` 時，相容就經 `NativeSessionRead` 確認沒有 `admission_request_id`、沒有執行中的工作，不相容（不能 invoke）就等它變 `idle`。`idle` 只代表 compute 不在，**不代表**沒有進行中的回合（第 10 點），所以 stop 與否只看開放意圖紀錄；有任何未結束的工作就**延後 stop**，等到終態。官方說明 stop 會結束 container，處理器沒在寬限期內停在安全點，回合就被強制結束，自己 stop 的 session 也不保證自行重啟（[How stopping a session affects an in-flight turn](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#how-stopping-a-session-affects-an-in-flight-turn)），所以這一步不能省。之後 `stop_session` 舊 SID（FA 不刪除；官方說 stop 保留檔案系統，但平台在 30 天無活動後會永久刪除 session）。U8 之後舊 SID 的 RID 讀取會被 `replaced()` 拒絕（`native_turns.py` 2027–2029）；新 SID 讀得到舊 SID 的回條，但接不了舊 SID 上仍是 `accepted` 的工作（改送會 409，第 4 點第 6 步），所以舊 SID 上的 RID 全部到終態之前不切換 | swap |
| U9 恢復 | 釋放租約；同 RID 重送的訊息進新 SID；每個對話的下一個回合照 CTX-03 載入 checkpoint、顯示一次通知 | 重讀逐字稿、接回工作 |

**有界重試、否決與退回：**

- 同一個（Agent, 目標版本）最多嘗試 3 次（第一次＋重試 2 次），間隔加隨機抖動。U5／U6 失敗時舊 SID 仍是目前環境，不受影響，租約釋放。
- 結果不明（例如建立沒收到確認、工作檔還原 `NATIVE_FILES_UNKNOWN`）：保持 `upgrading` 狀態等核對，不算失敗、不換 ID 重做（界線 7）；租約到期後誰接手核對未定（生命週期表）。
- 同一目標版本累計 3 次健康檢查失敗：在綁定列（與專案層的版本否決表）標記**否決**，之後不再自動升級到它；Agent 留在目前相容的版本。工作台狀態顯示「新版本 vN 沒通過檢查，助理繼續用 vM」。發布更新的版本會解除。
- **crash-loop：否決＋自動退回前一個版本＋通知**（Neal 決定 3）。判定：切換後 30 分鐘內，新 SID 上至少 3 次崩潰類失敗，且分屬**至少 3 個不同對話與 RID**。只算：回合中 Hosted 程序退出或重啟、runtime 起不來而回合無法開始、`prepare` 或 checkpoint 載入失敗。不算：同一 RID 的重試、平台關機（重新部署、縮容、閒置回收）、工具子程序被殺、模型或工具錯誤、使用者取消、逾時、401／403、限流。退回走同一套升級流程，目標改成前一個版本。**Hosted 用哪個訊號區分「程序崩潰」與「平台關機」未定**：現行 Hosted 沒有這種訊號（`native_turns.py` 的 `_recovery_error` 只有 `task_active`／`task_unconfirmed`），見已知未解問題 5。
- **被否決的目前版本不受一般閒置門檻限制。** crash-loop 會留下未確認的工作，照一般定義永遠不會閒置，退回就會卡死。所以目前版本被否決時，U1／U3 只要求「沒有可確認**正在執行**的回合」：`active` 且相容時經 `NativeSessionRead` 讀；`idle` 時無法得知，**不能**當成沒有（第 10 點）——這種情況怎麼放行未定，見已知未解問題 1 的選項 (a)。未確認的工作保留原 RID 與未決狀態，不改寫、不換 RID 重送，並通知主人。
- 不中斷正在跑的回合。延後超過 6 小時仍不閒置：不強制、不提供「現在就更新」按鈕，只在工作台顯示一次「有新版本等待助理閒置後更新」（Neal 決定 4）。

### 6. 工作檔要不要帶過去

**原則：不為了檢查或搬檔去叫醒已回收的 session**（第 10 點）。叫醒可能被平台綁到任意版本，也是另一筆費用。所以舊 SID 只有在 `get_session` 讀到 `active`（compute 還在跑）時才可呼叫或讀檔；`idle` 或其他狀態一律不碰。

| 方案 | 做法 | 優點 | 缺點 |
|---|---|---|---|
| ① 切換當下搬到新 session | 沿用現有 `FollowFiles`（manifest＋hash，session 對 session），只在舊 SID `active` 時做 | 程式現成、官方 Files API | 要先建好新 session（數分鐘）才能搬，期間舊 compute 可能逾時回收 |
| **② 切換當下先存到 Blob，新 session 再還原（Neal 決定 2）** | U4：舊 SID `active` 時，用舊 VM 的 helper 產生 manifest（現有 `fa-workspace-files-v1` 範圍），API 經 Files API 下載各檔存進該 Team 的 Blob（ADR-0002「Blob 保存成果／固定版本」，ADR-0006 經 API 薄轉送）；U7 再上傳到新 session 並核 hash | 快照在建立新 session **之前**完成，不受新 session 建立時間影響；舊 SID 可立刻 `stop`，省費用；快照可保留作備份 | 新增 Blob 存放與保留規則 |
| ③ 每個回合結束都存一次 | Hosted 在回合結束時把工作區增量存 Blob | 任何時候升級都有最新檔案，包括 compute 已回收的情況 | 每回合多成本與延遲；新增最多 |
| ④ 不搬，明說 | 沿用 2026-10-04 系統政策 | 最簡單 | 使用者失去工作檔 |

**Neal 決定 2：② 為主，④ 為退路。** 為了讓 ② 大多數時候能做，升級的主要觸發是「回合結束後的短暫空檔」（第 12 點），那時舊 compute 還在。舊 SID 已是 `idle`、舊版本不相容（不能 invoke 它的 helper，例如所有 `foundry-native-v1` 版本）、舊 image 沒有 files helper、或快照有任一步明確失敗：走 ④，`files_migrated=false`、理由記錄。FA 不刪舊 SID 與檔案，但平台在 30 天無活動後會永久刪除 session，所以**長期副本只有 ② 的 Blob 快照**。③ 留作日後若 ② 的成功率太低時再提。

② 的快照規則：

- **每次 Files 呼叫前都重讀舊 SID metadata**，確認仍是 `active`。快照途中變成 `idle`，就立刻改走 ④，不再對舊 SID 發任何 Files 呼叫；做到一半的 Blob 快照標成不完整、不發布、不還原。官方沒說 Files 呼叫會不會重設閒置計時，FA 不依賴它。
- **大小上限沿用現有 `fa-workspace-files-v1`**（`native_files_wire.py`：單檔 32 MiB、總計 64 MiB、最多 2000 個項目；Neal 決定 2）。單檔 32 MiB 已低於官方 Files API 單檔上傳上限 50 MB（[Session file operations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#session-file-operations)），所以 U7 上傳不會撞到平台上限。manifest 超過任一上限：不做部分快照，整次走 ④ 並記理由。
- **快照保留 30 天**（Neal 決定 2）。到期由誰、用什麼機制刪除未定，刪除仍需另外批准（生命週期表）。

### 12. 誰觸發升級，以及部署不再 follow（原第 7 點前半）

- **主要觸發：回合結束時。** API 記下某 Agent 最後一件未決工作的終態回條後，若 V_rec ≠ 目標版本，等一小段安靜時間（讓主人的快速追問先進來）再跑 U1。安靜時間必須明顯短於該版本的 `idle_timeout_seconds`（官方範圍 120～3600 秒、預設 900；FA stage 與 `profile.defaults.json` 是 3600，dev 10-12 起 3540）：建議取 120 秒與閒置逾時四分之一的較小值。這時舊 compute 多半仍 `active`，可以帶檔案；不是的話照第 6 點走 ④。
- **備援：API 背景 reconciler。** 每 15 分鐘加隨機抖動掃一次綁定列，同一 Agent 以綁定列租約防止多個 API 副本同時做（和 [ADR-0006 的 API 排程時鐘](0006-team-trust-boundary-grokbot-mode.md#api-routine-clock) 同一類背景工作，預設關、依環境開）。它只處理：(a) V_rec 不相容的 Agent（反正不能跑，走 ④ 不搬檔升級並通知）；(b) 舊 compute 仍 `active` 但錯過回合結束觸發的 Agent。相容且 compute 已回收的 Agent **不主動升級**，等它下一個回合結束再升（Neal 決定 5）。
- **部署：** 部署只發布版本並設定 selector（目標版本），**不再**在部署當下 `follow_all`；送出時也不再 `_follow_before_admission`。這兩項只在本草案定稿後停用：送出時 follow 在 phase 4、部署時 `follow_all` 在 phase 6（第 7 點）。stage 部署成功不再等所有 Agent 換完。

### 草案的實作階段（phase 4～6）

| Phase | 內容 | 主要測試（離線替身；真平台另驗） |
|---|---|---|
| 4 | 閒置升級 U0～U9（派送意圖紀錄、租約、暫停、建立、健康檢查、切換、恢復、否決、crash-loop 自動退回），先做 ④ 不搬檔；同時停用送出時 `_follow_before_admission` 的「selector 動了就 follow」 | 有未決工作→延後不切；租約內新回合收 503＋同 RID 重送；3 次失敗→否決；crash-loop（3 個不同對話與 RID）→否決並退回；切換後 checkpoint 接續、通知一次 |
| 5 | 工作檔 ②：Blob 快照與還原（保留 30 天） | 只在 `active` 做；hash 不符／不明→不發布、`files_migrated=false` 或保持待核 |
| 6 | 觸發與部署：回合結束觸發、背景 reconciler（預設關）、`ci_deploy` 拿掉 `follow_all`（部署前相容閘門與版本保留已在決定的 phase 3b） | 部署不呼叫任何 session；部署成功不等所有 Agent 換完 |

### 已知未解問題（審查第 2～4 輪；定稿前要解）

1. **過期的派送意圖紀錄會讓 Agent 永遠不閒置。** 寫入結果有歧義、逾時或 5xx、API 崩潰、沒有 poller 時，意圖紀錄沒有結束路徑；`NativeRead` 又不能用在 `idle` 或不相容的 SID（第 10 點、第 3 點），所以也無法核對。metadata `idle` 不能拿來當結束依據（第 10 點）。選項：(a) V_rec 不相容時放寬閘門：不相容的 VM 本來就收不了新工作，只要求沒有可確認正在執行的回合；(b) 超過「回合上限的 N 倍」、且 metadata 是 `idle` 或 404 的意圖紀錄，以「版本已變、無法接續」結束（原 Hosted 回條不改；這是接受風險，因為 `idle` 不代表沒有進行中的回合）；(c) Hosted 在回合到終態時主動回報 API。另外，第 4 點第 6 步改送新 SID 時，舊意圖與新意圖誰先關、誰先開也未定（建議先開新、再關舊，不留被誤判成閒置的空窗）。Neal 決定 7：先定義這些再做。
2. **Hosted `pending` 不會被撤回。** U2 寫的 `pending` 在 U3 延後或 API 崩潰時沒有撤回路徑；`native_environment*.py` 沒有撤回指令；`pending` 期間 `native_turns.py` 670–672 對所有新工作回 503。選項：只在 U3 再確認之後才寫 `pending`；每次釋放或租約到期（含 reconciler）都撤回；把撤回指令列為 phase 4 的必要範圍。
3. **意圖紀錄的寫入方式與 ETag 競爭。** Azure Table 的同 partition 交易只支援 Insert／Update／Merge／Delete／InsertOrReplace／InsertOrMerge，同一實體只能出現一次，沒有「只檢查條件、不寫」的操作（[Performing entity group transactions](https://learn.microsoft.com/en-us/rest/api/storageservices/performing-entity-group-transactions)）。要用綁定列當柵欄，交易就得帶 If-Match 改寫綁定列，綁定列的 ETag 每次送出都會變，同一 Agent 的並行送出（不同對話、routine、交辦）互相衝突。沒有在升級時回 `AGENT_VERSION_FOLLOWING` 會誤導；需要另一個可重試的碼（名稱未定）與確定的寫入方式。
4. **滾動部署時的舊 API 副本。** G 之前的 API 副本不讀綁定列、不寫意圖紀錄；舊 SID 是 `idle` 時 U2 不寫 Hosted `pending`，換版期間這些副本沒有被擋住。做法未定（例如 G 第一版先上線、確認所有副本都讀綁定列後才啟用 U0～U9）。
5. **crash-loop 的崩潰訊號。** Neal 決定 3 的判定需要區分程序崩潰、平台關機與工具子程序被殺；Hosted 現在沒有這種訊號。用哪個訊號、在哪裡記，未定。
6. **U8 兩處寫入的可重入。** 綁定列 CAS 與 Hosted `agent` 紀錄 commit 不在同一個交易，中斷後要能從 `upgrading` 接續、重送 commit 不出錯；現行 commit 指令在重送時的行為本文未核。決定部分已有不一致分支（第 4 點第 6 步），但草案的接續方式未定。
7. **U6 的 codec 檢查。** API 沒有每個對話的 codec 標記（第 3 點）。選項：API 在接單時持久化標記並回填既有對話；或 U6 在新 session 對每個開放對話做唯讀載入檢查。未定。
8. **「每個對話通知一次」的標記。**（審查第 3 輪；PR #816 審查第 2 輪縮小範圍）第 8 點的表頭說每個對話顯示一次。決定部分的換版通知已改由綁定列的通知待送位交付：每個 Agent 一個，被第一個主人本人的回應清掉（第 4 點末段），所以不分對話去重、不需要每個對話的紀錄；`created_at` 保底通知仍只附在偵測到它的回應上。**仍是草案的：** 跨對話去重要不要改成每個對話各顯示一次（存在哪裡：API 綁定列、對話紀錄或工作台）、非主人入口看到時要不要補發、換環境與 `created_at` 通知要不要也用待送位、回應在網路上遺失時是否補送，都未定（生命週期表）。
9. **同一 SID 相容漂移時，釘在舊版本的 RID 怎麼判定。**（審查第 4 輪）SID 沒換時，回條的 `hosted_session_id` 就是目前 SID；`NativeReceipt` 沒有時間或執行版本欄位；Hosted 拒絕釘選的輸入時（`native_turns.py` 463–464、1595–1596）只對那次執行回 409、不碰回條。所以 API 分不出「還在跑」與「永遠不會跑」，第 9 點的通知在這種情況無法判定。選項：(a) 新 image 在回條以外另記接單時的執行版本，API 與綁定列的版本觀察紀錄比對；(b) 新 image 拒絕釘選輸入時另寫一筆側紀錄（不改回條，Neal 決定 8 不變）；(c) 接受無法判定，照一般未決狀態回報。未定。這個問題只影響**換環境之前**能不能通知；換環境本身不等這些 RID（決定部分第 7a 點適用條件 (iii)），所以不會卡住 Agent。

## 平台未知，以及 FA 怎麼保持安全

- 叫醒時綁哪個版本、叫醒後版本變了的情況是否等同 resume（因而 `$HOME` 照常還原）、`get_session` 是否喚醒：官方都沒寫。FA 不依賴它們：版本以 VM 自報為準、漂移由 expected-version 偵測、同一 session 的版本變化由 `version_indicator` 比對的漂移旗標偵測、以通知待送位在第一個主人本人的回應告知，`get_session` 的 `created_at` 改變（換了物件）只是順便讀到的保底訊號、讀到才告知，搬檔只碰 `active`。
- 平台若日後保證「叫醒不換版」，第 4 點的漂移分支只是不再觸發，不需拆掉。
- Neal 決定不向 Microsoft 回報這個漂移行為（總協調 2026-10-12 轉述）。

## Neal 已決定（2026-10-12 19:53）

Neal 2026-10-12 19:53（+08）回覆「都同意」，以下 8 項照當時的建議定案（編號沿用上一版「待 Neal 決定」）。第 1、6 項與第 8 項的核心落在「決定」部分；第 2～5、7 項與第 8 項的一般機制是草案的方向，草案定稿前不實作。

1. **採用 G。** 「決定」部分改為已確認；AGENTS 界線 2 改成第 11 點的措辭（本 PR 已改 AGENTS.md）。草案部分仍是草案，生命週期表的「未定」格子與已知未解問題解決後才定稿。
2. **工作檔：② 切換當下存 Blob，④ 不搬檔當退路。** 大小上限沿用 `fa-workspace-files-v1`（單檔 32 MiB、總計 64 MiB、最多 2000 個項目）；快照保留 30 天。（草案）
3. **crash-loop：否決＋自動退回前一個版本＋通知。** 判定收緊：切換後 30 分鐘內，至少 3 次崩潰類失敗，且分屬至少 3 個不同對話與 RID；同 RID 重試、平台關機、工具子程序被殺都不算。（草案）
4. **延後超過 6 小時：只通知，不強制升級；暫不提供「現在就更新」按鈕。**（草案）
5. **相容且 compute 已回收的 Agent：不主動升級，等它下一個回合結束後再升。**（草案）
6. **支援集合只放目前的 profile，不寫跨 profile 轉接。**（決定，第 3 點）
7. **派送意圖紀錄：要做，但先定義它的結束與過期。** 定義出來之前它只留在草案（已知未解問題 1、3）。（草案）
8. **漂移後釘在舊版本、無法接續的工作：** 標成「版本已變、無法接續」，通知主人以新訊息重送，不擋升級；原 Hosted 回條保持原樣。（決定：不改寫回條、不換 RID、通知重送，含來源碰不到時不擋棄用來源換新環境，第 7、9 點；草案：同一 SID 漂移時怎麼判定（已知未解問題 9）、派送意圖紀錄收尾、閒置升級時「不擋升級」的一般機制）

## 官方依據

- [Manage hosted agent sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)：建立時綁版、`version_indicator`、idle 與 stop 的保存、Files API（2026-10-12 讀，頁面更新 2026-09-23）
- [Manage hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#release-a-version-without-changing-production)：selector 不搬移既有 session、保留舊版本與 image（2026-10-12 讀，頁面更新 2026-09-30）
- [Hosted agents concepts](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions)：第一次使用時建立、Active／Idle／Resumed、30 天刪除、`$HOME` 保存（2026-10-12 讀，頁面更新 2026-09-29）
- 固定 SDK `azure-ai-projects==2.3.0`（[`requirements.txt`](../../src/fa-shared-context-poc/requirements.txt)）的 `create_session`／`get_session`／`AgentSessionStatus`，見 [ADR-0003 A 節第 2 點](0003-hosted-environment-continuity.md#platform-version-drift-replacement)

## Verified against

- 程式基準：`origin/main` `201fd3a8`（本分支開出點）；#813 2026-10-12 撰寫時仍 OPEN（後續：#813 已合併 `8732d7b7`，#815 已合併 `01e0fdb2`，見第 10 點）；第 1、2 輪只讀 PR 說明，第 3 輪核了它的 `_prepared_before` 與 `_require_selected_version`（見下）。
- 已讀程式：`shared_context/behavior_native_sessions.py`（`native_session_id`、`_session_metadata`、`_latest_version_reader`、`_metadata_sessions`、`reader`、`_platform_reader`、`_destination`、`follow`、`advance`、`admission`）；`behavior_native_turn_api.py`（`submit`、`_follow_before_admission`、`_not_admitted`）；`behavior_agent_versions.py`（`HostedVersions.current`、`endpoint_version`、`main` 的 `follow_all`）；`behavior_foundry.py`（`create_session`、`verify_session`、invoke headers）；`native_turns.py`（`replaced`、`_submit_locked`、`native_response`）；`behavior_native_turn_wire.py`（`require_expected_version`）；`behavior_wire.py`（`WireModel`）；`behavior_session_files.py`（`_source`、`require_source_version`、`copy_step`）；`native_environment_sync.py`；`behavior_work_policy.py` 1395–1415。
- Review round 1（2026-10-12）另核：`native_responses_wire.py` 的 `NATIVE_STATE_PROFILE`、`behavior_agent_versions.py` 寫死的 profile 比較、`native_turns.py`（`native_response` 的 403／409 順序、`session_state`、`replaced`、接單檢查 670–674、`execution_version` 釘選 463–464 與 1595–1596、`rid:<RID>` 所在的 `fa/v1/turns`）、`behavior_native_turn_wire.py`（`NativeSessionState`、`require_expected_version`）、`behavior_native_turn_api.py`（`submit` docstring、`_same_session`）、`behavior_foundry.py`（`_check_session_version`、`verify_session`）、`native_files_wire.py` 的大小上限、`deploy/azd/profile.defaults.json` 與 `deploy/stage/stage.json` 的閒置逾時與 `checkpoint_profile`；兩個 wire 檔自 2026-09-15 起 81 個 commit。Learn 重讀 manage-hosted-sessions（idle 120～3600 秒、Files 單檔上傳 50 MB、stop 對進行中回合的影響）與 hosted-agents（「new or existing infrastructure」、30 天刪除）。
- Review round 2（2026-10-12）另核（`origin/main` `1d2d5f91`，與 `201fd3a8` 之間只改工作台）：`native_checkpoint.py` 1–8（checkpoint 只在 `fa/v1/conv/<CID>`）；`behavior_native_turn_api.py` `_kept_conversations`（138–141，記憶體 LRU）、`_keep_admitted`、`_prepared_before`（659–670）；`NativeTurnCapture`／`NativeConversation`（無 codec 欄位）；`behavior_native_sessions.py` `advance`（632–643 的接受集合）、`_source_session_status`（91）、retarget（645–660）；`native_files_wire.py` `FILES_ERRORS`（16–21）；`behavior_session_files.py` `copy_step`（148–160）；`native_environment_files.py` `source_unavailable`（56–67）；`native_turns.py` `_original_session`（673–676）、`submit` 的非終態路徑（1093–1095）、`pending` 拒收（670–672）、`_recovery_error`；`native_environment*.py` 沒有撤回 `pending` 的指令。Learn 重讀 manage-hosted-sessions 的「How stopping a session affects an in-flight turn」（deferred 回合保持進行中、stop 不保證自行重啟）與 Azure Table「Performing entity group transactions」（可用操作、同一實體只能出現一次）。
- Review round 3（2026-10-12）另核（`origin/main` `1d2d5f91`；#813 head `5464a2f2`）：`native_environment_sync.py` 17–20（部署時與送出時 follow 都 `idle_only=True`）；`native_environment.py` `_scan_page` 363–377（任何 `accepted`／`started` 回條都回 `accepted_or_started`）與 744（commit 才換 `hosted_session_id`）；`behavior_native_sessions.py` `advance` 674–680（等待對照）；`native_turns.py` `replaced` 558–567、`read` 839–844（讀取不 409）、`_Meta.state_profile` 173–174；`behavior_native_turn_wire.py` 197、725、747（預設 profile 不寫）與 `NativeReceipt` 798–799（沒有執行版本欄位、profile 一律寫）；`native_responses_wire.py` 12（`NativeStateProfile` 是 `Literal`）；`behavior_foundry.py` 361（一律送 API 常數 profile）；#813 的 `_prepared_before`（668–688）與 `_require_selected_version`（`behavior_native_sessions.py` 168–175）；工作台 README 的換環境原句（ready 後顯示，不分對話）。
- Review round 4（2026-10-12）另核（`origin/main` `1d2d5f91`）：`native_environment.py` 的 `_scan_page`（325–412，含 `require_complete` 與 `check_latest_checkpoint` 371–392、`responses_environment_blocker` 356–358）、`_tasks_pending`（435–453）、retarget 與 `follow_session` 的 `admitting` 檢查（554–555、658–661、708–709）、`idle_only` 比對（595–599）、`files_unsupported`（631–632）、Hosted 來源檢查（698–705）、commit（743–745）；`native_turns.py` `_AgentSession`（288–302）、`_original_session` 的 `NATIVE_ADMISSION_PENDING`（677–679）、`_settle_admission`（703–733）、`execution_version` 釘選（463–464、1595–1596）；`native_task_observer.py` `quiet`（75–107）；`native_sdk.py` `NativeBootstrapSource`（104–135，非 200 一律 503）；`behavior_native_sessions.py` `_source_session_status`（91–95）、API 端 `follow` 的 stop 與來源讀回（584–600）、`advance`（632–680）；`NativeReceipt` 沒有時間或執行版本欄位。
- Review round 5（2026-10-12）另核（`origin/main` `1d2d5f91`）：第 8 點的 2026-10-04 原句（「舊環境與檔案都保留」對已失效或被刪的來源不成立）；`native_sdk.py` `NativeBootstrapSource`（104–135：121–123 非 200 一律 503，131 接受的狀態沒有 `deleting`）；`behavior_native_sessions.py` `_source_session_status`（91–95，94 的狀態含 `deleting`）與 API 端 `follow` 的 stop 與讀回（580–600）；`native_environment.py` `follow_session`（587–592 已 `ready` 原樣回傳、603 新舊同 SID 拒絕、609 要求較新的版本）；`native_turns.py` `execution_version` 釘選（463–464、1595–1596）；`behavior_native_turn_api.py` `NativeTurnGateway._invoke`（273–274）與 `behavior_foundry.py` `invoke_native`（476）：經它的呼叫約 51 處、10 個檔（`behavior_native_turn_api.py`、`behavior_native_sessions.py`、`behavior_native_responses_api.py`、`behavior_native_stream_api.py`、`behavior_session_files.py`（108 的別名）、`native_agent_api.py`、`native_group_api.py`、`native_routines.py`、`native_hitl_attention.py`、`native_skill_draft_api.py`；`native_group_tools.py` 的同名 `_invoke` 是另一個函式，不算）。
- Review round 6（2026-10-12）另核（`origin/main` `1d2d5f91`）：`behavior_foundry.py` `create_session`（208–238：`VersionRefIndicator(agent_version=target.agent_version)` 並核對讀回，所以 SID 建立時綁的版本寫得出來）；`native_environment.py` 的 commit 結果 `NativeEnvironmentView`（`previous_hosted_session_id`、`hosted_session_id`、`hosted_agent_version`，508–515）；`behavior_session_files.py` `SessionFiles`（34–75，Files API 的檢查點）；`behavior_native_sessions.py` 589–590（follow 對非 `idle` 來源 `stop_session`）；`behavior_native_turn_api.py` 的 `_follow_before_admission`（672 起；現行是 Hosted 拒絕之後、API 回應拒絕之前跑）。
- 2026-10-13 dev 唯讀證據（connector 工作線取得，只讀、沒有寫入或叫醒任何資源）：API `list_sessions`＋`get_session`；24 個 session、相隔約 50 秒兩次快照，沒有任何欄位改變（第 10 點 ①）；`list_sessions` 的 `created_at`＝`last_accessed_at`，`get_session` 的 `created_at` 叫醒不變；同一物件漂移例子 `db0386d0`（v103→v97）、`8d9a3d14`（v145→v141）、`95b9ee1b`（v104→v105）。2026-10-13 修訂輪依此改第 4、8、9 點的通知觸發，未另讀 log 或程式。
- PR #816 審查第 2 輪（2026-10-13）：設計修訂，未另讀程式或 log。依總協調決定，換版通知改由綁定列的通知待送位交付（第 2、4、8 點、生命週期表、未解問題 8），舊 image 的漂移 CAS 等 VM 接受 expected＝V_obs 才寫。
- 實際驗證：文件變更；未跑程式測試，沒有雲端呼叫。dev 漂移數據來自總協調的唯讀整理，本工作階段未另讀 log。
- 限制：Grok Bot 行為只來自研究摘要；`get_session` 不喚醒是觀察，非官方保證。
