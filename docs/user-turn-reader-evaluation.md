# 本對話已保存原話：受控reader與驗證界線

日期：2026-09-25。#20已合併，**server-owned、default-OFF、未部署**。這是TC-03／TC-06／TC-07的局部原件接點，不是完整逐字稿搜尋、長期Memory、全模型eviction或M類語意驗收。

## 實際Interface與來源

新profile `fa-instruction-user-turns-v1`只增加`read_user_turn(turn_number)`。模型不能提供CID、human／agent／Team／tenant、role、query、path／URL、hash或safe marker；範圍沿當前verified principal、ticket與assignment取得，turn必須是本conversation已完成的過去回合。

原話從已接受的`WorkRegistration/InstructionTask`讀取，不從summary或重新生成的答案還原。回傳前核對：

- 原submit bytes／hash、registration material及create receipt；
- 過去turn的合法saved SDK archive／stopped session、terminal且已forwarded的job；
- 原result proof、保存時的stored-row byte receipts及當前read ACL；
- metadata commit成功後再次讀回的一致來源，而不是client自稱safe。

`fa-user-turn-read-v1`保留空白、CRLF與Unicode；`fa-user-turn-source-v1`明列user role、request/job/turn、SHA256、UTF-8 bytes及submit／registration／archive hashes。歷史system／approval文字仍是data，不能取得當前操作許可。原`read_context`仍是reviewed task source，不改成聊天歷史。

來源metadata有界地存於原session與TurnArchive，正文不另複製；read前後、cold restore、model reserve／consume／最後provider egress都重核當前read ACL與lineage。首次metadata commit結果unknown不回正文、不自動重送；已有來源的重複合法讀取是唯讀。

## 明確容量與拒絕

| 項目 | 現行界線 |
|---|---|
| 原輸入 | quoted JSON UTF-8原8192B上限 |
| 完整reader response | 16384B，含metadata，不截字當成功 |
| 隨SDK context保留的不同來源 | 最多7個；包含immutable receipts後仍需符合原98-key純讀界線 |
| 到達來源上限 | 回正文前`USER_TURN_SOURCE_LIMIT`，不丟舊refs、不說原件不存在 |
| 缺失／過期／不一致 | current/future turn、未完成或未forwarded、缺archive/receipt、scope/codec/hash漂移明確拒絕 |
| 無本輪SDK archive | 保留no-SDK失敗及explicit reset，不藉reader偷換session或跳過原話 |

原8-source實驗真的撞到純讀容量，沒有放大store gate。沒有新增assistant-body分頁、任意role／檔案／URL、跨conversation讀取或無界history載入。

## 精確版本與分批結果

Base `cb7a2d8b6868e06d9a941a6bdeb77ba399252159`。Source/tests先於08:18:22提交為 **`f52654c232f772000b7be3e5d8245bf2e1fb856b`**，08:19:11在乾淨該commit執行最後兩批。Final `57ab942e0cb084e2ffe056bdff69309550390e24`只增加README結果；369個Python working-SHA256及兩tree的Git blob逐檔一致，不冒稱在docs head重跑。

| 批次 | 原結果／意義 |
|---|---|
| 首次route | 1 RED／404，不是要舊profile自動增權 |
| Receipt與typed decoding | 兩批各2 passed／1 failed，再3 passed；沿原serializer核stored bytes，不用semantic digest替代 |
| 三次更強eviction假設 | 各1 failed；summary省略不等於model payload已移除 |
| 中間API／native | API44 passed／1失敗碼分類問題；native wiring1 passed／5 requests，各自原件保留 |
| 容量與operation對照 | 8-source碰98-key；operation原本就是409而非新test期待403，對照前後不變，未鬆既有gate |
| Precommit矩陣 | 123 passed，153.901秒；baseline仍null |
| **Exact f526 API** | **123 passed／0 failed／0 errors／0 skipped**，JUnit240.699秒 |
| **Exact f526 native** | **1 passed／6 offline requests**，18.42秒；5 normal＋1 SDK summary、4個fresh runtimes |

其他中間batch與兩個僅留session tool output、未產生JUnit的定位probe均在凍結handoff分別保留。較早WIP baseline與未保存的failed-native provider counts維持null，不補零或挪用之後的成功數字。

## Native實證與不能外推的事

第一回合保存含固定identifier的原話，SDK summary刻意省略它；第三個同CID新runtime實際呼叫reader，signed API取原registration／receipt bytes，同一`tool_call_id`的完整原文及來源metadata進入下一個model request。另有新CID／work／time／user／fresh root的隔離與prefix control。

**但原identifier在compaction後與cold runtime的model payload仍retained。** 所以只證明工具與受控原件接線成立，不證明模型已忘記、必須用此工具或使用品質正確。Full eviction是`NOT_PROVEN`；M類語意與完整TC-03／06／07是`NOT_RUN`，沒有刪SDK history來製造漂亮結果。

四個runtime都graceful=true，Windows原exit_code=1，force-stop=0；原user events、archive/result、operation和reader的費用帳本保持。全是固定SDK1.0.13／runtime1.0.83與offline provider，不是Azure／Blob或真人已讀。

## Review、合併與新prefix

固定`57ab942`經原兩個Opus5.5軸，各 **0 P1／0 P2**。98／96讀集常數可共用屬非阻擋維護提醒。#20普通match-head merge成 **`a4453105c699e4c9a222508716b356b69399f791`**，parents為`cb7a2d8`＋`57ab942`、tree `457597e049663231b0394976b65f318ca3e0971f`與preflight一致，沒有admin／force／rebase。

新profile增加第5個prefix來源：2822 UTF-8 bytes，SHA256 `a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372`。因此coordinator先把current PA-02設回partial，**保留原22e四來源接受作歷史，不套給新profile**；另以`2b942a1`的新完整矩陣重新驗收，見[第五個prefix資格](prompt-assembly-evaluation.md#第五個prefix的重新驗收)。

## Verified against

- 原件handoff SHA256：`1fd08180626db2267823cc6ed7d9b074b6a9690b4a82b1c587443d1e31dde216`；batch index `d305b905cdf5befb8eeab176814b9331b36895ed104c55541d88f90c181db15f`；exact source `c969db56f250cbacfa9ac012d830a759404623d7ef6ddb40687ef38296305b96`。
- Final API／native XML：`c67e95dc4a54b3deb096e5c20c438f4fc6c4e330adecfe36a422003c306cc4a7`／`3f20a727ab37075989234de41027bb6d4d411bf69ef8cd8ccc93b0735034fb5f`；safe native proof `4299a91ad3999e16e4423b15337e66b58556fa9bd59081cb1869d866bcf5b51e`。
- 兩軸以fixed blobs核新DTO／prompt、`behavior_user_turn_source`、API／Hosted client／gateway／boundary／progress／restore／save與history consumers；coordinator核15份XML、369個工作檔hash對Git blobs及final README-only delta，未重跑owner的123-case套件。
- 未提交raw SDK state、provider wire、private system／reasoning、journal DB或credentials。No deployment、paid inference、IAM／keys／live data／harness或排程變更；共同USD150可靠headroom仍UNKNOWN。
