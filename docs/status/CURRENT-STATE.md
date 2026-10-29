# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-28（+08）；核對origin/main `25a1f970`；stage是第36版（2026.10.25-36，#986，[部署後驗證](../progress.md#stage-v36-1026)）：API與Hosted只服務v2（2d-ii，#935）、閘門武裝，GUARD="1"（不能關、不退版）。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（Neal 10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。合併者靠流程遵守（無branch protection，見ADR-0008 10-16）。

**進行中（10-22）：** 2d-ii（#891）已在stage：API與Hosted只服務`foundry-native-v2`，`stage.json`與dev都武裝相容閘門（#934）；IDLE、RECONCILER仍"0"，快照URL未設。**ADR-0009前提重核（[ADR註記](../adr/0009-version-model-g-actual-version-idle-upgrade.md#platform-in-place-resume-2026-10-20)）：平台原地接續是主路徑，FA的換版（`cut`）是備援。** dev走`ci_deploy`（#919，`deploy/dev/`不匯出公開repo）。

**GitHub Actions：** repo為private、free方案（2000 Linux分鐘／月），10-16用完，Neal設USD10預算；重型套件不跑PR／push；#865的離線套件只每週一與手動跑。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐；HITL A／B／t4開；A／B對話中換模型開；Teams connector第1波（Work IQ）旗標開；Teams逾時回覆補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| stage第36版（10-25） | 閘門armed＋pass、0拒絕；公開`v2026.10.25-36`（含私有化部署）；A／B冒煙OK；10-26 A每日排程四天unknown後首次completed（[驗證](../progress.md#stage-v36-1026)）。 |
| 品質整理第二輪（10-27） | 拆11個高CC函式；刪M2–M4舊叢集與僅測試用的behavior_*（#1010、#1013，Neal核准）；[基準](../code-quality-baseline.md#第二輪結果2026-10-27)。10-28 dev測試通過（含#1019～#1021，[紀錄](../progress.md#dev-test-r2-1028)）；API啟動374→282→142秒（[紀錄](../progress.md#startup-private-1028)）；下次stage部署重建API與Hosted；`azure.yaml`、`behavior-job.bicep:8`仍提已刪映像。 |

## 下一步

1. **送出延遲**：#949、#955、#956已在dev（warm A約1.3秒，未上stage）；剩換版後首次listing、開頁被扣2～4秒（[觀察](../progress.md#stage-v35-1022)）。
2. **azd端到端**（10-21～22，公開repo `v2026.10.21-34`，[紀錄](../progress.md#azd-e2e-public-1021)）：前10分鐘PASS、已拆除無殘留；#936的`y/N`未重跑。
3. **私有化部署**（[ADR-0010](../adr/0010-azd-private-network-option.md)、[ADR-0011](../adr/0011-private-mode-cosmos-memory.md)）：隨第36版10-25上stage並公開（stage仍public）。10-26 Neal決定stage改private（Foundry與資料重建、可失去Teams）；dev演練A2+B1 PASS（[紀錄](../progress.md#stage-private-rehearsal-1026)），第9節8點10-28 dev 7/8 PASS（WebIQ可用，[紀錄](../progress.md#startup-private-1028)）。未收：DNS existing、延遲及格線、費用。
4. **再來**：4a需要的一半、6b；5c、開IDLE延後；待補Teams relay runbook。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 待Neal | stage加Teams與relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據。排程時鐘在API、stage已開，A每日routine已在跑。stage A重建session要Neal核准。 |
| WebIQ只留toolbox | dev三步都做完（[步驟](../../deploy/README.md#dev-webiq-env-removal)）；stage沒有這些步驟。 |
| 接手必知（未修） | `native_turns.py`回合尾巴：Hosted存檔後才標completed（manual／diag／委派等回合會先停SDK，上限約15秒，auto不停）；#916已合併但dev沒碰到競態，completed時間不變。 |
| 每日routine | 10-22～25停在`unknown`（`gpt-6.1-sol` 500K TPM、context逐日變大）；#957改每次開新對話，10-26首次completed；TPM仍dev／stage共用（[根因](../progress.md#stage-v35-1022)、[10-26](../progress.md#stage-v36-1026)）。 |
| 開放項目 | A「超過2小時」橫幅疑似誤報；E換版未驗；A重複404是bridge輪詢交辦回覆（設計行為），預期的404 WARNING要不要降級待決；stage `desktop_act`卡住；azd eastus2（[10-11](../progress.md#azd-eastus2-1011)）Portal未驗收；ADR-0009 §8其餘文案待Neal確認；每次發布要在progress記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| 退役Agent | #867已合併，未完成onboarding由`team-agent-retire-release`釋出（[deploy](../../deploy/README.md#retire-agent)）。只有operator CLI能退役，名額與顯示名稱不釋出。dev `fa-agent-cc96…`補tombstone需批准。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續；見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限**USD900／停損880**（10-25，[紀錄](../fa-cloud-budget-status.md#dev-api-scale-zero-1025)）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
