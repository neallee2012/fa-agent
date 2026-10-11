# SK-12 task-local 隔離 probe：單一 personal source 的限定正反例

日期：2026-09-26。**最新批次 SUBSET_PASS／整體仍 PARTIAL，不能接受完整 SK-12。**
原 native 批次的固定產品base為`6fae3653b404d6740d986e366a1de9659460bf2f`；
16:22接回批次以`c5c687a`加未提交檔案執行；最新#59共同來源雙native重驗為`b66c745`，31個契約仍歸25480f1批次。
本工作只新增probe／callback契約測試，沒有修改FA／SDK／harness／原policy。

## 原 native 批次保持 FAIL

先提交source`334a7f64d685f2b515f4b234557be0f9bc537750`再執行：
13:45:43–13:46:06（UTC+08），**1 failed，pytest18.54s**。
只啟動1個witness SDK/runtime1.0.13／1.0.83，正常stop返回、Windows exit1、force-stop0。
原規劃的FA create及cold resume兩個runtime均 **NOT_RUN**，未追加執行。

已支持的局部事實是：固定SDK的公開typed
`client.rpc.skills.get_discovery_paths(project_paths=[])`回傳task root內唯一preferred
`personal-copilot`位置；驗過邊界後才在`data\skills`建立純文字SKILL。
Public `discover`在不加explicit directories的情況辨識到同canary，未安裝plugin或猜schema。

第一、第二個已保存的callback request SHA相同，沒有native tool／skill事件。
SDK其後報重試，撞上fixture callback上限；最後顯示`WITNESS_REQUEST_BUDGET_EXCEEDED`。
這不是FA隔離洩漏或native loader壞掉的證據，真正第一個native HTTP bridge error仍 **UNKNOWN**。
後續被guard擋住的callback沒有逐筆記入舊proof，不能把兩筆記錄冒充全部callback數。

舊SafeJSON的`canary_name_visible=true`查的是整份request，而user prompt本身就點名canary；
所以它**不是model-facing catalog證據**。原值／hash不改，只更正判讀。
Public discovery仍有效，native正文載入及FA negative尚未成立。

## 零 runtime 的 fixture 修補

| 固定source | 原始結果 | 支持範圍 |
|---|---|---|
| `efd47df04b5ba9568428a40153653f401bc084b5` | 1 PASS／4 FAIL，6.01s | SSE建構正例通過；缺少guards／provenance。WS案例最初先碰到不完整context，原FAIL保留 |
| `c6159da7f8931f1d5a2b0af58b6fe78323e90317` | 2 FAIL，3.58s | 修正兩個oracle後，確認catalog欄缺失及未在forwarder建構前拒WS |
| `a04392094948f4b20580c587e575aed4848a6ba7` | 5 PASS，3.42s | 修後synthetic public callback契約；SDK start、subprocess與HTTP send由fixture禁止 |
| `256e91680c3ee122be1cf98191e3dbb6423a2672` | 12 PASS，4.67s | 同一零runtime檔補HTTP目的地/方法與owned-root lexical／reparse停止護欄；無越界讀取或新SDK |
| `12259580dbd81fc4fca1047a5ca2747b3fab02a4` | 2 FAIL，4.02s | 公開typed retry/error事件原本未記錄；synthetic隱私反例，零runtime |
| `3913a7cb552801a97e828d7a965caa2aff3daccb` | 14 PASS，4.19s | 保留前12案並補safe event提取；free-text reason/type/code/message只存hash，numeric status可用，無stack/URL |

修補只在test-helper：明確拒絕WebSocket／不支持的nonstream格式；先記attempt再辨識重複digest，
不把retry當post-tool新回合；分開tool/system catalog與user prompt中的名稱；
記safe request stream及response status/type/hash。這些修補**沒有重跑或修復原native FAIL的證明**。
不改FA mode/tools/permission，不加強global flags，不默許另用原剩餘兩runtime重試witness。
原Opus Spec唯讀診斷亦未找到高把握的首個HTTP契約根因：witness省略`streaming=True`
只是未證差額，SDK文件說的是delta events，不能直接推原request.stream。
此為來源c1e checkpoint的歷史狀態：observer只為後續診斷保留公開retry/error類型、
safe string hash及numeric status，當時沒有新的native觀察。Zero-runtime GREEN不改寫原native FAIL；
16:29新目標後的實際native結果另列於下方。

## 接續工作樹的零 runtime 整合

2026-09-26T16:22:11–16:22:30+08:00，在本工作樹基準
`c5c687af1041114aad45e8a7d3f1e82bb85e6162`加三個未提交接回檔案執行。
兩個測試檔與本報告在追加本節前，Git blob均與來源
`c1e1ba110cc7dae5a205b1888cb2208bed5d60d7`一致；沒有複製來源工作樹的未提交差異。
執行時的檔案bytes、Git blobs、完整argv、時間及輸出hash另存execution回條。
**受測基準為WIP，`tested_commit`／帳冊baseline均為null**，不倒稱在c1e或clean c5執行。

只選`test_skill_isolation_callback_contract.py`，結果為**14 PASS／0 FAIL／0 ERROR／0 SKIP**，
JUnit 6.711秒；一筆Starlette／AnyIO deprecation warning不當測試失敗或本批已修問題。
重用來源worktree已有的pytest9.0.2／SDK1.0.13測試環境，禁止寫bytecode；
本工作樹原系統Python缺pytest，沒有為此安裝套件或讀取main checkout。
測項只呼叫synthetic callback、path guard及typed public-event observer；
autouse fixture禁止SDK start、subprocess及httpx send，**沒有啟動SDK runtime或provider回合**。

使用既有測試interpreter的選案命令如下；實際獨立`--basetemp`與JUnit路徑留於去敏回條：

```powershell
python -X utf8 -B -m pytest -q --tb=short -p no:cacheprovider `
  tests\shared_context\test_skill_isolation_callback_contract.py
```

新帳冊run為`sk12-takeover-zero-runtime-integration-20260926`，
`kind=evaluation_tooling`、`coverage=none`。492筆保留原491筆完整前綴、
規格快照及既有綁定；所有目標的implementation／acceptance／baseline／blockers及
12個scenario狀態不變。SK-12仍partial，四個已接受C基準不變，
不把本批14個guard案例當產品或完整SK-12通過。

本批JUnit SHA256：`8884f0594a69b747878880bfd429221bb805c0b3775e9d2e57c94694920188cb`；
execution回條SHA256：`84db232beb72b609ecaf8b1ef369643958982e42b3eb22ad9db1b6ab9c175f50`。
原native FAIL、後續六批零runtime紀錄及first-error UNKNOWN均保留，沒有重跑native。

16:22接手檢查時原工作階段仍顯示busy；當時未取得發訊確認，因此未要求它停工、
修改其檔案／程序或宣稱執行所有權已交接。下一個native動作前須確認單一執行者及
新的有界診斷範圍／runtime額度，取得request.stream與公開retry/error的安全觀察；
不把`streaming=True`猜成已證根因，也不挪用原剩餘兩runtime重試witness。
該批未commit、push、發布PR、改排程或執行雲端／計費操作；舊批准沒有自動沿用。

## 16:29新目標後的有界診斷與三階段結果

使用者16:29重新明確要求完整Target State實作、維護evaluation、必要commit／PR、
Opus5.5清P1/P2後正常merge，以及共用累計USD150內的必要雲端操作。
本線只在自己的worktree、named task roots及新scope執行離線診斷；
不修改原工作階段或其#55，不取代它的程序，也沒有新增雲端／付費模型或沿用原剩餘runtime額度。
可靠雲端headroom仍unknown。

| 本批source | 實際JUnit結果 | 秒 | 支持範圍 |
|---|---|---:|---|
| `6cb87c5`，native setup | 1 ERROR | 3.979 | 固定runtime缺件，test body未進入；不是產品隔離失敗 |
| `6cb87c5`，安全診斷 | 1 FAIL | 21.600 | 1個runtime、6個相同digest callback；未要求stream=true，fixture以`WITNESS_NONSTREAM_REQUEST`拒絕 |
| `6cb87c5`＋regression WIP | 1 PASS／8 FAIL，另12 deselected | 4.092 | 非串流完整JSON、嚴格flag形狀與final message的零runtime RED；baseline=null |
| `6cb87c5`＋helper/test WIP | 21 PASS | 4.662 | JSON／SSE分流及原guards皆通過；baseline=null，不倒填後來commit |
| `e012a35`，僅修回應格式 | 1 SKIP，proof=BLOCKED | 6.300 | 1個runtime、2個不同request，已呼叫native `skill`但無正文；exit0不是資格通過 |
| `79b8adf`，公開根目錄對齊 | 22 PASS＝21個contract＋1個native情境 | 18.004 | witness→FA create→cold resume三階段通過，僅單一personal source |

程式／測試固定於
`79b8adf52da0cfe151aea2c59dc37f9a8450891d`，
tree `e7a87579ac35b7bd40b2bea54861d41479736233`；執行時另有文件／帳冊WIP，不稱整棵工作樹clean。
測前核`src/tests/controller`與HEAD一致。SDK1.0.13的client bytes仍等官方固定來源；
runtime1.0.83由既有npm feed補齊，發布已21日、未執行install scripts，兩個binary hash與既有pin一致。

兩項修改都只在fixture，不改產品或SDK：

- 未要求串流時，按官方Chat Completions格式回`chat.completion`／完整`message` JSON；
  `stream=true`仍用原SSE helper。False、null、缺省均有正例，非boolean flag明確拒絕且不保存原值。
- Client的`base_directory`與session的`config_directory`都使用原task-owned `data`根。
  先前public discovery找到`data\skills`，但session另指空`config`根；對齊後才取得正文。
  Witness與FA注入根一致；原FA mode、tools、permission callback及非路徑政策均逐項核對未變。

最新native案在2026-09-26T16:49:18–16:49:40+08:00執行。Witness在沒有
explicit `skill_directories`的情況由public discovery找到canonical `personal-copilot`，
實際出現`skill.invoked`，第二個model request才有canary正文。
**`catalog_name_visible`仍為false**；此flag只掃tools及system訊息，不代表所有role都沒有目錄資訊。
本案是明確點名後的可載入正例，不是隱式選用／初始model catalog品質。
同根FA create與另一process的cold resume各2次scripted callback，均沒有該canary，
但都沿原M3 custom reader讀到目前合法skill的精確version/hash/body。
同一SDK SID、原user events、合法公開history、archive、reply及cost rows保持；沒有新grant或假收件。
FA端的排除判斷由實際工具集只含指定custom tools、原policy快照、合法reader及沒有native
`skill.invoked`共同支持；payload沒出現canary只是輔助觀察，不是獨立拒絕證明。
本案未讓FA的scripted provider強行呼叫未列出的native `skill`，不宣稱已測該拒絕分支。

三個owned runtime均實際stop、Windows exit1、force-stop0；全批6次callback＝witness2＋FA2＋resume2。
這不是真模型／Hosted／Blob證據，也不涵蓋任意ambient根、installed plugins、所有OS、
未完成工作恢復或完整SK-12。原334a第一次HTTP錯誤仍不回填成已證唯一根因。
帳冊新增六筆至498，原492筆與規格／綁定不改；SK-12只更新partial的current baseline至79b，
四個已接受C基準與12個scenario不動。

79b批次JUnit SHA256：`ed509ff6b79a4a56eb64fc6dca80270f60c400f90f06f465662aa57793b8b112`；
safe proof：`7535e050e1fd09a237cacfa6fa6af43bf5f30819c9ec5bad421033f50a27e248`；
execution：`29f4926e7ebb7c0a1665ccb82eefcf3978e242b7409e9ea547a3ee327a81076e`。
每批原件、source/scope、argv、時間及failure/skip分開保存；交付仍依同一final head的兩軸review與正常GitHub gates。

## 初審與資格斷言補強

Opus5.5分別以Standards與Spec軸審`c5c687a...9398c91`，兩軸均**0 P1／0 P2**；
初審只適用9398，不當後續head已審。Standards的一項P3為官方格式來源須固定permalink；
Spec四項P3要求區分payload缺席與政策控制、收窄catalog掃描文案、斷言native invocation，
以及真正讀取SDK版本／將client SHA留進execution回條。這些P3並未否定79b原proof的實際觀察。

已把OpenAI連結固定於`384fee3252e2336a85450ff38f9bae41f9199616`，並明列上述負例與catalog界線。
Fixture在建立task state前讀取並核對SDK package version=1.0.13，proof使用該實測值；
witness runtime停止後必須已觀察到`skill.invoked`，才可進入FA階段。

| 跟進source | 實際結果 | 秒 | 範圍 |
|---|---|---:|---|
| `9398c91`＋version test WIP | 1 FAIL | 6.974 | 未核SDK版本時，合成錯版仍可建立task state；零runtime RED，baseline=null |
| `9398c91`＋guard/test WIP | 22 PASS | 3.442 | 新版本拒絕與原21個契約；baseline=null，不當native資格 |
| `492257fb365ef3927ad3f1e52fffafe5502a54b1` | 23 PASS＝22個contract＋1個native情境 | 17.468 | 原三階段同scope，加版本及invocation斷言；文件WIP不影響固定source |

固定source tree為`392a3bc2953ebcc04bb85a2b5079e58094e4c3fd`，
執行於2026-09-26T17:29:19–17:29:42+08:00。
SDK版本實測1.0.13、client SHA在execution內核原pin；1個witness `skill.invoked`、
3個owned runtime實際stop、6次scripted callback、force0，原保存／current reader要求皆通過。
仍沒有新增FA未列出native tool的強行呼叫反例；不能把重疊的79b／492兩批相加當獨立品質樣本。
新兩筆初審及三筆跟進追加至503，原498筆前綴／規格／綁定保留；
SK-12仍partial，current baseline改為實測492，四個C基準與12情境不變。

492批次JUnit SHA256：`5990d916fe1a1e5ee1f3961a0350dfaca4395b5b3a7bffe884713a4bec8e2223`；
safe proof：`c6a264b29ed4b4baef453fa13357cd69ee3acb8f09fe14c4c64679294cac2d92`；
execution：`1f2d8d72a468981addf1638c4165f30c65c2fe4b082831a92265bb6f6a785087`。
最終head的兩軸回條另外保存於session／PR，不能用9398初審替代，也不為回填review而改舊run。

## 新主線的純資料整合

交付前main已合併#56為`fa494fe2676cbd021a85c89a182de30cc03dbe8e`，新增human operation
rejection及其歷史／unknown修補。本線503冊與main504冊共同前491筆完全相同；
以已發布main504作不可變前綴，再追加本線未發布的12筆成516。原run payload/hash、
規格及entry binding不變，只有本線suffix sequence由491–502改到504–515。
兩份前件與逐筆對照留於`sk12-main-ledger-union-20260926.json`及其引用的session原件；
不宣稱本線503冊仍是516冊的完整前綴。

這是資料／Git整合，不是把492的23P改算到新主線。原線#55已另發布failure-priority P2修補：
callback hint不能把實際例外或安全違規變成BLOCKED／PASS；本線的重現與整合驗證另列於下方。
原本JUnit FAIL與舊proof BLOCKED的差異仍保持原件，不能改寫過去status來補綠。

## 已發布P2的接回與共同來源驗證

原線公開candidate `a92dce4825dafd541f7748370a0e6ceb387d37ed`（修補source f26ad430）
把hint與正式終局分開。本線沒有取其WIP，先將四個真正外層終局反例接到本線整合基準f74，
再次得到1P/3F：安全違規及SDK例外被誤寫BLOCKED，completed flag又能掩蓋之後的例外成SUBSET_PASS。
這是本線實際重現的P2，不因9398兩軸當時0P1/P2而忽略，也不改寫當時review紀錄。

接回修補時保留本線的JSON/SSE、共同根、實測SDK版本及invocation gate；
實際例外清掉stale completion／formal blocker，安全違規優先FAILED，只有真skip路徑為BLOCKED。
Callback原因另存`diagnostic_hint`，`write_proof`也防止violations被蓋過。
比較及記錄真正forward的FA policy；未啟process的stop只算cleanup，不造退出回條或掩蓋budget拒絕。
Native案加上`FA_SK12_NATIVE_SCOPE=approved`測試opt-in；**旗標不是批准**，不改SDK／FA政策或舊額度。

| 整合source | 結果 | 秒 | 支持範圍 |
|---|---|---:|---|
| `f74ea8f`＋終局test WIP | 1 PASS／3 FAIL | 5.056 | 真外層終局RED，零runtime，baseline=null |
| `f74ea8f`＋修補/test WIP | 26 PASS | 3.696 | 新四案與原22案，零runtime，baseline=null |
| 同WIP，獨立opt-in gate | 1 SKIP | 3.500 | 無旗標時在fixtures/body前停止；native NOT_RUN，不是native PASS |
| `7a93cdd3f2ea685fa0ff074b36bd77b2e2c43f6a` | 27 PASS＝26contract＋1native情境 | 16.354 | 已含main fa494fe與P2，乾淨固定source的三階段驗證 |

固定tree `bcbde64e1e8a1fbb2fa7f036bab004b241e82a28`，
2026-09-26T18:03:18–18:03:39+08:00執行。
JUnit與safe proof同為成功：proof=SUBSET_PASS、blocker/hint均null、violations為空。
三個不同owned PID均stop、exit1、force0；未啟runtime的cleanup計數0，
witness `skill.invoked`為1，6次scripted callback，原合法reader／SID／history／archive／reply／cost要求仍通過。
這是新主線上的新批，不重算舊22P／23P，也沒有新增真模型、plugin或未列出native tool的強行呼叫資格。

新四批追加到520，published main504及整合516前綴保持；
SK-12仍partial，最新current baseline指實測7a93，SP20及四個C／12情境不動。
JUnit SHA256：`a21f34d6035ea0135429140f3aaf4862b0ef4e266a865ee56e7f45c967edc266`；
safe proof：`89e56413012cef3576f1dc9410654619d37e3afdb806bd8b8e445c43a8ece4c8`；
execution：`b387faadcf839989c982ee517a3ac6c8690608828f594692a8ed374c6dbd38d8`。
最終head仍交同兩個Opus context複核，不用舊source的review或測試來代替。

## 合併回條與真正未列出tool的失敗反例

39ae最終Standards／Spec均由Opus5.5確認0P1/P2。#57以正常exact-head matching合併為
`395adc39d0c6748d9c86548320e25b875b662366`；讀回parents為fa494fe＋39ae，
tree `4644b8c6f55180e723f528d48f89d82f58901031`與已審head完全相同，沒有admin bypass。
GitHub當時沒有checks；rules/rulesets因plan限制回403，不據此宣稱所有保護都不存在。
新Goal仍持續；合併不是完整Target State或部署接受。

後續獨立切片直接處理原P3保留的缺口：scripted provider在FA create與cold resume各先要求
未列入可用工具的native `skill`，args只指向已由witness證明可載入的同一個惰性canary，
再要求合法M3 `read_skill`。原FA allowlist／policy／permission不變；
既有每work六次model request上限足以容納每turn三次，不改額度。

Observer只對**精確call ID、`skill`名稱及固定canary參數**的startup事件保持被動，
不由probe自己先abort再冒充SDK拒絕；其他非預期工具／參數或FA `skill.invoked`仍觸發violation。
工具call ID及error code只存hash，保留公開typed success/error存在旗標，不保存錯誤原文。
每個FA階段必須有精確配對的`ToolExecutionCompleteData(success=false, error!=None)`，
且沒有canary正文／`skill.invoked`，之後合法reader仍成功。缺機器可讀回條即BLOCKED，
不以自由文案或payload缺席補綠，也不從error hash推定唯一內部拒絕原因。

Canary檔在各階段前後仍為同一owned path及bytes；避免因反例檔已被清掉而得到假陰性。
強行呼叫本身及其歷史會含canary名稱，所以raw name flag可能true；不當成目錄／正文洩漏。
原control保留為獨立情境，新case的proof另用`unlisted-skill-proof.json`，不覆舊原件。

| 新批次 | 結果 | 秒 | 範圍 |
|---|---|---:|---|
| `395adc3`＋observer/test WIP | 31 PASS | 4.627 | 精確被動attempt及typed failure隱私反例，無runtime；baseline=null |
| `25480f160bdde38250891c6139dd9070ed79d23a` | 33 PASS＝31contract＋2native情境 | 31.596 | 同source原控制及新強行呼叫反例，0F/E/skip |

固定tree `eddea0244f9cf5d01f07a7965c48578ccf9b9b56`，
2026-09-26T18:42:44–18:43:20+08:00執行；execution保存測前／後實際git status及測試檔hash。
兩份proof都是SUBSET_PASS、無violation，六個不同owned runtime均實際stop/exit1/force0；
原case六次callback，新case八次。新case兩個FA階段各有一個typed失敗回條，
隨後current M3 reader正常，同SID／原user/history/archive/reply/cost保持。
有`tool.execution_start`只代表嘗試；本次沒有FA `skill.invoked`或canary正文。

帳冊追加前PR兩軸final review、merge回條及新兩批到525；原520前綴不變，
SK12仍partial，current baseline指實測25480f1；其他ambient根／installed plugins／平台及
其他未列出工具仍未驗，四個C及12情境不升級。
新JUnit SHA256：`d7923a4b726578f1cb121de2d497b7fc4427996307e7a4faf3fb26a134be3691`；
原控制proof：`e50891a9f2dcca1312657d5ba3855b9e28f23111d8e4c66bb29a26fb2b0811a1`；
強行呼叫proof：`2bff6463afeb58f7933e8d2391da5c16911e816a95c8f66b585624913cc4787f`；
execution：`cf32d5da6e820e260c1246221efb80c1ea8b23a9ebe92200ef57808ed9bfe887`。

### #58共同來源的接回

新主線`f6ce532e19bd39b574a97ed4c87bb4db2d2c9ca1`已合併反向approval unknown修補，
本線先保main530完整前綴，再把未發布的五筆由sequence520–524接成530–534，共535筆。
原525 payload/hash/spec/entry bindings全部不改；原525不是535的完整前綴。
三方前件及對照保存在`sk12-unlisted-main-union-20260926.json`與其引用的session原件；
SP20 d091及其他主線狀態保留，只有本線SK12沿自己partial資格。
這是Git／資料整合，不把25480f1的33P改算到新主線；共同來源結果另記。

固定`8c33e15e2efa68b3325870216cc87357c6af20c5`／
tree `b5ad098fc02d04023299b100da589d8ed22854cf`後，只重驗兩個native情境：
**2 PASS，30.490秒**，2026-09-26T18:57:37–18:58:12+08:00。
兩份proof均SUBSET_PASS、無blocker／violation；六runtime實際stop、6＋8callback、
強行呼叫仍有兩個精確typed失敗回條，原canary bytes與合法reader／SID／history／reply／cost保持。
兩個test檔與25480f1逐字相同，但不因此把原31個contract計入新執行；execution另存前後git status及檔案hash。
新run追加至536，保main530與整合535前綴，SK12只更新partial current baseline至實測8c33e15。
JUnit SHA256：`3d65b94ce9918a6eb62c2628fa5cefb2eab2bc8812540777cc3662d8c9043d80`；
控制proof：`ed84b79ea9e1ccff1b822da549cba392794c8be4b9ffea4be396972ded4b9466`；
強行呼叫proof：`65fb46fde4ce6e6593b7577e8a58d3725bf83a8e4115b746cde194e7ded7a08f`；
execution：`7662725bcbe414b695ac25033fd3b2ac89a37667bd147471ba670c7df9895029`。

### 正常合併阻擋與#59來源接回

89f96f5最終Standards／Spec均為Opus5.5的0P1/P2，無需修正；共同P3保留：
精確success=true回條日後可明確分成violation，而不是missing-receipt BLOCKED。
固定版本真的載入時既有invocation／body guard已判FAILED，因此本批結果不受影響，不回改舊run。

正常merge時GitHub回CONFLICTING/DIRTY，沒有bypass。讀target branch確認已前進到
`efb7445284a91bbf88802ee9b568e6c9ffa2dd7c`（#59）；PR的baseRefOid/base.sha當時仍顯示f6ce532，
所以合併前還須核真正live target ref，不能只看PR快照。新main包含同action批准決定與原command plan綁定，
本線保持其產品邏輯，將published537作完整前綴，六筆未發布紀錄移至537–542成543。
原536的payload/hash/spec/entry bindings保留，不稱它是543的full-prefix；
三方前件及對照另存`sk12-pr60-latest-main-union-20260926.json`。
這次整合不改兩個SK12 test檔，也不是重跑證據；新的共同來源結果與兩軸複核另記。

固定`b66c7458aee63ba013ad4d55563c291025a2ea3f`／
tree `8c16b865e45e3ca1dafd111a0a78fed6099be54b`後，在2026-09-26T19:28:26–19:29:02+08:00
只重驗同兩個native，**2 PASS／30.457秒**。六owned runtime實際stop、force0，6＋8callback；
新case兩個typed failure仍精確配對，canary／current reader／原保存要求全保持，兩proof均SUBSET_PASS。
測前／後status空、test hashes及SDK/runtime hashes已在execution保存；原31個contract不算本次重跑。
新run追加至544，保published537／整合543前綴；SK12只更新partial到實測b66，其他C／SP20／scenarios不變。
JUnit SHA256：`43a830d0be70e9fcdd97f3a8271e44b43243b37ffe577447b4cd6600e8170744`；
控制proof：`90291414ad3d9a379ca7e0afcc85a8a583f8ec4581f9e4a57d9064eb88cdfdf9`；
強行呼叫proof：`bf6fc32bf95bdeefe4892a17757ddd3994921c16be1e0a14b6da13c64db32b08`；
execution：`40846a6f2d95adce741d1df989276ff0c9a08689d6d352bc4fa920f071077935`。

### #60交付後的接續

#60已正常合併為`2f4cc90`；a44最終兩軸0P1/P2，b66的native結果不重跑或改算來源。
前一工作階段另完成固定SDK的local-path plugin公開契約研究，尚未安裝plugin或啟動新runtime。
查閱2026-09-26固定SDK1.0.13／`f13e4a2`的[plugin文件](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/plugin-directories.md)
與[typed RPC](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L22231-L22260)：
API可接受local path與SKILL.md-only內容，但plugins API仍標為Experimental，local install已deprecated，
不視為穩定的產品安裝接點。Model `request_handler`不約束installer所有網路效果；
install/list回傳也不能直接當cache-root或ambient可見性的證明。

2026-09-26接續重新對齊：此缺口保留NOT_RUN，但**不作目前部署前置**。
本次不新增plugin producer／完整隔離流程，先處理實際部署路徑的必要缺口；
若部署前要啟用plugin，須先補該路徑必要案例；即使未啟用，完整SK-12仍保留
「已安裝但未授權的plugin不混入」驗證，並未從最終目標刪除。

## 2026-09-27：明確成功回條不再降成缺證

在已發布`8b4beaf9ac8cd82a0f40afb754a1a8ea3cffe669`的相同tree上，處理#60保留的
nonblocking判定提醒：當denial probe在FA create／resume取得精確`unlisted-<phase>`的
`tool.execution_complete`且`success=true`時，observer記
`UNLISTED_NATIVE_TOOL_SUCCEEDED`並沿既有公開abort停止；這是矛盾回條／FAIL，
不再到末段被當成missing failure receipt而BLOCKED。沒有回條仍走原缺證處理，
`success=false`及其他call的成功不受影響，沒有改FA產品／工具／permission policy。

| 固定source | 結果 | 證據層級 |
|---|---|---|
| `699e9e9a5d7a8cba10f0b8842f7c04c720f6bac3` | 2 FAIL／4 PASS，5.77s | 已先commit；兩phase的明確成功回條重現未記violation，另四個正對照通過 |
| `a908384f3a513bb69be9d87f5465b05f5de91ff7` | 37 PASS，4.48s | 已先commit；全零runtime contract檔，含六個新增typed-event案例 |

JUnit SHA256：RED `ae14ccde9919039ab2385edbe1df1834880c7c759d00b00f6322611b71b9583d`；
fixed `0d94f34f12cbf46c0f4bd0b12b5b59ef08269aa570bf8fabfc228b45dae39025`。
這是checker／observer限定修補；#57／#60的native來源與原結果保持，沒有重跑SDK/model、
回填舊批次或提升SK-12接受。

## Verified against

- 新檔：`tests/shared_context/test_skill_isolation_native.py` 的root邊界、三runtime budget、
  `WitnessProvider`及original FA create/resume路徑；
  `tests/shared_context/test_skill_isolation_callback_contract.py`只做in-memory公共callback測試。
- 接續批次：上述兩測試檔原樣接回後，在`c5c687a`＋WIP只執行14個callback契約。
  `controller/context_target_eval.py record`追加新run，再用`validate --previous`核原491筆；
  另逐項比較76個target狀態／基準及12個scenario，沒有新的完整接受。
- 後續新批：固定79b的21 contract＋1 native情境22 PASS；前面的環境錯誤、RED及BLOCKED均另入帳。
  SDK client SHA256仍為`7cb524f79cf8431399b8292eeccb999ddf26e3b9c32c252680a29c991c55e8b9`。
- Review跟進：固定492的22 contract＋1 native情境23 PASS；版本為真正讀取，invocation已有明確gate，
  SDK client SHA留於本批execution。沒有改產品／SDK policy或擴大ambient/plugin資格。
- 共同來源：f74純整合後重現並接回已發布P2，固定7a93的26contract＋1native共27 PASS；
  `src`與已發布main fa494fe一致，差異只有本線probe／證據，不修改操作拒絕產品邏輯。
- 強行呼叫：固定25480f1的31contract＋兩native共33 PASS。公開typed事件依固定SDK
  `session_events.ToolExecutionStartData`／`ToolExecutionCompleteData`；available_tools作用於整個合併工具集，
  且優先於excluded_tools（同版client.py:2361–2369）。本批以失敗回條核實，不只讀該配置描述。
- #58整合：固定8c33e15只重跑兩native，2 PASS；test bytes與254相同，src保持published main f6ce532，
  不修改反向approval unknown產品邏輯，不將原31個contract倒稱在此source執行。
- #59整合：固定b66c745只重跑兩native，2 PASS；同test bytes／新main efb7445產品接點，
  舊review與舊535／536的歸屬不回填，合併需另核live target ref及新final head。
- 官方wire契約（查閱2026-09-26）：OpenAI的
  [`CompletionCreateParamsNonStreaming/Streaming`](https://github.com/openai/openai-python/blob/384fee3252e2336a85450ff38f9bae41f9199616/src/openai/types/chat/completion_create_params.py)
  與[`ChatCompletion`](https://github.com/openai/openai-python/blob/384fee3252e2336a85450ff38f9bae41f9199616/src/openai/types/chat/chat_completion.py)；
  只用其JSON／SSE格式，不據最新套件推定固定SDK的新能力。
- SDK固定source為[`f13e4a2`](https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e)：
  public discovery型別／方法見`python/copilot/generated/rpc.py:13065–13090,24340–24370,40882–40886`。
  `CopilotClient.rpc`是公開typed server API，未呼raw RPC或私有CLI flag。
- 同版`copilot_request_handler.py:719–738`明確支援buffered response，因此未把本次錯誤
  直接歸因為buffered httpx Response。原first-error／request-stream細節不足，不能補造根因。
- 原native JUnit SHA256：`51f8c86d83c493ccf67f629a1373ee245654586cd86d0c1815faa973c8e0d58d`；
  SafeJSON：`a2caa7bf0ebfe1118de3fcba663e3e0d9311f1e1cca9d113216f2d8c653d1f67`。
  Zero-runtime fixed JUnit：`29ff7322482e3ccdcd811e2fbfc388a1cfb6eb34743b5677e676b8e184d6725f`。
  擴充guard JUnit：`d82244a2564b4ee4f02487602f18beb660cb96ff02446017e6da4f20ddc22a4a`。
  Safe-event fixed JUnit：`501e5994b3261e15e539c3f5c0e77c01776782f8bab4d6e58faef682f00e172d`。
- 完整argv／日期／source/tree／RED與fixed結果分開留session原件，沒有回填先前source-only報告。
  沒有讀寫真HOME或harness skills／settings；無live model、cloud、paid、plugin install、
  arbitrary script或SK06 deactivation重驗。各native批次均有自己的明確scope／counter上限。
