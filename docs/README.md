# FA 文件導覽

專案對外介紹見根目錄的 [README](../README.md)。新工作階段先讀 [AGENTS](../AGENTS.md) 與 [CURRENT-STATE](status/CURRENT-STATE.md)，再依任務讀下表的文件。下表是導覽，不是即時部署狀態或操作批准。

| 文件 | 內容 |
|---|---|
| [專案協作指引](../AGENTS.md) | 開工入口、SDK-first／Hosted 架構界線、批准與證據原則 |
| [目前狀態摘要](status/CURRENT-STATE.md) | 新工作階段先讀：現在在哪、近期完成、下一步與待核對事項 |
| [開發與規格維護慣例](CONVENTIONS.md) | 文件權威、變更同步、針對性驗證、POC 結果與部署差異 |
| [擴充 FA](extending.md) | 新使用者加 MCP、skills、connector：`azd up` 後的預設狀態、要不要改程式，以及 FA 沒有 plugin 機制 |
| [公開 repo 同步](public-sync.md) | stage 部署成功後把該版去識別化推到公開 repo：排除清單、fail-closed 檢查、Neal 的一次性設定與私有連結待決 |
| [CURRENT-STATE 工作法研究](current-state-workflow-research.md) | 來源 repo 的短摘要、權威分工與實際接線；區分明文規則、觀察與移植建議 |
| [Grok 四章 Context 完整比較](grok-context-comparison.md) | 系統提示、回合組裝、逐字稿／壓縮及 skills 的逐項差異；公開基準、WIP 與實驗結果分開 |
| [完整 Context Target State](grok-aligned-target-state.md) | 76 個目標、12 個跨回合情境；完整定義不等於全部已實作或驗收 |
| [B1／B2 進度讀數](status/PROGRESS-B1-B2.md) | 76＋34 項逐 ID 階段、證據與加權進度；只是讀數，不是部署或合併前置條件 |
| [Context 評測 baseline](context-evaluation-baseline.md) | M4 checker 限制、兩輪乾淨對話人審、原 8 pass／1 strict mismatch 及品質界線 |
| [CTX-03 本機驗收結論](ctx03-local-acceptance.md) | 一般大狀態可恢復；default compaction 候選僅 test-only 可行，正式產品仍未採用 |
| [CTX-03 保存契約](ctx03-checkpoint-contract.md) | `v1` 已確認：Q11 最小來源接線、拒絕與採用條件；產品尚未實作或啟用 |
| [Context 研究證據索引](context-research-evidence.md) | 固定來源／hash 與未公開原件的界線；不附 traces、私有快照、日誌或資料庫 |
| [行為控制規格 v1.2](FA-AGENT-BEHAVIOR-SPEC.md) | 完整後續契約：補終局未生效、實際內容批准綁定、必要材料 pin／GC；B01–B12 分離線與環境證據，P2 待辦及啟用限制明列，新增能力未實作 |
| [行為控制分工決策](adr/0004-agent-behavior-control.md) | Hosted Harness＋FA 耐久控制＋平台執行端；承接 M4，不擴張本批批准 |
| [Hosted 優先與連續性決策](adr/0003-hosted-environment-continuity.md) | 現行原則：允許 compute 更換，驗收獨立環境與工作連續性 |
| [原始持久助理決策](adr/0001-persistent-agent-computer.md) | 長期助理、context 與 10 倍目標；同一 VM 限制已被 ADR-0003 取代 |
| [共享 context 決策](adr/0002-shared-context-without-file-share.md) | 以共享工作狀態、長期記憶與工具取代 File Share；Non-VNet Foundry 的取捨 |
| [領域用語](../CONTEXT.md) | Agent、助理電腦、工作、記憶與本輪脈絡的區別 |
| [需求](requirements.md) | 已確認需求、首版邊界與暫緩項目 |
| [規格與待決事項](specification.md) | 產品物件、行為契約、候選架構與未知項目 |
| [Agent 行為設計背景](FA-AGENT-BEHAVIOR-DESIGN.md) | Grok 九面向、取捨與補證據；已由行為控制規格承接，不再作開發／驗收規範 |
| [行為設計官方查證](FA-BEHAVIOR-PLATFORM-EVIDENCE.md) | Foundry／Copilot SDK、記憶、交易與訊息語義的支持範圍，不代替 FA 實測 |
| [POC 完整方案](FA-Hosted-Sandbox-POC-Spec.md) | v1.2 共享 context 修訂、原始 G0–G3 歷史規格及可選 G4 |
| [POC 結果](../POC-RESULTS.md) | 共享 Context 雲端驗收、部署、費用限制與歷史實測 |
| [POC 分析與後續計畫](POC-ANALYSIS-AND-PLAN.md) | 結果剖析、證據限制、Hosted 角色分工與 M0–M5 執行順序 |
| [M0 證據基準](M0-EVIDENCE-BASELINE.md) | 三個共享Context runs原件核對、去敏catalog與未結清事項 |
| [M1 私人環境結果](M1-PRIVATE-ENVIRONMENT-POC.md) | 私人profile、雲端備份、替代session及A8→A9相容版本恢復通過；歷史失敗保留 |
| [M1 雲端備份設計](M1-CLOUD-BACKUP-DESIGN.md) | 專用Blob／私網、API MI最小授權、固定版本還原及已執行上限 |
| [M2 助理與自主 UI 結果](M2-ASSISTANT-POC.md) | 文字／checkpoint／A→B發布通過；UI失敗原件、額度與收尾，不標完整M2 PASS |
| [M3 成果與人工批准結果](M3-ARTIFACTS-POC.md) | 真成果／不可變版本、真人下載／決定與後續模型採用生效SOP通過；歷史FAIL保留，附CLI入口 |
| [M4 Context 品質與時間結果](M4-CONTEXT-POC.md) | 最新r3四案通過、checkpoint前incomplete並停止；歷史FAIL保留；程式基準 `1ca1c762` 已推送 |
| [原件與權威儲存研究](FA-DATA-STORE-RESEARCH.md) | 參考Grok的分層；Table＋Blob保存原件／版本，Memory為投影，交易與備份邊界分開 |
| [Hosted HOME官方查證](fa-hosted-home-ownership-evidence.md) | 已確認持久化語意；non-root ownership初始化仍NOT_CONFIRMED，附支援問題 |
| [共享 Context 實作](../src/fa-shared-context-poc/README.md) | API、身分邊界、Memory 契約與同 sandbox GUI |
| [雲端驗收 controller](../tests/shared_controller/README.md) | 指定 A/B sessions、衝突／恢復／GUI 與有界收尾 |
| [POC 歷史交接](FA-POC-Handoff.md) | 特定舊批次的交接材料；不是通用開工入口或新批准 |
| [詳細進度與歷史](progress.md) | 帶日期的實作、部署及結果紀錄；由 CURRENT-STATE 按需導入 |
| [平台研究](fa-platform-evidence.md) | Foundry／Copilot SDK 官方證據與訪談決策 |
| [掛載與桌面查證](fa-hosted-mount-desktop-evidence.md) | 最新 TypeSpec／REST／SDK 與 browser 範例的精確界線 |
| [Grok Bot Box 研究](grok-box-evidence.md) | 電腦生命週期、身份與隔離差異 |

## 各階段 POC 結果

M1～M4 各階段的結果、費用與證據界線，分別寫在 [M1](M1-PRIVATE-ENVIRONMENT-POC.md)、[M2](M2-ASSISTANT-POC.md)、[M3](M3-ARTIFACTS-POC.md)、[M4](M4-CONTEXT-POC.md) 與 [POC 結果](../POC-RESULTS.md)。報告引用的 `evidence/`、`.azure/` 不隨 Git 提供。

## 給接手 agent

先讀 [AGENTS](../AGENTS.md) 與 [CURRENT-STATE](status/CURRENT-STATE.md)，再按任務讀它們指向的規格、程式與結果。不要把歷史 handoff 當成現在的工作排程；也不要從舊 worktree 推定另一工作線尚未開始。

工作狀態改變時，先更新對應來源與必要的 [詳細紀錄](progress.md)，再同步短摘要。操作權限、未決效果與原批次紀錄仍須核對，不能因整理文件而重啟、清零或重送。

本 repository 的公開研究來源不含 Azure 資源憑證、私人記憶、原始對話或工具日誌。實作證據也應去敏後再提交；不要把「部署成功」當成 POC 驗收成功。
