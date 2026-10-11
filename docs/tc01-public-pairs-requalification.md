# TC-01：工具配對後的完整程式資格重驗

日期：2026-09-25。受測commit：**`0dac615451024fafd4c87ab275f244df569ea866`**。
狀態：**109個程式checks通過；候選head `e730dd5`已經Opus5.5兩軸各0P1/P2，
兩軸均明確建議接受TC-01 C，帳冊恢復`0dac615`上的C接受。**
後續純資格狀態head`54d3298`也經兩軸各0P1/P2後，已正常合併為
`3f7391d1e92c7210ca0bded33f54b9eba9241c13`；parents為`0dac615`與`54d3298`，
tree`d5ffb9e104dad47507158d09dd0ec5fb1fd29b9d`與已審head相同，沒有admin bypass。
不是新的語意品質、Hosted、Azure Blob、background pressure或human-read接受。

## 固定範圍

TC-01原條款是：SDK管模型對話；UI是允許公開訊息／事件的投影；
模型壓縮不刪已保存公開答案，preview不變成權威history。判定為C。

因公開工具配對新增producer／DTO／UI欄位，前一切片已將當前TC-01撤為partial。
這次不借`cb4b180`舊75-case接受，也不只用新增mapper tests：
先固定scope，再collect並逐筆核對76個Python node IDs，沿用舊完整TC-01的43個Python範圍，
加新配對31個contracts與2個native案例；DOM更新為33個cases。沒有為湊數重跑PA-02的其他unit矩陣。

| 群組 | Python cases | 實際範圍 |
|---|---:|---|
| 所有instruction profiles的native投影 | 16 | Legacy及五種versioned instruction profiles、ordinary／checkpoint／cold restore、catalog變更；每個實際projection進strict bridge DTO |
| Typed API→bridge | 14 | Legacy與全部五種assembly schemas、malformed拒絕、原reply保留、metadata不授權、無Hosted SDK冷匯入 |
| Native公開答案保留 | 1 | 四個新Python／runtime程序、原SDK／conversation延續，compaction及兩次cold followup |
| Preview非權威 | 2 | 真loopback HTTP與stream parser；private reasoning/tool data不進preview，未保存reply仍404，不重送模型 |
| 已設定／未設定compaction護欄 | 9 | 八個合成event/gateway反例及ordinary control；未觀察到自然觸發的native background pressure |
| Frozen兩輪bridge | 1 | 真API/Table/Hosted adapter／bridge配合SDK I/O doubles，原文／cold archive／純讀與原人工ACK分開 |
| 公開工具配對契約 | 31 | Registry、同名交錯、重複／異常、隱私、legacy bytes、8KiB裁切、actual emitter callback doubles、保存／thin-client |
| 新配對／memory native | 2 | 真SDK/tool loop、來源與公開配對；memory正文／raw IDs不進公開progress |
| **Python合計** | **76** | 舊43＋新31＋新2；不是76個native案例 |

另跑同一source的**33個minimal-DOM cases**，涵蓋原saved reply／preview消失／頁面重建，
以及工具關聯呈現。這些不是33段agent traces，也不是每個native案例都開瀏覽器。
本批沒有新的browser執行；前一切片的renderer觀察保持其原版本／範圍。

## 結果與可支持的結論

- Python開始`2026-09-25T20:38:03.9098743+08:00`，結束`20:43:30.1040223+08:00`；
  **76 passed／0 failed／0 skipped，pytest 322.48s**。DOM **33 passed／0 failed／0 skipped**。
  全程HEAD為已合併`0dac615`，沒有產品／測試WIP；後來的文件commit不回填成受測source。
- 16個profile proof均核`actual_projection_accepted_by_strict_bridge_dto`、
  source canaries只作data、prior archive不變、原user bytes與SID／獨立conversation關係。
  共84 normal＋8 SDK summary＝92個offline provider requests、58次正常SDK stop；force-stop為0。
- Native retention：四個不同Python／runtime PID、同原SDK／conversation。
  六次offline requests涵蓋建立、manual compaction與兩個cold followups；
  13次舊reply讀回均`reply_available=true`、`completed_turn_has_stream=false`、未ACK。
  相同turn的bytes／SHA完全相同；第一份已保存公開reply為117687B，壓縮前／中／後不縮水。
- 另有memory native六次及新配對native兩次offline requests，所以可核的native-driven總數為
  **92＋6＋6＋2＝106**。其他HTTP／SDK I/O doubles不當額外native requests。
  這不是使用者turn數、語意樣本數或Azure用量／費用。
- 固定SDK `1.0.13`、runtime `1.0.83`，provider全合成且走離線transport。
  模型對話／checkpoint仍由SDK管理，UI只讀已保存reply／公開progress；來源分工另由現有source核對。

因此本批支持**當前TC-01的C範圍**：允許的公開view、保存與preview分離，在同一完整source上重驗。
它不接受SP-03的M語意、PA-02以外的新prompt目標、TC-09完整並行、Q14／Hosted／真模型、
Memory語意或真人已讀。SDK summary只按原checkpoint flag辨識，不是模型品質gold。

## 雙軸裁決

`claude-opus-5.5` Standards／Spec各自對base`0dac615`、候選head
`e730dd52d760c032e176971235353f4bb1244982`完整核對，皆為0P1/P2並建議接受C。
兩軸均核原43-case範圍未縮、新配對覆蓋、node-index／JUnit、16份prefix proof、
四process／13次舊答案保留、scope先凍結與乾淨commit執行及原333前綴。
這兩筆source review不新增產品執行，也不是human gold。
因此只改TC-01的implementation／acceptance／blocker；baseline仍為實際測過的`0dac615`。
目前PA-02／TC-01／TC-11為3／76接受，scenario0／12，SP-03等M條目不由此推定。

## 原件與重現

預先固定selectors在`tc01-pairs-requalification-scope-20260925.json`，collect後的逐node映射
另存node-index；每個收集node剛好落入一組，沒有靠總數猜覆蓋。
設定`M2_TEST_RUNTIME`到已核固定bundle、`PYTHONPATH`到本worktree source，
`FA_PREFIX_STABILITY_EVIDENCE_DIR`／`CTX03_EVIDENCE_DIR`／`FA_WORK_MEMORY_EVIDENCE_DIR`
各用本session獨立目錄。真SDK、loopback與暫存都沿既有fixture關閉，不碰主工作台或真人對話。

| 原件 | SHA-256 |
|---|---|
| scope JSON | `228b4d1592ea69e7f637e6966f4c162e2dee36c8d8fcd8ecdab860c0fc65dd8e` |
| node-index JSON | `4743638a72cd5839813e99958b402c8e2e9ad41f60e0f14067f27e29b86c2697` |
| Python JUnit | `75d79f94ad12796d3ca1963fbdd956013c66264fb93e56bc30c42b4fe30a53b9` |
| DOM JUnit | `77b655aab260326ee156d7c0381fdf1f2e2e5d62159c589c11c032bad63b3f68` |
| native retention proof | `71557d1b1fa6051540aac53e9616ff1222c9b83603b30f3ff498f7a1e175495b` |
| reviewed-memory native proof | `4b3e00e9311f07e5e2e751ba3300b9afac43f04c16409d49000a3bcfd1fe6b5d` |

16份profile proof及其他原件留session artifacts，Git只記去敏結論與hash；不發布raw SDK state、
私有reasoning或登入材料。所有較早FAIL／partial／接受run保持原樣。

## Verified against

- 精確受測source：`0dac615`；repo正式測試入口及scope的76個node IDs、原HTML的33個DOM案例。
- 已核資料：JUnit逐case、16份profile proof、retention四process／13次reply觀察、memory proof及scope指紋。
- 限制：只有定義中的程式C資格，不將local SDK、scripted provider、minimal DOM或本機儲存
  宣稱成Azure、真人或任意模型的品質結果。發布／合併仍依同一final head的Opus5.5兩軸與正常checks。
