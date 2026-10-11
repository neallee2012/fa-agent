# M1 私人環境端到端驗收

日期：2026-09-14。狀態：**本輪定義的 M1 私人環境端到端 PASS，經有界續跑完成。**
目前版本 A9／B6；最後 A90／B54，共144筆sessions皆native idle。
A6／B5的HOME權限失敗、A7的純檔案結果，以及本輪相同設定重部署未產生新版本的失敗均保留。

使用者選擇並批准[Azure雲端備份設計與執行範圍](M1-CLOUD-BACKUP-DESIGN.md)。
專用Blob帳號、私網、API MI的container級讀寫角色與資料保護均已部署。
本機只保存receipt及證據；還原來源是Azure的固定Blob版本，不是本機備份或File Share。

依 [ADR-0003](adr/0003-hosted-environment-continuity.md) 與 [里程碑計畫](POC-ANALYSIS-AND-PLAN.md)，
驗收「這位 agent 的必要檔案、設定和browser profile能否跨停止、替代session與相容版本恢復」，
不要求原VM／PID不變。M1仍不含Copilot SDK對話／工作恢復或模型自主OS UI。

## 最新結果：真正 Hosted 與 Azure Blob 的完整 M1

| 項目 | 實際結果 |
|---|---|
| 時間 | 07:15:29–07:28:35 UTC，累計786.177秒，包含定位與續跑間隔；不是效能benchmark |
| 用量 | **26／40次Hosted invocations、7／10次stop、4／4個新sessions、51／80次session／version／inventory讀取**；12次真正headed GUI；模型與Memory updates均0 |
| 建置／部署 | API build **dsc**、Hosted build **dsb**，合計2／3次；API唯一新revision `ca-facontext-poc--m1backup`；A新增8、9，B新增6 |
| Hosted image | `sha256:1661873f09bc5b2d4f76dae06b37b398ef9186fb55d83ce6aa25f220fa556e52`，A8／B6／A9共用；沒有為續跑重新build |
| API image | `sha256:685b084f9517c3ad2b6a63e70dfd1d9aa4a79279e573cb8530acc28876ec4b08` |
| A/B私人環境 | 各自工作檔、工具設定、不同browser cookie及`zh-TW`偏好；warm與stop/resume均保留。agent effective UID10001，browser四個UID欄位全10001；沒有全程root或關閉sandbox |
| 真正工具／profile | Python 3.11.14、Chromium 152.0.7977.82、Openbox 3.6.1、Xvfb套件`2:21.1.7-3+deb12u13`；工具實際讀設定啟動，browser實際送回原cookie與語言 |
| 私人隔離 | B指定A的HOME被403拒絕；B自己的session沒有A的run。A/B交叉雲端讀取均為404，並核對`BACKUP_NOT_FOUND`，不只比HTTP status |
| 私網資料路徑 | API容器內DNS實測`stfam1backupdemo.blob.core.windows.net`→`192.168.96.69`，符合PE NIC及DNS record；Storage公網Disabled、SharedKey關閉，真實上傳及固定版本讀回成功 |
| 備份 | A **658450 bytes**、B **661419 bytes**，各180個regular files；展開檔案量各約19.46 MB，均低於限制；不封裝整個HOME、憑證或程序RAM |
| 替代session | A8原session先idle；新A8 session先回404，再從Azure備份還原。工作／設定hash、原cookie、語言及工具基線相符；重複restore被拒後再讀仍相符 |
| 相容版本 | A8→A9，唯一definition差異是明確`PYTHONUTF8=1`；image、CPU／memory、GUID、MI與endpoint不變。新A9 session先無資料，錯hash拒絕後正常還原，再stop/resume並觀察新程序 |
| 資料保護 | Blob versioning、Blob／container soft delete各7天、account `CanNotDelete` lock；API MI唯一新增assignment在專用container，只有Blob read/write，無新增delete |
| 收尾 | 新session數A+3、B+1，完整集合吻合；最多2個本輪active，最後全部144筆native idle。沒有刪除舊session、原始資料或雲端資源 |

### 保留失敗，而不是把同版本重部署算成版本更新

第一段`m1-e2e-20260914-1400`已完成21次呼叫，但相同image／definition再部署後，
CLI仍回A8；另以官方SDK一次原生versions listing確認最高確實是8，不是單純CLI顯示舊快取。
因此該段維持`FAILED_OR_BLOCKED`，含335個通過checks及1個失敗check，原report沒有改寫。
這是本次CLI／服務組合的實測，不把文件中的「每次deploy都會有新版本」當成實際證據。

續跑將A的Python UTF-8模式明確設為`PYTHONUTF8=1`，形成真正不同但工具基線相容的definition；
只新增A9及剩下的第4個session，再執行最後5次呼叫，106個checks通過。
`m1-e2e-20260914-1400-upgrade`保留來源report hash及**累計**額度，
狀態為`M1_END_TO_END_PASS_AFTER_CONTINUATION`。兩段沒有重跑seed、增加備份或重設60分鐘時限。
這只驗相容設定版本的恢復，**不是不同image／OS／Chromium大版本的任意升級**。

### 證據、費用與未涵蓋項目

兩段共**241份runtime原件、3821563 bytes**，另有128份部署／費用／離線操作證據；
各manifest本身另存。逐檔大小與SHA-256核對，保留執行時controller副本；
14份PNG中另目視核對了A9恢復後的實際headed Chromium畫面。
API與Hosted的21／24份建置staging檔案核對來源hash後才移除，原件、舊失敗及A7報告均保留。
去敏索引見[evidence summary](evidence/m1-e2e-20260914-1400.json)；raw evidence及`.azure`不隨Git公開。

07:31:27 UTC的RG本月延遲帳務為**US$13.45929129279025447**，
使用原identity／scope／`ClientType`，此次HTTP200、沒有429重試。
這不是即時最終帳單，也不能把與前次讀值的差額全算成本輪；US$100總額及本輪US$10預留不變。
Blob、Private Endpoint、DNS、ACR與其他保留資源仍會計費；沒有自動刪除live backups。

本機完整suite **365 tests PASS**，含真正Invocations HTTP adapter及Blob SDK的外部transport替身；
離線操作器也驗成功、前／中／後段失敗收尾與版本重用後續跑。它們不代替上表的真正Azure驗收。
未涵蓋：整個HOME、30天實際失效、真實網站登入續期、任意桌面app、SDK對話／業務工作恢復、
自主OS UI、四agent排程或10倍效能。下一階段是M2，不在本輪增加模型用量。
程式及文件隨本次M1交付保存；發布狀態以Git記錄為準，原始雲端證據仍不納入Git。

## 歷史 A7：root 只初始化專用目錄，再降權執行

2026-09-14 12:40 +08，使用者批准「root 只初始化專用目錄，立即降權至 UID10001」。
這是明確批准的啟動例外，**不是把 agent／Chromium 改成全程 root**。
本次只驗 A 的純檔案；B、舊 sessions／資料、API、RBAC、模型與基礎資源都不變。
正式 ownership 初始化契約仍 [NOT_CONFIRMED](fa-hosted-home-ownership-evidence.md)；
下列是此精確 image／session 的實測，不冒稱官方已保證所有版本與部署方式。

| 項目 | 實際結果 |
|---|---|
| 費用前置檢查 | 04:55:22 UTC 回報本月 **US$12.70575112512573927**；第一次429後等待22秒、依21秒retry-after只重試一次。帳務有延遲，不是即時餘額或本輪後總額 |
| 建置 | ACR **dsa，僅1次 build**，05:02:03–05:04:24 UTC；18個allowlist檔案、102455 bytes，未上傳 `.azure`、raw evidence、venv 或憑證 |
| 部署 | 只新增 `fa-context-a:7`；B維持5。A/B GUID、MI及endpoint未變；既有image passthrough另明列 `remoteBuild: false`，沒有第二次建置 |
| image | `sha256:11c617bddab90882b13d69e85665629c612dea63002e44328b19997a4095dac5` |
| 真正執行 | `m1-root-20260914-1240`，05:13:20–05:14:04 UTC，43.859秒；**2次invocations、2次stop、64個checks通過**，不是64輪測試 |
| 初始化 | SDK import前，UID0只建立 `/home/session/.fa-m1` 並設為10001:10001／0700；HOME自身仍0:0／0755，沒有遞迴chown或chmod HOME |
| agent程序 | seed與resume的 `Uid`／`Gid` 四欄全為10001，`Groups=[]`；`CapEff`／`CapPrm`／`CapAmb`全0。初始化後 `exec` agent，不留本程式的root helper |
| 寫入與讀回 | 兩次HTTP200；同一session／A7 binding。`work.txt`為52 bytes、10001:10001／0600；獨立計算的原檔hash與checkpoint hash均保持一致，read不再提供nonce |
| 真正恢復 | 先經native show確認idle，再read；`process_boot_marker`改變。bootstrap的`created`由true變false，既有專用目錄未被重建或改owner／mode |
| 收尾 | 最後再次native idle。完整清單A86→87、B53→53；最後**140筆全idle**，只新增指定A session。這些是快照，不是連續監控 |
| 明確未測 | GUI／browser／profile／工具設定、A/B私人讀取隔離、替代session、版本遷移、Copilot SDK工作恢復、自主OS UI；模型／Memory／Context API資料呼叫均0 |

新session為 `m1-root-20260914-1240-a`，native `version_indicator.agent_version`為`7`。
兩次程序marker分別為 `eb2c783d9ea4482294cfb3baaee907eb`／`f2ef059149dd446399815de6700246bd`。
原檔SHA-256是 `db32865f40f479a2fb8dced1b33d276f926cd89d6004aa946370691d0094a279`；
checkpoint SHA-256是 `a265e55595f863143fa684eddfcb0e9dfb306f44ac9a995f992103fc777b39a5`。
這證明**專用私人目錄**跨stop/resume可用，不是整個HOME可寫、共享filesystem mount或完整私人環境恢復。

### 啟動契約與邊界

[hosted_bootstrap.py](../src/fa-shared-context-poc/hosted_bootstrap.py)只接受已觀察的Linux UID0與`HOME=/home/session`。
使用directory fd／`O_NOFOLLOW`限制路徑，僅對**本次新建**的`.fa-m1`做`fchown`／`fchmod`；
既有目錄只驗owner／mode，不修補、不遞迴、不更動內容。接著清supplementary groups、
設定real/effective/saved GID與UID，再檢查程序身分與上述三種capabilities；任一步不符就停止，不以root啟動agent。
這不是把容器改成privileged，也未變更Azure RBAC。

沒有設定`no_new_privs`或清除capability bounding set，以免未經驗證地破壞既有Chromium sandbox機制；
**不宣稱禁止所有setuid程式或所有未來提權方式**。本輪未啟動browser，不能據此宣布新image的GUI／sandbox已驗收。
SDK或其他工具若需要`.fa-m1`以外的HOME路徑，須另列必要目錄與驗收，不擴大成整個HOME的chown。
Invocations固定1.1.0，Core補上exact pin2.1.0，與既有回報版本一致；未升級CLI或SDK版本來混入其他變因。

純檔案入口與原GUI入口分開：`seed_file`／`read_file`只處理`work.txt`與checkpoint，
拒絕截圖請求、跨scope checkpoint及read重送nonce；原`seed`／`read`的profile原子替換與非root限制保留。
相關68個本機測試通過，操作器另以離線成功及3種拒絕／收尾情境檢查；Windows的POSIX替身不是雲端權限證據。
本機Docker engine未啟動，沒有本機Linux容器驗收；上表才是真正Hosted的結果。

### 本輪額度與證據

限定1 build、A的1個新version／session、2次資料呼叫、2次stop、最多1個active；
build與session窗口各最多20分鐘，單次deploy command最多240秒。管理讀取上限32次、實際14次
（含事前及部署後6次version／inventory讀取）；每次idle等待最多30秒／10次查詢。
沒有重送資料操作、啟動`eval generate`、動B或刪除雲端資源。
操作器只對Windows本機report原子rename的短暫`winerror`5／32加有界、可見重試，不重試Azure副作用。

本機 `evidence\m1-root-20260914-1240` 的manifest列出**34份原件、272654 bytes**，
包含執行時操作器與共用helper副本；manifest本身另存且不計入這34份。
原report及manifest未改寫，A6／B5的失敗原件hash也再度核對不變。
去敏索引見 [root-init證據摘要](evidence/m1-root-20260914-1240.json)；
source/build、readbacks、費用及收尾資料在ignored `.azure\m1-root-20260914-1240-*`。
本次原始證據不隨Git提供；root-init驗收結束時程式及報告尚未commit／push，後續發布狀態以Git記錄為準。

## 歷史：A6／B5 停在 HOME 權限

使用者於 2026-09-14 10:50 +08 要求先修資料遺失風險，再進入下方有界雲端測試。
`Preferences` 已改為同目錄暫存檔完整寫入、flush/fsync、關閉後 `os.replace`；
write、flush、fsync、replace 任一步失敗都保留原檔並移除自己的暫存檔。
成功路徑也核對 fsync 在 replace 前、原設定在替換前未改變。沒有 runtime 提權。
這修的是單一設定檔替換前的資料遺失風險，不宣稱整個profile具備交易或斷電耐久保證。

| 項目 | 實際結果 |
|---|---|
| 費用前置檢查 | 02:55 UTC 首次429；等待超過服務要求的29秒後，只重試一次，02:58:11 回200；同一身份／scope，沒有更換 `ClientType` |
| 帳務讀值 | RG 本月已回報 **US$12.34470313309993287**；距US$100的算術差額約87.66，但帳務有延遲，不是即時餘額或測試後總額 |
| 建置 | ACR **ds9，一次實際 build**，03:04:38–03:06:40 UTC；17個allowlist source files，沒有上傳 `.azure`、raw evidence 或憑證 |
| 部署 | `fa-context-a:6`／`fa-context-b:5`，GUID／MI與原版本相同；沒有改API revision、RBAC、模型或其他基礎資源 |
| image | `sha256:6612fd0c94178e18771c67140ec1e865f18999c3dfabefc4211ed2019855234c` |
| 執行窗口 | `m1-20260914-1050`，03:16:36–03:17:14 UTC，38.64秒；**3次invocations**，沒有重送seed |
| A inspect | UID10001；`/home/session` owner UID0、mode `0o755`；agent/version/session與HTTP adapter binding相符 |
| B inspect | UID10001；同樣觀察到HOME owner UID0、mode `0o755`；binding相符。B的寫入未執行，不冒稱已觀察到B的寫入錯誤 |
| A seed | **HTTP503，`ENVIRONMENT_HOME_NOT_WRITABLE`，`PermissionError`／`errno:13`**；在建立 `/home/session/.fa-m1` 時被拒 |
| 尚未執行 | 其餘9次呼叫、私人檔案／profile／cookie保存、A/B讀取拒絕、warm／stop-resume驗收。實際browser啟動0次、PNG0份；模型／Memory／Context API資料呼叫0次 |
| 收尾 | 兩個新sessions均經native show確認idle；最終完整清單A86／B53，共139筆皆idle，僅增加指定兩筆，沒有額外增建 |

新 session IDs 是 `m1-20260914-1050-a`／`m1-20260914-1050-b`。
清單是取樣快照，不冒稱連續監控；agent version 的 `active` 也不代表 session compute 還在跑。
原先共享 Context 的 PASS 保留為 A5／B4 的歷史結果，不能拿來宣稱 A6／B5 已通過未執行的測項。

原始錯誤：

```json
{"error":{"code":"ENVIRONMENT_HOME_NOT_WRITABLE","message":"private HOME access failed; no privilege or temporary-directory fallback","operation":"seed","error_type":"PermissionError","errno":13,"diagnostic":"[Errno 13] Permission denied: '/home/session/.fa-m1'"}}
```

這否定了「只在 image 預建 app-owned `/home/session` 就能解決此環境」的候選做法，
不代表可以判定所有Hosted只能以root執行，也不是共享 Context／Azure Files 授權問題。
實測只能確定 runtime 的 owner／mode 與寫入拒絕，尚不能確定平台內部用哪種方式初始化／覆蓋 HOME。
下一步是確認官方支援的**非root可寫持久目錄／ownership設定**，或取得平台答覆；
不自動改root啟動／chmod平台HOME、不用 `/tmp` 假裝持久，也不拿剩下9次呼叫當新一輪重試額度。

### 原件與診斷修正

本機 `evidence\m1-20260914-1050` 的32份原件已核對manifest／SHA-256；沒有改寫原run report。
原runner遇到 azd 在JSON後追加 `ERROR: POST ... failed with HTTP 503: 503 Service Unavailable`，
先記成解析錯誤。後續只在本機重解析既有輸出，定位出上述實際503／errno13，沒有重新invoke。
解析器現只接受與HTTP錯誤狀態相符的這種尾訊，仍拒絕狀態不符、成功回應上的錯誤尾訊或額外JSON。
新的transport log寫入也避免Windows把CRLF擴成CRCRLF；舊檔保留，離線讀取時明確還原這層換行轉寫。

- 原report SHA-256：`de23cbb7699e207a9a8d42d654c937cd80063cb424791b67ae2ca63b93f0edc5`
- 原A seed stdout SHA-256：`1ee8f5806bcdb035c50af4fe72818db2a1aa95b1bc16afb8ef17a6a9e4e6bac2`
- 結構化離線診斷：`.azure\m1-20260914-1050-diagnosis.json`
- 費用、source manifest、build與版本readback：`.azure\m1-20260914-1050-*`

本機收尾記錄是 `.azure\m1-20260914-1050-local-closeout.json`：
17個container source files仍與build manifest逐byte相同，僅清除核對過的建置staging副本；32份原件保留。
一次性runner執行後另加了錯誤收證修正，該記錄分列執行前與目前SHA-256，不能把目前檔案冒充當時版本；沒有再次執行runner。

CLI準備時曾有不支援的`agent show --agent-name`及Dockerfile相對路徑／參數錯誤；
這些是本機CLI失敗，不算成功部署或Hosted呼叫。ACR前後run清單確認只有ds9是本輪新build。
沒有 `eval generate` 或模型評估，因本輪允許的模型呼叫數是0。本次Git交付包含程式、測試、報告與去敏catalog，不包含原始證據或`.azure`。

## 後續定位：只查文件與重讀原件

2026-09-14 12:06 +08，使用者批准先查官方契約、整理支援問題；本階段沒有新的Azure查詢、
build、deploy、invoke、檔案API操作、提權或支援案件送出，也沒有刷新資源／費用現況。
依診斷流程保留既有失敗作為訊號；不為重現而重跑已結束的runner。
離線解析原件不是新的Hosted重現，更不是修復驗收。

**官方查證結果：`NOT_CONFIRMED`。** [公開契約與source整理](fa-hosted-home-ownership-evidence.md)
確認HOME持久化／stop保存語意，但未找到明確的non-root ownership初始化控制項。
已查公開v1 schema、azd及相符SDK release；這不是「不支援non-root」的判定。
該查證階段先準備下方支援草稿，沒有追加部署；12:40 +08另獲root-init實驗批准，結果見上方。

### 可確認的最小問題

| 層次 | 已確認的事實 | 尚不能推出的結論 |
|---|---|---|
| A6／B5 Image宣告 | 當時Dockerfile 設 `HOME=/home/app`、預建 app-owned 0700 的 `/home/session`、最後 `USER 10001` | 這是source／build provenance，不是直接檢查image內目錄的結果；也不能推定任意HOME路徑都會持久化 |
| 部署宣告 | 既有A6／B5 definition readback的`environment_variables`只有本POC的`CONTEXT_*`設定，沒有HOME覆寫 | 不能因此判定平台或SDK在哪一個階段改變了HOME |
| 真正runtime | A6／B5都回 `HOME=/home/session`、effective UID10001、home owner UID0、0755；兩份回應的SDK／平台session binding一致 | 可以確認與image宣告有差異，但尚未分辨平台掛載、初始化／還原或SDK選路徑的機制 |
| SDK線索 | requirements固定 `azure-ai-agentserver-invocations==1.1.0`；兩份response header回報 `azure-ai-agentserver-core/2.1.0 (python/3.11)` | Header不是完整套件清單，也不能單憑版本字串判定SDK有bug |
| 首個失敗操作 | [environment.py](../src/fa-shared-context-poc/shared_context/environment.py) 的 `base.mkdir(mode=0o700, exist_ok=True)`，目標為 `$HOME/.fa-m1`，得到 `PermissionError`／errno13 | `ENVIRONMENT_HOME_NOT_WRITABLE`與HTTP503是本POC的錯誤映射，不是官方Foundry錯誤碼 |
| 尚未到達 | 該呼叫在工作檔、工具設定、profile及browser啟動前就失敗；environment分支不呼叫Context API／Memory，並略過舊HOME marker寫入 | 只能縮小本次失敗位置，不是宣稱已另行部署過不含SDK／GUI的最小image |

本機支援草稿：`.azure\m1-home-support-draft-20260914.json`，
狀態 `LOCAL_SUPPORT_DRAFT_NOT_SENT`。它核對4份既有原件及4個runtime source檔的hash，
保存失敗與inspect的時間、request/session correlation、版本及差異，**沒有覆寫原件**。
公開／Git文件只放上述摘要；完整草稿仍被Git忽略，送往Microsoft支援前須另確認接收管道及資料範圍。

查證當時缺少runtime的effective GID／supplementary groups、HOME對應的掛載／還原資訊、
SDK初始化前的HOME及owner觀察。這些是可供平台團隊判讀的待補資料，不先假設根因。
若需新增探針，只取相關目錄與程序的必要欄位，不傾印所有env、憑證或完整掛載清單。

### 下一個測試應先與GUI分開

現有 `seed`／`read` 會啟動browser；`include_screenshot=false`只關閉截圖回傳，**不會關閉browser**。
因此，後續另獲批准的初始化候選須先實作／離線驗證獨立的純檔案探針，
而不是把現有seed改名後宣稱沒有GUI。

最小目標是：非root寫入一份有界合成檔並回傳receipt → 指定session stop並確認idle →
同session read只讀原檔、核對原bytes／hash及新process boot marker → 最終stop。
這通常可縮為2次資料呼叫、2次stop；必要管理讀取與image／版本差異仍需另列上限、費用及批准。
這是12:06查證時的候選驗收形狀；後續12:40獲批准並實作、執行，上方另列結果，不能取代完整M1。

## 已實作的公開入口

沿用既有 `POST /invocations`／azd CLI，不另建服務或 CLI，也不改原 17-gate controller 的完成定義。
新增 opt-in `operation: environment`，目前有七個 action：

| action | 輸入／行為 | 不能用它證明什麼 |
|---|---|---|
| `inspect` | 回平台 agent／version／session binding、HOME 路徑、owner UID／mode、effective UID；不寫檔 | 可讀 metadata 不等於 HOME 可寫 |
| `seed` | 在 HOME 專用目錄建立合成工作檔、工具設定、profile；啟動真正 headed Chromium，完成 loopback fixture 後關閉，再寫入 checkpoint | 一次成功不等於跨 stop/resume 持久 |
| `read` | 必須提供操作者另存的 `checkpoint_sha256`；核對原檔、實際套用設定、使用原 profile 重新啟動 Chromium | 同一程序重讀不是 Hosted resume；不接受重新提供 nonce |
| `seed_file` | 只建立合成工作檔與file-only checkpoint，回傳程序及bootstrap資訊；不啟動GUI | 不驗profile、工具設定、SDK工作或整個HOME可寫 |
| `read_file` | 只按外部checkpoint hash讀回原工作檔；不收nonce、不重建內容 | 單次read仍須配合外部native idle及新程序證據才算resume |
| `backup` | 須唯一32位hex `backup_id`及checkpoint hash；先真正read／乾淨關閉browser，再封存、透過API create-only上傳，回固定Blob版本receipt | 這個action會啟動GUI；上傳成功本身不等於已驗恢復 |
| `restore` | 同一stable agent的新session，以`backup` receipt指定雲端版本；驗完整封存件後，建立新run並明確重綁checkpoint | 不覆寫既有run；還須另做`read`才能驗實際profile／工具；不是整個HOME或SDK工作恢復 |

程式：[environment.py](../src/fa-shared-context-poc/shared_context/environment.py)、
[GUI 接線](../src/fa-shared-context-poc/shared_context/gui.py)、
[公開入口測試](../tests/shared_context/test_environment.py)。

`target_agent_id` 要用平台 `FOUNDRY_AGENT_ID` 的 stable GUID，**不是** MI principal／client ID、
`agent-a` 角色名稱或使用者送來的 header。它必須與目前平台 binding 相同，否則在讀取私人 HOME 前拒絕。
`FOUNDRY_AGENT_NAME`、`FOUNDRY_AGENT_VERSION`、`FOUNDRY_AGENT_SESSION_ID` 也必須存在。
HTTP 入口對這個 operation 不執行舊的 HOME marker 寫入探針，避免授權前碰到私人目錄。

這是單一受控操作者、每位 agent 使用自己的 Hosted session 的合成 POC；
不是多人共用 session 的正式 per-user ACL，也不是用目錄名稱取代平台隔離。
未加入 `x-client-*` 身份信任、任意 path／URL／shell command、跨 agent export 或匿名入口。

### 保存清單與獨立核對

資料放在平台 `$HOME` 下 `.fa-m1/<agent-and-run-key>`，不落到 `/tmp` 充當持久資料。

| 保存內容 | 如何檢查 |
|---|---|
| `work.txt` | 合成 nonce 產生的固定 UTF-8 內容；操作者保留 seed receipt，read 比對 bytes／SHA-256 |
| `tool-settings.json` | 固定 `PYTHONUTF8=1` 契約；每次由設定檔啟動獨立 Python 子程序，檢查 `sys.flags.utf8_mode`，不是只重讀 JSON |
| `profile/` | seed 設 `intl.accept_languages=zh-TW`；loopback HTTP server 獨立觀察 Chromium 的 `Accept-Language` |
| browser 自己保存的 cookie | 只有 seed 的 loopback 首頁回 `Set-Cookie`；後續 read **不再設定 cookie**，只核對 browser 送回值的 hash |
| `checkpoint.json` | schema、agent/version/session binding、原工作／設定 hashes、Python／Chromium／Openbox／Xvfb 版本、cookie hash；read 的預期 hash 來自操作者另存 receipt |

Cookie 是無敏感資料的測試 marker，`Max-Age=86400`，不是登入狀態；本輪只測短時間恢復，
不以它宣稱完整 30 天 profile 保留、所有網站登入或 OS credential store 可恢復。

GUI 的 display、socket、下載與截圖仍用短的 owned `/tmp/fagui-*` 目錄，每次下載使用新的合成 nonce；
**profile 另在 HOME 保留**。read 只更新 profile 的暫存下載路徑，不重建語言偏好或 browser cookie。
正常結束先關閉 browser 視窗並等 Chromium 成功退出，才清理自己啟動的 GUI processes／暫存目錄。
HOME 的私人工作與 profile 不隨 GUI cleanup 刪除。

`seed` 不覆寫既有 run；即使前次只寫了一部分或回覆遺失，仍拒絕自動重送。
checkpoint 只在檔案、工具和 GUI 檢查成功後寫入、flush/fsync；中途失敗保留部分檔案供定位，
不聲稱整批寫入具有交易 rollback 或跨服務 exactly-once。

每個 response 都明示 `hosted_continuity_validated=false`：單次探針不能自行宣布跨生命週期通過，
整輪須由外部操作者核對原始 receipt、session 狀態和新程序 marker。

### 失敗不降級成成功

- HOME 缺失／不可寫或 runtime 為 root：`ENVIRONMENT_HOME_UNAVAILABLE`／`ENVIRONMENT_HOME_NOT_WRITABLE`／`ENVIRONMENT_RUNTIME_UNAVAILABLE`。
- 跨 agent：403 `PRIVATE_ENVIRONMENT_FORBIDDEN`；B 用自己的身份找 A 的 run 不應取得 A 的內容。
- 檔案／checkpoint／profile／cookie 或工具版本不符：409／502 明確錯誤，不重新 seed、不改用 Memory 或原請求補答案。
- 額外欄位、任意路徑或命令：422；symlink／junction 與非 regular files 拒絕。
- 同一程序同時只允許一個 environment operation；GUI 仍沿用既有單一操作鎖。
- replacement session 或 agent version 不同：`ENVIRONMENT_RESTORE_REQUIRED`，不擅自把同一份目錄當作已批准 migration。

## 歷史：build-time ownership 候選（A6／B5）

既有真正 Hosted A5／B4 曾看到 `$HOME=/home/session`、root:root、0755，UID10001 無法寫入。
候選 Dockerfile 在 **image build 時**預建 `/home/session`，owner 為 UID10001／app，mode0700；
runtime 仍 `USER 10001`，沒有 sudo、啟動後 chmod 平台目錄或提升權限。

本輪已驗出這個做法未解決權限：官方 HOME 持久契約**不等於**承諾平台一定沿用 image 的初始 owner。
若平台以自己的 volume／還原資料覆蓋它，仍可能失敗；尤其不能用新 session 的結果推論舊 session ownership 已修復。
本輪用了兩個新sessions，inspect後實際seed被拒即停止，沒有連續rebuild猜測。

## 離線結果與尚未完成

A6／B5階段Shared Context suite：**176 tests PASS**。當時新增覆蓋公開 dispatch／真實 Invocations HTTP adapter、
檔案保存／receipt pinning、A/B 邊界、root／缺 binding／不可寫、損壞、不可重送、
profile 不重建、cookie／語言／版本觀察、乾淨關閉及並行拒絕；原 API／Memory／GUI 路徑回歸通過。
診斷修正後controller整套 **22 tests／23 subtests PASS**，包括失敗尾訊解析與CRLF原樣保存。

本機是 Windows：檔案測試使用 repo-owned fixture；Linux UID／平台環境與 GUI／工具程序用外部邊界替身，
loopback HTTP fixture 則有真正本機 HTTP 測試。**沒有把這些替身、本機檔案或 HTTP cookie jar 當成真正 Chromium／Hosted 恢復證據。**
沒有本機Docker GUI；各輪真正雲端結果分列於上方，不用本機結果抵銷歷史HOME寫入失敗。

上方最新M1已補齊真正非root的profile／工具、A/B隔離、雲端backup／restore、替代session及相容版本恢復。
沒有實際等待30天失效，也沒有SDK對話checkpoint、程序RAM／socket保存或任意桌面app恢復。
M2的自主滑鼠／鍵盤／視窗決策尚未實作，不能因固定GUI探針通過而標PASS。

## 歷史 A6／B5 批准範圍：不是新的重試配額

以下保留原定上限。費用先取得上述有延遲的12.3447美元讀值，再按使用者10:50 +08指示執行；
沒有以舊的US$1.3187、idle或零replicas推定零費用。尚未執行的測項維持NOT_RUN。

| 項目 | 本輪批准上限／前置條件 |
|---|---|
| 環境 | 原 subscription／West US 3／`rg-faexample`／Non-VNet Foundry；不加外部 VM、File Share、Storage、RBAC、模型或 API revision |
| 建置 | 現有 Basic ACR **1 次 build**；執行前保存本地 source hashes，成功後固定唯一 image digest；不自動重建 |
| Agent versions | `fa-context-a`／`fa-context-b` 各新增 **1 個版本**，共用該 digest；服務回傳的 version 必須明確記錄，不猜編號或覆蓋舊版本 |
| Sessions | 每個新版本明確建立 **1 個 session，共 2 個**；舊 137 筆是歷史基準，先刷新完整 inventory，不能假設目前仍 idle |
| 執行規格 | 每個 2 CPU／4 GiB、idle timeout300；最多2 active；CLI 保持已驗版本，不為新的120秒最低值升級 extension |
| 呼叫 | **最多12次 Hosted invocations**，其中最多6次 headed GUI；0 SDK 模型／Memory／Context API 資料呼叫 |
| 時間 | build最多20分鐘、部署最多20分鐘、session驗收最多20分鐘；到限停止新操作並收尾，不無限等待／重試 |
| 唯讀觀察 | session／version／inventory管理讀取最多60次；每個 idle 等待最多30秒、最多10次查詢 |
| 停止／清理 | 最多4次指定新 session 的 `stop`（2次恢復測試、2次最終收尾），確認 native idle；不刪 session、私人資料、舊資源或 versions |
| 證據 | 原始 response、errors、PNG、source/image/binding、前中後完整 inventory、stop/idle/new-process 與 hashes 存 ignored evidence；去敏後另出報告 |

12 次 invocation 的固定順序：

**實際只送出前三次，第三次seed失敗；第4–12次未送出。**

1. A inspect；B inspect。
2. A seed；B 指定 A 的 GUID read，應403；B 用自己的 GUID 配 A receipt read，應404。
3. B seed（同 run ID、不同合成 nonce）；A read；B read。
4. A stop 並確認 idle → A read；B stop 並確認 idle → B read。
5. A 重複 seed，應409；A 用錯誤 checkpoint hash read，應409。

這是12次呼叫，不是12輪；A/B 皆需比對 **seed→warm read→stop/resume read**：
原工作／設定／cookie hashes 不變，agent/version/session binding 相同，而 `process_boot_marker` 在 resume 後改變。
若未觀察到新程序，resume 那項為未驗證，不用 PID 或同 session ID 硬算 PASS。
還要核對 response 的 `runtime.session_id`、`outcome.binding.session_id` 與 native session ID 三者相同。
最終 stop 只針對本輪兩個新 IDs；若無法確認 idle，收尾為 BLOCKED，不宣稱已停止計費。

後續仍沿用已修正的 **`--agent-endpoint`＋指定 session ID** invoke，
不使用 service-name invoke／`--new-session`；native create／show／stop 則使用明確 agent 名稱。
任何意外增建、第三個 active、HOME 權限失敗、cookie／檔案不符或費用風險都停止此輪；
保留原件後定位，不自動加版本／加額度。操作者需將這一輪編排與原 17-gate 功能 controller 分開記錄。

### Payload 範例

下例 GUID 只是合成示範，必須換成此次批准版本的真實 `FOUNDRY_AGENT_ID`；
實際 session ID 由 Invocations URL query／CLI 的 `--session-id` 路由，不放在 body 冒充平台 binding。

```json
{"operation":"environment","action":"inspect","target_agent_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","run_id":"m1-example"}
```

```json
{"operation":"environment","action":"seed","target_agent_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","run_id":"m1-example","nonce":"amber-v1","include_screenshot":true}
```

```json
{"operation":"environment","action":"read","target_agent_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","run_id":"m1-example","checkpoint_sha256":"<64 lowercase hex characters from separately retained seed receipt>","include_screenshot":true}
```

雲端備份也沿用同一入口。下例`backup_id`僅為格式示範，實際必須使用未用過的隨機ID，
並保存回應中的完整`outcome.backup`；不要重送已成功或結果不確定的PUT。

```json
{"operation":"environment","action":"backup","target_agent_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","run_id":"m1-example","backup_id":"11111111111111111111111111111111","checkpoint_sha256":"<retained checkpoint SHA-256>"}
```

還原時以相同stable agent、相同`run_id`，在明確新session送
`{"operation":"environment","action":"restore","target_agent_id":"<stable GUID>","run_id":"m1-example","backup":{...}}`；
`backup`必須是原receipt的`backup_id`、`owner_oid`、`sha256`、`bytes`、`version_id`及`etag`完整物件。
成功後另外保存新的`checkpoint_sha256`並做`read`，不能繼續使用來源session的checkpoint hash。
這些是請求格式說明，不是額外雲端測試或費用授權。

## 官方契約依據

2026-09-14 重新查閱：

- [Hosted runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)：
  HOME 與 SIGTERM／checkpoint 責任、平台 agent/session env、`x-agent-user-id` 分區；
  `x-client-*` 是 caller 資料，不是授權來源。本 POC 不採用範例的 cwd fallback。
- [Manage hosted sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)：
  HOME跨idle恢復、session／conversation分離、30天無活動後清除、Invocations的query-string routing。
  更新agent需新版本；session是否可沿用須分辨explicit version pin與平台路由。
  後續查到azd的opt-in、best-effort同session carry-over線索，見[官方查證](fa-hosted-home-ownership-evidence.md)；
  本輪未啟用，不把它當成ownership修復或產品所需的完整migration契約。
- [Hosted overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)：
  compute 可重建，不能將外部共享狀態恢復等同私人 OS 環境恢復。

Session Files API 是另外的檔案存取介面，不是多 agent 共用 filesystem mount；
本輪沒有執行其 upload/download 或用它跳過 HOME ownership 的真實驗證。
