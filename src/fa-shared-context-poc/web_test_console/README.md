# FA 本機工作台

第一次用工作台：部署後怎麼打開、送第一則訊息、看批准卡和回條，見根目錄 README 的 [前 10 分鐘](../../../README.md#first-10-minutes)。本頁下面是逐次變更紀錄。

## 「待你處理」卡片重畫時保留原卡（dev 2026-10-18 現場，CODE/offline，未部署）

- **現場**：每次 `render()` 都把輸入框上方的卡片區（`attention-dock`）整片丟掉重建（每次約 400 個節點）。內建瀏覽器每 6 秒觸發一次
  `visibilitychange`，按「允許／拒絕」常落在剛被移除的舊卡上，什麼都沒送出（安全，但主人按不到）；一般瀏覽器裡任何重畫
  （例如進度輪詢）也會讓焦點跑掉、正在輸入的回答或規則文字被重設。沒有資料遺失、沒有重送。
- **現在**：`renderInlineAttention` 仍每次畫出卡片，但和卡片區裡同一張卡（同一筆待辦與 `hitlObservationScope`）
  比對畫出來的樣子（標籤、文字、屬性、`disabled`／`hidden`／`open`，不比輸入框內容）；一樣就留下原節點，並把
  `hitlEditors`／`hitlReaders` 指回原卡的輸入框與讀取鈕（原卡已有焦點時，`render()` 不再重設游標）。新卡加入、消失的卡移除、變了的卡換成新畫的；只有卡片順序改變時才移動原卡。
  卡片內容、排序規則與送出的內容都不變；卡片區只有這一處重建。
- 驗證：DOM 測試：同樣的卡重畫後是同一個節點且保留打到一半的字；重畫前綁定的選項鈕仍在卡片區、按下送出一次回答；
  新增／移除一張卡時其他卡不重建；改了的卡重畫、旁邊沒變的卡保留；留下的批准卡打開的「詳細」重畫後仍開著。在 ae3d30e9 五個都 FAIL，修正後整份 679 個通過。
  焦點與輸入法在真瀏覽器的行為未另行驗證（DOM 替身沒有 `document.activeElement`）。

## Release 30 dev 測試：停止與追加的文字（2026-10-14，CODE/offline，未部署）

- **停止後寫「已停止」**（原本就有的問題）：Hosted 把停下的原生回合存成 `status=unknown`、`failure_code=NATIVE_TURN_CANCELLED`，
  頁面原本把它和其他 unknown 一樣寫「這次回覆的結果還不確定。」，停止提示停在「已要求停止這次回覆。」。
  現在回覆泡泡與停止提示（`cancellationMessage`，含輸入框下方）都寫「已停止。停止前可能已經做了部分動作。」
  （停止前可能已經用過工具，所以不說什麼都沒做），處理摘要寫「已停止」（協作還沒確認時寫「已停止；和另一位助理的協作還沒確認」，不蓋掉協作狀態）；同一則裡只出現一次。按「更新回覆」讀到後一樣顯示。
  有處理步驟時，同一則下的摘要列原本仍寫「結果不確定・用了 1 個工具・20 秒」，現在寫「已停止・用了 1 個工具・20 秒」
  （協作還沒確認時是「已停止；和另一位助理的協作還沒確認・…」），工具數和秒數不變。
  其他 `failure_code` 的 unknown 照舊寫「結果還不確定」。
- **追加 `delivery=queued` 的說法**（原本就有的問題，89fa95c5）：queued 是 enqueue：原回合這一輪 SDK 回合先做完，SDK 再把追加當下一輪 SDK 回合，在同一個 FA 回合裡處理，
  原本寫「助理會在這段對話的下一輪處理。」，現在寫「助理已收到，在這次回覆中接著處理。」。其他追加狀態的文字沒有說成下一輪，不變。

## 等待其他助理回覆時的讀取節奏與原生讀取少寫入（審查 #13 與修正第 1 輪，2026-10-14，CODE/offline，未部署）

- **沒變化的讀取先照常讀 30 秒再退避**（release 30 dev 測試與修正第 2 輪，2026-10-14）：等其他助理回覆時，整段讀取
  （不含 `observed_at`）沒變化，前 30 秒照常每 1 秒讀（推送開著時 3 秒），之後才加倍：2→4→5 秒，最多 5 秒，推送開不開都一樣。
  原本第一次沒變化就加倍，交辦的回覆晚 4–8 秒才出現；拿掉退避又讓每個可見分頁在整段等待中每秒打 3+N 個 API GET，
  而等待沒有上限（另一位助理可能停在批准卡上）。有任何變化、推送提示（`realtimeReadProgress`）、分頁回到前景、
  切換對話、按「更新回覆」都回到 1 秒並重新計 30 秒。
- **等太久就說結果未確認**（2026-10-21）：交辦已被接收、但另一位助理超過 2 小時沒有回覆（對方 crash、session 不見，
  或它的結果通知重試約 1.9 小時後放棄），bridge 在 composer 加 `peer_reply_overdue: true`，頁面顯示「另一位助理已接收交辦，
  超過 2 小時還沒有回覆；結果尚未確認。回覆送到後會出現在這段對話，不要重新交辦。」（規格 2026-10-01「等待到期」），
  並改為每 30 秒讀一次。等待不結束、不改原回條、不重送；晚到的回覆照常顯示，分頁回到前景或推送提示時立刻讀。
  2 小時從這個 bridge 最近一次看到交辦回合的回條內容改變算起（`native-read` 列的 `observed_at`；換環境通知出現或清除會
  重新計時），不是對方接單的時間。執行中的新回合照原本節奏讀。
  被版本拒絕的交辦（#915）與對方 unknown／busy 都會送 `agent_notice` 回來，本來就會結束等待。
- **退避不再被重畫打斷**：讀取失敗的退避（2→4→8…最多 30 秒）由 `scheduleProgress` 記下到期時間（`progressDueAt`），
  同一段等待不因 `render()`（60 秒 Team 讀取、登入刷新、推送重連）重新計時。
- **分頁回到前景**：`visibilitychange` 轉為可見時把間隔重設為 1 秒，不必等完隱藏前累積的退避。
- **沒有推送就不退太久**：`realtime.phase` 不是 `open` 時，退避上限 5 秒（沒有事件會通知回覆已完成）；推送開著時只有讀取失敗的退避會到 30 秒。
- **讀取失敗後重新計**：失敗把間隔加倍；下一次成功的讀取從 1 秒重新開始，沒變化的 30 秒也重新計。
- **原生讀取少寫入**：`native.py` 只在回條有變化時才寫 `native-read` 列，並略過記憶體已有的訊息的 journal 重讀；
  輪詢每秒讀同一份回條時不再每次 commit（遠端模式不再每次上傳 journal）。沒有新增 API、權限或保存格式。
- 驗證：DOM 測試 render 不推遲到期讀取、可見後 1 秒內讀取、無推送 ≤5 秒、錯誤後從 1 秒重算（長等待改由讀取失敗造成）；
  節奏測試：沒推送時 30 秒內每 1 秒，之後 2、4、5、5 秒；推送開著時 3 秒，之後 4、5、5 秒；有變化或推送提示回到原節奏。
  在 173b14fe（每次都 1 秒）FAIL。

## 回合結束、等批准與停止不再要手動更新（stage／dev 2026-10-10～12 現場，CODE/offline，未部署）

現場：回覆寫完後一直顯示「回覆中，尚未確認保存。」「正在準備」「停止這次回覆」，按「更新回覆」沒用，只有「詳細 → 更新工作狀態」才變「已完成」；
有些完成的回覆一直「正在載入回覆…」；回合等批准的 10 分鐘裡頁面只讀了一次進度、重新整理後也不讀，按「停止這次回覆」API 沒收到停止。

- **回覆讀取失敗不再擋住回條**：`GET /api/instructions/<rid>/status`（bridge `turn_status`）先存好已完成的回條，再讀回覆；
  原本回覆讀取一失敗（雜湊／長度不符、`/result` 上游錯誤）整個讀取就失敗，頁面拿不到「已完成」，不可重試的錯誤還會停掉輪詢。
  現在回覆的錯誤記在那一則（task `error`），回條照常回給頁面；下一次讀取再讀回覆。登入失敗照舊往上丟。
- **「正在載入回覆…」會結束**：舊回合的 `/status` 讀完（失敗、或沒帶回覆）後重畫，改顯示「尚未載入回覆，可從選單更新。」；
  按「更新回覆」會讓這種回合在畫面上時再讀一次自己的 `/status`。
- **進度讀取有上限**：頁面一次進度讀取最多等 45 秒，逾時照讀取重試的退避再讀（顯示「暫時讀不到進度，正在重試」），晚到的回應不採用；
  bridge 同一段對話的進度讀取排隊最多 25 秒，等不到回 409 `PROGRESS_READ_BUSY`（`automatic_retry: true`），頁面退避再讀。只限 GET，POST 不設逾時、不重送。
  平台歷史讀取（`GET /api/instructions/<thread>/platform-history`）用同一個排隊與同一個上限，等不到時同樣回 409、不送到 API。
- **停止直接送出**：bridge 已知在跑（accepted／started、尚未要求停止）的回合，按「停止這次回覆」直接記 journal 並送 `NativeCancel`，
  不先重讀回條與接續；其他情況照舊先讀。同一個 RID，重按不再送。
- **所有確認都在頁面內問**（dev 8765 2026-10-12 現場：bridge 與 API 都沒收到停止）：原本按「停止這次回覆」先跳 `window.confirm`，
  內嵌瀏覽器（例如 Claude desktop app 的內建瀏覽器）不顯示原生對話框、直接回 false，停止就默默沒送；設定／管理頁另外 20 處確認也一樣。
  現在全部改用同一個頁面內確認框（`#page-confirm`，`role="alertdialog"`，`confirmInPage()`）：原本的問題文字原封不動，
  焦點起始在「取消」（按 Enter 或一直按住 Enter 都不會確認刪除、停用、停止、封存），Tab 只在「確定／取消」間切換；開著時頁面其餘部分（`#app-shell`）設為 `inert`，焦點和點擊都到不了背後，關閉時恢復；按「取消」或 Esc 關閉、不送任何東西；停止與位置固定的按鈕關閉後焦點回到原按鈕，設定頁清單裡的按鈕（確認前頁面已重畫、原按鈕已不在頁面上）不回焦點；按「確定」才送原本那一個請求，
  連按不會送第二次。同時只會有一個問題，開新的會把舊的當成取消。停止的確認在換對話、回合結束或按鈕停用時自動關閉。
  確認框開著時頁面仍會更新，所以每一處在「確定」後再核對一次原本的前提（同一位助理／同一份讀取／同一份草稿／同一段對話與 request ID），
  不符合就不送；request、body 與 request ID 都和原本一樣，沒有新增請求或重送。
- **已完成的回合不需要停止**：API 對停止回了原本、未變的回條，而且狀態是 `completed`（bridge 還以為在跑）時，記成已確認的答覆
  （`cancel_accepted: false`），頁面寫「工作已經結束，不需要停止。」；其他狀態（例如 `busy_refused`）不說已結束，照舊記成停止結果未知，
  頁面提供「核對原停止回條」。
- **API 沒送出的停止可以再按**（#813）：換版期間 API 找不到可安全呼叫的 session 時，停止回帶 `not_dispatched: true` 的拒絕
  （adapter 在 cancel、delivered、append 也讀這個欄位）。bridge 把停止記成 `REFUSED`（原本記成未確認、之後重按都默默不送，
  頁面還寫「傳送結果尚未確認」），頁面寫「停止請求沒有送出：助理正在換版本，這次沒有送到任何工作環境。可以稍後再按一次
  「停止這次回覆」。」；重按在同一筆 journal 上先記回未確認，再送同一個 `NativeCancel`（同一個 RID 與 CID）。送達回條同樣記
  `REFUSED`、可再送，錯誤寫「確認收到沒有送出，請稍後再按一次。」（不用送訊息的說法）。
- **等批准時講明**：回合被主人的批准卡／提問卡擋住時，回覆泡泡寫「這個回合正在等你批准」／「這個回合正在等你回答」。卡片的畫法與位置不變。

還要雲端核對：stage bridge 對那個 RID 的 `/status` 是哪一步失敗（回覆讀取？）、15:15:34 那次進度讀取花了多久、
停止時 bridge 有沒有發出 `GET /requests/<rid>` 及何時回來。`/api/state` 直接在網址列打開（沒有 `X-Console-Agent`）會回 agent 模式的 bootstrap，
其中 `automatic_polling:false` 不是頁面輪詢的開關，頁面也不讀它。

### Verified against（回合結束與停止）

- 程式路徑：`index.html` 的 `updateProgress`／`readOlderTurn`／`scheduleProgress`／「更新回覆」與「停止這次回覆」按鈕；
  `bridge.py` 的 `progress_lock`（`PROGRESS_LOCK_WAIT_SECONDS`）、`instruction_status`／`instruction_progress`／`instruction_platform_history`／`cancel_instruction`；
  `native.py` 的 `turn_status`／`change`／`_refresh_responses`。
- 離線測試：`test_web_test_console_native.py` 新增回覆讀取失敗仍回回條（2 案）、進度讀取排隊上限（2 案）、已知在跑的回合直接送停止、
  已完成回合的停止記 API 原答（`test_a_stop_for_a_turn_that_already_finished_records_the_api_answer`）、
  `busy_refused` 回條不記成已結束（`test_a_stop_answered_with_a_busy_refused_receipt_is_not_recorded_as_finished`）；
  `test_web_test_console_responses.py` 新增平台歷史讀取排隊上限（`test_a_platform_history_read_behind_a_stuck_read_answers_retryable`）；
  `test_web_test_console_workbench.cjs` 新增舊回合讀取失敗／沒帶回覆（2 案）、進度讀取逾時、已完成回合的停止顯示「不需要停止」、
  等批准／等回答字樣（2 案），皆在修改前失敗、修改後通過。
- 頁面內確認：`test_web_test_console_workbench.cjs` 的 harness 把 `window.confirm`／`alert`／`prompt` 換成一呼叫就失敗的替身，
  不再預設回 true，所有 `window.confirm` stub 都拿掉，改用 `ui.answer(true|false)` 回答頁面內的確認框。
  新增 index.html 不含原生對話框呼叫的 guard、新問題把舊問題當取消（2 案）；Esc 案例另驗頁面任一處按 Esc 也關閉不送；
  HITL 規則新增、skill 批准、排程停用、封存並開新 session 各驗「取消不送、確定（連按兩次）只送一次」。
  另有 guard 要求每個 `confirmInPage(` 呼叫處都寫成 `await confirmInPage(`（不符就報 index.html 行號）；
  表格測試（25 列，涵蓋全部 21 個呼叫處）逐一按下動作、按「取消」驗證沒送到該路徑，再按「確定」驗證剛好送一個；
  任一處拿掉 `await` 都會讓對應列失敗。另驗確認框開著時 `#app-shell` 是 `inert`，取消／確定／Esc／失效關閉後都恢復。

<a id="connectors-settings"></a>
## Connectors 設定頁與助理的 connector 開關（C4，2026-10-10，CODE/offline，未部署）

Team 主人在左下 ⚙ →「Agent Team 設定」→ **Connectors** 加入、調整批准、停用與重新啟用 Microsoft Teams；
每位助理的 ⚙ → MCP 頂端多一個 connector 開關，只影響那位助理之後的新對話。所有權限和寫入仍由 API 決定
（C1 `/team-connectors/v1`、C2 `/work-policy/v1/agent-connectors`）；這頁只轉送主人的讀取和一次一個變更。
Team MCP 分頁不變，不混入 OAuth connector 的表單。

bridge 給頁面的資料（`team_connectors.py`）：

| 路由 | 形狀 | 內容 |
|---|---|---|
| `GET /api/team-connectors` | `fa-console-team-connectors-v1` | `can_manage`（目前登入有沒有 `Behavior.Approve`）；每個 connector 一張卡：`connector_id`、`display_name`、`disclosure`、`revoke_url`、`version`（讀到的 toolbox 預設版本）、`status`（`not-added`／`enabled`／`disabled`／`foreign`）、`settings`（`enabled`、`read_approval`、`write_approval`） |
| `POST /api/team-connectors/changes` | 收 `fa-team-connector-change-v1`（API 的整份期望狀態）；回 `fa-console-team-connector-change-v1` | `request_id`、`state=applied`、`toolbox_version`、同上的卡、`original_request_resent=false` |
| `GET`／`POST /api/agent-connectors` | 收 `fa-agent-connectors-command-v1`；回 `fa-console-agent-connectors-v1` | `revision`、`applies_to=new-conversations-only`、`can_manage`、`connectors[]`（`connector_id`、`display_name`、`on`） |

頁面看不到 toolbox 或 connection 名稱、client ID、secret、token、consent URL 或 connection ARM 內容。

- **Teams 卡**：資料範圍說明照 API 的原文（能讀的不只 Teams，也可能包含 Microsoft 365 郵件與檔案）；狀態只寫「已加入」或
  「已停用」，連線狀態一律是「要實際使用才知道」，沒有假的綠燈。「說明」展開後寫明「已加入」不代表授權有效、每位助理各要同意一次，
  以及停用不撤銷授權、要撤銷請管理員到 Entra 系統管理中心（會影響整個組織）。讀取預設不問、寫入預設每次問。
- **第一次加入**才有用戶端密碼欄（`type=password`）。密碼只在欄位裡和那一個請求裡：按下確認就清空欄位，頁面變數、確認框、
  回應與 bridge log 都沒有它。已加入的卡不再要密碼。
- **每個變更**（加入、批准、停用、重新啟用、助理開關）先確認一次、送一次，request ID 在送出前決定。結果不明
  （`TEAM_CONNECTOR_OUTCOME_UNKNOWN`、回應格式不符、連不上）時只顯示「用原編號重送」，送原本的 body；未確認前不能做新的變更。
  加入的結果不明時，頁面沒保存密碼，要再輸入一次才能用原編號重送；API 用同一 request ID 找回原版本，不會建第二個或重寫 connection。
  版本已變（`TEAM_CONNECTOR_CHANGED`）或其他拒絕：重新讀取，不提供重送。停用與重新啟用沿用已保存的批准設定，不夾帶未儲存的修改。
- **非主人**：讀取被拒時只顯示 owner-only 說明，不顯示卡片；主人但沒有 `Behavior.Approve` 時看得到卡片和批准設定的文字，沒有控制項。
  bridge 也在送出前擋下沒有 `Behavior.Approve` 的變更。
- **助理開關**：讀寫的是目前選中助理的 per-Agent narrowing；換助理時清掉舊資料、經新助理重讀；確認框和結果都寫「只影響這位助理之後的新對話」。
- API 的錯誤碼沿用 Team MCP 的名稱，bridge 換成 `TEAM_CONNECTOR_*` 讓文案講 connector；route 不存在時顯示「這個環境還沒開放 Connectors」。

### Verified against（Connectors 設定頁）

- 程式基準 C3 head `099874ba`。已讀 `team_connector_api.py`、`team_connector.py`、`team_connector_wire.py`、`connector_registry.py`、
  `team_connector_narrowing.py`、`behavior_work_policy_api.py` 的 agent-connectors 路由，以及工作台 `team_mcp.py`、`adapter.py`、`bridge.py`、
  `index.html` 的 Team MCP 與設定頁。
- `test_web_test_console_team_connectors.py`：bridge 接真 `/team-connectors/v1`（假 Foundry），驗加入只寫一個 connection 與一個版本、
  失去回應後同一 body 重送拿回同一版本、批准／停用／重新啟用各一版且不帶密碼、版本過期與缺密碼在寫入前被拒、唯讀與非主人、
  格式錯誤不送出、密碼不在回應、錯誤訊息與 caplog、助理開關失去回應後只重送原 body。
  `test_web_test_console_agent_policy.py` 用真兩位助理 bridge 與真 API 驗 B 關掉不影響 A、同一 ID 回原結果、版本過期被拒。
- `test_web_test_console_workbench.cjs`：Teams 卡文字與說明、第一次加入只送一次且頁面任何地方都找不到密碼、結果不明時要重輸密碼再用原 body 重送、
  批准／停用／重新啟用的 body、版本過期不重送、唯讀與 owner-only、助理開關（送一次、結果不明只重送原 body、唯讀、換助理重讀）、
  關閉 Team 設定與助理設定的焦點回到原本的 ⚙。
- 只有離線證據；8765 桌面與 390×844 畫面、dev 實測都還沒做。

<a id="tool-source-unavailable"></a>
## 回覆下方講明這次沒用到哪個工具（Neal 2026-10-10，CODE/offline，未部署）

Neal 2026-10-10：「工具列不出來時，這一回合少那個工具照常跑，並在畫面上講明」。Hosted 端的行為與資料形狀見
[Shared Context README](../README.md#toolbox-list-unavailable)。

- **bridge（`native.py` `task_view`）**：回條的 `assembly.unavailable_sources` 有內容時，投影成
  `native_receipt.unavailable_sources`（每筆 `entry_id`、`source`、`code`、`reason`）；沒有時不出這個欄位。
- **對話（`index.html` `renderConversation`）**：在助理回覆正下方，每個來源一行 `p.reason.tool-source-unavailable`：
  「這次沒用到 {來源}：工具清單讀不到（{原因}），其他功能照常。」原因對照：`upstream_auth` 上游驗證失敗、`source_error`
  工具來源回報錯誤、`service_error` 工具服務暫時無法使用、`network` 連線逾時或中斷、`response_invalid` 回應無法讀取、
  `identity_unavailable` 助理身分暫時取不到，其他值顯示「原因不明」。主行不放代碼；`TOOLBOX_LIST_FAILED` 與原因分類照舊
  在「詳細 → 支援明細」的 `native_receipt` 裡。回覆本身不改、不送任何請求。

### Verified against（回覆下方講明沒用到的工具）

- 程式基準：merge-base `8120b548`（origin/main，2026-10-10 rebase 後）＋本分支 `32a2f6f2`、`45408a67`、`6cb75e6d`、`6b8432cd`、
  `aa0cb163`、`23d096cb`、`5d526b6e`（工作台本身是 `45408a67`；`5d526b6e` 讓同一來源每回合只出一筆，所以畫面也只一行）。已讀 `native.py` `task_view`、`index.html` `renderConversation`
  與支援明細的 `identifiers` 輸出。
- DOM：`node tests/shared_context/test_web_test_console_workbench.cjs` 592 passed；新增 1 案（兩個來源各一行、
  文字逐字比對、緊接在回覆後、不含代碼、支援明細有 `TOOLBOX_LIST_FAILED`／`upstream_auth`、沒有來源時不出現、沒有 POST）。
- bridge：`test_web_test_console_native.py` 62 passed、4 failed；新增 1 案（回條帶 assembly 時投影、沒有時不出欄位）通過。
  4 個失敗是 `test_native_errors_remain_visible_and_do_not_switch_lane[NATIVE_STATE_STORE_MISSING-404|TTL-409|UNKNOWN-503]`
  與 `test_responses_progress_rereads_only_the_latest_or_unfinished_turns`，在 merge-base `8120b548` 同樣失敗，非本分支造成。
- 混版注意：降級回合的 receipt 帶 `unavailable_sources`；舊版 API／工作台（`WireModel` `extra=forbid`、schema
  `fa-native-assembly-v1`）會拒收那則回條，所以 Hosted、API、工作台要一起出版。
- 只有離線證據；未部署 dev／stage，沒有截圖。

<a id="settings-split"></a>
## 設定分拆：助理齒輪與「Agent Team 設定」（Neal 2026-10-08 11:54，CODE/offline，未部署）

Neal 2026-10-08 11:54：「各別Agent的設定，調整位置到每一個agent的選單上…加一個齒輪」；「整個teams的設定，例如Team MCP, Team Skill,
Team Plugin等，都拉除來放在原本左下角的設定齒輪中，選單的名字從"設定"改成"Agent Team 設定"」。只改 `index.html`；
API、bridge、權限與請求內容都沒變。設定頁還是同一頁（`#settings-page`），只是分成兩個範圍，各自只顯示自己的分頁：

| 入口 | 標題 | 分頁 | 內容 |
|---|---|---|---|
| 助理清單每一列右側的 ⚙（`button.agent-settings`） | 「{助理名稱} 的設定」 | 已安裝（含角色說明）、Skills、MCP、Plugins、排程（有開才顯示） | 這位助理新對話用哪些 skill／MCP／內建能力與「套用到新對話」、這位助理的排程 |
| 左下 ⚙ →「Agent Team 設定」（原「設定」） | 「Agent Team 設定」 | Team MCP、Team Skills、批准規則 | Team MCP 新增／修改／停用；skill 審核、批准、停用、取代、自己寫草稿；Team 真人批准規則 |

- **助理齒輪**：每列是 `div.agent-row`，裡面是原本的列按鈕（點了切換助理，行為不變）和一顆獨立的 ⚙ 按鈕
  （`aria-label`＝「{名稱} 的設定」，原生按鈕可用 Tab／Enter／Space）。⚙ 不在列按鈕裡面，所以點它不會觸發列的點擊；列不可用時 ⚙ 也停用。
  點**目前選中**的助理的 ⚙：直接開它的設定，不切換、不重讀對話。點**別位**助理的 ⚙：先 `switchAgent`（和點那一列相同，
  同步換成那位助理、背景讀它的狀態），再開它的設定；讀取中顯示「正在讀取這位助理…」。這樣所有設定請求、防重送與「結果未確認的選擇」
  （`template_selections`）仍然只綁目前選中的助理，不會送錯或顯示別位助理的資料。頁面頂端的助理下拉拿掉了。
- **Agent Team 設定**：Team MCP（`#settings-team-mcp`）和 skill 審核（`#settings-skill-reviews`）從助理的 MCP／Skills 分頁搬出來，
  各自成為分頁；批准規則照舊，右上「⋯ → Team 批准規則／原操作紀錄」仍直接開到這裡。授權路徑不變：仍經 API、Team 主人身分，
  用目前選中的助理的 `X-Console-Agent`；副標寫明「整個 Team 共用，不屬於任何一位助理；這頁經由目前選中的助理「X」讀寫你的 Team」，
  還不能讀時說明原因；Team MCP 只有主人看得到時也寫在副標。開這頁不讀助理的新對話設定。
- **Team Plugin**：目前沒有 Team 層級的 plugin（Plugins 分頁只有每位助理的內建能力開關），所以沒有新做。
- **不留舊資料**：原本「換助理就清空設定頁」只在讀過助理設定後才觸發；Team 頁不讀助理設定，改成「登入狀態或目前助理一改變就清空」，
  助理設定、Team MCP、skill 審核、排程一起清，進行中的回應作廢，換到的助理再重讀。
- **skill「批准並取代」第 3 步**：前兩步（批准新的、停用舊的）在 Team 頁做；第 3 步會改目前選中助理的新對話設定，
  所以讀到那位助理的設定後，切到它的設定 → Skills，看得到要換的內容，再照舊跳確認框、只送一次。確認文字點名是哪位助理。
  讀取失敗時停在 Team Skills，那裡有「繼續取代」，只接著做第 3 步，不重送前兩步。
- **文案**：「套用到新對話」的確認框點名助理；Team MCP、skill 審核與排程卡片原本的「在下方打開」「設定 → 排程」，
  改成「助理清單右側的 ⚙ → MCP／Skills／排程」。
- 現有行為不變：request ID、`expected_version`／ETag、卡片外的確認框、讀取與錯誤訊息、手機版開設定時收起側欄、Escape 關閉。
  關閉助理設定時焦點回到那位助理的 ⚙，關閉 Team 設定回到左下 ⚙。

### Verified against（設定分拆）

- 程式基準 main `2163242a`。已讀 `index.html` 的 `renderAgents`、`switchAgent`、`renderSettings`、`loadSettings`、
  `teamMcpTask`／`skillTask`／`routineSettingsTask` 與其 live 檢查、`continueSkillReplace`、`openSettings`／`closeSettings`、Escape 處理。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：577 PASS。新增 4 案
  （目前助理的 ⚙ 不觸發列的點擊、不切換、不重讀，關閉後焦點回到它；Team 頁只有 Team 分頁、不讀助理設定、副標點名助理、選單改名；
  Team 頁換助理後清掉舊 Team 資料並經新助理重讀；取代第 3 步讀取失敗時停在 Team 頁，「繼續取代」不重送前兩步），
  改寫 1 案（別位助理的 ⚙ 先切換、只讀那位助理、讀取中不顯示拒絕），
  其餘設定、skill 審核、Team MCP、排程測試改從新入口打開，並檢查助理頁沒有 Team 區塊、取代第 3 步切到助理頁。
- 反向驗證：換回 main 的 `index.html`，6 則新／改寫測試全失敗；對新版逐一破壞（⚙ 連帶觸發列點擊、別位助理不先切換、
  清空條件改回只在讀過助理設定後、Team 頁也顯示助理分頁、取代第 3 步留在 Team 頁、讀取前就切頁、保留舊錯誤、
  關閉後焦點回輸入框），各自讓對應測試失敗。
- 鍵盤：在無頭瀏覽器實按，Tab 順序是「列 → 那列的 ⚙」，在 ⚙ 上按 Enter 或空白鍵都開到那位助理的設定。
- 畫面：自帶的無頭 Chromium（`playwright-core` 1.63.0，裝在工作階段暫存區、不進 repo），所有請求由腳本以假資料回應，
  沒有開 port、沒用 8765、沒碰共用瀏覽器；拍了修改前後與手機寬度的對照圖。
- 只有離線證據；未部署 dev／stage。

<a id="routine-from-chat"></a>
## 從對話建立排程：批准卡與「排程」分頁（Neal 2026-10-07，CODE/offline，預設關閉、未部署）

設計、官方依據與 API／Hosted 的界線見 [Shared Context README](../README.md#native-routine-proposals)；這裡只記工作台的部分。

- **批准卡**：助理呼叫 `propose_routine` 時，既有批准卡的類別是 `routine`。摘要寫「要排程：每天 HH:MM（時區）在這個對話執行：「…」」，
  理由寫明只有主人在卡上按「允許這次」才算、助理的話或對話裡的回覆都不算，並指到「助理清單右側的 ⚙ → 排程」（10-08 前是「設定 → 排程」）。沒有「以後允許這類」
  （和 Toolbox 的 always 一樣），按一下就送，不另跳確認。按完卡片變成紀錄「助理要排程：…／你允許了這次」；
  bridge 把同一份白話（`display.routine`：時間、時區、內容，各截 500 字）存在決定旁邊，重新整理後一樣顯示，不進 command。
  決定失敗時，卡片除了原本的錯誤，另提示到「助理清單右側的 ⚙ → 排程」核對是否已建立（API 先寫排程再記決定）。
- **排程分頁**：只有 bridge 的 `FA_NATIVE_ROUTINES_ENABLED=1` 時，`/api/state` 才帶 `routines_available=true`、顯示分頁。
  `GET /api/routines` 依序讀完 API 的 definitions 分頁（`after`，最多 50 頁），只列 `kind=routine`、這位 Agent、這位主人
  （`owner_oid`＝登入者）的排程：時間、時區、內容、啟用中／已停用、來源對話前 8 碼、批准時間。
  `POST /api/routines/{routine_id}/versions/{version_id}/disable`（body `{}`，走既有 ≤16 bytes 規則）要 `Behavior.Approve`：
  bridge 先讀目前版本，已停用就直接回那一份；仍啟用但不是畫面上那個版本回 409 `ROUTINE_SETTINGS_CHANGED`；
  否則送同內容、`enabled=false`、`expected_version_id`＝畫面版本的新版本，請求 ID 由那個版本決定。頁面停用前先跳一次確認。
- **Agent 編號**：agent 模式用 `X-Console-Agent` 的 prefix；單一 Agent 且沒有 Agent scope 的設定檔，用這個工作台自己的
  native 回合裡唯一的 Agent，還沒有回合時回 `ROUTINE_SETTINGS_NO_AGENT`（請先送一則訊息）。
- adapter 讓 `/native-hitl/v1/*` 與 definitions 路徑上的 `NATIVE_ROUTINE_*` 錯誤碼原樣傳回，definitions 讀取可帶 `after`。
- **讀到排程回合（10-08）**：排程回合不是這頁送的回合，原本對話一安靜就不再讀，切回 t3 要按「更新回覆」才看得到（dev #732 P4）。
  現在 `switchThread`／`switchAgent` 讀完本機狀態後，標記讀一次整段對話（`/api/instructions/progress`）；第一次打開的對話仍先快速讀最新回合，
  之後才整段讀一次。有推送時，這位助理的 `turn.finished` 若不是這頁送的回合（例如排程回合），也讀一次目前對話；別的助理只標未讀，照舊。

### Verified against（從對話建立排程：工作台）

- 程式基準 main `f553d8a9`。已讀 `index.html` 的 `renderHitlCard`、`sendNativeHitlDecision`、`hitlDisplay`／`hitlActionText`、
  `renderSettings`、Team MCP 分頁（範本）；`native_hitl.py` 的 `action_display`／`decide`；`adapter.py` 的 `send`；
  `bridge.py` 的中介層與 `state`；`team_mcp.py`、`native_routines.py`。
- `tests/shared_context/test_native_routine_proposals.py` 的工作台三則：真 API＋bridge 下，按卡片允許後紀錄帶 `display.routine`、
  分頁列出一筆、停用寫一個新版本、再按一次不再寫；舊版本被拒、帶 body 被擋；沒開旗標時 `routines_available=false` 且路由回
  `ROUTINE_SETTINGS_UNAVAILABLE`。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：568 PASS（新增 6 案：卡片文案與
  只有一次批准、決定失敗提示核對、存檔紀錄重讀、分頁只在開啟時出現、停用綁畫面版本且回應遺失時重送同版本、取消確認或唯讀不送）。
- 反向驗證：卡片又出現「以後允許這類」、摘要不寫排程內容、停用不核版本，各自讓對應測試失敗。
- 10-08 讀到排程回合：DOM 573 PASS（新增 5 案：切回對話讀一次並顯示排程回合、第一次打開先快讀最新回合再整段讀、推送讀進非本頁回合而別的助理不讀、
  快讀失敗（503／斷線）照退避不連續重讀）；反向：切換後不讀、快讀後不再整段讀、推送忽略非本頁回合、快讀失敗不清標記，各自讓對應測試失敗。
  推送：這位助理的 `turn.finished` 只帶 RID，所以凡不是本頁送的回合（排程、交辦或別段對話）都讀一次目前對話（同一事件只讀一次）。
- 只有離線證據；未部署 dev／stage。

<a id="typing-prewarm"></a>
## 打字時先叫醒 VM（Neal 2026-10-07，CODE/offline，預設關閉、未部署）

Neal 2026-10-07 15:47：「是否要做『打字時先叫醒 VM』← 先在Dev做驗證，取得實際的效果」。設計、官方依據、費用與 API／Hosted
的界線見 [Shared Context README](../README.md#typing-prewarm)；這裡只記工作台的部分。

- **開關**：bridge 的環境變數 `FA_CONSOLE_PREWARM=1`（恰好是 `1`）。`serve` 與 `serve-remote` 都讀；`preview` 永遠關閉。
  開著時 bridge 把頁面裡的 `const PREWARM_CONFIGURED=false;` 換成 `true`（和 `REALTIME_CONFIGURED` 同一種作法）；
  沒開時頁面完全不發，bridge 的路由也回 404 `PREWARM_DISABLED`。stage 遠端工作台由 `deploy/stage/stage.json` 的
  `console_environment` 設定，開關與生效時機見 [Shared Context README](../README.md#typing-prewarm-stage)。
- **何時發**：只在輸入框的 input 事件（真的在打字），而且：native lane（`foundry-native-v1`／`foundry-responses-v1`）、
  框裡有字、已登入、這位 Agent 可用、這段對話裡自己的回合沒在跑（那時 VM 本來就醒著）。同一位 Agent 每 2 分鐘最多一次
  （`PREWARM_INTERVAL_MS`）；打開或切換對話、頁面載入時帶回的草稿都不發。
- **發什麼**：`POST /api/instructions/prewarm`，body `{}`，帶這位 Agent 的 `X-Console-Agent` 與頁面的 CSRF 值。bridge 轉成
  API 的 `POST /work-policy/v1/native-turns/prewarm`，回 `{"prewarm":"sent"|"recent"|"skipped"}`；API 回應格式不符時回
  `{"prewarm":"unconfirmed"}`（頁面一樣不看）。
- **不做的事**：頁面不等它、不顯示它的結果或錯誤；送出照舊立刻 submit，不會排在它後面。bridge 不拿 message lock、
  不寫 journal、不改任何狀態，所以它沒有「結果不明要核對」這回事；失敗就只是這次沒叫醒。

### Verified against（打字預熱：工作台）

- 程式基準 main `2716dcd9`。已讀 `index.html` 的 `renderComposer`、`prefetchNextDraft`、`runningOwnTurn`、`agentFetch`、
  `remoteResponse`（輸入框有字時不會因 `REMOTE_SESSION_CHANGED` 重新整理）、`postBridge` 與 input 事件；`bridge.py` 的中介層
  （POST 需同源、CSRF、JSON、`{}` 且 ≤16 bytes）、`create_app`、`requested_console`；`realtime.page`；`__main__.py`。
- `tests/shared_context/test_web_test_console_prewarm.py`（8）：沒設定時頁面標記是 `false`、路由 404、不碰 API；設定後標記是
  `true`，轉送一個 `{}` 到這位 Agent 的 `/work-policy/v1/native-turns/prewarm`、journal 不變；API 拒絕照原碼傳回；帶欄位或
  CSRF 不符被擋；只有恰好 `1` 才開；`serve` 從環境變數讀。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：562 PASS（新增 6 案：
  打字送一次、2 分鐘後才再送；預熱還沒回時按送出立刻 submit；預熱被拒什麼都不顯示；沒設定不送；自己的回合在跑或非 native
  不送；帶所選 Agent 的 `X-Console-Agent`）。
- 反向驗證：bridge 不檢查開關、頁面不看開關、不看回合是否在跑、不限 2 分鐘，四項各自讓對應測試失敗。
- 只有離線證據；未部署 dev／stage。

<a id="desktop-running-session"></a>
## 換版後第一回合進行中，桌面就連到正在跑的 VM（Neal 2026-10-07，CODE/offline，未部署）

Neal 2026-10-07 13:12 回報 stage 第 20 版「桌面無法連線」：換版後第一個回合已在新 VM 執行，即時桌面卻一直連換版前
那台已閒置的 VM 而被拒（`DESKTOP_VM_NOT_RUNNING`），要等回合結束、頁面重讀整段對話才換過去（第 18 版也看過，約 27 秒
後才換）。只改工作台（`native.py`、`index.html`）；API、Hosted 沒動。

- **根因**：桌面用 composer 的 `hosted_session_id`，它取自 bridge 記得的對話。回合進行中頁面只讀這一回合的 `/status`，
  不重讀對話；換版前讀的對話（或工作台重啟後還沒讀對話時的備援：第一筆、也就是最舊的回條）仍指舊 session。
- **修法**（`native.py` 的 `running_session`，不多讀任何東西）：回條上的 `hosted_session_id` 是接單並執行該回合的
  session（Hosted 以自己的身分寫入，回合只在同一個 session 開始，見 `native_turns._original_session`）。在「進行中
  （`accepted`／`started`）的回合」與「回合號大於所記對話 `last_turn` 的回合（表示記住的對話比它舊）」中取最新的一則：
  還沒配到回合號的算最新，同號取較晚記住的。同一位 Agent 一次只跑一個回合，所以仍記成進行中、但後面已有更大回合號的
  回條是舊的讀取，不採用。都沒有時照原本用對話的答案（`execution_hosted_session_id`，沒有就 `hosted_session_id`）。
  還沒讀過對話時（例如 stage 部署重啟工作台後），改取回合號最大的回條，不再取最舊的；都沒有回合號才照原規則用第一筆。
- **沒選的做法**：送出時順手重讀整段對話——多一次讀取，會拖慢剛壓下來的送出時間（[RTT A](#rtt-a-round-2)）。
- **影響範圍**：這個欄位只有頁面在用——桌面「連線」、批准卡的「開啟既有助理電腦面板」、「本對話綁定 Hosted session」
  說明。三處本來就是「現在是哪台 VM」（原本就顯示 `execution_hosted_session_id`），現在只是不用等回合結束；所以沒有
  另拆 `desktop_session_id`。append 用回條本身、reset 走舊路線的 projection，都不讀它。
- **頁面**：已連線時若同一對話換到另一台 VM，照原規則斷線，提示「這個對話換到另一台 VM 執行，桌面連線已中斷；按「連線」
  看新的桌面。」，不自動重連；換助理或對話仍是原本的「已切換助理或對話」。`DESKTOP_VM_NOT_RUNNING` 補一句「送出訊息、
  助理開始回合後再按「連線」。」（修正後回合進行中就連得上，所以不寫「回合結束後」）。

### Verified against（桌面跟著正在跑的 VM）

- 程式基準 `main` `cc868cc4`。已讀 `native.py` `view`／`turn_status`／`refresh`／`_refresh_responses`／`_remember`；`bridge.py`
  `instruction_status`／`instruction_progress`；`index.html` `desktopSession`／`syncDesktop`／`connectDesktop`／`disconnectDesktop`／
  `runningOwnTurn`／`updateProgress`；`shared_context/native_turns.py` `_submit_locked`（回條的 `hosted_session_id`）／
  `_original_session`／`_allocate`（`turn_number`＝開始時的 `meta.n+1`；對話的 `hosted_session_id` 建立後不變）／`conversation`；
  `behavior_desktop_view_api.py` `_authorize`（這位 Agent 的任一 session，Foundry 狀態 active 就接受）。
- 離線：`pytest tests/shared_context/test_web_test_console_native_environment.py tests/shared_context/test_web_test_console_desktop.py`
  21 PASS。新增 `test_the_desktop_names_the_session_the_first_turn_after_a_follow_runs_on`（換版前讀過對話 → 換版 → 送出第一回合；
  送出後、進行中、結束三個時點都指新 session，期間只讀這一回合）與 `test_the_running_session_prefers_...`（規則邊界）。
  DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs` 556 PASS，新增
  「the live desktop follows the conversation to another VM and asks to connect again」。
- 反向驗證：composer 改回原算式，換版那則拿到舊 session 而失敗；`index.html` 換回 `main` 版本，DOM 新案失敗。
- 工作台 `test_web_test_console_*`（41 檔）＋`test_native_console_runtime.py`：591 PASS；另 4 FAIL＋1 ERROR 在 `main` `cc868cc4`
  同樣重現，與本改動無關（3× `test_native_errors_remain_visible_and_do_not_switch_lane`、
  `test_responses_progress_rereads_only_the_latest_or_unfinished_turns`、appends 測試缺 pinned runtime 1.0.83）。
- 限制：只有離線證據；未部署 dev／stage。live 待部署線在 dev 驗「換版後第一回合進行中，桌面就能連上」。

<a id="hitl-one-click"></a>
## 批准／提問卡一按就送、送出後留下問答紀錄、按鈕重做（Neal 2026-10-07，CODE/offline，未部署）

Neal 2026-10-07 08:29（附 stage 截圖）：「按下HTLT的按鈕時，應該可以直接送出，不要在跑出一個確定按鈕」、「按下送出後，應該馬上把
問題與user的回答顯示在畫面中(可以參考Copilot App的畫面)，接著馬上顯示agent收到user決定後的思考與工作過程」、「給user的按鈕有點醜，
需要提高質感」。只改工作台（`index.html`，以及 bridge 的 `native_hitl.py`／`native_questions.py`）；API、Hosted 沒動。

| 改了什麼 | 參考做法 | FA 現在 |
|---|---|---|
| 卡片按鈕先跳瀏覽器確認框（提問卡的選項、「送出我的回答」；批准卡的「允許這次」「拒絕」「確認規則並允許」） | Copilot App 的提問：點選項就送出（Neal 附圖二）；Grok Bot 把提問與批准放在對話時間軸裡處理（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)）。確認框本來就不是防重送機制。 | 一按就送。防重送照舊：按下的同一次重畫，整張卡換成紀錄，舊按鈕再按也因同一個請求還在進行而不送。提問的 request ID 由原問題決定，同一題再按、換答案或重開工作台都只回原紀錄；批准的 request ID 由原 call 加這次的選擇決定，同一個選擇只送一次，換另一個選擇時 bridge 先重讀原 call，已有決定就拒絕（H1 也只留第一個決定）。結果不明只能「只核對原回答／只核對原決定」。「以後允許這類」仍是兩步：先展開規則草稿，按「確認規則並允許」才送。卡片以外的確認（設定、skill、MCP、規則清單刪除、停止回覆、封存對話）不變。 |
| 送出後卡片只多一行「已送出回答：…」，下一次讀取後整張卡消失，畫面沒有留下問答；回合進度停在「正在等你回答」直到下次讀取 | Copilot App：「Asked user: …」下一行「Answer: …」，接著是助理的工作過程（Neal 附圖二）。 | 按下當下卡片收成兩行：「助理問：…」「你的回答：…」；批准卡是「助理要執行：命令」（或「助理要使用：工具（目標）」、「助理要使用 Toolbox 工具：名稱」）加「你允許了這次」／「你允許了，並新增規則「…」」／「你拒絕了」；別的助理的卡寫出那位助理的名字。送出中加小字「送出中…」，bridge 回了原紀錄（或核對後讀回原回條）才拿掉，不寫「已送達」。被拒（例如已過期）就展開回原卡片並顯示原因，同時立刻讀一次狀態：若 bridge 已把這次存成「結果不明」，卡片馬上換成「還不確定有沒有送到」與核對按鈕。同一次重畫，回合進度從「正在等你回答」換成目前動作（例如「正在回覆」），上方「需要處理」與標題列也不再列它。 |
| 卡片放在回合最後（助理回覆與進度之下） | Copilot App 的時間軸：問答之後才是助理接著做的事。 | 有卡片或問答紀錄的回合，這一格放在你的訊息之後、助理回覆之前；助理收到決定後的串流文字與進度就出現在紀錄下方。沒有卡片的回合版面完全不變。**（Neal 2026-10-12 修訂）** 還在等你的批准／提問／Connect 卡改放在輸入框正上方（不在聊天捲動區內，不必捲動就看得到，太高時卡片區自己捲動）；回合那一格只留一行「卡片在輸入框上方」，按下後卡片換成紀錄留在回合裡。其他對話的卡片位置不變。卡片區內多張卡片依建立時間由舊到新排列，2 張以上頂端顯示「有 N 張待處理卡片」；總覽的「查看原回合」會捲到卡片區並聚焦該卡。 |
| 選項相連沒有間距、自由回答框三行寬且送出鈕在下一行、批准三鈕同一樣式 | Copilot App／GitHub 深色主題的選單與按鈕 | 選項是有編號的整列（細邊框，滑過與鍵盤焦點用主題強調色），上下鍵可在選項間移動、Enter／空白鍵選取；自由回答框和「送出」併成一列（Enter 送出、Shift＋Enter 換行，輸入法選字時的 Enter 不送）；批准卡「允許這次」是主要按鈕、「以後允許這類」次要、「拒絕」紅色外框。顏色全用既有 `--cp-*` 變數，深色、淺色主題都適用。 |

**重新整理或切換對話後紀錄還在：** 來源是 bridge 已保存的原紀錄（`composer.native_questions.commands`、`composer.native_hitl.commands`），
依它的 `selection.call` 放回原回合、依保存時間排序。為了能再顯示「助理問：…」，bridge 寫入原紀錄時另存一份畫面摘要 `display`
（提問：題目文字；決定：tool、category、target、shell 命令前 500 字、是否 Toolbox），history 帶出 `display` 與 `created_at`。
`display` 不在送出的 command 裡，也不參與 request ID 或核對；這次之前保存的紀錄沒有它，畫面改寫「助理問了你一個問題」「助理要使用一個工具」。
同一個 call 若因結果不明留下兩筆本機原紀錄，畫面顯示已確認（或核對後讀回原回條）的那一筆。

**已知限制（不影響保存或重送）：** B 的卡在 A 的對話裡回答時，原紀錄存在 B 的 bridge，掛在 A 的哪一回合只來自當時的 Team 待辦清單；
所以紀錄只在這次頁面留在 A 的回合，切換對話或重新整理後不在 A 這邊顯示（B 有那段收件對話時，從 B 那邊看得到）。要補，需要另讀 B 的紀錄並保存掛載位置，留待總協調決定。

**串流：** 卡片期間，回合的 Responses 串流（[RTT B](#rtt-b-workbench)）一直開著，回答不會關掉或重開它（dev smoke：等批准 53 秒同一串流不斷，
批准後同一個 response 繼續）。串流要連續 4 段都沒有事件（API 每段約 230 秒）才放棄。2026-10-10 起真人自己回合的卡片沒有可見期限、可以等很久；
串流放棄後照原本 `/status` 輪詢與推送，卡片與決定不受影響。

**等你決定（2026-10-10，Neal 決定，對齊 [Grok Bot approvals](https://docs.x.ai/grok-bot/approvals-security-and-privacy)）：**
`wait` 為 `interactive` 的批准卡只寫「等你決定」，提問卡寫「等你回答」，不顯示時間；詳細裡也不列期限。按鈕一直可按，
直到狀態變成已決定、中止或回合結束（API 的 24 小時清理上限到了也會變成已到期）。`background`（排程、其他 Agent 的回合）
和沒有 `wait` 的舊卡照舊顯示「…前有效／前回答」。

**不變（防資料損失、重送與明顯錯誤）：** request ID、journal 先以 FULL 寫入再送一次、UNKNOWN 只核對、Team route、`Behavior.Approve`、
期限與規則版本核對、「批准不等於效果完成」都沒有改。

### Verified against（卡片一按就送）

- 程式基準 `main` `3ff0751b`。已讀 `index.html` `sendQuestionAnswer`／`renderQuestionCard`／`sendNativeHitlDecision`／`renderHitlCard`／
  `requestNativeHitl`／`renderAttention`／`renderInlineAttention`／`processingActivity`／`processingPanel`／`renderConversation`／
  `syncLiveText`／`openLiveText`；`native_hitl.py` `decide`／`_once`／`history`；`native_questions.py` `answer`／`history`／`refresh`；
  `native.py` `view` 與 running turn 的 `/status`（只刷新原 RID 的 HITL／提問）。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：555 PASS。新增 7 案（按下當下卡片變紀錄、
  回合與標題列不再等你、串流不斷且接著顯示；重新整理／切換對話後紀錄留在原回合且排在回覆前；被拒時卡片展開並顯示原因、不自動再送；
  結果不明的決定只核對、核對讀回原回條後就算確認；送出失敗立刻讀狀態、同一 call 顯示已確認的那筆決定；Enter 送出與 Shift＋Enter、
  輸入法選字不送；上下鍵移動選項），改寫 12 案（原本斷言確認框或卡片內「已送出回答」的提問與批准案例）。換回 `main` 的 `index.html`
  時，第一輪的 18 案有 17 案 FAIL（改寫的「規則已滿或過期」案只拿掉確認框取消那一步，兩邊都通過）；兩軸 review 後補的 3 項
  （標題列、核對讀回、失敗後立刻讀）在補之前的頁面上 3 案 FAIL。
- Python：`python -m pytest tests/shared_context/test_web_test_console_questions.py tests/shared_context/test_web_test_console_hitl.py
  tests/shared_context/test_web_test_console_team_attention.py tests/shared_context/test_web_test_console.py
  tests/shared_context/test_web_test_console_routines.py`：63 PASS。新增 1 案、加強 1 案（決定與回答保存 `display`／`created_at`，
  送出的 body 不含 `display`）；換回 `main` 的兩支 bridge 檔時 3 FAIL。
- 截圖：離線假資料（本機 8799，Playwright，深色／淺色／窄視窗），不是 dev／stage 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="rtt-a-round-2"></a>
## 按下送出就看到助理在忙、續問不再先拿編號、連線不再每回合重連（RTT A 第二輪，2026-10-07，CODE/offline，未部署）

#706 部署到 dev 後實測（量測線 `rtt-a-dev-dee2a517-20261007`，A 暖回合中位數）：「助理開始動作」3.25 秒、第一段文字 10.3 秒；
Neal 的目標是前者 ≤ 1 秒。總協調 10-07 01:4x 交辦三項，只改工作台：

| 拿掉／改掉 | 參考做法 | FA 現在 | 預估省下（依 #706 dev 實測） |
|---|---|---|---|
| 按下送出後，回覆泡泡寫「正在傳送你的訊息…」，主角是訊息，看不出助理在忙 | charter-agent 送出時先加 user bubble 與帶 `thinking` 的 agent bubble 並設 busy，之後才呼叫 bridge（[研究筆記第 13 行](../../../docs/charter-agent-desktop-client-research.md)；[app.js#L756-L767](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L756-L767)）。 | 按下送出當下（dev 量到 5～9 毫秒）就顯示「助理準備中…」並閃動；只說助理這一側在準備，不說已接單或已收到（AGENTS 第 7 條）。伺服器接單後換成「正在準備回覆…」；被拒時照原規則顯示「沒有送出」等字樣。 | 畫面上助理開始動作約 3.25 秒 → 按下即出現（量測規則由量測線對照調整） |
| 續問時先 `POST /api/instructions/{thread}/next` 拿下一個 RID，再送 submit（dev：220～290 ms） | charter-agent 按下送出只發一個請求（同上研究筆記）。 | 上一回合結束後，輸入框一有文字（打字，或回合進行中打好的字），頁面就先向本機 bridge 拿這段對話的下一個 RID，每個已送出的 RID 只拿一次；按下送出時直接 submit。拿 RID 的請求還沒回來時，送出會等同一個請求，不會再拿第二次。打開或瀏覽對話不拿。輸入框裡還沒送出的文字、模型與附件，在這段對話的新 RID 輸入框出現時跟著搬過去（不論是 `/next` 回覆或別的讀取先帶來新 RID，或拿 RID 途中切走再回來）；新 RID 底下已經打了字就不蓋掉；原 RID 正在送出時不搬，由送出本身處理，避免同一則訊息再出現在可送出的輸入框。`/next` 的回覆不帶頁面的 CSRF 值，套用時照 `run()` 保留原值。 | 送出到 submit 開始約 0.22～0.29 秒；「正在準備回覆」與第一段文字都提早 |
| bridge 連 API 的 httpx 連線閒置 5 秒就關（暖回合約隔 12 秒，每次送出都重做 TCP＋TLS；dev：閒置 ≥6 秒 317 ms、連線還在 74 ms） | Container Apps 入口預設閒置 4 分鐘才斷線（[Premium ingress，request-idle-limit 預設 4 分鐘](https://learn.microsoft.com/azure/container-apps/premium-ingress#configure-premium-ingress)）。 | 連線保留 120 秒。原本寫在 `httpx.Client(limits=...)` 的設定，在指定 `transport=` 時 httpx 0.28.1 不採用（`Client._init_transport` 直接回傳傳入的 transport），所以實際一直是 httpx 預設（100 條、保留 20 條、5 秒）；現在把設定放在 transport 上，只改保留時間，連線數維持實際在用的 100／20。 | 暖回合的送出約 0.24 秒；結束後的 `/status` 讀取在連線閒置超過 5 秒時也約 0.24 秒 |

合計估算：伺服器接單後的「正在準備回覆」約 3.25 → 2.7～2.8 秒，第一段文字約 10.3 → 9.6～9.8 秒；要等 dev 部署後實測。

**不變（防資料損失、重送與明顯錯誤）：** RID 一樣在送出前固定，送出只用 bridge journal 已配好的 RID；bridge 的 `/next` 在目前草稿還是空的時候
回同一個 RID，所以預先拿和送出時拿只會得到同一個編號。先拿的 RID 只是本機一筆空草稿，不送任何東西，也不會出現在對話或對話清單裡。
以下情況不先拿：結果還沒核清的追加文字還在輸入框（避免它變成新訊息）；這則訊息正在等同 RID 換版重送；回合還在進行或可以追加；
正在送出；新對話正在開。換了 RID 的對話不再被當成「還要讀」而一直輪詢。送出去重、UNKNOWN 不重送、回條核對都沒改。
保留的連線若剛好被對方關掉，httpx 不會自動重送（`retries=0`），送出照舊記成「傳送結果尚未確認」、只核對同一個 RID。

### Verified against（RTT A 第二輪）

- 程式基準 `main` `1e0fa123`。已讀 `index.html` `sendChat`／`renderComposer`／`renderConversation`／`scheduleProgress`／`runningOwnTurn`／
  `pendingAppendDraft`／`clearSentDraft`、`bridge.py` `Console.__init__`／`new_instruction`、`native.py` `follow_up`／`view`、
  `instructions.py` `new`／`current`／`all`／`threads`；httpx 0.28.1 `Client._init_transport`（指定 transport 時不用 `limits`）；
  量測依據為量測線報告 `rtt-a-dev-dee2a517-20261007`（工作階段 `2407d6bd`，未入 repo）第 0、2 節與量測工具的 (b) 規則。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：541 PASS。新增 6 案（打字後先拿
  RID、送出第一個請求就是 submit 且帶原 CSRF 值；拿 RID 途中送出只等同一個請求；拿 RID 途中切走再回來，文字還在；別的讀取比 `/next`
  先帶來新 RID，輸入框文字不消失；送出還在等 RID 時別的讀取先帶來新 RID，不會再送一次；已拿 RID 的已結束對話不再輪詢），改寫 2 案
  （等待泡泡改為「助理準備中…」；回合中打的字在結束後跟著換到新 RID），8 案在 `main` 的 `index.html` 上 FAIL。
  替身的 `/next` 回覆照真的 bridge 不帶 `csrf`（兩軸 review 指出原替身每份 state 都帶 `csrf`，遮住了送出被 403 擋下的錯誤；CSRF、切走、
  「別的讀取先到」與「不會再送一次」四案在各自修正前一版上 FAIL）。「接單後畫面立刻更新」一案的反向斷言換成新字樣。另 8 個測試定義（迴圈展開後 11 案）原本斷言
  「打字後沒有任何 `/next`」，改成「沒有 submit、`/next` 最多一次」，或改用 URL 找自己的 POST；結果未核清的追加與等同 RID 換版重送兩種情況
  不先拿 RID，原測項不改即通過。
- Python：`python -m pytest tests/shared_context/test_web_test_console.py`：7 PASS。新增 1 案（bridge 連 API 的連線保留 120 秒、
  連線數維持 100／20），在 `main` 的 `bridge.py` 上 FAIL（實際 5.0 秒）。`native.py` 與 API 沒改，其他 Python 測項不重跑。
- 離線替身，不是 dev／stage 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="rtt-b-workbench"></a>
## 回覆還在寫就先看到文字（RTT B 階段 PR2，2026-10-07，CODE/offline，未部署）

目標同 [RTT A](#rtt-a-workbench) 引的 Neal 原話（10-06 23:3x）：送出後 3～8 秒看到 Agent 回覆的第一段文字。dev 第 17 版暖回合，
模型第一個 token 約 +7.4 秒，畫面第一段文字卻要 +12.1 秒，因為要等存檔、用量回報、推送 `turn.finished` 與讀回後才顯示；
長回覆差更多（stage 實例：模型寫了 58.6 秒，使用者全程看不到字）。PR1（#708，`1e0fa123`）已讓 Hosted 把文字寫進官方
Responses 串流、API 提供唯讀轉送（見 [Shared Context README](../README.md#rtt-b-reply-preview)）；本片是工作台這一端。

| 改了什麼 | 參考做法 | FA 現在 |
|---|---|---|
| 自己送出的 Responses 回合進行中，頁面只能等 `/status` 輪詢或推送，回合存檔後才看到第一段文字 | charter-agent 讀 Responses SSE，UI 收到 `text.delta` 就即時顯示（[app.js#L777-L918](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L777-L918)、[bridge.py#L1180-L1298](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L1180-L1298)）；官方背景串流斷線後用 `starting_after` 接續，後續 `response.in_progress` 代表整份重來（[Learn：stream-with-reconnect](https://learn.microsoft.com/azure/foundry/agents/how-to/stream-with-reconnect)，2026-10-07 查閱；Hosted 用的 `azure-ai-agentserver-responses==2.2.0` 原始碼核對見 [Shared Context README](../README.md#rtt-b-reply-preview)）。 | 頁面開 `GET /api/instructions/{RID}/stream`，bridge 原樣轉送 API 的唯讀轉送。頁面只把 phase=`commentary` 項目的 `response.output_text.delta` 接成暫時文字（Hosted 寫進去的工具行與回覆文字），下方標「回覆中，尚未確認保存。」；只改這一個泡泡，不重畫整段對話。 |

**怎麼結束、怎麼接續：**

- 收到 `final_answer` 項目（代表已存檔）或終局事件：停止串流，立刻讀一次 `/status`，保存的回覆取代預覽。
- 回合不再是「進行中的自己這一則」（完成、失敗、取消、不確定、換對話、登入失效）：同一次重畫就丟掉預覽，不把它留成結果。
- 串流斷線（包含 API 的 230 秒上限）：0.5／1／2 秒後帶 `starting_after=<最後的 sequence_number>` 重接，最多 3 次；第二個以後的
  `response.in_progress` 清空預覽（官方重置規則）。
- 頁面在 bridge 收到 submit 回條（`accepted`／`started`）之後才開串流。API 在回合開始前回 `NATIVE_RID_MISSING`（404）或
  `NATIVE_STREAM_NOT_STARTED`（409，dev 實測每次約 0.5～0.7 秒）：bridge 只把這兩種標成 `automatic_retry: true`，頁面依此每 0.4 秒再問，
  合計最多 25 次；問完時回合仍是 `accepted`
  （排在同一位 Agent 的其他回合後面），等 `/status` 顯示它 `started` 後再開一次。`NATIVE_STREAM_UNAVAILABLE`（例如超過官方 10 分鐘
  重播期）或其他錯誤：這一則不再開串流，照原本 `/status` 輪詢與 Web PubSub 推送。舊版 API 沒有這條路由時回 404，頁面同樣只試一次就停。
- 冷回合可能 30 秒以上沒有事件，API 每 15 秒送一次 `: keep-alive`；bridge 讀取逾時 90 秒、頁面不設逾時，所以不會當成斷線。

**不變（防資料損失、重送與明顯錯誤）：** 串流只讀，不送出、不重送、不改回條、不保存；`/status` 輪詢與推送照舊，畫面最後一律是
保存的回覆。bridge 不認得的 RID 回 403；不是 Responses 的回合回 `NATIVE_STREAM_UNAVAILABLE`；還沒讀到回條回
`NATIVE_STREAM_NOT_STARTED`。只接受一個純數字且最多 12 位的 `starting_after`，並核對路徑與 origin。bridge 只轉完整的行，
整行裡出現登入 token 就停（其他讀取是整份回應讀完一起查；這裡逐行查，token 被切在兩段之間也查得到）；單次轉送最長 240 秒。
串流和一般讀取共用 bridge 連 API 的連線池（實際 100 條，見 [RTT A 第二輪](#rtt-a-round-2)），每個看著的回合佔一條。

**dev 實測（PR1 部署後，2026-10-07，部署線工作階段 `2407d6bd` 報告 `rttb-pr1-dev-verify.md`，未入 repo；尚未含本片與 #709／#710）：**
從按下送出算起，API 收到第一個 delta：暖回合短回覆 7.33 秒、300 字 8.12 秒；現行畫面出現文字 9.84 秒、11.15 秒。預覽和存檔回覆
一字不差。可見文字比模型第一個 byte 晚 1.0～1.7 秒（模型先推理）。本片上線後，畫面第一段文字約在第一個 delta 到達 API 再加
bridge 轉送時間出現；要等 A＋B 一起部署後實測。只部署本片、沒有 PR1 的 API 時，頁面每則回合多一次被拒的 `/stream` 請求，其他照舊。

### Verified against（RTT B 工作台）

- 程式基準 `main` `7229bc54`（PR1 #708、A 階段 #706／#709／#710 都已合併）。已讀 `index.html` `runningOwnTurn`／`scheduleProgress`／
  `updateProgress`／`render` 的回覆內容判斷、`bridge.py` `Console.__init__`／`instruction_status`／lifespan、`native.py` `turn_status`、
  `adapter.py` `PullAdapter.send`、`responses.py`，以及 API 端 `behavior_native_stream_api.py` `stream_source`／`relay`／`forwarded`；
  dev 驗證報告（同上）第 1～5 步。
- Python：`python -m pytest tests/shared_context/test_web_test_console_*.py tests/shared_context/test_native_stream_api.py`（本機 venv）：
  608 PASS、4 FAIL、1 ERROR、2 SKIP。失敗的都和 [RTT A](#rtt-a-workbench) 記錄的既有失敗相同
  （`test_native_errors_remain_visible_and_do_not_switch_lane` ×3、`test_responses_progress_rereads_only_the_latest_or_unfinished_turns`；
  ERROR 需要 `M2_TEST_RUNTIME`）。`test_web_test_console_responses.py` 新增 13 案（9 案打 bridge 路由、4 案直接測 adapter）：只轉白名單
  欄位並把 `starting_after` 帶到平台；回條還沒 started 回 `NATIVE_STREAM_NOT_STARTED`（`automatic_retry: true`）、平台沒有可重播串流回
  `NATIVE_STREAM_UNAVAILABLE`（`false`），兩者都沒有 POST；5 種不合格的 cursor 在任何 backend 請求前就 403；不認得的 RID 403、
  還沒有回條的 RID 不打 API、請頁面稍後再問；登入 token 整段或被切在兩段之間都會停；API 的 `NATIVE_RID_MISSING` 標成可再問，
  不是字串的錯誤碼照 HTTP 狀態對照、不變成 500。
- DOM：`node --test tests/shared_context/test_web_test_console_workbench.cjs`：548 PASS。新增 7 案：commentary 文字邊到邊顯示並標
  「尚未確認保存」；第二個 `response.in_progress` 清空、其他項目不顯示；`final_answer` 立刻讀 `/status`、保存的回覆取代預覽、不再重開；
  回合結束（`completed` 沒帶回覆、`failed`）同一次重畫就丟掉預覽（拿掉 `render` 開頭的 `syncLiveText(false)` 時 `completed` 那案 FAIL）；
  排隊回合等滿 25 次後，回條變成 `started` 再開一次、只開一次（拿掉這段時 FAIL）；`NATIVE_RID_MISSING` 與 `NATIVE_STREAM_NOT_STARTED`
  都 0.4 秒後再問、斷線後帶最後的 sequence 重接、有收到事件的斷線不扣重接次數。原本「進行中只讀 `/status`」那案改成不把 `/stream`
  算進讀取，並核對 Responses 回合只開一次串流。
- 離線替身，不是 dev／stage 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="rtt-a-workbench"></a>
## 送出與讀回覆少掉三個來回（RTT A 階段，Neal 2026-10-06，CODE/offline，未部署）

Neal 原話（10-06 23:3x）：「在不違反以下原則的情況下，依照評估的結果，最佳化FA agent對話的round trip time。我在使用grokbot的體驗上，
幾乎是我按下"送出"鈕後，1秒內就看到Agent開始動作，3-8秒就會收到Agent回覆的第一個訊息。」
依據是 dev 第 17 版實測（A 暖回合中位數）：按下送出到「正在準備回覆」約 3.8 秒，到第一段文字約 12.1 秒。其中工作台 bridge
每次送出前讀 `agent-template/options` 約 0.9 秒；回合結束後依序讀回條、進度、回覆三次，約 1.9 秒。
本片只改工作台（bridge 與頁面）；API／Hosted 的送出與模型前準備由另一條 A 階段線處理，第一段文字串流屬 B 階段。

| 拿掉／改掉 | 參考做法 | FA 現在 | 預估省下（依 dev 第 17 版） |
|---|---|---|---|
| 路線已固定的對話，每次送出前仍讀 `GET /work-policy/v1/agent-template/options`，只為了在本機先確認 Responses 已啟用 | charter-agent 送出前不做版本或環境檢查，直接把訊息交給伺服器（[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）。API #680 起，Hosted 在建立 `rid:<RID>` 前拒絕（例如 Responses 沒開）時會標 `not_dispatched`，能不能送由 submit 自己回答。 | 第一則訊息照舊讀範本（Responses 時加讀 options）決定路線並先擋未啟用的 Responses；路線固定後，送出直接 `POST submit`。伺服器的 `not_dispatched` 拒絕記成「沒有送出」的終態，換版照舊只在 `server_following` 時同 RID 重試。支援明細的平台歷史第一次展開時才讀 options。 | 每則續問約 0.9 秒：「正在準備回覆」與第一段文字都提早 |
| 回合結束後（推送或輪詢）依序讀回條、進度、回覆三次；回合進行中每次輪詢讀回條加進度兩次 | charter-agent 的 `turn.complete` 帶最終文字（[bridge.py#L1051-L1088](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L1051-L1088)）；API #664 已有一次讀回條、公開進度與單頁回覆的 `/status`（見 [Shared README](../README.md)）。 | `turn_status` 先讀一次 `GET .../conversations/{CID}/requests/{RID}/status`；`/status` 帶來的進度照 `/progress` 同樣的核對才採用；單頁回覆在 bridge 再核一次長度與 hash 才顯示，多頁回覆照原本逐頁讀。`/status` 失敗（例如還沒有這條讀取的舊映像，或它讀不出進度）時退回原本的分開讀取；登入失效照舊直接回報、不退回。 | 單頁回覆約 1.1～1.3 秒（三次約 1.9 秒 → 一次約 0.6～0.8 秒）；進行中每次輪詢約 0.5 秒（少一次約 0.46 秒的進度讀取） |
| 「正在傳送你的訊息…」「正在準備回覆…」是靜態灰字 | charter-agent 送出時先畫 user bubble，再加帶 `thinking` 樣式的 agent bubble 並設 busy，之後才呼叫 bridge（[app.js#L756-L767](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L756-L767)）。 | 你的訊息與「正在傳送你的訊息…」本來就在按下後同步畫出（dev 第 17 版 5～9 毫秒）。現在仍在進行的等待字樣（傳送中、準備回覆、載入回覆、換版後自動送出）都多一個閃動小點與 `aria-busy`；結果不確定、沒有送出等已停下的狀態不閃。系統設定「減少動態」時不閃。伺服器回「已接單」前，文字仍是「正在傳送你的訊息…」（[第二輪](#rtt-a-round-2)起改為「助理準備中…」）。 | 不改時間，只讓每個等待狀態看得出還在動 |

合計估算：暖回合按下送出到「正在準備回覆」約 3.8 → 2.9 秒，到第一段文字約 12.1 → 10.0 秒；要等 dev 部署後實測。

**不變（防資料損失、重送與明顯錯誤）：** 同 RID 不重送（本機已有 submit 紀錄就拒絕）；UNKNOWN 只核對、不重送；回條核對（原命令、
capture、digest、session、路線）；進度核對（Agent、CID、RID、session、digest、序號不倒退）；回覆核對（result reference、長度、hash）；
批准卡與提問卡的讀取時機。沒有新增 API、權限或保存格式；`/status` 是 #664 已部署的唯讀路由（dev 第 16 版、stage 第 17 版都已包含）。

**還沒處理的情況：** 新對話第一則訊息仍串行讀範本與 options（約 1 秒），因為路線要先選對，否則對話會被固定在錯的路線；兩個讀取彼此
獨立，之後可改成同時讀（約省 0.5 秒）。被拿掉的 options 讀取，10-05 時是「在寫入前擋下已停用 Responses」的最小機制（見[打開頁面少讀](#lighter-page-load)）；
API #680 之後，Hosted 在建立 `rid:<RID>` 前的拒絕帶 `not_dispatched`，工作台記成「沒有送出」，這個保護改由伺服器的回答提供。
仍不帶 `not_dispatched` 的只剩三種：`platform_client` 的 503（`RESPONSES_CONVERSATION_MODE` 是程式常數，部署版本固定為
`"conversation"`，只有測試會改成 `None`，部署版本走不到）；API 本身沒開 native（`FA_NATIVE_TURNS` 未設，`_context` 回 503）；以及
Agent 被換到完全沒開 native 的映像時，API 讀 session 就被拒。後兩種是部署設定錯誤，工作台記成「傳送結果尚未確認」、不重送、
不鎖住對話；要顯示成「沒有送出」，需由 API 線在這些送出前的拒絕補 `not_dispatched`。

### Verified against（RTT A 工作台）

- 程式基準 `main` `068fe017`。已讀 `native.py` `refresh_routing`／`submit`／`turn_status`／`read_progress`／`_answer`／`read_platform_history`、
  `bridge.py` `submit_instruction`／`instruction_status`、`index.html` `sendChat`／`renderConversation`；API 端
  `behavior_native_turn_api.py` `submit`／`read_status`／`_invoke`、`native_turns.py` `_admission_checks`／`_submit_locked`／`turn_status`、
  `native_progress.py` `read_progress`、`behavior_native_responses_api.py` `template_responses_options`／`platform_client`／
  `submit_native_turn`、`behavior_agent_versions.py` `current_hosted_version`；量測依據為 v17 量測線（工作階段 `2407d6bd`，
  未入 repo）時間拆解報告第 2.1 節，以及送出路徑盤點（工作階段 `703c18c9`）。
- Python：`python -m pytest tests/shared_context/test_web_test_console_*.py`（本機 venv，`TIKTOKEN_CACHE_DIR` 指向既有快取）：
  579 PASS、4 FAIL、1 ERROR、2 SKIP。4 個 FAIL 在 `main` `068fe017` 改前同樣失敗（`test_native_errors_remain_visible_and_do_not_switch_lane` ×3、
  `test_responses_progress_rereads_only_the_latest_or_unfinished_turns`）；ERROR 是需要預先安裝 SDK runtime（`M2_TEST_RUNTIME`）的環境前置。
  新增 8 案：路線固定的 Responses 續問第一個 backend 請求就是 submit、不讀範本或 options；伺服器 `not_dispatched` 拒絕記成終態、
  不重送；單頁回覆一次 `/status`；多頁回覆 `/status` 後逐頁讀；`/status` 失敗時退回分開讀取；`/status` 帶的回覆和回條不符就不顯示；
  `/status` 登入失效直接回報、不退回；`/status` 帶的進度不是這個 session 的就不採用。8 案都在 `main` 的 `native.py` 上 FAIL
  （拒絕那案讓 options 也說 Responses 未啟用，`main` 會在本機先擋下、不送 submit）。原本 monkeypatch `read_progress` 的 4 案改成接受新的關鍵字參數。
- DOM：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：535 PASS。新增 1 案（按下送出同步畫出
  訊息與閃動等待泡泡、接單後「正在準備回覆…」也閃、已停下的狀態不閃、減少動態時不閃），在 `main` 的 `index.html` 上 FAIL。
- 本機替身量測（暫時測試檔，未提交；每個 bridge→API 請求加 0.3 秒延遲）：路線固定的 Responses 續問送出，改前 2 次請求 0.685 秒
  → 改後 1 次 0.400 秒；完成的兩頁回覆讀取，改前 4 次 1.291 秒 → 改後 3 次 0.992 秒；單頁回覆 3 次 → 1 次（由上述測試鎖定）。
- 離線替身，不是 dev／stage 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="plain-main-screen"></a>
## 主畫面只說白話，工程證據收進「⋯ → 支援明細」（Neal 2026-10-06，CODE/offline，未部署）

Neal 原話：「FA portal為什麼要控制user能否傳送訊息、能否續問? 有遵守我們的原則嗎」（10-06 09:58）、
「FA portal還有那些部分是沒有遵守的?」（10:00）；長期原則：「參考Grokbot的實作方式。儘可能地使用Foundry Hosted Agent的能力。……
請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則」。本片處理原則審查（`origin/main` `36c71f1a`）的 P01、P04、P08、P10、P12；
功能不減少，只是收起來或改白話。原「技術明細」側欄改名為**支援明細**，預設關閉。

| 拿掉／改掉 | 參考做法 | FA 現在 |
|---|---|---|
| P01 頁首狀態寫「同一個 session，接著聊」「已接單，等待執行」「目前不能接續」等 | Grok Bot 側欄只分「需要處理／新動態／工作中」（[Settings and notifications](https://docs.x.ai/grok-bot/settings-and-notifications)，2026-10-06 查閱） | 頁首只顯示**可以聊天／需要處理／發生問題**；原因照舊在輸入框下方一行。 |
| P01 錯誤在頁首下方，`詳情`直接展開原始 JSON | Grok Bot：「Errors appear above the composer under Notifications … Some notices include Copy request ID for support」（同上） | 錯誤移到輸入框正上方，只顯示 `friendlyError` 的第一句（有「請…」的下一步時一起保留，去掉「不會…」的保證句）；有 request ID（送出失敗的那則訊息、這則訊息自己的錯誤，或在這則訊息自己的路由上失敗的動作）時可「複製 request ID」；「詳情」打開支援明細的「最近的錯誤」，內有完整說明、request ID 與原始錯誤。 |
| P01 回覆泡泡下的 SHA256、原回條、UNKNOWN、接單、「不會自動重送」、委派編號、接續用量 | charter-agent 錯誤泛化成一句話，不把證據層級當主功能（[研究](../../../docs/charter-agent-desktop-client-research.md)） | 泡泡下只寫「這則訊息沒有送達」「還不確定停止請求有沒有送到」「附檔：notes.txt」這類白話；SHA、回條欄位、原因代碼、委派編號、交接紀錄的工作 ID、用量全部放在支援明細「識別碼、歷史與回條」的 `message` 區；交接紀錄只寫誰交給誰、狀態與原因，按鈕是「打開某助理的原對話」。 |
| P01 側欄助理說明「已核對服務接點；不代表 Hosted 已驗收」、附件候選「候選不是讀取權限…」與每個候選的 SHA | 同上 | 側欄改回「工作助理」；附件候選只寫名稱與第幾版，SHA 只在滑鼠停留時顯示；後端仍照舊核對來源與權限。 |
| P04 Responses 對話在聊天區常駐「平台對話歷史」，每次整段讀進度只要回條有變就順帶讀第一頁 | Foundry Responses 以 `conversation`／`previous_response_id` 管歷史，charter 只渲染一般聊天紀錄（同上研究） | 面板搬進支援明細，**第一次展開**或按「更新平台歷史」才讀；bridge 的 `_refresh_responses` 不再自動讀。 |
| P08 處理過程寫「第 N 秒 ·」「工具關聯 #N」「SDK turn」、推理摘要可用性、「最新步驟讀寫未確認」「已知至少」 | Grok Bot transcript 顯示 tool activity，不是 SDK trace（[Chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)） | 摘要只寫「正在讀取／正在使用桌面／正在使用工具／正在等你批准／正在等你回答／正在準備／正在回覆／已完成／沒有完成／已停止」＋工具數與秒數；步驟只寫工具名稱與完成與否，推理摘要內容改稱「思考摘要」。編號、時間戳、原始摘要、可用性、截斷／未確認、原因代碼都在支援明細的「處理過程」。讀取方式不變（#664／#666 處理）。 |
| P10 ⋯ 選單第一層有「助理模板：下載／比對／選擇」「查看系統提示」；設定頁技能寫版本編號、內建能力帶 `fa-…-v1`；技能審核卡帶內部識別碼；技能草稿原文內嵌在對話裡 | Grok Bot 用 Marketplace／private skills，沒有 system prompt viewer（同上設定頁） | 兩個捷徑拿掉，功能留在支援明細（展開「系統提示」自動讀一次）。設定頁「已安裝」顯示**角色**（一般助理，或有 Team 核准的角色說明時會套用的角色版本）與使用中的技能／能力的白話名稱；技能審核照舊（查看內容、批准、拒絕、取代、停用），卡片拿掉內部識別碼。「查看技能草稿」改在支援明細顯示原文與下載。 |
| P12 目錄列出永遠不會被呼叫的 `fa-stage-c`／`fa-stage-d`，顯示「服務尚未就緒」 | Grok Bot 側欄只列使用者可操作的 Bots | `AgentConsoles.view` 不列 server 標為 `unavailable` 的 target（用 server 狀態，不寫死名稱）；預設助理與建立中的助理例外。被隱藏的只出現在支援明細「目錄中未列出的助理」，仍不能被選用。 |

**不變（防資料損失、重送與明顯錯誤的動作都還在）：** once-intent journal、原 RID、UNKNOWN 不重送；核對原接單、以完整原請求重試、以原內容重送、
核對原停止回條、核對追加、確認收到，全部照原入口可按（有的在支援明細；續接原取件與操作批准卡已由 P05 拿掉）。
模型選單照 Neal 10:24「保留」決定留在輸入框旁，只把容量文字收進支援明細的「Session 與模型容量」。
P02 模型選單、P07 批准卡、P09 Team MCP 等 Neal 決定；P03／P05／P06 改由伺服器做；P11 每則回覆的按鈕由響應式版面線處理；本片都不碰。
沒有新增 API、權限或保存格式；`/api/agents` 多一個 `hidden_agents` 欄位只給支援明細看。

### Verified against（介面去工程化）

- 程式基準 `main` `c32006fc`（`index.html` 與審查基準 `36c71f1a` 相同）；2026-10-06 合併 `main` `61ebab6c`（含 #663、#666、#674、#679、#686、#687、#689 與官方 toolbox 批准卡）後重驗：被拿掉的環境面板、固定工作與 operation 卡沒有帶回，`queued_relay` 的交辦狀態改成白話、代碼放進支援明細；再合併 `main` `3ce6d081`（含 #675 響應式版面）：`#error` 只用輸入框上方這一處的樣式（拿掉 #675 窄視窗的舊上方邊距），停止回條的兩句保留「這則訊息下方「⋯」裡的「核對原停止回條」」。已讀 `index.html` `render`／`renderConversation`／
  `renderComposer`／`processingPanel`／`processingLine`／`renderProcessing`／`renderPlatformHistory`／`readPlatformHistory`／
  `readNativeSkillDraft`／`renderSettings`／`settingsItems`／`renderSkillReviews`／`renderAttachmentPicker`／`renderAgents`／
  `cancellationMessage`／`delegationText`／`friendlyError`，`native.py` `_refresh_responses`／`read_platform_history`，
  `agent_connections.py` `AgentConsoles.view`，`deploy/stage/stage.json` 的 C／D target。
- DOM（`node --test tests/shared_context/test_web_test_console_workbench.cjs`）：625 PASS；合併 `main` `61ebab6c` 後 528 PASS、再合併 `3ce6d081`（#675）後 534 PASS（`main` 拿掉環境與固定工作的測試；#674、#679、#687 幾案改成新字樣，例如「正在準備」「交辦沒有送達」「可以修改後重新送出」）。既有約 80 案改成新的白話斷言，原本檢查
  工程字串的斷言改到支援明細（`identifiers`、`processing-events`、`processing-detail`、`error-full`、`skill-draft-body`）去找；
  新增 9 案：主畫面沒有 SHA／RID／UNKNOWN／回條／工具關聯／SDK／「不會自動重送」等字，而支援明細找得到且「以完整原請求重試」只送一次；
  錯誤一句話（保留「請…」那一步）＋複製 request ID，且只在失敗的是這則訊息自己的路由時才附 request ID；平台歷史在支援明細、展開才讀一次；
  隱藏的助理只在支援明細，若仍是目前選中的助理則保留名稱與原因；登入前、指令不完整或沒有模型時頁首是「需要處理」；
  支援明細裡收起的技能草稿不會被下一次更新重新打開。
- 兩軸 review（Standards／Spec，附 Neal 原話）：Standards 1 個 P2（多助理登入前頁首誤報「發生問題」）已修；兩軸其餘 P3 中，
  頁首漏判、錯誤句掉了下一步、request ID 誤附、被隱藏的選中助理、角色措辭、停止狀態用詞、重複程式與草稿面板重建已修。
  未改：輸入框下方的封鎖說明（`followUpMessage`，輸入框常開線正在改）、環境面板文字（P06）、每則回覆按鈕（P11）。
- 第二輪兩軸 review（rebase 到 `3ce6d081` 之後）：0 個 P1／P2；P3 已修：rebase 時切掉的 #675 驗證句補回、「核對追加」提示寫出按鈕在「⋯」裡、錯誤一句話改成保留第一個含「請」的下一步（例如遠端重啟時「請先複製內容，再重新整理頁面」）、交接紀錄面板改白話並把工作 ID 移到支援明細。
- Python（`test_web_test_console_{agents,responses,frozen_interface,agent_onboarding,agent_policy,native_scope,remote}.py`）：
  114 PASS；合併 `main` `61ebab6c` 後連同 `test_web_test_console_realtime.py` 共 137 PASS（#675 沒有改 Python）。目錄測試改成 `agent-four`（server unavailable）不列出、只在 `hidden_agents`、仍回 `AGENT_NOT_AVAILABLE`，
  預設助理不可用時仍列出並停用；Responses 3 案改成整段讀進度時 `platform_history.page` 為 `null`、明確 GET 才讀。
- 真瀏覽器（Playwright Chromium、loopback 替身伺服器、DOM 測試的合成狀態）看過修改前後主畫面與支援明細。這不是 Hosted 或 Azure 證據。
- 離線替身，不是 dev／stage 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="responsive-layout"></a>
## 版面跟著視窗縮放（Neal 2026-10-06，CODE/offline，未部署）

Neal 原話：「UI的部分，目前不符合response design的設計，縮小視窗時，網頁layout沒有跟著縮小，很多功能和資訊會被改調」。
總協調在 stage 第 14 版量到：視窗 960×560 時對話區只剩約 160px、700×500 時約 110px；輸入框下方的狀態文字和展開的「環境詳細」
占掉 150～200px；側欄固定 260px，960×560 只看得到 2.5 位助理；每則回覆下 6 個文字按鈕，390px 寬時「詳細」被拆成兩行。
只改版面：CSS、HTML 外框，以及純顯示的 JS（回覆動作的「⋯」選單、它的收起與重繪後保持打開、輸入框高度上下限、
側欄拖曳寬度、桌面框比例）；不改讀取、送出、換版判斷、重送、UNKNOWN 核對或 VNC 連線的邏輯，原有 id 不變。
改的文字只有「核對追加」與停止回條未確認的提示，多寫了按鈕現在的位置。

| 項目 | 現在 |
|---|---|
| 整頁 | `.app-shell` 用 `100dvh` grid；標題列、輸入區依內容高度，只有對話區（`.chat-scroll`，`flex:1 1 auto;min-height:0`）捲動。標題列從固定 68px 改成依內容（至少 56px）。「對話訊息」量到的 649px 是捲動區裡的內容高度，不是固定高度。 |
| 輸入框下方 | 停止回條提示和說明列放進 `.composer-status`，沒事時都隱藏（說明列空白時也不佔位）；整區有高度上限（一般 `clamp(3.6em,16dvh,14em)`、矮視窗 `max(3em,10dvh)`），超過時在區內捲動，**一行都沒拿掉**。待核對的停止回條提示排在說明前面。換版面板與換版完成提示已由「換版由伺服器處理」（#687）拿掉。 |
| 側欄 | 預設寬度 `clamp(220px,22vw,280px)`；900px 以下改成標題列漢堡鍵開關的抽屜（原本 720px），抽屜不顯示拖曳把手。間距、助理列與底部收緊，助理清單佔滿剩下的高度並自己捲動。 |
| 側欄寬度可拖曳（Neal 2026-10-06 12:39） | 原話：「可否讓左邊的panel可以往右或往左縮放大小，縮放的過程中，即時桌面也需要等比例的縮放」。側欄右緣有把手（`#sidebar-resizer`）：用 Pointer Events 拖曳（滑鼠、觸控、筆同一套），範圍 220px～`min(50vw,640px)`；把手可 focus，左右鍵每次 16px、Home／End 到最小／最大，雙擊恢復預設；觸控裝置（`pointer:coarse`）另把可點範圍加寬到 24px。依 [WAI-ARIA APG 視窗分隔線](https://www.w3.org/WAI/ARIA/apg/patterns/windowsplitter/)（2026-10-06 查閱）用 `role="separator"`、`aria-orientation="vertical"`、`aria-valuenow／min／max`、`aria-controls="sidebar"`，名稱與側欄同為「Agent 清單」；APG 的 Enter 收合不在需求內，沒有做。寬度只存在這個瀏覽器的 `localStorage`（`fa-sidebar-width`，只在拖曳結束或按鍵時寫），不是工作狀態，也不送伺服器；視窗變窄時由 CSS 再限制，原本選的寬度保留給寬視窗。 |
| 即時桌面等比縮放 | `#desktop-screen` 寬度跟著側欄、高度用 `aspect-ratio` 依 VM 畫面比例計算（連上前用 Hosted 桌面的 1280×800，`behavior_desktop.py`），不變形、不裁切。比例取自 noVNC 的畫面 canvas（`scaleViewport` 時它的 `width`／`height` 等於遠端解析度），以 `MutationObserver` 跟著更新；比例設在 `#desktop-panel` 上，未連線時的灰框也沿用最後一次的比例。縮放由 noVNC 自己的 `ResizeObserver` 在下一個 animation frame 重算（`novnc/core/rfb.js` `_handleResize`），所以拖曳中每一幀都跟著變。只改顯示縮放：沒有開 `resizeSession`，不改 VM 解析度，連線、操作與「中斷／全螢幕」都不變。 |
| 每則回覆的動作 | 「詳細」和「以原內容重送」留在列上；下載答案文字、查看截圖、下載原檔、查看交接紀錄、查看技能草稿、核對原停止回條、核對追加都收進該則的「⋯」（原則審查 P11：核對類保留、按得到，但不常駐）。有待核對時（停止回條 UNKNOWN、追加未確認或讀回失敗），「⋯」旁有紅點，`aria-label` 為「這則的更多動作（有待核對）」；「核對追加」與停止回條未確認的提示都寫出按鈕在這則訊息下方的「⋯」裡。選單和右上「⋯」一樣：點外面、選了動作或按 Esc 就收起，Esc 後焦點回到該則的「⋯」；展開時捲進可見範圍。回合進行中畫面每秒重建，打開的選單會保持打開（不捲動），所以進行中也按得到。「複製」原本就在泡泡右下角。 |
| 面板與選單 | 右上選單、⚙ 選單限制在視窗內並內部捲動；支援明細（含系統提示、助理模板）、設定頁（含批准規則）本來就貼齊視窗並內部捲動；即時桌面見上一列；批准卡、skill 審核的內容框上限改成 `min(原值,50dvh)`；截圖寬度不超過泡泡。 |
| 模型選單 | Neal 2026-10-06 10:24 決定照原樣保留：任何尺寸都在輸入框旁、看得到也能切換；420px 以下只把寬度上限縮成 38%。 |
| 矮視窗（高 640px 以下） | 標題和狀態同一行、輸入框從一行起跳（`resizeComposer` 的上下限改讀 CSS `min-height`／`max-height`，一般視窗仍是 52px 起）、間距收緊、不顯示快捷鍵提示。 |

驗收依 [WCAG 2.1 SC 1.4.10 Reflow](https://www.w3.org/WAI/WCAG21/Understanding/reflow.html)（2026-10-06 查閱）：「Content can be presented without
loss of information or functionality, and without requiring scrolling in two dimensions for: Vertical scrolling content at a width
equivalent to 320 CSS pixels」；表格與程式碼區塊屬原文例外的二維內容，維持各自的水平捲動。另一條是總協調訂的：高約 480px 時對話區至少占一半。

### Verified against（響應式版面）

- 程式基準：開發時 `main` `2559fd2b`，合併前併入 `main` `61ebab6c`（含 #663、#666、#674、#679、#686、#687、#689；衝突只在輸入區外框與重繪時的清除，兩邊都保留）。
  已讀 `index.html` 的 `<style>`、輸入區 HTML、`renderComposer`（只看哪些行何時顯示；開發時另讀過已由 #687 拿掉的 `renderEnvironment`）、
  `renderConversation` 的 `turn-tools`、document 的 click capture 與 Esc 處理、`resizeComposer`；原則審查 P11（總協調工作階段的審查檔，未進 Git）。
- DOM（`node --test tests/shared_context/test_web_test_console_workbench.cjs`）：併入 `61ebab6c` 後 525 PASS（含下方側欄 2 案；main 拿掉功能時一併刪了測試）。
  新增 4 案：版面高度規則（沒有固定高度、輸入區上限、900px 抽屜、選單與面板在視窗內）、狀態區沒事時全部隱藏而 UNKNOWN 停止時看得到、回覆「⋯」選單裡的
  動作照常只讀一次、點外面／Esc／選了就收、重繪後保持打開、核對類在「⋯」裡且有待核對時有標示、模型選單不被藏起；4 案在 `main`（`2559fd2b` 與 `61ebab6c`）的 `index.html` 上都 FAIL。
  既有 11 處直接在 `turn-tools` 找按鈕的測試改用 `turnActions(turn)`（含「⋯」選單裡的按鈕），斷言不變；側欄斷點斷言 720→900。
- 真瀏覽器（Playwright Chromium、loopback 離線替身、合成狀態；8765 被另一個工作階段的工作台占用，改用 8775／8774 並排比較修改前後）：
  1440×900、1024×768、820×1000、390×844、960×560、700×500、320×640、700×480、320×480（另加 1280×600）都沒有水平捲動，送出鍵、輸入框與模型選單
  都在畫面內，模型選單可切換。對話區占視窗高度：沒事時 60～79%（修改前 43～76%）；有停止回條未確認與續問提示時 52～77%（併入 `61ebab6c` 後重量；
  修改前在 stage 第 14 版還有換版面板，為 14～66%）。強制重繪時打開的「⋯」保持打開、捲動位置不變。
  960×560 時側欄看得到 4 位完整的助理（修改前 3 位）。
- 側欄拖曳與即時桌面：另讀 `index.html` 的 `connectDesktop`／`disconnectDesktop`／`renderDesktop`，`novnc/core/rfb.js` 的建構子（`_screen`、
  `ResizeObserver`）、`_handleResize`、`_updateScale`、`_requestRemoteResize`（只在 `resizeSession` 時改遠端解析度），`novnc/core/display.js` 的
  `resize`／`viewportChangeSize`／`autoscale`（畫面 canvas 的 `width`／`height` 等於遠端解析度），`novnc/core/util/cursor.js` `attach`
  （游標 canvas 掛在 `body`，不在桌面框裡），以及 `shared_context/behavior_desktop.py:45`（Hosted 桌面 1280×800）。
  同一離線替身另加只送一張 1280×800 合成畫面的假 VNC（RFB 3.8、無密碼，只在 session 檔案夾，不進 Git）：
  1440×900 下一次拖曳中側欄 280→370→460→580px，noVNC 畫面同步 259×162→349×218→439×275→559×350，比例一直是 1.6；
  窄（220px）、預設（280px）、寬（560px）三種寬度的畫面都正圓、四角記號都在。觸控 pointer、左右鍵、Home／End、雙擊恢復、
  重新整理後保留、1024px 視窗時限制在 512px、820px 抽屜不顯示把手都看過。DOM 另加 2 案（拖曳／按鍵／雙擊／重新整理後保留，
  以及桌面框比例跟著 canvas 且不隨側欄改變），在加入前的 `index.html` 與 `main` `61ebab6c` 上都 FAIL；併入後重測拖曳、等比縮放與重新整理後保留，結果相同。截圖與量測放在 PR，不進 Git。這不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="remove-normal-pull"></a>
## 拿掉 legacy 固定工作（normal_pull）與預算／期限 gate（P05，第 16 版，CODE/offline，未部署、未合併）

協調決定（2026-10-06 12:43）：
> P05 選 A：工作台整條拿掉 legacy `normal_pull`，包括固定工作、預算／期限面板和 preflight。依據是 Neal 的原話「把非必要的邏輯拿掉」，而且這條路徑只在診斷區、ADR-0006／0007 之後已經沒有正式回合在用。伺服器端的 `/work-policy/v1/*` 先不動。請一併拿掉對應的測試和 README 段落，PR 寫明哪些伺服器 route 變成沒人呼叫，之後另行清理。這屬於 v16，合併順序排在 #679 之後。

| 拿掉的邏輯 | 參考做法 | 伺服器怎麼擋 | FA 現在 |
|---|---|---|---|
| 設定檔 `mode=normal_pull` 固定工作在 `/api/state` 顯示，並提供交辦／狀態／取件／ACK／續接按鈕 | charter-agent 的 bridge 只送使用者訊息，沒有固定工作、預算 gate 或診斷式 pull 控制（[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）。 | 工作台入口先回 403 `ACTION_NOT_ALLOWED`，不讓請求到 backend；真正 instruction/native 仍由 server admission 與 RID 去重擋重送。 | 舊設定仍可解析與啟動，但 `normal_pull` task 不列出；直接打 `/api/tasks/<normal_pull>/...` 回 403，不打後端。 |
| 工作台自己的 `work_policy_reader`、`blocking_code`、預算／期限面板，以及 normal submit 前的 preflight gate | Grok Bot 的花費在 Settings/Billing 管理，不逐訊息用聊天 UI 擋送出（[Grok Bot settings](https://docs.x.ai/grok-bot/settings-and-notifications)）；ADR-0006 把用量入帳放在伺服器／回合結束。 | `/work-policy/v1/submit` 仍在 server admission 生效點核對權限、順序與帳務；工作台不再加第二個 normal fixed-work gate。 | 工作台不再用預算、期限或本機 policy observation 決定能否送固定工作。job-bound instruction lane 仍使用伺服器 preflight 來保留原 RID／conversation 契約；這不是 legacy 固定工作 gate。 |
| 原取件 `pull-reconcile`／`pull-continue` sidecar 流程 | charter-agent 沒有診斷式 pull continuation；Grok Bot 讓 Bot 對話自己持續呈現狀態，不要求人手續接取件（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)）。 | — | CLI 仍接受 `--pull-continuation` 與 hash，sidecar schema 可解析，方便退版；bridge 忽略它、不提供按鈕、不 POST 續接 route。 |
| normal_pull 專用原操作讀取／拒絕／批准卡 | Grok Bot 的 approval/attention 顯示在正式 bot 對話與通知設定，不掛在固定工作診斷區（[settings and notifications](https://docs.x.ai/grok-bot/settings-and-notifications)）。 | 正式 CLI 仍可呼叫 operation routes；工作台 normal task route 不再暴露這些控制。native HITL／Team attention 仍各自由 server 核對原 pending id 與權限。 | `/api/tasks/<id>/operation*` 本機 route 與 `operation_review.py` 移除；native HITL 與 Team approval UI 不受影響。 |

**伺服器 route 變成沒人呼叫（之後另行清理）**
- legacy 固定工作 submit：`POST /behavior/v1/bindings/{binding_id}/assistant/submit` 與 lifecycle `POST /behavior/v1/bindings/{binding_id}/work/submit`。`adapter.py` 仍保留 request helper 給舊型別相容，但 bridge 不再讓 `normal_pull` task 到達它。
- legacy 原取件續接：`POST /behavior/v1/bindings/{binding_id}/delivery/pull-reconcile`、`POST /behavior/v1/bindings/{binding_id}/delivery/pull-continue`。

**仍有人呼叫，不列入清理**
- `/work-policy/v1/preflight`、`/work-policy/v1/submit`、`/work-policy/v1/submit-reconcile`：job-bound instruction lane 使用。
- `/behavior/v1/bindings/{binding_id}/delivery/pull` 與 `/delivery/acknowledge`：instruction delivery 與 `original_failure` 仍使用。
- `GET /behavior/v1/bindings/{binding_id}/operation`、`GET /operation/decision-state`、`POST /operation/approve`、`POST /operation/reject`、`POST /operation/approval-reconcile`、`POST /operation/rejection-reconcile`：`shared_context.behavior_operation_cli` 的 show/state/approve/reject/reconcile 仍使用（reconcile 走 `_RECONCILE_PATHS`），不能列為沒人呼叫。

**只為相容保留**
- `ConsoleConfig`／`SteadyConsoleConfig`／`StandingConsoleConfig` 仍接受 `mode=normal_pull` tasks；舊 stage `console.json`、remote bundle、onboarding 產物和舊 SQLite rows 不會讓容器啟動失敗。
- `AgentOnboarding` 仍用原 config 裡的 normal task 當隱藏 seed 來 derive 新 Agent 的 config，因為 config schema 仍要求至少一個 task；新 Agent 的工作台同樣不列出或執行它。
- `pull_continuation.PullContinuationSidecar` 與 `serve --pull-continuation/--pull-continuation-sha256` 保留為啟動相容；沒有 runtime action。

### Verified against（P05）

- **程式基準**：`neallee2012-workbench-remove-normal-pull` `a4030381`（PR #689）上一輪 review 後修正（`665306fa`）；#679 合併後 base 改為 `main`，2026-10-06 合併 `main` `33afb53e`（含 #679、#680、#687、#686）後重驗。
- **已讀**：`bridge.py` task/filter/status flow、`freeform.py` instruction preflight、`index.html` diagnostics action loop、`native.py` task action projection、`__main__.py` CLI sidecar parsing、`agent_onboarding.py` hidden task seed、`behavior_operation_cli.py` operation route callers、相關測試與 README 錨點。
- RED：新增 Python 接受測試在 `26c239da` 上 FAIL：舊 normal config 仍載入但 `/api/state` 不列 task、normal task action 403 且不打後端、舊 normal journal row 不污染 state。新增 DOM 測試在舊 `index.html` 上 FAIL：頁面仍含預算／期限與固定工作控制文案。
- GREEN：`test_web_test_console.py` 6 PASS、`test_memory_preflight.py`（含 instruction preflight 容錯）PASS、`test_web_test_console_steady.py` 3 PASS、`test_web_test_console_remove_normal_pull.py` 3 PASS；`test_web_test_console_workbench.cjs` 619 PASS。
- RED checks：`test_instruction_status_keeps_reading_job_when_preflight_is_temporarily_unavailable` 在 `a4030381` 的 `bridge.py` 上 FAIL；`test_pull_continuation_arguments_remain_startup_compatible_without_actions` 在 `a4030381` 的 `bridge.py` 上 FAIL。
- 全工作台 Python 重跑：573 PASS／2 SKIP；已知既有失敗仍是 `test_native_errors_remain_visible_and_do_not_switch_lane`×3、`test_metadata_preview_can_confirm_without_guessing_source_or_sending_the_draft`、`test_console_append_reaches_real_n1_and_fixed_sdk_without_a_new_turn`（本機缺 pinned runtime）。
- `grep -l "web_test_console" tests/shared_context/*.py` 對應 65 檔也已跑；1044 PASS／2 SKIP，另有既有或環境相關失敗（大量 fixed SDK runtime 缺件、native public progress/prompt prefix 類 runtime 前提、`test_fresh_api_only_cold_import` 子行程 PYTHONPATH 前提、以及與本 PR 無關的 native/memory no-SDK 既有差異）。
- **合併 main 後**（`4ddb2f22`）：DOM 517 PASS（main 532 減去本 PR 拿掉的固定工作案）；全部 `test_web_test_console*.py`＋`test_memory_preflight.py`（在 `b41bcdcd`）567 PASS／2 SKIP／3 known FAIL／1 ERROR（缺本機 runtime）。合併前 review 後再強化 `test_web_test_console_steady.py` 的 token 測試（299 秒不刷新、不打後端、token 不進 state／log／stdout／SQLite），並補回「過期後只換一次、不送出任何東西」：6 PASS。
- 未部署、未改 `deploy/stage/release.json`，伺服器 `/work-policy/v1/*` 未動。

<a id="read-retry"></a>
## 讀不到進度時自動重試，不卡在錯誤畫面（stage v15 現場，第 16 版，CODE/offline，未部署、未合併）

stage v15 12:40（總協調轉述）：讀進度時平台的 `get_session` 卡滿 10 秒逾時，API 回 503 `NATIVE_BOOTSTRAP_UNKNOWN`（`automatic_retry:false`），工作台就停止輪詢、畫面卡在錯誤上；其實回合已完成，之後的讀取也都成功。
API 線（#688，已合併為 `3260086c`）讓讀取路徑有限重試；GET 讀取路由的暫時性 5xx 帶 `automatic_retry: true`（內部重試用完仍帶，Table 暫時讀不到是 503 `BEHAVIOR_READ_FAILED`），再讀也不會好的 5xx（503 `NATIVE_TURNS_DISABLED`、平台 session 已結束的 503 `NATIVE_BOOTSTRAP_UNKNOWN`）帶 `false`，4xx 一律不帶。這裡是工作台的配合。

| 改掉的邏輯 | 參考做法 | FA 現在 |
|---|---|---|
| 進度／狀態讀取遇到非白名單的錯誤（例如 503 `NATIVE_BOOTSTRAP_UNKNOWN`）就停止輪詢，畫面卡在錯誤 | charter-agent 讀取失敗時不讓畫面停在終態錯誤，只有 `turn.error` 才是錯誤（[app.js#L777-L918](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L777-L918)）；讀取是純讀，重讀不會重送任何工作。 | 只讀取的請求（`/status`、整段進度、`/reply`）遇到 `automatic_retry: true`、bridge 的 `BACKEND_OFFLINE`／`BACKEND_UNAVAILABLE`／`BACKEND_LIMIT`（上游沒有錯誤碼的 5xx 也是 `BACKEND_UNAVAILABLE`）或網路錯誤時，照平常每秒一次的節奏放慢成 2→4→8…最多 30 秒繼續讀，只顯示一行「暫時讀不到進度，正在重試。訊息不會重送。」，讀到成功就恢復。API 標 `false` 的 5xx、bridge 自己判定的固定錯誤（例如 502 `NATIVE_STATE_INVALID`）與 4xx（例如 403、登入過期）照舊停下。 |
| 打開對話的那一次 `/status` 失敗就不再讀 | 同上。 | 暫時性失敗時，等 backoff 後再讀同一則一次，直到成功。 |
| 回覆讀取（`/reply`）失敗就停在錯誤 | 同上。 | 暫時性失敗時，等 backoff 後再讀。 |
| 進度暫停（硬錯誤）時，Web PubSub 提示也不讀 | Web PubSub 提示本來就是「到了就讀一次」；總協調要求它也是一條恢復路徑。 | 提示到了就讀一次（訊息送出中除外）；讀到成功就恢復輪詢，失敗照上面的規則。 |

**bridge**：只對 GET 把 API 的 `error.automatic_retry: true` 原樣轉給頁面（`adapter.py` `PullAdapter.send`、`bridge.py` 錯誤處理）；頁面發的 POST 一律 `automatic_retry: false`（即使 bridge 在寫入前先讀了一次）。

**和 API #688 同一次發布**：#688 之前的 API 對 stage v15 那種 503 `NATIVE_BOOTSTRAP_UNKNOWN` 回 `automatic_retry: false`，這時輪詢仍會停下，只有 Web PubSub 提示會再讀一次。寫入規則不變：UNKNOWN 只核對原件、不重送。

### Verified against（讀取重試）

- **程式基準**：`neallee2012-workbench-thin-composer` `26c239da`（#679）。已讀 `index.html` `updateProgress`／`scheduleProgress`／`openingTurn`／`readTurnReply`／`autoLoadCompletedReply`／`friendlyError`／`realtimeReadProgress`，`adapter.py` `PullAdapter.send`，`bridge.py` 錯誤處理。
- **DOM**：642 PASS。新增 8 案、改寫 1 案（依 API #688 的細化：API 標 `false` 的 5xx 不重讀）：
  - 進行中回合的讀取遇到帶 `automatic_retry` 的 503、或 bridge 回報的 `BACKEND_UNAVAILABLE`，2 秒後再讀，讀到完成就顯示回覆（×2）；
  - 打開對話的 `/status` 暫時失敗，backoff 後再讀並顯示回覆；
  - `/reply` 暫時失敗，2 秒後再讀並顯示回覆；
  - 進度暫停時，推送提示讀一次並恢復輪詢（改寫 #666 的「暫停時不讀」）；
  - 不可重試的錯誤照舊停下：403、API 標 `false` 的 503 `NATIVE_TURNS_DISABLED`、bridge 自己的 502（×3，守門案）。
  - 打開對話的 `/status` 遇到讀取衝突（409 `RESULT_UNKNOWN`／412）不會以 0 ms 連續重讀（review 後補，在第一版 `4807b973` 上 FAIL）；換對話時 backoff 重設，回覆的重試計時到了一律放行下次自動讀取。
  - 除守門案外，在 #679 的 `index.html` 上都 FAIL。
- **Python**：`test_web_test_console_native.py` 新增 1 案：GET 的 `automatic_retry: true` 傳到頁面，POST 的不傳且仍記 UNKNOWN；在 #679 的 `adapter.py`／`bridge.py` 上 FAIL。`native`／`realtime`／`continuation`／`responses` 4 檔 96 PASS／3 FAIL（已知 `test_native_errors_remain_visible_and_do_not_switch_lane`）。
- **合併 main 後**：2026-10-06 合併 `main` `27a9b450`（含 #679、#680、#688、#687）後重驗（`fead71c7`）：DOM 532 PASS；`native`／`native_environment`／`realtime`／`continuation`／`responses`／`test_web_test_console.py` 148 PASS／3 known FAIL。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="server-following"></a>
## 換版由伺服器處理：工作台只送訊息（P06，第 16 版，CODE/offline，未部署、未合併）

Neal 09:58 原話：「FA portal為什麼要控制user能否傳送訊息、能否續問?」；總原則：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
Hosted 官方說明：session 建立時綁單一版本，selector 換到新版不會遷移既有 session（見 repo 研究文件 [`docs/hosted-cold-start-latency-research.md`](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)）。API #680 已把 submit 內的 follow 前進到伺服器；工作台只負責送訊息與同 RID 重試。

| 拿掉／改掉的邏輯 | 參考做法 | 伺服器怎麼擋 | FA 現在 |
|---|---|---|---|
| 頁面在送出前或打開對話時 `GET environment`、`POST environment-auto`、`environment-prepare`／`environment-reconcile` | charter-agent 送出時不做版本／環境檢查，只把訊息送到 bridge（[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）；Hosted session 由平台／伺服器依版本處理。 | submit 若需要跟版且確定沒送出，回 `server_following: true`、`not_dispatched: true`、同 `request_id`，狀態為 409 `NATIVE_SESSION_CHANGED` 或 503 `AGENT_VERSION_FOLLOWING`。 | 頁面直接 `POST submit`。收到 `server_following` 才每 3 秒同 RID 呼叫 `submit-retry`，最多 10 分鐘；成功就結束，其他錯誤顯示並停。 |
| 前端先呼環境 follow、再最多 3 次重送 | Grok Bot 的更新／復原是 settings/error state，不在 composer 做多步確認（[settings and notifications](https://docs.x.ai/grok-bot/settings-and-notifications)）；[Hosted 版本綁定研究](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)。 | 不帶 `server_following` 的版本／檔案錯誤只代表送出結果未確認或伺服器拒絕，不能由頁面推定可自動重試。 | 沒有 `server_following` 就不自動重送；不帶 `not_dispatched` 時顯示「傳送結果尚未確認；保留原請求，不會自動重送。」；帶 `not_dispatched` 才顯示「沒有送出」。 |
| 「在新環境接續」與「檢查不搬檔接續」按鈕、no-files preview/continue 卡 | [Hosted 版本綁定研究](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)；charter-agent 沒有 portal 端檔案搬移確認（[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）。 | `NATIVE_FILES_UNKNOWN`、`NATIVE_ENVIRONMENT_UNAVAILABLE`、`WORK_POLICY_REVOKED` 等 not-dispatched 拒絕是此 RID 終態，伺服器保留原碼與原因。 | UI 不再顯示確認卡，也沒有 `/environment-without-files*` 路由；錯誤只用白話句顯示，owner 可修改後新 RID 再送。 |
| `renderEnvironment` 的 needs_follow／pending／unknown／omission 狀態機與自動打開環境詳細 | Grok Bot 把恢復與錯誤放在 bot 狀態，不讓 composer 成為第二個工作權威（[settings and notifications](https://docs.x.ai/grok-bot/settings-and-notifications)）；[Hosted 版本綁定研究](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)。 | API／Hosted 保存版本 follow、檔案、權限與原回條；工作台讀到的狀態不能擴權。 | 前端 `renderEnvironment`／環境面板已移除；Web PubSub `agent.environment` 只喚醒等待中的同 RID retry。 |

官方／Grok Bot／charter-agent 都沒有讓前端先判斷「能不能換版後再送」：charter-agent 直接送訊息，Grok Bot 用設定與錯誤狀態呈現恢復，Hosted 官方則把 session 綁版本；所以 FA 拿掉前端環境狀態機，把跟版權威留在伺服器。

**保留（最小防資料損失／重送）：**
- 同 RID 只在伺服器明確 `server_following` 時自動 `submit-retry`；不換新 RID、不改 body。
- 任何 `not_dispatched: true` 且 request_id 相符的拒絕都保留原碼與原 RID；非 version-follow 拒絕是終態。
- 無 `server_following` 的換版拒絕、Hosted resume 舊 task、UNKNOWN 與其他 5xx 不自動重送。
- 既有舊 journal 的 environment phase 仍可被讀取；只是沒有頁面或 HTTP route 會再呼叫它，避免舊資料讓 bridge 崩潰。

### Verified against（伺服器跟版）

- **程式基準**：`26c239da`（#679，疊 PR 起點）；本 PR 未部署、未合併、未改 `deploy/stage/release.json`。
- **已讀程式**：`index.html` `sendChat`／即時事件／`resendWhileServerFollows`／移除的舊 `renderEnvironment`，`adapter.py` submit refusal，`native.py` `submit`／`_retry_refused`，`bridge.py` error handler與環境 routes。
- **DOM**：`node --test --test-timeout=20000 tests/shared_context/test_web_test_console_workbench.cjs`：523 PASS。新增／更新 server_following、手動重送接回自動迴圈、離開等待後不再承諾自動送出、未標 `not_dispatched` 的 UNKNOWN 文案、舊 UI 不存在等案；在 #679／4fa／5054673d／99d0436f／fa316404 code 上新案 FAIL（RED check：暫回舊 code 後 DOM pattern 非 0）。
- **Python**：受影響檔案 72 案中 69 PASS／3 known FAIL（`test_native_errors_remain_visible_and_do_not_switch_lane` 既有 `session_read` 差異）；route-removal 與舊 pending environment journal 案 PASS；新增 `server_following`／plain sentence／UNKNOWN 文案案 PASS。RED check：前兩輪暫回舊 code 後新增 Python tests 非 0；第三／四輪以 DOM 新案覆蓋手動重送、離開等待與文案回歸。
- **合併 main 後**：2026-10-06 合併 `main` `9ddcd967`（含 #679、#680、#688）後重驗：DOM 524 PASS；`native`／`native_environment`／`continuation`／`automatic_environment`／`responses`／`files_omission`／`transfer`／`realtime`／`test_web_test_console.py` 153 PASS／3 known FAIL。合併時把 `test_web_test_console_native_environment.py` 裡 9 個不依賴已移除路由的測試留下（換版拒絕先核原 RID 與接單觀察、接單前拒絕不記成 UNKNOWN、跟版後同 CID 接續且舊回條不變、跟版後不重播舊 UNKNOWN、未開始的 UNKNOWN 可用新 RID、明確換版拒絕可同輸入重送、UNKNOWN 手動重送不能沿用較早的明確拒絕、模型拒絕後可用新 RID、新對話第一則被拒不擋），只刪用到已移除路由的 8 個；這 9 個在 P06 程式上都 PASS。
- **限制**：離線替身，不是 dev／stage 驗收；API #680 已合併（`ebcd4306`），本 PR 應在同一版發布。

<a id="thin-composer"></a>
## 輸入框常開、打開對話只讀一次（Neal 2026-10-06，第 16 版，CODE/offline，未部署、未合併）

Neal 09:58 原話：「FA portal為什麼要控制user能否傳送訊息、能否續問?」；長期原則：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
dev 第 15 版（fdbb，09:20）：bridge 重啟後打開已完成的新對話，同時送出 `/reply`、`/progress`、`/environment`、`/status` 四個讀取，之後畫面停在「尚未取得原對話的接續狀態」超過 3 分鐘。#660、#669 都是在頁面上補讀，治標；根本原因是 bridge 用記憶中的回條決定「能不能續問」，記憶一清空就鎖住輸入框。

工作台是薄 client（AGENTS.md 第 9 條）。這則訊息現在跑、排隊還是被拒，由伺服器在 submit 時回答。工作台只保留一條規則：送出過的 RID 不再送。

| 拿掉／改掉的邏輯 | 參考做法 | 伺服器怎麼擋 | FA 現在 |
|---|---|---|---|
| bridge `view()` 用記憶中的最後回條（已結束、已讀過、續接中）決定 `can_follow_up`，沒讀過就鎖輸入框 | Grok Bot：「Send another instruction while work is in progress」（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)，2026-10-06 查閱）；charter-agent `sendPrompt` 只擋空字串與自己的 `busy`（[app.js#L737-L771](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L737-L771)）。 | 同一對話上一則還在跑：Responses 在模型前寫成 `busy_refused`／`NATIVE_TURN_BUSY`（`shared_context/native_turns.py` `_response_slot`，約 765-800 行）；Invocations 由官方 chain 加 `execution_lock` 依序排隊。上一則 UNKNOWN：Hosted 從最後一個合法 checkpoint 接續（ADR-0007 第 1 節），沒有 checkpoint 時 API `_prepare` 回 409 `continuation_blocker`（`behavior_native_turn_api.py` `_prepare` 約 497-505 行；Hosted `_continuation_blocker` 約 1544 行）。 | 已登入、不是唯讀的收件對話，就能寫。 |
| 追問的 `/next` 在記憶沒確認上一回合結束時，先向 API 整段重讀 | charter-agent 送出不先讀歷史（[bridge.py#L806-L853](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L806-L853)）。 | 同上；去重由 Hosted `rid:<RID>` create-or-read 負責，最壞是收到 `busy_refused`，換新 RID 再送。 | `/next` 只在本機配新 RID，不打網路。 |
| 「上一則 UNKNOWN 就先不讓追問」（工作台自己的規則） | 三者都沒有這條：Grok Bot 可在工作進行中再送指令（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)）；charter-agent 只擋自己的 `busy`（[app.js#L737-L771](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L737-L771)）；Hosted 自己會接續（API 線 2026-10-05 確認）。 | 同上第一列。原 RID 一律不重送：bridge `submit` 的 `RECONCILE_REQUIRED`（`native.py` 約 1143 行）。 | 新訊息照送（新 RID）；UNKNOWN 那則保留原樣，仍可核對。 |
| 頁面補讀：#660 的 `status`＋整段讀取、#669 最多 3 次 `status`＋整段讀取，以及用 `observed_at` 判斷新舊 | 同第一列：畫面不需要知道「能不能續問」，自然不用補讀到確認為止。 | — | 打開這個 bridge 還沒讀過的對話（`observed_at` 為 `null`），只讀**最新一則**的 `/status` 一次（回條、進度、回覆一起到），然後停。 |
| 打開頁面時，每一則歷史回合都讀一次公開進度（#663：新的在先、一次 2 個，全部讀完） | charter-agent 不回頭讀已結束回合（同上連結）。 | — | 舊回合捲進畫面才讀它的 `/status`（回覆與時間一起到），新的在先、一次最多 2 個；「處理過程」打開時才讀進度。讀不到時顯示「尚未載入回覆，可從選單更新。」 |
| `busy_refused` 後清掉輸入框 | 同第一列。 | `busy_refused` 是這個 RID 的終態「沒執行」（API 線 2026-10-05 確認）。 | 文字留在輸入框，由使用者決定是否再送（新 RID），不自動重送。 |
| `view()` 在整個 Agent 換版中（`environment` pending）就鎖輸入框 | 同第一列；Hosted 官方 session 綁版本，換版由伺服器跟（API #680）。 | submit 回 `server_following: true` 才代表伺服器正在跟版且這次沒送出；無此欄位就只是拒絕。 | 照送；有 `server_following` 時同 RID 自動 retry，沒有時只顯示原因（見[伺服器跟版](#server-following)）。 |
| `can_submit` 在 Responses 停用時先關掉 | 同第一列。 | bridge `submit` 在寫任何紀錄前回 503 `NATIVE_TURNS_DISABLED`；Hosted `_submit_locked` 同樣在建立前擋。 | 照送，被拒就顯示原因，文字保留。 |
| 回合進行中有可追加的回合時 `can_follow_up=false` | 同第一列。 | 不需要：送出時有追加目標就走追加（`sendChat` 先判斷 `can_append`）。 | 由送出那一刻決定走追加或新回合。 |

**保留（防重送與明顯錯誤）：**
- 送出過的 RID 不再送：bridge `submit` 的 `RECONCILE_REQUIRED`。送出過的文字（結果 UNKNOWN、或同 RID 重送的結果 UNKNOWN／已接單）離開輸入框、留在它自己的回合裡（`renderComposer`），不會再按一下就用新 RID 送出同一段話；改寫成不同的文字就是新訊息。只有伺服器說「沒接單」（換版拒絕等同 RID 重送；其他拒絕是終態）或 `busy_refused`（終態沒執行）時文字才留著。
- 同 RID 自動重送只給 `server_following: true` 的換版拒絕（409 `NATIVE_SESSION_CHANGED`／503 `AGENT_VERSION_FOLLOWING`），以及 Hosted 等舊 SDK runtime 結束時的 503 `NATIVE_EXECUTION_UNAVAILABLE`（#903），每 3 秒同 RID `submit-retry`、最多 10 分鐘；放棄後回合上仍有「以原內容重送」。其他 `not_dispatched` 拒絕是終態，像 `busy_refused`：bridge 拒絕同 RID 重送（403），回合上沒有重送鍵，可修改後用新 RID 再送（目前草稿的文字留在輸入框）。
- 送出（或換版的同 RID 重送）結果 UNKNOWN、而 Hosted 讀回說沒有這個 RID（`NATIVE_RID_MISSING`，回應在建立前就遺失）：這筆維持 UNKNOWN、不重送，錯誤顯示在它自己的回合上，整段讀取照常讀完其餘部分。
- 伺服器已經回報的 `continuation_blocker`（整段讀取時讀到）照樣顯示並擋住輸入框：這是伺服器的回答，不是工作台自己猜。
- 伺服器說 `not_dispatched: true` 且 `request_id` 相符（API #680，同一次發布；只在確定沒送出、沒建立 `rid:<RID>` 時才帶，4xx 或「不確定的是換版動作」的 5xx 都可能）時，保留伺服器的原碼、記成「未接單」，整段讀取不再去讀這個 RID。本機還沒有句子的碼先用通用句。不帶這個欄位的 5xx 或不確定回應照舊記 UNKNOWN、不重送。
- 第一則訊息送出結果 UNKNOWN 時還沒有 CID，下一則要等它（`native.py` `_cid` 的 `INSTRUCTION_PENDING`），否則會開成另一段對話。
- 其他助理開來的收件對話是唯讀。
- 回合進行中有追加目標時，送出仍走「追加」；沒有時開新回合，由伺服器排隊或回 `busy_refused`。

### Verified against（輸入框常開）

- **程式基準**：`neallee2012-workbench-realtime-push` `1025a497`（#666）；review 第一輪修正 `36e7cf8d`、合併 `main` `6d4eaf1a`（含 #663、#666）、第二輪修正與合併 `main` `32f625af`（含 #670）、第三輪修正 `7c60d5a8` 與合併 `main` `91fa9bb4`（含 #664）後，第四輪修正再核一次；2026-10-06 合併 `main` `690e577a`（含 #668、#674、#677、#688）後重驗，行號依此更新：DOM 635 PASS，全部 `test_web_test_console_*.py` 703 PASS／4 FAIL／1 ERROR（已知項）。
- **已讀**：
  - `native.py` `view`／`follow_up`／`_cid`／`submit`／`turn_status`／`_not_accepted`／`_retry_refused`／`refresh`／`_refresh_responses`／`task_view`、`adapter.py` `PullAdapter.send` 的 submit 拒絕分支；
  - `index.html` `scheduleProgress`／`updateProgress`／`readTurnReply`／`autoLoadCompletedReply`／`renderConversation`／`processingPanel`／`sendChat`／`renderComposer`；
  - `shared_context/native_turns.py` `_response_slot`／`_submit_locked`／`_continuation_blocker`、`behavior_native_turn_api.py` `_prepare`。
- **DOM**：625 PASS。
  - 新增 5 案：
    - bridge 重啟後任何對話都能立刻寫，打開只送一次讀取；
    - 舊回合捲進畫面才讀回覆，新的在先、一次 2 個；
    - UNKNOWN 不擋新訊息，那則也不重送；
    - `busy_refused` 保留文字；
    - 打開「處理過程」才讀進度一次。
  - 除 UNKNOWN 那案（舊頁面也沒擋，擋的是 bridge）外，在 #666 的 `index.html` 上都 FAIL。
  - 隨邏輯刪除：#660 重啟迴圈、#669 的 4 案、#663 歷史進度 3 案、「結束後慢到的 running 快照再讀一次」。
  - 改寫：3 個側欄 activity 案改成打開處理過程時讀；「late post-ready」案拿掉 reply 邊界輪詢。
  - 兩軸 review 後再加 3 案（628 PASS），在修正前的 `index.html` 上都 FAIL：
    - 送出結果 UNKNOWN 的文字離開輸入框，不會一鍵用新 RID 再送；
    - 頁面自己打開的「處理過程」不算使用者點開：不讀進度，回合結束就收起（改用 `summary` 的 click，程式設 `open` 時瀏覽器也會發 `toggle`）；
    - 打開時先讀最新一則，畫面上的舊回合等它回來才讀（真瀏覽器的 IntersectionObserver 會立刻回報畫面上的舊回合）。
  - 第二輪 review 後再加 1 案（629 PASS），在修正前 FAIL：同 RID 重送結果 UNKNOWN 時文字也離開輸入框；還在等同 RID 重送的拒絕保留文字。
  - 第三輪 review 後再加 1 案（合併 `main` 後 634 PASS），在修正前 FAIL：終態拒絕沒有同 RID 重送鍵，文案說可修改後重新送出；「接單前版本拒絕」案的測試資料改成 bridge 現在的樣子（`submit-retry` 可按、輸入框鎖住）。
  - `nativeChat("unknown")` 測試資料改成 bridge 現在的樣子（UNKNOWN 不鎖對話，沒有 `NATIVE_TURN_UNKNOWN` blocker），頁面上已不可能出現的 `NATIVE_TURN_UNKNOWN` 追問訊息一併拿掉；相關 4 案改成斷言「不鎖、可寫、沒有 POST」。
- **Python**：`test_web_test_console_native.py` 新增 4 案並改名 1 案：
  - bridge 重啟後不讀就能寫；
  - UNKNOWN 不擋新訊息且不重送；
  - 較晚一則 UNKNOWN 時保留它的 RID、新訊息照送；
  - 第一則 UNKNOWN 時下一則等它的 CID；
  - 改名：重啟後舊對話立刻可續問。
  - 這 5 案在 #666 的 `native.py` 上都 FAIL。
  - `continuation`、`automatic_environment` 的「進行中／準備中不可續問」斷言改成可續問。
  - 兩軸 review 後再加 2 案，在修正前的 `native.py`／`adapter.py` 上都 FAIL：伺服器標 `not_dispatched` 的拒絕記成終態且整段讀取仍成功；伺服器回報的 `continuation_blocker` 顯示並擋住輸入框。
  - 第二輪 review 後再加 2 案並加強 1 案，在修正前的 `native.py` 上都 FAIL：非換版的 `not_dispatched` 拒絕不能同 RID 重送（同一段文字只用新 RID 跑一次）；換版拒絕在同 RID 重送前鎖住輸入框；建立前遺失的 UNKNOWN 送出不讓之後的整段讀取失敗、也不重送。
  - 對齊 API #680 再加 2 案：`not_dispatched` 的 403 `ATTACHMENTS_DISABLED` 與 503 `NATIVE_FILES_UNKNOWN` 保留原碼、記成終態，在修正前的 `adapter.py` 上 FAIL。
  - 第三輪 review 後再加 1 案，在修正前的 `native.py` 上 FAIL：換版拒絕後同 RID 重送結果 UNKNOWN 且建立前就遺失，之後的整段讀取照常、不重送。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="responses-accepted-display"></a>
## 剛送出的 Responses 回合顯示「已接單」（API #668 配合，第 16 版，CODE/offline，未部署、未合併）

API 線（3118f7dc）的 #668 讓 owner 的 `responses-turns/submit` 在 Hosted 寫好 accepted 回條後就回覆：`status=accepted`、`dispatch.state=needs_response`、還沒有 `response_id`，平台的 Responses POST 改在背景送。送出後到第一次進度讀取之間（約 1 秒），`processingPanel` 原本會把這種回合顯示成「尚未確認接手」並展開說明。
現在第一次核回還沒開始（`response_reconciliation` 為 null）而且沒有錯誤時，照一般「已接單」顯示（[介面去工程化](#plain-main-screen)之後主畫面字樣是「正在準備」，核回開始後是「正在等助理接手」）；核回開始後才顯示原本的說明。這只改顯示，不影響核回、去重或重送；bridge 自動的第一次核回照常執行。

### Verified against（已接單顯示）

- 程式基準 `main` `2559fd2b`。已讀 `index.html` `processingPanel`、`updateProgress` 的 `submit-reconcile`，以及 `native.py` `response_reconciliation`／`task_view`。
- DOM：新增 1 案，在 `main` 的 `index.html` 上 FAIL；原「暫時的接手不確定不當成終態 unknown」案照舊 PASS。
- 2026-10-06 合併 `main`（含 #663、#666）後重驗：DOM 635 PASS。
- 2026-10-06 #668 合併後（`main` `a40b13fe`）重驗：另讀 #668 的 `behavior_native_responses_api.py` 背景送出與 `test_web_test_console_responses.py` 改過的 3 案（submit 回 200 accepted、錯誤預算 1＋10、401／403 在第一次核回停下），核回預算段（「N6-3 Responses 工作台」）依此改寫；DOM 639 PASS，`test_web_test_console_responses.py` 31 PASS。
- 離線替身；未部署、未改 `deploy/stage/release.json`。

<a id="asynchronous-peer-delivery"></a>
## 非同步交辦的只讀狀態（2026-10-06，CODE/offline）

A的工具在官方task確認保存後，立刻回「已交給 B 排隊，B 有空就會處理，結果會回到這段對話」並完成回合；
不等B HTTP或90秒。背景投遞屬於原官方SDK task，
不是靠開著此頁輪詢才能繼續。工作台保留原回答，`queued_relay.status=queued`時繼續讀取原對話，
但A已完成的回合不再占用輸入權，可送下一則訊息。

讀回真正接收回條後沿原peer-reply路徑展示B回覆；若官方期限已到且確定未送，
顯示「交辦未送達」，若原POST／ACK未明則顯示「交辦待核對」，都保原內容與ID，
不產生重送POST或把原完成回合改成unknown。沒有新增manual環境按鈕、timer或狀態權威，
使用相同vanilla HTML／Clawpilot樣式及既有登入／Agent/CID隔離。

**Verified against：** `native.py`讀取／history／peer觀察與`index.html`原processing panel；
signed API＋傳輸替身測完A回合仍可接續、等待旗標與後台結局；DOM核未送達／待核對不取代原答案、
不發POST。新版欄位需配同build的API/Hosted/工作台，舊資料省略欄位保持相容。
Responses也會重讀帶queued引用的舊completed回合；A開始新回合後，原交辦的未送達／未知仍可更新，
不只更新最新RID。原回答、RID與已送出的內容不變。
背景SDK保存、恢復與官方timeout界線見[產品契約](../README.md#asynchronous-peer-delivery)，未live採用。

<a id="realtime-push"></a>
## Web PubSub 即時推送：工作台接收端（Neal 2026-10-06，第 16 版，CODE/offline，未部署、未合併）

Neal 08:28 原話：「如果可以降低FA portal and api 的呼叫次數，就加Azure Web PubSub 這類雲端資源。」
API 的 negotiate、發布事件、Bicep 與部署由「Web PubSub 即時推送」那條線負責；這裡只做工作台接收端。介面在 2026-10-06 由兩邊對齊。

**運作方式**
- **連線**：bridge 啟動時讀環境變數 `FA_REALTIME_ENDPOINT`，只接受 `https://<name>.webpubsub.azure.com`；沒設定或格式不對就不開推送，頁面照舊輪詢。用環境變數是為了退版安全：舊 image 不認得它也照樣啟動。
- **頁面標記**：有設定時，bridge 把頁面裡的 `const REALTIME_CONFIGURED=false;` 換成 `true`，並在 CSP `connect-src` 加上同一個主機。沒設定時頁面完全不打 negotiate。
- **negotiate**：頁面經 `GET /api/realtime/negotiate` 取得連線網址。bridge 只把目前 Agent 的請求轉給 API `/work-policy/v1/realtime/negotiate`。
  - 回傳的 `url` 必須同時符合：主機名稱與設定相同、不帶 port、`/client/hubs/<hub>`、query 只有一個 `access_token`。不符合就回 `REALTIME_INVALID`，token 不會送到別處。
  - token 不寫 journal、不記 log。
- **WebSocket**：瀏覽器用官方的「簡單 WebSocket 客戶端」`new WebSocket(url)`（[Learn](https://learn.microsoft.com/azure/azure-web-pubsub/howto-websocket-connect)，2026-10-06 查閱），不帶 subprotocol，客戶端沒有任何角色。group 由伺服器在發 token 時指定（[client access URL](https://learn.microsoft.com/azure/azure-web-pubsub/howto-generate-client-access-url)），一個 Team 一個。
- **重連**：token 只在連線時使用，每次重連都重新 negotiate（[token 期限](https://learn.microsoft.com/azure/azure-web-pubsub/chat-howto-authenticate#token-lifetime-and-refresh)）。
- **事件只是提示**：只帶 ID，頁面收到後自己讀那一項。官方說明斷線可能漏訊息（[可靠性](https://learn.microsoft.com/azure/reliability/reliability-web-pubsub#resilience-to-transient-faults)，2026-10-06 查閱），後端發布也只試一次、不重送。所以連線中全 Team 待辦仍做安全讀：距上次讀取滿 5 分鐘就讀，頁面隱藏再顯示不會把它往後延，已逾時就在顯示時立刻讀。漏掉的提示最多晚 5 分鐘（卡片 10 分鐘才過期），不會永遠看不到。
- **連線編號**：每次連線嘗試都有自己的編號，較慢的舊 negotiate 回來時直接丟掉，不會多開第二條連線。
- **錯誤碼**：API 回 503 `REALTIME_DISABLED`（推送沒開）時，頁面照常輪詢，不再重試，直到換登入或重新整理；`REALTIME_UNAVAILABLE` 等其他錯誤照退避重試。bridge 沒設定端點時也回 `REALTIME_DISABLED`（404），名稱和 API 一致。
- **退回輪詢**：連不上、10 秒內沒打開或斷線時，馬上回到原本的輪詢，並在 5 秒、15 秒、60 秒後重連，之後每 5 分鐘試一次。
- **為什麼不用官方 JS 客戶端 SDK**：`@azure/web-pubsub-client` 需要打包工具，而 `index.html` 是不經建置的單一檔案。事件只是提示，用不到 SDK 的重送與確認功能，所以採官方支援的簡單客戶端。

| 拿掉／改掉的邏輯 | 參考做法 | FA 現在（連線正常時） |
|---|---|---|
| 全 Team 待辦每 60 秒背景重掃 | Web PubSub 的目的就是「Developers no longer need to poll the server by sending repeated HTTP requests at intervals」（[JS client 說明](https://learn.microsoft.com/javascript/api/overview/azure/web-pubsub-client-readme?view=azure-node-latest)，2026-10-06 查閱）；Grok Bot 的批准卡出現在該 Bot 的對話、側欄標「Needs attention」（[settings and notifications](https://docs.x.ai/grok-bot/settings-and-notifications)，2026-10-06 查閱）。 | 載入時讀一次（等連線最多 10 秒，避免讀兩次）。新卡（`status=pending`）事件才讀一次。已決定、已回答或已結束（`hitl.closed`）的卡片依卡片 ID 從清單拿掉，不重掃；目前對話的卡也一樣拿掉，同時讀這段對話。如果拿掉時正好有一次掃描在跑，掃描結束後再讀一次，避免帶回決定前的舊卡。重複事件只處理一次，另有 5 分鐘安全讀；讀取失敗回到 60 秒重試，重連後補讀一次。 |
| 回合進行中每秒讀一次這則狀態 | charter-agent 回合中用串流推到畫面，不輪詢（[bridge.py#L806-L853](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L806-L853)）。 | 3 秒一次；`turn.finished` 一到立刻讀這一則。 |
| 等交辦回覆時每秒整段讀一次 | 同上。 | 3 秒一次；`agent.replied` 一到立刻讀。 |
| 其他 Agent 有新回覆或新收件，要點開才知道 | Grok Bot 在側欄標記需要處理的 Bot（同上連結）。 | 事件只在那位 Agent 的側欄加提示，不讀。目前 Agent 收到新對話（`agent.received`）時，用 `GET /api/instructions/received?request_id=` 匯入那一則，目前的對話不變。 |

其他對話出現新卡時讀整份 Team 待辦，沒有改成讀單張卡：提問沒有單張讀取路由，為此新增路由不符合最小機制，一份 Team 清單本來就只是一個 GET。
`agent.environment` 只喚醒正在等伺服器換版的同 RID 重送（[換版由伺服器處理](#server-following)），不讀進度。頁面在背景時不讀，切回前景才補讀；沒有提示就不讀。訊息送出中時，提示不觸發讀取；進度暫停（硬錯誤）時，`turn.finished`／`agent.replied` 提示讀一次作為恢復路徑（[讀取重試](#read-retry)起）。單一 Agent 的工作台只處理自己的回合，其他 Agent 的提示不理會。背景讀取不顯示載入區塊（[不跳動](#no-jump-background-reads)）。

**請求數對照**：離線替身、虛擬時間，同一段操作，只算 GET。由 DOM 測項「push lowers the request count of the same session (the PR's table)」算出並斷言（`node --test --test-name-pattern="request count" tests/shared_context/test_web_test_console_workbench.cjs`）。

| 情境 | 只輪詢 | 有推送 |
|---|---|---|
| 2 分鐘：回合 20 秒、第 30 秒 B 出現批准卡 | 25（Team 3、status 20、其他 2） | 12（Team 2、status 7、negotiate 1、其他 2） |
| 10 分鐘：回合 60 秒、第 70 秒 B 出現批准卡 | 73（Team 11、status 60、其他 2） | 26（Team 3，含 1 次 5 分鐘安全讀；status 20、negotiate 1、其他 2） |

B 的批准卡在輪詢時最晚要等 60 秒，有推送時事件一到就讀。真實的 dev 量測要等 Web PubSub 資源建好（第 16 版 dev 部署）。

### Verified against（Web PubSub 接收端）

- **程式基準**：`neallee2012-workbench-send-poll-slimming` `a30593fa`（#663）；2026-10-06 合併 `main` `df1e8805`（含 #663 `156da3f7`）後重驗。
- **已讀**：`index.html` `scheduleTeamAttention`／`scheduleProgress`／`updateProgress`／`runningOwnTurn`／`rememberAgentActivity`、`bridge.py` `create_app`／`page`／`instruction_progress`、`native.py` `open_received`／`_import_received`、`adapter.py` `PullAdapter.send`、`__main__.py`、`infra/web-console/web-console.bicep`。
- **DOM**：新增 15 案，全部 629 PASS；合併 `main` 後 634 PASS（`main` 619 案＋本 PR 15 案）。
  - 第一批 7 案：沒設定不 negotiate、Team 只讀一次且事件才讀、斷線回輪詢、連不上或逾時回輪詢、回合 3 秒且結束事件立刻讀、斷線回 1 秒、其他 Agent 只加側欄提示並匯入新收件、目前對話的卡片隨對話讀、背景時不讀。
  - review 後再加 6 案：5 分鐘安全讀與決定後依 ID 拿掉、慢的舊 negotiate 不多開連線、API 說推送沒開就不重試、進度暫停時不讀、單一 Agent 工作台不理會其他 Agent、請求數對照。
  - 第二輪 review 後再加 2 案：掃描途中被決定的卡會在掃描後再讀一次、目前對話的卡也會拿掉；隱藏再顯示頁面不延後安全讀。
  - 依賴推送的案子在各自修正前都 FAIL。
- **Python**：`test_web_test_console_realtime.py` 19 PASS，涵蓋端點格式、頁面標記與 CSP、negotiate 只轉送且 journal 不含 token、拒收送往別處的 url 與格式錯的回覆、API 的 503 錯誤碼原名傳到頁面，以及收件匯入的參數檢查。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="lighter-page-load"></a>
## 打開頁面少讀（Neal 2026-10-05，第 16 版，CODE/offline，未部署、未合併）

Neal 原話：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
stage／dev：打開頁面就發 22 個 API 請求；既有對話先顯示「正在準備整個 Agent 的新環境」一兩分鐘；開新對話時送出鍵先停用約 1 秒。
總協調 2026-10-05 23:4x 把這些排到第 16 版，第 15 版發布前不合併。

| 拿掉／改掉的邏輯 | 參考做法 | FA 現在 |
|---|---|---|
| 打開或切換對話就先 `GET environment`，看到 `needs_follow` 就自動跟版（新對話的送出鍵因此先停用約 1 秒） | charter-agent 送出前沒有版本或環境檢查（[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）；伺服器在 submit 內回 `server_following` 時才代表可同 RID 重試（[伺服器跟版](#server-following)）。 | 打開對話不讀環境、不自動跟版；只有 submit 的 `server_following` 進入同 RID retry loop。 |
| 頁面載入時每一則歷史回合同時讀一次公開進度 | charter-agent 不回頭讀已結束回合（[bridge.py#L806-L853](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L806-L853)）。回覆時間（Neal 10-04 要的）只能從進度讀，所以保留讀取但不一次全送。 | 新的在先、一次最多 2 個；讀完一個才補下一個。名額只算目前 Agent、對話與登入下的讀取，切換對話或重新登入不會被舊讀取卡住。進行中的回合仍由它自己的狀態讀取更新。（[輸入框常開](#thin-composer)再改成：舊回合捲進畫面才讀 `/status`，處理過程打開才讀進度。） |
| 頁面載入就讀 Team 已批准的工具與 skill 目錄（`GET /api/agents/onboarding/options`） | charter-agent 沒有建立助理的流程，打開畫面只準備對話（[研究摘要](../../../docs/charter-agent-desktop-client-research.md)）；三者都沒有在打開對話時讀建立助理用的目錄。這份目錄只在建立新助理時用。 | 打開「＋ 新增助理」才讀；讀失敗時下次打開再讀。建立紀錄（`GET /api/agents/onboarding`）仍在載入時讀，建立到一半的助理才會自動續做。 |

**檢查過但保留：送出前的 routing 讀取。** lane 固定後，Invocations 的 `refresh_routing` 本來就不打網路；Responses 只讀一次
`agent-template/options`，是唯一在寫入前擋下已停用 Responses 的地方。跳過它時，API 的 `NATIVE_TURNS_DISABLED` 不在 `_NOT_ACCEPTED`，
回合會被記成 UNKNOWN，`submit-reconcile`／`submit-retry` 都回 `NATIVE_RID_MISSING`，使用者只能開新對話。第一則訊息讀範本同理：
lane 由它決定，API 以 409 `NATIVE_STATE_SCOPE` 拒絕不一致的 lane，同樣會留下 UNKNOWN。這是「防止明顯錯誤」的最小機制，所以不拿。
（10-06 RTT A 更新：API #680 之後，Hosted 在建立 `rid:<RID>` 前的這類拒絕帶 `not_dispatched`，工作台記成「沒有送出」的終態，不再是
UNKNOWN；所以路線固定後的 options 讀取已拿掉，第一則訊息仍讀範本選路線。見[送出與讀回覆少掉三個來回](#rtt-a-workbench)。）

**打開頁面現在還會發的請求**（離線替身量到的，一個 8 則回合的既有對話）：

| 請求 | 為什麼留 |
|---|---|
| `GET /api/state`（Agent 模式是 `GET /api/agents` 加 `GET /api/instructions/models`） | 對話內容與可選模型；models 路由也順帶列出其他助理開來的對話（最多 15 秒一次）並讀 routing。 |
| `GET /api/agents/onboarding` | 建立到一半的助理要自動用原編號續做。 |
| `GET /api/native-hitl/team/attention` | Team 待辦，之後 60 秒一次（[不跳動](#no-jump-background-reads)）；有 Web PubSub 推送時，載入讀一次，之後只在新卡事件與 5 分鐘安全讀時讀（[即時推送](#realtime-push)）。 |
| `GET /api/realtime/negotiate`（只在設定 `FA_REALTIME_ENDPOINT` 時） | 取得 Web PubSub 連線網址；每次重連重打（[即時推送](#realtime-push)）。 |
| 每則還沒有進度的回合 `GET /api/instructions/{rid}/progress` | 回覆時間只能從公開進度讀。bridge 會把讀到的進度留在記憶體，bridge 沒重啟時重新整理頁面不會再讀；新的在先、一次 2 個，總數沒有減少。若 API 之後在回條或 state 帶回覆時間，這一項可以整個拿掉（屬 API 線）。（[輸入框常開](#thin-composer)起：改讀 `/status`，只讀最新一則與捲進畫面的舊回合；`/progress` 只在打開「處理過程」時讀。） |
| bridge 重啟後（每次 stage 部署）：最新一則的 `/status`、`/reply`、一次整段 `/progress?thread_id=` | [#669](#running-turn-status) 的重啟補讀：`/status` 確認可續問後停。#669 寫的「同時送出四個讀取」，在本 PR 拿掉 `/environment` 後是三個。（[輸入框常開](#thin-composer)起：只讀最新一則的 `/status` 一次，不補讀、不整段讀。） |

不變：換版跟版、不搬檔接續的真人確認與 UNKNOWN 核對；同 RID 去重與 UNKNOWN 不重送；回覆時間仍只用保存的真實時間。

### Verified against（打開頁面少讀）

- 程式基準 `main` `9ef46b5d`，2026-10-06 合併 `main` `e95f9cd5`（含 #669）後重驗。已讀 `index.html` `scheduleAutomaticEnvironment`／`refreshNativeProgress`／`readNativeProgress`／
  `renderOnboarding`／`loadOnboardingOptions`、`bridge.py` `submit_instruction`／`instruction_models`、`native.py` `refresh_routing`／`submit`／
  `read_progress`／`task_view`、`shared_context/behavior_native_turn_api.py` `_prepare`、`shared_context/native_turns.py` `_submit_locked`。
- DOM：新增 5 案（打開對話不讀環境 ×2：needs_follow／unverified；歷史進度新的在先、一次 2 個；切換對話與重新登入不被舊讀取卡住 ×2；
  打開「＋ 新增助理」才讀目錄），在 `main` 或前一版的 `index.html` 上都 FAIL。原「空白新對話送出前先檢查」2 案與
  「unverified 只讀一次」1 案描述的是被拿掉的行為，改寫成上述 2 案；「空白對話持續推進 scan_pending」改成由 bridge 回報的
  未完成跟版觸發，保留原本要守的行為；onboarding 建立／目錄 6 案改成先打開面板，另 5 案（既有紀錄自動續做）照舊不打開。
  合併 #669 後，它的重啟測項改成「三個讀取亂序回來」且斷言不讀 `/environment`（`b4273e47`）；全部 619 PASS。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="running-turn-status"></a>
## 回合進行中只讀這一則，回覆跟著結束一起到（Neal 2026-10-05，CODE/offline，未部署）

> 第 16 版起，下面兩段「重啟後卡住」的頁面補讀（#660、#669）由[輸入框常開](#thin-composer)取代：bridge 不再用記憶決定能不能續問，打開對話只讀最新一則一次。

**bridge 重啟後舊對話卡住（第 15 版發布阻擋，fdbb 在 dev `d4fcdf11`／`1c5d6fd3` 量到，總協調 23:xx 交辦）：**
bridge（8765）重啟後記憶清空，打開預設選中的舊對話時 `observed_at=null`、`can_follow_up=false`、`follow_up_blocker=null`，
畫面停在「尚未取得原對話的接續狀態」且不發任何讀取，至少 100 秒～4 分鐘；手動切對話觸發一次整段讀取（約 28 秒）才恢復。
stage 每次部署都換新容器，等於 bridge 重啟。**修正**：畫面看到 `observed_at` 明確為 `null`（這個 bridge 還沒讀過這段對話）、
沒有 blocker、最新一則自己的回合有回條時，立刻只讀一次那一則的 `status`（同下方路由），輸入框隨即可用；
接著在背景整段讀一次（不擋送出），帶進排程、歷史與其他回合的批准卡（第一次打開本來就會整段讀），然後停。
`status` 讀取失敗最多再試 2 次（間隔隨原退避），成功就清掉計數，下次重啟仍會再讀。不加回背景輪詢，也不在 `/api/state` 裡同步整段讀取。

**重啟後偶爾仍卡住（第 15 版發布阻擋，fdbb 在 dev `46910b28` 09:20 量到，總協調 2026-10-06 09:26 交辦）：**
- **現象**：預設選中的新對話（已完成、1 回合）同時送出 `/reply`、`/progress`、`/environment`、`/status`。之後畫面停在「尚未取得原對話的接續狀態」超過 3 分鐘、不再讀取；當時 `observed_at` 已有值、`can_follow_up=false`、沒有 blocker。09:24 手動讀同一則的 `/status`，bridge 回 `can_follow_up=true`。
- **原因**：`readTurnReply` 結束時會把 `stateRevision` 加一，晚回來的 `/status` 因此整筆被丟掉，頁面改用 `/reply` 那份狀態。重啟後同時送出的四個讀取中，只有 `/status` 會寫 `observed_at`，所以 `/reply` 那份不是「`observed_at` 為 null」（原本會再讀一次，但多花一輪），就是和 `/status` 同一時刻卻是「不能續問、沒有原因」（fdbb 的情況）。後者在原本的 `unobservedTurn`（現改名 `unconfirmedTurn`）不會再讀，畫面就一直卡住。
- **修正 1：較新的優先。** 同一段對話裡，以 bridge 的 `observed_at` 判斷新舊：
  - 較新的 `/status` 就算 revision 已變也照樣套用；
  - 較舊的 `/reply` 只補上回覆文字，不覆蓋整份狀態。
- **修正 2：有上限的補讀。** 最新一則自己的回合已結束（completed／busy_refused），畫面卻是「已讀過、不能續問、沒有原因、沒有在等其他助理」時，也比照未讀補讀它的 `status`。連同第一次最多讀 3 次；仍不能續問就整段讀一次，然後停。不加回背景輪詢。
- 任何一次成功讀取只要確認了最新回合（可續問、或已不需要補讀），就清掉它的計數，所以之後再重啟仍會再讀，即使是第 3 次才成功也一樣。

### Verified against（重啟後偶爾仍卡住）

- 程式基準 `main` `2559fd2b`。已讀 `index.html` `readTurnReply`／`unconfirmedTurn`（原 `unobservedTurn`）／`updateProgress`／`scheduleProgress`／`automaticConversationReadNeeded`／`followUpMessage`，以及 `native.py` `view`／`turn_status`／`download`。
- DOM：新增 5 案，全部 616 PASS。
  - 四個讀取同時回來：`/status` 先回、之後回來的 `/reply` 是在它之前算的（`observed_at` 為 null）；
  - 同上，但 `/reply` 先回、`/status` 後回；
  - fdbb 的情況：`/reply` 與 `/status` 同一時刻，卻是「不能續問、沒有原因」；
  - bridge 一直回「不能續問」時，最多 3 次 `status` 加 1 次整段讀取就停；
  - 第 3 次讀取才確認的回合，之後再重啟仍會再讀（review 後補）。
- 前 4 案在 `main` 的 `index.html` 上都 FAIL，原因各自對應：前兩案「較新的優先」少了 → 多讀一次 `status`；fdbb 的情況一直不能續問；有上限的那案 0 次讀取。第 5 案在第一版修正（`e796d88d`）上 FAIL。
- 依 review：重啟後同時送出的四個讀取（`/reply`、`/progress`、`/environment`、`/status`；第 16 版[打開頁面少讀](#lighter-page-load)起沒有 `/environment`，是三個）中，只有 `/status`（`turn_status`）會寫 `observed_at`，所以同時回來的 `/reply` 只會帶 null 或相同時間；測項照這個情況建。整段讀取（`refresh`／`_refresh_responses`）與 `reconcile_response` 也會寫 `observed_at`，所以平常 `/reply` 可能帶較舊的真實時間，`conversationStateOrder` 兩種都會處理。
- 離線替身，不是 dev 驗收；未部署、未改 `deploy/stage/release.json`。

Neal 原話：「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
dev 第 15 版量測：模型 2～3 秒就結束，畫面晚 8～14 秒才出現回覆——回合進行中工作台每次讀整段對話
（`/api/instructions/progress?thread_id=`，每次 11.5～14.8 秒，對話越長越慢），讀到結束後下一輪才另讀 `reply`（2.6 秒）。
主因：Responses lane 每次整段讀取都對這段對話**每一則**舊 RID 各讀一次回條；HITL 也對每一則舊 RID 各讀一次待批。

| 拿掉／改掉的邏輯 | 參考做法 | FA 現在 |
|---|---|---|
| 回合進行中每次讀整段對話，再交替讀這一則的進度（C14／C54） | charter-agent 一個請求對應一個進行中的回合，只等這一則的事件（[bridge.py#L654-L682](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L654-L682)）；結束時 `turn.complete` 帶最終文字（[bridge.py#L1051-L1088](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L1051-L1088)）。 | 自己的原生回合在 accepted／started 時，每秒只讀新的 `GET /api/instructions/{RID}/status`：這一則的回條、公開進度（到 finished 為止），結束時同一次讀回覆文字，回傳的就是整份畫面狀態。等待其他助理回覆、換版等待、續接等仍走原本整段讀取。 |
| 回合結束後下一輪才另讀 `reply` | 同上，最終文字跟著完成事件一起到。 | 狀態讀取看到結束就讀原結果（同一份 hash／長度核對），不另讀 `reply`。 |
| 回合中為了批准卡逐 RID 讀整段對話的待批與提問 | Grok Bot 的批准與提問在對話裡顯示（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)，2026-10-05 查閱）；批准與 ask_user 都是在工具呼叫裡等待。 | 只讀這一則的待批與提問，而且只在有工具正在執行、公開進度沒有新事件（例如在等批准），或讀不到進度時才讀；看到回合結束的那次再讀一次，讓卡片關閉。 |
| 回合結束（非正常完成）後仍逐秒讀整段對話 | 同上，一個請求對應一個回合。 | 以 UNKNOWN（Hosted 自動續接）、失敗、取消或忙碌結束時，畫面立刻做**一次**整段讀取，帶進續接、blocker 與歷史，之後照原規則。 |
| Responses lane 整段讀取時對每一則舊 RID 重讀回條 | charter-agent 只保存最新的 `previous_response_id`，不回頭重讀已結束的回覆（[bridge.py#L806-L853](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L806-L853)）；FA 的 Invocations lane 本來就只重讀最新一則。 | 較早且在記憶中已 `completed`／`busy_refused` 的回合不重讀；最新一則與未結束的照讀。 |
| HITL 整段讀取時對每一則舊回合重讀待批 | 同上，已結束的回合不回頭重讀；回合關閉後也不能再新增或恢復批准（`NATIVE_HITL_TURN_CLOSED`）。 | 已讀到 closed 且已結束的回合不重讀。 |

不變：回條、進度與結果的核對（scope、digest、hash、長度）；UNKNOWN 不重送；批准只能用原卡片；
`delivered` ACK 仍另外送。沒有新增 API、權限或保存格式；狀態讀取只組合既有的 GET。

**部分完成：**「回合結束到畫面出現回覆 ≤ 2 秒」以 dev 數字估算現在約 4～6 秒（進行中那次讀取＋1 秒間隔＋結束那次的回條、
進度、結果讀取），比原本 8～14 秒短但未達標。API 線的 PR-D（`GET .../conversations/{cid}/requests/{rid}/status`，
一次讀回條、進度與第 0 頁原文）合併並部署後，bridge 這一步可再從 2～3 個 GET 縮成 1 個；介面已談定，未合併前不接。
（10-06 RTT A：#664 已合併部署，bridge 已接上，見[送出與讀回覆少掉三個來回](#rtt-a-workbench)。）
sqlite `disk I/O error` 與 `ERR_INSUFFICIENT_RESOURCES` 是否消失要 dev 部署後量測。

### Verified against（回合中只讀這一則）

- 程式基準 `main` `d4fcdf11`。已讀 `native.py` `refresh`／`_refresh_responses`／`read_progress`／`download`／`_answer`、
  `native_hitl.py` `refresh`、`native_questions.py` `refresh`、`bridge.py` `instruction_progress`／`read_instruction_reply`，
  `index.html` `scheduleProgress`／`updateProgress`／`refreshNativeProgress`／`autoLoadCompletedReply`；
  公開進度事件格式 `shared_context/behavior_assistant_wire.py` `PublicProgressEvent`（`tool_start_seq` 對應開始與結束）。
- DOM：609 PASS。新增 8 案（兩 lane 回合中只讀 status、不另讀這一則的進度；結束那次 status 直接帶回覆、不讀 `reply`；
  以 unknown／failed 結束後做一次整段讀取；兩 lane bridge 重啟後舊對話只讀一次 status 就可續問；等待其他助理回覆時仍整段讀取，節奏見「等待其他助理回覆時的讀取節奏與原生讀取少寫入」條目）。
  除最後一案（保留原行為）外，在修正前都 FAIL；重啟兩案另以「讓重啟判斷失效」的突變確認會 FAIL。
  測試替身讓 `/{RID}/status` 和整段 progress 回同樣形狀的狀態。
- Python：新增 6 案（status 路由只讀這一則、結束時帶回覆且可接續、不讀 history；批准／提問只在工具執行中或進度停住時讀；
  結束那次讀一次待批／提問讓卡片關閉；bridge 重啟（同一 journal 開新 bridge）後一次 status 讀取即可續問並取得新 RID；
  Responses 不重讀較早已結束回合；HITL 不重讀已關閉的已結束回合），在 `main` 或修正前都 FAIL。
  `test_web_test_console_{native,continuation,hitl,questions,automatic_environment,responses}.py`：110 PASS／3 FAIL（`main` 上同樣失敗的
  `test_native_errors_remain_visible_and_do_not_switch_lane`）。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="one-request-send"></a>
## 按下送出就是一個請求（Neal 2026-10-05，CODE/offline，未部署）

> 2026-10-06 P06 更新：送出後的換版也不再由頁面 `environment-auto`；只有 API #680 回 `server_following` 時，頁面才同 RID `submit-retry`。見[伺服器跟版](#server-following)。

Neal 原話：「charter-agent 按下訊息發送鍵後，似乎是馬上就送出去，對比於FA Agent的需要等個幾秒鐘，體感差很多」、
「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
dev 第 15 版量測（22:15～22:21）：「正在準備回覆…」出現前串行四步共 10～21 秒——
`GET environment` 約 1 秒、`POST environment-auto` 1.3～1.9 秒（Agent 已接續完成仍每次跑）、追問的 `POST next` 7.7～10 秒、
`POST submit` 7.8～8.3 秒，之後還要等 `GET /api/state` 才顯示。

| 拿掉／改掉的邏輯 | 參考做法 | FA 現在 |
|---|---|---|
| 送出前先 `GET environment` 再 `POST environment-auto`（C09／C10） | charter-agent 先畫使用者訊息與 Thinking…，背景一個請求，送出前沒有版本或環境檢查（[app.js#L756-L767](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L756-L767)、[bridge.py#L498-L528](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L498-L528)）；Hosted 官方：閒置 session 由平台自動恢復，換版跟隨在 submit 伺服器端完成（API #680）。 | 直接 `POST submit`。只有 submit 回 `server_following: true` 且同 `request_id` 時，才同 RID、同 body 每 3 秒 `submit-retry`；頁面不再 `environment-auto`，10 分鐘未完成就停並保留手動重送。 |
| 追問的 `/next` 先重讀整段對話（C20 的一部分） | charter-agent 送出不先讀歷史，只帶已保存的 `previous_response_id`（[bridge.py#L806-L853](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L806-L853)）；同一對話一次只跑一回合由 Hosted 保證（Responses 在模型前回 `busy_refused`，Invocations 依序排隊，API 線 2026-10-05 確認）。 | 上一次進度讀取已在記憶中確認這回合結束時，`/next` 直接配新 RID，不再向 API 重讀；記憶中沒有（例如 bridge 重啟、多個 replica）才照舊重讀。**部分完成**：`/next` 沒有併進 submit，追問仍是兩個串行 POST，但第一個只剩本機往返。（[輸入框常開](#thin-composer)起，`/next` 一律不打網路。） |
| submit 回來後等 `GET /api/state` 才顯示「已接單」 | charter-agent 收到事件就更新同一個泡泡（[app.js#L777-L918](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L777-L918)）。 | submit 回傳的 task 直接更新畫面，完整狀態之後再讀。 |
| 新對話建立後再 `GET /api/state`，被搶先時再讀兩次（C08） | charter-agent 建立 session 後立刻用回傳的 `agent_session_id`，不再讀一次（[bridge.py#L719-L746](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L719-L746)）；建立回應本身就是新對話的狀態。 | 直接採用建立回應（登入仍有效、仍是同一個開啟動作、同一位助理時）；並讓較早的讀取失效，不會把畫面換回舊對話。 |

不變：同 RID 去重與 UNKNOWN 不重送（`submit-retry` 只接受 journal 確認未接單的 RID，body 用原 journal 的那份）；
入口 Entra、CSRF、附件與大小檢查；UNKNOWN 核對。P06 起沒有不搬檔接續真人確認卡。沒有新增 API、權限或保存格式。
送出 7.8～8.3 秒大部分在 API／Hosted 線（PR-A／B／C）處理；工作台 bridge 在 submit 前仍會跑一次 `refresh_routing`
（Responses lane 讀 options，新對話另讀範本），留待後續一起瘦身（10-06 RTT A：路線固定後不再讀，見[少掉三個來回](#rtt-a-workbench)）。換版重送只在 journal 證明未接單時進行；
P06 起，換版沒完成時只依 `server_following` 同 RID 重試；10 分鐘仍未完成就顯示「換版還沒完成，訊息已保留；可稍後以原內容重送。」並保留手動重送。

### Verified against（一個請求送出）

- 程式基準 `main` `90892e79`。已讀 `index.html` `sendChat`／`resendWhileServerFollows`／`run`、
  `native.py` `submit`／`follow_up`／`resume_accepted`／`_retry_refused`／`task_view`、`bridge.py` `submit_instruction`／`new_instruction`。
  介面依 API 線 2026-10-05 22:2x 回覆：AGENT_VERSION_FOLLOWING 一律代表沒建立回條；NATIVE_SESSION_CHANGED 只有 `not_dispatched=true` 才是。
- DOM（`node --test tests/shared_context/test_web_test_console_workbench.cjs`）：601 PASS。新增送出 8 案（兩 lane 一個 submit、
  追問只 next＋submit、submit 回傳立即顯示已接單、兩種換版拒絕各只 follow 一次＋同 RID submit-retry 一次、
  journal 未證明未接單就顯示錯誤不重送、follow 失敗停下不重送），在 `main` 的 `index.html` 上都 FAIL。
  「送出前跟版」的 29 案（idle file follow／system policy 經送出觸發／送出中逾時核對等）隨邏輯刪除。
  新對話：5 案改成用建立回應；「建立後再讀被搶先」改成「較早讀取晚到不換回舊對話」、「建立後再讀時登入過期」
  改成「開新對話途中登入過期不採用舊回應」；只針對「建立後再讀失敗」的 1 案刪除。
- Python：`test_web_test_console_{native,native_environment,continuation,frozen_interface,prompt_assembly,responses,transfer}.py`
  124 PASS／4 FAIL。追問案新增「`/next` 不呼叫 API」斷言（原 `native.py` FAIL、新版 PASS）。
  4 FAIL：`test_native_errors_remain_visible_and_do_not_switch_lane` 3 案（`main` `601aca90` 已重現）與
  `test_metadata_preview_can_confirm_without_guessing_source_or_sending_the_draft`（還原 `native.py` 到 `main` 同樣 FAIL）。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

<a id="no-jump-background-reads"></a>
## 畫面不再跳動、背景少讀（Neal 2026-10-05，CODE/offline，未部署）

Neal 原話：「FA portal的畫面會跳動」、「請嚴格依照hosted agent官方和grok bot的方式，詳細審視FA agent的作法，把非必要的邏輯拿掉」。
stage -14 錄影（21:35）：全 Team 待辦每 5 秒背景重讀，讀取中把空的「需要處理」區塊顯示成載入狀態、讀完又收起，整段對話每約 5 秒被推一下。

| 拿掉／改掉的邏輯 | 參考做法 | FA 現在 |
|---|---|---|
| 背景讀全 Team 待辦時顯示「需要處理／正在讀取全 Team…」載入區塊 | Grok Bot：「The transcript shows tool activity, computer use, created files, questions, approval requests…」（[chat and collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)，2026-10-05 查閱）；charter-agent 送出後只更新目前對話的泡泡（[app.js#L756-L767](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/assets/app.js#L756-L767)），沒有背景區塊。 | 背景讀取不改版面；只有真的有待辦、讀取失敗（顯示一次，下次讀成功才收起，重試中不閃）或你按「更新全 Team 待辦」時才出現。 |
| 全 Team 待辦每 5 秒重掃 | 同上：批准跟著對話走；charter-agent 唯一的背景排程預設 30 分鐘一次、只掃有宣告的 project（[config.py#L110-L114](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/config.py#L110-L114)）。 | 目前對話自己的卡隨它的進度讀取立刻出現（不再等全 Team 清單）；全 Team 清單是 Team 層級，載入／換助理時讀一次，切換對話不重讀，之後每 60 秒背景讀；「更新全 Team 待辦」按鈕在區塊有卡或有錯誤時可按（沒有卡時區塊不出現，與原本相同）。掃描途中沿用上一份完整清單，不閃出其他對話的舊卡。 |
| 沒選中但還在跑的 Agent，每 5 秒讀它的完整進度更新側欄 | charter-agent 是單一對話、不讀其他對話（[bridge.py#L654-L682](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L654-L682)）；回合在 Hosted 照跑，不需要畫面盯著。 | 拿掉。點回該 Agent 時讀它的最新狀態；B 回到 A 對話的回覆仍照原規則更新 B 側欄（只讀一次）。 |
| 每次讀進度時順便列「其他助理開給我的對話」（每 15 秒最多一次） | 收件清單與送出／回覆無關；charter-agent 每個 project 只保存自己的 `agent_session_id`／`previous_response_id`，沒有跨助理收件清單（[bridge.py#L719-L746](https://github.com/MSFT-Innovation-Hub-India/charter-agent/blob/92a44cf3705a53620a9d38db54304396652b1857/desktop-client/charter_client/bridge.py#L719-L746)）。 | 只在載入或切換到這位助理時讀清單；用 RID 直接打開收件對話照舊。 |

不變：批准仍只能用原卡片與確認視窗、同 once ID 只核回；登入、Team 範圍、request ID 去重與 UNKNOWN 不重送都沒動。
（2026-10-07 起卡片按鈕不再跳確認視窗，見[卡片一按就送](#hitl-one-click)。）
沒有新增 API、權限或保存格式。

**要 Neal 決定：** 全 Team 待辦要不要常駐即時。現在是 60 秒背景（有卡或錯誤時可手動更新）；若要 A 交辦給 B 時 B 的批准卡在 5 秒內出現，或改成完全手動，請決定。
（2026-10-06 08:28 Neal 決定改用 Web PubSub 推送，見[即時推送](#realtime-push)。）

### Verified against（不跳動與背景少讀）

- 程式基準 `main` `601aca90`。已讀 `index.html` 的 `readTeamAttention`／`scheduleTeamAttention`／`renderAttention`／`currentAttention`、
  側欄 `rememberAgentActivity`／原 `updateAgentActivity`，以及 `bridge.py` `instruction_progress`、`native.py` `refresh_received`
  （讀進度只走新的 `open_received`：用 RID 打開指定收件對話，不列清單）。
- DOM（`node --test tests/shared_context/test_web_test_console_workbench.cjs`）：623 PASS。新增 7 案：背景讀取不顯示載入區塊且錯誤顯示一次、重試中不閃，
  手動更新顯示載入、目前對話新卡不等全 Team、切換對話不重掃全 Team、掃描途中不閃其他對話舊卡、
  不讀沒選中的 Agent、晚到接單只更新側欄；除手動更新（保留原行為）外，在 `main` 的 `index.html` 上都 FAIL。
  原 11 個沒選中 Agent 5 秒讀取的測項與 1 個「等待交辦回覆的慢速讀取」測項隨邏輯刪除；Team 計時器斷言 5000→60000；
  新對話 4 案改成由 60 秒背景讀觸發 Team 讀取（新對話不再重掃）。
- Python：`python -m pytest tests/shared_context/test_web_test_console_native.py`：26 PASS／3 FAIL。收件清單兩案改成從載入
  （`/api/instructions/models`）列出，並斷言讀進度不再列清單。3 FAIL 是 `test_native_errors_remain_visible_and_do_not_switch_lane`，
  在 `main` `601aca90` 單跑同樣 3 FAIL（期待 `read`、實際先 `session_read`），與本片無關。
  `test_web_test_console_{team_attention,agents,hitl,questions,remote}.py`：81 PASS。
- 離線替身，不是 stage／dev 驗收；未部署、未改 `deploy/stage/release.json`。

## 空白對話不中斷接續、顯示收件人換版排隊（2026-10-05，CODE/offline）

stage B原件：空白對話的messages沒有最後一筆，last也不存在，原比較把undefined===undefined當真，
下一個`latest.state_profile`拋TypeError，render中斷後scan_pending不再推進。
現先核兩個物件存在；原DOM在progress_available=true的空白Responses對話先重現相同TypeError，
修後同body第二個environment-auto確實送出並ready，不自送文字。

發版後全員接續及交辦等待已由伺服器／原Hosted持久task推進，不要求畫面停在收件人。
既有公開進度顯示「已排隊，B 換版完成後處理；尚未確認送達，不會重送。」，
不把排隊當已送達／已執行；兩lane皆保原工作、HITL、草稿與同ID。
詳見[伺服器接續README](../README.md#server-environment-follow)；未改主題或引入前端狀態權威，未live複測。

<a id="agent-received-conversations"></a>
## B 也看得到 A 交辦的對話（Neal 2026-10-05，CODE/offline，未部署）

Neal 原話：「當A傳給B時，B回覆的訊息會顯示在A，但是B那邊反而沒有顯示，可以讓B也顯示回覆給A的訊息嗎?」
A 用 `send_agent_message` 交辦時，B 在自己的 Hosted session 開一段**新對話**（RID＝A 交辦紀錄的 `message_id`），
回覆以 `agent_result` 回到 A。原本工作台的對話清單只來自本機 journal，所以 B 這段對話不會出現。

- **從 B 自己的回條讀清單**：B 的工作台呼叫新的
  [`GET /work-policy/v1/native-turns/received`](../README.md#agent-received-conversations)，
  只拿到「其他助理在 B 開的對話」的 RID、CID、狀態、寄件助理與種類，不含內容。
  工作台每 15 秒最多讀一頁（最新 50 筆 key），只在載入或切換到這位助理時讀（2026-10-05 起不再於更新進度時順便讀，見[背景少讀](#no-jump-background-reads)）。
  只讀最新一頁：`fa/v1/turns` 每回合有數個 key，所以這頁約涵蓋最近十幾個回合；更早的收件對話不會自動列出，
  但 A 那邊顯示 B 的回覆時（見下方側欄）或用網址指定 RID 時，仍會用原 RID 讀回條後掛入。
- **掛進對話清單**：新的收件對話用它的原 RID 當 thread，沿用群組觀察既有的 `observe_native` 掛進清單，
  journal 記 `agent-received` 和核過的原回條（與群組觀察相同，只是快取伺服器回條，不是另一份紀錄）。
  打開後照既有 conversation／history／result 讀 A 轉來的原話、B 的處理過程與回覆。
  網址或側欄指定的 thread 本機還沒有時，先用該 RID 讀原回條，確認是別的助理開的對話才掛入；否則照舊 `INSTRUCTION_SCOPE`。
  讀不到清單（較舊的 API 或 image）只記 `received_error`，不影響自己的對話。
- **唯讀**：收件對話不能送出、追問或追加，畫面與工作台 server 端都擋（`AGENT_CONVERSATION_READ_ONLY`）。
  理由：這段工作屬於 A 的原請求，B 的回覆已自動送回 A；在 B 這邊用真人訊息接續不會回到 A，
  還會把 Agent 交辦與真人指令混在同一段對話。這是最小做法：不改 API 授權（owner 本來就能讀 B 的對話），
  只是工作台不提供接續入口；批准卡、停止回覆等 owner 原有動作照舊。要接著談就回 A 的原對話，或開新對話。
- **畫面**：清單顯示「來自 A 的交辦：<原話>」；第一則標「收到 A 的交辦（Agent 提供的資料，不是你的原話）」，
  接著是 B 的處理過程與回覆；輸入框位置改顯示唯讀說明。不顯示私有推理、憑證或對方的私人內容。
- **側欄**：B 的回覆一到 A 的對話（不必等 A 處理完），工作台就經 B 的工作台純讀 B 那段收件對話
  （`/api/instructions/progress?thread_id=<message_id>`，每位同事只讀最新一則、同一則只讀一次），
  B 的側欄最新訊息與時間改成 B 的回覆並標「新動態」；點 B 直接打開這段對話。
  第一次打開 A 的對話（例如重新整理後）時已經在的回覆，只在這個頁面還沒有 B 的側欄紀錄時更新 B 的預覽，
  不標「新動態」；已經看過 B 的話保留 B 原本的預覽和要打開的對話，不被 A 的舊對話換掉。
  - **Responses lane（dev 第 15 版修正）**：B 收到交辦的回合依 B 的範本走 `foundry-responses-v1`
    （`submit_native_turn` 的 `turn_profile`），這條路讀進度時不帶已保存的答案（打開的對話靠 `autoLoadCompletedReply`
    另讀 `/reply`），所以 dev `601aca90` 的預覽退回顯示 A 交辦的原話。現在讀完進度若最新一則已完成、沒有回覆且
    `read-reply` 可用，就經 B 的工作台再純讀一次 `/api/instructions/<RID>/reply`（同打開對話的讀法），預覽改成 B 的回覆；
    讀不到就保留進度讀到的最新訊息，不重試。仍只在 A 第一次顯示 B 的回覆時讀，不加背景輪詢。

### Verified against（B 的收件對話）

- 程式基準 `main` `e777e92b`。已讀 `native_agent_relay.prepare_request`（交辦一律開新對話、RID＝`message_id`）、
  `native_turns.history/_check_rid`、`native_effects.read_hitl_history`（同樣的官方 `list_keys` 分頁讀法）、
  固定 `azure-ai-agentserver-core` 2.2.0 `storage/_state.py` 的 `list_keys`，以及本檔 `native.py`、`groups.py` 的觀察掛入。
- 官方能力（2026-10-05 查閱）：Foundry [List conversations](https://learn.microsoft.com/rest/api/microsoft-foundry/aiproject)
  可依 `agent_name` 篩選，但列的是 Foundry Responses conversation（preview），不是 FA native State store 的對話；
  [Hosted sessions](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions#list-sessions)
  列的是 VM session，官方明寫 session 與 conversation 不同；Copilot SDK 1.0.13 `CopilotClient.list_sessions`
  （[client.py](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L3824)）
  只列 SDK session，沒有 FA 對話欄位。所以用 State store 正式 `list_keys` 讀 B 既有回條。
- 工作台（真 signed API＋Hosted 傳輸替身）：列出、打開、唯讀、追問與追加被拒、清單未到期前用 RID 直接打開、
  非收件 RID 仍 `INSTRUCTION_SCOPE`、15 秒內只讀一次清單、舊 API 回 404 時不擋自己的對話送出；
  主人自己的新草稿 `can_submit` 仍為 true；全程沒有 submit／receive。
- DOM：清單標籤、Agent 資料標示、唯讀說明；B 的回覆到 A（A 尚未處理完）時只經 B 讀一次、B 側欄更新並標新動態、
  點 B 打開這段對話；重新整理後已存在的回覆只更新預覽、不標新動態；之後才打開的 A 舊對話不換掉已看過的 B 預覽與對話；全程無 POST。
- mutation：拿掉 Hosted「本身開了新對話」條件、bridge 讀清單、唯讀判定、側欄讀取、清單標籤、
  側欄改看 A 的處理回覆、無條件標新動態或拿掉「已看過 B 不覆蓋」，各自讓對應測項失敗。
- 既有失敗：`test_web_test_console_native.py` 三個 `test_native_errors_remain_visible_and_do_not_switch_lane`
  （期待 `read`、實際先 `session_read`）在 `main` `e777e92b` 單跑同樣失敗，與本片無關。未部署、未改 `deploy/stage/release.json`。
- dev 第 15 版修正（基準 `main` `d4fcdf11`，含 #656 只在載入時列清單、#657 送出流程）：已讀 `behavior_native_responses_api.submit_native_turn`
  （收件回合的 profile 依範本）、`native.py` `_refresh_responses`（不呼叫 `_answer`）與 `download`、`bridge.py`
  `instruction_progress`（`open_received` 仍用 RID 打開）。Python（真 signed API＋Hosted 傳輸替身）：Responses 收件對話的進度讀回
  `reply` 為 null、`read-reply` 可用，`/reply` 帶回 B 的答案，全程只有 GET。DOM：B 的進度沒有答案時，只經 B 讀一次進度與一次 `/reply`，
  預覽是 B 的回覆並標新動態，再次 render 與跑完所有待執行計時器後都不再讀 B，無 POST；換回修正前的 `refreshPeerPreview`，預覽變成
  「Agent：請查一下 VM 的 CPU、記憶體和磁碟」（即 dev 所見）而失敗。`/reply` 回非 JSON 的 502 時，仍保留進度讀到的內容、標新動態、
  點 B 打開這段對話，且不重讀（#659 審查 P3：原本這種失敗會連進度內容一起丟掉）。

<a id="chat-bubbles-menus-sidebar"></a>
## 對話泡泡、選單與側欄（Neal 2026-10-04，CODE/offline，未部署）

Neal 2026-10-04 晚間提出的工作台 UI 修正。全部只在瀏覽器端，不改 API、保存格式、權限或重送規則。

- **選單點外面自動收起**：右上「⋯」與左下「⚙」選單打開後，點選單以外的地方，或選了選單裡的任一個按鈕，選單就收起；
  選單裡的「歷史對話」下拉與「Session 資訊」等嵌套項目照常操作，不會被關掉。Esc 照舊收起，焦點回到選單按鈕。
  用 document 的 capture 階段判斷，會停止事件傳遞的元件（例如桌面畫布）也算點外面。
- **助理回覆支援 Markdown**：助理回覆（含串流中尚未保存的文字）與其他助理傳來的訊息，用 DOM 節點渲染常見的
  CommonMark／GFM 寫法：標題、段落與換行、粗體／斜體／刪除線、條列（含編號與巢狀）、行內 code 與 code block、
  引用、分隔線、表格（含對齊）、連結。你自己打的訊息、排程與自動接續的文字仍是純文字。
  - 安全：完全不解析 HTML，也不使用 `innerHTML`；模型輸出的 `<script>`、`<img onerror>` 等原樣當文字顯示。
    只有 `http://`／`https://` 連結會變成可點連結（新分頁、`rel="noopener noreferrer"`）；`javascript:`、`data:`、
    相對路徑等照原文顯示。Markdown 圖片只變成「圖片：說明」連結，不自動載入遠端網址。
  - 不支援（照原文顯示）：原始 HTML、參照式連結、setext 標題（`---` 一律是分隔線）、縮排式 code block、
    HTML entity 解碼、task list 勾選框。超過 64 欄的表格不當表格；超過 200,000 字元、或表格格子合計超過 20,000 個的
    回覆，改用原本的純文字顯示。
  - 「下載答案文字」、保存的原文、bytes 與 SHA-256 都不變；Markdown 只影響畫面。
- **時間與複製**：使用者訊息、追加的文字、其他助理的訊息與已保存的助理回覆，泡泡右下角都有「複製」，複製該則原文
  （助理回覆是原始 Markdown），按下後短暫顯示「已複製」，失敗顯示「無法複製」。時間只用已保存的真實時間，沒有就不顯示：
  - 你的訊息：工作台送出前寫入 journal 的 `submission.intent.created_at`（滑鼠停留顯示「送出時間」）。
  - 助理回覆：Agent 公開處理步驟裡 `SDK turn ended` 的時間（「Agent 記錄的回合結束時間」）；沒讀到處理步驟
    （例如較舊的回合）就不顯示。原生回合的 `reply.observed_at` 是工作台最後一次讀取對話的時間，不是回覆時間，不使用。
  - 排程、自動接續、追加的文字與其他助理的訊息沒有可用的保存時間，不顯示時間；串流中尚未保存的文字不顯示時間也不提供複製。
  - 顯示台灣時間：今天只顯示 `HH:mm`，其他日期加 `MM/DD`，不同年份再加年份；滑鼠停留顯示完整時間。用絕對時間而不是
    「幾分鐘前」，不必另開計時器；跨過台灣午夜後，畫面下一次更新時替昨天的訊息補上日期。
- **側欄可以捲動**：整個左側欄可以上下捲動，展開「即時桌面」與「＋ 新增助理」後表單捲得到底；助理清單保留自己的捲動，
  至少 120px 高；底部登入／設定列固定在側欄底部。窄螢幕的抽屜是同一個側欄，一樣可以捲動。

### Verified against（泡泡、選單與側欄）

- 程式基準：`main` `a667fc51`（含 #639 新對話草稿修正）加本分支各 commit。已讀 `index.html` 的 `renderConversation`、Esc 處理與側欄 CSS。
- 選單：DOM 測試涵蓋點外面收起、選動作收起、嵌套「歷史對話」／`#conversation`／「Session 資訊」不收、⚙ 選單、
  Esc 焦點回到選單按鈕。
- Markdown：不引用外部套件。marked 的 README 明說不 sanitize 輸出 HTML，要另配 DOMPurify；markdown-it 預設安全，
  但兩者都輸出 HTML 字串，要用 `innerHTML` 插入，並多 vendor 一或兩個套件（另受 NPM 7 天隔離政策影響）。
  自寫的 renderer 只建 DOM 節點、不解析 HTML，安全性由構造保證，也能在既有離線 DOM 測試中檢查結構。語法依
  [CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/) 與 [GFM](https://github.github.com/gfm/) 的子集
  （查閱 2026-10-04）。DOM 測試涵蓋表格（含對齊）、粗體、連結、條列、code、引用、分隔線；code span 先於強調
  （粗體裡的 `` `**/*.ts` `` 不會被拆開）、結尾有空白的 code span；`<img onerror>`、`<script>`、`<iframe>`、`javascript:`、
  `data:`、相對連結都當文字，沒有任何 IMG／SCRIPT 節點。惡意與超長輸入有界：標題與表格分隔列用字串判斷，避免 regex
  在長空白上回溯；連結掃描每次有長度上限、每則回覆另有總量上限（用完後該則後面的連結以文字顯示，一般回覆遠用不到）；
  表格欄數與格子數有上限；子節點逐一加入，大量短行不會超過瀏覽器的參數上限；renderer 任何例外都改用純文字，
  不會讓對話畫到一半（review 找到的表格放大、regex 回溯與參數上限都已補測）。單獨量測 45k 字元回覆約 18 ms，
  17 種惡意輸入（接近 200,000 字元上限）各在 0.1 秒左右內完成（本機 Node 量測最慢約 90 ms），產生的節點數都不超過字元數。
- 時間與複製：已讀 `native.py` `task_view`（原生回條、公開回合都沒有回覆時間；`reply.observed_at` 是
  `self.observed[thread]`）與 `bridge.py` `Journal.begin/get`（`intent.created_at` 在送出前寫入 SQLite）。DOM 測試涵蓋
  送出時間與回合結束時間的台灣時間、跨日與跨年格式、跨過午夜後下一次更新補上日期、複製原文（含原始 Markdown）、
  「已複製」1.5 秒後還原、沒有保存時間時沒有任何 `<time>`。Clipboard API 只在 secure context 可寫
  （[MDN](https://developer.mozilla.org/docs/Web/API/Clipboard/writeText)）；本機 `127.0.0.1` 與遠端 https 都符合。
- 側欄：DOM 測試核 CSS；真瀏覽器量到 1100×560 與 400×560 抽屜都能捲到「建立助理」按鈕，16 位助理時清單自己捲動。
- 真瀏覽器（Playwright Chromium、loopback 替身伺服器、合成狀態）：Markdown 版面、惡意文字原樣、選單三種情況、
  側欄捲動、複製寫入原始 Markdown 都看過。這不是 Hosted 或 Azure 證據。
- 完整 DOM 621 PASS（main 613 案＋新增 8 案；既有 8 案改成讀渲染後文字）。未部署、未改 `deploy/stage/release.json`。

<a id="new-conversation-opening"></a>
## 新對話切換延遲時保留草稿（CODE/offline）

**現象（10-04 21:46～21:47，stage `2026.10.04-13`，總協調 Playwright 重現；15:38 Neal 首次遇到）：** 按「新對話」後，
畫面停在舊對話好幾秒、輸入框仍可打字；新對話切過來時，這段時間打的字和選的模型不見（字其實存到舊對話的草稿），
送出也沒有建立回合。

**原因：** 舊程式只靠`busy`擋輸入；新對話POST回來後的狀態讀取若被其他舊對話寫入搶先（`stateRevision`已變），
那次讀取會被丟棄，`busy`照樣解除，畫面回到舊對話且可編輯；之後才切到新對話，舊對話畫面上打的字就跟著留在舊對話。
離線替身已重現同一狀態（舊對話、`busy=false`）；stage當時的原始network未保存，觸發的那一筆寫入沒有逐筆核對。

**現在的行為：**

- 按下「新對話」當下，原對話的文字與模型存回原對話，畫面立刻換成空白的「正在開新對話…」：可以先打字、
  選模型，但送出、歷史對話切換、追問與附件都停用，不會在舊對話畫面打字或送出。
- 新對話一出現，剛才打的字和**明確選過**的模型直接成為它的草稿；沒選過就用新對話自己的預設模型。
- 狀態讀取被搶先時，以同一個新對話編號最多再讀兩次；仍沒切過去就明說「新對話已建立，但畫面還沒切過去」，
  請人從歷史對話打開，字保留、不會自動送出。開新對話失敗時回到原對話與原草稿，剛才打的字留著，
  下次開新對話或打開剛建立的對話時帶回。
- 訊息還在送出（含準備新環境）時「新對話」停用，切換不會把正在送的訊息無聲丟掉；
  送出在原對話中沒成功時，草稿保留並顯示沒送出的原因（原本的處理）。已知缺口（原有、本片未改）：送出途中從歷史對話
  或助理清單離開這段對話時，回來不會說明這次送出的結果；字保留在原對話，不會自動重送。

**Team 待辦分頁（140 次讀取）：** 不是每次渲染都重讀。後端`/attention`每次都從頭掃 Behavior 紀錄表這個分區的
所有資料列（每頁 20 列，各種紀錄都算，不只批准／提問），只回本 Team 未結束、未過期的批准／提問；工作台每 5 秒重掃一次（2026-10-05 起改 60 秒背景；2026-10-06 起有推送時只在事件時讀），
切換對話（含新對話）時待辦檢視跟著清空並立刻重掃，紀錄越多頁數越多。舊程式每讀一頁就重繪整個工作台，連帶重建模型選單並重設`model.value`（多餘）。現在只有該頁讓待辦清單
改變時才重繪，模型選單只在清單改變時重建、值不同才寫入。頁數由後端另一片處理：只讀等待期限內寫入的列，
見[Team 待處理的時間窗](../README.md#team-hitl-attention-window)；工作台不用改。

### Verified against（新對話切換）

- 程式基準：`main` `b3aeb01b`（與先前 `c54aa6a4` 之間只有文件變更）。只改前端`index.html`與 DOM 測試；沒有改 bridge、API、SDK 或權限。
- 已讀程式：`web_test_console/index.html` 的`run`、`readState`、`renderComposer`、`renderConversation`、`switchAgent`、
  `switchThread`、`sendChat`、`readTeamAttention`、`scheduleAutomaticEnvironment`；`web_test_console/native.py`
  composer（`can_new_conversation`）、`bridge.py` `new_conversation`；`web_test_console/native_hitl.py` `team_attention`、
  `shared_context/native_hitl_attention.py` `read_team_attention`、`shared_context/behavior_store.py` `read_record_page`。
- 實際驗證：`node --test tests/shared_context/test_web_test_console_workbench.cjs`（本機 Node 24，離線假 DOM）。
- 新增 5 個 DOM 測試，在原`index.html`上全部 FAIL、修正後 PASS：切換延遲期間打字與選模型、狀態讀取被搶先、
  開新對話失敗、送出中按新對話、空白 Team 分頁不改模型。原有 2 組「切換中輸入框停用」的斷言改成
  「不顯示舊草稿、不能送出」。完整 DOM 613 PASS。
- 未部署、未改`deploy/stage/release.json`；stage 驗收需 Neal 核准部署後再測。

<a id="system-prompt-view"></a>
## 查看系統提示（唯讀，CODE/offline）

「⋯ → 支援明細」的 **系統提示（唯讀）** 段落第一次展開時，自動讀一次目前選定助理
（2026-10-06 起不再放在 ⋯ 選單第一層；見[介面去工程化](#plain-main-screen)）
**開新對話時**會送進 SDK `system_message`（replace 模式）的全文，依順序分段顯示：

| 段落 | 來源 |
|---|---|
| FA 固定指示（例如 `fa-instruction-v4`、`fa-instruction-agent-role-v4`） | 版本常數；用模板保存的 `prefix_sha256` 核對 |
| 能力／工具說明段落 | 目前選定且仍可用的能力（`fa-capability-addendum-v1`） |
| 原生回合來源附註 | 模板有選 `fa-instruction-context-origin-v1` 時 |
| 角色段落標題＋Team 核准的角色說明原文 | 角色版本 head 的原文與 SHA-256（「＋」建立時填的內容） |

每段與全文都附 bytes 和 SHA-256，「下載文字」只下載後端給的全文。同事名冊、群組、工作提醒、
外掛工具（桌面／瀏覽器／Team MCP）說明與接續原工作說明是**每回合當下才加入**的段落，這裡只標出會插在
哪裡，不顯示也不猜內容：有角色說明的角色版本，前四種在角色說明之前、接續段落在最後；其他情況全部在最後。
工具定義另外送出，不在這段文字裡。

各回合的「本回合 Context 明細」另有 **查看這回合的系統提示**：後端只讀該回合原回條，依它保存的
profile、能力與角色原文重組，再用該回合保存的整段 `system_message` SHA-256 核對。

- 一致：顯示全文與「下載這回合的系統提示文字」。這回合沒選同 Team 協作時，Hosted 在名冊原本的位置加的那句固定說明
  （[PA-03](../README.md#pa-03-not-selected)）會列成「同事名冊說明」段落，一起用整段 hash 核對；加這句之前保存的回合不含它，照原樣核對。
- 不一致或沒保存：明說「無法重建」及原因（沒有組裝紀錄、這回合另有只存 hash 的當下段落或沒有同事／名冊讀不到時加的一句說明、
  段落 hash 不符、這版程式沒有該提示版本），不拿目前版本代替。只顯示能用該回合**自己保存的** hash
  核對的段落（固定指示用模板 `prefix_sha256`；能力、來源附註、角色用回合 assembly 的段落 hash；
  角色原文也用回條保存的版本 hash）；其餘段落只寫「無法重建」，當下段落只列 bytes 和 SHA-256。

全部是 GET：不改 selection、不啟動模型、不寫帳本或用量，也不新增權限；只有現有入口 Entra 身份的
Team 主人讀得到自己 Team 的助理。工作台只核對 bytes／SHA-256 與欄位一致，不自行組裝或補文字。
API 太舊（404）時顯示「後端還沒有提供」，不改用其他來源。

### Verified against（系統提示）

- 程式基準：`main` `00cd454d` 加本分支三個功能 commit 與 review 修正。
- 官方（查閱日期 2026-10-04）：Hosted agent 的行為在自己的程式裡，不像 prompt agent 在 Foundry 保存 instructions
  （[比較表](https://learn.microsoft.com/azure/foundry/how-to/develop/create-agent-visual-studio-code)、
  [Hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)）；固定 SDK 1.0.13
  的 `SystemMessageReplaceConfig` 由呼叫端提供整段文字
  （[session.py](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/session.py)），
  沒有讀回介面，所以由 API 用同一組組裝函式重組。
- 離線：真 native executor＋離線 SDK 替身，5 種 profile／角色／來源附註組合的重組全文與 SDK 實收
  `system_message` 逐 byte 相同；能力段落、只存 hash 的當下段落、舊回合、版本 hash 不符各有案例。
  signed API、bridge（竄改／舊 API／跨對話）與 DOM 3 案；完整 DOM 607 PASS。未部署、未改 `release.json`。

<a id="plain-approval-card"></a>
## 批准卡白話與收納（CODE/offline）

卡片主畫面顯示「等你決定（HH:mm 前有效）」和實際詢問理由：相同規則版本比對到ask-first時，
直接引用Team規則；`review_uncertain`說明系統不確定是否在原要求範圍；沒有原要求或判斷結果時，
用白話說明缺少資訊。規則版本改過不引用新版文字充當舊理由。完整日期／秒數與原JSON留在「詳細」，
真正的到期判斷、原B reference、權限與確認視窗都沒有改。

「以後允許這類」先展開可編輯的規則草稿，第二次按「確認規則並允許」才走原確認視窗與原writer。
（2026-10-07 起卡片按鈕不再跳確認視窗，「確認規則並允許」一按就送，見[卡片一按就送](#hitl-one-click)。）
建議只取原助理、命令／參數與位置，不自行概括成所有shell或聲稱任意命令唯讀；人可以直接修改。
過長的建議不截斷、不改成較寬的空泛規則，留空讓人寫原本就必要的範圍；人清空後不自動補回。
平常的登入／付款接手提醒、viewer入口、「不是效果已完成」與重讀按鈕收進「詳細」；
原review帶`takeover_required`／`human_takeover`時才將接手提醒放在主畫面，不另造客戶端權限分類器。

### Verified against（白話與草稿）

- 基準`6ebbd968`；前端HTML與`#612`的`630221ea`一致。白話／收納／選擇後展開的8個反例先FAIL。
- 完整DOM604 PASS，含原三選、B目標／pending_id、規則版本、原確認取消、期限、重讀、512字建議界線、
  人清空草稿保持；沒有新授權、SDK或後端流程。
- **15:38新對話首次清字：未重現、未宣稱已修。** 兩lane×Team空頁／B卡4案在未改草稿程式上，
  重播新對話POST/GET、Team回應未到即填字、之後4次5秒更新；追蹤textarea每次寫入，沒有暫時空值。
  總協調確認原network未保存，決定按未重現交付；本片只留回歸，不加猜測性的草稿覆寫或重跑live。
  （後續：21:47在stage -13重現，修正見[新對話切換延遲時保留草稿](#new-conversation-opening)。）
- 總協調另回報`#612`於stage `2026.10.04-6`、15:35已驗B卡掛A原回合，Neal本人允許後結果回A；
  這是既有版本的一次現場觀察，不是本片文案或清字問題的live驗收。

<a id="team-attention-inline"></a>
## Connect 卡：connector 未連線（C3，CODE/offline，未部署）

助理用到這位 Agent 還沒連線的 connector（或連線已被撤銷）時，待辦與原回合內多一張 Connect 卡
（`connect_required`，[後端契約](../README.md#connector-connect-card)）。

- 卡片顯示 connector 名稱、這是「這位助理」的連線（每個 Agent 各連一次）、registry 的資料範圍說明，以及
  「連線 {名稱}」外部連結（`target=_blank`、`rel=noopener noreferrer`）、「我連好了，繼續」與「先不用」。撤銷的卡沒有
  連線連結，改給服務的授權頁（`revoke_url`），並說明 FA 不會自動重試。
- consent URL 只來自這張卡自己的私有讀取 `GET /api/native-hitl/{thread|team}/consents/{consent_id}`
  （`native_consents.py`，回 readback 加 `connector`），只留在頁面記憶體；待辦清單、狀態讀取、journal 與 log 都沒有 URL。
- 按鈕：`POST /api/native-hitl/{thread|team}/consents`，body 只有 `consent` 參照與 `choice`（`resume`／`decline`）。
  request ID 由卡片與 owner 決定，所以連點、換按另一顆或重新整理都只回已存的原件，API 只收到一次；回覆遺失時只能
  `consent-reconcile` 核對原件。按下後卡片變成一行紀錄（「助理要連接 X／你按了「我連好了，繼續」」）。
- 等待文字照 #751：使用者自己的回合不顯示期限，排程與 Agent 間回合顯示期限。

## 全 Team 待辦與原回合內的批准／提問（CODE/offline）

「需要處理」沿H1的owner-only分頁讀取整個Team，包括工作台未建立本機thread的轉送回合。
每頁最多20筆原資料列；空頁有cursor仍接著讀，先讀到的卡可先顯示，讀完前不宣稱完整。
可見頁面沿60秒背景唯讀更新（2026-10-05前為5秒；背景讀取不顯示載入區塊，見[背景少讀](#no-jump-background-reads)）；不喚起新模型、不重送工作，也不由目錄／畫面推導新權限。
來源原件核清時，卡片掛在目前對話匹配的原RID下；B由A轉來時也可掛A原回合。
上方保留Team總覽與「查看原回合」，其他對話或來源未核清的卡留在總覽，不猜root。

卡片顯示原助理、原要求摘要或RID、操作／問題、期限及原審查理由；規則版本不同時明示不同，
不拿新版規則補造舊理由。批准三選仍是「允許這次／以後允許這類／拒絕」，不可用時說明原因，
完整JSON收在「詳細」。提問走原選項／freeform 的回答路徑（2026-10-07 起不再跳確認視窗），不會走批准writer。

人在A操作B卡時，HTTP的`X-Console-Agent`仍是B；Team-mode薄路由在B的原console／journal
核原Agent、Team、CID／RID及pending/question完整參照，再用原H1決定／回答端點。
沒有B本機thread時，不建立假的人類請求；原once ID、digest、deadline、ETag與失ACK只核回都保持。
未配置或不可用的B仍可列出提醒，但操作明示原入口不可用，不改送A或default Agent。
已讀到的控制觀察按目標Agent／原request ID合併；C的讀回或A的規則不會洗掉B的UNKNOWN核對入口。
即使Team待批卡已移除，現有原控制紀錄仍標明B、以B的原command ID核回，不重送決定。

### Verified against（Team與inline）

與5e6e分工：H1 API／wire／原root只讀投影由其API-only提交`8822cbc6`提供，本線整合console／DOM。
原3個DOM反例先FAIL；修後涵蓋A看B、B原pending_id/問題參照、同Team頁面跨空頁、未核來源不掛錯回合，
過期／無approve／錯call／失ACK、切Agent晚回覆及草稿保留。真signed API測試核B原journal而非A的寫入。
本機完整DOM566 PASS；H1 discovery／root／console／questions／remote／Agent相干Python148 PASS，
並通過禁止全部`copilot`匯入的API／console冷啟動。fixture前置錯誤另存，不列成產品RED。
合入`b8a6740b`一般換版入口／追加回覆主線後，完整DOM586 PASS、相干交會Python47 PASS；前批不回填。
審查補上跨Agent控制觀察的3個RED→GREEN（C讀回／A規則／同ID不同Agent），完整DOM589 PASS。
所有驗證均離線，不是stage/dev驗收；未部署，未改`release.json`，沒有新增IAM或SDK相依。

<a id="approval-peer-mcp-ui"></a>
## 批准卡、Agent 回覆與 MCP 設定（CODE/offline）

- 待批標題取原 pending RID 的已讀回訊息，不再用 thread 第一則標籤；缺原件時只顯原RID提示，不借用別則。
- 目前對話的待批卡自動純讀原操作一次；錯誤明示、人工可重讀，沒有自動決定。
  shell顯示完整command與位置，其他tool顯示名稱／目標；原JSON收進「詳細」。全部仍用textContent，
  原reference／digest、期限、三選與確認視窗不變。規則讀取期間不開放新增或輸入；原revision／ETag不重設。
- 原回合已有delegation時，沿它的原message_id純讀同Agent／CID中的返回RID；核來源peer與原continuation，
  不從平台角色文字猜來源，也不建立假的人類request。使用既有進度計時器等待返回（其他Agent的慢速讀取已於2026-10-05拿掉）；
  收到後立即顯示「某助理 回覆」與本助理的處理，按已記錄的turn number排序。原request／unknown及平台history來源不變。
- 已保存native回覆會查原圖片清單一次並自動顯示已核hash／長度的縮圖，點開大圖；空清單不插入空白提示。
  失敗明示且不自動重試，原手動reader、PNG限制及登入世代檢查保持。
- MCP connection只列支援的RemoteTool（固定SDK值含RemoteTool_Preview）／CustomKeys；API寫入也核類別。
  AppInsights、ContainerRegistry及其他非MCP類別不可選；多Team的secret限制與不傳secret的原規則不變。
- 「關閉不可用的 MCP」只改目前助理的未套用勾選，不動其他選項；仍要原inspect／確認／select才生效。
  不自動更新所有助理，也不改Team toolbox或取得新權限。

**B收件側限制（已由 [B 的收件對話](#agent-received-conversations) 補上）：** 已在工作台原CID中讀到的Agent request
會標「收到某助理的交辦」；B 自動建立之 CID 的入站新對話，現在由 B 自己的回條清單發現並以唯讀方式顯示。

### Verified against（批准／返回／MCP）

原pending標題、未自動讀卡、規則讀取窗口、返回觀察停止、native縮圖與MCP類別／清除入口均先有反例。
本機DOM與signed API／既有journal／外部I/O替身驗證，不是stage或真模型驗收；
原確認、同ID核回、不擴權與未完成狀態的回歸保持。固定SDK為azure-ai-projects 2.3.0，未升套件。
DOM完整545 PASS。五個相關Python模組118 PASS／3 FAIL；三案都是既有native錯誤測試仍期待`read`、
實際先遇`session_read`，在未修改的`f0b93134`快照同樣3 FAIL；未為此改SDK或弱化測試。
合入`083d0273`自動接續／批准新主線後，完整DOM551 PASS、相關Python119 PASS；
上述3個已證基線案例另列而未重跑，不把它們寫成通過。原字串／回條／模型用量均不回填。
官方用法沿[toolbox authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-authentication)，
FA不自行實作credential injection；connection分類是原薄API的選項／寫入檢查。

<a id="environment-notices"></a>
## 換環境與版本通知（ADR-0009 phase 2c，CODE/offline）

2026-10-06（P06）起工作台不顯示任何 [ADR-0009 §8](../../../docs/adr/0009-version-model-g-actual-version-idle-upgrade.md#8-使用者看到的通知)
通知；phase 2c 補回，原則是幾乎無感、少而誠實、不當成錯誤：

- **來源：** API 綁定列的通知槽（每個 Agent 一個，M4），隨主人自己的送出回應（`/submit`、換版後的同 RID 重送
  `submit-retry`）帶 `environment_notice`；API 在回應寫出後才清掉（[共用 README](../README.md#g-phase2c-clear)），所以同一則通知只來一次。
  bridge 把它投影到 task 的 `native_receipt.environment_notice`（讀取路徑上的「較早的版本接下的……」也一樣投影，之前沒投影）。
- **顯示：** 輸入框下方 `#environment-note`（`role="status"`，與「已換成某模型」同一排），不是錯誤區、不進聊天泡泡；
  顯示到這位助理下一則被接下、且沒帶通知的訊息為止（重新整理後不再顯示：API 已清掉，這是「看到一次」）。句子照 ADR §8 原文：
  `environment_updated`「助理已更新到新版本，工作檔也一起帶過來了。」；`environment_switched_without_files`
  「上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。」；`environment_source_retired`「上次換環境時，舊環境已失效或被平台刪除，沒有搬工作檔；之前 VM 裡的工作檔可能已經不在了。」；
  `platform_version_changed`「平台把助理的執行環境換成了另一個版本（vX→vY）；FA 已記錄，相容就照常執行。」；
  `environment_rolled_back`（phase 4d crash-loop 退回）「助理已退回先前的版本；剛才中斷的訊息請再送一次。」（Neal 2026-10-19
  決定）；帶 `without_files: true` 時（退回沒帶工作檔，M4 的 A 第 1 項）句後接不搬檔那句；
  `environment_upgrade_vetoed`（phase 4b-4，新版本 3 次沒通過健康檢查）「助理這次沒有更新，會繼續使用目前的版本。」（Neal 2026-10-19 決定，不帶版本號）
  （`platform_version_changed` 與「舊環境已失效」兩句仍待 Neal 確認）。「較早的版本接下的……」照舊在那則訊息的處理狀態裡顯示。
  **「有新版本 vN，會在助理閒置時更新。」**（phase 6a (c)，延後超過 6 小時；不是通知種類）：API 在回應的 `native_receipt.upgrade_waiting_version`
  帶版本號，bridge 原樣投影，同一個 `#environment-note` 顯示；console 的 API 請求在 `X-FA-Notice-Kinds: 2` 之外另帶 `X-FA-Upgrade-Waiting: 1`，沒有後者的舊 console 收不到也不會讓 API 清掉。
  槽裡有上面的通知時先送通知，這句留到下一則回應。
- **換版中：** 同 RID 自動重送期間，「助理正在換到新版本……完成後會自動送出」在自動重送開始 30 秒後
  （`SERVER_FOLLOWING_QUIET_MS`，ADR-0009 M4 的 A 第 4 項）才出現；短暫換版只看到訊息照常送出。
- **版本差異：** 沒有部署順序要求。bridge 的每個 API 請求帶 `X-FA-Notice-Kinds: 2`、`X-FA-Upgrade-Waiting: 1`、phase 4d 的
  `X-FA-Rollback-Notice: 1` 與 phase 4b-4 的 `X-FA-Upgrade-Vetoed-Notice: 1`（`adapter.py`），API 只對帶第一個的主人 submit 送換環境通知並清除，另帶第二個才送並清除 6 小時的「有新版本」，
  另帶第三個才送並清除「已退回」，另帶第四個才送並清除「沒有更新」；部署中 API 已更新、工作台還是舊 bridge 時，舊 bridge 不帶 header（或少帶後兩個），只會拿到它認得的通知，
  不會 502，它收不到的也不清（[共用 README](../README.md#g-phase2c-clear)）。工作台先上線、API 還是舊版時，舊 API 照樣認得
  `X-FA-Notice-Kinds: 2`、不理會另外三個 header，換環境通知照常顯示並清除。另外 bridge 解回條時，
  不認識的通知（更新的 API 的新種類或新欄位）直接略過，不回 502 `NATIVE_STATE_INVALID`；回條其他內容照舊嚴格核對。
  頁面也只顯示認識的種類。

## 環境自動接續（Neal 2026-10-04確認，CODE/offline）

> **通知部分：** 第 16 版 P06（`f56020a5`，2026-10-06）起不再顯示下方 ready 後的通知；phase 2c 起改由 API 通知槽送來、
> 在輸入框下方狀態列顯示（[換環境與版本通知](#environment-notices)），下方 ready 流程本身仍是歷史設計。

Neal **10:22:31（台灣）**問「這兩個功能應該是FA應該要幫user處理的? 使用者不需要處理?」，
於 **10:24:05** 回覆「對」。正常native畫面不再要求使用者核對環境或同意不搬檔；
新bridge固定回`environment_policy=fa-environment-system-policy-20261004`，HTML依server契約啟動，
不是客戶端自己取得授權或選擇可丟棄的資料。

畫面載入與送出前沿`environment-auto`（2026-10-05起送出前不再跑，見[一個請求](#one-request-send)），UNKNOWN先只讀原件，保同ID／body及原journal。
ready後只有當次真正按送出、且被換版拒絕未接單的那則訊息，才以同RID自送一次（2026-10-05起，見[一個請求](#one-request-send)）；單純載入不會送未要求送出的文字。
一般換版讀到合法metadata `needs_follow`，即使沒有草稿、沒有先前的接續回條、
`follow_up_blocker=null`，也會啟動同一政策入口。顯示與啟動共用目前環境快照，
不再只顯示「自動接續」卻沒有POST。只讀metadata本身仍不啟動模型或變更環境。
舊對話與空白新對話第一次載入都先純讀目標一次，即使只有上一版ready；
讀到`unverified`不推論需換版、不送POST，也不反覆重讀相同觀察範圍。
頁面自動接續取得ready後，另GET原thread的progress；只有server回覆可續聊才開放輸入，
不以環境ready推論原對話已核清。讀回期間核Agent、CID、RID、登入及觀察世代，晚到結果不解鎖別段對話。
較新的回覆／環境觀察會使舊GET作廢，舊finally也不能把新目標記成已處理；
既有UI寫入中的busy與threadLoading仍保留，不把已完成回合讀409改成accepted或繞過真的執行中工作。
同scope仍缺可續聊狀態時，作廢舊讀回後沿既有進度計時器補讀，即使Responses回合已completed；
保留新回覆、不重POST，真正讀取／登入拒絕仍依原暫停規則處理。
每批仍2秒／10次；達上限保持等待，既有progress讀回有新token或ready才喚醒下一批。
沒有進度顯示「等待原工作結束中」；HITL／提問原卡片仍由人處理，不跳過。
正常等待不顯示空的「環境詳細」；自動核對確實失敗並停止時，顯示錯誤原因及技術控制，
也保留server允許的「新對話」，不讓空原因封住所有入口。未知PUT／發布及checkpoint防線不變，
原未知資料不被清除；技術控制仍沿原路由核對，不授權重送工作。
技術入口重入既有CHECKING時，scan／accepted／admission／task的合法等待共用相同原因集合；
原confirm ID、body、來源／目標與每批10次仍須一致，不把task等待報成環境衝突。

ready後通知原句：「上次換環境時沒有搬工作檔，當時舊環境的檔案不會出現在現在的環境。」
沿#595的同Agent／目標／observer守衛，不讓舊成功蓋掉後來UNKNOWN。舊bridge無policy識別時維持相容UI。
來源／自動決定與限制見[Shared README](../README.md#automatic-environment-policy)。
已用原DOM runner覆蓋草稿附件、載入、HITL及批次喚醒；未修改主題、登入/卡片DOM或進行live操作。

### Verified against（一般換版入口修補）

- 基準`8c8a3951`，工作台程式與stage採用的`608b1e6c`相同。總協調回報stage
  `2026.10.04-4` A/B卡住；本線以原HTML重現：原completed對話無草稿、進度409後只GET環境，POST為0。
- 根因是啟動條件只認舊pending/UNKNOWN或錯誤碼，漏掉第一次`needs_follow`；
  另以真bridge/journal核對ready後仍需唯讀原對話，不由DOM替身假定`can_follow_up=true`。
- 修補後共用DOM **548 PASS**，包含兩lane無草稿真POST、ready後解鎖、舊metadata提示收尾、
  核對失敗仍有出口、登入/CID變更的晚到讀回、原HITL及一次送出。bridge兩案各有PASS，
  初次重開測試client的loop/離線socket守衛失敗另保留，不算產品或平台結果。
- 整合main `110cb60f`的批准卡／原回覆修補後，共用DOM **556 PASS**、直接相鄰bridge **2 PASS**；
  兩份來源與原回條保持，不重跑其他後端或SDK runtime。
- 13:23補充的空白新對話及兩軸發現的晚到觀察覆蓋皆另有先RED後GREEN；
  最後共用DOM **562 PASS**、bridge **3 PASS**，包含既有確認等待task時同body重入。
  metadata正向必核管理scope；後來收到不合法metadata也不能忽略後繼續送草稿。
- 同批另修[原確認的retarget進度](../README.md#omission-retarget-progress)，API／Hosted／bridge使用同一build；
  原安全界線與SDK版本不變。selector、release與部署均未改，未live複測。
- 最後兩軸又指出「作廢GET後Responses未補讀」與「CHECKING/task_active仍被UI拒絕」；
  兩者先RED再修，最後共用DOM **571 PASS**，含原計時器補讀及合法等待同ID／10次上限。

<a id="new-conversation-loading"></a>
## 新對話載入期間保護輸入（CODE/offline）

「新對話」POST完成不代表新composer已載入。原本在尾端GET `/api/state`前就解除busy，
此時若重繪（例如分頁恢復可見），舊輸入框會提早開放，之後又切到新草稿。
現在只對`conversations/new`保留原Agent的busy，直到狀態讀回結束；載入期間不接受輸入，
完成後才開啟新對話空白草稿。不搬移文字、不改server的RID，不把舊稿當作新訊息。
舊草稿仍在原key；GET失敗會解除busy並明示錯誤，不重送建立請求。切換Agent後，舊回應不影響新畫面。
若登入在等待GET時過期，收尾仍依目前state重繪該Agent的控制項，讓重新登入可用，不把登入狀態改回有效。

### Verified against（新對話載入）

- 基準`55ba2829`；核`run`／`setScopeBusy`／`renderComposer`與新對話按鈕。
- POST ACK後、GET尚未回來時觸發原visibility重繪，輸入提早啟用先FAIL；修後保持disabled。
- 完整DOM **537 PASS**，含新舊草稿隔離、GET失敗、登入過期收尾及POST／GET途中切換Agent，沒有submit／next重送。
- 這是總協調10:32新提供的「建立新對話」時序；不是回填下方較早同RID情境的未重現紀錄。未操作live。

<a id="no-files-ready-note"></a>
## 不搬檔 ready 的完成提示（CODE/offline）

> **通知已補回（phase 2c）：** 第 16 版 P06（`f56020a5`，2026-10-06）拿掉了這個 ready 完成提示；phase 2c 起「這次沒有搬工作檔」由 API 通知槽隨主人的送出回應送來、在輸入框下方狀態列顯示一次，見[換環境與版本通知](#environment-notices)。以下是當時設計的歷史紀錄。

原控制POST收到ready後，仍要等尾端GET composer完成才記通知，所以畫面會繼續顯示檢查中。
現在核對原Agent／thread／目標後，先記已確認的ready，立即顯示「這次沒有搬工作檔」；
通知留在控制面板外，面板收起也不會把文字一起藏掉。新操作或不同目標不沿用舊完成通知。
核對時保留已核目標並比對最新observation；沒有目標，或最新觀察指向另一版，不拿歷史ready提前報完成。
只是顯示已讀回的結果；沒有新輪詢、重送、批准、SDK或後端格式改動。

`Journal.begin`在操作開始時先持久化`UNKNOWN / RECONCILE_REQUIRED`，完成後才寫CHECKING或CONFIRMED。
這是預期的在途狀態；原POST仍在途時，既有UI顯示等待。失ACK／重開頁面後的真正UNKNOWN仍保留，
不得因這個說明改成成功或允許重送；本片只補回歸，不重寫journal。

**較早同RID的首次輸入回報：當批未重現、未宣稱已修。** B原件是同thread／同舊completed RID／原CID、
`editable=false`、`can_follow_up=true`；不是換成新draft RID。該假設的補丁已撤除。
用已核SHA的419e HTML及B去敏req24～29，在獨立瀏覽器攔截全部網路後回放，第一次fill仍保留。
依總協調決定只補同RID草稿回歸；若stage再出現，當場取前端狀態再查，不加推測性的覆寫補丁。

### Verified against（ready提示與過渡狀態）

- 基準`e47f4f5e`；已核`run`、`continueWithoutFiles`、`rememberFileOmission`、`renderEnvironment`、
  `Journal.begin/finish`與`NativeInstructionControl.environment_preparation`。
- ready提示被慢GET延後先FAIL；DOM核通知不因收面板消失、不同目標不誤報、原UNKNOWN及同RID輸入保持。
- `test_web_test_console_workbench.cjs`完整 **527 PASS**；原523批保留，另鎖reconcile新／缺／同目標（含尚未ready）。
  不相干的新RID實驗已撤，原試驗輸出另存不回填。
- 整合`987ce11c`來源漂移出口後 **532 PASS**；同一目標的新pending／reader缺失／真正UNKNOWN不沿用前次完成提示。
- 瀏覽器回放只用既有去敏原件，所有context／測試分頁均關閉；未讀寫live或重跑原回合。

## 原來源漂移且已有搬檔 staging（CODE/offline）

「核對原新環境」仍只核目的端、必要的原staging及source metadata，不重新invoke來源或續搬。
server回`pending/source_version_changed`時，原UNKNOWN保持；即使有manifest或先前timeout，
畫面也顯示「檢查不搬檔接續」，隱藏正常續搬。這只開server預檢，不直接授權丟檔或送草稿。
已保存確認的readback可附固定類別`files_confirmation_reason`；API會核原理由而不對同ID重新分類，
bridge仍用原確認／草稿與既有啟用控制，不由UI解讀該類別擴權。

真人仍須確認原警告；server只允許已確認staging前綴、無未知PUT／發布／active工作時不搬檔。
取消不寫入；同意後原Files材料保留但不發布，新ready明示`files_migrated=false`。
草稿、檔案／圖片、Agent/CID/login世代、原once及同確認ID核回不變；
不搬檔成功後也不自動重送已POST或未知的對話。

### Verified against（staging漂移）

基準`e47f4f5e`；共用HTML `renderEnvironment`只依server觀察顯示選項，
bridge原environment UNKNOWN讀回與兩個DOM取消／一次確認案例，均保持原草稿附件、不觸發一般follow。
後端條件與未完成的正常保檔路徑見[Shared README](../README.md#source-drift-staging-exit)。
未更動既有Clawpilot主題、設定／HITL DOM、`#588`完成回覆reader或發布流程；未操作8765。

<a id="native-completed-reply"></a>
## Native 完成回合自動讀回覆（CODE/offline）

原本只有選單會GET `/reply`；進度回completed不等於已載入文字，Responses也會停止進度輪詢。
現在目前對話最後一個native回合已completed、沒有cached reply，且server允許`read-reply`時，
沿原Agent／RID自動純GET一次；頁面重讀或完成進度都走同一接點，不靠繼續輪詢取得文字。
不讀背景Agent或整頁歷史，不送submit／收件確認，也不重新執行回合。

自動與手動沿同一reader；同時只讀一次，自動失敗明示錯誤，後續由原按鈕手動重讀。
自動讀取只在可見時進行；若在背景完成，切回分頁沿原visibilitychange重繪補讀，不新增輪詢。
登入、Agent或thread改變後不採舊回應；成功時更新畫面世代，晚到進度不能抹掉剛讀回的答案。
未送草稿保持，SDK、後端與stage發布流程不變。

### Verified against（完成回覆）

- 基準`419e044b`；已核共用HTML的`render`、`scheduleProgress`、`updateProgress`、原fetch handler。
- 兩lane完成但沒有`/reply`各先FAIL；晚到進度與thread切換反例也先FAIL。
- `test_web_test_console_workbench.cjs`完整 **516 PASS**；原515批保留，另加背景完成後切回分頁反例（先FAIL）。
  含自動一次、失敗後手動重讀、晚到進度、Agent／thread／登入切換與草稿保留；
  只有合成DOM，未碰dev 8765或live回合。

## Receipt 列頁拒絕的去敏回應（CODE/offline）

`files_check`新增可省略的`backend_status`與固定白名單`backend_code`，
由Hosted固定SDK錯誤取出並穿過API／bridge嚴格DTO；未知碼為`UNCLASSIFIED`。
不顯示或傳遞原錯誤message／param／資料key／游標；不把新status當作可自動重送的許可。
既有scan_pending、CHECKING、UNKNOWN與真人確認流程不變。

### Verified against（backend status／code）

真SDK error extractor與API log／bridge讀回測試確認欄位保留；非法code、字串或布林HTTP狀態拒絕，
舊view未帶欄位仍相容。來源與限制見[Shared README](../README.md#receipt-page-rejection)。
外部probe已取消，現場診斷由總協調在同build的A原入口取得，不用其他身分替代。

## 單頁逾時與原確認重入（CODE/offline）

不搬檔check單頁首次本地deadline仍回未完成進度，保同頁／同確認ID並至多再讀一次；
UI明示逾時與重讀，不宣稱讀成功。第二次或backend失敗停下，保原件與草稿，
仍沿每2秒、含首次最多10次的總窗口；千筆/百筆一頁加timeout可由下一次人工續做完成剩餘頁。

依本次批准，「核對原新環境」對原UNKNOWN確認先讀遠端，再同ID/body冪等接續，
由Hosted create-only／digest/CAS保證一次生效。bridge只新增`files-omission-result-*`結果，
不覆寫原UNKNOWN；核回CHECKING後可沿同一窗口續做，已ready才刷新composer，不重送原訊息。
新目標版本則再問一次真人、產生新確認ID，不把103舊確認當成104授權，也不讓103的UNKNOWN擋104預檢。

### Verified against（原ID與新目標）

真API/Hosted測試覆蓋缺原件／已存／不同digest／晚到並行；bridge和DOM覆蓋原UNKNOWN只追加結果、
同target只核回續做而不重問、新target一次新警告及新ID，附件／草稿／世代隔離保持。
`files_check.failure_kind`分本地deadline和backend/transport；原503耗時不能外推為同一後端原因。
詳見[Shared README](../README.md#receipt-list-pages)，未操作dev／stage。

## 同意後仍在檢查的續做（CODE/offline）

metadata-only預覽先取得真人同意、再開始掃歷史時，未完成不再當409拒絕。
API以`files_confirmation_pending`精確指出這次確認尚未dispatch，bridge在原once journal記`CHECKING`，
保原command／RID；UI接續同body、每2秒一次，preview與同意後CHECKING共用含首次最多10次窗口。
此狀態不是遠端確認已保存，不回ready；每次呼叫若失ACK則UNKNOWN，立即停止、不重POST。

上限後按「繼續已同意的檢查」仍沿原同意，不重問警告或換ID；其他Agent／CID／登入／草稿／
target／confirmation或follow識別變化會停。確認已保存但follow還scan_pending時，只續原follow，
不再呼confirmation；剩餘次數不重置，原文字／附件與未知紀錄保持。
儲存503以safe`stage/key_category`顯示是哪類讀取失敗，不猜完整key，也不顯示上游私有訊息。

### Verified against（同意後CHECKING）

真bridge/once與DOM先RED，核原body/RID跨CHECKING、一次warning、前後共用10次、
人工續做不重問、真失ACK不重送，以及已確認後只續follow。後端另有300 RID完整讀取測試，
不以UI的pending當server idle或完成授權。詳見[Shared README](../README.md#no-files-large-history)。

## Native routine／原工作提醒（CODE/offline，預設關閉）

`FA_NATIVE_ROUTINES_ENABLED=1`時，原progress讀取會沿root API查原CID的提醒／排程回條，再核Agent／CID／RID。
工作台不持有producer權限、不呼`trigger`／`due`，不建立排程或另開SDK；讀不到時顯示未確認，不換ID。
active與UNKNOWN的排程原件不必等completed history才可見；`routine_text`標成「排程觸發（不是你的新訊息）」，
不放進真人`instruction`，也沒有submit retry／reconcile按鈕。原owner可沿既有入口追加／取消。

提醒沿`state.composer.attention`，只替**同原item**加到期註記；HITL／question原參照、writer、once與草稿保持。
沒有卡片的未結工作只在同一「需要處理」區顯示原RID提示，不另造批准卡。
回答／到期後原卡由既有reader移除；取消／完成不再提醒。未啟用或未登入時沒有新讀取或提示。
本機與remote仍共用原HTML／Clawpilot主題；沒有email、Teams、OS推播或部署變更。
單一DST異常／暫未看見N1回條只列局部`issues`；其他原工作和卡片照常可見，不把未確認改判未接單或重送。
取消routine仍可在owner歷史查看，但不是新的待辦提示。

### Verified against（routines UI）

初始`a229be94`、P2修後`1679dede`、正式squash main `f7a45f20`（tree核同）；`NativeRoutineControl`、`NativeInstructionControl`及共用HTML；
`test_web_test_console_routines.py`真signed bridge與API、`test_web_test_console_workbench.cjs`完整485 PASS。
修後連最新main的完整DOM494 PASS；局部issues不遮原attention，舊結果仍保留。
source／callback／模型I/O的層級與原FAIL另見[評測](../../../docs/agent-runtime-evaluation-baseline.md#native-routines-evaluation)；未連現場8765／stage。

## 舊不搬檔／前端環境接續紀錄（已由 P06 取代）

> 第 16 版 P06 起，工作台不再提供 `/environment-without-files*`、「不搬檔接續」、`environment-prepare` 或 `environment-auto` 的前端流程；由[換版由伺服器處理](#server-following)取代。以下舊設計不再是現行流程，保留詳細歷史請看 Git 紀錄與 Shared README 的舊結果段落；現行 Hosted 依據見 [`docs/hosted-cold-start-latency-research.md`](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)。

## 舊 Hosted 前端接續細節（已由 P06 取代）

> 第 16 版 P06 起，送出前閒置跟版、前端 `followNativeEnvironment`、`environment-auto`、不搬檔確認、`NATIVE_SESSION_CHANGED` 指向「在新環境接續」等流程都不是現行工作台行為；現行流程見[換版由伺服器處理](#server-following)。Hosted 官方依據：session 綁定建立時的 agent version，selector 換版不遷移既有 session（[`docs/hosted-cold-start-latency-research.md`](../../../docs/hosted-cold-start-latency-research.md#換版對既有-session-的影響)）。

舊段落只作歷史紀錄；本 PR 不再列出 `/environment-without-files*`、`environment-prepare`、`environment-reconcile` 或「在新環境接續／檢查不搬檔接續」作為現行操作。

## 設定頁（S1，LOCAL，未部署）

> 2026-10-08 起入口與分頁已分拆（助理的設定在助理清單的 ⚙，Team 的在左下 ⚙「Agent Team 設定」，頂端下拉拿掉），見[設定分拆](#settings-split)；本段只作歷史紀錄。

左下帳號區旁的齒輪 →「設定」會整頁蓋住聊天區，8765 和 stage 遠端共用同一份頁面。分頁有：已安裝、Skills、MCP、Plugins、批准規則。
遠端模式的齒輪選單另有「重新登入」，登入後 `#login` 會隱藏，這裡仍能找到。
總協調 2026-10-03 14:53 核准 S1；Skills 審核（S2）和 MCP 新增（S3）另做。

- **只改一位助理的新對話設定**：預設是左側目前選的助理；多 Agent 時可以在設定頁上方的選單直接切換，切換會沿用既有的 `switchAgent`，並重新讀取。
  - 讀取沿用既有 `GET /api/agent-template`、`/options`、`/selection`。
  - 寫入沿用既有 `POST /api/agent-template/inspect` → `select`，結果不明時只用 `selection-reconcile` 核對。
  - `bridge.py` 和 API 都沒有新增路由或權限，`Behavior.Approve` 等所有判斷仍在 API。
- **開關只是草稿**：底部會列出要開啟和關閉的項目。按「套用到新對話」後，先唯讀比對（inspect）取得模板 hash，再跳確認視窗，確認後才送一次 select。
  - 編號是新的 `ui-settings-…`，`expected_selection_id` 用剛讀到的目前設定。
  - 送出前若草稿或助理改變，或在確認視窗取消，就什麼都不送。送出中重複點擊也不會再送。
  - 結果不明時保留原紀錄，畫面只提供「核對原請求」，不換編號重送。之後重新讀取目前設定；不管成功或失敗，都不從畫面推斷結果。
  - 只影響之後的新對話。舊對話、已開始的工作和既有權限都不變。
- **各分頁內容**：
  - **Skills**：Team 已批准、目前生效的 skill。開啟 skill 時會自動帶上「讀取已批准的 skill」（`fa-skills-v1`）；只要還有 skill 或 skill 附檔開著，這一項就不能關。批准、停用和新增 skill 排在下一片（S2）。
  - **MCP**：catalog 裡的 MCP 項目（例如 toolbox 的 WebIQ）。只能對這位助理開或關；新增或修改 MCP 要找部署線，金鑰不經過這個頁面。
  - **Plugins**：FA 內建能力（kind `fa-builtin`，對照 Copilot 的 Built-in）。只能開或關，不能在這裡新增或移除。
  - **已安裝**：目前新對話設定裡的項目，可以依 Skills／MCP／Plugins 篩選，也可以依名稱或工具搜尋（只在畫面上篩選，不呼叫 API）。
  - 目前環境沒有提供的項目只能關閉。角色版本不能組合能力時，開啟會被擋下並說明原因。數量上限沿用伺服器的規定（skill 4 個、能力 12 項），最後仍由伺服器判斷。
  - 已安裝項目的版本或 digest 若和目前提供的不同，草稿會改用目前提供的參照（後端會拒絕過期的 digest），畫面顯示「套用後改用目前批准的版本」，確認視窗也會列出。已安裝、但目前環境已不提供的**能力**只要還開著，「套用」就會停用；MCP可用「關閉不可用的 MCP」一次調整未套用勾選，仍走原核對及確認。已安裝的 skill 不在此列：選項清單只列最新版本已生效的 skill，所以有新版本在等批准的 skill 不會出現在清單裡，但舊版本仍有效，照樣可以套用；最後仍由後端核對。
  - 設定頁關著時切換助理或登出，進行中的讀取或送出會作廢，回到設定頁時重新讀取，不會卡在「忙碌」。原送出紀錄仍留在 journal，可以核對。
- **批准規則**：原本聊天頁上的 `#hitl-rules-panel` 整塊搬到這個分頁，ID、函式、API、revision 和 owner＋`Behavior.Approve` 都不變，「尚未讀取，不以空清單當成沒有規則」也照舊。聊天選單的「Team 批准規則／原操作紀錄」改成直接開到這個分頁。
  - 「需要處理」裡的批准卡和問題卡仍留在聊天頁。
- **Esc** 會關閉設定頁，草稿留在記憶體；重新讀取時會以伺服器的目前設定為準。
- **後端在寫入前就拒絕的選擇（2026-10-03 窄修）**：
  - 適用的錯誤碼是 `PROFILE_SELECTION_CONFLICT`／`SOURCES_CHANGED`／`DENIED`／`INVALID`／`UNSUPPORTED`，而且上游狀態是 403、409 或 422。API 的 transaction 會先查原回條，`decide` 只執行一次，所以這些錯誤確定沒有寫入。
  - 新的寫入在 journal 記成 `REJECTED`；舊 journal 裡同樣條件的 `UNKNOWN`，在讀取時改顯示 `REJECTED`，但不改寫 journal。
  - 設定頁不再為這類紀錄顯示「未確認／核對原請求」，畫面改說「…這次沒有套用。已讀回最新設定，請確認後重新選擇。」，開關也確實已重設成後端目前的值。支援明細裡顯示「沒有生效；不需要核對」，也不提供核對按鈕。
  - 通用的 409／412 `RESULT_UNKNOWN`、`BACKEND_OFFLINE`、5xx 仍維持 UNKNOWN，照舊只核對原請求。同一意圖不重送的規則不變。
  - **2026-10-06 補修（v16 dev）**：上游狀態是 400／401／403／404／413／415／422 時，不論 adapter 留下哪個碼（例如把清單外的碼包成
    `BACKEND_UNAVAILABLE`），也一律算寫入前拒絕（`PRE_WRITE_STATUSES`）。`/select` 只在唯一一次 control transaction 提交前回這些狀態
    （登入、body、權限與選擇檢查）；提交後只剩組回應，失敗只會是 5xx。409／412（可能是同一 RID 的提交競態）、429、5xx 照舊 UNKNOWN。
    起因：dev journal 一筆 10-01 的選擇得到 422，碼被包成 `BACKEND_UNAVAILABLE` 而記成 UNKNOWN；API 沒有這筆資料，reconcile 只能回
    503 `PROFILE_SELECTION_UNKNOWN`，永遠結不了案。讀取時改判 `REJECTED`，不改寫 journal、不重送。

### Verified against（設定頁）

- `tests/shared_context/test_web_test_console_workbench.cjs` 新增 9 案：
  - 分頁和搜尋；
  - 草稿只改 skills／capabilities，其他欄位逐字不變，並自動帶上 `fa-skills-v1`；
  - inspect 後只送一次 select，雙擊不重送；
  - 取消或草稿改變就不送；結果不明只核對原編號；
  - 目前環境沒有的項目只能關閉，角色版本不能組合時擋下；
  - 規則區塊在設定頁，批准卡和問題卡留在聊天頁；
  - 設定頁的 Agent 選單，以及遠端選單裡的「重新登入」；
  - 已安裝項目改用目前提供的版本，已撤下的能力擋住套用；有新版本在等批准的 skill 仍可套用；
  - 設定頁關著時換助理，進行中的工作會作廢，不會卡住。
- Node 全檔 409／409 通過；`test_web_test_console_hitl.py`（32）、`_templates`、主檔與 remote 共 146 通過。
- HITL 測試需要固定版本的 tokenizer 快取（`TIKTOKEN_CACHE_DIR`）。

## 設定頁 S2：skill 審核（Q1、Q3，LOCAL，未部署）

> 2026-10-08 起 skill 審核（含 Q4 自己寫草稿）在左下 ⚙「Agent Team 設定」→ Team Skills，見[設定分拆](#settings-split)。

設定頁的 Skills 分頁上方加了「skill 審核（整個 Team）」。owner 可以在這裡查看內容、批准、拒絕、停用，或用新 skill 取代舊的。API 端走 behavior API 的 `/team-skills/v1`（見[共用 README](../README.md#team-owner-skill-review-behavior-apilocal未部署)），沿用既有 M3 的資料、版本和決定規則；沒有新增 IAM，也沒有刪除。

- **網頁代理**（`team_skills.py`）：
  - `GET /api/team-skills/reviews`、`/skills/{artifact}`、`/versions/{version_id}`（內容會核對 SHA256 和 UTF-8）、`/resources/{name}`。
  - `POST /api/team-skills/skills/{artifact}/decisions|deactivations`，body 上限 2 KiB；bridge 只放行這兩條路徑，CSRF 和 Origin 規則不變。送出前先檢查 `Behavior.Approve`，沒有就直接回 `TEAM_SKILLS_OWNER_ONLY`，不往上游送。
  - 讀到的 ETag 會用 `If-Match` 帶給 API。`adapter.send` 只允許這組路由帶 `If-Match`，也只允許審核清單帶 `after`。
- **畫面**：
  - 狀態分為待審核、生效中、已拒絕、已停用、上傳未完成。
  - 必須先按「查看內容」，看到用途、正文和附檔，才能批准或拒絕。
  - 每個決定都有一個頁面產生的固定 `decision_id`，確認後只送一次。
  - 失敗或回應不明時，會先重新讀取這份 skill：已經記錄就顯示「決定已記錄」；還沒有時，使用者確認後才用**同一個** `decision_id` 重送（M3 對同一編號只記一次）。不換編號重送。
  - 頁面重新整理後就沒有原編號了；這時再送一次，舊版本早已有決定的話，會被 M3 以「已決定／目前版本已改變」拒絕，不會變成兩個決定。
- **取代（Q3）**：在待審核的 skill 上選「取代：舊 skill」→「批准並取代」。
  - 依序執行：① 批准新 skill；② 停用舊 skill 目前的版本；③ 重新讀取這位助理的新對話設定，把草稿裡的舊 skill 換成新的，自動帶出同一個「套用到新對話」確認視窗。
  - 每一步都只送一次；任何一步失敗就停下，畫面提供「繼續取代」，沿用原本的決定編號接著做。
  - 這位助理原本沒有選用舊 skill 時，不會自動開啟新的，只提示使用者自行開啟。
- 審核狀態會隨換助理或登出清掉，避免舊回應寫到新畫面。

### Verified against（S2）

- `tests/shared_context/test_web_test_console_team_skills.py`：網頁端 `TeamSkills` 經 MockTransport 轉送到真的 `/team-skills/v1` app（`test_team_skills_api.py` 的 fixture）。涵蓋：
  - 讀取清單、版本內容和附檔；
  - 批准一次，同一編號再送一次時回原紀錄；
  - 換了編號再批准會被 `M3_ALREADY_DECIDED` 拒絕；
  - 停用；
  - 只有讀取權限時不送出；
  - `If-Match` 和 `after` 只限這組路由；
  - bridge 的 CSRF、query 拒絕和 2 KiB 上限。
- `test_web_test_console_workbench.cjs` 新增 4 案：
  - 先查看內容才能決定，固定編號只送一次；
  - 失敗後先讀回核對，確認後才用同一編號重送；
  - 停用；
  - 取代的三步流程，最後只送一次選版。

## 設定頁 Q4：owner 自己新增 skill（CODE/offline，未部署）

總協調 2026-10-03 17:20 代 Neal 定案「直接做」（可被推翻）。Skills 分頁的「skill 審核」區多了「新增 skill（自己寫一份草稿）」：

- **欄位**：名稱（一行、64 字內）、用途（一行、512 bytes 內）、SKILL.md 本文（8 KiB 內），以及選填的附檔（.md／.txt／.json 純文字，最多 4 個、每個 8 KiB 內（中文約 2700 字），檔名只用英數和 `. _ -`）。這些限制都沿用 Agent 起草 skill（SK-07 #516）的既有規則；名稱上限 64 字，是為了讓 4 份 skill 仍放得進助理的 skill 目錄。
- **送出**：確認後只送一次 `POST /api/team-skills/drafts`（body 上限 128 KiB，和 API 相同；bridge 只多放行這一條路徑，CSRF 和 Origin 規則不變）。網頁先用與 API 相同的 `OwnerSkillDraftInput` 檢查格式，不合格直接回 `TEAM_SKILL_DRAFT_INVALID`，不往上游送；沒有 `Behavior.Approve` 也不送。
- **結果**：
  - API 建立的是**未批准草稿**，起草者標成「你（Team 主人）」；它出現在審核清單，畫面會自動打開它，讓你查看後批准。
  - 批准仍走原本的 S2 流程，所以「新增 skill」本身不會讓任何東西生效。
  - 要修改既有 skill 時，寫一份新的再用「批准並取代」換掉（Q3）；不做原地編輯。
- **只送一次**：草稿編號由「名稱＋用途＋本文＋附檔」推導，相同內容永遠對到同一份草稿。
  - 回應不明（`SKILL_DRAFT_OUTCOME_UNKNOWN`、網路中斷）時，表單內容保留；再按一次送的是一模一樣的內容，只會補完原草稿，不會另建。
  - 相同內容先前已建立過（不論已拒絕、生效中或已停用）時，回原紀錄並提示「沒有另建」。
  - 改了任何字就是另一份新草稿。

### Verified against（Q4）

- `test_team_skills_api.py` 新增：
  - 建立未批准草稿，相同內容再送回原紀錄；
  - 附檔可讀，批准後 Agent 的 skill 目錄看到 owner 寫的名稱和用途；
  - 改內容就是新草稿；
  - 只有 owner 加 `Behavior.Approve` 能建立；
  - Agent 不能接手 owner 草稿，owner 也不能冒用 Agent 的來源；
  - 9 種格式錯誤在寫入前就拒絕；
  - blob 寫入後回應遺失：先回 503，再送同內容會補完同一個版本；
  - blob 根本沒寫進去（本文或附檔）：先回 503，再送同內容時，API 用同一份內容補寫一次 blob 再完成同一個版本（儲存區只會新建，不會覆寫或另建）。
- `test_web_test_console_team_skills.py` 新增：
  - 經真 API 建立後，再送一次仍是同一份；
  - 格式錯誤和只有讀取權限時不送出；
  - bridge 的 128 KiB 上限和 CSRF。
- `test_web_test_console_workbench.cjs` 新增 3 案：
  - 確認後只送一次，建立後打開草稿，不會自動批准；
  - 回應不明時保留表單，重送內容逐字相同；
  - 欄位和附檔檢查（附檔上限是 8192 UTF-8 bytes，不是字數），取消確認時不送出。

## 設定頁 Team MCP：直接操作官方 Toolbox（CODE/offline，預設關閉）

<a id="team-mcp-settings"></a>

> 2026-10-08 起 Team MCP 在左下 ⚙「Agent Team 設定」→ Team MCP，見[設定分拆](#settings-split)。

2026-10-06 Neal「改走官方」：設定頁 → FA API 薄轉送 → 官方 Toolbox REST（API 端見[共用 README](../README.md#team-mcp-official-toolbox)）。
MCP 分頁上方的「Team MCP（整個 Team，只有 Team 主人看得到）」直接反映 Team toolbox 的預設版本；FA 不再保存自己的
revision 或 pending。API 沒開（`FA_TEAM_MCP_ENABLED`）時只說明「這個環境還沒開放」，不顯示表單、不送寫入。

- **只有 owner 看得到**：讀取回 `TEAM_MCP_OWNER_ONLY` 時整區隱藏、不重複讀；`can_manage`（目前登入有 `Behavior.Approve`）
  為 false 時只顯示清單，不顯示表單與修改／停用按鈕。入口本來就在設定頁，不在聊天主畫面。
- **新增／修改**：名稱、https 網址、驗證用的 connection、允許的工具，以及「呼叫前要不要先問你」：
  `每次呼叫都要我批准`（`require_approval=always`，預設）或 `不用批准，直接執行`（`never`）。這個設定就是批准卡的唯一來源。
- **connection**：可選既有的，或在 `can_create_connection` 時選「建立新的 connection（輸入金鑰）」，填標頭（預設
  `x-apikey`）與金鑰。金鑰欄位是 password，按確認後立即清空；金鑰只放在送出的那一次 body，由 API 建到 Foundry。
  確認視窗、提示與結果都不顯示金鑰。多 Team 環境只列出 Team 自己的帶金鑰 connection。
  標頭填 `x-feed-url` 時是接[讀 feed 的 MCP](../README.md#feed-reader-mcp)：金鑰欄位貼完整 https feed 網址，頁面送出前先檢查格式
  （`TEAM_MCP_FEED_URL_INVALID`），API 另加自己的簽章標頭；環境沒設簽章金鑰時回 `TEAM_MCP_FEED_UNAVAILABLE`。
- **停用**：確認視窗寫明「停用後，所有對話（含進行中的）從下一回合起都不能再用這個工具；舊紀錄保留，不重送」。
  Team 最後一個 MCP 的「停用」不可按，並說明「先新增另一個 MCP，再停用這個」（toolbox 版本至少要有一個工具，
  Team toolbox 不刪除）。toolbox 裡不是這裡管理的工具會提示數量，不顯示也不修改。
- **只送一次**：每個變更由頁面產生固定的 `request_id`，帶上讀到的 `default_version`；確認後只送一次
  `POST /api/team-mcp/changes`。結果不明（`TEAM_MCP_OUTCOME_UNKNOWN`、離線或回應格式不符）時顯示「用原編號重送」，
  按下後送出完全相同的 body；未確認前不能做新的變更。完成後重讀助理設定，新的 MCP 出現在下方開關裡。
- **網頁代理**（`team_mcp.py`）：路由 `GET /api/team-mcp/servers`、`/connections`；`POST /api/team-mcp/changes`
  （上限 16 KiB）。`/pending/resume` 已移除。bridge 只放行這一條 POST，CSRF 與 Origin 規則不變；
  代理不寫 journal、不記 body。送出前檢查 `Behavior.Approve` 並用和 API 相同的 `TeamMcpChange` 檢查格式；
  金鑰在檢查後只寫回送出的 body。回應必須是同一個 `request_id` 且狀態為 applied 才算完成。
- **批准卡**：Toolbox 工具（review `source=toolbox`）的卡片在主畫面顯示「要使用 Toolbox 工具：名稱」與完整參數 JSON，
  理由是「在 Foundry 設定為每次呼叫都要你批准」，只有「允許這次／拒絕」，確認視窗也列出參數。其他工具的卡片不變。
  （2026-10-07 起不再跳確認視窗，參數已在卡片上；見[卡片一按就送](#hitl-one-click)。）
  「批准規則」分頁註明這些規則不管 Toolbox 裡的 MCP。
  C5（Teams 寫入）：參數有 `parentUrl`／`entityUrl`／`actionUrl`／`functionUrl` 時，先各列一行「目標（路徑／ID，
  未換成名稱）」；有 `jsonBody` 時列「完整內容」（縮排後的 JSON，不截斷）。結果不明（`effect_unknown`）的卡寫明
  結果不確定、系統沒有重送，沒有重試按鈕。見 [Shared Context README](../README.md#connector-teams-writes)。

### Verified against（Team MCP 工作台）

- `test_web_test_console_workbench.cjs`：
  - 新增：預設每次批准，確認後只送一次，帶讀到的 `default_version`，完成後重讀助理設定；
  - 停用結果不明：只出現「用原編號重送」，重送的是完全相同的 body，未確認前不能做新變更；
  - 最後一個不能停用並說明替代做法；另有不管理的工具時可以停用；
  - 新 connection 的金鑰只在送出的 body，送出後欄位清空，畫面不顯示；
  - 環境沒開放：不顯示表單、不送出；非 owner 整區隱藏；唯讀登入不顯示表單；
  - 修改：保留不在清單裡的原 connection 與原批准設定；
  - Toolbox 批准卡：名稱與完整參數、無「以後允許這類」、只送一次 `allow_once`。
  - 完整 DOM 620 PASS。
- `test_web_test_console_team_mcp.py`：接真 `/team-mcp/v1` 路由與假 Foundry，同編號重送不建第二版、金鑰只送一次、
  唯讀與格式錯誤不送出、bridge 只放行 changes、無 resume／DELETE。

## SP-22 文字群組（LOCAL，預設關閉）

工作台、API及Hosted均須明設`FA_NATIVE_GROUPS_ENABLED=1`；預設不顯示群組入口，不額外查後端。
「群組討論（文字）」開獨立panel，保留原一對一草稿、工具列、append、圖片及remote設定。
只收原問題、ready同Team成員及主持助理，首版群組不支援附圖；群組保存和明示啟動分開。
顯示原公共帖／作者／輪次、10帖及3輪上限，保留final名額；不把群組本文當platform history或批准。

開題與start沿原SQLite FULL／remote after-commit保存once意圖，回應未知先讀原ID。
開題只有exact NOT_FOUND才准同ID／原body由真人明示續存；server的create-only不會多建群組。
續存明確拒絕另存觀察、解除新開題阻塞；原UNKNOWN不回填，5xx／未知仍保原編號。
原group-start UNKNOWN不回填為從未送出或成功；已有原human accepted可走既有N6有限核回，
不建立另一個retry loop。未確認回條清楚提示，不能由「已存group」推論模型已接手。
群組原回合經API owner activity核過Agent／CID／RID後，才可開在原native對話中；
沿既有取消、append與同一個`composer.attention`／H3卡片處理，沒有新批准type或writer。
從group panel讀取／切換晚到的舊登入或Agent回應不發布，文字用textContent顯示。

### Verified against（群組工作台）

`groups.GroupControl`／原bridge CSRF及`InstructionStore.observe_native`、native已核原回條read；
`index.html`的獨立group panel。新測項與#462附件工具列／#467 H3／WAKE-03／N6歷史相干回歸分批，
見[結果](../../../docs/agent-runtime-evaluation-baseline.md)。未部署、未在現場8765或remote建立群組。

這是薄 client，不是新的後端、controller 或 agent loop。本機模式只綁 `127.0.0.1`；遠端模式見下一節。使用既有 Python、FastAPI、HTTPX、Uvicorn 與 vanilla 單檔 HTML。頁面與 bridge 位於 `shared_context` 外；UI 修改不改後端 core pin。

## Native HITL H3（CODE/offline，預設關閉）

H1／H2／H3 的程式接線已齊，**沒有啟用或 live 驗收**。工作台沿共用
`FA_NATIVE_HITL_ENABLED`，缺省或 `0` 不發 HITL backend calls、不開批准介面；
舊 Table 路徑也不多發這些 calls。API／Hosted 各自需要原旗標，但這不是現在可啟用的指示：
開旗標、產品 Auto Review 真模型、部署／IAM／Toolbox 都另等批准，沒有新增 CLI 安裝或登入流程。

### 真人介面與原材料

- 規則放在「Agent Team 設定」頁的「批准規則」分頁（2026-10-08 前叫「設定」）：可從左下齒輪 →「Agent Team 設定」進入，或從聊天選單的「Team 批准規則／原操作紀錄」直接開到這一頁。
  最多 20 條自然語言規則，可以新增、編輯、刪除；
  處理方式為「先問我」或「自動允許」。每筆顯示原 stable rule ID，送出綁讀到的 revision／ETag。
  規則變了要重新讀取與確認，刪除不清歷史；規則本身不是待批 call 的人類決定。
- `NativeInstructionControl.refresh` 沿原 Agent／CID／RID 的 H2 `POST /turn-pending` **純讀**
  分頁取得 `references/attention/next_after/closed`。有原對話／RID 紀錄的工作台重開，
  或另一個還原同 journal 的 bridge，都重新查 API 持久參照，不靠一次性事件或 RAM wait。
  不增加跨 Agent queue，也不從 Responses 平台 history、角色或最後問句猜批准。
- `view().attention` 仍進 `state.composer.attention`，每項都是 `{thread_id,label,item}`；
  共用 wire 的 `NativeAttention` union，沒有複製 schema。`renderComposer()` 呼
  `renderAttention()`；HITL啟用時另沿Team分頁補齊其他Agent，總覽仍在同一
  `section#attention-panel[aria-label=需要處理]`／`div#attention-items`，可匹配的卡放到原回合內。
  `question_required`（PA-11 的 SDK `ask_user`）顯示題目與選項；`approval_required` 顯示原批准參照。
  Panel 是否顯示只看兩種 attention 的總數。歷史參照另在 `hitl-history-panel` 純讀，不假稱仍待處理。
- 目前對話的卡片會自動純讀原操作，另保留手動重讀；白話摘要先顯示，詳細保留 tool／category／target／完整參數、call／digest、
  原 review 與最新 rules revision、原期限／狀態。文字與 JSON 用 `textContent`，
  支援 H1 192 KiB 委派材料，不沿用舊 operation review 的 64 KiB 上限。
  畫面時間轉台灣時間，原 wire 的 UTC／bytes／hash 不改。
- 三選是「允許這次」「以後允許這類」「拒絕」。第二項先由人填寫或編輯日後的完整規則範圍，
  再按「確認規則並允許」；滿 20 不覆蓋，也不自動改成 once，仍可另由真人明選「允許這次」。
  2026-10-07 起按鈕一按就送、不再跳確認視窗，卡片收成「助理要執行／你允許了這次」紀錄（[卡片一按就送](#hitl-one-click)）。
  過期／closed／deny／interrupted／effect_unknown／已放行保持原 readback，只查原件。
  允許、grant、authorization、control receipt 都不顯示成「效果已完成」。
- 登入、密碼、2FA、CAPTCHA、付款確認只提供「開啟既有助理電腦面板」；
  不自動連線、不新授桌面能力、不改 H2 takeover／ceiling 拒絕。一般問句或 WAKE-03 的「同意」不會批准工具。

### 實際本機／remote 路由

以下 `{thread_id}` 是目前所選 Agent 原 journal 的 thread，不是 body 可指定的 Team 或權限。
所有 GET 禁 body／query；POST 沿原 origin／cookie／CSRF、strict JSON，控制 body 最多 8192 bytes。
完整 pending reader 沿共用 `NATIVE_HITL_HTTP_BYTES`，沒有把操作參數塞進人類決定 body。

| 工作台路由 | 真接點與 body |
|---|---|
| `GET /api/native-hitl/{thread_id}/rules` | API root `GET /native-hitl/v1/rules`；回真 `NativeHitlRules`、`can_approve` 與原控制紀錄。 |
| `POST /api/native-hitl/{thread_id}/rules` | `NativeHitlRuleChange` 的 `operation/rule_id/rule/expected_rules_revision/expected_rules_etag`；控制 request ID 由 bridge 固定，不收 caller 自訂 ID。 |
| `GET /api/native-hitl/{thread_id}/pending/{pending_id}` | 真 `NativeHitlReadback`＋最新 `NativeHitlRules`，核完整 pending hash、原 Agent／CID／RID／call 與本 Team。 |
| `POST /api/native-hitl/{thread_id}/decisions` | `approval` 為完整 `NativeHitlApprovalReference`，另有 `choice/rule/expected_rules_revision/expected_rules_etag`；由共用 `NativeHitlDecision` 驗證完整 command。 |
| `POST /api/native-hitl/{thread_id}/reconcile` | 只收 `{request_id}`，從 journal 取回已存原 command，POST 到原 H1 `/rules/reconcile` 或 `/decisions/reconcile`；不送 writer。 |
| `POST /api/native-hitl/{thread_id}/answers` | PA-11：`{question,answer,was_freeform}`；寫 `NativeHitlAnswer` 到 API `/answers`，一題一個固定 request ID。見下方「助理在問你」。 |
| `POST /api/native-hitl/{thread_id}/answer-reconcile` | PA-11：只收 `{request_id}`，帶 journal 的原回答到 API `/answers/reconcile`；不送 writer。 |

Reader／writer 位在 `web_test_console.native_hitl.NativeHitlControl`，用原 `PullAdapter` 的
HTTP／bounded response／token 邊界，但 Team control 路徑固定在 API root，不是 generic Agent relay。
`/api/native-hitl/team/attention`是新增薄GET；`/team/pending/{id}`、`/team/decisions`、`/team/reconcile`、
`/team/answers`及`/team/answer-reconcile`仍用目標Agent的原console/journal與原H1端點，不改原thread路由。
TokenVault 的 `Behavior.Approve` 只是前置檢查；真正 owner／Team／scope／驗簽及 CAS 都仍由 H1 API 核對。
API 404／未啟用明示不可用，不回空規則、不改走 legacy writer。

### 只送一次與畫面世代

真人按下決定後重新讀 pending／完整 digest／當前規則／權限。原 `Journal` 的 `native-hitl` phase
先以 SQLite FULL 保存完整 control command、固定 request ID、revision／ETag、pending digest／call、
設定 hash／Team／原 thread；commit（remote 含 `after_commit` 保存 hook）成功後才 POST 一次。
固定 ID 綁 owner／Team 與正規化後的完整 command／approval reference；雙 click、同 journal 的雙 client／重啟不重 POST，
不同連線送相同意圖仍由 H1 原 key／CAS 防第二個決定。這不是通用 HTTP／外部效果 exactly-once 保證。

UNKNOWN 原 command／狀態不改寫，只以原件 reconcile；讀回結果另作 observation。
後續合法新意圖須最新讀回、真人另行確認及不同 key，不將舊 UNKNOWN 視為未發生。
控制紀錄帶原 `thread_id`，跨 thread 的決定先前往原對話核對；規則仍為本 Team 共用。
remote hook 失敗先保 UNKNOWN、不 POST；舊 cookie 的 `REMOTE_SESSION_CHANGED` 不重新送出。
未送的規則草稿也會阻止 remote 自動 reload 丟掉內容。

讀寫的 success／error／finally 沿原 Agent／thread／設定／登入觀察世代，加上規則編輯版本隔離；
手動／自動更新登入都使舊結果失效。後到回應不填新選擇、不清新草稿、不結束新讀取。
規則欄位輸入不重建正在輸入的 textarea；普通進度重繪保留游標。
沒有新 token 檔案、DOM 欄位或 log，原 vault／secret 邊界保持。

### Verified against（H3）

固定 base `56ddb0ed`（#452，保留 #410／#430／#441／#444）加本 PR WIP；
Shared 的必要差異只有把 H2 私有 control receipt decoder 移到共用
`native_hitl_wire.NativeHitlControlReceipt`，runtime 和 UI 都匯入它，格式不變。
沒有改桌面 driver、部署、N5、Toolbox 或全域 eval ledger。

- `h3-run13`：71 PASS，含新 H3 32 案、H1 相干舊案、PA-11 原問句、native 結果／取消及 Responses adapter。
  兩 lane 都用既有簽章 API／fake native／Responses；不是啟動真 SDK、模型或原 8765。
  大材料核到 192 KiB；20/21、revision／舊決定保留、失 ACK、同 intent 並發／重啟、
  不同新 intent 不蓋 UNKNOWN、paging、缺身份／Approve／跨 Team、strict JSON 與 remote hook/cookie 都有直接案例。
- `h3-node-final`：338 PASS，包含原 template／context／result／cancel／PA-11、HITL 三選、規則 CRUD、
  大文字安全預覽、expired／closed／unknown、Agent／thread／設定／登入／編輯晚到結果與 finally。
- 控制歷史多帶原 thread 的後續窄差異，`h3-history-delta` 2 PASS；不把不同批相加。
  API-only 冷匯入正式 API／共用 DTO／整個 bridge，不載入 Copilot 或 Hosted SDK。
- 原 FAIL 保留：初次缺固定公用 tokenizer、fixture 身分／scope／Windows loop／Record／缺省欄位錯誤
  不算產品 RED；真正產品反例是過長 control ID、漏接 pending attention、
  規則輸入框重建、編輯後舊讀取卡住、不可用訊息／登入世代、Team scope 及歷史冒充待處理。
  修正後另跑新批；run1 未保存完整 WIP hash，run7 的 source helper 未執行，兩者只有原 log／XML，不回填 hash。
  每輪 source／原命令／log／XML 留本 owner session，不把 WIP 倒標為後 commit 或 image。
- 實作 `0caaa5c5` 後普通整合已對齊的 #462／main `87501a59`；保留附件工具列、remote 登入更新，
  原 `remoteSessionCheck` 改沿 published `remoteResponse`，仍保H3未送草稿與單一attention。
  整合WIP的14個Python交會案、Node347案通過，不改寫前批來源、不重跑H2。
- 首審兩軸同報刪除其他規則誤清草稿 P2；兩個新 DOM 反例先失敗，再改為只有成功提交 add/update 才清草稿。
  delete 保留未送原文及原 revision／ETag；15 個相干 DOM 案通過，原 FAIL 不改，只由原軸複核此差異。
- 原兩軸複核 `3d69ad98` 各0P1／0P2；PR需要的 `e13a08e0` 普通整合僅有兩份狀態文件衝突，
  H3邏輯及P2修復不變。9個onboarding／原P2交會DOM案例通過，不重跑完整矩陣。

## 遠端模式（ADR-0008 §3，stage 已部署）

依 [ADR-0008](../../../docs/adr/0008-environment-tiers-staging-cicd.md) §3，同一套工作台可以放在 Azure Container Apps，
**必須用 Entra ID 登入才能使用**，沒有匿名入口。網頁仍是薄 client，權限照舊由 API 核對 Entra 身分和 oid→Team。
stage 網址與部署紀錄見 [progress](../../../docs/progress.md#2026-10-02遠端工作台adr-0008-3entra-與-stage-身分設定3ef6)。

**誰能進來（四層）**

1. Entra 遠端工作台 app 開「需要指派」，只指派 Neal；沒被指派的人在登入時就被拒絕。
2. Container Apps 內建認證設定「必須登入」，`allowedPrincipals` 只列 Neal 的 oid。未登入的請求進不到程式。
3. bridge 核對平台注入的 `X-MS-CLIENT-PRINCIPAL-ID` 等於設定的 `human_oid`（外部無法偽造這個 header），否則回 `REMOTE_PRINCIPAL_DENIED`。頁面、API、noVNC 檔案與桌面 WebSocket 都要過這關。
4. FA API 照舊驗簽章、scope、client 與 oid→Team owner；遠端 app 是 API 允許的第二個 delegated client（`FA_REMOTE_CONSOLE_CLIENT_ID`，見 [API README](../README.md#遠端工作台的第二個-delegated-clientadr-0008codeoffline未部署)）。

**和本機的差別（同一份程式，沒有 `remote` 時路徑完全不變）**

- **登入與連線**：Container Apps 在頁面載入前就完成 Entra 登入。頁面載入後會**自動連線**，不用再按「連線」；左下角顯示「已登入：<帳號>」。自動連線只讀身分、助理目錄與模型清單，不送任何工作。
  - 內建認證的 token store 保存對 FA API 的使用者 token（`Behavior.Read`、`Behavior.Approve`）。連線與自動更新前，頁面先呼叫 `/.auth/refresh`，bridge 再讀當次請求的 `X-MS-TOKEN-AAD-ACCESS-TOKEN`，交給原本的 `Console.authenticate`／`TokenVault`；身分與到期檢查不變，不用 Azure CLI。
  - 更新失敗或拿不到 token 時，才顯示「登入已失效」與「重新登入」按鈕，按下後導到 `/.auth/login/aad`。登入 cookie 仍有效、token store 卻沒有這筆（例如被輪替或清掉）時，頁面照常載入，bridge 回 401 `LOGIN_REQUIRED`，同樣走「重新登入」；只有登入身分不是設定的 owner 才回 403 `REMOTE_PRINCIPAL_DENIED`。
- **同源與 CSRF**：origin 換成 `https://<網址>`；cookie 改名 `__Host-fa_console` 並加 `Secure`；CSP 與桌面 viewer 改用 `wss://`。另有兩點和本機不同：
  - Entra 登入後，瀏覽器是從 login.microsoftonline.com 轉回首頁，這個請求會帶 `Sec-Fetch-Site: cross-site`。所以遠端只有「載入首頁（GET `/`）」不限制來源；所有 API 仍只接受同源。
  - Container Apps 登入層會把「用 cookie 登入、卻沒有 Origin／Referer 的 POST」當成 CSRF 擋下，並回空內容（[官方說明](https://learn.microsoft.com/azure/app-service/overview-authentication-authorization#mitigation-of-cross-site-request-forgery)）。所以遠端改用 `Referrer-Policy: same-origin`（只有送給自己時才帶 Referer），authConfig 的 `allowedExternalRedirectUrls` 也加入自己的網址。
  - 登入層自己擋下（401／403，且回應不是 JSON）時，頁面顯示「雲端登入層拒絕…」（`PLATFORM_REQUEST_REJECTED`），並出現「重新登入」；這種請求沒有送到工作台。其他非 JSON 的錯誤（例如 ingress 的 5xx）維持原本「是否完成尚未確認」的說法，不重送。
- **只送一次**：SQLite journal 在容器本機 `/data/state` 執行。每次有寫入的 commit 後、回應之前，把所有 journal 打包上傳到同一個 Blob，上傳時必須持有該 Blob 的 lease。
  - 上傳失敗時回 `REMOTE_STATE_UNAVAILABLE`，這次不會送出工作 POST；所以已送出的 POST，它的 intent 一定已存進 Blob。之後只查原回條，不自動重送。
  - 新的 replica 啟動時，如果 Blob 正被舊 replica 鎖住，會先打斷舊 lease 再還原；舊 replica 下一次寫入會失敗，並自行結束讓平台重啟，不會覆蓋新紀錄。`maxReplicas=1`。
  - 換 revision 時新舊兩個 revision 會短暫並存，各自重啟時會互相打斷 lease，交換幾次後只剩新的那個（stage 實測約 1 分鐘）。這段期間寫入仍一律要持有 lease，所以不會覆蓋或重送；只是操作可能暫時看到 `REMOTE_STATE_UNAVAILABLE`。
  - 被鎖住的 Blob 不能寫入（連「不存在才建立」也會被拒），所以啟動時只在 Blob 不存在時才建立空檔。
  - 還原只接受 256 MiB 以內的打包檔，所以寫入端也守同一個上限：打包後超過就回 `REMOTE_STATE_FULL`，Blob 維持上一次的內容（重啟仍能還原），replica 不結束、照常可讀。第一次超過一半時，stderr 會寫一行 `REMOTE_STATE_NEAR_LIMIT`，供管理者提早處理。不自動刪舊紀錄：回條與進度列之後還會被讀回，刪了可能讓頁面少了需要的狀態。
  - 管理者看到 `REMOTE_STATE_FULL` 時：之後**每一次寫入都會被拒**；回 `REMOTE_STATE_FULL` 的那些 commit 只寫進容器本機，沒有進 Blob，replica 一重啟（閒置縮到 0、換 revision、平台重啟）就會遺失，Blob 仍是最後一次成功上傳的內容。處理順序：
    1. 請使用者先停止送新工作，並且不要手動重啟或重新部署；這時重啟只會丟掉那些本機紀錄，不會讓寫入恢復。
    2. 恢復寫入只有兩條路，都需要部署批准：放寬上限（改 `remote.py` 的 `MAX_ARCHIVE_BYTES`，打包與還原共用這個常數，改完重新部署）；或在 replica 停止、Blob lease 已打斷後，離線縮小 Blob 裡的打包檔。這兩種做法都還沒實際演練過（未驗證），目前沒有現成的裁切工具。
    3. 恢復後，遺失的紀錄若屬於已送出的工作，頁面照原 request ID 查原回條，不自動重送；沒送出的那次在回 `REMOTE_STATE_FULL` 時就停在 POST 之前。
    看到 `REMOTE_STATE_NEAR_LIMIT` 時就先規劃第 2 步，不要等到 `REMOTE_STATE_FULL`。
  - 啟動被拒時，log 會寫出原因的類別與服務代碼，例如 `REMOTE_CONFIGURATION_REJECTED (HttpResponseError 412)`；不寫 URL 或 header。
- **新增助理（＋）**：和本機相同的流程，不需要重啟或重新部署。細節（Blob 保存、stage 等權限生效）見下方「新增助理」段落。
- **閒置縮到 0 之後**：重新啟動的 replica 會換新的頁面 cookie。舊分頁的下一次請求會收到 `REMOTE_SESSION_CHANGED`，頁面自動重新整理一次，不會重送任何操作。以下兩種情況不自動重新整理，只顯示訊息：頁面開啟未滿 10 秒（避免無限重整），或頁面上任何文字欄位（輸入框、新增 skill、新增助理、Team MCP 等）還有改過且未送出的內容、任何檔案欄位已選了檔案、任何對話還有未送出的附件（圖片、文字檔或目錄；檔案欄位讀完就清空，附件只留在記憶體），或 HITL 還有草稿（讓使用者先複製）。這是逐一掃描頁面上所有欄位，不是固定清單；搜尋框是篩選條件，不算草稿。

**設定組**：用離線指令把部署線審核過的本機 manifest 轉成遠端版本。只改 delegated client（遠端 Entra app）與容器內路徑，SHA 依新 bytes 重算：

```powershell
& '<既有 Python interpreter>' -B -m web_test_console remote-bundle `
  --agents '<本機 manifest>' --agents-sha256 '<原檔 SHA-256>' `
  --client-id '<遠端 Entra app client ID>' --out '<操作者私有的新資料夾>'
```

輸出檔留在操作者私有資料夾，不進 Git。`configFiles.json` 是部署參數，會成為 Container Apps secret 並掛在 `/config`。指令也會印出三個 SHA，供部署參數使用。

**部署**：[`infra/web-console/web-console.bicep`](../../../infra/web-console/web-console.bicep) 建立以下資源，AVM 版本與 FA 現用相同。

- UAMI、關閉共用金鑰與公開網路的儲存帳戶（`journal`、`easyauth-tokens` 兩個 container，加 blob private endpoint）、AcrPull。
- Container App：min 1／max 1（2026-10-07 起常駐，Neal 批准；Bicep 參數 `minReplicas`，預設 1），0.25 vCPU。原本 min 0 時閒置約 5 分鐘會縮到 0，下一則訊息要等工作台重新開機。常駐每月約 USD5.83（West US 3 閒置費率，[計費](https://learn.microsoft.com/azure/container-apps/billing)）。CI 的 console 步驟讀出整份 template 後只換映像與 ENV，scale 會保留。
- 內建認證：用 managed identity 當 federated credential，不需要 client secret；token store 也用 managed identity。

Entra app、指派、同意與 federated credential 用 Microsoft Graph 另外建立。CI 每次部署只換成同 build 的 image 與宣告的 ENV（`ci_deploy.py console`，其餘 template 原樣保留）；設定組改變時才重跑 Bicep。

**第一次建立時的注意事項**：app 會比登入設定早一步建立，所以第一個 replica 可能在登入 sidecar 掛上之前就開始執行。這時 journal 是空的，也沒有 token，但仍應盡快換掉。所以 azd（`fa_azd.py console_app`）第一次部署時帶 `ingressExternal=false`，app 只在 Container Apps 環境內可達；登入設定建好後，第二次部署才打開外部 ingress。工作台不以 token header 判斷能否進入：沒有登入層時，這個 header 一樣能偽造，真正的防線是 ingress 的順序與 API 的簽章驗證。手動重跑 Bicep 時參數預設 `true`。2026-10-02 stage 第一次部署就遇到這種情況：用新的 `revisionSuffix` 重跑 Bicep，再停用（不是刪除）第一個 revision；之後的 revision 都會從一開始就帶 sidecar。部署後請用 `az containerapp replica list` 確認每個 replica 都有 `http-auth`。

容器啟動指令是 `python -m web_test_console serve-remote`。除了沿用 `serve` 的設定與 SHA 參數，再加 `--state-root`、`--state-blob-url`、`--public-origin`、`--owner-oid`、`--managed-identity-client-id`。設定的 `human_oid` 必須等於 owner，所有 journal 必須位在 `--state-root` 內，否則不啟動。

### Verified against（遠端模式）

- 程式基準：見 PR；已讀 `bridge.create_app`／`Journal.connect`／`Console.authenticate`、`desktop.install_desktop_routes`、`__main__`、`agent_connections.read_connection`，以及 index.html 的 `run`／`refreshAuthentication`／桌面 WebSocket。
- 實際驗證：`test_web_test_console_remote.py`（含遠端重啟後 cookie 不符回 `REMOTE_SESSION_CHANGED`）。
  - lease 交接：測試替身照 Azure 的規則，被鎖住的 Blob 沒帶 lease 的寫入一律 412。新 replica 能接手並還原最新 commit，舊 replica 寫入失敗並只觸發一次結束。這項在修正前會失敗。
  - 上傳失敗時請求被擋下；壓縮檔只接受一般相對路徑檔案。
  - 容量：打包超過一半時只警告一次；超過上限回 `REMOTE_STATE_FULL`，不上傳、不結束 replica，新 replica 仍能還原上一次的內容。這項在修正前會失敗。
  - 缺 principal 或 principal 錯誤時，頁面、API、noVNC 都回 403。
  - 遠端從 cross-site 轉回首頁可以載入，但 cross-site 的 API 請求仍被擋；`Referrer-Policy` 遠端為 `same-origin`、本機仍為 `no-referrer`；`/api/state` 只有遠端才帶 `remote_user`。
  - 只有帶 token header 才能登入，且 token 能從 middleware 傳到登入流程。
  - `__Host-` cookie／`wss` CSP；`serve-remote` 對 owner 與 state root 的檢查，以及啟動被拒時寫出原因；`remote-bundle` 只改 client 與路徑。
- Node 工作台新增 5 項：遠端載入後只自動送一次 `/api/authenticate`（先 `/.auth/refresh`）並顯示已登入帳號；登入失效時按鈕導到 `/.auth/login/aad`；登入層回空的 403 時顯示清楚訊息且不重送；已連線後被登入層拒絕時會出現「重新登入」，而 ingress 的 503 仍顯示「尚未確認」；本機 8765 不自動連線，也不呼叫 `/.auth/refresh`。
- 本機不變：Node 工作台全套 311 通過。`test_web_test_console_*.py` 先前已在 main `56399a71` 跑過整套比對（455 通過；另有 91 個 tokenizer 快取造成的錯誤，與 base 逐項相同）；這次修正重跑了受影響的 remote／desktop／agents／frozen_interface／steady（63 通過）。
- live（stage，2026-10-02）：未登入時頁面、`/api/state`、`/novnc/core/rfb.js` 都 302 導到 tenant 的 Entra 登入頁。上面兩個登入後的 bug 和 lease 的 bug，都是從 live 的 sidecar 與 app log 找到的。

## 附件工具列（LOCAL，未部署）

文字來源、UTF-8文字檔與圖片改由模型選單同一工具列的三個icon開啟。
使用inline SVG、tooltip、`aria-label`／`aria-controls`／`aria-expanded`，不從外站載圖。
初始不展開，點同一按鈕可收合，切換種類只開一個原picker；Agent、對話、草稿或登入範圍改變時收合。
開合只是畫面動作，不會自動查來源、讀檔、上傳或送出工作，也不清掉原文字／檔案草稿。

原picker與`instruction`／`user-text-files`／`user-images`等id保持不變。
附件數量、UTF-8／圖片大小限制、來源與模型資格、上傳／預覽，以及editable／can_follow_up的停用條件沿原路。
遠端`remoteResponse`的草稿保護、登入與自動連線不改；H3「需要處理」及onboarding也不改。
本機與遠端共用此HTML；native公開步驟的保存／讀回是另一片，不由這次工具列改動取代。

### Verified against（附件工具列）

基準`675814c1`，改動在`index.html`的picker外殼、`renderAttachmentToolbar`與既有composer接點。
兩個公開DOM反例先RED，加入icon與開合後完整Node313案通過，包含原三個PA-08附圖案例及遠端草稿保護。
Playwright只開本機檔案、使用合成state與攔截fetch：1280px下初始全收合、點文件只開文件panel，
三個icon均在模型工具列且有正確無障礙標籤；375px下無橫向溢出、舊lane不顯示圖片icon、全程零POST。
不是stage／dev live驗收，沒有呼叫真bridge或模型。
固定`675814c1→61c16e16`的Standards／Spec兩軸未發現P1／P2；後續只同步審查文件。

## 原生公開處理步驟（LOCAL，未部署）

兩條原生 lane 在原回覆下沿既有步驟面板，顯示已保存的工具名稱、狀態與安全短摘要。
只有原模型／capture允許且provider確實提供時才顯示公開推理摘要，不顯示原始私有reasoning或工具內容。
進行中、完成後與重新開啟歷史對話都讀同一個 RID；舊回合沒有snapshot時仍明示未提供，不補造事件或零次工具。

`GET /api/instructions/{rid}/progress` 由原native bridge取得Agent／CID，再查owner的純讀入口；
原答案更新不等待這個GET，讀取失敗保留上一份已知步驟並標「未確認」。
snapshot `finished`只表示這份顯示紀錄已封口，不取代原回合狀態；截斷標示「已截斷」。
已完成且核回的snapshot不反覆抓取；切換Agent、thread、console設定或登入後，晚到回應不能改目前畫面。
若原回合已結束、進行中發出的GET才晚到且尚未封口，沿同RID再做一次純GET取得收尾；
補讀仍未確認就保留原步驟，不循環重查、不重送工作。
原推理摘要、工具配對、圖片顯示、H3及取消仍各走原接點，不新增批准或自動送出。

### Verified against（原生公開步驟）

基準`87501a59`加本片：`NativeAdapter.progress`、`NativeInstructionControl.read_progress`、
bridge獨立GET，以及`refreshNativeProgress/readNativeProgress/processingPanel`。
三個原生DOM呈現反例與已完成歷史切換反例先RED；後續完整Node320案通過。
新增薄client反例核GET失敗仍保原答案與步驟；真SDK完整bridge接線一案通過。
另驗慢進度GET不延遲答案、舊console回應不污染畫面；全部無額外POST。
早期兩個截圖時序FAIL與補驗分開保留，沒有宣稱真stage／dev驗收。
普通整合正式`db039f18`後，公開進度與H3的39個Python案例、完整Node359案通過；H3及onboarding接點不變。
Spec軸指出完成時進度GET在途會漏讀收尾；雙lane各含已封口／仍未封口的四個DOM反例先RED，
改成至多一次原RID補讀後，連慢GET不擋答案及舊設定拒絕共六個直接案例、完整Node363案通過。
Standards於`d2675692`為0 P1／0 P2；原Spec複核`d2675692→ba242609`關閉此P2，結果0 P1／0 P2。
保存容量及後端分批來源見[原生公開進度](../README.md#原生公開處理步驟local未部署)。

## 附加圖片（B1 PA-08）

原生對話（Invocations 的 `foundry-native-v1` 和 Responses 的 `foundry-responses-v1`）在選到支援讀圖的模型（`supports_image_input=true`）時，composer 會出現「附加圖片」。
這個旗標來自 API `/work-policy/v1/models`：只有模型事實宣告 `supports_image_input` 時才多帶 `supports_image_input: true`，
其他部署的回應不變。
- **本機轉檔**：png／jpg／webp 先在瀏覽器裡轉成 PNG，長邊縮到最多 2000 px；超過 512 KiB 時再逐步縮小，直到放得下。
  一回合最多 4 張，檔名換成 `.png`，必要時加數字避免重名。轉檔失敗或超過上限時，這批不加入，也不送出任何東西。
- **先上傳**：每張圖用 `POST /api/instructions/{rid}/image-upload`（`{name, png_base64}`，本機 body 上限 = 一張圖的 base64 加 4 KiB）
  經 API 的 `/work-policy/v1/native-turns/images` 存到這位助理那邊。這個存放以內容雜湊為 key，重傳同一張圖不會多一份，
  回應不明時可以直接重傳。bridge 會核對助理回報的雜湊和長度等於本機那份 PNG。
- **送出只帶參照**：送出時的 body 只多一個 `images`（name、sha256、byte_length、media_type），不再帶圖片內容。
  歷史中的使用者回合會顯示附圖的名稱、長度與 sha256。
- **兩條原生路徑相同**：Responses 對話的上傳走同一個 `image-upload`，存在同一位助理那邊；送出照 server 選的路徑走
  `/work-policy/v1/responses-turns/submit`，同樣只帶 `images` 參照。
- **不支援的路徑會明確拒絕**：舊的 job-bound 路徑會在本機回 409 `USER_IMAGES_UNSUPPORTED`，不上傳也不送出；
  模型不讀圖時按鈕停用。助理端的其他拒絕（沒上傳、空間不足、模型不支援）照原錯誤碼顯示，原草稿保留。

## 需要處理：助理在問你（B1 PA-11，預設關閉）

助理用 SDK 內建的 `ask_user` 提問時，問題會出現在對話上方「需要處理」，和 H3 批准卡共用同一份清單。
回答只是回答，不批准任何操作、不新增規則，也不產生 grant。
- **什麼時候有**：只在 `FA_NATIVE_HITL_ENABLED=1`（和 H3 同一個旗標）時。關閉時工作台不讀問題、不顯示卡片，
  `/answers` 回 409 `NATIVE_HITL_UNAVAILABLE`。已拿掉舊的「回覆以問號結尾」判斷。
- **怎麼找到問題**：進行中的回合（回條不是 completed／busy_refused）才查 API 的 `POST /native-hitl/v1/turn-questions`，
  依原 Agent／CID／RID 分頁。只列還沒回答、沒過期、回合沒關的問題；重開工作台也能從 API 找回。
- **卡片內容**：題目、選項按鈕；`allow_freeform=true` 時另有自己輸入的欄位（最多 2000 字）。別的對話的問題只給「前往這段對話」。
  輸入中的草稿在畫面更新、登入或切換時都保留，直到這次回答確認送出。
  2026-10-07 起選項與「送出」一按就送、不再跳確認視窗，送出後卡片收成「助理問／你的回答」紀錄（[卡片一按就送](#hitl-one-click)）。
- **送出一次**：`POST /api/native-hitl/{thread_id}/answers`，body 為
  `{question: NativeHitlQuestionReference, answer, was_freeform}`。先重讀原問題，核對 digest、原 Agent／CID／RID、期限、
  回合未關、選項合法；再把完整 `NativeHitlAnswer` 以 FULL 寫進 journal 的 `native-question` phase，
  （remote 含 `after_commit`）成功後才 POST 一次。request ID 只由 owner／Team／原問題決定，
  所以同一題再按一次、換別的答案或重開工作台，都只回傳已存的原回答，不會再送。
- **結果不明**：只能按「只核對原回答」，走 `POST /api/native-hitl/{thread_id}/answer-reconcile`（body `{request_id}`），
  帶原 command 到 API 的 `/answers/reconcile`；確認後才標成已送出。不換 ID、不改答。
- **權限**：Team 的真人 owner 一般登入即可回答，不需要 `Behavior.Approve`；批准與規則仍要 `Behavior.Approve`。
- **格式**：`attention` 每項仍是 `{thread_id, label, item}`。問題的 `item` 是
  `shared_context.native_hitl_wire.NativeQuestionRequiredItem`（`type="question_required"`、`item_id`＝原 `question_id`、
  `question` 為 `NativeHitlQuestionReference`、`text`、`choices`、`allow_freeform`、`created_at`、`expires_at`、
  `purpose="attention-only-not-approval"`），和 `approval_required` 共用 `NativeAttention` union。
  已回答的原 command 放在 `composer.native_questions.commands`。

## 全新 A/B 原生設定的離線匯出（CODE/offline）

`python -m web_test_console.fresh_export` 從已核准 fresh 路徑的 config／plan，
以原 `StandingConsoleConfig`、`InstructionConnection`、`AgentConnections` 產生 A/B 各自檔案及 manifest。
不讀 Table、不改 API／IAM／旗標、不保存 token，也不把 plan 當成已安裝證明；
`installation_verified=false` 保持明示。操作／停止條件見
[fresh runbook](../../../docs/POC-RUNBOOK.md#fresh-console-export)。
manifest 固定匯出環境的絕對路徑；在目的地產生，移到不同作業系統或目錄時重新匯出，
已有不同內容不覆寫。這是給 configured A/B 的檔案準備，不擴大 onboarding 的 HTTP reader，
不改聊天 UI、舊 lane、Responses 或真人批准流程。

原生 sidecar 只指定路徑，不能替 scoped B 批准範本。B 尚無版本化 profile／自己的
selection 時，API template export／native capture 都回 `AGENT_TEMPLATE_PROFILE_UNSUPPORTED`；
明確 `foundry-native-v1` 連線不 fallback 到 job-bound。沿既有主人選版流程保存精確範本
後才可建立新對話；設定匯出、選版成功、session ready 與模型成功仍分開判斷。
遠端 selection 的 checkpoint platform 必須是 API／Hosted 的 `linux-x64`，
不沿 Windows 匯出者的本機 default。

### Verified against（fresh 匯出）

本 PR WIP 的 `fresh_export.main` 與 `test_fresh_installation.py`，
檔案沿真 DTO／`read_connection` 讀回，含 A/B 身份及 SHA、原生 sidecar、無寫 Table、
重跑相同內容及拒絕覆寫。API image 不包含此 package，匯出不要求替 API 加套件。
2026-10-07 接續另核 `NativeInstructionControl.refresh_routing`、`configured_prompt_override`、
template export／`new_conversation_template` 及原 `select_profile`；fresh signed API 回歸
確認 scoped B 明選後才能 export／capture，沒有放寬 server default 或改工作台路由。

## WAKE-03 忙碌時追加文字（LOCAL，未部署）

承接後端#429：
已核原native回條仍accepted／started時，輸入框可追加文字，送出鈕顯示「追加」。
原模型與設定固定，Invocations native／Responses共用後端控制入口，不另開普通真人RID、
不呼`/next`／`/submit`，也不重送Responses。取消按鈕不因追加HTTP等待而停用。
本片只收文字；已有附件保留於草稿，不悄悄丟棄或塞進追加body。

- `POST /api/instructions/{rid}/append`只接受`append_id`與`content`，沿原localhost／cookie／CSRF
  及所選Agent；bridge從原回條取得CID／digest，再送原owner API。瀏覽器不供權限、模型或SID。
- 原journal記錄追加意圖和完整body後才POST；同追加ID再來只GET核對、不重送。
  每筆追加的本機ID綁原Agent／RID及上一筆本機意圖；它不是執行名額或授權。
  原submit／command／capture不改，UNKNOWN原件另保留，讀回結果存獨立observation。
- `GET /api/instructions/{rid}/appends`只讀後端原控制清單。原進度更新對accepted／sending追加
  另做純GET；unknown可手動「核對追加」，沒有自動POST。重開controller可沿journal找回原ID。
- 追加顯示在原回合內，區分已保存、尚未確認套用、SDK已套用、同一回合中接著處理（queued）、未套用及unknown；
  不拿message ID當完成，也不把queued說成另一筆普通交辦。未套用且 `reason=turn_finished`（確定沒有送達，例如回覆已顯示、
  回合還沒存成 completed 的尾段送出的追加）由本頁自動以新 RID 當下一則送出一次（2026-10-16，G 計畫「不掉訊息」；
  2026-10-19 起 Hosted 在最後一輪 SDK 結束時就回這個結果；`foundry-native-v1` 上本頁不再等原回合結束，Hosted 把新回合排在原回合之後，
  Responses 沒有佇列（原回合未結束時會 `busy_refused`），照舊等原回合結束才送；多則追加照追加順序送；
  只有結果未知才不能換 ID），只送那段文字、不帶附件；輸入框或附件已有其他草稿時不送，改留「帶回輸入框」給真人按，
  不覆蓋已輸入的其他草稿；重新整理後不再自動送。其他原因（例如按「停止」後的 `cancelled`）不自動送，只留「帶回輸入框」。
  unknown只有查讀。
- 已POST的原文與未送草稿分開記住：同版原文在結果未核清前，不因回合完成或重新登入
  變成另一筆追加／普通交辦。POST ACK與GET已核回條共用清稿條件，只清未另編輯的原文，
  不清下一筆草稿（包含重新輸入的相同文字）；後端仍回unknown時不清稿、不換ID重送。
  `receipt_observed`只標示bridge確實讀過回條，不能把本機UNKNOWN意圖或SDK未知結果當成已套用。
- 登入、Agent或thread切換後，不發布晚到回應、不清掉另一位Agent的同名草稿。
  普通送出、舊Table、N6平台history及原RID有限reconcile都沿原路。

### Verified against（WAKE-03 UI）

以正式N6-3／4 `56399a71`為起點；接點為`appends.AppendControl`、
`NativeInstructionControl.view/task_view`、原bridge與`index.html`，沒有後端新權威或模型loop。
55個追加／native／Responses bridge案例通過，另有登入失效後不發布晚到ACK的直接案例；
其中一案真的走8765 bridge→簽章owner API→真N1→SDK1.0.13/runtime1.0.83，
模型HTTP離線，結果仍是原RID第1回合，追加被下一model request使用。
Node完整296案通過；前一批原截圖非同步案例1 FAIL保留，該ID單獨補跑通過，未改圖片產品邏輯。
初始缺can_append、localhost body例外、202受理、前端輸入停用／純GET進度／未套用帶回均保留RED；
測試重建controller沿原單event-loop方式，不開第二個相同loop。
其後正常整合正式`cbb92f37`（附圖、遠端與N6採用），81個Python直接案例、Node299案通過。
合併另以RED鎖住「原已送附件不該擋文字追加」；現在只檢查未送的附件／檔案／圖片草稿，
原凍結附件不當成新草稿。圖片選取仍沿既有editable／can_follow_up條件，本片不改普通附圖。
新兩軸同報兩個草稿P2後，先加三個DOM反例：原回合完成仍保留未送文字；
舊ACK不得清下一筆不同文字或同文的新草稿。修正把可見草稿鍵與append傳送ID分開，
ACK另核原草稿編輯版本，九個直接案例與完整Node302案通過；不自動搬成新工作或重送。
原兩軸複核各另找到一個已送原文防重送P2；兩個DOM反例與登入更新的同根因反例都先RED，
再以已送快照及POST／GET共用核回修正。完整Node305案與4個公開bridge案例通過；
GET與晚到ACK都保留下一筆草稿，UNKNOWN原journal不改，已核投影與本機意圖明確分開。
再普通整合正式`061c947c`的Responses附圖與問句提醒；兩個HTML交會處保留雙方功能，
完整Node306案與8個公開bridge交會案例通過，Shared Context與76項帳本沿正式main原樣。
原Standards／Spec各自複核`3266b50b`，本輪新增P2均關閉，兩軸各0 P1／0 P2。
其後普通合入正式`f0af6b8b`，只有CURRENT-STATE文件衝突；本片產品／測試檔與受審版本相同。
另跑一案bridge→簽章owner→真N1→固定SDK的離線provider，仍在原RID套用追加；
只確認新主線接點相容，不把HITL預設關閉的案例說成H2批准或真模型驗收。
這不是現場8765、Hosted或真模型品質驗收；實際採用仍沿部署批次。

<a id="n6-responses-工作台準備local未啟用"></a>
## N6-3 Responses 工作台（LOCAL，預設未啟用）

新對話依原 API 模板的 `TemplateSettings.state_profile` 選路，只有
`GET /work-policy/v1/agent-template/options` 的 `responses.enabled=true` 才能在模板編輯器選取
`foundry-responses-v1`。API 沿同 build、`responses 2.0.0`、兩個正式旗標核對；probe 不算。
只有未固定lane的新草稿，在本機輸入驗證通過後才讀原模板／資格，拒絕時不改用 Invocations 或猜 ID。
舊Table／Invocations已有lane、command或intent時，送出／重新登入／讀模型不依賴模板GET；
已固定Responses只查必要Options，不藉最新模板改路。真正新對話仍須server選路，不能因模板故障降級。
唯一例外是job-bound連線（未設`state_profile`）遇到API明確回409 `AGENT_TEMPLATE_PROFILE_UNSUPPORTED`
（Agent沒有prompt profile也沒有selection，例如尚未寫selection的peer）：server本來就沒有選路，
新對話沿原job-bound submit；模板面板仍顯示`TEMPLATE_UNAVAILABLE`。native連線照舊在寫入前拒絕，
因為native capture同樣拒絕這種Agent；其他模板錯誤（含404或其他409錯碼）也照舊拒絕。
`InstructionConnection` 只保留舊 Table／Invocations 部署接線，不能替新 lane 擴權。

- 原 SQLite journal 在同一交易擴充可用 lane；舊 command、CID、原 UNKNOWN、已固定路徑不重寫。
  Responses 的 submit／reconcile／cancel 走 `/work-policy/v1/responses-turns`；
  原回條、用量、圖片、附件和收件仍走既有 native 控制面。未知、已開始或已排隊不是重新交辦的理由。
- **進度 GET 是純讀。** 瀏覽器看到 server 投影的原真人 RID 是 accepted、無 response_id、
  dispatch=needs_response/not_taken 且允許核回時，才另送帶原 CSRF 的既有 submit-reconcile POST。
  bridge 保原 command／CID／SID／digest，同一 journal 記真錯次數與下次時間；
  正常等待最多60秒一次、不扣真錯預算。#668 起 owner submit 的平台 POST 改在 API 背景送，背景 POST 的錯誤不計入
  工作台預算；核回的真錯仍最多10次（記在 journal，重開controller不清零），所以持續失敗時同 RID、同 body 最多 1＋10 次 POST。
  平台 401／403 改在第一次核回才看到並停下
  （多一次同 RID POST，被拒、不會執行）。API 不另做送出中記錄，撞車由官方 409
  `conversation_locked` 擋，工作台看到的行為不變。遇 `NATIVE_RESPONSE_UNCONFIRMED` 顯示「尚未確認接手」，不是終態unknown卡。
  達上限保留原RID，仍可取消；舊Invocations不自動續送，Agent來訊由原T5 follow處理。
- 歷史來源只讀 server `responses.history_source`，不從 CID／response_id 字樣推測。
  一次一頁、`after`是平台item ID；response-chain最後一頁明示previous ID，按鈕才往前讀，
  不自動掃完整鏈。新CID可用資格關閉時，已保存歷史的selector仍獨立保留。
- 8765 `GET /api/instructions/{thread_id}/platform-history` 只依原thread的已核CID／response_id
  或上一頁cursor查 API 平台scope，不先呼VM歷史。2026-10-06 起只在「⋯ → 支援明細 → 平台對話歷史」展開或按
  「更新平台歷史」時才讀；整段進度讀取不再順帶讀第一頁。平台訊息與FA回條分開呈現：
  `platform_role=user`只叫「平台輸入」，不冒充真人／Agent來源；commentary不當答案，
  只有API已核的final_answer顯示文字。缺phase項目明示未確認，只有已知原RID能按原結果下載回讀，
  不以順序、文字或平台item ID補造RID。
- 原生圖片／草稿、取消、原結果下載、舊對話與Agent切換照原入口；換登入／Agent／thread的晚到
  平台回應不發布。此片保留輪詢，不實作SSE；日後若接串流，每次response.in_progress必須重置累積畫面。

### Verified against（N6-3 交付接點）

`native.NativeInstructionControl`／`responses.ResponsesAdapter`、`InstructionStore`、
`freeform.InstructionControl.validate_input`及原bridge/query/CSRF路由與`index.html`平台歷史、資格選項、同RID核回。
N6-1a與N6-2共享DTO/helper沿原owner，不新增API索引、工作權威、queue或agent loop。
[分批驗證](../../../docs/agent-runtime-evaluation-baseline.md#2026-10-02n6-3n6-4-完整consumerlocal預設關)
分開記本機替身、真官方task、RED及後續範圍；不是現場8765/Hosted驗收。

### 早期本機準備紀錄（保留原範圍）

以下記錄早期 `3a214dc5` 的準備範圍，後續接線以本節上方為準。
N6-3/4 已核准改用新 `foundry-responses-v1`；本線已正常整合 **N6-1a正式main `09ae3dff` shared wire**，
`response_id`／未接手狀態只用這個來源。**N6-2正式路由與history DTO仍待發布**，
不自行定義platform history cursor或兩步送出API。
`InstructionConnection` 仍不接受新旗標，正式 HTTP／自動 reconcile／T5 follow 尚未開啟；
不拿本節的離線準備宣稱 Responses 已可使用。
後續新模式只消費server `TemplateSettings.state_profile`，不能由sidecar自行開啟。
還須按API回報的同build `responses 2.0.0`、`FA_NATIVE_TURNS=foundry-native-v1` 與
`FA_RESPONSES_ENABLED=1` eligibility顯示／允許新模板；probe不算，拒絕不fallback Invocations。
歷史assistant只採`phase=final_answer`，commentary是進度；若平台未提供phase，
沿N6-2明示可用性回原FA NativeResult，不把所有output_text拼成最後答案。D3未live確認。

先完成兩個不依未發布 API 的本機接點：原 SQLite `instruction_lanes` 用同一 transaction
擴充合法 profile，原 Table/native lane、command、CID 與 UNKNOWN intent 不變；
新增 Responses pin 後，即使重啟／移除偏好也不換路徑。已有命令或未知意圖但缺 lane 的舊對話
仍固定 legacy，不自動升級。畫面共用原生狀態／Agent 訊息／CID隔離／圖片完整性判讀，
不捏造 Job／工具數量，也不因新 profile 把圖片降成未核 hash 的 legacy 預覽。

### Verified against（N6 本機準備）

- 程式 `3a214dc5353390a628dc5ecf99320a49b5237964`，基準 main `75696b6f`；
  `instructions.InstructionStore`、`index.html.nativeProfile`，沒有改 Shared DTO、Hosted、T5或套件。
- `test_web_test_console_responses.py` 與直接舊 lane 回歸共9案 PASS／11.416秒；
  Node 雙原生呈現、原圖片／換版等15案 PASS。新lane原先明拒、Responses呈現缺原生面板／漏CID過濾的RED均保留。
  [分批原件及限制](../../../docs/agent-runtime-evaluation-baseline.md#2026-10-02n6-工作台本機準備尚未接新協定)。
- 後續：依71c7/79fa的固定介面接 accepted且無response_id 的原RID reconcile、平台歷史與T5持久follow，
  純GET回條不得啟模型，舊lane不擴大自動送出。此片先保留輪詢，不新增SSE；
  若後續接串流，沿官方 [Responses 2.2.0 resilience contract](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/resilience-contract.md)
  每次 `response.in_progress` 清除累積畫面，不拼接重播內容。N6-0合併前不合本片；未部署。

## N2 原生對話模式（CODE/offline，未更換現場 8765）

依總協調確認的 N2 分片，既有 `fa-console-instructions-connection-v1` sidecar 新增可省略欄位：

```json
{
  "schema_version": "fa-console-instructions-connection-v1",
  "configuration_sha256": "<原設定的sha256>",
  "backend_core_sha256": "<部署核定的core sha256>",
  "state_profile": "foundry-native-v1"
}
```

這只是部署接線設定，不授予新權限；範例不是可直接使用的設定。沒有 `state_profile` 時，
舊 sidecar bytes／hash 和工作台行為不變。每位 Agent 沿自己的 sidecar／journal，
不能因為切換助理，就套用別人的對話或登入範圍。現場啟用等 N1／N2／T5 合併與部署後，
由總協調安排；本片未操作原 8765 或改現場檔案。

啟用前，部署線沿原owner登入，先呼叫API的
[`POST /agents/{agent_id}/work-policy/v1/native-turns/prepare`](../README.md#明示的-api-mi-native-preparelocal未部署)，
由API原MI明示準備native環境；工作台不代做、不新增人工表單。
未prepare或原SID不符時保留`NATIVE_SESSION_CHANGED`，不偷偷建立環境、換RID或改lane。

已有native工作的Agent換版，改走
[`GET/POST .../native-turns/environment`](../README.md#原生-agent-明示換版接續local未部署)，不重跑bootstrap。
原「在新環境接續」按鈕會確認**整個Agent、所有原生對話**的環境更換；
不改草稿、原CID/RID或UNKNOWN，也不順便送出訊息。2026-10-03窄修：確認一次後，只有最新
`CONFIRMED`且`reason=scan_pending`可每2秒以完全相同body續做，含首次最多10次；RID由原bridge接點固定。
UI顯示核對次數；ready／current、其他pending原因、任何錯誤或範圍變更立即停止，上限後由真人再次明確繼續。
這不是自動送訊息或通用重試；UNKNOWN／純讀觀察不會啟動續做，切走再回原Agent／對話也不恢復舊循環。
「核對原新環境」只GET。journal分開保存明示操作與觀察，不回填舊UNKNOWN為歷史成功。
新回合可以使用不同SID，但同一RID的原內容／capture／歷史SID不可改。
明確`AGENT_VERSION_FOLLOWING`／`NATIVE_ADMISSION_PENDING`未接單回覆另記REFUSED，
提供「以原內容重送」的人工動作；重送仍用原RID／內容，拒絕原件不覆寫。
若任何次重送變成真正UNKNOWN，先核原回條，不能借較早的REFUSED再送。
前次ready／current不隱藏純讀的「更新環境版本」，下一版仍須重新查看並明確確認。

Verified against（續掃窄修）：基準`7d61ae9d`，原`NativeInstructionControl.environment_prepare`、
HTML的`followNativeEnvironment`／`run`／`clearEnvironmentViews`。
bridge正例核相同RID、一次create／stop；失聯後即使GET見到pending也保UNKNOWN、不能自動續做。
續做前及2秒等待後都再核最新state與純讀observation，不拿較早scan_pending的ACK蓋過晚到unknown／ready或另一個RID。
observation只能阻止續做，不能替UNKNOWN提供續做資格。直接29個Node案例及完整400個Node案例通過；
後端／bridge相干80個Python案例通過。
詳見[原生接續的分批結果](../README.md#verified-against換版接續)；只本機離線，未操作live或改登入／附件接點。

### 一段對話只走一條路

- 只有**沒有原 command、沒有任何送出意圖**的新對話，才依明選設定走 N2。
- 舊 CID、舊 command、缺完整 command 的未知意圖，一律保留原 Table 路徑。
  不因讀到404或改sidecar而換後端，不把舊RID當成新工作。
- 新模式在本機保存原 command 時，同一筆 SQLite transaction 固定該 thread 的 lane。
  這是「曾用哪個HTTP路徑」的 client 紀錄，不是工作、回合、權限或完成的權威。
  重啟後即使移除明選旗標，已有 native 對話也不會改送舊API。
- 原文字、檔案、附件ref與RID保留；送出前先保存原意圖。HTTP失聯只讀原RID，
  不自動重送、新增RID或重造capture。之後核回原回條時另存唯讀觀察，
  原 submit／cancel／acknowledge 的 UNKNOWN 紀錄不改寫成歷史成功。
  只有明確讀到原回條仍為 `accepted`，才提供人工「繼續原接單」：
  沿同RID／完整原command，N2從原store取回同一capture；不是新接單，且不自動執行。
  404／unknown／started 不能作為這個人工續接的條件。

### 所有進度與歷史都問原 Agent

`web_test_console.native` 沿原TokenVault、固定backend origin、Agent prefix和HTTP client，
使用 [N2真人API](../README.md#n2真人原生-api-入口codeoffline尚未切換-8765)。
不建立 `FixedTask`、`JobStatus`、WorkRegistration或假的job／binding。
server回條才提供RID對應CID／原SID，client不先分配SDK conversation或回合號。
查讀回覆與歷史不啟動模型；API再以唯讀invocation讓Agent讀自己的store。

結果每頁完整核對原CID／RID／part／reference，讀齊後核整體UTF-8 bytes／SHA-256，
才能顯示或下載；不沿舊32 KiB上限截短，不拿分頁當完整答案。
進度只顯示native的accepted／started／completed／unknown／busy_refused；
沒有工具步驟或用量時不填成0。Agent訊息以 `agent_text`／`source` 標示，
不放進使用者原話，也不補造本機human提交。

取消和確認收件都只能由明確動作送出，指定原CID／RID；確認收件再綁原result digest。
取消不等於SDK停止、completed不等於人已收到、delivered也不等於真人已讀。
已有cancel／ack意圖不再次POST；回應未知時透過原RID查讀取得目前紀錄，
原UNKNOWN留下，不另開第二份效果。
原生store缺失、TTL不符、unknown及SID已變更都有白話訊息與原code，
不改走舊provider或空歷史；登入在讀取中改變時，不發布舊回應。

**2026-10-02 未開始回合收尾（LOCAL，未部署）：**
原回條明確為unknown／`NATIVE_TURN_NOT_STARTED`才顯示「這次沒有開始執行，請重新送出」，
並明說原紀錄保留、須用新訊息，不會自動重送；一般unknown不套用這句話。
純GET仍不收尾或啟模型。原CID有已核可接續資料時可明確建立新RID，
原RID的「繼續原接單」仍停用；沒有合法恢復資料不改成空歷史。
接點為`native.NativeInstructionControl.view`與`index.html`的`processingPanel`；
工作台HTTP路徑與既有unknown／未接單行為7案、native前端21案通過，未更新現場8765。

**2026-10-02 模型在任何輸出前明確拒絕（LOCAL，未部署）：**
原回條為 unknown／`NATIVE_MODEL_REFUSED` 時，畫面顯示「模型拒絕了這次請求」，並說明沒有執行工具或其他動作、可以重新送出、不會自動重送（2026-10-04 起文字改為下段）。
這個 code 只在一種情況出現：Hosted 模型直連在交出任何回應之前，就收到 provider 的明確拒絕（4xx，或重試後仍是 429）。
同一段對話可以照 `NATIVE_TURN_NOT_STARTED` 的方式建立新 RID，不限 turn 編號；原 RID 保留，工作台不會自動重送。
接點是 `native.NativeInstructionControl.view` 的 `settled` 與 `index.html` 的 `processingPanel`。
[後端條件與部署順序](../README.md#原生未開始回合收尾local未部署)。

<a id="model-refusal-explicit"></a>
**2026-10-04 模型拒絕改為明確失敗的白話說明（LOCAL，未部署）：**
stage A 新對話選 `gpt-6-luna`，Azure 在輸出前回 400 `unsupported_parameter`（`reasoning.effort`），回條已是 `NATIVE_MODEL_REFUSED`，
畫面卻在回覆位置寫「原回合結果未知」、步驟面板標題寫「原回合結果未確認」。現在三處都改看這個 code：
- 回覆位置與步驟面板說明：「模型服務拒絕了這次請求（gpt-6-luna：不支援 reasoning.effort），沒有執行任何動作。可以換模型再問一次；工作台不會自動重送。」
  括號內的模型取自原回條，原因取自該回合公開步驟裡 Hosted 寫的固定摘要（`Model provider refused: status=… code=… param=…`）；讀不到步驟時只顯示模型名。
- 步驟面板標題：「模型拒絕了這次請求」；該步驟本身也顯示同一個去敏原因。
- 其他 unknown（已呼叫工具、已開始輸出、傳輸中斷、5xx）仍是原本的「結果未知」字樣，判斷不放寬。

同一段對話換模型的條件在 Hosted：見[後端說明](../README.md#vm-直連-responsesadr-0006-5-t4b預設關閉)。
新對話第一回合就被拒時，Hosted 的 `last_turn` 已是 1，所以 `settled` 照樣放行新 RID；
測試：`test_web_test_console_native_environment.py::test_a_refused_first_turn_of_a_new_conversation_does_not_block_it`。
前端：`node --test .\tests\shared_context\test_web_test_console_workbench.cjs` 的
「native model refusal explains that nothing ran and another model may be asked」與
「native model refusal names the model and the provider's sanitized reason from the public step」
（含同一則步驟在一般 unknown 回合仍顯示「結果未知」）；全檔 605 項通過。未更新現場 8765。

<a id="model-lock"></a>
**2026-10-13 已有完成回合的對話固定模型（過渡做法，LOCAL，未部署）：**
stage 第 28 版，Neal 在已有完成回合的對話把模型選單換成 `gpt-5.4-mini`，之後每次送出都被 Hosted 在接單前回 409
`NATIVE_CAPTURE_CHANGED`（`not_dispatched`，什麼都沒執行）；畫面只寫「原因見支援明細」，頁首是「發生問題」。
開關關閉時規則不變：對話的 capture 固定模型，只有還沒有任何 completed 回合時能換（見[後端說明](../README.md#vm-直連-responsesadr-0006-5-t4b預設關閉)）。
這是開關關閉時的做法；伺服器回報可換模型（`responses.model_switch`，見下方[對話中換模型](#model-switch)）時，
`pinnedConversationModel` 一律回 null，下列固定與提示都不出現；被拒訊息的句子照舊顯示（API 讀到開關、回話的 Hosted session
仍可能沒打開，這時換模型照樣被拒，一般錯誤字樣只會叫人看支援明細）。
開關在所有環境打開並穩定後，可以刪掉 `pinnedConversationModel` 及其呼叫點（選單 `change`、`renderModelLockNotice`）。
- **模型選單**：目前對話（native 或 Responses）有一個回合的狀態是 `completed`，就用該回合 `admission.model` 當作固定模型，
  選單一律顯示它（蓋過草稿裡記的模型）。換成別的模型時，選單退回固定模型，輸入框下方顯示
  「這段對話固定用 <模型>；要換模型請開新對話」和按鈕「用 <新模型> 開新對話」；按下就以新模型開新對話（原對話草稿留在原對話）。
  只改本頁狀態，不寫設定、不多打 API。新對話、還沒有完成回合的對話、完成回合讀不到模型、或固定模型已不在清單時照舊自由選擇。
- **被拒的訊息**：`NATIVE_CAPTURE_CHANGED` 且帶 `not_dispatched` 時，回覆位置與輸入框上方都顯示
  「這段對話固定用 <原模型>，換模型要開新對話；這則訊息沒有執行。」（讀不到原模型時寫「原本的模型」），
  頁首是「需要處理」而不是「發生問題」。沒有 `not_dispatched` 的同一 code（重送內容不同、回條比對不符）仍是一般錯誤字樣。
  request ID 規則不變：這則沒有送出，換回原模型後照原流程再送。
- 測試：`test_web_test_console_workbench.cjs` 的「after a completed turn the model dropdown shows the conversation's model…」、
  「before any completed turn the model stays free to choose」、「a NATIVE_CAPTURE_CHANGED refusal says the conversation keeps its model…」，
  在原 `index.html` 上都 FAIL；全檔 641 項通過。未更新現場 8765 或 stage。
- **#821 review 補修（2026-10-13）**：「新對話」按鈕不再把 click 事件當成模型傳進 `openNewConversation`；
  「用 <新模型> 開新對話」沒開成時，開新對話草稿的模型退回原本的值；固定模型的被拒句子用被拒那則訊息自己對話的模型，
  不是目前畫面上對話的模型。

<a id="model-switch"></a>
**2026-10-13 對話中換模型（`FA_NATIVE_MODEL_SWITCH`；程式預設關，stage A／B 第 29 版起、dev、新 azd 環境已打開）：**
開關只在 Hosted definition 上（見[後端說明](../README.md#native-model-switch)）；API 讀所選版本的值，放在 composer 的
`responses.model_switch`，頁面只照它走，不自己決定。目前只有 Responses lane 的 template options 帶這個值；
沒讀到（例如 Invocations lane，或工作台重啟後還沒讀過 options 的既有對話）時當作關閉，照上面的固定做法。
- **模型選單**：打開時任何對話都能自由選；追問的新草稿預設是這段對話目前的模型，不會悄悄換成專案預設。
  「目前的模型」和伺服器一致：最後一個已佔用回合號（`admission.turn_number`）的回合的模型，不論它最後是 completed 還是 unknown
  （`_allocate` 佔用回合時就採用新模型）；接單前被拒（`not_accepted`）或忙碌未執行（`busy_refused`）的不算（`threadModel`）。
  所以換模型那一回合結束成 unknown 後，追問仍預設新模型，不會悄悄換回舊模型。
- **換模型的提示**：送出的模型和這段對話目前的模型不同時，輸入框上方顯示一次
  「已換成 <新模型>；這一回合會比較慢（重新載入對話）。」，那一回合結束、被拒、換對話或再送下一則時消失。
- **放不下**：`NATIVE_MODEL_CONTEXT_EXCEEDED` 且帶 `not_dispatched` 時，回覆位置與錯誤列顯示
  「這段對話太長，<新模型> 放不下；請開新對話或改用原模型。」，模型名取自被拒訊息自己的 `requested_model`
  （bridge 的 task 新欄位，只用來顯示，讀不到時寫「所選的模型」），頁首是「需要處理」。這則沒有送出，request ID 規則不變。
- 測試：`test_web_test_console_workbench.cjs` 的「with model switching on, the dropdown stays free…」、
  「a send that switches the model says once…」、「a switch the conversation is too long for…」、「#821 follow-ups…」，
  在原 `index.html` 上都 FAIL；review round 1 加的「a follow-up after a switched turn that ended unknown…」在修正前也 FAIL；
  全檔 646 項通過。`test_web_test_console_native_environment.py` 驗 `requested_model`。

原生CID的文字檔設定沿Agent原template；附件選單經N2讀原CID的已保存template，
再沿既有Team來源目錄讀metadata，提交仍重核完整來源graph。
原附件／檔案下載不需要虛構舊Session或claim。
原生尚無reset、舊SDK診斷context或operation面時，保持停用，
不借另一個工作ID假裝提供。

### 原生圖片查看與下載（LOCAL，未部署）

捕獲 `fa-image-delivery-v1` 且原回條已有回合號時，可按「查看截圖」，沿既有
`GET /api/instructions/{rid}/images` 及 `/images/{version_id}` 取原PNG。
不是把 native RID 填成舊 binding/job，也不要求最後答案已成功：unknown可查已保存圖片，
但仍不重送原模型工作。缺完整原回條或不在該Agent/thread的RID不猜來源。

原生沒有公開legacy工具事件，因此不捏造「交付成功」來自動載圖；明確按鈕才查原清單，
空清單只說「尚無已確認保存的截圖」。原legacy成功事件的自動圖像流程不变。
N2/bridge核原scope、submit digest、version/PNG bytes；前端另核最多四張、序號、長度、
SHA及response hash後才建立預覽/下載。登入或Agent切換後丟棄晚到回應，沒有公開URL或自動ACK。

**Verified against：** `72db7bc1` 的 `NativeAdapter/NativeInstructionControl.images`、
`Console.instruction_images/instruction_image`、既有images路由及gallery。43個相干Python案含
簽JWT N2、真SDK圖像回呼/下一回合隔離、ASGI native/legacy讀取；Node17案含新圖片七案、
legacy畫廊四案、既有技能草稿六案。這不是瀏覽器視覺／現場8765／Hosted驗收；
[完整結果與限制](../../../docs/agent-runtime-evaluation-baseline.md#2026-10-02c5-native-圖片原件與工作台local)。

### 原生技能草稿查看與下載（LOCAL，未部署）

captured skillify 的原 human 回合有「查看技能草稿」。沿原 native receipt 的 Agent/CID/RID
讀 `GET /api/instructions/{rid}/skill-draft`；「下載原草稿」讀同一路徑的 `skill-draft.md`。
這些動作只讀原 M3 版本，不呼模型、不再保存、不自動批准；原 SID 換版或已結束仍可讀。
Agent 訊息不冒充使用者原話，沒有原 receipt 時不猜 CID 或建立假工作。

畫面顯示目前 M3 的未批准、已生效、拒絕或停用狀態；原寫入回條仍標示
`at-original-draft-write`，不把其 `approved=false` 當成後來的人審狀態。
正文只作文字呈現，原 Team 技能清單／HumanApprover／C2 選入流程不變；「已保存」不表示「已啟用」。
下載在長度、SHA-256 與回應 hash 相符後才交瀏覽器；更換登入或 Agent 後丟棄晚到回應。

**Verified against：** `f79219a0` 的 `native.NativeAdapter/NativeInstructionControl.skill_draft`、
`Console.instruction_skill_draft`、bridge GET 路由、`index.html` 原 scope/auth revision。
真 HTTP 讀／下載測項保原 UTF-8 bytes 且沒有額外 POST；Node 六案核正文、不同 bytes/hash、
拒絕下載、登入晚到及錯 RID，與換版 UI 合併批十案通過。
[完整分批結果](../../../docs/agent-runtime-evaluation-baseline.md#2026-10-02c5-native-skillify-完整-m3-接線local)
只屬本機離線，不代表已更換現場 8765；圖片交付另片。

**2026-10-02 測試等待修補：** main `713b7d9b` 的原草稿下載案有時在真 WebCrypto
雜湊尚未完成時，就讀取不存在的下載檔。`0326e2b2` 僅用 Node 原生
[`mock.method`](https://github.com/nodejs/node/blob/v24.19.0/doc/api/test.md)
觀察原按鈕觸發的 handler，等待它實際回傳的 Promise 後才核下載、拒絕及登入晚到。
不替換 handler／雜湊實作、不加 sleep 或延長 timeout；產品 HTML、原 body/hash、
M3 審閱與批准不變。原六案及受控延後真雜湊的六案均 PASS；
[新結果](../../../docs/agent-runtime-evaluation-baseline.md#2026-10-02recovery-啟動與草稿非同步測試修補local)
不回填先前十案、十七案或他線 270/271 的原結果。

### Verified against（N2 工作台）

- 基準：已審真人API `4d5689d3`，普通整合main `8d378a21`；shared wire僅同步
  N1固定`67cb0bc6`的原檔，沒有在client加2hop或每turn100限制。
- 實際接點：`instructions.InstructionConnection/InstructionStore`、`native.NativeAdapter/NativeInstructionControl`、
  原`Console`分流、`PullAdapter.send`僅限native的202／分頁selector，以及原`index.html`呈現。
- `n2-ui-03`：新模式與原journal／舊凍結兩回合HTTP範圍22案通過；
  `n2-ui-04`為34案通過、1個測試用了不存在的new路由，修成既有`/{thread_id}/next`後，
  `n2-ui-05`包含續問、UNKNOWN、附件目錄與舊對話目錄的15案通過。
  重複範圍不相加；全部為本機離線來源，不是Hosted或模型品質結果。
- `n2-ui-06`三案通過，涵蓋人工accepted續接、舊無lane資料與登入變更；
  最後`n2-ui-07`包含完整新client及原journal／凍結兩回合29案通過。
- 既有Node工作台呈現案例加3個native案例通過，保留原Clawpilot樣式；
  原生Agent訊息不標「你」、完整答案不截斷，查讀不自動ACK。
- 額外以只讀loopback合成頁在真瀏覽器確認；初次發現Agent來源標籤被原無障礙CSS裁掉，
  已改為可見文字與靠左訊息，不能只靠DOM文字存在宣稱視覺有區分。
- 整合N1已合main `5dcbce0c` 後，`n2-n1-official-local-01`一案通過：
  8765薄client→真signed N2 API→官方core 2.1.0 local task／State store，
  原CID兩回合、checkpoint接續、大答案多頁、client重啟只讀與原帳本startup。
  只有SDK／模型I/O是合成替身；沒有真Hosted或模型呼叫。最終合成頁console無錯誤。
- 兩軸同報一個P2：歷史游標前進後，中間Agent答案暫時讀不到會一直漏顯。
  `n2-history-red`以HTTP一次失聯重現；改為沿已讀歷史補讀未驗完整答案，
  已驗答案仍重用cache、不重送模型。`n2-history-green`的正常／失聯兩案、
  答案完整性、登入變更與官方local串接共5案通過；
  修復版`cc701fe1`原Standards／Spec只複核delta，均0 P1／0 P2。原批次結果保留。
- 尚未更換8765、啟用雲端旗標或呼叫模型；T5與N1 runtime真實採用沿各自工作線。

## 即時看助理 VM 桌面（VNC D3，live 已驗證）

- 左側助理清單下方新增「即時桌面」面板：按「連線」會看到目前對話所在 VM 的桌面（縮放預覽），可切換全螢幕，
  也能直接用滑鼠和鍵盤操作。照 Grokbot 作法不加輸入鎖，Agent 會繼續工作。切換助理、對話、登出，或同一對話換到
  另一台 VM 時自動中斷；不自動重連，平台約 30 分鐘會結束連線（1001），需要時再按一次。連哪一台 VM 的規則
  （2026-10-07 起跟著正在跑的回合）見[最上方的說明](#desktop-running-session)。
- 路徑：瀏覽器 noVNC → 8765 `WS /api/agents/{agent_id}/desktop?session=<hosted_session_id>` → API
  `/desktop/v1/agents/{agent_id}/sessions/{session}`（D2）→ Foundry `invocations_ws` → Hosted `x11vnc -inetd`（D1）。
  8765 只負責本機邊界：WebSocket 不經過 HTTP middleware，所以 handler 自己核 Host、Origin 與 `fa_console` cookie；
  通過後用目前登入者的 bearer 連 API（瀏覽器的 WebSocket 不能帶 Authorization）。Team 主人、Agent 與 session
  的檢查都在 API。8765 不留紀錄、不重試。
- 面板會用中文說明關閉原因，例如：沒有開著的桌面（`DESKTOP_NOT_RUNNING`）、VM 沒在執行，不會為了看桌面喚醒
  （`DESKTOP_VM_NOT_RUNNING`）、只有 Team 主人可看（`DESKTOP_VIEW_DENIED`）、連不到桌面通道
  （`DESKTOP_VIEW_UPSTREAM_REFUSED`，例如尚未部署或 endpoint 尚未開 `invocations_ws`）。
- 沒連線時不顯示黑色畫面，改在同一位置用灰字顯示上面的說明。面板打開時讀一次所選助理目前的模板
  （`GET /api/agent-template`）：每次打開、換對話，或在本頁選定新模板後都會重讀；模板沒有 `fa-desktop-v1` 時顯示
  「這位助理沒有開啟桌面能力。」並停用「連線」。讀不到模板或這位助理沒有模板工具時不猜（記為未知、不重複讀），
  「連線」照舊，由 API／Hosted 的回覆決定。
- noVNC：內附 `@novnc/novnc` **1.7.0**（2026-04-28；npm integrity
  `sha512-ucEJOx4T2avIRCleodk7YobZj5O2Ga2AeLfQ69A/yjG9HHba2+PDgwSkN3FttrmG+70ZGx21sElNFouK13RzyA==`）
  的 `core/`、`vendor/pako` 與授權檔（核心 MPL-2.0，pako MIT）。檔案未修改，放在 `novnc/`，由 `/novnc/*.js`
  以固定的 `text/javascript` 提供，不依主機的 MIME 設定。CSP 只多開本機 `ws://127.0.0.1:<port>` 與游標用的 `data:` 圖片。
- **游標（2026-10-04，CODE/offline；Hosted 端要新 Agent 版本才生效，部署另行核准）**：原本畫面裡看不到游標。
  - 根因：x11vnc 對支援「游標形狀」更新的 viewer 只送形狀、不把游標畫進畫面，游標位置要靠另一種「游標位置」更新
    （[x11vnc(1)](https://github.com/LibVNC/x11vnc/blob/master/x11vnc.1) 的 `-cursor`、`-nocursorshape`、`-cursorpos`）。
    內附 noVNC 1.7.0 只宣告游標形狀（`core/rfb.js` 的 `pseudoEncodingCursor` 與 VMware cursor，`core/encodings.js`
    沒有游標位置），而且只把形狀當成使用者自己滑鼠停在畫布上時的樣子（`core/util/cursor.js`）。所以 Agent 移動的
    游標從來不會出現，使用者滑鼠不在畫布上時也沒有游標。根視窗游標是什麼形狀都不影響這個結論。
  - 修法：Hosted 的 x11vnc 加 `-nocursorshape`（`shared_context/behavior_desktop_view.py`），把桌面自己的游標畫進畫面，
    Agent 移動也看得到；工作台設 `rfb.showDotCursor=true`，伺服器不送游標形狀時，用小圓點標出使用者自己滑鼠的位置。
    `showDotCursor` 在 1.7.0 只移除了舊的 constructor 參數，屬性仍在，官方 `app/ui.js` 照用
    （[1.7.0 release](https://github.com/novnc/noVNC/releases/tag/v1.7.0)、
    [1.6.0 API 說明](https://github.com/novnc/noVNC/blob/v1.6.0/docs/API.md)）。
  - 界線：x11vnc 已在 image 內，不需要新套件，但 `behavior_desktop_view.py` 屬 Hosted 程式；只部署工作台時，舊 Agent 版本
    仍送游標形狀，畫面大致與原本相同（只在伺服器沒送或送了透明游標時多一個小圓點）。依官方文件與原始碼判斷，
    未在 live 或本機 x11vnc 實測；live 驗收看「Agent 操作時畫面裡有游標移動」。
- 驗證：`tests/shared_context/test_web_test_console_desktop.py`（CSP、只提供 noVNC JS、他 Origin／無 cookie 不連 API、
  selector／登入／助理檢查、owner bearer 與雙向轉送、上游被拒、頁面關閉原因）。另外在本機用真瀏覽器跑過 harness：
  真的 `install_desktop_routes`、同樣的 CSP、替身 RFB 桌面。結果：noVNC 模組載入、畫面顯示、4404 原因顯示，
  滑鼠與鍵盤事件都送到桌面。這不是 Hosted、x11vnc 或 Azure 證據。
- **live 驗證（2026-10-02 00:08～00:16 台灣時間，main `b3f89f4e`，fdbb 執行）**：
  - A 在一個回合內用桌面工具開了 l3afpad、輸入 hello；回合進行中從 8765 連上 viewer，路徑為 API broker →
    `invocations_ws` → x11vnc。RFB 003.008、安全類型 None、1280×800；截圖看得到 `*hello.txt` 正在輸入。
  - 送出一次滑鼠移動後收到 14 次畫面更新。
  - 連線撐了 303 秒，最後由 client 主動關閉，**沒有被 Container Apps 的 240 秒切斷**；回合結束後桌面仍在。
  - test agent 的 active session 沒開桌面時，3.3 秒內收到 `4404 DESKTOP_NOT_RUNNING`。
  - 尚未證明：約 30 分鐘的 1001 與手動重連。viewer 連著時 session 的 `last_accessed` 沒有更新，所以**不能**用它
    證明 WebSocket 會讓 session 保持 active，費用影響仍待觀察。
  - 部署者的證據放在其 session 檔案 `i-p1/desktop-share-b3f8/`（含 SHA256SUMS）；本文只摘要去敏結果。

## 現場工作台（2026-09-28）

**2026-09-29（Hosted47）：** 原部署線19:43先備份journal（`integrity_check` ok，計數config1／intents95／instructions62／threads33／commands54），再以固定`f4589df` session快照沿同config／journal重啟8765，仍detached常駐。
換版帶入#166的委派結果唯讀面板（A3）與#170的模板加入已批准skills／capability。19:45經8765 session把模板改選selection `15e869c2…`（加開delegate、記憶候選、skillify、skills、playwright與Linux checkpoint），smoke對話也經同一session送出。
Linux checkpoint閒置恢復後保存失敗，20:04同方式縮回selection `5c5d25d6…`；委派沒有派出、Playwright navigate失敗，20:13再縮回selection `bb7c4010…`；21:33 S2再釘選使用者批准的skill（`e5ae6897…`）。都只影響之後的新對話，四次select都沒有呼叫模型，journal原有列都保留。
以下較早內容保留當時結果；實際版本與限制見[f4589df採用紀錄](../../../docs/progress.md#2026-09-29main-f4589df採用hosted47v46開機失敗與capability縮回)。

**2026-09-29（Hosted45）：** 原部署線16:41先備份journal，再以固定`deafbf1` session快照沿同config／journal重啟8765，改為detached啟動；`web_test_console`程式與`fd76890`相同，換版只帶入新的`shared_context`。
16:43經使用者本人的8765 session把模板改選selection `a78c59a0…`（加開Linux checkpoint、`sdk_start` v1與WebIQ-MCP、`fa-desktop-v1`、playwright），smoke對話也經同一session送出。
Linux checkpoint在Hosted閒置恢復後的回合保存失敗、Playwright navigate失敗，17:20同方式縮回selection `e25969c1…`（移除checkpoint與playwright），只影響之後的新對話。兩次select都沒有呼叫模型，journal原有列都保留。
以下較早內容保留當時結果；實際版本與限制見[deafbf1採用紀錄](../../../docs/progress.md#2026-09-29main-deafbf1採用hosted45checkpoint與playwright縮回)。

**2026-09-29（Hosted44）：** 15:41的v44新對話smoke仍經`7a1bdcf`快照送出。該快照的`TemplateSettings`沒有`capabilities`，模板inspect回400 `TEMPLATE_INVALID`（未送出）。
原部署線因此停機，15:43以sqlite backup備份journal（`integrity_check` ok，計數config1／intents83／instructions50／threads23／commands46），再以固定`fd76890` session快照沿同config／journal重啟8765，仍detached常駐；換版後`LOGIN_REQUIRED`，重新連線後才送出模板select。
15:49經使用者本人的8765 session把模板改選selection `a9f39231…`（加開WebIQ-MCP、`fa-desktop-v1`、playwright），capability smoke三個新對話也經同一session送出。
兩個MCP工具在SDK session內不可呼叫，16:01同方式退回selection `34abb3e7…`（只留`fa-desktop-v1`），只影響之後的新對話。`web_test_console`程式與`7a1bdcf`相同，換版只帶入含`TemplateSettings.capabilities`的新`shared_context`。
以下較早內容保留當時結果；實際版本與限制見[fd76890採用紀錄](../../../docs/progress.md#2026-09-29main-fd76890採用hosted44desktop-capability)。

**2026-09-29（Hosted43）：** 原部署線停機後於10:20以sqlite backup備份journal（`integrity_check` ok），再以固定`7a1bdcf` session快照（#146[SK-10草稿結果的進度相容](#sk-10-草稿結果的進度相容)）沿同config／journal重啟8765；
原件`b7a1-workbench-switch.json`核得前後計數都是config1／intents76／instructions43／threads16／commands40；SK-10草稿對話的progress由502變200、`stage=saved`（純讀、未送出）。
新對話由後端選Hosted43；API之後換到2c6fce1，console程式不變。14:29經使用者本人的8765 session把新對話模板改選selection `269291af…`（加開Team附件`text_attachment`，只影響新對話、未呼叫模型）。
memory smoke的對話回合經同一8765 session送出，memory設定、提案與bridge各stage由部署線腳本沿使用者CLI登入送出，不經頁面按鈕。
14:57起8765改為detached常駐（同快照／config／journal），不再隨部署工作階段的shell停止；停止程序不刪journal或雲端成果，重啟仍須沿原config／journal。
以下較早內容保留當時結果；實際版本與限制見[2c6fce1採用紀錄](../../../docs/progress.md#2026-09-29main-2c6fce1採用hosted43memory-smoke)。

**2026-09-29：** 原部署線停機後以sqlite backup備份journal（備份`integrity_check` ok），再以固定`9d5e086` session快照（含#137[在新環境接續原對話](#在新環境接續原對話)與拒絕／unknown區分）沿同config／journal重啟8765；
原件`d9d5-workbench-switch.json`核得前後計數都是config1／intents68／instructions37／threads14／commands34；換版後`LOGIN_REQUIRED`，由總協調在使用者頁面按「連線」。
新對話由後端選Hosted42。原部署線用使用者本人的8765做新對話兩回合，並對前一天的v41 smoke對話做一次「在新環境接續」：舊對話送出明確failed，準備新環境只POST 1次、未呼叫模型，下一則訊息恢復前文。
使用者授權後，原部署線09:27經同一session把新對話模板改選`fa-instruction-history-skill-draft-v1`（selection `3ff96398…`，其他設定不變、只影響新對話），1回合草稿後端存成；
**已知缺漏**：工作台的`ConversationTurn`（extra=forbid）不認得`skill_draft_outcome`，讀該對話progress回502 `CONVERSATION_INVALID`，後端資料與回條正常；修補進行中，修好後只換8765快照。
以下09-28內容保留當時結果；實際版本與限制見[9d5e086採用紀錄](../../../docs/progress.md#2026-09-29main-9d5e086採用hosted42在新環境接續)。

原部署線19:53停機後以sqlite backup備份journal，再以固定`c916a0f` session快照（含#129[版本已變更提示](#hosted-版本已變更的原對話)）沿同config／journal重啟8765；
原件`c916-workbench-switch.json`核得換版前後計數都是config1／threads13／intents66／instructions35／commands32，頁面HTML與快照一致；換版後`LOGIN_REQUIRED`，由總協調在使用者頁面按「連線」。
更早16:17先備份journal，再以固定`758e83e` session快照（含#121[單次人工批准](#原操作的單次人工批准)卡片）沿同config／journal重啟8765；
原件`b758-workbench-switch.json`核得換版前後計數都是config1／threads12／intents65／instructions34／commands32，頁面HTML與快照一致；換版後由使用者重新登入。先前14:33換`166a1bb`快照時同樣沿原config／journal。
程序附屬原部署工作階段，該工作階段結束即停止，不是常駐服務；停止程序不刪journal或雲端成果，重啟仍須沿原config／journal。

新對話由後端選Hosted41，舊對話沿原版本／SID。使用者批准後，原部署線以使用者本人登入的session於15:47、16:18各一次選定新對話模板，
目前為`fa-instruction-history-v1`＋`user_text_file`／`turn_origin`／`model_capacity`；只影響之後的新對話，第一次送出時採用當下的選定。
原turn4、turn8已讀回為`failed`並顯示版本已變更、不能續問；先保留舊頁面草稿再重新整理，改開新對話。
09-27為Hosted37試用準備的空白草稿只是本機草稿、沒有Hosted session；依當時回條，首次送出採送出當下的路由。

隨快照上線的UI入口包括#101[新對話設定的人工選擇](#新對話設定的人工選擇)、#108[文字來源附件選擇](#文字來源附件選擇)、
#113[使用者選取的UTF-8文字檔](#使用者選取的utf-8文字檔)、#121單次人工批准、#129版本已變更提示，以及c18起已含的#85[下載原答案文字](#下載原答案文字)與#90[原回合的宣告模型容量](#原回合的宣告模型容量)。
後端的Team附件、Skills、checkpoint與`sdk_start`仍未開；原部署線除兩次模板選定、c916兩回合smoke及舊turn4／turn8各一次進度讀取外，沒有做附件、文字檔、下載、容量畫面或批准的live操作。
各節原本的本機結果照舊保留，不改寫成live驗收；實際版本與限制見[採用紀錄](../../../docs/progress.md#2026-09-28main-c916a0f採用hosted41版本漂移修補m3-route)。

## 簡單聊天介面

2026-10-01本機修正：處理步驟的固定上限／交接原因code會顯示白話文字及原code，
包括單回合模型上限、工作額度、交接期限／訊息數，以及「訊息已接收，B 的結果尚未確認」。
不再把這些已知原因只列成「結果未確認」；其他沒有可信原因的SDK錯誤仍維持未知。
已核對的`fa-assistant-notice-v1`模型上限失敗結果亦顯示原因；未核對內容不得冒稱已確認。
沿原progress／結果資料，不新增發送、重試或授權操作，也不由錯誤code推定B已停止。
實際採用仍須更新工作台快照；此處不是已更換8765的回條。

左側是 **Agent 清單**，不是對話清單；未提供多 Agent manifest 時，維持原可信配置的單一助理。
提供下節完整配置後，才使用 server 目錄及固定 Agent 路由，不建立假 Agent。右側顯示訊息，輸入框固定在底部。送出以後清空輸入框，原文保留在
訊息與原 request；失敗或尚未確認時保留草稿，不自動重送。

- **Enter** 送出，**Shift + Enter** 換行；中文輸入法選字的 Enter 不送出。
- 回覆完成後直接輸入追問。UI 只在使用者按送出時，沿既有 `next` 取得一個新訊息 ID，
  然後提交一次；不需要另外按「追問或補充」。`next` 失敗時不提交。
- 右上角 `⋯` 收納新對話、reset、更新回覆、模型清單、歷史對話及 session ID。
- 主聊天畫面移除原本的編號流程、工作表格、結果／確認收到面板及技術告示。
  必要的狀態、公開處理步驟與舊回條操作留在「支援明細」抽屜，不自動執行 ACK。
- 手機版收合 Agent 清單；讀取進度不反覆重建相同訊息，也不在使用者閱讀上方內容時強制捲到底部。

### 多 Agent 配置與交接（CODE/offline）

以 server 的長期 `agent_id` 選擇助理，不把 OID、Hosted 名稱或目錄順序當成 Agent ID。
同一登入只供相同 backend origin、tenant、human、delegated client、audience 與 Team 的配置共用；
Agent 切換不會換帳號、產生 B policy 或新增權限。每個 HTTP 請求以 `X-Console-Agent` 固定自己的
`Console`，不是共用一個可變的「目前 Agent」。後端仍逐次核對身分與當前授權。

部署線須提供已核准的 `fa-console-agents-v1` manifest；用既有 CLI 匯出真 schema：

```powershell
python -B -m web_test_console schema --configuration agents
python -B -m web_test_console serve `
  --config '<原預設助理 standing-v3 配置>' --config-sha256 '<原檔 SHA-256>' `
  --journal '<同一份原 journal>' `
  --instructions '<原 instructions sidecar>' --instructions-sha256 '<原檔 SHA-256>' `
  --agents '<部署線核定的 manifest>' --agents-sha256 '<manifest 原檔 SHA-256>' `
  --port 8873
```

這是接線用法，不是操作原 8765 的批准。manifest 上限64 KiB，必須配對原 bytes 的 SHA，包含：

| 欄位 | 來源與限制 |
| --- | --- |
| `default_agent_id` | 明確對應舊無 prefix 入口；須與 server `PublicAgentDirectory.default_agent_id` 相同 |
| `agents` | 1～16份明確連接資料（`MAX_TEAM_AGENTS`）；Agent ID及解析後的journal路徑不可重複 |
| `agent_id` | server Agent ID；非預設配置還須與原 standing policy 的 `agent_scope.agent_id` 一致 |
| `config_path`／`config_sha256` | 該 Agent 完整既有配置的絕對路徑與原檔 SHA；不能複製 A policy 充當 B |
| `instructions_path`／`instructions_sha256` | 該 Agent 既有 sidecar 的絕對路徑與原檔 SHA；內部 `configuration_sha256` 仍核對解析後的 config digest |
| `journal_path` | 該 Agent 獨立持久 journal 的絕對路徑；預設那份必須就是 CLI 原 journal |

原配置、sidecar及journal皆不改寫內容。已存在的journal先以唯讀方式核其原config digest；
不能把另一份配置的journal接到B，也不得換空journal躲過UNKNOWN。B配置缺失、hash或journal不符、
目錄404／403／不可用、預設映射不符時，對應操作停用；不fallback A，不用舊目錄放行。
server 目錄可列最多16位（`MAX_TEAM_AGENTS`），本機 manifest 也最多16份連接。server 標為 `ready` 但沒有本機連接的 Agent 顯示 `AGENT_CONFIGURATION_MISSING` 並停用；server 標為 `unavailable` 的不列在左側清單（2026-10-06 起，例如永遠不會被呼叫的 executor 佔位 target），只列在「⋯ → 支援明細 → 目錄中未列出的助理」並附 server 原因（例如 `AGENT_SCOPE_MISSING`），仍不能被選用；預設助理與建立中的（`AGENT_ONBOARDING_IN_PROGRESS`）例外，照舊列出並停用。兩者都不套用其他 Agent 的配置。新增本機連接見下方「新增助理」。
登入後重新載入頁面時，若 bridge 上次讀目錄已超過 60 秒（`DIRECTORY_TTL_SECONDS`），會先重讀一次，所以剛退役或剛新增的助理不必再按「更新助理目錄」；讀取失敗就顯示目錄錯誤，不沿用舊目錄放行。
API prefix也涵蓋既有`/behavior/v1/...`的狀態、操作預覽／決定、pull／ACK及圖片。
原相對command與request ID留在原journal，送出時只經已核對的固定Agent prefix；不搬移或重寫舊意圖。

頁面以Agent＋對話保存各自草稿、附件及選擇；pending與錯誤不因另一Agent使用相同本機ID而共用。
切換Agent、手動重新登入、登入失效或對話改變會隔離舊回應及其`catch`／`finally`，不讓晚到回應清掉新草稿、busy狀態、
模板或下載。預設助理只由明確目錄欄位選定；已選B變成不可用時保留B與草稿，不自動跳A。

「查看交接紀錄」只GET原Agent／binding的`AgentMessageList`，再核對Agent、CID、binding及訊息自己的工作邊。
顯示誰交給誰、原工作、已保存／等待／unknown／不另提醒的理由；不讀私有content、OID、checkpoint或推理。
原工作連結只開啟本機已有紀錄；沒有本機紀錄的peer工作只顯示ID，不偽造submit。
`queued`不代表模型已啟動，B的`completed`不代表真人收到；final與ACK仍沿原工作的既有回條。

#### Verified against

- 本片從已發布 `a3ac6c5` 工作；共用DTO取API線已發布 `434bf8e` 的純wire單檔，未修改其格式。
  CLI／`AgentConnections`／`AgentConsoles`、原`StandingPolicyReader`與逐請求FastAPI dependency是實際入口。
- 真B policy／signed API的選版先重現`TEMPLATE_SELECTION_RESPONSE_INVALID`；client改用原
  `policy.scope_ids`／`agent_id`核ID與回條。B記憶回合的狀態也核真正觀察，而非僅HTTP200；
  admission改用同一scope。原A命名與hash不變。
- `test_web_test_console_agents.py`涵蓋固定檔案hash、缺配置、兩client、相同task ID、錯目錄、404／403與journal不改。
  `test_web_test_console_agent_policy.py`涵蓋真正B選版、接單、記憶觀察、UNKNOWN的新bridge續查及唯讀交接。
- Node同一工作台測項已涵蓋Agent切換、同ID草稿／pending、晚到送出及進度、明確預設、不fallback、
  交接語意與scoped下載；同時保留原聊天／模板／取消／操作控制。結果來自本片WIP，原FAIL與fixture錯誤另存，
  不回填成final commit重跑；未啟動SDK、模型、Hosted、Azure或原8765。
- 本片完整接線WIP的最小Python批次13 PASS，單一共用頁面DOM批次216 PASS；
  阻擋Copilot SDK匯入的console冷啟動PASS。該批API目錄／訊息清單使用固定共用DTO的合成回應，
  B的policy、選版、接單、記憶與重建則使用真正本機API／Table producer；不等於B已完成live啟用。
- 審查另補兩個復原情境：A送出期間切B再切回A，原操作完成後只純讀目前對話，不套用晚到畫面；
  目錄失效後重新登入，先核新回應的目錄與登入狀態，不讓舊停用狀態阻擋復原，也不切換到A。
  兩者均先直接RED；修後共用DOM220 PASS。原Python與216案的WIP來源保留，不倒標為新提交重跑。
- 普通整合API線已發布 `d6b93ae` 後，peer fixture改走真正目錄、`default_agent_id`、
  A/B prefix路由及原工作訊息GET，不再在傳輸替身移除prefix；六個直接接線案通過。
  A的binding配B prefix仍403，B選版／記憶觀察／UNKNOWN重建保持。含訊息的格式及拒絕反例仍以合成
  metadata測client核對；沒有啟B consumer、模型或live，不能據此宣稱完整A→B→A驗收。

#### 新增助理（CAP-06 PR5，CODE/offline）

**尚未部署，也未操作原 8765。** 以 manifest 啟動時，左側出現「＋ 新增助理」：只填名稱與角色說明（上限2048 bytes，畫面顯示剩餘 bytes）。

- **一律走已登入的預設 Team 入口。** bridge 只做薄路由：`POST /api/agents/onboarding`（strict JSON，只收 `display_name`、`role_prompt`，PR6 起可再一起帶 `capabilities`、`skills`）、
  `GET /api/agents/onboarding`、`GET /api/agents/onboarding/{rid}`（純讀 server 回條）、`POST /api/agents/onboarding/{rid}/continue`（body `{}`）。
  保留 Host、Origin、cookie、CSRF 檢查；只有建立路徑有 body 例外。瀏覽器塞路徑、policy、request ID 或其他欄位一律回 400，不送 server。
- **本機只記原請求。** 原 journal 旁的 `agents/onboarding.sqlite3`（沿用同一個 `Journal`，phase `agent-onboarding`）只保存 rid、完整 command、它的 hash
  與最後一次觀察到的 server 結果；不另存可自己往前推的建立進度。失聯或重開 8765 後仍查同一個 rid，續做時重送同一個 rid、同一份 body
  （server 定義的冪等續做），不換 ID、不另建第二位。還有一筆未完成時不能新建。額滿顯示 server 回的 `limit`，不留下本機連線。
- **ready 後才建本機連線。** 受控層讀 server `GET /work-policy/v1/agents/{agent_id}/console-connection`（只給該 Team 的 active 主人、只給 ready Agent、不寫列），
  核對 tenant／human／Team 一致且 policy 不是 A 的，再在固定目錄 `agents/<sha256(agent_id) 前32字>/` 以 create-only 寫 `config.json`、`instructions.json`，
  journal 路徑為同目錄 `journal.sqlite3`（每位 Agent 自己一份）。目錄名用 hash，避免 Windows 路徑字元問題。同名檔已存在且內容不同時回
  `AGENT_ONBOARDING_LOCAL_CONFLICT`，不覆寫。
- **新 Agent 預設走 native（2026-10-02）。** `instructions.json` 寫入 `state_profile="foundry-native-v1"`（`NEW_AGENT_STATE_PROFILE`），
  和 A、B 的 fresh export 一樣，新對話走 native；模板選了 Responses 時仍以模板為準。已連結的 Agent、既有對話與 lane 不變。
  若舊版 8765 已寫出不含 `state_profile` 的 `instructions.json`、但 journal 沒記到完成，續做時沿用那份舊檔（該 Agent 仍走 job-bound），
  不會變成永遠的本機衝突。
- **連結完成後自動準備對話環境（2026-10-02）。** 本機連線確認後，8765 用目前登入的原 owner 對新 Agent 呼叫一次
  `POST /agents/{agent_id}/work-policy/v1/native-turns/prepare`（body 只有一個固定的 `request_id`，存在本機 journal 的
  `agent-native-prepare` 階段），再讀 `GET .../native-turns/preparation`；讀回 ready 才顯示「可以開始對話」。
  - 讀回 409 `NATIVE_SESSION_CHANGED`（確定還沒準備好）或 prepare 被拒：顯示原因與「重試準備」，按下仍用同一個 `request_id`
    （伺服器端 prepare 對同一 ID 冪等，已準備過就回原結果）。
  - 讀不到結果：顯示「再查一次」，只讀 `preparation`、不重送 prepare。
  - **自動再試**：新 Agent 剛建好時，Hosted 版本約需 3～4 分鐘才可讀取，第一次常常還沒好。結果不明，或讀回確定還沒準備好
    （409 `NATIVE_SESSION_CHANGED`、503 `AGENT_VERSION_FOLLOWING`）時，頁面每 30 秒自動處理一次：不明只讀；還沒準備好用同一個
    `request_id` 重送 prepare。以第一次準備的時間起算最多 5 分鐘（由伺服器端的 `auto_retry` 決定，重新整理頁面不會延長），
    之後停下，顯示原因和按鈕。401 不自動重試，直接顯示「請重新登入」；本機的 403（例如還沒替新 Agent 指派角色）也不自動重試，
    顯示「需要部署線補角色」（遠端 stage 的 403 見下方「stage 等權限生效」）。
  - 已 ready 的不會再送。面板沒有正在建立的助理時，會優先顯示最近一位已連結但還沒準備好的助理（含舊版 8765 連結、
    還沒準備的，按鈕是「準備對話環境」），所以之後又新增一位也不會讓它的按鈕消失。
  - 新 Agent 的版本沿用 A 的 definition（`copy_source`／`original_source` 只去掉 probe 變數），所以 `FA_NATIVE_TURNS` 會一起帶上。
  - 準備結果存在伺服器端，在本機準備好的助理，從遠端網頁也能直接對話；反過來也一樣。
- **新對話的 lane 沿用 A。** 建立時記下 A 目前新對話用的 `state_profile`：A 已切 Responses 時，新 Agent 自己的選版也是
  Responses；A 還是 native 就維持 native。做法比照 `turn_origin_profile`，只影響之後建立的 Agent。
- **不用重啟就能選用（2026-10-03，本機與遠端共用）。** 連線完成後，新 Agent 直接加入執行中的 manifest，頁面隨即重讀 Agent 目錄。
  加入前的檢查和 manifest 項目一樣：tenant、真人、client、audience、Team 都要和預設 Agent 相同，SHA 也要一致。
  - 重啟時，從 onboarding 紀錄中 CONFIRMED 的連線把新 Agent 補回，所以用原本的 manifest 重啟也看得到。紀錄讀不到時不會擋住啟動，只是那位 Agent 顯示為未設定。
  - `agents/manifest-<sha前16字>.json` 照舊用 create-only 寫出，留作紀錄；原 manifest、原 journal、原 config 都不改。
  - 遠端模式下，新 Agent 的 `config.json`／`instructions.json` 寫好後，要先持有 lease 存進 Blob，才在 journal 記成 CONFIRMED。存不進去就回 `REMOTE_STATE_UNAVAILABLE`（保存檔已達上限時是 `REMOTE_STATE_FULL`），連線不算完成；之後用同一個編號續做。
- **stage 等權限生效（2026-10-03，總協調 00:33 決定）。** stage 的 API 會在 ready 前自動指派新 Agent 的兩個 project 角色（#468／#470）；但官方說明 managed identity 的角色可能要數小時才生效。
  - 所以遠端模式下，prepare 回 403 時，每 10 分鐘用同一個 `prepare-…` 編號重送，最多 2 小時（從第一次準備起算），畫面寫「角色已自動指派，正在等權限生效」。403 是沒有副作用的拒絕，伺服器端對同一編號冪等。
  - 本機（dev 沒有自動指派）的 403 照舊不自動重試，顯示「需要部署線補角色」。401 一律請重新登入。
- **server 端配合修正。** 目錄的 registry Agent 名稱改用 onboarding 時的 `display_name`；onboarding 還沒到最後 ready 前，目錄顯示
  `unavailable`＋`AGENT_ONBOARDING_IN_PROGRESS`，避免選到後才回 `ROLE_PROMPT_MISSING`。
- **補設中（#468，預設關閉；stage 已開）。** server 開了 endpoint 協定或角色補設、而還沒讀回成功時，202 回應帶 `reason: "AGENT_SETUP_PENDING"`。
  畫面顯示「已建立，角色補設中」並說明這是正常的，續做間隔從 2 秒改為 30 秒（權限剛開通時可能要等數小時，避免一直打 ARM），仍用同一個 rid、
  同一份 body，不另建。

測試：`test_web_test_console_agent_onboarding.py`（失聯重開續查同一 rid、額滿、瀏覽器塞欄位、固定目錄與舊檔不變、不用重啟即可選用並在重啟後由紀錄補回、遠端先存 Blob 才 CONFIRMED、stage 403 每 10 分鐘同一編號最多 2 小時、
新 Agent 的 native `state_profile`、舊版已寫檔沿用、自動 prepare 的成功／拒絕後同 ID 重試／回覆遺失／結果不明只查）、
`test_agent_onboarding.py`（目錄名稱與 ready 時機、console-connection 權限與不寫列）、Node 工作台的「＋」面板兩案。

**PR6：勾選 Team 已批准的工具與 skill（CODE/offline）。** 面板多了「工具／MCP」與「Skill」兩組勾選，資料只來自
`GET /api/agents/onboarding/options`（2026-10-05 第 16 版起，打開「＋ 新增助理」時才讀，見[打開頁面少讀](#lighter-page-load)）：它走預設 Team 入口，回 server 的 `agent-template/options`（Team 已批准的 runtime 能力與 skill）
和 A 目前生效的選擇（`agent-template` 的 `settings.capabilities` 與 `skills`），預設勾選等於 A 的選擇，不改也能直接建立。
新助理一律使用 `NEW_AGENT_PROFILE`（2026-10-04 起是 `fa-instruction-agent-role-v4`，可選 skillify）。回應另附 `unsupported`：這是 server 選擇規則（`check_selectable` 與
`base_supports` 共用同一套判斷）認定該範本無法組合的能力。新版能組合目前所有內建能力，所以這份清單目前是空的；舊的 agent-role v1 才不能組合 `fa-skillify-v1`。
這些能力不會帶進預設勾選，面板上顯示為停用並附原因，所以照預設按「建立」不會再收到 422 `CAPABILITY_BASE_UNSUPPORTED`。
server 端照舊明確拒絕，不會默默過濾。
能力依 `kind` 分成 MCP（`mcp-api`／`mcp-hosted`）與內建工具（`fa-builtin`）。前端先擋超過12項工具、超過4個 skill、勾了 skill 卻沒勾
`fa-skills-v1`；目錄讀不到時停用送出並顯示原因。送出時依 `entry_id`／名稱排序，原樣放進 command（`capabilities`、`skills`），
bridge 只核欄位形狀，是否在目錄內、sha 是否相符一律以 server 為準（拒絕視為 S0 前決定，可換新編號）。若建立後 Team 目錄才變，
server 回 `AGENT_ONBOARDING_SELECTION_STALE`（不是上面那些 S0 前拒絕碼），8765 不記成終局，保留原編號續做、也不讓另開新編號；bridge 保留 server 附的 `cause`，畫面依 `cause` 說明該怎麼排除（工具讀不到、skill 版本變了、
模型容量），其他碼原樣顯示並請部署線處理。onboarding journal 保存的 command
本來就含整份 body，所以續做沿用同一份選擇；舊紀錄沒有這兩個欄位，照舊續做。建立 body 上限因此調為 16384 bytes。

### Agent 摘要與新動態

**CODE/offline；尚未採用到原8765。** 左側保留原Agent清單，每位助理顯示其目前對話最新已知訊息的短摘要與台灣時間。
來源只有該Agent本機journal所提供的state，或已沿該Agent既有入口讀到的公開資料；不為摘要另查後端。
尚未載入該Agent、空白對話或沒有資料時，沿原「已連線／描述」文字，不拿另一位助理的對話補值。
摘要只用純文字，最多120個Unicode字元；輸入框尚未送出的變更不冒充新訊息。取消／失敗結果沿原`resultText`，
不以較早正常答案蓋過最新結果，也不修改完整對話。

使用者訊息沿原`submission.intent.created_at`等本機紀錄；回覆沿已讀`reply.observed_at`，已取結果沿`fetch.observed_at`。
回覆時間表示讀取紀錄，不是模型生成時間；本頁首次載入相同訊息後固定該時間，不因重複查讀刷新。
時間缺漏時不補造，完整日期與台灣時區可由時間提示查看。

未選中的Agent若有新回覆、已讀交接卡片狀態改變，或已讀公開投影出現B回傳結果，顯示「新動態」；點進該Agent即清除。
提示只存在本頁記憶體，不改journal格式、不持久化未讀帳本，也不等於真人收件或ACK。
A已讀的B結果metadata只用來提示，不變成B的訊息正文；沒有B本機submit時不造對話或工作連結。

**2026-10-05 已拿掉（見[背景少讀](#no-jump-background-reads)）：** 原本未選中但已知原回合仍在執行的Agent，
每5秒沿自己的`X-Console-Agent`及原thread／CID GET `/api/instructions/progress`更新側欄。現在不在背景讀；
點回該Agent時讀它的最新狀態，原接單回應晚於Agent切換時只更新側欄預覽，不再次submit。
以下是原設計紀錄，保留供對照：

未選中但**已知原回合仍在執行**的Agent，每5秒沿自己的`X-Console-Agent`及原thread／CID，
GET既有`/api/instructions/progress`；這不是idle Agent掃描。只有同登入、同原Agent／CID／job／binding的回應可更新側欄，
不覆寫目前聊天、草稿或template。原接單回應晚於Agent切換時也只接回原回合，不再次submit。
終局、頁面隱藏、登入失效或Agent不可用就停查；切換／重連後的舊回應不能續用。
`409 RESULT_UNKNOWN`且`upstream_status=412`依原規則靜默等下一個5秒，不立即retry或POST；
其他讀取錯誤明示在該Agent，不把unknown當成功，不增加後端端點、權限、manifest或任何工作POST。
交接提示只沿既有交接紀錄讀取，不替idle Agent新增交接輪詢。

#### Verified against：Agent 摘要與新動態

- 基準為已發布`fec04a8e16ea2b7c90b56440e97f08932ebc0ed4`加本片`index.html`／原CJS測試WIP。
  實際接點為`rememberAgentActivity`、`updateAgentActivity`、`readAgentMessages`及原`sendChat`。
- `node --test tests\shared_context\test_web_test_console_workbench.cjs`：242 PASS。
  HTML SHA-256 `4e1cfa7d0dd07c04c8223f21fa398455cdc64c9027beb239c0980319a8cde55e`；
  測試 SHA-256 `3ffaa284a7e62a5fe9766007e954da450953ed70079e2db9f7a5917a49573c82`。
  結果來自提交前WIP，不倒標成final commit重跑。
- 同一切換控制案例以published基準HTML重現1 FAIL：載入結束未重建停用的交接按鈕。
  本片把原`busy`／`threadLoading`納入原渲染比對，修後包含於上述242案。
  其他摘要、五秒期限、晚到接單／CID／目錄／重連的逐片RED，以及早期共用DOM的239 PASS／1 FAIL均保留；
  最後批次不改寫原失敗結果。
- 只用既有minimal-DOM、合成state／projection及可控計時器；未改bridge／backend／journal，
  未跑Python、native／模型／Hosted或原8765，未宣稱live或瀏覽器驗收。

### 下載原答案文字

**2026-09-28現場：** 程式自c18起已在8765，現隨[166a1bb快照](#現場工作台2026-09-28)上線；沒有live下載實測。

此為本機CODE／offline接線，尚未採用。
每則可讀取的已保存答案旁提供「下載答案文字」，不必開支援明細或自行拆JSON。
只有使用者明確點選才GET `/api/instructions/{task_id}/reply.txt`。
Bridge沿原task／admission與current human/read，重用原conversation/reply純GET及完整JSON SHA／身份核對，
再將`TextReply.text`原樣編成UTF-8；保留前後空白、Unicode與換行，不trim、摘要、補BOM或重新生成。
同一處另有截圖：回合的公開進度有成功的`deliver_image`時，頁面自動讀一次`/api/instructions/{task_id}/images`與`/images/{version_id}`並顯示縮圖；
讀取失敗就顯示錯誤、不自動重試，由「查看截圖」手動再讀。沒有交付截圖的回覆不顯示此按鈕；進度紀錄因8 KiB上限被截掉前段時，只顯示按鈕、不自動讀。
bridge以本人token讀API的`/behavior/v1/bindings/{binding_id}/images`，核對長度、SHA與PNG標頭後回`image/png`；
頁面只用本機blob URL顯示縮圖、開大圖與下載，不產生公開網址（見上層README「把截圖交給使用者」）。

檔名只由已核對的turn number組成`fa-reply-N.txt`，回應為`text/plain; charset=utf-8`、
no-store與此文字檔bytes的SHA。這個SHA不同於原完整TextReply JSON的SHA，原JSON下載入口仍保留。
未保存的preview、錯scope／hash／格式或已撤讀權時，不使用cached reply補檔，也不送成功外觀的錯誤文件。
本機入口拒絕query/body選其他scope；舊固定工作不取得此自由回覆能力。

瀏覽器先核成功回應、文字格式與固定安全檔名，才送交下載；失敗保留可見錯誤。
每個task的pending與結果提示獨立，雙擊不重複下載請求，重新登入後的晚到回應不觸發檔案保存。
這不是fetch challenge或ACK，不刪改原答案、草稿、journal或交付紀錄；重啟後仍從後端讀原結果。

#### Verified against

- `Console.download_instruction_reply`及本機reply.txt GET，沿原
  `InstructionControl.reread_reply`／`InstructionAdapter.read_text_reply`，沒有新後端權威或產生答案路徑。
- HTML逐則手動按鈕與獨立登入版本；沿既有Blob下載模式與Clawpilot樣式，不加入套件或把HTML答案執行。
- `test_web_test_console_answer_download.py`走原signed API／保存producer，SDK/model I/O為替身，
  核exact Unicode／CRLF／空白、純讀／重啟、撤權、不下載preview、錯回應與本機邊界。
- Node測項核手動GET、原bytes、拒絕假檔／壞檔名／空內容及舊登入晚到；不當live browser或部署接受。

#### 2026-09-27 分批結果

原cb0bd99＋test WIP先以兩案得到404／2 FAIL；接線後API11 PASS。
UI初次3 PASS／4 FAIL是fixture的「未點擊」計數仍為undefined、測試卻期待0；
只改oracle後七案7 PASS，不把產品改成成功外觀，也不回填前批。

固定`de5d92c8ea02aecc878565f9138bf400b7ef794f`（tree `99074bfa1b39ff4208e6cc840480c58d90285915`），
在2026-09-27T12:42:39–12:43:13+08:00：**Python14 PASS**＝新下載11＋原對話／操作預覽／冷匯入3，
另獨立 **DOM76 PASS**，含已合併#80操作UI與七個新下載案例。前後worktree均clean。
後續接回#83獨立CLI修補時，下載/UI/fixture檔逐字相同，不把這些結果改稱在新merge重跑。

Python JUnit SHA256：`5300fd5f06d837eea47bd29c9d50b31fc096158f13f95db747323e6d81df14e6`；
execution：`cbb366bf6f875b20a6f58cbab363d3ba498da62e8cbfaebb008b5305cb95f5e7`。
DOM TAP：`db11a3480c0a13851b1a6f50fabe7412fcc5b52ed99481fbca15e924076b0762`；
execution：`ab61ab5a73f979009714f0dec84fb9f1d58ceee7b054b0639d2303dea2ed1dfe`。
本機合成API／minimal-DOM與真browser／現場使用分開；沒有啟模型或重啟8765。
六批結果與原#79最後review／merge回條追加至628，保620前綴；SP-13仍partial，沒有新增完整接受。

3611bd2兩軸Opus5.5均0P1/P2後，#82主線增至633筆並讓PA-02／TC-01 current回partial；
本線完整保留該published前綴及舊接受歷史，不用下載功能重設資格。
自己的八筆未發布紀錄620–627只移至633–640，payload／hash／spec綁定不變，原628冊與sequence對照另存。
固定整合`8c64af4f45106cd824c0def79fd7ea1f5a4f34bc`只補exact文字、重啟讀回與SDK-free匯入
**3 PASS／13.034秒**；下載/UI/fixtures仍等原de5，沒有把原14／76改稱新source重跑。
JUnit SHA256：`7aed95531e012de18f4c1d1bfd42e69146f16c7f9829c0de12bb31a761865e6f`；
execution：`9e4fe541cff2885704b431e9bb55ce1413fa7dbacf921e8108f302ab6c6998f7`。
加上原兩軸回條與此三案後帳冊643筆，SP-13仍partial指實測8c64，accepted C依main為2／76，
情境0／12；此狀態不是產品能力歸零或測試失敗，也不增加全矩陣部署gate。新final另交同軸核整合。

### 原操作內容與人類拒絕

> P05 後已移除。

這個小節原本描述 `normal_pull` 固定工作的操作預覽與拒絕入口。P05 後工作台不再列出 normal 固定工作，也移除了 `/api/tasks/{task_id}/operation*` 本機 route 與 `operation_review.py`。正式人類批准／拒絕請使用 native HITL／Team attention 流程；舊 normal 專用 UI 不再是有效入口。

### 原操作的單次人工批准

> P05 後已移除。

保留這個標題只為避免舊文件連結失效。normal 專用 approve/reconcile 控制已移除；工作台不再替固定工作送出 operation approval 或 rejection。


### 停止目前回覆（本機入口已採用）

原部署線於2026-09-27 11:01:25+08:00沿同一config／journal採入已合併#73；
page／登入／state200，取消欄位存在，GET取消路徑405／Allow:POST。只核入口，沒有送真取消或模型。
11:18再固定於私有`cf7a592`source快照，避免HTML／延後匯入讀到工作樹WIP；
原8個thread、54個intent及22筆instruction／command保持，不由其他工作線重啟現場。

已核對接單且尚未觀察到終局的自由交辦，可由使用者按「停止這次回覆」並確認。
這只針對目前對話的原訊息／工作，不是reset、新對話、刪除答案或收件確認。
舊固定工作不取得新的控制能力；登入及後端當前human/read權仍走原入口。

本機`POST /api/instructions/{task_id}/cancel`只接受空物件，不讓頁面指定binding、job、
execution generation或身份。Bridge沿原command／admission選工作，先GET核原job狀態；
任一已讀到的終局優先於較早的active投影。未接單、未登入、已結束或scope不符時不送取消。

GET後在寫intent前沿原gate重核本機登入，已到期就保持尚未送出，重新登入後仍可再明確取消。
送出前沿原SQLite FULL journal保存`cancel` intent、穩定request ID、內容hash及觀察到的generation，
再向既有`POST /work-policy/v1/bindings/{binding_id}/cancel`送一次原Command。
本機以原job／generation核對server的cancel／cancel-bound control receipt與typed結果，
不假設request body的ID就是server取消交易ID。兩個tab／client共用同journal時，
原intent的唯一鍵防止重複POST；重啟、雙擊或再次按鈕只讀既有本機紀錄。

「取消受理」不代表執行者已停止、答案已保存或真人已收件。
排隊工作的原回條可指出當時已無執行者；active工作仍須等原後端停止／保存流程，
不由UI關閉連線冒充成功。若工作先完成，原server回條的`cancel_accepted=false`保留其終局，
不把成功改成取消。若generation在GET／POST間改變，回條無法與原觀察配對就保持UNKNOWN。
Active受理後若同一task的進度／status另回終局，畫面改稱「曾於執行中受理」及最近工作狀態，
不再永遠顯示「仍待停止」；原取消回條仍保持當時的`executor_stopped=false`，不倒改journal。

傳輸中斷、拒絕或回條錯誤均保原取消intent與UNKNOWN，不自動重送／換ID／退款。
使用者可繼續純讀原進度；即使之後job顯示cancelled，也不回寫本機UNKNOWN成確認受理。
初版停止接線不提供取消UNKNOWN的伺服器回條對帳入口；後續的有界唯讀核對另見下節，
不回填初版的結果或改寫原journal。
原答案、草稿、對話、SDK歷史、未決效果與ACK不刪不重建，原`can_follow_up`／`can_reset`仍由後端決定。

#### Verified against

- `freeform.InstructionControl.cancel`／`InstructionAdapter.cancel`、`CancellationReceipt`，
  `bridge.Console.cancel_instruction`及`index.html`的明確確認／pending與原task呈現。
- 沿既有`WorkPolicyControl.cancel`、`WorkerCore.cancel`及原`worker_key`核control回條，
  沒有新增後端取消權威、SDK loop或雲端session停止指令。
- `test_web_test_console_cancel.py`以原signed API／Table/controller與離線runtime替身核queued／active、
  original-ID、restart／競爭、回條變造／回應遺失、authority變更及terminal/generation races。
  `test_web_test_console_workbench.cjs`分開核minimal-DOM人工確認、一次送出與不同對話／draft不串線。
  這些不是live Hosted／browser或現場部署證明。

#### 2026-09-27 分批結果與瀏覽器閉環

| 受測來源／批次 | 實際結果與界線 |
|---|---|
| `978f224`＋新test WIP | 原route缺件4 FAIL；初接後同四案4 PASS |
| 同base的UI／狀態WIP | 新UI五案5 PASS；fresh terminal被舊queued投影蓋過1 FAIL，修後API22 PASS；完整UI47 PASS分批保留 |
| 固定`96279096acfb8fba8bfa09c1acf3aeb8945fc146` | API選28案得到27 PASS／1 setup ERROR；後者只缺fixture預設runtime路徑，補指已存在的固定binary後原案1 PASS，沒有新安裝或啟動原生runtime |
| 同962，獨立UI檔 | 47 PASS；瀏覽器其後才指出取消後仍說「接著聊／等待回覆」，新定點1 FAIL另保留 |
| 固定`33133fec2304205af85c2b9c0e07b150a42d5ab6` | 改為原server已觀察到的cancelled文案，保unknown取消回條；47 PASS／289.8426ms。Bridge／取消transport未改，不重算原Python批 |
| 固定整合`182dff005258017f9ab7598f08b0e4b9f5cb7d51` | 已含#71 template main，只驗queued取消／重啟與API-only冷匯入兩案2 PASS；沒有重跑或回填所有舊cases |

962的API批次涵蓋24個取消案例及直接相關舊控制：queued／active、原ID／重啟／競爭、
錯回條／回應遺失、前後撤權、完成／generation競態、原SDK-I/O abort及原對話／API依賴邊界。
27 PASS與setup ERROR、後來單案補跑各自保存，不寫成同一次28 PASS。
TAP／JUnit／execution的來源、時間與hash留在session；WIP批的baseline均null。
固定API JUnit SHA256：`90c34ad9dc0db912e6ff24750e2298ba23fbbb1b82ade73df0dab4ef600f5f9c`；
原setup單案補跑：`66e27eb1d3208a4bd2dc6c466ebb188c7d4fb353ae7891e9c027a09d88cc6450`；
最終UI TAP：`6741cf57eafec4b898d8967caca97044361a2aca79827240b9f457b8cdc7df0d`。

另在獨立`127.0.0.1:18766`、全新journal與合成簽章ASGI後端用真瀏覽器操作一次queued情境：
取消確認框不送cancel，確認後原API只收到一次cancel POST；重新載入不追加submit/cancel/ACK，
原文與CONFIRMED回條保留，SDK-I/O及executor starts都為0。桌面與390px畫面可讀，console無error。
初次操作用962，之後331只更新HTML文案再reload；這是跨兩階段觀察，browser run baseline=null，
不能冒稱整個操作已在後來commit重新執行。合成backend不是live Hosted／provider停止或使用者帳號驗收。
安全browser proof SHA256：`8cb5deb4e1c24244df5c4abe4980bf654c01c0779510610df81f7ef3507319e4`。
Owned測試server已停止，沒有動使用者8765工作台；原SDK/Hosted/預算/權限設定均未改。

本組另記#70最後雙軸與正常merge回條，帳冊579→594保完整前綴；
SP-13只更新partial到實測331，TC-12僅追加子集證據、原baseline保持，四個C與12情境不升級。

#### 首審修補及模板共同來源

固定fac99d0的Opus5.5首審分開保存：Standards 0P1/P2、Spec 0P1/1P2。
Spec指出active取消回條雖保留`executor_stopped=false`，若原task之後已回終局，
註記仍永遠說「仍待停止」。aeb6ce0改用原task的turn／status呈現有時間限定的最近結果，
不改回條、journal或UNKNOWN；其他task的終局不混入。UI新七案先1 PASS／6 FAIL，修後定點九案9 PASS。

另採Standards非阻擋建議：923c8d2在GET後、intent前重用原gate讀取當前登入。
本機到期先回LOGIN_EXPIRED，不消耗once intent；使用者重新登入後仍可取消。
合成clock反例原為1 FAIL，修後1 PASS；該RED的server JWT clock未前移、實際回200，
所以它只證明本機送出前檢查缺失，不是live token或server401重現。重連正例使用同身分／scope的新合成token，
沒有移轉授權或修改真正帳號。README的「ACK loss」也已更正為「取消回應遺失」。

| 修後固定來源 | 實際結果 |
|---|---|
| `d5ae11ba2591491e1637b4dcf55c728e3472406e` | 取消API檔25 PASS；完整UI檔54 PASS，兩個獨立批次 |
| `3da0834bff0c2e0b2453fe40caf0fec3909085d1`（接回#74） | 共同bridge必要取消／模板／body／auth／冷匯入11 PASS；合併UI58 PASS＝原修後54＋模板4 |

#74的template consumer檔案與原tests逐字保留，`freeform.py`及取消API tests也未被merge改動；
shared UI／bridge合併兩套錯碼、路由與測項，重新登入同時清Context與template各自的觀察版本。
本線不重跑原模板64案或舊瀏覽器／native，也不將其結果歸到新source。
3da Python JUnit SHA256：`9396f1a6815a60456c40731311d0363acd644c8041a0b2bfa8f58b307ee5c7bd`；
UI TAP：`ab4fddbf54478031ca9a9ff94e36259496c94e98a6bed4d9e97864f4c8a65aaf`。
兩筆原review與八個修補／共同來源批次追加到604，保594完整前綴；
SP-13仍partial，current baseline指實測3da，TC-12原基準與四個C／12情境保持。
修後final仍需原兩軸確認；首審有P2的回條與所有RED保持，不能拿測試通過代替review。

### 原停止回條的唯讀核對

2026-09-27 12:42採用的API／client已包含本入口；只用原owned smoke admission的generation
核到409 `CANCELLATION_RECEIPT_CHANGED`，未送cancel，也未驗證live原回條200。以下原208／ada批保持。

原取消回應UNKNOWN的訊息旁可按「核對原停止回條」。這只GET
`/api/instructions/{task_id}/cancel-receipt`，不接受body／query挑選其他工作或世代。
Bridge核原admission、UNKNOWN intent的method/path/job/generation/body hash，
才沿原登入GET後端`/work-policy/v1/bindings/{binding_id}/cancel-receipt/{generation}`。
原intent缺件／變造或未確認接單時拒絕，不根據最新job另造一個取消。

後端以現行原human/read權、原registered job、同一generation及execution binding，
沿取消writer共用的request builder與原`BehaviorStore.reconcile`讀不可變回條。
整段在consistent read內，不呼叫cancel／worker query／controller.step、不初始化缺少的standing control，
不新增guard／receipt／工作寫入，不觸發SDK、模型、取件或ACK。
回覆須no-store、exact SHA、typed shape及原worker control request ID都相符。

查到的是該job/generation/context/config對應的取消控制交易，不是對任意外部效果的exactly-once保證，
也不證明哪一個HTTP caller送達。原取消傳輸UNKNOWN保持原樣；新的查詢觀察只留本機記憶體，
畫面分開說「原未確認紀錄保留」與「已唯讀核對原回條」。
受理／當時是否已停止／後來工作終局仍分開，不把GET當作收件或改寫原cancel journal。
重新載入不自動查詢，重新登入／失去登入會清查詢觀察並隔離晚到回應。

找不到回條仍是`CANCELLATION_RECEIPT_UNKNOWN`，不是「確定未執行」或重送許可。
原generation／request已不吻合回`CANCELLATION_RECEIPT_CHANGED`，不改查新generation。
這個切片不掃描任意歷史世代、不猜舊owner/config，也不提供取消重送或新批准。
舊API未部署此GET時，工作台明示核對不可用，原停止按鈕與UNKNOWN紀錄保持。

#### Verified against

- `WorkerCore._cancellation_request`由原cancel writer與新reader共用，既有取消key/digest/read-set不另造一套。
- `WorkerCore.read_cancellation_receipt`、`WorkPolicyControl.cancellation_receipt`與新work-policy純GET，
  沿當前authority及原store receipt核對；沒有新持久化型別或history migration。
- `InstructionControl.cancellation_intent/read_cancellation`、`InstructionAdapter.checked_cancellation/read_cancellation`、
  bridge GET與HTML逐則手動按鈕；原POST仍使用同一typed回條驗證。
- `test_cancellation_receipt_read.py`核queued／active回應遺失、missing、世代／請求錯配、前後撤權、
  不初始化、原intent／body／scope及回條損毀；存儲與journal bytes保持，沒有額外dispatch。
- Node案例核人工GET、保UNKNOWN、錯task／找不到回條、重新登入後晚到回應及不自動POST；
  都是離線fixture／minimal-DOM，不是已部署或live取消核對的證明。

#### 2026-09-27 固定來源與結果

初次四案在未改API的cf7a592＋test WIP得到4 FAIL（404）；接線後4 PASS，
擴充API／bridge28 PASS、UI四案4 PASS、四個撤權／不初始化／錯配guard案例4 PASS各自保存，
這些WIP batch的baseline均null。

再先固定`20861a9ab4212ebd5e45b1f081b8e02d7b64fc88`（tree `43dcf4d5e2b71dec1bf345dabe1e4a92b1cb2212`），
已含#75／#76主線，於2026-09-27T11:32:29–11:34:06+08:00執行：
Python **62 PASS**＝32新回條案例＋25原取消案例＋5個worker/store/API-only控制，
另獨立UI **62 PASS**＝58原案例＋4個新回條觀察。兩種口徑不相加成124個獨立品質樣本。
執行前後worktree均clean，來源hash／argv／時間保存；沒有新browser、SDK process、模型或雲端執行。

Python JUnit SHA256：`c422651603405ba43c4d88ce81a26fbb60401680bfcf9943b787b50d723f43ae`；
execution：`5c87aede0c0332bf55553bc7950d2b3ac7030ec1ff76a659be025facf950df8b`。
UI TAP：`7ad8eedcbc2add6410de1a8fe3a835608e988da789ee73f6fbc41fb0f44b0029`；
execution：`3de7f87c98df139a42275224b6a6d1b6318417a63a85348006a3b434ca3f52c7`。
帳冊607→617保原前綴，另記原#73最後review／merge及原持有線的停止入口採用回條；
入口採用沒有送真取消，不借它宣稱本新GET已部署或live核對已過。SP-13／TC-12仍只是子集。

原dfec81f兩軸Opus5.5皆0P1/P2後，接回#78的獨立操作決定回條主線69ae683。
取消worker／work-policy API、thin client與兩個測試檔逐字保持，只有新的操作approval/revocation讀取方法並存。
固定`ada6fdfed1ac1d6fafc1ad7c7f903930c299a2ea`另選queued／active原回條兩案 **2 PASS／6.860秒**，
不把舊62／62改稱在merge重跑。JUnit SHA256：
`f9bb2c779c072cc3e3f96e28416ee7e2c8f76381b09389125d85d3b6a1359b7b`；
execution：`ed88d01ed963fde9da9ee330e69badf4b56cc5eb5b63b675ad48165125a738f4`。
原兩筆review與本次兩案追加至620，保617前綴；SP-13仍partial，current baseline指實測ada，
TC-12原baseline／四個C／12情境保持，新的final另請同context做整合複核。

### 助理模板：下載與唯讀比對

**2026-09-27已採用：** #74已合併，既有API補入#71的default getter後，
原8765以原config／journal恢復；真登入、下載及inspect均200，8個原thread保留。
目前版本／費用與範圍見[部署進度](../../../docs/progress.md#2026-09-27模板工作台可實際下載與唯讀比對)；
沒有為此呼叫模型或取得新權限。

「⋯ → 支援明細」的「助理模板與新對話設定」（2026-10-06 前在右上角選單第一層）使用原登入及standing配置，
不必先建立聊天或執行工作。下載沿`GET /api/agent-template`取回原API的完整JSON bytes，
只含角色、技能版本與設定參照，不包含憑證、私人歷史或操作授權。
工作台核schema、完整回應SHA與no-store後才提供`fa-agent-template.json`；
下載失敗不產生成功外觀的空檔。

選取JSON不會自動上傳；按「唯讀比對此檔案」才送
`POST /api/agent-template/inspect`。輸入上限8192 bytes，沿原same-origin／CSRF及當前read權；
未知欄位、重複JSON keys或額外credentials在本機拒絕，不傳給後端。
Bridge重用`AgentTemplate`／`TemplateInspection` DTO，核原輸入digest、技能集合及一致的比對結果，
不複製另一份schema；optional設定由原DTO保存。

角色／設定／技能相符只代表參照一致，畫面仍明示`authorization=false`、
`activated=false`與目的地的連線／資料／工具授權需求。上述下載與比對沒有apply、install、連接帳號、
模型呼叫或新的持久化資料；原journal、聊天與回條不變。
登出、重新連線或另選檔案會讓前次未完成比對失效，晚到回應不填回已清的觀察。
未部署對應API或來源已改變會明示模板不可用，不自動啟用profile或改policy補綠。

#### 加入已批准 skills 或 capability（LOCAL，未部署）

面板中的「加入已批准 skills 或 capability」讓使用者不必手寫 skill 的 version／hash，或 capability 的 entry_sha256。
- 「讀取可加入項目」同時讀取目前模板（`GET /api/agent-template`）與可加入項目（`GET /api/agent-template/options`）。
  後者對應後端唯讀的 `GET /work-policy/v1/agent-template/options`，會列出：
  - Team 目前**有效**的已批准 skill，參照和選版時核對的完全相同；
  - 目前部署可用的 capability，例如 `fa-skills-v1`、`fa-desktop-v1`。
  - 回應固定為 `authorization=false`、`activated=false`；未批准的草稿不會列出。
- 勾選 skill（最多 4 個）與 capability 後，按「以勾選項目建立待選模板並比對」：
  - 工作台以目前模板為底，替換 `skills` 與 `settings.capabilities`，再走原本的唯讀比對；
  - 選定仍需讀取目前選擇，並由你確認後才送出，後端會再核對角色、skill 版本與 capability。
  - 目前角色不能組合 capability 時，畫面會提示；後端也會拒絕。
- 讀取與建立都不寫 journal、不呼叫模型，也不改變任何設定。

#### Verified against

- `web_test_console.agent_template`沿原`PullAdapter`與shared DTO；`bridge`只提供受原本機邊界保護的下載／比對入口。
- `index.html`沿原Clawpilot樣式、`textContent`及手動按鈕；沒有自動比對或自動套用。
- `test_web_test_console_templates.py`以真正signed API／M3版本producer驗完整下載、比對、撤權及錯配；
  `test_web_test_console_workbench.cjs`驗手動下載／POST、檔案上限與重連後晚到回應。
  這些是本機合成資料，沒有真SDK、模型或雲端活動；完整模板安裝不在此唯讀功能範圍。
- 2026-09-27在固定`bedcbfdd90a2e444ebe69abdae610e8d072a52b0`：
  新模板19案與原console／standing直接回歸共64 PASS；獨立Node DOM檔46 PASS。
  原兩個未接線反例分別是下載404、inspect被原空body邊界拒絕403，沒有把拒絕當成功。
  另以隔離8783合成工作台在真瀏覽器完成下載→選檔→手動比對，呈現參照相符且未啟用／授權。
  合成server及其journal已清理；沒有碰使用者8765或原journal。此結果不是live API已部署或真模型驗收。

#### 新對話設定的人工選擇

**2026-09-28現場：** 程式隨[166a1bb](#現場工作台2026-09-28)上線；14:10讀取時live只純GET目前選擇回200、`selection_id=null`，當時沒有選擇或reconcile操作。
其後15:47選定`603c2dde…`（`fa-instruction-user-files-v1`），16:18改選`c852414b…`（`fa-instruction-history-v1`），目前是history-v1；沒有做reconcile。

**CODE/offline，未部署。** 在原模板面板選擇JSON後，手動「唯讀比對此檔案」，
再「讀取目前新對話設定」，即可檢視待選的profile、技能版本／hash與完整settings／flags。
只有按「選定為新對話設定…」並確認才送出選擇；選檔、下載、讀取、登入或重連都不會自動選擇。
`inspect.references_match=false`只是與目前參照不同，不是不可選或已獲准：
後端仍核指定human的Read+Approve、role hash、目的Team目前批准的skill refs及checkpoint／start相容性。

| 本機入口 | 原後端入口／內容 |
|---|---|
| `GET /api/agent-template/selection` | `GET /work-policy/v1/agent-template/selection`；目前pointer與模板只當觀察，不改寫原意圖 |
| `POST /api/agent-template/select` | `POST /work-policy/v1/agent-template/select`；shared `SelectConversationProfile(request_id, expected_selection_id, template)` |
| `POST /api/agent-template/selection-reconcile` | 本機只收原`request_id`；以journal保存的完整原command送`POST /work-policy/v1/agent-template/selection-reconcile`純查回條 |

只影響新對話，不改原policy／舊對話，也不是新增操作權限。
成功只說「新對話設定已選定」，不表示模型已執行、能力已部署或權限移轉。
沒有prepare／approve-operation／execute、policy reinstall、ENV或Hosted設定變更；
舊API／reader的404明示`TEMPLATE_SELECTION_UNAVAILABLE`，不fallback或自動開flags。

仍沿原vault、Host／Origin／cookie／CSRF與strict JSON邊界。只對上表的精確POST路徑開body例外；
模板上限8192 bytes，完整選擇command上限8704 bytes，本機原ID核對body上限256 bytes。
重用shared `AgentTemplate`及server的current/result/receipt DTO，沒有另一份模板schema。
選擇前以既有SQLite FULL transaction保存完整canonical command、原request ID、原`expected_selection_id`
（包括null）、模板SHA、body SHA及config SHA。相同expected ID＋模板SHA的意圖不能靠雙擊、
雙client、重啟或換request ID重送；後端CAS拒絕時也不改寫expected值重試。
已知限制：明確403／422及部分409仍沿既有once保留UNKNOWN，該expected ID＋模板SHA組不能重送。
本次不新增重試語意、不改old intent，也不納入共用once helper重構。

原UNKNOWN只核對保存的command；不能用DTO預設值補回缺欄位，再冒稱是原件。
核對觀察另存原journal，原select intent／outcome保持；fresh GET讀目前pointer不把UNKNOWN改成成功。
失敗觀察回寫沿既有SQLite短transaction核目前狀態，雙client的晚到錯誤不能蓋掉已保存的確認回條。
後續不同選擇須重新讀取並由人再次明確確認；原紀錄仍可查看與核對，不是從未發生。
換檔、重連與失登入沿原observation generation隔離，晚到結果、error與finally不填回新選擇。
壞的選擇紀錄明示錯誤並停用此選擇，不使原聊天／結果／取消／拒絕／Context控制失去可讀性。

##### Verified against

- 實作起點`4d2d503b8cadd074e13a4cf32a54e13f690348e5`（#98）；
  code/test固定為`846cd9071469790443bccfd9b23a4c5606bc417c`。
  核過`behavior_profile_selection`／`behavior_work_policy_api`的共用DTO與原wire shape，
  `agent_template`的原command／receipt核對、`bridge`既有journal／boundary及`index.html`人工控制。
- 2026-09-27 18:57（UTC+08）的**原WIP**：Python **64 PASS**（模板檔58案＋原boundary、
  preview、config once、API無SDK匯入及#98原回條／history兩案），Node DOM **92 PASS**。
  `selection-wip-source.json`記8檔逐byte SHA，manifest SHA256
  `cd196768dfbed6f6874cb7bf8b0d8554cc9bb3068e45ba4a37106a0ea9ee377c`；
  JUnit SHA256 `d4b36fce4f7d9d9a46f0e537cb8e8f2cc048c1741bc724a6e4298e0856c8a10e`。
  原GET 404、select／reconcile body 403、DOM缺控制及缺欄位被預設值補回的FAIL均保留，
  不因後續修補改寫原結果，也不把64／92倒標成固定commit已重跑。
- 普通整合exact #99 `30be32319aa81aefe16bfe73a3399a002feef0a7`後，
  固定`3ee3db8276a029aecd56f971dd23e26c813a35dc`，
  tree `fc3acc32b762b8197a5732b23736b547acaceef8`；
  19:02:39～19:02:54（UTC+08）只補**5 PASS**（11.39秒）：
  current GET、不同readonly profile once／重啟、原command response-loss核對、
  API cold import與#99原profile selection／origin相容案。
  JUnit SHA256 `75b314d8710801aad703be164eab5877169beaab63e2374112d19577c7b35959`。
  8個本次code/test檔逐byte未變，未重跑UI；#99原code／ledger／歷史完整保留，沒有新eval rows。
- GitHub在`addcf3a`明示CONFLICTING後，才依creator條件普通整合exact #100
  `e6fd2802d8626025e92acef378e19f8e6c6cdad3`；僅CURRENT有衝突，Shared README／API自動合併。
  保其附件metadata GET及全部published code／ledger，不新增picker或後端授權行為。
  2026-09-27 19:13（UTC+08），**merge WIP** input tree
  `c4e51c21b042b78e6e2e547634cd3124812e5103`只補**3 PASS**（10.08秒）：
  選擇once／重啟、原profile選擇→附件metadata相容、API cold import。
  JUnit SHA256 `44786d5df464195cfb4c070ed67289bc2117bfc4d59e426373a80b1d7567122c`。
  API只多保留#100原11行route；UI及其餘本次code/test未改，不重跑UI／browser／native／模型，
  不把這3案回填到846或3ee；完成測試後只增補本段文件與摘要。
- 交付前另保留一個並行反例：原回條已被另一client確認後，晚到error仍以舊snapshot回寫UNKNOWN，
  首次**1 FAIL**。修補只在原journal內用既有`BEGIN IMMEDIATE`做當前確認狀態檢查／失敗回寫，
  不持鎖跨HTTP、不改原select row或API／UI。2026-09-27 19:25（UTC+08），base `c55e693`
  的WIP input tree `bce742ac50dcdbeedb06ea32b477435b8e1d090d`只驗該反例及原response-loss／
  重啟核對，**2 PASS**（6.12秒）；JUnit SHA256
  `4b947a6505b2cf2953177c3779b4f9248782cbb8101dd6c645df54089499da06`。
  原FAIL與先前64／92、5、3各保來源，不改綁或重跑無關範圍。
- Creator於2026-09-27回報原兩位Opus5.5 turn9：對exact
  `e6fd2802d8626025e92acef378e19f8e6c6cdad3`→`8a35f9958c52ef03b7e773297782a682078af9b4`，
  tree `55b0a464c6d9dfdbe021e1f24226c6297310b3a2`，Standards／Spec各0P1／0P2。
  這只適用該已審head；#102整合後的新head仍待原兩軸核必要integration/doc delta。
- 本次只普通整合fixed #102 `d3ccbc2e897ffab08449c798be68f714e2a0dc24`，
  解CURRENT文件衝突並保Shared README自動合併及其原706 ledger／新follow-up測項。
  `src`／`tests`原486個非Markdown檔案SHA256均與已審8a逐byte相同；
  只核diff、連結及CURRENT篇幅，沒有重跑產品／UI／原測試、新follow-up測項、native或模型。
  #102既有兩案仍歸其原45d2cff來源，不冒稱為這次執行，沒有新增review rows。
- 依creator指定，再普通整合fixed #103 `fe5ab6bc9c0d08a2339cdf6cd0d9977ee5a81b4d`，
  只解CURRENT文件衝突，Shared README自動合併；兩個SDK檔、原測項、723 ledger與歷史原樣保留。
  本選擇功能的產品／UI／測試仍逐byte同已審8a；除#103兩個SDK檔外，原484個非Markdown
  `src`／`tests`檔也同8a。新增SDK測項及兩SDK檔採#103原blob，沒有在本線重跑。
  本次只核diff、連結、篇幅與非文件來源；#103已發布的SDK／現場觀察不是本線執行或部署，
  未讀取父未提交WIP或private c18資料。新head仍待原兩軸核8a後必要integration/doc delta。
- Runner沿現有Python，逐次設`PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`及本worktree的
  `PYTHONPATH=src\fa-shared-context-poc`，使用`-X utf8 -B -m pytest -q -p no:cacheprovider`。
  主要測項為`tests\shared_context\test_web_test_console_templates.py`；
  DOM命令為`node --test tests\shared_context\test_web_test_console_workbench.cjs`。
  原輸出留本次session的`selection-*` artifacts，不進Git；合成ASGI的8765 URL沒有開真8765 socket。
  沒有Azure、真帳號登入、真M3決定、SDK runtime／native、模型、Hosted部署或瀏覽器驗收。

### 驗證聊天須與使用者工作台隔離

效能、記憶代碼及多輪驗證使用獨立的loopback port與全新SQLite journal，
不得向使用者正在使用的8765工作台建立測試對話，也不得把測試thread留成預設。
可沿原可信config／sidecar與身份，但不得共用主journal或覆蓋使用者的選中對話／草稿。
驗證至少包含不同主題的新提問，不把「能記住代碼」等同正常換題能力。
測試結束後停止自己的驗證工作台並移除其本機聊天journal；
失敗／未核清的後端工作、once／費用帳本不得因此清零、重送或假稱已結案。

## 原文交辦、真回覆與同一對話追問

新的自由交辦直接走已接通的 standing backend，不必先執行固定 metrics。真人輸入非空白原文，唯一內容容量為 **quoted JSON UTF-8 ≤8192 bytes**，沿既有 `IncomingEvent.content` 的 8192B 持久化入口；SDK 新 Task envelope 另保留必要空間，不再讓 metrics 的 goal 欄位限制原文。只有超出容量時才在輸入框下提示；沒有額外字元數天花板，不 trim、不截斷、不限定工作種類。收件人／Team／Agent／origin 都沿原可信配置，頁面不能換目標或替自己批准副作用。

API 與 Hosted 必須都已接入正式 InstructionSubmit／多輪契約。父用最小部署接線檔註冊，不改原 config bytes、model SHA 或既有 journal；這不是新權利、有效期限或人工批准。`InstructionConnection` 只含 `schema_version=fa-console-instructions-connection-v1`、原 `configuration_sha256`、父核過的新 `backend_core_sha256`。core metadata 不是 client live attestation。未接線時文字送出保持不可用，不以空框宣稱已完成。

```powershell
& '<既有 Python interpreter>' -B -m web_test_console schema --configuration instructions
& '<既有 Python interpreter>' -B -m web_test_console serve `
  --config '<原 standing-v3 JSON>' --config-sha256 '<原 raw SHA>' `
  --journal '<同一份原 SQLite>' `
  --instructions '<父核定的部署接線 JSON>' --instructions-sha256 '<該檔 raw SHA>' `
  --port 8765
```

若原 console 已帶 `--pull-continuation`，仍可沿用原 sidecar／參數；新文字交辦不覆寫其舊 history。Journal 原 `config/intents` schema 不改，另加 `instructions/instruction_threads/instruction_commands` 表保存原文與訊息關聯。`thread_id` 只是本機分組，真正的 conversation_id／SDK session 仍由 server 提供。點送出時先 FULL SQLite 保存原文／穩定 request ID／完整 command，再走同一次 submit intent；原文只在本機 journal 與指定 backend，不進 console logs，bearer／challenge 仍不落磁碟。

HTTP 沿 SDK 正式契約：

- 一般文字送出不先呼叫獨立 `preflight`：先保存原 ID、內容、預期 conversation／turn／model，
  再一次 `POST /work-policy/v1/submit`，由後端核對目前權限及對話順序。
  預期識別只供回應核對，不是本機自授權。工作台的狀態讀取仍可對 job-bound instruction 做純讀
  `POST /work-policy/v1/preflight`；legacy fixed work 不再用它當 UI gate。

  新memory對話的preflight可帶`memory_setting_ref`，綁原admission、不可變settings ID及capture摘要。
  `candidate`是該request的候選設定，`captured`是原registration已保存的設定；原CID／重送不重讀
  當前selector。工作台只有在此server參照完整時，才接受原binding加上該admission精確派生的一組
  memory source／claim；錯ID、多槽、少槽或缺參照都拒絕。其他binding欄位仍完全相等，
  preflight仍是facts而不是permission。未啟用時欄位省略、舊wire不變；需相容API／工作台才可啟用，
  不自動變更模板、記憶設定、SQLite原command或重送工作。
- `POST /work-policy/v1/submit` 傳同一份原文 command。核原 binding/job/work 以及回傳 conversation_id/turn_number；首次對話識別只由 server 產生，不由 UI 建立 SDK session。
- `GET /work-policy/v1/models` 只讀原 project 配置的真聊天部署。`model` 在 `conversation_id` 後，兩個 None 都省略，使用者實際選值納入 command SHA，傳到原 SDK／provider；同 conversation 下一 turn 可按正式 backend 能力改模型。不硬編更多部署，也不把 embedding 當聊天。容量 null 顯示未知，不是無限 tokens；輸出規格僅展示，UI 不强塞 token 上限。
- Server-owned `model_capacity_profile="fa-model-capacity-v1"` opt-in 時，conversation turn 可帶
  `fa-model-capacity-snapshot-v1`。薄 client 只驗其版本／digest 與原 turn model 相符，保留為
  **server-curated facts**；沒有讓使用者輸入 capacity 或把它顯示成 runtime／provider
  已強制的有效上限。缺省不新增舊欄位；snapshot 的 context/input/output null 仍是 unknown。
- `GET /work-policy/v1/conversations/{conversation_id}` 是純讀進度 projection。只按 server `can_follow_up` 顯示追問能力，不把 saved、Web ACK 或自己推測的 SDK archive 當成權利。每次新 submit 仍由 server 原子核同 conversation；未決或 UNKNOWN 不換 ID 跳過。這個純讀只重讀本對話各回合的工作登記；同 Agent 其他對話累積的回合不計入 4 MiB 純讀上限。`/models`、`/agent-template/selection`、`/agent-messages` 及 `/environment` 的 GET 也一樣，不再於純讀內讀整個 Agent 的工作歷史。圖片只由 `/behavior/v1/bindings/{binding_id}/images` 另行讀取，不內嵌在進度回應。
  已保存的 versioned instruction turn 可帶 `prompt_assembly`；bridge 的 `ConversationTurn`
  使用既有 SDK-independent `AssemblyRecord` union 驗證已支援 schema：
  `fa-prompt-assembly-v1`、`fa-skill-prompt-assembly-v1`、
  `fa-user-turn-prompt-assembly-v1`、`fa-skill-purpose-prompt-assembly-v1`，
  以及新 default-OFF 已審工作記憶 profile 的 `fa-work-memory-prompt-assembly-v1`。
  缺省／None 不增加舊序列化欄位；未知 schema、錯誤 hash／長度／能力或額外欄位仍拒絕，
  不放寬 `extra="forbid"`。這只是 server 的公開組裝 metadata，不是 body／system prompt、
  live attestation、操作許可、完成或收件證明，也不改 `can_follow_up`／`can_reset` 的來源。
  最新已完成回合的 projection 已在本機時，準備追問草稿不重複遠端 GET；
  它不是提交許可，後端仍核對當前狀態。
  執行中的 `stream` 是選用、暫存的文字預覽，`durable=false/complete=false`，
  只含公開答案文字，不含 reasoning、工具參數或凭證。畫面標示「尚未確認保存」，
  不因此開啟追問或ACK；後端重啟、預覽過期或失敗時，以原工作狀態及已保存答案為準。
  預覽最多32,768字，這只是顯示界線，不截斷正式答案；目前透過既有GET輪詢更新，
  不是另開一個可重播模型請求的瀏覽器連線。
  `/api/state` 與進度回應只整理選中 thread 的訊息；P05 後不再混入 legacy normal 固定工作，
  其他對話保留在 `composer.threads`，切換時再讀原thread；不刪除或重寫journal。
- 每則新追問在使用者按「送出」時產生新的穩定訊息 ID，沿原 server conversation_id。重整不產生新 ID；原回應未知先手動 `POST /work-policy/v1/submit-reconcile` 原 command 核接單。只有下節 typed 觀察核實尚未完成接單後，才能明確以完整原請求重試；不自動重送，也不把原 UNKNOWN 改寫成成功。
- 可用「開始另一段對話」建立獨立草稿，不需等其他 conversation 的工作完成。同一對話只沿 server `can_follow_up` 順序接續；HTTP 傳送以 message ID 分別鎖定，沒有新自由工作全局 busy/C1 gate，跨進程仍由原 SQLite once intent 保護。
- `GET /work-policy/v1/conversations/{cid}/turns/{turn_number}/reply` 是可重複純讀。核 `x-content-sha256`、exact JSON UTF-8 bytes、no-store、conversation/turn/SDK session，再以 `textContent` 顯示真文字。進度更新發現可讀回覆時可同樣純 GET 取得，不建立 fetch/effect/ACK。
- 正式「確認收到」仍是人手動作：必要時先沿原 delivery/pull 取得原 challenge，核與已讀回覆相同後才同 ID ACK 一次。舊 fetch UNKNOWN／失去 challenge 不盲重送；純讀回覆或畫面 render 不是 client_received。既有舊路徑與五 headers 不改。

工作與訊息原文在同一對話中排列。新文字工作以純 GET conversation projection／reply 更新真進度及已保存回覆，正常間隔約1秒；進度 GET 的 `RESULT_UNKNOWN`／upstream 412 只保留目前畫面，安靜等約1秒後下一輪，不清除原送出錯誤或重送工作。其他暫時讀取錯誤採最長30秒間隔再查，權限錯誤等待使用者處理。頁面隱藏、對話已可追問且回覆已取得，或已失敗／取消時停止。更新不啟動模型，也不宣稱 token 串流；legacy fixed work 已由 P05 從工作台移除。

上一 turn 真完成且 server 已封存即可追問，不以 Web ACK 作全局模型門檻；舊結果的 read/ACK 不被新 turn 的成本或執行期限一起鎖住。畫面明示觀察時間；失聯保留原 UNKNOWN，不以狀態顯示或 render 取代真正的收件回條。

### 原接單核對與完整原請求重試

**2026-09-29，CODE/offline。** 多對話同時寫入可能使其中一筆 admission 回 412；
API 已有最小已知拒絕重試，工作台另保留人工恢復原件的路徑。
「核對原接單」只查原 command，不啟動模型或再次 submit：

- 正常已接單回應仍沿原 `InstructionReply`。
- shared `WorkSubmitObservation` 的 `not_admitted`／`admission_incomplete` 觀察，
  只有 RID、原完整 command SHA 與 `retry_same_request=true` 精確符合時，
  才提供「以完整原請求重試」。這是當次純讀觀察，不是永續操作權；submit 時 API 仍核目前身份／費用／CAS。
- 使用者明確按下後只送已保存的原 content／model／CID／attachments／files 與相同 RID，
  不讀目前輸入框、不修改原 command。沿既有 SQLite journal 追加 `submit-retry-*`，
  每一筆有效 reconcile 觀察只能使用一次；雙點擊、另一個 bridge 或重啟都不重用它。
  沒有 DB migration、另建 journal 或改 config。
- retry 回應遺失仍是 UNKNOWN，先重新核對原件；不第三次盲送。後續 confirmed admission
  另存原核對／retry phase，第一筆 submit 的 intent／UNKNOWN bytes 保持。
  讀失敗、拒讀、舊 API、缺欄／錯 hash 或未知狀態都不啟用重試。

#### Verified against：原接單恢復

`freeform.InstructionAdapter.reconcile`／`InstructionControl.retry_submission`、
bridge 原 action gate、`index.html` 及 shared admission／reconcile 接點。
直接 signed API／SQLite 案例在 `test_instruction_admission_race.py`：
412 後原 RID、restart、雙 client、完整附件 command、回應遺失後只核對、錯 wire／拒讀保持停用。
Node targeted `original submission retry` 案例驗人工操作、雙點擊只送一次、錯誤不啟用、
晚到回應不換回舊 thread；沒有真瀏覽器或 live 操作。

### 文字來源附件選擇

**2026-09-28現場：** 程式隨[166a1bb](#現場工作台2026-09-28)上線，但`text_attachment_profile`仍None；沒有live候選讀取或附件送出。

**CODE/offline，未部署。** 展開輸入框下的「文字來源附件」，手動讀取本對話候選，
從`recorded`項目選至多4份，再按原「送出」。顯示logical filename、版本、SHA及bytes；
名稱可修改，但仍由shared `TextAttachment`／`Filename`驗證，不是本機路徑或URL。
沒有選檔上傳、本文／dependencies下載、grant或自動template select；只由原Send交辦。
**候選不是讀取權限；執行時仍由後端核原來源／版本／權限。**

本機`GET /api/instructions/{原draft request_id}/attachment-sources`先從原journal核thread與CID，
全新且無CID才用`GET /work-policy/v1/attachment-sources`；既有對話／追問只用
`GET /work-policy/v1/conversations/{原CID}/attachment-sources`。重用#106的
`AttachmentSourceCatalog`，核外層thread／RID／CID及catalog原CID、no-store與strict JSON，
metadata response上限16KiB。未知admission、錯CID、404、disabled或scope不支援都不fallback global，
也不藉目前新對話profile替舊對話開附件。已封存／缺Hosted SID的409沿原code明示，不重試成另一個目錄。
`missing/expired/withdrawn/unsupported`只解釋狀態、不能勾選；不附檔的普通文字交辦仍可使用。

未送出的選擇保存`name+ExactSourceRef`快照，不用catalog位置當身份；候選排序或版本變更不替換舊選擇。
失效快照保留並停用該次帶附件送出，需人類移除後重選。草稿附件與既有文字草稿同屬本頁記憶體，
重新整理前須自行保留；送出時才進原durable command。換thread／新draft、重登及失登入
會使舊metadata結果／error／finally失效，不污染新頁或釋放新讀取的busy狀態。

原`instructions/instruction_threads/instruction_commands` schema與插入欄位不變。
在原`InstructionStore.prepare(..., freeze=True)`的短FULL／`BEGIN IMMEDIATE`交易內，
核RID仍屬目前draft、原文字及完整command一致後，一起保存content/model/attachments/CID；
不持SQLite transaction跨HTTP或await，其他caller仍可先freeze再prepare。
舊content-only若無任何submit／submit-reconcile intent，可用同原文明確完成command；
若已有送出意圖則明示`INSTRUCTION_INCOMPLETE`並停用重送，不補造空附件或新CID。

原`InstructionSubmit.attachments`空集合省略，所以無附件的原serialized bytes不增加`[]`。
原wire submit SHA、transport body SHA、preflight及reconcile都包含完整原command；
prepare後不可用同RID換name/ref/model/content。回應遺失仍只原ID＋原command核對，原UNKNOWN不改寫。
已知保守once限制：附件凍結後若因來源撤回、附件disabled等被拒，目前adapter可能泛化為
`UNKNOWN/BACKEND_DENIED`；不能改同RID附件。本次不新增重試、解鎖或重寫原intent的語意。
追問Send先snapshot自己的附件，再沿既有next取新RID，隨原文/model移到新turn；
不改前一turn附件，也不把原附件自動再附到追問。只有`next`成立才繼續原Send，失登入不續送。

#### Verified against：附件picker

- Published base **`f72e2c90a782a6f28b3b27be932ea90cc0d55516`**（#106），
  程式／測試固定為`dffe9945c63408f9b709cee3a2f7241c0291dcd7`；
  `bridge`僅本機入口，`freeform`沿shared DTO與原CID，`instructions`原schema短交易，
  `index.html`沿原vanilla／Clawpilot與draft/observation接點；shared API／Hosted程式未改。
- 2026-09-27 21:39（UTC+08）**WIP：45 Python PASS**，56.33秒：
  `test_web_test_console_attachments.py`39案＋原store四案與template once／原回條兩案。
  真signed ASGI／原source與admission producer、合成journal；fixture明確禁止runtime step／process及真network。
  JUnit SHA256 `5c42d12a74a83c8076545a296adc670c7c024f9b5a43d67c29428e8c90210028`；
  六檔source manifest SHA256 `2cf42a86408de4dcd957b269a13c268a0b077e8beded18021e49c3f62444f00c`。
- 同批**117 DOM PASS**；後只加picker高度／換行等4行CSS，再於21:42補同117案，
  HTML SHA256（**Windows CRLF物化bytes**）
  `9d11d20a0ef171db6e8af6e1b8020d9a852c93fb16b328dba067f4ff3da2fa5a`；
  Git LF blob SHA256為`f47f407940bd25505650dbb1c4a0b404dfd01d4518d2c9798ca682d8f16adacc`，
  兩者只差行尾物化，不互換原受測bytes或hash。
  包含25個picker及92個原控制分支；不重跑未改Python，也不把WIP結果倒標成fixed commit重跑。
  `node --test tests\shared_context\test_web_test_console_workbench.cjs`沿原runner，沒有瀏覽器／live驗收。
- 原metadata404、submit body403、DOM未接線及舊UNKNOWN被呈現可送出的2 FAIL都保留。
  首個fixture因frozen controller誤patch有2 setup/teardown ERROR，修fixture後才取得404反例；
  cold命令首試shell引號SyntaxError未執行，改UTF-8 stdin後確認無Hosted SDK匯入及無附件wire原樣。
- Python沿指定既有interpreter，逐process設`PYTHONUTF8=1`／`PYTHONDONTWRITEBYTECODE=1`、
  自己的`PYTHONPATH=src\fa-shared-context-poc`，以`-X utf8 -B -m pytest -q -p no:cacheprovider`跑上述selectors。
  原輸出／FAIL／manifest留本session `picker-*`，不進Git；SQL rollback、雙client與舊schema原rows均有直接反例。
  追問新RID的實際UI移轉以DOM驗，原CID延續另驗local draft／prepare；沒有執行新SDK／模型回合。
  未操作Azure、真M3、父8765、config/journal、父未提交WIP或部署；不新增eval/review rows。
- Creator於2026-09-27回報原兩Opus5.5 turn11，對exact
  `f72e2c90a782a6f28b3b27be932ea90cc0d55516`→`9bf5674abf16f4873366a74cf33406f478d49677`、
  tree `10c4ed1ecbdf4ff0689821690d0270feb0e0cbea`，Standards／Spec各0P1／0P2。
  結果只適用已審9bf；新整合head仍待原兩軸核必要integration/doc delta。
- 依指定普通整合fixed #107 `e3cb385df8e42733be258be8293d8a584e318c81`，
  僅CURRENT衝突，Shared README自動合併；其原735 ledger、測項及歷史保留，不增加review rows。
  原492個非Markdown `src`／`tests`檔SHA256與已審9bf逐byte相同；新native測項採#107原blob，
  本次只核diff、來源、連結及篇幅，未重跑45／117或#107 native，不改綁原結果。
- Creator回報原兩Opus5.5 turn12對exact
  `e3cb385df8e42733be258be8293d8a584e318c81`→`f95e0bdd7debbc3ecc70b5828baa53176172cb79`、
  tree `6318d738fcf775a080e763ad59b04fe96403b44b`的最後delta各0P1／0P2；
  與turn11的完整f72→9bf結果分開，不外推其後整合head。
- 依指定再普通整合fixed #109 `c611d2cbbd160f5a49581922e30426b7278f15be`，
  這是**產品相容整合，不是純文件**：保其shared `files`／`user_text_file_profile`、
  API／Hosted／原件metadata與737 ledger。`freeform.InstructionReader`自動合併的一行只把
  原command的`files`傳給shared preflight；空集合仍省略，reference-only原hash／reconcile不換意圖。
  本picker UI、local submit欄位及prepare不新增literal控制或flags，也沒有修改#109產品。
- 2026-09-27T22:17:45～22:18:02+08:00，merge-WIP parents `f95e0bd`＋`c611d2c`，
  input tree `796b642e663bda344fa3e010e04691e80d82e3e5`，只跑原
  `test_web_test_console_attachments.py`三案：無附件wire／四份ref、reference preflight原wire hash、
  response-loss後原command／UNKNOWN重啟核對，**3 PASS／0 FAIL／0 ERROR／0 SKIP，10.81秒**。
  JUnit SHA256 `eb5d08162be7894263a17e7c463a2ab3977a72c8dd3d527a20cd6f926124c850`，
  source／output留本session `picker109-interop-*`，不新增eval rows或改綁原45／117來源。
  未跑#109 native／模型、DOM或完整matrix；481個不在#109差額內的原非Markdown檔同f95，
  含UI與原picker測試。受測後只補文件／摘要，這個新interop及文件仍交父原兩軸核對。

### 使用者選取的UTF-8文字檔

**2026-09-28現場：** 程式隨[166a1bb](#現場工作台2026-09-28)上線；c18 smoke舊對話的capability純GET回profile null、`authorization=false`，沒有live選檔或送出。

**CODE/offline，未部署。** 先在輸入區純讀「本對話的文字檔設定」，再選自己的小型
txt／md／csv／json檔。只有明選的File才在本機讀取；選檔、改名與顯示metadata不POST正文，
也不以帶內容preflight探能力。只顯示logical filename／UTF-8 bytes／原件hash，不解析或執行內容，
不默改名稱，不渲染Markdown、JSON或CSV為HTML／公式。

新對話使用原完整`GET /work-policy/v1/agent-template`的effective設定；沒有selection record
不自行推定disabled。既有對話只用新增獨立pure
`GET /work-policy/v1/conversations/{原CID}/user-text-file-capability`。
shared `UserTextFileCapability`的schema、CID、read_at、nullable profile及`authorization=false`
均必填；profile=null是captured disabled，缺欄／未知schema／錯CID／404是不可核對，
不fallback current selector或reference catalog。數字0也不能冒充false。
讀取沿原CID ownership、未封存／原Hosted SID、最後registration與currentRead；
只局部共用#106的scope解析，helper不開交易，reader僅一層consistent-read，沒有新authority。
能力只是設定觀察，原submit仍核captured profile與當前權限；不自動選版、grant或開flag。

先檢File.size，再核arrayBuffer實際bytes，最多8192；用
`TextDecoder("utf-8", {fatal:true, ignoreBOM:true})`及重編碼比對保留BOM／CRLF原字。
不用Blob.text的replacement，不trim／normalize／截斷；合法空檔與U+FFFD保留，invalid UTF-8及NUL明拒。
[MDN的fatal／ignoreBOM語意](https://developer.mozilla.org/en-US/docs/Web/API/TextDecoder/TextDecoder)
與[保留BOM](https://developer.mozilla.org/en-US/docs/Web/API/TextDecoder/ignoreBOM)已核對，未加decoder套件。
MDN查閱日期為2026-09-27；其後合併日期不是這兩份文件的來源查閱日期。

refs＋files合計最多4個互異logical names。原content加`files:[{name,text}]`的完整compact JSON
經UTF-8計量必須≤8192 bytes，包含escaping與檔名；不是單看字元數或File.size。
前端預檢後仍以shared `InstructionSubmit`／`UserTextFile`作寫入前最後驗證。
無files真正省略，不改原no-files wire；完整files／refs／content／model／CID／RID沿#108既有
instruction_commands短FULL／IMMEDIATE交易一次freeze，沒有DB migration或持鎖跨HTTP。
prepare後不能同RID換文字檔，unknown只原ID＋完整command核對；保守once拒絕限制仍不解鎖。

File read／decode、capability及原件下載均綁thread／draft RID／CID／auth generation；
換檔、換對話、改草稿、重登與失登入使late result/error/finally失效。
清除或改名同時使舊pending讀取失效，不卡busy。追問原Send先snapshot refs／files／text，
next回原thread的新RID後才一併移轉，不繼承或改前一turn。未送草稿只在本頁記憶體，
不宣稱跨reload持久。原reference picker、模板once、取消、拒絕、Context及答案下載保持。

本機`GET /api/instructions/{原task RID}/files/{name}`只代理#109原
`GET /work-policy/v1/conversations/{CID}/turns/{turn}/files/{name}`：
local保存command／admission決定原CID／turn/name，每次backend currenthumanRead才釋放原件，
核rawbytes、SHA、長度、Content-Disposition、no-store／nosniff；0-byte原件可合法下載，
404／denied／錯header不產空成功檔或以本機快照繞讀權。
本片不改`read_original_file`；後續整合#111已發布的定位／scoped refresh差額另列於下方，
未讀取其未發布WIP，也不將cached binding ID當authority。
一般state只投影file metadata；原submit journal完整body保留，公開view用
`body_files_omitted=true`標示省略正文，不把省略view當原command。
既有錯碼限制：新對話能力沿完整template getter，失敗可能保留`TEMPLATE_UNAVAILABLE`／
`TEMPLATE_RESPONSE_INVALID`等template錯碼，而非一律改成file capability錯碼；
部分pure GET的409／412仍由原adapter泛化為`RESULT_UNKNOWN`。這只是讀取未核清，
不表示寫入已發生；本次不因此重構download/helper、解鎖原intent或新增自動重試。

#### Verified against：文字檔UI

- Published base `78708c7638c2efb6b22d0dbba39021fc2d4bd77d`（含#108／#109），
  程式與測試固定`e54a00138a1e35e46cd73ab910cf2bed85c23ac5`。
  本片確有shared能力DTO／GET及scope helper差額；不修改熱conversation response、
  reference catalog schema／bytes、原件讀取helper或instruction_commands schema。
- 2026-09-27T23:35:46+08:00的**WIP**：57 Python PASS（51 literal／capability／download案＋
  6個直接reference/no-files/once/schema/store分支），90.47秒；同source **157 DOM PASS**
  （40 literal＋117原分支）。8檔Windows物化SHA由`literal-wip-source.json`保存，
  manifest SHA256 `d87a695228a6193cd711ecf3508c3a9b173fcfbe713cf352a706b1515568afcb`；
  JUnit SHA256 `e58d42231756565c8240b57430b7f7e990064ef1875e69a75e41bebfcf71d1a5`。
  固定commit後不倒標成已重跑；冷匯入另確認API／DTO／client無Hosted SDK相依，
  原`read_original_file`的AST與published base相同。
- 原scoped404、local files403、未接線DOM及下載function scope錯誤各留FAIL；
  archived fixture漏archived_at先1 FAIL／47 PASS，補原schema配對後五分支通過；
  數字0冒充false與clear/capability busy反例各先FAIL，修後納入最終批次，不回填舊結果。
  這些是合成schema／ASGI／journal／DOM案例，archived注入不冒稱實際執行reset。
- 沿指定existing Python，每process設`PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`與本src PYTHONPATH；
  pytest `-X utf8 -B -m pytest -q --tb=short -p no:cacheprovider`，新檔為
  `tests\shared_context\test_web_test_console_text_files.py`。
  DOM沿`node --test tests\shared_context\test_web_test_console_workbench.cjs`；
  原log／FAIL／JUnit／source留本session `literal-*`，不進Git、不新增review或eval rows。
  fixture明確禁止runtime step、process及真network；未跑native／SDK runtime／真模型／Azure／
  真M3／父8765，未讀或搬入父未提交WIP／private c18/38，沒有部署或新增框架。
- Creator於2026-09-28回報原兩Opus5.5 turn14，對exact
  `78708c7638c2efb6b22d0dbba39021fc2d4bd77d`→`ac7c9f159edd5d4ddfacf1789fe2a1ca84cc5ce9`、
  tree `52d9191e503a0f2382dae415fc11b934764fc3af`，Standards／Spec各0P1／0P2。
  此結果只限已審ac7，未外推新整合head。
- 普通整合fixed #114 `4075af88a7d684943771b1db735648b6c5157b0f`（含#110／#111／#112），
  **不是docs-only**：保#111原`_instruction_record`用cached binding ID作定位hint，
  隨後仍refresh原scope／currentRead；舊file／reply入口改用其published `refresh=False`。
  本片新capability仍是一層consistent-read，scope helper不開交易；CLI、eval與原件來源完整保留。
  不重構download/helper或新增重試，UI產品及DOM測試未變，不重跑157 DOM或完整57案。
- 2026-09-28T00:20:17～00:20:29+08:00，merge-WIP parents `ac7c9f1`＋`4075af8`，
  input tree `dd58919f93d4c45c6a25940062e465e06d57880d`，只跑同測試檔三案，
  **3 PASS／0 FAIL／0 ERROR／0 SKIP，7.44秒**：captured capability不借current selector；
  原件BOM／CRLF bytes及首次下載後撤回目前讀權即拒絕；policy已啟用但無selection record的正例。
  後兩案分別補撤權斷言與新增初始policy正例，不重標舊57案來源。
  JUnit SHA256 `be2b8739e5ccfad915ad4f3e65d4cc6485e41f0dba04dcddce7d8a7f8dc2189a`；
  原source／log／JUnit留本session `literal114-*`，受測後只補本文與CURRENT，不改產品。
  沒有CLI／native／模型操作或新eval rows；新head的必要interop／文件仍交父原兩軸核對。

### 2026-09-25 用途 profile 的 projection consumer 修補

這是用途切片首審後確認的 decoder 缺口，不是 SDK／checkpoint／模型故障。固定
`d4dce9180a5be1d17dd3a8d84dc094c78eeea041` 的 `ConversationTurn` 未宣告 `prompt_assembly`；
本輪真 signed API 在已保存用途回合回 200 及合法 record，但 bridge 公開
`GET /api/instructions/progress` 回 `502 CONVERSATION_INVALID`。中間沒有 strip 欄位。
只新增上述 optional typed consumer；原 producer／scope／journal／once／reset 不變。
原用途切片 104／1 不包含這條 bridge 證據，不能倒稱當時已通過。

#### Verified against

- 程式：`web_test_console/freeform.py::ConversationTurn`／`InstructionControl.refresh_conversation`，
  `bridge.py::instruction_progress`；原 API `WorkPolicyControl.conversation` 仍核保存的
  assembly 與 immutable archive material，再提供 projection。
- 入口：`test_web_test_console_prompt_assembly.py` 沿真 signed API／Table producer／Hosted
  Adapter／bridge，只有 SDK I/O 是離線替身；不是 browser、native 壓力或雲端驗收。
  四種 record 與 legacy 無 record 都驗進度／答案純讀；未知／malformed records 拒絕，
  原 cached projection／saved reply／journal 保留。
- 舊缺省 projection 的 1434 UTF-8 bytes／SHA-256
  `ee8a1daac1be79d106bebe3bc8ddad34e2c88aab33ecccb1bebf9944ff1dad23`
  已在 exact `d4dce918` 獨立 Git archive 與目前 source 比對；不新增 None 欄位。
  `AssemblyRecord` 冷匯入不需 Hosted Copilot SDK；所有回條與權限判定保持原入口。
- 此修補不改任何 prefix、skill 用途／metadata digest、compaction observer 或其驗收。
  第六 profile 的完整 PA-02 資格與 SK-02 M 品質仍未驗；精確受測 source／新批次結果見
  [Shared Context README](../README.md#用途首審修補cli-字面預覽與-projection-consumer)，與原 14 批分開。

已審工作記憶切片沿同一 typed union 加入新 record；新增 schema 的真 signed API→bridge
progress／malformed 拒絕及 SDK-free import 個案另驗。前述四-schema 首審結果保持原樣，
不回填成當時已支持第五種 record／第七個 prefix；完整 current PA-02／TC-01 資格另由
協調線判定。薄 client 不顯示原記憶正文、私人 review packet，亦不靠 metadata 擴增 read/
operation 權。細節見 [已審工作記憶接點](../README.md#已審工作記憶的唯讀接點local預設-off)。

### SK-10 草稿結果的進度相容

**CODE/offline，待換工作台快照。** `ConversationTurn`沿shared `SkillDraftOutcome`接受optional
`skill_draft_outcome`，不再因API合法草稿結果被`extra=forbid`拒絕而使progress回502。
舊wire缺欄仍省略，不加null；未知stage、錯digest或額外權限欄位仍拒絕，保原可讀projection／journal。
畫面只在原turn的`stage=saved`顯示「草稿已存、未批准（原回合紀錄）」；
不替create/upload未知結果宣稱保存，也不改追問、收件或授權。此outcome沒有artifact ID，
因此不把receipt ID冒充artifact ID、不新增lookup或批准按鈕；原API、資料與回條不變。

#### Verified against：草稿結果reader

- 基準`63c47d2135205c2d65ea7a45e5683b1b272cc35d`；程式／測試固定
  `4c4b3d4ce173573ad0ee8372b8a478623078ebdb`。原signed API接單＋合成合法outcome wire
  經真正bridge progress reader先重現`502 CONVERSATION_INVALID`，修後6 Python PASS；
  包含缺欄舊wire、四種stage、history-skill-draft shared assembly及malformed拒絕，不冒稱真M3草稿或模型回合。
  4個targeted Node DOM案例驗saved提示、沒有POST／批准、原進度／polling／答案保持；cold import不需Hosted SDK。
- 窄掃目前producer `WorkPolicyControl.conversation`、API stream overlay與console
  `ConversationProjection`／`ConversationTurn`／`ReplyPreview`：只缺本欄。
  `PublicProgress`及`AssemblyRecord`已共用shared DTO；#128沿原assembly、#130未新增此projection欄，
  #137新環境接續走獨立shared `TransferView`，不需放寬conversation reader。
  各批是2026-09-29本機WIP來源，原FAIL／JUnit／SHA留session `console-outcome-*`，不倒標fixed head重跑。
  未操作live、8765、Azure、SDK runtime或模型；由部署線另換console快照，不需為此重建API／Hosted image。

### CAP-01 原父回合的子題投影（A3，CODE/offline）

工作台沿 shared `DelegationReply` 顯示原父回合的子題狀態；不讀 SDK private 事件，
也不把 child 的公開結果當成另一則助理答案。**只有父助理整合後的原回覆會顯示為答案**，
沒有新增批准、重新委派或 child 發訊按鈕；父工作、原收件回條及 UNKNOWN 不因子題完成而改判。

| 原 ledger 觀察 | 父回合下的顯示 |
|---|---|
| `running` | 子題處理中 |
| `completed`，shared DTO 已核完整結果 | 已保存結果；不等於父回合完成 |
| `failed` | 失敗及公開 `failure_code` |
| `unknown`／`incomplete` | 未確認；後者另說明結果不完整 |
| `stop_confirmed=false`，只有取消意圖或 child／parent 一項停止觀察 | 不顯示已取消；取消提出不等於停止 |
| `stop_confirmed=true` | 已取消；由原 ledger 核同一 child cancelled 與 parent idle |
| intent、state、result 三者皆不存在，GET 204 | 不顯示子題卡片 |
| 缺原件的 `DELEGATION_UNKNOWN`／舊 API 404／拒讀／無效 wire | 明示未確認或狀態不可用，不冒充沒有意圖，也不沿舊成功畫面掩蓋錯誤 |

這片有一個必要的 **readonly API 差額**，不是純 UI：
`GET /work-policy/v1/conversations/{conversation_id}/turns/{turn_number}/delegation`。
沿 `_instruction_record` 核原真人與 current read，再以原 parent／captured grant 讀 ledger，
在同一層 consistent-read 內核原回條與完整結果。無 query/body、無新 grant／dispatch／POST，
不改熱 conversation schema、writer、SDK runtime 或 ModelCost；不取得或回傳 private session ticket。
完整結果使用既有 `MAX_DELEGATION_BYTES`，不塞進原有較小的 conversation response。

本機 `/api/instructions/{task_id}/delegation` 從原 journal 的 admission 決定 CID／turn，
核 shared schema、原父 job／SDK session、完整 response hash 及 `fresh=false`。
只有這條 GET 接受空的 204；200 空 body、未知 schema、錯父回合或 404 都不能當成「沒有委派」。
觀察只留本次頁面記憶體；原有進度更新會讀取尚在執行的父回合，手動更新也會重新核對歷史回合。
換 thread、失登入或重新連線會清掉觀察；晚到的 result/error/finally 不能填回另一個 thread／登入，
同 thread 新 RID 也不會承接前一回合的子題。所有文字以 `textContent` 顯示，child result 不渲染為 HTML 或答案。

#### Verified against：A3 子題投影

- published base `58ae5646c63c243278cb20ee2930bcf48fedc461`；程式／直接測試固定
  `d2765d5bcd2609961b074e1c0a85f3a3b238b5e2`。
- `test_web_test_console_delegation.py` 走真正 signed API／ledger／bridge：
  沒有意圖與 UNKNOWN 分開、六個 ledger status、完整 UTF-8 大結果、取消未齊／已確認、
  新選版後原 capture、console 重啟、current read 撤銷及撤銷競態、錯 CID／turn／identity、
  strict JSON／hash／204／舊 API、丟回應後只 GET 原件。沒有啟動 SDK 或真模型。
- 2026-09-29 WIP01：新 reader 與原 `test_web_test_console_outcomes.py` 合計 **18 Python PASS**；
  WIP02：撤權競態新案與 strict 邊界加強原案 **2 PASS**（含一個重跑，不加總成 20 個不同案例）。
  `node --test --test-name-pattern='delegation|skill draft outcome|process snapshot|unchanged polling|retained answers' tests\shared_context\test_web_test_console_workbench.cjs`
  為 **13 targeted DOM PASS**。API／shared reader／console 禁止匯入 Hosted SDK 的 cold import 亦通過。
- 各批來源、輸出及 JUnit 保留於 session `a3-*`；測後核產品與 DOM 檔案 hash 未變，
  不倒標成固定 commit 重新執行。首個探針因 fixture 漏 history profile 必需的 literal-file 設定而在 setup 回 422；
  只補 fixture，原 FAIL 保留，沒有為測試放寬 profile 或權限。
- **未部署。** 採用需包含此 human GET 的 API 版本，並由部署線換 8765 console 快照；
  A3 不改 Hosted image。後端尚無 route 時只顯示子題狀態不可用，父回覆與原控制仍可使用。

### 處理過程與公開摘要

回合進行中，回覆泡泡下方預設展開處理過程，沿原本每秒一次的純讀更新顯示
「開始處理」「使用工具：網路搜尋」「讀取 skill」「整理回覆中」等白話步驟與秒數。
使用者自行收合後不會被每次更新打開；秒數更新不重建訊息，也不把正在閱讀的畫面拉到底。
回合結束後收合為工具使用次數與時間，仍可點開；失敗／取消也保留已走到的步驟，
沒有完成回報的工具不會冒稱成功。原「詳細」的技術資料仍保留。

次數與時間只按現有公開 snapshot 計算，不是完整 SDK 紀錄；同一工具的並行呼叫沿
`tool_start_seq` 配對。省略了早期步驟或舊資料缺配對時，「部分步驟」的說明放在支援明細的處理過程，
不按名稱猜配對。進行中的秒數從可見步驟時間算到本機現在；沒有結束事件時不宣稱
完整耗時，進度讀取暫停時不繼續顯示工具「進行中」。2026-10-06 起主畫面摘要只用
「正在讀取／正在使用桌面／正在等你批准／正在回覆／已完成」這類白話，見[介面去工程化](#plain-main-screen)。

#### Verified against：回覆泡泡下的即時步驟

- `index.html::processingPanel`／`processingLine`／`renderConversation`；既有 Clawpilot 樣式。
- `test_web_test_console_workbench.cjs` 的 `inline processing` 案例涵蓋展開、更新、
  收合、失敗、並行配對、舊／裁切 snapshot、登入／對話切換與私人推理不顯示。
- 沿 `node --test tests\shared_context\test_web_test_console_workbench.cjs` 的離線 DOM 測試；
  本片只改 UI，不加後端欄位、權限、輪詢或模型呼叫。由部署線另換 8765 快照，
  不需部署 API／Hosted，也不代表現場已採用。

可展開的處理過程只顯示正式 projection 的 `stage/tool_started/tool_completed/error`、真時間、
工具名稱與安全摘要／來源。新Hosted投影可在原`summary`帶去敏後的搜尋關鍵字、網址、
skill名稱，以及「助理說明（節錄）」：這是SDK正式`assistant.message`的公開中間文字，
最多256字元，不是private reasoning、原參數JSON或完整檔案內容。舊snapshot沒有時不補造。
助理說明仍只放處理步驟，不取代答案、不改保存與追問條件；所有內容用`textContent`。
事件與去敏範圍、API／Hosted同版採用順序見[公開進度摘要](../README.md#公開工具參數與助理說明)。

這是snapshot，不是完整event replay。舊Completions路徑仍顯示「此模型未提供推理摘要」；
新版Responses模板的新對話，只有原回合已啟用摘要、且SDK正式provider-tagged
`summary_text`確實提供時，主畫面步驟顯示「思考摘要：…」，支援明細的處理過程顯示原標示「推理摘要（OpenAI，節錄）」。
gpt-4.1-mini或沒有收到摘要時仍如實顯示未提供；不把SDK private reasoning delta、
scratchpad、internal prompt或opaque tokens轉成可見思考，也不另call model生成摘要。
普通答案／暫存預覽仍在原泡泡，不用摘要頂替；API與Hosted採用、舊對話保留及新codec
見[Responses 2b](../README.md#responses-推理摘要與原對話保留)。

新`tool_start_seq`存在時，支援明細的處理過程在開始／完成顯示相同「工具關聯 #起點序號」，讓同名且交錯的呼叫可辨認
（2026-10-06 起主畫面不顯示這個編號）。
它不是第幾次呼叫、raw call ID或跨execution關聯；早期start可能已被8 KiB snapshot省略。
欄位缺少時照舊顯示；非安全整數、stage/error上的引用或向後關係不成立時，UI不猜配對。
仍使用`textContent`，不因此POST、ACK、啟模型或改追問條件。
`freeform.ConversationProjection`共用typed `PublicProgress`，舊資料不增加null欄位；
部署使用相容的API／Hosted／8765同一批更新；不為回退舊版另外拆reader-first階段。
對應Python／DOM／真瀏覽器呈現的不同證據層見[配對驗證](../../../docs/tool-progress-correlation-evaluation.md)。

新自由入口不沿用舊固定測試的 240 秒、6 次模型、1 generation、600 秒排隊、每 work USD2、cost 1h 或全局單 C1 規則。`required_microusd=0` 只表示取消固定每 work 預留，不是免費。原累計上限、reserve 與未知 liability 仍由 backend 維持；UI 只稱費用觀察／保守保留，不冒稱即時實付或退額。

### 不能續問時的終局提示

原回合已終局、後端仍回`can_follow_up=false`時，頁首顯示「需要處理」（2026-10-06 起頁首只有「可以聊天／需要處理／發生問題」三種），
輸入框不再宣稱「接著聊」或「等待助理回覆」。只把已知公開blocker轉為簡短原因；未知或缺少原因一律顯示
「目前不能接續，原因未提供」，不回顯未知內容或猜測SDK故障。
只在原`can_reset`／`can_new_conversation`旗標允許時提示對應既有入口；
不改動旗標、權限或控制按鈕，不自動重設、開新對話或重送原請求。

#### Verified against：不能續問提示

`index.html`的composer投影與`test_web_test_console_workbench.cjs`直接DOM案例。
只改顯示，不新增API／SDK流程；採用由部署線更新工作台快照，原config／journal保持。

### Hosted 版本已變更的原對話

後端回`CONVERSATION_SESSION_VERSION_CHANGED`時，顯示「此對話的執行環境已被平台換成新版本，
請開新對話」；只在原旗標允許時提示新對話入口，不自動reset、遷移SID或重送。
舊答案仍沿原讀取權限取得，沒有將舊回覆、SDK state或已送出的request改成新版本。

查詢進度時，後端可重新排入既有的原工作觀察器，只核對尚未claim的`starting`／
`capacity_uncertain`啟動。它不重新POST模型工作；僅當原claim-gate可永久封住且沒有
assignment／SDK／操作材料時，才保存原job的版本失敗。已claim、讀回失敗或無法證明的狀態
仍保UNKNOWN，不用「平台版本不同」或「沒有看到模型用量」單獨結案。
因此首次查詢可能仍顯示原狀態，下一次進度才讀到明確失敗；不把查詢HTTP200當作已修復。
若原job已`failed`但`result_forwarded=false`，進度回
`CONVERSATION_FAILURE_DELIVERY_PENDING`並接回同一觀察器，沿原結果及回條冪等補轉交。
轉交再失敗仍保留待處理，後續查詢可重接；重複查詢不並行重開觀察器，已轉交就不再排入。
這不重判失敗、不送SDK／模型，也不建立另一筆使用者請求。

### 在新環境接續原對話

原最新回合已是合法的版本失敗、而且failure已轉交時，提供「在新環境接續」。
按下後先唯讀檢查最後合法SDK archive，再由真人確認顯示的目標版本。
這一步只準備server決定的新Hosted SID並保存原轉移意圖，不送模型、不重送失敗訊息，
也不先建立下一筆submit。顯示「環境已備妥，送出時恢復前文；失敗的訊息不會自動重送；
先前環境的暫存檔與登入狀態可能不在」。使用者之後自行輸入並按送出，才沿原next／submit流程
產生新request，保同conversation／SDK SID，從原合法archive恢復。

準備回應不明只提供「核對原新環境」，用完整原command查同一SID，不能再create或换ID；
原本機UNKNOWN保留，另列核對觀察。缺archive／part、codec／profile不相容或身份範圍不符，
明確拒絕，不用空session冒充恢復；未知工作不提供此入口，仍可依既有權限另開新對話。
只保存SDK session filesystem內的合法UTF-8檔案；一般工作目錄、任意`$HOME`、登入或套件
不保證移轉。原failed job、回合號、舊SID、回條與已保存答案保持不變。
只有server在reserve交易之前明確標示的4xx拒絕，才記為REFUSED；
真人重新檢視／確認後另存新的本機attempt，舊意圖與拒絕回條保留。
已登錄或回應遺失、一般5xx、缺少或無效標記一律仍是UNKNOWN；即使錯誤發生在登錄前，
無法證明時也只查原件，可能只能另開新對話，不自動重送。

#### Verified against：明確新環境接續

`behavior_conversation_transfer`／work-policy原API與工作台薄client；直接案例為
`test_conversation_transfer.py`、`test_web_test_console_transfer.py`及workbench DOM。
本機使用HTTP／SDK I/O替身，不代表已部署或完成真模型的跨環境驗證。

## 本回合 Context 明細

支援明細的「讀取已保存的 Context 明細」只送
`GET /api/instructions/{task_id}/context`；bridge從原admission選定conversation／turn，
不讓頁面指定scope／include-body／provider參數。後端需要目前指定human與read權。
新endpoint未部署、尚無保存資料或權限改變時明示失敗，不啟模型、補資料或自動重試。

頁面區分prefix／Stored SDK state JSON／inline zlib／回覆JSON／本文與儲存zlib；
不把bytes當tokens，完整request／provider tokens、回合時間及畫面bytes顯示未知。
若原回合曾保存`model_capacity` snapshot，另列該回合model及context/input/output宣告容量；
資料沿原`WorkRegistration`，不借目前下拉選單／model facts補值，沒有snapshot仍明示未記錄。
這是server-curated facts，不是provider已強制的上限，也不是實際token用量；個別limit的null不以零或相減公式補造。
技能目錄／原話／記憶參照是當時保存的metadata，不當目前可用或已載入的證明，
compaction只稱原checkpoint保存的觀察，不稱這次已壓縮或恢復。
一般續回合沿用舊execution時，API核原previous archive metadata後標記inherited，
頁面顯示「沿用較早回合的 compaction 觀察」；原observation job保留於版本明細，不當新事件。
原件未重下載，SDK state不解碼；不顯示本文、PID、Blob位置或private reasoning。

這是手動純GET，不加入進度輪詢、發challenge或ACK，不改原回覆、draft或journal。
晚到結果綁原task，切換工作不串資料；登出及讀取錯誤清除本機metadata觀察，非刪除保存成果。
登入失效或使用者重新按「連線」時，同時清除本機Context的錯誤與讀取中標記，讓新連線可以重新純讀；
舊連線的晚到成功、拒絕、讀取錯誤及收尾都不得填回已清除的明細，或清掉新請求的讀取中狀態。
這個Context讀取版本只隔離本機觀察，不取消已送出的HTTP、不改後端授權、原答案／草稿／journal或ACK。
一般切換task仍保留相同連線下的原task觀察，不因無關進度更新而丟棄合法回應。
Bridge驗`fa-turn-context-v1`、exact response SHA／no-store、原request/job/conversation/turn/model；
未知schema／用零補未知tokens／混用bytes口徑／額外欄位／duplicate key都明確拒絕。
API、bridge、HTML一起部署才有完整接點；既有progress／reply wire shape不變。
容量欄位的共用DTO須先更新reader或與API同批採用，避免舊strict client拒絕新增的optional欄位；
無snapshot的回合不增加None欄位，原已保存task/archive不改，沒有新的模型或記憶寫入。

### 原回合的宣告模型容量

**2026-09-28現場：** 程式自c18起已含，現隨[166a1bb](#現場工作台2026-09-28)上線；沒有live容量畫面實測。

此新增欄位尚未採用。原4cabfc2＋test WIP以兩個已保存回合重現缺少`model_capacity`的1 FAIL；
接線後八個API／consumer案例8 PASS，另三個定點DOM控制3 PASS。沒有拿scripted模型回覆判定換題品質。

固定`547a2aba658122237d50e6b0afadc6545ee29a33`（tree `8d1e56fa90257144ddbc4c006476a00984c74c48`），
2026-09-27 14:36–14:37跑直接Python **19 PASS**（8個新容量案例＋11個原metadata／撤權／匯入控制），
另完整DOM **78 PASS**。前後worktree均clean；原NONE回合省略新欄位，無snapshot不借目前facts補值。
真正producer案例先以原模型登錄／保存，再切換模型或移除目前facts，純GET仍讀回各自原snapshot；
bridge正反例則另以回應替身核decode、model與digest，不冒充新的producer或live驗收。

Python JUnit SHA256：`3dd9fbc11644e229234d043e5c585bdc46733cc9372a140bff6e1fc30b2cb979`；
execution：`a563715b9a6684068159004e9b6ceb56a50c4444c2733277040cf1cb0ccc95bf`。
DOM TAP：`5b7bc31e1deeceaf9ec985f108beb1cce75266098f77ae822d1d99cb97d41866`；
execution：`afd8919b185a5fa9a33b8a9ada9aeffbd92a221454cf458814fc718c3197e893`。
這些只支持容量metadata接線及未知值口徑；不證provider實效上限、完整request用量或語意換題品質。
帳冊643→651保完整前綴，另記原#85交付與本五批；SP-13／PA-16仍partial，沒有新增完整接受。

### Verified against

- API／DTO：`behavior_turn_context.read_saved_turn_context`與`behavior_work_policy_api.turn_context`。
- Client／呈現：`freeform.InstructionControl.read_turn_context`、
  `bridge.read_instruction_context`、`index.html::renderTurnContext`；沿用Clawpilot變數，沒有新套件。
- `test_turn_context_read.py`：signed API／真保存producer到bridge，SDK I/O為offline替身。
  `test_turn_context_capacity.py`：真正原登錄／保存／API的模型容量快照與另列的typed bridge consumer案例。
  `test_web_test_console_workbench.cjs`：minimal-DOM的手動GET、晚到／錯turn／登出及原答案／回條不變。
  重連反例另涵蓋舊成功／拒絕／讀取錯誤晚到、新讀取不被鎖住，以及AVAILABLE狀態下手動重連；
  不由這些離線案例推定真瀏覽器、跨帳號或現場已部署。
  這兩層都不當Hosted或自然模型品質；分批／固定來源見[Shared Context README](../README.md#每回合-context-的唯讀明細local未部署)。

### 2026-09-27：重連不復活舊 Context 明細（LOCAL，未部署）

原HTML只在當下未登入時清cache，未讓既有讀取失效；重新連線後舊成功可能填回metadata，
未完成讀取也會繼續鎖住同task的新讀取。修補只加獨立的Context讀取版本：
登入不可用或明確重連先清本機觀察／pending，舊response、error及finally均不能影響新版讀取。
原跨task結果、真錯誤提示、答案／草稿／journal與ACK語意不變，不增加API欄位或權限。

| 批次 | 來源與實際結果 |
|---|---|
| 新反例 | `4c9cada`＋test WIP，原HTML未改：5 FAIL，0 PASS |
| 修後定點 | 同base＋HTML/test WIP：8 PASS，含原Context控制；兩批baseline均null |
| 固定完整UI檔 | `cc83a7ebfdedcef64c9bd4001b28d4ff36f33204`，42 PASS＝37原案＋5新案，0 FAIL/skip |

固定批於2026-09-27T00:33:41+08:00執行，前後worktree均clean；TAP總耗時261.7051ms。
來源tree `8f76bdf5846481c8bd06b1a86378dfbdfd5b58a0`；
TAP SHA256 `c53a5a35e0243e766fc5e6d2fb54cabdbe41cdf18445148a1a3b9ef5a5dd8a2c`，
execution SHA256 `2dcfba4a8802632f66446ffd7195d056c22976b5209024ce3d4bee749b320eee`。
這是synthetic minimal-DOM，不是真browser、跨帳號、後端或部署驗收；沒有啟動SDK／模型，
沒有重啟使用者工作台。本機TAP匯入最初未處理CRLF而失敗，修讀取端後只重做結果登錄，沒有重跑或改判測試。
SP-13只更新partial，PA-16原接受基準與四個C／12情境不變；三批追加到帳冊，不回填原FAIL。

## 一般使用者畫面

主畫面只保留 Agent、聊天訊息、模型選擇與輸入。P05 後 legacy 固定測試與預算／期限面板也不再留在診斷區；舊 config／journal 只作相容。SHA、工作識別碼、回條與錯誤細節均為次要資訊；沒有新增 controller、批准或後端執行流程。

- 已交辦或交辦回應未知時，主畫面不再引導重新送出。重啟後仍看原紀錄，不重新建立工作。
- 已核對結果／已確認收件優先於舊取件的 NOT_STARTED 觀察；舊觀察和原 UNKNOWN 在明細保留。取得結果不等於收到；只有原 ACK 或原 failure ACK reconciliation 的 `client_received` 回條才顯示已收到。收件回應未知不因 GET 顯示 fulfilled 而改成成功。
- 取消結果只按已驗 payload 的已知欄位整理：工作已取消、是否已保存資料、模型結果是否仍未明、未驗證正常完成、預算未退回。保存不等於正常完成，取消不表示退款；未明操作只在主區顯示筆數，完整識別碼留在明細。
- 正常固定 metrics 只顯示真回傳的三個整數欄位；不填預期答案。無法辨認的格式明示不能整理，原始內容及 payload 下載 bytes/SHA 不变，不把取消資料或保存狀態轉成成功答案。

原 config／journal 舊列與 sidecar 不變；文字交辦有獨立的本機原文 intent，沿同一 authority／budget，而不是新的權威。單檔 HTML 仍用 Clawpilot 變數／Segoe UI，未加入框架或套件。直接測試：

```powershell
node --test .\tests\shared_context\test_web_test_console_workbench.cjs
```

這是 minimal-DOM 離線測試，不是瀏覽器或原生验收。原 `preview` 命令仍不接後端；正式啟動參數與 journal 不需改。

新增的`retained answers survive another turn's preview eviction and page reconstruction`案例，
同時保留前回合的完整已保存答案與後回合的暫存preview。Preview消失、頁面重新建立或後回合
取得正式答案，都不得刪改前回合文字；沒有server可追問狀態前仍不可續問，也不因render而POST或ACK。
這只核薄client投影，不是後端compaction／Azure保存、真瀏覽器reload或human-read證據。

### Verified against

- 此呈現規則的產品基準為`cb7a2d8b6868e06d9a941a6bdeb77ba399252159`；
  已讀`index.html:464–511`的`renderConversation`：依原task分離已保存reply與暫存stream，
  使用`textContent`，不因畫面重建送出ACK。
- 本次只新增上述minimal-DOM回歸與本段說明，沒有修改HTML、bridge、journal或後端。
  第一個WIP targeted run為1 passed；之後在`234dde4b8de1d460a0715e0263a4cfcb3ce3a0f3`
  提交後跑完整suite，32 passed／0 failed。Node `v24.19.0`，JUnit SHA256
  `4d4a606db63fd09ef81b825f71e1d959dfdee2e0f1b3be9ab96d110434ab6d5c`；
  不把這個minimal-DOM批次當瀏覽器或model/backend執行。

## legacy normal／standing／steady 相容摘要（P05 後）

P05 後，工作台不再提供 legacy `normal_pull` 固定工作、預算／期限 gate、原取件續接或 normal 專用操作批准。舊段落保留為本摘要，避免讀者誤以為這些診斷入口仍可用。

- `fa-web-test-console-v1`、`fa-web-test-console-steady-v2`、`fa-web-test-console-standing-v3` 仍可解析舊 tasks 與 standing policy 欄位；這是部署／退版相容，不是 UI 可執行工作。
- `work_policy.py` 只保留 job-bound instruction lane 需要的 typed observation DTO 與核對 helper；不再提供 normal fixed-work `query_for` 或 `blocking_code` gate。
- `standing.py` 的 preflight checker 仍供 `InstructionReader` 使用；normal `AssistantSubmit` 固定工作不再由 bridge 建立、顯示或送出。
- `pull_continuation.py` 只剩 sidecar schema，CLI 參數仍接受；runtime 不再讀取、核對或續接 normal pull。
- `original_failure` 是正式歷史轉交讀取路徑，仍保留；它不啟用 normal submit。

若要查舊設計，請用 git 歷史；目前 README 只記錄 P05 後的有效行為。


## 先預覽，不接雲端

從 repo 的 `src\fa-shared-context-poc` 執行：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& '<既有 Python interpreter>' -B -m web_test_console preview --port 8765
```

開啟 `http://127.0.0.1:8765/`。沒有配置、登入、範例答案或後端請求。預覽按鈕保持停用。可用 `?scoutTheme=light` 或 `?scoutTheme=dark` 切換 Clawpilot 主題。

## 原 v1 短期配置（相容，不延期）

先輸出 schema；此命令不產任何可用日期、批准或 request ID：

```powershell
& '<既有 Python interpreter>' -B -m web_test_console schema
```

舊配置採 `fa-web-test-console-v1`，由父端從**實際已批准**的 scope／binding 填入，不由瀏覽器傳入。以下全局時效欄位只屬於 legacy v1，不是 steady 的要求：

| 欄位 | 要求 |
|---|---|
| `backend_origin` | 一個精確 HTTPS origin；無 path、query、port 或 redirect。不是任意 URL proxy。 |
| `tenant_id`／`human_oid`／`client_id`／`audience` | 原 tenant、指定真人與 delegated client。後端仍用真 JWT 驗證簽章與角色。 |
| `scope_id`／`approval_reference`／`approval_sha256` | 本次有限批准的原引用；client 不產批准。 |
| `not_before`／`expires_at` | 父明定的 UTC 有限窗口，最多四小時；不延後端 policy 或舊 claim。 |
| `backend_configuration_sha256`／`backend_core_sha256` | 父核過的部署 pins；此通用 API 沒有 code attestation endpoint，client **不宣稱自己已驗 live code pin**。 |
| `cli` | 既有 Azure CLI 的絕對 executable 路徑、專案專屬 `azure_config_dir` 與明確 subscription ID。 |
| `tasks` | 最多 16 個不同 binding；指定 task/job/work/event/root-request/obligation/owner/route/channel-version 與三個固定 request ID。 |
| `allowed_actions` | `status`、`submit`、`fetch`、`acknowledge` 的明確子集；預設只有 `status`。 |
| `route` | 只允許原 `authenticated-pull`，recipient 必須是指定真人。`msteams` 不能透過此台履行責任。 |

P05 後，這些 `normal_pull` task 只為舊配置相容而保留在 schema；工作台不列出、不送出，也不再用它們的預算／期限或 preflight gate。`original_failure` 仍可走正式歷史轉交讀取路徑。

啟動時固定原配置 bytes 的 SHA：

```powershell
& '<既有 Python interpreter>' -B -m web_test_console serve `
  --config '<父已批准的固定 JSON>' `
  --config-sha256 '<該檔案 exact SHA-256>' `
  --journal '<父選定的持久本機目錄>\console.sqlite3' `
  --port 8765
```

`preview` 與 `serve` 兩個模式都不要開到網卡、反向 proxy 或 public cloud；遠端只用上方「遠端模式」的 `serve-remote`（Container Apps＋Entra 登入）。啟動不登入、不取 token、不送工作。第一次須手動按「連線」（讀取既有登入）；之後的自動更新沿用同一個 CLI：

`az account get-access-token --subscription <fixed> --resource <fixed> --only-show-errors --output json`

CLI不允許同時指定`--tenant`與`--subscription`；由固定subscription選取帳號後，
仍核對回傳的tenant／subscription與token中的身份，不因省略互斥參數放寬tenant界線。
子程序明確設定 `AZURE_CONFIG_DIR`，不呼 `az login`、不切全域 subscription、不建立 AAD app。token 只在 bridge 記憶體及到精確 origin 的 `Authorization` header；不進 HTML、JS、瀏覽器儲存、URL、argv、journal、ARM 或下載內容。本機 claims 核對只是送出前檢查，**不是 JWT 簽章驗證的替代品**；錯簽章仍由原 backend 拒絕。

### 既有登入自動更新

成功連線後，bridge 依 token 的 `exp` 提供下次更新時間，頁面約在到期前 5 分鐘呼叫本機
`POST /api/authenticate/refresh`。隱藏頁面先暫停計時，回到頁面時再核對；如果已到期但這個
bridge 仍保有先前的連線，就嘗試更新一次。從未成功連線、bridge 重啟或 legacy scope 到期時，
不會自動登入，也不會延長 scope。

自動與手動連線共用 `Console.authenticate`、原 `CliTokenProvider` 和相同的 `TokenVault`
身份／到期檢查。bridge 原有 lock 保證同時最多一個 CLI 取 token；重疊的自動要求在取得 lock
後重新核對更新時間。新 token 驗證成功才換入，不在重取期間先清掉仍有效的 token。
如果 CLI 仍回同一份快到期的快取 token，下一次只等到它到期，不立刻反覆呼叫 CLI。

CLI 失敗或 token 不合法時顯示 `LOGIN_EXPIRED`，停止自動重試，等待手動按「連線」；
晚到的舊畫面狀態不能把這次失敗改回可用。更新時間與失敗標記只留在記憶體，不新增持久儲存。
此流程不改 API 授權，不刷新工作批准、不清除原結果或 UNKNOWN，也不重送進行中或已送過的
submit、cancel、fetch、ACK 或其他工作 POST；只恢復之後正常使用 token 的能力。

只有 401 或登入問題的錯誤代碼（`LOGIN_*`、`IDENTITY_MISMATCH`、`PLATFORM_REQUEST_REJECTED`、遠端登入帳號不是指定帳號的 `REMOTE_PRINCIPAL_DENIED`）才停止自動更新。`REMOTE_SESSION_CHANGED` 不停止：它要的是重新整理頁面（有草稿時先複製），不是重新登入。其他失敗（例如助理目錄暫時讀不到的 503、ingress 的 5xx）不停止自動更新，也不導向登入頁，1 分鐘後再試；多助理模式下，自動更新已換到新 token 但助理目錄讀取失敗時，回應仍是成功，目錄錯誤只顯示在 `agent_directory_error`（和頁面載入時相同）。

#### Verified against：既有登入自動更新

- 程式基準：`1c541c9` 加本次 console 差異；已核 `adapter.TokenVault`、
  `bridge.Console.authenticate`／`authentication`，以及 HTML 的 `scheduleAuthentication`／`refreshAuthentication`。
- 直接反例：`test_web_test_console_steady.py` 的到期案例先得到 `401 LOGIN_EXPIRED`；
  更新後原純讀回 `200`，CLI provider 共呼叫兩次（首次連線加一次更新），原工作 POST 仍只有一次。
  同檔另核 5 分鐘邊界、重疊更新、CLI／身份拒絕後停止及快取 token 不迴圈。
- 前端：`test_web_test_console_workbench.cjs` 以可控時鐘核更新時間、進行中 POST 不重送、
  失敗待手動連線、舊狀態不覆蓋失敗，以及回到可見頁面時補更新。
- 都是合成 token／離線 transport；不代表已在 live 8765 換版，也沒有執行真 Azure 登入或雲端操作。

## 一次性與收件語意

| 操作 | 真正呼叫與限制 |
|---|---|
| 固定測試 | 普通助理 `POST /behavior/v1/bindings/{binding_id}/assistant/submit`；lifecycle 則既有 `/work/submit`。先用 SQLite FULL transaction 保存原 request ID／body SHA／非 secret intent，再只 POST 一次。 |
| 純讀核對 | `GET /behavior/v1/bindings/{binding_id}/jobs/{job_id}`。不呼 controller、`workers.reconcile`、callback、model 或任何 query reservation。沒有自動輪詢。 |
| 取得原結果 | 先純讀 status 確認有 delivery ID，再由使用者手動 `POST /delivery/pull` 一次。不是每次重整 fetch；保留原八次配額，client 自己只用一次。 |
| 顯示 | 先核原 bytes 長度／SHA、五個 headers、typed `DeliveryBinding`、原 task/work/event/root/owner/recipient/channel 與 payload artifacts，再以 `textContent` 顯示。失敗仍顯示失敗；空內容不填 dummy metrics。 |
| 確認收到 | 使用記憶體內原 exchange／ACK token／binding／digest，沿 `wire.PullAcknowledgment` 手動 `POST /delivery/acknowledge` 一次。只接受原 receipt 的 `client_received`／fulfilled／原 obligation、channel 與 evidence/request identity。不是 `provider_accepted`，也不是真人已讀。 |
| 下載 | 原 verified payload bytes 或已檢查的收件摘要；不含 bearer 或 challenge token。摘要明列觀察時間、不是目前 live 結果。 |

`/work/reconcile` 不是此台的輪詢：現有 runtime 後續仍可能 `_enqueue`。這個 client 因此**不呼叫它**。失聯、timeout、commit unknown、失去 challenge 時只保留原 intent；不換 ID、重新 ACK POST、返還 budget 或自稱已收到。GET status 顯示 fulfilled 也不會自動把未知 ACK 改成 client_received。僅 `original_failure` 有下述明確手動「核對原收件回條」；其他模式仍由管理者按原 requests／receipts 處理。

雙擊、重整及同 journal 重啟不再送提交／fetch／ACK。不同配置不能打開同 journal 來重置已送狀態。不要刪 journal 或換路徑避開未明責任。journal 是 client 的 once-intent 記錄，不是新的後端權威；它無法阻止人手刪檔或另造任意 client。

challenge 和原 payload 只在單一 bridge process 記憶體；重啟後明列 `CHALLENGE_LOST`。已保存的正向 ACK 回條可唯讀下載，但標成**歷史觀察，不是目前 live 結果**。新登入不恢復舊窗口權利。

## 原 failure：正式獨立轉交接點

此 adapter 依 frozen `web-handoff-manifest.json` SHA `e5b12d0a02dcc7da99b74284eea19784c7aebbcbbceb9cce471e567d97218c29`、`web-handoff-contract.md` SHA `efb171996cf9d10b57dcfcd16795d094bb76fc04ac01662fbce591b63ed49f33` 實作。後端是 `OriginalFailureWebHandoff`，不是一般 msteams→pull 旁路。

- `GET /web/v1/original-failure/{handoff_id}/status`
- `POST /web/v1/original-failure/{handoff_id}/fetch`，body `wire.Command`
- `POST /web/v1/original-failure/{handoff_id}/acknowledge`，body `wire.PullAcknowledgment`

`OriginalFailureAdapter.paths()` 只產以上三條固定路徑；手動原 ACK 核對另以固定 `/reconcile` 路徑實作，沒有任意 path／operator 設定。父先在**獨立 operator 平面**完成原 signed source、positive stop、cancelled result 與 typed web handoff 的批准；UI 永不提供 preview／authorize／transfer／stop／model。

可信 task 使用 `mode=original_failure`、原 `handoff_id`、`allowed_actions` 只含 `status/fetch/acknowledge`。另外提供 `original_failure` typed 參考：

| 欄位 | 來源／要求 |
|---|---|
| `contract` | 固定 `fa-original-web-handoff-status-v1`，不是 enable boolean。 |
| `payload_sha256`／`payload_bytes` | 父從已核的原 `WebHandoffPlan.payload` 取得 exact hash／長度，不由 HTTP200 或頁面自報。 |
| `delivery_entry_id` | 原 `config.delivery.entry_id`，等於 task web route.account_id。 |
| `owner_conversation_id` | 原 owner.conversation_id，等於 task web route.thread_id。 |

task 的其他 work/event/root-request/owner/obligation/job/recipient 都是原件，不能自行建立。delivery ID 必須是 `control_key("delivery-final-id", original_obligation_id)`、version=1、artifacts=[]，web route 為原 tenant／原真人／authenticated-pull／`fa-pull-inbox-v1`。缺 `original_failure` 的舊配置仍保持 `RETAINED_BACKEND_NOT_VERIFIED`；normal 模式禁止帶此欄位，舊 normal JSON 序列化不新增 null 欄位。

receiver 使用原同真人 delegated **Behavior.Read**（原 verifier 已映射 acknowledge），不需要 UI 取得 Approve。所有請求仍到固定 HTTPS origin；先 exact `WebHandoffStatus` 核 handoff/job/obligation/delivery/payloadSHA、cancelled、pending/fulfilled 與確認層級的一致性。status 到期仍可純讀；`expired=true` 只停用這筆 fetch/ACK，不關閉 steady 介面或其他工作。

fetch 仍先核原 bytes／五個 headers／shared `_Payload`／typed Binding 與原 task、root、owner、recipient。再核這份原 saved failure 的 SHA／長度、version1、failure+cancelled、result evidence、空 artifacts／known effects／unresolved operations 與原 notification code。不重新序列化、不填 dummy 答案。fetch 後仍 pending。

ACK 回應外殼**恰** `result/control_request_id/control_request_digest`，不接受額外 `guard_revision`。result 也只接受正式五欄，必須 fulfilled/client_received/[client_received]、同 handoff、`fulfillment_receipt_id=control_key("inbox-ack-evidence", delivery_id)`；`control_request_id=handoff_key("fulfillment", handoff_id)`。

**信任界線**：公開 response 沒有完整 `VerifiedDeliveryReceipt`。client 只能核 control digest 的合法 SHA256 形式與上述原 IDs／回條語義，不能拿 `hash(result)` 冒稱重算 server proof。完整 canonical proof、request/read-set/write-set/history 由可信 backend 驗。本批真 producer ASGI 整合測試把 UI 收到的 digest 與原 `ControlRequest` receipt、實際 `VerifiedDeliveryReceipt` canonical digest 逐一比對；此測試不是 client 額外造一層權威。

同 handoff 換 fetch request ID 不會取得新 exchange；client 更保守地只用原 fixed request 一次。ACK／履行回覆遺失、重啟或失去 challenge，都保留原 intent／UNKNOWN，不重 POST、不新 ID。GET status 顯示 fulfilled **不會**改本機 UNKNOWN 為已收件。

### 手動「核對原收件回條」

只對 `original_failure`，且既有 `allowed_actions` 已允許 `acknowledge`，才提供此按鈕。**不用改 config schema／config SHA、scope、日期或原 journal 路徑**。原資料库必須已有 `phase=acknowledge` 的 durable intent，而且 outcome 仍 `UNKNOWN`；沒有原 ACK、已確認或錯工作都拒絕。

使用原 journal 已存的 typed `binding`／`exchange_id`／`request_id`／`body_sha256`／配置 SHA 核對同 handoff、原 payload SHA、version1、空 artifacts、原 recipient/route/channel。只接受原 ACK path/method/request ID；不從 browser 接受 ID、token 或 Binding。**不依賴已消失的 challenge 或 payload，不重新合成 ACK**。

原 `body_sha256` 是當時確切 ACK bytes 的摘要。因 challenge 刻意不落盤，重啟後不能獨立重算該原 ACK digest；client 驗格式與其固定原 intent 關聯，並把原 intent SHA／body SHA 綁進新的 reconcile intent。它不是證明伺服器已收到 ACK：原 ACK 是否存在、是否可完成履行，仍由正式 backend 核對，不以 GET fulfilled 代替。

點擊後先以同真人登入核對目前 status；expired 拒絕寫入。接著在原 journal 用 FULL transaction **另存** `phase=reconcile` 的一次性 intent，再送：

```text
POST /web/v1/original-failure/{同原handoff_id}/reconcile
{"request_id":"原ACK完全相同的request_id"}
```

這可能補完已持久 ACK 的原履行 EGT，**不是 pure GET**。按鈕旁會明說可能補履行，只由人手點擊一次，不由 login／status／fetch／refresh／poll 觸發。不 fetch、不重送 ACK、不造新 exchange、ID、批准或預算。

回應重用與 ACK 同一正式三欄外殼／五欄 result 校驗：原 deterministic control/evidence/handoff IDs、client_received／fulfilled 語義，以及 control digest 的合法 SHA256 形式。完整 `VerifiedDeliveryReceipt` 未在 response 出現，不能虛構 client 已獨立驗 server-only proof。

成功另存 reconcile outcome／原 receipt，**原 ACK UNKNOWN row 的 intent 與 outcome bytes 完全保留**。畫面顯示「原ACK回應遺失；已核對原回條client_received（未重送）」，收件摘要含 `confirmation_source=original_ack_reconciliation`、`original_ack.state=UNKNOWN`、新的 `reconciliation` 回條與 `ack_resent=false`；失敗／取消的工作狀態不變，不表示真人已讀。

如果 reconcile 自身 timeout、回覆不合法或拒絕，該 phase 保持 UNKNOWN，不重 POST；雙擊、refresh、重啟或不同 bridge instance 使用同 journal 都只觀察原 attempt。再次需要處理由管理者按原 ID 做獨立核對。原 backend grant 已過期時不寫，GET status／本機歷史摘要仍可用。token 正常過期／wrong human／CSRF 保護不變。

本補丁保留原 HTTPX 90 秒 read timeout、connect10、原 bounded body時間與 retries=0；**沒有把 native BACKEND_OFFLINE 直接歸因到某個 transport timeout，也沒有拉長 timeout 充當修復**。保留父既有 CLI 修正：get-access-token 只傳固定 subscription+resource，核回應 tenant/subscription；不恢復互斥的 `--tenant`。

status、fetch、ACK、reconcile 的 authorization/grant 期限都由原 backend 控制，不延舊 cleanup／window，也不把原 Teams route 改成 pull；只有正式 typed transfer＋原 web receipt 才解除原 pending。

## 本機驗證

本批 standing 正式 adapter 的直接測試：

```powershell
$env:PYTHONPATH = '<repo>\src\fa-shared-context-poc;<repo>\tests\shared_context'
& '<既有 Python interpreter>' -B -m pytest -q -p no:cacheprovider `
  '<repo>\tests\shared_context\test_web_test_console_standing.py' `
  --basetemp '<本批 artifact>\pytest'
```

正式慢 fixture 依父凍結的 Window／原 signed／stop／result／handoff producers，用本批 `run_tests.ps1` 的 isolated cwd／PYTHONPATH 串行跑。一次閉環後以同一孤立 snapshot 測 expiry、owner 撤權、ACK 與 fulfillment 真 EGT ACKloss，不需重跑原後端 22／72 suites。其他 HTTP 範例反例使用已核 SHA 的 `tests\shared_context\web_test_console_original_failure\producer-example.json`，它只是正式離線 producer 原件重播，不是 native 配置。

真 API／JWT／原 producer 的 browser fixture **只在 tests**，不由正式 launcher 匯入：

```powershell
& '<既有 Python interpreter>' -B '<repo>\tests\shared_context\test_web_test_console_browser_fixture.py' `
  --journal '<全新 offline artifact>\fixture.sqlite3' --port 8766
```

此 fixture 明標 `OFFLINE_TEST`，以本機 HTTPX→ASGI、記憶體儲存與 SDK/model 替身跑原成果與 receipt。只有 fixture 的獨立 controller 完成工作，正式 console 沒有此能力。不得把它放進 native image 或當 live 配置。

## 尚未證明

本批沒有 cloud、登入、部署、真模型、Teams／M365、Git 或新 agent 操作。截圖只證 localhost UI。正式 standing-v3 reader／submit 已接線並用真 backend ASGI/producer、合成 JWT 與離線 SDK protocol 替身驗證；不代表 native Hosted 正常工作已通過。原 failure 已由父原生結案，本包不重做其 fetch/ACK/reconcile/stop。父仍須依 actual merged code、正式已安裝 policy／fresh cost／原權利選可信 client 配置後驗原生流程；此包不產真 scope/date/grant。

官方參考：[HTTPX transports](https://www.python-httpx.org/advanced/transports/)、[FastAPI sync handlers](https://fastapi.tiangolo.com/async/)、[Azure CLI configuration](https://learn.microsoft.com/en-us/cli/azure/azure-cli-configuration)、[Azure CLI access tokens](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli)。未新增依賴或 publiccloud 資源。
