# 已審工作記憶：受控唯讀接點與本機驗證

日期：2026-09-25。#30已在新main整合後，固定`efd2a348`同兩Opus5.5軸各0P1/P2，
合併為`cfd9b7d6639adad24776668916a4cc56f18eee69`。**Default-OFF、未部署、PA-13仍partial。**
這不是私人Agent經驗、User偏好、自動記憶提取／發布或Memory Store語意搜尋。

## 合法producer與scope

新server-owned `fa-instruction-work-memory-v1`提供`read_work_memory(claim_id)`。
只可選原`policy.profile.claim_id`一個alias，沿binding/template subject、current purpose與Team visibility。
Tenant／Team／source_actor_id由目前verified principal與binding決定，不從模型輸入或自稱review回條取得。
非Team範圍明確拒絕；沒有cast Actor、借human身份、加grant或假造private正例。

合法資料沿原signed `preview → human review → commit`，再由
`BehaviorRuntime.current_claim/context`、assistant `_reviewed`、`MemoryGovernance`及
`SourceMaterials`核必要原件與回條。這些authority helpers沒有重寫。
General text仍需原`human_text_enabled`／`text_policy_version`，新prompt profile不會自己開啟文字或批准權。

## 給模型的內容與保存界線

| 接點 | 已實作 |
|---|---|
| Initial model | 只有有界metadata；沒有note文字或原件正文。Server本來會讀原件驗權，不能稱server從未讀body |
| Actual read | exact已審note、完整必要UTF-8原件及版本/hash，在同tool_call_id結果進下一request |
| Provenance | 原Session／TurnArchive CAS只加optional有界reference，None省略舊bytes；包含claim version/digest、text SHA/bytes、reviewrecord digest/policy、manifest及source refs |
| Current checks | read前後、restore、reserve、consume、final provider egress重核來源、人審、期限、目前read policy及scope |
| 版本／撤回 | 同文字不同version不當current；原capture沿後續turn固定，來源更正／撤銷不能默換snapshot來恢復舊body |
| 失敗 | unknown/reserved/consumed保留原ID與責任，不refund/replay；原reader本身不加no-SDK terminal白名單。後續[明確v2契約](sdk-prestart-evaluation.md#v2記憶拒絕的限定收尾)只收足證never-started失敗，不保證所有未決工作可reset |
| 語義 | 返回的是knowledge data，不是system指令、操作批准或新權限 |

一般memory更新後的無縫接續尚未實作；目前採保守拒絕，原archive及答覆不被改寫。
這個限制與「同版合法來源可cold restore」是不同能力，不把後者當完整PA-14更新策略。
Manifest也綁subject-wide sequence，同一Team subject新增另一則claim就可能使原capture過期，
不只所選note自身修改才有影響。沒有以新v2終局處理掩蓋這個availability限制。

容量沿24個必要source graph、每source既有8192B、metadata8192B、整個response24576 UTF-8 bytes。
這些是storage／transport界線，不是model capacity或2%token預算；token count未知仍拒，model metadata的tokens為null。
Exact24576／24577 DTO邊界以三個合法大小的原件驗，沒有放大單source上限。

**Catalog階段就組完整`WorkMemoryRead`，包含`profile.source`、其他必要原件、文字與provenance。**
所以即使模型最後不呼叫工具，來源不fit也可能每turn在SDK entry前413、維持unresolved/UNKNOWN。
啟用前須確認完整來源能fit；沒有截斷、auto-refresh/retry或新增fake preflight。

## 四個受測source與20個原批次

| 階段 | 真正受測source | 固定結果 |
|---|---|---|
| 原13批的final | `358f8fc03aa1861119052b9b51eaa277fd19cef3` | 88 API PASS／136.862秒＋1 native PASS／16.885秒，6offline requests／2runtimes |
| Capture補2批的final | `6bc19415c8b2bf1f7d26d7053b19f5918f6c462e` | test-only後2 PASS／6.598秒；沒有native重跑 |
| 診斷補2批的final | `b55b1281ffcdc944369ff52578b1bbcade5d9342` | 8 PASS／13.261秒；產品只有safe primary error分類一行 |
| 新main整合3批的final | `d5abb68600a77b358ddb221890832d0f97b193f9` | 34 callback/API PASS／26.533秒＋1 native PASS／17.622秒，另6offline requests／2runtimes |

各final都是先commit才在乾淨source執行；後來的README／merge heads不被標成執行基準。
原實作的162個product Python在capture補測時不變，test-only6bc之後全382檔另有精確hash；
整合後383個Python工作bytes／Git blobs又逐檔核對，兩種口徑不混用。

原13批保留：初profile1RED、API21P/9F的retired-template/private-policy/public-code fixture錯，
effects17P/2F的reserve-vs-consume／UTF8 canonical hash期待錯，
**read-policy race1P/1F是真實新實作缺口**，後加原gate/policy predicates與finalscope核對修正。
Bound17P/1F原test用了23505B單source，改三個合法原件而未放寬產品。
不同native成功批各6次請求，不加總成獨立品質樣本。

Capture初1P/1F是test錯期待403，原store本來就因完整ControlRequest/readset不符回409
`BEHAVIOR_REQUEST_CONFLICT`，不是新資料洩漏；沒有改store或以新request ID重送。

診斷初3RED的upstream code仍在，但primary是UNCLASSIFIED。現在原safe whitelist加
`WORK_MEMORY_ERRORS`，主碼與upstream一致、私密canary不入journal/log、first failure不被覆寫；
session仍running，無stop/child/terminal。這不是權限、readset或no-SDK契約放寬。

## 新main TOOL-06的真整合反例

Landing preflight發現外部#29已推進main到`d5e875d`，先前0d union不能沿用。
Owner以正式conflict流程在自己的branch普通merge，唯一README衝突保留完整memory段與incoming TOOL-06段；
純merge `3ab886f`與後續產品適配分開，沒有覆蓋新main的全局資料或費用歷史。

新main讓一般instruction工具可回安全schema約束，但原例外只有read_skill。
Memory的alias/schema也含authority界線，unknownalias／scopeextra／nonobject各在permission/invoke兩入口，
合併後先得到**6個RED**：回普通`Invalid tool arguments`，而非已約定的`Authority refused`。

最小適配只將原`tool_failure`的固定server-code例外設為`{read_skill, read_work_memory}`。
模型不能提供旗標自選分類；一般schema feedback、structured／handler／unknown遮罩、foreign-session拒絕
與raw value保護不改。沒有動Memory來源、read-policy、capture409、once／unknown或no-SDK。

新34是原15callbacks＋8memoryschema＋2foreign-session＋9個直接API/authority相容案例；
不是較早parent的virtual-union34。新native使用完全未改的原test，coldresume未知alias仍安全拒，
再合法讀回exactnote/原件；sameCID/SID/rawuser、oldarchive/registration與未批准operation保持。
Actual stops為Windows1／graceful／force0，無native Memory、cloud、paid或compaction事件。

## 採用與第七prefix資格

整合final `efd2a348`只比tested d5abb多README，兩軸各0P1/P2後普通match-head merge，
actual `cfd9b7d`的parents=d5e875d＋efd2a348、tree
`06384b21a5bcddef45e6106301d9beb4b73c3c3e`等同已審preview。沒有admin bypass、force或重寫歷史。

第7個literal prefix為2719B／
`1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3`。
採入時current PA-02／TC-01先partial，不借前六prefix接受。
後續`cb4b180`的[七prefix重驗](prompt-assembly-evaluation.md#第七個prefix與已審記憶重驗)是新的完整baseline；
這不把本reader變成完整PA-13／PA-16或Memory相關性／private偏好已驗。

## 正常工作觀察與知識身分分開

2026-09-26，#39最終整合`69f960d`兩軸修後各0P1/P2，合併為
`d21f7e2c16dec091503aaeea7b28f2257b8c78ee`。此切片只改善合法的same-source讀取可用性，
不增加Memory權利、換版策略、profile／prefix、SDK loop或新的no-SDK終局。

原public反例在完整context組裝後交錯合法`WorkerCore.reconcile`：只有job/query列的觀察計數改變，
binding、claim／review、manifest及原件完全相同，舊catalog仍409 `ASSISTANT_SOURCE_CHANGED`。
產品四行只從**知識身分的精確快照比較**排除job row；不是從授權或交易讀集拿掉它。
讀前／讀後的`_scope → authorize/_gate`仍核目前job，`_command`仍在原完整read_set內核job和所有gate，
最後以guard／ETag／CAS提交。其他policy/budget/registration/delegation/session/obligation/pool及
claim/source/review精確條件保持。

因此正常觀察可以繼續原回合；真正cancel、owner/epoch/execution、撤權、期限、same-text新版
與來源更正仍拒。Capture與舊archive不刷新，subject-wide sequence限制仍在，model已consume的
原request／費用／intent不會被清除或重播。

| 原7批 | 結果／來源 |
|---|---|
| Catalog反例 | 1 RED／11.813秒，WIP；是純job觀察造成的真liveness失敗 |
| Scoped controls | WIP分別3／5／11／11 PASS，來源不改標未來commit |
| 固定API/journal/contracts | `b0b6e835f5a185510c8cab487d8bbfba913baaee`先commit後42 PASS／114.970秒（15新scope＋3generic no-SDK＋24既有） |
| 固定SDK控制 | 同b0b6下1 PASS／24.775秒，2runtimes／6offline requests，工具exactnote/source及cold route保持 |

真SDK仍配離線provider及本機`offline_contract`業務模式；兩個rawstop為Windows1／graceful／force0。
其他Local／合成Hosted案例的SDK I/O與stop替身不當native／Azure證據。

### 兩項review修正另存，不改原件

初稿7b28的Standards為0P1/2P2、Spec為0P1/0P2；不是產品授權放寬，
而是證據標記與模式宣稱不精確：

- 兩個negative案例其實有**job觀察後撤銷text policy**，舊JSON檔名／說明只提observer。
  新檔`local/hosted-query-update-text-policy-revocation.json`明列兩步mutation，
  policy enabled與version-presence的True→False皆實際讀回/assert；
  知識相同比對時點明示在job更新後、policy撤銷前。純observer正例只記job變化、policytransition為null、回200。
- Catalog `work-memory` capture不走`_ledger_conflicts`，實測offline_contract的晚guard競爭是單次412。
  Production `mode="native"`且非Teams的model/model-send則沿既有receipt-first、已確認412且無receipt才
  同ControlRequest最多共3次attempt；unknown不授權新dispatch。**該native業務模式競爭NOT_RUN**，
  不能用真SDK離線控制冒充它，亦不是本slice新增retry。

Test/docs source `4d7be778f6f8c64c194f754212ae19839f525a1e`先commit後另跑4 PASS／13.225秒，
含兩個policy拒絕、generic wire及pure-observer正例；沒有SDK constructor或model request。
原7批與舊13＋5原件未改名／覆寫，純observer的初始RED仍是真缺口，不能被錯解成policy撤銷案例。

最後69f只普通merge已審main4ee，product/controller對487修後head不變；
391個Python等4d7，另兩個是已審parent Unicode tests。不聲稱在merge／preview重跑4、42、native1或8/22。
最終兩軸各0P1/P2後才落地，原兩P2均解除；整個target仍partial，不保證無限抗衝突或full Memory品質。

## Verified against

- 已審source重用`behavior_work_memory_source._scope/load/capture`、Runtime與MemoryGov原權威，
  `_Session/TurnArchive`與restore/model-source/egress消費者、Hosted custom tool及strict prompt-record union。
  原兩Opus5.5分別核initial、diagnostics與new-main integrated fixed heads；各0P1/P2，非同一clear反覆套用。
- Original handoff `951ecf4e3dbbffbafee0a7f53e1a01b5165d8cb8d419c8f6832d70fee9243548`；
  diagnostic `ae60edb3206009ca9542c6ea9f4a81edebe672ad4e1e9f5bd087fe67162bafff`；
  integration `1231a1624cd79af721a80228a10333d275f27b1c34c9e52bafdeeb13109035b5`。
- 四批index SHA：13-batch `4cdd81058dde136ae050e3f1ff0ecbb735c8a7e0171d295ba1f60d6b1b4a34de`、
  capture `f031870294986c1fa3276c81356dd882225082d5d26fd743445ed143d4e85f74`、
  diagnostics `e71a501c9501437fdf82eee452cb04183fdb1ad0b7565ba90b37fa9f8a249b03`、
  integration `322d8f59f18a353ac5e262b530b952a6c947c0b1cede8817f7ff46f308063917`。
- 整合34／native XML：`f5ad522edbb8dec8c967e5a85a06158fecc28a1c918ef7fd501813667c5ea91a`／
  `c163d5cf4e4f782f9d66aa81a0ce74edb7d22583ebc6847568a26c0470c83169`；
  safe native proof `6fb2e6915a453ce2cea333df9856179dfbfba5f2517edc8742d892b78a407751`。
- Incoming#29的另一套runtime目標／baseline及成本窗口原文保留，Grok285前綴未變；不同維度不合算
  為一個完成率。可靠USD150 headroom仍UNKNOWN，本scope沒有新增cloud/paid/IAM或部署。
- Normal-observer切片核對`behavior_work_memory_source.load`、`AssistantAuthority._gate/_command`與原store guard/CAS；
  b0b6→7b28只README，P2修補只tests／直接文件，4d7→487亦只README，69f無手工Git整合。
  原7批handoff／index為`89ad48bd0aae5eb29ad8272661c41dadc7ff0e7977591e0afd1b5f498515d34e`／
  `e58f5c532e99f0555eb389f85d920bf8321f178ae37afd91012e398cbd320fea`。
  新4-case XML為`fe038be902b3e302c023c3a03f69cb6370e32e0792b9cba92da880354ce5b2a0`，
  pure-Git回條`697874b0eb507698393b6ff674fc1485da0682d905207b933f67e7166fe36f1b`；固定head兩軸回報與新舊原件分開保留。
