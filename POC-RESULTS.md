# Foundry Hosted Sandbox POC 結果

**最新（09/17 23:39）：新版配置切換、執行前準備與新claim審核已完成，但r2真工作尚未成功。** 新工作只提交一次，現在停在 `capacity_uncertain`；原生readonly證據顯示assistant仍 `accepted`、沒有 `acquire` receipt、model requests為0，也沒有成果或交付回條。問題在SDK啟動前，尚未取得原HTTP錯誤碼；不能說又遇到同一個模型串流問題。原supervisor按上限停止，普通API／模型入口已關閉、min0；r2的C1責任與991／9991 budget、scans965保留，不重送或補額。先前「暫存路徑不可用」是錯誤推論，已撤回並由真部署排除。第一次SDK工作的取消報告其實已完成 `client_received`；下方17:29的「尚未交付」僅是當時歷史。詳見[r2原生嘗試](docs/evidence/sdk-r2-native-attempt-20260917.json)及[最新進度](docs/progress.md)。完整v1.2與Teams雙向仍未完成。

**最新（09/17 17:29）：原失敗 SDK 執行已按使用者明確授權取消，C1名額已釋放，資料／額度歷史保留。** 停止端點明確回 `session_already_stopped`，結合持久取消防重播與獨立讀回才封存原 attempt；不靠idle metadata、不捏造PID。工作結果為cancelled，模型回應仍unknown，沒有重跑或退額。取消結果尚未取得交付回條，成功SDK工作及Teams雙向流程仍未通過；目前一般API已停用。詳見[取消檢查點](docs/evidence/sdk-cancellation-recovery-20260917.json)。下段為取消前歷史。

**最新（09/17 16:16）：第一次真 SDK 工作已啟動，但未完成。** delivery 安裝機制、原 metrics 上傳／讀回與使用者明確人工審核已完成；`sdk-job-0917` 的第一次模型互動後停在 `sdk_failed / capacity_uncertain`，沒有成果、`finish_task` 或交付回條。原請求只有一筆消耗的模型發送權，未重送；C1未知槽位保留，budget993／9993、掃描餘額981未重設。普通接單已關閉、API降回min0，等待原執行的合法收尾／恢復；不得把SDK shutdown或部署前提當作業務成功。詳見[第一次 SDK 原生嘗試](docs/evidence/sdk-first-native-attempt-20260917.json)與[進度](docs/progress.md)。下段15:12狀態保留為歷史。

**v1.2 最新整合狀態（09/17 15:12）：SDK 配置切換、前置證據替換及獨立 controller 空循環已有真雲端結果，完整助理流程仍未完成。** 同 authority 的 `sdk-0917` 已經批准並只套用一次；後續修正跨版本 readiness 的替換阻擋，保留舊證據、276筆其他紀錄、220筆既有 receipts，以及995／9995的原 budget。獨立 controller 已完成兩次空循環並停止；這不證明有工作時的 timer redrive、責任接手或交付成功。Teams 真 incoming 個人路由已通過，成功後未再握手；沒有 Bot outgoing。一般接單仍關閉，delivery／inbox readiness、metrics／claim、真 SDK 模型工具循環、`finish_task` 及成果交付尚未通過。詳見[最新進度](docs/progress.md)與[SDK 前置證據檢查點](docs/evidence/sdk-readiness-supersession-20260917.json)。

**保護遷移檢查點（09/17 早上）已完成。** 已修正四項發布問題，以真 delegated operator 批准、一次 EGT 在同 authority 更新 protection／config pins；套用前後及獨立還原後，原資料、計數、期限與歷史的非 pointer 集合 hash 全部一致。原到期來源在獨立 operator 檢查中，還原前後皆真正被 `SOURCE_EXPIRED` 拒絕；readonly Recovery 從原 backup 還原49 bytes／hash 保真，不啟用原件。暫時 operator 與 Recovery revisions 已停用。這不延長舊 RuntimePolicy，也不代表一般 Agent API、真正 SDK 助理流程或 Teams 已完成。詳見[原生檢查點證據](docs/evidence/v12-protection-release-20260917.json)。

## 2026-09-16：六項缺口的限定 POC 功能驗收

以下為新增驗收，不改寫下方早期里程碑。六項的**指定功能情境**已有實作與環境證據；最後資源停用核對另記於[進度](docs/progress.md)。機器可讀的去敏索引見[原生驗收證據](docs/evidence/six-gap-20260916-native-progress.json)。

| 缺口 | 本輪已驗證內容 |
| --- | --- |
| M4 中斷接續 | checkpoint後停止、同工作／SDK session以新process恢復、重新讀正確來源、不重做既有draft。原失敗案保留，不冒稱任意crash恢復。 |
| 原件與必要材料 | Table／Blob原件、ID／版本／hash讀回、64 KiB二進位保真、獨立原件備份隔離還原與缺件／損毀拒絕。不是整套控制Table或跨區災難復原。 |
| 執行前把關 | 真delegated-user測試批准固定內容；錯digest拒絕；並行只一次效果／扣額；撤銷後拒絕；原ID核對；批准後alias替換不改實際內容；取消仍列已發生效果。範圍是固定受控backend Work修改，不是任意工具。 |
| 可靠交付 | 成果／取消報告均有持久outbox與實際`client_received`；sender重啟及ACK競爭只補交原bytes，不重跑工作。另以client-side fault injection模擬真server ACK後回應遺失，再由新程序查原責任確認。 |
| 背景與多agent | C4四個不同原生agent、第五筆排隊後完成；C1滿槽拒絕不扣額、lease到期不代表可接手、parent verified park讓槽、child完成、parent原checkpoint恢復generation2並交付。 |
| 記憶與摘要治理 | 回原件、來源／版本／review依賴、來源更正與撤回、claim及source真時鐘到期拒絕、私人scope檢查。來源bytes保留；同版本不能偷偷延長或移除期限。 |

**證據界線：** controller／client故障注入有明確標記，不當成Azure本身發生的故障；`client_received`不是真人已讀；四agent驗證不是CPU時間重疊證明。Memory仍是可重建的檢索層，不能稱作無損權威資料庫。未新增共享檔案系統掛載證據，亦不把這些結果當成完整FA產品或Teams通道驗收。

**收尾時發現的證據續期限制：** source-expiry改動沒有新增DELETE／GC路徑，但含程式hash的inventory內容位址已變。不能把語意相容當成相同inventory bytes。後續以舊pin續期的`c1-renew1`不納入本輪通過證據，入口已停用；重新啟用須採正確current pins或經驗證的同authority遷移。重複使用已存在source v1造成的409碰撞紀錄也保留，不計為新驗收成功。既有scope、計數與原始收據均不重設。

---

**結案後閱讀指引（2026-09-14）：** 本報告與 `docs/POC-GATE-RESULTS.json` 保留原實測結果；分析與新計畫見 [POC 分析與後續計畫](docs/POC-ANALYSIS-AND-PLAN.md)。使用者其後以 [ADR-0003](docs/adr/0003-hosted-environment-continuity.md) 改採 Hosted 環境／工作連續性，不要求同一 VM；這不把原本未驗證的私人環境或 SDK 能力改成 PASS。

**原件範圍：** 下述 `evidence/`、`.azure/` 路徑指原執行工作區的受控原件，未加入 Git；新 checkout 不自動具有這些檔案。報告中的 hash 是原執行者記錄，取得原件並核對前，不能聲稱已獨立重驗。

**結論：共享 Context POC 的 17 項功能驗收已在真正 Azure Hosted A/B 通過；不是完整 FA 產品驗收，也不是所有執行限制都通過。**

最後完整功能測試於 `2026-09-14 00:34:18–00:38:15 +08` 完成，耗時 **236.828 秒**，
17 gates／370 checks 全部 PASS。收尾另發現 azd 的服務名稱呼叫方式會建立多餘 session；
已停止、修正並做獨立唯讀回歸，詳見下方。**不能因此回頭宣稱全程遵守最多兩個 active sessions。**

決定與取捨見 [ADR-0002](docs/adr/0002-shared-context-without-file-share.md)。
使用者在 `2026-09-13T21:24:50.006+08:00` 明確授權完成整體 POC 與後續決定，沿用原訂閱、West US 3、原 resource group 與 US$100；固定 VM 仍列待驗證，G4 不自動加入。

## 現行方案與功能結果

精確工作資料放在 **Table**，長期偏好放在 **Memory Store**，每個 agent 透過 **Context API**
取得本輪需要的工作狀態、Team 記憶與自己的私人記憶。不是共享一份 RAM、完整聊天或 OS 目錄。

| 驗收範圍 | 真實結果 |
|---|---|
| 共享工作狀態 | A 建立、B 讀到相同資料；重複建立 409、非 owner 修改 403、缺 ETag 428、舊 ETag 412 |
| 衝突與交接 | 相同 ETag 的兩筆並行修改只有一筆 200、另一筆 412，revision 只加 1；交接後只有 B 能修改 |
| 長期記憶 | 3 次 native update 完成；B 找回 A 的 Team `sepia` 偏好，A 找回自己的 `indigo` 偏好 |
| 私人／Team 邊界 | B 讀寫 A 私人記憶及 receipt 被拒；跨 Team、body scope／owner 注入被拒；B context 不含 A 私人值 |
| 停止與恢復 | A 真正 `idle`，B 更新工作及 Team `coral` 偏好；A 同 session resume、boot marker 改變，仍取得最新工作與本人記憶 |
| 已發布成果 | revision 7、artifact version 3；A/B 的建立、修改、交接、再次發布共八種操作均拒絕，最終 ETag／內容／hash 不變 |
| 原生存取與 GUI | A/B 直接 Table／Memory 皆 HTTP403；A 同 sandbox 的非 root、headed Chromium、GUI 輸入、截圖與下載驗證、程序／暫存清理通過 |

Storage 的 403 可能來自私網限制，**不單獨證明 RBAC**。Memory scope 不是 ACL，私人／Team
邊界由 API 的 Entra token 驗證與 OID allowlist 強制執行。無 token 存取受保護 API 已回
HTTP401 `UNAUTHENTICATED`；`/healthz` 的 200 只代表啟動健康。

## 部署與取捨

| 元件 | 實際配置 |
|---|---|
| Foundry | `cog-fasample-memory-demo`／`faexample`，West US 3；public endpoint、Entra only、無 VNet injection |
| Hosted | `fa-context-a:5`、`fa-context-b:4`，各 2 vCPU／4 GiB，idle timeout 300 秒 |
| Context API | `ca-facontext-poc--poc4`，Consumption、0.25 CPU／0.5 GiB、min0／max1，專用 UAMI |
| 權威狀態 | `stfacontextpocdemo`／`workstate`，Shared Key 關閉，public access `Disabled`／`Deny` |
| API 私網 | `cae-facontext-private`、獨立 VNet、Table PE `Approved`，private DNS `192.168.96.68` |
| 記憶與模型 | `poc-team-memory`；`gpt-4.1-mini`／`2025-04-14`、`text-embedding-3-small`／`1`，GlobalStandard capacity10 |

Foundry project：<https://cog-fasample-memory-demo.services.ai.azure.com/api/projects/faexample>

Context API：<https://ca-facontext-poc.example-00000002.westus3.azurecontainerapps.io>

Hosted A5/B4 image：
`sha256:d1afd4674948ebe7fd176454c75503db1489f6002f3ef832dcb0534bfdf47420`

API poc4 image：
`sha256:a80f1076cf740e31f5dc6e515654e493bec0d997731ef1b92b1a423128aee11f`

API 的真實 ready revision、digest、env、OID allowlist、UAMI、HTTPS 與流量已讀回；不是只看 ARM
`Succeeded`。組織政策 `MCAPSGovDeployPolicies`／`StorageAccount_PublicNetwork_Modify`
強制關閉 Storage 公網，因此只讓輔助 API 接入私網，**Foundry 本身仍 Non-VNet**；
沒有豁免政策或把 Hosted 身分加進原生 Table／Memory 權限。

Memory 使用 `azure-ai-projects==2.3.0`、`api-version=v1`、
`Foundry-Features: MemoryStores=V1Preview`，仍是 **Preview**。GlobalStandard 不保證只在
West US 3 處理推論。依據：[Memory 概念](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-memory)、
[權限文件](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage#authorization-and-permissions)、
[Hosted Memory quickstart](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-memory-hosted-agent#prerequisites)。

## 已定位的失敗與修正

| 問題 | 修正與不能外推的界線 |
|---|---|
| Hosted HOME 無法寫入 | `/home/session` 由 root 擁有、0755，實際 UID10001；改為明確診斷，不讓 marker 寫入遮蔽 API 結果，不提升權限 |
| Native Memory scope | 前端拒絕 `:`，非同步 extractor 另拒絕 `/`；統一以 `sc1-`＋base64url 編碼合法邏輯 scope，原失敗 receipt 保留 |
| TLS 信任 | `trust_env=False` 曾忽略平台 CA；明確載入平台 bundle／system trust，保持憑證及 hostname 驗證，未用 `verify=False` |
| GUI | 精確排除 numeric loopback，縮短暫存路徑避免 `SingletonSocket` 超過 108 bytes；維持非 root 與 browser sandbox |
| CLI 回應格式 | azd 把 extension update notice 接在 JSON 後；只辨識實際已知通知，未知尾端資料仍失敗 |
| 記憶遺失 opaque 值 | 抽取確實省略 random verification code；改驗官方適用的長期 dashboard 偏好，原 FAIL 不改寫，精確值仍交給 Table |

**Memory 不是無損資料庫。** 成功案例的搜尋問題與工作 goal 沒有附答案，仍要求 native 搜尋
同時找回唯一 marker 與偏好值；沒有用原 payload 或 Table 回填答案。這只證明合成偏好能跨 agent
與程序恢復，不能外推為任意 UUID、驗證碼、整份聊天、長歷史品質或正式工作負載的可靠度。

### Session 數量偏差與收尾修正

完整功能 controller 雖然每次都傳入同一對 `--session-id`，但已安裝 azd extension 的
**service-name 路徑仍建立多餘 session**。收尾列出 A85／B52 筆記錄；先前完整測試窗口內有
50 筆額外 `active` 記錄，已全部執行 stop。不能把「只指定兩個 ID」當成「實際只有兩個 active」；
這是本次執行控制缺口，實際額外 compute／費用無法由記錄數推算。

有界 runtime 對照：服務名稱模式使 A 記錄數 84→85，直接 endpoint 模式 85→85。
controller 已改用從 `agent show` 取得的 `--agent-endpoint`，拒絕不符 agent／project 的 URL；
不升級 CLI、不重建映像。`2026-09-14T00:47:34Z` 使用修改後 controller 實際讀回 A/B runtime
及先前已發布成果，revision／ETag／artifact 完全相同，前後 session 集合維持 A85／B52，
**全部 idle，沒有新增記錄**。這是路由及持久成果的針對性回歸，不是重做或改寫完整 17-gate 報告。

## 費用、保留資源與未驗證項目

預算仍為 **US$100**，但**目前總額與剩餘額未知**。最後成功的延遲讀值為
`2026-09-13T08:44:48Z` 的 USD1.318733141815279205；
`2026-09-14T00:45:34Z` 查詢仍回 HTTP429、retry-after23。沒有把 429 當零元，也沒有保證帳單低於預算。

新方案共 8 次成功 image builds。五輪 controller 加最初 operator 探針，按 native ID 去重：
7 次 accepted updates（6 completed、1 failed）、10 次 accepted searches；回報的 tokens
為 input16679／output1070／embedding939，合計18688。失敗或被拒呼叫的未回報用量未知；
保守呼叫上界為 updates10／searches21，含負向測試，**不是帳單**。
路由診斷及最後唯讀成果驗證沒有 Memory 呼叫。

| 保留範圍 | 狀態與費用風險 |
|---|---|
| 新 Foundry、模型、Memory Store、Table／API／Basic ACR | 保留可重現；A/B 全部 idle，API 在 `2026-09-14T00:53:29Z` 實際 0 replicas，不是只看 min0 配置 |
| 舊 controller VM | `2026-09-14T00:50:36Z` 已讀回 `PowerState/deallocated`；磁碟仍可能計費 |
| 舊 NAT／PIP／PE／DNS／VNet、Premium ACR、Storage、Key Vault、舊 public Foundry | 未獲本次廣泛刪除授權，均保留；idle／deallocated 不代表這些費用停止 |
| 舊 VNet Foundry | `cog-fasample-vnet-demo` 已刪除；個別 ARM GET 在 `2026-09-13T14:09:32Z` 回 404 `ResourceNotFound`，未 purge |

舊 Foundry 刪除過程的 `GatewayTimeout`／`RequestConflict` 與 correlation
`00000000-0000-0000-0000-000000000010` 保留為歷史，不再是 blocker。
未刪整個 resource group、未新增排程。程式與文件的發布狀態以本 repository 的 Git 記錄為準。

固定 VM、完整私人環境／browser profile、RAM snapshot、正式 FA 與 10 倍效能仍未驗證；
G4 `NOT_RUN_OPTIONAL`。HOME `unavailable` 不是持久化成功。原 SMB G0 `BLOCKED` 保留，
不再執行剩餘94輪，更不能把本次 Context PASS 當作 OS mount PASS。

## 重現與證據

實作：[API／Hosted README](src/fa-shared-context-poc/README.md)；
執行：[controller README](tests/shared_controller/README.md)；
機器可讀摘要：[POC-GATE-RESULTS.json](docs/POC-GATE-RESULTS.json)。
本機 runtime 133 tests、最新 controller 20 tests 通過；雲端證據才是上表功能判定依據。

完整成功 run：`sc-9a84c17c014b4c3a99bbb3459f5cbb4d`，165 個證據檔、494607 bytes。
原件在 `evidence\shared-context-cloud-20260913-preferences\sc-9a84c17c014b4c3a99bbb3459f5cbb4d\`，
`report.json` SHA256：
`fc6a949efa3aa961b0ed41f29627c04f0317b6c636b98b5f921d5f9db3d683da`。
最終 artifact SHA256：
`8df2ef82b70f6c9ff90d4788c4a418506552ac0cfb93ef4612394464afeeb28a`。

GUI `after` PNG 已視覺確認輸入、`GUI_COMPLETE` 及下載，SHA256：
`9de905196a35a479c84848c12b06b145bfa3231513e525b170312ab84deaf066`。
畫面保留 Chromium 對 `--host-resolver-rules` 的警告；不是使用 `--no-sandbox`。

路由回歸：
`evidence\shared-context-endpoint-closeout-20260914\sc-668dc3d214c841adbb2e26f46d5efbfc\report.json`，
SHA256 `00e83cff68e993e23f458516f426e46a6f217eed77a3e68ee2518baf15af1550`。
`.azure\shared-context-final-evidence-manifest.json` 保存逐檔雜湊，
`.azure\shared-context-final-usage-ledger.json` 保存去重用量；這些 raw evidence／本機狀態不加入 Git。

失敗 runs `sc-fe105805c7814f698f0d81bbc316ccfd`（冒號 scope）、
`sc-1b5d6b6c67a64dbfb605c8bb356d5417`（extractor／GUI）、
`sc-5babb8b1de184cc18d0bf0753d310f4c`（CLI 尾端通知）、
`sc-1610b743ebcd4c59a78c1d49e34326c7`（opaque 值未被抽取）均保留，不覆寫成成功。
初始 operator 單一 sepia scope 的 1update／1search、2093tokens 與 native IDs
保存在機器可讀結果及 `.azure\shared-memory-compatibility*.json`，沒有重送該 update。

<details>
<summary>歷史 SMB 與早期環境紀錄（非後續執行計畫）</summary>

## 歷史 SMB 診斷：已完成 6／100 輪，現已停止此方向

當時結論：G0 未通過，問題縮小到 Linux kernel 喚起憑證 helper 的流程。Hosted v7 能取得 MI 票證，真正 mount 仍失敗；不是單純沒有 root 或少加一個 Azure Files 角色。

使用者批准最多 100 輪必要的修正、映像建置／部署與測試，取代先前三次 build 限制。
此次用了 **6 個單 session 測試、3 個新映像／版本**；不是已執行 100 次。
原 US$100、最多兩個同時 active sessions、每段 active window 最多 60 分鐘等限制不變。

| 輪次 | 映像／版本 | 實際結果 |
| --- | --- | --- |
| 1 | `dse`／v5 | native 取票回 `-1`，尚未執行 mount。 |
| 2 | 同一 v5 | 不改設定重測一次，仍在 native 取票失敗。 |
| 3 | 原始 `dsd`／v4 對照 | 同樣取票失敗，排除「只有新 upcall wrapper 才會出錯」的說法。 |
| 4 | `dsf`／v6 | 失敗後的 UID0／effective UID10002 都能解析私有 IP、連 TCP445，並完成 HTTPS／TLS；未帶認證的 HEAD 回 HTTP400。這只證明連線，不是登入成功。 |
| 5 | 同一 v6，首次 mount 前等待 20 秒 | native 取票成功；真正 mount 回 `errno=2`。container 內 helper／路徑存在，但 upcall wrapper 沒有 entry／exit 紀錄。 |
| 6 | `dsg`／v7，同樣等待 20 秒 | 再次取票成功、mount `errno=2`。額外匿名 keyring 對照：自行加入 dummy key 成功，要求 kernel 喚起 helper 的 `request_key()` 卻回 `ENOENT`。 |

**關鍵證據：** v7 的 `/team`、`/sbin/request-key`、Python、`cifs.upcall` 都存在；
request-key 設定精確相符，credential cache 為 UID10002／0600。
kernel 記錄 `session_setup_failed=-2`、`mount_failed=-2`；
獨立子程序的 `local_add.status=success`、`unlink_return_code=0`，
但 `request_key.errno=2`，且 CIFS wrapper 無紀錄。
dummy key 不含認證資料，只在新建匿名 keyring 中操作，沒有讀取其他 keys。

依 [Linux 官方 Key Request Service](https://docs.kernel.org/security/keys/request-key.html)，
這條路徑需要 kernel 喚起 `/sbin/request-key`，再由它執行 helper。
**container 看得到檔案，不等於 kernel 的呼叫環境也看得到。**
目前證據支持 kernel→user helper 呼叫環境的阻塞；尚未證明究竟是哪個平台路徑、
loader 或 namespace 設定缺失，不能外推為所有 Foundry 模式都不支援 mount。
沒有讀取 host root、切換 host namespace、修改 kernel 或擴大權限。

前四輪的取票失敗另有啟動時序因素：兩輪加上 20 秒等待後成功。
這是對照證據，不是官方 readiness 保證，也沒有把固定 sleep 當成正式修復。
同樣沒有把票證存在、HTTPS 或 Storage REST 成功當成共享掛載通過。

**此處停止盲目重測：** 剩餘 94 輪不應消耗在相同錯誤。
下一個必要前提是由 Foundry 團隊確認這個 Hosted runtime 的
`request_key`／`cifs.spnego` helper 呼叫支援與可用設定；不是再增加 Azure RBAC。
G0 保持 BLOCKED，G1／G2／本版本 G3 為 NOT_RUN_DEPENDENCY；
先前獨立 G3 PASS 仍保留，固定 VM 仍為 DEFERRED_NOT_VALIDATED。

最新映像：
`sha256:3798fc597de79fa53f0673c21dabc55547a50617dacd7a5af5970b5c24bc0e18`，
407355530 bytes，Hosted v7／runtime `00000000-0000-0000-0000-000000000011`。
最新 run `mi-diagnostic-round6-20260913`，
`2026-09-13T10:08:40Z`–`10:09:19Z`。
六個 session 在 `10:12:30Z` 收證時全部重新讀回 `idle`；
每輪測試後 controller 均已 deallocate；最終收證後也已重新讀回 `PowerState/deallocated`。
各輪 credential cleanup 均成功，沒有保存 token／ticket 內容。
console 皆保留收到的內容，但以 read timeout 結束，不能稱完整 console。
證據見 `evidence\mi-smb-20260913\round1-*` 至 `round6-*`。
六輪原件另合併於
`evidence\private-controller-mi-diagnostic-campaign-20260913\mi-diagnostic-campaign-20260913\campaign.json`，
144409 bytes，23 個內嵌原始檔皆逐一核對 SHA256。
整份 ZIP 為 17166 bytes，SHA256：
`0505c11912ed3f39800b786687dd8c073623c15c39389a34279e453f8f6d2efb`。
收證根目錄其他 `sessions-after-*.json` 是歷史 metadata；本次狀態以 campaign 內六個 session 的讀回為準。

此次未改 Storage、RBAC、OS、kernel 或套件版本，未執行 G4、commit、push 或刪除資源。
累計 16 次 cloud builds：13 成功、3 次先前失敗。
最近一次成功的延遲入帳費用仍為 `2026-09-13T08:44:48Z` 的約 USD1.32；
收尾 `10:17:43Z` 的 Cost Management 仍回 HTTP429，不能當作費用為零或保證最終帳單。
保留的 NAT、PE、ACR、磁碟等仍可能計費。

## v4 的歷史紀錄

**當時進度：Hosted 已取得身分憑證，仍未掛上共享資料夾。**
v4 的 `probe_environment` 成功；接著 `mount_shared` 的安全日誌記錄預期 MI
`aud`／`oid`／`tid`、native helper `1.0.12`、`refresh_action=refreshed`，
以及 owner `10002`／mode `0600` 的 credential cache。
真正 mount 隨後回傳 **process exit32／`mount_errno=2`**，原因尚未定位。
這不能只憑 cache 存在就算 SMB 登入或掛載成功，也不是舊 account-key 的 errno13 結果。

外層 Foundry 回應是 HTTP400 `server_error`，但原始 console 證明程式正常啟動、
probe 已成功、失敗發生於 mount；不是單純 session 啟動失敗。
原始 mount stderr 在 MI 模式中被安全隱藏，因此尚缺更精確的 kernel／upcall 階段證據。
A 未通過，所以 B 沒有執行 mount，G1/G2/本映像 G3 均未跑。

本輪 `vnet-mi-g0-20260913-1`：`08:52:27Z`–`08:53:09Z`。
兩個 session 已停止並讀回 `idle`，controller 已於 `09:02:54Z` 讀回 `PowerState/deallocated`。
憑證清理回報成功；沒有保存 token 或 credential cache 內容。
證據 ZIP 9063 bytes，SHA256
`711b21477517a0dad6f01c995035e92e20bd7f11b693c0dd5e53b4df02c19ce4`。
A/B console 分別保留 10198／6202 bytes 後讀取逾時，不能稱完整 console。
摘要見 `evidence\mi-smb-20260913\hosted-g0-proof.json`；原件見
`evidence\private-controller-vnet-mi-g0-20260913-1\`。

**MI／SMB OAuth 映像與設定：** `dsd` 於
`2026-09-13T08:39:10Z`–`08:40:29Z` 成功，實際 Linux 檢查通過：
NativeLibrary `1.0.12`、原生日誌確實導向 `/dev/null`、映像不含 credential cache，
以及 `cifs.upcall 7.4`。這只是映像相容性，不是 Azure 登入或共享掛載證據。
Storage 的 SMB OAuth 已啟用，既有 Hosted runtime 的限定 MI Admin 角色也已讀回；
v4 已 active，runtime principal 與批准對象相同，尚無 MI 掛載成功證據。
設定的正確 ARM 欄位是
`properties.azureFilesIdentityBasedAuthentication.smbOAuthSettings.isSmbOAuthEnabled=true`。
第一次讀回檢查誤用了 CLI 參數名稱而報錯，原始回應已證明設定成功，沒有重送變更。

先前 `dsa` 是我寫錯 upcall 版本旗標；`dsb` 是套件 postinst 沒選到映像 Python，
已透過明確的 `DPkg::Path` 修正，沒有開啟 pip fallback 或升級 SDK；
`dsc` 已通過 native 初始化，但 `mount.cifs -V` 回傳 exit2，舊檢查沒有印出 captured stderr。
最後改用套件 metadata 查版本，保留真正 mount 給 Hosted G0，並讓命令失敗會輸出有界診斷。
**當時追加批准的 3 次 builds 已全用完。** 當時總計 13 次：10 成功、3 失敗；後續授權以上述 100 輪為準。
成功 digest：`sha256:10b6f915a0f425c93e7acd056d536bd704cdb49e4059693f94ebff08364e95b9`，
407301066 bytes。完整證據保留於 `evidence\mi-smb-20260913\`。

**先前 account-key 診斷：** v3 的真正 Hosted kernel log 收到
`0xc0000022 STATUS_ACCESS_DENIED`／`Send error in SessSetup = -13`。
不是單純「沒有 root」；Storage 的 `allowSharedKeyAccess=false` 與該 account-key SMB 認證路徑有衝突疑點。
尚未以政策對照或現行 key 比對證明唯一根因，因此不改政策、不宣稱 mount 已可行。

實測依據：POC 規格 v1.0（`c5f7a03`）。執行日期：2026-09-12 至 2026-09-13 UTC。
2026-09-13 已同步 [POC 規格 v1.1](docs/FA-Hosted-Sandbox-POC-Spec.md)（`091cfd3`），以下補列新版產品原則的相容性；不回改既有實測結果。
第一輪已停止；後續依使用者批准完成 VNet 部署並恢復探索。沒有執行 G4、模型部署、delete、commit、push 或排程。

## 0. 產品原則相容性與接續決定

[設計決策 DP-02](docs/adr/0001-persistent-agent-computer.md) 要求每個 agent 長期綁定同一台 VM，只有故障或明確維護才換機。
新版規格指出：Hosted 標準 idle 釋放 compute，resume 配置新 compute，與此要求不相容。
這是**文件契約落差，不是本 POC 的實測失敗**；不能把未執行的 G2 改成 FAIL，也不能外推為所有 Foundry 模式都不支援。

G3 PASS 仍只證明測試配置的同 sandbox 桌面／瀏覽器能力；固定 VM、完整持久環境、長期記憶與 OpenClaw 10 倍效能均未驗收。
即使未來 G0–G4 全數通過，也不代表上述產品原則已滿足。VNet injection 可處理網路前提，不能解決固定 VM 的生命週期契約落差。

**2026-09-13 10:34 +08，使用者明確要求先把固定 VM 列為待驗證，基於目前 Hosted 能力完整執行 POC。**
因此恢復 G0–G3 的 VNet 準備與執行流程，不再以固定 VM 相容性作本次探索的前置阻擋。
固定 VM 仍未驗證，不宣稱符合 DP-02；此次決定不等於正式 runtime 採用或產品驗收。
使用者已另行批准精確 VNet／私有控制 VM／窄 scope RBAC 計畫；`Microsoft.ContainerService` 已註冊。
部署前驗證已完成；`fasample-vnet-20260913-1` 已於 `2026-09-13T03:30:33Z` 開始。
該 deployment 最終只有 registry connection 失敗：
`ValidationError: Credentials Property can't be empty for auth type RegistryIdentity`。
新 Foundry account／project／capability host／網路均已建立，四個 PE 連線均 `Approved`，
account 已讀回正確 `networkInjections` 與 `publicNetworkAccess=Disabled`。
`fasample-vnet-complete-20260913-1` 已成功完成 connection 與 controller，沒有重建可用的 account/VNet。
修復依官方 azd RegistryIdentity CREATE 範式補上 credentials；新 project 的 agent v1 使用第一輪 v5 的相同 immutable image。
**新 VNet G0 已到達 mount 階段**：Hosted 成功取得 Key Vault secret，Azure Files DNS 為 `10.0.0.85`，TCP 445 可達，
但 `mount.cifs` 回傳 `mount error(13): Permission denied`（process exit 32）。
這排除了第一輪的 Key Vault 網路阻塞，卻尚不能區分 SMB 認證／Storage 政策與 runtime mount 權限；不逕行判定 Hosted 不支援 mount。
該 run 的 A/B 均已 stop，且再次列出時都為 `idle`、`has_more=false`；
controller 已讀回 `PowerState/deallocated`，等待診斷期間不保留 VM compute。
舊 Foundry 未刪除；原 US$100 預算不提高，保留的既有資源仍可能持續計費。

**後續診斷已獲批准：** 使用者允許在 G0 通過前新增**一個**診斷 image／Hosted version。
它繼承既有 v5 digest 的全部系統／Python 套件，只更新探針，補 credential 來源／格式與有限的 CIFS kernel 訊息。
不輸出 key／key hash、不加權限、不改政策或降級加密；若掛載通過，以同一版本跑 G1–G3。
此唯一診斷 build 已完成（`ds8`），並在新 project v2 執行；mount 仍未通過。
Credential 來源／格式／檔案檢查已有結果，但 kernel 探針的 `dmesg` 參數不相容，診斷尚未完成。
不把探針錯誤當成 Hosted 平台限制；使用者在得知此缺失後批准的修正版已由 `ds9` 建置並以 v3 執行。
v3 已取得有效 kernel 診斷，確認錯誤發生在 SMB session setup。

## 1. 第一輪逐項判定

| Gate | 結果 | 實測範圍與限制 |
| --- | --- | --- |
| G0 原生 Azure Files mount | **BLOCKED** | A 在讀取 Key Vault secret 時收到 `ForbiddenByConnection`，尚未執行 `mount`。B 已建立，但此 run 未進行 B 的 mount。 |
| G1 跨 session 同一份檔案 | **NOT_RUN_DEPENDENCY** | 沒有已確認的 CIFS mount，未執行共享讀寫、owner 交接或 rename 驗收。 |
| G2 真正 idle／resume 與重掛 | **NOT_RUN_DEPENDENCY** | 依賴 G0/G1；未測 compute resume、platform-auto 或 application-remount，`remount_mode=unknown`。 |
| G3 同一 sandbox 的桌面與瀏覽器 | **PASS** | Hosted v5 的 Xvfb、Openbox、non-root headed Chromium、OS GUI 輸入／按鈕啟動、前後畫面、下載內容/hash、程序退出均有證據。 |
| G4 Copilot SDK | **NOT_RUN_OPTIONAL** | 不在批准範圍，沒有部署模型或執行模型 eval。 |

G3 沒有把成果發布到 `/team`；跨 session 讀取瀏覽器成果仍是未執行的依賴項。
本輪不能作為「跨 agent 共用檔案已可行」或正式採用原生 Hosted 路線的依據。

## 2. 精確環境與版本

| 項目 | 實際值 |
| --- | --- |
| Subscription | `00000000-0000-0000-0000-000000000012` |
| Tenant | `00000000-0000-0000-0000-000000000013` |
| Caller | `admin@contoso.onmicrosoft.com`，專案隔離 `AZURE_CONFIG_DIR` |
| Region | `westus3`；原指定 West US 2 不在當時 Hosted 支援清單，經使用者改選 |
| Resource group | `rg-faexample` |
| Foundry account / project | `cog-contosodemo01` / `faexample` |
| Agent | `fa-hosted-sandbox-poc`；最後部署 v5，狀態 `active` |
| Runtime identity principal | `00000000-0000-0000-0000-000000000014` |
| Protocol / port | `invocations` `2.0.0` / HTTP `8088` |
| Session resources | 每個 session 2 vCPU、4 GiB；最多同時兩個測試 session |
| Idle timeout | `300` 秒 |
| azd / agents extension | `1.32.0` / `1.0.0-beta.13` |
| G3 OS / browser | Debian GNU/Linux 12、Chromium `152.0.7977.82` |
| G3 desktop | Xvfb `2:21.1.7-3+deb12u13`、Openbox `3.6.1` |

Project endpoint：
`https://cog-contosodemo01.services.ai.azure.com/api/projects/faexample`

G3 的 immutable image reference：

```text
crcontosodemo01.azurecr.io/fa-hosted-sandbox-poc/fa-hosted-sandbox-poc-faexample@sha256:d6f8f7134abc4b2cf5089ecc2c5e1de2a6cf1b189bd962afb547f668c51bab01
```

ACR 回報 `imageSize=381763894` bytes（約 364.1 MiB；不是容器解壓後的容量）。
Base image 為 `python:3.12.10-slim-bookworm`，建置記錄的 digest：
`sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db`。
原始碼 hash 保存在 `evidence/inventory/v5-source-hashes.json`。
直接 Python 依賴在 `requirements.txt` 固定版本；Debian apt 與轉接依賴未做完整 snapshot lock，
所以**重現本次測試應使用 image digest**，不能假設未來從 tag 重新 build 會產生相同 bytes。

## 3. 第一輪 G0：阻塞在合法網路前提，不是 mount syscall

代表 run：`20260912T163232Z`，agent v2。
觀察時間：`2026-09-12T16:32:38.947122Z` 至 `2026-09-12T16:32:41.745713Z`。

| Alias | Session ID | 本 run 的操作 |
| --- | --- | --- |
| A | `0e19a34b0f0c5deb00ejEyZh8zMGlqdqEVrGEduLYKRhE2RnxJ` | environment probe 成功；讀取掛載所需 secret 失敗 |
| B | `051889829fe7c13800UrR2OQtPndjYS79RXmJmB6uAYKCOh8jv` | 已建立；A 失敗後沒有繼續 |

G0 image digest：
`sha256:ce95cf22c6bd257b365e5829f5c33b9df585421a8c43908a01b352807c709dae`。

A 的環境觀察：kernel `6.12.8+`、UID/GID `0`、
`CapPrm/CapEff/CapBnd=000001ffffffffff`，filesystem 清單有 `cifs`／`smb3`，
`/dev/fuse` 不存在，`/team` 未掛載。這些資訊**都不是 mount PASS**。
此呼叫在 secret lookup 就停止，沒有取得這次 mount 的 DNS、TCP 445 或 syscall 結果。

當時 container console 的診斷摘錄：

```text
ForbiddenByConnection
Public network access is disabled and request is not from a trusted service nor via an approved private link.
```

當時錯誤中的 caller `appid`／`oid` 均為上述 runtime identity。
重新讀回 RBAC 已確認它在 dedicated vault 上有 `Key Vault Secrets User`；
因此不能繼續把這個錯誤解釋為等待 RBAC propagation 即可解決。

本輪管理面讀回：

| 資源 | 實際設定 | Policy 證據 |
| --- | --- | --- |
| Key Vault | `publicNetworkAccess=Disabled`，沒有已配置的 Private Endpoint | `MCAPSGovDeployPolicies` → `keyvault_publicnetwork_modify` |
| Storage | `publicNetworkAccess=Disabled` | 同 assignment → `storageaccount_publicnetwork_modify` |
| Storage | `allowSharedKeyAccess=false` | 同 assignment → `storageaccount_disablelocalauth_modify` |

以上 assignment 繼承自 tenant management group。Storage 欄位是觀察到的配置差異；
**本輪未到 SMB 認證階段，不把欄位值當成已重現的 SMB 拒絕**。
沒有建立政策豁免、增加權限、外掛 privileged device、VNet 或其他 backend。

證據：

- `evidence/20260912T163232Z/run-manifest.json`：平台實際回 HTTP `400`、generic `server_error`。
- `evidence/inventory/g0-diagnostic-transcript-excerpt.txt`：先前會話保留的 console 摘錄，**不是完整 log export**。
- `evidence/inventory/g0-v2-console.log`：停止後再取 log，只有 `session_state=Stopped` 與 `stream_interrupted`；沒有假裝取回原始 stack。
- `evidence/inventory/key-vault.json`、`key-vault-policy.json`、`storage.json`、`storage-policy.json`、`agent-vault-rbac.json`：本輪重新取得的管理面證據。

### VNet 接續 G0（2026-09-13）

| 項目 | 實際結果 |
| --- | --- |
| Run / UTC | `vnet-g0-20260913T0414Z`；實際開始 `2026-09-13T04:07:39.916821+00:00`，ID 僅為標籤 |
| 新 project | [cog-fasample-vnet-demo / faexample](https://cog-fasample-vnet-demo.services.ai.azure.com/api/projects/faexample)（僅私有網路可達） |
| 新 agent version / image | v1；`sha256:d6f8f7134abc4b2cf5089ecc2c5e1de2a6cf1b189bd962afb547f668c51bab01`，與舊 project v5 相同，不是新 build |
| Hosted principal | `00000000-0000-0000-0000-000000000011`；只在原 vault 新增 `Key Vault Secrets User` |
| A / B | `0458096c0015e2a200JK7UtCagSkJHhgHSSgDV2BrMdIDkv5hL` / `07940de277b78fea00Br8Mofs68nkClkenwUv66AczcziiXDUJ` |
| Hosted environment | UID 0、kernel `6.12.8+`、cifs/smb3 存在、`/team` 未掛載 |
| Hosted 網路 | 已通過 secret lookup；mount 診斷含 Azure Files DNS `10.0.0.85`、`tcp_445=true` |
| 真正 mount 結果 | process exit 32；`mount error(13): Permission denied` |
| 判定 | **BLOCKED — SMB／mount 拒絕原因待釐清**；不能只由 error 13 判定缺少 `CAP_SYS_ADMIN` 或 kernel 不支援 |
| 清理 | A/B 的 stop API 都回 `stopped=true`，其後讀回都為 `idle`；controller 已 deallocate |

外部 Invocations API 把應用錯誤換成 generic HTTP 400；因此原始 manifest 的
`ENVIRONMENT_OR_APPROVAL` 不是根因診斷。Container log 中的
`MOUNT_PRIVILEGE_OR_KERNEL` 也是既有程式的粗略分類，不等於已證明的平台限制。
完整 console 已在 stop **之前**擷取，證據位於
`evidence/private-controller-vnet-g0-20260913T0414Z/vnet-g0-20260913T0414Z/`。
取回 ZIP 的 SHA-256：`7eca79af4b30e5f5ca5dbe339017b882d1c164b35ce45ec85fa98b77d9044555`。

Storage 目前仍為 `allowSharedKeyAccess=false`、`enableHttpsTrafficOnly=true`，
`protocolSettings.smb={}`；未改政策、開 public access 或降級 SMB 加密。
針對新 Hosted principal 查詢 `team-poc` share scope（含 scope 繼承）未找到角色，
在原 vault 則確認有 `Key Vault Secrets User`，證據 `runtime-file-and-vault-roles.json`。
目前是「managed identity 讀 Key Vault，再以 account key 做 SMB 認證」，不是 managed identity 直接登入 SMB。
因此不能只因沒有 Azure Files RBAC 就認定根因，也不以盲目新增 SMB share 角色取代認證診斷。
[Microsoft 的 Linux error 13 文件](https://learn.microsoft.com/troubleshoot/azure/azure-storage/files/connectivity/files-troubleshoot-smb-connectivity)
列出加密、網路、NTLM、key-access policy 等多種原因，不能只憑同一錯誤字串下結論。

### 診斷版 v2 G0（2026-09-13）

唯一追加 build `ds8` 成功，時間為 `05:07:56.294805Z`–`05:08:23.240853Z`；
新 immutable digest 為 `sha256:2e0f8e9013618ee911a3b26243cd14faefb2e21969885af7fac017d6bb9d9be1`。
它繼承 v5 的依賴，只替換 `poc_core.py`；新 project v2 已讀回 `active`，
runtime principal 仍為 `00000000-0000-0000-0000-000000000011`，未增加角色或更改政策。

Run `vnet-g0-diagnostics-20260913-1` 於 `2026-09-13T05:14:21.625687+00:00` 開始，
A 的 mount 呼叫在 `05:14:35.568955+00:00` 回傳 generic HTTP 400；
停止前的 console 保存了實際 `mount error(13): Permission denied`，process exit 32。
新的分類為 `MOUNT_ACCESS_DENIED_UNCLASSIFIED`、`denial_origin=unknown`。

| 檢查 | 實際結果與界線 |
| --- | --- |
| 執行權限 | UID/GID 0；`CapEff=000001ffffffffff`，包含 `CAP_SYS_ADMIN`。不能只歸因為「不是 root」 |
| 私有網路 | Hosted Files DNS `10.0.0.85`，`tcp_445=true` |
| Credential 來源 | `key_vault`；secret version `00000000000000000000000000000000`，沒有 environment override |
| Key 格式 | 無 quotes、CR/LF、NUL 或 whitespace；有效的 64-byte base64。**格式正確不代表內容一定等於 Storage 現行 key** |
| Credential file | UID/GID 0、mode `0600`、regular file、process 可讀、內容符合預期；已刪除 |
| Kernel 診斷 | before/after 均 return code 1：`dmesg: unknown time format: raw`；沒有取得 kernel 訊息 |
| 判定 | **G0 BLOCKED**；G1/G2 未執行。G3 保留先前 v1 的獨立 PASS，不把它記為 v2／共享成果 PASS |

`dmesg` 的錯誤是**本次診斷程式參數有誤**，不是 kernel log 權限被拒絕。
既有 mock 測試未驗證真實 CLI 支援的參數，建置時只有 Python compilation，也未捕捉此相容性問題。
控制 VM 的 util-linux `2.39.3` help 另確認 `--time-format` 僅列出
`[delta|reltime|ctime|notime|iso]`；這只是 CLI 契約對照，不是 Hosted mount 證據。
因此 SMB 認證／服務政策／runtime 限制仍未能區分，不宣稱共享掛載不可行。

本輪 A/B：
`008e52c5670b4f1200QHljkM7Kn6aZZIdQl7jDZUntXUgJYc16` /
`0c5f3c6edd6e57ad00ymW66uIXIWT4yMurISWHXJb3JB3pr2gU`。
兩個 stop API 均回 `stopped=true`；`05:22:39.085881+00:00` 再列新 project 的五個 sessions 全為 `idle`、
`has_more=false`。控制 VM 最後再次讀回 `PowerState/deallocated`。
本輪 ZIP 只包含該 run 與根層 metadata，SHA-256：
`67f3c4123445d187128d02c697701e9607b311a119b55dce7fd221c2f326e8be`。
證據位於 `evidence/private-controller-vnet-g0-diagnostics-20260913-1/`；
build、角色、session 狀態與最後 VM 狀態位於 `evidence/review-20260913T022914Z/`。

### 修正版 v3 G0：SMB session setup 拒絕

追加批准的 build `ds9` 於 `2026-09-13T05:38:21.993271Z`–`05:38:47.236116Z` 成功。
它移除不支援的 `--time-format=raw`，保留原有 timeout／redaction／輸出上限；
建置時以映像內真正的 `dmesg` 驗 CLI 參數與合成 log 時間戳，不只依賴 mocks。
這些建置檢查不是 mount 證據；下列結果才來自真正 Hosted runtime。

| 項目 | 實測值 |
| --- | --- |
| Run | `vnet-g0-corrected-diagnostics-20260913-1` |
| UTC | `05:44:36.275610`–`05:45:19.127827`，2026-09-13 |
| 新 project agent version | v3，`active`；CPU `2`／memory `4Gi` |
| Image digest | `sha256:c3861e70477956cd341547ad5d75fc8c84a8bf7f6da36aa7fd4d771104563bfe` |
| Hosted identity | 與 v1/v2 相同，未加角色或權限 |
| Key／credential file | 仍為相同 Key Vault secret version；格式有效、UID/GID 0、`0600`、可讀且內容符合預期、已刪除 |
| Hosted Files 網路 | `10.0.0.85`、TCP 445 成功 |
| dmesg | 前後均 return code 0，`status=available`；六行新增 CIFS 訊息，未截斷 |
| Mount | process exit 32、errno 13，未完成掛載 |

本次 kernel 訊息：

```text
CIFS: Attempting to mount //stfaexampledemo.file.core.windows.net/team-poc
CIFS: Status code returned 0xc0000022 STATUS_ACCESS_DENIED
CIFS: VFS: \\stfaexampledemo.file.core.windows.net Send error in SessSetup = -13
CIFS: VFS: cifs_mount failed w/return code = -13
```

**判讀：CIFS 已進入 SMB session setup 並收到拒絕，不是僅憑 mount 工具的 Permission denied 猜測。**
這足以把當前失敗定位到 SMB 連線建立／認證階段，不能再把它直接歸因為使用者不是 root。
但沒有成功 mount，仍不能驗收 G0，亦不保證修正認證後就不存在其他 runtime 限制。
程式原始 `denial_origin=unknown` 保留不回改；上述是根據本輪 kernel 訊息的人工判讀。

管理面再次讀回 `allowSharedKeyAccess=false`、`publicNetworkAccess=Disabled`、
`azureFilesIdentityBasedAuthentication=null`，SMB `authenticationMethods`／`channelEncryption`／`versions` 等未設定。
[SMB 官方文件](https://learn.microsoft.com/azure/storage/files/files-smb-protocol)
說明未變更時 null 表示使用預設值，不能把 null 解釋成已停用 NTLMv2 或只允許 AES-256-GCM。
[Linux error 13 官方排障文件](https://learn.microsoft.com/troubleshoot/azure/azure-storage/files/connectivity/files-troubleshoot-smb-connectivity)
明列「Storage account key access is disabled or disallowed via a policy」為原因之一，建議改用身分式認證。
因此**既有 key-access policy 是目前最有力的設定疑點**，但本 POC 沒有改政策作對照，
也沒有比對 secret 與 Storage 現行 key，不能宣稱已證明唯一根因。
新增 SMB Share Contributor 並不會把目前 account-key mount 自動轉成 Kerberos。

G1/G2 仍為 `NOT_RUN_DEPENDENCY`；本輪未重跑無關的桌面 G3，先前 v1 的獨立 G3 PASS 保留。
固定 VM 仍為 `DEFERRED_NOT_VALIDATED`。要繼續共享 gates，須先確認組織允許的 SMB 認證方案，
而不是繼續增加 root 權限、Private Endpoint 或同樣的診斷 build。

A/B 為 `0ca4684d353a7ab900OP6dbiLR80cMmmJQQfctxaN5v8IW2ss1` /
`0a4158feef49904200RA8sfLBeiK5qzVc0uWkc3urjyHNBvArc`；兩個 stop 均成功。
`05:45:40.729626+00:00` 讀回新 project 七個 sessions 全為 `idle`、`has_more=false`；
控制 VM 已讀回 `PowerState/deallocated`，未刪除資源。
ZIP 為 10,386 bytes，SHA-256：
`6a8d84b229a1ca2ada58e1267ec27c54e45bee682ba9aa6f578093e5747787bb`。
原始資料位於 `evidence/private-controller-vnet-g0-corrected-diagnostics-20260913-1/`；
摘要 `corrected-diagnostic-mount-details.json` 與 management read-back 位於 `evidence/review-20260913T022914Z/`。

## 4. G3：真正 Hosted 桌面通過

| 項目 | 證據 |
| --- | --- |
| Run | `g3-20260913T0133Z`（ID 是標籤；實際時間以下列 UTC 為準） |
| Agent version | `5` |
| Session A | `030cd3cfabd8fcf600XI0ZXJeIqG5LmRMVNMHQv2r7P2ZAFGQe` |
| 全流程 UTC | `2026-09-13T01:31:50.806871Z` – `2026-09-13T01:32:16.926156Z` |
| Desktop invocation UTC | `2026-09-13T01:31:56.207538Z` – `2026-09-13T01:31:59.637304Z` |
| Nonce | `f7ab6e8c06b4982320b1e9b6` |
| DISPLAY / loopback | `:90` / `http://127.0.0.1:41565/` |
| PIDs | server `7`、Xvfb `58`、Openbox `59`、Chromium `60` |
| Browser identity | UID `10001`，沒有 `--headless`、`--no-sandbox` 或 remote CDP |
| GUI 動作 | `xdotool` 真正輸入 nonce，再由 Tab／Return 啟動按鈕的 click handler |
| 完成字串 | `GUI_COMPLETE:f7ab6e8c06b4982320b1e9b6` |
| Cleanup | Chromium、Openbox、Xvfb 的退出碼均為 `0`；官方 session stop 成功 |

前幾版 browser 能顯示但輸入失敗；根因之一是把 `type` 和後續 `key` 串在單一
`xdotool` command。官方文件說明 `type` 會消耗後面所有 arguments。
v5 將 focus、type、key 分開執行，也修正了 Linux `pwd` 匯入造成的 Windows 測試 regression。
沒有以直接執行頁面 JavaScript 或 HTTP callback 取代 GUI 輸入。

完整 manifest：[G3 run-manifest.json](evidence/g3-20260913T0133Z/run-manifest.json)。
畫面：[before.png](evidence/g3-20260913T0133Z/before.png)、
[after.png](evidence/g3-20260913T0133Z/after.png)。

![Hosted v5 完成畫面](evidence/g3-20260913T0133Z/after.png)

| Artifact | SHA-256 |
| --- | --- |
| `before.png` | `71b59d30731d7ee3f62b0ada983246318b3e473b38333f66ef92bf8e3a88e1a4` |
| `after.png` | `99933acc145da88a310efe4bd8e845de41b16dfb36df99f572139ef6f05bb0de` |
| `browser-artifact.txt`（74 bytes） | `a57f0487d7dd1bce2751b64d2d7c6e1db3dbbc3341f58404a8d820bef6c477a7` |

Controller 同時比對了 server 回報 hash、下載後實際 hash 與預期完整內容。
圖像是在該 session 的 display 擷取，檔案經官方 session files API 下載；
這只能證明本地桌面與 session-private artifact，不能證明 Azure Files 共享。

### 私有 VNet 環境的獨立 G3：PASS

在新 project 的 v1（相同舊 v5 image digest）亦完成了獨立 G3，沒有新增 image/version，
也沒有執行 mount 或發布 `/team`。實際畫面已檢查，顯示完整 nonce 與 `GUI_COMPLETE`；
下載後 bytes/hash 與 manifest 相符，Chromium／Openbox／Xvfb 都以 0 退出。

| 項目 | 證據 |
| --- | --- |
| Run | `vnet-g3-20260913-1` |
| UTC | `2026-09-13T04:30:07.020586+00:00` – `2026-09-13T04:30:31.313489+00:00` |
| Session | `0be723b77803080000ue0bdYTufDg7az4aLZ56TohWKpsMKrrS`；stop 回 `stopped=true` |
| Nonce | `6335b5c09ad786283b940937` |
| before.png | 33,243 bytes；`09acb2a60951ceeb66fd4dbc0cfadf70dd52411dddeb88b2ce3834ed5d935bad` |
| after.png | 44,998 bytes；`e0050c7780ffa1550933d3176ffc3a540f0f1cb8736c2c108adcfe0e43317660` |
| browser-artifact.txt | 75 bytes；`79dd361852d5e02cd8b7f9ad6060d358a65cd853e13346739aaaa1341b315e8d` |
| 共享成果 | `NOT_RUN_DEPENDENCY`，沒有以 Files REST 下載冒充共享 mount |
| Console 限制 | 已保存 9,963 bytes，串流其後 read timeout；不宣稱取得完整 console。畫面／下載／退出證據不依赖此串流完整性 |
| 控制 VM | 取證後已讀回 `PowerState/deallocated`，證據 `controller-deallocated-after-g3.json` |

證據目錄：`evidence/private-controller-vnet-g3-20260913-1/vnet-g3-20260913-1/`。
取回 ZIP 共 80,278 bytes，SHA-256：
`506048da6265d4abf311a4f212b70f40a193c0fa3b3f434237a2834aa9b46943`。
ARM Run Command 的分段傳輸在 Hosted stop 後才執行；耗時不代表 Hosted session 持續運作。

![私有 Hosted v1 完成畫面](evidence/private-controller-vnet-g3-20260913-1/vnet-g3-20260913-1/after.png)

## 5. 實作、重現與未驗收部分

程式與操作說明見 [runbook](docs/POC-RUNBOOK.md)、`src/fa-hosted-sandbox-poc/`、
`controller/`、`infra/poc/`、`scripts/`、`tests/`。
第一輪本地共 19 個測試：18 passed、1 Windows symlink 權限限制而 skip。
本機 Docker Desktop 未完成啟動；真正 image 由 ACR remote build 建置，G3 在 Hosted 實測。
本地測試或 Docker 不會替代雲端 gate。

v5 當時對應的 G3-only runner 已完成第一輪真實端到端執行。共享 runner 則仍是保守的探針：
G0 未過不執行依賴項；create／invoke 失敗也保存 manifest 並停止已建立 sessions。
新增本地 regression 覆蓋了 B 建立失敗時停止 A、G0 失敗時停止 A/B、不接受錯誤 bytes/hash、
錯誤 share、私有 marker 外洩或 headless 假通過。

本次接續已在本地補齊 G1 的 A-side `inspect`、G2 直接平台 resume header／重掛模式、
G3 Files REST／PNG／內容 hash 驗證，並接上 private controller 的完整模式。
這些變更尚未取得新 Hosted 實測證據，需新 image、version 與 run；不外推 v5 的 PASS。

本地也已修正 mount 錯誤分類：`mount error(13)` 會保留 `mount_errno=13`、
`MOUNT_ACCESS_DENIED_UNCLASSIFIED` 與 `denial_origin=unknown`。
這個修正沒有回改既有 v1／v5 image 或原始 SSE，也不會自動解決掛載拒絕。

以下**不是已交付驗收通過的能力**：

- G1/G2 的完整共享實測；第一輪的 A-side rename 觀察缺口已在本地修正，但尚未在 Hosted 驗收。
- G2 的直接平台 resume 診斷；runner 不會把固定等待時間當成恢復證據。
- 失敗步驟的 exactly-once／自動重試保證。成功結果可重播，但排除失敗後須用新 run ID；controller 不自動重送。
- 正式 browser 強化、完整隔離安全測試、write lease、多使用者 ACL、長時 token refresh。
- bit-for-bit 的全依賴重建；本次可重現部署的固定物件是已記錄的 image digest。

## 6. 剩餘資源與費用

第一輪最後 session inventory 中，**舊 project 的 17 個 sessions 全部回報 `idle`**；
G3 的最後一次操作另有成功的 stop receipt。沒有持續執行的測試串流。
這不是 G2 resume 驗收，也不代表保留的 registry／storage 等資源免費。

| 保留項目 | 實際狀態／範圍 | 成本與處置 |
| --- | --- | --- |
| Foundry account / project | `cog-contosodemo01` / `faexample` | 保留；model deployment list 為空 |
| Hosted agent / sessions | API 列出 versions 1–5，`has_more=false`；17 個 idle sessions | 保留版本及 session-private filesystem，不 delete；版本 `active` 不代表 session compute 正在工作 |
| ACR | `crcontosodemo01`，**Premium**；`ds1`–`ds9` 共 9 次成功 builds；MI 接續 `dsa` 建置檢查失敗，合計 10 次已 queue builds | **與最初 Basic 不符**；未擅自降級，追加 builds 均有明確批准 |
| Storage / Files | `stfaexampledemo`，Standard_LRS；`team-poc`，Hot，quota 5 GiB | 保留，沒有在此 share 完成 POC 資料讀寫 |
| Key Vault / secret | `kv-faexample-de`，Standard；`azure-files-account-key` | 僅讀回 secret ARM metadata，不把 key 放入證據 |
| Runtime RBAC | dedicated vault 的 `Key Vault Secrets User` | Role assignment `00000000-0000-0000-0000-000000000015` 保留 |
| Project connection | `crcontosodemo01-conn`，`ManagedIdentity` | 保留；資源範圍 RBAC 清單另存 `resource-group-rbac.json` |
| Defender for Storage | `isEnabled=true`、malware scanning enabled、`capGBPerMonth=10000`、`overrideSubscriptionLevelSettings=false` | 繼承訂閱設定，可能另有費用；沒有自行關閉 |
| Event Grid system topic | `stfaexampledemo-00000000-0000-0000-0000-000000000016` | Storage 相關 topic 已存在；不將它漏出清理／成本清單 |
| Application Insights / Log Analytics | **未建立** | runtime `appinsights_configured=False`；不能報稱已配置 telemetry |

### VNet 接續新增資源

以下是 VNet 接續環境；新 G0 的 mount error 13 證據另列於第 3 節：

| 項目 | 已讀回狀態／處置 |
| --- | --- |
| `cog-fasample-vnet-demo` / `faexample` / `caphostproj` | Succeeded；account 已有正確 `networkInjections`，`publicNetworkAccess=Disabled` |
| `vnet-fasample-westus3`、三個 subnets、NSG、NAT／outbound IP | 已建立；CIDR、delegation、NAT attachment 與 `defaultOutboundAccess=false` 已讀回 |
| 六個 Private DNS zones／links、四個 PE／其 NIC | 四個 PE 都 Succeeded／Approved；新 Hosted 已通 secret lookup 及 Azure Files private DNS／TCP 445，但 mount 未成功 |
| 新 project 的 AcrPull | Principal `00000000-0000-0000-0000-000000000017` 在原 ACR 的角色已讀回 |
| `poc-container-registry` connection | completion 部署成功；ManagedIdentity、target=`crcontosodemo01.azurecr.io` |
| `vm-fasample-controller`／OS disk | 已建立且 VM 已 deallocate；Standard_D2ls_v7、NVMe、32 GiB StandardSSD_LRS；NIC 僅 `192.168.82.4`，沒有 public IP。保留磁碟仍計費 |

接續證據在 `evidence/review-20260913T022914Z/`：
`resources-after-first-deploy.json`、`new-foundry-account.json`、`new-project.json`、
`private-endpoints.json`、`vnet-readback.json`、`acr-live-roles.json`、`vnet-deployment-1-operations.json`。

ACR 的 policy states 只看到 audit 類規則，沒有找到 SKU modify 的證據；
activity log 也沒有可讀的 create request SKU。**不能將 Premium 差異歸因於組織政策**。
`azure.yaml` 沒有明確約束 registry SKU，原先沒有在 provision 後即核對實際 SKU，是本次執行缺失。

**最新成功的費用查詢：約 US$1.32，不是最終帳單。**
2026-09-13T08:44:48.851821Z 對本 RG 的 `ActualCost` 查詢成功，`nextLink=null`；
區間為 2026-09-12T00:00:00Z 至該查詢時間。
明細：Azure Container Apps `0.10604438715`、Container Registry `0.9901635948`、
Foundry Tools `0.18312526`、Key Vault `0.000021`、Storage `0.00001408`、
Azure DNS `0.000188844086021505`、NAT Gateway `0.0215884757792577`、
Virtual Network `0.0175875`、Microsoft Defender for Cloud `0.0`，幣別皆為 USD。
帳務可能延遲，未入帳的 build／runtime／VM 等費用仍可能追加；不由此保證剩餘預算。
原始請求與回應保存在 `evidence\mi-smb-20260913\cost-before-deploy-*.json`。
收尾再次查詢收到 HTTP429，沒有用錯誤回應覆蓋最後一次成功數值，也沒有密集重試。
先前 02:34 UTC 的 US$0.697526374636、04:16 UTC 的 US$0.836410819036、
05:21 UTC 的 US$0.975295263436 仍保留為歷史證據。

先前以本 RG、2026-09-12 至 09-14 的 `ActualCost` 查詢兩次都收到：

```text
Too Many Requests({"error":{"code":"429","message":"Too many requests. Please retry."}})
```

沒有把空檔或錯誤回應當成 $0。原「預期低於 US$10」也未獲帳單證實。
Premium ACR 的 Retail Prices API 查詢同樣限流，pricing 頁未提供可讀單價；
本報告不捏造 Premium／Defender 的持續費用總額。

原核准前的零售估算只可作背景：兩個 2-vCPU／4-GiB sessions 各工作一小時約 US$0.492，
Hot LRS Files 5 GiB 約 US$0.13／月，另有交易、建置與其他資源費用。
**Basic ACR 的 US$0.1666／日估算不適用於實際 Premium**。
US$100 是停止新工作的門檻，不是 Azure 硬性帳單上限；
保留資源仍可能累積成本，需要後續更新帳單並決定保留／清理。
本輪不擅自新增 Budget、監控或自動刪除排程。

成功建置共 10 次（`ds1`–`ds9`、`dsd`），另有 `dsa`–`dsc` 三次失敗，共 13 次。
診斷 build 首次 CLI 呼叫因 Dockerfile 相對路徑錯誤在本機退出，沒有 queue cloud build，不另外計次。
第一段建置／診斷約 `2026-09-12T16:07Z`–`16:49Z`；續作的 v5 build／G3 約
`2026-09-13T01:26Z`–`01:32Z`。兩段之間不是持續 active test window。
未保存完整 billable CPU 對帳，因此不把這些 wall-clock 時段當成精確 compute 費用。

完整清單見 `evidence/inventory/`：`resources.json`、`sessions.json`、`acr-builds.json`、
`acr-images.json`、`file-share.json`、`secret-metadata.json`、`project-connections.json`、
`agent-versions.json`、`resource-group-rbac.json`、`defender-storage.json`、
`event-grid.json`、`model-deployments.json`、`cost-query*.stderr`。

## 7. 結案界線與下一個必要決策

第一輪的網路阻塞已透過批准的 VNet 配置排除；接續尚未完成，不宣布原生 Hosted 共享儲存成功或不支援。
舊 account-key 曾卡在 SMB session setup；最新 MI 路線已走到實際 mount 的 errno2，
尚須定位失敗階段，不是再次盲加 Private Endpoint 或 root 權限。
兩個追加批准的診斷版本均已執行；v2 探針參數錯誤已由 v3 修正並取得真正 kernel 訊息。
使用者後續已批准 MI／SMB OAuth 路線；成功映像、Storage 啟用、限定角色與真正 v4 G0 已完成，
但掛載未成功。追加 build 額度已用完，不會自行建置新診斷映像。
account-key 路線的唯一根因仍未證明，不再用開放 Shared Key 方式測試。
任何診斷 image、認證架構或政策範圍的變更都應明確記錄；不把錯誤分類文字當成實測根因，
也不為通過 POC 而開啟 public access、取消加密或繞過組織政策。

當前交付保存在此 worktree；`evidence/` 和 `.azure/` 被 gitignore 排除，
含受控 session／resource 對映，**沒有自動上傳或提交**。
引用的原始證據以該目錄實際內容為準；早期部分 console 診斷僅保留會話摘錄，限制已明列。
刪除前應逐項批准上表的精確資源，不用整個 resource group delete 取代盤點。

</details>

## 官方來源

- [Hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Session API／routing](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Hosted runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)
- [Idle／resume 診斷](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/debug-hosted-agent#diagnose-invocation-latency)
- [Azure Files Linux SMB](https://learn.microsoft.com/en-us/azure/storage/files/storage-how-to-use-files-linux)
- [Shared Key 設定的服務相容性](https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent)
- [xdotool 官方指令規則](https://github.com/jordansissel/xdotool/blob/master/xdotool.pod)
- [ACR pricing](https://azure.microsoft.com/en-us/pricing/details/container-registry/)
