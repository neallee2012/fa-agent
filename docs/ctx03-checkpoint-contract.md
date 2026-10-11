# CTX-03：SDK checkpoint 保存契約

版本：`ctx03-checkpoint-contract/v1`

日期：2026-09-24。狀態：**契約已確認，本機產品整合進行中；新 codec 尚未正式驗收或啟用。** 使用者先確認 `draft-2` 的最小來源接線設計並定稿為 `v1`，之後另行授權本機實作。本文件中的 codec 名稱與新增錯誤碼仍需各批接線與驗證，不代表既有雲端程式已接受它們；本文件更新沒有部署或改寫原資料。

**後續授權更新：** 使用者於 2026-09-24 13:50 明確要求自主完成完整 Target State 的實作及 evaluation 維護，本機產品整合已進入實作階段，進度見[狀態帳冊](context-implementation-status.md)。上段保留契約確認當時的範圍；新授權不等於新 codec 已完成或可部署，雲端／付費模型／權限／真實對外效果仍另取適用批准。

**雲端授權增補（2026-09-24）：** 必要部署與設定已取得後續批准，上限為共用 USD150；唯讀成本 preflight 已完成，但可靠 headroom 仍 unknown。IAM／RBAC 權限變更與額外付費推論不由此自動批准，未核清範圍與餘額前不新增計費操作。[範圍與執行條件](context-implementation-status.md#雲端授權與共同預算)不改本契約的 private／unknown／default-OFF 與採用前驗收規則，也不把 Q14 從 NOT_RUN 改成通過。

`draft-2` 補正的不是投影範圍，而是來源證據：現有 authority 可驗誰能保存，尚未把特定 compaction 動作接進該證據鏈；SDK 有可選 `trigger`，但無參數呼叫不自動記成 `manual`。完整唯讀依據見[Q11 證據索引](context-research-evidence.md#q11-feasibility)。

## 1. 要解決的問題與目前結論

一般 filtered 大狀態已有本機保存／恢復證據；原生 compaction 產生的兩類 model diagnostics 則超出既有私有保存規則。候選 B 已證明一條有限路徑：固定 SDK／runtime、default manual compaction、清除兩個 exact event types 的 `data` 後，可以在兩個新程序中續問、更正與回想。

這不足以直接解除 production 拒絕。需要明確回答：誰選擇保存格式、哪些內容能存、如何認出先前已處理的事件、未知 shape 如何拒絕，以及合法 reference 何時能成為下一輪的恢復點。

**本契約採用：保留舊格式，另定義嚴格且預設停用的新 codec；不讓 client 用一個 `safe=true` 旗標繞過既有檢查。** 契約已確認；後續仍須明確批准本機實作、補足相應證據，最後再另決定平台啟用範圍。[本機結論](ctx03-local-acceptance.md) · [證據識別](context-research-evidence.md#ctx03-default-positive)

## 2. 適用範圍

| 項目 | 本版契約的第一個適用範圍 |
|---|---|
| SDK／runtime | 精確固定 Python SDK `1.0.13`／runtime `1.0.83`；不接受相容版本推定 |
| 已有實驗平台 | Windows x64；不同 Python／runtime process，同 SDK session／conversation |
| 已有模型傳輸證據 | Offline OpenAI-compatible completions fixture；配置的模型名稱不是實際模型品質證明 |
| 已驗 compaction 呼叫 | 原 default-positive 使用無參數 `history.compact()`；此歷史結果保留，不回填新的 trigger |
| 正式契約要求的 compaction 呼叫 | 從最後合法點恢復，由受控路徑顯式標註 `trigger=manual`，**不傳 `custom_instructions` 或 `token_limit` override**；此新呼叫形狀尚未實測 |
| 後續一般回合 | 可沿同一已驗 codec 恢復及保存，保留已處理事件；新的未驗 compaction 不自動納入 |
| 資料 | SDK 指定 session filesystem 的完整、合法 UTF-8 狀態，非整個 HOME 或任意工具資料夾 |
| 現在是否可啟用 | **否**。Formal writer／reader、真正部署傳輸與平台路徑均未完成採用驗收 |

Automatic compaction、帶 custom 指令的 compaction、其他 SDK／runtime、跨 OS、真 Hosted／Blob、其他 provider 格式及真模型摘要品質，不由上述證據自動覆蓋。實作可在不支援的路徑明確拒絕，不能換成廣泛 `model.*` drop、升 SDK 或清空歷史求成功。

**2026-09-29 Linux 採用範圍增補：** 固定 SDK `1.0.13`／runtime `1.0.83` 的
Linux x64／scripted provider 已沿原產品 API、codec、ArtifactStore、manual compact 與
fresh-runtime restore 完成兩個本機 native 案例。新增 `platform=linux-x64`，
與實際 Linux x64 比對；舊 `win32-x64` profile、None、原 references／hash 不改。
資料格式與 private／unknown／source／正常 stop 契約沒有放寬，也不由這次 Linux 本機結果
宣稱跨 OS、Hosted／真 Blob 或真模型品質通過。[實作與分批結果](../src/fa-shared-context-poc/README.md#linux-接線-verified-against)

使用者已指定**部署後 checkpoint 直接開給新對話，不設觀察期**。
沿既有受信 human 選版固定 Linux preset，再由原 immutable registration／task 捕獲；
不是重寫已安裝 policy、變更舊對話或讓聊天／upload body 自授。新對話的設定變更只影響新對話；
舊對話的保存／恢復 consumer 依其原已捕獲選版核對，不能被後來切回 None 破壞。
reader 先認得 Linux profile 再啟用 writer，是避免寫出唯一但不可讀恢復點的相容性要求，
不是新增批准層。以上更新取代本節「第一個適用範圍」對新部署的 Windows-only／預設停用限制；
歷史批次當時的 OFF／未驗結果仍保留。

**2026-09-25攔截接點修正：** 收到新的compaction事件而未設定checkpoint profile，
同樣必須沿原model gateway在後續模型授權／傳送前拒絕；「未啟用」不能解讀成忽略訊號後照常dispatch。
此修正不啟用automatic/model-switch compaction、不增加codec或改寫舊archive。
最小seam反例與修補沿[產品README](../src/fa-shared-context-poc/README.md)分批記錄；
尚未實測native背景壓力或真preview曝光，不從合成事件推定曾發生那些結果。

### 2026-09-30 Responses 專用 codec

使用者已批准只替新版模板的新Responses對話增加版本化codec；舊Completions、
既有preset、checkpoint原件／hash／selection及未決請求不改，也不為回退舊版拆部署階段。
API與Hosted仍須同一build。這個增補不開放原始或加密reasoning存入可發布checkpoint。

| 新格式 | 範圍 |
|---|---|
| `session-fs-responses-v1`／`artifact-session-fs-responses-v1` | 新Responses對話的普通完整SDK狀態，綁`model_provider=openai-responses` |
| `session-fs-responses-diag-v1`／`artifact-session-fs-responses-diag-v1` | 同一provider的manual-no-custom壓縮及後續續存，沿原source／正常stop／parts／hash契約 |

固定SDK／runtime的離線原件已觀察到：
`model.message.data.message`與`model.response.data.response`保存assistant的公開本文，
並附加`reasoning_text`、`reasoning_opaque`、`encrypted_content`、`reasoningBlocks`。
新codec只在這兩種精確外層／內層形狀和一般assistant訊息，沿既有private欄位政策移除
這些已知欄位及其對應camelCase名稱；不刪event、不重掛parent、不編造替代本文。
模型呼叫metadata的`reasoningSummary`只接受固定SDK已知模式，不作摘要內容；
兩類既有raw diagnostics仍只在原已驗來源條件下令`data={}`。

Provider-tagged block必須為`openai-responses`，已知`reasoning`項目及
`summary_text`／`reasoning_text`部分各守精確欄位；未知欄位、容器或type拒絕。
加密資料、raw reasoning與它們的副本不得留在event、sidecar或已發布parts；
禁留值只取真正加密／私有payload；已辨識的公開`summary_text`、狀態、provider及item ID
不列為秘密。扁平reasoning文字若精確等於公開摘要可移除欄位而不禁用相同的公開原文；
不同的raw reasoning或任何encrypted payload仍保留禁留檢查，不能藉公開摘要繞過。
原始SDK記憶體files保持不變，只發布通過檢查的派生副本。
失敗時不建立新的合法reference，最後一份合法狀態與原UNKNOWN保留。

原生Responses可用固定索引title `Session response`與`NNN-session-response.md`；
新codec僅認這個已觀察形式，仍核索引/path/序號，sidecar本文精確等於原summary去掉title標記的內容。
未更改原Completions對title／body的規則，亦不改寫原生sidecar來讓驗證通過。
跨程序續接使用FA自己的完整公開狀態；要求`store=false`，不靠供應商端response ID恢復。

**Verified against：** `behavior_responses_checkpoint`、`behavior_sdk_state/files/checkpoint/compaction`、
`test_instruction_responses.py`與`test_instruction_responses_native.py`。
含用量、未知私有shape的RED→green，以及固定SDK／runtime、Windows本機離線provider的
manual保存與新程序恢復；不由這些結果推定真模型、Linux Hosted或Azure Blob已驗。

**2026-09-30現場拒絕診斷：** Responses原拒絕處另記有上限、只有欄名／型別的
`RESPONSES_CHECKPOINT_SHAPE_REJECTED` WARNING，不記原始值、不新增保存欄位，
也不改拒絕條件、原SDK files或最後合法狀態。部署後新CID取得的現場shape為
`reasoning`項目多一個字串`response_id`；codec只把它加入已知欄位（須為字串），
與item ID同屬識別metadata、不列為秘密，隨整個reasoning容器從派生副本移除，
也不用於供應商端恢復。其他未知欄位仍拒絕。限制與直接測試見
[產品README](../src/fa-shared-context-poc/README.md#responses-checkpoint-現場-response_id-修正)。

本稿將未來 profile 稱為 **manual-no-custom**：沒有自訂摘要內容或 token-limit override，但有明確的來源 metadata。這與原先「完全不傳參數」的 fixture 不相同；不因文件改名就宣稱新增呼叫已驗。

來源判定必須來自受控呼叫路徑及其對應原生事件，不能只看空的 `customInstructions`、單獨的 `trigger=manual` 或 client 標記。未知／缺省 trigger 不默認 manual；`data.customInstructions` 若非空仍拒絕，不刪除求通過。原三個已保存點的相關欄位都是 **missing**，不是 null／empty；未來正式呼叫的序列化仍須另驗，不人工補寫舊事件。

### 2.1 Q11 已核對到什麼

| 來源 | 已有能力 | 不能替代的證據 |
|---|---|---|
| `WorkRegistration`、immutable task/profile、當前 authority | Tenant／Team／Agent／job、owner／epoch、撤銷、task bytes 與 registration/material 比對 | 目前沒有選定 compaction mode/profile 或特定 compact 呼叫結果 |
| `AssistantSession`／`run_copilot` | 建立／恢復 SDK、送入原指令、保留正常 stop、保存 state | 被核的產品路徑沒有受控 `history.compact` 呼叫或與之綁定的保存回條 |
| `SdkStateUpload` | 原 job/session/conversation、state hash／bytes、stop、prepare/reference | 上傳意圖在 SDK 停止後建立，不是先前 compaction 動作的授權意圖 |
| 固定 SDK `SessionHistoryCompactRequest.trigger` | 可明示 `manual`／`model_switch`；文件明說省略時不記 attribution，initiator unknown | Caller 提供的 attribution 不是 FA authority，也不自動把 state 綁到 job |
| 固定 SDK compaction events | 有可選 `trigger`、`request_id`、`service_request_id`、`custom_instructions` 等欄位 | 不能說 SDK 沒有欄位；也未有依據把這些 request IDs 當成 FA job ID 或原 RPC 的關聯 ID |
| 13:25 已保存點唯讀檢查 | candidate／follow1／follow2 中同一組 start/complete 的四欄皆 missing | 缺省不能補成 manual；這不是三次新 compaction，也不推翻原凍結 runner 實際呼叫 RPC 的結果 |

**裁決：現有授權機制可重用，Q11 的 compaction 語義接線仍缺。** 不是需要重建 authority，也不是只改 validator 就可上線。`draft-1` 將這個必要接點列成條件，現在已確認它並非現有產品能力。

### 2.2 最小來源接線契約（已確認，未實作）

1. **伺服器端預期。** 沿現有 immutable job/profile 與其 digest，加入明確的 checkpoint profile／預期模式；由受信設定產生，不由原文指令、模型輸出或 upload body 自授。缺省仍停用，不新增逐次人工批准表單。
2. **受控執行觀察。** 由原 Hosted Adapter 在該 profile 的受控路徑呼叫 `history.compact`，顯式指定 `Trigger.MANUAL`，其他參數保持未提供；記錄該次 RPC 成功及可核對的 native start/complete、checkpoint／原 SDK identity 關係。有額外、未知或無法唯一歸屬的 compaction 就拒絕，不能猜是哪一次。
3. **沿既有保存紀錄綁定。** 將不含私有內容的執行觀察、預期 profile 與當前 state／job 關係納入原 upload/reference 的驗證及摘要；不另建 control plane，不以事後 upload intent 冒充事前動作授權。

這些是新增的產品契約語義，**本次未實作或驗證**。明示 trigger 只是 SDK 支持的 attribution，不是不可偽造的憑證；本方案沿用對受控 Hosted Adapter 的既有信任假設，不宣稱能抵抗已被攻破的 Adapter。單獨拿一份自行填寫的「safe／manual」JSON 仍不夠。

本次沒有為補 trigger 重跑原 fixture；原缺欄位的 test-only checkpoints 也不升格成新 profile。受控觸發何時發生仍須有明確產品路徑，不能藉本契約暗中新增 UI／一般工具／自動排程或模糊的壓縮政策。

### 2.3 2026-10-02：ADR-0007 native RID/CID 接續（C0，CODE/offline）

此增補只替新 native lane 接原 manual profile，不改舊 job-bound Blob consumer 或已發布原件。
`NativeCheckpointTurn` 由原 RID 的 started CAS、分配 turn 和已選 base checkpoint 產生；
真 `SdkSessionFiles.compact` 記 `NativeCompactionExecution`，以 agent/RID/CID/SDK session、
capture digest、base hash、原 model-request digest、runtime PID及 RPC/事件結果代替舊 job 欄位。
仍只呼正式 `history.compact(trigger=manual)`，不改 SDK 1.0.13／runtime 1.0.83。

State store 的 `native-session-fs-diag-empty-v1`／`native-session-fs-responses-diag-v1`
分別保存既有兩個診斷 state codec，不降級或換名求通過。manifest 明列原 profile/source 及
本次 RID/turn、直接前一合法 RID/turn/hash；後續一般續存沿用原 compaction execution，
不要求 execution.request_id 等於新的 RID。普通舊 reference 仍可讀且不改寫，
但新增的診斷格式不能缺原 RID lineage。
source/profile/provider/codec、完整分段、正常停止、繼承事件或未知 shape 不符時不建立新的合法引用；
SDK/store unknown 不重跑 SDK、不換 RID，仍保留最後合法 checkpoint。

固定 Responses runtime 的索引有已觀察的 `Session <SDK session 前8字>` 形式；
原 `Session response` 是這類形式的既有特例，不是所有 native SID 的固定名稱。
reader 只補和原 session.start、index/path/number精確吻合的格式，摘要與 sidecar body 保持原 bytes，
其他未知／Completions 不因此放寬。
[實作、真 SDK＋離線 transport 的三回合冷恢復與原 FAIL](../src/fa-shared-context-poc/README.md#verified-againstc0-profilescheckpoint)。
這些本機結果不代替 Hosted、真模型或雲端儲存驗收。

## 3. Module 與 Interface 的分工

不新增 conversation engine、custom compactor、一般授權平台或第二個 storage registry。使用既有 Module／Seam：

| Module／Seam | Interface 必須提供的保證 |
|---|---|
| Hosted `SdkSessionFiles` Adapter | SDK 原生 read／write／append／rename 等操作；同一 assigned root、失敗狀態不掩蓋；正常 flush／stop 後才封存 |
| SDK-independent checkpoint codec Module | 統一處理 format、版本、事件轉換、private checks、必要檔案與 canonical encoding；API、Hosted writer、restore loader 使用同一規則，不各自重寫 |
| 現有 `SdkCheckpointStore`／`ArtifactStore` Seam | 將驗證後的 bytes 存成 immutable parts，固定版本／ETag／hash 讀回；回傳完整 reference，或明確錯誤 |
| 既有 authority／conversation 完成路徑 | 核當前 binding、ticket、工作及撤銷；保留 upload intent、正常 stop、once、unknown、答案／archive／progress 交易 |

對外仍是「保存已封存的狀態」與「載入已發布的 reference」。型別可沿現有 `SdkState`、`StoredSdkArchive`、`SaveSdkState` 演進；這些名字不構成要求新增多層 pass-through wrapper。

**API-only 必須仍能在完全禁止 `copilot` imports 的 fresh process 啟動。** 只有 Hosted filesystem Adapter 依賴 SDK，純 DTO／codec 不匯入 runtime。

## 4. 格式與版本不能混用

| 格式 | 契約處理 |
|---|---|
| 舊 inline `SdkArchive` | 依原契約讀取；不做就地遷移，不把 unavailable 改成空白狀態 |
| 現有 `session-fs-v1`／`artifact-session-fs-v1` | 保留普通 filtered state 規則，仍拒絕兩種 raw diagnostic event types |
| `session-fs-diag-empty-v1`（待實作） | 明確宣告本文件限定的 diagnostics projection；對應部署／profile 未啟用時拒絕，文件確認不會開啟功能 |
| `artifact-session-fs-diag-empty-v1`（待實作） | 只引用上一列 codec 的 canonical bytes；不得與舊 state codec 交叉配對 |
| `ctx03-candidate-b-v1`／其 test-only reference | **永不直接作為 production 輸入**；不靠換名、去掉 `test_only_format` 或複製測試 store 來遷移 |
| 不認識的 codec／版本 | 拒絕，不猜、不自動降級，不先交 runtime 試讀 |

新格式沿用 SDK／runtime version、SDK session、完整 `files`／`directories` 及現有 reference 的 tenant／team／agent／conversation／job／artifact／hash／parts 綁定；另保存有序的 `projected_event_ids`。Codec 名稱決定投影規則版本，不另加可由呼叫者自由選取的任意 filter。

伺服器必須從**已批准的部署／工作配置**決定可用 codec 與 producer／consumer profile，不能只信 upload body 自稱的版本或安全性。Hosted Adapter 的固定 SDK guard、API 的 scope／格式驗證與正式接線驗收各負其責；API 不為重新驗 raw shape 而要求上傳已禁止的原文。

Codec 或政策語義改變就產生新版本。Raw 初版雖曾使用相近 DTO，也不能被新 reader 靜默當作合規資料。

## 5. 可保存與不可保存的內容

| 內容 | 規則 |
|---|---|
| 合法 user／assistant／tool history | 保持原說話者、事件關係及順序；沿既有 private filter，不重新生成歷史 |
| Event envelope | 保留 id、type、timestamp、parent 及其原值；不得刪 event、重掛 parent 或重編事件順序 |
| 八個既有 private fields／三種 private events | 完整沿用現有 `public_events` 轉換，不以「private checkpoint 不展示」代替處理 |
| 兩種已知 raw diagnostics | 只有符合下一節條件時，才在派生副本將整個 `data` 換成 `{}` |
| `session.compaction_complete`、summary、checkpoint sidecars | 不新增裁剪或重寫。必要內容保持；仍含禁止值就拒絕保存 |
| `customInstructions` | 第一版不處理帶 custom 指令的 compaction；不刪除此欄位，不把它概括成「都屬 private system」 |
| 已知 transport credential／被移除 private 值的副本 | 只在記憶體供拒絕檢查；若在 envelope、檔名、sidecar、summary 或其他保存位置殘留，拒絕 |
| 其他 SDK-owned UTF-8 檔案 | 完整保留、核對路徑及 private 結構／已知值；不因本次 runtime 未讀到就任意排除 |
| Binary／未知 filesystem capability | 第一版不支持；明確拒絕，不宣告未實作的 SQLite capability 或略掉必要檔案 |
| 原始 provider wire、私有 system／reasoning、任意 HOME | 不屬此保存契約，不以診斷需求額外持久化 |

既有八個 private field 名稱、三類 private events 與語義見[本機驗收結論](ctx03-local-acceptance.md)。它們是明確欄位政策及已知值保護，**不是可以辨識任意秘密的語意 DLP**。

已核的 default positive 沒在 compact request／raw state 觀察到已知 marker；這個未 exercise 範圍必須保留。負向敏感 custom 案例仍應拒絕，不因正向恢復成功改判。

## 6. Diagnostics projection 的精確規則

### 6.1 處理順序

1. 檢查伺服器預期 profile、codec、正常 SDK 停止與指定 identity。若有新 compaction，核對本次受控呼叫的執行觀察；若只是一般續存，從先前合法 reference 核對既有來源，不冒稱本 job 重新壓縮。單獨的 event trigger 或 body 標記不足；只處理一致、完整且未發生未解 filesystem 錯誤的狀態。
2. 在副本執行原 `public_events`；SDK 原始 in-memory files 不改寫。
3. 對**原始事件**做下列固定 envelope／SDK 必需形狀及有界 provider metadata 檢查，再只把兩種 target events 的 `data` 設為 `{}`。
4. 驗證事件數、相對順序及 parsed envelope 值完全相同；非 target data 必須等於 legacy filter 結果，所有 sidecar bytes 不變。
5. 執行結構／已知 private 值檢查，再驗必要檔案、大小、完整性與 canonical encoding；任何失敗均不發起 parts PUT。

`events.jsonl` 重新序列化可以改變 whitespace；等值要求是 parsed events／envelope 及其順序，不假稱 JSONL 原 bytes 完全不變。Sidecar bytes 則不得因投影而改動。

### 6.2 Target envelope

兩種 target 的 envelope 只接受：

```text
type, id, timestamp, parentId, data
```

`type` 只能是 `model.model_call_success` 或 `model.messages_snapshot`；`id`／`type`／`timestamp` 為非空字串，timestamp 可按固定 ISO 格式解析；`parentId` 為 null 或字串，原值保留。未知 envelope key、非 object data 或不合型別資料均拒絕。

不能由公開 dataclass 欄位較少，就丟掉 raw envelope 中其他欄位。也不推定所有 parent 的內部 consumer 語義已知。

### 6.3 `model.messages_snapshot`

`data` 的 keys 必須恰為 `kind`、`messages`；`kind` 必須為 `messages_snapshot`。`messages` 是 list，各項是 object，`role` 為 `system`、`assistant`、`tool`、`user` 之一。

這沿用已驗候選的有限形狀判斷，不聲稱是完整 provider message schema。新 role／新增外層欄位不自動接受。

### 6.4 `model.model_call_success`

`data` keys 恰為：

```text
kind, turn, modelCallDurationMs, ttftMs, outputTtftMs,
interTokenLatencyMs, modelCall, responseChunk, responseUsage,
requestMessages, quotaSnapshots, rte, reasoningEffort, toolCount
```

`kind` 為 `model_call_success`；`turn`／`modelCallDurationMs`／`toolCount` 為 int（不把 bool 當 int）；三個 latency 值為 int 或 float；`rte` 是 bool，`reasoningEffort` 是字串，`quotaSnapshots` 是空 object；`requestMessages` 是可解析為 JSON list 的字串。

三個 nested objects 的必需 key sets 如下，不以此冒稱內部所有值都有官方完整 schema：

| Object | Required keys |
|---|---|
| `modelCall` | `model`, `api_id`, `api_endpoint`, `request_id`, `client_request_id`, `service_request_id`, `rte`, `initiator`, `transport` |
| `responseChunk` | `usage`, `object`, `id`, `choices`, `created`, `model` |
| `responseUsage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_cache_frontier_source`, `prompt_cache_details_reported` |

**2026-09-29 provider metadata 相容修補：** `modelCall` 仍只接受上述 exact keys；
`responseChunk` 另接受固定 SDK/runtime 已觀察到的可選 `system_fingerprint`、
`service_tier`（string 或 null）及 `prompt_filter_results`（list 或 null）。
後者每項只含非負 int `prompt_index` 與 object `content_filter_results`；
不以空 annotations 冒充內容過濾通過。固定 runtime 未把本次 SSE 的 per-choice filter
保留在該 diagnostic choices；它不能因此被當成實際內容過濾通過的證據。

同日以固定 SDK／runtime、本機 Linux 與實際 project OpenAI v1 provider 重現後，
再明列 `latency_checkpoint`、`routing`（object 或 null）與 `obfuscation`（string 或 null）。
去值觀察中，latency object 是八個 `*_ms` int 欄位，routing 是 `serving_pipereplica` string；
不保存原欄位值，亦不把本機重現稱為已讀取失敗 Hosted 的原事件。

`responseUsage` 另接受 `prompt_tokens_details`／`completion_tokens_details`（object 或 null）；
已知 token counters 必須是非負 int 或 null，不把 bool 當 count。
`responseChunk`／`responseUsage` 的未知 metadata key，以及 token details 中未知 counter，
可使用有限 JSON scalar／list／object，但不能使用含內容或 private 的欄位名（包括巢狀
`content`、`text`、`message`／`messages`、`requestMessages`、`tool_calls`、`arguments`、
prompt／instruction／body／payload 及既有 private 欄位；比對忽略大小寫、底線與連字號）。
新增三欄及這些未知 metadata 合計的 canonical UTF-8 JSON 最多 **64KiB**，
每個 metadata value 的結構深度最多 **8**，number 必須有限。
未知 metadata 通過時記一行 WARNING，僅有 event type、key 名稱及型別，不記值；
raw shape 拒絕亦記去值 WARNING 的 missing／extra keys／types，方便在 Hosted 回收前定位。
原已知 metadata 的既有結構與 token counter 檢查不變。
必需 keys、外層 schema／版本、envelope、來源與其他 private checks 不變。

這是同一固定 SDK 可產生的 provider 輸入形狀補正，不改既有兩類 event 的 `data={}`
投影、持久 codec、sidecar 或已發布參照，也不保存這些 raw metadata。
未知 metadata 沒有新增到其他檔案的 copy 路徑；`requestMessages`／system/private 欄位的
抽取與 `forbidden_values`／原件複本比對照舊執行。不能以 metadata 名義夾帶內容欄位，
也不能把其他 SDK／envelope key 的變動用一般 redaction 或「整個 `model.*` 一律清空」掩蓋。
本機 scripted-provider 或真 provider native 結果，不等於已讀到回收容器內的 live event，
也不等於 live smoke 已通過；[實作與直接回歸](../src/fa-shared-context-poc/README.md#2026-09-29provider-metadata-的-checkpoint-相容修補)分開記錄。

### 6.5 先前已投影事件與續存

`data={}` 只有在該事件確實來自**同 scope、已發布且成功驗證的上一個 checkpoint**時才能作為 inherited entry。只看 client 傳來的 `projected_event_ids` 不足以證明來源。

Loader 從上一個合法 reference 取得其有序 target IDs／envelopes；writer 續存時必須核對它們仍存在、值未改、順序未變且 data 仍為 `{}`。新出現的空 data、改了 inherited payload、重複 target ID 或偽造其他 checkpoint 的 IDs 一律拒絕。

新 state 的 `projected_event_ids` 必須精確等於其中兩種 target events 的有序 ID 清單；它與 payload 一起受 canonical hash 保護。API reader 仍驗格式、scope、版本及完整性；這份 metadata **不是批准原始內容可保存的自授權旗標**。

## 7. 必要檔案與容量

必有非空、可驗的 `events.jsonl`／`workspace.yaml`，且只有正確的 `session.start` identity。採新 codec 時，必有原生成功 compaction 的完整記錄、`checkpoints/index.md`，以及每個被保留成功事件所引用的 checkpoint 檔案。

檔案必須在指定 SDK root 之內，轉為合法 session-relative path；拒絕 traversal、絕對宿主路徑、空／dot segment、NUL、混淆的分隔符及檔案／目錄衝突。Index 必須依固定 SDK 格式指向同一 checkpoint，不能只靠 basename substring 當完整關聯驗證。

SDK session 使用其設定的虛擬 working directory；runtime process 本身仍在原私有實體工作目錄啟動。
固定 runtime 1.0.83 在 resume 套用新 working directory 前會先 `stat` 舊值；
對已驗證原 state 中唯一 `session.start` 的 `context.cwd`，可提供**精確、只限 stat 的虛擬根目錄 metadata 別名**。
它不查宿主路徑、不宣稱該實體目錄存在、不授予 read/write/list/exists/delete 或子路徑存取，
也不採用 user message／後來檔案改寫中的 cwd。相對、traversal、NUL或混合分隔符不建立別名。
原 state bytes、hash及事件不因這個相容讀取而被改寫；後續狀態仍由正常 SDK 及原保存流程產生。

保存 SDK 實際寫出的完整必要檔案集合，不只保存「觀察到這次 read_file 的五個檔名」。若 runtime 查詢本來不存在的 optional `plan.md`，不製造空檔；如果它確實存在，就不得因本次沒讀到而省略。

| 容量／計量 | 本版契約 |
|---|---|
| Session filesystem 內容 | 保留現有 8 MiB 技術界線與 failed 狀態，不允許 callback 失敗後仍發布部分狀態 |
| 完整 canonical state | 最多 8 MiB，包含所有格式 metadata 與檔案內容，不只計 events 長度 |
| Immutable parts | 每片最多 512 KiB；1–16 片；byte lengths、part IDs、順序與總 hash 相符 |
| SDK-state HTTP／restore response | 沿現有 state 加 4,096 bytes 的專用封包界線；新增 DTO 分支也必須使用正確 limits，不能掉回普通小 response 上限 |
| Reference 大小 | 量 canonical JSON bytes；與易讀格式的檔案 bytes 分開 |
| Zlib | 只用來對照原 inline 瓶頸，不是新 codec 的 Blob 保存格式 |
| Provider request／模型視窗 | 與 state 大小是不同限制；不能因 8 MiB state 可保存就承諾完整模型 request 可送出 |

HTTP 必須量**實際完整 envelope**，含 `sdk_stop` 等欄位與 Unicode 編碼；不能從內部 state bytes 推定一定能過 transport。超額要明確拒絕，不截斷，也不藉此重加財務／token 人造 gate。

## 8. 保存、發布與完成的順序

```text
SDK 本輪執行
  -> 新 compaction：伺服器預期 profile + 受控 manual-no-custom + RPC/native 觀察
     一般續存：先前合法 reference + inherited 事件與來源驗證（不額外壓縮）
  -> session.disconnect() 完成 SessionFs callbacks
  -> client.stop() 正常返回，保留真實停止結果
  -> 等既有 progress drain，封存一致狀態
  -> codec 驗證及 private 處理
  -> 驗當前 authority，持久化該 job 的 upload intent
  -> create-only PUT 各 part，逐片固定版本讀回
  -> 完整 bytes/hash/shape 通過後，以原 guarded transaction 發布 reference
  -> 原答案 / archive reference / 最後 progress 依原交易保存
  -> 由 conversation 的完成與恢復條件決定能否接受下一輪
```

Parts 寫入成功不等於 reference 已發布；reference 已發布也不等於答案已保存、native 完成或使用者已讀。必要的 consumer 只能使用權威紀錄中已發布的 reference，不直接採用 client 任意送入的 part 清單。

**一致性接點補充（既有行為的直接覆蓋）：** 同一 conversation 的前一回合仍在 compaction
或保存時，新 submit 明確回 `CONVERSATION_TURN_UNRESOLVED`，不接受後再丟失或重排，
也不另加 queue。呼叫端保留原輸入，前一回合完成後才提交；撤權不等到 compaction 完成
才生效，未完成或遭撤權的結果不能發布成新 checkpoint／新世代。
下一回合沿原捕獲 profile/provider 與現行能力範圍重建 prefix；inherited compaction 保留
原 execution，不改標成新 job。這不新增格式或 epoch 帳本。實際範圍、測試及未驗事項見
[compaction 交會](../src/fa-shared-context-poc/README.md#compaction-一致性與版本刷新tc-09tc-10pa-04本機子集)。

API 的 `sdk-state` upload、`restore` response、wire DTO、Hosted `_archive`／restore 與最後 conversation archive reader 必須一起接線。同一支新 codec 不能只有 writer 會寫、reader／response limits 卻仍只辨認舊型別。

本機 Windows 的正常 stop 曾回 runtime exit `1`；保留原值及 graceful／force-stop 證據，不把 Python driver 的 `0` 填成 runtime 結果。此觀察不放寬 Hosted/Linux 原有的 native 成功條件。

**2026-10-06 註記：改為接上常駐 SDK（取代 2026-10-05「常駐不停程序的做法暫緩」的決定）。** 固定版 `github-copilot-sdk==1.0.13` 仍沒有 flush／save 介面（[`session.disconnect()`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L3037-L3083)、[`client.stop()`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py#L2034-L2080)）。官方[部署建議](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md)是一位使用者一個長駐 CLI server，並沒有每回合停程序。native 依 [ADR-0005](adr/0005-long-running-conversation-worker.md) 改用 PoC 驗證過的證據，不自建「保存完成」確認層：

- 回合 idle 完成後，本回合的 callback 仍綁著，再讓 event loop 跑一次；files 沒變、沒有還在跑的自動 compaction，才以不凍結的快照保存一般 checkpoint 並保留 SDK。快照與 release 的 files digest 在同一個同步步驟記下。
- 下一回合只有在拿到的已發布 checkpoint sha256 等於這份快照、files 在 release 後沒變時，才沿用同一份記憶體 files 與 SDK session；`SdkSessionFiles.previous` 改指向這份已發布 checkpoint。否則停掉常駐程序，照舊從 checkpoint 還原。
- manual profile、diag codec、這回合呼叫過委派（`task`）、取消、失敗時，照上面的 `disconnect()` → `client.stop()` 順序保存，並以 warning 記 `RESIDENT_SDK_STOPPED reason=...`。只選了委派但沒呼叫、或工具呼叫由審核直接放行（沒出卡）的回合不算：前者沒有任何子工作，後者沒有等待，SDK 的核准也只限該次呼叫（`PermissionDecisionApproveOnce`）（2026-10-06 dev 修正）。出過批准卡或提問的回合也保留（2026-10-06 v17）：idle 前 runtime 已拿到每個回覆，等待只讓回合變長，保存仍走上一項同一套 files 與 compaction 檢查；理由與出處見 [README](../src/fa-shared-context-poc/README.md#resident-sdk-after-hitl)。
- 殘餘風險：release 檢查通過後，`run_copilot` 不經任何 await 立刻取要發布的快照；快照不合格（例如 runtime 自己的 compaction 剛開始）就記 `RESIDENT_RELEASE_REFUSED`，照上面的順序停 SDK，停止期間本回合的 gateway 與 callback 仍綁著，runtime 可以把它做完。快照之後才到的寫入不在已發布 checkpoint 內；這只會發生在 auto profile、背景壓縮剛好在 idle 之後才開始的情況；下一回合以 `RESIDENT_FILES_MOVED` 退回從 checkpoint 還原，那次壓縮的事件不在歷史裡，下一回合可以重新壓縮，使用者訊息與回覆都已保存，不會遺失。
- 退路：native checkpoint 不存 `sdk_stop`，發布格式不變；退版或 VM 回收時下一回合就是冷還原。
- 回覆預覽（2026-10-05）照舊：SDK idle 後、保存前先存一份顯示用文字，不算完成；保留 SDK 時它同樣在保存前寫出（見[行為規格](FA-AGENT-BEHAVIOR-SPEC.md)同日修訂）。

## 9. 拒絕、unknown 與最後合法 checkpoint

| 情況 | 結果契約 |
|---|---|
| Private 結構／已知禁止值仍在 | `SDK_STATE_PRIVATE_MATERIAL`；不 PUT，不發布新 reference |
| 跨 tenant／team／agent／conversation／SDK／job 綁定 | `ASSISTANT_CHECKPOINT_SCOPE`；不讀成其他人的恢復點 |
| 不支持的 codec／version／profile／raw shape | 新設計碼 `SDK_STATE_UNSUPPORTED_FORMAT`（待實作），附安全 reason；不把它偽裝成尚不確定的 PUT |
| 缺件、hash／part／index／必要內容不符 | `SDK_STATE_INCOMPLETE`；不啟動空白同名 SDK session |
| 原 upload intent 已存在但沒有可確認的完整 reference | `SDK_STATE_UPLOAD_UNKNOWN`；保留 pending，禁止重送 PUT |
| 已有同一請求的確定完整 reference | 可唯讀重新核對原紀錄與內容後回傳原 reference；不新增模型／parts／交付效果 |
| Stop／native 執行結果未解 | 沿既有 unresolved／`ASSISTANT_SDK_UNKNOWN`；不偽造 graceful 成功 |
| Snapshot 或完整 envelope 超界 | 明確的容量拒絕，不裁剪資料；原答案／先前合法 checkpoint 保留 |

新增碼尚未存在於產品，需在正式接線時定義、測試及映射；不得讓 `_archive` 將明確不支持的格式一概改成 upload unknown。對使用者可以保留 `ArchiveUnavailable`／無法追問的現有形狀，但內部安全原因必須可辨識。

**最後合法 checkpoint 保留，不代表可自動退回舊歷史重跑。** 若目前最新回合無法保存合規狀態，不能默默載入較舊點，讓已交付答案、已接受效果或 unknown 操作消失；該 conversation 的普通追問仍應被擋，直到既有明確恢復／reset 流程合法處理。

可以保存依原契約可保存的公開答案及不可恢復原因；不能把 preview 當 durable answer，或重跑模型重建「看起來一樣」的歷史。Once、撤銷、財務 `budget_enforced=false`、usage／pending／legacy liabilities 都保持原義。

## 10. 稽核與保留

一般紀錄只保存 code/profile/codec 識別、結果碼、必要 counts／bytes、完整合規 state hash、part receipts、native 成功／停止狀態及公開交付關聯。

不因除錯而落盤 raw checkpoint、provider request／response、removed-private 值或其周邊文字；不預設保存原始 private payload hash 是必要稽核資料。需要定位時只記安全的位置／型別／數量，不把檔名或 exception 的原內容直接輸出。

新 codec 不建立清理排程。未結工作、unknown attempts、最後合法恢復點與舊答案不能被一般 GC 刪除；任何新保留期限、實體刪除或 live 遷移另作決定。

### 2026-09-25 補充：已保存 user 原話的受控 reader（LOCAL，預設 OFF）

此小節是 TC-03／TC-06／TC-07 的原件接線，不改上面的 checkpoint codec、投影、stop、
保存／恢復規則或正式採用條件。模型視窗與 summary 仍由固定 SDK 管理；FA 不建立第二份
transcript，不把 host 端 `get_events()`／SessionFs 的存在當成模型已具備原話回讀工具。

僅 server-owned 新 profile `fa-instruction-user-turns-v1` 提供 SDK custom tool
`read_user_turn(turn_number)`。CID／tenant／Team／human／agent／job／ticket 由原目前 assignment
決定；工具不能指定 scope、role、query、路徑、URL、hash 或自稱 safe。舊 profile、
`read_context` 的 reviewed task-source 語意及固定 prefix bytes 不改。

Reader 的本文權威是原 `WorkRegistration`／`InstructionTask` 的 accepted user content，
不是 SDK 摘要或重新產生的文字。讀取需核原 submit bytes hash、registration material／原
create 回條，以及同一 past turn 的已保存合法 archive、stopped session、terminal job、
`result_forwarded`、result proof 和原保存回條；失敗／取消可沿原合法已保存 terminal 契約，
未完成、未保存、no-SDK 無本輪 archive、缺件或錯版本都不能冒充可讀原件。

回應以 `fa-user-turn-read-v1`／`fa-user-turn-source-v1` 明示 user role、原 request／job／
turn、UTF-8 bytes／SHA-256、registration 與 turn-archive hashes；空白、CRLF、Unicode 不改。
舊內容中的 system／approval marker 仍是歷史 data，不是當前指令、人工批准或 operation grant。
原輸入維持 JSON UTF-8 **8192 bytes** 的既有上限，完整 reader 回應最多 **16384 bytes**；
超限明確拒絕，不截短或回空成功。

只在既有 `_Session`／`TurnArchive` 加可缺省的 `user_turn_sources` metadata，不複製正文。
缺省空集合不增加舊序列化 bytes；最多七個不同 past turns，因 source／receipt／現行授權
必須同時符合原 control-store 98-key 純讀／交易界線。讀同一原件只做當前合法查讀，不新增
metadata 效果；新來源記錄的 unknown 不自動重送。達界線不丟棄既有依賴，也不假稱來源不存在。

原件讀前後、SDK restore，以及後續 reserve／consume／最後 provider egress 都重新核對
目前 read ACL 和原來源 lineage。新回合沿原 archive 繼承這些參照；撤權、內容／版本／
回條漂移或超出完整檢查界線均 fail-closed。舊答案／archive／operation 與費用帳本不因
查讀被重寫；no-SDK failure 的 explicit reset 要求不被 reader 繞過。

新 profile／prompt-assembly record 與舊版本分開，reader／writer 同版接線後才可 server opt-in；
未知／未啟用版本不猜測。其 prefix 穩定與 scope 控制另驗，**不借 PA-02 舊 baseline 的接受**。
固定 SDK offline 接線即使 summary 省略原文，也不自動證明原文已從 model payload evict；
仍須記實際觀察。Full eviction、M 類語意品質及完整 TC 目標不由 scripted output 推定。
具體接點、prefix hash、原批次與限制見 [Shared Context README](../src/fa-shared-context-poc/README.md)。

## 11. 正式採用前的驗收

以下 `Q` 項是**新 codec 正式接線的待驗要求**，不是把現有 16 個 test-only negatives 改名成全部完成。既有 C03 判準及歷史結果不覆寫。

| ID | 必須驗證 | 現有證據能提供什麼 |
|---|---|---|
| Q01 | Fresh API process 禁止所有 `copilot` imports，仍能匯入新 DTO／routes | 舊普通路徑已驗；新 codec 尚未實作 |
| Q02 | 精確兩類 raw shape→`data={}`；未知 key／type／nested key 拒絕，raw 不改寫 | 候選 B 有限定 shape 證據，不是新產品 Module 的通過結果 |
| Q03 | Upload／restore／wire／archive consumer 全走相同 codec 與大小限制 | Test-only store 不覆蓋真 API 全路徑 |
| Q04 | Inherited IDs 只能來自當前合法 prior reference，payload／envelope 改動與偽造 ID 被擋 | 候選有部分反例；正式 authority 綁定仍須驗 |
| Q05 | 正向 default 與敏感 custom 負向分開；禁止值在 envelope／sidecar／summary 一律不發布 | 原敏感案例仍 blocked；default compact 的 marker 移除未 exercise |
| Q06 | Callback／flush／stop 失敗、封存後寫入與 late events 不變成部分成功 | 真正常 stop 已觀察；故障分支須在新接線驗 |
| Q07 | 必要 checkpoint／index／paths／binary／UTF-8／檔案衝突的精確反例 | 現有 missing checkpoint 是 test loader 拒絕，不是所有 runtime 保證 |
| Q08 | 超舊 raw/compressed 界線，並量近 8 MiB state、part、Unicode HTTP envelope | 小於上限的大狀態已有證據；近上限與真 API 邊界仍未驗 |
| Q09 | Partial PUT、unknown prepare／publish、讀回不符不重送，舊答案／reference 不變 | Test-only unknown PUT 有證據；新正式交易仍須驗 |
| Q10 | 新 Python／runtime 從正式 reference 恢復，實際 model-facing source／summary／更正／先前答案正確，工具不重播 | Default fixture 兩個續問已驗；不等於正式 reader 或所有 consumer 已驗 |
| Q11 | 伺服器 profile、受控呼叫、native attribution 與當前保存點相符；偽造／缺省 trigger、未知多次事件及 unsupported automatic／custom／version／platform／provider 明確拒絕 | 唯讀已確認現有接線缺口；原三個保存點的 trigger/request IDs 皆 missing，不能當正式 gate 通過 |
| Q12 | Legacy inline／ordinary v1 保持可讀，raw 初版與 test-only 格式不可偷渡 | 舊相容測試為基礎；新 union／reader 尚未驗 |
| Q13 | 既有 preview、完成、once、撤銷、usage、unknown 與保存原因映射不回歸 | 原 module hashes 未變，不代替新修改後驗收 |
| Q14 | 實際目標平台／Blob／provider 的合法樣本與必要 lifecycle 驗證 | **未驗、未批准新雲端／付費模型**；需要另定有界批次 |

本機 codec／API 行為可用離線測試先驗，不必先升版或呼叫付費模型。若需要聲稱真摘要品質，還要另外做有來源的人審／評測，不能以 Q10 的 scripted transport 代替。

## 12. 採用與回退條件

目前文件狀態是 **v1，最小來源接線契約已確認**，新 codec 接受開關維持關閉。唯讀可行性核對與設計確認均已完成；使用者於 13:50 給出後續本機實作授權，現在由乾淨工作樹的單一產品 owner 接線，不另開競爭中的同一套產品改動。實際完成／未驗項目以持續帳冊與對應 PR 為準。

後續若採用，先讓 reader／DTO 認識經批准的版本與拒絕規則，再在明確允許的 producer／consumer profile 開啟 writer；不能先寫出舊 reader 看不懂的唯一恢復點。

回退時關閉新 writer，不重寫已保存的 bytes；仍保留可驗的 reader／工具與已發布參照，或明確標成不可恢復。不能刪掉新 reference 後假稱回到舊狀態，也不借 rollback 重送模型或外部效果。

本次契約確認只批准這份設計。文件、研究 PR 或某批實驗被接受，都**不等於**已批准產品實作或啟用、SDK 升版、部署、付費模型、敏感原文保存或 live 資料遷移。

## 13. 依據

- [本機驗收結論](ctx03-local-acceptance.md)與[原件識別／核對界線](context-research-evidence.md#ctx03-default-positive)：固定版本 empirical evidence。
- [完整 Target State](grok-aligned-target-state.md)：`TC-01`–`TC-12`、`TC-16`、`SP-29`／`SP-30`、`PA-16`。
- 固定 SDK [SessionFsProvider](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session_fs_provider.py)、[session persistence](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md)、[SessionFs E2E](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/e2e/test_session_fs_e2e.py)、[generated events](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py)。
- Q11 固定來源：[request trigger 的缺省語義](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L23717-L23752)、[compact RPC 方法](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L42532-L42536)、[RPC result](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/rpc.py#L18074-L18114)、[event optional fields](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L7731-L7753)、[event trigger enum](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L12204-L12215)。
- 凍結 WIP 的 `behavior_sdk_state.py`、`behavior_sdk_files.py`、`behavior_checkpoint_privacy.py`、`behavior_sdk_checkpoint.py`、`behavior_assistant_api.py`／`behavior_assistant_hosted.py`，以及候選 B：核對時的原件不隨本文件提供。產品原碼其後由 #4 合併，固定來源與 LF／CRLF 差異見[證據索引](context-research-evidence.md#合併後的來源定位)；原實驗資料仍未公開，也不是新部署驗收。

官方 filesystem Interface 不保證任意 event 改寫後可恢復；本契約不從「搜尋未命中」「E2E 未 assert」或「typed view 較窄」推導全 consumer 安全。
