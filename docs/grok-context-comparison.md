# Grok Bot Internals 與 Foundry Agents：Context 管理完整盤點

研究日期：2026-09-23（台北）；repo 發布整理：2026-09-24。第 1–8 節的四章原碼比較基準為 **9/23 19:15 未提交 WIP／Hosted35**；9/24 CTX-03 的後續結果另列，不回填舊批次。**四項 baseline／target state 見第 8 節；完整 76 項目標見[獨立規格](grok-aligned-target-state.md)。**

**發布範圍：** 本文件保留完整比較、數值口徑及原公開來源，但不附原始 traces、SDK 私有快照、工具／模型日誌、資料庫或本機／app session 連結。`L`／`W`／`U` 引用改指向[研究證據索引](context-research-evidence.md)，明示哪些原件未隨 Git 提供；它們不是新增的公開原碼 permalink。這個文件 PR **不包含其他工作分支的產品 WIP，也不使本機結果成為雲端驗收**。

比較範圍：**系統提示、每回合 Prompt 組裝、逐字稿與壓縮、Skills 與 Context 預算**。本文把 Grok 作者的描述、FA 實際程式、平台提供的能力，以及已公開記載的驗收結果分開，不把它們混成同一種證據。

**2026-09-24 後續狀態：** 兩輪人工檢閱已接受各自批次；使用者於 08:49 批准 CTX-03 的本機保存／恢復第一切片，由原維護線實作，本研究核對凍結交付。當日進展依下方增補分開記錄，不倒改本文原基準；[切片驗收條件與結果](ctx03-local-acceptance.md)明列 raw／compressed 界線、同 SDK 身分的新 process 恢復、缺件／未知／舊格式相容與完成保護。其他候選、新雲端驗收不包含於此授權；本次文件發布另有明確批准，不發布產品 WIP。

**12:31 限定可行性增補：** 固定 SDK `1.0.13`／runtime `1.0.83`，不傳 `custom_instructions` 的手動 compaction，候選 B 已在隔離測試完成保存及兩個新程序的更正／回想；實際 SDK 模型輸入保有原 source、summary、後續更正與先前答案，不只比對預製輸出。已核對 29 份凍結檔案及新舊 manifest 的 126 個相同 module hash entries；沒有跟讀 live worktree 或重跑模型。**這是固定路徑的 test-only 證據，不是正式 CTX-03 驗收**：production 仍拒絕候選；原敏感 `customInstructions` 案例仍 blocked。本次 compact 未出現已知 canary，不能宣稱其敏感指令處理通過；automatic compaction、Hosted／Blob、其他版本與真模型品質仍未驗。[本批證據識別](context-research-evidence.md#ctx03-default-positive) · [本機裁決](ctx03-local-acceptance.md)

**09:58 本機差異增補：** 新 SessionFs／外部分片保存已使一般 filtered 長對話跨過舊 inline 容量並可恢復；API DTO／Hosted SDK 依賴也已分離。整體仍是 **PARTIAL**：manual compaction 的 raw 模型診斷副本會觸發私有材料拒絕，不能把其原生 RPC 成功當成已安全保存。初版 raw 保存的隱私契約遺漏已被修正，歷史實驗不倒改；詳見[本機驗收紀錄](ctx03-local-acceptance.md)。這不是新雲端部署，本文既有 Hosted35／API baseline 與 09:58 本機 WIP 分開。

**22:05 評測基準補充：** 維護方已交付修復後的乾淨合成對話 intake，API core 更新為 `ffc68461a0c3a3cfba4bf9cbfe15ee62de8635e64acf2131471d19aeb601ab2d`，Hosted35 core 不變。這次兩個 API 變更 raw 副本及三回合 JSONL 的 hash 已核；system prompt／Memory／compaction／skills／完成語義仍未變。測試污染的恢復是另開乾淨對話，不是更換 prompt。詳見[人工檢閱 intake][T02]；19:15 的四章原碼比較與更早結果保留，不回填成這批新基準。

**21:27 目標擴充：** 使用者要求以 Grok Bot 為目標，完整定義四個範圍。已另建立[四章完整 Target State][T01]：包含 system prompt 的 21 個骨架、可插拔互動／協作／安全行為，以及組裝、壓縮、Memory、Skills 的 **76 個條目與 12 個跨回合情境**。本報告第 8 節仍是 CTX-01～04 的工程候選，不再被當作完整四章目標；新規格也不代表已實作或已批准執行。

維護方在本次要求後確認四章產品 code 仍可沿用 19:15。另補充 21:19 的測試對話續問仍回代碼，已排除單純 UI 重用舊答案，但原對話的測試限制仍在；因此「前文回想 PASS」不外推成換題／指令作用域品質 PASS。新規格已納入本輪限制、跨輪偏好與測試隔離的判準，沒有為此另跑雲端或模型。[U06][T01]

## 最新增補：19:15 串流與保存接點，四項 context 差額仍在

維護 session 已提供新的凍結程式。本研究只讀該包，**16/16 raw source 副本的 SHA-256 與 snapshot 相符**；副本留在受限研究材料中、不隨本 PR 提供。未讀 live worktree、未更動產品、未重跑模型或雲端。[L01]

| 新基準 | 精確值／狀態 |
|---|---|
| 時間、HEAD | `2026-09-23T11:15:55.375264+00:00`；HEAD 仍 `257d8a117943b6a17669aed420dfd572d7933d85` |
| Git 狀態 | 原 branch `neallee2012-fix-pr2-p1-p2-reviews`，仍未 commit／push |
| API | core `f164083c09abcfeaea11406a5fbf4ba9a66b67fb0cc29400a1695195dd3f0225`；維護方回報 Healthy revision `ca-fa-behavior-poc--chat-quick-0923` |
| Hosted | **35**；core `fcaa439bf6b113726c12bbb2ea8b7da3b2cd775a588ca3d6a965925fd8e5f35b` |
| 核對範圍 | 16 個 raw 副本；snapshot 另列 122 個 module hashes，但不是這 122 份都交付本文或都由本研究重新計算 |

API 與 Hosted core **明確不同**：維護方說明 API 後加 acquire 純讀合併及 inventory page1000，不把兩者混稱同一映像。[L01][U05]

實際新增的是模型→API→Hosted→SDK 的分段串流，以及工作台從既有 GET 輪詢取得的**非持久公開答案 preview**。Preview 明記 `durable=false`／`complete=false`，不等於 saved、完成或可追問；只擷取公開 `delta.content`，不把 reasoning／tool arguments 放進這個 preview。[L04][L07][L08][L10][L11]

最終 `PublicProgress` 可隨 `SaveTextReply`／`TurnArchive.progress` 與答案、archive 在原交易一起保存；欄位省略 `None`，舊 bytes 語義保留。仍等進度寫入、要求正常 SDK stop；SDK 仍逐 turn 起停，**沒有常駐化**。這些是既有完成流程的調整，不是取消耐久保存或提前宣告成功。[L04][L05][L06]

**CTX-01–04 的核心 baseline／target 不變**：552-byte 短 prompt／replace、未加 prefix epoch／updates、未加自動 Memory、原兩檔 archive 的 128 KiB／16,000 bytes、未明設 `infinite_sessions`、runtime skills 關閉。新公開 preview 不會成為 CTX-02 的記憶事實，也不會被當成 CTX-03 的恢復點。[L03][L04][L05][L06][L07][U05]

## 17:16 凍結版本：保留已完成項目的歷史

17:16 版本採用 [/code-review pr 2][U02] 提供的 **HEAD `257d8a117943b6a17669aed420dfd572d7933d85`＋當時未提交修改**；不把 HEAD 單獨當最新程式，也不稱 WIP 已發布。凍結時間為 `2026-09-23T09:16:36.862731+00:00`，當時 core 識別為 `be9190d9f541d6233c5cd2343bc45277cc49572bcc10392f8f729026f4ce7232`。[W01]

本研究只使用凍結材料，從本地 Git object 加上其實際 diff 重建 **9 個 tracked 檔案，9/9 SHA-256 與 manifest 相符**；未讀取或複製變動中的 peer worktree。這驗證的是指定檔案，不是獨立重算整個 core，也不是重做 Hosted 驗收。詳見第 1.1 節。[W01][W03]

**新鮮度確認：** 17:49 使用者要求定義四項 baseline／target state 後，維護 session 再比對來源方工作樹，回覆 manifest 的 **10/10 檔案仍相符、HEAD 未變，四項沒有新的已整合實作**。這是來源方的 10/10 確認，與本研究的 9/9 重建核對分開記錄。[U03]

**17:51 當時的並行工作界線：** 使用者另批准接單、dispatch→SDK、模型回合、保存完成的效能調整；當時沒有新穩定快照，並未批准 CTX-01–04 新功能。本報告未跟讀該 WIP；19:15 的結果現已另列於上方及第 6.6 節，不能倒改當時狀態。[U04][U05]

| 項目 | 17:16／17:19 當時判定與證據 | 不再重做／仍保留的界線 |
|---|---|---|
| 自然追問、同 Hosted／SDK 對話 | **已有程式接線及真 Hosted 結果**：Hosted33 三輪同 Hosted SID、同 SDK SID，第三輪包含 `read_context`；模型次數 `[1,1,2]`。[W02][W05][W08][W09][W10][U02] | 撤回「自然追問根本未通／每輪必建新 Hosted session」；SDK process 仍逐輪啟停，不是永遠同 PID |
| Reset | **已完成指定案例**：停止並保留舊 SID／檔案，後續新對話取得新 SID。[W02][W08][W09] | 不重跑已完成案例；不把 reset 當成刪除原帳本、檔案或未知工作 |
| Progress／authority 競爭 | **已修正本批接點**：每 turn lock、合併 progress、最終 drain；最後才保存回答／archive。[W05][U02] | 不宣稱每一次歷史自然失敗都來自同一根因，也不把它說成完整 streaming 最佳化 |
| 第 7 輪無顯示／GET413 | **修正已上線並讀回**：原工作 `succeeded`，conversation／原答案 GET200；沒有新模型請求，原答案 hash 不變。[W02][U02] | 這次是集合 `consistent_read` 誤套 98-key 交易界線，不是 archive 超限、模型持續推論或已證明的 compaction 失效；七輪不是新上限 |
| 動態 unclaimed 歷史載入 | 維護 session 回報已修正重新載入，新 API revision Healthy／latest ready。[U02] | 取代 16:55「修正只 build、未上線」的暫時狀態；不等於較早所有未結工作已結清 |
| 財務阻擋 | **本批已啟用 accounting-only 行為**：`budget_enforced=false`；取消財務預算阻擋與 USD110 總上限，保留用量、高水位、pending、unknown 與原 policy／receipt。[W02][W09][W12][U02] | 取代「已批准但未實作／啟用」；不把舊金額常數重新當成缺陷，也不混淆 token／封包／archive 的技術容量 |
| 四筆較早的 `claimed capacity_uncertain` | **歷史清理仍 blocked**：provider stop 已確認，但欠缺合法 terminal adoption；原 session／files／ledger 保留。[U02] | 與已成功的 Hosted33 新對話分開，不轉成這份 context 研究的新清理任務 |

## 最新重點結論

**FA 並不是「沒有 context 管理」，而是已有 SDK 對話保存、精確來源、批准及記憶分層，尚未把這些能力整合成 Grok 文章所描述的完整每回合組裝流程。** 最大差異不在 system prompt 長短，而在：哪些內容常駐、何時更新、長歷史如何縮小而仍可恢復、skills 如何按需進入模型，以及這些路徑是否真正跑通。

| 範圍 | Grok 文章描述的重點 | 最新 WIP 的 FA 重點與剩餘差額 |
|---|---|---|
| 系統提示 | 模組化行為規則、條件段落、明確說話工具 | 19:15 仍是 552-byte 短 prompt／replace，區分 SDK history 與工作資料；串流不改成 Grok 式完整模組化提示。[G01][L03][L04] |
| 每回合組裝 | 凍結四段 prefix、以更新註記補變更、加入回覆／附件／routine 等脈絡 | 同 conversation 的 Hosted／SDK SID 與版本已固定，追問／reset 已走通；仍未建立 frozen-prefix／epoch／`instructions_update`，也未做每輪自動記憶沉澱。[G02][W08][W09][W10][U02] |
| 逐字稿與壓縮 | 模型／UI 雙視圖、壓縮封存、保留必要區塊、每回合沉澱記憶 | 19:15 增加非持久 preview 及最終 progress 同交易保存，但 portable archive 邊界與原生 compaction 差額未改變。Preview、durable answer、SDK state 是三種狀態。[G03][L05][L06][L07][W06] |
| Skills 與預算 | 目錄先占有限比例，本文按需讀取，長常駐指引移到 skill | SDK runtime skills 仍關閉，M3 `kind="skill"` 仍不等於 loader；自由指令沒有舊 32,768／1,024 人造 token cap。財務 gate 已取消，技術容量另計。[G04][W04][W09][F34][S06] |

**這次 context 管理的後續候選收斂為四類：穩定 prefix／增量更新策略、每輪自動記憶沉澱、portable archive／原生 compaction 的完整接續、按需 runtime skill loader。** 這不是全產品只剩四個問題，也不表示第 3.1 節中 Grok 的全部工具／行為能力都已完成。維護 session 明確表示沒有另開這四類實作；它們尚未因本報告而獲得實作批准。不得再把已完成的自然追問、reset、GET413 修復或財務阻擋移除重列成待辦。[U02]

## 1. 基準、方法與證據界線

### 1.1 最新 WIP 與歷史公開基準

#### 19:15 最新凍結包

來源是 `latency-0923\context-snapshot\snapshot.json` 與其 16 個 raw 副本，本研究保留在 [19:15 snapshot][L01]。它不是 diff 的猜測重建，16/16 檔案 bytes 都已按 manifest 核對；沒有因此宣稱兩個部署 core 都由這一小包完整重建。

| 材料 | SHA-256 |
|---|---|
| `snapshot.json` | `cd7f9ef2f632e9ea7c663ee026916316caf83817fc68527643f314e3252931fa` |
| `stage-comparison.json` | `dc5d87453f0639f4c7075a4236669f45e2f8c318fe765c9929e82242444afba4` |

另將六個既有本地檔案 bytes 對照 snapshot 的 `all_module_hashes`：`memory_governance.py`、`memory_projection.py`、`source_materials.py`、`m3_artifacts.py`、`m3_config.py`、`m4_fixtures.py` 全部相符。這支持本文對這些未變分層保留 `F` 引用；不是已取得 122 個 module 的新 raw 原件。`behavior_assistant_checkpoint.py` 也與 17:16 副本完全相同。[L01][W06]

#### 17:16 前一凍結包（歷史保留）

前一版採用維護 session 提供的凍結包：

| 欄位 | 值 |
|---|---|
| Snapshot 時間 | `2026-09-23T09:16:36.862731+00:00`（台北 17:16） |
| HEAD | `257d8a117943b6a17669aed420dfd572d7933d85` |
| Branch | `neallee2012-fix-pr2-p1-p2-reviews` |
| 發布狀態 | `published_commit=false`；HEAD 之上的本批修改仍未提交 |
| 對方提供的完整 core SHA-256 | `be9190d9f541d6233c5cd2343bc45277cc49572bcc10392f8f729026f4ce7232` |
| 本研究核對範圍 | 7 個 tracked 檔案的 diff＋2 個未改動的 tracked 檔案，共 9 個內容 hash 相符 |

三份原凍結材料保留為受限的唯讀研究參考副本，不隨本報告發布：

| 材料 | SHA-256 |
|---|---|
| [snapshot manifest][W01] | `8556e7fb9f91e7b57b478b46b17366abe58d89fb197d5a225296e70b3e869841` |
| [去敏 outcome evidence][W02] | `fec6b4f9eda4228e07c81be77a4c613db4104380a2422fd2ae8ae6499e0fc4a4` |
| [相對 HEAD 的實際 patch][W03] | `72850bd4cf0f0317f4ae21650a21848edefbbada07d0b0997e54dc328d439607` |

只從本 session 的 Git object 匯出指定 HEAD，於研究資料夾套用凍結 patch，沒有 checkout／合併至產品 worktree。9 份重建參考檔的 raw SHA-256 都與 manifest 相符。`m2_copilot.py`、`behavior_assistant_checkpoint.py` 也與原公開基準的檔案 bytes 相同，故這兩份舊 commit 引用仍可支持其未變契約。[W01][W03][W04][W06]

Manifest 第 10 個檔案 `behavior_work_budget.py` 是未追蹤的新檔，只收到 hash，未收到本文；本研究沒有重建或逐行審查它。提供的 patch 也不涵蓋全部 API／server／工作台支援修正；GET413 底層根因、完整部署歷史等依去敏結果與維護方 A/B/C 回報標示，不把九檔核對冒稱整個 core／所有映像的獨立驗證。[W01][W02][U02]

歷史公開基準仍保留如下，供追溯原比較：

初版研究完成時，本地 worktree 與當次 GitHub `main` 讀回都是：

```text
6f9a637fab51785c70d093fdca0b0d1ac416aa04
2026-09-22T13:24:55Z
Merge pull request #2: configurable multi-turn Hosted agent workbench
```

因此，本報告**包含 9/22 合併的自由指令、多輪對話、可選模型與新工作台**，不是只比較 9/15 的 M4 POC。#2 的 GitHub 狀態已是 merged；其描述仍保留早先 Draft 階段文字，不能把歷史描述當成當前 PR 狀態。[F00][F01]

下文 `L` 引用代表 19:15 最新凍結材料，`W` 代表保留的 17:16 基準，`F` 代表公開 commit 的原 M2／M3／M4 及相符的未變分層。`F` 不是最新 WIP 的 GitHub permalink；本研究不假定所有未提供檔案都和新工作樹相同。

固定的執行依賴是 Python `github-copilot-sdk==1.0.13`、Copilot runtime `1.0.83`、`azure-ai-projects==2.3.0`。本次另核對 SDK 的 `v1.0.13` tag 指向 `f13e4a2cc7e4e220974d2333142234e162a3252e`；下文 SDK 引用固定在該 commit，不以最新版 `main` 代替。[F02][F03][S01]

### 1.2 證據怎麼讀

| 標示 | 含義 | 不代表 |
|---|---|---|
| 最新 WIP 原碼 | 19:15 的 16 份 raw 副本與 manifest 相符；17:16 的 9 份 tracked 檔案另由固定 HEAD＋diff 重建核對 | 本批已 commit／push，或全部 API／Hosted core 已獨立重算 |
| 歷史 FA 原碼 | 固定公開 commit 中可讀到的程式路徑、型別與條件 | 所有後續 WIP 都未更動 |
| 測試契約 | repository 測試中的輸入、替身與斷言 | 本次重新執行過測試，或是真模型／Hosted 證據 |
| repo 記載的實測 | 已提交報告記錄的特定版本、案例與結果 | 本次取得所有 ignored 原始證據，或重驗了當下雲端狀態 |
| 去敏實測結果／完整協作回報 | 維護 session 提供的指定案例結果與 A/B/C，已保存來源及時間 | 本研究再次執行雲端／模型，或已讀取全部原始私人資料 |
| 平台能力 | 固定 SDK 原碼，或研究當日的官方文件 | FA 已接線、已配置、已驗收 |
| 設計建議 | 由差異推導的改善方向 | 使用者已授權實作、部署或增加支出 |

原研究階段沒有啟動 Hosted session、呼叫模型、修改 Azure、執行 repository 測試、commit 或 push；本次另獲批准的操作只發布整理後的研究文件。新結果採「維護方提供的去敏實測結果」，舊 POC 採「repo 已記載」，都不冒充本研究獨立重跑。參考檔重建及 hash 核對不執行產品程式。[F00][F04][W02]

研究使用本地原碼、Git 歷史、GitHub commit／PR／固定版本 SDK、WebIQ 與 Microsoft Learn。Azure DevOps 的 Bluebird 不適用於這個 GitHub repository，沒有假定它有本案索引。依 repository 指示，亦唯讀查閱 Microsoft 官方 `microsoft-foundry` skill；沒有安裝或更新本機 skill，也沒有執行部署流程。

### 1.3 Grok 來源到底能證明到哪裡

指定網站的四章是前端 hash 路由；本次研究讀取首頁內嵌的 `research-data` JSON，取得真正章節正文，不只採用搜尋摘要或導覽列。站方 metadata 是 **version `1.1`、研究日期 `2026-09-15`、snapshot `2026-09-14`**。因此比較對象是作者對該快照的描述，不是保證 9/23 線上 Grok Bot 仍使用相同實作。[G00]

| Grok 證據 | 可以支持 | 不能支持 |
|---|---|---|
| xAI 官方產品頁／公告 | Grok Bot 產品、自己的雲端電腦、記憶／協作等產品描述 | 四章中的函式、常數、完整模型 payload 或實際 cache 命中率。[A03][A04] |
| leescot 四章 | 作者對其本機 bundle／狀態快照的逆向分析與樣本觀察 | 官方 production source 的獨立驗證，或模型每次都遵守提示。[G01][G02][G03][G04] |
| 另一篇公開 gist 分析 | 部分描述相符，例如 frozen sections、`cursor.isSummary`、`assembleTurnAction` | 真正獨立來源保證；兩者可能使用同源材料，不能因此提升為官方證實。[C01] |

下文所有 Grok 內部細節均讀作「**作者描述／作者在其快照辨識到**」。作者列出的 `host-main.cjs` 行號只作定位線索，本次沒有取得該 bundle 逐行核對。另有自稱洩漏的 repository，但沒有讀其內容，也不拿它作本報告的實作證明。

`xai-org/grok-prompts` 是一般 Grok chat assistant／產品功能的公開 prompt 集；`xai-org/grok-build` 是另一個 CLI/TUI coding agent。兩者都沒有在本次證據中建立與這份 Grok Bot harness 的可核對版本對應。**搜尋識別符零命中本身不足以證明毫無關聯**；本報告只是不拿這兩個 repository 替四章背書。[A05][A06]

站方引用的 `prod.cursor.com/docs/grok-bot/settings` 本次未能成功讀取，故不引用其內容，也不從連線失敗推論該功能不存在。

### 1.4 四種「context」不能混用

1. **模型本輪輸入**：system、user、assistant、tool messages 與工具 schema。
2. **SDK 對話狀態**：跨 user turns 的訊息與工具歷史，以及 SDK 的保存／恢復資料。
3. **FA 權威工作資料**：工作版本、批准、來源、收件責任、已發生或未知的操作。
4. **長期記憶／投影**：可檢索的偏好、摘要或已審 claim；不是批准紀錄，也不是逐字稿的替代品。

FA 的領域文件已區分 Working memory、Shared work state 與 Agent context。實作上這些層仍走不同入口，不能只因為同名 `context` 就認定已全部裝入模型。[F05]

## 2. 最新 FA 呼叫路徑與保留的舊 POC 分層

### 2.1 新自由指令／工作台：SDK 是對話的主要管理者

19:15 路徑繼承 17:16 的 conversation 身分與恢復契約，新增串流／preview 與最終 progress 同交易保存：

```text
原文指令 + conversation_id + 選定模型
  → 驗證人類／Team／Agent 歸屬
  → Conversation 固定自己的 Hosted SID／version 與 SDK SID
  → 首輪建立 Hosted session；後續輪驗同一 session，不默默換機制或版本
  → 後續回合：還原前一回合的 SDK archive
  → 本輪啟動 SDK process，以同一 sdk_session_id create／resume
  → FA 短版 system message + 原文 instruction.content
      （明確區分 SDK 聊天前文與 read_context 工作來源）
  → Copilot SDK 處理模型／工具循環
      ├─ read_context：讀本工作允許的精確來源
      ├─ execute_operation：只執行另行獲准的固定操作
      └─ 公開回答串流可提供 process-local preview
          （GET 輪詢；durable=false／complete=false，不開放提前追問）
  → 本輪 SDK 程序停止；等待已排入的 progress 完成
  → 保存完整公開回答、SDK archive、工作結果及可選最終 progress
      （同原交易；未填 progress 時保留舊序列化行為）
  → 下一則追問仍使用相同 Hosted／SDK session，但不是相同 SDK PID

明確 reset：
  舊 conversation 封存 → 原 Hosted SID 停止、檔案保留
  → 後續另開的新 conversation 使用新 SID
```

`WorkPolicyControl.register` 保存 conversation 的 `hosted_session_id` 與 `hosted_agent_version`；`instruction_target` 使用它們，`FoundryExecutor.start` 在後續 turn 驗證既有 session，而不是每輪重新 create。舊的 per-work session 沒有這組欄位時，必須明確 reset／開新對話，不默默轉換。[W08][W09][W10]

`AssistantSession._run_turn` 直接把 `instruction.content` 傳給 `run_copilot`，使用選定模型及 `read_context`／`execute_operation`。最新 system message 與工具描述明定：**追問之前使用者說過什麼，使用本 SDK session 的 history；`read_context` 是另行審核的任務來源，不是聊天記憶查詢器**。固定計算工具 `save_result`／`finish_task` 仍不提供給自由回答分支，由應用程式保存最終文字（`behavior_assistant_hosted.py:388–482`）。[W05][W04]

`Conversation` 現在還保存 Hosted SID／version、`reset_request_id` 與 `archived_at`。前一筆仍 pending／unknown、未完成必要保存，或 archive 不可用時，仍不會偷偷另起空白對話接受追問；reset 亦不丟棄 running／unknown 的工作。不同 conversation 可以分開執行，同一 conversation 保留必要順序（`behavior_instruction.py:31–194`）。[W08]

**但這不等於每輪會自動注入完整長期記憶。** `read_context` 仍只回 `InstructionContext(source, content)`，是指定、已核對的人類審核來源；`MemoryGovernance` 的完整 manifest、所有 claims、私人／Team Memory，不是這個工具的回傳形狀，也不是每次模型呼叫前強制執行的注入 hook。[W05][W07][W11]

此處還要分清**使用者的一輪交辦**與**SDK 內的一次模型呼叫**：一輪可能有多次模型／工具往返。FA 把 user turn 交給 SDK，不是在每次模型呼叫前重建全部 Grok 註記。Hosted33 第三輪的兩次模型請求，中間有 `read_context`，就是這種區分。[W02][W05][U02]

17:16 已以 per-turn `_authority_lock` 處理必要 authority 呼叫、合併 progress 並在結果保存前 drain。19:15 保留這個方向，將「SDK turn ended」的最終 progress 交給最終保存交易，並加入真串流；仍不是常駐 SDK，也不宣稱所有歷史問題同根因（新副本 `behavior_assistant_hosted.py:377–551`、`behavior_instruction.py:196–274`）。[W05][L04][L06]

新 API 的 `acquire_current` 把前置純讀合成一次 `consistent_read`，原 guarded commit 及「已消費的 start 不可再啟 SDK」仍在（`behavior_assistant.py:372–423`）。這是已交付的效能修正，不另列為 context 新待辦。[L09]

### 2.2 原 M2／M3／M4：以工具拉取工作與記憶

原共享 Context 路徑則是：

```text
M2／M4 模型呼叫 read_context
  → Context API
  → 現行 Work／ETag／revision
  → 用 work.goal 查 Team Memory
  → 用 work.goal 查本 Agent 的 Private Memory
  → M3／M4 加入真正已批准的 SOP／skill 內容
  → JSON 作為 tool result 回到 SDK
```

`ContextService.assemble` 回傳 `actor`、`team`、`role_instruction`、`work`、`team_memory`、`private_memory` 與 `trust_boundary`。一般舊路徑的查詢取 `goal[:2048]`；M4 改成超過 2,048 字元明確拒絕，不暗中截短。Memory 搜尋各自設定 `max_memories=10`。[F11][F12][F13]

M4 特別要求**新 process／resume 後重讀 context**；舊 checkpoint 的 context 不算當前來源。其 prompt 和工具集合會依 `prepare_checkpoint`／`complete_after_checkpoint` 階段改變，避免 checkpoint 前就提供結案工具。這是已存在的階段化組裝，不應說成 FA 完全沒有動態提示。[F14]

### 2.3 v1.2 治理式 Context：存在，但不是完整模型 prompt

`BehaviorRuntime.context → MemoryGovernance.assemble` 會依 binding 與指定的 necessary claim IDs 取回已審 claims、精確原件，以及可選摘要。它有：

- `covered_sequence`、claim／source 版本、來源讀取 revision。
- `work_revision`、`release_version`、`catalog_version`、`memory_schema_version`。
- 各區的 bytes、tokens、總量與 omissions。
- 缺必要事實、來源失效、撤銷、版本變更與 token 計數未知的明確錯誤。
- 只有可選 projection 先被移除；必要 claims／原件仍放不下就拒絕，不裁掉它們。[F15][F16]

這些是實際原碼，不只是文件上的類別名稱。不過 `ContextSection.kind` 目前只有 `claim`、`original`、`projection`；**不是 system／工作狀態／近期對話／tool schema／skills 的完整模型封包清單**。因此不能把 `ContextManifest.total_tokens` 當成完整模型 request 的 token 數。[F16][F17][F33]

`NativeMemoryProjection` 已有原生 Memory update／search、來源 lineage 核對，以及同 projection／query 的結果重用。投影過時、搜尋結果未知或缺少有效 claim 時，能明示 degraded 並保留精確來源。這是**有來源的檢索投影及回退**，不是 SDK conversation 的逐字稿壓縮器；新自由指令的 `read_context` 也沒有直接改走這條 semantic projection 入口。[F18][F09]

## 3. Grok 四章逐項比較

### 3.1 系統提示：長篇行為模組，對照短 prompt 與程式控制

#### Grok 的完整提示骨架

作者描述 `buildSandSystemPromptSections(options)` 產生段落，再由 `renderSandPromptSections()` 攤平成文字；定位約為其 bundle 的 `763420`／`763548`。開頭是一句簡短身份描述，行為主要由後續模組承擔。[G01]

作者列出的 **21 個骨架段落**如下；`Voice calls` 與 `Canvases` 受旗標控制，不代表每次全部出現：

| 順序 | 段落 | 順序 | 段落 |
|---:|---|---:|---|
| 1 | SendToUser is your only voice | 12 | Debugging your computer |
| 2 | Tone and reply shape | 13 | The Grok Bot app UI |
| 3 | Showing your work | 14 | Writing on the user's behalf |
| 4 | Never fabricate data | 15 | Cursor Origin |
| 5 | Asking for decisions | 16 | Code changes |
| 6 | Threaded replies | 17 | Canvases（條件式） |
| 7 | Voice calls（條件式） | 18 | Autonomy |
| 8 | Where you work | 19 | Initiative |
| 9 | Long-running commands | 20 | When your own action needs approval |
| 10 | Managing plugins and connectors | 21 | Security |
| 11 | Reaching services that have no connector | — | — |

此外還有依模式／旗標加入的群聊、表單、MCP 帳號、不可信內容、委派與多工、購買、寫作風格、秘密處理等段落，以及 Memory／Routines／Teammates and groups／Connector instructions 四段動態內容。不能把 21 誤讀為「整份 prompt 永遠只有 21 段」。[G01][G02]

#### 和 FA 的逐項差異

| 主題 | Grok 作者描述 | FA 現行實作／差異 |
|---|---|---|
| 身份與人格 | 簡短身份句＋模組化行為段落 | AST 核對 19:15：legacy **264 ASCII bytes**、自由指令 **552 ASCII bytes**，與 17:16 相同（初版公開基準為 435）。不是完整 payload／tokens；19:15 literal 位於 `behavior_assistant_hosted.py:486–492`。[L03][L04][W05] |
| SDK 基礎提示 | 由 Grok 自己的段落組裝器建立 | FA 使用 `system_message={"mode":"replace",...}`，不是 append。官方 SDK 說明這會替換其基礎提示／文字護欄；但 FA 的 ToolSet、permission handler 與 server-side 檢查仍在，不能說所有程式保護也一起消失。[F06][F39][S09] |
| 對使用者說話 | 只有 `SendToUser` 會送達；普通 assistant text 是私有草稿。人類開啟的回合要求先回應，routine 可沉默 | 自由指令直接允許模型回答，應用程式保存 `final_message`，經既有結果路徑提供取用／交付。不是靠 `SendToUser`；保存、交付及收件確認仍是不同狀態。[F07][F22][F42] |
| 語氣與長文風格 | 明定短回覆、溫暖語氣及較長輸出的寫作指南 | 自由指令僅明說依使用者語言回覆，未載入同等完整風格模組。不能把開發本 repo 的 coding agent 個人指示當成 Hosted 應用的 runtime prompt。[F07][S06] |
| 顯示工作過程 | 提示規範如何對外展示工作，並限制內部細節 | FA 保留安全 stage／tool progress，另有僅公開答案文字的非持久 preview；preview 不是 reasoning／tool log，也不是完整已保存回答。最終 progress 可跟答案同交易保存。[L05][L06][L07][F40] |
| 不捏造資料／成功 | 以提示約束資料與完成宣稱 | FA 提示要求外部成功有 receipt，固定 profile 另驗結果。自由文字答案保存不代表所有事實已被外部查證；不能把 metrics verifier 的保證擴大到任意回答。[F07][F22] |
| 詢問決策／表單 | 提示包含決策邊界及可選的結構化表單 | FA 有人工批准與型別化 API，但自由指令工具集合沒有互動 `ask_user`／表單工具。人類批准工作流不等於模型能在途中自行開表單。[F07][F34] |
| 回覆串、聲音與畫布 | 有 message／reply 脈絡；voice／canvas 可依旗標出現 | FA 同 conversation 的 Hosted／SDK 身分與追問已走通，但仍沒有同款語音／畫布／thread-note 組裝。不是 Foundry 平台不支援這些功能的證明。[W02][W07][W08] |
| 工作電腦 | 文章描述多 bot 可共用 box 檔案系統／瀏覽器狀態 | FA 採每 session 隔離，同 conversation 維持 Hosted SID／version，不是每輪新 session。不能把 Grok 電腦共享語意搬成 FA 跨 Team filesystem。[A01][W08][W10] |
| 長工作與主動性 | 提示要求跟進長工作、主動行動；主動建議需有真實訊號 | FA 新自由指令移除固定整輪期限，但仍有傳輸等待與工作狀態控制；尚未把完整 initiative／routine 記憶回饋接進一般 user turn。[F07][F29][F42] |
| Plugins／MCP／無 connector 時的替代方案 | 有安裝、帳號、使用電腦存取服務的行為規則 | FA 關閉 config／plugin／MCP 自動探索，自由入口只有兩個具名工具，不能據自由文字輸入框就宣稱已有通用 browser／shell／connector 自主工作能力。[F06][F07] |
| Debugging／Code changes／App UI | 多個專門操作指引；部分可搬成 skills | FA 舊 M2 有受限私人檔案與桌面探針，但自由入口沒有一般程式編輯／OS 除錯工具。兩條能力路徑不能互相冒充。[F14][F07] |
| 代使用者操作／購買／批准 | 按動作類型界定詢問與安全規則 | FA 短提示禁止任意刪除、發布、聯絡外部；`execute_operation` 只對另行批准的固定操作。模型看見工具或已批准 skill 不代表取得新授權。[F07][F39][F09] |
| 不可信內容 | 提示要求辨識 `cursor_untrusted_data_1337` 包裝，另區分自身 Auto-review 通知 | FA 寫明 tool results 是資料而非指令，另用 typed DTO、來源／批准核對。沒有同名通用 XML 包裝；字串標記本身也不能替代程式權限檢查。[F07][F09][F10] |
| 秘密 | 文章描述環境變數注入與輸出去敏 | FA 將 API token／assignment ticket 留在受控 Python HTTP 路徑，不交給 SDK。有限 profile 的型別化邊界，不應被說成任意自由文字都能偵測所有秘密。[F43] |
| 委派／多工／群聊 | 描述 `Task` executor、待辦佇列與群聊規則 | FA 的獨立工作並行不等於 SDK 子代理協作；目前 `custom_agents=[]`、MCP／built-in tools 不開放。不能把工作並行的接線當成已有 Grok 式群聊與自動委派。[F06][F08] |

以上 Grok 欄均來自 [G01]，並非本次實際操作 Grok 的行為結果。

**判讀：** FA 的短提示是有意識的隔離與有限工具設計，不是單純少寫幾千字。若要增加人格、工作習慣或互動能力，應逐項接到可用工具／資料與交付路徑；單加一段要求 browser、skills 或 SendToUser 的文字，不會產生對應能力。

### 3.2 每回合 Prompt 組裝：穩定 prefix／差異更新，是最明顯的差距

#### 四段凍結與更新

| 項目 | Grok 作者描述 | FA 對照 |
|---|---|---|
| 凍結集合 | `memory`、`automations`、`agent_directory`、`mcp_instructions` | 沒有對應的四段 snapshot 管理；FA 的短 system text 本身穩定，但查詢結果主要作為 tool messages 進入。[F06][F11] |
| 保存與世代 | `promptSectionSnapshots` 存在 bot 的 `store.db`；`compactionEpoch=summaryArchives.length` | FA 有 release／catalog／memory／work 版本，但不是用 compaction 次數控制 system prefix。[F33] |
| 何時重算 | 同 epoch 保留 frozen render；壓縮後才換下一版 prefix | FA 讀取必要來源時重新核當前版本與權限，不等某次 SDK compaction。[F09][F16] |
| 期間如何看到變更 | `renderFrozenPromptSectionUpdates` 產生 `<instructions_update>`，放到當輪使用者訊息的註記中 | FA 沒有此增量註記協定；新自由 user message 仍是原文，後續靠 SDK history 及工具取得來源。[F07] |
| Diff 與全文 | 逐行差異太大時改送全文 | 沒有一般 prompt-section diff／full replacement 策略；治理式 context 是選取當前必要 claims／原件。[F16] |
| 差異集合不相同 | 四段都可凍結，但 turn-note 更新只涵蓋前三段，**不含 `mcp_instructions`**；agent profile 另有 snapshot 更新機制 | FA 沒有同等組合；不應把 Grok 這項例外漏掉或假定所有 frozen block 都有相同更新方式。[F06] |
| Cache 的意義 | 目標是盡量維持 provider prompt prefix | FA 原生 provider 可能仍有快取，Memory projection 也有結果重用；**沒有找到這種 FA 自管 prefix／epoch 機制，不等於所有快取都不存在**。[F18] |

Grok 來源：[G02]，作者定位包含 `SAND_FROZEN_PROMPT_SECTIONS`（約 `727493`）、`resolveFrozenPromptSection`／`renderFrozenPromptSectionUpdates`（約 `727626`）。

**重要細節：凍結的是 system 區塊，不是「更新之後完全看不到新記憶」。** 新內容可透過當輪 update note 出現；frozen render、即時 render、模型這次真正收到的完整 messages 是三種不同視圖。至於更新發生於回合中的哪個時間點、何時被採用，不能只看資料庫裡的 frozen 欄位判定。[G02]

#### 每輪訊息的組件

文章的 `assembleTurnAction()` 大致依以下結構組裝，實際組件受回合種類與旗標影響，不是所有回合都固定包含所有欄位：[G02]

```text
一般回合：
  message address／sender machine／reply context
  → 使用者原文
  → 附件註記
  → automation status／agent profile update／instructions_update／writing style
  → 適用的回覆、未完成工作、未答決策等提醒

允許沉默的喚醒：
  適用的 hidden／trusted automation 標記
  → 四塊動態註記可改成前插
  → 當輪內容
```

| 組件 | 對 FA 的精確差異 |
|---|---|
| message／machine／reply 註記 | FA 已固定 conversation／turn／Hosted SID／SDK SID，但未把文章同款註記組成每則 user message；不能將「缺這組註記」說成「追問缺前文」。[W05][W08][W09] |
| 使用者原文 | 原文傳入 SDK，對話 history 處理追問；現已有 Hosted33 三輪與 `read_context` 的結果，不再只有離線兩輪測試。[W02][W05][F44][U02] |
| 附件註記 | 自由入口 `InstructionSubmit` 沒有附件欄位；舊桌面 PNG 是另一條路徑，不能當成一般附件組裝已完成。[F10][F25] |
| routine／profile／差異註記 | FA 沒有同等自動注入；Role instruction 在舊共享 Context JSON 中是工具資料，並非每輪單獨的 privileged system block。[F11] |
| 回覆與未完成工作提醒 | Grok 文中有 `USER_MESSAGE_REPLY_REMINDER_BODY`、`UNFINISHED_TASKS_REMINDER_BODY`；FA 主要靠工作及 archive 狀態保護，而非同款 reminder 文本。[F08][F22] |
| 未答問題／錯過的批准卡片 | Grok 描述 `buildUnansweredQuestionsNote`；FA 現有批准／未知操作紀錄不等於已做成這種自動對話回注。[F07][F09] |
| 隱藏喚醒與合法沉默 | Grok 區分人類開啟與 routine；FA 最新自由回答路徑沒有這組 hidden-prompt／silence-mode 協定。[F07] |

#### 記憶的層次與取捨

| 主題 | Grok 作者描述 | FA 現行實作 |
|---|---|---|
| 層次 | User → Project → Own 的渲染順序；衝突時 Own > Project > User | Team／Private scope、指定 subject／必要 claims；沒有相同的三層優先序。[F11][F16] |
| 更正與寫入 | 各 shard 單一寫入者；跨 shard 以自己那一層的更正表達 | 已審 claim 的版本／supersedes／撤銷及來源核對；不是讓模型用更高層文字覆蓋批准。[F41][F16] |
| 記憶種類 | `profile`、`log`、`note` | 治理式 claim 有 `profile`、`episode`、`note`，但存在類型不代表每輪自動產生。[F41][F42] |
| 篩選／衰退 | 作者讀到半衰期 30 天、單條 500 字元、profile 100 條、recent 30 條／4,000 字元；episode／note 權重 1.5／0.5 | 現行組裝以必要 claim IDs、當前來源與有效時間為主；未見對應的 30 天 scoring／同款 tier budget。24-claim subject directory 是治理資料結構，不是可直接對比的 Grok profile 容量。[F41][F16] |
| 記憶進入模型的方式 | 作為常駐／更新的 Memory prompt 區塊 | M4 透過查詢工具讀 Team／Private Memory；新自由入口只回指定原件。兩者都不是 Grok 三層區塊的直接等價物。[F12][F09] |

Grok 來源：[G02]。上述常數是作者在其版本讀到的設定，不是官方承諾的所有版本限制。

**可取的設計意涵：** 穩定指令與變動資料分開，有利於維持 prefix；但 FA 的當前批准、撤銷及未知效果不能為了快取而凍結到下次 compaction。既有 server-side 即時核對應保留。[F09][F16]

### 3.3 逐字稿與壓縮：不只做摘要，還包含保存原文與回合後記憶

| 主題 | Grok 作者描述 | FA 現行對照 |
|---|---|---|
| 模型視圖與 UI 視圖 | 模型 JSONL 與 UI `transcript_entries` 分開；只壓縮前者 | FA 也把 SDK archive 與公開回答／進度分開保存，這部分在方向上相近；但不是相同儲存格式或相同壓縮語義。[F21][F22][F40] |
| 舊內容去哪裡 | 被壓縮內容進 `summaryArchives`／`conversation-blobs.db` | FA 有精確來源與 SDK 封存，但沒有自建同款 summary archive store。SDK 本身的 compaction／workspace 保存需另按固定版契約核對。[F21][S03] |
| 誰決定觸發門檻 | server 回傳 `earlyCompactionContextTokenThreshold` 與 remaining-window 狀態，box 記錄 | FA 沒有這個 Grok 回傳協定；SDK 宣告的 0.80／0.95 預設是另一套契約，不能當作 Grok 的固定百分比。[S03][F06] |
| 接近 token 上限 | `approaching_token_limit`，可背景進行 | FA 沒有明確配置／驗收自己的背景壓縮政策；不因此斷言原生 SDK compaction 被關閉。[F06][S03] |
| 明顯超限／input error | `significantly_over_token_limit`、`input_token_limit_error`，阻塞完成壓縮 | FA provider context error 明確失敗、不截斷或重送；尚無這條新入口「收到容量錯誤後成功恢復」的證據。[F19] |
| 圖片壓力 | 另有 `approaching_image_limit` | FA 舊桌面在送出時只保留最新 PNG；這是有選擇的圖片裁切，不是 Grok 的 image-limit compaction。[F25][F26] |
| 摘要前保留什麼 | `prepareMessagesForCompaction` 保留 system、開頭的 user-info、手動附加 skills 區塊 | FA 必要 claims／原件不因 projection 超額而裁切，但那是治理式 context；並非 SDK chat compaction 保留同等區塊的證據。[F16] |
| 最近使用者原話 | OpenAI 分支有最多 **10,000 tokens** 的原話保留額度 | FA 沒有自訂對應的 recent-user-verbatim budget；SDK history／compaction 的實際保留效果待精確驗收。[F06][S03] |
| Provider 分支 | bundle 中有 OpenAI、Anthropic、xAI 不同摘要提示；Anthropic 分支另取 summary 區塊 | FA 依 SDK 及其模型傳輸擴充，不維護這三條自製 summarizer。bundle 有分支也不證明那個 Grok Bot 實際跑過全部分支。[F06] |
| 摘要插回方式 | 以 `user` role 插回，標記 `providerOptions.cursor.isSummary` | FA 沒有自行合成這種訊息；把來源摘要傳作工具資料與改寫 SDK conversation 是兩件事。[F16][F18] |
| 與 prefix 的連動 | 壓縮完成讓 epoch 增加，下一輪重建 frozen sections | FA 沒有這個雙向連動；目前 context versions 不以 SDK 壓縮事件遞增。[F33][F06] |
| 每輪記憶沉澱 | `runTurnMemory()` 在回合後運作，不必等壓縮 | 19:15 在 progress drain 後保存回答／archive／可選 progress，仍未自動抽取 Memory（`behavior_assistant_hosted.py:532–543`）。串流 preview 不是有效記憶來源。[L04][L06][F18] |
| 每輪結算 | 文章有 `lastTurnSettlement` 的 nonce／outcome／時間收據 | FA 有自己的工作、模型 request、保存及交付紀錄；不能從任一收據結構推論跨服務 exactly-once。[F22][F23] |

Grok 來源：[G03]。作者定位包含 compaction response 欄位約 `706482`、分割程序約 `531166`、OpenAI 原話預算約 `559821`。

**數字特別校準：**

| 作者記載 | 正確口徑 | 不應推論 |
|---|---|---|
| 模型逐字稿 327 行：assistant 171、tool 142、user 14；UI 51 筆 | 9/14 單一 bot 的樣本筆數 | 通用訊息上限、壓縮比例，或所有 bot 的使用者 token 占比 |
| OpenAI 原話保留 10,000 tokens | 作者辨識到的該 provider 分支 | 全 context window 大小；文中另提的 Codex CLI 20,000 不是 Grok Bot 值 |
| 預設每 6 次有意義交流產生 episode | 回合後記憶沉澱的節奏 | 每 6 次一定進行模型 context compaction |
| Episode turn text cap 2,000 字元、pending 64、時間跨度 21,600,000 ms＝6 小時 | 記憶摘要的輸入／待處理／時間邊界 | 原始 SDK 對話可保留的總量或整個工作期限 |
| 短寒暄可由長度、問號及 trivial-exchange 清單篩除 | `isMemorableExchange()` 的啟發式 | 所有短訊息都沒有記憶價值 |

新版「先保留證據、稍後合成」與舊版逐回合抽取也需要分開看；不是每次 user turn 都立即重新摘要整段歷史。[G03]

**尚待補足的不是「自然追問能不能用」，而是「觸發原生 compaction 後的長對話保存與恢復」。** 最新材料沒有本批這個情境的真 Hosted 驗證；三輪／七輪成功及 M4 的有限 recall／checkpoint 都不能代替它。七輪 GET413 是已修正的讀取集合問題，不能拿來證明 archive 已超限或 SDK compaction 壞掉。[W02][W06][U02] Grok 的靜態 bundle 分析同樣不能代替真實行為驗證。

### 3.4 Skills 與 Context 預算：FA 的 skill 成果，不等於 runtime skill

| 主題 | Grok 作者描述 | FA 現行對照 |
|---|---|---|
| 初始目錄預算 | `floor(effectiveTokenLimit * 0.02)`，`INITIAL_SKILL_CATALOG_CONTEXT_FRACTION=0.02` | FA 沒有一般 skill catalog 或其 token 配額；這不是把配額設成 0，而是技能載入功能未啟用。[F06][S06] |
| 2% 管的是什麼 | **初始目錄**，不是所有 skill 本文、工具結果及整輪總成本 | 不能直接把 2% 套在 FA M3 成果大小或完整模型輸入上 |
| 超額降級 | 先縮短描述，再只列路徑，最後省略條目並告知省略數及目錄 | FA 必要 claims 超額時先丟可選 projection，再拒絕必要資料溢出；兩者針對不同資料類別，不是相同 budget 算法。[F16] |
| 使用方式 | 列出 skill 名稱／描述／完整路徑，使用前以 Read 載入；點名與未點名有不同提示規則 | FA 沒有對應 skill／Read 工具組合；自由工具只有 `read_context`、`execute_operation`。[F07] |
| 長指引移出常駐 prompt | `skillifyEnabled` 讓 code-changes、routines 等只留精簡指引，本文搬到 skill | FA 尚無同等 skillify／catalog builder；不能把程式已支援 Markdown artifact 說成按需 skill 載入。[F34][F06] |
| 本文來源／批准 | 作者描述 managed-skills 快取與 skill-authoring 等能力 | FA M3 對 `sop`／`skill` 有內容格式、immutable version、hash 與人工批准；但目前只是 context 可讀的政策文字，不是工具安裝／runtime skill activation。[F34][F38] |
| 內容大小 | 2% 是 catalog tokens，不是單一 skill 檔案通用 bytes 上限 | M3 的 SOP／skill 每份最多 **8 KiB**，設定清單最多 **4 個 artifact IDs**；它們只是該 POC 路徑的契約。[F38][F35] |
| 載入是否擴權 | 文章是提示層規則，實際執行權仍需另外核對 | FA 以工具 allowlist／callback／固定操作批准控制權限。即使將來啟用 skill，也不能順便放開 ambient host 能力。[F39][S08] |

Grok 來源：[G04]；作者定位包含 `applySkillCatalogBudget`（約 `601070`）、`renderAgentSkillItems`（約 `601604`）。

作者的單一樣本列了 **20 個 managed skills**，包含任務流程、行為手冊及 meta 類型；20 不是上限。其 `automations` 區塊 frozen render 為 **19,614 字元**、即時 render 為 **3,057 字元**，作者將這個差異解釋為 routines 的 skillify。這是同一樣本的兩種 render 視圖，不是本次做的前後 A/B 效能測試；不能推算整體成本或延遲改善比例。[G02][G04]

FA 的關閉是明確可追查的：`mode="empty"` 的 SDK 預設關閉 skills、embedding retrieval、原生 memory、自動 custom instructions 等 ambient 功能；FA 同時給 `skill_directories=[]`、`plugin_directories=[]`、`instruction_directories=[]`、`custom_agents=[]`、`mcp_servers={}`，並限制工具集合。**原生 SDK memory 關閉不代表 FA 的 Foundry Memory／治理式記憶關閉**；它們是另外接上的服務。[F06][S06][F13]

官方 SDK 本來就有受控 skills、tool search 和 session storage 擴充。比較結果不是「必須像 Grok 自建一整套」，而是「先將已批准技能與固定 SDK 的正式擴充接起來，保持隔離，再補缺少的應用程式契約」。[S03][S07][S08]

## 4. FA 的數值限制：分層看，不能混成一個 context window

| 所在層／路徑 | 原碼中的限制或行為 | 正確解讀 |
|---|---|---|
| 舊 M2／固定 profile 的模型設定 | `MAX_PROMPT_TOKENS=32768`、`MAX_OUTPUT_TOKENS=1024` | 舊路徑的 FA 設定，不是所有模型的實際容量 |
| 9/22 自由指令模型設定 | 移除上述 provider token overrides 與 `model_capabilities` override | 不再強制舊 32,768／1,024；不代表模型、傳輸或儲存無限 |
| 自由指令 ingress | 指令經 `json.dumps(..., ensure_ascii=False)` 後最多 8,192 UTF-8 bytes | 傳輸上限，不是 token 數；不可只用使用者可見字數估算 |
| 自由指令 `read_context` | `InstructionContext.content` 最多 16,384 字元 | 單一來源工具的型別上限，不是可用長期記憶總量 |
| 自由指令 provider request | JSON 最多 `128*1024` bytes | 完整請求的技術封包界線；比外層 gateway 的 4 MiB 更緊 |
| M4 Context JSON | 最多 32,768 UTF-8 bytes | 固定驗收案例的大小契約 |
| M4 每次模型 input | 驗收上限 8,192 tokens | 固定測項門檻，不是新自由指令的通用設定 |
| 治理式 context | server 設定預設／允許上限 8,192 text-section tokens、1,048,576 bytes | 計 claims／原件／projection，不包含全部聊天封包 |
| SDK portable archive | 原始 JSON 最多 128 KiB，`SdkArchive.data` 最多 16,000 bytes | zlib 後的輸送／保存容量另有限制；不是語意壓縮 |
| 公開回答保存 | 原始序列化內容最多 4 MiB、壓縮後最多 32,768 bytes | 回答保存和 SDK 對話 archive 是不同容量，可能只剩回答可讀 |
| 19:15 非持久 preview | 每筆最多 32,768 **字元**、process-local 最多 64 jobs、最近更新後 600 秒未更新可失效 | 只限制可選 preview，不裁切原始完整回答，也不是工作 deadline／模型 token cap |

來源：[F06][F07][F10][F12][F19][F20][F21][F22][F30][F31][F32][L05][L06][L07]。

`ServerTokenizer` 固定 `tiktoken==0.11.0`、`o200k_base`，檢查本機 encoding ranks 的 hash；其註解明確指出只算 text sections，**不是 provider usage 或完整 chat envelope tokens**。[F17]

自由指令仍保留每次模型 request 的費用預估、pending 與實際 SSE usage 回填；**最新已取消的是財務預算阻擋，不是計量資料**。`reserve_writes`／dispatch／decision 的金額上限判斷已受 `budget_enforced` 控制；停用時 decision 的 remaining／ceiling 等不再顯示為有效預算值，而舊 policy、receipt、高水位及 unknown 保留。[W09][W12][W02]

因此，程式還存在舊 `110_000_000` 金額或 legacy `MAX_PROMPT_TOKENS` 常數，**不能單憑常數就重新提「移除上限」的工作**。要追實際分支：財務 gate 為 false；自由指令未傳舊 32,768／1,024 overrides；archive／傳輸等技術容量仍然存在，且不是財務預算。[W04][W09][W12]

供應商回 `context_length_exceeded`／`context_window_exceeded` 時，新 gateway 轉成 `ASSISTANT_CONTEXT_LIMIT`，不以靜默截斷或自動重送換取成功。這能防止假成功，但還不等於已有「超限後可安全接續」的完整使用體驗。[F19]

## 5. 逐字稿、語意壓縮與恢復：FA 已有什麼、仍缺什麼

### 5.1 已有的對話保存

新自由指令目前保存 `events.jsonl` 和 `workspace.yaml` 兩個 SDK 檔案。`export(public_only=True)` 移除列舉的 reasoning／system-message 欄位或正文，再將內容包成有 hash 的 zlib archive；還原時核對 archive、session identity 並用 create-only 寫入。[F21]

因此正確名稱是**經明確欄位處理的 SDK 對話封存**，不是「所有原始內部訊息逐位元永久保存」，也不是一般秘密偵測保證。它只打包兩個檔案；不能據此宣稱 SDK workspace 中任意附件、checkpoint sidecar、planning files 或工具產物都已可攜。[F21][S02]

當 archive 超界／不可用，回答仍可保存，`can_follow_up` 會變成 false；不允許在失去 context 後假裝原對話仍能繼續。`test_instruction_flow.py` 的 archive-limit 案例明確測這個分支，但其 SDK 是 I/O 替身，不能當成真正 runtime 長對話驗收。[F22][F24]

### 5.2 已有的非語意裁切

舊桌面路徑的 `_retain_latest_inline_png` 只讓**最新一張 inline PNG** 進入模型請求；較早圖片改為明確文字標記，歷史文字與 tool-call／tool-result 關係保留。原圖仍在 SDK history；這不是刪除歷史，也不是把圖片摘要成語意記憶。[F25]

這項契約有真 SDK 搭配離線 HTTP 的測試程式，檢查送出 request 只剩最新圖片、所有文字及 receipt 仍在、history 保留原圖。本次僅閱讀該測試，未重新執行。[F26]

### 5.3 不能把「沒寫自訂 compactor」說成「SDK 沒有壓縮」

固定 Python SDK 的 `InfiniteSessionConfig` 已宣告：

- `enabled` 的文件預設值為 `True`。
- 背景 compaction threshold 預設 `0.80`。
- buffer exhaustion threshold 預設 `0.95`。

SDK `create_session`／`resume_session` 可以把這些設定送到 runtime。FA 的 `run_copilot` **沒有明確傳 `infinite_sessions`**，所以只能說 FA 未自訂策略、未提供本案實際觸發的證據，不能從欄位省略推論成「已關閉」，也不能把 SDK 文件預設當成這個部署已成功完成 compaction。[S03][S04][F06]

同一 SDK 另有 `LargeToolOutputConfig`、`ToolSearchConfig`，以及有使用條件的 `history.clearContext`。這些原生能力可優先評估；但 FA 沒有因此自動得到完整大輸出回讀工具、skills catalog 或受治理的 handoff flow。`clearContext` 需要工具呼叫進行中、非空 seed prompt，且不適用 remote sessions；不能當成任意時機可呼叫的清空按鈕。[S03][S05]

### 5.4 「摘要沒有丟必要事實」與「長對話能接續」是兩個不同測試

`MemoryGovernance` 的離線案例會刻意讓摘要丟失 `cobalt`，確認原 claim 與原件仍可讀；也驗必要事實放不下時拒絕而非截短。這支持來源回退設計，**不是**大量真 user／assistant turns 經 runtime compaction 後，再跨 Hosted process 成功接續的證明。[F27][F45]

## 6. 最新實測結果、部署狀態與保留的歷史

### 6.1 M4 的 checkpoint 缺口有後續補驗

repo 根 README 還保留「r3 四案通過、第五案 incomplete」的摘要，但 `docs\M4-CONTEXT-POC.md` 置頂新增了 9/16 A18 `long-resumed` 的實測記載：

- 保存未完成 checkpoint → 真正 stop／idle → 同一 SDK session 換 process。
- 重新讀 context，完成答案，原 draft 不重送。
- 該次 8 次 input-token 記錄均不超過 8,192。
- context 讀取約 1,310 ms，案例約 62,312 ms。

應說：**r3 歷史仍是 PARTIAL；其 checkpoint 缺口後來在獨立 r4 單案補驗通過**。不能繼續說「完全沒有 Hosted checkpoint 證據」，也不能把跨批補驗改寫成原五案同批全部 PASS，或拿單次時間當 p95／10 倍效能。[F28]

### 6.2 Hosted33 的多輪／reset 已有真實結果

凍結 evidence 記錄三輪都是 Hosted33、同 Hosted SID、同 SDK SID；維護方 A/B/C 補充第三輪確實走了 `read_context → model`。以下是提供的既有樣本，不是本研究新發起的測試或財務 gate 取消後的新 benchmark：[W02][U02]

| 回合 | Hosted 版本 | 模型請求數 | 案例耗時 |
|---|---|---:|---:|
| 1 | 33 | 1 | 32.345 秒 |
| 2 | 33 | 1 | 24.350 秒 |
| 3（含 `read_context`） | 33 | 2 | 32.758 秒 |

Reset 的去敏結果為 `old_session_retained=true`、`stop_observed=true`、`new_hosted_sid=true`、`old_files_deleted=false`。程式也核對原工作終局與保存條件，沒有將 reset 寫成任意刪除／拋棄 unknown 的入口。[W02][W08][W09]

因此應撤回「自然追問根本未通／每輪新 Hosted session」。同時仍不能外推成永遠同 VM、永遠同 SDK PID、所有跨版本恢復、任意長度 history 或已觸發 compaction 的保存案例都通過。現行 SDK 程序仍逐輪啟動／停止，原生壓縮的完整長對話驗收仍是另一個缺口。[W04][W08][U02]

### 6.3 第 7 輪 GET413：讀取修正，不是重新推論

原第 7 輪已 `succeeded`、模型請求數為 1。維護方定位：conversation 的集合 `consistent_read` 誤套 **98-key 交易界線**，造成 GET413，並非模型一直算五分鐘，也不是此案已撞到 SDK archive／compaction 上限。[W02][U02]

最新回報已是原七輪 conversation GET200、原答案 GET200，原答案 hash 不變；去敏 evidence 記錄 `new_model_requests=0`、`fresh_full_history_validation=true`。修復的是取得已完成結果的路徑，沒有重送原訊息／答案生成。[W02][U02]

16:55 暫報的「只完成 build、新 revision 被 unclaimed fence 擋住」是中途狀態；17:19 完整回報已確認動態歷史重新載入修正及新 API 上線，舊暫報不再代表現在。[U01][U02]

這個 98-key 根因與完整底層修正來自維護方回報，不在本研究重建的七檔 diff 範圍內，故不宣稱此處已獨立逐行審核全部修復。七輪成功也不是任意長度對話的無上限證明。

### 6.4 17:16 部署與財務紀錄（歷史）

| 部署面 | 17:19 回報的當時識別 |
|---|---|
| API revision | `ca-fa-behavior-poc--budget-accounting-0923`；Healthy／latest ready |
| API image | `sha256:c20105b15de434bce2bd45c85c6bfbd970d893d83dfdaf77d5e4c438d179bb5e`，build `ds3h` |
| Hosted | 仍為 **33**，不是 Hosted34 |
| Hosted image | `sha256:1ec80f0ca2bc1d4824bd588b462c705d999ce82ba0cad5085c7401af38e0b29c` |
| 財務阻擋 | `budget_enforced=false`，保留 accounting；舊 policy／receipt 不改寫 |
| Git | 本批修正仍為未提交 WIP，沒有本次 commit／push |

來源：[W01][W02][U02]。這是 17:16／17:19 當時狀態，保留原 API 與 Hosted33 識別；19:15 的 API／Hosted35 另記，不將後來部署回填成當時已發生。

財務取消與正確 accounting 是此批已完成的使用者需求，不是 context 功能尚缺的一項，也不是本研究可以再次啟用財務 gate 的理由。模型容量、HTTP 封包與 archive 保存限制仍須獨立描述。

### 6.5 仍保留的歷史與效能界線

9/22 Hosted31 的兩筆 `WORKER_START_UNKNOWN` 不倒改成成功；M4 A18 的有限 checkpoint 補驗也不改寫成新一般入口的驗收。另有四筆較早 `claimed capacity_uncertain` 的清理仍 blocked，缺合法 terminal adoption；它們不是 Hosted33 新對話目前全部失敗的證據。[F04][F28][U02]

17:19 回報明確說明 **28.186 秒**樣本是取消預算以前的資料；當時只有純讀回驗證，沒有新聊天速度 benchmark。不能宣稱「取消預算後已降到幾秒」，也不能把五分鐘 UI 無顯示當成模型推論時間。[U02]

**17:51 當時的範圍更新：** 接單、dispatch→SDK、模型回合及保存完成的效能調整獲另行批准；當時尚無穩定程式或前後數值，不得先宣稱能力完成。19:15 的新結果見下一節；這批授權始終不代表 CTX-01–04 已批准。[U04][U05]

### 6.6 19:15：Hosted35 串流與前後樣本

`stage-comparison.json` 記錄新的合成短訊息兩輪前後樣本；各組使用不同 Hosted conversation。其量測是 **console API 的觀察，不是瀏覽器 paint，也不是 provider TTFT**；沒有沿用歷史 28.186 秒樣本。[L02]

| 指標（秒） | 首輪 before | 首輪 after3 | 追問 before | 追問 after3 |
|---|---:|---:|---:|---:|
| 送出 → 完整 console 回覆 | 33.530 | 27.910 | 28.988 | 22.697 |
| 點擊 → 完整 console 回覆（含 draft preparation） | 33.530 | 27.910 | 31.215 | 22.792 |
| 首段 preview | 無這項 preview 觀察 | 19.517 | 無這項 preview 觀察 | 13.705 |
| SDK turn | 5.952 | 6.223 | 5.084 | 4.662 |
| SDK end → native complete | 6.119 | 6.324 | 5.944 | 6.437 |
| 模型請求數 | 1 | 1 | 1 | 1 |

這組樣本的送出至完整回覆分別減少 **5.620／6.291 秒**；但首輪 SDK turn、兩輪 SDK end→native complete 都沒有改善。尤其追問保存／完成段從 **5.944 增為 6.437 秒**，不能寫成「每一段都更快」。樣本數、輸入及採樣方式也不足以宣稱 p95、普遍提升比例或因果上全由某一修正造成。[L02]

維護方另回報：最新兩輪同 Hosted35／SDK SID，追問記得前文；SDK start 為 6–47 ms、stop 為 2.27–2.32 秒，所以沒有冒險改成常駐。這些細項來源是維護方回報，聚合 JSON 並未逐項包含 start／stop 原始 spans，本研究不冒稱讀過那些原始日誌。[U05]

一次中途串流案例曾因 `finally` 提前關閉 stream 失敗，pending 保留；後續修正串流生命週期後，新 Hosted35 案例成功，不是重送失敗原 ID。`stage-comparison.json` 亦明列 `failed_intermediate_turns=1`、`additional_successful_intermediate_turns=2`，不把只列出的 before／after3 四筆冒稱整批從無失敗。[L02][U05]

原第七輪 reply hash 未變、原 conversation 現為十輪 GET200、財務 gate 仍 false、帳本未清零，四筆較早 claimed 清理仍 blocked。這些維護方狀態不拿來推論無限對話、原生 compaction 完整通過或較早 unknown 已消失。[U05]

## 7. 9/23 差額清單：四項候選的歷史基準

以下是 9/23 當時扣除已完成項目後的分類；9/24 CTX-03 的進展見[本機驗收結論](ctx03-local-acceptance.md)，不再把它說成尚未開工。當時已完成多輪生命週期、reset、progress 競爭、聊天 UI、GET413、動態歷史載入與財務阻擋移除；本研究沒有為該次比較另開實作、部署或模型驗收。[U02]

| 候選 | 最新仍缺的內容 | 優先重用／最小切入點 | 不要重做 |
|---|---|---|---|
| **CTX-01 穩定 prefix／增量更新** | 沒有 FA frozen-prefix、epoch 或 instructions-update 協定 | 先判斷既有 SDK／provider 已能滿足多少穩定指令與更新一致性需求；必要差額才接到 `run_copilot`／既有 context 入口 | 不重建 agent loop；不因 Grok 有同名函式就要求照抄 KV／epoch |
| **CTX-02 每輪自動記憶沉澱** | 回合結束仍只保存回答與 SDK archive，未自動將適合長期保存的事實送進治理／Memory 流程 | 沿現有 SourceMaterials／MemoryGovernance／projection，明定觸發、必要性、來源及 scope；不是保存所有聊天 | 不另建競爭中的聊天歷史；不重做已完成的對話續接 |
| **CTX-03 長對話 archive／原生 compaction 接續** | 128 KiB raw／16,000 bytes archive 邊界未擴充；沒有本批真 compaction 長對話結果 | 優先評估固定 SDK 的 `session_fs`／`create_session_fs_handler` 與必要原件保存，再驗被明確批准的缺口案例 | 不把 GET413 當成未修的 archive 問題；不重跑那三輪／reset，也不自建第二套 transcript engine |
| **CTX-04 按需 runtime skill loader** | SDK skills 仍關閉；M3 `kind="skill"` 不是執行時 loader | 用已批准版本形成精簡 catalog，接 SDK 正式 skills／tool-discovery 能力，維持 `mode="empty"` 的隔離原則 | 不重做 M3 成果批准；不為了 skills 放開全域設定、MCP 或任意 shell |

差額依據：[W04][W05][W06][W07][W08][U02]；可重用的官方能力：[S03][S07][S08]。這是避免重工的分類，不是開發排序、時程、預算或新雲端操作批准。

### 7.1 保留的設計原則

穩定指令與變動資料可以分開，但當前批准／撤銷／未知操作不能等到 compaction 才更新。既有 `ContextManifest` 可追溯必要來源，卻不能把 text-section token count 改名成完整模型封包用量。[F16][F17][F33]

長對話改善必須同時處理模型 context 與持久化完整性：只降低模型 tokens，卻仍因 archive 超界而失去追問，不算完成。反過來，三輪／七輪成功也不需要被推倒重做才能規劃這項後續能力。[W02][W06][W07][S03]

Skills 載入不授予 shell、browser、網路、跨 Team 資料或外部副作用權限。可借鑑 Grok「目錄先行、本文按需」，但 2%／10,000 tokens／30 天／6 次交流各屬不同層，不能直接當成 FA 的新限制，尤其不能以 context budget 名義重新引入已取消的財務阻擋。[G02][G03][G04][W09][W12]

### 7.2 不合併成同一份待辦的項目

- 四筆較早未結工作：屬原維護線的 terminal adoption／清理問題，保留 blocked 與原資料；不和 CTX-01–04 混為一談。
- Latency／streaming／常駐 SDK：19:15 已交付串流及指定前後樣本，常駐 SDK 未實作；保存／完成段仍未改善。本研究不重複承辦，也不把效能變更寫成 CTX-01–04 完成。
- Git 發布：產品 WIP 與部署識別分開；本次只經明確批准發布研究文件，不為了補 permalink 而提交其他維護分支的產品變更。

### 7.3 平台能力仍須分層

平台面也不能誤判：Foundry Hosted 的 **Invocations** 不替應用程式管理 conversation history；**Responses** 的平台托管 history 是另一個協定能力。FA 使用 Copilot SDK 管理其對話，不是因為 Foundry「完全不能保存歷史」。同樣，session `$HOME` 的持久化只保證該 session 的狀態語意，不是多 Agent 的共享 filesystem。[A01]

Foundry Memory 的低階 API 要求呼叫者明確提供 `scope`；官方文件中 prompt agent 的 memory tool 自動辨識 user identity，不能直接套用到 FA 的低階 `search_memories` 呼叫。FA 自行檢查 Team／Private scope 是必要的應用程式邊界。[A02][F11][F13]

## 8. 四項工程候選的 baseline 與 target state

這一節保留原 CTX-01～04 的工程聚焦；**四章完整的行為目標改見[獨立完整規格][T01]**。不要把這四項等同於 system prompt、回合組裝、壓縮與 skills 所有目標都已定義或實作。

本節回應 17:49 的要求，baseline 已依 19:15 凍結材料增補，target state 的四項能力範圍不變。**Baseline 是該凍結時點可證明的狀態；target state 是這項能力完成後，使用者與系統應能觀察到的結果。** 答案串流的 delta 不等於 context 的增量更新，preview 也不等於耐久 turn；不因串流上線就把四項 target 改判完成。這仍不是已開工清單。

### 8.0 共用基準與不變條件

| 項目 | 本次定義 |
|---|---|
| 程式 baseline | HEAD 仍 `257d8a117943b6a17669aed420dfd572d7933d85`＋19:15 未提交 WIP；API／Hosted core 分別為本報告首頁所列兩個不同 hash |
| 新鮮度 | 本研究已核 19:15 的 16/16 raw 副本；17:16 的 9/9 重建與來源方 10/10 確認保留為歷史，不相加冒稱最新完整倉庫核對 |
| 平台版本 | Python Copilot SDK `1.0.13`／runtime `1.0.83`；Foundry **Hosted35**；不在本研究自行升級 |
| 已有能力 | 同 conversation 的 Hosted／SDK SID、前文追問、reset、工作來源工具、回答保存；新增分段串流及非持久 preview，指定前後樣本另列 |
| 保留的控制 | 身分與私人／Team scope、當前操作批准、防重送、工作／成果／unknown 紀錄與 accounting；`budget_enforced=false` 不倒退 |
| 程序生命週期 | 19:15 仍逐輪啟停 SDK，正常 stop 仍是現行保存契約；target 不另外要求常駐，也不授權繞過現行完成條件 |
| 完成與 preview | `durable=false`／`complete=false` 的 preview 只供暫時呈現；記憶處理、恢復點與完成品質判定不能以它代替保存回條 |
| 效能批次接點 | 19:15 已交付 progress 可隨答案／archive 同交易保存及 acquire 純讀合併；沿新接點補 context 差額，不重做這些最佳化 |
| 目標驗收 | 每項只檢查真正差額；以下是未來完成判準，**本輪沒有執行或宣稱已通過** |

來源：[L01][L02][L03][L04][L05][L06][L07][L09][U05]；歷史：[W01][W02][U02][U03][U04]。

#### 四項目標的一頁對照

| 候選 | 已確認 baseline | Target state（一句話） |
|---|---|---|
| **CTX-01：穩定指令與當輪更新** | 短版 system prompt＋SDK history；工作來源靠工具讀取；沒有 FA 的統一版本化組裝／更新契約 | **不變的指令保持穩定，變動的資料在需要時更新；模型看得到適用脈絡，但快取絕不延後權限／批准失效。** |
| **CTX-02：回合後記憶沉澱與後續取用** | 回答／SDK archive 已保存，已有 claim／來源／投影治理，但一般回合不自動沉澱或跨對話注入 | **值得記住且可合法保存的事實，從已保存回合形成有來源的有效記憶；在新對話需要時能正確取用、更正與停用。** |
| **CTX-03：長對話壓縮與可恢復保存** | SDK 有原生 compaction 契約，FA 未明確整合驗收；portable archive 仍是兩檔、128 KiB raw／16,000 bytes 壓縮資料 | **對話變長時由 SDK 管模型 context，必要對話狀態由可靠儲存保存；壓縮與重啟後仍能接續，不因舊 inline archive 尺寸而被迫失憶。** |
| **CTX-04：受控按需 runtime skills** | `mode="empty"`、skills 關閉、具名工具；M3 有批准文字成果，沒有 runtime skill loader | **模型先看到有權使用的技能目錄，只在需要時載入已批准的精確版本，且技能不能帶來額外工具或資料權限。** |

### 8.1 CTX-01：穩定指令與當輪更新

**Baseline。** 19:15 的 `AssistantSession._run_turn` 仍提供 552-byte system text，`run_copilot` 仍以 `mode="replace"` 傳入。沒有 FA 自管的 frozen sections、epoch、context incremental-update protocol；新答案串流並未實作它們。並非「每輪沒有前文」或「所有快取不存在」；既有 `ContextManifest` 也仍不是完整模型封包用量。[L03][L04][F33]

**Target state。**

| 必須成立的行為 | 精確範圍 |
|---|---|
| 穩定內容不因無關事件改寫 | 相同指令／catalog 版本下，**FA 管理的穩定區塊**順序、bytes、hash 相同；時間戳、目前工作進度及檢索結果不混進這段 |
| 變動內容有來源且不重複堆疊 | 每個新 user turn 依其身分、工作及版本取得必要資料；無變更不重貼全份 context，更新可用 delta 或當前版本替換，不能累積無界更新串 |
| 使用者原文及聊天歷史維持各自語意 | 原文交辦不被改寫成系統附註；SDK 仍是聊天 history 的管理者；工作來源與檢索內容保持資料身分，不偽裝成新的人類批准 |
| 新鮮度優先於 cache | 必要工作／批准／來源變動，下一個需要它的回合使用新版；回合中執行副作用前仍核當前權限，不等下次 compaction 才使撤銷生效 |
| 大小與失敗可解釋 | 能列出這次組裝所用的版本／來源、哪些可選資料未放入及理由；必要資料缺失或不適配模型容量時明確回錯，不猜、不靜默裁切 |

**最小完成判準。**

| 情境 | 完成時應看見 |
|---|---|
| 同一有效版本，連續兩輪只有使用者問題不同 | 實際送出的 FA 穩定區塊 bytes／hash 不變；兩則原文各自保留，沒有重複全歷史 |
| 工作來源或已批准指令更新後再追問 | 新回合只有當前有效版本；舊版本不會因 cached prefix 成為現行依據 |
| 第一輪後撤銷某項操作許可，再嘗試操作 | 即使歷史含舊允許文字，現行程式仍拒絕，且不產生外部效果 |
| 相同問題在另一個無權 scope 提出 | 不會取得前一個 scope 的動態內容；驗取出的 bytes／來源，不只看模型說「沒有存取」 |

**沿用接點，不先造新框架。** 優先使用 SDK `system_message`／create-resume options 與必要的 `on_user_prompt_submitted.additionalContext`。官方明說該 hook 是加入脈絡的入口，**不是拒絕 prompt 或強制授權的機制**；硬性權限仍留在 FA 現有控制。採 `replace` 或 `customize` 應依所需可控段落選擇，不因「要快取」就開回 ambient CLI 設定。[S09][S10][S11]

**範圍外。** 不強制實作 Grok 的 `compactionEpoch`、某種 XML 名稱或另一份 prompt KV；不承諾 cache 命中率、降價比例、10 倍速度。穩定 bytes 是可驗收結果，provider 是否命中快取是另外的觀察。

### 8.2 CTX-02：回合後記憶沉澱與後續取用

**Baseline。** 19:15 在 SDK stop／progress drain 後保存回答、archive 與可選最終 progress，仍沒有自動 Memory 抽取；更早到達的 preview 是暫存，不是可沉澱的正式 turn。`ClaimCandidate`／`MemoryGovernance`／projection 這批未變，仍不是完整的「回合→記憶→新對話」閉環。保留的 composition 使用 `DurableHumanReviewVerifier`；型別列了 `rule`／`independent-model`，**不代表自動規則 Adapter 已接線**。[L01][L04][L06][L07][F41][F46][F47]

**Target state。**

| 必須成立的行為 | 精確範圍 |
|---|---|
| 觸發點是已保存的有效回合 | 從有固定來源參照的 user instruction／公開答案／已核對結果判斷候選；不依賴程序退出事件，也不從未完成的串流片段產生正式事實 |
| 不值得記住就明確略過 | 問候、ACK、一次性工作狀態、已失效操作批准不變成長期記憶；任意 assistant 自述不自動當事實，更不保存秘密、私有 reasoning 或整份聊天 |
| 候選有來源、範圍與狀態 | 每筆候選綁來源 turn／內容版本、subject、私人或 Team scope、用途及有效性；同一保存回合重處理不重複產生正式記憶 |
| 自動不等於跳過既有規則 | 明確、低風險且已允許用途的資料可由受信規則處理；矛盾、來源不足、敏感或跨 scope 的內容不自動生效。若目前只有人類審核 Adapter，候選保持待處理，不冒充已自動記住，也不預設每筆都新增一張人工表單 |
| 記憶處理與回答交付分開 | 記憶可能是 skipped／pending／effective／failed-or-unknown；背景抽取尚未完成不應扣住已保存答案，也不能為抽取失敗重跑原聊天或未知付費請求 |
| 新對話真正用得到 | 同一授權 subject／scope 的新 conversation 在相關問題上能檢索有效 claim，必要時回讀精確來源；不是只把 Memory update 寫成功就算完成 |
| 更正先停止使用舊內容 | 更正／撤銷先更新有效狀態，再處理 projection；舊檢索結果不得重新啟用失效 claim。停止使用不等於自動物理刪除原資料 |

目標中的自動 review 規則仍須在落地時明確配置其適用資料，不把「模型判斷可以記住」當授權。保留原有必要性、支持性、矛盾、敏感性及同意用途檢查，不建立另一套一般批准平台。[F46]

**最小完成判準。**

| 情境 | 完成時應看見 |
|---|---|
| 在合成回合明確給出可保存的非敏感偏好，保存並完成記憶處理，再開新 conversation | 有有效 claim／原來源參照；相關回答採用該偏好，無須把舊聊天硬塞進新 history |
| 純問候、無根據的 assistant 敘述、工具文字自稱「已批准」 | 不新增有效記憶或操作授權；略過／拒絕原因可查 |
| 相同回合再次消費，或抽取送出後回應未知 | 不重複新增有效 claim，不盲重送未知抽取；原回答仍可取得 |
| 明確更正後，在新對話詢問；另一個無權 agent 同時查詢 | 新對話只使用新有效值；無權 agent 取不到私人內容；若 projection 過時，精確來源回退或明確 degraded，不猜舊值 |

**沿用接點。** 接在「回合結果已耐久保存」這個 Interface，不綁目前 `client.stop()` 的位置；沿用 `SourceMaterials`／`MemoryGovernance`／projection。必要的處理意圖與 receipt 可由既有耐久控制承接，不先指定新增 queue、scheduler 或第二個 agent loop。Foundry Memory 低階 API 的 scope 仍由 FA 明確供給，原生 update 被接受也不等於抽取已完成。[A02][F13][F18]

**範圍外。** 不要求每輪必呼叫第二個模型；不照抄「每六輪摘要、30 天半衰期」；不把尚未生效的 candidate 放入後續答案；不以本功能改寫已批准 SOP／skill 或工作權威。

### 8.3 CTX-03：長對話壓縮與可恢復保存

**Baseline。** 新 Hosted35 有指定兩輪追問結果；FA 仍未明設 `infinite_sessions`，不能因此說 SDK 原生 compaction 關閉。Checkpoint exporter 與 17:16 bytes 相同，仍只包 `events.jsonl`／`workspace.yaml`，raw `131072`／壓縮資料 `16000` bytes；`ArchiveUnavailable` 契約保留。`TurnArchive.progress` 是新增的可選公開進度欄位，不代表擴充了 SDK archive 的內容或容量。沒有本批觸發 compaction 的真長對話證據；十輪 GET200 也不能代替。[L01][L03][L05][L06][W06][U05]

**Target state。**

| 必須成立的行為 | 精確範圍 |
|---|---|
| SDK 負責縮小模型視窗 | 依現用模型與固定 SDK/runtime 的實際能力管理 context 壓力；FA 不自建競爭中的摘要器或每輪重送全部逐字稿 |
| 持久資料不再被舊 inline 封包綁死 | 有效且仍可處理的 SDK 對話，不會單因兩檔 JSON 超過 128 KiB 或壓縮包超過 16,000 bytes 而失去接續；必要資料由適合的持久儲存保存，控制紀錄只保留必要參照 |
| 保存的是可恢復狀態，而非任意兩個檔名 | 按固定 SDK 的真實恢復契約保留必要檔案／checkpoint／相關 artifact 參照及身份／版本；不能只增大 zlib 上限就宣稱完整 |
| 概要、原件與權威分開 | SDK 摘要可降低模型輸入；需要精確值時能讀合法保存的原件，批准／未知效果仍讀 FA 權威，不能靠摘要猜 |
| 重新啟動不改變對話身分與效果 | 同 SDK session 在新 process 恢復後保留必要前文／tool 關係；既有工作已接受的副作用不重送，未知仍未知。替代 Hosted session 只在另有明確恢復流程時採用，不默默換 SID |
| 保存失敗明確且不毀舊結果 | 部分保存、缺件、hash／版本不符時不從空 history 假成功；既有已保存答案與最後有效恢復點仍可查，只有受影響的接續明確失敗 |

既有對私有 reasoning／system 資料與憑證的處理不能為了 resume 而默默放寬；若某種必要 SDK 檔案與目前匯出處理不相容，須核固定版本的支持契約，不能用刪欄／補欄的猜測解決。這項目標是合規必要狀態的完整恢復，不是保存整個 `$HOME` 或任意內部日誌。

**最小完成判準。**

| 情境 | 完成時應看見 |
|---|---|
| 真正由同版 SDK 產生的合法合成對話狀態，分別超過舊 raw `131072`、壓縮資料 `16000` bytes 界線 | 已保存回答仍可讀，必要狀態可保存／恢復，能接下一輪；要量測真正原封裝大小，不用無關填充檔案冒充 |
| 真正觸發一次 SDK 原生 compaction，再新 process resume | 有同版 runtime 的成功回條／事件，且後續正確回答預設必要事實、回讀精確來源、保留未結操作狀態；只有事件名稱或摘要文字不算通過 |
| 保存途中中斷，或故意移除一個必要 checkpoint／檔案 | 不更新成「可恢復」的假成功；舊回答可讀、接續回明確原因，不創建空白同名 session |
| 恢復後遇到已確認效果、未知效果或另一個 scope 的 state | 不重送已確認或未知效果；跨 scope state 被拒絕，不因檔名或 SDK SID 相似而混用 |

SDK `session.compaction_complete` 型別有 `success` 與可選 pre/post token、checkpoint 等欄位；未提供的值須標未知，不填零。若要宣稱縮減程度，只能用實際回傳值；不能把單次回合數或 GET200 當 compaction 證據。[S13]

**沿用接點。** 先評估固定 Python SDK 的 `session_fs`＋`create_session_fs_handler`／`SessionFsProvider`，或符合既有 SDK 恢復契約的外部封存方式。正式 storage Interface 不等於隨便把每次寫入換成 Blob upload；需滿足實際用到的 read／append／rename 等語意，若該路徑用到 SQLite 再核其交易契約。儲存選擇留待實作設計，不要求現在決定搬哪個資料庫。[S08][S12][S14]

**範圍外。** 不承諾無限 context／無限儲存；不把 SDK 預設 0.80／0.95、Grok 10,000 tokens 當產品必定生效值；不重新加入舊財務 gate；不把常駐 SDK 列為本項先決條件。這些新情境的執行仍須另有批准，本輪只定義判準。

### 8.4 CTX-04：受控按需 runtime skills

**Baseline。** 19:15 仍為 `mode="empty"`、空 `skill_directories`／plugins／instructions 與具名 custom tools。M3 `kind="skill"` 的文字版本／批准分層未變，仍沒有 SDK runtime loader。**只填目錄不夠**：還要對準 `enable_skills`、allowlist、permission handler 及 create／resume 的實際契約；串流支援不會自動打開 skills。[L01][L03][F34][F35][F39][S06][S07][S14]

**Target state。**

| 必須成立的行為 | 精確範圍 |
|---|---|
| 看得見的只限當前有權使用者 | 初始目錄列已批准、適用於當前 Agent／Team 的 name、用途、版本／參照；私人技能不因全域目錄掃描外洩 |
| 先目錄，後本文 | 未選中的 skill 本文不塞進常駐 prompt；使用者點名或任務適合時，透過受控載入取得精確已批准 bytes／hash，不能只顯示名稱就宣稱已使用 |
| 版本與撤銷真的生效 | 同一載入固定 artifact／version／hash；下輪取用按最新有效狀態。舊已載本文可能仍在 SDK history，不能假稱已從模型記憶抹除；但它不再代表現行技能或新操作許可 |
| Skill 不擴權 | 讀到 skill 不取得任意 shell／網路／MCP／跨 scope 存取；需要的工具另依既有可用性與操作批准。本文聲稱「已批准」不成立 |
| Catalog 有大小策略，但不改工作額度 | 依真模型容量優先保留相關／點名條目，省略非必要說明時要標明；有權且被點名的 skill 仍可定位，載不下時明確告知，不靜默假裝執行 |
| Resume 後仍是同一受控能力 | 恢復重新提供必要技能設定並核版本，不從本機全域 skills／plugins 偷補；不重載所有不相關本文 |

**最小完成判準。**

| 情境 | 完成時應看見 |
|---|---|
| 放入一個已批准且有獨特合成規則的 skill，再要求使用它 | 初始 payload 沒有完整本文；實際受控載入後本文 hash 對上批准版本，模型在所需任務採用其規則 |
| 一個無關 skill 與一個他人私人 skill 同時存在 | 無關本文沒有進入模型；私人 skill 的目錄／本文均無法由當前 agent 取得 |
| 切換有效版本或停用後再追問／resume | 使用當前有效版或清楚拒絕；不得因舊 history／cache 仍有內容而自稱新操作已獲准 |
| skill 要求未開放工具或偽稱能擴權；另給超出目錄預算的多技能集合 | 未開放工具仍不可用；目錄省略有紀錄、被點名且有權的技能可定位，沒有偷開全域探索或裁掉必要安全指令 |

**沿用接點。** 優先沿 SDK 自訂 skills／工具探索機制，用 FA 的批准成果提供隔離、版本固定的素材；保持 `mode="empty"`，只加入真正所需的 allowlist／permission 支援。官方文件指出 `custom_agents[].skills` 可 eager preload 全文，因此不能為了通過載入測試就把所有技能掛到該欄位，然後稱作按需載入。[S07][S14]

**範圍外。** 不安裝 plugin marketplace、不啟用通用電腦／browser／shell，不執行任意 skill 附帶腳本；先處理已批准純文字技能的發現／載入。通用技能可能需要額外工具，這是後續能力範圍，不把它混成本項 loader 已完成的證據。

### 8.5 四項之間的最小分工，及與效能批次的接點

| 誰負責 | 可交給其他項目的最小結果 | 不接管 |
|---|---|---|
| CTX-01 | 當輪使用的穩定指令版本與相關動態 context | 不重新管理 SDK 對話、不執行記憶抽取 |
| CTX-02 | 可查來源及有效狀態的記憶候選／claim／檢索結果 | 不把全部回合變成永久記憶、不管理 model compaction |
| CTX-03 | 與 conversation／SDK identity 綁定的可恢復必要狀態 | 不以摘要代替 FA 工作／批准／副作用紀錄 |
| CTX-04 | 有權、已批准、精確版本的技能目錄及載入內容 | 不藉技能文本授予工具權限 |
| 19:15 效能結果 | 已交付串流、非持久 preview、最終 progress 合併保存、acquire 讀取合併及有限前後樣本 | 不把 preview 當 durable turn，不自動擴張成上述四項新功能 |

不要求四項一起實作才能交付：CTX-01 可先用既有資料、CTX-03 不必等自動記憶、CTX-04 可先用已批准 artifact。CTX-02／04 日後透過既有組裝接點取用即可，不再疊統一控制框架。19:15 的穩定接點已知，尤其要接在真正保存完成而非 preview 到達的位置；不因新增 `progress` 欄位或串流方法就重做既有能力。

## 9. 來源索引

### 完整目標規格與本次來源確認

| 索引 | 來源與界線 |
|---|---|
| [T01] | `target-state/v0.1`：四章完整目標、76 個條目、12 個未執行情境；不是實作或評測結果 |
| [U06] | 21:27 要求後維護方確認 code 仍為 19:15；補充 21:19 測試對話持續回代碼的去敏觀察，未提供原私人對話 |

[T01]: grok-aligned-target-state.md
[T02]: context-evaluation-baseline.md#round-1
[U06]: context-research-evidence.md#maintainer-observations

### 19:15 最新凍結程式與效能聚合

| 索引 | 來源與界線 |
|---|---|
| [L01] | 19:15 snapshot：16 份 raw hashes、122 份宣告 module hashes、HEAD、API／Hosted 不同 core |
| [L02] | 四筆 before／after3 聚合、額外成功與失敗計數；不是原文 trace 或瀏覽器 paint |
| [L03] | 最新 `m2_copilot.py`：system replace、SDK lifecycle、空 skills 與未明設 infinite_sessions |
| [L04] | 最新 `behavior_assistant_hosted.py`：stream_model、552-byte prompt（486–492）、最終 progress／save（532–543） |
| [L05][L06] | 最新 wire／instruction：optional progress、省略 None、同交易保存、SdkArchive 邊界 |
| [L07] | `behavior_instruction_stream.py`：BoundedStream、LiveReplies、AnswerEvents；preview 狀態與內容篩選 |
| [L08] | `behavior_assistant_api.py`：真正 model stream、preview 擷取及用量保存；208–284 行 |
| [L09] | `behavior_assistant.py`：acquire_current 的一次一致性純讀與既有 acquire guard；372–423 行 |
| [L10][L11] | 最新模型串流輸出 Adapter／Gateway，不將 response 提前關閉 |
| [U05] | 19:16 維護方回報：部署、兩輪前文、start／stop spans、十輪讀回、保留的 failed／blocked |

[L01]: context-research-evidence.md#wip-1915
[L02]: context-research-evidence.md#wip-1915
[L03]: context-research-evidence.md#wip-1915
[L04]: context-research-evidence.md#wip-1915
[L05]: context-research-evidence.md#wip-1915
[L06]: context-research-evidence.md#wip-1915
[L07]: context-research-evidence.md#wip-1915
[L08]: context-research-evidence.md#wip-1915
[L09]: context-research-evidence.md#wip-1915
[L10]: context-research-evidence.md#wip-1915
[L11]: context-research-evidence.md#wip-1915
[U05]: context-research-evidence.md#maintainer-observations

### 17:16 凍結程式與後續協作歷史

| 索引 | 來源與界線 |
|---|---|
| [W01] | 17:16 snapshot manifest：HEAD／branch／published 狀態／10 個宣告檔案 hash／core 識別 |
| [W02] | 去敏 Hosted 三輪、reset、第 7 輪讀取修正及 `budget_enforced=false` 結果 |
| [W03] | 相對 HEAD 的七檔實際 diff；不是完整 repository 差異 |
| [W04][W06] | `m2_copilot.py`／`behavior_assistant_checkpoint.py`；hash 與初版公開基準相同 |
| [W05] | `behavior_assistant_hosted.py`：`AssistantSession`、552-byte system message、progress／authority lock 與 drain；主要在 284–348、388–491 行 |
| [W07] | `behavior_assistant_wire.py`：Hosted version 欄位、Context 與 SdkArchive 型別 |
| [W08] | `behavior_instruction.py`：Conversation／Hosted SID、reset、SDK archive／結果保存；主要在 18–289 行 |
| [W09] | `behavior_work_policy.py`：register、固定 Hosted version／SID、conversation／reset 與 accounting-only 分支；主要在 84–269、741–866、1104–1149 行 |
| [W10] | `behavior_foundry.py`：verify_session、instruction_target、FoundryExecutor.start；主要在 214–233、386–392、1236–1277 行 |
| [W11][W12] | `behavior_assistant.py`／`behavior_instruction_cost.py`：當前來源核對與模型費用 gate 分支 |
| [U01] | 16:55 中途暫報，只保留歷史，不代表最後部署狀態 |
| [U02] | 17:19 完整 A/B/C 回報；部署 image／revision、GET413 根因、責任邊界與未實作項目 |
| [U03] | 17:49 需求後來源方再確認：17:16 四項基準未變、manifest 10/10 hashes 仍相符 |
| [U04] | 17:51 新效能批次授權與進行中範圍；尚無新的已整合 context snapshot |

[U01]: context-research-evidence.md#maintainer-observations
[U02]: context-research-evidence.md#maintainer-observations
[U03]: context-research-evidence.md#maintainer-observations
[U04]: context-research-evidence.md#maintainer-observations
[W01]: context-research-evidence.md#wip-1716
[W02]: context-research-evidence.md#wip-1716
[W03]: context-research-evidence.md#wip-1716
[W04]: context-research-evidence.md#wip-1716
[W05]: context-research-evidence.md#wip-1716
[W06]: context-research-evidence.md#wip-1716
[W07]: context-research-evidence.md#wip-1716
[W08]: context-research-evidence.md#wip-1716
[W09]: context-research-evidence.md#wip-1716
[W10]: context-research-evidence.md#wip-1716
[W11]: context-research-evidence.md#wip-1716
[W12]: context-research-evidence.md#wip-1716

### Grok 指定四章與來源校準

| 索引 | 來源與定位 |
|---|---|
| [G00] | leescot 首頁 metadata／研究方法；v1.1，snapshot 2026-09-14 |
| [G01] | 系統提示，§2.1 組裝與 §2.2 行為段落 |
| [G02] | 每回合 Prompt 組裝，§3.1–3.4 |
| [G03] | 逐字稿與壓縮，§4.1–4.4 |
| [G04] | Skills 與 Context 預算，第 5 章 |
| [A03][A04] | xAI 官方產品頁／公告，只支持產品層級描述 |
| [A05][A06] | 一般 Grok prompts 與 Grok Build，僅用於釐清不同來源 |
| [C01] | 非官方 gist 分析，只作有限描述交叉比對，不作官方證明 |

[G00]: https://leescot.com/research/grok-bot-internals/
[G01]: https://leescot.com/research/grok-bot-internals/#doc/system-prompt
[G02]: https://leescot.com/research/grok-bot-internals/#doc/prompt-assembly
[G03]: https://leescot.com/research/grok-bot-internals/#doc/compaction
[G04]: https://leescot.com/research/grok-bot-internals/#doc/skills
[A03]: https://x.ai/bot
[A04]: https://x.ai/news/introducing-grok-bot
[A05]: https://github.com/xai-org/grok-prompts
[A06]: https://github.com/xai-org/grok-build
[C01]: https://gist.github.com/gfsaaser24/9f785c0c095b693b31b5efbcbae7bcd0

### FA：保留的公開 commit 原碼、測試與歷史記載

| 索引 | 來源與定位 |
|---|---|
| [F00][F01] | 比較 commit、已合併的工作台 PR 與歷史部署結果 |
| [F02][F03][F30] | 依賴版本、舊模型常數 |
| [F06][F07][F09][F10] | SDK 組態、最新自由指令入口與 `read_context` |
| [F08][F21][F22][F32] | Conversation、SDK archive、保存與恢復 |
| [F11][F12][F13][F14][F31][F36] | 原共享 Context、M4、Memory、階段化提示及 API 接線 |
| [F15][F16][F17][F18][F20][F33] | 治理式 Context、投影、tokenizer 與 manifest |
| [F19][F23] | Provider 封包界線、context limit 與費用計數 |
| [F24][F26][F27][F37][F44][F45] | Archive 超界、圖片裁切、必要原件、兩輪對話的測試契約 |
| [F04][F28][F29] | 最新實測狀態、M4 後續補驗及 SDK-first 修訂 |
| [F34][F35][F38] | SOP／skill 的內容、批准版本與固定政策清單 |
| [F39][F40][F43] | Permission handler、公開進度型別、受控模型／HTTP 邊界 |
| [F41][F42] | Claim 種類／subject 目錄、一般回合完成後的實際保存路徑 |

[F00]: （原私有 repo 連結）
[F01]: （原私有 repo 連結）
[F02]: （原私有 repo 連結）
[F03]: （原私有 repo 連結）
[F04]: （原私有 repo 連結）
[F05]: （原私有 repo 連結）
[F06]: （原私有 repo 連結）
[F07]: （原私有 repo 連結）
[F08]: （原私有 repo 連結）
[F09]: （原私有 repo 連結）
[F10]: （原私有 repo 連結）
[F11]: （原私有 repo 連結）
[F12]: （原私有 repo 連結）
[F13]: （原私有 repo 連結）
[F14]: （原私有 repo 連結）
[F15]: （原私有 repo 連結）
[F16]: （原私有 repo 連結）
[F17]: （原私有 repo 連結）
[F18]: （原私有 repo 連結）
[F19]: （原私有 repo 連結）
[F20]: （原私有 repo 連結）
[F21]: （原私有 repo 連結）
[F22]: （原私有 repo 連結）
[F23]: （原私有 repo 連結）
[F24]: （原私有 repo 連結）
[F25]: （原私有 repo 連結）
[F26]: （原私有 repo 連結）
[F27]: （原私有 repo 連結）
[F28]: （原私有 repo 連結）
[F29]: （原私有 repo 連結）
[F30]: （原私有 repo 連結）
[F31]: （原私有 repo 連結）
[F32]: （原私有 repo 連結）
[F33]: （原私有 repo 連結）
[F34]: （原私有 repo 連結）
[F35]: （原私有 repo 連結）
[F36]: （原私有 repo 連結）
[F37]: （原私有 repo 連結）
[F38]: （原私有 repo 連結）
[F39]: （原私有 repo 連結）
[F40]: （原私有 repo 連結）
[F41]: （原私有 repo 連結）
[F42]: （原私有 repo 連結）
[F43]: （原私有 repo 連結）
[F44]: （原私有 repo 連結）
[F45]: （原私有 repo 連結）
[F46]: （原私有 repo 連結）
[F47]: （原私有 repo 連結）

### 固定 Copilot SDK 契約與官方平台文件

| 索引 | 來源與定位 |
|---|---|
| [S01] | 固定 SDK commit |
| [S02][S08] | Session persistence、多租戶、Python 正式 session filesystem 擴充 |
| [S03][S04][S05] | 原生 compaction／大輸出／工具探索型別、設定傳遞、context clearing 條件 |
| [S06][S07][S09] | `empty` mode、skills、system message replace／customize |
| [A01][A02] | Foundry Hosted 協定／儲存分工、Memory 低階 scope 契約 |

[S01]: https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e
[S02]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md
[S03]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L1253-L1323
[S04]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L2792-L2805
[S05]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/context-management.md
[S06]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/_mode.py
[S07]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/skills.md
[S08]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/setup/multi-tenancy.md
[S09]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L284-L381
[S10]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/hooks/user-prompt-submitted.md
[S11]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L1013-L1170
[S12]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session_fs_provider.py
[S13]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L7731-L7899
[S14]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L2245-L2330
[A01]: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents
[A02]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage

引用方式：`G` 是使用者指定的作者研究；`L` 是 19:15 新凍結材料；`W` 是保留的 17:16 材料；`F` 是公開 FA commit；`S` 是固定 SDK commit；`A` 是官方文件／repository；`C` 是非官方交叉參考；`U` 是有時間點的維護方回報。`L`／`W`／`U` 不冒充已發布 commit 或本研究重跑。官方文件可能更新，其新版範例不自動成為固定套件的契約；Grok 作者 bundle 行號也不是官方 commit permalink。
