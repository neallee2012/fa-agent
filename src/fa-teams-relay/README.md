# Teams relay（N2 T1，2026-10-10，CODE/offline，未部署）

在 Teams 裡和 FA Agent 對話的轉送 agent。設計見 [teams-chat-design.md](../../docs/teams-chat-design.md#n2-設計)。
每個 FA Agent 發布一個 relay（Foundry 入口網站一鍵發布到 Teams）。它不叫模型、不存東西、不碰 Teams API。

**每則訊息。**
1. `ResponsesAgentServerHost`（`azure-ai-agentserver-*` 2.2.0，與 10-10 echo 探針同一組釘版）收到平台的 Responses 請求。
2. 平台沒帶 `x-agent-user-id` 就回「這個助理只回應它的主人。」，不呼叫 FA API。只收文字：輸入裡有圖片、檔案或其他非 `input_text` 內容就回「v1 只收文字。」，不呼叫 FA API。
3. 組 `RelayTurn`：`activity_id`＝body `metadata.ActivityId`、`chat_id`＝body `conversation`、
   `user_oid`＝header `x-agent-user-id`（SDK 的 `platform_context.user_id_key`）、`text`＝輸入文字。
3b. 訊息去頭尾空白後若全文符合 `(允許|拒絕)\s+([0-9]{6})`，改組 `RelayDecision`（`chat_id`、`activity_id`、`user_oid`、
   `choice`＝`allow_once`／`deny`、`code`）打 `POST {BEHAVIOR_API_URL}/teams-relay/v1/decisions`，不進 `/turns`、不進模型；
   其他（含「允許他吧」）照常當一般訊息。輪詢與 110 秒上限規則相同。
4. 用 relay 自己的 managed identity 取 `BEHAVIOR_API_AUDIENCE` 的 token（和 FA Agent 呼叫 API 薄轉送同一種），
   `POST {BEHAVIOR_API_URL}/teams-relay/v1/turns`。同一則訊息每次送一樣的 body。
5. 約每 3 秒再送一次，直到 `state` 是 `done`、`needs_approval`、`busy`、`refused`、`failed`，把 API 的 `text` 當回覆。
   API 回 4xx 就回「暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。」不再送；5xx 或連線錯誤照常再試。
6. 110 秒內沒有結果：曾收到 2xx `running` 就回「這一輪還在跑，完成後結果在工作台。」，回合本身照常跑完，結果只在工作台；
   一次 2xx 都沒有就回「暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。」，不自動重送。

**環境變數。** `BEHAVIOR_API_URL`（FA API 的 HTTPS origin）、`BEHAVIOR_API_AUDIENCE`、`AZURE_CLIENT_ID`（可省略）。
API 端要設 `FA_TEAMS_RELAY_ENABLED=1` 和 `FA_TEAMS_RELAY_AGENTS`（見
[Shared Context README](../fa-shared-context-poc/README.md#teams-relay)）。

**測試。** `tests/fa_teams_relay/test_teams_relay.py`：Starlette `TestClient` 打真的 ASGI app，FA API 是
`httpx.MockTransport` 假 server，時鐘與 sleep 可注入（110 秒上限不必真等）。

**還沒驗證。** image 沒有 build、沒有部署；`pip check` 只在 echo 探針的 Docker build 跑過同一組（多了 httpx／httpcore，
兩者的相依都已在清單內，但這份清單本身還沒 build 過）。平台實際送來的 body 欄位以 10-10 探針為準。
