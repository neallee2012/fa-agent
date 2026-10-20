# 環境分層（dev／stage）、staging 自動部署與遠端工作台

狀態：accepted。
- 方向由 Neal 2026-10-02 決定：
  - 16:57 提出四點需求（原文見「背景」）。
  - 17:01 補充：「這個測試網頁需要透過EntraID登入才能使用」。
  - 17:01 優先層：「和其他工作並行」。
  - 17:02：「好，請建議ADR」。
- 本 ADR 只決定方向。Neal 已明確批准的只有「在 `cog-fasample-memory-demo` 建立 stage project」。
- **其他 Azure 資源、RBAC、federated credential、Entra app registration，都等總協調整理清單後，由 Neal 另外批准。**
  - 清單 #415 已由 Neal 在 2026-10-02 17:26 批准（Q9 改成 stage 第一版就開 WebIQ）。
  - stage「＋」自助新增 Agent 的角色自動指派（新 IAM）已由 Neal 在 2026-10-03 00:12 於 #468 批准，見下方「6. stage 自助新增 Agent」。
  - account 層的 gpt-5.4-mini 容量調整（100→500）已由 Neal 在 2026-10-03 00:09 於 #464 批准，見「1. 環境分層」的共用部分限制。
  - **2026-10-04 10:58**：stage A、B 補 `Behavior.Agent` app role。Neal 原文：「同意，前提是有遵守我的的準則，"參考Grokbot的實作方式。儘可能地使用Foundry Hosted Agent的能力。……足以防止資料損失、重送、與明顯錯誤的最小機制……"」
    - 範圍：在 API app `fa-context-poc-api`（appId `00000000…`，SP `00000000…`）替 stage A（`00000000-0000-0000-0000-00000000001d`）、B（`00000000-0000-0000-0000-00000000001e`）各加一筆 `Behavior.Agent` app role。
    - 結果：總協調在 10:58:43、10:58:47 指派並讀回。之後 #599 改成設定檔 Agent 只核 oid（ADR-0006 門禁 C），因為受控身分的 token 會被平台快取，補了角色仍時好時壞。這兩筆角色保留，不刪。
  - **2026-10-04 11:22**：App Insights。Neal 原文：「可以把app insight擴充套件裝上去，Foundry project也可以啟用app insight」
    - 範圍：本機安裝 az `application-insights` 擴充；dev 新增 `log-fa-dev`、`appi-fa-dev`，以及 dev 專案連線 `dev-appinsights`。
    - 結果：部署 `dev-monitoring-20261004` 在 11:30 Succeeded，what-if 只新建這 3 個；#598 已把設定寫成程式碼。這是 dev 資源，屬於 Neal 這次的單次批准，不在 stage 的長期批准範圍內。
    - 詳細紀錄見 [progress 2026-10-04 stage 發布與完整測試](../progress.md#2026-10-04stage-發布-20261004-2-3-與完整測試4-項未過修理中)。
- **驗收：2026-10-02 22:40（台灣時間）三項都通過**，結果見「5. 追蹤方式」。
- **2026-10-04 晚間（#636 合併後）：stage 部署改為每次都要 Neal 事先核准。** Neal 原文：「後續如果要上staging，需要我的核准」。
  - 從這則起，每一次 stage 部署（改 `deploy/stage/release.json`，或手動觸發 deploy-staging）都要先取得 Neal 對該次部署的核准。下方「2. 部署規則」原本給 stage CI/CD 的長期批准不再適用。
  - CI 的觸發條件不變，`release.json` 仍是觸發開關；改的是「誰可以改 release.json」：只有在 Neal 核准該次部署後才改。（2026-10-05 起：總協調在 release PR 裡改 `release.json`，Neal 決定合併這個 PR，就是對該次部署的核准，不另外先核准。）
- **2026-10-05 07:5x：stage 發布改成「先在 dev 完整測試，再由 Neal 決定 release PR」。** Neal 原文：「後續的流程也是開發完成後先在Dev做完整測試，再開PR把當次的變更連同測試報告附在PR中讓我決定是否上prod」。
  - 目前沒有比 stage 更上層的環境，這裡的「prod」就是指 stage release。
  - 10-04「每次部署都要 Neal 事先核准」照舊；從這則起，固定照下方「2. 部署規則」的發布順序，由 Neal 決定是否合併當次的 release PR。
  - **08:16 Neal 澄清**，原文：「我剛剛說的pr是staging的上線審核的pr，其他的pr按照原來的流程」。**只有 stage release PR（改 `release.json`）**要附 dev 測試報告、由 Neal 決定是否合併；開發、修正和文件 PR 照 [ADR-0006](0006-team-trust-boundary-grokbot-mode.md) 的原本流程：兩軸 review 清零且 GitHub 顯示 CLEAN 後，由該工作階段自己合併，不需要 Neal 逐一核准，也不用附 dev 報告。

決策日期：2026-10-02。

基準：
- main `ff649632`
- dev 現況（2026-10-02 17:10 唯讀讀回）：
  - Subscription `00000000-…`、RG `rg-faexample`、westus3。
  - Foundry account `cog-fasample-memory-demo` 只有一個 project `faexample`（default，2026-09-13 建立）。
  - account 的模型部署：`gpt-4.1-mini`（GlobalStandard 500）、`gpt-5.4-mini`（100）、`text-embedding-3-small`（10）、`poc-kimi-k26-20260922`（1）。
- repo 目前沒有 `.github/workflows`；dev 由部署線（fdbb）手動部署。
- 官方文件查閱日期：2026-10-02。

## 背景

Neal 原話（2026-10-02 16:57）：

> 「我想要把現在的這個 faexample 這個foundry project定義為 development 環境，1. 請在這個foundry account建立一個新的project for stage env 2. 在GH repo中建立CI/CD Actions for staging deployment 3. 現在都是用8765在本地測試，我需要有一個可以從遠端連進去的測試網頁可以測試」

目前的狀況：
- 只有一個環境：`faexample`。每次部署都由部署線手動建 image、跑 `behavior_agent_versions sync`、切 API，並逐次取得批准（[runbook](../../src/fa-shared-context-poc/README.md#cap-06-pr4全自動-onboarding-apilocal未部署) 的 PR7 段落）。
- 測試只能用本機 8765 工作台，無法從遠端連入。

## 實作原則（沿用 ADR-0006，所有切片適用）

1. 原話：「請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。」
2. 原話：「「SDK-first／Hosted 優先」原則，在 FA 自己建一層之前，應該先確認官方功能跟最佳實踐，確認是否真的不足。」

## 決策

### 1. 環境分層

- **dev**：現有的 `faexample` project，維持部署線手動部署。
  - **2026-10-19（Neal 決定）**：dev 改成可用 `ci_deploy.py --config deploy/dev/dev.json` 部署，見 [Deploying dev](../../deploy/README.md#deploying-dev)；批准規則不變。
- **stage**：同一個 account（`cog-fasample-memory-demo`）下的新 project。
- **共用**：account、模型部署、quota、account 層設定。ACR 是否共用，由 stage 計畫決定。
- **分開**：project、Agent、State store、Conversations、Memory scope、API、Storage、identity、監控。
- 兩個環境的資料互不讀取。
- prod 尚未定義。Neal 2026-10-05 說的「上prod」指的是 stage release（見「2. 部署規則」）。

官方依據：Foundry 規劃指南把「一個 Foundry resource、多個 project」列為實驗與早期原型的同址做法，代價是事故、quota 用盡與設定錯誤的影響範圍共用；production 則建議預設隔離（[Foundry rollout 規劃](https://learn.microsoft.com/azure/foundry/concepts/planning)）。dev／stage 都不是 production，因此採同址；prod 定義時再依該指南評估是否另建 resource。

**共用部分的限制（防止明顯錯誤）：**
- account 層的變更（模型部署、容量、account 設定）會同時影響 dev 和 stage。
- 因此 stage 的 CI 不部署、也不修改 account 層資源；這類變更照 dev 規則另外批准。
- 具體例子：`infra/shared-context/foundry.bicep` 用 AVM 宣告整個 account，其中 `gpt-4.1-mini` 容量寫 10，而現場是 500。若 stage 重跑這份檔案，會把 dev 的容量改掉。stage 的 project 必須以 `existing` 參照 account，只建立 project 本身。
- 模型部署共用同一份 TPM，所以 stage 的 smoke 和 dev 的測試會互相占用額度。
- **2026-10-03 容量調整（#464）**：長對話時，單一請求就超過 gpt-5.4-mini 100k TPM 而回 429，即使隔再久重送也一樣（自動壓縮驗收 t3 第 19 回合）。Neal 00:09 批准（原文：「批准調容量」），部署線把 gpt-5.4-mini（GlobalStandard）capacity 從 100 調到 500，00:15 讀回 `provisioningState=Succeeded`，模型版本和其他設定沒動。這是 account 層寫入，同時影響 dev 和 stage；調容量本身不收費，仍照實際用掉的 token 計費。上方「基準」的 100 是 10-02 17:10 的讀回，不改寫。

### 2. 部署規則

- **dev**：照現行規則，每次部署前都要批准。
- **stage**：由 CI/CD 部署。~~屬於 Neal 的長期批准~~（2026-10-02 起，到 2026-10-04 晚間為止）。
  - **2026-10-05 起的發布順序**（Neal 07:5x 原文見上方「狀態」；目前沒有比 stage 更上層的環境，「prod」就是指 stage release）：
    1. 開發 PR 合併到 main。
    2. 在 dev 完整測試：包含這次要發布的所有變更。dev 部署照上面 dev 的規則逐次批准。~~以及退回上一個 stage 版本的演練~~（2026-10-06 起不做，見下方「失敗就停」那一條）。
    3. 總協調開 release PR（改 `deploy/stage/release.json`），PR 內附變更清單和 dev 測試報告。~~和退版方式~~（2026-10-06 起不附）。
    4. 由 Neal 決定是否合併；合併才部署 stage。
    - **只有這種 stage release PR** 要附 dev 測試報告、由 Neal 決定是否合併（Neal 08:16 澄清）。開發、修正和文件 PR 照原本流程（兩軸 review 清零且 GitHub 顯示 CLEAN 後，由該工作階段自己合併），不需要 Neal 逐一核准，也不用附 dev 報告；這些 PR 合併不會部署 stage。
    - 注意：CI 部署的是合併當下 main 的完整內容。dev 測試之後 main 若又合併了其他程式變更，那些變更不在這次的 dev 測試報告裡。
  - **2026-10-04 晚間起，每一次部署都要 Neal 事先核准**（Neal 原文：「後續如果要上staging，需要我的核准」）：改 `deploy/stage/release.json` 或手動觸發 deploy-staging 之前，先取得 Neal 對該次部署的核准。CI 的觸發條件不變。2026-10-05 起，改 `release.json` 一律走上面的發布順序：Neal 決定合併 release PR，就是對該次部署的核准。
  - ~~merge 到 main 就部署~~（2026-10-02 起，到 2026-10-03 為止）。
  - **2026-10-03 起改為「發布版本才部署」**。Neal 23:12 原文：「可否改成不要每一次merge to main都佈署stage，而是每一個milestone or version佈署一次」。
    - 發布方式：開一個小 PR，只改 `deploy/stage/release.json` 的 `version` 和 `milestone`。合併後，CI 部署當下 main 的完整內容。（2026-10-05 起由總協調在 dev 完整測試後開，PR 內附的內容見上面的發布順序。）
    - 其他 merge 都不會觸發部署。另外保留 `workflow_dispatch`，只能在 main 上手動觸發，同樣部署 main HEAD。
    - 2026-10-16 起 deploy-staging 在 Azure 登入前核對：部署的 commit 必須是 release PR（改了 `deploy/stage/release.json`、帶第一道指紋）的合併 commit（手動觸發要填那個 PR）；main 在 release PR 之後又前進，就要開新的 release PR。見 [stage 部署說明](../../src/fa-shared-context-poc/README.md)「release 門檻」。
    - 2026-10-16 記錄：「誰合併 release PR」目前沒有、也無法由系統強制。repo 是免費方案的 private repo，branch protection 與 rulesets 的 API 都回 403；repo 沒有 CODEOWNERS；agent 工作階段也用 `neallee2012` 合併，所以檢查 `mergedBy` 永遠會通過，門檻因此不加這項檢查。合併 release PR 由 Neal 決定，目前靠流程遵守。真正強制需要兩件事：agent 改用獨立的 GitHub App 身分合併，以及升級 Pro／Team 方案，在 `staging` environment 設必要審核人。
    - 不用 tag 觸發：federated credential 只信任 main 上的 `staging` environment，改用 tag 就得改雲端 IAM。
  - 範圍只限 stage 資源，包括每次部署的最小 smoke 模型費用。
  - ~~這項長期批准在 stage 資源與 CI 身分依上方「狀態」另外批准、建立之後才生效；建立前不自動部署。~~（2026-10-04 晚間起不再有長期批准，見上面 stage 的「2026-10-04 晚間起」那一條。）
- 同 build 規則照舊：API 與 Hosted image 由同一個 commit 建置（SdkStartGate 比對 `code_sha256`）。
- 失敗就停、往前修，不自動回退。~~2026-10-05 起，dev 測試要演練退回上一個 stage 版本，release PR 寫明退版方式~~；**2026-10-06 起不再做退版演練，release PR 也不附退版方式**（Neal 10:12 原文：「後續不需要再做回滾的部分了」）。CI 本身仍不自動回退。
- CI 用 OIDC 加 federated credential，綁 GitHub Environment `staging`，不存長期密鑰；RBAC 只給到 stage 資源（[GitHub Actions 以 OIDC 登入 Azure](https://learn.microsoft.com/azure/developer/github/connect-from-azure-openid-connect)）。

### 3. 遠端工作台

- 必須用 Entra ID 登入才能使用，不能有匿名 fallback。
- 網頁仍是薄 client。權限照舊由 API 核對 Entra 身分和 oid→Team，網頁不成為授權來源（AGENTS 架構界線第 4、9 條）。
- 「只送一次」的紀錄改存在伺服器端，依使用者分開。
- 本機 8765 繼續可用。
- 能進入的使用者範圍，待遠端網頁的計畫決定。

### 4. 優先層

和其他工作並行，不搶占主軸（ADR-0006＝ADR-0007＝多 Agent 協同合作的優先順序不變）。

### 5. 追蹤方式

- 不另建 target state 或帳本。
- 驗收以三項為準：
  1. stage 部署成功。
  2. merge 後自動部署到 stage（2026-10-03 起觸發條件改為發布版本，見「2. 部署規則」）。
  3. 遠端用 Entra 登入後，A、B 各跑一回合通過。
- **驗收結果（2026-10-02，台灣時間，三項都通過）**：
  1. stage 資源 W1～W12 依 #415 建立並讀回；全新環境初始化（#433）回條 `a936ae64…`；stage API 啟動。
  2. #449 在 21:53 合併後，run 37016099084 自動部署成功：A／B／C／D 建了同一個 build 的 v3，API 與遠端網頁換成新 revision。之後 #451、#454 合併也都自動部署成功。
  3. Neal 在遠端網頁用 Entra 登入（畫面顯示「已登入：Microsoft Administrator」，登入後自動連線）：
     - 22:38：用 fa-stage-a 開 Google Maps 台北市大安區並截圖，網頁即時桌面看得到瀏覽器畫面。
     - 22:40：用 fa-stage-b 送「Hi」，B 回覆「Hi! How can I help?」。
- 進度寫在 [CURRENT-STATE](../status/CURRENT-STATE.md)。

### 6. stage 自助新增 Agent（#468）

- Neal 2026-10-03 00:12 在 #468 批准，原文：「同意：同意上述新 IAM，也同意「＋」自動寫入。並且記錄一個代辦事項，說明風險並後續需要設計新的方案。」
- **做法**：stage 的「＋」新增 Agent 後，程式自動補 endpoint 協定，並為新 Agent 的身分新增 #306 的兩筆角色指派（Foundry User、Cognitive Services OpenAI User）。
  - endpoint 協定用 API 身分既有的 Foundry User 補上，不需要新權限。
  - 角色指派需要新 IAM：stage API UAMI 取得受限的 **Role Based Access Control Administrator**。範圍只限 stage project；ABAC 規定只能新增、只能指派這 2 個角色、只能給 ServicePrincipal。細節與剩餘風險見 [ADR-0006 第 1 節已知例外](0006-team-trust-boundary-grokbot-mode.md)。
- **順序**：
  1. c7bb 完成程式（預設關閉，兩軸審查）。
  2. b281 先唯讀核對，再指派 IAM 並讀回。
  3. 用 repo 的 stage 設定打開開關。
  4. 3ef6 讓遠端網頁也能用「＋」。
  5. 請 Neal 實測一次。
- **生效延遲**：官方說明 managed identity 的角色變更可能要數小時才生效，剛建好的 Agent 可能先顯示「準備中」，之後自動補好。
- **待辦**：風險和後續新方案記在 #469，不在現階段範圍。
- dev 不受影響：dev 新增 Agent 的角色仍由部署線手動指派，每次照 dev 規則批准。

## 取代與影響

**AGENTS.md**
- 「批准、費用與 Git」段補一句：~~staging 的自動部署依本 ADR 已長期批准~~；2026-10-04 晚間起改為「staging 部署每次都需 Neal 核准」；2026-10-05 起再補上發布順序（dev 完整測試→release PR 附變更清單和 dev 測試報告→Neal 決定是否合併；2026-10-06 起不做退版演練、不附退版方式），並寫明只限 stage release PR，其他 PR 兩軸 review 清零後由該工作階段自己合併；同節的「不直接推 `main` 或自行 merge」相應改成「PR 只依本節第一段的規則合併」；dev 與其他環境照舊。
- 架構界線第 9 條補：遠端工作台依本 ADR 必須用 Entra 登入。

**不變的部分**
- dev 的部署方式、資料與批准界線不變；本 ADR 不授權任何 dev 資源變更。
- `deploy/shared-context/azure.yaml` 寫死 dev 的 endpoint 與 image，是歷史配置；stage 不直接沿用。
- 同 build、換版接續（#369）、新 Agent 的角色與 endpoint 補設（#306）等部署規則，在 stage 同樣適用。#468 之後，stage 的角色與 endpoint 補設改由「＋」自動完成（見「6. stage 自助新增 Agent」）；dev 照舊手動。（已被 2026-10-15 修訂取代：Neal 把 #468 延伸到 dev 和 azd 預設，見 [ADR-0006](0006-team-trust-boundary-grokbot-mode.md) 該段修訂。）

**相關工作線**
- stage project、資源與 CI/CD：「Stage 環境與 CI/CD」工作線（b281）。
- 遠端工作台：「遠端測試網頁」工作線（3ef6）。
- 兩條線的計畫都先交總協調，由 Neal 一次批准清單上的資源與權限。

## 官方依據

- Foundry rollout 規劃（隔離邊界、同址與隔離拓樸）：https://learn.microsoft.com/azure/foundry/concepts/planning
- 建立 Foundry project：https://learn.microsoft.com/azure/foundry/how-to/create-projects
- Hosted agents 概念（版本不可變、agent identity、project managed identity）：https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents
- Foundry RBAC 角色：https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry
- GitHub Actions 以 OIDC 登入 Azure：https://learn.microsoft.com/azure/developer/github/connect-from-azure-openid-connect
- User-assigned managed identity 的 federated credential：https://learn.microsoft.com/entra/workload-id/workload-identity-federation-create-trust-user-assigned-managed-identity
- GitHub Environments：https://docs.github.com/actions/deployment/targeting-different-environments/using-environments-for-deployment
- Container Apps 內建 Entra 驗證：https://learn.microsoft.com/azure/container-apps/authentication-entra
