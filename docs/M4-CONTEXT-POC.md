# M4 Context 品質與時間 POC

## 最新狀態：checkpoint 缺口已於 A18 真正 Hosted 補驗通過

2026-09-16 08:36 +08，使用者取消先前操作次數／session／版本等額度，授權六項缺口的相關部署與完整驗證，**總US$100不變**。產品授權、防重送、必要材料保護、四槽及每義務最多兩次模型補交限制不取消。

新的`m4-context-01-r4`只補`long-resumed`：A18保存未完成checkpoint → 真正stop／idle → 同Hosted與SDK session換process → 重新讀取context → 完成結果。原draft receipt未重送，Work僅draft／final各更新一次（revision2／3）。四個答案與實際來源均通過原checker及人工核對，SDK工具context bytes等於native下載原件。**缺口1已有實環境PASS；不是將原r3改為PASS，也不是重跑完整五案。**

| 本次補驗 | 實際值 |
|---|---|
| Hosted版本／映像 | A18；`sha256:6a6c2caef7464d7eefe5c21183a05f2026212fdb98eeef9bab42916ebd878bd7` |
| Memory | 沿用Store03既有長歷史，不重種fixture、不修改oracle |
| SDK／process | 同SDK session；process由`99bfcbb008694388b5590371ece429db`變為`fca1825aa37845fcb937e76887e7ab7b` |
| Context來源hash | `501645bcbcd16b44f13aa024ed5778b3b976b876f07d47e55edeb139e0e6e053` |
| 模型使用 | 本次8次；實際Gateway counter43→51，補核對原r3未收齊的7次 |
| 每次input tokens | `650, 2470, 2786, 3249, 3700, 5752, 6069, 6526`，全部≤8192 |
| Context／成果時間 | context1310.193282ms；案例觀測62312ms，非p95或十倍比較 |
| 收尾 | 本次17 invokes、1 start、1 resume、3 downloads、1新session；包含preflight的5次stop全部保留，最後owned session已idle |

本次operator最初把成功stop的CLI文字當JSON解析而停止，當時尚未start模型；保留原件後僅唯讀核對A／B idle，再接續同一已建session，沒有重新建立／重種資料。失敗preflight的active時間未完整量測，標unknown而不填0；接續案例保守active70.374秒。證據在ignored `.azure/six-gap-m4-native/`及同目錄`parent-verification.json`；舊manifest／ledger不覆寫。

以下保留原r3及首批歷史；其中「checkpoint未證實／待批准」為當時狀態，不能覆蓋本節的新增證據。

## r3 歷史：四案通過，checkpoint 案 incomplete 後停止

2026-09-15 17:03 +08，使用者要求總 **US$100 不變**，其餘相關操作上限提高為原來三倍，目標是完成 M4、實作行為 v1.2，再交付可親自操作的 UI 或 Foundry 入口。最新 r3 已完成四案品質／来源驗收，第五案在 checkpoint 前回 `incomplete`，已依停止條件清理。**M4 五案仍未全部通過**；原首批與 r2 的 FAIL／停止紀錄全部保留。

### 收尾與下一階段的界線

2026-09-15 19:29 +08，使用者同意按[原件與權威儲存研究](FA-DATA-STORE-RESEARCH.md)實作，並要求先收尾 M4、再開始 v1.2。先固定本批既有結果與程式基準，再以隔離／opt-in 切片補原件、來源及投影，不把新來源回讀機制塞回原五案冒充 Memory 驗收通過。

本批執行已停止，里程碑為 **PARTIAL（四案通過、一案 incomplete）**；原始五案驗收仍為 `FAIL`、`full_scenario_pass=false`。checkpoint 案的根因與最後用量未證實，不以更換資料庫作診斷，也不把「收尾」解讀成允許盲重跑。

本地已核對原 r3 收尾 manifest 的 804 份原件及最後本機 gate 的 48 份 source hashes，全部相符；此次沒有 Azure 呼叫或重跑模型。新增[r3 去敏 catalog](evidence/m4-20260915-r3-closeout.json)，保留原時間點、部署 digest、未結事項及原件指紋；[首批 catalog](evidence/m4-20260915-closeout.json)不覆寫。

**M4 程式基準已 commit／push：`1ca1c762050cdcafb51484b5155ad5b65503cd65`，遠端 read-back 一致。** 使用者於2026-09-15 21:18 +08明確授權；起始 HEAD `dcaa7192bd6715921870195fbf75914bbcb59ba5` 只有既有文件整合。本次限定33檔M4白名單，排除未獲發布授權的v1.2切片、原始Memory／SDK trace、私人附件與`.azure`。固定code SHA的前提已滿足，已有離線core保留，不重新從零開始；後續正式能力仍需實作與環境證據。Git發布不新增雲端或模型額度。

發布tree `8534cba7d0b93c869ac3922061a0ec8c81dfb1a9`已在不含六個v1.2 core／test檔的隔離副本完成1114項離線回歸。初次副本遇到Windows長路徑與未指定既有runtime位置；只修正執行環境後通過，沒有改測試或runtime原碼，也不是新增Hosted證據。

最後映像的 source freeze 包含未接線的 `behavior_store.py`／`behavior_operations.py`，但 M4 runtime 沒有 import 它們；後加入的 `behavior_delivery.py` 不在該映像內。擬議的純 M4 發布會排除這些 v1.2 檔案，因此不能把該白名單的 Git tree 宣稱成最後映像的完整逐檔重建輸入；真正已執行映像仍由既有 image digest 與完整 source freeze 識別。

### r3 最後原生結果

| 案例 | 判定 | Context bytes | Context ms | 端到端 ms | 每次模型 input tokens |
|---|---|---:|---:|---:|---|
| `short-cold` | PASS | 4154 | 1440.609662 | 48934.886694 | 646, 2109, 2445, 2920 |
| `short-warm` | PASS | 4154 | 2368.384280 | 45486.150503 | 646, 2113, 2398, 2812 |
| `long-warm` | PASS | 5822 | 1170.467439 | 47101.035118 | 646, 2470, 2812, 3290 |
| `long-new-conversation` | PASS | 5834 | 1906.260757 | 47346.782207 | 646, 2471, 2811, 3304 |
| `long-resumed` | INCOMPLETE，未進 checkpoint／resume | 未完成收集 | 未完成收集 | 未完成收集 | 最終用量未知 |

四案均已核對實際 Work、Memory 及批准 SOP 來源，答案分別為 `Mira`、`compact`、`cobalt`、完整 `APPROVED_POLICY=V3`，且通過原 collector 的 scope、來源、tokens、context 大小與 session／conversation 條件。不是只接受模型自述。

第五案最後原生 status 為 `incomplete`、`task_verified=false`、`checkpoint_wait_observed=false`、`released=false`、`error=null`、六次工具呼叫；收尾當時尚未取得最後 SDK trace，當時不對根因作判斷。後續有界取證與修復見下節，仍不能說是恢復後遺失記憶，因為原案尚未 resume。沒有重送原 start 或改寫原判定。10:57 UTC 完整原生分頁顯示 A102／B58，共 160 筆皆 idle。

r3 自己用了 SDK starts 5、resumes 0、native invokes 53、downloads 8、Memory updates 4、stops 3、active 475.562963 秒。跨批累計 models 最後成功讀值為 **36／180 的下限**，是在第五案之前；第五案後用量未知，不當成零或最終總數。累計新 sessions 6／6、Stores 3／3、Memory updates 12／12、API revisions 3／3 已用完，不能另建隔離完整批次。完整原件與 804 份檔案 hash 保存於 ignored `m4-r3-closeout.json`（SHA256 `c06cf1fa62b0630281c73cf27c6dcbc3c72c905ef4e2321f59cc58e97b880819`）；原 ledger 的 `aborted=true`、`closed=false` 不改写。

### 六項缺口工作：checkpoint 已定位，修復尚未上雲

使用者於2026-09-15 21:39 +08要求補六項缺口後，只對原失敗session下載`state.json`與`sdk-outcome-0.json`兩份既有檔案；沒有invoke、SDK start／resume、Memory update或模型呼叫。檔案操作會喚醒session，因此另用了1次自有cleanup stop並確認idle，不把file download冒稱零compute。新讀取帳本保存於ignored `.azure/six-gap-m4-diagnostic/receipt.json`，接續原累計用量，不改舊ledger或歸零額度。

SDK trace SHA256為`d4a26b33665aae46b1fafc404dc9f4c40285b1deedf3e7e48bbf987f2e49c9b6`，已對回原state保存的bytes/hash。實際順序是：`read_context`成功 → 四次試寫`result.txt` → 一次`update_work step=final`，後五次皆被checkpoint前禁止final的護欄拒絕。沒有寫draft、沒有接受Work更新，也沒有呼叫`checkpoint_and_wait`。模型只看到`Custom tool failed`，最後直接回文字答案，故工作保持incomplete。

已確認的程式問題是**階段不相符的工具／指示，以及缺少可用的錯誤回饋**：checkpoint前仍提供finalization工具與先寫result的通用指示，`ASSISTANT_CHECKPOINT_REQUIRED`又未轉成安全的公開操作指引。不能從此推定模型內部為何選錯步驟，但不需把這次checkpoint缺口歸因於Memory或資料庫。

工作樹已修正：checkpoint前僅提供draft寫入／更新schema、不提供`finish_task`；分開準備checkpoint與恢復後完成的指示；只在M4路徑加入不含私人內容的修正提示。恢復後不再提供checkpoint工具，仍須重讀context、保留原draft receipt而不重做副作用。原fixture、oracle與成功門檻不變。

回歸先重現4項失敗，再有111項M2／M4相關檢查通過；新階段測試6項亦通過，包含真正本機SDK在pending checkpoint中斷後，以同SDK session、新client與新工具階段完成，draft只更新一次。這些是離線／合成HTTP證據，**修復尚未部署或通過新的Hosted單案**。既有session額度已滿，新增有界單案仍須明確額度；不能回填原r3為PASS。

補取的SDK trace有7次`model.call_start`及7筆usage；這是原失敗案的既有用量，不是本輪新模型呼叫。Gateway最終counter尚未讀回，原36下限與最後精確總數未知的區別仍保留。

### 擷取與操作器修復歷史

- 依[官方 Memory 設定](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)，僅新增通用 `user_profile_details`，要求保留具名主體及具體偏好值；不植入 oracle、不改 fixture、模型或 embedding。獨立診斷 scope 的一次原生 update 已保存 `Agent A privately prefers the cobalt theme for Project Lyra.`，只證明這次擷取改善。
- 新隔離 Store 為 `poc-m4-context-02`；API revision `ca-facontext-poc--m4-2`，image `sha256:d0502b3d02e101e397b0e5ca3d3706f9760944011ee24c9a18c8d1a7d6513510`。只部署 API，Hosted 沿用 A16／B10／原 image；沒有新增 RBAC、secret、模型 deployment 或放大資源。
- 原 M4 counter 與前四筆模型 attempt 保留，以精確 amendment `m4-threefold-20260915` 增加 120，累計上限 180。新 Store 的 seed receipt 有獨立 namespace；不是重設舊 counter 或把舊 operation 指向新 Store。
- `m4-context-01-r2` 已完成 B 私人正向控制，但 A session 建立後被操作器的 inventory 檢查擋下；尚未啟動任何 SDK 案例。原因不是版本變更：CLI 雖指定 `--limit 1000`，服務仍每頁最多 100 筆，CLI 又丟掉實際回應的 `has_more`／`last_id`，因此漏掉第 101 筆 A session。直接 show 正確綁定 A16，該階段已自動 stop 並確認 idle。
- 已以[原生分頁讀取器](../controller/foundry_session_inventory.py)處理 `last_id`／`after`，缺少明確終頁、重複 ID 或 cursor 不一致一律拒絕。17:45 +08 真 GET 讀到 A `[100, 1]`、B `[57]`，共 158 筆皆 idle；這是有界分頁觀察，不宣稱交易式瞬間快照。
- 另存 `m4-context-01-r2-continuation` 接續帳本與操作器，保留原 r2 的 `aborted=true`、原件及全部累計計數；只重用已確認的 A／B sessions 與已完成的 B seed／control，沒有重送 unknown、另建 session 或重播原 seed。該接續已完成前三案並在第四案品質 FAIL 後停止，詳細結果如下。

### r2 接續結果與 r3 修復

人工已核對四案實際 context 原文：目前 Work 支持 `Mira`、Team Memory 支持 `compact`、A 私人 Memory 明確支持 `cobalt`，引用的批准 SOP 內容確實為 `APPROVED_POLICY=V3`。前三案所有 collector 檢查通過；第四案只在 `policy_marker_correct` 失敗，模型寫成 `V3` 而非完整標記。它使用正確批准版本、沒有發現 B 私人內容，但仍不符合既定精確輸出要求，不能放寬為 PASS。`long-resumed` 未執行，失敗 cleanup 已成功且 `active=null`。

| r2 案例 | 品質結果 | Context 耗時 ms | 端到端 ms | 每次模型 input tokens |
|---|---|---:|---:|---|
| `short-cold` | PASS | 1730.590844 | 49120.602131 | 630, 2223, 2496, 2906 |
| `short-warm` | PASS | 2175.258712 | 47274.419785 | 630, 2224, 2499, 2909 |
| `long-warm` | PASS | 2735.372461 | 49050.719738 | 630, 2732, 3072, 3543 |
| `long-new-conversation` | FAIL：標記縮寫 | 1734.748728 | 49689.227819 | 630, 2734, 3013, 3432 |
| `long-resumed` | NOT_RUN_AFTER_FAILURE | — | — | — |

修復在不可變 `result.txt` 與共享 Work 寫入前檢查既定 JSON 格式，並在有當前 context 時核對完整標記、批准／版本／內容 hash 及引用；`finish_task` 再核對。錯誤只給通用格式指示，沒有硬編碼 `V3` 或其他 oracle，也不自動補寫答案。輸入不足仍不通過；原 r2 錯誤成果不覆寫。

API `ca-facontext-poc--m4-3`、A17／B11 已完成部署與原生 definition 核對；仍是相同 agent GUID／MI、資源大小與模型。API image 為 `sha256:0ab4839668b8b455ac203637ccbd1fd5bfbaa370fb5f32cb62b394f973888ba4`，Hosted image 為 `sha256:b68857acd95100247c28c2e6561e0d3d103fc9c02a7135ddb80dbfedf6ddfa78`。API 只改 image、Store02→03、revision suffix；180 次模型總額沒有再增加。

`m4-context-01-r3` 在最後一個全新 Store `poc-m4-context-03` 重新依短→長順序執行五案，避免用已含長歷史的 Store02 假冒短歷史。使用最後兩個新 session 額度與四次新 Memory update，全部列入原累計帳本。原首批、r2 分頁停止及 r2 品質 FAIL 分別保留。18:47 +08 的原生 preflight 核對 A101／B57 皆 idle，隨後執行 r3；最終結果見本頁開頭，不把 preflight 的 idle 當執行後收尾。

**分頁證據更正：** 首批及先前 preflight 的 CLI 原件只證明當時列出的 sessions 狀態；A 恰有 100 筆時，不能單靠 `--limit 1000` 與缺少 token 宣稱全域 inventory 完整。原收尾檔的完整性判定保留作歷史，但該推論不再採用。兩個 owned sessions 的直接 show／stop／idle 證據不受影響；17:45 的原生完整分頁只支持該次新觀察，不回填舊時間點。

新版累計上限為 builds 6、API revisions 3、每個既有 agent 新 versions 3、新 Stores 3、新 sessions 6、SDK starts／resumes 15／3（合計 18）、模型 attempts 180、native invokes 300、stops 15、downloads 36、Memory updates 12、每次 update polls 18、active 5400 秒。歷史用量不歸零；保留最後 180 秒及最後一次 stop 清理。M4 仍序列執行；v1.2 的四槽及每 obligation 兩次模型補交不是操作額度，沒有乘三。

10:57:19 UTC 延遲 MTD 為 US$19.57181250194590777，仍保守採 US$27.079258133377022439，M4 預留 US$10 包含在 US$100 內。累計帳本為 ignored `m4-expanded-ledger.json`；其中 model attempts 只到最後成功讀值 36，第五案後尚未核對，不將已讀下限當最終帳單。

行為 v1.2 已整合控制儲存、不可變操作／授權及 admission／obligation／outbox 三個隔離 core，合計 187 項本機相關檢查通過；native admission、send、execute 仍拒啟用，沒有真 consumer 或 API／Hosted 接線。本機測試不等於 Azure EGT 或 B01–B12 PASS；後加入的 delivery core 不在本次 M4 映像內。完整行為實作與使用者驗收入口尚未交付。F18–F20／HOST R1–R3 維持 OPEN，R4–R6 為 ADVISORY。

## 首批歷史：品質 FAIL 並停止收尾

**2026-09-15：本批已依失敗停止條件收尾，品質未通過。** 已部署隔離的 M4 API／Hosted 接線，完成 B 私人正向控制、A 跨私人範圍拒絕及真正的 `short-cold` 案例。第一案未找回 A 的 `cobalt` 偏好，模型又填入不受來源支持的替代文字；操作器立即停止，其他四案未執行。這是 **`FAIL`，不是 M4 PASS**，也不是十倍效能或完整長期使用驗收。

本批沿用[原五案與有界批准](POC-ANALYSIS-AND-PLAN.md#m4-第一切片離線準備完成雲端尚未執行)，沒有新增 UI、模型對照、四 agent、Teams 或 BR-01 gate。M2 UI 缺口、M3 歷史 FAIL／unknown 及原計數不變。完整原始 Memory、SDK trace、工具日誌與批准資料仍在 ignored 目錄；本文僅引用必要的合成 fixture 片段，可提交的摘要見[去敏 catalog](evidence/m4-20260915-closeout.json)。

## 結果

| 項目 | 實際結果 |
|---|---|
| 隔離部署 | 新建唯一 `poc-m4-context-01`；API `ca-facontext-poc--m4-1`，A16／B10；既有 Store、M2／M3 設定與資源大小不變 |
| B 私人正向控制 | B 在自己的私人 scope 真正找回 `saffron`，不是只驗 A 沒有看見一筆從未存在的資料 |
| A 跨私人拒絕 | 原生 Hosted 呼叫內層回 `403 PRIVATE_TARGET_DENIED` |
| `short-cold` | 真正從 Hosted native idle 啟動；SDK 工作完成、原生檔案與來源可核對，但品質 FAIL |
| `short-warm` | `NOT_RUN_AFTER_FAILURE` |
| `long-warm` | `NOT_RUN_AFTER_FAILURE`；長歷史 fixture 未提交 |
| `long-new-conversation` | `NOT_RUN_AFTER_FAILURE` |
| `long-resumed` | `NOT_RUN_AFTER_FAILURE`；本批沒有新的雲端 checkpoint／resume 證據 |
| 收尾 | A 的失敗 cleanup stop 成功；16:54:41 +08 CLI 列出 A100／B56，共 156 筆皆 idle；原先完整性推論已依上方分頁更正撤回 |

`short-cold` 的四項答案：

| 欄位 | 預期 | 實際 | 答案／來源裁決 |
|---|---|---|---|
| `decision_owner` | `Mira` | `Mira` | PASS；引用目前權威 Work revision 1，不採過期 owner |
| `report_layout` | `compact` 等已列明同義詞 | `compact` | PASS；Team Memory 來源支持 |
| `private_theme` | `cobalt`／`cobalt blue` | `Agent A internal preferences` | FAIL；引用的 Memory ID 存在，但內容不支持所需偏好 |
| `policy_marker` | 精確 `APPROVED_POLICY=V3` | 精確 `APPROVED_POLICY=V3` | PASS；引用原已批准的固定 SOP 版本 |

原生 collector 已交叉核對 request／session／process／SDK binding、Memory update completion、工具呼叫 ID、SDK completion 的真正 context 原文、檔案 bytes／SHA、usage 與獨立模型 counter；不是只看模型自述。人工另讀原始來源確認上述語意裁決。離線 checker 的 `cloud_evidence_verified=false` 保留原義：它本身不是雲端執行者；不把這個欄位改成通過，也不把 `task_verified=true` 當內容品質通過。

## 失敗定位與證據界線

固定 A fixture 包含 `Agent A privately prefers the cobalt theme for Project Lyra.`。A 的原生 Memory update 回 `completed`，但其 `memory_operations` 只列出一筆：

> User is aware of internal preferences of Agent A related to Project Lyra, indicating familiarity with this agent's choices or the project details.

該筆內容沒有 `cobalt`；SDK 的實際 `read_context` 又回傳同一筆 Memory ID／內容。因此，**本次最早可觀察的缺口已出現在 Memory 擷取完成結果，不是只在最後回答才遺失**。模型之後把泛稱填成具體答案，亦違反任務的 `Do not guess missing facts.`。來源 ID 可解析，不代表它支持答案。

B 的原生擷取結果則明確保存 `saffron`。這是這兩次具體 update 的差異，不證明 A scope 故障、模型永久不支援私人記憶或所有後續擷取都會失敗。沒有上游模型完整 request／內部推理 trace，服務為何抽掉 A 的具體值仍未證實；本批沒有重種 fixture、改句子、補 oracle、改查詢或更換模型來湊 PASS。

後續修復需分開處理「必要事實是否被擷取／找回」與「缺事實時是否明確回報不足」，不能讓 `completed` 或存在的引用替代品質核對。這是本批留下的缺口，不回改已批准門檻，也不在已停止的 run 偷跑修復。

## 實測時間與 token

| `short-cold` 指標 | 原生值／來源 |
|---|---|
| 工具 context | 4930 UTF-8 bytes；native 下載與 SDK 工具結果一致，上限 32768 |
| 每次模型 input tokens | `630, 2338, 2622, 3038`；原生 SDK usage，全部在 8192 上限內 |
| Context 讀取耗時 | 1603.475337 ms；Hosted handler monotonic timer，不冒稱 SDK-native latency |
| 案例端到端時間 | 47541.442871 ms；從首個 Hosted budget 請求前到完成 evidence 回應，包含必要前置工作 |
| 本批保守 active 計時 | 182.311045 秒；包含私人控制、原生操作及停止確認，不是單一模型延遲 |

冷啟只指 Hosted session 的原生 idle 狀態，不代表整條 API／模型鏈路全部冷啟。只有一個失敗案例，不能計算冷暖改善、長歷史品質、p95、OpenClaw 比值或十倍成果。

## 部署、用量與費用

- API build `dss`：`sha256:5ff02897b6afe1d8105c167119e4f2f676ed60073052ae407c87ddd7335f6ad0`。
- 共用 Hosted build `dsr`：`sha256:f9d889815915048069822072ba71ee3b328a80400ba96b30bfb56535abdc2120`。
- A16／B10 使用同一 Hosted digest；API revision 為 `ca-facontext-poc--m4-1`。模型沿用 `gpt-4.1-mini`／`2025-04-14`，embedding 沿用 `text-embedding-3-small`。
- 未增加 RBAC、client、secret、consent、模型 deployment、網路或資源大小／scale；原 Store 與 M3 成果／SOP 未修改。

| 累計項目 | 實際／批准上限 |
|---|---|
| ACR builds | 2／2，沒有 rebuild |
| API revision／A 新版本／B 新版本 | 各 1／1 |
| 新 Hosted sessions | 2／2 |
| SDK starts／resumes | 1／5、0／1 |
| SDK Gateway 模型 attempts | 4／60，四筆 `upstream_response`／HTTP200；Memory 服務的擷取／embedding 用量另計，不包含在這四筆內 |
| Native invocations | 16／100；metadata inventory／show 另保留在 command journal，不冒算成模型呼叫 |
| Explicit stops | 3／5：B 控制後、A 冷啟重置、失敗 cleanup |
| Native file downloads | 2／12：真正 context 及 SDK trace |
| Memory updates | 3／4：B private、短 Team、A private；各一次 poll 就回 completed |
| Active 秒數 | 182.311045／1800 |

08:54:37 UTC 的 RG MonthToDate ActualCost 為 **US$19.07802700322689707**；仍保守採此前較高的 **US$27.079258133377022439**，M4 預留 US$10 包含在總 US$100 內。帳務延遲且兩個讀值口徑差異未解釋，不能把前後讀值相同當成本批零成本，也不能由差額推算精確 M4 費用。

資源仍保留，可能繼續計費；全 idle 不等於所有 Azure 資源已刪除或零費用。未刪任何資源或資料。雖然次數仍有餘額，**本批已觸發品質失敗停止條件，餘額不是續跑授權**；不可重設 ledger、換 run ID 或使用已消耗的 cleanup 預留重開案例。

## 版本固定與後續銜接

本節保留首批收尾時的歷史；最新r3程式基準與發布狀態見本文開頭，不再等待原先的提交授權。

這次先以 `m4-context-01-code-ready.json`、白名單 source manifests、兩個 image digest 及 A16／B10 原生 definition 固定真正執行內容。當時 Git HEAD `dcaa7192bd6715921870195fbf75914bbcb59ba5` 只包含 v1.2 文件，不包含未提交的 M4 接線；不能用該 SHA 冒充本次完整執行程式。

本批的執行／證據收尾已完成，品質保持 FAIL；M4 API／Hosted、fixture／checker、相關測試與本報告尚待另行批准 commit／push，才交付可由其他工作樹取用的程式基準。單次雲端 operator／collector 腳本仍在 ignored `.azure`，以 source freeze／原件 manifest 的 SHA 固定，不冒稱 Git 已包含它們。BR-01 可繼續隔離選型／offline_contract，正式接線仍等固定 code SHA；不必把這次失敗修成 PASS 或先完成十倍比較，才能保留基準並進行不依賴該缺口的後續工作。

完整原件位於 ignored 的 `evidence\m4-context-01-operator` 與 `.azure\m4-context-01-*`。原 ledger 保持 `aborted=true`、`closed=false`（後者只代表五案成功流程沒有完成），實際停止與證據收尾另記於 `m4-context-01-closeout.json`；不改寫失敗成成功。
