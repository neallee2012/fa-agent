# FA Agent 行為控制規格 v1.2

## 2026-10-10 toolbox 列不出工具：這一回合少那個 toolbox 照常跑（Neal 決策；CODE/offline，未部署）

Neal 2026-10-10：「工具列不出來時，這一回合少那個工具照常跑，並在畫面上講明」。起因是 stage 的 `fa-org-tools`
（WebIQ 來源上游回 401）讓 `tools/list` 回 `-32006`，fa-stage-a 每一回合都在綁定工具時被拒（`NATIVE_CAPABILITY_REFUSED`），
連單純聊天也不行。

- **只拿掉那個 toolbox**：回合開始時某個 toolbox 列不出工具（身分 token 取不到、連線失敗或逾時、回應讀不懂、5xx／429、
  toolbox 回來源失敗的 `-32006` 或非明確的 JSON-RPC 伺服器錯誤如 `-32603`），這一回合照常跑。它的 capture 工具
  仍綁著，呼叫只回 `NATIVE_TOOLBOX_UNAVAILABLE`、不送任何請求；模型被告知這不是權限拒絕、照常回答並告訴使用者這次沒用到。其他 toolbox 與能力照常。
- **仍然拒絕**：toolbox 本身對這個 Agent 的明確 HTTP 4xx、對請求或設定的明確 JSON-RPC 答覆（`-32700`、`-32600`、
  `-32601`、`-32602`、不帶撤銷文字的 `-32007`）、清單裡少了 capture 的工具、批准設定（`always` 沒 HITL、
  不明值、讀不到 default version）、重複或無效 schema、沒有 project endpoint；skills／mcp-api 的授權與版本檢查不變。
- **不連 Connect 卡**：`-32006` 只有帶 consent 連結、且（有逐來源清單時）其中有 `CONSENT_REQUIRED` 才出 Connect 卡；
  上游失敗同樣用 `-32006` 回時當作列不出工具。
- **記錄與畫面**：回合的組裝紀錄（`NativeAssembly.unavailable_sources`）存 `entry_id`、來源名稱、`TOOLBOX_LIST_FAILED`
  與固定原因分類；Hosted 記一行 `FA_NATIVE_TOOL_SOURCE_UNAVAILABLE`，同一回合同一來源只記一次。工作台在那則回覆下方顯示
  「這次沒用到 WebIQ：工具清單讀不到（上游驗證失敗），其他功能照常。」，代碼只在支援明細。
- **不重試、不記住失敗**：每回合照舊只列一次；下一回合重新列。

實作、測試與 `Verified against` 見 [README toolbox 列不出工具](../src/fa-shared-context-poc/README.md#toolbox-list-unavailable)。
降級回合的 receipt 多了 `unavailable_sources`，舊版 API／工作台會拒收，Hosted、API、工作台要一起出版。
dev／stage 驗證待部署後執行。

## 2026-10-07 平行工具呼叫：批准寫入排隊，失敗說清楚能否再呼叫（CODE/offline，未部署）

stage 第 18 版 Neal 實際使用：模型平行送 4 個 Team MCP `read_item`，兩個在 HITL 批准寫入時互撞（API 412
`BEHAVIOR_CONFLICT`），工具沒執行，模型卻收到通用的「Authority refused…do not resend」，之後拒絕使用者的重試要求。

- **同一回合的 HITL 控制寫入排隊**：`/pending`、`/authorize`、`/questions`、`/observations` 一次只送一筆；
  只排寫入本身，人類等待、審查與工具執行照舊平行。API 不加自動重試（`automatic_retry: false`、批准／否決只有一個贏不變）。
- **批准寫入失敗＝沒有執行**：工具執行前，批准寫入回 `BEHAVIOR_CONFLICT`（別的寫入先贏）或 `BEHAVIOR_COMMIT_UNKNOWN`
  （回覆遺失）時，模型收到 `NATIVE_HITL_NOT_STARTED`：工具沒有執行、不是人類否決也不是權限拒絕，可以用新的呼叫再做
  （會重新經過批准），使用者要求重試時照做；不附「不要重送」。這適用所有經 HITL 的工具（含桌面、shell、peer），
  因為工具只在批准完成後才執行，新呼叫有新的 call ID 與新的批准。有批准卡時原等待收尾為 `interrupted`。
  其他執行前失敗（回合停止、過期、否決等）與執行後失敗照舊。
- **toolbox 工具失敗**：toolbox `tools/list` 標 MCP `annotations.readOnlyHint=true` 的工具，任何失敗回
  `NATIVE_TOOLBOX_READ_FAILED`（只讀、可再呼叫）；沒標只讀時，執行前就被拒回 `NATIVE_TOOLBOX_NOT_RUN`（沒有執行），
  結果不明（逾時、5xx、連線中斷、回應讀不懂）回 `NATIVE_TOOLBOX_OUTCOME_UNKNOWN`（2026-10-10 C5：不重送，請使用者先到服務上核對）。HITL 對本回合綁定的 toolbox 工具原樣傳遞這些代碼。
  標註只改失敗文字，不改授權、`require_approval` 或批准。同一個 SDK call ID 重播照舊不重送。
- 系統提示（有 hash 的 profile）不改；其中「unknown outcome 不要重送」指結果不明的效果，與上述「沒有執行／只讀」不衝突。

根因證據、官方依據（MCP `ToolAnnotations`、Foundry toolbox `tools/list`、Table 原子批次）、測試與 `Verified against`
見 [README 平行 toolbox 呼叫失敗與重試](../src/fa-shared-context-poc/README.md#toolbox-parallel-retry)。dev 驗證待部署後執行。

## 2026-10-07 RTT B：模型邊寫邊顯示即時文字（PR1：Hosted＋API，CODE/offline，未部署）

同一則 Neal 2026-10-06 23:3x 指示（見下方「RTT A」）；總協調 2026-10-07 核准設計與共用檔案改動。
目的是第一段文字不必等存檔、用量回報與讀回（v17 dev 暖回合模型第一個 token 約 +7.4 秒，畫面文字 +12.1 秒）。

- **即時文字只是預覽，不是結果**：Hosted 把主代理、使用者可見 phase（`None`／`final_answer`）的 Copilot SDK
  `assistant.message_delta`（官方暫時事件，不寫入 session 記錄）以官方 `response.output_text.delta` 寫進 `commentary` 項目；
  reasoning、子代理、`commentary` 或未知 phase 不送；套與存檔回覆相同的 redact。`final_answer` 仍是存檔後送出的原文；
  記憶、交付、恢復、checkpoint 都不讀即時文字。取代 README「N6-1a：VM Responses 回合」中「不串保存前的模型文字」的限制，
  只限上述預覽。2026-10-05「回覆預覽」（SDK 停止前寫一次）不變。
- **送出**：只有 owner `/submit` 回覆後送出自己這一則時，以官方 `background+store+stream` 建立（重播必須建立時就開），
  讀到 `response.created` 即關閉連線（背景串流斷線不取消回合）；沒有 created 或中斷照舊 `NATIVE_RESPONSE_UNCONFIRMED`、不重送。
  推進其他 holder、`/reconcile`、T5、routine、continuation 維持 `stream=false`，保住 409 `conversation_locked`。
- **讀取**：新唯讀路由 `GET .../responses-turns/conversations/{cid}/requests/{rid}/stream?starting_after=N`，
  T1 後只重播原回條上的 `response_id`，用 API MI 呼叫官方重播，只轉白名單欄位；斷線以 `starting_after` 接續；
  回條未開始回 409 `NATIVE_STREAM_NOT_STARTED`、不可重播回 `NATIVE_STREAM_UNAVAILABLE`。
- **用戶端**（PR2）：第二個以後的 `response.in_progress` 清空預覽；轉送在 `final_answer` 項目（已存檔）或終局事件後結束，
  之後讀 `/status` 換成存檔回覆。

細節、官方依據與 `Verified against` 見 [README RTT B 即時文字預覽串流](../src/fa-shared-context-poc/README.md#rtt-b-reply-preview)。
dev 平台驗證（逐筆即時轉送、提前關閉 POST 不取消、`starting_after`）待部署後執行。

## 2026-10-06 RTT A：模型前準備瘦身（CODE/offline，未部署）

Neal 2026-10-06 23:3x：「在不違反以下原則的情況下，依照評估的結果，最佳化FA agent對話的round trip time。我在使用grokbot的體驗上，幾乎是我按下"送出"鈕後，1秒內就看到Agent開始動作，3-8秒就會收到Agent回覆的第一個訊息。1. 請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。2. GrokBot的作法 3. Hosted Agent ans Copilot SDK的最佳實踐 4. CharterAgent的作法參考」。
依據是 v17 dev 暖回合的逐段量測（原件在部署線工作區，未存入 repo）。

- **approved skills 依精確版本沿用目錄**：Hosted 程序依 capture 的 `template.skills` 精確版本保留 API 回的目錄；
  同一組版本的下一回合 bind 不再讀 `/skill-catalog`（v17 每回合約 0.63 秒）。取代 2026-10-06「bind 沿用工具清單」修訂中
  「approved skills 不變」一項：版本改變或撤銷時，API 在送出解析 capture 時（既有核對）就回 409 `PROFILE_SELECTION_SOURCES_CHANGED`，
  回合不會被收單；回合中每次讀 skill 本文照舊由 API 核對，改版或撤銷回 `SKILL_SOURCE_CHANGED`。
  送出後、開始前才改版的回合，bind 不再擋，模型看得到舊目錄但讀不到本文。
- **記憶召回提前並行**：私人／Team 記憶召回不依賴 bind、提醒與 operations 的結果，改在 bind 之前啟動、在原位置取用；
  失敗照舊處理、不重送；回合先失敗時取消未取用的召回。
- **收單重用剛讀到／寫入的紀錄**（RTT A 第二批）：同一個收單鎖內，不再重讀剛讀到或剛以 CAS 寫入的 agent 紀錄、`rid:<RID>`
  與對話 meta；標記收單意圖前的重讀與 CAS、`rid:<RID>` create-or-read 去重、寫入確認遺失時讀回都保留；清除意圖的 CAS
  失敗就退回重讀。細節見 [README 收單重用](../src/fa-shared-context-poc/README.md#rtt-a-admission-reuse)。
  （「標記前的重讀」由下方第三批「收單開頭讀取並行」取代：改用開頭讀到的 ETag 做 CAS，紀錄被改動就重讀再標記。）
- **接續回合送出預讀**（RTT A 第三批）：API 確認 session 的同時，用已保留的 reader 先讀對話、用該對話固定的 template
  與 selection 先解析 capture；只有與照順序讀到的完全相同才採用，否則照原順序重讀。只讀不寫，去重仍只在 Hosted。
  細節見 [README 接續回合送出預讀](../src/fa-shared-context-poc/README.md#rtt-a-submit-prefetch)。
- **收單開頭讀取並行**（RTT A 第三批）：Hosted 收單開頭的 agent 紀錄、`rid:<RID>` 與對話 meta 一起讀；標記收單意圖與
  Responses 槽位直接用剛讀到的 ETag 做 CAS，被改動就重讀再做，不蓋掉別人的改動。去重、意圖的標記與清除不變。
  細節見 [README 收單開頭讀取並行](../src/fa-shared-context-poc/README.md#rtt-a-admission-parallel)。
- 只加計時：native 送出、讀取、skill 目錄、用量回報路由與 Hosted 收單的分段 `FA_TIMING`。

細節見 [README bind 工具清單快取](../src/fa-shared-context-poc/README.md#bind-tool-list-cache)與
[送出路徑與通知鏈的分段](../src/fa-shared-context-poc/README.md#rtt-a-timing)。

## 2026-10-06 伺服器接手換版；接單前拒絕碼固定（CODE/offline，未部署）

依 Neal 2026-10-05「嚴格依照 hosted agent 官方和 grok bot 的方式……把非必要的邏輯拿掉」與 2026-10-06
「對標 grokbot and charter agent……做法是對的就直接開始」，換版／不搬檔的判斷移到伺服器，
工作台的狀態機由工作台線（#679）拿掉。
官方 Hosted session 綁定版本，selector 不遷移既有 session（[研究](hosted-cold-start-latency-research.md)），
所以 FA 仍要 follow；改由伺服器做：`/submit` 在接單前遇到 `NATIVE_SESSION_CHANGED`／`AGENT_VERSION_FOLLOWING` 時，
同一請求呼叫一次既有 `advance`，回原錯誤碼加 `not_dispatched`、`request_id`、`server_following=true`，
工作台用同一 RID 與 body 重送。換版本身失敗回該錯誤碼、不帶 `server_following`；
未 prepare 的 Agent 不在 submit 裡 bootstrap，已 prepare 但新版本 session 還不存在時照樣推進（由伺服器取代前端的 environment-auto）。不新增真人「確認不搬檔」：Hosted 對真人確認不比系統政策寬，
原搬檔結果未確定時回原錯誤碼（多半是 `NATIVE_FILES_UNKNOWN`），不帶 `server_following`。
接單前的附件、來源、文字檔與圖片拒絕都回固定錯誤碼（白話改寫由工作台處理）；補上原本會被改成通用碼的來源讀取錯誤。
首次 submit 在 Hosted 建立 `rid:<RID>` 之前確定被拒（4xx，或整個 Agent 換版中的 503）時，API 與 Hosted 都標
`not_dispatched`，只表示這次沒送出，client 只能用同一 RID、同一份 body 再送；其他 5xx、結果不確定與 `NATIVE_RID_CONFLICT`
不標。須和工作台 #679 同一次發布。
細節見 [README](../src/fa-shared-context-poc/README.md#server-following-submit)。

## 2026-10-06 native 回合結束不再停 SDK 程序（CODE/offline，未部署）

Neal 09:24：「回合結束時停止 SDK 程序（約 2.3 秒）<--- 需要每回合結束都停止SDK程序嗎? 是否有參考sdk官方最佳實踐?」；
08:26：「對標grokbot and charter agent, 和hosted agent的官方最佳實踐，如果做法是對的，就直接開始，不用等」。
本修訂取代 native 路徑「SDK 正常停止後才保存 checkpoint」的要求；legacy job-bound 路徑不變。

- 依 SDK 官方 Pattern 1（一位使用者一個長駐 CLI server，工作結束只 `session.disconnect()`）及
  [ADR-0005](adr/0005-long-running-conversation-worker.md)，每台 Agent VM 最多保留 2 個閒置 SDK，超過就照舊停最舊的。
- 回合 idle 完成、程序活著、checkpoint 正常且 idle 後 files 沒再變時，不停 SDK，直接保存 checkpoint 與回覆。
  這回合呼叫過委派（`task`）、manual compaction／diag codec、取消、checkpoint 失敗、自動 compaction 還在跑時，
  照舊停程序，並以 warning 記 `RESIDENT_SDK_STOPPED reason=...`。（2026-10-06 dev 修正：原本「選了委派」或「審過任何工具」
  就停，開了委派與 HITL 的 Agent 每回合都停；只選未呼叫、或審核直接放行沒出卡的回合沒有子工作也沒有等待，現在保留。
  2026-10-06 v17：出過批准卡或提問的回合也保留；idle 時 SDK 已拿到每個回覆，核准只限該次呼叫，卡片與問題綁原回合，
  理由與出處見 [README](../src/fa-shared-context-poc/README.md#resident-sdk-after-hitl)。）
- 下一回合只在拿到的已發布 checkpoint 正是 release 快照、且 files 之後沒變時沿用；否則停掉並從 checkpoint 還原。
- SDK session 的 hooks 與 ask_user 轉接到當前回合的 operations guard 與 HITL；沒有綁定回合時一律拒絕。
- 保留：接單、RID 去重、UNKNOWN 不重送、CAS 保存與回條。殘餘風險：固定版 SDK 沒有 flush 介面，
  release 後晚到的寫入（只在 auto profile 背景壓縮剛好在 idle 後開始時）不在已發布 checkpoint 內；
  下一回合會以 `RESIDENT_FILES_MOVED` 退回從 checkpoint 還原，歷史完整，使用者資料不會遺失。

實作與測項見 [native 常駐 SDK](../src/fa-shared-context-poc/README.md#native-resident-sdk)。
回覆預覽照舊：保留 SDK 時，預覽同樣在保存前寫出，`completed` 仍要等保存完成。

## 2026-10-06 平台讀取重試一次、讀取路由可安全重讀（CODE/offline，未部署）

stage 2026-10-06 12:40：一次 `get_session` 卡滿 10 秒（`retry_total=0`）就讓 `status` 回 503 `NATIVE_BOOTSTRAP_UNKNOWN`、
`automatic_retry:false`，工作台因此停止輪詢，回覆要手動重新整理才出現。本修訂取代「平台 metadata 讀取一律不重試」
與「讀取路由的 5xx 一律 `automatic_retry:false`」兩處。

- API native lane 對 Foundry 控制面的**讀取**（`behavior_native_sessions` 的 `get_session`、`get_version`、`list_sessions`，
  以及 `ProjectAgentFoundry` 的 `get_agent`、`get_version`、`list_versions`）改用 azure-core 官方 `RetryPolicy`，
  連線失敗或讀取逾時重試一次（`PLATFORM_READ_RETRY`，第一次重試不等待）。平台回的錯誤狀態碼不重試，
  因為 azure-core 會照 `Retry-After` 等待且沒有上限。GET 是冪等的，重讀不會送出任何東西。**寫入**（`create_version`、
  `update_details`、session 建立／停止）與所有送訊息的呼叫照舊 0 次重試；舊 conversation lane 與 recovery 的讀取不在這次範圍。
- `install_native_turn_routes` 的 GET 讀取路由（回條、`status`、進度、歷史、環境…）遇到 Hosted／平台錯誤碼的 5xx 時回
  `automatic_retry:true`，說明「這次讀取沒有完成，再讀一次是安全的」；Table 暫時讀不到（`BEHAVIOR_READ_FAILED`）與
  讀取時記用量的 commit 回覆遺失（`BEHAVIOR_COMMIT_UNKNOWN`，同 RID 重讀會沿用原 commit）同樣如此。
  再讀也不會好的 `NATIVE_TURNS_DISABLED` 與已結束的平台 session 仍是 `false`；其他未列出的錯誤（例如未預期例外）
  仍走共用錯誤處理、回 `false`。重試用盡仍是同一個 503，工作台照原節奏輪詢。
  4xx 不帶；所有 POST（送出、reconcile、換版操作）照舊 `automatic_retry:false`，UNKNOWN 不重送的規則不變。

實作與工作台介面見[平台讀取重試一次](../src/fa-shared-context-poc/README.md#platform-read-retry)。

## 2026-10-06 Responses 送出改到背景（CODE/offline，未部署）

Neal：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」；
「對標grokbot and charter agent, 和hosted agent的官方最佳實踐，如果做法是對的，就直接開始，不用等」。
本修訂只改 owner `responses-turns/submit` 的回覆時點；取代 2026-10-02 N6-3／N6-4 段「真正HTTP或未確認接手回503…吃原有限錯誤預算」
與「首次與後續真錯共10次」在 owner submit 上的說法（`/reconcile` 的 503 與 10 次預算不變）。

- Hosted 寫好 accepted 回條（`dispatch.state=needs_response`）即回覆；同 RID、同 body 的 Responses POST 在回覆後送一次，
  不再讀回條。charter-agent 也是背景送 POST、畫面不等；官方 Responses `background: true` 本來就立即回 queued。
- 確認與補送沿既有工作台 `submit-reconcile`／API `/reconcile`（同步、讀回條、有限退避）；不新增 queue、ledger 或 poller。
  背景 POST 的錯誤只寫 log、不計入工作台預算，所以持續失敗時同 RID 最多 1＋10 次 POST；
  平台 401／403 改在第一次 reconcile 才看到並停下（多一次同 RID POST，被拒、不會執行）。
- 不重送：背景 POST 與 reconcile 撞在一起時，同 CID 的第二個 POST 由官方 SDK 回 409 `conversation_locked`
  （FA 當成等待，不扣預算）；Hosted 另有 `NATIVE_TURN_QUEUED` 與 `rid:<RID>` started CAS。UNKNOWN 不重送、Hosted 的 session／版本核對保留。
- `/reconcile`、T5、routine、continuation、群組啟動不變；新對話仍先建平台對話（CID 屬於回條與 digest）。

實作與量測見[Responses 送出改到背景](../src/fa-shared-context-poc/README.md#responses-submit-after-reply)。

## 2026-10-06 非同步交辦（CODE/offline）

Neal14:14「請直接參考官方建議一的做法」、14:15「遵循FA專案的原則」；
總協調確認A只等原官方`Task.start()`保存確認，就回「已交給 B 排隊，B 有空就會處理，結果會回到這段對話」，
A回合正常結束。背景仍在A的VM，以相同ID投遞；API只核身分、轉送、去重及讀回原件。
此決定取代同日10:20／13:24的前景60～90秒方案；不再等B HTTP或模型結果，
不能把FA的queued冒充B已收到／accepted，也不能丟掉排隊工作。

- 沒有前景等待倒數；後台沿core2.2預設timeout，**不使用部署1200秒**。
  task正常等待不耗錯誤retry，程序重啟不重跑原模型、不刷新原SDK期限。
- 原引用須綁Agent/CID/RID/tool-call／recipient／原command digest與SDK task ID，
  task保存確認後才能在A完成／中斷後繼續原投遞；不同命令／取消仍擋。原Team、HITL及once不放寬。
- ACK與嘗試標記放原State store的獨立task結果紀錄，原已完成答案、UNKNOWN與checkpoint不重寫。
  B仍以原message ID將回覆送回A原對話；A可在等待期间處理新回合。
- B忙碌、換版或登記後未就緒都由背景原task等待；B的原生queue暫滿也保accepted，不因十次滿額就退單。
  真正權限／輸入錯誤仍明拒，歷史已持久的拒絕不改寫。
- 官方到期只有可證沒有未知POST/ACK才標未送達；其餘保持待核對，僅核原件、不換ID補送。
  工作台等待／未送達／待核對分開，不設正常流程的人工作業入口或自動重送。

實作、SDK差額及離線界線見[非同步交辦README](../src/fa-shared-context-poc/README.md#asynchronous-peer-delivery)。
本片不部署、不中斷原工作、不改release.json/IAM/SDK版本；#684的page_changed修復分開保留。

## 2026-10-06 API 讀取直接用已知的 session（CODE/offline，未部署）

Neal：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」；
「對標grokbot and charter agent, 和hosted agent的官方最佳實踐，如果做法是對的，就直接開始，不用等」。
本修訂取代「每次讀取都重讀 selector 與平台 session」的做法，也取代 2026-10-03 兩句
「原回條GET沒有當前版reader就明拒需接續，不透過舊SID嘗試」與「ordinary current每次讀當前reader與原binding，不回退舊SID」。
換版操作與 `GET /environment` 仍每次讀 selector。

- API 在程序內記住每個 Agent 的「選定版本＋session」，讀取（含 2026-10-05 新增的 `status`）直接呼叫；
  即 10-05「讀取路由不再重做舊對話檢查」所說另案處理的 3 次 Foundry 重讀。官方 session 建立時綁定版本、selector 不搬既有
  session；charter-agent 也是保存 `agent_session_id` 直接帶著呼叫。
- （2026-10-12 修正）用已記住的 reader 前先比對 endpoint selector：版本不同就丟掉，不呼叫舊版本的 session
  （呼叫舊 SID 會讓平台在 Hosted 執行前用另一個版本把它重建）。只有讀取與輪詢沿用 10 秒內開始的 selector 讀取，
  selector 暫時讀不到時沿用 120 秒內確認過的 reader；送出、寫入（圖片上傳、取消、送達回條、追加）、排程 routine、
  Agent 間交辦與接續、換版後的接續通知、prewarm 每次在請求內讀 selector。選定版本還沒有 session 時不呼叫舊版本的 session：
  送出回 503 `AGENT_VERSION_FOLLOWING`（`not_dispatched: true`），交辦收件回 503，圖片上傳、追加與接續回 409
  `NATIVE_SESSION_CHANGED`。取消與送達回條送到持有回合的 session：Agent 紀錄仍指向它，且平台 metadata（不叫醒 session）
  確認它 `active` 時才呼叫（在跑的回合所在的 session 是 active；只有 `active` 的 SID 可呼叫，見
  [ADR-0009 §10](adr/0009-version-model-g-actual-version-idle-upgrade.md#10-不叫醒已回收-session-的規則立即適用phase-0)）；
  否則不呼叫任何 session，回原錯誤碼並帶 `not_dispatched: true`，同一個指令可原樣再送；metadata 讀取被拒（401／403）
  則原樣回報，不帶 `not_dispatched`。metadata 讀取與呼叫之間 session 仍可能轉成 idle，這時呼叫可能叫醒它：此風險接受，
  只限使用者明確按下的停止或送達回條，對象片刻前還是 active。
  排程 routine 不呼叫舊 SID，但會建立選定版本的 session（`_destination`，再以 `idle_only` 換環境）；
  被拒絕時 routine 時鐘記 `FA_NATIVE_ROUTINE_CLOCK_REFUSED`，下一個 tick 以同一個固定 RID 再試。
  其餘恢復要靠外部 follow、agent-versions sync 或漂移替換建立選定版本的 session。
- （2026-10-12，ADR-0009 §10 phase 0）換環境（follow／`advance`／retarget）也只碰 `active` 的來源：來源 idle
  （compute 已回收）或其他非 active 狀態時，不 invoke、不經 Files API 讀檔、不 stop，回 409
  `NATIVE_FILES_SOURCE_UNAVAILABLE`；自動接續以不搬檔完成，API 回條記 `files_migrated=false` 與
  `files_omission_reason`（工作台目前不顯示通知，2026-10-06 起；補回列在 ADR-0009 §8）。檔案已複製、停止前才變 idle
  也一樣（已核對的 staging 保留、不發布）。stop 只送給 `active` 的來源。停止後核對讀來源檔案只限 FA 自己的 stop：
  `stop_started` 在平台接受 FA 的 `stop_session` 之後才寫；沒有這個紀錄就變 inactive 的來源當成被回收，不讀檔、不搬檔
  （ADR-0009 §10 窄例外）。舊版 `fa-chat-…` 對話在 admission 只讀 metadata，SID 不是 `active` 就回 409
  `CONVERSATION_SESSION_UNAVAILABLE`，照既有規則明確 reset 或開新對話（reset 用 `stop_session`，不 invoke）。
- （2026-10-13，ADR-0009 phase 1）API 在 Context Table 為每個 Agent 記一列綁定列（目前 SID、建立時的版本、實際版本、
  漂移旗標與通知位、之前的 SID），只記錄、不改任何回合結果；平台在同一 session 換了版本時，主人本人的回應帶
  `environment_notice`，phase 1 不清除（顯示在 phase 2）。詳見 [共用 README](../src/fa-shared-context-poc/README.md#binding-row)。
- 只在已知會變時重查：本程序的環境操作開始與結束時（操作期間的讀取不會喚醒正要停止的舊 session）；Hosted／平台回 `NATIVE_SESSION_CHANGED`、`AGENT_VERSION_FOLLOWING`、
  binding 不符或 404（重查一次並重做同一個讀取）；其他平台失敗（下一個請求重查）；同一綁定 1 天未用。
- Hosted 的紀錄讀取先讀一次 Agent 紀錄，已有更新版本接手就 409 `NATIVE_SESSION_CHANGED`、不讀紀錄；
  expected-version 前置條件保留。送出仍先核一次 Agent 紀錄；接單前被拒只清快取，工作台用同一個 RID 重送，UNKNOWN 不重送。

實作、量測與部署注意見[API 讀取直接用已知的 session](../src/fa-shared-context-poc/README.md#api-reader-cache)。

## 2026-10-06 群組 context 與交辦名冊只在需要時讀（CODE/offline，未部署）

Neal：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」；
「對標grokbot and charter agent, 和hosted agent的官方最佳實踐，如果做法是對的，就直接開始，不用等」。

- 群組：Hosted 只在可能屬於群組的回合問 API `/native-groups/v1/context`，回合結束的 `/complete` 同理。
  排程回合與真人在既有對話的後續回合不問（API 規則下它們不可能是群組 root）；API 回「沒有群組」的 root，
  VM 記住後同一 root 的後續回合也不問。群組回合照舊每回合讀最新狀態。
- 名冊：`/recipients` 同一段對話在 VM 內最多 5 分鐘讀一次（回應沒有版本，沿用 mcp-api catalog 的 300 秒上限），
  交辦失敗就重讀；空名冊不記住。收件者就緒與同 Team 核對仍由 API 在送出當下做。PA-03「沒選協作就明說沒有名冊」不變。
- 兩者都只放程序記憶體（有上限），VM 回收、換版或重啟後重新查；不加回條、command 或 capture 欄位，
  `submit_digest` 與存檔格式不變。

依據、量測與退版影響見[群組 context 與交辦名冊只在需要時讀](../src/fa-shared-context-poc/README.md#hosted-group-roster-slimming)。

## 2026-10-06 即時推送提示（Azure Web PubSub，CODE/offline，未部署）

Neal 2026-10-06 08:28：「如果可以降低FA portal and api 的呼叫次數，就加Azure Web PubSub 這類雲端資源。」
API 在既有寫入成功之後，發一則只含 ID 的提示到該 Team 的 group（`team.<team_id>`，team 由伺服器決定）：
`turn.finished`（VM 的終態用量報告入帳後）、`hitl.approval`／`hitl.question`／`hitl.closed`、`agent.received`／`agent.replied`、
`agent.environment`（部署 follow 接版後）。工作台用 Team 主人專用的 `GET /work-policy/v1/realtime/negotiate` 拿 60 分鐘的
client URL，只能收自己 Team 的 group。**提示不是權威**：不改任何回條、交付、批准或去重；工作台收到後照舊讀原路由，
連不上就輪詢。發布失敗只記一行，不影響原寫入或回應；同一事件可能重複，接收端要冪等。Hosted VM 不發布，
因為 Service Owner 可以送到任何 group。欄位、發布點與 WAF 對照見
[README 即時推送提示](../src/fa-shared-context-poc/README.md#realtime-push)。

## 2026-10-06 bind 沿用工具清單（CODE/offline，未部署）

Neal 2026-10-05：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
總協調轉來的 v15 dev 量測：暖回合的 `native_bind_capabilities` 每回合 2.5～5.6 秒。官方 toolbox 整合（.NET `AddFoundryToolboxes`、Python
BYO 範例）都只在啟動或第一次請求時列一次工具、整個程序共用一個 MCP session；本修訂讓 Hosted 照這樣做。

- **toolbox**：Hosted 程序依 `(project endpoint, toolbox, 實際版本)` 保留 `tools/list` 結果與 MCP session（最多 32 個）；
  官方 toolbox 版本是 immutable 快照，`require_approval` 屬於版本設定。
  - grant 有釘版本就直接用。沒釘版本時每回合先讀官方 toolbox（`GET …/toolboxes/{name}`）取得目前的 `default_version`，
    這回合的清單與工具呼叫都走該版本的 endpoint；owner 換版本（例如改成 `always`）後下一回合立刻生效。
  - 讀取失敗時不沿用保留的清單：這回合照舊對 consumer endpoint 重新列、不保留；那也失敗就 bind 失敗、不開工具。
    清單成功時 fail-closed（總協調 2026-10-06）：這回合實際服務的版本不明，HITL 開著時該 toolbox 的工具一律先問，
    沒開時整個回合在綁定時拒絕（`reason=default_version_unread`，和沒開 HITL 時遇到 `always` 相同）。
  - 遇到新版本或清單缺 capture 的工具時，開新 session 再列一次。
- **session 結束**：工具呼叫帶 session 收到 HTTP 404（MCP 規格：伺服器已結束 session、尚未執行）時，重開 session 後在新
  session 再送一次；每個 tool call 仍最多執行一次。開新 session 有鎖，平行呼叫共用同一個新 session。其他失敗照舊回
  REFUSED／UNKNOWN、不重送。
- **mcp-api**：同一組 capture grant 的 API catalog 保留 5 分鐘；broker 每次呼叫仍核 grant 與 schema digest，任何呼叫失敗就丟掉保留的 catalog。
- **approved skills 不變**：每回合照舊向 API 讀 catalog，有效版本改變仍在 SDK 啟動前回 409 `SKILL_SOURCE_CHANGED`。
  （2026-10-06 RTT A 起改為依精確版本沿用目錄；版本改變由送出時既有的 409 擋下，見本檔頂端「RTT A」修訂。）
- **保留**：每回合開哪些工具只看該回合 capture；`require_approval` 依該回合確認過的版本判斷（2026-10-06 起 `always`
  在 HITL 開著時每次呼叫先問、沒開時拒用，見共用 README「N5」）；
  平台 call id 只帶當回合的值；每個 tool call 最多執行一次，結果不明的不重送。
- 每個 provider 另記 `FA_TIMING native_bind_<名稱>`。

實作、官方依據與量測見[bind 工具清單快取](../src/fa-shared-context-poc/README.md#bind-tool-list-cache)。

## 2026-10-05 讀取路由不再重做舊對話檢查（CODE/offline，未部署）

本機量測：一次 `status` 輪詢要跑 11 輪 Table（讀到終態那一次另加 7 輪用量補記）、3 次 Foundry 控制面呼叫、1 次 Hosted。
讀取路由拿掉舊 Table 對話檢查 `_legacy`（5 輪）：讀取只會讀 Hosted 自己存的紀錄，CID 也都由伺服器產生，這項檢查在讀取時沒有作用。
送出路徑照舊保留。

**讀到終態時補記用量保留不動**：Hosted 回合結束時報送用量，但報送失敗的某些路徑不會留下標記，`delivered` 也要使用者按確認才會送。
讀到終態時的冪等補記是防止帳本漏記的最後一道。成本只發生在讀到終態回條的請求上（重讀也會再跑一次，但冪等、不重複入帳）。
入口的 Entra 身分＝Team 主人，以及 Agent 選定（`_context`），都保留不動。另外 3 次 Foundry 版本／session 重讀，另案處理。

## 2026-10-05 回覆時間取自官方 item 的 created_at（CODE/offline，未部署）

工作台目前只能從 progress 讀每則回覆的時間，bridge 重啟後打開頁面，要逐則讀一次。歷史頁的每個 turn 和 `status`（`completed` 時）
改為直接帶 `finished_at`。這個值取自官方 State store 中該回合結果 item `turn:N` 的 `created_at`：`turn:N` 只在回合完成時建立一次，
之後不再改。精度是秒，只供顯示。這個時間**不寫進回條**：`WireModel` 拒收不認識的欄位，回條若多了新欄位，退回上一版後，
舊程式就讀不了 v16 寫的回條，歷史和後續送出都會失敗。用官方 item 本身的時間，存檔格式不變，也不多讀。
`unknown`／`busy_refused` 沒有 `turn:N`，所以沒有這個時間。

## 2026-10-05 回覆預覽：SDK 停止期間先顯示回覆（CODE/offline，未部署）

模型回完後，SDK 要先停止（約 2.3 秒），回合才能保存 checkpoint 並標成 `completed`。這段時間裡，Hosted 會在停止前，
把這次要保存的回覆文字另存一份「預覽」（TURNS store 的 `preview:<sha256(RID)>`，只寫一次，最多 128 KiB），
寫入和停止同時進行。`status` 路由只有在原回條還是 `started` 時，才帶 `preview`，而且不會和正式的 `text` 同時出現。
預覽只用來顯示：不是結果，不算送達；記憶、交付、恢復和 checkpoint 都不讀它。`completed` 仍要等保存完成。
保存失敗或變成 UNKNOWN 時，`status` 不再帶預覽，畫面要改顯示錯誤。預覽寫入失敗只記 log，不影響回合。
常駐程序、不停程序的做法當時暫緩；2026-10-06 已改為接上常駐 SDK（見本規格頂端同日「native 回合結束不再停 SDK 程序」與 [CTX-03 §8 註記](ctx03-checkpoint-contract.md#8-保存發布與完成的順序)）。

## 2026-10-05 單一 RID 輕量狀態讀取（CODE/offline，未部署）

Neal 2026-10-05 的送出量測：回合本身 1～3 秒，畫面卻要 8～14 秒後才看到回覆，因為工作台輪詢整段對話的進度，
回合結束後再另外讀回覆。新增 `GET .../native-turns/conversations/{CID}/requests/{RID}/status`：
Hosted 用一次 invocation 回 `NativeTurnStatus`（`fa-native-turn-status-v1`），內容是原回條加公開進度；
已完成且全文只有一頁時，同一次回應附上 `text`（API 會核對總 bytes 與 hash），多頁時回 `next_part=0`，
改用既有的 result 分頁。這只是查讀，不是收件確認，`delivered` 仍另外送；舊的回條、進度、result 路由都保留。
官方 Responses 背景串流的評估另行回報。細節見
[README 路由表](../src/fa-shared-context-poc/README.md#n2真人原生-api-入口codeoffline尚未切換-8765)。

## 2026-10-05 送出只在 Hosted 去重一次（CODE/offline，未部署）

Neal：「這些檢查是必要的嗎? 有嚴格對標hosted agent的最佳實踐、和grokbot的做法嗎」；
「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
本修訂取代 2026-10-01 N2 段「只有明確 NATIVE_RID_MISSING 才解析首次 capture」。

- 真人 submit 入口只核一次 owner→Team，`admission` 只讀一次目前環境；不先讀 RID、不在外送前重跑入口核對、
  不為 Responses 另讀 selector。撤權在下一回合入口生效（ADR-0006 第 1 節）。
- 同一 RID 只在 Hosted 的 `rid:<RID>` create-or-read 去重一次（ADR-0007 第 1、2 節）。Hosted 回
  `NATIVE_RID_CONFLICT`，或送 Hosted 前就被換版中／設定變更擋下時，才讀一次原回條：已接單的同內容沿原
  capture／CID 續送，不同內容明拒，查無回條就回原錯誤；已完成的直接回原回條。
- Responses 未啟用由 Hosted 以 403 `NATIVE_TURNS_DISABLED` 拒絕；群組把它和原本的 409 一樣記成成員未接單。
- 保留：入口身分、接單前換版快速拒絕（`AGENT_VERSION_FOLLOWING`／帶 `not_dispatched` 的
  `NATIVE_SESSION_CHANGED`，工作台用同 RID 重送）、UNKNOWN 不重送、Hosted 的 session／版本／Responses 啟用檢查。
- 經 `/submit` 重送已接單的 Responses 新對話第一則，可能多留一個未被引用的空平台對話；回條、CID 與執行次數不變。

實作與量測見[送出只在 Hosted 去重一次](../src/fa-shared-context-poc/README.md#owner-submit-slimming)。

## 2026-10-05 發版後全Agent自動接續（CODE/offline）

**2026-10-06補充：** 自動idle-only跟版遇到原工作回條分頁`page_changed`時，保留原操作與資料，
只重設掃描位置，回pending並從頭核對；已卡住的原目標亦適用。不把正常回條變動轉成永久檔案衝突，
不跳過回條／checkpoint，不重送原工作。手動檢查與其他未知效果的拒絕保持；
若selector已更高且原來源metadata仍核為原版，沿原retarget與idle-only界線自動續接，
不要求主人退回selector、不丟舊pending，未知檔案效果仍擋住。
尚未選定的retarget由同ID純讀原進度，批次到頂後仍能看見原工作完成；觀察不寫狀態或另建操作。
[實作與離線範圍](../src/fa-shared-context-poc/README.md#server-environment-follow)不等於live已驗。

**同日接單競態補充（CODE/offline）：** idle-only的首次prepare若尚無pending／following，
只因讀後的接單ETag變動遇到無`files_check`的`NATIVE_ENVIRONMENT_CONFLICT`，可讀回核對一次。
除ETag／admission外的整個環境必須相同、來源與selector仍符合原操作，重核授權後只再prepare一次；
同RID／source／target不變，Hosted原CAS仍生效。正常忙碌回pending，工作完成才接續。
已存在的following、未知ACK、檔案檢查、真正scope或版本變更、第二次衝突都不套用此重核。
沒有新增輪詢、排程或持久格式；已退出的CLI不常駐，後續尚未接單的新訊息沿既有送出前follow。
`Verified against`：`NativeSessions.follow`、`test_native_follow_admission_race.py`與
兩lane的`test_native_turn_api.py`送出前接續測項；詳細紅綠與限制見上述README。

Neal 18:2x問「為什麼還需要這樣切換，FA portal 應該都自動對標到最新版?」；
總協調確認：發版後每位助理接到**已發布selector版**，不等開portal。
部署`sync --route`尾端沿原NativeSessions.advance／N1/CAS，全部configured與Team roster的Agent獨立推進；
stage在API Healthy後執行，dev在新API內共用CLI。不是新的follow判斷或另存遷移狀態機。

- CLI要推進follow前，先沿原`composition.installation()`核原安裝與attach政策；
  純候選發布及`--defer-follow`不做此動作。未載入政策必須明示失敗，不代造政策。
  2026-10-05總協調確認：配置中的舊型peer（C／D）缺native政策時明示
  `environment_follow.state=skipped`／`reason=AGENT_SCOPE_MISSING`，不因此令CLI非0。
  原A、登記的native Agent與真正推進失敗／pending不適用此豁免；[dev入口修補](../src/fa-shared-context-poc/README.md#server-environment-cli-startup)保留原失敗與離線範圍。
- 每Agent同一版本使用穩定操作ID；原工作、HITL、question、未知PUT/publish、checkpoint不繞過。
  2秒／10次批次後只讀進度，有進展才續；部署等待上限只停止命令等待、不改原pending或取消工作，
  CLI非成功並列出版本已發布／環境未完成，禁止報整體ready。
- 已驗sender在交辦時若收件人只因換版未就緒，沿既有官方SDK持久傳送task保存原完整command，
  經同Team agent-only原環境接點推進。持久排隊後才顯示「已排隊，B 換版完成後處理；尚未確認送達，不會重送。」
  不能用queued作收件N1 acceptance；原工具要等真正接收回條才說已交給，舊訊息不換ID。
- 正常等待不消耗錯誤重試額度；shutdown以官方return sentinel留原task恢復，
  不重跑sender模型、不代批准。已不合法的原sender／Team仍拒絕。
- sender兩lane恢復須先接回原effect index所對應的既有SDK傳送task，不能先把sender變UNKNOWN
  再放寬API守衛。真正收件回條先沿原N1/CAS保留，task完成後才處理原checkpoint／終態；
  不依賴SDK task output、不改已持久UNKNOWN或原turn bytes。再次shutdown、取消及真錯誤仍有明確出口。
  已存ACK、dispatch尚未持久時，原task只核同tool-call的既有收件回條後續dispatch，不重走環境接單；
  原回條缺失／scope不同必須停止，不能拿同recipient的另一則ACK新送。
- 空白對話的progress renderer先核last／latest存在，避免undefined相等後讀欄位而拋錯中斷續做。
  前端只顯示／保留原草稿，不是伺服器跟版的唯一驅動。
- 預檢仍讀每一RID與最新checkpoint，只在同一scan slice內用已核較新meta turn排除舊completed重讀，
  不存跨slice快取、不略過舊accepted/started。不能以此宣稱live 4～6分鐘已縮為特定數字。

官方沒有selector自動遷移既有session保證；服務／task版本、實作與必要WAF對照見
[伺服器接續README](../src/fa-shared-context-poc/README.md#server-environment-follow)。
本片不部署、不改release.json/IAM；仍依ADR-0008先dev完整測試，再由Neal決定stage release。

## 2026-10-05 B 也看得到 A 交辦的對話（CODE/offline，未部署）

Neal：「當A傳給B時，B回覆的訊息會顯示在A，但是B那邊反而沒有顯示，可以讓B也顯示回覆給A的訊息嗎?」
B 的工作台改為向 B 自己讀「其他助理在 B 開的對話」（新 owner 純讀頁 `GET .../native-turns/received`，
Hosted 用 State store 正式 `list_keys` 讀 B 既有的原回條，不新寫紀錄、不建 API 索引），掛進對話清單並標明來源。
這類對話在 B 這邊**唯讀**：B 的回覆已自動送回 A 的原請求，在 B 這邊用真人訊息接續不會回到 A，也會混成真人指令；
不改授權，只是工作台不提供接續入口。A 的對話顯示 B 的回覆時，B 的側欄同步顯示該回覆與時間。
細節見 [Shared Context](../src/fa-shared-context-poc/README.md#agent-received-conversations) 與
[工作台 README](../src/fa-shared-context-poc/web_test_console/README.md#agent-received-conversations)。

## 2026-10-05 PA-03：沒選協作時明說這回合沒有同事名冊（CODE/offline，未部署）

[dev 第 14 版](progress.md#dev-v14-999a-live)用 v4 重跑 PA-03 仍 FAIL：中途關掉 `fa-agent-collaboration-v1` 後，名冊和交辦工具都已移除，
但 gpt-5.4-mini 照前面回合的回答和帶回的記憶列出 5 位同事。用 dev 存下的原始輸入直接呼叫 dev 模型重播：
原樣時 gpt-5.4-mini 5/5 照列、gpt-6.1-sol 5/5 正確；在名冊原本的位置補一句「本回合沒有選用同 Team 協作能力…」後，
gpt-5.4-mini 10 次中 9 次正確（1 次附上舊名單但標明不可用）、gpt-6.1-sol 5/5 正確。
修法沿用既有 `NativeAgentTools.prepare()` 的 `unavailable_reason` 接點，只在 native、沒選協作時加這一句，只講同 Team 同事與
`send_agent_message`，不涉及 `fa-delegate-v1` 子助理；不改 base prompt 與其 hash，不新增工具、gate 或狀態。
唯讀的系統提示檢視把這句當成固定段落一起核對整段 hash，這類回合仍能重建全文。
細節與重播結果見 [Shared Context PA-03](../src/fa-shared-context-poc/README.md#pa-03-not-selected)。

## 2026-10-05 Hosted SDK tracing 預設打開（CODE/offline，未部署）

依 Neal 2026-10-05「Tracing……可以預設打開」：Hosted 回合的 SDK tracing（`FA_SDK_TELEMETRY`）沒有設定時就啟用，
只有明確設成 `0` 才關；讀不懂的值關閉並記一次 warning。它只送到 Foundry 替專案連結、平台注入連線字串的
Application Insights；專案沒開監控就安靜地不送，回合照常。送遙測失敗只記 log，不重試、不延遲、不改回合結果。

- 內容：依 Neal 2026-10-04 同意，對話輸入、輸出、系統提示、工具參數與結果，以及模型服務公開回傳的推理摘要，
  跟同一個旗標一起記錄。不記、也不嘗試取得原始推理；任何模型回應帶有原始推理文字，或讀不到明文時，
  該程序停止送所有對話內容，工具 span 照送。憑證、token 與 connector 秘密不進 trace。
- 同一回合的模型請求帶同一個 W3C `traceparent`（取自第一次收單的請求），讓 Foundry 服務端的 span 與 SDK span
  可以串成一條 trace；服務端是否沿用尚待部署後實測。
- 套用範圍：程式預設，A、B 與「＋」建立的 Agent 都不需改 ENV；sync 與 onboarding 不會加上這個旗標。
  要關掉某個 Agent，以部署 overlay 設 `FA_SDK_TELEMETRY=0`，之後的版本會保留。
  `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL` 維持不設，與平台預設一致。
- 本片不新增雲端資源、權限、gate 或另一套 instrumentation。實作、官方來源與限制見
  [Hosted SDK tracing](../src/fa-shared-context-poc/README.md#hosted-sdk-tracing)。

### Verified against（Hosted SDK tracing）

`sdk_telemetry.enabled()`、`m2_copilot._ModelGateway`、`behavior_agent_versions` 的 sync／onboarding ENV 路徑，
與 `deploy/stage` 的設定；測試 `test_sdk_telemetry.py`（真固定 SDK／runtime＋離線模型替身＋本機假 ingestion）
與 `test_endpoint_versions.py -k tracing`。未部署、未在 Hosted 實測。

## 2026-10-04 唯讀查看系統提示（CODE/offline，未部署）

Team 主人可在工作台看到每位助理送進 SDK `system_message` 的全文：新對話目前會用的版本，以及單一原生回合的版本。
全文只由 API 依版本常數、回合保存的能力 grants 與 Team 核准的角色原文重組；回合版本必須和該回合保存的整段
SHA-256 一致才顯示，否則明說「無法重建」，不拿目前版本代替。同事名冊、群組、提醒、外掛說明與接續段落只列
bytes／SHA-256。全部是 owner-only GET，不改 selection、不啟動模型、不寫帳本或用量，也不新增權限。
API 契約見 [Shared Context 唯讀查看系統提示](../src/fa-shared-context-poc/README.md#system-prompt-view)，
畫面見[工作台 README](../src/fa-shared-context-poc/web_test_console/README.md#system-prompt-view)。

## 2026-10-04 SP agent-role v4：新 Agent 套用同樣五處（CODE/offline，未部署）

新增精確版本 `fa-instruction-agent-role-v4`：以 agent-role v3 逐字為底，只加上和 `fa-instruction-v4` 相同的五處，
用的是同一組文字。這五處是：
- 代辦交易
- 事實來源
- 本回合能力
- 先問或寫明假設
- `ask_user` 提問卡

v3 的「在哪裡工作」規則保留。onboarding 的新 Agent 預設改為 v4，只影響部署後新增的 Agent；
已建立的 Agent 與舊對話不變，agent-role v1～v3 的正文、hash 與舊紀錄都不變。
本片不新增工具、權限、gate、judge 或狀態機。
實作與選版欄位見 [Shared Context B1 SP agent-role v4](../src/fa-shared-context-poc/README.md#b1-sp-agent-role-v4)。

## 2026-10-04 SP v4：代辦交易、事實來源、本回合能力、先問或寫明假設（CODE/offline，未部署）

新增精確版本 `fa-instruction-v4`，v3 逐字沿用，只改五處（每處核對只出現一次）：
- **購買**：搜尋、報價、比較、準備訂單都不是購買。有桌面工具（`fa-desktop-v1`，瀏覽器顯示在即時桌面）時，
  助理在自己的瀏覽器裡直接做到最後一步前；送出、付款、登入、驗證碼、CAPTCHA 等交給真人在即時桌面接手或確認。
  沒有桌面時瀏覽器無畫面、回合結束就關，只給選項、官網連結和步驟。不代填密碼、驗證碼或付款資料，
  不自己按付款；項目或金額變了要重新確認；結果不明只核原回條，不重下。
- **事實來源**：列時刻表、價格、地址等，只列本次工具結果實際看到的項目並附來源；看不到就說，或用瀏覽器去官網查。
- **本回合能力**：能做什麼以這回合實際提供的工具和段落為準；前面回合有、現在沒有的能力（例如同事清單）說明目前不可用。
- **先問或寫明假設**（Neal 指定的通用規則）：先用對話、記憶與工作脈絡判斷；判斷不了時，
  猜錯代價高或只有使用者知道的資訊（例如訂票的起訖站）用 `ask_user` 問一句並給 2～4 個選項、可自填；
  猜錯代價低就照最可能的解讀做並寫一句假設（例如「VM」指助理自己的電腦）。同事轉來的請求一樣。
  長期偏好或固定事實的回答要在最後回覆複述，讓既有記憶流程保存；一次性回答不當長期。
- **表單**：原「聊天內表單未啟用」改成「除了已提供的 `ask_user` 提問以外」。

停點仍由既有機制負責：capability 附加段的交接句，以及 native HITL 自動審查要求真人接手登入、
驗證碼、CAPTCHA、付款確認與送出／付款按鈕。記憶沿用既有私人記憶（回合後抽取使用者原文與最後回覆）
和記憶候選工具，不另建。v1～v3、agent-role 系列的正文、hash 與舊紀錄不變；轉送 VM 預設（上一節）保留。
本片不新增工具、權限、gate、judge 或狀態機。

實作、stage 採用步驟與實測腳本見 [Shared Context SP v4](../src/fa-shared-context-poc/README.md#sp-v4)。
只驗文字與接線，不證明真模型會照做；部署後由總協調依實測腳本初判、Neal 確認。

## 2026-10-04 轉送工作中的 VM 指涉（CODE/offline）

收件端已驗 `agent_source.kind=agent_request`（request／handoff）時，送入 SDK 的固定框架說明：
轉送的使用者或同事要求若提到 VM、這台機器、你的電腦、sandbox，又未在訊息或工作脈絡指定
其他主機，指的是**收件助理自己的 Hosted Linux 沙盒**。明確指定的其他主機仍優先；
只用原可用工具／權限，不以這段說明取得新授權或要求不必要的外部 SSH 憑證。
原文仍留在 `Original message` 下，原來源、IDs、capture、模板與能力版本不變；
直接真人訊息及 Agent 結果／狀態通知不加這段框架。

### Verified against（VM 指涉）

Published base `78c1d5ecc58426fb499d01387bff10b4c472995b`，程式 WIP tree
`f79e04a8ceec36a33da5e2bbaa861e3fedd74d43` 的相干離線 40 PASS。hash 判讀、
原 RED 與 SDK 訊息接點見 [Shared Context](../src/fa-shared-context-poc/README.md#relay-vm-context)。
只驗送出的框架與原材料未改，不當真模型理解保證；未部署。

## 2026-10-04 HITL：Team 待處理發現與原回合來源（API CODE/offline）

`GET /native-hitl/v1/attention?cursor=<opaque>&limit=20` 由既有 Entra Team owner＋
`Behavior.Read` 讀取本人 Team 各 Agent 的待批准與 `ask_user` 提問，不依賴工作台已知 thread／RID。
Agent principal 不可用此列舉。Team 由已驗身份決定，不接受 body、team／agent／store／key selector。
同一 `FA_NATIVE_HITL_ENABLED` 開關，未開時不新增路由或查讀。

回 `NativeHitlTeamAttentionPage`，含 `team_id/items/next_cursor/observed_at`；
`items[].item` 完整重用 `NativeAttention` union，原批准／提問 ID、call、digest、期限保持。
`items[].origin={state,root,sender}` 只供定位：已驗原 NativeReceipt、同 Team root／sender 的
`NativeTurnAddress` 才標 `verified`，真人原回合的 sender 為 null。缺來源／原 reader 不可用時
標 `unavailable`、root／sender 為 null，仍顯示原卡，不從模型文字猜掛到 A。
不附訊息摘錄、effect output、checkpoint／system／reasoning，也不把來源連結當成批准。

列舉直接掃原 H1 Table 資料列的一頁，再沿原完整 H1 readback 篩去過期、已決定、已回答與
turn_closed；不新建表、index、pending、session 或回填舊記錄。`limit` 預設／最多 20，
是**實體資料列**頁大小，不是完整 Team 項目數。空過濾頁仍可能有 `next_cursor`；
讀到 null 才完成這輪發現，下一輪從頭刷新。跨頁不是原子快照，失敗不能當成空清單。
原來源查讀不入帳、不送出或執行接續；只沿現在版本的原 reader，必要時可能喚醒既有 VM。

（2026-10-04 修訂）每輪第一頁固定時間下限 `now − FA_NATIVE_HITL_TTL_SECONDS − 5 分鐘`，只掃 Table
伺服器 `Timestamp` 在下限之後的資料列；下限寫進 opaque cursor，續頁沿用。等待只在建立後 TTL 內有效，
下限之前的列都已過期，所以列出結果不變。限制：調低 TTL 時，以舊（較長）期限建立、尚未到期的等待
暫時不在 Team 清單（原回合照常），最長到它自己的期限。細節與官方依據見
[Shared Context](../src/fa-shared-context-poc/README.md#team-hitl-attention-window)。
（2026-10-10 修訂）下限改用兩種等待期限中較長的一個（`max(FA_NATIVE_HITL_TTL_SECONDS,
FA_NATIVE_HITL_INTERACTIVE_TTL_SECONDS)`），所以真人自己回合裡超過 600 秒的等待仍會列出。

UI 顯示位置可以是 A 原回合，但操作仍先 GET B 原 `pending/{id}`／`questions/{id}`，
再走原 `/decisions`／`answers`／reconcile，使用 B 原 command／ID／期限與確認。
批准仍須原 `Behavior.Approve`；回答是 owner 原 submit 權，不是操作批准。
例外（Neal 2026-10-11 核准）：Teams 面沒有委派 token。relay 身分在白名單內，
且平台依登入 token 設定的 `x-agent-user-id` 經 API 核對等於 Team 主人 oid 時，
這個 oid 即視為批准依據，不另核 `Behavior.Approve`。範圍僅限互動回合批准卡的 allow_once／deny，
只經 `POST /teams-relay/v1/decisions` 且 `FA_TEAMS_RELAY_ENABLED=1`；
排程（routine）批准卡、規則變更、提問回答與 Connect 決定仍只能在工作台。
不需要 UI 取得 B 的 Agent token，Team 面板只作總覽，完整卡片／DOM 由工作台切片整合。

### Verified against（Team 待處理 API）

Base `c5e062e4bbbb1494851fa600b56565069f28f1e3`；程式 WIP tree
`daaad38bd06d2fcd22297e35efb925cbd48bd37c`：相干離線 42 PASS，API-only cold／
實際 Page JSON Schema 產生 PASS。接口及原 RED 見
[Shared Context](../src/fa-shared-context-poc/README.md#team-hitl-attention)。
這是 API 切片，不宣稱 UI 已接線或 live 已驗；沒有部署或 release 設定修改。

## 2026-10-04 HITL：收件端未送出與效果未知分開（CODE/offline）

`send_agent_message` 的收件者解析／client／原回條前置若由 API 明確拒絕，
原 `NATIVE_RELAY_RECIPIENT_UNAVAILABLE` 必須穿過 HITL，不能因它是 `ToolFailure`
就改成 `NATIVE_HITL_EFFECT_UNKNOWN`。模型收到「沒有送出：對方目前無法接收，可能正在換版；
這次沒有新增投遞，可以稍後再送一次」；若同 ID 先前已有結果不明的嘗試，仍先核原回條，
不得換 ID 重送或把舊投遞說成從未發生。

只對上述工具及既有固定碼套用；其他工具、送出後失 ACK、真正效果不明都保持原 UNKNOWN。
若本次有批准卡，原等待收尾為 `interrupted`，不新標 `effect_unknown`；
原 action／grant／authorization 不退回、不移轉、不當成投遞成功，既有 UNKNOWN 也不覆寫。
未新增重試、接單、儲存或批准狀態。收件方傳訊前自動跟版經 #601 負責人確認屬另一接線範圍，
本片不修改 `NativeSessions.advance`、relay dispatch 或 release 設定。

### Verified against（未送出分類）

Base `110cb60f1e6614002daf6b9c67d3b4e45a5b62cb`；WIP tree
`fc4a5e5e486eb05c9b4f93c267018766d911dae2` 的相干離線回歸 23 PASS。
原 RED 與真正 signed API→relay client→tool→HITL→模型回饋接點見
[Shared Context](../src/fa-shared-context-poc/README.md#relay-not-sent)。
沒有重送 live 原呼叫、執行真模型或部署。

## 2026-10-05 HITL：桌面動作輸入前拒絕與效果未知分開（CODE/offline）

`desktop_act` 在送出任何輸入前就被桌面拒絕（畫面編號過期 `DESKTOP_OBSERVATION_STALE`、
桌面已暫停 `DESKTOP_HALTED`、參數不合 `DESKTOP_ACTION`）時，動作確定沒發生，原碼與桌面指引必須穿過 HITL，
不得改成 `NATIVE_HITL_EFFECT_UNKNOWN`。模型照各碼的原指引處理：畫面過期就重新觀察、再以新的 call 決定下一步；
桌面暫停就停止桌面工作並告知使用者；參數不合就照公布的 schema 重下。
有批准卡時原等待收尾為 `interrupted`；同一個 call 不再執行，effect 不標成功。
可能發生在輸入之後的失敗（`DESKTOP_ACTION_FAILED`、`DESKTOP_LIMIT`、`DESKTOP_OBSERVE_FAILED` 等）及其他工具的同碼
仍是效果未知、不重送。HITL 審查與真人接手（登入、密碼、驗證碼、付款、送出）不變。
同時移除桌面移動的 xdotool `--sync`：游標已在目標位置時它會等 15 秒、超過 10 秒指令上限而停下桌面。
根因證據與界線見 [Shared Context](../src/fa-shared-context-poc/README.md#desktop-act-hitl-unknown)；沒有部署或 release 設定修改。

### Verified against（桌面輸入前拒絕）

Base `999a7970ba6b46a2210e4eff1106aa5c1ee77518`。`test_native_hitl_desktop.py`（真 HITL API／JWT／NativeEffects＋
真桌面 toolbox，X display 為替身）涵蓋三個輸入前拒絕碼、輸入後失敗仍未知、其他工具同碼仍未知，原碼為 RED；
本機 Docker 真 Xvfb／openbox／xdotool 跑 `test_behavior_desktop.py` 整檔通過，原碼重現 `DESKTOP_ACTION_FAILED`。
這是本機證據，不是 Hosted 證據；沒有重送 live 原 RID、執行真模型或部署。

## 2026-10-04 系統政策自動接續（Neal確認；CODE/offline）

Neal原話 **10:22:31（台灣時間）**：
「這兩個功能應該是FA應該要幫user處理的? 使用者不需要處理?」；
**10:24:05**確認「對」。「這兩個功能」是stage的「核對原新環境」與「檢查不搬檔接續」，
時間由總協調按原session紀錄轉達。這是Neal決定，不再只是可推翻的協調提案。
此節取代下方不搬檔需一次真人確認、idle自動跟版不得省略檔案的限制。

- 正常工作台／routine用同一`NativeSessions.advance`政策入口；舊手動路由僅供相容／技術讀回。
  來源helper缺失、漂移或已明確拒絕安全搬檔時，server核原scope／檔案進度後自動不搬檔；
  不能由UI idle、模型文字或客戶端自填理由擴權。
- 自動決定ID固定於policy／Agent／source SID／target SID／target version；不因重試、頁數、routine occurrence重造。
  新`_Omission.decision_basis=system_policy`及回條`files_omission_basis/files_omission_reason`明示來源。
  舊人工決定維持原ID、原理由及原body hash，不補寫成系統決定；只能依同scope原件續做。
- 原follow或決定UNKNOWN先只讀核回；只追加結果，原UNKNOWN不覆寫。
  核對只讀目的端／原staging及N1／metadata；unknown PUT只GET原target/path，發布不明只核原發布，
  未核清不能繼續拷貝、換ID、重送訊息或假裝空目錄。
- accepted／started、接單、原task／通知未結時回正常pending並自動等待，不強制轉unknown。
  HITL／question原卡片保持，不自動答覆或批准。壞checkpoint及仍未知的檔案效果停止給人處理。
- 每批2秒、含首次最多10次。到上限不當失敗／UNKNOWN；唯讀`progress_token`依原掃描位置、
  原等待RID、task及來源metadata version/status變化產生，不採用觀察時間或任意ETag作進度。
  stop已送但來源稍後才idle時，唯讀metadata即可喚醒原接續，不因record本身未變而卡住。
  唯讀進度確認有變化才續下一批同body／ID；無進度顯示「等待原工作結束中」，不新增倒數。
- 送出前、頁面載入自動處理。只有原確未POST的發送意圖，在ready及Agent/CID/login/observer／
  draft編輯與附件重新核對後，走原once送一次；載入畫面本身不授權送出從未按送出的草稿。
- 第一次一般換版的合法metadata `needs_follow`也須啟動，不以有草稿、舊follow或非空錯誤碼為前提。
  頁面接續ready後再唯讀原thread進度，server確認可續聊才開放輸入；核對失敗須顯示原因，
  保留server允許的新對話／技術核對入口，不清除原UNKNOWN或用空白歷史解鎖。
  空白新對話與只有舊ready的首次載入也先純讀目標；`unverified`不等於可換版。
  後來的回覆／目標觀察使舊讀回失效，不能由晚到ready蓋掉新UNKNOWN或已讀原回覆。
- 正常畫面不顯示兩個人工環境按鈕；未解的真正UNKNOWN才在「環境詳細」保留技術控制。
  ready後唯一通知為「已換到新版環境；這次沒有搬工作檔，舊環境與檔案都保留。」
  新bridge固定宣告policy識別，舊bridge沒有此契約時仍沿原相容UI，不由新HTML猜server能力。
- routines先保原occurrence intent，再跟版；environment_pending不是N1接單回條。
  producer每批最多10次，唯讀原進度有變化才續下一批；無變化保留due reference，交既有下一次clock tick，
  不能重造routine RID或冒Team owner。此接線不啟用原本default-off的routine flags／Job／IAM。
- 跨目的的原確認在retarget分頁未完成時保pending，不以尚未選定目的為`NATIVE_FILES_CONFLICT`。
  `files_confirmation_pending`是「尚未生效」而非「保證confirm未到達」；原內容與retarget intent可已保存，
  但不可帶`files_migrated`或已生效決定。API核同confirm ID／follow／來源／目的後才接原進度，
  真正選定目的後才保存省略決定；不丟棄舊staging，也不允許另一retarget或未知檔案效果。

### Verified against（自動接續）

基準`55ba2829`；真API/N1、bridge once、共用DOM與routine producer接點。
legacy／drift自動ready、原決定失ACK、working-task進度、HITL卡片與unknown PUT、原草稿附件一次送出皆有離線反例。
官方固定SDK仍負責session／Files／task儲存；FA只改既有接點與系統政策，不包第二個agent loop。
詳見[Shared README](../src/fa-shared-context-poc/README.md#automatic-environment-policy)；未部署／live。

## 2026-10-04 HITL：批准使用摩擦（CODE/offline，未部署）

- native 回合提供 `read_tool_output`，只讀該 SDK session 的 `/session-state/temp` 目錄及內容，
  使用既有 `SdkSessionFiles`／SDK `SessionFsProvider`，不跑 shell、不讀 VM 其他檔案、
  checkpoint 或 system。`path` 缺省為 temp 目錄，`offset/limit` 以字元計，
  預設 8000／最多 16000，`next_offset` 接續原文；不是新的 effect／批准或跨 session reader。
  shell 即使用來讀同一輸出，仍走原 Team 規則，不因這個工具而放寬。
- WebIQ 原卡由總協調讀回為 `matched_rule_ids=[]`、`source=uncertain`、`reason=review_uncertain`，
  不是 shell 規則命中過寬。只釐清 Auto Review 指引：無命中、MCP 身分或前文的無關拒絕本身
  不代表不確定；明確要求的唯讀搜尋若無適用 Ask 規則／具體阻擋，可允許。
  真正的目標／後果不明仍 Ask，takeover／Ask-first 優先與既有規則皆不改。
- `NATIVE_HITL_AUTHORITY_UNAVAILABLE` 的回饋只限制這一次呼叫，並明說使用者之後的新請求
  可依當時權限重新嘗試；原 UNKNOWN 效果仍先核對，不可換 ID 或把同一未知操作當新請求重做。
  原錯誤碼、grant、狀態、retry／poll／TTL 與持久紀錄不變。

### Verified against（批准使用摩擦）

Base `b9a1bae1b087a311500fd654437bd8309b2e4e92`；程式 WIP tree
`750ec0dce73965615e4d4f4b6ddaa67291b3ffe7` 的範圍內 32 PASS、真 SDK 兩協定 2 PASS、
API-only cold PASS。原生 `view` 未讀到虛擬檔的失敗對照與官方缺口見
[Shared Context](../src/fa-shared-context-poc/README.md#hitl-批准使用摩擦codeoffline未部署)。
審查提示的離線契約不等於真模型品質驗收；未部署、未重跑 stage 或修改 release 設定。

## 2026-10-04 已確認來源漂移：保留 staging、不搬檔接續（CODE/offline）

此修訂僅擴充真人不搬檔的合格條件，不實作selector-last的正常搬檔新流程。
舊「任何manifest都拒絕省略」改為：**來源metadata已證版本不同，且沒有未知PUT或發布時，
可由真人選擇保留原staging而不發布**；不是默默丟檔或把manifest改成空。

- 原environment-reconcile只核目的端／原staging及來源metadata；不invoke／Files讀來源，不auto-follow。
  已知來源漂移回`pending/source_version_changed`，不是正常可重試的files_pending。
  工作台保留UNKNOWN／草稿與附件，隱藏正常續搬、提供server不搬檔預檢。
- `missing=source_changed`僅由API核原source SID／紀錄版本與metadata產生，不是外部owner body選項。
  檔案進度`sending`非空，或`publishing/published`為true時仍拒絕；原active/admission/task、
  HITL／提問與checkpoint核對不省略。來源查不到／403不等於已證漂移。
- 原確認create-only／digest與ETag CAS不變；`_Omission.original`保存完整原Files材料，
  following保留同一份原manifest及進度。ready只標`files_migrated=false`，不發布staging、不刪來源或舊檔。
  未啟用確認的readback可帶原manifest參照，但不得宣稱已遷移；確認缺件只代表缺件，後續仍核全部安全條件。
- 同target沿原follow／新確認；新target必須新真人確認及原retarget，舊確認／UNKNOWN不改。
  新reader仍獨立核source metadata，雙端漂移時已證source_changed優先，不以original_destination遮住合格staging出口。
  此優先序只選新確認的理由；已存未啟用確認先read/check讀回原`files_confirmation_reason`，
  核該理由仍成立後沿原分類啟用。同ID/body不因後來另一個理由也成立而重分類；原理由不再成立則拒絕並保原件。
  正常Files階段先核source版本，不能對已知漂移的來源再次搬檔。UI仍薄client、2秒／10次窗口及原once不變。

### Verified against（t3出口）

基準`e47f4f5e`；`NativeSessions.observe_environment/without_files`、`FollowFiles`、
`native_files_omission`、`_Following`與共用工作台。真API/N1離線重現、兩lane及原journal／DOM
見[Shared README](../src/fa-shared-context-poc/README.md#source-drift-staging-exit)。
dev419e的source_stopped失敗仍為原失敗；未部署／live，不將安全出口當ADR-0003保檔完成。

## 2026-10-04 頁內續做與平台頁界游標（CODE/offline）

本修訂取代下方「最後完成項目的opaque cursor可直接作after」的頁中讓出規則。
dev `2c0cbb2b` A已有HTTP400 `bad_request`／63ms讀回，但舊log未區分cursor來源；
本次修正已離線重現的中頁entry.id問題，不將它先定為現場唯一根因，也不做live對照。

- `after`只能是None或平台先前回應的`page.last_id`，原字串交固定SDK編碼。
  頁中耗盡10秒預算時，原`check_progress`保存取這頁的after、已完整處理項數及有序頁digest。
  下一次同after重讀該頁，先核對頁的id/key順序、last_id、has_more及大小，再略過已核前綴；
  metadata也計入位置。只有整頁完成才能前進到服務last_id，不從entry.id／key重建游標。
- 進度key先保存／核回再讀首頁，避免自己的新metadata改變desc首頁；頁digest不含可變ETag。
  同頁新增／移序／頁界不同時明確409、`files_check.category=page_changed`停止，
  不跳過未處理項、不當成terminal或正常scan_pending。
  初次啟用不搬檔的已知確認metadata寫入會改變原頁；只重設mutable following的after／offset／digest，
  原immutable確認保留舊partial快照並從頭重核。已啟用的同ID續做不再次重設，未知頁變動仍拒絕。
- 同scope／Agent ETag保存頁內進度；讀失敗不越過失敗RID。
  首次本地list deadline的一次有界同頁重讀仍保原offset/digest；backend錯誤仍503停止。
  `cursor_source=none/page_last_id`讓list失敗能辨識實際送出的cursor來源，不輸出原cursor或key。
- 新保存進度以`cursor_profile=page-last-id-v1`區分。舊check／follow沒有來源標記的after不再使用，
  續做時從頭重核；不重寫原確認、UNKNOWN、immutable following快照或工作結果，不換RID/body。
  一般follow／retarget也保存原頁after、頁內位置與digest；仍16-key／10秒。
  進入confirming時另從頭掃，之後的slice續同一次掃描，慢頁不能永遠重讀同一前綴。
- 不搬檔check仍每次最多100-key一頁，read期限、UI每2秒／含首次10次與真人確認規則不變。
  accepted／started、admission／task、HITL／提問、checkpoint與Files未知都不因分頁調整而放行。
  不採「最新100筆就是沒有未決工作」，也不另建API工作權威。

### Verified against（頁界與舊進度）

程式基準`2c0cbb2b`及本片diff。真API／N1、具名storage transport核千筆多次頁內讓出、
同頁重讀／完整頁才用last_id、頁變動停止、metadata與舊進度；13個頁界案例含確認後draining／confirming
慢16筆頁、確認metadata加入原partial頁及已存未啟用的同ID續做。既有頁timeout、原確認及兩lane回歸保留。
固定SDK契約、程式位置與限制見[Shared README](../src/fa-shared-context-poc/README.md#receipt-page-boundaries)。
本機不是dev／stage驗收；未部署或live讀取，PG原稿未送出，下一步由總協調採新main在A核一次。

## 2026-10-04 receipt 列頁拒絕：保留安全的狀態與錯誤碼（CODE/offline）

總協調回報dev `592cd87e`：第二個list的`failure_kind=backend/read_elapsed_ms=61`。
這能定位本次列頁await失敗，不能從舊診斷判定實際HTTP狀態、錯誤碼、游標不合法或平台限制。
本修補僅將固定core 2.2.0 `FoundryStorageError.status_code`及`response_body.error.code`
投影到`files_check.backend_status/backend_code`；HTTP狀態限整數100–599，code只接受固定Literal清單，
不在清單或原SDK未保留body時記`UNCLASSIFIED`，不傳message／param／innererror／key／內容。
非該SDK服務錯誤不捏造HTTP狀態；既有deadline/backend分類、重讀上限、確認與游標語意不變。
Hosted→API log→bridge沿同DTO嚴格驗證，不能以新欄位自動重試、跳過錯誤頁或擴權。

官方state-store文件明定keys per page為1–100、預設20；Python方法接受`after`與`order=desc`。
固定生成的page模型按`created_at`排序、`last_id`是data最後項目的id，不是key的字典序最小值。
完整頁下一次帶原`last_id`；SDK以`quote(value, safe="")`做一次UTF-8百分比編碼，
caller不預先quote／unquote／替換`:`、`/`或`%`。不改用`before`、不另造Table分頁器。
這些是官方契約與離線請求證據，不等於原A頁面已恢復；舊5b92兩次缺stage的錯誤仍無法追認同因。

外部探針已由總協調取消：preview state store僅供Hosted agents，API MI／真人結果不能冒充A原路徑；
不執行外部list、不偽造Agent/call-ID header、不新增IAM。由fdbb採同build後，在A原入口取得新診斷，
再決定產品修正或整理PG問題，不先把猜測當根因。

### Verified against（列頁錯誤診斷）

基準`592cd87e`；7個固定SDK HTTP錯誤反例先RED，修後allowlist／遮蔽非法code通過；
三組兩頁HTTP請求鎖`last_id → after`，含`+/=:%`、已含百分比文字及Unicode，不雙重編碼。
API log／bridge與舊wire相干35案PASS，API-only冷匯入阻擋Hosted SDK通過。
官方來源與固定程式位置見[Shared README](../src/fa-shared-context-poc/README.md#receipt-page-rejection)。

## 2026-10-04 單頁 receipt list 與原確認冪等續做（CODE/offline）

固定core 2.2.0的`list_keys`本來就只發一個`items:keys`頁面請求，返回`keys/last_id/has_more`；
不是Table SDK的自動全量迭代。N1 store是同project／Agent的`fa/v1/turns`，查詢為
`limit`、opaque `after`與`order=desc`，`rid:`在FA讀回後才篩選；不臆造PartitionKey／RowKey filter、
底層Table continuation token或補寫舊tags。SDK HTTP管線可能重試同一頁，沒有自動換after掃下一頁。

不搬檔預檢每次只取最多100個key的一頁，完整RID之間仍用原10秒讓出預算；
每個list await仍限5秒，位置存原`check_progress.after`。一般follow仍原16-key掃描方式。
只有本地單頁list deadline，才保存未前進的原cursor並允許下一次同scope重讀一次；
返回仍是scan_pending，附`failure_kind=deadline/read_attempt=1`，不宣稱讀成功。
同cursor第二次deadline回503／attempt2；其他服務／傳輸失敗、403或進度寫入未知不走這個讀取重試。
marker持久保存，重啟或手動點擊不能把同頁失敗當作新頁；成功頁後才清除該次失敗標記。
1000 keys／100每頁若另有一次逾時，首個10次UI窗口可能還沒讀完，需真人再續原窗口之外的剩餘頁，
不能為了測試完成而把自動上限偷偷加到11次。

新診斷另列`failure_kind`及邏輯讀取的`read_elapsed_ms`；整個invocation elapsed含前後其他讀取，
不能當成單頁時間。舊5b92兩次缺stage的15.5／15.9秒，仍不能由新診斷追認同一列頁原因。

### 原確認 ID／內容與 UNKNOWN（總協調本次明確批准）

此段取代「不搬檔確認UNKNOWN永不再送」的舊規則，**只適用這個已有人類同意的確認控制**，
不適用原訊息、模型工作、工具效果或檔案PUT。人工按核對時先讀原確認：
遠端已存就沿用；查不到在同scope以**原確認ID及原body**create-only續做；晚到同ID只生效一次。
新的`files-confirmation:<ID digest>`在原N1 store綁定canonical command digest與原follow／雙SID版本，
不同內容拒絕。原`_Omission`仍create-only、保完整原following快照；既有舊原件不回填或覆寫。
已存未啟用的同ID確認可完成原CAS，已active／ready不重建；不建立新操作來抹掉舊未知結果。

`without-files/reconcile`因此是明確owner控制POST，使用submit權限，不再只當純read；
一般environment GET／純觀察仍不派送。bridge只新增`files-omission-result-*`結果紀錄，
原UNKNOWN原件不改；新的CHECKING進度仍用原ID/body，有界每2秒／10次，失敗結果另存、不可假稱成功。
同IDdigest／scope不符、未決Files／active／task／checkpoint仍拒絕；不拿人類同意解除資料安全檢查。

**目標版本不外推。** 舊103確認只能用在103；selected變104須新目標、新真人確認、新確認ID。
舊目標UNKNOWN不可擋新目標的預檢，但仍保留舊確認原件，沿既有retarget/CAS防晚到舊writer，
不把舊body偷偷套到新版目標。
新reader讀回舊操作若仍pending，才走原pending-retarget；若其實已ready（只有ACK丟失），
新的follow以已生效的目前SID／版本為來源，不再把更早來源當成待遷移對象。
新的確認及後續同ID核回都保持這個source／follow身份，舊UNKNOWN與ready原件不改。

### Verified against（單頁／確認重入）

基準`6aa86125`；固定SDK request-builder測試證明每次GET只有limit/after/order與一頁回應；
1000個N1 key、單頁timeout、同cursor重讀一次、再次失敗停止及最後ready都有相干離線反例。
原確認查無／已存／不同digest／晚到並行、bridge UNKNOWN只追加結果、新target不被舊UNKNOWN封死，
分別經真API/N1／journal與共用DOM核對。這不證明舊live頁面的後端耗時原因，也不是live驗收。

## 2026-10-04 大量歷史：完整讀取單位與確認後有界續做（CODE/offline）

本修訂處理#580後dev A的下一個阻擋：metadata-only預覽先取得真人同意，
真正讀原歷史時才回scan_pending；以及單筆checkpoint跨過整段掃描期限而反覆從頭的問題。
原15.5／15.9秒的`NATIVE_STATE_STORE_UNKNOWN`沒有key類別，不能斷言是某筆Table讀取或平台逾時；
本修訂以確定可重現的讀取期限交互修正及安全診斷回答，不將離線結果外推為已解釋舊live根因。

- 一般follow仍沿原16-key／10秒截止。不搬檔的完整檢查改在**完整RID讀取單位之間**檢查10秒預算，
  不在讀到該RID的meta／turn／checkpoint一半時取消並重做同前綴；目前單位可超過剩餘預算後再讓出。
  各邏輯儲存讀取另有5秒期限，checkpoint按每個segment套用；單次失敗或超時仍503，不當成已讀完。
  同一conversation store只開啟一次並沿原helper核meta／turn／完整checkpoint，不減少hash、shape或codec核對。
  外層drain收到期限已到的scan_pending也必須立即返回，不能繼續列下一頁或逐key列頁。
- 若讀取失敗前已有完整RID，先以原scope／Agent ETag保存最後完成的opaque cursor，再明確回原錯誤。
  失敗的RID不前進、不跳過；進度保存或原Agent比較不明也不能改成成功。
- `files_check.category=storage_read_failed`補安全`stage`及`key_category`：
  開啟／列頁／讀取／關閉，以及Agent、following、確認、檢查進度、receipt、conversation、turn、checkpoint。
  僅傳白名單與RID／CID短碼，不傳完整key、endpoint、憑證、例外訊息或私人內容。
- 已有真人同意但原掃描尚未完成，API回`pending/scan_pending`及精確
  `files_confirmation_pending=<confirmation RID>`，**不發Hosted confirm**，不是省略決定已保存。
  bridge以原一次journal記`CHECKING`、原body/RID；下次仍沿同命令，不產生after-refusal新ID。
  每次送出前先回UNKNOWN，只有server再次確認未dispatch的正常進度才能回CHECKING；
  一旦confirm已發送／回應未知便停在UNKNOWN，只核原件，不自動重送。
- UI從preview到已同意後的CHECKING共用每2秒、含首次最多10次的窗口，原警告只問一次。
  已CHECKING的下次真人續做也沿原同意及RID，目標／Agent／CID／登入／草稿變化就停止。
  每次timer醒來、POST之前重核最新CHECKING／follow／target／確認RID，不只在回應後比對；
  最新已是UNKNOWN或完成狀態時不再發送。
  若confirm已保存、後續follow尚在掃描，只續**原follow**，不再POST確認；仍共享剩餘次數。

### Verified against（大量歷史）

基準`5b92a518`；300個具名儲存替身的N1 RID（歷史completed與unknown兩組）走真API／runtime，
所有RID實際讀取、單次read失敗保游標、完成checkpoint後到不搬檔ready；原bytes與once不改。
原期限中斷重做及缺少stage/key診斷先RED，確認後UI無timer、bridge無可續狀態也先RED。
真503仍停止、不假稱不存在；舊live的精確key要等新診斷讀回。固定SDK版本／checkpoint bytes、
metadata-only GET、原active／HITL／question及Files未知阻擋不變，沒有部署或新增權限。

## 2026-10-04 Native routines／reminders（CODE/offline，預設關閉）

總協調核准短計畫`20c11b36…`的S1／S3與S2程式；新增資源／IAM、部署及S4 live未批准。
此節取代本功能「尚無native來源／只留#259 building block」的描述，不變更其他已延後工作或原76項驗收定義。

- 採官方Container Apps Scheduled Job作薄時鐘的方向，FA不另建timer／cron loop／lease。
  固定SDK下Foundry Routines preview不能直接表達FA原N1握手，差額與官方來源見[實作說明](../src/fa-shared-context-poc/README.md#native-routines)。
- owner的定義版本、Agent／CID、UTC occurrence及內容digest分開保存；改版核原version，舊ID不反向啟用新head。
  機器只送既有routine／排定時刻，不冒human principal，不帶任意新指令／批准。
- 先存原派送意圖，再沿N1及原Responses／Invocations入口；scope＋routine＋排定UTC決定根RID。
  並行／失ACK只核原件，started／UNKNOWN／已完成不換ID重跑，④仍保原UNKNOWN並保留routine來源。
- 依ADR-0006只在新派送、SDK回合開頭與④入口核版本；已進入SDK的回合不逐步回API核權。
  中途停止沿既有N1取消／SDK abort；本片不新增ADR例外或回合內授權gate。
- `kind=reminder`只讀原未結工作，無SDK dispatch。已取消／完成的責任不靠提醒復活；原問答與批准仍走既有HITL原件。
  讀不到原件就是未確認，不以問號、模型摘要或progress猜待批／完成。
- 單一DST異常或缺N1回條以`issues`逐筆明示，不讓整頁提醒或其他Agent停擺；缺原件不能當成可換ID重送。
  owner原歷史仍可讀取消的routine，但Agent reader／SDK不得把它重新注入為未完成工作。
- PA-09的排程來源與state是資料；人類長文guide按需讀、只綁當次RID。PA-10 reader只讀明確相關原工作、效果及原attention，
  不移用once；metadata、已接單、Job成功或畫面呈現都不等於交付。
- API／Hosted／工作台`FA_NATIVE_ROUTINES_ENABLED`及producer旗標皆預設關；沒有新增IAM或部署觸發。
  更多格式、任意cron UI及對外通知不在本片；M類追問／接續品質仍待live。

### Verified against（native routines）

`a229be94`、原SDK與N1接點、`native_routine_*`／`native_routines`／`native_work_context`及原attention工作台；
具名離線測試與真固定SDK的模型HTTP替身結果，見[評測](agent-runtime-evaluation-baseline.md#native-routines-evaluation)。

## 2026-10-07 Native routines：API 兼任排程時鐘（CODE/offline，預設關閉）

總協調依 Neal 10-07 16:41「為什麼需要新的雲端資源呢?」改選方案 C。本節取代上一節「採 Container Apps Scheduled Job 作薄時鐘、FA 不另建 timer」的方向；其他契約不變。

- API 常駐單副本，由 lifespan 每分鐘跑一次既有 `due`／`trigger`；授權是程序內時鐘，不新增 HTTP 入口、身分或雲端資源。
- 不用 Foundry Routines：creator 身分只收真人委派 token（dev P1 實測，和官方文件不符），agent 身分又叫不到 API 建立的 session。
- 補送只限 #581 原規則允許的「最近一次、晚於批准」的 occurrence。固定 RID＋CAS 意圖＋N1 `rid:<RID>` 讓重啟與新舊 revision 重疊都不會多一個回合。
- 旗標 `FA_NATIVE_ROUTINE_CLOCK=api` 預設關。實作、證據與限制見 [README](../src/fa-shared-context-poc/README.md#native-routine-api-clock)、[ADR-0006 補充](adr/0006-team-trust-boundary-grokbot-mode.md#api-routine-clock)。
- 10-07 dev 驗證全部 PASS 後，Job 路徑（`native_routine_tick`、producer 授權與 `/due`、`/trigger`、`/environment-observe`）已移除；到期 occurrence 只由 API 程序內時鐘送出。

## 2026-10-07 從對話建立排程：助理提出、主人在批准卡上允許（CODE/offline，預設關閉）

總協調核准計畫（10-07）。Neal 原本在對話裡要求「每天幫我檢查是否有新文章」，工作台沒有入口；本節新增入口，不改上一節的時鐘與 #581 契約。

- 助理只能「提出」：Hosted 的 SDK 自訂工具 `propose_routine(local_time, time_zone, content)`，只在真人回合、旗標與 HITL 都開時註冊。
- 寫入只發生在主人的 `/native-hitl/v1/decisions` 請求：HITL 類別 `routine` 一律出卡、沒有 Auto Review 或 Team 規則、沒有「以後允許這類」；
  API 先以主人身分寫該卡草稿的每日 definition（ID 由卡片決定，重送讀回同一份），成功後才記決定。模型輸出或對話回覆都不算批准（AGENTS 第 6 條）。
- 批准排程不是批准每次的操作：每次執行的工具呼叫照一般 HITL。停用在工作台「助理清單右側的 ⚙ → 排程」（2026-10-08 前是「設定 → 排程」），寫停用的新版本並核目前版本；不做編輯、刪除或重新啟用。
- 旗標 `FA_NATIVE_ROUTINE_PROPOSALS=1` 預設關；API 另需 `FA_NATIVE_ROUTINES_ENABLED=1`、`FA_NATIVE_ROUTINE_CLOCK=api`，Hosted 另需 `FA_NATIVE_ROUTINES_ENABLED=1`、`FA_NATIVE_HITL_ENABLED=1`。
  細節與證據見 [README](../src/fa-shared-context-poc/README.md#native-routine-proposals)、[工作台 README](../src/fa-shared-context-poc/web_test_console/README.md#routine-from-chat)。
- 10-08 修正：真人與排程回合給同一份工具清單（固定 runtime 1.0.83 對前面用過、後來移除的工具會告訴模型「不可用」，加回時不會說已恢復）；
  非真人回合呼叫 `propose_routine` 在出卡前就拒絕。見 [README](../src/fa-shared-context-poc/README.md#native-routine-same-tools)。

## 2026-10-04 不搬檔預檢：歷史 UNKNOWN 與分頁進度分開（CODE/offline）

本修訂依總協調明確要求，取代下方不搬檔流程「所有N1 UNKNOWN一律阻擋」的舊規則。
`receipt.status=unknown`是已結束、結果不明的原工作；只保留原件、不重送，也不把它改成completed。
它本身不阻擋真人不搬檔或新正常工作。8765 submit／fetch／environment／reset journal的UNKNOWN
是各次HTTP觀察，不能拿其筆數推論N1有幾筆active或假造某個RID。

仍在`accepted/started`、admission未核清、目前執行鎖／official task或通知仍活躍時，繼續擋住；
等待批准／提問的原回合仍由其started／active狀態保護。已完成對話的目前checkpoint仍須核對；
未知回合不在環境跟版時補造結果或checkpoint，後續使用該對話仍走原恢復契約。
已有manifest／sending／publishing、確認失ACK或原Agent比較不符仍不允許省略。
terminal UNKNOWN的例外不適用Files ACK、接單或真人確認本身的UNKNOWN。

預檢沿固定core 2.2.0原`list_keys(limit=16)`、10秒截止；截止是`scan_pending`，不是「有未決工作」。
同確認RID／follow／雙SID與版本的游標只存於原N1 `turns` store的`files-check:<scope digest>`，
綁原Agent ETag；不改原RID、following意圖、批准或checkpoint，也不是另一個執行權威。
只在完整處理一個entry後保存SDK的opaque `entry.id`位置，不能把`entry.key`當cursor；
失ACK讀同key核原進度，不能信UI傳入的cursor或改ID跳過工作。綁定變動要重新核對。
scan完成也不是ready：確認前仍核admission／execution／task及原CAS；不因快取讀完就放行新工作。

工作台只對原preview的`scan_pending`，以相同body／同RID每2秒續做、含首次最多10次。
掃完前不出確認視窗、不發confirmation／follow／訊息；上限、錯誤、登入／Agent／CID／草稿／
目標或原RID改變就停。真人可稍後再檢查同次操作，server沿持久游標續讀，沒有無限背景POST。
只有預檢核齊才詢問原不搬檔警告；metadata-only替代reader預覽仍明示原工作尚未核。
已保存但未啟用的確認也先沿同RID做有界唯讀預檢；核齊後只啟用原確認，不再問一次或寫新確認。
check完成時回本次核對結果（pending／reason=null），不沿用原follow保存的舊scan_pending；
原following原件不改。目的reader已存在且版本匹配時，已核回的保存確認必須到該reader做實際check，
不能因更早目的漂移又回metadata-only，擋住原確認的啟用。

`NativeFilesCheckDetail`只帶白名單類別，以及可取得的RID／CID前8碼；不附原內容／私有推理。
`active_waiting`、`admission_pending`、`task_active/unconfirmed/access_denied`、
`execution_active`、`checkpoint_unconfirmed`區分實際阻擋；`scan_pending`只表示讀取進度，
其中RID若存在是讀到的位置，不是宣稱那筆active。沒有RID就明示未取得，不能猜。
該detail經Hosted→API→bridge嚴格型別驗證，API診斷log及畫面沿同一投影，不輸出任意upstream文字。

### Verified against（歷史 UNKNOWN／bounded check）

基準`bd3c4ba8`；真API兩lane重現terminal UNKNOWN預檢409後修正，舊receipt bytes不改且新CID可送。
真N1 store的多頁／實際async截止核opaque cursor續讀，active頁未略過；bridge／DOM核同body10次、
scan尚未齊不confirm、scope改變就停與原RID短碼顯示。原metadata-only、once及失ACK路徑保持。
dev原check只有API的`NATIVE_FILES_CONFIRMATION_REQUIRED`及10.899秒，缺Hosted內部分類；
無法判定該次精確RID或斷言必為掃描截止，原FAIL不回填PASS，部署及現場重測仍交總協調。

## 原 follow UNKNOWN 遇到目的漂移：明確另接新版（CODE/offline）

此修正處理總協調於2026-10-03回報的stage A：原follow47未確認、目的SID已是51；
`reconcile`不能再以原SID呼`session_read`。metadata確認目的版不同即回版本拒絕，不invoke、
不把原UNKNOWN改成成功。沒有observation或UI曾逾時，仍可開**預檢**，不是自動跟版或重送。

`without-files/preview`沿既有路徑：新版reader尚未存在時，只回
`fa-native-environment-replacement-preview-v1`／`confirmation_required`，
明列`original_work_checked=false`。`destination_version_changed`代表metadata已證漂移；
`reader_required`只表示需新版reader，不宣稱來源失效或已排空。preview不create／invoke／寫確認。
警告保留「這次不會搬工作檔；舊環境的檔案不會出現在新環境」，另說明原工作尚待核。

真人確認後才允許準備目前選版的reader，讀同Agent的原FA紀錄及平台metadata。
必須核對原pending RID／SID／版本、無manifest／sending／publish、admission及
accepted／started／UNKNOWN／task／最新checkpoint；HITL／提問仍由原未決狀態擋住。
只有原來源不可用或原目的確實漂移才可省略；來源及目的helper都可用時仍拒絕略過，
`reader_required`本身不是省略權限。原目的漂移以內部`missing=original_destination`區分，
不能把新reader的helper可用誤判為「原目的仍可用」。

通過後沿既有retarget產生穩定的新follow RID，保留原UNKNOWN、回條／intent／required-files內容；
舊following只加既有`retarget_request_id`防晚到寫入關聯，不冒稱舊操作ready。
新ready仍為`files_migrated=false`、帶原確認ID；原訊息不送，④缺manifest仍不得自動跨SID接續。
確認失ACK只核該確認，不再POST；原token／Agent／CID／登入世代、草稿及once journal保持。
真正發送確認前，API另以同一原scope做唯讀`check`。只有API能確定本次confirm尚未dispatch的
明確409拒絕，才回`confirmation_not_dispatched=true`與精確確認RID；不從任意上游409推論。
bridge只接受確認POST的相同RID及已知拒絕碼，將**本次新確認**記REFUSED，保留原follow／UNKNOWN；
下一次真人預檢／確認派生新確認ID並關聯前次拒絕，不重送舊ID。未帶有效證明、403／503、
真正confirm已dispatch或失ACK仍UNKNOWN，只能核原件，不自動降級。
stage部署仍等總協調發布版本，此修正不改release.json、不增加IAM或live操作。

### Verified against（目的漂移）

基準`69c65f38`；真API／N1／固定SDK離線transport與共用DOM先重現
「metadata已漂移卻invoke原目的」「UNKNOWN無observation入口消失」及「缺新reader而預檢503」。
直接程式為`NativeSessions.observe_environment/without_files`、`NativeSessionFilesOmission`、
`NativeAdapter.without_files`及共用HTML；原State store／retarget／Files發布機制未重做。
來源及目的分別漂移、兩端仍可用、未決工作／檔案、原UNKNOWN journal及lost ACK均有相干測試。
官方與實測差異、暫停後搬檔限制見[Shared README](../src/fa-shared-context-poc/README.md#suspended-session-file-gap)，
不把新reader的離線成功當成stage驗收。

## 2026-10-03 舊來源版本漂移：資格判斷不 invoke 來源（CODE/offline）

此修正處理 stage `b2ff46ba` 在來源 `session_read` 回409，尚未到不搬檔確認的FAIL。
原follow及不搬檔preview不得為了探測helper去invoke舊SID：只讀目的reader的FA資料及平台metadata。
新bootstrap／ready才保存server產生的 `files_profile`；pending帶 `previous_files_profile`，
綁同一來源SID／歷史版本，舊資料不回填。沒有聲明或metadata已漂移時，
不猜helper可用、不呼來源，回明確不可遷移，交原真人不搬檔確認；同意不等於自動送原稿。

正常已保存helper聲明、metadata同版的來源，仍可在原scope取得必要的檔型／mode manifest；
其invoke的期望版本必須是來源自己的版本，不是新selector目的版。
初始inspect若回 `NATIVE_SESSION_CHANGED` 或平台409，沿原following及Agent CAS記
`source_invocation_refused=true`；後續不再invoke該來源，省略preview只核原紀錄。
兩筆CAS只成功一筆時，新reader的`NativeSessionState.source_invocation_refused`唯讀合併同操作Agent／following觀察，
不改各SID原`following`回條；retarget含已存intent重入須承接最新拒絕，不重寫immutable intent。
已有manifest／未決Files不得利用此狀態丟棄。來源資格判斷與不搬檔路徑均不invoke舊SID。
FA原accepted／started／admission保留並擋住follow，不為manual Files切換把它們收成UNKNOWN。

檔案bytes原本即走平台Session Files GET/PUT，而非invocation；不能用listing的
`is_directory=false`代替regular/link/mode證據。正常有證據的helper路徑與明確不搬檔分開，
不新增Blob、IAM、per-invoke version override或fallback SDK。
診斷只新增安全格式的上游error code、實際operation/stage、期望版與Hosted實際版，
不記response body/message、token或私人內容；原409不能被推成平台全面禁止舊版SID。
官方版本／Files界線與直接測試見[Shared README](../src/fa-shared-context-poc/README.md#source-version-read-only)。

## 2026-10-03 Hosted③補充：真人確認不搬檔接續（CODE/offline）

本修訂只放寬下文「舊 image 無 helper 就永遠無法接續」的首次換版情境；
正常有 helper 時仍必須搬檔，`idle_only` 不得自動選擇不搬檔。
缺 helper 必須由 server 讀原 source／destination 的能力確認，不以檔案缺失、403、timeout、
壞 checkpoint 或未知傳送代替。原整個 Agent 的 admission、RID、task、HITL／提問仍須排空；
不搬檔路徑另拒絕 UNKNOWN，並核每段對話目前的合法 checkpoint，不清原回條來騰空。

使用者先按「檢查不搬檔接續」，server 預檢成功才出現一次確認：
**「這次不會搬工作檔；舊環境的檔案不會出現在新環境」**。
明確確認固定 Agent、來源 SID、目的版本及原 follow；原檔案、原對話、原批准／UNKNOWN 全部保留。
確認不是新訊息；ready 後也不重送原 RID，使用者的新對話或新追問仍走原 once／新 RID 接點。

API 新增原 environment 下的 `without-files/preview`、`without-files`、`without-files/reconcile`：
preview／reconcile 只核原件，不能 create／stop／寫確認；只有 owner 明確 POST 才能記錄決定。
Hosted 原 Invocations 增加 `session_files_omission`，不新增模型工具或其他儲存。
以原 State store 不可變 `files-omission:<digest>` 保存確認及原 `_Following` 完整快照，
再以原 following 的 ETag CAS 生效並封住 Files writer；原 `files_required=True` 不改成 false，
不偽造空 manifest，也不在已有 manifest／sending／publish 時略過。
確認寫入或 CAS 失 ACK 只核原件；競爭者先寫入的檔案進度不會被確認覆蓋。
immutable 已保存但 following CAS 尚未生效時，核回回 `files_confirmation_saved`，
不回 `files_migrated=false` 或 ready、不把它當 grant；真人按「繼續已確認的接續」後，
`without-files/continue` 只讀原確認並核完整原快照／現況，完成原 CAS，不重新寫確認。
工作台按 confirmation RID 持久保留核回關聯，後續正常換版不能讓已核回的舊 UNKNOWN 再次變未決。

同一目的 SID 仍用原 follow RID，只有正常 phase／ready 進度前進。
若目的 image 已舊到不懂新控制，且部署已產生新版 SID，明確確認可沿原 retarget 產生
固定的新 follow ID；原操作保留並以 `retarget_request_id` 指向新操作，
舊 required-files 內容／原 manifest 不改、舊操作不冒稱 ready。無需新 IAM 或修改舊 schema。
目的 image 限**含本修正或更新版**，以 `files_omission_profile=fa-files-omission-v1` 辨識控制能力；
更早 image 明拒，回滾也不例外。更早版本需另行同 build API／Hosted 回復並核既有狀態相容，
不能以此批准刪除／改寫新狀態。

確認後仍按原排空、官方 stop／inactive、confirming 重掃及 Agent ready CAS 接線，
ready 保存 `files_migrated=false` 與原 `files_skip_request_id`，不帶搬檔 manifest。
有 helper 的目的端只初始化自己的新工作根，不讀／拷來源，讓後續正常工作及換版沿原 Files 路徑；
畫面一次明示「這次沒有搬工作檔」。④仍不能以缺 manifest 的 ready 自動恢復舊中斷工作。
原草稿文字／附件、登入／Agent／CID 世代隔離、timeout 與原確認 journal 保持。

### Verified against（不搬檔接續）

基準 `14ba0e89`；`native_files_omission`、原 `native_environment`／API／wire、
`NativeInstructionControl`、bridge 和共用 HTML。舊來源未確認的兩 lane×手動／idle 四案仍明拒；
新 offline 反例覆蓋明確確認、新對話／原 CID 接續、原 required intent、失 ACK、競爭、
舊目的明拒、patched 無 helper 目的及正常有 helper 搬檔。沒有 live、部署、SDK 升級或新原件清理。

## 2026-10-03 Hosted③：工作檔遷移與送出前閒置跟版（CODE/offline）

本修訂接續 Hosted① 的純 metadata 入口，取代原「新訊息必須另按手動接續」的正常閒置路徑。
兩個 native lane 在**原草稿尚未 POST** 時先讀平台 metadata，再以原
`POST /native-turns/environment`、`idle_only=true` 做伺服器核對。
版本由 `NativeSessions.selected_version()` 決定，來源仍是原 Agent item；不另造 selector 或版本帳本。
目前 SID 的實際版本只讀 `get_session.version_indicator`，不把新版本 selector 當成來源版本。
來源 SID 被平台以新版恢復時，歷史版本不改；符合下列保存條件便接到新的具體 SID，再送新草稿一次。

伺服器沿原 16-key／10 秒掃描與 task observer 核對整個 Agent。
`idle_only` 不呼 `close_suspended_start`，不清未決 admission，也不把 accepted／started
或其中的 HITL／ask_user 等待改成 unknown。未決工作、通知、讀取拒絕或結果不明都停並保草稿。
這不是④的中斷 continuation，不重跑舊 RID；next 仍先取得新的原生 RID，不借舊 RID 送新回合。

工作檔只含 HOME 下 `.fa-assistant-v12/desktop/files` 的已落盤非敏感普通檔與相對目錄，
包含二進位、未提交內容與必要的非敏感 Git 檔，不一律略過 dotfile。
上限為 **2,000 項、單檔 32 MiB、合計 64 MiB、manifest 256 KiB**；超界明拒，不截斷。
source helper 必須確認檔型與 mode，拒絕連結、hardlink、特殊檔、越界及已知敏感／外部引用。
不搬整個 HOME、shell HOME／tmp、browser profile／登入、SDK raw state 或私有 system／reasoning。
這不是任意秘密的 DLP，也不保證任意工作樹無損；不支援／缺件／必要 metadata 不明就不 ready。

排空並沿原 Agent CAS 擋住新 admission 後，manifest 與每檔送出前意圖存在原 `_Following`。
它們固定 Agent、source/target SID、兩端版本與 follow RID；重啟不從最新目錄重算原意圖。
API MI 用固定 `azure-ai-projects==2.3.0` 的 Session Files download／upload，
只 PUT 操作專用的平面 staging；寫入重試為零。失 ACK 只 GET **原 target／原 path**，
完整 bytes/hash 相同才確認；404、403、timeout 或不同內容都不准第二 PUT，也不以 retarget 繞過。
只有下一個尚未開始的檔才能新 PUT。尚有未決上傳／發布時不能另換目的操作；舊 SID／image 不刪。
原檔已核清後的明確 retarget，先在原 `_Following` CAS 固定接手 request ID、封住後續 begin，
再改 Agent 指標；原 manifest／已確認進度保留，避免讀回和指標更新之間又送出舊 PUT。

來源 helper 再核原 manifest、官方清單一致後才允許原 stop；
stop 後以官方清單及分批原檔 hash 再核，不一致就不發布。
目的 helper 從已核 staging 複製成另一組 inode，核齊檔型、mode、目錄及內容才發布至工作根；
不把上傳中的 inode 移入正式樹，因此晚到 PUT 仍只碰 staging。
不同目的內容不覆寫、不刪。最後只由原 Agent item 的 ready CAS 發布 binding，
`files_manifest_sha256` 連回這次 manifest；舊 ready／checkpoint／receipt bytes 不回填或改寫。
發布／ready 失 ACK 只核同一原操作，不重拷或啟模型。
目錄核對把原目錄索引、分頁 cursor 與已見子項存在同一進度；10 秒窗口結束回
`files_pending`，不重掃同一前綴。停止後另從首目錄開始核一輪，跨次同樣沿原 cursor。
selector 又更新時，明確 reconcile 仍讀原目的 SID 的原意圖與 staging，不建立新的 served reader。
原目的實際版本漂移時，僅原 `read/observe/confirm_readback` 可核同 SID 的既存 sending，
歷史 target version 不改；不因此開放新的 begin、publish 或 ready。

`CONFIRMED` 的 `scan_pending`／`files_pending` 共用 #501 同 body／RID 的 2 秒續做，
含首次最多 10 次，不因換檔重開窗口。UNKNOWN 不自動續做；原檔讀回已確認為正常
files_pending 時，真人可明確續做同 body，舊 UNKNOWN journal 仍保留。
跟版 ready 後重核 Agent／CID／登入／觀察世代／草稿與送出條件，才走既有 once journal。
切換、超時、目標改版與晚到結果不會替另一份草稿送出，文字、檔案與圖片保持。
手動跟版超時釋放這次 UI 等待 token，保留本機未確認標記，只開原件核對；
晚到舊 finally 不得釋放新一次核對的鎖。
純 metadata 更新不授權搬檔；正常已在本版的核對也不能拿工作台快取 ready 代替 server。

UI 固定提示「已切換到新版環境；未納入保存的暫存檔與登入狀態可能不在」；
純讀「更新環境版本」收在詳細，local／stage 仍共用原 vanilla HTML。
舊 image 沒有 helper、保存根缺失／不符、必要檔超界或不支援、原工作未決、權限或傳輸未核清時，
仍需確認原狀態，不以空目錄、Blob fallback、新 IAM、bootstrap 或重送訊息掩蓋。

### Verified against（Hosted③）

- `behavior_native_sessions`／`behavior_session_files`、`native_environment`／`native_environment_files`、
  `native_files_wire`／`native_workspace_files`，沿原 `private_files`、runtime factory／dispatch 與 Agent CAS。
- `web_test_console/native.py`／共用 HTML；真 owner API、固定 Hosted local store／兩 lane handler，
  模型與 Files transport 為具名離線替身；涵蓋漂移後新 RID、原回條不變、上限、失 ACK、重啟與草稿。
- [實作與限制](../src/fa-shared-context-poc/README.md#hosted-workspace-follow)；
  不改 SDK 版本、不代替 Hosted／檔案權限或 stage live 驗收。

## 2026-10-03 中斷後自動接續④（核准契約；CODE/offline，兩軸 CLEAN）

聯合計畫`5f18a290…`§4–6的安全續行分支，不取代原結果保存／CTX-03或增設偵測／lease。
只有官方`entry_mode=recovered`／`is_recovery`可為原started請求建立一次持久handoff；
原結果已完整就只核回，普通GET／重述／retry／resumed／取消或timeout不能冒充process-loss。
原UNKNOWN不覆寫，不掃歷史UNKNOWN找工作。

原Agent＋CID＋本地原請求root固定派生RID，明列`continuation_of/root_request_id/attempt`，
每root最多2個自動接續；跨程序／SID不重算，同attempt不改編號。T5原root10交辦獨立保持。
先讓出舊執行槽，再沿原官方enqueue與同Agent API轉送；不等待新SDK、不搬舊task的lease。
續行不是新真人訊息，必須保留原command／來源，使用原模板與現行撤權、取消及預算規則。

恢復只採原CID最後合法checkpoint與有界原件引用；原請求、附件、相關答覆、append順序／狀態、
HITL／ask_user答案與期限都維持原來源，不重發追加、不復活等待協程、不挪用舊once。
沒有合法checkpoint、conversation已前進或無法核清來源就停；跨SID須先採③工作檔／binding的真正ready，
legacy缺manifest或平台metadata不是搬檔證據。已接③ #548正式Files／ready與原ready控制POST通知；
已整合② #553正式來源。通知只安排既有持久handoff，GET／reconcile不派送。
fresh／queued錯版不啟SDK；官方recovery只可讀原材料交接，不在錯版接著跑舊SDK。

`NativeEffects`水位是dispatcher的控制，不只是一句模型提示：done只讀原結果；
有可核未dispatch證據才重新規劃／審查；pending、未知、已拒絕或無唯一原操作關聯不自動重做，
不能藉新call ID／參數／工具繞過。舊缺水位的紀錄不回填成未開始。
原peer／group結果仍回原message／root；等待接續不能先發布舊UNKNOWN來消耗唯一結果回程。
停止且核清尚無child／未決admission後，另以固定通知ID補原回程；原UNKNOWN不改寫。
已接單child仍獨自負責結果。暫時換版／workspace拒絕、5xx或讀回不明不冒充停止；
取消／明確撤權／CID前進的停止原因保留，不能把撤權顯示成真人取消。

取消待接單或執行中續行另存root停止意圖，不改原UNKNOWN。原RID與派生RID各保自己的進度與結果，
進度不是checkpoint／批准／原回合成功；原對話顯示接續關聯、次數及root已記錄用量與未確認部分，
不填零或冒稱真人已收到。完整接點、官方差額與目前限制見
[④實作說明](../src/fa-shared-context-poc/README.md#native-auto-continuation)。

## 2026-10-03 HITL：混合 effects 讀取與失敗診斷（CODE/offline）

補正 #519：`fa/v1/effects` 也保存私人記憶更新與 Team note claim，不能只憑 `fx:` 前綴
當成工具 effect。歷史 reader 只略過 kind／request_id 與既有 claim key helper 一致的
`fa-private-memory-update-claim-v1`／`fa-team-note-claim-v1`；
其他 `fx:` 紀錄仍經原工具嚴格 decode、key／digest／scope 核對。空過濾頁保留 `next_after`，
不改 claim、不另 store、不吞工具的壞資料，也不增加寫入／審查／效果。

原 `NativeHitlClient` 失敗路徑新增 `NATIVE_HITL_AUTHORITY_FAILURE`，JSON **只有**
`operation/status/code/exception_class` 四欄：固定操作名稱、實收 HTTP 數字或 null、
白名單錯誤碼與已知例外類別。HTTP 200 解碼失敗／超限仍記 200，送出前失敗為 null，
不拿包裝後的 `Problem.status=503` 當 HTTP 狀態；無法辨識的值固定為 `UNCLASSIFIED`／`unknown`。
不記 body／URL／token／message／traceback，不改原 status/code、poll、TTL、review、retry 或 reconcile 次數。

### Verified against（混合 effects 與診斷）

基準 `99f0842bbc7c022ed0a2de7adc594c41c1c18475`，WIP tree
`c2615c51f4e5a500922c368fa01ad921f80a1f4b`：相干離線 96 PASS、API-only cold PASS。
兩個原 RED、固定四欄及完整驗證界線見
[Shared Context 接點](../src/fa-shared-context-poc/README.md#hitl-混合-effects-與失敗診斷codeoffline未驗-live)。
原 SP16-02 的 `AUTHORITY_UNAVAILABLE` 仍未定位唯一原因；reader 修正不是該等待中斷的根因證明。
未重送原 RID、未執行模型／SDK runtime／live 或部署。

## 2026-10-03 HITL：owner 事後讀自動審查原文（CODE/offline）

SP-30 的操作說明不只限出卡的 Ask。原 Team owner 可沿既有 native 歷史入口
`GET /work-policy/v1/native-turns/conversations/{CID}/requests/{RID}/hitl-reviews`
讀該 Agent／CID／RID 已保存的 action 原文、依序的審查結果、公開 reason 與審查時間。
共用批准卡的 `NativeHitlAction`／`NativeHitlReview`，參數不另遮蔽或截斷；
不公開 effect output／error／progress、執行 token、system／reasoning 或別的 Agent／Team 資料。

API 沿既有 Entra owner＋`Behavior.Read` 與 server-selected Agent 驗證，只轉送 typed read；
Agent principal 不能使用此入口。Hosted 沿 `fa/v1/effects` 原 key 及既有 State store 分頁讀取，
不另建 store／索引／事件倉庫。`after` 是官方分頁游標，`limit` 預設／最多 5 個 store keys；
先核原 Agent／CID／RID，再投影原文。空頁仍可能有 `next_after`，不能當成整段歷史不存在。

action／reviews 原本就隨同一 effect CAS 保存，auto deny 也在拒絕前保存；
本次唯一新增的保存內容是每次新審查的 `reviewed_at`，沿同一次 CAS，不增加寫入次數。
舊缺 action／review 的項目為 `not_recorded`，缺時間為 `unknown`，不回填、不改舊 bytes／ID／digest。
GET 不建 pending、不重審、不放行／消耗、不改 effect 狀態；review allow 不是效果完成證明。
Ask 卡、規則優先順序、模型、TTL、once grant、旗標、工作台及公開進度均不改。

### Verified against（owner 審查原文）

固定實作 base `8c0965a0d2ce7edca18f063a21cd45134930017b` 加本片；
因 GitHub DIRTY 普通整合 published `5c8a8133c8346cddbe14ad92982b4bc7100eccb9`，保留其原 Ask-first 修正；
真正的 writer／reader、DTO、離線 RED 與回歸範圍見
[Shared Context 接口紀錄](../src/fa-shared-context-poc/README.md#hitl-自動審查原文讀回codeoffline未驗-live)。
未執行真 SDK runtime／模型或 live；本片沒有觀察或宣稱任何 image／部署生效。

## 2026-10-03 PA-16 B：請求及用量來源分欄（CODE/offline）

沿已合 A 的原 `assembly` 加可省略 `inference`，None 不出 JSON；舊資料不補寫，
原 receipt／assembly schema_version、必填、既有 digest 與唯一 `AgentTurnUsageRecord` 不變。
gateway logical request、Direct send-start attempts、SDK usage／context snapshot、provider 原報分欄；
429 原嘗試不以 SDK event 數取代，未觀察不是零。固定 SDK 沒有可信跨來源 ID 對照就明示未配對，
不靠同時間、同模型或相同字串硬接；可核的當回合 event ID 去重，異 session 不混入。

原完整 stream 才可 measured 的計費契約保持；失敗／中斷仍保已提供的可信 SDK／provider 觀察值，
但不把部分 stream 觀察加回原帳，也不加 cache／reasoning／compaction／shutdown 成第二份用量。
無可信文字段／完整 request counter 就 null＋原因，bytes、memory counts、context 快照不能冒充 tokens。
Review、受控 manual compaction、已核 child 來源按真接點標用途，其他不猜。

本片只加有界 metadata 觀察；不改 prompt、SDK/tool loop、retry／容量／checkpoint／unknown，
不保存 body／system／reasoning／encrypted／secret／raw events，不新增 store／UI 或授權面。
SDK-independent DTO 與原保存／GET／失 ACK／重入沿 A，舊 strict binary 仍需正常同 build 配對新版 reader。

### Verified against（PA-16 B）

Base `9ad803be`＋B 工作樹；固定官方來源、具名 SDK／HTTP 替身與原 signed 用量帳案例、
欄位／摘要上限及未驗 live 界線見
[Shared Context PA-16 B](../src/fa-shared-context-poc/README.md#pa-16-brequestsdkprovider-分欄codeoffline未驗-live)。
程式／離線接線不等於 Hosted、真模型或完整 PA-16 live 接受，不新增全域驗收帳列。

## 2026-10-03 PA-16 A：native 組裝來源摘要（CODE/offline，待 B）

在既有 `NativeReceipt`／原 `turn:<n>` 加可省略的 `assembly`，格式為
`fa-native-assembly-v1`；兩條 native lane 共用收集及保存接點，不另建 store、帳本或 agent loop。
它只記原 capture 連結、版本、來源 refs／hash、大小及已知／未知原因，不能成為批准或新權限。

- 原 capture 的選版、實際 bind 的 FA `ToolSpec`、SDK create／resume 參數觀察分開。
  system／角色／記憶／skill／附件正文、工具 description／參數／結果、秘密及整包 SDK event 不進新摘要；
  原合法 role text、對話／checkpoint 原件不刪改，不放寬 CTX-03。
- Skills 選中／列入目錄不等於讀取。只在受控 `read_skill` 成功核過版本後記讀取；
  沒有觀察為 null＋`no_read_observed`，拒絕／失敗另記，不推論模型採用或編造「不相關」理由。
- Memory 只接原查詢：disabled／未配置、Agent 來源不適用、無結果、取用失敗及既有大小省略分開。
  有 ID／updated_at 就保原 metadata，沒有版本就 unknown；不重查、不改記憶保存或用量 backlog。
  附件及前一 checkpoint 沿原 refs；compaction 事件只保安全 metadata，缺事件不等於沒發生。
- 沒有可信 native 文字段 counter 時 tokens 為 null＋`counter_unavailable`，bytes 不當 tokens。
  本 A 片不實作 B 的 request／SDK／provider 分欄；**`AgentTurnUsageRecord` 仍是唯一計費帳**。
- 新摘要採有界 compact summary；觀察失敗或大小省略明記 partial／原因，不把成功答覆改成 unknown。
  必要 prompt、圖片、委派容量與錯誤語意保持；真正結果／checkpoint／CAS 失敗仍照原契約拒絕。
- 舊 JSON 缺欄位就不序列化、不補 null／0／空陣列，也不以新環境或目錄重建。原 receipt schema、
  submit／capture／舊 result／checkpoint／approval／usage hashes 不變；新結果可含自己的 metadata，
  重入採原持久 turn 的同一份摘要。API-only reader 不載 SDK；T5 不轉送新私人記憶 refs。

### Verified against（PA-16 A）

initial base `7d61ae9d`；必要普通 merge 保留 published `9d5efcc7` 記憶 S1／S2及 `35c5a4a7` 換版續掃，
以後者為審查基準；第二次整合不改 A 程式或測試 bytes。
固定版本及 SDK-first 缺口、實際欄位、摘要上限、
舊 bytes／digest fixture、兩 lane 離線保存／讀回、例外與原 FAIL 界線見
[Shared Context PA-16 A](../src/fa-shared-context-poc/README.md#pa-16-anative-每回合組裝來源codeofflineb-未實作)。
只有 A 的程式與離線接線，不升完整 PA-16／全域驗收狀態；B 與 live 都未完成。

## 2026-10-03 新 native 對話預設自動壓縮（LOCAL，未部署）

TC S1 的 `auto-no-custom-v1` 已在 live 驗收通過，改成新 native 對話的預設，只在沒有選擇時補上：
- 沒有 profile selection 的 Agent，新對話的 capture 補這個 checkpoint profile；provider 跟模板，平台用 API 本身的平台（部署時是 linux-x64）。
- 「＋」建立的新 Agent，第一份 selection 補同一個預設。
- 既有 selection、舊對話、已明選的 checkpoint profile、`CheckpointProfile` 的預設值與既有 hash 都不變。
- job-bound lane 不支援這個 profile：沒有 selection 的 Agent 不受影響；「＋」新 Agent 若被切到 job-bound，回合會在 SDK 啟動前明確拒絕（和已選 auto 的 A、B 相同）。

實作與測項見 [Shared Context TC 預設](../src/fa-shared-context-poc/README.md#tc-預設新-native-對話預設自動壓縮local未部署)。

## 2026-10-03 TC-06：壓縮後的許可與原附檔（CODE/offline）

固定 runtime 1.0.83 壓縮後只逐字保留第一則與最後 3 則使用者原話，中間只剩摘要，可能讓已撤回的許可留著、撤回不見；
附檔內容也不在保留的原話裡（2026-10-04 G-D 起附檔隨原話送出，被保留的那幾則原話會含附檔，中間回合的附檔仍只剩摘要）。SDK 只有手動壓縮能帶摘要指示（Experimental），自動壓縮沒有，所以在 FA 接點補兩處：
- native `read_user_file` 的回合數改成選填，沒給時讀最近一份同名附檔，結果標出回合；附檔不在電腦磁碟上。
  給錯回合或找不到時回可更正的 `USER_FILE_NOT_FOUND`（省略回合數再讀，沒有就告訴使用者），不顯示成權限拒絕。
- 已壓縮過的對話，每回合 prompt 加固定提醒：只在這次要依賴較早訊息裡的許可或規則（例如省略確認、直接讀檔或寄送）時，
  先用 `read_user_turn` 讀回原話或問使用者；附檔依檔名讀，不知道回合就省略。沒壓縮過的對話不變；
  不改 system prompt、profile 或 runtime 門檻，不加 gate。

實作與測項見 [Shared Context TC-06](../src/fa-shared-context-poc/README.md#tc-06壓縮後的許可與原附檔codeoffline未驗-live)。

## 2026-10-03 HITL 原等待的讀取競爭修正（CODE/offline）

這是 H1～H3 已合後的窄修，不改 `allow_rule`／`allow_once` 授權語意或持久 command：
前者同時保存新規則與眼前原 call 的一次決定，仍須最新 review／原 TTL／scope／ceiling 通過；
兩條規則同時符合時，下一個新 call 的 `ask_first` 繼續優先。

H1 `read` 先完成既有 guard／read-set 核對，再判斷 state／decision／grant 的跨列關係。
合法 owner 寫入穿插 GET 時回原 `BEHAVIOR_CONFLICT`，不把前後兩版混讀當成永久壞資料；
穩定無效原件仍明拒。H2 只有在已有原 pending、已核原期限、原 call 仍活著時，
把 **GET 的 exact 412／BEHAVIOR_CONFLICT** 留在原本等待輪詢中再次觀察。
不重送任何 writer、不換 RID／grant／effect，不捕捉所有 412／record-invalid／5xx；期限到或取消仍停止。

公開固定碼分開真人 `NATIVE_HITL_HUMAN_DENIED`、系統 `NATIVE_HITL_INTERRUPTED`、
期限 `NATIVE_HITL_EXPIRED`、效果未知 `NATIVE_HITL_EFFECT_UNKNOWN`；
permission feedback、工具失敗文字及既有公開進度共用固定訊息，不輸出 raw exception／私有推理。
一般系統停止不能冒稱使用者按了拒絕，未知效果仍不能推定未執行。

### Verified against（原等待修正）

固定 base `9b79df24`；存檔 live `3f440ded` 的 HITL reader/runtime/wire 與它相同。
2 個 signed API／Table GET 交錯反例先重現 grant 已存、interrupted、無 authorization、效果 0、
尚未過期，修後同原 call 一次且下一 call 仍 Ask。現場缺 Hosted log／effect GET，
所以這是已證的程式缺陷，**不是 live 唯一根因的證明**。
真正兩 lane handler／executor／callback 與獨立 reviewer 使用明示 I/O 替身；
審查用量原本已含於 `NativeReceipt.usage` 的合計，沒有新增第二份帳。
四筆各為 3 次的 live 數字沒有逐次明細，不能單靠總數判漏算；保存／重述／失敗邊界與原批次見
[修正來源](../src/fa-shared-context-poc/README.md#hitl-原等待讀取競爭修正codeoffline)。

## 2026-10-03 SP-22／WAKE-06：有界文字群組（LOCAL，預設關）

依總協調核准：同Team的orchestrator管理新群組，最多3輪、Agent公開張貼10帖、
每成員每輪3帖；orchestrator及最後收尾也計入，人類開題／transport ACK／silence不計。
總額及orchestrator當輪均保留一格收尾；接受新交辦時另保留收件者的結果名額，
未決原結果不跨輪、不被final或FYI抹掉。原T5 root10個新交辦獨立計算，重述不重設。

開題／公開帖／換輪沿Team分區與原CAS；原本文create-only，root CAS才發布引用及扣額，
不能把兩個key或T5儲存說成跨store原子交易。未知查原ID，不以新ID盲送；原已發布內容不改寫。
group root是owner明選的orchestrator原Responses RID；同Team Agent來源沿原JWT／RID／root核，
不由模型、平台role或UI猜sender。request／handoff仍走N6-4，result／blocked只取原保存結果或失敗回條；
FYI／receipt明記silence理由、不啟新peer回合、不回ACK。真人待答與批准仍留原紀錄。
成員不可再以request／handoff交辦給主持者，須沿原result／blocked回覆；在公開與留名額前明拒，避免循環待答。
若fresh交辦遇exact409 `NATIVE_TURNS_DISABLED`／`NATIVE_SESSION_CHANGED`，且重讀原收件RID確實不存在，
只將group該交辦記為server拒收並釋放結果格，不捏造Native回條或Agent張貼；同message不再當新工作重放。
此觀察寫入的CAS／回應不明回503供原有限follow核對；其他transport／unknown仍留待決，不靠一般4xx猜未執行。

Hosted只在既有SDK回合提供群組讀取／FYI／換輪工具；T5的接單、有限follow、結果wake不另包loop。
`next_group_round.expected_round`是目前輪次，不是目標輪次；工具欄位需明示先讀`read_group`
的`group.round`，以原CAS防止並發換輪。原換輪的exact409 `NATIVE_GROUP_ROUND_CHANGED`
須以固定公開碼／文字到達模型，說明輪次不符及讀回再呼叫，不得被泛用權限拒絕文字取代。
讀回若已達原預期輪次，不再換輪；仍需換輪才以目前值呼叫，不新增自動重試。
成員給其他合法成員的新交辦不全面禁止；只有交辦回主持者的原`NATIVE_GROUP_REPLY_REQUIRED`
沿原`/send`回固定原因；本片僅接齊／驗證HITL-off時將此原因送到模型，提示由原回合完成結果回程、
FYI用原工具，不改循環阻擋、配額或自動重試。HITL-on的`agent_call`包裝仍把handler失敗
交原效果未知流程，回`NATIVE_HITL_EFFECT_UNKNOWN`；此組合未接齊新傳訊指引，不關HITL或放寬once契約。
未知／真權限拒絕不冒充輪次衝突，非原工具不套用新文字；3006批G4原未觸發不回填PASS。
完成回呼保存原結果的公開引用，沒有委派且無未決結果的orchestrator才保存final；
unknown／busy_refused明示blocked，不等於停止證明或可重做。
群組context不可用時明示不提供群組工具，普通回合仍可繼續。
群組公開失敗不攔原T5結果；原relay先執行，群組保存失敗沿原通知重試。
主持者已完成但成員公開尚未結算時，只存原final引用、不先公開；最後原結果結算的CAS同時發布該引用，
不再呼模型或新增wake／queue。UNKNOWN開題先查同ID，僅確定NOT_FOUND時由真人明示同body續存；
若續存明確拒絕，另記觀察釋放新開題入口，不覆寫原UNKNOWN意圖。
原root與成員都保有自己的CID／SID／checkpoint。首版是新文字群組，不支援群組附圖。

`FA_NATIVE_GROUPS_ENABLED`於API／Hosted／工作台都預設關閉，只接受未設、0或1；
不因N6已採用而自開。8765只加獨立group panel，原tool bar、append、附件與remote接點不改權威。
查看原Agent回合沿已核group activity，接既有native讀取／取消與共用NeedsAttention／H3卡；
群組文字、FYI或原結果不是批准，不新增批准writer，也不拿Agent JWT呼owner append。
程式／離線／真SDK替身及live分開記錄，未部署、live由總協調另排測試Agent。

## 2026-10-03 DELEG D1：選用唯讀 child reader（LOCAL，預設關閉）

新capability `fa-delegate-reader-v1` 只為原 `fa-delegate-v1` child 開父已有的 `read_user_file`；
需原delegation選取、父profile宣告reader與當前captured grant，不新增全域名額或另一個agent loop。
未由部署列入可用builtin、未經原選取／capture時仍是原零工具child，舊配置及grant hash不變。
child只讀原CID已完成真人回合的filename；錯CID／當前回合／錯child來源仍拒絕。
pre-hook核已保存的原child ID，不以固定SDK callback只有父SID作為child權限證明。
結果先存再交回原parent、unknown不重派、取消與原回條沿既有路徑。
固定SDK可能重複傳遞同一唯讀tool call，handler可被呼叫兩次但沒有外部效果；不另加通用去重。
唯讀是本片最小範圍，不是永久限制；D2 live由總協調另排test Agent。

### Verified against（DELEG D1）

固定SDK旗標／UUID／重複handler判讀、實作接點、測試範圍及官方來源見
[D1 README](../src/fa-shared-context-poc/README.md#deleg-d1選用原對話文字檔-readerlocal預設關閉)。
本片沒有改live環境／模板、IAM或SDK版本。

## 2026-10-03 原生公開步驟（LOCAL，未部署）

依總協調確認，native Invocations／Responses從原SDK事件的既有去敏`public_progress`保存顯示snapshot，
不把私有reasoning、秘密或token放入其中。推理摘要沿原`reasoning_summary`資格與provider公開summary規則，
`assistant.reasoning`事件名本身不是可公開全文的批准。
每原RID額外上限8KiB（含snapshot身份外殼、實際儲存JSON bytes與truncated），與checkpoint及答案各自8MiB分開；
超過只截斷此顯示紀錄並明示，不能縮小原答案容量或改NativeReceipt／_Turn／result digest。
保存best-effort；寫入不明只核原key，讀寫失敗／逾時不重跑模型、不擋原答案。
2026-10-03 收尾修補：進度 I/O 的 5 秒期限與 SDK 正常 disconnect→stop 後的 1 秒封口等待分開。
單次 I/O 失敗仍保留有上限的合法後續步驟，收尾只嘗試一次最終 snapshot；
若有未確認 PUT，先讀回原 key 的完全相同 snapshot，未核清不再 PUT。非法事件仍明確拒絕。
短等待逾時取消顯示 writer，不另排補寫；既有三筆未封口紀錄維持 unknown，GET 不回填。
owner純GET綁同Agent／CID／RID，工作台在進行中、完成後與歷史讀回時顯示原步驟；
未提供、未確認、已截斷與原回合成功各自分清，不把畫面當授權或交付回條。

### Verified against（原生公開步驟）

`native_progress_wire/native_progress`與原API／SDK／工作台接點；基準及公開測試、官方來源見
[Shared Context README](../src/fa-shared-context-poc/README.md#原生公開處理步驟local未部署)。
收尾修補只有本機離線接線；原 #474 的 dev 未封口回報與本次修補結果分開，不當作修補的 live 通過。

## 2026-10-02 Native HITL：H3 工作台（CODE/offline、整組預設關閉）

H3 沿原 vanilla 工作台／Clawpilot、JWT／Team owner 與 SQLite FULL journal 接 H1/H2，
不建立另一個 runtime／queue／批准權威。API、Hosted、工作台都沿既有
`FA_NATIVE_HITL_ENABLED`；缺省或 `0` 不增 HITL 查讀／模型／writer，legacy 路徑不變。
H1～H3 CODE 接線齊不等於已獲准啟用；產品 reviewer 模型、live、Toolbox／IAM／部署仍另行批准。

- 每 Team 20 條自然語言規則的新增、編輯、刪除，沿原 revision／ETag；刪除只改現行清單，
  不清舊 snapshot、decision、grant 或 effect。只允許已登入的真人 owner＋`Behavior.Approve` 寫，
  工作台前置檢查不是後端驗簽／Team 身分權威，body／角色字串不能擴權。
- 原生 Invocations／Responses 都從原 RID 的 `/turn-pending` 分頁索引找到真正參照，
  再 GET 完整 pending、核內容 hash／原 Agent／CID／RID／SDK call；可由原 journal 重開發現，
  不是一次 progress event、assistant 問句或平台 history 猜測。
- `state.composer.attention` 仍是一份 `{thread_id,label,item}` 清單，`item` 使用共用 `NativeAttention` union。
  `approval_required` 與 PA-11 的 `question_required`（SDK `ask_user`）共用原 `attention-panel`，只依兩種真正注意事項顯示；
  已保存參照另放規則區的純讀歷史，不因有歷史就假稱仍待處理。回答問題沒有批准權。
- 原動作完整參數以安全文字顯示，不縮為舊 64 KiB preview。三選為 `allow_once/allow_rule/deny`；
  allow_rule 先由人編輯、檢視範圍並確認，滿 20 不覆蓋、不自動改成 once。
  期限、closed、expired、deny、interrupted、effect_unknown、原放行資料依 readback 顯示；
  允許、grant、authorization 與 control receipt 都不是效果已完成。
- 每次新決定／規則修改重讀當前材料與規則，保存完整原 command、控制 RID、revision／ETag、
  pending digest／原 call 後，沿原 journal commit／remote after_commit 成功才 POST 一次。
  相同 intent 使用穩定原 key；UNKNOWN 只以保存的原 command reconcile，不換 ID／續期／重做。
  後續合法的新意圖須另有最新讀回及明確人類確認，另存不同 key，不抹掉舊 UNKNOWN。
- 登入、Agent／thread／設定切換、規則編輯及手動／自動登入更新都隔離晚到的成功、錯誤與 finally；
  remote 存檔失敗／舊 cookie 保原 once，不 POST。Token 不寫進新 journal／DOM／log。
  登入／密碼／2FA／CAPTCHA／付款確認只開既有 viewer 面板供真人接手，不新授桌面能力或繞過 H2 拒絕。

### Verified against（Native HITL H3）

固定 base `56ddb0ed78f5815643cc0eaba4b583fed96b68d3`（#452 合後），本 PR WIP 的
`web_test_console.native_hitl`、原 bridge／native／adapter／index 接點與直接測試。
控制回條 `NativeHitlControlReceipt` 從 H2 私有 decoder 提至原 SDK-independent wire，格式不變，沒有重抄 schema。
71 個 Python 相干案例及 338 個 Node 案例通過；history thread 小增補另 2 案通過，不加總或倒標為後 commit。
API-only 環境可冷匯入正式 API、共用 DTO 與整個 bridge，未載入 Copilot／Hosted SDK。
全部為簽章假 API／工具及模型替身；沒有 runtime 程序、真模型、原 8765 或雲端執行。
完整 route／writer 使用方式與原 FAIL 來源見 [工作台 README](../src/fa-shared-context-poc/web_test_console/README.md#native-hitl-h3codeoffline預設關閉)。

## 2026-10-02 Native HITL：H2 原 call 接線（CODE/offline、H3 未接、預設關閉）

H2 接固定 SDK permission await 與原 native provider 的共同 `NativeHitlRuntime`，
保留 schema／managed ceiling／captured capability／影像空間／取消等原拒絕；custom tool 仍 `skip_permission=False`。
原生 Invocations 與 N6 Responses 共用 executor，不造新 RID／wake／第二個 Responses request。
SDK child 先通過既有 typed task 驗證，再於原 pre-tool hook await 同一判定，不另建 agent loop。

每新動作依最新 Team rules 做一筆、無 tools、非原 Agent session 的獨立 Auto Review；
用原模型 transport／身分／同一回合用量。只信 typed action、owner-approved 規則與可核真人要求，
不把模型／工具／外部內容當人類決定。schema／matched IDs／revision 不明、審查失敗或無可核真人要求均 ask，
保公開原因；Ask 優先於 automatic allow，reviewer deny／takeover 與既有能力上限不能被 once 蓋過
（例外：比對到 Ask first 的非 takeover deny 已改為 ask，同內容／同原 call 已有的有效真人 once 可接續）。
比對到 Ask first 規則時，reviewer 的 allow 或非 takeover 的 deny 都改為 ask，由真人決定（2026-10-03 釐清，比照 grokbot）。
新版仍 ask 時，同內容／同原 call 的有效 human once 可以接續，不新造 grant；allow_rule 的自增 revision 也重判。
已開始動作固定原要求，WAKE-03 的追加文字只供新動作脈絡；「同意」不解除原 pending。
標準 Responses reasoning item 僅略過、不作 tool call 或審查輸出；有效 JSON 的 deny／takeover 不因此降級。

callback 與 provider 共用一個原 NativeEffects claim；H1 的一次範圍在 `/authorize` 最後 CAS
核原 call／digest／effect token／最新 rules／期限後，才可由活著的原 callback 交給 provider。
重入／冷重建不取回 fresh claim，不重跑效果／重發結果；失 ACK 只核原件，unknown 不由之後的 expiry／新決定抹除。
SDK 直接 MCP 只記原 permission 與 SDK completion／未知觀察，不宣稱 remote generic exactly-once。
完整合法 PNG 在同 effect 的 State store 分段讀回後只留小參照，不縮小既有桌面影像容量。
完整委派仍可用原 192 KiB 上限；大型 control material 沿原 Table EGT／InlineMaterial 分段，不截斷或改走任意 writer。
登入／密碼／2FA／CAPTCHA／付款確認沿 takeover 拒絕並交真人 viewer，不以一次點選自動代填。
takeover 指輸入、填寫或送出這些資料的動作（含帶憑證的 URL）；只開啟、閱讀或截圖含登入表單的頁面是唯讀，不算 takeover（2026-10-03 釐清）。

H3 讀 `/turn-pending` 的原 Team／Agent／CID／RID 分頁索引，可重開後取原 approval reference；
索引不調度、不跨 Agent，也不從 assistant 問句或 Responses history 推批准。
回合收尾 `/end` 封閉該 RID 的新 pending／decision／authorization；它只撤銷 FA 等待，不冒充平台 stop／lease。
attention 仍為 H1 的 `approval_required` readonly 投影；PA-11 的 SDK `ask_user` 問題另用 `question_required`，回答沒有批准權。
舊 H1 決定／回條／read-set／bytes 都保留，不補造舊 RAM wait 或新索引。

### Verified against（Native HITL H2）

固定 base `2c1c54434d9228da80f998e9bedb49d5415d15e3` 加 H2；實際 DTO、控制路由、provider 接點、
模型／SDK 替身與原始測試來源見 [H2 README](../src/fa-shared-context-poc/README.md#native-hitl-h2codeofflineh3-未接預設關閉)。
API／Hosted 旗標未啟用，沒有真模型／SDK 程序／雲端／Toolbox／IAM／UI 操作，也未更新全域評測帳本。
H2 固定交付後停止，H3 由父線在同一 session 另派並先協調 published-main UI hunk。

## 2026-10-02 N6 正式策略採用（CODE/offline，未部署）

依D1／D2與總協調核准的清單，程式採`conversation`及`retain-cid`；
兩mode不讀ENV。sync／onboarding僅保留正式`FA_RESPONSES_ENABLED`的原值，
不自動補1；N0、model-direct、N6 probe／interrupt仍剝除。
**2026-10-04修訂**：唯一例外是已開`FA_NATIVE_GROUPS_ENABLED=1`且`FA_NATIVE_TURNS=foundry-native-v1`
而未設此旗標的定義補1，因群組首回合固定走Responses（stage A／B缺此旗標時start回409）；明設0仍保留0。
同build最小active、identity、雙flags、responses 2.0.0及非probe核對不變；
原預設native模板、舊CID／RID／unknown與scope/history規則不變。

這不是現場啟用：第一個真新build版本建立前由部署desired definition放好雙flags；
讀回version與endpoint兩層protocol、更新同build API／Hosted／8765後，
先只選test agent 2的Responses新對話，四項live通過才選A／B新對話。
不重寫原版本／metadata或pin selector繞cache，不新增IAM；403仍停。
原程式、版本化結果及部署限制見[正式策略採用](../src/fa-shared-context-poc/README.md#n6-正式策略採用codeoffline未部署)。

## 2026-10-02 全新空 Table 的原生安裝（CODE/offline）

新增與歷史 cutover 分開的 fresh 入口；限 v1／no Teams、assistant-v1.2、
C1／一 template／一 root，configured A/B 共用原 authority、Team、root 與唯一成本帳。
全 Table 任一 entity（含其他 partition／unknown schema）都拒絕；讀取失敗不是空表。
同 authority 的 guard、原 request 回條、v1 installation、standing policy、初始 counters、
空 pool／A/B WorkRegistry 及 B registry 一次 create-only EGT 保存，
沒有半套可服務的狀態，不刪除／重設／遷移任何既有帳本。

Azure 沒有跨 partition 的 atomic-empty predicate；全表查空及送出前再查只是觀測。
同 guard 的初始化競爭由 EGT 的 create-only guard 處理；
專用 Table 尚未有其他 writer 是操作前提，不用新鎖服務或假造平台原子保證補上。

批准沿原 bootstrap 的 operator 檔信任模式：exact config、plan、輸入 digest、
installation ID、A/B scope、載入 code、原 RID 與最多 15 分鐘時間窗。
`authorizing_human_oid` 是批准來源，不代表通過真人 JWT；
不得由該欄位構造 `BehaviorPrincipal`，執行時的 owner／Team／Agent 驗證仍照舊。
先查原回條才判斷新寫入；同原命令已成功時只讀回，過期批准也不重寫。
失 ACK／查無原回條保持 unknown，不能改 ID 或把「查不到」當成可清表重送。

新帳的 `initial_accounted_microusd`／`budget_enforced` 明列於批准輸入，
不是 Azure 花費觀測或免計費宣告；只有實際全空 Table 可建立零期初與空 legacy liabilities。
已有帳本的原 floor、未知用量、reservations 和原費用回條不變。
本路徑不啟動舊 workers／inbox，也不 fake handoff、retirement、source review 或 readiness；
不要求其 `NativeEvidenceBundle`。fresh reader 核真正原交易，不冒用舊 standing history。
Blob/source/backup 實際使用時的保護核對及 native 當前 target／同 build／prepare 接點維持，
`healthz` 不宣稱 readiness 或 cloud PASS，無 policy 的原啟動錯誤不被忽略。

### Verified against（全新初始化）

原始 base `3241d435`、整合 published main `56399a71`，本 PR WIP 的
`behavior_fresh_installation`／`behavior_fresh_operator`、`behavior_store.initialize_empty`、
原 `installed_origin`／`AgentScopeRegistry` 及 `create_production_app`；
local Table transport、signed A/B API、真 CLI 與 API-only cold import 見
[實作／官方來源](../src/fa-shared-context-poc/README.md#fresh-native-initialization)、
[操作手冊](POC-RUNBOOK.md#fresh-native-initialization)。不代表 W9 或 live 已完成。

## 2026-10-02 N6-3／N6-4：工作台與T5 Responses接線（LOCAL，預設關）

新對話只依server模板`state_profile=foundry-responses-v1`及原Options的Responses資格走新lane；
既有Table／Invocations CID與原資料不搬，拒絕不fallback，probe不開正式能力。
已有lane或原command／intent的對話不為送出、登入或讀模型再查模板；
新草稿先驗本機輸入，才讀server選路。已固定Responses只另讀必要Options，不改原lane。
8765平台歷史只依server的conversation/response selector，API MI純GET核Agent/Team metadata，
不先喚醒VM；原回條/進度仍走FA。平台`user`角色不是FA來源證明，不反推RID。
只有完成的final_answer作答案，commentary不混入；缺phase明示未確認，已核RID保原NativeResult查收。
一次一頁，previous response由明示動作讀；未實作SSE，不改官方in_progress重置契約。

T5 request與原CID的result/wake都呼N6-2同一`submit_native_turn`：
首次只持久接單/取slot即ACK，後續原SDK follow用同ID/body核回並補第二步；
首次/send的ACK逾時或5xx未知時，沿原NativeRelaySend官方task有界等待原接單ACK，
避免A先結束而使未接單的原請求失去started來源；不放寬已完成sender的接單權限。
task核回accepted後登錄同一固定accepted follow即回ACK，不等第二步、模型或整輪。
原查回條task真錯限10次，耗盡／取消仍回原unknown；之後B才接單屬已知殘留，不加pending registry。
兩次接單讀空而競爭CID時，僅fresh分支的exact409 NATIVE_RID_CONFLICT可重讀一次原件，
以原capture／lane／CID及quota核對接續；無原件、材料不符、一般409或權限錯誤仍明拒。
明確拒絕及409 quota unknown不擴大自動重試。
needs_response/not_taken正常等待、最多60秒間隔，already_queued/started/終態即停止。
真正HTTP或未確認接手回503、新碼`NATIVE_RESPONSE_UNCONFIRMED`吃原有限錯誤預算；
錯誤不包成正常等待、不把RID改終態，401/403不擴權重試。
root10、message去重、in_reply_to、sender與原結果來源不變；不搬A2A、不做SP-22/WAKE-06。

8765自動核回只限原真人accepted且沒有response_id；進度GET保持純讀，
另沿既有CSRF POST讀本機保存的原command，不更換RID/CID/SID/digest。
首次與後續真錯共10次，journal保存上限與backoff，重啟不清零；達上限可取消原請求，
unknown/started/queued與舊lane不重送。follow登錄失ACK仍保留B已確認接單，明示排程未確認。
官方task負責恢復，沿原函式名與新舊input，不建立第二個runner或queue。
[產品接點與驗證範圍](../src/fa-shared-context-poc/README.md#n6-4t5-responses-後續送出-consumerlocal部分完成)。

## 2026-10-02 WAKE-03 原 RID 的文字追加（LOCAL）

總協調16:45確認：忙碌時追加有獨立`append_id`，綁原CID／RID與`expected_submit_digest`，
不建立普通新交辦、不修改原command／capture／版本，也不必先取得模型槽。
先由owner入口核對並保存控制意圖，才回受理；同原RID內同追加ID／內容回原件，換內容拒絕。
只接文字，不把批准、撤銷或取消改成文字標記；取消走原路，HITL由另一片處理。

追加用固定Copilot SDK 1.0.13正式的`send(mode="enqueue")`，排在目前SDK回合完成之後，沿官方queue進原session下一輪，
不打斷原工作的工具迴圈（2026-10-04 stage A：原用`immediate`時，中途插入的追加讓模型放棄未完成的原工作）；回條標明實際delivery；
仍屬原FA工作，直到SDK queue清空／idle才保存原RID的最後結果與checkpoint，不另包FA agent loop。
保存的結果依序串接每一輪的最終回答；交辦結束的那一輪先放固定交辦文字，模型在那一輪寫的文字接在後面（沒寫就不放）；
之後各輪的回答接在後面，不被交辦文字取代（2026-10-04 stage A）。
同回合第二次交辦會被明確拒絕、不會送出（原RID只保存一筆交辦）；第一次的交辦文字、模型文字和所有答案照樣保留。
送出結果不明也算已交辦；只有明確拒絕且沒有新增投遞才可在同回合再交辦。
SDK send只回message ID，不代表已套用；須以原`user.message`事件的messageId／delivery核對。
一次send-token CAS後不盲送第二次；SDK或保存結果不明只查原追加ID。
尚未送入SDK且原回合已結束時明示未套用；已嘗試但缺可核回執則保留unknown，不冒稱未執行。
取消仍獨立受理、優先停止，不等待追加RPC或模型槽；原RID與控制紀錄皆保留。
舊Table lane不受此片影響。

### Verified against（WAKE-03）

本片`native_append_wire/native_appends`、`NativeTurnGateway.append/read`、N1 dispatch與原控制觀察器，
以及`SdkSteeringChannel`接入原`run_copilot`的idle等待。只保存控制意圖／回條，不自管SDK工具或模型循環。
signed owner→真N1→固定SDK/runtime的離線provider案例已打通native與Responses，含順序、重述、
錯目標／版本、取消、未套用、API／SDK未知ACK。總協調後續同意先交後端；
既有submit與現行8765行為不改（23個直接相容案例通過），UI追加另片接，不把後端交付當成UI或live完成。
後端固定`4c3b18d1→cac17ba8`的兩軸各0 P1／0 P2；模型目錄包裝整合不改本片程式與測試。
[介面、證據與官方來源](../src/fa-shared-context-poc/README.md#wake-03-忙碌時追加文字local工作台接線待完成)。
後續UI另片從N6-3／4正式main接線，讓忙碌原native回合可送文字追加；原模型與目標固定，
不呼普通next／submit、不授批准。追加意圖與未知保存在原journal，純GET回讀另記observation，
取消不等追加HTTP完成；換登入／Agent／thread不發布晚到內容。未套用只提供明示帶回空白草稿，
不自動重送。附件保留於草稿，本片不把它們轉成追加；Table行為不變，live另批。
2026-10-04 stage A：追加由後續輪處理、交辦後追加、交辦輪模型文字與同回合第二次交辦被拒，均以固定SDK真runtime離線測項驗證回覆不掉答案；
第二次交辦另走真N1（掛原RID交辦保存）驗證RID completed
（#611 與後續；測項見[實作 Verified against](../src/fa-shared-context-poc/README.md#verified-againstwake-03)）。
追加改用`enqueue`後，工具迴圈中途的追加排在原工作之後、回覆含兩輪答案，以固定SDK真runtime離線測項驗證；改回`immediate`該測項失敗。

## 2026-10-02 Native HITL：H1 控制契約（CODE/offline、預設關閉）

已核准 SP-20／PA-11／E-08 的 H1→H2→H3：Team（一位使用者）所有 Agent 共用最多 20 條
自然語言規則，選「自動允許／先詢問」，Ask 優先，allow 不放寬既有能力上限或 reviewer 拒絕。
獨立 Auto Review 同時判斷規則及 allow／ask／deny，失敗／不可用／不確定轉 ask；
shell／MCP／desktop／automation 寫入／委派／其他 Agent 呼叫納入，memory／一般設定不額外審查。
這取代 native lane 先前暫案的固定「發送／花費／刪除一律先問」及另開 RID wake；
不變更既有 legacy operation 批准，也不把產品規則當開發／部署操作批准。

H1 僅落實 SDK-independent DTO、API owner writer、版本化 Team 規則與持久待決／決定／一次批准範圍。
規則／決定僅既有 Entra 驗證 Team owner＋`Behavior.Approve` 可寫（Teams relay 的 allow_once／deny 為已核准例外，見該節前述「例外」）；Agent 只能提出／查讀自己的待決、
保存原 call 的中斷／過期／效果不明觀察，不能用角色、body、模型或外部內容冒充真人。
沿 `BehaviorStore` 原 CAS、create-only intent／回條與不可變歷史，不開放 generic Team 資料 writer。
API 不透過被 SDK 等待佔用的 Agent 執行佇列，沒有新增 loop、legacy job／binding 或 grant consumer。

每動作讀最新 rules revision，命中 ID 或 default／fallback 來源固定保存；自然語言 matcher 仍留 H2。
CRUD 改的是新版清單，stable ID、舊版本與原人類決定不變。H1 三個 choice 為 allow_once／allow_rule／deny；
allow_rule 要真人確認精確文本，滿 20 明拒，可另選 allow_once，不覆寫舊規則。
完整 action 綁 Agent／CID／RID／SDK session／tool-call／target／arguments digest；原 command／owner 身份持久保存。
同 intent／失 ACK 只讀同 key，不重寫舊決定；舊 unknown 不因另一個新決定成功而回填。

`pending/allow/deny/expired/interrupted/effect_unknown` 分開，預設 TTL 600 秒在初次 intent 固定，
可由伺服器設定替換，不稱官方 session 保證。
（2026-10-10 修訂，Neal 決定，對齊 [Grok Bot approvals](https://docs.x.ai/grok-bot/approvals-security-and-privacy)：對話發起的批准等人、背景批准約 10 分鐘過期。）
批准卡與 ask_user 提問都帶 `wait`：真人自己的回合（`human_origin`）為 `interactive`，等使用者決定、畫面不顯示期限；
API 只用 `FA_NATIVE_HITL_INTERACTIVE_TTL_SECONDS`（預設 86400 秒）當內部清理上限。排程與其他 Agent 送來的回合為
`background`，照 `FA_NATIVE_HITL_TTL_SECONDS`（預設 600 秒）過期，過期不執行。舊紀錄沒有 `wait` 欄位，一律當 `background`。
同對話的新訊息不會自動拒絕等待中的批准；真人訊息也不打斷背景等待。等待一分鐘後 Hosted 讀取間隔從 0.25 秒放寬到 3 秒。
接續回合（continuation）接手原回合時，原回合還沒決定的卡片收成 `interrupted`；已有的真人決定保留。持久 pending **不是可恢復的 RAM waiter**；
讀回 allow 或 `NativeHitlOnceGrant.uses=1` 也不是 effect dispatch。H2 才接 SDK await、
當前獨立審查／ceilings、原存活 call 與一次 reservation／consume；規則變版須重新審查，不重跑未知效果。
原決定和 grant 不因過期／中斷觀察被抹除，也不捏造 SDK shutdown／lease／effect-not-applied 證明。

`NativeAttentionItem.type=approval_required` 只由持久 pending 投影，保留原 pending ID／digest／call。
PA-11 的 readonly 問句注意事項將用另一 type，不可成為批准；N6 history、assistant 文字及舊卡片都不是批准來源。
登入／密碼／2FA／CAPTCHA／付款確認由真人 VM viewer 接手；H1 不擴充桌面能力或動 UI。
N6 Responses 與 native Invocations 後續共用此契約，不用 remote MCP approval items 偽裝 FA custom tool。

### Verified against（Native HITL H1）

基準 main `74ee30665e1217f34b9f822eea28e3ca65e84f62` 加本片；實際欄位、API 與離線來源見
[H1 README](../src/fa-shared-context-poc/README.md#native-hitl-h1codeoffline整組未齊不啟用)。
H1 預設關閉，不代表 SP-20／PA-11／E-08 已整體完成；H2／H3 全齊且另獲准才可啟用。
本片沒有 SDK 原生執行、Auto Review 模型流量、DOM、雲端、Toolbox 建版或 IAM 寫入。

## 2026-10-02 空 bootstrap 僅在同 build reader 準備（LOCAL，未部署）

只有原`preparation=None`時，API才可在server選定的同build具體版本reader上送`session_prepare`。
固定bootstrap SID即使存在，也只用來查原狀態，不能因為存在就向舊build準備。
同版reader不存在時，重用原API MI、既有固定版本SID命名及`_destination`明示建立／讀回；
已存在但尚未prepare的reader，直接沿N1原state／ETag準備，不先要求它已有preparation。
已有preparation時只讀回原結果與已發布binding；API重啟或換build不藉bootstrap隱式升版。
固定SID不存在時，沿既有版本reader查原結果，不另建SID對照表。

`preparation=None`不代表沒有native資料；N1原四空條件及原ETag CAS不變，
已有RID／effects／admission或並行接單仍由原接點拒絕，不搬、不覆、不清原資料。
目的SID與具體版本須精確讀回；403立即停止，不以後續讀回掩蓋拒絕。
建立回應未知只讀同SID，未確認即停，同程序不重POST；送出前仍核owner與撤權。
普通read／submit／reconcile不建立或prepare環境，不停止legacy，不呼模型／SDK、不改IAM或selector。
A在下一個修正版build前維持legacy；下一批先sync，再由原owner同一次明示prepare，
核對回應版本／SID及原準備結果讀回後才開native新對話，不走舊版prepare→follow或raw RPC。

### Verified against（同版 bootstrap）

基準main`e24d6fad`加本片：`NativeSessions.prepare`／`_destination`及owner HTTP回歸；
39案本機相關測試通過，N1四空檢查未修改，未執行live。
[實作、官方來源與下一批操作](../src/fa-shared-context-poc/README.md#同版-bootstraplocal未部署)。

## 2026-10-02 已發布 SID 的獨立原生 task（LOCAL，未部署）

明示follow／retarget仍先核來源binding的原task與RID已排空、source stop及inactive；
最後同一次agent item CAS發布新SID與server產生的`chain_task_id`。
同一binding的所有CID共用此官方chain，重啟沿同ID；新SID的新RID不再向舊creation SID的task取lease。
舊task不刪、不改、不搬input，原RID／CID／checkpoint與unknown保持；不以新task重跑舊工作。
既有agent item沒有此欄位時明確沿用原`agent_id` task，直到下次明示換版。
task ID依Agent與SID固定，不能由client或model提供；本片不增加binding代數、queue、retry或恢復排程。

start、enqueue、handler task scope、owned recovery及唯讀observer都取已保存的binding task ID。
排空必查來源已保存ID，不得改查尚不存在的目標task而誤判已排空；
有accepted／started、queue／lease或未確認狀態仍不發布新binding。
兩次Hosted跨creation SID的lease PATCH502是現場對照，平台是否禁止此用法仍未有明文證據；
本片避免該失敗路徑，不宣稱平台根因或Responses遷移已驗。

另修空native初始化的bootstrap：只有`preparation is None`、`admission_started=False`、
turns僅有agent item且effects為空，才能以原ETag明示綁到API MI candidate；
這時不需查停活躍legacy SID。已有prepare、RID、effects或接單競態仍拒絕，
不影響一般follow／retarget的stop／inactive條件，也不停止使用者舊lane的session。
[接點、限制與部署步驟](../src/fa-shared-context-poc/README.md#每個已發布-sid-使用自己的原生-tasklocal未部署)。

## 2026-10-02 原生未開始回合的明示收尾（LOCAL，未部署）

本修訂只解除「原input已保存、task仍suspended，而N1原RID永遠accepted」的缺口；
不修飾平台502的原因，也不增加原RID跨SID執行權、retry、queue或恢復排程。
只在明示換版／重指時，核原官方task為suspended且lease為null、無active／pending input，
原inline input的RID／accepted_etag／call_id與原N1回條全部吻合，且回條仍accepted、
沒有run_token／turn_number，才用原ETag CAS將該RID收斂為unknown／`NATIVE_TURN_NOT_STARTED`。
原command、capture、CID／SID、RID、最後合法checkpoint與用量原值保留；
不改官方task／lease／input，不以去敏的診斷摘要重建原材料，也不在任何GET中做收尾。

同SID的新start遇固定SDK真正的transport錯誤時，同樣只嘗試原accepted的CAS收尾；
已started或terminal的勝出者不覆寫。CAS／讀回未確認就明確報錯，不回成功形狀的accepted。
即使官方稍後重入同input，也會讀到原terminal狀態而不再進模型。
正常排隊／工作中的task不屬於此收尾條件；既有SDK RetryPolicy與排空／stop／inactive／commit保持。

8765只對此明確未開始的unknown顯示「這次沒有開始執行，請重新送出」，
不把一般unknown說成未執行；原RID不重送，使用者明確建立新RID。
原CID有已核可接續資料時沿原checkpoint追問，否則保留不可接續狀態，不改成空歷史。
[固定SDK依據、驗證與部署步驟](../src/fa-shared-context-poc/README.md#原生未開始回合收尾local未部署)。

## 2026-10-02 過期 pending 的明示重指（LOCAL，未部署）

當已保存的接續目標落後目前build，owner／部署線可用**新的接續ID**明示改指目前server選定版本，
不能以原ID修改目標，也不自動取消或恢復舊SID的intake。本片不增加UI自動操作。
輸入需確認原source SID、原pending接續ID及其目的SID；新目的SID由API原MI／Agent／版本派生，
N1在寫入前再次核runtime版本正是caller確認的server版本。

原pending與其已建立SID、原RID／UNKNOWN、原CID／checkpoint均保留。
原State store以新接續ID的key保存不可變的原pending參照與新目標，同ID跨build改目標也拒絕；
沿新操作自己的小頁游標判讀，
有accepted／started、未確認接單或官方通知／queue task時仍pending，原agent操作指標不變。
只有排空成立，才以原agent item ETag CAS把目前操作指向新接續，不重寫原following記錄；
舊操作若已完成或有別的操作勝出，就明確拒絕，不拿新ETag強行覆蓋；
落敗意圖不占用目的SID的following，保留後仍可用新命令從勝出SID接續。
目的SID若已保存另一個不相容的following，須在指標CAS前拒絕，不能回409卻先改掉目前操作。
重指全程沿原接續ID自己的進度；指標勝出後，reader／follow核不可變意圖再選該進度，
不把單次讀空當作跨key鎖，也不讓晚到的普通following取代新操作。普通接續的舊來源行為保持。

CAS後重用既有follow的原SID stop／inactive讀回／目的版本／commit檢查；只變更操作不等於ready。
回應未知時，只能用同一份新命令明示核對／續行，不重送原模型工作。
完成後可在原CID以新的RID追問，從原合法checkpoint恢復；本機結果不代替live記憶驗收。
[操作步驟與官方來源](../src/fa-shared-context-poc/README.md#明示改指目前版本local未部署)。

## 2026-10-02 C5：native 圖片保存及查收（LOCAL，未部署）

原 capture 選入 `fa-image-delivery-v1` 時，SDK `deliver_image({})` 只讀本輪 browser/desktop
真工具回呼的最新 PNG，不接受模型提供的路徑、base64、Agent 或成果 ID。沿原 PNG 轉檔與
512 KiB 上限，每原 RID 最多確認四張；重複同圖回原回條，下一 RID 不繼承 latest_image。
PNG 以原 RID/內容 hash 的 key 在原 Agent/CID State store create-only；確認完整 bytes 後，
才 CAS 追加小清單。失回應核原 key，不覆寫 PNG、不換 ID；並行不能使第五張成為已交付項目。
不造 Job/claim，不搬進 M3，不建立公開圖片 URL；確認保存不等於真人已收到。

N1/N2 依原 Agent/CID/RID/submit digest/version/hash 讀完整原件，owner 查收不啟模型、不重送。
8765 沿既有 images endpoint/畫廊，用原回條提供明確「查看截圖」，不捏造 legacy 工具成功事件；
已有回合的 completed/unknown 均可查已確認圖片。驗長度/hash 後才顯示，換登入/Agent丟棄晚到回應。
[實作、官方來源與分批結果](../src/fa-shared-context-poc/README.md#c5原生圖片保存與-8765-取圖local未部署)；
這是 C5 圖片切片，不代表 C6 全能力、真 browser、Hosted 或部署已完成。

## 2026-10-02 C5：native skillify 接回 M3（LOCAL，未部署）

**2026-10-03 選填文字附檔擴充（CODE/offline）：** 原`save_skill_draft`可選最多4份
`{name, media_type, text}`，每份完整UTF-8≤8192bytes，正文8KiB不變。只擴原Agent save/reconcile；
不新增API writer、Actor例外、操作者代簽或IAM，也不擴工具需求作者流程／binary／檔案樹／script/plugin。
server依原Agent/CID/RID與資料派生ID／version／hash，所有資源與草稿共用完整`NativeArtifactSource`，
不造legacy work；舊來源仍核同source_work，native與legacy不能混用。原M3批准與metadata核對不變。
全部資源確認後才回未批准草稿成功；UNKNOWN只核原key／pending，不新create/PUT或補批准。
無／空附檔保原v1格式、ID、digest與回條bytes；有附檔才新材料／v2回條。
save/reconcile完整JSON只有帶附檔時可到131072bytes，無／空仍32768bytes；不改其他入口上限。
新對話明確選入已批准版本後由原`read_skill_resource`讀取；live另由部署線安排。

新 native human 回合只有捕獲精確 `fa-skillify-v1` 才能沿 SDK `save_skill_draft` 保存一份
未批准 Team 草稿。API 以原 Agent token、N2 原 RID/CID/capture 及 C0 固定 effect 槽核對來源，
server 產生 `NativeArtifactSource`（tenant、Team、Agent GUID/OID、CID/RID、單一 submit digest）。
不得由 body 自證來源；public M3 create/upload 契約不放寬，native source_work/revision 省略，
不填假 work/RID/0，也不改舊版本、metadata prefix 或批准 hash。
有界 C2 目錄只帶原 `native_submit_sha256` 短引用，完整來源留 M3；read 核原 digest 與指定版。
Team 選項逐項取得精確 ref，不因四個長用途擋整個 Team；真正模型選入仍守原 4096 B 上限。

內容／ID只屬原一次效果；失回應查同一 M3 artifact/version/pending，reconcile 不新建或重傳 bytes。
後續核回、owner 讀取、人審及已批准 C2 讀取不依原回合 started 或舊 SID 存活。
保存只表示未批准草稿，原 M3 人工決定與後續模板選入仍必要；寫入回條標示
`at-original-draft-write`，目前審閱狀態另由 M3 讀回。8765 只讀原件，下載核完整長度/hash。
本修訂取代「native skillify 只存私人下載檔」的中間提議；圖片保留 Agent State store，見上方獨立切片。
官方 SDK 已提供 custom tools，FA 只補原 native 來源與 Team 批准接點；
[實作／官方來源／分批結果](../src/fa-shared-context-poc/README.md#c5原生-skillify-草稿與-m3-批准local未部署)。

## 2026-10-02 原生 Agent 明示換版接續（LOCAL，未部署）

### 2026-10-03 純 metadata／drift 入口修訂（CODE/offline）

本修訂只取代下文的「GET先讀原環境回條」與必須由client提供來源SID的介面：
`GET /native-turns/environment`、`/preparation`只走官方metadata list/get，回獨立
`fa-native-environment-metadata-v1`，不invoke舊session，不以metadata自證ready、空閒或checkpoint完整。
漂移／目的reader尚未建立是`needs_follow`，目的reader可讀也只是`unverified`。
原POST可省來源SID，由目的版本reader的原Agent item／ETag決定，原明示來源與retarget契約保留。
原回條GET沒有當前版reader就明拒需接續，不透過舊SID嘗試。
`POST /environment/reconcile`核指定原操作；不得create、stop或再follow，UNKNOWN歷史保留。
onboarding用帶原command的`POST /prepare/reconcile`核N1準備結果，不從metadata猜READY；
pure read只選當前版本專用reader，舊unversioned bootstrap不因metadata漂成新版就取得資格。
已確認的N1接續回條若屬部署端原ID，工作台保存該關聯並以它核回；本機意圖與UNKNOWN不改。

原首次submit明確409 `NATIVE_SESSION_CHANGED`可在接續後取得獨立拒收觀察：
新環境ready、沒有未決接單／接續、原RID不存在且前後Agent item／ETag相同，才明示未接單。
這份觀察不是重送許可，不以單次404、一般GET409或log推定沒有執行。
原CID、RID、回條、UNKNOWN與checkpoint保持；草稿不清、不送模型。
工作檔遷移、selector發布及有界自動continuation由後續已核准切片接線，不由這片冒充完成。
SDK-first依據與離線接點見[Shared README](../src/fa-shared-context-poc/README.md#hosted-接續①純-metadata-與版本漂移入口codeoffline)。

### 2026-10-03 發布選版修訂②（CODE/offline）

dev／stage一致由endpoint selector決定新session的具體版本，PR7同build最小版與永久cache退場。
GET所指版本須active、Agent身分及API build相容；完整definition hash涵蓋image／resources／協定／session／ENV，
同core不同定義可建新immutable版。已發布相同定義不換selector；registered Agent保自己的ENV，
部署-only overlay不加入已批准的authority target。發布前固定原served版，再建／核候選、切selector並讀回。

新正式API帶`x-client-fa-expected-version`，僅作容器端執行前條件，不是Foundry路由參數。
ordinary current每次讀當前reader與原binding，不回退舊SID；實際版本不符不得dispatch／啟SDK。
API接單前確知本次未dispatch才給相符request ID的`not_dispatched`，不推翻已存UNKNOWN；
工作台對版本錯誤自動純GET metadata，新的needs_follow不能被舊ready遮住。無事的ready/current panel隱藏；
UNKNOWN、pending或錯誤仍顯示原核對入口，不自動寫入。

stage A／B、API與網頁明設HITL=1、TTL600、groups=1，builtin目錄補兩個reader；
依總協調決定，可由Neal推翻，不新增IAM。idle新預設900不是永不中斷保證。
回滾用官方create_version復刻舊definition為版號更大的新immutable版，hash相同才切selector／新SID；
不新增回滾UI，不刪舊版，也不以metadata當Files或continuation完成。
③負責正常idle新版本下的新稿預發follow／Files／ready後一次submit；②本身不宣稱此交會已live接受。
完整發布命令、停止條件與來源見[②README](../src/fa-shared-context-poc/README.md#hosted-selector)。

本修訂補上 ADR-0003／ADR-0007 的既有 native Agent 換版，不擴大原 bootstrap prepare。
部署線 sync 後逐 Agent 明示執行，8765「在新環境接續」共用同一入口，
並確認這是**整個 Agent 的執行環境、所有 native CID**，不是只換目前對話。
API 用原 MI 建新 SID、指定本 build 的具體版本；不把 idle 當成自動升版或生效批准。
2026-10-02 d8d2 的原 SID 在平台讀回已是新版，與官方固定版本說明不一致，原因未定。
已明示 pending 接續的來源停機，只核原 Agent／SID、合法版本形狀及目前狀態，
不要求目前來源版本等於歷史 `previous_agent_version`；歷史欄位不改。
目的 SID／版本、排空、實際 inactive 與最後 CAS 仍嚴核，不把409一律當成功或改為noop。

原 Agent／Team、State store、task_id、CID、SDK session ID、capture、答案、checkpoint 與
RID／UNKNOWN 都保留；歷史 SID不改，目前執行SID另列。新回合仍須明確送出，
從最後合法checkpoint接續；缺件／不相容不改成空歷史，不重跑原失敗訊息。

原agent item的ETag控制暫停新單與發布新binding；同列保留尚未核清接單的RID／digest，
不能以RID暫時404推定未接單。支援新版的來源先暫停新RID，再讓舊chain原序排完；
舊binary無此接點不能假稱被retrofit，仍須核原回條與真正停止狀態。
排空以N1沒有accepted／started回條為先；每頁仍16個key，2026-10-03窄修改為在原10秒掃描窗口內連續讀頁，
只有完整掃完的頁才前進after；期限到保留最後完整游標，未排空回pending與原因。
停止來源後的confirming仍從after=None重掃；不改原CAS、key判斷或同RID冪等。已完成effects／candidate不當成未決工作。
8765與共用遠端UI確認一次後，僅最新CONFIRMED／scan_pending可用原body、同RID每2秒續做，
含首次最多10次；其他狀態、錯誤、身份／對話切換或上限即停並顯示進度。UNKNOWN只沿原核對規則，
不把較早pending或純讀觀察當續做批准；不新增排程，也不重送對話訊息。

RID已terminal後，T5官方通知／enqueue one-shot仍可能retry，或chain的suspended寫入未確認；
此時直接stop會留下新SID不能接管的foreign lease。只為這個必要情境，
在RID排空後讀原task與各函式in_progress的limit1存在性，403／未知保持pending，
不強搶lease、不建TaskManager、不刪queue。排完才官方stop_session、GET確認inactive，
保留volume，再CAS記錄old→new與原結果；失聯只核同一接續，不換ID或重送。

跨SID後固定SDK的creation SID不變，開機掃描可能漏原task；
新增重入只處理**本SID持有且已過期的lease／原input**，started或unknown不重跑模型。
真人新工作明拒`AGENT_VERSION_FOLLOWING`；T5同碼表示尚未接單，
由發送端官方RetryPolicy以相同訊息ID／內容再送，不改成對方失敗結果。
API不新增Table SID／對話權威，工作台不新增Agent loop。
[接點、runbook與官方來源](../src/fa-shared-context-poc/README.md#原生-agent-明示換版接續local未部署)。

## 2026-10-02 C4：原生同程序 SDK 委派（CODE/offline）

只有原NativeTurnCapture選入`fa-delegate-v1`時，C0 provider才使用SDK原生`task`／`custom_agents`；
固定同步、零工具child，父模型／能力不因此擴大，mode/model等覆寫仍拒絕。這是同程序child，
不是T5長期Agent跨VM收件匣。原task event和tool_call_id必須唯一相符，不能為未知效果改ID重派。
原RID／SDK session的NativeEffects先reserve，SDK才能執行；observe只更新同一原effect的必要當前狀態，
取消後仍可保存原started/result/childCancelled/parentIdle，但禁止新reserve／model dispatch。
完整結果先持久保存，post-hook再回父；失敗／空結果不能成為父成功，取消ACK不算停止。
主／子模型沿同一C0 sender，已發及未量到的用量一併回原RID，沒有第二份Job／ModelCost或claim。
第二回合沿原checkpoint恢復，舊child的結果與計費保持，不重派；舊Table委派資料與介面保持。
直接重用[固定SDK官方custom agents](https://github.com/github/copilot-sdk/blob/v1.0.13/docs/features/custom-agents.md)；
FA只補SDK沒有的原RID效果／完整結果／取消與帳務關聯，不另建agent loop、queue或技能引擎。
C4依賴C0合main；本機接點通過不代表A整組能力或Hosted品質已驗，C5另行接續。

**2026-10-04 平行子任務（stage B 實測）。** 原本一個父回合只能有一個child，同一回合平行的第二個`task`
被當成「已占用或結果未知」拒絕，模型重試時又因參數相同被判成無法對應、回「效果待真人核對」。
SDK 本來就平行執行同一回合的多個`task`，所以 native 改為每個task call各自保存reservation、child與結果，
一回合最多4個（固定值，不另設開關）；job lane 的伺服器帳本仍是一個job一個child，維持1個。
在保留紀錄之前就被拒、根本沒開始的呼叫，明確告訴模型「沒有執行，可再呼叫或自己處理」，並讓同參數的重試只對應自己；
保留紀錄寫入未確認等真正未知的情況仍回原本的未解決說法、不再接新的task，回合照舊以unknown結束。

## 2026-10-01 Native T5：同一N1收件與結果喚醒接點

新lane以已驗Agent token及原N1 CID/RID核sender，模型不選sender／root／ID；
收件者必須是ready的同Team不同Agent，包括onboarding registry Agent。
2026-10-02提示修補：工具須讓模型看到完整display_name與canonical agent_id對應，
但名稱只作資料；wire／handler仍只收精確ID，resolver仍是同Team與收件資格的權威。
目錄ready不等於native已prepare，不為篩選新增逐Agent查詢，不把空cache當不可接收。
收件端已知preflight拒絕須明說「對方目前無法接收；這次沒有新增投遞」，
但先前同ID的結果仍未能確認，不可宣稱整筆從未發生或換ID重送；sender授權與一般失ACK不誤標為收件端問題。
API relay不借human入口，B沿自己的State store先保存原RID，再交原官方chain。
已核持久回條才由SDK terminal工具結束A當輪，不等待／輪詢B；ordinary busy僅排隊。
唯一result／wake的RID直接等於原request message ID，送回A原CID的新回合；
原completed→hook中斷可補同一wake，不重跑SDK，純讀不發通知。

依Neal18:46／18:47，native只保留root預設10則新交辦的單一常數，不加2hop或每turn100；
舊lane完全保持。root metadata只存ID／hash，在既有control table的獨立Team partition做單item CAS，
不複製內容／回條，不進舊authority inventory。result唯一性由A store處理，不占root名額。
API每次核回原Agent／SID／CID／RID／command／capture／source；同ID不同內容拒絕，
ACK不明沿原ID查回，不換ID或造成功。大結果保留完整引用／分頁，Agent輸入不冒充真人原文。
Native `agent_notice`沿原unknown／busy_refused回條及failure code指紋通知，不把unknown偽稱B停止；
沒有正式停止引用的`server_notice`仍拒絕。舊B23收尾另沿#314。
多跳交辦以可選delegation／continuation保存原關聯，「已交給C」不能先占用給A的唯一result。
目錄暫失只停當輪交辦工具，不作廢已接受工作。通知與queue滿時的延後接單使用官方單次task與
RetryPolicy，不建FA queue；通知重試耗盡明示notification_error，queue重試耗盡明示失敗並通知，
原RID、結果、用量及未知副作用均保持，模型不重跑。
官方delivery retry使用10次、1秒起退避、max_delay=60秒並沿SDK jitter，涵蓋正常10秒中斷；
耗盡時原A對話可見通知失敗，原答案不改。接到子Agent的真ACK就保存delegation於原started RID，
取消／shutdown／checkpoint保存失敗仍保留，不能先用unknown通知占掉上游結果。
Native relay只沿明示prepare後的client，不自行建立session或退回舊fa-session。
延期接單的SID／task-store瞬斷也必須進同一官方retry；耗盡明示unknown並通知。
子Agent已持久接收後，本機retain暫失只標記待核對，不改成工具失敗或換ID再送；
原SDK失敗／取消的unknown回條須保留已核delegation。

## 2026-10-01 原生環境明示 prepare（bootstrap-only，LOCAL）

只針對尚未接受native工作的初始化綁定。部署線沿原owner登入或由真人owner呼叫
`POST /work-policy/v1/native-turns/prepare`，API以原MI、server固定SID和當前build明選native版本
正式create_session並讀回，再由N1 CAS原agent item，保存舊SID→新SID與原準備結果。
同Agent重送回原結果；不刪舊SID、不stop、不換store／task_id、不搬CID/checkpoint，
原RID和UNKNOWN保持原件。普通submit/read不自動建立或換SID，不以403推成切換授權。

原store必須沒有任何RID/待執行資料/effects；首筆admission與prepare在同一agent item上用ETag互斥。
改舊SID前由server唯讀確認舊compute inactive，active/未確認/403不能當成已停。
新版不只核RAM的SID；但不宣稱修改agent列就能fence仍活著的舊binary。
這不是一般跨SID恢復；已有native工作的遺失/過期接續，依ADR-0003另列T5後待辦。
固定SDK的同task_id跨SIDsuspended續接與foreign lease/recovery限制分開描述，
不能用session_id immutable一詞概括成全部不支援，也不能把local成功當Hosted已驗。
[官方來源／接點／本機結果](../src/fa-shared-context-poc/README.md#明示的-api-mi-native-preparelocal未部署)。

## 2026-10-01 N2 工作台明選原生對話（CODE/offline，未啟用現場）

既有 InstructionConnection sidecar 可明設 `state_profile=foundry-native-v1`。
只有沒有原command與任何送出意圖的新對話才走native；原CID、原command及未知意圖固定原lane，
重啟或改sidecar不遷移。lane與command同本機交易保存，僅表示client原路由，不是工作權威。
進度、原文、Agent來源與結果一律經N2向Agent讀回；不造FixedTask／Job／binding。
答案需讀齊分頁並核整體bytes/hash才顯示；Agent原文分開標示，不冒充真人。
submit／cancel／ack回應未知保留原UNKNOWN，後續原回條另存唯讀觀察，不自動重送。
只有人工明確讀到accepted才可沿同RID完整原請求續接，不用404、unknown或started推定能再執行。
取消與收件確認仍是獨立指定CID/RID的動作，查讀不自動ACK。
未設定時保留原工作台；現場啟用由總協調在N1／N2／T5合併部署後安排。
[工作台接點與本機結果](../src/fa-shared-context-poc/web_test_console/README.md#n2-原生對話模式codeoffline未更換現場-8765)。

## 2026-10-01 N1 本機接點：原生回條與官方多回合 chain

依 [ADR-0007 §1／§3／§7／§8](adr/0007-foundry-hosted-native-first.md)，
只有明選 `foundry-native-v1` 的新路徑使用 Agent State store；
旧CID／Table工作／批准／checkpoint原件不搬不刪。VM採官方multi-turn chain，FA只補RID/CAS、
原resolver capture、真CID/turn結果及公開讀取；没有另建scheduler或TaskManager。
先保存接單回條再排隊，handler用原accepted_etag開始；started重入不重跑模型，
只有meta/rid/原turn/所有checkpoint完整時補completed。shutdown不寫假終態。

真人 `NativeSubmit` 與已驗Agent來源的 `NativeAgentReceive` 分開；
Agent command 的 `authorize_own_result=false`，來源／root／message/result 引用加入原digest，
不能因走SDK使用者訊息通道就變成真人批准。handler執行時才分配CID回合序號和base checkpoint；
取消只作用指定CID/RID。完成引用及固定usage同一次CAS保存，大答案明示分段，
不沿舊32 KiB壓縮截斷，也不回SDK events／private reasoning。

原host初始化先核turns/effects TTL=-1，純讀不建store／不啟動chain。
固定SDK恢復掃描受Hosted SID限制，換SID只允許讀原狀態，禁止以新task ID繞過；
State store跨版可讀不代表chain跨SID已恢復。
完成後才呼用量／T5 callback；重入只補原回條指向的同ID副作用，不換wake ID、不重跑SDK。

N1原片可用一般SDK／普通native checkpoint及本對話公開history。2026-10-02 C0另接
`fa-sdk-start-gate-v1`、`fa-instruction-context-origin-v1` 與 `manual-no-custom-v1` 的原生 consumer：
start 綁本次 API/Hosted code pin 與原 RID CAS，context 綁原 command/capture/source/turn，
manual 使用官方 compact/SessionFs並在N3保存RID/CID/前一合法hash及原compaction觀測。
不再造Job/ExecutorBinding/queue；Native診斷codec不改標普通格式，未知shape仍明拒並保留最後合法點。
後續普通續存沿原compaction的RID，不冒稱當前RID重新壓縮，亦不掃完整歷史。
共用能力Interface只組裝實際已交付consumer，未選不啟動；無consumer的profile／skills／capability仍明拒。
原skill/version/hash/template不改，尚不能據此宣稱A整組工具可用。詳見
[C0契約／相干離線結果](../src/fa-shared-context-poc/README.md#c0原生能力共用-interfacecodeoffline未部署)及
[N1實作／離線範圍](../src/fa-shared-context-poc/README.md#n1原生回合介面codeoffline未部署)。
本機通過不等於新路徑已部署，N0來源與本線結果分開記錄。

Neal 18:47 的native-only修訂：hop只是非負來源深度，不設2跳上限；不額外設每回合100次模型上限。
root新交辦10則由T5 relay單點處理，N1不另數結果或回覆。舊Hosted／Table的#279護欄不變。
## 2026-10-01 N2 真人原生入口（CODE/offline，未部署）

依 ADR-0007，明確啟用的 `/work-policy/v1/native-turns` 只接受原真人 owner 的
InstructionSubmit；server 選定 Agent／Team／設定，經 Invocations 送 N1。
不產生 WorkRegistration／Job／claim，不另存 API 對話索引，不改帳本內部。
原 template／selection ID 保留，能力取原選版、目前 head 與 catalog 的同 digest 交集；
同 RID 及重啟恢復沿 N1 原 command／capture，不重新選版。原附件 graph reader 只接已驗 scope，
完整讀回／hash／格式驗證不變，不用假 RuntimeBinding 或 Session。

read／conversation／history／result 都是唯讀 invocation。MISSING／TTL／UNKNOWN 保留原錯誤；
不依查不到資料建立 store、換 provider、接成空對話或重送未知工作。
只有明確 NATIVE_RID_MISSING 才解析首次 capture（2026-10-05 已由頂端「送出只在 Hosted 去重一次」取代）；N1 啟用初始化需先備妥 turns store。
cancel 指定 CID／RID，delivered 再綁原 result_digest；兩者不啟動模型回合。
回覆完整分頁，不沿舊 32 KiB 截斷；舊 CID／API／8765 保持原路徑。
沿固定 `ed663129` 保存的原 hosted_session_id；換SID可讀原資料，但不能藉此開始新回合或更換task。
Agent歷史沿 source／agent_text 標示，不冒充 user_text；human入口不接受Agent receive。
原生終態回條可呼 c7bb 已發布的 Agent/RID 原帳本函式作補記，同資料只記一次；
VM回合末主送仍是主要路徑，不以人類poll作為唯一入帳方式。未知保存沿原回條核對、不改報文。
[確切路由、官方來源與本機範圍](../src/fa-shared-context-poc/README.md#n2真人原生-api-入口codeoffline尚未切換-8765)。
T5 Agent receive／WAKE 及 N1 的VM主送接線另接，不把 Agent 訊息冒充真人。

## 2026-10-01 舊CAP-02收尾：已驗停止不要求不存在的SDK結果

原peer已有matching worker completion，state=stopped、整棵執行樹已停止／後續啟動已fence，
且job依原取消意圖為cancelled、沒有保存的SDK結果或reply時，可以保存固定blocked並消費原queue項目。
不把已停止當成SDK成功，也不捏造B答案；既有副作用／費用與A原回覆保持原樣。
`PeerResultForward.stop_evidence_sha256`綁原completion，其result_sha256維持None；
一般saved result與generation0未啟動取消的舊bytes不變，message及recovery history reader同步核對。

B23這筆已由唯讀確認post_unknown且從未claimed，已有cancel_requested，故只可沿既有
`fence_unclaimed`→worker observe→blocked／ack處理，不需要停止Foundry session。
實際apply仍須再核原job／launch／version／無結果；late claim或資料不同即拒絕。
這是舊Table工作的一次性相容收尾，不建立新收件匣、排程或agent loop；
新CAP-02另依ADR-0007採原生優先。未claimed fence只證明原FA執行不能開始，不宣稱VM被停止。

## 2026-10-01 N0 本機接點：測試 Agent 的 Foundry 原生探針

依 [ADR-0007](adr/0007-foundry-hosted-native-first.md)，只在固定測試 Agent 明確選入
`FA_N0_STATE_PROBE=fa-foundry-native-n0-v1` 時，於原 SDK host lifespan 前啟用 resilient tasks。
探針只處理合成 key；TTL 必須讀回為 `-1`，更新沿原 ETag，換版階段只讀原 store／key，
不重建遺失資料或更換 ID 補做。401／403 停下回報，不擴 IAM。
A／B、普通 invocation、原回條、Table／Blob／帳本及 8765 不變。
[入口、精確限制與離線結果](../src/fa-shared-context-poc/README.md#n0foundry-原生啟用與-state-store-探針codeoffline未部署)
以上保留#313當時CODE/offline界線；2026-10-01 N0本批已獲總協調確認，
結果與#306角色前提見README N3 Verified against。TaskManager為間接啟動證據，
shutdown未觀察，不擴成N1或完整恢復驗收。

## 2026-10-01 一般 Agent 接單純寫入接點

本片只收尾已開始的Table路徑；Neal 16:00改定POC以Hosted原生優先，preview／experimental可用，
權威不必留在FA。State store／Resilient Tasks／Conversations的取代方向由ADR-0007另定，
不因這片保留既有多列資料就要求後續架構照搬。

依 ADR-0006 §4／§5 已確認拆分，T1只準備 caller 原交易的 registration、queued job／pool
及registry append，不自行提交、不先更新RAM、不產生人類交付責任。
訊息、root計數和收件資料由T5同一CAS保存；真正ExecutorBinding及最終執行票
仍由原WorkerCore.start之後建立，RID不變，unknown不另開工作。
`WorkRegistration.agent_origin` 保留原已驗sender的開始回條、relay、refs、上限及外層request，
不使用舊PeerWorkReference或假human。human原話reader不把Agent brief升格為使用者輸入。
新／已就緒CID才admit；忙碌、沒有合法checkpoint或原SOP／草稿寫入仍unknown時不寫registration/job；
訊息仍由T5收件匣保留，result／server blocked WAKE須用其原結果／停止回條另接。

原子性直接沿[Table EGT](https://learn.microsoft.com/rest/api/storageservices/performing-entity-group-transactions)
及[ETag](https://learn.microsoft.com/rest/api/storageservices/update-entity2)，FA只補現有record的關聯寫入，
不新增SDK對話層、queue或State store。[官方來源／本機範圍](../src/fa-shared-context-poc/README.md#一般-agent-接單的純-builderlocal尚未接-t5)
列明固定版本與限制；這個過渡接口不代表T5、正式Hosted或原生替代方案已部署。

## 2026-10-01 緊急接點：peer 安裝沿原 root 已驗證的 origin

peer mounted app第一次建立時，不在Hosted claim的HTTP窗口內重掃同一份完整inventory。
root仍走原`installed_origin`與完整歷史驗證；整個root installation成功後才保留其不可變origin，
並綁同一程序、config物件、store物件和安裝fence。這不是目前policy、row/ETag或執行權的快取。
peer沒有有效root、綁定不符或root已關閉時明確拒絕，不用truthy flag或同步全掃fallback放行。

peer仍執行原`MigrationStore._check_installation()`、使用已驗origin的atomic probe短讀、
自己的`attach_standing_policy`與`activate_target`，並沿原startup/claim驗證取得回條。
root再次驗證失敗時移除可重用origin；驗證進行中不先丟掉仍有效的舊origin。
不增加retry、不拉長20／30秒timeout、不改原unknown或重派B。
本機只驗「首個B claim內inventory=0、claim_state=fresh」及scope/fence拒絕，
不是B業務完成、live通過或完整歷史golden驗收；結果見[固定評測](agent-runtime-evaluation-baseline.md#2026-10-01b23-首次-claim-沿用-root-origin)。

## 2026-10-01 SDK 橋接前置：原工作資料與 VM 回合入口

純 builder 只能沿真 WorkRegistration／job／ExecutorBinding 準備 Assignment 與原 code-bound
SDK binding；尚無真 binding 時拒絕，不用虛構 Native claim 或 offline owner 補洞。
新 VM owner 是 `SdkVmStartOwner`，綁 fresh begin 的 ticket ID／開始回條 hash／worker process。
原 Hosted owner 的 launch／claim／process_generation 要求保持，不混用兩種證明。

VM queue thread 可跨多回合存活，故以原 dispatch finally 的單回合完成標記觀察 SDK entry，
使用 `SdkVmNoStart.owned_turn_completed`，不宣稱 thread 已結束。
entry已嘗試、私有journal寫入未知或未完成回合都不能形成no-SDK結果。
SDK profile/code、resident release與checkpoint profile一致性仍保留；不在發票後改Task。
正式入口切換仍待T2接線，本段只描述本機接口，不冒稱已啟用Hosted新模式。

新 Conversation 可在 server 的 `submit/register(...,agent_vm=...)` 捕獲 Agent VM，
與 WorkRegistration 一起首次保存；已存在的捕獲與 Task 不可因重送換綁。
fresh begin 可注入原 AssistantAuthority，在同一 CAS 建立 `vm_turn_ticket_id` 綁定的 running
Session，沒有 legacy bearer。原開始身分可重用，mutable Session 與寫入時的取消／execution
仍讀目前資料；原SDK已停止時仍可保存取消的checkpoint和回覆。
T2 接 client／新helper route，本線保留這些writer和一般Agent admission純builder。
在尚未啟用直連的VM模型路徑，原reserve／consume helper仍保留一次性intent與共享費用帳；
模型預約的read set明列root budget。VM consume沿原已接單政策，不在回合內重新撤權，
但仍讀目前CostLedger並做CAS；history只允許具有撤權前原registration和真VM開始回條的
該回合請求，不能藉此允許新admission、legacy dispatch或清除未知費用。

## 2026-10-01 緊急修訂：移除合計8次、區分已接收與拒絕

總協調確認：現行2跳／8則訊息、同root唯一handoff及回覆，搭配#279的peer每回合100次，
已可阻止無限互叫。移除 `model_allowance` 及 `reserve_model` 的A/B合計8次計數，
不再因選了collaboration而限制未交接的真人回合，也不限制B只能使用合計第7次。
一般root預算、原請求只執行一次、原回條、訊息／跳數與peer100均保留，不清用量或未決請求。
這取代下列舊段落「暫時保留合計8次」的限制；不是放寬訊息數或提前啟用T4直連。

已核回接收回條之後，B結果未知、查讀逾時或等待到期，工具回 `AGENT_MESSAGE_RESULT_UNCONFIRMED`：
「訊息已接收，B 的結果尚未確認」，不冒稱系統拒絕送出、B沒執行或B已停止。
真的送出前拒絕及已知refs讀取拒絕仍保留原碼；錯誤不是重送許可。
仍有效的模型上限／額度／交接期限和訊息數錯誤，以固定白名單code傳到Hosted公開進度及工作台；
不傳任意exception、body或token。既有progress schema不變，已保存的失敗通知亦顯示其固定原因。
原live失敗和unknown不回填，新結果與[本機驗證](agent-runtime-evaluation-baseline.md#2026-10-01cap-02-移除合計8次與已接收回饋)分開。

## 2026-10-01 T1b 本機接點：一次性開始與原完成列

本片是 ADR-0006 §4 的 wire／receipt 接點，尚未切換正式 dispatch。
server 用已核 Team／Agent／原 RID／Assignment／VM 世代發票；私有 handle 的 digest 只綁 bytes，
不是 caller 身分。worker 的 JWT 與 T2 原控制握手在 begin 的同一 context 區間核對，
一次 CAS 同時消耗票和建立開始回條；已消耗／取消／unknown 不能再次開始。
開始回條保存已驗 `caller_oid`、actor、process、control connection；同 Team 互信不混用 CID 或結果來源。

worker 的結果、記憶用 T3 原 BlobReceipt，checkpoint 保留 CTX-03 原 StoredSdkArchive 命名和 reader；
不改寫 SDK events、不降級舊 checkpoint、不另造分片格式。完成列以原 scoped RID create-only，
內容必須對原開始者／開始回條及實際保存材料；相同內容讀回、不同內容409。
API 先依原 job／RID／usage 做獨立用量 CAS，再 consume completion；第一步失敗就保留原 end，
下一次沿原資料核對，不重送模型。None 用量明確記 unknown，不填0，也不永久卡住完成消費。

接入 T2 時由既有 SDK callback 執行／abort；本片不新增 agent loop、連線表、排程、每工具 authority
重核或模型 counter。source=agent 的票要有原 root／hop／chain／deadline／in_reply_to 和模型上限，
human不套用peer預設100。正式入口與 SDK 相容接點仍須完成後才能切換。

## 2026-10-01 本機接點：T5 peer 回合模型呼叫上限

依 ADR-0006 §5 與總協調確認，先交付 VM 每回合模型計數，不等待尚未發布的 B 票 producer。
上限由可信 caller 傳入；現片只對原已登錄 peer 回合傳100，真人 instruction 不新增上限。
在 per-turn gateway 真正送模型請求前佔用名額，並行不能超出；失敗與未知不能退回名額或重送，
下一個已接單回合的計數重新開始，不把常駐程序誤當同一個回合。

第100次可送，第101次以 `MODEL_CALL_LIMIT_REACHED` 明確結束。
只有在收到原 SDK 的真停止回條後，才保存 `failure=model_call_limit_reached` 及 failed 結果；
原 peer 收到固定原因的 blocked，重讀不重建結果。原件／回條未知仍沿既有規則保留，
不假造退出或把未完成寫成成功。保存及啟動 reader 同步接受此 peer-only 失敗值，
舊 failure bytes 不回填或重寫。

本片保留舊 root 合計8次模型及訊息保護；新票鏈與替代機制上線後才能移除，
不是先刪限制等 T4 補回。T1票資料、T5正式多方轉送與T4直連仍各自接線。
本機37案、固定版本及限制見 [T5結果](agent-runtime-evaluation-baseline.md#2026-10-01t5-peer-回合模型上限)。

## 2026-10-01 T1 第一片：團隊主人索引

依 ADR-0006，API 的真人入口先驗原 Entra signature／tenant／audience／expiry、delegated scope
和原 client；再用已驗的 oid 讀取唯一固定索引，取得該使用者的 Team。不接受 token 額外的 team
claim、request body 或模型自稱的 team。未知、停用或 role 不符一律拒絕，沒有匿名入口。

`IdentityRegistration` 固定 tenant／oid／kind／team，owner 沒有 agent_id；新 owner 僅由原已驗證的
組織 operator 經 CLI 登錄。team→owner 與 oid→team 兩列在同一 CAS create，保證一人一 Team、
一 Team 一主人；不能換人、換 Team 或覆蓋已存在索引。停用只改 active 並保存原 deactivation RID，
不刪資料、不重新啟用。相同 request ID 保留原回條；未知回應只能核對原 ID。
原 operator 的 CLI 唯讀查詢／reconcile 使用原 signature、tenant、client、scope 與固定 operator oid，
不因其 Team 已停用而失去原回條；這不恢復 Team 使用權，也不能用於登錄或停用寫入。
停用 RID 須先通過原 Identifier 格式；無效輸入不寫入，更新後的整份 identity 仍須通過型別驗證。

舊 config 的四個 Agent 仍走原 actor map；原單一 owner 在尚無自己的索引列時，
沿明列 config 相容值，不自動回填。只要有索引列，就不再以 config 覆蓋停用或壞資料。
真人入口多 1 個固定 key／1 GET；同一 HTTP 在 peer router 與 mounted handler 間重用該次查詢，
下一請求重新讀取，不做跨請求 TTL。Agent 舊路徑仍為 0 GET。

本片只完成 owner→Team 與可信 operator 寫入；Agent 動態索引由 CAP-06 PR3 接同一格式和 store。
實際 runtime 仍須屬於該 Team 主人，登錄新 owner 不會讓它使用舊 Team 的 Agent。
每 Agent VM 的新票、consume／開始／結束回條仍由 T1 後續與 T2 共同接線，未因本片自動啟用。

## 2026-10-01 已確認修訂：admission 核可一次，回合內沿用 turn ticket

使用者確認以防止資料損失、重送與明顯錯誤的最小機制取代逐 handler 的授權重驗。
最終執行模式依 [ADR-0006](adr/0006-team-trust-boundary-grokbot-mode.md)：入口核對團隊主人，
同 Team 互信；本節記錄第一片在既有路徑的相容接點，不宣稱已完成每 Agent VM 模式。
進行中撤權於下一回合 admission 生效，不另設短 TTL，也不把舊 R3／R4 的單次請求窗口當成此契約。
本片取消及 execution 改變仍由寫入交易的 read set 核對；CAS、一次性執行、
穩定 request ID／原回條、用量帳及答案／SDK checkpoint 回條不得刪除。
新模型閘道的一次 CAS 依 ADR §4 另行接線；本片不改 legacy metrics 的精確 read set。
票綁原 tenant／Team／Agent／job／execution，防止錯派及重送，不新增同 Team 資料 ACL；
模型文字或 caller body 不能自行簽發或跨 Team 擴權。
既有 checkpoint 版本、完整 bytes／hash、publication／stop 核對是防資料損失，不是重複政策查詢。

**實作中，未部署：** 第一片以 `WorkRegistration.turn_authorization` 保存核可版本，
`registry_scopes` 承接 admission 已解析的 `ServerAgentScope`；舊紀錄缺欄位時不回填。
claim／issue 才把該核可綁到真正 execution 與一次性私有 ticket，不預先冒稱 Hosted 已接單。
API 用票 digest 與原指派的記憶體索引驗身分，不讀 Table；mutable session state 另作單筆讀取，
不把它當跨請求快取。重啟從既有已核對 startup inventory 重建索引，不重發 ticket。
其餘 handler、source、VM 世代及開始／結束回條依 ADR 的 T1／T2 接點處理，
不再擴建舊的每回合 claim 鏈；本片不宣稱本修訂已全數接線。

## 2026-10-01 本機接點：WAKE-05 owner routine producer

只接已驗 Entra 身分／scope 的原 owner，定義來自原已批准 server policy，
預設不啟用；目前限每天固定本地 HH:MM，不實作 timer、cron 或 CRUD，
也沒有建立 SDK／Foundry live 排程。機器觸發與 live 啟用仍須另取得明確批准。

首次登錄只接受 server 固定排程最近一個已到期的 occurrence；未來、非排定時刻與更早補跑均拒絕。
本地時間轉 UTC 若不存在或重複，明確拒絕，不選其中一個 fold 來繞過。
request ID 由原 authority／Agent scope、routine ID 與排定 UTC 時刻形成，
不因到達時間或批准版本改變；同 ID 不換內容重送，原衝突／未知先讀原回條。
原查讀若回 `not_admitted`／`admission_incomplete`，保留其同 ID 處理規則；
已登錄的原工作可以完成本機接單／enqueue，不因新時段到期而卡死，也不重送未知外部效果。

owner、批准版本、時區、觸發資料與通知政策沿原 policy／WorkRegistration／ingress 保存。
來源為 routine，不是新的 human 交辦，也不是操作授權；目前權限、原額度與
原 worker/controller 照常核對。純查讀原 occurrence 不觸發較早時段、不重跑模型。
原人類紀錄的 bytes 與來源保持；本機結果與尚未執行的 live／M/H 分開。
接點及固定結果見 [Shared Context](../src/fa-shared-context-poc/README.md#wake-05owner-觸發每日-routinelocal預設關閉)
與 [評測增量](agent-runtime-evaluation-baseline.md#2026-10-01wake-05-owner-routine-producer)。

## 2026-10-01 本機修正：CAP-02 固定收件人與拒絕說明

新assignment只從既有server A→B配對捕獲B的公開ID／名稱，作為協作工具唯一收件人提示；
schema與handler核同一ID，不能以名稱、訊息內容或模型輸入建立權限。
原JWT／ticket、capability、B無模型登錄、雙端registered read與root／回條契約不變。
舊assignment的缺欄保持原樣，不補寫目前目錄或偷偷換session。

傳送／查讀的公開錯誤回覆沿固定原因碼白名單到Hosted及模型，不公開任意exception、body或私人值。
API內部記錄沿既有code格式檢查保存原原因碼，不先用公開遮罩抹掉B registry／read gate的拒絕類別。
只有送出前已確定拒絕，才回「訊息沒有送達B，也沒有任何B結果」；
unknown或已送出後讀取失敗仍保留原件與不確定性，只說沒有已確認的B結果。
A不得把自己的計算／推測說成B的回覆；本片只修接點與措辭，不加重送或另一套交付機制。
本機結果及原live FAIL分開保存於[評測增量](agent-runtime-evaluation-baseline.md#2026-10-01cap-02-傳送拒絕與固定收件人)；
原live參數未取得前不宣稱已確診，下一批部署後才另跑獲准的新root。

## 2026-10-01 本機接點：長回合的原 Hosted session 保活

依已確認的無命令執行時限需求，活回合可定期送受控只讀請求進入原Hosted session，
間隔須小於原agent版本的idle timeout；這與Hosted往API送progress是不同方向。
只核目前的原execution／owner／claim／session，不寫worker job、probe/report、批准、費用或checkpoint，
不建立新session、不重跑原工作。已取消、失聯待核或平台非active時不得透過保活重新喚醒compute。

保活成功只刷新既有process-local活動提示，維持#212的CAS避讓，不能充當授權或停止證據。
保活失敗只記錄本次結果，等下一個正常間隔，不把回合改成failed、不自動重送start或cancel；
最後合法狀態與未決用量照原規則保留。沒有新持久格式或命令時限。
精確間隔、原GET invocation入口、SDK版本限制與離線證據見
[Shared Context README](../src/fa-shared-context-poc/README.md#原-hosted-session-的只讀保活2026-10-01codeoffline)；
本機接線不代表已部署或平台保活現場驗收。

## 2026-10-01 本機接點：CAP-02 原工作消費與接回

承接下方第一片。A原observer從持久queue推進B，沿既有C1 worker、原claim與native permit，
不新增human admission或B的人類obligation；B只拿原公開brief及當下可讀的固定Team refs。
原SDK callback等待B保存結果後續原A，不新建A turn；B ready／restart也指回原A observer。
同一process的B consumer不重疊，跨process仍由原CAS／once-start控制。

A／B模型reserve共讀既有session counter，合計最多8次、保留A最後一次整合或失敗通知，
用量仍寫原CostLedger。B保存完整結果、SDK狀態與native完成證明後才送原root的result／blocked；
內部收件不是human ACK，原A只準備一次final。取消、期限、未知效果及大結果超限均保留原件／回條，
不能靠重派、截斷或假設已停止湊成成功。無SDK的取消只引用原generation-0取消回條。

本機雙SDK使用固定1.0.13／runtime1.0.83，平台與provider為離線替身；沒有live或H接受。
第一片與第二片的精確限制及分批結果見[Shared Context](../src/fa-shared-context-poc/README.md#cap-02-窄協作工具與-team-refs)
與[評測增量](agent-runtime-evaluation-baseline.md)；routine及一般多方群聊仍不在本片。

## 2026-09-30 本機接點：CAP-02 可信訊息與無模型接收工作

依[CAP-02](agent-runtime-target-state.md#93-cap-02-兩位長期-agent-交接)切片接線。
原A的JWT／ticket／captured capability及目前assignment決定sender、root與唯一B；
訊息本文只當資料，不進human submit或產生新權限。B先沿既有registry登錄無模型的原work，
以真正registered binding核兩端Source／Artifact read；不能拿template或root.allowed_refs代替目前ACL。
B profile不載私人memory／history，source_ids不由訊息擴張；保存、queue與真人交付責任分開。

root、訊息及原回條接入current-runtime／migration reader；同ID同內容只讀原結果，
不同內容拒絕，unknown不換ID。receipt／FYI不啟模型，不代替人類交付。
原第一片只接producer、查讀與工作台投影；B consumer、原A SDK等待／結果接續與共用8次
請求由上方2026-10-01接點承接，不回填為第一片的既有證據，也不宣稱live／品質接受。
精確路由、期限、目前refs子集與驗證見[Shared Context](../src/fa-shared-context-poc/README.md#cap-02-窄協作工具與-team-refs)。

## 2026-09-28 本機接點：受控回讀原user檔案

獨立`fa-instruction-user-files-v1`維持原literal輸入profile，增加明確的
`read_user_file(turn_number, name)`。只讀同一原對話已完成的過去user input，沿既有
UserTurnSource／原receipt／registered hash／currentread與模型再取用／恢復檢查，
不創建任意檔案權限或另一套history。本文／來源保持歷史資料地位，不重啟原指令或猜缺失原件。
原v1 input及原user-turn prompt/catalog不變；新capability必須匹配reader後由human選入新對話。

## 2026-09-27 本機接點：隨原指令保存的小型文字檔

PA-08增加明確`fa-user-text-files-v1`設定，允許本次human請求帶literal UTF-8文字，
而非讀取任意路徑／URL或擴大source scope。原指令加file metadata/text的JSON合計沿8192-byte界線，
與引用附件合計至多4個logical filenames；完整保留、不截斷，不執行內容。
files／profile沿原Task、Registration、submit hash捕獲，既有conversation與原request不讀新selector；
SDK以原生selection傳本文且保留原prompt；2026-10-04 G-D 查到固定 runtime 一則訊息只渲染第一個 selection，
改在原話後接固定 `<attached_file>` 區塊送出每個檔，原話與回條不變，native `read_user_turn` 另列該回合使用者附的文字檔名（來源附件不列，`read_user_file` 也不讀它們）。原human可在當前read權內取回該turn原件，
公開Context只含reference metadata，不由檔名／內容授予任何操作或行為安裝權。
本切片不涵蓋binary／image／大檔或完整PA-08接受，詳見[實作](../src/fa-shared-context-poc/README.md#小型utf-8原文檔輸入local未部署)。

原human可純讀`GET /work-policy/v1/conversations/{cid}/user-text-file-capability`，
只觀察原CID捕獲的文字檔profile，不新增提交許可。shared回應的schema／CID／read_at、
nullable `user_text_file_profile`及`authorization=false`都必填；null是已知停用，缺欄／未知版本是錯誤。
沿既有CID歸屬、未封存／Hosted SID及currentRead核對，一層consistent-read，不讀本文或dependencies。
工作台新對話沿effective template GET，舊CID不得借global selector或reference catalog推literal能力。
只有明選File才本機解碼，BOM／CRLF保留；原Send前不POST本文，下載仍由原件GET每次核目前讀權。

## 2026-09-27 本機接點：新對話template設定的人工選擇

為使已實作能力有可用的採用接點，原指定human可在現有Read及Approve權限內，
選擇已知唯讀instruction profile與目的Team的已批准skill參照。
選擇只影響之後的新conversation，不修改standing policy／舊批准／舊WorkRegistration，
也不變更source、Team、agent、root budget或操作grant；原checkpoint／SDK-start profile保持。

沿BehaviorStore保存不可變原command／回條及CAS current pointer；
重送只核原ID與內容，未知不換ID；既有對話永遠沿捕獲的選擇，恢復不重讀current pointer。
原模板的`authorization=false`不因此變成權限，原M3版本／撤銷仍每次核對。
這不是一般任意profile編輯或自動部署，API／Hosted須先相容，工作是否完成仍由原執行／保存／
交付流程判定。詳見[Shared Context](../src/fa-shared-context-poc/README.md#真人選擇新對話設定版本local未部署)。

同一人工選版可明選`turn_origin_profile="fa-instruction-context-origin-v1"`。
此值沿原TemplateSettings／AssistantProfile捕獲至新InstructionTask，None完全省略；
既有對話、原request重送與重建均沿原選擇，不重讀目前selector或ENV。
只有捕獲此值的Hosted `read_context` callback選用既有origin-aware API，工具集合、prefix、
原user prompt和操作權限不變；它不是每輪自動注入或新human身份。
舊strict reader不能接受非None新欄位，SDK/runtime版本相同也不代表FA reader相容；
必須先有匹配API／Hosted／client才能選用，未選用者保持原wire與v1 callback。

## 2026-09-27 本機接點：原有權限的文字來源附件

PA-08先接既有binding已允許的Team文字來源，不新增任意檔案／URL讀權或上傳權。
明確`text_attachment_profile="fa-source-text-attachments-v1"`允許真人指令帶固定
name／source version／hash／byte_length，原request與task保存完整參照；原user prompt不改（送SDK時依G-D在原話後接附檔區塊）。
每次最多4個原8KiB來源、32KiB總本文，必要來源圖與read-set沿原界線，不截斷當成功。

Hosted取用API核過的完整本文，原本用固定SDK原生SelectionAttachment傳入，2026-10-04 G-D 起改在原話後接固定 `<attached_file>` 區塊；不從name推導實體路徑。
版本更新不替換原選擇；withdrawal／expiry／必要來源變更仍在恢復與model入口及egress重核。
既有工作回條與SDK保存材料不重寫，未知不重送；舊profile不自動取得附件能力。
Context只公開原選擇metadata，未宣稱任意media、image壓力、自然模型理解或完整PA-08接受。
API／Hosted／strict clients須同時相容才可啟用，詳細接點見[Shared Context](../src/fa-shared-context-poc/README.md#原有權限文字來源的附件local預設-off)。

同範圍的human唯讀來源目錄只列原binding current metadata及缺件／撤銷／到期狀態，
讓使用者選擇既有參照；沒有本文或新的source權限。目錄列出不代表依賴／內容已重驗，
實際attachment admission仍須做原完整檢查。缺少附件profile或不支援的private scope明示不可用，
不以空清單假稱沒有來源。
舊對話的metadata目錄須依其原已登錄profile及當前讀權，而非新對話的current selector；
不同或缺失CID不得fallback。原全域目錄仍只代表新對話，兩者都不能替代實際接單的完整驗證。

## 2026-09-27 本機接點：技能manifest的受控文字資源

**2026-10-03 SK-07 ①：native C2 接線（CODE/offline，預設關）。** 新 `fa-skill-resources-v1`
必須與原 `fa-skills-v1` 一起選取；未列入 API 的 `FA_CAPABILITY_BUILTINS` 不可選，不變更舊 grant。
原捕獲回合具有兩個精確 grant 時，native `read_skill` 才回既有 v3 本文＋manifest，
並由同一 provider 提供 `read_skill_resource(name, resource)`。API 從原 Agent／CID／RID 讀回
capture，用原 M3 批准版本／metadata hash 核定資源；模型不提供 Team、來源版本或路徑。
沿原同 Team／同 source_work、4項×8KiB文字界線，舊 v2 reader／manifest bytes不變。
資源是資料，不註冊腳本、plugin或任意檔案工具，也不把 manifest當操作批准；
未知 alias、錯 scope、版本／批准／hash失效由 API 明拒；Hosted 只在同一權限讀回的已批准 manifest 確實沒有這個 alias 時，
改回可自行更正的 `SKILL_RESOURCE_NOT_FOUND` 並列出該 manifest 的 alias（2026-10-04 TOOL-06），其餘拒絕仍回 `NATIVE_CAPABILITY_REFUSED`。
不合 schema 的參數（例如把資源檔名當 skill name、resource 填路徑或 URL）不送 API，改回可自行更正的
`SKILL_RESOURCE_ARGUMENTS`；需要的 skill 不在名單時告訴使用者不可用，不改用其他 skill（2026-10-03 SK-12）。
接點與分批結果見 [SK-07 ①](../src/fa-shared-context-poc/README.md#sk-07-①native-c2-批准附檔codeoffline預設關)。

**2026-10-03 SK-07 ②：既有工具需求（CODE/offline，沿①預設關）。** `X-Artifact-Skill-Tools`
是版本化 `fa-skill-tool-requirements-v1`，隨原用途／資源一起納入 metadata SHA及人工批准。
首版只可宣告既有 `fa-delegate-v1/task` 的精確 capability digest；未知工具、shell／script、
plugin／URL／path與授權欄位都拒絕，不新增dispatcher或註冊工具。已批准內容不等於操作批准。
帶工具需求的正文使用明確v4結果；舊reader不支持時明拒，不靜默剝欄位當完整成功。
API只回原manifest，Hosted沿原capture及C0實際綁定的工具計算`tool_availability`：
缺capture／digest不符為`not_captured`，沒綁定為`not_bound`，全部`authorization=false`。
即使API回覆自稱available，Hosted也重算；缺工具不擋合法附檔資料，不自動補grant。
實際task仍沿原SDK、NativeEffects與HITL；下一回合移除能力後不沿用歷史availability。
①同Team／source_work與4×8KiB文字限制不變；native作者流程／binary／檔案樹不擴大，仍非完整SK-07接受。

SK-07先接同Team／同source_work、固定版本／hash的M3文字deliverable，不開一般filesystem、
URL、script或plugin。資源manifest隨skill版本metadata hash由指定human批准；內容生效仍不等於
外部操作批准，也不能把參照帶到另一Team取得權限。

新`fa-instruction-skill-resources-v1`只新增受控唯讀`read_skill_resource`，模型只選skill name及
manifest alias；真正artifact/version/hash來自目前有效的批准manifest。完整read後再核目前skill、
來源與身分；撤銷、換版、缺件、hash或文字格式不符皆明確拒絕，不讀未核快取或截斷本文。
原profile及無manifest的版本／批准不改；新欄位／reader須一起部署，不自動切換舊對話profile。
目前受控source最多4項／每項8KiB，僅資料取用子集合，不宣稱一般附帶工具或完整SK-07已接受。
詳細Interface與實際結果沿[Shared Context](../src/fa-shared-context-poc/README.md#已批准技能的附帶文字資源local預設-off)。

## 2026-09-27 本機接點：技能目錄預算與漸進降級

SK-03／SK-04沿既有approved-skill來源增加明確opt-in的
`skill_catalog_budget_profile="fa-skill-catalog-budget-v1"`，與宣告model capacity一起固定於原task。
它只限制model-facing `read_skill` description，以既有context文字counter重算完整字串；
本v1選用宣告input容量的2%，不是所有模型／provider的硬性通則或整個prompt的容量保證。

超出時先縮短metadata（以明確excerpt欄位表示，不改批准原文／hash），再只留名稱，
最後依預算省略條目、優先保留明確點名者，並保留省略數／查找方法。目錄省略不等於技能不存在或失去讀權：
原受控reader仍可按有權名稱讀取完整固定正文，未批准／停用／跨scope仍拒絕。
缺容量、未知計數或最小提示仍放不下要回明確錯誤，不填零、不截正文、不退回未核目錄。
接單前按原配置的selection數與實際counter確認最小提示可放入，不先接受明知無法啟動的工作。

此切片不新增權限、control record、SDK loop或壓縮器，也不啟用native skills／plugins。
原4-entry／4096-byte catalog界線、body-fit／實際provider分詞／完整視窗壓力缺口保持，
不由本機formatter及scripted-provider情境推定完整SK-03／04已接受或可直接部署。
實作接點與採用限制見[Shared Context](../src/fa-shared-context-poc/README.md#技能目錄的-token-預算與降級local預設-off)。

## 2026-10-03 native 技能目錄預算（CODE/offline）

#566 指出 native preflight 拒絕帶預算設定的 capture，SK-03／04 在 native 沒有處理端。固定 SDK 沒有目錄預算機制，
所以 native 完全重用上面的 #75 呈現（同一個 2% 預算、同樣的降級順序、同一個 server counter）：
API 新增 native 的 presentation 路由，Hosted 核對 catalog 與容量後改用這份說明，有預算時名稱不放 enum，
被省略但有權的 skill 仍可依名稱讀取；PA-16 記錄目錄階段與 `omitted_by_budget`。preflight 不再拒絕，
「＋」新 Agent 在主人選了 skills 工具時，沿用 A 新對話實際用的預算設定。不新增權限、gate 或 agent loop。
接點、部署耦合與測試界線見[Shared Context native lane](../src/fa-shared-context-poc/README.md#native-lanesk-03sk-04codeoffline未驗-live)。

## 2026-10-01 本機接點：SP 系統提示 v2

新增固定版本 `fa-instruction-v2`，只改新對話可選的行為指引，不改既有 profile 正文、
bytes／hash、缺省欄位、SDK/runtime、批准或原件。原人工 template 選擇及 standing policy
明選仍優先；原本適用 default 的新對話允許 ENV 明選 v1／v2，未設定才預設 v2。
**明確 `FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE=fa-instruction-v1` 保持 v1，不自動升版。**
此處只取代下節的「未設定維持舊行為」；peer scope 的既有不介入規則保持。

原 `WorkRegistration`／immutable task 捕獲選擇，既有對話、重送及 history 重建不重讀目前
ENV 或換 role。未識別設定在需要 default 的新工作接單前明確拒絕；request body、模型文字、
外部資料或偽 approval marker 都不能選版或提供可信執行證明。

v2 納入 SP-01／02／04／05／14／15／16／18／19／21／25／27／30 的精簡行為段落，
保留原指令作用域，寫作指引依 SP-28 按需取用。SP-07 voice、SP-12 環境診斷、
SP-22 群聊、SP-23 表單仍未啟用，只說明限制，不新增功能或承諾。
已配置工具沿原 catalog／capability capture／Hosted helper 組裝；基礎 prefix 不混入當輪資料。
語意是否遵守仍需真模型與適當人工判讀，不能因文字包含安全規則就宣稱程式已強制。

安全 metadata 仍由 API 依原 task 重算，`prefix_tokens=null`，既有 record 格式及缺省
序列化保持；沒有第二個 loop、額外模型呼叫或新的權限控制層。完整本機結果、版本與採用界線見
[Shared Context SP v2](../src/fa-shared-context-poc/README.md#sp-系統提示-v2local新對話預設)；
本修訂不宣稱已部署、不回填舊評測或全域 Target 接受狀態。

### 2026-10-01 補充：v2 另行選入 skillify

`fa-instruction-v2` 可沿原人工 template 選版，另外選入並批准精確版本的 `fa-skillify-v1`；
原 immutable task 捕獲該 grant 後，才取得既有 `save_skill_draft`。這取代原 SP v2 切片
「skillify 只能補已存在 writer 的指引」對這個組合的限制，不擴張其他 profile 或 capability。
預設 v2、未選 skillify 的 v2、模型文字、request body 或偽 approval／attestation 都不能開啟 writer。

工具只保存一份未批准、Team-visible 的草稿；原選版、現行身份／權限、來源 owner、
版本、單一 intent、unknown 不重送及原回條核對保持。草稿生效仍須另行人類批准，
也不授權發布 SOP、套用 code／harness 或個別外部操作。v2 不暗加 history reader；
模型只能整理目前 SDK 對話仍可用的程序，缺件時請使用者提供。

v2 base 保持 6593 UTF-8 bytes／原 hash、`prefix_tokens=null`；新組合沿既有
`capability_assembly` 的 base＋capability set digest 保存。原 profile、grant 版本、
合法組合、WorkRegistration、replay／history 與舊結果不改寫。
實作與本機結果見[Shared Context 修復說明](../src/fa-shared-context-poc/README.md#sp-v2-另選-skillify-的草稿修復local未部署)；
沒有 live／真模型驗收或雲端寫操作。

## 2026-10-03 本機接點：live 探針修正 `fa-instruction-agent-role-v3`

依 dev 上 12 則探針中的 3 則 FAIL，新增精確版本 `fa-instruction-agent-role-v3`。只在 v2 上加兩處句子，其他段落逐字沿用：
- **SP-14（01、04）**：要寫又寄不出去時，直接交出完整草稿，說明沒有寄出及原因（沒有寄送工具或需要人批准）；只有缺少的必要資訊（例如收件人）才另外問。
- **SP-08（12）**：分清楚使用者的裝置和助理的電腦。使用者要在自己的裝置上做事時，明說不能操作，提議改在助理的電腦上做或給步驟，等使用者選；不能靜默換到別台，也不把自己那台叫「我的電腦」。

v2 的正文、hash 與舊紀錄都不變。新 Agent 的 onboarding 改用 v3，只影響之後建立的 Agent；A、B 的新對話仍由總協調選版。
實作與 selection 值見 [Shared Context B1 SP v3](../src/fa-shared-context-poc/README.md#b1-sp-agent-role-v3)。
部署後只重跑探針 01、04、12，由總協調初判。

## 2026-10-02 本機接點：native 共用 prompt `fa-instruction-agent-role-v2`

新增精確版本 `fa-instruction-agent-role-v2`，讓 A、B 和新 Agent 用同一份平台 prompt，native 和 job-bound 都可用：
- v3 的行為段落逐字沿用，只做幾處精準替換：
  - 代寫段補 4afa 的短反例，安撫性措辭不得新增未提供的進度或服務保證。
  - 只有應用程式回報的真人決定才算批准。
  - 以這回合實際提供的工具為準；連線帳號不猜、不換。
  - 新增「在自己的 Hosted 電腦上工作」段。
- 加上 agent-role v1 的原文讀取工具。
- 不宣告 `execute_operation`（native 上一律不能用；job-bound 用新版時也不註冊）。

角色文字可有可無；有的話放在 system message 最後。v1、v3 的正文、hash 與舊紀錄都不變。
新 Agent 的 onboarding 改用新版，只影響之後建立的 Agent。A、B 的新對話仍須真人選版；舊對話不改。
新 Agent 連結到 8765 時預設走 job-bound，部署期間 A、B 也會暫時改回 job-bound，所以兩條 lane 都接上新版。
需要程式強制的項目由原負責線處理，例如 SP-20 批准由 HITL 負責。本片不新增 sender、權限、gate 或 judge。

實作、selection 欄位與本機結果見 [Shared Context B1 SP](../src/fa-shared-context-poc/README.md#b1-sp-agent-role-v2)。
沒有 live 或真模型語意驗收；驗收用真模型探針，由總協調初判、Neal 確認。

## 2026-10-01 本機接點：SP v3 來源歸因與代寫事實

新增精確版本 `fa-instruction-v3`，只在 v2 的 Facts and evidence 與 Drafting and sending
各補一條短規則，不改 v2 或其他原段落、prefix bytes／hash 及已保存紀錄。
來源必須真正返回本次匹配結果，才能把結果歸給該助理或工具；派送／接單不是核對完成，
失敗／待處理／未知如實保留。允許的自行計算須標成自己做的，不能代替被要求的獨立核對；
真正匹配的 B 回覆仍可正確歸因。代寫保留原事實與時間基準，未知原因、日期、
承諾與完成狀態省略或留待補，不把改寫變成新的事實承諾。

未設定 ENV 的程式 default 仍為 v2，ENV 白名單仍只接受 v1／v2；
人工 template／policy 明選仍優先，v3 沿原受信人工選版進入原 immutable task。
舊 `WorkRegistration`、續問、replay／history／resume 不隨目前設定或選擇換版，
user body／模型文字不能選版或證明批准。公開 metadata 沿原 record 重算，沒有新的保存格式。

v3 base 沒有 writer；只有另外選入並批准精確 `fa-skillify-v1`，且原捕獲與目前權威相符，
才能沿原 `save_skill_draft` 保存一份未批准 Team 草稿。身份、Team、read＋execute、
原人類選擇、來源 owner、grant 版本、單一 intent、撤銷、unknown 與回條／history 邊界保持。
不發布草稿、不改有效目錄、不暗加 history／Memory／其他工具，也不放寬其他 profile；
內容生效與個別操作批准仍分開。

實作與本機結果見 [Shared Context SP v3](../src/fa-shared-context-poc/README.md#sp-v3-來源歸因與代寫事實local人工選版)。
本片只有版本化指引與既有 consumers 接線，不新增 sender、writer、權限、模型呼叫、judge、
攔截器、狀態機或壓力政策。沒有 live 或真模型語意驗收，原 FAIL 與全域評測結果保持。

## 2026-09-26 本機接點：新對話的版本化 prompt default

> 歷史規則；未設定 ENV 的行為由上方 2026-10-01 SP v2 修訂取代，原結果與其餘契約保留。

API可由受信環境設定`FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE=fa-instruction-v1`，
只替原policy未指定prompt profile的**新對話**選用既有v1內容；未設定保持舊行為，
原policy有明確profile時仍優先。使用者原文、request body、模型輸出不能選擇此設定。

**2026-10-07 接點釐清（不變更規則）：** 登錄的 scoped Agent 不套用此 server default。
fresh B 沒有版本化 profile／自己的 selection 時，template export 與 native capture
都明確回 `AGENT_TEMPLATE_PROFILE_UNSUPPORTED`；fresh 初始化、原生連線檔或 session ready
不能代替主人的選版。主人沿原 `/agent-template/select` 明選精確版本與設定後，
selection 只影響該 Agent 的新對話，不複製其他 Agent 的批准、身份或私有內容，
不修改原 standing policy、fresh plan、舊對話或模型權限。Hosted 的 checkpoint platform
必須對齊目的端 `linux-x64`，不能由 Windows 操作者的本機 default 代選。

選擇隨原`WorkRegistration.profile`及`InstructionTask`保存，不修改原policy／批准。
續問、原request核對及重建採用原對話已保存的profile，不因default停用、改值或程式重啟而切換。
未知default只在需要該預設的新工作入口明確拒絕，接單／預留之前停止；原對話仍沿原設定。
這不鎖定使用者的每回合格式，原文仍由SDK按原流程處理。

本切片不新增工具、操作權、checkpoint codec或資料型別，也不開啟skills／plugins／Memory。
目前是本機接線，尚未部署／啟用；實際採用及相容回退依[Shared Context說明](../src/fa-shared-context-poc/README.md#新對話的版本化-prompt-defaultlocal預設-off)，
不把同對話profile切換或真模型品質當成已通過。

## 2026-09-23 修訂：取消財務預算阻擋，保留記帳

使用者明確選擇「停用所有預算阻擋，取消 USD110 上限，但保留用量紀錄」。
一般聊天路徑不再因原累計 USD110／USD10 保留額或成本資料過舊而拒絕交辦、
派送與模型請求。不以超大上限或清零帳本冒充取消。

變更由原使用者的 `approve` 權限持久記錄在同一財務帳本，預設仍為啟用；
舊政策、批准與回條不改寫。實際 token 用量、可核對的估價、已觀察高水位、
在途與未決模型請求繼續保存；無 usage 不填零，未知請求不重送。
重新啟用預算阻擋也須明確授權，不會重設先前支出。
停用狀態回傳 `budget_enforced=false`，剩餘額、累計上限與安全保留額為 `null`，
畫面明示沒有 FA 財務上限，而不是「免費」或供應商沒有容量限制。

身份、來源與操作權限、撤銷、防重送、對話順序及資料完整性檢查保留。
下文舊累計上限描述保留為歷史，以本節的新授權為準；
實際部署及啟用狀態另見 `progress.md`，不由規格推論已上線。

### 2026-09-27 本機修補：自由指令 SSE 觀察與用量 framing

公開preview與usage reader共用完整SSE data-event framing，不將一行直接當成一個event。
沿標準接受冒號後可省略的單一space、多data-line以LF串接、CR／LF／CRLF及一個開頭UTF-8 BOM；
跨transport chunk仍保持同一event，只有空行才結束event，EOF未完成資料不發布為preview或usage。
JSON／token計數驗證仍沿原接點，缺失、無效或相衝用量不填零；pending及原consume責任保持。
這個parser只讀取觀察資料，不改provider／SDK原bytes、request ID、stop／completion、
quote／費率／reserve／usage receipt／財務政策，不重建model loop或回填舊結果。
逐回合完整用量索引仍未接線，不把本修補當成完整turn tokens、真模型品質或效能驗收。

## 2026-09-23 修訂：對話沿用 Hosted session，只有明確 reset／new 才換新

使用者明確要求：同一段對話的追問必須沿用同一個 Hosted session，不再每個工作
建立及停止不同 session。第一次交辦保存伺服器產生的 session ID 與版本；
後續工作沿原 ID／版本呼叫，不因部署新版、重啟 API 或重送請求而偷偷替換。
Invocations 以 `agent_session_id` query parameter 綁定原 sandbox；對話歷史仍用
同一 Copilot SDK session 與其保存／恢復能力，不另造競爭中的聊天權威。

每回合可依原契約確認 SDK executor 已停止，但不得因此停止對話所屬的 Hosted session。
平台閒置暫停後仍恢復原 session ID；若原 session 遺失、過期或版本不符，明確拒絕，
需要使用者選擇 reset／new，不自動建立替代 session 或重做未知工作。
平台的 session 保存期限與閒置運算管理不是永久同一台執行機器的保證。

「New session」開始獨立對話。「Reset」必須由原使用者明確操作，先封存目前對話、
保留歷史與檔案，再開全新環境；新環境在第一則交辦時建立。
仍在執行或結果未明的工作不能被 reset 抹除，須先完成、取消或核對。
reset 的持久操作與本機新對話建立各自防重送，停止回應不明不得假稱已停止或自動再送。

2026-09-27工作台接線沿上述取消契約：人工「停止這次回覆」只取消原已接單工作，
不reset／刪除conversation、清除已保存答案或ACK。既有取消受理回條與executor已停止分開，
本機原intent回應UNKNOWN也不因後來job終局而改寫；原進度仍可純讀。
本機能力與實際部署分開記錄，詳見[工作台停止接點](../src/fa-shared-context-poc/web_test_console/README.md#停止目前回覆本機入口已採用)。

原取消回應遺失可另做明確的唯讀核對：只讀原job／generation仍能精確重建的取消控制交易回條，
沿當前human/read及一致snapshot，不再次呼叫取消或推進worker。查無回條仍unknown，不當作未執行；
原context／世代不符不換新request查詢。新查詢觀察與原傳輸UNKNOWN分開，不能回寫成從未失聯或已收件。

這次遷移的特別授權是：既有舊式對話可從工作台移除，並在安全停止後刪除其專屬
Hosted session／檔案。這不等於刪除或重設必要的工作、費用、UNKNOWN 與回條帳本；
也不授權刪除 Agent 範本 session、其他工作環境或專案根目錄。

日期：2026-09-15。狀態：**本版定點修訂 F15–F17 的 P1 文件契約；其餘差異審核待辦未全部關閉，新增能力尚未實作驗收。**

使用者要求將[行為設計](FA-AGENT-BEHAVIOR-DESIGN.md)升格為規格，並依目前 M4 進度納入後續開發。[ADR-0004](adr/0004-agent-behavior-control.md)記錄架構方向；本文件是行為契約與 B01–B12 的唯一規範來源，[開發計畫](POC-ANALYSIS-AND-PLAN.md)管理排程與任務依賴。

「必須／不得」是後續切片的實作要求，不表示現有版本已符合。本文不重驗或重寫 M1–M3 結果，不增加目前已批准 M4 批次的案例、操作、模型、費用或完成門檻。

需求索引為 [requirements.md](requirements.md) 的 `2026-09-15-behavior-1.2`，涵蓋 FR-01–FR-34；本次不新增或重編 FR，FR-27–FR-34 不宣稱已存在於下述程式提交。第 13 節保留 v1.1 初次整合及本次 delta 裁決。v1.1 的「可直接凍結」口徑不再沿用；v1.2 提供完整後續開發契約，不是受影響能力已可啟用或整個 BR 切片已完成的證明。

## 2026-09-22 修訂：移除人造POC額度與呈現處理過程

使用者10:10明確要求移除新一般工作路徑的固定240秒執行、600秒排隊、
六次模型請求、一次執行世代及C1全局單工作限制。
不以超大數字、遠期日期或反覆續期冒充移除；獨立工作／對話可並行，
同一工作的防重送與必要對話順序保護仍保留。

模型必須可設定並真正傳到SDK／模型出口，不固定`gpt-4.1-mini`，
也不強制FA自訂的32,768輸入／1,024輸出token上限。
供應商的實際模型容量、技術封包及持久化容量仍須如實處理，不能宣稱無限或靜默截斷。
原SDK／供應商傳輸錯誤處理不能暗中重新施加整筆工作的固定期限。

取消每工作固定USD2預留、成本觀察超過一小時即拒絕新工作的門檻，
以及取件八次、結果版本／回條等人造計數上限。
原累計USD110內含USD10收尾保留額不變，實際及未決用量仍計入同一總額；
歷史UNKNOWN／預留不得清零或退款，成本延遲不宣稱供應商帳單硬上限。
已保存結果的純讀可持續進行，提交／外部效果／ACK仍沿原ID保持一次效果與正確讀回。
舊工作、舊policy及回條維持原bytes；本節是新路徑實作要求，不倒改歷史。

UI另需提供「處理過程」：沿實際SDK事件顯示工作階段、工具呼叫及去敏結果，
模型正式提供可公開reasoning摘要時才顯示摘要；沒有時明示未提供。
不顯示完整私有思考鏈、內部prompt或憑證，不從答案反推或編造思考內容，
也不為顯示摘要額外呼叫模型或重做工具。這不是新增trace平台或驗收gate。

## 2026-09-22 核心入口：自由指令與接續對話

使用者明確要求解除「只接受固定任務」的限制，並將
**輸入指令 → Agent接單並執行 → 顯示進度與真實回覆 → 可以接著追問或補充**
列為本次核心交付，不再排成固定計算驗收之後的附加功能。

一般入口接收使用者原文，真正傳到Copilot SDK；不得只把輸入型別改為字串，
下游仍強制metrics目標、固定答案結構或抹除使用者內容。
對話與接續優先重用SDK的session、context、events及可用的保存／恢復能力，
不另建一套agent loop或競爭中的對話權威。第二則追問必須使用實際前文，
不能以介面上排成一串、實際每次丟失上下文冒充多輪。

固定計算案例只作回歸fixture與既有工作歷史；原schema、body、digest、帳本、
UNKNOWN及回條不得就地改寫。自由指令仍遵守原身份／資源存取權、共同預算、
每工作額度、必要取消、防重送及資料保存；不新增任務種類白名單、批准精靈或證據等級。
畫面只顯示真正接單、執行、保存與收件結果，不以mock聊天或輸入框存在宣稱已完成。
部署只驗本次差異及最短真實指令／追問流程；Teams仍依原決定延後。

### 2026-09-29 本機修補：並行接單與原 RID 恢復

自由指令 admission 在原 Table CAS 或最後費用觀察明確回 412、尚未提交該交易時，
可以有限次重新讀取並核對後重試同一 admission EGT；費用上限、原工作、sequence 與身份不放寬。
這不是模型／工具／外部效果重送，timeout／commit unknown 不能當作已知拒絕。

原 command 的 `submit-reconcile` 仍只讀。沒有 admission 與已接單但尚未 enqueue 的情況分開觀察，
核目前讀權及原 RID／完整 hash；讀取失敗或原件缺回條仍未知。
工作台可在有效觀察後，讓真人明確以原 RID、原完整 command 補完 admission／queue，
沿原回條及 create-only 身份防止重複；不得修改舊 UNKNOWN、換 ID 或覆蓋新內容。
此例外只適用本機 admission／enqueue 的原子交易，不擴成一般 effect 重試或新的 agent loop。
程式與精確離線範圍見 Shared Context README「並行接單的原 RID 恢復」；未宣稱 live 已修復。

## 2026-09-21 部署前原則：只保留最小必要機制

使用者16:05再次明定：**以足以防止資料損失、重送與明顯錯誤的最小機制完成部署，
不得把商業／軍規式流程、指標、證據層級或繁瑣gate列為前置。**
本節優先約束下述要求的部署順序；規格完整性本身不是阻擋理由。

2026-09-26使用者再次確認本原則。完整Target State仍是實作目標，但先交付可用部署，
不等全部評測、完整plugin隔離、指標或證據分級完成才部署。未完成能力如實保留，
不因此取消原資料保護或宣稱完整目標已通過；只有本次部署路徑的具體風險才阻擋。

- 保留原資料、成果、UNKNOWN、費用及回條；核對本次動作的正確身份、目標與原工作。
- 沿原ID防重送，結果不明先核原回條；需要改動共用工作／C1狀態時，重用既有guard/CAS，
  防止改錯工作或尚未安全結清就讓另一執行者進入。
- 修正必然造成啟動、路由、SDK呼叫、保存或收件錯誤的實際缺陷；只跑涵蓋改動的直接案例，
  再驗最短真實端到端路徑。不重跑無關大套，也不把mock、saved或部署成功叫做業務完成。

每個部署阻擋只需說明具體會丟失什麼資料、重送哪個效果或造成哪個明顯執行錯誤。
無法指出者列為非阻擋改善，不追加批准層、證據包或新框架。
缺少額外provider簽章／request ID、完整生命週期敘事、全面稽核及長時間指標，
不單獨構成部署gate；沿現有可信操作來源核對實際目標與回條，誠實保留缺件即可。
SDK／平台已有能力直接沿用，FA只補本次必要的業務差額。

目前原工作收尾應收斂到原停止回條與工作對應、保存既有成果／未知、原子結清及正確讀回，
不是建立通用採認治理系統。此原則不改寫舊批准、pin或日期，不授權重做已結案效果，
也不取消原累計預算、身份範圍與真實完成條件。

## 2026-09-21 實作原則修訂：SDK-first，FA只補已確認的缺口

使用者明確要求：**SDK能處理的工作，以SDK為主；SDK不能處理的，才考慮自建FA整合層。**
本節優先約束下述邏輯Module的實作方式；它們不是重建agent runtime的理由。
不得因診斷工具或包裝程式出錯，就再疊一套取代SDK能力的框架。

| 責任 | 優先採用方式 | FA保留的最小工作 |
|---|---|---|
| Agent回合、模型與工具呼叫循環 | 固定版本Copilot SDK的session、tools及正式擴充Interface | 接上既有受控工具與模型存取方式；不另外編排同一套agent loop。 |
| SDK session、對話事件、串流、保存與resume | 官方session／event／persistence能力，依實際版本及儲存契約使用 | 對應FA的Agent／Team／conversation／work身份及需要的外部保存；不維護第二份競爭中的SDK對話權威。 |
| CLI/runtime啟動、停止、連線與錯誤事件 | 官方client生命週期與診斷機制 | 部署Adapter、必要的有界等待及去敏記錄；不把自行包裝的狀態當SDK事實。 |
| 工具permission與hooks | 官方permission handler／hooks | 將FA當前身份、資料權與外部效果政策接入該Seam，不複製工具執行器。 |
| 雲端compute、managed identity與儲存 | 對應平台的正式能力 | 保存必要資源對應及部署配置；先確認平台能力，不另造同義控制面。 |
| 共用USD預算、業務工作歸屬、跨入口去重、成果及通道收件責任 | SDK／平台若沒有符合要求的保證，才保留FA控制 | 只處理已指明的業務差額；未知效果不重送，實際回條與真人已讀分開。 |

新增或修改FA自建Implementation前，先對照**實際固定版本**的官方文件或原始碼，
說明現有SDK／平台Interface為何不足，再採最小Adapter或Module。
若只是連接錯誤、序列化錯誤、測試替身失真或呼叫順序錯誤，修正整合接點，
不以新增一般重試器、生命週期框架或多層批准流程替代。
驗收先走真正SDK的最小呼叫鏈，再加必要FA業務控制；不能僅靠mock宣稱SDK能力已整合。

當前版本基準為SDK `1.0.13`／runtime `1.0.83`；參考固定版本的
[Python SDK Interface](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/README.md)、
[Azure managed identity接法](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/setup/azure-managed-identity.md)，
既有session／events資料來源見[官方平台證據](FA-BEHAVIOR-PLATFORM-EVIDENCE.md)。
最新版`main`的能力不能直接套用至已固定版本；若文件與實際版本不一致，核對版本原始碼及最小重現。

既有FA程式也依此原則收斂，重複SDK能力的部分應以SDK取代，而非永久保留兩套。
這項方向不授權刪除或重寫原帳本、UNKNOWN、批准、回條、用量或已結案工作；
也不代表目前所有自建Module都已完成瘦身或原生驗收。

## 2026-09-20 使用流程修訂：每筆工作與共用預算，不設全局短窗口

使用者已明確要求取消全局短時測試窗口，改以每筆工作及原POC累計預算控制。
此節覆蓋後續web-first正常流程的「全系統每一／四小時到期、重新開窗才能繼續」要求；
不改寫先前run的期限、批准、回條、unknown或停止結果，也不回填已驗收狀態。
這是正在實作的新流程，完成情形仍以[進度](progress.md)與實際原件為準。

採用Grok Bot的行為方向是：使用者說明成果與決策邊界、已授權低風險步驟自主處理、
可更改進行中的工作、工作與結果持續可找回。它不要求移植其共享電腦、憑證或未公開內部實作；
也不能將「少打擾」解讀為停用授權、防重送或資料保護。
參考[官方協作說明](https://docs.x.ai/grok-bot/chat-and-collaboration)及
[批准與安全界線](https://docs.x.ai/grok-bot/security)，既有研究界線見
[行為設計第4.4節](FA-AGENT-BEHAVIOR-DESIGN.md#44-grok-有界補證據可結案不是運行保證)。

| 控制 | 後續正常流程 |
|---|---|
| 全局一／四小時使用截止、到點鎖住整個介面 | 移除。不是延到遙遠日期，也不是背景自動重批或默默修改legacy期限。 |
| 執行時間、模型請求次數、排隊與容量 | 綁每筆工作／attempt。開始前保存適用額度，重啟與未知結果不歸零。 |
| 費用 | 同一POC累計USD110，內含USD10收尾保留額；不是每work或每次部署各110。核已觀察高水位、在途與預留用量及新工作的有界成本。 |
| 已保存結果、狀態、確認收到 | 依當前人員、工作與成果存取權核對，不要求模型仍在執行，也不受原執行截止一併鎖死。查收仍需真回條。 |
| 已批准範圍內的低風險讀取、保存與原人收件 | 不逐小步重問、不要求每次新開operator窗口；超出原批准範圍才另行處理。 |
| 登入憑證、撤銷、私有資料、具體外部效果 | 保留真正的有效期、目前存取權與必要effect-point檢查；取消全局窗口不代表永久憑證或任意對外操作。 |
| 停止、未知效果、防重送、共用額度原子預留 | 保留。完成／取消不能造出不存在的回條，不用換ID迴避未知結果。 |

一次部署或歷史狀態轉換可以有自身短期操作批准；不得讓它的截止成為安裝後所有正常工作、
讀取結果及ACK的共同截止。穩定使用政策與每工作執行額度須明確區分，
舊schema到期拒絕不能靠缺省值、寬鬆解析或修改原資料被靜默解除。

成本資料過舊、預留不足或超出共用預算時，停止新的付費工作並顯示原因；
保留有限的查狀態、取消與原結果收件能力，不以費用阻擋抹除未履行責任。
Azure Cost Management[官方文件](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets)
明列budget通知不會停止資源，成本通常延遲8–24小時，budget每天評估。
因此預算警示不是硬停機開關，估價與應用層預留也不得宣稱供應商帳單硬上限；
執行器、模型用量及既有資源耗用仍需主動控制。

已確認執行終止、成果持久保存且容量已正當釋放的A工作，即使原人尚未ACK，
也不得僅因A的交付責任pending，阻擋所有獨立B工作接單。
A仍保留待交付狀態及必要材料；B獨立核對目前權利、剩餘共用預算及容量。
相反地，A仍屬`capacity_uncertain`或舊執行者未確認停止時，不得假釋其槽位給B。
一次歷史安裝轉換所需的特定drained條件，不能直接升格為正常平台的全局接單前提。

必要行為驗證以新模式跨越舊全局截止後仍能在有效權利及剩餘額度內接受合法工作、
單work逾期後不再產生新效果但原人仍可查收、超預算／撤權／錯身份仍拒，
已終止但欠ACK的工作不阻塞獨立新工作，以及重啟／雙擊／ACKloss不重做工作為準。
固定metrics是首個有界驗收案例，
不是所有FA工作的永久內容限制；首版未完成的工具或任意副作用不因此自動開放。

## 1. 最新基準與適用範圍

已發布程式基準為 `f097fa687b0e331879e1d3c8d3b5d259b96b46e1`，位於 `origin/neallee2012-hosted-sandbox-poc`；本規格工作樹已快轉至此基準。對齊時 `origin/main` 仍是 M1 的 `3392c8e`，不能以 main 或舊提案判定目前開發進度。

| 能力 | 已有基礎 | 本規格新增部分 |
|---|---|---|
| M2 助理 | [文字工具循環、同 SDK checkpoint 跨 process 恢復、A→B 交接發布通過](M2-ASSISTANT-POC.md)；SDK 1.0.13／runtime release 1.0.83 | 跨入口持久接單、交付責任、一般背景工作的喚醒與 execution ownership；不重建第二套 SDK 迴圈 |
| M2 自主 UI | `M2-UI-01` 已測失敗、`M2-UI-02` 未測；160 次額度耗盡 | 保留獨立待辦；不是本批 M4 的前置條件，也不因本文而取消 |
| M3 成果與批准 | [真檔案／不可變版本、真人下載及 SOP v1 批准→v2 拒絕→v3 批准→consumer 採用通過](M3-ARTIFACTS-POC.md) | 一般工具操作授權、撤銷、跨通道交付與恢復；沿用既有版本、批准者驗證及來源核對 |
| M3 完成口徑 | `core_e2e_pass=true`、`full_scenario_pass=false`；歷史格式 FAIL 與 retired unknown 保留 | 不將新增契約回填成舊 run PASS，也不重送舊操作 |
| M4 本批 | **已批准、實作中、未提交／未雲端驗收**；五案 fixture／離線 checker 已有，隔離 Memory、context、gateway／counter、Hosted 與 native collector 正在接線 | 本批先完成既定驗收；本文的產品化行為增量另排在其後 |

M4 進度依 2026-09-15 開發工作階段回報及 14:13 +08 的唯讀工作樹觀察；不是 f097fa6 已包含 M4。參考檔為 `controller/m4_context_eval.py`、`shared_context/m4_context.py`、`m4_config.py`、`m4_fixtures.py` 及該工作樹的計畫文件，未複製其未完成程式到本分支。固定 fixture SHA 為 `8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090`；後續整合須以實際 M4 提交與收尾結果更新基準，不能把 WIP 當 release。

M4 現有五案、上限與不包含項目只在[開發計畫的 M4 本批段落](POC-ANALYSIS-AND-PLAN.md#m4-本批已批准實作中)維護。本文不是新一輪雲端執行授權，也不讓舊 M2／M3 counter 重新開始。

## 2. Module 與可重用的 Seam

| Module／Interface | 責任及輸出 | 實作落點 |
|---|---|---|
| `AgentRuntime.accept(event)`、`read(cursor)` | 驗證並保存事件，回持久 admission receipt；管理 conversation 單寫者、交付責任與 outbox | FA 耐久控制；不是模型先說「收到」再補保存 |
| `JobRuntime.start(spec)`、`observe(job_id)`、`cancel(job_id)` | 回 job receipt／目前狀態；管理 owner、attempt、容量、checkpoint、completion 與核對 | FA 控制與 Hosted Adapter 接線；沿用 `AssistantEngine.dispatch`／SDK runner |
| `ToolBroker.execute(intent, execution_context)` | 回 `operation_id`、明確結果狀態及 receipt／結果參照 | 每次工具 dispatch 的權限與副作用 Seam；沿用現有受控 tools、M3 ArtifactService |
| `ContextBuilder.build(turn_ref, budget)` | 回有來源的 context 及 manifest；不改權威工作或批准 | Hosted 組裝與受控 Context Interface；接續 M4，而非複製另一套檢索 |
| `MemoryGovernor.propose(candidate)`、`resolve(query, access_context)` | 候選審核 receipt、有效知識與來源；提案被接受不等於抽取完成 | 沿用 Memory Adapter，加治理；不放寬 POC 的合成資料入口 |

以上為 FA 邏輯 Interface，不是現成 SDK 方法或五個獨立部署。Hosted Harness 負責 SDK events／hooks、工具接線及本輪行為；FA 控制保存跨程序的權威狀態；平台／工具端負責實際執行。Hosted 不引入 Table／JWT server 相依，亦不取得原生資料權限。
上述Interface受本文件SDK-first修訂約束：session與回合生命週期委由SDK，
FA只保留可指出業務必要性的狀態及Adapter，不從這張邏輯表推導另一套通用runtime。

現有 [AssistantEngine](../src/fa-shared-context-poc/shared_context/m2_assistant.py) 已有私人 run state、binding、`pending_or_unknown` 操作與明確 resume；它的 threads／process locks 不當成全域 mailbox／容量。現有 [ArtifactService](../src/fa-shared-context-poc/shared_context/m3_artifacts.py) 的 `create`／`upload`／`reconcile`／`decide`／`policy_context` 直接沿用。只有新增責任才新增 Module，不要求先搬目錄或改名全部 M2／M3 程式。

### 最小持久資料

| 紀錄 | 必要欄位／不變量 |
|---|---|
| Event／admission | `event_id`、`schema_version`、`runtime_version`、`root_request_id`、`conversation_id`、可選 `work_id`、`causation_id`、來源及防重播參照、payload reference／digest、伺服器 sequence、可重建的 admission receipt |
| Work／execution | `work_id`、目標／必要成果集合及驗收條件版本；`job_id`、父 job、輸入 digest、`execution_owner`、`execution_epoch`、attempt、binding、狀態、checkpoint／result reference、檢查結果與來源、deadline、取消請求、root budget 參照 |
| Delivery obligation | `obligation_id`、原 request、授權收件人／通道、結果類型、目前 delivery version、`required_confirmation_level`、履行狀態、下次檢視／到期時間、責任者、持久模型補交 attempts |
| Delivery／outbox | `delivery_id`／version、不可變 payload／digest、成果版本參照、`message_kind`、`send_outcome`、`confirmation_level`、目的端身份／路由、通道能力版本、送出授權參照、receipt、retry／核對紀錄 |
| Operation／授權預留 | `operation_id`、attempt、target／args digest、`canonicalization_version`、實際執行描述／digest、解析資源 ID／必要版本與內容保護參照、execution epoch、批准／政策／撤銷 revision、預留次數／金額、授權生效紀錄、執行結果、`effect_state`、原 attempt 終局性證據／能力版本、目的端 receipt、錯誤／核對責任 |
| 履責材料保護 | 最小合規輸入／結果／receipt／恢復材料的精確版本／digest、scope、所屬 request／work／operation／delivery、保護模式／配置版本；pin 模式另含 pin ID／generation／狀態，不回收模式含受保護範圍與依據；必要保留窗口及回收狀態，識別不代替實際內容 |
| Root budget／控制面 | root／入口／Team 配額版本、已預留／耗用額度、pending 計數、child／retry 共用關聯、due 去重鍵、掃描 cursor／上限；未知用量不填零 |

這些是新 runtime 紀錄，不塞進或改寫 M2 已 published 的 Work。身份、Team、來源與授權由伺服器核對後寫入，不信任模型或外部 payload 自報。正文可以保存為受控內容參照；不把 tokens、cookies、credential 或完整私人對話存入遙測與語意記憶。

新 Interface 沿用 `StrictModel`／明確 Problem 錯誤的慣例：未知欄位／schema version、缺必要關聯、無效 enum 或非有限數值拒絕；不自動降級為舊命令。ID 為不可變 opaque string、digest 為 SHA-256，時間為帶時區 timestamp；缺 receipt 保持缺省而非填假值。讀取 cursor 綁定 conversation 與已授權讀者，條件寫入以預期 revision／ETag 防止覆蓋；同一請求的重試先核對原 receipt。

## 3. BC-01：事件、接單與連續對話

對應 FR-27、FR-30、FR-33。

1. 入口先驗身份、scope、payload 與版本，再保存 event、work 關聯及必要的交付責任；成功後才回 admission receipt。保存失敗必須明確失敗，不回成功形狀的 ack。
2. `source_kind` 至少分 human、routine、agent、tool_result、system，另保留內容 provenance。Web／文件由受控入口映射至 tool_result 或外部內容參照，仍標為 untrusted；system 只表示 FA 產生的事件，不能替所攜外部內容升權。引用內容、工具結果與 agent FYI 不因指令文字或 `"role":"user"` 而取得人類授權。
3. 重送沿用原 `event_id`；相同 ID／digest 回原 receipt，相同 ID／不同 digest 拒絕。不同合法交辦不可只因內容相同而合併。
4. 每個 conversation 的狀態轉移只有一位持有效權的寫入者，使用條件更新，不僅依靠 SDK／process lock。長 job 提交後釋放模型回合；completion 以新事件接續原 work。
5. 追加指示只在安全點作用於指定 work／revision。可合併尚未處理的同來源普通補充，但保留原 IDs；批准、撤銷、取消、跨來源事件不合併。
6. 接單、查狀態與取消受理不需要先取得模型槽位。已接受但尚未推理必須顯示 queued，不把 ack 冒充正在執行。

### 接單的提交點與啟用前提

每個入口沿用第 6 節的原子提交集合。admission 的去重紀錄、必要 work／obligation、固定 receipt 內容及後續處理意圖不可分裂；receipt 可由同一已提交紀錄確定性重建，不另做一次容易遺失的寫入。只有權威儲存確認提交後才算已接單。僅在確認未提交且沒有在途提交時才判為未接單；commit 結果不明回 `admission_unknown` Problem 與原 ID 供核對，不當成功或另建工作。重送先核對整個提交，不因只找到 event 就虛構 receipt。

開啟新 admission 前，必須固定原子儲存 Adapter、處理版本及能在入口 process 消失後運作的 consumer／timer，並有可運行的重送、有限掃描與責任接手方式；配置不完整拒絕啟用。這是 BR-01／02 新能力的依賴，不是把所有 wake 工作延至 BR-03，也不是本批 M4 新門檻。

### 有界接單與必要成果

入口、Team 與 root request 必須設定有限 pending 配額、事件／payload 大小、排隊期限及控制面預算；配額檢查與 admission 預留不可分裂。額滿在接單前回明確 Problem，不能先承諾再丟工作。不占模型槽不代表沒有入口限額。

在接單或明確修訂 work 時，保存結果種類、必要成果集合與驗收條件／版本及來源；人類判斷項目另列，不能用模型自述代替。一般短答不強求複雜驗證器，但已知的必交項、格式、scope 或必要操作不得事後降低。核對證據綁定同一 work revision；缺項、答錯工作或必要操作仍未知時，不因有可下載的檔案就進 succeeded。

## 4. BC-02：工作完成與結果交付分離

對應 FR-27、FR-32、FR-33。

| 軸 | 狀態 |
|---|---|
| 工作執行 | `accepted → queued → running`；可進 `waiting_dependency`／`waiting_approval`，解除後回 queued；結果可轉 `ready_to_finalize → succeeded`，亦可明確 failed／cancelled |
| 交付 | `pending → acked → final_prepared → delivered`，亦可 `pending → final_prepared`；另有 `delivery_unknown`／`delivery_failed` |
| 外部操作 | `prepared → dispatching → confirmed / failed / outcome_unknown`；另記 `effect_state = not_applied / applied / partial / unknown`，執行失敗不推定效果未生效 |

工作 failed／cancelled 也需要交付結果；`succeeded + acked` 仍欠 final。合法等待須有原因、可核對的通知狀態、指定觸發／責任者，以及有限的 `review_at`／到期處理。通知失敗仍需交付核對，逾期重新檢視或升級責任，不自動批准，也不因填了負責人就永久排除 watchdog。外部結果未知時不可宣稱成功或已回滾；即使已告知使用者，operation 的核對責任仍保留。

只有持有效 owner／epoch 且取得容量的 worker 可進 running；只有結果已保存並通過任務必要檢查、沒有未核對的必要操作，才可進 succeeded。終態不靠重送重新開啟，後續修訂建立新的關聯工作。`delivery_unknown` 僅在原 receipt 核對或通道冪等契約支持時轉回送出流程，`delivery_failed` 的再試須保存原因與原 ID，不清除歷史。

`message_kind` 必須分 `ack`／`progress`／`final`／`failure`；`send_outcome` 必須分 `emitted`／`suppressed`／`failed`／`unknown`。模型文字、reaction、批准卡、widget、內部 subagent result 及 suppressed success 都不清除最終交付責任。重複 delivery 的去重只能引用原有效 receipt，不能捏造新發送。

完成交付需同時滿足：結果及所引用成果／來源版本已保存、收件授權成立、final／failure 與原 request 對應、不可變 delivery payload 已落盤，以及該 obligation 事先保存的確認要求已由有效 receipt 支持。失敗結果可以沒有成果檔，但必須明列已知效果、未知部分與後續責任，不能為了交付 failure 虛構成果或工作失敗。

### 通道、回條與送出授權

#### 2026-09-30 本機修補：未收件責任不占用新接單容量

自由指令未ACK仍保留原交付責任，不因job成功、純GET或畫面顯示自動fulfilled；
但不能把所有責任塞在單列、越過48KiB後封死其他新admission。
新的instruction成員改由小型head、有界ID頁及每obligation成員列保存，與原obligation／receipt同EGT。
原v1項目、非instruction與原ACK語意保持，原回條／request／hash不重寫，不刪歷史或偽造ACK。
scan與current CAS仍能定位未收件項；admission／ACK只讀所需成員，不載完整backlog當新gate。

採用分reader相容、writer啟用兩階段；第一筆新page後只能回退到認得新kind的reader相容版。
這僅防止資料遺失與開機／接單錯誤，不新增操作批准層。精確開關、直接反例與限制見
[有界pending索引](../src/fa-shared-context-poc/README.md#未收件責任的有界索引2026-09-30pr1codeoffline)。

`confirmation_level` 分 `persisted`、`provider_accepted`、`client_received`、`ui_rendered`、`human_read`。每個通道／驗收 fixture 固定能力版本、supported levels、`max_confirmation_level` 及各 receipt 的確切語義；obligation 另存 `required_confirmation_level`。不靠 enum 排序推定一層自動證明其他層，也不把 Grok 報告的機械步驟對映成這些 enum。要求超出能力時，啟用／接單前拒絕不支援的組合；接單後能力喪失則保留未交付責任，不暗降要求。僅 FA outbox 落盤的 persisted 不能冒充外部通道接受。

有效 receipt 必須由受信任 Adapter 核對，綁定 obligation、delivery ID／version、payload digest、所引成果版本、目的端 tenant／account／recipient／channel／thread，以及能力版本與確認語義。錯版本、錯目的地、錯 digest、超出能力或缺 receipt 均不完成目前交付；遲到回條只記到原 delivery 供核對。要求或路由變更須有明確授權與版本紀錄，不能沿用舊回條消除新責任。

API／UI 的 `delivered` 必須同時帶可核對 receipt 的確認層級、要求與能力版本；缺少即非法組合，不只靠顯示文案提醒。SSE write／transcript insert 不是 UI render；沒有證據的層級保持未知，不要求所有工作等人類已讀。

首次 send、retry、替代通道與成果下載，均須在內容離開 FA 前經第 6 節的當前授權 gate；outbox sender 使用 ToolBroker 或等效強制 Seam，不能只在寫 delivered 時核對。綁定目前 tenant／account／收件者／通道／thread、內容與成果 scope，失效目的地不得發送。改路由或通道須重新核准適用範圍、建立新 delivery 並保留舊紀錄；原 delivery 的重試不換 ID。撤銷與在途送出的先後按同一授權生效點判定，不承諾追回已送出的內容。

Sender 核對自己的有效處理權／epoch 與原 obligation 關聯，不要求已結束的模型 job 繼續占槽。回條證明的是原送出及其授權時點，不因回條到達前發生撤銷就重送；後續下載仍重新核對當前讀取權。

### Watchdog 與尚未履行責任

Watchdog 對到期、缺 final 或等待需重新檢視的 obligation 建立 `delivery_due`；每個 obligation／處理世代只允許一筆未完成 due，timer 重送沿用同一 ID，不每個 tick 新增一筆。先用保存結果作非模型補交，不能重做工作。

本規格的模型補交硬上限為**每 obligation 2 次**，計數單位為 FA／SDK 發往模型入口的請求。每次可能送出的補交模型請求，先原子預留 attempt 與 root budget；並行 due 只能取得剩餘名額中的一次。生成失敗、timeout、發出後中斷或是否發出不明都占次數；為保守有界，已預留但未能證明送出的名額也不自動退回。重送同 attempt 先核對，不再呼叫；新請求（含 SDK／client transport 自動 retry）必須再取名額，禁止繞過帳本。重啟、換 consumer、delivery version 或通道不能歸零。無法在送出前約束 retry 的配置不啟用自動模型補交；不宣稱此上限能控制或計量供應商不可見的內部重試。

耗盡或預算不足就停止自動模型補交，保留 `needs_attention` 原因與責任者。已授權通道可用且有預留通知額度時，確定性 failure 通知亦走同一 outbox／授權／receipt 契約；它可以說「結果未能交付」，不能把已成功或未知的操作寫成「未生效」。所有通道不可用時不假稱通知成功。

Obligation 的履行狀態與自動重試狀態分開：只有目前 final／failure 達到上述交付契約才標履行。`delivery_failed + owner`、needs_attention、停止重試或記日誌都仍在未履行庫存；工作取消、失敗通知與操作核對各自保留語義。

開始、阻塞、需批准、重要成果、完成才通知；heartbeat 留在狀態欄，不逐次發聊天。短答仍須先持久保存 event、必要 obligation 與 admission receipt，只省略獨立可見 ack，走 `pending → final_prepared`；需要模型時仍取 owner／容量。保存失敗不得先送 final，也不虛構 ack 回條。

## 5. BC-03：執行歸屬、背景工作與取消

對應 FR-09、FR-12、FR-29、FR-33。

`execution_owner` 是有權啟動／恢復 job 的執行端，不是 Work／Artifact 的編輯 owner。每次 dispatch／resume／副作用提交都核對 owner、epoch 與 binding。Hosted／SDK session 可更換，但不能默認新 session 擁有舊工作；平台原生 handler attempt 與 FA job 必須明確對映，不能各自 timeout 後各重跑一次。

背景工作的必要順序是：保存 job／attempt 與觀察意圖 → dispatch → 保存 execution ID 關聯 → 確認 completion 監聽或核對機制已接上。分開記錄 started 與 observation armed；先到的 completion 依既知 job／attempt 進持久 inbox，待關聯核對，不因順序不同丟棄。spawn 結果不明先核對，不再 spawn。

完成結果先保存，再送 completion event；父代理忙碌就入 inbox。重複 completion 不重做結果或副作用。父代理可處理後 silence，但原人類的 obligation 不隨之消失。中斷後只能依 checkpoint／receipt 接續，不宣稱 RAM、thread、shell PID 或任意視窗會復活。

全域實際執行容量最多 4；第五個排隊。首個落地切片可以只開一槽，但取得／釋放都走同一容量契約，SDK subagent 不走旁路。等待批准／依賴只有在已 checkpoint 且確認沒有仍跑的 shell／browser／模型工作時才釋放；純讀狀態不占模型槽。互動保留槽與公平權重仍是第 12 節待選政策，不暗加第五槽。

容量作用域為整個 FA 組織工作空間，不是每 conversation／每 process 各 4 槽。一槽代表一個可獨立推進的 executor job 與其受控工具程序樹，不是每個 PID 一槽；並行的 SDK child 或獨立 shell／browser job 必須另取槽，不得掛父 job 名義藏併發。串行 helper 或有可信 idle 證據的 browser 程序不另算活躍 job。驗收保存 permit→job／attempt→實際活動及停止／idle 的 Adapter 證據，不能只數 job rows、失效 epoch 或 PID。

Lease 過期代表失聯，不代表舊工作停止。stale epoch 不得更新狀態或發新工具；無法確認舊 executor 停止／失效時保留槽位並顯示 `capacity_uncertain`。`cancel` 先回取消受理，停止新 dispatch，再確認既有 executor 停止；有在途未知操作時列清單，不宣稱全部撤銷。

Queue completion 為正常路徑，另有非模型 reconciliation 處理失聯／漏通知。正式 wake 必須有可運行的 consumer／timer；單靠 Table 排程列、API process、私人檔案或 F3 原生恢復，不能承諾 FA 端必定按時醒來。

### 2026-09-25 本機切片：不可再開始的 SDK entry 拒絕

本節是 BC-01／02／03 的必要本機 Implementation 契約，**不是逐欄人工批准、部署、
雲端停止或 IAM 授權**。預設不啟用；只有 server-owned instruction profile 明選
`sdk_start_profile="fa-sdk-start-gate-v1"`，才可使用以下版本。缺省不增加舊序列化欄位，
不重寫舊 `SdkStop`、NativeReport、TurnArchive、LocalExecution 或已持久回條。

- 原 assignment 取得 `fa-sdk-start-binding-v1`，綁原 session／SDK identity／job、
  task／profile／execution digest、worker configuration 與實際 installed source hash。
  使用者文字、submit body、model/tool 參數不能啟用這項能力。
- 原 private executor journal 的 `fa-sdk-start-gate-record-v1` 只有兩種不可逆轉移：
  `open → entry_attempted`，或 `open → closed_before_entry`。兩者在同一 SQLite
  `BEGIN IMMEDIATE` transaction 互斥；不以未見 PID、沒有 receipt、phase 或 exception 推定未執行。
  `entry_attempted` 必須在實際受控 `CopilotClient`／runtime 建立或啟動嘗試前確認持久化；
  只要 intent 曾成功取得或寫入結果不明，就不允許 no-SDK 終局，也不自動重新呼叫 SDK。
- 本版只對已確認 acquire 後、實際 SDK entry 前的固定 approved-skill source／ACL 拒絕
  嘗試關閉 gate。原 owned dispatch thread 確實 join／terminal、gate 的 close 回條已確認、
  原 code／owner／journal 一致後，可信 observer 才能產生 `fa-sdk-no-start-v1`。
  版本與必要欄位須明示；未知／缺版本、未啟用 binding、仍活的 thread、lost journal write、
  restart 後無原 owned thread、曾嘗試 construction/start、transport unknown 都不能補成安全終局。
- Hosted 使用獨立 `sdk_not_started` report phase；不是原 `sealed` child-exit report。
  原 MI／process capability／fresh probe／launch／claim／generation 檢查照常。API 另核
  server opt-in、精確 code/config/task/owner、第一代原工作及沒有 model intents、child、
  已知／未決操作、結果或新 SDK 保存。取消或 owner／epoch 競態仍保守拒絕。
  這依賴原受控 Adapter 與受保護 journal 的信任假設，不宣稱 source hash 是 runtime attestation。
- 合法 report 在原 CAS／once transaction 一起保存原工作 failed result、
  `fa-no-sdk-turn-v1` 及 native evidence，再走原 result-forwarding／failure 查收。
  **沒有 `SdkStop`、PID／exit code 或本回合 `TurnArchive`。** 原 user/task、首次失敗、
  費用／unknown、早先 archive／答覆保留，不改寫為成功或零支出。
- 本切片不做一般 no-archive continuation。對話明示 `SDK_NOT_STARTED_RESET_REQUIRED`；
  原失敗已轉交後才開放既有使用者明確 reset，不自動換 SDK session，也不偷偷從較早
  checkpoint 跳過本回合原話。關閉新 writer 不會使已保存的新版本失去可辨識 reader。

舊正常 SDK stop、原未完成工作及 unclaimed launch fence 各維持原契約。新的 gate 不是
第二個 agent loop、共享權威儲存或雲端 stop：Local Adapter、合成 Hosted observer 與真正
Azure 執行的證據分開。具體程式、各批 RED／GREEN、預設與未驗限制見
[Shared Context README 的 no-SDK 切片](../src/fa-shared-context-poc/README.md#sdk-entry-前的版本化拒絕終局local預設-off)。

### 2026-09-25 本機補充：記憶拒絕的 v2 entry gate

本節只新增 PA-13／TC-12 的受控記憶 pre-entry 收尾，不把上一節明定 skill-only 的
v1 回條擴充成記憶批准。Server 必須在原 `fa-instruction-work-memory-v1` profile 另選
`sdk_start_profile="fa-sdk-start-gate-v2"`；預設仍 OFF，其他 prompt profile 不接受 v2。
沒有新 prompt、記憶讀權、scope、Memory Store 呼叫、模型 token 上限或自動恢復流程。

- v2 的 binding／gate record／no-start disposition／NoSdkTurn／result proof 分別明示
  `fa-sdk-start-binding-v2`、`fa-sdk-start-gate-record-v2`、`fa-sdk-no-start-v2`、
  `fa-no-sdk-turn-v2`、`fa-sdk-no-start-result-v2`；Local evidence 為
  `fa-local-no-sdk-evidence-v2`。Reader 驗各版本與原 profile 的精確配對，混版、缺版、
  未知版、用 v1 skill code 充作 v2 memory refusal 都拒絕。原 v1／None 的 bytes、
  skill-only allowlist 與記憶拒絕仍 unresolved 的行為保持；不改已保存的舊回條。
- 沿原 journal、SDK constructor 前的 effect point、原 owned-thread join、claim／probe／
  report 與 CAS，沒有第二個 ledger／supervisor／recovery engine。只有 acquire 已確認、
  gate 確認 `closed_before_entry`，且原 observer 證明 thread 已完成，才可能收尾。
  記憶來源／claim／human review 的撤回、失效、版本變更、scope／文字 policy 拒絕與
  完整來源確定超限，使用固定 `SdkMemoryRefusalCode` 子集；不是整個診斷 allowlist。
  新文字 policy error 的 HTTP 分類／client 解碼只由 server 的 v2 binding 啟用。
- `ASSISTANT_SOURCE_CHANGED` 是完整 snapshot 的通用不一致碼，可能只因合法 observer
  更新 job row，不足以證明知識失效；不在 v2 closure allowlist 中。保留原 safe diagnosis，
  但不得因此關 gate、產 no-SDK disposition 或開放 reset；來源核對與完整 read-set 不放寬。
- Catalog 已保存的 `work_memory_source` 是原操作 provenance，不是 SDK state；已確認
  capture 後的確定來源拒絕可沿上述證據收尾。**capture commit／ACK 不明不在子集內**，
  缺 claim／原件／receipt、unknown token count、未知傳輸／例外仍保留原未決狀態。
  8192-byte 每原件、8192-byte provenance、24576-byte 完整回應及原 read-policy/read-set
  predicates 全部不變；超限不截斷、不刷新來源、不重送。
- 原工作第一代／零 model intent／零 child／無新 SDK state／無已知或未決效果的 consumer
  條件不變。即使 constructor 失敗無 PID，只要 entry 已嘗試，或工具是在 entry 後拒絕，
  都不能產生 no-SDK 終局。取消、owner／epoch／config 改變、過期 probe、非原 supervisor、
  unjoined thread、journal／claim／acquire ACK 不明也不補成停止證據。
- 當 current policy／身份已撤到原 report 或結果 CAS 都不准，即使已有 closed gate，
  仍 **blocked／UNKNOWN**，不繞過目前授權。Report ACK 不明先保留 unknown；只有沿既有
  immutable report／receipt／sealed evidence 對帳才能消費原結果，不以重送或新 proof 代替。
  晚到新 probe 不能重寫已保存結果。
- 合法收尾仍為原 failed notice／delivery；沒有 SDK answer、`SdkStop`、PID、exit code 或
  新 `TurnArchive`。原 user instruction、早先 archive、費用與未決紀錄保留。對話只開放
  已有的 explicit reset；不能同 SID 繼續、不自動 reset／換 job、不退款或取消 unknown。

LOCAL 的 signed API、synthetic Hosted journal/report 與固定 SDK offline control 分別記錄。
不宣稱 Azure 停止、真模型品質、私人記憶、一般 memory version transition 或完整 Target State。
實作基準與分批結果見
[Shared Context README 的記憶 no-SDK 補充](../src/fa-shared-context-poc/README.md#記憶拒絕的-no-sdk-收尾localv2預設-off)；
原 no-SDK 與記憶切片歷史結果不回填。

Job／child／retry／模型補交／delivery／reconciliation 共用 root budget，不因派子工作、恢復或換 ID 發新的總額。核對頻率、每批掃描數／時間、queue 重送、fan-out／深度與 I/O 次數都須有有限配置；掃描保存 cursor，重複 due 不增加可用額度。可預留非模型核對／終止通知額度；耗盡則停止該 root 的自動工作、保留未知效果／容量／obligation 並交責任者，不為了清帳擅自釋槽。全域健康盤點另有有限配額，不重新啟動已耗盡 root。

## 6. BC-04：自主操作、有效批准與副作用

對應 FR-08、FR-18、FR-19、FR-24、FR-25、FR-28、FR-33。

已授權讀取、局部草稿、合理檔名／格式及等價工具順序可自主處理。只有重大／不可逆結果、無法消除的目標歧義或僅人類知道的資訊才阻塞詢問。對外傳訊、支出、權限變更、刪除、排程及行為發布依現行政策取得精確批准；SOP 的 M3 批准不等於一次 Azure 操作的授權。

每次 `execute` 必須核對當前 actor／Team grant、工作委託、target／args digest、政策版本、批准範圍／次數／有效期／支出及 execution epoch，保存有效決策與理由。不能只記 enabled。若使用模型分類器，另外記其 effective mode／結果；off／shadow 不停用必要的人類批准與 scope gate。

### 2026-09-26 本機接點：拒絕尚未批准的操作

`POST /behavior/v1/bindings/{binding_id}/operation/reject`沿原指定human及當前Behavior.Approve、
owner／policy／binding核對。`fa-operation-rejection-v1`包含原request ID、decision ID、
description digest及adapter contract digest；人別／時間由server決定，不接受model／body代填。
只對已prepare、沒有grant及任何attempt的pending operation有效；已有grant沿原revoke，
已有／未知attempt及applied/partial結果不由此動作取消或改寫。

沿原同guard／CAS交易保存operation的`human_rejection`與create-only人類決定／回條，
原canonical material及effect/business結果不變。拒絕不是假grant、`not_applied`證明或退款。
新欄位缺省不輸出，舊operation bytes保持；unknown ACK沿原plan／consume／receipt處理，
consume後未有決定回條仍unknown，原request不再invoke，也不回填為已拒絕。
拒絕intent與原command plan一起保存，將decision ID綁原request／body；換request沿用同一decision
明確conflict，不讓後來的回條改變原unknown對帳。另給新request及新decision，必須是有權human
重新明示的新決定並重核目前pending狀態，不是自動retry；即使新決定成功，原request仍按原回條處理。
Approve也必須核同binding／decision的rejection intent，並把該absence predicate放入原EGT讀集；
已有拒絕intent時不建立新的grant。這不改寫歷史批准：已有plan若缺新predicate、之後出現intent，
或原operation已被人類拒絕，只能核其原receipt，沒有回條仍unknown、不重新invoke，
也不改舊read-set／digest；後續拒絕用相同或不同decision ID，都不能把原批准unknown改判conflict。
此補充只封閉既有rejection intent被approve挪用的接點，不宣稱所有不同action已有全域nonce registry。

同一approve action也不能換request沿用已規劃的decision ID。新的approval沿本binding原
CommandPlan／plan-index查原control request關聯；plan-reserve在原EGT內核讀到的index未變，
有交錯則明確conflict，不自動重試。沿用原資料格式、原請求與回條，不補造approval intent或重寫歷史。
同原request/body仍核原receipt；新的request＋新的decision須由有權human明示並重核原操作，
即使成功也不把前一unknown批准回填成success或conflict。
Revoke同樣沿原plan-index綁其action／decision與原request，並在plan-reserve核同一index；
unknown撤銷不容許換request沿用decision來改寫原回條歸屬。不同新decision仍須有權human
明示並核原grant digest／revocation revision；原unknown不因此清除，預留與既有外部效果不退款或改寫。

原操作一旦有拒絕決定，新的approve及Core authorize均拒絕；換model文字、request／attempt ID不解除。
本切片不提供重開原operation或覆寫舊decision的能力；如需改做另一項操作，仍須新的明確工作／
prepare／人類批准，不由模型自行鑄造新operation ID。這與原create-only grant／不可靜默重批准分工一致。
Approve與reject競態沿同guard裁決，不能同時落成grant和拒絕；衝突先讀原回條，不自行重試。

Preview可見去敏拒絕 metadata及原內容，但不回傳rejector OID、不把decision當外部效果結果。
API／Core為本機切片，沒有新model tool、IAM、workbench按鈕或雲端啟用；
混合版本reader／rollback未驗，不把舊reader可能拒絕新欄位說成已相容。
Current runtime的新增歷史及migration分類器明確識別此決定／intent；分類成功不解除原pending
工作／交付責任，也不讓原本model-only retirement lane接受business operations。

### 2026-09-27 本機接點：原拒絕回條的唯讀對帳與操作者 CLI

`POST .../operation/rejection-reconcile`接收原封不動的`OperationRejection`，只允許目前仍有
Behavior.Read的原configured human讀取原binding／request。沿既有CommandPlan核對人別、
binding及input digest，再以原ControlRequest查immutable receipt；整段在同一consistent-read
guard內，不reserve、consume、invoke或建立新plan。沒有原plan或decision receipt仍回unknown，
不能推定從未發生，也不將已consume但未確認的拒絕補做；查讀中的撤權／寫入競態明確conflict。
查讀不需要重新取得Behavior.Approve；讀權已撤銷仍拒絕，不因保存過回條延長權限。

`behavior_operation_cli`提供show／state／reject／reconcile，沿現有delegated human與固定原操作；
單次approve consumer另見[操作者單次批准 CLI](#2026-09-27-本機接點操作者單次批准-cli)，不把原reject preview直接當批准卡。
Reject先核原request檔與server preview的內容／adapter digest，再要求互動式確認；
CLI在唯一POST之前，排他建立並flush同目錄的`<request-file>.attempt.json`；v2保存完整
原command及hash、binding、operation與連線配置。已有attempt不得再次reject；只沿原材料
reconcile。v2在輸入檔遺失時仍可核原回條，不重建或改寫檔案；若輸入檔仍在但已變更則拒絕。
舊v1的hash-only marker維持原樣，仍須完整原輸入檔，缺件或未知format不得猜測原command。
這個本機marker不是授權或已送出的證明；崩潰、timeout、無效回應仍保留，不刪除或換ID清除。
CLI確認不取代server身份及pending gate，不新增IAM、grant、操作工具或資料表。
錯誤只輸出固定client code／HTTP狀態，不轉印遠端任意error欄位、憑證或診斷全文。
stdout的資料JSON保留terminal編碼支持的可列印Unicode；非列印／格式控制及無法編碼的字元
仍用可逆JSON escape表示。這不改request／attempt canonical bytes、hash或stderr錯誤格式，
也不把顯示文字當成新的批准或作用範圍。
CLI以scoped guard隔離Azure／MSAL日誌，離開時恢復原logger設定，其他logger不受影響；
不修改SDK檔案或harness。API httpx的no-retry／no-proxy設定不冒充Azure登入pipeline的配置。
此接點為LOCAL，需先有相符API reader才能使用，未部署或驗收mixed-version rollout。

### 2026-09-27 本機接點：原批准／撤銷回條的唯讀查回

`POST .../operation/approval-reconcile`與`POST .../operation/revocation-reconcile`分別接收
原完整`OperationApproval`及`OperationRevocation`。沿原configured human的當前Behavior.Read，
核原binding、principal、CommandPlan及完整input digest，以同一consistent-read查原receipt。
Kind由固定server route選定，不由body中的action或model文字決定；不進`_run`、不建plan、
不consume，也不重新批准、撤銷、續期、退額或恢復grant。原rejection-reconcile路徑及錯碼保持。

過期或後來撤銷的批准仍可作歷史查回，但回條中的approved uses不是目前剩餘次數或可執行證明。
查回撤銷須保留當時的expected revision，不改成目前revision。後來另一個明示決定成功，
不會把原plan／consume後未確認的決定洗成成功；沒有原receipt仍unknown。當前讀權消失或查讀
期間authority變動仍拒絕／conflict。本唯讀接點沒有新增producer或server schema；
以下CLI approve／revoke只是既有writers的consumer，仍未提供CLI execute或UI批准卡。
此讀取能力不構成新的操作授權或完整SP-20／PA-11／TC-12驗收。

### 2026-09-27 本機接點：操作者單次批准 CLI

`behavior_operation_cli approve <binding> --request-file <original.json>`讀取操作者提供的完整
`OperationApproval`，不生成request／decision ID、期限或新範圍。原uses／units只允JSON整數1；
期限仍依原明確UTC型別及server當前規則核對，不由本機時鐘或CLI文字代替server批准。
確認前核完整InlineMaterial hash與OperationDescription，包括kind、canonicalization、
adapter/version、method/target、content bytes、resources和dependencies，顯示UTF-8或base64及hash。
原show／reject preview保持原樣，不能拿摘要代替上述完整批准內容。

只有interactive terminal明示輸入approve才建立新的approval attempt-v1，保存完整原command、
hash、binding／operation與連線配置；排他create／flush／fsync完成後才單次POST原approve API。
後續agent可能依server的原grant執行，CLI本身不execute、不取消、退款或把unknown改判未發生。
已有attempt一律只reconcile；reconcile依保存schema使用原approval-reconcile或rejection-reconcile，
不重送writer。原輸入檔缺失可讀attempt中原command，存在但不同則拒絕；舊rejection v1/v2不改寫。
批准回條只表示原decision已保存，purpose明列not-current-permission；過期／撤銷後查回也不續權。
沿原Read＋Approve／Read身份、scoped auth logging及bounded API傳輸，不新增IAM、資料表、
ControlRequest格式或server授權規則。此能力為LOCAL，未對真實服務登入、發出live grant或執行外部操作。

### 2026-09-27 本機接點：操作者單次撤銷 CLI

`behavior_operation_cli revoke <binding> --request-file <original.json>`接收原完整
`OperationRevocation`，不生成新request／decision ID或改用目前revision。
先讀完整operation與typed`OperationDecisionState`，核同binding／operation／description／
adapter及原grant ID，要求grant存在、未撤銷且revision等於原command。
這些是定位及比對事實，不是授權；server仍以原human的當前Read／Approve和完整gate判定。

TTY輸入revoke後，先以新revocation attempt-v1持久保存完整原command、grant／operation及
連線配置，完成排他create／flush／fsync才單次POST原revoke API。
已有attempt只查原revocation-reconcile，不重新GET目前grant來替換原revision。
歷史回條必須對上原control request、grant ID及expected revision+1，purpose明列
not-effect-outcome；不取消工作、退款、逆轉已知／未知效果或使已發出的外部請求停止。
過期grant可被明確撤銷但不延長expiry／補uses／units。原approve與rejection v1/v2格式保持；
未知格式、缺grant、已撤銷／錯revision或缺相符reader明確拒絕，未做live操作或變更server契約。

### 2026-09-27 本機接點：目前操作決定的觀察快照

`GET .../operation/decision-state`只允許原configured human的當前Behavior.Read，
在一個consistent-read中核原operation／content／adapter及相符grant的owner scope。
回傳versioned`OperationDecisionState`，purpose固定為observation-only-not-operation-permission，
包含server觀察時間、目前policy revision、effect／business／attempt事實及nullable grant／rejection。
Grant只列原approval ID、期限、policy／revocation revision、revoked及目前剩餘uses／units；
不回subjects、actor／rejector OID欄位、budget ID或操作本文。

這不是receipt、grant或can_execute／can_reject判斷。Effect unknown不代表未批准；
grant為null也不代表沒有未確認的決定，仍需原command回條查讀。已撤銷／過期／耗用的grant
照原事實呈現、不恢復額度或更改狀態。損壞或跨scope的grant明確拒絕，讀取中變更則conflict；
沒有寫入、dispatch或新決定。既有GET operation、writers及歷史回條格式不變，CLI／工作台
不因本快照增加操作能力；它也不是現有卡片的部署前置或完整目標驗收。

### 批准綁定實際執行內容

批准依據是版本化的實際操作描述，不只是參數字串。描述須涵蓋會影響此次授權含義的 request method／target／body、腳本／計畫 bytes 及其影響效果的內容相依、解析後資源 ID／必要版本與執行 Adapter 版本；固定 `canonicalization_version` 及 digest 規則，不得省略影響效果的欄位，未知解析／canonicalization 版本拒絕。批准紀錄、operation、最後授權 gate 與 executor 都核對同一描述／digest。身份、帳號及 credential reference 另經受控通道核對，不為計算 digest 把秘密值放進一般紀錄。

`plan_ref=current`、檔名、URL、branch 或其他可變 alias 只能用於準備階段的定位。需在批准前解析並固定其實際內容／版本，按下文保護所需材料；executor 必須 consume 核對過的不可變 bytes／版本，或在效果點由目的端**原子執行**必要版本條件。先 hash alias 或可變檔案、在 gate 後重讀該名稱，不是安全綁定；另一次先讀版本再無條件執行也不夠。

批准後或最後 gate 後參照改指 B 時，只能仍執行已批准的 A，或停止並取得適用的新批准；不得自動改綁 B。若有意允許動態範圍，批准必須明列允許的 selector、可變參數及不可逾越條件，最後 gate 仍核對本次實際解析結果，不默認 alias 的所有未來內容均獲准。不能建立上述綁定的操作能力保持停用，不因帳本中的 args digest 一致就 dispatch。

### 授權生效點與效果狀態

準備操作不等於取得可長期使用的送出許可。每次實際 dispatch 前，以同一可線性化決策核對目前 grant／政策／撤銷 revision、owner／epoch、operation／attempt／digest，並原子預留所需批准次數、金額及執行預算；保存此授權生效紀錄後才可進 dispatching。共用批准／預算的所有 conversations 及撤銷寫入必須經同一序列化權威，conversation 單寫者或分開讀取計數不夠。

撤銷先於此生效點就拒絕新送出；晚於此點則標為在途，不承諾追溯撤回。生效紀錄重播只回原 reservation／狀態，不再扣額度，也不等於再次 dispatch 的許可。已提交後失聯，先核對原 attempt，不能以舊批准快照盲送。舊批准不符合就等新批准；無人類憑據的 agent 訊息不補成人類決定。政策／身份核對不可用時，副作用拒絕並回明確原因。撤銷不等 prefix 或 cache 更新。

此生效點界定 FA 管理的批准／撤銷；目的端 IAM 仍依其實際契約授權，不把 JWT 或外部權限快照當成與 FA 帳本的跨系統交易。

未知／部分效果不得自動歸還批准與費用預留；失敗試次仍計入執行預算。只有可信核對證明可釋放的未用額度才可條件更新，保留原紀錄；未生效不等於沒有模型／網路費用，估價也不是供應商帳單硬上限。

`operation_id` 與實際操作描述／digest 在第一次外部 dispatch 前持久保存，重試沿用，不能換 ID 躲掉未知結果。同 ID 不同實際內容／參數拒絕。執行結果與 `effect_state` 分開：`not_applied` 表示**未生效且原 attempt 已不可再生效**，須有下述權威終局性證據；`applied` 記已知效果；`partial` 記每個已知子效果與未完成部分；`unknown` 保留核對責任。HTTP 500、shell 非零退出、timeout 或本機 exception 都不證明 not_applied。

### 未生效證據的終局性與重試

「截至目前觀測無效果」另存觀察結果／時點，不能直接轉 not_applied。非冪等自動重試須同時證明沒有已發生效果，以及原 attempt 的所有在途投遞已被終局拒絕／撤回，或在**真正效果點**被有效 fence，之後不可能遲到生效。保存證據來源、所涵蓋的 operation／attempt／目的端、能力契約版本、終局狀態或 fencing token 與適用範圍；本機自報終態不能代替目的端保證。

一次權威 negative query／查無紀錄、cancel 受理、關閉 connection、kill 本機 caller、lease／epoch 過期，均不足以排除目的端仍在排隊或執行。無法證明終局性時保持 unknown，不發出另一個非冪等 attempt，也不據此釋放可能仍會耗用的預留。spawn 與送訊等操作若要採此重試分支，也須滿足同一條件。

另一安全分支是目的端冪等契約仍在有效期內，且覆蓋**整個重試單位、相同實際內容與所有可能重疊的投遞**；重送沿用同一冪等 key，無須額外證明原 attempt 終局。兩個分支都須重新經當前授權／預算 gate。query-only 能力、已過期 key 或只覆蓋部分步驟的冪等性，不支持整包重送；partial 仍須按已知效果核對可重試子操作，補償亦另受批准。已確認效果不重做，必要 partial／unknown 未核對前 work 不進 succeeded。

必要反例為「保留在途 A → 權威查詢無效果 → 不得發 B／退預留 → 放行遲到 A」；對照終局拒絕／有效 fence 後才允許新非冪等 attempt，以及有效整單位冪等 key 下可安全重送的分支。只有 negative query 能力的 Adapter 必須明確拒絕非冪等自動重試，而非等待一段任意時間後改判 not_applied。

SDK hooks、shell、browser 與 connector 必須受相同授權結果約束。已有廣泛 credentials 的 shell／已登入 browser 不因外包給 ToolBroker 就安全；不能限制其副作用的工具不開放自動執行。Secrets 留在受控憑證通道，不進模型、事件正文或 Memory。

### 首次一般輸出的秘密防護

憑證通道與一般內容通道分開。工具／connector／SDK Adapter 在 success、error、stream chunk、exception、retry 及 checkpoint／resume 內容**首次離開受控憑證處理範圍前**，作型別化允許清單、敏感欄位隔離／遮罩；一般工具結果、模型輸入、inbox、trace／log、checkpoint、Memory 及使用者輸出都不能先收到原文再補去敏。ContextBuilder 與持久寫入入口另檢查合規結果及處理版本，但不取代最早的 Adapter。

無法安全分類／處理就拒絕輸出或保存該敏感部分，回不含秘密值的 Problem 及必要非敏感狀態；不把完整原文藏進例外／debug log，也不以刪欄後的空 success 掩蓋缺結果。文字 regex 不是任意秘密偵測保證；無可用安全輸出契約的工具／影像／事件路徑不開放自動回流。後續 B09 只用合成秘密標記，不能以真 token 驗證。

### 原子提交集合與跨服務核對

下列每一列的必要資料必須在一個有界、條件式提交中成立；適用的配額／版本／去重檢查亦在同一提交。沒有後續工作時明記終止原因，不虛構 wake。

| 轉移 | 不可分裂的最小集合 |
|---|---|
| admission | event ID／digest／防重播與 runtime 歸屬、去重判定、必要 work／obligation、必要材料保護模式／版本與引用（pin 模式含有效世代／附著）、固定 receipt 內容、配額預留及後續處理意圖 |
| 消費事件／completion | inbox 去重／消費狀態、預期 revision／owner 的 work／job 轉移、受保護的結果／恢復參照、受影響 obligation／outbox 與後續處理意圖 |
| 準備 final／failure | 核對後且受保護的結果／檢查參照、obligation 的目前 delivery version、不可變 delivery payload 的有效保護參照及 outbox 送出意圖 |
| 套用交付回條 | receipt 關聯核對、該 delivery／outbox 狀態、對應 obligation 的履行判定及後續處理意圖 |
| 取得模型補交名額 | due 世代核對、attempt ID／計數、root budget 預留與接續意圖 |
| dispatch 授權 | 共用批准／撤銷／政策與 epoch 判定、operation／attempt、實際操作描述／canonicalization／解析版本／有效內容保護、次數／金額／預算預留及授權生效紀錄 |
| 材料引用／回收判定 | 精確材料版本、保護模式及適用的 pin generation／有效或失效狀態、所保護的未結引用、回收世代／排他宣告；不得與上述引用附著互相漏看，不回收模式禁止回收宣告 |

沿用 Table 時，BR-01 必須固定同 table／PartitionKey 的 entity 配置、批次上限與有界 Interface；不是「同 Team」就自動有交易，也不用多筆獨立 ETag 冒充原子性。跨分區的共用批准／配額若無等效可線性化 Adapter，相關新功能不得啟用。超界提交在接單／轉移前明確拒絕或拆成各自可追蹤的工作，不把同一原子集合切成部分成功。

Blob／queue／外部目的端不在上述交易內。大內容先保存、核對不可變版本並建立下述存續保護，再提交控制紀錄；只做一次 Blob 存在性檢查不夠。queue 由已提交的處理意圖發送，漏送以有限核對補上。外部接受後本機未記到時保存 unknown，依原 key／receipt 核對，不承諾跨服務或任意外部 exactly-once。未接上真實原子 Adapter、材料保護與 wake 前，只能報離線契約結果，不能宣稱對應 Hosted／儲存恢復保證通過。

### 必要材料的 pin／引用存續與回收

未結責任所需的最小合規輸入、實際執行內容、結果、receipt 及恢復材料，從準備引用起到可合法釋放為止必須有存續保護，沿用私人／Team scope 及秘密隔離。不可變版本仍可能被刪除；保留 digest、tombstone 或空引用不等於保留履責材料，也不要求保存所有原始聊天或秘密。只保存合規必要內容及受控參照，不能以 pin 放寬原批准／保存清單。

引用建立／附著與 GC 資格判定必須由同一可線性化權威處理，或有可證明等效的競態排除協定。準備期間先取得精確版本的 pin／generation，再核對材料可讀及 digest；控制提交必須同時條件式驗證該 pin 仍有效、材料未被宣告回收，並把 pin 附著到 request／work／operation／delivery。不能先讀有效 pin，暫停後再無條件 commit。若採不同儲存的等效協定，須證明在提交可明確核對前，GC 不能解除其保護；不能假設 Blob 與 Table 已有跨服務交易。

Pin 到期或 orphan 回收先原子失效其世代、阻止舊 writer 後續附著；僅時間到期或「尚未找到控制引用」不授權刪除。提交與回收須採同一個到期／失效生效點，不能讓 GC 視為已失效而 writer 仍能用同一世代提交。暫停 writer 拿舊 pin 恢復提交時必須失敗，不回成功 admission；重新準備前須核對原提交及材料。控制 commit 結果未知時保留保護，由權威紀錄確認引用是否已附著，不因 timeout 自動解 pin。若 pin 已附著，材料跨一般 retention 仍受未履行 obligation／unknown operation／待恢復工作保護。

GC 只能在確認無有效 pin／未結引用、原準備 writer 已不能再附著，且承諾的取得窗口及保留條件均已滿足後，排他宣告指定版本可回收；宣告後拒絕新 pin。外部刪除依第 6 節操作契約執行，結果未知時不把材料重新標為可引用。回收只作用於已核對的精確版本，不刪可變 alias，也不使後續同名新版受到舊刪除影響。

首切片尚無上述協定時，可採明確的「必要材料不自動回收」模式：停用會碰到準備中／未結材料的 auto-GC／retention 刪除，並確認沒有其他已配置的自動路徑繞過保護。這不是一個未經核對的設定布林；不能保護材料的儲存配置不承接相應新責任。無法建立存續保護時，拒絕成功 admission／轉移並回明確 Problem；不藉遺失原件後重新做工作來假裝恢復。已接單後材料異常不可用則保留失敗／核對責任，不倒改原 receipt。已完成且不再被引用的資料保留期仍由第 12 節另定，不要求永久保存所有內容。

不回收模式以受保護 scope、配置版本及可核對依據作為上述提交的材料保護參照，不偽造 pin receipt；這是 pin／GC 協定尚未實作時的替代模式。改啟 GC 前，須先排空受影響責任或把所有準備中／未結引用安全轉入 pin 保護，核對完成前禁止切換；不能只改旗標便讓舊資料失去保護。

### 過期與防重播

BR-01 為每個入口固定可信來源、scope、可接受重送期限與防重播契約。刪除一般 event 正文不刪掉最後的 ID／digest／結果或 tombstone；若要刪除最後去重依據，必須先能以保留的權威紀錄永久拒絕該來源的舊 namespace／epoch／有效窗口。期限／epoch 必須受伺服器或可驗證來源約束，不用外部自報時間，也不替舊事件重新包裝成新 epoch。

來源無法提供可驗到期判定時，保留最小 tombstone；不能只憑「找不到 opaque ID」就把它當新請求。未核對 operation、未履行 obligation 及在途回條所需識別不隨一般 retention 清除；期限／大小配額按第 12 節固定。舊事件明確拒絕或進原紀錄核對，新的合法交辦即使內容相同仍可受理。

## 7. BC-05：Context、compaction 與工具效率

對應 FR-26、FR-30、FR-34、NFR-01、NFR-02。

本輪 context 依序由已批准指令、權威工作／批准／未知操作、近期使用者原話與必要事件、有效摘要及有權讀取的記憶組成。`ContextManifest` 記錄來源／covered sequence、work revision、release／catalog／memory 版本、各區大小／tokens及省略理由。精確狀態不足時先重載，不從語意摘要猜測。

摘要須保存來源範圍及 fingerprint；來源前綴或權威版本不再相符時失效並重建必要部分，不沿用舊批准。SDK 原生 compaction 管模型 session 壓力，FA checkpoint 管精確產品狀態，不在每回合重複摘要全部歷史。

每個已選模型設定有界輸入／輸出／工具往返預算。超出時只移除低優先資料並標明，必要約束仍容不下就明確阻塞。未知 token 數不填零。M4 本批的 bytes／tokens 數值及「超限即 FAIL、不暗中截斷」維持原案例契約，不由這項未來 context 策略改寫。

穩定 prefix 使用版本化指令／catalog，變動事件與檢索放後面；tool schema／skills 按需載入、限制大小並驗來源。實際 SDK 版本不支援動態集合時用受控 Adapter 或 session 重建，不套用僅見於另一語言／未發布 source 的欄位。權限撤銷即時核對，不等 cache 失效。

### 2026-09-26 本機切片：逐回合保存 metadata 的純讀觀察

指定human可在目前read權下，純GET原已註冊turn的`fa-turn-context-v1`。
核原request/job/conversation、保存材料及當前授權；沒有metadata就明確未就緒，
錯配／未知schema不以空成功代替。讀取不安裝設定、不啟模型、不改回條／工作／預算。

只呈現已保存的版本與來源參照、bytes口徑及固定omissions；skill catalog不當已載入或目前權限，
原話／工作記憶未捕獲時保留null，不由啟用profile推造來源。SDK／prompt／Memory本文、
原始model packets、private reasoning、credentials及Blob位置不匯出。
Stored state JSON、legacy inline zlib、公開回覆JSON／本文／儲存zlib各自列數值；
不下載checkpoint Blob或解碼SDK state來補數字，也不把參照metadata稱為目前內容驗證。
只有原checkpoint的配對compaction source可投影其保存觀察，不以此另判restore通過。
一般續存可沿用較早job的同一execution；reader沿註冊的前一archive metadata核scope、
material、profile、previous hash及execution一致性，明標inherited與原observation job，
不誤拒合法續回合，也不把它冒稱本回合新的compaction。
完整request／provider tokens、wall-clock及UI-rendered bytes缺資料時一律null，不填零、
不借另一通道的有限索引或裁剪event snapshot計算總量。

薄client驗原admission識別及完整wire／digest後供人手動GET查看；不得把這個回應變成
can_follow_up／reset許可、操作批准或ACK。原progress／reply格式與保存／授權權威不變。
這是PA-16／TC-16的本機consumer切片，不宣稱完整source-load trace、保留／刪除政策或Hosted驗收。

### 2026-09-25 本機切片：自由指令的工具參數錯誤回饋

`instruction=True` 的 SDK custom-tool permission／invoke 接點，若在呼叫 handler 前由本機
已註冊 JSON schema 判定參數錯誤，回傳該 schema 的約束路徑／類型，讓模型能修正輸入。
不回傳原提交值、未知 property 名稱或完整 validation exception；沿既有 1024 字元回饋上限。
只放行本機 validator 產生的診斷，不以訊息字首或 handler 自稱「schema error」作判定。
`read_skill`／`read_work_memory` 是例外：它們的 schema 包含當前授權 skill／claim alias，
全部 validation 拒絕仍沿原
`Authority refused…` 遮罩；不把撤銷／不在目錄或夾帶 path 的拒絕只呈現成普通參數錯誤。

身份／工具集合／操作權限沒有放寬。除下節明列的原話reader固定原因外，handler的`ToolFailure`、
未知exception或可能已發生效果仍沿原遮罩與「unknown不重送」提醒，不宣稱沒有副作用；
結構化有限profile的回饋bytes不變。
公開 event 投影仍不帶 raw `data`，也未新增工具、prompt profile、重試器或 agent loop。
此切片只有 SDK callback＋I/O 替身的本機證據；真 SDK／模型修正品質及 Hosted 採用仍未驗。
Approved-skill 的既有 native 拒絕斷言不變，本次未重跑該 native suite。
結果與保留反例見[五類 baseline](agent-runtime-evaluation-baseline.md#tool-06-第一個實作切片安全的-schema-回饋)。

### 2026-09-25 本機切片：原話reader的固定拒絕原因

已啟用`read_user_turn`的instruction可取得四個精確reader code的固定model-facing提示：
`USER_TURN_SCOPE_DENIED`、`USER_TURN_NOT_AVAILABLE`、`USER_TURN_SOURCE_CHANGED`、
`USER_TURN_SOURCE_LIMIT`。提示只含固定code／文字；不傳原API message、detail、正文、actor、路徑或憑證。
`ToolFailure.public_code`只在受控Python接點內攜帶分類，不是model／request body可選欄位。
只有該tool及當前reader capability才可翻譯；未知、完整性或API／commit結果未明仍為opaque refusal。
Code可來自既有API header白名單，或client對原assignment的本機past-turn範圍檢查。
成功回應的conversation／turn與請求不符是`USER_TURN_SOURCE_INVALID`完整性失敗，
不可再沿用`SCOPE_DENIED`給可修正turn提示；原文不返回模型。

不得把固定提示當成新讀權、未產生副作用的證明或重送許可。Skill／memory catalog拒絕、
`execute_operation`、structured profile及原once／unknown契約不改。不存在或超界資料只能如實報告，
不能由摘要補值、截短原件或借其他conversation／actor讀取。SDK仍負責工具迴圈，
公開event仍不包含raw failure data；實際模型是否依提示自行修正須另作M驗收。

### 2026-09-25 本機切片：公開工具事件的安全配對

公開progress可帶`tool_start_seq`，只引用同一execution公開序列中的start；
不是SDK raw call ID、全域操作ID或任何授權／重送憑據。Start引用自身、complete引用較早起點，
有可見起點時必須同工具；重複／重用ID或不完整completion不得捏造配對或failed結果。
來源僅是當次真正註冊的SDK tools，參數、正文、raw ID及private reasoning不進公開投影。

舊欄位缺省仍可讀，None不新增序列化欄位；8 KiB snapshot可省略較早start，但保留關聯不代表
完整event replay。模型對話、保存、工作成功、交付與收件條件不變。先更新API／bridge／console
typed readers，再啟用新Hosted producer；不得為混版而放寬strict未知欄位拒絕。
本機證據與TC-01新資格界線見[工具事件配對](tool-progress-correlation-evaluation.md)。

### 2026-09-30 修訂：公開工具摘要與助理中間說明

8765 的處理過程可顯示經去敏、最多256字元的搜尋關鍵字、HTTP(S)網址及skill名稱；
只取當次已註冊工具的指定欄位，不公開整包arguments、header、工具結果或檔案／skill正文。
網址去掉帳密、query及fragment；沿既有已知token遮罩，再遮罩常見憑證格式與文字中的秘密欄位。
超長顯示以`...`明示節錄，不截改真正工具輸入、原訊息或保存答案。

沿固定SDK 1.0.13的`tool.execution_start.data.arguments`與`assistant.message.data.content`，
不另外叫模型生成說明。助理說明只收頂層、帶已註冊tool request的公開中間訊息，
或明確`phase=commentary`的公開文字；非空且非私人phase，子題訊息及最終答案不混入此類步驟。
它是助理說出的文字，不是「已照意圖執行」、保存成功、操作批准或收件回條。

沿既有`PublicProgressEvent`的`stage/tool_started`及`summary`，不加欄位／kind或第二份event store；
API與工作台仍用原strict DTO、同一8 KiB snapshot、身份範圍與輪詢。舊reader可讀相同shape，
此片不新增資料格式回退下限；API／Hosted仍須按既有code binding使用同一版本。
固定SDK的`assistant.reasoning*`是完整thinking資料，`reasoning_summary`是模型請求模式，
並非本路徑已取得的公開摘要；沒有可確認的公開摘要時維持「此模型未提供推理摘要」。
實作、SDK原件與採用順序見[公開進度摘要](../src/fa-shared-context-poc/README.md#公開工具參數與助理說明)。

### 2026-09-30 增補：Responses 的官方推理摘要（2b）

只讓真人選定新版模板後建立的新對話使用`model_provider=openai-responses`；
舊CID、selection、checkpoint、帳本及UNKNOWN保持Completions，不轉換、清空或重送。
Provider與摘要選項由原可信入口捕獲在work/task，不能由模型、訊息body或目前ENV替舊回合改選。
只有server模型facts明列支持摘要才啟用；不支持的gpt-4.1-mini及未回傳摘要均明示未提供。

固定SDK正式Responses事件的provider-tagged `reasoning_blocks`中，只有明確
`reasoning.summary[]`中`type=summary_text`的文字可作公開摘要。通用reasoning事件、raw text／opaque
不是替代來源。摘要經去敏、限長後，只顯示在8765處理步驟，不取代原答案或成功／收件條件。

Responses要求`store=false`與完整本機input，不依賴`previous_response_id`、供應商conversation
或item reference。沿同一request、ModelCost與共用CostLedger記input、output、reasoning用量；
reasoning已在output總數內，不加算一次；缺用量保留pending，不當0或清除既有責任。

新的版本化Responses codec依[CTX-03](ctx03-checkpoint-contract.md#2026-09-30-responses-專用-codec)
精確去除已觀察的兩種巢狀model訊息私有欄位，保留公開原文、envelope、順序與parent；
未知private shape拒絕且保留最後合法checkpoint，不擴大成一般診斷資料放行。
API／Hosted使用同一build，再選新模板並換8765；不用為舊版回退安排多階段。
本機與採用狀態分開，實作入口與限制見[Responses 2b](../src/fa-shared-context-poc/README.md#responses-推理摘要與原對話保留)。

### 2026-09-25 本機切片：SK-01 版本綁定的批准用途

用途描述是選用資料，不是 system 指令、priority 或操作批准。本切片只補 SK-01 的 metadata
接點，SK-02 真模型相關性仍未驗；不新增技能庫、native loader、MCP／scripts／plugin 或私人
Agent ACL。既有 M3 author／Team／human 身份、immutable version、ETag／receipt 為唯一權威。

有權的 owning Agent 可在新 skill version 提供 `fa-skill-purpose-v1`／字面 description，
限制 printable、非秘密、非空白、最多 512 UTF-8 bytes；不裁切、不從 title／正文推導。
帶用途版本必須有 server 重算的 `metadata_sha256`，綁 `fa-skill-version-metadata-v1`、
原 Team／artifact，以及完整 immutable version metadata／Blob receipt／body hash／用途。
正文 `sha256` 與 metadata digest 是不同口徑；真人決定須同時 pin version ID 及兩者，
否則不得生效。用途改字要新版與新批准，不能沿舊 upload／decision receipt 改 metadata。
舊版本的缺省用途與新 digest 欄位均省略，不重寫 persisted records。

只有 server-owned、default-OFF 的 `fa-instruction-skill-purpose-v1` prompt profile 啟用
`fa-approved-skill-catalog-v2`／`fa-approved-skill-read-v2`；unknown／missing 新 schema 拒絕。
舊 v1 的 `description=null`、prompt／reader／wire bytes 保持；新 catalog 對 legacy 無用途仍
回 null，不發明用途。只有當前 configured／effective／approved／有讀權的條目可列入；
initial metadata 不載本文，`read_skill` 仍是唯一按需正文路徑。

目錄沿 4 entries／4096 UTF-8 bytes，正文沿 8192 bytes；超界完整拒絕，不引入 token 比例、
降級／省略／ranking。當輪 read 前後與 model reserve／consume／egress 重核目前批准與
原目錄；合法完成回合後的新 turn 可採新用途，原 archive 不改。原 unfinished work、
unknown 操作、no-SDK／explicit-reset、費用與身份界線均不因用途欄位放寬。
此切片不新增批准 TTL；沿現有有效版本及 current access，不能用文字自稱已批准或未過期。

### 2026-09-26 本機接點：單一有效skill版本停用

SK-09的停用不以「reject新提案」取代：原reject仍保留上一有效版，原內容approval不可重寫。
有權的configured M3 delegated human可對指定**當前有效skill版本**提交獨立的
`fa-skill-deactivation-v1`，綁原artifact path／Team、decision ID、version ID、body SHA與
必要的purpose metadata SHA，並以exact artifact ETag核對。原M3驗證人的OID／client／
`Artifacts.Approve` scope，不新增IAM或model管理工具；agent及本文中的human／approval標記
都不能執行這個管理動作。普通deliverable與SOP不在此停用接點內。

原ArtifactDocument同row CAS一起保存有界immutable deactivation receipt與
`effective_version_id=None`；deactivations尚未存在時省略欄位，舊資料bytes不變。
回條含原input、server人別／時間與applied revision，decision ID在該artifact的approval／
deactivation間不可挪用，版本原件／Blob receipt／purpose hash／content approval保持不變。
未知write ACK只讀回原artifact核同decision；same ID同material可取得原回條，
不同material明確conflict，不能換ID清掉unknown。回應是當前artifact snapshot，
舊回條不代表現在仍停用；後來的新版本批准不能被舊ID重送停用。

停用不要求先下載Blob，也不消滅未知upload：已有pending保留；原owner之後可沿目前
ETag對帳該version，不第二PUT，也不自動把未批准新版本生效。
舊approval重送不會重新啟用已停用版本；此v1接點只停用，重新生效須另提出新版並走原人工批准，
不提供靜默重啟舊版本或改舊decision。

Current catalog／read／model-source門檻沿既有接點觀察effectivity變更。歷史skill本文、
原SDK事件、舊archive及獨立operationgrant仍保持；停用不是資料刪除、模型記憶抹除、退款，
也不宣稱能追回已外送或已發生的效果。管理CLI只允interactive exact-version確認，
不自動重送POST；需要核未知回條時先show/readback。舊reader遇到新欄位可拒絕，
不能宣稱mixed-version部署／rollback已驗；本輪沒有部署或改雲端權限。

### 2026-09-26 本機前置：可攜模板參照與目的地檢視

SK-11第一切片只匯出`fa-agent-template-v1`的role／skill／configuration版本參照，
不包含角色原文、技能本文、credentials、私人history／Memory、帳務或既有操作grant。
來源是原standing policy的versioned instruction profile及當前配置／已批准的M3 skill，
由指定human的現行Behavior.Read與原standing read authority核對；Agent或body自稱授權不可匯出。
Legacy無版本role不捏造可攜版本，缺件名稱明確列unavailable，不把草稿當已批准內容。

2026-09-27的consumer接續納入目前新對話的可信default：原policy沒有明確profile時，
可匯出已設定的既有v1參照。它描述新對話設定，不改policy、舊對話或其保存版本，
也不使template取得任何來源權限；未選versioned role時仍保持原拒絕。

GET`/work-policy/v1/agent-template`提供有完整bytes hash的no-store附件；
POST其`/inspect`只比較輸入的固定版本／hash與本目的地目前的role、settings及configured skills。
嚴格有界schema拒絕隱含scope、credentials及操作批准欄；即使參照吻合，結果仍
`authorization=false/activated=false`。目的地的帳號／tool grants、模型連線、reviewed來源、
私人記憶、身份／工作政策及SDK/runtime相容性都必須在真正使用前另行取得與核對。

此階段沒有apply／安裝／配置寫入能力，不將匯入檢視說成bot已可執行。
完整SK-11仍需後續真正的目的地採用與相應驗收；檔案或hash本身不能轉移來源的Team權限。
目前實作及API細節見[Shared Context](../src/fa-shared-context-poc/README.md#可攜模板參照匯出與匯入檢視local未部署)。

### 2026-09-26 本機前置：已批准 Team SOP 的未生效 skill 草稿 copy

此節只取 SK-10 的受控前置，不是任意示範提取、自由 authoring、搬移主 prompt 或完整
SK-08／SK-10／M 驗收。僅以目前 Team 可讀且 human-approved 的指定文字 SOP 作來源，
server 逐 byte 複製到原 work 的單一 skill 草稿 slot；不接收 model/user draft body，
不複製私人對話，也不宣稱 printable 能辨識秘密。

Server 必須明選新的 `fa-instruction-sop-copy-v1` 與 optional／default-OFF `sop_copy`。
`fa-team-sop-skill-copy-v1` 明列 `authorization="one-approved-sop-byte-copy-per-work"`、
Team/source visibility、Team ID、source artifact/version/body SHA、精確 human approval digest，
以及 server literal `fa-skill-purpose-v1`。原 `StandingPolicyInstallation` 的人類安裝決定
保存整份 profile；WorkRegistration／Task/profile digest 綁相同設定。舊 None 不增 bytes，
舊安裝不能靠新增 flag 得到此寫權。`authorize_own_result` 或 SOP 內容批准本身都不是 copy
write grant；缺原明示 installation、目前 Actor 寫權或原合法 owned legacy source work 就拒絕。
這是**每個 work 一個 slot**，不是整個 installation lifetime 只有一份；新的合法 admission
可以有另一個 server-bound target，但不能把原未知 copy 自動換 ID 重送。

`copy_sop_to_skill_draft` 是固定 SDK custom tool，參數為空物件；不接受 body、purpose、
Actor、Team、source work、artifact/version、approval 或路徑。原 verified principal/ticket/
owner/epoch/job gate 先核對，再使用原 server Actor map／M3 service；不 cast Actor、
冒 human、以 ticket 代替 M3 bearer 或新增 legacy work。來源只支持 SOP 的 text/plain/
text/markdown、完整 UTF-8 ≤8192B；用途沿 512 UTF-8 bytes，不從 title/正文推導。
新的 copy create/upload 前，來源 effective version、body hash、配置 human 的 approval 及讀權
在讀前後／effect 前重核，
same-body 新版仍不相同。初始 model 只有 server-selected metadata，正文不載入 model。

原 artifact-effect slot 與原 ControlRequest/guard/CAS 保存 versioned immutable copy intent，
綁 registration/job/execution、來源 version/approval/bytes/payload digest、目標及用途。
Create-only M3 create/upload 各只能沿已確認且新取得的 stage 發送；create ACK不明只 GET
原 ID 比對 owner/kind/source，之後只有尚未消費的 upload stage 可發送。`upload_unknown`
只能原 version stat/readback/reconcile，沒有 Blob/不完整回條仍 unknown，不第二 PUT、不換 ID。
重複已確認工具呼叫只核原回條，不建新 target。這不是 control store＋M3 的跨服務原子交易；
撤權／競爭／後端不明可留下原 intent 與部分草稿，必須保留，不自動刪除、reset 或退款。

回覆只含 `fa-sop-skill-draft-receipt-v1` 的安全 job/target/source refs、hash、用途、
`visibility="team"` 與 required `state_scope="at-original-copy-write"`；
`approved=false`、`effective_version_id=null` 描述**原 copy 寫入時**，不是當前 target 狀態。
人類後續 approve/reject、新版本或新的 effective pointer 不改寫此歷史回條；
回讀仍核現在讀權、原 intent/operation lineage，以及原 target/version 的不可變
bytes、metadata／Blob receipt。只有 approval 決定與後續版本是不同狀態，不忽略原件損毀。
**未生效不等於私人**：現有 M3 Team ACL 讓合法同隊 agent 可讀草稿。
Copy 當時不使草稿進 effective/catalog，不覆寫已存在目標、
舊 approved version 或 human decision。原 source approval 不批准新 target metadata；
人工另沿 version/bodyhash/metadatahash/ETag決定。模型永不取得 approve 能力。

保存原結果與新的 copy 執行許可分開：copy profile 的 failure/cancel 不再先要求 source
仍 effective、legacy work 仍可寫或 target 仍 unapproved，而沿原 caller/ticket/current-read/
save 契約；交易內消費原 copy ledger，不重做 M3。整體 read/report 權已撤到原保存不合法時
仍 blocked。成功 reply 不得藏住 unresolved；failed/cancelled 則誠實記入原 known/unresolved。
`create_unknown` 只屬未決；`created`／`upload_unknown` 的 metadata create 已知，但整份 copy
仍未決；`saved` 才有原完整 receipt。原 reply/archive/history/projection 保存
`fa-sop-copy-outcome-v1` 的 stage/intent digest 與 receipt ID，和已知／未決 result欄位互核。
來源換版後，已發生 upload 可在目前原 M3 read/reconcile 權仍成立時，只核原 version；
不能再 PUT。無法完成原核對時保留unknown，但原合法 failure結果仍可保存／交付，
不偽造 stop/archive、清除effect、退款或自動reset。相同conversation的新turn不能繞過未決copy。
已確認 copy 的 source批准或 target 後續狀態變更不再使 reserve/consume/egress 重做 copy；
它們仍驗本輪權限及原已確認 effect。這不是給新 copy 放寬來源授權。

本輪只最小補齊未發布的 copy-v1 receipt 時點標記及新的 outcome DTO；
舊 None／原 profile-prefix bytes 不變，原 beta67/native1 raw格式與結果另保留、不回填。
API DTO 不依賴 SDK，Hosted 不新增 API-only dependencies。
固定 SDK loop／empty mode、native loader/discovery/MCP/plugins關閉、原費用/once/身份界線不變。
完整相容性與分層本機結果見
[Shared Context README](../src/fa-shared-context-poc/README.md#已批准-sop-逐-byte-複製成-skill-草稿local預設-off)；
本節不授予部署、付費、IAM 或新私人資料用途。

新 literal prefix／record/hash、binary upload header、CLI 顯示與 metadata canonical hash
算法以 [Shared Context README](../src/fa-shared-context-poc/README.md#版本綁定的-skill-用途local預設-off)
及 `m3_models.skill_metadata_sha256`／`SkillPurpose` 為實作契約來源。新 profile 有獨立
prefix／create/resume／反例接線驗證，不承接原 PA-02 五個 prefix 的接受。
API、Hosted、M3 client／CLI 需同版能力；新用途不等於新 cloud／paid／IAM／部署批准。

### 2026-09-25 本機切片：宣告模型容量的快照與 SDK 配置傳遞

此節只接 TC-04 的 **configured capacity facts**，預設 OFF；不是容量上限已生效的聲明，
不取代 TC-04 對真實有效 limits 的驗收，也不完成 TC-05 四種 triggers。
僅 server-owned `AssistantProfile.model_capacity_profile="fa-model-capacity-v1"` 啟用。
缺省欄位仍省略；舊 structured 工作的固定 limits 不擴散到自由 instruction。

新 instruction 在原可信 deployment catalog／loaded facts 選定模型後，保存不可變
`fa-model-capacity-snapshot-v1`，包含 model、profile、`source=server-curated-model-facts`、
context/input/output 可空欄位與來源 digest。Digest 綁定版本、selected model 及正規化的
容量欄位，不含檔案路徑、時間或費率，不冒稱原始來源檔 hash／vendor attestation。
該快照進原 Task／WorkRegistration/profile digest；後續 reader 用原 snapshot 重建歷史，
不能以目前檔案倒改已保存 task。對話 metadata 與薄 client 也只稱「宣告快照」。

`context_window_tokens` 對應總 context、`max_prompt_tokens` 對應輸入、
`max_output_tokens` 對應輸出。只映射已知正整數，缺資料不填 0、infinite 或 FA 的旧
32768／1024；不計算 `input=context-output`，也不強制獨立輸入／輸出 maxima 的和等於
context。個別 input/output 超過已宣告 total、bool／非整數／零／負值或不合版本均拒絕。
新 `max_prompt_tokens` 是可選且明示的 server fact，不從 deployment 名稱猜值。

Hosted 沿固定 SDK `ModelCapabilitiesOverride/ModelLimitsOverride` 的正式 create／resume
參數提供當輪 snapshot；provider 配置只映射正式支持的已知 input/output。換模型的新回合
取得新 snapshot，不帶前一個 model 的容量。Native 對話在已有完成回合後換模型只在 Hosted
`FA_NATIVE_MODEL_SWITCH=1`（預設關）時接受：以新模型從已發布 checkpoint cold resume（丟掉留著的 SDK，不用實驗性
`set_model`），新模型宣告的輸入上限小於上一回合量到的 SDK context 時在接單前回 `NATIVE_MODEL_CONTEXT_EXCEEDED`，
不壓縮（2026-10-13，[產品說明](../src/fa-shared-context-poc/README.md#native-model-switch)）。完全未知時 SDK override 的數值集合為空，
保留 runtime 自己的行為；不能把 runtime default 又冒稱為已知 server fact。
原 user bytes、session、cost/once/unknown、checkpoint/no-SDK/原話 reader 邊界保持。

Facts 沿原 `InstructionModels` load/cache 生命週期，**不是 hot-watch**；單改檔案不表示
目前 process 已載入新 facts。API issue、acquire、restore、model reserve／consume／最後
egress 都核目前 loaded model 的容量 snapshot；已載入的當前值與原 Task 不符時明確
`MODEL_CAPACITY_CHANGED`，不替換 task、回收未知預留或重送。新正常真人回合可取得重新載入
的 facts，舊 archive／registration 保留。這不是跨檔案／儲存／provider 的原子交易。

**固定版本實證差額：** Python SDK `1.0.13`／runtime `1.0.83` 的正式 RPC 已收到宣告
overrides，但本機 `metadata.get_context_attribution()` 的 prompt/context 顯示值仍回
128000，與 declared 60000／14000 不同；OpenAI-compatible completions request 沒有
`max_tokens`／`max_completion_tokens`。官方固定版 E2E 亦明記 output 配置不 echo 到該 wire。
因此配置、runtime observation、provider 保證分開；**有效 override enforcement 及 provider
hard limit 均 NOT_PROVEN**。不使用 caller-projected `context_info` 補假綠燈。
未取得這條 create/resume 路徑的可靠發送前 oversize 拒絕證據，不新增 FA tokenizer、
token estimate gate、compaction loop 或 gateway 私下加 hard-limit；也不因此盲目付費重送。
真正的安全小窗口前檢、長 history／images 及完整 TC-04／TC-05 仍待後續相應驗證。

本節沒有部署、雲端探針、IAM 或新增模型執行授權。版本來源、原 FAIL、精確測試基準及
配置／觀察值見 [Shared Context README](../src/fa-shared-context-poc/README.md) 的宣告容量節。

## 8. BC-06：有來源的記憶與 User Model

對應 FR-06、FR-18、FR-26、FR-31。

沿用私人／Team scope，另明列人類 subject、可讀者與同意用途；不默認跨 Team 共用 User Model。profile 保存明確穩定偏好，episode 保存有意義工作的短摘要，note 有有限用途／有效期；問候、ack、完整聊天、批准及 operation receipt 不作語意記憶。

候選必須有 claim、subject／scope、evidence references、有效時間及版本。流程為候選 → 稽核必要性／支持性／矛盾／敏感性 → 程式核對真實來源與範圍 → 提交抽取並保存 native receipt → 核對輸出 lineage → 可供檢索。稽核可用規則、人類或獨立模型；第二個模型不是高風險內容的授權者。

FA 保存已審 claim 與失效狀態，Memory Store 為檢索投影。無法對回有效 claim 的資料不自動生效。更正／撤銷先阻止使用及失效快取，物理刪除另按保留／刪除政策；不得藉 profile 更新發布 SOP、skills、角色或批准規則。

沿用原生 update receipt；202 不是完成，未知提交不得重送。必要記憶不可用時停止並回原因；確實不依賴該記憶的工作可在明示 degraded 狀態繼續，不回空成功。這套一般治理在 M4 本批之後增量建置，不放寬該批固定 fixture／Store。

### 2026-09-30 增補：真人選類型，沿原已審記憶 reader

真人可在原preview／review／commit明示profile、episode或note；類型及有效期綁同一
candidate digest，更正仍需精確舊版本與新review，不以字串較新代替有效性判斷。
缺省note保持原bytes；原prompt、read_reviewed_note名稱、同human／Agent／Team／subject、
purpose及讀權不改。該reader帶回`applicability`的類型和期限，原source capture不增欄位。
profile是明示穩定偏好，episode是工作紀錄，note是指定用途備忘；期限由真人按用途確認，
不以類型自動延長效期或把記憶當現行Work／批准權威。
有效期在讀取／context組裝結束前再次核對，避免耗時組裝送出已到期claim；
新版更正不覆寫舊資料、也不復活舊capture。此片不新增Memory Store抽取或私人跨Agent可見性。
實作、直接驗證及最多三則部署後探針見[MEM-02入口](../src/fa-shared-context-poc/README.md#真人選擇記憶類型mem-02)。

### 2026-09-25 本機接點：已審 Team 工作 note 唯讀

僅 server-owned、default-OFF `fa-instruction-work-memory-v1` 將既有已 commit／已 human
review 的 Team note 接到固定 SDK custom `read_work_memory(claim_id)`。此切片只支持目前
binding／template subject／policy purpose 的一個 `profile.claim_id`；重用原
`Runtime.context`、`MemoryGovernance`、SourceMaterials／審核 receipt，不引入另一個
lookup/agent loop，不執行 Memory Store search／extract／發布或自動偏好保存。

目前 verified principal 的 source actor 映射才可建立 source context；請求與模型不能選
scope／tenant／Team／subject／visibility、版本或批准。私人／人類偏好視圖尚未支援；
非 Team current policy 明確拒絕，不編造 human/Agent 身分或放寬原來源 ACL。

初始僅 metadata；正文需實際工具回讀，保留原 reviewed text、claim version/digest、
human-review record digest、必要 source refs／逐字原件。metadata/body 都是 knowledge data，
不是 system／operation permission。捕捉的有界 provenance 沿原 Session／TurnArchive／CAS
保存，None省略舊 bytes；不改 persisted claim／review／history 或建立新記憶權威。
未確認或缺件、withdrawal／revocation／expiry、same-body 不同版本及目前 read policy 的競態
都 fail closed；read、restore 及後續 model reserve／consume／egress 重核原精確材料。
後續 turn 只在原參照仍 current 時接續，不自動移植已更正／撤銷來源後恢復舊正文；
這個保守限制不能寫成已完成一般記憶更新接續。原 no-SDK／UNKNOWN／explicit reset 不放寬。

容量只沿現有 source／MemoryGovernance 與 typed tool transport 邊界；不是有效模型
token 配置或 2% 規則，缺 token 不能當零。第七 prefix、DTO／HTTP 版本、
source/response byte bounds及驗證口徑見
[Shared Context README](../src/fa-shared-context-poc/README.md#已審工作記憶的唯讀接點local預設-off)。
原 `read_context`／structured／成本／once／compaction guard 行為不改。

### 2026-09-26 本機修補：正常 job 觀察不改知識版本

本節只補 PA-13／PA-16／TC-12 的既有 Team note reader liveness，不是 TC-08
provider summarization、記憶更新 transition 或新的 no-SDK eligibility。
同一原 job 的 query reservation、查詢計數、last observation 及仍有效的
`starting`／`running`／`capacity_uncertain` 轉換，不單獨構成 reviewed knowledge 變版。

記憶 `load` 的精確來源 snapshot 不再把 job 整列 bytes／ETag 當成知識 identity；
只分離此 job row，並非移除所有 authority predicates。讀前與回傳前沿原
`authorize`／`_scope`／`_gate` 核當前 identity、Team、owner／epoch、原 execution/input、
取消狀態及容量。Policy、budget、registration、delegation、session、obligation、pool
與 claim／human review／來源 head／期限／manifest 等原精確比較保持。
沒有刷新 captured source、覆寫 archive／user text、放寬 subject-wide sequence 或改 prompt/schema。

Catalog capture／model reserve／consume 的 `ControlRequest` 仍含原全部 gate keys，
job 仍由原交易中的 `_gate` 檢查；原 guard／ETag／EGT/CAS、create-only receipt、
request ID 及 unknown 規則不變。`work-memory` catalog capture 不在 `_ledger_conflicts`
的 action 集合內，沿單次 `store.transact`；本切片實測的 `runtime.mode="offline_contract"`
晚 guard 競爭回 `412 BEHAVIOR_CONFLICT`，不重試該 capture。
Production `runtime.mode="native"` 且非 Teams 的 `model`／`model-send` 則沿**既有**
`_ledger_conflicts`：每次 `transact` 先查原 receipt；只有已確定的 412 conflict、查無原 receipt
時，才以同一 `ControlRequest` 最多共三次嘗試並記 conflict log，每次重核原 guard／權限。
因此正常 observer 寫入可使第一次 412、第二次成功；已有 receipt、unknown 或其他錯誤
不授予再次 dispatch。本切片沒有新增 retry engine、換 request ID、退款或重送模型／外部效果。
此 **native 業務模式的競爭路徑 NOT_RUN**；真固定 SDK＋offline provider control 不等於
`BehaviorRuntime.mode="native"`。最後 provider egress 仍重新核對當前權限及相同來源。

真正撤權、cancel、owner／epoch／execution 改變、policy／read permission、review／claim／
source 版本或期限改變仍拒絕；same-body 新版本及同 subject 其他 claim 改變 manifest 的保守
限制不放寬。通用 `ASSISTANT_SOURCE_CHANGED` 仍不得關閉 v2 gate 或產 no-SDK 終局。
局部正例／反例、合成與固定 SDK 的證據分層見
[Shared Context README 的 observer 修補](../src/fa-shared-context-poc/README.md#正常-job-觀察與已審記憶讀取local)。
本機合成接線不等於 full PA-13／PA-16、私人偏好或 M 品質接受，也不授予 cloud／paid／IAM。

## 9. BC-07：安靜與協作

對應 FR-07、FR-27、FR-32。

處理結果為 `reply`、`work_started`、`waiting` 或 `silence(reason)`。按該事件及受影響的 obligations 判定，不只看 conversation 是否有人待答。無關 FYI、重複已處理事件、無新資訊且無待交付責任的 routine 可 silence；被該事件影響的人類待答、需批准或未告知終止／未知結果不可靜音。無關 FYI 可以不回，不表示另一工作欠的答案消失。

Agent 間訊息分 request、result、handoff、fyi、blocked，帶 `in_reply_to`；transport receipt 不產生新模型對話。協作啟動前需有有限 hop、訊息、token 與時間預算；全輪無新工作／內容即停止，不互傳「收到」。首版只做工作交接，不先要求 Group Chat 才能交付。

Routine 必須有 owner、已批准版本、時區、occurrence ID 與通知政策；每次到點建立一般事件，同樣受容量、scope 與副作用批准限制。排程設定不等於永久委託；重複 occurrence 去重。連續失敗可合併通知文案，不能刪掉各 occurrence 的 obligation／失敗。合併通知須逐一記錄原 request／obligation、相同有效收件範圍、涵蓋的結果與各自所需有效 receipt；不符合就分開交付。排程產品與部署不在本次規格化授權中。

## 10. BC-08：相容性、落地與完成判定

對應 FR-16–FR-19、FR-33、NFR-01、NFR-02。

新 runtime 使用版本化資料／路由或明確 opt-in；關閉時 M2／M3 既有 Interface 與 M4 已批准流程保持原行為，不放寬原 `AssistantCommand` 的合成驗收限制。對舊紀錄只建立核對後的 reference／projection，不改 published work、native receipts、有效 SOP 或累計 counter。既有 `pending_or_unknown` 對應新 `outcome_unknown`，不能遷移成 prepared 再發一次。

已接單 request 的 runtime／schema 歸屬固定，關閉旗標只停止新的 admission，不能連同舊工作的 completion、reconciliation 或 delivery consumer 一起丟掉。退場採原版本 drain、相容 consumer，或以含未完成帳本／epoch 的 receipt 移交責任；在確認接手前原責任者保留。回退不支援既有 schema 時明確阻止回退／停用動作，不默默降級為舊命令。新舊入口行為不混用，也不以關閉功能取消未知操作。

開發先在現有 Seam 增量接線，實際需要共用才抽型別與傳輸 package。M3 現有 512 KiB／12 版與 SOP／skill 8 KiB 限制不因本規格放寬；正式檔案保存／退役另定。恢復跨 Hosted session／版本時，另納入 SDK 保存清單並核對身份，不用 M1 私人備份當 SDK 已驗。

每個新增切片只跑影響範圍的既有回歸與必要故障案例。已完成 M2／M3 不重跑等價雲端測項；新增 cloud evidence 另批准精確配置與用量。未知、局部完成與失敗保留，不能以模型自述、exit 0、工具 success 或 native idle 作整體完成。

度量端到端成果時間、首次有效進展、交付缺失、重複副作用、無必要詢問、通知數，以及模型／工具／控制／記憶成本。BR-02 起可按 obligation 年齡／狀態、要求與已知確認差距查詢未履行庫存，包含 needs_attention／delivery_failed；這是觀測，不用清掉失敗美化指標。任務成功、結果可取得、通知已交付分開統計，failure 已交付不能提高任務成功率。五個 M4 案例不是 p95／10 倍統計證據；更完整 benchmark 仍按原 NFR-01 另定同品質基準，不用短 ack 或少給內容湊數。

## 11. 新增行為驗收 B01–B12

這是增量驗收矩陣，不是 M2／M3 已通過項目的新結果，也不是本批 M4 追加 gate。開發任務 BR-01–BR-06 及依賴見[計畫](POC-ANALYSIS-AND-PLAN.md#m4-之後的行為開發切片)。

| ID／契約／開發任務 | 離線契約與必要反例 | 所選實際環境才能支持的主張 |
|---|---|---|
| B01；BC-01／02；BR-01／02 | 短答也先 admission／obligation，只省可見 ack；逐提交切點中斷、保存失敗不送 final；漏必要成果／錯工作不 succeeded；等待有到期核對；`B01-materials`：Blob 核對後、控制提交前插入 GC／pin 失效，舊 writer 不得成功接單 | 真入口 receipt、不可分裂的接單資料及 final／failure 回條相關聯；無 receipt 的等待／失敗不當完成；所選保護模式下必要材料在 process 消失後仍可取得，不只看控制 rows |
| B02；BC-02；BR-02 | 並行／重送 due、生成連敗、請求後 crash、零預算、通道全失效；每 obligation 原子預留最多 2 次，不因版本／重啟歸零，不重做成果，未交付仍欠答 | 真 consumer 中斷／恢復延續帳本；非模型補交與確定性通知使用相同 outbox／授權／回條，證明實際達到的層級 |
| B03；BC-01／03；BR-03 | job 期間追加／取消、completion 先到、spawn unknown；started 不當 armed，completion 重播不重做 | 真 spawn→subscription 或可運行核對接線與 job／attempt 相關聯；PID／設定布林不代替 armed 證據 |
| B04；BC-02／04／08；BR-02 | 第 6 節提交集合前後、目的端接受前後中斷；錯／舊 receipt、prepare 後撤權／改路由、final_prepared 時關旗標；`B04-materials`：控制 commit 結果未知／writer 暫停時 GC 不丟保護，恢復不拿失效 pin 附著；不丟責任、不盲重送 | 真 store／wake／目的端的提交、接受及核對證據；材料引用／GC 的排除機制或停用路徑有據；原 delivery ID 可核對，sender 無授權旁路，未聲稱跨服務原子 |
| B05；BC-01／04；BR-01 | 並行重播、同 ID 換 digest、亂序、retention／restart 拒舊、新合法 ID 同內容；commit 後 error、多步 partial 不整包重跑；`B05-terminality`：延遲 A→負面查詢→不得發 B／退額→遲到 A；對照終局拒絕／效果點 fence 與有效冪等分支，過期 key 不沿用 | 真原子批次／條件寫入跨 process 保證；所選 Adapter 分列 query-only、終局拒絕／有效 fence、整單位冪等及期限證據，缺終局／冪等者不開放非冪等自動重試；fake store 或查詢可用不代替 |
| B06；BC-03／08；BR-03 | owner／epoch／binding、checkpoint／批准核對；舊 epoch 不寫入，遲到回條由當前 owner 核對；prepared／unknown／armed 時退場不轉成新操作 | 指定 SDK／Hosted 版本跨 process／替代 session 恢復的 request→job→attempt→實際 owner 證據；原責任 drain／移交可查，已確認效果不重送 |
| B07；BC-03；BR-03／06 | `B07-C1`：C=1 的取得、排隊、釋放、未知保留及 child 無旁路；`B07-C4`：同契約在 C=4 的容量演算；取消未確認不回 cancelled | BR-03 只主張 B07-C1 的真一槽；BR-06 才驗 B07-C4：四個不同 agent 的真 executor、第五個工作排隊，stale worker 未證明停止就不釋槽／啟替身 |
| B08；BC-05／06；BR-04／05 | compaction 前後必要條件／來源與權威版本相符；至少一個假引用對抗 fixture，加矛盾、撤銷、過期、跨私人及摘要失效反例 | 真 SDK compaction／checkpoint、Memory receipt／lineage 及授權投影；不從 fake Memory 推定 native 抽取正確 |
| B09；BC-04；BR-01／02／06 | 網頁／FYI／Memory 不批准；off／shadow 不繞 gate；跨 conversation 搶一次批准／費用、撤銷插入 prepare→dispatch、reservation crash 重播；partial 不盲重試；`B09-terminality` 依 B05 的遲到 A 反例核對授權／預留；`B09-execution-binding` 在批准後及最後 gate 後替換 current／腳本／計畫／資源版本，只能執行 A 或拒絕新內容；合成秘密涵蓋 success／error／stream／retry／checkpoint／resume | 開放路徑逐 call 的有效決策對回實際消費的操作描述／canonicalization／版本，不能只比 args 字串；驗終局性／冪等分支及材料綁定；既有秘密防護取證仍受下述未關閉覆蓋待辦限制，未證安全能力不開放 |
| B10；BC-02／03／07；BR-03／06 | 無關 FYI 不因另一 obligation 待答而刷訊息；routine 失敗、等待通知失敗／到期、合併通知逐義務核對；receipt 不誘發模型互答，child／深層交接共用有限 root 預算 | 真路由／consumer／通道不另生 ping-pong；routine 僅在對應功能獲准後取證，無變化不通知，帶義務失敗仍可追蹤 |
| B11；BC-01／03／04／06／08；BR-01／02／03／05 | Store／Memory／policy 故障不回空成功／不 dispatch；consumer 停擺配額滿、重複 timer、漏通知、核對預算耗盡、配置不完整與退場仍保留責任；`B11-materials`：unknown／待恢復跨 retention 不只剩 digest，缺保護就拒絕新成功轉移 | 真 consumer／timer 在 API process 不存活時可按有限預算從保存意圖接續；驗所選 store／wake 及必要材料保護，不靠 process lock 或 Table 排程列宣稱可醒；控制配額進展問題仍見待辦 |
| B12；BC-02／08；BR-02／06 | 原請求所需固定成果版本、hash／scope／條件；錯工作、少一成果、必要 unknown 不 succeeded；ack／reaction／suppressed 不清責任；錯版本／收件者／digest、缺回條、超 max、偷降 required 都拒絕；`B12-materials`：未交付成果／回條跨 retention 仍保留必要內容 | 授權收件人在承諾窗口內真能取得固定成果版本；下載前當前授權與回條綁定相符；驗未結引用的保護模式；每通道只主張實得 provider／client／UI 證據，SSE 不冒充已讀 |

`B07-C1` 是單槽子條件，不是完整四槽驗收。完整 B07 需覆蓋 C1／C4 對應契約及 C4 真實 executor 證據；四個 mocked permits 不算四個 agent。不得把 BR-06 的四槽要求套到 BR-03 或本批 M4。

每份證據分記 B ID／子條件、`offline_contract` 與 `actual_environment` 的結果（PASS／PARTIAL／FAIL／NOT_RUN／BLOCKED），另列實作／配置版本、故障切點、入口、request／job／operation／delivery 關聯、預期與保存結果及未涵蓋項目。沒跑的欄位不填 PASS；薄入口 persisted 不代替 Teams receipt，假原子 store 只證明上層演算。選型前不能宣稱 B04／B05 Hosted 通過，離線開發仍可前進。

只取得新增主張需要、另獲批准的實際環境證據；不要求每個離線案例都各部署一次，也不新增重複審查、長時間 burn-in 或與切片無關的 gate。

**v1.2 的驗收界線：** 本次新增上述 terminality、execution-binding、materials 子條件，未完成 F20 所要求的全面「能力→適用 MUST→必要反例→PASS」矩陣。因此不得宣稱整個 B02／B07／B09 或 BR 切片已由本次修文完整結案。F18 的滿額控制通道、F19 的父子停駐／讓槽及 F20 的實際 SDK 出口／秘密路徑覆蓋仍開放，見第 13 節；未列證據的路徑不是已通過。若採停用 auto-GC 模式，只支持該模式的材料保護，不宣稱 pin／GC 並行協定已驗。

## 12. 尚待選型，不阻擋既定 M4

| 項目 | 已定最低契約 | 留待對應切片決定 |
|---|---|---|
| Wake／狀態儲存 | 第 3／6 節原子集合、跨 process 權威狀態、可重送運輸與有限核對；啟用前有可運行 consumer／timer | BR-01 固定 Table 分區／批次或等效 Adapter、queue／consumer 宿主與費用；BR-02 接交付、BR-03 擴背景，不因 Grok 採 Temporal 就引入它 |
| 共用批准／秘密處理 | 逐次可線性化耗用／撤銷、效果分類、送出前授權、首次一般輸出隔離 | BR-01 固定授權權威與各已開放 Adapter 的安全輸出契約；BR-02／06 接入 sender／新通道，無法滿足者不啟用 |
| 安全重試／實際內容 | 非冪等重試有未生效且不可再生效證據，或採有效整單位冪等分支；批准與 executor 消費同一實際描述／版本 | BR-01 固定 canonicalization、終局／fence／冪等及解析綁定契約；只有 query-only／可變 alias 的能力不放行相應自動操作，B05／B09 按實際能力取證 |
| 履責材料存續 | 準備至未結責任解除期間材料受保護；pin 世代／引用附著／GC 排除競態，未知提交不解 pin | BR-01 固定保護模式；先無協定就停用相關 auto-GC／retention 刪除，不受保護者拒接相應新責任；BR-02 延伸至成果／回條，其他材料隨切片接線；不選定新的 storage 產品 |
| 互動與容量 | 總上限 4，等待無活躍工作才可釋槽，未知不釋放 | 四槽全工作或一槽互動＋三槽背景、公平權重；不以這項選擇延後單槽可靠交付 |
| 通道 | 固定 delivery ID、當前收件授權、版本化 receipt、required／supported／max 與任務必要成果分開核對 | BR-02 薄入口先固定能力與要求；BR-06 另定 Teams 身份／路由／回條，不沿用薄入口結果宣稱通過 |
| 期限、配額與保留 | root 共用有限預算、等待有檢視期限、pending／due 有界、到期後仍能防重播、未履行責任不清除 | BR-01 固定入口／儲存配額與 anti-replay，BR-02／03 配置通知／核對／child 上限；各 workload deadline、retention／退場政策，設定不完整不啟用 |
| User Model／效能 | 同意與讀者 scope、來源可追溯、同品質比較 | 跨 Team 分享、敏感 profile、正式 token budget／SLA／benchmark；不能由本文默認放行 |

本規格只加入後續開發，不授權新部署、模型消耗、權限／排程變更、刪除、外部傳訊、commit 或 push。現有 M4 的具體批准由其開發工作階段持有；本分支不重放該批准或平行啟動另一批。

F15–F17 的相關能力在上述安全前提與對應環境證據不足時保持不啟用／不宣稱就緒；選型、離線演算及不依賴缺口的接線可繼續。F18–F20 及 HOST 待辦未因本版發行而關閉。本文件修訂不需等 M4 上雲，也不改其現行 gate。

## 13. 審核整合與 v1.2 差異裁決

### v1.1 初次整合（歷史）

來源為使用者於 2026-09-15 提供的 `HOST-REVIEW.md` 自審與 `COPILOT-CHALLENGE.md`（報告標示 gpt-6-astra／xhigh）。這是文件契約的裁決與修訂，不是本輪重跑複審、漏洞驗證或 runtime PASS。研究的 CODE_VISIBLE／CONFIRMED_STATIC 不自動成為 FA 保證。

下表保留初次修文採用紀錄，不表示後續 delta 已全部關閉；最新處置在下一小節。

| 挑戰 ID／採用優先序 | 合併來源與處置 | 規範落點 |
|---|---|---|
| F01／P1 | 採用：共用批准／次數／支出與撤銷須有同一原子生效點 | BC-04；B09 |
| F02／P1 | 採用：執行失敗與效果狀態分開，不整包重送 partial | BC-04；B05／09 |
| F03／P1 | 採用：outbox、retry、替代通道及下載均在送出前授權 | BC-02／04；B04／09／12 |
| F04／P1 | 合併 HOST P1-4：固定接單原子集合與啟用依賴；待選型不是邏輯矛盾，fake store 不能支持 Hosted 主張 | BC-01／04、§12；B01／04／05／11 |
| F05／P1 | HOST P2-1 升為 P1：在首次一般輸出前隔離／遮罩，失敗拒絕而非事後去敏 | BC-04；B09 |
| F06／P2 | 合併 HOST P2-2／3：保留 delivered 名稱，強制合法組合、required／max 與精確回條綁定，不默降要求 | BC-02；B04／12 |
| F07／P2 | HOST P1-3 改為 P2：硬上限與持久預留；不採「delivery_failed 加責任者即可不再欠答」 | BC-02；B02 |
| F08／P2 | HOST P1-2 改為 P2：短答只省可見 ack，補合法直接 final 路徑 | BC-01／02；B01 |
| F09／P2 | HOST P1-1 改為 P2：明列 B07-C1／C4 主張，不改四槽目標 | BC-03；B07、BR-03／06 |
| F10／P2 | 合併 HOST P2-4：按事件及所影響責任判 silence；等待有期限，可合併文案但不可吞 obligation | BC-02／07；B01／02／10 |
| F11／P2 | 採用：控制面配額、due 去重、root 共用預算與耗盡責任 | BC-01／03；B02／10／11 |
| F12／P2 | 採用：可信防重播窗口／tombstone，不能以 opaque ID 已不存在當新請求 | BC-04；B05 |
| F13／P2 | 採用：runtime 歸屬固定，關閉新入口不丟 drain／交付／核對 | BC-08；B04／06／11 |
| F14／P2 | 採用：必要成果與檢查版本有據，任務成功不等於連結可取或 failure 已交付 | BC-01／02／08；B01／12 |

初次 HOST 的其餘建議已寫入 v1.1：未履行庫存／年齡與確認差距列為 BR-02 觀測，不另增 SLA；Module 保持邏輯分工，外部內容映射、B08 假引用 fixture、需求索引已補。這不包含後續 HOST delta 的全部待辦，亦不以文件修訂替代實作或新增 M4 工作。

### v1.2 定點修訂與仍開放事項

依使用者提供的 `COPILOT-CHALLENGE-v1.1.md` delta 及本次只修 P1 的交辦，採分層裁決：舊 `freeze with nits` 不再作直接啟用基準。該報告將 F02／F04／F07／F09／F11 標為 RESIDUAL，其餘 F01–F14 標為 FIXED，未標 REGRESSION；這些是文件審核狀態，不是 FA 事故或 runtime 驗證。F02／F04 的殘餘由下列 P1 修文承接；F07／F09／F11 對應的 P2 仍待後續處理。

| 挑戰 ID／優先序 | 本版處置 | 修文／反例落點 |
|---|---|---|
| F15／P1 | 文件契約已修：目前無效果不等於終局未生效；非冪等分支要求原 attempt 不可再生效，整單位冪等分支不錯加終局要求 | BC-04 終局性；B05-terminality／B09-terminality，延遲 A／負面查詢／禁止 B 與冪等有效期對照 |
| F16／P1 | 文件契約已修：批准綁 canonicalization 版本、實際 request／script／plan 與解析版本；executor 不在 gate 後重讀 alias | BC-04 實際內容、dispatch 原子集合；B09-execution-binding，批准後／最後 gate 後替換參照 |
| F17／P1 | 文件契約已修：最小必要材料有 pin 世代／引用保護與 GC 排除；失效舊 pin 不成功提交，未知 commit 不解除保護；無協定先停用相應回收 | BC-04 材料存續／原子集合；B01-materials／B04-materials／B11-materials／B12-materials |
| F18／P2 | OPEN：一般 admission 配額與 completion／取消／接手控制通道仍待分流與有界預留設計 | BR-01／02／03；B03／B10／B11，未宣稱滿額仍能排空已驗 |
| F19／P2 | OPEN：所選 SDK 的父子停駐、停止／idle 證據、讓槽與重新取槽協定仍待固定 | BR-03 的 B07-C1、BR-06 的 B07-C4；不以永久排隊當協作成功 |
| F20／P2 | OPEN：能力／配置對應必要反例與範圍 PASS 規則、真 SDK retry 出口及已開放秘密路徑證據仍待固定 | 各受影響 BR／B 子條件；fake client／乾淨最終訊息不代替真 Adapter 證據 |

HOST delta 的 R1（共同序列化權威缺失時的拒啟用案例）、R2（模型補交不可耗用非模型終止通知保留額度）、R3（只對已開放且注入標記的路徑取證）維持 OPEN，隨上述 P2 收斂，不因相關 MUST 已存在就標結案。R4（delivered 不單獨顯示）、R5（需求索引／FR-27–FR-34 對映）、R6（只做 delta 複核）保留為 ADVISORY；本次維持既有合法組合與索引關聯，不另起全面審查。

本版只完成三個 P1 的規格修復與直接對應更新；沒有宣稱全部 review findings 已修完，沒有新的 runtime／Hosted PASS，也沒有自動回收或其他雲端執行授權。
