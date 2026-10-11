# 交給接手 agent 的提示

**歷史 POC 交接材料：** 新工作階段改從 [AGENTS](../AGENTS.md) 與 [CURRENT-STATE](status/CURRENT-STATE.md) 開始。本檔保留原批次提示、結果及限制；下文「現行／目前／已批准」只適用其原時間與範圍，不是新的排程或操作授權。

**M4程式基準已發布：`1ca1c762050cdcafb51484b5155ad5b65503cd65`，origin同名分支read-back一致。**
使用者於2026-09-15 21:18 +08明確授權；本輪固定33檔M4程式、測試、部署定義與相關文件，排除六個未接線v1.2 core／test檔、私人原件及`.azure`。
完整候選tree的1114項離線回歸通過；初次驗證副本的Windows長路徑／runtime定位問題已只調整測試執行環境解決，原日誌保留，沒有改runtime程式。
M4基準門檻已解除，可從原件／來源保存增量接v1.2；相關能力仍未啟用，沒有新的Azure／模型額度。
以下19:29及更早的「尚待批准」保留為歷史，不再是目前授權狀態。

**2026-09-15 19:29 +08後續順序：先固定M4基準，再實作原件／來源保存並增量接v1.2。**
使用者同意[研究方案](FA-DATA-STORE-RESEARCH.md)，目前沿用Table＋Blob，不建立新資料庫或照搬每agent SQLite。
本地r3收尾804份原件、最後gate的48份source hashes皆核對相符；最新摘要為[r3去敏catalog](evidence/m4-20260915-r3-closeout.json)。
M4執行已停止，PARTIAL（四PASS、一incomplete）；原五案驗收`FAIL`保留，checkpoint根因及最後用量仍未知。
尚無新的M4 commit／push授權，Git code SHA仍待固定；不得混進未獲發布授權的v1.2 core或原始執行資料。
新原件／claim／投影屬後續隔離切片，不回改M4 fixture／門檻、不新增本批雲端試跑。

**2026-09-15 18:57 +08接續點：M4原生 r3 已停止，不再等待操作器。**
使用者已批准US$100不變、相關操作累計上限三倍；API `m4-3`／A17／B11，最後Store `poc-m4-context-03`。
r2分頁修復後前三案通過、第四案因完整policy marker縮寫而FAIL，已停止並保存原件，第五案未跑。
格式／批准來源檢查已補上；18:47完整原生分頁核對158筆皆idle，再啟動`.azure\m4-final-operator`原五案。
帳本為`m4-context-01-r3-ledger.json`，累計為`m4-expanded-ledger.json`；原首批與r2兩階段失敗原件不改。
前四案collector及人工來源核對通過；第五案checkpoint前回`incomplete`、未resume，SDK trace尚未收齊，根因未證實。
已cleanup且原生完整inventory160筆全idle；收尾在ignored `m4-r3-closeout.json`。models36只到第五案之前，不是最終總數。
新sessions／Stores／Memory updates／API revisions已達累計上限，不得重啟driver、重設counter或重送unknown。詳見[M4最新狀態](M4-CONTEXT-POC.md)。
v1.2三個隔離core已整合、187項相關離線測試通過；native admission/send/execute仍停用，沒有consumer或API／Hosted接線。

**2026-09-15 16:54 +08歷史：M4首批已按品質失敗停止並收尾，未PASS。** API `m4-1`／A16／B10；
B私人正向控制、A跨私人403通過，`short-cold`因A偏好未被擷取保留及模型無支持填答而FAIL，其餘四案未測。
最後A100／B56共156筆全idle；模型4／60、invokes16／100、stops3／5、兩個新sessions、active182.311秒。
先讀[M4結果](M4-CONTEXT-POC.md)及[去敏catalog](evidence/m4-20260915-closeout.json)。原ledger的`aborted=true`保留；
`closed=false`表示五案成功流程未完成，不代表仍有工作在跑；實際停止收尾另見ignored `m4-context-01-closeout.json`。
不得用餘額重跑、改fixture或換run ID。M4 source／測試／報告尚待批准commit／push固定code基準；
目前已發布`dcaa7192bd6715921870195fbf75914bbcb59ba5`只有v1.2文件，不含本次M4接線。

**以下帶「實作中／尚未上雲」的M4接續點保留當日早先準備歷史，不覆蓋上述收尾。**

新增行為開發必讀[行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)，按[BR-01–BR-06](POC-ANALYSIS-AND-PLAN.md#m4-之後的行為開發切片)接在本批 M4 之後。F15–F17 已補文件契約：重試終局性、實際內容綁定、必要材料保護；無安全前提不啟用相應能力，F18–F20／HOST 待辦仍開放。BR-01／02 先固定原子儲存／材料保護與基本 wake，BR-03 只驗 B07-C1，BR-06 才驗完整四槽／Teams；離線結果不是 Hosted 保證。原[設計提案](FA-AGENT-BEHAVIOR-DESIGN.md)只作研究／取捨背景，不再使用其舊 M2-A／M2-B 排程。保留 M2 UI 缺口、M3 版本／批准與 unknown；不新增本批 M4 gate、改 fixture／oracle／counter 或從另一 session 平行執行。新增切片的部署／模型／權限／通知仍須另有範圍。

**以下 M3 段落為已發布的收尾及歷史證據，不是目前 M4 的操作指令；M4 接續點另列於後。**

**M3收尾狀態：核心端到端閉環已完成並停止該批付費工作。** 先讀[M3結果、真成果與CLI入口](M3-ARTIFACTS-POC.md)及[去敏catalog](evidence/m3-20260915-0510.json)。真人已下載兩版成果，批准SOP v1、拒絕v2、批准v3；兩個真consumer分別產生V1及V3。`core_e2e_pass=true`，原v1格式FAIL與retired unknown仍使`full_scenario_pass=false`，不能改寫成全部歷史測項通過。

最終部署為API `ca-facontext-poc--m3-2`、A15／B9；原成果兩版使用A14，修復SOP與consumers使用A15獨立binding。
`m3-artifacts-01-sop-repair`目前生效版本為`57f3ccdd2ff512b90ded888d8d9a2ec8`，
SHA=`8ca595e5201e7069aee68127fa2d451b18a40aaf75ad645ef985f6f590fa0d2c`。
最後consumer=`m3-artifacts-01-repair-consumer-v3`，version=`30edfe7794fbfba0b76a3c52f8200261`，
SHA=`7d75dcc02c9f7c7ac096bfdcc2eead54b254ea92124170a0ed819f9669af32ea`。
Native SDK trace已下載並核對，真正`read_context`工具結果包含已批准v3；不是只讀模型自述。

**不要再喚醒Hosted或重跑人類決定。** 唯一ledger為`.azure\m3-artifacts-01-ledger.json`：
native72／80、SDK jobs8／8、stops7／7、new sessions2／2、native downloads2／24、
active1471.358／3600秒；模型counter59／80。餘下21次模型不是新的job／stop授權。
05:09:58 UTC的完整inventory為A99／B55共154筆全idle，M3原session及替代session均idle。
只讀session inventory不會喚醒；`--cloud api-probe`仍經Hosted，不能當成不耗active的查詢。
既有成果可用原人類CLI查看／下載；新交辦、修訂、M4或UI對照須另列有界雲端範圍。

**保留原件與邊界。** 原`deliverable-v1`格式失敗、原`sop-v1` unknown、SOP v3之後的Foundry HTTP500讀取，
以及真人登入／讀取失敗都保留；v3僅離線匯入已完成原件並補讀版本，沒有重跑SDK job。
原plan、stop amendment、repair amendment、A14/A15 definitions及各binding不能覆寫或重設。
完整hash manifest有684份原件，留在`.azure\m3-final-evidence-manifest.json`；沒有原件只可核對repo摘要。
最後費用讀值為05:10:16 UTC的US$18.1654，較早US$27.0793較高且差異原因未知；
M3預留US$10仍包含在總US$100內，精確成本未結清，保留資源仍計費，未批准整組刪除。
M2仍部分通過、160次額度耗盡；`M2-UI-01`／`M2-UI-02`及模型對照延後。
使用者於2026-09-15 13:27 +08授權commit／push M2／M3基準，再準備M4的小型離線驗收；發布版本以Git記錄為準。
新的雲端工作仍須先列明並批准次數、版本、費用與停止條件，不沿用已耗盡的M3批次。

**M4接續點：** 基準`f097fa687b0e331879e1d3c8d3b5d259b96b46e1`已push並核對遠端指標。
`controller\m4_context_eval.py`提供五案離線觀測檢查，已import共用`shared_context.m4_fixtures`，34項測試通過；M4檔案尚未commit。
完整fixture SHA為`8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090`；
oracle只供評分，不傳給模型。檢查器的PASS不是真雲端證據，也不會給10倍PASS。
M4 API／Hosted專用接線已交付，原生操作器仍在整合；不可用既有M3部署或counter直接跑這組案例。
M2／M3／M4 runtime與fixture合併回歸702項通過，包含原actual SDK的本機MockTransport案例及fresh-process匯入隔離。
本機Windows須用短暫存路徑；父端首次選的`.azure\m4-parent-baseline-1`使PNG路徑達283字元、2項失敗，
改成repo內短路徑後同一完整範圍通過，程式與assertion未改。兩份日誌都在ignored `.azure`，不外推為自主UI通過。
[首批範圍](POC-ANALYSIS-AND-PLAN.md#m4-第一切片離線準備完成雲端尚未執行)已列明；
使用者已回覆「批准，按此範圍與上限執行 M4」；`.azure\m4-cloud-approval.json`於05:57:44 UTC保存批准，
綁定凍結提案SHA `1912290f71458b8071110d61541b9158331f87d45c87e4931904cfed48aaf0ea`。
`.azure\m4-cloud-proposal.json`保留提出時狀態，不因批准而改寫。API／Hosted已交付，native collector／operator仍在整合；
部署前必須完成operator驗證、scope preview及immutable definition核對，不能用本機結果宣稱雲端通過。
唯讀preflight已確認A15／B9、API `m3-2`、全idle inventory、原Store及既有模型，新M4 Store不存在；
尚未建立新Memory Store、build／deploy、喚醒Hosted或使用模型。06:09:32 UTC延遲費用US$18.5746，
仍以較早較高的US$27.0793作保守預算參照。

API／Memory／部署helper的其餘範圍122項通過。後續runtime另補checkpoint單檔snapshot，
以受影響的147項範圍重驗通過；部署helper新增fresh-account guard後23項通過，這些含先前已跑項目，不直接相加。
`evidence.checkpoint_snapshot`供waiting時native下載，不依賴尚不存在的`SDK outcome`；
context時間是Hosted handler monotonic timer實測，不冒稱SDK-native latency。相關source／log hashes見ignored
`.azure\m4-context-01-local-validation.json`及`m4-context-01-local-validation-supplement.json`。
原操作器工作程序已為`cancelled`，未留下完整交付；父端已接手現有五個檔案，不再等待或另派同一工作。
已修正真實`container_configuration.image`欄位、100筆session邊界、首筆current-process context選取、
checkpoint單檔下載與SDK工具結果原文核對，以及cost重試／分頁契約。API文字回應明確拒絕，不補解析成成功。
操作器與部署helper合跑49項、另7個subtests通過；checkpoint採真runtime serializer加fake檔案邊界，
SDK envelope對照既有M3原件，沒有重跑M3。完整fake流程仍只報`SYNTHETIC_ONLY`，不是Hosted驗收。
日誌為ignored `.azure\m4-parent-operator-takeover-tests.log`。仍未建立`code-ready`；部署前尚須凍結來源、
刷新已過期的身份／成本／完整idle inventory，並核對scope preview及實際新definition。

<details>
<summary>M3執行歷程：以下「目前／續接點」均是歷史快照，請勿重跑</summary>

**目前先推M3，讀[執行順序與契約](POC-ANALYSIS-AND-PLAN.md)。** 使用者於2026-09-14 21:32 +08同意保留`M2-UI-01`／`M2-UI-02`，將UI模型對照延後；M2仍為部分通過。先做不依賴UI的成果檔案、不可變版本與人工批准，保留revision6成果、舊失敗及全部counter receipts。重新處理UI時才讀[M2結果](M2-ASSISTANT-POC.md)；原160次模型額度已用完。M2收尾時A13／B9、五個自有sessions已idle，另有未歸屬active session未處理；這是歷史快照，操作雲端前重讀。目前M2尚未commit／push。

**M3本機切片已通過：** 成果版本、人工JWT、Blob adapter、HTTP／CLI、Hosted的`artifact_task`與
獨立M3 model route／counter已整合。`m3_client`／`records`保持Hosted輕量相依，不載入server Table／人類登入；
舊`storage.Record`import保留。不要再等已交付的模組或重做相同離線測試。
先讀[實作契約及已批准雲端範圍](../src/fa-shared-context-poc/README.md#m3-成果與批准)，
不要從零重建或覆寫M2原件。人類CLI是檢視／下載／批准入口，自然語言工作走原Hosted入口新增的成果欄位。
admin OID已由唯讀Graph核對為`00000000-0000-0000-0000-00000000001b`，不是尚未查證；
2026-09-15 08:35 +08已批准`.azure\m3-cloud-plan.json`原範圍。
新client／scope／僅admin Principal consent、M3 private container及其API MI read/write已設定；
API `m3-1` HTTPS健康檢查200、A14 active，B仍9。兩個ACR builds已用完，僅剩第二個API revision供policy root存在後配置清單。
真正成果v1／v2已保存，真人CLI登入／下載已驗，SOP批准尚未驗收；未commit／push。
最多80模型attempts、8 jobs、80 invokes、A一版及一個新session，M3預留US$10仍在原US$100內。
最新01:59 UTC帳務回US$17.7911，低於昨日14:59 UTC的US$27.0793；原因未查明，預算guard保守取較高值。
不是退款、即時硬上限或最終帳單；兩份原件均保留。不要重跑已接受的cloud script或另建client／image。

**第一個Hosted成果已存在，但格式FAIL：** `deliverable-v1`已完成142 bytes、固定version
`a3e569b2d33b88a70975ac9c62b8fcdd`；內容表達TOTAL為40，未照prompt輸出`TOTAL=40`。
不要重送start或上傳來改原版；v2已用新work修訂完成，156 bytes並包含`TOTAL=41`。
操作器已自動stop且native idle，當時counts為6 invokes／1 job／1 stop／1 session。
使用者已另批准stop上限4→5，其他上限不變；原plan不改，追加授權在`.azure\m3-stop-amendment.json`。
薄操作器已完成原件接續及單一追加授權；v2工具序列誤判另已修復，43項本機測試通過。
v1／v2均已離線匯入，真API讀回native Blob版本／ETag／hash／來源，修訂後v1不變。
不可手改ledger計數、改寫原件或重送已完成工作湊PASS。

**當前阻擋：SOP漏建metadata被錯記為未知提交。** 真SDK trace保存在
`evidence\m3-artifacts-01-operator\sop-v1-sdk-outcome.json`；submit的pending記錄保留，
`0090-invoke.response.json`只讀確認SOP root HTTP404 `NOT_FOUND`。
本機`m2_assistant.py`已補`M3_ARTIFACT_CREATE_REQUIRED`前置防護與明確順序提示，
M2／M3助理29項測試通過；真正未知create／upload仍不得重送。修復後另獲追加授權，ACR `dsq`成功，A15已部署。
第二個API revision亦未執行，不得對不存在的SOP先配置policy清單。
第三次stop後`0093-verify-idle.stdout.txt`確認自有session idle；目前24／80模型attempts、
24／80 invokes、3／8 jobs、3／5 stops、1／24 native downloads、1／1新session、active 524.778秒。
原build／A版本／新session授權用完後已另取得修復批次批准：
累計build上限3、A新增版本上限2、新session上限2、stop上限7；其餘限制不變。
`.azure\m3-repair-amendment.json`保存使用者原句並綁定提案hash；新
`.azure\m3-artifacts-01-repair-a-definition.json`已證明A15 active且只改image，B仍9。
替代`m3-artifacts-01-a-repair`尚未建立；先完成operator append式amendment／binding，再以唯一spare
建立隔離的`m3-artifacts-01-sop-repair`提案，後續四個尚未執行jobs仍計入原8次上限。
原run／counter／plan SHA／A14 definition及所有unknown不可重設或改寫。
真人CLI已完成登入、show及兩版Blob下載，逐byte與Hosted原件相同；摘要在
`.azure\m3-human-deliverables-verified.json`。首讀`M3_REQUEST_UNKNOWN`根因未確認，原失敗保留。
僅GET重試，未自動批准任何SOP；不能把模型自稱approved當作身份驗證。
內建terminal canvas未渲染，獨立PowerShell入口已可執行；後續SOP決定需用原CLI，不用只允許GET的診斷wrapper。

**最新續接點：** repair amendment已套用，A15替代session已建立，新SOP v1已完成；
API已更新至`m3-2`，只配置`m3-artifacts-01-sop-repair`。
SOP version=`22b40dca4920d6ec640fbe5abb52cc4d`，SHA=`47c682d192a8364284b82b48ad638cbac195102189c4b9f80218fd904a1fe8d8`。
未批准context／effective404／agent403已通過；目前model30／80、native35／80、jobs4／8、stop4／7、
session2／2、native downloads1／24、active977.169秒。新舊session均idle，還沒有任何人類SOP決定。
自動開窗不可靠，Computer Use也未允許PowerShell；只請使用者自行開PowerShell7，指令必須放fenced code block，
用`Join-Path`避免複製時遺失反斜線。入口為`.azure\m3-human-decision.ps1 -Step approve-v1`，不代填stdin。
人工預覽曾遇`M3_REQUEST_UNKNOWN`；ACA冷啟動與30.937秒health提供線索，但舊transport類型未保存。
本機`m3_cli.py`已補只有唯讀timeout重試一次與安全phase診斷，19項CLI測試通過；批准仍不重送，不需重build。
人類批准成功後才刷新成本並啟動`repair-consumer-v1`／`repair-sop-v2`，不可重跑已完成的`repair-sop-v1`。

</details>

**現行交接：先讀 [ADR-0003](adr/0003-hosted-environment-continuity.md)、[POC 分析與後續計畫](POC-ANALYSIS-AND-PLAN.md) 與 [POC 結果](../POC-RESULTS.md)。** 使用者已確認 Hosted 優先，以獨立、可持續恢復的環境／工作取代同一台 VM 限制；不預設新增外部 VM。[ADR-0002](adr/0002-shared-context-without-file-share.md) 的共享狀態／Memory／Context API 方向保留，SMB 已停止。

**2026-09-14 收尾：** 共享 Context 的 17 項真實 Hosted 功能已通過，舊 VNet Foundry 已刪除。
API poc4、A5／B4 的原始證據、Memory 遺失 opaque 值的限制及費用未知均見結果報告。
service-name azd 曾產生多餘 active sessions，已 stop；[controller](../tests/shared_controller/README.md)
現要求 `--endpoint-a/b`，使用 endpoint 模式與明確 session，已確認唯讀回歸不增加 session。
不要重做已完成的付費測試、重送已接受的 native updates 或把「功能 PASS」改寫成預算／歷史 session
上限也已通過。保留資源仍可能計費；未批准整組刪除，程式與文件的發布狀態以 Git 記錄為準。

## 接下來做什麼

依計畫的M0–M5：M1已完成，M2文字閉環通過但UI待驗，M3核心閉環完成；M4首批因品質FAIL停止收尾。
先取得批准固定M4 API／Hosted程式與失敗報告的Git基準，再銜接BR-01；正式接線前不混入另一工作樹的offline_contract。
M4重試／修復驗收、新試用或UI模型對照須另定範圍，不沿用本批剩餘計數。歷史SMB與完整Web工作台不在本批範圍。

每一站須交付 [計畫第 5 節](POC-ANALYSIS-AND-PLAN.md) 的使用者驗收情境、入口及證據：M1 先驗底層電腦，M2 才有真正自然語言／跨對話接續，並包含自主 OS UI；M5 接最小 Teams 入口。不能以模型呼叫固定 GUI probe 代替逐步看畫面／決策／操作。

`HostedClient.dispatch` 現已接上 `operation: assistant` 的真實SDK工具循環；舊探針仍保留。原HOME canary的`unavailable`、`.fa-m1`私人目錄成功、M1恢復及M2 checkpoint結果分開記錄，原G4歷史不改寫。

已通過的 GUI gate 只執行 xdotool 視窗聚焦、鍵盤輸入與 Tab／Return，加上截圖／下載核對；它不是滑鼠／拖曳、任意桌面應用或模型自主 UI 操作的證據。

原規劃階段僅批准方案整理；後來已另行授權並執行M1／M2。該授權不是無限額度；新的執行仍須明列精確範圍、費用與停止條件，新增權限、刪除或對外發布各自確認。

原始 `evidence/` 與 `.azure/` 被 gitignore 排除，不保證在接手工作樹存在。先取得受控原件／hash 及可分享範圍；沒有原件時，只能說核對了 repo 摘要與程式，不聲稱重新驗證雲端結果。不要直接將整包原始輸出或 credentials 加入 Git。

<details>
<summary>歷史 SMB 交接（只供重現，不是新的執行授權）</summary>

## 歷史 SMB 交接（只供重現）

請先讀 `adr/0001-persistent-agent-computer.md`、`requirements.md` 與 `specification.md`，再完整閱讀同目錄的 `FA-Hosted-Sandbox-POC-Spec.md`。設計原則約束產品方向；POC 文件只管理其能力探針的執行與驗收，不能覆蓋原則。

**2026-09-13 的設計原則：FA 是代管的 OpenClaw，每 agent 長期跑在同一台固定 VM 上，有長期記憶、完整持久環境與工作，並以快 10 倍為目標。** 可關機／重啟，只在故障或明確維護時移轉；不能將持久 `$HOME`、穩定 session ID 或閒置後新 compute 還原解讀成符合。

官方 Hosted 標準 idle／resume 會釋放並重新配置 compute，與固定 VM 原則有契約落差。先處理 runtime 採用前提；若使用者仍明確決定要做獨立的 Hosted 能力探索，才按下述範圍提出執行計畫。不要以保活流量繞過生命週期，也不要自行改成外部 VM。

本次只確認：兩個不同 Hosted sessions 能否真正 OS 掛載同一 Azure Files share、idle／resume 後恢復共享存取，以及同一 sandbox 能否執行本地 desktop／headed browser 並透過工具取畫面／輸入。人類接手與整套 FA 不在範圍內；Copilot SDK 的 G4 是需先批准的可選增補。

G0–G4 全部通過也不驗收固定 VM、完整環境與工作持久性、長期記憶、context 品質或 10 倍效能；結果需分開記錄「能力測項結果」及「產品原則相容性」，不能宣稱原生路線因此獲准採用。

先盤點使用者指定的可寫專案／工作目錄與 Azure 測試環境，提出精確資源、權限、費用及停止條件。本文不是部署、支出、授權或刪除資源的同意；取得明確批准後才操作。不要自行選 subscription 或切換成外部 VM／遠端 browser。

研究已查到公開契約的界線，不必重做整輪競品研究。實作遇到掛載／GUI 支援疑問時，讀 `fa-hosted-mount-desktop-evidence.md`；涉及原始需求或 SDK 時再讀 `fa-platform-evidence.md`；只有需要解釋 Grok Bot 差異才讀 `grok-box-evidence.md`。

實測必須在真正 Hosted runtime。REST 上傳、私有 `$HOME`、本地 Docker、一般 Azure VM 或遠端 CDP 的成功不能代替目標能力。保留每次精確配置與失敗證據，不繞過平台隔離限制；只修正相關失敗再重測。

依規格交付實作、固定版本、G0–G3 證據、明確 PASS／PARTIAL／FAIL／BLOCKED 報告、費用及剩餘資源清單。全程以繁體中文溝通，不自動 commit／push、發送訊息或發布任何東西。

</details>
