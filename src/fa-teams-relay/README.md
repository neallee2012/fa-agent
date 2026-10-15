# Teams relay（N2 T1，2026-10-10）

**狀態：** 已部署在作者的測試環境（dev）並從 Teams 實測；預備環境（stage）沒開。實測紀錄見 [progress](../../docs/progress.md#teams-relay-observed-1011)。

在 Teams 裡和 FA Agent 對話的轉送 agent。設計見 [teams-chat-design.md](../../docs/teams-chat-design.md#n2-設計)。
每個 FA Agent 發布一個 relay（Foundry 入口網站一鍵發布到 Teams）。它不叫模型、不存東西、不碰 Teams API。

> 補上先前的回覆（2026-10-14，CODE／offline，未部署）：第 4～6 步的 header、ack 與逾時文字只在這個版本的 image；
> dev 上目前的 relay 還是舊版。新舊 relay／API 任意搭配都照常運作，見 [Teams 對話設計](../../docs/teams-chat-design.md)「新舊版本混用」。

**每則訊息。**
1. `ResponsesAgentServerHost`（`azure-ai-agentserver-*` 2.2.0，與 10-10 echo 探針同一組釘版）收到平台的 Responses 請求。
2. 平台沒帶 `x-agent-user-id` 就回「這個助理只回應它的主人。」，不呼叫 FA API。只收文字：輸入裡有圖片、檔案或其他非 `input_text` 內容就回「v1 只收文字。」，不呼叫 FA API。
3. 組 `RelayTurn`：`activity_id`＝body `metadata.ActivityId`、`chat_id`＝body `conversation`、
   `user_oid`＝header `x-agent-user-id`（SDK 的 `platform_context.user_id_key`）、`text`＝輸入文字。
3b. 訊息去頭尾空白後若全文符合 `(允許|拒絕)\s+([0-9]{6})`，改組 `RelayDecision`（`chat_id`、`activity_id`、`user_oid`、
   `choice`＝`allow_once`／`deny`、`code`）打 `POST {BEHAVIOR_API_URL}/teams-relay/v1/decisions`，不進 `/turns`、不進模型；
   其他（含「允許他吧」）照常當一般訊息。輪詢與 110 秒上限規則相同。
4. 用 relay 自己的 managed identity 取 `BEHAVIOR_API_AUDIENCE` 的 token（和 FA Agent 呼叫 API 薄轉送同一種），
   `POST {BEHAVIOR_API_URL}/teams-relay/v1/turns`。同一則訊息每次送一樣的 body。一對一對話的每個請求都帶 header `X-FA-Relay-Ack: 1`，
   告訴 API 這個 relay 會 ack（舊 API 會忽略它；用 header 是因為 API 的 body 欄位嚴格，多一個欄位舊 API 會拒絕）。
   一對一是指 body `metadata.ActivityProtocolConversationId` 以 `a:` 開頭（Microsoft 範例裡 Teams 個人對話的格式；群組與頻道是
   `19:…@thread…`）；其他值或沒有這個欄位都不帶 header，API 就照補上功能之前的方式回覆，群組或頻道永遠不會附上主人其他對話的回覆。
   真實 Teams 個人對話的前綴還沒在 dev 讀回；若不是 `a:`，結果只是補上功能沒有作用，回覆照舊。
5. 約每 3 秒再送一次，直到 `state` 是 `done`、`needs_approval`、`busy`、`refused`、`failed`，把 API 的 `text` 當回覆。
   API 回 4xx 就回「暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。」不再送；5xx 或連線錯誤照常再試。
   最終回覆帶 `confirm`（一串 request ID，表示這段文字附了先前的回覆或這則自己被記過）時，先打一次
   `POST {BEHAVIOR_API_URL}/teams-relay/v1/acks`（`chat_id`、`user_oid`、`request_ids`＝`confirm`，最多等 5 秒），
   不論成敗都照常回文字；ack 失敗只代表 API 之後可能再附一次（罕見的重複），不會掉。
6. 110 秒內沒有結果：曾收到 2xx `running` 就回 API 最後一次給的 `running` 文字，relay 自己不另外承諾。新版 API 會記下這個回合，
   回「這一輪還在跑；完成後，回覆會附在你下一則訊息的回覆前面，也可以先到工作台看。」，下一則訊息（開了新對話也一樣）的
   最終回覆會附上它（見 [Shared Context README](../fa-shared-context-poc/README.md#teams-relay)「補上先前的回覆」）；
   較舊的 API 回「這一輪還在跑，完成後結果在工作台。」。回合本身照常跑完。
   一次 2xx 都沒有就回「暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。」，不自動重送。

**環境變數。** `BEHAVIOR_API_URL`（FA API 的 HTTPS origin）、`BEHAVIOR_API_AUDIENCE`、`AZURE_CLIENT_ID`（可省略）。
API 端要設 `FA_TEAMS_RELAY_ENABLED=1` 和 `FA_TEAMS_RELAY_AGENTS`（見
[Shared Context README](../fa-shared-context-poc/README.md#teams-relay)）。

**測試。** `tests/fa_teams_relay/test_teams_relay.py`：Starlette `TestClient` 打真的 ASGI app，FA API 是
`httpx.MockTransport` 假 server，時鐘與 sleep 可注入（110 秒上限不必真等）。

**驗證。** image 已在作者的測試環境 build 並部署（[progress](../../docs/progress.md#teams-relay-t2-1010)）；平台實際送來的 body 欄位以 10-10 探針為準。預備環境沒開，沒有部署腳本。
