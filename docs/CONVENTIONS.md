# CONVENTIONS — 開發、規格與證據維護

適用於 `src`、`controller`、`scripts`、`tests`、`infra`、`deploy` 與相關文件。
協作入口及架構界線見 [AGENTS.md](../AGENTS.md)；本文件說明如何把需求、程式、部署與實際結果對齊，不新增一套產品架構或批准系統。

## 1. 每種資訊只有一個主要來源

| 資訊 | 主要來源與用途 |
|---|---|
| 產品需求與用語 | [requirements.md](requirements.md) 與 [CONTEXT.md](../CONTEXT.md)；需求編號保持穩定 |
| 架構決策與取代關係 | [ADR](adr/)；沿既有編號記錄背景、取捨、範圍及哪些舊決策被取代 |
| 產品契約與待決事項 | [specification.md](specification.md)；行為細節以它指向的 [行為控制規格](FA-AGENT-BEHAVIOR-SPEC.md) 為準 |
| Context 目標與特定保存契約 | [Target State](grok-aligned-target-state.md)、[CTX-03 契約](ctx03-checkpoint-contract.md)；完整目標清單不是完成清單 |
| 實際程式行為與執行入口 | 目前工作樹的程式、測試、requirements、Dockerfile、部署配置及對應 README |
| 開工摘要與接續導覽（衍生） | [CURRENT-STATE](status/CURRENT-STATE.md)；只摘要有來源的現在／近期完成／下一步／待核對事項，不取代規格、結果或批准 |
| 詳細進度、部署與交接紀錄 | [progress.md](progress.md) 與對應階段報告；保留日期、提交／WIP／部署基準及實際結果，按需讀取，不作每次開場必讀全文 |
| 測試計畫與歷史結果 | [POC 計畫](POC-ANALYSIS-AND-PLAN.md)、各階段報告／JSON、[POC-RESULTS.md](../POC-RESULTS.md) |
| 官方能力與研究依據 | 對應研究文件及官方文件／固定版本原始碼；支持平台能力判讀，不替代 FA 的實測 |

產品文件的權威關係依 specification 的「文件權威順序」。同一份文件內，明確說明取代範圍的新修訂優先於被取代的段落；日期較新不代表整份文件覆蓋所有舊契約。

程式是「目前實際怎麼做」的依據，已確認需求是「應該怎麼做」的依據，實測只證明該版本及情境的結果。三者不一致時，不能只把其中一份改到看似一致。

沿用本 repo 的文件分工；確有獨立維護需求時才新增文件並補入口連結，不先建空的文件樹。引用索引、腳本、Make targets 或 hooks 前先確認它們實際存在，流程說明不能冒充已接線的自動檢查。

## 2. 標準工作流程

1. **固定範圍與基準。** 核對本次 diff、相關需求／修訂、實際入口與 consumer、既有測試及套件配置。完成條件是能指出要改哪條路徑、保持哪些行為，以及哪些部分尚未接線；不從舊 worktree 或摘要猜現況。
2. **先確認會改變的語意。** 新需求、重大介面／權限／生命週期變更或相互矛盾的要求，先用使用者場景釐清。已明確授權的修補直接執行；新設計標為提案，不先寫成「目前已實作」。
3. **沿既有接點實作。** 優先使用固定版本 SDK、平台能力與共用 helper。保留舊資料的可讀性及原操作結果；新增格式或狀態必須對齊實際 writer、reader、API、Hosted 與 UI consumer，不只新增型別或測試替身。
4. **跑直接涵蓋改動的驗證。** 使用既有 runner，先選測項再執行。結果須能辨別原問題與修正後行為；涉及真實平台的主張另需該平台證據，不能拿離線通過代替。
5. **同步文件並交付。** 更新下節列出的必要文件，明示完成、未完成及限制；若接續狀態改變，再依第 8 節刷新 CURRENT-STATE。修改保存到工作樹；commit、push、PR 或部署依 AGENTS 的批准界線，不因流程走完自動執行。

### 發現規格與程式不同

先確認是否有已批准的新修訂或明確的歷史標示。若本次要求已明確指定修正方向，修正並同步相關文件；若應採哪個行為仍不清楚，在 [progress.md](progress.md) 記下規格段落、程式位置、差異及影響，於 CURRENT-STATE 留一句待核對事項及來源連結，暫停受影響部分並詢問。無關工作不必全部停下，也不趁機重寫歷史。

只有在相同條件下於基準版本重現，才稱失敗為「既有問題」；無法做比較時標「歸因未驗證」。

## 3. 哪些文件要一起更新

| 本次改動 | 同步範圍 |
|---|---|
| 已確認需求或架構方向 | 對應 requirements／ADR／規格；progress 只記決策影響與接續入口，不複製完整契約 |
| API、SDK、模型／工具路由、工作或對話生命週期 | 對應行為規格及 [Shared Context README](../src/fa-shared-context-poc/README.md) 的相關段落 |
| Context、codec、checkpoint、記憶取用 | 對應 Target State 項目或 CTX-03 契約、實作 README；若有新結果，更新對應評測紀錄而非整份目標清單改成完成 |
| 工作台欄位、文案、可操作條件或請求流程 | [工作台 README](../src/fa-shared-context-poc/web_test_console/README.md)；涉及後端語意時同步該契約 |
| 部署、身份、網路、image、啟動方式 | 對應部署說明／runbook；新部署結果另記 progress，配置檔存在不等於已部署 |
| 測試案例、fixture、checker 或判定方式 | 對應測試／評測說明；保留舊批次及舊規則的結果，不用新 checker 回填歷史 PASS |
| 純文字修正、導航或本協作指引 | 只更新相關文件；不要求新增程式、ADR、history 或重跑雲端 |

「同步」指同一組交付變更／PR 能完整說明行為與影響，不是強迫每次建立三份新文件。可獨立成立的 rename、機械整理、行為修改及測試保持可逐步追查的提交；相依變更不可留下無法運作或誤導的中間狀態。

## 4. 本機驗證

先從相關 README、測試及 manifest 確認 runner、工作目錄與 interpreter。重用該工作樹既有環境；只有相依清單變更或實際缺少套件／工具時才安裝，不為文件工作安裝 SDK 或執行 Azure setup。

Windows 程序明確使用 UTF-8；Python 子程序也要繼承設定：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
```

以下是已存在入口的用法範例，**只選與本次 diff 相關的一個或數個測項，不是每次全跑的 gate**。`python` 應使用所選環境的 interpreter。

| 範圍 | 入口範例 |
|---|---|
| Python API／Hosted／checkpoint | `python -X utf8 -m pytest .\tests\shared_context\test_sdk_checkpoint.py -q`；將檔案換成受影響測項，可合併 selectors |
| 指定 A/B controller 的離線契約 | `python -X utf8 -m unittest discover -s .\tests\shared_controller -p "test_*.py"` |
| 工作台前端行為 | `node --test .\tests\shared_context\test_web_test_console_workbench.cjs` |
| 工作台真人路徑 | [`verify-workbench`](../.claude/skills/verify-workbench/SKILL.md) 的 standin 層；適用範圍見本節末 |
| 文件 | 檢查新增／變更連結、路徑、敘述與 diff；若沒有相關文件測試，不新增工具或跑產品完整套件 |

Shared Context 的 Windows 暫存與長路徑方式見其 README「本機命令」；暫存目錄必須是本次專用、可清理的目錄，不指向使用者資料。Controller 的 fake runner 與原生雲端入口分別見 [controller README](../tests/shared_controller/README.md)。

真 SDK＋離線模型 transport 只支持 SDK 接線／恢復的觀察，不是真模型品質或 Hosted／Blob 驗收。涉及套件邊界時，依實際 API／Hosted image 的相依集合檢查；整套開發環境可匯入，不足以證明精簡映像可啟動。

UI／效能驗證使用獨立 loopback port、測試對話與 journal，不污染使用者工作台、草稿或正式對話。詳細隔離與收尾方式沿用工作台 README。

工作台驗收分兩層（Neal 2026-10-30）：

- **開發 PR**：改到 `web_test_console`、bridge，或工作台會走的 API 路由時，附 standin 證據。其他 PR 不跑。
- **stage release PR**：dev 測試報告要有 dev 層結果，每位助理各跑一輪真回合，逐位列出。某位助理不通過就寫不通過，不用另一位的結果代替。

standin 用假 Hosted，看不到真登入、真工具與真模型的問題，兩層的證據不能互換。

## 5. 文件中的「已實作」與「已驗證」

### Program First

新增或實質更新描述目前實作的文件時，先讀對應程式，並加入或更新 `## Verified against`，列出真正核過的位置與證據。規劃文件改用「提案／已確認但未實作」標示；不要求把所有既有文件一次搬成新格式。

CURRENT-STATE 是衍生摘要，以第 8 節的核對基準及逐項來源連結記錄依據，不在摘要重抄完整 `Verified against`、測試輸出或歷史。

可採以下最小格式，沒有做的事直接寫未執行，不用補做無關驗證：

```markdown
## Verified against

- 程式基準：<commit；若有未提交差異，另列範圍>
- 已讀程式：<repo 相對路徑與函式／class，必要時附行號>
- 實際驗證：<命令、環境、測項及結果；或未執行與原因>
- 證據與限制：<去敏紀錄／結果連結，以及沒有涵蓋的部分>
```

官方能力主張另附官方來源、適用版本及查閱日期。最新文件與固定 runtime 不符時，核對該版本原始碼或最小重現；文件支持、程式接線與真實執行分別寫清楚。

### 結果不互相代替

- 沿用對應報告／JSON 的原始狀態值；`FAIL`、`BLOCKED`、`NOT_RUN`、`UNKNOWN`、`incomplete` 不因後續修正變成原批次的 PASS。
- 新一輪結果另記版本、輸入、範圍及觀察時間，連回舊結果。只有部分測項通過就說部分通過；目標、候選、test-only 與正式採用保持區別。
- 外層命令 exit code、HTTP／ARM 狀態、內層操作與實際成果分開判讀。Timeout 表示當時未取得終局，不等於完成、未執行或可以重送。
- 品質與效能比較固定可比較的任務、模型、版本、資源及品質條件；縮短 prompt、暖啟動或單次成功不能證明整體 10 倍提升。
- 缺少原件就明示僅引用 repo 紀錄；不為補一個報告數字而自動重跑模型、Memory、部署或 Hosted 工作。

這些是報告應說清楚的事，不是要求每次部署建立額外證據平台或完整歷史證明。必要阻擋須能指出具體資料損失、重送或執行錯誤風險。

### 證據與私密資料

原始 evidence、私有 session、對話、SDK logs、checkpoint 與登入資料留在受控且不進 Git 的位置。版本化文件只放必要去敏摘要、可分享來源及 hash；`.gitignore` 的例外清單不代表資料已審核可公開。

不要把真 token、key、cookie、簽章 URL 或 private reasoning 放進範例、fixture、log 或提交，即使只是 dev／POC。公開畫面與報告只呈現安全的操作進度及正式提供的公開摘要。

## 6. Azure、部署與生產差異

先確認正在處理哪個 service、環境與批准範圍。Repository 根目錄的 `azure.yaml` 自 2026-10-10 起是**新環境**的 `azd up` 入口（[設計](azd-new-environment.md)），只部署到 azd environment 指定的訂閱，不用於 dev／stage；dev／stage 照原本流程與批准。

執行 Azure 工具前沿 [runbook](POC-RUNBOOK.md) 的專案隔離 `AZURE_CONFIG_DIR`；每個新 process 重新設定，`az` 資源命令明確指定批准的 subscription。使用 `azd` 前另讀 Foundry skill 的 azd guidance，核對 environment 與 service；不從舊報告複製實際身份、endpoint 或版本。

修改 Bicep 前查適用的 Azure Verified Module 與 WAF-aligned example；採用時固定版本，不採用時說明原因及 contract 差異。部署前核實本次變更的必要前提，不為沿用範本擴建基礎設施。

POC 設定與正式產品需求的差距，集中記在 [specification.md](specification.md) 的「尚待決策」對應項目：**差異、影響、正式環境需要什麼、待誰決定及相關文件**。已有項目就更新，不另開重複清單。具體且已批准的操作步驟才放入對應 runbook／部署 README，並連回該決策。

取得 PR 發布批准後，涉及部署的 PR 說明附 `Deployment notes`，指出影響、必要操作、資料相容性及是否已部署；不因 merge、image build 或 ARM `Succeeded` 就宣稱功能已生效。

計費操作、重試及收尾沿本次有效批准與原工作紀錄。舊 controller 的停止策略只適用其指定測試 sessions，不套到需保留的正常對話；停止 compute 也不表示保留的儲存或網路已停止計費。驗收或診斷結束後，只處理自己且在授權範圍內的程序／資源。

## 7. POC 與命名

本 repo 的 POC 可以只驗一個能力，不必先建完整產品；但身份、資料保護、操作授權、防重送及誠實結果不能省略。既有 `src` 不因名稱含 `poc` 就免除這些規則，也不要求把可用的 POC 程式全部重寫才可正式接線。

只針對新的實驗保存必要的問題、版本、結果與限制；沿對應階段報告或 progress 記錄，不為每次探針建立新文件樹。正式採用依實際契約、接線與必要測項決定，不靠搬目錄或改標題。

保留既有 ADR、FR、B、CTX、M 系列識別及檔名，新增文件沿所在目錄慣例，不做無關 rename。日期使用 `YYYY-MM-DD`，時間包含時區；實際欄位值、hash 與序列化 bytes 不為美化文件而改寫。

## 8. CURRENT-STATE

### 用途與讀取

[CURRENT-STATE](status/CURRENT-STATE.md) 是新工作階段的**非權威開工摘要**。先讀 AGENTS 與它，再沿任務連結讀細節；不是把所有規格、progress 或 POC 報告再抄成一份。

本文採用來源 repo 的短 digest／按需讀取模式，另補明確維護責任與觸發；來源查證及未採用的自動化假設見 [工作法研究](current-state-workflow-research.md)。此處是 FA 的維護規範，不宣稱來源 repo 已有同樣的更新檢查。

| 部分 | 保留內容 |
|---|---|
| 頁首 | 非權威聲明、更新日期、最後核對時間／程式基準、未納入的 WIP 或環境範圍 |
| 現在在哪 | 當前接續焦點與最重要的能力界線，不重抄產品介紹 |
| 近期完成 | 最多三項仍影響下一步的結果，每項附來源；不列完整完成清單 |
| 下一步 | 最多三個具體接續點，已有承接者時標明工作線／角色；不自行指派另一工作階段或推定新授權 |
| 待決／待核對 | 真正需要決策、解除阻塞或補證據的事項，以及去哪核對 |
| 去哪找細節 | 可用的規格、實作、結果與歷史入口；不連不存在的檔案 |

FA 摘要控制在 **60 行、UTF-8 8 KiB 以內**，兩項都要滿足；這是可核對的篇幅限制，不冒充精確 token 數。超出時把細節放回對應文件，只留結論與連結；不靠壓成超長單行掩蓋膨脹。

### 誰更新、何時更新

變更負責者維護自己影響的摘要項目；同一組交付有整合者時，由整合者彙整。平行工作先讀最新版本再合併各自差異，不整份覆蓋別人的接續點，也不把另一分支尚未交付的工作寫成已合併。

以下任何一項改變，就在同一組交付／PR、工作交接或階段收尾前更新：

- 當前焦點、已完成／部分完成／阻塞狀態，或下一步與承接者改變。
- 關鍵需求／決策／操作範圍有新確認，或舊結論被新證據取代。
- 原先列出的程式基準、實際部署或驗收狀態改變；「程式已合併」與「已部署」分開。

未改變接續資訊的文案、格式或機械整理，不強迫更新摘要。新工作階段也不只改日期假裝刷新；來源未核對時保留原核對時間，明示待核對。更新日期是編修日期，不是全部環境已重新驗證的日期。

### 更新順序與完成條件

1. 先更新真正擁有該資訊的規格、決策、實作說明或結果紀錄；遇到未定的差異，先保留差異，不讓摘要裁決產品行為。
2. 以來源的新結論替換摘要對應項目，補來源與適用基準。更正原因、舊 FAIL／UNKNOWN、完整用量及操作歷史保留在 progress／原報告；摘要只留必要提醒，不累積整頁刪除線。
3. 核對每項狀態、來源連結、篇幅與接續點；未執行、本機通過、雲端通過及未納入的平行工作仍須分清。提交／推送批准另依 AGENTS，不為同步摘要自動 commit。

`progress.md` 保持詳細事件與歷史紀錄，CURRENT-STATE 可以隨現況重寫；兩者是互補，不搬移或清空既有歷史。根目錄 README 是給新讀者的產品概述，完整文件索引在 [docs/README](README.md)；英文版 `README.en.md` 以繁中 README 為準，兩份在同一個 PR 一起修改；[FA-POC-Handoff](FA-POC-Handoff.md) 保留特定舊批次的交接材料，不再是通用開工入口。

本次移植採**文件規範＋交付時維護**，沒有新增定時任務、harness hook、自動生成器或 CI gate。若日後真的加入檢查，須連到已接線的實作並說明它能檢查什麼；檔案存在、日期或行數通過，都不能證明內容仍正確。
