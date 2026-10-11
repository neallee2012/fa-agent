# N6：Hosted 改用 Responses 協定 — 遷移設計（不含程式）

- 決定：Neal 2026-10-02 08:16 在 #321 留言「改成Responses」，取代 ADR-0007 §9 ①。
- 狀態：**總協調 2026-10-02 08:57 核准**，同時核准 N6-0 部署到 test agent 實測 D1～D4（條件比照 N0：同一個 build、不動 A／B、不新增資源或 IAM；遇到 403 或需要新 IAM 就停下）。實作依第 8 節的切片進行；N6-6 依 Neal 10-02 15:08 的決定不做（#396）。A／B 已依 Neal 08:18 的決定切到 Invocations native。
- **實測進度（10-02，main `21231bcd`，test agent 1，全程 API MI，fdbb 執行）**：D1、D2、D4 通過；D3 ①②③ 已做，④ 回 unknown，待 915 補診斷後下一批再跑。結果見第 9 節。
- 基準：main `75696b6f`（含 79fa #364：沒開始的 RID 只在明示 follow／retarget 時收成 `unknown/NATIVE_TURN_NOT_STARTED`，不跨 SID 重跑）。
- 官方查證全文（含逐字引文和 SDK tag 行號）：[n6-responses-official-research.md](n6-responses-official-research.md)。
- 撰寫：83d4；71c7 提供固定 SDK 與 N1／C0 接點的界線。

原則（原文）：
1. 「請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。」
2. 「「SDK-first／Hosted 優先」原則，在 FA 自己建一層之前，應該先確認官方功能跟最佳實踐，確認是否真的不足。」

## 0. 結論

1. **同一個容器同時提供 Invocations 和 Responses。** 官方明文支援多協定 host，用 mixin `class MyHost(InvocationAgentServerHost, ResponsesAgentServerHost)`[S15][S23]。所以不必一次切換：
   - **Responses** 負責對話回合，包括 Foundry 代管歷史、stored background 和 crash 重入。
   - **Invocations** 留作 FA 控制面，負責接單回條、讀回條、取消、交付、session 操作。這些讀寫若改走 Responses，會在對話歷史裡塞進假回合。
   - 舊的 Table lane 和 Invocations native 的 CID 都繼續可用，不搬資料。
2. **三個套件必須一起升級**：`azure-ai-agentserver-responses==2.2.0`、`azure-ai-agentserver-core==2.2.0`、`azure-ai-agentserver-invocations==1.2.0`。
   - invocations 1.1.0 要求 `core<2.2.0`，和 responses 2.2.0 衝突[S1][S4]。
   - 三者都是 2026-09 底剛發布的 GA；但 resilience 功能本身仍標示 preview[S26]。
3. **生命週期只認一個權威。**
   - 新 CID 的回合由 Responses host 的 resilient task 負責：`background=true`、`store=true`、`ResponsesServerOptions(resilient_background=True)`，`steerable_conversations=False`。
   - 「只執行一次」仍由 FA 的 `rid` 回條 CAS 保證。
   - 新 CID **不再使用** N1 的 `@multi_turn_task(task_id=<agent>)` chain，不疊兩層 lease 或 queue。
4. **官方缺口一：沒有 Agent 層級的互斥。** 官方只依 `conversation_chain_id` 序列化同一個對話，沒有跨對話、整個 Agent 或整台 VM 的互斥（查證結論為 not provided）[S16][S48][S49]。
   - FA 用行程內 `asyncio.Lock` 自己補上一個最小互斥。官方範例的 `_session_lock` 只保護建立 session[S42]，不能當成整個回合加鎖的官方先例。
   - 前提是同一個 Agent 的所有 CID 仍共用一個 Hosted session，見第 4 節的 D1。**D1 已通過**：兩個 conversation 落在同一個 SID。
   - **官方缺口二：`previous_response_id` 模式沒有序列化。** 沒有 `conversation` 時，官方使用 one-shot task，重疊請求不會回 409[S50]。所以「同一個 CID 同時只能有一個未結束的 RID」改由接單步驟保證，不依賴 409。D1 通過後，新 CID 走 `conversation` 模式，但接單步驟的保證照樣保留。
5. **重入不是重跑。** 官方原文：「Recovery reenters the handler from its beginning. It isn't deterministic replay」[S26]。
   - ADR-0007 §1 的重入規則和 #364 的底線照舊：`started` 但無法證明完成的回合標成 `unknown`，不重跑模型或外部效果。
   - `resilient_background` 不能用來重跑既有的 unknown。
6. **省下的 FA 自建部分不多，主要是新增能力。**
   - 能省：新 CID 的 Agent chain、8765 讀歷史時不必喚醒 VM、取消與 SSE 重播改用官方機制。
   - 省不掉：`rid` 回條、effects、N3 checkpoint（Copilot SDK 仍需要）、Agent 層級互斥、T5 relay、T1 入口、用量帳。
7. **T5 Agent 之間互傳仍走 FA relay，只把傳輸改成 Responses。**
   - 官方 A2A 收件端只拿得到平台驗證過的不透明 `x-agent-user-id`[S24]，對不到 FA 的 Agent 或 Team。
   - 沒有 hop 上限和去重規則，而且 v1.0 只支援文字[S30][S31]。
   - 對外開放官方 A2A（N6-6）依 Neal 10-02 15:08 的決定不做（#396）。
8. **N6 主體預計不新增 IAM。**
   - API MI 和所有 Hosted agent identity 都已有 project 範圍的 Foundry User（README:9581、#306）。
   - 官方說 A2A 呼叫端需要「Foundry Agent Consumer role or another Foundry role that grants endpoint access」[S30]。
   - 是否確實夠用，列入 N6-0 實測。
9. **A／B 建議先切到 Invocations native 當過渡**（第 7 節）。不會重工，因為 Responses lane 沿用同一套 State store 和能力接點，Invocations native 的 CID 也繼續有效。

## 1. 固定版本與部署接點

| 項目 | 現在（`75696b6f`） | N6 |
|---|---|---|
| Hosted 依賴（`requirements-hosted.txt:1-2`） | invocations 1.1.0、core 2.1.0 | responses **2.2.0**、core **2.2.0**、invocations **1.2.0**。三行同一個 PR 一起改 |
| API image（`Dockerfile.api`、`requirements.txt`） | 不含 agentserver 套件 | 不變。API 用既有 httpx client 呼叫 Responses endpoint，不新增 SDK |
| Host（`behavior_hosted.py:760-790`、`hosted_main.py:7,14`） | `InvocationAgentServerHost` | `class FaHost(InvocationAgentServerHost, ResponsesAgentServerHost)`[S15]；只在新旗標下註冊 `@app.response_handler` |
| `protocol_versions`（`controller/private_controller.py:54`、`deploy/.../agent.yaml:7`、`behavior_agent_versions.py:223-228`） | `invocations 2.0.0`；桌面另加 `invocations_ws` | 加一筆 `{"protocol":"responses","version":"2.0.0"}`[S28]。容器協定版本仍是 2.0.0，不是套件版本 |
| PR7 sync（`behavior_agent_versions sync`） | 複製 A 同 build 的定義 | 不變。A 的定義加上 responses 後，sync 和 onboarding 自動帶到所有 Agent |
| API／Hosted 同 build（SdkStartGate `code_sha256`、`fa_build`） | 必須同 build | 不變。只有 active version 的定義含 responses 的 Agent，API 才會把新 CID 交給 Responses lane |
| Endpoint | `/agents/{name}/endpoint/protocols/invocations` | 另加 `/agents/{name}/endpoint/protocols/openai/responses`，以及 conversation 建立用的 `.../openai/conversations`[S23][S29] |

升級時要一起確認的官方變更：
- core 2.2.0 新增 `AgentConfig.session_guid`，Azure Monitor 預設取樣改成 100%[S10]。FA 目前 `configure_observability=None`，預期不受影響，要以測試確認。
- responses 2.2.0：
  - 用量欄位 `cache_write_tokens` 變成必填[S11]；FA 在 response 物件回報用量時要帶上。
  - 歷史抓取筆數預設改成不限[S11]。FA 不用 `get_history()`，和官方範例相同，所以不受影響。
  - task id 會帶上 `FOUNDRY_AGENT_SESSION_GUID`[S11]，所以新 session 不會接手舊 session 的 task。這和 #364「不跨 SID 重跑」一致。
- invocations 1.2.0 只在呼叫端沒帶合法 ID 時才補 fallback ID[S12]。FA 本來就會核對回傳的 ID header，要以回歸測試確認。
- #364 依賴固定 core 2.1 的 `TransportClassifiedError` 和 task GET 分類。core 2.2 仍有 `_raise_classified` 分類矩陣[S54][S56]，**N6-0 要對 #364 跑一次回歸**。

## 2. 新 CID 的回合生命週期

**請求形狀（API → Responses）**
- `POST .../openai/responses`，body 包含：
  - `input`：原本那一則人類訊息
  - `background=true`、`store=true`
  - `agent_session_id`：該 Agent 的 FA session，放在 JSON body 的**最上層**。官方範例用 OpenAI SDK 的 `extra_body={"agent_session_id": ...}` 傳入，SDK 會把它併到 body 最上層，不是另一層 `extra_body` 物件[S58]。FA 用 httpx 直接送 raw JSON，實作時以官方範例的實際 wire 核對；N6-0 的 D1 一併確認。
  - 串接方式依 D1 二選一。**D1 已通過，新 CID 用 `conversation`**：
    - `conversation`：Foundry conversation id。
    - `previous_response_id`：上一回合的 response id。官方明示 session 範例用的就是這種[S58]。D1 通過後只留作備用，不實作。
- FA 的 RID、CID 和 digest 放在 `x-client-fa-rid`、`x-client-fa-cid`、`x-client-fa-submit-digest` 三個 header。
  - gateway 只轉送 `x-client-*` 開頭的自訂 header[S24]。FA native 現在用的 header 本來就是這個前綴（`behavior_native_turn_wire.py:30`、`behavior_wire.py:27-29`）。
  - RID 和 digest 不放進 `metadata`，避免寫進保存的 response。`metadata` 只放範圍核對用的 `fa_agent_id` 和 `fa_team_id`（見第 3 節）。
- `background=true`、`store=true` 加上 server 端 `resilient_background=True`，才會在 crash 後重入[S27][S47]。少了任何一項，crash 後只會被標成 `failed`，不會重入。

**接單仍由 FA 負責，分成兩步**
1. API 先用既有 Invocations 控制面送 `NativeSubmit`（只接單），完整 capture 寫進 Agent 的 `rid` 回條。
   - capture 太大，放不進 Responses 請求；也不能放進 input，否則會進入對話歷史。
   - **同一個 CID 同時只能有一個未結束的 RID。** `conv/<CID>` 的 `meta` 用單 key CAS 記下 `inflight_rid`，順序是：
     1. 先建立 `rid` 回條（`accepted`）。
     2. 再以 `if_match` 把 `meta.inflight_rid` 設成自己。前一個 `inflight_rid` 的回條已經結束（`completed`、`unknown` 或 `busy_refused`）時，才可以接手。
     - 判斷時直接讀 `inflight_rid` 指向的回條，不必另外清除，所以 crash 不會留下卡住的標記。
     - **沒拿到名額時，不執行第二步**：
       - 真人送出：可以把自己的回條 CAS 成 `busy_refused/NATIVE_TURN_BUSY`。這是明確的忙碌，客戶端之後可以改用新 RID。
       - T5 的 `agent_request` 和 wake：**不可以**收成終態，否則結果會遺失。回條維持 `accepted`，之後用同一個 RID 重送，再爭取一次名額。
       - 名額 CAS 結果不明，或在建立回條後就 crash：同樣維持 `accepted`，用同一個 RID 重送時再爭取一次。
     - 兩種串接模式都適用，`previous_response_id` 也從這裡取得前一回合的 `response_id`，不會分叉。
2. API 再送 Responses 回合，只帶 `x-client-fa-rid` 和 digest。
   - handler 讀回 `rid` 回條，核對 input 的 SHA 等於回條中原訊息的 digest，不一致就拒絕。
   - 第二步沒收到 ACK 時，回條維持 `accepted`。用同一個 RID、在同一個 SID 再送一次 Responses。handler 看到 `rid` 已是 `started` 或之後的狀態，就只回原狀態，不重跑。
   - **已知代價**：Foundry 歷史可能多出一則重複的使用者訊息。這只是畫面上的重複，不是重新執行。

**`accepted` 怎麼收尾，不會一直懸著**

| 情況 | 處理 |
|---|---|
| 第二步結果不明、收到 409 `conversation_locked`，或還沒拿到 CID 名額 | 維持 `accepted`。之後在同一個 SID 用同一個 RID 再送；絕不換 RID，也不跨 SID 執行。重送由下方三個地方負責，不靠使用者再操作 |
| 使用者在第二步之前或等鎖時取消 | 既有 `NativeCancel` 把 `accepted` CAS 成 `unknown/NATIVE_TURN_CANCELLED` |
| 平台在 handler 前明確拒絕，且能證明沒有建立 response | **目前一律維持 `accepted`。** 還沒有可核對的平台依據（錯誤碼、格式）能證明請求在 handler 前就被拒、也沒有建立 response。400、404、502 都不能光憑狀態碼判定（79fa 10-02）。<br>・永久性錯誤卡住 CID 時，由使用者取消（`NATIVE_TURN_CANCELLED`）釋出名額。<br>・重送方遇到真正的錯誤，照原錯誤界線和重試上限處理，不會無限重送。<br>・等 D 實測取得可核對的錯誤碼或格式後，才把這類情況收成 `unknown/NATIVE_TURN_NOT_STARTED` |
| 明示 follow／retarget | 依 #364 收成 `NATIVE_TURN_NOT_STARTED` |

**誰負責用同一個 RID 重送**（都是同 RID、同 SID，可重複執行，不會造成重跑）
1. **T5**：既有 relay 的 resilient follow task（固定 message identity）會一直重送，直到第二步已被接手，或回條已經結束（fcd 確認，由 fcd 在 N6-4 接上）：
   - **接手**：回條變成 `started`，或收到 `already_queued`。之後由 Responses resilient task 負責執行和重入，B 完成後沿原本的 B→A 結果與 wake 路徑回到 A；follow 不必等 B 整輪完成。
   - **結束**：`completed`、`unknown` 或 `busy_refused`。
   - **等待中**：`not_taken` 或等 CID 名額是正常等待，不扣失敗重試次數。
     - 在原 SDK follow task 內用官方 backoff 等待，輪詢間隔設上限。
     - 不新增 queue 或 runner，也不把 RID 改成終態。
   - **真正的錯誤**：HTTP 或權限錯誤沿用原本的錯誤界線。
   - **共用 helper 契約（79fa 提出、10-02 確認）**：
     - **T5 第一次接單**（source 非空，原 RID 尚不存在）：只建立回條並取得 CID 名額，立刻回 accepted，A 不等 B。第二步由既有 follow 下一次用同一個 RID 呼叫時再送。
     - **原 RID 已存在時**：同一個 helper 送第二步，依結果分兩類處理：
       - 400、404、502、timeout 是明確錯誤，交給既有的有限次重試，扣錯誤預算。
       - 第二步拿到 ACK 後重讀原 N1，若仍是 `accepted`／`needs_response`，也算「尚未確認接手」，同樣扣預算。理由：handler 會先送出 created／in_progress 才驗證，所以永久的前置錯誤也可能每次都回 200。
       - 以上兩種一律回 **503 `NATIVE_RESPONSE_UNCONFIRMED`**（10-02 與 79fa 確認），回條維持 `accepted`，以同一個 RID 有限次 reconcile／follow。
         - **不要**用 `NATIVE_TURN_UNKNOWN`：8765 會把它當成終態，顯示「不會自動重送」（`index.html:1659`、`native.py:671`）。
         - relay 把新碼列入 `uncertain`，計入錯誤預算；8765 對新碼做有限 backoff 自動 reconcile。
         - 401／403 照原分類停止，不重試。Invocations、prepare、conversation-create、history、cancel 也維持原分類。
         - 已知代價：遇到永久前置錯誤時，每次重送都會在 Foundry 歷史留下一筆 failed response，最多到預算上限。handler 有 CAS 保護，不會重跑。
         - **代 holder 補送的例外（79fa 提出）**：呼叫者自己的 RID 是 `not_taken` 或正在等名額時，API 會依「誰負責重送」第 3 點，先替同一個 CID 的 holder 補送第二步。
           - 如果這次補送之後，holder 仍是「尚未確認接手」，就把 503 `NATIVE_RESPONSE_UNCONFIRMED` 傳回給這次的呼叫者，計入呼叫者自己的有限預算。否則跟隨者會一直輪詢，不斷替永久失敗的 holder 建立 response。
           - 純粹等名額或排隊、沒有代送時，仍然不扣預算。
           - 同一個 RID 重述時，只推動同一個 holder 一次。不新增 holder 帳本、queue 或錯誤碼。
         - 一般的啟動競態只是**可能**多扣預算，不保證整段等待只多扣一次。
       - 409 `conversation_locked` 和 `not_taken` 屬於正常等待，不扣預算。
     - **真人第一次送出**：第二步遇到真正的錯誤時回 503，原 accepted 保留。之後可以用原本的 read／reconcile 核對；8765 自動 reconcile 對這類錯誤同樣使用有限 backoff。
     - 不新增 `dispatch_error` 欄位、queue 或 runner。
2. **8765**：輪詢時看到回條維持 `accepted`、還沒有 `response_id`，就自動用既有 reconcile 路徑送同一個 RID。
3. **API**：同一個 CID 的新請求因忙碌被擋時，若 API 發現占用名額的 RID 仍是 `accepted`、沒有 `response_id`，就先替它補送第二步，再回覆新請求忙碌。這樣使用者離線時，只要這個 CID 再有任何動作就會被帶動，不會永遠卡住。
- 使用者也可以直接取消，回條收成 `NATIVE_TURN_CANCELLED` 後就釋出名額。

**handler 的順序**（只寫新增或改變的部分；其餘依 ADR-0007 §1）
1. 先占一個排隊名額。同一個 RID 已經在排隊時，重複的請求不碰回條，只回「已在排隊」。排隊數超過 9 時：
   - 真人回合：把 `rid` CAS 成 `busy_refused/NATIVE_TURN_BUSY`，回應明確失敗。
   - T5 的 `agent_request` 和 wake：**不收終態**，回條維持 `accepted`，回「未接手」，由 relay 用同一個 RID 重送。
2. 等 Agent 層級的 `asyncio.Lock` 時，同時監看 `cancellation_signal` 和 `context.shutdown`，哪個先到就處理哪個。
   - 取消先到（`client_cancelled`，或 FA 的取消標記）：把 `accepted` CAS 成 `unknown/NATIVE_TURN_CANCELLED`，不開始執行。
   - shutdown 先到：什麼都不寫，回條維持 `accepted`，依官方 `exit_for_recovery` 交給同一個 SID 的 recovery 重入。
   - lease 失效先到（非 client、非 shutdown 的 `cancellation_signal`）：什麼都不寫，回條維持 `accepted`。平台會把這個 response 標成 failed，不會重入（見下方「lease 失效」列），所以由「誰負責用同一個 RID 重送」的三方，用同一個 RID 再送一次第二步。
3. 拿到鎖之後，重新讀 `rid` 回條、取消標記和 `meta.inflight_rid`，再 CAS `accepted → started`（`if_match`），同時寫入 `response_id` 和 `run_token`。
   - `meta.inflight_rid` 不是自己時，表示還沒拿到 CID 名額：不執行，回「未接手、可用同一個 RID 重送」。
   - CAS 失敗表示 recovery 或取消已經改了回條，這時依重入表處理，不執行。
   - 之後依 §1 的 meta、checkpoint、`turn:<n>`、`completed` 順序。
4. 執行既有的 `native_sdk` executor（Copilot SDK，使用 `get_input_text()`，不用 `get_history()`）。SDK 事件轉成 Responses SSE 的規則：
   - **commentary（進度）**：只送既有 `public_progress` 的 `tool_started.tool_name`，也就是工具名稱。`phase="commentary"`。
   - **不送模型原始文字的 delta**，也不送 raw 事件、summary、reasoning、工具參數。原因（df3d 在 10-02 核對）：最終答案在保存前還會被處理，原始 delta 會繞過這些處理。
     - `m2_copilot` 會對完整的 `final_message` 再做一次已知秘密遮蔽（`m2_copilot.py:794-801`、約 1272 行）。逐段遮蔽可能漏掉被切開的字串。
     - `native_sdk._run` 用 `turn_text` 把各輪答案和交辦文字（`peer_tools.handoff_texts`）串成保存結果。
     - `native_turns._save_turn` 保存前，會核對 graceful stop、完整 checkpoint、private codec 和 8 MiB 上限。
   - **final_answer**：只取 `turn:<n>` 已保存的原結果（`phase="final_answer"`）。可以用官方的 text delta 事件一次送出，但不拿保存前的文字當預覽。
5. 完成後 emit `response.completed`。輸出文字直接採用 `turn:<n>` 裡的原結果，不另做第二份。

**重入**（`context.is_recovery=True`）：先讀 `rid`，不看 SSE 狀態。

| `rid` 狀態 | 處理 |
|---|---|
| `completed`、`unknown`、`busy_refused` | 先 emit `response.in_progress`，這是官方規定的畫面重置[S47]；再送原結果或原失敗碼 |
| `started`，且同一個 RID 的 `turn:<n>` 和 checkpoint 都通過 `verify_checkpoint` | 補上 `completed`，送出原結果 |
| 其他 `started` | 標成 `unknown`，回應失敗，**不重跑**。依官方規定，「effects are your responsibility」 |
| `accepted`，同一個 session | 照一般流程 CAS 成 `started` 再執行 |
| 不同 SID | 依 #364 處理：只在明示 follow／retarget 時收成 `NATIVE_TURN_NOT_STARTED` |

**取消、shutdown 和忙碌**

| 情況 | 處理 |
|---|---|
| 使用者取消 | API 對 `rid` 中記錄的 `response_id` 呼叫官方 `POST /responses/{id}/cancel`（只適用 background）。handler 收到第三個參數 `cancellation_signal`，且 `client_cancelled=True`，就停止。還在等鎖的 RID，改由既有的 FA `NativeCancel` 標記，handler 拿到鎖後先檢查 |
| shutdown | 看 `context.shutdown`，和取消分開。依官方做法呼叫 `exit_for_recovery` 交給 recovery，不當成使用者取消[S53]。`exit_for_recovery` 只能在 shutdown 時呼叫（core `tasks/_context.py:168` 起） |
| lease 失效（`cancellation_signal` 已觸發，但不是 `client_cancelled`，也不是 shutdown） | 官方 `_bg_resolve_cancelled`（responses `hosting/_orchestrator.py:727-781`）會直接把 response 標成 `failed`，**不會重入**；只有「shutdown＋resilient background」才會保持 `in_progress`。FA 對應成 `fenced`：<br>・回條已是 `started`：停止 SDK，**當場**把 `started` CAS 成 `unknown/NATIVE_TURN_UNKNOWN`，釋出 CID 名額，不重跑。CAS 寫入結果不明時，照實記成「未確認」，不宣稱 unknown 已保存（71c7）。<br>・回條還是 `accepted`：什麼都不寫，由重送三方用同一個 RID 補送第二步；不宣稱平台已自動 recovery。<br>只有 `client_cancelled` 才用 `NATIVE_TURN_CANCELLED`，不把一般的 lease 訊號說成使用者取消。<br>不呼叫 `exit_for_recovery`，不設假的 shutdown，也不開新 task（df3d 在真 host 確認） |
| 同一個對話重疊送出 | 接單步驟已經擋下同一個 CID 的第二個 RID。若仍收到官方 409 `conversation_locked`[S51]，表示 handler 還沒執行，回條維持 `accepted`。API 回「忙碌」，之後在同一個 SID 用同一個 RID 再送第二步；不換 RID。T5 的 wake 也一樣，用同一個確定的 RID 重送 |
| task 或 lease PATCH 回 502 | 換成 Responses **不會**消除這個問題，底層是同一支 `PATCH /tasks/{id}`[S56]。SDK 把 5xx 分類為 transient[S54]。core 設定的 `AsyncRetryPolicy` 列了 502[S56]，但 PATCH 不是冪等方法，實際會不會重試 PATCH，要看安裝的 azure-core 版本和它對方法的限制；**由 N6-0 實際升級後在 #364 回歸中核實，設計不依賴這個重試，也不為此加 retry**。lease 續約連續失敗 3 次後，只停掉本地執行，不把 task 標成終態[S55]。之後由 `rid` 重入規則處理，不重跑 |

## 3. 逐項對照

| 項目 | 結論 | 說明 |
|---|---|---|
| N1 `rid`／`meta`／`turn`／CAS | **沿用** | 和協定無關，是「只執行一次」的權威 |
| N1 Agent chain（`multi_turn_task(task_id=agent)`、enqueue 重試、TaskObserver） | **新 CID 拿掉，既有 Invocations native CID 保留** | 新 CID 改由 Responses task 加上 Agent 鎖處理；舊 CID 照原路徑追問和收尾，不刪除 |
| N3 checkpoint（`native_checkpoint.py`） | **沿用** | Copilot SDK 的 session 仍需要。官方範例也是用 SDK 自己的 session，不用 `get_history()`[S42] |
| `turn:<n>`、`NativeResult` 分頁 | **沿用** | 回條 digest 和 T5 的 result 綁定都靠它；8765 一般顯示不再需要 |
| N2 8765 歷史讀取 | **改由 API MI 讀 Foundry 保存的歷史，不經 VM**。D1 已通過，所以讀 Conversations items | 用量、capture、交付回條、候選、圖片、附件來源仍走 FA 讀取。D3 ① 讀 items 回 200，但當時 session 本來就是 active，所以「讀取不喚醒 VM」仍未證實。**讀取前必須核對 CID 屬於這個 Agent 和 Team**，見下方「歷史讀取的範圍核對」 |
| N2 送出和讀回條 | **改成兩步送出**（第 2 節）；其他讀取照用 Invocations 控制面 | `NativeTurnGateway._invoke` 不變；新增 Responses POST、conversation 建立、cancel |
| 進度 | **短期照舊**輪詢 FA 回條；**之後**改由 API 轉送 `GET /responses/{id}?stream=true` | 串流用戶端每次收到 `response.in_progress` 都要重置累積內容[S47] |
| T5 | **relay 不變，B 端回合改走 Responses** | 見第 5 節 |
| 換版接續、retarget、明示 prepare | **沿用 FA 自管 session**（D1、D2 已通過，第 4 節） | #359、#360、#364 的規則保留；換版後原 CID 用新 RID 接到新版本的 session，不另建 conversation |
| C0～C6 能力接點 | **沿用**；只改 context 的來源 | `NativeCapabilityContext` 改由 Responses request 組出：`receipt`、`scope.sdk_session_id`、`control`（取消、shutdown、FA 取消標記）、`effects`。`native_capability_api` 的 RID 簽名回呼不變，session id 從 `get_request_context()` 取得 |
| effects、私人記憶、候選、用量回報 | **沿用** | 協定無關。用量另補 `cache_write_tokens` |

**歷史讀取的範圍核對（N6-2 必要接點，79fa 提出）**
- **問題**：所有 conversation 和 response 都由同一個 API MI 建立。官方只依呼叫者隔離[S58]，所以平台分不出 Agent 和 Team。官方也沒有保證：透過 Agent X 的 endpoint 讀 Agent Y 的 conversation id 會被拒。
- **最小做法**：
  - API 建立 conversation 時（chain 模式則在每次建立 response 時），寫入 server 固定的 `metadata`：`fa_agent_id`、`fa_team_id`。這兩個欄位不含使用者內容，也不放 RID。
  - 讀歷史前，先 GET 該 conversation 或 response，核對這兩個值等於本次 T1 授權的 Agent 和 Team。不一致時拒絕，回 403 `NATIVE_STATE_SCOPE`。
  - 整個過程不經 VM，也不需要對照表。
- **D3 結果（10-02）**：照原設計使用 metadata 核對（總協調定案）。之前準備的備案，例如改問原 Agent（`NativeConversationRead`）或在 API 為每個 conversation 建一個 key，**都不需要啟用**。
  - ③：從原 endpoint 讀 C1，`fa_agent_id`、`fa_team_id` 都原樣讀回。
  - ②：用另一個 Agent 的 endpoint 讀，回 404 `not_found`，記為「被拒」。這不能當作官方保證或隔離證明，所以 metadata 核對照樣保留。
  - D1b probe 失敗時看到的 `metadata={}`，來自 probe 自己用 `request={}` 產生的失敗回應，不代表 metadata 遺失。
- **已知風險**：有 Foundry User 的 identity 理論上可以改寫 metadata。這和 #306 是同一類例外。D3 ④ 回 unknown，還無法判斷 agent identity 能否讀到；結果出來後再交 Neal 決定（第 12 節第 4 項）。

## 4. Session、版本與 conversation（D1、D2 已通過；平台觀察與啟用分開）

- **現在**：每個 Agent、每個版本一個 Hosted session，由 API MI 明示建立，帶 `version_indicator`。所有 CID 共用（`behavior_native_sessions.native_session_id`、`behavior_foundry.py:202-221`）。這就是 ADR-0006「每個 Agent 一台 VM」。
- **官方**：「a stable `agent_session_id` is automatically associated with the conversation」[S29]。
  - conversation 和 session 是兩件事。
  - 換 agent version 之後，conversation 還能不能接續、`$HOME` 會不會保留，官方都**沒有寫明**[S23][S29]。
- **已有官方依據的部分**：
  - 官方明示 session 範例：`extra_body={"agent_session_id": ...}` 搭配 `previous_response_id` 串回合，可以讓多個回合鏈共用同一個 session[S58]。
  - 平台會依呼叫者隔離 response chain 和 `get_history()`。FA 的回合一律由 API MI 送出，因此不受這個隔離影響。
- **D1（N6-0 實測）**：明示 `agent_session_id` 改成搭配 `conversation=X` 時，平台是否仍路由到 FA 指定的 session？官方只寫了「沒有明示時，平台會自動配一個」。
  - **結果：會（10-02 通過）。**採用「會」的路線，以下「不會」的分支保留作紀錄，不實作。
  - **會**：用 `conversation`，維持每個 Agent 一台 VM，Agent 鎖有效，8765 可以直接讀 Conversations。換版接續沿用 FA 自管 session（#359／#360／#364 不變）。
  - **不會**：預設改走 **(a)**：新 CID 用 `previous_response_id` 串回合，並明示 session（官方範例的做法）。
    - 8765 讀歷史要改成 API 逐段讀 response chain（`GET /responses/{id}/input_items`），「不喚醒 VM」要另外實測。
    - 只有 Neal 改選 **(b)**「接受每個對話一個 sandbox」時，才會改變 ADR-0006 §2：桌面和私人檔案不再跨 CID 共用。
- **D2（N6-0 實測）**：換版後，舊 conversation 送出新回合會落在哪個版本或 session？結果決定 follow／retarget 是照舊，還是要先建新 conversation。
  - **結果（10-02 通過）**：在原 C1 用新 RID 送出，落在新版本 v20 和新 session S2，回合 completed。所以 follow／retarget 照舊：原 CID 保留，只有合法的新 RID 在新 session 執行，不另建 conversation、不搬歷史。這和 79fa N6-5 測試用的 retain-CID 模式一致。
  - 正式打開 Responses 模式（目前 `None`）仍由總協調決定，並排同一個 build。D4 已於 16:40 在 probe 範圍通過（第 9 節）。

**10-02後續觀察：** 部署線回報D1d兩CID在明示同SID完成、D2d原C1以新RID在明示新版本／SID完成。
這兩點已在該探針成立，不再列為未觀察；不是舊RID搬移或checkpoint完整恢復（D4另於16:40在probe範圍通過，是合成probe的重入，不是真native模型回合；見第9節）。
[分批結果與限制](../src/fa-shared-context-poc/README.md#n6-2n6-5-平台前置分批結果2026-10-02)
保留舊失敗及新的D3結果，正式mode仍None，啟用另行核准。

**N6-5 的預設關閉交付（總協調10-02 15:56、測試範圍16:00確認）**
- 不等D結果即可交付本機實作；正式啟用仍等D1／D2。判斷集中於單一
  `responses_environment_blocker()`，目前mode=None明拒Responses跨SID接續。
- `retain-cid`候選保原CID／capture／最後response_id與checkpoint；只新RID在新SID執行，
  不搬歷史、不建立隱式conversation對照。平台結果不符時另窄修策略，不假裝舊CID已改來源。
- 原Agent先pending阻止新接單。明示follow／retarget才以原ETag將來源SID的未開始Responses回條
  收成`unknown/NATIVE_TURN_NOT_STARTED`；started或CAS先贏者保持。沿原通知及CID名額接手機制，
  不清空meta、不拿查不到task當未執行證據。
- 來源SID的兩種官方Responses task及原三種FA輔助task都須排空，仍沿原stop／inactive／最後CAS發布ready。
  缺少Responses掃描標記的舊進度須重掃，不可當作false；Invocations舊lane規則不改。
- 不新設runner／queue／lease管理；公開owner API／N1控制面測follow、retarget、晚到原RID與checkpoint恢復，
  只有外部HTTP與模型替身。[接點與本機證據](../src/fa-shared-context-poc/README.md#n6-5-responses-換版與-retargetlocal預設關閉)
  不代替Hosted實測。

## 5. T5 和官方 A2A

- 官方要求收件端使用 Responses（「Incoming A2A requires the responses protocol」）[S30]。N6 之後技術上可以開。
- **FA Agent 之間的互傳仍走 relay**，理由都是官方缺口：
  - 容器協定 2.0.0 會在每次請求注入平台驗證過的 `x-agent-user-id`[S24]，所以 B 可以確認「同一個呼叫者」。
    - 但這個值不透明（官方原文是「Treat their values as opaque」），對不到 Entra oid，也對不到 FA 的 Agent 或 Team。
    - 因此 B 仍無法核對 sender 和 Team。ADR-0007 §5「讀不到已驗證身分」的寫法要在 ADR 更新時一併修正。
  - 沒有 hop 上限或迴圈防護，逐字查無[S30][S31]。
  - A2A 的 task／message 對到 Responses 的方式和去重規則，官方沒有寫明。
  - v1.0 只支援 JSON-RPC 和文字，不支援 streaming[S30]。
- **改的部分**：B 收到的 `agent_request` 回合，以及回給 A 的 wake 回合，都改成「Invocations 接單＋Responses 執行」。root 限額 10、message id 去重、`in_reply_to` 和 sender 綁定都不變。
- **N6-6（對外開放 incoming A2A）：不做**（Neal 2026-10-02 15:08，#396，原文：「如果是FA Agent間互傳，就做N6-6。如果是要跟FA外的Agent互傳，就先不做 N6-6。」）
  - N6-6 的定義是讓外部系統呼叫 FA，屬於後者，所以先不做。
  - 不寫 agent card，不 PATCH a2a protocol，不新增 Foundry Agent Consumer。
  - FA Agent 之間的互傳照上方規則走 T5 relay，由 N6-4 改成 Responses。
  - 原本評估的內容留作日後參考：
    - 需要用 `PATCH /agents/{name}` 設定 agent card 和 a2a protocol[S30]。
    - 外部呼叫者需要 Foundry Agent Consumer 這項新 IAM。
    - 開放後，有 Foundry User 的 identity 可能繞過 FA 的 Team 檢查，屬於 #306 例外的延伸。

## 6. 舊資料

- 舊 Table lane 的 CID 和 B23：原樣保留，照原路徑讀和追問，不搬也不刪。
- Invocations native 的 CID（test agent 2／3／4，以及過渡期間 A／B 新建的 CID）：
  - 多協定 host 保留整條 Invocations native 路徑，原 CID 照常追問。
  - 原本的 unknown、`NATIVE_TURN_NOT_STARTED`、13cc／d8d2 的原件都不改。
- 新 CID：只有模板旗標選了新的 `state_profile`（建議 `foundry-responses-v1`）並建立 Foundry conversation 的對話，才走 Responses。

**新 lane 的開啟條件（71c7 提出，N6-2 選 lane 必需）**
- **開啟條件**：Agent 要能接受 `state_profile="foundry-responses-v1"` 的新 CID，API 選定的同 build active version 定義必須同時符合四點：
  1. `protocol_versions` 含 `responses 2.0.0`。只宣告協定不等於 handler 已經開啟。
  2. 環境變數 `FA_RESPONSES_ENABLED=1`。這是正式旗標，N6-0 的 `create_host` 只在這個旗標下註冊 Responses handler。
  3. 環境變數 `FA_NATIVE_TURNS=foundry-native-v1`（df3d 補充）。Responses lane 沿用同一個 N1 `NativeTurnRuntime`，接單、回條、checkpoint 和 effects 都由它負責。
     - 只開 Responses 旗標、沒有 native runtime 時，handler 明確拒絕，回 `NATIVE_TURNS_DISABLED`。
     - 不會偷偷開啟舊 lane，也不會另建第二個 runtime。
  4. 不是靠 probe 旗標開的。probe 只用於 N6-0 實測，**永遠不會**由 sync 或 onboarding 傳播，也不能當成開啟。
- **不符合時**：API 對新 CID 明確拒絕，沿用 N2 現有的拒絕碼，不新增錯誤碼；不會自動改走 Invocations native。8765 只在 API 判定可用時才顯示這個模板。
- **旗標怎麼傳播**：
  - 原N6-0預設關閉階段：sync 和「＋」clone 移除 `FA_RESPONSES_ENABLED`；test agent實測由部署線手動設定。原批次保持。
  - 總協調核准正式採用後：sync／onboarding保留該正式旗標的原值，未設不補1、0不改1；其餘probe keys仍移除。程式mode採`conversation`／`retain-cid`，不是ENV。
  - 第一個真新build版本建立前，部署desired definition須放好雙flags，A source與configured B逐一核對；image_source不代填ENV，不能先建off版再追同build高版。能力備齊後先只選test2新對話模板，驗完才A/B；不改最小active/cache或舊CID。
- 舊 CID 不受影響。

## 7. A／B 什麼時候切

**建議：#364 在 test agent 上 live 讀回正常後，先把 A／B 切到 Invocations native 當過渡。**

不會重工的原因：
- Responses lane 沿用同一套 `rid`、checkpoint、effects 和 C0～C6。
- 多協定 host 保留 Invocations native，A／B 在過渡期建立的 CID 之後仍可追問，不需要搬資料。
- 唯一的成本是過渡期的 CID 不會有 Foundry 代管歷史和 A2A；使用者開新對話時就會改走 Responses。

先切的好處：
- A／B 可以提早離開 Table lane，並提早用上 T5 和 native 能力。
- N6 還有 D1～D4 要先實測，時間不確定。

替代方案：A／B 留在 Table lane 等 N6 完成，大約多等 1～2 週。好處是 A／B 只會有一種 native lane。

由總協調決定。**結果：Neal 2026-10-02 08:18 決定「可以馬上切換」到 Invocations native，由 fdbb 在 `75696b6f` 這批處理。**

## 8. 切片、依賴、owner、工作量

工作量以「單一 owner 人日」估算，不是並行後的日曆時間。

| 切片 | 內容 | 建議 owner | 依賴 | 人日 |
|---|---|---|---|---|
| **N6-0** | 三套件一起升級；多協定 host 骨架，Responses handler 由旗標控制、預設關閉；`protocol_versions` 加 responses；回歸測試（Invocations native、#364、N0 probe）；實測腳本 | 71c7（固定 SDK、host）；fdbb 部署和實測 | 總協調批准部署 | 1.5 |
| **N6-1a** | VM 端 Responses handler：兩步接單的第二步、Agent 鎖、重入、取消對應、SSE、`response_id` 寫進 `rid` | 71c7（N1 owner） | N6-0。**D1～D4 不阻擋合併**：handler 預設關閉、旗標不傳播，對 `conversation`／`previous_response_id` 兩種模式都適用，SID 不同或 CID 不合法時明確拒絕。D 的結果只決定 N6-2 的路由、CID 選擇與正式開啟時機（10-02 與 71c7 確認） | 2.5～3 |
| **N6-1b** | `NativeCapabilityContext` 改由 Responses 組出；用量的 `cache_write_tokens`；私人記憶和候選接點回歸 | 83d4 | N6-1a 的介面 | 1 |
| **N6-2** | API：新 CID 建立 Foundry conversation、兩步送出、cancel 轉官方、讀 Conversations 歷史的轉送 | 79fa（N2、session 線 owner） | N6-1a 的介面。**D1～D4 不阻擋合併**：routing mode 預設 None，雙旗標 gate 會拒絕，新 route 也拒絕其他 lane 的 CID；歷史範圍核對收在 `check_history_scope` 一個函式裡。D1 決定 routing mode，D3 決定 scope 的實作方式，結果只影響上述函式或常數（10-02 確認） | 2～3 |
| **N6-3** | 8765：新模板旗標、歷史改讀 Conversations、進度照舊輪詢 | fcd（native 8765） | N6-2 | 1～1.5 |
| **N6-4** | T5：B 的回合和 A 的 wake 改走兩步送出；`conversation_locked` 時用同一個 RID 重送 | fcd（T5 owner） | N6-1a、N6-2 | 1～1.5 |
| **N6-5** | 換版接續和 retarget 在 Responses 下的處理 | 79fa（#359／#360／#364） | N6-1a／N6-2正式介面；**D1～D4不阻擋預設關閉合併**（總協調10-02 15:56）。D1／D2判斷集中單一policy，結果決定正式啟用或窄修。 | 1～2 |
| **N6-6** | 對外開放 incoming A2A | — | **不做**（Neal 10-02 15:08，#396 已關閉；見第 5 節） | — |
| 文件 | ADR-0007 §4、§7、§9 改寫；AGENTS 第 3 條；README | 83d4（docs-only，不跑 Opus） | 本設計核准 | 0.5 |

- 合計：主體約 **10.5～14 人日**。N6-6 依 Neal 決定不做。
- 1b60 正在做 T6b，c7bb 正在做 T4b，兩人主體都不安排，只在必要時審查介面。
- 順序：N6-0 → N6-1a（預設關閉，可以先合併）→ N6-1b、N6-2 並行 → N6-3、N6-4、N6-5 並行。D1、D2 已通過，N6-2 路由走 `conversation`、新 CID 直接用 conversation id（D4 確認格式相符）；正式開啟由總協調排同一個 build，D3 ④ 待下一批。
- 每片照兩軸只看 P1／P2；GitHub 顯示 CLEAN 就合併。B 清單和 live 驗收交 C6／fdbb 的流程。

## 9. N6-0 live 實測（要總協調批准）

範圍：
- 限 test agent，照 N0 的條件：正常 main build，API 和 Hosted 同版，不動 A／B。
- 不新增資源或 IAM。遇到需要新角色時就停下來，回報給 Neal。

| # | 要確認的事 | 判讀 |
|---|---|---|
| D1 | 同時帶 `conversation` 和明示 `agent_session_id`，是否落在指定 session（看 Hosted log 的 session id） | 決定第 4 節的走向 |
| D2 | 換版後，同一個 conversation 再送回合會落在哪個版本或 session | 決定 N6-5 |
| D3 | 以下各操作都只做一次唯讀：<br>① API MI 以既有角色讀 Conversations items 是否回 200，讀取時 Hosted 是否被喚醒（看 log）。<br>② 經 Agent X 的 endpoint 讀 Agent Y 的 conversation id 是否被拒。<br>③ conversation 的 `metadata` 能否原樣讀回。<br>④ 改用一個 agent identity 讀同一個 conversation 是否被拒。<br>**④ 的判讀變數（71c7 提醒）**：官方要求對外呼叫 Foundry 服務時原樣轉送 `x-agent-foundry-call-id`，平台會依它解析呼叫者[S24]。官方列出的是 Storage、Toolbox 和其他 agent，Conversations 是否同樣適用，沒有查到明文。因此 ④ 要記錄這次有沒有帶 call id：<br>　・帶了：結果只能寫成「Agent bearer＋原呼叫者 context」，不能說成「純 agent identity 可以跨讀」或「角色不足」。這也是被注入的 Agent 實際會走的路徑，官方 SDK 會自動轉送 call id。<br>　・不要為了測試刻意拿掉必要的 header。<br>　・只輸出 present 和 identity 種類，不輸出不透明的值。 | 決定 N6-2 是否需要新 IAM、歷史讀取的範圍核對要用 metadata 還是官方隔離，以及第 12 節第 4 項 |
| D4 | `x-client-fa-*` header 是否原樣傳到 handler；Foundry conversation id 的長度和格式是否符合 FA 的 `Identifier`（`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`）；中途結束 process 後，`is_recovery` 重入是否只讀 `rid`、不重跑 | 決定 RID 和 CID 的對應方式。符合格式時，新 CID 直接使用 conversation id，不另存對照表；並確認重入規則 |

- 任何一步結果不明，都保留原紀錄，不重送也不換 RID。

**結果（10-02，main `21231bcd`，同一個 build，只在 test agent 1，全程 API MI；fdbb 執行，原件在 fdbb `files\i-p1\b1c\n6-d1d-*`、`n6-d2d-*`、`n6-d3d-01…08`、`n6-d4d-*`）**

| # | 結果 | 判讀 |
|---|---|---|
| D1 | **通過**。S1d 由 API MI 建立（201）；C1、C2 都 completed，ProbeRecord 各 effect=1；兩個 conversation 都落在同一個 SID 和版本 v19。`cid_fa_identifier=true`、handler 端 `call_id_present=true`、`session_guid_present=false`；422 沒有再出現 | 第 4 節採「會」：新 CID 用 `conversation`。GUID 沒觀察到只代表 probe 沒看到，不能推出平台沒有 incarnation，也不據此新增檢查 |
| D2 | **通過**。換到 v20 後，在原 C1 用新 RID 送出，observed 為 v20／S2，回合 completed | follow／retarget 照舊（第 3、4 節），N6-5 採 retain-CID |
| D3 | ① API MI 讀 C1 items 回 200，但 session 本來就是 active，只能記「GET 成功」。② 用另一個 Agent 的 endpoint 讀，回 404 `not_found`，記為「被拒」，不推論成隔離，也不是停止條件（總協調裁定）。③ 原 endpoint 讀 C1，metadata 的 `fa_agent_id`、`fa_team_id` 原樣讀回。④ `agent-read` 回 503 `N6_PROBE_READ_UNKNOWN`，已停。這個碼來自共用的例外處理，涵蓋讀原 RID、取 MI token 和 HTTP／OS 錯誤，所以連請求是否到達 Agent MI 的 items GET 都不能確定；也沒有 host 端的 call id 觀察（頂層的 `call_id_present=false` 是 sender 預填，不是 host 測得）。915 正在補最小的 reason／status 診斷 | 歷史讀取照原設計用 metadata 核對，不啟用備案（第 3 節）。④ 不能判定為 IAM 不足，也不放寬授權；915 補診斷後下一批再跑 |
| D4 | **通過**（test agent 1 v21，沒有 native 旗標，帶中斷 SID）。hold 回 200，receipt-read 讀到 `phase=hold`、`process_id=7`；`interrupt` 只送一次（202）。新 process 起來後，SDK 重放時 `storage/responses` 回 409，`FA_N6_PROBE` 帶 `is_recovery=true`；原 response completed，`state_write_performed=false`、`synthetic_effect_count=1`，中斷前後原紀錄逐字相同，SID 不變。四筆 RID 的 `x-client-fa-*` header 都原樣到達、digest 重算一致；Foundry CID 55 字元，符合 `Identifier` | 在 probe 範圍內，重入只讀原紀錄、不重跑；這是合成 probe 的重入，不等於真 native 模型回合已驗證。新 CID 直接用 conversation id，不另存對照表 |

- **同一請求可能送達不只一次（D3 ④ 觀察到）**：sender 只送一次外層 POST，但 Hosted 端同一個關聯 ID 的 `POST /invocations` 503 入口紀錄有 3 次。目前不確定是哪一層重送的，由 915 依原件釐清。這次是唯讀，沒有副作用。
  - 設計上已經靠同一個 RID、原 ETag／CAS 和讀回保護（第 2 節），不依賴「client 不重試就只會送達一次」。
  - 不因此改成換 RID 重送，也不把 503 直接當成沒有執行。
  - 不假設閘道固定重送幾次，也不另外加框架或 gate。

## 10. IAM 和 Azure 資源

- N6-0～N6-5：預期**不新增**。
  - API MI 已有 Foundry User 和 Cognitive Services OpenAI User（README:9581）。
  - Agent identity 已有 #306 的兩個角色。
  - D3 實測若出現 403，就停下來交 Neal 決定。
- 新的資料面風險：
  - 對話內容改存在 Foundry Conversations 後，官方說平台會依呼叫者隔離 response chain 和 `get_history()`[S58]，而 FA 的回合全由 API MI 建立。
  - 但「其他有 Foundry User 的 identity，例如任一 agent identity，能不能透過 Conversations API 讀到 API MI 建立的 conversation」，官方沒有寫明。列入 D3 一起實測。
  - 讀得到的話，和 #306 屬於同一類例外，要記錄在 ADR-0006，並請 Neal 確認。
- N6-6：依 Neal 決定不做，所以**不新增** Foundry Agent Consumer，也不寫 agent card。

## 11. 官方沒有寫明的地方（要實測，不當成保證）

1. conversation 和 agent version 的關係；換版後能否接續（D2）。**D2 已觀察到：原 conversation 用新 RID 會接到新版本的 session**（單次觀察）。
2. 讀 Conversations 是否會喚醒 sandbox；最小需要哪個 RBAC 角色（D3）。D3 ① 有 200，但當時 session 是 active，所以「不喚醒」仍未證實。
3. conversation 本身的保留期限。目前只查到 session 30 天、A2A task 60 天。
4. A2A message 怎麼對到 Responses、怎麼去重；有沒有 hop 上限。
5. `$HOME` 換版後是否保留。
6. 官方沒有「Invocations 改 Responses」的逐步遷移文件；現有官方範例 pin 的是 responses 1.0.0b9，比 2.2.0 舊[S43]。
7. **Foundry Conversations 是否原樣保留 message 的 `phase`（`commentary`／`final_answer`）**：
   - 已驗的範圍：N6-1a 走公開的 generic item 寫入 phase，在 SSE、completed snapshot 和本機 GET 都會保留。本機 GET 不能當作平台的證據。
   - 讀取端的規則（N6-2、N6-3）：讀不到 phase 時，改用已知 RID 的 FA `NativeResult` 原結果；沒有可核對的原 RID，就明示「讀取未確認」。不要把所有 `output_text` 串起來，也不要自建 response→RID 對照。
   - 驗證時機：不加進 N6-0 的 D 項目。等 1a 部署到 test agent 的第一個真回合時讀一次，照實記錄；不額外加 GET。

## 12. 要決定的事

| # | 誰決定 | 內容 |
|---|---|---|
| 1 | 總協調 | 核准本設計；批准 N6-0 部署和 D1～D4 實測。**已核准（08:57）** |
| 2 | 總協調 | A／B 是否先切 Invocations native 當過渡（第 7 節）。**Neal 08:18 已決定切換** |
| 3 | Neal（只在 D1 為「不會」、而且想改選 (b) 時） | 預設 (a)：依官方範例明示 session，用 `previous_response_id` 串回合；改選 (b) 則是每個對話一個 sandbox，會改變 ADR-0006 §2。**D1 為「會」，不需要決定** |
| 4 | Neal（只在 D3 實測讀得到時） | 其他 agent identity 若能讀到 API MI 建立的 conversation，是否記為 #306 例外的延伸。**D3 ④ 回 unknown，待下一批** |
| 5 | Neal | N6-6 對外 A2A。**已決定不做**（10-02 15:08，#396） |

## 官方依據

編號沿用 [n6-responses-official-research.md](n6-responses-official-research.md) 的來源清單。全部於 2026-10-02 查閱；SDK 原始碼一律以 release tag 為準。

- [S1] https://pypi.org/pypi/azure-ai-agentserver-responses/json
- [S4] https://pypi.org/pypi/azure-ai-agentserver-invocations/1.1.0/json
- [S10] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/CHANGELOG.md
- [S11] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/CHANGELOG.md
- [S12] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-invocations_1.2.0/sdk/agentserver/azure-ai-agentserver-invocations/CHANGELOG.md
- [S15] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/handler-implementation-guide.md （Multi-Protocol Composition，約 228-238 行）
- [S16] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/resilient-responses-developer-guide.md
- [S23] https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- [S24] https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract
- [S26] https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience
- [S27] https://learn.microsoft.com/azure/foundry/agents/how-to/recover-long-running-work
- [S28] https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-hosted-agent-preview
- [S29] https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions
- [S30] https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
- [S31] https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent
- [S32] https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions
- [S37] https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/conversations
- [S42] https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/voicelive/github-copilot-responses-voicelive （Copilot SDK＋Responses 官方範例；`_session_lock` 只保護建立 session，回合仍共用同一個 SDK session）
- [S43] 同上範例的 `requirements.txt`（pin 為 responses 1.0.0b9、core 2.1.0、github-copilot-sdk 1.0.11）
- [S47] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/resilience-contract.md （dispatch matrix；Streaming sub-contract「reset its accumulator」）
- [S48] 同 tag：`azure/ai/agentserver/responses/hosting/_task_id.py`
- [S49] 同 tag：`azure/ai/agentserver/responses/hosting/_chain_id.py`
- [S50] 同 tag：`azure/ai/agentserver/responses/hosting/_resilient_orchestrator.py`（約 603-607 行 `_pick_primitive`：沒有 `conversation_id` 而且非 steerable 時，使用 one-shot task）
- [S51] 同 tag：`azure/ai/agentserver/responses/hosting/_endpoint_handler.py`（約 987-1024 行：`conversation_fork_not_supported`、`conversation_locked`）
- [S53] 同 tag：`azure/ai/agentserver/responses/_response_context.py`
- [S54] https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/docs/task-and-streaming-spec.md （§28 HTTP 錯誤分類矩陣）
- [S55] 同 core tag：`azure/ai/agentserver/core/tasks/_lease.py`
- [S56] 同 core tag：`azure/ai/agentserver/core/tasks/_client.py`（`AsyncRetryPolicy`、`PATCH /tasks/{id}`）
- [S58] https://learn.microsoft.com/azure/foundry/agents/how-to/multiplex-session-users （以 OpenAI SDK 的 `extra_body={"agent_session_id": ...}` 明示 session，wire 上是 body 最上層的 `agent_session_id`；用 `previous_response_id` 串接回合；平台依呼叫者隔離 response chain 和 `get_history()`）
- [S24] 補充：同一頁「Platform request headers (container protocol 2.0.0)」說明 `x-agent-user-id`「trustworthy… Treat their values as opaque」；「Forward custom request headers」說明只轉送 `x-client-*`。
