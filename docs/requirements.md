# FA 需求基線

更新日期：2026-10-02（FR-09 依 Neal 在 #432 的決定拿掉全域容量上限）；2026-10-01（依 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md) 修訂 FR-02、FR-05～07、FR-09、FR-12、FR-15、FR-19、FR-20；其餘沿 2026-09-15）。此文件區分使用者已確認需求、POC 已覆蓋範圍與尚未驗收的產品能力。程式基準為 M2／M3 的 `f097fa6`；M4 已批准、實作中，新增行為需求接在本批 M4 之後。

需求索引版本：`2026-09-15-behavior-1.2`。FR-01–FR-34 的定義與編號保持不變；FR-27–FR-34 是本次規格分支增量，不是 f097fa6 已包含的已驗能力。

## 0. 整體設計原則

**FA 是代管的 OpenClaw：有長期記憶的 AI 助理，優先跑在獨立的 Hosted sandbox，保有可持續恢復的完整必要環境與工作，並以快一個量級為目標。** 長期助理不等於永久保留同一台 VM，也不等於只保留聊天紀錄。

2026-09-14 使用者明確確認 [ADR-0003](adr/0003-hosted-environment-continuity.md)：允許底層 compute 更換，驗收身份、記憶、必要環境與工作的連續性。它取代 [ADR-0001](adr/0001-persistent-agent-computer.md) 的同一 VM 限制，不取消持久環境、context 品質或 10 倍效能目標。

2026-09-15 使用者要求把 Grok 對標行為提案升格為規格並納入後續開發；[ADR-0004](adr/0004-agent-behavior-control.md)與定點修訂 F15–F17 後的[行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)承接此決定。其餘 delta 待辦仍開放；它不擴張正在執行的 M4 批次，也不把研究者觀察的 Grok 靜態程式當成 FA 運行保證。

2026-10-01 Neal 確認 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md)：每個 Team 只屬於一位使用者，同 Team Agent 互信、不同 Team 完全隔離；每個 Agent 一台 Hosted VM，回合在 VM 內完成，API 只管入口與去重、結束時存檔、跨 VM 傳話與組織資源。下表標「ADR-0006」的條目依此修訂；方向確認不是實作完成或部署批准。

## 1. 產品定位

| ID | 已確認需求 |
|---|---|
| FR-01 | FA 是產品名稱；首版服務單一組織，不從第一天提供多客戶 SaaS。 |
| FR-02 | 層級為組織工作空間 → Teams → Agents；不增加另一個也叫 FA 的中間管理層。ADR-0006：每個 Team 只屬於一位使用者（Team 主人），不由多位使用者共用。 |
| FR-03 | 長期目標為通用數位員工；首版涵蓋雲端方案研究、架構規劃、真實 POC 驗證與投影片製作。 |
| FR-04 | FA 提供 Web 工作台，每個 Team 有自己的 workspace，agents 在其中協作。 |

維持 Foundry Hosted Agents 作為優先 runtime、GitHub Copilot SDK 作為 harness；M2 文字工具循環、同 SDK 跨 process checkpoint 恢復及交接已通過，自主 UI 缺口仍保留。2026-09-13 的 [ADR-0002](adr/0002-shared-context-without-file-share.md) 已以共享 context 取代 File Share。外部 VM 不是預設元件；只有 Hosted 的必要能力缺口有精確證據時才另提方案。

## 2. Team 與 Agent

| ID | 已確認需求 |
|---|---|
| FR-05 | 可建立多個 Team；每 Team 包含多位 agents。ADR-0006：每位使用者擁有自己的 Team（第一版每人一個）；Team 主人可 onboard 自己的 agents，數量上限由組織設定、Team 主人可在範圍內調低。 |
| FR-06 | 每個 agent 只屬於一個 Team。角色／skills 範本可重用，但身份、私人環境與私人記憶不跨 Team 共用。ADR-0006：同 Team 的 agents 互信，可讀寫同 Team 的共用記憶與成果；不同 Team 完全隔離，由 API 轉送點依 agent 身分限在 Team 範圍。 |
| FR-07 | Team Lead 自主拆任務、分工、追蹤與整合；專家 agents 可直接溝通與交接，不必每句經過 Lead。ADR-0006：跨 VM 傳話由轉送點核對 sender 與收件人同 Team；sender 由身分決定、不由模型自稱，跳數與則數上限保留。 |
| FR-08 | Lead 不因角色而繞過權限、支出或破壞性操作的批准。 |
| FR-09 | 比照 Grokbot：同一個 agent 的多個對話依序排隊，不同 agent 可以並行；**沒有全域同時執行上限**（Neal 2026-10-02 20:57 在 #432：「同意建議：不加全域上限，比照 Grokbot，FR-09 的「超過排隊」先拿掉。」）。原「第一版 4 個 agent 同時執行；超過排隊」的容量目標已拿掉。這也不是可建立的 agent／Team 總數限制。 |

「直接互動」指 FA Team 內 agents 之間的協作，不代表每個 agent 都需要獨立 Microsoft Teams 帳號或人類直聊入口。

## 3. 個人電腦與工具

| ID | 已確認需求 |
|---|---|
| FR-10 | 每個 agent 有獨立 Linux Hosted 工作環境；目標為 SDK／CLI、desktop、browser 與私人工具在同一 agent sandbox 執行。模型服務與受控 connectors 可在外部。首版不要求 Windows／原生 Office。 |
| FR-11 | 完整必要環境與工作可持續恢復：已核准工具／相依版本、設定、檔案、browser profile、長期記憶與工作進度須有明確保存或可重建契約。不只以 `$HOME` 舊檔或 GUI 啟動作證；不承諾 RAM、原程序與視窗完整快照，登入仍受憑證有效性限制。 |
| FR-12 | 允許 Hosted compute 回收／更換，不要求同一台 VM；FA Agent 身份、私人／Team 邊界、必要環境及工作須延續。ADR-0006 對應：一個 agent 在同一 Hosted 版本只有一個 Hosted session（一台 VM）；一個 conversation 對應該 VM 內的一個 SDK session，歷史與 checkpoint 分開保存。新對話只在原 worker 開新 SDK session；VM 閒置回收或換版時，各對話從最後合法 checkpoint 在新 VM 恢復，換版沿用「在新環境接續」的明確確認。 |
| FR-13 | 目前先確認桌面／瀏覽器及工具可用；人類觀看、連線及接手移至後續。 |

未來接手需求保留：明確交還控制權；接手期間停止 agent 新操作，不偷偷終止既有程序或部署。目前不因此要求 VNC、RDP 或串流 UI。

## 4. Team 共用空間

| ID | 已確認需求 |
|---|---|
| FR-14 | 共用空間包含 memory、SOP、skills、team topology、文化與工作成果。 |
| FR-15 | 透過共用 Context API／工具分享工作狀態、Team 記憶、必要討論與成果；每個 agent 按工作組裝本輪 context。不再要求各 agent OS 上有真正共同掛載目錄；此變更由 ADR-0002 取代原 File Share 要求。ADR-0006：Hosted 經 API 薄轉送讀寫自己 Team 的資料，API 只核 agent 所屬 Team（ETag／CAS、不提供刪除），不再每步做權威核對；語意記憶（Memory Store）同樣經 API。 |
| FR-16 | 精確共享狀態、成果與語意記憶分開。Table 保存工作與 operation receipts，Memory Store 保存語意知識；M3 已新增專用 Blob 成果及不可變版本，不以 Memory 或原 tiny artifact text 代替交易狀態、一般檔案及版本生命週期。 |
| FR-17 | 每份成果同時只有一位編輯者；明確交接，其他 agent 可讀、評論、提修訂；保留草稿與發布版本。 |
| FR-18 | 工作記憶可自動累積；改變行為的 SOP、skills、角色／組織設定須人類批准後版本化發布。 |
| FR-19 | 所有共享寫入工具與資料存取路徑都要遵守已發布內容的保護；私人工作目錄、直接資料存取或記憶更新不能繞過批准發布。ADR-0006：已發布內容對 agent 只讀，只由 API 在 Team 主人批准後寫入。 |

## 5. Connector 與外部操作

| ID | 已確認需求 |
|---|---|
| FR-20 | 組織維護 connector catalog、實際 connections、帳號與權限；Team 在被授權範圍內自行啟用／停用。ADR-0006：模型由 VM 以 Agent 身分直連 project endpoint，用量隨回合結束回報入帳；有共用秘密的 connector 留在 API broker，秘密不進 VM；代表使用者的對外動作照舊走批准與回條。 |
| FR-21 | 首版必備 Teams、Web Search、Azure POC 操作能力。 |
| FR-22 | Slack、Discord、Outlook、Gmail、X 等保留擴充方向，不屬於首版全部交付承諾。 |
| FR-23 | 真實雲端 POC 先限制在 Azure 專用測試範圍；研究可以跨雲。 |
| FR-24 | 每個 POC 先批准計畫及支出範圍，範圍內自主執行；越界、新增權限、刪除資源再確認。 |
| FR-25 | 預估費用不等於硬上限；Team 不因選 connector 或改走 browser 而擴大存取權。 |

## 6. 基礎 POC 與產品首版不是同一階段

依 [POC 結果](../POC-RESULTS.md) 與 [機器摘要](POC-GATE-RESULTS.json)，共享 Context 已完成 17 項功能 gate；它是合成資料的能力驗證，不是完整助理。原 SMB G0 保持 BLOCKED、G1/G2 依賴項未完成，且已停止此方向，不再是協作方案前置條件。

原 17-gate 功能 PASS 不涵蓋歷史 session 上限、預算符合性或後來 M1–M4 的驗收；各批結果獨立保存。兩個指定測試 sessions 不取代當時 FR-09 的四 agent 容量目標（該目標已依 #432 拿掉，舊結果不改寫）。[後續計畫](POC-ANALYSIS-AND-PLAN.md)不得將任一批既有批准外推為新增部署、支出或刪除授權。

| 需求 | POC 覆蓋與缺口 |
|---|---|
| FR-10／FR-13 | M2 SDK 文字工具循環通過；自主 UI 仍 PARTIAL，人類接手未實作 |
| FR-11／FR-12 | M1 指定私人環境／profile 恢復與 M2 同 SDK 跨 process 接續通過；SDK 跨替代 session／版本、完整 HOME 與任意工具升級未因此完成 |
| FR-15–FR-17／FR-19 | Context API、Table CAS／owner、M3 Blob 不可變版本與真人下載已有結果；通用檔案生命週期及正式授權治理未完整實作 |
| FR-18 | M3 真 SOP 版本批准／拒絕與後續 consumer 採用已通過；agent `publish` 仍非人類批准，多人審批／撤銷與通用 skill 執行不外推 |
| FR-26／NFR-01／NFR-02 | M4 五案已批准、實作中／未雲端驗收；一般記憶治理、效能對照及 10 倍目標未完成 |

完整 Web 工作台、Teams 路由、正式身份治理、業務工作鏈及投影片品質尚未完成；M3 只證明明列的成果與 SOP 閉環。完整規格仍有[待決事項](specification.md#5-尚待決策)，不能把 M3 `core_e2e_pass=true` 擴張為全部歷史測項 PASS。

## 7. 長期記憶、Context 與效能

| ID | 已確認方向與驗收界線 |
|---|---|
| FR-26 | 助理的長期記憶與工作狀態跨任務、對話、程序重啟及 compute 更換延續，不依賴單一模型 conversation 或一直不清空的 context。私人與 Team 記憶維持既定權限界線；語意抽取不是任意值的無損保存。 |
| NFR-01 | 整體工作體驗以比 OpenClaw 快一個量級（10 倍）為目標；必須以可比較任務、環境、模型與品質門檻量測。基準版本、工作集、主要統計指標與門檻尚待定案，目前沒有 benchmark 結果。 |
| NFR-02 | Context 管理是核心能力；本輪使用相關且有界的記憶／狀態，不無限制累積所有歷史。長期使用仍須能找回必要資訊、維持任務正確性與工作連續性，不能只縮短 prompt 便宣稱改善。 |

效能驗收須涵蓋端到端完成時間，並拆看啟動／恢復、context 準備、模型及工具時間；不能只用首次回字變快宣稱整體快 10 倍。具體量測設計見 [specification.md](specification.md)。

## 8. 行為控制增量

以下納入後續開發，不回改本批 M4 案例／上限；具體狀態、Interface 與驗收以[行為控制規格](FA-AGENT-BEHAVIOR-SPEC.md)為準。

| ID | 已納入的行為需求 |
|---|---|
| FR-27 | 接單、工作完成與最終交付分開；每個需回答的交辦有可追蹤交付責任，不因只 ack、模型停止或內部背景工作完成而消失。 |
| FR-28 | 已授權、低風險的細節預設自主處理；重大或不可逆結果、真正無法消除的歧義、僅使用者知道的資訊才阻塞詢問，不放寬 FR-08／FR-24。 |
| FR-29 | 長工作具有持久執行歸屬、接續點、完成事件及取消語義；主入口可接收與查狀態，未知舊執行者不被任意替換或釋放容量。 |
| FR-30 | 人類、routine、agent、工具與系統事件具可驗來源，進入正確的 logical conversation／work；連續對話不混用私人資料或依賴永不壓縮的 history。 |
| FR-31 | 一般工作記憶／User Model 有來源、scope、審核、更正、到期及撤銷；精確進度、批准、版本及操作回條不由語意記憶代替。 |
| FR-32 | Silence 是合法但有理由的結果；不隱藏正在等待的交付、批准或失敗，agent 間不以 receipt 觸發無限互答。 |
| FR-33 | 輸入、操作及交付都有穩定識別與核對機制；重送不製造重複效果，結果不明明示並停止盲重試，不承諾任意外部 exactly-once。 |
| FR-34 | 本輪 context、摘要、skills／tool catalog 有版本、來源與有界預算；優化 prefix／工具載入不延遲撤銷、不丟必要約束、不以未知 tokens 冒充節省。 |
