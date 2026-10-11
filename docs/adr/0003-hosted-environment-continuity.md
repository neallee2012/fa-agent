# Hosted 優先，驗收環境與工作連續性

狀態：accepted｜決策日期：2026-09-14。

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
「已換到新版環境；這次沒有搬工作檔，舊環境與檔案都保留。」
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

## 官方依據

- [Hosted 隔離、session 保留與 compute 生命週期](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Session 與 conversation 的區別及路由](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Runtime contract、SIGTERM 與持久 checkpoint 的責任](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)
- [Copilot SDK 的 CLI runtime 與工具整合](https://github.com/github/copilot-sdk)
- [先發布候選、不改production的版本流程](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent#release-a-version-without-changing-production)（2026-10-04重讀；session綁版的文件主張不覆蓋本次漂移實測）

官方文件於 2026-09-14 重讀；支援契約不等於此專案已實測通過。
