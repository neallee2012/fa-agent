# CURRENT-STATE.md 工作法研究 — 來源 另一個私有 repo

> 本檔是**研究證據**，不是新規則；FA 的實際採用方式見 [CONVENTIONS](CONVENTIONS.md#8-current-state)。只萃取工作法，不搬入來源 repo 的人名、客戶、帳號、環境網域、成本數字或目前業務工作清單。

## 來源基準與範圍

- Repo：另一個私有 repo（private；以既有 GitHub 存取經 `gh` CLI 唯讀查詢）。
- 基準 commit：main HEAD **`c019b3052afce691207f4831491d56ac6c74f7de`**（本次研究擷取時的值）。檔案引用綁此 SHA；連結格式為 `[路徑:行號](blob URL#L..)`。Ruleset API 的觀察另屬擷取時狀態，不由 commit 固定。
- 只讀：`docs/status/CURRENT-STATE.md`、`CLAUDE.md`（repo 內無 `AGENTS.md`）、`docs/CONVENTIONS.md`、`docs/README.md`、`docs/status/` 目錄索引、及其直接指向的 `backlog.md`／`.claude/resume-manifest.md`／`.githooks/pre-push`／`.claude/settings.json`／`.claude/hooks/*`／`.claude/commands/merge-acg.md`／`Makefile`／`.github/workflows/ci.yml`／GitHub rulesets API。未 clone、未掃全 repo，未深讀 `docs/status/` 內具體業務進度檔（如個別 feature 追蹤檔）的內容。
- 歷史慣例僅取 `docs/status/CURRENT-STATE.md` 最近 10 筆修改 commit（GitHub Commits API，`path=docs/status/CURRENT-STATE.md&sha=main`），未擴及全 repo history。

## 一、CURRENT-STATE.md 本體：用途與寫法

**用途（文件自述）**：session 開場用的「現在在哪／上次做什麼／下一棒／待決」精簡 digest；文件本身宣告「紀律：本檔保持精簡（目標 ≤ ~60 行）」，並記錄自己「曾經過大 → 某次瘦身」的維護史（CURRENT-STATE.md:1-9）。`CLAUDE.md` 與 `docs/README.md` 都把它列為「新 session 只讀 2 份精簡檔」之一（另一份是根層協作指引）（CLAUDE.md:127-133、docs/README.md:13）；`.claude/resume-manifest.md` 重申同一順序（resume-manifest.md:11-13）。

**本次版面的寫法（觀察）**：五類段落——現在在哪／近期已 close／下一棒／待決 blocker／去哪找細節（CURRENT-STATE.md:12-73，五個標題分別在 L12/30/44/62/73）。末段是細節入口表；這是本次版本的結構，不是已找到 schema 或自動檢查的證明。

**觀察到的寫作習慣（非規則，僅實測，不可當強制規範照搬）**：
- 每條陳述傾向附「時間點＋確認方式／PR 或 commit 短 SHA」作實證錨點，而非只寫結論。
- 用固定 emoji 前綴做狀態速覽（完成／阻塞／部分／警訊／已清空），是視覺索引，非任何 schema 強制欄位。
- 舊結論被推翻時用刪除線＋一句話解釋「錯在哪」，不直接刪掉——把誤判本身當教訓留檔。
- 本次擷取實測：83 行、非空白 69 行、約 17,165 字元，已超出文件自訂「≤~60 行」目標——**這是觀察到的落差，沒有任何腳本回報或攔阻這個落差**。

## 二、更新時機與維護責任

**文件要求**：只找到「新 session 開場讀它」的規則（同上），**沒有找到任何「何時必須寫回 CURRENT-STATE.md」的顯式觸發規則**——對比之下，`features/<name>/current.md` 有紅線 2 的「code＋current.md＋history 三步驟同 commit」明文規則（CONVENTIONS.md:33-49）。`docs/README.md` 把 CURRENT-STATE.md 歸類「衍生視圖……改動後重生成」（docs/README.md:92-99），但「重生成」未定義觸發時點或負責角色。

**維護責任**：在已查的 repo 根目錄與 `.github/` 未見 CODEOWNERS，digest 也無 owner 欄位。`.claude/resume-manifest.md` 有 drift／待決策的最終決策角色（resume-manifest.md:8-9），但不能由此推定它同時指定了 digest 的日常維護者。**本次未找到明文日常維護責任；由變更負責者更新，是 FA 另定的移植規則。**

**觀察到的更新節奏**：抽樣近 10 筆修改本檔的 commit，主旨偏向功能／修復／文件描述，未見專門的定期刷新標題（GitHub Commits API 抽樣 SHA 前綴：`81b9348c`、`a2c919e7`、`d8e776f8` 等，約一個月）。這支持把更新納入交付的建議，**但提交主旨本身不足以證明每個功能 PR 都有同步、每筆同時改了產品程式，或不存在其他更新機制**。

## 三、與其他文件的權威分工

`docs/README.md` 明文列出一張「權威 vs 衍生」表（docs/README.md:92-99）：

| 類型 | 檔案 | 角色 |
|---|---|---|
| 現況權威 | `features/<name>/current.md` | program-first，需有 `## Verified against` |
| 規則權威 | `principles/` + `.claude/rules/` | 跨功能紅線 |
| 議題權威 | `ISSUES.md` / `FUTURE-SCOPE.md` | drift／待決策／加強項 |
| **衍生視圖** | `status/backlog.md` / **`CURRENT-STATE.md`** | 從權威源生成；改動後重生成 |
| 歷史 record | `implementation-plans/archive/`／`archive/`／`history/` | 設計脈絡，不反映現況 |

`backlog.md` 頁首也自稱「衍生排序快照（**非權威**）」，並明訂 source of truth 是另外兩份權威文件（backlog.md:1-6）。**核心分工原則**：現況摘要類文件永遠是「指標＋快照」，實質權威落在別的、範圍更窄的文件；讀者發現摘要與權威文件矛盾時，以權威文件為準。

**觀察到的分工裂縫（真實 drift 案例）**：`docs/README.md`、`docs/CONVENTIONS.md`、`.claude/resume-manifest.md` 三份文件指向 `docs/status/SESSION-HANDOVER.md` 作為跨 session handover 檔（docs/README.md:37,67-69、CONVENTIONS.md:104、resume-manifest.md:5-6），但在基準 SHA 下該檔經 Contents API 回應 **404 Not Found**。移植時不能假設文件描述的分工與所有連結仍有效。

## 四、逐項核實：是否有 scripts/hooks/CI 真正自動維護或檢查

| 機制 | 文件宣稱 | 實地核查結果 |
|---|---|---|
| git pre-push hook | 「其餘驗證由 PR required CI 承接」（CLAUDE.md:94-96） | 讀 `.githooks/pre-push` 原始碼：唯一動作是呼叫密鑰掃描（pre-push:2-4,87）。**未觸及** `docs/`、spec 同步或 CURRENT-STATE.md。 |
| CI `verify-docs` | Makefile 註解稱驗「英文 KB 同步＋ISSUE 編號＋DEPLOYMENT 章節唯一」（Makefile:429-432） | CI 內對應 step 實際只跑其中翻譯同步一項（ci.yml:118-120）。**三個子檢查皆不涉及 CURRENT-STATE.md 的內容、行數或新鮮度**。 |
| CONVENTIONS.md「pre-commit hook 自動拒絕」 | 紅線 2 違反時「pre-commit hook 自動拒絕只改 code 不改 spec 的 commit」（CONVENTIONS.md:49,283-287） | `.githooks/` 目錄下**沒有 `pre-commit` 檔**，只有 `pre-push`，且該 `pre-push` 只做密鑰掃描。**「文件要求」與「實際接線」不一致**的具體實例。 |
| Claude Code hooks（`settings.json`） | — | 目前唯一真正掛載的是 `Stop`→`self-validation-reminder.py`（settings.json:1-17），作用是掃描逐字稿關鍵詞、於「即將交付」情境提醒自我驗證，**與 CURRENT-STATE.md 無關**。`review-reminder.py`（PostToolUse）只在編輯 `docs/features/`／`docs/implementation-plans/`／`apps/` 時提示對應 review 指令，**未涵蓋 `docs/status/`**（review-reminder.py:34-36）。`run-tests.py`／`dep-scan.py` 存在於 `hooks/` 目錄但**未被 `settings.json` 引用**，且內含寫死的個人機器絕對路徑（run-tests.py:19），屬明顯陳舊未接線遺留腳本。 |
| 人工合併守門（`/merge-acg` slash command） | 檢查項含「三大紅線 spec 同步」 | 該 persona 文字**明文自承**：「CLAUDE.md 紅線第 2 條……**CI 沒有擋這個，只有這裡會擋**」（merge-acg.md:266-269）——spec 三步驟同步是**手動跑 slash command 時才檢查**，非自動 CI；且其檢查清單只掃 `docs/history/*.md`＋`docs/features/*/current.md`，**同樣不含 `docs/status/CURRENT-STATE.md` 或 `backlog.md`**。 |
| GitHub branch ruleset | 「真正的 merge enforcement 是 GitHub active ruleset」（CLAUDE.md:96） | API 查證確有 1 條 `active` ruleset（`main - required CI + PR only`，ruleset 頁面）——**真實接線**，但範圍是「PR-only＋required CI 通過」，屬程式碼 CI 檢查，非文件內容檢查。 |

**小結（限本次範圍）**：在已查的 repo hook／CI／Claude 設定及人工 slash-command 中，未找到針對 `CURRENT-STATE.md` 新鮮度、行數或內容的檢查。能確認的是文件文字紀律與開場閱讀慣例，不是所有使用者環境都沒有其他機制；本次未查個人設定，也未執行這些 hooks。人工 command 的規則、程式已接線與本次實際執行成功，仍是三件事。

## 五、適合移植的最小機制

1. 一份**固定段落結構、有自訂行數上限**的短 digest：段落＝現在在哪／近期完成／下一步／待決／去哪找細節；只指到權威文件，不重抄細節。
2. digest 在自己的 header 明講「非權威、目標行數上限、詳情去哪找」，讓單一文件自帶「怎麼讀我」的說明。
3. 用一張**總索引裡的「權威 vs 衍生」對照表**，交叉確認每份文件的角色，而非只靠單一文件單方宣稱（但仍需接受它會 drift，見上）。
4. **把更新納入功能／決策的同一組交付**；這是根據抽樣提出的 FA 維護建議，不是已證明來源 repo 每次都履行的規則。
5. 舊結論被推翻時**留痕跡（刪除線＋一句話解釋錯在哪）**，取代直接刪除，防止同類誤判重演。

## 六、不能照搬的前提

- **上游必須先有「權威文件」可供摘要引用**：對照表能成立的前提是已有等同「議題權威」「規則權威」「現況權威」的文件在被真的維護；若移植目標尚無對等文件，新 digest 會變成指向空氣的空殼。
- **人工 slash-command 守門依賴特定 GitHub PR 工作流與已客製的 command 檔**，且來源倉庫自己承認「CI 沒有擋，只有這裡擋」——複製這份人工 checklist 文字並不等於取得自動化；若移植目標沒有等價、被實際執行的人工複核步驟，這層保障不會自動存在。
- **`.claude/hooks` 的事件模型是 Claude Code 專屬機制**；已查的 repo 設定未引用 `run-tests.py`／`dep-scan.py`，不能只憑腳本存在就宣稱已生效；本次沒有查個人或其他層級設定，不外推它們永遠不會被呼叫。
- **具體行數／字數目標是該倉庫規模（大量 feature spec 與歷史文件）下的經驗值**，不是普遍常數；移植時應以自身文件量與可讀性重新定基準，不宜照抄行數。
- **「單一決策角色」假設有人持續在場、可即時裁決 drift**；若移植目標的批准模式不同，紅線「發現 drift 不自行修、等待決」的做法需要改寫成符合實際批准流程的等待對象，而非照抄角色名稱。

## 七、維護觸發與完成條件建議

- **觸發時機建議**：里程碑／驗收段落結束、或既有「現況權威」文件的關鍵結論改變時，於同一次交付內順便更新摘要；不建議另建獨立排程或阻擋性 CI 檢查——目前找不到任何來源倉庫真的靠自動化維護這類 digest 的先例。
- **完成條件建議**：更新後至少標注「本次更新依據」的來源（對應 commit／PR／權威文件段落），並留一句話標示摘要對應到哪個時間點或 HEAD，讓下個 session 能判斷資料是否可能已過期。
- **不建議做的事**：不要在任何規範文件宣稱「已有 hook／CI 檢查本檔」，除非真的接了對應腳本／workflow；若之後真的接線，才把此事實與真實路徑一併寫進文件，避免重演本研究發現的「文件宣稱與實際接線不一致」問題。

## FA 的採用範圍

新增 `docs/status/CURRENT-STATE.md`，與原 `progress.md` 的詳細歷史互補；文件角色表沿用 CONVENTIONS，不另外建立空的 backlog 或 handover 文件。AGENTS、README 與舊交接入口統一導向短摘要。

更新由變更負責者在狀態改變的同一組交付中完成。FA 另選 60 行及 UTF-8 8 KiB 的篇幅限制；沒有新排程、harness hook、生成器或 CI gate。這些是本次適配，不倒稱來源 repo 已有相同機制。
