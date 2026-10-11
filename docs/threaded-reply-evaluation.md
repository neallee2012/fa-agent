# Threaded replies：原對話、回覆與回條關聯

日期：2026-09-25。狀態：**SP-06 的文字／authenticated-pull 路徑有新增本機證據；不是完整條目或雲端驗收。** 本切片補可重現的公開介面測試，沒有修改產品行為，也沒有為製造 RED 而破壞既有程式。

#15固定head `a2711f0`經Opus5.5 Standards／Spec各0 P1／0 P2後，已合併為 `8368fbc3510bb2e1fc4e0b001f74f4aa0ea3ff28`。與#14目錄接續的合併前snapshot另有13-case整合結果，見[技能接續報告](runtime-skills-evaluation.md#目錄接續修後與合併)；不加回下方原批次。

## 範圍與既有接點

目標是 [SP-06](grok-aligned-target-state.md#41-骨架-21-項與基礎結構) 的原始 request／conversation／reply 關聯及重送邊界，沿既有已確認的入口，不新增 controller、conversation authority 或 receipt 格式：

| 公開接點 | 本次確認 |
|---|---|
| `POST /work-policy/v1/submit` | A、B各自接單，先完成B再完成A仍不串線；相同原請求回到原binding，換conversation重用原ID則拒絕 |
| Conversation／turn reply GET | 原request ID、binding、turn與完整答案相符；A追問只恢復A歷史，B及舊A的答案bytes不變 |
| `GET /behavior/v1/bindings/{binding_id}/jobs/{job_id}` | binding與job錯配被拒；不能用一條路徑冒充另一筆工作狀態 |
| Delivery pull／acknowledge | A舊回條延遲到A新回合完成後，仍只履行舊A；套到B被拒，重複正確回條回原結果，不產生另一份交付 |
| 當前讀權 | Agent身分不能以人類查收入口取得該對話答案；沒有為測試增設身份／grant或匿名fallback |

這裡「交錯」是兩段獨立對話已接單、結果共存並反序完成，不代表同時有兩個活躍compute槽。Job `succeeded`／`result_forwarded` 和收件責任分開；只讀答案不被當作已確認收件，更不當作真人已讀。

## 固定測試與實際觀察

新增 `test_conversation_routing.py` 的三案使用真FA HTTP／權威服務與Table transport fixture，只有SDK I/O是替身，禁止啟動runtime程序。`test_conversation_routing_native.py` 另用固定SDK及真正runtime，以明確offline provider測一個跨對話／fresh-local-root恢復案例。

Native case先送B、再送A、再追問A。每次實際provider request都核所需context與另一對話的marker不在；SDK `send`原文、兩段不同SID、A追問沿用原SID及原答案bytes均有斷言。重送B原submit後沒有第四個模型request。

SDK為 `github-copilot-sdk==1.0.13`；後加的觀察直接包住產品原本使用的公開 `get_status`，沒有額外RPC，三次讀回均為runtime `1.0.83`。最後native批次為3個offline requests、3次正常 `stop()` 返回、0次 `force_stop()`；沒有從這份紀錄推算OS全機網路流量或未記錄的exit code。

| 批次 | 結果 | JUnit秒／被測基準 |
|---|---|---|
| 既有two-turn入口 | 1 passed | 7.865；`ee80e2e` |
| 新interleaved WIP | 1 passed | 5.711；完整WIP基準未知 |
| Receipt初次expectation | 1 failed | 6.102；產品已正確403拒絕，測試誤期待內部error code |
| 對齊公開error contract | 1 passed | 7.188；不改產品或403拒絕條件 |
| Original-request replay WIP | 1 passed | 6.420 |
| 初次native WIP | 1 passed、3 offline requests | 13.385 |
| 固定四案合批 | 4 passed：3個I/O替身案＋1個native案 | 22.474；`0f055f6dde022b954f4de81a16840df3a1ba3449` |
| 增加公開runtime版本觀察後 | 另1 native passed、3 offline requests | 13.128；`1252ea4d559470b193e519b92f625681904db964` |

每列是不同執行；後面重新涵蓋先前案例，不能相加作獨立樣本數或品質成功率。最後兩列都在對應commit建立後、乾淨工作樹上執行，早期WIP則不倒填後來的commit。

## 失敗與環境界線

首次既有入口檢查在collection前缺FastAPI，沒有跑模型或runtime；之後只依既有 `tests/shared_context/requirements.txt` 還原本worktree的venv，未改manifest。首次native preflight因錯假設目錄只有一個`.node`檔而停止：目錄還有MSAL addon。改核指定的 `runtime.node` 與EXE原固定hash後才執行，沒有下載或修改共用runtime。

Receipt案的首個FAIL不是產品授權缺陷。`behavior_api._install_error_handlers`刻意把內部403投影成公開 `BEHAVIOR_ACCESS_DENIED`；測試原先期待 `INBOX_ACK_DENIED`。修正只對齊公開接點，仍要求403、`automatic_retry=false`、其他工作未履行、原答案不變。原FAIL XML保留，不回填成產品RED→GREEN修復。

## 未涵蓋

沒有真模型語意品質、Azure Table／Blob或已部署Hosted觀察；沒有聲稱client receipt等於human-read。未在所有profile／Teams／群聊或所有executor completion callback競態重跑這個矩陣，也沒有以未啟用通道當N/A完成整個SP-06或其他Target State。

目前只把SP-06從待盤點移到局部實作／證據；完整76目標／12情境的接受條件保持。既有產品已滿足本矩陣，所以不新增一個重複的路由層或為了宣稱修補而改動核心。

## Verified against

- 產品基準：`ee80e2e01d630d65eae93fc37370a43398d31490`；本切片只有新增兩個測試及相應資料／文件。已讀 `behavior_instruction` 的conversation／archive/reply契約、`behavior_work_policy.read_reply/conversation`、`behavior_api` 的path/error投影、`behavior_runtime.fetch/acknowledge/job_status`及 `behavior_inbox.acknowledge`。
- 三案測試：[公開API／receipt](../tests/shared_context/test_conversation_routing.py)；固定SDK案例：[native routing](../tests/shared_context/test_conversation_routing_native.py)。Public SDK觀察包住原 `send/get_status/stop/force_stop`，不換自己的session loop。
- 固定四案JUnit SHA256：`6b3ba7c87997ffedbcb639d7fed6a1c8405d863ec0562d3df3a5804ccc711fe3`；版本觀察native JUnit：`f407609926a51d49417599f51dad74afb6877064fb2c813690bb266d4e69842c`；safe native proof：`87cb3cd08e4c9eb70607338eab65ec87acd6fadf61ba228f700d5fee88ad14a9`。
- 所有中間JUnit、版本、日期與raw／Git換行口徑另存受控handoff；不提交SDK state、provider wire、DB或credentials。Tokenizer沿既有唯讀public encoding副本，沒有重新取件Azure blob。

## 2026-09-25：Unicode JSONL 測試替身修正

後續在`291dd4b`上做原文／對話窄盤點時，發現`test_instruction_flow.sdk_io`的resume
使用`str.splitlines()`，會把JSON字串裡合法的U+0085／U+2028／U+2029也當事件分行。
測試因此出現`JSONDecodeError`，把本可恢復的合成回合記成failed；這是**測試替身缺口**，
不是新發現的產品conversation／checkpoint缺陷。

修正只將該替身的JSONL解析接到既有`behavior_checkpoint_privacy.jsonl_lines`，
以LF／CRLF為分隔，不新增解析器，也不忽略內部空記錄。產品parser本來就使用這個helper，
`src`／controller／權限／prefix／codec／全域帳冊均未修改。

新增三個回歸以相同原文建立兩條conversation，先完成B再完成A，然後只追問A；
原文及回答都包含對應Unicode分隔字、空白、CRLF與文字形式的role／approval marker。
斷言完整內容仍是user資料、A不讀到B回覆、SDK SID不串線、舊reply bytes不變、沒有ACK或額外model request。

| 批次 | 實際結果 | 基準 |
|---|---|---|
| 新三案／舊替身 | 3 failed，11.65s | `291dd4b`＋新test overlay；三案均在follow-up錯誤failed |
| 修後窄合批 | 9 passed／0 failed／0 skipped，30.94s | 已先提交且乾淨的`492097be1b74bc2acd050b6050fb027968f78f7d` |

九案是原三個routing、新三個Unicode、一個原two-turn及兩個legacy/versioned exact-input案例；
SDK I/O均為替身，沒有啟動真runtime或真模型。不是九段真人對話，也不新增PA-07／SP-06完整接受。
原批次的FAIL、內容、hash與資格不回填；這是提高後續eval替身可信度，不是降低原文標準。

在session獨立探針另有真SDK／offline-provider對照：含U+2028的相同原文在兩conversation不串線，
`CopilotSession.send`與已保存`user.message`逐字相同。Provider-facing訊息卻會由SDK加入時間
及移除尾端空白，因此初探針「整個provider message必須等於raw input」的FAIL是錯誤邊界期待，
不是原件遺失；未把更正探針後的PASS回填成該批成功。這個對照只作原因核對，不併入九案。

### Verified against

- 實際呼叫：`test_instruction_flow.sdk_io`的resume JSONL parser；
  正式`behavior_checkpoint_privacy.jsonl_lines`只分LF／CRLF，保留字串裡的Unicode。
- 新測項：`test_conversation_routing.test_identical_unicode_originals_and_replies_survive_isolated_follow_up`，
  參數為U+0085／U+2028／U+2029；RED與fixed時此檔SHA-256皆為
  `7cb8f548559b6cef337d663de77f25f9076025879b50682cfb8921a70a1744b5`。
- RED原件`sdk-io-unicode-jsonl-red-20260925.xml` SHA-256
  `135a3d6509ff07b40becdf513c36d9d72c39d89b48dfeffdb5038389fdc41dc7`；
  fixed原件`sdk-io-unicode-jsonl-fixed-20260925.xml` SHA-256
  `5963ae7168b8c8d67aff95c20e230e6026ffa8ecd1901b308cb59a1fe65f0b18`。
- Windows／Python3.12.10／pytest9.0.2。本次test-only修補不宣稱已重跑舊109、真Hosted、
  其他通道或裝置脈絡；收尾與合併仍沿既有Opus5.5雙軸及正常gates。

## 2026-09-26：剩餘bridge回放與checkpoint assertion

恢復後分別重現夜間留下的兩個P3。公開bridge的`real_interface` SDK-I/O替身在cold follow-up使用
`raw.splitlines()`，U+2028進入合法JSON字串後被錯拆，表面成為`WORKER_START_UNKNOWN`，
實際例外在test helper的`json.loads`。另一個checkpoint案例已成功產生合規snapshot、
也通過private marker移除斷言，卻在測試自己用`splitlines()`讀回合法公開答案時失敗。
兩個接點都改用既有`jsonl_lines`，**沒有改產品parser或放寬私有內容、空record拒絕**。

既有兩輪bridge流程保留原LF輸入，另加入U+0085／U+2028／U+2029。
各案仍核同SID、原文逐字恢復、切換模型、十次唯讀reply不改inventory，以及explicit client-received
ACK只送一次。每個參數另存不同proof檔，避免覆蓋；SDK-I/O仍是替身，不是真runtime／browser。
Checkpoint原有private system／reasoning filter案例保留原space答案，另驗三種Unicode答案；
輸入JSON用`ensure_ascii=False`，不以預先escaped字串避開問題。

| 批次 | 實際結果與來源 |
|---|---|
| Bridge U+2028原替身 | 1 failed／21.077秒；03e3d45＋新test WIP，錯在回放JSON解析 |
| Bridge窄修LF／U+2028 | 2 passed／17.461秒；WIP |
| Checkpoint原讀回斷言 | 1 failed／2.101秒；snapshot成功後的test parser失敗 |
| 固定`ba318a2`兩個檔案 | 29 passed／1 failed／75.250秒，先commit才跑；全部8個bridge cases通過 |
| 未修改03e3d45的git archive | 同一privacy-test stub缺`assignment.task`重現1 failed／3.403秒 |
| 固定`0dea17e`受影響checkpoint檔 | 22 passed／0 failed／0 skipped，5.995秒，先commit才跑 |

固定合批中唯一FAIL與Unicode不同：舊credential-before-upload案例用空assignment stub，
在`AssistantSession` initializer就缺task，未到原privacy拒絕斷言。
只有在本worktree專用base archive重現後才歸為原fixture缺件；最小補上非instruction task，
不改產品constructor、不增加fallback，不放寬「私有material不能HTTP upload」。

最後的22不是把前次29/1回填全綠；bridge來源與`ba318a2`逐blob相同，未重跑其8案。
四個bridge proof分別保存原文／同SID及readonly／explicit-ACK不變式。
這些測試可靠度修補沒有改363-run ledger或任何完整接受，也不把先前有缺口的替身結果重新標成成功。

### Verified against

- 受測source：`ba318a21063cc0d4ab9669af3dee76a304a27538`（兩檔29/1）；
  `0dea17ee6e5c97c9160dc32d5ca9a82ac5f7c726`（checkpoint22）。
  直接接點為`test_web_test_console_frozen_interface.real_interface`與
  `test_sdk_checkpoint_boundaries.test_new_filesystem_snapshot_preserves_existing_private_event_filter`，
  共同沿原LF／CRLF-only helper，不忽略空或無效record。
- 合批XML SHA256 `c51d6da15acb98387afcf3c5190dcfaa8a3798af06dfe6caaa922b99faf5111f`；
  unchanged-base stub重現 `40006edbd8f509b4184cdd2ed957775ef47001b2619ad2581493e67a2ad18df4`；
  最後22-case `318d697c9b5fc3b12ed01dd89d6ce41256580e3af17babe5dcbc700759ddca3e`。
  最初RED／early GREEN當時未凍結工作檔hash，baseline維持WIP，不補造精確source。
- Windows／Python3.12.10／pytest9.0.2；只真API/bridge接線加SDK-I/O替身、snapshot與冷匯入。
  新native/model/cloud/browser及舊193／109等完整資格矩陣均NOT_RUN，產品／controller／判準未改。

## 2026-09-26：PA-07 缺省與不可信metadata的文字入口資格

本切片只補[PA-07](grok-aligned-target-state.md#52-條目)現行文字入口：
metadata沒給時不生造欄位，body／非權威header不覆蓋原身份、conversation或收件路由。
使用者裝置／必要位置的可信producer與必要模型可見脈絡仍未定義；沒有增加schema或裝置能力，
也沒有把「無欄」改說成模型必定回答unknown。完整PA-07仍只subset，global接受狀態不變。
Session盤點把此缺口稱為G3，並非Target State的正式子項，也不是既有Hosted G3 gate。

沿目前已接通的public API／bridge，先核實現行返回，再寫資格期待：

| 接點／新案例 | 現行契約與核對範圍 |
|---|---|
| Signed API，3案 | 額外`location` body為422；可信human攜合成metadata headers仍沿原身份／route／CID／turn，原文含role/location/device字樣仍是user文字；agent即使header自稱原human仍403 |
| Bridge body，2案 | `location`／`device_id`額外欄位各為403 `LOCAL_REQUEST_DENIED`，不是API的422；拒絕不改原draft／journal／backend inventory，也不發backend request、SDK或model |
| Bridge缺省／header，2案 | 正常無metadata及合成header兩個變體都保持原command、原文與訊息／reply識別；backend submit的同名header值不等於caller canary，純GET回覆不變成ACK |

Header只用合成actor/principal、conversation、recipient、device、location、
`Forwarded`／`X-Forwarded-For`與`User-Agent` canary；沒有取得真IP、UA、定位或身份資料。
API正例核SDK-I/O接點收到的原字串、`role=user`與固定system prefix；
bridge沿既有`real_interface`的Hosted run I/O替身，不宣稱它觀察了真SDK provider整包。
原文中的合成location/device字樣刻意保留，不能把資料中出現關鍵字當作可信metadata。
拒絕的主要證據是registration／store未變，以及bridge沒有backend request。
零SDK／model呼叫是manual-step fixture中的觀察：沒有自動`start_work`，
只有顯式`controller.step`／`complete`才會進I/O，不當作另一條自動啟動路徑的獨立驗收。
Header canary斷言只核submit的同名值，未覆蓋progress GET全header或改名／字串串接轉送。
目前adapter對所有backend請求重建headers是下列程式接點的唯讀證據，不冒充該完整矩陣已測。

| 固定批次 | 結果 |
|---|---|
| `6436959fc28795cf6ce8de3af8e5e2bb4118a9ff`，先commit兩個test檔再跑5 selectors／7 cases | **7 passed／0 failed／0 skipped**，pytest 11.59秒；2026-09-26T02:26:51–02:27:15+08:00（外層執行窗口） |

這是既有行為資格，沒有強造RED，首批沒有assertion失敗。六欄前件是session-only
`test_pa07_same_text_probe.py::test_client_cannot_invent_device_or_recipient_metadata`：
`device_id`／`machine`／`reply_to`／`recipient_id`／`channel`／`actor_id`被API拒絕。
它在產品基準`291dd4b6105fd5d5b9622477bf6b75223dccde4b`的
`pa07-original-input-subset-20260925.xml`中PASS；該原件SHA256為
`329f8681cf42b87e510fd1323e86141b5bb17acf5691ed94a9efef2ff60affea`。
原20合批保留18 repo PASS、此session案PASS及Unicode替身案FAIL，不回填成全綠。
真SDK Unicode對照與上兩節PR37／38 helper批次各自保持，不重跑或提高接受。
沒有改`test_web_test_console_frozen_interface.py`／`test_sdk_checkpoint_boundaries.py`、
`test_instruction_flow` helper、Memory／no-SDK或產品程式，
也沒有修改363-run ledger、CURRENT、TargetState或Shared Context README。

### Verified against

- Base：`4ee75f0d13d09d7c90fd7917f7a847115f918faf`。受測source tree：
  `256102f3db081a65d03797b3e2c1efc3cdf26385`；上述source提交後工作樹乾淨才執行。
  新案例位於[API instruction](../tests/shared_context/test_instruction_prompt.py)與
  [bridge projection](../tests/shared_context/test_web_test_console_prompt_assembly.py)；沿既有fixtures，不另造產品入口。
- 已讀產品接點：`shared_context/behavior_auth.py:64–99`核verified principal，
  `shared_context/behavior_work_policy_api.py:119–133`接原submit、`shared_context/behavior_wire.py:35–43`拒絕extra；
  `web_test_console/bridge.py:845–857`要求body只有content/model，
  `web_test_console/adapter.py:352–361`的`send`重建backend headers。
  路徑均相對`src/fa-shared-context-poc`，不是新加的產品行為。
- 執行環境：Windows／Python3.12.10／pytest9.0.2；SDK套件1.0.13用於既有I/O替身，
  不代表本批啟動runtime1.0.83。後續僅修本小節的來源／措辭，不變更受測test bytes或重跑批次。
- Session原件：`pa07-g3-scope-20260926.json`保留執行前NOT_RUN；
  `pa07-g3-fixed-source-20260926.xml` SHA256
  `d2d6b21628f2b46c6a5c7b990618e9db9a6484d506fae905fc4c0c780064fe7e`；
  execution receipt SHA256 `f9fc8961c8a2ef2c72ef88c879e46e3322f6fe072ab322ab5bab39d769d0ee46`。
  原件只在受控session保存，不把raw fixture、token或私有prompt加入Git。
- 新native runtime、真模型、Hosted／Azure／M365、實體browser、human語意品質、
  舊109及全PA-07 C資格均**NOT_RUN**。這七案不構成七個品質樣本，亦不啟用未宣告的裝置／通道能力。

## 2026-09-26：TC-02 final-save 回應遺失的限定資格

本節只補[TC-02](grok-aligned-target-state.md#6-逐字稿壓縮與記憶沉澱的完整目標)
「同一事件重新讀取不新增效果／不以新答案覆蓋原歷史」的一個保存接點。
沿原 signed instruction／controller／SID＋ticket、`save-text-reply`、原status／reply與
ControlStore receipt，**沒有修改產品、once／unknown、shared helper或stop/gateway路徑**。
這是既有行為資格，沒有強造RED，兩個首批均通過；不是全TC-02接受。

### 固定來源與原始結果

先提交source/tests **`f3374347579ec463f1d6567c673a2975bcfc0dc0`**，
tree `507b888b576a68cf321a8e75d555c864332fcd19`，base
`50cfcb664cb41b174f4f0fcd6fe233c20c7a83b0`，再在clean source分開執行：

| 批次／2026-09-26 UTC+08:00 | 結果與真正層級 |
|---|---|
| SDK-I/O doubles，08:34:08–08:34:32 | **5 passed／0 failed／0 skipped**，pytest14.84s；真signed API/store/controller/model gateway，SDK外部I/O替身 |
| 唯一fixed SDK控制A，08:36:42–08:37:00 | **1 passed／0 failed／0 skipped**，pytest10.75s；SDK1.0.13/runtime1.0.83、scripted offline provider，業務模式仍`offline_contract` |

沒有把double的停止當真process證據。Native A直接包住原SDK的send/status/stop，官方stop
正常返回後核原owned process已退出；實際**1 client／1 SDK send／1 provider request／1 stop，
force-stop0**。Windows exit code如實為1，與原save command的pid／exit code相同且
`graceful=true`；不是Azure/Hosted停止或供應商idle證據。

### A／B／C 的保存與重播邊界

| 案例 | 故障／重播位置及結果 |
|---|---|
| A：保存後回應遺失 | Transport先讓真API完整執行、讀完200 response，才throw回應遺失。Client只查**原assignment SID**的status；`ready_to_finalize`／原result digest相符。只有1次原save attempt與API dispatch，原reply/result/archive／control receipt及成本不變，沒有第二SDK send/model或checkpoint寫入 |
| A：cancelled變體（double） | 原產品stop接點完成後、save dispatch前走真signed cancel，不手造stop/result。保存與ACK前後work都維持cancelled；取消不代表沒有可讀答案或不得確認收到，也不因ACK變成功 |
| B：save送出前斷線（double） | Transport不把save交給API；只有1次client attempt、0次save API dispatch。原SDK/model已各走1次，外層依原契約`WORKER_START_UNKNOWN`，原status仍running、job未終局；原final receipt/reply/result/archive row不存在，公開reply404、不開follow-up。Provider刻意不報usage，原pending cost與consumed model intent保留，不退款或換ID補送 |
| C：原confirmed save重放（double） | 在原save已commit、原response尚未返回時，先重放同ID同body，再以同ID改text或turn。Exact replay200；異text409、異turn403。原receipt及三份immutable row hash不變；不是用content-length錯誤代替身分／digest拒絕 |

C刻意有**原save＋exact replay＋異文/metadata replay**三個API呼叫；SafeJSON的
`original_save_api_dispatches=1`只計原SDK client送出的save，其餘在`replays`另列，
不能把三個HTTP請求寫成只有一個。拒絕沒有新增保存回條或覆寫原件。

所有案例都實際進controller一次，並觀察API scheduling callback；它只觀察排程接點，
不冒充完整production auto-start。SDK-I/O計數與native計數分開。Checkpoint所需的Blob
put是原執行的合法保存：本批各案在save之前已put1次，之後不增加；不是宣稱整個turn零寫入。
M3 create/upload/reconcile、Blob put與business effect routes各自計數，spies都呼原實作。
沒有fake saved row、手工填`SdkStop`、改ControlRequest或繞過`verify_output`。

### 四種公開說法不互換

| 說法 | 本批可對到的事實 |
|---|---|
| save accepted | 原交易create了reply/result/archive與control receipt；A的client雖遺失回應，材料仍在 |
| receipt readable | 原SID的status與`BehaviorStore.reconcile`核回原ControlRequest／receipt；不是第二save |
| client_received | 純GET reply／status仍未ACK；只有原exchange／binding／digest的明確ACK回條才成立 |
| human_read | **NOT_ESTABLISHED**；程式測試、GET或client_received都不能宣稱真人已讀 |

### Verified against

- 產品接點（相對`src/fa-shared-context-poc`）：`shared_context/behavior_assistant_hosted.py:193–212`
  的`save_text_reply/_save_text`只讀原status處理未知；`behavior_instruction.py:220–318`
  保存create-only結果；`behavior_store.py:429–523`核原receipt；`behavior_assistant_api.py:189–197,359–391`
  沿原ticket/command；`behavior_assistant.py:1151–1232`由原executor完成／驗原結果。
  以上後四個basename亦位於`shared_context`，未改程式。
- 新案例：[save composition](../tests/shared_context/test_instruction_save_receipts.py)及
  [單一native A](../tests/shared_context/test_instruction_save_receipts_native.py)。
  Reuse原`instruction_inventory/sdk_io`，沒有改parent helper、SOP copy或TC-05 probe檔。
- Windows／Python3.12.10／pytest9.0.2。Session保存各批完整argv、日期、source/tree、log、
  JUnit及SafeJSON；只輸出必要ID/hash/counter/狀態，不記token、challenge、provider本文或private SDK state。
  Double JUnit SHA256 `0b9540909f0a0b21ba5658f0ea608aa3da58759bce728eeb38165f9927313873`；
  native JUnit `2bd607c42441fb4b5b0f07119b22af7552fd18d9d838440b519c77cf437e1787`；
  native SafeJSON `0d41533dc616e1f629ea724f469a3e4a0c476ec26ea7a95e3200f740c1d59f05`。
- 不重跑舊TC01/271／SOP23，不回填原批次或修改ledger/CURRENT/Target/Shared README。
  Cloud/paid/IAM/browser/build、SDK/harness更新、任意外部exactly-once與完整TC-02均未驗／未做。
