# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-11深夜（+08）；核對origin/main `9bbb437a`；stage第26版（2026.10.11-26）10-11 22:39部署；dev最後測`5fee7ec8`。正常保檔未達成。
> 本文不是操作批准；原資料、回條、unknown 與共用上限 USD650 保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 2026-10-01 21:33 原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）由B1／B2層提到最高層（Neal 10-02 16:38「提前做」）；其他排程見下一步3。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`逐次批准；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。

**stage第25、26版已部署（deploy-staging皆成功）：** 第25版（#790，merge `150a70a9`，10-11 21:17）與第24版執行期只差#784（Teams relay重試；stage relay仍關）。第26版（#796，merge `9bbb437a`，22:39）執行期同25，只有文件／工具：#791 dev上限650／停損630、#792 README限制移出、#793租戶縮寫與舊v18名稱去識別、#794工作計畫、#795 dev費用最佳化。第24版起已有Team connector第1波（Teams經Work IQ；旗標已開；stage加Teams與Entra redirect-URI租戶寫入仍待Neal）、互動批准卡、toolbox不可用照常回合。25版dev測試（`5fee7ec8`，報告在#790）：8765回歸1～4 PASS（4第一次因模型自編`refs`被VM輸入驗證拒絕，重試通過，非回歸）、Teams文字批准allow／重複／deny PASS、App Insights讀回PASS（A v144）。

**第23版設定沿用（[紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐（約USD11.66／月）；HITL A／B／t4開；A／B新對話`auto-no-custom-v1`。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。**FR-09（#432）：** 不設全域同時執行上限；同Agent依序、不同Agent並行。DELEG-05只防卡死、藏名額，保留合法checkpoint；不改76項或帳本。

**Neal 10-03 13:16「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；SP-27暫不適用；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。

**native：** A／B跨版回合live通過；A→B交辦56b0通過；A桌面與Playwright瀏覽器跨回合常駐（e7e6，SP-08 PASS）。CAP-06「＋」建test agent已live（#298），只限原Team。原件與unknown不動。

原則是**只保留足以防資料損失、重送與明顯錯誤的最小機制**；並且**SDK-first／Hosted 優先：FA 自建一層前，先查官方功能與最佳實踐，PR 附官方連結並說明官方缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。docs-only PR不跑Opus；權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| 公開repo（10-11） | 第25版同步未過人工gate 2（租戶名稱縮寫、v18儲存名稱），#793修正並隨第26版同步。Neal核准把`fa-agent`歷史改寫成單一乾淨commit（v26樹的orphan commit、刪v25 tag／Release、v26改指向）；腳本已備但被工具權限擋住，由Neal自己執行。v26的gate 2內容審查進行中；`fa-agent`維持private，何時改public由Neal決定；不再刪除重建repo（Neal 10-11）。[規則](../public-sync.md#每一版的兩道人工審查) |
| dev費用最佳化（10-11） | 上限USD650／停損630（Neal），不降規模；舊VNet POC殘留已刪；A／B閒置逾時900秒（A v145／B v101，core `0b09441b`）。[紀錄](../fa-cloud-budget-status.md#dev-cost-cleanup-1011) |

## 下一步

1. **下一版stage release**：目前沒有待部署的執行期變更（26＝25）；有時先dev驗證，再由總協調開release PR，Neal決定。待Neal：執行`fa-agent`歷史改寫、stage加Teams、Entra redirect-URI租戶寫入、Teams登入、PG稿`5055d28c`送否、`fa-agent`何時改public。Teams內對話[N2](../teams-chat-design.md)：T1已合併，T2見[dev紀錄](../progress.md#teams-relay-t2-1010)；Teams批准只收allow_once／deny，以主人oid為依據、不另核`Behavior.Approve`（Neal 10-11核准例外，範圍見[SPEC](../FA-AGENT-BEHAVIOR-SPEC.md)）。TC-06維持75；DELEG-01等c7bb、t4保留INFEASIBLE。
2. **RTT最佳化**：暖回合與打字預熱已上stage。B[工作檔缺口](../../src/fa-shared-context-poc/README.md#suspended-session-file-gap)未完成，Files真人list403已停；④仍拒不搬檔ready。
3. [Native routines](../progress.md#native-routines-offline-progress)：時鐘在API，stage已開；Neal尚未建第一個排程。循環交辦HITL-on仍未知；SK-07②只查原工具，啟用交fdbb。其餘[B1/B2](PROGRESS-B1-B2.md)排程不變，SP-12不做；83d4接MEM-05／07、TN-2延後；Team MCP走官方Toolbox API、批准照`require_approval`（Neal 10-06，#682）。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 開放項目 | stage `desktop_act`卡住（尚未調查）；模型「已發」措辭P3；azd新環境（#766、#785已合併，fazd1010～1012已刪）Portal未驗收（[紀錄](../progress.md#azd-eastus2-1011)）。 |
| stage「＋」IAM | #468：stage API UAMI受限RBAC Admin，已部署；Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，N2／T5／跨版／A→B live PASS。舊CID不搬不刪。①已改Responses、③暫定；#306記憶風險例外不變，私人記憶A／B／test已開。未接consumer仍明拒；#284仍draft。 |
| ADR-0006 | T1～T5均已核准並部署。T6a／T6b建立規則已實作、未部署，只存數值；T6b接單／入帳與T4b私人記憶用量帳降backlog（Neal 10-02），只留組織總上限，私人記憶用量未入帳是已知缺口。#306例外延伸到Team note（#400）與stage「＋」自動指派（#468）。 |
| SP v2／v3／role-v2／v3 | v2：6 PASS、第3則FAIL、第7則前提不足。v3（4afa）：③PASS、①FAIL、②NOT_RUN。role-v2（1c05）9 PASS／3 FAIL；role v3（3006）3則全PASS。都不升全域SP acceptance。 |
| README移出的限制 | 4項皆未開始，依序：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續（最後）；清單在[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限USD650（Neal 10-11，[原話](../fa-cloud-budget-status.md#dev-cap-650-1011)），停損USD630，不降規模；10-11實查約USD526.5（報表晚一天）；stage另列月USD200。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[stage 10-04](../progress.md#2026-10-04stage-發布-20261004-2-3-與完整測試4-項未過修理中)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
