# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-11-04（+08）；核對origin/main `f3118993`；stage是第41版（2026.11.04-41，#1171，[紀錄](../progress.md#stage-v40-v41-1104)）：SDK 1.0.26／runtime 1.0.103，API只送v3（3d-ii）、閘門武裝，GUARD="1"（不能關、不退版）。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（Neal 10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**。合併者靠流程遵守（無branch protection，見ADR-0008 10-16）。

**進行中（11-04）：** 3d-ii（#1167）已在stage：API只送`foundry-native-v3`、支援集合只剩v3；v2別名與v2重送已拿掉（#1179，ADR-0009 §3③），隨第43版上stage。`stage.json`與dev都武裝相容閘門（#934）；IDLE、RECONCILER仍"0"，快照URL未設。**ADR-0009前提重核（[ADR註記](../adr/0009-version-model-g-actual-version-idle-upgrade.md#platform-in-place-resume-2026-10-20)）：平台原地接續是主路徑，FA的換版（`cut`）是備援。** dev走`ci_deploy`（#919，`deploy/dev/`不匯出公開repo）。

**GitHub Actions：** repo為private、free方案（2000 Linux分鐘／月），Neal設USD10預算；11-04第41版deploy與public-sync跑成功，10-28原因未查明。重型套件不跑PR／push。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐；HITL A／B／t4開；A／B對話中換模型開；Teams connector第1波（Work IQ）旗標開；Teams逾時回覆補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| stage第40、41版（11-03～04） | SDK 1.0.26上stage；閘門pass、公開第二道通過；Neal實測A、B、交辦通過。E的模型拒絕見[紀錄](../progress.md#stage-v40-v41-1104)。 |
| dev全新安裝（10-30） | 新表`workstatefresh`；本機bridge要開HITL才有Connect卡（[紀錄](../progress.md#r7-close-1031)）。 |
| 品質整理二至十三輪 | 第十三輪見[基準頁](../code-quality-baseline.md#第十三輪結果2026-11-04)：刪死碼、補測、拆`_read`；第十二輪全量的失敗#1172已修，未重跑。 |

## 下一步

0. **第43版**（Neal 11-04：不出42版）：#1170新助理照A選版、#1174推理模型只走Responses、拿掉v2別名與重送（#1179；`OLDER_RELEASES`的1.0.13／1.0.83要等3d-ii前的對話遷移完或確認沒有）、ub-mode第十三輪。M11核准：Guard隨44版，一步升SDK最早45版。
1. **送出延遲**：剩換版後首次listing、開頁扣2～4秒（[觀察](../progress.md#stage-v35-1022)）。
2. **其他**：azd端到端（`v2026.10.21-34`前10分鐘PASS、#936未重跑，[紀錄](../progress.md#azd-e2e-public-1021)）；私有化（[ADR-0010](../adr/0010-azd-private-network-option.md)／[0011](../adr/0011-private-mode-cosmos-memory.md)，Neal 10-26決定stage改private，[dev演練](../progress.md#stage-private-rehearsal-1026)、[10-28 7/8](../progress.md#startup-private-1028)；未收DNS existing、延遲及格線、費用）；4a一半、6b、5c、開IDLE延後；Teams relay runbook。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 待Neal | E改Responses選版；stage加Teams與relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據。排程時鐘在API、stage已開。stage A重建session要Neal核准。 |
| WebIQ只留toolbox | dev三步都做完（[步驟](../../deploy/README.md#dev-webiq-env-removal)）；stage沒有這些步驟。 |
| 接手必知（未修） | `native_turns.py`回合尾巴：Hosted存檔後才標completed（manual／diag／委派等回合會先停SDK，上限約15秒，auto不停）；#916已合併但dev沒碰到競態，completed時間不變。 |
| 每日routine | #957後10-26起completed；TPM仍dev／stage共用（[根因](../progress.md#stage-v35-1022)）。vocus.cc每天等批准逾時，A的排程11-01已停用（[紀錄](../progress.md#stage-v38-verify-1031)）。 |
| 開放項目 | stage A一次`turn_status`逾時30秒（未定因）；B唯讀交辦已completed仍顯示「正在回覆」（推定已修，待dev實看）；dev B未連Teams，等Connect卡要不要擋整個回合待決（[10-30](../progress.md#fa-context-b-connect-wait-1030)）；A「超過2小時」橫幅疑似誤報；A重複404是bridge輪詢交辦（設計行為），WARNING要不要降級待決；stage `desktop_act`卡住；azd eastus2（[10-11](../progress.md#azd-eastus2-1011)）Portal未驗收；ADR-0009 §8其餘文案待Neal確認；每次發布要在progress記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| 退役Agent | #867已合併，未完成onboarding由`team-agent-retire-release`釋出（[deploy](../../deploy/README.md#retire-agent)）。只有operator CLI能退役，名額與顯示名稱不釋出。dev `fa-agent-cc96…`補tombstone需批准。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始，見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限**USD900／停損880**（10-25，[紀錄](../fa-cloud-budget-status.md#dev-api-scale-zero-1025)）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
