# CURRENT-STATE — FA 開工摘要

> **非權威快照**；先讀 AGENTS 與本檔，再按需讀來源。[維護規範](../CONVENTIONS.md#8-current-state)：60行／UTF-8 8KiB內。
> 2026-11-27（+08）；核對origin/main `5b77ff9d`；stage是第59版（2026.11.24-59，[紀錄](../progress.md#stage-v59-1124)）：只改工作台，文字框空白但有附檔可送出，可退回第58版。**第58版起存超過4個skill（或模板超8192 bytes、catalog超4096 bytes）後不能退回第57版前的image**（Neal 11-23同意）；56版起刪除後API不能退回56版前。API只送v3、閘門武裝，GUARD="1"（不能關、不退版）。
> 本文不是操作批准；原資料、回條、unknown與費用上限以來源為準。

## 現在在哪

**優先順序（Neal 10-01 21:33）：**「ADR-0006=ADR-0007=多Agent協同合作 > VM桌面 ~=B1 B2 target state」。native真人批准卡片（SP-20、PA-11、E-08）提到最高層（10-02 16:38）。

**環境（[ADR-0008](../adr/0008-environment-tiers-staging-cicd.md)）：** dev＝`faexample`；stage月USD200獨立[預算](../fa-cloud-budget-status.md)，改`deploy/stage/release.json`或手動才部署，**每次都需Neal核准，release PR流程依AGENTS**。無branch protection，合併靠流程。

**進行中：** 舊job lane已退役，舊client方法與讀舊列模型刻意留。相容閘門（#934）stage與dev都武裝；IDLE、RECONCILER仍"0"，快照URL未設。**[ADR-0009註記](../adr/0009-version-model-g-actual-version-idle-upgrade.md#platform-in-place-resume-2026-10-20)：平台原地接續是主路徑，FA換版（`cut`）是備援。** dev走`ci_deploy`（#919，`deploy/dev/`不匯出公開repo）。

**GitHub Actions：** private、free方案（2000分鐘／月），預算USD10；11-24第59版deploy與public-sync成功；public-sync逾時60分鐘（#1285）；重型套件不跑PR／push。

**沿用設定（[第23版](../progress.md#stage-v23-1008)）：** 排程開（API時鐘＋對話提案）、打字預熱開、stage閒置逾時3600；HITL A／B／t4、對話中換模型開；Teams connector第1波（Work IQ）開；Teams逾時補送（#827）仍關，需Neal。

**ADR-0006（#267）：** 信任邊界見AGENTS。FR-09（#432）：無全域同時上限；同Agent依序、不同Agent並行。

**Neal 10-03「都同意」（[ADR-0007 §9 ⑤](../adr/0007-foundry-hosted-native-first.md#9-決定狀態)）：** 探針由總協調判讀即算數；改程式只留R1（覆寫／刪除／丟棄未提交改動前先問）；CAP-06「＋」限原Team。

## 近期完成

| 批次 | 結果 |
|---|---|
| [ADR-0014](../adr/0014-team-toolbox-skills.md) | dev phase 3通過；留測試skill與team-one toolbox第7版。 |
| 第44～59版 | 轉交快（[50](../progress.md#stage-v50-1119)）、升SDK（[51](../progress.md#stage-v51-1119)）、壓縮65%（[53](../progress.md#stage-v53-1121)）、能力上限128（[54](../progress.md#stage-v54-1121)）、貼上長文字轉附件（[55](../progress.md#stage-v55-1122)）、刪Team skill與排程（[56](../progress.md#stage-v56-1122)）、系統讀得懂32個skill（[57](../progress.md#stage-v57-1123)）、skill上限32（[58](../progress.md#stage-v58-1123)）、只附檔可送出（[59](../progress.md#stage-v59-1124)）；[44、45](../progress.md#stage-v44-v45-1107)真人回合待Neal。 |

## 下一步

1. **skill上限32實測**：Neal在stage實測選5個以上skill（dev、stage都沒實測過）；dev B有一筆Teams同意卡待連線請求，Neal在工作台處理。
2. **模型用量上限**：gpt-6.1-sol每分鐘50萬token（dev／stage共用），長對話會撞（[紀錄](../progress.md#stage-v52-1120)）；提高TPM、重試／文案待Neal。**轉交**：B的`web-lookup` skill建好後重跑轉交測試，通過後skill與R1／R2才在stage生效（現只在dev）。`OLDER_RELEASES`的1.0.13／1.0.83待確認（[51](../progress.md#stage-v51-1119)）；`REASONS_DROPPED problem=ids`觀察中。
3. **其他**：送出延遲（換版後首次listing、開頁扣2～4秒，[觀察](../progress.md#stage-v35-1022)）；azd端到端（`v2026.10.21-34`前10分鐘PASS、#936未重跑，[紀錄](../progress.md#azd-e2e-public-1021)）；私有化（[ADR-0010](../adr/0010-azd-private-network-option.md)／[0011](../adr/0011-private-mode-cosmos-memory.md)，stage改private已決（10-26），[演練](../progress.md#stage-private-rehearsal-1026)、[10-28](../progress.md#startup-private-1028)；未收DNS existing、延遲、費用）；4a一半、6b、5c、IDLE延後；Teams relay runbook。

## 待核對但不作繁瑣部署gate

| 項目 | 界線／來源 |
|---|---|
| 待Neal | 建B的`web-lookup` Team Skill；Table內未完成／UNKNOWN舊job數未核（本機連不到，要放行主機或Neal自查，[紀錄](../progress.md#legacy-job-lane-retire-1117)）；E改Responses選版；stage加Teams與relay、Entra redirect-URI寫入、Teams登入、PG稿`5055d28c`送否。Teams批准只收allow_once／deny，依主人oid。stage A重建session要Neal核准。 |
| 第48～55版未驗證 | #1273、舊對話唯讀、退役路由dev沒測；temperature 0只套gpt-4.1-mini，gpt-5.4-mini未驗；dev首次轉交被A拒（`NATIVE_HITL_REVIEW_DENIED`）原因未查；`NATIVE_STREAM_NOT_STARTED`由r52的3筆變8筆；51～53版stage沒送訊息；65%門檻雲端未讀回；勾超過12項只讀回上限、沒測存檔；貼上轉附件stage沒實測（dev已用真Ctrl+V測）。 |
| 第59版未驗證 | 多附檔、圖片／PDF、附檔加文字混合、送出失敗後文字框狀態都沒測；只附檔首次送出不預熱VM、不預取下一則request ID（只影響速度）；stage沒實測；stage有無發布新Agent版本沒讀回（推論有，程式相同；未讀job log）。 |
| 第56版未驗證 | stage沒實操刪除與名稱顯示、v56沒跑過回合；先停用再刪skill、實刪排程只有本機測；新增助理未完成時刪它勾的skill會卡住（不可挽回，待修）；助理讀的catalog標題仍固定英文（Hosted草稿工具缺可省略name，未做）；刪skill後「已保存草稿…」提示沒清；刪排程後，等批准的那次執行不在提醒清單（對話裡仍可批准）。 |
| 第57～58版未驗證 | stage沒回合在v58跑過、幾個Agent還停在v57前image未查；存5個以上skill在dev、stage都沒實測（存檔、模板、目錄只在本機測）；選項全列、目錄保名、assembly讀回、頁面顯示「32」沒走到；舊image失敗路徑只讀原始碼；32個skill時一份模板最多約50項能力；選項清單任一skill讀取回403整份失敗；dev B回合被Teams卡擋沒驗到（細節見[紀錄](../progress.md#stage-v58-1123)）。 |
| WebIQ只留toolbox | dev三步都做完（[步驟](../../deploy/README.md#dev-webiq-env-removal)）；stage沒有這些步驟。 |
| 接手必知（未修） | `native_turns.py`回合尾巴：Hosted存檔後才標completed（manual／diag／委派等回合先停SDK，上限約15秒，auto不停）；#916已合，dev沒碰到競態。 |
| 開放項目 | 較早9項見[清單](../progress.md#current-state-trim-1116)；發布記send RTT p50／p95。 |
| stage「＋」IAM | #468：Neal實測「＋」仍待；風險#469見[ADR-0008 §6](../adr/0008-environment-tiers-staging-cicd.md)。 |
| ADR-0007／0006 | [0007](../adr/0007-foundry-hosted-native-first.md) N0～N3已合、live PASS；#306記憶風險例外不變；#284draft。0006 T1～T5已部署；T6a／T6b未部署，私人記憶用量未入帳。 |
| 退役Agent、SP | 退役只有operator CLI（[deploy](../../deploy/README.md#retire-agent)）；SP不升全域acceptance；dev tombstone待批（[清單](../progress.md#current-state-trim-1116)）。 |
| 費用 | dev**停損USD1500**（[11-26](../fa-cloud-budget-status.md#dev-stop-loss-1500-1126)）；量整個訂用帳戶本月累計。 |

## 來源入口

- [費用](../fa-cloud-budget-status.md)、[帳本](../context-evaluation-status.json)、[公開repo紀錄](../public-sync.md#公開紀錄)。
