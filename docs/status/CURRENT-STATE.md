# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-10-17（+08）；核對origin/main `848e0e08`；stage仍是第30版（2026.10.15-30，#841），10-15部署，冒煙PASS（[紀錄](../progress.md#stage-v30-1015)）；main已多出第30版之後的修正，尚未發release。正常保檔未達成。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，部署與帳本細節以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（Neal 10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`逐次批准；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**（「prod」即stage release）。合併者無法由系統強制（free方案無branch protection，agent以Neal身分合併），靠流程遵守，見ADR-0008 10-16記錄。

**進行中（10-17）：** ADR-0009 2d-i程式完成並已合併（P5～P9；#872修好檔案流程），dev測試進行中。dev開關`FA_NATIVE_SESSION_GUARD_ENFORCE=1`；各環境預設關，打開後不能關、不退版。檔案規則：來源有工作檔才寫`pending`柵欄搬檔（最多約120秒），沒有就直接commit；來源idle／退役／碰不到時不搬。

**GitHub Actions：** repo為private、free方案（2000 Linux分鐘／月），10-16用完，Neal設USD10預算；重型套件不跑PR／push。#865的離線shared_context套件只每週一與手動跑（約130 runner分鐘／次）。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app與工作台常駐；HITL A／B／t4開；A／B對話中換模型開；Teams connector第1波（Work IQ）旗標開；Teams逾時回覆補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式長期只留R1（覆寫／刪除／丟棄未提交改動前先問）。CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| 稽核第二輪1～20項（10-16～17） | 全部修好並合併：#865 CI、#866 stage release閘門（必須有`release.json`）與console登入前只走內網、#867退役卡住的onboarding並刪失敗onboarding留下的Foundry agent、#868 HITL、#869用量帳本重試、#870／#871工作台、#873讀回條412重試、#874 Hosted回合穩健（存檔重試、cancel／unknown合併、通知退避約1.9小時）、#876目的端建立與就緒界限（120秒）、patrol首次延後5～60分鐘、#878 flag關時前置檢查401／403／503維持pending。 |
| 2d-i檔案流程修正（#872，10-17） | FA停來源後不再讀它的檔案；被叫醒的來源再停一次並照staging發布；叫醒時清計時；目的端OSError 503受drive計時界定。[ADR-0009 §10](../adr/0009-version-model-g-actual-version-idle-upgrade.md#10-不叫醒已回收-session-的規則立即適用phase-0)檢查②在dev 10-16 FAILED，改為不讀。 |
| 已知失敗測試（#875、#877） | 53條修好42條；剩11條見`tests/shared_context/known-failing-on-main.txt`。472條需Windows runtime的測試在`ci-quarantine.txt`。 |

## 下一步

1. **發release 31 PR**（改`deploy/stage/release.json`，附變更清單與dev測試報告），由Neal決定是否合併。前置：dev 2d-i測試（部署線重部署最新main，需Neal經部署線批准）。測試狀態（部署線回報）：PASS＝8、9、11（送出不變慢，`api_submit_session` p50 271→236 ms）；暫過＝7（閘門7：2次full掃描最多0.8秒，上限10秒；樣本待stage補）；待做＝1、4（B有工作檔，等#872重部署）、5、6、10。
2. **ADR-0009**：2d-i測試完→2d-ii收斂→phase 4～6。3b閘門已部署、未武裝；2d-i閘門8只擋閒置升級旗標。
3. **待Neal**：全新環境完整實跑`azd up`；stage加Teams與relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據。排程時鐘在API、stage已開，Neal尚未建第一個排程。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 接手必知（未修） | ①`native_turns.py`回合尾巴：Hosted在SDK停止（每步最多15秒）與存檔後才標completed，約15秒內追加會變not_applied；工作台已自動改成新回合（#871），縮短尾巴需設計（對照ADR-0009、CTX-03）。②`m2_copilot.py`的force_stop逾時會放開執行鎖但CLI可能還在跑（最壞約105秒），無測試，牽涉「不能同時有兩個SDK」（#874）。③`native_teams_relay.py`不看`automatic_retry`，讀回條遇可重試412時Teams不重試，未驗證。④11條known-failing（上述檔案）。 |
| 開放項目 | stage `desktop_act`卡住；azd新環境Portal未驗收（[紀錄](../progress.md#azd-eastus2-1011)）；ADR-0009 §8其餘文案待Neal確認；每次發布要在progress記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284仍draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳是已知缺口。 |
| 退役Agent | #867已合併，未完成onboarding由`team-agent-retire-release`釋出（[deploy](../../deploy/README.md#retire-agent)）。只有operator CLI能退役，名額與顯示名稱不釋出，退役同時停用身分。dev `fa-agent-cc96…`補tombstone需批准。 |
| SP v2／v3／role | 都不升全域SP acceptance（v2：6 PASS、第3則FAIL；v3：①FAIL；role-v2 9 PASS／3 FAIL，role v3 3則PASS）。 |
| README移出的限制 | 4項皆未開始：桌面UI驗收、connector stage真人驗收＋GitHub、Team隔離評估、工作檔跨VM延續；見[POC計畫](../POC-ANALYSIS-AND-PLAN.md#從-readme-移來的已知限制neal-2026-10-11)。 |
| 費用 | dev上限**USD800／停損780**（10-13，[紀錄](../fa-cloud-budget-status.md#dev-cap-800-1013)）；release 30測試前後dev約USD600.44→609.91（10-14，之後未更新）；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)、[第27版冒煙](../progress.md#stage-smoke-v27-1012)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
