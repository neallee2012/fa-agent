# 公開 repo 同步（public-sync）

這個私有 repo 每次**成功部署到 stage** 之後，會把那一版去識別化，推到一個公開 repo。公開 repo 只會看到已經上過 stage 的版本，看不到開發中的分支或未部署的提交。

目前狀態（2026-10-13）：公開 repo `neallee2012/fa-agent` 已改成 public，最新同步是 release `v2026.10.13-28`。第一次同步（release 24）事後檢查發現的洩漏，已由替換規則與 verify 補上（見下方「保留與替換的資源名稱」）；經過與歷史改寫見「公開紀錄」。2026-10-13 第一次完整的 Azure 清單比對發現已公開的 5 個 Azure Bot Service 名稱，替換規則已補上、從下一版起生效，Neal 已決定（2026-10-13）只修以後、不重寫公開歷史（見「清單比對基準」）。

## 何時同步、同步什麼

1. stage release PR（改 `deploy/stage/release.json`）合併到 `main`，[deploy-staging](../.github/workflows/deploy-staging.yml) 部署 stage。手動觸發的 deploy-staging 要填 release PR，而且部署的 commit 必須正好是那個帶指紋的 release PR 合併的 commit，否則 deploy-staging 在登入 Azure 前就停下（2026-10-16 起），public-sync 也不會啟動（見下面「release 部署失敗後手動部署」）。
2. 只有那次 deploy-staging **成功**，[public-sync](../.github/workflows/public-sync.yml) 才會開始；失敗、取消或跳過都不會。
3. 環境 `public-sync` 只允許 `main`。目前的 GitHub 方案不支援 private repo 的必要審核人，所以不會停下來等人核准；改由公開 repo 先保持 private 當審核關（Neal 2026-10-11 決定），Neal 看過推進去的內容再自己改成 public（審查流程見下方「每一版的兩道審查」）。2026-10-12 已改成 public，之後每次同步立刻公開，第一道審查要在 release PR 合併前完成；第二道由 public-sync 最後一步自動比對。
4. 取出那次部署的**同一個 commit**，只匯出該 commit 追蹤的檔案，套用替換表，再跑下面的 fail-closed 檢查。任何一項沒過就停，什麼都不推。
5. 讀 `release.json` 的 `version` 與 `milestone`。`milestone` 經同一套替換與檢查後當作 Release 說明。
6. 推送之前先讀合併成部署 commit 的 release PR 說明（只認合併當時的說明），取出第一道記錄的 `public-sync fingerprint:`。指紋是對 release PR 開出當時的 `main` 算的；合併前 `main` 又前進（有新的合併進來），指紋就過期，這次 release 照樣部署 stage，但**什麼都不會公開**（第 7 步比對不符就停）。復原方式是重做第一道、升 `release.json` 的版本，再開新的 release PR 記錄新指紋（那是另一次要核准的 stage 部署）；重跑這次的 public-sync 沒有用，也不要重跑。
7. 用公開 repo 的權杖 clone 公開 repo，把整棵樹換成這次匯出（保留 `.git`），`git add -A` 後以 `git write-tree` 算出將要發布的 tree SHA，交給 `prepush_check.py` 和第 6 步的指紋比對。**沒有可用的指紋或不相符，就停下、什麼都不推**（沒有 commit、tag、Release），註記會說明原因。相符才以 `github-actions[bot]` 名義建立**一個** commit `Release <version>`，打 tag `v<version>`，推到公開 repo 的 `main`，再建立同名 GitHub Release。
8. 最後執行第二道檢查（`gate2_check.py`，唯讀）：公開 tag 的 tree SHA 要等於 release PR 記錄的指紋與部署 commit 的匯出，公開檔案要通過 verify。只要推送成功就會執行，建立 GitHub Release 失敗也不會略過。推送前已比對過指紋，這一步主要是再核一次已發布的內容與 verify；失敗時內容已經公開，只負責亮紅燈叫人（見「每一版的兩道審查」）。

要注意的幾點：

- 公開 repo 只看得到每次 release 一個 commit，不含私有 repo 的提交歷史、SHA 或作者。
- 公開 repo 的內容每次都**整棵取代**。直接加在公開 repo 的檔案，下次同步會被刪掉；要長期保留的檔案請放在私有 repo。
- 同一版重跑時，若內容與既有 tag 相同，就直接結束（Release 不存在時才補建）。若 `version` 沒變但內容不同（例如手動重新部署了較新的 `main`），會**失敗並且不推**；請先升 `release.json` 的 `version`。
- **排隊只留一個**：`concurrency` 不取消執行中的同步，但同一時間只保留一個等待中的執行。若前一次同步還在執行，期間又有兩次 release 部署成功，中間那次會被較新的取代、不會單獨發布。內容不會遺失，因為下一次發布帶的是完整的樹；只是公開 repo 會少那一版的 tag 與 Release。
- **不要重跑舊的 public-sync**：在 Actions 重跑一個舊的 public-sync 時，若它的 tag 在公開 repo 還不存在，會把**較舊的樹**疊在公開 `main` 最上面。沒有審核人會擋下這種重跑，所以不要手動重跑舊的 public-sync；要補發請改用新的 release。
- **release 部署失敗後手動部署**：release 部署失敗後，手動觸發 deploy-staging 時填那個 release PR；只有部署的 commit 就是它合併的那一個（中間 `main` 沒有新的合併）才會部署，推送前比對指紋相符，公開 repo 才會收到它。`main` 已經有新的合併時，deploy-staging 的 `release-gate` 在登入 Azure 前就停下（2026-10-16 起；之前會部署較新的 `main` HEAD、只是不公開）。要部署並公開那一版，請對它重做第一道、開新的 release PR 記錄指紋。
- **指紋因 `main` 前進而過期**：第一道算出指紋後、release PR 合併前，只要 `main` 多了別的合併，部署 commit 的匯出樹就不是第一道審過的那棵。release 仍會部署，但 public-sync 停在推送之前，這一版什麼都不公開。復原：對新的 `main` 重做第一道，升版本，開新的 release PR（又是一次要 Neal 核准的 stage 部署）；不是重跑 public-sync。合併 release PR 前先確認 `main` 沒有動。

## 不會同步的檔案

排除清單只有一份：[`scrub_export.py`](../scripts/public_export/scrub_export.py) 的 `EXCLUDED_PATHS` 與 `EXCLUDED_PREFIXES`。

| 路徑 | 原因 |
|---|---|
| `scripts/public_export/mapping.json`、`review.json`、`candidates.txt` | 內含原始識別資訊（替換前的值） |
| `.github/workflows/`（整個目錄） | 推送 workflow 檔需要 Workflows 權限，只開 Contents 的權杖推不上去；`public-sync.yml`、`deploy-staging.yml` 也只對私有 repo 與 Neal 的 stage 有意義 |
| `.claude/` | 本機 agent 設定 |

所以公開 repo **沒有任何 CI workflow**。也因此公開 repo 裡有兩個讀 workflow 檔的測試會失敗：`tests/shared_context/test_stage_ci_deploy_feed.py` 與 `test_stage_version_publication.py` 各一個（讀 `deploy-staging.yml`），目前沒有改。`tests/infra/test_shared_context_avm.py::test_infrastructure_workflow_compiles_without_azure_credentials_or_deployment`（讀 `validate-infrastructure.yml`）在沒有 `.github/workflows/` 目錄時改成跳過並寫明原因；私有 repo 有這個目錄，照常執行。另外 `tests/context_eval/test_context_target_eval.py::test_committed_ledger_preserves_the_historical_strict_failure_and_batch_review` 在公開副本也會失敗（`run content fingerprint differs`）：`docs/context-evaluation-status.json` 目前有 4 個 run 的內容被替換（各自記錄的 `sha256` 原樣保留），重算的指紋對不上；補強替換規則（#786）之前的匯出就已如此。

## 替換與 fail-closed 檢查

[`scripts/public_export/`](../scripts/public_export/) 有下列工具：

- `scrub_export.py`：從指定 checkout 的 HEAD 匯出追蹤檔案。`--plain` 不替換，當作比對基準；不加則依 `mapping.json` 替換 GUID（含無連字號形式與 8 碼前綴）、租戶與人名、Email、資源名稱（含作者環境名稱與 azd 產生的 uniqueString 後綴）、CAE 網域、IP、私有 repo 名稱、GitHub 擁有者與 repo 的數字 ID 等。二進位檔原樣複製。
- `verify_export.py`：檢查匯出結果，**只要有一項不過就以非 0 結束**：
  0. 匯出與基準的檔案清單一致，且沒有任何排除路徑。
  1. gitleaks 真的跑完：找不到執行檔、非 0 結束、沒有報告、掃描位元組為 0 或少於檢查文字的一半，都算失敗；每個發現都必須在已知誤判清單 `gitleaks_known_false_positives` 內。清單只收精確的（gitleaks 規則, 檔案路徑）配對，不接受萬用字元；每對設 `count`，同一檔案多出一個發現也算新的發現而失敗。匯出的內容不能放寬掃描：一律用 gitleaks 預設規則，加 `--ignore-gitleaks-allow`（不認 `gitleaks:allow` 註解）、`--gitleaks-ignore-path` 指向空的暫存目錄，並清掉 `GITLEAKS_CONFIG`、`GITLEAKS_CONFIG_TOML` 環境變數；匯出裡任何位置有 `.gitleaks.toml` 或 `.gitleaksignore` 就直接失敗。
  2. 替換表裡的任何原始值都不能再出現。
  3. 基準中每個長 hex／base64 token（hash、digest）都必須原樣保留。
  4. 殘留掃描：匯出裡**任何位置（含 `tests/`）的每一個 GUID**，都必須是替換表的 placeholder、`keep_guids` 裡的公開 ID，或是已審過、以 sha256 列在 `synthetic_guid_sha256` 的測試／假值；其他一律失敗，不看附近有沒有 tenant 之類的字。`/subscriptions/` ID、Azure 主機名、Email、IPv4、Teams 連結（任何帶路徑的 Teams 網址、含 gov 雲與 deep link；只提網域名不算）與 Teams 討論串／對話 ID（含 URL 編碼形式；樣式見 `verify_export.py` 的 `SWEEPS`；說明文字不要寫出完整樣例，否則自己也會被掃到）、GitHub OIDC subject 裡的 `名稱@數字 ID`（`github-id`；只放行 `example-owner` 與 `foundry-agents` 兩個 placeholder 加全 0 的 ID，其他名稱即使 ID 是 0 也失敗），以及 azd 樣式的產生名稱（`cog-`、`cr`、`st`、`stbk`、`stcon`、`wps-` 加 13 個小寫字元，`generated-name`；沒有數字的後綴替換規則不會改，這裡會擋下）則必須在 `residual_allow` 內。`residual_allow` 放行替換後的真實帳戶主機時，一律寫**完整主機名並以 `$` 結尾**：第一次同步時有一條只看替換後後綴（`demo.`）的樣式，讓前面沒換掉的真實帳戶名稱一起通過。

  `--text <檔案> --text-out <檔案>` 用同一套替換與檢查處理 Release 說明，通過才寫出。輸出會進入 CI log，所以只印 `檔案:行號`，不印完整值：`[2]` 不印原始值的任何字元，只寫規則 ID、GUID 的 placeholder，或前綴／`verify_absent` 字串的序號（例如 `rule <id> absent #1`）；`[3]`、`[4]` 最多印出被標記值的前 4 個字元。
- `discover.py`：掃描 HEAD 的 GUID，依 `review.json` 的人工判斷重建 `mapping.json`，並把每個 GUID 的判斷與上下文寫到 `candidates.txt`（git 忽略）。已有的 placeholder 編號不變。匯出後仍會留下、但還沒列入 `synthetic_guid_sha256` 的 GUID 會印成 `UNLISTED`（只印前 4 碼）；收錄步驟見下方「發現新的識別資訊時」；只把 sha256 寫進 `review.json`，不存原值。
- `fingerprint.py`：在記憶體裡做出某個私有 commit 的匯出（用該 commit 的 `mapping.json`），以 git plumbing 在暫存 repo 算出公開 commit 會有的 tree SHA，印成 `public-sync fingerprint: <tree SHA>`。第一道把這行記在 release PR。
- `prepush_check.py`：public-sync 推送前的比對。將要發布的 tree SHA 必須等於 release PR 說明裡唯一的 `public-sync fingerprint:`；沒有、不只一個或不相符都以 1 結束，什麼都不推。只印 tree SHA。
- `gate2_check.py`：第二道。比對公開 tag 的 tree SHA 與第一道的指紋、與部署 commit 的指紋，再對公開檔案重跑 verify；只印 tree SHA、檢查編號與檔案行號。
- `inventory_scan.py`：第一道的 Azure 清單比對，見「每一版的兩道審查」。只印 `inv-<hash>`、種類與檔案位置，不印名稱。

CI 用的 gitleaks 是 8.30.1 官方 release，下載後先核對 sha256 才使用。

## Neal 的一次性設定

順序建議如下；最後才設 `PUBLIC_REPO`，因為它一設好，下一次 stage 部署成功就會觸發同步。

1. **建立公開 repo**：可以是空的；若建立時勾了 README，第一次同步會整棵取代。預設分支用 `main`。（已完成：`neallee2012/fa-agent`，2026-10-11 建為 private，2026-10-12 由 Neal 改成 public。）
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

設定狀態（2026-10-13）：`neallee2012/fa-agent`（public）、環境 `public-sync`（限 `main`、無審核人）、secret `PUBLIC_REPO_TOKEN`、變數 `PUBLIC_REPO` 都已設定；最新同步見文件開頭與「公開紀錄」。

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
- **限制**：全新的一般資源名稱（例如新的資源群組或專案名稱，既不是 Azure 主機名、也不是 13 字元的 azd 後綴）verify 偵測不到；verify 只認得 `review.json` 已列的值與 `SWEEPS` 的通用樣式。第一道的 Azure 清單比對（`inventory_scan.py`）補上已存在於 Azure 的名稱；不在 Azure 的名稱仍靠人工檢查，見下面「每一版的兩道審查」。

`verify_export.py` 本身會被匯出，所以真實名稱不能寫在它裡面（公開副本會被替換成 placeholder，檢查就失效）；真實名稱只放在 `review.json` 的規則，由 `[2]` 檢查不得殘留，`verify_export.py` 只放 `github-id`、`generated-name` 這類通用樣式。測試也照這個做法：真實值從 `review.json` 讀，預期輸出寫 placeholder。

## 每一版的兩道審查

verify 只擋已知的值與通用樣式。公開之後，每次同步會立刻公開，沒有 private 這道緩衝，所以**人要看的部分全部放在第一道（release PR 合併前）**；第二道（public-sync 之後）改成自動比對，只有比對不符或 verify 有發現時才需要人（2026-10-13 Neal 核准這個做法）。

1. **第一道（release PR 合併前）**：release PR 合併才會觸發 deploy-staging，成功後接著 public-sync，所以在合併前做完。
   1. **先確認上一次 public-sync（含第二道）是綠燈**；若第二道失敗，先照下面「第二道發現洩漏」處理完，才能做新的 release。
   2. 在本機把 `main` 與 release 分支合併（不推送），對合併結果照「發現新的識別資訊時」第 3 點跑完整流程：`--plain` 基準匯出、一般替換匯出、帶 gitleaks 的 `verify_export.py --baseline`。
   3. **Azure 清單比對**（找 verify 不認得的新資源名稱）：用自己的隔離 az 登入，唯讀列出指定訂用帳戶的資源與資源群組名稱、訂用帳戶與租戶名稱、租戶已驗證網域，在記憶體裡做成黑名單，不分大小寫掃**整棵**匯出樹；名稱拆開（`-`、`_`、`.`、`/`、空白）後 5 個字元以上的片段也掃，常見 Azure 字（`storage`、`westus3` 等）略過。比對以完整的字為單位：前後緊接英文字母或數字就不算（`-`、`.`、空白、引號、中文都算分隔），所以片段出現在較長的字裡面不會被標出。

      ```powershell
      python -X utf8 scripts/public_export/inventory_scan.py --export <空目錄>\export `
        --azure-config-dir <隔離的 AZURE_CONFIG_DIR> --subscription <訂用帳戶> [--subscription <另一個>]
      ```

      輸出只有 `HIT inv-<8 碼 hash> <種類> 檔案:行:欄`，不印名稱本身。每個 HIT 打開該行判斷：要替換就照「發現新的識別資訊時」加 `review.json` 規則；確認可以公開就加到 `review.json` 的 `inventory_allow`（`{value, why}`）。一個字只有在 `inventory_allow` 裡（或屬於常見字清單）才不會被標出，「上一版已經公開」不是放行理由；已審過的基準見下面「清單比對基準」。讀不到 Graph 的已驗證網域時會印警告並略過這一類。清單只放在記憶體；要留存請用 `--save-inventory` 寫到 repo 與匯出目錄以外的路徑（放在 repo 內會被拒絕）。清單可能把所有東西都漏掉時直接以 2 結束、不算通過：某個訂用帳戶列出 0 個資源群組或 0 個資源（沒有讀取權限的身分會得到空清單、az 卻回 0）、`--inventory` 不是含 `kind`／`value` 且同時有資源群組與資源的非空清單、或黑名單扣掉略過的字後是空的。
   4. **人工只看兩件事**：上一步與 verify 標出的項目；以及這一版新增或修改的文字（`git diff <上一版 release 的 commit>..<合併結果>` 的範圍），特別是說明文字裡臨時取的內部代號、人名與連結。不必再整棵樹逐檔看。
   5. **記錄指紋**：對合併結果算出公開 commit 會有的 tree SHA：

      ```powershell
      python -X utf8 scripts/public_export/fingerprint.py --src . --rev HEAD
      ```

      把印出的那一行 `public-sync fingerprint: <tree SHA>` 原樣貼進 release PR 說明，連同 verify、清單比對與人工檢查的結果（是否通過、範圍、有無發現）。沒通過不合併 release PR。public-sync 推送前會比對這一行：沒有這一行、合併後又編輯過說明、或部署的內容已不是這棵樹，就不會公開。
   6. 若合併前 `main` 又進了新的變更，結果與指紋只對檢查當時的內容有效，需重做。
2. **第二道（public-sync 之後，自動）**：public-sync 最後一步執行 `gate2_check.py`。只要推送那一步成功就會跑，後面建立 GitHub Release 失敗也一樣：
   - 用推送前讀到的同一份 release PR 說明（合併它的 PR；只認合併當時的說明，合併後又編輯過就不採用），把 `public-sync fingerprint:` 那一行和剛發布的公開 tag 的 tree SHA 比對；推送前已比對過，這裡不符代表推上去的不是比對過的樹；
   - 也比對部署的 commit 匯出後的 tree SHA（用該 commit 自己的 `mapping.json`，和 public-sync 當時一樣），分得出「第一道之後 `main` 又變了」與「公開 repo 的內容不是這次匯出」；
   - 對公開 tag 的檔案（不帶權杖的淺 clone，唯讀，不推送）重跑 `verify_export.py` 的 [0]–[4]，含 gitleaks；clone 裡沒有該 tag 就直接失敗，不會拿 clone 的 HEAD 代替。

   全部通過，第二道就結束，不需要人。任何一項不符或有發現，這一步失敗、public-sync 顯示紅燈，總協調依下面「第二道發現洩漏」處理。PR 說明沒有可用的指紋時，推送前的比對就已停下、不會公開；workflow 會用註記（`release PR:`）分開說明原因：沒有合併的 PR（例如手動觸發的 deploy-staging）、對到不只一個 PR、說明在合併後被編輯過、或讀 GitHub API 失敗。總協調要在本機重跑時（`--no-gitleaks` 只給本機重跑用，用了要在報告寫明 [1] 沒跑；workflow 一律帶 gitleaks）：

   ```powershell
   python -X utf8 scripts/public_export/gate2_check.py --tag v<version> --src . --rev <部署的 commit> `
     --mapping scripts/public_export/mapping.json --expected <第一道記錄的 tree SHA> `
     --public-repo neallee2012/fa-agent --gitleaks <gitleaks 路徑>
   ```

   tree SHA 只和檔案路徑、內容與執行位元有關，所以本機合併、GitHub 的合併 commit 與 squash 合併只要內容相同，指紋就相同；release 27、28 的私有 commit 重算出的 tree SHA 與公開 commit `a493fefc`、`ba9adcb6` 的 tree 完全一致。比對只用 git 的 tree SHA，verify 讀的公開檔案也直接取自 git 物件、不讀 clone 的工作目錄，所以 Windows 上 `core.autocrlf` 把換行改成 CRLF 的 clone 不會造成假差異（release 28 人工第二道曾因此要重新 clone）。自己手動逐位元組比對公開檔案時，clone 要加 `-c core.autocrlf=false`。

**接受的剩餘風險**：既不在 Azure 清單、也不在既有規則裡的全新名稱（例如說明文字裡臨時取的內部代號、外部服務帳號名稱），工具認不出來，仍靠第一道對這一版新增與修改文字的人工檢查，和過去一樣。

### 清單比對基準

`inventory_allow` 是逐一審過、可以公開的清單字；其他清單字出現在匯出裡一律標出。基準建立於 2026-10-13：

- 清單來源：以隔離的 az 登入唯讀讀取這個登入可見的全部 3 個訂用帳戶（含 dev 與 stage 的資源），共 608 個名稱（資源 539、資源群組 56、訂用帳戶 3、租戶網域 6、租戶名稱 4；Graph 已驗證網域可讀）；每個訂用帳戶的資源群組與資源都不是 0。
- 掃描對象：`main` `903f7809`（release 28，與公開 `v2026.10.13-28` 同一棵樹）的完整匯出，不用 `--previous`、以完整的字比對：1,361 個文字檔、黑名單 728 個字，標出 158 個不同的字。
- 審查結果：150 個加入 `inventory_allow`，各附理由：資源名稱裡的一般英文字或公開產品字 101 個、只在資源群組範圍內唯一的 FA 產品資源名稱 33 個（Container Apps、job、身分、Log Analytics、Application Insights、private endpoint、DNS 連結，依「保留與替換的資源名稱」保留）、Azure 平台與 privatelink 名稱 11 個、一般的測試／示範標籤 5 個。另外 8 個是 5 個 Azure Bot Service 名稱與其中 3 個數字片段：Bot Service 名稱全域唯一，和 Web PubSub 一樣改成替換（`review.json` 規則 `bot-*`）。補上規則與 `inventory_allow` 後，同一份清單對同一 commit 的匯出 0 個 HIT。
- **已公開的洩漏**：這 5 個 Bot Service 名稱已在公開的 `v2026.10.13-28` 裡（`v2026.10.12-27` 也有同樣的 9 處；更早的 `v2026.10.11-26` 未核對），5 個檔案共 9 處（`deploy/teams/personal.example.json:10`、`docs/progress.md:110`、`:166`、`:5438`、`docs/teams-chat-design.md:55`、`:62`（兩個名稱）、`infra/teams/personal-bot.bicep:6`、`src/fa-shared-context-poc/shared_context/teams_deployment.py:76`）。以新的 `mapping.json` 對公開 `v2026.10.13-28` 重跑第二道：[A]、[B] 相符，[C] 的 [2] 標出這 9 處而失敗。新規則從下一版起替換。**Neal 2026-10-13 決定：只修以後、不重寫公開歷史**；下一版（第 29 版）整棵取代，舊版 tag 與 Release 保留，不刪除也不改寫。理由：Bot Service 名稱是全球唯一的資源名稱，但不是秘密，要發訊息仍需 App ID 與憑證。

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

## 公開紀錄

依日期記下公開 repo 實際發生的事；各版的審查細節在對應的 release PR。

- **2026-10-11 release 24 第一次同步**：事後獨立檢查發現匯出留有作者環境的資源名稱、GitHub 擁有者數字 ID、私有 repo 的純文字名稱與一個私有 commit。公開 repo 當時是 private，已刪除後重建；替換規則與 verify 隨後補上。
- **release 25（10-11）**：第二道人工審查未過：M365 租戶名稱的縮寫（原規則只認全名）與 v18 環境的儲存體帳戶名稱還留在匯出裡。已用 `tenant-name-abbrev`、`storage-v18-stem` 規則修正，隨 release 26 同步。
- **release 26（10-11）**：第二道審查通過（內容樹 `b8654bda`）。Neal 核准把公開 repo 的歷史改寫成單一乾淨的 root commit `d6f6a976`（內容就是 release 26 的樹），並刪除 v25 的 tag 與 Release；腳本由 Neal 自己執行。
- **2026-10-12**：Neal 把 `fa-agent` 改成 **public**。
- **release 27（10-12）**：接在 `d6f6a976` 之後的 commit `a493fefc`，9 個檔案，都是文件。第二道審查通過：gitleaks 掃描目錄 143 個發現、掃描 git 歷史 144 個，全部對上已知誤判清單；183 個連結都能解析。
- **release 28（10-13）**：commit `ba9adcb6`（tree `7c07241d`）。
- **release 29（10-14）**：stage 第 29 版部署後的 public-sync；第二道 `gate2_check.py` 第一次自動執行，通過。公開 commit、tag 與 tree 在這裡未記，要引用請讀 public-sync 的 run 與公開 repo。
- **release 30（10-15）**：stage 第 30 版部署後的 public-sync（run 38666821029，成功）；第二道 `gate2_check.py` 自動通過：指紋相符、部署 commit 匯出的樹與公開樹相同、公開檔案 verify 通過。公開 tag `v2026.10.15-30`，commit `2725c646`，tree `ff9fef76`（讀自該 run 的 log）。
- **2026-10-13 第一次完整清單比對**：發現公開 `v2026.10.13-28` 裡有 5 個 Azure Bot Service 名稱（見「清單比對基準」），已加替換規則，從下一版起生效；已公開的歷史依 Neal 的決定保留（只修以後、不重寫），下一版整棵取代。
- **限制**：GitHub 可能仍保留沒有任何引用的 v25 commit 物件，要請 GitHub Support 清除才會消失；這是擁有者向 GitHub 提出的要求，agent 不代做。

## Verified against

- 程式基準：分支 `neallee2012-public-sync`（從 `origin/main` `3f6ad101` 開出，2026-10-10 與 `origin/main` `4bb59ac4` 比對無落後）；第二輪 review 修正後以 `8d345c6e` 重驗。
- 已讀程式：`scripts/public_export/scrub_export.py`、`verify_export.py`、`discover.py`；`.github/workflows/public-sync.yml`、`deploy-staging.yml`、`validate-infrastructure.yml`；`controller/progress_b1_b2.py`。
- 實際驗證：`tests/public_export` 離線測試 41 個、`tests/context_eval/test_progress_b1_b2.py` 17 個通過（臨時 git repo fixture，gitleaks 在行程邊界以替身取代，並核對 hermetic 旗標與環境）。本機對本分支 HEAD 的乾淨 detached checkout 跑 plain／scrub 匯出與 verify（真 gitleaks 8.30.1）：1,343 個檔案、沒有 `.github/workflows/`、改寫私有 repo 連結 548 處（Markdown 連結 494、網址 53、進度產生器的 `REPO` 常數 1）、匯出中的 GUID 全部屬於 placeholder（274 處）、`keep_guids`（41 處）或已審的 47 個假 GUID（256 處），結果 PASS。在 `tests/` 加一個未審的隨機 GUID、或加一個改寫規則抓不到的私有 repo 網址，verify 都以 1 結束。`8d345c6e` 重驗：gitleaks 144 個發現全部對上精確清單、結果 PASS；在未列入清單的 `tests/` 檔案放一個假 AWS key，或在已列入的檔案多放一個，`[1]` 都以 1 結束；把保留的連結文字 ``[`<SHA>`](私有網址)`` 裡的 SHA 改一位，`[3]` 以 1 結束。裸網址規則收緊前後，對同一 HEAD 的匯出逐檔比對沒有任何差異（54 處改寫結果相同）。
- 第三輪 review 後（本段所在 commit）：已知誤報必須有 `count` 且至少 1，少了就 `[1]` 失敗；替換規則用具名群組保留的文字仍做 hash 檢查；`tests/public_export` 43 個與 `tests/context_eval/test_progress_b1_b2.py` 17 個通過。乾淨 detached checkout 加真 gitleaks 8.30.1：1,343 個檔案、gitleaks 144 個發現全部對上清單、原值 287 種模式零殘留、hash 未改動、殘留掃描無未審項目，PASS；公開副本內 `test_progress_b1_b2.py` 16 個通過。
- 第一次同步後的補強（分支 `neallee2012-public-export-resource-names`，rebase 到含 #785 的 `origin/main`）：`tests/public_export` 81 個通過。乾淨匯出（HEAD 追蹤檔）加真 gitleaks 8.30.1：1,360 個檔案、gitleaks 144 個發現全部對上清單、原值 296 種模式零殘留、hash 未改動、殘留掃描 1,423 處無未審項目，PASS；匯出中作者環境名稱長短兩種寫法、擁有者數字 ID、私有 repo 純文字名稱、被引用的私有 commit、#785 的兩個 azd 後綴都是 0 處。公開副本另跑受影響的測試（`test_progress_b1_b2.py`、`test_context_target_eval.py`、`test_diagnostic_image.py`、`test_private_controller.py`、`test_vnet_template.py`、`test_private_collection.py`、`test_agent_setup.py`、`test_azd_hooks.py`、`test_auth.py`、`test_teams_deployment.py`、`test_stage_ci_deploy_feed.py`、`tests/public_export`）：314 個通過，失敗的兩個是上面記載的 workflow 測試與 ledger 指紋測試（後者在補強之前的匯出也失敗）。
- OIDC 擁有者與文件補充（分支 `neallee2012-public-export-followup`，從含 #786 的 `origin/main` 開出）：`tests/public_export` 83 個通過。HEAD 追蹤檔的 plain／scrub 匯出加真 gitleaks 8.30.1：1,361 個檔案、gitleaks 144 個發現全部對上清單、原值 297 種模式零殘留、hash 未改動、殘留掃描 1,421 處無未審項目，PASS；匯出中 `名稱@全 0 ID` 只剩 `example-owner` 與 `foundry-agents` 兩個 placeholder，擁有者名稱以 `名稱@` 形式 0 處。五條同理由的主機放行合併成一條前後，verify 對同一份匯出的輸出逐行相同。ledger 內容被替換的 run 是 4 個（`sha256` 與基準相同）。
- 第二道自動化與 Azure 清單比對（分支 `neallee2012-public-sync-gate2-auto`，從 `origin/main` `2c2ad24d` 開出，2026-10-13）：`tests/public_export` 116 個通過（新增 20 個；Azure 與 gitleaks 都用替身，含 clone 工作目錄是 CRLF 的情況）。真實重現：`fingerprint.py --rev 8417951c`（release 27）與 `--rev 903f7809`（release 28）算出的 tree SHA，與公開 commit `a493fefc`、`ba9adcb6` 的 tree（GitHub API 讀回）完全相同；`gate2_check.py` 對公開 tag `v2026.10.12-27`（用預設 `core.autocrlf` 的 Windows clone）與 `v2026.10.13-28` 加真 gitleaks 8.30.1：指紋相符、部署 commit 相符、verify PASS。`inventory_scan.py` 以隔離的 az 登入唯讀讀 3 個訂用帳戶（608 個名稱，Graph 網域可讀），對 `2c2ad24d` 的匯出：不加 `--previous` 有 161 個不同的字被標出（多半是資源名稱裡的一般英文字，另有一些已公開的產品與測試資源名稱）；加 `--previous`（公開 release 27）後 0 個。GitHub 上的新 workflow 步驟**未實際執行**，要等下一次 public-sync。上面「verify PASS」是當時的 `mapping.json`；加上 `bot-*` 規則後，同一指令的 [C] 會因已公開的 Bot Service 名稱失敗（見下一項）。
- 第二道自動化 review 第 1 輪修正（同分支，2026-10-13）：`tests/public_export` 139 個通過（Azure、gitleaks、GitHub API 都用替身；workflow 內嵌的 PR 說明選取程式直接執行測試）。清單比對改成完整的字、移除 `--previous`，清單為空或格式不對以 2 結束；完整比對與審查結果見「清單比對基準」。分支 HEAD 的 plain／scrub 匯出加真 gitleaks 8.30.1：1,367 個檔案、gitleaks 144 個發現全部對上清單、原值 305 種模式零殘留、hash 未改動、殘留掃描 1,423 處無未審項目，PASS；同一份匯出的清單比對 0 個 HIT。新的 GraphQL 查詢以 `gh` 唯讀對 `903f7809`（PR 817）核對過欄位；workflow 仍**未在 GitHub 上執行**。
- 推送前比對指紋（分支 `neallee2012-audit-deploy-fixes`，2026-10-14，Neal 核准修正稽核發現）：`tests/public_export` 147 個通過（Windows）；同一組部署測試在 Linux（WSL Ubuntu 24.04）404 個通過。新增的 `prepush_check.py` 以假的 release PR 說明與真的 `git write-tree` 測試，workflow 結構測試確認比對在 `git commit`／`git tag`／`git push` 之前。GitHub 上的新步驟**未實際執行**，要等下一次 public-sync。
- 「Commit, tag and push」這一步的 shell 另在本機以 bare repo 代替公開 repo 模擬：空 repo 首次推送、同內容重跑不推、同版號不同內容失敗、新版號接在既有 `main` 之後，皆符合預期。
- 證據與限制：GitHub 上的 workflow **未實際執行**（公開 repo、PAT、環境與變數都尚未設定）；權杖經 credential helper 傳遞、只開 Contents 的 PAT 推送、gitleaks 下載與 `gh release create` 只做過靜態檢查。
