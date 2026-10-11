# CTX-03：本機保存／恢復的驗收結論

原驗證日期：2026-09-24；合併讀回更新：2026-09-25。**PR #8 的 `c51e730` 已修signed-token P2、通過Opus 5.5兩軸review並合併**，merge commit `457b01a75b1f0a2c4559ebad67dfc1e1b1bbd644`；沒有部署，完整 CTX-03仍PARTIAL。第1–12節保留原基準、scanner修正及當時review；短history red→green見第13節，不回填舊批次。

此文件是驗收結論的 repo 發布版，不附 raw SDK state、private system／reasoning、原始 traces、資料庫、模型／工具日誌或本機路徑。原件 hash、核對方法及非公開來源界線見[證據索引](context-research-evidence.md#ctx03-local)。產品 WIP 不在本 PR；本頁不代表新部署或允許放寬正式 validator。

原產品 ordinary 保存程式後來已合併，新 diag codec 的接線在獨立 PR #8；「本 PR」不應被理解為把所有歷史程式都包含在同一個文件／評測 PR。各段按自己的固定 commit／snapshot 判讀。

## 1. 12:31 歷史基準的三種結果

| 路徑 | 截至本次的結論 |
|---|---|
| 一般 filtered 大狀態的產品保存路徑 | 本機真 SDK create→新 process resume 成立，跨過原 inline 容量；API-only 依賴已修 |
| 固定版、default manual compaction 的候選 B | 只在隔離 test-only store／DTO 上完成保存→兩個新程序更正／回想 |
| 正式 compaction 保存／恢復 | **仍 blocked**：production `SdkState` 拒絕 raw 與 metadata-only candidate；未採用新保存契約 |

原敏感 `custom_instructions` 反例維持 `PRIVATE_VALUE_RETAINED`。新的 default positive 不傳該參數，沒有把舊負向改成 Pass，也沒有增加第三種欄位清除策略。

## 2. 固定基準

以下是原 09:58／12:31 的本機材料與當時雲端紀錄，不是第 10 節新產品 PR 的 commit 或目前環境讀回。

| 項目 | 識別 |
|---|---|
| 產品 WIP HEAD | `257d8a117943b6a17669aed420dfd572d7933d85` 加未提交變更 |
| 最新本機 product core | `3022d8eb509031d597507a68d473d3cc98583f0c294f0d372c0499f926172508` |
| Python SDK／runtime | `1.0.13`／`1.0.83` |
| SDK 官方 source | [`f13e4a2cc7e4e220974d2333142234e162a3252e`](https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e) |
| Native 實驗 | Windows x64，真 SDK／runtime，明確的 offline HTTP model fixture |
| 既有雲端 | Hosted35／API `chat-snapshot-0923` 保持，沒有部署 CTX-03 新版本 |

`core` 不是 Git commit，也不是 Azure image 的即時盤點。所有「本機通過」都不能轉寫成 Hosted／Azure Blob、跨 OS、共享 filesystem 或永遠同 PID 已驗收。

## 3. 原本固定的八項接受條件

以下整理原 `C03-01`–`C03-08`，不因候選成功修改原判準。完整目標仍見 `TC-11` 與[四章 Target State](grok-aligned-target-state.md)。

| ID | 必須成立 | 本次證據層次 |
|---|---|---|
| C03-01 | 用固定 SDK 正式保存 Interface 或最小且有理由的 Adapter，不另建 transcript engine | 採正式 `session_fs`／`create_session_fs_handler`；任意事件改寫並非官方保證 |
| C03-02 | 必要 SDK 產生狀態跨過原 raw `131072` bytes 界線，仍能保存與接續 | 有大型普通對話的真本機結果，不用無關 padding |
| C03-03 | 同時跨過原 compressed `16000` bytes 界線，非只改常數 | 有實際 zlib 比較量測；新路徑不是用壓縮 bytes 寫 Blob |
| C03-04 | 同 conversation／SDK identity，在新的 Python／runtime process 恢復必要來源及工具關係 | 普通路徑與 default test-only 候選分別有本機證據 |
| C03-05 | 缺件、hash／版本／scope 不符、partial save／unknown 不變成空白成功 | 既有產品負向與 candidate-only 負向分開記錄 |
| C03-06 | 保留 preview、normal stop、隔離、已知／未知效果與 accounting 契約 | 產品 module hashes 保持；不代表新增候選已完成產品 once 整合 |
| C03-07 | 若執行原生 compaction，需真成功事件／state，加新 process continuation | **test-only default 路徑成立；正式產品驗收仍 blocked** |
| C03-08 | 舊 inline `SdkArchive`／`ArchiveUnavailable` 仍可依原契約讀取 | 無資料遷移或舊紀錄改寫；不將 raw 初版默認成新合規格式 |

## 4. 產品切片的實作方向

`behavior_sdk_files.SdkSessionFiles` 使用固定 SDK 的正式 filesystem Interface；`behavior_sdk_state.py` 僅放純 DTO／驗證，API 不需要匯入 Hosted SDK。

SDK 自己寫 events、workspace、checkpoints 等完整檔案；FA 重用 `ArtifactStore`／`BlobArtifactStore` Interface，以每片最多 **512 KiB** 的 create-only、固定版本／ETag／hash 及讀回保全保存資料，Table 僅留小參照。總量沿既有 **8 MiB** 技術界線，不宣稱無限。

Upload intent 先持久化，完成所有 parts 的讀回後才發布 reference。正常順序仍為 SDK `session.disconnect()` 刷完 SessionFs callbacks，再 `client.stop()`，最後依原答案／archive／progress 交易完成保存。拒絕／未知不重送 PUT，不以空白同 SID 假裝恢復，保留舊答案與最後合法 checkpoint。

這裡描述的是已核對的未提交產品切片，不是本 PR 新增的程式。跨程序實驗的 store 是 **test-only SQLite ArtifactStore**；沒有宣告 SDK SQLite capability，也沒有對真正 Azure Blob 新增寫入驗證。

## 5. 歷史結果不可合併成同一次 Pass

| 時點 | 觀察 | 保留的判斷 |
|---|---|---|
| 09:36 初版 | 四個真 process：create、resume、manual compact、resume compacted | Raw 保存漏掉原私有欄位處理；其恢復成功撤回作為 privacy-compliant PASS 的依據 |
| 09:58 窄修 | API-only imports 分離；legacy private filter 共用；普通大狀態可恢復 | Compaction raw model snapshots 保存被拒絕；最後合法 reference 保留 |
| 11:34 候選 B | 第一個 fixture 未 exercise 指定 private marker；第二次仍有禁止值殘留 | 發布前停止，fresh resume／candidate-dependent negatives **NOT_RUN**，不判 runtime 相容性 |
| 11:44 限定診斷 | 35 events；legacy 後三個 matches，清兩個 data 後剩一個 | 殘留在 `session.compaction_complete.data.customInstructions`；只定位，不修正或放寬 |
| 12:31 default positive | 不傳 `custom_instructions`，固定候選完成 compact→兩個新程序續問 | 只支持此固定版 test-only 路徑；原敏感案例與 production guard 不變 |

09:36 的原件、09:58 的拒絕、候選 fixture 失敗與 11:44 新觀察都各自保留；後來捕獲的 native PID／位置不能回填成前一批已捕獲。

## 6. 私有保存的既有規則

共用 `public_events` 保持舊 `export(public_only=True)` 的八個具名欄位：

```text
reasoning_text  reasoningText  reasoning_blocks  reasoningBlocks
system_message systemMessage system_prompt     systemPrompt
```

三種 private events 為 `assistant.reasoning`、`assistant.reasoning_delta`、`system.message`，沿原契約清除 `content`／`delta_content`／`deltaContent`。保留 event ID、parent、順序與一般答案／tool 關係，不刪 event。

已知 transport credential 與被移除的 private 值只在記憶體作禁止保存值。若 opaque sidecar、summary 或其他位置仍有副本，保存前拒絕；API DTO 也驗證，不能只信 client 標記。這不是通用 secret detection／語意 DLP 的保證。

正式 validator 目前仍拒絕 `model.model_call_success`、`model.messages_snapshot`，包含 test-only 候選的 `data={}`。稱 checkpoint「不公開」不能取代私有保存規則；保存全文需要授權，不能只為 resume 成功而擴張。

## 7. Default positive 的限定證據

候選 B 演算法固定為 `3c92344247a9007a31b53ad6e5d1f9f97fb222ec87baab280ba4d81965c633bc`。先走 legacy filter，之後只在兩個 exact event types／已知 shape 上將 `data` 清為 `{}`；其餘 envelope、id／parent／order、其他 data、檔案集合與 sidecar bytes 保持。

本批只做 **一次 default manual compact＋兩個 fresh-process resumes，共三次 offline model requests**。Ordinary create／resume 沿用安全原件複本，沒有重跑；原敏感 custom 反例也沒重跑。

| 階段 | 事件數 | candidate bytes | zlib 比較 bytes | reference canonical JSON bytes |
|---|---:|---:|---:|---:|
| Default compact | 35 | 175,338 | 46,131 | 669 |
| Follow1：更正後保存 | 42 | 179,346 | 46,695 | 667 |
| Follow2：恢復更正與先前答案 | 49 | 183,330 | 47,169 | 667 |

Reference 的 669／667／667 是 producer 的 canonical JSON bytes；易讀 JSON 檔案本身為 832／830／830 bytes，不能混用。三個不同 runtime 都正常 disconnect／stop，force stop 為 0；Windows `exit_code=1` 原值保留，與成功 Python driver 的 `0` 分開。

### 不只是預製答案相同

Follow1 從 candidate 恢復，檢查**實際 SDK 送出的 model messages**含原 source／native summary，當前 user correction 改為 `851`。Follow2 從 follow1 的新保存點恢復，檢查 prior user correction 及 prior assistant answer 仍在；離線 transport 先核這些輸入才產生回答。

原 `catalog-read-1` 的 start／result event ID 與 data hashes 保持，兩個續問沒有工具重播。這證明保存與 model-facing 接線，不是真 LLM 對更正或摘要的理解品質。

### 私有 marker 的 exercise 範圍

本次 default compact 的 model request／raw state **沒有觀察到已知 canary**，包括已配置但未進入該 compact request 的 current system marker。因此不能說「候選成功移除敏感 customInstructions」。

Follow1／2 各自的 current system marker 有在 raw 出現，legacy 處理後不在保存 parts；之前 private／credential 值不回流到後續 model-facing history。這是已 exercise 的部分，不泛化為所有內容均安全。

### 負向與保存完整性

既有 candidate-dependent suite 回報 **16 passed**，無模型：scope／SDK／runtime／hash／missing part、必要 checkpoint／index／empty state、unknown shape／envelope／prior payload、private envelope／sidecar／summary、production 拒絕及 unknown PUT 不重送。

缺 checkpoint 的拒絕是 **candidate integrity loader 在啟動 runtime 前**發生，不是 runtime 對所有缺件的原生保證。原 ordinary refs／answers／兩列 store 保持；隔離 candidate store 有三個 published checkpoints，加一個故意模擬的 unknown pending attempt，後者無 reference、不重送。

研究者核對 29 份凍結檔案與兩份 manifest 的 126 個相同 module hash entries，沒有重跑 SDK／模型或讀 live stores。上述 store 完整性依據是執行方凍結的 assertions／結果，而非研究者另一次資料庫實驗。[本批原件識別](context-research-evidence.md#ctx03-default-positive)

## 8. 官方 Interface 與未知

固定 SDK 的 [session persistence](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md)、[SessionFsProvider](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session_fs_provider.py)、[SessionFs E2E](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/e2e/test_session_fs_e2e.py) 與 [compaction E2E](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/e2e/test_compaction_e2e.py) 支持正式接點及原生測試路徑。

查閱範圍內未找到對兩個 raw event types 的專用寫入抑制契約；這不是證明所有版本永遠不支援。`eventLog.read(types)` 是讀取 view，不是寫入 filter；typed parser 不是去敏器；官方 E2E 沒 assert 某個欄位，也不能推成所有 consumer 都不依賴它。

Candidate B 的實測不取代官方全 consumer 保證。Automatic compaction、其他版本、cross-OS、Hosted／Azure Blob、接近 8 MiB 的雲端情境及真模型品質仍未驗。

## 9. 契約已確認，產品尚未接入

研究文件 PR 建立後，已完成 Q11 唯讀可行性核對，使用者已確認[正式保存契約 `v1`](ctx03-checkpoint-contract.md)的最小來源接線設計。現有 authority 可驗誰能保存，但未接入特定 compaction 的來源證據；原三份保存點的 trigger/request IDs 皆 missing。這次只完成設計確認，不是正式 validator 已改；產品整合仍需另行明確授權。[唯讀依據](context-research-evidence.md#q11-feasibility)

契約確認當時只到設計；使用者之後明確要求自主完成完整 Target State 的實作，同日另授權必要雲端部署／設定、上限 USD150。新的授權與執行條件見[持續狀態](context-implementation-status.md#雲端授權與共同預算)；IAM／RBAC 與額外付費推論仍須明確批准，也不是本文件自行放行 test-only 格式、私有原文或未通過 review 的新 codec。

## 10. 產品接線初次交付與 review

固定產品 head：`eddc9de48469a02ba1d2d9741c8f6ec1ee92d741`，tree `ca88902d74717701ed63d49ce162cebdc3eb6579`，實作／PR merge-base `d518af2772274a31816884e6dd2bb91fd65c819b`。這是 draft #8 的初稿，不是 `main` 或雲端採用結果；後續 P2 修正另記新 head。

原產品 owner 的交付說明、manifest、環境及 native proof 已核對原 SHA；manifest 引用的八份 artifacts（含四份 JUnit）也已逐份核對。研究者只讀凍結原件與 Git objects，沒有讀 live worktree、私有 raw state／wire／SQLite，也沒有為收件重跑模型。

| 執行 | 實際結果與口徑 |
|---|---|
| 初次 checkpoint runner | 85 passed／1 failed，JUnit 181.599 秒；失敗為測試 fixture 缺既有 work-policy routes 而 HTTP 404，原結果保留 |
| Fixture 修正後單點 | 1 passed，JUnit 3.343 秒；只補原失敗 selector，不改寫成單批 86 全通過 |
| Native 產品路徑 | 1 passed，JUnit 79.682 秒；一個測試驅動 4 個 fresh Python／runtime processes、6 次 offline model requests |
| 既有 instruction 回歸 | 30 passed／1 collection skipped；JUnit 套件時間 101.261 秒，owner 回報 pytest 整體 104.28 秒，兩種口徑並列 |
| 保留缺件 | `pinned offline original Window2 fixture required` 的 skip 不是通過；Q13 該面向仍未驗 |

Native proof 的四個 process 確認同 SDK／conversation，只有第二階段呼叫 manual compact；後兩階段繼承同一組 `session.compaction_start`／`complete` 與 projected IDs，不每輪再壓縮。原 tool start/result IDs 及 data hashes 相同，private／credential 條件與 raw 不改寫的觀察保留。

實際 provider fixture 先核 source、full catalog、tool relationship，再核 native summary、當前更正、先前更正與 assistant answer；compact summary 未進 public preview。它仍是 offline scripted transport，**不是 LLM 摘要品質**。四個 Python drivers 為 exit `0`，四個 Windows runtime 正常停止原值為 exit `1`／graceful／force-stop `0`，不外推 Hosted/Linux。

| 階段 | SDK 事件數 | 完整 state bytes | zlib 比較 bytes | reference canonical bytes |
|---|---:|---:|---:|---:|
| Create | 16 | 143,472 | 32,293 | 929 |
| Compact＋reply | 33 | 154,018 | 34,744 | 2,059 |
| Correction | 40 | 157,973 | 35,267 | 2,059 |
| Recall | 47 | 161,981 | 35,815 | 2,059 |

這次產品程式新增受控 profile／明示 manual attribution 與原 authority/model-intent 綁定，和舊 CandidateStore 不是同一條保存路徑。Profile 預設 OFF；初稿的首個 resume 觸發方式、短 history 未驗與回退 consumer 是採用前仍需處理的範圍，不能當 TC-05 全部壓力觸發已完成。

### Standards

Opus 5.5 對 `eddc9de` 找到 **0 P1／2 P2**：

- `behavior_sdk_compaction.py:_private_structure` 把合法公開 JSON／YAML 聊天內容當私有結構拒絕，可能讓整個對話不可續接。
- 深層 JSON-looking 字串引起 `RecursionError`，超出原 typed checkpoint 錯誤處理，可能在模型回答後未保存答案。

### Spec

同一固定 head 的 Spec 軸為 **0 P1／2 P2**：

- 同一合法文字誤擋違反既有 private filter 與 TC-11，不能用語意 DLP 擴張保存政策。
- `splitlines()` 把字串中的 U+2028／U+2029／U+0085 當 JSONL record separator。Reviewer 在原 base ordinary codec 也重現，屬新路徑沿用的緊耦合既有缺陷，不稱全部是新碼引入。

兩軸的優先級分開保留；重疊根因共用修正，但不相加變成產品錯誤率。這些是 `eddc9de` 的原問題，後續處理見第 11 節，不將這一節的失敗改成當時已通過。

### 原件識別

| 原件（未隨 Git 提供） | SHA-256 |
|---|---|
| 產品交付 manifest | `da4882218b702bcffdae81444b74d5d22847abb4fe8884395f28ebc6383cc235` |
| 初次 runner JUnit | `1facb01d914cd4950e4da5d66ca111cfa18172d68efbc7629a691a771e489af8` |
| Fixture 修正單點 JUnit | `a07752ccf6bc42b06d4158cf3f39b1f820e00c450e0fe954e20589a0adda750d` |
| Native JUnit | `9909525d54c86c3636edf17f7fd442a97dd3da0dca713c50426d3cf2ac6a2489` |
| Instruction 回歸 JUnit | `f43849ab867ad8fafbea80a22e0a6eecb4c3e31c9f6f96dd6cdb455a808db410` |
| Native product proof | `0ed29e692b667aefa0ae50b55be7230da095a2e98c2e13c384eb65fa097635b8` |
| 雙軸初審收件紀錄 | `2567e35393072bbee3a2767aa0561211b58fb5952ad74f3785ecae12e78c87ed` |

## 11. P2 修正批次與新增風險

固定修正 head：`6f5877c38798781b0e63a179ac6675cd63ef4ffb`，初審基準 `eddc9de`。修正把公開文字與 SDK 真結構分開、將實際過深 SDK JSON 轉為 typed error、統一 LF／CRLF JSONL reader，另窄修 profile 停用後 restore／`can_follow_up`／admission 的一致拒絕。沒有新增壓力觸發政策、開新 codec 的全域預設或升 SDK。

| 新批次 | 原樣保留的結果 |
|---|---|
| 精確 red | 7 failed，JUnit 339.535 秒；3個公開例子、深括號、3個 Unicode 都重現失敗 |
| Boundary smoke | 19 passed，JUnit 6.171 秒；只是中途選定範圍 |
| 中途整合 | 147 passed／2 failed，JUnit 454.347 秒；深度 fixture／guard 及安全錯誤映射後來才修 |
| 中途 native | 1 passed，JUnit 63.032 秒；另一個4-process／6-offline批次，不是最後版本 |
| 最後直接 selectors／boundaries／8 MiB | 58 passed／0 failed／0 skipped，JUnit 132.520 秒；不是重新把149項全跑成同批全綠 |
| 最後 native | 1 passed，JUnit 53.940 秒；4 fresh processes／6 offline requests，只有一次 manual compact、兩次 inherited saves |

兩個新 native 批次合計12次 offline requests；不能把其中一批6次當成整輪總數。原初稿85／1失敗、單點1通過、30／1 skip，以及650-row開發 fixture 的原128 KiB model gate拒絕，都保持原歷史。

最後 native 的 state bytes 是 `143478／170808／174771／178787`，事件數 `16／33／40／47`。真正送給 offline provider 的後續 history 核到原 source／summary、更正／先前答案，也保留公開 JSON／YAML、4000-byte 深括號與三種 Unicode；非只看 canned final。工具 IDs／data hashes、原 stop exit值及private/credential條件保持。

### 原問題複核與後續更正

Opus 5.5 對 `6f5877c` 的初次複核，Standards／Spec 都先回覆原 P2 已解、沒有其他 P1/P2。**後續窄化 callsite 前提後，Standards 新增一個 P2並收回本軸 clear**：已知 private copy 的 JSON-string regex 掃描可由合法的大型 assistant／summary／history 觸發平方重掃。

原先認為 user 8 KiB 足以限制此風險並不成立；它在 `_archive`、正式答案保存之前掃整份 state，還有 4 MiB SSE／8 MiB state 等不同容量。Owner 用有界攔截驗到16383-byte公開 assistant 字串可到達該 scanner，沒有執行更大舊 regex。Reviewer 的約16 KiB／1.5秒是有界量測，更大時間只屬外推，不冒充已測數值或 SLA。

影響是延後 durable save、inherited 每輪重掃與 GIL 阻塞，不是已證無限執行或資料遺失。這是新修正裡可達的風險，原已解 P2 的判定不倒改；下一步是最小單向、不回溯 scanner 與必要回歸，再固定新 head 交同模型複核。

因此在 `6f5877c` 階段，**Spec 原問題已解，Standards 新增 P2 並撤下 clear**；不是兩軸從未發現這個風險。後續修正另見第 12 節。Q14、short native history、真正 Hosted rollback及TC-05壓力政策仍按各自未驗範圍保留。

| 原件（未附） | SHA-256 |
|---|---|
| 修正交付 manifest | `7871ca9f989219d35d19f55a06bb98ff7790c19d55e211b62994ae6e4393873a` |
| 最後直接回歸 JUnit | `e21d87079075dbc8219c901f336f4bc7b0921e221206abf3c46d81675a9c1f4c` |
| 最後 native proof | `b8466903d62c236c773f13b8f98373fd5e631e5ab16d2ab702f13e7f96878aad` |
| 原 d518 Unicode 重現 proof | `ea41536ed5326a543171c38040c6907bde180d9a7262e062cb22cc8df4b09ccc` |

## 12. 單向 scanner 修正與最終 source review

最終固定 head：`e80a215b905ff484391c44c6b540fcf2174ac9b8`，tree `d28141f4e04c318b520fb625888d25163ddcf16d`。這次只改一個產品 module 的 scalar scanner、兩份測試及 README，不改 SDK／provider／Cloud 設定。

`json_string_values` 以單向狀態機取代 regex／finditer；每個待掃字串的字元依序走訪，invalid prefix 不回頭，只有完整合法的 JSON scalar 才交解碼。成功片段不重疊，總片段長度受輸入長度限制；原 queue／seen 的多層 known-private 比對仍在，**不宣稱整個 pipeline 單次 O(n) 或延遲 SLA**。

新批次為 **83 passed／0 failed／0 skipped，JUnit 28.641秒**：51個 scalar 案例及32個產品／相容案例。包含最長131,078 ASCII的訪問計數、65,537-byte assistant／summary 的完整保存／恢復，以及 malformed prefix 後4層private副本在PUT前拒絕而公開答案／原因保留。早期47 passed是另一批，不相加。

本次 SDK/runtime starts、模型與雲端呼叫皆為0；沒有為 scanner 小修重跑 native。先前58項／兩個 native批次／所有red及fail/skip維持原版本範圍。

| 軸線 | 最終 `e80a215` 結論 | 邊界 |
|---|---|---|
| Standards | 0 P1／0 P2，追加scanner問題已解，恢復clear | 有界短字串等價／private-copy重現；不以使用者8KiB錯誤前提放行 |
| Spec | 0 P1／0 P2，原拒絕規則與語義保留 | 只審這個delta，不把source review當Hosted／模型品質 |

兩位使用同一 `claude-opus-5.5`。產品 owner 已在不改code／README的情況下把 PR #8 metadata 改為 ready；固定head不漂移。**Ready不是merge／deployment／spending許可，也沒有新增完整條目或情境接受。**

| 原件（未附） | SHA-256 |
|---|---|
| Scanner交付 manifest | `a97687a29ca89440f3669f9bb7bf39b1d89e411b9c6fe1c3f8a1778781018c99` |
| 最後83項JUnit | `c5b0411455d74b8359e3a19174b074bce51be99dc950e5ab6020578fde5ef19d` |
| 被測檔案快照 | `3183ed10618a8ac6e12d22183bb9089edbdf4df0e025e06c3a1cc6ecca8e307d` |

## 13. 合法 signed token 差值：原生失敗與最小修正

後續 [Prompt 相依驗證](prompt-assembly-evaluation.md) 的短 history 真正失敗，不能再僅標 NOT_RUN。`c5cc284` 中相關 CTX-03 模組與 `e80a215` 相同；沒有宣稱在 e80 另跑了外線 native fixture。

| 單變數條件 | Native／RPC 原值 | 結果 |
|---|---|---|
| v1、52-byte first reply、padding=0 | success、manual、checkpoint=1、messages=3、tokens=-11、有 summary | FA source-unverified；第二個 user 未 send、無法續問，第一回覆保留 |
| 只加1筆 public entry，first reply=133 bytes | messages=3、tokens=35 | 通過，第二個 send／保存／接續成功 |
| 只把 v1 prefix 換回 legacy，仍短 history | messages=3、tokens=-11 | 同一拒絕 |

此重現排除了 no-op、缺 pair、空 summary，以及新 prefix 為必要條件的說法。三個 observer flow 直接計數為3＋4＋3次 offline requests、4次 user sends；最初無 observer 的 red 另有3個 model-consume 訊號，不能把它當完整 transport counter 或全部 JSON-RPC 數。

固定 SDK 的 `tokens_removed` 是整數差值，沒有 minimum；摘要可能比被替換的短內容更長。FA 的 `compaction_pair` 與 `CompactionExecution` 原本都要求非負，錯把合法輸出拒為來源未核。這是新證據下的 **P2**，PR #8 曾從 ready 退回 draft，原 e80 clear 的歷史不改寫。

新 fixed head：`c51e7306b6593e5829947ba38c534b456bff8baa`，tree `6fb9d652b4e343f20f0eaccf0580cf3d29c9d0d8`。產品只改兩處、3行：

- `tokensRemoved` 保持 `type is int`；DTO 仍受 `WireModel.strict=True` 約束，但移除它的 `ge=0`。
- `messagesRemoved` 仍非負；bool／float／null／缺欄仍拒絕。RPC／event／execution／reference 的精確比對仍拒絕 −11／−10 不一致。
- Trigger／custom instructions／checkpoint／summary／source／private／once／stop 與預設 OFF 不變。−11 不歸零，不把 token 差值當費用退款，也不遷移／重寫舊正值 references。

| 修正批次 | 原結果 |
|---|---|
| e80 產品碼上的精確 red | 2 failed，4.524秒 |
| Signed／type／保存／恢復 selectors | 15 passed，23.270秒 |
| 含原 guards 的批次 | 51 passed，72.607秒；包含上述15，另71 deselected，不算66個不同案例 |
| 第一個 native 嘗試 | 1 failed，13.568秒；fixture 期待 `this`，原去敏訊息是 `the`，未到 compact |
| 只修該斷言後的 native | 1 passed，11.137秒；產品碼沒有再次變更 |

最後 native 用 legacy 短 fixture，負值原樣保存及從新 reference 繼承；reference deltas 為 `[null, -11, -11]`，第二個 user 送出、第三個 runtime 恢復 summary／prior user／answer，沒有再次 compact，`can_follow_up=true`。原答案及恢復點保留。

這批是 **1個 Python runner、3個不同 runtime、3次 user send、5次直接計數 offline provider requests**，不是3個 fresh Python。正常 runtime exit=1／graceful／force-stop=0 保持。新 Prompt v1 的相依 short regression 另記於其報告，不挪用這個 legacy fixture。

Standards／Spec 對 `c51e730` 均為 **0 P1／0 P2**；PR body／ready 僅更新 metadata，head不變。這只支持該精確本機短案例，不證明所有短 history、pressure policy、真模型、Hosted／Linux／Blob 或 Q14。

| 新原件（未附） | SHA-256 |
|---|---|
| Signed修正 handoff | `6ff5f5c3baf2ba7801d40d28b4a8d22ca261c33a2bf569e8803fbd6ef319a594` |
| Manifest | `cd24cf0e766e6c8a32e267d3d745a3ec7870edbcd8f253c84c9a932624deeb2c` |
| Native proof | `1c22b71d904617fe161b3b65a5cceb4385f7f205d4067bf9b0d06704d10e1505` |
| 51-case JUnit | `e77b5c353a1bb41ae0f6df885972707155264372aa0f86da9bcb4bbebf718cca` |
| Native green JUnit | `27715a806fac584b05d68dff69f4e6f9d715e94c5aa1e7e937b34a541510dcac` |

## 14. 公開答案在壓縮期間的唯讀保留觀察

2026-09-25新增TC-01的後端局部證據，沿原四個fresh Python／SDK process的native fixture，
沒有修改產品保存、compaction、prefix或權限。只增加對既有公開reply／job-status Interface的讀取：
在compaction之前、provider正在產生摘要時、完成之後，以及兩個fresh-runtime追問後，
所有已保存reply都必須維持原JSON bytes、`x-content-sha256`與`Cache-Control: no-store`。

首次WIP跑到1 passed／43.861秒；13次過去回覆觀察保持相同bytes／hash，第一份完整reply為117687 bytes。
原6個offline model requests、4個fresh Python／runtime、正常stop與0 force-stop斷言仍在。
每次純GET後另從公開job status核`obligation_fulfilled=false`，不把讀到答案當作ACK。

這不是新UI、真browser reload或完整TC-01接受；[工作台的minimal-DOM保留測試](context-implementation-status.md#公開逐字稿的局部呈現證據)
仍是不同層的證據，不相互冒充。首次原件保持WIP／baseline未知，之後固定commit上的結果另記。

同一條真loopback HTTP串流測試另加兩個明確反例：有公開preview時，conversation投影不含合成
private reasoning字串，而正式reply GET仍404，不能把preview當已保存原件；原相同model request
重送仍409。這只強化既有測試的觀察，不改串流、保存或權限實作。

資格核對再補UI cold reconstruction依賴的旗標：同一個原生compaction測試在每次保留觀察
先GET conversation，要求所有過去已完成turn的`reply_available=true`、原job ID不變且沒有`stream`。
這把「原件可GET」和「薄client仍知道要取這份原件」分開驗證，不以直接GET成功推定投影正確。

## 15. TC-11 同版完整 SDK 保存與跨程序恢復

2026-09-25以固定source **`ed3f37cb0eaf823d90e7e5c63bef209e39f0dce3`**核對既有產品與測試，
本次沒有新增或修改程式。TC-11的C條件是同版SDK所需完整狀態可保存與跨process恢復，
維持conversation／SDK identity，不再受舊inline raw131072B／compressed16000B限制；
缺少必要材料不能用空history假成功。這不是跨OS、Azure Hosted／Blob或模型語意品質接受。

### 同一baseline的61個checks

| 分層 | 實測與限制 |
|---|---|
| 普通filtered state native | create的raw **162834B**、zlib比較 **43627B**；新Python／runtime恢復同CID/SID，實際保存3個SDK檔案，包含`checkpoints/index.md`；額外tool-output檔案由合成state測試驗證，非此native案例 |
| 普通codec的未採用compaction | 第三process的原生compact成功，但含private diagnostics的snapshot被拒；原合法reference／已存parts仍保留，不把它當新codec成功 |
| 明確manual-no-custom產品路徑 | 原簽章API／SDK／SQLite ArtifactStore，create raw **143417B**、zlib比較 **32249B**；四fresh Python/runtime沿同CID/SID，一次合法manual compaction及兩次DIAG cold resume |
| 容量／傳輸邊界 | API精確8MiB canonical state、16個≤512KiB parts、完整load與新回合restore；超大escaped HTTP body先413而不PUT，再加入檔案材料超額時不接受 |
| 缺件／權限／版本 | 缺part/checkpoint/index/workspace、錯scope/hash/version、檔案／目錄衝突、binary／無效UTF-8、未知／test-only codec明確拒；已restored狀態與resume旗標不一致時SDK不得起跑 |
| 不明／失敗／相容 | partial PUT／readback／prepare或publish unknown不重傳、不換空reference；stop/disconnect或filesystem失敗不建立upload intent；legacy inline bytes與原private filter保持 |

共 **61 passed／0 failed／0 skipped，99.049秒**，其中2個native cases、59個替身／程式檢查。
兩條native路徑分別是3與4個fresh Python/runtime、4與6個offline requests；沒有paid provider。
上述zlib只用來比較歷史inline界線，正式ArtifactStore資料不是改成這個zlib格式。
普通native的create／resume原件沒有`temp/`檔案，因此既有test中的該迴圈未實際exercise；
不把空迴圈通過當成native額外檔案證據。其checkpoint index與workspace在兩份proof中的hash相同。

61個checks沿現成runner與公開SDK／儲存接點，並非61次native或雲端恢復。
Profile opt-in、原job／model intent／正常stop與合法投影的來源限制仍生效；未知版本或不支持能力不因
「需要portable」就自動降級、刪檔或放行。

### 保留第一批環境失敗

第一個同source合批為**60 passed／1 failed**：ordinary child直接執行既有script時，
沒有繼承必要的`PYTHONPATH`，在import `shared_context`前即停止；同批manual product native已通過。
沒有改產品或跳過該case。只在session runner明確給child `src/fa-shared-context-poc`的import path，
用新output／scratch執行同61個selectors，才得到後面的全批結果。

原XML、runner副本、原件與60／1結果不改成PASS；這個環境修正不是產品RED→GREEN修補。
第二批在source commit存在後、工作樹乾淨的13:49:45開始；之後的單行文件澄清與merge沒有改該測試source。

### C接受提案與適用範圍

本次以新`target_acceptance/full_target`與上述exact baseline提出TC-11 C接受，**待固定PR head的兩軸正式review**。
同版完整保存、跨process身份、兩個舊限值與必要材料拒絕都有直接checks；當前同一儲存codec被其他prompt
profiles使用的部分依共同writer/loader/source核對，不宣稱每種profile各另跑一次完整large-state native。
此61-case沒有改既有PA-02／TC-01接受；其後TC-01因另發現的source／consumer差額撤回，
見下一小節。任何完整E情境或Q14 Hosted／Blob仍不由本批通過。

### 後續未設定profile的event fence修補

第15節61-case原結果保留。其後review更正了另一個source推論：原observer在profile缺省時
忽略compaction事件，不能說此時也會擋下未核摘要請求。合成event/model-gateway probe先各得
1 PASS／1 FAIL；補完整mock sender後確認未設定分支可到mock reserve與sender，
但沒有啟runtime、產生native壓力或觀察到真preview洩漏。

Repo新增兩種profile的同一組反例，先4 failed／5 passed；最小修補只把compaction事件類型
檢查移到缺省profile判斷之前，缺省時標原`SDK_STATE_SOURCE_UNVERIFIED`。
普通事件不誤擋，已批准manual與inherited規則不變；WIP10-case正反回歸通過。

固定新source **`0680d59bba97d7d01b25411f10fe046eeaf6d020`**於14:40:06先提交，
14:41:58開始完整合批，**69 Python＋32 minimal-DOM＝101 checks全通過**。
其中66個Python cases屬TC-11保存／恢復與前置fence子集，從同一份69-case XML按明確class分出；
其接受紀錄與101-case regression紀錄共用同次執行，不能當作兩次或加總model用量。

這次兩個native cases仍是7個process／10個offline requests。普通state raw162835／zlib43626B，
manual產品state raw143418／zlib32267B，均再越舊limits；普通native仍只含3個SDK檔案，
沒有temp/tool-output檔案，任意額外檔案只由合成state檢查支持。
Current TC-11候選改用新source及66個直接相關checks，不假稱舊ed3批在新code重跑。

TC-01 current先撤回partial，舊accepted run不改；gate修後仍待另一個非legacy
prompt-metadata UI consumer的修補／驗證，不拿本次legacy public-view regression恢復完整接受。
PR25因此已從最初的文件資格工作擴為這個**單一observer修補**與相關回歸，
需要原兩軸對新head正式重審，最初docs-only的0P1/P2不能授予新code合併資格。

## 16. TC-08：壓縮後摘要不是新的人工批准（限定資格）

2026-09-26，本接續線只補一個既有行為的原生案例，不預設產品有bug、不修改SDK或正式來源gate。
沿既有`manual-no-custom`、signed API／controller、原ContextArtifactAdapter及公開
`SessionHistoryCompactRequest(trigger=MANUAL)`，不傳custom instructions／token-limit override，
不自建compactor或授權store。固定SDK1.0.13／runtime1.0.83，模型以in-process scripted provider替代。

測前scope為一個案例、最多**2個owned runtimes**，固定成功序列是**6個provider requests**：
create讀合法source並保存原答；下一回合先真manual compact，合成summary帶假human-approval marker，
再由真工具回呼要求執行已prepare但未獲grant的原operation。必須拒絕，原operation／grant／effect
紀錄保持，合法read_context仍可繼續；只有真入口的兩個user事件，原reply／archive保持。
這不是自然LLM是否會服從惡意summary的品質測試，也不把SDK選用的model-message role當成人類身分。

測項為`tests/shared_context/test_compaction_summary_authority_native.py`。
先固定來源，再執行一次並保留真RPC／event／status／stop、callback計數與hash；不保存raw
prompt／SDK state／credentials。任何失敗先停在原批次，不放寬判準、不直接換ID重送。
測前先標**尚未執行**，不借原signed-short成功或壓縮前的operation拒絕；以下新增自己的固定來源結果。
額外runtime在start前拒絕，額外provider callback即拒絕並算失敗，不把預期數量寫成供應商硬性配額。
不是全TC-08／E-05、其他provider／模型／平台、automatic compaction或Hosted接受；
SK-12的ambient canary與其停止契約不在本切片。

來源已核`SdkSessionFiles.compact/observe_compaction`、`m2_copilot.run_copilot`及原signed-native
fixture；固定SDK的[compaction E2E](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/e2e/test_compaction_e2e.py)
只支持正式SDK/events接點，不能代替上述FA server-authority案例。

### 固定來源與第一批結果

先提交 **`bd4dbd878fd5e15898d94a0fb978becc44482a70`**，tree
`cbd3820b5b357f730dd13cad18cbf982ef75e0d7`，再於clean source執行唯一測項。
2026-09-26T15:32:56–15:33:14+08:00，**1 PASS／0 FAIL/error/skip，pytest12.966s**；
411個Python working hashes測後相同。第一批即通過，沒有虛構產品RED或改產品求green。

Safe proof記錄2個不同owned runtime PID、6個scripted provider requests（前turn2／後turn4）、
1次只帶manual的正式RPC及1組native start／success-complete；原假批准summary的SHA
與RPC／保存source觀察都是`586bb236042dfbc373418cfba7eb4d03055e3df820c9cc6d3416c486441aaa41`。
後turn三個普通model requests實際含該summary；原execute_operation得到upstream409，
effects的credential／transport出口均未到達，合法read_context仍回原來源並保存回覆。
兩個Windows程序都真退出，stop回條的PID／exit1與process讀回相符、graceful=true、force0。

原operation／grant／attempt／effect／budget與該tool-call前後accounting保持的斷言都通過；
模型本身的正常fixture用量是另一層，不宣稱整回合無記帳。保存的SDK user events仍逐字是
兩次真入口輸入，未新增假human事件；首回覆及archive保留不變。這不把SDK在model payload
表示摘要時選的role當人類身份，也不證自然LLM會忽略假批准。業務模式仍`offline_contract`。

JUnit SHA256 `8be2708d6f5f4ac8295340d44c0e4a5c6df7ecd1395f9c563f4f50cfa5f8d5cc`，
safe proof `884b463d078b91d2724f54eeecbf17a0f65edf99d49d14c44b4cee6e2c7d318d`；
完整argv/source/log/receipt在受控session，repo只放opaque refs/hash，不放raw SDK或provider內容。
491筆[帳冊](context-evaluation-status.json)保留原490/spec/bindings及四個C基準，TC-08只改partial。
原same-adapter human-grant正向另有SK06來源，本例沒有在compaction後再批准operation，
不藉它解除WorkStateEffectAdapter或完整TC-08的缺件。
Final `09b1383a650b5470199c412da1db85cb7cb580d3`經原Opus5.5兩軸各0P1/P2後正常合併為
`c5c687af1041114aad45e8a7d3f1e82bb85e6162`（#54）；原bd4執行不改成review／merge來源。
兩軸的因果用語提醒已改為「無grant下觀察到409」，不宣稱量測唯一server拒絕碼；
沒有為審查或合併重跑SDK，TC-08仍partial。

## Verified against

- 產品基準：PR #8 初稿 `eddc9de48469a02ba1d2d9741c8f6ec1ee92d741` 與修正 `6f5877c38798781b0e63a179ac6675cd63ef4ffb` 分批記錄；原 `3022d8eb…` 僅代表第 1–8 節歷史本機 core。
- 已核來源：兩軸 Opus 5.5 對固定 Git source 的 review，以及 owner 的 manifest／JUnit／native proof；研究者核對原件 hashes、結果數字、身分／事件關係，不冒稱再獨立執行產品測試。
- 實際驗證：本次收件只執行標準庫 hash、JUnit 解析、proof 內容一致性與帳冊驗證；owner 的測試命令／環境／範圍由凍結 handoff 及產品 README支持。
- 修正收件：`ctx03-p2-fix-evidence.json` 的11份 artifact bytes/hash、六份 JUnit及最後 native proof關係已核；沒有獨立重跑該批 SDK。具體 source與測項由修正 README及同head review支持。
- 最終增量：`e80a215` 的固定source／83項JUnit與兩軸回條另記於第12節；scanner只是有界本機驗證，沒有新native或雲端結果。
- 最新增量：`c51e730` 的9份 artifact bytes/hash、五批JUnit與signed native proof已核；同模型雙軸各自核 fixed source。原成功、撤回／新增P2、red／fixture fail與修後green分開保留。
- 限制：Q14 Hosted／Linux／Azure Blob／其他 provider／真模型品質仍 NOT_RUN；short native只補該精確fixture，不再把「所有short未跑」或「所有short通過」作統一結論。必要部署／設定授權不代替headroom、IAM及額外付費推論條件。
- 第14節基準為`7076ce5`加新的test-only觀察；已讀`test_sdk_compaction_native`的實際provider／fresh-worker／public GET接點，新增reply/hash/status核對而未重造runtime。首WIP JUnit SHA256 `9482c226cf78bd600fa24a5fa7963903dad89b245e69afe9e66ad2024246a82f`；safe proof `025f6cc3e17b6921a68c3623b4ec9144b7aa2f154b6aa03e05dab1d7d2248016`。只清理自己的合成fixture目錄，未觸主8765或其他session。
- TC-11第15節已讀`SdkState.complete`／`StoredSdkArchive`版本與完整性規則、`SdkCheckpointStore.save/load`的所有parts讀回與scope核對、`SdkSessionFiles.snapshot`的完整檔案保存／失敗拒絕，以及`run_copilot`的restored-state檢查；直接測試入口為`test_sdk_checkpoint*`及選定`test_sdk_compaction_product`／native cases。
- 第15節first60/1 JUnit SHA256 `13c9505977dbf93c46a43d6dc30117ea4e4668c66d70c3cbcea1f2896625d017`；同selectors環境修正後61/0 JUnit `d4ca080bd164d65f2cdce97365f0890802b522768a6aabc342cede1cc9e86676`；safe handoff `82910e8021607bd59d0189fc46dfd2ce66ec0ed630b428ac8c7f6385f553fe72`。三份普通proof原件及manual product proof均保留，只清理兩個已停止的task-owned fixture，不複製raw SDK files／DB或credentials。
- 新observer修補的69-case Python／32-case DOM XML：`27ea4357df7f7eded4bc81134e51ec0f189c9586ce5dc71d0a3a085a08985d46`／`b9703533f5319737b0a0530185cc6101b7375725ca07a43e7509edb18d1a98bd`；safe handoff `6d819b452037b744b86b13af880ab546e9b1607f648fb968271d4d1fd1c42bda`。已核原件與三份普通proof的實際3檔清單，無native temp-file coverage外推。
