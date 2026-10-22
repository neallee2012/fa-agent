# Grokbot 模式：以團隊為信任邊界

狀態：accepted（Neal 2026-10-01 09:27 確認兩個前提；09:30 補充「每個 Agent 一台 VM」。09:35 決定閒置逾時 3600 秒；10:00 決定團隊資料採 API 薄轉送。本 ADR 是方向決策，不授權部署、新 IAM 或支出；T1～T5 已核准動工）｜決策日期：2026-10-01。

## 背景

使用者原話：「每個使用者可以建立自己的Agent團隊，每個agent自己獨立一台microvm，且每個user的agent團隊應該要是完全區隔開來的才對?所以是否可以遵循grokbot的模式?」

Neal 確認了兩個前提：

1. 一個 Agent 團隊只屬於一位使用者，不會有多位使用者共用同一個團隊。
2. 同一團隊內的 Agent 互相信任，可以讀寫同團隊的共用記憶和成果；不同使用者的團隊之間完全隔離。

09:30 補充：「在VM沒有被收回的情況下，新對話為什麼還需要冷啟動?」

**現況的問題**：
- Hosted 每一步都回 API 做完整的權威核對。live 的第 2 回合中，admission、claim、acquire、restore 依序讀取約 610 批，花了約 5 秒（[ADR-0005 PoC 結果](0005-long-running-conversation-worker.md#poc-結果2026-10-01)）。
- 每個對話各綁一個 Hosted session（一台 microVM），所以新對話一定要冷啟動。

Grokbot 的做法（外部唯讀調查）：入口驗一次身分，之後整個回合都在 host 內完成；同一個 Agent 的訊息依序排隊；Agent 之間傳訊由 host 綁定 sender。

## 實作原則（Neal 2026-10-01 再次強調，所有切片適用）

1. 原話：「我要把目標再對齊一下，請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。」
2. 原話：「「SDK-first／Hosted 優先」原則，在 FA 自己建一層之前，應該先確認官方功能跟最佳實踐，確認是否真的不足。」

落實方式：
- **新設計或新機制**：先查 Copilot SDK、Foundry Hosted agents（Sessions、Conversations、State store）、Azure 官方文件與最佳實踐。PR 或設計要附上官方連結，並寫一句「官方能力缺少什麼，所以 FA 才自己做」；官方已有的就直接用，不另做一層。
- **既有的 FA 自建層**：碰到時順手確認有沒有官方能力可以取代；可以的話回報總協調，不自行大改。
- reviewer prompt 原文附上這兩條；只有資料損失、重送或明顯錯誤才列 P1／P2。

## 決策

### 1. 以團隊為信任邊界

- 每個團隊屬於一位使用者，也就是團隊主人。團隊內的 Agent 互相信任，不同團隊完全隔離。
- 入口只確認一次「下指令的是團隊主人本人」。之後整個回合在該 Agent 自己的 Hosted microVM 內完成。
- Teams relay 這條入口邊（2026-10-10 接受）現在也轉送批准決定（Neal 2026-10-11 核准）：僅限互動回合批准卡的 allow_once／deny，經 `POST /teams-relay/v1/decisions` 且 `FA_TEAMS_RELAY_ENABLED=1`、relay 身分在白名單，依平台驗證的 oid 等於團隊主人，不另核 `Behavior.Approve`；排程批准卡、規則變更、提問與 Connect 決定仍只在工作台（見 [Teams 聊天設計](../teams-chat-design.md#信任多出來的一條邊)）。
- 只有下面 4 件事留在 VM 外，其他每一步回 API 的權威核對都移除：

| # | 留在 VM 外的事 | 理由 |
|---|---|---|
| 1 | **入口身分確認與訊息去重**：Entra 身分要等於團隊主人；用穩定的 request ID 加原回條去重 | 防止別人下指令、防止重送 |
| 2 | **外部存檔**：VM 會被回收，所以每回合結束時寫一次 checkpoint、結果和記憶，用 ETag／CAS | 防止資料遺失或被覆蓋 |
| 3 | **不同 VM 之間的 Agent 傳話**：轉送點只核對 sender 和收件人是否屬於同一團隊；sender 由 token 的 oid 決定，不由模型自稱。跳數和訊息數的上限保留 | 防止跨團隊冒名，也防止互相呼叫形成無窮迴圈 |
| 4 | **組織付費或組織管理的資源**：模型由 VM 用 Agent 自己的身分直連 project endpoint，用量隨結束回條回報；有共用秘密的 connector 和語意記憶（Memory Store）經 API，用入口票上的能力清單核對（見第 5 節） | 秘密不進 VM；這些資源沒有逐團隊的原生隔離 |

- 回合中撤權，下一回合在入口生效。取消照舊以 SDK abort 中止這一回合。
- **已知例外（Neal 於 2026-10-01 16:37 選 (b)，issue #306；見 [ADR-0007 第 9 節 ②](0007-foundry-hosted-native-first.md#9-決定狀態)）**：
  - 為了私人記憶直連和 Toolbox，每個 Hosted Agent 的 agent identity 都加上 project 範圍的 Foundry User，以及 Cognitive Services OpenAI User。
  - 這兩個角色涵蓋整個 project 的 memory store 和 Agent 設定。因此在 POC 期間，「不同團隊完全隔離」這項原則**對記憶和 Agent 設定不再由權限保證**，只靠 FA 自己選用的 scope 維持。Neal 已接受這項風險。
  - 第 4 列的 Team 記憶仍經 API 讀寫，這條路徑本身不變；但其他 Agent 是否能直接讀取它，同樣不再由權限保證。
- **例外延伸到 Team note（Neal 2026-10-02 17:30 在 #400 確認：「同意：照上面暫定做。」）**：
  - Hosted 用自己的 agent identity，直接讀寫自己 Team 的 Memory Store scope（TN-1，`native_team_memory.py`）。權限面沒有變化，因為上面的兩個角色本來就能讀寫所有 scope。
  - Team scope 只由 API 依 Agent 登記的 Team 算出，放在每回合的 capture 裡；模型、request body 和 UI 都不能指定。每個 Agent 第一次用的 scope 會固定下來，之後收到別的 scope 就不讀也不寫。
  - **取消逐則人工批准**：同意來自兩處，一是真人在回合中說的原話（C5 候選，必須逐字出現在真人訊息裡），二是部署時打開的 Team 開關 `FA_TEAM_MEMORY_ENABLED`。Agent 來訊、工具輸出和網頁內容不能成為 note。
  - 記憶仍然只是資料，不是指令或批准。第一版不提供更新或刪除；更正就是真人的新說法。
- **例外延伸到 stage「＋」自動指派角色（Neal 2026-10-03 00:12 在 #468 批准，原文：「同意：同意上述新 IAM，也同意「＋」自動寫入。並且記錄一個代辦事項，說明風險並後續需要設計新的方案。」）**：
  - **新 IAM**：stage API 的 UAMI 取得一筆 **Role Based Access Control Administrator**。範圍只限 stage project（`cog-fasample-memory-demo/projects/faexample-stage`），並以 ABAC 條件限制：只能新增角色指派、一律不能刪除；只能指派 Foundry User 和 Cognitive Services OpenAI User 這兩個角色；只能指派給 ServicePrincipal。
  - **用途**：stage 用「＋」新增 Agent 時，程式自動補上上面 #306 的兩個角色和 endpoint 協定，不必再由部署線手動補。Neal 同意「＋」自動觸發這兩類寫入。原因是官方 blueprint 不能繼承 Azure RBAC，新 Agent 一律有自己的身分（計畫見 #468）。
  - **剩餘風險**：stage API 身分被盜用時，對方可以把這兩個角色給 stage project 上**任何** service principal，不只 FA 的 Agent；但不能刪除既有指派，也碰不到 dev 或其他 project。這和 #306 是同一類風險。
  - **待辦**：風險與後續新方案記在 #469。方向包括：改由只做指派的獨立身分處理，等官方提供 Agent 預設權限後改用官方做法並移除這筆 IAM，以及指派前先確認目標是本 project 的 Agent 身分。
  - dev 不在這次範圍內；dev 新增 Agent 的角色仍由部署線手動指派。（已被下方 2026-10-15 修訂取代：dev 也延伸，不再手動。）
  - **修訂（Neal 2026-10-15）**：同一筆受限 RBAC Administrator 例外延伸到 dev，也延伸到 azd 新環境並預設開啟（`FA_ENABLE_AGENT_ONBOARDING` 預設 `true`，`infra/azd/main.parameters.json`）；範圍、條件與剩餘風險同上，各環境只限自己的 project。azd 環境可用 `azd env set FA_ENABLE_AGENT_ONBOARDING false` 關掉。#469 的風險追蹤不變。

### 2. 每個 Agent 一台 VM，取代每個對話一台

- **Hosted session 改成每個 Agent 一個。** 新對話直接交給這個 Agent 正在執行的 worker，只在 worker 裡新開一個 SDK session（本機實測啟動約 0.07 秒），不重建 VM。
- 冷啟動只在兩種情況發生：VM 因閒置被回收，或部署後換了版本。
- **同一個 Agent 同時收到兩個對話**：比照 Grokbot，每個 Agent 一條排他佇列，依序處理；不同 Agent 可以並行，**沒有全域同時執行上限**。原本 FR-09 的「4 個同時執行」已依 Neal 2026-10-02 20:57 在 #432 的決定拿掉（原文：「同意建議：不加全域上限，比照 Grokbot，FR-09 的「超過排隊」先拿掉。」）。若固定版本的 SDK 能在同一個程序內安全地並行多個 session，可列為後續選項；第一版先排隊。
- **對話之間的隔離**：同一位使用者、同一個 Agent 的不同對話共用同一台 VM，也就是共用檔案和程序。這符合「同團隊互信」的前提。每個對話的歷史和 checkpoint 仍然分開保存（以 SDK session ID 和 conversation ID 為 key），不能互相覆蓋。
- **Agent worker 的接點**（71c7 負責觸發與佇列；ResidentPool 和 SDK 保存沿用原實作）：
  - **控制連線**：每個 Agent 的每個 VM 世代只有一條控制連線，不是每個對話一條。這條連線負責把新 RID 交給 worker，以及送回原 RID 的回條。
  - **VM 世代**：
    - 世代是 API 管理的「Agent Hosted session 代次」，以 session ID 加 Hosted 版本為準。只有 session ID 或版本改變才換代。
    - 同一個 session 閒置後恢復 compute，沿用原世代；程序和控制連線的識別另外更新，不拿每回合 claim 的程序代次冒充 VM 世代。
    - 尚未消耗的排隊票，在同世代冷恢復後照原 RID 執行；已消耗或 unknown 的票只查原回條。
  - **排隊**：同一個 Agent 的所有對話進同一條排他佇列，依序執行。
  - **取消**：一律指定 CID 和 RID，並優先處理。VM 健康時，取消一個對話不能停掉同一 VM 上的其他對話。
  - **失聯**：必須停掉整台 VM 時，其他對話各自從已保存的 checkpoint 和待處理回條，沿原 RID 恢復。不能把它們當成一起被取消，也不能換新 ID 重送。
- **閒置逾時**：官方預設 15 分鐘，可設 2～60 分鐘（[hosted-agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)）。設得越長，冷啟動越少，但每個 Agent 保持運作期間約多 US$0.25／小時（2 vCPU／4 GiB，West US 3 牌價）。**Neal 2026-10-01 09:35 決定調到最長：`idle_timeout_seconds=3600`。** 由部署線在 Agent 新版本設定；每個 Agent 最後一個請求後最多再運作 60 分鐘，約 US$0.25。
- **部署換版時的接續**：
  - 換版後，這個 Agent 的新回合改用新版本的 Hosted session，舊 VM 等閒置後被回收。
  - 每個既有對話從它最後一個合法 checkpoint，在新 VM 冷恢復成新的 SDK session。
  - 目前的「在新環境接續」機制（`CONVERSATION_SESSION_VERSION_CHANGED` 加使用者確認）保留不變，避免偷偷換環境。要不要改成自動接續，之後再請 Neal 決定。

### 3. 團隊資料的最小隔離方式（Neal 10:00 採 API 薄轉送）

> **待 ADR-0007 定案後可能被取代（2026-10-01 16:00）**：Neal 決定 POC 階段盡量使用 Hosted agent 原生能力（preview／experimental 也可用），權威資料不必留在 FA。83d4 正在撰寫 ADR-0007（Foundry Hosted 原生優先），本節的 API 薄轉送可能改用 State store 等原生能力。定案前，現有實作與本節仍有效；不要再擴充這一層。

- **做法**：VM 讀寫團隊資料一律經過 API 的薄轉送。API 只做一件核對：用 agent token 的 oid 查出它屬於哪個團隊，再到那個團隊的範圍讀寫。不再做每一步的權威核對。不新增 IAM，網路和儲存帳戶也都不改。
- **防跨團隊與防資料遺失**：
  - 團隊只由 oid 決定。request body、模型輸出或 VM 傳來的 team／table／partition／key 一律不當作授權依據。
  - 轉送只提供固定用途的操作，例如結束時存檔、讀 checkpoint、讀寫團隊成果與記憶。實際的 key 和路徑由 API 依團隊組出，不接受任意 key。
  - 只提供讀取、只能建立一次的寫入，以及用 ETag／CAS 的更新；**不提供刪除**。同團隊的 Agent 仍可以更新彼此的資料，這符合「同團隊互信」的前提。
  - **已發布內容（SOP、skills、角色）對 agent 只能讀。** 只有 API 在團隊主人批准後才能寫入，保持「內容生效需批准」（FR-18、FR-19）。
- **不讓 VM 直連儲存的原因**：兩個儲存帳戶都只開放私人端點，Foundry 帳戶也沒有 VNet 注入；官方規定 VNet 注入只能在建立帳戶時設定（[Foundry 網路限制](https://learn.microsoft.com/azure/foundry/how-to/virtual-networks#limitations)）。所以照現況，VM 根本連不到儲存帳戶。另外，內建的 Storage Blob／Table Data Contributor 可以刪除整個容器或資料表，而 Table 沒有軟刪除；因此不採用這些角色，也不開放 VM 刪除資料。
- **現有資料不搬、不刪、不改寫**：
  - 原 Behavior 表只有一個 partition 和一個 guard，列永遠不刪，歷史讀取器也會對整個 partition 做雜湊比對，所以它繼續作為組織的權威紀錄。
  - 舊 checkpoint 和成果都照原路徑讀回。
  - 如果新增沒有 `kind` 的紀錄型別，要同時加進 `validate_additions` 和 `_quiescence` 的型別白名單，否則存檔後可能讓啟動失敗。
- **語意記憶（Memory Store）**：官方只能在 project 層授權，`scope` 也只是用來分區，不是 ACL（[memory-usage](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)、[ADR-0002](0002-shared-context-without-file-share.md)）。所以照第 1 節第 4 項，經 API 核對團隊後才能使用。例外：私人記憶（#306）和 Team note（#400）由 Hosted 直接讀寫，scope 仍由 FA 固定，不由模型決定（見第 1 節已知例外）。
- **模型直連**：agent identity 預設就能透過 project endpoint 呼叫模型（[hosted-agent-permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)），所以第 5 節 A 讓 VM 直連模型，不經 API。
- **最小驗證**：
  - 離線單元測試：
    - 團隊只由 oid 決定，送別的團隊的 key 會被拒絕。
    - 沒有刪除操作。
    - agent token 寫已發布內容會被拒絕。
    - 衝突時回 412，而且不會覆寫。
  - 部署後在 A 的容器內跑一次探針，需要批准。只記錄狀態碼：自己的團隊讀寫成功；讀別的團隊、寫已發布內容、刪除都被拒絕。
### 4. 入口票、VM 內執行回條、費用紀錄的最小分工

> **待 ADR-0007 定案後可能被取代（2026-10-01 16:00）**：同第 3 節的說明，入口票與開始／結束回條的保存位置可能改用 State store 等 Hosted 原生能力。定案前，現有實作與本節仍有效；新增設計先對齊 ADR-0007 草案，不再擴充 FA 自建層。

本節取代舊的接點：每一步 handler 回 API 核對、每次模型呼叫分兩次走 reserve／consume、每回合都要做的 Hosted claim。

| 項目 | 誰寫 | 寫在哪 | 防的是什麼 |
|---|---|---|---|
| **待開始收件意圖** | API 入口／轉送點 | 轉送時與訊息、root 計數同一 CAS 保存不可變 Agent registration、queued job／pool | 已接單卻漏掉工作、重複接單 |
| **執行票** | 原 `WorkerCore.start` 成功後的 server-owned VM adapter | 沿同一 registration／job／RID 保存，綁定真正的執行資料 | 別人下指令、重送 |
| **開始回條** | VM 內的 worker | 消耗入口票的那一次 CAS | 同一回合被執行兩次 |
| **結束回條** | VM 內的 worker | 經薄轉送寫入，以 RID 為 key，只能寫一次 | 結果遺失或被覆蓋 |
| **交付** | API | 照舊的交付責任與 client 收件回條 | 真人沒收到卻被當成已交付 |
| **費用紀錄** | VM 回報用量，API 入帳 | 結束回條帶用量；API 讀結束回條後，先用獨立 CAS 入帳（以 job＋RID 為 key，只寫一次），再做 completion consumption | 漏記、重複扣 |

- **接單與執行票分開（2026-10-01 總協調確認）**：真人入口核對團隊主人，Agent 轉送核對原 sender／收件者同 Team，同一個 RID 只接一次。T5 在同一 CAS 保存「訊息、root 計數、不可變 Agent registration，以及 queued job／pool」，作為不可遺失的待開始收件意圖，不先假造 `ExecutorBinding`。
- **執行票**：上述 CAS 成功後，由同一個 `WorkerCore.start` 的原 CAS 保存真正的 `ExecutorBinding`／`_Attempt`；VM adapter 再按原 registration／job／RID 產生 Assignment 和最終執行票，綁定 team、agent、conversation、RID 和 Hosted 版本。票不是另一筆接單，不新增 RID；這取代先前「轉送訊息時同 CAS 建立最終票」的說法。
- **開始回條**：熱路徑由 worker 直接收件，冷路徑經 invocation，兩條路消耗同一張一次性票；`begin` 只消耗票並建立原 Session／開始回條，不把 queued job 改成 starting。最多只有一方拿到執行權，重複或 unknown 只能查原回條，不重送。接單成功、票已送出與取得執行權是不同事實，不能用查無回條或傳輸逾時推定「未接受」而另開工作。
- **結束回條**：回合結束時，worker 經薄轉送用 ETag CAS 寫 checkpoint、結果和記憶，最後寫一列以 RID 為 key、只能建立一次的完成記錄。這一列就是權威；API 交付前讀這一列，不另外等通知。worker 中途死掉、沒有完成記錄時，維持 unknown，不重送；收尾規則照 c7bb 的「已證明原程序不在」路徑。
- **交付**：VM 不碰交付。
- **費用紀錄**：VM 直連模型，不再每次呼叫都回 API 預約。VM 累加每次串流的用量，回合結束時放進結束回條；API 讀到結束回條後，先用一筆獨立的 CAS 把用量寫進帳本（`record_turn_usage`，以 job＋RID 為 key，只寫一次），成功或確認已記過，才做 completion consumption 與交付。沒有結束回條的回合，用量記為 unknown，不歸零、不退費。有共用秘密的 connector 和 Memory Store 留在 API，核對入口票上的能力清單。細節見第 5 節 A。
- **保活**：部署線回報（fdbb 更正後的去敏摘要，經 71c7 轉述）：在 `fec04a8e`（API `fec0-v63-1001`、Hosted A63、閒置逾時 300 秒）上，`text-66aa609b` 的 sleep 1080 秒從台灣時間 09:06:34 跑到 09:24:34，exit 0 並 succeeded；同一個 session 的追問 `text-99e40b60` 也 succeeded。保活不再列為阻塞，細節以 71c7 的 T2 方案為準。等票的時間不延長閒置逾時。

### 5. 組織資源與跨 VM 傳話（c7bb 研究，2026-10-01；只做設計，未實作）

依據：main `f3e7c894` 的程式與本 ADR，以及下列官方文件。

**結論**
- **模型**：VM 直接用 Agent 自己的身分呼叫 Foundry project endpoint，模型呼叫不再每次回 API 做 reserve／consume。官方文件寫明，Hosted agent 的身分預設就能經 project endpoint 做模型推論，**不需要新增 RBAC**。
- **用量**：回合結束時，隨結束回條回報一次。沒有結束回條的回合，用量記為 unknown，不歸零、不退費。
- **公司 connector**：有共用秘密的（例如 WebIQ）留在 API broker，秘密不進 VM。Playwright 這類 VM 內的本機能力不需要憑證。Teams 發訊這類對外動作，照舊走 API 的批准流程與回條。
- **跨 VM 傳話（收件匣模式，Neal 15:35）**：A 送出後訊息寫進 B 的收件匣，A 這一輪立刻結束；B 依排他佇列處理，完成後結果寫進 A 的收件匣並喚醒 A 在原 CID 開新回合。轉送點只看發送 VM 的 JWT oid 和它的入口票，上限在同一筆 CAS 內檢查。
- **新 IAM／RBAC**：第一版不需要。

#### A. 組織資源（T4，c7bb 負責）

**A1. VM 直連模型**
- **官方依據**：
  - 每個 Hosted agent 部署時都有專屬的 Entra agent identity，「預設可經 project endpoint 做模型推論」（[hosted-agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)）。
  - 只有繞過 project endpoint、直接呼叫帳戶層級的 OpenAI endpoint 時，才需要另外指派 Cognitive Services OpenAI User（[hosted-agent-permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions#agent-access-beyond-defaults)）。
  - 這個身分屬於 agent 層級（`instance_identity.principal_id`），不屬於某個版本（[manage-hosted-agent](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent)）。
- **不能用部署層級的 RBAC 限制「哪個 Agent 用哪個模型」**：OpenAI User 一經指派，就能用該資源上所有已部署的模型（[Azure OpenAI RBAC](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/role-based-access-control)）。所以改由 FA 的 worker 只用入口票核准的模型送出；在「同團隊互信、VM 內跑的是 FA 自己的程式」前提下可以接受。組織若要壓低某個部署的用量，用部署的 TPM 配額控制，但它限制的是速率，不是金額。
- **接點**：SDK 的模型層本來就經過 FA 的 `CopilotRequestHandler`（`m2_copilot.py` 的 `_ResidentGateway`），VM 也已經有 `ManagedIdentityCredential`。只要把這個 handler 的上游從 API `/model` 改成 project endpoint（scope `https://ai.azure.com/.default`）。回合與工具循環仍由 SDK 管理。
- **尚未確認**：權限文件只明列 `AIServices/responses/*`。**第一版只讓 Responses 模型直連**，Completions 模型暫時保留 API 路徑。部署線依批准做一次 live 確認：用 agent 身分送一個故意無效的 body，回 400 代表已授權，而且不產生 token；回 401／403 代表沒有權限。

**A2. 用量帳：不必每次預約**

`budget_enforced=false` 時，原本每次預約的三個用途改成：

| 原本的用途 | 新做法 |
|---|---|
| 預算關卡 | 目前沒有開啟。之後若要開，改在入口每回合檢查一次，最多超出一個回合的用量 |
| 同一個請求只送一次 | VM 依請求 digest 去重，`retries=0`；結果未知時，這一回合以失敗收尾。VM 死掉時，這一回合是 unknown，也不會自動續跑，所以不會重送 |
| 未決用量 | 已接單、但沒有結束回條的回合，用量就是 unknown。由現有資料推導，不另外寫一列；不歸零，也不退費 |

- **記帳**：VM 用 `usage_from_sse` 累加每次串流的用量，放進結束回條（第 4 節）。API 讀到結束回條後，分兩步：
  1. 先用 `record_turn_usage` 寫進 CostLedger。這是一筆獨立的 CAS，key 和 request_id 都是 `(job_id, RID)`，同一筆只寫一次；同內容再送回原回條，內容不同回 409。
  2. 入帳成功或確認已記過，才做 completion consumption 與交付。入帳失敗就先不 consume，下一次照同一份結束回條重做，不會重複記帳。
  分開做的原因：開機時的 history 驗證會逐筆重建每次 CostLedger 寫入的原 request，入帳併進 completion 的 CAS 會讓兩邊互相綁住。舊帳本的 pending 與已計量數值都不清除。
- **Azure 計量**：平台的 token 指標只能依部署或模型拆分，沒有「誰呼叫」的維度，所以只能用來核對總量和回頭補查 unknown 回合（[監控參考](https://learn.microsoft.com/azure/foundry/openai/monitor-openai-reference)）。若要每個團隊嚴格的 token 上限，可以用 APIM 的 `llm-token-limit`（[policy](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)），但那需要新資源與費用，第一版不做，由 Neal 決定。
- **和 CAP-02 的交會（總協調決定）**：
  - 移除 `reserve_model` 裡「root 合計 8 次模型請求」，改用兩項替代：
    - 交辦鏈的上限照 B 節第 3 點（深度 2；request／handoff／FYI 8 則，另加 result／blocked），在轉送點的同一筆 CAS 內檢查。
    - 入口票帶「每回合模型呼叫上限」，由 VM 在本機計數；超過時明確停止，不重送。數值沿用既有一般回合上限，沒有的話首版用 100 次，之後由 Team policy 調整。
  - **2026-10-01 緊急更正（總協調確認）**：現行2跳／8則訊息、唯一handoff／回覆及#279 peer每回合100次已足以防互叫，A/B合計8次直接移除；不再等完整新T5票鏈。一般root預算、once／回條及上述界線保留；T4直連仍須沿同一gateway每回合計數，不因這次刪除而自動啟用。
- **一起搬的東西**：
  - 8765 的 live 預覽：改由 VM 經現有 progress 路徑送出。
  - delegation 子模型與 compaction 呼叫：改在 VM 內處理，用量併入同一回合；CTX-03 保存核對不變。
  - 非 instruction 的舊路徑（例如 msteams 的 model-intent 恢復）：不動。
- **已知取捨：直連不經過 API 的 request 檢查**（總協調確認，2026-10-01）：
  - **差異**：API 的 `/model` 會在送出前做 `AssistantProvider.validate`，包括 128 KiB 的文字 body 上限、工具白名單、`include` 與 `store` 的格式。VM 直連時，只剩 `_ModelGateway` 的 4 MiB body 上限、模型檢查、串流檢查和重送拒絕。所以開啟後，原本會被拒（422 或 403）的超大回合，或白名單外的工具宣告，都會照常送出。
  - **為什麼可以接受**：這符合「同團隊互信」的前提。工具在建立 Agent 時就已決定，SDK 只會宣告該回合捕獲的能力；VM 內跑的是 FA 自己的程式；用量照常隨結束回條入帳，不會漏記。
  - **仍保留的保護**：peer 回合、持有 collaboration 能力的回合、delegation 與 compaction 都照舊走 API，A／B 合計上限和逐次 `record_usage` 不變。
  - **實作**：見 #288（預設關閉）。開啟條件是 T1b 把 `turn_model_usage()` 接進結束回條並翻開程式鎖、T5 整片上線，屆時再一起評估。

**A3. 公司管理的 connector**

| 類型 | 例子 | 做法 |
|---|---|---|
| VM 內的本機能力 | Playwright | 不需要憑證；允許清單由入口票帶入 |
| 有共用秘密的 connector | WebIQ（`x-apikey`）、第三方 MCP 金鑰 | 留在 API broker（現有的 `capability_call`），秘密不進 VM。只核對入口票帶的能力清單，不再每次重讀 registry 或做 authorize。同一個 `tool_call_id` 只執行一次 |
| 能用 Entra 驗證的 connector | Azure 服務、支援 agent identity 的 MCP | 第一版不做。之後要直連時，每一項都要 Neal 批准 |
| 代表使用者對外行動 | Teams 發訊 | 照舊走 API 的批准流程與交付回條 |

- **秘密不發給 VM**：秘密一旦進了 VM，就可能被 prompt injection 誘導送出去；被複製走的金鑰在團隊以外也能用，會打破團隊隔離。Foundry Toolbox 的存取權是 project 層級，無法區分團隊，所以也不用它來做團隊控制（[toolbox](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox)）。
- **撤權**：下一回合入口重新擷取能力清單時生效。
- **WebIQ 改走官方 toolbox（2026-10-22 註記）**：上表「有共用秘密的 connector」一列的 WebIQ 例子已被 [ADR-0007 N5](0007-foundry-hosted-native-first.md) 取代。Neal 2026-10-22 決定「只留 toolbox」：WebIQ 的 key 只放 Foundry project connection，由 toolbox `fa-org-tools` 使用，API 和 VM 都拿不到；API broker 路徑 `WebIQ-MCP` 和 API 的 secret 已退場（[README](../../src/fa-shared-context-poc/README.md#webiq-toolbox-only)）。toolbox 是 project 層級、沒有 Team ACL，這點對**組織共用**的 WebIQ 不構成問題。其他有共用秘密、沒有官方 toolbox 的 connector 仍走 API broker（通用的 `mcp-api` 保留）。

#### B. 跨 VM 傳話：收件匣模式（T5，fcd 負責；Neal 2026-10-01 15:35 決定）

Neal 同意 CAP-02 改用 Grokbot 的收件匣模式。下面是新語意；原本「A 在同一回合內等待並接回 B 的結果」的同步設計已被取代，摘要列在本節最後。

**native-only 最新修訂（Neal 2026-10-01 18:46「靠近Grokbot」、18:47「不要比grokbot嚴格」）：**
新 CID 依 ADR-0007／N1 的 State store 回條與官方 chain 接收，不沿用舊 Table 接單。
每個 root 新交辦預設 **10 則**，只在 `MAX_NATIVE_RELAY_MESSAGES` 一處設定；
不設跨 Agent hop 上限，也不另加每回合100次模型上限。hop只保留來源深度。
舊 lane 的既有格式與保護不變。這是本次明確產品決定，不宣稱 Grokbot 所有場景都有相同的全域限制。

1. **送出就結束**：A 呼叫 `send_agent_message` 後，訊息寫進 B 的收件匣，工具立刻回「已交給 B」，A 這一輪就結束。A 不等待、不輪詢，也不把結果接回原回合。
2. **B 排隊處理**：B 收件匣的訊息依 B 的排他佇列（T2）處理。B 忙碌只是排隊，不算失敗。
3. **結果喚醒 A**：B 完成後，結果寫進 A 的收件匣，用 WAKE 機制喚醒 A，在 A 原本的 CID 開一個新回合（例如「B 回報：…」）。原真人的最後交付仍由 A 在這個新回合完成。
4. **只保留最小保護**：
   - **發送者綁定**：native VM 帶自己的 agent JWT，轉送點讀回該 Agent 的原 CID／RID 回條核對來源。模型只提供 `recipient_agent_id`、`kind`、`content`、`refs`；sender、root、message ID及result的`in_reply_to`由host／relay依原件產生。Agent資料不冒充真人授權。
   - **收件者**：必須和發送者同團隊，而且不能傳給自己。入口票帶的團隊目錄只當工具 enum 的提示，不能用來授權。
   - **去重與原回條**：`message_id = H(發送回合的 RID, SDK 的 tool_call_id)`，模型無法指定。訊息列以 `message_id` 為 key、只能建立一次，並記下內容 digest。同 ID 同內容回原回條，不同內容回 409；結果未知時只能用同一個請求再試。
   - **上限**：native relay的一個root只用一個CAS item，最多10個message ID／原輸入hash，不存訊息內容、結果或接收回條。重送同ID同hash不重複計數，不同hash拒絕；CAS不明先讀原item，不換ID。reply不占新交辦名額，也不在這個item設reply狀態。
   - **接單（native沿N1原語）**：先核root名額，再由B自己的State store `create_item(rid:<message_id>)`持久接收，才回「已交給 B」。B的N1回條就是收件原件，排隊沿官方chain；不另建inbox／queue／WorkRegistration／Job。不是把不同store的寫入稱作跨key原子交易。
   - **唯一result與wake**：A自己的`rid:<in_reply_to>`同時是唯一result接收與wake ID；同ID不同來源／內容拒絕。B先完成checkpoint／turn／completed回條，完成hook才經API relay送A原CID的新RID。回調中斷可用原件補同一wake，不重跑B；read/history不觸發hook。即時wake不等待Routines，N4只接後續排程。
   - **結果來源**：結果的 sender 綁定在 B 消耗入口票時的 JWT oid；B 沒有消耗票、已取消或已失聯時，API 寫一則來源標為 `server` 的 `blocked` 通知交給 A，不冒稱是 B。
   - FYI 只保存，不喚醒；receipt 不計入上限，也不喚醒。
   - **refs**：只能指向自己團隊的資料，收件者經第 3 節的薄轉送讀取。
5. **移除（已被收件匣模式取代）**：
   - A 回合內等待與輪詢、A 等待逾時
   - `receiver_busy` 失敗
   - peer 專用的 claim 和 registered gate
   - 把結果接回 A 原本的 SDK 回合
   - root 合計次數上限
   - 只能交接一次的限制
   - 「同一條鏈上正在等結果的 Agent 不能當收件者」：不會再有人在回合內等待；native由root新交辦總數限制互叫，不另設2跳或100次限制
   - sender 分享白名單、`validate_team_refs` 的兩端讀權核對、固定的 A→B 配對 enum、`PeerWorkReference`、B「沒有 job 先登錄」及其專用 consumer、A 的 observer 代推 B 佇列

**已取代的同步設計（保留作紀錄）**：2026-10-01 上午的版本要求 A 用自己的 JWT 讀轉送點等待結果，到 root 期限仍是 unknown 就回報失敗，並禁止把正在等結果的 Agent 當收件者以免互等。收件匣模式下 A 不再等待，這些規則一併移除。

**原生來源欄位**：N1的`NativeAgentOrigin`保存sender Agent／CID／RID、root原地址、hop與`in_reply_to`／原結果引用，
並納入NativeReceipt原digest。沒有等待chain／deadline，也不新增T1票。舊票設計只保留為Table路徑歷史。

#### 新 IAM／RBAC

- **第一版不需要**：
  - 模型：使用 Agent 身分在 project 上的預設權限。
  - connector：秘密留在 API。
  - 傳話：沿用 Agent 呼叫 API 的 token。
  - 團隊資料：第 3 節的 API 薄轉送（Neal 10:00）。
- **之後若要做，逐項送 Neal**：Entra connector 直連（對 Agent 身分的 RBAC），以及每個團隊嚴格的 token 上限（APIM）。
- **需要決定、但不是 IAM**：VM 直連 Responses 的那一次 live 確認（無效 body、不產生 token）。

## 要修改的既有界線與需求

**AGENTS.md 九條界線**（本 PR 已改，只改受影響的句子）：

| 條 | 改法 |
|---|---|
| 2 | 每個 Agent 一台 Hosted VM（一個 Hosted session），可服務多個對話。私人資料不跨團隊。 |
| 3 | 精確狀態、成果、語意記憶的分工不變；Hosted 經 API 薄轉送讀寫自己團隊的資料，只核對 agent 所屬團隊；語意記憶也經 API（私人記憶 #306 與 Team note #400 例外，見第 1 節）。 |
| 4 | 改寫為「以團隊為信任邊界」：入口確認一次團隊主人，跨團隊靠 API 轉送點依 agent 身分隔離；模型由 VM 直連，用量在回合結束時入帳；組織 connector 的秘密留在 API。模型輸出和 request body 仍不能擴權到團隊以外。 |
| 9 | API 只負責入口與去重、團隊資料薄轉送、跨 VM 轉送、用量入帳、共用秘密 connector broker、onboarding 與已發布內容的寫入；回合執行和模型呼叫在 Hosted。 |

第 1、5、6、7、8 條不變。第 6 條「有權的人」就是團隊主人。

**requirements**（本 PR 已改，FR 編號不變，標註 2026-10-01 依本 ADR 修訂）：FR-02、FR-05、FR-06、FR-07、FR-09、FR-12、FR-15、FR-19、FR-20。

**既有 ADR**（本 PR 在文首加註，不改寫原文）：
- **ADR-0002**：資料分工和「經共用 Context API 讀寫」都保留；API 改成只核對 agent 所屬團隊，不再每一步做權威核對。Memory Store 也經 API（私人記憶 #306 與 Team note #400 例外，已在 ADR-0002 文首加註）。
- **ADR-0004**：入口接單、去重、交付回條和結束時的保存都保留；「跨程序的工作歸屬與批准由 FA 每步核對」縮減為本 ADR 第 1 節的 4 件事。
- **ADR-0005**：常駐 worker 改成每個 Agent 一個，可服務多個對話，熱路徑只需入口的 ticket 加結束時的保存。PoC 程式保留。

**CAP-02**（Neal 15:35 改收件匣模式，見第 5 節 B）：
- 轉送點只核對 sender 和收件人屬於同一團隊；sender 由 token 的 oid 決定。A 送出即結束，B 的結果經收件匣喚醒 A。
- 原本「sender 分享白名單加收件端 read 核對」、回合內等待與 `receiver_busy` 失敗都刪除。
- native只保留每個root預設10則新交辦、每個request在A store唯一result/wake、同Team／非self及原回條；舊lane上限不改。

**CAP-06**：
- 「團隊管理員」就是團隊主人本人。onboarding 時登錄新 Agent 的 oid 屬於哪個團隊；不需要建立儲存資源，也不需要指派角色。
- **上限的最小作法**：
  - server config 設一個組織層級的「每個團隊最多幾個 Agent」，預設值同時就是硬上限，因為費用由組織支付。
  - 團隊主人可以在這個範圍內調低，不能調高；第一版不做 UI，只由 operator CLI 設定。
  - 名額檢查和佔用照舊放在同一個 CAS。

## 和進行中工作的關係（不要求停工）

- **79fa 的 turn ticket（#264 與後續切片）**：方向一致。它就是第 1 節第 1 項的「入口核一次、回合內沿用」。後續切片可以直接刪掉 Hosted 每一步回 API 的核對，例如 catalog、session、restore 的重複讀取；claim 和 R6 的合併可以簡化，因為第 2 節改成每個 Agent 一個 session，claim 只在 VM 首次啟動時做。
- **71c7 的常駐觸發**：改以「每個 Agent 一個 worker、可服務多個對話」為前提。新對話也走熱路徑，等待中的請求依 Agent 排他佇列處理。總協調會另外通知 71c7。
- **CAP-06 PR1～PR5（1b60）**：
  - PR1：目錄依團隊列出 Agent；CAP-02 收件人簡化為「同團隊」。
  - PR2：role prompt 版本列只由 API 寫入，Agent 只能讀；其他照舊。
  - PR3：門禁 C 的 registry 加上 agent oid 對應團隊的映射；T3 薄轉送和 T5 轉送都沿用這份映射。門禁 C 只核 app-only token 的 oid，不要求 `Behavior.Agent` app role；設定檔 actor 同樣只核 oid（2026-10-04 stage 實測：受控身分 token 由平台快取約 24 小時，補指派角色後仍時好時壞，見 [受控身分授權限制](https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations#limitation-of-using-managed-identities-for-authorization)）。
  - PR4：不再需要建立團隊儲存資源或指派角色，只保留 onboarding 自動建立 Foundry agent 和登錄 oid。
  - PR5：不變。

## 實作切片與預估

總協調 2026-10-01 09:40 核准 T1、T2、T4、T5 動工；Neal 10:00 決定 T3 採 API 薄轉送，不需要新 IAM，由 83d4 直接實作。各切片由現有工作線接手，不另開新線重做。

| 切片 | 內容 | 負責 | 依賴 | 規模 | 狀態 |
|---|---|---|---|---|---|
| T1 團隊與入口 | 團隊 registry（team_id 對應主人 oid，每位使用者一個團隊）；verifier 接受租戶內帶 scope 的真人，依 oid 找到團隊；入口核對主人；入口票，以及 VM 開始／結束回條的接點；去重不變 | 79fa，併入 turn ticket 第 1～3 片 | #264 | 中 | 已核准 |
| T2 每個 Agent 一個 session | conversation 改綁「Agent session＋SDK session」；新對話走同一個 worker；每個 Agent 一條排他佇列；換版時沿用「在新環境接續」 | 71c7，和常駐觸發合併成同一件事 | T1 | 大 | 已核准 |
| T3 團隊資料薄轉送 | VM 用 agent token 呼叫；API 依 oid 找到團隊，只提供固定用途的讀取、只能建立一次的寫入和 ETag CAS 更新；不提供刪除；已發布內容只能讀；移除每一步的 API 權威核對；現有資料不搬 | 83d4 | T1、PR3 的 oid→團隊映射 | 中 | 已核准 |
| T4 組織資源 | VM 直連模型（第一版只限 Responses），回合結束時隨結束回條回報用量；有共用秘密的 connector 留在 API broker，核對入口票的能力清單；細節見第 5 節 A | c7bb，研究完成後直接實作 | T1；直連仍須沿交接跳數／訊息數與peer每回合上限；合計8次依緊急更正另行移除，不是直連啟用批准 | 中 | 已核准。**T4b（私人記憶用量上限／用量帳）降為 backlog**（Neal 10-02 約 15:10 同意；理由同下方 T6）。私人記憶的查詢 embedding 和非同步萃取用量**目前沒有入帳**，照實列為已知缺口，只寫進 log |
| T5 Agent 轉送 | CAP-02 改收件匣模式（Neal 15:35）：送出即結束、B 排隊、結果喚醒 A；同團隊核對加上限；細節見第 5 節 B | fcd，#259 之後接手 | T1、T2、PR3 的 oid→團隊映射 | 小 | 已核准 |
| T6 多 Team runtime | 每個 Team 各自擁有 standing policy、成本帳本和 root budget，讓每位使用者的 Team 都能獨立執行 Agent，彼此完全隔離。在此之前，CAP-06 第一版只能在 config 原本的 Team 內新增 Agent | 83d4 設計；T6a 83d4、T6b 1b60 | T1、T3、CAP-06 PR3 | 大 | 設計已核准；T6a（#282）、T6b 建立規則（#365）已實作、未部署。**T6b 的接單／入帳串接（Team 額度檢查、`team_scope`、connector 執行）降為 backlog**（Neal 10-02 約 15:10 同意）：依「不要比 grokbot 嚴格」，Grokbot 新建 Agent 可直接使用，計費在後端、以帳戶為單位；記憶只有內容長度限制，沒有花費上限。所以只保留組織總上限（$110，保留 $10），作用類似 Grokbot 的帳戶層級計費；Team 設定只保存數值，不在接單或入帳時強制執行 |

**T6b 新 Team 的產生規則（Neal 2026-10-02 08:28 核准 A/A/A/A/B）**：
- Q1 A：由原 operator 用既有 T6a 初始化指令建立 Team，不自動建立。
- Q2 A：server config 的 `team_defaults` 放一份組織預設（Team 模型金額上限、Agent 上限、可用 connector）。operator 建立時只能維持或往下調，Team owner 之後也只能往下調，規則同 CAP-06；往上調一律拒絕。
- Q3 A：累計上限，沿用 T6a 的用量帳；要提高時由管理員改版（另一片，本片不提供）。
- Q4 A：組織總上限維持 $110、保留 $10，不改。
- Q5 B：沒指定 connector 時，直接繼承組織已核准的清單。

本片只保存這些數值：owner 每一步都留在 Team 設定列，啟動時逐步核對原回條；調到低於目前用量只影響之後的接單。接單／入帳的 Team 額度檢查、`team_scope` 與 connector 執行都還沒接上，而且已依 Neal 10-02 約 15:10 的同意**降為 backlog**（見上表 T6 列），不是待做的後續切片。

CAP-06 PR1～PR5 仍由 1b60 負責，依本 ADR 調整（「團隊管理員」就是團隊主人）。

每支都跑兩軸審查，只改 UI 的跑 Standards。reviewer prompt 要原文附上「實作原則」兩條，只有會造成資料損失、重送或明顯錯誤的問題才列 P1／P2。清零且 GitHub 顯示 CLEAN 就合併。

<a id="api-routine-clock"></a>
## 補充：API 兼任已批准排程的時鐘（2026-10-07，總協調決定方案 C；stage 自 2026-10-08 起已開）

- **新增職責**：「要修改的既有界線」第 9 條的 API 分工，加上「已批准排程的到期觸發」。API 常駐單副本（dev／stage 都是 min=max=1），由 lifespan 的背景 task 每分鐘沿用 #581 的 `due`／`trigger` 送出到期的 occurrence。不建 Container Apps Job、專用 MI、app role、AcrPull 或 Foundry routine；回合仍在 Hosted 執行，API 不跑模型。旗標 `FA_NATIVE_ROUTINE_CLOCK=api`：程式預設關閉；`deploy/stage/stage.json` 自 2026-10-08 起設為 `api`（stage release 31 帶著它），`deploy/azd/profile.defaults.json` 也設為 `api`。
- **理由**：Neal 10-07 16:41 問「為什麼需要新的雲端資源呢?」。Hosted Agent 只在有請求時讓 session 的 VM 醒著，閒置逾時後收回運算（[Hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)，10-07 查閱）；固定版 `github-copilot-sdk==1.0.13` 也沒有排程或定時喚醒 API（[PyPI 1.0.13 說明](https://pypi.org/project/github-copilot-sdk/1.0.13/)，10-07 查閱）。所以一定要有外部時鐘；而 API 本來就常駐，是不用新資源的最小選擇。
- **Foundry Routines 的反例（dev 探針 P1，10-07 17:18）**：用 API 的 app token 建立 `authorization.identity="creator"` 的 routine，服務回 `creator' requires a human user principal … authenticated with a service principal (app) token`。官方文件寫 creator 可以是「人或服務主體」，實際只收真人委派 token。改用 agent 身分時，Entra 隔離讓它叫不到 API 建立的那台 VM；不帶 session 又會每次開新 VM，違反第 2 節。證據與連結見 [Shared Context README](../../src/fa-shared-context-poc/README.md#native-routine-api-clock)。
- **信任邊界不變**：時鐘只能送 owner 已批准、目前仍啟用的定義的到期 occurrence；不能自填內容、RID 或授權，也沒有 HTTP 入口。去重沿用固定 RID＋CAS 意圖＋Hosted `rid:<RID>`，重啟或新舊 revision 重疊都不會重送。
- **必須常駐**：時鐘需要 API `minReplicas=1`。`behavior-api.bicep` 預設是 0，只有 stage 參數檔設 1；dev 目前是 1。若 API 縮到 0 或改成多副本，要重新評估本節。縮到 0 時排程會延後、不會重送；多副本時去重仍成立，只是每個副本各讀一次。

<a id="agent-retirement"></a>
## 補充：退役 Agent 是 tombstone，不是刪除（2026-10-15）

- **起因**：2026-10-15 經 Neal 批准，dev 一位「＋」測試 Agent（`fa-agent-cc96…`）在 Foundry 端以 `force=true` 刪除
  （連同 85 個版本、56 個 session），兩筆角色指派也移除；API 端仍保留它的 roster、註冊列與綁定列，工作台列出一位載入失敗的助理。
- **決定**：退役是在 Team 資料**新增**一筆 create-only 的 `team_agent_retired` 列（誰、何時、原因、request ID），
  並在同一筆 CAS 停用這位 Agent 的身分索引（沿用 owner 停用的既有欄位）。第 3 節「不提供刪除」不變：roster、註冊列、
  onboarding、綁定列、routine 定義、對話與回條都不刪、不改寫；roster 名額與顯示名稱也不釋出。tombstone 只由原組織
  operator 經 operator CLI 寫入（與停用 owner 同一權限），Agent、模型或工作台都寫不了。
  （2026-10-16 修訂，總協調依 Neal「修 1-20」決定：onboarding 卡在 `registered` 的 Agent，退役 CAS 可一併把它從 `pending`
  名單拿掉，還回該名額；roster 名額仍不釋出。已經有 tombstone、onboarding 停在 `admitted`／`creating`／`registered` 的
  Agent（舊 CLI 退役），由獨立的 `team-agent-retire-release` CAS 只把它從 `pending` 拿掉，冪等、限本 Team；沒有 tombstone
  的 onboarding 永遠不從 `pending` 拿掉。）
- **效果**：所有即時路徑都以 tombstone 為準：工作台目錄與 API 啟動不列、不建 peer；不 invoke、不 follow、不 sync 版本；
  routine 到期不觸發（只記 log，定義保留）；部署相容性閘門不因它的綁定列擋部署；送給它的訊息回 410 `AGENT_RETIRED`。
  歷史讀取（startup inventory）照常解析它的原紀錄，並核對 tombstone 與它的原回條。
- **「不拒絕」的範圍**：ADR-0009 的 M2「不拒絕主人的訊息」針對版本不相容或來源碰不到——助理還在，訊息可以在請求內換到新
  session 送達。退役的助理沒有可以換去的環境，照實回「這位助理已退役，訊息沒有送出」才是誠實的結果；若照 M2 回
  `server_following` 讓工作台同 RID 自動重送，會變成永遠不會成功的重試。所以 M2 不適用於退役。
- **Foundry 端清除另外決定**：刪 Foundry agent（`force=true`）與它身分的角色指派是 operator CLI 的選用步驟，先唯讀預覽、
  再明確確認，每次都需要該環境的批准；API 受控身分依第 1 節的 ABAC 條件仍不能刪除角色指派，由有權限的真人依 CLI 列出的
  確切指派 ID 刪除後重跑讀回。只有 operator CLI 能退役、名額與顯示名稱不釋出、退役同時停用身分、角色指派由真人刪，
  四項由總協調 2026-10-15 確認。（2026-10-16 修訂，總協調依 Neal「修 1-20」決定：onboarding **失敗**留下的 Foundry agent
  也由 FA 刪除——只限它自己的那個、operator 預覽後確認，不寫 Team 資料。）做法見
  [deploy README](../../deploy/README.md#retire-agent)。

## 官方依據

- [Hosted agents：每個 session 一台 VM 隔離、agent identity 是執行時身分、閒置逾時 2～60 分鐘、每個請求會重置閒置計時](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Agent identity](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-identity)、[Hosted agent 預設權限（模型推理與 session 儲存）](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Foundry VNet 注入只能在建立帳戶時設定](https://learn.microsoft.com/azure/foundry/how-to/virtual-networks#limitations)、[Memory 的 scope 與授權](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)
- [Storage 內建角色的權限（Contributor 含刪除）](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/storage)
- [Blob 並行控制](https://learn.microsoft.com/azure/storage/blobs/concurrency-manage)、[Table Update Entity 的 If-Match](https://learn.microsoft.com/rest/api/storageservices/update-entity2)
- 團隊資料隔離的完整研究（含不採用的直連方案和 IAM 清單）由 83d4 提供，基準為 main `f3e7c894`。
