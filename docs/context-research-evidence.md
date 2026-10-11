# Context 研究：證據索引與發布界線

整理日期：2026-09-24。此頁只發布來源識別、核對範圍與結論，不包含原始對話、SDK state、模型／工具日誌、憑證、資料庫或本機路徑。

## 怎麼讀這批文件

| 文件 | 用途 |
|---|---|
| [四章完整比較](grok-context-comparison.md) | Grok 作者描述、FA 公開原碼、後續 WIP、平台能力與建議分層 |
| [完整 Target State](grok-aligned-target-state.md) | 原 `target-state/v0.1` 的 76 個目標及 12 個跨回合情境，不是已驗收清單 |
| [評測起步與兩輪 baseline](context-evaluation-baseline.md) | M4 checker 的限制、兩輪人工接受，以及不能外推的品質結論 |
| [CTX-03 本機驗收結論](ctx03-local-acceptance.md) | 一般保存、私有資料阻擋、default-positive 可行性與產品未採用的區別 |
| [CTX-03 保存契約](ctx03-checkpoint-contract.md) | Q11 唯讀核對及最小來源接線設計已確認為 `v1`；產品尚未實作或啟用 |

**本 PR 只有文件，不包含維護分支的產品 WIP。** 公開 commit 的 permalink 可查原碼；WIP 與本機實驗的原件未隨 Git 提供，讀者不能只靠這個 PR 重現它們。SHA-256 用來識別原件，不會把一份結果摘要變成原件或獨立實驗。

日期／時間以下均以台北為敘述時區；UTC snapshot 時間另列。`core` 是原執行方對 module hash map 的摘要，不是 Git commit SHA，也不是新部署證明。

## Public-source baseline

| 類別 | 可查來源／適用範圍 |
|---|---|
| FA 公開研究基準 | `6f9a637fab51785c70d093fdca0b0d1ac416aa04`，包含已合併的 #2；不是後續未提交 WIP 的內容 |
| 固定 Copilot SDK | [`f13e4a2cc7e4e220974d2333142234e162a3252e`](https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e)，對應研究使用的 Python SDK `1.0.13` |
| Grok 四章 | [系統提示](https://leescot.com/research/grok-bot-internals/#doc/system-prompt)、[每回合組裝](https://leescot.com/research/grok-bot-internals/#doc/prompt-assembly)、[逐字稿與壓縮](https://leescot.com/research/grok-bot-internals/#doc/compaction)、[Skills](https://leescot.com/research/grok-bot-internals/#doc/skills) |
| Grok 證據等級 | 作者 `v1.1`／2026-09-14 snapshot 的研究重建，不是 xAI 對現行 production 的官方原碼或行為保證 |

比較報告的 `F` 與 `S` 引用保留固定 commit／行號；Microsoft Learn 的連結屬研究當日查閱的官方文件，可能後續更新。外部文件有某項能力不代表 FA 已接線或已實測。

## WIP-1716

對應比較報告 `W01`–`W12`：2026-09-23 17:16 凍結包，HEAD 為 `257d8a117943b6a17669aed420dfd572d7933d85` 加未提交修改，snapshot UTC `2026-09-23T09:16:36.862731+00:00`。

| 原件識別（未附） | SHA-256 |
|---|---|
| `context-comparison-snapshot.json` | `8556e7fb9f91e7b57b478b46b17366abe58d89fb197d5a225296e70b3e869841` |
| `context-comparison-evidence.json` | `fec6b4f9eda4228e07c81be77a4c613db4104380a2422fd2ae8ae6499e0fc4a4` |
| `context-comparison-worktree.patch` | `72850bd4cf0f0317f4ae21650a21848edefbbada07d0b0997e54dc328d439607` |

核對方法是從本地 Git object 取得固定 HEAD，套用提供的七檔 diff，另對兩個未改 tracked 檔案比對，共九份內容 hash 相符。Manifest 的第十個 `behavior_work_budget.py` 當時只有 hash，沒有 raw 本文；**不宣稱十檔全由研究者讀過，更不宣稱整個 core 已獨立重建**。

`W04`–`W12` 分別定位 `m2_copilot.py`、`behavior_assistant_hosted.py`、`behavior_assistant_checkpoint.py`、`behavior_assistant_wire.py`、`behavior_instruction.py`、`behavior_work_policy.py`、`behavior_foundry.py`、`behavior_assistant.py` 與 `behavior_instruction_cost.py`。原報告保留對應函式／行號；本索引不把 WIP 行號冒充公開 commit 的同一行。

## WIP-1915

對應比較報告 `L01`–`L11`、Target State 的 `B01`–`B06`：2026-09-23 19:15 凍結包，UTC `2026-09-23T11:15:55.375264+00:00`，HEAD 仍相同且有未提交 WIP。

| 原件識別（未附） | SHA-256 |
|---|---|
| `snapshot.json` | `cd7f9ef2f632e9ea7c663ee026916316caf83817fc68527643f314e3252931fa` |
| `stage-comparison.json` | `dc5d87453f0639f4c7075a4236669f45e2f8c318fe765c9929e82242444afba4` |
| API core | `f164083c09abcfeaea11406a5fbf4ba9a66b67fb0cc29400a1695195dd3f0225` |
| Hosted35 core | `fcaa439bf6b113726c12bbb2ea8b7da3b2cd775a588ca3d6a965925fd8e5f35b` |

研究者核對了 16 份 raw 副本的 bytes/hash。Manifest 另列 122 份 module hashes，**不是 122 份原碼全部交付或重新計算**。六個與本地公開版本相符的未變分層另作核對，不能與前一批九份相加，湊成一個「最新完整倉庫」的數字。

| 引用 | 凍結材料中的定位 |
|---|---|
| `L03`／`B02` | `m2_copilot.py`：system replace、SDK lifecycle、空 skills／未明設 infinite_sessions |
| `L04`／`B03` | `behavior_assistant_hosted.py`：串流、552-byte prompt、最終 progress／save |
| `L05`、`L06`／`B05`、`B06` | wire／instruction：optional progress、省略 None、同交易保存與 archive 限制 |
| `B04` | `behavior_assistant_checkpoint.py`：仍與前版兩檔 archive 相同 |
| `L07` | `behavior_instruction_stream.py`：非持久 preview |
| `L08`、`L09` | `behavior_assistant_api.py`／`behavior_assistant.py`：模型串流、用量與 acquire 讀取 |
| `L10`、`L11` | 模型串流輸出 Adapter／Gateway |

`stage-comparison.json` 是 console API 時間聚合，不是完整文字 trace、provider TTFT 或瀏覽器 paint。四筆主要 before／after 樣本之外，另列中途失敗與額外成功；不隱去它們後宣稱整批零失敗。

## Maintainer-observations

比較報告 `U01`–`U06` 與 Target State `U01` 原本指向協作回報。發布版不保留 app session ID、私人訊息全文或本機導覽連結，而將來源等級明示為**維護方在指定時點提供的結果**。

| 時點 | 可以引用的限定結論 |
|---|---|
| 9/23 16:55 | 中途 build／fence 狀態；不是最終部署結論 |
| 9/23 17:19 | Hosted33 多輪、reset、GET413 讀取修正、accounting-only 與未結歷史 |
| 9/23 17:49 | 來源方再次比對其十份 hashes；與研究者的九份重建核對分開 |
| 9/23 17:51 | 另批准效能工作，不代表四項 context 候選同時開工 |
| 9/23 19:16 | Hosted35 串流、指定兩輪前文、start／stop、保留的 failed／blocked |
| 9/23 21:27 | 四章 code 仍可沿用 19:15；原測試對話持續代碼限制不等於 UI 重用答案 |
| 9/23 22:05 | 清理獲准的測試聊天後另開乾淨對話；API 讀取最佳化與行為 baseline 分開 |

回報沒有捕獲的原始 spans、完整 provider payload、私人對話或瀏覽器 timing，不會由摘要補造。這些引用不是目前雲端狀態的即時讀回。

## Evaluation-baseline

| 批次原件識別（未附） | SHA-256 |
|---|---|
| Round 1 三回合合成 JSONL | `a72e5ff34336ab1927555f540e6b57b1bc0d4387b70bf79c5309571c25e16120` |
| Round 1 snapshot | `9c5f3127abd96d4e5c2c66019bc3598c44fa05ae3f6179c7213d8dda9ab05772` |
| Round 2 固定 `cases.json` | `34c2359bde092752492197425b52bb7852f2c8404d0f3bf1102adf609db61e81` |
| Round 2 獨立 `expectations.json` | `8d826c3fadfdc904d5a060dbf57070f0f95c9fc6fd53b09da84f088d3775f3c1` |
| Round 2 公開內容投影 JSONL | `a4f6caaccfb2f6a60a840a18a4bdc7455436484b5419faccd31360f712e4b815` |
| Round 2 manifest | `4db2ef6e5e885075e5b92edc0b53a939aaa75aba33fc6ff01c2a68da04644902` |
| Round 2 snapshot | `f92f82b76de930dfdc8c0045b1d82c4be6216659a26dcf6fc74a3c9d1199b403` |
| Round 2 客觀結果 | `c97280f40c51ffa15532073a818c4406aca498032c315bf753da38d63f74091f` |
| Round 2 人工批次收尾紀錄 | `e8b2302d8fbaa513aaef1174dcebc29bfccd4eac128a2bead622132ac4ebca54` |

原件雖已去敏並獲准本機人工 review，仍不因本次文件 PR 自動公開。發布的是[集合結構、接受狀態及限制](context-evaluation-baseline.md)，不是個別對話或人工原文註記。

## CTX03-local

產品 WIP 的 HEAD 仍為 `257d8a117943b6a17669aed420dfd572d7933d85`，不是這個文件 PR 的 commit。兩次本機 core 與部署分開。

| 批次原件識別（未附） | SHA-256 |
|---|---|
| 9/24 09:36 snapshot | `0f288782c9bbeadf7943c0d016a0ca38a425e76836802a99d1e4281e93e18f8f` |
| 09:36 local evidence | `38ad86bb5fb4593e4a95c5c3b98b6ce73b21371984b882f6bacd89caf92e8745` |
| 09:58 snapshot | `78ee34efdac2291e678d98e6ca6e7d4d390bb2ca3c8ac392ae8a9e8850f66d3e` |
| 09:58 boundary evidence | `dd7dffca8e0b083f0546c70ae7a33042c08ea00ffc720817570ac3cc043c944e` |
| 11:34 candidate B result | `16b282ce28161789083c33c9d81187b9e52d11d443e5345120267be76e1236b6` |
| 11:44 metadata-only location diagnostic | `bdacc6d98a573023640d0fbc0ce2f861f79816fa5618f0bf582291b57206b616` |

09:36 的 raw compaction 恢復只保留為歷史接線證據，不能繼續作為符合私有保存規則的 PASS。09:58 一般 filtered state 可恢復，但 raw model snapshots 被拒絕；11:34 未得到合規 candidate，fresh resume／candidate-dependent negatives 未跑。11:44 新診斷只定位禁止值殘留於 `session.compaction_complete.data.customInstructions`，不回填前兩次未捕獲的證據。

## CTX03-default-positive

UTC snapshot `2026-09-24T04:31:23.093756+00:00`。固定 SDK `1.0.13`／runtime `1.0.83`；本機 production core 仍為 `3022d8eb509031d597507a68d473d3cc98583f0c294f0d372c0499f926172508`，未部署。

| 原件識別（未附） | SHA-256 |
|---|---|
| `result.json` | `7ff14092a9231b4bca6eb8c270afe23634e72960668335b12da4cb5f8718e87c` |
| `snapshot.json` | `36c253314307819459bbb1125d539ad2b893f8fa06b69581371332783ee4b078` |
| test-only additions patch | `e4bc25a19f551cb33f556a1b702e9f43e4032930617dd7564a0b25ee78ba9a6c` |
| 固定候選 `candidate_b.py` | `3c92344247a9007a31b53ad6e5d1f9f97fb222ec87baab280ba4d81965c633bc` |
| 研究者收件裁決 | `f0992170e79b569834310fdaedb92b550a9d27049bde88c81c9be8d3c71be814` |

收件核對為 13 份 snapshot scripts／safe proofs、15 份 root evidence，加 manifest 共 29 份；新舊 manifest 的 126 個 module hash entries 一致。這不是重新讀取 126 個 live modules，也不是研究者再次執行 SDK、開啟對方 live SQLite 或重新驗雲端。

限定結論：無 `custom_instructions` 的一次 manual compact，經候選 B 轉換後可完成兩個新程序續問／更正；16 項既有 test-only 負向檢查通過。原敏感案例仍 blocked，production 仍拒絕 raw 與 metadata-only candidate。詳見[驗收結論](ctx03-local-acceptance.md)。

## Q11-feasibility

9/24 13:25 補交的資料只讀既有保存點，沒有新的 SDK／模型／Azure 執行。另取得兩份 exact-hash 原碼，使 Q11 所用十五個 module 都對上原 `3022d8eb…` 基準；閱讀的是需要的定義與呼叫路徑，不是全倉庫審核。

| 原件識別（未附） | SHA-256 |
|---|---|
| Q11 `manifest.json` | `133109790999ec61498df9988902e24f4006d624277b3b72cdb40b8b5623b011` |
| `compaction-metadata.json` | `1bcfb3211b16f8213f8ff48bdf34ddba77735c6d8ac4390b2d20625bd53e7f83` |
| 唯讀匯出程式 | `9a04ec5e806d0a518f1119a17a28361ebecb3a41716f18f459daa41e9dbb61a6` |
| `behavior_runtime.py` | `b18713b8fb57aff31a40f010d74ff9ce7006753634f664036ec0aecec2525b0b` |
| `behavior_instruction_models.py` | `8e89b8f7ef88203f85d0253be9313092e2af4a5d3cc4828a80877f8ca52b5c65` |
| 固定官方 `generated/rpc.py` | `0dd7ca6eb6c816fb4d1e672eeb8a8c33b755a1b85708cc2d24d820d499cbcb3c` |
| 固定官方 `generated/session_events.py` | `2fa7175a66dbd5d76c9db9d33ff6d3b18dd383fd8065838cbc94893d2eb885aa` |

維護方以 SQLite URI `mode=ro` 加 `query_only=ON` 讀取原 published parts，先核 reference、receipt、parts 與完整 state hash；沒有呼叫會建立資料表的 test store constructor。前後十八個 DB／reference／proof 相關檔案 hashes 不變。研究者核對五份新凍結檔案及三份既有 reference 的對應，沒有自行讀 live store。

三個保存點 candidate／follow1／follow2 都保留同一次 compaction：start 的 0-based index 為 24，complete 為 32。兩種事件的 `trigger`、`customInstructions`、`requestId`、`serviceRequestId` 全是 **missing**；不是 null／empty，不推定或補成 manual。這是既有資料的新唯讀觀察，不是三次新 compaction。

| 原碼定位（未提交 WIP，非公開 permalink） | 可支持的結論 |
|---|---|
| `behavior_assistant_wire.py:27–49,86–107`；`behavior_assistant.py:88–136` | Instruction／profile／task 可固定原指令、版本、來源與 digest，沒有 compaction mode/profile |
| `behavior_runtime.py:390–429`；`behavior_work_policy.py:942–960`；`behavior_assistant.py:234–276` | 核當前 owner、policy、job、registration/material 與取消／capacity；不是某次 compact 的來源回條 |
| `m2_copilot.py:617–663`；`behavior_assistant_hosted.py:377–389,519–548` | 產品路徑 create/resume/send，記 stop 並保存，未接受控 compact／對應結果 |
| `behavior_sdk_checkpoint.py:16–26,97–151`；`behavior_wire.py:243–247` | Upload 綁 job/session/state/stop，在停止後建立；stop 不是 compaction attribution |

固定 SDK 的 [request 定義](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L23717-L23752)明說：未傳 `trigger` 時，保存事件沒有 trigger attribution，initiator unknown。[事件型別](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L7731-L7753)確實有相關可選欄位，不能說 SDK 不提供；[enum](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L12204-L12215)描述 manual 可來自 compact API，也不能反推無參數呼叫一定寫 manual。

結論是 **現有 authority 可重用，但 Q11 的動作來源接線尚未實作**。原 default-positive 的恢復結果不變；不能單靠其保存事件滿足新產品的來源 gate。使用者已確認[契約第 2.2 節的最小來源接線設計](ctx03-checkpoint-contract.md)，定稿為 `v1`；沒有批准產品實作、補跑或部署。

### 合併後的來源定位

研究文件與原產品改動分別由 #3、#4 合併。上表的「未提交 WIP」描述的是唯讀核對當時的材料；#4 合併後，可在固定 commit `f59157a8ee959b9c6e6c00a56305373538993d6a` 查閱產品原碼，例如 task/profile、authority、registration 核對與 SDK state 發布。原始 traces、SDK snapshots 與資料庫仍未隨這份文件提供。

合併後 main `79fb62bf8c0eaeceb05f7bbf199ca664a94c616b` 的 126 個 module Git blobs 使用 LF；原凍結副本使用 CRLF。逐檔只轉換換行後，126 個 hash 都對上原 manifest，沒有其他未解差異；但 **raw bytes／core hashes 並不相同**，不能直接互換舊基準。此核對只讀 immutable Git objects，不是新的 build、部署、SDK 或模型驗收。

## 發布時保留與排除的資料

保留原創比較、固定來源連結、目標條目、集合計數、限定結果、SHA-256 及明確未驗項目。排除 raw JSONL、模型 request／response、私人 system／reasoning、credential／ticket、checkpoint 內容、SQLite、app session ID、本機絕對路徑與臨時 review URL。

原受限材料保持不變。未提供的原件不製造替代附件，不把摘要或 hash 稱為可公開重現的全套證據；需要進一步核實時，應另外決定合法的最小材料與存取方式。
