# FA 產品規格與待決事項

更新日期：2026-09-15。已發布基準：`f097fa6`；M4 已批准、實作中，尚未提交／雲端驗收。

本文件不是已凍結的完整產品架構。需求基線以 [requirements.md](requirements.md) 為準；[ADR-0003](adr/0003-hosted-environment-continuity.md) 已將同一 VM 限制改為 Hosted 環境與工作連續性。[ADR-0001](adr/0001-persistent-agent-computer.md) 的長期助理、完整必要環境、context 與 10 倍目標仍保留。

[ADR-0004](adr/0004-agent-behavior-control.md)確認行為控制分工；[Agent 行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md)是新增行為、Interface、狀態轉移與 B01–B12 的規範來源。本版定點修 F15–F17，P2 差異待辦仍開放；它適用於本批 M4 之後的開發，不重開 M2／M3 或擴張 M4 五案。

依 [ADR-0002](adr/0002-shared-context-without-file-share.md)，共享 Context 已部署並完成 17 項功能 gate；後續 M2 文字／恢復／交接與 M3 成果／真人批准核心閉環亦有獨立結果。精確範圍見[計畫](POC-ANALYSIS-AND-PLAN.md)及 M2／M3 報告；自主 UI 仍未通過，完整 FA 未完成，功能、執行限制、費用與環境持久性分開判定。

## 1. 產品物件與責任

以下是描述需求的概念模型，不是已發布 API／database schema。

| 物件 | 責任與界線 |
|---|---|
| Organization workspace | 管理 Teams、connector catalog、connections、授權與組織設定 |
| Team／Team workspace | 成員、Lead、協作訊息、共同檔案、工作記憶與發布規則 |
| Agent | 持久 logical identity、角色、私人工作環境與工作記憶；只屬一 Team |
| Agent Computer | 每 agent 獨立、可持續恢復的工作環境；目前優先使用 Hosted sandbox，不要求同一台 VM |
| Runtime binding | FA Agent 到 Hosted definition／version／session、SDK session 與 conversation 的映射；compute 可更換，但私人環境與授權不可混用 |
| Task／Work item | 目標、分工、執行紀錄、批准、交付物與恢復資訊 |
| Job／Execution owner | Work 的一次可接續執行與有權執行者；不同於成果編輯 owner，新增契約見行為規格 |
| Delivery obligation | 向原授權收件人交付答案或失敗結果的責任；不等於 job completed |
| Operation authorization／receipt | 具體操作的有效授權與執行／未知結果；不等於 SOP 生效批准 |
| Artifact | 成果內容、版本、owner、草稿／發布狀態與來源 |
| Context release | 經批准的 SOP／skills／topology／行為設定版本 |
| Connector definition | 服務／工具類型與支援操作的描述，不包含已授權的使用權 |
| Connection | 具體外部服務、帳號／principal、認證及可用 scopes |
| Team grant | 某 Team 對特定 connection／操作範圍的授權 |

FA 的 Agent ID、Foundry Hosted agent、session、模型 conversation、compute instance、外部 connection principal 必須分開記錄。M2 已有受控 binding；正式動態身份配置與新 execution owner 契約仍需接線。

領域名詞見 [CONTEXT.md](../CONTEXT.md)；長期身份與工作環境不等於永久保留某個程序或實體 compute。

### POC 已實作的部分

| 元件 | 實際契約與界線 |
|---|---|
| Context API | Entra token／OID allowlist 選 actor／Team，組裝 work、角色與 Team／私人記憶；不是排程器或 agent harness |
| Table／M3 Blob | 精確 work／operation receipts、ETag／owner；舊 work 上限 48 KiB。M3 另有固定 Blob 版本、來源／hash，成果 512 KiB／12 版，SOP／skill 8 KiB |
| Memory Store | 非同步抽取、持久 receipt、最多 10 筆的 scope search；合成偏好有成功結果，任意 opaque 值無損保留沒有保證 |
| Hosted A/B | M2 SDK 文字工具循環、同 SDK checkpoint 跨 process 恢復、交接發布已通過；一般背景喚醒與交付控制未完成 |
| GUI／恢復 | M1 指定私人環境與 profile 通過；M2 自主 UI 仍 PARTIAL，SDK 跨替代 session／版本與完整 HOME 不外推 |
| M3 人工批准 | 真人登入／下載、指定 SOP 版本批准／拒絕及 context 選版已通過；通用操作批准／撤銷與多人治理另做 |

## 2. 已定行為契約

### 協作

- Lead 負責整體拆分與交付，專家可以直接互傳訊息／交接。
- 透過共享工作狀態與工具交換資訊、成果版本及交接，不再依賴共同 OS 掛載。
- 每份成果有單一編輯者及明確交接；共享寫入須避免覆蓋較新版本，私人檔案要經受控發布才成為共同成果。
- 正式排程遵守 4 active agents；已 checkpoint 且無活躍工作才可釋放等待槽，未知執行者不釋放。公平權重及是否保留互動槽仍待選，不改本批 M4 的較小執行上限。

### Context 與成果發布

- 工作記憶可自動累積，外部內容維持資料地位。
- 改變行為的內容經批准後才成為生效版本。
- 共享工作狀態、長期語意記憶與成果分開；M3 已有獨立版本／批准契約，正式通用檔案生命週期仍未完成。
- Published 內容需要在所有保留的工具與資料寫入路徑受到保護；不能只靠 prompt 告訴 agent「不要修改」。
- Foundry Memory Store 採穩定 Team／私人 scope；應用程式先授權再選 scope。非同步記憶抽取不代替目前進度、批准、鎖定或已完成操作紀錄。
- 已實作 ETag／owner／handoff、published work 不可變及 M3 人工版本批准；agent `publish` 仍不能代替 FR-18。多人審批、撤銷與一般工具操作授權不由 M3 PASS 代替。

### 交付與耐久執行

採[行為規格 BC-01–BC-04](FA-AGENT-BEHAVIOR-SPEC.md)：持久接單後才 ack，job／operation／delivery 三軸分開，逐次驗有效批准與 execution owner；背景完成先保存再喚醒，結果未知先核對不重送。回條必須明列保存／通道接受／client／UI／人類已讀的確認層級，不以模型自述補足。後續切片與相容性要求見規格，本文不另列第二套狀態機。

### 電腦生命週期

- Agent 的長期身份、角色與私人狀態獨立於 compute；優先在專屬 Hosted sandbox 執行 SDK／CLI、browser、shell 與私人工具。
- 允許 Hosted idle／resume 回收或更換 compute。不以相同 PID、同一台 VM 或 session 永不到期作為驗收條件。
- 明確管理 agent／Hosted session／SDK session／conversation 對應，避免新對話意外新增私人電腦或混用狀態；版本切換與 session 到期另有恢復契約。
- image／工具版本提供可重建基線；私人檔案、profile、設定、SDK 狀態與工作 checkpoint 有明確保存位置及恢復核對。現有 HOME `unavailable` 不能視為完成。
- 依官方 runtime contract 處理 SIGTERM、活躍工作及 resilient execution；不以無限保活假裝永久 compute。恢復不預設 RAM／process／socket 復活，已完成外部操作不能因重啟重送。
- 任務停止、取消排程、重建電腦、撤銷 SaaS grant、刪除 Agent 與刪除資料是不同操作。
- Browser sign-in 的保留與憑證刷新不能只以磁碟持久性推定。
- 人類觀看／接手暫緩；未來不得用殺掉所有背景程序冒充安全交接。

### 完整持久範圍

| 狀態 | 必須保留或可恢復的內容 |
|---|---|
| 私人運行環境 | 已核准的工具與相依版本、OS／使用者設定、工作目錄及私人檔案；不能只保留 `$HOME` 而遺失其他位置的必要環境 |
| Browser 與工具設定 | Profile、偏好及工作設定；認證需依安全儲存、到期與撤銷政策處理，不能以保留 cookies 承諾永遠免重登 |
| 長期記憶 | 私人與 Team 工作記憶、來源及權限邊界；不跟單次模型 context 或平台 session TTL 同生共死，語意抽取不取代精確狀態 |
| 工作狀態 | 目標、進度、成果、owner／交接、待批准事項及已完成外部操作的紀錄；恢復時不重送已完成副作用 |
| 環境恢復 | idle、程序中斷、版本更新及替代 session 時的必要環境／資料／工作恢復；備份、保留期、RPO／RTO 尚待設計，不先承諾零資料損失 |

「完整」指必要環境及工作狀態的完整性，不要求保存任意 RAM、暫存 cache 或過期憑證。可重建工具與必須保存的私人狀態要分開列清單；只用新 image 啟動成功或一個舊檔案不能完成驗收。

### Context 與 10 倍效能

- 長期記憶是可持續累積的知識，本輪 context 是處理當前工作的相關子集，工作狀態則記錄執行與批准進度；三者不可混成一份無限增長的聊天紀錄。
- 記憶檢索、摘要／壓縮與工作恢復不得略過重要約束、來源或批准狀態。目前 `ContextService.assemble` 以 goal 前 2048 字依序查 Team／私人記憶各最多 10 筆；token 預算、一般工作相關性及品質仍未驗收。
- 10 倍是必須驗證的產品目標，不是目前已有結果，也不是承諾模型供應商的每次生成都快 10 倍。
- Benchmark 須固定 OpenClaw 版本／配置、FA 版本、任務與工具、模型與推理設定、資源、網路及初始資料；冷啟動、暖機與恢復情境分開比較，不以 FA 暖機對 OpenClaw 冷啟動冒充提升。
- 報告端到端完成時間，另拆啟動／恢復、context 準備、模型、工具與協調開銷，記錄 context tokens、任務成功率及品質。比較相同統計量時，時間比值 `OpenClaw / FA >= 10` 才可能支持 10 倍目標；工作集、樣本數、p50／p95 採用門檻仍待批准。
- 必須另測長歷史及跨工作恢復情境；不能靠丟掉記憶、跳過工具或批准、減少交付內容降低延遲。掛載成功或 SDK smoke 不是此項驗收。

### 外部操作

- 組織批准 connection／權限後，Team 自選其子集。
- Catalog 可見、tool list、啟用狀態與真正執行授權不是同一件事。
- Azure POC 的操作限定於批准範圍；Lead、CLI、browser 與其他工具都受相同界線。
- 重試造成的重複建資源／傳訊／刪除等副作用，需要獨立設計，不由「恢復 handler」自動解決。

## 3. 候選 runtime 方案

| 候選 | 說明 | 狀態 |
|---|---|---|
| A：原生 Hosted，現行主線 | SDK／CLI、desktop／browser 在各自 Hosted sandbox；Team 經受控 Context 協作，FA 增加耐久工作／交付控制 | ADR-0003／0004；M1、M2 文字及 M3 核心閉環有結果；M4 實作中，自主 UI／一般耐久工作仍有缺口 |
| B：外部 computer，必要時才評估 | 只針對有精確證據的 Hosted 必要能力缺口提出；明確決定工具或整體 runtime 的執行位置 | 未選定、不是預設元件，不因需要長期記憶或共享 Context 就新增固定 VM |

現行 POC 使用兩個 Hosted definitions，各有不同 MI，再由 API allowlist 對應 actor／Team。這是已完成的 A/B 探索配置，不是完整動態 Agent IAM／connector 治理；正式 Agent 綁定、退役與重新配置仍需設計。

2026-09-14 重讀 [官方 compute 生命週期](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#session-compute-lifecycle) 與 [runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)：compute 可更換、指定檔案可恢復；Invocations 不管理聊天歷史。依新原則，更換 compute 不再是採用阻擋，完整必要環境與工作能否恢復才是驗收重點。

先以合法、非 root 的精確 Hosted 配置驗證環境連續性；有不符合必要需求的證據時才重開方案決策，不發明停用回收或特權參數，也不以 `/tmp` 冒充持久工作環境。

若未來採外部工具，必須明確路由 shell、檔案、browser 與 computer-use；working directory 或角色 prompt 不是遠端路由或安全隔離。歷史 controller VM 不直接轉作 agent runtime。

**禁止的推論**：SDK custom agents 不等於各自有 OS；長期助理不等於一定需要額外固定 VM；共享 Context PASS 不等於私人環境、SDK、自主協作或 10 倍效能 PASS。

## 4. 基礎能力採用門檻

已執行基準以 [POC 規格 v1.2](FA-Hosted-Sandbox-POC-Spec.md) 與 [結果 JSON](POC-GATE-RESULTS.json) 為準：

| 範圍 | 現況 |
|---|---|
| 共享 Context 功能 | repo 記錄 17 gates／370 checks PASS；包含 work／ETag／交接、記憶與 scope、程序恢復、發布保護、原生存取拒絕、GUI 及指定 sessions 收尾 |
| 執行限制／費用 | 歷史多餘 active sessions 偏差保留，endpoint 修正僅完成針對性唯讀回歸；目前總成本與預算符合性未知 |
| 原 SMB G0–G2 | 歷史結果，不再執行；G0 BLOCKED，依賴項未完成，不以 Context PASS 覆蓋 |
| 原 G4 | 歷史 NOT_RUN_OPTIONAL 不改；後來 M2 SDK 驗收另有報告，不回填舊 gate |
| M2／M3／M4 | M2 PARTIAL；M3 core_e2e_pass=true／full_scenario_pass=false；M4 已批准實作中、未雲端驗收 |
| 後續產品能力 | 依 ADR-0003 與 M1–M5 驗 Hosted 環境連續性、SDK 閉環、成果／批准、context／效能及四 agent 業務切片 |

技術 POC 通過只支持精確配置的可行性；不能直接宣稱正式平台支援、SLA 或完整產品驗收。

Memory Store 為 Preview；POC 的 Non-VNet Foundry、私網 Context API／Table 及 GlobalStandard 模型配置，不自動滿足正式生產網路或資料處理區域要求。原始 evidence 未加入 Git；本輪分析不宣稱重新核對雲端原件。

新的 [後續計畫](POC-ANALYSIS-AND-PLAN.md) 與已執行測項分開版本化。此次改需求不重寫原 G0–G4 或 17-gate 結果，也不降低尚未通過的私人環境與工作連續性要求。

## 5. 尚待決策

| 項目 | 為何尚未定案 |
|---|---|
| Hosted 環境連續性 | M1 明列環境與 M2 同 SDK 跨 process 恢復已驗；SDK 跨替代 session／版本、完整必要狀態保存與退役仍需補 |
| SDK 真正助理 | 文字／恢復／交接已通過；自主 UI、正式多 agent、一般背景工作與交付控制尚未完整整合 |
| 共享狀態及成果儲存 | M3 專用 Blob／不可變版本／人類批准已有結果；一般生命週期、撤銷及正式規模仍待定 |
| Memory 與網路 | POC 允許 Non-VNet Foundry 使用 Memory Store；Preview 的正式採用、保留期與生產網路策略待確認 |
| Agent IAM mapping | POC A/B MI＋API OID allowlist 已接通；正式動態 agent／Team grants、退役與身份替換未完成 |
| SaaS identity | Team service account、per-agent principal、人類 delegated identity 的適用範圍 |
| Teams 路由 | 人類訊息如何選 Team、回覆身份、主動通知及批准 UI |
| 模型與認證 | Copilot-backed／Foundry BYOK、模型清單、token refresh 與費用 |
| 排程／恢復 | 行為規格已定 owner／epoch、等待釋槽、取消與未知不重送；queue／consumer、deadline、公平權重仍待選 |
| 完整持久環境的實作 | 工具／profile／資料與工作狀態已納入範圍，具體儲存、備份、保留、RPO／RTO 及退役撤銷方式未定 |
| Context 與效能驗收 | 記憶載入策略、長歷史正確性、OpenClaw 基準版本、工作集、樣本數與 10 倍目標統計門檻未定 |
| 正式成果驗收 | 研究來源、架構完整性、POC 成功定義與投影片品質的具體閾值 |
| 人類介入 | 已暫緩；未來另定可視化、接手、控制權及既有程序處理 |
| 收尾與再執行 | 費用與保留資源去向未結清；原始證據受控交接、下一輪精確範圍與支出另批准 |
| 即時推送提示（v16） | POC 用 Web PubSub Free_F1：沒有 SLA、沒有可用性區域、20 條連線、每天 20,000 則，也不能用 private endpoint（公開網路、只允許 Entra）。所以只當提示，工作台保留輪詢。正式環境要定：是否升 Standard／Premium（99.9%／99.95%）、是否需要 private endpoint，以及連線數規模；待 Neal 決定。見 [README 即時推送提示](../src/fa-shared-context-poc/README.md#realtime-push) |

## 6. 文件權威順序

1. 使用者後續明確決策。
2. [行為控制決策](adr/0004-agent-behavior-control.md)、[Hosted 連續性決策](adr/0003-hosted-environment-continuity.md)、[共享 context 決策](adr/0002-shared-context-without-file-share.md)、[持久助理決策](adr/0001-persistent-agent-computer.md) 未被取代部分與 [需求基線](requirements.md)。
3. 本文件與[行為控制規格](FA-AGENT-BEHAVIOR-SPEC.md)；具體行為契約／B IDs 只在後者維護，尚待選型項目不因規格化自動決定。
4. [POC 規格](FA-Hosted-Sandbox-POC-Spec.md) 的測試配置與驗收。
5. 研究文件的來源證據及建議。

這是本 repository 的產品文件關係，不取代接手工具的安全規則。更新需求時同步記錄 [progress.md](progress.md) 的影響，不用研究推論偷偷覆蓋已定需求。
