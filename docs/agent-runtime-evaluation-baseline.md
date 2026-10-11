# FA Agent：五類設計／實作盤點與評測 Baseline

<a id="native-routines-evaluation"></a>
## 2026-10-04：Native routines／reminders（CODE/offline，預設關）

核准短計畫`20c11b36…`；產品／測試`a229be94e8f6d06e6d9450e5bea9c8208b8827b4`，
基底main`bd3c4ba8`。S1來源與context、S2受限producer與one-shot Job client、S3原attention／工作台已接；
不是#259 controller復活，不新增timer、IAM、雲端資源或部署。Job與S4仍須Neal另批。

| 批次原件（本工作階段files） | 結果與支持範圍 |
|---|---|
| `routine-due-recovery-green1-20261004.xml` | 30 PASS；原意圖先保存、失ACK跨日同RID核回、舊#259日界規則相容。 |
| `routine-continuation-green1-20261004.xml` | 17 PASS；routine沿原API與④接續保來源／UNKNOWN，舊continuation API一起回歸。 |
| `routine-auth-lanes-attention-20261004.xml` | 27 PASS／1 FAIL；兩lane真handler→NativeSdkExecutor、producer身分負例及原question回答／過期通過；長文案在首次started CAS出現`PermissionError errno=13 winerror=5`。原FAIL保留。 |
| `routine-writing-isolated-20261004.xml` | 上一長文selector獨立1 PASS；不修改SDK或UNKNOWN來迎合Windows。 |
| `routine-real-sdk-20261004.xml` | 真SDK1.0.13／runtime1.0.83，官方local N1三回合1 PASS；5個合成模型HTTP requests，含routine suffix、原工作reader及按需長文tool。不是Hosted或真模型。 |
| `routine-api-cold-20261004.xml` | 冷程序禁用`copilot`／`azure.ai.agentserver`匯入，API與Job client 1 PASS。 |
| `routine-dom-full-20261004.log` | 共用HTML完整485 PASS；無網路、模型或writer呼叫，沒有新批准卡。 |
| `routine-rebase-bd3c4ba8-20261004.xml` | 78 PASS／10 FAIL：8個SimpleNamespace receipt fixture缺新增human分類欄位、1個首次started CAS WinError5、1個原factory case。fixture改用實際DTO後，相關記憶與獨立usage案35 PASS（`routine-typed-memory-bd3c4ba8-20261004.xml`）。 |
| `routine-factory-bd3c4ba8-baseline-20261004.xml` | 同一factory selector在未改的main副本仍1 FAIL；它直接呼executor而未提供完整N1狀態。此片未修改該產品路徑或把基線FAIL改PASS。 |

各批重疊不相加，原RED、fixture／環境錯誤、Windows失敗與後續通過分開保存。
固定`a229be94`相干34檔為**529 PASS／9 FAIL／1 deselected**（`routines-qualify-a229be94.xml`）。
2個lane-context的SimpleNamespace fixture改用完整DTO，4個本機store／admission失敗案例獨立補驗，
加上相關consumer共**25 PASS**；3個console測項仍預期第一個RPC為`read`，實際published路徑為`session_read`，
三者在未改`bd3c4ba8`皆重現FAIL，不改產品預檢迎合舊斷言。原factory基線另1 FAIL，於大批明確排除。
本片全部新增案例加冷API重新執行**36 PASS**（`routines-scope-final-a229be94.xml`），
共用DOM重跑**485 PASS**（`routines-dom-final-a229be94.log`）；包含真固定SDK三回合5個合成HTTP請求。
最終两軸及merge資訊在交付時補入，不用這些後續PASS改寫前批。
Docker Linux engine pipe不存在，Linux批為NOT_RUN，未重啟共用服務。

### Verified against（native routines）

**初輪Opus5.5（固定`6e4ac0dc`）：Standards 0 P1／1 P2，Spec 0 P1／3 P2。**
四個實際問題：逐步回API違ADR-0006、單筆DST／單Agent失敗遮蔽其他工作、缺N1回條使整頁reminders失敗、
取消routine仍注入SDK。已分別保存`routine-review-std1-red`、`spec1-red`、`spec2-red`、`spec3-red`；
另外兩個壞時間／游標的Agent隔離反例保留在`spec1-protocol-red`，不改原批數字。
修法沿原入口／N1取消，不新增架構例外；每筆錯誤有`issues`，不清原意圖／UNKNOWN；
owner歷史與Agent未完成提示分開。原核准計畫及原通過紀錄不改，修後增量與原兩軸複核另記。
較早fixture修正在後續commit的歷史，交付採squash整片，不把中間狀態單獨落main。
固定修後`1679dede`連同`5b92a518`／#580交會、兩lane與真固定SDK、原Effects／continuation及source檢查，
**106 PASS／0 FAIL**（`routines-review-fixes-qualification.xml`，
SHA `4b3d0ca33cae4e113d0bac49b4858d87dfefc44ad082a11374a55894b47965bb`）；
共用DOM **494 PASS**（`routines-review-fixes-dom.log`，SHA `8979ce40c5a60509f1a44f4826b1739cdaf45af63596a4ec610cf8b417195f48`）。
主張限CODE/offline，原軸複核已回覆。交會首跑42 PASS／15 setup ERROR的Windows長路徑原件另留；
同35個source selectors用本次唯一短暫存路徑35 PASS，未改SDK／產品。
**最終原兩軸（固定`090a56cd`）：Standards 0 P1／0 P2、Spec 0 P1／0 P2，CLEAN。**
Spec在記憶體重跑原三反例並另報34＋12個相干測項通過；Standards核ADR／程式與append-only帳冊。
回條`routines-final-review-20261004.json` SHA `4757e6944e08e5cd11fc02b03e093c2936055752a971a6564a3987e5db384cae`。
合併採squash；之後把兩個target的`current_baseline`校準為真實main squash SHA，不改任何舊run baseline／原件。
**正式交付：** #581於2026-10-04 03:39:40+08:00
squash為`f7a45f20b44fe583f1bee134616fb5a02c673055`。main tree `86dd6b5e…`與最終head `220e0cf0`完全相同；
與已審head只差審查文件。PA-09／PA-10的目前baseline改指該main commit，原run依舊引用當時受測版本。
交付原件`routines-delivery-20261004.json` SHA `e7c1a0703a9beb3527a2b64081ea8271fccc0a0ad908ce7140ad20364a9eefa3`；
旗標保持關閉，沒有部署或雲端／IAM操作。

- `NativeRoutineService`原owner／版本／occurrence意圖、兩lane原N1與SDK、NativeEffects檢查、HITL原append、
  ④來源保存、metadata startup reader；工作台只讀原Agent/CID/RID，不呼producer。
- PA-09的程式判準是來源可分辨、短答不注入長文指南；長文指南為按需SDK tool、綁本次RID。
  PA-10的C機制涵蓋原工作／effects／HITL引用、回答／過期不重建、取消不開新工作。
- PA-10的M品質、正式Job至少一次觸發與雲端IAM／實際費用均未驗；不升完整PA-09／PA-10／WAKE-05或E-07驗收。
- [API、旗標及官方差額](../src/fa-shared-context-poc/README.md#native-routines)；較早#259原批不變。

<a id="native-continuation-evaluation"></a>
## 2026-10-03：中斷後安全接續④（CODE/offline，兩軸 CLEAN）

依核准聯合計畫`5f18a290…`§4–6；基準由`08df49a1`正常整合published `b7ded4c7`，
再整合③ #548的`d0d8d967`；後續已合入② #553的`14ba0e89`及最新published `92bd2986`。
原RID／UNKNOWN不改；官方process-loss重入才可沿N1意圖與原enqueue產生穩定新RID，
同root最多2次、先讓槽、不搬lease。原checkpoint／append／問答、當前撤權與新once、效果水位及
原peer／group回程一起接線；工作台只讀關聯與用量未知，不造新真人訊息或重送。

| 原件 | 結果與範圍 |
|---|---|
| `continuation-core-1003.xml` | 92 PASS／1 FAIL；早期相干批，原分段收尾案例遇本機PermissionError；隔離原selector為1 PASS，原批不改。 |
| `continuation-final-1003.xml` | 68 PASS／2 FAIL；原effects案例在admission／checkpoint保存階段失敗，尚未到效果觀察斷言。 |
| `continuation-effects-min-1003.xml`／`continuation-effects-raced-1003.xml` | 最小四案為3 PASS／1 FAIL，`raced`獨立程序1 PASS。 |
| `continuation-effects-baseline-1003.xml` | 相同環境、08df原始碼副本四案3 PASS／1 FAIL（absent的store `PermissionError errno=13 winerror=5`）。只證基準也有本機store失敗，不把前述各次失敗的唯一原因當成已定。 |
| `continuation-b7-final-1003.xml` | 85 PASS／1 FAIL，139.39秒；所有本片案例、真固定SDK custom/task、兩lane官方host恢復、API冷匯入及原收尾。唯一`raced`失敗在admission前；`continuation-d0-raced-1003.xml`原selector獨立1 PASS，未放寬產品拒絕。 |
| `continuation-real-files2-1003.xml` | 4 PASS／22.04秒；③原兩lane跟版，加④兩lane官方恢復→signed環境API→真staging／publisher／ready CAS→新SID原checkpoint續行一次。兩檔各PUT一次、stop一次、重述不多跑；舊UNKNOWN保留。 |
| `continuation-ready-final-1003.xml` | 27 PASS／32.36秒；第一個ready及already-ready POST的同通知ID、失ACK、metadata GET／reconcile零通知，並保原環境控制。 |
| `continuation-d0-ui-1003.log` | 199 PASS；原聊天、progress、append、attention、group、settings及③follow／environment與④顯示並存。 |

各批重疊不相加，不回填原FAIL／ERROR。早期逐片RED／修正、取消pending／active的2案、
缺checkpoint／水位與普通失敗不重跑的3案、意圖失ACK的2案、ready／版本反例與新once原件都留在
同一session的`continuation-*`檔；JSON列出本片主要批次及hash。
意圖create失ACK且原件相同才續同derived RID；讀不到意圖則原UNKNOWN並停止，不能寫成兩者都自動完成。
加入第三個host的測試初稿曾重用已關閉event loop（2 ERROR），改測試生命週期後通過；
初次交會另有tokenizer環境漏設及原fixture未識別第一個ready通知，均修正fixture／執行設定，
不把它們算產品RED或改鬆產品路徑。較早跨SID案只替代Files發布事實，不能代替上表真publisher交會。

### Verified against（④）

- 真固定Copilot SDK1.0.13／runtime1.0.83、core/Responses2.2.0、Invocations1.2.0；模型HTTP、
  外部效果與平台task／版本／Files網路為具名離線替身。新SID整合不是Azure Files權限或Hosted live。
- `native_continuations`／`native_operations`、原`NativeEffects`、兩lane入口、Agent-only `/continue`、
  原HITL／append／T5／group及工作台；API冷程序禁止`copilot`／`azure.ai.agentserver`匯入仍通過。
- [官方差額與實作](../src/fa-shared-context-poc/README.md#native-auto-continuation)；
  未建IAM、雲端資源、排程器或另一套SDK loop，未操作8765／stage或live，未升完整Target acceptance。
- 本片兩個原Opus5.5 Standards／Spec軸皆已CLEAN；原#487審查未移用到此片。

**首輪審查（固定`ee8ef6b7`）：** Standards為0 P1／3 P2，Spec為0 P1／1 P2，
其中Spec與Standards第一項相同。三個實際問題為：停止前未接單仍壓住原回程、
意圖未落盤卻漏原terminal通知、並行工具共用索引耗盡CAS而誤拒。
`continuation-review-red-1003.xml`真3 FAIL／2對照PASS；窄修後
`continuation-review-green-1003.xml` 5 PASS。停止／暫時待ready與原T5／group consumer
另8 PASS（`continuation-stopped-source-1003.xml`）。
原軸指出API／relay還會遮蔽三個確定停止碼；真signed API及Hosted client再3 FAIL／5對照PASS
（`continuation-refusal-wire-red-1003.xml`），沿`/continue`限定status/code修補，不把其他4xx／5xx誤判停止。
固定候選另跑36案為35 PASS／1首次started CAS的`PermissionError`；
同head隔離active取消案1 PASS，原批保留。修後完整增量與原軸複核仍待收尾，不提前寫CLEAN。
增量首批`continuation-review-final-1003.xml`為70 PASS／2 FAIL：N2案首次started CAS的本機store失敗，
另pending通知案把callback次數誤當邏輯交付。斷網Linux也重現後，核固定core原始碼確認
one-shot `@task`會在terminal刪紀錄，穩定task ID不是永久去重；沒有新增context、lease或通知帳本。
取消測項改核通知只攜同一原件，並由真T5／group consumer的重述確認只有一個wake／結果帖／原root名額。
**修後固定snapshot `04ce353f…`：斷網Linux 72 PASS／0 FAIL／0 skip，71.30秒。**
沿79fa已建本機映像`fa-selector-offline:79fa`、空HOME與tmpfs，SDK／runtime版本不變；
`continuation-linux-review/full.xml` SHA `f2b27ab36fa6206bbaad3a86db3143a0bad6eb30995664c18a5f7fccedb879d0`。
包含三項P2、exact拒絕碼、原接續／取消／HITL／effect、真Files兩lane、API冷匯入與
T5／group冪等回程。未改SDK、重試上限或UNKNOWN來繞過Windows限制；原軸固定delta複核仍待完成。

**正式②③整合（main `92bd2986`＋本片）：** snapshot `3ac0f183…`在同一斷網Linux環境
115 PASS／0 FAIL／0 skip，106.61秒；最新共用UI相干205 PASS。XML／log／hash見JSON的
`published_integration`。涵蓋完整接續增量、原relay／effect／final-save，
②③兩lane的真API首次送出，以及本片恢復後的Files／ready交接。
首個整合批64 PASS／27 FAIL，原因是relay fixture仍提供舊未帶版本的SID；
改為當前已發布版本的SID、保留健康sender，再把「收件者未準備」注入只限收件者，
沒有鬆動產品版本guard。中間112 PASS／3 FAIL及Windows fixture批也保留，不回填為全綠。

**第二輪原軸（`3169cee0`）：** 原三P2均關閉，Standards新增0 P1／2 P2、Spec新增0 P1／1 P2
（相同的通知查讀問題）。新修補涵蓋終態handoff讀取5xx的原retry／耗盡標記、停止後不穩定
admission回503、排程失敗仍安排原通知，以及child取消移出admission lock防止recover競態死結。
固定core recovery入口沒帶retry參數；因此將handoff資料查讀／引用登記放進原fresh enqueue重試，
不是自行補agent loop或改SDK。另修TC06附件查找略過完成的continuation來源。
`continuation-delta2-red2-1003.xml` 4 FAIL／1對照PASS；停止觀察另1 RED，五邊界修後5 PASS，
附件來源另1 RED。全部runtime與原附件reader **20 PASS**，原件與hash見JSON的`second_review_delta`。
新Linux批在開始前因Docker engine pipe不存在而**NOT_RUN**，未重啟共用服務；
前版115 PASS不冒充新delta結果，原軸複核尚待完成。

**兼容 #564 正式交會（`b2ff46ba`）：** 保留原省略檔案流程，未新增skip writer。
`files_migrated=false`且無manifest仍不可自動跨SID；原intent／UNKNOWN維持，
只在既有continuation投影明示`NATIVE_CONTINUATION_WORKSPACE_REQUIRED`（1 RED後修正）。
接點批48 PASS／1 FAIL／1 ERROR；兩個原省略檔案案例遇Windows接受寫入PermissionError，
各自獨立補驗合計2 PASS，原批不改。Node相干209 PASS；API／DTO冷匯入另2 PASS。
新P2的20案與這些交會案是分批證據，不和舊Linux115相加，也不當成live。

**原軸再核（`561d3db9`）：** 前輪查讀／停止觀察／取消鎖問題已關閉；兩軸各餘0 P1／1 P2，
同指enqueue.start的完整transport例外與prepared意圖缺少重排。現在先存pending引用再碰task API，
並由已有retry的原notification冪等補排prepared工作，保持自動接續而不因失ACK取消已admitted child；
不是新增排程器。四個反例先4 FAIL（含AzureError／TransportClassifiedError及新舊附件不同內容），
第一批修後5 PASS，全部runtime＋原TC06 **23 PASS**；metadata、兩lane及原件ID／回合號均核對。
較新的中斷真人附件改沿已核continuation原鏈讀回，不退回舊同名檔；前輪「略過續行」只屬當時版本。
原件見JSON的`dispatch_start_delta`，仍未補跑Linux／live；原兩軸複核待完成。

**最終結論：固定產品`4ac051e6d233c7696a8a64e0890dbff783d9b9b1`，Standards 0 P1／0 P2、
Spec 0 P1／0 P2，全部已報問題已關。** 兩原軸確認原pending引用、完整transport分類、
既有notification retry、N1原RID防重送，以及真正較新的人類附件來源；Spec另核23案XML與hash。
審查紀錄`continuation-final-review-1003.json` SHA
`1fed1245fddbb3c0afb81af5156f86ea4dafd3fbbe4281e74acd57d49daaccfe`。
之後僅記錄結果，不再修改已審產品／測項；未live，整合後由79fa與部署線安排。

## 2026-10-03：SP-22 換輪與循環交辦錯誤指引（CODE/offline）

基準main `160bde100bbfd40a208552e5f291d964f99ec6da`加本片。3006批G4的原場次沒有換輪，
G5在題目明講目前輪次後走完三輪；沿[已發布紀錄](progress.md#2026-10-03main-300641f3採用ab開hitlsp-22群組通過role-v3探針3過)，
不改G4原結果、不把本機腳本模型算成新的群聊品質或live驗收。

兩個直接缺口：`expected_round`缺欄位說明；原`ToolFailure`沒有`public_code`，
經既有model boundary被改成`Authority refused`。現在沿固定SDK正式
`Field(description=...)`送目前輪次及防並發用途；exact409輪次衝突沿共用固定文字到達模型，
提示先`read_group`再填目前輪次，不增權、不自動重試、不改原CAS／3輪10帖／保留格。

成員不是全面禁傳訊；回主持者的新request／handoff本來就以`NATIVE_GROUP_REPLY_REQUIRED`
阻擋循環，其他合法成員交辦與原結果回程不變。這個確定拒絕的原因原本被HTTP／Hosted遮掉，
本片只補原`/send` exact409及模型固定說明，測試保留原結果仍回來。
G5第2輪缺當時call/log，不能把這個程式分支當成該次唯一根因。
**傳訊指引只驗HITL-off（3006 t1／t2）**；HITL-on的原`agent_call`包裝會改為
`NATIVE_HITL_EFFECT_UNKNOWN`，本片未接齊此組合，也不修改效果／once契約或關閉HITL。

| 原件 | 實際結果與界線 |
|---|---|
| `sp22-round-red-1003.xml` | 2個新fixture setup ERROR：Windows事件迴圈晚於離線socket guard建立；非產品RED。 |
| `sp22-round-red2-1003.xml` | 2 FAIL／7.50秒：真signed API與工具schema，缺目前輪次說明及固定錯誤指引。 |
| `sp22-round-seams-red-1003.xml` | 7 FAIL／1 PASS：4個產品反例（含真SDK模型可見文字），另3個新fixture的async token／import錯誤；真權限拒絕對照PASS。 |
| `sp22-round-member-red2-1003.xml` | 2 FAIL／1 PASS：request與handoff的原循環拒絕被遮為`NATIVE_RELAY_REFUSED`；成員交給其他成員的對照PASS。 |
| `sp22-round-seams-green-1003.xml` | 8 PASS／18.32秒：直接反例、讀回後換輪、重述不再換輪、stale輪次拒絕與原結果回程。 |
| `sp22-round-regression-1003.xml` | 63 PASS／58.10秒：群組state/API/tools、native傳訊tools及既有receiver錯誤6案；含固定真SDK、wrong status／unknown code／未登錄工具反例。 |
| `sp22-round-api-import-1003.log` | 禁止匯入Copilot／Hosted SDK後，API入口、共用boundary、群組DTO／API及relay API冷匯入通過。 |
| `sp22-round-readback-red-1003.xml` → `sp22-round-readback-green-1003.xml` | 初審非阻擋提醒的窄增量：原提示缺「已到原預期輪次就不再換」先2 FAIL；補文字後schema／signed API／真SDK三案PASS／10.52秒，不另改CAS或重跑63案。 |

各批重疊不相加、原FAIL／ERROR不回填；hash與selectors見[JSON](agent-runtime-evaluation-baseline.json)
的`sp22-round-feedback-20261003`。沿既有環境：Copilot SDK1.0.13、core/Responses2.2.0、
Invocations1.2.0；runtime套件manifest為1.0.83（runtime不接受`--version`，未把該探測失敗當測試失敗）。
真SDK測項只使用腳本模型與HTTP／VM替身，沒有真模型、Hosted、Azure、IAM或排程操作，
沒有跑UI或無關大套件；程式旗標仍預設關，修後live另由部署線安排。
兩個fresh Opus5.5軸初審`9ba27aff`：Standards 0 P1／1 P2、Spec 0 P1／0 P2。
P2是文件將傳訊指引外推到HITL-on；按原軸建議明列HITL-off適用範圍，
不改既有HITL效果未知或once契約，另補上述讀回後不盲目再換輪文字。
原兩軸對`ae7a10f7c5b344045b6b35e8485cad2656b1437f`的窄複核皆0 P1／0 P2，
Standards明確關閉原P2，沒有新增finding；兩軸複核只讀delta，不重跑既有批次。
審查原件`sp22-round-review-1003.json`
SHA `edddde5237b115d02400c02e70086943a9e75fb02a21d455951cf6c9cb327f5c`；
其後只同步審查結果與交接狀態，不再修改已審產品或測項。
Spec另報19案及63案通過但未保存log/XML，不加入本片的正式批次或合計數字。

### Verified against（換輪窄修）

`native_group_tools/_wire`、`native_agent_api/_tools`、`behavior_assistant_boundary`及
`m2_copilot`的原`Tool.parameters`／`ToolResult`接點。沒有修改`m2_copilot`的工具循環或permission設定；
官方固定來源與部署界線見[README](../src/fa-shared-context-poc/README.md#群組換輪與傳訊錯誤指引codeoffline)。

## 2026-10-03：SP-22／WAKE-06 有界群組（LOCAL，預設關）

基準main `db039f18`（含#462工具列、#467 H3），產品固定`03f0f571`。
已接Team group CAS／公開原文／3輪10帖與成員3帖、收尾及結果保留名額；
typed FYI／receipt有理由沉默、不啟peer工作，T5／原結果wake及SDK執行接點保持。
8765獨立panel保原草稿／UNKNOWN，原回合只讀附到既有native及NeedsAttention；沒有新批准writer。
API／Hosted／工作台均預設關閉，未部署或做live；原兩個Opus5.5軸對`efa9c58f`各0 P1／0 P2。

初審固定`03f0f571`：Standards為1 P1／1 P2，Spec為0 P1／2 P2，共3個不同問題：
群組讀取／公開失敗會阻斷普通工作或T5、成員交辦回主持者造成待答循環、未到API的未知開題永久卡住。
固定`f8255bda`修補：明示群組工具不可用而不終止普通工作，原relay不被群組公開攔住；
原final早於成員公開時只存已核引用，由最後原結果CAS一併發布，不新呼模型／wake／queue。
群組request/handoff回主持者在發布前明拒，result/blocked照舊；
UNKNOWN開題只有exact NOT_FOUND才同ID/body由真人續存，明確拒絕另留觀察，不改原UNKNOWN。
原軸只核finding＋QA增量，不重審上游或開新review，全部已報finding已關閉。
`f8255bda`複核：Standards 0／0；Spec原兩P2已關，但新增首輪漏報P2——收件版本明確拒絕卻未釋放group結果格。
`efa9c58f`只補fresh exact409版本／session拒絕並確認原peer RID不存在的收尾：
原delivery另存server refusal，不造B回條／發言、不加Agent帖數；CAS不明為503可沿原有限follow核，
同message不能因之後資格恢復而重放。未知／transport維持pending。此最後增量兩軸各0／0，
Spec另重跑原情境2案通過；最終回條`sp22-review-final-1003.json`
SHA `ebcbc5d6dc742bb0b966cbed28df95b3b77a0c4c7bcd5259819942514813d61b`。

| 原件 | 實際結果與範圍 |
|---|---|
| `sp22-final-code-1003.xml` | 22 PASS／24.64秒；四個新群組檔的state/API/tool/console，含固定真SDK。 |
| `sp22-publication-final-1003.xml` | 9 PASS／0.79秒；最後原收件者與公開ref核對、計數／CAS。 |
| `sp22-pinned-sdk-1003.xml` | 2 PASS／6.51秒；Copilot SDK1.0.13/runtime1.0.83使用FYI工具後原final，模型及群組HTTP為腳本替身，無peer start。 |
| `sp22-integrated-1003.xml` | 86 PASS／1 FAIL，122.14秒；原append案例在起始admission回NATIVE_ENVIRONMENT_UNKNOWN，尚未到追加。原因未驗證，不稱既有失敗。 |
| `sp22-ui-sdk-delta-1003.xml` | 6 PASS／16.76秒；上列失敗selector單跑及最後群組UI／SDK，原FAIL批不回填。 |
| `sp22-h3-intersection-1003.xml` | 9 PASS／24秒；H3原三選／attention、remote journal／cookie及defaultoff交會。 |
| `sp22-ui-node-final-1003.log` | 71 PASS；群組defaultoff／純文字／晚到丟棄，原Responses／append／icon／HITL／attention。 |
| `sp22-opening-scope-final-1003.xml` | QA增量`3904928d`：14 PASS／9.86秒；單Agent工作台只啟自己的主持回合，開題先沿原quoted text上限，及state／UNKNOWN直接回歸。 |
| `sp22-ui-qa-delta-1003.log` | 4 PASS；群組選單收起、原unknown body不換ID、不抹新草稿、literal及晚到丟棄。 |
| `sp22-review-red-1003.xml`／`sp22-review-p1-red-1003.xml` | 前者3個P2真RED＋4個fixture引用錯誤；修正引用後後者4個P1真RED，分開保留。 |
| `sp22-review-final-1003.xml` | 35 PASS／33.42秒，含成員首次公開失敗仍wake→主持者先完成→原結果重試後只一個final，以及result／blocked兩情況。 |
| `sp22-review-node-final-1003.log`／`sp22-review-ui-last-1003.log` | 86 PASS；最後登入錯誤一致處理另4 PASS，不相加。 |
| `sp22-refusal-red-1003.xml` → `sp22-refusal-green-1003.xml` | 1 RED＋1未知對照；兩種確定拒絕、CAS412回503後續存、失ACK及原store共17 PASS／11.76秒。 |
| `sp22-refusal-ui-scope-1003.log` | 1 PASS；系統拒收另標示，不冒充B公開帖或吃掉Agent帖數。 |

另以正式HTML與獨立loopback合成回應做瀏覽器QA；group panel保留單一attention與3個原工具列icon，
未執行script文字、無橫向溢出、0寫入請求（群組list的POST是純讀）。已停止自己的helper／頁面，
截圖`sp22-panel-light-final-1003.png` SHA `c23c2372801b07251b7a9ae47baa99dfaf58dc750685cc59c32c633e5842af2f`。
這不是原8765、真後端或live；QA小delta另交原兩軸，不把未審後改動算入初稿回條。
其後正常整合正式main `b3fa25c8`為`d14ecb33`，保留公開步驟與remote修正；
群組五個新Module與`3904928d`相同，只解三份文件衝突。真SDK／群組bridge六案PASS／13.49秒、
Node群組／公開步驟／原接點86案PASS，原件及hash在JSON的`released_main_join`，不是重審上游整片。
最後再普通整合正式main `c4c55b06`為`62531a89`；五個群組Module仍與已審`efa9c58f`相同。
bridge僅合併question/group imports、message map、各自有界body paths及兩個installer；
新ask-user與角色接點15 Python案／25.87秒、72 Node案通過，原件hash見`final_main_join`。

回覆名額反例`sp22-reply-reservation-red-1003.xml`先證實成員未回覆仍可換輪，後續固定原result slot，
不把正常待答藏成silence；hash及批次限制見[JSON](agent-runtime-evaluation-baseline.json)。
早期TDD缺Interface、JSON-mode／validator繫結／插入縮排與fixture邊界失敗均保留，不當作已通過或基準失敗。
API／DTO／工作台冷匯入另在禁止Hosted及Copilot SDK時通過；沒有安裝API全套Hosted相依掩蓋耦合。
中間修補另出1個journal觀察缺created_at真錯、2個fixture誤把原native CID改Responses的錯誤，均已修；
原中間批8 PASS／3 FAIL不改寫。初審回條及固定35案hash見JSON，不把初審寫成已清零。

### Verified against（群組）

`native_group_wire/native_groups/native_group_api/native_group_tools`；原`native_agent_api/native_sdk`接點；
`web_test_console/groups`及原bridge/journal/native/attention。官方能力與僅補群組語意的缺口見
[README](../src/fa-shared-context-poc/README.md#sp-22wake-06-文字群組local預設關閉)。
分批重疊不相加，未新增雲端資源或模型帳；程式接線不等於群聊語意／Hosted／真人收件驗收。

## 原始盤點與後續批次

日期：2026-09-25。程式基準：`cb7a2d8b6868e06d9a941a6bdeb77ba399252159`，即本 worktree 的 #19 merge commit。

**後續主線整合：** 本文原 baseline 的102／33-case兩批固定上述基準；TOOL-06產品切片與後續P2修正的分批記錄另見下節，不屬於這兩批。
其後此工作線整合
`ecb566020dab21bd3d77856b59cc7b44625cd1ba`（#27）；原話 reader、capacity、用途 metadata、
六個 prefix 與公開 view 的新進度以[目前摘要](status/CURRENT-STATE.md)及其來源為準。
以下「目前／本輪」指原 `cb7a2d8` 盤點，不把舊兩工具／三工具配置或 172-run 觀察外推到新的主線；
之後再整合`0d391245542a1bcb4f91ca3daf0f756b3cb4ec1a`（#28），來源線已補齊PA-02／TC-01複核回條及285-run狀態；
原172／283的run前綴完整保留，不用原測試 PASS 代替新接線驗收。

**結論：已有多項受控 runtime 契約與局部歷史實測，但不能宣稱五類已達 Grok 參照能力。**
本輪完成 `evals-start → eval-audit`，建立可重跑的**離線契約 baseline**；
五類語意品質仍是 `not_evaluated`，不是零分，也不是工程測試全綠就等於 AI 品質全綠。

[Target State](agent-runtime-target-state.md)定義五類共 34 個評測切面及正反例；
[JSON 紀錄](agent-runtime-evaluation-baseline.json)保存本輪固定 selectors、分層結果與原件 hashes。
原盤點階段不改產品程式或舊判準；後續TOOL-06有明列的局部程式修正。SDK pin未變、未部署，
原172筆帳冊未覆寫；未執行產品真模型、Memory服務或Hosted工作。

<a id="2026-10-02n6-正式模式採用codeoffline待兩軸"></a>
## 2026-10-02：N6 正式模式採用（CODE/offline，兩軸清零）

總協調核准啟用清單後，先交窄採用PR，SP-22／WAKE-06一頁計畫移到本片合併後。
基準main `ba09c3c8`、產品固定`34b49c67`；79fa核三個接點沒有第四mode：
程式選`conversation`／`retain-cid`，source builder保留`FA_RESPONSES_ENABLED`原值，
未設不補1、0不改1，全部真正probe keys仍剝除。
同build最小active／身份／flags／protocol／非probe／server模板與舊CID保持，不作雲端設定寫入。

| 原件 | 實際結果 |
|---|---|
| `n6-adopt-red-1002.xml` | 4 RED／1正對照，9.65秒；原formal flag的0／1被剝除，兩mode仍None。 |
| `n6-adopt-green-1002.xml` | 5 PASS／7.69秒；兩source builder、正式Options與native預設模板、原CID/checkpoint接續。 |
| `n6-adopt-targeted-1002.xml` | 104 PASS／96.34秒；完整版本、Responses API／環境／entry四檔，另舊CID路由與缺phase已知RID兩案。 |

104案hash `40fc052c758dd5098619661e7f9540d278341f42eb3713e16da362884fa6fcb0`；
直接API fixtures不再假注入conversation，直接使用正式程式選擇，None拒絕仍由反例明設。
原RED與全部歷史批次保留，不相加；兩個fresh Opus5.5軸對`34b49c67`各0 P1／0 P2，
`n6-adopt-review-final-1002.json` SHA `7c6d125d45e99f901abfc6b9533915ac633730ac196e206d2086390ad609ca59`。
正常整合正式main `d10cc710`為`f855707a`，受審產品／測試未變；
交會`n6-adopt-d10c-join-1002.xml`為6 PASS／1 admission setup ERROR（NATIVE_BOOTSTRAP_UNKNOWN），
同案`n6-adopt-d10c-env-single-1002.xml`單跑1 PASS／4.29秒；原因未驗證，不將原ERROR回填或稱既有問題。

Spec另報一個額外矩陣255 PASS／2 FAIL／10 ERROR，未存完整XML/log，不算PASS或加進本片104案。
其「flaky／舊失敗」原推論已收回。父線只對缺`InstructionControl.store`的明確selector做ba09原基準對照，
`n6-adopt-base-missing-store-1002.xml`重現相同AttributeError，確定該fixture在本片前已失敗，不混入無關修補；
其他額外批次的歸因不外推。全程沒有部署、雲端變更或真模型呼叫。

### Verified against（正式採用）

`behavior_native_responses_api`、`native_responses_wire`及`behavior_agent_onboarding._PROBE_ENV`；
原`copy_source`／`original_source`、Options、正式entry及N6-5控制接點。
官方SDK/task來源及必要部署順序見[README](../src/fa-shared-context-poc/README.md#n6-正式策略採用codeoffline未部署)：
source ENV、endpoint protocol、最小active版與owner模板明選仍由部署線執行，
不能拿本機mode採用當test2／A／B已切Responses或IAM已驗足夠。

<a id="2026-10-02n6-t5-首ack收斂local待p1複核"></a>
## 2026-10-02：N6 T5 首ACK收斂（LOCAL，兩軸清零）

原兩軸在`18984879`均關閉P2，但P1指出同一剩餘窗口：原/send逾時後仍在API處理，
follow也讀不到RID，兩個候選CID競爭後409被當永久拒絕，B的accepted仍可能無人推進。
83d4依N6 §2／ADR-0007 §10確認：未知首ACK可沿原task有界等接單ACK，A不先結束；
拿ACK立即返回、不等第二步／模型／整輪。10次錯誤耗盡或取消仍unknown，晚到接單屬明示殘留，
不放寬completed sender權限、不加pending registry。

固定`ed5e8843`，基準為正常整合main `1326a82c`後的`eb7933b5`。
API只在fresh exact409 `NATIVE_RID_CONFLICT`重讀原件一次，完整重用原capture／lane／CID／source／quota分支，
原終態直接回；讀空、材料不同、一般409／403仍拒絕。兩候選平台conversation可存在，
但只採用一張N1回條／CID／digest與root message，不自動刪掉未採用候選。

| 原件 | 實際結果與界線 |
|---|---|
| `n6-main-1326-join-1002.xml` | 11 PASS／4 setup ERROR；缺M2_TEST_RUNTIME，非產品失敗。 |
| `n6-main-1326-sdk-1002.xml` → `n6-main-1326-sdk-fixed-1002.xml` | 重用既有runtime後3 PASS／1 fixture FAIL；純Copilot SDK測試沒有Hosted task manager。明示替換registration邊界後4 PASS，不冒稱Hosted驗收。 |
| `n6-overlap-red-1002.xml` | 4 RED／15.65秒；真gateway與共用helper，HTTP邊界決定性交錯：兩次讀空、兩CID、兩種勝方×capture selection變更；後到者真的收到409。 |
| shell 198 → `n6-ack-cancel-fixed-1002.xml` | 新取消案暴露raw task未看ctx.cancel；teardown超過120秒後停止，沒有完整JUnit，不算通過。沿官方合作式停止訊號後1 PASS／4.44秒。 |
| `n6-p1-convergence-final-1002.xml` | 71 PASS／2 fixture FAIL，91.51秒；前述並發、ACK即返、10次耗盡、取消unknown、舊input恢復及原API／SDK接點均涵蓋。 |
| `n6-p1-boundary-fixed-1002.xml` | 最後8 PASS／15.57秒；一個反例改用「自洽但不同材料」的原件，另一個HTTP-only fixture明示task registration不可用。產品未改，原71/2不回填。 |

完整hash與限制見[JSON](agent-runtime-evaluation-baseline.json)的`n6-first-ack-convergence-20261002`。
原Standards／Spec只核`eb7933b5..ed5e8843`的最後P1增量，均確認P1 closed、沒有新增P1/P2；
P2先前已closed，不重新擴查。兩軸各0／0，回條`n6-review-final-1002.json`
SHA `713a76fcc311455c5e2c1b7fd9216c293e1ab06dab5d2cfa591d904bc870b4f7`。
兩軸最後是讀碼複核（Standards另讀官方SDK 2.2.0），沒有冒稱重跑產品批次。
隨後正常整合正式main `69e7853d`為`c6af682e`；本線已審產品檔無差異，WAKE-03後端／原來源交會
另14 PASS／27.24秒，`n6-main-69e7-join-1002.xml`
SHA `fe62a1dc25afb8428e719cbc5a223ce58e5a40075df8bd26763e831f9717e4ee`。
正式mode仍None，沒有部署、HITL／追加UI、SP-22或WAKE-06實作。

### Verified against（首ACK收斂）

`NativeAgentRelayGateway.send`的單次original重讀、`NativeAgentRelayClient.send/following`的ACK界線與SDK例外；
`test_native_responses_relay_api.py`、`test_native_responses_relay.py`與原HTTP／Copilot SDK fixture。
不改N6-2 helper、N1 VM或wire；不以離線model／VM邊界替身宣稱平台任意exactly-once。

## 2026-10-02：N6-3／N6-4 兩軸修補（LOCAL，待原軸複核）

兩個Opus5.5軸在base `751f2816`／head `f0c89e55`各提出同一組1 P1、1 P2：
首次T5 /send回應不明時沒有follow，可能留下無人推進的B accepted；固定舊lane及無效輸入多出模板GET。
原結論保存在`n6-review-initial-1002.json`（SHA `251ed3efc0671881f870488762942a39510a5712d818bddde76f479bc3f1d217`），不把原審查寫成清零。

修補固定`18984879`：首次unknown也保原command的官方task有限核回；拿到ACK後只登錄同一固定accepted follow即返回，
不讓換版caller等B接手。舊NativeRelaySend持久input/ID與原callback名稱保留，不加queue或第三種input。
固定Table/native不再讀模板；新輸入先本機驗證，新草稿仍須server選路；固定Responses只取必要Options。

| 原件 | 實際結果與界線 |
|---|---|
| `n6-spec-red-1002.xml` | 3個新test誤用Problem.code，屬fixture錯誤；6個既有positive control通過，不當作產品RED。 |
| `n6-spec-actual-red-1002.xml` → `n6-spec-green-1002.xml` | 真正23 RED＋1對照，修後24 PASS；涵蓋首次timeout/500/503及20個舊附件／文字檔／原件失ACK案例。新草稿SQLite rollback明示允許必要模板GET，仍零POST／command／submitintent。 |
| `n6-review-fixes-regression-1002.xml` | 152 PASS／1 FAIL，236.89秒；原官方task、換版ACK即返、有限錯誤、舊input恢復及完整Responses／附件／文字檔／frozen介面相干批。唯一失敗是新測項誤以為本機直接回503。 |
| `n6-pinned-diagnostic-1002.xml` → `n6-pinned-green-1002.xml` | 讀回原契約其實是409 BACKEND_UNAVAILABLE、upstream_status=503；只改期待後1 PASS／6.19秒。產品未再改，固定native登入／讀模型不碰模板，真正新草稿故障拒絕且零POST。 |

153案的原失敗批不回填成PASS，最後只重跑受影響的一案；各批有重疊，不相加。
完整hash見[JSON](agent-runtime-evaluation-baseline.json)的`n6-console-t5-review-fixes-20261002`。
原兩軸核`16129130..18984879`後，P2已關、P1並發窗口續修，見上節；未合併、正式mode仍None。

### Verified against（N6審查修補）

`native_agent_tools.NativeAgentRelayClient`原task／send與accepted identity；
`InstructionStore._stored_lane`、`NativeInstructionControl.refresh_routing`、
`InstructionControl.validate_input`及bridge的submit／authenticate／models接點。
SDK-first來源及離線環境沿下節；測項包含舊native與409 quota不新增retry，
沒有把HTTP替身或SDK local task當成Hosted、真模型或任意exactly-once驗收。

## 2026-10-02：N6-3／N6-4 完整consumer（LOCAL，預設關）

正式 N6-0/1a/2/5 已正常整合；本線新變更只有T5的原relay consumer、8765與直接測項／文件，
沒有重寫VM回合、共用兩步helper、scope/history DTO，也沒有開live mode。
T5初次只接單，原follow同RID補第二步、真錯有限預算；新CID依receiver模板、wake保A原CID lane。
8765提交前讀server模板/資格，舊lane保持；平台history不猜FA角色/RID、不自動掃全鏈。
進度GET純讀，只有browser明確的CSRF POST可執行原RID核回；budget跨controller重載保留。
第一個POST遇401/403同樣停止自動核回，newcode `NATIVE_RESPONSE_UNCONFIRMED`明示尚未接手。

| 批次 | 原結果與範圍 |
|---|---|
| `n6-ui-t5-joined-1002.xml` | 144 PASS／247.05秒；原Responses UI/T5全部新測項及直接舊native、換版、模板回歸，API/VM/platform為指定HTTP替身，follow是真core2.2 local task。 |
| `n6-ui-permission-red.xml` → `n6-ui-permission-green.xml` | 首次401/403後錯把accepted當可自動送，2 RED；保存原status並停止後3 PASS（含持久錯誤預算對照）。 |
| `n6-t5-signed-error-red.xml` | 真JWT HTTP把暫時未接手碼遮成一般503，1 RED；隨後只在原relay路由保留安全碼及reconcile_original，不放寬全域錯誤handler。 |
| `n6-final-ui-delta-1002.xml` | 29 PASS／88.52秒；完整Responses工作台檔及T5真JWT暫時錯誤碼、原換版拒絕HTTP接點。 |
| `n6-ui-node-final-1002.xml` | 44 PASS；server資格checkbox/禁止未核模板、平台角色分離、原cursor/晚到丟棄、CSRF自動核回及舊圖/草稿/模板相干案例。 |
| `n6-final-main-join-1002.xml` | 5 PASS／13.710秒；正式main `751f2816`交會：同SID follow恢復、舊task input、終態回條仍有官方task、舊Invocations CID不改路。 |
| `n6-main-1771-join-1002.xml` | 8 PASS／26.21秒；隨後整合main `177147e0`的可省略Team memory capture欄位，核T5原件／失ACK、兩lane的同CID wake及舊CID路由；本線產品與測試檔相對審查候選未變。 |
| `n6-history-browser-observations.json` | 正式HTML＋獨立loopback合成回應，平台輸入純文字、原答案／缺phase提示、原item游標純GET讀下一頁；0 POST、無橫向溢出，已關閉自己的頁面與helper。不是真後端驗收。 |

原144案批hash `5309bd2414c69e5b6d49298118e23d11ef5988e78905102360be2f582707fbc7`；
最後29案`2addcb9943c34eb28d6693729bb737f653e8b89bc729a2c8cbc6d8c0af629213`，
44 Node案`bc06ec1bea94b28fdbb68a86ecf89fa76aac71756cd573808f60839c7e331f1b`。
產品固定`ce344928`，最後測試`555f8776`；審查／主線交會另外記錄。
正常整合N6-5後固定`f0c89e55`，兩個Opus5.5軸以main `751f2816`為base審查，後續發現與修補見上節。
主線交會原件hash `e70a79ebea0ec07aa8ba6e6de572a260611237fa74102c3fc65269aa9321e7f9`；
瀏覽器觀察hash `da183b23d3726113c7062c1a6bccc5f9f5a528644e5c124092b69d43f1a50094`，
畫面`n6-history-light.png` hash `c8b724fb3c2d602925e516b59165dda510158340b02d7440293596f154c94ff2`。
後續main `177147e0`正常整合為`b2509098`，只解CURRENT摘要交會，保留新live來源、TN-1及真人批准卡片的優先順序；
八案原件hash `28d2e8c4a6efa5f2adec14ef51187b7bae620893f2d1ca33bbce94f828526bff`，不重跑上游整片或把它算成本線新實作。
後續權限與HTTP小delta分開驗，不把早期未接線PASS宣稱整片或覆寫RED；參照數以各JUnit為準，不相加成不同案例數。
斷線前已完成的原件保留；斷線後本批重新執行，沒有接續已被殺掉的pytest當成完成。

### Verified against（N6-3/4）

`native_agent_tools.py`原官方follow、`native_agent_relay.py`原材料與`native_agent_api.py`兩個收件點；
`web_test_console/responses.py/native.py/bridge.py/adapter.py/instructions.py/index.html`；
N6-2 `submit_native_turn`/Options/平台reader及N6-1a runtime是上游原件，不由此片另加模式/欄位。
測試core/responses2.2.0、invocations1.2.0、azure-core1.41.0環境唯讀重用，不升共用環境。
官方能力來源沿[N6-2](../src/fa-shared-context-poc/README.md#n6-2-兩步-api-與平台歷史local正式路由模式未啟用)
及[T5](../src/fa-shared-context-poc/README.md#n6-4t5-responses-後續送出-consumerlocal部分完成)：
官方提供SDK task/recovery/平台歷史；FA只接原scope/RID/Team入口與現有工作台，不新建queue或agent loop。
沒有Live/真模型/Foundry歷史保留期/任意exactly-once驗收；SP-22/WAKE-06另案。

## 2026-10-02：N6 T5 follow consumer（部分完成）

固定 `44773107b275c522143ff4754ce23c2389ad3b00`，已普通整合1a `0829adcd`
與0 `4869cab8`；VM／wire逐byte沿owner來源，沒有在consumer線重寫舊P1。
Hosted adapter 已能在B持久accepted後即還A，沿原官方 `fa-native-version-message-v1`
在needs_response/not_taken時繼續同一訊息；queued/started/terminal停止，wake同樣處理。
正常等待不扣失敗預算，HTTP/權限錯誤依原界線，排程unknown明示且不丟B已確認回條。
**API兩步producer／N6-2 shared helper、平台歷史及8765自動reconcile仍未接，未審／未部署。**

| 原件 | 結果與原因 |
|---|---|
| `n6-t5-accepted-follow-red.xml` | 1 RED：持久accepted回A後沒有任何follow，未開始的peer工作無人推進。 |
| `n6-t5-follow-cancel-recovery.xml` | 1 PASS／1 FAIL：開發中只await恢復sentinel但未return，官方task被取消而非保留重入。 |
| `n6-t5-follow-cancel-recovery-green.xml` | 檔名含green但實際仍1 PASS／1 FAIL：已回sentinel，冷恢復的Union input卻是dict；未假改PASS。 |
| `n6-t5-follow-cancel-recovery-typed-green.xml` | 2 PASS／3.897秒；沿正式DTO重新驗證恢復input，新follow不需再呼send。 |
| `n6-t5-follow-client-fixed.xml` | 23 PASS／14.114秒：即時ACK、超過10次正常等slot、wake、queued/started/terminal停、錯誤有限retry、scope拒絕、取消、新舊input恢復與legacy transport直接回歸。 |

固定23案SHA `a2fc011229990bf31ad2661177fe6c8ff05d74920af4e003a33bc32de7686ef5`。
真core/responses2.2.0、invocations1.2.0、azure-core1.41.0的官方local task；接收API是HTTP替身，
沒有真peer VM、模型或雲端。原單點/批次重疊不相加，前兩次recovery失敗確為開發中接線錯誤而非環境歸因。
API-only wire22案的整合驗證則只代表SDK-independent schema，不混成官方2.2 runtime測試。

**Verified against：** `native_agent_tools.py`原task/typedinput/停止條件，
`test_native_responses_relay.py`與原transport/version-follow/wake測項。
官方能力及有限重試缺口見[README](../src/fa-shared-context-poc/README.md#n6-4t5-responses-後續送出-consumerlocal部分完成)；
單key回條、root10、原messageID與sender來源由原producer持有，這批未修改。
其後正式1a #384合main `09ae3dff`，
本線普通整合為 `1d519faac540ef75bd9844a7eff65ff841a5e5f2`；VM/wire與正式main相同，
本線follow程式/測試與`44773107`相同，只解README/CURRENT文件交會。
直接join另23 Python案 PASS／10.634秒（follow＋wire＋local lane，**不是原先23案選取集合**），
`n6-consumer-released-1a-integration.xml` SHA `0e39df56dd2f33e4d970953faa929a7df29c9cd32730de2ca9c9415ef39f272f`；
UI相干10 Node案通過。1a正式交付解除該依賴，仍不能替代N6-2 producer/history介面或本片兩軸。

## 2026-10-02：N6 工作台本機準備（尚未接新協定）

基準 main `75696b6f`，固定本機準備碼 `3a214dc5353390a628dc5ecf99320a49b5237964`。
已擴充本機 journal 的 Responses lane 保存，保留舊 Table/native 命令與未知意圖；
原生畫面判讀同時辨識兩種 profile，不漏掉 Agent 來源的 CID 過濾、也不降低圖片 hash 驗證。
**新 sidecar profile、N6-1a wire、N6-2 HTTP/歷史、accepted自動核回及T5 follow尚未接通**。
不改新協定DTO、不猜conversation/previous_response_id、不升級共用環境，未審／未部署。

| 原件 | 範圍與結果 |
|---|---|
| `n6-console-lane-actual-red.xml` | 1 RED：已知新profile被原 `INSTRUCTION_ROUTE_CHANGED` 擋下；前一份 `n6-console-lane-red.xml` 是fixture漏created_at，另留而不充當產品RED。 |
| `n6-console-lane-fixed.xml` | 9 PASS／11.416秒：交易式擴充舊SQLite check、保留原Table/native pin/command/CID/UNKNOWN；既有未釘lane的命令/未知意圖不升級，新pin不受後來偏好影響。 |
| `n6-console-presentation-red.xml` | 1 PASS／2 RED：原native對照通過，新Responses缺原生狀態面板，另一CID的Agent訊息出現在工作選單。 |
| `n6-console-presentation-green.xml` | 15 Node案PASS：兩種native狀態、Responses Agent視圖隔離／圖片hash反例，以及原圖片／換版／未開始unknown呈現，沒有POST。 |

**Verified against：** `web_test_console/instructions.py`、`index.html`及上述public journal／呈現測項。
前端是合成state，不是平台Conversations歷史或真HTTP/SDK/Hosted；資料服務/套件未改。
N6-1a的RID/dispatch固定介面和N6-2共用兩步送出／平台分頁是必要後續依賴，不能用此批PASS代替；
79fa優先處理每SID task_id P1，N6-2/5暫停。對應TOOL-05/06僅本機查讀狀態契約，不升級全域驗收。
hash與未完成範圍見 [JSON](agent-runtime-evaluation-baseline.json) 的 `n6-console-local-preparation-20261002`。

## 2026-10-02：Recovery 啟動與草稿非同步測試修補（LOCAL）

固定 main `713b7d9b9784746a4bb30534952ebc7a234a62b6`（已含 #365），產品與測試修補
`0326e2b2b06a5c0e06a1925c7d6ed2452b1d0c89`。沒有 N6 `3a214dc5`／`1ad5133c`，
不重審上游、不跑整套 Python 或 271 個 Node 案；本節不變更任何全域評測或舊 FAIL。

- **Recovery：P2 正式獨立入口啟動錯誤。** API image 的 module 入口進
  `behavior_server_main.py:138–155`，`recovery` 選獨立 config 並以 Uvicorn factory 呼叫
  `behavior_server.create_recovery_app` → `recovery_application` → `ServerComposition`。
  刻意沒有 runtime／來源 Table，卻在建構時無條件寫 `runtime.agent_message_composition`。
  最小修補只保護這個合法的 `None`；不是改一般 serve／Hosted task 恢復，也沒有 live 故障或部署證據。
- **草稿：測試非同步競態，未發現需要修改的產品下載錯誤。** 原精確案初跑 PASS，
  同基準重複 60 次有 1 FAIL／59 PASS，錯在 `downloaded` 尚未建立就讀 `.text()`。
  只在診斷環境把真 SHA-256 延後四次事件迴圈即可重現同一錯誤；修後等待原 click handler
  的真正 Promise，包含負向與登入晚到，不用兩次 `setImmediate` 冒充下載已結束。

| 原件 | 結果 | SHA-256 |
|---|---|---|
| `recovery-baseline-red.xml` | 原精確案 1 FAIL，`AttributeError` | `edcaf043930b55713a3a7fad98b12dbf992e433dba47462dd926cd1bbd1fc72b` |
| `draft-baseline-stress.json` | 原精確案 60 次，1 FAIL／59 PASS | `7eb0be29544bb7ee1973fb7b7715afc1a62443e6c277eb72bbff547fb8c2435f` |
| `draft-delayed-digest-red.xml` | 原測項／expected 加受控延遲，1 FAIL | `d91beb6479c473b09af5272392dc47843abc7b17de301b536b82a08b1bb71144` |
| `recovery-consumers-green.xml` | 恢復、缺目錄拒絕、正常組裝、CAP-02 原接單／失回應六案 PASS | `33b6ae87230b5135852fcf18afdebb48ffda7a15438b3f5df284b593ade4245a` |
| `draft-consumers-green.xml` | `native skill draft` 六案 PASS | `e3f6f5880a61e25cd17e3b6dbb03b7407ebb848b17b010125f0e10dcf896b9af` |
| `draft-delayed-digest-green.xml` | 同一受控延遲下六案 PASS | `b588dca5812894ff25b5b10d2d8670fa62be49ed634d27631fc73ada217bb4c6` |

原件與精確 selectors 留在本工作階段，不進 Git；批次重疊不加總，不回填他線 270/271。
Python 是真 Azure SDK 物件配離線 HTTP transport；Node 是正式 HTML／真 WebCrypto 配合成 DOM／HTTP。
沒有 Azure 儲存、Hosted、真模型或現場 8765 操作；原 M3 bytes/hash／人審／Table/native 邊界不變。

**main 前進後的交會：** 正常整合已發布 `6d78bf5b`，只人工解 CURRENT 摘要衝突；
本片兩個程式／測試檔與 `627891ff` 完全相同。因上游已改 HTML，再跑同一組直接接點：
Python 六案 PASS（`recovery-merge-green.xml`，SHA `a87fd9b0c76a7070e0f4d7812a966765bc477e0ec4b9bdf7af214841da5f12d4`）；
Node 六案 PASS（`draft-merge-green.xml`，SHA `2ab5363d083c4e81e8818e96ced8b8af9e35338645f9d66274fdeb631c4ad8e2`）。
這是新 main 加本片修補的另批本機結果，不回填前批、不重跑上游矩陣。
新獨立 Claude Opus 5.5 Standards／Spec 均對固定 `713b7d9b..627891ff` 回報
**0 P1／0 P2**；上述正常整合沒有改變本片產品／測試差異，不重審上游。

### Verified against（Recovery／草稿修補）

`behavior_server_main.main`、`behavior_server_config.load_configuration`、
`behavior_server.ServerComposition/recovery_application/_lifespan`、
`behavior_api.create_source_recovery_app`，以及 `index.html` 的
`readNativeSkillDraft/downloadNativeSkillDraft/nativeSkillDraftPanel` 與原 Node 六案。
原 Node 等待邏輯參考 [Node 24.19 官方 mock.method 契約](https://github.com/nodejs/node/blob/v24.19.0/doc/api/test.md)；
使用預設原方法，沒有新增 FA 層、測試框架或治理 gate。

**正式交付：** #371於台灣2026-10-02 09:45:10
CLEAN/exact-head正常合併`b63186ae6315d05af408cebdde86d4c1b9007680`，final head
`ec22196219eb58182566b8313940c2e98ae56efa`。合併瞬間main另進#372設計文件，
故merge tree與head不全等；差異僅`docs/n6-responses-migration-design.md`，本片六檔逐一不變。
父線已核GitHub和八份JUnit原件hash，不重跑同一批或重審程式；原件留修補工作階段，未部署。

## 2026-10-02：C5 native 圖片原件與工作台（LOCAL）

固定 `72db7bc1ab502685601d1443a95af81e3608cec7`（parent為skillify正式main `c1f0e61b`）。
原C0圖片回呼→內容編址State store／小清單CAS→N1/N2 owner read→8765原PNG已接；
不造Job/binding、M3 image或公開URL。已交C6；#361兩軸清零並合併，尚未部署。

| 批次 | 原結果及界線 |
|---|---|
| `c5-native-image-first-red.xml` | 1 RED，缺native image provider/reader；後補原State store保存。 |
| `c5-native-image-storage-bounds.xml` | 前批7 PASS／1 FAIL；失敗在圖片操作前的N1 store初始化，Windows PermissionError errno13/winerror5，未改storage retry。 |
| `c5-native-image-max-separate.xml` | 同碼最大PNG另批1 PASS；實際512KiB全部讀回，含base64/來源的JSON小於1,000,000 bytes，不回填前批。 |
| `c5-native-image-api-red.xml`、`c5-native-image-console-red.xml` | 各1 RED：N2沒有images路由回404，8765仍走legacy gate回403；分別補原RID讀取分流。 |
| `c5-native-image-real-sdk-first.xml` | 2 PASS／19.548秒；Completions/Responses，真SDK與stdio MCP回呼、同圖不多存、下一RID沒有舊latest_image。模型/MCP結果為合成，沒有真browser。 |
| `c5-native-image-integrated.xml` | 固定產品相干43 PASS／41.231秒；含上述圖片行為、未選writer、四張/並行上限、空/偽參數/非法PNG、原scope/version/bytes拒絕、native/legacy工作台、wire冷匯入及其他providers。 |
| `c5-native-image-ui.xml` | Node17 PASS；native completed/unknown只明確查圖、bytes/hash/過多/缺清單/登入晚到拒絕，legacy四案及既有skillify六案保留。前兩個native按鈕案在接UI前均RED。 |

固定43案SHA `66c69e6176c99ef4825987e7e911872d91612ea76c11ca3934e607ec2d7406c4`；
Node原件SHA `8778db86d541b3e3f9ea86d7afdbf7edc1aec99fed5f3510763b37f14e46ea20`。
批次有重疊，不加總；原Windows FAIL不抹掉、不外推成平台原因或已修復。
保存／可下載不是人已收到，unknown只讀原件。未引用PNG不冒稱交付，原不可變資料不刪除。
另以真瀏覽器讀取隔離loopback的正式HTML與既有合成fixture，PNG可解碼（1×1）、預覽及下載區可見，
觀察到零POST；只有fixture favicon 404。原件 `c5-native-image-browser.json`
SHA `a3a669b9caa674cd00cd4bf81e477146e8050592ae7724811279966bd9a7117c`，
已關閉自己建立的tab並停止helper；不是現場8765或真Hosted圖片驗收。
**兩軸清零：** Claude Opus5.5 Standards／Spec都為0 P1／0 P2，固定
`c1f0e61b..f44507f3`；只審本圖片分片，未重跑C4/C6或改產品碼。
後續只有瀏覽器原件與review結果文件，產品及測試維持原清零版本。
**正式交付：** #361於台灣2026-10-02 06:06:28，在GitHub final head/CLEAN讀回後正常merge
`4fcc5821545bb8a99c214d6ae3bb51874c9a34ea`；final `9e0c9b661e87c2f9b62a74e7fd3cc63642c258e4`，
兩者tree皆`5cc3e960df9aeb97c31b4b2230ec51db04a160fd`。沒有admin/force／本線部署。
本線C4、skillify、圖片及native8765皆已交正式main；C6整合與live/A-B切換仍由原owner分開驗收。

### Verified against（C5 image）

`native_images.py`、`native_image_wire.py`、`native_sdk`正式factory、N1 dispatch/N2圖片路由，
以及`web_test_console/native.py/bridge.py/index.html`；測項
`test_native_images.py`、`test_native_image_api.py`、`test_web_test_console_native_images.py`及原gallery。
固定SDK1.0.13/runtime1.0.83與core2.1.0官方local backend；有三個真SDK案，其餘SDK/HTTP邊界有明示替身。
[官方能力與缺口](../src/fa-shared-context-poc/README.md#c5原生圖片保存與-8765-取圖local未部署)；
沒有真browser工具/Hosted/原8765/付費模型，只有上述隔離UI瀏覽器預覽；C6整組另行記錄。

## 2026-10-02：C5 native skillify 完整 M3 接線（LOCAL）

固定 code `f79219a03d4bb9f4e6a4220afc8afb98492211d0`（已整合 main `d8d2df4d`），
初次交 C6 時尚未兩軸／PR／部署；後續修正及清零見本節下方。範圍是原 RID 的 skillify consumer、server 來源核對、
M3 未批准 Team 草稿→原人工批准→C2 精確讀取，以及 native 8765 原件查看／下載。
較早「只存私人草稿」方向已被完整 M3 流程取代；圖片仍待下一片，不宣稱 C5 或 A/B 已全量完成。
對應 TOOL-01／03／06 的本機契約，不升級整個 Target State 或模型品質驗收。

| 批次 | 原結果與範圍 |
|---|---|
| `c5-native-m3-contracts.xml` | `933a62dc`：89 PASS／6.464 秒；native source 與原 M3/purpose/review/skills 相容，public create/upload 不接受假的 native source，舊 bytes/hash 保留。 |
| `c5-draft-reconcile-old-session-red.xml` | 1 RED／5.978 秒；原 Blob 已保存但失回應，native reconcile 還呼舊 SID 而失敗。 |
| `c5-draft-reconcile-old-session-green.xml` | 修後 3 PASS／7.220 秒；running／舊 SID 不可讀都只核原 M3 pending，不重傳，Agent write/owner read 仍分開。 |
| `c5-draft-real-sdk-first.xml` | 測試設定錯誤的 3 FAIL／28.401 秒：synthetic capture 是 Responses，但 model stub 回 Completions。不是產品 RED；未延長 timeout 或改產品取得 PASS。 |
| `c5-draft-real-sdk-green.xml` | 修正測試 wire API 後 3 PASS／13.196 秒；固定 SDK 1.0.13/runtime 1.0.83 的未選、同槽重複及保存回應遺失，原 RID 的模型用量與 callback 有核對。 |
| `c5-draft-main-integration.xml` | `f79219a0` merge WIP：45 PASS／1 FAIL／29.938 秒；C5 writer/proof/M3/JWT/8765、C1與候選 registry、N1 union、換版直接交會。失敗在 SDK 前 RID started 寫入，記錄 `PermissionError errno=13/winerror=5`。 |
| `c5-draft-main-unselected-reread.xml` | 同碼失敗測項另批 1 PASS／6.526 秒；沒有把上一批回填成 46 PASS，Windows 底層故障未歸因為 Hosted 或已修復。 |
| Node 相干選取 | skill draft 六案及換版／native 狀態交會，共十案 PASS；下載不同 bytes/hash、denied、登入晚到及錯 RID 都不產生成功檔案或 POST。 |

各批的 hash、selectors 與限制記在 [JSON](agent-runtime-evaluation-baseline.json) 的
`c5-native-skillify-20261002`；批次有重疊，不能相加成不同案例數。
M3 ArtifactService／HumanApprover／ApprovedSkillSource 與簽 JWT 路由是真程式、儲存為本機替身；
真 SDK 案的模型與 M3 HTTP response 仍是明示替身。不是 Azure Blob、Hosted、付費模型或 C6 全鏈結果。
保存回條的未批准只代表原寫入時，讀取狀態另反映 M3 現況；批准與 C2 選入均不要求原 RID/SID 存活。

**C6 另批完整 consumer 交會：** `7420cc4b190ebcafbc5396d399889bd78cfede4c`（包含本片 `f79219a0`）
加 test WIP `bf50efec93eceba2f72d79bbf7bb88755ffd8c549997f4cc4101fee072f722bb`，
兩案 PASS／23.144 秒。沿正式 factory/真 SDK→Agent token/N2 原 RID/effects proof→本片真 M3 writer→
原清單/正文→簽 JWT 真人指定版本批准→C2 另一 CID 讀取；正常與保存成功後失 HTTP 回應皆只 PUT 一次。
去掉舊 SID transport 後仍能核回，View=effective 與原 Receipt=unapproved 保持區別，legacy skill 不變。
原件 `c6-tracer-13.xml` SHA `10f7b8a123bb8bc0c13ed207e9516e777fae0810fa2eae38a348f61e4fe7c434`，
由 C6 保存，父線只核來源/hash，未重跑。這是 feature merge＋指定 test WIP，不是 main/final head、
Azure Blob 或真模型；C6 的前批 JWT fixture 失敗獨立保留。

**兩軸同一 P2 已修並清零。** 固定 `10fb4aff` 的 Standards／Spec 各 0 P1／1 P2，
都是 C2 目錄攜完整來源，四個普通長度用途超過 4096 B、連 Team 選項／onboarding 也失敗。
真正 RED `c5-skillify-catalog-p2-actual-red.xml` 為四筆179-byte用途回413；
短引用後最大512-byte用途仍會在選項的四筆合組處卡住（`c5-skillify-options-p2-actual-red.xml`，
1 PASS／1 FAIL），故選項改逐項取得同一精確ref，遠端每筆讀取數不增，模型選入上限不放寬。
`c4f70b222e47c14f4a447e655602986b9ab1f853` 的直接21案 PASS／20.566秒，
`c5-skillify-p2-fixed.xml` SHA `9eab45b29f436a4e769ad776c503065c9add0039e84d8c994029b4ae6654c8f0`。
原M3來源/批准不改；catalog只攜原submit digest，不能拿不同來源ref讀取。早先用途長度算錯、
legacy fixture沒有Agent GUID的測試失敗各自保留，不作產品RED。#359主線普通整合後三個JWT/核回
交會 PASS（`c5-skillify-source-version-integration.xml`），沒有重審或重跑C4/C6。
兩個原 Opus5.5 軸只複核 `4abf507a..c4f70b22` 五檔修正，Standards／Spec 都是 0 P1／0 P2。
四個極長用途仍可能超過原4KiB選入上限而明拒，不截字、不擋Team選項；未把這個保留的界線算成修補失敗。
**正式交付：** #358於台灣2026-10-02 05:37:07在CLEAN/exact-head下正常merge
`c1f0e61b2d526acaf48711936e59b6f583707f25`，final head `29b485c606c7a4cbe4edeaca43dd6b0cc95531fa`，
兩者tree皆`45d86fd56e021aec815edab38dda8adef837a704`；沒有admin/force或本線部署。

### Verified against（C5）

`native_skill_draft.py` 的正式 provider/effects、`native_skill_draft_api.py` 的真 Agent/N2 intake
及 M3-only reconcile/read、`native_skill_draft_wire.py`、`m3_models/artifacts/skills.py`、
`native_sdk` registry、N1 dispatch/proof、`web_test_console/native.py/bridge.py/index.html`。
新增 source 與 API/SDK/UI 測項位於 `tests/shared_context/test_native_artifact_source.py`、
`test_native_skill_draft*.py`、`test_web_test_console_native.py` 及 Node workbench。
官方能力依據與明確缺口見 [C5 README](../src/fa-shared-context-poc/README.md#c5原生-skillify-草稿與-m3-批准local未部署)；
不另建 agent loop、fake Job、artifact 平台或部署 gate。

## 2026-10-02：C4 原生 RID 的 SDK 同程序委派（開發中）

沿C0已發布early Interface `cf619329`，不造Job／claim／新queue。共用SDK事件Adapter
`a62d2cbb`保留legacy接點；完整child結果codec `6ab1026a`保持原儲存bytes與界線。
原effect觀測接點`dcedf603`由71c7確認可供C0正常整合，沿同一effects item保存取消後的原child觀測，
不核新dispatch權，也不能把cached/foreign/stale claim當成fresh。

`native_delegation` consumer已接C0真provider registry，只有原capture選入才啟用；目前未審／未部署。
開發前批固定SDK1.0.13／runtime1.0.83＋official local State store的7個案例PASS（32.774秒），
`c4-native-current.xml` SHA `5037c94f0a28c3af36131c39285d6c683d0040ee33e46bd289fee28a4cd99f93`：
選入後原RID擁有child結果與合併用量、未選不註冊task、同RID第二task拒絕、
mode/model覆寫拒絕、prepare讀回不明不重派、空child不成為父成功結果。
正常主子合計3次請求、4500 input／300 output只留一份原RID用量；模型I/O均為合成，不是真品質。

真取消案例另為**1 RED**（6.606秒，`c4-native-cancel-red.xml`，
SHA `c9c98898062d014264f667b45e3f9a0e45b81751e3797f7203e5b2bbb202aa64`）：
child cancelled與parent idle已收，原RID轉unknown；但C0尚未呼request_cancel，所以cancel_requested=false。
等C0 owner補原abort ordering後再驗，不以abort ACK宣稱停止，不為通過更改觀察值。
同effects/C0組裝20案PASS與legacy ledger/hooks/schema43案PASS分別另存，不加總成C4完整驗收。
這一前批尚未註冊factory；後續已補實際registry，詳見下方固定驗證。C4仍須等C0合main才合併，C5尚未開始。
後續已普通整合C0固定取消hook `7fd36ff5`至`d52a6a20`，真SDK取消案例及selected/unselected
共3案PASS（14.124秒），`c4-c0-cancel-integration.xml` SHA
`43ef6fc576a81fe37622201b5712094938bd23bb829e31df9ea99d65d0c9b2e8`。
request_cancel與session.abort沿C0同一watcher並行，不阻塞真正abort，不以ACK取代child/parent事件。

**固定CODE驗證：** `71011f88162c471c7a8d8a617666361871b5489e`以C0 `1fb8f0da`為依賴，
28案PASS、0 fail/error/skip，46.988秒；`c4-fixed-71011f88.xml` SHA
`059981c048f762ca989c77cb8c0d98138d90633a1baad9fe4bf7da4bdb824c7b`。
其中10個是真SDK的C4案例，Completions／Responses都沿父RID合計主子用量，只有被選入才註冊task；
第二回合冷恢復不重派前一child，原效果不被新RID覆寫。另18案核共用SDK事件Adapter的舊路徑。
原native constructor/registry缺口各有RED；prepare未知只發原第一次模型請求，不為取得成功結果重派。
不完整child的明確原因由native adapter保留為DELEGATION_INCOMPLETE，父回合仍unknown。
Hosted匯入刻意禁止legacy delegation ledger、Job/worker/server/Table仍通過，沒有增加API套件依賴。
本次用模型script與官方local State store，不代表真模型委派品質或Hosted；
C0的effect觀測共用層由C0原兩軸驗，C4只需審新增consumer/model/child delta。

**C4兩軸已清零。** 固定比較基準`1fb8f0da`→head `f4b232e2`，
Claude Opus5.5 Standards與Spec均0 P1／0 P2。兩軸沒有重跑原28案／43案，
只核C4完整diff、原件與冷匯入，不重審C0共享層。PR實際base_ref仍為main；
此SHA只是審查比較基準，不把C0尚未合併說成已在main。
C0 #352及獨立CAS修正合main後，才正常整合、核剩餘C4delta並合#354。
本機通過及review清零不是部署；C5仍按原順序接續。

**CAS主線整合已核：** main `fe55cedf`的最終CAS P1/P2修正已由C0 `cb8970c4`帶入，
本線普通整合為`aa1bdfef`。C4所有產品與測試檔對照原清零head `f4b232e2`逐檔不變。
只跑成功／冷續、取消與prepare未知3個真SDK交會案例，3 PASS／22.768秒，
`c4-cas-main-integration.xml` SHA `891d8bc68fc9ae8218ffe216de47c16753c97a9204c82a091ee280a32d2dccc5`。
仍須C0實際合main才合C4；沒有因等待而重跑完整套件或重審上游。

**C0前置已解除：** #352於台灣2026-10-02 03:32:36合併
`4b4a24c1e314da5a9092c165c13ac0588cba5d7f`，merge tree和已整合的`cb8970c4`完全相同。
C4已正常整合該main；產品及測試逐檔仍等於原兩軸清零head，
PR實際diff只剩C4本片，不含C0額外產品改動。沒有為純Git整合再跑同樣的全套或重審。
本機／兩軸／前置均完成，仍要GitHub CLEAN才正常合併，不能將此寫成live已採用。

**C4已交付：** #354於台灣2026-10-02 03:36:03，
兩軸清零且C0/CAS已在main後，以CLEAN／exact-head正常merge
`f9b40824c2af8cdff3053ad89cbb6681fdba6f95`。parents為C0 `4b4a24c1`和final head `40871e59`，
tree `f6a64be86fd5cfb3f570bb42fae215989030c42c`與final head相同，已讀回main包含關係。
無admin／force／直接push main，未部署或重送live。已交71c7作C6整合；
C5按原批准接skillify未批准草稿及image保存／native8765取圖，memory候選仍由原線負責。

## 2026-10-02：T5 native 收件者提示修補

基準main `8047ebbf1551a6831825cbe43481e53e1abe7ff1`加本次產品／測試差異；未疊父線C4／C0 WIP。
完成以下驗證後，僅整合main `4fd73bc2`已發布的部署文件／帳本；產品及測試檔未變，不重跑等價測試。
**初次本機74案PASS，0 fail/error/skip，57.606秒**；沿用既有SDK1.0.13／core2.1.0環境及短的本次專用TEMP，
沒有重建環境、安裝套件或執行live。API／Foundry回應為離線替身，真SDK的模型transport也是scripted provider。

先RED再修補三個接點：模型實收schema缺名稱（1 fail）、receiver preflight沒有獨立分類
（2 fail／2 sender反例pass）、經HTTP masking後模型仍只見`Authority refused`（1 fail）。
GREEN後補上精確ID／同名資料與空native cache不過濾的反例。
最後範圍如下，JUnit原件含完整參數化測項名稱：

```powershell
python -X utf8 -m pytest tests\shared_context\test_native_agent_tools.py `
  tests\shared_context\test_native_agent_api.py tests\shared_context\test_native_agent_transport.py `
  tests\shared_context\test_agent_message_tools.py `
  tests\shared_context\test_native_agent_client.py::test_quota_uncertainty_is_not_reported_as_a_send_refusal_and_has_no_retry -q
```

8案native tools／16案native API／4案transport／45案legacy tools／1案quota unknown：
真`NativeSdkExecutor → run_copilot`的模型請求保有全部名稱／ID對應、Unicode及相似名；
固定tool feedback分清收件者無法接收、sender拒絕與unknown，沒有洩漏upstream任意文字。
原同ID已被接收但ACK遺失後，再讀回403的案例保留先前回條與名額，不新增投遞、不改原件。
沒有新Foundry目錄查詢；UUID enum、handler與原resolver保持精確ID權威。
固定SDK官方來源及已讀程式見[README](../src/fa-shared-context-poc/README.md#verified-against2026-10-02-t5-收件者提示)。

原件位於本次session的`files`，SHA-256：

| 原件 | SHA-256 |
|---|---|
| `recipient-names-red.xml` | `4a8ef97375883aac0a181a63022cc74a928cf38901f3902f61532edb9ecf4286` |
| `recipient-preflight-red.xml` | `3f485c6bd82ee784018950e5ecb6bb0e3c88499fa3f29027d69ad028bbdceac1` |
| `recipient-feedback-red.xml` | `8949d8dca9fde95c1386a08e246440b1217d3c6d4e5c22d6e2f7c28553c4537d` |
| `native-recipient-targeted.xml` | `a64bb2198b4e60176f6066e408015616925146c3e842b8e87ed0028780ed5658` |
| `native-recipient-targeted.txt` | `ebe41f9e321a24ab2f199484cfc02be2344d9d18f08c0d7abb6ac2ae5a3b4eb5` |

**Standards P2修補。** exact head `e2fa41399b1eb6020c316b2b946fec313de6213c`初審：
Standards 0 P1／1 P2、Spec 0 P1／0 P2。正式`behavior_api._install_error_handlers`會將新code遮為
`BEHAVIOR_ACCESS_DENIED`／`BEHAVIOR_CONFLICT`，初次gateway及固定HTTP回覆測試未涵蓋此接點。
補上真`relay_http` app（含原錯誤處理）→Hosted client→模型固定提示的回歸，
4個收件端案例RED、sender及lost-ACK兩個反例PASS；僅在`/send`直接回單一安全code與固定訊息後，
**7案PASS，13.445秒**（6個新參數化案例＋原signed HTTP權限案例）。
不重跑未改的SDK／模型工具套件，兩批不相加為81個不同案例；修後只交原Standards軸窄複核。

```powershell
python -X utf8 -m pytest `
  tests\shared_context\test_native_agent_api.py::test_native_recipient_feedback_survives_the_real_api_error_handler `
  tests\shared_context\test_native_agent_api.py::test_signed_agent_http_entry_never_accepts_a_human_or_model_supplied_authority -q
```

| P2原件 | SHA-256 |
|---|---|
| `recipient-http-red.xml` | `0b35af3bc5569d65c66eabf5878e7d1e89cc7e7affc556c518a92f447b08edfc` |
| `recipient-http-green.xml` | `474e335570e91679de00fb8dde2089f982aab418c0a2a31aec6fca9c5c752af3` |
| `recipient-http-green.txt` | `0c02bdc88234a79d8c19a5363517d7d8420ef4f6fec8b540346474e141b771b5` |

首次HTTP測試的事件迴圈建立被fixture外部I/O封鎖，6案失敗；改為和既有fixture一樣先建迴圈才載入app。
該次只屬測試配置失敗，另存`recipient-http-harness-failed.xml`（SHA-256
`813ead8013883d9d9d1d37fd317a65fe54b230175435ce45c9622e686a7a594e`），不算產品RED或聲稱pre-existing。

**原live FAIL保留。** 依fdbb／總協調回報，13cc批次test agent 2→3的模型選到舊test agent，
原收件端讀回403／`NATIVE_TURNS_DISABLED`，這次止於reserve／接收invoke前。
原RID `text-b3bb009acb52e592c0a51d17f74b4917`不重送；原`d26t5-send-intent.json`
SHA-256為`aebdcf0dcde12d6860636be975fec7fbf3dd34cea0df870b5d7cc458c3970b59`。
本次只讀必要metadata，不重判歷史同ID或舊UNKNOWN，不回填PASS。
scripted provider的選人不是真模型品質證據；未跑新smoke、prepare、reset、cancel或雲端寫入，後續由總協調另排。

## 2026-10-01：Native T5 收件與原CID結果喚醒

固定實作`203ceb34dbda3634344acb90386682c5cf3d6dbd`，**88個相干案例PASS，0 fail/error/skip，55.861秒**。
API／registry／Table使用明示離線替身，State store／queue是core2.1.0官方local backend；
terminal交辦正反兩案使用真本機SDK1.0.13／runtime1.0.83及離線model transport，不是付費模型／Azure驗收。
各先前RED與增量批次分開，不相加成模型或live樣本。

**I收件：** verified Agent token／原CID-RID→自己的source，receiver用#323 resolver並核ready；
真onboarding流程產生的registry Agent可收，取其原角色文字與設定，不只測config B。
N1 `create_item`先保存原RID才排隊；同ID相同材料只一個接單，不同材料409；
busy時只accepted／queued，前一回合不被取消，空下來後依序各跑一次。
真SDK成功terminal送出只一個model request、一個send、零poll，保存固定「已交給…」。
失回條那案只送一次，模型再一輪說明未確認，不冒稱對方未收到或已處理。

**W結果：** B的completed原件先持久保存，再用原request message ID作A wake RID；
重複wake在A原CID只開一個新回合，不重用舊A execution。完成hook失ACK後重入只補同一次通知，
B的SDK仍一次，read/history不呼hook。大結果保存完整引用，A可按原引用分頁讀，不截成假完整答案。
API核對原command/capture/source/CID/RID/Agent/SID，四個有效但錯配回條先RED後拒絕。

**限額：** 按Neal18:46／18:47最後決定，native不再限制2hop或額外每turn100；
一處常數`MAX_NATIVE_RELAY_MESSAGES=10`。Team root item只有ID/hash，10筆1785 bytes、上限4096，
不存內容／result／回條；獨立quota partition不進舊authority inventory。
第11則拒絕、同ID不同hash拒絕、同ID重入不加次數、最後一格併發不超額；
CAS ACK遺失只讀回，不在同一次呼叫再次寫入。每request唯一result交A自己的State store，不在quota另加reply狀態。

**原語及缺口：** State store、官方chain、SDK terminal tools和現有TableRepository的If-Match均直接沿用；
FA只補Team/sender/原request-result關聯與root10這組業務規則，不新建queue、scheduler或agent loop。
官方連結及1785-byte量測見[README Native T5](../src/fa-shared-context-poc/README.md#native-t5持久接收立即交辦與原cid結果喚醒local)。
新路徑仍由`FA_NATIVE_TURNS=foundry-native-v1`明選，不碰舊CID／Table／B23；
FYI／無真native停止引用的server_notice不接；review修正新增agent_notice引用原未確認回條，不宣稱B已停止。

原件`files/t5-fixed-203ceb34.xml` SHA-256
`e4e259de48783ab01a25b199558d946805f2b6a7a5569856cb9224e6183b2838`。
整合N2正式main `bd957654`至`f6b593df`後，10個直接相交案例另通過（15.936秒）；
原件`t5-main-bd957.xml` SHA-256 `77b451910554e130a9cb2d39be1f8beb82a5be9c8c3997eef6f2d5e4799d048f`。
Hosted工具在明確禁止載入`azure.data`及API composition/work-policy的程序中可匯入，未替Hosted加Table套件。
測試開發時更正過JSON列表須走wire JSON reader、真SDK要用真runtime fixture，
以及Windows graceful stop不能強行要求exit0；這些測試設定更正不算產品RED。
初審固定`736ca29d`：Standards 1 P1／1 P2；Spec 1 P1／2 P2，共用P1為通知缺少正式自動恢復。
以下是固定`a066d646`修正，尚待原兩軸窄複核，尚未部署：

- 通知改官方單次task＋RetryPolicy（固定core2.1.0預設3次），用量失敗不擋wake、不重跑SDK。
  queue滿時才交官方單次task延後同RID；耗盡明示busy_refused／failure code並通知原請求方。
  通知本身耗盡以原回條notification_error明示，原結果／usage不改；不把Task回條說成外部效果exactly-once。
- 目錄暫失只停當輪send工具並明示限制，已接單回合仍執行。
- 原unknown／busy_refused沿同wake ID送agent_notice，內容只說未確認，不捏造已停止／沒有外部效果。
- delegation／continuation持久連回原請求；B「已交C」不占A唯一result，C真答案經B新回合再回A原CID。

12個重點合約案例通過（23.124秒），`t5-review-fixes-contract.xml` SHA
`5c518391e07aad3a652f1390ef714c91239e6dee06cbd37937d6a8e5eb4bc893`。
固定a066擴大99案為**98 PASS／1 FAIL**（58.725秒），`t5-a066d646.xml` SHA
`4a58f41c4e157bf2eb361a0e120574122fe449d1ee63ef43d3ab9e1f16b82eb4`。
失敗在`test_terminal_usage_is_sent_without_user_poll_and_reuses_original_report[wrong_rid]`的checkpoint階段，
記錄為SDK_STATE_UPLOAD_UNKNOWN、底層原因未保留；不是callback assertion本身。
同案單獨重跑1 PASS（5.017秒），`t5-wrong-rid-isolated.xml` SHA
`b5a524957c39d183e0d45f95d57c16d55e26255be9c4b746e09fa7c100198914`；不能合寫成同批99案全綠。

已唯讀比對N1獨立官方local backend原件`n1-sdk-storage-probe28.log`
（1089 bytes，SHA `7d44358103cbd47772ffacbdabdb059f4171cb14c2e47c568cb245745e49457e`）：
completed=155後在`storage/_local_state.py:291 os.replace`有PermissionError errno13／winerror5。
它不是本次wrong_rid的trace；另兩筆測試曾記相同PermissionError數值，也不能代替本筆缺少的例外鏈。
因此**本筆底層歸因仍未確認**，不寫成PR回歸或已知基準原因，也沒有增加storage retry。
live後仍須新CID的A→B和A→onboarded Agent分別驗。

**第二輪原軸結果與修正：** 固定`174920b5`的Standards已0 P1／0 P2（直接20案PASS，36.19秒）；
Spec為0 P1／2 P2：3秒retry窗口不足／root失敗不可見，以及子Agent已接受後取消時交辦記錄未保存。
整合已清零prepare #338／main `9dde56e1`至固定`2e6973e0`，不隱式建立或改SID。
修正為官方10次delivery retry，1秒起退避、最多60秒；真人原對話可見peer通知錯誤，
原reply/usage保持。原started RID在子Agent真ACK時就保存delegation，不等SDK或checkpoint結束；
取消、shutdown及存檔失敗不先占掉上游唯一result。

8個直接案例全部通過（47.473秒），包含**真10秒API中斷／queue滿後無人重送自動恢復**，
交辦後取消仍由C→B→A交付、root與child通知錯誤的真人投影及prepare接點；
`t5-review-round2.xml` SHA `94da6a16b9918d26fdc9c71bd6aaf6da8a0bd5016a1f71aafc1cfb625590cbff`。
另Node工作台測項`native notification exhaustion is visible without replacing the saved reply`通過，
確認新增錯誤會重新繪製、展開顯示且不更動原答案或POST操作。

固定`2e6973e0`106個相干測項（兩個已跑真延遲案不重複）為105 PASS／1 FAIL，69.683秒；
`t5-2e6973e0.xml` SHA `8bdf060255f8ed7e853e74618a4a1c48f09903a14d9e869e353ae488424b7c93`。
失敗case停在accepted、未進入SDK，因此沒有走到交辦／取消接點；local task原件顯示suspended/run_completion，
底層原因沒有對應例外鏈，不假定和PermissionError同因。該case單跑1 PASS／2.605秒，
`t5-r2-cancel-isolated.xml` SHA `7c8721a43f1ca6428a7854dd815851de9f4234688b017b5692e35b68461188f5`。
另先前108案的單一失敗是測試改用短等待後仍假設必能看到首次POST的同步時點，已改成等實際自動完成；
不修改產品delay來讓測試通過。仍不合寫成同批全綠；最後兩項由原Spec續核。

**第三輪Spec仍0 P1／2 P2，最後窄路徑修正：** 固定`eecfcd41`主要情境已核通，
剩延期SID讀取／chain.start的瞬斷未轉retry，及retain暫失誤報工具失敗。
`f328a498f2be63f5766e9b17cd681fee624fc814`只補這兩處：
延期5xx／AzureError轉原官方retry，耗盡把原accepted改unknown並通知；C已收到的真ACK
維持成功，retain不明記log與固定提示，取消／SDK失敗的outcome保留原delegation，不重送。
快時鐘測試的retry_on已改成和production完全相同，不能藉retry所有Exception掩蓋問題。

兩個延期案例先RED（`t5-enqueue-predicate-red.xml` SHA
`c7b40a64d26fba4db1b0e882155c88b42b8347568a4606fdc84a92171ced53d8`），
兩個retain案例先RED（`t5-retain-interrupted-red.xml` SHA
`6b6fc037b9e9276dce4a4da949f73d2e0ca3124b7c92a676bf6fa25100170cef`）。
固定f328的5個必要案例PASS／8.584秒，
`t5-f328a498-critical.xml` SHA `2dfd229492b7c96ab29b4aaaf1cccc69070ff4ea9c0c1c174b46df5a524afdf7`。
相鄰25案24 PASS／1 FAIL（`t5-r4-targeted.xml` SHA
`aa204c22425a41407d18f448dfa68e1548ce3311f1512fe3312a5caea93be509`）：
該筆directory回歸在local儲存記明PermissionError errno13／winerror5後變成NATIVE_STATE_STORE_UNKNOWN；
它和本次最早缺少例外鏈的SDK_STATE_UPLOAD_UNKNOWN分開，不互相外推。
同案單跑1 PASS／2.615秒（`t5-r4-directory-isolated.xml` SHA
`bd41e57d2524ae01ba47927d614dd7baefbbf59a2cefdc21097e215d38d84ff7`），沒有增加storage retry。

**原兩軸已清零。** Standards在`174920b5`原軸delta為0 P1／0 P2；
Spec最後固定`ff6a788f`（code f328）為0 P1／0 P2，直接7案PASS／8.78秒，
並以原官方local probe核排滿後一次State store 503會自動恢復、wake完成且SDK僅一次。
沒有重跑完整兩軸，也不把回條自洽或人工replay當自動恢復證據。

之後普通整合已審main `3aaf1e9a`（#343可寫root），commit `f81c3e12`、
tree `c5b43d80f0f5861a2dec3d0b89de74742eb0e1b9`。T5核心無產品delta；
native factory只採上游已審的一行root修正。交會7案6 PASS／1長路徑熱快取assert失敗
（worktree下期望JSON路徑286字、暫存檔297字；work/sdk與權威store檢查均通過）：
`t5-main-3aaf1e9a.xml` SHA `e1527e125bd1bb9e4529b2b928ab22597ddeb7f96d9da2e847c31fb2441ce725`。
同一factory案例改用已檢查的短TEMP目錄1 PASS／1.982秒：
`t5-root-short-path.xml` SHA `1fcd18bbf9f610f2bee7bd398bc0155d208b2a2ec68b2576c18b24dbbb0178c8`。
未為此更改產品、權限或storage retry；兩次執行分開記錄。Node原生工作台4案另通過。
新viewer's vendor／LICENSE／AUTHORS完整沿上游，不修其既有空白。
合併及本機結果不是live成功；部署與新版本／新RID驗收仍交總協調／fdbb另排。

**已交付：** #332於台灣2026-10-02 00:57:03，
在兩軸清零及GitHub CLEAN／MERGEABLE後普通exact-head合併為
`13cc1156e95aacf490ad2d1b6d55c8e4af4913e8`；parents為main `3aaf1e9a`與final head `76dfa76d`，
merge tree `d343551978e2428153b6877892e0f80c1986c2c0`已讀回，無admin／force／直接push main。
已通知總協調及fdbb。**本批live安排只做新onboarding的native test agent 2→test agent 3一次**；
A／B不在本批切native，A→B等79fa的native換版接續上線後再排，不能用prepare替已接工作偷換版本。
舊CID、B23、UNKNOWN與兩筆過期queued保持。此紀錄不宣稱live smoke已執行或通過。

## 2026-10-01：已驗停止 peer 的原回條收尾

固定修補 `dadfc7b415e47124874d83046db50ecd549f4f05`只處理舊CAP-02回條相容，不新增收件匣或排程。
原停止／永久claim-gate fence能把job收成cancelled，但沒有SDK結果時，peer轉送找不到結果列，
回`OPERATION_RECORD_MISSING`，原queue與receiver因而無法前進。RED原件一案鎖住此情況。
修補以`stop_evidence_sha256`引用原matching completion，保留cancelled、沒有SDK答案／成功結果，
保存唯一固定blocked，再沿原ack／forwarded更新；原來源、A回覆、未知副作用與費用都不變。

第一個16案相干批次14 PASS／2 FAIL，兩個unclaimed案例揭露recovery history還只接受version-refusal結果；
同步修正該reader後，4個claimed/unclaimed與後續A active/ended組合皆通過。
最後再選6案（上述4案＋舊版本拒絕＋未開始取消）全部通過，40.268秒，不與前批相加。
這些走原native claim/fence/worker/message/reader，平台與儲存為合成I/O，沒有Azure、SDK或真模型執行。
已結束的後續A不會被強行喚醒／重派B；late claim被原fence拒絕，重讀不多送blocked或重跑原job。

限定操作函式也以合成ID做read-only預覽與Table-only apply，1案通過（11.290秒）；
CLI的真config／MI／雲端資料尚未執行。開發時發現腳本誤用API投影欄位代替ledger view，
已在本機改正讀回，沒有把這次失敗帶到live。
整合main `7d9dd97a`至`08d860f6`後，原未claimed且後續A已結束的案例另1案通過（13.672秒）；
`b23-closure-main-7d9.xml` SHA-256 `310e3b070c7f2824fdd15f25649922c46cac10c316e4467429598dea8d42139f`，不相加為不同樣本。
fdbb已唯讀核回B23：post_unknown、從未native claim、無Assistant Session/acquire/保存結果、cancel_requested=true。
因此本次批准走**Table-only未claimed fence**，不停止Foundry session；必須先採用含新reader的同build，
再依原job/launch/session/version重核。scope變動、有保存結果或late claim即停止，不以idle當作停止證明。

原件索引：
- `b23-stopped-receiver-red2.xml`（1 RED）：`4685d1e0bed2dc167fbdce97599d6ef5af144e34c9e73f5b051f96ae8a1ae9e3`。
- `b23-closure-final.xml`（6 PASS）：`bb189fc31fff2f3966eba19ace5dd193d2f55814929f2884d8d0d09a45fa1de5`。
- `b23-operator-local-green.xml`（1 PASS）：`afaa72132285b7fdf13672aec65aceddbebf74a04d296dc8bbea3eae7a057ff4`。
- 限定腳本`b23_close_unclaimed.py`：`4aa7be8e3bab1e4cf4e21d57eda779744b008599fd5d336a86a0e3e4fb25bef1`；
  其只讀依賴`b23_read_original_state.py`：`a3532375b27707cf3aa8be3dc3cd046f538a01364f824f863bc43ace352ae5b9`。

Opus5.5固定`37b8e670`：**Standards 0 P1／0 P2；Spec 0 P1／0 P2**。
Standards另跑6個相干selector通過（33.29秒），不與前批相加；Spec以dump確認None欄位不改舊bytes。
操作函式1案通過仍屬`dadfc7b4`；main #309將選版改為stub，舊ENV版測試準備步驟不再符合，
不把它寫成新head的PASS，也不因此加部署gate。
操作檔移除錯型的azure-core transport placeholder，改用httpx.MockTransport對應invocation，
並明說session API是apply路徑不呼叫、不是被transport攔截。`close_original`函式未變；
原Spec僅複核此型別delta後0/0。實際交付腳本SHA：
`f078fbe1cdbb298751c5bd12c305d863c4fbce34d23922a9e8c1c6c09251d6a3`。
審後整合main `0592b435`至`2508b7f2`只解文件衝突，已審程式／測試未變；
未claimed且後續A已結束的相交案例另1 PASS（13.305秒），
`b23-closure-main-0592.xml` SHA-256 `fce6d7d981ce770bf8ad7bd579a6aeeafca7574b2d56b85384231a7c65b2926b`。

**官方來源與缺口：** [Hosted管理](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent)
說明平台管理compute，不能以idle推定FA原工作已封口；[Table EGT](https://learn.microsoft.com/rest/api/storageservices/performing-entity-group-transactions)
提供同partition原子changeset／If-Match。官方API不解讀這批舊FA queue與worker回條，
所以只補原證明到舊blocked的最小adapter，不以新State store重建／搬改舊unknown。
兩軸已清零；限定live執行尚待新reader採用，本節不是live已釋放receiver的宣稱。
#314已於台灣17:35:21正常合併`172df737`，
head `878225ca`。新build與限定腳本已交fdbb；目前等待實際執行回條，不以合併或交接宣稱B已釋放。

**後續live收尾：** fdbb在`341dc110`執行限定腳本一次，18:21:57原件`b23-apply-result.json`
SHA-256 `35e831723d5e5a5e40abcd5fd66b60670a4033d0fc20c03c57a22ce93443aa10`已讀回核對：
job為cancelled、result_forwarded=true、原訊息completed／consumed；未stop provider、未產生SDK結果、未另派工作。
後兩筆queued唯讀原件`b23-later-queue-read.json` SHA-256
`22188330c506eb4c3502fa236b07d4619346e4de87511c379c5a8d6a37bee108`顯示兩個deadline已過、B job都不存在。
依總協調決定保持原狀，不重送、換ID、延長期限、取消第二筆A或替舊lane寫新程式。

## 2026-10-01：B23 首次 claim 沿用 root origin

固定實作 `c988bf01659f0bb632d426521bca7a774fe2b39c`，基準`edae6d28`。
**19個相干案例PASS，0 fail/error/skip，25.005秒**；含真signed HTTP／原native claim回條，
Storage／平台是離線I/O替身，沒有SDK執行檔、模型或Azure／8765操作。
第一次B claim內`read_inventory`由1次降成0次，取得`claim_state=fresh`；不是增加timeout或重試。

測試先在`h.legacy()/h.activate()`後、合成standing rows之前，真呼叫root.installation取得合法genesis origin，
沒有stub `installed_origin`或手填truthy cache；B經真`application(peer)`、原身分及claim入口。
standing-history的舊handoff hash是明示合成setup，故不宣稱完整歷史golden驗收。
RED鎖的是首次claim重掃inventory；之後遇到合成history的`ROTATION_HISTORY_INVALID`不是這次live錯誤。
既有完整歷史fixture所需`test_channel_window`缺檔，單項試跑skip，沒有重新安裝／建立venv。

其餘案例核root仍全驗證、錯誤後清cache、不同程序/config/store/fence或缺記錄／truthy flag拒絕，
以及保留實際fence與atomic probe短讀；改變原安裝列或probe列仍拒絕，自己的policy／target不變。
缺證據的舊測試在基準`edae6d28`也因既有1個GET失敗，僅校正「不得寫入／初始化」的斷言，
不為測試改產品fallback。新fault fixture的`_read`參數一度漏掉既有第三參數，已修正後重跑19案。

原件`b23-origin-qualified.xml` SHA-256 `7fdaac342eefa150d062fc6a02e38e9237a6c53708c61065c43d347ef590fc5f`；
RED原件`b23-origin-red.xml` SHA-256 `96abb3d7205a2b5dda16251e7270f8d0be94a0804358e50a9324520d153839c6`。
整合main `e0ece806`至`d1207152`後，真mounted app／首次B claim的一個相交案例另通過（7.594秒），
原件`b23-origin-main-e0ec.xml` SHA-256 `0db796e3ea36ec3b65fb85516b6d417b47ba7056397b00202d2ef2ad4f4871c2`；
不與前19案相加成不同樣本。
Opus5.5固定`029dc84b`：**Standards 0 P1／0 P2；Spec 0 P1／0 P2**。
Standards另跑原peer app讀取失敗／重建1案通過；Spec唯讀核對，沒有重跑測試。
Standards指出README新小節吃到舊內容的非阻擋結構問題，已補舊段標題，未改產品。
合併時main又前進到`09fe8e05`；只解spec／CURRENT文字衝突，B origin程式及測試完全未變。
整合`404bdb44`另選first claim／root全驗證2案通過（6.940秒），
`b23-origin-main-09fe.xml` SHA-256 `5dcaf953fab03016ebb61d812d5b2f33134d83d97e72057e714dc9c231440d49`；
不重跑兩軸或把各批相加。
B23原unknown及工作不變。live時間線支持此阻塞路徑，但原boot是否因412失去peer app快取仍未證實；
不以本機成功倒填原live結果。前一支#289已合`edae6d28`，
兩支交付後才由部署線安排同build採用與新回合測試。

**Verified against：** `behavior_server`安裝分流、原MigrationStore及probe、peer router，
`test_peer_installation_origin.py`、4個原peer routing案例、3個root bootstrap／拒絕案例。

**交付：** #294於台灣15:11:29正常合併
`104edef88d727e868759ac0b534df572ec2a517f`，head `5322dd00`，包含已合併的#289。
兩支各自兩軸清零且CLEAN後合併，無admin／force。已通知總協調與fdbb安排同build的API／Hosted／工作台，
部署後才以新回合重新驗證；本線沒有操作live或重送原未知工作。

## 2026-10-01：CAP-02 移除合計8次與已接收回饋

依總協調緊急確認，直接移除A/B模型請求合計8次，而非只限縮到有handoff的root。
現有2跳／8則訊息、同root唯一handoff及回覆、once／原回條與peer每回合100次仍保留。
固定實作 `200f0a80abe2dd4faa43629e821475ff735f57e8`：
**134個相干Python案例通過，0 fail/error/skip，92.121秒；14個工作台案例另外通過。**
兩批不相加為模型樣本，沒有真模型或Azure／8765操作；新main整合另記。

先以正式signed reserve-model入口重現：選collaboration但沒有handoff，第9次回429，
而非預期200。刪除加總後真人與B皆能第9次，原message／hop上限與request重播規則仍通過。
已核回接收回條後，B unknown、read失敗或等待到期，現在明說「訊息已接收，B 的結果尚未確認」，
不是系統拒絕，也不表示B未執行／已停止；已知refs拒絕碼保留。

Chat Completions與Responses的真本機SDK測項確認模型實收這個固定回饋及公開進度code；
其餘包含SDK／平台／Storage I/O替身，不冒稱全部為真SDK或live。
保留的模型上限、工作額度及peer期限碼可由API header到Hosted，再沿原progress schema到UI；
畫面顯示白話及原code，未核對結果不冒稱已確認，沒有新增自動POST／重送。
新的公共投影只收固定白名單code，不輸出任意例外、body、token或private reasoning。

原RED：第9次429一案；三種已接收後失敗的回饋分類三案；單回合上限碼未進公開進度一案；
工作台兩案只有原碼、沒有說明。原件保留，不回填PASS。
開發中曾把前一個測試的尾段誤放到新測試後方，修正後再跑上述134案；這是測試編修失誤，不算產品RED。
Python原件 `peer-urgent-targeted.xml` SHA-256
`4939fb14e9f13f7ad917133161180f0a327e83e983e8955a264676858b7bcf9b`；
UI原件 `peer-urgent-ui.xml` SHA-256
`037c976ffb33d89794e901c3e3deb04e3d597aa2ad8d96825e2f1e078cb2b465`。
再整合main `3a66a3e6` 至 `cc88b45f`，只解CURRENT-STATE文字衝突；
上游m2只新增截圖filename指引，未改本片邏輯。7個直接相交案例另通過（26.827秒），
原件`peer-urgent-main-3a66.xml` SHA-256 `8615f34ebc604258eeefb54cbd85b9920b4bf40074977e23173779171d278fc4`。
各批不相加，原live失敗不改判。
Opus5.5固定`d421888a`：**Standards 0 P1／0 P2；Spec 0 P1／0 P2**。
Standards核來源／deadline讀取集合、once、已接收分類與公開碼；Spec另做一段不寫檔的分類檢查，
確認5xx／unknown轉成結果未確認、已知refs拒絕仍保留碼。兩軸沒有重跑測試套件。

B23另外已知hosted_claim ReadTimeout；71c7指出mounted app未快取時會在claim前重掃完整inventory。
該次boot的確切觸發仍不冒稱已證實，下一支以獨立RED／green處理；本片不拉長timeout、不重送原RID。
send的645 gets／511 batches只記性能線索，不把效能改善加成此片交付gate。

**Verified against：** `reserve_model`／`check_model_time`、message ledger、Hosted peer tool／boundary、
`public_event`／`m2_copilot.emit`、signed progress／`ConversationProjection`及工作台處理步驟／結果。

**交付：** #289於台灣14:10:29正常合併`edae6d28`，
head `8b633769`；原13:04 B23與12:57真人失敗不回填，配合後續#294由部署線安排新回合採用。
後續fdbb回報：build `0cbe40a4`已上線，15:06選回collaboration，新回合使用12次模型請求、未於第9次被擋。
本線已核該build包含#289，未獨立重讀該新回合原trace；此處記部署線觀察，不當完整CAP品質接受。
#294的`104edef8`尚未部署，B23舊unknown維持原樣，後續由新CID驗證首次claim。

## 2026-10-01：T5 peer 回合模型上限

固定實作 `ec1653274695d986311e06f4e71d8d7c137bce1b`，基準 main `5053fd3c`。
**相干37案 PASS，0 fail/error/skip，66.628秒**；沿用SDK1.0.13／runtime1.0.83，
沒有安裝套件、建立venv、呼叫真模型或操作Azure／8765。
其中5案是真本機SDK/runtime配離線transport：100次正常結束、101次拒絕、
真人不加100上限、較小caller上限、常駐SDK下一回合重新計數；其餘32案是SDK／平台／儲存替身或不啟動SDK的輸入拒絕。
不同SDK request context共用同gateway的並行案例是I/O替身，不冒稱真delegation／compaction端到端驗收。

第101次拒絕保留 `MODEL_CALL_LIMIT_REACHED`；正常SDK退出可保存failed及原停止回條，
API／startup reader讀得回新增的peer-only失敗類型，A讀到唯一blocked及同一原result。
同ID／傳輸未知不重送，原8次peer_limit仍直接驗證；真人原失敗交付與取消／回應遺失規則保持。
SDK在受控錯誤後可用非零exit code退出，原件照實記錄，不要求它偽裝exit0。
真正停止或保存未知時仍unknown，這不是用上限訊號取代停止證明。

原RED分別是缺少caller參數（TypeError）及新失敗值被正式HTTP拒絕（422）。
首次相干主批33 PASS／4 FAIL：3案在完全相同的main快照仍失敗，證明是測試替身缺task、
或把URL物件寫成字串；另1案是新測試誤把Hosted通用503遮罩當成API的403。
只修正替身及改用正式HTTP接點驗拒絕，未加產品fallback；定點5案通過後，
再於上述固定實作跑37案全通過。舊失敗原件保留，不回填PASS或將各批相加。

JUnit `t5-model-limit-ec165327.xml`，SHA-256
`bab0a4427cfd7595f9e1ac7b6966500658de962570c4b0efe7699b66e0ed1baa`；
selectors、RED與main對照hash見JSON `t5-peer-model-call-limit-20261001`。
**Standards（固定25be1558）：0 P1／0 P2。** Opus5.5核對首次await前計數、真停止、
peer-only保存、取消優先時content／proof一致性及原件hash；沒有需要追加重現的疑點。
**Spec（同一固定head）：0 P1／0 P2。** Opus5.5核對100／101、SDK吞錯不當成功、
新回合reset、真人不變、原unknown及唯一blocked；沒有重跑測試。兩軸各自零問題，不合併或重排。
本片不升級完整T5、CAP品質或76項帳冊的接受狀態；B票producer仍由79fa提供，
T4不能因此提早移除舊模型上限。

其後整合main `df013f70`（T1主人索引、T2控制接點、角色／目錄與薄轉送），
只有spec與CURRENT-STATE文字衝突，保留兩邊意圖；已審模型上限核心／tests的bytes不變。
整合提交`69f21bda`另選6案全部通過（27.995秒），核SDK上限、signed接單、failed保存、
原8次及真人拒絕接點；不是追加6個獨立覆蓋或重跑完整兩軸。
JUnit `t5-limit-main-df013.xml` SHA-256
`7f22eddaa7544b58ba74213bb69b4e70cf332bd630eb373e14e499c28977efca`。

#279於台灣12:45:34正常合併`48223e99`，
head為`d1379653`，CLEAN後以固定head合併，沒有admin／force或直接push main。
合併瞬間main新增#281 Agent身分索引，實際第一parent為`30c2c02e`；
模型上限程式完全不變，另在真正merge提交選唯一相交的peer保存／回傳／startup案例，
1案通過（9.238秒）；原件hash見JSON，不回填原37案或相加為44案。
本片已交付但未部署，B票producer及完整替代仍未完成。

**Verified against：** `m2_copilot`、`behavior_assistant_hosted`、`behavior_http_diagnostics`、
`SaveTextReply`／`TurnArchive`、`behavior_agent_message_runtime` 與直接相干tests。

## 2026-10-01：WAKE-05 owner routine producer

固定實作 `50433c485351aedac802c5cd7524b0bce391f05f`、基準 main `45f87d10`。
相干四檔 **61 PASS／0 FAIL/error/skip，53.819秒**；既有 Starlette deprecated alias warning
保留，不改寫成產品錯誤。結果原件與前述各個 RED 見 JSON `wake05-owner-producer-20261001`。
再整合 main `fec04a8e`（#258 authority 讀取優化）至`cfa78c55`，
同四檔另跑61案通過、55.444秒；這是另一批整合結果，不與前批相加。

本片只做 owner 觸發、固定每日時刻的本機 producer，預設關閉。
相同 occurrence 沿同一個 request／registration／job；offset 等價不產生新 ID，
未來／非排程／舊時段及 DST 不存在／重複時刻拒絕。一般 412 沿原回條查讀，
沒有為了讓兩個並行 caller 都立即回 200 而新增 retry 或鎖。
原 HTTP 回應遺失、原 runtime 重建、已保存結果再次查讀都不重做工作；
native-mode synthetic fixture 的原 startup reader 也讀得回 occurrence。

原人類 source 與 ordinary submit 相容案例一起執行。routine 的登錄、
ingress 與 opt-in origin 有獨立來源標記，但不是操作授權。
真 SDK/runtime 的外部 I/O 以既有替身隔離；沒有啟動 SDK 執行檔、真模型或 Azure。
冷匯入另核 pure wire 不需 API 套件、API producer 不匯入 Copilot SDK。
Windows 缺 IANA 資料的失敗已確認為 `ZoneInfoNotFoundError`，只在 API 相依補固定
`tzdata==2026.4`，沒有重建 venv 或替 Hosted 加 API 套件。

初次缺 runtime fixture 路徑、錯用 context request ID／撤銷 route、
重建測試沿用已綁 inbox，以及 event loop 建立晚於 socket guard，均為測試設定修正，
不是產品 RED；並行 412 是原契約，沒有藉測試改成新重送流程。
WAKE-05 的機器觸發、live 排程、一般 cron/CRUD 與 M/H 品質接受仍未完成。
本次未改 B2 百分比或 76 項帳冊；CAP-02 原 live FAIL／原件調查保持獨立。

**Verified against：** `behavior_routine`／wire、`WorkPolicyControl`／history、
`BehaviorRuntime.submit`、`instruction_turn_origin` 與 owner HTTP 路由；
`test_routine_producer.py`、`test_routine_native.py`、`test_instruction_turn_origin.py`、
`test_work_policy_submit_compatibility.py`。

Opus 5.5 初審在`f4b7de0e`：Standards與Spec各0 P1／1 P2。
`b281300d`分別修正登錄digest與原話reader的一致性，以及partial admission的原ID恢復：
routine不是人類原文，三個依賴UserTurnSource的reader明確404，owner的保存reply仍可讀；
`not_admitted`／`admission_incomplete`保留原`retry_same_request`，已登錄的原工作可補enqueue，
未登錄的舊時段仍不能補跑。原回條、權限與外部未知效果規則不放寬。
直接RED後固定修正提交另跑67案通過（257.157秒）；兩原軸只複核各自P2 delta，
在`b281300d`均回0 P1／0 P2。Standards另選5案、Spec另選13案通過，沒有重開完整審查。
再機械整合main `43115cf7`至`0315fc9a`，已審routine程式及測試blob不變，
核心29案另通過（157.966秒）；各批結果與範圍不相加。

## 2026-10-01：CAP-02 傳送拒絕與固定收件人

**後續08:32唯讀診斷已核回原件，原live FAIL不改判。** 在fec0 API明設`PYTHONPATH=/app`後，
三個reader module指紋吻合`fec04a8e`，原TurnArchive通過JSON及Python重驗，
archive `72996c3a…`、17,609 bytes完整核回。先前None-only summary／archive錯誤
是診斷腳本誤載`/opt/fa-checkout`舊code，不是正式reader拒絕已批准shape，沒有改產品或原資料。
原同一tool call的兩種SDK事件都記`recipient_agent_id="B"`、`kind="request"`、空refs；
kind合法，但B名稱不是server Agent ID，確認#256補收件人提示／enum所對應的輸入缺陷。
原API拒絕碼未保留、這次未讀B registry，不把它們說成已直接核驗。
固定去值結果見JSON `cap02-original-archive-diagnosis-20261001`，新root live驗證另記。

**以下保留修補時的未確認狀態。** 部署線原回報基準`fa10c71e`、
A62／B19、A v2／Responses。父線的去值原件索引確認A共2次模型請求、
1次`send_agent_message`從started變failed、公開message投影為空；API timing的
`outcome=returned`不是HTTP成功，空投影／空B log也不能證明未曾寫入或送達。
原function-call arguments與當時B registry仍待取得，不能先把「模型填B名稱」或
「B模板read gate」當成live根因。本線未重讀Azure或重試原回合。

已獨立重現並修正三個接點：

- 正式factory將`AGENT_MESSAGE_ROUTE_DENIED`變成`CAPABILITY_UNAVAILABLE`；
  signed API又將原因碼變成`UNCLASSIFIED`，Hosted無協作專用傳遞。
  現在只傳固定白名單code並留去值log，不讀錯誤body或公開私有字串。
- 工具只給自由字串欄位，沒有提供server B ID。新assignment捕獲唯一B ID／名稱，
  schema列唯一enum，handler拒絕名稱／其他Agent；server原授權與B registered read不變。
- 固定SDK的schema拒絕原本只有generic feedback；現在送出前拒絕明寫沒有送達B／沒有B結果，
  unknown則保留「沒有已確認的B結果」。禁止A把自己的計算冒稱B回覆，不加重試或新機制。

### Verified against：傳送拒絕

- 原兩軸固定`304c57b3`：Standards 0 P1／0 P2，Spec 0 P1／1 P2。
  B registry的`AGENT_SCOPE_INVALID`等原碼在API logger之前就被公開header白名單遮掉。
  `fc77d7d9`只改為沿既有`_log_request_failure`記原`Problem.code`；
  不記message／body，非code字串仍拒絕，header／Hosted／模型的白名單不擴大。
  v1／v2 signed API兩例先RED，再加原原因碼與私文遮罩回歸共**7 passed，17.55秒**；
  只由原Spec軸複核此delta，不重開Standards或把舊91案說成新head全跑。
- 實作`e6b97173`；整合main `5b195899`後固定`bfe0007d`：**91 passed，0 failed/error/skipped，
  137.56秒**。包括v2／Responses的signed API／B登錄／原件查讀，兩種provider的固定SDK
  失敗回饋，及兩SDK沿真API從schema取得B ID後A→B→A、A唯一final。
- 同批保留scope／refs拒絕、原ID／receipt、response-loss不重送、FYI不喚醒、8次上限、
  B普通human路徑與既有其他工具的失敗文字。Table／平台／provider I/O均為合成替身，
  SDK1.0.13／runtime1.0.83是真本機；不是Azure／真模型品質或live成功。
- 先前相干批是87 passed＋1 setup error：測試誤把archive codec字串填入checkpoint profile；
  只改fixture為現有`model_provider`設定後，該v2案例單獨1 passed，再於上述整合批通過。
  原失敗紀錄保留，不稱產品故障或「既有問題」。
- 已讀`behavior_agent_message_api`、`behavior_agent_message_tools`／wire、
  `AssistantAuthority.issue`、`Assignment`、`AssistantClient._post`、
  `behavior_assistant_api`錯誤出口與`m2_copilot.tool_failure`。
  本機B正常登錄讀取通過不代表原live registry已核清；沒有修改B gate或重寫舊登錄。
- 原live去值索引`cap02-live-send-fa10c71-observation-reference-20261001.json`
  SHA-256 `c94e9d8de0e3ca1d69768eaae1e2721834355bb0595bdd7d07e2f378193acce6`；
  本線只核讀這份索引，不冒稱重核其六份原件。各分批檔案／hash見JSON `cap02-send-rejections-20261001`。
- 下一批由fdbb同build部署後，重新選collab並另跑原計畫②③；不重試原RID。
  原ID／unknown／用量保留，WAKE-04／07維持暫停；本片不改76項帳冊或任何全域接受狀態。
  總協調已確認本修補兩軸清零與CLEAN後照常合併，不等原archive診斷；該獨立問題由父線續查。

## 2026-10-01：CAP-02 第二片原queue與A→B→A接續

### 第二片原軸 P1 修正

原兩軸對`513284a`的P1複核已清零。Standards另指出「已確定終局的B若refs不可用或
pre-SDK版本拒絕缺assistant result，queue第一筆會永久卡住」；另以窄delta處理，
不重開整個PR審查。只在有原終局證明時讀原worker result，或回無本文／refs的固定blocked；
`behavior_agent_message_history`與原unclaimed reader同步。未知執行不因此釋放。
此delta的兩個直接反例及正常大結果、原回條重播、原human版本拒絕共**5 passed／79.016秒**；
原件在JSON `cap02-terminal-queue-20261001`；Opus 5.5 Standards只核`55670ec9`此delta後
為0 P1／0 P2，原兩軸清零維持。整合main `48d89d89`沒有改CAP邏輯，
雙SDK配對與兩個terminal案例再3 passed／75.628秒；尚未做live或H接受。

原Opus 5.5 Standards回2 P1／0 P2，Spec回1 P1／0 P2，其中A交付隔離是同一問題；
共兩個不同P1。修正只在A的協作推進接點記錄B的Problem後繼續A自己的原stop／forward／final，
直接B接點仍拒絕；B未取得assistant claim前也不做一般query，不能把正常起步誤寫unknown。
`WORKER_STOP_UNKNOWN`與start/query unknown一樣保留原claim；沒有假稱B成功或停止。

直接取消／B撤權後A的已保存final、未claim空窗及原相干案例：
**17 passed，0 failed/error，1 deselected**，236.254秒（2026-10-01 02:32台北時間開始）。
兩個問題均先由正式controller接點重現；此批未再啟SDK或模型，原雙SDK證據保持分批。
原軸清零另以review回報記錄，不以測試取代。hash見JSON的`cap02-second-review-fixes-20261001`。

基準`99c3d17bba88795438d2659a41bbe13706c663cb`加本片；沿#226第一片及主線#239的
取消後checkpoint保存，不另造agent loop。**本機接線通過不等於Hosted／真模型／H接受**，
不修改B2百分比、76項帳冊或前批結果。

| ID／接點 | 本輪已取得的證據 | 未宣稱 |
|---|---|---|
| CAP-02 C1／C2 | B原claim／ticket、Hosted／SDK SID獨立；queue work沒有B human admission／obligation；SDK prompt明示Agent brief不是human批准，B不讀選版head／私人memory或繼承resident worker PoC | 跨Team、第三位Agent、任意Artifact root分享 |
| C3／WAKE-03 | 原A observer驅動B C1；兩root queue按順序、兩observer不重疊B首次start；pending queue的restart訊號只回原A；遺失B result回應核同ID、不重執行B | 真API停機／Hosted機器故障恢復的live驗收 |
| C4／WAKE-06 | A／B共讀原session counter，合計8次且留A final；FYI／receipt不等待或開模型，unknown保留claim；A final forward後才消費內部result | 任意外部exactly-once或真人已讀 |
| C5 | 兩個真SDK1.0.13／runtime1.0.83沿真正API與原controller：A 2次、B 1次離線provider請求，B完整結果回原A，A只準備1筆final；兩SID不同 | 真模型品質、Azure計費數字、Hosted端到端已採用 |
| 保存／取消 | 未派送、已排未開、已啟動三條取消接點；不把取消受理當停止，generation-0取消只核原回條；大B結果完整保留並回超限blocked；錯結果／逾期等待明拒 | 沒有停止／結果證明的unknown被自動清掉 |

### Verified against：CAP-02 第二片

- `test_agent_message_continuation.py`、第一片API、tool／wire與既有ledger組合：
  **126 passed，0 failed/error/skipped，2 deselected**（兩個native SDK案例另跑），
  2026-10-01 01:42台北時間開始，293.857秒。
- 雙SDK配對＋單工具SDK回歸：**2 passed／31.257秒**。配對案真的啟動兩個既有固定runtime、
  經JWT／ticket、模型reserve／consume、保存及native回報接點；provider／平台I/O是替身，
  controller由本機測試驅動，沒有Azure操作。單工具案是另一個獨立案例，不加總為同一root或M樣本。
- 原B1c human保存／共用費用帳與原observer兩例：**4 passed／40.488秒**，與前批區分記錄。
- 初次雙SDK配對失敗是測試回報硬填`exit_code=0`；改成保留SDK實際停止回報才通過，
  沒有改產品停止判準或把Windows結果當Linux／Hosted證據。
- 主線#240另提供第二hop投影的
  4個Python／7個DOM直接案例，Standards清零，只有回歸沒有產品RED；原A待答、兩journal、
  草稿／晚到回應隔離均保留，本線未重做該片。
- 程式：`behavior_agent_message_runtime`、message ledger／API／wire、Hosted factory、
  `AssistantAuthority`／`WorkPolicyControl`、原SDK checkpoint／instruction history及server observer。
  SDK async ToolHandler使用[固定版官方Interface](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/tools.py)。
  Hash與命令範圍在JSON的`cap02-queue-return-20261001`。產品兩軸審查另記，不以測試代替清零。
- 部署後最多三則：B registry／同build版本、ENV與兩份console config讀回；1個合成Team
  A→B→A root（合計provider≤8、A唯一final）；同次root核原message／FYI／receipt與0新增模型。
  只列探針，部署線依批准執行；原8765／journal與未知結果不由本線改動。

## 2026-09-30：CAP-02 第一片可信訊息接線

### 第一片兩軸 P2 修復

以`22bff2df2405e525752c7bfa18aa869173cb820d`為修正基準。原Opus 5.5
Standards為0 P1／2 P2，Spec為0 P1／1 P2；修正`24fd228`後，Opus 5.5兩軸delta均為
0 P1／0 P2。Spec由原軸複核；Standards原agent狀態已清除且查無最後結果，改由同模型
只恢復兩項P2的窄複核，沒有重開Spec或整個PR範圍。

- busy讀取已改為每批≤98 key，並在B登錄前執行；真BehaviorStore的98／99／197筆邊界先重現再通過。
- 第一次root寫入前及交易內核期限；已留下root但ledger尚未初始化的中途狀態可讀空的已接收訊息清單，
  只有一半初始化或儲存錯誤仍拒絕，不把unknown當成成功。
- sender正式Source read、B固定拒絕條件、handoff／回覆預留與queue容量都先於B登錄；
  登錄後仍保留B registered gate。精確412只重試原ledger request，已提交則回原回條、不再寫；
  未接單的peer metadata不占busy。同ID同內容可補完原registration，不能將它讓給新ID／新內容。
  若登錄後才撤權／到期，保留原metadata，不改digest或刪資料。

直接API／refs／ledger組合**84 passed，0 failed/error/skipped**，2026-09-30 22:12台北時間開始，
607.373秒；來源與hash見JSON的`cap02-pr1-review-fixes-20260930`。整合main `cf63ba9`
只解CURRENT-STATE文件衝突，CAP程式不變；原訊息回讀與雙端Source撤權各單獨1 passed。
先前整合測試曾遇`MemoryError`／`RuntimeError`，未做base歸因；原碼分開重跑後通過，
不稱為已查證既有問題。其中只有故障注入、合成身份與本機I/O，沒有模型或Azure執行。
這批不重跑無關的UI／完整SDK套件。

基準`51ad9ed165b42a8607ef25bfb1c7941979e5da58`，共用wire與工具分批提交至`90e194c`，
再加本片API／B無模型登錄／reader整合。此為C類直接證據，**不是A→B→A完整接受**；
B consumer、B模型、原A SDK等待、兩Agent合計8次provider限制及live／H仍未由本片驗證。

| 接點／ID | 本片證據 | 保留的限制 |
|---|---|---|
| CAP-02 C1／WAKE-01 | signed JWT＋原ticket／assignment才可保存訊息；偽造sender／root／source、錯目標與錯ticket拒絕。B以A agent來源登錄真work，沒有假human／B worker job或B human obligation。 | 不支援第三位Agent、跨Team或一般來源producer。 |
| CAP-02 C2 | 只有兩端原scope都允許的固定Source通過；root不擴B.source_ids，撤權後read拒絕；B不捕獲memory／私人history。純wire冷匯入不需API-only套件。 | root目前只釘選A原profile Source；Artifact helper的局部結果不等於root已開任意Artifact分享。 |
| CAP-02 C3／WAKE-03 | 同ID回原訊息、異內容拒絕；注入保存後response loss時真Hosted client只send一次，之後純讀原key；B只有registration、尚無job時保留新handoff為queued。 | response loss是故障注入；沒有宣稱在live重現。B queue消費／重啟接續屬第二片。 |
| CAP-02 C4／WAKE-06 | FYI為silence，不消掉原人類obligation；18項API案例中的provider案例只保留最多8個A模型intent，沒有真正送模型。 | receipt與result／blocked完整分流沿原46項ledger契約，不當成本片已接B→A。 |

### Verified against：CAP-02 第一片

- 主整合：`test_agent_message_api.py`、`test_agent_message_wire.py`、`test_agent_message_tools.py`
  （排除已由工具線跑過的`pinned_sdk`）51 passed，0 failed/error/skipped、1 deselected，
  2026-09-30 21:06 台北時間開始，211.366秒。其後busy補修的兩個直接案例再2 passed／13.834秒；
  與主批重疊，不加總成53個不同案例。
- 既有B1c／API回歸一批28 passed；純wire＋原ledger契約一批59 passed；皆保留各自執行時點，
  不是同一個最新head的全套宣稱。最後busy改動只重跑受影響的兩個案例。
- 程式：`behavior_agent_message_api`、`behavior_agent_peer_work`、`behavior_agent_message_history`、
  `behavior_assistant`／API／Hosted、`behavior_work_policy`／history與`behavior_runtime`。
  API資料走真composition、JWT verifier、metadata writer、SourceMaterials及startup reader；
  Table／HTTP是離線I/O替身，沒有Azure或原8765操作。
- 新入口加入前直接得到404；接線中發現的startup未分類、wire匯入API-only套件、B未登錄及busy
  無job拒絕，皆以原接點收斂。錯把原本不共享來源的fixture當共同Source正例，已改為登錄前
  明確批准兩端共享來源；沒有放寬產品權限。一次冷快取診斷等待未完成，原碼重跑通過，
  不將該等待歸因為已確診產品故障。
- 工具線另交付固定SDK1.0.13/runtime1.0.83＋離線provider的單工具案例（未啟B）；
  不把它當成Hosted、真模型或A→B→A證據。各原件hash見JSON的`cap02-api-producer-20260930`。
- 不改B2百分比或76項帳冊接受狀態。部署後只安排三則同一root控制探針：先B registry／同build
  版本與ENV及兩份console config讀回；再A→B→A合成Team任務且provider合計≤8；
  最後依批准重讀／重播原message與FYI／receipt，新增模型為0且A final不重交。第二、三則須等consumer片。

## 2026-10-01：WAKE-04／07 原保存結果回歸

基準`fa10c71ee5201b738c1cab573fd71482aec4001c`，本片只有測試／fixture與文件，產品程式未改。
新增兩例走既有正式入口；功能原本存在，**不是產品RED→green**。final preparation回應遺失
是明確故障注入，不是live事故或新的SDK／模型執行。

| ID | 直接證據 | 仍未涵蓋 |
|---|---|---|
| WAKE-04 | `execution-ready`在保存前回409且不通知；原signed保存成功後，ready callback已可讀原reply、archive與VerifiedWorkResult。重複ready只指同binding與同hash，不多寫入；原startup reader可讀回 | 真API停機期間的喚醒時效、routine／其他外部producer；已有in-process事件不當完整持久喚醒 |
| WAKE-07 | 原result已保存／forward且只有ack outbox時仍無final；final preparation commit後注入回應遺失，下一次controller用原final ID繼續awaiting ACK，不再start/query/stop工作，不改reply/archive/result或USD帳，真人尚未ACK仍為false | 不外推為所有due／跨通道模型補交計數或M/H品質接受；既有持久repair計數不重設 |

**Verified against：** `test_instruction_completion.py`＋`test_instruction_observer_resume.py`
共**12 passed，0 failed/error/skipped，39.017秒**，其中新增2個組合案例。
原observer替身補上真runtime已有的`work_policy=None`，而native route沿原provider接點；
第一輪fixture缺欄／provider而拒絕不是產品故障。沒有取消runtime gate或放寬授權。
原件hash見JSON `wake-saved-results-20261001`，本片不改B2百分比。
Opus 5.5 Standards在`c49e337b`核對為0 P1／0 P2，另重跑4個受影響案例通過；
依test-only規則不跑Spec，沒有把review當成live／H接受。

2026-10-01 05:35解除暫停後，整合main `919f1899`至`9a0d9ad4`，
同兩個測試檔另跑12案通過（JUnit 17.254秒），原結果不改寫或相加。
observer回歸檔與已審版本逐位元相同；completion fixture與main相同，
衝突只有同一個`work_policy=None`的keyword位置，沒有改測試邏輯，依批准不重審。
新結果與hash見JSON `wake-main-integration-20261001`；仍未執行SDK程序、模型或Azure。

WAKE-05仍沒有routine producer，最小設計維持五點，未實作或部署：
1. 可信owner、已批准版本與通知方式由原有權人確定，不由模型字串升權。
2. 以官方routine能力保存時區與觸發資料，不另造一般scheduler。
3. occurrence ID與原處理意圖先保存，同ID回原結果，不換ID重跑。
4. 每次效果仍核目前工具／操作權與原額度，排程不是永久批准。
5. 無新資訊可記silence；失敗、待批與人類交付責任不能被它清除。

CAP-02部署／live由fdbb依批准處理；這片沒有新增模型探針需求，不為補測重跑原unknown。

## 2026-09-30：WAKE-01／03 正式入口回歸

產品基準 `17765726201ac2898b85624e1a27f79c46fe98b5`；本片只新增測試與說明，
沒有修改產品授權、CAS、fence、保存紀錄或重送規則。新增兩例都在現有實作通過，
是缺少直接組合案例的補強，**不是產品 RED→green，也沒有用故障注入製造產品失敗**。

| ID | 本片直接案例 | 仍未涵蓋 |
|---|---|---|
| WAKE-01 | 同一份已接單原文含偽造 system／routine／approval marker；signed agent 重播人類 submit 回 403，真人同原 ID 回原 binding，Context 來源仍 human、authorization=false；無新接單、模型或控制寫入 | routine、一般 tool-result 與長期 Agent message producer 尚未接線；一個人類入口不代表全部來源完成 |
| WAKE-03 | 工作已取得原 execution binding 且仍 starting／running；同 CID 的文字追加／假取消回 409，錯 work 或 agent 的取消回 403；真人取消只作用於原 job，回受理而非已停止，重播回原 receipt，原人類 obligation 仍未履行 | 不新增忙碌期間的安全追加功能；遲到批准、其他來源控制事件及 H 品質不由本片接受 |

### Verified against：WAKE 第一片

- `tests/shared_context/test_instruction_turn_origin.py`：33 passed，0 failed/error/skipped，
  其中新增 2 例、既有 31 例；2026-09-30 18:31 台北時間開始，108.869 秒。
- 新案例：
  `test_agent_cannot_replay_the_same_human_instruction_or_promote_its_embedded_markers`、
  `test_busy_instruction_rejects_textual_control_but_accepts_only_its_human_cancel`。
- 同檔原 `context-origin` 測試核身份、ticket、版本、原件與 caller 欄位拒絕；新案例在第一次
  合法 context 讀取完成後固定快照，避免把正常 context-read 回條誤判為重播寫入。
- 證據與檔案 hash 見 JSON 的 `wake-entry-cancel-20260930`。JWT 為合成簽章、Table／HTTP
  為離線 transport；没有 SDK process、模型、Azure 或 live 8765 操作。M 類仍待 H，
  不提升 B2 百分比或修改進度線產物。
- 後續順序依總協調：本片後接 CAP-02（含 WAKE-06 訊息契約接線），WAKE-04／07 另排在
  CAP-02 後；Routine 沒有 producer，WAKE-05 只提供最小設計，不實作排程。

## 1. 基準與來源怎麼讀

| 層次 | 本輪核對結果 | 不能外推 |
|---|---|---|
| 本機程式 | 原盤點開工 `git status` 乾淨；HEAD 為 `cb7a2d8`，已含 #19。該階段只新增評測文件與摘要；後續產品切片另列 | Git 合併不是已部署；原盤點沒有查 live Azure 配置 |
| Runtime pin | requirements 的 Python Copilot SDK `1.0.13`；`run_copilot` 核 runtime `1.0.83` | 本輪沒有啟動 Copilot runtime，不冒稱新的真 SDK 接線驗收 |
| 歷史帳冊 | schema 2，172 runs；4 hosted、48 local_sdk_offline、69 offline_checker、51 source_only | 不同種類、版本、粒度的 run 不能加總成 172 個品質樣本 |
| 原四章目標 | 76 targets／12 scenarios；帳冊宣告 1 target accepted（PA-02）、11 partial；scenario accepted 0 | 來源仍將 PA-02 列為待修後兩軸複核的接受提案；本輪未核複核結果，不視為已結清，也不代表本輪 HEAD 或新增五類完整接受 |
| 本輪結果 | 39 個 selectors 展開為 68 個 cases 通過；另 34 個 M4 checker cases 通過；0 failed／errors／skipped | 包含未配置的 adapter 與固定工作分支，不是目前一般助理完整路徑的覆蓋；cases 與真人 traces 不是相同單位 |
| 第三方參照 | 實讀 Grok §6–10；來源為非官方靜態研究 | 工具／wake 數、模型與常數是樣本，不是 xAI 官方產品保證 |
| 官方依據 | 已讀固定 SDK tag 的 loop／custom agents／persistence 及 Foundry runtime contract | SDK 支援不等於 FA 已接上；最新版能力也不自動適用 runtime `1.0.83` |

開工時 `CURRENT-STATE` 還寫 #19 待合併／複核；本輪用實際 worktree HEAD 定位，
只確認 #19 已合併，不從合併推論新的 PR review 結果或部署狀態。
[持續狀態](context-implementation-status.md#狀態來源)與
[PA-02 修後結果](prompt-assembly-evaluation.md#修後42-case同baseline矩陣)仍記「待修後兩軸複核」；
由原協調線核對修後回條並更新來源，本輪未查遠端複核紀錄，保留此待辦，不改寫歷史結果。
Bluebird 未配置此 GitHub repo 的 scope，因此以本 worktree 的來源與測試核對，沒有借其他 repo 的索引作證。

本機沒有 `microsoft-foundry` skill；已按 repository 指引唯讀閱讀官方 skill。
**沒有安裝該 skill、執行其 dependency setup、Foundry observe workflow 或建立遠端 eval suite。**
正式來源及精確 SDK commit 見 [Target State §8](agent-runtime-target-state.md#8-官方依據與研究界線)。

## 2. 五類設計、實作與缺口

### 2.1 工具呼叫與路由

**設計：** SDK 管工具循環；FA 在 permission／API／實際效果入口核身份與當前授權。
Catalog、schema 可見與外部操作已獲准是三件不同的事。沿 BC-04／05、FR-20／25 與 SP-08–12／24／26。

**實作：**

- [`AssistantSession._run_turn`](../src/fa-shared-context-poc/shared_context/behavior_assistant_hosted.py)（537–673）：
  結構化 fixture 有 `read_context`、`save_result`、`finish_task`、`execute_operation`；
  一般 instruction 只保留 `read_context`／`execute_operation`。
  明選 approved-skill profile 才加 `read_skill`，目錄是 skill metadata，**不是通用 connector discovery**。
- [`run_copilot`](../src/fa-shared-context-poc/shared_context/m2_copilot.py)（399–443、499–681）：
  每個 custom tool 驗 JSON schema／session／名稱；permission approve-once 不取代業務批准。
  明確排除全部 builtin／MCP，`mcp_servers={}`、`custom_agents=[]`、停用 ambient config discovery。
  工具結果仍由 SDK loop 回模型，不是 FA 自造第二個 loop。
- 目前程式的正式組裝由 [`build_server`](../src/fa-shared-context-poc/shared_context/behavior_server.py)（565–567）
  配置 `WorkStateEffectAdapter`，交給 [`ToolBrokerCore`](../src/fa-shared-context-poc/shared_context/behavior_operations.py)
  （419–434）選用 `WorkStateDispatcher`。Hosted 的 `execute_operation` 經
  [`operation` API](../src/fa-shared-context-poc/shared_context/behavior_assistant_api.py)（172–178）進入
  [`BehaviorRuntime.execute_operation`](../src/fa-shared-context-poc/shared_context/behavior_runtime.py)（1027–1060）。
  `behavior_operations` 保留原操作 ID、固定內容、批准與未決結果；不因模型換 ID 就重做。
- 真正效果由 [`WorkStateDispatcher.execute`／`WorkStateEffectReceiver`](../src/fa-shared-context-poc/shared_context/behavior_effects.py)
  執行（1–38、482–546）：此組裝未配置自訂 transport，派送時直接呼叫同程序 receiver，
  **不是以 HTTP 呼叫外部 connector**。受控效果只容許修改一筆 queued、尚未 enqueue 到 executor 的
  DeliveryCore work goal，且須核精確內容／ETag／當前授權；不修改 legacy Context／M2／M3 資料。
  原 runtime／dispatcher 的 `check_mutable_target` 仍拒絕固定或已 enqueue 的目標
  （`WORK_EFFECT_EXECUTION_BOUND`），不能因此宣稱一般聊天已有可用的任意外部操作正例。
- [`ContextArtifactAdapter`／`BehaviorDispatcher`](../src/fa-shared-context-poc/shared_context/behavior_dispatch.py)
  的 HTTP artifact 路徑仍有獨立程式與測試，但 **`build_server` 沒有配置它**。
  HTTP transport 被測試替換，不等於它是目前一般助理 `execute_operation` 的實際 consumer。

**缺口：** 這條一般聊天路徑沒有 Grok 類的通用動態工具發現、connector 帳號目錄、
任意 shell／browser／desktop 或背景命令工具。M2／M3／M4 的專用驗收工具不能當成一般聊天已全數啟用。
沒有本輪真模型工具選擇、錯誤恢復品質或 catalog cache 效益的結果。

**本輪 tools：6 selectors／12 cases 通過。** 其中 9 cases 來自 `test_behavior_dispatch.py`，
驗的是未配置的 `ContextArtifactAdapter` HTTP 路徑；另 2 cases 驗 `ToolBrokerCore` 輸入／原 ID 規則，
1 case 驗 legacy profile 拒絕。此批**未選 `test_behavior_effects.py`、未走目前一般助理的完整操作路徑**；
保留 12 passed 原結果，但不以它接受 `TOOL-03`／`TOOL-06` 的現行 consumer，也不視為整組工具目標已驗收。

### 2.2 事件與喚醒

**設計：** 可信來源→耐久接單→原 work／obligation→受控 consumer；控制事件與使用者文字分開。
背景完成先保存；忙碌／重啟後沿原結果接續。Routine、FYI 與人類待答不採同一通知規則。沿 BC-01／02／03／07。

**實作：**

- [`DeliveryCore.admit`](../src/fa-shared-context-poc/shared_context/behavior_delivery.py)（167–215、766–889）：
  `IncomingEvent` 不接受 actor／source／approval；來源由 `VerifiedIngress` 決定，
  型別包含 human／routine／agent／tool_result／system。一次條件提交保存 event、work、obligation 與處理意圖。
- [`BehaviorRuntime.submit`](../src/fa-shared-context-poc/shared_context/behavior_runtime.py)（1090–1125）
  的實際入口由 principal 映射 human／agent；**enum 列出 routine 不等於有 routine producer**。
- [`ServerComposition.launch_instruction`／`instruction_ready`／`resume_instructions`](../src/fa-shared-context-poc/shared_context/behavior_server.py)
  （381–443）：同 binding 的觀察工作去重，ready 以 `asyncio.Event` 喚醒，超時讀回原工作，
  啟動時只接續已接單且未結清的 instruction。
- [`WorkerCore.forward_result`](../src/fa-shared-context-poc/shared_context/behavior_workers.py)（959–1086）
  使用保存結果；`DeliveryConsumer` 有 due／掃描／補交接點。
  另有 [`run_controller`](../src/fa-shared-context-poc/shared_context/behavior_server.py)（1024–1103）的獨立有界 consumer 入口。

**缺口：** 尚未在一般助理路徑看到 routine CRUD／時區／occurrence producer、完整 connector／卡片完成喚醒、
群聊／FYI silence 決策或自主背景 subagent 通知。現有 callback 是具體接點，不是通用事件 bus；
in-process observer 通過不證明 API 停機期間所有事件仍能按時喚醒，本輪未驗部署中的 supervisor。

**本輪 wakes：6 selectors／8 cases 通過。** 其中 7 cases 用
[`test_behavior_delivery.Harness`](../tests/shared_context/test_behavior_delivery.py)（46–95），
`work_policy=None`，走固定工作的接單／due 分支，不是目前自由指令 admission；
另 1 case 以替身 controller 驗同程序 ready observer 去重／喚醒。
這些只支持 `WAKE-01`–`WAKE-07` 的對應局部契約，不證明一般助理端到端接單／重啟喚醒。

### 2.3 回合生命週期

**設計：** 同一 conversation 沿原 Hosted／SDK session；SDK loop 停止、結果保存、工作成功、
executor 停止與人類交付各自判定。9/22–9/23 修訂優先於較早的固定測試窗口／每回合換環境。

**實作：**

- [`run_copilot`](../src/fa-shared-context-poc/shared_context/m2_copilot.py)（611–741）：
  正式 `start`、create／resume、events、send、disconnect／stop；resume 使用 `continue_pending_work=False`。
  同 SID 不能拿來盲重送 orphan tool；正常 owned process 停止與 force-stop／未知分開。
- [`AssistantSession._run_turn`](../src/fa-shared-context-poc/shared_context/behavior_assistant_hosted.py)：
  先排清公開 progress，再保存完整回答與 archive；有正常 stop 的失敗才沿 failure 路徑保存。
- [`behavior_instruction.save_reply`／`previous_turn`](../src/fa-shared-context-poc/shared_context/behavior_instruction.py)
  （209–320）：把 reply、TurnArchive、版本化 prompt／catalog 及必要 result 放入原條件寫入；
  核原 conversation／turn／SDK identity、必要操作與 graceful stop。前一 archive 不可用就拒絕空恢復。
- [`WorkerCore`](../src/fa-shared-context-poc/shared_context/behavior_workers.py)（656–735、959–1086）
  核 attempt／owner／epoch、保存遲到 evidence，unknown 保留容量；root 終局再轉交 DeliveryCore。
  checkpoint、versioned no-SDK disposition 與同 SID 接續各有獨立切片，**不是任意啟動失敗都有安全終局**。

**缺口：** 一般 instruction 的 pool 是按 work 建立（`pool_key`，426–443），
舊 shared-pool C4 演算不能拿來宣稱它已有跨所有助理的四 active 限制。
長對話 compaction 的語意品質、所有中斷／Hosted 恢復、完整私人環境及端到端效能仍需各自驗收；
新 codec／prompt 等能力沿既有 opt-in，不能從 merge 推定部署已採用。

**本輪 lifecycle：9 selectors／14 cases 通過。** 其中 3 個 worker cases 使用
[`WorkerHarness`](../tests/shared_context/test_behavior_workers.py)（78–104），另 3 個 delivery cases
使用上述 `Harness`；這 6 cases 都是 `work_policy=None` 的固定工作分支，
**不只是 C4，啟動 unknown／取消／交付案例也未覆蓋自由指令分支**。
其餘 2 cases 驗替身 SDK 的 progress／final 順序，6 cases 驗合成 checkpoint 保存／恢復邊界。
包含大於舊 inline archive 邊界的檔案及錯 scope／hash／版本／缺檔拒絕，但未執行真 Copilot runtime、
Hosted restore、真模型摘要或一般 instruction 的端到端結算。Target 為 `TURN-01`–`TURN-07`。

### 2.4 記憶的兩條路線

**設計：** Table／Blob 的精確 work、來源、版本與回條是權威；Memory 是有來源的檢索投影。
一般記憶需 subject／scope／同意用途、審核、更正／撤銷及有效期，不能透過記憶發布 SOP 或批准。沿 FR-16／18／26／31、BC-06。

**實作：**

- 舊 [`ContextService`](../src/fa-shared-context-poc/shared_context/service.py)（117–181）
  有 scope 隔離、抽取開始／poll／search；`assemble` 用 goal 前 2048 字，依序取 Team／本人記憶。
  固定 POC 的合成輸入與原生非同步 update，不是一般聊天每回合自動沉澱。
- [`MemoryGovernance`](../src/fa-shared-context-poc/shared_context/memory_governance.py)（254–396、401–578）
  已有 exact sources、五項 review、不可變 claim、更正／撤銷／有效期與 context manifest。
  `record_projection` 要求語意核對接點；程式不因 citation 存在便自動批准。
- [`NativeMemoryProjection`](../src/fa-shared-context-poc/shared_context/memory_projection.py)（541–741）
  保留 start／poll／completed receipt、來源 lineage、一次送出與 unknown；
  search 候選須能核回原 receipt 和 exact reviewed claim，未核 paraphrase 不能直接採用。
  投影不足時明示 degraded 並取目前合法原件；原件缺失不回空成功。
- [`build_server`](../src/fa-shared-context-poc/shared_context/behavior_server.py)（545–570）
  接的是 `DurableHumanReviewVerifier`，可 opt-in configured Memory projection；
  **不是已接兩個模型的 dreaming pipeline，也不是 eval 的 LLM judge**。

**缺口：** 一般 Hosted 回合的 `_run_turn`／reply-save 路徑沒有自動提出並送審記憶的 post-turn hook，
也沒有跨回合 synthesis 的 producer／排程／獨立模型稽核接線。
現有治理 building blocks 不應寫成完全沒有記憶；同樣不能寫成兩條自動路線已完成。
現有 native exact-match 投影策略保守，不能外推一般語意回想與相關性。

**本輪 baseline：19 個離線治理／原生 SDK serialization＋替身 transport 檢查通過；不含真 Memory 抽取或 recall。
兩條自動路線及語意品質仍待驗，target `MEM-01`–`MEM-07`。**

#### 2026-09-30：MEM-02 最小接線增補

上述19案為原批次，不回填新結果。依`eval-audit`核對目前程式：
`MemoryGovernance`已有三種類型、來源與更正／期限；真正缺口是HTTP固定寫note，
以及已審reader／CLI只收note。新切片沿原人審接通三種類型，另以結果metadata回傳有效期，
不更動scope、prompt或已保存source capture。組裝途中到期仍返回資料是另一次RED，
修正後在回傳前核期限。[實作與直接測試](../src/fa-shared-context-poc/README.md#真人選擇記憶類型mem-02)。

目前有明確pass/fail程式檢查，不需另建LLM judge；真人是否把短期事實選成長期偏好、
跨對話的語意正確性仍須部署後原trace與人工判斷，不能從離線成功率外推。
MEM-05／07既有receipt／exact-source fallback／必要context上限不重造，
後續測試擴充依總協調排序暫停，先接CAP-02資料邊界。

#### 2026-09-30：MEM-05／07 既有接點補測

總協調最新批准本片從最新main獨立交付、不等CAP-02；只有測試與說明，不改產品或啟用Memory Store。
MEM-05的新signed API案例區分202接受、completed原回條與實際recall：即使搜尋文字完全相同，
只要memory ID不在原回條就明示degraded、回到合法精確claim／原件；重讀不再送搜尋，
原件撤回後整次拒絕，completed回條本身保留不改寫。
MEM-07的新context案例在同subject累積8版無關episode後，只取原required claim；
無關episode到期不污染或阻斷該必要記憶，來源／內容／token與bytes保持，缺optional projection有省略理由。

來源提交`9121a34`記錄兩個新案例在原邏輯下通過。先前另在**只存在測試程序的刻意缺陷**下取得2個RED：
忽略required IDs改載全部heads，以及只憑相同文字接受未核lineage；還原即GREEN。
這是回歸敏感度檢查，不是將刻意缺陷寫成產品原版漏洞，也不是一般語意相關性的品質率。
沒有新LLM judge、原生Memory／真模型／Hosted執行；MEM-05／07及PA-13／14的完整M驗收仍未完成。

**Verified against：** `test_memory_governance.py::test_required_memory_is_unchanged_by_long_unrelated_history_and_optional_summary_omission`、
`test_behavior_memory_api.py::test_completed_native_receipt_is_not_recall_without_lineage_and_never_replaces_missing_originals`。
本支用既有pytest環境只跑上述兩案：**2 PASS**（signed API＋真Azure SDK序列化、離線transport）；未重跑刻意缺陷mutation。
獨立分支以main `a3ac6c5`為基底，只移植`9121a34`的測試與必要說明；不帶入CAP-02程式或文件，不新增雲端、計費或排程前置。

### 2.5 Subagent 委派

**設計：** Lead／父工作負責拆分與交付；child 有最小必要資訊／能力，沿共同工作權限與實際容量，
子結果不等於人類已收件。SDK custom agent 不等於新的持久 FA Agent 或獨立 Hosted 電腦。沿 FR-07／09、SP-26、BC-03／07。

**實作：**

- [`RuntimeBinding.child_jobs`／`enqueue_child`／`_controller_park`](../src/fa-shared-context-poc/shared_context/behavior_runtime.py)
  （111–167、1166–1317）：伺服器預先配置 leaf work；HTTP 只選 child ID／request ID，
  不傳 owner、任意 script／work 或自行授權。父工作先安全 park，再讓 child 前進，原 checkpoint 用於接續。
- [`WorkerCore`](../src/fa-shared-context-poc/shared_context/behavior_workers.py)（484–545、656–735）
  保存 parent／children、容量與 completion；child 沒有 root delivery obligation。
  父取消不假稱 child 已停止，結果未知不能以換 ID 重跑。
- 這是**受控 checksum worker 路徑**。
  [`AssistantRuntime.__init__`](../src/fa-shared-context-poc/shared_context/behavior_assistant.py)（115–142）
  拒絕 `binding.child_jobs`；一般 SDK 配置的 `custom_agents=[]`，全部 builtin／MCP 排除。
  所以不能把已存在的 child control code 當成助理已自主使用 SDK subagents。

**缺口：** 一般助理沒有模型選擇／建立 subtask、專用 child 工具 profile、SDK 子事件到 FA 工作的接線，
亦無真模型 parent→child→整合品質 baseline。SDK 官方已有 custom-agent 能力，先驗固定版本與 FA 控制接點，
不需要為此新造另一套 agent orchestration loop。

**本輪 baseline：15 個控制契約檢查通過；其中 C1 案例確實跑本機 checksum child processes，但 Azure／模型都是未執行。
不是自主委派成功率，target `DELEG-01`–`DELEG-06`。**

## 3. eval-audit：依影響排序的發現

已檢查本地 checker、fixtures、測試、帳冊、M4 報告及兩輪乾淨對話 baseline。
沒有使用 observability MCP；目前可用工具目錄未提供 Phoenix／Braintrust／LangSmith 類 trace store。
Repo 有報告與去敏 catalog，但舊 private traces／review UI 原件不在這個 worktree；
**本輪未重新人工檢閱那些原件，也未跨 worktree 尋找私人資料。**

### Evaluator Design｜優先 1：字詞支持檢查不能當語意裁判

**Status: Problem exists（作為通用語意指標時）。**
`m4_context_eval.evaluate` 的 `*_source_terms` 只查來源是否含答案 alias；
本輪在固定 base 重現兩個反例：`Mira is not the confirmed owner. Owen is the current owner.`，
以及 `Mira owns Project Vega. Project Lyra is owned by Owen.` 都仍使 checker 通過。
這是[既有已記錄限制](context-evaluation-baseline.md#1-為什麼先稽核既有-evaluator)，不是新發現的模型錯誤或新回歸。

**Fix:** 保留 M4 狹義 code check 及原結果，但不拿它接受 `MEM-04`／語意品質。
先由 `error-discovery` 檢閱支持性；穩定 failure mode 確認後才考慮 `write-judge-prompt`，
再經 `validate-evaluator`。不要以增加幾個否定詞 regex 冒充一般語意理解。

### Labeled Data｜優先 2：172 runs 不是足量且獨立的已標註 traces

**Status: Problem exists。**
帳冊有 51 筆 source review、69 筆 offline checker 等混合紀錄；
只有兩筆 `human_review.scope=batch`，沒有 `scope=target` 的逐條目接受。
乾淨對話兩輪合計 15 user turns／7 conversations，有批次接受，但沒有逐筆 gold。
Round 2 的 8 pass／1 strict mismatch 是客觀 checks，不能改成 12 筆人工 Pass。

**Fix:** 依五類分層取樣，加入隨機樣本；以 conversation／root case 保存相關性，
先補人類的二元判讀與原文原因，不重跑舊短對話湊數。
沒有適用 trace 的類別走 `generate-synthetic-data → 真正執行 → error-discovery`；
未實作或未獲計費執行批准的類別保留缺口，不生成假 traces。

### Error Analysis｜優先 3：有真實錯誤紀錄，但沒有覆蓋五類的 taxonomy

**Status: Problem exists（覆蓋不足，不是完全未做）。**
[M4 報告](M4-CONTEXT-POC.md)已有抽取遺漏、固定輸出不符、checkpoint 階段工具不合等可定位事件；
兩輪乾淨對話 review 則是 0 自由註記／0 failure patterns。
沒有證據顯示這已覆蓋 routine、一般工具路由或自主 subagent。

**Fix:** 下一輪用 `error-discovery` 先 open coding，再整理自然出現的 failure modes。
下表區分真正觀察與本輪 checker probes；target 的負向例只是待測假設，不能預填為觀察標籤。

| 已有觀察 | 來源／當時結果 | 這次如何使用 |
|---|---|---|
| Memory `completed` 卻沒保留必要私人偏好；模型填不受支持的替代文字 | M4 首批 `short-cold` FAIL | 記憶抽取／recall／答案支持性分開檢閱，不只驗 Memory status |
| 模型輸出 `V3` 而非完整既定標記 | M4 r2 `long-new-conversation` FAIL | 保留精確輸出契約，不因看起來意思一樣就回填 PASS |
| checkpoint 前暴露不合階段的 final 工具，錯誤只有 generic failure | M4 r3 incomplete；r4 後續單案另有 PASS | 工具集合、錯誤回饋、工作完成三者各核；不把未進 checkpoint 說成恢復丟資料 |
| `收到。` 不等於測前固定的 `收到` | 乾淨 Round 2 strict mismatch | 格式 code check 與人工批次接受分開，不變更原判準 |
| 引用存在／含同詞但不支持答案 | 六個 constructed checker probes 中兩個反例 | 稽核 evaluator 的邊界；不是兩段 agent trace 或 2/6 產品錯誤率 |

### Human Review Process｜優先 4：批次接受尚不能產生語意 baseline

**Status: Cannot determine（原件／review 專業範圍）；Problem exists（逐筆標籤缺口）。**
既有報告描述了安全公開投影及 3/3、12/12 已閱，但沒有逐筆語意標籤；
Round 2 只有一筆工具開始／完成配對，其餘不能補造中間路徑。
不能從報告推定是外包標註、只看 final，或宣稱 reviewer 已校準。

**Fix:** 由懂 FA 契約的人檢閱原輸入、必要來源、可公開工具過程、最終結果與回條，
先留自由文字／span 原因再定 taxonomy。既有 review UI 原件能合法取得時先重用；
不為本輪文件盤點重造 dashboard，也不公開 private reasoning。

### Pipeline Hygiene｜優先 5：版本與覆蓋仍需明確分層

**Status: Problem exists（跨範圍沿用風險）；OK（既有帳冊保留歷史的做法）。**
`context_target_eval` 固定原 76／12 IDs，驗的是結構、規格綁定與接受宣告前提，
**不執行 agent 或判讀語意**。它 validate 成功不代表五類 target 已驗收；
開工摘要也曾落後實際 merge HEAD。

**Fix:** 本輪用獨立五類 scope／結果檔，不改舊 ledger、分母或 `spec_sha256`；
同步 CURRENT-STATE 的已核基準與新入口。後續 model／prompt／工具／記憶路徑有重要變更，
重新取適用 traces 做 error analysis，保留原 FAIL／UNKNOWN 與各自 baseline。
本輪沒有新增 CI、排程或把整份評測要求變成部署 gate。

### Judge Validation｜目前不是「未校準 judge 已上線」

**Status: Cannot determine（repo 外）；所查資產內未見已建立的語意 LLM judge。**
已讀 checker 是確定性程式；`DurableHumanReviewVerifier` 是產品記憶批准契約，
code-review 模型的 P1／P2 也不是產品品質 judge。沒有可適用的 confusion matrix／TPR／TNR 或 train／dev／test gold。

**Fix:** 現在不製作或採用整體 helpfulness 1–5 分裁判。若未來建立單一 failure-mode judge，
按 scenario family／conversation 隔離資料，使用 held-out human labels 與 TPR／TNR；
不能把 prompt few-shots 再拿來計校準成績。詳見 [Target State §7](agent-runtime-target-state.md#7-評測起步順序與量測單位)。

其餘 evaluator design 檢查：所查 M4 checks 為明確布林門檻，格式／scope／版本／bytes／tokens 用程式判定；
沒有發現以 ROUGE／embedding similarity／Likert 作主品質指標，這些不列為缺失。

## 4. 本輪實際結果與重現

2026-09-25 **09:10:17–09:10:50 +08:00**，在 Windows／Python 3.12.10 的 worktree-local venv 執行。
產品 code、controller 與 tests 均未修改，執行仍對準 `cb7a2d8`。

| 範圍 | 固定 selectors／展開後 test cases | 原結果 | 實際受測路徑／分支 |
|---|---:|---|---|
| tools | 6／12 | 12 passed | 9 個未配置的 HTTP adapter cases＋2 個 broker core cases＋1 個 legacy profile 拒絕；未驗目前 WorkState effect consumer |
| wakes | 6／8 | 8 passed | 7 個固定工作 delivery cases（`work_policy=None`）＋1 個替身 controller 的 ready observer case |
| lifecycle | 9／14 | 14 passed | 6 個固定工作 worker／delivery cases（`work_policy=None`）＋2 個 progress 順序 cases＋6 個合成 checkpoint cases |
| memory | 11／19 | 19 passed | 治理與原生 SDK serialization＋替身 Memory transport，不是真抽取／recall |
| subagents（受控 checksum 路徑） | 7／15 | 15 passed | 預先配置的 checksum child 控制，不是 SDK 自主 subagent |
| evaluation tooling（原 M4 checker） | 1 檔／34 | 34 passed | Checker 自身測項，不計產品品質 |

合計 **102 passed，0 failed／errors／skipped，pytest 28.21s**。
此耗時是離線 runner 時間，不是 Agent 的 latency、p95 或十倍效能證據。
一項 Starlette `BlockingPortal` alias deprecation warning 保留，未修改程式壓掉它。

**2026-09-25 Opus P2 修正：** 上表補的是原 cases 的 consumer／分支解讀，不是新執行。
Markdown 與 JSON 的 limitations 已同步；執行前 scope 原件、selectors、原 `run` 結果、計數與 hashes
全部保留，沒有換測項後沿用舊 PASS。Wakes 的 7 個與 lifecycle 的 6 個固定分支 cases 共 13 個，
不能合稱目前自由指令路徑通過；tools 也不以未配置 adapter 的 9 個 cases 替代目前效果路徑。
後續若驗現行 consumer，須沿 `operation` API／`WorkStateDispatcher` 及具 `work_policy` 的
自由指令 fixture 另選直接案例、另記結果。此文件修正本身沒有重跑產品測試；之後的定點複核與新批次見下節，
不把後來的結果回填本批。

最初 system Python 因 `No module named pytest` 未收集測試；
其後只安裝 repo 原本的 test／behavior requirements 到本 worktree `.venv`，
並以正式 tiktoken loader 準備固定 cache。這是環境 setup 阻塞，不是產品 test FAIL；
requirements、harness 與已安裝 skills 均未改動。

**重現已選範圍**（在 repository 根目錄；環境須有原 requirements 及固定 tokenizer cache）：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:COPILOT_SKIP_CLI_DOWNLOAD = '1'
$env:TIKTOKEN_CACHE_DIR = Join-Path (Get-Location) '.venv\fa-tokenizer-cache'
$baseline = Get-Content .\docs\agent-runtime-evaluation-baseline.json -Raw -Encoding utf8 | ConvertFrom-Json
$selectors = @($baseline.groups | ForEach-Object { $_.selectors })
$base = Join-Path $env:TEMP ('faeval-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -X utf8 -m pytest @selectors `
  .\tests\context_eval\test_m4_context_eval.py `
  -q -p no:cacheprovider --tb=short --basetemp "\\?\$base"
```

六個 checker probes 另行執行，沒有算進 102 個 pytest cases：
正常對照通過；來源否定／另一專案 owner 仍通過；不存在 citation、拒絕 policy、8193 tokens 被拒。
沒有改 `source_terms`，也沒有用新規則重評歷史 M4。

原件留在本 session artifacts，Git 只保存必要去敏摘要：

| 原件 | SHA-256 |
|---|---|
| `agent-runtime-scope-20260925.json`，執行前 scope、當時 `run=null` | `f236b1bf3ec097af9d4ebe266623c387ce14079c1ce11b5244bc0c877114f290` |
| `agent-runtime-offline-20260925.xml` | `795637d7056ee731854a99cf9145271c8514ab1875481b69670aa977ac8de473` |
| `agent-runtime-offline-20260925.log` | `399ba2635a032306b483c40f825d039bf6bb6e049315fa23fe29a5037c6be7df` |
| `agent-runtime-checker-probes-20260925.json` | `8aead290ab28d2540d2c20dfb35c4bb9153104bd834609f5540c1e9b2bd8778d` |

Scope 原件 hash 不等於填入結果後的 JSON hash；同樣，原帳冊的 working bytes SHA-256
`d22c4c0b8eb756fe662d305a18173a213b1e83acaa2f2b8dda67d2178f616268`
只指本輪讀到的檔案 bytes，不冒稱 Git blob hash。

### 修後定點複核與現行接點新批次

2026-09-25，兩位 `claude-opus-5.5` reviewer 分 Standards／Spec 定點複核上述文件修正，
原兩個 P2 均為 **RESOLVED，各軸 0 P1／0 P2**；不是本次完整 PR review，也不替原協調線完成 PA-02 的產品複核。
Standards 同時確認 selector／case 的 P3 用語已修，另提醒保留原產品 owner 的接續／待命資訊。
固定四檔快照、manifest 及結果原件保存在 `opus55-runtime-p2-rereview-20260925`，
結果 SHA-256 為 `28f1c33f4bf0d2bf52653ad1ac0d52b2d668fe8400245b6e0bb06d9f0043f728`。

隨後依預先固定的新 scope，在 **16:45:17–16:45:52 +08:00**、
同一 `cb7a2d8` 產品基準完成 **22 selectors／33 cases，33 passed、0 failed／errors／skipped，26.79s**：

| 新批次分組 | Cases | 能支持的範圍 |
|---|---:|---|
| configured-effect-component | 14 | 實際 `WorkStateEffectAdapter／WorkStateDispatcher／receiver`，含同 EGT 效果與回條、撤權、unknown／原 ID 核對；使用可修改的 queued fixture，不是已 enqueue 助理工作的正例 |
| runtime-effect-fences | 5 | 經 runtime 接口確認固定／已 enqueue 目標拒絕與原結果保留；此組仍使用 fixed-work fixtures |
| free-instruction-admission-and-wake | 8 | `InstructionSubmit`＋已安裝 `WorkPolicyControl` 的接單、原 ID、獨立 work slots、startup observer 及 progress；executor start 是替身 |
| free-instruction-lifecycle | 6 | 同一自由指令路徑的兩輪保存、取消、failure delivery、checkpoint 缺件／unknown 與 completion 通知；SDK methods 與 provider 回覆為離線替身 |

這次補的是先前欠缺的 consumer／分支證據，不是新模型 traces，也不把 `test_instruction_effect…`
的 model reservation 斷言冒稱 WorkState receiver 操作。完整 selectors 保存在 JSON 的 `follow_up_runs`。
原 102 cases 與本次 33 cases 不合算成品質樣本，亦不改寫原 172-run 帳冊。

| 新原件 | SHA-256 |
|---|---|
| `agent-runtime-current-path-scope-20260925.json` | `f23bf6a517a60379421188f8fc66e32df973e8299cd48c5096dab50cacda11c4` |
| `agent-runtime-current-path-20260925.xml` | `363da95660b56aaf80364c3e97901129ebaecef4f9e0f042689f3599a30856a0` |
| `agent-runtime-current-path-20260925.log` | `1fd0c8e7def9f09212baf397c5b26c85078d6f7d117794939979f437347642ae` |

### TOOL-06 第一個實作切片：安全的 schema 回饋

主線整合基準 `b6b25cd9c03634d93c97ac3ac56014115fbdfb1e` 之後，
在真正的 `run_copilot` callback 路徑重現：instruction 的本機參數 validator 雖產生
`Invalid tool arguments`，卻被共用 `tool_failure` 改成泛用權限拒絕。
這與先前 M4「缺少可操作回饋」同屬需評測的接點，但本次是新程式反例，不冒稱新的真模型失敗樣本。

初版修正讓 `instruction=True` 且本機 validator 產生的 schema 診斷通過，
回傳約束路徑／類型、不含輸入值。Permission 與 invoke 兩條路徑皆在 handler 前檢查，
正確參數仍沿原 handler／當前權限；handler 失敗及偽造診斷字首仍遮罩。
結構化 profile、未知效果不重送、公開 event 不含 raw data 的契約保持。
沒有新 tool、runtime loop、模型請求或批准方式。

| 保存的執行 | 原結果／判讀 |
|---|---|
| 首次新測試收集 | 1 error；測試誤從 SDK top-level 匯入 permission decision。修成現用 `copilot.generated.rpc`；不是產品 RED |
| Callback RED（早期7-case測試版） | 4 failed／3 passed；四案均因 schema 診斷被遮罩，在 `b6b25cd`＋測試 overlay 重現。當時測試檔hash未保存，不能對回最終11-case測試hash |
| 首次修改後合批 | 21 passed／6 failed；四案是新測試誤讀已去敏 event 的 raw `data`，兩案是舊 progress fixture 缺 `assignment` |
| 未修改 base 的兩個 progress 案例 | 同樣 2 failed，`AssistantSession.__init__` 的 `client.assignment.task`；用 `b6b25cd` 的隔離 Git archive 重現，非本次 schema 修正所致 |
| 修後直接範圍 | 25 passed／0 failed／0 skipped，71.06s；11 個 callback 正反例、1 個工具接續、8 個 instruction flow、5 個直接相關 completion cases |

原 27-case 批次的失敗沒有刪除或改稱全綠。兩個既有 progress fixture 案例本次未修改；
後續 targeted 命令明選不含它們的 25 個直接案例，不以略過來宣稱完整 suite 通過。
目前 **TOOL-06 implementation=partial、acceptance=offline_callback_subset**：
只驗 SDK 方法替身搭配實際 FA permission／invoke callbacks，
沒有真 Copilot runtime、模型自行修正品質、人審或 Hosted 採用。
Authority 類別的可操作回饋、完整 TOOL-06 正反矩陣仍未完成。

**Opus Standards P2 後續修正：** `read_skill` 的 schema 包含當前catalog授權，
原 native 契約對撤銷／不在目錄名稱及額外path仍要求`Authority refused…`。
本線選擇**保留該契約**，不更改native斷言：`read_skill`全部本機schema拒絕仍遮罩，
其他instruction的安全診斷才適用旁路。四個新callback反例確實重現初版的enum／additionalProperties字串變更，
修後同樣四案及原十一案共15 passed；再加入工具接續與signed skill API／最後provider gate，合批33 passed、19.96s。
沒有handler或正文讀取，也未放寬任何批准；原25-case及兩批4RED都保留。

這批為`b1d4ababed2285a1f949f93773fd969c0d9d5eef`＋source/test WIP。
修後 `m2_copilot.py` SHA-256為`79488b6fb80ca71925ff89bfe363641fd0e880278b16d777aaec507fc7a774bf`，
測試檔為`9b141c053390fb801c1651b63d6b90d2c739c1d87736b3197a96bbea9a87ea04`。
`test_approved_skill_native.py`及`test_skill_purpose_native.py`保持原樣，本機沒有固定1.0.83 runtime，
所以本次native為**NOT_RUN**，不宣稱其先前結果就是新head的執行結果。

| 原件 | SHA-256 |
|---|---|
| `instruction-tool-feedback-red-20260925.xml` | `bdc4ed49d896ccafe4fff6f8ed63bc7c11621825617ac727c1a0fd52859ecd1b` |
| `instruction-tool-feedback-red-callbacks-20260925.xml` | `a92e2075e1d83b4c71c20c42c310117d819c9ff8558658a78935d5a3bc849834` |
| `instruction-tool-feedback-green-20260925.xml`（檔名不代表通過） | `25bae7236356ef868933e9fd78bd57239bbf3c1246670625f2a3e14655183554` |
| `instruction-progress-base-b6b25cd-20260925.xml` | `0b9102b07441531b0fced702858411f0b2c35720c4dc7b91bbced3be465a8dcc` |
| `instruction-tool-feedback-targeted-20260925.xml` | `34d0eac73bbcd3b22b0ea355cf2fa9764c353a2a51c599d29e9598f98d0ebe7b` |
| `instruction-skill-feedback-p2-red-20260925.xml`（callback內斷言被adapter包裝；4 fail） | `ecfdc612d66d19deaceca1c6f5429581b84108dbd36b1eac8ddb3239583b2975` |
| `instruction-skill-feedback-p2-red-feedback-20260925.xml`（直接字串差異；4 fail） | `e8d4206f8ba218e54eb35035767765397b2ab4c9e0e6db6e8c1efd475577a187` |
| `instruction-skill-feedback-p2-green-20260925.xml`（15 pass） | `916089852b817dc4670c0ecc56c9a1f1cf21260ec138e77ea58ce816684fc905` |
| `instruction-skill-feedback-p2-targeted-20260925.xml`（33 pass） | `d097a0620ef67cb063fac52c2808e3e4720622ee30cadac5041c0e7b7cd7a801` |

前一個25-case批次的驗證是 pre-commit WIP：`m2_copilot.py` working bytes SHA-256
`bf45b0273e6f1f81404687b01c664090c78a4df1eac9884efdcfe17284cb270b`；
新測試檔 SHA-256 `a9439df6c6ca2b1e16933d0fb7bb0c9273d8a6fb37f0d776390eeea40cfb615a`。
後來的 commit 不回填成當時測試基準；API／Hosted image 必須沿原版本／installed-code 契約更新才會生效。

### TOOL-06 PR 審查與合併讀回

**2026-09-25 17:58:14 +08:00，第一切片已正常合併。**
PR #29的精確final head
`24e0da4bb7467ac6de92fb083c5dbd6795618e27`，在base
`0d391245542a1bcb4f91ca3daf0f756b3cb4ec1a`上經同兩位`claude-opus-5.5`
Standards／Spec完整複核，各軸均為 **0 P1／0 P2**；原`52615e2`兩個P2及修正歷史保留。
非阻擋P3不當作已全部修正；未來動態記憶alias schema須由該切片明確決定拒絕回饋語意。

GitHub合併前回報同一head、`MERGEABLE/CLEAN`，沒有回報任何checks；
以正常`--merge --match-head-commit`路徑完成，沒有admin bypass、直接推main或刪分支。
合併commit為`d5e875d0b9bb73824771dc1b005b03d1c33a05d7`，parents正是上述base與head；
tree `2aa169a5a76371b8ca008dd587524ebd129bcc44`與已審head完全相同。
PR body保存final-head雙軸回條；此合併讀回不是新的產品／模型執行。

此段與相應CURRENT-STATE是合併後的接續更新，隨下一個工具配對切片保存，不冒稱已包含在上述已審PR head。
完整Target State、真runtime／Hosted及語意品質仍未完成；不得因這個PR合併把它們標PASS。

### 第二切片：公開工具事件配對

[新契約與分層結果](tool-progress-correlation-evaluation.md)加入安全的`tool_start_seq`，
沿SDK實際註冊工具配對同名交錯呼叫，不公開raw IDs／參數／正文。API、保存progress、
thin-client與UI消費端已接線，舊無欄位資料維持原bytes；不是新的操作權或完整event replay。
最終40 Python、33 minimal-DOM、真瀏覽器合成renderer及後續4案真SDK／離線provider分開保存；
完整native矩陣、Hosted／語意品質仍未驗。
因公開producer／DTO／consumer已改，TC-01在新snapshot先回partial；舊接受run保留。

## 5. 接續與仍未建立的部分

**已完成：** 五類設計／實作盤點、34 個 target 切面、六面向 audit、固定的可重現離線子集，
以及 TOOL-06 的局部 schema feedback 實作與回歸；不是全部 target 實作完成。
這是開始 AI 評測的第一步，不是「AI 品質 baseline 已完成」。

**尚缺：** 覆蓋五類且可合法檢閱的真 agent traces、逐筆 human labels、語意 failure taxonomy、
需要時才建立並校準的 judge，以及適用的真模型／Hosted 驗收。未實作的 routine、自動記憶與自主委派先列能力缺口。

下一個必要接點是按 [Target State §7](agent-runtime-target-state.md#7-評測起步順序與量測單位)
取得／檢閱適用 traces，或對缺資料的已實作路徑產生經批准的新合成執行。
本 worktree 的 `docs`／`tests` 及本 session artifacts 沒有可直接檢閱的完整 agent trace exports，
`.azure` 也不存在；舊兩輪私人原件仍按原索引保留，不到另一工作樹取件，不從摘要重建對話。
因此 `error-discovery` 停在資料入口，尚未建立 review UI 或開始人審。

依 `generate-synthetic-data` 已準備
[20 個三維 tuple 草案](../tests/context_eval/fixtures/agent_runtime_error_discovery_tuples.json)，
維度為 intent／context／challenge，對準已觀察失敗及能力界線。它們**不是生成好的 queries、執行 traces 或 gold labels**；
需先由懂 FA 的人確認情境真實性，再分步生成自然語言輸入、沿實際 pipeline 執行並做 error-discovery。
使用者暫時不在不能視為已確認，也不為沒有資料的畫面填入理想答案。

### 完整實作目標與本工作線範圍

使用者後續交辦完整 Target State 的實作，要求持續維護 evaluation，允許按需要 commit／PR，
並授權必要雲端部署／操作不超過 USD150；另明確補充：

> 補充，pr如果opus 5.5 review結果都沒有p1/p2，你可以直接merge

本線採用完整合併條件：**Opus 5.5 的 Standards／Spec 兩軸審查同一個精確 final head，兩軸皆無P1/P2，
且正常 required checks、hooks與分支保護條件均滿足，才可merge；不得使用admin bypass或直接推main。**
Head一變就重新核對修後final head，舊head或定點文件review不能替新head放行。
使用者的補充批准不等於取消repository既有檢查／保護。

本次五類 34 切面與既有 76／12 的範圍確認當時無人可答，本工作線暫以五類目標及所需既有契約為範圍，
不宣稱已取得將另一條 76 項工作線全部重做或結案的決定。全目標仍未完成。

2026-09-26接續時使用者重新交辦完整Target State，並再次要求先完成最小可用部署。
完整目標與evaluation持續維護，但上節的traces、人審、taxonomy、judge、完整plugin隔離
及額外指標不是部署前置。每次只驗本次變更及最短真實流程；不能指出資料損失、重送或
明顯執行錯誤的缺件，列為非阻擋後續工作，不新增一套gate，也不把未驗項目改成PASS。

USD150 以原 FA 共用累計上限解讀，不重設舊帳或每個 session 各算一份；
2026-09-26接續按最新已回報成本、原預留／未決與本次操作費用估算檢查餘裕，
無法合理控制在上限內才暫緩新增計費；不把未知填零，也不以完整帳單或額外證據包單獨阻擋。
新批准不自動包含 IAM／RBAC、資料刪除、私人資料新用途或排程變更。
既有帳務、未決操作與必要原件保留；按需發布只包含去敏文件／程式，不包含私人 traces、登入資料或原始 SDK state。

## Verified against

- 分批基準：盤點與102／33-case均固定`cb7a2d8b6868e06d9a941a6bdeb77ba399252159`；
  其後整合新主線，TOOL-06產品修正測於`b6b25cd9c03634d93c97ac3ac56014115fbdfb1e`＋明列hash的source/test WIP。
  本交付包含該局部程式修正，不是純文件PR；各批結果不回填為之後的commit。
- 已讀程式：上列實際 SDK entry、Hosted turn、delivery／worker／runtime／server、
  source／claim／native projection 接點，以及對應測試；P2 修正另核
  `build_server → ToolBrokerCore → WorkStateDispatcher → WorkStateEffectReceiver`、
  `operation` API 與 `test_behavior_dispatch`／`test_behavior_delivery`／`test_behavior_workers` 的實際 fixture 分支。
  固定套件由 requirements 與本機 package metadata 分別核對。
- 實際驗證：本節前述 39 selectors＋既有 M4 checker test 檔，102 passed；六個獨立 checker probes；
  新接點22 selectors／33 passed另記，TOOL-06直接範圍25 passed及兩個base重現失敗均保留。
  `context_target_eval.py validate`核過原172及整合後283-run帳冊，原172的run前綴逐筆相同。
- 證據限制：code checks／合成 transport／本機 checksum processes，不是新的 Copilot runtime、
  真模型、Hosted、Azure Memory、完整外部通道或真人標註結果；未重驗舊 private artifacts。
