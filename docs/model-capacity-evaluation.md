# 選定模型容量：宣告快照、SDK配置與未證的限制

日期：2026-09-25。#22已合併，**default-OFF，只完成容量事實與configured overrides傳遞**。Runtime effective limits、provider hard limit、長history切小模型的安全preflight仍未證明；TC-04只列partial。

## 已接線的範圍

Server-owned `model_capacity_profile="fa-model-capacity-v1"`讓選定deployment的已知server-curated facts成為不可變`fa-model-capacity-snapshot-v1`，沿原InstructionTask、WorkRegistration／profile digest及wire進入Hosted的公開SDK create／resume。

- Context、明示input與output分別映射正式`ModelLimitsOverride`，不從context減output推算input。
- Unknown不填0／infinite，不把舊structured的32768／1024套回自由instruction。數字必須是合法positive integer；不強行假設獨立maxima一定相加等於total。
- Snapshot綁model、source、正規化數字與digest，不是原file bytes、vendor live事實或CLI attestation。
- Current loaded selected-facts於issue、acquire、restore、reserve、consume及最後egress漂移會拒絕原工作，不改其Task、archive或重送原request。原file load/cache不是hot watcher。
- 薄client只讀typed declared metadata；user/model body不能自訂caps、limit、snapshot或profile。

## 三層數字不能混用

| 層 | 實際結果 |
|---|---|
| Server宣告total/input/output | 64000/60000/2048 → 16000/14000/512 → null/null/768 |
| 公開SDK create/resume RPC | 各model及known numeric值依高階serializer送出，`modelCapabilities.limits`的鍵為camelCase；unknown不帶入前一模型numeric配置。這只證傳輸，不證runtime接受該limits形狀或值 |
| 公開runtime attribution | 六次不帶caller limit的觀察都是prompt/display context 128000，不符合前兩個declared input |
| OpenAI-style provider wire | 三個request均無`max_tokens`／`max_completion_tokens` |

固定SDK1.0.13官方E2E亦明記該wire不echo output配置。這不能證明容量已enforced，也不能用`context_info`傳入caller值後再當觀察回條。沒有改SDK／私有RPC、在gateway補hardlimit、重造tokenizer或用paid超限重試；另一個public model-switch preflight surface尚未採用。

## 原12批與精確基準

產品base `a445310`。Source/tests先提交`ac5354020a03a6f1f7c37898a026dce7764812b6`，10:26:33在乾淨該commit跑最後75/API及1/native；交審`88d7464`只加README。

| 原批次 | 結果與意義 |
|---|---|
| API／Task／Hosted initial | 1 RED，knownfacts沒有送到SDK options |
| 初版接線 | 3 passed |
| 兩個native strong assertions | 各1 failed；第一個另有test observer重複查已disconnectsession問題，缺完整count／stop原件不補零 |
| Native配置／觀察分列 | 1 passed／3 offline requests，不改128000與wire absent原事實 |
| API first | 42 passed／4 failed；fake second-model expectation及safe error mapping分別修 |
| API corrected／consumer | 54 passed／10 passed，兩批 |
| Precommit suite | 74 passed／1 failed，既有private config-new.json缺件 |
| Exact base archive重現 | 同selector1 failed／FileNotFoundError，未讀或建立私人config |
| Exact ac535 API | 75 passed／73.057秒；換public serializer selector，不回填上一cohort全綠 |
| Exact ac535 native | 1 passed／13.517秒，3 requests／3 fresh runtimes |

原三個native runtime正常stop，Windows exit_code=1、graceful=true、force-stop=0；同CID/SID、原user events及舊Task/archive保持。這是原版本的實測，不改標成之後修補版本跑過。

## 同根因P2與最小修復

初稿`88d7464`經Opus5.5兩軸各0 P1／1 P2，**同一root不算兩個bug**：capacity opt-in誤套structured `AssistantSubmit`，接單後在issue拒絕，留下`WORKER_START_UNKNOWN`。

修正只在`current_model_capacity()`最前對`task.instruction is None`回None。Structured維持原fixed model／limits／prefix／tools／save-finish；不造snapshot或把unknown改成terminal，instruction的其餘檢查不變。

| 修後獨立批次 | 結果 |
|---|---|
| 精確RED | 1 failed／4.776秒 |
| WIP最小GREEN | 2 passed／9.374秒，包含3個offline structured requests |
| Exact `eb5b89a` API | 17 passed／21.587秒 |
| Exact `eb5b89a`新structured native | 1 passed／8.883秒，3 requests／1 runtime |

`eb5b89a543d628cc6d72fa6b48e4459c34f959b8`於11:07:05先commit，兩final批次11:07:27才開始；final `b56ecb9`只多README，372個工作檔hash／Git blob與實測source相同。

正例真的從signed AssistantSubmit到controller、SDK `read_context→save_result→finish_task`、M3 3/2/1及delivery pull。仍是gpt-4.1-mini、32768/1024/33792、264B prefix、原四tools；原Task／registration不變，pull不是human ACK。

Restore負例另驗acquire後把loaded output2048改4096，原client.restore回403 `MODEL_CAPACITY_CHANGED`，0 model、沒有新archive，原assignment／registration／先前archive及running unresolved保持；沒有擴no-SDK終局或fake stop。

## 合併、整合及後續

同兩個Opus5.5軸對`b56ecb95df6bb750d53c2f697c7af8e94d731dcf`各0 P1／0 P2，原P2解除。#22普通match-head merge為`1b7f4d9b76005a614ceba6b13743bd2034b0203d`，parents為`7076ce5`＋`b56ecb9`。

實際tree `21291b5c9a5922b447f871cc9231022471d96285`與合併前14-case snapshot一致。該獨立整合含capacity兩native、reader checkpoint prefix、restore／unknown、bridge、舊bytes及cold imports，不挪用owner17／75數字，也不宣稱capacity enforcement。

後續獨立診斷已完成，下節保留正反證據；**產品採用仍BLOCKED**。沒有新的產品patch、commit或PR，也沒有改既有configured-only結果或TC-04 partial。

## 固定SDK的公開介面診斷與採用阻塞

診斷產品base／final都為`1b7f4d9b76005a614ceba6b13743bd2034b0203d`，372個tracked Python檔案未變。
測試本身是session內的凍結實驗檔，不在該Git commit，所以各批diagnostic baseline均為null，
另保留逐批test source hash；不能把產品base當成精確測試commit。

### 確認的serializer差異，不是已證明的唯一根因

固定SDK `1.0.13`／官方commit `f13e4a2cc7e4e220974d2333142234e162a3252e`：

| 公開接點 | 直接讀到／觀察到的結果 |
|---|---|
| [高階ModelLimitsOverride與serializer](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L127-L176) | Python numeric欄位經helper輸出camelCase：`maxPromptTokens`等 |
| [create](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L2719-L2721)／[resume](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L3408-L3409) | 都使用同一高階helper；本診斷只重現create，resume這點是source核對 |
| [Generated limits DTO](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L20505-L20542) | `from_dict`／`to_dict`只讀寫snake_case numeric欄位 |
| [高階set_model](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L3170-L3185) | 把camelCase字典交給generated `from_dict`；真method配合不呼叫runtime的public receiver，收到`limits={}`，`vision=false`仍保留 |

因此**同版高階set_model的三個numeric欄位在此轉換中丟失**是可重現事實，不是FA自行拼錯JSON。
但這不證明它是create後attribution128000的唯一原因，也沒有證明任何未觀察到的inference limit實際改變。
原本的FA create/resume使用公開高階types；本slice沒有改用私有RPC或patch SDK。

### 正式typed替代仍未改善觀察

維持相同model、64000/60000/2048 facts、原user文字hash及短合成回覆，用公開
[`ModelSwitchToRequest`與`model.switch_to`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L35446-L35559)
傳generated snake_case limits，**明確`run_compaction_preflight=False`**：

- Safe RPC欄位確認是typed公有參數，不是手寫未知JSON-RPC；回覆`applied`、非deferred，無confirmation／persistence error。
- 立即的無callerlimits [`get_context_attribution`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L42453-L42455)
  回null；依公開可空語意保留UNKNOWN，不補0或用`context_info`傳入宣告值再自證。
- 一次正常offline request之後，prompt／display limit仍是128000，provider仍無output token欄位；60000的期望再次RED。
- 一次model-change、零compaction、原user event精確，工作保存成功且runtime正常stop，不代表capacity已生效。

所以沒有已證實安全且能改善這個觀察的公開替代，**不採自動switch workaround**。
`preflight=True`未跑，既不宣稱它必須也不宣稱它足夠；若未來要採model-switch compaction，
須另外處理CTX-03版本契約，不能把不同事件偽標成manual。

### 七批原結果與更正索引

| 批次（台北2026-09-25） | 原結果 | 範圍 |
|---|---|---|
| 11:39 initial fixture | 1 failed | 缺正常short executor root，runtime directory `/`錯；完整request／stop counts未知 |
| 11:40 high-level create | 1 failed | 配置已送，前後attribution128000而非60000 |
| 11:41 same-source repeat | 1 failed | 同一缺口重現，不是新獨立品質樣本 |
| 11:43 high-level set_model shape | 1 failed | numeric limits丟失；0 runtime／0 model |
| 11:43 generated switch first | 1 failed | Safe observations已保存，但test誤解參考null；原TypeError保留 |
| 11:45 generated post-turn observation | 1 failed | typed switch applied後仍128000≠60000 |
| 11:45 generated DTO controls | 3 passed | known／partial／all-unknown roundtrip，0 runtime／0 model；不是enforcement GREEN |

合計是六個失敗test與三個DTO checks通過：四個刻意capacity／serializer RED（含同source重複），
另兩個診斷fixture／observer錯誤，不是六個新產品bug。四份完整native receipts各有1個offline request，
stop exit_code=1／graceful=true／force-stop=0；首fixture缺完整count，**不可算「總共只有4次」**。

交付時發現V1派生index把DTO陣列的null欄位誤算成1個runtime／3次model calls；
authoritative `capacity-interface-batches-v2.json`只修那兩個counts為0。
V1、correction回條及原XML／log／safe evidence／test source都保留；沒有補跑，也沒有修改原失敗結果。

### 採用狀態

診斷為`DIAGNOSIS_COMPLETE_PRODUCT_ADOPTION_BLOCKED`。SDK serializer差異與typed替代的反證
一起保留，TC-04仍partial，effective／provider hard limit及可靠小視窗preflight仍NOT_PROVEN。
這不是停止整體Target State工作；只暫停沒有證據可安全採用的容量替代，接續不依賴此修法的切片。

## Verified against

- 初始handoff `dd155da30d108f35796d161810c24f03be14891a23d8bd719481caba1943b042`；修後handoff `f3a785cc10173b1531826f6581b81c2b4caecbcc6f2a7d27d1c03748ac27abc7`。原12批及新4批XML、各版本372個source pairs都分開核對。
- 原75/API XML `34040a9ca501af29b677f19936f1b42df1595833934311aecdfcdf09bfc3ba1c`；原native `2dcd224b9ac1dcb370ad7a0b2153576f0396ab2e3fbd1334b1861b93d6e50b9d`。
- 修後17/API XML `f15fb57e3fd8637e80c34695be291be22583bcd6d3bfedc9670f4e9e69e9593a`；structured native `530558b87b2a7e4c4f50f282d130c961a4f56dbbc4b2486774c77cdfc18bf782`。
- Integration ZIP `b34555c39390d8ac41698b8a8d87be5e9f72f96548ee1018b7bae0ae9d2c0905`／JUnit `d584c6da3d78fd0151a503e804d8036ded8007e588cf55581b54c66403cf25ed`，只清理自己已停止的解壓fixture，沒有改其他workspace。
- 無deployment、paid inference、IAM／keys、live data、harness或schedule操作。Source事實／SDK配置／runtime觀察／provider保證分層；raw SDK、private per-source attribution、tokens、DB／credentials不公開。
- 診斷handoff SHA256 `1572d148295a17ad27bc595373a7cd5407dd7694f27dbc912376a6aff2e5e4e8`；
  V2 index `b5ec325e95bcb88331e63381604d4f6d380e3244f6a22edf68c60d727f001eee`；
  index-only correction `f482070607a43b90fbe4c5245701a10044ed278f9d196b8747c72b9dceb2bdf4`。
- Coordinator另讀自身固定SDK的上述五段source，核三份installed檔案與凍結official copies byte-identical；
  並核372個產品source pairs、七份JUnit及所有逐批hash。收件未重跑任何SDK／模型，也沒讀peer live worktree。
- 最後typed-switch safe proof SHA256 `8c4bef403aad93407f4032155250888fa2be5a27609e76c6d1f7e86a98fda68c`；
  官方copies hash是client `7cb524f79cf8431399b8292eeccb999ddf26e3b9c32c252680a29c991c55e8b9`、
  session `5875be04c7fc65e7b9f11b4f1b7ce60d720167dfa6c1f4942f158be8cb33813f`、
  generated RPC `0dd7ca6eb6c816fb4d1e672eeb8a8c33b755a1b85708cc2d24d820d499cbcb3c`。
