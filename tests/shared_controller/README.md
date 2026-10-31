# 指定 Hosted A/B sessions 驗證 controller

`scripts\validate-shared-hosted.py` 只用 Python 3.11+ stdlib。它不建立資源、身份、角色、
registry、agent 或額外 session，也不刪除 session；不修改已建置的 runtime。

> **注意：下列範例指令已失效（已於 2026-10 移除）。** 範例的 `--azd-project ".\deploy\shared-context"` 所指的 `deploy/shared-context/azure.yaml` 已刪除，該目錄不再是 azd 專案；controller 現在會在缺少 `azure.yaml` 時直接報錯，不會讓 azd 往上層找。需要重跑時，`--azd-project` 要改指向另一個含自己 `azure.yaml` 的 azd 專案。舊檔可看固定連結：（原私有 repo 連結）。

由 parent 建立／設定 API、A/B agents 與兩個 sessions 後，從 repository root 執行：

```powershell
python .\scripts\validate-shared-hosted.py `
  --azd-project ".\deploy\shared-context" `
  --environment facontext-shared `
  --session-a "<既有 A session ID>" `
  --session-b "<既有 B session ID>" `
  --endpoint-a "<agent show 回傳的 A HTTPS invocations endpoint>" `
  --endpoint-b "<agent show 回傳的 B HTTPS invocations endpoint>" `
  --agent-a fa-context-a --agent-b fa-context-b `
  --evidence-dir ".\evidence\shared-hosted" `
  --max-seconds 2700 --gui
```

`--gui` 可省略；省略時 G3 標為 `NOT_RUN`，不是 PASS。可用 `--team`／`--actor-a`／
`--actor-b` 調整實際 allowlist 名稱，預設 `team-one`／`agent-a`／`agent-b`。A/B 的
agent/service、session、actor 名稱必須分別不同。`--max-seconds` 範圍 1–3600。
API URL／audience／MI 在已部署 Hosted image 的 env，不由本 controller 持有 token。
`--endpoint-a/b` 是必要欄位，取自各自 `azd ai agent show --output json` 的
`agent_endpoints.invocations`；本 POC 限定 public Azure 的 HTTPS `services.ai.azure.com`、
同一 project、正確 agent 與 `invocations?api-version=v1`。不能傳 playground URL、API URL 或其他主機。

本次 parent 已部署 `fa-context-a` version `5`、`fa-context-b` version `4`；由 parent 建立並
提供各自綁定版本的 sessions。Controller 不自行建立 sessions，也不在既有 `--session-id`
上另加 `--version`。此版本資訊來自部署交接，不冒充 controller 已讀回驗證的 runtime 版本。
所有 azd subprocess 的 cwd 都是 `--azd-project` 解析後的絕對路徑（本次為
`deploy\shared-context`），不是 controller 所在的 `scripts` 目錄；`-C` 也使用同一路徑。

繼承呼叫端的 `AZURE_CONFIG_DIR`，僅在子程序 env 設
`AZURE_DEV_USER_AGENT=microsoft_foundry_skill`。每條 azd invoke 明確使用
`--agent-endpoint <verified endpoint> --session-id <id> --input-file <absolute path>
--output raw -C <project> -e <env>`，protocol 由 endpoint 決定，不傳 service name 或 `--version`。
只用 `sessions stop/show` 控制同一批 sessions；沒有 `--new-session` 或刪除命令。

**已知 CLI 路由陷阱：** 本次安裝版本的 service-name 模式，即使指定 `--session-id` 仍增建一筆
多餘 session；endpoint 模式的有界對照沒有增加。舊完整 run 的額外 active 記錄已停止，
但不可因此宣稱歷史上始終只有兩個 active。修改後 controller 的 A/B runtime／已發布成果唯讀
回歸前後維持 A85／B52、全部 idle。首次切換版本或呼叫方式時，編排者仍須比對完整 session
清單前後差異，不能只看回應的指定 session ID；不要自行執行更多付費重測。

## 行為與證據

- 順序涵蓋共享工作、create conflict、非 owner／ETag、同 ETag CAS、交接、Memory 抽取／
  recall／授權、A/B context、A stop→native idle→B 更新→A 同 ID resume、發布不可變。
- CAS 檢查兩次實際 HTTP status 是一筆 200／一筆 412，並 GET 比對勝出的內容及 revision +1，
  不只相信 runtime 回傳的 boolean。
- Memory payload 是有界的合成 dashboard 外觀偏好：隨機 marker，Team `sepia`、A private `indigo`、
  B 在 A idle 時新增 `coral`。goal／search 只包含 marker，不含答案或私人 value。
  `completed` 必須有非空 operations；kind 保留 `created`／`updated` 等原值，不以 `create` 過濾。
  抽取後還必須 search 命中 marker 和 value，才算 recall PASS。
- 歷史 verification-code fixture 確實失敗：native extraction 完成，但省略了 opaque value。
  該 FAIL 保留；偏好案例通過也不代表 Memory 能逐字保存任意資料。精確成果／版本／hash 用 Table。
- Private target read/write、private operation、cross-team、注入 scope／owner 都有負向測試。
  B context 不得包含 A 的 private value，A resume context 必須找回自己的 private fixture。
- 標準成功路徑新增 **3 次 accepted Memory updates、8 次 accepted searches**，加 parent
  原有 1 update／1 search，共 4／9。每次 context 計兩次 search。direct native probes 的
  意外成功也會計數。保守 guardrail 另限制本 controller 最多 8 update attempts／32 search
  attempts，包括預期會被拒絕的 probes；不把失敗或未知結果當成沒有可能花費。
  這裡 `parent_*` 只代表最初那一次 operator 探針；多次 run 的總用量要把各 run 的新增量相加，
  再加一次 operator 基線，不可重複累加每份 report 的 `total_*_including_parent`。
- 每個 native operation 最多 12 次 poll、120 秒；pending 只重試讀取、不重送更新。
  sessions show 最多 6 次、60 秒；只接受 native `idle`，不把 `Stopped`、404 或 provisioning
  Succeeded 當成 session idle。
- direct Storage／Memory 都須真正 HTTP 401/403。token／網路錯誤、404、500 皆 FAIL。
  **Storage 可能被 private network 擋住，拒絕結果不單獨證明 RBAC。**
- GUI 選項只在 A 執行一次，檢查非 root／sandbox、cleanup、PNG hash/size、前後截圖差異，
  並驗證下載文字與 hash。PNG 另存至該次 evidence 目錄。
- 每次執行在 evidence-dir 下新增唯一 `sc-<uuid>` 目錄，逐步寫入 request、經 token redaction
  的原始 stdout/stderr，以及原子更新的 `report.json`。保留 outer HTTP 與 inner API 結果區別。
- gate 失敗後依賴它的 gate 為 `NOT_RUN`；獨立 direct probes／可選 GUI 仍可在剩餘預算內執行。
  無盲目 mutation retry。無論核心 gate 是否失敗，finally 都會嘗試停止 A/B 並 show 驗證 idle。
- 工作預算保留最多最後 240 秒（或短預算的四分之一）給 cleanup。每次子程序有 timeout，
  invocation 最多 180 秒。超過總預算不再啟動命令，因此總預算已耗盡或 controller 被強制
  kill 時無法保證 cleanup；report 明確 FAIL／未執行，parent 必須手動檢查指定 sessions。

所有必要 gate PASS 才 exit 0；失敗或必要 gate 未執行 exit 1；參數錯誤 exit 2。
runtime 不要求可寫 HOME、`home_marker`、PID 或 `fixed_vm_validated` 欄位。只檢查明確
session ID、A/B 的不同 boot markers，以及 A resume 後 marker 改變和外部狀態／記憶重載。
`home_marker_retained` 只記錄，不當成 gate；缺任一 marker 時為 null，不把兩邊缺值誤判為保留成功。
Controller 本身永遠不聲稱固定 VM／共享掛載已驗證。未知 transport 或 outer HTTP 500
會使當前 gate FAIL、阻止後續依賴的 mutations，不重送不確定是否已接受的更新；finally
仍對兩個指定 sessions 執行 stop/show。

## 離線測試

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s .\tests\shared_controller -p "test_*.py" -v
python .\scripts\validate-shared-hosted.py --help
```

測試用 fake azd runner／fake data plane／fake PNG，**完全不呼叫 Azure**。測試驗證 parser、
完整順序、精確 command scope、env 繼承、Memory 次數、ETag／發布、不重試 mutation、
budget、FAIL/NOT_RUN、credential redaction 與 finally cleanup；不能取代真正 Hosted 執行。
已知格式邊界：raw invoke 必須是 HTTP status＋headers＋JSON object；sessions show 支援
top-level 或 properties/session 下的 state/status。遇到未支援格式會保存原始證據並 FAIL，
不猜測成功，也不自行建立新 sessions。
已實測 azd 可能把 extension 更新通知直接接在 JSON 後。Parser 只接受完整 JSON 加上
已知的三行 update notice。M1實測另確認失敗回應會追加
`ERROR: POST <HTTPS URL> failed with HTTP <status>: <status> <reason>`；
parser只在HTTP錯誤狀態及尾訊狀態一致時接受它，保留原錯誤body，不改成成功。
其他尾端文字、第二個JSON或截斷結果仍失敗。新的stdout/stderr保存明確保留換行，
避免Windows把原CRLF再擴成CRCRLF；既有原件不改寫。
