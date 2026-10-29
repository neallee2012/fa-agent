# FA 雲端授權與 USD150 成本 preflight

<a id="dev-api-scale-zero-1025"></a>
## 2026-10-25 dev 上限 USD800 → USD900（Neal 決定；先試過 API 縮到 0，不可行）

- **結論**：dev 共用上限改為 **USD900**、停損 **USD880**（Neal 10-25 選「改成提高上限」，金額選「USD900／停損 880」）。API 維持最少 1 份、2 vCPU／4Gi；排程時鐘與提案改回 `api`／`1`（`deploy/dev/dev.json` 回到 `c0c30add` 的值），合併後部署 dev，讀回記在下一份 dev 測試報告。其他規則不變（`budget_enforced=false`、不清帳、stage 另列每月 USD200）。
- **起因**（Cost Management `ActualCost`，報表會延遲）：到 10-24 約 **USD758.39**，距原停損 USD780 約 USD22；7 天平均約 USD13.75／天，沒有測試時約 USD8.1／天。Neal 先選「維持上限，先降低 dev 常駐費用」，省錢項目只選「A1 API 空閒縮到 0」（推論可省約 USD3.1／天）；刪除殘留資源與縮小 API 規格都沒有選。
- **A1 的實際操作與結果**（#981 先把時鐘與提案改 `0`，因為時鐘為 `api` 時 `ci_deploy.py` 要求最少副本至少 1）：
  - 只換 API（`ci_deploy.py --config deploy/dev/dev.json api`，沿用 `c0c30add` 的 API 映像；`c0c30add..1e34c3ec` 只改了 `dev.json` 和文件）：`ca-fa-behavior-poc--1e34c3e-r3602-1` ready，讀回時鐘 `0`、提案 `0`。
  - 最少副本改 0（直接用 `az containerapp update --min-replicas 0`，不經 `ci_deploy.py`；revision `…r3602-2`）：`runningState=ActivationFailed`。03:52:30Z 建立後啟動探針一路失敗（探針每 5 秒一次、可失敗 120 次，約 10 分鐘），03:57:31Z KEDA 把它從 1 縮到 0，從沒 ready。期間流量權重寫在 r3602-2，但 r3602-1 仍有 1 份副本、`/healthz` 回 200（沒有確認是哪個 revision 回的）。
  - 原因：API 每次部署新 revision 都要約 5～6 分鐘才開好 port（兩次實測，含平台建立時間：r3602-1 建立後啟動探針至少失敗 64 次；改回最少 1 份的 r3602-3 從送出到 ready 6.3 分）。縮到 0 那次，還沒啟動完就被收掉（推論：模板沒設 `cooldownPeriod`，預設 300 秒，與建立到縮回相隔 301 秒相符；不是探針次數用完）。
  - 沒有量過「縮到 0 之後被訊息叫醒」要多久；推論和部署時差不多，閒置後第一則訊息要等數分鐘，所以不改收回時間。
  - 還原：`az containerapp update --min-replicas 1`，`…r3602-3` Healthy、100% 流量、`/healthz` 200；r3602-1 讀到時在 `Deprovisioning`。A1 沒有省到錢。
- **原因（10-28 查明）**：API 開 port 前把整份 authority 工作資料讀完並逐列核對兩次（建 app 時一次、lifespan 又一次）。每次讀取只要約 7～13 秒。dev `…57e43d8-r3701-1`（10-27，32,595 筆、88 MB；時間取自該 revision 的 Container App console 與 system log，唯讀讀取）：container 23:29:10Z 啟動，23:31:56Z 建好 app（建 app 那次核對約 2.5 分），23:35:24Z 才開 port（約 6.2 分）。修正（#1021）拿掉 lifespan 那次重複核對，預估開 port 前少約 2.5～3.5 分；3.5 分是上限，只計 lifespan 整段（含 memory.warm、peer router `prepare()` 等其他 lifespan 檢查），不全是核對；剩下的一次核對會隨資料量變長，還沒處理，部署後要讀回實際時間。
- **10-28 實測**：3702 實測約 282 秒（3701 374 秒），剩 lifespan 約 121 秒未查（[dev 測試](progress.md#dev-test-r2-1028)）。
- **10-28 實測（run 3703，main `25a1f970`，含 #1026、#1028、#1029）**：開 port 前約 142 秒（3702：282 秒、3701：374 秒），讀取 1 次（[紀錄](progress.md#startup-private-1028)）。
- **10-28 核對加速（本機量測，dev 未讀回）**：用本機合成資料（100 次對話、3,936 列，每筆約 2.7 KB，與 dev 相近）量，`validate_additions` 7.42 秒裡有 6.68 秒花在逐列試 51 種資料格式；改成先依格式定義排除一定不合的格式後，逐列只剩 0.36 秒，`validate_additions` 1.10 秒，整段核對 11.42→4.96 秒。dev 前後實際秒數要部署後讀回。
- **10-28 開機讀取上限（不變）**：開 port 前那次讀取的上限維持 256 MiB（以 dev 每筆約 2.7 KB 算約 10 萬筆；依 10-27 的 32,595 筆、每天約 305 筆推算約 7 個月）。API container 的記憶體峰值實測約是資料量的 11～19 倍（含基線）：dev 10-01 讀 75.6 MB 峰值約 857 MB（progress.md 第 3309 行）、127 MiB 合成資料峰值 2.33 GiB（progress.md 第 3528 行，API 因此改 4Gi）。所以 API 可能在讀到 256 MiB、回 413 之前就先記憶體不足；資料到約 200～300 MiB 就有風險 [推論]。同時兩次完整核對已改成一次一個（#1029）。根本解法是有記憶體上限或串流的核對，或先在 API image 實測後再訂上限，列為後續 [推論]。

<a id="stage-actualcost-1015"></a>
## 2026-10-15 stage 第 30 版部署當天 ActualCost 實查（stage 月預算 USD200 維持）

- **實查**（Cost Management `ActualCost`，報表會延遲）：stage 查詢區間 2025-10-16～2026-10-15 合計約 **USD68.60**（RG 59.65＋`ME` 8.95）。不含當天部分回合（報表延遲）。查詢區間不是單一月份，沒有拿它和月預算逐月比較。
- **預算**：stage 每月 USD200 不變（見下方 [stage 環境另編預算](#2026-10-02-stage-環境另編預算adr-0008)）；其他規則不變（`budget_enforced=false`、不清帳）。第 30 版部署見 [progress](progress.md#stage-v30-1015)。
<a id="dev-v30-test-1014"></a>
## 2026-10-14 第 30 版候選 dev 測試前後實查（上限 USD800／停損 USD780 維持）

- **測試**：main `1c2be0b4` 的 dev 測試，結果見 [progress](progress.md#dev-test-v30-1014)。
- **實查**（Cost Management `ActualCost`，報表會延遲）：測試前約 **USD600.44**（`rg-faexample` 578.42＋`ME_cae-facontext-private` 22.03；分項四捨五入，相加為 600.45），測試後約 **USD609.91**（587.76＋22.15），兩次差約 USD9.47。查詢時間與報表日期未記；因報表延遲，這個差額不等於測試本身的花費。
- **上限**：不變，仍是 [USD800／停損 USD780](#dev-cap-800-1013)；距停損約 USD170。其他規則不變（`budget_enforced=false`、不清帳、stage 另列每月 USD200）。
- **10-21 讀數**：第 34 版測試期間 dev 讀數 USD715.18→716.58（報表有延遲；原記在 CURRENT-STATE 費用列，2026-10-23 移到這裡）。
<a id="dev-cap-800-1013"></a>
## 2026-10-13 dev 上限 USD650 → USD800（Neal 授權）

- **Neal 10-13 下午選了選項**：「上限 800，停損 780」。
- **上限**：dev 共用上限由 USD650 提高到 **USD800**，停損 **USD780**，到停損就先停下來回報。
- **起因**：每日固定開銷約 USD15～30，照這個速度約 10-15 會碰到原停損 USD630；接下來還有 dev 測試要跑（release 29，含換模型；G phase 2；phase 3／3b），每次約 USD5～10。
- **決定前實查**：約 **USD572**（`rg-faexample` USD550.63＋`ME_cae-facontext-private` USD21.13；報表約晚一天）。之後再查 RG 一次回 429，沒有拿到更新的數字。
- 其他規則不變：不清帳；`budget_enforced=false`，這仍是記錄用的上限，沒有建立 Azure Budget；stage 照舊另列每月 USD200；每次 dev 部署照舊逐次批准。

<a id="dev-cap-650-1013"></a>
## 2026-10-13 dev 部署批准（上限 USD650／停損 USD630 維持）

- **Neal 10-13 早上（07:1x 前）回答**（他自己選了選項）：「上限 650，停損 630，批准部署」。
- **上限**：沒有改變。dev 共用上限自 10-11 起就是 **USD650**、停損 **USD630**（[10-11 紀錄](#dev-cap-650-1011)）；這次回答是重申這兩個數字，到停損就先停下來回報。總協調當時的提問誤引了舊的停損 USD580（那是 USD600 上限時的數字），以本節與 10-11 紀錄為準。
- **批准範圍**：只批准這一次 dev 部署，目標是 main `01e0fdb2`（#812、#813、#814、#815 都已合併），用來測 release 28。不含 stage 部署；stage 仍每次都要 Neal 核准。
- **實查**（Cost Management `ActualCost`，RG 範圍，2025-10-14 起到 10-12 報表日：API 單次查詢最長一年，所以起點是往回約一年的滾動窗口，不是 dev 的開帳日；API `2023-11-01`）：`rg-faexample` USD549.15、`ME_cae-facontext-private` USD20.83，合計約 **USD570**。每日 RG：10-08 21.87、10-09 15.14、10-10 21.63、10-11 30.29、10-12 14.09（10-12 只是部分資料，報表約晚一天）。
- 其他規則不變：不清帳；`budget_enforced=false`，這仍是記錄用的上限，沒有建立 Azure Budget；stage 照舊另列每月 USD200。

<a id="dev-cost-cleanup-1011"></a>
## 2026-10-11 dev 費用最佳化（Neal 批准）

- **Neal 10-11 原話**：「好，我想最佳化費用」。刪除項目與閒置逾時另以選項逐項確認：刪除「Premium registry＋它的 PE、NAT gateway＋公用 IP、兩個孤兒 PE」，閒置逾時選「A／B 改成 15 分鐘」。
- **依據**（Cost Management `ActualCost`，dev RG，10-04～10-11，合計約 USD209.8）：Hosted 運算 USD74.1、Foundry 專案底下的 Container Apps 環境 USD47.5，兩者都跟著使用量變動。固定開銷中，可以刪的是 9/12～9/13 舊 VNet POC 的殘留資源（那個 Foundry 帳號早已刪除，controller VM 已停機）。最近 8 天只有 A、B（以及一次 `fa-delegate-v1`）有回合。
- **已刪除**（逐項先讀回目標再刪，刪後讀回 0 筆）：
  - `pe-fasample-acr` 與 Premium registry `crcontosodemo01`（最後一次 push 在 9/13，只放 SMB／VNet 診斷映像），約 USD1.75／天。
  - 先從 `snet-agents`、`snet-controller` 解除 NAT，再刪 `nat-fasample-westus3` 與 `pip-fasample-egress`，約 USD1.1／天。之後如果要重開 controller VM，得先用 `infra/vnet` 重建 NAT。
  - `pe-fasample-foundry`（指向已刪除的帳號）與 `pe-fasample-file`（File Share 依 ADR-0002 已停用），約 USD0.45／天。`pe-fasample-vault` 保留：key vault 關閉了公開存取，只能從這個 PE 連進去。
  - 合計約 USD3.3／天，由 8 天實際費用換算，實際節省以之後的報表為準。
- **A／B 閒置逾時從 3600 秒改為 900 秒**：A 改為 v145、B 改為 v101（core `0b09441b`），都已 route 並 follow 完成，讀回 `idle_timeout_seconds=900`。官方 sync 只會把閒置逾時往上調，所以這次用同一個 sync 加上覆寫，只對 A、B 設定精確值；～～之後的一般 sync 會維持 900；代價是對話間隔超過 15 分鐘要冷啟動～～（已被下方 10-12 更正取代：A／B 現為 3540 秒）。
  - **10-12 更正**：上面「都已 route 並 follow 完成」只是讀回，當時沒有用實際回合驗證。10-12 中午 A 不收新回合（`NATIVE_SESSION_CHANGED`）：10-11 22:10 follow 建的 A session（…8d9a3d14）閒置被回收再叫醒後，平台回報的版本變成 v141，不是 FA 記錄的 v145，FA 依設計拒絕悄悄換環境。progress 以前也記過平台在 session 叫醒時換版本（3 次）；閒置改成 15 分鐘會讓叫醒更頻繁，增加碰到平台換版的機會。B 做了相同的改動，10-12 12:24 重跑官方 follow 時建了新 session（v101）並 ready，沒有碰到換版。
  - **恢復**（Neal 10-12 選「A／B 都改回 1 小時」）：用官方 sync 指定 3600 時，因為取較大值、定義相同就沿用舊版本，B 被改回舊的 v100，follow 失敗；A 那次在連進容器時就收到 503，沒有送出。改用同一個 sync 加閒置覆寫，設成舊版本都沒用過的 **3540 秒（59 分鐘）**，產生新的具體版本：B **v102**、A **v146**，route 和 follow 都 ready。
  - **實際回合驗證**（10-12，dev 8765）：A v146 一則 Learn 搜尋回合，經批准後完成（13:20～13:22，用了 1 個工具）；A 那張批准卡出現在輸入框上方的停靠區（#805）。B v102 一則回合被接受並執行，但先停在 Teams 連線卡：B 沒連 Teams，而且 12:31 按過的「先不用」只記在 VM 記憶體，換版到 v102 後就遺失（持久化修正見 #807）；Neal 13:30 再按一次「先不用」後，回合完成，回覆「B 正常」。A 舊環境的工作檔留在舊 session，新環境看不到；對話與 checkpoint 不受影響。
- **部署 follow 只叫醒 A、B、C**：其餘 Agent 仍會收到新版本，但不叫醒。原本 4 個 `fa-agent-*`（各 2 vCPU／4Gi、閒置 1 小時）每次部署都會白跑；粗估每次部署省約 USD1（推論：用 Container Apps 單價推算，沒有查 Foundry Hosted 的實際單價）。這只改了本機部署腳本，沒有寫入雲端。
- 上一節 `dev-cap-650-1011` 補充：起因還包括 10-11 的 Teams 短測；dev 規格維持現狀，沒有降規模（Neal 10-11 確認）。上述最佳化只刪除閒置資源並調整閒置逾時，A／B 的規格沒有變。

<a id="dev-cap-650-1011"></a>
## 2026-10-11 dev 上限 USD600 → USD650（Neal 授權）

- **Neal 10-11 原話**：「同意 Teams 短測，預算上限提高到 USD650」。
- **上限**：dev 共用上限由 USD600 提高到 **USD650**。停損比照原本的間隔，訂在 **USD630**，到這個數字就先停下來回報。
- **實查**（Cost Management `ActualCost`，RG 範圍，2025-10-11 起到 10-11 報表日，API `2023-11-01`）：`rg-faexample` USD506.60、`ME_cae-facontext-private` USD19.87，合計約 **USD526.5**。報表約晚一天，10-11 只出現 1.84；10-08～10-10 每日 RG 約 USD21.9、15.1、21.6。
- 起因：照每日固定開銷，約 10-13 會到原停損 USD580；另外還有 Neal 同意的 10-11 Teams 短測。dev 規格維持現狀（Neal 10-11 確認：只提高上限、不降規模）。10-11 的計費操作：dev 三次部署（`ff4b4cbb`、`7eb334d7`、第 25 版候選 `5fee7ec8`，各兩個 build、一次 API 換版、一次 Hosted sync）與一次 relay 新版本。
- 其他規則不變：不清帳；`budget_enforced=false`，這仍是記錄用的上限，沒有建立 Azure Budget；stage 照舊另列每月 USD200。

<a id="dev-cap-600-1010"></a>
## 2026-10-10 dev 上限 USD550 → USD600（Neal 授權）

- **Neal 10-10 原話**：「在遵守專案原則，且預算控制在600 USD的情況下，使用你的最佳判斷」。
- **上限**：dev 共用上限由 USD550 提高到 **USD600**。停損比照原本的間隔，訂在 **USD580**，到這個數字就先停下來回報。
- **實查**（Cost Management `ActualCost`，RG 範圍，2025-10-11 起到 10-10 報表日，API `2023-11-01`）：`rg-faexample` USD483.08、`ME_cae-facontext-private` USD19.03，合計約 **USD502.1**。報表約晚一天；10-05～10-09 每日 RG 約 USD15～52，10-10 只出現 0.09，估計當天約 USD20，所以 10-10 結束約 USD522。
- 10-10 的計費操作：connector 第一里程碑的兩個 build、一次 API 換版與一次 Hosted sync（[紀錄](progress.md#connector-m1-dev-1010)）。
- 其他規則不變：不清帳；`budget_enforced=false`，這仍是記錄用的上限，沒有建立 Azure Budget；stage 照舊另列每月 USD200。

<a id="dev-cap-550-1006"></a>
## 2026-10-06 dev 上限 USD450 → USD550（Neal 批准）

- **Neal 10-06 13:59 原話**：「上限提高到 USD550」。
- **起因**：fdbb 13:5x 查到 dev 到 10-05 為止約 USD370.5（RG 354.34＋`ME_cae` 16.14），10-06 結束估計約 USD395～400。每天的固定開銷約 USD20～24，其中約 5～8 是算在 dev RG 合計裡的 stage 費用：帳戶層級的 Hosted 拆不開；stage aml 可以按資源拆，但落在 dev 的 RG 裡。第 16 版的 dev 測試本身約 USD5～10；只要晚一天部署，固定開銷加上測試費用就可能超過原本的停損 USD430。
- **上限**：dev 共用上限由 USD450 提高到 **USD550**。停損比照原本的間隔，訂在 **USD530**，到這個數字就先停下來回報（總協調訂）。
- 其他規則不變：部署前後各查一次 ActualCost；不清帳；`budget_enforced=false`，這仍是記錄用的上限，沒有建立 Azure Budget；stage 照舊另列每月 USD200。

<a id="dev-cap-450-1006"></a>
## 2026-10-06 dev 上限 USD250 → USD450（Neal 批准）

- **Neal 10-06 08:2x 原話**（總協調轉達）：「dev 預算上限，我建議提高到 USD450。決定後才能跑第 15 版的 dev 測試。<--- 同意」
- **上限**：dev 共用上限由 USD250 提高到 **USD450**。依總協調指示，保守合計到 **USD430 先停下來回報**，不直接用到 450。
- **暫停解除**：開始第 15 版（main `46910b28`）的 dev 完整測試與退版演練。開始前和結束後各查一次 ActualCost，寫進測試報告；報表約晚一天，本批花費另外估算補上。
- Neal 沒有批准讓 t1～t4 不再自動換版，維持現狀；如果判斷會讓本批超過停損線，先回報，不自行改設定。
- 不清帳；`budget_enforced=false` 不變。這仍是記錄和追蹤用的上限，不是自動擋下的 gate，也沒有建立 Azure Budget。
- 10-05 的實查數字（約 USD358.6）見下一節。

<a id="dev-actualcost-1005"></a>
## 2026-10-05 dev 實查 ActualCost：已超過共用上限 USD250，暫停新的計費操作

- **實查結果**：Cost Management `ActualCost`，RG 範圍，期間 2025-10-06T00:00Z 到台灣 2026-10-05 23:40～23:46（API `2023-11-01`；報表約落後一天）。
  - `rg-faexample`：**USD342.90**
  - `ME_cae-facontext-private`（dev Container Apps 環境的基礎設施 RG）：**USD15.67**
  - 合計約 **USD358.6**，已超過 dev 共用上限 USD250。
- **先前各節的累計估計已作廢**，以本節實查數字為準。09-29 曾實查 RG 加 infra 約 USD107.45；之後各節的「≤USD125／250」「約 USD130／250」等，都只是沿用這個數字再加估，沒有重查。
- 帳戶層級的 Hosted vCPU＋Memory 費用記在 dev 和 stage 共用的同一個 Foundry 帳戶，**含 stage 的部分，拆不開**（總協調 10-05 唯讀查詢）。所以上面的 RG 合計可能含有一部分 stage 的 Hosted 費用。

`rg-faexample` 每日費用（USD，前三大服務）。下表只涵蓋 09-25 以後，共約 USD271.1；之前約 USD71.8。RG 累計在 UTC 帳單日 10-02 底約 244.7、10-03 底約 306.4；上限是按 RG 加 infra 計（09-29 時 infra 已有 11.22），所以合計在 **UTC 10-02 底已約 USD256 以上，在 10-02 那個帳單日就超過上限**（台灣時間可能落在 10-03 凌晨）。

| 日期（UTC 帳單日） | 合計 | 主要來源 |
|---|---|---|
| 09-25～09-28 | 每日 5.3～6.1 | Container Registry 1.8～1.9、Virtual Network 1.6、NAT Gateway 或 Container Apps 各約 1.1 |
| 09-29 | 8.30 | Container Apps 2.5、Container Registry 2.1、Virtual Network 1.6 |
| 09-30 | 14.67 | Container Apps 5.6、Foundry Tools 3.1、Container Registry 2.6 |
| 10-01 | 27.97 | Foundry Tools 11.8、Container Apps 10.7、Container Registry 2.5 |
| 10-02 | **98.96** | Foundry Tools 46.6、Container Apps 44.3、Container Registry 2.7 |
| 10-03 | **61.66** | Foundry Tools 27.6、Container Apps 26.6、Container Registry 2.8 |
| 10-04 | 25.74 | Container Apps 10.0、Foundry Tools 8.9、Foundry Models 2.1 |
| 10-05（未完整） | 10.81 | Container Apps 4.3、Foundry Tools 3.7、Foundry Models 1.0 |

依服務（同一期間）：Container Apps 114.48、Foundry Tools／Foundry Agents 112.16、Container Registry 46.85、Virtual Network Private Link 31.56、
NAT Gateway 23.98、Azure OpenAI GPT5 7.01、IP Addresses 2.66、Azure OpenAI 1.92；其餘每項都低於 USD1.1。模型費用合計約 USD9。

總協調 10-05 唯讀補查（10-01～10-05，依資源與計費項目）：
- 帳戶層級 Hosted vCPU＋Memory 約 USD104（dev 和 stage 共用，拆不開）。
- dev project 的 aml workspace vCPU＋Memory 約 USD87；stage project 同一項 USD3.8。
- Premium ACR 25.6、NAT 16.6、6 個 Private Endpoint 各約 3.7、`ca-fa-behavior-poc` 約 18。

處理：
- Neal 不在。依總協調 10-05 23:50 指示，**dev 暫停所有新的計費操作**：不重建、不 PATCH、不 sync、不跑模型回合，也不刪除或縮減資源。
- 第 15 版的 dev 完整測試與退版演練等 Neal 決定。
- 現有資源照常計費（10-04 約 USD26，10-05 未完整的部分約 USD11）。
- 不清帳；`budget_enforced=false` 與上限 USD250 都不變。提高上限或縮減資源要 Neal 批准。
- 查詢原件在 fdbb session `files\i-p1\d63\`（請求 `cost-query.json`；原始回應 `cost-by-service.json`、`cost-daily.json`；`cost-rg-total.json`、`cost-me_cae.json` 是事後從查詢輸出抄錄的兩個總額）。

## 2026-10-02 stage 環境另編預算（ADR-0008）

| 環境 | 上限 | 依據 | 備註 |
|---|---|---|---|
| dev（`faexample`，RG `rg-faexample`） | 共用 USD550（原 USD250→450） | Neal 2026-10-06 13:59（[原話](#dev-cap-550-1006)） | stage 帳面上另列預算；但帳戶層級的 Hosted 拆不開，stage aml 也落在 dev 的 RG 裡，實際上每天約 USD5～8 會算進 dev 的合計（見上）。 |
| stage（`faexample-stage`，RG `rg-faexample-stage`） | **每月 USD200** | Neal 2026-10-02 17:29（總協調轉達） | 常駐估算約 USD70～190／月（主要是 API 2 vCPU／4 GiB、min 1）。2026-10-07 起，feed app `ca-fa-feed-stage` 與工作台 `ca-fa-console-stage` 也常駐（min 1，Neal 08:20／08:3x 批准），閒置費率合計每月約多 USD11.66。每次自動部署 Azure 費用低於 USD0.1，CI 不跑模型回合。 |

- 兩者都是記錄和追蹤用的上限，不是自動擋下的 gate；沒有建立 Azure Budget 或其他雲端設定。
- stage 的 Hosted session、模型與 WebIQ 費用另計在 stage 這一行；模型部署和 quota 與 dev 共用，所以帳單需要依 project／RG 拆分。目前帳戶層級的 Hosted 還拆不開，算在 dev 的合計裡（見上表 dev 列）。
- stage 實際用量接近每月 USD200 時，回報總協調。估算來源與官方價格見 [stage 部署 runbook](../src/fa-shared-context-poc/README.md#stage-環境與-cicdadr-0008)。

## 2026-10-01 main 59658f5採用（Hosted A60／B17、取消保存、Responses、PoC）

本批沒有重查 `ActualCost`；ACR build、API PATCH、Hosted A60／B17、8765 重啟、模板 select、取消後追問、Responses a／b1／b2、#235 探針與 B PoC 的多數費用均**未量測**，不自行估算。已知數字只記 B PoC：10 回合使用 gpt-4.1-mini，約 **US$0.02**。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。版本與限制見[59658f5 採用紀錄](progress.md#2026-10-01main-59658f5採用hosted-a60b17取消保存responsespoc)。

## 2026-10-01 main 6cf71aa採用（Hosted A61／B18、SP v2 選入、截圖回合卡住）

本批沒有重查 `ActualCost`；ACR build、API PATCH、Hosted A61／B18、8765 切換、#242 截圖探針與 SP v2 八則費用均**未量測**，不自行估算。已知數字只記 FA 觀察：截圖探針到第 17 次模型請求時 stream 失敗；沒有 token 或 USD 換算。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。版本與限制見[6cf71aa 採用紀錄](progress.md#2026-10-01main-6cf71aa採用hosted-a61b18sp-v2-選入截圖回合卡住)。

## 2026-09-30 main 3e3af44採用（Hosted A59／B16、Responses去值診斷）

本批沒有重查 `ActualCost`；ACR build、API PATCH、Hosted A59／B16、8765 切換、模板 select 與 Responses 診斷費用均**未量測**，不自行估算。已知數字只記 FA 觀察：診斷回合使用 gpt-5.4-mini、1 次模型請求；沒有 token 或 USD 換算。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。版本與限制見[3e3af44 採用紀錄](progress.md#2026-09-30main-3e3af44採用hosted-a59b16responses去值診斷)。


## 2026-09-30 main 0d34211採用（Hosted A58／B15、fa-shell-v1）

本批沒有重查 `ActualCost`；ACR build、API PATCH、Hosted A58／B15、8765 切換、模板 select 與 shell smoke 費用均**未量測**，不自行估算。已知數字只記 FA 觀察：shell smoke 的模型請求為 ① 2、② 2、③ 1、一般追問 2，合計 7 次；沒有 token 或 USD 換算。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。版本與限制見[0d34211 採用紀錄](progress.md#2026-09-30main-0d34211採用hosted-a58b15fa-shell-v1)。

## 2026-09-30 main a3ac6c5採用（Hosted A57／B14、Responses縮回）

本批沒有重查 `ActualCost`；ACR build、API PATCH、Hosted A57／B14、8765 切換、B1c、Responses smoke、圖片交付與模板 select 的費用均**未量測**，不自行估算。已知量測只有 B1c 的 FA_MODEL_USAGE 觀察值：prompt 214 tokens、completion 6 tokens、cached 0；沒有換算 USD。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。版本與限制見[a3ac 採用紀錄](progress.md#2026-09-30main-a3ac6c5採用hosted-a57b14responses縮回)。


## 2026-09-30 live 部署、CAP-05 與 B 登錄

本批沒有重查 `ActualCost`；不自行估算 ACR build、API PATCH、Hosted 新版、8765 smoke、MEM-03、圖片交付或 B 登錄費用，未量測者一律記「未量測」。既有共用上限 USD250、`budget_enforced=false` 與舊 unknown／reserve 均保持，不清帳。

唯一有量測的是 CAP-05 桌面 live：gpt-5.4-mini 共 15 次請求，prompt 134,728 tokens（cached 64,256）、completion 4,265 tokens，約 **US$0.1**。57d269e 圖片 smoke、MEM-03、部署鏈各 batch、B 登錄與 WorkRegistry 寫入均未量測，不填估算值。版本與限制見[09-30 紀錄](progress.md#2026-09-30live-部署鏈cap-05-與-b-登錄)。

## 2026-09-29 Hosted47 skills與memory後續smoke

沒有重查`ActualCost`，仍保守估計約**USD130／250**；不是即時餘額或供應商硬上限。
真模型只有6回合smoke：FA記`model_requests`合計7（都是gpt-4.1-mini；B1～B5各1、D2 2），其中B5在API的`provider_token`階段失敗，沒有`FA_MODEL_USAGE`。S2 select與D3的claim讀取都沒有呼叫模型。
沒有ACR build、API PATCH或Hosted新版本；雲端變更只有模板select 1次（S2，已列入上批）；帳冊`budget_enforced=false`。
版本與限制見[後續smoke紀錄](progress.md#2026-09-29hosted47-skills與memory後續smoke)。

## 2026-09-29 main f4589df採用（Hosted47、v46開機失敗與capability縮回）

本批沒有重查`ActualCost`；報表有延遲，本批尚未反映。沿上批≤USD125加本批保守估算，約**USD130／250**；不是即時餘額或供應商硬上限。

ACR build共4次，都是CPU2：v46為`ds51` API（`9b357efa…`）約62秒、`ds52` Hosted desktop（`57653cb4…`，Hosted v46已建立但未採用）約3分；v47為`ds54` API約63秒、`ds53` Hosted desktop約3分。
今晚真模型請求估計約35次；本批紀錄列出的13回合，FA記`model_requests`合計24（gpt-4.1-mini與gpt-5.4-mini），另有1筆提交結果未知。模板select與reader核對都沒有呼叫模型。
API資源由1 vCPU／2Gi改為2 vCPU／4Gi（已核准）。
本批雲端變更只有：API PATCH 3次（v46 reader開機失敗1次、v47 reader、選版ENV 45→47）、Hosted新增2版（v46未採用、v47）、模板select 4次（含S2釘選）；帳冊`budget_enforced=false`。
版本與限制見[採用紀錄](progress.md#2026-09-29main-f4589df採用hosted47v46開機失敗與capability縮回)。

## 2026-09-29 main deafbf1採用（Hosted45、checkpoint與Playwright縮回）

兩RG `ActualCost`窗口自`2025-09-30T00:00:00Z`至`2026-09-29T09:21:38Z`：project為`96.2269458805561`、infra（ME_cae）為`11.22381108197849`，合計約**USD107.45**，各nextLink=null。
報表約延遲1天，fd76與本批尚未完全反映；保守估計**≤USD125／250**，不是即時餘額或供應商硬上限。

ACR各CPU2：`ds4y` API約51秒、`ds50` Hosted desktop約3分。
真模型只有smoke的unique提交，共8次請求（都是gpt-4.1-mini）：a1 1次、a2 2次、MCP 3次、縮回後兩回合各1次。模板select與reader核對都沒有呼叫模型。
WebIQ依CAP決定不計入共用上限，本批2次呼叫（`FA_CAPABILITY_CALL` ok）。
本批雲端變更只有：API PATCH 2次（reader、選版ENV 44→45）、Hosted新增1版、模板select 2次；帳冊`budget_enforced=false`。
版本與限制見[採用紀錄](progress.md#2026-09-29main-deafbf1採用hosted45checkpoint與playwright縮回)。

## 2026-09-29 main fd76890採用（Hosted44、desktop capability）

本批沒有重查`ActualCost`；報表有延遲，本批尚未反映。沿用上批約USD121.5，加本批保守估算，約**USD124／250**；不是即時餘額或供應商硬上限。

ACR各CPU2：`ds4v` API約58秒（被取代、未部署）、`ds4w` Hosted desktop約3分、`ds4x` API約65秒；另有`ds4t`（1秒即失敗）與`ds4u`（約26秒）兩次只讀parent image檔的短run。
真模型只有smoke的unique提交，約11次請求：v44新對話1回合記`model_requests=1`；Playwright 3次、WebIQ 5次（以上gpt-4.1-mini）；desktop 2次（gpt-5.4-mini）。
模板select、reader與secret核對都沒有呼叫模型。新deployment `gpt-5.4-mini`（GlobalStandard）閒置時不計費。WebIQ依CAP決定不計入共用上限，本批實際0次呼叫。
WebIQ回合輪詢時先轉`capacity_uncertain`，16:20唯讀讀回終局`failed`（`model_requests=5`），已含在上述約11次內。
沒有新增infra、IAM或排程；新增1個model deployment，API共PATCH 3次（reader、選版ENV 43→44、secretRef），Hosted新增1版，模板select 2次；只停止自己的smoke session、未刪除；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與限制見[採用紀錄](progress.md#2026-09-29main-fd76890採用hosted44desktop-capability)。

## 2026-09-29 main 2c6fce1採用（Hosted43、memory smoke）

兩RG `ActualCost`窗口自`2025-09-30T00:00:00Z`起：project至`2026-09-29T04:58:38Z`為`94.4320867254529`；
infra（ME_cae）之後查詢多次429，沿用至`02:36:06Z`的`10.98377860937267`；合計約**USD105.42**，各nextLink=null。
報表有延遲，本批build與smoke未完全反映，不是即時餘額或供應商硬上限。
原legacy reserve USD10、work reserve USD2及model pending USD0.10595保持，合計USD12.10595；再加尚未反映部分的保守估算，約**USD121.5／250**。

ACR各CPU2：877eaed `ds4p` API約48秒、`ds4q` Hosted約28秒；58eedd8 `ds4r`約54秒、2c6fce1 `ds4s`約50秒（後兩批只建API）。
真模型只有smoke的unique提交：877eaed新對話1回合及memory A／A′／A″各1回合記`model_requests=1`，新對話B記`model_requests=2`（含1次`read_reviewed_note`）；
memory設定、提案、bridge、模板select與reader核對都沒有呼叫模型。前後reserve／pending不變；FA帳內`model_metered`由21175增至25811 microUSD，只是FA計量，不當本批實付。
沒有新增infra、IAM或排程；API共PATCH 4次（3次換image＋core、1次選版ENV 42→43）、Hosted新增1版，只停止自己的smoke session、未刪除；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與限制見[採用紀錄](progress.md#2026-09-29main-2c6fce1採用hosted43memory-smoke)。

## 2026-09-29 main 9d5e086採用（Hosted42、在新環境接續）

**共用上限由USD150提高為USD250**，依據是使用者2026-09-28 21:18的核准（經總協調轉達）；仍是FA工作共用累計上限，不是每批各一份。本批停損線為保守合計USD235。
兩RG `ActualCost`窗口`2025-09-30T00:00:00Z`至`2026-09-29T01:19:51Z`：
project `93.5474295153841`、infra `10.92377040207386`，合計**USD104.47120**，各nextLink=null；MTD（2026-09-01起）同值。
報表有延遲，本批build與smoke尚未反映，不是即時餘額或供應商硬上限。
原legacy reserve USD10、work reserve USD2及model pending USD0.10595保持，合計USD12.10595；再加本批保守估算USD5，約**USD121.6／250**。

ACR `ds4m`／`ds4n`各CPU2、約43／27秒。真模型只有smoke的unique提交：新對話兩回合與在新環境接續一回合各記`model_requests=1`，SK-10草稿回合記`model_requests=2`（含1次tool call）；
舊對話送出為`model_requests=0`，模板inspect／select、準備新環境、reader核對與重啟後讀回都沒有呼叫模型。前後reserve／pending不變；
FA帳內`model_metered`由17349增至18960（重啟後讀回），SK-10後為21175 microUSD；只是FA計量，不當本批實付，本批仍約USD121.6／250。
沒有新增infra、IAM或排程；API revision重啟1次，只停止自己的smoke session、未刪除；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與限制見[採用紀錄](progress.md#2026-09-29main-9d5e086採用hosted42在新環境接續)。

## 2026-09-28 main c916a0f採用（Hosted41、M3 route）

兩RG `ActualCost`窗口`2025-09-29T00:00:00Z`至`2026-09-28T11:45:05Z`：
project `89.8842945186712`、infra `10.50359474956058`，合計**USD100.38788926823178**，各nextLink=null。
部署後12:12:20Z重查project仍是同值；infra重查遇429，沿用11:45Z讀數。報表有延遲，本批build與smoke尚未反映，不是即時餘額或供應商硬上限。
原legacy reserve USD10、work reserve USD2及model pending USD0.10595保持，合計USD12.10595；
再加本批保守估算USD3約**USD115.49384**，低於共用USD150。兩個smoke送出前的intent都以USD6邊際核對，當時保守合計約USD118.49384。

ACR `ds4h`／`ds4j`／`ds4k`各CPU2、實際約44／26／26秒。真模型只有smoke兩個unique提交，FA各記`model_requests=1`；
reader核對、舊turn進度GET、M3 probe與空槽讀取都沒有送出新的模型工作。前後reserve／pending不變；
FA帳內`model_metered`由16333增至17349 microUSD，未扣除同時段其他工作，只是FA計量，不當本批實付。
沒有新增infra、IAM或排程，只停止自己的smoke compute；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與限制見[採用紀錄](progress.md#2026-09-28main-c916a0f採用hosted41版本漂移修補m3-route)。

## 2026-09-28 main74b4bda採用與兩次模板選定

兩RG `ActualCost`窗口`2025-09-29T00:00:00Z`至`2026-09-28T08:19:55Z`：
project `89.4500360302325`、infra `10.44358905196655`，合計**USD99.89362508219905**，各nextLink=null。
這與build前07:43:22Z窗口的數字相同；報表有延遲，本批build與smoke尚未反映，不是即時餘額或供應商硬上限。
原legacy reserve USD10、work reserve USD2及model pending USD0.10595保持，合計約USD111.99958；
再加本批保守估算USD3約**USD114.99958**，低於共用USD150。

ACR `ds4f`／`ds4g`各CPU2、實際約52／28秒。真模型只有smoke兩個unique提交，FA各記`model_requests=1`；
兩次模板選定沒有呼叫模型或建立Hosted session。smoke前後reserve／pending不變；
FA帳內`model_metered`由15146增至16333 microUSD，包含同時段的平行工作，只是FA計量，不當本批實付。
沒有新增infra、IAM或排程，只停止自己的smoke compute；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與限制見[採用紀錄](progress.md#2026-09-28main74b4bda採用hosted40tz與兩次模板選定)。

## 2026-09-28 main166a1bb相容採用與兩回合smoke

兩RG `ActualCost`窗口`2025-09-29T00:00:00Z`至`2026-09-28T05:45:41Z`：
project `88.9982128826024`、infra `10.38358373653144`，合計**USD99.38179661913384**，各nextLink=null。
這次查詢起點比前幾次晚一天，未另補查2025-09-28；報表仍有延遲，不是即時餘額或供應商硬上限。
原legacy reserve USD10、work reserve USD2及model pending USD0.10595保持，
加本批保守估算USD3約**USD114.48774661913384**，低於共用USD150；兩個smoke提交前都以此值核對。

ACR `ds4d`／`ds4e`各CPU2、實際約35／50秒；一次失敗的reader PATCH在驗證階段回400，沒有建立revision。
真模型只有本批兩個unique提交，FA各記`model_requests=1`；兩輪前後reserve／pending不變，
FA帳內`model_metered`由14690增至15146 microUSD，只是FA計量，不當本批實付，也不把其他工作的差額歸給本批。
沒有新增infra、IAM或排程，只停止自己的smoke compute；帳冊`budget_enforced=false`，原unknown與預留未清。
版本與未啟用範圍見[採用紀錄](progress.md#2026-09-28main166a1bb相容採用hosted39兩回合smoke通過)。

## 2026-09-27 Hosted38採用與後續有界驗收

原部署線19:53:38窗口查回兩RG合計**USD94.194286072195435**，
保全部reserve USD10＋USD2／model pending USD0.10595，再保本次hotfix估算USD1為約USD107.30024。
ds48僅CPU2、19:58:42～19:59:10、600秒上限，owner模型0；20:10採用readback後本線才送兩個新38提交。
原API image/core、權限、policy與其他ENV不變；此處不是新的USD150額度。

20:49同兩scope純讀合計**USD94.194286072195436**。保前述未決責任及未完全反映的build/驗證估算，
新增規則批次前以USD3估算得到USD109.30024；其後Linux大狀態再保USD1估算仍約USD110.30024＜150。
兩個cold提交已收尾；另外三個規則提交亦收尾，兩批不重用或重送request。
沒有用submission數代替物理provider request count，也沒有把全域FA已計量差額當本probe實付。

Linux大狀態`ds49`／`ds4a`／`ds4b`在SDK前遇測試fixture缺件，分別約14／13／16秒；
`ds4c`補暫存fixture依賴後約26秒通過。四run皆CPU2、300秒、無真模型、無image build/push或部署設定更新。
這些報表仍有延遲，不代表即時硬餘額；任何後續工作需維持原累計費用、預留及unknown，不能清帳重算。

## 2026-09-27 隔離恢復驗證與 Linux SDK 根因重現

沿原兩RG、原隔離CLI身份於`2026-09-27T10:47:51Z`重查同窗
`2025-09-28T00:00:00Z`起的ActualCost：project `83.8336211252565`、
infra `9.778320621782916`，合計**USD93.611941747039416**，兩scope均無nextLink。
原legacy reserve USD10、work reserve USD2及model pending USD0.105950保留，
加本批USD1估算為約**USD106.71789**，低於共用USD150；帳單延遲，不稱即時headroom或硬cap。

四個新測試request、兩個獨立conversation；36／37各第一輪保存、第二輪failed，沒有重送。
只處理本批自己的reset／stop，原使用者journal與其他未決工作不動。
ACR `ds43`定義解析失敗、`ds44`～`ds47`為Linux scripted-provider實驗，
各CPU2／300秒run上限、實際約2／23／25／20／24秒，未啟真模型、build/push image或改部署。
原429及400查詢紀錄保留，之後改沿最新已發布窗口取得結果；未把400未保留的訊息當成已證根因，也未更動帳務policy或清零unknown。
修後Hosted採用仍須按當時最新費用與保留責任再核，不另取一份USD150。

## 2026-09-27 c18相容部署與Hosted37單次試用

最近一次成功的Cost查詢窗口為`2025-09-28T00:00:00Z`至`2026-09-27T08:51:49Z`，兩RG合計
**USD93.073705050868925**。17:27恢復後每RG各刷新2次都回429，沒有換身份或throttle bucket，
也沒有填造最新實付；明示沿用此已報值，並在唯一POST前重讀當前accounting。
原legacy reserve USD10、work reserve USD2、model pending USD0.10595保持；
加本批整體估算USD3（兩個900秒上限build、Hosted短啟動及一次mini模型請求）合計**USD108.179655050868925**，
低於共用USD150。17:46 reserve／pending與`budget_enforced=false`不變。
這不是即時headroom或硬停費保證；真模型只記本批一個request，不把並行工作的全域計量差額歸給本批。
實際版本與保留項目見[c18紀錄](progress.md#2026-09-27c18同源部署與hosted37試用入口)。

## 2026-09-27 原回條 reader 與拒絕卡片採用

同兩RG、`2025-09-28T00:00:00Z`至`2026-09-27T04:01:19Z`純讀：
project `82.4625492206114`、infra `9.603301036879415`，合計約**USD92.06585**，各nextLink=null。
原legacy reserve USD10、work reserve USD2、model pending USD0.105950及financial false都保持；
不重複加總FA已計量支出為實付。保留責任再加本次USD1估算約USD105.17180，低於共用USD150，
但報表有延遲，不宣稱即時headroom或供應商硬上限。

唯一受理的ACR `ds40`，CPU2、900秒上限、實際約48秒；首個不支援`--cpu`的CLI解析錯誤未送成build。
只有既有API的一次image/core/revision更新，Hosted36、原policy、ENV、scale、身份不改；
核對只讀原資料與入口，未新增聊天、模型、reject或cancel。12:45 accounting全部原components一致。
實際版本及限制見[採用短紀錄](progress.md#2026-09-27原回條-reader-與拒絕卡片已採用)。

## 2026-09-27 模板工作台的必要採用

本次只更新已審的模板API getter與恢復原本機工作台；一個2 CPU／900秒上限的ACR build
實際約47秒，驗證只做authenticated GET／readonly inspect，沒有新增聊天或模型請求。
原兩RG以`2025-09-28T00:00:00Z`至`2026-09-27T02:22:15Z`同窗讀回，
project `82.0112918931748`、infra `9.543296096054837`，合計約**USD91.55459**，各nextLink為null。
這不是即時剩餘額度；沒有擴建infra、改scale／Hosted36、IAM或排程，也未清除預留或unknown。
可用功能與實際版本見[模板採用紀錄](progress.md#2026-09-27模板工作台可實際下載與唯讀比對)。

## 2026-09-27 新對話v1的必要採用

只為#67做一個API reader build（900秒上限）及一個獨立短模型smoke，
不重建Hosted36、不增infra／scale／IAM或重裝policy。兩個原FA RG再次以相同窗口查詢，
已報`79.4864249871486`與`9.242885070210322`，合計約**USD88.72931**，各`nextLink=null`。
這是最新已回報資料，不是即時可花額度或停費保證；原legacy reserve USD10、
work reserve USD2、model pending USD0.105950和財務false均保留，未清零unknown。
部署順序、一次保存結果與只停止owned smoke compute見[本次進度](progress.md#2026-09-27新對話v1-default已最小採用)。

## 2026-09-26 最小部署更新

使用者重新明定：部署前採足以防資料損失、重送與明顯錯誤的最小機制，
不套用繁瑣流程、指標或證據gate。下方舊`BLOCKED`判斷是當時紀錄，
不是要求把所有歷史帳務核清才能更新既有服務。
共用USD150上限、目前身份／權限、資料與unknown保存仍不變。

本次刷新兩個既有FA RG的同一窗口：
`2025-09-27T00:00:00Z`至`2026-09-26T13:48:37Z`，USD、`nextLink=null`。
project已報`79.029198133712`，關聯infra已報`9.177879480198023`，
合計約**USD88.2071**。初次429後按正常間隔補查，沒有換身份或繞過配額。
報表有延遲，不宣稱這是即時remaining或供應商硬上限。

在此基礎上只更新既有API／Hosted A：兩個ACR build各設900秒上限、2 CPU，
實際約47／41秒；維持原replicas 1、CPU／memory，沒有新增infra／IAM／排程。
另做一次`gpt-4.1-mini`短聊天，模型請求1；測試session已讀回stopped，資料保留。
部署明細與結果見[進度](progress.md#2026-09-26最小可用部署與一次真聊天通過)。

新authenticated accounting route已實際GET200、`all_fields_present=true`；
原legacy reserve USD10、work reserve USD2、model pending USD0.105950均保留，
smoke後FA metered欄只增加本次紀錄，沒有把未知用量或支出清零。
這不把FA欄位與Azure報表重複相加當實付，也不補造舊工作的終局。
後續仍以必要的當前費用核對控制新增支出，不以全量稽核、原生資格矩陣或帳冊篇幅擋住可用部署。

## 原2026-09-24 preflight 紀錄

日期：2026-09-24。狀態：**必要部署／設定已授權；新計費執行仍為 `BLOCKED_FOR_NEW_BILLABLE_EXECUTION`，可靠 headroom 為 `UNKNOWN`。** 本頁是本次工作協調紀錄，不是產品預算 API，也不是硬性停費機制。

此為9/24凍結狀態；後續依各節的新批准與實際成本處理，不把完整帳單或額外證據流程當部署前置。
最新API帳務純讀及本線有界建置見[9/26部署接續](#2026-09-26-部署接續的成本與純讀)。

## 已取得與尚未確認的授權

使用者原文：

> 我授權必要的雲端部屬跟設定的動作，只需要確認預算不要超過USD 150

本頁只記授權日期及原文，不由工具 turn 的分組／收件時間反推精確說話秒數。執行採保守的 **FA 工作共用累計 USD150**，不是每個 session 或切片再取得 USD150，也不清零舊帳。

| 範圍 | 處理方式 |
|---|---|
| 必要雲端部署／設定 | 已授權；仍須核定當前環境、成本餘額、review 與適用驗收 |
| 額外真模型付費推論 | 不從「部署／設定」自動推定已批准；是否包含於同一 USD150 仍待明確確認 |
| IAM／RBAC／雲端存取權限變更 | 保持獨立批准界線，不因費用低或需要讀帳就自動提權 |
| 資料刪除、私人資料新用途、無關外部效果 | 未自動批准；保留既有資料／效果／unknown 契約 |

曾就必要付費推論提出範圍確認，但未取得使用者可用答覆；**未回覆不等於同意或拒絕**。因此先繼續本機實作與已允許的唯讀核對，不呼叫付費模型，不以換 key／身份／權限避過存取拒絕。

## 凍結 preflight 的結果

來源完成時間：`2026-09-24T09:27:56.842560+00:00`。兩個已確認與 FA 有關聯的 RG，使用相同 Cost Management 查詢區間：

`2025-09-25T00:00:00Z` 至 `2026-09-24T09:13:40.523150+00:00`。

原 RG 約於 `09:13–09:14 UTC` 取得，關聯的 infrastructure RG 約於 `09:24 UTC` 取得同截止資料。API `2023-11-01`、`ActualCost`／Daily／Cost Sum，按 ResourceId／ServiceName 分組；共兩份完整回應、402 rows，幣別 USD。

| 項目 | 原報表 USD |
|---|---:|
| 原 FA RG | 67.48947495941958346007 |
| 經 environment／managedBy 雙向確認的 infrastructure RG | 7.622599089881405 |
| **已回報合計** | **75.11207404930098846007** |
| 150 減合計的純算術差額 | 74.88792595069901153993 |
| **可靠可用 headroom** | **UNKNOWN／null** |

第二個 RG 的 load balancer／public IP 不能漏算。五個已記帳、但不在目前 ARM 清單的資源 ID 仍計入，沒有因刪除或別名歸屬而歸零。九個無關 RG 沒有展開帳務，不聲稱已排除所有未記錄的跨 scope 費用。

**USD74.89 不是已核發額度。** ActualCost 當期資料仍可能延遲、補報或重計，並非即時支出上界或 final invoice；support／稅等是否分攤到本工作也未核。本次沒有把 reserve、high-water 差額與帳務總額全部再次相加當新增費用。

## 為何仍不能開新計費批次

| 缺口 | 已知事實與限制 |
|---|---|
| 完整 current accounting | 原 Table entity GET 為 `403 TableErrorCode.AUTHORIZATION_FAILURE`。Public network Disabled 不能單獨證明拒絕只來自網路或 RBAC；未改角色、取 key 或新起診斷 job |
| 合法既有 API 的部分資料 | 純讀回 high-water USD55.701788、legacy＋work reserve USD12、`budget_enforced=false`，ceiling／remaining 仍 null；沒有 current model pending／metered 明細 |
| 歷史 pending／unknown | 舊 model pending/metered 數字只作歷史；四筆工作仍 `capacity_uncertain`，不推定免費、未執行或可重送 |
| 仍會累計的保留資源 | API／controller 常駐 replicas、ACR、網路與 storage／disk 等仍有保留成本；session stopped 不代表檔案與網路免費 |
| 在途 inventory | 初次 preflight 尚缺其餘57個 manual jobs；下節的獨立跟進已補齊目前可返回history，但不把終態／缺失endTime當費用結清 |
| 保留期間與遲報上界 | 近期最高日費不是可信上界；示例留款情境也不是支出許可 |

來源方的「暫建議新增計費額度 USD0」代表 **暫不新增承諾**，不是剩餘金額等於零，也沒有寫入或修改任何產品 budget／帳本。

## 有界唯讀跟進

獨立跟進完成於 `2026-09-24T09:45:52.385488+00:00`；新增70份 artifacts 已逐份核 bytes／hash，原81份及原 manifest 未改。57個原本未讀的 manual jobs 都完成目前可返回的分頁，觀察區間 `09:38:11.825400–09:39:07.244039 UTC`：

| 項目 | 結果 |
|---|---:|
| 完成分頁的 jobs／GET 次數 | 57／57 |
| 唯一 executions | 182 |
| `Succeeded`／`Failed` | 168／14 |
| 列出的非終態／未知 status | 0 |
| 缺少 endTime | 14（全部為 Failed） |

**未補造十四筆結束時間，也不推定它們零秒或費用已全入帳。** 原 controller 的62筆沿用前批時間；不同 resource 的順序讀取不是全環境原子快照，也不保證服務保存無限歷史。四筆 business `capacity_uncertain` 是不同層，沒有因此被結清。

另一次 ARM GET 與原部署 source audit 確認仍為 `chat-snapshot-0923`／core `ffc68461a0c3a3cfba4bf9cbfe15ee62de8635e64acf2131471d19aeb601ab2d`。Owner 將原 build context 的122個 module 重算到同 core，追實際 factory／routes／DTO，**沒有現成的完整 current CostLedger breakdown 純讀入口**。不靠不同 preflight 分支相減猜數字；不重新呼叫可能提高 observation/high-water 的分支。

這批只有58次 ARM GET；沒有新 business API／Table／Cost Management 查詢、job／model execution 或雲端寫入。**原 USD75.112074 帳務觀察並未刷新，可靠 headroom 仍 null。**

| 跟進原件（未附） | SHA-256 |
|---|---|
| `manifest.json` | `01fb81f581d6cd47ad22033d4f7f8119bdd16f81891abdd9ce107f5abe20396e` |
| `result.json` | `bd351214b12c3df7489eb036ffd8ad096103b73457895d72236d89a7d8881119` |
| 固定部署 source audit | `734b22af5cffcb03ffd9d7704980382179c035d2647711e8da0b58af84a28b22` |
| 最小存取選項 | `aba144582e62485b4922d91df88430d1d34bc724a3621032e17a4dc1a327ed9f` |

## 可繼續的下一步

這次唯讀查詢已停止，不無限追加 probes。既有有權且已可達私網的 data owner 若能合法提供特定成本 row 的去敏 snapshot，是一個待確認可用性的選項，不等於允許 agent 換身份。

另一個最小補法已在**本機 supporting slice**交付：PR #10 的 `c0c54ced752a793f8bc3e85c82ce0664ed7f668d` 新增狹窄的 authenticated accounting read projection，重用既有 human／tenant／team／current-policy／consistent-read，只回必要成本 metadata，不新增 IAM、keys、network、ledger 或寫入效果。最終130項本機測試通過，來源程式及README-only final `e78c270a824ffa9185a9a95f4d6ea3042906680e` 均已由Opus兩軸確認無P1/P2；欄位省略及CRLF hash口徑已補正，沒有改writer或重跑測試。**尚未部署，不能因此宣稱可讀到現在帳冊或發放額度**。

這個read projection刻意保留原row的presence：缺欄位回null；現行writer本身也可能省略零值counter／預設mode，所以null不只來自舊row，不能反推「目前就是0」。即使部署後仍須核可得到的完整性，不能為解除blocked而填零。Source review／單元測試也沒有證明現用Table的新存取已成功。

不得觸發 executions／模型、猜 ticket、借另一個身份／keys、改 RBAC／network、進 container shell 或開新 job 繞過403。保留期間與費用上界仍不能確定時，維持新計費 blocked，其餘本機修正與 review 繼續。

Azure budget alerts 只通知，不會自動停止資源。這份 USD150 工作上限不修改原 `budget_enforced=false`、high-water、reserve 或 UNKNOWN 歷史，也不以停用／刪除資料來偽造預算合規。

**暫停新增工作不會停止既有資源繼續計費。** 因此還需合法、有界的保留／收尾安排，不能承諾只要不呼叫模型，總費用就永遠不會超過 USD150。

## Verified against

- 來源基準：既有 operator 固定環境的唯讀結果，main 規範 `5d8ebb1fa9f3517a8cf8d92e326a47e446603fd3`；未部署新的產品版本。
- 收件完整性：初次 manifest 鎖定81份原件、跟進另70份，協調者逐份核 bytes／SHA-256，另核報告及 Decimal 加總／算術差額；沒有再發一次原 Cost 查詢，也未公開 raw 帳務、資源 IDs、token 或本機路徑。
- `REPORT.md` SHA-256：`376396019cee42bf04d71439b1282f0989f03d6c2679b3f56bd2339337b9d196`。
- `reconciliation.json` SHA-256：`acbc086cd90a97112c96f0d08472ac6ccce9cc33053ecd171641ed50c5b2918d`。
- `manifest.json` SHA-256：`33324f8c253ec030e97812dba76182a4b919d7531dedb49eb38dd470de56a891`。
- 限制：本次沒有 build／deploy、native job／model/session 啟停、雲端配置／權限／帳冊寫入。普通 metadata／storage 讀取可能有正常交易費，不宣稱 Azure 成本絕對為零。
- 本機支援碼來源：PR #10 固定 source；safe handoff SHA `973704527f566ad91711a039d006489aacbbec25b14cacff8cf4ef301f87ef22`、最終JUnit SHA `7b9cfa58abf302588c8648e7aff10584e0d0d4776d9fba462bb04101e7152bed`。協調者只核凍結原件／130結果與review，不重跑該批或宣稱已部署；原collection缺依賴／fixture／RED歷史由handoff保留。
- 官方查閱日 2026-09-24：[Cost Management 資料](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data)明示 open-month estimate、不同計費類型的延遲；[Budget 說明](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets)明示 alerts 不停止 consumption。官方通常時間不是這個帳戶的已驗遲報上界。

## 2026-09-25 五類 runtime 工作線唯讀補查

使用者在五類 runtime 工作線交辦完整 Target State 實作及 evaluation，授權必要雲端部署／操作不超過
USD150。此線仍採 **FA 共用累計上限**，不是另一筆 USD150；未自動包含 IAM／RBAC、資料刪除、
私人資料新用途或排程變更。原記帳／未知效果與上述 9/24 觀察保留，不用這次批准清零。

9/25補充原文：

> 我授權必要的雲端部屬與操作，只要預算不超過USD150

本工作線將同一交辦中的evaluation及「必要的雲端操作」解讀為包含達成本次目標所必要的付費模型／Memory評測，
不再沿用9/24「只有部署／設定，所以付費推論未確認」作為本線的批准缺口；舊記錄只代表當時狀態，不回寫。
這不是任意模型／資料用途的全面授權：執行前仍須固定適用模型、資料、用量範圍及共享成本，
而目前 **headroom UNKNOWN** 的實際阻塞仍在，因此這次沒有新增付費推論。

已使用 runbook 指定的既有隔離 Azure CLI 登入，唯讀核原 FA RG 與 Container Apps environment
所指 infrastructure RG。未切換身份、改權限、建立 job 或執行模型。
第一次 Cost Management 查詢沿舊起日到本次時間，因超過一年而回 `BadRequest`；
沒有因此得到成本數字。第二次將起日固定為 **2025-09-26T00:00:00Z**，終點為此次保存的 query timestamp，
兩個 RG 各查一次 `ActualCost`／`Cost Sum`，API `2023-11-01`，回應 `nextLink=null`、幣別 USD。

| 有界查詢結果 | USD |
|---|---:|
| 原 FA RG（16:50:09 +08:00 讀回） | 72.3773112350024 |
| environment 指向的 infrastructure RG（16:50:22 +08:00 讀回） | 8.28266409645788 |
| 本次窗口已回報合計 | 80.65997533146028 |
| 可靠 headroom | UNKNOWN／null |

此窗口與 9/24 不完全相同，**不宣稱整個專案 lifetime total，也不直接用兩份總額相減當本日費用**。
ActualCost 仍可能延遲，完整 current accounting、四筆舊未決工作及保留成本上界未由這兩份讀回解除；
不能把 `150 - 80.65997533146028` 當已核發可花額度。因此本線未新增計費執行。

Payload 及兩份回應留在本 session artifacts；原 BadRequest 保留於當次工具紀錄。
不隨 Git 發布 resource IDs、登入資料或原始帳務。
本次沒有部署／帳冊寫入；原 `budget_enforced=false` 與前件狀態不改。

## 2026-09-26 接續工作階段的有界唯讀補查

使用者於13:42重新交辦完整Target State，其中雲端批准原文為：

> 我授權必要的雲端部屬與操作，只要預算不超過USD150

本次沿上節的判讀（不是原文引句）：必要評測包括在目標內，但須先核共享成本；
不是每個工作階段另有USD150，也不擴為IAM／RBAC、刪除、私人資料新用途或排程變更。

使用原專案隔離`AZURE_CONFIG_DIR`及明確subscription，先唯讀核原FA resource group與
managed environment回傳的infrastructure group；沒有切換身份、列key或讀container私有檔案。
兩個Cost Management查詢共用窗口 **2025-09-27T00:00:00Z至2026-09-26T06:26:25.8090347Z**，
API `2023-11-01`、`ActualCost`／`Cost Sum`／無分組，兩份都是單一USD row且`nextLink=null`。

| 查詢完成時間（UTC+08）／範圍 | 已回報USD |
|---|---:|
| 14:26:47／原FA RG | 77.2592114233808 |
| 14:26:49／environment所指infra RG | 8.942857679608274 |
| Decimal合計 | **86.202069102989074** |
| 可靠headroom | **UNKNOWN／null** |

窗口與9/25不同，不作lifetime total、每日增量或可花額度。API配置讀回仍是
`chat-snapshot-0923`，min/max replicas均1；這只證當時image／scaling配置，不證工作已停止。
本批**未讀current business accounting、未重試Table403、未核清四筆舊unknown或遲報／保留費上界**，
因此維持新計費執行blocked；不能拿`150 - 86.202069102989074`發放額度。
未起模型／job、部署、改設定／權限或刪資料；既有資源仍可能繼續計費。
Environment關聯及API image／scaling僅保留CLI投影、session紀錄及去敏`summary.json`，
不是另存完整ARM原始回應；summary SHA256為
`3b4be8f8a55734adb8816d4ff3fc0249e6477aadd80815433e9534802436ee6b`。

查詢payload與raw回應留於受控session，不在Git公開scope IDs或帳務原件。
Query SHA256 `7c27c1cfcea8a3ca2c6d4854739c5cc85eac69b059d5d107cf46307fc8a72afd`；
project／infra回應SHA分別為
`dc1431f8b1771bda6f99fb1085f27d92fdf95925e3d73f51f4cc7a6513b4bcb7`、
`8ccc56708c691daa62c1c1f1f40dad3205c9fc42f264b2e87ea1ae479f8c9caf`。
本日另唯讀核[官方成本資料說明](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data)：
已回報成本在invoice前仍為估計；文件的一般延遲描述不是本環境的即時支出上界。

## 2026-09-26 部署接續的成本與純讀

本session使用者重新授權必要雲端部署／操作、共用累計USD150，並要求最小機制優先。
本線只做三筆600秒timeout的API image建置，未新增模型、Hosted session、replica或權限。
不是每session另開額度；保留原預留、未決請求與`budget_enforced=false`，不清零或退款。

同一查詢窗口2025-09-27至2026-09-26T13:49:26Z，原RG讀回USD79.029198133712；
infra RG兩次429沒有得到新數字，未當零。改以官方subscription query、只過濾同兩RG，
完整回傳project USD79.029198133712、infra USD9.177879480198023，
`nextLink=null`，**已回報合計USD88.207077613910023**。不是即時硬上限或任意可花額度。

22:19原身分GET新API的`/work-policy/v1/accounting`成功，所有必要欄位存在：
high-water USD55.701788、legacy reserve USD10、work reserve USD2、
model pending USD0.105950、metered USD0.009782；reservation_count=1，財務阻擋仍false。
這是讀取原帳冊，不刷新其舊成本觀察、不把pending改為已結清，也不把它與ActualCost相加當新支出。
API的`headroom_resolved=false`保持；後續計費仍須看共享支出、原預留及適當保留額。

本線`ds3t`／`ds3u`為FAILED，`ds3v`為Succeeded，均2CPU，合計約110秒；
依[ACR公開價格](https://azure.microsoft.com/en-us/pricing/details/container-registry/)的
USD0.0001／CPU秒，建置compute估算約USD0.022，非已出帳金額，未包含一般storage／network費。
新image `e0cca62e`未部署；另一工作線已更新API，本線更新前核對即停止，未送出PATCH。
沒有把單一建置成功或帳務GET當成完整部署／模型品質驗收，也沒有擅刪未使用image。
