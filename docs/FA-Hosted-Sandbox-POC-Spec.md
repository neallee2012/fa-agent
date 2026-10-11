# FA 原生 Hosted Sandbox POC：執行方案與驗收規格

版本：1.2｜日期：2026-09-13｜執行結果更新：2026-09-14｜狀態：共享 context 功能通過；執行限制與 SMB 歷史見結果報告

**結案後更新（2026-09-14）：** 本文件保留已執行 v1.2 的測試範圍與條件；後續依 [ADR-0003](adr/0003-hosted-environment-continuity.md) 採 Hosted 優先，以環境／工作連續性取代同一 VM 限制。新的 [M0–M5 計畫](POC-ANALYSIS-AND-PLAN.md) 不重寫原 gate 結果，也不是本文件舊預算或刪除授權的延長。

**2026-09-20使用流程決策：** 後續web-first已取消全局短窗口，改為每工作執行額度及原POC累計USD110；
見[現行行為規格](FA-AGENT-BEHAVIOR-SPEC.md#2026-09-20-使用流程修訂每筆工作與共用預算不設全局短窗口)。
本文件的60分鐘與US$100仍描述當時已執行的歷史條件，不是新流程的開窗要求；
舊run期限、批准及失敗結果均不改寫。

## 已執行 v1.2 修訂：共享 Context 取代 File Share

2026-09-13 20:02 +08，使用者接受 [ADR-0002](adr/0002-shared-context-without-file-share.md)。本節對下方原 v1.1 的衝突內容優先適用：

- 後續以共享工作狀態、Team／私人長期記憶、必要討論與成果參照組裝各 agent 本輪 context；不再要求 OS 共同掛載，不再執行 SMB 盲測。
- 原 R1–R3／G0–G2 及第 3–7 節中的 File Share、Key Vault key、CIFS、VNet、重掛配置，保留為**歷史測試規格**，不是新方案的資源計畫或 gate。原 G0 實測仍是 BLOCKED。
- 新方案驗收跨 agent／session 的狀態與記憶共享、私人／Team 邊界、更新衝突處理及工作恢復；[介面與實作](../src/fa-shared-context-poc/README.md)及[指定 sessions 驗收 controller](../tests/shared_controller/README.md)已完成。不把 Memory Store 建立或本機測試通過當成雲端驗收。
- 允許建立 Non-VNet Foundry 使用 Memory Store，並確認範圍後刪除目前 VNet Foundry；保持 Entra 驗證，不擴大其他資源的 public access。
- 原 US$100、最多兩個同時 active Hosted sessions、每個 2 vCPU／4 GiB、60 分鐘測試窗口維持。Memory 所需模型與費用納入新的精確計畫，不自動啟用 G4。
- G3 的本地 desktop／browser 能力方向保留；固定 VM 維持 DEFERRED_NOT_VALIDATED，完整私人環境及 10 倍效能仍未驗收。

新方案程式位於 `src/fa-shared-context-poc`，部署配置位於 `infra/shared-context` 與
`deploy/shared-context`，雲端驗收入口是 `scripts/validate-shared-hosted.py`。先讀 ADR-0002
與 [目前結果](../POC-RESULTS.md)；只有重現歷史測試時才使用下方原 SMB 流程。

### 新方案的確定性驗收

| 項目 | 必要證據 |
|---|---|
| 共享狀態 | A 建立、B 讀到相同 revision／ETag／artifact hash；重複建立拒絕，非 owner 不可修改 |
| 更新與交接 | 缺 ETag／舊 ETag 拒絕；同身分、同 ETag 的兩筆並行修改只有一筆成功；handoff 後只有新 owner 可改 |
| Team／私人記憶 | 以合成 dashboard 色彩偏好驗證；真正 native update 完成且有 memory operations，另一 agent 搜尋找回 Team 值，只有本人可讀寫私人記憶與其 receipt |
| Context 邊界 | 組合角色指令、權威工作狀態、Team 記憶與本人的私人記憶；拒絕跨 Team、私人 target 與 body scope 注入 |
| 真實恢復 | 停止 A 並讀回 `idle`；B 更新狀態及 Team 記憶；同 A session resume 後 boot marker 改變，並找回最新狀態及本人記憶 |
| 已發布成果 | publish 後 create／PATCH／handoff／再次 publish 都不能改變原成果、版本與 hash |
| 原生存取邊界 | A/B 直接 Table／Memory 請求回 HTTP401/403；token／TLS／網路錯誤或404不算 PASS，Storage 網路拒絕不單獨證明 RBAC |
| G3 同 sandbox GUI | 非 root、headed Chromium、Xvfb/Openbox、GUI input、不同前後 PNG、下載內容／hash、程序及暫存清理 |
| 收尾 | 兩個指定 sessions 均 stop 並讀回 `idle`；保留失敗與未執行狀態、費用限制及原始證據 |

HOME 不可寫必須明列診斷，不提升為 root，也不把 GUI 的 ephemeral 暫存目錄冒充持久環境。
這輪恢復驗的是外部工作狀態與長期記憶，不驗 RAM snapshot、完整 browser profile 或固定 VM。
原生 Memory scope 字元契約由 adapter 處理；scope 名稱不是安全邊界，權限仍由 Context API 強制執行。
Memory 是經模型抽取、合併的語意知識，不保證逐字保存任意值。歷史 verification-code
fixture 的值曾被省略，FAIL 必須保留；精確資料始終以 Table 為準，偏好案例不得外推成無損儲存。

2026-09-14 實際完成 17 項功能 gate。收尾另發現 azd service-name 模式在 explicit session override
之外增建多餘 session，因此**歷史兩 active 上限不能標為通過**。已停止多餘記錄，controller
改用 `agent show` 提供的完整 endpoint；針對性回歸前後完整 session 集合相同且全為 idle。
此差異不修改原限制，也不覆寫原始功能報告；預算符合性仍因 Cost Management HTTP429 而未知。

## 0. 原 v1.1 接手說明（歷史測試規格）

你的任務是用最小實驗回答 Foundry 原生能力問題，不是建立整套 Foundry Agents（FA）。

**產品前提已收緊。** 先讀 [設計決策](adr/0001-persistent-agent-computer.md)：FA 要求每 agent 長期使用同一台固定 VM，換機只限故障或明確維護，並保有長期記憶、完整持久環境與工作，以比 OpenClaw 快 10 倍為目標。目前官方 Hosted 標準 idle／resume 會換 compute，與固定 VM 契約不相容。

本方案的 G0–G4 仍可回答獨立能力問題，但不驗上述完整產品契約。**雲端執行前，須先確認符合原則的 runtime 路線，或取得使用者明確決定「即使不能據此採用正式 runtime，仍進行 Hosted 能力探索」。** 後者不放寬 FA 設計原則；既有規格不構成繼續支出的授權。不能靠保活請求、長連線或提高 idle timeout 宣稱固定 VM。

**2026-09-13 接續決定：** 使用者明確要求「先把固定VM的要求列為待驗證事項，我想基於目前hosted agent的能力把POC完整跑完」。因此固定 VM 列為 **DEFERRED_NOT_VALIDATED**，不作本次 Hosted 能力探索的前置阻擋；完整產品契約與正式 runtime 採用仍未驗收。此次接續維持既有 G0–G3 範圍與 US$100 預算，G4 仍須另行批准；資源／權限／費用計畫與實測證據要求不變。

執行順序：

1. 先處理上述產品相容性前提，再讀第 1–4 節，確認範圍、證據與待填輸入。
2. 提交第 4 節的資源／權限／費用計畫，取得使用者明確批准。
3. 在使用者指定、可寫入的專案或隔離工作目錄實作第 5–6 節；先做本地控制測試。
4. 部署到真正的 Foundry Hosted runtime，依第 7 節執行 G0–G3。G3 可獨立於掛載成敗執行。
5. G0–G3 結果確定後，若原批准範圍包含 G4，再執行 Copilot SDK 煙霧測試。
6. 依第 8–10 節交付證據、判定、剩餘資源與清理計畫。

**授權邊界：收到本文不是部署、支出、授予權限、刪除資源、發送訊息、commit 或 push 的授權。** 發生這些動作前，取得符合實際範圍的確認。研究資料與本地非付費準備不必因等待雲端授權停擺。

所有溝通與報告使用白話繁體中文；程式碼識別字、原始錯誤、雜湊與 API 欄位保留原文。

## 1. 決策問題與明確範圍

### 必須回答

| ID | 問題 |
|---|---|
| R1 | 真正的 Foundry Hosted session 能否將 Azure Files 掛成 Linux OS 檔案系統？ |
| R2 | 兩個不同 sessions 能否同時掛同一 share，直接透過檔案 I/O 互相讀到成果？ |
| R3 | 平台 idle 回收 compute 後，該 session 能否恢復共用掛載並讀到其他 session 的最新寫入？ |
| R4 | 同一 Hosted sandbox 能否執行 display server、window manager、headed browser，並以工具取畫面及操作測試頁？ |

R3 描述的是平台現有的 compute 回收／恢復能力，不代表 FA 接受它作為正常生命週期。同 session、同路徑或相同檔案內容不能證明同一台 VM。

### 範圍內

- 一個可重複建置的 Linux image、固定版本的 Hosted agent，以及兩個独立 sessions：A、B。
- 一個專用 Azure Files 測試 share；優先驗證 SMB 3.1.1。
- CLI 檔案存取、單一編輯者交接、基本更新／rename、一輪真實 idle／resume。
- Sandbox-local GUI／browser；機器可取得畫面與執行點擊／輸入，不提供人類遠端桌面介面。
- 輸入、原始結果、失敗原因及證據的可追溯交付。
- G4 作為可選增補：Copilot SDK 在 Hosted 內呼叫明確工具，操作該 session 的同一共用掛載。

### 範圍外

- FA Web 工作台、Teams bot、完整 Team Lead 編排、4 agents 壓測。
- 人類觀看／接手、VNC/noVNC/RDP、遠端桌面串流與控制權 UI。
- 真實客戶資料、第三方網站登入、2FA／CAPTCHA、郵件／社群發布。
- 完整 context database、向量搜尋、SOP／skills 審批產品。
- 固定 VM 的長期綁定、完整私人環境／工作恢復、長期記憶與 context 品質驗收、OpenClaw 對照及 10 倍效能 benchmark。
- 生產級 HA／DR、30 天保留驗證、RAM／程序快照恢復、長時間 burn-in。
- 自動切換成外部 Azure VM、AKS、ACA 或遠端 browser 以掩蓋原生能力失敗。

**POC 只有兩個 sessions 是驗證規模，不修改 FA 首版「4 個 agent 同時工作，超過排隊」的產品目標。**

## 2. 現有事實與不能偷換的概念

- Foundry 官方描述 per-session VM-isolated sandbox；平台可恢復 session 的 `$HOME` 檔案狀態。
- 2026-09-13 重讀官方 overview／session 管理：標準 idle 會釋放 compute，resume 會配置新 compute。這與 DP-02 是文件層級的不相容，不是掛載實測失敗；也不能以目前已知契約否定所有 Foundry 模式。
- 公開 TypeSpec／REST／SDK 配置沒有找到客戶 volume／mount 設定；sandbox 自行掛載的 runtime 許可仍未確認。
- 自帶 container image 明確受支援；這不是 image 內任意 kernel／device／GUI 能力均可用的保證。
- 已讀官方 browser 範例使用遠端 Playwright CDP，不是本地 desktop／browser 的證據。
- `/files` 是 session 檔案 API；上傳內容寫入 `$HOME`，不等於跨 sessions 共用 mount。
- 同一 Hosted agent 的不同 sessions 有 sandbox 隔離，但通常使用同一個 Hosted agent identity。本 POC 不宣稱驗證 per-agent IAM 隔離。
- Grok Bot 官方是每 user 一台共享 Firecracker microVM；其同機 `/workspace` 不能證明 Foundry 跨 session 掛載。

**PASS 的對象必須是精確的 Hosted 配置。** 本地 Docker 成功、Azure VM 掛載成功、Storage REST 成功、session 私有檔案恢復，均不能替代 R1–R4。

必要背景指標：

- 掛載／桌面證據與固定 source commits：讀 `fa-hosted-mount-desktop-evidence.md`。
- 需要 SDK 整合、身份、connector 或原始需求脈絡時：讀 `fa-platform-evidence.md`。
- 需要解釋 Grok Box 差異時才讀 `grok-box-evidence.md`；不要因此改成所有 FA agents 共用同一台電腦。

## 3. 建議最小架構

| 元件 | POC 建議 | 理由／限制 |
|---|---|---|
| Foundry project | 優先使用批准的既有專用測試 project | 不使用 production project；找不到合適資源就先提新建計畫 |
| Hosted agent | 一個部署、同一固定 image／version | 減少變因；两個 session 不等於兩個獨立 Entra 身份 |
| Sessions | A、B，各自持續重用明確 session ID | 由同一 client 建立、呼叫；避免 caller identity 差異干擾 session routing |
| Sandbox size | 建議每 session 2 vCPU／4 GiB，實作前重查區域與支援規格 | 是 POC 建議，非已取得 quota；避免最小規格使 GUI 測試失去辨識力 |
| Image | linux/amd64，自帶 image，以 digest 固定 | 不用 source-code 預設 image 的未公開套件清單作假設 |
| Shared storage | 一個專用 Storage Account 中的一個 Azure Files SMB share | 只放測試資料；優先 SMB 3.1.1，符合官方 Linux 建議 |
| Network | 優先既有且批准的 private path／VNet outbound／DNS | 先確認 Hosted 到 file endpoint 的 TCP 445 路徑；public endpoint 只能在明確批准的受限測試配置下使用 |
| Secret store | 需要測試 key 時使用批准的 Key Vault | 身份只取需要的 secret；secret 不進 image、Git、一般日誌或回應 |
| Controller | 接手 agent 所在的授權工作環境 | 以官方 client／API 驅動兩個 sessions，收集結果；不是 FA 正式控制平面 |
| Database | 本 POC 不新建 | 使用 controller 的版本化 JSON／JSONL 保存測試狀態，不冒充正式 Team database |

### 協定及身份選擇

基線只測 **SMB 3.1.1**，保留傳輸加密。理由是 Azure Files 有明確 Linux 掛載與 SMB／FileREST 雙入口文件，且可選小型專用測試 share。

認證依序選擇：

1. 已具備且獲批准的 Linux Kerberos／identity-based Azure Files 配置。
2. 若沒有，提案使用**只有測試 share 的專用 Storage Account key**進行能力驗證，取得使用者對此例外的批准。
3. 若組織禁止 Shared Key，停止此支線；改列所需 Kerberos 前提，或提出 NFS 分支取得另一次範圍／費用批准。

Key 路線的權限範圍大於單一檔案，不代表 FA 的最終身份設計；shared key 與掛載 `uid`／`gid` 也不能證明 per-agent 授權隔離。SAS 不能被假定可直接替代 SMB mount credentials。

NFS 與 BlobFuse 不是隱含 fallback。NFS 有自己的 storage SKU／網路／UID-GID 條件；BlobFuse 有快取與鎖定語意限制。任何替代協定都要在報告分開命名，不覆蓋 SMB 原始結果。

## 4. 執行前輸入、資源與費用批准

### 接手 agent 必填

| 欄位 | 要填的內容 |
|---|---|
| 工作位置 | 使用者指定的 repository／隔離工作目錄及允許寫入的位置 |
| 探索目的 | 說明固定 VM 契約落差，以及使用者是否仍明確批准獨立 Hosted 能力探索；不能只寫「驗證 FA 可行」 |
| Azure 範圍 | tenant、subscription、resource group、region、Foundry project |
| 現有資源 | 可重用的 Foundry、ACR、Storage、Key Vault、VNet／private endpoints |
| 身份 | 執行者權限、Hosted agent identity、必要 model／secret／image／Storage 權限 |
| Network | file endpoint、DNS、TCP 445、Key Vault／ACR／Foundry 443 的可達方式 |
| Storage | account／share、SMB 認證方式、配額與冗餘 SKU、無客戶資料的確認 |
| G4 | 是否啟用；若啟用，指定批准的模型 deployment 與認證方式 |
| 成本 | 按當日當區價格估算、允許支出、停止條件、負責批准人 |
| 操作 | 可建立／變更的精確資源與 roles；清理需另確認的刪除清單 |
| 保存 | 證據輸出位置、可分享範圍、資源／secret 何時到期與由誰處理 |

環境不足時回報 **BLOCKED** 與缺少條件；不要自行挑 subscription、替使用者接受付費試用或建立其他雲資源。

### 建議資源上限，需納入批准

- 同時最多 2 個 active Hosted sessions。
- 一個 Hosted deployment／version、一個測試 share、單一 image。
- 先完成 image 建置，再開始計算活動測試窗口；建議初始活動測試上限 60 分鐘。
- 時間不足時保留已取得證據，停止新增付費操作並請求延長；不要默默提高規格或迴圈重試。
- Region／sandbox size／storage SKU 變動會改變費用與測試對象，應重新確認。

### 費用估算格式

使用當期官方 pricing／calculator，逐項列出 Hosted session 使用量、Storage 容量／交易、ACR／建置、Key Vault／網路／日誌，以及 G4 的模型 token 費用。不要在沒有區域與 SKU 的情況下猜一個金額。

另外列出「測試後即使 session idle 仍可能收費」的磁碟／share、registry、private endpoints 與其他持續資源。

預算警示與帳單資料有延遲，不是精確金額硬上限；用有界 sessions、操作數、時間窗口及停止新工作共同限制風險。

### IaC／工具規則

- 使用既有專案工具慣例；Bicep 先查 Azure Verified Modules，Storage 候選為 `avm/res/storage/storage-account`，使用時固定已核對版本。
- 找不到符合 Hosted contract 的 AVM 時可用當期官方 SDK／REST／azd；記錄不用 AVM 的原因，不發明 module／API 欄位。
- 依赖版本以實作當日可取得的官方 release 和來源確認後固定。研究中的 Python SDK `2.7.0` 是 unreleased source，不當成已發布依賴。
- Windows 控制端明確使用 UTF-8；Azure CLI 用此專案專屬、不進 Git 的 `AZURE_CONFIG_DIR`，每條資源操作指定 subscription。
- 工作區路徑遵守執行端 OS；本文的 `/team`、`/tmp`、`$HOME` 是 **Linux sandbox 內路徑**，不是 Windows 控制端路徑。

## 5. 實作元件與介面規格

### 5.1 Image

建議沿官方 Python Hosted Invocations 範例建立自帶 image；Python 版本由接手者核對官方支援後固定。依賴全部在 image 建置階段声明並安裝：

- 官方 Hosted server adapter 與健康檢查。
- CIFS client／基本網路及檔案系統診斷工具。
- Xvfb、輕量 window manager（例如 Openbox）、Chromium 或同等 headed browser。
- 本地畫面擷取與 GUI 輸入工具（例如 mss／Pillow、xdotool）；也可用已核對的等價工具。
- 本地 browser automation client；限定操作本 sandbox browser。
- G4 啟用時再加入固定版本 Copilot SDK／相容 CLI。

這份套件清單是**待驗證 image 設計**，不是對 Hosted 支援性的宣告。記錄 base image digest、OS／package／browser 版本、image 大小；確保 image 與測試檔案不超出實際 session storage 預算。

Browser 優先使用標準 non-root／sandbox 設定。若只有降低 browser 隔離才能啟動，回報獨立的限制；不得悄悄降級後將基線 GUI 判定為完整 PASS。

### 5.2 Hosted Invocations handler

使用官方 Invocations server contract。下列是**本 POC 自訂 JSON payload**，不是 Foundry 新增 API 欄位：

| `operation` | 行為 |
|---|---|
| `probe_environment` | 回傳白名單環境資訊、kernel／mount／capability／device／版本狀態 |
| `mount_shared` | 按已批准且固定的 share／mount 配置嘗試掛載，回傳結果；不接受任意 host/path |
| `shared_io_step` | 只執行測試契約允許的 read／write／hash／rename，路徑限本 run 目錄 |
| `desktop_smoke` | 啟動／檢查本地 display、window manager、browser，操作固定本地測試頁並取畫面 |
| `status` | 回報本 run 已有狀態，不自動補做 mount 或重跑失敗測試 |
| `sdk_smoke` | 可選；SDK 只呼叫本 POC 的明確工具，操作固定測試資料 |

必填共同欄位：`schema_version`、`poc_id`、`run_id`、`operation`。檔案步驟另含 `step_id` 與受限的 `relative_path`。

安全與錯誤契約：

- 固定 operation allowlist；不接受網路輸入的任意 shell 指令、URL、UNC host 或絕對路徑。
- 使用正規化路徑與 symlink 邊界檢查，限制在此 run 的測試目錄。
- 每個 operation 有 timeout；日誌保留退出碼及經去敏的 stderr。失敗不得回傳成功形狀。
- 重送相同 `run_id + step_id` 應回報既有結果或明確衝突，不重複啟動 browser／寫入不同內容。
- stdout／JSON 回應分離，避免 CLI 診斷破壞結構化結果。
- readiness 僅回報服務健康，不在健康檢查中重跑 mount／GUI／模型推理。

### 5.3 Session routing

Controller 透過当期官方 `/sessions` 與 Invocations 流程管理 A、B；Invocations 使用官方 query parameter `agent_session_id` 綁定 session。把 session ID 塞在任意 payload 欄位不算正確路由。

Controller 保存 `Hosted agent + version + session ID + image digest + caller identity` 的映射。A、B 不得共用同一 session ID；後續 A 呼叫也不得意外新建 C session。

版本、image 或 endpoint 改變時建立新 run，避免把不同配置結果拼成一次成功。

### 5.4 Linux 檔案配置

| 路徑 | 用途 |
|---|---|
| `/team` | 真正 Azure Files OS mount；基線測試前須能證明掛載來源與 filesystem type |
| `/team/poc/<poc_id>/<run_id>/` | 此次實驗唯一可寫的共用成果目錄 |
| `$HOME/fa-poc/` | 此 session 的私有 checkpoint／私有隔離測試資料；不是 Team mount |
| 非持久暫存位置 | 短生命週期 CIFS credentials file、GUI runtime data；不得預設它一定是 tmpfs |

使用 key 認證時，以最小身份在 runtime 取得 secret，建立僅 owner 可讀的短生命週期 credentials file；mount 後按 client 需求清除檔案，避免放在 `$HOME` 或共用 share。若暫存區可持久化，明確記錄風險及處置；不能只依路徑名稱推定安全。

記錄 mount options 時移除 credential 路徑及敏感值。禁止把 key 放入 image layer、controller 命令歷史、process arguments、manifest、截圖或一般日誌。

### 5.5 單一編輯者與結構化工具的界線

本 POC 由 controller 對每個測試成果序列化指派 owner：A 寫完關閉檔案後才交给 B。Manifest 記錄 owner、版本、SHA-256 與步驟。

這驗證 CLI 與結構化工具可以指向同一份內容，**不宣稱已完成平台強制的 write lease、per-agent ACL 或完整多使用者 database 交易**。正式 FA 的 Published SOP／skills 唯讀與編輯權強制機制是後續設計。

## 6. 分階段執行與完成條件

| 階段 | 工作 | 完成條件 |
|---|---|---|
| P0 計畫 | 填妥環境、scope、身份、費用及資源差異 | 使用者批准精確執行範圍；未批准不進雲端變更 |
| P1 本地控制 | 建置 image；驗 handler 輸入、路徑限制、輸出格式及 GUI 本地功能 | 能辨識一般 image／程式錯誤；本地 mount 特權不能冒充 Hosted 能力 |
| P2 部署 | 固定 image digest／Hosted version，建立 A、B | 官方 endpoint 可用，映射可追溯，版本一致 |
| P3 基礎探針 | G0、G1；G3 可以平行但保持不同 run 狀態 | 各 gate 有確定 PASS／FAIL／BLOCKED，不靠無限 retry |
| P4 恢復 | G1 通過後跑 G2 | 已證實真實 idle／resume，並完成重新掛載與新內容驗證 |
| P5 SDK | 已批准時才跑 G4 | 模型／SDK／CLI 與檔案工具的執行位置有證據 |
| P6 交付 | 報告、證據、剩餘資源、費用與清理計畫 | 符合第 10 節，不把部分成功宣稱完整成功 |

遇到可辨識的網路、認證、image 錯誤，只修正該邊界並重跑相關 gate。原始 run 保留；修正後用新 run ID。不換 backend 或偷偷增加權限使測試「變綠」。

## 7. 測試案例與通過條件

### G0：原生掛載許可

1. 在 A、B 分別記錄 runtime UID／GID、kernel version、容器可見 capabilities、filesystem 支援、mountinfo、`/dev/fuse` 是否存在。
2. 驗證 file endpoint DNS、TCP 445 與批准的認證配置。
3. 經 `mount_shared` 使用普通、平台允許的方式掛 `/team`；不申請未公開的 privileged／device 選項。
4. 記錄返回碼、原始錯誤與實際 filesystem／source／target。

**PASS：**A、B 都成功看到指定 Azure Files share 的真實 OS mount。

**不算 PASS：**只有 root、capability bit、TCP 445 連通、REST 可讀、目錄存在，或只是 bind mount 私有目錄。

**停止條件：**平台拒絕 syscall／capability／device 且無官方允許的修正路徑，記錄 FAIL_FOR_TESTED_CONFIG；不繞過 sandbox 邊界。

### G1：跨 session 同一份檔案

測試資料只含唯一 nonce、run ID 與合成 bytes；建議一份小文字檔及一份約 1 MiB 二進位檔。

1. A 在共用 run 目錄写入 v1，關閉檔案，回傳 SHA-256。
2. B 直接開啟自己的 `/team` 路徑，讀到同一 nonce、bytes、hash。
3. Controller 明確交接 owner 給 B；B 更新為 v2，關閉後 A 讀到 v2。
4. B 對一份本 run 檔案 rename，A 觀察新舊檔名；不得刪除既有使用者資料。
5. A 同時在其 `$HOME/fa-poc/` 建立私有 marker，確認 B 的對應私有位置沒有該內容。這是輔助隔離觀察，不是完整安全測試。
6. 結構化工具讀取同一成果並回傳相同 hash；不從工具的私有 cache 返回替代內容。

**PASS：**所有共享 I/O 直接經兩邊 mount，內容及 hash 一致；私有 marker 未跨 session 出現。

記錄 close-to-read 的實際延遲。POC 建議觀察窗口 10 秒：超過則報 timeout／不一致，不將此數字宣稱產品 SLA。不要插入 REST 上傳、同步器或下載副本來補結果。

若增加 SMB／FileREST 對同一檔案的交接，將其標為額外測項，依官方 deferred-close／ETag 說明處理；不把 HTTP 412 當成 kernel mount 不支持。

### G2：真實 idle／resume 與重掛

1. 在批准的版本設定中使用官方支援的短 idle timeout；目前文件範圍為 2–60 分鐘，可提案用 2 分鐘。
2. A 完成工作後關閉活躍請求／串流；GUI 若仍有活動，先停止本 run 的 GUI 工作，以免干擾 idle 判讀。
3. 使用不會刷新 session activity 的官方狀態／日誌觀察；若不確定查詢是否保活，先查明。不要持續 invoke A 來「等待」idle。
4. A idle 期間，由 B 更新共享成果為 v3，關閉檔案。
5. 恢復 A，保存官方 `resume` 診斷、平台狀態／日誌或其他直接 compute 恢復證據。僅等待固定分鐘數不算。
6. **先檢查 mount 是否還存在，再執行應用重掛**；結果必須區分 platform-auto、application-remount、manual-only。
7. A 讀到 B 最新 v3，再交接 A 寫 v4、B 讀取驗證。

**PASS_WITH_APPLICATION_REMOUNT：**確認真實 resume，應用能在新一輪啟動／首次工具操作時依批准配置重掛，讀寫最新共享資料成功。

**PASS_PLATFORM_AUTO：**確認 resume，且在應用任何重掛動作之前就有正確共用 mount，後續讀寫成功。

**BLOCKED：**無法證明 compute 真的經過 idle／resume；不能用 `$HOME` 舊檔仍在作為替代。

應用重掛可作為本 POC 技術通過結果，但是否接受為 FA 正式要求仍由使用者決定；不要將兩種恢復模式合併成「平台自動恢復」。

兩種 PASS 都只回答回收 compute 後的共享存取能力。它們**不滿足固定 VM 的 FR-12**，也不證明已保留工具安裝、browser profile、長期記憶或完整工作進度。不得以 G2 通過回頭放寬設計原則。

### G3：同一 sandbox 的桌面與瀏覽器

1. 啟動本 sandbox 的 virtual display server 與 window manager。
2. 啟動 **headed** browser，連線到本 sandbox loopback 上的固定測試頁；頁面顯示 run ID／隨機 nonce、輸入欄與按鈕。
3. 保存本地 display／window manager／browser 的 PID、版本、DISPLAY，以及 loopback 連線證據。
4. 以 OS GUI 工具進行一次輸入與點擊，讓頁面顯示指定完成字串；取得整個 display 的前後畫面。
5. 驗證 browser 可將一份合成檔案寫到本地工作目錄；若 G1 已通過，再把成果寫入共用 run 目錄並由 B 讀取 hash。
6. 正常結束本 run 啟動的程序，記錄退出狀態；按精確 PID 處理，不廣泛終止程序。

**PASS：**本地 display、window manager、headed browser 與 GUI 輸入均成功，畫面含本次唯一標記；不是遠端 CDP 或只有 headless。

**PARTIAL：**只有本地 headless browser 成功，或 browser 能顯示但 GUI 輸入失敗；分項記錄，不能當成完整 desktop PASS。

**無需：**任何 VNC/RDP 服務、對外開 browser debug port、人類接手 UI 或第三方網站憑證。

### G4：可選 Copilot SDK 整合

前提：使用者批准模型與費用；G0–G3 的核心結果已確定。

- 沿官方 Copilot SDK Hosted Invocations 範例整合；固定 SDK／CLI／image 版本。
- 只給 `read_poc_artifact`、`write_poc_artifact` 等最小工具；固定此 run 目錄与已批准操作，設定有界工具呼叫次數／timeout。
- 一次簡單任務：讀 v4、寫回指定合成摘要，再由 B 讀取核對。
- 記錄 SDK session 與 Foundry session 的映射、工具執行位置與成果 hash。
- 不照抄範例的全域 `approve_all`；模型不能獲得任意 Azure 變更、外部發訊息或刪除資料權限。
- 若用短效 Entra bearer token，清楚記錄有效時間；單次成功不能宣稱長任務 token refresh 已完成。刷新測試不在本輪必要範圍。

**PASS：**SDK 真正發出工具呼叫，工具在指定 Hosted sandbox 對同一 mount 操作，B 讀到結果。

G4 未執行時寫 **NOT_RUN_OPTIONAL**，不影響 G0–G3 基礎能力結論。

## 8. 結果格式、錯誤分類與判定

### 每個 gate 結果必填

`poc_id`、`run_id`、`gate_id`、開始／完成 UTC 時間、image digest、agent version、session aliases／受控映射、測試配置、狀態、reason_code、去敏原始錯誤、artifact paths。

狀態使用：

- `PASS`：精確測試條件全部成立。
- `PARTIAL`：列出已成功與未成功子項；不能作整體採用依據。
- `FAIL_FOR_TESTED_CONFIG`：此精確配置已重現失敗，不擴張成所有版本都不支援。
- `BLOCKED`：前提不足，尚未測到目標能力。
- `NOT_RUN_DEPENDENCY`／`NOT_RUN_OPTIONAL`：明確說明沒有執行。

G2 另填 `remount_mode = platform_auto | application_remount | manual_only | unknown`，保留上節細分判定。

最終報告另列「產品原則相容性」：把已知文件契約落差、測試觀察及尚未驗收的固定 VM／完整持久環境／長期記憶／效能分開。不能把文件上的不相容填成未執行 gate 的 `FAIL_FOR_TESTED_CONFIG`，也不能把 gate 的 PASS 寫成整體架構 PASS。

### 失敗分類

| reason_code | 意義及下一步 |
|---|---|
| `ENVIRONMENT_OR_APPROVAL` | 缺測試範圍、支出批准、資源或合法身份；先補前提 |
| `REGION_OR_QUOTA` | 該區域／訂閱容量不符；不推論 mount 被禁止 |
| `IMAGE_OR_DEPENDENCY` | image、套件、server contract 或 startup 問題；先修 image，再重跑受影響 gate |
| `NETWORK_OR_DNS` | file endpoint／port 路徑不通；不要改測 REST 後聲稱 mount 成功 |
| `AUTHENTICATION_OR_SHARE_ACL` | SMB／share 認證問題；不把它當成 VM kernel 限制 |
| `MOUNT_PRIVILEGE_OR_KERNEL` | 已有直接權限／kernel／device 拒絕證據；保留原錯誤，確認官方允許的路徑 |
| `SHARED_IO_CONSISTENCY` | 寫入、hash、rename 或可見性不一致；保留兩邊證據 |
| `RESUME_NOT_PROVEN` | 未證實真實 idle／resume；維持 BLOCKED |
| `REMOUNT_FAILURE` | 恢復後無法重掛或看到錯誤來源／舊內容 |
| `GUI_OR_BROWSER_RUNTIME` | display／browser／輸入／容量問題；headless 與 GUI 分開 |
| `SDK_OR_MODEL_AUTH` | G4 的 SDK／CLI／provider／token 問題；不推翻既有 mount 結果 |

### 最終決策矩陣

| 結果組合 | 可得出的結論 |
|---|---|
| G0、G1、G2、G3 通過 | 該配置的掛載／共享／恢復／GUI 基礎能力有可重現證據；不解除固定 VM 的契約落差，也不驗收長期記憶、完整持久環境或 10 倍效能 |
| 掛載通過，GUI 未通過 | 共享檔案可行，不承諾桌面；列出 GUI 的精確失敗邊界 |
| GUI 通過，掛載未通過 | 桌面工具可行，但不滿足已定的跨 agent 共用掛載要求 |
| G2 僅人工重掛 | 不滿足自主恢復目標；不能把檔案仍存在當作恢復完成 |
| 仍採標準 idle 回收／新 compute 恢復 | 與 DP-02 不相容；無論能力 gates 結果如何，都不能據此採用為 FA 固定 VM runtime |
| 核心測項 BLOCKED | 尚無採用／否決證據，列最小缺口，不強推外部 VM |
| 平台明確不支持或精確配置反覆拒絕 | 提交證據與影響，讓使用者決定改 storage 契約、等平台支持或另設 computer 層 |

## 9. 清理與剩餘費用

- 到達停止條件後，停止發出新模型／工具請求，關閉本 run 的長連線及程序，確認 sessions 透過官方支援方式停止或 idle。
- 列出實際仍存在的 Hosted versions、images、share、account、secrets、role assignments、network resources、logs 與預估持續費用。
- **刪除前請使用者確認精確資源清單。** 現有共用資源不可因本 POC 被刪除；不要用 resource-group 層級刪除代替盤點。
- 證據保存在使用者指定位置；去敏後才放入可分享檔案。清理 temporary credentials 與工具產生的暫存不得波及使用者資料。
- POC 結束不自動建立排程、永久成本監控或發布 release。

## 10. 交付物與完成定義

### 接手 agent 應交付

1. 可重複建置的 image 定義、固定依賴清單、部署／設定檔與短操作說明。
2. Hosted probe handler、controller、G0–G3 測試及已批准的 G4。
3. 去敏的 `run-manifest.json`、每 gate 結果、原始錯誤、mount／resume 證據、GUI 圖像與內容 hashes。
4. `POC-RESULTS.md`：逐項 PASS／PARTIAL／FAIL／BLOCKED、測過的精確配置、尚未測的項目、產品原則相容性與設計影響。
5. 成本／活動窗口摘要、現存資源與待批准清理清單。

建議 evidence 目錄：

    evidence/
      <run_id>/
        run-manifest.json
        g0-mount/
        g1-shared-io/
        g2-resume/
        g3-desktop/
        g4-sdk/
        results.json
        resource-inventory.json

### 完成定義

每個必要 gate 都有可追溯狀態與證據，未通過時仍給出精確失敗邊界；所有付費資源有去向。若所有必要 gates PASS，才可寫「這個配置的原生基礎 POC 成功」，並同時標明它不等於符合固定 VM 等整體設計原則。若 blocked，交付完整阻塞報告也可結案，但不能改寫成產品成功。

不以「程式碼已產生」「HTTP 200」「部署成功」「找到同名檔案」「有 browser process」或「用另一個 backend 成功」代替上述完成定義。

## 11. 官方來源與實作前重查點

- [Hosted 隔離／storage／sandbox 規格](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Session API 與 routing](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Image-based 部署](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)
- [Idle／resume 診斷](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/debug-hosted-agent#diagnose-invocation-latency)
- [Azure Files Linux SMB、認證、加密與 deferred close](https://learn.microsoft.com/en-us/azure/storage/files/storage-how-to-use-files-linux)
- [Azure Files NFS，僅另開分支時閱讀](https://learn.microsoft.com/en-us/azure/storage/files/storage-files-how-to-mount-nfs-shares)
- [AVM Storage Account module](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/storage/storage-account)
- [官方 Copilot SDK Hosted 範例，僅 G4 啟用時閱讀](https://github.com/microsoft-foundry/foundry-samples/tree/8599c85e2928906b752ec25807dfbbb45c1a1077/samples/python/hosted-agents/bring-your-own/invocations/github-copilot)

平台與 SDK 可能更新；接手者應重查當期 schema／region／SKU／packages 並記錄差異。來源更新可以改變測試配置，但不能默默改變使用者要驗證的共享掛載與本地桌面要求。
