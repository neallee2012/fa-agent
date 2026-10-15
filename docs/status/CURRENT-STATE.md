# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-15（+08）；核對origin/main `b47c3cb1`；stage第29版（2026.10.14-29，#828）10-14部署；dev最後測main `1c2be0b4`（release 30），工作台在`b47c3cb1`重測中。正常保檔未達成。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）由B1／B2層提到最高層（Neal 10-02 16:38「提前做」）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`逐次批准；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。

**進行中（10-14～15）：** release 30 dev測試（main `1c2be0b4`）發現的工作台問題已由#837修好，`b47c3cb1`工作台重測中；之後見下一步1。

**stage第28～29版已部署：** 28（#817，10-13）帶工作台停靠卡片／頁內停止確認與G phase 0。29（10-14 09:16合併，deploy-staging成功）：stage A／B打開對話中換模型（`FA_NATIVE_MODEL_SWITCH=1`，#822）、G phase 1綁定列（#818，只記錄）、public-sync自動第二道（#820，首次執行通過）。第29版stage冒煙PASS（[紀錄](../progress.md#stage-smoke-v29-1014)）。

**main上待release 30（第29版之後）：** G 2a #824、2b #825、2c通知#830、phase 3送出路徑#831、3b部署前相容閘門#829（唯讀、未打開）；Teams逾時回覆補送#827（relay端改動要stage relay才生效，仍關，需Neal）；稽核修正#832～#836與#837（見近期完成）。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐（約USD11.66／月）；HITL A／B／t4開；A／B新對話`auto-no-custom-v1`；Teams connector第1波（Work IQ）旗標開。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。native：A／B跨版回合、A→B交辦live通過；A桌面與Playwright常駐（SP-08 PASS）；CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。docs-only PR不跑Opus；權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| release 30 dev測試（10-14，`1c2be0b4`） | 部署／follow／閘門PASS（閘門5.5秒）、通知、追加、換模型PASS；交辦回覆約13秒才出現（#833退化）與停止顯示「不確定」→#837修正（另改追加排入文案）。[紀錄](../progress.md#dev-test-v30-1014)。 |
| ADR-0009 G phase 0～3b（10-12～14） | 0（#813、#815）與1已上stage；2a～2c、3、3b已合併待release 30。設計10-13定稿、M1～M10已核准（#823；[紀錄](../progress.md#adr0009-idle-upgrade-final-1013)）。[ADR-0009](../adr/0009-version-model-g-actual-version-idle-upgrade.md)10-14修訂：開放RID集合的最終形狀是另一個`open-rids`項目，不搬進`agent`紀錄（#836）。 |
| 稽核修正（10-14） | /ponytail-audit第1～18項：#832部署／CI、#833工作台、#834 API、#835 Hosted、#836開放RID集合，已合併、未部署。 |

## 下一步

1. **release 30**：`b47c3cb1`工作台重測（需Neal登入）→總協調開release 30 PR附dev報告→Neal決定。待Neal：stage加Teams與stage relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據（Neal 10-11核准例外，見[SPEC](../FA-AGENT-BEHAVIOR-SPEC.md)）。
2. **ADR-0009**（不相容一律不拒絕；3b閘門未打開）：2d-i擴充→搬移→閘門→2d-ii收斂→phase 4～6。
3. **RTT與排程**：暖回合與打字預熱已上stage；B[工作檔缺口](../../src/fa-shared-context-poc/README.md#suspended-session-file-gap)未完成。[Native routines](../progress.md#native-routines-offline-progress)時鐘在API、stage已開，Neal尚未建第一個排程；其餘[B1/B2](PROGRESS-B1-B2.md)不變，SP-12不做；Team MCP走官方Toolbox API（Neal 10-06，#682）。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| phase 0 dev證據 | ①`get_session`不喚醒：PASS（10-13）；②對FA自己stop的session做Files GET前後讀狀態：**未做**。 |
| 開放項目 | stage `desktop_act`卡住（未調查）；模型「已發」措辭P3；azd新環境Portal未驗收（[紀錄](../progress.md#azd-eastus2-1011)）；ADR-0009 §8文案待Neal確認。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續；見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限**USD800／停損780**（Neal 10-13，[紀錄](../fa-cloud-budget-status.md#dev-cap-800-1013)）；release 30測試前後dev約USD600.44→609.91（[紀錄](../fa-cloud-budget-status.md#dev-v30-test-1014)）；A／B dev閒置逾時3540秒（[紀錄](../fa-cloud-budget-status.md#dev-cost-cleanup-1011)）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)、[第27版冒煙](../progress.md#stage-smoke-v27-1012)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
