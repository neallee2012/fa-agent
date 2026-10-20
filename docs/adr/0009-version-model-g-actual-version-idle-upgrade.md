# 版本模型 G：照 session 實際的版本執行，助理閒置時才升級

狀態：**「決定」部分已確認**（Neal 2026-10-12 19:53（+08）「都同意」）；**「閒置升級機制」設計定稿（Neal 2026-10-13 要求：「開始處理，解決未解的問題，設計定稿」）；M 項已核准**（對已確認部分與 Neal 決定的修訂 M1（含 M2 不拒絕）、M3～M10 採 A；機制細節沒有逐項另經 Neal 核准，phase 4～6 的實作 PR 照常審查）。原已知未解問題 1～9 與生命週期表的「未定」格子都已有解法；程式行號核到 `origin/main` `dc9bad4b`（審查第 3～5 輪）。仍待的：第 8 點新文案（「舊環境已失效」「換成了另一個版本」）待 Neal 確認（M4 退回句 Neal 2026-10-19 已定，見第 8 點）、M1 閘門 7（全掃描時間）在 dev 部署 2d-i 時量（閘門 4 已 PASS，2026-10-15；2d-i 實作前的決定見 M1 各處標 2026-10-15 的註記）、幾處標「推論」的要由實作 PR 測試（例如第 7a 點 (r) 來源晚寫完的競爭）、(r)(s) 的進度停止門檻 N 與卡住 `admitting` 的上限由實作 PR 訂。兩部分都尚未實作｜日期：2026-10-12（定稿輪與 #823 審查第 1～5 輪修正 2026-10-13）。

本 ADR 合併後只是設計契約；程式、測試、部署流程另走實作 PR，不因本文合併而生效，也不授權任何部署、雲端寫入或刪除。

## 本 ADR 分兩部分

| 部分 | 內容 | 地位 |
|---|---|---|
| **決定（已確認）** | 第 1～4、7～11 點與 phase 0～3b：版本是執行期事實、相容規則、漂移偵測與處理、v1→v2 遷移、不叫醒已回收 session、通知句子、保留舊版本、AGENTS 界線 2 措辭 | Neal 2026-10-12 19:53 確認；單獨成立，**不依賴**閒置升級機制 |
| **閒置升級機制（原「草案」）** | 第 5、6、12 點與 phase 4～6：U0～U9、Hosted `pending` 的切換用法、reconciler、否決與自動退回、Blob 快照、觸發；2026-10-13 定稿時拿掉了派送意圖紀錄與租約（Hosted 當唯一柵欄） | **設計定稿（Neal 2026-10-13 要求）；M 項已核准**；尚未實作，上線前換環境照第 7 點。Neal 已定方向（「Neal 已決定」第 2～5、7 項，以及第 8 項的一般機制）；其中幾項的做法與幾處已確認的決定另有修訂，Neal 2026-10-13 核准 M1（含 M2 不拒絕）、M3～M10 採 A |

**Neal 決定 8 在兩部分的切法（全文一致）：** 決定＝無法接續的 RID 不改寫回條、不換 RID、通知主人以新訊息重送，以及來源碰不到時這些 RID 不擋棄用來源換新環境、讀到時以「之前的 SID 清單」判斷是否回通知的規則（第 7a 點、第 9 點）；閒置升級機制＝同一 SID 漂移時無法接續的 RID 怎麼判定（原已知未解問題 9，定稿以 Hosted `refused:<RID>` 解決；SID 換過的情況用之前的 SID 清單判斷，不需要標記），以及閒置升級時「不擋升級」的一般機制。M9（Neal 2026-10-13 核准）修訂決定 8：從沒開始執行的回條由新 SID 以 CAS 接手執行、不請主人重送。

決定部分文中的「草案」一律指「閒置升級機制」一節（2026-10-13 設計定稿，Neal 同日要求；M 項已核准）；「已知未解問題 N」指該節末「原已知未解問題的解法」第 N 項。點的編號沿用上一版，沒有重排：第 5、6 點移到草案，第 7 點拆成決定（部署規則）與草案的第 12 點（觸發）。所有協調物件的建立、結束、過期與收拾方式見「協調物件生命週期」。

## 一句話

**版本是「正在跑的那台 VM 自己是哪一版」這個事實，不是 FA 堅持它該是哪一版。** 相容就照跑並記下來；不相容就不呼叫它、換到相容的 session（2d-i 起；之前只觀察與記 log），不拒絕主人的訊息（Neal 2026-10-13，第 9 點「不拒絕」）。換到新版本是一件有紀錄、有通知的事，做完從對話歷史與 checkpoint 接續；「只在助理閒置時換」靠草案的閒置升級機制，它上線前換環境照第 7 點（入口是現行 follow；來源碰不到時是棄用來源換新環境）。

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
- **stage 證據（唯讀，2026-10-18，時間為 UTC）：明確指定版本建立的 session，閒置恢復後仍被綁到舊版。**
  - 第 31 版部署時，A 與 B 各得新 SID：A `fa-native-8efce15e…` 建立於 12:35:35Z，B `fa-native-ad2abcf2…` 建立於 12:35:36Z；兩者的 follow 都回報 ready 82。A 是 FA 以 `VersionRefIndicator(agent_version=82)` 明確建立（`behavior_foundry.py` 224–233），VM 在 commit 時確認是 v82（`native_environment.py` 729）。
  - 兩者各閒置約 69～71 分鐘，超過 3600 s 的閒置逾時；AppTraces 與 API log 在這段時間都沒有活動。工作台相隔 9 秒叫醒兩者：A 的 last_accessed 是 13:47:20Z，B 是 13:47:29Z。
  - A 醒來是 v81：先回 409 `NATIVE_SESSION_CHANGED`，再記 `NATIVE_VERSION_OBSERVED` created 82 → observed 81 `drift=True`；`version_indicator` 在 13:5x 與 14:09Z 兩次讀回都是 v81。B 醒來是 v82，沒有 `NATIVE_VERSION_OBSERVED`。
  - 這和官方文件不符：[manage-hosted-sessions：Create a session explicitly](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#create-a-session-explicitly-advanced) 寫明帶 `version_indicator` 建立的 session 固定在該版本。同一次部署的 A、B 結果不同，所以平台行為並不一致。dev 的 157→156 看起來是同一類（建立 157、之後觀察到 156），但那次沒有逐項核對，不當成已證實同因。
  - 限制：沒有直接證據顯示 VM 被回收，只知道閒置超過 3600 s；last_accessed_at 看起來記的是恢復時間、不是最後一次呼叫（推論）。
  - FA 的補救：2d-i 在回合結束後的背景換版（background cut），它需要 GUARD 開啟；stage 第 31 版 GUARD 關（`"0"`），所以 A 停在 v81，要等第 32 版在 stage 開 GUARD。在那之前，`follow` 與 verify-only 會讀 SID 的平台 metadata，結果多一個 `observed_version`；與預期版本不同時回報 `drifted`，不再回 `ok`（只回報，不改行為：部署的 follow 與 verify-only 都把 `drifted` 當已處理，不讓部署失敗、也不擋部署）。

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
- **本節的欄位：**
  - 目前 SID。
  - 這個 SID 建立時綁的版本：世代 0 就是算出 SID 的那個版本；換環境建立的新 SID 是 commit 結果的 `hosted_agent_version`。這個值寫得出來：`create_session` 以 `VersionRefIndicator(agent_version=target.agent_version)` 釘版本並核對讀回（`behavior_foundry.py` 208–238）。
  - **同一 SID 漂移旗標：** 第一次觀察到的實際版本不同於這個 SID 建立時綁的版本就設定；之後版本又變回建立時的版本也不清除，只在目前 SID 換掉時清除；第 7a 點適用條件 (iii) 靠它判斷。不能只比「建立時的版本≠紀錄的實際版本」：版本號不單調，建立時 145、醒來 141、再醒來 145 時這個比較會說沒漂移，但釘在 141 的回條仍永遠卡住。
  - **漂移通知待送位：** 與漂移旗標在同一次 CAS 設定，同一次寫入也記下這時觀察到的版本，作為通知的 vY（紀錄的實際版本之後還會變，例如漂回建立時的版本，不能當 vY）。第一個主人本人的回應帶上第 8 點換版通知、寫出後以 CAS 清除，非主人呼叫者不清；目前 SID 換掉時隨漂移旗標一起清除（第 4 點末段）。
  - 紀錄的實際版本：含觀察時間、來源。另記最近一次讀到的 `created_at`：只取自 `get_session`、不取 `list_sessions`，只在既有流程本來就讀 `get_session` 時順便記下（認領時讀了 metadata、第 4 點第 6 步舊 image 409「SID 沒變」分支），不為它另加讀取，所以可能是空的。
  - 對應的協定 profile。
  - 之前的 SID 清單：各附版本；每次換環境 commit 後加入來源 SID，第 7a 點 (e)(f) 靠它判斷。
  - 最近一次換環境的理由與 `files_migrated`。
  - 換環境通知（`switch_notice`，phase 2c 起，M4 通知槽的一部分）：換環境 commit 寫綁定列時同一次寫入設定（已帶工作檔／沒帶工作檔、舊環境還在／舊環境已失效），與漂移通知待送位同樣由第一個主人本人的回應帶上、寫出後清除；換環境通知優先於漂移通知，沒送出的較嚴重通知不被後來的換環境蓋掉。
  - 棄用來源的決定（`abandon`，第 7a 點 (g)，phase 2b 起）：API 以 CAS 寫入後不翻轉，切換寫進綁定列時清掉。
  - 升級狀態、租約、預留 SID、否決清單、派送意圖紀錄都**不在**本節，只在草案（第 5 點）。
- 世代 0＝現有由版本算出的 SID，照舊可讀；沒有綁定列的既有 Agent 先「認領」它目前的 SID（phase 1）。
- 只有 API 寫這一列，時點只有下面四種，加上 phase 2b 起寫棄用來源的決定（第 7a 點 (g)；建立、結束與崩潰後收拾見「協調物件生命週期」）：
  - **認領：** 同時寫 SID 建立時綁的版本；紀錄的實際版本也寫成這個版本。認領時若已讀到不同的版本（例如 metadata），就把讀到的版本寫成紀錄的實際版本，並在同一次寫入設定漂移旗標與通知待送位（連同通知的 vY）；認領在請求之外讀 metadata、沒有 VM 可確認，沿用這個規則，通知位留著等下一個主人回應。否則世代 0 的 SID 建立時 145、認領時已回報 141，之後每次觀察都等於紀錄的 141、不算「觀察到不同版本」，旗標永遠不會設定。
  - **送出時觀察到不同版本：** 第 4 點第 6 步的漂移 CAS；觀察到的版本不同於 SID 建立時綁的版本時同時設定漂移旗標與通知待送位；舊 image 要等 VM 接受 expected＝V_obs 之後才寫。或同一分支讀 `get_session` 時順便讀到的 `created_at` 不同於紀錄（保底訊號，沒有保證的讀取點，見第 4 點末段）。
  - **換環境完成：** 草案上線前＝第 7 點換環境回傳的 commit 結果，phase 1 起就寫：目前 SID＝新 SID、**新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`**、清除漂移旗標與通知待送位、來源 SID 加進之前的 SID 清單；之後＝草案 U8。
  - **通知清除：** 主人本人的回應帶上通知且已寫出後，單列 CAS 把通知待送位清掉（第 4 點末段）。
- 三個假設「SID↔版本」的地方改查綁定列：`_scoped`、`_latest_version_reader`、`_metadata_sessions`。
- **兩份「目前綁定」紀錄**：API 綁定列是定位的權威（零 session 呼叫）；Hosted state store 的 `agent` 紀錄仍是 VM 內的柵欄，照現行由 retarget／follow commit 指令寫。兩者不一致（例如 follow 已 commit、API 還沒更新綁定列就崩潰）由第 4 點第 6 步的「不一致」分支處理，不當成漂移；來源不相容時送出不走到第 6 步（第 1 步照第 9 點「不拒絕」），改由重跑切換補寫（第 7a 點「commit 後、綁定列 CAS 前崩潰」）。
- `replaced()` 改成比 SID，不再比版本號大小：平台漂移已經讓版本號不單調（A 的 SID 建立時 145、醒來 141），不必等草案。被取代＝「Hosted `agent` 紀錄的目前 SID 不是我，**而且**我也不是 `environment.state="pending"` 的目標 SID」。只比目前 SID 不行：follow 進行中，`hosted_session_id` 要到 commit 才換成新 SID（`native_environment.py` 744），新 SID 在這段期間收到的 `_RECORD_READS` 會被誤拒成 409（`native_turns.py` 2027–2029；現行在 `pending` 時改比 `previous_agent_version`，558–567）。
- **分兩步上線（審查第 2 輪對齊 #824）：** phase 2a（#824，已合併，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15））只做放寬的一半：`pending` 的目標 SID 不算被取代。收緊的一半（既不是目前 SID、也不是 `pending` 目標的 SID，不論版本都 409）延到 phase 3：phase 3 之前 API 仍依 selector 選讀取者，收緊會讓 retarget 候選 SID 的讀取變 409，主人看得到。
- 收緊之後，**還沒切換的目的 session 不能用 `_RECORD_READS` 讀東西**（它既不是目前 SID、也還不是 `pending` 目標）。所以閒置升級在切換前對目的 session 的讀取（U3 的 checkpoint 讀回、U9 的預先還原）一律用 `_RECORD_READS` 以外的新指令，不擴充 `prewarm`（`prewarm` 在 `_RECORD_READS` 內，`native_turns.py` 66–68、2027）。

### 3. API 與 Hosted 的相容規則

**協定 ID 沿用 state profile（`FA_NATIVE_TURNS`），不新增握手；但要從 `foundry-native-v2` 起，profile 相同才代表相容。**

- **`foundry-native-v1` 不能當相容依據。** 這個字串從定義以來沒換過（`native_responses_wire.py` 10；`behavior_agent_versions.py` 133、338、350 與 `behavior_native_sessions.py` 119 還有寫死的字面比較），但 `extra="forbid"`、`strict=True` 的 wire DTO 一直在變：2026-09-15 起 `behavior_native_turn_wire.py`、`behavior_wire.py` 在 `origin/main` 有 81 個 commit。已部署的 v97、v103、v141、v145 等都報 `foundry-native-v1`，schema 卻各不相同；只比字串的話，API 會把它們當相容，結果是 422 或解不開的 202 回條（界線 7）。以前安全，是因為 `fa_build` 必須完全相等。
- **規則：一個 profile 要先在 repo 裡有 schema 快照測試，才可以放進支援集合。** 快照測試出現之前就存在的 profile（目前只有 `foundry-native-v1`）**永遠不相容**。phase 2 把 profile 換成 `foundry-native-v2` 並加快照測試；第一個 G API 的支援集合只有 `{"foundry-native-v2"}`，所以**所有 G 之前的版本都不相容**：FA 不 invoke 它們、也不拒絕主人的訊息（第 9 點「不拒絕」）；M1 以先擴充、再收斂的部署順序，讓收斂時已沒有 Agent 綁在這些版本上，萬一還有就在請求內換到新 session。
- **profile 字串涵蓋四件事，任一件變了就換字串：** (a) `NATIVE_REQUEST` 與回應 DTO 的 JSON schema；(b) Hosted state store 項目的 schema；(c) CTX-03 的固定 SDK／runtime 版本；(d) 這個 image 讀得懂的 checkpoint codec 清單（對應對話設定的 `checkpoint_profile`，例如 `auto-no-custom-v1`）。快照測試把四件事一起存成快照，任一件變了但 profile 沒換就失敗。**v2 快照規則（總協調 2026-10-17，在 Neal 核准的 G 計畫下）：** ① 快照只涵蓋 Hosted 讀寫的 model；只有 API 用的模組（`native_binding`、`native_cut`、`native_deploy_gate`、`native_session_patrol`、`native_relay_quota`、`native_groups`、`native_hitl`、`native_hitl_material`、`native_routines`、`native_teams_relay`、`native_version_veto`；`test_native_profile_snapshot.py` 的 `API_ONLY_MODULES`）不在內，改它們不必換 profile，H0 比對也略過它們。② stage 第 31 版起，v2 只能新增「預設時不輸出的選填欄位」。基準檔是 `tests/shared_context/fixtures/native_v2_wire_<commit>.json`，檔名與 `produced_from.commit` 都是產生它的程式所在的 commit；目前是 `8a8608dd`（第 31 版的 release PR 在它部署的 main commit 上重產；和前一份 `11ff64ef` 比只有 `produced_from.commit` 不同）。`v2_drift` 用 H0 的比對器，再加嚴三項，因為第 31 版的 image 嚴格解碼、不認識的值一律拒收（literal_error／union_tag_invalid）：enum 或 Literal 的值**加了或少了**都算漂移（H0 只擋少了）；request 多了新的 operation 算漂移；程式裡有 model 不在基準裡算漂移。其餘照 H0：既有欄位改型別、刪掉、改成必填或預設輸出、預設不輸出的欄位改預設值、多了預設會輸出的新欄位，都算漂移。唯一例外是能證明舊 image 永遠收不到的新值：逐條把 `v2_drift` 的那一行寫進 `V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE` 並附證明（目前是空的）。漂移就要換 profile；通過的改動只重產 profile 快照。③ 基準檔只照 `test_native_profile_snapshot.py` 檔頭寫的指令產生，`FA_REGENERATE_PROFILE_SNAPSHOT` 碰不到它，整檔 digest 也釘住。第 31 版之前，改到 v2 wire 的 PR 各自重產；**第 31 版的 release PR 要在它部署的 main commit 上照同一指令再重產一次**，檔名與 `produced_from.commit` 改成那個 commit，連同 `V2_BASELINE_SHA256` 放進同一個 release PR（Shared Context README「發布 stage 版本」）。這一步不能省：基準若比第 31 版舊，第 31 版已有、基準沒記到的欄位之後被改型別也比對不到。
- 每個版本的 definition 已經帶 `FA_NATIVE_TURNS`，API 用 `get_version` 讀（`_require_native_version`、`_responses_enabled` 已在讀），不呼叫 session。上面那些寫死 `"foundry-native-v1"` 的比較都改成「在支援集合內」。
- **送出前的相容判斷：** 版本 Y 相容 ⇔ Y 的 profile 在 API 的支援集合內，而且 Y 的 definition 身份（agent GUID、instance identity）相符、狀態 `active`。**每次送出前**都檢查，只讀 definition，不呼叫 session。
- **checkpoint codec 在 Hosted 載入時檢查，不在送出前檢查。** native checkpoint 只存在 Hosted state store 的 `fa/v1/conv/<CID>`（`native_checkpoint.py` 1–8）；API 沒有持久的 codec 標記：回合 API 只把對話與 capture 放在記憶體 LRU（`_kept_conversations`，`behavior_native_turn_api.py` 138–141），capture 裡的 `template.settings.checkpoint_profile` 是對話**設定**的 codec，不是最後合法 checkpoint 實際用的 codec。所以 codec 由 Hosted 在載入 checkpoint 時依 [CTX-03](../ctx03-checkpoint-contract.md) 第 4 節檢查，不認識就明確拒絕、保留最後合法狀態（同文件第 9 節），不能當成空對話。日後若要改成送出前檢查，前提是 API 在接單時把 codec 標記持久化到每個對話、並為既有對話回填；做到之前不宣稱送出前檢查得到。
- `HostedVersions.current` 的 `fa_build` 完全相等檢查**拿掉**，換成上面的判斷；`fa_build` 只留作資訊。
- **P1 待辦：舊 image 也鎖提示前綴與 profile（2026-10-17，#880 審查重現）：** 和 SDK 開始閘門（M1「A2d 服務哪些舊 image」下的 2026-10-17 註記）同一類死結。舊 image 的 `preflight`（`native_sdk.py` `preflight`）在 `rid:<RID>` 之前比對 capture：同一 profile 改了提示（`prefix_sha256` 或 `capability_catalog_version` 不同）回 409 `NATIVE_CAPTURE_CHANGED`；換成舊 image 不認識的 profile ID 回 409 `NATIVE_PROFILE_UNSUPPORTED`；兩者都是 `not_dispatched`，沒有回條，回合結束的背景切換也就不會開始。`preflight` 對每個還沒有 `rid:<RID>` 的送出都跑（`native_turns.py` `_admission_checks`；已有回條的同 RID 重送不再跑），所以擋的是送到舊 image 的所有新訊息，不只沒有選版、新開的對話（原註記只寫 `NATIVE_CAPTURE_CHANGED` 與新對話，更正）。現在沒有發生：審查者核對 stage 第 30 版與 main 的提示前綴相同（本 PR 沒有另行核對）。**任何會改提示或 profile 的發版之前必須修好**，可能的做法是 4c 的請求內切換（被這兩個碼拒絕時，在請求內切到新版本再以同 RID、同 body 送）。
  - **已解決（2026-10-18，4c-2 PR）。規則：** `FA_NATIVE_SESSION_GUARD_ENFORCE` 下，主人的送出（工作台、群組、Teams 都經 `NativeTurnGateway.submit`）的新 RID 被綁定列的 SID 以這兩個碼拒絕、帶 `not_dispatched`，而且這個請求讀到的 selector 版本不是那個 SID 的版本時，就當成舊 image：那裡沒有接單，所以每個請求的第一次嘗試把 Agent 交給既有的背景切換（`native_cut.send_refused` → `CutDriver` → `NativeSessions.cut`，每個 Agent 同時最多一次；來源有工作檔就照搬檔模式帶過去），這次嘗試回 503 `AGENT_VERSION_FOLLOWING`（`not_dispatched`），請求內等候（W＝10 秒）以同 RID、同 body 重送，切換一完成就送到新 SID；超過 W 回 `server_following`，工作台以同 RID 自動重送。不拒絕、主人不必手動重送，同一 RID 只在新 SID 接單一次。SID 的版本就是 selector 版本時，這兩個碼照舊是拒絕（真的 capture 不符）。沒有遇到拒絕的送出不多任何呼叫（規則只在拒絕的處理裡，讀這個請求已讀過的 selector）。`GUARD` 關（stage）不必改：selector 一動，`_located` 在呼叫前就判綁定列的 SID 不可用，送出時的 follow 先把 Agent（連同工作檔）搬到新版本，舊 image 收不到新 RID（離線測試確認）。**交辦與 routine（2026-10-19，同 PR 審查修正）：** Agent 間交辦、回覆結果、續接與群組成員送達（`native_agent_api.py` `_send`、`_returned`、`_continued`）和 routine（`native_routines.py` `trigger`）直接呼叫 `submit_native_turn`，沒有主人送出的請求內等候。原本這兩個碼原樣回：Hosted 的工具把它對應成 `NATIVE_RELAY_REFUSED`、叫模型不要重送，訊息就丟了；routine 每分鐘對同一個舊 SID 重試，又沒有回合結束可觸發切換。現在同一條規則（`behavior_native_turn_api.py` 的模組函式 `older_image`）也在這些拒絕處理裡跑：成立時交給背景切換，回 503 `AGENT_VERSION_FOLLOWING`（`not_dispatched`）；交辦經 `_waiting` 與 Hosted 的 `NativeRecipientFollowing` 以同一個 relay RID 重送，routine 走既有的 503 `NATIVE_ROUTINE_UNCONFIRMED`，下一個 tick 以同一個 occurrence RID 送到新 SID。**`580c980e`（2026-10-06）之前的 image 不帶 `not_dispatched`：** 核對相容底線 `9aaa0824` 到 `580c980e` 之間每個改過 `native_turns.py` 的 commit，新 RID 的 409 `NATIVE_CAPTURE_CHANGED` 只來自 `_submit_locked` 的 `preflight`（`native_sdk.py`）與對話 capture 比對，`NATIVE_PROFILE_UNSUPPORTED` 只來自 `preflight`（含 `native_capabilities.selected_providers`、`native_sdk_profiles.check_start_capture`），都在 `create_item(f"rid:...")` 之前；`_allocate` 裡另一個 `NATIVE_CAPTURE_CHANGED` 只在背景回合（`_handle_turn`）、回條已寫成 `started` 之後，結果寫進回條，不會是送出的 409；同 RID 已有回條時這些檢查都不跑。`580c980e` 起改由 `_admission_checks` 的拒絕處理標 `not_dispatched`，檢查位置不變。所以 API 的 Foundry client 對 `submit`、`agent_receive`、`routine_receive`、`continue` 收到的這兩個 409 一律當成 `not_dispatched`，不要求欄位（交辦、routine、續接原本完全收不到這個欄位）；其他碼、其他狀態照舊。API 自己在 202 之後比對回應不符時丟的 409 `NATIVE_CAPTURE_CHANGED` 不經 client，不受影響。
- Hosted 端照舊要求 `x-client-fa-state-profile` 等於自己的 profile（`native_turns.py` `native_response` 開頭）。API 照現行送**自己目前的** profile（`behavior_foundry.py` 361 一律送常數）；支援集合只有這一個 profile（Neal 決定 6），所以相容的 session 的 profile 就是它，profile 不同的 session 在第 4 點第 1 步就不送。這個檢查排在 expected-version 檢查**之前**、也在讀 request body 之前，不符時回 **403 `NATIVE_TURNS_DISABLED`**，不是 409；所以「醒來綁到不同 profile 的版本」看到的是 403（第 4 點第 5 步、第 9 點）。
- 功能層級的 profile（`files_profile`、`files_omission_profile`、Responses profile）維持「缺功能就那個功能明確拒絕」（例如 `NATIVE_FILES_UNSUPPORTED`），不讓整個回合不相容。
- **支援集合只放目前的 profile，不寫跨 profile 轉接**（Neal 決定 6）。換 profile 時，舊 profile 的 session 一律不相容，等換環境。M1（Neal 2026-10-13 核准）只在 v1→v2 遷移期間有限度放寬（Hosted 暫時接受 v1 header 當自己 schema 的別名，收斂時拿掉）；2d-i 上線前照本條。
- 大部分發版（提示、工具、只改 Hosted 的行為）不動上面四件事，所以**相容、不必強迫升級**。
- **state store 項目一律寫明 profile。** 現行 `_Meta`（`native_turns.py` 173–174）與 `NativeSubmit`、`NativeAgentReceive`、`NativeRoutineSubmit`（`behavior_native_turn_wire.py` 197、725、747）在 profile 等於預設值時不寫這個欄位（`NativeReceipt` 等其他項目一律寫）。只換常數的話，v2 會把沒標的 v1 時代項目讀成 v2，v1 image 也會把沒標的 v2 項目讀成 v1。所以規則是**「沒標＝v1 wire」**（審查第 2 輪改寫）：
  - **帶 digest 的項目照送出時的 profile 寫，永遠不改標。** `state_profile` 是原送出內容的一部分：`NativeReceipt` 以自己的 `state_profile` 重建原送出、核 `submit_digest`（`behavior_native_turn_wire.py` 846–876）。所以 `NativeSubmit`、`NativeAgentReceive`、`NativeRoutineSubmit`、`NativeContinuationSubmit`、`NativeRelayAcceptance`、`AgentTemplate` 與回條，以 v1 送出的照 v1 寫（固定的 v1 不寫出，位元組與現在相同），以 v2 送出的明寫 v2。依 image 或事後改標會讓已存回條解不開、同 RID 重送變 `NATIVE_RID_CONFLICT`（#824 第 4 項同此）。
  - **沒有 digest 的項目（`_Meta`）由寫入的 image 標自己的 profile**；#824 起對話 `meta` 一律寫明。
  - **讀取：** 沒標的項目一律當 v1 wire 讀（v1 時代寫的，或 M1 擴充期間以 v1 header 接的單）；遷移掃描與接續既有對話都要讀。
- **對話與回條的 profile 比對改成比「通道」，不比版本（審查第 1 輪補）。** 現行 Hosted 以字串相等比對：`meta.state_profile != request.state_profile`（`native_turns.py` 1029，接續對話時拒絕 `NATIVE_STATE_SCOPE`）、`meta.state_profile != receipt.state_profile`（855、1285，另見 911）。API 一改送 v2，每個 v1 時代的對話都會被拒，等於所有既有對話都斷掉。phase 2 改成比通道：一般回合（沒標、`foundry-native-v1`、`foundry-native-v2`）是同一個通道；Responses（`foundry-responses-v1` 與 phase 2 訂的 Responses v2 標記）是另一個；跨通道照舊拒絕。
  - **phase 2 必要測試：** v1 與沒標的對話在 v2 請求下照常接續；Responses 通道的 `foundry-responses-v1` 對話同樣接續；帶 digest 的新項目照這次請求的 profile 寫（M1 的 2d-i 期間以 v1 header 接的單不標、位元組與 v1 相同，同 RID 重送不衝突），只有 `_Meta` 標 image 自己的 profile。
- 從 v2 起，較舊 image 讀到較新 profile 寫的項目必須明確拒絕（同 CTX-03「不認識的格式不猜」），不能解碼失敗後當成空狀態。已凍結的 v1 image 改不了：`NativeStateProfile` 是嚴格的 `Literal`（`native_responses_wire.py` 12），讀到明寫的 `foundry-native-v2` 會驗證失敗，但失敗後怎麼處理無法再改。所以遷移後 FA **永不再 invoke v1 的 session**（第 4 點第 1 步不送、第 7 點遷移不碰、第 10 點不叫醒）。
- **已接的工作仍釘在接單時的版本。** Hosted 照現行拒絕 `execution_version` 不同的排隊輸入與已接受輸入（`native_turns.py` 463–464、1595–1596），G **不放寬**：同一個 profile 只保證 wire 與存檔格式相同，不保證另一版會把做到一半的工作接對。「相容就照跑」只適用**新接的單**；漂移時已有釘在舊版本的工作怎麼辦，見第 9 點。舊版 `fa-chat-…` 對話的 `verify_session`／`_check_session_version`（`behavior_foundry.py` 195–205、258–278）同樣照舊釘版本：版本不同就照現有規則明確 reset 或開新對話。

### 4. 送出一個回合：怎麼處理實際版本與漂移

`require_expected_version` 保留「完全相等」，但角色從「相容閘門」改成「**漂移偵測器**」：

1. API 直接讀綁定列（不用記憶體快取），得到目前 SID 與紀錄的實際版本 V_rec，用第 3 點判斷 V_rec 相容。不相容：照第 9 點「不拒絕」處理（2d-i 之前只記 log、照現行送出；2d-i 起不送到這個 SID、在請求內換環境）。草案上線前，保留的送出時 follow（`_follow_before_admission`）在這一步的判斷**之前**先跑；第 7a 點 (j) 的補寫依賴這個順序（否則來源不相容時每次送出都停在這裡，重跑切換永遠不會發生）。
2. （保留編號）原草案的派送意圖紀錄在這一步寫；本節**沒有**這一步。定稿輪建議不建立這個紀錄（M6，原已知未解問題 1、3）。
3. 呼叫時 `x-client-fa-expected-version`＝**V_rec**（不是 selector），`x-client-fa-state-profile`＝API 目前的 profile（V_rec 相容，所以就是 V_rec 的 profile；第 3 點）。
4. VM 版本＝V_rec：照常接單。
5. 回 403 `NATIVE_TURNS_DISABLED`：現行程式在讀 body 前就丟出，所以確定沒派送。先重讀綁定列；SID 已換就照第 6 步「SID 已換」處理；SID 沒變，就當「醒來綁到 profile 不同的版本」，照第 9 點「不拒絕」處理（2d-i 之前 API 只送 v1、所有 image 都收 v1，計畫內不會出現）。
6. 回 409 `NATIVE_SESSION_CHANGED`：這個碼現在有兩種原因，不能直接當漂移——(a) Hosted 的 `agent` 紀錄已指向別的 SID（`native_turns.py` `_original_session` 673–676、讀取前的 `replaced()` 2027–2029）；(b) VM 版本≠expected（`require_expected_version`）。新 image 在 409 本文加 `reason`（`replaced`／`version`）與 `actual_version`；舊 image 沒有，所以一律先重讀綁定列：
   - **SID 已換。** 確定沒派送（403，或 409 帶 `not_dispatched`）才同 RID、同 body 改送新 SID。舊 SID 可能已接單時（舊 image 的 409 沒有 `not_dispatched`）不能推定新 SID 可以接手：`rid:<RID>` 雖然存在同一個 Agent 的 `fa/v1/turns`、新舊 SID 讀同一份，但新 SID 接不了舊 SID 的工作：舊 SID 接下、仍是 `accepted` 的 RID 若改送到新 SID，會走 `_require_original_session` 被 409 拒絕（`native_turns.py` 1093–1095 → 673–676）。所以先經新 SID 以 `NativeRead` 讀這個 RID（讀取不會 409，回條照原樣回來，839–844）：讀到終態回條就照原回條；`NATIVE_RID_MISSING` 才確定沒接單、再送；讀到 `accepted`／`started` 且 `hosted_session_id`＝舊 SID，就明確失敗，原 RID 保持未決（界線 7），**不可改送**、不換 RID；舊 SID 在綁定列的之前的 SID 清單時，失敗回應帶第 8 點「這則訊息是較早的版本接下的……」（第 7a 點 (e)）。（M9，Neal 2026-10-13 核准：從沒開始的 `accepted` 改由新 SID 以 CAS 接手、不失敗，含已退役的來源（#825 審查第 3 輪）；實作前照上文。）
   - **SID 沒變。** 取實際版本 V_obs：新 image 用 409 本文的 `actual_version`；舊 image 只能用 `get_session` metadata（session 剛被叫醒，此時是 `active`）。metadata 的值只是假設，下一步 VM 接受 expected＝V_obs 才算確認（第 1 點）；漂移 CAS 與通知待送位也等這個確認才寫（下一條）。
   - **V_obs＝V_rec：不一致，不是漂移。** VM 就是紀錄的版本卻仍 409，原因只能是 (a)：Hosted `agent` 紀錄已指向別的 SID，綁定列還沒跟上（例如換環境的兩處寫入只完成一半）。不記漂移、不重送；這一回合明確失敗（舊 image 沒有 `not_dispatched` 時，RID 照界線 7 保持未決），留 `NATIVE_BINDING_DIVERGED` log。修復：草案上線前交給保留的送出時 follow（`_follow_before_admission`）收斂，再依它回傳的 commit 結果 CAS 更新綁定列；草案的 U8 另要求兩處寫入可重入（草案已知未解問題 6）。這條只在第 1 步通過（V_rec 相容）時走得到；綁定列仍指向不相容的來源時，修復靠重跑切換（第 7a 點）。
   - **V_obs≠V_rec：漂移。** 要寫的是綁定列 CAS：記下 V_obs 與來源（`vm`／`metadata-active`）；V_obs≠SID 建立時綁的版本時，同一次寫入設定漂移旗標與通知待送位（第 2 點），並留 `NATIVE_VERSION_OBSERVED` log。通知不再只附在這個請求的回應上，由通知待送位交付（本點末段）。
     - **什麼時候寫，看 V_obs 從哪來：** 新 image 用 409 本文的 `actual_version`，是 VM 自報的事實，立刻寫，再依下面處理。舊 image 的 V_obs 只是 metadata 的假設（第 1 點），**要等 VM 在同一個請求內接受 expected＝V_obs 之後才寫**：下面「讀這個 RID」或「再送一次」，任何一次以 expected＝V_obs 對同一 SID 的呼叫沒有被 409／403 拒絕，才算確認；沒確認（讀不到、再送仍是 409）就什麼都不寫，V_rec、旗標、通知位都維持原樣，下一個請求重新觀察。這樣 metadata 讀錯時不會設旗標，也不會發出錯誤的通知；代價是確認前崩潰或失敗的那次漂移不留紀錄、下次再觀察（多一次 409）。
     - **V_obs 不相容：** 舊 image 沒有 VM 可確認（不相容的 VM 不 invoke），不寫旗標；新 image 照上面寫入。兩者這一回合都照第 9 點「不拒絕」處理。
     - **V_obs 相容：** 409 **帶** `not_dispatched: true`（新 image）就同 RID、同 body、expected＝V_obs 再送一次；**沒有** `not_dispatched`（舊 image）就不推定沒送出，依界線 7 先以 expected＝V_obs 對同一 SID 讀這個 RID（`NativeRead`；session 此時 `active` 且相容，不會為此叫醒已回收的 session）：`NATIVE_RID_MISSING` 才確定沒接單，再送一次；讀到原回條就照原回條；讀不到就明確失敗（`dispatch_unknown`），不換 SID、不換 RID 重送。
   - 再送仍是 409：不再試，明確失敗，原 RID 保持未決（第 9 點）；舊 image 此時沒有確認，不寫漂移 CAS。
7. 逾時、5xx、回應遺失：照現行界線 7，不重送。

「每個 RID 最多再送一次」只是**重試上限**，防止漂移時繞圈；**不重複執行靠的是 Hosted 的 `rid:<RID>` create-or-read 去重**：同 RID、同 body 送幾次都只建立一次，body 不同就回 `NATIVE_RID_CONFLICT`。

Hosted 端配合（新 image 才有）：`native_response` 在 dispatch 前的所有拒絕（expected version、profile、`replaced()`）都加 `not_dispatched: true`、`reason` 與 `actual_version`。舊 image 沒有，所以上面「先讀綁定列、再核回條」是必要的。

**平台在同一個 session 換了版本：用綁定列的「通知待送」位交付一次通知（2026-10-13 更正；PR #816 審查第 2 輪改交付方式；總協調決定，通知句待 Neal 確認）。** dev 實際看過的平台自行變動就是這一種：同一個 session 物件（`get_session` 的 `created_at` 沒變）叫醒後回報的版本不同於建立時綁的版本（見「平台實際行為」）。FA 第一次觀察到它，就是同一 SID 漂移旗標從「未設定」變成「設定」的那次寫入（第 2 點：認領時讀到不同版本，或本點第 6 步的漂移 CAS；舊 image 要等 VM 接受 expected＝V_obs 才寫）；**同一次 CAS 一併把綁定列的通知待送位設成「待送」**。通知不只附在做這次寫入的那個請求的回應上，因為那樣會在三種情況永遠漏掉，而旗標之後不再清除、不會有第二次機會：(a) 認領在請求之外發生，沒有回應可附；(b) 先看到的是非主人呼叫者（routine、Agent 間交辦、群組、HITL 提醒，也就是第 7a 點 (k) 的入口中不是主人本人的）；(c) 回應遺失（逾時、5xx、CAS 之後崩潰）。交付規則：

- **帶上：** 每個**主人本人的工作台回應**在 API 直接讀到的綁定列（第 1 步）通知待送位是「待送」時，附第 8 點「平台把助理的執行環境換成了另一個版本（vX→vY）……」；vX＝這個 SID 建立時綁的版本，vY＝設定漂移旗標時觀察到的版本（與通知待送位同一次寫入、存在綁定列，第 2 點）。不用紀錄的實際版本：它之後還會變，漂回建立時的版本時通知會變成 vX→vX。非主人呼叫者不帶、也不清。**只在工作台狀態欄位顯示（M4 的 A 第 2 項）：** Teams 主人回覆是聊天文字，不帶這則通知也不清（換環境通知照帶，第 8 點）。
- **清除：** 通知已隨回應寫出（工作台 HTTP 回應已完整寫出）之後，以 CAS（If-Match 剛讀的 ETag）把待送位清掉；衝突就重讀，仍是「待送」就再清一次，否則放著。順序是先送、後清：寫出前崩潰，位元還在，下一個主人回應再顯示；清除前崩潰或清除失敗，同樣下次再顯示，最壞重複一次，所以是**至少一次**，清除遺失後的重複顯示接受。殘餘：回應已寫出卻在網路上遺失，API 無從得知，這則通知會漏掉（界線 7：寫出不等於真人已收到）。
- **不過期：** 待送位沒有時限；目前 SID 換掉（換環境 commit，第 7a 點 (f)）時隨漂移旗標一起清除，換環境另有自己的通知（第 8 點）。旗標已設定後再觀察到其他版本（含漂移後漂回）不再設待送位。
- **範圍：** 待送位每個 Agent 一個，被第一個主人本人的回應清掉，其他對話不再顯示；「每個對話各顯示一次」的標記與補發仍屬草案（已知未解問題 8）。
- **phase 1 的限制：** 工作台當時不顯示任何第 8 點通知，所以 phase 1 只設待送位、回應欄位帶通知，不實作清除（每個主人回應都帶）；清除與顯示一起在 phase 2c 實作（#830：工作台 HTTP 回應寫出後以 CAS 清除；Teams 不帶、不清這則漂移通知，只有換環境通知由 Teams 最終回覆帶上、寫出後清除，`running` 輪詢不帶也不清）。

句子保持中性、不說檔案不見：官方說叫醒時 `$HOME` 與 `/files` 會還原，同一物件換版時 session 的檔案區照理保留。不相容的漂移另照第 9 點「不拒絕」處理。這不是 FA 換環境，但 FA 照實說；AGENTS 界線 2「平台自行重建環境時也照實記錄與告知」靠這一句兌現。

**`get_session` 的 `created_at` 改變：只是順便讀到的保底訊號（2026-10-13 更正）。** 先前的推論（B 每次以不同版本被叫醒時 `created_at` 都重設）被 dev 證據推翻：`list_sessions` 的 `created_at` 等於最後存取時間，`get_session` 的 `created_at` 在叫醒時不變。所以 `created_at` **不是**版本漂移的偵測器。它只在既有流程本來就讀 `get_session` 時順便比對（認領時讀了 metadata、本點第 6 步舊 image 409「SID 沒變」分支；新 image 的 409 帶 `actual_version`，送出時不讀 `get_session`），FA 不為它另加讀取，**沒有保證的讀取點，可能完全漏掉**。讀到與綁定列記錄的值不同（記錄是空的不算），才當成「換了一個 session 物件」，記下來並把第 8 點「平台重建了……」通知附在這個請求的回應上（保底訊號本來就可能漏，不做待送位）。dev 目前沒看過這種改變，官方也沒有定義 `created_at` 何時會變；真的換了物件時，FA 不假設 `$HOME` 還在。

### 7. 部署、`foundry-native-v1` → v2 遷移與保留舊版本

- **`foundry-native-v1` → v2 的一次性遷移：入口沿用現行 follow，但來源碰不到時不是 follow。** phase 2 換成 v2 的那次部署，所有 Agent 都還是 v1、全部不相容；換環境的入口仍是部署時的 `follow_all` 與送出時的 `_follow_before_admission`。現行程式在這次遷移有三個缺口：
  - **來源 403 不算來源拒絕。** v2 的 API 帶 `foundry-native-v2` profile 呼叫 v1 的 VM，會在讀 body 前被 403 `NATIVE_TURNS_DISABLED` 拒絕；`advance`（`behavior_native_sessions.py` 632–643）只吸收 `FILES_ERRORS ∪ {NATIVE_SESSION_CHANGED, NATIVE_ENVIRONMENT_CONFLICT}`，`NATIVE_TURNS_DISABLED` 不在 `FILES_ERRORS`（`native_files_wire.py` 16–21），所以直接往上丟；`copy_step` 只接 409（`behavior_session_files.py` 148–160）。
  - **送出時不 follow。** `origin/main` 的 `_prepared_before`（`behavior_native_turn_api.py` 659–670）讀 v1 session 的 state 會拿到 403，把 Agent 當成「沒準備過」。#813（phase 0）之後更早停下：v2 還沒有 session 時，`_require_selected_version` 在讀任何 v1 state 之前就回 503 `AGENT_VERSION_FOLLOWING`，送出直接拒絕、不 follow（#813 `5464a2f2` 的 `behavior_native_turn_api.py` 609–614、687）。所以真正的缺口是「v2 session 還沒建立 → 不算準備過 → 送出時不 follow」，只能等部署時的 `follow_all`。
  - **現行 follow 處處假設來源碰得到。** 部署時與送出時的 follow 都用 `idle_only=True`（`native_environment_sync.py` 17–20）。審查第 3、4 輪找到的等待點，每一個在來源碰不到時都永遠結束不了：
    - `_scan_page`（`native_environment.py` 363–377）讀到同一個 Agent `fa/v1/turns` 的任何 `accepted`／`started` 回條就回 `accepted_or_started`；
    - Hosted `agent` 紀錄的 `admitting` 不是空的就在掃描前回 `admission_pending`（554–555、658–661、708–709），commit 時又原樣帶進新紀錄（743–745），之後每個 v2 送出都被 503 `NATIVE_ADMISSION_PENDING` 擋下（`native_turns.py` 677–679），`_settle_admission` 也清不掉它（回條的 `hosted_session_id` 不是新 SID，726–733 回 409）；
    - `_tasks_pending` 經 `quiet()`（`native_task_observer.py` 75–107）看到 chain task `in_progress`、有 lease 或還有 steering 輸入就回 `task_active`；
    - 來源 metadata 404 時 API 的 `_source_session_status` 丟 503（`behavior_native_sessions.py` 91–95），follow 回 `source_unavailable`、永遠不 commit（586–598），閒置超過 30 天被平台刪掉的 Agent 就永遠換不了。

    v1 來源上的這些東西，v2 碰不到（403 在讀 body 前），第 10 點也不准為此叫醒 `idle` 的來源；同時 Hosted `pending` 讓新工作一律 503（`native_turns.py` 670–672），第 4 點第 1 步也拒絕送出，Agent 卡死。逐一在 follow 裡加例外，每一輪都會再找到下一個，所以下面第 3 項（第 7a 點）改前提。M1（Neal 2026-10-13 核准）另用「先擴充、再收斂」的部署順序讓這次遷移根本不出現不相容的來源。

  所以 **phase 2 的必要範圍**（不是「用測試確認」）：
  1. 任何仍會 invoke 來源的路徑，來源回 403 `NATIVE_TURNS_DISABLED` 一律當來源拒絕：走既有 `source_unavailable` 階段寫 `source_invocation_refused`（`native_environment_files.py` 56–67），之後照 2026-10-04 不搬檔路徑；`advance` 與 `copy_step` 的接受集合加上它。第 7a 點的棄用來源換新環境本身不 invoke 來源，這一項是其他路徑的防線。
  2. phase 1 之後，送出時的「準備過」改成「這個 Agent 有綁定列」：只讀綁定列，不呼叫任何 session；v2 session 還沒建立也照樣換環境。
  3. **來源碰不到時不是 follow，而是「棄用來源換新環境」**（Neal 決定 8 套用到這次遷移），見第 7a 點。

  三項做不到，phase 2 就不單獨上線，改成與草案機制一起上線。切換完成前不拒絕主人的訊息（第 9 點「不拒絕」）：送出時 follow 照現行回 `server_following`，工作台以同 RID 自動重送。
- **停用送出時 follow 與部署時 `follow_all` 屬草案**（第 12 點、phase 4、6）。草案上線前，這兩條現行路徑一律保留，它們是觸發換環境（含第 7a 點的棄用來源換新環境）的唯一入口。（M1 的 A（Neal 2026-10-13 核准）把兩者、連同 routine 自己的換環境，停用提前到 2d-i，觸發改由回合結束後的背景工作負責。）
- **部署前的相容閘門：** CI 部署新 API 前，讀所有綁定列的 profile；新 API 的支援集合必須涵蓋所有被紀錄中的 profile，否則拒絕部署（先讓舊 profile 的 Agent 換到新 profile）。閘門在 v1→v2 遷移完成後接上（phase 3b），否則第一次 v2 部署一定被擋。
- **保留舊版本：** repo 目前沒有任何 `delete_version` 呼叫；本 ADR 把它定成規則：綁定列目前 SID 與之前的 SID 清單最後一個（前一個）引用的版本與其 ACR image 不刪、不讓保留政策清掉（phase 3b）。刪除任何版本、image 或 session 仍需另外批准。

### 7a. 棄用來源換新環境

實作：phase 2b，#825（已合併，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15）；另寫切換路徑、不寫 Hosted `pending`，決定存在綁定列，驗收表 (a)～(s)，(r)(s) 照本文的做法；從沒開始的回條依 M9 由新 session 接手（審查第 2 輪起，總協調決定；第 3 輪起含已退役的來源）；接手後還沒送出的 Responses slot 持有者後面，主人的下一則照常接單排隊；等待的 Responses 訊息輪到時由 Hosted 以 Agent 身分請 API 送出（第 3 輪）；細節見[共用 README](../../src/fa-shared-context-poc/README.md#abandon-source)）。

原為第 7 點第 3 項，審查第 5 輪升成獨立一節；文中「第 7a 點」與舊稱「第 7 點第 3 項」指同一件事。**本節不只用於 v1→v2 的一次性遷移**：任何換環境遇到來源碰不到（第 9 點的不相容漂移、404 或 `failed`／`deleted`／`expired`），或下面適用條件 (iii) 的同一 SID 漂移，都照本節處理。

#### 適用情況

API 讀來源 SID 的 metadata（`get_session`，不 invoke），而且下列任一成立：

- **適用條件 (i)** 來源版本不相容（例如 v2 API 之下任何 `foundry-native-v1` 版本），metadata 是 `idle`、`failed`、`deleted`、`expired` 或 404。
- **適用條件 (ii)** 不論相容與否，metadata 是 `failed`、`deleted`、`expired` 或 404（來源已經不存在，上面的東西永遠不會結束）。**來源相容時要再讀確認一次**：隔一段有界的間隔再讀一次 metadata（間隔由實作 PR 訂，例如數秒到一分鐘；2d-i 起送出時讀到的，第二次至少隔 5 秒、在請求等候 W 之內做，M1 第 2 步），兩次都是 404 或 `failed`／`deleted`／`expired`，才寫下「棄用來源」的決定（(g)）；第二次讀到其他狀態就不棄用，照現行 follow。任一次讀取是 5xx 或逾時，都不算 404 或終態（照原錯誤處理，第 9 點），不寫決定。理由：這個決定寫入後不翻轉、會放棄搬檔，而相容的來源本來可以照常 follow，單次 404 可能只是暫時讀錯。來源不相容時適用條件 (i) 不變，一次就算（反正碰不到它）。
- **適用條件 (iii)** 來源就是綁定列的目前 SID，綁定列這個 SID 的**同一 SID 漂移旗標**已設定（第 2 點：曾觀察到不同於建立時綁的版本；之後版本變回來也不清除；換環境新建、從沒漂移過的 SID 不適用），metadata 是 `idle` 或適用條件 (ii) 的狀態，而且換環境只剩這個 SID 上的非終態回條在等。這些回條可能釘在漂移前的版本、Hosted 永遠拒絕執行（`native_turns.py` 463–464、1595–1596），永遠到不了終態；判定它們是哪一種屬草案已知未解問題 9，**換環境不依賴這個判定**。代價要說明白：漂移之後才在目前版本接下、本來下一次叫醒會照常接續的回條，也一併放棄——回條原樣保留，換完讀到時同樣回第 8 點「這則訊息是較早的版本接下的……」通知（(e)），主人以新訊息重送。來源是相容的，所以 (ii) 的狀態（404 或 `failed`／`deleted`／`expired`）照兩次讀取規則（(m)）確認後才算。限制同 (d)：來源若正是 selector 版本算出的 SID，現行 `follow_session` 無法為同一版本另建 session（`native_environment.py` 603 新舊同 SID、609 要求較新的版本），要等發布新版本或草案機制；期間這些回條照一般未決狀態回報（第 9 點）。metadata 是 `active` 時照現行等待，不棄用（相容的 SID 還在用，等它閒置）。

**來源不相容但仍是 `active`（或 `creating`／`updating`／`deleting`）：只等 metadata，只觀察與記 log，不拒絕。**（Neal 2026-10-13 決定這種情況不可出現拒絕窗口；M1 的 A（Neal 2026-10-13 核准）以部署順序讓計畫內的遷移不出現這種來源，平台漂移時改成共用 store 安靜就立刻切換；2d-i 上線前照本段。）每次換環境的嘗試只重讀來源 metadata，不 invoke、不 stop、不切換；變成適用條件 (i) 的狀態後才棄用來源換新環境。理由：(1) v2 API 本來就 invoke 不了 v1（403 在讀 body 前），沒有別的事可做；(2) 不能 stop：現行 follow 對 `active` 的來源會 `stop_session`（`behavior_native_sessions.py` 589–590），但那是在 Hosted 掃描確認回條都已終態之後，棄用來源沒有這個確認，stop 會強制結束還沒停在安全點的回合（官方 stop 說明，第 10 點）；不 stop，v1 VM 還能自己把回條寫到終態，主人看到的是正常結果；(3) 平台在該版本的 `idle_timeout_seconds`（官方 120～3600 秒、stage 3600）沒有請求後回收 compute，**前提是 FA 完全不再呼叫這個 SID**。這個前提在 phase 2 不會自然成立，要靠 #813、第 10 點、第 7 點第 1 項，加上下面的零呼叫需求一起保證。

- **零呼叫需求什麼時候強制（#823 審查第 3 輪）：** 強制與請求內切換一起開，也就是 **2d-i 起**（目的端短切換與請求內等候上線，這種情況有無縫的路可走時）；否則擋下呼叫就只剩拒絕。兩者共用同一個開關 `FA_NATIVE_SESSION_GUARD_ENFORCE`（`native_binding.py` 454），2d-i 只有這一個開關，不分開開（總協調 2026-10-15）。2d-i 之前是**觀察模式**（#825 的 `SessionGuard` 預設）：下面的檢查點只對已判定不相容的 SID 留 `NATIVE_SESSION_CALL_INCOMPATIBLE` log，不讀 Table、不擋，呼叫照常送出；計畫內這時只有 v1，這種 SID 只會因平台漂移出現。
- **零呼叫需求（強制模式）：** 綁定列紀錄的 profile 不在支援集合內的 SID，API 的**任何路徑**都不 invoke 它、也不對它發 Files API 呼叫，改在請求內換環境（第 9 點「不拒絕」）；在送出請求之前只讀綁定列判斷，不靠對方回 403（被拒絕的請求也是一次請求，可能重設閒置計時）。`origin/main` `1d2d5f91` 有約 51 處呼叫、分在 10 個檔：回合、串流與 Responses API、session 與換環境（含搬檔）、Agent 間交辦、群組、routine、HITL 提醒、skill 草稿；全部經 `NativeTurnGateway._invoke`（`behavior_native_turn_api.py` 273–274）呼叫 `invoke_native`（`behavior_foundry.py` 476），這是 invoke 的檢查點；Files API 呼叫全部經 `SessionFiles`（`behavior_session_files.py` 34–75），這是 Files 的檢查點。零呼叫的範圍另含 `stop_session`（現行 follow 在 `behavior_native_sessions.py` 589–590 對非 `idle` 來源呼叫它）：只有下面「逾時回報」經批准的操作者決定可以例外。讀這種 SID 上的 RID（例如工作台輪詢進行中的 RID）時，API 不經該 SID 讀回條，照 (e) 回報；現行 API 沒有持久的開放 RID 紀錄（第 10 點），所以這時只能回一般未決狀態。
- **假設：** 重讀 metadata（`get_session`）不延長閒置。這是 dev 觀察、不是官方保證，第 10 點已列為實作 PR 要用 log 確認的事。
- **逾時回報（只記 log）：** 從第一次嘗試起超過 `idle_timeout_seconds` 仍是 `active`（可能是長回合還在跑，也可能有東西還在呼叫 v1 SID，例如滾動部署中的舊 API 副本）：留一次專用 log `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`，不顯示句子、不拒絕，現行 follow 照常。由總協調／操作者讀這個 log；要對來源 `stop_session`（接受可能強制結束還沒停在安全點的回合）仍需經批准的操作者決定，FA 不自動 stop、不另外發訊息或開工單。2d-i 起改照 M1「收斂後還剩的情況」：有界等待後走本節的棄用路徑，不等操作者。

等待期間的送出不拒絕（第 9 點「不拒絕」）；來源變成 `idle` 後，下一次送出時的 follow（或部署時 `follow_all`）就完成切換。

#### 做法

FA 不 invoke 來源、不經來源讀檔或讀狀態、不 stop 它；來源上進行中的任何東西（非終態回條、`admitting`、chain task、steering 輸入）都不接續、不複製、不擋切換，回條原樣保留（切換之後唯一的寫入是 (r)(s) 對進度已停止 N 分鐘、仍 `started` 的回條寫終態 `unknown`，見驗收表後「(r)(s) 的競爭」）；不搬檔（④，`files_migrated=false`）；新的 session 從它讀得懂的共用對話歷史與 checkpoint 接續（第 3 點：沒標 profile 的項目當 v1 時代讀；codec 不認識就照 CTX-03 明確拒絕、保留最後合法狀態）；通知主人：來源還在（`idle`）用第 8 點 ④ 原句，來源已失效或被平台刪除（`failed`／`deleted`／`expired`／確認過的 404）用第 8 點「舊環境已失效」句。切換後來源 SID 進之前的 SID 清單，讀到那些非終態回條時照 (e) 回「較早的版本接下的」通知。

`idle` 不代表沒有進行中的回合（第 10 點）；適用條件 (i) 與 (iii) 都接受這個風險：切換後 FA 不再 invoke 來源 SID；新 session 也接不了舊 SID 接下的工作（`_original_session`，`native_turns.py` 673–676；第 4 點第 6 步），排隊輸入另受 `execution_version` 釘選拒絕（463–464、1595–1596），所以 deferred 的回合本來就不會被接手。

**commit 後、綁定列 CAS 前崩潰：修復靠重跑切換，不靠第 4 點第 6 步。** 這時綁定列的目前 SID 仍是來源；來源不相容時送出不走到第 6 步（第 4 點第 1 步照第 9 點「不拒絕」）。下一次送出時的 follow 或部署時 `follow_all` 以同一個來源重跑切換時，必須冪等地回傳已 commit 的結果（現行 `follow_session` 對同一來源、同一目標、已 `ready` 的環境原樣回傳，`native_environment.py` 587–592），API 依它 CAS 補寫綁定列；不建立第二個新 session、不重做切換、不重新判斷 metadata（以 (g) 寫下的決定為準）。重跑前 selector 若已換到更新的版本，重跑是一次新的 follow（從已 commit 的新 SID 換到更新的版本），它的 commit 結果的之前 SID（`previous_hosted_session_id`）是那個新 SID、不是原來源；所以 API 寫綁定列時，commit 結果的之前 SID≠綁定列目前 SID，就把綁定列目前 SID 也加進之前的 SID 清單，原來源才不會漏掉（漏掉也不會誤叫人重送：(e) 判定不了就回一般未決狀態）。崩潰到補寫之間的保險：(e) 看到 `hosted_session_id`＝綁定列目前 SID（仍是來源），照進行中回報，不叫任何人重送。

**實作方式不限：** 實作 PR 可以另寫一條切換路徑，不必把這些例外穿進 `follow_session`；另寫路徑而不寫 Hosted `pending` 時，照實更新生命週期表第二列，而且同樣要滿足上一段的冪等。實作 PR 必須重新核 `follow_session` 與 API 端 `follow`／`advance` 的每一個等待原因與拒絕，把來源碰不到時結束不了的補進下表。審查第 4 輪看到、尚未逐一判斷的：`_tasks_pending` 的 `task_access_denied`（`native_environment.py` 435–453）；不搬檔路徑 `require_complete` 的 `NATIVE_FILES_CONFIRMATION_REQUIRED` 與 `check_latest_checkpoint`（`_scan_page` 371–392）；Responses 回條的 `responses_environment_blocker`（356–358）；`files_unsupported`（631–632）；Hosted 端的 `source_unavailable`／`source_active`（698–705）。另外，Hosted 端 `NativeBootstrapSource` 接受的狀態（`native_sdk.py` 131）沒有 `deleting`，API 端 `_source_session_status`（`behavior_native_sessions.py` 94）與上面的等待清單有；只有切換路徑重用 Hosted 的檢查時才有影響，屆時兩邊對 `deleting` 的處理要一致。

#### 驗收表

實作 PR 的需求與驗收測試（離線替身；除非另註，來源不相容、metadata `idle`）：

| # | 情況 | 驗收結果 |
|---|---|---|
| (a) | 同一 Agent 的 `fa/v1/turns` 有 `hosted_session_id`＝來源 SID 的 `accepted`／`started` 回條 | 切換完成；切換本身不等、不改回條；`started` 的逐位元組不變（不改寫、不換 RID），經新 session 以 `NativeRead` 讀回原回條。從沒開始的（`accepted`、`run_token` 空），來源退役與否都一樣（#825 審查第 3 輪，總協調決定：退役來源跑不了它，接手只有好處）：切換 commit 後由新 session 依 M9 以 CAS 接手（執行 SID 改成新 SID、記下原 SID `adopted_from`；原始請求位元組與 RID 不變），在新 session 跑一次、排在同一對話較新的訊息前面（#825 審查第 2 輪）。切換後的寫入只有這個接手，以及進度已停止 N 分鐘、仍 `started` 的回條寫成終態 `unknown`（(r)(s) 的競爭） |
| (b) | Hosted `agent` 紀錄的 `admitting` 不是空的（來源留下的） | 不回 `admission_pending`，切換完成；新紀錄 `admitting` 為空、`chain_task_id`＝`binding_task_id(agent, 新 SID)`；切換後同一 Agent 第一次送出新 RID→202 接單，不是 503 `NATIVE_ADMISSION_PENDING` 或 `AGENT_VERSION_FOLLOWING`；那個 `admitting` 的 RID：有非終態回條照 (e)，沒有 `rid:` 回條→`NATIVE_RID_MISSING`，同 RID 可照常送到新 session |
| (c) | 來源的 chain task `in_progress` 或有 lease，或有 steering `pending_inputs`／`active_input` | 不回 `task_active`／`task_unconfirmed`，切換完成；留 log（task ID 與筆數，不含內容）；對應的 RID 照 (e) |
| (d) | 來源 metadata 404（API 端與 Hosted 端 `NativeBootstrapSource`，`native_sdk.py` 121–123，都讀到） | 與 `deleted` 相同，切換完成；不回 `source_unavailable`；相容的來源 404 要兩次讀到才算（(m)）。限制同第 9 點：相容的來源若正是 selector 版本算出的 SID，現行 `follow_session` 無法為同一版本另建 session（`native_environment.py` 603 新舊同 SID、609 要求較新的版本），要等發布新版本或草案機制（M5 在 phase 3 消除這個限制）；期間送出不拒絕，照 `server_following` 回報、工作台以同 RID 自動重送（第 9 點「不拒絕」） |
| (e) | 讀一個非終態的 RID | 由 API 判斷（Hosted 沒有綁定列）：`receipt.hosted_session_id` 在綁定列的「之前的 SID」清單，而且**不可能再完成**（來源在切換時已退役、回條已開始——從沒開始的依 M9 接手，#825 審查第 3 輪起含退役來源；或 Hosted 已依 (r)(s) 的競爭把它標成 `unknown`）→回第 8 點「這則訊息是較早的版本接下的……」；在清單但可能還會完成→照實回報、不發這句（#825 審查第 2 輪：從沒開始的在讀取時依 M9 接手，讀回來已是目前 SID 的；來源 `idle` 上已開始的照一般進行中，寫完就是正常答案，標記後才發這句）；等於綁定列目前 SID→照一般進行中回報，**不**叫主人重送（界線 7：不叫主人重送還在跑的工作）；兩者都不是→無法判定，照一般未決狀態回報，不發這句 |
| (f) | 切換 commit 成功 | API 依 commit 結果以 CAS 更新綁定列：目前 SID＝新 SID、新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`、同一 SID 漂移旗標與通知待送位清除、來源 SID 加進「之前的 SID」清單（phase 1 起每次換環境 commit 都這樣寫，第 2 點）。「第一次棄用來源之前，不存在屬於已換掉 SID 的非終態回條」（現行 follow 每次 commit 前都要求同一 Agent 的所有回條已終態，`_scan_page` 不分 SID）是對既有資料的推論，測試證明不了：實作 PR 上線後第一次換環境時，對該 Agent 既有的回條做一次唯讀讀回（經新 session 讀同一份 `fa/v1/turns`，不叫醒任何 `idle` session，第 10 點），`hosted_session_id` 不是目前 SID 的非終態回條，把那個 SID 補進之前的 SID 清單（版本不明也可以，(e) 只看在不在清單）；讀不到就照實記成未驗證 |
| (g) | 同一次切換重試時 `idle_only` 或呼叫路徑不同（部署時 `follow_all`、送出時 follow、`advance` 的不搬檔路徑） | 「棄用來源」的決定由 API 依 metadata 做出後，**先以 CAS 寫進綁定列**（`abandon` 欄位，單一寫入者；不是 Hosted 的切換指令或進度紀錄，不需要舊 API 讀得到的 Hosted wire 變更），再送切換指令（#825 的 `session_abandon`）；之後任何呼叫路徑、任何 `idle_only` 都先讀這一列、照它做，不再判斷 metadata，不回 409 `NATIVE_ENVIRONMENT_CONFLICT`（現行 `native_environment.py` 595–599 比 `record.idle_only != command.idle_only`）；決定寫入後不因之後的 metadata 改變而翻轉，切換寫進綁定列時清掉 |
| (h) | 來源不相容、metadata `active` | 不 invoke、不 stop、不切換，只重讀 metadata；送出不拒絕：2d-i 之前只記 `NATIVE_SESSION_CALL_INCOMPATIBLE` log、照現行送出，2d-i 起照 M1「收斂後還剩的情況」（第 9 點「不拒絕」） |
| (i) | 之後的換環境（v2→v3 或漂移）遇到 `hosted_session_id` 在「之前的 SID」清單的非終態回條 | 不等它（同 (e) 的判斷）；`hosted_session_id`＝目前 SID 的非終態回條照現行等待，除非符合適用條件 (iii)（驗收見 (n)(p)(q)） |
| (j) | commit 後、綁定列 CAS 前崩潰 | 下一次送出時的 follow 或 `follow_all` 重跑切換：拿到已 `ready` 的同一結果，綁定列寫成目前 SID＝新 SID、之前的 SID 清單含來源；不建立第二個新 session；補寫前讀來源上的 RID→照進行中回報，不叫重送。變化：重跑前 selector 已換到更新的版本→重跑是新的 follow，commit 結果的之前 SID≠綁定列目前 SID→之前的 SID 清單同時含原來源與那次 commit 的之前 SID；之後讀原來源上的非終態 RID→照 (e) 回「較早的版本接下的」通知 |
| (k) | 來源 `active`、綁定列紀錄的 profile 不在支援集合；對每一個 API 入口各打一次（回合、串流、Responses、Agent 間交辦、群組、routine、HITL 提醒、skill 草稿、session 與換環境、工作台輪詢 RID） | **強制模式（2d-i 起）：** 離線替身在兩個檢查點記錄：對該 SID 的 `invoke_native`（經 `NativeTurnGateway._invoke`）與 `SessionFiles` 的 Files API 呼叫都是 0；`stop_session` 也是 0；每個入口照請求內切換或一般未決回報，不拒絕、不透傳 403。**觀察模式（2d-i 之前的預設）：** 呼叫照常，只對該 SID 留 `NATIVE_SESSION_CALL_INCOMPATIBLE`，不讀 Table，任何入口都不拒絕 |
| (l) | (h) 持續超過 `idle_timeout_seconds` | 只留一次 `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`；不顯示句子、不拒絕，現行 follow 照常；不 stop（stop 仍需經批准的操作者決定）。2d-i 起改成有界等待後走棄用路徑（M1） |
| (m) | 相容的來源：第一次讀 404，再讀是 `active`；或任一次讀取是 5xx／逾時 | 不寫棄用決定、不切換到棄用路徑，照現行 follow（5xx／逾時照原錯誤處理）；兩次都 404 才照 (d) 切換 |
| (n) | 相容的來源是目前 SID、綁定列這個 SID 的漂移旗標已設定、metadata `idle`，換環境只剩這個 SID 上的非終態回條在等（測試同時放 Hosted 拒絕執行的釘選回條，與漂移後才在目前版本接下的回條） | 換環境不等它們，切換完成；兩種回條都逐位元組不變（之後的終態標記照 (r)(s) 的競爭）；換完讀這些 RID→照 (e) 回「較早的版本接下的」通知（#825 審查第 2 輪：從沒開始的釘選回條依 M9 由新 session 接手執行；已開始的照 (e) 先照一般進行中，標記後才回通知）。同樣情況但 metadata `active`→照現行等待，不棄用；metadata 第一次 404、再讀 `idle`→照 `idle` 處理，不當成已失效 |
| (o) | 通知 | 來源 `idle`→④ 原句；來源 `failed`／`deleted`／`expired`／確認過的 404→「舊環境已失效」句 |
| (p) | 換環境 commit 後的新 SID 從沒漂移過（漂移旗標未設定）、metadata `idle`，換環境只剩它上面的非終態回條在等 | 適用條件 (iii) **不適用**：照現行等待，不棄用、不通知 |
| (q) | 同一 SID 漂移後又漂回（例如建立時 145，觀察到 141，再觀察到 145），之後 metadata `idle`，只剩它上面的非終態回條在等 | 漂移旗標仍是設定的；適用條件 (iii) 適用，照 (n) 切換 |
| (r) | （phase 2b 重核時新增，#823 審查第 3 輪定做法，第 5 輪改條件）Invocations 對話在別的 SID 上開始、**`base_ckpt` 空，而且最後的回條（`meta.rid`）不是 `completed`**（`accepted`／`started`，或終態 `unknown`），切換後主人在同一對話送新訊息。這個條件等於「對話沒有任何已完成的回合」：checkpoint 只在回合完成時保存（`_save_turn`），`_allocate` 從上一個已完成的回合帶 `base_ckpt`（`origin/main` `dc9bad4b` `native_turns.py` 1326–1337），所以「有已完成回合、checkpoint 卻不在」走不到 | 切換不受影響；新 session **不帶 checkpoint 開始**這個對話，新訊息照常執行、回合編號接續。最後回條還沒到終態時，先照下面「(r)(s) 的競爭」的規則讓它不能再完成，再開始。**這是 2b 要改的行為，不是現況**：現行 `_continuation_blocker`（1752–1765）在 SID 不同、`n≥1`、`base_ckpt` 空、`meta.rid` 不是 `completed` 時都回 `NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE`，終態 `unknown` 的對話因此永遠卡住；2b 要讓它在本列條件回 None。留下的回條除了該段要求的終態標記，其餘逐位元組不變，讀到時照 (e)（M9 上線後，從沒開始的改由新 SID 接手，接手後就是一般的第一回合） |
| (s) | （phase 2b 重核時新增，#823 審查第 3 輪定做法，第 5 輪加前提）Responses 對話的 slot（`_Meta.inflight_rid`）被留在之前 SID 上的未完成回條占住，切換後主人在同一對話送新訊息 | 切換不受影響；新 session **釋放這個 slot**，新訊息照常執行，不回 `busy_refused`／`NATIVE_TURN_BUSY`。釋放前同樣先照「(r)(s) 的競爭」讓留下的回條不能再完成。M9 上線前：所有留下的回條都這樣處理，回條除了終態標記之外不變，讀到時照 (e) 回「較早的版本接下的」通知。M9 上線後：從沒開始的（`accepted`、`run_token` 空，含 Responses `needs_response`／`not_taken`，來源退役與否都一樣）由新 SID 接手、保有 slot；它還沒送出時，主人在同一對話的下一則訊息照常接單、排在它後面（一次只跑一則，之後的依序排隊，**不拒絕**；#825 審查第 3 輪總協調決定，取代原「之後照一般 Responses『一次一則』」）。輪到的 Responses 訊息由新 SID 的等待 task 以 Agent 身分請 API 送出（`/work-policy/v1/native-agent-messages/waiting-dispatch`，與 reconcile 同一個 `_send_response`），不需要任何輪詢或下一則訊息；已開始的照 M9 前的做法 |

**(r)(s) 的依據（#823 審查第 3 輪，總協調決定；目的是切換後沒有任何對話卡住）：**

- **(r) 對 CTX-03 的解讀：** CTX-03 的拒絕（第 9 節「缺件……不啟動空白同名 SDK session」、不認識的 codec、保留最後合法 checkpoint）都以「這個對話應該有 checkpoint」為前提：有已完成的回合，就有已發布或應發布的 reference，空白開始會讓已交付的答案與效果從歷史消失。對話沒有任何已完成的回合時，從來沒有發布過 reference，也沒有「最後合法 checkpoint」可保留，所以**空白開始是合法的**，不違反 CTX-03；同一 SID 上，第一回合沒完成的對話本來也照常接續、不要求 checkpoint（`_continuation_blocker` 對同一 SID 回 None）。只要有一個已完成的回合，`base_ckpt` 就有值或最後回條是 `completed`，照現行帶 checkpoint 接續，CTX-03 的拒絕照舊（checkpoint 讀不到或 codec 不認識時明確拒絕）。
- **(r)(s) 的競爭（#823 審查第 4 輪，總協調決定）：** 現況：回合完成時**不寫** `meta`，只有配號（`_allocate`，`origin/main` `dc9bad4b` `native_turns.py` 1340）與 Responses slot（927）會寫，所以不能靠 `meta` 的 CAS 分先後；`idle` 也不代表沒有回合在跑（第 10 點）。若新 session 先開始、來源上的回合之後自己寫完，同一對話會分叉、工作重複、同時跑兩個回合（違反 ADR-0006 同一對話依序）。所以新 session **只有在留下的回條不可能再完成時**，才釋放 slot 或不帶 checkpoint 開始：
  - **從沒開始**（`accepted`、沒有 `run_token`）：可以直接進行（M9 上線後由新 SID 接手）。來源萬一醒來也開始不了：Invocations 開始前 `_require_original_session`（1639→673–676）看到 `agent` 紀錄的 SID 已換，回 `NATIVE_SESSION_CHANGED`。但這個檢查在配號（`_allocate_timed` 1686→`_allocate` 1304 起）**之前**，`_allocate` 本身不再核 SID：在 1639 通過之後才 commit 的來源回合，可以一直等到新 session 的回合完成，再配到 n+2、執行過時的訊息（重複工作），所以 2b 要在 `_allocate` 裡再核一次（見下面「剩下的風險」的圍欄）；Responses 開始前核 slot（`native_responses.py` 203–206），配號時再核一次（`native_turns.py` 1324–1325）。Responses 的 `close_suspended_start`（1355–1371）只在 `environment.state="pending"` 時關閉，棄用路徑不寫 `pending`（生命週期表第二列），所以 2b 不能靠它，要以自己的 CAS 處理或交給 M9。
  - **來源已退役**（404 或 `failed`／`deleted`／`expired`，平台跑不了它）上**已開始**的，或回條**已是終態**（`unknown` 等）：可以直接進行（退役來源上從沒開始的照上一項接手，#825 審查第 3 輪）。
  - **`started`、有 `run_token`，來源是 `idle`（不是退役）：新訊息照常接單、排在留下的回條後面（#823 審查第 5 輪改；取代原「最多等 10 分鐘就放棄」）。** 做法與今天「同一對話前一則還在跑」相同：新 session **接單**（Invocations 回 202、進 chain 佇列，`native_turns.py` 1110–1116；Responses 也照常接單排隊、不回 `busy_refused`，見 (s)），不拒絕、不在請求裡等；工作台看到的就是一則排隊中的訊息，不顯示換版句子，也不走 `server_following` 重送。現行 blocker 在接單前（1035–1037）回的 409 `NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE` 2b 不能沿用（工作台只對 `AGENT_VERSION_FOLLOWING`／`NATIVE_SESSION_CHANGED` 自動重送，`web_test_console/index.html` 773），等待改在 Hosted 的佇列裡：排到這一則時，留下的回條還是 `started`，就不配號、留在佇列稍後再看（實作 PR 選既有的延後機制），直到下面任一成立：
    - **來源寫完（終態）：** `completed` 照有 checkpoint 接續；`unknown` 等其他終態照 (r)(s) 開始。
    - **進度停止 N 分鐘（N 預設 10，實作 PR 可調）：** 判斷「還在跑」只看共用 store 裡會變的東西：留下回條的 ETag，與它的公開進度快照 `progress:<sha256(RID)>`（`native_progress.py` `_key`、`NativeProgressRecorder._save`）的 ETag。新 session 第一次看到時記下兩個 ETag 與 Hosted 時鐘的時間；任一個變了就重記、重新計時；連續 N 分鐘兩個都沒變，才當成遺失。觀察紀錄要持久（「協調物件生命週期」的 `stall:` 列），不用記憶體計時，否則 VM 換掉或重啟就重算。進度快照不是每個回合都有：它只在 SDK 送出公開進度事件時才寫（best effort，寫入失敗後就不再寫，`observe`／`_flush`），沒有快照就只看回條 ETag，從第一次觀察起算。**不用** `get_session` 的 `last_accessed_at`：dev 唯讀證據裡它在兩次快照之間從沒變過（「Verified against」的 2026-10-13 dev 證據），而且 FA 不再呼叫來源，它不代表回合有沒有在跑。
    - **外層上限：** 進度一直在變的回合，由來源自己的 Hosted 回合逾時（`ctx.timeout_exceeded`→`NATIVE_TURN_TIMEOUT`，`native_turns.py` 1655、1711）結束、寫成終態，本文不另訂數字。
    - **當成遺失時：** **先**在共用 store 以 CAS 把回條寫成 `unknown`（`_unknown`，1393–1411，只接受 `accepted`／`started`，用讀到的 ETag），**再**釋放 slot 或開始。這次 CAS 失敗**不代表**來源剛寫完：ETag 在仍是 `started` 時也會變（例如來源回合記下交辦的 `_retain_delegation`，403–416），而且 `_unknown` 在讀回仍是 `accepted`／`started` 時會丟 503 `NATIVE_STATE_STORE_UNKNOWN`（1409–1411）。所以 CAS 失敗或 503 一律重讀：已是終態就照上面處理；仍是 `started` 就把它當成「有變化」，重記 ETag、重新計時、繼續排隊，不把 503 往外丟給主人。
    - 代價照實說：一直沒有公開進度、回條也沒寫的回合（例如一個很長、不出事件的工具呼叫）超過 N 分鐘會被當成遺失；正常長回合只要還有進度就不會被放棄。等待期間同一 Agent 的其他對話也排在後面（ADR-0006 同一 Agent 依序），與前一則還在跑時相同。
  - **為什麼標記後來源晚到的完成進不來（本段與下一段的行號核到 #835 `53b61f57` 的 `native_turns.py`；原核 `origin/main` `dc9bad4b`）：** 圍欄是回條本身的 `run_token`／ETag，不是 `_original_session`（它只在開始與接單時檢查，`_save_turn` 不呼叫）。來源執行中每 1 秒重讀回條（第 28 版與 #835 之前每 0.1 秒；store 結果不明不算答案），狀態或 `run_token` 變了就停止 SDK（`_observe_control` 1685–1688、`stop_requested` 263–264）；SDK 結束後重讀，狀態不是 `started` 就不保存（`_execute_started` 1926–1930，例外路徑 1960–1962 同樣）；保存時回條用原 ETag CAS（`_save_turn` 1795→`_cas` 1055–1072），ETag 已變就拿回 `unknown` 的回條、不改寫（1796–1801）。
  - **剩下的風險（照實說）：** `_save_turn` 在回條 CAS **之前**已寫入 checkpoint 與 `turn:{n}`（1764–1792），被擋下的晚到保存會留下沒有人引用的項目（配號只用 `completed` 回條的 checkpoint，1439–1442），不影響對話，FA 不刪；那個回合已做的外部動作（工具呼叫、交辦）不會撤回，結果也不交付，主人看到 (e) 通知、以新訊息重送；只有進度停止 N 分鐘的回合會被這樣放棄（上一段「代價」）。**來源的 store 讀取一直結果不明（5xx）時的上限（#835）：** 執行中的回合連續 `CONTROL_UNCONFIRMED_FENCE_SECONDS` 沒有一次確定的回條讀取，就設 `fenced`、停止 SDK（`_observe_control` 1674–1677），不再跑到 SDK 預設的每回合 1 天逾時（`multi_turn_task` 沒有另設）。這個值是 `STALL_SECONDS / 2`（N/2＝300 秒，由同一個常數算出，兩者不會各自漂移）。store 恢復後照上一段寫成 `NATIVE_TURN_UNKNOWN`；仍不通時回條留在 `started`，交給新 session 的停滯規則，晚到的保存照樣被 ETag CAS 擋下。上限從最後一次確定讀取起算：回合若在 store 斷線前已經超過 N/2 沒有進度，停下的時間仍可能比新 session 開始晚，最多晚 N/2 再加一次退避（≤5 秒），與上一段「代價」同類。只在等 execution slot、還沒開始的回合不套這個上限（沒有外部動作，停下只會丟訊息）。另外，`dc9bad4b` 的 `_allocate` 不檢查 `meta.rid` 的回條是否已終態（當時 1326–1340）：來源上已 `started`、還沒配號的回條（這時 `meta.rid` 不是它，不符合 (r)）若在新 session 配號之後才配號，會把 `meta.rid` 改成自己。**2b 必須補這個圍欄，兩項都在 `_allocate` 裡、meta CAS 之前檢查**（已補：`_allocation_fence`，`_allocate` 1443 呼叫，meta CAS 在 1460）：(1) Hosted `agent` 紀錄的目前 SID 等於這個 session 的 SID（#823 審查第 5 輪補；開始前的 `_require_original_session`（1842）在配號之前，擋不住「它通過之後才 commit」的來源回合等到新 session 的回合完成後配到 n+2）；(2) `meta.rid` 不是自己、而且它的回條不是終態。任一項不成立就不配號（來源的晚到回合以 `NATIVE_TURN_UNKNOWN` 結束、不進 SDK）。新 session 自己的回合照上一段先排隊等留下的回條、之後才配號，所以這兩項只會擋到來源的晚到回合。(1) 之後、meta CAS 之前才 commit 的情況擋不住，但這時 meta CAS 寫成的 `meta.rid` 就是來源這個 `started` 回合，新 session 的回合照 (r)(s) 的等待規則排在它後面，不會分叉；新 session 先配號的話，來源的 meta CAS 因 ETag 已變而失敗。兩項都以測試確認。
- **(s) Hosted 怎麼判斷，不需要綁定列：** commit 之後只有目前 SID 能接單，所以 slot 的回條（`meta.inflight_rid`）非終態、而且它的 `hosted_session_id` 不是執行中的 SID，就是「被之前的 SID 占住」。釋放是對 `meta` 的一次 CAS（改成新 RID），在上一段的條件成立之後才做；回條除了上一段的終態標記之外不動。
- **測試（2b 實作 PR）：** (r) 最後回條是 `accepted`、終態 `unknown`、來源已退役三種，新訊息都在新 session 不帶 checkpoint 執行、回合編號接續、留下的回條讀到時得到 (e) 通知；有已完成回合（`base_ckpt` 有值或最後回條 `completed`）的照常帶 checkpoint 接續，checkpoint 讀不到時照 CTX-03 拒絕。`started` 且來源 `idle`：(1) 新訊息立刻 202 接單、排在留下的回條後面，工作台看到一般排隊訊息、沒有 `server_following`；來源之後寫完→新訊息帶這次的 checkpoint 接續；(2) 回條與進度快照連續 N 分鐘沒變→回條先成 `unknown`、再開始新回合，之後來源的保存被擋下、回條維持 `unknown`、`turn:{n}` 成為孤兒、新回合不受影響；(3) 進度快照（或回條 ETag）在 N 分鐘內變過→重新計時、不標記，長回合不被放棄；沒有進度快照的回合只看回條 ETag；(4) 標記 CAS 時來源剛好寫完→CAS 失敗，重讀是 `completed`，照有 checkpoint 接續；(5) 標記 CAS 失敗或 `_unknown` 回 503、重讀仍是 `started`→繼續排隊、重新計時，不把 503 回給主人；(6) 新 session 的 VM 重啟後，`stall:` 觀察紀錄讀回、不重算。配號圍欄：(7) 來源回合在 1639 通過後才 commit、等新 session 的回合完成後才配號→`_allocate` 核到 `agent` 紀錄的 SID 已換，不配號、不執行；(8) commit 落在 `_allocate` 的 SID 檢查之後、meta CAS 之前→`meta.rid`＝來源回合，新 session 的回合排在它後面照 (1)～(5)；(9) `meta.rid` 的回條非終態時晚到的配號不改 `meta`。(s) 同樣三種順序；slot 改成新 RID 後，留下的回條讀到時得到 (e) 通知；slot 屬於目前 SID 的非終態回條照舊 `busy_refused`。

### 8. 使用者看到的通知

| 情況 | 句子（工作台頁尾與狀態欄位，每個 Agent 由主人本人看到一次（M4 的 A 第 1 項，不分對話）；「平台在同一個 session 換了版本」只在工作台狀態欄位，見該列） |
|---|---|
| 換到新版本、工作檔已帶過去 | 「助理已更新到新版本，工作檔也一起帶過來了。」 |
| 換到新版本、沒帶工作檔（④），舊環境還在 | 「上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。」（**Neal 2026-10-20 決定**，改成「上次」說法：換環境通知可能一直待送到主人的下一次回答才出現，例如平台漂移之後隔很久，「這次」會被讀成「今天」；新句不論緊接在切換之後或很晚才出現都成立，「沒有搬工作檔」仍明示，也不宣稱檔案已遺失；取代 2026-10-15 核准的「已換到新版環境；這次沒有搬工作檔，舊環境的檔案不會出現在新環境。」，再往前取代 2026-10-04 原句「……舊環境與檔案都保留。」：切換後 FA 不再經舊 SID 讀檔，第 7a 點「做法」；程式改動 #850）。2d-i 用在來源 `idle`、碰不到或沒有工作檔時；來源 `active` 而且有工作檔就照常帶過去，用上一列（M1 第 2 步「工作檔」） |
| 換到新版本、沒帶工作檔，舊環境已失效或被刪（第 7a 點：`failed`／`deleted`／`expired`／確認過的 404） | 「上次換環境時，舊環境已失效或被平台刪除，沒有搬工作檔；之前 VM 裡的工作檔可能已經不在了。」（總協調 2026-10-12 決定，待 Neal 確認；**總協調 2026-10-20 決定**改成「上次」說法，延伸 Neal 2026-10-20 對上一列的決定，理由相同：通知可能很晚才出現，「這次」會被讀成今天） |
| 平台在同一個 session 換了版本（同一 SID 漂移旗標與通知待送位一起設定，第 4 點；dev 實際看過的就是這種） | 「平台把助理的執行環境換成了另一個版本（vX→vY）；FA 已記錄，相容就照常執行。」（vX＝SID 建立時綁的版本、vY＝設定漂移旗標時觀察到的版本，與待送位一起存在綁定列，第 2 點；總協調 2026-10-13 決定，待 Neal 確認）。由綁定列的通知待送位交付：每個 Agent 由第一個主人本人的工作台回應在狀態欄位顯示一次，不分對話；不進聊天文字，所以 Teams 不顯示也不清除（M4 的 A 第 2 項）；非主人呼叫者不顯示也不清除（第 4 點末段） |
| 平台重建了環境（`get_session` 的 `created_at` 改變；只是順便讀到的保底訊號，沒有保證的讀取點，可能漏掉；dev 叫醒不會改，目前沒看過） | 「平台重建了助理的執行環境，之前 VM 裡的工作檔可能不在了。」 |
| 版本不相容 | **不顯示句子、只記 log**（#823 審查第 3 輪；Neal 2026-10-13：M2 一律不拒絕）：訊息照常送出（2d-i 之前）或在請求內換到新 session（2d-i 起），沒有「沒送出」的情況，原句「……這則訊息沒有送出……再送一次即可」已刪；只留 `NATIVE_SESSION_CALL_INCOMPATIBLE` log（第 7a 點零呼叫需求） |
| 不相容的舊環境逾時仍在運作（第 7a 點逾時回報） | **不顯示句子、只記 log**（#823 審查第 3 輪）：只留一次 `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`，由總協調／操作者讀；原句「舊環境仍在運作……這則訊息沒有送出」已刪 |
| 新版本 crash-loop、已退回先前的版本（閒置升級第 5 點、M4；phase 4d） | 「助理已退回先前的版本；剛才中斷的訊息請再送一次。」（**Neal 2026-10-19 決定**，取代 M4 第 5 項的草案句「新版本 vN 執行不穩定，助理已退回 vM；……」，用較短、不帶版本號的版本）。工作台與 Teams 同一句；退回時沒帶工作檔（這次退回沒搬，或之前沒送出的換環境通知本來就是沒帶工作檔），句後接上表「上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。」（M4 的 A 第 1 項：「沒帶工作檔」不被「已退回」蓋掉） |
| 新版本 3 次沒通過健康檢查、本 Agent 否決它（閒置升級第 5 點、U3、M4 第 6 項；phase 4b-4） | 「助理這次沒有更新，會繼續使用目前的版本。」（**Neal 2026-10-19 決定**，取代第 5 點的草案句「新版本 vN 沒通過檢查，助理繼續用 vM」，不帶版本號）。U3 第 3 次失敗寫本 Agent 否決的同一次寫入放進通知槽（槽值 `vetoed`，只放進空的槽；之後任何換環境通知都蓋過它），只在工作台狀態欄位交付一次，Teams 不顯示、也不清除它（總協調 2026-10-19：Teams 使用者本來不知道有更新在等，這句只是雜訊；M4 第 6 項維持只在狀態欄位）；工作台另帶 `X-FA-Upgrade-Vetoed-Notice: 1` 才收得到、才清除，舊工作台拿不到也不清 |
| 釘在舊版本的工作無法接續（第 9 點） | 「這則訊息是較早的版本接下的，目前環境無法接續；請用新訊息再送一次。」**交辦／peer 訊息的寄件者（總協調 2026-10-19 決定）：** 同一個意思——`refused:<RID>` 保證這個 RID 從沒執行，所以寄件者的 blocked 通知（`prepare_outcome`，`NATIVE_EARLIER_VERSION_UNFINISHED`）說「對方沒有執行這次交辦……可以用新的交辦再送一次」，不用其他未確認結果的「請勿重新交辦……不表示對方已停止或沒有外部效果」。那句只留給真正不明的結果。 |
| 升級中，這則訊息先暫停（草案上線後） | 沿用現有「換版中」的同 RID 重送提示；補一句「助理正在閒置更新，訊息稍後自動送出」 |
| 有新版本在等（草案上線後） | 狀態欄位：「有新版本 vN，會在助理閒置時更新。」 |

工作台不顯示版本號以外的內部識別；API 回條帶前一個 SID、新 SID、理由、`files_migrated`，供稽核。

**待辦（已完成，phase 2c #830）：本節的通知都要由工作台顯示，包括不搬檔那句。** 2026-10-06（`f56020a5`）工作台不再處理換環境，「ready 後顯示原句」隨之移除（`bridge.py` 的 P06 註解；`index.html` 的 `agent.environment` 只叫醒等待中的請求），當時只有 API 回條帶 `files_migrated`／`files_omission_reason`。phase 2c 補回：換環境 commit 寫綁定列時，同一次寫入在通知槽記下換環境通知（M4 的一般化：已帶工作檔／沒帶工作檔、舊環境還在／舊環境已失效，沒送出的較嚴重通知不被蓋掉；`files_migrated` 沒有值（image 沒有工作檔功能）照實記成沒帶工作檔），與同一 SID 漂移通知同樣由第一個主人本人的回應帶上、寫出後清除（第 4 點末段）；工作台在輸入框下方狀態列顯示一次、不當錯誤；Teams 主人的最終回覆把換環境的句子接在回覆最後，同一 SID 漂移那句只在工作台狀態欄位（M4 的 A 第 2 項），Teams 不帶也不清。「這則訊息是較早的版本接下的……」照舊在那則訊息上顯示。仍未做的：「平台重建了……」（沒有讀到不同 `created_at` 的讀取點，第 4 點）、「升級中」「有新版本在等」與退回句（草案上線後）。

「每個對話顯示一次」需要記住哪個對話已經通知過；定稿輪的解法是不建立這種標記（M4 的 A，Neal 2026-10-13 核准：每個 Agent 一個通知槽）。決定部分只要求通知會出現，不依賴這個標記：換環境的通知由上面的待辦補回（工作台目前不顯示）；平台在同一個 session 換了版本，靠綁定列的通知待送位，由第一個主人本人的回應帶上、寫出後清除，不分對話（第 4 點末段）；平台重建（`get_session` 的 `created_at` 改變）只在順便讀到時附在那個請求的回應上，沒有保證會偵測到（第 4 點）；SID 換過之後無法接續的 RID，讀到時以之前的 SID 清單判斷、回通知句子，不需要另存標記（第 7a 點 (e)）；同一 SID 漂移後釘在舊版本的 RID 見第 9 點與已知未解問題 9。

2026-10-04 原句的「舊環境與檔案都保留」只到平台 30 天無活動後永久刪除 session 為止（來源已失效或被刪時改用上表「舊環境已失效」句）；草案 ② 上線後，長期副本是保留 30 天的 Blob 快照（Neal 決定 2）。實作 PR 調整這句時另請 Neal 確認文案。**已確認（Neal 2026-10-15）：** 改成上表 ④ 列的新句，程式改動 #850。

### 9. 漂移怎麼處理（整理）

<a id="no-refusal"></a>**不拒絕（M2，Neal 2026-10-13；#823 審查第 3 輪統一）：** 版本不相容或來源碰不到，**都不拒絕主人的訊息**。本文其他地方的「照第 9 點」「不相容就不送」一律照本段讀：

- **2d-i 之前（phase 2b、2c、3）：** FA 不因版本相容性新增任何拒絕。已判定不相容的 SID 只留 `NATIVE_SESSION_CALL_INCOMPATIBLE` log（零呼叫檢查點的觀察模式，第 7a 點），請求照現行路徑送出；換環境照現行 follow，來源碰不到時照第 7a 點；切換未完成時照現行 `server_following`，工作台以同 RID 自動重送。這時 API 只送 v1、所有 image 都收 v1，計畫內不會出現不相容；萬一出現（平台漂移到不收 v1 的 image），Hosted 自己回的 403／409 照現行處理。
- **2d-i 起：** 零呼叫強制與請求內切換一起開：不呼叫不相容的 SID，在請求內做目的端切換（M1、M5），訊息照「切換期間送出的訊息」等候後送到新 session；超過 W 回 `server_following`、工作台同 RID 自動重送，不是「沒送出」。
- **不在此列（照原規則）：** metadata 讀不到、401／403、5xx、逾時與回應遺失（界線 7）；CTX-03 的 codec 與缺件拒絕；`NATIVE_STATE_SCOPE`／`NATIVE_CAPTURE_CHANGED`；`execution_version` 釘選（第 3 點）；功能層級的 `NATIVE_FILES_UNSUPPORTED`；舊版 `fa-chat-…` 對話的 reset 規則。

| 觀察 | 做法 |
|---|---|
| 醒來的版本≠紀錄、但相容 | 照跑（第 4 點第 6 步），記錄新版本；同一次 CAS 設通知待送位，由第一個主人本人的回應帶上第 8 點「換成了另一個版本」通知（第 4 點末段）。換到目標版本是另一件事（草案第 5 點），這裡不做 |
| 醒來的版本不相容（409 帶 `actual_version`，或舊 image 的 metadata；或 403 `NATIVE_TURNS_DISABLED` 且綁定列的 SID 沒變＝profile 不同） | 不拒絕（本點「不拒絕」）：2d-i 之前只記 log、照現行送出與回應；2d-i 起不 invoke 這個 SID、在請求內換到新 session。不相容的 VM 不搬檔。換環境：草案上線前靠第 7a 點（不相容的來源碰不到時棄用來源換新環境）。限制：若漂移的正是 selector 版本算出的 SID（A 的情況），現行 follow 沒辦法為同一版本另建 session，要等 M5（phase 3）；期間照 `server_following` 回報、工作台同 RID 自動重送，不進 409 迴圈 |
| 409 `NATIVE_SESSION_CHANGED`，綁定列的 SID 已換 | 不是漂移：第 4 點第 6 步「SID 已換」——只有確定沒派送才改送新 SID；舊 SID 上非終態的 RID 不可改送 |
| 409，SID 沒變，V_obs＝V_rec | 不是漂移，是綁定不一致：第 4 點第 6 步「不一致」分支 |
| 漂移，而舊 SID 上還有釘在舊版本的工作（排隊或已接受，`execution_version`≠V_obs） | Hosted 照舊拒絕執行（第 3 點）。決定（Neal 決定 8）：這些 RID 不改寫、不換 RID 重送，原 Hosted 回條保持原樣，請主人以新訊息重送（第 8 點）。**SID 換過之後**（換環境或棄用來源換新環境，第 7a 點）：回條的 `hosted_session_id` 在之前的 SID 清單，讀到時回「版本已變、無法接續」的通知，不需要另存標記，也不擋換環境。**同一 SID 相容的漂移**（V_obs≠V_rec、SID 不變、不換環境）：回條的 `hosted_session_id` 就是目前 SID，回條也沒有時間或執行版本欄位，API 分不出哪個 RID 釘在舊版本；判定方式屬草案（已知未解問題 9），判定出來之前這些 RID 照界線 7 以一般未決狀態回報，不叫主人重送。這些 RID **不擋換環境**：漂移過的 SID 變成 `idle` 或已失效後，換環境照第 7a 點適用條件 (iii) 不等它們，換完 SID 進之前的 SID 清單，讀到時照前一種回通知；換環境不依賴已知未解問題 9。不相容的漂移會換環境，換完就屬前一種。閒置升級機制另有：閒置升級時「不擋升級」的一般機制（原已知未解問題 1、9；從沒開始的回條改由新 SID 接手見 M9） |
| `get_session` 的 `created_at` 改變（只在既有的 `get_session` 讀取順便比對，沒有保證的讀取點；叫醒不會改；漂移不靠它） | 記錄，並把通知附在讀到它的那個請求的回應上（第 4 點、第 8 點）；每個對話各一次的去重屬草案（已知未解問題 8）；照相容規則決定（不相容照本點「不拒絕」） |
| metadata 讀不到、401／403、逾時、5xx | 照原錯誤處理，不推定漂移 |
| 404、`failed`／`deleted`／`expired` | 舊 SID 已不能用：不呼叫、不刪除；**不拒絕**（本點「不拒絕」）。2b 起：送出時 follow 走第 7a 點的棄用來源換新環境（適用條件 (ii)，不論相容與否；相容的來源要再讀確認一次；限制同上），未完成時照 `server_following`、工作台同 RID 自動重送；之後由草案機制（④ 不搬檔）；通知用第 8 點「舊環境已失效」句。2d-i 起（M1 的 A）：送出時讀到 404 或已失效，API 在這個請求內做目的端切換（④），訊息照「切換期間送出的訊息」等候後送到新 session（見 M1 的 A「停用 follow 之後的退路」） |

### 10. 不叫醒已回收 session 的規則（立即適用，phase 0）

- 任何「只為了檢查」的讀取，先用 `get_session`（不 invoke），只有 `active` 才可再呼叫 session 或讀它的檔案。
- `FollowFiles._source()` 與 `require_source_version()` 原本接受 `idle`：改成只接受 `active`；`idle` 記 `source_unavailable`，走 2026-10-04 的不搬檔路徑（API 回條記 `files_migrated=false`、`files_omission_reason`；工作台目前不顯示通知，見第 8 點待辦）。stop 前的 `_source_session_status` 讀取同理：stop 只送給 `active`。
- **~~窄例外：FA 自己 stop 之後的停止後核對~~（2026-10-16 起取消；dev 驗證項 ② FAIL，見本點末）。** 原本搬檔的最後一步在 stop 之後經 Files API 再讀一次來源（`stopped_step`）；dev 證實這次讀取會把停掉的 session 叫醒，而且醒在 selector 選的版本、不是它釘的版本，Hosted 的 `source_stopped` 因此 409，背景柵欄再把它當成搬不了、不搬檔 commit。**現在：** 第二次讀取移到 stop **之前**、來源還 `active` 時——就是 `before_stop` 的容器內 `verify_source`（每個項目照 manifest 重新 hash、重新列目錄），柵欄期間不接新回合；stop 之後 API **不再**對這個 session 發任何 Files 呼叫或呼叫來源 helper，只送目的端 `source_stopped`／`list_stopped` 步驟紀錄（`list_stopped` 的頁面照 manifest 產生，只為滿足 H2d image 的關卡；之後 Hosted 不再要求時拿掉），從目的端已核對的 staging 發布。FA 的 stop 已記下而且 staging 已核對之後，來源若又醒著（任何版本）一律當成被叫醒：背景柵欄再 stop 一次、照 staging 發布或等下一次；第一次看到它醒在別的版本時清掉柵欄的推進計時（每個 process 每次叫醒一次，叫醒之前的錯誤不算進 30 分鐘；清除的 Table 寫入失敗就不算清過，這一輪不讀目的端、不讀計時，下一輪先再清一次）。`NATIVE_FILES_SOURCE_UNAVAILABLE`／`NATIVE_FILES_UNAVAILABLE` 只在出錯後再讀 metadata、看到來源 `active` 時才當成被叫醒：不算永久性錯誤、也不起算計時；來源仍 `idle` 就是目的端自己的錯：卡住的 `admitting`（409）照永久性處理，OSError（503）可能會好，照暫時性的 30 分鐘上限（現行 flag-off 的 follow 只改順序，其餘不變）。以下保留原條件供對照：只在 **FA 自己的 stop 已被平台接受並記下**（`stop_started`，在 `stop_session` 成功之後才寫；`session_already_stopped` 不算）才送停止後的步驟；Hosted 在每一步 `source_stopped`／`list_stopped` 也再核 inactive。來源沒有這個紀錄就變 inactive（例如 FA 的 stop 失敗後被平台回收）＝被回收：不讀檔，以 `source_not_migratable` 不搬檔完成。依據：官方說 stop 結束 compute、保留檔案系統（[Manage hosted agent sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)）；Files 呼叫會不會重新配置 compute，官方沒有寫。代價：stop 已被接受、紀錄沒寫成（崩潰或寫入失敗）時，下一次當成被回收，不搬檔，但不叫醒。
- **背景切換柵欄的明確例外（2d-i P5，#858 審查第 4 輪，2026-10-16）：** FA 在綁定列的 `upgrade` 寫下自己停止來源的標記（`stop_started`）之後，若來源是 `idle`（或醒在別的版本：只有叫醒會這樣），FA 補記檔案進度的 stop，把這次搬移做完，即使檔案進度沒記下 FA 的 stop（停止已被接受、紀錄遺失），或平台其實先回收了它（`session_already_stopped`）。**2026-10-16 起不再讀它的檔案**（上一條，驗證項 ②）：已核對的 staging 就是要發布的內容，stop 之後只送目的端的步驟紀錄，所以不會為讀檔叫醒它。`active` 照現行 follow 再停一次；`creating`／`updating`／`deleting` 等下一次觸發。來源已 `failed`／`deleted`／`expired`／404 時檔案已經不在：照第 7a 點棄用來源（同樣要兩次相隔的讀取確認），不搬檔 commit，通知「舊環境已失效」（`source_retired`）。送訊息時遇到這個柵欄，也由同一個收斂步驟處理（每次重送推一步），不會改走不搬檔的系統政策。
- **背景切換柵欄搬不了檔（2d-i P5，#858 審查第 5 輪，總協調 2026-10-16 依 Neal「不卡住 Agent、還搬得動就不丟檔、搬不動就照實說」）：** FA 停來源之後推進柵欄時出錯，分兩類。**暫時性**（5xx、429、逾時、網路、`NATIVE_FILES_UNKNOWN`、metadata 讀取失敗、ETag 競爭等不在下列清單的錯誤）：柵欄原樣留著，下一次觸發（回合結束或送出）再推；但這個 process 內從第一次失敗起連續只見暫時性錯誤超過 30 分鐘（`CUT_TRANSIENT_SECONDS`），就當成搬不了（`basis="files_overdue"`，等了 30 分鐘本身就是確認；2d-i 開啟時這個計時存在綁定列，超過 60 分鐘還在，表示 30 分鐘之後沒有出錯的一輪，是中斷（凍結、部署）不是連續失敗，下一次出錯重新起算）。**永久性**（重試也不會成功）：`NATIVE_FILES_CONFLICT`（檔案位元組或 hash 與保存清單不同、串流超出界限）、`NATIVE_FILES_LIMIT`、`NATIVE_FILES_UNSAFE`、`NATIVE_FILES_SOURCE_CHANGED`（來源清單變了）、`NATIVE_FILES_SOURCE_UNAVAILABLE`（不是 FA 停的）、`NATIVE_FILES_UNAVAILABLE`／`NATIVE_FILES_UNSUPPORTED`（沒有驗證過的清單）——**例外（2026-10-16，驗證項 ②）：** 已核對的 staging 加上 FA 已標記的 stop 之後，出錯後再讀 metadata 看到來源 `active` 時，`NATIVE_FILES_SOURCE_UNAVAILABLE`／`NATIVE_FILES_UNAVAILABLE` 只表示來源又醒著，不算永久性、也不起算任何計時（來源仍 `idle` 時是目的端自己的錯：卡住的 `admitting`（409）照永久性確認，OSError（503）可能會好，照暫時性的 30 分鐘上限；metadata 讀不到算暫時性），柵欄留著，下一次再 stop 一次並照 staging 發布（`NATIVE_UPGRADE_CUT_SOURCE_WOKEN` 警告）——、`NATIVE_FILES_ACCESS_DENIED`（Session Files 回 401／403）。永久性錯誤要在之後的觸發、隔至少 5 秒（`RETIRED_CONFIRM_SECONDS`）再讀到一次才算確認，中間出現暫時性錯誤就重新起算（`basis="files_unmovable"`）。確認後在柵欄自己的目的 SID 照第 7a 點棄用路徑不搬檔 commit（先寫決定，任何呼叫者都能接著做完）：綁定列 `last_switch` 記 `files_migrated=false` 與上述 `basis`，來源還在（`idle`／`active`）所以通知用第 8 點 ④ 原句，不新增文案；同時記 `NATIVE_UPGRADE_CUT_FILES_UNMOVABLE code=… basis=…` 警告。來源已失效時仍照上一條走「舊環境已失效」；來源狀態讀不到或是 `creating`／`updating`／`deleting` 時不做決定，等下一次。只要來源 `idle`／`active` 而且讀得到、搬得動，檔案照常搬。限制：兩個計時都只在這個 process 的記憶體裡（重啟或換副本重新起算）；目的 SID 自己的錯誤（例如 `NATIVE_SESSION_CHANGED`）不在這條處理範圍。
- 舊版 `fa-chat-…` 對話（`behavior_work_policy.py` admission 的 `verify_session`）：`verify_session` 只讀 metadata，之後若原 SID 不是 `active`，不再為了驗證去呼叫它，回 409 `CONVERSATION_SESSION_UNAVAILABLE`；如何接續這些舊對話照現有明確 reset／新對話規則。接受的使用者代價：閒置超過逾時的舊對話要明確 reset（reset 用 `stop_session`，不 invoke）或開新對話。
- **metadata 的 `idle` 不代表沒有未完成的回合。** 官方說明：處理器在關機寬限期內停在安全點並 defer 時，response 或 task 會保持進行中，留給之後的 lifetime 接手；自己 stop 的 session 不保證會自行重啟（[How stopping a session affects an in-flight turn](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#how-stopping-a-session-affects-an-in-flight-turn)）。所以 `idle` 只代表「compute 不在、現在不能碰」；有沒有未完成的工作，只能看 API 端的開放 RID／回條紀錄。現行 API 沒有持久的開放 RID 紀錄（只有記憶體 LRU，第 3 點），這是草案已知未解問題 1 的起點。
- 注意：`get_session` 本身不會喚醒 compute 是 dev 觀察，官方文件沒有寫。**dev 驗證項**：① **PASS（2026-10-13，release 28 dev 部署前；[紀錄](../progress.md#adr0009-phase0-merged-1013)）：** 相隔約 50 秒取兩次快照，對 24 個 session 各做 list＋get，沒有任何欄位改變，`get_session` 不喚醒、不改 `last_accessed_at`／`created_at`（仍只是 dev 觀察，官方沒寫）；② **FAILED on dev 2026-10-16（停掉的 session 被讀檔會醒，可能醒在別的版本）：** Agent B 的背景切換柵欄在 FA stop 來源（建立時釘 113）之後，`stopped_step` 經 Files API 下載檔案的同一秒（03:02:00–02）來源出現新 container，之後讀回 `active`／114（selector 選的版本，不是釘的 113）；Hosted `source_stopped` 回 409 `NATIVE_FILES_UNAVAILABLE`，第二次讀到後柵欄不搬檔 commit 的路就打開了。已照上面「窄例外」取消的做法修正：stop 之後不再讀來源。
- **已接受的殘餘風險（phase 0）：** (a) 部署前已在途的 follow：舊程式在送 stop 之前就寫了 `stop_started`，之後 stop 失敗、來源又被回收，新程式分不出這筆紀錄是舊程式寫的，`stopped_step` 會憑它讀一次該來源的檔案（共用 README「§10 的窄例外與已接受風險」同樣記載；2026-10-16 起 `stopped_step` 不再讀來源，這一項只剩「憑它從 staging 發布」）。(b) 窄時窗：平台已接受 FA 的 stop，但 `stop_started` 還沒寫成（崩潰或寫入失敗），重試時平台仍回報 `active`，`before_stop` 會對一個 FA 剛 stop 的來源呼叫 `verify_source`（main 不會，因為 main 是先記錄再送 stop）。(a)(b) 都接受為風險；若 dev 證據顯示真的有被叫醒，已知的修法是送 stop 之前先寫 stop 意圖標記。(c) 讀與呼叫之間的時窗：metadata 讀到 `active` 之後、真正呼叫或讀檔之前，session 剛好閒置逾時變 `idle`，這次呼叫就會叫醒它（#815 只在讀 metadata 時檢查，未涵蓋這個時窗）。接受，範圍有限：時窗只有一次讀取到一次呼叫之間，每次最多多叫醒一次，代價和一般回合叫醒相同。
- **phase 0 的完成條件：沒有任何路徑叫醒非 `active` 的 session**（上面的窄例外原本只讀 FA 自己 stop 的來源；它就是 ② FAIL 的原因，2026-10-16 起取消）。實作是 #813（已合併，`8732d7b7`）**加上**本點「follow 與舊對話只碰 `active`」的 #815（已合併，`01e0fdb2`），**兩者都已合併，phase 0 的程式部分完成（2026-10-13）**。只合併 #813 不算完成：#813 只處理 prepare 定位與 API 舊 reader 快取，follow 檢查舊來源時仍會叫醒 `idle` 的 session；#815 補上這一塊。上面的 dev 驗證項 ① 已 PASS，② 在 dev 2026-10-16 FAIL（停止後讀檔叫醒來源），已改成 stop 之後不讀；合併不代表已部署或已在 dev 驗證。第 8 點的通知顯示不屬 phase 0（見第 8 點待辦）。

### 11. AGENTS 界線 2 的措辭（Neal 已同意，本 PR 已改 AGENTS.md）

原文：「……VM 回收或換版時從 checkpoint 明確接續，不偷偷換新環境。」

改成：

> 每個 Agent 一個 Hosted session（一台 VM）……compute 可更換。**版本以該 session 實際執行的版本為準，相容就照跑；換到新版本只在助理閒置時做，事前暫停新回合、有工作在跑就延後，並記錄、通知，從對話歷史與 checkpoint 明確接續**；平台自行重建環境時也照實記錄與告知，不偷偷換新環境。

「只在閒置時換」的做法就是草案的閒置升級機制；它定稿上線前，換環境仍走現行 follow（第 7 點）。AGENTS 括號裡的「現行 follow」指第 7 點的換環境入口，包括來源碰不到時的棄用來源換新環境。為了不讓讀者把 AGENTS 這句當成已上線的行為，AGENTS.md 在上面那句最後（句號前）加了括號「（閒置升級機制上線前，換環境仍走現行 follow；見 ADR-0009 §7、§11）」（Neal 2026-10-13 核准把「定稿前」改成「上線前」），Neal 同意的句子本身不改。

「平台自行重建環境時也照實記錄與告知」在決定部分的兌現方式（2026-10-13 更正後）：dev 實際看過的平台自行變動是同一個 session 換了版本，靠漂移 CAS 同時設的綁定列通知待送位，由第一個主人本人的回應帶上換版通知、寫出後清除（第 4 點末段、第 8 點）；`get_session` 的 `created_at` 改變只是順便讀到的保底訊號，沒有保證會偵測到。對 AGENTS 界線 2 這句的意義：告知保證的是「主人本人至少看到一次」（網路上遺失的回應除外），不是每個入口、每個對話都告知；非主人入口與跨對話的補發屬草案（已知未解問題 8）。

## 遷移計畫（每一步一個小 PR，各附測試）

| Phase | 內容 | 主要測試（離線替身；真平台另驗） |
|---|---|---|
| **0（立即）** | #813（不再呼叫 selector 已不選的舊 SID；版本定位器取選定版本否則最高版本）＋第 10 點「follow 與舊對話只碰 `active`」（#815）；兩者都合併才算完成（#813 已合併 `8732d7b7`，#815 已合併 `01e0fdb2`；程式完成；dev 驗證項 ② 2026-10-16 FAIL，已改成 stop 之後不讀來源） | 舊 SID `idle` 時 follow 零呼叫、零 Files 讀取、走不搬檔（API 回條記 `files_migrated=false`）；FA 的 stop 失敗後被回收的來源不讀檔；`active` 時照舊搬檔 |
| 1 | **程式完成並已部署 stage 第 29 版（PR #818；只記錄）。** API 綁定列的認領、實際版本與漂移旗標、通知待送位（清除屬 phase 2）；細節見下面「phase 1 細節」 | 見「phase 1 細節」 |
| 2 | 工作台補回第 8 點的通知顯示並實作待送位清除；profile 換成 `foundry-native-v2` 與 schema 快照測試；Hosted 拒絕帶 `not_dispatched`；`replaced()` 改比 SID；profile 比對改比通道；v1→v2 遷移的三項必要範圍（第 7 點）。**拆成 2a～2d 四個 PR**（與 #824 一致）：2a 協定欄位（仍是 v1、使用者無感；`replaced()` 只放寬 `pending` 目標 SID，收緊的一半延到 phase 3；**已合併 #824，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15）**）、2b 第 7 點／第 7a 點的三項必要範圍（**已合併 #825，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15）**：另寫切換路徑、不寫 Hosted `pending`、棄用決定存在綁定列、驗收表 (a)～(s)，(r)(s) 照第 7a 點的做法；來源不相容但仍 `active` 只觀察與記 log，不拒絕）、2c 通知顯示與待送位清除（**已合併 #830，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15）**：通知槽加換環境通知、回應寫出後 CAS 清除、Teams 最終回覆帶通知、工作台狀態列顯示）、2d 換 profile（最後合併；**2d-i 程式完成、待 dev 測試**：P0～P9，部署開關 `FA_NATIVE_SESSION_GUARD_ENFORCE` 在 `stage.json` 寫 `"1"`（下一個 release 部署後才在 stage 打開），azd／dev 預設關，見 [2d-i 開關](../../src/fa-shared-context-poc/README.md#native-2di-switch)）。2a～2c 都是 v1 時代的一般部署。細節見下面「phase 2 細節」。**M1 已核准（Neal 2026-10-13），2d 拆成 2d-i（擴充）與 2d-ii（收斂），排在 phase 3 之後，2d-i 帶目的端切換並停用所有 follow 入口，見 M1** | 見「phase 2 細節」 |
| 3 | **已合併（PR #831；離線替身驗證，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15））。** 呼叫綁定列的 SID 前先確認 selector 仍選它建立時的版本、沒有棄用決定、metadata 是 `active`／`idle`，否則不呼叫、改跑 follow（第 10 點 phase 0 保證）；`replaced()` 收緊只對帶 `x-client-fa-locator: binding-row` 的呼叫，其他照 2a。 `HostedVersions.current` 的 `fa_build` 檢查只拿掉它在送出路徑上的作用（有綁定列時不經它），仍是 follow 目標的閘門，換成快照比對照 M1 留在 2d-i。送出流程改用綁定列與 V_rec（第 4 點），含漂移重送一次、核回條與「不一致」分支；`_same_session`（`behavior_native_turn_api.py` 257–261，#813 刻意保留 409）與 `NativeConversation.execution_hosted_session_id` 改以綁定列的目前 SID／之前的 SID 清單判斷；#813 的 selector 快取判斷改以綁定列為準。`replaced()` 收緊的一半（既不是目前 SID、也不是 `pending` 的目標 SID 才算被取代；2a 延後，第 2 點）在此一併做。**保留** `_follow_before_admission` 與 `follow_all`（停用屬草案 phase 4、6；M1 的 A 提前到 2d-i） | 醒來版本不同但相容→同 RID 只派送一次；409 無 `not_dispatched`→先讀綁定列再讀 RID；舊 SID 上非終態的 RID→不改送新 SID、明確失敗；409 且 V_obs＝V_rec→不記漂移、交給 follow；403 `NATIVE_TURNS_DISABLED`＋SID 沒變→不由 FA 拒絕（記 log、照現行回應，第 9 點「不拒絕」）；follow 換環境後既有對話在新 SID 接續 |
| 3b | **已合併（PR #829；閘門預設不打開，離線替身驗證，已隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15））。** 第 7 點的部署規則：CI 部署前相容閘門（v1→v2 遷移完成後接上）、綁定列引用的版本與 ACR image 不刪、不讓保留政策清掉。做法見 [deploy README〈部署前相容閘門〉](../../deploy/README.md#compat-gate)與[〈版本與 image 不刪〉](../../deploy/README.md#version-retention) | 支援集合缺被紀錄的 profile→部署拒絕；閘門只讀綁定列與 definition，不呼叫任何 session；綁定列引用的版本不在任何清除清單 |

**phase 1 細節（已實作）：**

- 內容：認領現有 SID（同時寫這個 SID 建立時綁的版本，第 2 點；不多一次寫入）；記錄觀察到的實際版本（不同於建立時綁的版本時同一次寫入設定漂移旗標與通知待送位，並記下通知的 vY；舊 image 要等 VM 接受 expected＝V_obs 才寫）；主人本人的回應帶上第 8 點換版通知。**清除屬 phase 2**，與工作台顯示一起實作，phase 1 每個主人回應都帶。
- `get_session` 的 `created_at` 只在既有流程本來就讀 `get_session` 時順便記下（第 2 點），不另加讀取、沒有保證的讀取點；「`created_at` 不同於紀錄時寫入」**未實作**（phase 1 沒有讀到不同 `created_at` 的讀取點）。
- 不改任何回合的結果。新增的 Table 寫入只有：每個 Agent 認領一次；觀察到不同版本時一次；每次換環境 commit 後一次（目前 SID、新 SID 建立時綁的版本＝commit 結果的 `hosted_agent_version`、清除漂移旗標與通知待送位、之前的 SID 清單，第 7a 點 (f)）；phase 2 起通知清除一次。送出路徑本身不多寫。
- 測試：讀列不呼叫 session；CAS 衝突重讀；漂移只產生紀錄；旗標與通知待送位同一次 CAS 設定，舊 image 在 VM 接受 expected＝V_obs 之前兩者都不寫；非主人呼叫者（routine、交辦、群組、HITL）不帶不清，主人回應（工作台兩個 submit 路徑與 Teams 主人回覆）帶上、通知是 vX→設定旗標時的版本（漂回後也是）；綁定列讀寫失敗→回合結果不變；認領在請求外設定、第一個主人回應帶上；旗標已設定後不再設待送位；漂移後漂回，旗標仍在；follow commit 後綁定列目前 SID＝新 SID、建立時綁的版本＝commit 結果的 `hosted_agent_version`、旗標與待送位清除、來源 SID 在之前的 SID 清單。

**phase 2 細節：**

- 工作台補回第 8 點的通知顯示（含不搬檔那句；2026-10-06 起不顯示，見第 8 點待辦），同時實作漂移通知待送位的清除（主人的工作台回應寫出後 CAS 清除；漂移只在工作台狀態欄位，Teams 不帶不清，第 4 點末段、M4 的 A 第 2 項）。
- 相容規則：profile 換成 `foundry-native-v2`，`HostedVersions.current` 與各處寫死的 `"foundry-native-v1"` 比較改集合判斷；schema 快照測試（wire、state store、SDK／runtime 固定版本、checkpoint codec 清單），v1 的快照測試在 2a（#824）加入；接線是 2d（M1 的 A：2d-i）部署前的閘門（M1 的 A「閘門清單」第 3 項），2026-10-15 改為部署時擋（CI job `v1-wire-snapshot` 加上 `deploy-staging` 部署前同一個測試，不用分支保護）。
- Hosted 在 dispatch 前的拒絕加 `not_dispatched`、`reason`、`actual_version`，`NativeSessionState` 加 `actual_version`；`replaced()` 改比 SID：2a 只放寬（`pending` 的目標 SID 不算被取代），收緊的一半延到 phase 3（第 2 點末）。
- state store 項目照第 3 點「沒標＝v1 wire」寫（帶 digest 的照送出時的 profile、`_Meta` 標 image 自己的），舊 image（v2 起）讀新 profile 項目明確拒絕；對話與回條的 profile 比對改比通道（第 3 點）。
- v1→v2 遷移的三項必要範圍（第 7 點）：來源 403 `NATIVE_TURNS_DISABLED` 當來源拒絕、有綁定列就算準備過、來源碰不到時棄用來源換新環境（第 7a 點，實作 PR 可另寫路徑並重核 `follow_session` 的等待原因）。
- 測試：
  - 通知：主人回應帶上通知並在寫出後清除，寫出前或清除前崩潰→下一個主人回應再帶，清除 CAS 衝突就重讀。
  - 相容：v1 一律不相容；schema 變但 profile 沒變→測試失敗；follow 進行中，`pending` 目標 SID 的 `_RECORD_READS` 照常讀、不 409；以 v2 送出的項目都帶 profile、以 v1 送出的位元組不變、同 RID 重送不變成 `NATIVE_RID_CONFLICT`，沒標的項目當 v1 wire 讀；**v1 與沒標的對話、`foundry-responses-v1` 對話在 v2 請求下照常接續**；較舊版本 session 相容時照跑；`execution_version` 不同的排隊／已接受輸入照舊拒絕。
  - 遷移：v1 來源回 403→寫 `source_invocation_refused`、不搬檔換到 v2；還沒有 v2 session、有綁定列→送出時照樣 follow、不呼叫任何 session；v1 來源 `active`→只重讀 metadata，不 invoke、不 stop、不切換、不拒絕，逾時只留一次專用 log。
  - **第 7a 點驗收表 (a)～(s) 全部列為測試**：含讀來源上的 RID→以之前的 SID 清單判斷、得到「版本已變、無法接續」的通知；新 session 上進行中的 RID 不得到這句；commit 後 CAS 前崩潰→重跑切換補寫；不相容 SID 在每個 API 入口零呼叫（強制模式）、觀察模式下只記 log 不拒絕；沒有已完成回合的對話在新 session 不帶 checkpoint 開始（(r)）；被之前 SID 占住的 Responses slot 被釋放（(s)）；相容來源暫時 404 不棄用；同一 SID 漂移（含漂移後漂回）的 `idle` SID 不卡換環境、從沒漂移過的新 SID 照現行等待。

\#813 涵蓋 phase 0 的「API 舊 reader 快取」與「版本定位器」兩項，屬**過渡做法**：它用 selector 判斷快取是否過期，版本定位器在選定版本沒有 session 時退回「版本號最大的」。G 之下版本號不單調（第 2 點），這個退路只在 phase 3 之前使用；phase 3 起定位一律查綁定列，selector 只當換環境的目標。A 節的世代上限 3、`pending_work` 立即失敗等規則不實作；替代做法（延後、否決）屬草案。

## 協調物件生命週期

只列本 ADR 用到的協調物件。依據只採 `origin/main` 程式或 Microsoft Learn，推論另註「推論」。2026-10-13 定稿輪把原本的「未定」格子全部填上；做法**先減少協調物件，再談怎麼收拾**：原草案的 API 派送意圖紀錄、API 租約與預留 SID、每個對話的通知標記都**不建立**，改由 Hosted 既有的紀錄承擔（理由見「閒置升級機制」的「設計前提」）。新增的只有：綁定列的 `idle_upgrade` 子物件、Hosted `fa/v1/turns` 裡的開放 RID 集合（`open-rids` 項目）、放在另一個帶期限 store 的 Hosted 標記（`deferred:`／`crashes:`／`refused:` 與切換進度）、Blob 快照與專案層否決列；決定部分另有 phase 2b 的 `stall:` 進度觀察紀錄（#823 審查第 5 輪）。「決定」部分的第一列與「漂移通知待送位」列的**決定內容**不變，只在格子裡加註閒置升級上線後改走哪裡（M3、M4，Neal 2026-10-13 核准）；Hosted `pending` 的現行用法也不變，定稿只加切換用法。

本表與後面 phase 4～6 的各列都依 Neal 2026-10-13 核准的修訂（M1（含 M2 不拒絕）、M3～M10 採 A，見「對已確認部分與 Neal 決定的修訂提案」）。

| 物件 | 所在 | 誰建立 | 誰結束 | 何時過期 | API 崩潰後誰收拾 | SID 是 `idle` 或 404 時 |
|---|---|---|---|---|---|---|
| 綁定列的位置與版本欄位（第 2 點） | 決定 | API：phase 1 認領（Table Insert Entity，已存在時回 409 就讀回，不覆寫）；之後只以 CAS 更新（含每次換環境 commit 後寫目前 SID、新 SID 建立時綁的版本、清除漂移旗標與通知待送位、之前的 SID 清單；phase 2b 起另含 `abandon` 欄位：第 7a 點 (g) 寫下棄用來源的決定，切換寫進綁定列時清掉）。同一 SID 漂移旗標只在兩處設定：認領時讀到的版本不同於建立時綁的版本，或第 4 點第 6 步的漂移 CAS（與 V_obs 同一次寫入）；設定是冪等的，已設定再寫仍是設定；從未設定變成設定的那次寫入同時設通知待送位（見第三列） | 不結束；FA 不刪（界線 3 不提供刪除） | 不過期 | 每次寫入都是單列 CAS，寫成或沒寫，沒有半套狀態。漂移 CAS 之前崩潰：V_rec 與漂移旗標都沒變，下一次送出重新觀察、再寫同一個 CAS。follow 已 commit、綁定列沒跟上時：下一次送出時的 follow 或 `follow_all` 以同一來源重跑切換，拿到已 `ready` 的同一結果（`native_environment.py` 587–592）後補寫（第 7a 點）；V_rec 相容時，送出也可能先走第 4 點第 6 步「不一致」分支，同樣交給送出時 follow 收斂（閒置升級上線後改走 U8 的補寫，見M3） | 讀列不呼叫 session；`idle` 時不寫、不叫醒；404 只在送出時得知，記理由、不拒絕（第 9 點「不拒絕」），不刪列 |
| Hosted `agent` 紀錄的 `environment.state="pending"` | 現行 follow 的用法：決定部分原樣沿用；切換用法（U6）：閒置升級。第 7a 點的棄用來源換新環境（phase 2b，#825）**不寫** `pending`：新 session 以 `session_abandon` 一次 CAS 直接寫成 `ready`，之前留下的 `pending`（例如等不到來源的現行 follow）同時被取代；棄用的決定本身存在綁定列（`abandon` 欄位，(g)），換完清掉。M1 第 2 步的 2d-i 背景切換只在來源有工作檔時寫 H0 形狀的 `pending`（搬檔柵欄，最多約 120 秒，與現行 follow 相同；目的 SID 先記進綁定列 `upgrade`）；來源沒有工作檔時**不寫**，安靜時以 If-Match＝E0 直接 commit 成 `ready`（Neal 2026-10-15，M1 第 2 步「工作檔」）。所以 2d-i 期間會遇到的 `pending` 是搬檔柵欄與舊式 follow 留下的兩種 | Hosted 的**目的 session**（不 invoke 舊 SID）；細節見表後「`pending` 切換用法」 | 現行用法：commit 成 `ready`（743–745）或 retarget；M1 的 A 停用 follow 入口後，遺留的舊式 `pending` 由背景工作或請求內等候撤回，只有已到 commit 這一步才接手（M1 第 2 步）。切換用法：commit（U7）或撤回 | 現行用法不過期；切換用法：`cut_deadline`（寫入後 5 秒） | API 主動撤回（必要）；新 image 的 session 也會撤回過期的 | 見表後 |
| 漂移通知待送位（綁定列欄位，第 2、4、8 點） | 決定 | API：與同一 SID 漂移旗標同一次 CAS 設定（第 4 點第 6 步的漂移 CAS；認領時讀到不同版本則與認領同一次寫入），同一次寫入記下這時觀察到的版本作為通知的 vY，之後紀錄的實際版本再變也不改；舊 image 要等 VM 接受 expected＝V_obs 才寫 | API（phase 2 起，與顯示一起實作；phase 1 不清，每個主人回應都帶）：第一個主人本人的工作台回應帶上通知（只顯示在工作台狀態欄位，M4 的 A 第 2 項；Teams 主人回覆不帶不清）、寫出後以 CAS 清除；非主人呼叫者（routine、Agent 間交辦、群組、HITL 提醒）不清 | 不過期；SID 換掉時隨漂移旗標一起清除 | 位元留著，下一個主人回應再顯示（至少一次）；清除遺失後可能重複顯示一次，接受；回應寫出後在網路上遺失則漏掉 | 不呼叫 session；`idle` 時不寫、不叫醒；404 不受影響（位元留著，SID 換掉時清除）。M4 把它一般化成每個 Agent 一個「通知槽」，phase 2c（#830）起同一個槽另記換環境通知（`switch_notice`，第 2 點），交付與清除照本列，另外也由 Teams 主人的最終回覆帶上、寫出後清除；「已退回」等草案通知仍待閒置升級 |
| 派送意圖紀錄 | **不建立**（M6，Neal 2026-10-13 核准，取代 Neal 決定 7 的做法）。它要解決的「有沒有尚無終態回條的 RID」，Hosted 已經持久記著：每個新 RID 接單先以 CAS 把 `admitting` 寫進 `agent` 紀錄（`native_turns.py` 680 起），回條建好後 `_settle_admission`（703 起）以 CAS 清除；`rid:<RID>` 回條在 Agent 共用的 `fa/v1/turns`，任何 session（含新的目的 session）都讀得到 | — | — | — | — | — |
| 綁定列的 `idle_upgrade` 子物件（目標版本 `target_version`、延後起點 `deferred_since`、最近一次嘗試時間 `last_attempt_at`、同一目標的健康檢查失敗次數 `health_failures`、本 Agent 否決的版本 `vetoed_versions`、最近一次切換時間 `switched_at` 與切換後的啟動失敗 `start_failures`；嘗試序號在 M5 的 `fresh`。不叫 `upgrade`：那是 2d-i 的搬檔柵欄 `NativeBindingUpgrade`） | 閒置升級 | API：U0 以單列 CAS 寫入（4b-1 已實作，`record_due`：只在 `FA_NATIVE_IDLE_UPGRADE` 開時寫；比 4b-1 舊的 API 以 `extra="forbid"` 解不開帶它的列，回 409 `NATIVE_BINDING_INVALID`，同 `clocks` 的規則，開過之後 API 不退回 4b-1 以下）；每次要建立新的目的 session 前先 CAS 把嘗試序號加一（先預留再建立，序號進新 SID 的雜湊，U2） | API：U8 的 `record_switch` 同一次寫入清掉目標、序號與延後起點，保留否決版本，記下切換時間（crash-loop 觀察用，下一次切換時換掉）；selector 換到別的目標時 U0 重設 | 不過期，**沒有租約**：每一步都可重入（固定 SID 的建立同 ID 只成功一次、沒收到確認就讀回；Hosted 的 `following:` 進度與 `agent` 紀錄都走 ETag），所以不需要「誰持有」 | 不需要收拾：下一次觸發（任一 API 副本）從綁定列與 Hosted 進度接著做；崩潰前已加一的序號只是跳過一個 SID，可能留下一個沒用的目的 session，平台閒置逾時後回收、30 天無活動刪除（FA 不刪） | 讀寫都不呼叫 session。舊 SID `idle`：相容就不升級（Neal 決定 5），不相容走第 7a 點。目的 SID `idle`／404 或醒來版本不是目標：放棄這個目的 SID、序號加一重建，不算健康檢查失敗 |
| Blob 工作檔快照（②） | 閒置升級（phase 5） | API，U1：寫進**快照專用儲存帳號**（Neal 2026-10-17，與 `no-auto-gc` 備份帳號分開；API 設定 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`）的 container 下 `fa-upgrade-snapshots/<tenant>/<team>/<agent>/<upgrade_id>/`（tenant 與 Team 同時在路徑與每個 blob 的 metadata，沿用 `m3/v1/{tenant}/{team}/`；讀回時兩者都要相符）；所有檔案寫完並核過 hash 後，**最後**才寫 `manifest.json`（發布標記） | FA 程式不刪。以 Blob lifecycle management 規則（前綴篩選＋`daysAfterCreationGreaterThan: 30` 的刪除動作，由平台定期執行；[官方](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview)）在 30 天後刪除；規則只放在快照專用帳號上（`infra/shared-context/upgrade-snapshots.bicep`），**不放在**備份帳號。建立帳號、規則與 API 的角色指派是雲端與 IAM 寫入，**每次部署都要 Neal 批准**（含 dev，見 phase 5c）；批准前不建帳號，phase 5 保持關閉 | 30 天（Neal 決定 2）；官方說規則生效最多要 24 小時、之後定期執行，所以實際刪除可能晚一天以上 | 沒有 `manifest.json` 的前綴＝不完整：永遠不還原、不當成功；同一條 30 天規則清掉。重試換新的 `upgrade_id` 前綴，不續寫半套 | 舊 SID 在途中變 `idle`／404：立刻停、不寫 manifest、改走 ④（第 6 點） |
| 「每個對話通知一次」的標記 | **不建立**（M4）：通知改成每個 Agent 一個通知槽，由第一個主人本人的回應帶上、寫出後清除，規則同上面的漂移通知待送位 | — | — | — | — | — |
| 否決 | 閒置升級 | 兩種，都由 API 寫：(a) **本 Agent**：綁定列 `idle_upgrade` 的否決版本清單（`vetoed_versions`），同一目標健康檢查累計 3 次失敗時寫（失敗可能只跟這個 Agent 有關，所以不擴到別人）；(b) **專案層**：Context Table 獨立分割區 `native-version-veto`，每個（Hosted agent 名稱, 版本）一列，crash-loop 判定時以 Insert 寫，已存在回 409 就讀回（4b-1 已有 store 與讀取：`native_version_veto.py`，只有 API 用；寫入者是 4d） | 不解除、不刪：否決只擋**那一個具體版本**，selector 換到更新的版本就是新目標，自然不受影響（取代原「發布更新的版本時解除」的寫入） | 不過期 | 單列寫入，寫成或沒寫；沒寫成時下一次判定再寫 | 不呼叫 session，不受影響 |
| Hosted `fa/v1/turns` 的開放 RID 集合（`open-rids` 項目，不在 `agent` 紀錄；每個非終態 RID 一筆，只記 RID；「已補齊」標記 `complete_for`／`complete_since`） | 閒置升級（phase 4；**開 `FA_NATIVE_IDLE_UPGRADE` 前必須有**）；已先實作（審計第 8 項，2026-10-14） | Hosted：新 RID 接單時與 `rid:` 建立並行加入（對接單時並行讀到的集合做一次 CAS），失敗就在 `_settle_admission` 清 `admitting` 前再加，再失敗就不清 `admitting`。開始執行不寫集合（設計前提 (a) 只 CAS `agent` 紀錄）。從沒有集合的 image 換過來的第一次 follow 對既有回條全掃描補齊，commit 後記「已補齊」 | Hosted：回條寫成終態後在背景以 CAS 移除（不在 API 送出路徑，也不延後同一 chain 的下一個回合） | 不過期；筆數受同一 Agent 的排隊上限約束 | 與 API 無關。移除失敗或在終態與移除之間崩潰：下一次掃描讀到該 RID 已終態就移除 | 不呼叫 session。做法見第 5 點「開放 RID 集合的現行實作」 |
| Hosted 標記 store（新的、與 `fa/v1/turns` 分開的 State store，項目帶到期時間）：`deferred:<run_token>`、`crashes:<SID>`、`refused:<RID>`、切換進度 | 閒置升級（phase 4，新 image） | Hosted：`deferred:` 在進行中的回合呼叫 `exit_for_recovery()` 之前寫一次（`native_turns.py` 1594、1610、1632 等處）；`crashes:` 在 recovered 重入卻沒有 `deferred:`、或 checkpoint／`prepare` 載入失敗時以 CAS 附加（最多 8 筆）；`refused:` 在依 `execution_version` 拒絕執行時（463、1595）寫一次，回條不動（Neal 決定 8）；切換進度由 `NativeSessionCut` 寫：2d-i（#856）先寫進目的 session 在 `fa/v1/turns` 的 `following:`（搬檔模式時 H0 的搬檔階段要讀這筆），phase 4d 起才改寫進標記 store | 不另外結束 | `deferred:` 與 `refused:` 30 天（與平台 session 30 天刪除一致）；`crashes:` 最後一筆後 1 天（判定窗只有 30 分鐘）；切換進度最後更新後 7 天（含被放棄的嘗試）。平台 store 沒有原生到期時，Hosted 每次寫這個 store 時順便刪掉已過期的項目（Hosted 自己的內部項目，不是 API 薄轉送的 Team 資料） | 與 API 無關；API 只經 `NativeSessionState.recent_crashes` 與讀取投影唯讀 | 只在有執行時寫；API 只在 SID `active` 時讀，不為它叫醒 |
| 留下回條的進度觀察紀錄 `stall:<sha256(RID)>`（看到的回條 ETag、進度快照 ETag 或「沒有快照」、第一次看到的 Hosted 時間） | 決定部分（phase 2b，第 7a 點「(r)(s) 的競爭」；#823 審查第 5 輪新增） | Hosted 的新 session：同一對話排隊的回合第一次看到之前 SID 留下的 `started` 回條時，寫進 Agent 共用的 `fa/v1/turns`（與 `progress:`、`preview:` 同一個 store）；兩個 ETag 任一個變了就以 CAS 重寫、重新計時 | 不另外結束：留下的回條到終態（來源寫完，或 N 分鐘沒變後由新 session 寫成 `unknown`）後就不再讀它 | 不過期；FA 不刪（同 `progress:`／`preview:`） | 與 API 無關；新 session 的 VM 重啟後讀回接著算，不重算 | 只由目前 SID 在執行回合時寫；不呼叫、不叫醒來源 |
| 切換時已退役的來源標記 `retired:<sha256(SID)>`（SID、API 決定時看到的 `source_status`） | 決定部分（phase 2b，#825 實作「(r)(s) 的競爭」的「來源已退役」） | Hosted 的新 session：`session_abandon` 帶的 `source_status` 是 `failed`／`deleted`／`expired`／404 時，commit 前以不可變寫入放進 `fa/v1/turns` | 不另外結束 | 不過期；FA 不刪 | 與 API 無關；重跑切換寫同一個值 | 不呼叫、不叫醒來源；只在新 session 看到那個 SID 留下的 `started` 回條時讀 |
| 現行 follow 留在 `fa/v1/turns` 的 `following:` 進度（含 phase 4 前被放棄的 follow） | 決定部分（現行） | Hosted（現行 follow） | 不結束 | 不過期 | 不需要 | 不呼叫 session。phase 6 停用 `follow_all` 後不再新增；既有的保持原樣（掃描成本由開放 RID 集合避開，不必清） |

**`pending` 切換用法（上表第二列的細節）：**

- **誰寫：** Hosted 的目的 session，經 `NativeSessionCut` 寫進 Agent 共用的 `fa/v1/turns`（現行 follow 的寫入點是 `native_environment.py` 575、653、679）；寫入不 invoke 舊 SID。以 U5 開始時讀到的 `agent` 紀錄 ETag 為 If-Match，另帶 `cut_deadline`。
- **期限：** `cut_deadline`＝寫入後 5 秒（Hosted 的時鐘），**必須短於最短的請求內等候**（8 秒，見「切換期間送出的訊息」），所以碰到柵欄的訊息一定在自己的等候內等到 commit 或撤回。安全靠 CAS 不靠時鐘：撤回與 commit 都是對同一 ETag 的寫入，只有一個寫得成；時鐘偏差只讓撤回早一點或晚一點。
- **撤回（三條路）：** ① **API 主動撤回（必要）：** 跑切換的背景工作，或在請求內等候中看到 503 超過 `cut_deadline` 的送出請求，都對目的 session 送 `NativeSessionWithdraw`（以 `pending` 的 ETag 為 If-Match；已 commit 或已撤回就原樣回傳）。第一次升級到新 image 時舊 SID 跑的是前一版 image，它的 670–672 只會回 503、不會看期限，這是唯一的修復路，所以必須有。② 目的 session 自己在指令逾時撤回。③ 新 image 的任一 session 在接單檢查看到已過期的切換 `pending` 時以 CAS 撤回再接單（從第二個新 image 版本起，舊 SID 那一側才有這條）。
- **撤回寫回什麼（總協調 2026-10-15）：** `NativeSessionWithdraw` 由 H2d（或之後）的 session 執行（H0 沒有這個指令）：寫 `pending` 前存過 `environment-before:<rid>` 的，以 CAS 還原它；舊式 follow 留下、沒有存的 `pending`，由 `pending.previous_*` 組出 `ready` 的環境（現行 `pending` 寫入時已蓋掉之前的 `ready`，沒有別的可還原）。已 commit 就原樣回傳 commit 結果。組出的 `ready` 以這次撤回的 `request_id` 為自己的 `request_id`，而開放 RID 集合的「已補齊」標記綁的是當初讓來源成為目前 SID 的那個 request，兩者對不上，所以下一次切換會做一次完整掃描（只多一次，不靠猜測信任舊標記）；`pending.previous_hosted_session_id` 不是目前 SID 時拒絕（409），不組。
- **撤回後不會再 commit：** 被撤回的 `pending` 之後不會被 commit 蓋過去：follow 的 commit 讀到 `pending` 已不是自己的（被撤回、來源又接了新工作）就回 409；撤回發生在這次讀取之後的，由 commit 的 CAS 擋下。
- **SID 是 `idle` 或 404 時：** 目的 SID 變 `idle`／404→API 撤回（①），不需要它醒著（撤回寫的是共用 store，但指令要由某個 session 執行：目的 SID 不可用時改由 API 對舊 SID 送撤回指令，僅限舊 SID `active` 且是新 image；兩者都不行時，`pending` 留著直到下一次切換嘗試或下一個送出把它做完或撤回，期間送出照「切換期間送出的訊息」的退路）。舊 SID `idle`：沒有人送就沒人受影響；下一個主人送出照常叫醒它（正常工作，不是第 10 點禁止的「為了檢查叫醒」）。

## 閒置升級機制（設計定稿，Neal 2026-10-13 要求；M 項已核准；尚未實作）

**狀態：** 2026-10-13 設計定稿（Neal 同日要求「開始處理，解決未解的問題，設計定稿」），#823 審查第 1～3 輪同日修正。原「已知未解問題」1～9 與上表的「未定」格子都已有解法（本節末「原已知未解問題的解法」）。本節有幾處改了已確認的決定或 Neal 的決定，集中列在「對已確認部分與 Neal 決定的修訂提案」，**這些 M 項 Neal 2026-10-13 已核准**（M1（含 M2 不拒絕）、M3～M10 採 A）；其餘機制細節沒有逐項另經 Neal 核准，phase 4～6 的實作 PR 照常審查。尚未實作；上線前換環境照第 7 點。

**最高原則（Neal 2026-10-13，適用 G 的所有 phase）：**「不要影響使用者體驗，轉換過程與體驗必須絲滑，使用者近似無感」。本節每個選擇先看使用者感受：不出現拒絕窗口、不丟脈絡、不讓人看到等待、不丟工作、通知只留 AGENTS 界線 2 要求的照實告知、送出路徑的延遲不變。

### 設計前提：Hosted 是唯一的柵欄

讀 `origin/main` `a9f39149` 的程式得到三個事實，以及一個缺口（審查第 1 輪指出）：

1. 同一個 Agent 的所有 session 共用一份 State store `fa/v1/turns`，`agent` 紀錄是**新 RID** 接單的單點：新 RID 接單先以 CAS（If-Match）把 `admitting` 寫進 `agent` 紀錄（`native_turns.py` `_original_session` 649 起，寫入在 680 起），回條建好後 `_settle_admission`（703 起）再以 CAS 清除。所以只要 `agent` 紀錄的 ETag 沒變，這段時間就沒有**新 RID** 被接單。
2. 換環境的掃描與 commit 由**目的 session** 執行：`follow_session`（`native_environment.py` 582 起）讀共用 store 的回條（`_drain_pages` 288、`_scan_page` 325）與 chain task（`_tasks_pending` 435）；寫 `pending`（653、679）與 commit（743–745）都是寫共用的 `agent` 紀錄，**不 invoke 舊 SID**。
3. `pending` 讓這個 Agent 每個 session 的**新 RID** 都回 503 `AGENT_VERSION_FOLLOWING`（`native_turns.py` 670–672）；commit 之後，舊 SID 的接單與讀取回 409（673–676、`replaced()`）。

**新 RID 接單的入口（審查第 2 輪補列）：** 下列入口在 `rid:<RID>` 建立前都經 `_submit_locked`→`_admission_checks`（1019）→`_original_session(admitting=True)`（1036），所以都受事實 1、3 約束；4a 的「掃描中有新 RID 接單」測試每一種各一個：

| 入口 | Hosted 位置 | 誰送 |
|---|---|---|
| 主人送出（工作台、Teams 主人回覆） | `submit`（`native_turns.py` 974 起） | API `NativeTurnGateway.submit` |
| Agent 間交辦 | `receive`（978） | API `native_agent_api.py` 280／382／448 |
| routine | `receive_routine`（984） | API `native_routines.py` 598 |
| 群組開場 | `submit`，Responses profile（之後另有 Responses 第二步） | API `native_group_api.py` 94 經 `gateway.submit` |
| 中斷回合的續接 | `continue_turn`（994） | Hosted `dispatch_continuation`（`native_continuations.py` 393）經 `on_continuation`→API `continue_original`（`native_agent_api.py` 342）→目前 SID |

**缺口：已是 `accepted` 的回條開始執行時，既不寫 `agent` 紀錄、也不看 `pending`。** 670–672 的拒收只在 `receipt is None` 時生效。現行有四條路會開始執行已接單的回條：

| 路徑 | 位置 |
|---|---|
| 同 RID 重送到已是 `accepted` 的 RID：`_submit_locked` 直接 `chain.start` | `native_turns.py` 1098–1109 |
| chain 排隊輸入輪到執行 | `_enqueue_attempt`，480–484 |
| session 啟動時接回自己擁有的輸入 | `recover_owned`，569 起（586 `chain.start`） |
| Responses 第二步：接單之後 API 另外 POST 同一 RID；Hosted 的 Responses 處理器只以 `_require_original_session(receipt=…)` 檢查 | API：`behavior_native_responses_api.py`：工作台 `/submit` 在回覆主人後經 `_send_after_reply`（147，由 211 延後執行）；Teams 主人回覆（`native_teams_relay.py` 166 經 `gateway.submit`）同步走 `_send_response`（91）。Hosted：`native_responses.py` 155 |

所以「ETag 沒變」不保證「舊 SID 沒有開始執行任何回合」：掃描只要放過一個 `accepted` 回條（孤兒規則、退回時的放寬），這四條路就可能在掃描到 commit 之間在舊 SID 上開始它。定稿的修正（新 image，phase 4a 驗收）：

- **(a) 四條路都經過柵欄。** 開始執行前對 `agent` 紀錄做一次 CAS（2d-ii 之後寫一個開始計數欄位；欄位本身與 M7 的其他保留欄位一起在 H2d 的 v2 快照裡定好，總協調 2026-10-15）：檢查 `pending`，並讓 `agent` 紀錄的 ETag 前進，所以切換的 E0 比對看得到這次開始；開放 RID 集合在開始時不寫（2026-10-14 修訂，見第 5 點）。看到**切換用法**的 `pending`（帶 `cut_deadline`）就不寫、不開始，回 503 `AGENT_VERSION_FOLLOWING`＋`not_dispatched`。只認切換用法：現行 follow 的 `pending` 本來就要等已開始的回條結束，若也擋開始，follow 會自己卡死。被擋之後：同 RID 重送→照「切換期間送出的訊息」的請求內等候；`_enqueue_attempt`→照現行的 task 重試，切換若撤回就照常執行；`recover_owned`→留 `_recovery_error`，這個 SID 的新工作照現行回 503（668–669），切換撤回後重跑 `recover_owned`（要有測試）；Responses 處理器→RID 保持 `accepted`／`needs_response`，工作台既有的 reconcile 以同 RID 再 POST，切換撤回後在原 SID 執行。commit 之後四條路照現行在 `_require_original_session` 回 409。
- **(b) 孤兒規則收窄**（原問題 1）：只有同時符合下列全部的 `accepted` 回條才當孤兒、不擋切換：`rid:` 回條本身的 `run_token` 是空的（從沒開始）；不是 Responses 的 `needs_response`／`not_taken`（這兩種會被 API 第二步或 reconcile 再 POST）；`_tasks_pending` 看不到它的 owned／排隊輸入；不是 `_defer_enqueue` 留下的 RID；`agent` 紀錄的 ETag 在相隔 ≥60 秒的兩次讀取之間沒有變。
  - **`_defer_enqueue` 的 RID 不算孤兒（審查第 2 輪補）：** 交辦與 routine 在 steering 佇列滿時（`native_turns.py` 1115–1118）回條留在 `accepted`、`run_token` 空，由另一個 enqueue task（task ID 由 Agent 與 RID 算出，514 起）稍後排進 chain。`_tasks_pending` 只看 chain task（`native_environment.py` 443），看不到它。所以帶 `source` 或 `routine` 的 `accepted` 回條，要讀到它的 enqueue task 已終態或不存在才可能算孤兒；讀不到就算忙碌。
  - 孤兒照第 7a 點 (iii) 處理（回條原樣，換完讀到時回 (e) 通知），或在 M9 核准後由新 SID 接手。
- 舊 image 沒有 (a)：切換來源是舊 image 時（第一次升級到新 image，以及 M1 的 2d-i 期間），掃描一律用嚴格規則——舊 SID 上任何 `accepted`／`started` 回條都算忙碌，不用孤兒規則。**更正（總協調 2026-10-15）：** 原文「也不用開放 RID 集合（舊 image 不維護它）」不對：stage 第 30 版（H0）起已維護集合（#836）。嚴格規則不變；掃描範圍照集合的補齊標記：來源的 `complete_for` 等於來源 SID 時只讀集合列出的回條，否則全掃描（第 5 點「補齊與信任範圍」）。

有了 (a)，API **不需要**自己的派送意圖紀錄、租約或升級鎖：「接單」「開始執行」與「切換」都是對同一個 `agent` 紀錄的 CAS，Hosted 已經把它們排出先後。API 只留**提示**（什麼時候試）與**記錄**（綁定列）；「現在是不是閒置」「能不能切」一律由目的 session 在 CAS 之下決定。滾動部署時還不讀綁定列的舊 API 副本也一樣被擋：它們只能經 Hosted 派送，而 Hosted 看的是同一個 `agent` 紀錄。

現行 follow 的使用者問題也出在這裡：它在**開始等待時**就寫 `pending`（653），一直持有到掃描、搬檔、stop 全部做完；這整段期間新訊息都被拒，工作台顯示「助理正在換到新版本，訊息已保留，完成後會自動送出」，每 3 秒重送、10 分鐘後放棄（`web_test_console/index.html` 774、1049–1052、7480 起）。定稿把**準備**（建新 session、健康檢查、存與還原工作檔）和**切換**分開：準備全部在**沒有柵欄**時背景做，主人照常聊天；切換只是一段很短的 CAS 區段。

### 5. 閒置時升級（Grok 換 bundle 的 Foundry 版）

**閒置的定義（Hosted 判斷，不由 API 判斷）：**

- 目的 session 讀共用 store：舊 SID 上沒有非終態回條（`accepted`／`started`；孤兒照設計前提 (b)）、`agent` 紀錄沒有 `admitting`、chain task 安靜（沒有 `in_progress`、lease 或 steering 輸入），而且**從這次嘗試開始讀 `agent` 紀錄（ETag E0）到寫入 `pending` 為止，ETag 沒有變**。
- **掃描範圍：只看開放 RID 集合列出的 RID（必要，開旗標前要有）。** 現行 follow 的 `_drain_pages` 逐頁讀 `fa/v1/turns` 的所有項目（每頁 100 個、每次呼叫約 10 秒，`native_environment.py` 18–20），成本隨歷史回條無限增加，跨多次呼叫的掃描期間只要有人接單就 `busy`，忙碌的 Agent 會一直切不成。所以由 Hosted 在 `fa/v1/turns` 的 `open-rids` 項目維護開放 RID 集合（生命週期表：接單時加入、終態時移除），探查只讀這個集合列出的回條（狀態與 `run_token` 讀回條本身）與 `_tasks_pending`，成本只跟同時進行中的工作量有關。phase 4 的 dev 驗收仍量探查時間（門檻例如 p95 ≤ 1 秒）。例外：切換來源是舊 image 時用嚴格規則（設計前提末段）；集合的 `complete_for` 不等於來源 SID 時全掃描（2026-10-15 更正：H0 起舊 image 也維護集合）。
- **開放 RID 集合的現行實作（審計第 8 項，Neal 2026-10-14 核准，先於 phase 4）：** 現行 follow 每次部署都把 `fa/v1/turns` 的每張回條讀兩次（`_drain_pages` 與確認時再一次），回條永不刪除（CTX-03），期間 `pending` 讓新工作一律 503，所以每次部署的停擺時間隨歷史無限變長。這一步先把集合做出來、讓現行 follow 用上，phase 4 直接沿用：
  - **修訂（總協調 2026-10-14，#836）：** 另一個項目 `open-rids` 是集合的最終形狀，2d-ii 之後也留在原處、不放進 `agent` 紀錄（免得重做）；設計前提 (a) 的開始執行柵欄只對 `agent` 紀錄做一次 CAS，開始時不寫集合，所以集合只記 RID。
  - **放哪裡：** `fa/v1/turns` 的另一個項目 `open-rids`（`native_open_rids.py`），**不放 `agent` 紀錄**：M1 要求 `agent` 紀錄在 2d-ii 收斂前維持 H0 讀得懂的形狀（舊 image 嚴格解碼，加欄位會讓它在回 503／409 之前就解碼失敗，也讓退版的 image 讀不了）。舊 image 只處理 `rid:` 開頭的鍵，這個項目它看不到也不會改。
  - **欄位：** 每個非終態 RID 一筆，只記 RID（排序、不重複；狀態與 `run_token` 讀回條本身）；`complete_for`＝「已補齊」標記，**以 SID 與那一次持有為範圍**：`complete_since` 記讓這個 SID 成為目前 SID 的那次 follow（環境的 request ID）；`responses`＝曾接過 Responses 回條（黏著，讓只讀集合的掃描照舊把 `include_responses` 交給 `_tasks_pending`）。
  - **加入：** 新 RID 接單時，集合與 `agent` 紀錄、`rid:`、對話 meta 在同一批並行讀取裡讀到；加入是對讀到的 ETag 做一次 CAS，和 `rid:` 建立並行，所以送出路徑一般不多來回（有衝突或結果不明時才改回讀＋CAS）。沒成功的話，`_settle_admission` 清 `admitting` 前再加一次；再失敗就不清 `admitting`，送出回 503，回條已建好但還沒執行；用戶端照常以同 RID 重送，重送的 settle 補加、清除 `admitting`，回合才執行一次。follow 不替它 settle（現行 follow 是 `idle_only`／`files_required`，只等 `admitting` 清掉，期間回 `admission_pending`）。所以開著的回條不是在集合裡，就是還掛在 `admitting`（follow 先看到 `admission_pending`）。目的 session 替來源 settle 的接單也照這條加入。
  - **移除：** 回條寫成終態之後（`_cas`）在背景以 CAS 移除，不延後同一 chain 的下一個回合；VM 關機時在短時限內等它們做完。失敗、崩潰或被截斷只留下多餘的一筆，下一次掃描讀到它已終態就移除，不會藏住開著的回條。
  - **補齊與信任範圍：** follow 只有在 `complete_for` 等於這次的來源 SID 時才只讀集合；否則照舊全掃描（第一次就是補齊）。全掃描略過的 `settled`（之前的 SID）開著的回條會加進集合。commit 成功後才寫 `complete_for`＝新 SID、`complete_since`＝這次 follow（中途崩潰就是下一次再全掃描）。只有目前的 SID 能接新 RID，而它的 image 每一筆都會加入，所以標記在那一次持有期間成立。沒有完整集合的來源（標記對不上 SID，例如換版或退版之後），下一次 follow 自動全掃描再補齊一次。棄用來源換新環境不經掃描，也**不讀寫集合**（集合的儲存錯誤不會讓它失敗）：同一個 SID 若之後經棄用再拿回 Agent（綁定列已不列為之前 SID 的第 0 代 SID），它的標記還指著上一次持有；follow 開始時（`agent` 紀錄還是來源自己的環境）比對 `complete_since` 與來源環境的 request ID，不同就先清掉標記再全掃描。省略檔案的檢查（`require_complete`）照設計要讀每張已完成回條的 checkpoint，仍全掃描。
  - **成本：** 補齊之後每次 follow 是讀一次 `open-rids`，加上每次掃描讀集合裡的開著的回條（通常是 0～幾張），與歷史回條數無關；兩次掃描都是這個成本，所以不另外快取第一次的結果。仍要付全掃描的情況：每個 Agent 第一次從沒有集合的 image 換到這個 image 時（一次），以及上面那幾種標記對不上的情況。
  - **和閒置升級閘門的關係：** 這是第 5 點「掃描範圍」與「閘門清單」第 8 項的前半；4a 仍要讓四條開始執行的路各對 `agent` 紀錄做一次 CAS（設計前提 (a)，不寫集合），加上孤兒規則與 4a 的計數測試，之後才能開 `FA_NATIVE_IDLE_UPGRADE`。
- **HITL（閘門 6 盤點，2026-10-15 核對 `origin/main` `fb8941f2`）：** 卡片只有一種形態：回合停著等主人，**續接只在原 SID 的 VM 記憶體**（SDK 回呼裡的等待迴圈，`native_hitl_runtime.py` 的 `_allowed`／`_owner_reply`）；沒有任何路徑在別的 session 重建這個等待。原稿的第二種形態「回合已結束、只留下等主人回覆的卡片，回覆是新回合」現行程式不存在：回合一般結束時 `finish_turn` 一定寫 `/end`，之後主人再回覆一律 409 `NATIVE_HITL_TURN_CLOSED`。#807 的「先不用」現在是 API 的持久決定（`consent-declined` 標記，同一對話之後的同一工具箱不再出卡），不是續接路徑。所以每種卡片都算「不閒置」，而且已經由第一條的回條掃描擋住（卡片開著時回條是 `started`），不需要另一個標記：
  - 批准卡：工具（shell／desktop／agent_call／FA provider MCP、toolbox `require_approval=always`）、SDK 直連 MCP、交辦（`task`）、routine 提案（`propose_routine`）；
  - 提問卡（SDK `ask_user`）；
  - Connect 卡（`connect_required`）。
  - 不是卡片、不擋升級（沒有回合在等）：skill 草稿審查、Teams relay 的回覆待送、接管（立即拒絕）。
  - **代價：** 互動式卡片的等待上限是 86400 秒（背景 600 秒），所以一張沒人處理的互動式卡片最久可以讓這個 Agent 24 小時都不閒置、不升級；這是照實的等待，不另訂較短的上限。這 24 小時只在沒有切換時成立：一旦換環境，來源 `idle` 上停著等卡片的回合照停滯規則，N=600 秒沒有變化就被標記 `unknown`、卡片照下一項收掉。
  - **被留下的回合要收卡（本閘門的實作 PR 補上）：** 回合被 recovery 交給接續回合、或換環境後留在之前的 SID 上不可能再完成時，它的等待已經不在了；之前提問與 Connect 卡（交接）以及所有種類（棄用來源）會一直開到期限，按「允許」不會有任何效果，routine 提案卡甚至會寫出沒有人確認的排程。現在照一般回合結束收卡（寫該 RID 的 `/end`，未決的批准記 `interrupted`），由 Hosted 做（`/end` 只有 Agent 能寫）：交接時在原回合的結束通知裡；棄用來源時，退役來源上已開始的回合在 commit 後的掃描裡、來源 `idle` 的在停滯標記之後（之前來源可能自己寫完，卡片照常有效）。之後的切斷（4a `NativeSessionCut`）照同一規則。
- metadata `idle` **不算**閒置的證據（第 10 點）。

| 步驟 | 做什麼 | 有柵欄嗎 | 使用者感受 |
|---|---|---|---|
| U0 觸發 | 只是**提示**（第 12 點）。條件：綁定列的 V_rec≠目標版本、目標相容且 `active`、目標不在本 Agent 或專案層的否決內、`FA_NATIVE_IDLE_UPGRADE` 開；舊 SID 相容時另要求它是 `active`（Neal 決定 5）；V_rec 不相容走第 7a 點。CAS 寫綁定列 `idle_upgrade`（`target_version`、`deferred_since`）。**4b-1 已實作**，在既有的 `NativeSessions.cut` 內（不另寫一條 U0～U9）：否決回 `vetoed`、否決列讀不到（Table 讀取失敗；程式錯誤不吞）回 `veto_unread`，相容來源不是 `active` 時不建目的 session；條件都成立、目標相容後才寫 `idle_upgrade`，來源 `idle` 或目標不相容時什麼都不寫；`FA_NATIVE_IDLE_UPGRADE` 關時 `cut` 與 2d-i 完全相同 | 無 | 無 |
| U1 存工作檔（phase 5） | 舊 SID `active` 時照第 6 點 ② 把工作檔存進 Blob（在柵欄之前存，改了 Neal 決定 2 的「切換當下」，見 M10）；開始前先讀 `agent` 紀錄的 ETag，記為這次快照的 E_snap。舊 SID 不是 `active`、不相容、沒有 files helper 或任一步明確失敗：走 ④。**修訂（2026-10-19，5b）：** ① **先探查、再快照**：U5 的探查先跑，安靜（E0）之後才開始快照；舊 SID 的 helper 在列檔之前讀 ETag＝E_snap，必須等於 E0，否則這次不切。理由：照原順序（先快照、後探查），快照開始前已接單、還在跑的回合可以在快照途中寫檔，之後不再動 ETag，E0＝E_snap 也看不出來；先由探查確認沒有進行中的回合，就關掉這個缺口。② 快照途中舊 SID 變 `idle`：立刻停、不再呼叫它，**這次不切**，等下一次找到 `active` 的回合結束再做。③ **快照失敗（舊 image 沒有 helper、超過上限、Blob 寫不成、逾時）：照現行的搬檔柵欄，不走 ④（Neal 2026-10-19 決定，見決定 2 的修訂）** | 無 | 無 |
| U2 預留並建立目的 session | 先以 CAS 把嘗試序號加一，再算新 SID：`fa-native-` 開頭，`hash("fa-native-session-v2", Agent 範圍材料, 目前 SID, 目標版本, 序號)`；以 `create_session`＋`VersionRefIndicator(目標)`＋呼叫端的 `agent_session_id` 建立（`behavior_foundry.py` 208–240 已這樣做），沒收到確認就用同一 SID 讀回。新 SID 由目前 SID 算出，所以**同一版本也能另建 session**，第 7a 點 (d)、(iii) 與第 9 點「無法為同一版本另建 session」的限制在這裡消失（M5，Neal 2026-10-13 核准，提前到 phase 3）。上一次嘗試已建好、仍健康且 `active` 的目的 session 直接沿用 | 無 | 無；多一台 VM 跑幾分鐘（費用） |
| U3 健康檢查 | 對目的 session 帶 expected＝目標送 `NativeSessionRead`（版本不同就 409，新 image 另回 `actual_version`）、profile 在支援集合、`prepare` 成功。**不另查 codec 清單**：相容＝同一個 profile＝同一份 codec 清單（第 3 點 (d)）。**另對最近的對話做 checkpoint 讀回：** 目的 session 對舊 SID 最近 N 個對話（例如最近 7 天內有回合的、最多 5 個）各跑一次 `check_latest_checkpoint`（`native_files_omission.py` 113，現行由 `_scan_page` 371–392 呼叫），確認新 image 讀得懂、可還原（用 `_RECORD_READS` 以外的新指令 `session_checkpoint_check`（`NativeSessionCheckpointCheck`，G0b 已預留），第 2 點末：目的 session 這時還不是目前 SID）；任一個明確拒絕就算健康失敗（各對話的 codec 仍由 Hosted 載入時照 CTX-03 檢查）。失敗：健康失敗次數加一，下一次觸發重試；同一目標累計 3 次→本 Agent 否決。目的 session 醒來的版本不是目標（平台漂移）：放棄這個 SID、序號加一，不算健康失敗。**4b-2 已實作**（API 的 `NativeSessions._health`，在 `cut` 內 U5 之前；哪些算失敗、時限、抖動、多副本與退回時的規則見下方「有界重試」） | 無 | 無 |
| U4 還原工作檔（phase 5） | 有完整快照（有 `manifest.json`）才做：上傳到目的 session、依 manifest 核 hash（沿用 `fa-workspace-files-v1` 範圍）。還原不明（`NATIVE_FILES_UNKNOWN`）：這次不切換，保留原 RID 與狀態待核（界線 7），下一次觸發再核。**修訂（2026-10-19，5b）：** 還原走目的 session 現有的搬檔暫存（`native_workspace_files.py` 的 `prepare`／`confirm`／`publish`，與柵欄同一套）：帶 manifest 的探查先 `prepare`（正式工作區已有別的內容就拒絕），檔案上傳到暫存路徑，切換指令逐檔核 hash 與 mode、連空目錄一起 publish，再在同一次 commit 記 `files_migrated` 與 manifest digest，所以 Hosted 的紀錄就是事實（U8）。publish 之前不碰正式工作區，還原失敗不需要換新的目的 session | 無 | 無 |
| U5 探查 | 由目的 session 執行一個切換指令（`NativeSessionCut`，phase 4 新指令；只在目的 session 上跑）：讀 `agent` 紀錄得 E0（有 phase 5 快照時要求 E0＝E_snap，否則表示快照之後跑過回合，這次放棄、下一次回合結束重做增量快照）；目前 SID 必須是舊 SID、沒有 `admitting`、沒有別的 `pending`；照上面的定義掃描。一次做不完就把進度（含 E0）存在既有的 `following:` 進度紀錄，回 `scanning`，下次接著掃。不閒置：回 `busy`，什麼都沒寫。**修訂（2026-10-19，5b）：** 順序改成**先探查、再快照**（見 U1 的修訂 ①）：探查先得 E0，之後才快照；E_snap（舊 SID 的 helper 在列檔之前讀到的 ETag）必須等於 E0，否則這次不切、下一次回合結束做增量快照。切換指令仍以 If-Match＝E0 commit | 無 | 無 |
| U6 柵欄 | 同一個指令接著以 If-Match＝E0 寫 `pending`（目標＝目的 SID、`cut_deadline`＝寫入後 5 秒，短於請求內等候）。寫不進＝掃描期間有人接單：回 `busy`，什麼都沒寫 | **有**（U6～U7） | 見「切換期間送出的訊息」 |
| U7 commit | 同一個指令：寫不可變的 `environment:<digest>`，以 CAS 把 `agent` 紀錄改成目的 SID、`ready`、新的 `chain_task_id`（同現行 743–745）。U6～U7 只有幾次 State store 讀寫（推論：亞秒到數秒；phase 4 在 dev 量測並訂上限） | **有** | 同上 |
| U8 記錄 | API 依 commit 結果以 `record_switch` 寫綁定列（`native_binding.py` 215 起，已可重入）：目前 SID、新 SID 建立時綁的版本、之前的 SID 清單、`files_migrated`、清掉漂移旗標、清掉 `idle_upgrade` 的目標並記切換時間、設通知槽。U7 與 U8 是兩處寫入，**Hosted 的 commit 是唯一的事實**，綁定列是它的副本：沒跟上時由 U8 的重跑（同一切換指令對已 `ready` 的同一目標原樣回傳，現行 `follow_session` 591 已如此）或送出時的靜默修復（M3）補寫 | 無 | 無 |
| U9 收尾 | 綁定列寫好後才 `stop_session` 舊 SID（省費用；官方：stop 結束 compute、保留檔案系統）；U5～U6 已確認舊 SID 上沒有執行中的回合，所以不會強制結束任何回合。stop 失敗或 API 在這之前崩潰：沒有人再呼叫舊 SID，平台在閒置逾時後回收。接著對新 SID 送既有的 `prewarm`（`native_turns.py` `_RECORD_READS`）；注意現行 `prewarm` 只列 toolbox（1956–1965），**不**載入對話。所以另加：切換前（U3 之後、U5 之前）在目的 session 預先還原最近 1～2 個對話的 checkpoint（`_RECORD_READS` 以外的新指令 `session_prerestore`（`NativeSessionPrerestore`，G0b 已預留），**不擴充 `prewarm`**：收緊後的 `replaced()` 會讓還沒切換的目的 session 的 `_RECORD_READS` 回 409，第 2 點末；只讀共用 store、不寫回條），否則每個對話在新 SID 的第一則訊息都要付一次 checkpoint 還原（`_continuation_blocker` 1712 起判斷跨 SID 接續）。進入 30 分鐘 crash-loop 觀察。**4b-3 已實作**（API：`_prerestore` 在 U3 之後、U5 之前；`_finish` 讀回綁定列已是新 SID 後才 stop 舊 SID、再 `prewarm`；預先還原在 cut 的時限內，stop 與 `prewarm` 用 `_finish` 自己從開始算的 40 秒時限（工作檔 fence 可能用掉 cut 的大半時限；API 關閉時照樣停），各帶逾時，任何失敗連同 traceback 只記 log、不改切換結果） | 無 | 最近的對話下一則不比平常慢；其他對話第一則多一次 checkpoint 還原（phase 4 dev 量測） |

**切換期間送出的訊息（U6～U7）：**

- 新 image 在切換用法的 `pending` 下，503 `AGENT_VERSION_FOLLOWING` 本文加 `not_dispatched: true` 與 `retry_after_ms`（到 `cut_deadline` 的剩餘時間）；`replaced` 409 本文加 `current_hosted_session_id`。舊 image 的 503 沒有這兩個欄位，但它在 670–672 回的 503 一定在建立回條之前（確定沒接單）。
- **請求內等候由 Hosted 的回應推動，不輪詢綁定列。** API 在同一個請求裡最多等 W（預設 10 秒；任何入口都不得短於 8 秒，也就是要比 `cut_deadline` 的 5 秒長出一次重送的時間）。超過 W 之後訊息靠工作台的同 RID 自動重送，所以**任何讓訊息停在 `server_following` 的等待，從第一則被擋的訊息起算的總長都要比工作台的 600000 ms 自動重送窗口**（`SERVER_FOLLOWING_TIMEOUT_MS`，`web_test_console/index.html` 774；只對 773 的 `AGENT_VERSION_FOLLOWING`／`NATIVE_SESSION_CHANGED` 重送）**短，並留出明確的安全餘裕**（#823 審查第 5 輪）；等待理由可能超過這個時間的（例如來源上的長回合），改成接單後排隊（第 7a 點「(r)(s) 的競爭」），不留在請求或重送迴圈裡：
  1. 收到 503＋`not_dispatched`（或舊 image 在建立回條前回的 503）：等 `retry_after_ms`（舊 image 沒有時固定 500 毫秒），以同 RID、同 body **再送同一個舊 SID**。
  2. 舊 SID 回接單（202 或回條）：切換已撤回或還沒寫，由舊 SID 照常處理，結束等候（這就是「退回」，主人只覺得稍慢）。
  3. 舊 SID 回 409 `NATIVE_SESSION_CHANGED`：切換已 commit。新 image 帶 `current_hosted_session_id`；舊 image 沒有時，API 取綁定列 `upgrade` 裡自己在 U2 建立的目的 SID，讀它的 `session_state` 確認它就是 Hosted 紀錄的目前 SID。照 M3 以 `record_switch` 補寫綁定列，再以同 RID、同 body 送新 SID（`not_dispatched` 保證沒派送過，界線 7；`rid:<RID>` 去重保證最多執行一次）。（4c-2，2026-10-18 已實作新 image 這一半：API 的 Foundry client 保留 `current_hosted_session_id`，`relocate` 只修到它指名的 SID、讀不到就不猜，主人送出的 `_after_refusal` 不重讀綁定列，以同 RID 直接送那個 SID 一次。）
  4. 503 持續超過 `cut_deadline`（新 image 依 `retry_after_ms`；2d-i 期間的 `pending` 沒有期限欄位，請求用自己的時鐘：這個請求裡 503 已持續 ≥5 秒）：**2d-i 搬檔模式的柵欄例外**（綁定列 `upgrade` 記著這次切換的目的 SID，M1 第 2 步）：請求不撤回，照 1 繼續等到 W，之後走 5，和現在 follow 時一樣（Neal 2026-10-15）；超過約 120 秒上限仍沒 commit 的由背景工作撤回。其他的（舊式 follow 留下的，或之後新 image 的切換用法）：這個請求自己對目的 session 送 `NativeSessionWithdraw`（生命週期表「`pending` 切換用法」撤回 ①；已 commit 就原樣回傳 commit 結果、改走 3），再回 1。**API 主動撤回是必要的**：第一次升級到新 image 時，舊 SID 的前一版 image 不會自己撤回過期的 `pending`。
  5. 到 W 仍沒有結果（例如目的 session 也叫不到）：照現行回 `server_following`，工作台用既有的同 RID 自動重送，文案照 M4 的分級顯示；背景工作繼續做完或撤回這次切換。
- **攔截點在送出的呼叫端，不在 `_invoke`**（`_invoke` 也服務讀取，而且換 SID 要由擁有 client 的呼叫端做）：
  - 共用一個 helper 包住 `submit_native_turn`（`behavior_native_responses_api.py` 167）裡的接單呼叫，並把實際使用的 client 交回呼叫端，讓之後的 Responses 第二步（`_send_after_reply`／`_send_response`）也送到同一個 SID。
  - 經過它的呼叫端各要一個測試：`NativeTurnGateway.submit`（`behavior_native_turn_api.py` 594，經 676、851）、`native_agent_api.py` 280／382／448、`native_routines.py` 598、`native_group_api.py` 94 與 `native_teams_relay.py` 166（兩者經 `gateway.submit`）。
  - 等不到時各入口照既有處理：Agent 間交辦回 503 並要求只重送原訊息（`native_agent_api.py` 509 起、`native_agent_tools.py` 437 起）；Teams 與群組照各自的同 RID 規則。
  - **routine 不再退回它自己的換環境路徑**（審查第 2 輪改）：`native_routines.py` 518–538 遇到 `AGENT_VERSION_FOLLOWING` 會經 `_destination` 自己 follow，那會寫現行 follow 的 `pending`；草案上線後（M1 的 A：2d-i 起）這條停用。等不到時照既有的 503 `NATIVE_ROUTINE_UNCONFIRMED`（同 RID，600–606）結束這次觸發，排程時鐘下一次 tick 以同一個 occurrence RID 再觸發（`native_routine_clock.py` 58 起；未結的 occurrence 仍在 `due` 內是推論，實作 PR 以測試確認）。
- **送出路徑不多任何讀寫**：觸發在回合結束後由 API 背景工作做（與 ADR-0006 排程時鐘同一類 lifespan 背景 task），不延後回應；只有剛好碰到 U6～U7 的那一則訊息會在請求內等候。

**有界重試、否決、crash-loop 與退回：**

- 同一個（Agent, 目標）健康檢查最多失敗 3 次（間隔加隨機抖動）→本 Agent 否決，留在目前相容的版本；工作台**狀態欄位**（不是聊天區）顯示一次「助理這次沒有更新，會繼續使用目前的版本。」，Teams 不顯示（Neal 2026-10-19 定句，取代原草案「新版本 vN 沒通過檢查，助理繼續用 vM」；交付見第 8 點）。U5～U6 的 `busy` 不算失敗，下一次回合結束再試，沒有次數上限。
  - **U3 哪些結果算健康失敗（4b-2，總協調 2026-10-19 決定）：** 對話結果 `unreadable` 或 `unsupported`，這一次嘗試算一次失敗（不論幾個對話）；`ok` 不算。目的 image 對 `session_checkpoint_check` 回 #906 之前的 G0b 拒絕（422 `NATIVE_REQUEST_INVALID`、訊息「… is reserved; this image does not run it」）：當作沒檢查、照常切換，不算失敗；其他 422（例如請求本身被拒）、5xx、逾時、403 `NATIVE_STATE_SCOPE` 等是「沒有答案」：不計次、這次不切換，只記這次嘗試的時間，下一次照下面的間隔與抖動再查；一個對話都沒答到時一律算「沒有答案」，不會照常切換。最近的對話**全部**回 `NATIVE_STATE_SCOPE`（新 image 的 scope 本身有問題）：這一次嘗試算一次失敗，第 3 次就否決（審查第 2 輪）。第 3 次失敗只寫本 Agent 的 `vetoed_versions`（沿用 4d 的 `record_veto`），不寫專案層否決列。API 只知道這個 process 從主人送出記下的最近對話，一個都沒有時不送檢查、照常切換。
  - **U3 的時限、抖動與多副本（總協調 2026-10-19）：** 一次只問一個對話、新到舊，整個 U3 在 20 秒內（每次呼叫以剩下的時間為逾時，小於 5 秒就不再問，較舊的這次不查），因為 API 的單次讀取逾時是 30 秒、Hosted 逐一讀各對話；同一個對話對同一個目標連續兩次 `NATIVE_STATE_SCOPE` 就略過它（process 內記憶，以 Agent、對話、目標版本為鍵，新目標重新算；略過的對話仍會問，只是它的拒絕不再讓這次嘗試變成「沒有答案」）。失敗或沒有答案後，下一次檢查至少等 60 秒再加 0～60 秒的隨機抖動（綁定列 `idle_upgrade.last_attempt_at`），之前不建立、不叫醒目的 session。失敗次數以這次嘗試讀到的 `last_attempt_at` 為鍵 CAS 寫入，兩個副本同一次嘗試只算一次。
  - **退回時的 U3（總協調 2026-10-19）：** 4d 退回的那一次 `cut`（`_ROLLBACK`）照樣跑 U3，但結果只記 log、不計次、不否決，退回照常進行：例如 42 crash-loop、留下壞的 checkpoint，41 讀回 `unreadable`，若計次則第 3 次回合結束會否決 41、`_rollback_blocked` 接手，Agent 就永遠留在 crash-loop 的 42。crash-loop 的版本比一個對話讀不回 checkpoint 嚴重；各對話的 codec 仍由 Hosted 載入時照 CTX-03 檢查（見 U3 列）。
- **crash-loop 的訊號（取代原問題 5）：**
  - 官方：`TaskContext.entry_mode` 為 `"recovered"` 表示「前一個 lifetime 沒做完、框架以保存的輸入重入」（[Recover long-running work](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/recover-long-running-work)）；崩潰與優雅延後都會這樣重入，所以單看它分不出來。
  - FA 補自己的一半：Hosted 在每個進行中回合呼叫 `exit_for_recovery()` 之前寫不可變的 `deferred:<run_token>`（標記 store）；重入時 `started` 回條的 `run_token` 沒有這個標記＝前一個 lifetime 沒經過優雅延後就結束（崩潰、OOM、程序退出），附加到 `crashes:<SID>`。
  - 對照 Neal 決定 3 的排除項：平台關機（重新部署、縮容、閒置回收、stop）會設 `ctx.shutdown`，FA 走延後並寫標記→不算；寬限期內沒停到安全點被強制結束，官方說等同取消、multi-turn chain 變 `suspended`，不會以 recovered 重入→不算；工具子程序被殺、模型或工具錯誤、使用者取消、逾時、401／403、限流都不會讓 lifetime 重入→不算。
  - checkpoint 或 `prepare` 載入失敗：Hosted 照 CTX-03 明確拒絕，同時附加一筆。runtime 起不來：Hosted 程式沒機會記，由 API 在新 SID 連續回平台 5xx／逾時、又沒有回條時記在綁定列 `idle_upgrade` 的切換後啟動失敗（`start_failures`）（只在切換後 30 分鐘內寫）。同一 RID 重試只算一次。
  - 殘餘：關機寬限期內 `deferred:` 寫入失敗，會把一次優雅延後算成崩潰；要 3 個不同 CID 與 RID 才判定，單次誤算不會觸發退回。
- **判定：** 切換後 30 分鐘內，`recent_crashes`（時間在切換之後）加上切換後啟動失敗，合計 ≥3 筆、分屬 ≥3 個不同的 CID 與 RID。API 在讀到新 SID 的終態回條或失敗時順便檢查，不另外輪詢、不叫醒。
- **處理（Neal 決定 3）：** 寫專案層否決列；本 Agent 退回：目標＝綁定列之前的 SID 清單最後一個的版本（phase 3b 保證不刪），走同一套 U0～U9、建新 SID（由目前 SID 算出），**不**叫醒原本那個舊 SID（叫醒有漂移風險，它的檔案也停在升級前）；工作檔：目前 SID `active` 就照 ② 帶，否則 ④。通知槽寫「已退回」（第 8 點新句，見M4）。
- **退回目標的否決（總協調 2026-10-19）：** 退回不看目標版本的專案層否決、只看本 Agent 的；目標也在本 Agent 的否決清單時沒有可退的版本，記一次警告、留在目前版本繼續服務，不再重試。
- **被否決的目前版本不卡退回（取代原問題 1 的選項 (a)）：** 退回時 U5 只等「確定正在跑」的回合（`started` 且 chain task 有 lease），最多等一個回合逾時；`accepted`、或停在 `started` 卻沒有 lease 的回條（崩潰留下的）不等，照第 7a 點適用條件 (iii) 的方式處理：回條原樣保留，換完讀到時回「較早的版本接下的」通知（(e)）；M9（Neal 2026-10-13 核准）：從沒開始的 `accepted` 回條改由新 SID 接手。
  - **被放過的 `accepted` 回條不會在舊 SID 開始執行：** 退回的來源一定是有設計前提 (a) 的新 image。
  - **被放過的 `started`（無 lease）回條仍可能被舊 SID 動到（審查第 2 輪更正，原文說「不會被開始執行」不完整）：** 舊 SID 的 recovered 重入在 `_handle_turn`（`native_turns.py` 1580–1590）對非 `accepted` 回條直接呼叫 `_reenter`，排在 `_require_original_session`（1599）之前，SID 已換也照樣進去。`_reenter`（1391 起）不開始新回合，只做三件事：把這個 RID 寫成終態（`completed`／`unknown`）、經 `prepare_continuation` 記下續接意圖、或經 `on_recover_queued` 補送已排隊的交辦。續接與補送都是新 RID，照上面「新 RID 接單的入口」走柵欄（切換中 503、commit 後由 API 送到目前 SID），所以不會在舊 SID 開始新工作。
  - 因此 (e) 的「較早的版本接下的」通知以**讀到時**的回條狀態為準：退回後這個 RID 若已被舊 SID 寫成終態，就照原回條回，不發這句。4d 要有這個測試。
- 不中斷正在跑的回合。延後超過 6 小時仍切不成：不強制、不提供「現在就更新」，只在工作台**狀態欄位**顯示一次「有新版本 vN，會在助理閒置時更新」（Neal 決定 4）。

### 6. 工作檔要不要帶過去

**原則不變：不為了檢查或搬檔去叫醒已回收的 session**（第 10 點）；舊 SID 只有 `get_session` 讀到 `active` 時才可呼叫或讀檔。

| 方案 | 做法 | 優點 | 缺點 |
|---|---|---|---|
| ① 切換當下搬到新 session | 沿用現有 `FollowFiles`（manifest＋hash，session 對 session），只在舊 SID `active` 時做 | 程式現成、官方 Files API | 要先建好新 session 才能搬，期間舊 compute 可能逾時回收 |
| **② 先存到 Blob，再還原到新 session（Neal 決定 2）** | U1：舊 SID `active` 時，用舊 VM 的 helper 產生 manifest（現有 `fa-workspace-files-v1` 範圍），API 經 Files API 下載各檔存進該 Team 的 Blob（ADR-0002「Blob 保存成果／固定版本」，ADR-0006 經 API 薄轉送；放在快照專用儲存帳號，Neal 2026-10-17，見下方快照規則）；U4 上傳到新 session 並核 hash | 快照在建立新 session **之前**完成，不受建立時間影響；快照可保留作備份 | 新增 Blob 存放與保留規則 |
| ③ 每個回合結束都存一次 | Hosted 在回合結束時把工作區增量存 Blob | 任何時候升級都有最新檔案 | 每回合多成本與延遲 |
| ④ 不搬，明說 | 沿用 2026-10-04 系統政策 | 最簡單 | 使用者失去工作檔 |

**Neal 決定 2：② 為主，④ 為退路。** 主要觸發是回合結束當下（第 12 點），那時舊 compute 幾乎一定還 `active`，所以 ② 大多做得成。舊 SID 已是 `idle`、不相容（例如所有 `foundry-native-v1` 版本）、舊 image 沒有 files helper、或快照任一步明確失敗：走 ④，`files_migrated=false`、理由記錄。FA 不刪舊 SID 與檔案，但平台 30 天無活動後會永久刪除 session，所以**長期副本只有 ② 的 Blob 快照**。③ 留作日後 ② 成功率太低時再提。**修訂（Neal 2026-10-19 決定）：** 快照失敗時退回現行的搬檔柵欄，**不走 ④**：工作檔照樣搬，代價是切換當下送出的訊息可能要等最多約 5 秒。④ 只留給柵欄本身也搬不了檔的情況（例如來源已退役或不相容，第 7a 點與 M1 第 2 步）。快照途中舊 SID 變 `idle`：這次不切、不搬，等下一次找到 `active` 的回合結束（2026-10-19，5b）。

② 的快照規則：

- **快照與切換之間不加柵欄（樂觀做法；改 Neal 決定 2 的「切換當下」時點，見 M10）：** 快照時主人可能照常聊天、改到檔案。判斷依據是 `agent` 紀錄的 ETag：快照開始時記 E_snap，U5 要求 E0＝E_snap，U6 再以 If-Match 確認到寫入 `pending` 為止都沒有接單；只要沒有接單，就沒有回合在快照之後改過檔案。不相等就放棄這次切換，下一次回合結束時做**增量**快照（只傳 hash 變了的檔），那時目的 session 已建好、健康，快照到切換只隔幾秒。
- **每次 Files 呼叫前都重讀舊 SID metadata**，確認仍是 `active`；途中變成 `idle` 就立刻停、不寫 `manifest.json`、改走 ④，不再對舊 SID 發任何 Files 呼叫。**修訂（2026-10-19，5b）：** 途中變 `idle` **這次不切**（不走 ④、不丟工作檔），下一次找到 `active` 的回合結束再做；快照本身失敗則照現行的搬檔柵欄（Neal 2026-10-19，決定 2 的修訂）。官方沒說 Files 呼叫會不會重設閒置計時，FA 不依賴它。
- **大小上限沿用 `fa-workspace-files-v1`**（`native_files_wire.py`：單檔 32 MiB、總計 64 MiB、最多 2000 個項目；Neal 決定 2）。單檔 32 MiB 低於官方 Files API 單檔上傳上限 50 MB（[Session file operations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#session-file-operations)）。manifest 超過任一上限：不做部分快照，整次走 ④ 並記理由（**修訂（Neal 2026-10-19）：** 改成照現行的搬檔柵欄，柵欄也有同樣的上限；兩者都搬不了才是 ④）。Hosted 回應經 `invoke_native` 的 8 MiB 上限讀（`behavior_foundry.py` `MAX_NATIVE_TURN_HTTP_BYTES`），所以 2000 個項目的工作區樹讀得完。
- **保留 30 天**（Neal 決定 2），刪除由 Blob lifecycle 規則做（生命週期表）。
- **快照放在專用儲存帳號（Neal 2026-10-17）：** 不放在 `no-auto-gc` 備份帳號（stage `stfam1bkstagedemo`；它的來源／備份保護檢查遇到任何啟用的刪除規則就停），另建一個只放快照的帳號（`infra/shared-context/upgrade-snapshots.bicep`：StorageV2、LRS、只走 HTTPS／TLS 1.2、不開公開存取與共用金鑰、私有端點、不開版本控制、一個 container、30 天規則、帳號加 CanNotDelete 鎖、只有 API 身分在該 container 有 `backup-role.bicep` 的讀寫自訂角色，不含刪除；FA 不刪快照，刪除交給 30 天規則），30 天規則因此安全。API 讀 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`；沒設就不做 phase 5，**永遠不退回備份帳號**，設成來源或備份帳號時 API 啟動即失敗。
- **已接受的殘餘：** 回合以外的背景程序（例如回合啟動、仍在跑的 shell 工作或桌面程式）在快照之後改的檔，ETag 看不出來，這次不會帶過去；舊 session 的檔案系統仍在（stop 保留檔案系統，平台 30 天後刪除），快照也在。

### 12. 誰觸發升級，以及部署不再 follow（原第 7 點前半）

- **主要觸發：回合結束時。** API 讀到某 Agent 一個回合的終態回條後（主人、routine、交辦都算），若 V_rec≠目標版本，就在背景開始 U0（2d-i 的背景切換：來源 metadata 是 `idle` 時什麼都不做、回 `source_idle`，所以讀到舊終態回條觸發也無害，見第 2 步「只在共用 store 安靜時切」）；**不再等安靜時間**（原草案 120 秒）：準備階段沒有柵欄，切換靠 CAS，不會吃掉主人的快速追問——追問先到，切換就 `busy`，下一次回合結束再試，而且那時目的 session 已經是熱的。送出在第 4 點第 1 步判定 V_rec 不相容時也觸發，走第 7a 點。
- **備援：API 背景 reconciler。** 預設關（`FA_NATIVE_UPGRADE_RECONCILER`），依環境開；每 15 分鐘加隨機抖動掃一次綁定列。多個副本不需要租約（每一步可重入），只以綁定列 `idle_upgrade` 的「最近一次嘗試時間」（`last_attempt_at`）避免重複做同一件事。它只處理：(a) V_rec 不相容的 Agent（反正不能跑，走第 7a 點並通知）；(b) 舊 SID 仍 `active`、但錯過回合結束觸發的 Agent（例如 API 在途中重啟）；(c) 延後超過 6 小時的狀態欄位。相容且 compute 已回收的 Agent **不主動升級**，等它下一個回合結束（Neal 決定 5）。**6a 實作（2026-10-17，#885）：** 綁定列還沒有嘗試時間欄位（phase 4b 才加），所以去重只在 process 內：交給切換 worker 的 Agent（(a)、(b)）1 小時內不再交；V_rec＝selector（沒有要做的）、相容但不是 `active`（不動），或選定版本還沒就緒的 Agent，綁定列沒變就 24 小時內不再讀（每日巡查的週期）：V_rec＝selector 的 Agent 每天只讀一次 selector、不讀 `get_session`；V_rec 不是 selector 的閒置 Agent 到期時每天多讀一次 `get_session`。API 重啟會清空這份紀錄，重啟後第一輪全部重讀，正好接住 (b)。(a) 不相容但還沒有棄用決定的列，6a 不寫決定（交給 `cut` 只回 `unbound`），第 7a 點的決定與通知等 phase 4b。**(c) 實作（6a (c)）：** 只在 `FA_NATIVE_IDLE_UPGRADE=1` 也開時啟動；掃到綁定列 `idle_upgrade.deferred_since` 超過 6 小時、目標仍不是 V_rec，就以單列 CAS 設 `idle_upgrade.waiting=pending`，每個目標一次；目標已在本 Agent 的 `vetoed_versions` 或有專案層否決列時不設（U0 不會切過去；本 Agent 否決後，否決前已設的 note 也不再顯示，免得「沒有更新」之後又出現「有新版本 vN」，審查第 2 輪）（判斷用本來就讀到的列，排在每日退避之前；不讀 selector、不叫醒、不切換）。交付：`NativeReceipt.upgrade_waiting_version`（v2 凍結不能加列舉值，所以不是通知種類，是預設不輸出的選填欄位），只給主人本人的工作台送出回應，且要同時帶 `X-FA-Notice-Kinds: 2` 與另一個標頭 `X-FA-Upgrade-Waiting: 1`（`Notice-Kinds` 維持 `2`，舊 API 只認得這個值；API 與工作台哪個先上線都不會退步）；槽裡有較重的通知時留到下一則；寫出後改 `shown`（保留，同一目標不再做）；Teams 不帶。目標換了或切換完成，`idle_upgrade` 重建，note 結束。
- **部署：** 只發布版本並設定 selector（目標版本），**不再**在部署當下 `follow_all`；送出時也不再 `_follow_before_admission`（phase 4 停送出時、phase 6 停部署時，第 7 點）。部署成功不等任何 Agent 換完；部署前相容閘門與版本保留照 phase 3b。
- **啟用條件（取代原問題 4 的未定）：** `FA_NATIVE_IDLE_UPGRADE` 預設關。只有在 phase 4 的 API 是唯一運作中的 revision（dev／stage 的 API 是單副本，ADR-0006「API 兼任排程時鐘」一節）而且 Hosted 新 image 已是所有 Agent 的目標、開放 RID 集合已補齊（M1 的 A「閘門清單」第 8 項；M1 的 A 時 2d-i 另要清單其他各項）後才開；即使有舊 API revision 重疊，它們也只能經 Hosted 派送，被同一個 `agent` 紀錄擋住（設計前提）。**程式怎麼擋（4b-1）：** `FA_NATIVE_IDLE_UPGRADE=1` 而 `FA_NATIVE_SESSION_GUARD_ENFORCE` 不是 `1` 時，API 啟動就拒絕（`ValueError`），`ci_deploy.py` 也拒絕這份 profile；打開的那一次，部署照 2d-i 打開時的規則要求單一 revision 模式、只有一個 active revision 而且是 `latestReadyRevisionName`（API 程序自己看不到 revision），另以 AST 讀服務中 commit 的 `native_binding.NATIVE_IDLE_SCHEMA`（4b-1 起為 1；6a (c) 的 `idle_upgrade.waiting` 起為 2；4d 起為 3，`ci_deploy` 要求 3：比它舊的 API 解不開「已退回」的通知槽值 `rolled_back`／`rolled_back_without_files` 與柵欄的 `rollback`），不夠就拒絕（2026-10-19 起開著時的每一次部署也查，只是不要求只剩一個 active revision）；開過之後 API 不退回 4d 以下（舊 API 讀到帶這些值或 `waiting` 的列回 409）。所以先以 `"0"` 部署含 4b-1 的 commit，下一次部署才改 `"1"`（同 2d-i 開關的兩步）：否則新 revision 第一次寫 `idle_upgrade` 時，還在服務的舊 revision 讀那一列會回 409。新 image 已是所有 Agent 的目標、開放 RID 集合已補齊、4a 的開始計數已上線，程式不檢查，由打開的人核對。

### 實作階段（phase 4～6）

每一步一個小 PR，各附離線替身測試；真平台另在 dev 驗，stage 照 AGENTS 的 release PR 流程由 Neal 決定。每一步都列「使用者感受」。phase 4～6 用到的 wire／state 欄位（`cut_deadline`、`retry_after_ms`、`NativeSessionCut`、`NativeSessionWithdraw`、`replaced` 409 的 `current_hosted_session_id`、`recent_crashes`、開放 RID 集合、標記 store 的 `deferred:`／`crashes:`／`refused:` 項目）要在 phase 2 的 v2 schema 快照裡先定好（M7），phase 4～6 就不必再換 profile、不會再有一次「全部不相容」。這份快照就是 H2d（2d-i 的 Hosted image）的 v2 快照，在 H2d 定案、含設計前提 (a) 的開始計數欄位；2d-ii 只改 API（總協調 2026-10-15）。

**Phase 4：閒置升級核心（先做 ④ 不搬檔）**

| 步驟 | 內容 | 驗收（離線替身） | 使用者感受 |
|---|---|---|---|
| 4a Hosted 切換指令（新 image） | `NativeSessionCut`＝U5～U7；切換用法的 `pending`（帶 `cut_deadline`）與 `NativeSessionWithdraw`；設計前提 (a)(b)；開放 RID 集合與標記 store；同一版本另建；503 帶 `retry_after_ms`、`replaced` 409 帶 `current_hosted_session_id`；`refused:<RID>` 與讀取投影。細節見表後「4a 細節」 | 見「4a 細節」 | 切換的柵欄區段（U6～U7）在替身上 ≤ 1 秒；切換以外的時間主人照常聊天 |
| 4b API 協調（U0、U2、U3、U8、U9） | `idle_upgrade` 子物件（4b-1 已加，連同 U0 條件與 `FA_NATIVE_IDLE_UPGRADE`）；新 SID 雜湊與先預留再建立；健康檢查（含最近對話的 `check_latest_checkpoint`）；切換前預先還原最近 1～2 個對話（兩者都用 `_RECORD_READS` 以外的新指令：`session_checkpoint_check`、`session_prerestore`，G0b 在第 31 版之前預留，這個 image 回 422）；回合結束觸發（背景）；`FA_NATIVE_IDLE_UPGRADE` 預設關；`record_switch` 寫 `files_migrated=false`；stop 舊 SID；`prewarm`；設通知槽。**已實作：** 4b-1（U0）、4b-2（U3）、4b-3（U9）、**4b-4**（U3 否決的「沒有更新」通知：綁定列槽值 `vetoed`、通知種類 `environment_upgrade_vetoed`、header `X-FA-Upgrade-Vetoed-Notice: 1`，`NATIVE_IDLE_SCHEMA`／`API_IDLE_SCHEMA` 4；第 8 點、M4 第 6 項） | 送出路徑的 Table 讀寫次數與 phase 3 相同（計數）；每一步之間崩潰→下一次觸發接著做，不會出現同序號的第二個 SID；目的 session 醒來版本不同→序號加一、不算失敗；3 次健康失敗→本 Agent 否決、狀態欄位一次；最近對話 checkpoint 讀不回→算健康失敗；收緊的 `replaced()` 下，切換前對目的 session 的讀回與預先還原不回 409；舊 SID 相容且 `idle`→不升級；V_rec 不相容→走第 7a 點；U7 後、U8 前崩潰→重跑補寫，之前的 SID 清單含舊 SID；stop 只在 U8 之後 | 部署與升級都不打斷對話；最近的對話升級後第一則不比平常慢（替身確認 checkpoint 已預先還原、有送 `prewarm`） |
| 4c 切換期間送出 | API 請求內等候（由 Hosted 回應推動：同 RID 再送舊 SID、接單＝已撤回、409 帶 `current_hosted_session_id`＝送新 SID、超過 `cut_deadline` 由 API 撤回）；M3 的靜默修復；工作台M4 的分級文案；停用送出時 `_follow_before_admission` 的「selector 動了就 follow」 | 柵欄期間送出→同一 RID 只派送一次、送到新 SID、回應正常；切換被撤回→同 RID 由舊 SID 接單；`pending` 過期而舊 SID 是前一版 image→API 撤回後由舊 SID 接單；超過 W→`server_following`、工作台同 RID 自動重送；`replaced` 409→綁定列修好、同 RID 重送、不回錯誤；六個呼叫端（工作台 submit、交辦 280／382／448、routine 598、群組 94、Teams 166）在柵欄期間各一個測試（請求內等候吸收，或照既有處理）；Responses 第二步送到等候後實際使用的 SID | 那一則訊息最多慢 10 秒，不出現錯誤列 |
| 4d crash-loop、否決與退回 | `deferred:`、`crashes:`、`recent_crashes`、切換後啟動失敗、專案層否決列、退回 | 設了 `shutdown` 的延後→不算；沒有標記的重入→算一筆；同 RID 重試只算一次；強制結束後變 `suspended`→不算；3 個不同 CID＋RID→否決＋退回＋通知槽「已退回」；被否決版本上崩潰留下的 `started`（無 lease）回條不擋退回、切換本身不改它；讀到時仍非終態→回 (e) 通知，已被舊 SID 的 recovered 重入寫成終態→照原回條；續接與補送的新 RID 在切換中回 503、commit 後送到目前 SID；退回不叫醒原舊 SID | 不穩的新版本最多影響 3 則訊息就自動退回，主人看到一行說明 |

**4a 細節：**

- 內容：任一 session 接單檢查時以 CAS 撤回過期的切換 `pending`；四條開始執行的路都經 `agent` 紀錄 CAS（設計前提 (a)）；開放 RID 集合（含第一次的補齊，已先實作，見第 5 點）；標記 store；同一版本另建（`native_environment.py` 608 的「要較新的版本」改成「目的 SID≠目前 SID、版本＝指令明定」）。
- 驗收（離線替身）：
  - 掃描中有新 RID 接單（回合、Responses、交辦、群組、routine 各一）→ ETag 變、回 `busy`、什麼都沒寫。
  - **切換用法的 `pending` 下，四條路（同 RID 重送到 `accepted` 的 RID、`_enqueue_attempt`、`recover_owned`、Responses 處理器）各一個測試→不開始、回 503＋`not_dispatched`、回條不變；撤回後照常開始；現行 follow 的 `pending` 下不受影響。**
  - 孤兒只在設計前提 (b) 全部條件成立時不擋切換；反例各一個（`run_token` 非空、Responses `needs_response`／`not_taken`、`_defer_enqueue` 留下且 enqueue task 還在、兩次讀取間 ETag 變了）→擋。沒有完整集合的來源→全掃描、嚴格規則。
  - 探查只讀開放 RID 集合（替身計數：讀取次數不隨歷史回條數增加）；集合補齊前不開旗標（M1 的 A「閘門清單」第 8 項）。
  - `pending` 寫入後 commit 前崩潰→API 撤回後照常接單，或重送同一指令做完；撤回與 commit 同時→只有一個成功。
  - 舊 SID `idle` 時整個切換對舊 SID 零呼叫（替身記錄 invoke 與 Files 都是 0）；同一版本另建成功；重送同一指令對已 `ready` 的目標原樣回傳。
  - 被拒絕的釘選輸入→`refused:` 有一筆、回條逐位元組不變；HITL 只有一種形態（回合停著等主人，見第 5 點 HITL 一項），每種卡片（提問、四種批准、Connect）各有測試。

**dev 驗收（phase 4）：** dev 部署開 `FA_NATIVE_IDLE_UPGRADE`，發布一個只改提示的相容版本；A、B 各聊幾輪。通過條件：① 工作台與 API log 沒有任何回到主人的拒絕（`server_following` 顯示次數 0；被請求內等候吸收的次數另計）；② `NATIVE_UPGRADE_CUT` log 的柵欄區段時間（訂出上限，例如 p95 ≤ 2 秒）；③ 升級後第一個回合的 RTT 不高於升級前的暖回合基準；④ 舊 SID 被 stop、綁定列與 Hosted `agent` 紀錄一致；⑤ 費用：多出的 VM 時間只有準備期間；⑥ U5 探查時間低於門檻（只讀開放 RID 集合，第 5 點）；⑦ 第一次升級到 phase 4 image 時，人為中斷在 U6 之後，確認 API 撤回（背景工作或請求內等候）能收拾 `pending`；⑧ 升級後各對話第一則訊息的 checkpoint 還原時間（預先還原的對話與其他對話分開記）。**stage：** 照 release PR 流程，Neal 核准後才部署；旗標在 stage 另經 Neal 決定才開。

**Phase 5：工作檔 ②（Blob 快照與還原）**

| 步驟 | 內容 | 驗收（離線替身） | 使用者感受 |
|---|---|---|---|
| 5a 快照存放 | Team Blob 的 `fa-upgrade-snapshots/<tenant>/<team>/<agent>/<upgrade_id>/`（tenant、Team 也在 metadata）、`manifest.json` 最後寫、hash 核對、`fa-workspace-files-v1` 上限 | 沒有 manifest 的前綴永遠不還原；超過上限→整次不快照並記理由（原寫「整次 ④」；**修訂（Neal 2026-10-19）：** 改成照現行的搬檔柵欄，兩者都搬不了才是 ④）；hash 不符→不寫 manifest | 無 |
| 5b 接進 U1／U4 | 快照、增量快照、還原；E_snap 條件；途中 `idle`→④。**已實作（2026-10-19，`upgrade_snapshot_flow.py`）**：只在 `FA_NATIVE_IDLE_UPGRADE` 與 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL` 都開時，由背景 `cut` 在安靜的探查（E0）之後做：舊 SID 的 helper（`NativeSessionRead` 新的選填欄位 `workspace`，回應多 `workspace_root_mode`／`workspace_entries`，預設不輸出，過 v2_drift）回工作區樹與先讀的 `agent` ETag＝E_snap，要等於 E0；檔案寫進快照帳號、manifest 最後寫；同一舊 SID 上一份快照裡 hash 相同的檔從 Blob 複製、不再下載（增量，上一份快照只記在 process 記憶體）；讀回 manifest，經目的 session 現有的搬檔暫存還原（`NativeSessionCut` 新的選填欄位 `files_restore`，預設不輸出：帶它的探查先 `prepare`，檔案上傳到暫存路徑，以 E0 送的不帶柵欄切換指令逐檔核 hash 與 mode、連空目錄 publish，再在同一次 commit 記 `files_migrated=true` 與 manifest digest）；綁定列照 Hosted 的結果記，通知「已更新」，重跑、M3 修復或兩處寫入之間崩潰都一樣。舊 image 拒絕新欄位（422）就照現行柵欄，並依版本記住、之後不再問（2026-10-19 審查：只記舊 image 對未知欄位的那一句拒絕「only the typed native request is accepted」，其他 422 只算這一次失敗；第 3 輪：只記只帶 `workspace` 的 `session_read` 收到的那一句，`session_cut` 的同一句可能是 5b image 的 wire 拒絕這一份 manifest，也只算這一次）。**與本列原文不同（G 計畫優先）：** 途中 `idle`→不再呼叫、這次不切（不走 ④ 丟工作檔），下一次找到 `active` 的回合結束再做；快照寫不成（舊 image 沒有 helper、超過上限、Blob 失敗）→照現行的搬檔柵欄；探查與快照順序對調（先探查再快照），這樣快照開始時沒有進行中的回合 | 只在 `active` 時呼叫；快照後有接單→這次不切、下一次增量；途中 `idle`→不再呼叫、這次不切；還原不明→不切換、保留待核（第 4 輪：暫存檔的上傳回應不明，照柵欄的做法立刻讀回那個暫存路徑，內容相符就繼續、同一次切換完成；讀回失敗就在同一次 `cut` 照現行柵欄，並算一次還原失敗；讀回之前不再對那個路徑 PUT，記在 process 記憶體）；暫存的檔 hash 不符→不切，下一次同一個目的 session 再試；同一目標的切換指令連續 3 次還原失敗（例如 Hosted 的 rename 被拒）→之後照現行的搬檔柵欄（Neal 2026-10-19：快照失敗退回柵欄；次數只記在 process 記憶體）；目的 session 的正式工作區已有別的內容（publish 之後 commit 輸給新接單）→換新的目的 SID（M5），之後快照失敗也一樣，不退回會被 `prepare` 拒絕的柵欄；但它的工作區正好是這個 Agent 上一份已發布的快照（同一個舊 SID）時保留它、這次不切、不換新 VM，下一次回合結束重新快照（2026-10-19 審查），這樣保留也算一次失敗（第 3 輪：例如 Blob 一直寫不進去）；到了 3 次、走柵欄之前，目的 session 的正式工作區有內容但不是舊 SID 現在的整棵樹→換新的目的 SID（M5），是同一棵樹才照柵欄；任何一次切到這個目標（柵欄也算）都清掉次數；manifest 格式不明→拒絕、不還原、不切；目的 session 的等候、探查、快照、還原與不帶柵欄的切換指令共用一個從 `cut` 開始算的 `CUT_DESTINATION_SECONDS` 時限，每次呼叫的逾時是剩下的時間，單檔的上傳／下載剩不到它自己的上限（15 秒）就不開始，超過就照現行柵欄；快照與還原在時限前 15 秒就停，留給切換指令；切換指令回應不明→讀回 Hosted 的紀錄（已 commit 就照記），不算還原失敗（第 3 輪）；讀回或 M3 修復記下的 commit 之後也跑 U9（停舊 SID、預熱新 SID，第 4 輪）；關機時停（2026-10-19 審查） | 換完工作檔都在；通知一行「已更新，工作檔也一起帶過來了」 |
| 5c 專用帳號與 30 天刪除規則（Neal 2026-10-17） | 快照專用儲存帳號（`upgrade-snapshots.bicep`，`deployUpgradeSnapshots` 預設關；azd `FA_DEPLOY_UPGRADE_SNAPSHOTS`）＋其上的 Blob lifecycle management 規則（前綴＋30 天）＋API 身分的 container 角色；API 設定 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL` | 規則內容（只刪該前綴、30 天）；預設不建任何資源；角色範圍只有該 container、只給 API 身分；設定指向來源或備份帳號時啟動失敗 | 無 |

**dev 驗收（phase 5）：** 在 A 的工作區建幾個檔、觸發升級，新 session 看得到同樣的檔（hash 相同）；量快照與還原時間；途中讓舊 session 閒置的情況照 ④（**修訂（2026-10-19）：** 途中閒置：這次不切、不搬，下一次 `active` 的回合結束才換；另驗快照失敗（例如快照帳號暫時寫不進）時照現行柵欄搬檔，以及新 session 的檔案 mode 與空目錄和舊的一樣）。**5c 的規則絕不可套在 `no-auto-gc` 備份帳號**（`backup-foundation.bicep`；stage `stfam1bkstagedemo`）：該帳號的來源／備份保護檢查只要看到帳號上有任何啟用的刪除規則（不論前綴）就回 `SOURCE_PROTECTION_UNVERIFIED`，規則變更也會讓釘住的 `evidence_sha256` 不符而回 409 `SOURCE_PROTECTION_CHANGED`，來源與備份工作都會停（`source_materials.py` `verify()`）。**Neal 決定（2026-10-17）：** 快照放在新的專用儲存帳號，與受保護的 `no-auto-gc` 備份帳號分開，30 天規則因此安全（第 6 點）。**建立這個帳號與 API 身分的角色指派是雲端與 IAM 寫入，每次部署都要 Neal 批准，dev 也一樣**：既有的 dev 部署批准不涵蓋新的雲端資源或 IAM。程式只先備好（`deployUpgradeSnapshots`／`FA_DEPLOY_UPGRADE_SNAPSHOTS` 預設關，stage 的 `upgrade-snapshots.stage.parameters.json` 也是 `false`）；批准前不建帳號、API 不設 `FA_UPGRADE_SNAPSHOT_CONTAINER_URL`，phase 5 保持關閉。實際刪除時間是 30 天加上帳號的 blob 軟刪除保留期，專用帳號是 7 天軟刪除，約 37 天。

**Phase 6：觸發與部署**

| 步驟 | 內容 | 驗收（離線替身） | 使用者感受 |
|---|---|---|---|
| 6a reconciler | 預設關；只做第 12 點的 (a)(b)(c)；以「最近一次嘗試時間」避免重複（2026-10-17，#885：先做 (a)(b)，去重在 process 內加每日退避，見第 12 點；(c) 已做；嘗試時間欄位與 (a) 的棄用決定等 4b） | 兩個副本同時跑→結果相同、不重複建立 SID；相容且 `idle` 的 Agent 不動；不相容的 Agent 走第 7a 點 | 無 |
| 6b 部署不再 follow | `ci_deploy` 拿掉 `follow_all`（3b 的相容閘門與版本保留不變） | 部署過程對 session 零呼叫；部署成功不等任何 Agent | 部署當下沒有任何 Agent 被打斷；各 Agent 在自己下一次回合結束後換好 |

**dev／stage 驗收（phase 6）：** dev 部署一個新版本，確認部署 log 沒有任何 session 呼叫、A／B 在下一次回合結束後換好、主人看不到拒絕；stage 照 release PR 流程。

### 原已知未解問題的解法（2026-10-13 定稿輪；審查第 2～4 輪原列 9 項）

1. **過期的派送意圖紀錄會讓 Agent 永遠不閒置 → 不建立這個紀錄。** 未完成的工作由 Hosted 的 `admitting` 與共用 store 的回條表示，目的 session 讀得到，不必 invoke 舊 SID。它們都有既有的結束路徑：`admitting` 由 `_settle_admission` 清；活著的 SID 上的回合由 Hosted 的回合逾時寫成終態；`idle` 的相容 SID 上被延後的回合，下一次送出叫醒它時由 recovery 接手、結束後才升級（Neal 決定 5）；不相容或已失效的 SID 照第 7a 點不等；同一 SID 漂移後永遠不會跑的回條照第 7a 點 (iii) 不等。
   - **孤兒回條（推論）：** `rid:` 已建立、chain 輸入卻沒排進去就崩潰時，`_settle_admission` 會清掉 `admitting`，留下一個活著的 SID 上沒有任何計時在跑的 `accepted` 回條，它會永遠擋住切換。
   - 規則（審查第 1 輪收窄）：照設計前提 (b) 的全部條件才算孤兒（`run_token` 空、不是 Responses `needs_response`／`not_taken`、看不到 owned／排隊輸入、不是 `_defer_enqueue` 留下的、相隔 ≥60 秒兩次讀取 ETag 沒變），留 log、照第 7a 點 (iii) 不等（回條原樣，換完讀到時回 (e) 通知；M9 由新 SID 接手）；而且只在來源是有設計前提 (a) 的新 image 時適用。寫入順序由實作 PR 核對確認。
   - 「先開新、再關舊」的問題也隨之消失：`admitting` 每個 Agent 只有一個，由 Hosted 排序。
2. **Hosted `pending` 不會被撤回 → 只在切換的一瞬間寫，而且會自己失效。** 準備階段不寫；U6 以 If-Match 寫、帶 `cut_deadline`（5 秒，短於請求內等候）；撤回的三條路見生命週期表後「`pending` 切換用法」，其中 **API 主動撤回是必要的**（背景工作或請求內等候超過期限的送出請求都會做）。第一次升級到新 image 時，舊 SID 跑的是前一版 image，它的 670–672 只會回 503、不會看期限，API 撤回是唯一的修復路；從舊 SID 那一側自行撤回，要從第二個新 image 版本起才有。寫入由目的 session 做，不需要舊 SID 醒著。
3. **意圖紀錄的寫入方式與 ETag 競爭 → 消失。** 沒有 API 端的意圖紀錄，就沒有綁定列 ETag 被每次送出改寫的問題；綁定列在送出路徑只讀不寫（第 4 點的漂移 CAS 只在觀察到不同版本時寫）。切換期間的可重試回應沿用 503 `AGENT_VERSION_FOLLOWING`＋`not_dispatched`，另加 `retry_after_ms`，不需要新錯誤碼。
4. **滾動部署時的舊 API 副本 → 由 Hosted 擋。** 寫 `pending` 不靠舊 SID 醒著，所以不論舊 SID 狀態，舊副本經 Hosted 派送都會在切換期間收到 503、commit 後收到 409；另加啟用條件（第 12 點）。
5. **crash-loop 的崩潰訊號 → 官方 `entry_mode="recovered"`＋FA 的 `deferred:` 標記**（第 5 點）；記在 Hosted `crashes:<SID>`，不改回條；runtime 起不來由 API 記在綁定列。
6. **U8 兩處寫入的可重入 → Hosted commit 是唯一事實，綁定列是副本。** 同一切換指令對已 `ready` 的同一目標原樣回傳（`follow_session` 591 的現行行為），`record_switch` 可重入（`native_binding.py` 215 起，含 selector 中途換版時兩個之前 SID 都保留）；送出時遇到 `replaced` 409 由 API 靜默修復（M3）；舊 SID 只在綁定列寫好後才 stop。
7. **U6 的 codec 檢查 → 拿掉，改在 U3 對最近的對話做 checkpoint 讀回。** 相容就是同一個 profile，profile 已涵蓋 codec 清單（第 3 點 (d)，快照測試保證）；跨 profile 不在支援集合內（Neal 決定 6；M1 只在遷移期間有限度放寬），走第 7a 點。U3 另對最近的對話跑 `check_latest_checkpoint`，讀不回就算健康失敗。各對話的 codec 仍由 Hosted 載入時照 CTX-03 檢查、不認識就明確拒絕並保留最後合法狀態。
8. **「每個對話通知一次」的標記 → 不建立。** 改成每個 Agent 一個通知槽，由第一個主人本人的回應帶上、寫出後清除（M4 的 A）；非主人入口不顯示也不清；回應在網路上遺失則漏掉（界線 7，接受）。
9. **同一 SID 相容漂移時釘在舊版本的 RID → 新 image 寫 `refused:<RID>`。** Hosted 依 `execution_version` 拒絕執行時寫一次，回條不動（Neal 決定 8）；讀這個 RID 時，Hosted 的讀取投影（不是存檔）帶「版本已變、無法接續」，API 回第 8 點「這則訊息是較早的版本接下的……」。舊 image 沒有這個項目，照選項 (c) 以一般未決狀態回報。這些 RID 本來就不擋換環境（第 7a 點 (iii)），閒置升級的掃描對它們同樣適用 (iii)。

## 對已確認部分與 Neal 決定的修訂提案（Neal 2026-10-13 已核准：M1（含 M2 不拒絕）、M3～M10 採 A）

依 Neal 2026-10-13 的最高原則，重新檢查已確認的部分，下面幾處會讓主人看到拒絕、等待、多餘的通知或丟掉工作。**Neal 2026-10-13 已核准 M1（含 M2 不拒絕）、M3～M10 採 A（Neal 核准紀錄：M1 含子選項「不設固定期限」、遷移期間放寬決定 6；M6 取代決定 7、M9 修訂決定 8、M10 修訂決定 2 的時點）**，下面各項只留核准的做法與原分析，其餘選項已刪。尚未實作：決定部分的原文在各 phase 上線前仍是現行行為，上線時才依此改。原 M2（不相容但仍 `active` 的來源）併入 M1：Neal 2026-10-13 決定這種情況**一律不可拒絕**，所以不再保留「拒絕＋分級文案」的做法，改用部署順序讓它不發生。

### M1（併入原 M2）：v1→v2 先擴充、再收斂，遷移期間沒有不相容的來源（Neal 已核准（2026-10-13）：A，含子選項：不設固定期限）

**問題：** 照原文，第一個 G API 上線那一刻所有 Agent 都不相容；`active` 的 v1 來源要等它閒置（stage 最多約 1 小時），期間每則訊息都被拒（第 7a 點）。Neal 2026-10-13：這種情況不可出現拒絕窗口。

**A（Neal 已核准）：用部署順序讓「不相容的 `active` 來源」不發生。**

**名稱與順序（審查第 2 輪對齊 #824）：**

- phase 2 的 2a～2c（2a 協定欄位〔#824，已合併〕、2b 第 7 點／第 7a 點三項必要範圍、2c 通知顯示）都是 v1 時代的一般部署，換環境照現行 follow，不屬本節。
- 本節只改 2d（換 profile），拆成 **2d-i 擴充**與 **2d-ii 收斂**。
- 順序：2a～2c→phase 3（含 M5）→2d-i→逐一搬移→閘門清空→2d-ii。

**v1 wire 凍結：** 以 2d-i 前的最後一個 image 為基準（以下稱 H0）。從 H0 起到 2d-ii 為止，不改 v1 schema：2d-i 的 image 與凍結期間發布的任何 image，v1 wire 快照都要與 H0 相同（#824 的快照測試比對；部署時擋是閘門清單第 3 項）。只改提示、工具或 Hosted 行為的發版照常。

H0 的基準檔是 `tests/shared_context/fixtures/native_v1_wire_h0.json`（2d-i P1，#846，已合併）：以 stage 第 30 版（`fb8941f2`）的程式產生一次，內含各 v1 wire／state 模型的完整 schema、各欄位怎麼輸出、`NATIVE_REQUEST` 的各成員、SDK／runtime 固定版本、checkpoint codec 清單，以及 v1 項目的 canonical bytes；`FA_REGENERATE_PROFILE_SNAPSHOT` 不會改寫它。`test_native_profile_snapshot.py` 對它只允許新增（新模型、request／command 層的新成員、加寬的 enum 例如 v2 profile 值、預設時不輸出的新選填欄位），其他差異都會失敗，包括既有欄位變成 union 或多了 H0 讀不懂的 union 成員、預設時不輸出的欄位改了預設值；在閘門清單第 3 項的 CI 與部署前測試擋下。因此 SDK／runtime 固定版本、codec 清單與 pydantic（2.12.5，基準檔的 schema 由它產生）的升級，要等到 2d-ii 之後的第一個 image 才能做（總協調 2026-10-15 確認凍結；#846 的測試擋下）。

到 2d-ii 為止，送出用的 `NATIVE_STATE_PROFILE` 和由它建出的 DTO 預設值（`exclude_if=unlabeled_profile` 的 `state_profile`）都維持未標記的 v1，v1 bytes 不動；2d-i 只把 `_Meta` 的標記、連同 profile 快照的 key 改成 v2。

1. **2d-i 擴充（API＋Hosted 一起部署）：**
   - Hosted 新 image（以下稱 H2d）帶 v2 schema，定義宣告 `foundry-native-v2`；**同時接受 `foundry-native-v1` header**，接受的 v1 wire 就是凍結的 H0 快照。
   - API（A2d）只送 v1。寫死 `"foundry-native-v1"` 的比較（`behavior_agent_versions.py` 133、338、350，`behavior_native_sessions.py` 119）同時接受宣告 v2 的定義：2d-i 起 API 的支援集合（`SUPPORTED_STATE_PROFILES`）是 {v1, v2}，送出用的 profile 到 2d-ii 都是 v1（總協調 2026-10-15，另見下面「和 Neal 決定 6 的衝突」(ii)）。
   - **A2d 服務哪些舊 image：** `HostedVersions.current` 的 `fa_build` 完全相等，改成「`9aaa0824`（2026-10-02）以後的任何 image，v1 wire 與 H0 快照只差新增、預設值時不輸出的選填欄位，而且 A2d 送給它的請求不帶那些欄位」。不能只認「與 H0 相同」：閒置的 Agent 可能還綁在更早的 image 上，不相容就只能不搬檔換走（④），工作檔就丟了；相容的照常服務，回合結束時照第 2 步切換，來源有工作檔就帶過去（Neal 2026-10-15）。
     - **SDK 開始閘門的同 build 釘選（總協調決定，2026-10-17）：** dev 發現上一項只拿掉了 `HostedVersions.current` 的 `fa_build` 相等，另一處同 build 檢查還在：模板選了 `fa-sdk-start-gate-v1` 時，API 在 capture 帶自己的 `code_sha256()`（`behavior_native_capture.py` `project_capture`），每個 Hosted image 接單前要求它等於自己的 `code_sha256()`，否則 409 `SDK_START_GATE_CODE_CHANGED`（`native_sdk_profiles.py` `check_start_capture`）。API 一換 core，所有還在舊 image 上的 Agent 每則訊息都被拒；拒絕發生在 `rid:<RID>` 之前、沒有回條，回合結束的背景切換（`_accounted` 讀到終態回條才觸發）也就不會開始，要等平台回收 VM。這個檢查原本是「API 與 Hosted 同 build」的釘選（行為規格 2026-09-25「實際 installed source hash」），不是 wire 或行為相容的依據：wire 由 profile 與 H0 快照管，SDK／runtime 版本、提示前綴與 codec 各有自己的檢查。**規則（2026-10-17 #880 審查後改寫）：** 釘選在派送時才填，不屬於任何保存的身份。(1) `submit_native_turn` 對還沒有回條的 RID，把 capture 的 `sdk_start_code_sha256` 填成這次實際收到請求的 SID（`client.target`）所在版本的 `fa_build`（就是那個 image 的 `code_sha256()`，sync 由同一 build 發布）；版本讀不到或沒有 `fa_build` 時不改，保留 capture 原有的釘選（`pinned_to`；通常是 API 自己的 build，routine intent 保存的則可能是較早 API build 的）。所有送出路徑（owner、漂移或 SID 已換的同 RID 改送、交辦與回覆、續接、routine）都經過這一處，不在呼叫端各自填。(2) 已有回條的 RID 一律原樣送回回條裡的 capture，不重填：Hosted `_submit_locked` 與 API `submit_native_turn` 都以 `submit_digest` 比對同 RID 重送，重填會變 `NATIVE_RID_CONFLICT`。(3) 保存的身份不含釘選：routine intent 保存解析時的 capture，與回條比對時先換上回條自己的釘選再比 `submit_digest`（`native_routines.py` `_same_occurrence`），所以同一 occurrence 換版本後重送不會 `NATIVE_ROUTINE_CONFLICT`，RID 與 intent 都不變（界線 7）；交辦額度的 digest（`message_material_digest`）本來就不含 capture。phase 4c「切換期間送出的訊息」第 3 步改送新 SID 時，helper 照常呼叫 `submit_native_turn`，由 (1) 依新 SID 填，不另外重填，也不改任何已保存的 capture 或 digest（原寫「helper 也照這條重填」，對 routine 與交辦已保存的 digest 不成立，作廢）。舊 image 不用改，照常服務到回合結束，之後照第 2 步切換；送出時不另觸發切換。`fa_build` 因此不只是資訊。(4) M9 接手的回條（#880 審查 P2，2026-10-18）：回條的釘選是原 image 的，照 (2) 不重填；新 SID 的 Hosted 對 `rid:<RID>` 記著 `adopted_from` 的回條（只有 Hosted 自己的 `adopt` CAS 會寫）接受這個釘選，改由自己的 `preflight` 其餘檢查（profile、提示前綴與能力目錄、SDK 版本、checkpoint codec）決定能不能在這裡跑，不相容就在原 RID 上失敗（界線 7）。可以信這個釘選，是因為接單的 image 在 `rid:<RID>` 建立前已要求它等於自己的 `code_sha256()`，而 Hosted state store 只有 Hosted 寫；Hosted 讀不到版本定義，無法自己核 `COMPATIBILITY_FLOOR`，這部分由 API 送出前的相容判斷擋（原 SID 收單時已過）。新 RID 照舊要求等於自己的 build。不改 wire。**退版注意：** 退到本 PR 之前的程式碼，routine 比對又直接用 `intent.submission.sha256`，所以已送到另一個 build 的 image、釘選被改填的 occurrence，之後的 tick 會變 409 `NATIVE_ROUTINE_CONFLICT`；往前升級沒有這個問題。舊 image 也鎖提示前綴與 profile：2026-10-18 已解決，見 §3「P1 待辦」。
   - 較早的 image 沒有快照測試：實作 PR 對每個綁定列引用的 image，以它的 `fa_build` commit 離線產生 v1 快照與 H0 比對；比不出來或不符合的，照 2026-10-02 之前的 image 處理（閘門 4）。
   - **存檔：** 照第 3 點「沒標＝v1 wire」。以 v1 header 接的單（帶 digest 的 DTO 與回條）照 v1 寫、不標，位元組與 H0 寫的相同，H0 與 A2d 都解得開、同 RID 重送不衝突。`_Meta` 標 v2（沒有 digest）；對話比對改比通道。
   - `_Meta` 標 v2 之後舊 image 讀不懂，只有宣告 v2 的 H2d 會寫（切換後，以及它新建的對話，見下一項）；嚴格掃描保證切換時舊 SID 上沒有非終態回條，舊 SID 不會再為 recovered 重入讀它，`_RECORD_READS` 也先被 `replaced()` 擋下（推論，實作 PR 測試）。
   - **不退版到 H2d 以下（總協調 2026-10-15 決定，已告知 Neal）：** 某個 Agent 的切換在 H2d image 上 commit 之後，就不再退回比 H2d 舊的 image（H0 讀不懂 v2 標記的 `_Meta`）；出問題時往前修、發新版。
     - **範圍比切換更大（#851 審查，2026-10-15）：** 不只切換後才有 v2 標記。宣告 v2 的 image 上**新建**的每個對話，`_Meta` 都標 v2：全新的 SID、第 7a 點換到的目標 SID 都一樣，不必經過切換。H0 讀這種 `_Meta` 時解碼失敗，回 409 `NATIVE_STATE_INVALID`。所以 P9（宣告 v2 的 image）部署之後，Hosted 就不支援退回 H2d 以下。API 也一樣：沒有 P3 的 API 服務不了 v2 對話，P9 部署之後 API 不支援退回 P3 以下。
     - **API 也不退回 P5 以下（#858 審查，2026-10-15）：** P5 在綁定列加的新欄位與新值（`upgrade`、`profile_unread`、`source_status="active"`、`basis="stuck_admission"`／`"files_unmovable"`／`"files_overdue"`）只在 `FA_NATIVE_SESSION_GUARD_ENFORCE` 開（P9）時才寫；較舊的 API revision（例如 release 30，或滾動更新時重疊的舊副本）以 `extra="forbid"` 嚴格解碼綁定列，讀到這種列回 409 `NATIVE_BINDING_INVALID`。所以 P9 部署之後 API 不支援退回 P5 以下；**P9 只在每個 API 副本都跑 P5 以上之後才部署。**
   - `agent` 紀錄（`_AgentSession`，`native_turns.py` 286 起，沒有 profile 欄位、嚴格解碼）在收斂前**保持 H0 讀得懂的形狀**：不加 `cut_deadline` 等新欄位，否則 H0 在 670–672 之前就解碼失敗，回的不是乾淨的 503。開放 RID 集合本來就不是這筆紀錄的欄位：它是 `fa/v1/turns` 裡另一個 `open-rids` 項目（最終形狀，見第 5 點「開放 RID 集合的現行實作」的修訂），H0 掃描回條時略過不是 `rid:` 開頭的項目，不受影響。
   - `replaced()`：H2d 帶 #824 的放寬（`pending` 目標 SID 照常讀）；收緊的一半在 phase 3（第 2 點末）。
2. **搬移（每個 Agent 各自、背景、沒有拒絕窗口）：**
   - **停用所有現行 follow 入口：** 部署時 `follow_all`（6b 提前）、送出時 `_follow_before_admission` 的「selector 動了就 follow」（4c 的一半提前）、routine 自己的換環境（`native_routines.py` 518–538）。它們都在開始等待時就寫 `pending`（`native_environment.py` 653），整段掃描、搬檔、stop 期間都拒收；selector 一指向 H2d 就又是拒絕窗口。
   - **改用目的端短切換：** H2d 帶 `NativeSessionCut` 的最小版本（U5、U7，嚴格規則、不用孤兒規則），由 API 在每個 Agent 回合結束後背景觸發。**只有來源有工作檔時才寫 `pending`（Neal 2026-10-15，見下面「工作檔」）：** 來源有工作檔時，安靜檢查後以 If-Match＝E0 寫 H0 形狀的 `pending`，在柵欄內照現行 follow 搬檔再 commit（最多約 120 秒）；寫 `pending` 前，背景工作先把目的 SID 記進綁定列 `upgrade`，所以它不是下面的「舊式 follow 留下的 `pending`」。來源沒有工作檔時不寫 `pending`：沒有要柵欄保護的步驟，安靜時以 If-Match＝E0 寫 `environment:<digest>`、直接把 `agent` 紀錄 commit 成 `ready`（H0 的形狀），做法同第 7a 點的棄用路徑（Q2）。不安靜就回 `busy`、不寫 `agent` 紀錄（只可能寫目的 session 自己的 `following:` 掃描進度，和冪等的開放 RID 集合清理）。安靜與否先由探查確認，E0 由探查回給 API（見下面「工作檔」的「探查→列檔→切換」）。掃描一次做不完時可跨呼叫接著做，E0 保留到寫 `pending` 或 commit。commit 之後舊 SID 回 409，送出照「切換期間送出的訊息」第 3 步改送新 SID。請求內等候在 2d-i 用於搬檔模式的柵欄（只等、不撤回）、舊式 follow 留下的 `pending`、404／已失效與不相容的來源；API 主動撤回（H0 不會自己撤回）用於舊式 `pending`，以及超過約 120 秒上限仍沒 commit 的搬檔柵欄（由背景工作撤回）。
   - **觸發條件（總協調 2026-10-15）：** 綁定列的 V_rec≠selector 選的版本就切，不只 v1→v2：凍結期間發布的版本（只改提示、工具或 Hosted 行為）也靠它送到各 Agent。這是 2d-i 的背景切換，開關是 `FA_NATIVE_SESSION_GUARD_ENFORCE`；phase 4 的 U0～U9（`FA_NATIVE_IDLE_UPGRADE`，第 12 點）另外開。
   - **只在共用 store 安靜時切**：舊 SID 上沒有任何 `accepted`／`started` 回條、沒有 `admitting`、chain task 安靜，而且 ETag 從 E0 到寫入 `pending`（搬檔模式）或 commit（來源沒有工作檔）沒變；不安靜就等回條做完、下一次回合結束再試。判斷看共用 store，**不看 metadata 是否 `idle`**，所以 `active` 的舊 image 也能在兩個回合之間切走。**來源 `idle` 時不切（總協調 2026-10-16）：** 不叫醒就搬不了它的工作檔，所以背景切換在建立目的 session、探查或讀檔之前就回 `source_idle`，不建 session、不碰來源（目的 session 已存在時先照常收斂它留下的舊式 `pending` 或漏記的 commit）；Agent 在下一個真正的回合結束（來源 `active`）時連工作檔一起換（同第 3 步第 4 項「閒置的 Agent 等到下一個回合結束」）。已失效（`failed`／`deleted`／`expired`／404）的來源照舊走第 7a 點。
   - **對舊 image 的要求只有 670–676**：新 RID 看到 `pending` 回 503（670–672，`9aaa0824` 2026-10-02 起）、commit 後回 409（673–676）。2d-i 的搬檔模式靠 670–672 擋住柵欄期間的新訊息（和現行 follow 相同），來源沒有工作檔時不寫 `pending`、只靠 673–676；670–672 另對舊式 follow 留下的 `pending` 有意義。
   - 比這更舊的 image 沒有 670–672，也沒有 `agent` 紀錄的接單 CAS（同樣 `9aaa0824` 起才有），與共用 store 的切換根本不相容：這種 image 上的 Agent 不在搬移範圍，由閘門 4 先列出、交 Neal 決定（例如照第 7a 點於閒置時換）。**已決定（總協調 2026-10-15）：** 沒有綁定列的 Agent 在第一次被定位時認領；它唯一的 session 跑的 image 比 H0 相容範圍舊（例如 dev C／D 09-16 的 session）時，認領成不相容，照第 7a 點棄用換到新的 SID，從不 invoke 它。
   - `replaced()` 在 `17edb75f`（2026-10-06）之前與現行都比版本號大小（`native_turns.py` 558–567），所以切換目標的版本號要大於來源（新發布的 H2d 一定成立）。
   - **工作檔：和現在一樣，來源有工作檔就帶過去（Neal 2026-10-15 最終決定）。** 現行 follow 在來源 `active` 時就搬檔（phase 0 列「`active` 時照舊搬檔」；`behavior_session_files.py` `_source` 只准 `active` 的來源），2d-i 的切換在回合剛結束時觸發，來源一定是 `active`，所以「不搬」會丟掉使用者今天保得住的檔案。做法分兩種，都不比現在差：
     - **來源有工作檔（搬檔模式）：** 安靜檢查通過後，以 If-Match＝E0 寫 H0 形狀的 `pending` 柵欄，沿用現行 follow 的搬檔階段（`native_environment_files.py` 的 inspect、list、begin、confirm、verify_source 等，manifest＋hash），搬完 commit；柵欄最多約 120 秒，與現在每次 follow 相同。搬檔模式在 commit 前照現行 follow 仍要停掉來源（stop），這段時間也算在約 120 秒的柵欄內；要不要沿用由 P5（API 端）決定。H0 的搬檔 helper（`_operation`）只在 `agent.environment` 是對應的 `pending`、而且沒有接單中或執行中的回合時才肯 inspect／verify_source，所以搬檔一定在柵欄內做。柵欄期間的訊息照現在：在請求內最多等 W（10 秒），之後回 `server_following`，工作台以同 RID 自動重送（「切換期間送出的訊息」第 4 步的搬檔模式例外）。`files_migrated=true`，通知用第 8 點「工作檔已帶過去」句。
     - **來源沒有工作檔：** 照「改用目的端短切換」一項，以 If-Match＝E0 直接 commit 成 `ready`，不寫 `pending`、不讓任何訊息等（Q2）；`files_migrated=false`，通知用第 8 點 ④ 句。怎麼在寫 `pending` 之前知道來源有沒有工作檔：H0 的 inspect 要在 `pending` 下才做，不能拿來判斷；用 API 端的 `SessionFiles` 對 `active` 的來源列檔（`idle` 的來源不切，回 `source_idle`，見「只在共用 store 安靜時切」），**順序固定為「探查→列檔→切換」（#856 審查）**：① API 先送 `NativeSessionCut` 的 `mode="probe"`，跑同一套安靜檢查，安靜就回 E0（`quiet_etag`，store 安靜時的 `agent` 紀錄 ETag），不 commit；② API 在拿到 E0 **之後**才列來源的檔；③ 再送 `mode="cut"`，帶 `expected_etag=E0` 與列檔結果 `files_required`。沒有工作檔的切換**必須**帶 `expected_etag`（wire 驗證直接拒絕沒帶的），ETag 從 E0 動過就回 busy、不 commit。原因：列檔之前已接單、列檔後寫檔、又在切換掃描前做完的回合，掃描看起來安靜，只有 ETag 能看出它（它的接單讓 ETag 前進）；若沒有這一步，直接 commit 會把它寫的檔留在舊 SID。下一次回合結束重來；回合以外的背景程序改的檔照第 6 點「已接受的殘餘」。
     - **帶不了的情況（照現在）：** 來源 `idle`、已退役（404 或 `failed`／`deleted`／`expired`）、不相容或碰不到（第 7a 點、本步「來源 404 或已失效」）時不搬，走 ④：`files_migrated=false`，通知用第 8 點 ④ 句「上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。」（Neal 2026-10-15 核准）或「舊環境已失效」句。切換後舊 SID 進之前的 SID 清單，FA 不再 invoke 它、也不經它讀檔（第 7a 點「做法」），所以這些情況留在舊環境的工作檔不會出現在新環境，phase 5 也不會補回（phase 5 的 ② 快照只涵蓋當時被換走的 session，見第 6 點 U1）；平台在 session 30 天沒有使用後刪除（Microsoft Learn：hosted-agents concepts、manage-hosted-sessions）。
     - **經過（2026-10-15）：** 本 PR 先記成「2d-i 不搬檔」，依據的兩個前提都錯了，由總協調先後更正：① 原以為 H0 期間的工作檔之後由 phase 5 補回，其實切換後碰不到舊 SID、phase 5 補不回；② 原以為「不搬」和 2d-i 之前每次部署 follow 的結果相同（`files_migrated=false`），其實現行 follow 對 `active` 的來源照常搬檔。Neal 看過兩次更正後選定本項：搬檔與否和現在一致，來源有工作檔時才寫柵欄（計畫的 Q1 因此定案），沒有就直接 commit（Q2）。原文「舊 SID `active` 時在柵欄之外用 ① 先帶過去」取消（上面 `_operation` 的限制）。
   - **停用 follow 之後的兩個退路（審查第 2 輪補）：**
     - **舊式 follow 留下的 `pending`：** 停用前最後一次 follow（部署時、送出時或 routine）可能留下沒帶 `cut_deadline`、綁定列 `upgrade` 也沒記目的 SID 的 `pending`；停用後沒有入口推進它，這個 Agent 會一直 503。
       - **預設撤回：** 接手重跑舊 follow 會一直持有 `pending` 做完掃描、搬檔與 stop，正是要消除的拒絕窗口。所以背景工作與請求內等候（第 4 步）都以這個 `pending` 的 ETag 為 If-Match 撤回（`NativeSessionWithdraw` 也接受舊式 `pending`），再照本節搬移。**更正（總協調 2026-10-15）：** 原文「撤回指令送給 `pending` 紀錄本身寫的目標 SID」不一定可行：那個目標可能跑 H0，沒有這個指令。撤回改由 H2d 的 session 執行（例如這次切換的目的 session）；舊式 `pending` 沒有存 `environment-before:<rid>`，就由 `pending.previous_*` 組出 `ready` 的環境（生命週期表後「`pending` 切換用法」）。
       - **例外才接手：** `following:` 進度顯示已到 commit 這一步，才以原 follow 指令（同一 `request_id`、同一目標，現行可重入）推進到 commit。
     - **來源 404 或已失效：** 送出時讀到綁定列目前 SID 是 404 或 `failed`／`deleted`／`expired`（相容的來源照第 7a 點 (m) 兩次讀取；第二次至少隔 5 秒，在這個請求的等候 W 之內做，總協調 2026-10-15），API **不拒絕**，在這個請求內做目的端切換（④，第 8 點「舊環境已失效」句），訊息照請求內等候送到新 session；超過 W 照現行 `server_following`，工作台同 RID 自動重送，背景工作接著做完。第 9 點最後一列已註明。
    - **實作（2d-i P6，2026-10-16；`FA_NATIVE_SESSION_GUARD_ENFORCE` 打開時才有，關閉時與 main 相同）：**
      - **請求內等候的接法：** 「切換期間送出的訊息」原寫成包住 `submit_native_turn` 的接單呼叫；但柵欄的 `pending` 多半在 API 自己讀 session 狀態時就被擋下（`admission`），根本到不了 Hosted。所以改成每個入口整個重做同一次嘗試（同 RID、同 body），等於工作台的同 RID 自動重送提前在請求內做：`NativeTurnGateway.submit`（工作台、Responses、群組、Teams 都經過它）、`NativeAgentRelayGateway` 的交辦與回覆、routine。只有沒送出的拒絕會重做；沒碰到切換的送出只做一次，呼叫與延遲不變（測試數過）。重做期間會再讀綁定列（每 500 毫秒最多一次，只在碰到切換的那一則），這是與「不輪詢綁定列」原文的差異。綁定列記著柵欄時，送出不等 sessions lock（柵欄每步持有約 10 秒），拿不到就當成還在切換、照等候重做。**代價：** 等到上限時，任何可重做的拒絕都回 `server_following`，包括 Hosted 自己 task recovery 的 503（單次嘗試原本不帶它）：請求內最多 10 秒重做、每個等候占一個 worker thread，之後工作台最多 600 秒同 RID 重送；重做不再預讀對話。④ 期間每次送出另多一次目的 session 的 `get_session`（判斷是否確定失敗）。
      - **停用 follow 入口：** 綁定的 Agent 的 `advance`（送出被拒後、`/environment/advance`、routine、Agent 間交辦的換環境）不再 follow，只做本節列的事：已寫或 metadata 現在做出的 §7a 決定（已失效：第二次讀取在同一請求的等候內、至少隔 5 秒；不相容：不論 `active` 與否立刻，照「收斂後還剩的情況」）切到選定版本的 session（同版本為 M5 新 SID）；舊式 `pending` 由 H2d session 撤回，檔案進度記了 FA 的停止（`stop_started`）才接手推到 commit；卡住的 `admitting` 由被擋的送出觸發同一個 300 秒上限。routine 不再自己 follow，等不到回 503 `NATIVE_ROUTINE_UNCONFIRMED`（同 occurrence RID）。部署的 `follow_all` 留 `verify_only` hook（只讀 metadata），P9 打開。
      - **持久的起算點：** 卡住的 `admitting`、柵欄推進的 30 分鐘暫時性上限與 5 秒永久性確認、已失效的兩次讀取，第一次看到的時間記在綁定列 `clocks`（每種一筆；切換時整列重建而清掉）。
      - **P9 前必須有的兩個界限：** ③ 已標停止的柵欄，來源 metadata 一直 `creating`／`updating`／`deleting` 或讀不到：用同一個 30 分鐘持久上限，之後照實不搬檔 commit（還在的來源用 ④ 句，`deleting` 用「舊環境已失效」），log 帶 Agent 與柵欄。④ 柵欄的目的 session 讀不到：送出等候（`server_following`）並記 `NATIVE_UPGRADE_CUT_FENCE_BROKEN`；只有確定的失敗（4xx、invoke 404、metadata 已失效）才計時，平台 5xx 中斷不算。確定失敗持久 300 秒後：沒標停止（來源沒被 FA 停、檔案還能搬）的柵欄由選定版本的新 SID（升 M5 attempt，絕不是壞掉的那個）撤回，舊 SID 帶著檔案照常接單；**只有已標停止的**才走 §7a 棄用到新 SID，不搬檔、照實通知。操作者路徑：`agent-versions fence --agent-id`（只讀綁定列）與 `--clear --confirm <同一個 ID>`（沒標停止就撤回；標了停止就先推進幾輪，推不完才照實不搬檔 commit；目的 session 壞掉同 ④，但不等 300 秒；`pending` 已不在就清記錄），Shared Context README 有 runbook 與所需權限。
      - **只做決定、沒人開口的 Agent（P8 留下的缺口）：** 照第 4 步 (b)「背景工作…切走」，巡查把寫下的決定（含之前寫了還沒切的）交給回合結束用的同一個背景切換 worker，`cut` 先執行綁定列的棄用決定再看版本；巡查本身仍只讀 `get_session`。不採「閘門把決定了但沒切的列當成不擋」：那一列仍指向 2d-i 之前的 image，第 3 步閘門的條件就沒達成。
      - **API 也不退回 P6 以下：** `clocks` 與 basis `files_overdue` 的新用法只在開關打開時寫；理由同上一段 P5。
3. **2d-ii 收斂：** API 改送 v2、改用 profile 集合判斷。部署前由第 7 點的相容閘門確認綁定列已沒有任何一列指向 2d-i 之前的 image（閘門因此提早到 2d-ii 前接上，phase 3b 的這一項跟著提前）。收斂之後的第一個 Hosted image 拿掉 v1 別名，凍結結束。**2d-ii 的 PR 要等 stage 搬完才合併（總協調 2026-10-15）：** stage 部署時的相容閘門報告（未武裝）沒有任何仍在服務的 v1 列。**改送 v2 的做法（總協調 2026-10-17，接第 3 點「v2 快照規則」）：** 把 `NATIVE_STATE_PROFILE`（連同由它建出的 DTO `state_profile` 預設值）從 v1 改成 v2，會被 `v2_drift` 擋下：v2 不是 `unlabeled_profile`，欄位變成預設輸出。離線探查（2026-10-17，`91ffd6a8` 的程式只改這個常數）得到 35 行 `state_profile: written omitted in H0, emitted now`，另有 10 行由此帶出的 `NativeRelayAcceptance` union 差異。第 31 版的 image 把沒寫的 `state_profile` 讀成 v1，所以這個預設值本身就是 wire 的一部分。在請求裡明確寫 v2、DTO 預設不動，schema 沒變，`v2_drift` 通過（v2 已在第 31 版的 Literal 裡）。2d-ii 的 PR 照後者做；要改預設就得換 profile。
4. **收斂要等的閒置 Agent（子選項，Neal 已核准（2026-10-13））：** 不設固定期限。相容又閒置的 Agent 照 Neal 決定 5 不主動搬；每個 Agent 符合下列之一，收斂就不再等它：
   - (a) 2d-i 之後有過一個回合：回合結束時照第 2 步切走，來源有工作檔就照第 2 步帶過去（Neal 2026-10-15）；phase 5 上線後是 ② 的回合結束快照。
   - (b) 它的 session 已被平台以 30 天無活動刪除（metadata 404／`deleted`／`expired`）：檔案本來就不在了，背景工作照第 2 步「來源 404 或已失效」以 ④ 切走，主人下一次開口時看到「舊環境已失效」句。
     - **怎麼發現（#823 審查第 3 輪補）：** 閒置的 Agent 沒有回合結束可觸發，所以 API 另有一個**唯讀的 `get_session` 巡查**：背景、低頻（每天一次加隨機抖動；與排程時鐘同一類 lifespan 背景 task），2d-i 部署起開、2d-ii 部署後關。每次只對綁定列目前 SID 仍是 2d-i 之前 image 的 Agent 逐一讀 metadata；**只讀 `get_session`，從不 invoke、不讀檔、不 stop**（第 10 點；`get_session` 不喚醒是 dev 觀察 ①）。讀到 404／`deleted`／`expired`（相容的來源照第 7a 點 (m) 隔數秒再讀一次確認）就照 (b) 切走；讀到 `idle`／`active` 什麼都不做（決定 5）；讀取失敗下一次再讀。2d-ii 的閘門（綁定列沒有指向 2d-i 之前的 image）靠回合結束觸發 (a) 與這個巡查 (b) 達成；沒有巡查，閒置到被平台刪除的 Agent 永遠不會被發現，收斂就完成不了。
   - 不再有「7 天後一律以 ④ 切走」，工作檔不會因為收斂等不及而丟掉：閒置的 Agent 等到下一個回合結束，照第 2 步連工作檔一起換。只有 (b) 這種來源已被刪的，工作檔本來就不在了。代價：凍結期最長約為最後一個閒置 Agent 的最後回合後 30 天，期間不改 v1 wire。這個子選項完全照決定 5，不再碰它。

**閘門清單（審查第 2 輪集中列出，#823 審查第 3 輪補 0、0a 與門檻）：** 下列都通過才部署 2d-i，兩個例外（總協調 2026-10-15）：第 7 項在 2d-i 部署到 dev 時量，擋的是 2d-i 上 stage；第 8 項不擋 2d-i，只擋開 `FA_NATIVE_IDLE_UPGRADE`（新 image 之間的閒置升級）。2d-ii 另有自己的閘門（第 3 步：綁定列沒有指向 2d-i 之前的 image）。這個閘門就是 phase 3b 的部署前相容閘門（#829）：每次部署都以新 API image 唯讀檢查綁定列與 definition、不呼叫 session；預設不打開、只報告，所以不擋 2d-i；觀察報告沒有 v1 的列之後，由 2d-ii 的 release PR 打開（`compatibility_gate_armed`），必要時用綁定單一 release、經批准的 override。2d-i 起寫綁定列要把 `state_profile` 寫成該 session 版本宣告的 profile（現行一律用欄位預設值，即 API 常數 v1），寫入點是 `claim()`（`native_binding.py` 263，核到 `origin/main` `b7ad3dfd`）、換環境的 commit `record_switch()`（322），以及沒有列時連同棄用決定認領的 `decide_abandon()`（430）；否則打開後已搬走的 Agent 也會被擋。閘門 4（舊 image 盤點）可直接讀這份報告。**2d-ii 打開閘門前另一項（#829 審查第 1 輪）：** 報告裡的 `unclaimed`（服務中、還沒有綁定列的 Agent）要先認領或退役。閘門對它們只報告、不拒絕（第 7 點只檢查紀錄的 profile；否則新 onboarding 的 Agent 會擋住每次部署），而沒有列時閘門無法不呼叫 session 就判斷它的 session 是所選版本推導的第 0 代 SID，還是 `native_session_id` 不帶版本的 SID。2d-i 起沒有列的 Agent 在第一次被定位時認領（比 H0 相容範圍舊的照第 2 步認領成不相容、棄用換新，總協調 2026-10-15），`unclaimed` 會隨使用清空。

| # | 閘門 | 怎麼確認 |
|---|---|---|
| 0 | 2a～2c 已部署（#823 審查第 3 輪補） | 2a 協定欄位（#824）、2b 棄用來源換新環境（#825）、2c 通知顯示都已合併，並在同一環境部署 2d-i 之前已部署（dev 先，stage 照 release PR 流程）；依部署紀錄核對。**已滿足：** 2a～2c（連同 phase 3、3b）先在 dev 測過第 30 版候選（[紀錄](../progress.md#dev-test-v30-1014)），再隨 [stage 第 30 版](../progress.md#stage-v30-1015)部署（2026-10-15） |
| 0a | 第 7a 點 (r)(s) 已實作（#823 審查第 3 輪補） | 2b 實作 PR 的 (r)(s) 測試照第 7a 點的做法（不帶 checkpoint 開始、釋放 slot），不是明確拒絕。**已滿足：** #825 的 (r)(s) 測試（已合併） |
| 1 | phase 3 與 M5（同版本另建）已部署 | 送出改用綁定列與 V_rec、同一版本另建 session 在 dev 通過。**進行中（dev）：** phase 3 已部署；M5 的 dev 測試經 Neal 2026-10-15 核准：刪掉一個 dev 測試 agent 的 session、送一則訊息，預期得到同版本的新 SID、對舊 SID 零呼叫 |
| 2 | API 主動撤回（背景工作與請求內等候的 `NativeSessionWithdraw`，含舊式 `pending`） | 4a／4c 的撤回測試；dev 人為中斷在 U6 之後，確認撤回收拾得了。2d-i 驗兩種：舊式 `pending`（dev 怎麼造出一個由實作 PR 訂），以及搬檔模式的柵欄中斷在搬檔途中、超過約 120 秒上限由背景工作撤回；另驗柵欄期間的送出只等 W、不撤回，之後 `server_following` 同 RID 重送 |
| 3 | v1 快照測試在部署時擋（Neal 2026-10-15） | CI job `v1-wire-snapshot`（`.github/workflows/v1-wire-snapshot.yml`，不設路徑篩選）在每個 PR 與 push 到 `main` 時跑 `test_native_profile_snapshot.py`，每個 PR 都看得到結果；`deploy-staging` 在任何部署步驟前跑同一個測試，失敗就停下整次部署，所以快照壞掉的 stage release 部署不了。不用分支保護：這個 private repo 的 GitHub 方案沒有這項功能（Neal 2026-10-15）；因此 PR 仍可在這個 job 失敗時合併，擋的是部署，不是合併。確認方式：讀 `deploy-staging.yml` 的步驟順序與該次部署 run 的這一步結果。**已接線：** 部署時擋 #845、H0 基準檔與只允許新增的比對 #846（都已合併）；每次部署仍以該 run 的結果為準 |
| 4 | 舊 image 盤點 | 唯讀讀 dev／stage 的綁定列與 definition：沒有 session 綁在 2026-10-02 之前的 image，也沒有 v1 wire 不只差新增選填欄位的 image；有的話先列出交 Neal。**PASS（2026-10-15，總協調唯讀核對）：** dev 綁定列只有 A／B，綁定的 image 都是 10-11 以後；stage C／D 沒有 session。dev C／D 沒有綁定列、只有 09-16 的 session，照第 2 步「比這更舊的 image」的決定處理 |
| 5 | 沒有遺留的舊式 `pending`（三個 follow 入口都算） | 盡力核對、不叫醒 session：上一次部署 `follow_all` 的每個 Agent 結果、送出時 follow（`_follow_before_admission`）與 routine 自己 follow（`native_routines.py` 518–538）的 API log，都是 `ready` 或沒有進行中的 follow；`pending` 在 Hosted `agent` 紀錄，只對 `active` 的 SID 讀 `session_state` 確認（第 10 點）。核對不到的（`idle` 的 SID、log 已過期）由第 2 步「遺留的舊式 `pending` 預設撤回」收拾。**PASS（2026-10-15，總協調唯讀核對）：** 沒有遺留的舊式 `pending`；上一次部署 `follow_all` 的結果都是 `ready` |
| 6 | HITL 卡片盤點 | 每種卡片列出續接是否只在原 SID 的 VM 記憶體；是的話算不閒置（第 5 點），並各有測試。**盤點與測試已完成（閘門 6 PR #844，已合併）：** 盤點見第 5 點 HITL 一項（每種都只在原 SID 的記憶體、都算不閒置）；不閒置測試 `test_native_idle_waits.py`（提問、四種批准與 Connect 卡，兩條 lane）；被留下回合的收卡與其測試（`test_native_continuation_hitl.py`、`test_native_abandon_source.py::test_gate6_*`、routine 提案在回合關閉後不寫排程）。**仍待：** 4a 上線後，對 `NativeSessionCut` 重做同一組不閒置測試 |
| 7 | 全掃描時間 | **改（總協調 2026-10-15：不另做一次性探針，從切換自己的 log 量全掃描；Neal 2026-10-15 核准在 dev 量全掃描時間）：** 2d-i 部署到 dev 時，從 2d-i image 自己的 `NativeSessionCut` log（掃描方式（只讀集合／全掃描）、掃描時間、讀到的回條數、呼叫次數）量；log 必須記下掃描方式。來源的 `complete_for` 不等於來源 SID 時是全掃描（設計前提末段）。dev A／B 的 `complete_for` 已是目前 SID，它們的切換只讀集合、量不到全掃描，所以判讀 p95 之前至少要有一筆沒有完整集合的 Agent（例如 dev 測試 agent）的全掃描樣本。**通過門檻：全掃描 p95 ≤ 10 秒**；沒過就不把 2d-i 送上 stage，交 Neal 決定。掃描可跨呼叫接著做（E0 保留到 commit），所以沒過不會讓切換永遠做不完，只是忙碌的 Agent 較難切成。數字寫進 dev 測試報告 |
| 8 | 開放 RID 集合與一次性補齊 | 4a 測試；每個 Agent 的 `open-rids` 項目 `complete_for` 是它目前的 SID（「已補齊」）。**只擋開 `FA_NATIVE_IDLE_UPGRADE`，不擋部署 2d-i**（見清單前說明）。**部分完成（審計第 8 項，2026-10-14）：** 集合維護、以 SID 與那一次持有為範圍的補齊標記與只讀集合的掃描已實作（`fa/v1/turns` 的 `open-rids` 項目是最終形狀，見「開放 RID 集合的現行實作」）；(a) 的開始執行 CAS 與 4a 的計數測試仍待 4a |

**和 Neal 決定 6 的衝突（照實說；Neal 2026-10-13 已核准）：** 決定 6 是「支援集合只放目前的 profile，不寫跨 profile 轉接」。A 有兩處遷移期的通融：

- **(i) Hosted 暫時接受兩個 profile：** H2d 的 `native_response` profile 檢查與 `NativeStateProfile` Literal 同時接受 v1（凍結成 H0 的 wire）與 v2。存在期間：2d-i 部署起，到收斂之後的第一個 Hosted image 為止；API 端始終只送一個 profile，約 51 處呼叫不必分流。**這一項放寬決定 6。**
- **(ii) API 的相容判斷暫時以「v1 wire 與 H0 快照只差新增選填欄位」代替 `fa_build` 相等：** 原文「支援集合仍只有一個 profile（v1）」**更正（總協調 2026-10-15）**：到 2d-ii 都只有一個的是 API **送出**的 profile（v1）；支援／接受的集合 2d-i 起是 {v1, v2}（宣告 v2 的 H2d 定義與綁定列都算相容）。比對方式提早換成快照；2d-ii 起變成 `{"foundry-native-v2"}`。不另放寬決定 6，但凍結期間不能改 v1 wire。
- 不算通融的：v2 image 永久讀得懂沒標或標 v1 的舊項目（第 3 點已確認，接續既有對話所需）。

**收斂後還剩的情況：平台把 session 漂移到收斂前的版本。** 部署順序擋不住平台自己把 SID 綁回舊 image。A 的做法是不等 metadata 變 `idle`（取代第 7a 點「來源不相容但仍是 `active`：只等 metadata」）：

- FA 對不相容的 SID 零呼叫；漂移前接下的回條照第 7a 點 (iii) 處理，而且 `execution_version` 不同本來就不會被執行，所以舊 image 上沒有 FA 的工作在跑（推論，實作 PR 以測試確認：舊 image 讀到新形狀的 `agent` 紀錄必須解碼失敗、不能當成空）。
- API 立刻做目的端切換（需要 M5 的同版本另建），這則訊息在請求內等候；建立新 session 超過 W 時照現行回 `server_following`，工作台以同 RID 自動重送，訊息不是「沒送出」。
- **共用 store 一直不安靜（例如卡住的 `admitting`，#823 審查第 3 輪改）：** 短的有界等待之後改走第 7a 點的棄用來源換新環境，不等操作者、不拒絕（#823 審查第 5 輪改：不再用「等到 store 安靜、最多 10 分鐘就放棄長回合」）。**來源上還在跑的長回合不必等、也不會因此被放棄**：切換後同一對話的新訊息在新 session 照常 202 接單、排在留下的回條後面，只有進度停止 N 分鐘才當成遺失（第 7a 點「(r)(s) 的競爭」）。這段等待只為讓剛寫進 `admitting`、還沒落成回條的接單有機會落成；卡住的 `admitting` 沒有回條、也沒有進度快照，進度規則管不到它，所以仍要一個固定上限：從主人第一則被擋的訊息起算（起點要持久），連同建立新 session 的時間，**總和要比工作台 600000 ms 的自動重送窗口**（`SERVER_FOLLOWING_TIMEOUT_MS`，`web_test_console/index.html` 774）**留出明確的安全餘裕**，例如不超過 5 分鐘，實作 PR 訂確切值；這段期間訊息照請求內等候、超過 W 回 `server_following`，工作台每 3 秒同 RID 自動重送，畫面顯示「助理正在換到新版本，訊息已保留，完成後會自動送出」，切換完成後自動送出。棄用路徑照第 7a 點：不 invoke、不 stop，`admitting` 不帶進新紀錄（(b)），留下的回條原樣、讀到時照 (e)，從沒開始的照 M9 接手；留一次 `NATIVE_ENVIRONMENT_SOURCE_ACTIVE_OVERDUE`。2d-i 的搬移（相容的舊 image）也用同一個上限處理卡住的 `admitting`；只是有回合在跑的相容來源照常服務，照第 2 步等下一次回合結束。

**範圍影響：**

- A 讓 2d 依賴 phase 3（送出改用綁定列與 V_rec）、M5，以及 phase 4、6 的一部分：目的端短切換、請求內等候、API 撤回、回合結束背景觸發，加上 2d-i 停用所有現行 follow 入口（第 2 步）；第 7a 點的已失效／404 來源也改由背景工作或請求內切換處理。
- 順序：2a～2c→phase 3（含 M5）→2d-i→搬移→相容閘門→2d-ii。
- 決定部分要跟著改的地方：第 3 點決定 6 一條；第 7 點 phase 2 範圍與「停用 follow 屬草案」一條；第 7a 點「只等 metadata」與逾時回報；第 8 點「不相容」「舊環境仍在運作」兩句（#823 審查第 3 輪已改成只記 log）；第 9 點 404／已失效一列（改成請求內切換）；遷移計畫表 phase 2、3、3b 三列。


### 其他修訂提案

| # | 改哪裡 | 和最高原則衝突的地方 | 核准的做法（其餘選項已刪） |
|---|---|---|---|
| M3 | 第 4 點第 6 步「V_obs＝V_rec：不一致」分支（這一回合明確失敗） | Hosted 已 commit、綁定列沒跟上時，主人的訊息被拒 | Neal 已核准（2026-10-13）：A. 新 image 的 `replaced` 409 本文帶 `current_hosted_session_id`（與 `not_dispatched`）；API 讀那個 SID 的 `session_state`、依其 `environment` 以 `record_switch` 補寫綁定列，再以同 RID 送新 SID；主人看不到。舊 image 沒有這個欄位時，API 用綁定列 `upgrade` 記的目的 SID 確認（「切換期間送出的訊息」第 3 步），都確認不了才照原文。  |
| M4 | 第 8 點通知 | 「每個對話顯示一次」讓同一件事在每個對話都出現；同一 session 相容漂移的通知對主人沒有影響卻佔聊天區；「升級中先暫停」在柵欄只有幾秒時是雜訊 | Neal 已核准（2026-10-13）：A. 見表後「M4 的 A」。 |
| M5 | 第 7a 點 (d)、(iii) 與第 9 點的限制：「漂移的正是 selector 版本算出的 SID 時無法為同一版本另建 session，期間每個回合都明確拒絕」 | A 在 dev 遇到的正是這種：等到發布新版本前，A 的每則訊息都被拒 | Neal 已核准（2026-10-13）：A. 新 SID 的算法（由目前 SID＋目標版本＋序號算出，`fa-native-session-v2`）與「同一版本另建」提前到 phase 3（M1 的 A 處理收斂後漂移時也需要它），第 7a 點與第 9 點的切換就能立刻換到同一版本的新 session。 |
| M6 | Neal 決定 7（派送意圖紀錄要做，先定義結束與過期） | 這個紀錄每次送出都要多寫一次 Table（送出路徑變慢），又會帶出結束、過期與 ETag 競爭（原問題 1、3） | Neal 已核准（2026-10-13）：A. 不建立；由 Hosted 既有的 `admitting`、開放 RID 集合與 `rid:` 回條承擔（設計前提）。  |
| M7 | phase 2 的 v2 schema 範圍（加項） | phase 4～6 若要新欄位就得再換 profile，等於再一次全部不相容 | Neal 已核准（2026-10-13）：A. phase 2 的 v2 schema 快照先放進 phase 4～6 要用的欄位與項目（見「實作階段」開頭），先不啟用。**補充（總協調 2026-10-17）：** stage 第 31 版起 v2 只允許新增預設時不輸出的選填欄位（第 3 點「v2 快照規則」），所以漏放的欄位之後仍可以這樣加，不必換 profile；新的指令（operation）、enum 值或 model，以及改、刪既有的，都要換。phase 4～6 的指令因此要在第 31 版之前先預留。  |
| M8 | Neal 決定 3 的否決怎麼存與解除（細化） | 原文「綁定列＋專案層否決表」「發布更新的版本時解除」沒有寫入者 | Neal 已核准（2026-10-13）：A. 健康檢查失敗只否決本 Agent（綁定列）；crash-loop 寫專案層否決列；否決只擋那一個具體版本，不需要解除。  |
| M9 | Neal 決定 8（無法接續的 RID 不改寫回條、請主人重送） | 從沒開始執行的 `accepted` 回條（孤兒、切換時被放過、或 SID 換掉前剛接單）其實可以在新 SID 執行；照決定 8 主人要自己重送，等於丟掉一則工作 | Neal 已核准（2026-10-13）：A. 新 SID 以 CAS 接手從沒開始的回條，主人不必重送；見表後「M9 的 A」。 |
| M10 | Neal 決定 2（② 在「切換當下」存 Blob） | 在柵欄裡存檔，柵欄時間就隨檔案大小變長（數秒到數分鐘），超過請求內等候，主人會看到「換版中」 | Neal 已核准（2026-10-13）：A. 回合結束後、柵欄之前存（樂觀快照，第 6 點）：以 E_snap＝E0 確認快照之後沒有接單，不相等就下一次回合結束做增量快照；② 為主、④ 為退路不變。**修訂（2026-10-19，5b）：** 先探查（得 E0）、再快照，E_snap 在列檔前讀、須等於 E0（U1 修訂 ①、U5）；快照或還原失敗照現行的搬檔柵欄，不走 ④（Neal 2026-10-19，決定 2 的修訂）；④ 只留給柵欄本身也搬不了檔的情況 |

**M4 的 A：**

1. 表頭改成「每個 Agent 由主人本人看到一次」；綁定列的通知待送位一般化成**通知槽**：種類＋版本，同時只留一則，嚴重的蓋過輕的（已退回＞已更新＞相容漂移）。**「這次沒帶工作檔」是通知槽上另一個不會被蓋掉的欄位**：`files_migrated=false` 一旦設定，就一直留到帶著它的通知寫出、清除為止，所以「已退回」蓋過「已更新」時，沒帶工作檔的說明不會消失。交付與清除照第 4 點末段。
2. **同一 session 相容漂移：** 照實記錄，只在工作台狀態欄位顯示（不進聊天區）；閒置升級會在下一次回合結束把它換回目標版本，換完的「已更新」通知已涵蓋；不相容的漂移照原文。
3. 「已更新、工作檔已帶過來」改成頁尾一行、不打斷。
4. 「升級中先暫停」只在請求內等候超過 W、工作台開始自動重送 30 秒後才顯示。
5. 新增退回句：「新版本 vN 執行不穩定，助理已退回 vM；剛才中斷的訊息請再送一次。」（文案待 Neal 確認；M9 已核准，從沒開始的訊息已由新 SID 接手，這句只適用已開始而中斷的）。**Neal 2026-10-19 決定用較短的版本：**「助理已退回先前的版本；剛才中斷的訊息請再送一次。」（不帶版本號；沒帶工作檔時句後接不搬檔那句，見第 8 點）
6. 「沒通過檢查」「有新版本在等」只在狀態欄位。


**M9 的 A：** API 讀到 `accepted`、`run_token` 空、`hosted_session_id` 在之前 SID 清單的回條時（第 4 點第 6 步「SID 已換」、工作台輪詢、孤兒），請新 SID 以該回條 `rid:` 項目的 ETag 為 If-Match，把執行 SID 改成自己，再照一般路徑執行（經設計前提 (a) 的柵欄；Responses 回條由 API 照第二步 POST 到新 SID）。舊 SID 開始執行時的 CAS 用的是接單時的 ETag（`native_turns.py` 1620–1622；Responses 在 1330 附近同樣以接單 ETag 擋晚到的處理器），接手後一定失敗，所以不會兩邊都跑。（#825 審查第 2 輪核第 28 版 `daa822dd` 更正：Invocations 的 `started` CAS 確實用接單時的 ETag，但卡在 execution slot 的舊回合每 0.1 秒重讀回條，看到新 SID 已開始時會走 `_reenter` 把它寫成 `unknown`；Responses 處理器在 slot 後重讀、用重讀的 ETag CAS，擋不住。所以 #825 的實作另在接手時寫入 `rid:` 項目的新欄位 `adopted_from`：舊 image 的 `extra="forbid"` 讀不了這個項目，每次讀都以 `NATIVE_STATE_INVALID` 失敗；它的寫入（`_cas`）先帶舊 ETag 送出，被換過的 ETag 擋下（審查第 4 輪更正措辭：「寫入前就失敗」只說讀）。舊回合不會開始、也不會改寫它；本 image 核回條的 SID。實作接手所有之前 SID 上從沒開始的回條，含已退役的來源（#825 審查第 3 輪，總協調決定：退役來源跑不了它，接手只有好處，也沒有晚到開始的圍欄問題）；Responses 回條由新 SID 的等待 task 以 Agent 身分請 API 送出，見共用 README〈切換後留下的回條〉。）只碰從沒開始的 `accepted`；已開始、終態、`unknown` 一律不改（界線 5、7），照第 7a 點 (e) 通知。這改寫了回條的執行 SID，所以碰到決定 8 的「原 Hosted 回條保持原樣」。

未採用的 B／C 選項（保留現行 follow 一次切換、每個對話各通知一次、另建派送意圖紀錄、各 phase 另換 profile、否決只寫專案層、照原決定 8 請主人重送、切換當下存檔等）已刪除；逐項改動清單見 #823 核准前的版本（PR 的 commit 歷史）。

這次重新檢查**沒有**衝突、維持原決定的：Neal 決定 1、4（6 小時只通知，改放狀態欄位）、5（審查第 2 輪起 M1 的子選項不設期限、完全照決定 5）；第 10 點不叫醒的規則（閒置升級的每一步都不叫醒 `idle` 的舊 SID）。有衝突、已修訂的（Neal 2026-10-13 核准）：決定 2（M10）、3（M8，細化）、6（M1）、7（M6）、8（M9）。冷啟動：U2 先建好並健康檢查新 session、U3 讀回最近對話的 checkpoint、切換前預先還原最近的對話，最近的對話下一則不比平常慢；其他對話第一則多一次 checkpoint 還原（phase 4 dev 量測）；既有的打字預熱照綁定列的目前 SID 打到新 session。

## 平台未知，以及 FA 怎麼保持安全

- 叫醒時綁哪個版本、叫醒後版本變了的情況是否等同 resume（因而 `$HOME` 照常還原）、`get_session` 是否喚醒：官方都沒寫。FA 不依賴它們：版本以 VM 自報為準、漂移由 expected-version 偵測、同一 session 的版本變化由 `version_indicator` 比對的漂移旗標偵測、以通知待送位在第一個主人本人的回應告知，`get_session` 的 `created_at` 改變（換了物件）只是順便讀到的保底訊號、讀到才告知，搬檔只碰 `active`。
- 平台若日後保證「叫醒不換版」，第 4 點的漂移分支只是不再觸發，不需拆掉。
- Neal 決定不向 Microsoft 回報這個漂移行為（總協調 2026-10-12 轉述）。

## Neal 已決定（2026-10-12 19:53）

Neal 2026-10-12 19:53（+08）回覆「都同意」，以下 8 項照當時的建議定案（編號沿用上一版「待 Neal 決定」）。第 1、6 項與第 8 項的核心落在「決定」部分；第 2～5、7 項與第 8 項的一般機制是閒置升級機制的方向（2026-10-13 設計定稿，Neal 同日要求；尚未實作）。2026-10-13 定稿輪與審查第 1 輪另有修訂（M 項），Neal 同日核准：第 2 項（M10）、第 3 項（M8）、第 6 項（M1，遷移期間有限度放寬；M1 的子選項審查第 2 輪起不再碰第 5 項）、第 7 項（M6）、第 8 項（M9）；見「對已確認部分與 Neal 決定的修訂提案」。

1. **採用 G。** 「決定」部分改為已確認；AGENTS 界線 2 改成第 11 點的措辭（本 PR 已改 AGENTS.md）。草案部分仍是草案，生命週期表的「未定」格子與已知未解問題解決後才定稿（2026-10-13 設計定稿，Neal 同日要求；M 項已核准）。
2. **工作檔：② 切換當下存 Blob，④ 不搬檔當退路。** 大小上限沿用 `fa-workspace-files-v1`（單檔 32 MiB、總計 64 MiB、最多 2000 個項目）；快照保留 30 天。（草案；快照時點見 M10）
   - **修訂（Neal 2026-10-13，M10）：** 快照改在回合結束後、柵欄之前存（樂觀快照，增量），不在切換當下；② 為主、④ 為退路不變。
   - **修訂（Neal 2026-10-19，5b）：** 快照或還原失敗時退回現行的搬檔柵欄（工作檔照樣搬，切換當下送出的訊息最多約等 5 秒），不走 ④；④ 只留給柵欄本身也搬不了檔的情況（來源已退役或不相容）。順序是先探查、再快照（U1 修訂 ①、U5）；快照途中舊 SID 變 `idle`：這次不切、不搬。
3. **crash-loop：否決＋自動退回前一個版本＋通知。** 判定收緊：切換後 30 分鐘內，至少 3 次崩潰類失敗，且分屬至少 3 個不同對話與 RID；同 RID 重試、平台關機、工具子程序被殺都不算。（草案）
4. **延後超過 6 小時：只通知，不強制升級；暫不提供「現在就更新」按鈕。**（草案）
5. **相容且 compute 已回收的 Agent：不主動升級，等它下一個回合結束後再升。**（草案）
6. **支援集合只放目前的 profile，不寫跨 profile 轉接。**（決定，第 3 點）
   - **修訂（Neal 2026-10-13，M1）：** 遷移期間有限度放寬——Hosted 暫時同時接受 v1、v2（2d-i 起到收斂後的第一個 Hosted image），API 的相容判斷暫以 v1 wire 快照代替 `fa_build` 相等；收斂後回到單一 profile。
7. **派送意圖紀錄：要做，但先定義它的結束與過期。** 定義出來之前它只留在草案（已知未解問題 1、3）。（草案；定稿輪建議不建立，見 M6）
   - **修訂（Neal 2026-10-13，M6）：** 不建立派送意圖紀錄；由 Hosted 既有的 `admitting`、開放 RID 集合與 `rid:` 回條承擔。
8. **漂移後釘在舊版本、無法接續的工作：** 標成「版本已變、無法接續」，通知主人以新訊息重送，不擋升級；原 Hosted 回條保持原樣。（決定：不改寫回條、不換 RID、通知重送，含來源碰不到時不擋棄用來源換新環境，第 7、9 點；閒置升級機制：同一 SID 漂移時怎麼判定〔原已知未解問題 9，定稿以 `refused:<RID>` 解決〕、閒置升級時「不擋升級」的一般機制；從沒開始的回條改由新 SID 接手見 M9）
   - **修訂（Neal 2026-10-13，M9）：** 從沒開始的 `accepted` 回條由新 SID 以 CAS 接手執行、不請主人重送；已開始、終態、`unknown` 一律不改，仍照原決定通知重送。

## 官方依據

- [Manage hosted agent sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)：建立時綁版、`version_indicator`、idle 與 stop 的保存、Files API（2026-10-12 讀，頁面更新 2026-09-23）
- [Manage hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#release-a-version-without-changing-production)：selector 不搬移既有 session、保留舊版本與 image（2026-10-12 讀，頁面更新 2026-09-30）
- [Hosted agents concepts](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions)：第一次使用時建立、Active／Idle／Resumed、30 天刪除、`$HOME` 保存（2026-10-12 讀，頁面更新 2026-09-29）
- [Recover long-running work after a crash](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/recover-long-running-work)：`entry_mode` 的 `"recovered"`、優雅關機時 `exit_for_recovery()` 讓回合保持進行中（2026-10-13 讀，頁面更新 2026-09-23）
- [Blob lifecycle management overview](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview)：以前綴篩選、依建立時間刪除，規則生效最多 24 小時、定期執行（2026-10-13 讀，頁面更新 2026-08-25）
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
- 2026-10-13 定稿輪另核（`origin/main` `a9f39149`）：
  - `native_turns.py`：`_AgentSession` 288–319、`replaced` 558、`session_state` 593、`_original_session` 649 起的 `pending` 拒收 670–672 與 `admitting` CAS 680 起、`_settle_admission` 703 起、`read` 839、`exit_for_recovery` 426／446／449／1577／1594／1610／1632、`entry_mode` 重入 1563–1594、`_reenter` 1392 起、`run_token` 1620、`execution_version` 463／1595。
  - `native_environment.py`：`_drain_pages` 288、`_scan_page` 325、`_tasks_pending` 435、`retarget_session` 487、`follow_session` 582 起（591 已 `ready` 原樣回傳、608 要求較新版本、653／679 寫 `pending`、743 commit）。
  - 其他：`native_binding.py` `record_switch` 215 起；`behavior_native_sessions.py` `_destination` 626 起與 `environment_creates`、`follow` 679 起；`behavior_foundry.py` `create_session` 208–240（帶呼叫端 `agent_session_id`）；`behavior_native_turn_api.py` `submit` 與 `_follow_before_admission` 594–700；工作台 `index.html` 的自動重送（774：每 3 秒、10 分鐘；1049–1052 文案；7480 起 `resendWhileServerFollows`）；`native_agent_api.py` 509 起、`native_agent_tools.py` 437 起、`native_routines.py` 524 起。
  - Learn 重讀 manage-hosted-sessions（頁面更新 2026-09-23：閒置 120～3600 秒預設 900、stop 結束 compute 保留檔案系統、stop 對進行中回合的影響、Files 上傳 50 MB）、manage-hosted-agent（2026-09-30：selector 只能 100% 指一個版本、改 selector 不搬既有 session、保留舊版本）、recover-long-running-work、Blob lifecycle management。ADR-0006「API 兼任排程時鐘」一節（API 單副本）。
- PR #823 審查第 1 輪（2026-10-13）另核（`origin/main` `a9f39149`）：
  - `native_turns.py`：`_original_session` 的 `pending` 拒收只在 `receipt is None` 時生效（670–672）；已接單回條開始執行的四條路——`_submit_locked` 1098–1109、`_enqueue_attempt` 480–484、`recover_owned` 569 起（586）、chain 執行 1599 與以接單 ETag 寫 `started`／`run_token` 1620–1622；`_recovery_error` 的 503（668–669）；profile 字串比對 855、911、1029、1285；`_Meta` 只在建立時寫 SID（1065、1277）；`_continuation_blocker` 1712 起；`conversation` 1728 起；`prewarm` 1956–1965 只列 toolbox；Responses 以接單 ETag 擋晚到的處理器（1330 附近）。
  - `native_responses.py` 155（Responses 處理器的 `_require_original_session(receipt=…)`）；`native_responses_wire.py` 10–12（`foundry-native-v1`／`foundry-responses-v1`）；`behavior_native_responses_api.py` `_send_response` 91、`_send_after_reply` 147、`submit_native_turn` 167（211 延後第二步）；`native_teams_relay.py` 166；送出呼叫端 `behavior_native_turn_api.py` 594（經 676、851）、`native_agent_api.py` 280／382／448、`native_routines.py` 598、`native_group_api.py` 94；`native_environment.py` 371–392（`check_latest_checkpoint`）與 640–690（follow 在 draining 一開始就寫 `pending`）；`native_files_omission.py` 113。
  - 程式歷史：`pending` 拒收與每個 RID 的 `admitting` CAS 都從 `9aaa0824`（2026-10-02）起；`replaced()` 於 `17edb75f`（2026-10-06）改過；`66f7bbf6`（2026-10-01）只在第一次接單寫 `agent` 紀錄。
  - 沒有讀 dev／stage 的 session 或 log；哪些 session 綁在 2026-10-02 之前的 image 未核對（M1）。
- PR #823 審查第 2 輪（2026-10-13）另核（`origin/main` `a9f39149`；PR #824 只讀說明與它對本 ADR、SPEC 的改動）：
  - `behavior_native_turn_wire.py` `NativeReceipt.original_material`（846–876：以回條自己的 `state_profile` 重建原送出並核 `submit_digest`）。
  - `native_turns.py`：`_RECORD_READS` 66–68（含 `prewarm`）與 2027 的 `replaced()` 檢查；`submit` 974、`receive` 978、`receive_routine` 984、`continue_turn` 994、`_admission_checks` 1012–1036；`_defer_enqueue` 514 起與 1115–1118；`_handle_turn` 1573 起（1580–1590 recovered 重入在 1599 的 `_require_original_session` 之前呼叫 `_reenter`）；`_reenter` 1391 起。
  - `native_environment.py` `_tasks_pending` 435–453（只看 chain task）；`native_continuations.py` `dispatch_continuation` 393 起、`prepare_continuation` 296 起；`native_agent_api.py` `continue_original` 342 起；`native_group_api.py` 89–96（群組開場以 Responses profile 送出）。
  - `native_routines.py` 515–538（routine 自己 follow）與 600–606（503 `NATIVE_ROUTINE_UNCONFIRMED`）；`native_routine_clock.py` `tick` 58 起。
  - 沒有比對 2026-10-02 以後各 image 的 v1 wire 差異；「只差新增選填欄位」是閘門 4 要核的條件，不是已知事實。
- Neal 2026-10-13（經總協調轉述）：要求「開始處理，解決未解的問題，設計定稿」；經總協調的選擇題核准 M1（含子選項不設固定期限、遷移期間放寬決定 6，與 M2 合併；同日另決定 M2 一律不可拒絕）、M3～M10 採 A。紀錄為「設計定稿（Neal 2026-10-13 要求）；M 項已核准」：機制細節沒有逐項另經 Neal 核准，phase 4～6 的實作 PR 照常審查；核准不含實作或部署。
- PR #823 審查第 3 輪（2026-10-13）另核（`origin/main` `dc9bad4b`；PR #825 head `936720b2` 的說明與它對本 ADR 的改動）：`native_turns.py` `_continuation_blocker`（1752–1765：跨 SID、`n≥1`、`base_ckpt` 空、最後回條不是 `completed` 才回 `NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE`）、Responses slot（915–925：`inflight_rid` 的回條非終態時，主人來源回 `busy_refused`／`NATIVE_TURN_BUSY`）；[CTX-03](../ctx03-checkpoint-contract.md) 第 8、9 節。#825 的 (g) 決定存在綁定列 `abandon`、`SessionGuard` 預設觀察模式、(r)(s) 目前實作為明確拒絕（本輪改成第 7a 點的做法，#825 合併 main 後跟著調整）。
- PR #823 審查第 4 輪（2026-10-13）另核（`origin/main` `dc9bad4b`）：`native_turns.py` `_allocate`（1304–1345：從上一個 `completed` 回條帶 `base_ckpt` 1326–1337、meta CAS 1340）、`_save_turn`（1566–1598：checkpoint 與 `turn:{n}` 在回條 CAS 之前寫）、`_execute_started`（1702–1706、1736–1738）、`_observe_control`（1499–1500）與 `stop_requested`（244–245）、`_unknown`（1393–1405）、`_cas`（944–961）、`close_suspended_start`（1355–1371）、`_require_original_session`（1639→673–676）；`native_responses.py` 的 slot 檢查（203–206）；工作台自動重送常數（`web_test_console/index.html` 773–774）。
- PR #823 審查第 5 輪（2026-10-13）另核（`origin/main` `dc9bad4b`）：`native_turns.py` 1639 的 SID 檢查在 `_allocate_timed`（1686）之前、`_allocate`（1304–1345）的 meta CAS（1340）前沒有 SID 檢查；`_unknown` 讀回仍是 `accepted`／`started` 時丟 503（1406–1411）；`_retain_delegation`（403–416，`started` 時也會改回條 ETag）；Invocations 接單後進 chain 佇列、佇列滿才 `busy_refused`（1110–1121）；Hosted 回合逾時 `ctx.timeout_exceeded`→`NATIVE_TURN_TIMEOUT`（1655、1711）；`native_progress.py`（`_key`、`NativeProgressRecorder.observe`／`_save`／`_flush`／`finish`：只在有公開進度事件時寫、best effort、失敗後不再寫）與 `native_progress_wire.py` `NativeProgressSnapshot`（沒有時間欄位）；`web_test_console/index.html` 773–774（`SERVER_FOLLOWING_TIMEOUT_MS=600000`、只對 `AGENT_VERSION_FOLLOWING`／`NATIVE_SESSION_CHANGED` 重送）。`last_accessed_at` 不用的依據是上面 2026-10-13 dev 唯讀證據，未另讀。
- 2026-10-15 2d-i 實作前決定輪（`origin/main` `b7ad3dfd`）：Neal 原決定 2d-i 不搬檔，總協調先後更正兩個錯的前提（phase 5 補不回、現行 follow 對 `active` 來源照常搬檔）後，Neal 2026-10-15 最終決定和現在一致：來源有工作檔才寫 `pending` 柵欄搬檔，沒有就直接 commit（M1 第 2 步「工作檔」；計畫 Q1、Q2），並核准第 8 點 ④ 新句；其餘註「總協調 2026-10-15」的是總協調依 2d-i 計畫做的決定（不退版到 H2d 以下另已告知 Neal）。另核：`native_binding.py` `claim` 263、`record_switch` 322、`decide_abandon` 430、`SUPPORTED_STATE_PROFILES` 32、`FA_NATIVE_SESSION_GUARD_ENFORCE` 454–458；`native_environment_files.py` `_operation`（7 起，25：不是 `pending` 時只准 read／observe）；`behavior_session_files.py` `_source`（125–139：只准 `active` 的來源）；`native_environment.py` `_drain_pages` 288、297（`complete_for` 等於來源 SID 時只讀集合）。閘門 4、5 的證據是總協調的唯讀核對，本工作階段沒有讀雲端；未跑程式測試。
- 2026-10-17 phase 5c 決定（PR #884）：Neal 決定快照放在新的專用儲存帳號，與 `no-auto-gc` 備份帳號分開，30 天規則因此安全；改動見第 6 點、生命週期表 ② 與 phase 5c。建立帳號與角色指派每次部署都要 Neal 批准（含 dev）。
- 實際驗證：文件變更；未跑程式測試，沒有雲端呼叫。dev 漂移數據來自總協調的唯讀整理，本工作階段未另讀 log。
- 限制：Grok Bot 行為只來自研究摘要；`get_session` 不喚醒是觀察，非官方保證。
