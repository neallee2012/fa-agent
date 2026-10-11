# TOOL-06：原話reader的安全固定拒絕提示

日期：2026-09-25。Source/tests先提交：
**`7305070507d357c7a0504fe07f5f6200ffabab05`**，base為`3f7391d1e92c7210ca0bded33f54b9eba9241c13`。
狀態：LOCAL局部實作／真SDK離線驗證；尚未部署、尚非完整TOOL-06或M品質接受。

## 原缺口與最小接線

原話reader的API已能明確拒絕不合法turn、不可用原件、更動或超限，但Hosted把所有Problem
變成泛用ToolFailure，SDK boundary再一律遮罩。原native反例確實收到
`Authority refused the action; do not resend unknown effects or claim completion.`，
而不是可分辨的`USER_TURN_SCOPE_DENIED`及已完成past turn範圍。

本切片只在**SDK entry後的`read_user_turn`工具**處理四個既有code。
沒有改pre-entry／no-SDK terminal分類、SDK entry intent、10s observer、來源readset／policy、
費用、工作ID、once或reset。尤其不把通用`ASSISTANT_SOURCE_CHANGED`變成任何新終局。

| 接點 | 改動與不變量 |
|---|---|
| `AssistantClient._post`／`read_user_turn` | Header白名單不改；另先核原assignment的past-turn範圍。成功回應CID／turn不符是SOURCE_INVALID，不使用raw API body作提示 |
| Hosted `tool.handle` | 僅`read_user_turn`的Problem攜帶`public_code`，不帶message/detail；其他tool仍用原遮罩 |
| `ToolFailure`／SDK callback | 新optional Python-only code，不是wire或tool args欄位；交由既有boundary選固定文字 |
| `AssistantModelBoundary.public_tool_failure` | 必須instruction/text reply、當前reader capability與正確tool name；只翻譯下列四code，其餘返回原opaque refusal |
| SDK結果／公開event | 沿正式failure ToolResult；不改工具迴圈，仍做既有secret redaction／1024界線。公開event不帶raw failure data/code/detail |

| 固定code | 給模型的安全範圍提示 |
|---|---|
| `USER_TURN_SCOPE_DENIED` | 只能讀同conversation中已完成的過去turn number |
| `USER_TURN_NOT_AVAILABLE` | 原件不可用，如實報缺件，不捏造 |
| `USER_TURN_SOURCE_CHANGED` | 原provenance已變，不重用cached text或替代來源 |
| `USER_TURN_SOURCE_LIMIT` | 完整原文／證據超出既有讀取界線，不截短或繞過 |

所有提示仍是拒絕，保留「不宣稱完成、不重送unknown effects」。
`USER_TURN_SOURCE_INVALID`、`ASSISTANT_API_UNKNOWN`、`BEHAVIOR_COMMIT_UNKNOWN`、
任意未知code、偽造訊息字首、無code的ToolFailure及未知Exception仍opaque。
`execute_operation`、`read_skill`、`read_work_memory`、structured profile的原拒絕字串／權限不變。
模型在args自填`public_code`被schema拒絕，不能選分類或擴權。

## 分批結果

| 批次 | 結果 | 真正範圍 |
|---|---|---|
| base＋新測試overlay native RED | 1 failed，15.32s | 真API產生scope拒絕，但下一次model request的tool feedback沒有固定code。後續合法讀取與原兩筆work仍正常 |
| 首次WIP unit／callback | 43 passed，8.00s | 18個新code/遮罩/spoof正反例＋25個既有callback控制；不是真runtime |
| 首次WIP native | 4 passed，36.81s | 新原話reader案＋兩個skill拒絕＋memory reader；離線provider |
| **乾淨`7305070` callback** | **43 passed／0 failed／0 skipped，5.93s** | commit後執行，SDK I/O doubles，不回填WIP批次 |
| **同一乾淨commit native** | **4 passed／0 failed／0 skipped，32.13s** | 真固定SDK/runtime、實際API與scripted provider，無paid模型／Hosted |
| 公開progress增補WIP | 19 passed，16.73s | 明確新增callback／已保存conversation的code不公開斷言，產品仍等同730 |
| 已提交test-only `e0d185d` | 19 passed，14.52s | 相同增補測試在提交後重驗；src/tests乾淨，當時只有evaluation文件WIP |

新native案有兩個不同job、同一段合法對話、四次scripted model requests：
先保存原話，再故意請求turn99取得固定範圍拒絕，然後沿原work讀合法turn1。
只核正確拒絕提示能傳到SDK、原文逐字正確及工作／obligation界線；是provider腳本選下一動作，
**不是LLM自行學會修正**，也沒有新的user intent、操作批准或human-read。

未觸碰現有Memory owner的v2 preSDK收尾工作。產品只改m2 adapter、instruction boundary和
Hosted tool exception接點；記憶／skill handlers、source/read-policy與所有批准／UNKNOWN原件不改。
這不改prefix、公開progress schema、archive codec或TC-01的公開/模型view分工；
仍以既有SDK管理對話，固定提示只是允許的tool failure內容。

## 原件

| 原件 | SHA-256 |
|---|---|
| `user-turn-refusal-feedback-red-20260925.xml` | `530b80f15afa1218360f2a7aea6143ecc62883ef73eb030a13d83fdde5561b8c` |
| `user-turn-refusal-feedback-unit-20260925.xml` | `3232fe366dc0e917b315d1c66f668567fe9368376781993a77c40f79f42ca279` |
| `user-turn-refusal-feedback-native-20260925.xml` | `7b838cf009632604e4b38b57a8c9704acc6353f0bcb958d13ae68988c7c6588a` |
| `user-turn-refusal-fixed-unit-20260925.xml` | `1cdb0f712ffaa0e7ea3b548ec238ba83c3fd34e5e63bb9742c97abef9b8d0431` |
| `user-turn-refusal-fixed-native-20260925.xml` | `447f8b2872d2f9255ce9f0e0eaec140a177926fa13b367e07b2508dc3cfc9b98` |

Native測試檔SHA-256為`d86a0b8be4156a8a36f1bcc3a2edec36f0c7d4ebbf2cb5ee0b31ffd208cb5caa`，
RED與730 fixed批同一測試檔。後加public-progress斷言的test-only版本為
`e0d185d6c540a0305f6d3b7bc27ca16794a5a76b`，新native檔SHA-256
`b4d421ac2b23786fed2781cfc508fe82d7634ba3b79658418586bfc33d2fce04`；
舊批不因此回填。兩份19-case原件如下。Git不發布tokens／原始SDK state或私人對話。

| 增補原件 | SHA-256 |
|---|---|
| `user-turn-refusal-public-projection-20260925.xml` | `f8ac2be7e127d97472a5e18d043ccb422405ca76a07f383899a3fad2d6ca16e3` |
| `user-turn-refusal-public-fixed-20260925.xml` | `ccf421319cddc29a8d312fb0c60d0bdf1ef2a70973a39b80863b2eca95aff0b0` |

## Verified against

### PR35 初審P2與main33整合

初稿`d26c51b`的Standards為0P1/1P2（CURRENT近期完成4項超限），
Spec為0P1/1P2（client成功回應的CID／turn不符，也沿`SCOPE_DENIED`被提示成可修正範圍）。
兩軸原結果保留；此時沒有放行或merge。

收到協調線窗口後，只普通merge exact main33
`ddbcd64e222daa53024e5cfa58eebf9a438e9b38`；pure merge為
`71dd74d1d3088d91d158a26f169e1f8d2f254e1c`，
tree`95c539871ea14b9dad945dc491b2657488fd756e`與先前merge-tree相同。
沒有產品衝突、沒有改其v2前置拒絕或observer/source-readset契約。

完整性P2沿實際API→client→Hosted→SDK重現兩個RED：故意回傳合法shape但其他CID／turn的
`UserTurnRead`，原模型回饋竟是`SCOPE_DENIED`固定範圍提示。最小修法是在client分清兩層：

- 先建既有typed command，再核original assignment的past-turn範圍；不合法數字不發HTTP，
  可用固定`SCOPE_DENIED`提示，這是已知本機範圍，不是response完整性。
- 已成功回應的CID／turn不符，一律`USER_TURN_SOURCE_INVALID`，不在對照表，保持原opaque字串。
  不返回異常原文、不暗示換turn即可修好，也不創新工作／重送原effect。

原生測試分開核本機範圍拒絕、真正API header拒絕、異常CID、異常turn；
後兩者必須捕捉SOURCE_INVALID並回精確opaque。原文／code／detail仍不出public progress。
另外直接在`user_turn_access=True`時測其他工具名稱，確實測到名稱gate，
不再靠capability關閉掩蓋。CURRENT近期完成已回到3項。

修後source/tests先提交為 **`1a80402c02d48f8829413328f8eef0d844347f9f`**；
再在相同乾淨source/tests執行60個callbacks/API/client/v2-conflict檢查及7個真SDK／離線provider案例。
當時只有後續evaluation文字WIP，不把測後docs head當test baseline。

| P2批次 | 結果 | SHA-256 |
|---|---|---|
| `reader-refusal-integrity-p2-red-20260925.xml` | 2 failed，24.62s；pure-merge source＋test overlay | `7bee3f149afbe9cddeecdcbfb21e12bf648a1568aaa9ef5f27c770eed61a48f7` |
| `reader-refusal-p2-green-unit-20260925.xml` | 60 passed，46.41s；WIP | `8e237074f73e2d3fd46ec943c125f53f9ec1c23f4a68fc4c7ddbac5290cf3a6f` |
| `reader-refusal-p2-green-native-20260925.xml` | 7 passed，68.76s；WIP | `1ff671cb01e774a210021762f0f9e282ec442149762b9dcc325f6b383d7032aa` |
| `reader-refusal-p2-fixed-unit-20260925.xml` | 60 passed，36.77s；fixed1a | `eeb52746643ec17e427d84209557104bca5ed3c84d9724d57a059fe386d8b49f` |
| `reader-refusal-p2-fixed-native-20260925.xml` | 7 passed，60.26s；fixed1a | `ed38ab47035038fecd6be41b1c6ec4145cab2161670b19096403b679efe8ee0d` |

60-case含固定提示／舊遮罩、完整性八個client反例、兩個pre-HTTP scope反例，
以及main33的三個generic-conflict護欄；沒有重跑v2大矩陣或TC-01的109。
Native檔新SHA-256為`7793b4e331ddc2fa1ff2932a374c6e17e496fbed4ec6a41bc476cb64a1678852`；
原730／e0d及早期FAIL/PASS不回填。Global337帳冊與既有C baseline不改，
本輪只補獨立runtime TOOL-06局部結果；final head仍須兩軸重新確認。

- Source/tests commit `7305070`，SDK1.0.13／runtime1.0.83，依既有核准feed取得的隔離runtime。
- 已讀正式ToolResult、reader API code header、Hosted exception接點、boundary與既有error masking。
- 實際跑上列RED/WIP/fixed批次；沒有改失敗判準或把後續commit當較早執行基準。
- 未驗真模型是否適當使用提示、H/M品質、Hosted、異常真服務返回或跨平台；完整TOOL-06仍partial。
