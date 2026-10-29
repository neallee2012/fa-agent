# POC 結果分析與 Hosted 優先後續計畫

**2026-09-26接續順序：** 先完成可用部署，再逐步補齊完整Target State。
部署前只處理防資料損失、重送或明顯執行錯誤的必要缺口，沿現有入口跑受影響案例與最短真實流程；
不以完整plugin探針、全面指標、證據分級或額外流程阻擋。未實作／未驗能力保留原狀態。
本次必要雲端操作採共用累計USD150，不另開一份額度；Git交付依使用者要求經Opus5.5修清P1/P2後正常合併。
規範見[部署前最小原則](FA-AGENT-BEHAVIOR-SPEC.md#2026-09-21-部署前原則只保留最小必要機制)，
實際完成與下一步以[CURRENT-STATE](status/CURRENT-STATE.md)為入口，以下各日期保留歷史。

**2026-09-20當前執行方向：** 使用者先改採web-first，再明確取消全局短時測試窗口，
以每工作執行額度及同一POC累計USD110（內含USD10收尾保留額）控制。
現行語義見[行為規格修訂](FA-AGENT-BEHAVIOR-SPEC.md#2026-09-20-使用流程修訂每筆工作與共用預算不設全局短窗口)，
實作及原生狀態見[進度](progress.md)。後端與網頁分工併行，原結果正式交付、
正常SDK完整工作及網頁收件仍須實證；Teams整合延後。
下方日期、US$100與早期批次條件保留歷史，不用它們覆蓋後續明確決策，
也不將本次改向回填成早期測試通過或重啟已結案操作。

更新日期：2026-09-15。M4最新r3四案通過、第五案checkpoint前incomplete，已停止；原五案驗收仍為FAIL，總US$100不變。
API `m4-3`／Hosted A17／B11；首批品質FAIL、r2分頁與品質FAIL歷史分開保存，詳見[M4結果](M4-CONTEXT-POC.md)。
21:18 +08取得M4白名單commit／push授權後，程式基準已發布為`1ca1c762050cdcafb51484b5155ad5b65503cd65`，origin read-back一致；起始`dcaa7192bd6715921870195fbf75914bbcb59ba5`只有v1.2文件。
後續先接[Table＋Blob原件／來源保存](FA-DATA-STORE-RESEARCH.md)，再按[ADR-0004](adr/0004-agent-behavior-control.md)及[行為控制規格](FA-AGENT-BEHAVIOR-SPEC.md)增量接線。
Git發布不擴張雲端／模型／權限額度，不把新來源回讀機制用來回填M4 PASS。

**後續執行更新：** M1已完成；[M2實際結果](M2-ASSISTANT-POC.md)通過SDK文字工作、未完成checkpoint恢復及A→B發布，但自主UI未通過，160次模型額度已用完。下面第1–4節保留原分析時間點，不是最新雲端快照。

**2026-09-14 21:32 +08 執行順序決定：** 使用者同意保留[M2 UI缺口](M2-ASSISTANT-POC.md#保留的-ui-缺口)，先推進不依賴UI的M3成果／版本／人工批准。UI模型對照延後獨立執行，M2仍為部分通過；這不是取消UI驗收，也不授權重設已耗盡的M2 counter。M3新的雲端權限與付費測試另列範圍。

**現行順序：固定M4的PARTIAL基準，補原件／來源保存，再增量整合v1.2並交付可親自驗收的UI或Foundry入口。** 三個行為core已在工作樹離線整合但未接線，不代表BR-01完成；本次M4發布不包含它們。M2自主UI缺口與M3歷史FAIL／unknown保留，不重做SMB、不預設固定VM。第1–4節保留2026-09-14以`05ca4ca`分析原共享Context POC的快照，不用其中的「尚未接SDK」判定最新進度。

## 1. 證據範圍與完成口徑

本輪已讀取 [結果報告](../POC-RESULTS.md)、[機器摘要](POC-GATE-RESULTS.json)、實作與 controller，並核對摘要確有 17 個 gate、全部標為 PASS。370 checks 與 236.828 秒是原 run 記錄的檢查數與總時間，不是 370 個獨立業務案例，也不是助理效能 benchmark。

| 證據 | 本輪可確認的範圍 |
|---|---|
| Git 內報告／JSON | 完整功能 run `sc-9a84c17c014b4c3a99bbb3459f5cbb4d`，Hosted A5／B4、API poc4；17 項功能 PASS，另有執行限制偏差 |
| 程式碼 | 實際資料分工、ETag／owner／發布邊界、記憶查詢、探針行為及驗收方式；不是重新執行雲端測試 |
| 原始 evidence | 報告記錄 165 檔、494607 bytes 及 report／manifest hashes；`evidence/`、`.azure/` 未加入 Git，本工作樹也不存在，故本輪未獨立核對原件或截圖 |
| 雲端現況 | 僅引用原報告的時間點；沒有重新讀取 Azure live state，不能把先前 idle／零 replicas 當作現在狀態 |

完整功能測試時間為 2026-09-14 00:34–00:38 +08。後來 controller 的 endpoint 路由修正，只做了 A/B runtime／已發布成果的唯讀回歸，**沒有重跑完整 17 gates**。原功能結果與修正後回歸的證據鏈必須分開保留。

本輪只調整方案與文件，不建立資源、不重送 Memory updates、不執行付費測試，也不沿用原 POC 的部署、刪除或發布授權。

## 2. 結果如何改變方案

| 面向 | 已有結果與程式依據 | 對方案的影響／仍未證明的部分 |
|---|---|---|
| 共享狀態與更新衝突 | A 建立、B 讀取；同 owner／同 ETag 的兩筆 PATCH 得到一筆 200、一筆 412，revision 只加 1。[TableRepository.replace](../src/fa-shared-context-poc/shared_context/storage.py) 使用 `IfNotModified` | 保留 Table＋受控 API；這是單一工作狀態的條件更新，不是跨服務交易或完整分散式鎖 |
| 單一編輯者與發布 | owner／handoff 有正負向結果；發布後八種修改被拒。[ContextService](../src/fa-shared-context-poc/shared_context/service.py) 凍結整個 published work | 不再從零設計 owner／CAS；但目前沒有完整版本歷史，`publish` 是 agent 操作，不是人類批准 SOP／skills 的流程 |
| 長期記憶 | Team `sepia`、私人 `indigo`、idle 期間 `coral` 偏好被找回；opaque verification-code 曾被抽取省略 | Memory 用於語意知識／偏好；精確值、批准、版本、hash 與工作進度仍由權威狀態保存，不能把更換 fixture 後的 PASS 當成無損記憶 |
| 本輪 context | [assemble](../src/fa-shared-context-poc/shared_context/service.py) 取完整 work、goal 前 2048 字，依序搜尋 Team 與本人記憶；[adapter](../src/fa-shared-context-poc/shared_context/memory.py) 每次最多 10 筆 | 已有按需組裝的接線；10＋10 是筆數限制，不是 token 預算、相關性品質或效能驗收 |
| 恢復 | [resume_gate](../scripts/validate-shared-hosted.py) 明確 stop A、讀回 idle、由 B 更新，再以同 session 呼叫 A；boot marker 改變後可重載狀態／偏好 | 外部狀態不依賴原程序是重要成果；沒有驗完整 SDK checkpoint、突然故障、自然 idle timeout、HOME 或 browser profile |
| GUI | A 非 root、headed Chromium、OS 視窗聚焦／鍵盤輸入、截圖、下載及清理通過；[實作](../src/fa-shared-context-poc/shared_context/gui.py) 呼叫固定的 xdotool 序列，使用 ephemeral temporary directory | 已證明部分 OS UI 操作鏈，不是 SDK 自主看畫面／決策；本 gate 未驗滑鼠定位／點擊／拖曳、一般桌面應用或私人 profile 持久性 |
| 身份與原生存取 | API 驗 token／OID allowlist；跨 Team、私人 target／receipt 被拒；直接 Table／Memory 為 403 | 保留先授權再選 scope 的邊界；scope 不是 ACL，Storage 網路拒絕不能單獨證明 RBAC，靜態 A/B allowlist 不是完整組織治理 |
| Session 控制 | service-name 模式曾增建額外 sessions；50 筆額外 active 被 stop。endpoint-only 回歸未新增記錄 | 修正方向保留；[Controller.invoke](../scripts/validate-shared-hosted.py) 的兩個明確 ID 不等於平台全量實際並行上限，後續須比對 inventory |
| 真正的 AI 助理 | hosted_main.invoke 呼叫 HostedClient.dispatch（兩者已於 2026-10 移除），執行已指定的 API／GUI／runtime 探針 | A/B 尚不是由 Copilot SDK 自主推理、選工具、分工的助理；Memory 內部使用模型也不等於已接上 agent harness |
| 檔案成果 | [models](../src/fa-shared-context-poc/shared_context/models.py) 只支援 tiny artifact text；Table 工作 JSON 上限 48 KiB | 不把這個 payload 擴充成任意文件庫；正式檔案傳遞、版本歷史、來源及發布要另定合約 |
| 費用 | 最後成功的延遲讀值約 US$1.32；較新查詢為 HTTP429，總額及剩餘額未知 | US$100 符合性仍 UNKNOWN。18688 個已回報 tokens、session 記錄數及零 active 都不是帳單 |
| SMB | 6／100 輪後停止，MI 取票成功但 mount／kernel helper callout 仍 ENOENT，原 G0 BLOCKED | 保留失敗與定位界線，不再使用剩餘 94 輪；Context API 的 PASS 不回寫成 OS mount 成功 |

## 3. Hosted sandbox 與固定 VM 的角色

**Hosted sandbox 已經是助理的隔離電腦，不需要在它外面再疊一台固定 VM 才能使用共享 Context。** 2026-09-14 使用者確認以環境與工作連續性取代「同一台 VM」；compute 更換本身不再是架構失敗。

| 層次 | 應放什麼 | 不應混同 |
|---|---|---|
| FA Agent／Team | 長期身份、歸屬、角色與授權 | 不是 process PID、Hosted session 或一段聊天 |
| Hosted sandbox | 目標 SDK／CLI、shell、browser、GUI、必要工具及私人工作目錄 | 不是 Table／Memory 的替代品，也不要求底層 VM 不更換 |
| Image＋私人持久狀態 | 可重建的工具版本基線、私人檔案、profile、必要設定與 SDK 狀態 | `$HOME` 可保存不等於所有 root filesystem 變更都會保存 |
| Context API＋Table | 精確工作狀態、owner、ETag、成果參照與操作紀錄 | 不是完整聊天、語意摘要或 agent 排程器 |
| Memory Store | 有權限邊界的 Team／私人語意知識 | 不是無損資料庫、批准來源或完整 SOP 發布系統 |
| 模型／harness | 模型負責推理；Copilot SDK／CLI 驅動助理工具循環 | Memory extraction 的模型呼叫不算 SDK 整合 |

歷史 controller VM 只是測試控制端，不是 agent computer；不因它曾存在就將它納入產品架構。外部電腦只在 Hosted 有不能滿足必要需求的精確證據時才提案，不是本計畫的預設 fallback。

## 4. 調整後的執行方案

保留已有的 Hosted A/B 邊界、Context API、Table CAS／owner、Memory adapter 與非 root GUI 工具。下一階段將探針換成真正的助理執行入口，透過受控工具使用同一套服務，不重新發明共享儲存。

正式網路仍需決策：POC 的 Foundry 是 Non-VNet；Context API 接入私網 Table，不代表 Memory Store 也在私網。維持 Entra 驗證與既有 Storage 公網禁用政策，不為接 SDK 放寬原生資料權限。

只改受影響元件及相關測項。不要為整理結果重建映像、重跑完整 POC，或把舊 `azure.yaml`／SMB runbook 當作新方案入口。

## 5. 後續里程碑

以下是計畫，不是執行或支出批准。每一階段先完成可離線準備，雲端測項另列精確資源、模型、版本、次數、時間窗口、費用與收尾方式。先完成最小可用閉環，不用長時間 burn-in 或完整工作台阻擋核心驗證。

| 階段 | 最小交付與完成條件 | 前提／範圍 |
|---|---|---|
| M0：收尾與可重現基準 | 取得受控原件位置、可分享 manifest／hash 與權限；確認當期費用、保留資源及保留／清理選擇。把功能結果、路由回歸、用量、費用分開 | 不重送既有操作；刪除逐項確認。原件未取得前，保留「僅核對 repo 摘要／程式」的證據限制 |
| M1：Hosted 私人環境連續性 | 明確 image／工具與私人持久狀態清單；在非 root、真正 Hosted 中驗檔案、profile、工具設定跨請求及 stop／resume 恢復，其他 agent 不可讀寫 | 先驗底層環境，真正跨 SDK 對話／工作的恢復在 M2 接線驗收。HOME 不可寫必須處理，不改用 `/tmp` 當作持久化成功；雲端測試另批准 |
| M2：Copilot SDK 真正助理閉環 | 從一位助理開始：自然語言工作→模型選工具→讀取 context→更新成果／工作；包含自主操作 Hosted OS UI，再接第二位助理交接，記錄工具位置、身份、結果及恢復狀態 | 先固定 SDK／CLI／模型與認證契約；模型呼叫另批准。原 G4 仍是 NOT_RUN_OPTIONAL，不能因計畫列入就改 PASS；固定 GUI probe 不算自主 UI |
| M3：成果與批准契約 | 實際檔案及版本參照、hash、來源與可取得性；SOP／skills 的人類批准要綁精確內容版本，保留原有 owner／ETag 邊界 | 不把 tiny Table text 當一般文件庫；不把 agent `publish` 當人類批准。POC採專用Blob container與人類CLI，正式生命週期管理另定 |
| M4：Context 品質與效能 | 最新r3四案通過、checkpoint前incomplete並停止；原五案驗收仍FAIL | 固定PARTIAL與程式基準，保留來源／metrics及未結事項；不盲重跑，不宣稱p95或10倍 |
| M5：行為控制與四 agent 業務切片 | 依 BR-01–BR-06 先補可靠交付、背景恢復及必要 context／授權，再驗四個實際執行者與 Teams 業務切片 | 沿用 M2／M3 與 M4 收尾基準，不重做；新增雲端、通知、routine 另批准，不先做完整 SaaS |

### 各里程碑的使用者驗收

以下是各里程碑的可測功能；M1受控驗收已完成，M2只有文字／恢復／交接通過，自主UI仍阻擋；M3核心閉環已完成但歷史FAIL保留，M4／M5尚未完成。M1–M4先有CLI／薄驗收入口，M5才接最小Teams業務入口，不提前承諾完整Web工作台、即時桌面串流或人類接管。

| 里程碑 | 你可以怎麼測 | 應看到什麼才算成立 |
|---|---|---|
| M0：可核對的 POC 基準 | 開啟結果／證據目錄，取回受控原件核對 hash；查看帳務時間點及保留資源清單 | 每項結論可追到原件；費用、資源去向與未結清項目明列。這一站不新增聊天或助理功能，費用未知不能標成成本收尾完成 |
| M1：會保留工作的雲端電腦 | 用驗收入口在 A 建立合成檔案、設定 browser profile 偏好與必要工具設定；停止後恢復，再讓 B 嘗試取得 A 私人資料 | A 的檔案內容／hash、偏好與工具配置相符或按契約重建；B 不能取得 A 私人內容。另有替代 session／版本更新的受控恢復案例；此時還不是自然語言助理 |
| M2：可交辦、會用工具的助理 | 用自然語言請 A 讀測試資料、製作並存下報告；交給 B 接續檢查。換對話或中斷後要求接續；另外交辦下方 OS UI 任務 | 模型實際選工具並在正確 Hosted sandbox 執行，產生可讀成果與工作紀錄；接續不重送已完成副作用。UI 要有逐步觀察／決策／操作證據，不是模型只呼叫固定探針 |
| M3：可版本化交付與批准 | 取得真實成果檔，請助理修訂成新版並讀回舊版；提出一項 SOP 變更，分別測試未批准、拒絕及批准指定版本 | 舊版本內容／hash 不變；未批准或被拒 SOP 不生效，只有你批准的精確版本生效。Agent 不能自行冒充人類批准，其他寫入路徑也不能改已發布內容 |
| M4：長期使用仍找得到、快得有根據 | 加入代表性長歷史與干擾資訊，在新對話／恢復後追問先前決策；用同一工作集重跑 FA／OpenClaw 對照 | 找回相關且有來源的內容、遵守批准與私人邊界，context 不無限增長；可核對品質、端到端時間與 tokens。10 倍只在已批准的同等品質／統計門檻達成時標 PASS，產生報表本身不算達標 |
| M5：可在 Teams 交辦的協作團隊 | 同時交辦五件工作，觀察四位助理處理、第五件排隊；測交接／取消／恢復，再交辦一次研究→方案→批准的 Azure POC→可編輯成果 | 實際執行量遵守四 agent 上限，而非只顯示四個名字；工作與成果能追蹤。未批准不建資源，批准後只做指定範圍；可從 Teams 取得進度與交付物，不代表完整工作台已完成 |

每一站交付可重跑的情境、入口用法、預期結果與本輪證據。負向或恢復案例失敗須明列，不用一句「可以 demo」代替完成條件；模型、工作集、品質／延遲門檻及測試費用在執行前確認。

### M1：恢復什麼，而不是保留哪一台 VM

1. 列出基礎 image、工具／相依版本、私人檔案、browser profile、必要設定、SDK 狀態與工作 checkpoint 的保存／重建責任。只測一個 home marker 不夠。
2. 對 UID10001／HOME owner/mode 的落差，先核對官方配置與最小修正；不能把此版本的 `unavailable` 擴張成整個平台不支援，也不能以全程root或暫存目錄掩蓋。2026-09-14另獲批准的例外是「root只初始化專用目錄，立即降權」，A7純檔案stop/resume已通過；不代表完整HOME／profile可用或官方通用保證，見[M1結果](M1-PRIVATE-ENVIRONMENT-POC.md)。
3. 以合成檔案及本地測試頁的 profile 偏好驗證：同 agent 的新請求、stop→native idle→resume 後，必要檔案／設定／工具版本一致或按已聲明方式恢復；A/B 私人內容不互通。不使用真實網站登入資料作第一個案例。
4. 分開記錄 image 基線重建與持久檔案保留；SDK session、真正跨對話及業務 checkpoint 的接續於 M2 做端到端驗收，不能用 marker 冒充。compute／PID 改變是允許的，資料或重要約束遺失不是。
5. 為 session 到期／替代 session 與 agent version 更新定義恢復方式，不把「同一個 session resume」外推為永久保存。可先以受控替代 session 案例驗恢復路徑，不宣稱已完成 30 天保留實測。

**M1實際完成狀態：** [完整結果](M1-PRIVATE-ENVIRONMENT-POC.md)已驗A8／B6的私人profile／工具、A/B隔離、
warm及stop/resume，並透過[私網Blob備份](M1-CLOUD-BACKUP-DESIGN.md)恢復至新A8與A9 session。
A9沿用同一image，僅新增明確`PYTHONUTF8=1`設定；同設定deploy重用A8的失敗原件保留。
累計26calls／7stops，4個新sessions皆停止，完整144筆清單全idle。這不代表SDK工作、任意工具升級或自主OS UI完成。

### M2：從探針變成助理

**已執行：** A12／B9以真實SDK完成下列最小文字閉環；版本、原件與限制見[M2結果](M2-ASSISTANT-POC.md)。同一SDK跨process恢復已通過，替代session／版本還原及自然token到期仍未驗。

- 將目前由 controller 寫死的操作順序與真正模型推理分開。驗收必須看到模型產生的工具呼叫、實際執行位置與權威狀態，不接受固定回應假裝 SDK 自主工作。
- 明確保存 FA Agent、Hosted definition/version/session、SDK session 與 conversation 的映射；更換對話不能無意建立新私人電腦或混入其他 agent 的狀態。
- 固定可用的 SDK／CLI 版本、模型 deployment 與 provider 認證。官方有 Entra bearer-token callback 指引，但此 repo 尚未實測 SDK 長任務 token refresh；不只照 README 的概括敘述判定支援或不支援。
- 工具採明確 allowlist 與 permission handler，不照抄 `approve_all`。模型不能自行擴大 Azure 範圍、跳過批准或取得 Table／Memory 原生權限。
- 依 Hosted runtime contract 處理 SIGTERM、持久 checkpoint 與可恢復執行。Invocations 不自帶對話歷史；一般背景 subprocess 也不等於 resilient task。先驗中斷後接續，對已接受但結果未知的副作用不盲目重送。
- Agent Framework Group Chat 保留作共同討論的既定方向，但不取代權威工作狀態，也不在最小 SDK 閉環前同時引入第二套重複排程。

### M2：自主操作 Hosted OS UI

**最新自主UI已實際執行，但未通過。** A13能填字與開啟native file chooser，仍未完成正確欄位／選檔／下載；另一版面未跑，見[M2結果](M2-ASSISTANT-POC.md)。以下A5／M1內容保留為較早的底層能力證據。

[原GUI摘要](POC-GATE-RESULTS.json)記載A5／UID10001、Xvfb／Openbox／headed Chromium通過。輸入由[desktop_smoke._send_gui_input](../src/fa-hosted-sandbox-poc/desktop_smoke.py)執行固定OS鍵盤步驟，前後PNG與下載由controller核對；不把它改寫成自主決策成功。

原A5的PASS沒有涵蓋滑鼠定位／點擊／拖曳、一般桌面應用、模型辨識畫面並決定動作或持久profile。
後續M1已在真正A8／B6／A9驗持久profile及固定OS輸入，保留14份PNG；它們仍是腳本探針，
沒有交給SDK作下一步決策，不能算自主OS UI。

M2 必須提供受控的畫面／OS 可存取性觀察及滑鼠、鍵盤、視窗操作工具，讓 SDK 驅動逐步觀察→決策→操作→再觀察。**模型只呼叫一次現有 `operation: gui`、內部仍跑固定腳本，不算這項驗收通過。**

| 情境 | 可交辦的任務與驗收 |
|---|---|
| 自主操作表單 | 只給任務與合成資料，不給座標或完整操作序列；助理在自己的 Hosted 桌面找到欄位／按鈕，至少完成滑鼠定位點擊與鍵盤輸入，送出本地測試表單並下載結果。獨立核對回傳值、檔案內容與 hash |
| 超出網頁 DOM 的操作 | 使用同 sandbox 的 OS 檔案選擇視窗選取合成檔案，或切換／聚焦另一測試視窗；保留 UI 觀察與操作紀錄，不能只靠網頁 selector 完成所有動作 |
| 介面變動後再做 | 更動測試頁布局或視窗焦點，再交辦相同任務；助理需重新觀察並完成，不靠固定座標或預寫 Tab／Return 序列。無法辨識時明確停止或回報，不假裝完成 |
| 執行位置及批准邊界 | 每筆工具紀錄可對應 FA Agent／Hosted session；不用本機、外部 VM 或遠端 browser 替代。未批准的外部傳送、付費或越界操作須被阻擋，不能改走 shell／API 繞過 |

先用 loopback 測試頁與合成檔案，不登入真實 SaaS、不上傳私人資料、不安裝未批准軟體。模型／CLI／UI 工具版本、允許動作、呼叫／時間上限與失敗收尾均需在雲端執行前批准；這項基本 UI 驗收不承諾任意桌面程式、拖曳工作流或人類遠端接管。

### M3／M4：工作狀態、記憶與 context 不互相替代

**M3最小閉環已於2026-09-15完成：** 受控工具交付真實檔案、保存不可變版本及來源／hash，再由真人批准精確版本。新版本提出、未批准或被拒絕，都不能自行變成生效版本。批准身份由伺服器驗證，不採用模型自稱「已獲批准」或現有agent `publish`。此階段不依賴模型點擊UI，也不改寫已發布的M2工作；歷史格式FAIL及unknown仍保留，詳見[M3結果](M3-ARTIFACTS-POC.md)。

**2026-09-14 21:39 +08 批准者決定：** 使用者指定`admin@contoso.onmicrosoft.com`為M3 POC的唯一人工批准者。後續已依明確授權設定專用public client／scope／Principal consent，以驗證後的人類身份與伺服器allowlist判定，不信任request body自稱的批准者。A/B agent可提交成果及規範草稿，不能代替人類批准。一般成果寫檔／版本保存不必逐份人工批准；此處批准針對SOP／skills指定版本的生效，也不等於某次雲端部署或支出的授權。

**M3是可親自試用的交付點，不必等M4。** 完成條件除了程式及測試，還包括可用的CLI／薄入口、操作說明與真正Hosted／儲存路徑驗收。使用者應能：

1. 以自然語言交辦合成任務，取得可下載、可核對來源與hash的成果檔。
2. 要求修訂成新版，仍可取回舊版，確認原內容沒有被覆寫。
3. 提出SOP／skill新版，以admin身份拒絕或批准精確版本；未批准／拒絕時保留原生效版，批准後才切換。

M3後端、人類JWT與CLI、Blob adapter、Hosted成果工具及獨立模型路由已整合並完成真雲端核心驗收。
原成果v1／v2在A14保存；修復批次另獲批准，最終API `m3-2`／A15，B仍9。
真人下載兩版成果與Hosted原件逐byte一致，舊版Blob版本／ETag／hash／來源不變。
真人批准SOP v1、拒絕v2、批准v3；未批准／被拒v2均保留v1，兩個後續真consumer分別產生V1與V3。
最後consumer SDK trace已核對真正`read_context`的批准版本；不是把部署成功當作能力驗收。
v1字面格式FAIL、原SOP漏建metadata的unknown與中途HTTP500讀取失敗保留；`full_scenario_pass=false`。
本機契約採專用Blob成果container＋Table版本紀錄，與M1私人備份分開；一般成果最多512 KiB／12版，
SOP／skill先限8 KiB UTF-8文字／Markdown。來源work不被成果修訂覆寫，批准僅切換精確版本指標。
介面、未知寫入處理及限制見[實作說明](../src/fa-shared-context-poc/README.md#m3-成果與批准)。
本批已收尾：模型59／80、native72／80、jobs8／8、stop7／7、sessions2／2、active1471.358秒，
A99／B55共154筆全idle。雖有21次模型餘額，其他必需額度已耗盡，不得再啟動工作。
既有成果的唯讀CLI可用；新交辦／修訂試用、M4或UI對照需另定有界範圍，不重設舊counter。
預留US$10、總預算仍US$100；05:10:16 UTC延遲帳務US$18.1654低於較早US$27.0793，原因未查明；
保守參照較高值，M3精確成本及最終帳單仍未知。完整限制與入口見[M3結果](M3-ARTIFACTS-POC.md)。
首個入口不依賴自主OS UI，也不提前承諾Teams、完整Web工作台或任意桌面任務。
M4另驗長歷史context品質、記憶相關性、成本／延遲及效能對照，不作為上述基本試用的前置條件。

- Table 保留精確工作狀態與操作 receipt。現有 48 KiB payload、inline artifact text、published 後整個 work 凍結，是 POC 邊界；正式版本歷史與檔案生命週期需獨立設計，不靜默改舊 API 行為。
- Memory 由合成色彩偏好擴充到代表性工作資料，驗相關性、矛盾／過期知識、遺漏、私人隔離及來源；批准與政策指令仍走已批准版本，不由記憶抽取自動生效。
- 明確定義整體 context token 預算、工作狀態／角色指令優先順序及截取方式。目前兩次 Memory search 是依序執行；先量延遲與 token，再決定是否需要平行、cache 或改查詢策略。
- 記憶服務失敗時要先定「停止」或「明示缺少記憶後繼續」的行為，不默默回空記憶成功。Memory update 已接受但 receipt 不完整的狀態需可追查，不承諾跨服務 exactly-once。
- 10 倍比較固定 OpenClaw／FA 版本、模型、工具、資料、資源與品質；分開冷啟動、暖機、長歷史與恢復。比較相同統計量的端到端時間比值 `OpenClaw / FA >= 10`，不能用短 prompt、跳過工作或不等品質來湊數。

### M4 三倍操作額度與修復重驗

2026-09-15 17:03 +08，使用者明確要求US$100總預算不變，其他相關操作額度提高為原三倍。
這是累計總上限，不是再加三倍，也不歸零首批、診斷probe或r2用量；M2／M3舊counter不放寬。
上限與修復部署見[M4最新紀錄](M4-CONTEXT-POC.md)。五案、fixture SHA、來源／品質門檻不變，不增加BR gate或十倍要求。
操作器保留最後180秒及最後一次stop作清理；新權限、client、secret、consent、刪除與GitHub發布不從三倍次數自行推定。
v1.2四槽及每obligation最多兩次模型補交維持原契約；原首批與r2停止紀錄不因新批准改成PASS。

### M4 第一切片：離線準備完成，雲端尚未執行

**本節標題與以下準備步驟保留歷史及既有連結；最新r3四案通過、checkpoint前incomplete並停止，見[M4結果](M4-CONTEXT-POC.md)。**

2026-09-15，M2／M3基準已commit／push至`f097fa687b0e331879e1d3c8d3b5d259b96b46e1`。
使用者起初授權離線準備，之後已明確批准下列有界雲端範圍。
API與Hosted專用接線已交付，原生操作器仍在整合；尚未部署M4、建立Memory Store或請求模型。

原M2／M3部署不能直接冒充M4：舊`SyntheticMemory`只接受顏色偏好的marker／value，
舊Gateway沒有M4路由，M3的`artifact_task`不允許checkpoint模式。
新增本機接線使用隔離的M4資料入口、Memory Store、context及model counter，沿用文字checkpoint機制；
不重開M3 counter、不放寬一般記憶上傳、不更改舊路由。M4只更新`artifact_text`，保留權威工作`progress`，
resume後必須當前程序重新讀context。共用fixture的34項檢查與M2／M3／M4 runtime合併回歸共702項通過。
初次回歸有2個舊mock desktop案例因Windows測試路徑達283字元而失敗；
只縮短測試暫存路徑、未改程式或assertion後，同一完整範圍通過，失敗日誌保留。這不代表自主UI缺口已解決。

| 固定案例 | 要核對的差異 |
|---|---|
| `short-cold` | 種入短歷史後先stop，從native idle重新開始；計時須從第一個Hosted請求開始，不能讓budget／context前置讀取偷暖機 |
| `short-warm` | 同一Hosted session／process、另一個SDK conversation；量相同短任務的暖機路徑 |
| `long-warm` | 加入128則、52147 UTF-8 bytes的歷史與干擾，再查目前決策／偏好；不是把全部歷史塞進prompt |
| `long-new-conversation` | 新SDK conversation找回同一組有來源的事實，不借用上一個SDK對話歷史 |
| `long-resumed` | 未完成checkpoint後stop／resume；SDK session相同、process不同、Hosted session相同，不重送已接受副作用 |

短歷史為272 bytes。兩種歷史都保留過期owner資訊；目前owner以Table權威狀態為準。
Team偏好、A私人偏好與B私人偏好分開；必須先在B自己的scope實際找到私人偏好，才能把A沒有洩漏當成有效負測試。
另需取得跨私人讀取的真正HTTP403。SOP只讀既有已批准v3；不新增提案／人類批准，
若原版本被人類改變則停止，不能為了符合答案覆寫回v3。

合成資料與判定規則由[離線檢查器](../controller/m4_context_eval.py)產生。
完整fixture SHA為`8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090`。
`cases`只含任務與生命週期；`seed_data`供控制端種入受控資料，`oracle`只供離線判定，
**不可把整包fixture或oracle傳給模型**。已納入compact／concise／brief等明列同義詞，
避免把已知同義表達誤判為字面格式失敗。

```powershell
$py = '.\src\fa-shared-context-poc\.venv\Scripts\python.exe'
& $py .\controller\m4_context_eval.py fixtures
& $py .\controller\m4_context_eval.py fixtures --part cases
& $py -m pytest .\tests\context_eval -q -p no:cacheprovider
# 只檢查已捕獲的觀測，不會執行模型或Azure：
& $py .\controller\m4_context_eval.py evaluate --case short-cold `
    --observation '.\.azure\m4-short-cold-observation.json'
```

判定要求四項合成答案與來源指標全部正確、來源含相符的支持詞、B私人內容零洩漏、
精確批准版本相符、實際工具context文字不超過32768 UTF-8 bytes，
每個模型請求原生回報的input tokens不超過8192。超過上限列FAIL，先不自動截斷內容或修改門檻。
觀測必須包含`case_id`／fixture SHA、原始`context_text`、含value／citation的answer、
`metrics`、`binding`及B正向控制／403負向控制；後四案另提供`previous_binding`。
缺欄位、錯scope、重複JSON key、未知token數、NaN／負延遲等明確回`INVALID_INPUT`，不補零或猜值。

這只是固定合成案例的檢查，不是通用語意裁判。來源詞比對不能判斷所有否定、過期或矛盾語意，
因此雲端收尾仍須人工核對來源；native collector還要核對Memory update完成、scope、模型／session binding、
原生tool結果及hash，不能用假觀測取代。
檢查器永遠回`cloud_evidence_verified=false`，只表示它本身沒有完成這些原生核對；
它也不會因產出報表就給10倍PASS。五案只作首批功能與時間基準，不計P95或宣稱統計顯著。
冷啟定義限Hosted native idle；ACA是否冷啟另記，不能宣稱整條鏈路全冷。
恢復的端到端時間須排除明列的idle／人類等待，並另外保存原始牆鐘時間。

**已批准的M4雲端上限：**

| 項目 | 本批提案 |
|---|---|
| 範圍 | 原subscription／RG／Foundry project、West US 3；admin執行 |
| 資料隔離 | 新建唯一`poc-m4-context-01` Memory Store；重用已核對的既有模型deployments，不改`poc-team-memory` |
| 建置／部署 | 最多2 builds（API及共用Hosted image）、1 API revision、A及B各1新版本；大小／scale不變 |
| 權限／網路 | 不加RBAC、client、secret、consent或網路變更，不刪資料；既有權限不足就停止另批 |
| 執行 | 2個新sessions、自有最多1 active、全體最多2 active；5次SDK start＋1次resume，共6次 |
| 次數 | 新M4 counter最多60模型attempts、100 native invokes、12 native file downloads |
| Memory | 最多4次update提交（短Team、長Team、A私人、B私人），每筆最多6次poll；unknown不重送 |
| 停止／時間 | 最多5次stop：B種入後、A種入後冷啟重置、checkpoint、最後收尾及1次失敗cleanup備用；active累計1800秒 |
| 費用 | M4預留US$10，仍包含在原US$100內，不是精確估價；執行前刷新帳務與完整inventory，保守採已知較高值檢查空間 |

使用者已明確回覆「批准，按此範圍與上限執行 M4」，批准記錄時間為2026-09-15 05:57:44 UTC。
凍結提案原件留在ignored的`.azure\m4-cloud-proposal.json`，保留提出時的`awaiting_user_approval`狀態，不覆寫歷史；
獨立`.azure\m4-cloud-approval.json`綁定提案SHA
`1912290f71458b8071110d61541b9158331f87d45c87e4931904cfed48aaf0ea`。
M4 API／Hosted專用接線已交付，native collector與單一累計ledger仍在整合；
原operator程序已`cancelled`，父端接手現有檔案；不再等待該程序交付。
父端已修真native schema／inventory邊界，接上checkpoint單檔、SDK工具結果原文與首筆current-process context，
並修正cost重試原件契約；操作器與部署helper合跑49項、另7個subtests通過。
完整fake流程僅為`SYNTHETIC_ONLY`。尚須凍結來源、刷新身份／成本／全idle inventory、
核對部署預覽／固定definition才進入雲端驗收；尚未建立`code-ready`。
唯讀preflight已確認A15／B9、API `m3-2`、完整session inventory全idle、原Memory Store設定與既有chat／embedding deployments；
新`poc-m4-context-01`尚不存在，未build／deploy、未喚醒Hosted或執行模型。
06:09:32 UTC的延遲MTD費用為US$18.57459634039678957；
仍保守用較早較高的US$27.079258133377022439作預算檢查，不能把差額當成退款或M4精確成本。
不包含UI、換模型、OpenClaw比較、四agent、Teams、新SOP決定或既有M2／M3成果修改。
這批若失敗，保留原件並停下；不能使用cleanup備用次數增加測試、改run ID重開額度或偷加build。

### M4 本批：已批准、實作中

以下保留規格分支整合前的批准摘要；實際已部署並在首案品質FAIL後停止，最新結果以[M4收尾](M4-CONTEXT-POC.md)為準。本節不是另一份ledger或續跑授權。

2026-09-15 與開發工作階段核對：使用者已批准以下有界批次，目前正在接線，尚未提交／雲端驗收。此前工作樹文件的 `awaiting_user_approval` 是較早快照，不覆蓋後續批准。實際執行只由持有批准及累計 ledger 的開發工作階段進行，本規格分支不平行重跑。

M2／M3 基準為 `f097fa6`；M4 的 `controller/m4_context_eval.py` 與 fixture／checker 已有離線結果。隔離的 Memory／context、獨立 gateway／counter、Hosted `evaluation_run_id`／checkpoint 接線及 native operator／collector 實作中，不把預定檔案當成已完成。參考資料為另一開發工作樹的唯讀觀察與狀態回報，尚不是本分支已合入的 M4 code。

| 固定案例 | 保持原完成條件 |
|---|---|
| `short-cold` | 種入短歷史後從 Hosted native idle 開始；計時從首個 Hosted 請求起，前置查詢不得偷暖機 |
| `short-warm` | 同一 Hosted session／process、另一 SDK conversation 的短任務 |
| `long-warm` | 128 則／52147 UTF-8 bytes 歷史及干擾後找目前決策／偏好，不整包塞 prompt |
| `long-new-conversation` | 全新 SDK conversation 找回有來源事實，不借用前一 SDK history |
| `long-resumed` | 未完成 checkpoint 後 stop／resume；同 SDK／Hosted session、不同 process，已接受副作用不重送 |

短歷史 272 bytes，固定 fixture SHA `8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090`。`cases`／`seed_data`／`oracle` 分離，oracle 不送模型。四項答案及支持來源需正確；目前 owner 由 Table 取，B 私人資訊先有正向 control 再驗 A 不洩漏及真 403。沿用已批准 SOP v3；人類若改變該版本就停止，不改回去湊答案。

保持工具 context ≤32768 UTF-8 bytes、每個模型請求原生 input tokens ≤8192；超限 FAIL，不暗中截斷或改門檻。未知 tokens／缺欄位／錯 scope 不填零，原生來源／binding／Memory completion 由 collector 核對。五案是小型品質與時間基準，不計 p95／10 倍；明列 idle／人類等待，保留原始牆鐘時間。

| 已批准範圍 | 本批界線 |
|---|---|
| 資源／資料 | 原 subscription／RG／Foundry project、West US 3；唯一新 Memory Store `poc-m4-context-01`，不改原 Store／M3 成果／SOP |
| 建置／部署 | 最多 2 builds、1 API revision、A／B 各 1 新版本、2 新 sessions；不改 scale／網路 |
| 執行 | 5 starts＋1 resume；自有最多 1 active、全體最多 2 active；60 模型 attempts、100 invokes、12 native downloads |
| Memory／時間 | 4 次 update，各最多 6 次 poll；5 stops、累計 active 1800 秒，未知提交不重送 |
| 費用 | M4 預留 US$10 包含在總 US$100；依原 ledger／批准 guard，不視估價為硬封頂 |
| 不包含 | UI、換模型、OpenClaw、四 agent／Teams、新 SOP 決定、新 RBAC／client／secret／consent／network、刪除資料 |

本節記錄既有批准，不另開一份授權。當時只完成本機工作及唯讀 preflight，尚未建 M4 Store、build／deploy、Hosted invoke 或模型工作；後續實際狀態由該批 ledger／收尾更新。本批結果不得被下列新增需求阻擋或改寫。

### M4 之後的行為開發切片

下表為增量開發 backlog。17:03 +08新增目標已包含v1.2實作；三個隔離core已在工作樹整合，但不在本次M4發布白名單，尚無API／Hosted／consumer接線或B01–B12環境PASS。基線前提是本批 M4 明確收尾（PASS／PARTIAL／BLOCKED 均保留其真實結果）及固定接線版本，不是要求先完成整個 M4 benchmark 或達成 10 倍。隔離離線實作不得交叉修改正在執行的 M4 批次；部署沿用明確累計上限，不自行新增權限或通知範圍。

2026-09-15首批執行／證據收尾已完成，品質FAIL保留；當時待M4提交授權，現已發布上述固定code SHA。
BR-01的隔離offline_contract不等於正式接線或BR-01 PASS，也不會關閉F18–F20／HOST待辦。

| ID／切片 | 依賴 | 最小開發交付與重用位置 | 完成條件 |
|---|---|---|---|
| BR-01：事件與有效操作授權 | M4 本批基線 | 在現有 Context／Table／M2 tools 加入接單原子集合、固定 runtime、配額／防重播、共用批准 reservation／撤銷、effect state 與終局性、實際執行描述／canonicalization、必要材料保護及首次秘密隔離；沿用 M3 批准者驗證，固定真儲存／基本 wake 與啟用條件 | BC-01／04；B01／B11 的 materials、B05-terminality、B09-terminality／execution-binding 及既有反例；無終局／冪等不重送、無實際內容綁定不 dispatch、材料無保護不成功接單；無 pin／GC 協定先停用相應回收，不以 fake store 宣稱原子恢復 |
| BR-02：單 agent 可靠交付 | BR-01 | 接 `AssistantEngine`／`finish_task` 已保存結果至 obligation／outbox／薄入口；接基本 consumer／watchdog、補交名額、送出前授權、required／max／回條關聯及未履行庫存，延伸 BR-01 材料保護至成果／receipt；不重做 M2 任務或 M3 檔案庫 | BC-02／08；B01、B02、B04／B12 的 materials 與既有條件，B09 sender／B11 交付部分；未交付／unknown 跨 retention 不失材料，delivery_failed 仍欠答，關旗標仍 drain；本版未關閉通知預留／出口計數的 P2 |
| BR-03：背景、ownership 與恢復 | BR-02 | 沿用 M2 binding／checkpoint，在既定 wake Adapter 擴 completion inbox、首次 shell armed、owner／epoch、取消、root 共用預算與單槽；不把 BR-02 必要 consumer 延至此時才接 | BC-03／07／08；B03、B06、B07-C1、B10、B11 相關斷點；真單槽／受控 executor 與退場核對；明列 B07-C4 未涵蓋，四槽實測在 BR-06 |
| BR-04：Context 與版本管理 | BR-01、M4 本批結果 | 接續實際 M4 context／collector，補產品 ContextManifest、摘要來源失效、有界預算、按需 skills／schemas；不改固定 fixture 的原判定 | BC-05／08；B08 的 context 部分與撤銷案例；品質保持且省略理由可查，未知 token 不填零 |
| BR-05：一般記憶治理 | BR-04 | 從既有 Memory Adapter 擴為 claim／evidence／audit／失效，保持私人／Team ACL 與原 receipt；一般入口與 M4 fixture 分開 | BC-06；B08 記憶部分、B11；假引用／撤銷／錯 scope 不進 context，不用新 update 蓋掉 unknown |
| BR-06：四 agent／Teams／受控 routine | BR-02、BR-03、BR-04 | 完成 M5 四槽／第五排隊、協作 silence、Teams 路由與通道能力／回條，再接已批准的研究→Azure POC→成果；routine 另外啟用，合併通知保留各 obligation | BC-03／04／07；B07-C4、B09、B10、B12 的 Teams 部分；四個不同 agent 的真 executor、有限協作、必要成果／當前收件權與回條相符；不沿用薄入口證據宣稱 Teams／UI 已讀 |

BR-04 可與交付／背景切片在不同檔案平行開發；BR-05 的完整 User Model 不阻擋不依賴它的最小 Teams 閉環。M2-UI-01／02 保留獨立補驗，不能以以上任何切片 PASS 替代。完整 benchmark／正式多人批准／完整 Web 工作台仍沿用原規劃，不塞進第一個可靠交付切片。

規範與 B01–B12 的唯一來源為[行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)，不把舊提案的 M2-A／M2-B 當新排程。每個切片分開交 `offline_contract`／`actual_environment` 的結果與未涵蓋子條件；B07-C1 不算完整 B07，真原子儲存／材料保護／wake 未接上不能宣稱相應 Hosted 保證通過。基本 consumer 是 BR-01／02 新入口啟用依賴，BR-03 才擴背景工作。

v1.2 只修 F15–F17 的 P1 契約，取代 v1.1 可直接凍結的口徑；不阻擋選型／離線工作。F18 控制通道配額、F19 父子停駐／讓槽、F20 能力 PASS 規則及 HOST R1–R3 維持開放；具體狀態只在規格第 13 節維護，不因本版完成而將 BR-01–BR-06 整體標完成。後續恢復材料在 BR-03 亦須沿用 BR-01 保護，其他材料隨相關切片接線。

先提交影響範圍內的程式／回歸與可用入口，再按新增主張需要另批准有界雲端驗收；只新增必要的防遺失、重送、資料外洩與明顯錯誤案例，不逐項要求重複部署。本次文件修訂不需等 M4 上雲，所有新契約與未關閉待辦都不回套既定 M4 gate 或舊結果。

### 從 README 移來的已知限制（Neal 2026-10-11）

Neal 要求把 README 刪掉的「目前還做不到的事」列入待辦、排入工作計劃（原話：「把 readme 刪掉的章節內容列入待辦事項排入工作計劃中」）。下表四項**都還沒開始**，依 Neal 2026-10-11 的順序由上而下排列（工作檔延續排最後），也不是執行或支出批准；雲端測項、租戶寫入與新 IAM 仍依 [AGENTS](../AGENTS.md) 逐次取得 Neal 批准。Neal 同日決定「第一項跟第三項可以不用做」，所以「比 OpenClaw 快 10 倍」的量測與「一套部署服務多位 Team 主人」不排入本表；[requirements](requirements.md) 與 [specification](specification.md) 的原文沒有因此修改。

| 項目 | 缺口 | 做到什麼才算完成 | 前提／要 Neal 決定 | 參考 |
|---|---|---|---|---|
| 原生桌面 UI 驗收 | 選檔、下載這類原生桌面 UI 操作還沒通過驗收；瀏覽器操作走 Playwright | `M2-UI-01`、`M2-UI-02` 在 Hosted 通過：模型自行觀察，完成原生選檔、提交與下載，獨立核對內容與 hash；第二次改變欄位位置，不用第一版結果代替 | 另批准有界雲端驗收（模型、次數、費用）；GUI 啟動不等於自主 UI | [M2 結果](M2-ASSISTANT-POC.md)、本文第 7 節 |
| connector：stage 真人驗收與 GitHub | Teams connector 已隨第 24 版部署到 stage，但 stage 沒有真人驗收紀錄；GitHub connector 還在設計 | stage：真人加入 Teams、同意連線，讀到訊息並測允許與拒絕送出，結果記入 progress 與[驗收段](extending.md#connector-acceptance)。GitHub：照設計 P2 實作，先 dev 完整測試，再經 release PR 上 stage | stage：Neal 在 stage 加入 Teams，並批准 Entra redirect-URI 租戶寫入。GitHub：Neal 決定開工；現有 registry 只支援 `CustomOAuth2`，Foundry 代管 OAuth 要另外補 | [connector 設計](connector-design.md#分階段)、[擴充](extending.md) |
| Team 隔離改用 Azure 權限的評估 | 不同 Team 的記憶與助理設定靠 FA 固定的 scope 隔離，不是靠 Azure RBAC | 一份評估：比較兩種隔離在記憶、Table、Blob 的做法、IAM 變更與風險，並由 Neal 決定是否改；評估本身不改權限 | Neal 認為需要才開始；任何新 IAM／RBAC 都是雲端權限變更，要另批准 | [ADR-0006 信任邊界](adr/0006-team-trust-boundary-grokbot-mode.md#1-以團隊為信任邊界)、[資料隔離](adr/0006-team-trust-boundary-grokbot-mode.md#3-團隊資料的最小隔離方式neal-1000-採-api-薄轉送) |
| 工作檔跨 VM 延續 | VM 被回收或換版後，工作檔不會自動延續，只有對話歷史與 checkpoint 會接上；現在只能由真人確認不搬檔接續 | 回收或換版後，工作檔經核對 hash 回到新環境，或明確拒絕並保留原檔；不需要真人確認「不搬檔」才能接續。**排最後**（Neal 2026-10-11 決定先做上面三項）；在做到之前的因應：重要的中間成果在工作階段結束前先交付（存到 Blob），每次發版都會讓助理換到新的 VM | 先選方案（候選版本先搬檔再切 selector，或其他）；要解決 stop 後 Files GET 互鎖與封存一致性；雲端驗證另批准 | [ADR-0003](adr/0003-hosted-environment-continuity.md)、[工作檔缺口](../src/fa-shared-context-poc/README.md#suspended-session-file-gap)、[搬檔漂移](../src/fa-shared-context-poc/README.md#source-drift-staging-exit)、[CTX-03 契約](ctx03-checkpoint-contract.md) |

## 6. 執行限制的修正方向

| 問題 | 後續必要控制 |
|---|---|
| Session 數量不能靠指定 ID 推定 | Hosted 呼叫維持已驗證 endpoint＋session；首次換版本／呼叫方式時，核對整個批准範圍的 session inventory 前後差異及 active 狀態，不能只看這兩個 session |
| 功能 controller 的 PASS 不含成本／所有執行限制 | 分開回報 functional、operational、cost 與 environment-continuity 狀態；未知不當 PASS，也不覆寫歷史偏差 |
| Memory 非同步操作可能重送 | 依 native update/search ID 去重記帳；poll 只讀同一 receipt，不為了取得漂亮結果重送 update；不重複累加 operator 基線 |
| 帳務 HTTP429／資料延遲 | 按服務 Retry-After 有界讀取；保留最後成功值的時間點及缺口，不算出假剩餘額，也不把估價當精確硬上限 |
| 保留資源仍可能計費 | 新舊 Foundry、ACR、Storage、NAT／PE／DNS、VM disk 等分開列保留目的；零 Hosted active／API replicas／VM deallocated 不等於整組零費用 |
| 原件不隨 Git 交接 | 提供經去敏的 evidence catalog、版本／digest／hash 與受控取得方式；不直接把 `.azure/`、credentials 或整包原始輸出加入 Git |

這些是防止多建資源、重送與誤報的必要控制，不要求為此重建整套企業治理平台。

## 7. 尚待決定，不以 POC PASS 補成已定案

- Hosted 的完整私人環境保存／恢復清單、session 到期與版本更新策略。
- SDK 自主 UI、SDK 跨替代 session／版本及自然 token 到期；已通過的文字循環不重列為尚未接線。
- M3 以外的一般成果生命週期、撤銷／多人批准；既有 Blob 版本與真人 SOP 閉環保留。
- Memory Preview、Non-VNet Foundry、保留期及資料處理區域是否符合正式環境要求。
- 本批 M4 以外的產品 Context 預算、Memory 治理、benchmark 工作集及品質／p50／p95 門檻；不重新決定已固定的五案門檻。
- 四槽的公平權重／互動保留策略與 wake 宿主；等待無活躍工作才釋槽、未知不釋槽及取消語義已由行為規格定義。
- 當期費用、保留資源去向及下一次精確測試的授權；原 US$100 不是新的預算或剩餘額承諾。

## 8. 官方契約與可重用程式

| 來源 | 本計畫採用的界線 |
|---|---|
| [Hosted overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)、[session 管理](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions) | per-session VM 隔離、compute 可更換、指定檔案持久；session 與 conversation 分開，session 有未活動保留期限 |
| [Runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract) | Invocations 不管理聊天歷史；SIGTERM 與持久 checkpoint／resilient task 有各自責任 |
| [Memory 概念](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-memory) | 語意抽取／合併、Preview、無 VNet integration；目前每 store 最多 100 scopes，4 active 不等於可建立無限私人 scopes |
| [Copilot SDK](https://github.com/github/copilot-sdk)、[Azure MI 指引](https://github.com/github/copilot-sdk/blob/main/docs/setup/azure-managed-identity.md) | SDK 驅動 CLI agent runtime；有 bearer-token provider 接法，但需固定實際版本及驗證，此 POC 尚未整合 |
| [VM 狀態與計費](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing) | 只用於歷史 controller 的費用判讀；deallocated 不代表磁碟／網路停止計費，不是新增 VM 的理由 |
| [共享 Context 實作](../src/fa-shared-context-poc/README.md)、[controller](../tests/shared_controller/README.md) | 延續已存在的介面、錯誤、身份與測試邊界；不將探針當成完整產品 |

官方文件於 2026-09-14 讀取。Memory 最新文件另列 item CRUD、TTL 與直接 remember／forget；本 POC 的 2.3.0 adapter 未覆蓋這些路徑。若後續需要，應另核對精確版本與權限，不以新功能宣告推翻既有 extraction 失敗，也不自動更新已固定依賴。
