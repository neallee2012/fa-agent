# M0：證據基準與未結清項目

日期：2026-09-14；本文時間均為 UTC，臺灣時間加 8 小時。

**後續更新：** 本文保留02:13階段的M0核對快照。之後M1前置查詢於02:58取得有延遲的本月帳務讀值US$12.3447，並進行一輪有界測試；見[M1結果](M1-PRIVATE-ENVIRONMENT-POC.md)。M1另修正了controller解析器，因此下列source hash是核對當時的版本，不代表後續修改後的工作樹內容。

**證據部分已完成本機原件核對；M0 整體尚未完成。** 02:13核對時的費用為 `UNKNOWN`／`BLOCKED_HTTP429`，資源保留／刪除仍待使用者選擇。M0核對階段沒有重新執行完整雲端驗證。

## 1. 補充歷史，不覆寫歷史

本工作樹以 `f7a5e3bcc9761a5d43f28083b23a1ba97bbff1d2` 的 [M0 計畫](POC-ANALYSIS-AND-PLAN.md) 為基準。該規劃工作階段沒有原始 `evidence\`／`.azure\`，其「只核對 repo 摘要／程式」及「endpoint 修正後只做 targeted 回歸」仍是當時的正確範圍。

**這個工作樹有受控原件，且另有規劃文件未收錄的新完整 demo。** 本文新增其證據鏈，不修改原報告、歷史失敗、原 G0 `BLOCKED` 或 G4 `NOT_RUN_OPTIONAL`。

可分享的 [JSON 目錄](evidence/shared-context-catalog.json) 只含相對路徑、時間、範圍、版本、數量與 hashes；路徑以 repository root 為基準。已逐一核對三個 run 的 **360 檔／1132760 bytes**：檔案集合、長度及 SHA-256 全部吻合，無缺檔或多檔；此數量不含外部 manifest／readback。

| 原件 | 時間、範圍與結果 | 本輪可確認的界線 |
|---|---|---|
| `sc-9a84c17c014b4c3a99bbb3459f5cbb4d` | 09-13 16:34:18–16:38:15；完整功能，17 PASS／370 checks／236.828 秒；165 檔／494607 bytes | 原功能 PASS 保留；額外 50 active records 的歷史執行限制偏差不因後續成功而消失 |
| `sc-668dc3d214c841adbb2e26f46d5efbfc` | 09-14 00:46:27–00:47:34；`PASS_TARGETED_ENDPOINT_ROUTING`，4 PASS／13 NOT_RUN／24 checks／67.016 秒；21 檔／123179 bytes | 只含 A/B runtime、讀取已發布狀態及 stop/idle 收尾；無新增 Memory 呼叫，不是完整 17 gates 重跑 |
| `sc-6727ef07785d457e82c3f8c51fed9b6b` | 09-14 01:22:30–01:25:34；endpoint 修正後新增的完整功能 demo，17 PASS／391 checks／184.187 秒；174 檔／514974 bytes | 是已完成的新 run，本輪僅取回／核對原件；不是本次 M0 再次執行 |

370／391 是 controller assertion 數，不是獨立業務案例；新 demo 多出的 21 checks 來自 Memory polling 檢查（extraction +14、idle/resume +7），不能推論品質或效能改善。這些秒數也不是 SDK 助理 benchmark。

三個 report 及 manifest 的完整 SHA-256、個別 PNG hashes 均在目錄中。原完整 run／demo 的原 manifest 均吻合；endpoint 的 manifest 是本輪從保留原件**新建的本機清單**，不是冒稱執行當時已存在。PNG 本輪只核對檔案 hash，未新增視覺判讀。

### 版本與可重現性

- 已記錄 Hosted `fa-context-a:5`／`fa-context-b:4`、API `ca-facontext-poc--poc4` 與 immutable image digests。新 demo 另有本機保存的 agent-definition 原件；本輪未做 Hosted definition／session API 刷新。
- demo preflight 記錄 source commit `05ca4ca625803df70213c1f9ea3714f4643367f2`；controller SHA-256 `1b99b34814a805f1031ad242c75ed5a187381985182a6680fbe2a7b0ef1f6615` 與 execution／completion、M0核對時的檔案相符；與該 commit 的內容在 CRLF／LF 正規化後相同。Git blob 的原始 bytes hash 另列，不混用。
- 原完整 run、endpoint report **未記錄執行時 source commit／controller hash**，目錄保留 `null`。`05ca4ca...` 是後來收錄成果的 repository 基準，不冒充這兩次執行的逐 byte 原始碼證明。
- image digest 可定位映像，不證明未來從 tag 重建會得到相同 bytes。本輪沒有 build、deploy 或重新驗映像內容。

## 2. 原件取回與分享邊界

原件保存在本工作樹的 `evidence\` 與 `.azure\`，目前授權的本機操作者可以讀取。目錄列出精確相對位置；檔案仍被 Git 忽略，**沒有自動提交、上傳或公開下載位置**。

已為唯一去敏 catalog `docs\evidence\shared-context-catalog.json` 加上明確 Git 例外；其他 raw evidence（包括原始 `evidence\`／`.azure\`）繼續忽略，不因這項例外取得發布許可。

private repository 的分享權限不等於發佈 raw evidence 的許可。原件可能包含 request／response、session／resource 對映與私人測試內容；協作者應先取得資料擁有者對「指定 run／檔案集合、接收者、傳遞方式」的批准，再由授權操作者受控提供並核對 hashes。本文不新增 RBAC、不授予原件存取權，也不承諾其他工作階段已有原件。分享本目錄不代表同意分享 PNG 或完整 report。

在**此工作樹根目錄**可離線核對 report／manifest（不執行任何雲端操作）：

```powershell
$catalog = Get-Content -Raw .\docs\evidence\shared-context-catalog.json | ConvertFrom-Json
foreach ($run in $catalog.runs) {
    foreach ($entry in @($run.report, $run.manifest)) {
        if ((Get-FileHash -Algorithm SHA256 $entry.path).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "Hash mismatch: $($entry.path)"
        }
    }
    $manifest = Get-Content -Raw $run.manifest.path | ConvertFrom-Json
    foreach ($file in $manifest.files) {
        $path = Join-Path $run.evidence_root $file.path
        if ((Get-Item $path).Length -ne $file.bytes -or
            (Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant() -ne $file.sha256) {
            throw "Evidence mismatch: $path"
        }
    }
}
```

## 3. 資源唯讀快照：不是永久現況

本輪先完成 Foundry dependency check，再以 process-only `AZURE_CONFIG_DIR=$HOME\.azure-foundry-agents`、`PYTHONUTF8=1` 核對指定 subscription／tenant／user；沒有 login、全域 config 或 azd 操作。ARM 範圍固定為 subscription `00000000-0000-0000-0000-000000000012`／RG `rg-faexample`。

| 觀察時間 | 唯讀結果 | 不可外推 |
|---|---|---|
| 02:12:10 | ARM inventory 列出 52 個資源／子資源；新 Basic ACR、舊 Premium ACR、兩組 Foundry／project、Storage、Key Vault、NAT、PIP、PE、DNS、VM 磁碟等仍保留 | 不是完整計費 meter 清單，也不是清理批准 |
| 02:12:13 | `vm-fasample-controller`：`PowerState/deallocated`，`Standard_D2ls_v7` | VM compute 停止不代表磁碟／網路等費用歸零 |
| 02:13:29–02:13:31 | API poc4 `Succeeded`，Consumption、0.25 CPU／0.5 Gi、min0／max1、100% latest-revision traffic；replica list 為 **0**，`nextLink=null` | 只是該時間點。01:27 的舊快照是 1 replica，兩者均保留，不把舊值叫作現在 |
| 02:13:33 | `cog-fasample-memory-demo`：`Succeeded`、`publicNetworkAccess=Enabled`、`disableLocalAuth=true`，無 VNet injection／IP 或 VNet allowlist | Non-VNet Foundry 不因 Table PE 而變成全私網 |
| 02:13:35–02:13:37 | `stfacontextpocdemo`：`publicNetworkAccess=Disabled`、`allowSharedKeyAccess=false`；`pe-facontext-table` 的 `table` 連線 `Approved` | 本輪沒有 Table 資料面讀取；`workstate` 與功能存取沿用既有 run 證據 |
| 01:26:59（既有 demo 收尾；本輪本機重核） | 六份 before／during／after inventory 的 ID 集合相同：A 85／B 52；各自 active 0→1→0；結束 137 筆皆 idle、沒有新 session records | 非連續取樣，不保證整段時間無瞬時額外 active；本輪未刷新 Hosted session 現況 |

`cog-fasample-vnet-demo` 不在本輪 inventory；原本 09-13 14:09:32 的個別 GET 已回 404 `ResourceNotFound`。本輪沒有重做刪除／purge，也沒有用清單缺席冒稱新的個別 GET 404。

## 4. 費用、保留風險與關閉條件

本輪只新增 **一次** RG scoped `Microsoft.CostManagement/query`（`2023-03-01`；原 `.azure\non-vnet-cost-query.json` 的 `ActualCost`／`MonthToDate`／`ServiceName`）。02:12:59 請求、02:13:00 完成，仍為 **HTTP429**：

```json
{"error":{"code":"429","message":"Too many requests. Please retry."}}
```

`x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after: 3`；`x-ms-request-id: 00000000-0000-0000-0000-00000000002c`。沒有重試、改 client-type header、跟隨 redirect 或保存 token。01:20 舊查詢的 retry-after `0` 仍只是歷史錯誤，不能當成零費用。

- 截至02:13核對時，最後成功的歷史延遲讀值是 09-13 08:44:48 的 **US$1.3187331418152792**；不是現在總額。當時總額、剩餘額與US$100符合性均 `UNKNOWN`，成本收尾 `BLOCKED`；後續02:58成功讀值另記於上方。
- 原 usage ledger 只涵蓋五次 controller runs＋operator，記錄 18688 tokens，**不含新增 demo**。demo 另記錄 3 accepted/completed updates、8 accepted searches；不能再加一次它繼承的 `parent_*` 計數。未回報／失敗／被拒用量不當作零；tokens、idle、replicas 都不是帳單。
- 新 `crfacontextpocdemo` Basic／舊 `crcontosodemo01` Premium ACR、Storage 資料與交易、VM OS disk、NAT/PIP、Private Endpoints／DNS、監控或其他既有服務仍可能計費；零 replicas／deallocated 並非全 RG 零成本。沒有選定付費保留期間。
- **仍待決定：**取得可用當期 Cost Management 讀值並界定延遲／範圍；由使用者逐項決定保留／移除資源及資料。原 US$100 不構成新支出授權，本文不授權刪除，也不替使用者決定保留多久。

## 5. 交付與驗收邊界

已驗：JSON 可解析、目錄／文件連結在此工作樹可解析、三個 report／manifest 與全部原件 hashes、各 gate／check 計數、demo provenance／六份 session snapshot／closeout 一致性，以及限定 ARM／VM／API replicas 快照。完整本機核對摘要位於 `.azure\m0-local-evidence-verification.json`，其餘 readback 由目錄索引；交付檔案的最終核對記錄為 `.azure\m0-validation.json`。

未驗：SDK 自主推理與 UI 決策、M1 私人 HOME／profile 持久性、共享 filesystem mount、固定 VM、長歷史品質及 10 倍效能。固定 `xdotool` GUI PASS 仍不等於「模型看畫面→決策→OS 操作」；Storage REST／本機 Docker／session-private HOME 都不證明 Hosted 可 mount 共享 filesystem。

M0核對階段沒有 Foundry inference、Memory update/search、Hosted invoke/start/create/stop/delete、build/deploy、RBAC／資源變更、上傳、Git commit/push 或刪除。**交付狀態：證據已核對；最終成本與資源去向未結清，M0 不標為全部 PASS。**
