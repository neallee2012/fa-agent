# Runtime skills：固定 SDK 接點與受控來源產品驗證

## 正常Unicode空白的政策本文相容性

2026-09-27接續SK-07 review的既有問題：在未改產品的已發布base
`047879bd93356893d34590f279c9144b0d3866c8`，新增直接案例得到6 FAIL／6 PASS。
Skill／SOP的全形空白／NBSP本文、用途及title被Python `isprintable()`誤當成隱藏控制字元；
安裝的Python字元分類讀回確認兩者都是`Zs`，而RTL override／NUL仍分別是`Cf`／`Cc`。

現由同一`policy_text_is_visible`接點允許正常`Zs`，沿原M3 producer、current policy reader、
skill body／title／purpose validators一致套用；不normalize、不改既有bytes/hash/schema。
控制／格式字元（包括NUL／ESC／DEL／zero-width／RTL override／BOM）仍拒，
與純資料resource允許BOM／joiner的規則分開。
本機直接12案與既有purpose／M3 artifact回歸合批89 PASS；不是新SDK／model或雲端結果。
原`policy-unicode-red.xml`／`policy-unicode-green.xml`留存，不把WIP結果回填成之後commit執行。

固定`56b823597a1cc282e787ac53f8b13c0a764f11f7`於2026-09-27T14:12:50+08:00
只驗新檔12個直接正反例，12 PASS／0 FAIL／0 ERROR／0 SKIP，0.634秒。
JUnit `policy-unicode-fixed.xml` SHA256
`a4bbca3bb39ff261a3eb2aad23b784b689f4fde2377c4854d871b601ce944ae0`。
這是literal text相容性修補，不是新profile／新的語意品質資格，帳冊接受狀態不變。

## SK-07：批准manifest的附帶文字資源

### SK-07：native 附檔作者（2026-10-03）

live合成skill準備缺作者入口，總協調11:04記BLOCKED、不擋部署；11:09核准短計畫SHA`f4af78e2…`，
只擴原save_skill_draft，無新API writer／Actor例外／IAM／工具需求作者流程。
基準`7b3f0736`：新resources欄位先RED（extra_forbidden），M3同native source與真人批准接通後16案通過。
直接批13PASS／3fixture失敗是startup禁止loop socketpair；改成既有先建loop再取fixture順序，
後批24PASS／2FAIL抓到resource pending的blob404被當一般notfound；改沿原reconcile報UNKNOWN。
`native-draft-resources-sdk01`30PASS，含兩lane×正常／失API ACK的真SDK／離線模型，
Agent起草兩檔→真人批准→selection→新CID讀取；沒有額外PUT。`extra04`另8PASS核最壞Unicode
JSONescaping、resource create ACK不明、native／legacy雙向來源拒絕；各批重疊不加總。
無／空附檔golden由published舊wire取得：material SHA`5051673dd56d8fc4e1225506f338c98d9e89b0ee8d85428ec7baf3e0ae5cc040`，
回條JSON SHA`3b92e1c8148428d113241f952bb4af160d6e363e00fc05a3b4157f6045a0a4c4`。
原記錄不改寫；本線原件在session files `native-draft-resources-*`，未執行live或更新評測百分比。
`full01`共306案：**305 PASS／1 FAIL**；本片新38案全部通過，失敗為未使用draft writer的
`test_only_allowlisted_assembly_events_survive_and_no_event_is_not_zero_compactions` 在State store回
`NATIVE_STATE_STORE_UNKNOWN`，歸因未驗，不能稱為既有問題或改成PASS。
同一selector獨立補驗1 PASS；再以未改的assembly整檔40案＋save/reconcile雙入口
32／128KiB精確外殼4案，`final02` **44 PASS／0 FAIL／0 ERROR**。後兩批不回填或加總原批。
舊M3 Actor／human區別、v1草稿、client/CLI、①②reader、原C5與API-only冷匯入均在上述相干範圍，
仍不外推到Hosted或真人批准／模型品質。
其後普通整合已發布 #502／`e5f5215c`（relay fixture隔離），產品與本片測項未改；
`native-draft-resources-join03`新作者38案、舊draft API15案及assembly40案 **93 PASS**。
這是新來源批次，不推定#502是前述單次State store未知的原因，原305／1仍保留。

### SK-07 ①：native C2 批准附檔（2026-10-03）

本輪依計畫只接既有4×8KiB、同Team／source_work文字manifest，不擴作者流程／binary／腳本工具。
`test_native_skill_resources.py` 是新的 N1／兩lane完整路徑，不借舊 Job-bound 的「native」測項代替。
第一輪 fixture錯用舊Actor在M3授權被拒；修fixture後真正RED是原生入口409 `NATIVE_PROFILE_UNSUPPORTED`，
新consumer接通後PASS。之後Responses fixture缺journal目錄的setup錯誤另存，補齊後兩lane與冷恢復通過。
新增能力預設OFF，舊skills grant固定 `a82ccd4890a37a413b7443488d929cbe643d738360e6b24002a21e6eed66db59`。
同RID讀精確manifest／原檔、移除reader後冷恢復、舊v2拒新resource API、8192-byte Unicode、
錯CID/RID／路徑／URL／未列腳本與停用反例均走原reader；未註冊腳本工具沿SDK拒絕，仍可讀合法資源。
分批 stdout／JUnit 留本線 session files 的 `sk07p1-*`，不回填原FAIL／setup錯誤，不改全域評測帳本。
`sk07p1-full01` 收集時缺 MCP 套件，沒有執行案例；只在本線 session venv 依既有
`requirements-behavior.txt` 補齊固定相依，manifest未改。`sk07p1-full02` **101 PASS／0 FAIL／0 ERROR**，
含新6案、既有native capability API／provider、M3資源與越界、能力依存矩陣、PA-16組裝、原十能力／
skillify→批准→reader與API-only冷匯入。真SDK案例使用1.0.13／1.0.83、本機State store及離線模型；
其他案例是純契約／替身I/O，不當雲端／品質驗收。選取矩陣補新能力相依，原十能力fixture不自動選入新opt-in。

### SK-07 ②：工具需求不授權（2026-10-03）

①先於台灣10:20:45正常合併#511／`d5502cd3`，兩軸CLEAN、101本機PASS；②才開始。
工具header保存／精確metadata批准先1 RED，再1 PASS；native正文缺v4需求／可用性先2 RED，
接原C2 reader後3案通過。擴成兩native lane、移除delegate後冷恢復、缺實際consumer、API偽available、
unknown tools／path／script／grant欄位、錯hash、版本不可變與舊reader明拒後，直接14案通過。
缺delegate仍讀合法附檔且不派child；有原captured/bound task才沿原SDK子回合整合，全部用量在原RID。
工具manifest沒有新增權限、installer或dispatcher；未執行live，不改全域帳本／SK-07 acceptance。
原件在本線受控files的`sk07p2-*`；分批互有重疊，不加總或回填RED。
`sk07p2-full01` **222 PASS／0 FAIL／0 ERROR**（132.41秒），含新15案（加CLI預覽）、
①資源全鏈與舊v2/v3相容、M3版本／批准／用途／client未知回條／CLI、native C2／C0、
PA-16、原十能力／skillify與API-only冷匯入；只一個既有Starlette deprecation warning。
固定SDK仍1.0.13／runtime1.0.83；模型HTTP離線、State store本機，不當live證據。

### 原資源切片（2026-09-27）

2026-09-27本機增量：M3版本化manifest＋目前讀權＋新resources profile的custom tool。
只取同Team／同source_work、固定deliverable版本的≤8KiB文字；沒有任意filesystem／URL、
script／plugin或雲端操作。舊profile不新增工具；新prefix不能借舊矩陣算完整資格。

開發批次依實際層分開：首批6 PASS／4 FAIL是strict Python解析把JSON array當tuple拒絕；
header採JSON mode後58 PASS／2 FAIL，指出M3持久化再讀仍需要JSON list；
改成與既有M3版本一致的list後9 PASS／1 FAIL是測試把含ETag的HTTP envelope當storage DTO。
修測試讀真正record後10 PASS，擴scope/client/body拒絕後與purpose合批67 PASS。
新profile/API與舊purpose直接6 PASS；相關回歸131 PASS／1 FAIL是停用fixture漏schema_version，
補正後只重跑該案1 PASS。後續直接25 PASS包含新manifest守衛及API不依賴Hosted SDK的cold import。

一個真固定SDK＋scripted-provider情境1 PASS：依序`read_skill`取得已批准manifest，
`read_skill_resource`取得精確完整JSON文字，再回最終答案；1runtime／3個provider transport請求，
正常stop。它不是自然模型是否會正確選資源的品質測試，也不是Hosted／Azure／Blob驗收。
此native批次先於reader合併兩次initial metadata讀取的小整理，不能倒稱新來源已重驗。
上述都是WIP，原失敗與fixture錯誤保持，不相加成一輪全綠；原件為本session的
`skill-resources-first/header/core/core2/producer/api/regression/revoked/direct/native.xml`各批。

### Verified against

- M3 upload／decide／effective／download與client/header；manifest由原metadata digest一起批准，
  相同version重送不能換resource參照，read前後核有效skill與來源。
- 新resource prompt／API及provider／Hosted tool boundary；僅目前profile可讀，舊schema拒新v3。
- `test_skill_resources.py`及`test_skill_resources_api.py`直接守衛，另一個
  `test_skill_resources_native.py`只有實際SDK與scripted transport，不是真模型或全部平台。

### 固定source 24eb7cb

2026-09-27T12:05:15+08:00，在
`24eb7cb811b9e32b2df22de067eb027cc40705c0`以三個直接檔與API cold-import案執行，
**26 PASS／0 FAIL／0 ERROR／0 SKIP**，18.795秒。
其中只有1案使用真SDK1.0.13／runtime1.0.83、1runtime／3scripted requests；
其餘25案是local契約／I/O替身／cold import，不能相稱native。
已包含目前reader的單一initial metadata read；原native WIP及較早67／131案不回填此source。
JUnit `skill-resources-fixed.xml` SHA256：
`e101cf4c1c8aad91c25cb7e67b2fb87695e6e318e0eb696bcf3fe2cd4e6444af`。

帳冊由原618追加此一筆至619，SK-07只partial at24eb。
初版曾保留4個current C，但新resource prefix尚未取得共同矩陣資格；review後current
PA-02／TC-01明確回partial，原c8e接受run／baseline原樣保留，current只餘TC-11／SK-06兩個C。
這個資格收窄不新增部署gate，也不把完整矩陣列成先決條件；新profile尚未部署／啟用。

### SK-07 review：正式provider與目前資格

兩軸都找到正式`_CurrentAssistantProvider.send`漏收／轉送`skill_resource_access`的P2；
原26案只有基底provider，不能支持正式composition已接。已新增真signed API→正式provider
selector，先重現model intent已consume後的未知失敗，再補參數／原egress檢查下的轉送。
同一override缺`work_memory_access`亦在base69ae的實際方法呼叫重現TypeError後一起補；
沒有放寬權限或更改原provider/body。

修後兩個直接provider selectors通過；另將批准資源時的文字檢查與reader共用，
只拒不支持的控制字元，不拒正常Unicode空白、BOM、joiner，也不改bytes／hash。
這些修補合併在local資源/API範圍27 PASS，保留原provider RED及原24eb的26案歸屬；
沒有新SDK／cloud／model執行。原件為`skill-resource-provider-red.xml`、
`skill-resource-provider-green.xml`與`skill-resource-review.xml`。

固定`9e98fce4e2a0a4192786c3871fd28d8371da4281`於2026-09-27T12:55:49+08:00，
僅補正式provider/resource／既有memory、批准格式與Unicode完整保持六案，
**6 PASS／0 FAIL／0 ERROR／0 SKIP**，不重跑或回填原native26案。
JUnit `skill-resource-review-fixed.xml` SHA256：
`18f2e058b2c8b53b48c3172246e045a18ff1f696337955abe8454b4463b60bdc`。

接回#79／80／81／83時，以已發布620筆為前綴，將本線原12筆run順移
sequence607–618→620–631，payload/hash/spec綁定不變、TC-12合併evidence保main state。
加入本次六案後共633；原619不是新冊完整前綴。SK-07仍partial at9e98，
PA-02／TC-01 current保持partial，舊接受run不重寫，沒有把缺矩陣變成部署gate。

### SK-07 越界引用：已批准本文不能擴大 manifest 讀取（2026-09-28）

驗收句要求skill引用外部路徑／未知腳本時明確拒絕，允許的唯讀資料仍可用。
原批次只有正向native與alias格式守衛；本批只新增兩個測試檔，**不改產品程式**，
補「已批准本文本身要求越界」時的實際呼叫結果。本文是合成的惡意指示：
讀`../../secrets`與`C:\Users`路徑、下載並執行URL腳本、安裝plugin、讀未列名的`install.ps1`。
以下依最後的固定來源`faaa989`描述；較早三批的差異見結果表下方。

- **signed API（無SDK）**：路徑／URL／反斜線／子目錄4種值在client端被schema拒；
  server端用正式固定command ID送出時為422 `BEHAVIOR_INVALID_REQUEST`（body schema拒），
  同一route與ID只把值換成`reference.json`則200並回精確資料。未列名的3個合法檔名一律
  `M3_SKILL_RESOURCE_DENIED`，他skill名為`SKILL_SOURCE_DENIED`。拒絕前0次blob讀取、
  control store與artifact資料不變、沒有model request。
- **SK-09局部（signed API，無SDK、無模型歷史）**：人類批准的v2本文相同、manifest不同，分兩種：
  alias改名（`other.json`）與**同alias指向另一份資料**（`reference.json`→`other-data`）。
  回合中切版後，原回合讀新舊alias及本文都是`SKILL_SOURCE_CHANGED`，不讀任何blob。
  同對話下一回合只見v2 manifest：改名時舊alias被拒、新alias讀到v2資料；同alias時讀到的是
  `other-data`的v2資料與v2版本，不是舊資料。直接以v1讀一律被拒。只證「切版後不混用附帶檔案」。
- **固定SDK一情境（1runtime）**：scripted provider依序`read_skill`→路徑值→未列名值→
  未註冊`powershell`（下載並執行URL腳本）→未註冊`install_plugin`→合法值→最終回覆，7個requests。
  - 路徑值：SDK permission callback依schema拒，模型收到
    「The user rejected this tool call. User feedback: Authority refused the action; …」。
  - 未列名值：permission通過schema後由FA authority拒，模型收到固定的「Authority refused the action; …」。
  - 兩個未註冊工具：沒有觸發permission callback，SDK回「Tool '…' does not exist.」並列出
    四個可用工具，完成事件皆為失敗。
  - SDK session看到的是虛擬根`/`，runtime process的實體工作目錄是`<executor root>/<session>/work`。
    惡意路徑從該工作目錄解析的位置實際放了含隨機內容的canary檔，執行中已斷言解析結果就是它；
    7個request都沒有出現該內容。資源資料只出現在合法讀取後的最後一個request。
  - 每個request只有profile的四個custom tools，builtin／MCP排除、discovery／ambient目錄皆空。
    模型工作期間只讀`reference-data`；回合結束後的讀取只有本回合SDK checkpoint自己的artifact。
    succeeded、forwarded、graceful stop、force0。

固定來源四批，各自保留：

| Source | 時間 | 結果 | JUnit SHA256 |
|---|---|---|---|
| `e60dfad478996db761554dab10cbb6b500e6ecea` | 2026-09-28T14:11:21+08:00 | 3 PASS／**1 FAIL**，131.698秒；native 1runtime | `f6c551ce5cd9116e2c6f081b101923f8623a16d842d09522adcf0c2a481da095` |
| `c06310d2f75e8ea2d3e0946be7cad7b3583eeae4` | 2026-09-28T14:16:36+08:00 | 4 PASS／0 FAIL，109.749秒；native 1runtime | `638887dcdc54db8759e2117d024ab0290a5a1facdd143bac303eaeb4e768a8f0` |
| `04253efd24a507f6aa170a3baa5c7190cfa20f37` | 2026-09-28T14:40:27+08:00 | 6 PASS／0 FAIL，17.378秒；native 1runtime | `1350e5ebcdbdb25f2ca0be35cd15fcf9ed1aef205424b2fda1f6b67b0ce43f20` |
| `faaa98978c03f2f2b6a0da5abb99b16c8f94f2d9` | 2026-09-28T14:50:01+08:00 | **6 PASS**／0 FAIL／0 ERROR／0 SKIP，103.321秒；native 1runtime | `5400e672ceebf8b9b4b607224965bacfefefd9af7691ae59a5b5e455024ec05e` |

- **e60dfad的FAIL是測試oracle錯誤**：當時是5步情境，產品行為與上面對應的部分相同，但最後的blob
  讀取斷言把回合結束後SDK checkpoint保存的讀回也算進去。原FAIL與JUnit不回填成PASS。
- **c06310d**只改native檔（+21／−6）：讀取改為模型工作期間的快照、資料偵測加上一次escape、
  路徑拒絕斷言加嚴為須含「Authority refused」、最後一個request加資料出現的正向對照，以及改名證據欄位。
- **兩軸初審後的更正**：前兩批的server端路徑案例用錯command ID。程式先驗body schema，
  所以當時的422很可能就是schema拒；但就算schema放行，錯ID也會回422 `ASSISTANT_COMMAND_ID`，
  測試分不出兩者，**不能證明server端是schema拒**；client端拒絕與native permission拒絕不受影響。
  04253ef改正這點，並補精確permission／完成事件與拒絕原文斷言、未註冊工具、檔案canary、
  任意escape深度的資料偵測、回合後讀取只限SDK checkpoint，以及同alias切版。
  前兩批的server端422紀錄保持原樣，原因無法區分，不回填成schema證據。
- **兩軸複核後的更正**：04253ef把canary放在executor root往上兩層，但實際工作目錄是
  `<executor root>/<session>/work`，惡意路徑解析不到那裡，因此該批的檔案canary檢查
  **不能證明檔案未被讀取**；其他斷言不受影響。faaa989把canary移到正確位置並在執行中斷言，
  也改成精確斷言「does not exist」原文及未註冊工具不進permission callback。
- 不計入結果的開發批次：`wip-api-1`（3 ERROR，測試漏匯入fixture）、`wip-api-2`（3 PASS）、
  `wip-review-api`（5 PASS，只跑API檔）。原件在本session的`evals\sk07-boundary\`。

本批**沒有**涵蓋：惡意本文下呼叫`execute_operation`、登入帳號、binary資源
（binary在上傳前被拒的證據是較早的`test_skill_resources.py`批次），以及自然模型是否會照惡意本文行動。
這些能力在此profile只是未提供或另由其他批次負責，不能由本批推定。
只測resources profile、一個Windows runtime與本機I/O替身，不是Hosted／Azure／Blob或部署驗收，
profile仍未啟用。帳冊資料待共享佇列輪到後另加；在兩軸複核與帳冊落地前，SK-07仍記partial。

#### Verified against

- 程式基準：main `166a1bb822314a8c1447e6c68605781892aedc79`；本批只新增測試，產品未改。
- 已讀：`AssistantAuthority.read_skill_resource`（原profile、captured catalog與current skills前後核）、
  `ArtifactService.skill_resource`（只讀目前effective版本manifest所列alias）、
  `behavior_assistant_api`的固定command ID與`behavior_api._body` schema拒絕、Hosted
  `read_skill_resource` handler、`m2_copilot`的permission／`tool_failure`、Hosted以`root/work`作為
  process工作目錄而session選項為虛擬根，以及`SdkCheckpointStore.save`的逐part讀回。
- 實際驗證：上表四批，Python3.12.10／pytest9.0.2、SDK1.0.13／runtime1.0.83，runtime binary
  SHA256 `615052c83c747d6825ab5e99ebf3faf8f0eefde20a985f99ced24c9023313ff4`。

## SK-03／04：技能目錄預算與漸進降級

2026-09-27本機切片，預設OFF、尚未部署。[實作契約](../src/fa-shared-context-poc/README.md#技能目錄的-token-預算與降級local預設-off)
只對既有skills＋capacity profile再明確opt-in的工作生效，不改skills來源／批准／完整正文。
目錄description用配置的文字tokenizer計數，不當provider usage；宣告容量不冒充有效模型硬上限。
先保完整metadata，再明確excerpt、names及omitted；省略名稱仍能走原reader，schema不以enum把它封死。

開發批次分開保存：unit16 PASS／1 FAIL是句尾標點使點名排序未生效，修後17 PASS；
初次合成API26 PASS／1 FAIL是fixture只改檔、沒更新已載入model facts，修為既有loaded-facts接點。
相關單次回歸78 PASS；補接單前容量拒絕／同SID冷檔案系統接續後，直接API17 PASS。
明確excerpt避免冒稱完整批准用途後，unit＋API34 PASS。
另有真固定SDK的一個scripted-provider情境通過：1runtime、2個model transport requests，
實際wire description在BPE預算內，被省略的有權名稱可載完整body，原保存catalog仍完整。
該native批次在excerpt文案調整前執行；不能把它當調整後來源已重驗。

原RED／fixture差額不回寫，以上均為WIP，不相加成固定來源的一次全綠。
原runtime1.0.83、正常stop、無native skill／compaction事件；沒有呼叫真模型或Azure。
Byte-length counter只在純演算法測項作替身；API／native用實際固定`o200k_base`，兩者不混稱。
這是SK-03／04的部分接線：權威catalog原4-entry／4096B不變，未測任意大目錄、
provider自身分詞／整個history fit、語意相關性或正文容量不足的完整處理。

### Verified against

- `behavior_skill_catalog`、`AssistantAuthority.skill_catalog_presentation`、
  `AssistantClient.skill_catalog_presentation`及原`read_skill`。
- 原profile／task／template參照的optional欄位與API safe errors；未新增持久控制型別。
- 本session的`catalog-budget-unit.xml`、`catalog-budget-unit2.xml`、
  `catalog-budget-api.xml`、`catalog-budget-regression.xml`、
  `catalog-budget-api-final.xml`、`catalog-budget-excerpts.xml`及`catalog-budget-native.xml`。
- 固定SDK來源／正式能力差額見上方實作契約；後續固定source結果另記，不回填本節WIP。

### 固定source 9b634fa

在`9b634fa21ac7d712e6f8924f25021939bbb076ce`於2026-09-27T10:04:18+08:00
執行三個直接測試檔，**35 PASS／0 FAIL／0 ERROR／0 SKIP**：
17個純formatter案例、17個BPE／signed API／SDK I/O替身案例，以及1個真正SDK情境。
本批已包含明確excerpt文案；SDK情境只有1個runtime／2個scripted requests，
沒有真模型、Azure或本次部署；舊78-case回歸不回填為本批。

JUnit `catalog-budget-fixed.xml` SHA256
`b8ec7c6c46a85dd0233186afee648c05baf11f882f776d75b0836ca2908ee89d`。
Python3.12.10／pytest9.0.2；native維持SDK1.0.13／runtime1.0.83。
帳冊只新增本批一筆SK-03／04 subset，另接下方#72原SK-09的4-case回條，579→581。
原579前綴與4個C／12情境保持；SK-03／04現在partial，不宣稱完整目標或部署已完成。

### 接回工作台的相容檢查（WIP）

接回#74模板工作台後，另只驗2個必要consumer案例：新budget參照經shared DTO完整下載／
送inspect，及原thin-client簽章API路徑，均PASS且沒有工作／journal副作用。
此為merge WIP的`catalog-budget-template-interop.xml`，不是把原35案重算到新來源；
未重跑原46個DOM或browser，也沒有操作使用者8765／journal或Azure。

### Review修補：中文點名與接單前可行性

初審各有一項P2：Spec發現Python預設`\w`把中文字算入識別名邊界，
「請使用<name>技能」無法優先保留；Standards要求把接回SK-09回條的檔名／hash
在來源段落明列，不能讓帳冊引用缺少說明。兩者均已定點修補，原回條payload不改。

另處理已知不足預算仍接單的可用性問題：先以同一formatter／counter核原selection各種
可用數下的最小提示，放不下或counter未知就於registration／預留前明確拒絕，
不等進入工作後才因必然的TOO_SMALL停在未決。沒有硬寫死64 tokens或另一道人造上限。
點名邊界改用ASCII識別規則，保留中文相連的正例及原ASCII完整名稱界線。

新兩個反例原先2 FAIL；修後直接formatter／API檔38 PASS，均WIP，
含新unknown-counter接單前反例，不混入原9b634的native資格。
`catalog-budget-review-red.xml`／`catalog-budget-review-complete.xml`保留；
沒有新runtime、model、Azure或使用者工作台操作。

固定`dd6e5903ab100f89a7bdd243530cf4fd0e3789fb`後，只補四個直接正反例，
2026-09-27T10:55:25+08:00為4 PASS／0 FAIL／0 ERROR／0 SKIP：
中文相連、已知不足拒接、未知counter拒接及合法Hosted接線／同SID接續（SDK I/O替身）。
JUnit `catalog-budget-review-fixed.xml` SHA256
`227b3674595a6cd4264c782870c0993807defec60829bc05353f5f1297fcf031`。
原35案／native維持9b634歸屬，不重跑或回填。

整合#73時，先保已發布604完整前綴，再把本線原兩筆skill run順移至604／605；
payload／hash／spec綁定不變，原581不是新冊完整前綴。新增此四案後共607筆，
SK-03／04仍partial、修補來源dd6e；主線SP-13／PA-16／TC-12及其他原資格不動。

## 既有批次起點（2026-09-25）

日期：2026-09-25。狀態：**受控技能來源 #12 與合法terminal回合的目錄接續 #14 均已合併，預設 OFF、尚未部署；不是完整 SK 驗收。** 下方保留原9/24 native相容性fixture及後續各版結果；產品沿伺服器端批准／來源檢查與SDK custom-tool loop，沒有啟用會受callback fail-open影響的native loader。

## 固定來源

Repository baseline `5d8ebb1fa9f3517a8cf8d92e326a47e446603fd3`，Python 3.12.10／Windows AMD64，`github-copilot-sdk==1.0.13`／runtime `1.0.83`。官方 source 固定於 [`f13e4a2cc7e4e220974d2333142234e162a3252e`](https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e)。

使用正式 `CopilotClient(mode="empty", use_logged_in_user=False)`、`CopilotRequestHandler` offline HTTP、`RuntimeConnection.for_stdio`、明確 skill directories／enable／disabled 設定與 `ToolSet().add_builtin("skill")`，沒有 internal raw RPC 或發明 skill-provider callback。

Discovery、managed settings、file hooks、ambient instructions、plugins、MCP、其他 agents、generic read／shell／browser 保持停用。`on_permission_request` 一律以拒絕為基準；實際所有 probes 的 callback 次數為0，**不能把沒有發生的 read-approval branch 當權限驗收**。

## 三批不同問題、不同結果

| 批次 | 實際結果 | 新 offline requests |
|---|---|---:|
| 初次 create/load/resume | 一次保留的 blocked attempt 後，受控目錄初次按需載入成立；同 SID 恢復／reload 不會自動重附加正文 | 5（2＋2＋1） |
| Resume 明確再 invoke／explicit deny | 再 invoke 重新附加正文；正式 typed pre-tool hook 的明確 deny 有效 | 4（2＋2） |
| Callback exception | 例外 warning 後原生 tool 仍 success、skill.invoked=1，正文進入下一個 request：**fail-open** | 2 |

只描述上述有界批次，不宣稱所有 SDK JSON-RPC 或 OS 全機網路流量。Provider usage 是 synthetic 100／10，不是真實 token／費用／品質資料；沒有 remote／paid model call。

## 初次按需載入與同目錄恢復

Native 管理 list 有兩筆 metadata，只有合成批准項目 enabled；disabled control 仍可出現在管理 list，不能因此聲稱跨 scope 全不可見。模型請求只見允許的 catalog／skill tool，沒有 disabled 名稱或正文。

第一個實際模型請求只含 name／description，沒有 body marker／version。依觀察到的 `skill` schema 呼叫後，第二個 request 有對應 tool-call/result、exact marker／version 與 newline-normalized 完整正文。**原檔 CRLF、native 注入 LF，不能說原 bytes 相同。**

Existing `SdkSessionFiles`／`checkpoint_files`／`SdkState` 保存 filtered synthetic state，正常 disconnect→stop 後，新 Python／runtime 恢復同 SID、同 skill directory。`reload` 無 error／warning；但當時 model request 沒有先前 body marker，而 `get_invoked` 仍留有相同 content hash。**Catalog、invocation record 與模型已取得正文，是三件不同的事。**

第一次 blocked attempt 是過嚴 newline 比對，以及 `infinite_sessions.enabled=False` 缺必要 workspace persistence，錯誤為 `SDK_STATE_INCOMPLETE`；不是 private-material blocker。只保留一次 fixture correction，沒有擴 permission。

## Resume 再 invoke 與明確 deny

兩個 fresh-resume flow 都讀同一份合規 synthetic seed 的 byte-identical 複本，不重跑 create。

| 觀察 | Load | Deny |
|---|---|---|
| 第一個模型請求 body marker | 不在 | 不在 |
| 正式 `on_pre_tool_use` | 1次，gate=true，回 None（不 override） | 1次，gate=false，明確 `permissionDecision=deny` |
| 下一個模型請求 | 新增 exact marker／version／normalized body | 收到明確拒絕的 tool result，沒有新增 body |
| 新 `skill.invoked` | 1 | 0 |
| `get_invoked` | 同內容hash，invoked turn更新 | 保留原紀錄，不假裝刪除歷史 |

Deny 有 tool start／failed complete，不能寫成「沒有工具事件」。正文由原生新增 user-role context 提供，不是 fixture 手塞，也不是把完整正文放進 tool result；hook 沒有 modifiedArgs／additionalContext。

這只驗 formal hook 的**明確拒絕**，不是完整 M3 authority、版本／撤銷、新增目錄、或所有 resume 情境的驗收。

## 未處理的 hook exception：真實 fail-open

固定 source 的 [`session.py` `_handle_hooks_invoke`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L2934-L3010) catch Exception，記 warning 並回 None；[client router](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L792-L797) 與 [generated response](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L4130-L4145) 保留省略 output 的語義。[Pre-tool 文件](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/hooks/pre-tool-use.md) 將 null／None 定義為預設執行，明確 deny 才拒絕。

一個新 fresh-resume probe 讓同一 skill 的 typed callback 直接 raise synthetic exception，沒有 catch 後改成 deny。實際觀察：warning=1、tool success=true、新 skill.invoked=1、下一個模型 request body marker 由0→1。這是直接 runtime 證據，不只從空 output 推測。

公開 constructor／resume／SessionHooks 沒有 callback timeout 設定；`session_idle_timeout_seconds` 是閒置清理，不是 hook timeout。因此 timeout 為 **NOT_RUN_NO_PUBLIC_CALLBACK_TIMEOUT_OPTION**，不以 turn deadline／無限 sleep／私有 RPC 代替，也不外推取消或斷線行為。

## 產品接續的限制

目前 FA 預設關閉此 native skill 能力；這是 SDK 接點限制，**不是聲稱現行 FA 已啟用的漏洞**。不能以「callback exception已拋出」或 warning 就宣稱讀取授權 fail-closed。

以下已合併的有界產品切片採既有伺服器端的批准／team／版本／hash 驗證及 SDK 正式 custom-tool loop，僅做受控 approved-skill catalog／按需來源讀取。Native `skill` 不因本 probe 就啟用，不依賴 hook 作唯一權威，也不另建一般 filesystem loader、transcript engine 或新批准控制面。

這個 source adapter 是 SDK-first 下已確認的 authority 缺口補接；它若不能沿有效的既有身份／scope seam 安全接線，必須明確阻塞，不偽造 Principal 或放開 IAM。舊技能文字可能作為歷史保留，不宣稱可抹除模型知識或完整 SK-09 語意已驗。

## 受控技能來源產品切片

初稿 `44420a93b115317d45a38cbac73ddacb3a723dbc` 基於已審 Prompt `a9a3ce1`；Hosted 相依 P1 修正後，最終 source 為 `d05851e899816d2e04333d05138d1612932f7996`。#12 已合併，merge commit `37b09dd9388f2082bc7e7f756ad6e6c2db749e33`，不是部署回條。

本節記錄#12交付時的接點與限制；新的跨回合catalog採用行為另見[修後接續](#目錄接續修後與合併)，不回寫第一切片的實際結果。

| 接點 | 實作與限制 |
|---|---|
| Server profile | `fa-instruction-skills-v1` 與 `skill_artifacts` 只由受信設定選定，沿原 profile／configuration digest、WorkRegistration、immutable task；default OFF，舊缺省 bytes 保持 |
| Catalog | 使用現有同 OID 的 server-bound `Actor`，核 Team、kind=skill、configured human 的有效批准及 immutable version；最多4個選定來源，4096 UTF-8 bytes，只回 metadata |
| Body read | `read_skill` 只收當輪 catalog 名稱，不能指定 path／URL／row／身份；使用原 ArtifactService，讀取前後核目前批准、version/hash/source、8192-byte 正文與完整 receipt |
| Model egress | `model_sources` 在 reserve／consume／provider 發送前重核當輪 catalog；不以 prompt、client manifest 或 fail-open hook 授權 |
| History | 保存原 `_Session`／`TurnArchive.skill_catalog` 與 material；舊目錄變動目前會拒絕 cold restore／新回合，不改寫舊回條 |
| Failure | Source／callback例外或 schema/path/profile spoof 回失敗，不回先前正文、不增加權限／operation／帳本效果 |

這是 **Team 層級**的有界來源接點，不是 Agent-private skill ACL。初始工具 description 僅 catalog；正文由通過權威驗證的 custom tool result 按需進模型。Native `builtin:skill`、ambient discovery、MCP／plugins／scripts及一般 filesystem仍關閉。

### 分批測試結果

原21個開發批次的索引留在凍結 handoff，包括環境缺 pytest、尚無模組／配置的 red、Actor 綁定修正及初期快取缺件。以下具有獨立 JUnit 的批次按 XML 中的**台北日期 2026-09-24**記錄，不把凌晨才匯入的時間當測試日期。

| 原件／批次 | 結果 | JUnit 秒 |
|---|---|---:|
| Direct API／source／legacy guards | 116 passed；含68個新 source/API cases | 77.832 |
| Exact body sizes／model flags | 另6 passed | 12.513 |
| 首次較廣回歸，快取缺失 | 291 passed／3 failed／51 setup errors | 102.351 |
| 使用已核 offline cache後 | 338 passed／5 failed；3個已知失敗明確 deselected | 640.745 |
| 不變 a9 基準比對 | 3 failed：兩個缺私有config、一個舊DTO assertion | 12.580 |
| 不變 a9 readiness比對 | 5 failed：現行錯誤與舊fixture期待不符 | 6.033 |
| Final custom-source native | 3 passed，合計9個offline requests／4個normal stops | 23.559 |
| 原legacy／v1／CTX-03 native | 另3 passed，不與新矩陣合算 | 34.196 |

Setup errors 在帳冊獨立列 `pytest_errors`，不併成 semantic failed 或改寫成0。原 fixture 缺件沒有從私人 config 或別人的 live worktree補入。未知完整WIP/test基準留 null；原件的final source識別不代表每個開發步驟都已是該提交。

特別是9-request native與最後15-case冷入口：XML開始時間均早於對應程式提交。已核程式檔hash與後來的版本相同，但完整被測Git tree不明，兩筆 `baseline` 均為null，不能標成在 `44420a9`／`d05851e` 提交上執行。這是在資料PR合併前依Standards P2更正的版本歸屬，測試結果、原件hash及已合併83筆歷史不變。

Native 三案分別是批准來源正向5次請求、source exception2次、schema spoof2次。後兩案 tool failed、body未披露；未批准 operation row不變，native skill loader未開。這是 fixed SDK／runtime 的offline接線證據，不是真模型選用／遵從品質；`prefix_tokens`／`catalog_tokens`為null，byte界線不是2%token預算。

### 新 P1 與精確修正

初次 Opus 5.5 review：**Standards 1 P1／0 P2，Spec 0 P1／0 P2**。`m3_skills` 的 module-level `Actor` import連到API專用 `auth`／FastAPI，讓只有 `requirements-hosted.txt` 的Hosted入口無法匯入；full development venv的成功不能代替薄映像依賴檢查。

最小修正把型別註記的Actor放到 `TYPE_CHECKING`，只在API端 `catalog()` 的原 `isinstance` 前延遲匯入真的Actor。批准／scope／hash驗證全保留，沒有新增FastAPI到Hosted、沒有ImportError fallback，也沒有偽造身份。

| P1修正批次 | 保留的結果 |
|---|---|
| 44420a9 的冷Hosted入口 | 1 failed，5.976秒，依賴closure明確拒絕FastAPI |
| 移除eager import後 | 85 passed／1不同fixture failure，63.547秒 |
| 同一cold stub在a9重現 | 1 failed，5.070秒；缺少 `task.job_id`，與新的import P1不同 |
| Selector拼字錯誤 | runner exit4／0 tests；不是0失敗的PASS |
| 補stub必要欄位後 | 15 passed，18.329秒；closure白名單、禁網路、acquire412與failure journal斷言未放寬 |

所有這些 JUnit 日期仍是9/24；**修後review、精確prospective main冷入口及合併**發生在台北9/25，分開記錄。新head沒有重跑舊9-request native或640秒suite，不倒改它們的版本範圍。

Final `d05851e` 由同兩位 `claude-opus-5.5` 複核，各 **0 P1／0 P2**。可列印字元的非阻擋提醒經 M3 正常upload與policy_context都已驗同規則而撤回；沒有為不成立的路徑增加catalog正文預載。

協調者另外在預期main整合tree `0ceb521ba10c3217be18827d68c5e4ed30909ca5` 跑既有cold dependency-closure測試，1 passed／7.209秒，不啟SDK或模型。合併後tree與parents精確讀回一致，沒有admin bypass、rebase或部署。

### PR12交付時的差額

PR12已有批准來源、明確版本、按需custom read與回合內重核，但當時 **SK-09／SK-12 的catalog變更接續尚未完成**。原整份目錄相等要求會因新批准／撤銷／未讀條目的更新，擋住已有對話的新回合；這是保守第一切片的限制，不是動態目錄需求已成立。

因此後續切片區分「合法的下一個新回合」與「同一未完成工作的恢復」：前者取得當前有權版本並保留舊snapshot，後者和回合中途的變動仍須fail-closed。不可直接刪掉所有相等檢查、覆寫舊history／批准，或以SDK hook當授權；更新須沿真API／SDK／save／restore consumer驗證。

Relevance、人類／真模型語意、model-token目錄預算、三段降級、skillify、附帶資源、Agent-private ACL、歷史抹除或發送後原子撤銷，仍不由這份本機切片接受。

## 目錄接續切片初審

#14首次固定head `bbc3be893f2d462430808b5b5ab95843304e55ef`，base `37b09dd`，**當時尚未合併**；當時main `ee80e2e` 只另外納入已審資料更新。該切片沿SDK正式resume工具配置更新current catalog，沒有改固定prefix／DTO，也沒有啟native loader。

原owner的測試分開保存：初次RED 1 passed／1 failed；最終同測項疊到固定base再1 failed；第一批擴充API 79 passed／7 failed，修正fixture的route／public-versus-upstream error斷言及實際restore ACL分類後窄8 passed。最後166 passed與native4 passed是兩個批次；native最後19個offline requests＝新的五回合10次＋原三案9次，9個graceful stop的原exit code均為1，force-stop為0。較早單跑新案例的10次不加進最後19次。

五回合正向矩陣涵蓋成功回合後的v1→v2、撤銷、新可用、未讀條目更版；353個Python working-byte hashes在測後／commit後核對一致，但不因此把pre-commit XML倒稱在未來commit上執行。新的未完成checkpoint仍精確比catalog，steady-work重啟本身仍 `WORK_POLICY_START_FENCED`，沒有宣稱一般unfinished resume已啟用。

**原Opus5.5初審分軸結果：Standards 0 P1／0 P2；Spec 0 P1／1 P2。** 如果當輪已取得C2後又發生合法更版／撤銷成C3，原reserve／consume guard會拒絕該輪；但只接受「前一輪succeeded、failure=None」的新規則也會拒絕之後每一輪，即使已graceful stop並保存合法失敗archive。這仍可能永久封住對話，與SK-09下一適用回合採當前版的要求衝突。

當時修正交原owner：只有terminal、result_forwarded、graceful stop、合法archive及原identity／profile／assembly完整的新真人回合才能接續；未決、未停止、未forwarded或同一未完成job仍不得繞過。不以縮小說明取代此行為修正。Standards另列非阻擋提醒：新的直接SKILL restore錯誤會在journal退成 `UNCLASSIFIED`，應保留安全的typed診斷，不能帶入raw body。

此處保存的是**有P2的首次候選與結果**，不當最終版或merge／部署證明。原handoff SHA256 `c4bd837a9e8316c7d61a3fda9b3bec812cc3bf875eb99fcdfe84e7fceb4701ec`、native transition proof `b38296df5d36c2705af390a831f679248892c8001f9c30547fa01e7cb20bbcf6`；修後結果如下，首審不改。

## 目錄接續修後與合併

固定修後head `e17a20e9c8739bd15e7edf01c94daba5c3bd1b09` 是`bbc3be8`的直接子提交。`validate_turn_skills`改沿既有admission的`succeeded／failed／cancelled` terminal集合，仍要求已forwarded、graceful stop、合法原archive/material及同identity／profile／assembly／ACL。只讓新的真人回合採current catalog，不重啟已失敗工作、不改舊結果。

Hosted的相連診斷沿既有`SKILL_ERRORS`白名單保留HTTP分類、phase及錯誤碼，避免退成`UNCLASSIFIED`；不保存raw body／credentials／私人exception文字，也不推定stop。其餘within-turn的read／reserve／consume／最後provider重核、未完成checkpoint guard、DTO／prefix／writer與SDK options不變。

| 修後批次（台北9/25） | 結果 | JUnit秒 |
|---|---|---:|
| 精確terminal／diagnostic RED | 2 failed | 6.351 |
| 最小修正 | 2 passed | 5.877 |
| 擴充條件首次 | 11 passed／1 failed；新test誤期待首次lost-PUT的內部碼 | 14.255 |
| 對齊client外層與retained-intent差異 | 12 passed；原PUT僅一次，產品upload不變 | 15.321 |
| 新native三回合單案 | 1 passed／5 offline requests | 20.211 |
| 最後targeted | 92 passed／0 failed／0 errors／0 skipped | 86.754 |
| 最後native | 5 passed／24 offline requests／12 graceful stops | 56.090 |

真正API/controller路徑是C1成功→C2 issue→原M3 upload/approve切C3→reserve fence→正常保存stop與failed／cancelled→先證terminal但未forwarded仍拒→controller轉交→新真人回合以原conversation／SDK取得C3。未以直接改持久rows偽造terminal；cancelled正例先取得合法已發布stopped archive，再走原cancellation API。

新native案例實際是三個process的`succeeded→failed→succeeded`，requests為2／1／2；C3於C2 provider request期間生效，read及下一reserve被擋，正常stop後保存失敗archive，再讓第三輪採C3。原user/tool events、成功／失敗archive/result及未批准operation不變，failed tool不replay。最後24次包含這5次和原四案19次；早期單跑5次是另一批，不合併計數。12次runtime stop均graceful=true，Windows原exit_code=1，force-stop=0。

七批都是 **`bbc3be8`加未提交WIP**；`tested_commit`及帳冊baseline均為null。353個Python working-byte hashes測後與commit後相同只證檔案一致，不倒稱在尚未存在的`e17a20e`跑過。修後handoff SHA256 `b5abc698c6ede52fa29527439f5fcc8504066301b2817f2e58b1a712484d80eb`、native proof `4298ca6d85d7e20de9519b146f6d2973c975cc6ff4c4e3fc7fcaadd2d9e12f33`。

同兩位Opus5.5分軸複核`e17a20e`，**Standards 0 P1／0 P2、Spec 0 P1／0 P2**；原P2與相連診斷提醒均解除。#15路由測試先合併為`8368fbc`，#14再合併為`578d8805db4da429f7c604b204bfd34b3d4f53c2`，ordinary merge／match-head、沒有admin bypass。產品merge的parents為`8368fbc`＋`e17a20e`，tree `4df6ca05cb3f1348d6b1d3a916fef65f9a1d930f`與合併前測試snapshot精確一致。

該snapshot另跑13個focused cases，涵蓋路由、terminal接續、safe diagnostics及冷Hosted入口；其中只有一個native routing案，3個offline requests／3次正常SDK stop返回／0 force-stop。這是獨立整合批次，不回填原92／5或宣稱全套回歸。原ZIP、XML與safe結果保留，解壓的task-owned scratch已清理。

### 剩餘界線

PR14交付時，目錄若在issue後、SDK restore／取目錄前改變，可能沒有TurnArchive而留下`WORKER_START_UNKNOWN`。後續[PR18的獨立no-SDK契約](sdk-prestart-evaluation.md)已合併，但default-OFF且只涵蓋已ack acquire、第一代、可證未嘗試且已封死entry的skill拒絕；其他unknown仍須核原工作，不能推定停止或自動reset。沒有把該新版本回填成PR14的能力。

真SDK cancellation完整中斷／恢復仍NOT_RUN，只有API合法終局正例。Team級ACL、四個configured名稱、4096B目錄／8192B正文、token未知的界線不變；非Agent-private artifact權限、一般connector帳號更新、Memory/routine/profile完整更新、真模型語意、Hosted／Blob／Q14或完整SK驗收。

帳冊由108筆追加23筆到131：原八份與修後七份JUnit、兩版PR14及PR15的六份分軸review、一份development索引、一個整合snapshot。SK-09與PA-05的目錄部分列partial；**PA-06的connector條目未因skill工具更新而升級**，完整target／scenario接受仍各0。

## 用途metadata隨批准版本

2026-09-25，#26已合併為`e2460eaca75d21bf3640a5e35a811a5fd8585b26`，default-OFF。
用途不是從mutable title或body首段猜出，而是作者在新immutable skill version提供的字面metadata。
SK-01仍partial；沒有宣稱Agent-private ACL、SK-02模型相關性、token比例或完整skill能力已驗。

| 接點 | 已實作界線 |
|---|---|
| 版本用途 | `fa-skill-purpose-v1`，printable／非空，≤512 UTF-8 bytes；只供skill，保留Unicode與前後空白 |
| Upload | 單一canonical paddedbase64url JSON header，≤2048 ASCII bytes含escaping；duplicate header/key、unknown schema、無效UTF-8、控制字元與超限拒絕 |
| 完整性 | `metadata_sha256`綁Team/artifact及完整immutable version／Blob receipt／body hash／來源／用途，只排approval與hash自身 |
| Human approval | 原version/body SHA之外再pin metadata SHA；只改用途也需新版與新批准，olddecision／ack不能換字，unknown沿原reconcile不重送 |
| Runtime | Server-only `fa-instruction-skill-purpose-v1`選v2catalog/read；初始只metadata，正文經原custom read_skill；current Actor/Team/approval與read前後/restore/egress gate不變 |
| Legacy | v1 description仍null、舊None不多bytes；v2的legacy版本用途明確null，不推測內容 |
| 容量 | 仍4entries／4096B catalog／8192B body，tokens unknown，非2%預算或降級策略 |

新literal prefix為3002B、SHA256 `65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879`。
用途及目錄放在工具description，不進穩定prefix；描述是選用資料，不是操作批准或已載入正文的證據。

### 原14批與exact source

Base `2f33baa`，source/tests先提交`36e9d475392e24bc7d0ec1d4ec13517ad88ef88f`，
14:06:33在乾淨該source跑 **104 API/compat PASS／53.303秒＋1 native PASS／13.639秒**。
交審`d4dce918`只多README，376個Python hashes／Git blobs不變。

Native共5個offline requests／2個runtime：原signed M3 create/upload/approve→SDK初始用途metadata，
沒有current body canary；actual read_skill把exact version/body hash/metadata hash/CRLF body送進同call_id的
下一個request。新批准用途後同CID/SID cold resume採新目錄，舊archive／registration／history不改，
未批准operation仍拒。兩stop為Windows1／graceful／force0；無compaction或native-loader事件。

原14批保留purpose/profile初RED、兩次把checkpoint Blob回讀誤算skill預載的observer FAIL、
header9P/3F（完整escaping容量與duplicate schema兩個實作缺口），及consumer10P/1F
（test把無deadline指令誤當一天到期，改驗真正expired signed token，產品未加期限）。
缺完整native failed counts/stop的值仍null，成功批次不相加為品質樣本。

### CLI原文與thin-client consumer修補

首審`d4dce918`是Standards0P1/1P2、Spec0P1/0P2。CLI原先在確認前把中文用途印成Unicode escapes；
只改purpose preview為`ensure_ascii=False`，在input()前看原中文與空白，缺／錯metadata digest仍不能confirm。
Body/hash/version/decision bytes與送出後machine receipt不改。

Spec另提的consumer疑點後來真的重現：saved purpose API投影200且有合法`prompt_assembly`，
bridge progress卻502 `CONVERSATION_INVALID`。這個新實測不倒改Spec初審counts。
只在strict `ConversationTurn`加既有SDK-independent `AssemblyRecord | None`並省略None，
不寬extra、不去validator，不讓metadata成為follow-up／reset／ACK權限。

| 新批次 | 原結果 |
|---|---|
| CLI RED→GREEN | 4 passed／2 failed → 6 passed |
| Bridge RED→GREEN | 1 failed → 1 passed |
| Bridge controls | 12 passed；四schema每個12種malformed子斷言，不算48個額外pytest案例 |
| Exact `814cb892` | 22 passed／0 failed／0 skipped，24.551秒；15:02:17 commit後、15:02:43 pytest開始 |

22 cases分CLI6、bridge12、舊CLI2、legacy M3/catalog/archive bytes1、no-SDK explicit reset1。
Unknown／false hash/length/caps／private/extra/type全部拒，先前合法projection與reply保持。
真public GET只讀，不改Table／journal；合法metadata也不能解開queued工作。
冷子process禁止copilot import仍可decode；legacy1434B與None省略不變。
這六批**沒有native/model/browser/DOM或原104重跑**，SDK I/O double不當native stop。

修後`e96eb02`只比tested `814cb89211247215a03425275443bc24c3c391b0`多README，
377個Python hashes／Git blobs一致。同兩Opus5.5軸各0P1/P2，原CLI P2解除。

### 純Git整合、主線與資格

當GitHub PR base/merge preview停在b04而live main已0a223時，沒有借stale CLEAN合併。
Owner在自己的乾淨branch普通no-ff merge已審main，得到`30a41e4205fa074c52fb53ba6df6b126dc2e6778`，
parents=e96＋0a223、tree `bd7d2bd0d9afe48cec513f724c125c40bf7c6260`；無手工resolution或新增測試。
這個tree等於parent事先跑的34-case focused union，兩軸再核純整合各0P1/P2後才普通merge。

實際merge `e2460ea` parents=0a223＋30a，tree同bd7；34結果屬virtual snapshot，
不假稱在後來的Git commit重跑。原14、修6、34整合及各review均分開。

第6個prefix使current PA-02先回partial；TC-01也沒有因局部bridge22/union34直接轉綠。
合併後另在`ed6f64e`同一baseline重驗完整六prefix、實際native projection的strict DTO、真bridge與
public-view範圍，見[新資格矩陣](prompt-assembly-evaluation.md#第六個prefix與公開投影重驗)。
這個新current接受已隨#27在固定head正式雙軸審查後合併，仍不代表SK-02 M／fullSK通過。

## TOOL-06 整合：保留 approved-skill 拒絕契約

五類runtime工作線的初版schema回饋把所有instruction validation都視為可公開參數錯誤，
Opus5.5指出這會改變`read_skill`的動態catalog拒絕及既有native斷言。
該線以真`SkillName`／catalog enum配合SDK callback doubles重現四個RED，再修成：
**`read_skill`全部schema拒絕仍保留原`Authority refused…`**，handler不得被呼叫、正文不得載入。
Permission／invoke兩條路徑都驗證撤銷／目錄外名稱與額外path欄位；其他instruction的安全schema診斷仍可用。

這是維持原契約，不放寬原native測試或把失敗改成成功。原
`test_fixed_sdk_new_turns_replace_only_current_catalog_and_preserve_historical_bodies`及
`test_fixed_sdk_custom_tool_failure_never_discloses_body_or_enables_native_loading`
斷言均未修改；本機缺固定1.0.83 runtime，本次**native NOT_RUN**，不回填#12／#14／#26證據。
新增15個callback與33個callback／signed API合批均通過，部分重疊不相加；
精確基準、WIP hashes與原件見[TOOL-06](agent-runtime-evaluation-baseline.md#tool-06-第一個實作切片安全的-schema-回饋)。

後續[公開工具配對切片](tool-progress-correlation-evaluation.md)另取得固定runtime，真正重跑上述三個
相關native cases並通過；原「本次native NOT_RUN」是前一批歷史，不回填。
`read_skill`現在可在public progress顯示工具名稱／配對狀態，但仍不含skill正文或raw call ID。

## Verified against

- Native fixture 沒有改 repository／產品／SDK／harness，也沒有 commit/PR；三個 batch 的 source／safe results／seed hashes 固定保存。原私人 system／reasoning／wire／runtime log 刻意不落盤。
- Initial handoff／attempt-02 result：`5f5296409627bf864e39c2ebe347aaeb2ccf8f771f3f8151b34ef0b3f5a1e893`／`b82b2f191df8e066ee6e9932822cec36dfc09c27ac159b6aeef542b7170d705e`；協調者核8個pins。
- Resume followup handoff／result：`28ecbfc052db8db9ac932ed40a4cf0d1584fa6cb289d3e604c80f4b023936b2c`／`10031eddb0bab39db0925ee8d3318e1a711e51c3d364327e11f686fc358fb823`；核7個pins。
- Exception handoff／proof：`bdfaf61b90ba9ab1407935f593377f4976360df8e3be35c0ef3c838b52ca020c`／`a9d3f45575c4f80f647c95df3244a58d9ce9e4b81c935316b7e1cef15fc01f16`；核5個pins。
- 三批均正常 disconnect→stop，Windows runtime exit=1 與 Python driver exit=0 分開記，未增加 force-stop。實際時序／argv／request counts 在各 fixed handoff，沒有以本次收件另跑 SDK。
- 既有 approval／source controls、[SDK-first ADR](adr/0004-agent-behavior-control.md) 仍適用；不授權雲端、付費推論或權限變更，不把這些 observations當完整 SK／H／M 驗收。
- #12原產品基準：合併 `37b09dd9388f2082bc7e7f756ad6e6c2db749e33`；已讀 `m3_skills.ApprovedSkillSource.catalog/read`、`behavior_skill_prompt`、`AssistantAuthority.issue/current_skills/model_sources`、`restore_turn`、Hosted工具註冊與對應README。
- 原產品handoff／commands hashes：`e465e12c58625e72218a54ac19ec05a45396ffe16e00349fcd389e20b2e82a80`／`95b7b5530cd5591cb356a6f5edf113239aef13ce65750ff3162ac422346e3f14`；P1修正handoff為`09cb4da2eaf5ba0b19ecdf8db0d83a5cb63b9f9683ad3e53b385538846223bf1`。原JUnit和safe native hashes在帳冊的各個run引用，不發布原始private資料。
- 原PR12資料匯入只解析保留的XML timestamp／counts與hash，按同一UTC+08日期記錄；沒有重跑PR12的SDK／模型批次。整合／review／landing的回條與原測試分開，不把merge當部署、readiness或完整目標通過。
- #14初稿與修後均依固定handoff、source manifests、safe native／JUnit原件及兩軸pinned-blob檢查；協調線核改動檔CRLF hashes對到各head、原件hash／計數／timestamp，沒有重跑原166／4或92／5批次。候選、原P2及修後結果分別追加至131-run帳冊，原108筆／規格／bindings保持。
- 新合併基準`578d880`的exact tree已與13-case snapshot比對；snapshot ZIP SHA256 `f3407fbbd516d0791aece7142709e210628887fffd4f7e128852a5e05b64801d`，JUnit `84a86703a0d87e5614c1e08fa11212b992c61568bc58755a75b723208d33205a`。不把Git tree OID、原ZIP hash、working-byte hash或後來的merge commit互換。
- Purpose兩軸以fixed blobs核M3 header／immutable metadata／approval／CLI、v1/v2 catalog/read、Hosted／gateway／bridge全部consumer。Coordinator核原376與修377個工作檔hash／Git blobs，以及20批XML／source／時間；原件handoff `6ff6bcb336f058be54bdc975df67ec5c1b15fa04826230abb05346f584834154`、修後 `6742570c09c230b115635ea7d7ead456cb5c575ba4b9dc50dd0525a421def8b9`。
- 原104/native XML：`6ec5424c1cbd97c18f5e01e7a7d46287340d38b858139f71eab23c6fb915813b`／`c22034ab8fdfecd6d162138f8f944dea75386a60c96bf423ddbf46455c7f3367`；修後22 XML `d213bc4b49b35fe6682e257d54b883e94f9f4f04e2f8c2c52b54314e56a49a1b`。沒有把104/native原scope回填成當時已核CLI literal preview或bridge。
- Union ZIP／34-case XML：`4631e4cb96b122205a7799c7b42607be62dc54c4c715ad02e1bf1ff81b3252b3`／`c47ffc5588b308798d25555719a0995fb3bf51519cf3fbb0ebeb5608d877f96d`；owner整合回條 `5982666e7fb04112b2f8187b760264bb5c477d25a1182eae7860763e0e4c07e6`。原capacity blocker、default-OFF／未部署、無paid/IAM／harness更動的界線不變。

## SK-06：已載入技能不得擴權的本機 C 候選

2026-09-26，固定產品基準為 `e5c2b2209558e0d315b2b6eabbd57e71f66937ac`。
本組只新增兩個 scoped test files，產品、原 shared helpers、SDK／profile／authority 不改。
[SK-06](grok-aligned-target-state.md) 要求 skill 不覆蓋上層安全、當前資料權、操作批准或授權內的使用者要求；
下列保留初版程式性資格候選；**後續 P2 補證已限定本 PR 只交 subset，不申請完整 SK-06 C**，
缺失 producer 與新增結果見本節末。全域接受狀態仍由協調者另維護。

不是以「有技能名稱但沒載入」證防禦：每案均先由 signed M3 create／upload 與配置真人的
exact version/body/metadata decision 建立合法來源，再經 current catalog／`read_skill` 取得完整本文。
合成本文帶仿冒 system／human／approved marker、未配置的 install／shell／private-read 名稱及
額外 actor／Team／path／version 要求；只有 fake canary，沒有真私人路徑、收件人、憑證或可執行命令。
本文 SHA256 為 `c006fed0da0126cb3b5e20e4c4a2ff14a82eed8a31b88cb16f431026cf6ffd8e`。

| 層級 | 本次可支持的範圍 |
|---|---|
| 兩個真 loader | `fa-instruction-skills-v1` 與 `fa-instruction-skill-purpose-v1`，各自 exact typed body／version／hash；purpose profile 另核 approved metadata hash |
| 操作權限 | prepared/pending 無 grant、signed agent 冒真人批准被403拒、真人 exact approve 後 signed revoke；原 `execute_operation` 拒絕，operation／grant／attempt／dispatch／receipt／原budget未被換成批准或成功 |
| 排除錯誤 gate 的正向 | 同一個既有 server-configurable `ContextArtifactAdapter`、固定同Team metrics GET，原 signed prepare＋approve 未撤銷時，synthetic M3 transport 恰一次；再取原receipt不重送。沒有新adapter／手造grant |
| Scope／身份 | 真正載入後，extra Team／actor／path／version／approved、目錄外名稱、另一verified agent及跨Team路徑各拒；原合法read仍可用 |
| 正式 callback | 兩profile的permission／invoke拒絕fake install／shell／private-read、extra scope、錯SID；source handler不被非法call觸發，合法callback讀取可繼續。這10案只有SDK I/O doubles，非runtime／stop證據 |

現有operation只有prepare／approve／revoke，沒有獨立human reject-decision producer。
「agent冒human批准被拒」不寫成「已保存human reject」；也不造rejected row。
Metrics GET正向只證這個已存在的synthetic server composition可達，**不是部署或
`WorkStateEffectAdapter`正向資格，不解除既有generic-operation blocked項**。
拒絕路徑可能保存原command plan／consume責任紀錄，不冒稱整個control store零寫入；
其原operation、one-send效果紀錄與費用另作精確不變比對。

### 三批精確來源，不相加或回填

| 批次／開始時間（UTC+08） | 實際來源 | 原結果 |
|---|---|---|
| `sk06-operation-baseline`／09:04:49 | `b7abb6cd430032f5bf66a70de03ebb8756cd53f0` | 3 passed，8.938秒；兩loader pending＋同adapter真人grant正向 |
| `sk06-api-callback-exact`／09:10:25 | `ec8b530249b8407cb64482c3e21ef33b935640a2` | 33 passed，48.903秒；6個操作拒絕、1個操作正向、12個schema、4個identity、10個callback |
| `sk06-native-exact`／09:12:27 | `a9dc87e468a209b9debe7f18df2e4ba0cde71702` | 1 passed，8.684秒；唯一fixedSDK情境 |

每批皆先提交tests再於clean source執行。a9只比ec8多native安全觀察的finally保存，
API／callback檔案bytes與ec8相同；**33不是在a9重跑**。首3是後33的子集，不加成37個不同案例。
未發現產品bug，不強造產品RED；未修改判準後重跑native。

唯一native使用purpose profile、SDK1.0.13/runtime1.0.83，業務模式仍`offline_contract`：
1個owned runtime、4次scripted offline provider request，初始無本文，actual `read_skill` 後
exact批准本文進下一個payload，隨後`execute_operation` tool failed，再合法`read_context`成功，
安全答案保存並進原交付路徑。原user event保持原字串，operation效果出口0次，
工具拒絕前後的accounting不變；模型正常fixture usage不是免費或真費用證據。
真Windows stop為exit1／graceful=true／force0；permission callback實際3次，
**其結構性許可不代替server operation grant**。Native loader／discovery／MCP／plugins／其他agents仍OFF。

### Verified against

- 已核 `m3_skills.ApprovedSkillSource.catalog/read`、Hosted reader／tool registry、
  `m2_copilot.permission/invoke`、原operation prepare／approve／revoke／execute、
  `ContextArtifactAdapter`及current Actor／Team／typed API拒絕接點；產品無diff。
- 新測項：`tests/shared_context/test_skill_guidance_authority.py`、
  `tests/shared_context/test_skill_guidance_native.py`。Native provider是scripted，
  **不證自然LLM不受prompt injection、模型遵從品質、任意private filesystem或其他未配置能力可用**。
- 三份原JUnit SHA256依序為
  `50232c15c3dabfcfffd928935d941ef7d20b24d16d598ea5651614970da2a4ad`、
  `0cbdd62c26fe05ce579ac5fabce58d0a24e38907cda9b6b7ea29ad6cdbab647b`、
  `e11df7109b6dd063f02bd524705b9969d0ee3f96c7e9538951ba6fc1c5b5a3cb`；
  argv／environment／working-source hashes及safe JSON另存session `sk06-guidance-qualification`。
- 沒有Azure／paid／IAM／deployment／build、SDK／harness更新、新agents／reviewers，
  不改TC-05 recovery或copy scope，不重跑271或既有大型矩陣；原結果不回填。

### SK-06 P2：已載入的歷史本文與目前來源／讀權分開

初版 fixed `854ebb0` 兩軸回覆為 Standards **0 P1／0 P2**、Spec **0 P1／1 P2**；
缺口是已真正載入本文之後，來源換版／撤銷／目前讀權改變的證據不足。
本次依裁定只補可由原 producer 到達的四案，**維持 subset**；
原 b7／ec8／a9 三批與當時的來源歸屬不回填。

| 要求 | 當前合法接點與處理 |
|---|---|
| 兩 loader 的新版本取代 | 真 signed M3 upload＋配置真人批准 v2，保持同本文 SHA；GET effective 核 v2。舊 body/history、v1 catalog 不能因此繼續被當成本輪 current source |
| 兩 loader 的 read loss | 真 signed `/operator/v1/standing-work-policy/revoke`，撤銷原 work/read scope；不是單項 skill disable，也不改 M3 effective pointer 或已存內容批准 |
| 兩 loader 的 skill-only revocation | **BLOCKED／NOT_RUN，兩項都不是 PASS**。現有公開 M3 producer 沒有單獨撤銷已生效 skill 的接點；本次不新增 API／profile或直接改row |

`m3_artifacts.ArtifactService.decide` 對已決定版本回 `M3_ALREADY_DECIDED`；
只有 approve 更新 `effective_version_id`，reject 新提案刻意保留先前 effective version。
`m3_api.add_artifact_routes` 的 effective 是 GET-only。既有「v2 reject 保 v1 effective」
測試定義僅唯讀引用，沒有重跑它當撤銷防禦，也沒有捏造 human reject 或清空 pointer。

四個 API 案先取得真正 signed approved body，將該結果放入明確標示的 **SDK-state fixture**，
沿原公開 save／restore 接點保存並讀回原history；不直接塞 `TurnArchive` 或控制 rows。
Checkpoint／stop 是 API 層替身，**不是這四案執行了真 SDK 或新增 native cold restore 證據**。
原 archive 及當輪捕獲的 v1 catalog 均保留。

為排除「沒有 operation grant」造成的假 gate，同一當輪先另經原 signed prepare／approve
取得獨立有效的 operation grant。兩筆事先宣告的 model requests 在合法來源下 reserve；
第一筆 consume 後、provider credential 接點交錯上述真 mutation，
原 `before_send` 的 current authority／source 檢查在外送前403拒絕。
換版案公開診斷為 `SKILL_SOURCE_CHANGED`；read-loss案只保留
`upstream_error_phase=model_authority`，公開code缺省為null，不替遮罩補造code。
之後 catalog／read／新reserve／另一已reserve的consume／原consume重取都拒絕，
沒有新 provider dispatch；原 consumed intent／pending cost、另一未consume intent、
前回合 archive及獨立 operation/grant／receipt／budget不變。
這不新增「skill改版就撤銷所有獨立操作授權」的政策；全面read撤銷也不冒稱單項skill撤銷。

### P2 後的同源兩批

先提交 source/tests **`c7fb8b7da09c654a809187ae82897b9224ed387b`**
（tree `44cf5cd554cead7d2bc3c31179c677b46de15248`），兩批均在同一 clean commit：

| 批次／開始時間（UTC+08） | 結果 | JUnit SHA256 |
|---|---|---|
| `sk06-review-api-exact`／09:42:41 | 原33＋新4＝**37 passed**，47.638秒 | `d68deacb59e8da1d6abde927e6bc92b340fd8cf18757a6aaa070c9077d874907` |
| `sk06-review-native-exact`／09:43:51 | 原唯一 purpose SDK情境再跑一次，**1 passed**，8.596秒 | `38ba12f748bbeebd4b1a494a2b416c58000af8c56017e01457c4dbe96279f9e3` |

兩批均0 failed／errors／pytest skipped；兩個缺producer要求另外列BLOCKED／NOT_RUN，
不混成runner的skip或額外測項。Native檔未改，仍1個runtime／4個offline requests、
permission callback3、operation effect出口0、exit1／graceful／force0，
新結果另存，不把原a9結果改成c7。沒有新native情境、271／TC-05重跑或產品修改。
完整argv、source/environment hashes、四份authority mutation safe JSON及原件
保存在session `sk06-guidance-review`；不包含raw prompt／skill body／private reasoning／credentials。

## SK-06：正式停用 producer 合併後的共同來源 C 候選

2026-09-26，#48 合併後另固定 **`6fae3653b404d6740d986e366a1de9659460bf2f`**
（tree `61e076b2ffeea232276cfc6820ae8ec5cc2ae83a`）重新執行本節範圍。
這是新的共同來源資格，不是把 #46／c7 的兩個 BLOCKED 回填成 PASS，
也不借停用 producer 原 f24 的156／1結果。**只提出 current 完整 SK-06 C 候選，
是否接受由原兩軸裁定；本文件不宣告 accepted，也不改全域 ledger／Target。**

SK-06 的程式性條款仍是：「Skill 提供達成目標的程序，不覆蓋上層安全、當前資料權、
操作批准或授權內的使用者要求」；「惡意或過時 skill 要求外傳／安裝／升權時仍被程式擋住。」
本次沒有修改條款、產品、測試或 shared helpers。測前凍結四個既有 selectors，再以實際
`--collect-only` 清單固定 **41個唯一 node：39個 API／callback＋2個真 SDK情境**；
collection不算一次測試執行。

| 既有測項群組 | 唯一case數／證據層 |
|---|---|
| `test_skill_guidance_authority.py` | 37；原signed內容載入、operation grant正反、schema／identity、來源換版／standing-read撤銷；其中10個正式callback控制是SDK I/O doubles |
| `test_loaded_skill_deactivation_fences_sources_not_independent_operation_grants` | 2；兩loader先真讀已批准本文，再signed human skill-only deactivation，原reserve／consume／最後provider gate及後續read拒絕 |
| `test_skill_guidance_native.py` | 1；purpose profile真load→本文入next request→operation拒絕→合法read→安全答案保存 |
| `test_fixed_sdk_deactivation_keeps_history_but_requires_a_new_approved_version` | 1；三turn沿真停用→空catalog拒read→新v2批准，同CID／SID且舊本文／archive保持 |

現在skill-only停用沿已合併的 `POST /teams/{team}/artifacts/{artifact}/deactivations`，
不是全面standing-policy撤權、reject新提案或fixture清pointer。原版本／內容approval不改；
current source變更阻止新增模型外送，但獨立operationgrant、已consume的intent／pending cost、
另一未consumeintent均保留，不退款、不重送。兩個新增到本資格範圍的API cases沒有獨立JSON writer：
其證據是**固定測試原文中的signed public producer與狀態斷言＋本次每個node的JUnit PASS**，
不是另有未保存的逐request telemetry或推定counter。

### 新共同來源的實際結果

| 批次／開始時間（UTC+08） | Clean受測source | 原結果 |
|---|---|---|
| API／callback，12:12:40 | `6fae365` | **39 passed／0 failed／0 errors／0 skipped，55.395秒** |
| 既有SDK兩情境，12:13:50 | `6fae365` | **2 passed／0 failed／0 errors／0 skipped，19.749秒** |

兩個runner合計41個不同case，沒有額外未列測項，也不是同一次41-case runner。
SDK1.0.13／runtime1.0.83、Windows x64／Python3.12.10、businessmode `offline_contract`。
新safe原件實際記錄guidance 4次與deactivation 6次scripted provider requests，共10次；
1＋3個真owned runtime stop，共4個不同PID，四次均 **exit1／graceful=true／force0**。
這些值從本批proof陣列讀回，不把舊結果或測前預期填成新觀察。
Guidance permission callback實際3次；deactivation未另記該counter，保持null。

合法same-adapter synthetic metrics GET正向仍只在另行signed operation grant後到transport一次；
pending／agent冒真人批准／grant撤銷的反例仍可與它對照，不是停用所有能力求全綠。
沒有human operation reject producer，所以不造rejected row；已登錄operation仍綁server固定target。
原Standards「有grant時帶新destination參數」非阻擋建議沒有新增測試，**不宣稱已處理**。
SDK native loader／ambient discovery／plugins／MCP／新agent沒有啟用；scripted provider只證
程式邊界，不證自然LLM prompt-injection抗性、M品質、任意private filesystem或部署效果。
`ContextArtifactAdapter`的本機組合不接受原`WorkStateEffectAdapter`或其他generic-operation缺件。

### Verified against

- 精確source／tree如上；自有37／native測試blobs同c7，新增停用測試與runtime程式同f24。
  **整個src tree不等於f24**：其中Shared Context README有測後文件差額，本次仍以6fa為實測來源。
- 已讀 `m3_models.SkillDeactivationInput/Record`／`ArtifactDocument.valid_history`、
  `ArtifactService.deactivate_skill`、`m3_api`管理route與本次三個test files，
  並沿既有current catalog／read／model-source gate，不改operation權限。
- 測前scope SHA256 `e886562b7a6bf854490bab5bc8e4a6cbfb601873ef7a0db185d4daffcfec52ce`；
  actual collect清單 `f28bc2a0c7f07378497dc69b87ee1c71e3b0727f5248dd296fb8ec7d72abf84c`。
  API／SDK JUnit依序為 `50599d5fbc6990018ddd7a5698aba52852fe721688aba47f3a2dfdd8fc5c9e6a`／
  `ea4bdf44e7314c828964c7732ad5372dc6bcf4c548c38b49fbd5d18e630ea900`。
- 完整argv、nodeids、environment／SDK／runtime pins、406個src/tests/controller Python
  working hashes、各phase原件及safe proof另存session `sk06-current-qualification`。
  沒有raw prompt／skill body／private reasoning／credentials；後續doc-only commit不是受測baseline。
- 原b7/ec8/a9、c7的37＋1、producer f24的156＋1及TC-05均保來源，不重跑271或無關suite。
  無Azure／paid／IAM／build／deployment、SDK／harness更新、新agents／reviewers／factory，
  無自行合併或全域接受狀態更新。

## Skill deactivation 固定來源接續

接手工作階段另於2026-09-26在clean **`f24e28bfe67af86132a4334f321a446613aeb839`**
執行三批；這些早於上節12:12的6fa共同來源資格，不是其重跑或額外品質樣本。
各批共同408個tracked Python工作檔SHA相同，測後未改；沒有改產品、測試或判準。

| 批次／開始時間（UTC+08） | 原結果與範圍 |
|---|---|
| `api-consumer-fixed`／11:24:05 | **101 passed、2 setup errors、0 assertion failures、0 skipped**，13.224秒；一個native情境明確deselected。兩個loader案例因接手環境未設定`M2_TEST_RUNTIME`，尚未進斷言就失敗 |
| `current-source-fixed`／11:26:21 | **2 passed／0 failed／0 errors／0 skipped**，11.004秒；只補跑上述兩個loader案例，改用既有runtime路徑，沒有安裝／升級或改fixture |
| `sdk-lifecycle-fixed`／11:26:21 | **1 passed／0 failed／0 errors／0 skipped**，19.816秒；purpose-profile三回合，真SDK1.0.13／runtime1.0.83、scripted offline provider |

首批exit1與兩個setup errors保留，不能改稱「103全過」。101項涵蓋停用API／client／CLI及舊M3；
兩loader案例先取得已批准本文及獨立operation grant，再於provider credential接點交錯正式停用，
由原source gate拒絕外送；後續read／reserve／consume也拒絕，原intent、pending cost及grant保持。
這两案worker-start是明示替身，沒有SDK process，不用fixture stop冒充原生停止。
Native則沿原conversation／SID走v1→停用後空catalog拒read→另批准v2；3個process／6個offline requests，
Windows stop均exit1／graceful=true、force0，舊user/tool事件與archive保持。簽章身份及資料均合成，
不是實際真人審核內容、模型已遺忘、自然LLM防注入、Hosted或附帶資源切版證據。

### Verified against

- 實際檢視`m3_models`、`ArtifactService.deactivate_skill/decide/policy_context`、`m3_api`、
  `ArtifactClient.deactivate`、`m3_cli`與`ApprovedSkillSource.catalog/read`；實作沿原接點，未建新loop。
  測項為`tests/shared_context`內三個`test_skill_deactivation*`及
  `test_m3_artifacts.py`、`test_m3_api.py`、`test_m3_cli.py`；後兩批只跑context檔指定cases。
- JUnit SHA依三批順序為
  `418bbcf8272933bc99af76b47bbde8339f76668049ee1294f6c12f1fe4adf8f0`、
  `6967efc9023775982c632f38d95633adcbdb1f6d4a43543e62c31a880ebb59ab`、
  `92c48fdeb914eef1a042ced3c05eed189f7c79159f7e11720c90f309f18ee11c`；
  native安全回條為`a5dc4318c15668ccc05e808bbb52fa8703c0bee43bf4c1dad6da8bd1d4241b4d`。
  受控session的`skill-deactivation/<batch>/`保留argv、source、JUnit/log及回條，
  帳冊只使用opaque refs，不公開本機路徑、原prompt、credentials或SDK私有state。
- 新三筆接在main #50的476筆之後；兩邊原447前綴相同，原476／spec／bindings不改。
  SK-06仍接受實測6fa，其他三C及12情境保持；f24的較早mixed批次不改成6fa結果或新驗收。
  本次匯入沒有新SDK／雲端／paid執行，也不把此資料head當實測baseline。

## SK-09：回合中正式停用後的終局與新回合接續

2026-09-27補上先前分開驗過、尚未直接組合的一段：**signed human deactivation發生於
active turn之後，先保存原失敗／取消，再沿原對話取得目前catalog**。沒有發現或預設
產品bug，也未修改service、SDK、loader、授權或停止政策；只新增獨立API契約測試。
這是SK-09的**C子集合補證**，不提升完整SK-09、SK-12或情境接受狀態。

| 接點 | 本次觀察／判定 |
|---|---|
| 原有效來源與回合中停用 | 兩種skill profile都由signed M3 upload／human approve取得v1；真正read後，原model intent先consume，再於既有provider credential接點正式停用。最後authority gate回403 `SKILL_SOURCE_CHANGED`，provider transport未被呼叫 |
| 尚未終結的原責任 | running、stop尚未reconcile、result尚未forward三個時點，新增對話回合皆409 `CONVERSATION_TURN_UNRESOLVED`，control guard及原model accounting不變 |
| failed／cancelled終局 | 在fixture的graceful stop前提下，原sdk-state／save-text-reply、worker reconcile及controller forwarding完成後才允許新turn；取消另走signed cancel。原failed／cancelled結果、save receipt與archive保持，沒有成功reply |
| 下一合法回合 | 原conversation／SDK SID及保存的fixture history保持；當前catalog為空、unavailable count為1，同名skill read回403，不把歷史本文當目前可用能力 |
| 再批准新版 | 另走原human approve產生v2；之後的新turn真正read到v2本文與catalog entry。v1版本、原deactivation receipt及前三turn保存材料不改 |
| 用量與防重送邊界 | 原consumed intent仍true，另一reservation仍false，pending cost全程保持；每case實際1筆deactivation receipt。這不是零寫入replay或一般外部exactly-once保證 |

### 固定來源與分層結果

先提交source/tests **`4c2a0b35c09e160ba97bfda93b2fef890fd1642e`**，
tree `7e2ee531ab6b71305c18ed3503318bfe81044361`，base為已合併的`00d5c1b`；
09:30:10（UTC+08）在clean source執行唯一新檔，**4 passed／0 failed／0 errors／0 skipped，
22.50秒**。四案是兩profile×`failed`／`cancelled`，不是四次native或自然模型抽樣。
src／tests／controller內419個tracked Python檔案在測前後SHA相同；
後續報告／整合head不當作新實測來源。

本批使用真API／authority／store／controller程式與in-memory storage adapters，
**worker start、SDK state及`SdkStop(pid=99, exit_code=0, graceful=True)`明確是fixture**。
Historical tool events由真正read的合成本文建成SDK-state fixture，並由原save／restore保存，
不是這次執行了SDK工具循環或觀察到真process stop。native client／subprocess入口有禁止guard；
其0次觸及只表示此manual API case沒有啟動，不冒稱自動啟動、SDK send／stop或cold-resume證據。
Provider的0次transport則有真正MockTransport入口counter；不把兩種0混用。

四份SafeJSON各記1筆停用回條、0次provider transport、原intent狀態、
archive／result／reply與save receipt hash；legacy／purpose profile的synthetic pending
microusd分別為52909／52996，兩種terminal結果相同。這些不是雲端帳務或剩餘預算。
舊f24／6fa的native結果不重跑、不改綁；未涵蓋missing／unknown archive、一般unfinished
recovery、forced／ungraceful stop接續、native cancellation、附帶skill資源、
mixed-version部署或模型語意品質。

### Verified against

- 新測項：`tests/shared_context/test_skill_deactivation_continuation.py`；
  沿既有`deactivate_original`、`open_turn`、`settle`及signed M3 producers，
  未改shared helpers。原契約見[Target State SK-09](grok-aligned-target-state.md#7-skills-與-context-預算的完整目標)及
  [行為規格的單一版本停用段落](FA-AGENT-BEHAVIOR-SPEC.md#2026-09-26-本機接點單一有效skill版本停用)。
- 已讀`behavior_instruction.validate_turn_skills/restore_turn`與原save consumer、
  `behavior_assistant_hosted.AssistantClient`的read／reserve／finish接點；
  已有的mid-turn更版、正常回合間停用與SK-06 source fence，不冒稱本次新做或完整SK-09。
- 精確runner為`.venv` Python3.12.10／pytest9.0.2，
  `python -B -X utf8 -m pytest -q --tb=short -p no:cacheprovider --junitxml=<owned-output> --basetemp=<owned-temp> tests\shared_context\test_skill_deactivation_continuation.py`。
  沿既有SDK1.0.13套件，**native runtime／雲端／paid／build均未執行**，無套件或harness更新。
- Scope SHA256 `88f58de0927fce1ea5f71545c9de1c7089cbb66aa7ecc5c08a6851ac419904f1`；
  JUnit `ed3591a99faadc60f32e985d0e99a261cdafd7a037415fb570101c28ebed0019`；
  execution metadata `4f6c30296f8cfc40eaab52d95983049082d79b644dee18f7b0945b7079aea918`。
  完整argv、src／tests／controller內419檔hash、log及四份safe proof留在受控session的
  `sk09-midturn-4c2a0b3-20260927`原件；不公開原prompt、SDK私有state或credentials。
- 帳冊接回時另核原工作線的`sk09-midturn-execution-run-20260927.json`，
  SHA256 `e1f48e1e3b52991437c9723d70e6f1237185db980e93f8b75e80421cbda1685d`。
  這份去敏交接回條連到上列JUnit／execution metadata，不是新的測試執行或同一個檔案hash；
  帳冊引用此回條時，原實測source仍4c2a，未由整合者重跑。
