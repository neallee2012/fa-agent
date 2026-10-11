# FA Context：評測起步與兩輪乾淨對話 baseline

日期：2026-09-24。此文件發布評測設計、集合結構及驗收結論；原始 traces、private snapshots、review UI 資料與私人聊天不隨 Git 提供。原件識別見[證據索引](context-research-evidence.md#evaluation-baseline)。

**後續自主實作追蹤：** 使用者已要求完成整份 Target State 的實作，進度與新 evaluation 另記於[持續狀態帳冊](context-implementation-status.md)。下方原批次結論保持不變，不因新工作開工而重新評分。

**結論：兩輪人工 review 均完成並獲使用者批次接受；這不是 76 項完整 target 驗收、逐筆語意 gold 或一般產品成功率。** Round 2 的客觀結果仍保留原 8 pass／1 strict mismatch，不因人類接受整批而改寫判準。

## 1. 為什麼先稽核既有 evaluator

FA 已有 M4 固定合成資料、oracle、格式／scope／citation／批准／容量／生命週期檢查，以及歷史錯誤分析。評測不是從零開始，也不應先製造一個未校準的「整體 helpfulness 1–5 分」LLM judge。

`controller/m4_context_eval.py` 的 `source_terms` 以答案 alias 在來源中的字詞命中作檢查。這是可保留的狹義契約，不能被改稱通用語意支持。

六個離線 checker probes 的結論如下。它們是刻意構造的 checker 輸入，**不是六段 agent trace**：

| Probe 類型 | 預期 | Checker 結果 |
|---|---|---|
| 正常固定 fixture | 通過 | 通過 |
| 來源明確否定答案指定的 owner | 不支持答案 | 仍通過 |
| 關鍵字屬於另一個專案 | 不支持答案 | 仍通過 |
| Citation 不存在 | 拒絕 | 拒絕 |
| 對應 policy decision 已拒絕 | 拒絕 | 拒絕 |
| 舊 M4 測項 input tokens 超過 8,192 | 拒絕 | 拒絕 |

兩個反例證明字詞命中不能判斷否定與主體關係；**不能把 2/6 當產品錯誤率**。原 M4 結果另有人工來源核對，不由此倒改。M4 的固定 token／bytes 門檻也不是後續自由指令入口的通用限制。

相關既有資產：fixtures、checker tests、[M4 歷史](M4-CONTEXT-POC.md)。

## 2. 評測的固定做法

先看少量可核對的完整公開內容投影，由人自由註記，再決定是否有穩定的 failure mode 值得轉成 evaluator。不得為了湊樣本數重跑已完成工作，也不將 expectations 當模型輸入。

| 原則 | 本次做法 |
|---|---|
| 原件／版本固定 | 保存 exact 合成輸入、公開答案、來源 hash、API／Hosted 各自版本及缺件 |
| 來源方單一執行 | 沿唯一執行方的隔離工作台／journal，不複製同一批工作或 counter |
| 人工 review 與 AI 建議分開 | 初始 human labels 為空；自由文字與 span 註記不預填 taxonomy／gold |
| 「已閱」不是 Pass | 只記使用者已閱與批次接受；沒有逐筆判讀就不生成逐筆語意標籤 |
| 層次分開 | backend succeeded、native complete、durable reply、preview、工具來源與答案品質分開 |
| 缺件不補造 | 無 provider wire、私有 reasoning 或 browser paint，就明記未捕獲，不以回憶補錄 |

這兩個小集合不是隨機代表性採樣；沒有足夠資料估計一般 failure rate、宣稱分類飽和或校準語意 judge。

## Round 1

第一輪使用既有驗證原件，沒有為 intake 新增模型呼叫：**3 個 user turns，來自同一個乾淨合成 conversation**，不是三個獨立對話。

| 回合目的 | 可支持的限定觀察 |
|---|---|
| 一句中文能力說明 | 一般短答可檢閱 |
| 簡單算術且只答數字 | 當輪格式限制可檢閱 |
| 明確撤掉上輪格式後換題 | 有明示作用域轉換的情境可檢閱，不外推所有含糊換題 |

原 LATENCY 測試對話沒有混入，私人真人招呼也不放進集合。恢復正常招呼是經批准清理測試聊天後另開乾淨對話，不是換 system prompt、Memory 或 Hosted 版本。

每筆有原輸入／公開答案的來源識別與 durable reply 核對；當次工具事件投影未觀察到工具呼叫，因此沒有虛造 tool pairs 或 safe sources。Preview、provider wire、token usage、browser paint／TTFT 未捕獲。

**人工收尾：** 3/3 已閱，0 則自由文字註記，0 個 failure patterns；使用者接受本批。沒有自動建立三筆品質 Pass。

## Round 2

第二輪先固定 `cases.json` 與獨立 `expectations.json`，另獲批准後才執行。共 **6 個新 conversations、12 個原 user requests 各一次、13 次實際 model requests**。

| 對照 | 設計目的 |
|---|---|
| 本輪數字格式／整段對話 JSON 規則 | 區分一次性格式與持續規則 |
| 原事實未改／明確更正後回想 | 區分保留與更新 |
| 同對話回想／新對話未知 | 區分歷史連續性與隔離 |

六個 conversation／Hosted SID／SDK SID 各自不同；同 case 內維持三種身分。Fresh case 在 source case 完成後才新起，沒有把其他 case 的歷史放入輸入。未使用主工作台、舊 LATENCY 或第一輪 conversation。

多出的一次 model request 在 fresh isolation case：模型→`read_context`→模型。實際工具回傳是既有合成 metrics，不是聊天歷史；只有這一筆有真實開始／完成配對，其餘十一個 turn 的當輪 archive 未見工具呼叫。**User turn 不等於 model request。**

### 客觀條件與人審分開

| 項目 | 結果 |
|---|---|
| 執行／保存 | 六個 case 完成；blocked=0、not_run=0、批次 unknown=0，不代表語意全對 |
| 預先固定客觀 checks | 8 pass／1 strict mismatch |
| 保留的 strict mismatch | 實際 `收到。` 不等於預期 `收到`；沒有事後放寬 |
| 人工已閱 | 12/12 |
| 自由文字註記／failure patterns | 0／0 |
| 批次接受 | 使用者接受；沒有逐筆 gold 或個別 tool-relevance 標籤 |

來源與用量原件核對後，13 筆 ModelCost 都是 `stage=usage`：input tokens 共 3,772、output 274；retail estimate 為 1,953 microUSD（USD0.001953）。這是**零售估算，不是 Azure 帳單或共用 counter 差值**。原 accounting-only、pending／legacy liabilities 沒有被歸零。

每個 case 依既有保存／archive／stop 契約收尾，六個 `session_stop` confirmed；平台 session 及檔案保留。獨立 review 程序停止，原件保留，不修改主工作台。

### 仍缺什麼

沒有完整 provider HTTP 封包、私人 system／reasoning、browser paint 或真正 provider TTFT。Preview 的 first／last 時間是 API 觀察時間，不能當瀏覽器首字。

本批涵蓋明確設計的格式期限、事實更正、隔離對照，未涵蓋完整 Memory 沉澱、runtime skills 或長對話 compaction。人類接受也不會將已記錄的 strict mismatch 改成未發生。

## 3. 與 CTX-03 的關係

兩輪人審結案後，沒有繼續追加同類短對話湊數，而是另批准 CTX-03 的本機保存／恢復切片。

9/24 的 default-positive 是**固定 SDK／runtime 的離線保存與恢復接線實驗**：一次 manual compaction、兩個 fresh-process followups，實際 model-facing history 中有原來源、摘要、更正及先前答案。它不是新一輪真人語意 baseline，也不應加入上述十二筆的人審結果或當成 LLM 摘要品質 Pass。

目前一般 filtered 大狀態可恢復，候選 B 的 default 路徑有本機可行性；正式 validator 仍拒絕候選，敏感 custom 指令反例仍 blocked。詳見[CTX-03 裁決](ctx03-local-acceptance.md)。

## 4. 後續評測的採用順序

先定義產品真正採用的保存／恢復契約，再沿該路徑取得相應證據；不以 test-only candidate 取代產品驗收。若後續要評真模型摘要品質，需另有合法樣本與適用批准，保留真正來源／更正／失敗模式，不能只檢查摘要裡是否有關鍵字。

完整目標仍是 [76 個條目、12 個情境](grok-aligned-target-state.md)。程式可判的身分／版本／hash／效果契約優先用程式；需要語意判讀的部分先人審，標準穩定後才考慮撰寫與校準 LLM judge。
