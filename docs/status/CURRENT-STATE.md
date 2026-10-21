# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-20（+08）；核對origin/main `82fd9975`；stage是第32版（2026.10.20-32，#924），GUARD="1"（打開後不能關、不退版）；deploy-staging run 39366362203與public-sync 39367614602成功；冒煙OK、看到平台原地換版（同SID換image）（總協調10-20；progress紀錄待補）。**第33版（2026.10.20-33）release PR已開、待Neal決定**，沒有新開關。**main凍結到Neal決定**（main前進指紋就過期）。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（Neal 10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。合併者無法由系統強制（free方案無branch protection），靠流程遵守，見ADR-0008 10-16記錄。

**進行中（10-20）：** stage已開2d-i（GUARD）：部署只回報版本，助理在下一個回合結束後背景換版；IDLE、RECONCILER仍"0"，快照URL未設。**ADR-0009前提重核（總協調10-20，ADR待補）：平台原地接續是主路徑，FA的換版（`cut`）是備援。** dev走`ci_deploy`（#919，`deploy/dev/`不匯出公開repo），GUARD="1"。2d-ii是草稿#891。

**GitHub Actions：** repo為private、free方案（2000 Linux分鐘／月），10-16用完，Neal設USD10預算；重型套件不跑PR／push。#865的離線shared_context套件只每週一與手動跑（約130 runner分鐘／次）。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐；HITL A／B／t4開；A／B對話中換模型開；Teams connector第1波（Work IQ）旗標開；Teams逾時回覆補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| 第32版之後合併（10-20，#925～#930） | #926：GUARD開時onboarding加的助理也宣告v2；#927：漂移CAS記下觀察版本宣告的profile，之前已漂移的列在回合結束`cut`自己更正（不手寫Table）；兩者是2d-ii前提。#928：原地漂移的crash-loop寫專案層否決並提示操作者；#925 P3清理；#929、#930只改測試。 |
| 第33版候選dev測試（10-20，`82fd9975`） | 部署成功；#926、#927、#928、2d-ii預覽PASS；smoke部分（一次503 `FOUNDRY_INVOCATION_UNKNOWN`）。#927的v1→v2更正dev沒有v1列，只會在stage A／B跑。完整報告在第33版release PR。 |
| 閘門7（全掃描時間） | 暫時PASS：最大0.8秒（門檻10秒）；樣本從stage的`NATIVE_UPGRADE_CUT`補收（[來源](../progress.md#dev-test-v31-1018)第7項）。 |

## 下一步

1. **第33版**：Neal決定是否合併。
2. **2d-ii之前**（第33版部署後）：A、B、`fa-agent-f167…`各正常用一次；確認2d-ii閘門報告沒有v1 `row_profile`；武裝閘門或確認清單乾淨；`unclaimed`的Agent退役或認領。之後2d-ii #891先合併main再合併。
3. **延後／待補**：延後4a的fence／孤兒、5c、開IDLE；待補Teams relay runbook（dev relay沒有文件化的建置程序）。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 待Neal | 全新環境完整實跑`azd up`；stage加Teams與relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據。排程時鐘在API、stage已開，Neal尚未建第一個排程。stage A重建session要Neal核准。 |
| 接手必知（未修） | `native_turns.py`回合尾巴：Hosted存檔後才標completed（manual／diag／委派等回合會先停SDK，上限約15秒，auto不停）；#916已合併但dev沒碰到競態，completed時間不變。dev第33版測試：背景讀取30秒ReadTimeout後API回503一次（待辦）。 |
| 開放項目 | stage `desktop_act`卡住；azd新環境Portal未驗收（[紀錄](../progress.md#azd-eastus2-1011)）；ADR-0009 §8其餘文案待Neal確認；每次發布要在progress記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| 退役Agent | #867已合併，未完成onboarding由`team-agent-retire-release`釋出（[deploy](../../deploy/README.md#retire-agent)）。只有operator CLI能退役，名額與顯示名稱不釋出。dev `fa-agent-cc96…`補tombstone需批准。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續；見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限**USD800／停損780**（10-13，[紀錄](../fa-cloud-budget-status.md#dev-cap-800-1013)）；10-20第33版測試dev讀數USD705.12（有延遲）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
