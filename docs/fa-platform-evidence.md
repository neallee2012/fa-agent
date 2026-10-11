# Foundry Agents：平台研究與規格訪談紀錄

研究基準：2026-09-12。由背景研究的兩輪結果整理。

**2026-09-14 狀態指引：** 下文是公開研究及當時需求紀錄。後續 [POC 結果](../POC-RESULTS.md) 已有共享 Context／GUI 的精確配置證據，SMB 路線停止；[ADR-0003](adr/0003-hosted-environment-continuity.md) 又將固定 VM 限制改為 Hosted 環境／工作連續性。因此下文 2026-09-13 的「固定 VM 契約落差」不再是現行採用阻擋，也不能把舊「未實測」當成最新結果。當前方案見 [分析與計畫](POC-ANALYSIS-AND-PLAN.md)。

本文件只記錄文件／原始碼查證與使用者決策；沒有部署、掛載測試、桌面連線測試或端到端驗證。架構建議不等於已選型，也不構成建立資源或支出的授權。

最新範圍調整（2026-09-12 22:47 UTC+8）：優先查證 Foundry 每個 Hosted session 的 VM-isolated sandbox 能否真正掛載 Team 共用儲存體；其次確認 sandbox 內的 desktop／browser 工具。人類觀看與接手暫緩，不再作為本輪原生 Hosted 候選的阻擋條件。

針對性公開查證已完成，詳見 `fa-hosted-mount-desktop-evidence.md`：共享 OS mount 及 sandbox-local GUI/browser 仍無法從公開支援契約確認；最新 TypeSpec／REST／SDK 未找到掛載配置，官方 browser 範例實際連向遠端 Playwright。下一步需要平台確認或經批准的精確 POC，不能繼續把重複搜尋當成驗證。Grok Bot 研究另存 `grok-box-evidence.md`。

## 2026-09-13 增補：固定 VM 原則與平台契約落差

使用者新增「代管 OpenClaw」定位，並明確選擇每個 agent 長期綁定同一台 VM，只在故障或明確維護時移轉；完整環境、長期記憶與工作須持久，context 與 10 倍效能是核心目標。見 [設計決策](adr/0001-persistent-agent-computer.md)。

本次只重讀 [Hosted overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#session-compute-lifecycle) 及 [session 管理](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)，沒有重跑掛載／GUI 研究，也沒有執行 POC。官方生命週期表的原文：

> Idle: “The platform deprovisions compute and persists session state”

> Resumed: “Platform provisions new compute and restores persisted state.”

**判讀**：標準 Hosted idle／resume 與新固定 VM 契約不相容。相同 session ID、持久 `$HOME`、多 agent 隔離及恢復成功都不能補上同一 VM 的保證；完整環境／工作也不能只靠 session 檔案保存外推。這是文件契約落差，不是已發生的測試失敗，更不是所有 Foundry 模式的支援性結論。

下文保留 2026-09-12 的訪談與候選推論作為歷史；其中容許 compute 常態替換的前提已被收緊，不是目前產品契約。原生／外部分層需重審，G0–G4 只保留獨立能力驗證用途，不能用保活或換 backend 偷換需求。

## 1. 2026-09-12 已確認的產品需求（歷史基線）

1. FA 是產品名稱，第一版服務單一組織；層級為組織工作空間 → Teams → Agents。
2. 長期目標為通用數位員工；第一版必須完成雲端方案研究、架構規劃、真實 Azure POC 驗證與投影片製作。
3. FA 有 Web 工作台，每個 Team 有自己的 workspace，agents 在其中協作。
4. 每個 agent 只屬於一個 Team；角色範本可以重用，但私人電腦與記憶不跨 Team 共用。
5. Team Lead 自主拆解、分工、追蹤與整合；專家 agents 可以彼此直接溝通，不必每句經過 Lead。
6. 上述「直接溝通」只指 FA Team 內的 agents，沒有額外要求每個 agent 都有獨立 Microsoft Teams 對話入口。
7. 每個 agent 有專屬、持久的 Linux 桌面，包含瀏覽器等必要工具；首版不要求 Windows／原生 Office。
8. 電腦允許閒置關機；恢復資料與設定後接續工作，不要求 RAM、程序與視窗完整快照。登入狀態仍受憑證有效期限制。
9. 人類觀看及明確接手桌面已移至後續範圍。原先「接手期間暫停該 agent 新操作、不偷偷終止既有程序或部署」保留為未來設計；本輪只先確認 desktop／browser 等工具。
10. Team 共用空間必須同時有掛入各 agent Linux OS 的同一共用目錄，以及結構化 workspace 工具。
11. 底層可用 Azure Storage Account、合適的 database 或組合，尚未選定產品。
12. 每份成果同時只有一位編輯者，明確交接；其他 agent 可讀取、評論或提修訂。保留草稿與發布版本。
13. 工作記憶可自動累積；SOP、skills、角色／組織設定等改變行為的內容，經人類批准後版本化發布。
14. 管理員管理 connector 清單、實際 connection、帳號與權限；Team 只在被授權範圍內自選，不透過瀏覽器繞過限制。
15. 第一版必備 Teams、Web Search、Azure POC；其他 connector 後續擴充。
16. POC 部署驗證先限 Azure 專用測試範圍；研究可跨雲。
17. 每個 POC 先批准計畫與支出範圍；範圍內自主執行，越界、新增權限、刪除資源再確認。預估費用不是硬上限。
18. 第一版同時執行 4 個 agent，超過排隊；可建立多個 Team。執行中 agent、存活桌面與雲端 POC 資源成本必須分別計算。

以上來源為本次使用者 Q1–Q18 的明確回覆及 22:47 範圍調整；尚未確認的技術選型不能補成需求。

## 2. Foundry Hosted Agents 的已知合約

| 項目 | 證據等級與結果 |
|---|---|
| 隔離模型 | **官方明文**：自有程式以容器交付，執行於每個 session 的 VM-isolated sandbox。這不等於客戶可管理的永久 VM。 |
| OS | **官方明文**：容器要求 `linux/amd64`。自選 kernel、privileged container、Windows guest 管理未查證。 |
| 持久性 | **官方明文**：session 的 `$HOME`／上傳檔案狀態可跨 idle 恢復；不能外推成所有根目錄、RAM 與程序都保留。 |
| CPU／RAM | **官方明文**：0.5 vCPU／1 GiB、1／2、2／4 三種 sandbox 規格。桌面加瀏覽器的容量是否足夠未實測。 |
| 儲存 | **官方明文**：1 vCPU 以上總磁碟預算最高 20 GiB，約 20% 系統保留；其餘還要容納 image、`$HOME` 等，不是額外 20 GiB 使用者磁碟。 |
| Idle | **官方明文**：timeout 2–60 分鐘，預設 15 分鐘；compute 回收後可用新 compute 恢復狀態。 |
| 長期保留 | **官方明文**：overview 寫明 30 天未活動會刪除 session。長期員工資料不能只依賴 session filesystem。 |
| 入口 | **官方明文**：有 HTTP gateway、Responses、Invocations、`invocations_ws`；任意 TCP／VNC 直通及桌面串流完整合約未查證。 |
| Desktop | **未查證**：没有查到 Hosted 原生提供可觀看／接手的 Linux desktop，或自包 GUI 的完整支援承諾。不能改寫成「絕對不支援」。 |
| 共用掛載 | **未查證**：Hosted 專用 volume、Azure Files SMB／NFS mount、FUSE device／Linux capabilities 的公開配置承諾未找到。VNet 可達不代表有 mount 權限。 |
| 身份 | **官方明文**：新版每個 Hosted agent 有 dedicated Entra agent identity；多個 session 不自動等於多個獨立 principal。 |

來源：

- [Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Deploy hosted agent／container requirements](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Hosted runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)
- [Manage hosted sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Migrate initial-preview hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/migrate-hosted-agent-preview)

舊版 replica／start／stop 架構不能直接套到新版 session sandbox。產品的 logical agent、Hosted agent、session、conversation 與 computer 也不是同一個 ID。

路徑精確化：`/files` 是檔案 API endpoint；官方描述上傳內容写入 `$HOME`。不能把它當成另一个已確認的 OS mount point，更不能當成 Team 跨 session 共用目錄。

### Q19 追問：能否每個 FA agent 分配一個 microVM？

使用者尚未選擇 runtime／computer 分層，而是要求確認原生隔離能力。直接重讀 overview 與 session 管理官方文件後，精確答案如下：

- **官方明文**是「Hosted agents run in per-session VM-isolated sandboxes.」配置及狀態單位是 session，而不是可由客戶任意管理的 microVM 資源。
- **設計推論**：FA 可以為每個 logical agent 維護獨立、持續重用的 Hosted session，形成每 agent 分離的 sandbox；不能讓不同 agents 共用同一 session 後仍聲稱有各自的 OS 隔離。
- **官方明文**：idle 會回收 compute，之後將保存狀態恢復至 compute；不是保證永久維持同一個 VM instance 或程序。
- **官方明文**：不同 sessions 的 sandbox 隔離不等於不同 Entra principal。Hosted agent 層才有 dedicated agent identity；若需要每位 FA agent 的原生獨立身份，應另評估各自 Hosted agent 部署，而不是只切 sessions。
- 檔案狀態與對話歷史仍需分開維護；只重用 session 不會自動重播模型對話。Invocations 的 session 綁定由 query parameter 處理。
- 「microVM」在此不能升格成特定 virtualization engine、完整 guest 管理或 privileged OS 的產品承諾；文件證據支持的是 VM-isolated sandbox。
- 原生 sandbox 是否能滿足 desktop／browser 工具與真正共用掛載，仍維持未查證。人類接手已暫緩；外部電腦是候選，不是已證明必須。

來源：[Isolation model](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#isolation-model)、[Agent identity and endpoint](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#agent-identity-and-endpoint)、[Sessions versus conversations](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions#sessions-versus-conversations)。

## 3. Copilot SDK 與 Foundry 有直接整合證據

**原始碼／範例證據**：Microsoft Foundry 官方範例使用 `host: azure.ai.agent`、`kind: hosted`、Invocations protocol，由 `InvocationAgentServerHost` 包住 `CopilotClient`，依 `FOUNDRY_AGENT_SESSION_ID` 建立／恢復 Copilot session，串流 SSE 並載入 skills。

- [官方範例 azure.yaml](https://github.com/microsoft-foundry/foundry-samples/blob/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/bring-your-own/invocations/github-copilot/azure.yaml#L20-L52)
- [官方範例 main.py](https://github.com/microsoft-foundry/foundry-samples/blob/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/bring-your-own/invocations/github-copilot/src/github-copilot-invocations/main.py#L24-L174)

範例證明的是 SDK 在 Hosted Agents 上執行的接法，不是完整 FA、desktop、共享掛載或 Team 權限產品。範例的一次性 bearer token 與 `PermissionHandler.approve_all` 不能直接當成 FA 的長任務認證與安全預設。

**官方文件／原始碼證據**：

- SDK native shell／filesystem 工具作用於 CLI runtime 所在環境；custom handler 在 SDK 應用程式程序執行；遠端 MCP 在其 server 控制的環境執行。
- SDK 的 custom agents／隔離 context 不等於每位 agent 都有不同 OS。
- 若 SDK／CLI 留在 Foundry，桌面在外部，原生 shell 不會自動指向那台桌面電腦。
- BYOK 路線不需要 GitHub authentication／Copilot subscription，模型費用由供應商承擔。
- 有 Azure Identity 取得 Entra token 後交给 provider 的官方路徑；動態 bearer-token callback 仍有 experimental 標記。README 與專門指南存在 managed-identity 支援描述不一致，不能只引用 README 斷言不支援。
- SDK MIT 授權不等於 Copilot inference、下游模型及 SaaS 轉售條款全部已確認；本研究沒有法律／合約審查。

來源：

- [SDK host filesystem isolation](https://github.com/github/copilot-sdk/blob/f45c46fd1812f8bed5b4cbc250f47177c83068f0/docs/setup/multi-tenancy.md#L391-L406)
- [Custom tool handler implementation](https://github.com/github/copilot-sdk/blob/f45c46fd1812f8bed5b4cbc250f47177c83068f0/nodejs/src/session.ts#L1159-L1169)
- [BYOK](https://github.com/github/copilot-sdk/blob/f45c46fd1812f8bed5b4cbc250f47177c83068f0/docs/auth/byok.md)
- [Azure managed identity guide](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/setup/azure-managed-identity.md)
- [Provider types at v1.0.13](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/nodejs/src/types.ts#L3062-L3130)
- [SDK license](https://github.com/github/copilot-sdk/blob/f45c46fd1812f8bed5b4cbc250f47177c83068f0/LICENSE)

## 4. 共用目錄與結構化工具的儲存證據

| 機制 | 外部可管理 Linux 電腦 | Foundry Hosted runtime 內 |
|---|---|---|
| Azure Files SMB | **官方支援** Linux kernel client 與多 client 掛同一 share；仍需滿足 client、認證、TCP 445 等條件。 | **未查證** Hosted 專用掛載配置與權限。 |
| Azure Files NFS 4.1 | **官方支援** Linux NFS client／AZNFS helper 與開機掛載。 | **未查證** kernel mount、權限與完整網路路徑。 |
| BlobFuse | **有官方 Linux 方案，但非完整一般檔案系統等價物**；鎖定、快取同步與多來源寫入有限制。 | **未查證** 所需 `SYS_ADMIN`、`/dev/fuse` 是否能取得。 |
| Session `$HOME`／Files API | 不等於外部電腦的共同掛載。 | 支援 per-session 檔案，不能等同跨 session 共用一份 volume。 |
| Storage SDK／REST | 可作工具與應用程式存取途徑。 | API 存取不能偷換成已掛入 OS 的目錄。 |

來源：

- [Azure Files overview](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-introduction)
- [Linux SMB mounting](https://learn.microsoft.com/en-us/azure/storage/files/storage-how-to-use-files-linux)
- [Linux NFS mounting](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-how-to-mount-nfs-shares)
- [BlobFuse known issues](https://learn.microsoft.com/en-us/azure/storage/blobs/blobfuse2-known-issues)
- [HostedAgentDefinition](https://learn.microsoft.com/en-us/python/api/azure-ai-projects/azure.ai.projects.models.hostedagentdefinition?view=azure-python)
- [ContainerConfiguration](https://learn.microsoft.com/en-us/python/api/azure-ai-projects/azure.ai.projects.models.containerconfiguration?view=azure-python)
- [SessionConfiguration](https://learn.microsoft.com/en-us/python/api/azure-ai-projects/azure.ai.projects.models.sessionconfiguration?view=azure-python)

**必須處理的差異：**

- SMB 與 FileREST 可以存取同一份 share，但不因此得到跨儲存交易。Linux SMB deferred close 可影響 ETag 與 REST conditional operations，須處理 HTTP 412 等衝突。[官方說明](https://learn.microsoft.com/en-us/azure/storage/files/storage-how-to-use-files-linux#deferred-close-and-dual-protocol-access)
- NFS 的 trusted network、UID／GID、`sec=sys` 不是 Entra agent identity 授權；不能用「已有 managed identity」推論檔案權限已完成。[官方說明](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-how-to-mount-nfs-shares#prerequisite-configure-network-security)
- BlobFuse 不保證其他 mount／API 更新即時一致，亦有限制部分 lock 操作；不能無條件作為多 agent 共用工作目錄的替代。[官方說明](https://learn.microsoft.com/en-us/azure/storage/blobs/blobfuse2-known-issues)

## 5. 建議分層與尚未定案的選擇

以下都是**建議／推論**，不是使用者已批准的實作方案。

### 候選 B：Hosted harness 與個人電腦分開

本候選在第一輪研究中被建議優先考慮，但使用者沒有選定；22:47 已要求先深入確認原生 Hosted session 的共享掛載與桌面工具。

- FA 控制層：組織／Team／Agent、任務、訊息、批准、版本、connector grants。
- Foundry Hosted runtime：Copilot SDK／CLI、推理與工作協調。
- 受控 computer tools：把 shell、檔案、browser、畫面與鍵盤滑鼠操作綁到指定 agent computer。
- 每 agent 外部專屬 Linux 電腦：持久個人資料、桌面／browser、掛載 Team share。
- Team workspace：共用檔案與結構化 database；資料庫產品尚未選定。

依據是上節 Azure Files 的 Linux 支援、[Azure VM](https://learn.microsoft.com/en-us/azure/virtual-machines/overview)，以及 SDK 工具執行位置合約。這不是已完成的端到端實測。

必要工程代價：

1. 停用／限制會誤操作 Hosted 本機的 ambient tools，以明確的遠端工具路由取代；不能只設定 working directory 就宣稱同一台 computer。
2. computer 身份、控制權、閒置關機、工具回傳與故障恢復需要 FA 管理。
3. 後續啟用人類接手時，須阻擋新操作但不偷偷殺掉已啟動的背景工作；不納入本輪 desktop 工具查證 gate。
4. 共用掛載與 API 都必須遵守单一編輯者及 SOP 發布權限，不能只在 database 裡寫一個 owner 欄位。

### 候選 A：全部放 Hosted session

可重用平台的 session 狀態與工具本機性；目前優先深入確認「各 agent 掛同一個 Team share」，其次為 sandbox 內的 desktop／browser 工具。必須區分預設內建與可自行封裝進 image，也不能以遠端 headless browser 冒充同一 sandbox 內的桌面。若文件仍無精確證據，下一步是平台確認或經使用者批准的針對性 POC，不能用 `$HOME`、Storage SDK 或上傳 API 替代真正掛載。

### 尚未執行的最小驗證設計

本段只是釐清證據門檻，不是部署或支出授權。

1. 在兩個不同的 Hosted sessions 中，分別確認 Team 目錄確實是 OS mount，記錄來源、檔案系統類型與掛載條件；不能只是下載／同步到本機資料夾。
2. 由 session A 的一般 CLI 檔案操作寫入測試成果，session B 直接經自己的掛載讀到同一內容；明確交接單一編輯者後再反向驗證。不得用背景同步器或 SDK 代寫來冒充共享掛載。
3. 對 session idle／compute 回收與恢復，確認共用目錄能重新掛入、資料仍一致，並記錄是否需要應用程式重新掛載。檔案保存與程序存活分開判斷。
4. 在同一 sandbox 啟動實際 desktop／display 環境與 browser，確認工具可操作並取得畫面；分開列出 headless-only、virtual display 與完整桌面環境，不能只驗 browser process 存在。
5. 記錄失敗發生於平台配置、kernel／capability／device、網路、認證、檔案權限或容量的哪一層；單次 POC 成功也不能直接外推為平台正式支援或生產 SLA。

## 6. Context、connector 與長任務的界線

**建議／推論**：將 workspace 至少區分為 Working、Published 與 Structured state。工作檔案與記憶可寫；批准後 SOP／skills／topology 對 agent 的 OS 與工具兩條路都唯讀；任務、授權、訊息、批准及版本資料由受控 API／database 管理。

依據：[SDK skills 會注入 context](https://github.com/github/copilot-sdk/blob/f45c46fd1812f8bed5b4cbc250f47177c83068f0/docs/features/skills.md#L3-L13)。

**官方明文**：Foundry Toolbox 提供工具集、connection、認證與版本功能，MCP 認證也區分 shared authentication 與 individual OAuth identity passthrough。**建議／推論**：FA 的 Team grant、action scope、inbound channel routing 仍需獨立建模，不能把 tool 清單過濾當成完整執行授權。

來源：[Toolbox overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/toolbox-overview)、[MCP authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication)。

**官方明文**：resilient execution 不還原 RAM／local variables／call stack，恢復可能重新進入 handler。**建議／推論**：FA 需要 checkpoint、外部操作識別與去重；發訊息、建資源、刪資源不可因重試而重送。部分 resilience／state-store 能力仍有 preview 狀態，不能外推為整體 GA 或 SLA。

來源：[Long-running agent resilience](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/long-running-agent-resilience)、[Agent state store](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-state-store)。

## 7. 版本與證據限制

- 研究讀取的 SDK main：`f45c46fd1812f8bed5b4cbc250f47177c83068f0`。
- 穩定 release：[`v1.0.13`](https://github.com/github/copilot-sdk/releases/tag/v1.0.13)，2026-09-04；tag commit `f13e4a2cc7e4e220974d2333142234e162a3252e`。
- Foundry 範例 commit：`8599c85e2928906b752ec25807dfbbb45c1a1077`。
- Learn 頁面是研究當日讀取的內容，不是永久固定版本；實作前須重查精確 SKU、區域、API 與功能狀態。
- 研究發現部分公告頁首日期與後續更新內容、resilience private/public preview 描述有差異；不以未來搜尋爬取時間或單一公告推定功能何時已 GA。
- Grok Bot 已找到吻合的 x.ai 官方產品與 Agent Computer 文件，詳見 `grok-box-evidence.md`。官方是每 user 一台共享 Firecracker microVM，不是每 Bot 一台；同機 `/workspace` 不證明跨微型 VM 共享掛載。「Box」正式資源名稱與 Muse 的精確對標仍未確認。
- 原生 Hosted 共享掛載／desktop 工具與 Grok Bot 的公開研究已完成；前者核心能力仍未確認，需決定採平台支援確認或經批准的實際 POC。後續仍有 runtime／computer 分層、模型路線、資料存取與身份、恢復／取消、Teams 路由、預算與端到端驗收，尚未達成完整規格共識。
