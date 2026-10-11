# 以共享工作狀態與記憶取代 File Share

狀態：accepted｜決策日期：2026-09-13 20:02 +08。

**實施狀態更新（2026-09-14）：** 共享 Context 已部署，repo 結果記載 17 項功能 gate 通過，另有 session 執行限制偏差與費用未知；見 [POC 結果](../../POC-RESULTS.md)。[ADR-0003](0003-hosted-environment-continuity.md) 另將同一 VM 限制改為 Hosted 環境／工作連續性，不改變本決策的共享資料分工。下文的批准與資源計畫是當時紀錄，不是新的部署或刪除授權。

**2026-10-01 依 [ADR-0006](0006-team-trust-boundary-grokbot-mode.md) 調整：** Table／Blob／Memory Store 的分工和「經共用 Context API 讀寫」都保留；API 改成薄轉送，只依 agent 身分核對所屬 Team，不提供刪除，不再每一步做權威核對。Memory Store 因 scope 不是 ACL，同樣經 API。現有資料不搬。下文原文保留為當時紀錄。

**2026-10-02 例外（[ADR-0006 第 1 節已知例外](0006-team-trust-boundary-grokbot-mode.md)）：** Memory Store 的私人記憶（#306）和 Team note（#400，Neal 17:30 確認）改由 Hosted 用 agent identity 直接讀寫，不經 API。scope 仍由 FA 固定：Team scope 由 API 依 Agent 登記的 Team 算出，模型不能指定。記憶仍然只是資料，不是指令或批准。其他 Memory Store 用途仍經 API。

使用者決定以「共用工作狀態＋長期記憶＋按需組裝各 agent 的 context」取代跨 Hosted sessions 的 File Share／OS 共同掛載。既有 SMB 探索已取得 MI 票證，但真正掛載與 kernel helper callout 仍回 `ENOENT`；本次改變協作架構，不再以修通 SMB 作為後續工作的前置條件，也不把它回寫成掛載成功。

## 已決定的分工

- **共享工作狀態**：按 Team／Work item 保存目標、進度、owner、交接、批准狀態與成果版本參照，透過共用 Context API／工具讀寫。它是工作的明確目前狀態，不交由語意記憶摘要取代；更新需防止覆蓋別人的新版本。
- **長期記憶**：使用 Foundry Memory Store 探索跨 agent／conversation／session 的知識延續。Team 與私人記憶分開；使用穩定 scope，先在應用程式驗證存取權限。scope 本身不是團隊 ACL，一般記憶抽取是非同步的。
- **本輪脈絡**：由角色指令、相關私人記憶、共享工作狀態及必要的討論／文件組成，不把全體完整歷史塞進每次模型呼叫。需要共同討論時採 Microsoft Agent Framework Group Chat；流程狀態與 checkpoint 仍要接到適合的持久化服務。
- **成果與發布**：仍保留共同檔案／成果、單一編輯者、草稿／發布版本與人類批准規則。改由受控工具與成果參照共享，不再要求每台 agent 電腦有同一個 OS 掛載目錄。Memory Store 與 Toolbox 都不是一般檔案系統；Toolbox 也不代替工作狀態資料庫。

## Foundry 與網路取捨

本次 POC 允許重新建立 **Non-VNet integration 的 Foundry**，以使用目前仍為 Preview 的 Memory Store；官方列出 West US 3，但明列 Memory Store 不支援 VNet integration。新環境維持 Entra 驗證，不因可使用 public endpoint 就改為匿名存取或放寬組織政策。正式環境是否接受這個網路邊界，仍需獨立決策。

使用者允許刪除目前的 VNet Foundry。執行時核對 `cog-fasample-vnet-demo` 與其 project／agents 的精確範圍，再確認刪除；此同意不包含整個 resource group、另一個舊 Foundry、Storage、ACR、Key Vault、控制 VM、網路或不可逆 purge。保留已收回的原始證據；新建／刪除是否完成，以部署紀錄與讀回結果為準。

原 US$100 總預算不提高。Memory 所需模型、容量、權限與少量操作納入新的精確執行計畫；不沿用舊 SMB 計畫的 validation，也不把未用完的 94 輪視為無限重試或新增費用授權。

## 被替代與保留的契約

- 替代 FR-15 的「必須 OS 共同掛載」及 FR-19 對掛載路徑的要求；所有實際保留的寫入路徑仍須保護已發布內容。
- 原 G0–G2 的共享檔案系統測項改為歷史能力結果，不再是現行協作方案的 gate；原 G0 保持 `BLOCKED`，不是 `PASS`。
- 決策當時需驗證共享工作狀態跨 agent／session 讀寫、私人／Team 邊界、記憶更新完成後的跨 session 檢索及工作恢復；2026-09-14 的完成範圍與限制見上方實施更新。
- 本次 POC 執行時，[ADR-0001](0001-persistent-agent-computer.md) 的固定 VM 列為 `DEFERRED_NOT_VALIDATED`。結案後由 ADR-0003 取代同一 VM 限制；完整必要環境與工作、長期記憶及 10 倍效能原則保留。
- 本地 desktop／browser 的 G3 能力保留。G4 Copilot SDK 整合仍是另行批准項目，不因 Memory 所需模型部署而自動啟用。

## 官方依據

- [Memory 概念、區域與 VNet 限制](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-memory)
- [Memory Store scope 與讀寫介面](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)
- [Hosted agent 使用 Memory Store](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-memory-hosted-agent)
- [Agent Framework shared state](https://learn.microsoft.com/agent-framework/concepts/workflows/state) 與 [checkpoint](https://learn.microsoft.com/agent-framework/workflows/checkpoints)
- [Hosted agent 使用 Toolbox／MCP](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-toolbox-agent)
- [Foundry Workflow Designer 退役與 Hosted 限制](https://learn.microsoft.com/azure/foundry/agents/concepts/workflow)：新方案採 Agent Framework 程式編排，不採將於 2026-12-01 退役的 Designer。
