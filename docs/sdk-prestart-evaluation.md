# SDK啟動前拒絕：可信未啟動終局與驗證

日期：2026-09-25。**本機有界實作已合併、default-OFF、尚未部署。** v1保持已確認acquire後的skill來源／ACL拒絕；新增明確opt-in v2只涵蓋足證的記憶拒絕。兩者都不是所有啟動失敗、跨VM恢復或Azure stop的解答，不能由沒有PID／模型請求猜終局。

## 原診斷：BLOCKED，不回填

固定產品`578d8805db4da429f7c604b204bfd34b3d4f53c2`上，來源切版或Actor讀權改變，分別在restore／catalog取得前被拒。四個反例都未進SDK constructor或model，但Local工作停在`WORKER_START_UNKNOWN`／starting，沒有可合法保存的stop或本輪archive。

真正Hosted supervisor／journal配合signed claim、fresh probe及report的**合成transport**，即使原thread已join，仍只能報spawn_unknown／capacity_uncertain。已claim後不能用`fence_unclaimed`，會明確回`NATIVE_CLAIM_ALREADY_OWNED`；舊sealed DTO與result consumer要求真child exit／SdkStop。phase已是sdk_run也可能尚在讀catalog，不能用phase判定開始或停止。

| 原診斷批次 | 結果 | JUnit秒 |
|---|---|---:|
| Initial desired-outcome probe | 1 failed | 5.691 |
| 四種拒絕＋兩個support controls | 4 failed／2 passed | 15.072 |
| 既有thread／import／transport／authority | 9 passed | 14.515 |
| 既有unclaimed／late-claim／ACK-loss | 4 passed | 2.693 |
| 移到session artifact的portable重現 | 1 failed／1 support passed | 11.128 |

Support PASS證明保守拒絕及測項真的到達SDK入口，**不是正向終結已GREEN**。無變更control在入口刻意sentinel停止，沒有啟runtime。原診斷沒有產品變更；handoff SHA256 `0c00f36fa9b5e34571a85b45bc05c9fb9dd8237692b2fbb17e6eeb8d74dab90c`、portable repro `b33f87ac7e68ea8660c8178ceb0db8d0105bf50ee00adbde42465b8cd49eaa02`保持。

## 必要的新契約

依[BC-01／02／03契約](FA-AGENT-BEHAVIOR-SPEC.md)，server profile明確opt-in `sdk_start_profile="fa-sdk-start-gate-v1"`；缺省OFF，舊DTO與records不增加欄位。

| 接點 | 已實作的必要條件 |
|---|---|
| 真SDK entry | 原LocalJournal的`open→entry_attempted`與`open→closed_before_entry`在同一`BEGIN IMMEDIATE`互斥；持久intent早於CopilotClient／RuntimeConnection構造 |
| Positive observer | 原owned thread確實joined、close回條確認、同job/task/profile/execution／config/code／generation，才形成`fa-sdk-no-start-v1` |
| 不明仍不明 | reservation／entry／close／observer寫入或ACK不明、活thread、非原owner、曾嘗試但沒有PID，均不能成為never-started |
| Native consumer | 新`sdk_not_started`不同於舊sealed-child-exit；保留signed claim、process capability、fresh probe、owner／epoch，在原CAS重核server opt-in及無model intent／child／effect／新SDK state／取消 |
| 保存與交付 | 原CAS保存failed result、`fa-no-sdk-turn-v1`及native evidence，再走既有result-forwarding與failure pull；不造SdkStop、PID、exit code或TurnArchive |
| 後續動作 | 保留原user/task、first failure、費用／unknown及舊archive／答案；沒有本輪SDK context就回`SDK_NOT_STARTED_RESET_REQUIRED`，只提供使用者明確reset，不偷偷跳過原話或換session |

這重用原execution owner、journal、observer和結果接點，沒有第二個agent loop或通用recovery engine。仍信任原受控executor／journal；installed source hash不是runtime attestation，不抵抗已被攻陷的可信producer。

## 新實作的各批結果

新head `be002dfe116a71fcc8f2705745745204dfffbb84`，tree `541a098160ac3d8e5946bc4a5a1642ace2153c8a`。下面全是base `578d880`加**pre-commit WIP**，baseline均null。361個Python工作檔hash及Git blob提交後相同，只證檔案一致，不冒稱在未來commit跑過。

| 開始（台北9/25） | 批次 | 結果 |
|---|---|---|
| 03:13 | Server selector | 1 RED：舊DTO拒絕新opt-in |
| 03:14 | Controller接點 | 1 RED：仍WORKER_START_UNKNOWN |
| 03:24 | Local consumer | 1 GREEN |
| 03:28 | Hosted report consumer | 1 RED：仍capacity_uncertain |
| 03:30 | 兩種producer／consumer | 2 GREEN |
| 03:32 | Race／lost-write／legacy／cold entry | 11 GREEN |
| 03:37 | Scope／schema／start／cancel／replay | 29 GREEN |
| 03:42 | Failure pull | 33 passed／2 failed；test誤把outer envelope當inner notice，產品傳輸未改 |
| 03:42 | Native control初次 | 1 failed；test錯把model-facing trim當SDK原文；provider entry count未保存，保持null |
| 03:45 | Failure envelope斷言補正 | 2 GREEN |
| 03:45 | Native control斷言補正 | 1 GREEN／1 offline provider request |
| 04:02 | 初始journal／report ACK-loss及舊decoder | 6 GREEN |
| 04:15 | 最後targeted | 99 passed／0 failed／0 errors／0 skipped，119.013秒 |
| 04:15 | 最後native control | 1 passed／1 offline request，11.528秒 |

初次native失敗的log只證reserve／consume各一次，不能補成provider entry為0或1。後來的native control真的在constructor當下核到持久intent，然後正常SDK send／stop：Windows exit_code=1、graceful=true、force-stop=0，**沒有**建立no-SDK disposition。SDK user event原文與model-facing trim分開，沒有改原文規則。

Local／Hosted合成拒絕正例各0 constructor／0 model；failure pull只是原work的payload，不是ACK或真人已讀。Hosted的首份archive由原API保存合成state，不能冒稱真Hosted或模型品質。每批重疊不能相加成獨立樣本；唯一warning為既有Starlette alias deprecation。

## Review、合併與獨立整合

同兩位`claude-opus-5.5`對固定`be002df`的Standards／Spec各 **0 P1／0 P2**。Binding推導可共享及error-code列表一致性屬非阻擋維護提醒，未為風格更動已審head。

#18經ordinary match-head merge合併為`d03aba2cd015013211fad675f29641134e76f139`，parents為`ad3ff0b`＋`be002df`，沒有admin bypass、force或rebase。實際tree `a313832bf5823bfba7ceeeb61edff7b153c6d925`與合併前snapshot一致。

該snapshot另有13個focused integration PASS（48.84秒），包括四個native cases、Local／Hosted refusal、gate race、report ACK-loss、舊bytes及API／Hosted cold imports。Native合計10個offline requests（prefix三案9次＋正常entry control1次）。這不是重跑owner99-case套件，亦不包含協調者當時未發布的structured-prefix test。

## 限制與評測歸屬

原診斷、14個Implementation批次、兩軸review和整合snapshot分開追加帳冊；不覆寫原BLOCKED或RED。SP-09只列partial，因為尚未涵蓋一般長工作恢復、pre-acquire收尾、跨VM crash/corruption及真Azure環境。

未部署、未build services、未做cloud stop／paid inference／IAM／keys／live-data／harness或schedule變更；USD150可靠headroom仍UNKNOWN。新能力只在已ack acquire、第一代、已知skill拒絕且沒有其他未決活動的限定範圍生效。

## v2記憶拒絕的限定收尾

#33整合final `9c276cdd08f03c31baecaced23c38894836d9508`同兩Opus5.5軸各0P1/P2後，
普通match-head merge為`ddbcd64e222daa53024e5cfa58eebf9a438e9b38`。
新server-owned/default-OFF `fa-sdk-start-gate-v2`只能搭配`fa-instruction-work-memory-v1`；
不是把skills-only v1或None的舊回條默升成新批准。

Binding／gate record／disposition／NoSdkTurn／result proof／local evidence同版本綁定，
missing／unknown／mixed版本拒絕。v1／None的wire bytes、原memory拒絕UNKNOWN與可觀測診斷保持。
原SQL entry互斥、已ack acquire、owned thread已join、claim／supervisor／generation／epoch／
task/profile/code/config／fresh probe及目前report/CAS權限，仍全是必要條件。

| 可否收尾 | v2界線 |
|---|---|
| 明確memory/source/review/期限/scope/textpolicy/完整來源上限拒絕 | 只有原入口已關且所有強證明同時成立，才原CAS failed/noSDK結果與failure查收 |
| SDK entry已嘗試，即使constructor錯且無PID | 不得轉closed，不造disposition |
| SDK後才發生工具拒絕 | 不是pre-entry，不走此終局 |
| ACK／材料／token／完整性／capture／claim不明 | UNKNOWN保留，不換ID或推定沒有副作用 |
| 仍有intent、child、effect、SDK state/archive或owner/probe不符 | 拒絕收尾，保留原工作 |
| 目前policy已不允許report | 即使gate關閉也仍blocked |
| 原report ACK遺失 | 只核實際持久的原sealed receipt，不以late新proof改寫 |

Capture metadata只是operational provenance，不是SDK state，也不是有／無終局證明。
沒有本輪archive時仍沿原`SDK_NOT_STARTED_RESET_REQUIRED`與使用者explicit reset，不fake SdkStop、
PID、exit或TurnArchive，不自動同SID續行、reset、refund或重送。

### 原13批：版本與強證明

Source/tests `2564fdebe1a9f37de125a405b16487868b77cbd4`於20:37:14先commit，
20:39:27在乾淨source跑 **89 API/journal/wire/regression PASS**（65＋10＋14、238.465秒），
另 **1 native normal-entry control PASS**（9.991秒、1offline request）。
交付60300a6只多README，386個Python bytes／Git blobs相同，不冒稱在docshead測過。

原13批保留v1基準1RED、WIP v1diagnosticspill真正2P/2F與version-conditioned修正、
不同fixture期待／授權／import錯誤和後續GREEN。Local與合成Hosted正例的0SDK/0model只是補充，
另有真signed人審producer／journal／gate／claim/probe/report/CAS證據；先前archive與retained-effect
負例注入明列synthetic，不叫Azurestop或真外部操作。

Native normal control在constructor前讀回durable entry intent，正常SDK user event原文保持，
model-facing trim另列；實際Windows1／graceful／force0，沒有noSDK disposition。

### Generic衝突不能成為memory終局

初審兩軸各0P1/P2；Standards另提Low：`ASSISTANT_SOURCE_CHANGED`是aggregate snapshot不一致，
可能只因job/query等正常列更新，不一定是知識失效。後續已不只靠source推論：
真public Runtime.context交錯合法`WorkerCore.reconcile`，exact claim／review／manifest／originals
完全相同，wire／Local／Hosted共三個RED重現原v2錯誤failed／forwarded。

最小產品修補只從v2終局集合移除此碼。相同test bytes修後留下open gate、
Local starting／Hosted capacity_uncertain，無noSDK turn/result/reset；
primary／upstream診斷仍在。沒有鬆readset/predicates、改observer節奏或宣稱production頻率。

上段是#33當時的保守修補結果，不回填。後續#39另修[純job觀察的讀取可用性](reviewed-work-memory-evaluation.md#正常工作觀察與知識身分分開)：
job不再是knowledge identity，但仍在current `_gate`及atomic command讀集；真正文字policy撤銷或其他
aggregate衝突仍拒，generic code的v2終局禁止未變。新案例與模式限定另存，不把原失敗改成成功。

這仍是保守失敗，沒有新增一般競爭的自動重試。**Team subject-wide sequence也是manifest的一部分**：
同subject新增另一則claim亦會改manifest SHA，未必只有所選note本身更動。
這種已確認的記憶版本變更仍可能在後續pre-entry觸發memory-stale；v2只在足額proof時合法failed，
需要explicit reset，不宣稱已做無縫memory transition或擦除舊history。

### 兩次owner-owned主線整合與新5批

第一次純merge `53811f2`的parents=60300a6＋公開progress主線0dac615，只有README conflict，
保留雙線原文及automatic歷史校正。assistant_wire的progress optional欄位與v2驗證不重疊，
新source/tests `10aa245ac6e6b76e7ab2e7c698aefa6153dad0c4`再獨立提交保守修補。

該source於21:44:48先commit，exact **33API/contract PASS＋2短native PASS＋1targetedDOM PASS**。
33為57.368秒，2native為15.740秒、3個offline requests；只v2正常entry與既有pair shortcase，
DOM不是browser。新5批是puremerge4 WIPpass、generic3RED與exact33/2/1，原89／193大矩陣不重跑。

Beb4只多README；最新9c再普通merge metadata-only main3f，沒有手動檔案改動，
src/tests/controller與beb4相同，389Python及2DOM來源等10aa。其README較早「未採3f」描述的是
beb4交付時狀態，不能誤讀為最終9c沒有整合；沒有在9c或preview重跑。

最終兩軸重新核9c／tree`422397d67d71d75a2f19f3ca755790f9d4cf322f`，各0P1/P2才合併。
Actual ddbcd64 parents=3f7391d＋9c276cd，tree同reviewed preview；
peer337帳冊、TC-01在0dac的已接受baseline及其他runtime成果保持。
TC-12只列partial，不因有限noSDK結果而接受全部失敗／重試／取消路徑。
它不是TC-08的provider-specific summarization；該壓縮目標維持原unassessed，沒有本切片證據。

## Verified against

- 兩軸pinned source核對：`behavior_sdk_start`、`m2_copilot.run_copilot`的effect point、`AssistantSupervisor` observer、`behavior_instruction_no_sdk`、native report、work-policy projection/reset、history與migration consumers。API／Hosted dependency closure及舊serializer保留。
- Handoff／batch index SHA256：`f8988adf439cd118ef1e17881115c7822e13efb8d1d0f707290157c6af8de722`／`4bde315d38dfa58ebc96e6a97335bf1e59cb4d0afb415350b126c423b981e896`；source manifest `58a6b73ec62e4b430eaf408b259ed9c0225f9512b0b22fe7cbf6f753320c925f`。
- Final99／native JUnit：`dc50ed84f17453d9e0aef84ba2e11ebe75fecf99a741971f4b5b21bd147ee448`／`df3f6a8581f56aabbb03a08adf1a4867336ade58637c963184788ad3ce0d9d72`。
- Integration ZIP／JUnit：`5fd76544f18e140cb008f41f6185a2a97e0faf5c089ae03166b5da6834bbe4fe`／`f4a12ff7e026021f206cdbdccd559b71b66f3962d15a7fbdf19cd10cb0efa549`。只清理task-owned解壓目錄，原ZIP／XML／safe proofs留存。
- 沒有公開raw SDK state、provider wire、private system/reasoning、journal DB或credentials；工作檔CRLF hash、Git blob OID與snapshot hash不混用。
- V2原handoff/index為`2d87efa369283042b11b3ac8545e551ecb3fbc6087be6c3b7fc00b64ca7b28e5`／
  `3958d914aab521bae36636232cbc3691ab7b0cc7e5b934243726e604191abae6`。
  原89／native XML為`4e52ce2122c14249789555d37d546f9e6ebc70b98f86dca6011506aad9eb2348`／
  `0fcbd12413f7e8ab3a5396a527e5108938840681cf167a8a46782b081d9ce30b`。
- 新main／Low修後handoff/index為`98f52ab888bb246ba6a811f8dd43060f7f39f40f707cb2d7ec1c9661d5db5d8e`／
  `226d6d5ae41e34c0c02b6260d0fb77cd42ac9588f47452d3c04c8214290201bf`。
  33／2 XML為`ff6f9b27a747589251701e544679c3e000711ecff06f3ddeae16e9f963f92201`／
  `d56c4f2d342839a60d8e1c206931387710af9ac1cbc598a45991cc38695f02d6`，targeted DOM保留原log bytes而非正規化文字hash。
- 最新pure-Git回條`757cc6ad0401a660a7fbd229a16ae63afc3c8caa72af2333153175e225a80a32`明示沒有新執行。
  本次只分批匯入原13＋5、各次review及既有57-case ledger tooling回歸；不改原337或其他工作線的TC-01接受。
- PR36初稿曾誤掛TC-08，Spec review指出P2後，在合併前重新生成22個新增run的TC-12綁定與內容hash，
  保留原337筆、既有TC-12證據、原始XML／回條與被否決的draft commit。直接README／契約只更正target引用，產品未改。
