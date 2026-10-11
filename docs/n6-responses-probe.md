# N6-0：test Agent 的 D1～D4 操作

**本次 D3④傳輸相容小修為 CODE/offline，待正常 main 同 build 部署。** 只明示零重試HTTP transport，
保留TLS驗證、禁止環境proxy與原停止語意；[範圍](#d3④傳輸相容codeoffline)不代表ConnectError根因已確認。

**以下保留 #409 安全診斷交付時的結果。** 部署線回報 `21231bcd` 的
D1／D2／D4 通過；D3①③ GET 200、②跨 Agent endpoint 404，④停在 `503 N6_PROBE_READ_UNKNOWN`。
這些是部署線的獨立新批次，不改舊三批原件；本片不重跑 live，也不宣稱④的例外根因已修。

**以下保留 #395 前的 D1／D1b／D1c 原批次。** 首輪 D1 在 2026-10-02 11:34（台灣時間）
以 main `56b0bb9a`／test Agent v15 走到 response-create 403。#387 腳本的新獨立 D1b 已改用 API MI：
12:35 POST 200／in_progress 後進入 probe，log 為 `N6_PROBE_REQUEST_INVALID status=422`，
12:46 原 response 讀回 failed；尚不能分辨哪個欄位不符。第二 conversation、D2～D4 都未跑。
後續 D1c 在 main `9368bb59`／test v17、API MI 新SID回201後，14:06得到
`field=request_keys reason=unknown_fields`；原response failed，第二CID、D2～D4仍未跑。
依[核准設計 §9](n6-responses-migration-design.md#9-n6-0-live-實測要總協調批准)，只由 fdbb 執行；
不新增 Azure 資源／IAM，不對 A／B 開 Responses 或跑 D1～D4；舊 CID／RID、儲存共用方式、UID 與秘密不變。
「不動 A／B」不禁止正常升 build：合併後由 fdbb 同步更新 API、所有 Hosted（含 A／B）與 8765，
沿 #369 follow runbook 接續原工作；A／B 各一個既有 native／legacy 回合是原批准部署讀回，不是新 gate。
N6-1a 正式回合、兩步接單、Agent 鎖及 API／UI 仍是後片。
正式 N6-1a 需 `FA_NATIVE_TURNS=foundry-native-v1`＋`FA_RESPONSES_ENABLED=1`，
缺原 native runtime 時由正式 factory 明拒；本片 probe／D4 不注入它，D 通過也不是正式雙旗標驗收。

## D1c：標準與未知 unused 欄位（14:16 最新決定）

總協調 2026-10-02 14:16 明確取代先前的整體8-key拒絕與「nonempty一律拒絕」建議：
固定合成input、background/store/stream、metadata、conversation、header/digest與身分原核對不變。
其餘欄位不作FA執行、權限或內容輸入，**標準與未知都接受並忽略**；不再因多出鍵或unused model值回422。
SDK仍先做它自己的公開request語法驗證，FA不另造一套OpenAI schema，也不承諾繞過SDK拒絕。

- 標準鍵來源直接讀固定2.2的公開 `CreateResponse.__annotations__`，只區分診斷，不是FA拒絕白名單；
  `agent_session_id` 是沿用的路由擴充。沒有手抄另一份33鍵集合，更沒混入ResponseObject輸出欄位。
- 真正不在該集合的頂層鍵，排序後只記最多20個名稱：
  `FA_N6_PROBE_IGNORED unknown_keys=[...]`。只序列化鍵名列表，不repr request/context/mapping，不記任何值。
- 忽略接在**只限probe mode、只限POST `/responses`的公開ASGI middleware**：
  固定2.2在handler前就會讀取額外body `response_id`，故需先記未知鍵名並從交SDK的body副本移除未知值，
  才能保證它不間接變成FA record/log/output的ID。原platform headers與`agent_session_id`保留，
  33個標準欄原樣交SDK；SDK仍選原header或自己的fallback ID，不改private resolver/context。
  原JSON錯誤／disconnect訊息交回SDK，正式1a／Invocations／flag-off路徑不套此probe過濾。
- `tools`／`instructions` 非空也不執行、不載入、不送模型，只記固定欄名與 `nonempty=true`。
  `user` 不作caller，`agent_reference`不作FA權限或身份來源，其他unused欄位同樣忽略。
- FA回應只以已核metadata／CID、固定background、固定`model=n6-probe`及受控Agent名稱／已核版本建seed，
  不把原request或ignored值傳入response builder、record、工具或模型接點。失敗seed也不回填未核身分值。
  **這只說FA不使用／不echo；不宣稱SDK或平台沒有解析／保存原request。**
- 原不符input/flags/metadata/CID/header/digest/identity仍422帶窄field/reason、沒有probe record；
  duplicate RID保原record與一次效果，accepted unknown keys不等於另一份工作的授權。

D1c只能縮到原keys檢查：probe標頭、合成literal input、background/store=true、stream=false與原model檢查先過；
CID/digest/metadata/identity在keys之後，當時未驗到。**不知道那次live到底多了哪些鍵**，不猜是tools或agent_reference。
本片移除已定位的過窄keys阻擋，真正D1／第二CID／D2～D4仍需部署線新批次觀察。
原三組SID/CID/RID/response與原FAIL全保，正常main同build後才由總協調安排新獨立D1，不重送原件。

## D1b 422 的證據與診斷界線（保留歷史；unused 規則以上節取代）

- 原件確認 endpoint Responses 明開、API MI 建 SID 201／v15、session GET／conversation POST 成功，
  隨後請求到達 probe 並被本機檢查拒絕；不是再一次「未進 container」的403。
- 原件沒有 handler 當下的 request/context；sender intent 不能替每個內層欄位背書。
  **failed response.metadata={} 不能推出 request.metadata={}**：原 probe 拒絕時就用 `request={}` 建回應，
  SDK 的失敗回應也不是 request echo。Conversation 資源 metadata 是另一個物件，核對規則不變。
- 此診斷只拆分原本同義的拒絕條件，在既有 log 追加固定 enum，**沒有放寬接受條件、補空 metadata 或改原錯誤碼**：

  ```text
  FA_N6_PROBE_FAILED code=N6_PROBE_REQUEST_INVALID status=422 field=<固定欄位> reason=<固定原因>
  ```

  當時`field`含 `probe_header/input/background/store/stream/model/request_keys/cid/digest/identity/metadata/record`；
  `reason` 只取程式內固定原因，不帶值、未知鍵名、完整request、call ID或私人內容。
  通過真 SDK 的 conversation 字串／`{id}` 既有表示仍接受；其他形狀仍照原契約，不猜平台轉換。
- FA request 檢查失敗發生在 `record_once` 之前，不會進入 probe store/CAS或模型；
  但平台已保存 response/task，不能宣稱全平台沒有副作用。SDK也可能在 handler 前先拒絕不合法flags，
  這種情況沒有 FA field/reason，不硬改成 FA 422。
- **這次動到 `shared_context/responses_probe.py`，會改 code_sha，必須正常 main 同 build 更新 API／所有 Hosted／8765。**
  不沿用 #387「只換script、免image」的方式，不在 running image 補檔或 shadowpatch。
  由總協調安排，已有native工作沿#369，Responses仍只test Agent 1；新獨立D1另留原件，舊兩組SID/CID/RID/response全保。
  新reason出來前只稱診斷能力補齊，不稱根因已修或D1通過。

## 固定入口與執行前提

- 腳本：[scripts/n6-responses-probe.py](../scripts/n6-responses-probe.py)。部署線沿既有
  `b1c_exec.py cap02s` 進 API container，核 SHA 後放 `/tmp`，`PYTHONPATH=/app`。
  `cap02s` 若會固定附加本腳本不接受的 `--config`，沿部署線已建立的 n6 模式，不改 API 設定。
  只用 `load_configuration().identity.managed_identity_client_id` 建立 `ManagedIdentityCredential`；
  不用 operator token／DefaultAzureCredential／az login，也不輸出 config。
- API／Hosted 必須是**同一個正常 main build**；`--expected-build` 是 API 的
  `shared_context.behavior_code_identity.code_sha256()`／Foundry `metadata.fa_build`，不是 Git SHA。
  腳本在本機先比對 API build；每個寫入先純 GET 固定 test Agent 物件，核對
  `agent_endpoint.protocol_configuration.responses={}`。明列 Invocations-only、disabled 或未知 shape 就停，
  不 PATCH。官方「預設 Responses enabled」不能拿來猜實際 GET 的缺欄、null、空設定或未知欄位。
  每個寫入／probe invocation 前另讀回指定 test version 的 active、
  GUID、build 和旗標，不改 `version_selector`。不是同 build 就停。
- target 固定沿 N0：GUID `00000000-0000-0000-0000-00000000004f`、
  name `fa-agent-cc96a24795a872009197`、project 見 `responses_probe.PROBE_PROJECT_ENDPOINT`。
  不接受 URL／project／write-target 參數。D3 的 `--reader-agent-name` 只接受部署線已核准的
  另一個既有 test Agent 名稱，且只可搭配 conversation／items GET。
- test version 明設 `FA_RESPONSES_ENABLED=1`、
  `FA_N6_RESPONSES_PROBE=fa-foundry-responses-n6-v1`。一般版本兩者不設。
  `FA_N6_RESPONSES_INTERRUPT_SESSION_ID=<專用SID>` 只供 D4，並移除該 version 的 `FA_NATIVE_TURNS`；
  此 SID 不能有其他工作，且程式拒絕其 native／legacy／桌面 WS。
- 官方 replay root 由真 host 固定接到原 `runtime.supervisor.journal.root/agentserver`，在 SDK constructor
  前設定 `AGENTSERVER_STATE_ROOT`；不用部署線任意指定可寫路徑，不 chown／修改 HOME 或 UID。
- **session、conversation 與 Responses 都由同一個 API container 原 API MI 建立／呼叫**；
  不用本機 operator／AzureCliCredential 建 D 用 session，也不加 `x-ms-user-identity` 或新 IAM。
  `session-create` 沿既有零重試 httpx transport，使用固定 Projects `2.3.0`
  `create_session(agent_name, agent_session_id=<固定SID>, version_indicator=VersionRefIndicator(agent_version=<版>))`
  的官方 REST 路徑與 payload；只 POST 一次，201 必須回原 SID／指定版本。未知或 409 都停，不採用猜測結果。
  **不呼叫 FA prepare／follow／retarget、不發布 native binding、不寫原 native RID／effects**；
  不建／停 Agent 或 version。D2 不為觀察 GUID 額外建 session。

所有操作各送一次，沒有 retry／redirect。先指定新的 `--output` JSONL：第一行 intent 含完整命令、
固定 ID、API MI credential kind 和 script SHA，flush／fsync 後才送出；第二行只保存白名單 ID／hash／status／code／版本。
非 2xx 另保存安全 `error.code`、先遮掉 token／cookie／call ID／query 再截到 300 字元的 `error.message`，
及原 `apim-request-id`／`x-request-id`；不存整個 body、headers 或 config。讀不到可用 JSON error 就只留 hash／status。
檔案已存在就拒絕，不覆寫失敗。`N6_OUTCOME_UNKNOWN` 或非預期結果：**停在原 RID，不重送、不換 RID**。
所有 CID 操作都須帶成功的 `conversation-create` 原檔；不接受舊 CID 或建立結果不明的檔案。
`response-create` 另須成功的 API MI `session-create` 原檔 `--session-record`，
SID、建立時的版本／build 與本次命令必須相符；只讀 session 的 200 不足以代替建立者證據。

## 命令形狀

以下在 **API container** 執行；先把尖括號換成這輪事先固定、已核對的值。每一行用自己的新 output 檔，
不要把範例 RID 重複用在下一輪。RID 格式為 `n6-` 加 32 位小寫 hex；認證不放 CLI 參數。

```text
python /tmp/n6-responses-probe.py <operation> \
  --request-id <fixed-n6-rid> --session-id <fixed-sid> --agent-version <active-test-version> \
  --expected-build <api-code-sha256> --output /tmp/<unique-step>.jsonl [operation-specific arguments]
```

| operation | 額外參數／作用 |
|---|---|
| `agent-object-read` | 純 GET 固定 Agent 物件，確認目前 Responses endpoint 開啟；不猜 default、不 PATCH |
| `version-read` | 純 GET 指定 test version，核對同 build 和兩個旗標 |
| `session-create` | 固定 SID＋指定 version 的一次 POST，由原 API MI 建立；不重試、不呼 FA prepare |
| `session-read` | 純 GET 官方 `/agents/<test>/endpoint/sessions/<SID>`；保存 public SID／狀態／版本 |
| `conversation-create` | 只建空的合成 conversation，metadata 為固定 test Agent ID／API config Team ID |
| `response-create` | `--session-record <session-create.jsonl> --conversation-id <CID> --conversation-record <create.jsonl> [--mode observe\|hold]`；固定合成文字、`background=true, store=true`，`agent_session_id` 在 JSON 最上層 |
| `response-read` | `--response-id <original-response-id>`；只 GET 原 response，白名單解析 probe 結果 |
| `conversation-read`／`items-read` | `--conversation-id <CID> --conversation-record <create.jsonl> [--reader-agent-name <approved-test-X>]`；各一次 GET，items 只取一筆；**沒有 SID 路由參數、沒有 call ID** |
| `receipt-read` | 由 Invocations 只讀這個合成 RID 的原件／目前程序 metadata，不開始回合 |
| `agent-read` | 由受控 Invocations probe，以該 Agent MI **純 GET 一次**原 RID 綁定的同 conversation items |
| `interrupt` | `--process-id <剛觀察到的PID>`；只准 D4 專用 SID、目前程序、唯一 held 合成 RID，回 `interrupt_scheduled` 不代表已中斷 |

## D1：conversation 與明示 SID

1. 先 `agent-object-read`；已明確開啟才 `version-read`→`session-create`，保留 API MI 建立成功原檔，
   再 `session-read` 確認 active／指定版本。建立回應不明就停止，只能由部署線核對原 SID，不再 POST。
   接著 `conversation-create` 建 C1，保存成功原檔。
2. 用第一個事先固定 RID 對 C1 `response-create --mode observe`；保存 response ID。
   `response-read` 只讀該 response；未完成只能後續明示讀原 response，不能再 POST。
3. 用第二份 `conversation-create` 原檔建 C2，第二個固定 RID 送入**同一個明示 SID**。
4. 比對兩份回覆的 `record.client_cid/request_id/submit_digest`、`observed.session_id`、
   `observed.request_session_id`、`agent_version`、GUID presence/hash，以及原 Hosted `FA_N6_PROBE` log。
   只在兩個 conversation 實際同 SID 時，才支持後續單一 Agent 鎖前提。
   不同 SID 或拒絕不是程式自動轉為每 CID 一台 VM；交 creator 依設計選 `previous_response_id` 路線。

**首輪 403 原件保留。** 原 S1 由 operator 建、Responses 由 API MI 送；官方 caller isolation 是強假說，
不是取得原 error.code 的定案。Foundry User 可跨 caller 列出／管理 session，故 GET 200 不證明 invoke 也被允許；
昨晚 endpoint 曾讀回 Responses enabled，也不能當今天的讀回。原 403 只有 hash，不能還原 body，
不為補錯誤碼重送。原 S1／C1／RID1不改、不刪、不當新批次繼續送。
總協調已批准**本窄修 PR 合併後**由 fdbb 做獨立新 D1批次，事前固定新 SID／CID／RID；
這不是把未知請求換 ID 重送，也不是本程式自行取得新一輪執行權。

## D2：換版後的原 conversation

在 D1 已有明確終態後，fdbb 依原批准流程換正常 main 同 build 的 test version／指定 session。
保留 C1、原 response／RID／output，不修改舊版本。先 `session-read`，再以**新的已規劃 RID**
對原 C1 送一次 observe，仍帶原 `--conversation-record`；不能把未知請求換 RID 當重試。
對照新舊實際版本、public SID、request SID、response／conversation IDs、
`session_guid_present/session_guid_sha256`。GUID 是 incarnation metadata，不是 PID；
同 SID 重啟是否保持、換 SID 是否改變，都只記實際觀察，不由 SDK 名稱或文件推定。

## D3：歷史純讀與身分邊界

先由部署線確認 D1／D2 對應 test session 已 idle，保存前後 `session-read` 與該時間窗 Hosted log。
① API MI 對原 C1 `items-read` 一次；②用已批准 test X 的 `--reader-agent-name` 對 test Y 的 C1
`conversation-read` 一次；③經原 endpoint `conversation-read` 一次，核對兩個 metadata 欄位；
④原 target 的 `agent-read` 一次，由 Agent MI 讀同 C1 的 items。

每一步各留 HTTP status／body SHA／request correlation，不保存 items 內容。
①～③若讀取前後沒有可靠 session／log 觀察，只能寫「GET 200」，**不能寫不喚醒 VM 已通過**；
④本身會經 Invocations 進入測試 VM，不納入不喚醒證據。
④保留原 `get_request_context().platform_headers()` 的 call ID，不刻意刪除；只回
`call_id_present`、`credential_kind=agent_managed_identity`、
`identity_kind=agent_bearer_with_platform_caller_context`。
成功或拒絕都不能冒稱「純 Agent 角色隔離」結論。若可讀，交 Neal 決定是否延伸 #306 例外；
不是自動接受授權例外。
**任一步 401／403／404、需新 IAM、redirect 或未知就停止，交總協調，不換身分重試到 PASS。**

### D3④：安全診斷與仍未知的範圍

2026-10-02 的 `21231bcd` 批次，②跨 endpoint 的 `404 not_found` 由總協調明示記為「被拒」、
不當作該批停止點，但不推論完整隔離；不是通用放寬。①③讀取時 session 已 active，不能證明不喚醒 VM。
④的 `N6_PROBE_READ_UNKNOWN` 是共用例外處理，可能早於 items GET：
先讀原 RID 的 State store，再取得 Agent MI token／讀 items。原回應沒有保留確切例外，
所以不把它當成上游 HTTP 503、IAM 失敗或隔離失敗。

本片只為原 `503 N6_PROBE_READ_UNKNOWN` 增加安全診斷；401／403／404 的
`N6_PROBE_STOP` 狀態與回應維持原樣，不新增 retry、換 RID、成功 fallback 或授權例外：

- `error.reason` 優先記 `http_status=<100..599整數>`；沒有可用狀態時記
  `exception_type=<已知固定類別>`。沿既有 `_exception_type` 白名單，另識別 probe 使用的公開
  Azure／MI 例外；未知子類只記 `UNCLASSIFIED`，不直接回傳任意 `type(error).__name__`。
- `error.call_id_present` 直接讀固定 SDK 公開 `get_request_context()` 的布林結果，
  不產生／回傳 call ID。`error.identity_kind=not_observed` 明示未取得可確認的讀取身分結果，
  不能靠配置了 Agent MI 或 call ID 存在就宣稱 token／items 讀取成功。
- `error.message` 僅重述上述安全 reason／布林／固定身分標記，讓原 operator script 的
  去敏錯誤摘要直接保存，不必改腳本或結果分類；同樣內容寫入 `FA_N6_PROBE_READ_FAILED`。
  不記 exception message、任意值、URL、request、token 或 call ID 本身。
- 原 script 頂層 `call_id_present=false`／`credential_kind=api_managed_identity` 描述的是 sender，
  不是 Hosted context。錯誤中的新布林不可與它混為一談；也不偽造成功的 `agent_read` 物件。

三次入口原件在部署線的 `i-p1\smoke\d33-d3-agent-read-hosted-log.txt:23-25`：
台灣時間 16:32:54／16:32:59／16:33:05，都是 Invocations 503，關聯到
`i-p1\b1c\n6-d3d-08-agent-read.jsonl.json` 的同一份外層結果。固定腳本的一次 POST 呼叫與
單一 intent／result 支持「sender 一次、Hosted 三筆入口」；沒有各中介 hop 的重試紀錄，
不能斷言特定 gateway 的政策，也不能外推所有寫入都重送。正式寫入耐重送追查由 71c7 另持，本片不改。

**合併後仍須 API／所有 Hosted／8765 正常同 main build，由總協調安排下一批④。**
不在 running image 補檔、不只換腳本；原 RID／原 unknown 不重送或覆寫，D4 不由本線執行。

### D3④傳輸相容（CODE/offline）

2026-10-02 總協調批准，`read_conversation` 只加入
`transport=httpx.AsyncHTTPTransport(retries=0)`，對齊既有model direct與#434。
client保留`trust_env=False`、`follow_redirects=False`、原30秒timeout、原URL與`platform_headers()`；
不繼承proxy環境、不重複加call ID、不新增重試／模型／其他路徑，也不改原RID或unknown語意。

固定httpx 0.28.1的明示transport預設`verify=True`、`trust_env=True`、`proxy=None`：
CA依`SSL_CERT_FILE`、`SSL_CERT_DIR`、certifi選取；這個True是採用CA環境設定，
**不是信任任意憑證**。TLS仍須`CERT_REQUIRED`與hostname驗證，client的環境proxy開關仍為False。

71c7轉來的f7be／test1 v24新批只有一個sender ConnectError樣本；多筆入口不算獨立批次，
也不回填此前21231的泛用unknown。總協調轉述cbb9的toolbox／WebIQ成功，只支持CA／transport同因假說，
**未證probe根因**；沒有當次CA環境讀回，也不能由共用catch定位store／token／items哪一步。
本片不新取log或重送；④只和下一個已批准的正常同build一起由部署線執行。
即使後批通過，也只能結束本次相容性處理／支持假說，不倒寫舊ConnectError因果。

## D4：header／CID／中斷重入

三個自訂 header 的字串逐字和腳本固定值比對；digest 是本探針合成 input＋CID 的 SHA，
**不是 N6-1a 的正式 submit digest 契約**。`cid_fa_identifier` 明示 Foundry CID 是否符合 FA 64 字元規則；
不符合時保留完整 ID，不截斷、不自行建立對照表。

中斷只在上述 API MI 建立的專用 no-native SID 執行：保留 session-create 原檔→建新的合成 conversation 原檔→固定新 RID
`response-create --mode hold`→等原 log 出現該 RID 的 `FA_N6_PROBE`→`receipt-read` 留下原件與 PID→
人工明示 `interrupt --process-id <該PID>` **一次**。hold 上限 120 秒；時間到／取消只回明確失敗，
不假裝 recovery。這個入口只結束自身 test handler 所在程序，不接受 shell、任意 PID 或 exec。

之後只 `response-read` 原 response／`receipt-read` 原 RID；不再送 response-create。
只有原 response 的 `is_recovery=true`、`state_write_performed=false`、原 `record` 不變且
`synthetic_effect_count=1` 才支持本次只讀重入；若平台沒有重入、SID／GUID 變化或結果不明，
保留原記錄交 creator，不加模型／副作用重試。不把 `interrupt_scheduled`、HTTP 200 或新 PID 當 recovery PASS。

## Verified against

- 傳輸相容基準：main `534717ac`＋本片；真httpx constructor的CA file／directory／certifi三案
  先RED（缺明示transport），補接後相關21案PASS。以記憶體HTTP邊界攔截真網路，仍核真SSL context的
  `CERT_REQUIRED`／hostname、原timeout、proxy關閉、retries=0、原platform header／SID／RID與停止路徑；
  ConnectError仍503／READ_UNKNOWN、只嘗試一次。沒有真TLS握手、模型、SDK native回合或Cloud驗收。
  未改匯入／依賴、operator script與原診斷；不重跑無關矩陣。
- [HTTPX官方SSL／CA環境說明](https://www.python-httpx.org/advanced/ssl/)；
  固定[0.28.1 SSL選取](https://github.com/encode/httpx/blob/0.28.1/httpx/_config.py#L23-L41)、
  [AsyncHTTPTransport預設與連線池](https://github.com/encode/httpx/blob/0.28.1/httpx/_transports/default.py#L279-L311)、
  [AsyncClient的proxy與transport組裝](https://github.com/encode/httpx/blob/0.28.1/httpx/_client.py#L1399-L1443)。
  三個安裝檔均與distribution RECORD相符；這是沿既有官方transport接法，不新增TLS框架或關閉驗證。
- D3④診斷基準：main `1185a216`＋本片 probe／直接測試／文件；真固定 SDK public-host
  `/invocations` 的 status／exception 兩案先因缺診斷 RED，再 GREEN。最後最小相干 selectors
  20 PASS，涵蓋無 call ID、不洩任意例外名稱／值、原401／403／404、CLI仍停止且不再POST、
  flag-off／受控中斷原路徑；API-only cold import不含agentserver／Copilot、禁止網路。
  第一輪測試因 seed 與 host 的本機 root 不同而回404，不當作有效RED；調整到正式 StateStore
  讀取邊界後才得到原503缺診斷的反例。所有批次分開保存，不重跑native/model/live矩陣。
- 本片沿[core 2.2 公開 request context](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_request_context.py)
  及[invocations 1.2 的 context 生命週期](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-invocations_1.2.0/sdk/agentserver/azure-ai-agentserver-invocations/azure/ai/agentserver/invocations/_invocation.py#L546-L590)；
  handler回傳前context尚未reset，無context則官方回空物件，不patch私有context。
  [固定 storage error 的 status_code](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-core_2.2.0/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/storage/_errors.py)
  與前兩檔已核安裝RECORD。具體缺口是FA原catch丟掉安全原因，不是SDK欠缺身分接點。
- 實作：`behavior_hosted.create_host`、`responses_probe`、本腳本；基準 main `75696b6f`，
  普通合併 `6d78bf5b` 的 #365／#366／#367／#369，未改其桌面／per-binding task 語意。
- 本機：固定 core／responses 2.2.0、invocations 1.2.0、azure-core 1.41.0、Copilot SDK 1.0.13；
  `test_responses_probe{,_cli}`、`test_agentserver_compatibility`、N0、#364／binding task、adapters／onboarding
  與精簡 API-only cold import。測試 I/O 都是合成／離線；原 FAIL 與重跑結果分開保存，不升級為 live。
- [官方 handler／mixins](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/handler-implementation-guide.md)、
  [resilience 契約](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/docs/resilience-contract.md)、
  [平台 header](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract)；
  [完整核准來源清單](n6-responses-official-research.md)。本機未安裝 Foundry skill，只讀官方 skill／existing-agent 指引，
  沒有跑 updater、dependency bootstrap、azd、認證、雲端或計費操作。
- **前一輪 #387** D1 窄修只動 script/docs/tests，不動 `shared_context`／相依，不需 image rebuild 或 container pip。
  官方依據：[caller 隔離與跨 caller session 管理](https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user)、
  [實際 endpoint 的 protocol configuration](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent)、
  [固定 Projects 2.3.0 create_session 實作](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-projects_2.3.0/sdk/ai/azure-ai-projects/azure/ai/projects/operations/_operations.py#L5147-L5222)。
  API-only cold import 與最小 parser／一次 POST／unknown／去敏測項另驗，不重跑 0 的 native／MCP 大矩陣。
- 本次拒絕理由診斷的最小真 fixed-2.2 public-host 測項先 RED，再確認原拒絕不變、log只含enum、
  原probe store不寫，以及接受路徑／recovery保持；SDK標準failed response可能回填model，
  不能把SDK回應物件當FA診斷log或request echo。兩種conversation表示亦沿原公開context解析，不另建parser。
- D1c新語意的真public-host測項先RED；非空tools/instructions、標準unused與25個未知鍵皆能保存合成ProbeRecord，
  原RID再入不改record/效果，unknown log只排序20名且不含值；原protected負例保留。
  來源：[固定2.2 CreateResponse](https://github.com/Azure/azure-sdk-for-python/blob/azure-ai-agentserver-responses_2.2.0/sdk/agentserver/azure-ai-agentserver-responses/azure/ai/agentserver/responses/models/_generated/types.py#L2429-L2552)、
  [官方request reference](https://learn.microsoft.com/en-us/python/api/azure-ai-agentserver-responses/azure.ai.agentserver.responses.models.createresponse?view=azure-python)。
  reference頁可能落後，完整鍵以固定2.2公開model為準，不用latest main或輸出schema背書。
- SDK前過濾使用[公開add_middleware](https://learn.microsoft.com/en-us/python/api/azure-ai-agentserver-responses/azure.ai.agentserver.responses.responsesagentserverhost?view=azure-python#azure-ai-agentserver-responses-responsesagentserverhost-add-middleware)
  與[固定Starlette 1.6 ASGI request/receive介面](https://github.com/Kludex/starlette/blob/1.6.0/docs/middleware.md#inspecting-or-modifying-the-request)。
  具體缺口是handler接手前SDK已消費body response_id，不是SDK沒有host/middleware；只新增probe分支的必要接點。
