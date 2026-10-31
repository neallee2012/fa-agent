# 已安裝來源的增量核對：啟動與逐請求只驗新寫入的部分

> 2026-10-30 註：本文提到的 `validate_additions`、`_quiescence`、`behavior_migration_cli`、rotation 與 transition 程式已刪除；內容保留為當時紀錄，不改寫。

狀態：**已撤回**（2026-10-29，Neal）｜原狀態：**提案（待審）**｜提出日期：2026-10-28 +08｜第 1 版｜Neal 2026-10-28 同意先修（「這三個問題可以先修嗎」）；本文只是設計，尚未實作。

## 撤回說明（2026-10-29）

Neal 2026-10-29 決定撤回本 ADR：不做增量核對，dev 改成全新安裝重建。本文不再是待審提案，也不授權任何實作；下方原文保留為歷史紀錄。行號依 `origin/main` `91288716` 核對。

為什麼不需要了：

1. **全新安裝不做完整核對。** stage 由 CI 的 `deploy/stage/run_fresh_job.py` 以全新安裝建立；公開 azd 的首次安裝走 `fresh_install`（`deploy/azd/fa_azd.py:1385`，由 `:1642` 呼叫），之後的 `azd deploy` 走 `upgrade()`（`:1577`，由 `:1627` 呼叫），沿用首次安裝寫下的 fresh 指標，沒有轉成 work-policy 安裝的路徑。這種安裝在 `installed_origin`（`behavior_migration.py:555`）一開頭就分派到 `installed_fresh_origin`（`:560-562`；函式在 `behavior_fresh_installation.py:269`），只讀固定的幾個 key（`:280` 的一次 `read_snapshot`），成本不隨歷史成長。stage 第 37 版 API lifespan 約 3 秒（`docs/progress.md:20`）。
2. **逐請求的整份重驗已被取代。** #1026／#1029 讓 `behavior_server.py:436-463` 在 pointer 與 pins 沒變、同一 process 時重用已驗結果；完整核對只在開機、pointer 移動、新 process／replica 時做。**保證比原本窄：** 對 work-policy 安裝，不經 FA、直接寫進 Table 的列，要到下一次開機才會被發現（程式註解 `behavior_server.py:456` 同樣寫明）。全新安裝更窄：`installed_fresh_origin` 只核對固定的不可變 key，從不跑 `receipt_catalog` 與 `validate_additions`，所以下表的 (b1)「每列都有回條」、(b2)「沒有無法判讀的列」在全新安裝上開機也不檢查。stage 與公開部署本來就是如此；dev 重建成全新安裝後，所有環境都不再做這兩項。這個取捨在 dev 重建前交 Neal 確認；若日後需要這兩項檢查，另立 ADR。
3. **本文點名的 `teams_metrics` 路由只存在於 `ServerConfigurationV2`（2026-10-30 已連同 v2 一起刪除）。** `behavior_server.py:830-832` 只在 V2 才建 `TeamsMetrics`（原文 `:519`、`:592` 的 `require_current_native_evidence()` 現在在 `teams_metrics.py:500`、`:573`，行號已移動）。stage 的設定是 v1（`deploy/stage/runtime/config.json:2`），Teams 走 `native_teams_relay`（`behavior_server.py:969-970`）。
4. **剩下的成本只在 dev 的長期（work-policy）安裝。** 開機約 150 秒，隨歷史線性變長；150 秒與線性見 `docs/progress.md:123`；約 9.5 萬個 entity 會碰到 256 MiB 讀取上限，是下方原文背景的估計。處理方式是把 dev 重建成全新安裝（另開作業，Neal 2026-10-29 核准），不是新增增量核對程式。

以下為原文（2026-10-28 提案），未修改。

本文合併後不授權部署、雲端寫入或任何雲端讀取。實作另走 PR，dev 讀回依 AGENTS 的批准界線。

標示：[原始碼] 是讀過的本 repo 程式（基準見文末）；[文件] 是官方文件明寫；[推論] 是依程式或文件推得、尚未實測；[dev] 是 dev 上量到的數字（來源見背景）。

## 背景

API 開 port 前會跑 `ServerComposition.installation()` → `installed_origin` → `installed_work_policy_origin`（`behavior_server.py:421`、`behavior_migration.py:548`、`behavior_work_policy_installation.py:358`）[原始碼]。這條路徑：

1. 用 `read_inventory(max_entities=None)` 讀完整份 authority 分區（`:359`）。
2. 核對安裝的完整鏈：pointer → history → plan／approval → 原 catalog → 原 bootstrap handoff → 唯一一筆安裝 EGT 回條（`:360-400`）。
3. 跑 `validate_work_policy_history` 與 `validate_additions`，逐列核對安裝後新增的每一列（`:405-412`）。

dev 2026-10-28：32,693 個 entity、88 MB；讀取約 7～13 秒，核對約 150 秒（約 4.6 ms／列）[dev，PR #1021 與 progress.md:34]。10-01 是 24,464 筆、約 199 秒（含重複的那次）。成長是線性的：這些列是逐回合累積、只增不改的歷史，不能刪（AGENTS 第 5 條）。

會碰到的兩個上限：

- startup probe 600 秒（`prepare_environment.py:222`、`fa_azd.py:1371` × `behavior-api.bicep` `periodSeconds` 5）[原始碼]。
- 這次讀取雖然 `max_entities=None`，`max_bytes` 仍是預設 256 MiB（`behavior_migration.py:51`、`:310`）。照目前約 2.7 KB／entity，約 9.5 萬個 entity 時開機會直接 413 `BEHAVIOR_INVENTORY_LIMIT` [推論]。

同一份完整核對也在開 port 後**逐請求**重跑（PR #1021「會跑」清單）：`result_route`、`require_current_native_evidence()`、bounded assistant 的 `acquire_evidence`、Teams `publish`／`revoke`／`receipts`，以及 **Teams 收訊 `receive`（`teams_metrics.py:519`）與 artifact 內容 GET（`:592`）**。也就是每則 Teams 進來的訊息要等約 150 秒。stage 開 Teams 前必須先修。

## 現在的完整核對各保護什麼（回答 Neal「是否必要」）

| 部分 | 擋下什麼 | 程式證據 | 是否仍必要 |
|---|---|---|---|
| (a) 安裝完整鏈 | 被換掉或沒批准的 policy：pointer／history／plan／approval 對不上、操作者 OID 或時間窗不符、config sha 不符、安裝回條不是唯一那筆 | `installed_work_policy_origin` `:363-400` 的 `require` | 必要。它核的都是有 hash 釘住的不可變列，大小只跟「安裝當時」的 inventory 有關，不隨歷史成長 [原始碼] |
| (b1) 每列都有回條 | 不經 EGT 寫進來的列、改壞的列、殘缺寫入：每一列都必須等於最新回條記的 hash；同一 guard revision 不能有兩張回條 | `receipt_catalog`（`behavior_runtime_rotation_history.py:65-90`） | 必要 |
| (b2) 沒有無法判讀的列 | 沒有 `kind`、或同時符合多種型別的列 | `:1426-1441` `"unclassified late current-runtime row"` | 必要 |
| (b3) 舊設定的權限不能變成現在的權限 | 舊 binding／job／root 的 grant、plan、work、launch 被當成目前可執行的權限 | `:1444` `late row escaped the original root`、`:1448` `old grants/jobs cannot become current execution authority`、`:1511` `late plan cannot use a retired binding`、`:1513` | 必要 |
| (b4) 各種狀態機的歷史 | 例如費用帳本被重設或有不明的扣款寫入、回合沒有原始 admission 就有 start／end | `behavior_work_policy_history.py:255` `initial cumulative budget cannot replace or reset old exposure`、`:277` `unknown monetary writer`；`behavior_turn_receipts.py:362-460` | 必要 |
| 逐請求再跑整份 | 安裝在 process 跑起來後被換掉 | `installation()`（`behavior_server.py:421-459`） | 換安裝的偵測必要；逐請求重驗全部歷史看不出專屬目的 [推論]。peer 已經用同一 process 的 `_VerifiedOrigin` 加 `_check_installation()` 逐請求核對，不重掃（`:430-443`；規格「2026-10-01 緊急接點」） |

誰會寫出這些列 [原始碼＋推論]：

- FA 自己的 authority 寫入只有一條路：`BehaviorStore._submit` 送 EGT，同一批 CAS guard（revision+1）並建立一張 `_Receipt(guard_revision=…)`（`behavior_store.py:693-800`、`:453-488`；grep `submit_transaction` 沒有別的 authority writer）。API replica、Hosted 薄轉送（經 API）與同步 job 都走這條。
- 舊版 FA 寫的列，形狀和規則可能不同。例如 `_legacy_cursor_row`（`behavior_work_policy_installation.py:39`）處理舊 catalog 的 cursor。
- 不經 FA 的寫入：有 Table 資料寫入權的人或工具（Storage Explorer、腳本）。程式看不出有這類寫入，屬 [推論] 的防線。

**完整核對不是防惡意寫入者的簽章。** 列和回條都沒有 MAC；有 Table 寫入權的人可以寫出一組互相一致、能通過核對的列與回條。另外 `receipt_catalog`／`_checked_receipts`（`behavior_recovery_history.py:56-86`）只檢查 revision 不重複，**沒有檢查 1..R 連續**，所以「連列帶回條一起刪掉」今天的完整核對也抓不到（我讀程式看到的現況，不是本設計的主張）。它防的是寫錯、舊版、型別不明和 FA 以外的寫入。

## 結論

- **已驗證點＝guard revision R。** 每次 EGT 都讓 revision 加 1，並留下一張帶這個 revision 的回條，所以回條是全序。「R 之後的變化」就是 revision 在 `R+1..G` 的回條和它們寫的列（G 是現在的 guard）。
- **規則版本也是 key 的一部分。** 換版後第一次開機一定完整驗一次，用新規則驗舊列；通過才寫入新的已驗證點。
- **逐請求（第 1 階段，最先做）**：process 內保留上次驗過的 inventory 和各驗證器的狀態。每個請求只讀 guard，加上一個伺服器端過濾的「R 之後修改過」查詢，只驗新的部分。Teams 收訊不再每則等 150 秒。
- **啟動（第 2 階段）**：分區內加一列「已驗證點」。開機仍完整讀一次（7～13 秒），但只對 R 之後的部分跑昂貴的核對；用 `D_R`（R 當時整份 inventory 的 hash）證明 R 以前的內容沒變。
- **不讀整份（第 3 階段，之後再議）**：(a) 可以改成按 key 讀；(b) 要按 key 讀還得再設計。到 256 MiB 上限前要完成，或先調高這次讀取的上限。
- 保證不變：完整核對會拒絕的列，在被信任前仍會被拒絕。對不上、缺漏或讀不懂時，就回去完整驗一次，不靜默略過。唯一變窄的一點寫在「保證的變化」。

## 設計

### 1. 已驗證點的資料形狀

一列新的 authority row，用既有 `transact` 寫，跟其他列一樣有回條：

- key：`control_key("validated-origin", authority_domain, rules_sha256)`。每個規則版本一列。換版期間新舊 replica 各寫各的，不會互相蓋掉。
- `kind: "validated_origin"`，欄位：
  - `schema_version`
  - `rules_sha256`、`configuration_sha256`
  - `guard_revision`（R）
  - `inventory_sha256`（`D_R`）：R 當時「每個 row key → 最新回條 fingerprint」加上「每個 receipt key → entity hash」的 digest，不含 guard 和 validated-origin 列本身
  - `origin_sha256`：(a) 結果的 digest（plan sha、history sha、安裝回條 hash）
  - `folds`：需要接續累計的驗證器狀態（第 5 節 S3）。目前只有 work-policy 費用帳本：R 當時的 `CostLedger`，加上 admitted／dispatched 集合的 digest
- 大小：只有固定欄位和一份 `CostLedger`，預估遠小於 64 KiB 的 Payload 上限 [推論；實作時加上大小斷言]。
- **寫入者只有一個角色**：root `ServerComposition` 驗證成功後寫。peer、Hosted 薄轉送與一般路由都不寫：薄轉送的 key 是 API 依團隊組出來的（ADR-0006），組不出這個 key。
  - 寫入前先 `read_record`；已存的 R ≥ 這次的 R 就不寫。
  - `request_id = "validated-origin:" + rules_sha256 + ":" + R`。重送時 `transact` 會先 `_receipt` 對帳（`behavior_store.py:703`）。
  - 已驗證點只是省時間的捷徑：寫入失敗（409／412）只記 log，不讓啟動失敗。
- ADR-0006 第 102 行要求：新的列型別要同時加進 `validate_additions` 和 `_quiescence`（`behavior_migration.py:1165`）的白名單。它自己的分類只核 key、欄位形狀和回條；內容由下一次開機用 `D_R` 重算來驗。

### 2. 規則版本（`rules_sha256`）

`rules_sha256 = digest({"code": code_sha256(), "pydantic": pydantic.VERSION})`。

- `code_sha256()` 已存在（`behavior_code_identity.py`），會 hash `shared_context/*.py` 全部檔案。
- 刻意取最寬的範圍：任何 `validate_*` 規則、model 或 `_kind_models` 的變動都會換 key，換版後第一次開機一定完整驗一次。代價是每個 release 都要完整驗一次，換到的是不用維護「哪些檔案算規則」的清單。
- pydantic 版本會改變 `model_validate_json` 的判斷，所以也算進去。

### 3. 啟動流程（第 2 階段）

1. `read_inventory(max_entities=None)` 照舊（保留讀取期間 guard 改變就 412 的行為，`behavior_migration.py:317, 340`）。
2. 讀取這個 `rules_sha256` 的 validated-origin 列。以下任一情況就**完整驗一次**（跑今天的整條路徑），成功後寫入新的已驗證點：
   - 沒有這列，或讀不懂
   - `configuration_sha256` 不同
   - R 大於現在的 guard revision
   - 用現在的 inventory 重算 `D_R` 與記錄的不同
   - R 之後的回條 revision 不是剛好 `R+1..G`
   - 有任何列的最新回條 fingerprint 跟它的 hash 不同（`receipt_catalog` 本身就會擋）
   - 有一列不在 R 以前的集合裡，也沒有 R 之後的回條寫過它
3. 重算 `D_R` 的方法：對每個 row，取 revision ≤ R 的最新回條記的 fingerprint；對 revision ≤ R 的每張回條，取它現在的 entity hash。entity hash 等於回條 fingerprint 是 `receipt_catalog` 已經強制的（`:84-88`），所以舊 image 被覆寫了也算得出來。不需要 Timestamp。
4. 增量驗證 Δ（R 之後的回條和它們寫的列），見第 5 節。(a) 不隨歷史成長，每次開機照跑。
5. 都通過後，`bind_installation`、`restore_turn_tickets` 照舊，然後寫入新的已驗證點（R := G）。

開機成本：讀取 O(N)（7～13 秒）、`receipt_catalog` 與 `D_R` 的雜湊 O(N)（便宜但未量測），昂貴的核對只做 O(Δ)。

### 4. 逐請求（第 1 階段）

process 內快取 `_ValidatedState`，key 是 `(pid, config 物件, store 物件, installation fence, rules_sha256, guard revision, guard etag)`。內容：

- 上次驗過的 inventory view（rows／receipts／entity hash／etag），也就是開機那次完整讀取的結果；
- 各驗證器的狀態（第 5 節）；
- `T`：驗證時 guard entity 的 `Timestamp`。

每次 `installation()`（root）：

1. `_check_installation()`，加上 `attach_standing_policy`、`activate_target`，照舊（都是按 key 讀）。
2. 讀 guard（G1）。用 `PartitionKey eq P and Timestamp ge @since` 查 rows 和 receipts，`since = T − 5 分鐘`。`read_record_page` 已用同樣寫法（`behavior_store.py:627-680`）。再讀 guard（G2）；G1 ≠ G2 就回 412 `BEHAVIOR_CONFLICT`，沿原本的重試（`behavior_assistant_server.py:163-165` 最多 3 次）。
3. Δ＝查詢結果中 etag 跟快取不同或快取沒有的 entity，加上 Δ 回條寫過、但查詢沒回的列（按 key 補讀）。
4. 證明 Δ 完整：Δ 回條的 revision 必須剛好是 `R+1..G1`。Δ 裡的列只要沒有被 Δ 回條寫過（表示不經 EGT 的寫入），就完整驗一次。完整核對本來就會拒絕這種列，結果和今天相同。
5. 把 Δ 套到快取 view 上（copy-on-write），只跑第 5 節的增量驗證。成功就換上新的 `_ValidatedState`；失敗就照今天的做法丟掉 origin（`_verified_origin = None`），並讓請求失敗。
6. 同一 process 用一把鎖 single-flight，同時進來的請求等同一次結果。

Timestamp 只是線索，不是證明 [文件：Timestamp 由伺服器維護、client 不能設定，每次修改時會前進；文件沒有保證不同 entity 之間的順序]。多抓到的會被 etag 比對濾掉。少抓的 EGT 寫入，會被第 4 步的 revision 連續性抓到。少抓的非 EGT 寫入，靠 5 分鐘的餘裕擋時鐘誤差 [推論；這是唯一依賴 Timestamp 的地方]。

### 5. 增量驗證：哪些核對要用到歷史

`validate_additions` 不是逐列獨立的：17 個驗證器拿到的都是整份 `inventory`，逐列迴圈也會回頭讀舊列（`m._row(inventory, …)`）。「只看新列」不正確，每類要有自己的規則：

| 驗證器 | 怎麼挑列 | 成長來源 [推論] | 增量策略 |
|---|---|---|---|
| `receipt_catalog` | 全部 rows＋receipts | 每次寫入 | S0：Δ 回條 revision 連續、寫入的列都存在、Δ 列等於最新 Δ 回條；R 以前的部分由 `D_R` 或 etag 證明沒變 |
| 逐列迴圈（`:1349-1531`） | 不屬任何 history 的新增列 | 每回合的 usage／cost／progress 等 | S4：驗 Δ 列；再加上「讀過的可變列出現在 Δ」的舊列。驗證時記下 x→y 的讀取邊；會讀的有 job、launch、grant、claim、settlement auth、stop 回條（`:1446-1530`） |
| `validate_turn_receipt_history` | `kind` ∈ vm_turn_* | 每回合 | S2：以 ticket 為一族；Δ 碰到 ticket／start／consumption／session／job（job→tickets 反向索引）時重驗那一族；Δ 中這些 kind 的列都必須被某族認領 |
| `validate_agent_message_history` | MESSAGE_MODELS 的 kind | 每則訊息 | S2：以 request／collaboration root 為族 |
| `validate_delegation_history` | delegation kind（已支援 `row_ids`） | 每次委派 | S2：沿用 `row_ids=Δ` 參數，另加反向讀取 |
| `validate_work_policy_history` | registrations＋cost writers 依 revision 累計 | 每回合（費用） | S3：從 R 的 `CostLedger` 接續，只累計 revision > R 的 cost writer；registrations／policy 部分用 S1 |
| `validate_instruction_history` | steady_work_registration | 每個工作 | S1（未量測；太慢就改 S2） |
| `native_hitl`、`team_mcp`、`native_routines` | `kind` 前綴 | 每次 HITL／MCP／routine | S1（未量測；太慢就改 S2） |
| settlement、recovery、unclaimed、close、result、target、agent registry、team registry、team authority、`_operator_history` | `kind` 或特定 key | 少量 [推論] | S1 |
| (a) 安裝完整鏈 | pointer、history、catalog 與其中的列、安裝回條 | 不成長 | 開機照跑；逐請求只在 Δ 碰到它讀的 key 時重跑 |
| `restore_turn_tickets` | 有 `turn_authorization_digest` 的 session | 每回合 | 只對 Δ 中的 session publish（index 是冪等的 [推論；實作時核對 `ticket_index.publish`]） |

- S1＝只要 Δ 不是空的，就在記憶體 view 上把這個驗證器整個重跑一次。依 kind 挑列只是 dict 掃描，成本是 O(它挑到的列)，不是重讀 Table。這是最簡單而且精確的做法，只給挑到的列很少的驗證器用。
- S2＝依族重驗：把現有函式拆成「驗一族」和「Δ 碰到哪些族」，加上「Δ 中我負責的 kind 都被認領」。
- S3＝接續累計：狀態存在已驗證點的 `folds`；逐請求時放在記憶體。
- 哪個驗證器用 S1 還是 S2，要等第 0 階段量到各自的秒數再定。量到之前不做 S2 拆分。

另外一個不改語意的常數項修正：`instruction_model_cost`、`instruction_turn_usage`、`agent_turn_usage` 都有 `kind`，但不在 `_kind_models()`（`behavior_migration.py:684-707`），所以每列都要在約 40 個 model 間逐一試 `model_validate_json`（`:1426-1441`）。這可能就是 4.6 ms／列的大頭 [推論，未量測]。加進 kind 分派前，要先證明其他 model 都不會接受帶這個 `kind` 的資料，「剛好一個符合」的結果才不變。

### 6. 改舊列、刪舊列怎麼抓

- **改 R 以前的列**（不經 EGT）：開機時，它的 hash 跟最新回條不同，`receipt_catalog` 會擋；連回條一起改，`D_R` 會不同，回去完整驗。逐請求時，它的 Timestamp 會前進，進入 Δ，但沒有 Δ 回條寫過它，所以回去完整驗。
- **刪 R 以前的列**：開機時 `D_R` 會不同，連「列帶回條一起刪」都抓得到，比今天嚴。逐請求時抓不到，要等下一次開機（見下節）。
- **改寫已持久的列來讓新規則通過**：AGENTS 第 5 條不允許。真的發生時，它就是 R 以後的一次寫入（有回條），或是一次不經 EGT 的寫入；兩種都會進入 Δ 被驗。
- **偽造已驗證點**：已驗證點只能宣稱「R 時的狀態已驗過」，而 `D_R` 在開機時會用現有資料重算，所以它只能替一個真的存在過的狀態背書。刻意偽造 `D_R`，要先算出包含壞列在內的精確 hash，這只有刻意的寫入者做得到；而前面說過，刻意的寫入者今天就能寫出一組能通過核對的列。對這類寫入者，安全性沒有比今天差 [推論]。

### 7. 併發與 crash

- 讀 Δ 時，前後各讀一次 guard，不同就 412，跟 `read_inventory` 一樣（`behavior_migration.py:317, 340`）。EGT 是原子的，guard 沒變就表示沒有寫到一半的批次。
- 寫入的防線 `_check_installation()`（`behavior_migration.py:183-190`）與 `MigrationStore._guard()` 的 412（`:192-198`）完全不動。
- 已驗證點只在驗證成功後寫入。寫之前 crash，下次開機就完整驗一次。EGT 是原子的，不會只寫一半。同一個 R 重送時，`_receipt` 會先對帳。
- 多個 replica 同時開機時，各自完整驗一次，R 大的勝出。別人的 R 比較小也是真的驗過的，不會錯。
- 換版期間，新舊版各用自己的 `rules_sha256` 列，不會互相覆寫。

## 保證的變化

- **不變**：完整核對會拒絕的列，在開機或請求信任它之前仍會被拒絕。這包括 R 之後經 EGT 寫入的列、不經 EGT 改寫的舊列，以及新版規則對舊列的判斷（換版後第一次開機完整驗）。
- **變窄一點**：開機以後、不經 FA 直接**刪除**舊列，逐請求抓不到，要到下一次開機（`D_R`）才會發現。今天的逐請求完整核對抓得到「刪了列、留著回條」，但抓不到「列帶回條一起刪」。可以接受的理由：
  - VM 沒有刪除權（ADR-0006：薄轉送不提供刪除，也不給 Storage Table Data Contributor）。
  - FA 本身沒有刪除路徑。
  - AGENTS 第 5 條規定已持久的列不刪。
  - 開機時反而比今天多抓到「列帶回條一起刪」。
- **變寬一點**：開機多了 `D_R` 的檢查，見上一點。

## 分階段

0. **量測（程式小改）**：開機那次完整核對每一段各寫一行 INFO：讀取、`receipt_catalog`、(a)、每個 `validate_*`、逐列迴圈、`restore_turn_tickets`，附各自挑到的列數和 RSS。dev 部署後讀回，決定第 5 節每個驗證器用 S1 還是 S2。
1. **逐請求增量**：第 4、5 節（S0、S1、S3 在記憶體、S4；S2 只做量測後確定需要的）。不改 Table 資料形狀，不寫新列。解決 Teams 每則等 150 秒。
2. **開機增量**：validated-origin 列、`D_R`、`folds`，加上 `_quiescence`／`validate_additions` 白名單。
3. **不讀整份**（之後再議）：(a) 按 key 讀（catalog 裡的列都有 hash 釘住，不需要同一個快照；pointer 讀兩次確認沒變）。(b) 要把記憶體 view 改成按需讀取，412 的範圍會跟著變，需要另外設計。必須在 256 MiB 上限前完成，或先把這次讀取的 `max_bytes` 調高。

## 驗證方式

- **差異測試**（最主要）：用現有能建出 work-policy 安裝的 fixture，產生一串寫入。對每個切點 r，`完整驗(I_now)` 的結果都要等於 `從 r 的狀態增量驗(Δ)`，通過與拒絕訊息都要一樣。
- **變異測試**：在 Δ 放無型別的列、跨 root 的列、舊 binding 的 grant、跳號的回條、不經 EGT 改寫的舊列、刪掉的舊列（開機）、竄改過的 `D_R`／`rules_sha256`／R > G。每一種都必須被拒絕，或回去完整驗。
- **併發**：Δ 讀取期間有寫入要回 412；兩個 replica 寫已驗證點時，R 大的勝出。Windows 與 Linux 各跑 5 次。
- **dev**：第 0 階段的 INFO 讀回；第 1、2 階段部署後量開 port 時間與 Teams 收訊延遲，並在 dev 上比對一次「增量結果＝完整結果」。dev 讀寫依現有批准。

## 開放問題

- 150 秒裡，逐列迴圈和各驗證器各佔多少：未量測（第 0 階段）。
- 安裝 catalog 有幾列（決定 (a) 的成本）：未量測。
- 伺服器端 Timestamp 過濾在 3 萬多個 entity 的分區上要多久：未量測。如果逐請求太慢，備案是只在 guard 變了才查。但這樣就抓不到「guard 沒變、但有不經 EGT 的寫入」，屬於保證的變化，要另外說明。
- 把 inventory 常駐在記憶體的 RSS（assistant profile 是 2 GiB，`behavior-api.bicep:30`）：未量測。

## Verified against

- 程式基準：`origin/main` `bb64c051`（worktree `neallee2012-incremental-origin-check`）；只改文件。
- 已讀程式：
  - `behavior_work_policy_installation.py`：`installed_work_policy_origin`、`read_standing_catalog`、`_legacy_cursor_row`
  - `behavior_runtime_rotation_history.py`：`receipt_catalog`、`validate_additions` 全文
  - `behavior_migration.py`：`read_inventory`、`_FencedClient.submit_transaction`、`MigrationStore._check_installation`／`_guard`／`transact`、`installed_origin`、`_kind_models`、`_quiescence` 開頭
  - `behavior_store.py`：`_Guard`／`_Receipt`、`_submit`、`transact`、`read_record_page`
  - `behavior_recovery_history.py`：`_checked_receipts`、`_Inventory`
  - `behavior_turn_receipts.py`：`validate_turn_receipt_history`
  - `behavior_agent_message_history.py`：`validate_agent_message_history` 開頭
  - `behavior_work_policy_history.py`：`validate_work_policy_history` 前段
  - 其餘 13 個 `validate_*` 只讀了挑列的方式
  - `behavior_server.py`：`installation()`、`_VerifiedOrigin`
  - `behavior_code_identity.py`：`code_sha256`
  - `behavior_turn_ticket.py`：`restore_turn_tickets`
  - `infra/shared-context/behavior-api.bicep`：資源 profile
- 實際驗證：未執行（設計階段，沒有程式變更）。
- 官方來源：[Understanding the Table service data model — Timestamp](https://learn.microsoft.com/rest/api/storageservices/understanding-the-table-service-data-model#timestamp-property)（2026-10-28 查閱）：Timestamp 由伺服器維護、不能修改，每次修改 entity 時前進；設定的值會被忽略。
- 限制：dev 數字取自 PR #1021 與 progress.md，本次沒有讀雲端。成本拆分、catalog 大小、Timestamp 查詢延遲、RSS 都未量測。
