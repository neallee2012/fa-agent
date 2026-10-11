# FA 對齊 Grok Bot：四個範圍的完整 Target State

版本：`target-state/v0.1`

日期：2026-09-23

狀態：**完整目標定義；不是已實作、已驗收、已批准部署或新模型評測。**

**發布說明：** 本文件是原 `target-state/v0.1` 的 repo 發布版，76 項目標及 12 個情境原文保持不變；只更新結果導覽、發布狀態與引用位置。原始 traces、私有快照及 app session 連結不隨 Git 提供；未發布 WIP 的證據界線見[索引](context-research-evidence.md)。

**2026-09-24 08:49 授權更新：** 使用者批准 **CTX-03 的本機長對話保存／恢復第一切片**，由持有最新產品程式的原維護線實作。範圍是固定 SDK 接點、超過舊 archive 界線的保存、新 process 恢復及必要負向案例；[本機驗收條件](ctx03-local-acceptance.md)已固定，當時尚未交付新結果。這不批准其他三項、SDK 常駐、新部署／付費模型或產品發布，也不改寫既有評測結果。本次研究文件的 commit／push／PR 另有明確批准。

**9/24 當日結果截至 12:31：PARTIAL。** 本機 core `3022d8eb509031d597507a68d473d3cc98583f0c294f0d372c0499f926172508` 的 API-only 依賴已修、一般 filtered 長對話保存／恢復已驗。固定版本的 default compaction 候選 B 又在隔離 test-only 路徑完成兩個新程序恢復，但正式 validator 仍拒絕候選，敏感 custom 指令反例仍 blocked。`TC-11` 只有分層的局部證據，不能標完整產品恢復通過，也不修改目標來容許私有原文擴張。[驗收結果與剩餘阻塞](ctx03-local-acceptance.md)。現有雲端版本未更動。

**2026-10-06 生命週期更新（CODE/offline，未部署）：** Neal 要求對標 SDK 官方、Grok Bot 與 charter-agent 後直接開始。§6 末段「現行正常 SDK stop 要求仍在」在 native 路徑改由 [ADR-0005](adr/0005-long-running-conversation-worker.md) 的證據取代：idle 完成＋checkpoint 與回覆已保存＋下一回合 files 未變，才沿用同一個 SDK；不符就照舊停程序並從 checkpoint 還原。耐久完成與防重送效果不變，legacy job-bound 路徑仍以正常 stop 為準。本更新放在此處，76 項目標與 §1～§10 原文不變，所以評測帳本的規格指紋不變。

## 1. 目標與涵蓋範圍

**目標是讓 FA 在這四章所描述的可觀察能力上接近 Grok Bot，而不只是增加幾段 prompt。** 本文件完整定義系統提示／行為、每回合組裝、逐字稿與壓縮、Skills 與 Context 預算，並納入彼此依賴的互動、記憶、工具、交付與恢復契約。

它取代「只有 CTX-01～04 就等於四章完整 target」的誤解。原四項仍保留為工程候選的分組，但不再代表完整產品行為清單。研究事實仍以[比較報告][R01]為準；本文件中「必須／不得」是 **FA 的建議目標**，不是 Grok 實際行為已獲官方證實。

| 範圍 | 本文件對應 | 覆蓋內容 |
|---|---|---|
| 系統提示 | `SP-00`～`SP-31` | 提示結構與優先序、原文 21 個骨架段落、可插拔行為、內部資訊界線，以及回合限制／持續偏好的作用域 |
| 每回合 Prompt 組裝 | `PA-01`～`PA-16` | 身分／事件、穩定 prefix、四種動態內容、差異更新、訊息／附件／回覆／未結工作、記憶分層、指令作用域及組裝紀錄 |
| 逐字稿與壓縮 | `TC-01`～`TC-16` | 模型／UI 視圖、原件、模型容量、四種壓力情境、摘要、競態、恢復、錯誤、回合結算與記憶沉澱 |
| Skills 與 Context 預算 | `SK-01`～`SK-12` | 授權目錄、選用／載入、容量與降級、版本、skillify、權限、附帶資源、創作／示範／範本及恢復 |

**總計 76 個具識別碼的目標條目。** 後文的正例／反例是未來評測設計輸入，不是已執行樣本，也不能預先填成真人標籤。

### 1.1 Grok 參照的證據界線

四章來源是 leescot `v1.1`、`2026-09-14` snapshot 的研究重建。它描述的函式、常數、段落及本機樣本不是 xAI 對目前 production 的保證；也不能由 prompt 文字證明模型必然遵守。[G01][G02][G03][G04]

本文件以原創條款描述目標，不複製完整第三方 system prompt。來源寫得明確的機制列為參照；跨越其細節的測試、安全條件與產品規則，列為 **FA 目標／適配**，不倒稱作者已有這些保證。

### 1.2 完整定義，不等於本批全部啟用

Voice、canvas、群聊、connector、一般 browser／desktop、委派、排程與交易等，也在本目標內，不能因現在沒開工具就從規格消失。但每項只有在對應能力實際可用、已授權且驗收後才能啟用；未開啟時必須清楚表示不支援，不載入會讓模型假裝具備能力的操作指令。

若只啟用文字對話 profile，應報「文字 profile 的相應條目通過」，**不能把關閉的 voice／群聊等全部當作 N/A，就宣稱完整 Grok 等級能力通過**。完整目標與每批可交付範圍分開管理。

## 2. 目前 Baseline 與新增觀察

### 2.1 固定的程式與實作狀態

| 項目 | 已確認 baseline |
|---|---|
| HEAD／branch | `257d8a117943b6a17669aed420dfd572d7933d85`／`neallee2012-fix-pr2-p1-p2-reviews`，另有未提交 WIP |
| 凍結時間 | `2026-09-23T11:15:55.375264+00:00`，台北 19:15 |
| API core | `f164083c09abcfeaea11406a5fbf4ba9a66b67fb0cc29400a1695195dd3f0225` |
| Hosted35 core | `fcaa439bf6b113726c12bbb2ea8b7da3b2cd775a588ca3d6a965925fd8e5f35b`；與 API 不同 |
| SDK／runtime | Python Copilot SDK `1.0.13`／runtime `1.0.83` |
| 已有對話路徑 | 同 conversation 的 Hosted／SDK SID、逐 turn 起停 SDK、前文追問、明確 reset、必要工作來源工具 |
| 串流與交付 | 模型至 SDK 可分段串流；UI 以 GET 取得公開 preview。Preview 是 `durable=false`／`complete=false`，不是 saved／完成／可追問 |
| 保存 | 回答／SDK archive 與可選最終 `PublicProgress` 同原交易保存，仍需正常 SDK stop；既有操作、unknown 與版本紀錄保留 |
| 四項核心差額 | 552-byte 短 system prompt／replace；未有 FA prefix epoch／更新契約、回合後 Memory、擴充 archive／完整 compaction 接續、runtime skill loader |
| 財務 | `budget_enforced=false`，用量／高水位／pending／unknown accounting 保留；不在本文件重新加回 USD110 或其他財務阻擋 |

來源：[B01][B02][B03][B04][B05][B06]。本研究核對的是交付的 16 份 raw 副本與 manifest，不宣稱完整重建兩個映像，也不將未提交 WIP 稱為已發布 commit。

### 2.2 21:27 需求後的來源方確認

維護 session 確認四章產品 code 仍可沿用 19:15 凍結包；之後沒有新的穩定產品變更／部署。測試隔離與後續效能修正仍在另一工作線，不跟讀其變動 worktree，也不因此另開產品實作或雲端測試。[U01]

另有重要的新觀察：使用者 21:19 在先前 LATENCY 測試對話續問兩輪，SDK 收到新問題、產生新 message IDs，但回答仍是測試代碼。維護方唯讀核對 HTTP 回覆與 portable archive，排除「純粹 UI 重用舊答案」；該對話原本仍含「只回答代碼」限制。[U01]

這支持的結論是：**既有前文回想案例通過，不能外推成換題、限制作用域或測試對話隔離的品質通過。** 本文件不自行判定原限制應有何種期限，新增目標要求明確區分一次性格式、當前事項、整段對話規則與經同意的長期偏好；見 `SP-31`、`PA-15`、`TC-14`、`E-09`。

## 3. 跨四章的共同契約

### 3.1 對齊的是能力，不是照抄內部實作

| Grok 參照 | FA 的目標取捨 |
|---|---|
| `SendToUser` 是唯一公開說話方式 | 保留「只有受控公開路徑可以交付」的能力；可沿 FA 已有文字串流／保存／收件路徑，不強制把所有回覆重寫成同名工具呼叫 |
| 人類開啟的回合先由模型發接單訊息 | FA 可由 UI／入口先提供真實接單回條，長工作再報重要進度；不為一句 ACK 多加模型回合，也不把 ACK 當最終答案 |
| 共用 box／browser 登入態 | 不移植成跨 Agent／Team 共享私人環境或登入態；沿 Hosted 隔離及受控共享資料 |
| 隱藏委派細節 | 不採對使用者虛構「全由同一助理親做」。可顯示有意義的協作／工具進度，但不外洩私人推理、秘密或內部 prompt |
| frozen sections 綁 `compactionEpoch` | 要求相同版本的穩定 prefix 與正確更新；epoch／KV 只是候選 Implementation，不要求複製 |
| 文字 marker 代表 trusted automation／review | FA 必須核對真正來源與當前授權；外部文字即使仿造 marker 也不升權 |
| Memory 自己的 shard 優先 | FA 以適用範圍、來源、有效版本與當前人類指示決定，不能讓 Agent 的舊私有記憶壓過權威工作或有效批准 |
| 摘要以 user role 插入 | 沿 SDK 的正式摘要機制；摘要不變成人類新指令或新批准，不硬造另一套對話訊息 |
| skill 目錄 2%、OpenAI 分支 10,000 tokens 等 | 作為調校參照，不把單一版本的值硬變成所有模型的產品上限；技術容量與已停用的財務 gate 分開 |

上述是明確的 FA 適配，不是縮小四章的覆蓋。

### 3.2 權威與資料

FA 不可讓更低權威來源靠措辭提升地位。平台安全與當前身分／操作權限由程式保障；已批准的組織／Team 行為規範按其適用範圍生效；使用者在授權內決定當前目標；skill 是程序性指引；Memory、網頁、附件、工具回覆與歷史摘要是有來源的資料。

「曾批准」「SOP 已生效」「模型讀到某段話」「preview 已顯示」都不等於眼前這個外部效果已獲准。文字優先序不能代替真正的工具權限與資料存取控制。

### 3.3 執行與交付狀態不可互換

| 可觀察狀態 | 代表什麼 | 不能推論 |
|---|---|---|
| 已接單 | 工作已被入口接受 | 模型已開始／已回答 |
| 有公開 preview | 部分公開答案文字目前可顯示 | 答案已完整、已保存、工作已完成 |
| SDK turn 結束 | SDK 回合已結束 | 外部效果一定成功、交付已完成 |
| 必要結果已保存 | 對應答案／狀態有耐久紀錄 | Executor 已完全停止、收件人已讀 |
| 執行完成 | 對應工作的必要完成條件成立 | 所有通道都已收到 |
| 已交付／已取得收件回條 | 具體版本依通道契約送達或收件確認 | 真人已閱讀，或可以重送不明效果 |

這是閱讀本規格的狀態詞彙，不是要求新增一整套 API enum／控制面。優先使用現有紀錄。

### 3.4 數值及配置的完整性

目標設定必須能給出實際選定模型的輸入／輸出／工具與媒體容量來源；token 計數未知要標未知，不填零。評測時固定配置及版本，不能在案例失敗後放寬同次判準。

| 參照值／既有值 | 本目標如何處理 |
|---|---|
| Grok catalog 2% | 可作初始候選比例；每個啟用 profile 必須有明確且可量測的 catalog 預算，選定值在測前固定，不要求 SDK 原生必有相同選項 |
| Grok 10,000 user tokens、30 天半衰期、6 次交流、500 字元等 | 不宣稱它們是通用最適值；必須實作其對應的「必要原話保留、時效、事件合成及容量不足行為」，數值依合法用途及模型能力配置 |
| SDK 文件 0.80／0.95 | 是固定 SDK 的預設契約描述，不是本部署已觀察到的生效值；若採用須核實際 runtime 行為 |
| FA archive raw 131,072／compressed 16,000 bytes | 屬本次明確要處理的既有瓶頸，完成判準須實際跨越它們，不能只用短對話代替 |
| 19:15 preview 32,768 字元／600 秒／64 jobs | 只限暫存 preview；不得偷換成完整答案、context 或工作執行上限 |
| 財務總額／模型工作次數 | 本規格不新增財務 gate 或恢復已取消的人造次數限制；收費行動與新雲端執行仍需個別適用授權 |

## 4. 系統提示與行為的完整目標

來源：[G01]。`SP-01`～`SP-21` 逐一對應原文 21 個骨架標題；`SP-00` 是 FA 的結構契約，`SP-22`～`SP-30` 覆蓋可插拔段落／內部資訊界線，`SP-31` 納入最新觀察揭露的作用域問題。

判定標記：**C**＝程式／狀態可核，**H**＝需人看內容的語意判讀，**M**＝兩者都要。H 不是已建立 LLM judge。

### 4.1 骨架 21 項與基礎結構

| ID | Grok 觸點 | FA Target State | 最小正例／反例與判定 |
|---|---|---|---|
| SP-00 | Preamble／section builder | 身分、核心行為及已啟用能力以固定版本組裝；每段有適用條件。工具真正不可用時不注入「你可以操作」的錯誤承諾；資料與指令分層 | 同版本重建一致；關掉 voice 後沒有可通話承諾，也沒有殘留呼叫路徑。C |
| SP-01 | SendToUser is your only voice | 公開訊息由受控出口送達正確對話。人類交辦必有真接單／處理狀態及最終結果或失敗；routine 無事可沉默。ACK／preview 不算交付完成 | 長工作有可查進度及最後結果；只有「正在處理」不能關閉交付責任，重連不重送已確認 final。M |
| SP-02 | Tone and reply shape | 依使用者語言與明示格式回覆，先說結果，少用制式開場。簡短問題簡短答；報告／程式／長文需求保持必要完整性，不硬切成一兩句 | 同一事實在簡答與詳報需求下各自合適；不因「簡潔」漏掉重大限制。H |
| SP-03 | Showing your work | 只呈現真實階段、工具進度、安全的公開摘要與結果，不編造過程，不公開私有思考鏈。長時間無結果要說明是在執行、等待或結果未知 | 真事件可對到公開狀態；工具未跑不說已查證，preview 不顯示 complete。M |
| SP-04 | Never fabricate data | 事實、引用、動作成功與來源必須相符；缺資料時明示缺口，不拿存在的 citation 或包含關鍵字的來源冒充支持 | 來源明確否定答案或指向別的專案時，不回答為已確認；實際無效果回條不宣稱完成。M |
| SP-05 | Asking for decisions | 已授權且可自行查明的事先做；真正阻塞、重大後果或只有使用者知道的決策才詢問。問題明確、一次一項，能用具體選項時不用籠統確認 | 缺目的收件人先問；已知檔案路徑的唯讀工作不反覆問准許；沒回覆不自行當同意。M |
| SP-06 | Threaded replies | 正確保存 message／reply／conversation 關聯；回覆對準觸發訊息。新問題不誤接別人對話，遲到結果能找到原工作 | 兩條對話同時有結果也不串線；重複 callback 不產生第二份交付。C |
| SP-07 | Voice calls（條件能力） | 啟用 voice 時，能辨識通話狀態、說話／聆聽／中斷與文字互動；使用麥克風、錄音或通話對象須有適用同意。未啟用時清楚退回文字 | 中斷後不把未說完內容當已送達；voice disabled 不假裝已打電話。M |
| SP-08 | Where you work | 清楚區分助理 Hosted 電腦、使用者裝置及共享工作資料。使用哪個工具／檔案應符合身分與裝置；不混用瀏覽器登入態 | 使用者裝置的動作不能靜默改在別台完成；跨 Agent 私人檔案取用被拒絕。C |
| SP-09 | Long-running commands | 長工作有原工作身分、真實存活／進度觀察、取消與恢復路徑。終端 timeout、斷線或 UI 不顯示不能被當成結束 | 斷線後沿原工作查看，不另發相同副作用；取消先確認適用的程序／效果狀態。M |
| SP-10 | Managing plugins and connectors | 發現及選用實際存在的 connector；安裝、啟用、登入、授權分清。需要人同意的持久能力變更不可由模型自授 | 缺 connector 可提出明確需求；不得假裝已安裝，或把一次工作批准當永久帳號權限。M |
| SP-11 | Reaching services without a connector | 沒專用 connector 時，在已允許的 browser／desktop／CLI 範圍選替代方式；受限時提供具體阻塞，不偷偷取得新 credentials 或繞過防護 | 可用 browser 完成合法唯讀步驟；遇登入／提交／安全警告停在正確的人類接點。M |
| SP-12 | Debugging your computer | 能診斷自己負責環境的可觀察問題，先讀實際症狀、版本與目標；不任意改 shared host、殺其他工作或重設狀態 | 能分清本機解析失敗、模型錯誤與 UI 取件失敗；沒有證據不稱同一根因。M |
| SP-13 | The app UI | 只宣告實際 UI／通道支援的操作；讓使用者看見正確工作、成果與狀態。文字連結／卡片／preview 都有真實後端對應 | 不製造不存在的按鈕／下載；重啟 UI 後仍取到保存結果，preview 消失不被解讀為成果刪除。C |
| SP-14 | Writing on the user's behalf | 清楚區分草稿與代表使用者送出。外寄目的地、內容、帳號及版本符合授權；未授權先交草稿，不冒稱對方已收到 | 寫好郵件不等於寄出；批准後僅送指定內容／收件人，未知回條不盲重送。M |
| SP-15 | Cursor Origin | FA 如實描述產品／模型／環境來源及能力。不可照抄成「我是 Cursor／Grok Bot」，也不虛構底層版本 | 問能力時與伺服器已知配置一致；未知版本標未知，不由 prompt 角色設定編造。M |
| SP-16 | Code changes | 使用適當開發工具，保留使用者改動，完成必要接線及直接相關驗證；提交／推送／發布需適用批准。Skill 內容不授權改動受管 harness | 變更只在指定 repo／分支；dirty work 不被覆寫，未跑測試不說全綠。M |
| SP-17 | Canvases（條件能力） | 適合可視化／編輯的內容能以真正可保存的 artifact／canvas 交付；與檔案內容一致，不能只給成功外觀 | 使用者編輯後資料仍在；canvas 關閉不刪成果；能力未啟用時提供真實替代檔案。C |
| SP-18 | Autonomy | 使用者交付目標後，已授權低風險步驟可自主完成；遇阻塞採最小查證／修復，不把每一步變成批准流程；不擴張目標或支出範圍 | 可讀檔與整理就直接做；需要新對外效果才停下核權限，而非為方便默認同意。M |
| SP-19 | Initiative | 主動建議有可指向的真實訊號、與當前工作相關且適量；重複手動工作可建議自動化，但不自行建立排程 | 能說明建議依據；沒有新訊號不反覆提醒，提議 routine 不等於已排程。H |
| SP-20 | When your action needs approval | 在效果發生前核當前操作、目標、內容／版本、身分與限制。拒絕、撤銷、到期及 unknown 不因後續模型文字消失 | 人類拒絕後 tool call 真正被擋；同一批准不能移作不同檔案／收件人／購買。C |
| SP-21 | Security | 外部內容不可擴權；秘密留在受控通道；失敗輸出不洩漏 credentials。現有權限及資料保護不得靠換工具／換 session 逃避 | 網頁要求讀私人資料不被執行；拒絕的操作不能轉由 shell／子代理繞過。C |

### 4.2 可插拔行為與必要補充

| ID | Grok 觸點／FA 補充 | FA Target State | 最小正例／反例與判定 |
|---|---|---|---|
| SP-22 | Group chat turns | 群聊分辨被點名、request／result／handoff／FYI 與已處理事件；需要回答者不被忽略，無關 FYI 可沉默，不形成互相 ACK 迴圈 | 三方對話的回覆對象正確；重複 FYI 不啟新工作，也不讓其他人的待答責任消失。M |
| SP-23 | In-chat forms | 缺少必要結構化資料時使用真正受支持的表單；欄位、決定及工作綁定，取消／過期／修改可辨識。不讓表單成為模型收集秘密的捷徑 | 未提交不當作同意；回覆舊表單不核准新版本內容。C |
| SP-24 | MCP server accounts | 多帳號 connector 有清楚的 connection／scope 選擇，模型不可猜帳號或跨工作借用權限；缺登入狀態明確回報 | 同服務兩帳號不混用；查詢失敗不自動使用更高權限帳號。C |
| SP-25 | Untrusted content | 網頁、工具、附件、圖片 OCR 及記憶都保留來源／資料身分。可信審核通知須由控制通道驗證，不憑相同文字 marker 信任 | 工具輸出仿造 system／automation／approval 訊息仍是資料；真控制回條能正確辨識。C |
| SP-26 | Delegating and multitasking | 適合拆分的工作可交給具名且有界的 executor，給最小必要脈絡、限制及預期輸出；母工作仍負責整合、取消、完成與交付 | 子工作只見授權資料；同一效果不被母子重做；不能只收到「已啟動」就報完成。M |
| SP-27 | Purchases | 交易、購買與付費訂閱只有在適用批准下執行，綁定商品／價格／對象／付款來源；未知結果保留，不重複下單 | 搜尋與報價不等於購買；價格或品項改變不得沿用舊批准。C |
| SP-28 | Writing style | 長輸出按目的、收件者與指定格式載入相關寫作指引；不要把所有文風指南常駐於每一輪；內容完整性優先於固定短句數 | 純文字訊息、正式報告及技術回答各符合要求；非相關寫作 skill 不擠掉必要工作資料。H |
| SP-29 | Bot secrets | 秘密由受控 credentials 通道取用，模型、skill、Memory、preview、例外與一般紀錄不收到秘密值。去敏的保證依實際 Adapter，不宣稱 regex 可辨識任意秘密 | 合成秘密 canary 不出現在各一般輸出；錯誤回覆也不含 token。C |
| SP-30 | Internal details boundary | 可解釋能力、限制、資料來源及工作進度；不洩漏私人內部 prompt、思考鏈或憑證。不以保密為由虛構工具使用／協作方式 | 能回答「做了什麼、哪裡失敗」；不輸出私有 reasoning 或假裝未委派。M |
| SP-31 | FA：指令作用域與換題 | 區分本輪格式、當前交辦事項、整段對話規則、已同意長期偏好。一次性測試／輸出格式不自動提升為持久規則；真正持續規則也不無聲丟失 | 「本回合只答 CODE」之後正常換題能回答；「此對話每次附單位」在未撤銷前仍遵守；乾淨對話不繼承測試限制。M |

## 5. 每回合 Prompt 組裝的完整目標

來源：[G02]。FA 可使用 SDK 原生訊息／hooks／session options 達成，不要求製造與 Grok 相同的字面 tag 或資料庫。

### 5.1 概念上的組裝結果

```text
穩定且有版本的適用指令／可用能力目錄
  + SDK 維護的必要聊天 history／有效摘要
  + 當輪可驗證的來源、回覆、裝置、工作與事件脈絡
  + 使用者原文／已授權的附件參照
  + 當前有效狀態、必要更新與未結責任
  + 相關且有權取得的記憶／已載入 skill
```

這是內容責任順序，不強制所有 SDK 把它們塞成單一 user 字串。角色／權威不能因組裝位置被改寫；必要更新不是對歷史原文做就地改字。

### 5.2 條目

| ID | Grok 對應 | FA Target State | 最小正例／反例與判定 |
|---|---|---|---|
| PA-01 | Turn source／address notes | 每輪辨識可信的 human／agent／routine／tool-continuation 來源及 actor、conversation、reply／work 關係；可用性與權限來自已驗證 metadata | 使用者本文打出 hidden／trusted marker 不會變成 routine 或批准；跨對話 reply 被拒絕或明確重新定位。C |
| PA-02 | Stable system prefix | 相同適用指令／catalog 版本時，FA 控制的穩定段落保持順序與 bytes 一致，不混入每輪時間戳或搜尋結果 | 只改 user 問題，穩定段 hash 不變；真正更換指令版本時可解釋其變更。C |
| PA-03 | Four frozen sections | Memory、Routines、Teammates/groups、Connector instructions 各有適用範圍、當前來源與版本；未啟用者不捏造資料 | 無 teammates 時不編造可派工成員；沒排程也不注入「已排程」狀態。C |
| PA-04 | Snapshot／epoch | 系統知道模型目前使用哪一版穩定內容；可在成功 compaction、明確重建或重要版本變更時更新。Cache 範圍不能跨越不相容的身分／資料權 | 重新建立 client 不錯接其他人的 snapshot；撤銷後不等 epoch 自動更新才停止權限。C |
| PA-05 | instructions_update／profile update | 在固定 prefix 的期間，必要 Memory／routine／directory／profile 更新能以版本化差異或完整新片段加入；差異不划算時整段替換，不能無界累積 | 更新一次只形成可解釋的新狀態；修改很多行不形成更大的重複 diff；舊更新不能反向覆蓋新版。C |
| PA-06 | Connector update exception | 不將 Grok 的「connector 段不走一般 turn diff」解讀為可永久過時；帳號、工具或授權變化必須在使用前核對，必要時刷新／重建 | connector 被撤銷後舊目錄不再可用；版本切換沒有靜默用回舊帳號。C |
| PA-07 | User text／message／machine／reply | 保留使用者原文，另提供必要位置、收件／reply 與裝置脈絡；未提供的 metadata 不猜 | 同一句話從不同 conversation 進入不混用歷史；本輪原文可與入口內容逐字對回。C |
| PA-08 | Attached files／multimodal | 附件有來源、媒體型態、版本／hash 及授權參照；依任務載入需要的內容。過大或舊圖片可離開模型視窗，但原件仍可合法回讀 | 「依附件表格」實際引用該版資料；附件缺失不杜撰，裁掉舊 inline image 不刪原檔。M |
| PA-09 | Automation／writing-style suffixes | Routine 的 occurrence／目前狀態和人類長文的 style 指引依事件加入，不讓排程指示套進普通 user turn；不相關指引不常駐 | 排程提醒與真人追加問題可分辨；短答不載入整份長文指南。C |
| PA-10 | Reply／unfinished-work reminders | 只加入與當前事件相關的待答、未完成工作、已知／未知效果及必要下一步；新問題不讓先前責任憑空消失 | 中途追問回答後能接回原工作；使用者取消後不靠 reminder 復活它。M |
| PA-11 | Unanswered questions／approval cards | 中斷後可找回仍有效的未答問題與批准回條，綁原內容／版本；過期或已回答的不重複追問 | 重新連線不再問同一已答問題；舊卡片的批准不適用修訂後的不同操作。C |
| PA-12 | Hidden／silence turns | 可信 routine 喚醒可無公開訊息，但仍有內部處理結果；真人待答、需批准、未告知的失敗／未知不能被靜音。前插或後插不改變權威 | 無變更的 routine 沉默；同時有到期交付責任則不能用 silence 掩蓋。M |
| PA-13 | User／Project／Own memory | FA 具備對應的人類明示工作偏好、Team／專案工作知識、Agent 私人記憶視圖；每層的讀取依 subject／scope／用途，不默認跨 Team 共用 | A 的私有偏好不進 B 的 context；某使用者在另一 Team 沒授權就不自動帶過去。C |
| PA-14 | Tier／conflict／decay | Profile、episode、note 的有效性、相關度與時效可判讀；權威 Work／當前批准優先於摘要。更正核來源與版本，不只是「最後看到的字串贏」 | 新確認 owner 壓過舊 episode；逾期 note 不當現行事實；必要記憶缺失明示不足。M |
| PA-15 | FA：scope／topic switch | 每輪重新判斷哪些舊指令仍適用。本輪限制、當前事項、conversation 規則與長期偏好分開；每 turn 新 job 不等於使用者事項已結束 | 續寫同一任務保留必要格式；明確換題解除已到期格式；「以後都用繁中」只有在同意的記憶範圍內延續。M |
| PA-16 | Assembly accounting | 能說明這輪用了哪些版本、來源、技能、狀態與省略理由；區分 text-section 計數、完整 request 用量與 provider 回報，不保存秘密／私有 reasoning 當稽核捷徑 | 缺 token 資料顯示 unknown；optional 被省略有理由，必要限制放不下就明確拒絕，不能裁掉當成功。C |

### 5.3 必須能辨識的指令期限

| 指令形態 | 目標作用域 |
|---|---|
| 明說「本回合」「這一次輸出」 | 只作用於該次要求，完成後不得自動成為下輪格式 |
| 明說「這份報告」「這個交辦事項」 | 同一事項的追問／修訂沿用；不是只因建立新 job 就丟掉，也不是延伸至所有新事項 |
| 明說「本對話都……」 | 適用於原 conversation，直到有效撤銷、取代或 reset；不溢出到其他 conversation |
| 明說「以後記住……」 | 先經既有允許用途、敏感性及 scope 檢查；生效後屬有來源的偏好，不是全面操作授權 |
| 沒說期限的測試／格式命令 | 不自動提升為長期偏好。依當前交辦事項判斷；若會實質影響新要求且無法確定，明確說明／詢問，不猜永久有效或永久失效 |
| 評測用「只回代碼」 | 留在明確的 test conversation／執行範圍，不作一般主工作台後續自然互動的預設 |

同級新指令是否取代舊指令仍要看內容與適用範圍；不能用「永遠只看最後一句」取代這份契約。

## 6. 逐字稿、壓縮與記憶沉澱的完整目標

來源：[G03]。模型視窗的壓縮、SDK 持久狀態、FA 權威工作與長期記憶是不同層，不互相替代。

| ID | Grok 對應 | FA Target State | 最小正例／反例與判定 |
|---|---|---|---|
| TC-01 | Model／UI transcript views | SDK 管模型對話；UI 是已允許公開訊息／事件的投影。模型壓縮不刪 UI 已保存答案；UI preview 不變成模型權威歷史 | 壓縮後舊公開答案仍可讀；preview 消失也不代表已保存成果消失。C |
| TC-02 | Append journal／receipts | 新訊息、工具關聯、回合結果與已確認 receipt 可追溯，未知效果保留；不以重新生成的 assistant 回答覆蓋原歷史 | 同一事件重新讀取不新增效果；回合中斷不把 partial 改寫成成功。C |
| TC-03 | summaryArchives／originals | 移出模型視窗的合規必要原文、附件與工具結果保留可核對參照／範圍；需要精確字句時可回讀，並遵守保存與存取規則 | 摘要漏掉具體值，仍能回來源；只有 hash 沒有可取原件不算通過。M |
| TC-04 | Effective model limit | 根據選定模型／provider 的真實容量與 SDK 行為安排 input、output、tools、images；未知容量不假造固定值 | 換較小視窗模型前先辨識不適配；不能把舊 32,768／1,024 常數偷偷套回所有自由指令。C |
| TC-05 | Four compaction triggers | 能處理接近 token、明顯超額、已確認 input-limit rejection、image 壓力；預防性背景或阻塞處理由 SDK 能力決定，分別可觀察 | 有壓力才採相應行為；一個 GET413 不能自動被歸類為 model context-limit。C |
| TC-06 | Protected prefix／user info／attached skills | 當前必需指令、使用者精確要求、進行中工作與適用 skill 不只剩可能失真的摘要；保留或可明確重新載入固定版本 | 「請保留此字串」經壓縮仍能精確回讀；不可因摘要遺失讓已撤銷許可復活。M |
| TC-07 | Recent user verbatim budget | 關鍵近期原話有明確保留策略，工具 call/result 關係完整；空間不足時可離開熱視窗但不能混淆發話者或字面內容 | 摘要不能把工具建議改成使用者要求；引用原話時對回原件，不把摘要當逐字引文。M |
| TC-08 | Provider-specific summarization | 優先沿固定 SDK 對已宣告支持之模型／provider 的壓縮機制。摘要是派生資料，不製造新 human instruction／approval | 換 provider 只宣稱已驗過的能力；不能因 Grok bundle 有三個分支就宣稱 FA 三者都已支援。C |
| TC-09 | Compaction consistency | 壓縮期間的新訊息不丟失、不重排；完成結果對應正確前綴／版本。失敗時保留最後有效狀態，不拿半份摘要取代原歷史 | 同時進來的 follow-up 在壓縮後仍在正確位置；晚到摘要不能蓋掉更新或撤銷。C |
| TC-10 | Epoch／prefix refresh | 成功壓縮後可核對摘要來源，必要的穩定區塊／catalog 在下一個適用回合刷新；安全與批准變更不必等此時 | 壓縮成功但權限已變時用現行權限；失敗壓縮不虛增「已成功世代」。C |
| TC-11 | Portable persistence／resume | 正確保存同版 SDK 恢復所需的資料，跨 process 仍同 conversation／SDK identity；不只打包兩個檔名或單純調大常數。舊 inline 容量不再使合法長對話失去接續 | 真 SDK 狀態分別超過 raw 131,072 與 compressed 16,000 bytes 仍可保存／恢復；必要檔案缺失時不從空 history 假成功。C |
| TC-12 | Failure／retry／cancellation | compaction、模型、工具、保存、交付各有明確結果。不明提交或副作用不因「再試一次」被重送；取消及晚到回條沿原身分處理 | 已確認拒絕且可安全新嘗試與 unknown 分開；超時不宣稱 provider 已停止。C |
| TC-13 | Per-turn memory extraction | 有效且已保存的回合可產生有來源的記憶候選，不等 compaction 才開始；寒暄、ACK、未完成 preview、操作批准、未知自述不自動變成事實 | 明示安全偏好在新對話可用；只有 preview 或模型自稱完成不進有效 Memory。M |
| TC-14 | Episode／profile／note | 以合法且有意義的交流累積 episode，profile 保存明示穩定偏好，note 有用途／期限；測試格式及單次命令不自動升格 | 正常多輪偏好可持續；LATENCY「只回代碼」不成長期人格；無事可記時留 skipped，而非捏造事件。M |
| TC-15 | Turn settlement／lineage／revocation | 同一保存回合重處理不重複建立有效記憶；抽取接受／完成／有效投影分開，更正先阻止舊值使用。回合記憶失敗不扣住已保存答案 | native update 回 completed 但沒有必要事實，不標記憶成功；unknown 不盲重送，舊 projection 不能復活撤銷 claim。C |
| TC-16 | Metrics／retention | 以真 runtime 事件、必要狀態與去敏原件核對壓縮／恢復；保存範圍與保留期限明確。未結責任材料不能被一般清理誤刪；物理刪除另循批准 | 有成功 compaction 回條加恢復結果才可報通過；模型 tokens、archive bytes、UI bytes、時間口徑分開，不用零補未知。C |

### 6.1 壓縮後必須仍可成立的使用者體驗

使用者能續問同一交辦事項、修改已給出的目標、引用必要的原話、辨識哪些操作真的已完成，以及停止不再需要的工作。系統不得因「模型看不見舊全文」就忘記交付責任；也不得因保留歷史就永久沿用過期格式或批准。

這不承諾所有聊天永遠保留或模型一次看到全部資料。實際保留／刪除由既有政策與批准決定；目標是保存合法、必要、可追溯的恢復材料。

### 6.2 與現行串流的接點

```text
preview / streaming text
  → 只供暫時呈現，不觸發正式 Memory 或恢復點

對應回合結果耐久保存
  → 可啟動去重的記憶候選處理
  → SDK 必要狀態有合法恢復參照

執行完成／交付成立
  → 依各自回條更新，不由 preview 或摘要推導
```

現行正常 SDK stop 要求仍在；未來若使用者批准並驗證另一種生命週期，必須維持相同的耐久完成／防重送效果。這不是本文件授權常駐化或移除 stop gate。

## 7. Skills 與 Context 預算的完整目標

來源：[G04]。本範圍要求技能能被發現、正確選用及受控載入，不是只保存一個 `kind="skill"` 檔案。

| ID | Grok 對應 | FA Target State | 最小正例／反例與判定 |
|---|---|---|---|
| SK-01 | Managed catalog | 有權且已批准的技能才進入當前 Agent／Team 的 catalog；具 name、用途、精確版本／hash 與受控資源參照，不暴露他人的私有目錄 | 同名不同 scope 不誤載；未批准草稿不出現在可啟用目錄。C |
| SK-02 | Explicit／implicit selection | 使用者點名的已批准可用 skill 應選用，否則說明不可用；未點名時依任務相關性選最少所需技能，必要時簡述用途，不為展示能力亂載 | 明確點名且可用時實際載入；無關技能正文不進模型。M |
| SK-03 | Catalog context budget | 初始目錄有基於有效模型容量的 token 預算，計數與配置可核；為使用者指令、必要資料、工具與回覆留出空間 | 同一模型配置可重算相同目錄大小；未知 tokens 不填零，不能塞滿視窗才發現主請求放不下。C |
| SK-04 | Three-step degradation | 超額先縮短描述，再用名稱／路徑或邏輯參照，最後省略低相關完整條目，明記省略數／查找方式。只縮目錄，不任意截技能正文或核心安全規則 | 大 catalog 可發現被點名且有權技能；省略不變成假「不存在」，必要技能載不下明確回報。C |
| SK-05 | Read before use | 本文在需要時由受控 loader 讀入，內容／hash／批准版本相符；初始只放目錄，不能用已顯示名稱冒充已載入 | 合成 skill 的獨特規則只在真正載入後進 payload 並被採用；下載失敗不改用未核快取。M |
| SK-06 | Skill instructions precedence | Skill 提供達成目標的程序，不覆蓋上層安全、當前資料權、操作批准或授權內的使用者要求 | 惡意或過時 skill 要求外傳／安裝／升權時仍被程式擋住。C |
| SK-07 | Supplemental resources／tools | 附帶檔案與工具依 skill manifest／當前權限取用。載入文字不自動安裝 plugin、登入帳號、執行腳本或放開任意 filesystem | skill 引用外部路徑／未知腳本明確拒絕或要求適用批准；允許的唯讀資料仍可用。C |
| SK-08 | skillify | 可將冗長的程序指引搬成可版本化 skill，主 prompt 留精簡索引／使用條件；不可把不可省略的安全／授權規則搬成可選後就漏掉 | code-changes／routines 等長本文不常駐；啟用後仍能按需取得且不減少原安全能力。M |
| SK-09 | Version／disable／revocation | 一次載入固定版本；下一個適用回合使用當前有效版。停用／撤銷真生效；歷史有舊本文不等於仍獲准，也不假稱已從模型記憶徹底抹除 | 切版後不混用舊附帶檔案；已撤銷 skill 在 resume 後不可再當有效能力。C |
| SK-10 | Authoring／learn from demonstration | 使用者可交付示範或指示，形成有來源的 skill 草稿與測項；提案、審核、批准、生效分開，模型不可自行發布 | 示範裡的秘密／私人資料不被複製成模板；草稿不直接影響其他 Agent。M |
| SK-11 | Export bot template | 經授權可匯出可攜的角色／技能／設定參照，保留版本與必要依賴說明；不含 credentials、私人歷史或預設跨 Team 授權 | 匯入後未授權的 connector 仍須連接；不能因模板含 skill 名就取得來源帳號。C |
| SK-12 | Resume／rebuild／catalog changes | SDK 恢復時重新核對可用 catalog、版本與載入設定；沿 `mode="empty"` 明確 opt-in 所需能力，不靠 ambient skills／plugins 補缺件 | 換 process 仍能使用同一合法技能；全域安裝的無關 skill 不偷偷進來，SDK 原生 loader 不足時回真限制。C |

### 7.1 技能內容的功能分類也在目標內

| 類型 | 目標 |
|---|---|
| 任務流程 | 能承載排程、資料研究、訂位／採購等具體流程，但各流程的外部動作仍受 `SP-20`／`SP-27` 約束 |
| 行為手冊 | 群聊、通道、代使用者寫作、表單、desktop、connector 替代方法等，可以被選用且與真工具能力一致 |
| Meta 能力 | Skill authoring、從示範形成草稿、匯出模板、routines、程式修改等，不靠模型自授權發布 |

作者單一樣本的技能數量不是目標上限，也不代表本批必須一次開通所有商業場景。完整目標要求三類的承載與控制方式成立；具體 playbook 以宣告支持的集合逐一驗收，不能用一個 demo 宣稱全部技能都可工作。

## 8. SDK／FA 的分工與可行性界線

| 既有正式能力 | 優先用法 | FA 必須保留的差額 |
|---|---|---|
| SDK session／history／tools | 管單一模型與工具循環、原生事件與對話恢復 | Human／Team／Agent／work 身分、當前權限、效果回條與公開交付 |
| System message modes／hooks | 用正式 options、`additionalContext` 等組裝能力 | 版本、來源／作用域、正確批准與動態資料。Hook 本身不能強制授權 |
| 原生 compaction／usage events | 管模型視窗壓力，觀察真實成功與用量 | 精確工作狀態、必要原件存續、portable storage 完整性 |
| Session filesystem Interface | 依固定版本實際契約選適用 Adapter | 租戶／Agent scope、必要持久性／原子性及錯誤處理，不重寫整套 runtime |
| SDK skills／tool discovery | 在 empty mode 明確啟用受控技能 | 已批准版本、catalog／本文權限、更新／撤銷及按需內容 |
| Foundry Hosted／Memory | 承載 session 環境及受控記憶投影 | 不把 session 私人 `$HOME` 當共享檔案；低階 Memory scope 與結果 lineage 由 FA 核對 |

正式文件依固定 SDK commit 與 Microsoft 官方來源引用。[S01][S02][S03][S04][S05][S06][S07][A01][A02]

**這些是正式可評估的 Interface，不是每一種目標組合已經驗證可行。** 尤其 dynamic skills、實際 provider compaction 行為、voice／canvas／群聊接點、可攜式完整 state 與一般 browser／desktop 能力，都需對相應固定版本確認。若 SDK 已能完成就不重造；不能滿足時才採最小 Adapter，不能用 prompt 承諾掩蓋不可用工具。

## 9. 完整目標的跨回合情境

下表讓四章形成可觀察閉環。這是**評測案例設計，不是已跑過的案例、已完成的 error analysis 或人工 gold labels**。

| 情境 | 必須觀察到的結果 | 主要條目 |
|---|---|---|
| E-01 一般交辦、工具取得資料、交付 | 正確對話先有真接單或回覆，工具結果支持答案，最後能取得保存的完整結果；preview 不提前完成 | SP-01、03、04、PA-01、07、TC-01、02 |
| E-02 長工作途中追問／修改目標 | 新問題不丟、原未結責任不消失；已取消事項不被 reminder 復活；結果對回原工作 | SP-06、09、18、PA-10、11、TC-09、12 |
| E-03 不可信資料要求外傳／擴權 | 可繼續使用合法資料，但拒絕未授權效果；仿冒 system／approval marker 無效 | SP-20、21、25、29、PA-01、SK-06 |
| E-04 新對話沿用合法偏好／更正 | 經同意而生效的偏好可跨適用 conversation 使用；更正立即停止使用舊值；其他 scope 不洩漏 | PA-13、14、TC-13、14、15 |
| E-05 長 history → 壓縮 → 換 process | 真正 compaction 成功，必要原話／來源可取，工作／批准狀態不失真；不重送已確認或未知效果 | TC-03～12、PA-04、16 |
| E-06 已批准 skill 的按需使用 | 初始只有有權目錄，實際需要才載固定本文；切版／撤銷／resume 不沿用過時許可 | SK-01～09、12、PA-02～06 |
| E-07 Routine／群聊的沉默與待答 | 無關 FYI／無變更 routine 不刷屏；真人待答、失敗或批准要求不可被沉默掩蓋；無 ACK 迴圈 | SP-19、22、26、PA-09～12 |
| E-08 敏感外部效果 | 草稿、報價、建立 metadata 不等於外寄／購買完成；批准綁實際內容；拒絕／unknown 分支不重送 | SP-14、20、27、TC-12 |
| E-09 指令期限、換題與測試隔離 | 一輪限定「只回 CODE」不污染下一題；有效 conversation 規則仍保留；乾淨對話沒有 LATENCY 測試限制 | SP-31、PA-15、TC-14 |
| E-10 宣告能力與關閉能力 | 能力未開時不假裝可打電話／看桌面／開 canvas；開啟後能在真工具上完成該能力所需最小流程 | SP-07、08、10～13、17、SK-07 |
| E-11 保存失敗與串流中斷 | 已有文字 preview 仍可能不是成功；原保存／unknown 可追，重連不偽造答案、不清零用量 | SP-03、09、TC-01、02、11、12、16 |
| E-12 由示範形成可用技能 | 合法示範產生草稿、審核／批准後生效，再在隔離對話選用；不外帶秘密、舊批准或私人歷史 | SP-20、29、SK-10、11、12 |

### 9.1 如何判定，不先發明整體品質分數

| 類型 | 適合的判定方式 |
|---|---|
| 身分、scope、版本、hash、工具效果、保存／恢復及觀察狀態 | 優先程式／實際回條驗證；必要 byte、case 和版本可追溯 |
| 來源是否支持答案、換題是否正確、語氣／主動性是否恰當 | 先由人看必要去敏完整流程與輸出；語意 failure modes 穩定後才建立與校準 LLM judge |
| Latency | Preview、模型、保存完成、console 取得及 browser paint 分開；只報真的量測，不從 prompt 或單次成功算提升 |
| 尚未啟用／未實作／未跑／缺資料 | 分別記錄，不混成 Pass／Fail，也不併入 agent 成功率 |

舊 M4 的 keyword／citation 程式檢查可以保留作狹義契約；不得把 `source_terms` 命中升級成通用語意支持。已做的六個離線 probe 仍只是 evaluator 反例，不是這份 12 情境的執行結果。[R02]

## 10. 覆蓋對照與完成聲明

| 使用者指定的原章節內容 | Target 對應 |
|---|---|
| System prompt §2.1 組裝／短 preamble／21 個骨架 | SP-00～SP-21 |
| System prompt §2.2 完整行為及可插拔規則 | SP-01～SP-30 |
| 每回合組裝 §3.1 四段凍結 | PA-02～PA-04 |
| 每回合組裝 §3.2 差異／profile／connector 更新 | PA-05～PA-06 |
| 每回合組裝 §3.3 訊息、附件、提醒、未答問題、hidden turn | PA-01、PA-07～PA-12 |
| 每回合組裝 §3.4 記憶形狀、優先／衰退 | PA-13～PA-14、TC-13～TC-15 |
| 逐字稿 §4.1 模型／UI、archive | TC-01～TC-03、TC-11、TC-16 |
| 壓縮 §4.2 觸發與容量 | TC-04～TC-05 |
| 壓縮 §4.3 分割、保留、provider 摘要、插回與 epoch | TC-06～TC-12 |
| 記憶 §4.4 回合後沉澱／episode／結算 | TC-13～TC-15 |
| Skills 第 5 章 catalog、2% 與降級、read/load、skillify／類型 | SK-01～SK-12 |
| FA 新觀察：作用域／換題、測試對話污染 | SP-31、PA-15、TC-14、E-09 |
| FA 新實作：preview 與 durable／completed 分開 | SP-01、03、TC-01、02、11、E-11 |

**可以宣稱的完成狀態：** 四章的目標已完整定義，且已有對照與可設計評測的條目。

**尚不能宣稱的狀態：** FA 已達 Grok Bot 全能力、76 項均已實作、12 個跨回合情境已通過、LLM judge 已可信，或使用者已批准本文件帶來的新部署／新支出／新對外效果。

實作切片可以小，但對外報告必須清楚列出已支持 profile、未啟用能力、未完成條目及未驗條目。原 CTX-01～04 的工程候選不撤回，只是放回這個更完整的目標之下。

## 11. 來源

### Grok 研究四章

[G01]: https://leescot.com/research/grok-bot-internals/#doc/system-prompt
[G02]: https://leescot.com/research/grok-bot-internals/#doc/prompt-assembly
[G03]: https://leescot.com/research/grok-bot-internals/#doc/compaction
[G04]: https://leescot.com/research/grok-bot-internals/#doc/skills

### FA 基準與既有研究

[R01]: grok-context-comparison.md
[R02]: context-evaluation-baseline.md
[B01]: context-research-evidence.md#wip-1915
[B02]: context-research-evidence.md#wip-1915
[B03]: context-research-evidence.md#wip-1915
[B04]: context-research-evidence.md#wip-1915
[B05]: context-research-evidence.md#wip-1915
[B06]: context-research-evidence.md#wip-1915
[U01]: context-research-evidence.md#maintainer-observations

`U01` 指維護 session 在 21:27 本次要求後的來源方回覆：四章 code 未變，另提供 21:19 測試對話持續回代碼的去敏觀察。本研究沒有讀原始私人對話，也沒有自行重現或倒改其測試結果。

### 固定版本 SDK／官方平台

[S01]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py
[S02]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/hooks/user-prompt-submitted.md
[S03]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md
[S04]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session_fs_provider.py
[S05]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/skills.md
[S06]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/setup/multi-tenancy.md
[S07]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py
[A01]: https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents
[A02]: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/memory-usage
