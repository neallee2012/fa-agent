# 完整 Target State：自主實作與 evaluation 狀態

開始日期：2026-09-24。目標是[完整 76 項 Target State](grok-aligned-target-state.md)的**實作及相應驗收**，不是只完成規格文字或四個 CTX 候選。

使用者於 2026-09-24T13:50:10+08:00 已授權自主推進完整實作、離線驗證及分批 PR，不必為每個一般開發步驟重問 Go；同日另授權必要雲端部署與設定、上限 USD150，詳見[共同預算](#雲端授權與共同預算)。**IAM／RBAC 權限變更及額外付費推論不自動包含在此授權內**；未涵蓋的私人資料新用途、刪除或其他真實對外效果仍須適用批准。未驗項目不能當 N/A 後宣稱全規格通過。

需要人工語意判讀的條目會保留待人審狀態，不由 AI 自填人工 gold；可以先完成其他程式與離線檢查，不因此停止所有工作。

## 文字來源附件

PA-08新增明確opt-in，真人只可選原binding可讀的Team source/version/hash，不接任意path或URL。
原WorkRegistration保存參照，重送不換source；恢復及model gates沿完整必要來源圖核withdrawal／expiry。
原生SDK用完整selection/text送入，原user prompt保持；Context只回歷史參照，未新增模型或UI授權。

開發WIP分批保持：首批10 PASS／1 FAIL是測試把request ID中的「attachments」誤當新增欄位，
修正為結構化欄位判斷；原生首案FAIL是省略selection時SDK事件反序列化不相容，並未取得
模型本文判定；提供完整正式範圍後1 PASS，2runtime／2scripted provider requests且冷恢復保留本文。
擴控制時15 PASS／2 FAIL是fixture在standing安裝後試圖修改已受限source，沒有放寬產品，
改在安裝前準備來源後同17案PASS。與default/原序列化/template相容合批36 PASS；
最後必要來源withdrawal反例加入後，直接附件檔17 PASS。

以上不是同一輪，不相加為品質樣本；沒有Azure、真模型、檔案路徑讀取或使用者journal操作。
目前只涵蓋既有8KiB文字來源、same Team與已配置source IDs，不是images／binary、任意上傳或
完整PA-08／模型理解品質。原件為本session的`source-attachments-{first,native,native-range,controls,controls2,regression,graph}.xml`。

### Verified against（文字附件）

- `behavior_attachment_source`重用原SourceMaterials graph/receipt/withdrawal；`behavior_work_policy`捕獲、
  `behavior_instruction`與`AssistantAuthority.model_sources`在原SDK恢復／egress前核對。
- `m2_copilot`正式send的selection欄位、原生事件reader，以及API-only DTO／template／Context consumer；
  實際SDK只1.0.13/runtime1.0.83加scripted transport，不是Hosted或自然模型。

### 固定source 5c6f60c

2026-09-27T16:22:04+08:00於`5c6f60c790a8d037bf9b9599fde53af311d6322c`執行
兩個直接檔，**18 PASS／0 FAIL／0 ERROR／0 SKIP**，20.464秒。
17案為local API／source authority／SDK I/O替身，1案使用真正固定SDK的兩個process、
兩次scripted provider請求與同SID冷恢復；原prompt與附件text送入SDK相符，且無同名實體檔。
JUnit `source-attachments-fixed.xml` SHA256
`7be16d85a4b181ec6c5b0af15c3a6e763103c01578c78ec77dfd2940d3565878`。
原36案回歸／第一個缺selection的native FAIL維持原WIP，不倒稱此來源重跑。
接#91的整合未改附件程式／測項，PA-08僅partial、尚未部署，既有接受狀態不升級。

### 空附件不觸發未使用的source限制

接續#94 review P3，在未改產品的已發布c18重現1 FAIL／1 PASS：
普通指令沒有附件，卻因設定的source visibility是private而先被ATTACHMENT_SOURCE_DENIED；
真正帶private附件的拒絕負例原本通過。
現在僅在空來源集合直接回空結果，不做source讀取；Human/原policy檢查及非空的Team限制保持。
直接附件檔19 PASS（WIP），原source graph／expiry／withdrawal／相同ID控制也保留。
`empty-attachments-red.xml`與`empty-attachments-green.xml`留存，沒有新native／cloud／model或新授權。
固定`89555155eacf844a9528cc83eca15ab82a6980b2`於2026-09-27T16:54:26+08:00只補兩個
直接正反例，2 PASS／0 FAIL／0 ERROR／0 SKIP，5.838秒。JUnit `empty-attachments-fixed.xml`
SHA256 `71cbd508be87ba746acb3cf85905e8c72afa0db515fdd43a34790f4cc0aaec22`；
原5c6的18案/native與本次scope不互換，帳冊資格不因兩行修補升級。

## 狀態來源

機器可讀帳冊：[context-evaluation-status.json](context-evaluation-status.json)。Schema 2 分別保存 **76 個目標、12 個跨回合情境**的狀態、當前程式基準、相關 run IDs 與阻塞原因；兩種完成計數分開，不把分母改成模糊的「88 項功能」。

帳冊目前**769筆**：保#116已發布756完整前綴，再接38採用、真冷續行、三規則案例、大狀態與初期fixture失敗13筆。
這13筆早期整合由sequence735–747移至737–749；初審後另依P2更正五筆未發布來源／敘述metadata與hash。
原748／初審750／P2修正750／已審764及對照另存；本次只把已審未發布sequence751–763移至756–768，
修正後payload／hash／spec不改，已發布756不變。新literal profile／PA-08不承接本線TC-11基準驗收。
[修後資格](../src/fa-shared-context-poc/README.md#2026-09-27hosted38採用真冷續行與tc-11重新接受)中，
Windows raw162719／compressed43536在精確fe5來源跨process保原identity；
Linux raw162719／compressed43524及Hosted38冷續行則各掛實際core95db7749，是另baseline佐證、不算fe5執行。
TC-11只依Windows精確執行及相同拒絕回歸的C條件accepted atfe5，歷史0680及36／37失敗不變；完整接受**2／76、0／12**。
SP-31／PA-15／E-09只有固定題partial證據；1200同時受本輪無單位要求，未驗後續撤回效力或人審full。Linux額外API依賴僅在暫存fixture。

前一**756筆**保#113／#114已發布754及該線755完整前綴，[原user-file回讀](../src/fa-shared-context-poc/README.md#壓縮後回讀原user檔獨立profilelocal)原c679的15案保持，再加68508ce的新profile固定hash／literal capability兩案。
兩案是本機互通，不是#113 UI／browser／native重跑；input f438與read c679的原限制、未部署狀態分開保留。
PA-08仍partial，新增同CID過去原檔／既有provenance及currentread接線；13local＋API-only cold import＋1真SDK壓縮後取回原文的來源分清，不升完整模型理解或雲端資格，其他target/scenario保持。
前754筆保#112已發布751完整前綴，追加terminal Unicode可讀性原1F/1P、
修後2P與固定4618495的8P三筆。只改stdout安全呈現，原request／attempt／hash／stderr
與server／操作語意不變；所有target／baseline／scenario狀態保持，不以顯示修補提升SP-20接受。

前一**751筆**保#111已發布746完整前綴，追加revoke CLI原五批：缺命令RED／首例、
ede3175原96案、ab265限額／late-auth三案及7163整合互通三案，結果與來源分開。
SP-20仍partial/currentede3175，只增加既有server revoke/歷史查回的human CLI consumer；
原grant／revision凍結且不取消、退款或逆轉effects，server規則不變。Unknown-operation SDK、
other-target binding等原blockers完整保留；其他target／scenario及current完整1／76、0／12不改。
沒有live grant/revoke／SDK/model/cloud，也不把相關C完整接受當部署gate。

前一**746筆**保#110已發布745完整前綴，再接[原回合範圍讀取](../src/fa-shared-context-poc/README.md#原回合讀取的範圍化重新核對)固定cdc95d1七案。
本線未發布sequence737改為745，原payload／hash／spec保持；舊738候選不是新冊前綴。
只替SP-13補子集evidence；cached ID不是權限，現行record／owner／policy仍核，三個warm路徑只讀原registration而不讀八個無關registration。不是總HTTP、雲端延遲或10倍聲稱，所有state/baseline不改。
前745筆保#109已發布737完整前綴，追加原操作approve CLI的八批原結果；
首送decoder錯配、舊help期待與bool限制正規化失敗各自保留，固定c467為63 PASS，
整合109後03b81另2 PASS，不重跑或改綁。SP-20仍partial atc467，只完成原批准API的
human CLI consumer／完整預覽／immutable attempt／原receipt查回；server權限、
ControlRequest、grant／execute規則不改。所有其他target／scenario及TC-11 partial保持，
current完整接受仍1／76、0／12，沒有新native／live登入／雲端或review rows。

前一**737筆**保#107已發布735及其736完整前綴，[原文檔輸入](../src/fa-shared-context-poc/README.md#小型utf-8原文檔輸入local未部署)原f438的17案保持，再接572ea74的直接preflight兩案。
過量／同名file輸入在入口回422、無登錄或成本寫入；原兩個ValidationError反例保留。PA-08仍partial atf438，其他target/scenario與TC-11原狀態不變，沒有新SDK／雲端或真模型操作。
前735筆保#106已發布733完整前綴，新增原生SDK origin wire單例08033da及
後續observer拒絕標記的零runtime五案a57ca2c。前者證一個SDK新session把真正tool結果
帶到下一個scripted provider請求，不證模型理解；後者沒有重跑native，不能回填原proof。
只有PA-01 evidence追加，所有target／scenario狀態與current baseline保持，current完整接受
仍1／76、0／12；不借此抬高其他來源producer、Linux恢復或雲端資格。
本線原733候選不是新聯集前綴；兩筆payload／hash不變，只將未發布sequence731–732移到733–734。

前一**733筆**保#105已發布731及其732完整前綴，[原對話附件metadata目錄](../src/fa-shared-context-poc/README.md#原有權限文字來源的附件local預設-off)原a1d九案保持，再接e68封存／legacy／pure-read修補五案。
PA-08只加子集evidence、原baseline及native資格不變；整合41da兩案及封存反例FAIL各守來源，不合算為品質樣本。其他target／scenario狀態及TC-11 currentpartial保持。
前731筆保#104已發布725完整前綴，追加原human選版的captured origin設定五批：
選版API422 RED、capture1 PASS、callback2 PASS、固定3444的18 PASS、整合互通9ead的1 PASS。
只有新conversation捕獲非None版本值才讓Hosted `read_context`使用origin API；
舊對話、原request重送、default ENV與selector變化不重選原設定，工具／prefix／權限不改。
I/O fixture驗原permission／SDK Tool.handler返回，不是native模型吸收來源資訊的證據。
PA-01仍partial/current3444；其他target／scenario狀態尤其#103的TC-11 partial保持，
current完整接受仍1／76、0／12。不新增review row，不把原18案改綁本文件來源。
本線未發布五筆binding sequence從723–727移到725–729，payload／SHA／spec綁定不改；
原728候選另存，不稱為新聯集的完整前綴。#101 typed選版回條／UI與#104原resource結果全保留。
再記`18fc885`單一shared selector parser／typed API回條／新Task capture互通1 PASS，
沒有browser／DOM、model／SDK或live資格；原730各run完全保留，target／scenario狀態不改。

前一**725筆**保#103已發布723及其724完整前綴，接回[resource prefix／公開投影五案](prompt-assembly-evaluation.md#2026-09-27-resource-profile-的穩定性與公開投影)原固定84417，
再記[workspace修補單案相容性](prompt-assembly-evaluation.md#接回workspace修補的單案相容性)實測e9d760d；Windows checkpoint單案不當Linux或原五案重驗。
本線未發布run只移sequence706→723，payload／hash／規格綁定不改；原707候選不是新冊前綴。
四個resource路徑與一個原purpose控制只是增量，不恢復PA-02／TC-01或TC-11 C；原84417結果未包含後來workspace產品修補。
前723筆保已發布706完整前綴，再接SDK workspace的live36／37失敗、平台對照、固定修補及舊metadata觀察17筆。
只將未發布binding sequence705–721移至706–722，payload／hash／spec綁定保持；原722冊另存，不稱它是新冊前綴。
原request不重送、不同Hosted版本不混稱同源；[Linux根因與候選](../src/fa-shared-context-poc/README.md#sdk-虛擬工作目錄與舊狀態相容)
已驗新虛擬cwd及舊狀態stat-only相容，原checkpoint不改。38案與Linux四phase各保來源，尚未修後live接受。
TC-11原0680d59接受run保持，但current改為partial at2c9c254；完整接受現為**1／76、0／12**，
不是刪除舊能力或要求完整矩陣才部署。SP-31／PA-15及E-09不因一次marker或平台恢復案例取得語意full資格。

主線706筆保#100已發布705完整前綴，追加原對話follow-up來源metadata的固定45d2cff兩案。
只測第一回合由SDK I/O double保存、第二回合API讀取及previous錯配拒絕，不啟動第二個SDK／模型，
不是另線Linux resume故障的重現或修復。沒有產品碼變更，PA-01仍partial atca628，
所有target／scenario狀態不變；原34／選版interop1及雲端來源不混算，不新增review row。
前一**705筆**保#99已發布704完整前綴，只追加[附件來源目錄](../src/fa-shared-context-poc/README.md#原有權限文字來源的附件local預設-off)的固定2634310十三個metadata案例。
PA-08只新增子集evidence，原5c6f baseline／native冷恢復結果不變；其他target/scenario狀態不改，目錄列出不是授權或本文已驗證。
前704筆保#98已發布698完整前綴，追加[原交辦來源Context](../src/fa-shared-context-poc/README.md#原交辦來源的版本化-contextlocal程式化-opt-in)
六筆原API／client／codec與選版相容批次。PA-01由未盤點改為partial at實測ca628ca，
只描述已登錄human交辦的原request／conversation／work關係，不輸出原OID或授予權限。
舊context()／Hostedcallback原樣，新client需明確include_origin=True；routine／agent／tool來源
producer、完整actor/address、模型預設接收與native／雲端品質仍未驗。
原34案與整合#98後的單一1案保持各自source，不升full Target；其餘target／scenario與
2／76、0／12完整接受不變，不新增review row。profile writer／derive／history未被此功能修改。

前一**698筆**保#97已發布696及其697完整前綴，包含[新對話template選擇](../src/fa-shared-context-poc/README.md#真人選擇新對話設定版本local未部署)原c1a14案，
再加[原ID／preflight修補](../src/fa-shared-context-poc/README.md#初審修補原id核對及preflight)固定b88六案。SK-11仍partial atb88，原c1a不回填。
原policy／已有對話／操作grant不變，完整Window2 fixture與live採用未驗，其他狀態及2個C／0情境保持。
前696筆保#95／#96的693筆完整前綴，追加probe例外結果的原6 FAIL、
修後6 PASS及固定`425ed87`二十二PASS。只改HostedClient.request兩個exception message，
原unknown／denial分類、response解析與重試規則不改；SP-29仍partial at最新425，
先前CLI／backup／環境來源不重綁，其他target／scenario與2／76、0／12完整接受保持。
沒有真人登入或現場第二回合資格，也未加review row。人類M3 CLI不是pending artifact owner，
不新增一個必定403的reconcile入口或借用agent token；原client.reconcile權限保持。

前一**693筆**保#94已發布690完整前綴，追加環境公開錯誤原4 FAIL、修後4 PASS、
固定`463c186`十四PASS三批。SP-29仍partial，current指這個最新OS／工具錯誤projection子集合；
原CLI／backup結果維持原source，不冒稱本次全重驗。僅刪原diagnostic／例外鏈、保錯誤碼與
整數errno／returncode；GUI診斷物件、內層Problem與global SDK logs仍未全面驗。
原PA-08與其他target／scenario狀態保持，完整接受仍**2／76、0／12**，不增加review row。
實測後先純Git接`c18bfc6`的附件來源，環境code/tests逐字相同，沒有SDK／雲端或新模型執行。

前一**690筆**保#93已發布689完整前綴，接回原[文字來源附件18案](#固定source-5c6f60c)，PA-08仍partial at5c6f。
附件run只將未發布sequence686移至689，payload／hash／spec綁定不變，原687不是新冊完整前綴。
主線SP-29／TC-12等狀態與baseline保持，accepted C仍2／76、完整情境0／12，不以local替身或native接線當模型品質。
前689筆保已發布`3d576ff`（#91）的686筆完整前綴，追加備份公開錯誤診斷
`e32d91a`六FAIL、`33df666`六PASS與固定`6be9e42`二十五PASS。SP-29仍partial，
current指最新備份子集合實測6be9e42；原CLI的69ebb66／7fd0d3e各自來源與限制保持，
不稱已在新source重驗全部CLI。固定公開錯誤／有限上游code與原reference保持，不改備份協定、
global logs或財務政策；真MI／Hosted／Azure SDK內部log與其他秘密出口仍未驗。
其餘target／scenario狀態與完整接受**2／76、0／12**不變，沒有新增review row或人工gold。
本線原677候選另存；三個未發布binding的sequence從674–676移到686–688，
run payload／SHA／規格綁定與受測source都不變，不稱舊677冊是新689冊的完整前綴。
本次整合不重跑備份或M3；#91原P2修補與39／4案結果保持各自來源。

前一**686筆**保已發布674及其680完整前綴；先接原#90 review／merge與M3 client四批，
再記首審兩軸同一P2及五個修補／整合批。原None欄位注入反例2 FAIL後按完整欄位比對，
固定c5的39 PASS與397共同來源4 PASS分開；中間wrapper失敗的0測項不當PASS。
六筆原run的payload／SHA／spec綁定不改，只有未發布sequence670–675移至674–679；
原676冊與對照另存，不稱為新680的完整前綴。
新[client核對Interface](../src/fa-shared-context-poc/README.md#原-pending-upload-的-client-核對)只完成原pending版本metadata、
不重傳bytes，TC-12僅追加子集證據，各current baseline與2個C／12情境保持。
主線674筆保已發布`be2a0ad`的670筆完整前綴，追加M3 CLI登入診斷原2 FAIL、
修後8 PASS、固定`69ebb66`的33 PASS及`7fd0d3e`的late-log10 PASS四批。原33案不能獨證
late child分支，後者用無pytest handler的冷process正反對照與caplog斷言補足，原結果不回填。
真credential methods以離線token I/O stub核
SDK warning及CLI error JSON不洩原始canary；共用原operation CLI guard並保logger狀態恢復，
不是更改登入／批准或技能文字政策。SP-29只列CLI診斷partial at69ebb66，其他secret輸出路徑、
Hosted及live OAuth仍未驗；其餘target／scenario狀態不變，完整接受仍**2／76、0／12**。
功能／測試／直接文件與這四筆eval一起交付，不增加review row，不回填各原source。

前一**670筆**以已發布`a47e83d`的651筆為完整前綴，接回五個已合併切片的
19筆原執行紀錄。這次只核原JUnit／log／inputs／execution與source tree／規格綁定，
沒有重新執行產品、SDK、模型或雲端，也不新增review紀錄或人工gold。
原run payload／SHA／sequence、規格、所有target／scenario的implementation、acceptance、
current baseline及blockers保持；只追加新run與對應evidence ID，仍為**2／76、0／12**完整接受。
審查期間#90另發布8筆，原643前綴保持；本線未發布19筆只將sequence643–661後移至651–669，
payload／SHA／規格綁定不變。原662候選另存，不稱它是新670冊的完整前綴；
SP-13／PA-16依#90維持partial及實測547基準，不用較早的本線候選覆蓋。

| 已合併切片 | 新接回的歷史批次與來源 |
|---|---|
| #78 原批准／撤銷回條 | 3筆：`3b6cd03`兩個404 FAIL、`f69c4f9`三PASS、`9418cf6`四十五PASS；歷史回條不恢復grant |
| #81 目前決定觀察 | 4筆：404、首PASS、`d1a2ca3`十三PASS／六FAIL、`6077d08`二十四PASS；六FAIL是內部／公開錯碼oracle差異，不是六個漏擋缺陷 |
| #83 CLI state | 4筆：parser RED、首三PASS、`66903be`二十二PASS及`7ecc6bc`精確七PASS；原nested-private負例亦缺必要欄位，後者才單獨核extra-field，不能合算29案 |
| #86 SSE framing | 6筆：五個產品RED、最小五PASS、兩批ledger讀法fixture FAIL、`af9c6fd`二十八PASS及同source串流／deadline三PASS；未知token未填零，不改費率或原用量回條 |
| #87 M3 module入口 | 2筆：`664fff1`兩個silent-noop RED、`d6fbdba`十PASS；只補`python -m`入口，不改M3規則或執行真決定 |

各筆附原件opaque refs及SHA，直接報告仍在[Shared Context README](../src/fa-shared-context-poc/README.md)；
原19批有重疊，不相加成獨立品質樣本。#76十一批已在原643前綴中，不重複加入。
SSE六筆以修正文件範圍P2後的`bbfea72`作最終報告指標；原handoff仍保留`c1c2c27`，
受測source、checks與原件hash不改。這只修尚未合併的候選metadata，不修改任何已發布run。
這是source-only資料接回；現場仍依原部署線的`receipt-0927`／`cb0`、`51d`／Hosted36紀錄，
不因登錄更多本機結果宣稱#81／#83／#86已採用，未驗能力與完整Target仍保持開放。

前一651筆保已發布643完整前綴，追加原#85後續review／merge回條及原回合容量五批；
SP-13／PA-16仍partial，current指實測547；容量只沿原登錄snapshot，不是provider用量或語意換題驗收。
前一643筆保已發布633及本線641完整前綴，接回原#79 final／merge與文字下載六批，共八筆，
另記361兩軸0P1/P2及8c64共同來源三案。
只有未發布binding sequence628前件的620–627移至633–640，payload／hash／spec綁定不變，
原628冊另存，不稱為新冊完整前綴。SP-13仍partial at8c64，TC-12原baseline保持；下載未採用。
主線633先保620及其632前件，接回原11筆[操作拒絕execution](../src/fa-shared-context-poc/README.md#2026-09-27原拒絕回條唯讀查詢與操作者-clilocal未部署)、
[SK-07固定26案](runtime-skills-evaluation.md#固定source-24eb7cb)，再加[正式provider修補六案](runtime-skills-evaluation.md#sk-07-review正式provider與目前資格)。
原12筆只移sequence607–618→620–631，payload／hash／spec綁定不變；原619不是新冊完整前綴。SK-07仍partial at9e98、native仍24eb。
主線取消回條／入口採用／dfec review及ada共同來源全部保留，不回填為文字下載的新測試。
PA-11仍partial atd02，SK-07仍partial；新profile加入後current PA-02／TC-01依review回partial，
原c8e接受run／baseline保留，目前accepted C是TC-11／SK-06兩項，不加完整矩陣部署gate。
前一607筆保#73已發布604完整前綴，接回本線原[SK-03／04固定35PASS](runtime-skills-evaluation.md#固定source-9b634fa)
與#72的[SK-09 API4案](runtime-skills-evaluation.md#sk-09回合中正式停用後的終局與新回合接續)，再補[預算review修後4案](runtime-skills-evaluation.md#review修補中文點名與接單前可行性)。
SK-03／04仍partial atdd6e、native仍9b634；SK-09仍partial at4c2a，不把兩種API／native資格互換。
兩筆未發布run只移sequence579／580→604／605，payload／hash／spec綁定不變；原581不稱為新冊完整前綴。
主線604的聊天停止review與修補／共同UI結果全部保留，
不覆寫[先前分批](../src/fa-shared-context-poc/web_test_console/README.md#2026-09-27-分批結果與瀏覽器閉環)。
SP-13仍partial，current baseline指實測3da；TC-12僅新增子集證據，原baseline、四個C與12情境保持；未更新現場工作台。
前一579筆保#70已發布578完整前綴，追加原[模板default consumer固定4PASS](../src/fa-shared-context-poc/README.md#2026-09-27-模板default-consumer接續)，SK-11仍partial、來源3bc。
主線Context重連的5 FAIL／8 PASS／固定42 PASS三批保持原件，PA-16原基準不變；UI本機結果不當新部署。
當時未發布的template run僅移sequence575→578，payload／hash／spec綁定不變，不稱原576是新冊完整前綴。
前一575批保#64–#66沿用的已發布573完整前綴，另有[新default固定17PASS](prompt-assembly-evaluation.md#固定source-4f35fb9)
及[review修補API17PASS](prompt-assembly-evaluation.md#2026-09-27-review跟進)。SP-00仍partial，baseline為90a，
native仍歸4f35；4個C與12情境不變。這些是本機接線結果；後來[新對話default雲端採用](progress.md#2026-09-27新對話v1-default已最小採用)另記，不回填此批。
前一573批保#63的570前綴，新增human-rejection最後兩軸確認與native grant lifecycle批次。
先前#61接回10筆未合併紀錄時，payload／hash／規格／entry綁定不變，只把sequence537–546移至544–553；
原547冊另存，不稱它是新冊的完整前綴。SK12採主線實測b66的partial；整合554時PA16／TC16仍指eb047，現在維持修後實測ce1c。
先前#58、#60與SK12各批suffix整合沿原紀錄保留，不重排已發布main或回填新受測來源。
[受控copy](sop-skill-copy-evaluation.md)
只有SK-10前置partial，不是fullSkillify。第八profile初次271checks在68f全部PASS，
但Spec發現完整工具目錄每work變動，拒絕PA-02 C；不能以兩欄正規化模板代替原判準。
該未發布候選已更正為subset，原271數據保留。最小兩行修後c8e已完整同scope再跑271全過，
fullcanonical catalog沒有排除欄位。最終資料head ef7經原兩Opus5.5各0P1/P2且明確建議後，
已把PA-02／TC-01 current接受恢復於實測c8e，非文件head。
TC-01的68f範圍原先獲Spec建議接受，但不替代新的c8e歸屬；依Standards的來源P2，
兩筆尚未發布的68f候選均保留實際checks但列historical/subset。新strict candidate均指c8e；
兩個source／data裁決各自留存，不再跑或改寫已完成結果。舊cb4／0dac接受保留歷史。
SK-07之前PA-02／TC-01／TC-11／SK-06為4／76；其原接受run與baseline不改。
新resource profile尚未取得共同prefix／projection資格，目前 **2／76、scenario0／12**，
由TC-11／SK-06維持接受；PA-02／TC-01回partial，不把範圍擴張冒稱已驗，也不重跑無關gate。

### 已保存答案的文字下載

每則已保存回覆可明確下載UTF-8正文，沿既有current-read／conversation／reply SHA與身份核對，
不使用cached answer繞過撤權、不下載preview、不重新生成或ACK。空白／Unicode／CRLF保持，
新檔SHA只標此文字bytes，原完整JSON的下載與hash不變。
原cb0的兩個新route反例404，WIP API11P；UI3P/4F是mock未點擊計數oracle，修後7P。
固定de5d92c在保留#80操作UI後，Python14P／DOM76P分批；接#83獨立CLI時相關檔案未變，
沒有重算測試來源。新功能尚未在原8765採用，也不是一般canvas或全SP-13完成。
接#82時下載/UI/fixture檔仍未變；固定8c64只核文字/重啟/SDK-free三案3P。
新主線PA-02／TC-01 current回partial的裁決原樣保留，不用此下載子集重建完整接受。

### 聊天工作台的人工停止接點

後續[唯讀核對原取消回條](../src/fa-shared-context-poc/web_test_console/README.md#原停止回條的唯讀核對)
由current human/read與一致snapshot，重用原cancel request builder／store reconcile，
只查同job/generation/context/config的控制回條。Missing仍UNKNOWN，世代／request錯配不換新ID，
原journal UNKNOWN不改；UI新觀察獨立、純GET且不自動retry／cancel／ACK。
原四案404 RED、各WIP green保持；固定20861a9的Python62／UI62是不同兩批，
包含原取消writer和store只讀控制，不外推124獨立品質樣本、live、跨世代核對或完整TC-12。
原部署線11:01的停止入口採用另保；12:42已採用新reader／client，只有原資料與拒絕分支讀回，
沒有發真cancel或live原回條成功宣告，見[採用紀錄](progress.md#2026-09-27原回條-reader-與拒絕卡片已採用)。
dfec兩軸清後接回#78獨立的operation decision讀取，取消路徑及測試bytes未變；
固定ada只重跑queued／active回條2 PASS，沒有重算原62／62，新final只需同軸核整合歸屬。

薄client接上既有cancel API，先核原admission與job，再沿原journal寫once intent後只POST一次。
Typed control回條綁job／generation，受理不當停止／保存／收件；原response unknown與跨generation錯配保留，
不因讀到terminal而改判本機回條，也不刪答案／draft／journal或重啟對話。
962的API27 PASS／1 setup ERROR與補runtime路徑後單案1 PASS分開；331修瀏覽器發現的取消文案後
完整UI47 PASS。真瀏覽器只對隔離的synthetic ASGI／fresh journal驗一個queued情境，未啟SDK／模型。
182與#71整合時取消檔案未變，只另驗兩個直接控制；所有較早FAIL與WIP來源保留，未宣稱完整TC-12或live停止。

fac首審Standards0P1/P2、Spec0P1/1P2：active取消受理後若已有終局，註記不可一直說仍待停止。
六個同task終局反例先FAIL、另一task負例PASS；aeb修呈現，原取消回條／UNKNOWN不改。
另於923補GET後的本機登入重核，RED／GREEN只證local-vault合成clock邊界，不稱server401已重現。
固定d5取消API25P／UI54P分批；接回#74的獨立template UI後，固定3da共同bridge11P／UI58P。
兩邊consumer／auth觀察版本與原once/metadata讀取都保留；原部署線持有API/8765，本線沒有覆蓋。
詳見[首審與共同來源](../src/fa-shared-context-poc/web_test_console/README.md#首審修補及模板共同來源)，原裁決與failure不回填，final待原兩軸複核。

### PA-16／TC-16逐回合唯讀觀察

[原回合宣告容量](../src/fa-shared-context-poc/web_test_console/README.md#原回合的宣告模型容量)新增optional共用DTO欄位，
沿`WorkRegistration.profile.instruction.model_capacity`，不查目前模型清單或修改原task/archive。
原缺件1 FAIL後，固定547的19Python／78DOM分批通過；跨模型／清除current facts仍保原snapshot，
missing limits仍unknown，錯model/digest拒絕。舊無snapshot不加None欄位，reader須先於新API回應更新。
此為純讀metadata，不把同樣schema字串當作舊strict client已能接受新欄位；未採用、不影響原model profile啟用。

工作台另補[Context重連競態](../src/fa-shared-context-poc/web_test_console/README.md#2026-09-27重連不復活舊-context-明細local未部署)：
只隔離本機登入世代的Context observation/pending，舊成功／拒絕／錯誤／收尾不得復活已清metadata或解除新read。
原4c HTML的新五案都FAIL，定點修後8P；先固定cc83再跑完整42P，無API/SDK/model/工作台重啟。
新結果只支持thin-client使用路徑，不把完整tokens未知改零、不改後端授權、原答案／草稿／journal或ACK。

[Context明細](../src/fa-shared-context-poc/README.md#每回合-context-的唯讀明細local未部署)
沿原human/current-read、registration及一致快照，核保存的archive材料與原回覆，
區分recorded SDK state／inline zlib／公開JSON／本文／保存zlib。Catalog只稱歷史目錄，
原話／記憶只列真正捕獲的參照，不回私密本文或新作load／permission判定。
Complete request／provider tokens、wall-clock及UI bytes缺件明示null；不掃全authority補估。

先保3個真404 RED、strict-JSON oracle與immutable-write負例fixture FAIL，
再補真正原話reader及缺設定前置檢查；各WIP來源／結果不覆寫。
先固定eb047，再於clean source得Python85PASS（Context50＋原consumer35）及獨立Node36PASS；
不是同一121案，也沒有browser／native SDK／Hosted或自然模型品質證據。
547筆保537，PA16維持partial並更新基準eb047，TC16由unassessed改partial；
完整source-load trace、用量索引、retention／cleanup與平台驗收仍未完成，四C／0情境不動。

9ce初審Std0P1/1P2 BLOCKED、Spec0P1/P2分開記；後者不抵銷前者。
正式writer會讓第三／第四回合沿用原compaction execution，reader卻要求job相同；
兩個真signed保存後GET均409 RED。修後沿註冊previous archive只核一跳metadata／相同execution，
回傳inherited類別與原observation job；改來源、hash、material／identity仍拒絕，不重驗Blob。
main60整合後先固定ce1c，再跑Python92P及獨立Node37P。561保554／published544，
PA16／TC16 partial基準改ce1c，原85／36／P2／RED不回填；SK12仍只採主線b66，未新增native。
新final兩軸仍待複核；revoke兩檔WIP具名保存，沒有混入本Context交付。

其後原兩軸對248各確認0P1/P2、原P2閉合；再各自確認最後951只修一行基準文案，
#61正常exact-head合併b7c05af，parents為2f4／951、tree與受審head相同。
兩份final review已追加本輪566，原ce1c 92／37與Std初審P2都保持；不以merge改算受測來源。

### 技能指引子集與真正停用producer分開

[SK-06既有指引資格](runtime-skills-evaluation.md)保留b7的3、ec8的33與a9的1native各自來源；
首3是33子集。Initial854 Standards0P1/P2、Spec0P1/1P2後，c7同源補成37API及1native，
驗真正已載入本文之後的新版與standing read權撤回。當時兩個skill-onlyrevocation要求沒有producer，
所以BLOCKED/NOT_RUN，不能拿reject新版或全面readrevoke冒充；finalf04兩軸清後也只交subset。
SK-06當時先列partial atc7，原grant正向是existing ContextArtifactAdapter合成組合，
不解除目前WorkStateEffectAdapter的positive producer缺口，也不是自然LLM防注入品質。

後續[真正human停用producer](../src/fa-shared-context-poc/README.md#真人停用單一有效skill版本local未部署)
是獨立實作：f24先commit後156 API/consumer/直接相容控制與1個三turn真SDK案例通過，
兩批共用固定source。6bec只結果文件，原兩Opus5.5完整0P1/P2後正常合併為6fae365。
原14批包含404 RED、client/CLI缺口、fixture錯誤及各green，前12個WIP baselines保持null；
不能用後來程式把當時缺producer的兩個要求改成已驗。
SK-09只更新partial atf24：停用有原human/currentversion/hash/ETag與同row immutable回條，
歷史/approval/pendingupload/獨立operationgrant不抹除，不新加model管理tool或IAM。
空catalog與新批准版本可沿原CID/SID接續，並不宣稱所有midturn／未完成工作／附帶檔或部署恢復都通過。

新的current SK-06已在同一clean`6fae365`重驗：事前scope／actual collect與39API/callback＋2native
的XML都是41個唯一node，全部0F/E/skip。API含10個SDK-I/O callback控制；兩個真停用API
case以exactsource assertions＋per-node PASS佐證，沒有額外逐request JSON便不捏造。
新native proof的4＋6＝10個scriptedproviderrequests、1＋3＝4個實際stop都graceful/force0；
deactivation permissioncallback未另量測，保持null。

Final報告c620只有文件，原兩Opus5.5各0P1/P2，Spec逐條核對後明確建議完整SK-06 C
**只綁實測6fa，不用c620或mergehead**。本組資料據此記accepted／implemented，保原473前綴。
這是closedregistry、serverfixedoperationtarget、strictEmpty／currentauthority的程序邊界；
「已有grant再帶destination參數」額外執行例仍未新增，結構判斷及P3註記明列於run limitation。
不外推自然LLM／M、fullSK09、WorkStateEffectAdapter或deployment。
原b7/ec8/a9/c7/f24及兩個當時BLOCKED要求全部維持各自來源，沒有用新producer回填歷史。

接手工作階段於11:24–11:27在同一clean f24另執行的
[三批結果](runtime-skills-evaluation.md#skill-deactivation-固定來源接續)現在追加於476之後。
101 PASS／2 setup ERROR原樣保留；設定既有runtime路徑後只补跑兩loader 2 PASS，
另有三turn SDK 1 PASS。這些不是原owner的156／1或6fa的39／2，原件hash與時間各自保存。
未發布的450筆接手草稿已先保存，再採用已合併main #50；沒有把其舊partial狀態覆蓋新SK-06接受，
四個full-C基準及12情境均不變。2026-09-26T13:42:47+08:00使用者重新明確授權自主推進、
必要commit／PR、Opus5.5清P1/P2後正常merge與必要雲端操作；共用累計USD150及unknown成本界線不變。

### SK-11可攜模板參照切片

[匯出與目的地檢視](../src/fa-shared-context-poc/README.md#可攜模板參照匯出與匯入檢視local未部署)
沿原指定human／standing read權及M3批准catalog，只提供versioned prompt、settings與skill hash參照。
新GET不帶私史、本文、credentials或既有grant；POST inspect不寫設定或啟用，參照吻合仍無授權。
Legacy role無版本明示拒絕，停用、換版、名稱未配置、撤權與有界input都有分別結果。
這是第一個產品producer與consumer接點，**不是完整模板安裝／目的地採用**，SK-11只列partial。

先提交c106，再以相同410個Python來源跑100 PASS／1 tokenizer setup ERROR及補環境後原production控制1 PASS；
補跑不是template SDK案例，早期404與89/4／29／36 WIP批次均另記，前四批baseline保持null。
485筆保留old479/spec/bindings與四個current接受，未改SDK、固定prompt、部署或舊測試判準。
6d14的Opus5.5兩軸均0P1/P2。另依非阻擋提醒修精確negative fixture，
fixed4f72的4案全過，原c106結果保留但揭露當時兩個POST body缺欄位，不能獨自證query／duplicate guard。
初審兩筆與修後一批追加至488；產品未變、SK11仍partial。
原兩軸對final cd7d502各確認0P1/P2，#52正常合併為af2dac5；兩份final review另記為490，
受測product c106／input cases4f72不改成merge來源，沒有新增執行或完整接受。
SK12隔離probe不混成本線template證據；原runtime線的未交付快照與接續界線如下。

### SK-12固定快照接回與單一personal source正反例

[task-local probe報告](sk12-task-local-isolation-probe.md)保留原334a的一個native FAIL：
public discovery找到task-owned canary，不等於本文已載入；首個HTTP bridge原因仍UNKNOWN，
FA create／cold resume未執行。來源c1e已補回應／重試辨識、路徑與safe-event guards，
但原六批零runtime結果不能取代native正反例。

本接續將c1e的兩個測試檔與直接報告原樣接到c5c687a基準，不修改來源工作樹。
2026-09-26T16:22的獨立14案全過，未啟動SDK／模型；受測樹含未提交檔案，baseline保持null。
新run只列`evaluation_tooling`／`coverage=none`，保留491筆前綴及所有target／scenario狀態，
SK-12仍partial、四個C基準不變。當時未沿用舊commit／PR／雲端批准，也未干預原工作階段。

使用者16:29重新明確要求完整實作及必要交付後，本線在自己的task roots做新有界診斷。
6cb先保留1個runtime缺件setup ERROR，再用相同source實際捕獲非串流request被fixture拒絕的FAIL。
9案RED為1P/8F，response helper修後21P，兩批WIP baseline均null。
Fixed e012改回標準JSON後已到native skill，但正文未載入，1 SKIP/proof BLOCKED原樣保留；
不是看到exit0就記PASS。

固定79b只再對齊public client base與session config根，實際22P＝21個contract＋1個三階段native案。
同一canonical personal canary先由public discovery／native skill載入，再被FA create及cold resume排除；
合法M3 reader仍讀目前版本，原SID/user/history/archive/reply/cost保持。三runtime全部正常stop、
force0，6個scripted callback；tools/system的catalog掃描flag為false，不代表其他role都無目錄資訊，
也不外推隱式選用或FA強行呼叫未列出native tool的拒絕分支。
SK-12只更新partial baseline至79b；其他ambient根、installed plugins、平台與Hosted仍未驗，
四個C基準與12情境不變。原334a FAIL及第一次HTTP原因UNKNOWN不因這批而回填。

9398的Opus5.5初審兩軸均0P1/P2。依P3補清負例／catalog文案、固定官方permalink，
並新增真正的SDK版本核對及witness `skill.invoked` gate。
WIP錯版反例1FAIL、修後22PASS保留null baseline；固定492同scope23PASS為22contract＋1native，
3runtime/6callback均實際stop、force0，SDK client SHA已入execution。新批只把SK12 partial基準推到492，
不是新的完整接受；未列出native tool的強行呼叫及其他ambient/plugin反例仍未驗，最終head須另經兩軸複核。

交付前main已合併#56為fa494fe，本線以published504為帳冊前綴接回自己的12筆；兩線共同491筆，
本線舊sequence491–502改為504–515，原503冊／main504冊及逐run對照另存session原件。
SP20、TC08與其他主線狀態全部保留，只帶入本線SK12的partial基準／blockers，不把23P當新主線重跑結果。
原線#55另發布failure-priority P2修補，本線於f74真外層終局重現1P/3F後接回，修後26P，
兩批WIP baseline=null；另無opt-in的1SKIP保native NOT_RUN。提示不得掩蓋實際例外／安全違規，
completed flag也不能讓之後失敗變PASS，原錯誤proof不回寫。
固定7a93已含fa494fe，在相同有界scope完成27P＝26contract＋1個三階段native，
3runtime/6callback全stop、force0，proof SUBSET_PASS且無blocker/hint/violation。
新四批追加至520；SK12仍partial並指實測7a93，四個C／SP20／12情境保持，最後仍交原兩軸核新head。

39ae final兩軸Opus5.5均0P1/P2後，#57正常exact-head合併395adc3，實際tree／parents與已審snapshot一致。
新切片25480f1保原control，另在create/resume強行要求未列出native `skill`；
不是只看目錄缺席，而是各取得精確call ID hash配對的typed success=false/error-present回條，
無`skill.invoked`／canary本文，隨後合法M3 reader仍可用，反例檔的bytes也未消失。
Observer僅被動容許該精確惰性attempt，並未替SDK先abort；其他非預期effect仍fail。
WIP31P與固定33P（31contract＋兩native）分開；六runtime全stop、6＋8callback，
git status/test hashes在execution中實際保存。SK12仍partial at25480f1，其他plugin／根／平台未驗；
四個C及12情境不動，初審／合併與新兩批追加至525，不回填原520。

#58又合併反向approval unknown修補為f6ce532；本線保存main530原前綴後接回五筆成535，
只移未發布sequence，不改原525的payload或規格綁定，SP20 current d091及其他main狀態保留。
25480f1的33P仍屬原source；共同來源的同scope驗證另記，不能用main新增內容倒改過去結果。
固定8c33e15只重跑兩個native，2P／30.490秒，前後git status與test hashes另存；
六runtime全stop、6＋8callback、兩個typed失敗回條及原保存／reader條件維持。
新run追加至536，SK12 partial基準指8c33e15，原31個contract不計入這次執行；
SP20 d091與四個C／12情境不變，仍需對最新final head做兩軸審查。

89f96f5兩軸均0P1/P2後，正常merge被GitHub以conflict拒絕；target branch已前進到#59 efb7445，
PR的base欄位當時仍顯示f6ce532，不能把該欄位當live target ref。沒有bypass或宣稱已合併。
本線以published537為前綴接回六筆成543，原536的payload／綁定保留，SP20取main335，
SK12保自身8c資格；新的共同來源驗證與final-head複核另行記錄，舊89f verdict不套新head。
固定b66c745只重跑原control及forced兩native，2P/30.457秒，六runtime全stop、6＋8callback；
回條含空的前後status、test hashes及兩份成功proof，原31contract未重跑。新增run至544，
SK12仍partial/current b66，SP20 main335及四個C／12情境不變，最終head另交同兩軸複核。

### TC-08壓縮摘要與當前人工批准分開

[限定native資格](ctx03-local-acceptance.md#16-tc-08壓縮後摘要不是新的人工批准限定資格)
在clean bd4先固定再執行：1 PASS、2 owned runtime／6 scripted requests，
正式manual compaction後的新summary帶假human批准，實際送入後續model payload；
原operation在無grant下仍回409，合法read與原user事件／reply／archive保持；
本案未另捕獲server拒絕碼，不把這個狀態碼當成已證明唯一拒絕原因。
SDK1.0.13/runtime1.0.83沿原OpenAI/completions/http路徑，產品與SDK無diff。

這只把TC-08由unassessed改partial；原490與四個current C／12情境不動。
不是其他模型／provider／平台、自然摘要品質、automatic compaction或完整TC-08接受，
也沒有在壓縮後補真正operation grant。Final09b原兩軸0P1/P2後，#54正常合併為c5c687a；
兩份final review現在另入冊，原bd4實測source與結果保持。

### SP-20人工拒絕的真正producer

[操作拒絕切片](../src/fa-shared-context-poc/README.md#真人拒絕尚未批准的操作local未部署)
補原本缺少的human rejection，而不以沒有grant、拒絕agent自授或grant revoke冒充。
原prepared／無grant／無attempt操作可由指定human exact-digest拒絕，原guard/CAS一起保存
decision及operation marker；原material／effect/business／budget與unknown不改。
同原operation不能新approve或換attempt ID繞過；已有grant／effect不由reject改寫。

先前signed404、WIP26與clean2c71020的148 PASS分批記錄，前兩baseline=null；
真同adapter正向、approve/reject CAS、consume／decision ACKloss與API-only import分開核。
SP-20只列partial，不含UI卡片、重開原操作或native／完整能力接受，fixed final兩軸仍待完成。
496筆保old491與四個C／12情境，不把新增producer回填SK06過去缺human-reject的結果。
7c21d73初審Standards P1指出新no-kind決定未接history分類；Spec P2指出原unknown可被
新request／同decision的結果變成conflict。先保原148，再以真producer測出兩個unclassified
及nonce反例；兩個inventory fixture setup錯誤與pending種類expectation FAIL另記，不當產品RED。
修後保原決定shape、註冊兩reader，並將decision與原request/body一同固定於plan EGT；
不同新human決定仍可在pending下作出，但原unknown不回填。先固定ca8b324再跑188全過，
504筆保old496，SP20 current partial改指ca8；原blocked reviews及兩軸修後複核責任仍保留。
其後final4724由原Opus5.5兩軸各0P1/P2後正常合併#56為fa494，初審FAIL不回填。
原非阻擋跨action提醒另做[5ccc窄補](../src/fa-shared-context-poc/README.md#rejection-intent-與-approve-的跨-action-邊界)：
新的approve不能借既有rejection intent的DID，absence predicate入EGT；已有approval plan
若缺新predicate或後來遇到同DID的reject intent，只核原receipt，不改read-set或續發grant。
Parent2RED/2PASS、WIP32、clean5ccc192
及session-only舊method/currentconsumer1PASS分開，後者不是完整舊runtime、baseline=null。
510筆保old504及四C／12情境，SP20 current partial改5ccc；新的final兩軸仍待完成。

其後原兩軸對3c24各0P1/P2；Spec非阻擋提醒另以真signed API補反向
approval-unknown→human-reject→original-readback，same／different DID兩案皆重現409而非503。
修receipt讀取順序後WIP32PASS；整合#57後先固定d091，再跑194PASS與獨立legacy hybrid1PASS。
這四批另記、前兩baseline=null；530筆保原526／published520，SP20 partial基準推至d091，
不把舊192或新SK12 PASS轉算成本輪證據。新final仍待原兩軸複核，完整SP20／UI／Hosted未接受。

其後final788由原Opus5.5兩軸各0P1/P2後，#58正常合併為f6ce532。
Spec新記同action既有缺口另切片：新的approval request不能沿用已有plan的decision，
以既有binding plan-index查原control request並在reserve EGT核index未變，不新增intent型別。
真RED1P/2F、漏轉交覆寫方法23P/12F、直接診斷1F、補接35P各自保留null baseline；
先固定335140f再跑197P。537筆保530，SP20 partial指335；新final仍待原兩軸，不借#58清除。

其後finale356已由原Opus5.5兩軸0P1/P2清除並正常合併#59為efb7445；
兩份final回條另入本輪547帳冊，197P不改source。Spec留下revoke相同模式的獨立待辦，
不把本切片擴寫為全域decision registry或完整SP20接受。

Revoke缺口已另[接回同action保護](../src/fa-shared-context-poc/README.md#同-action-撤銷決定沿用原-command-plan)：
真signed API同DID新request／plan-reserve交錯先1P2F，原不同新decision正向保持。
共用plan-index／EGT接線後WIP38P；這兩檔未混入Context審查／合併。
先固定942272b再跑200P，566保published561；SP20 partial基準更新942，仍待本切片final兩軸，
沒有新schema／nonce權威／native／Hosted或完整SP20接受。

其後final551由原Opus5.5兩軸各0P1/P2確認，#62正常合併61180d7；
兩份final回條追加本輪570，原942的200案不改。另以[固定SDK見證](../src/fa-shared-context-poc/README.md#真-sdk-的人類拒絕接點見證本機-bounded-scenario)
讓真正human拒絕後，loaded skill仍宣稱獲准，但原Core回`OPERATION_HUMAN_REJECTED`且effect0。
首826因fixture extended-path在session.create失敗（provider0），正常stop、原FAIL保留；
只改普通owned root後先固定4933，再1PASS／8.697s，1runtime／4requests／3permission callbacks、
正常stop／force0，合法read／原保存保持。兩輪owned roots已清，沒有借SK12 runtime allowance。
SP20仍partial at4933，未把這個scoped witness當自然模型品質、任意adapter或full／Hosted接受。

其後final49c由原Opus5.5兩軸各0P1/P2清除，#63正常合併598164e；
兩份final回條在本輪573保留，原826 FAIL／4933 PASS不改。
另[真SDK撤銷／到期](../src/fa-shared-context-poc/README.md#真-sdk-的已撤銷已到期-grant-見證本機-bounded-scenarios)
在clean固定2f425只跑兩個新node，2PASS／14.177s、2owned runtime／8scripted requests／6permission callbacks，
精確grant拒絕、effect0、正常stop／force0；兩案root/parent已清。Expired只推進可信測試clock，
grant bytes及uses/units不變；revoked是真signed API。原default409兩個node只核collect，未重跑或回填。
SP20維持partial、基準2f425，unknown SDK／不同target binding／一般adapter與完整平台仍未接受。

### 已確認input-limit的限定觀察

[TC-05兩vector探針](input-limit-evaluation.md)在fixed50cf、SDK1.0.13/runtime1.0.83
經真signedAPI/controller/SDK callback執行，只有外部provider合成替代。每案只有1次provider
POST，6個model callbacks中的後5次被既有fence擋住；A精確code與B只有訊息字串可區分。
真owned stop後failed結果forwarded、synthetic pending用量仍保留；沒有觀察到compaction或恢復。
TC-05僅由unassessed改partial，不接受四trigger、native業務模式、coldrestore、外部exactly-once
或零費用。Fixture是先凍結的session檔，不在Git，native run baseline保null並用product／fixture
雙來源hash標示，不能把文件commit當受測程式。

### 不可變回條的限定盤點

TC-02在fixed`fe482b1`只讀Git與既有safe proof，沒有新test/SDK/model執行。
原message/command、create-only answer/turn、ControlStore receipt、copy stage/outcome及ACK
都有接點，但8KiB工具事件裁剪後再遇archive不可用的完整鏈、所有cross-surface與任意外部
exactly-once仍未知。Pure GET、原receipt對帳、copy尚未dispatch的stage接續不是同一種操作。
其後[final-save組合資格](threaded-reply-evaluation.md)已完成：受測source/tests先提交f337，
5個real API/store/controller＋SDK-I/O cases PASS，另唯一fixedSDK/offline A PASS。
正常保存回應遺失只查原SID/status、不再save；提交前失聯無save API dispatch，pending cost／
consumed intent保留；同ID異文／turn不覆原件。取消後讀回／明示ACK仍保cancelled，
GET不變成client_received，client_received不當human-read。
Original-save與C案例的兩個explicit replays分開計數；一個合法checkpoint Blob put先於save，
之後不再增加，不能寫成整回合零儲存寫入。個別403/409是原件／source觀察，測試assert拒絕集合；
safe JSON的cloud/paid0屬scenario宣告，不是測量counter。原非阻擋notes保留。
Final02ae經原兩Opus5.5 source及pure-integration各0P1/P2，正常合併為41deea0；
source/tests/controller整tree等actualtestedf337，沒有在merge重跑5＋1。TC-02只改partial，
完整工具鏈裁剪／archive不可用及cross-surface缺口保留，未以TC-01 overlap替代TC-02全接受。
盤點JSON SHA`807efc4b7ed56ec8a01033ccdcbb435e8705419d68a01a731cfc4c224d5e875d`，
MD SHA`2739df50ba28a212eb430c40d094fce4e6ef282f39430ac664e9b1cf4763036a`，兩份原件均保持只讀階段。
Final composition handoff SHA`e98a7f59a4648c26d81c6e58d4bcb6849dcdc700b8ba0162b46d2409ba844c69`，
summary SHA`dd4663a42a21dc6f7cfd50ce58835d0bc2a188848557adee3b0bb351ca79e2b5`另屬執行／review階段。

### 先前批次歸屬

兩線共同前285筆完全相同；當時尚未合併的本線14筆payload／hash／spec及entry綁定不變，只把sequence移至main318之後。原分支299與主線318前件、14筆新舊sequence對照各自保存；不宣稱299本身仍是新332的前綴，也不重排已合併main歷史。

新七筆分別保存初版RED、局部GREEN、completion shape RED、前一次40-case、最後40-case、33-case minimal-DOM及真browser renderer觀察；重疊測項不相加成獨立品質樣本。這些沒有新模型／Hosted執行、human gold或新的full-target接受。原owner的記憶reader仍按下方來源接續，本線沒有接管其未提交WIP。

另追加四筆固定runtime前提與本機原生結果：缺件setup error保留、核准npm feed補齊20日齡的
1.0.83平台包、原控制1 PASS、新配對1 PASS及合批4 PASS分開。新增的是**真SDK＋離線provider**
證據，不是真模型或Hosted；四案仍不足以重新接受整個TC-01。

Main30已合併後再追加68 Python／5 native／33 DOM三筆整合結果；新memory工具只公開名稱／狀態／
安全配對，正文與raw IDs不出public progress。原14批歷史均保留，不加總為品質樣本。

Main32整合後的`60cd87e`乾淨commit另跑memory普通／checkpoint兩案，2 PASS；原七profile矩陣
不重跑或回填。這一筆只核新test路徑與公開DTO相容，完整TC-01仍partial。

其後#31已在fixed`e029b98`兩Opus5.5完整review各0P1/P2後正常合併為`0dac615`，
merge tree與已審head相同。這次在乾淨`0dac615`完整重驗TC-01原43 Python範圍，加新31＋2與
33 DOM，76＋33全過；16 native投影、四process compaction／13次原reply讀回、preview、guard、
所有typed consumer及工具配對均重新核過。候選經兩軸確認後才恢復C接受，不把fake provider當模型品質。

CTX-03 #8 的[`c51e730` signed-token修正](ctx03-local-acceptance.md#13-合法-signed-token-差值原生失敗與最小修正)已經兩軸review並合併，沒有部署。e80的原clear、後來新增P2、退draft和修後結果各自保留；Q14真Hosted／Blob驗收不在本次TC-11本機C範圍，仍為NOT_RUN，不由本機保存／恢復結果代替。

版本化[Prompt基礎](prompt-assembly-evaluation.md) #11 `a9a3ce1`、帳務read projection #10 `e78c270`也已審核並合併。新能力仍預設OFF／未部署；合併不使[預算headroom](fa-cloud-budget-status.md)變成已知，也不證明所有prompt／模型行為已驗。

Approved skill source #12 `d05851e` 已修Hosted匯入P1、完成兩軸review並合併到 `37b09dd`。它使用原批准／scope／immutable version及SDK custom tools，不以已知fail-open的native hook作唯一權威。[產品驗證](runtime-skills-evaluation.md#受控技能來源產品切片)與前置SDK probes分開，不把Team來源接點當完整私有ACL／SK-09／skillify。

**Skill catalog continuation** 的#14 `e17a20e`已兩軸清除P1/P2並合併為`578d880`：合法terminal且已forwarded／graceful stop／archive可恢復的success、failed或cancelled回合後，新真人回合可採current catalog；原within-turn與unfinished-job拒絕仍在。`bbc3be8`的success-only P2及先前結果保留於[初審](runtime-skills-evaluation.md#目錄接續切片初審)，[修後結果](runtime-skills-evaluation.md#目錄接續修後與合併)另記，不宣稱完整SK-09。

協調線的[SP-06文字對話／收件路由](threaded-reply-evaluation.md)已隨#15合併為`8368fbc`：固定commit上的三個公開API案例及一個native案例涵蓋交錯對話、原SID恢復、request重送與late／duplicate receipt。這是既有行為的新增證據，不是重造路由或虛構產品RED；完整通道／executor callback矩陣未驗，SP-06仍partial。

原owner的 **SDK啟動前拒絕診斷**在其固定base上仍是BLOCKED：四種來源／ACL更動都在SDK entry前被拒，但當時已claim回條沒有合法無SDK終局。五批RED／support PASS原樣保留；後續新契約的14個批次另行追加，沒有把原診斷改成已修。

[No-SDK disposition](sdk-prestart-evaluation.md)已在#18固定`be002df`兩軸各0P1/P2後合併為`d03aba2`。真SDK entry效果點的不可逆gate、owned joined observer及原claim/probe/CAS綁定只允許可證的無SDK失敗；403、phase、沒有PID或回條不足以證明停止。沒有新archive時只提供failure查收與明確reset，不造SdkStop／TurnArchive、不清UNKNOWN，default-OFF且未部署。

後續[explicit v2](sdk-prestart-evaluation.md#v2記憶拒絕的限定收尾)只配work-memory profile，
經原強entry／observer／current-report/CAS證明收有限memory拒絕；v1／None不默升。
Generic `ASSISTANT_SOURCE_CHANGED`已從v2終局資格排除：真合法WorkerCore.reconcile只改job/query、
knowledge不變的三RED，修後維持unknown/open gate，不為避免reset而鬆readsets。
原13批2564的89API/native1、後5批10aa的33API/native2/DOM1分開，不把9c merge當測試執行。
最終9c同兩軸0P1/P2後合併為ddbcd64，latest metadata-only main3f的337歷史與C資格原樣保留。
這只增加TC-12 partial接點，不是全部失敗／重試／取消、Memory更新transition或Azurestop接受。

協調線保留`4c1f2cd`的37 PASS／23次offline並撤回完整宣告。[修後42-case](prompt-assembly-evaluation.md#修後42-case同baseline矩陣)在`22e62ac`同一乾淨baseline跑通，51次offline分47 normal／4 SDK summary；各normal的完整system、catalog版本／archive與cold restore均實測，沒有用core hash推測未走路徑。

### 公開逐字稿的局部呈現證據

最初`234dde4`只新增workbench regression與直接README，固定commit後32個minimal-DOM checks通過，不冒稱browser／backend。後續[TC-01 C候選](transcript-projection-evaluation.md)補真native compaction期間與兩個cold resumes的原reply bytes／hash／no-store，以及每個舊turn的reply_available／no-stream旗標和未ACK狀態。

Current `12f624f1`在新容量程式已整合後，同baseline跑4個Python checks＋32個minimal-DOM checks；只有一個Python case啟真SDK，其餘HTTP／bridge／SDK I/O替身明確分層。Preview在正式reply GET仍404、private reasoning不進公開投影，source另核preview不供SDK history。這份組合已通過#23的TC-01 C審查，不是實際browser端到端或human-read。

後續review更正source推論：未設定checkpoint profile時，原observer忽略compaction訊號。
Parent最小gateway反例證明可到mock reserve／sender，未證native背景壓力或真preview曝光。
Observer修補只在已收到未核事件時標原source error，不新增compactor／profile／NoSDK終局。
Repo四個RED、WIP十個GREEN與`0680d59`的101-check都保留。Nonlegacy `prompt_assembly`
的strict bridge缺口又經真API200／bridge502重現後修補；因此新的完整重驗才把所有instruction
native projection送進該DTO，並含真bridge各schema反例，不再拿legacy-only回歸外推全部profiles。

### 完整checkpoint的C資格核對

`ed3f37c`同一固定source的61個現有checks，涵蓋普通filtered大狀態及manual-no-custom產品保存／恢復，分別實測raw162834／143417B與zlib43627／32249B，均越過舊131072／16000界線。另核完整files、8MiB／16parts、wrong identity／codec／missing material、unknown upload與空history拒絕。

首批60 PASS／1 child-import FAIL保留；只補runner PYTHONPATH，次批61 PASS。後續event fence屬另一個真產品修補，`0680d59`新合批中66個Python checks對應TC-11，含正常／manual／無profile三路；普通native3檔與合成extra-file驗證分開。只支持固定Windows/SDK/離線model/local store的程式範圍，不接受Q14／跨OS／語意品質。

[受控原話reader](user-turn-reader-evaluation.md)已在#20 `57ab942`兩軸各0P1/P2後合併為`a445310`。實際tested source是先提交的`f52654c`，最後123/API及1/native在其後執行，final只README；15批與三次eviction假設FAIL保持。Summary省略不等於payload已忘記，full eviction NOT_PROVEN、完整TC／M品質未驗，不改read_context語意或自動寫Memory。

新第5個prefix整合後，coordinator先撤回current PA-02為partial，保留22e歷史接受；[新五prefix合批](prompt-assembly-evaluation.md#第五個prefix的重新驗收)用`2b942a1`同baseline62 PASS／64offline requests重新提出當前C接受。只有exact user-turn讀取、來源繼承與prefix不變式被證明，沒有把新profile借進舊42-case。

[TC-04 declared facts接線](model-capacity-evaluation.md)已在#22修後`b56ecb9`兩軸0P1/P2後合併為`1b7f4d9`。原structured接單後拒絕P2已修；known／unknown snapshot到SDK的配置傳達成立，但runtime attribution128000與宣告60000/14000不同、wire不帶output cap，所以effective／vendorhardlimit／safe preflight仍NOT_PROVEN，TC-04只partial。

[獨立SDK介面診斷](model-capacity-evaluation.md#固定sdk的公開介面診斷與採用阻塞)已凍結：官方高階camelCase／generated snake_case轉換有可重現numeric丟失，但public typed switch的反證仍是post-turn128000，無已證明安全改善的產品替代。七批原失敗／DTO controls保留，產品採用BLOCKED，不patch SDK、不把`applied`當capacity生效，也不自動啟用preflight compaction。

[SK-01用途描述](runtime-skills-evaluation.md#用途metadata隨批准版本)已在#26固定修後e96與純整合30a各經雙軸0P1/P2，合併為`e2460ea`。Metadata綁immutable version／human approval，CLI確認前顯示字面Unicode，typed bridge保留strict驗證與舊bytes。原14批、修6批及獨立34-case union不互相回填；SK-01 private ACL／SK-02 M／token budget仍不接受。

第6prefix及current TC-01的[新資格](prompt-assembly-evaluation.md#第六個prefix與公開投影重驗)在source `ed6f64e`先提交後，clean執行105 Python＋32 DOM全通過。PA-02子集80、TC-01子集71共用該137次checks，不能加總成獨立樣本。16個prefix native有83 normal＋7 summary requests，另retention native6次；actual projections／DTO、bridge替身與minimal DOM分層，沒有真browser／雲端／human-read宣告。

### 已審工作記憶唯讀接線

PA-13／PA-16的[局部接點](reviewed-work-memory-evaluation.md)已在#30整合final `efd2a348`
同兩軸0P1/P2後合併為`cfd9b7d`。Server-owned/default-OFF工具只讀原Team/binding允許的一筆
已持久人審note及必要原件；沿Runtime.context/current_claim、MemoryGov及原producer，不另建store或loop。

實際scope由verified principal、binding tenant/team/subject/purpose決定，模型不能自選其他actor、
scope或提供review回條。`derive_work`目前只保留profile.claim_id，必須遵守這個producer／allowlist界線；
若private／跨conversation用途尚無合法接點就保留差額，不cast身份、加grant或假造private正例。
本slice不做自動提取／發布、付費Native Memory search、任意記憶用途擴張或虛構token容量。

Current refs保守固定，來源更正／撤回不默換新版restore舊body；同subject另一claim的commit也會改manifest。
Catalog先組完整24576B response含task source，太大時可能preSDK413；只有明確opt-in v2且足證未嘗試SDK才可限定failed/reset，其他仍UNKNOWN。Private偏好／Agent經驗、
一般memory更新transition、M品質及fullPA-13／PA-16仍未完成。原受控operation正向仍待真
configured effect，不會為測試放寬已enqueued工作的不可變fence。

Original13＋capture2＋diagnostics2＋new-main integration3共20批分開保存：
`358f8fc`的88/native1、`6bc1941`的test-only2、`b55b128`的8、`d5abb68`的34/native1各自先commit後執行，
後來的README／merge heads不冒充tested source。新main#29保留generic schema feedback，
read_skill/read_work_memory動態authority schema沿同遮罩，6個integration RED與修後結果不回填。

第7prefix在`cb4b180`先標partial再重驗161 Python＋32 DOM＝193；PA-02子集109、TC-01子集75
共享16個Python cases，另25個TOOL-06 callback是同次額外回歸，不新接受另一套runtime目標。
原六prefix接受只是歷史；七profile資格已在#32固定`1c3db91`兩軸0P1/P2後合併。
公開配對是之後的新consumer差異，TC-01資格另列，不把該193批當作新UI metadata已驗。

### 2026-09-25 重啟接續

昨夜依VM關機要求於00:40收尾，所有本次worker待命，原件及83-run前件保存。使用者明確表示已重啟、可以繼續後，本機讀回 boot `00:50:12+08:00`、核main `37b09dd`，在乾淨的協調worktree fast-forward到已合併來源，未重寫任何分支歷史。

本次把已保存的 #12 結果追加19筆：開發歷史索引、13份各自JUnit、四份分軸review與一個精確整合tree cold-entry結果。XML日期用原timestamp換算UTC+08；9/24晚間測試不改成9/25匯入日期，午夜後review／整合觀察另記。沒有重跑舊SDK／model或刪除原失敗。

本日仍依相同目標前進，下一個台北時間 **2026-09-26 00:40** 開始收尾、00:50前待命；這是VM作業安排，不是產品全局deadline或完成宣告。

後續資料PR #13已於01:39合併為 `ee80e2e01d630d65eae93fc37370a43398d31490`。Standards曾列兩個pre-commit測試基準誤標的P2，兩筆改為null並保留原件後，固定`ccc662d`兩軸各0 P1／0 P2；原102筆由此成為本次新增6筆routing結果的可信前件。

#15於02:43合併、#14於02:45合併，皆ordinary merge／exact-head檢查且無admin bypass；最後tree `4df6ca05cb3f1348d6b1d3a916fef65f9a1d930f`與合併前13-case整合snapshot一致。現在再追加23筆：15份原／修後JUnit、六份分軸review、development索引及獨立整合snapshot。原15批皆WIP／base加測試overlay，baseline維持null；原失敗、初審P2及較早native執行不回填成最終版本的PASS。

資料PR16於03:01合併為`d28605bd3605f8654ba59231a9a5422dc7426c90`，固定`dce10d3`兩軸各0 P1／0 P2，131筆成為本次prefix新增4筆的可信前件。首次legacy probe是獨立WIP，正式32-case矩陣則在`1ac5348`建立後、乾淨工作樹上執行。

#17於03:53合併為`ad3ff0b`，135筆成為本次前件。#18於05:03合併為`d03aba2`，tree `a313832b`與獨立13-case snapshot一致。Coordinator保留本地structured測試提交，以merge `6442c84`接入已審產品，再固定`4c1f2cd`跑完整C批次；沒有丟掉本地提交或把後來的merge當owner測試基準。

#19在補完整checkpoint／catalog／inherited-restore範圍後於07:02合併為`cb7a2d8b6868e06d9a941a6bdeb77ba399252159`。原37-case、首審Spec P2、撤回分類與修後42-case保留；現在只追加一份UI局部觀察與兩份修後review，成為175筆，PA-02接受內容／baseline不變。

#20於09:43合併為`a4453105c699e4c9a222508716b356b69399f791`；parent保留UI test／資料提交，另以`b4101a02`整合已審reader，再固定`2b942a1`新prefix tests。現在175筆草稿上追加reader來源索引、15批、兩份review及兩次prefix觀察共20筆，成為195；原172不改，新reader資格不回填舊接受。

#21於10:16合併為`7076ce5`，195筆成為本次前件。#22於11:28合併為`1b7f4d9`，actual tree與14-case capacity/main snapshot一致。Coordinator保留本地TC-01測試／資料，merge `12f624f1`整合已審容量程式，再同baseline重跑4＋32分層checks，不把較早d9結果改標新commit。

#23於12:36合併為`2f33baa8ce5df29d2a862d2d73286188991700b8`，固定`609bd57`兩軸各0P1/P2，226筆成為本次前件。只追加七個SDK診斷批次、一個blocked採用來源索引及兩軸最終review，成為236筆。V2只更正派生index的DTO runtime／model counts，原V1與raw結果不改；所有未保存counts仍null。

#24在固定`734f3df`兩軸各0P1/P2後合併為`b04be4f22eb9e838889e97dd6f75ebd666251ab8`。本組另加TC-11兩批與PR24兩份review成為240筆，old236不動；native61-case的基準仍是實際跑過的`ed3f37c`，後續一行文件澄清和merge沒有改source/tests，不假稱換baseline重跑。

#25擴充為observer修補後，固定`5071556`重新雙軸0P1/P2才合併為`0a22301`，249筆保留current TC-01 partial。#26的GitHub preview一度落後main，owner在自己branch普通合併兩個已審head，產生30a／bd7；同tree先前34-case通過，兩軸再核純整合後普通merge為`e2460ea`，不借stale CLEAN。第6prefix採入後，current PA-02在`ed6f64e`先partial再資格核對，沒有偷用舊五prefix接受。

| 維度 | 意義 |
|---|---|
| `implementation` | 未盤點、已規劃、進行中、部分實作、已實作、阻塞；與測試結果分開 |
| `acceptance` | 未評估、部分證據、已接受、阻塞；沒有「關閉能力即 N/A」狀態 |
| Run `execution` | 評測／讀取過程完成、阻塞、失敗、未跑或未知 |
| Run `outcome` | 該次觀察的 pass、fail、mixed、blocked、not_run、not_graded 或 unknown |
| `coverage` | 沒有產品覆蓋、限定子集、明確的完整條目驗收 |
| `human_review` | 無人審、批次接受、單一條目／情境接受；`scope=target` 的 run 只能包含一個 ID，不將一份決定套給多個條目 |
| `baseline` | `git:`、`snapshot:`、`core:` 的精確識別；歷史不明時留 null，不捏造 commit |
| `specifications`／`run_spec_bindings` | 保存不可變規格快照；每個新 run 明確綁定執行時的規格及涵蓋條目／情境 hashes，而非永遠套最新規格 |

**完整條目驗收計數不是一般模型品質分數。** 數字為零不代表產品沒有能力，只表示帳冊尚未收錄符合整個條目、當前版本與相應程式／人審要求的完整接受紀錄。

### 2026-09-26 明確恢復

使用者於01:00:31+08:00明確`resume`；本機核到00:50:27重新開機，
local／main仍為`03e3d4520adc21a7b78e94239102929b31935a14`。
夜間363-run、三個accepted baseline及原337歷史不變；唯一local WIP原為日終CURRENT摘要，
已保留其patch與舊checkpoint，再改為本次接續狀態，不把前一晚pause當新的停工指示。

原記憶owner已完成[正常observer讀取切片](reviewed-work-memory-evaluation.md#正常工作觀察與知識身分分開)，
#39 final69f在兩個Standards P2澄清後，兩軸各0P1/P2並合併d21f7e2。
產品只把job整列從knowledge identity比較排除，current `_gate`與完整command/CAS仍核job。
真正撤權、更正、owner/cancel/期限／effect仍保護，subjectsequence、source版本、No-SDK eligibility不變；
native業務模式既有receipt-first有限重試僅source判讀，競爭測項未跑，不借真SDK離線控制外推。

協調線另外直接處理兩處已知Unicode測試解析差額：公開bridge回放替身與checkpoint privacy測試的
驗證端，分別以現有公開路徑重現再重用`jsonl_lines`。這是測試可信度修補，不是產品或M品質接受；
不回填上一晚結果。[分批結果](threaded-reply-evaluation.md)保留兩個Unicode RED、固定兩檔29P/1F，
以及不同privacy stub在未修改base重現後的最小修正與固定22P；不是把原FAIL回填全綠。
下一次收尾為**2026-09-27 00:40+08:00**開始、00:50前待命。

PA-07另線唯讀盤點確認原文與線性routing已有局部證據，但使用者device/location沒有可信producer或欄位；
缺值目前是absence，不是可證模型會口頭說unknown。G1/G2產品語義與用途未定，不由IP、User-Agent、
SDK working_directory或時間猜值；不蒐集M365／Azure身份資料。後續非權威metadata的test-only邊界已由
#40兩軸清後合併de8d8fa，7個case仍歸實測6436959；未計入本384-run，沒有新增PA-07接受，
也未改其unassessed狀態。SK-08可行性盤點只證底層M3可建未生效草稿，
不是盤點本身授予Runtime authoring權。其後SK-10前置已由#42修後兩軸清與consumer7檢查後合併：
server-bound、明確default-OFF地複製已批准且原本Team可讀的程序原件，
模型不能提交任意私有本文或自行選scope。這不是現場權限啟用、SOP生效或完整Skillify／M品質接受。
該唯讀盤點固定於d21f7e2，原件`skillify-feasibility\report.md` SHA256
`3341a2d5243b0fa389dbd6606566bd5c6da2dd0c43e0156af43e1603fa999e86`；
metadata SHA256 `2678a7d25212b51eb2f05746fdff118869f22fa54ba62f03b8caddf17487a33a`。
既有[M3成果／批准閉環](M3-ARTIFACTS-POC.md)僅是歷史入口，不當本輪新測試、現場啟用或寫入授權。

## 實作順序與分工

先完成已確認契約的 CTX-03 產品接線，再依實際依賴推進其他能力。每批完成後追加真實 evaluation 結果，更新受影響的目標，然後繼續下一批；不重寫舊失敗或要求人為每一小步重新授權。

| 工作組 | 主要範圍 | 第一個可交付結果 |
|---|---|---|
| 長對話保存／恢復 | TC-01–TC-12、TC-16，相關 privacy／completion | 版本化 codec、可信來源、真 API／store／restore 本機閉環；新 writer 預設關閉 |
| 系統提示／每輪組裝 | SP-00–SP-06、SP-31、PA-01–PA-16 | 版本／作用域明確的組裝，穩定內容與當輪更新分開，必要資料不靜默截掉 |
| 長期記憶 | TC-13–TC-15、PA-13–PA-14 | 已保存回合→有來源候選→有效 claim→後續取用／更正；沿既有治理 |
| Runtime skills | SK-01–SK-12，相關 SP-28 | 已批准目錄、按需讀取、固定版本／撤銷，不因載入文字擴權 |
| 互動與通道 | SP-07、SP-13、SP-17、SP-22、SP-23 | Voice／canvas／群聊／表單等的實際介面與狀態；未驗外部路徑保持未驗 |
| 執行、工具與安全 | SP-08–SP-12、SP-14–SP-16、SP-18–SP-21、SP-24–SP-30 | 長工作、connector、委派、代操作與外部效果控制，重用既有權威紀錄 |

工作組有交集，不能相加計算完成率。權威清單始終是原 76 個 IDs，不刪除未啟用的條目。

產品實作使用自己的乾淨worktree與獨立PR；有明確依賴時可基於固定、已審parent作stacked接續，保留歷史，不在其他owner的工作樹改檔。每個切片仍有單一owner；evaluation帳冊另線維護，不覆寫未提交工作。Local parent整合不是GitHub PR合併，更不是部署或驗收；部署結果要有獨立版本／環境證據。

## 雲端授權與共同預算

最新處理見[9/26最小部署更新](fa-cloud-budget-status.md#2026-09-26-最小部署更新)：
兩RG同窗已報約USD88.2071，current accounting已可純讀，原預留／pending保留。
依最新使用者原則，已完成既有資源的必要部署與一次短smoke；共用USD150仍適用，
不以完整帳單或額外證據流程作部署前置。以下9/24內容及較早86.20讀回只保留其歷史時點。

使用者於 **2026-09-24 的雲端授權訊息**明確允許必要部署與設定，要求預算不超過 **USD150**；[原文及完整界線](fa-cloud-budget-status.md)另存，不以工具 turn 時間反推精確秒數。協調線先採保守的 **FA 工作共用累計上限**，不是每個 session／切片各自 USD150，也不把舊支出與未結責任歸零。

唯讀 preflight 的81份原件及跟進70份皆已核：兩個相關 RG 已回報 ActualCost 合計 **USD75.11207404930098846007**，後續沒有刷新此帳務值。57個job histories缺口已補，但完整current accounting無現成合法入口、遲報／保留責任尚未封閉，**可靠 headroom 仍 unknown，不新增計費操作**。單純差額 USD74.88792595069901153993 不是可花額度；唯讀探查到此停止，最小authenticated projection只在本機開發，不因寫好程式就假稱上線或有額度。

Cost Management 或預算告警不是硬性停費保證。若無法確認下一批費用與保留成本仍在上限內，就停在新計費動作前；不為補數字新起模型／診斷 job、不重送 unknown，也不擅自刪資源或清帳。這是本次執行上限，不改產品既有 `budget_enforced=false` 與 accounting 語義。

授權不取消 Opus 5.5／P1P2 關卡、資料保存／隔離、不可重送及當前身份核對。IAM／RBAC 權限變更仍須明確批准；已就必要真模型付費驗收詢問其是否包含在同一 USD150，未取得使用者答覆，不能視為同意。必要雲端步驟須先有適用批准、可採用版本、成本餘額與固定環境；已批准範圍不反覆索取相同授權。

## 帳冊工具

`controller/context_target_eval.py` 只驗帳冊結構、引用一致性及接受宣告的基本前提；**不執行 agent，不呼叫 Azure／模型，也不以 LLM 判讀答案**。

```powershell
.\.venv\Scripts\python.exe -X utf8 controller\context_target_eval.py validate `
  --registry docs\context-evaluation-status.json

.\.venv\Scripts\python.exe -X utf8 controller\context_target_eval.py summary `
  --registry docs\context-evaluation-status.json

$specSha = (.\.venv\Scripts\python.exe -X utf8 controller\context_target_eval.py summary `
  --registry docs\context-evaluation-status.json | ConvertFrom-Json).spec_sha256

.\.venv\Scripts\python.exe -X utf8 controller\context_target_eval.py record `
  --registry docs\context-evaluation-status.json --run path\to\safe-run.json `
  --recorded-spec $specSha
```

每筆新 run 必須符合工具的明確 schema，提供日期、kind、execution／outcome、範圍、目標／情境、版本、有限結果與證據識別。`--recorded-spec` 必須選該次評估實際使用、已在帳冊封存的規格；上例只適用於按目前規格執行的新 run，不能拿來替舊資料補造來源。不要把原始 messages、private system／reasoning、token、SDK snapshots 或本機絕對路徑放入 run。

同一 run ID 的相同內容及規格綁定重複記錄是 no-op；內容或綁定不同必須使用新 ID，不覆寫舊 run。新 run 的序號與陣列位置一致；使用 `--previous` 時，整段舊陣列必須是新陣列的精確前綴，不允許調序或往前插入來藏掉回歸。

Run 的 `sha256` 是去掉該欄位後的 canonical JSON fingerprint，與排版／換行無關；evidence 的 `sha256` 則記錄取件當時的原件 bytes。Repo 文件之後可能更新，Git checkout 也可能轉換 LF／CRLF，不能把目前路徑的 bytes 默認成原件，或靜默改寫舊 hash。工具結果的 source snapshot 明列實測時的工作樹檔案 hashes，不冒稱它們都是 Git blob hashes。

更新狀態後執行 `validate`；合併、匯入或人工改動歷史時用 `--previous` 指定可信的上一份帳冊，確認舊 runs、順序、規格快照及 run 綁定都未被改寫。上一份 schema 2 帳冊依自己的封存規格驗證，不用新規格回頭否定舊資料。帳冊採單一 writer、原子替換更新，**不是分散式 CAS 或產品 authority store**，沒有可信前件時不能宣稱能辨認任意重新偽造的資料。

### 規格變更與舊版遷移

規格快照包含版本、76 個條款、12 個情境，以及目的／scope 與第 3–10 節的共同規則（包含 §5.3、§9.1、C/H/M 判定說明）；基準歷史第 2 節、前言進度與來源索引不是驗收規則。條目 hash 同時涵蓋共同規則，情境 hash 另包含其引用的目標要求。

改規格後使用 `sync-spec --registry ...`，新增快照而不覆寫舊版。受影響的目標／情境撤下接受狀態並標為待重新盤點；宣告版本與共同規則未變時，未變條款可保留有效證據。宣告版本改變則保守地使所有條目／情境失效，不能假稱只重驗部分。舊規格的晚到結果仍可用原規格 SHA 記錄，但不同條款 hash 不能拿來接受新要求。

Schema 1 只能用 `migrate --registry ...` 明確升級，且需原本的 76 條目表格 fingerprint 相符；若已改規格，先用原規格遷移，再 `sync-spec`。舊 run 物件與 hashes 完整保留，其缺少共同規則／情境綁定的事實以 null 保留，**不回填新 binding**；舊 accepted 不自動成為 schema 2 的完整接受。過去資料仍能用於歷史／子集參考。

## 防止錯誤的完成宣告

- 76 個目標與 12 個情境都不可遺漏；變更條款、情境或共同規則後，舊評估不能因只改根 fingerprint 就成為新要求的接受依據。
- Subset／source review／tooling 通過不等於 full-target acceptance。即使程式 checks 全過，也不能直接把條目標 accepted。
- 完整接受需明確 `target_acceptance`、`full_target`、相同當前 baseline、已實作且無未結 blockers；C／M 要有完整程式 checks，H／M 還需逐條目的人審接受，不能拿批次接受代替。
- Null 的未知用量／時間／基準不補成零；failed checks 不可與 pass 同列。
- Run fingerprint 與歷史比較能抓出內容改寫，但**不能驗證外部證據真實性、人的身分、語意品質或通用秘密偵測**。這些仍由原件核對及適用審查負責，不能用一份自填 JSON 當成授權。
- 同一 baseline／條款若新增產品 fail／mixed／blocked／unknown，或相關人審拒絕，會撤下既有 accepted 並保留原成功與待處理原因。恢復 accepted 的 C/M/H 證據須在最後未解結果之後記錄，且 `observed_on` 不早於任何相關未解結果的最晚觀察日；同日才以記錄順序區分。晚到的舊 pass／人審不能當成新驗收。
- `observed_on` 記實際觀察／判定日期，不是匯入日期；不同時間取得的 checks 或人審分開記錄，不為通過新鮮度條件改寫舊日期。日期欄位不提供日內精確排序的保證。
- 客觀 outcome 與人審決定分開保存：客觀 `pass` 可以同時有人的 `rejected`，但該結果不能支持接受，也會觸發重新檢視；不為消除矛盾而改寫實際 checks。

新腳本本身的 pytest 結果屬 `evaluation_tooling`，不折算成任何產品目標通過。原 `m4_context_eval.py` 與 oracle 保持原樣；已知 `source_terms` 語意限制仍保留。

第一批工具結果及實際 source fingerprint 見[離線測試紀錄](context-evaluation-ledger-tests.json)；原始 stdout 不隨 Git 提供，帳冊中的結果不會再推導出模型品質分數。

## PR 審查關卡

本實作線的 PR 依使用者要求，由 **Opus 5.5（`claude-opus-5.5`）**分 Standards／Spec 軸線審查固定 head/base。P1／P2 必須修正，補回歸案例並讓同模型複核修後版本；測試通過不能取代 review clean。若查明誤報，保留理由與複核結果，不單方面隱去或降級。

Code review 是 source review，不是 Hosted evaluation、真人 gold 或完整目標驗收。結果記錄模型、所審 commit、各軸線發現與處理狀態；[Opus review 紀錄](context-evaluation-opus-review.md)已保留初審、修正及 `77ac9c5` 兩軸 P1/P2 清零的結果。逐版最終 PR-head 回條保存於 PR 說明與 session artifact，避免為把「已審本 commit」寫進同一 commit 而無限改變 head。

## 保留的歷史口徑

Round 1 是同一 conversation 的三回合；Round 2 是六個 conversation、十二個 user requests、十三次 model requests。兩輪都只有批次人工接受，沒有逐筆 gold；Round 2 的八個客觀 pass／一個 strict mismatch 不改寫。

CTX-03 的 ordinary filtered、本機 privacy block、candidate B 失敗、限定定位、default positive 與 Q11 唯讀核對各自保留。原 default positive 不含正式產品來源 gate，且沒有證明真 LLM 摘要品質；不能把它換名成新產品接線通過。

原件仍依[證據索引](context-research-evidence.md)保存界線管理。此帳冊保留摘要及 hash，不公開原始 traces、私有快照或資料庫。

## 本機交疊整合檢查

協調者另用 `git merge-tree` 產生固定tree並在新隔離archive跑必要selectors，沒有實際合併PR、切main或修改產品：

- `e80a215`＋accounting `e78c270` 的tree `4e84321e8598322154c68e7a1bfc6032f57cfae4` 無文字衝突，4個policy／API consumer案例通過，6.210秒。
- Prompt `c5cc284`＋accounting `e78c270` 的tree `7d9f6c0d13c8fe2b6f3136d2b74703acd7c37d19` 無文字衝突；原長basetemp為4pass／2fail `WORKER_START_UNKNOWN`。單一案例在160字元basetemp失敗、改48字元成功、在同一parent加長回160又失敗；原6個selectors只改短tmp後全通過，13.186秒。

因此後一批是本次Windows測試暫存路徑長度敏感，不當成已證產品合成回歸；沒有測到精確Win32 error，不宣稱通用MAX_PATH值。這和真正native短history的 `SDK_STATE_SOURCE_UNVERIFIED` 是不同問題。原red／green／source.zip均保留，自有extraction／tmp已清理；使用原task-local完整依賴環境，不當成新API-without-SDK證明。

## Verified against

- 本384-run保留old363/spec/migration/bindings與三個完整接受baseline，新增21筆分層紀錄。
  Fixture合批29/1與不同stub的base重現及後22 PASS各自有實測source；記憶原7批及P2修後4-case不混算，
  原Std兩P2保留，final兩軸分開記。資料匯入初次因artifact path可為絕對／相對而未找到XML就停止，
  ledger尚未寫入；修正為在所屬artifact目錄內解參照後才保存，不改測試／模型結果。
- 2026-09-26恢復前已核AGENTS／CURRENT、夜間checkpoint/verification、main03e3d45及未提交摘要，
  363-run原件hash仍`40ef5c64a2fd32b2e3a4b1908b4cf6790e51afadedb7182d73a3a618b98bec31`。
  新記憶切片入口核`behavior_work_memory_source.load`及`test_memory_no_sdk_conflicts`的合法reconcile交錯；
  只確認待修接點，沒有把未交付source當成已修或重跑先前完整矩陣。
- 程式基準：`77ac9c5ce6b874e78d54c31b29c091f681f1df13`，已包含另獲批准的 `a811f634615945c3f92c6fd860aa9bcdaddf8a93` 規範及觀察日期修正；本次收尾不改帳冊程式或測試，不是新產品部署。
- 已讀程式：`controller/context_target_eval.py` 的 `read_spec`、`entry_fingerprint`、`validate`、`unresolved`、`append_run`、`migrate`、`sync_spec`；及 `tests/context_eval/test_context_target_eval.py` 對應回歸案例。
- 實際驗證：worktree-local Python 3.12.10／pytest 9.1.1，明設 `PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`，執行帳冊與原 M4 兩個測試檔，共 91 passed；命令／source fingerprints 見[觀察日期修正結果](context-evaluation-observation-date-tests.json)。
- 證據與限制：schema 1 的十二筆 run 保留，先前 71／88 結果不覆寫；Opus Standards／Spec 對 `77ac9c5` 均已確認無剩餘 P1/P2。這些是工具測試及 source review，不是 Hosted／Blob、真模型品質或全部目標驗收。
- 前次資料增補：PR #6 的 final head `7b80172` 已經兩軸複核並合併為 `5d8ebb1`；先前追加 #8 初稿 `eddc9de` 的四批驗證與雙軸 review，以及新雲端授權。對應 manifest 八份 artifact hashes、四份 JUnit 與 native proof 的關係已核，沒有為收件重跑該批產品。
- 新檢查點：從已審 `ba2cc094048455ee752a87b5e3f8730022881fd7` 的48筆run追加35筆到83；舊陣列／規格快照／bindings保持。來源包括Prompt原8批及相依23／1、短history red／controls、signed修正、獨立交疊檢查和3批skills probes；沒有新帳冊程式碼／M4 oracle變更。
- 已核source與原件：`c51e730` signed修正、`a9a3ce1` prompt整合及各自Opus回條；native-skills的initial8、resume7、exception5 pins。限定SDK實測、owner測試和協調者收件／交疊測試分開，不混當一批全綠。
- 前一83-run檢查點的未完成項：共同USD150可靠headroom、Q14／真模型／必要人審與其餘完整目標；當時approved runtime-skill adapter仍在實作。Ready PR與本機片段不產生完整target／scenario接受。
- 本日增補基準：`37b09dd9388f2082bc7e7f756ad6e6c2db749e33` 加本次文件／資料更新；已讀 `m3_skills`、`behavior_skill_prompt`、`AssistantAuthority.issue/current_skills`、`restore_turn`及相應測試，確認第一切片的whole-catalog限制。#12 adapter已合併，下一個catalog continuation仍在實作；不回填前一檢查點結果。
- 收件證據：原產品handoff、commands、native proofs與P1修正handoff／各JUnit重新核對，102-run資料按原83-run前件驗證；只有資料／文件更新，不修改已審的controller、runtime或判定規則。
- 新routing來源：`0f055f6`的4-case結果與`1252ea4`的公開runtime版本觀察皆在commit建立後執行；之前WIP與錯誤碼expectation的FAIL分開保留。新增6筆後是108筆，只有SP-06增為partial；產品程式及規格／帳冊工具未改。
- Catalog收件基準：main `578d8805db4da429f7c604b204bfd34b3d4f53c2`加本次資料／文件；原與修後handoff、15份JUnit及各版本工作檔hash核對，兩軸pinned-head review已完成。13-case union archive對到實際merge tree；131-run資料按108前件驗證，不把資料匯入當新產品測試。
- Prefix新增基準：`1ac534830572c25b249d0fa19c230aa3c8d83df5`只有一個新native test file，產品source仍等同`d28605b`。135-run資料按原131筆追加，PA-02仍partial、其餘target／scenario狀態未變；未把no-SDK新工作線的WIP或原BLOCKED診斷寫成已完成。
- 本次167-run來源：原5個blocked診斷、14個no-SDK WIP批次與來源索引、四份#17/#18 review、no-SDK/main整合、六個structured／qualification觀察及37-case候選。PR19 Spec P2後僅更正該未合併候選的coverage／宣告，原37結果與舊135/spec/migration/bindings不動；SP-09僅partial，沒有human gold或scenario接受。
- 修後172-run另加原PR19兩軸、首次五案、`5de36c2`第一版42-case subset及`22e62ac`最終42-case full C。舊37不改成42，WIP不冒認commit，摘要request不混作normal prefix；新接受宣告仍待同兩軸對最新head複核。
- UI三筆初稿從172→175時，基準為`234dde4`測試／說明；已讀workbench `renderConversation`的saved-reply／stream優先序與既有minimal-DOM harness，沒有新HTML／bridge行為或實際backend calls。該階段原172/spec/migration/bindings與舊PA-02 state不動；reader整合後的current baseline另外重新資格核對。
- 195-run交付的reader結果逐份核15XML與369個Python檔在`f52654c`／`57ab942`的相同Git blob，測試時間晚於source commit；新PA-02來源是`2b942a1`建立後的62-case。已合併172筆及其spec／migration／bindings完整保留；TC M品質仍未接受。
- 本次226-run保留原195筆，追加TC-01早期／完整／容量整合後的各層結果，以及capacity原12／修4批、初審／修後各軸及14-case snapshot。原structured P2與configured/runtime/wire差異不回填；新TC-01提案依`12f624f1`的36checks，既有PA-02 baseline不改，沒有新增human gold或scenario接受。
- 本次236-run對照已合併226筆；診斷本身沒有Git test commit，七批保持null baseline並附逐檔source hashes，產品仍clean `1b7f4d9`。Coordinator核372 source pairs、七份XML、V1／V2兩欄差異及自己固定SDK與官方copies相同，不執行探針；TC-04只新增具體blocked理由，原兩個完整接受及12情境狀態不變。
- 240-run的TC-11候選使用相同已提交source與完整原61 selectors，實際13:49:47 pytest開始；舊環境失敗不回填。原236/spec/migration/bindings保持，TC-04只加PR24 review evidence，PA-02／TC-01 state及12情境不變；新的完整接受宣告仍須固定PR審查。
- 249-run另存compaction source推論更正、兩個session probe、repo RED／WIP GREEN、fixed101 regression及其TC-11 66-case子集、PR25初版兩軸結果。101與66共用同次執行，不重計model requests；原240 runs均保持，TC-01只改current state為partial。PR25範圍已擴為單一observer修補，初版docs-only clearance不套給新code。
- 本次283-run保留已合併249筆，追加purpose index／20批／三版review、34-case union、新WIP11、137合批與80/71共享子集、PR25新scope兩軸。原376/377 source pairs及20份XML時間/hash已核；current接受只據`ed6f64e`新baseline，未改TC-11或12情境判斷，沒有新增人工gold。
- #27合併讀回為`ecb566020dab21bd3d77856b59cc7b44625cd1ba`，parents=e2460ea＋0c034f9、tree`83338cc8d210fa98650e8845ce5aa217fcf53031`符合審查head。本285-run只新增兩份source-review及既有case subset索引，original283與三個current accepted baselines不變，未重跑SDK／產品測試。
- 新memory-reader方向已讀`BehaviorRuntime._source_context/current_claim/context/_reviewed_context`、`MemoryGovernance`的reviewed-claim/source/revocation接點及`derive_work`的claim_ids限制；這是有界實作入口確認，不是新能力或private-scope批准。
- Reader實際交付另見[本機證據](reviewed-work-memory-evaluation.md)：owner四個受測source和383個Python hashes／blobs、20份XML/source/time及原17批不變均已核。Incoming#29的285帳冊與另一套runtime baseline／費用檔未覆蓋；原shared headroom UNKNOWN，不新增計費操作。
- 本次318-run保留old285/spec/migration/bindings，新增memory20批、各版review／source索引、WIP11、新193及109/75重疊子集、PR28兩份review。PA-13只升partial；TC-11 baseline／12情境／既有人審不變，不把193或Memory接點當M品質驗收。
- 本363-run另保留兩線已合併337筆與所有current full-C baselines，只追加v2原13／整合5、兩版分軸review、PR32已完成review及57-case帳冊工具回歸。Generic衝突三RED是真合法job/query更新，不是注入error；targetedDOM只一個renderer案例，沒有真browser或額外agent-quality樣本。
- V2 merge讀回`ddbcd64e222daa53024e5cfa58eebf9a438e9b38`，parents=3f7391d＋9c276cd，
  tree`422397d67d71d75a2f19f3ca755790f9d4cf322f`符合final審查與preview。Code/test/DOM對10aa等值，
  pure docs/data merge未跑新測試；peer TC-01的0dac完整資格與337前綴未改。
- PR36初稿6decaa2誤將22筆新run歸到TC-08，Spec P2後僅重建未合併部分為TC-12；引用的規格是
  「已確認拒絕且可安全新嘗試與unknown分開」。TC-08恢復原unassessed、TC-12原31筆evidence保持，
  原337／accepted baselines／spec／migration不變；新run結果、觀察日與原件hash不改。兩個目標原本都未盤點，總partial數不變但歸屬已修正。
- 同組採入peer已審#35，main291dd4b的產品／tests／controller逐blob保留，唯一merge conflict為CURRENT；
  原話reader回饋的實測1a80402與兩個P2修補沿[該線報告](user-turn-refusal-feedback-evaluation.md)，不改寫成此資料PR重跑。
