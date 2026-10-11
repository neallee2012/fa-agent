# TC-01：模型逐字稿與公開投影的程式驗證

日期：2026-09-25。Guard／nonlegacy metadata缺口修補後，六prefix的`ed6f64e`接受保持歷史。
#30又新增memory profile與schema，current先回partial；現在以[第七profile的接續資格](#第七profile的接續資格)
重新提出TC-01接受，已隨#32固定`1c3db91`兩軸0P1/P2後合併。沒有native背景壓力或真preview洩漏的新宣告。

**後續程式變更：** [公開工具配對切片](tool-progress-correlation-evaluation.md)新增`tool_start_seq`並修改
producer／typed DTO／UI consumer。本工作樹的新snapshot只具局部離線／browser-renderer／限定SDK證據，
TC-01當時回partial等待新完整資格；上面歷史接受與全部反例不改寫。
Main32的`cb4b180`七profile資格也早於本公開producer／DTO／UI變更，不能直接接受這次新增metadata。

**配對切片合併後的新資格：** 在乾淨已合併`0dac615`重驗原TC-01完整範圍及新配對契約，
76 Python＋33 DOM全通過，詳見[109-check重驗](tc01-public-pairs-requalification.md)。
候選e730已經本次Opus5.5兩軸各0P1/P2且明確建議C接受，帳冊才恢復接受；
純狀態final head仍待確認。不借#32或#31的source review代替這次資格，實測baseline不改。

## 條件與四層證據

TC-01要求SDK管理model對話，UI只投影已允許公開的訊息／事件；model壓縮不刪已保存答案，preview不能成為權威history。這不是同一工具跑出的36個native案例：

| 層 | 實際執行 | 對應斷言 |
|---|---|---|
| Native backend | 4個fresh Python／SDK process、6個offline model requests | 原大checkpoint確實compaction及cold resume；13次舊reply的body／hash／no-store相同；舊turn `reply_available=true`、job不變、沒有stream；純讀不ACK |
| HTTP stream | 真loopback HTTP＋合成provider，不啟SDK | Preview有公開文字但durable/complete皆false，private reasoning字串不在projection，正式reply GET為404；重送原model command409 |
| Bridge | 真API／SQLite journal／bridge、SDK I/O替身 | 原兩回合／model與原文關聯、reply digest、重複純GET與cold client重建不重送 |
| UI script | 真HTML script在minimal DOM執行32案 | Saved answer優先於preview/reference；preview消失與頁面狀態重建不改舊完整文字；未產生POST或ACK |

這四層可依共用Interface組合判斷，不等於真browser端到端跑過一次compaction。Source核對另確認：LiveReplies的writer排除SDK summary，reader只提供未完成turn的public projection；SDK恢復取archive，不取preview cache。這是source推論，不增加runtime案例數。

**source推論更正：** 上述「SDK summary不進preview」只涵蓋FA已標記的受控摘要request，
不能推出未設定profile的任意SDK compaction都會先被攔。舊observer在profile缺省時直接return，
合成event/gateway反例可到mock reserve／sender；因此current TC-01撤回，需由新固定source的
gate與projection checks重新支持，而不是把舊接受run或61-case TC-11觀察改標為新結果。

**另一個source／consumer差額：** thin-client的strict `ConversationTurn`對非legacy
`prompt_assembly`欄位可能拒絕。先前以共同source推論其他profiles的UI都可用並不充分；
用途profile的owner已從真API200→bridge502重現，再加strict typed optional欄位修補。
這與保存的原reply仍可由API取得是不同層，不改舊36 checks或虛構每profile皆有UI runtime證據。

本節四process的native保留案例使用legacy profile。其他prompt profiles的投影還會核各turn的archive；
這部分由[PA-02已保存的checkpoint案例](prompt-assembly-evaluation.md#第五個prefix的重新驗收)與共用projection source核對支持，
不是本36-check批次另跑每種profile一次。這個共用路徑推論與legacy的13次直接bytes／flag觀察分開。

## 壓縮時的原件與可見性

新增斷言放在既有`test_sdk_compaction_native`四process fixture，未改產品保存／compaction。每次觀察先純GET conversation，再取原reply與job status：

1. 第一份公開reply耐久保存後，compaction前、產生摘要的request中、compaction完成後都保留相同JSON bytes與SHA。
2. 後續兩個fresh runtime沿原SID恢復，前面所有reply均可讀，public `reply_available`仍true，completed turn沒有暫存stream。
3. 原first reply為117687 bytes；所有13個保留觀察均未履行收件責任。`succeeded`、pure GET、畫面文字與human-read仍分開。

`reply_available`不是由直接GET成功倒推。資格檢查先指出冷UI是否取舊答案取決於該旗標，之後才補上其實際projection斷言，避免「原件在但UI不會再取」漏測。

## 固定版本與原批次

| 批次 | 結果與基準 |
|---|---|
| 最初native WIP | 1 passed／43.861秒；baseline未知，尚未核public availability旗標 |
| `f13cea9` | 4個Python＋32個minimal-DOM cases通過，第一個分層候選 |
| Preview反例WIP | 1 passed／7.01秒；新增private不進projection與reply GET404 |
| `e9e052d` | 4＋32通過；qualified source檢查仍指出old-turn availability缺件 |
| **`d9f2717825bb55928d24f41b1b113e03f9229ecc`** | **4＋32 passed／0 failed／0 skipped**；11:06:53先commit，11:07:53才開始兩個runner，期間工作樹乾淨 |
| **併入容量修補後的`12f624f1c304657d3d8e8a051fbffac5e3a75df5`** | **再次4＋32 passed／0 failed／0 skipped**；11:29:26 merge commit後，11:34:14開始；current C宣告改用此baseline，不將前批回填 |

不同批次重疊，不相加為獨立樣本。最後的一個native案例仍是6次model requests，另外HTTP stream的合成transport與bridge替身不冒充native模型。四個真runtime皆正常stop，force-stop為0；不把fake SDK stop或DOM state當成實際程序／雲端停止回條。

## 驗收宣告與界線

原36個程式checks在`12f624f1`同一baseline支持當時已合併的TC-01 C接受；現在只保留為歷史，不使current state忽略新反例。`0680d59`新69 Python＋32 DOM已核event fence及既有public-view回歸，但尚不足以宣稱所有非legacy UI consumer可用；current保持partial。PA-02既有接受不由這個修補改寫。

## 修後完整重驗

前段partial是guard修完、bridge尚未採入時的狀態，不回寫成當時已通過。
#26的typed consumer已在真public bridge重現RED再GREEN，且metadata不能開啟follow-up／reset／ACK；
與#25 guard整合後，在固定 **`ed6f64e98367bddb5a86ebe359ce6a4fd25cc02a`**跑完整current矩陣。

同一執行有105個Python＋32個minimal-DOM checks全通過，TC-01選其中**39 Python＋32 DOM＝71**：

- 14個instruction native cases的真projection直接進入bridge使用的strict DTO，含所有五種instruction
  prefix設定、checkpoint／已壓縮cold restore、skill用途／版本更新及原話reader。這是實際typed消費，不再只靠假設新metadata會被接受。
- 12個真API→bridge cases核legacy／四schema、malformed拒絕、原合法view/reply保留、
  queued metadata不成權限及冷匯入無SDK；SDK I/O仍是明列的替身，不當native。
- 一個真SDK四process retention案在13次觀察核舊reply bytes/hash、reply_available、no-stream與非ACK；
  兩個HTTP/preview cases、九個已設定／未設定event guard與ordinary control，以及一個既有bridge/reconstruction case分別覆蓋其接點。
- 32個minimal-DOM cases仍不是browser，但會跑原HTML script的saved-reply/preview/reconstruction呈現邏輯。

這71和PA-02的80是同137-check合批的重疊子集，不是另一次執行。
Current TC-01以此新baseline重新提出接受，不借原legacy-only36或局部修補22的PASS；
native背景壓力仍未觸發、真preview曝光未觀察，程式保護的定義是已收到不合法compaction事件後阻止後續dispatch。
TC-09完整併發、SK語意、actualbrowser／Azure／Hosted／Blob及human-read都未由本C資格接受。

TC-01的程式結論由上述實測和明列的source分工共同支持。Current spec沒有要求C類測試一定在真browser／Azure執行；但不能寫成那些環境已驗。原Opus5.5 Standards／Spec曾正式核#27的六profile head，不是借#23舊接受或#25／#26局部clear放行；後續新增profile依下節重新資格核對。

## 第七profile的接續資格

已審工作記憶新增第五種prompt record schema；incoming TOOL-06也新增安全參數錯誤回饋。
Memory整合先用真callbacks重現六個authority分類RED，再保留`read_skill`／`read_work_memory`
的固定拒絕遮罩，其餘generic schema feedback仍保留。這些都已隨#30合併，原global資料與舊批次不回填。

在新固定source **`cb4b1805c9622817453f6711e23c7df0c82a034c`**先保留current partial，
再完整跑161 Python＋32 DOM＝193 checks。TC-01取其中**43 Python＋32 DOM＝75**：
16個instruction native projection DTO cases、14個真API→bridge cases、1個native保存回覆、
2個HTTP/preview、1個frozen bridge及9個event guard/ordinary control。

新增memory ordinary/checkpoint兩案實際讀出已審note及必要原件，後續cold restore保留provenance；
每輪真API projection都進bridge使用的strict DTO，核對job、reply可取與backend決定的follow-up。
原archive不變，newCID與原SDK identity關係明確。這不是以source推論新schema必然可用。

25個TOOL-06 callback checks是同batch的額外回歸，不算在TC-01子集內，也不接受另一工作線的完整
runtime target。109-case PA-02和75-case TC-01共享16個Python cases，不是新增執行或model用量。
該次current接受已在#32固定`1c3db91`經原兩軸正式審查後合併，沒有借六profile的#27或memory局部34＋1直接放行。

Memory仍只Team/binding單一已審note；private偏好、動態更新後接續、M品質與full PA-13／PA-16未接受。
Native、API/bridge SDK-I/O doubles與minimal DOM仍分層；無真browser／native背景壓力／雲端或human-read宣告。

## Verified against

- 產品基準為`7076ce5`，測試提交`f13cea9`／`e9e052d`／`d9f2717`只改既有tests及直接說明，沒有修改產品、UI或SDK。
- 完整backend XML SHA256：`5320bdf59aa29fed26a989842cdab2328595dbe619160d3f6e3c3c1bddf0983b`；minimal-DOM XML：`13c48d4b3ebb28de8b003ef44c662e513cac3d0a8e54c807972dc494401a73e4`。
- Safe native proof：`decbd30b53185ef5ed6359b6c0cc466140ea6dac458893471fc9e7ca3a7f9cef`；只新增public reply hash／byte count／flags，沒有公開正文、credentials或private reasoning。
- 容量修補整合後的current backend／UI XML：`d0a2ad9d7f8fc50104ea3f7f06168ac69052902d878871debdfbda5a60a0ca23`／`60470eedd8fe7d5c51581fd3a52e403b6a8e675ff8fa3561d2e6f66315ed937b`；safe native proof `bc6e0461b62ab73ce4a548eb5477e103971294932a4e57fdf73444a857d4836c`。依同一條件核13個availability／no-stream／bytes／非ACK觀察。
- 試驗用loopback server與SDK程序沿原fixture關閉，只清理自己已核的`fa-tc01-c7b-01`到`05`具名目錄；不是刪任何使用者聊天、主8765或未知雲端工作。
- 原195筆及其normative spec／migration／bindings保持；新增TC-01候選與既有review回條的索引均標清楚，不把資料索引當另一次model review。
- 歷史合併讀回：`2f33baa8ce5df29d2a862d2d73286188991700b8`的parents為`1b7f4d9`與`609bd57`，tree `96a6ce7ed16bf1b5bc4735d53c97f7871d333f65`符合當時審查基準。該次回條更新沒有新增SDK執行或改接受baseline；下列修後重驗是另一批，不倒改它。
- 最新重驗另用`ed6f64e`而非上述舊baseline；Python／DOM XML hashes為`19c993d8a5b598c6a2358015e317efa17f10d959e64726376123562850e7c601`／`3cb212d9e807b1dc527d89dc5f5f360cb69f1cd9587b35518b96c5720460e88e`。Native retention proof `001e8a2463393531fe5551832f71e9aa2ababcc314cabb2e49ca32a0cab9168e`，typed native projection／API bridge／DOM分層在同一source上重核；#27正式review與merge回條另保留，不新增或重標本次執行。
- 七profile重驗另用`cb4b180`，Python／DOM XML為`40b800c4a57ed722fa4ba2aca5b3009aeb0d24878d660401d013f6a2b456e922`／
  `5b04ee0f289532898270bb60b3fa920b40d000e5e8558782df2af06e7fc858db`；
  retention proof `9b5d325a99e47a669f45250d29b7e3c5a6e25f728805653ab64789b078122935`。
  原20個memory批次、source先commit、callback雙入口及不相加的subset index均有各自原件；新current宣告仍待正式review。
