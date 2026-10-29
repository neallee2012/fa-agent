# M3 成果、版本與人工批准 POC

**2026-09-15：M3 核心端到端閉環通過，已停止新增模型工作。** 真正 Hosted 產生成果、Blob 保存不可變版本、真人下載、精確版本批准／拒絕，以及後續模型讀取生效 SOP 都已完成。原始成果 v1 的格式失敗及舊 SOP 的 unknown 工作仍保留，所以 `core_e2e_pass=true`，但 **`full_scenario_pass=false`**；不是全部歷史測項通過。

這是 [M3 驗收情境](POC-ANALYSIS-AND-PLAN.md#5-後續里程碑) 的受控切片，不是完整 FA、正式多人批准系統、任意 skill 執行或自主桌面能力。[M2 UI 缺口](M2-ASSISTANT-POC.md#保留的-ui-缺口)與原 160 次模型計數不變。

## 實際完成的閉環

| 情境 | 結果與證據 |
|---|---|
| 真成果與修訂 | A14 產生 v1／v2，142／156 bytes；API 讀回兩版各自的 Blob native version、ETag、SHA、來源 work／revision，修訂沒有覆寫 v1 |
| 真人取得檔案 | 指定 admin 透過專用登入讀取、下載兩版 Blob；與 Hosted 原件逐 byte 一致，不是代理人代用 agent token |
| 尚未批准 | A15 新 SOP root 存在後，context 回 `not_approved` 且不含草稿；effective 回 HTTP404 `M3_NO_EFFECTIVE_VERSION` |
| Agent 不能批准 | 真正 app-only decision 回 inner HTTP403 `M3_HUMAN_REQUIRED`，不能以 Hosted outer200 判成成功 |
| 批准 v1 | 真人於 04:38:43 UTC 批准精確版本；後續真正 consumer 產生 `APPROVED_POLICY=V1\n` |
| 未批准／拒絕 v2 | 提出 v2 後仍提供 v1；真人於 04:52:34 UTC 拒絕 v2，再讀 context 仍為 v1 |
| 批准 v3 | 真人於 05:06:17 UTC 批准精確 v3，effective 指標才切換；獨立 native API 讀回三版及決定記錄 |
| 後續模型採用 v3 | A15 最後一個 SDK job 真正 `completed`、`task_verified=true`，產生 19 bytes 的 `APPROVED_POLICY=V3\n`，固定版本與 hash 相符 |
| 收尾 | 最後一次明確 stop 成功；05:09:58 UTC 完整 inventory 為 A99／B55，共 154 筆全 idle |

最後 consumer 的題目沒有提供整行答案。已透過 Foundry native files 下載原始 SDK trace，核對 SHA 後確認其第一個 `read_context` 工具結果確實包含已批准 v3 的版本、決定及內容；不是只看模型自稱遵守。測試工作 ID 仍含 v1／v3 標籤，因此這是**資料與批准流程的閉環驗收，不是盲測模型遵規能力或模型對照**。

## 可查看的真實成果

以下是從已核對原件匯出的合成內容，不是重寫的展示文字：

| 成果 | 固定版本 | 檔案 |
|---|---|---|
| 原始成果 v1 | `a3e569b2d33b88a70975ac9c62b8fcdd` | [142 bytes 原檔](evidence/m3-deliverable-v1.txt)，格式 FAIL 保留 |
| 修訂成果 v2 | `41e248c76c0d6c6ca83e824d6e6db404` | [156 bytes 原檔](evidence/m3-deliverable-v2.txt)，包含 `TOTAL=41` |
| v3 consumer | `30edfe7794fbfba0b76a3c52f8200261` | [19 bytes 原檔](evidence/m3-consumer-v3.txt) |

SOP artifact 固定為 `m3-artifacts-01-sop-repair`；三份內容各 18 bytes、沒有末尾換行：

| 版本 | 內容 | 固定 version ID | 人類決定 |
|---|---|---|---|
| v1 | `APPROVED_POLICY=V1` | `22b40dca4920d6ec640fbe5abb52cc4d` | approve，後被已批准 v3 取代 |
| v2 | `APPROVED_POLICY=V2` | `6bd9370c4e0e13123844c71bf1f09ddc` | reject，未生效 |
| v3 | `APPROVED_POLICY=V3` | `57f3ccdd2ff512b90ded888d8d9a2ec8` | approve，目前生效 |

v3 的 SHA-256 為 `8ca595e5201e7069aee68127fa2d451b18a40aaf75ad645ef985f6f590fa0d2c`，native Blob version 為 `2026-09-15T04:55:25.9424348Z`，ETag 為 `"0x8DF12E58E9DEA5C"`。最後 consumer 的 SHA 為 `7d75dcc02c9f7c7ac096bfdcc2eead54b254ea92124170a0ed819f9669af32ea`。

各版本完整 SHA、來源 work／revision、三筆 admin 決定及收尾來源見[去敏證據 catalog](evidence/m3-20260915-0510.json)。批准者 OID 均為指定 admin 的 `00000000-0000-0000-0000-00000000001b`；決定綁定精確 version／SHA／decision ID，並非模型產生的批准聲明。

## 現在如何親自查看

**此 CLI（`m3_artifacts_cli.py`）已於 2026-10 移除，下列命令為歷史紀錄。** 當時：**基本 CLI 已可使用，不必等 M4。** 可查看既有成果與 SOP 決定、下載指定舊版並比對 hash。新自然語言交辦／修訂仍走 Hosted `assistant` 入口，不是下面的人類 CLI；本批 SDK jobs 和 stop 額度已用完，**不得因模型尚餘 21 次就再啟動工作**。新的付費試用需另列有界範圍，不重設既有 counter。

在這個工作樹根目錄的 PowerShell 7 中執行下列唯讀範例。使用指定 admin 登入；公開 client ID 不是 secret，token 僅留在程序記憶體：

```powershell
$env:PYTHONUTF8 = '1'
$env:CONTEXT_API_URL = 'https://ca-facontext-poc.example-00000002.westus3.azurecontainerapps.io'
$env:CONTEXT_TENANT_ID = '00000000-0000-0000-0000-000000000013'
$env:CONTEXT_API_AUDIENCE = 'api://00000000-0000-0000-0000-000000000013/fa-context-poc-api'
$env:CONTEXT_M3_CLIENT_ID = '00000000-0000-0000-0000-00000000002e'
$env:CONTEXT_M3_TEAM = 'team-one'
$py = '.\src\fa-shared-context-poc\.venv\Scripts\python.exe'
$cli = '.\src\fa-shared-context-poc\m3_artifacts_cli.py'

& $py $cli show 'm3-artifacts-01-sop-repair'
& $py $cli download 'm3-artifacts-01-deliverable' `
    --version '41e248c76c0d6c6ca83e824d6e6db404' `
    --output '.\.azure\m3-user-deliverable-v2.txt'
Get-FileHash -Algorithm SHA256 -LiteralPath '.\.azure\m3-user-deliverable-v2.txt'
```

v2 預期 SHA 為 `18c4446933569a327c543286e7840213e29bb0750673f1d7fcf3e642aa70b174`。下載路徑必須尚未存在；CLI 不覆寫既有檔案。這些操作不喚醒 Hosted，但 Context API 可能冷啟動並產生費用。三次原定人類決定都已完成，**不要重跑 approve／reject 命令**。

真正的新批准仍須由人類核對 Blob 內容後親自輸入決定，沒有 `--yes`。唯讀 timeout 最多重試一次，顯示 `M3_READ_RETRY`；決定不重試。若 `phase=decision` 的回應未知，先讀現有決定回執，不盲目重送。完整限制見[實作契約](../src/fa-shared-context-poc/README.md#m3-成果與批准)。

## 部署、用量與費用

原成果 v1／v2 在 A14；修復後 SOP 與 consumers 在獨立 binding 的 A15 session，不能假裝所有測項都在同一 image 完成。修復批次經另行批准，仍使用原 `m3-artifacts-01` 累計 ledger，不重播原 unknown。

- 最終 API：`ca-facontext-poc--m3-2`；A15、B9，West US 3。
- API image：`sha256:11b1918fabc6f70a5e6d5a401b9726ec58a03ee37bd526b2ec9e6820347c0c0b`。
- A15 image：`sha256:2a52129d18f45f0049807d1c9b68505d984b9421a52bff063a8cdb207fbf14b2`。
- SDK `1.0.13`／runtime release `1.0.83`；模型 `gpt-4.1-mini`／`2025-04-14`。
- 專用 private Blob container 與 M1 backup 分離；只有 API MI 有 container 級 read/write，Hosted 沒有直接 Storage 權限。

| 累計項目 | 實際／批准上限 |
|---|---|
| ACR builds | 3／3，含追加的修復 build |
| API revisions／A 新版本部署 | 2／2、2／2；B 未部署 |
| 新 Hosted sessions | 2／2 |
| SDK jobs | 8／8 |
| 模型 attempts | 59／80；59 筆 `upstream_response` |
| Native invocations | 72／80 |
| Explicit stops | 7／7 |
| Native file downloads | 2／24，與兩次人類 Blob 下載分開 |
| Active 測試時間 | 1471.358／3600 秒，約 24.52 分鐘 |

Gateway 記錄的模型用量為 prompt 137142、completion 1684、total 138826 tokens；不是包括 Memory／其他 Azure 服務在內的整體成本。Budget 回應的 `delivery_state=unknown` 原樣保留，不能只靠 attempt counter 推定交付成功；交付另由檔案、版本與 SDK 證據核對。

05:10:16 UTC 的 RG MonthToDate ActualCost 為 **US$18.16541083236238097**。它低於較早讀到的 US$27.079258133377022439，原因尚未確認；執行時保守採較高值，不解讀為退款或節省。M3 預留 US$10 仍包含在原 US$100 內；**帳務有延遲，M3 精確分攤與最終總費用仍未知，不能標成硬上限已證明**。Hosted 全 idle 不等於 ACR、Storage、Private Endpoints 等保留資源免費；本輪沒有整組刪除。

## 保留的失敗與限制

1. 原始成果 v1 正確表達 TOTAL 為 40，卻未提供要求的字面 `TOTAL=40`，仍是 `OUTPUT_FORMAT_MISMATCH`，不是算術錯誤。
2. A14 原 SOP 漏建 metadata，submit 被錯記為 pending／unknown。A15 已增加 `M3_ARTIFACT_CREATE_REQUIRED` 前置防護，但原工作未重送、不清除 unknown，也不改成成功。
3. SOP v3 已完成保存後，獨立版本 GET 的 Foundry 外層曾回 HTTP500 `server_error`，request ID `f95c2fda0d22ded645dcf4bcb91c5fc6`。先保存並離線匯入成功原件，真人批准後才補讀成功；沒有重跑 v3 工作。
4. 真人入口曾遇視窗未呈現、登入逾時、路徑複製錯誤及 `M3_REQUEST_UNKNOWN`。後來手動原 CLI 成功；v2 拒絕及 v3 批准都實際經歷唯讀 timeout 重試。冷啟動是已觀察線索，不是所有舊連線錯誤的確定根因。

本次雲端批准情境實測的是合成 SOP；`skill` 的共用契約有本機覆蓋，沒有另外執行一套雲端 skill 情境。不外推撤銷／回滾、多人審批、正式文件生命週期、完整 Web／Teams 入口、長歷史品質或 10 倍效能。私有 HOME、native files 下載及 Blob REST **都不是共享 filesystem mount 的證據**。

## 證據保存

離線匯出前核對 684 份原件，包含 Hosted requests／responses、真人回執、停止與 inventory、授權和費用摘要。完整 hash manifest 留在 ignored 的 `.azure\m3-final-evidence-manifest.json`，SHA 為 `6f64ab61091a81e8e51c7da0a2aa61e777ff7ca482f6fe35444550b98ace7e8f`。

Repo 只提供選擇性 catalog 與三份合成成果，不匯出原始認證、Memory 內容或整包工具日誌。沒有受控原件的接手者，只能說核對了 repo 摘要，不能宣稱重驗 Azure。使用者於 2026-09-15 13:27 +08 授權保存並 commit／push M2／M3 基準；實際發布版本以 Git 記錄為準，這不是新的雲端測試授權。
