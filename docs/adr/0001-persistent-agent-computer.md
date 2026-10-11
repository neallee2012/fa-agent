# 代管、固定電腦上的長期 AI 助理

狀態：partially superseded by ADR-0003｜原決策日期：2026-09-13。

**2026-09-14 更新：** 使用者已透過 [ADR-0003](0003-hosted-environment-continuity.md) 將「同一台固定 VM」改為「獨立、可持續恢復的 Hosted 工作環境」。下文是原決策紀錄；DP-02 及其固定 VM 實作限制不再生效。長期助理、完整必要環境與工作、context 管理及 10 倍效能目標保留，不把這次更正視為已通過持久環境驗收。

FA 的架構定位是「代管的 OpenClaw」：有長期記憶的 AI 助理，長期跑在自己的固定 VM 上，保有完整持久的運行環境與工作，並以比 OpenClaw 快一個量級為目標。這是使用者指定的產品方向，不代表採用或 fork OpenClaw 本體，也不是已量測的競品效能結論。

## 已確認的原則

| ID | 原則 |
|---|---|
| DP-01 | 助理是長期存在的工作主體，不是每次任務或對話臨時建立的 sandbox。長期記憶與工作生命週期獨立於本輪模型 context。 |
| DP-02 | 每個 agent 長期綁定同一台 VM；可關機／重啟，只在故障或明確維護時移轉。不以閒置、換任務或換對話為由另配一台。 |
| DP-03 | 持久範圍涵蓋完整工作環境與工作：已核准的工具／相依套件、設定、私人檔案、browser profile、記憶及工作進度。只保存 `$HOME` 或聊天紀錄不算符合。 |
| DP-04 | Context 管理是核心能力：依工作載入相關記憶與狀態，不靠每輪重播所有歷史，也不能靠遺失重要資訊換取速度。 |
| DP-05 | 比 OpenClaw 快一個量級（10 倍）是必須驗證的產品目標。必須在可比較任務、環境與品質門檻下量測，不能只憑框架選擇或體感宣稱達成。 |

使用者已明確選擇 DP-02，而非「只固定私人環境，閒置後容許底層 compute 替換」。固定的是 agent 與 VM 的長期綁定，不要求雲端底層實體伺服器永不更換。

沿用先前不要求 RAM、所有程序與視窗完整快照的邊界；關機／重啟後應由持久狀態恢復工作。Browser 登入仍受撤銷、到期與服務政策限制，「完整持久」不代表保存所有憑證明文或承諾永遠免重登。

## 取捨與後果

- 不採用「每任務／對話重建電腦」或「閒置後以新 compute 還原少量檔案」作為正常產品生命週期。固定 VM 的成本、維護及容量管理是必須另行設計與批准的代價，不以放寬原則消除。
- 助理的常駐 runtime／harness 與私人 CLI、desktop、browser 應以這台 VM 為執行環境；模型服務、受控 connectors 及組織管理功能可在外部。只固定遠端 desktop、仍讓助理 runtime 隨任務漂移的方案不能直接視為符合。
- Foundry Hosted Agents 與 Copilot SDK 是原指定路線，但不能反過來縮減上述產品契約。若平台契約不符，需提出落差並重新決策，不自動改用外部 VM，也不以保活請求假裝平台保證固定 VM。
- 既有 G0–G3 只驗掛載、共享 I/O、idle／resume 與 GUI，G4 只驗 SDK 整合；全部通過也不代表固定 VM、完整持久環境、長期記憶或 10 倍效能已驗收。

2026-09-13 重讀 [Hosted overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#session-compute-lifecycle) 與 [session 管理](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)：目前公開的標準生命週期會在 idle 後釋放 compute，resume 時配置新 compute。這與 DP-02 不相容；它是文件契約落差，不是實測失敗，也不能外推成所有 Foundry 部署模式都不支援固定 VM。

需求與驗收細節見 [requirements.md](../requirements.md) 與 [specification.md](../specification.md)。後續只有使用者的明確決策可以更改這些原則。
