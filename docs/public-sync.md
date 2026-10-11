# 公開 repo 同步（public-sync）

這個私有 repo 每次**成功部署到 stage** 之後，會把那一版去識別化，推到一個公開 repo。公開 repo 只會看到已經上過 stage 的版本，看不到開發中的分支或未部署的提交。

目前狀態：第一次實際同步已推到公開 repo（release `v2026.10.10-24`，repo 仍是 private）。事後獨立檢查發現匯出還留著作者環境的資源名稱、GitHub 擁有者數字 ID、私有 repo 的純文字名稱與一個私有 commit；替換規則與 verify 已補上（見下方「保留與替換的資源名稱」），下一次同步才會帶到公開 repo。

## 何時同步、同步什麼

1. stage release PR（改 `deploy/stage/release.json`）合併到 `main`，[deploy-staging](../.github/workflows/deploy-staging.yml) 部署 stage。手動觸發的 deploy-staging 也算。
2. 只有那次 deploy-staging **成功**，[public-sync](../.github/workflows/public-sync.yml) 才會開始；失敗、取消或跳過都不會。
3. 環境 `public-sync` 只允許 `main`。目前的 GitHub 方案不支援 private repo 的必要審核人，所以不會停下來等人核准；改由公開 repo 先保持 private 當審核關（Neal 2026-10-11 決定），Neal 看過推進去的內容再自己改成 public（審查流程見下方「每一版的兩道人工審查」）。
4. 取出那次部署的**同一個 commit**，只匯出該 commit 追蹤的檔案，套用替換表，再跑下面的 fail-closed 檢查。任何一項沒過就停，什麼都不推。
5. 讀 `release.json` 的 `version` 與 `milestone`。`milestone` 經同一套替換與檢查後當作 Release 說明。
6. 用公開 repo 的權杖 clone 公開 repo，把整棵樹換成這次匯出（保留 `.git`），以 `github-actions[bot]` 名義建立**一個** commit `Release <version>`，打 tag `v<version>`，推到公開 repo 的 `main`，再建立同名 GitHub Release。

要注意的幾點：

- 公開 repo 只看得到每次 release 一個 commit，不含私有 repo 的提交歷史、SHA 或作者。
- 公開 repo 的內容每次都**整棵取代**。直接加在公開 repo 的檔案，下次同步會被刪掉；要長期保留的檔案請放在私有 repo。
- 同一版重跑時，若內容與既有 tag 相同，就直接結束（Release 不存在時才補建）。若 `version` 沒變但內容不同（例如手動重新部署了較新的 `main`），會**失敗並且不推**；請先升 `release.json` 的 `version`。
- **排隊只留一個**：`concurrency` 不取消執行中的同步，但同一時間只保留一個等待中的執行。若前一次同步還在執行，期間又有兩次 release 部署成功，中間那次會被較新的取代、不會單獨發布。內容不會遺失，因為下一次發布帶的是完整的樹；只是公開 repo 會少那一版的 tag 與 Release。
- **不要重跑舊的 public-sync**：在 Actions 重跑一個舊的 public-sync 時，若它的 tag 在公開 repo 還不存在，會把**較舊的樹**疊在公開 `main` 最上面。沒有審核人會擋下這種重跑，所以不要手動重跑舊的 public-sync；要補發請改用新的 release。
- **release 部署失敗後手動部署**：release 部署失敗、再手動觸發 deploy-staging 成功時，公開 repo 會收到當時部署的 `main` HEAD，版號用 `release.json` 的 `version`。這是可接受的：那個 commit 確實已部署到 stage。

## 不會同步的檔案

排除清單只有一份：[`scrub_export.py`](../scripts/public_export/scrub_export.py) 的 `EXCLUDED_PATHS` 與 `EXCLUDED_PREFIXES`。

| 路徑 | 原因 |
|---|---|
| `scripts/public_export/mapping.json`、`review.json`、`candidates.txt` | 內含原始識別資訊（替換前的值） |
| `.github/workflows/`（整個目錄） | 推送 workflow 檔需要 Workflows 權限，只開 Contents 的權杖推不上去；`public-sync.yml`、`deploy-staging.yml` 也只對私有 repo 與 Neal 的 stage 有意義 |
| `.claude/` | 本機 agent 設定 |

所以公開 repo **沒有任何 CI workflow**。也因此公開 repo 裡有三個讀 workflow 檔的測試會失敗：`tests/shared_context/test_stage_ci_deploy_feed.py` 與 `test_stage_version_publication.py` 各一個（讀 `deploy-staging.yml`），以及 `tests/infra/test_shared_context_avm.py::test_infrastructure_workflow_compiles_without_azure_credentials_or_deployment`（讀 `validate-infrastructure.yml`）；本次沒有改這三個測試。另外 `tests/context_eval/test_context_target_eval.py::test_committed_ledger_preserves_the_historical_strict_failure_and_batch_review` 在公開副本也會失敗（`run content fingerprint differs`）：`docs/context-evaluation-status.json` 目前有 4 個 run 的內容被替換（各自記錄的 `sha256` 原樣保留），重算的指紋對不上；補強替換規則（#786）之前的匯出就已如此。

## 替換與 fail-closed 檢查

[`scripts/public_export/`](../scripts/public_export/) 有三支工具：

- `scrub_export.py`：從指定 checkout 的 HEAD 匯出追蹤檔案。`--plain` 不替換，當作比對基準；不加則依 `mapping.json` 替換 GUID（含無連字號形式與 8 碼前綴）、租戶與人名、Email、資源名稱（含作者環境名稱與 azd 產生的 uniqueString 後綴）、CAE 網域、IP、私有 repo 名稱、GitHub 擁有者與 repo 的數字 ID 等。二進位檔原樣複製。
- `verify_export.py`：檢查匯出結果，**只要有一項不過就以非 0 結束**：
  0. 匯出與基準的檔案清單一致，且沒有任何排除路徑。
  1. gitleaks 真的跑完：找不到執行檔、非 0 結束、沒有報告、掃描位元組為 0 或少於檢查文字的一半，都算失敗；每個發現都必須在已知誤判清單 `gitleaks_known_false_positives` 內。清單只收精確的（gitleaks 規則, 檔案路徑）配對，不接受萬用字元；每對設 `count`，同一檔案多出一個發現也算新的發現而失敗。匯出的內容不能放寬掃描：一律用 gitleaks 預設規則，加 `--ignore-gitleaks-allow`（不認 `gitleaks:allow` 註解）、`--gitleaks-ignore-path` 指向空的暫存目錄，並清掉 `GITLEAKS_CONFIG`、`GITLEAKS_CONFIG_TOML` 環境變數；匯出裡任何位置有 `.gitleaks.toml` 或 `.gitleaksignore` 就直接失敗。
  2. 替換表裡的任何原始值都不能再出現。
  3. 基準中每個長 hex／base64 token（hash、digest）都必須原樣保留。
  4. 殘留掃描：匯出裡**任何位置（含 `tests/`）的每一個 GUID**，都必須是替換表的 placeholder、`keep_guids` 裡的公開 ID，或是已審過、以 sha256 列在 `synthetic_guid_sha256` 的測試／假值；其他一律失敗，不看附近有沒有 tenant 之類的字。`/subscriptions/` ID、Azure 主機名、Email、IPv4、Teams 連結（任何帶路徑的 Teams 網址、含 gov 雲與 deep link；只提網域名不算）與 Teams 討論串／對話 ID（含 URL 編碼形式；樣式見 `verify_export.py` 的 `SWEEPS`；說明文字不要寫出完整樣例，否則自己也會被掃到）、GitHub OIDC subject 裡的 `名稱@數字 ID`（`github-id`；只放行 `example-owner` 與 `foundry-agents` 兩個 placeholder 加全 0 的 ID，其他名稱即使 ID 是 0 也失敗），以及 azd 樣式的產生名稱（`cog-`、`cr`、`st`、`stbk`、`stcon`、`wps-` 加 13 個小寫字元，`generated-name`；沒有數字的後綴替換規則不會改，這裡會擋下）則必須在 `residual_allow` 內。`residual_allow` 放行替換後的真實帳戶主機時，一律寫**完整主機名並以 `$` 結尾**：第一次同步時有一條只看替換後後綴（`demo.`）的樣式，讓前面沒換掉的真實帳戶名稱一起通過。

  `--text <檔案> --text-out <檔案>` 用同一套替換與檢查處理 Release 說明，通過才寫出。輸出會進入 CI log，所以只印 `檔案:行號`，不印完整值：`[2]` 不印原始值的任何字元，只寫規則 ID、GUID 的 placeholder，或前綴／`verify_absent` 字串的序號（例如 `rule <id> absent #1`）；`[3]`、`[4]` 最多印出被標記值的前 4 個字元。
- `discover.py`：掃描 HEAD 的 GUID，依 `review.json` 的人工判斷重建 `mapping.json`，並把每個 GUID 的判斷與上下文寫到 `candidates.txt`（git 忽略）。已有的 placeholder 編號不變。匯出後仍會留下、但還沒列入 `synthetic_guid_sha256` 的 GUID 會印成 `UNLISTED`（只印前 4 碼）；收錄步驟見下方「發現新的識別資訊時」；只把 sha256 寫進 `review.json`，不存原值。

CI 用的 gitleaks 是 8.30.1 官方 release，下載後先核對 sha256 才使用。

## Neal 的一次性設定

順序建議如下；最後才設 `PUBLIC_REPO`，因為它一設好，下一次 stage 部署成功就會觸發同步。

1. **建立公開 repo**：可以是空的；若建立時勾了 README，第一次同步會整棵取代。預設分支用 `main`。（已完成：`neallee2012/fa-agent`，2026-10-11 建為 private；何時改成 public 由 Neal 決定。）
2. **建立 fine-grained PAT**（GitHub → Settings → Developer settings → Fine-grained tokens）：
   - Repository access 選 **Only select repositories**，只選這個公開 repo。
   - Repository permissions 只開 **Contents: Read and write**（Metadata 會自動是 Read-only）。
   - 設定到期日，到期前換新並更新下面的 secret。
3. **在私有 repo 建立環境**（Settings → Environments → New environment）：名稱 `public-sync`。
   - 方案支援時可勾 **Required reviewers** 並加上自己；目前方案不支援，見上面第 3 點。
   - Deployment branches and tags 可限制為 `main`。
   - 在這個環境的 Environment secrets 新增 `PUBLIC_REPO_TOKEN`，值為上一步的 PAT。
4. **設定 repo 變數**（Settings → Secrets and variables → Actions → Variables）：新增 `PUBLIC_REPO`，值為 `owner/name`。

若日後加了 Required reviewers，想取消時到 Settings → Environments → `public-sync` 取消勾選後儲存。想暫停同步：刪除 `PUBLIC_REPO` 變數即可。

目前狀態（2026-10-11）：`neallee2012/fa-agent`（private）、環境 `public-sync`（限 `main`、無審核人）、secret `PUBLIC_REPO_TOKEN`、變數 `PUBLIC_REPO` 都已設定；第一次同步已完成（release `v2026.10.10-24`）。

同步用的權杖：

- 是 fine-grained PAT，Repository access 只有公開 repo，權限只有 Contents（讀寫）與 Metadata（唯讀）。到期日 2027-01-09（依 Neal 2026-10-11 的截圖）。
- 到期後，public-sync 的發布步驟會失敗，直到重新產生權杖、並更新環境 `public-sync` 的 secret `PUBLIC_REPO_TOKEN`。
- 公開 repo 刪掉重建後，舊權杖對新 repo 沒有權限：要到權杖設定重新選取這個 repo（Repository access），否則推送會失敗。

## 發現新的識別資訊時

1. 先把來源修改 commit（工具只讀 HEAD）。
2. 編輯 `scripts/public_export/review.json`：要替換的非 GUID 值加到 `rules`；GUID 若只出現在 `tests/` 卻需替換，加到 `extra_scrub_guids`；公開的 GUID（例如內建角色 ID）加到 `keep_guids` 並寫原因；測試用的假 GUID 依下面的「收錄假 GUID」逐一收錄；確認無害的非 GUID 殘留加到 `residual_allow`；gitleaks 誤判確認後，以精確的規則與檔案加到 `gitleaks_known_false_positives`，`count` 設為目前的發現數。
3. 重建替換表並檢查：

   ```powershell
   python -X utf8 scripts/public_export/discover.py --src .
   python -X utf8 scripts/public_export/scrub_export.py --src . --out <空目錄>\baseline --mapping scripts/public_export/mapping.json --plain
   python -X utf8 scripts/public_export/scrub_export.py --src . --out <空目錄>\export --mapping scripts/public_export/mapping.json
   python -X utf8 scripts/public_export/verify_export.py --out <空目錄>\export --baseline <空目錄>\baseline --mapping scripts/public_export/mapping.json --gitleaks <gitleaks 路徑>
   ```

   `discover.py` 會列出新增的 GUID（只印前 4 碼）；完整上下文看 `candidates.txt`。`--out` 必須是不存在或空的目錄。
4. `review.json` 與 `mapping.json` 一起 commit。

### 收錄假 GUID

`discover.py` 印出的每個 `UNLISTED abcd...` 都要人工看過才收錄，不能整批放行：

1. 在 `candidates.txt` 找到該 GUID，讀它的檔案與上下文，確認是測試或文件用的假值，不是真的租戶、訂用帳戶、應用程式或物件 ID。真的 ID 改加到 `extra_scrub_guids`（要替換）或 `keep_guids`（公開 ID，寫原因）。
2. 只把確認過的值交給 `--accept-synthetic`，可以給完整 GUID，或至少 4 碼、而且只對得上一個未收錄 GUID 的前綴：

   ```powershell
   python -X utf8 scripts/public_export/discover.py --src . --accept-synthetic 8d4e 3a1f9c
   ```

   前綴對到 0 個或 2 個以上、少於 4 碼或不是 hex 時，以 2 結束，什麼都不寫。`is_synthetic()` 認得的明顯假值（重複數字、`0000-0000`、`123456789` 等）會一起收錄；其他沒點名的照樣是 `UNLISTED`，verify 會失敗。
3. 收錄只會增加 `synthetic_guid_sha256`，不會刪掉已審過的值。PR 說明要寫出新收錄了哪些前綴與理由。

## 待決事項

- Release 說明中的 `#123` 這類編號，在公開 repo 會自動連到公開 repo 自己的同號 issue／PR，不是私有 repo 的那一個。

## 保留與替換的資源名稱

第一次同步後改了預設（2026-10-11）：作者 dev／stage 環境用的名稱也替換，不再保留。

- **會替換**：
  - 作者環境名稱的長、短兩種寫法（規則 `resource-stem-env`、`resource-stem-short`），不分大小寫、不看邊界，所以資源群組、Foundry 帳戶與專案、端點主機、儲存體、Key Vault、VNet／VM／NSG 等名稱裡的部分都會換成 `faexample`／`fasample`。placeholder 只用小寫英數字，儲存體帳戶名稱仍然合法。
  - azd 產生的名稱（`infra/azd/main.bicep`：前綴加 13 字元 uniqueString）：已知的後綴各有編號 placeholder（`azd-token-N` → `example00000N`），其他含數字的後綴由 `azd-generated-name` 換成 `example000000`。
    沒有數字的新後綴（約 7%）不會被 `azd-generated-name` 換掉，verify 的 `generated-name` 會擋下、同步停止。補救：在 `review.json` 的 `rules` 照 `azd-token-1` 的格式加一條 `azd-token-N`（literal、`bounded: false`、`ignore_case: true`，`replace` 用下一個編號 `example00000N`），重跑 `discover.py`，把 `review.json` 與 `mapping.json` 一起 commit。
  - GitHub OIDC subject 裡的擁有者（`github-owner-id`）：名稱換成 `example-owner`、數字 ID 歸零，subject 不再把擁有者和私有 repo 名稱配在一起；擁有者名稱以 `名稱@` 的形式出現就由 `[2]` 擋下。repo 那一半的 `foundry-agents` 也是 azd 的 workload 名稱，只把 ID 歸零（`github-repo-id`）。單獨出現的擁有者名稱保留，公開 repo 也屬於同一位擁有者。
  - Key Vault 名稱、ACR 與 Cognitive Services 帳戶的後綴、Container Apps 環境網域（原本就會換）。
  - v18 環境的儲存體帳戶名稱（`storage-v18-stem`，release 25 第二道審查後加入）：儲存體帳戶名稱全域唯一、可被直接指向，備份帳戶又沒有可替換的後綴，所以整個前綴換成 `stfav18example`（例如 `stfav18examplebk`）。
  - v18 環境的 Web PubSub 名稱（`webpubsub-v18-name`，PR #793 review 後加入）：名稱就是全域唯一的主機名（`<名稱>.webpubsub.azure.com`），所以 `environment.example.json` 裡的值換成 `wps-fa-v18-example`。`prepare_environment.py` 用 f-string 推導名稱，是程式而不是實際值，不會被替換。v18 的資源群組、Container Apps 環境、Foundry 帳戶與 ACR 名稱則保留前綴：資源群組與環境名稱不是全域唯一，帳戶與 ACR 的後綴已替換。
  - M365 租戶名稱連同縮寫與大小寫變體（`tenant-name`、`tenant-name-abbrev`）：progress 筆記曾用縮寫寫成「原＜縮寫＞帳號」，第一版規則只認全名，縮寫留在匯出裡。兩條規則都不分大小寫，換成同一個 `Contoso`（例如「原Contoso帳號」）；`[2]` 對縮寫做不分大小寫、不看邊界的檢查，所以比縮寫長的寫法沒被替換也會擋下。
- **保留**：產品與程式本身的名稱（`fa-hosted-sandbox-poc`、`ca-fa-*`、`id-fa-*` 等），以及 azd 的環境與資源群組名稱（`rg-<環境名>`）。它們的後綴與網域已替換，指不到真實資源。
- **縮寫與大小寫變體也要涵蓋**：替換一個真實名稱（租戶、人名、環境、帳戶）時，先在整個 repo 不分大小寫搜尋它的縮寫與前綴（3 個字元以上），每個縮寫各加一條規則；規則與 `[2]` 都要不分大小寫（literal 規則設 `ignore_case: true`，`verify_absent` 另設 `verify_ignore_case: true`）。只寫全名、或只認一種大小寫的規則，會讓變體原樣進入匯出。
- 理由也記在 `review.json` 的 `_resource_names_policy`。發現新的真實名稱時，在 `review.json` 的 `rules` 加 literal 規則、在 `residual_allow` 用完整主機名放行替換後的主機，再重跑 `discover.py`。
- **限制**：全新的一般資源名稱（例如新的資源群組或專案名稱，既不是 Azure 主機名、也不是 13 字元的 azd 後綴）不會被自動偵測；verify 只認得 `review.json` 已列的值與 `SWEEPS` 的通用樣式。所以每一版都要經過下面的兩道人工審查。

`verify_export.py` 本身會被匯出，所以真實名稱不能寫在它裡面（公開副本會被替換成 placeholder，檢查就失效）；真實名稱只放在 `review.json` 的規則，由 `[2]` 檢查不得殘留，`verify_export.py` 只放 `github-id`、`generated-name` 這類通用樣式。測試也照這個做法：真實值從 `review.json` 讀，預期輸出寫 placeholder。

## 每一版的兩道人工審查

verify 只擋已知的值與通用樣式，所以每一版的人工殘留掃描，在 repo 改成 public 之前與之後都要做。公開之後，每次同步會立刻公開，沒有 private 這道緩衝，所以第一道關要在 release PR 合併前完成。

**掃描項目**（兩道關相同）：對整棵匯出樹找作者環境與資源名稱、真實主機名、擁有者與租戶 ID、人名、私有 repo 的名稱與連結，以及 verify 偵測不到的新一般資源名稱。

1. **第一道（release PR 合併前）**：release PR 合併才會觸發 deploy-staging，成功後接著 public-sync，所以在合併前審。
   - 在本機把 `main` 與 release 分支合併（不推送），對合併結果照「發現新的識別資訊時」第 3 點跑完整流程：`--plain` 基準匯出、一般替換匯出、帶 gitleaks 的 `verify_export.py --baseline`。
   - 再對匯出樹做上面的人工掃描。
   - 把結果（流程是否通過、掃描範圍、有無發現）記在 release PR。沒通過不合併 release PR。
   - 若合併前 `main` 又進了新的變更，掃描結果只對檢查當時的內容有效，需重做。
2. **第二道（public-sync 之後）**：clone 剛發布的公開 commit，對整棵樹重做同樣的人工掃描，確認公開內容與第一道看到的一致、沒有漏網。

**第二道發現洩漏、而 repo 已是 public**：

- 先停止後續 release，不要合併新的 release PR。
- 在 `main` 補 `review.json` 的規則與測試，修好替換與檢查，再用新版本重新發布。
- 修正不會移除已公開的內容：公開 repo 的 git 歷史與 GitHub Release、tag 仍保留洩漏的 commit。要清除，需要 repo 擁有者自己刪除該 Release 與 tag，並改寫或重建公開 repo；這是不可逆的擁有者操作，agent 不代做。
- 若洩漏的是任何秘密值，另外輪替該秘密。
- repo 仍是 private 時，同樣修規則；擁有者直接用下次同步整棵取代，不必重建。

## 指向私有 repo 的連結（預設改寫）

私有 repo 裡約有 550 個連結指向私有 repo 本身（issues、pull、blob、commit、actions、tree）。原樣公開會洩漏私有的 PR 編號、commit SHA 與 run ID，公開讀者打開也只會看到 404。所以匯出時**預設改寫**：

- Markdown 連結 `[文字](私有 repo 網址)` 只留文字。
- `<私有 repo 網址>` 與裸網址（含不帶 `https://` 的寫法）換成 `（原私有 repo 連結）`。
  裸網址只取 ASCII 網址字元，遇到中文、全形符號、引號、括號、反斜線、`|`、`*` 就停，結尾的 `.,:;!?` 也留在原文，所以網址後面的文字、表格欄位與粗體標記不會被吃掉。
- 其他 hash／digest 不動；只有被整段移除的私有 repo 網址裡的 SHA 會跟著消失（verify 的 hash 檢查對這些規則標了 `drops_tokens`）。豁免只涵蓋真正被刪掉的字元；Markdown 連結保留下來的文字（例如 ``[`<SHA>`](…)`` 裡的 SHA）仍必須與基準完全相同。
- `controller/progress_b1_b2.py` 用常數 `REPO` 組 PR／commit 連結；公開副本把它改成空字串，產生器就只寫 `#103` 與短 SHA 文字，不會產生「標記後面接 `/pull/103`」這種壞連結；verify 也會擋下標記後面直接接 `/` 的殘留。私有 repo 的行為不變。
- verify 會在匯出與 Release 說明中，找任何殘留的私有 repo GitHub 網址（不分大小寫）；有一個就失敗。
- 不是網址、只寫 `擁有者/名稱` 的純文字提及，由 `this-repo-plain` 換成「原私有 repo」（排在網址規則之後）；文件裡在提及旁引用的私有 commit 由 `private-commit-sha` 全面替換，標題與指向它的錨點一起改，所以仍然對得上。
- 其他單獨出現的 commit SHA（狀態、進度、各文件 `Verified against` 的程式基準）不替換：公開 repo 沒有私有歷史，這些 SHA 在公開 repo 指不到任何東西。要擋的是和私有 repo 名稱或網址一起出現、能拿來定位的引用。

規則在 `review.json` 的 `this-repo-progress-constant`、`this-repo-md-link`、`this-repo-autolink`、`this-repo-url`。想讓連結原樣公開：刪掉這四條規則後重跑 `discover.py`，把 `review.json` 與 `mapping.json` 一起 commit。根目錄 README 的「GitHub issue」用相對連結 `../../issues`，在兩個 repo 都指向各自的 issues。

## Verified against

- 程式基準：分支 `neallee2012-public-sync`（從 `origin/main` `3f6ad101` 開出，2026-10-10 與 `origin/main` `4bb59ac4` 比對無落後）；第二輪 review 修正後以 `8d345c6e` 重驗。
- 已讀程式：`scripts/public_export/scrub_export.py`、`verify_export.py`、`discover.py`；`.github/workflows/public-sync.yml`、`deploy-staging.yml`、`validate-infrastructure.yml`；`controller/progress_b1_b2.py`。
- 實際驗證：`tests/public_export` 離線測試 41 個、`tests/context_eval/test_progress_b1_b2.py` 17 個通過（臨時 git repo fixture，gitleaks 在行程邊界以替身取代，並核對 hermetic 旗標與環境）。本機對本分支 HEAD 的乾淨 detached checkout 跑 plain／scrub 匯出與 verify（真 gitleaks 8.30.1）：1,343 個檔案、沒有 `.github/workflows/`、改寫私有 repo 連結 548 處（Markdown 連結 494、網址 53、進度產生器的 `REPO` 常數 1）、匯出中的 GUID 全部屬於 placeholder（274 處）、`keep_guids`（41 處）或已審的 47 個假 GUID（256 處），結果 PASS。在 `tests/` 加一個未審的隨機 GUID、或加一個改寫規則抓不到的私有 repo 網址，verify 都以 1 結束。`8d345c6e` 重驗：gitleaks 144 個發現全部對上精確清單、結果 PASS；在未列入清單的 `tests/` 檔案放一個假 AWS key，或在已列入的檔案多放一個，`[1]` 都以 1 結束；把保留的連結文字 ``[`<SHA>`](私有網址)`` 裡的 SHA 改一位，`[3]` 以 1 結束。裸網址規則收緊前後，對同一 HEAD 的匯出逐檔比對沒有任何差異（54 處改寫結果相同）。
- 第三輪 review 後（本段所在 commit）：已知誤報必須有 `count` 且至少 1，少了就 `[1]` 失敗；替換規則用具名群組保留的文字仍做 hash 檢查；`tests/public_export` 43 個與 `tests/context_eval/test_progress_b1_b2.py` 17 個通過。乾淨 detached checkout 加真 gitleaks 8.30.1：1,343 個檔案、gitleaks 144 個發現全部對上清單、原值 287 種模式零殘留、hash 未改動、殘留掃描無未審項目，PASS；公開副本內 `test_progress_b1_b2.py` 16 個通過。
- 第一次同步後的補強（分支 `neallee2012-public-export-resource-names`，rebase 到含 #785 的 `origin/main`）：`tests/public_export` 81 個通過。乾淨匯出（HEAD 追蹤檔）加真 gitleaks 8.30.1：1,360 個檔案、gitleaks 144 個發現全部對上清單、原值 296 種模式零殘留、hash 未改動、殘留掃描 1,423 處無未審項目，PASS；匯出中作者環境名稱長短兩種寫法、擁有者數字 ID、私有 repo 純文字名稱、被引用的私有 commit、#785 的兩個 azd 後綴都是 0 處。公開副本另跑受影響的測試（`test_progress_b1_b2.py`、`test_context_target_eval.py`、`test_diagnostic_image.py`、`test_private_controller.py`、`test_vnet_template.py`、`test_private_collection.py`、`test_agent_setup.py`、`test_azd_hooks.py`、`test_auth.py`、`test_teams_deployment.py`、`test_stage_ci_deploy_feed.py`、`tests/public_export`）：314 個通過，失敗的兩個是上面記載的 workflow 測試與 ledger 指紋測試（後者在補強之前的匯出也失敗）。
- OIDC 擁有者與文件補充（分支 `neallee2012-public-export-followup`，從含 #786 的 `origin/main` 開出）：`tests/public_export` 83 個通過。HEAD 追蹤檔的 plain／scrub 匯出加真 gitleaks 8.30.1：1,361 個檔案、gitleaks 144 個發現全部對上清單、原值 297 種模式零殘留、hash 未改動、殘留掃描 1,421 處無未審項目，PASS；匯出中 `名稱@全 0 ID` 只剩 `example-owner` 與 `foundry-agents` 兩個 placeholder，擁有者名稱以 `名稱@` 形式 0 處。五條同理由的主機放行合併成一條前後，verify 對同一份匯出的輸出逐行相同。ledger 內容被替換的 run 是 4 個（`sha256` 與基準相同）。
- 「Commit, tag and push」這一步的 shell 另在本機以 bare repo 代替公開 repo 模擬：空 repo 首次推送、同內容重跑不推、同版號不同內容失敗、新版號接在既有 `main` 之後，皆符合預期。
- 證據與限制：GitHub 上的 workflow **未實際執行**（公開 repo、PAT、環境與變數都尚未設定）；權杖經 credential helper 傳遞、只開 Contents 的 PAT 推送、gitleaks 下載與 `gh release create` 只做過靜態檢查。
