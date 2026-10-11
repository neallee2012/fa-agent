# 版本化 Prompt：本機實作與分層驗證

## 2026-09-27 Resource profile 的穩定性與公開投影

只補`fa-instruction-skill-resources-v1`的缺口，沿既有native prefix fixture，
不放寬原system／完整canonical tool catalog／舊archive／strict bridge判準。
原18案參數與hash不變，新增普通、checkpoint、更版、更版加checkpoint四案，
另跑原purpose一案核shared fixture相容性。

測前固定resource prefix為3626 UTF-8 bytes，SHA256
`1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd`。
普通request仍須只有一段相同system bytes；工具目錄不刪除任何欄位，
只有已批准catalog版本改變才允許目錄hash改變。資源正文與raw tool IDs不能進入
prefix／tool catalog／public progress，原資源讀取必須有相符的公開配對事件。
Compaction由原`checkpoint_compaction`欄位分辨，不算普通prefix request。

固定`84417d132ffb92cf08e6a0434263dd3095573f6a`五案
**5 PASS／0 FAIL／0 ERROR／0 SKIP**，72.89秒。17次SDK opening及正常stop回傳，
force-stop呼叫0；35個普通／2個compaction scripted requests，沒有真模型或雲端呼叫。
Resource四案共6次真正custom resource read，核原M3 scope/version/hash、冷恢復、
獨立新對話、舊archive原bytes與strict public projection。
JUnit `resource-prefix-focused.xml` SHA256
`0f1442a92aff165c2a04048abd887a84d32a294baf746cf270dbbe3432f61971`；
逐案去敏結果保留在本session的`resource-prefix-84417`。

先前單案WIP1 PASS另留，本批不回填c8e／24eb或其他native來源。
尚未重跑其他profiles／全部可選設定／DOM聯集，**不單憑本批恢復PA-02／TC-01完整接受**。
這是resource路徑的增量證據，不是provider cache、自然模型品質或新部署，也不作部署gate。

### Verified against（Resource profile）

- 原`test_prompt_prefix_stability_native`完整system/tool catalog與保存／投影／stop判準；
  test helper只加可省略的resources/profile參數，原呼叫保持。
- `ResourceSkillReadResult`／`SkillResourceRead`及`PublicProgress`配對；
  產品程式零修改，SDK1.0.13／runtime1.0.83，scripted provider不是human gold。

### 接回workspace修補的單案相容性

原五案之後，#103發布虛擬workspace及舊cwd stat-only相容修補；本線接回該程式，
不把84417的結果改綁到新source。Review另提醒原assert只核「完成對應開始」，
未排除多出的起始事件；新斷言補resource starts／completions集合及數量一致。

在`e9d760d8d7b87eda967023c6e3df7eca4f9846f5`於2026-09-27T20:18:36+08:00
只跑resource checkpoint一路，**1 PASS／0 FAIL／0 ERROR／0 SKIP**，19.676秒。
4次SDK opening／正常stop、force0，7普通＋1compaction scripted requests；
此為Windows的必要consumer相容性，不是重跑#103的Linux原批或現場Hosted修復。
JUnit `resource-prefix-interop.xml` SHA256
`b634e6902507898d7b07e73c53fcf331f493c7f5e1e0ac3a68f209f965c4a94c`。

帳冊先保#103已發布723完整前綴，原resource五案只把未發布sequence706移至723，
payload／hash／spec綁定保持，再新增本單案。TC-11依主線維持partial及原接受run，
其他狀態不升級；本線舊707不是新冊完整前綴，不拿單案抹去原live FAIL。

## 2026-09-26 新對話 default 接線

這次只把既有`fa-instruction-v1`接到受信的新對話default，不改prefix bytes／hash、
原工具集合、舊policy或checkpoint codec，也不借局部結果更新七／八prefix的完整資格。
已有對話／request沿原profile；環境變數改值或取消不重解釋原件。
明確的policy profile優先；使用不到default時不因其錯值阻擋既有設定。

| 本次批次 | 實際結果 | 範圍 |
|---|---|---|
| 新接點原RED | 2 PASS／7 FAIL | 新default尚未進task；未知設定仍被接受，WIP |
| 第一版接線 | 5 PASS／4 FAIL | 選擇／接續已工作，但新503錯碼被generic mask，WIP |
| 安全錯碼與直接回歸 | 55 PASS／11 setup ERROR | 13個新案＋42個相關舊案通過；11個legacy lifecycle缺固定runtime，WIP |
| 補環境的原11案 | 11 PASS | 同版本1.0.83、獨立HOME、scripted provider；不是重跑前55案 |
| 明確policy與cache-miss補驗 | 16 PASS | 包含錯誤default不干預明確policy、原registration重建及混合profile拒絕，WIP |
| 新default native案 | 1 PASS | 2runtime／3scripted requests；新設定→保存→改成無效設定→原SID冷恢復，WIP |

以上是開發時的分批結果，不相加成單次全綠，不回填成之後的commit執行。
Native案沒有啟用compaction；兩次stop原值exit1／graceful／force0。
FA送給SDK並保存的user原文完整；provider呈現仍沿原生SDK轉換，沒有宣稱逐字完全相等。
V1 prefix仍為2094 UTF-8 bytes／`8e24b529…`，tokens未知。
Policy未改、未批准操作沒有生效，公開保存未含private prefix／credential標記。
沒有雲端、付費模型、人工標註或全Target／指令期限語意接受。

### Verified against

- `behavior_work_policy`／`behavior_work_policy_history`的同一原record重建函式，
  `behavior_instruction.registration_image`及原API／Hosted consumer。
- 本session的`prompt-default-red.xml`、`prompt-default-green.xml`、
  `prompt-default-targeted.xml`、`prompt-default-lifecycle.xml`、
  `prompt-default-complete.xml`與`prompt-default-native.xml`，原件保留。
- 原生觀察為`native-prompt-default/server-default.json`；不用scripted答案判定真模型品質。
- 固定source另記如下，不以WIP資格替代。

### 固定source 4f35fb9

在`4f35fb998b4d579c9e6348202ca6d803bd6db292`一次pytest執行
**17 PASS／0 FAIL／0 ERROR／0 SKIP**：16個API契約與1個固定SDK的新default情境。
JUnit起時2026-09-26T23:36:44+08:00；native仍是2runtime／3scripted requests、force0，
不把另16個I/O替身案例也說成native。Python3.12.10／SDK1.0.13／runtime1.0.83／pytest9.0.2。
JUnit `prompt-default-final.xml` SHA256：
`db0f79357ff691de9e0630ea7533231042f631018e3153e8d243abbd8c600a99`；
原生觀察另存`native-prompt-default-final/server-default.json`。

帳冊只追加這筆固定run到574，保已發布573完整前綴。
SP-00維持partial、baseline指本次實測source；4個C及12個情境不升級。
原WIP失敗／缺runtime與補驗留在上表，不倒稱在4f35執行；新profile尚未部署／啟用。

### 2026-09-27 review跟進

1807cff的Opus5.5兩軸均0P1/P2；另採納非阻擋建議，重用既有`_job_record`與
`PromptProfile`，把接單前default錯誤改為專屬`INSTRUCTION_DEFAULT_PROMPT_CONFIGURATION`。
Hosted原有的`INSTRUCTION_PROMPT_CONFIGURATION`屬接單後錯誤，不可被這個訊息說成未接單。
README也明示：舊API驗整個registry，回退可能使全部standing work／收件拒絕。
因此實際採用要先上reader，待不支援的舊revision退出，再開default，不另建治理流程。

修補WIP的17個API案例通過後，固定
`90a789f6a0967e4ee107951cbc7c05ac52981bd2`於2026-09-27T00:03:45+08:00
補驗同17案，**17 PASS／0 FAIL／0 ERROR／0 SKIP**。
這次沒有重跑native；4f35的SDK結果仍歸原source，不把兩個不同17-case批次混成同一輪。
JUnit `prompt-default-review-fixed.xml` SHA256：
`da885caaaefb9e9ef5a9d6dd0fc481891953bdd9a288ca243c216ff2b12cec14`。
帳冊新增至575，保已發布573及本線574前綴；SP-00仍partial，只把API修補來源更新到90a。
新final仍待同兩軸確認，沒有新部署／啟用或模型操作。

原驗證日期：2026-09-24；增補日期：2026-09-25。四／五／六prefix接受均保留歷史。#30新增第7個已審工作記憶profile後，current PA-02／TC-01再次先回partial；現在以[同一新baseline的七prefix矩陣](#第七個prefix與已審記憶重驗)重新提出接受，已在#32固定`1c3db91`兩軸0P1/P2後合併，不借六prefix的#27 clear或回填原結果。

## 固定來源與接線

初稿 `c5cc2846a418aa4af70ee7f76ecf80bcb108c1cd` 基於 CTX-03 `e80a215`；收件的短 history 實驗揭露 parent 的 signed-token 缺陷後，由原 owner 在 `c51e730` 修正。Prompt 以保留歷史的 local merge `374d8d11c4f4feab111f10efd666092aef3f2599` 接入；兩個 parent 正是 `c5cc284`／`c51e730`，沒有重寫既有 commits。

最終固定 head：`a9a3ce1cdc7dfab0c282fbf557ebd3398713190a`，tree `edbd01168a86adbabf5e37ff5e872b0eba463b04`，PR base 為 `c51e730`。Tested merge tree `36ec974a2dcb75022e4ca7c45ef60ced32b44e09` 到 final 只有 README 增補，產品與測試 bytes 相同。

受信的 `AssistantProfile.prompt_profile` 經既有 profile／configuration digest、WorkRegistration、不可變 `InstructionTask`，到 Hosted 的實際 ToolSpec 與既有 SDK `system_message` replace。使用者文字、`InstructionSubmit` body、模型輸出和 save client 都不能自選 profile 或提供 manifest。

`behavior_instruction_prompt.py` 是 SDK-independent Module；沒有第二個 agent loop、transcript engine、技能 loader、Memory 或 epoch/cache 系統。新 profile 預設 OFF；未知 profile、重複／不符能力明確拒絕。

| Profile | UTF-8 bytes | SHA-256 |
|---|---:|---|
| Legacy（未啟用） | 552 | `f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f` |
| `fa-instruction-v1` | 2094 | `8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb` |

前綴不含 user text、clock、job ID、檢索結果或可變權限；只宣告實際的 `read_context`／`execute_operation`。提及未配置的 voice／browser／skills／Memory 是否定句，不代表能力已啟用。

## 保存的是配置，不是 attestation

Server 依原 task 重算可選的 `TurnArchive.prompt_assembly`；save body 不接受 client manifest。History／conversation readers 核對 expected record 與 material；未啟用時 `None` 不序列化，舊 DTO／TextReply／reference bytes 保持。

Record 為 `fa-prompt-assembly-v1`、source=`server-profile-recomputed`、catalog=`fa-instruction-tools-v1`。只保存版本、hash、bytes、真實能力集合與 omissions；`prefix_tokens=null`，bytes 不當 tokens。它不證明模型已執行或確實收到配置，失敗 turn 也可保存配置。

原 live authority／revocation、once／unknown、accounting、private filter、checkpoint／stop 契約仍由原路徑執行，不由 prompt 授權。未保存 raw system／reasoning／credentials／full provider packets。

## 原批次：不回填為一次全綠

以下 JUnit 與 frozen manifest 的 hash／摘要均已核；數字不是新 head 全套重跑，也不能相加為不重複測項。

| 原件 | 結果 | JUnit 秒 |
|---|---|---:|
| `prompt-api-unit.xml` | 61 passed，包含新增 API／metadata cases | 59.485 |
| `prompt-compatibility.xml` | 131 passed | 182.670 |
| `prompt-lifecycle-regression.xml` | 8 passed／1 collection skipped | 39.734 |
| `prompt-integration.xml` | 49 passed／5 failed | 56.167 |
| `base-startup-diagnostics.xml` | 同兩個 startup fixture failure 在固定 e80 重現 | 4.723 |
| `prompt-final.xml` | 31 passed／1 failed，短 history 正向嘗試 | 32.755 |
| `prompt-native-ctx03-long.xml` | 另設長 history 的 1 passed | 10.293 |
| `prompt-native-frozen.xml` | 最後三個明確條件的 3 passed | 24.755 |

Startup 兩個 `test_failed_sdk_acquire_retains_status_not_upstream_body[412/503]` 的 fixture 未提供 acquire timing 需要的 `assignment.task`，在相同相依、immutable e80 snapshot 也失敗；本切片沒有修無關 fixture。Collection skip 是缺原 pinned Window2 fixture，沒有補造。

**短 history 已實際失敗，不能標 NOT_RUN。** 原 `SDK_STATE_SOURCE_UNVERIFIED` 後續經短／一筆 padding／legacy prefix 的單變數對照，確認 native 成功的 `tokens_removed=-11` 被 FA pair／DTO 的非負假設誤拒；長 fixture 通過不補回原失敗。修正與舊回條沿革見 [CTX-03 驗證](ctx03-local-acceptance.md)。

## Native 觀察的三個不同層

SDK／runtime 固定 `1.0.13`／`1.0.83`，Windows x64／Python 3.12.10，provider 是明確 offline fixture。

| 層次 | 實際觀察 |
|---|---|
| FA → SDK send／持久 user event | 原文完整保留，包含前後空白、CRLF、Unicode |
| 正常 provider system message | Legacy／v1 prefix 逐字匹配；compaction request 此欄為 null，不冒充 normal prefix |
| Provider user-message | 這組 fixture 的完整原文 occurrence=0、trimmed-content occurrence=1；沒有剝 SDK 原生脈絡求全等 |

最後原矩陣是 legacy、v1 ordinary、v1 long-history CTX-03，各兩個 turn：**6 次 SDK send、10 次 offline model-transport requests**（3／3／4；9 normal＋1 compaction）、6 個 runtime。原 CTX-03 regression 另有6次 offline requests，不混作同批；全 session 開發嘗試／全部 JSON-RPC 次數未彙整。

Private system event 確有產生，但公開 snapshot 不含 prefix／credential canaries。未批准的合成 tool call 沒有改 operation row，這不證明已批准 operation 正向成功。

## 接入 signed parent 後的精確 green

未改原短 probe，只將已審 parent `c51e730` 合入；v1、padding=0、52-byte first reply、相同兩個 user inputs 與 summary。

- `short-v1-native.xml`：1 passed，11.460秒。Native／RPC 成功、manual、checkpoint=1、messages=3、tokens=-11 原值保留；第二個 user message 送出，兩個回覆及 reference 保存，`can_follow_up=true`。
- `direct-unit.xml`：23 passed，36.807秒。只跑必要 bytes／profile／metadata／history、signed／type／mismatch／private 邊界；不是重跑舊完整矩陣。
- 這批為1個 Python runner、2個 runtime、2次 SDK send、4次直接計數的 offline requests。兩個 runtime 正常 stop 原值 exit=1／graceful／force-stop=0；不挪用 parent 的第三個 legacy runtime 充當第三個 v1 restore。

## Review 與未驗界線

Standards／Spec 均由同一 `claude-opus-5.5` 對 final `a9a3ce1` 獨立判為 **0 P1／0 P2**，核對 merge ancestry／tree、consumer 不漏接及這次固定證據；不是借 parent clear。

第三次 v1 fresh restore、同一 conversation legacy→v1、approved operation 正向、所有短 history、真模型／human scope semantics，以及 Hosted／Linux／Blob／Q14，均不是本批接受。SP-31／PA-15 只有 guidance 接線；source review 不等於語意品質。

## 2026-09-25 三種instruction profile的穩定性矩陣

這是原PR11之後的獨立資格核對，不回填上列結果。當時測試只觀察既有產品，沒有修改prefix、profile、SDK或權限；**該32-case批次只支持partial**，後續完整C批次另記。

固定測試commit `1ac534830572c25b249d0fa19c230aa3c8d83df5`，基於已合併`d28605b`。三種已實作的instruction profiles各走真HTTP／權威服務／固定SDK，第一回合、同conversation的fresh-process追問、另一個全新conversation各一次；中間改變server clock、user text、job/binding與本機工作目錄。

| Profile | 每次實際provider收到的FA prefix | 同版本結果 |
|---|---|---|
| Legacy `None` | 552 bytes，`f2d312f2…084799f` | 三次相同 |
| `fa-instruction-v1` | 2094 bytes，`8e24b529…13963cb` | 三次相同 |
| `fa-instruction-skills-v1` | 2572 bytes，`14a9d2a6…bf4ab3e` | 三次相同；當前工具目錄未變 |

完整hash沿本文件及[Shared Context README](../src/fa-shared-context-poc/README.md)先前發布的固定值作獨立預期，不從本次assembler輸出重算預期來自證。測試同時核SDK接收到的replace配置與實際provider system文字；每個request只出現一份相符prefix。User canary不進system段落，SDK `send`原文保持，ambient instructions／plugins／skills／MCP仍停用。

同profile的工具目錄以實際request正規化後的hash比較，三次一致；三種prefix的固定hash彼此不同。版本化profile在conversation投影中的配置metadata與實際prefix一致，tokens仍為null；legacy則維持不增加metadata的舊形狀。這只驗配置與實際送出相符，不把配置紀錄本身當runtime attestation。

| 批次 | 實際結果 | JUnit秒 |
|---|---|---:|
| 第一個legacy WIP probe | 1 passed，3個offline requests；baseline未知 | 13.029 |
| 固定commit後的完整矩陣 | 32 passed：3個native cases＋29個既有selector／metadata／bytes正反案例 | 35.272 |

後一批是9個offline requests、9次正常SDK `stop()`返回、0次 `force_stop()`；公開 `get_status`讀回的runtime均為`1.0.83`，Python SDK為`1.0.13`。較早3次是另一個WIP執行，不能加到後一批當獨立品質樣本。兩個warning分別是Starlette alias deprecation，以及刻意構造future profile反例時的Pydantic serialization warning。

**當時未涵蓋：** 非instruction的structured assistant路徑、完整system清單、source/tool canary及合法設定換版的舊安裝拒絕。這些缺件在下節補齊；provider cache命中、compaction摘要system、token估計與模型語意則仍是不同問題，不由相同bytes推論。

## PA-02程式驗收

**首審歷史：** `6e15129`的Standards為0 P1／0 P2，Spec為0 P1／1 P2。下方37-case保留為當時的提案，checks、baseline與原件不變，帳冊分類已撤回subset。補測與新的完整run見[修後42-case](#修後42-case同baseline矩陣)，不把首審clear或舊結果借給新head。

PA-02的C條件是「相同適用指令／catalog版本，FA控制的穩定段落保持順序與bytes；不混入每輪時間戳或搜尋結果；真正改版可解釋變更」。不是provider KV cache命中率，也不要求在同一CID內即時搬換selector；固定prefix期間的更新屬PA-05。

程式盤點確認當前主Hosted assistant的prefix來源只有三種instruction selector與m2的structured預設。Structured prefix在先前已發布`ad3ff0b`的`m2_copilot.SYSTEM_MESSAGE`為264 UTF-8 bytes，SHA256 `68daaf7242fa325f8bfb107c4cfc7bb44e065b9eed93d91780b3f402fb3b6f71`；不是從本次執行輸出產生預期。

| C條件 | 同一固定baseline的實際證據 |
|---|---|
| 相同版本的FA段落一致 | 四個已發布literal hashes／264、552、2094、2572 bytes；實際SDK options及每次provider request皆精確符合 |
| 內容順序與完整性 | system message清單**恰一個完整固定值**，不是僅找到一段匹配文字而忽略額外system |
| 當輪資料不進system | 不同user、clock、work／job、conversation、fresh filesystem；實際read_context回傳的source hash及tool-call canary確實進資料訊息，但不進system |
| 版本可解釋 | 各合法新設定／conversation的profile、hash、bytes與原TurnArchive／API投影相符；legacy不增加metadata；prefix版本不同時hash不同 |
| 舊安裝不靜默改版 | 新的desired selector不能attach原durable installation，明確`WORK_POLICY_SCOPE_CHANGED`；原CID／queued結果不變，沒有手改批准／profile rows |
| 支援生命週期不混淆 | Structured legacy fixed-binding可cold resume；steady-work則驗獨立新job，未完成restart仍fenced |

現行沒有受控的同CID selector migration Interface；selector來自持久`policy.profile`，`installed()`核原profile。新版本需要新設定／安裝。先前「同CID legacy→v1→skills」的測試建議經source核對撤回，不偽造該producer來湊通過。

### 同baseline合批與保留的失敗

固定source **`4c1f2cdb83f9927af1641e7758fd3086281aba0e`**先建立並確認工作樹乾淨，再於台北05:26:03執行完整37-case合批：**37 passed／0 failed／0 skipped，53.778秒**。其中5個native cases共23個offline provider requests（instruction三案各4、structured legacy5、steady6），另32個非native正反checks。沒有新產品程式、SDK版本或權限變更。

前置批次保持獨立，不能相加成品質樣本：

| 批次 | 原結果／意義 |
|---|---|
| Structured初次WIP | 1 failed，9.420秒；test錯把steady-work未完成restart當可用，原fence正確，未放寬產品 |
| 分開兩種合法policy後 | WIP 2 passed，17.442秒 |
| 固定`a2514c0`後 | 2 passed，18.489秒；structured各5／6次request，未宣稱有收集stop或exit code |
| Exact-system/source強化WIP | 6 passed／2 failed；structured tool ID經既有boundary正規化為`fa_call_1`，不是原provider ID |
| 安全診斷 | 1 failed；只有tool-ID期待不符，system清單／hash及source資料已正確 |
| 對齊既有canonical ID | 2 passed；未修改產品SSE／tool-ID正規化，之後才固定完整合批source |

Temporary診斷只留下counts／hashes／booleans，其測試內探針已移除。原FAIL XML與最初失敗測試副本仍保留，不回填為產品RED→GREEN修復。

首版曾提出`target_acceptance`／`full_target`與1／76，但Spec P2證明尚有可達路徑未覆蓋，因此在合併前撤回。新的接受只能由後續完整矩陣的獨立run支持，不能改寫37個已觀察checks或借舊subset review代替。

預定接受範圍只限PA-02的FA-owned穩定prefix不變式，不含PA-05 live更新、任意新profile、模型語意、provider cache／token／SLA、Azure／Hosted／Blob或human-read。未啟用的voice／其他能力仍是其他目標的待辦，沒有被列N/A。

### 修後42-case同baseline矩陣

新增覆蓋不是只增加相同常數的重複assert。Legacy、v1、skills各自有checkpoint opt-in；skills另有普通與checkpoint兩種catalog切版案例。Checkpoint case包含第一輪、發生manual compaction後的第二輪、**另開runtime讀回已發佈DIAG checkpoint的第三輪**，最後才建立獨立conversation；同一對話仍用原SDK SID。

Model request的摘要／正常分類來自原`AssistantClient.send_model(checkpoint_compaction=...)`實際參數，觀察後原樣forward，不以「沒有預期prefix」猜它是摘要。四份摘要請求另記為SDK compaction用途，不拿它們冒充正常FA prefix觀察。最終所有47份正常請求，包括compaction後與inherited DIAG cold restore，system清單都恰是該profile的一個固定值。

Catalog更新使用原M3 upload／human approval producer產生v2，沒有改row偽造有效版。實際`read_skill`目錄的version／hash改變，system prefix仍是原版本；當輪目錄逐次比對`TurnArchive.skill_catalog`，所有較早archive序列化保持不變。這只接受當前catalog版本的解釋與prefix穩定，不接受PA-05的一般live selector migration。

| 新批次 | 結果與歸屬 |
|---|---|
| 新五案首次WIP | 5 passed；與後續合批分開，baseline未知 |
| `5de36c2`的第一次擴充 | 42 passed／47次offline requests；包含compaction後正常請求，但尚未加入已壓縮狀態的再一次cold restore，保留subset |
| **`22e62acd7f659a9c4c48567f12d839b50fd81ca0`** | **42 passed／0 failed／0 skipped，JUnit 116.684秒**；06:32:04 commit後、06:33:42開始、乾淨工作樹 |

最終42個checks＝10個native cases＋32個非native正反案例；**51個offline provider requests＝47 normal＋4 SDK summary**。其中四個checkpoint cases、兩個catalog-change cases，沒有第二次未授權compaction。原37-case及第一次42-case均未重標成此結果。

當時帳冊追加原兩軸首審、首次新增五案、第一版42-case及最終42-case共5筆，成為172筆。只有最後一筆以`target_acceptance/full_target`與`22e62ac`current baseline支持PA-02 C接受；修後同兩軸0P1/P2並合併，完整target為1／76、scenario0／12。source review不是human gold，之後的其他局部觀察不改這份接受。

### 第五個prefix的重新驗收

[PR20受控原話reader](user-turn-reader-evaluation.md)增加`fa-instruction-user-turns-v1`，2822 bytes、hash `a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372`。原22e接受只涵蓋四來源，因此新產品整合時current state先設partial；沒有改舊accepted run或假稱它已測第5個prefix。

在完整既有矩陣上加入new reader的ordinary／checkpoint兩案，以及新selector與old installation的兩個拒絕方向。新兩案各四個user turns：第一回合、下一回合實際`read_user_turn(1)`、再開fresh runtime繼承來源metadata、最後另建獨立CID。讀回的原文、source CID/request/turn/hash及usage type核對，reader結果只進tool data；舊archive不改，新CID的inherited sources為空。

新profile照樣驗SDK options與每一normal request的**exact單一system清單**、literal prefix hash、source／tool／user canary分層、metadata與catalog版本。Checkpoint案包含摘要後的正常request及DIAG cold restore；沒有用舊profile的PASS推斷新capability。

| 批次 | 結果／基準 |
|---|---|
| 新reader paths＋舊安裝反例首次 | 7 passed；WIP，null baseline，與後來合批分開 |
| 完整五prefix矩陣 | **62 passed／0 failed／0 skipped，146.502秒**；`2b942a1d523897fea5e99ef4167f0e9dc63a4f2d`先於09:50:45提交，09:51:06在乾淨該commit執行 |

62個checks＝12個native cases＋50個非native正反案例，**64個offline provider requests＝59 normal＋5 SDK summary**。新兩reader案各6 normal，其中checkpoint案另1 summary；其餘已接受路徑同批重新核對。所有summary仍依原checkpoint flag辨識，不因missing prefix猜分類。

這是一筆新的`target_acceptance/full_target`與新current baseline，不把先前51次改成64次，也不疊加品質樣本。PR21已通過同兩軸review並合併；PA-02仍只是一個target，Reader的full eviction、TC M品質與其他功能不因此通過。

### 第六個prefix與公開投影重驗

**後續main30差異（歷史）：** 新增第七個`fa-instruction-work-memory-v1`後，原六prefix接受保持歷史，
當時PA-02先回partial；[本機整合子集](tool-progress-correlation-evaluation.md#main30-整合與修後限定驗證)
不能取代完整七prefix資格。後續#32的`cb4b180`完整七profile矩陣已提供新資格，見下節；
本線工具配對不改prompt，沿該PA-02資格，不回填原子集。

[用途metadata切片](runtime-skills-evaluation.md#用途metadata隨批准版本)已經兩軸修補與純Git整合後合併。
新`fa-instruction-skill-purpose-v1`穩定段為3002B／
`65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879`。
原五prefix接受不覆蓋它，source commit `ed6f64e`明確先保留current PA-02為partial再跑完整矩陣。

新增purpose的ordinary／checkpoint，以及同body只改已批准用途的ordinary／checkpoint四案。
每案真正`read_skill`返回v2 body／metadata；換版先沿M3產生新version及human approval，
metadata hash改變而body hash相同，穩定prefix不變。目錄與當輪archive相符，較早archive原樣保留。
Initial metadata沒有current body marker，讀後才進tool-result訊息；checkpoint案另含compaction後及
下一個fresh runtime從已發布DIAG狀態恢復的正常請求。

每個instruction native回合的真API projection還交給薄client實際使用的
`ConversationProjection.model_validate_json`驗證，核對CID／job／reply_available與server follow-up欄位。
這直接涵蓋過去缺失的typed metadata consumer；**它是native資料經真DTO，不是每個native案都啟了browser或bridge**。
同一批另有12-case真API→bridge正反矩陣和32-case minimal DOM，消費路徑的證據分層而不冒充。

| 合批資料 | 精確結果 |
|---|---|
| Source baseline | `ed6f64e98367bddb5a86ebe359ce6a4fd25cc02a`；16:00:41先commit，16:03:15開始，工作樹乾淨 |
| Python／DOM | **105＋32＝137 checks全過**；Python JUnit384.85秒，無FAIL／skip |
| PA-02子集 | **80個Python checks**：六來源、known literal hashes、exact system/data分層、current版本/approval/cold restore、selector／record反例 |
| TC-01子集 | **39 Python＋32 DOM＝71 checks**：14個native projection DTO cases與25個public-view／guard／bridge checks，加DOM |
| Prefix native模型請求 | 16個native cases：**83 normal＋7 SDK summary＝90**；summary按原checkpoint flag另記 |
| 另外的retention native | 四process／6個offline requests、13次舊reply可取與bytes/hash/no-stream／非ACK觀察 |

此批已記錄的native-driven offline requests為96；HTTP/bridge替身不是額外native。
**80與71是同137次checks的重疊子集**，不是兩個runner批次，也不能把model用量加兩次。
新的purpose四案及七個selector拒絕先前另有WIP11 PASS，沒有回填成本次commit的實測。

PA-02的current C提案使用新80-case子集；TC-01因兩個已修接點而用71-case子集重新提出，
見[公開投影的重驗](transcript-projection-evaluation.md#修後完整重驗)。
舊accepted runs、兩次current withdrawal、原compaction反例及bridge502都保持歷史。
這不接受SK-02 M相關性、full eviction、native背景壓力、真browser／Hosted／Blob或human-read。

### 第七個prefix與已審記憶重驗

[Team工作記憶reader](reviewed-work-memory-evaluation.md)已與新的TOOL-06 schema回饋整合，
在#30固定`efd2a348`雙軸0P1/P2後合併為`cfd9b7d`。
`fa-instruction-work-memory-v1`的literal prefix為2719B／
`1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3`。

先把current PA-02／TC-01設partial，再在source/test commit
**`cb4b1805c9622817453f6711e23c7df0c82a034c`**完整重驗。19:12:52先commit，19:15:39開始clean runner，
**161 Python＋32 minimal DOM＝193 checks全通過，無FAIL／skip**，Python JUnit336.68秒。
第一次memory ordinary/checkpoint＋九個selector拒絕的WIP11 PASS另存，不回填為此commit的執行。

| 同一次執行的分層 | 精確範圍 |
|---|---|
| PA-02子集 | 109個Python checks，涵蓋七來源、known literal hash、tool/source資料分層、scope／selector／record反例 |
| TC-01子集 | 43 Python＋32 DOM＝75，含所有16 instruction native投影的strict DTO消費與目前bridge全部schema |
| 額外回歸 | 25個TOOL-06 callback cases，不額外接受該runtime工作線的完整目標 |
| Prefix native | 18案（16 instruction＋2 structured），95 normal＋8受控SDK summary＝103 offline requests |
| 另外的retention native | 6個offline requests／四process與13次原reply可取／no-stream／非ACK觀察 |

兩個接受子集重疊16個Python cases，`109 + 75 - 16 + 25 = 193`。
它們與額外25都共用同次執行，不是多個model樣本；已計數native-driven offline requests為109，
不把HTTP／bridge SDK-I/O替身再當native。

新memory ordinary／checkpoint兩案都先經真signed人審producer，再由SDK實際呼叫
`read_work_memory(claim_id="claim-one")`，核exact note與必要原件、metadata/provenance和archive。
Initial request只含source metadata，沒有note正文；讀後正常回合、compaction後和下一個fresh runtime
都要求同一literal prefix。原記憶來源保持current，這**不代表已支援來源換版後無縫續行**。

所有16個instruction native案的真API projection仍逐回合交給bridge的strict DTO，
另有真API→bridge各schema與malformed反例、guard及DOM回歸。
原六prefix與前述withdrawal／失敗／consumer修補保持歷史；
該109／75接受宣告已隨#32固定`1c3db91`兩軸0P1/P2後合併，PA-13／PA-16仍只局部記憶接點。

## 第八個prefix與copy outcome完整重驗

2026-09-26，#42已採入[受控Team SOP copy-to-draft](sop-skill-copy-evaluation.md)。
因新增profile與公開outcome consumer，先把current PA-02／TC-01撤為partial；
舊cb4／0dac接受與原384筆歷史保持，不能把七profile的資格當作新八profile已驗。

固定source `68fbe3fd68b5dce7b584aded1f581a2e3294fd6a`先commit後，
05:58:52至06:05:44+08執行同一組預先collect的範圍：
238 Python＋33 minimal-DOM，271 PASS／0 FAIL／0 error／0 skip，Python407.83秒。
其中PA-02候選135、TC-01候選97 Python＋33 DOM＝130，共享19個Python case；
另25個tool-feedback回歸，所以135＋130−19＋25＝271，不加總為模型用量或獨立品質樣本。

範圍沿七prefix完整selectors，聯集最新版TC-01的原76 Python／33 DOM資格範圍，
再加入copy schema／authority opt-in／取消及未決outcome。20個native prefix案例包含
18 instruction與2 structured；109 normal＋9 SDK summary＝118 offline requests。
另有retention6、memory6、原配對case2及copy cancellation2，共可核native-driven134；
API／bridge I/O替身不是額外native requests。

原271結果可以支持TC-01的C候選：全部profile的實際API projection進strictbridge；
native保留四process／13次舊reply讀取、preview不成history、public工具配對；
新copy回條保存在archive／tool，公開turn只傳outcome。
Bridge positive經正式ToolSpec/current API/M3 copy再由SDK I/O替身保存，
nonnull outcome可讀，future stage／額外grant／false被拒且合法舊projection／reply保留。
真copy取消另如實保留ArchiveUnavailable，不外推可攜checkpoint或coldrestore。

### 完整目錄穩定性被Spec拒絕，不接受假綠

68f的271個PASS不是PA-02 full C：工具描述中仍帶每work不同的
`target_artifact_id`／`target_version_id`，四輪raw catalog hash不同。
原測試以刪除兩key的template hash比較，放寬了§5.1能力目錄的穩定範圍；
Standards列P2、Spec列P1並不建議接受PA-02（Spec另建議TC-01可接受）。
原checks數與原件保持，只將**未發布**的PA-02候選降為subset，不把它留成完整接受。

恢復完整raw-catalog比較後，真固定SDK在第二work重現拒絕；
hash-only原件記錄`21a503cb...`→`f696b61d...`，system prefix仍為原2645B／8f6151。
這是測試的嚴格catalog判準使合成provider拒絕，不宣稱production模型因此必然失敗。
產品最小修補只移除工具描述中的兩個target IDs，原server task、權限、目的地及receipt仍完整綁定；
工具本來只收`{}`。不再把其他欄位正規化掉，也不改scope或caller批准。

原嚴格RED與補充hash觀察各1 FAIL，修後ordinary／checkpoint兩案WIP為2 PASS／33.93秒；
此窄GREEN本身不能恢復full PA-02；下列新固定來源已完整重跑並經最終兩軸核對後，
才恢復兩個current C。仍不宣稱provider cache命中或加速比。

### 修後完整canonical目錄與同源資格

產品commit`6af3565`只刪Hosted工具描述的兩個target ID欄位，並保留原task及receipt綁定；
資料／報告後續commit`c8e0d00b7f2d2627786dfbb10b48982db61a638c`於06:47:11先完成，
06:49:04至06:55:54+08在乾淨該source執行相同預先collect集合：
**238 Python＋33 minimal-DOM＝271 PASS／0 FAIL／0 error／0 skip**，Python405.22秒。
PA-02仍是135、TC-01仍是97＋33＝130，共享19、另25feedback；與68f的node集合沒有縮小或替換。

Copy ordinary與checkpoint／cold restore共四work的完整canonical tool JSON都只有一個hash：
`9e7aa43c3782ab37d7f3d912342431cf1248529f1c05f727a9fcbac87bb5f74e`。
沒有刪任何目錄欄位做驗證，沒有template／exempt-fields例外，也不限制只比同一turn。
每work的target仍各自不同，receipt逐一對原signed admission的registration.profile；
target IDs只是從模型的穩定目錄移除，沒有改原current權限／M3／intent／回條或輸出功用。

20份prefix proof仍核109 normal＋9 summary、66個instruction正常stop return；
retention／memory／配對／copycancel合計known native-driven134，
13次舊reply保持可讀、無stream與未ACK。Fullcopyreceipt不擴為publicturn，
只公開typedoutcome；真正copytool pair的raw IDs／正文不出publicprogress。
這些數字都屬新c8e執行，不把初68f或更早WIP重新標綠。

新scope SHA`c3c23c169a3e16eceb64669d3e8199c825a0a78ae512b48873d9ae29941477a8`；
backend XML`2d1fbe5afc0ebb11a4e7cd7fb830e88fdb104adc82f89fa15cb09dc120204cdb`，
DOM XML`6acdc9422c1816ff9a8014813730aba24d93d8ba248668f4e4f9c109612066b4`，
完整safe handoff`e3268fe764ef937a357973d9157ef87770fe791f25a891d8a1134c513e4ac20c`。
原runner／collector與原qualified目錄保留，新資料在qualified-strict，
不覆寫初271、strict RED或曾被Spec拒絕的候選原payload。

修後c8e的source／evidence複核：Spec為0P1/0P2，明確建議接受該baseline的PA-02及TC-01 C；
Standards確認原catalog P2解除、嚴格範圍可支持currentC，但另列舊68f TC-01候選歸屬P2。
因此把**尚未發布的兩筆68f候選**都明示為historical/subset：保留135／130的實際PASS數、
原Spec對68f public-view範圍的建議及原payload，不繼承成c8e的current資格。
兩個新的strict full-target candidate與current baseline均指c8e。
最終文件／資料head`ef7a06ec8836af3d5b3a860266e7d8fefb5a88c6`經原兩Opus5.5各0P1/P2，
Standards明確解除historical P2，兩軸均建議恢復c8e的PA-02／TC-01 C；因此只在
實測`git:c8e0d00b7f2d2627786dfbb10b48982db61a638c`上恢復implemented/accepted，不拿文件head當受測來源。
這次狀態改動沒有再跑或改寫已完成結果，也不接受SK-10 M、Hosted或新scenario。
清理另存回條；原result中的`scratch_cleaned=false`是清理前的快照。

### 保留的開發失敗

- 初始copy兩案FAIL，是測試誤讀公開DTO不存在的完整`sop_copy_receipt`欄位；
  產品刻意只公開outcome，沒有為測試放寬公開資料。修正期待後另批2 native PASS。
- Consumer第一批18 PASS／13 setup ERROR：11個因命令漏M2_TEST_RUNTIME，
  2個因使用錯誤fixture holder。明設固定runtime、沿真authority持有的ArtifactService後，
  只重跑受阻13案，13 PASS；另補nonnull outcome／pending metadata兩案PASS。
- 原早期WIP沒有完整的safe native計數時保持未知，不回填零或套後來的commit／call數。

### 第八profile原件

68f scope SHA256 `0e947eef7daf369affdd35843adb747c504276ac9e2f262df3af459d1b7e9a11`，
backend XML `4bfea03464a35f90676598ccbe598154f7045aa4430d0da515acf8f263cf333f`，
DOM XML `ce3a90e690a55c6799965f79fbb97d496c6baee5973cfe348cfff69a9db597b8`。
各profile、retention、memory、copy outcome及hash-only mismatch都另存safe session原件；
固定SDK1.0.13／runtime1.0.83、offline_contract、local provider，不是native業務mode或Azure。

## Verified against

- 固定程式：`c5cc284` 初稿、`c51e730` parent 修補、`a9a3ce1` 整合；README 的完整命令／範圍。
- 收件核對：9個 Git blob OID／LF bytes／SHA、8份原 JUnit、4份 safe native JSON，以及新整合6 pins／2份 JUnit；協調者沒有為收件重跑該 native。
- 原 handoff／manifest SHA：`b69892fdd8e76ee24357c0847d67d9be1d255b6c680c833e949b6a18d8a10e5d`／`1d8fefd99c99a21d892ddfc3afa50aa427b68b6dc315de4c6d9f9571371b7f78`。
- 整合 handoff／manifest SHA：`1b774c081d43ad258306a241ba4bfa8e2cd0a062c8afdb621c436cf8133fb977`／`fb1360434d3dd92cfdf54739b57854f96964c2c7aa679cd378484631c4e0f0f1`。
- 新 native／unit JUnit SHA：`e5187293bb985aaf63d4efb59198582b37e507c1d36bd86fa05cef0e64ccdca2`／`cd54902a249ea4d58c4433a5dd13d66b340a1f93fe809a5b98b6316b88c656db`。
- 依賴／SDK／harness 未升版；無新雲端、付費推論或 IAM 操作。Source/code hash 不是部署 image 證明；working CRLF、Git LF 與 blob OID 不互換。
- 9/25增補：[三profile native矩陣](../tests/shared_context/test_prompt_prefix_stability_native.py)，沿真公開SDK `create_session/resume_session/send/get_status/stop` 接點觀察。已讀原`test_instruction_prompt`與approved-skill setup，重用原configured profiles、M3批准及既有權威路徑；未動pre-start owner的產品source。
- 第一WIP／固定矩陣JUnit SHA256：`47e6ae401ef46625bff95571b3fe4c65278463659b7478055e1d95883fed2ec4`／`a6dd328dbfccc5eacf6856c937a4856607a00c118db85989f9f5728ef8b03d80`。三份safe proof只留hash／counts／booleans，不保存system原文、SDK state或provider wire。
- Structured來源：[新增矩陣](../tests/shared_context/test_structured_prefix_stability_native.py)；已核`AssistantSession._run_turn`的預設／instruction分支、`m2_copilot`的published constant與SDK system設定、`WorkPolicyControl.attach/installed`的舊安裝拒絕，以及structured boundary的canonical tool-ID契約。沒有替不同policy鬆綁原worker規則。
- 37-case JUnit SHA256：`0a0916247b0d5d66c73a2cd3d5fb1969c65bf3ef391cdcfd16e1d40d83fc7d55`；五份safe native proof各綁同一source commit，完整pins另在帳冊。原135筆與normative specification／bindings不改。
- 修後最終42-case JUnit SHA256：`6f1eb6729dcf900073734ea8225094a348ce9b5dc21ea37892c3fc534c54a704`；十份safe proofs的normal／summary、catalog版本與原archive保持欄位已核，沒有保存system或SDK state原文。唯一被更正的舊資料是PR19尚未合併的37-case覆蓋宣告與摘要／限制／fingerprint；其checks／時間／來源不變，已合併135筆不動。
- 新62-case JUnit SHA256：`5e569a613f2bf6d6e7b92c52a1137a568f86439ef517753a0b97871c63b3eeba`；12份safe native proofs只含hash／counts／booleans與版本。既有測試檔重用原公開SDK與model seam，新reader來源用正式`UserTurnRead`解析；已核ordinary/checkpoint繼承與新CID空來源，原172筆不改。
- 新137-check合批的Python／DOM XML：`19c993d8a5b598c6a2358015e317efa17f10d959e64726376123562850e7c601`／`3cb212d9e807b1dc527d89dc5f5f360cb69f1cd9587b35518b96c5720460e88e`；safe handoff `2d7d6cdbd1acf8e09760c0a8b95e8108b00152b2250b47b08b6d901052c6d22f`。16份prefix proofs與另一retention proof各自核對，不保存private system／reasoning或provider wire。
- 新193-check的Python／DOM XML：`40b800c4a57ed722fa4ba2aca5b3009aeb0d24878d660401d013f6a2b456e922`／`5b04ee0f289532898270bb60b3fa920b40d000e5e8558782df2af06e7fc858db`；
  safe handoff `f4810fc34c9701a58d2da0f3605498807d600b47bcef4829c278d8b6f46c590b`，
  exact subset index `2c59af181bf39a385bb5d38f1f969609a695f099ceab7f1e4aa2e2c28d974772`。18份prefix及retention proof保留；原件沒有私人system／reasoning或provider wire。
