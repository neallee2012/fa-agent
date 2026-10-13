# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-13（+08）；核對origin/main `01e0fdb2`；stage第27版（2026.10.12-27）10-12約09:20部署，執行期同25／26；dev最後測`5fee7ec8`，`01e0fdb2`正在部署測試。正常保檔未達成。
> 本文不是操作批准；原資料、回條、unknown 與共用上限 USD650 保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）由B1／B2層提到最高層（Neal 10-02 16:38「提前做」）；其他排程見下一步3。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`逐次批准；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。

**進行中（10-13）：** Neal 10-13早上（07:1x前）批准這一次dev部署main `01e0fdb2`測release 28，重申dev上限USD650／停損630（[紀錄](../fa-cloud-budget-status.md#dev-cap-650-1013)，10-12實查約USD570）。測完由總協調開release 28 PR，附變更清單和dev報告，Neal決定是否合併。

**stage第25～27版已部署：** 第24版起有Team connector第1波（Teams經Work IQ，旗標已開）、互動批准卡、toolbox不可用照常回合。第25～27版執行期只差#784（Teams relay重試；stage relay仍關），26、27只有文件／工具。25版dev測試（`5fee7ec8`，報告在#790）與第27版stage冒煙（10-12）一般回合、WebIQ、A→B皆PASS。[冒煙紀錄](../progress.md#stage-smoke-v27-1012)

**第23版設定沿用（[紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐（約USD11.66／月）；HITL A／B／t4開；A／B新對話`auto-no-custom-v1`。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。

**native：** A／B跨版回合live通過；A→B交辦通過；A桌面與Playwright瀏覽器跨回合常駐（SP-08 PASS）。CAP-06「＋」建test agent已live（#298），只限原Team。原件與unknown不動。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。docs-only PR不跑Opus；權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| 版本模型G phase 0（10-12～13） | #813（`8732d7b7`）加#815（`01e0fdb2`）已合併：不再叫醒已回收或舊的session，follow與舊對話只碰`active`。**已合併，dev部署測試中**；`get_session`不喚醒dev實測PASS（10-13）。[紀錄](../progress.md#adr0009-phase0-merged-1013) |
| ADR-0009 G（10-12） | #814：Neal 10-12 19:53確認8項，A不實作，AGENTS界線2已改；閒置升級仍是**草案**（生命週期未定、9個未解問題），phase 1～6未實作。[ADR-0009](../adr/0009-version-model-g-actual-version-idle-upgrade.md) |
| 工作台就地確認（10-12） | #812：確認改在頁內，不再跳瀏覽器對話框。 |

## 下一步

1. **dev部署並測`01e0fdb2`（進行中）**，同時驗phase 0剩下的dev項；通過後總協調開**release 28 PR**，Neal決定（stage第28版會帶phase 0）。待Neal：stage加Teams、Entra redirect-URI租戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據（Neal 10-11核准例外，見[SPEC](../FA-AGENT-BEHAVIOR-SPEC.md)）。
2. **RTT最佳化**：暖回合與打字預熱已上stage。B[工作檔缺口](../../src/fa-shared-context-poc/README.md#suspended-session-file-gap)未完成，Files真人list403已停；④仍拒不搬檔ready。
3. [Native routines](../progress.md#native-routines-offline-progress)：時鐘在API，stage已開；Neal尚未建第一個排程。其餘[B1/B2](PROGRESS-B1-B2.md)排程不變，SP-12不做；Team MCP走官方Toolbox API、批准照`require_approval`（Neal 10-06，#682）。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| ADR-0009 通知 | [§8](../adr/0009-version-model-g-actual-version-idle-upgrade.md)三句新文案（舊環境已失效／舊環境逾時仍在運作／平台同一session換版）**待Neal確認**；工作台目前**不顯示**§8通知（含不搬檔那句，追蹤中，phase 2補）。 |
| phase 0 dev證據 | ①`get_session`不喚醒：PASS（10-13，24個session兩次快照無變）；②對FA自己stop的session做Files GET前後讀狀態：**未做**。另：`list_sessions`的`created_at`＝最後存取時間，漂移不靠它，見[ADR-0009](../adr/0009-version-model-g-actual-version-idle-upgrade.md)。 |
| 開放項目 | stage `desktop_act`卡住（未調查）；模型「已發」措辭P3；stage工作台不自動更新已完成回合（P3）；azd新環境Portal未驗收（[紀錄](../progress.md#azd-eastus2-1011)）。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續；見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限USD650／停損630（Neal 10-11定、10-13重申，[紀錄](../fa-cloud-budget-status.md#dev-cap-650-1013)），不降規模；A／B dev閒置逾時3540秒（[紀錄](../fa-cloud-budget-status.md#dev-cost-cleanup-1011)）；10-12實查約USD570（報表晚一天）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)、[stage 10-04](../progress.md#2026-10-04stage-發布-20261004-2-3-與完整測試4-項未過修理中)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
