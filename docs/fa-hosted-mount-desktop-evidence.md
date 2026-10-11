# Foundry Hosted session：共享掛載與桌面工具查證

查證日期：2026-09-12。這是先前平台研究的針對性增補。

**2026-09-14 狀態指引：** 這是當時的公開文件查證，不是最新實測狀態。後續 [POC 結果](../POC-RESULTS.md) 已有特定 Hosted 配置的非 root GUI PASS；SMB G0 仍 BLOCKED 且已停止，新協作改用 Context API／Table／Memory。[ADR-0003](adr/0003-hosted-environment-continuity.md) 已移除同一 VM 限制，改驗 Hosted 環境／工作連續性；當前工作依 [分析與計畫](POC-ANALYSIS-AND-PLAN.md)，不重啟下方歷史 gates。

**結論：公開資料仍無法確認原生 Hosted sandbox 可以真正掛載同一份 Team share，也不能確認 sandbox 內已有或可正常執行完整 desktop／browser。沒有找到明確禁止條款，因此不能把未知寫成不支援。**

尚未執行部署、登入 Azure、建立資源、實際掛載、GUI 測試或聯絡產品團隊。人類觀看／接手不在本輪範圍。外部 VM 沒有被選定為替代方案。

**2026-09-13 設計增補**：使用者已確認每 agent 長期固定同一台 VM、完整持久環境與工作、長期記憶及 10 倍效能目標。上述「未知」只針對掛載與 GUI，不涵蓋固定 VM：標準 Hosted idle／resume 的換 compute 契約已有明確落差，見 [平台增補](fa-platform-evidence.md) 與 [設計決策](adr/0001-persistent-agent-computer.md)。本文件的 gates 保留作獨立能力探針，通過不等於符合產品原則；是否仍執行需使用者明確決定。

## 1. 能力矩陣

| 能力 | 判定 | 證據及界線 |
|---|---|---|
| 每 session VM 隔離與持久檔案 | **明確支持** | Session 的 `$HOME` 跨 idle 保存；不等於跨 session 共用 volume。[L1] |
| 平台代掛載指定 Azure Files／其他 share | **未公開確認** | 最新 TypeSpec、v1／preview OpenAPI、Python SDK、session 建立 request 未列出 volume／mount／storage attachment 設定。[S1][S2][S3] |
| Sandbox 內自行掛 SMB／NFS | **未公開確認** | 未找到 Hosted 專用 kernel client、mount 權限／capability 或官方成功範例。網路可達與自帶 image 不是掛載許可。[L4] |
| Sandbox 內 FUSE／BlobFuse | **未公開確認** | BlobFuse 容器所需 `SYS_ADMIN`／`/dev/fuse` 等條件，沒有在 Hosted contract／schema 獲得承諾。[L6][S1] |
| 兩 sessions 同掛 canonical share | **未公開確認** | 無跨 session share attachment、ownership 或共同 volume 契約。[S2] |
| 外部 mount 跨 idle 自動恢復 | **未公開確認** | 平台恢復自身管理的 session state，不代表恢復任意應用建立的 mount。[L1] |
| Session Files／Storage API | **明確支持，但不是 OS 共用掛載** | 檔案 API 按 session 隔離；另有存取外部 Azure Storage 的權限指引。[L2][L3] |
| 平台預設 image 已含 desktop／browser | **未公開確認** | 未見可確認 X server、桌面及 Chromium／Firefox 已預裝的套件清單。[L3] |
| 自帶 image 封裝依賴 | **明確支持** | 可選 image-based 部署；不代表 image 裡需要的任意 OS 功能都能在 runtime 使用。[L3][L5] |
| Sandbox-local headless browser | **未公開確認** | 沒找到 Hosted 專用成功範例；已讀 browser sample 實際使用遠端 browser。[S4] |
| Sandbox-local desktop／headed browser | **未公開確認** | 自包 Xvfb／window manager／Chromium 是待驗證候選，不是已確認能力。[L4][S4] |
| Hosted 調用遠端 Playwright browser | **明確支持，有官方原始碼** | CLI 在 sandbox 內，透過 CDP endpoint 連到 Microsoft Playwright Workspaces 的 remote browser。[S4] |

### `/files` 不是已確認的 OS 掛載點

官方 overview 明寫：經 `/files` endpoint 上傳的檔案寫入 `$HOME`，共用其儲存。Session Files 文件則明寫每份檔案 scoped to a specific session。

因此，不能把 API 的 `/files` 路徑當成另一個 Team 共用 mount point；它證明的是同一 session 的檔案與 API 雙入口，不是跨 sessions 共用一份目錄。[L1][L2]

## 2. 配置面查證

REST／TypeSpec 固定 commit：

`Azure/azure-rest-api-specs@e42fad1db8c753fd1ba79372adf4cbe95ed34a0d`

已核對 v1 與 `virtual-public-preview` 規格；規格名稱不等於其中所有功能均已 GA。

| 物件 | 公開欄位摘要 |
|---|---|
| `HostedAgentDefinition` | kind、cpu、memory、environment_variables、container_configuration、protocol_versions、code_configuration、telemetry_config、session_configuration，以及繼承的 rai_config |
| `ContainerConfiguration` | image、registry_connection_id；後者是拉取 image 的認證，不是 storage attachment |
| `SessionConfiguration` | idle_timeout_seconds |
| `CreateAgentSessionRequest` | agent_session_id、version_indicator |

上述配置面未列出 mount、volume、device 或 Linux capabilities。[S1][S2]

Python SDK 交叉核對：

`Azure/azure-sdk-for-python@947d9a589072cd1fef0c6858339f86b6a2fb6e7f`

該 source checkout 的版本為 2.7.0，但 CHANGELOG 標為 **Unreleased**；已發布的 2.6.0 日期為 2026-09-04。不可將 main 上的版本寫成已發行套件。Models 與 REST 的配置面一致，沒有新增 mount 入口。[S3]

**解讀限制**：無公開欄位不能證明 runtime 一定禁止自行 mount；本輪也沒有送出試探 request。它只表示目前無法據此承諾平台代掛載／自行掛載支援。

## 3. Browser sample 的精確執行位置

官方 sample 固定 commit：

`microsoft-foundry/foundry-samples@8599c85e2928906b752ec25807dfbbb45c1a1077`

`14-browser-automation-agent` 的 Dockerfile 安裝 Node.js 與 Playwright CLI；程式從 Toolbox 取得 `cdp_url`，並透過 `PLAYWRIGHT_MCP_CDP_ENDPOINT` 注入 CLI。Toolbox 描述明寫建立 Microsoft Playwright Workspaces 的 remote browser sessions。[S4]

因此它證明：

**Hosted sandbox 的 CLI → 遠端 Playwright browser。**

它沒有證明本地 browser process、X server、Xvfb、完整桌面或 headed browser 在同一 Hosted sandbox 正常運行。

研究另檢查該 commit 下 114 份 Hosted Dockerfile，未命中常見 GUI／SMB／NFS／FUSE 套件及 capability 宣告。這只是有界檢查結果，不能推論所有遞移依賴、啟動腳本或未公開基礎 image 一定沒有這些能力。

## 4. 最小驗證設計，尚未執行

| Gate | 驗證對象 | 通過標準 |
|---|---|---|
| G0 | 真實 Hosted session 的 mount 許可 | 在平台允許的方式下確認實際 mount 成功，記錄 filesystem type、來源與目標。root、capability bit、網路可達或 REST 成功均不算完成。 |
| G1 | 兩個不同 sessions 共用同一 share | A 經一般檔案操作寫入並關閉檔案，B 經自身 mount 讀取；明確交接後反向驗證。不能有背景下載／同步／API 複製代替共享。 |
| G2 | Idle 後恢復／重掛 | 確认平台真正 resume，再驗 mount 及另一 session 在 idle 期間的新內容。分開記錄平台自動重掛、應用啟動時重掛與人工重掛。 |
| G3 | Sandbox-local GUI／browser | 自帶 image 啟動 display server／window manager／headed browser，載入本地測試頁並取畫面；記錄本地 PID、DISPLAY，排除遠端 CDP。headless-only 與完整 GUI 分開判定，不測人類接手。 |

官方診斷文件提供 `x-ms-debug-latency-session-start-type: cold|warm|resume` 及 micro-VM／readiness 計時，可作辨認實際恢復的證據；不可只等待幾分鐘就宣称已跨 idle 恢復。[L7]

若掛載被平台權限或 kernel／device 條件拒絕，記錄原始錯誤並停止；不繞過 sandbox 限制。實測成功也不自動等同正式支援合約或生產 SLA。

## 5. 下一個必要決策

公開證據的有界查證已完成，不再以重複搜尋掩蓋未知。下一步是：

- 取得平台對共享掛載協定、runtime mount 權限及 GUI/browser 的明確支持答覆；或
- 在使用者確認環境、計畫、資源與支出後，做上述最小 POC，再另確認正式支援範圍。

尚需決定優先掛載協定、是否接受應用负责重掛，以及原生 Hosted 的採用門檻。這些尚未由使用者選定。

## 6. 一手來源

### Learn

- [L1 Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)，研究讀到更新日期 2026-09-10。
- [L2 Manage hosted sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)，更新日期 2026-09-11。
- [L3 Deploy from source code](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code)，更新日期 2026-09-01。
- [L4 Runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)，更新日期 2026-08-27。
- [L5 Deploy a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent)，更新日期 2026-09-11。
- [L6 BlobFuse known issues](https://learn.microsoft.com/en-us/azure/storage/blobs/blobfuse2-known-issues)，只用於說明 FUSE 要求，不作 Hosted 支援證明。
- [L7 Debug hosted agent latency](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/debug-hosted-agent#diagnose-invocation-latency)，更新日期 2026-09-08。

### 固定版本原始碼

- [S1 Container／session TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/e42fad1db8c753fd1ba79372adf4cbe95ed34a0d/specification/ai-foundry/data-plane/Foundry/src/agents/models.tsp#L419-L447)
- [S1 Hosted definition TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/e42fad1db8c753fd1ba79372adf4cbe95ed34a0d/specification/ai-foundry/data-plane/Foundry/src/agents/models.tsp#L504-L542)
- [S2 Create session request](https://github.com/Azure/azure-rest-api-specs/blob/e42fad1db8c753fd1ba79372adf4cbe95ed34a0d/specification/ai-foundry/data-plane/Foundry/openapi3/v1/microsoft-foundry-openapi3.json#L33204-L33220)
- [S2 Container configuration](https://github.com/Azure/azure-rest-api-specs/blob/e42fad1db8c753fd1ba79372adf4cbe95ed34a0d/specification/ai-foundry/data-plane/Foundry/openapi3/v1/microsoft-foundry-openapi3.json#L32854-L32876)
- [S2 Session configuration](https://github.com/Azure/azure-rest-api-specs/blob/e42fad1db8c753fd1ba79372adf4cbe95ed34a0d/specification/ai-foundry/data-plane/Foundry/openapi3/v1/microsoft-foundry-openapi3.json#L79660-L79676)
- [S3 Python Hosted model](https://github.com/Azure/azure-sdk-for-python/blob/947d9a589072cd1fef0c6858339f86b6a2fb6e7f/sdk/ai/azure-ai-projects/azure/ai/projects/models/_models.py#L9894-L9975)
- [S3 Python SDK changelog](https://github.com/Azure/azure-sdk-for-python/blob/947d9a589072cd1fef0c6858339f86b6a2fb6e7f/sdk/ai/azure-ai-projects/CHANGELOG.md#L3-L23)
- [S4 Browser sample Dockerfile](https://github.com/microsoft-foundry/foundry-samples/blob/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/agent-framework/responses/14-browser-automation-agent/src/bat-python-maf/Dockerfile#L1-L24)
- [S4 CDP endpoint handling](https://github.com/microsoft-foundry/foundry-samples/blob/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/agent-framework/responses/14-browser-automation-agent/src/bat-python-maf/utils/tools.py#L139-L142)
- [S4 CLI remote-browser injection](https://github.com/microsoft-foundry/foundry-samples/blob/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/agent-framework/responses/14-browser-automation-agent/src/bat-python-maf/utils/tools.py#L226-L269)
