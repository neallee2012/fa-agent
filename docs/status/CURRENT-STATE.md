# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-11-21（+08）；核對origin/main `ceaea1c9`；stage是第54版（2026.11.21-54，#1300，[紀錄](../progress.md#stage-v54-1121)）：能力上限12→128（dev、stage同值）；第53版起長對話65%提早壓縮。API只送v3、閘門武裝，GUARD="1"（不能關、不退版）。
> 本文不是操作批准；原資料、回條、unknown與費用上限保持，以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33原文）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（Neal 10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，只在改`deploy/stage/release.json`或手動時部署，**每次都需Neal核准；release PR流程（dev測試報告、免退版演練）依AGENTS**。無branch protection，合併者靠流程遵守（ADR-0008 10-16）。

**進行中：** 舊job lane退役已收尾，API端舊client方法與讀舊列模型刻意留（[紀錄](../progress.md#stage-v49-1118)）。11-07起，API只送`foundry-native-v3`。`stage.json`與dev都武裝相容閘門（#934）；IDLE、RECONCILER仍"0"，快照URL未設。**ADR-0009前提重核（[註記](../adr/0009-version-model-g-actual-version-idle-upgrade.md#platform-in-place-resume-2026-10-20)）：平台原地接續是主路徑，FA的換版（`cut`）是備援。** dev走`ci_deploy`（#919，`deploy/dev/`不匯出公開repo）。

**GitHub Actions：** private repo、free方案（2000分鐘／月），Neal設USD10預算；11-21第54版deploy與public-sync成功；public-sync逾時已30→60分鐘（#1285）。重型套件不跑PR／push。

**沿用設定（[第23版紀錄](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；feed app、工作台常駐；HITL A／B／t4、A／B對話中換模型開；Teams connector第1波（Work IQ）旗標開；Teams逾時回覆補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 一Team一位使用者、同Team互信；每Agent一台VM與排他queue；資料改API薄轉送，不提供刪除。FR-09（#432）：不設全域同時執行上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式只留R1（覆寫／刪除／丟棄未提交改動前先問）。CAP-06「＋」只限原Team。

原則：**只保留防資料損失、重送與明顯錯誤的最小機制**；**SDK-first／Hosted優先：自建一層前先查官方功能，PR附官方連結並說明缺什麼**（見 [ADR-0006 實作原則](../adr/0006-team-trust-boundary-grokbot-mode.md#實作原則neal-2026-10-01-再次強調所有切片適用)）。權限／回條／費用界線不變。

## 近期完成

| 批次 | 結果 |
|---|---|
| stage第53、54版（11-21） | 第53版：#1292自動壓縮65%、#1293能力上限可設定、#1294（[紀錄](../progress.md#stage-v53-1121)）。第54版：能力上限128（#1299；dev run 4910已驗，[紀錄](../progress.md#stage-v54-1121)）。 |
| stage第50、51版（11-19） | #1281～#1283轉交更快（[50](../progress.md#stage-v50-1119)）；#1286升SDK（[51](../progress.md#stage-v51-1119)）。 |
| 第44～49版 | 舊job lane退役（[48](../progress.md#stage-v48-1118)、[49](../progress.md#stage-v49-1118)）；[44、45](../progress.md#stage-v44-v45-1107)真人回合待Neal。 |

## 下一步

0. **模型用量上限**：stage gpt-6.1-sol每分鐘50萬token（dev／stage共用），長對話會撞上限（[紀錄](../progress.md#stage-v52-1120)）；提高TPM、重試／文案修正待Neal。**轉交**：Neal建B的`web-lookup` Team Skill後重跑轉交測試，通過後skill與R1／R2在stage生效（R1／R2目前只在dev）。`OLDER_RELEASES`的1.0.13／1.0.83待確認（[紀錄](../progress.md#stage-v51-1119)）；`REASONS_DROPPED problem=ids`觀察中。
1. **送出延遲**：剩換版後首次listing、開頁扣2～4秒（[觀察](../progress.md#stage-v35-1022)）。
2. **其他**：azd端到端（`v2026.10.21-34`前10分鐘PASS、#936未重跑，[紀錄](../progress.md#azd-e2e-public-1021)）；私有化（[ADR-0010](../adr/0010-azd-private-network-option.md)／[0011](../adr/0011-private-mode-cosmos-memory.md)，Neal 10-26決定stage改private，[dev演練](../progress.md#stage-private-rehearsal-1026)、[10-28 7/8](../progress.md#startup-private-1028)；未收DNS existing、延遲及格線、費用）；4a一半、6b、5c、開IDLE延後；Teams relay runbook。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 待Neal | 建B的`web-lookup` Team Skill；雲端Table裡未完成／UNKNOWN的舊job數量未核（本機連Table被擋；放行Table主機或Neal自跑查詢，[紀錄](../progress.md#legacy-job-lane-retire-1117)）；E改Responses選版；stage加Teams與relay、Entra redirect-URI租用戶寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，以主人oid為依據。stage A重建session要Neal核准。 |
| 第48～54版未驗證 | #1273、舊對話唯讀、退役路由dev沒測到（[49](../progress.md#stage-v49-1118)）；temperature 0只套gpt-4.1-mini，dev的gpt-5.4-mini未驗到；dev首次轉交被A審查拒絕（`NATIVE_HITL_REVIEW_DENIED`）原因未查（r54再現）；`NATIVE_STREAM_NOT_STARTED`由第49版dev（r52）的3筆變8筆；第51～53版stage沒送訊息；第53版65%門檻雲端未讀回；勾超過12項只在dev讀回上限、沒實際存檔。 |
| WebIQ只留toolbox | dev三步都做完（[步驟](../../deploy/README.md#dev-webiq-env-removal)）；stage沒有這些步驟。 |
| 接手必知（未修） | `native_turns.py`回合尾巴：Hosted存檔後才標completed（manual／diag／委派等回合會先停SDK，上限約15秒，auto不停）；#916已合併，dev沒碰到競態。 |
| 每日routine | #957後10-26起completed；TPM仍dev／stage共用（[根因](../progress.md#stage-v35-1022)）。 |
| 開放項目 | 較早9項（A `turn_status`逾時、B「正在回覆」、Connect卡、2小時橫幅等）見[清單](../progress.md#current-state-trim-1116)；每次發布記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal本人實測「＋」仍待。風險#469，見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [Hosted原生優先](../adr/0007-foundry-hosted-native-first.md)：N0～N3與用量已合，live PASS；#306記憶風險例外不變；#284draft。ADR-0006 T1～T5已部署；T6a／T6b已實作未部署，私人記憶用量未入帳。 |
| 退役Agent、SP v2／v3／role | 退役只有operator CLI（[deploy](../../deploy/README.md#retire-agent)）；SP都不升全域acceptance；dev tombstone待批。細節見[清單](../progress.md#current-state-trim-1116)。 |
| 費用 | dev**停損USD1200**（Neal 11-21從950調高，[紀錄](../fa-cloud-budget-status.md#dev-stop-loss-1200-1121)）；量整個訂用帳戶本月累計，含其他專案；stage月USD200另列。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)。
- [SP v2 規格接點](../../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)。
