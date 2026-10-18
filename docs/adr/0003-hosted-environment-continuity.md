# Hosted 優先，驗收環境與工作連續性

狀態：accepted；2026-10-12「選A」一節已被 [ADR-0009](0009-version-model-g-actual-version-idle-upgrade.md) 取代｜決策日期：2026-09-14。

使用者確認：每個 agent 有獨立、可持續恢復的 Hosted 工作環境，允許底層 compute 更換，但身份、記憶、必要環境與工作必須延續。FA 維持「代管 OpenClaw」的長期助理定位，不再以長期綁定同一台 VM 作為硬性要求，也不預設新增外部 VM。

## 取代範圍

本決策取代 [ADR-0001](0001-persistent-agent-computer.md) 的 DP-02「同一台 VM」限制及依此要求獨立固定 VM 的實作假設。DP-01 的長期助理身份、DP-03 的完整必要環境與工作、DP-04 的 context 管理、DP-05 的 10 倍效能目標仍保留。以 [ADR-0002](0002-shared-context-without-file-share.md) 的共享工作狀態、Memory 與 Context API 協作，不恢復 File Share 要求。

**現行 DP-02：每個 agent 使用獨立、可持續恢復的 Hosted 工作環境；compute 可以回收或更換，身份、記憶、必要環境與工作不能因此遺失或混用。**

## 2026-10-04 Neal確認：環境接續由FA自動處理

Neal於 **2026-10-04 10:22:31（台灣時間）**問：
「這兩個功能應該是FA應該要幫user處理的? 使用者不需要處理?」
所指為stage畫面的「核對原新環境」及「檢查不搬檔接續」；
並於 **10:24:05** 回覆「對」，確認由FA自動處理。精確時間由總協調按session原紀錄提供
（UTC `02:22:31.839Z`／`02:24:05.970Z`）。

此決定取代本ADR及舊流程「不搬檔必須再問真人一次」：FA以系統政策自動核原回條，
原來源無法安全遷移時自動不搬檔接續，保存`files_migrated=false`、理由及`system_policy`依據。
舊UNKNOWN、原檔、staging、原人工決定不重寫；新目標用新決定ID，原訊息不重送。
仍在accepted／started、task或通知收尾時等待；HITL／提問由原卡片處理，不自動同意。
未核清PUT／發布及不合法checkpoint仍停止並明示，不能用系統政策放行。

總協調另確認「有界批次＋進度喚醒」：每批仍最多10次、每2秒；
達上限保持等待，不改UNKNOWN。唯讀觀察證明工作結束或原進度前進後，才續下一批同ID；
沒有進度時顯示「等待原工作結束中」，不增倒數或新gate。
送出前與畫面載入均適用；只有原確未POST草稿、原Agent/CID/登入/編輯世代仍一致時，ready後送一次。

這是UX／操作政策改變，**不是工作檔延續已完成**。自動不搬檔仍須顯示
「已換到新版環境；這次沒有搬工作檔，舊環境的檔案不會出現在新環境。」（Neal 2026-10-15 改句，原句「舊環境與檔案都保留」會讓人以為拿得回舊檔）
（P06（2026-10-06）起工作台一度不顯示；ADR-0009 phase 2c（#830）起由 API 通知槽送來，工作台在輸入框下方狀態列顯示一次，見 [ADR-0009 §8](0009-version-model-g-actual-version-idle-upgrade.md#8-使用者看到的通知)。）
正常保檔的候選先搬／selector最後切方案仍分開驗證；t4平台探針降優先，尚未執行。
新流程實作及離線範圍見[Shared README](../../src/fa-shared-context-poc/README.md#automatic-environment-policy)。

## 責任分工

### 2026-10-05補充：不依賴portal的接續觸發

Neal 18:2x原問「為什麼還需要這樣切換，FA portal 應該都自動對標到最新版?」；
本次將已批准的自動政策接到發版尾端與原交辦持久task，不要求主人逐一打開Agent。
這是FA沿原N1／follow接點自動推進，不把selector更新當作平台已遷移session；
完整工作檔延續仍未完成，t4的INFEASIBLE與原檔案／UNKNOWN限制保持。
部署MI僅操作原配置／roster的既有Agent，不因此取得新Team權限；stage發版仍依ADR-0008核准。
具體CODE/offline契約見[伺服器接續](../../src/fa-shared-context-poc/README.md#server-environment-follow)。

| 層次 | 現行方向 |
|---|---|
| 助理身份 | FA Agent／Team 的長期身份獨立於 Hosted session、程序與模型 conversation；綁定、重新配置與授權必須可追溯 |
| 助理執行環境 | 優先在 agent 專屬 Hosted sandbox 執行 Copilot SDK／CLI、shell、desktop、browser 與私人工具；SDK 工作循環尚未接上 |
| 私人環境連續性 | 固定版本 image／工具配置提供可重建基線；私人檔案、profile、必要設定與 SDK／工作狀態要有已驗證的持久保存及恢復路徑 |
| 精確共享狀態 | 延續 POC 的 Table／Context API，保存工作、owner、revision 與操作紀錄；完整版本歷史及人類批准狀態屬後續產品範圍，不以語意摘要替代 |
| 長期語意記憶 | 延續 Foundry Memory Store 的探索；不保證無損保存任意值，不能取代權威工作狀態或已發布規範 |
| 外部電腦 | 只在 Hosted 的精確契約或實測無法滿足必要能力時，再提出範圍、代價與證據供決策；不是預設元件 |

目前 POC 中的 controller VM 是歷史測試控制端，不是每個助理的工作電腦，也不是此決策要求保留的產品元件。

## 驗收界線

- Hosted 的正常 idle／resume 會更換 compute，不再因此判定架構不合格；但重用 session ID、程序 boot marker 改變或找到舊檔案，都不能單獨證明完整環境與工作已恢復。
- 必須分開驗證可寫持久目錄、browser profile、必要工具與設定、SDK 狀態及工作 checkpoint。現有 POC 的 HOME `unavailable`、GUI 暫存 profile 都不是這些項目的 PASS。
- 可由固定版本 image／可追溯配置重建的工具不要求保留所有 OS 增量；不能默默遺失工作中必要的安裝或設定。哪些狀態要保存、哪些要重建，須明列並逐項核對。
- 不要求 RAM、原 PID、socket 或所有視窗完整快照。背景工作跨程序中斷的恢復需另有 checkpoint／副作用處理，不能只把程序留在背景或發保活請求。
- Agent 生命週期不能只依賴單一 session 的保留期；session 到期、版本切換或替代 session 的資料恢復是後續契約，不宣稱本次 POC 已完成。
- 17 項共享 Context 功能驗收仍有效於原精確配置；本次需求調整不把未驗收的私人環境、SDK、自主協作或 10 倍效能改成 PASS。

後續執行順序見 [POC 分析與後續計畫](../POC-ANALYSIS-AND-PLAN.md)。本決策只調整方案，不授權新部署、支出、權限變更、刪除、發訊息、commit 或 push。

## 2026-10-03 stage缺口：暫停＋換版不等於工作檔可恢復

總協調回報stage `2026.10.03-1`：已暫停的舊來源有helper，帶來源原版本的
`session_files.inspect`仍回`NATIVE_SESSION_CHANGED`；同SID的metadata版本在重新呼叫後改變。
因此這個已觀察條件目前只能在原工作／檔案無未決狀態時，**由真人明確確認不搬檔接續**。
原檔案保留在舊SID不刪，但不會出現在新環境；工作檔延續尚未做到，`files_migrated=false`
不是本ADR的驗收通過，也不取代上方DP-02目標。來源／目的漂移的安全出口不會重播原工作。

官方建立時綁版與selector不遷移的敘述，不能替「暫停後以哪版resume」的實測差異背書。
Files list/download獨立於invocations，但目前引用文件未明確保證在暫停時不喚醒；
四欄目錄資料也不足以證明檔型／mode。來源、固定SDK及stage回報界線見
[Shared README](../../src/fa-shared-context-poc/README.md#suspended-session-file-gap)。
此批不自行新增snapshot流程或Files-only重建；已批准的唯讀探針及後續方案另行記錄，
不得先把未驗的保存／恢復主張寫成完成。

## 2026-10-04 補充：主動停止後的搬檔也有漂移缺口

總協調／fdbb回報dev `419e044b` t3：selector改版後25秒即開始follow，並非先閒置超過900秒；
流程到目的端`session_files/source_stopped`回409，原來源版本之後改變且active。
目前程式在stop後還透過Files GET讀來源，再要求其inactive，所以「Files讀取重新喚醒」是符合時序的
可能原因；現有log不足以把它擴大成「任何source invoke都必然換版」的結論。
這擴大了上方stage缺口：**操作前未暫停，仍不能保證正常換版能延續工作檔。**

本次安全出口讓來源版本已確認漂移且沒有未知PUT／發布的操作，由真人明確不搬檔接續，
保留原manifest／staging／UNKNOWN而不發布；不是恢復工作檔完成。
「production先pin、候選version_ref先搬檔、最後切selector」依官方指南另作提案，
尚未實作；還要解決API build與候選分離、封存一致性及stop後Files GET互鎖，不能只搬動`--route`。
不以此阻擋stage現有不搬檔完整測試，也不更改本ADR的必要工作檔延續目標。
來源與實作邊界見[Shared README](../../src/fa-shared-context-poc/README.md#source-drift-staging-exit)。

<a id="platform-version-drift-replacement"></a>
## 2026-10-12 Neal選A：平台把版本漂走時，自動換到新session接續（已被ADR-0009取代）

> **2026-10-12 已被取代，不會實作。** Neal 改為偏好選項G（「我比較prefer G」）：照session實際的版本執行，
> 相容就照跑，閒置時才升級。新設計見[ADR-0009](0009-version-model-g-actual-version-idle-upgrade.md)；
> 本節保留為歷史紀錄，其中綁定列、可重算SID、先預留再建立與平台狀態表由ADR-0009引用。

**狀態（原）：已確認，尚未實作。** 本節是設計契約；程式、測試與部署另走實作PR，不因本節合併而生效。

### 背景與決定

dev 10-12中午，A的session在閒置回收、再被叫醒後，平台回報的`version_indicator`是v141，
FA紀錄與目前選定的是v145；FA依設計拒絕悄悄換環境，A因此不收新回合
（[費用紀錄的10-12更正](../fa-cloud-budget-status.md#dev-cost-cleanup-1011)；progress另記過3次叫醒時換版）。
已觀察到：**呼叫一個過時的SID，平台會在那一刻用另一個版本把它重新建立**。

現行程式在這裡是死路：SID由版本算出（`native_session_id`），
`_platform_reader`讀到「v145的SID回報v141」就回409 `NATIVE_SESSION_CHANGED`；
送出改走`advance`時，`follow`的409被接住後又呼叫`_destination`，同一個SID讀回同樣的錯版，再回409。
同一個SID永遠讀不回v145，重送多少次都一樣。

Neal 2026-10-12在選項中回覆「選 A」。選項A的內容：
平台回報某Agent的session版本漂移時（叫醒的session的`version_indicator`不等於FA預期版本，
例如紀錄v145、平台說v141，或過時SID被用別的版本重新建立），FA自動在**目前選定版本的新session**接續，
對話歷史與checkpoint照常恢復，回覆明白告訴使用者
「助理換到新的執行環境，之前 VM 裡的工作檔不在了」；FA記下前一個SID；同一個request ID，不偷偷換。

這符合[AGENTS](../../AGENTS.md)界線2「從checkpoint明確接續，不偷偷換新環境」：換環境是明示、有紀錄、有通知的。
本節補上本ADR「驗收界線」所說「替代session的資料恢復是後續契約」中**版本漂移**這一種；
session過期、刪除或其他原因的替代仍是後續契約。

### 1. 什麼算漂移（觸發條件）

**唯一的權威判定是平台metadata：** 由版本V算出的SID（含下面第2點的世代SID），
`get_session`讀回的`version_indicator.agent_version`不等於V，就是漂移。
因為SID由版本算出，這個不一致本身就證明不是FA自己改的。現在有兩處會遇到：

| 位置 | 情況 |
|---|---|
| `_platform_reader`（讀取／送出取得reader） | 第2點Agent綁定列記載的目前世代SID，平台版本≠紀錄版本（也就是≠選定版本） |
| `_destination`（follow／advance建立或讀回目的） | 由同一列算出的選定版本目的SID已存在，但平台版本≠選定版本 |

**不算漂移：**

- selector改到新版、但舊SID仍回報自己的版本：這是正常跟版，照原follow流程。
- Hosted回409 `NATIVE_SESSION_CHANGED`本身。它有兩個來源：`require_expected_version`
  （VM內版本≠API帶的預期版本，在建立`rid:<RID>`前拒絕）及`replaced()`（FA自己已把Agent移到較新環境）。
  Hosted的409只提示API重讀metadata，由metadata決定是不是漂移。
  後續可在`require_expected_version`的拒絕加上VM實際版本以便診斷，但不是本設計的前提。
- metadata讀不到、401／403、逾時或5xx：照原錯誤處理，不推定漂移。
- 平台回404、session已`failed`／`deleted`／`expired`：仍走原有的退役處理，不在本節範圍。

### 2. 新session的SID怎麼產生，之後的請求怎麼找到它

- 沿用由版本算出的SID，再加**世代編號**：世代0就是現在的SID（不變，舊資料照讀）；
  世代g≥1改用**另一個雜湊領域標記**（建議`fa-native-drift-generation/v1`），再接原本的範圍材料、版本與`g`，
  結果仍以`fa-native-`開頭（`_metadata_sessions`靠這個前綴篩選）。雜湊材料的領域不同，
  所以世代SID不可能等於任何版本算出的世代0 SID。
- 用可重算的雜湊而不是隨機ID：建立後沒有收到確認時，現有的`environment_creates`與
  `FOUNDRY_SESSION_UNKNOWN`讀回都靠「重算同一個SID」找回原建立結果；
  官方SDK說呼叫端給的session ID在同一個agent endpoint內必須唯一，兩個API程序同時建立同一世代只會有一個成功，另一個讀回即可。
- 新session一律用`create_session`明確建立並帶`version_indicator=<選定版本>`，讀回SID、版本相符且為active／idle才用；
  **不靠呼叫不存在的SID讓平台順手建立**。
- 會用到這個SID的三個地方都要認得世代SID：`_scoped`的範圍檢查、`_latest_version_reader`的定位、
  `_metadata_sessions`的列表。
- 換環境沿用現有的不搬檔接續（2026-10-04的系統政策，`files_migrated=false`、`decision_basis=system_policy`），
  理由記為來源版本已變。舊SID不刪除、不再呼叫。

**Agent綁定列：目前世代記在API端，讀它不呼叫任何session。**

- 位置：API既有的Context Table（`CONTEXT_TABLE_NAME`）裡每個Agent一列
  （建議PartitionKey＝`team_id`、RowKey＝`native-binding:<agent_id>`，名稱在實作PR定案）。
  API直接讀Table，所以第5點①「不呼叫session」成立。現行`_state()`經Hosted `NativeSessionRead`讀狀態，
  只用在SID確定**之後**的核對，不用來定位SID。
- 欄位：每個版本的目前世代、目前SID與**已用過的最大世代**（只增不減）；最大世代還沒成為目前世代時，
  另記它的預留時間、已呼叫`create_session`的次數與是否作廢；最近一次替換的前一個SID、前一個紀錄版本、平台實際回報的版本、
  新SID、世代、理由`platform_version_drift`，以及觸發RID的`request_id_sha256`。
- 定位：`candidate()`、`_platform_reader`、`_destination`都先用選定版本V查這一列取得目前世代g，再算SID；
  沒有這一列或列上沒有V（現有Agent）才用世代0。所以替換之後，同一對話的下一個回合、另一個對話或非真人來源
  都直接找到新世代，不會對舊SID再判定一次漂移，也不會一路建立世代2、3。
- 寫入沿用既有owner／ETag／CAS契約（[AGENTS](../../AGENTS.md)界線5）：每次寫入都帶讀到的ETag，
  衝突就重讀再判斷，不覆寫別人寫的世代。

**替換的步驟（同一Agent同時遇到漂移，最多產生一個新世代）：**

1. 讀綁定列並記下ETag。列上V的目前世代讀回版本＝V且active／idle：直接用，不替換。
   否則先核舊SID有沒有未決或可能已派送的回合，有就明確失敗`pending_work`（第5點），不等待、不替換。
2. 找現有世代：對世代1～3逐一用metadata `get_session`讀（不invoke、不叫醒；
   不靠最多100筆、可能截斷的session列表），記下每個的版本與狀態，後面各步只用這些讀回。
   有版本＝V且active／idle的就**重用**，以CAS寫回列上的目前世代。
3. **先收尾已預留的世代，再考慮新號。** 列上V的最大世代r大於目前世代、且r沒有作廢，表示有一個預留還沒確認
   （預留者可能還在建立、建立失敗或中途停了）。這時只處理r，**不算下一個世代**；依第2步讀到r的SID狀態（狀態怎麼算見下表）：
   - 版本＝V且active／idle：以CAS把r標成目前世代並記來龍去脈，結束。
   - `creating`：視為已存在、還沒好。不再`create_session`，也不換號；回既有503 `AGENT_VERSION_FOLLOWING`
     （帶`not_dispatched: true`），由同RID重試稍後再來。
   - 讀不到（404）、預留未滿30天、且列上r的建立次數未滿3：先以CAS把建立次數加1，成功後用**同一SID**補`create_session`
     （同一endpoint內同一SID只會建立成功一次，另一邊讀回），再照第5步讀回。
   - 其他情況（下表的「作廢」各列、建立次數已滿3、預留滿30天，或`creating`超過預留時間上限）：
     以CAS把r標成作廢，編號不再用；本次明確失敗（理由`reservation_abandoned`，第7點），**不自動改用下一個世代**。
   - 讀r的metadata逾時、401／403或5xx：照原錯誤處理，不建立、不作廢。

   r成為目前世代之前，不會進到第4步。
4. 沒有可重用的世代、也沒有待收尾的預留，才建立新世代：下一個世代＝（列上V已用過的最大世代與第2步所見的最大世代）＋1；
   **每個版本最多到世代3**，超過就明確失敗並說需要發新版本（第7點）。
   先以CAS把列上V的最大世代寫成這個新世代（預留，記預留時間，建立次數記1），**成功後才`create_session`**；
   這樣即使建立後程序中止，這個編號也已記下，下一個請求由第3步收尾。
5. 建立後讀回SID：版本＝V且active／idle，就以CAS把列上V的目前世代改成它，並記上面的來龍去脈。
   讀回`creating`，或`create_session`逾時、5xx、回應遺失：預留保留不動，回503 `AGENT_VERSION_FOLLOWING`
   （帶`not_dispatched: true`），同RID重試時由第3步以同一SID收尾。平台明確拒絕呼叫端ID照第7點`create_refused`。
6. CAS輸家重讀列：列上已是新世代且讀回相符就直接用；**不用舊的讀取結果再算下一個世代**。
   列上的新世代只預留、還沒成為目前世代時，照第3步處理。

**上限怎麼算：** 每個版本最多3個世代**成為目前世代**。同一預留SID的重試只增加那個世代的建立次數，不佔新號、
不算進上限，所以兩次暫時失敗不會用掉上限；每個預留最多呼叫`create_session` 3次（含第一次），用完就作廢並明確失敗。
預留作廢後不自動改用下一號（第3步），所以用過的編號最多比已確認的世代多一個，永遠不超過3，第2步讀世代1～3就夠。
作廢只影響「目前世代又不能用」的請求：目前世代仍可用時，第1步照常直接用；作廢後要再替換，需要發新版本（主人動作）或人工處理。

**平台session狀態怎麼算：** 依固定版本SDK `azure-ai-projects==2.3.0`
（[`requirements.txt`](../../src/fa-shared-context-poc/requirements.txt)）的`AgentSessionStatus`，官方列出的狀態只有
`creating`、`active`、`idle`、`updating`、`failed`、`deleting`、`deleted`、`expired`，**沒有`provisioning`**。
本節只認下表；SDK升版或平台回傳列表外的值時，當作不明處理，不推定可用。

| 讀回 | 視為 | 做法 |
|---|---|---|
| `active`、`idle`，且版本＝V | 可用 | 重用，或以CAS標成目前世代 |
| `creating`（SDK說明：初始化中） | 已存在、還沒好 | 不建立、不換號，回503 `AGENT_VERSION_FOLLOWING`；超過預留時間上限（建議1小時，實作PR定案）仍是`creating`就作廢 |
| 404 | 不存在 | 只有第3步的預留世代、未滿30天、建立次數未滿3時，以同一SID補建立；其他世代的404只表示沒有可重用的 |
| `updating`（SDK說明：保留給日後的session re-parenting） | 不明 | 作廢：不建立、不使用 |
| `failed`、`deleting`、`deleted`、`expired` | 不能用 | 作廢：不建立、不刪除（SDK說`failed`要刪除才能恢復，刪除需另外批准，FA不自動刪） |
| 版本≠V、列表外的狀態或欄位缺漏 | 不明 | 作廢：不建立、不使用 |

「作廢」只用在第3步的預留世代。第2步讀到其他世代是這些狀態時只表示不能重用，不改列；
讀到其他世代是版本＝V的`creating`時，同樣視為已存在，不建立新世代，回503 `AGENT_VERSION_FOLLOWING`。

- 世代編號依版本只增不減：某世代30天無活動被平台刪除（讀回404）後，**不重用它的編號**，
  下次從列上的最大世代＋1，不會再用到平台已刪的ID（官方沒寫刪除後ID能否重用，見第10點）。

**Reader快取：** 快取的reader要帶綁定列的世代與ETag，列一改變就丟棄。現行`READER_IDLE_SECONDS`是24小時
（`behavior_native_sessions.py`第28行），快取內的reader不讀metadata就直接呼叫SID。本節依賴另一個修正reader快取的PR
（#813；#673是加入這個快取的PR，不是這個修正）；它合併前，快取的舊世代reader可能直接叫醒舊SID，繞過第5點①的metadata判定，所以本節的實作PR要排在它之後。

### 3. 恢復什麼、不恢復什麼

| 會接續 | 不會接續 |
|---|---|
| 對話歷史（Table／Responses原保存處）、原CID | 舊VM裡的工作檔（`$HOME`、上傳檔、工具裝的東西） |
| 每個對話的最後合法checkpoint，依[CTX-03契約](../ctx03-checkpoint-contract.md)核身份、codec與完整性後載入 | 舊VM記憶體內的狀態、程序、瀏覽器分頁 |
| Agent身份、Team資料、記憶、用量帳與原回條 | 舊session上未完成的工作（不重跑，見第5點） |

checkpoint缺件、格式不認得或不合法時，照CTX-03拒絕並保留最後合法狀態，不用空歷史開新session頂替。

### 4. 使用者看到的通知

- 句子固定為：「助理換到新的執行環境，之前 VM 裡的工作檔不在了」。
  這和2026-10-04一般換版的「舊環境與檔案都保留」不同：漂移時舊SID已被平台用別的版本重建，
  檔案是否還在無法保證，所以不說「保留」。（2026-10-15 起一般換版也不再說「保留」，改為「已換到新版環境；這次沒有搬工作檔，舊環境的檔案不會出現在新環境。」，Neal 核准。）
- 出現兩處：該回合回覆的頁尾，以及該回合的狀態欄位（API回條帶替換資訊：前一個SID、新SID、理由、`files_migrated=false`）。
- 每個對話在替換後的第一個回合顯示一次：判斷依據是該對話上次執行的SID屬於被漂移替換的前一個SID，
  不另建「已通知」紀錄。

### 5. 同一個request ID怎麼處理

- 只有兩種情況可以把同一個RID送到新session：
  ① 在任何呼叫前就由metadata讀到漂移（`_platform_reader`只讀第2點的API端綁定列與平台metadata，不呼叫session）；
  ② Hosted的拒絕明確帶`not_dispatched: true`（在建立`rid:<RID>`前拒絕）。
  ②依賴**漂移後那台VM的image**本身支援`require_expected_version`與`not_dispatched`
  （`native_turns.py`第1059～1060行在建立`rid:<RID>`前的拒絕加上`not_dispatched`）；
  平台可能用較舊的版本重建VM，舊image不一定有。409沒有帶`not_dispatched`時一律當作可能已派送，
  明確失敗（理由`dispatch_unknown`，第7點），不送新session。
- 逾時、5xx、回應遺失或沒有`not_dispatched`的錯誤：**不送新session**，原RID照界線7核原回條，
  可能已送出的回合絕不重送。這個RID留在舊SID依界線7核回條；在它讀到終態（或人工處理）前，
  之後的請求依下面的`pending_work`明確失敗，不替換。
- 送達方式沿用現有「伺服器跟版」：送出時偵測到漂移，API在同一個請求內完成替換
  （原`advance`有界一次），回409／503並帶`server_following: true`、`not_dispatched: true`與同一RID；
  工作台既有的同RID `submit-retry`把它送到新session，Hosted的`rid:<RID>`建立或讀回保證只派送一次。
  不新增同步長請求，也不由API自己另開重送迴圈。
- 舊session可能還有未決或可能已派送的回合（accepted／started／UNKNOWN的工作、HITL或提問）：
  **不等待、也不替換**，明確失敗（`NATIVE_ENVIRONMENT_DRIFT_REFUSED`，理由`pending_work`），
  工作台顯示原因與卡住的RID；不重送、不重跑、不改成功或失敗。
  - 判斷依API端既有的接單／回條紀錄（送往舊SID、還沒有終態回條的RID，以及未決的HITL／提問），不呼叫舊session；
    紀錄不足以判斷時當作「可能有」，同樣`pending_work`。
  - 解除：原RID經界線7的既有核回條流程讀到終態，或由人工處理；之後的請求才會觸發替換。本節不自動放行。
  - 這只適用於版本漂移：漂移代表舊VM已被重建，等待可能永遠等不到。2026-10-04自動政策
    （本ADR開頭「仍在accepted／started……時等待」）在一般換版仍照舊。

### 6. 非真人回合

Agent間交辦、排程routine、Teams relay都走同一個`current()`與同一套規則：判定、世代SID、
只送確定未派送的RID、一個回合最多替換一次。通知記在**收件Agent**該回合的狀態與對話上，主人在工作台看得到；
寄件方收到的仍是原回條。各來源既有的「收件人換版中」或同RID重試照舊，不另開新的重試機制。

**中斷後接續④不在此列。** README記載④不接受沒有manifest的ready做自動跨SID恢復；
漂移替換是`files_migrated=false`、沒有manifest，所以被中斷的舊回合仍停在`NATIVE_CONTINUATION_WORKSPACE_REQUIRED`，
不因本節自動在新session重做。

### 7. 上限與失敗

- 同一個RID最多觸發一次替換。替換後新session讀回又漂移，或送同一RID時又遇到漂移，
  就明確失敗（理由`repeated`；建議新代碼`NATIVE_ENVIRONMENT_DRIFT_REPEATED`，帶`not_dispatched: true`、**不帶**`server_following`），
  工作台顯示原因、不自動重送。
- 送出後沒有確定未派送（逾時、5xx、回應遺失，或Hosted的409沒有`not_dispatched`）：明確失敗（理由`dispatch_unknown`），
  原RID照界線7核原回條，不送新session（第5點）。
- 同一版本世代已到3：明確失敗（理由`generation_limit`），不無限建立session（每個session都計費）；
  失敗訊息要直接說「需要發新版本（主人動作）」，發新版本後由新版本的世代0接續。
  上限算成為目前世代的世代，同一預留SID的重試不算（第2點「上限怎麼算」）。
- 預留的世代作廢（同一SID建立3次仍沒成功、預留滿30天、`creating`超過時間上限，或讀回不能用／不明的狀態）：
  明確失敗（理由`reservation_abandoned`），不自動改用下一個世代；訊息說需要發新版本或人工處理。
- 舊session可能還有未決或已派送的回合：明確失敗（理由`pending_work`，見第5點），不等待、不替換。
- 建立新session被平台拒絕（例如不再接受呼叫端ID）：明確失敗（理由`create_refused`），不改用平台自動產生的ID，
  因為那樣沒有收到確認時找不回原建立結果。

### 8. Log與遙測

沿用現有`NATIVE_ENVIRONMENT_*`的warning格式，只記識別與版本，不記訊息內容、header或秘密；RID只記`request_id_sha256`。

| 標記 | 欄位 |
|---|---|
| `NATIVE_ENVIRONMENT_DRIFT_DETECTED` | `agent_id`、位置（reader／destination）、紀錄SID、紀錄版本、平台版本、選定版本、觸發訊號（metadata／hosted_409） |
| `NATIVE_ENVIRONMENT_DRIFT_REPLACED` | 前一個SID、新SID、世代、版本、`files_migrated=false`、`request_id_sha256` |
| `NATIVE_ENVIRONMENT_DRIFT_REFUSED` | 理由（與第7點逐項相同）：repeated／generation_limit／reservation_abandoned／pending_work／dispatch_unknown／create_refused |

Hosted的`FA_NATIVE_HTTP`已記VM自己的`agent_version`，可與上表對照。

### 9. 必要測試（實作PR）

離線替身與真平台證據分開；離線通過不代表平台漂移已在dev驗收。

- `test_native_source_drift_exit.py`／`test_native_source_no_invoke.py`：reader與destination兩處漂移都只讀metadata、零舊SID呼叫；
  先在現行程式重現409死路（RED），再驗世代1建立、讀回、Agent紀錄的前一個SID與理由。
- `test_native_automatic_environment.py`／`test_native_environment.py`：替換沿用系統政策不搬檔；
  舊session有accepted／started／UNKNOWN、HITL或提問（或API紀錄判斷不了）時回`pending_work`，不等待、不替換、不重送。
- `test_native_server_follow.py`／`test_native_turn_api.py`：metadata漂移與`not_dispatched` 409才回`server_following`；
  逾時、5xx、無`not_dispatched`不送新session；同RID重送只派送一次。
- 綁定列：替換後下一個回合與另一個對話都由列找到新世代、零舊SID呼叫、不再建立世代；讀列不呼叫任何session；
  已有版本相符的世代時重用不建立；兩程序同時漂移只產生一個新世代，CAS輸家重讀後沿用。
- 世代SID：`_scoped`、`_latest_version_reader`、`_metadata_sessions`都認得；兩程序同時建立同世代只成功一個；
  建立後沒有收到確認時以重算SID讀回；世代3上限與重複漂移的明確失敗。世代SID不等於任何版本的世代0 SID；
  預留後才建立、404後不重用編號、只預留的世代以同一SID補建立；漂移後VM的409沒有`not_dispatched`時回`dispatch_unknown`。
- 預留收尾：有未確認的預留時先收尾、不算下一個世代；同一SID兩次暫時失敗後第三次成功，世代仍是同一號、不碰上限；
  建立3次仍失敗、預留滿30天或讀回`failed`／`updating`／列表外狀態時作廢並回`reservation_abandoned`，不建立下一號；
  預留SID讀回`creating`時不建立、回503 `AGENT_VERSION_FOLLOWING`。
- 選定版本正常換版（舊SID仍回報自己版本）不被當成漂移。
- 非真人：交辦收件、routine、relay各一案，通知記在收件回合；④在漂移替換後仍回`NATIVE_CONTINUATION_WORKSPACE_REQUIRED`。
- checkpoint：新session載入最後合法CTX-03 checkpoint；壞checkpoint照原碼拒絕，不以空歷史接續。
- 工作台：頁尾與狀態顯示Neal原句一次，一般換版仍顯示原句。
  （P06 `f56020a5`（2026-10-06）拿掉了這個顯示；ADR-0009 phase 2c（#830）補回：換環境 commit 在 API 通知槽記下這句，隨主人的送出回應送到工作台、在輸入框下方狀態列顯示一次（每個 Agent 一次，不是每個對話），見 [ADR-0009 §8](0009-version-model-g-actual-version-idle-upgrade.md#8-使用者看到的通知)。）

### 10. 平台上仍不知道的事，以及平台改變時FA怎麼保持安全

官方文件（2026-10-12重讀）寫的是：session建立時綁一個版本，帶`version_indicator`就固定該版本，
之後發布新版也不變；`create_session`可帶呼叫端選的`agent_session_id`，在同一endpoint內必須唯一，不給就自動產生；
session在第一次使用時建立；同一個session ID再次被引用時，平台配新compute並還原保存的狀態；
30天無活動永久刪除；刪除session「已刪或不存在」都回204。

文件**沒有寫**：閒置後叫醒或重新建立時用哪個版本；不存在或已過期的SID被呼叫時以什麼版本建立、
`$HOME`會不會帶過去；刪除或過期後同一個ID能不能重用。平台沒有公開的「版本漂移」契約，
本節的觸發依據是dev實測，不是官方保證（同本ADR 2026-10-03所說，綁版敘述不能替叫醒後的實測差異背書）。

FA的安全來自自己的檢查，不靠平台行為不變：

- 每次判定都以metadata版本相等為準，Hosted的預期版本header在VM內再擋一次；平台不再漂移時這條路徑不會觸發。
- 平台若開始拒絕呼叫端ID、或世代SID也一直漂移，就落到第7點的明確失敗，不退回悄悄換環境。
- 不刪除或重用舊SID，世代編號只增不減；舊SID上的回條與UNKNOWN原樣保留。
- 若官方日後公布漂移契約或提供不漂移的保證，先比對本節再調整，不直接沿用舊假設。

### 已決定（總協調依Neal授權，2026-10-12）

- 舊session可能還有未決或已派送的回合時：不等待、不替換，明確失敗`pending_work`（第5、7點）。
- 每個版本最多世代3；用完就明確失敗，訊息說需要發新版本（主人動作，第7點）。

## 官方依據

- [Hosted 隔離、session 保留與 compute 生命週期](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Session 與 conversation 的區別及路由](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Runtime contract、SIGTERM 與持久 checkpoint 的責任](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)
- [Copilot SDK 的 CLI runtime 與工具整合](https://github.com/github/copilot-sdk)
- [先發布候選、不改production的版本流程](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#release-a-version-without-changing-production)（2026-10-04重讀；session綁版的文件主張不覆蓋本次漂移實測）
- [Python SDK `create_session`的呼叫端`agent_session_id`與`delete_session`回204](https://learn.microsoft.com/en-us/python/api/azure-ai-projects/azure.ai.projects.operations.agentsoperations?view=azure-python)（2026-10-12讀；漂移替換的世代SID依此）
- [明示建立session並綁具體版本](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#create-a-session-explicitly-advanced)、[session第一次使用時建立、Resumed與30天刪除](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions)（2026-10-12重讀）

官方文件於 2026-09-14 重讀；支援契約不等於此專案已實測通過。
