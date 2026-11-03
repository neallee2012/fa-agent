# 程式品質基準（2026-10-26）

這頁記下整理 code base 前的起點，之後每一輪整理都拿這裡的數字比較。工具是 [crapper](https://github.com/unclebob/crapper)（複雜度、覆蓋率、CRAP 分數）、[dryer](https://github.com/unclebob/dryer)（結構重複）與 [mutator](https://github.com/unclebob/mutator)（mutation 測試）。目標：每個函式 CRAP < 10，結構相似度 ≥ 0.8 的重複消掉。

## 範圍

- **量的**：`src/fa-shared-context-poc/shared_context`、`src/fa-shared-context-poc/web_test_console`、`deploy`、`scripts`，第 36 版（2026-10-25 部署到 stage 的那一版）。
- **不整理**：
  - `web_test_console/novnc`：外部的 noVNC，原樣放在 repo 裡。
  - `deploy/`：只有雲端部署才驗得到，這一階段只列數字，不改。
- 原始碼約 13.5 萬行（439 個 `.py`），測試約 18.8 萬行（689 個 `.py`）。

## 起點數字

覆蓋率還沒量，所以下面只有複雜度（cyclomatic complexity，CC），還沒有 CRAP。CC 1 的函式要 CRAP < 10 不需要測試；CC 越高，要越接近 100% 覆蓋才會低於 10。CC 2 也一樣（最多 6）。CC ≥ 10 的函式，覆蓋率 100% 時 CRAP 就等於 CC，也至少是 10，只能靠拆。長期目標是每個函式都 CC ≤ 9 且有足夠覆蓋；這一階段只先處理最重的幾個。

| 項目 | 數量 |
|---|---|
| 函式（不含 novnc） | 6,209 |
| CC ≥ 10 | 1,161 |
| CC ≥ 20 | 394 |
| CC ≥ 50 | 68 |
| CC ≥ 100 | 18 |
| dryer 重複（≥ 0.8，至少 4 行） | 376 對，其中 282 對完全相同 |
| 合併成群後 | 86 群；片段 ≥ 8 行或出現在 ≥ 4 個檔的有 41 群 |

### CC 最高的 20 個（shared_context、web_test_console）

| 函式 | 模組 | CC |
|---|---|---|
| `run_copilot` | `shared_context.m2_copilot` | 450 |
| `_quiescence` | `shared_context.behavior_migration` | 271 |
| `_validate` | `shared_context.behavior_recovery_history` | 186 |
| `validate_retirement` | `shared_context.behavior_runtime_rotation_history` | 178 |
| `send` | `web_test_console.adapter.PullAdapter` | 153 |
| `validate_agent_message_history` | `shared_context.behavior_agent_message_history` | 148 |
| `_run_turn` | `shared_context.behavior_assistant_hosted.AssistantSession` | 148 |
| `create_app` | `web_test_console.bridge` | 123 |
| `_run` | `shared_context.native_sdk.NativeSdkExecutor` | 119 |
| `validate_work_policy_history` | `shared_context.behavior_work_policy_history` | 112 |
| `without_files` | `shared_context.behavior_native_sessions.NativeSessions` | 111 |
| `_invoke` | `shared_context.behavior_native_turn_api.NativeTurnGateway` | 110 |
| `validate_additions` | `shared_context.behavior_runtime_rotation_history` | 106 |
| `validate_unclaimed_history` | `shared_context.behavior_recovery_history` | 104 |
| `install_work_policy_routes` | `shared_context.behavior_work_policy_api` | 104 |
| `task_view` | `web_test_console.native.NativeInstructionControl` | 103 |
| `delivery_history` | `shared_context.behavior_teams_recovery_history` | 101 |
| `install_assistant_routes` | `shared_context.behavior_assistant_api` | 100 |
| `register` | `shared_context.behavior_work_policy.WorkPolicyControl` | 99 |
| `_request` | `shared_context.behavior_foundry.FoundryHttpClient` | 98 |

`deploy/` 與 `scripts/` 最高的是 `deploy/azd/fa_azd.py` 的 `check_existing_vnet`（51）與 `deploy/shared-context/deployment_config.py` 的 `validate_config`（50）。

### 份量最重的重複群（依「片段總行數 × 檔案數」排）

| 片段 | 行數 | 檔案數 |
|---|---|---|
| `behavior_skill_copy_source.py` 與 `behavior_skill_draft_source.py`（4 段） | 27、22、17、15 | 2 |
| `web_test_console/native_consents.py` 與 `native_questions.py`（4 段） | 30、18、15、13 | 2 |
| `behavior_runtime_rotation_api.py` 與 `behavior_runtime_transition_api.py` 的 operator app 工廠（後者已於 2026-10 移除，這群不再存在） | 58 | 2 |
| prompt 模組共用的 6 行段落（`behavior_agent_role_prompt.py` 等） | 6 | 13 |
| 10 個模組共用的 6 行段落（`behavior_capabilities.py`、`m3_tools.py` 等） | 6 | 10 |
| `behavior_agent_registry_operator.py` 與 `behavior_team_registry_operator.py` | 28 | 2 |
| `memory_note_proposal_api.py` 同檔兩段 | 50 | 1 |
| `teams_personal.py` 與 `teams_runtime.py` | 19 | 2 |
| `deploy/stage/` 四支腳本與 `deploy/azd/fa_azd.py` 的同一段 | 5 | 5 |

## 分階段計畫

每一步是一個 PR，只做不改行為的重構，用現有測試鎖住行為；每一步跑完重新量，數字寫回這頁。

1. **消重複**：先做兩個模組成對、整段複製的群（consents／questions、skill copy／draft），再看 prompt 與共用 6 行段落是否值得抽成一處。prompt 文字是經過批准、有 digest 的內容（AGENTS 第 5、6 條），抽出後組出的文字必須逐位元組相同，並有測試比對。operator app 工廠兩邊的錯誤代碼、訊息與例外處理都不同，硬合要帶很多參數，先不動。
2. **一個模組量覆蓋率、算真正的 CRAP**：挑測試多、不靠雲端的模組（`native_turns`、`behavior_native_sessions` 或 `behavior_work_policy`），只跑它自己的測試；依工作把最高的 2～3 個函式拆開，mutator 只跑改到的行。
3. **`validate_*_history` 這類高 CC 函式最後才做**：它們守著已持久的批准與 digest（AGENTS 第 5 條），要先確認測試涵蓋每一條拒絕路徑才拆。
4. **暫不做**：整個 repo 的 mutation 測試（測試量太大）；`m2_copilot.run_copilot`（CC 450，200 多個檔引用，要先量覆蓋率再設計，不是整理）。

## 第一輪結果（2026-10-26）

第 1 步做了成對、整段複製的群；第 2 步做完 `native_turns` 最重的 4 個函式的拆分，但拆後的 CRAP 還沒以覆蓋率重量，`_start_turn` 也還是 CC 13，所以 CRAP < 10 的目標還沒達到。除了 #991 是先重構、再補測試以外，每個 PR 都是先補測試鎖住行為、再做機械式重構，最後跑 mutation，有漏網的就補測試（#990、#1000 沒有漏網）。

| 項目 | 起點（第 36 版） | 第一輪後（main `2b638fc8`） |
|---|---|---|
| 函式（不含 novnc） | 6,209 | 6,224 |
| CC ≥ 10 | 1,161 | 1,157 |
| CC ≥ 20 | 394 | 390 |
| CC ≥ 50 | 68 | 67 |
| CC ≥ 100 | 18 | 18 |
| dryer 重複（≥ 0.8，至少 4 行） | 376 對，282 對完全相同 | 365 對，273 對完全相同 |

函式變多，應該是拆分新增的小函式，加上同期其他 PR 加的程式（推測，沒有逐一核對）。兩次都用下面「重新量」的指令，報告去掉 novnc 的列後計數。

### 消重複

| 重複群 | 結果 |
|---|---|
| consents／questions | 合併成 `web_test_console/native_hitl_rows.py` 的 `NativeHitlRowControl`，537 → 436 行，補 13 個測試；mutation 66 點殺 65，1 點等價（#991） |
| `memory_note_proposal_api.py` 同檔兩段 | 合併成 `_install_note_routes(...)`，114 → 74 行；mutation 9 點殺 8，1 點等價（#990） |
| skill copy／draft | 不合併。兩邊的 effect 類別、錯誤碼前綴、Team id 位置都不同；命令名與 request id 是持久 receipt ID 和 digest 的輸入，一個字都不能變；三組錯誤訊息會出現在 HTTP body 裡，統一就是行為改變。硬抽要每個函式帶約 8 個參數，兩邊都不會變好讀 |
| operator app 工廠 | 不合併：錯誤代碼、訊息與例外處理都不同 |
| prompt 模組的 6 行段落 | 不合併：13 處各只 6 行，抽出後還要加逐位元組比對 prompt 的測試，划不來 |
| 10 個模組的 6 行段落 | dryer 誤判：結構相同，但每處檢查的規則都不同 |

不合併的四群沒有開 PR，理由來自這一輪讀程式碼的判斷，不是量測。

第一輪沒看的三群，第二輪在 #1002 處理：`teams_personal`／`teams_runtime` 的 activity route 合併成 `teams_runtime.activity_response`；4 支 `deploy/stage` 腳本與 `deploy/azd/fa_azd.py` 的 `_write_x` 合併成 `deployment_config.write_x`；registry operator 的 `main()` 先不合，因為四個讀 token 的 `main()` 都沒有測試（其中三個讀 `FA_TEAM_OPERATOR_TOKEN` 的幾乎相同，要先補測試才能合）。

### 拆 `native_turns` 的高 CC 函式

四列的拆前數字出自同一次量測：在 #995 之前的 main 上，用 Linux 容器（本機 Hosted 映像、固定版 runtime）跑 `native_turns` 的 103 個測試檔量覆蓋率，所以拆前有真正的 CRAP。拆後只量了 CC；只有 #999 在 Windows 量了新函式的行覆蓋（100%，CRAP 等於 CC）。

| 函式 | 拆前 CC／覆蓋／CRAP | 拆後 CC | mutation | PR |
|---|---|---|---|---|
| `_handle_turn` | 56／76.9%／94.5 | 6（拆出 8 個，最高 13） | 42 點全殺 | #995 |
| `_enqueue_attempt` | 29／61.4%／77.4 | 6（拆出 4 個，最高 8） | 34 點殺 33，1 點等價 | #999 |
| `_run_started` | 38／96.4%／38.1 | 8（拆出 6 個，最高 8） | 46 點殺 44，2 點近乎等價 | #996 |
| `dispatch` | 37／93.2%／37.4 | 3（改成依型別查表，另加 CC 1 的 `_handler`） | 15 點全殺（全部手寫） | #1000 |

這四個 PR 在 Linux 容器的失敗都和它的 main 基準相同（#996 最後一個只加測試的 commit 只在容器跑了 3 個檔）：`ci-quarantine.txt` 裡的 6 個；#995 另有 1 個 `test_native_reader_cache.py` 的 `[clock]`，main 上也失敗，已由 #992 修好。`_handle_turn` 拆出的 `_start_turn` 仍是 CC 13，覆蓋 100% 時 CRAP 就是 13，還要再拆一次。

### 做法上學到的

- **mutator 在 Windows**：原生跑法要建 symlink，沒有權限會丟 `WinError 1314`。改用 mutator 自己的 `discover_raw`／`apply_site` 產生同樣的 mutant，逐一套用（就地改檔後還原並以 SHA-256 核對；#991 是寫進複本再強制匯入複本）、跑測試。
- **mutator 的「未覆蓋」要人工再看**：它用 anchor 行判斷有沒有被測到，coverage.py 對多行的 `require(...)` 只標第一行，所以有些其實有跑到的 mutant 會被列成未覆蓋。
- **mutator 產生的點不夠**：拆分時要守的條件分支與字串，另外手寫 mutant（例如把條件改成 `if False`）補上；#996、#999 都補了一批。查表結構它完全不動（不改 `is None`、`__mro__`、dict 字面值或 lambda），#1000 的 15 點全部是手寫的：對調、刪掉表格項目、改錯參數形狀等。拿 mutation 數字跨 PR 比較時要記得這點。
- **等價的 mutant 不硬殺**：例如 #990、#991 的 path 參數一定是字串，`strict=True→False` 沒有可觀察的差別，記下理由後保留。

## 第二輪結果（2026-10-27）

第二輪用並行 swarm 做：每個 agent 負責一個檔案，各自一個 worktree 與 PR；逐一拆最重的函式，並刪掉已經沒人用的舊程式。數字都由各 PR 作者量測，再經兩軸 review 核對；CC 用 crapper 量，mutation 是手寫 mutant 就地套用、以 SHA 還原（同第一輪）。CRAP 只有 #1007 用 Linux 覆蓋量過，其餘只列 CC。

### 拆高 CC 函式與消重複

| PR | 函式 | CC 前 → 後（拆出的最高） | Mutation |
|---|---|---|---|
| #1002 | Teams activity route 兩份＋五份 `_write_x` 合併 | 改到的 8 檔 dryer 11 組 → 0 | Teams 17/17、deploy 14/14 |
| #1003 | `NativeGroupStore.post` | 73 → 5（9） | 32/32 |
| #1004 | `toolbox_provider` | 89 → 1（9） | 28/28（review 指出原判等價的一點其實不等價，已補測試） |
| #1005 | `PullAdapter.send` | 153 → 10（11） | 121/123；1 點等價、1 點需非實際的呼叫端輸入；review 抓到一處行為改變（錯誤內容在更多路徑被解析，造成 RecursionError），已修 |
| #1006 | `follow_session` | 85 → 11（10） | 第一輪 41 點、2 點存活後補殺；第二輪重構後 3/3 |
| #1007 | `_original_session` | 36 → 7（7），CRAP 36.5 → 7.0（Linux 覆蓋） | 85/86，1 點等價 |
| #1008 | `NativeSdkExecutor._run` | 119 → 11（10） | 作者 32 點：26 殺、6 等價（review 確認）；review 另提 3 點段落順序 mutant，已補測試殺掉 |
| #1009 | `NativeHitlService._changes` | 65 → 1（10） | 37/37 |
| #1011 | `NativeInstructionControl.task_view` | 103 → 20（9） | 第一輪 30/30；第二輪改寫後 16/16 |
| #1012 | `StandingPolicyReader.checked` | 65 → 7（10） | 47/48，1 點等價 |
| #1014 | `confirm_omission` | 64 → 7（9） | 21/21 |
| #1015 | `NativeResponsesHandler.handle` | 62 → 10（9） | 第一輪 23/24＋第二輪補殺；`_terminal_events` 沒包 aclosing 的 mutant 存活（沒有 finally，不可觀察） |

### 刪除（Neal 2026-10-27 核准）

| PR | 刪掉的 | 保留的 |
|---|---|---|
| #1010 | M2–M4 舊叢集 9 個模組（`hosted_main.py`、`hosted`、`environment`、`environment_backup`、`backup_client`、`production`、`m3_config`、`m3_cli`、`m3_artifacts_cli.py`）；4 個 Dockerfile 與 1 個 `.dockerignore`（`Dockerfile.api`、`Dockerfile.hosted`＋`.dockerignore`、`Dockerfile.behavior-hosted`、`Dockerfile.behavior-assistant`）；只測它們的測試 | `api`、`backup_api`、`m2_gateway`、`m3_api`、`m4_api`、`m4_config`、`m4_context`、`m4_fixtures`：約 240 個現行測試靠 `api.create_app` 建資料（第三輪 #1036 搬走其中 7 個，`m4_fixtures` 留在 src，見第三輪結果） |
| #1013 | 9 個只剩測試在用的 `behavior_*` 模組（1,166 行）、4 個測試檔；網頁交接的兩組服務層檢查改成直接呼叫服務（所在檔案缺 `test_channel_window` fixture，舊測試與新測試都從未實際執行） | — |

### 做法與教訓

- **並行 swarm**：每個 agent 一個檔案、各自 worktree 與 PR；最多約 10 條同時跑，上限是機器負載。
- **review 抓到的 P2 類型**：改變程式在哪些路徑執行（#1005）；只為了縮短而拆；同一組值用不同順序的 tuple；把可區分的 mutant 說成等價（#1004）；測試用私有 helper 算預期值。
- **最強的證據**：把新測試放到 origin/main 匯出的舊碼上跑，確認它在舊行為上也通過，才算鎖住行為。

## 第三輪結果（2026-10-28 至 29）

Neal 2026-10-28 核准一次做完第二輪「未處理／後續」的前 6 項，含刪程式。做法同第二輪：每項一個 agent、一個 worktree、一個 PR，全部 squash 合併進 main。#1035 是 stage 第 37 版的紀錄，不屬品質工作，不列。

| PR | 做了什麼 | 證據 |
|---|---|---|
| #1033（`26dd350f`） | 刪 `hosted_bootstrap.bootstrap()`、`prepare_private_directory()` 與 `__main__`：它們會 exec 已刪的 `hosted_main.py`，整棵樹沒有呼叫端；真入口 `behavior_server_main.py` 只用模組裡的其他名稱 | 新測試在舊碼失敗、刪後通過；相關三個測試檔 320 passed |
| #1034（`c10076e9`） | 清舊映像引用：刪 `deploy/shared-context/azure.yaml`（寫死已刪映像的 digest，沒人讀）、`infra/shared-context/api.bicep` 與 `api.parameters.json`；`behavior-job.bicep` 的 `@allowed` 拿掉 `fa-context-api`。**一處行為改變**：`scripts/validate-shared-hosted.py` 現在要求 `--azd-project` 底下有自己的 `azure.yaml`，沒有就 `parser.error`（exit 2），否則 `azd -C` 會往上找到根目錄的檔案、綁到錯的專案 | 新測試改前失敗、改後通過；`tests/shared_controller` 23 passed；`az bicep build` 通過。`tests/infra/test_shared_context_avm.py` 本機缺 `yaml`，沒跑 |
| #1036（`a9eb6718`） | 7 個只給測試用的 M2–M4 模組（`api`、`backup_api`、`m2_gateway`、`m3_api`、`m4_api`、`m4_config`、`m4_context`）搬到 `tests/shared_context/legacy_m2_m4/`，純搬移加 import 改寫。**`m4_fixtures` 留在 src**：`controller/m4_context_eval.py:16` 是非測試的呼叫端（離線驗收 CLI，`docs/POC-ANALYSIS-AND-PLAN.md` 有操作指令），`docs/context-evaluation-ledger-tests.json` 也記了它在 `src/` 的路徑與 hash | `tests/shared_context`＋`tests/context_eval` collect 數搬前搬後都是 13,512；受影響測試 665 passed、3 skipped |
| #1037（`fade4728`） | 8 個模組、26 個測試因 `pytest.importorskip("test_channel_window")`（repo 裡沒有這個模組）在任何環境都被跳過。62 個測試恢復執行（兩個 `web_test_console` 檔，修正一個過時的預期值）；補 Teams `fetch`／obligation 的 channel 檢查測試與 work policy history 三種竄改測試（被刪的舊檔原有 5 種，另 2 種在第四輪 #1042 補回）；刪 6 個不能誠實測的檔（fixture 都要建已刪的類別或完整 Window2 交接鏈） | 改後相關檔 214 passed、0 skipped；手動 mutation 的 mutant 都被殺掉，只有 bridge reconcile 的 mode gate 一點因後面還有一層檢查而等價、沒加測試；新測試放到舊 main 112 passed |
| #1038（`2b73c5d5`） | 刪正式程式不會建立的 `TeamsPreAcquireRecovery`、`ExpiredTeamsCleanup`、`OriginalFailureDeliveryContinuation`、`OriginalFailureWebHandoff`，連同只有它們用的輔助、`cleanup_authority` 與 `decision_final_check` 參數、`ClaimedSdkRecovery` 走不到的 Teams 分支；16 檔 +50／−2,260。歷史驗證還要讀的 model 與 `validate_*` 都保留 | 每批跑受影響測試；`ClaimedSdkRecovery` 刪前刪後同為 204 passed、23 errors，227 個測試 ID 逐一相同 |
| #1039（`5fc02e05`） | `validate_onboarding_history` CC 52 → 11、`validate_agent_admission_history` CC 47 → 11（拆出的最高值）。先補拒絕路徑測試、再依「檢查哪條規則」純搬移拆開。新增 `tests/shared_context/history_forgery.py`：改一列 row 時同步改所有背書它的 hash，測試才走得到函式本身的規則，不會停在通用的 hash 檢查 | 拒絕位置被測試走到：onboarding 1/8 → 8/8、admission 0/13 → 12/13；mutation onboarding 48/48、admission 50 點殺 43、等價 6、現有 fixture 建不出 1。新測試放到舊 main 全過 |

**保留不刪的（#1038；`RuntimeTransition` 與 `original_failure` mode 已在第四輪處理）**：

- `ClaimedSdkRecovery`：正式程式沒有建立點，但 7 個不被跳過的測試檔建立它，產出的資料交給正式程式會跑的 `validate_recovery_history` 驗證。
- `RuntimeTransition`：當時正式的 `RuntimeRotation` 借用它的 `_parse`／`_operator`／`_changes`／`_fresh`（第四輪 #1046 改成模組函式，類別已移出正式碼）。
- adapter 的 `original_failure` mode：當時 `web_test_console/bridge.py:288` 還會建立 `OriginalFailureAdapter`（第四輪 #1044 已刪）。

### 重新量（main `a9eb6718`）

照下面「重新量」的指令，在 worktree 根目錄執行，`--root` 指到 repo 外面，`-s` 用絕對路徑；crapper 加 `--no-coverage`，所以只有 CC，沒有 CRAP。第二輪沒有重量，所以和第一輪後的數字比較，差異包含第二、第三輪與同期其他 PR，沒有逐一歸因。去掉 novnc 後，dryer 輸出沒有 novnc 的列。

| 項目 | 起點（第 36 版） | 第一輪後（`2b638fc8`） | 第三輪後（`a9eb6718`） |
|---|---|---|---|
| 函式（不含 novnc） | 6,209 | 6,224 | 6,223 |
| CC ≥ 10 | 1,161 | 1,157 | 1,121 |
| CC ≥ 20 | 394 | 390 | 364 |
| CC ≥ 50 | 68 | 67 | 56 |
| CC ≥ 100 | 18 | 18 | 15 |
| dryer 重複（≥ 0.8，至少 4 行） | 376 對，282 對完全相同 | 365 對，273 對完全相同 | 326 對，240 對完全相同 |

- #1039 的兩個函式：量到入口函式 `validate_onboarding_history` 是 1、`validate_agent_admission_history` 是 6；拆出的 `_check_names`（onboarding）、`_check_relay`（admission）各為 11，和 PR 的「拆出的最高 11」一致。
- 仍在 CC ≥ 100 的有 15 個，最高是 `run_copilot`（450）、`_quiescence`（271）、`_validate`（186）、`validate_retirement`（178）、`validate_agent_message_history`（148）；`validate_instruction_history` 是 98，剛好在線下。

### 做法上學到的

- **`pytest --collect-only` 的 SKIPPED 行會揭露從沒跑過的測試**：#1037 就是由 `SKIPPED [7] test_web_first_backend.py:5` 看出 7 個模組只因為 import 一個不存在的模組而整個被跳過。
- **歷史驗證的竄改測試要連 hash 一起偽造**：直接改一列已保存的 row，只會被通用的 row／receipt hash 檢查擋下，測試名稱看起來像在測規則，其實一直停在那一關（#1039 的 `history_forgery`）。
- **「正式程式不建立」不等於可刪**：產出被正式驗證器讀取、或被正式類別借用的，要保留（`ClaimedSdkRecovery`；`RuntimeTransition` 當時也屬此類，第四輪先把借用的方法改成模組函式才移出）；#1038 只刪確認沒有任何建立點、也沒有驗證器依賴的。

## 第四輪結果（2026-10-29）

Neal 2026-10-29 核准做第三輪「未處理／後續」的項目。做法同第三輪：每項一個 worktree、一個 PR，squash 合併進 main。#1040 是第三輪基準頁、#1045 是 dev 探針，不屬品質工作，不列。PR 的檔案與刪除內容已用 `gh pr view`／`gh pr diff` 核對。

| PR | 做了什麼 | 證據 |
|---|---|---|
| #1041（`8fba4f5f`） | 刪 `select_target`、`close_original`、`reconcile_original` 與只給它們用的 `_operator`、`_retained`（+76／−350）。`test_standing_target.py` 改成用 `store.transact` 直接寫資料列，保住 `selected_targets`／`activate_target` 的覆蓋。保留 `validate_close_history`、`verify_closed`（rotation history 與 `behavior_foundry` 還在呼叫）與 `read_stop_receipt`（只剩測試在用，見後續） | 刪前 grep 整棵樹；受影響的 9 檔 198 passed、7 檔 126 passed；新的 2 個測試放到舊碼也過；4 個手動 mutant 全殺 |
| #1042（`4b807395`） | 補回 work policy history 的 unknown-row（`validate_additions` 擋下）與延長 work `valid_until`（`behavior_work_policy_history.py` 的「a work deadline cannot be renewed or borrowed」）兩種竄改測試（#1037 刪舊檔時漏掉）。延長時 `queue_deadline` 要一起改，否則先被 schema 擋下；舊測試只寫 `pytest.raises(Problem)`，沒發現。共用 fixture `test_behavior_channels_pin_refresh.py` 的 `review_source` 加了只能用關鍵字傳的 `claim_id`／`text`，原呼叫端不變 | 3 檔 48 passed（src 與 main 相同）；2 個 mutant 全殺 |
| #1043（`75c1a549`） | installer 的 plan／authorize／apply 批准路徑新增 5 個離線測試：成功路徑走真的 HTTP 路由，拒絕案例含 policy 版本（`revision`）不符。`test_standing_review_clock.py` 的 7 個測試缺 repo 外的擷取檔，仍留在 `ci-quarantine.txt` | 5 passed；9 個手動 mutant 全殺 |
| #1044（`d7081bc7`） | 刪 web_test_console 的 `original_failure` 模式：adapter、bridge 的 reconcile、`index.html` 的「核對原收件回條」面板、2 個測試檔與 2 個 JSON（+59／−1,183）。後端路由早已不存在。`CONFIG_CHANGED` 測試改用 console fixture 保留。**舊設定若帶 `mode: "original_failure"` 現在整份驗證失敗**（repo 內沒有這種設定） | 審查修正後（`8f8e6b58`）`test_web_test_console*.py` 加 `test_native_task_view.py` 1002 passed、1 error（缺 `M2_TEST_RUNTIME`，main 上也有）；node 714 passed |
| #1046（`b606e6d1`） | `RuntimeTransition` 的 `_parse`／`_operator`／`_fresh`／`_changes` 改成 `behavior_runtime_transition.py` 的模組函式，呼叫端直接呼叫。其餘寫入路徑搬到 `tests/shared_context/transition_writer.py`（`TransitionWriter`），11 個測試檔的 fixture 靠它建歷史；正式碼不再有這個類別。先補 34 個前置條件測試（原本沒有測試斷言 `TRANSITION_*` 錯誤碼） | 前置條件 34 passed、`test_behavior_runtime_transition.py` 19 passed；相關 11 檔 379 passed、4 failed、5 errors，與 `a9eb6718` 相同；手動 mutant 全殺（`kind` 條件第一次漏掉，補測試後殺掉） |

### 重新量（main `d7081bc7`）

指令同下面的「重新量」，在 worktree 根目錄執行，`--root` 指到 repo 外，crapper 加 `--no-coverage`，所以只有 CC。去掉 novnc 後，CC 欄是數字的列計為函式；dryer 的 `DUPLICATE` 行計為對，`score=1.00` 為完全相同。差異包含第四輪與同期其他 PR（含 #1045），沒有逐一歸因。

| 項目 | 第三輪後（`a9eb6718`） | 第四輪後（`d7081bc7`） |
|---|---|---|
| 函式（不含 novnc） | 6,223 | 6,220 |
| CC ≥ 10 | 1,121 | 1,117 |
| CC ≥ 20 | 364 | 360 |
| CC ≥ 50 | 56 | 56 |
| CC ≥ 100 | 15 | 15 |
| dryer 重複（≥ 0.8，至少 4 行） | 326 對，240 對完全相同 | 320 對，234 對完全相同 |

### 做法與教訓

- **兩軸審查不能在作者的 worktree 就地做 mutation**：兩邊同時改同一份檔案會互相干擾（第四輪發生過一次）。審查者要另開臨時 detached worktree，做完還原並核對 hash（#1044、#1046 的審查就是這樣做）。
- **刪「只剩測試在用的函式」時，連帶刪掉的測試檔可能是保留函式唯一的覆蓋**（#1041、#1044 都遇到）。先逐一列出被刪測試涵蓋的保留程式，改寫測試保住覆蓋，不能整檔刪。

## 第五輪結果（2026-10-29）

Neal 2026-10-29 核准做第四輪「未處理／後續」的項目。做法同第四輪：每項一個 worktree、一個 PR，squash 合併進 main。#1047（infra）、#1048（第四輪基準頁）與 #1050、#1055（model gateway 探針）不屬品質工作，不列。PR 的合併 commit、檔案與內容已用 `gh pr view`／`gh pr diff` 核對。

| PR | 做了什麼 | 證據 |
|---|---|---|
| #1049（`403d061f`） | 只改 `test_standing_install_approval.py`（+21／−1）。補 authorize 時間窗測試：拒絕「超過證據上限 1 秒」「晚於 now 16 分鐘」「等於 now」「早於 now」「證據撐 1 小時的 plan 仍受 15 分鐘上限」，接受剛好等於上限與剛好 15 分鐘。socket guard 的 event loop 由 `new_event_loop()`（Windows 預設 Proactor）改成 `SelectorEventLoop()`，Windows 上才擋得到 `asyncio.open_connection` | 該檔 6 passed；時間窗 6 個手動 mutant 全殺（第一版漏掉「拿掉 15 分鐘上限」，補「證據撐 1 小時」案例後殺掉）；在 `/apply` 路由插 `open_connection`，Selector 擋得到、Proactor 沒擋。Linux 沒驗 |
| #1051（`75304513`） | 只加測試（+263／−3，不改產品程式）。`test_standing_target.py`：`behavior_work_target._check` 的 11 個拒絕案例（含版本變舊）。新檔 `test_original_stop_close_history.py`：`OriginalStopClosed` 合法列被 `validate_close_history`、`verify_closed` 接受，竄改（session 被改寫、operator、proof 搬到別的 row、不同 launch、不同 evidence）都以 409 `ORIGINAL_CLOSE_DENIED` 拒絕。PR 附「能不能整串收掉」的評估，只評估、沒刪 | 3 檔 19 passed；13 個手動 mutant 全殺，審查後補的版本條件（`>` 改 `!=`）也殺。沒改產品程式，「放到舊碼」沒有意義 |
| #1052（`7715c151`） | 先補拒絕路徑測試，再拆 `validate_agent_message_history`（+941／−252）：CC 148 → 入口 8、拆出的最高 11。32 條 require（root scope 拆成兩個連續 require，共 33 個呼叫）都有拒絕案例，10 組「兩條同時不合格」案例釘住檢查順序。修正輪補「重複 root」拒絕（那條規則唯一的防線）與 work-result 分支 | 測試檔 56 passed，放到舊碼也 56 passed；33 個 `_require` 逐一改恆真全殺；入口 CC 本次重量為 8 |
| #1053（`6f3eeada`） | `mode` 只剩 `normal_pull` 後，刪恆真或恆假的分支（`bridge.py`、`agent_onboarding.py`、`standing.py`；+15／−20）；`Console.configured_tasks()` 刪除、`Console.tasks()` 摺進 `state()`；補 standing config 的 task 派生檢查測試（+2） | 三種 config 的 SHA 前後逐字相同；`test_web_test_console*.py`＋`test_native_task_view.py` 改前 1002 passed、1 error，改後 1004 passed、1 error（同一個缺 `M2_TEST_RUNTIME`）；node 714 passed；新測試放到舊碼 2 passed。3 個 mutant 殺掉，3 個沒殺：submit 的 `require(submission == "assistant")` 已證明等價，另 2 個見後續 |
| #1054（`ec8b5586`） | 先補拒絕路徑測試，再拆 `validate_instruction_history`（+1055／−238）：CC 98 → 入口 7、拆出的最高 9。40 條 require 都有拒絕案例，8 個案例釘住相鄰兩條規則的順序；用參數物件 `h`（整次讀取）、`t`（單一輪次）避免 Data Clumps | 新測試放到舊碼 72 passed；12 個使用端檔案 407 passed、1 error（缺 `M2_TEST_RUNTIME`，舊碼也有）。43 個 mutant 全殺，審查者改子條件、對調順序後另有 3 個 mutant 存活；修正輪補 3 個案例，殺掉其中 2 個，另 1 個（`and reply is None`）證明是等價 mutant |

### 重新量（main `ec8b5586`）

指令同下面的「重新量」，在 worktree 根目錄執行，`--root` 指到 repo 外，crapper 加 `--no-coverage`，所以只有 CC。計數方式同第四輪。同期其他 PR（#1047、#1048、#1050、#1055）只改 infra 或文件，不在量測範圍內，差異全部來自 #1052～#1054。

| 項目 | 第四輪後（`d7081bc7`） | 第五輪後（`ec8b5586`） |
|---|---|---|
| 函式（不含 novnc） | 6,220 | 6,266 |
| CC ≥ 10 | 1,117 | 1,118 |
| CC ≥ 20 | 360 | 358 |
| CC ≥ 50 | 56 | 54 |
| CC ≥ 100 | 15 | 14 |
| dryer 重複（≥ 0.8，至少 4 行） | 320 對，234 對完全相同 | 320 對，234 對完全相同 |

- 函式 +46：#1052 +29、#1054 +19、#1053 刪 `configured_tasks`／`tasks` −2。
- CC ≥ 10 +1：#1052 拆出的 `_check_forward_links`、`_check_root_counter`（11）與 `_check_message_identity`、`_check_response`（10）+4，入口 148 退出 −1；#1054 的 98 退出 −1（拆出的最高 9）；#1053 的 `StandingConsoleConfig.pinned_derivation` 10 → 9 −1。
- CC ≥ 100 剩 14 個：`validate_agent_message_history`（148）退出（`validate_instruction_history` 原本是 98，本來就不在這一級），最高仍是 `run_copilot`（450）、`_quiescence`（271）、`_validate`（186）、`validate_retirement`（178）、`_run_turn`（148）。

### 做法與教訓

- **暫存檔名加任務前綴**：共用 scratchpad 的通用檔名（例如 `mutate.py`）曾被兩個 agent 互相覆蓋，mutation 沒有套用上。
- **整個條件改成 `True` 的 mutation，抓不到子條件和順序的缺口**：#1054 第一輪寫 43／43 全殺，審查者改子條件、對調順序後還有 3 個 mutant 存活。要另外對子條件和相鄰檢查的順序做 mutation。
- **「偽造成本太高、沒測」要先試再下結論**：#1052 原本說做不到的「重複 root」拒絕，審查者用約 40 行就寫出來，而它是那條規則唯一的防線。

## 第六輪結果（2026-10-29）

第六輪處理第五輪「未處理／後續」的項目（#1062 經 Neal 2026-10-29 核准）。做法同第五輪：每項一個 worktree、一個 PR，squash 合併進 main。#1056 是第五輪基準頁，不屬品質工作，不列。#1024 不是品質工作，但它撤回了 [ADR-0012](adr/0012-incremental-authority-origin-validation.md)（已安裝來源的增量核對；dev 改以全新安裝重建），改變「啟動時會讀到哪些資料列」的前提，所以列在表尾。合併 commit 與檔案清單用 `gh pr view` 核對；內容以各 PR 本文、`gh pr diff`（#1057、#1058、#1062 的關鍵段落）與下面的獨立量測交叉核對。

| PR | 做了什麼 | 證據 |
|---|---|---|
| #1057（`9668b3c3`） | 只改 `test_standing_install_approval.py`（+11／−4）。installer 測試的 guard 擋下 `socket.connect`／`subprocess.Popen` 時，先把位址記進清單再 `pytest.fail`；fixture 收尾斷言清單為空。就算 TestClient 的 portal 吞掉例外、或正式碼接住重試，測試仍會失敗，訊息寫出被擋的位址 | 該檔 6 passed（Windows，Linux 沒驗）。探針把 `socket.create_connection` 包在 `except BaseException: pass` 裡：新測試有 2 個案例在收尾失敗並寫出位址，舊測試檔在同一探針下全過（缺陷重現） |
| #1058（`e4ae51b6`） | 刪 `agent_onboarding._link` 的 `require(tasks, "AGENT_ONBOARDING_UNAVAILABLE")` 一行（+0／−1）。`tasks` 來自 `min_length=1` 的設定類別，永遠非空；錯誤碼仍由 `bridge.py` 的入口檢查使用，所以保留 | onboarding 三個測試檔 94 passed，設定檔 SHA 不變。另證明第五輪 #1053 的 mutant 1 已被 `test_instruction_latency.py` 殺掉；mutant 2 落在死碼，見後續 |
| #1059（`4e3ede5c`） | 先補測試，再拆 Teams `delivery_history`（+221／−117，另新增 757 行測試）：CC 101 → 入口 7、拆出的最高 11。替身 view 的 `row` 用正式 `_parsed` 解析每一列；另有用正式程式產生的取消交付歷史案例（pin-v2、channels-v1 兩種安裝各一） | 148 個新案例，放到拆前的 `1dfa9751` 也 148 passed；拆分時相關 11 檔 406 passed、1 skipped；手動 mutation 拆分後 131/131（拆前 131 殺 129，補殺 2）。授權列的寫入端已在 #1038 刪，只能用替身 |
| #1060（`dde4faea`） | 先補拒絕測試，再拆 `validate_work_policy_history`（+348／−229，新增 586 行測試）：CC 112 → 入口 4、拆出的最高 11。補上第五輪列出的 borrowed 路徑：已 dispatched 的 work 用 slot 取得時的 policy，不是排隊時那份；改回排隊時的 policy 或改其他欄位都以 `a work deadline cannot be renewed or borrowed` 拒絕 | 95 個新測試，舊碼同為 95 passed；27 個相關檔舊碼、新碼同為 444 passed、11 errors（缺 `M2_TEST_RUNTIME`）；94 個手動 mutant 殺 88，6 個存活都判為等價 |
| #1061（`32e28fe0`） | 先補拒絕測試，再拆 `validate_retirement`（+464／−281，新增 855 行測試）：CC 178 → 入口 4、拆出的最高 10。原本的 `elif` 鏈改成「型別 → 檢查函式」的表，照原順序用 `isinstance` 分派；表裡 30 個類別兩兩沒有繼承關係，所以第一個符合的就是原本 `elif` 會走的那支 | 106 個新測試，舊碼同為 106 passed；7 個使用端檔舊碼 222 passed、10 errors，新碼 223 passed、9 errors（多出的 1 個是一次性 setup error，單獨重跑舊碼 10 passed，原因沒查到）；124 個手動 mutant 殺 97，27 個存活：14 被更早的驗證器先擋、10 等價、3 沒殺到 |
| #1062（`91288716`） | 收掉 close／target 讀取端（+354／−875，16 檔）：刪 `behavior_recovery_close.py`、`selection_record`、`StandingTarget`、`_check`、`validate_target_history`；保留 `selected_targets`、`activate_target`。遺留列一律明確失敗：dev 啟動 409、stage 啟動 502（`activate_target` 單點讀兩個 key）、peer 每次 503 且不留快取（`build_peer`）、`verify`／`reconcile_sealed` 403。Neal 選「不讀 Table，靠證據＋dev 先跑」，沒有讀 dev 或 stage 的 Table | 釘行為測試在舊碼 6 passed；新的 `test_retired_close_target.py` 15 passed；46 個相關檔 551 passed、13 errors（缺 `M2_TEST_RUNTIME`）、1 failed（`WebSearch-MCP`，舊碼同樣失敗）；8 個手動 mutant 全殺，含拿掉 `build_peer` 的檢查 |
| #1063（`c7fe6248`） | 先補拒絕路徑測試，再拆 recovery `_validate`（+424／−235，新增 778 行測試）：CC 186 → 入口 7、拆出的最高 11。35 條規則都有拒絕案例；原有測試全部停在通用的 receipt 檢查，沒有一個走到這 35 條 | 206 個新測試，舊碼同為 206 passed；新測試加 4 個既有檔 297 passed（新舊碼相同）；mutation 自動 273 個殺 261、手寫 14 個殺 13，剩 13 個判為等價或走不到。Teams／`ExpiredTeamsCleanup` 分支沒有測試，只做純搬移 |
| #1024（`f8e4a6a1`） | 只改文件：新增 ADR-0012 並記為已撤回（Neal 2026-10-29；提案原文保留為歷史），`docs/progress.md` 改 1 行；dev 改以全新安裝重建 | 沒有跑測試、沒有讀寫雲端；重建 dev 當時尚未做 |

### 重新量（main `c7fe6248`）

指令同下面的「重新量」，在 worktree 根目錄執行，`--root` 指到 repo 外，crapper 加 `--no-coverage`，所以只有 CC。計數方式同第四輪。`ec8b5586..c7fe6248` 之間量測範圍內只有 13 個檔案有變動，都屬 #1058～#1063；#1056（文件）、#1057（只改測試）、#1024（文件）不在量測範圍內。

| 項目 | 第五輪後（`ec8b5586`） | 第六輪後（`c7fe6248`） |
|---|---|---|
| 函式（不含 novnc） | 6,266 | 6,373 |
| CC ≥ 10 | 1,118 | 1,123 |
| CC ≥ 20 | 358 | 352 |
| CC ≥ 50 | 54 | 50 |
| CC ≥ 100 | 14 | 10 |
| dryer 重複（≥ 0.8，至少 4 行） | 320 對，234 對完全相同 | 320 對，234 對完全相同 |

歸因：把這 13 個檔案分別在 `ec8b5586` 與 `c7fe6248` 量一次（另外存到 repo 外），差異加總與全樹差異一致（+107／+5／−6／−4／−4）。

- 函式 +107：新增 120、刪 13。新增：#1063 +35、#1061 +44、#1060 +22、#1059 +18、#1062 +1（`refuse_retired_selection`）。刪掉的 13 個全是 #1062（`behavior_recovery_close` 9、`behavior_work_target` 3、`native_capability_api` 的 `_rows` 1）。#1057、#1058 不增減函式。
- CC ≥ 10 +5：新增 12 個 CC 10–11 的函式，退出 7 個。
  - 新增：#1059 的 `_check_outbox`、`_check_grant`（11）、`_check_plan`（10）；#1060 的 `_check_slot`（11）、`_replay_reservation`（10）；#1061 的 `_report_names_launch`（10）；#1063 的 `_check_original`、`_capacity_released`（11）與 `_terminal_proof`、`_launch_claimed`、`_job_cancelled`、`_check_obligation`（10）。
  - 退出：四個大函式（`_validate` 186、`validate_retirement` 178、`validate_work_policy_history` 112、`delivery_history` 101），加上 #1062 刪掉的 `_check_history`（46）、`read_stop_receipt`（19）、`validate_target_history`（12）。
- CC ≥ 20 −6：四個大函式、`_check_history`（46），以及 #1062 讓 `NativeLaunchAuthority.verify` 20 → 19。
- CC ≥ 50 −4、CC ≥ 100 −4：都是那四個大函式。
- 剩下的 CC ≥ 100 有 10 個：`run_copilot`（450）、`_quiescence`（271）、`_run_turn`（148）、`create_app`（123）、`without_files`（111）、`_invoke`（110）、`validate_unclaimed_history`（104）、`install_work_policy_routes`（104）、`validate_additions`（102，#1062 讓它 107 → 102）、`install_assistant_routes`（100）。
- #1062 另讓 `reconcile_sealed` 53 → 51、`register` 99 → 95、`_scope_registrations` 75 → 69，都沒跨門檻。
- dryer 的對數與完全相同的對數都沒變；只比對了數字，沒有逐對比較。
- 新拆出的 12 個 CC 10–11 函式，覆蓋 100% 時 CRAP 也是 10–11，CRAP < 10 的目標還沒達到。

### 做法與教訓

- **安全保證要分環境核對**：第六輪時，總協調說「刪掉後 dev 先跑、啟動會擋」。這只對 dev 成立：dev 是 standing 安裝，啟動掃全表，遺留列會在 `validate_additions` 被擋下。stage 是 fresh 安裝，`installed_fresh_origin` 只讀固定 key，遺留列會被悄悄略過（stage 的依據只有「沒有正式程式寫過這種列」與第 36 版重建過資料）。#1062 的作者在刪之前發現，改成 `activate_target` 單點讀兩個 key、有列就 502。ADR-0012 撤回（#1024）的理由也依賴同一個差異。
- **每個取得 runtime 的路徑都要擋**：#1062 第一版只在 root 的 `activate_target` 擋，peer 經 `for_agent → build_peer` 建立，不經啟動時的 installation，繞過了。兩軸審查各自抓到同一個問題；修正輪在 `build_peer` 補同一個檢查，peer 每次請求都 503、不進快取。
- **替身資料要能通過正式 model 解析**：#1059 第一版有 3 個拒絕案例（`outbox_kind` 的 `message_kind="result"`、`obligation_route` 的 `route=None`、`work_result` 的 `SimpleNamespace()`）用了正式解析就會先擋掉的值，斷言的訊息在真實資料上走不到。修正輪把替身 view 的 `row` 改成走正式 `_parsed`，手造的列都必須通過正式 model，並加一個用正式程式產生的交付歷史案例。

### 未處理／後續

第五輪已完成、不再列：message／instruction history 拆分（#1052、#1054）、authorize 時間窗與 Windows socket guard（#1049）、`mode` 死分支（#1053）。第六輪已完成、不再列：`_validate`（#1063）、`validate_retirement`（#1061）、`validate_work_policy_history`（#1060）、`delivery_history`（#1059）、borrowed 路徑（#1060）、onboarding 的死 require（#1058）、installer 的 guard（#1057）、close／target 的測試缺口與整串收掉（#1062）。以下第六輪新增的條目，行號與 CC 都在 `c7fe6248` 上核對過；沿用第五輪的條目，所在檔案第六輪沒有變動，行號重新對過。

- **close／target 收掉之後**（#1062）：
  - 測試 fixture `unbuilt_peer`（`test_retired_close_target.py:201`）抄了 `test_agent_runtime.py:21-37` 的 `peer_runtime`。
- **`mode` 死碼的殘留**：
  - 第五輪寫的「非 native 舊 freeform 執行緒路徑沒有任何測試」不成立：mutant「`state()` 非 native 執行緒的 `instructions.tasks(...)` 改回 `[]`」會被 `test_instruction_latency.py` 殺掉（#1058 實測；整理這頁時在 `c7fe6248` 重跑一次，1 failed，還原後檔案 SHA 相同）。另一個 mutant「reader 的 task 查找改成取第一個」落在上面的死碼裡，不是缺口。
- **`history_forgery` 預設值不一致**：`material()` 的 `later` 預設 `True`、`data()` 預設 `False`（`tests/shared_context/history_forgery.py:49、63`）。
- **既有測試環境問題**：沒有 `M2_TEST_RUNTIME`（pinned runtime 1.0.83）時，相關測試在本機是 error，例如 `test_web_test_console_appends.py` 的 `test_console_append_reaches_real_n1_and_fixed_sdk_without_a_new_turn`，在 main 上重現；`tests/infra` 在本機 venv 需要 `yaml`（`test_shared_context_avm.py:12`）。`test_native_capability_catalog_reads.py` 的 `test_catalog_gate_reads_one_guarded_legacy_row_snapshot` 在 `c7fe6248` 失敗（`['WebSearch-MCP'] == ['WebIQ-MCP']`）；這個測試是 #1062 改名的，`1dfa9751` 上的舊名 `test_catalog_gate_reads_both_rows_in_one_snapshot_with_the_same_result` 以同一訊息失敗，屬既有問題。
- **第二輪列出的兩個既有 bug 已修好**：adapter 遇到深層巢狀或非字串錯誤碼會當機（#1020）、恢復後的 Responses 回合收不到 `/cancel`（#1019），都在 10-28 合併，不再追蹤。
- **下次 stage 部署**：第三到第六輪的 PR 都在 stage 第 37 版（#1032）之後合併，`shared_context` 與 `web_test_console` 有變，部署時會重建 API 與 Hosted。第六輪的 #1062 會讓 stage 啟動在遺留 standing-target 列時回 502；stage 的 Table 沒有讀過，依據是沒有正式程式寫過這種列、第 36 版重建過資料。

## 第七輪結果（2026-10-30 至 31）

Neal 2026-10-30 核准，順序是先刪後拆，並核准刪除 Teams 舊分支與 `StandingPolicyReader`。dev 也是全新安裝之後，舊資料的驗證與遷移路徑不再需要。

| 項目 | PR | 結果 |
|---|---|---|
| 大函式拆分 | #1072、#1077、#1078、#1080、#1081、#1083、#1084 | `run_copilot` 450 → 1；`install_work_policy_routes` 104 → 4；`without_files` 111 → 7；`_invoke` 110 → 3；bridge `create_app` 123 → 4 |
| 刪除舊路徑 | #1070、#1073、#1074、#1075、#1079、#1082 | `StandingPolicyReader`、channel 舊 API、零呼叫程式、Teams 舊分支、work-policy 安裝、v2 Teams |
| 刪 rotation 與 migration | #1088、#1089 | 淨刪約 19,900 行，`_quiescence` 隨之消失 |
| 小修 | #1071、#1076、#1085、#1087 | `_payload_hash` 歸位；toolbox 總時限；`delivery_for` 內聯；azd 首裝 Portal 帶 `console_environment` |

### 重新量（main `052d671a`）

指令同下面的「重新量」，crapper 加 `--no-coverage`，所以只有 CC。

| 項目 | 第六輪後（`c7fe6248`） | 第七輪後（`052d671a`） |
|---|---|---|
| 函式（不含 novnc） | 6,373 | 5,866 |
| CC ≥ 10 | 1,123 | 966 |
| CC ≥ 20 | 352 | 283 |
| CC ≥ 50 | 50 | 35 |
| CC ≥ 100 | 10 | 2 |
| dryer 重複（≥ 0.8，至少 4 行） | 320 對，234 對完全相同 | 291 對，213 對完全相同 |

剩下的 CC ≥ 100 是 `_run_turn`（148）與 `install_assistant_routes`（100）。這一輪沒有量覆蓋率，也沒有逐項歸因。

### 驗證

- 全量 `tests/shared_context`（離線）：main `a83656c8` 32 failed、14,002 passed；`228c83df` 33 failed、12,941 passed。多出的 1 個是引用已刪 fixture 的測試，已修，該檔通過。這次比對不含 #1085 與最後兩個只改測試的 commit，所以 main `052d671a` 另跑一次全量：11 failed、12,964 passed（2:54）。這 11 個裡，10 個和 #1089 合併前 main 重跑同一批檔案的結果相同，含 `test_native_response_sdk` 那項，它是跑測前先建了工作目錄造成的 `FileExistsError`。多出的 1 個是 `test_native_send_wait` 的舊式 pending 測試。這項測試要重送一次，用的是真實的 10 秒等待。單獨跑與前 141 個 native 檔依序重跑都通過；等待上限設 0 可重現相同回應。當次沒有留下機器卡住的直接證據，所以「超時」是推斷。已改用假時鐘，修後沒有再跑全量。
- 32 個失敗在兩邊完全相同。其中 22 個補上 `N1_NATIVE_WORKSPACE`、`N6_NATIVE_WORKSPACE` 後通過，10 個在 main 上用同樣設定也失敗，清單相同。
- dev：`ca-fa-behavior-poc--052d671-r4001-1` 開機正常，A、B 真回合通過（[紀錄](progress.md#r7-close-1031)）。

### 做法與教訓

- 部分測試 fixture 會延遲 import 別的測試模組，刪測試檔時單跑相關檔案看不出來。大量刪除後要跑 `pytest --collect-only tests/shared_context`。
- 跟著舊遷移一起刪掉的測試，可能是某道還活著的閘門僅剩的保護。#1089 複審抓到 `MIGRATION_OPERATOR_REQUIRED` 已沒有測試，補上後用 mutant 確認。
- `ci-quarantine.txt` 還列著已刪測試檔的案例。已在下面的「離線測試基線清理」處理。
- PR CI 不跑 `shared-context-tests.yml`，它只在每週排程與手動觸發時跑，刪除型 PR 要自己跑全量。

### 未處理／後續

- `installed_origin` 只留 fresh、`agent_origin`：測試 Harness 靠 bootstrap（實作者報約 150 檔，沒有另外數），要先改。非 fresh 分支只有手動跑 `behavior_server_main.py bootstrap` 才走得到，走到時會以無 policy 狀態開機。
- `MIGRATION_HISTORY_UNVERIFIED` 與 epoch 不符的拒絕沒有測試，只有 guard schema 2 走得到，收 `_guard_epoch` 時一起處理。`MIGRATION_HISTORY_INVALID` 已無人發出。
- 綁工作狀態的一批：`_run_turn`、m2_assistant、`install_assistant_routes`。要先核對資料，API 先上，Hosted 晚一個版本。
- 其他 CC 超過 9：`append_saved_result` 28、`_submit`（`behavior_store` 16）、`scan_due` 12、`work_response` 12。`behavior_native_turn_api` 的 `_submit` 已在第八輪處理。
- `web_test_console/standing.py:75,90,94`：`instruction=False` 與 `last_guard_revision` 還留著。
- #1076 留下的 P3：`native_toolbox.py` token backoff。
- 舊失敗：已在下面的「離線測試基線清理」處理，修後在 `aac10725` 全量 0 failed，併入第八輪後在 `a50247ad` 再跑一次也是 0 failed。

## 離線測試基線清理（2026-10-31）

從 main `0e79caaa` 開始，只改測試、`ci-quarantine.txt` 與本頁。這一輪沒有確認到產品 bug，也沒有改產品程式。

### 隔離清單

用 `pytest --collect-only tests/shared_context` 收集到的 13,007 個節點，逐項核對 471 項，10 項對不上，清完剩 462 項，再核一次 0 項失效。

- `test_standing_review_clock.py` 的 7 項：檔案已刪，清單開頭的 `STANDING_CLOCK_PARENT_ART` 一併拿掉。
- `test_complete_sdk_flow_survives_controller_conflicts_without_repeating_effects` 的 3 個參數 id：測試還在，id 已變成 `[before-native]`。它仍然要 `M2_TEST_RUNTIME`，所以換成現在的 1 個 id，沒有直接刪。

`shared-context-tests.yml` 開頭的註解還提到 captured artifacts，這一輪不改 workflow。

### 全量結果

| | commit | failed | passed | skipped | xfailed | 耗時 |
|---|---|---:|---:|---:|---:|---|
| 修前 | `0e79caaa` | 10 | 12,965 | 30 | 2 | 4 小時 01 分 |
| 修後 | `aac10725` | 0 | 12,975 | 30 | 2 | 3 小時 17 分 |

修前的 10 個在修後全部通過，沒有新增失敗。測試數量沒有減少，也沒有新增隔離項目。

全量在這台 Windows 機器上要幾小時，上面第七輪的「2:54」是 2 小時 54 分。修後那次開跑時，機器上還有其他 session 的全量 pytest 在跑。

### 逐項判定

| 測試 | 判定 | 依據 | 修正 |
|---|---|---|---|
| `test_catalog_gate_reads_one_guarded_legacy_row_snapshot` | 測試過期 | `64aff624` 退場 `WebIQ-MCP`，`17b1e2e1` 把假資料改名 `WebSearch-MCP`，斷言沒跟著改 | `caadaaf5` |
| `test_literal_file_is_user_data_in_actual_sdk_requests_and_cold_resume` | 測試過期 | `cc26ecb9` 刻意把文字檔接在原話後面：runtime 1.0.83 一則訊息只顯示第一個 selection，Responses provider 也不送 `text/plain` blob | `7454eb16` |
| `test_pinned_sdk_executes_one_native_completions_turn_and_saves_original_result[True]` | 測試過期 | 同上，檔案在 prompt 裡，attachments 是 `None` | `7454eb16` |
| `test_a_subset_preserves_full_template_and_original_result_with_real_consumers` | 測試過期 | `1fa91150` 起各 consumer 並行 bind，`h.bindings` 是完成順序；close 依 consumer 順序入堆疊，改比對其反向 | `c709f7b1` |
| `test_a_all_ten_capabilities_share_original_ui_turn_and_image_readback[peers]` | 測試過期 | `65d69dd7` 起 API 送 `foundry-native-v2`，`state_profile` 在 submit digest 內；測試重組請求時漏了它，409 是對的。帶上後取回原回條 | `c709f7b1` |
| `test_saved_image_survives_original_child_cancel_and_remains_read_only_in_native_console[peers]` | 測試過期 | 同上 | `c709f7b1` |
| `test_a_handoff_uses_original_api_and_preserves_b_zero_capability_capture[peers]` | 測試過期 | `918f0089` 起回「結果會回到這段對話」，README 與 `test_native_agent_tools` 都是這句 | `c709f7b1` |
| `test_loaded_skill_deactivation_fences_sources_not_independent_operation_grants` 的 2 個參數 | 測試過期 | `aeafe33e`（R3）把 consume 定為送出前最後一道核對，README「撤權的秒級窗口」寫明 reserve 之後的 skill 目錄變更，這一次仍送出 | `aac10725` |
| `test_a_native_human_turn_recalls_first_and_updates_once_after_completion[cosmos]` | 間歇失敗，未重現，未改 | 見下 | 無 |

`test_skill_deactivation_context` 改成和 `test_historical_loaded_skill_does_not_bypass_current_model_source_or_read_authority` 的 `approved-replacement` 分支同一個形狀：目錄、讀取與新的 reserve 回 403，先前 reserve 的請求送出一次（200），已送出的重送回 409。第二次送出會改到 cost 與第二筆 intent，所以原本比對整組 row 不變的斷言改成逐項比對：第一筆 intent 不變、第二筆變成 consumed。

`[cosmos]` 那一項不屬於長期失敗。先前 3 次全量（`r7base`、`r7final`、`r7ofix_full`）都沒有它，修後全量也通過。單獨跑、整檔跑、前 60 個檔接著跑，以及照順序跑前 205 個檔（4,696 passed）都沒有重現。當次的紀錄是 `native_executor` 花了 8.9 秒，超過 `Harness.wait` 的 8 秒上限；`TaskDeferred` 是之後 teardown 的結果。機器負載是推測的原因，沒有直接證據。所以沒有放寬等待上限，也沒有放進隔離清單。

上面第七輪的 10 個裡，有一個是 `test_native_response_sdk`：跑測前先建了 `N6_NATIVE_WORKSPACE` 造成的 `FileExistsError`，這是操作問題，用全新目錄就不會出現。其餘 9 個是上表前 8 列的測試（skill 停用那列含 2 個參數），其中 8 個在隔離清單裡，`test_catalog_gate_reads_one_guarded_legacy_row_snapshot` 不在。

### 做法與教訓

- 過期的 9 個測試有 8 個在隔離清單裡，CI 不跑，產品改版時沒有人被提醒。本機要帶 `M2_TEST_RUNTIME` 跑全量才看得到，只看 CI 綠燈不足以確認它們還對得上產品。
- 清隔離清單要用收集結果比對到節點 id，只看檔案是否存在，會漏掉參數 id 已改的項目。

## 第八輪結果（2026-10-31 至 11-01）

目標是依工作拆函式，不動 wire 格式、錯誤碼、log 字串、HTTP 狀態與副作用順序。四條線並行，每條線先補測試鎖住舊行為，再拆，最後用手寫 mutant 確認守得住。過程中也刪了走不到的分支與條件。#1095 的 PR 另列了四項看不出實際影響的細微差異，例如 `messages` 不是 list 時，`TypeError` 文字由 `not iterable` 變成 `not reversible`，以及 `session_id` 改在 `steering.observe` 之前讀。

| 線 | PR | CC 前 → 後 |
|---|---|---|
| `m2_copilot` | #1095 | `send_request` 53 → 8；`_send_until_idle` 34 → 9；`_retain_latest_inline_png` 14 → 3；`_omit_unsendable_images` 13 → 3；`show_on_desktop` 13 → 5 |
| `behavior_work_policy` | #1096 | `register` 95 → 15；`conversation` 71 → 7；`verify_result_access` 67 → 12 |
| `behavior_foundry` | #1097 | `_request` 98 → 10；`report` 71 → 8；`reconcile_sealed` 51 → 17 |
| `behavior_server_config`、`behavior_native_turn_api` | #1099 | `exact` 94 → 4；`install_native_turn_routes` 50 → 12；`_submit` 45 → 14 |

### 重新量（main `431101d3`）

| 項目 | 第七輪後（`052d671a`） | 第八輪後（`431101d3`） |
|---|---|---|
| 函式（不含 novnc） | 5,866 | 5,987 |
| CC ≥ 10 | 966 | 971 |
| CC ≥ 20 | 283 | 272 |
| CC ≥ 50 | 35 | 26 |
| CC ≥ 100 | 2 | 2 |
| dryer 重複（≥ 0.8，至少 4 行） | 291 對，213 對完全相同 | 291 對，213 對完全相同 |

CC ≥ 10 多了 5 個，拆出來的函式有些仍在 10 到 15 之間，沒有逐項歸因。這一輪沒有消重複，也沒有量覆蓋率。

### 驗證

- 全量 `tests/shared_context`（離線，`a50247ad`，即 #1099 合併後的內容）：0 failed、13,756 passed、30 skipped、2 xfailed，3 小時 01 分。
- 中途的一次性失敗有 `test_official_timeout_distinguishes_unsent_wait_from_an_unconfirmed_post[False]`（0.25 秒逾時）、`test_m2_desktop` 的 WinError 5、`test_sdk_no_start` 的 `wrong-generation`、`test_vm_sdk_start` 的 5 秒等待。後來都沒有重現，原因沒有查到。
- 這一輪沒有部署，沒有 Hosted 或 dev 驗證，內容也還沒上 stage。

### 做法與教訓

- 拆函式最容易出錯的地方，是原本只讀一次的值變成 helper 各讀一次。#1097 的 `_request` 把 `self.target` 留給 helper 重讀，請求進行中 client 改指向後，回覆被對到新 session，變成 502 `FOUNDRY_NATIVE_BINDING_MISMATCH`。測試要在發送前和回覆時都改指向；只在回覆階段改的話，手寫 mutant 會存活。
- 手寫 mutant 的 runner 要檢查 pytest 真的跑了。config 線路由 63/63、submit 85/85 的第一次結果全是假的：實作者回報 subprocess 叫到錯的 `bash`（exit 127），測試根本沒跑，卻被算成「殺掉」。runner 現在遇到非 pytest 的結果就中止。
- 不要在背景測試還在跑的 worktree 裡做 mutant。全量改開獨立的 detached worktree，修正和測試才不會互相影響。
- 用 `| tail` 收測試輸出，一次性失敗的訊息就不見了。輸出要寫進檔案。

### 未處理／後續

- 綁工作狀態的一批：`_run_turn` 148、`install_assistant_routes` 100、m2_assistant。要先核對資料，API 先上，Hosted 晚一個版本。
- `NativeSessions.cut` 62、`follow` 56：等 ADR-0009 的閒置升級。
- 這輪碰過但仍超過 9：`reconcile_sealed` 17、`register` 15、`_turn_view` 15、`_submit` 14、`install_native_turn_routes` 12、`verify_result_access` 12、`_unless_rid_conflict` 12、`_request` 10、`_require_admitted` 10。
- 沒碰、仍超過 9：`append_saved_result` 28、`_after_refusal` 17、`_submit`（`behavior_store` 16）、`scan_due` 12、`work_response` 12。
- `_unless_rid_conflict`：9 個參數，「回傳就是繼續」不好讀，先保留。
- 13 條 `_selectors` 的訊息到不了客戶端，共用 handler 會把 422 改寫成通用回應。
- skill 在 reserve 之後才停用時，該請求仍送出一次。這是現行設計，Neal 2026-11-01 決定不收緊。

## 第九輪結果（2026-11-01）

目標是依工作拆函式，不動 wire 格式、錯誤碼、log 字串、HTTP 狀態與副作用順序。六條線並行：五條拆函式的線先補測試鎖住舊行為，再拆，最後用手寫 mutant 檢查，少數沒殺掉的列在各 PR；其中 #1101、#1104、#1105、#1106 也刪了走不到的條件。#1102 則合併幾處真重複。各 PR 列出了已知的細微差異，例如 `n` 的檢查移到 messages 之前（仍是同一個 422），以及 `TeamGrantCache.store` 的到期時間改在拿鎖之前算。

| 線 | PR | CC 前 → 後 |
|---|---|---|
| `behavior_recovery` | #1101 | `_snapshot` 60 → 9；`verify_platform_stop` 54 → 9；`_multi_models` 46 → 1 |
| 消重複 | #1102 | 共用 `_wait_window`、`_original_agent`、`TeamGrantCache`、`runtime_bindings` |
| `behavior_api`、`behavior_hosted` | #1103 | `behavior_router` 67 → 5；`create_host` 63 → 5 |
| assistant 的 gateway、boundary、progress | #1104 | `validate` 66 → 8；`response` 58 → 14；`public_event` 57 → 9 |
| effects、workers、agent message、instruction | #1105 | `save_reply` 64 → 6；`receive` 60 → 7；peer `forward_result` 57 → 1；`WorkerCore.forward_result` 44 → 7 |
| `behavior_delivery` | #1106 | `_defer_pending` 60 → 10；`_admit_once` 46 → 9；`record_receipt` 42 → 5 |

### 重新量（main `1d92544a`）

| 項目 | 第八輪後（`431101d3`） | 第九輪後（`1d92544a`） |
|---|---|---|
| 函式（不含 novnc） | 5,987 | 6,162 |
| CC ≥ 10 | 971 | 963 |
| CC ≥ 20 | 272 | 257 |
| CC ≥ 50 | 26 | 15 |
| CC ≥ 100 | 2 | 2 |
| dryer 重複（≥ 0.8，至少 4 行） | 291 對，213 對完全相同 | 281 對，209 對完全相同 |

CC ≥ 50 少的 11 個，正是這輪拆掉的 11 個 CC ≥ 50 函式。數字是在合併用的 tree 上量的，這個 tree 和 `1d92544a` 內容相同。這一輪沒有量覆蓋率。

### 驗證

- 全量 `tests/shared_context`（離線，六條線合併後的 `238b8473`，與 `1d92544a` 內容相同）：0 failed、14,796 passed、30 skipped、2 xfailed，3 小時 09 分。這次全量沒有一次性失敗。
- 六條線各自只跑相關測試，全量合併後才跑一次，所以中途沒有逐條的全量結果。
- 每條線的 poteto 與 ponytail 兩軸審查 P1／P2 都清零。
- 沒有在 Linux 上跑。#1101 對 weekly CI 的修正，要等 2026-11-02 那次才看得到結果。
- 這一輪沒有部署，沒有 Hosted 或 dev 驗證，內容也還沒上 stage。

### 做法與教訓

- 第八輪的「只讀一次」在 effects 線又出現：`store`、`runtime` 拆開後變成讀多次。審查抓到，`7deaa0ff`、`dfe7f4f1` 修回。這條規則已寫進實作指引，仍然發生，所以審查要逐一核對。
- 審查抓到 recovery 的假綠測試：改的值走不到判斷。測試要確認真的走到要鎖的那條拒絕。
- recovery 線新增的 18 筆拒絕測試原本靠 runtime，先進了分支上的隔離名單。審查後改用不靠 runtime 的 fixture，全部移出，所以 main 的隔離名單沒有變。`test_a_stored_teams_stop_proof_refuses_at_parse` 也改用同一個 fixture，補上它沒有隔離、週一 weekly CI 會變紅的缺口。
- 這輪合併 #1101 後，其他 PR 短暫顯示 UNKNOWN。連續合併的腳本因此跳過了五個 PR，改成逐一等它算完再合併。

### 未處理／後續

- 綁工作狀態的一批：`_run_turn` 148、`install_assistant_routes` 100、m2_assistant 的 `_execute` 59、`dispatch` 53。要先核對資料，API 先上，Hosted 晚一個版本。
- `NativeSessions.cut` 62、`follow` 56：等 ADR-0009 的閒置升級。
- 還沒碰、CC ≥ 50 的 9 個：`BehaviorRuntime.__init__` 65、`NativeInstructionControl.view` 54、`NativeAgentRelayClient.__init__` 53、`send_recorded` 52、`session_files` 52、`check_existing_vnet` 51、`valid_history` 51、`validate_config` 50、`behavior_operation_cli.main` 50。
- 這輪碰過但仍超過 9：`_finished_proof` 31、`execute` 17、`response` 14、`_read_frames` 12、`_install_invocation_handlers` 11，以及 CC 10 的 `_defer_pending`、`_cancel`、`_call_fragment`、`_check_tools`、`_tool_refused`、`_require_accepted_cancellation`。
- 重複的判讀結果：建議下一輪做 17 對，真重複但不划算 23 對，其餘是誤判、排除範圍或別條線的。
- 各 PR 列出的 P3 測試缺口沒有補，例如 `behavior_api.py:190`、`:412`、`behavior_hosted.py:985`。
- 既有問題：`tool.function` 不是 dict 時會丟 `AttributeError`，這輪沒有改。

## 第十輪結果（2026-11-01 至 02）

目標和第九輪相同：依工作拆函式，不動 wire 格式、錯誤碼、log 字串、HTTP 狀態與副作用順序。七條線並行，把第九輪列出、還沒碰的 9 個 CC ≥ 50 函式拆完，並處理前幾輪留下的一部分項目。有分支沒人守的先補測試；走不到的條件先刪，刪除和拆分分開 commit；最後用手寫 mutant 檢查，沒涵蓋到的列在各 PR（例如 #1114 刪 `_FinishChain` 後沒有重跑）。各 PR 也列出了已知的細微差異與刪除理由。

| 線 | PR | CC 前 → 後 |
|---|---|---|
| `behavior_runtime` | #1108 | `BehaviorRuntime.__init__` 65 → 5 |
| relay、environment files | #1109 | `NativeAgentRelayClient.__init__` 53 → 4；`session_files` 52 → 11 |
| agent messages | #1110 | `send_recorded` 52 → 7 |
| models、操作 CLI | #1111 | `valid_history` 51 → 2；`behavior_operation_cli.main` 50 → 9 |
| 部署檢查 | #1112 | `check_existing_vnet` 51 → 7；`validate_config` 50 → 9 |
| 工作台 | #1113 | `NativeInstructionControl.view` 54 → 10；`decode`、`owner_token` 合成一份，console 重複 16 組 → 2 組 |
| 前輪遺留 | #1114 | `_finished_proof` 31 → 8；`execute` 17 → 4；三對重複合併，背景迴圈共用 `BackgroundLoop` |

### 重新量（main `47b47bcd`）

| 項目 | 第九輪後（`1d92544a`） | 第十輪後（`47b47bcd`） |
|---|---|---|
| 函式（不含 novnc） | 6,162 | 6,266 |
| CC ≥ 10 | 963 | 958 |
| CC ≥ 20 | 257 | 247 |
| CC ≥ 50 | 15 | 6 |
| CC ≥ 100 | 2 | 2 |
| dryer 重複（≥ 0.8，至少 4 行） | 281 對，209 對完全相同 | 264 對，192 對完全相同 |

CC ≥ 50 少的 9 個，正是這輪拆掉的 9 個。剩下的 6 個都是刻意不動的，見下面「未處理／後續」。數字是在合併用的 tree `d98fbad4` 上量的，它和 `47b47bcd` 內容相同。這一輪沒有量覆蓋率。

### 驗證

- 全量 `tests/shared_context`（離線，七條線合併後的 `d98fbad4`，與 `47b47bcd` 內容相同）：0 failed、15,729 passed、30 skipped、2 xfailed，3 小時 10 分。
- weekly CI 的條件（不設 `M2_TEST_RUNTIME`，排除兩份名單）：這輪改到的 17 個測試檔 1,823 passed，排除 462 個 id；五個需要 runtime 的檔案 154 passed，2 deselected。兩份名單沒有變動。
- 工作台 standin（假 Hosted、固定回覆）：連線、送訊息、看到回覆、重新整理保存、Agent Team 設定與助理設定的分頁都通過（訊息用真鍵盤；連線、選單與分頁用 `.click()`，Escape 用 `dispatchEvent`）。和 `43b88ec5` 的第二份替身比對，九個畫面的文字 hash 與七個 GET 的狀態碼全部相同；`/api/agent-template/options` 的 500 和四個 409 在基準上也一樣。
- 每條線的 poteto 與 ponytail 兩軸審查 P1／P2 都清零。
- 沒有在 Linux 上跑。`tests/infra/test_azd_infra.py` 沒跑，本機 venv 沒有 yaml。
- 這一輪沒有部署，沒有 Hosted 或 dev 驗證，內容也還沒上 stage。

### 做法與教訓

- runtime 線的手寫 mutant runner 把 setup error 算成殺掉，poteto 審查抓到。之後改用嚴格 runner，並用鎖 `vars(self)` 的哨兵測試，讓 mutant 被斷言殺掉，不是被 setup 失敗蓋過。
- 平行審查曾共用一個暫存資料夾，一條線的輸出被另一條蓋掉。之後每個審查者用自己的資料夾，也不准用萬用字元刪除。
- 三條線用新舊差分補強手寫 mutant：relay 132,000 組情境，models 46,000 組，console 20,000 個 seed，結果都相同。
- 比對兩個替身時，兩頁都在 127.0.0.1，cookie 不分 port。後載入的頁面會讓另一頁回 403 `LOCAL_REQUEST_DENIED`，重新載入那一頁就恢復。這個現象已寫進 `verify-workbench`。

### 未處理／後續

- 綁工作狀態的一批：`_run_turn` 148、`install_assistant_routes` 100、m2_assistant 的 `_execute` 59、`dispatch` 53。要先核對資料，API 先上，Hosted 晚一個版本。
- `NativeSessions.cut` 62、`follow` 56：等 ADR-0009 的閒置升級。
- 這輪碰過但仍 ≥ 10：`_follow_environment` 25（原本 28）、`_list` 12、`session_files` 11，以及 CC 10 的 `_require_reply`、`_require_api_origin`、`_send_original`、`view`。`_list`、`_send_original` 是同一道檢查，`_require_api_origin` 只有一個條件，再拆只會更難讀。
- 第九輪判讀的重複：建議做的 17 對這輪只處理了一部分，沒有逐對對帳；「真重複但不划算」的 23 對沒有列為工作。
- 既有問題：`tool.function` 不是 dict 時會丟 `AttributeError`，這輪也沒有改。
- standin 走不到的沒有驗證：批准卡、Team MCP 和 Connectors 的寫入、助理設定的清單與套用、`chat-new`。這些要到 dev 才能驗。
- #1109 的 SDK 契約測試讀 core 2.2.0 的私有欄位 `Task._opts`、`_input_type`、`_output_type`。升 SDK 時可能要跟著改，這是刻意的。

## 第十一輪結果（2026-11-02）

規則和前兩輪相同：依工作拆函式，不動 wire 格式、錯誤碼、log 字串、HTTP 狀態與副作用順序。CC ≥ 50 剩下的 6 個仍刻意不動，這輪改拆 CC 20～41 的長判斷，分五條線並行；第六條線把 13 個 prompt 模組的固定來源檢查合成一份。各線先把新測試放到修改前的 src 上跑，確認測試釘住的是原本的行為，再動手拆；直接呼叫拆出來的新函式的測試除外。

| 線 | PR | CC 前 → 後 |
|---|---|---|
| 原生啟動授權、結果保存 | #1125 | `_describe` 41 → 1；`_verify_unclaimed_fence` 33 → 11；`_verify_lost_session` 30 → 16；`prepare` 28 → 4；`_state` 27 → 11；`fence_unclaimed` 25 → 6；`_installed` 24 → 11 |
| 工作政策、排程觸發、回條等待 | #1126 | `refresh` 38 → 14；`preflight` 38 → 13；`_reminders` 37 → 3；`trigger` 33 → 13；`wait_turn` 27 → 8；`turn_order` 26 → 16；`adopt_left` 25 → 1；`check` 24 → 10 |
| HITL、跨 VM 轉送 | #1127 | HITL：`_authorize` 32 → 14、`read` 27 → 11、`_authorize_effect` 24 → 7、`_request` 21 → 8。relay client：`_request` 41 → 8、`_follow_environment` 25 → 15、`recover_queued` 23 → 12。gateway：`_send` 34 → 19、`_original_source` 27 → 8、`_returned` 23 → 9 |
| 記憶 CLI、agent-versions、server | #1128 | memory_cli：`main` 32 → 5、`_proposal_action` 32 → 11、`_settings_action` 30 → 5、`_bridge_preview` 21 → 8。agent_versions：`main` 35 → 3、`sync_all` 29 → 11、`sync_agent_version` 25 → 15、`_check_environment` 20 → 6。server：`run_controller` 32 → 13、`_validate_approval` 32 → 7、`build_server` 22 → 9、`application` 21 → 3 |
| 交辦、助理、worker、compaction | #1129 | `observe` 41 → 14；`pre_tool` 40 → 12；`native_report_plan` 38 → 10；`prepare_session` 37 → 9；`_observe_changes` 34 → 12；`start` 32 → 10；`_provider_objects` 32 → 8；`publish_readiness` 27 → 9；`_operation_effects` 22 → 7；`validate_native_files` 22 → 12；`_model_sources` 21 → 7；`_raw_shape` 21 → 8 |
| prompt 固定來源 | #1124 | 13 個模組共用 `exact_prefix`；這條線單獨合併後，dryer 264 → 173 對（六條線都合併後是 174 對，見下表） |

同一段時間另外合併了兩批：

- #1131：Windows 上兩個 foundry 測試必定失敗。checksum supervisor 的子程序要先 import `behavior_hosted.py` 和它的相依才讀 park／cancel，Windows 上要 4～7 秒，超過 `HostedRuntime.observe` 等的 2 秒。只改測試，產品碼不動。
- 註解清理五個 PR，共約 749 處：#1130 修兩句和程式不符的註解，拿掉 501 個 `noqa`（保留 42 個有理由的，51 個改成 `# fixture`）和 72 條純分隔線；#1134、#1135、#1136、#1137 拿掉註解與 docstring 裡的審查輪次標籤，依序 18、33、15、約 108 處。測試名稱、ADR／§ 編號、`(#NNN)` 線索和產品用語都保留。#1130、#1134、#1136 動到 `shared_context`，下次 dev 部署的 `code_sha256` 會變。

### 重新量（main `a2738763`）

| 項目 | 第十一輪前 | 第十一輪後（`a2738763`） |
|---|---|---|
| 函式（不含 novnc） | 6,266 | 6,460 |
| CC ≥ 10 | 958 | 966 |
| CC ≥ 20 | 247 | 199 |
| CC ≥ 30 | 87 | 64 |
| CC ≥ 50 | 6 | 6 |
| CC ≥ 100 | 2 | 2 |
| dryer 重複（≥ 0.8，至少 4 行） | 264 對，192 對完全相同 | 174 對，126 對完全相同 |

「第十一輪前」是開工前另量的一次，數字和第十輪的 `47b47bcd` 完全相同，量的是哪個 commit 沒有留下紀錄；兩者之間合併的 #1118 只改了 `native_turns.py` 3 行。

CC ≥ 20 少了 48 個。CC ≥ 10 反而多 8 個：拆出來的函式有幾個仍 ≥ 10，期間也有其他 PR 合併，兩者沒有分開算。這一輪沒有量覆蓋率。

### 驗證

- 全量 `tests/shared_context`（Windows、離線，main `1ad3a272`，含第十一輪、#1131、註解清理與 #1133）：兩組同時跑，約 3 小時 37 分。結果是 43 failed、16,924 passed、30 skipped、2 xfailed。
- 43 個裡有 37 個是跑測時漏設環境。subprocess 測試要 `PYTHONPATH`；設了 `M2_TEST_RUNTIME`，真 runtime 測試就要有 `N1_NATIVE_WORKSPACE`、`N6_NATIVE_WORKSPACE`。補上後只重跑這 43 個，37 個通過。
- 剩下 6 個都不是這一輪造成的，也都不在 `ci-quarantine.txt` 或 `known-failing-on-main.txt`：
  - `test_native_queued_sender_recovery.py` 5 個：#1133 合併前的 `5b6a6077` 全過，main `3b8ad314` 上失敗。負責 #1133 的工作階段已接手處理。
  - `test_behavior_wire.py::test_wire_has_only_standard_library_and_pydantic_imports`：#1132 在 `behavior_wire.py` 加了 `from .sdk_pin import …`，這個測試只准 level 0 的 import。`5b6a6077`、`3b8ad314` 都失敗，已通知 #1132 的工作階段。
- 第一次全量用 `-q` 跑，撞到 4 小時上限，停在 71%（12,004 個，35 failed）。`-q` 的進度字元被 `FA_TIMING` log 行打亂，對不回測試名稱，所以那份結果作廢。
- 修改前 src 的檢查：A 179 個、B 205 個全過；D 88／92，失敗的 4 個直接呼叫拆出來的 `_pending_binding`、`_check_checkpoint_grant`、`_start_controller`；E 的 `test_assistant_carried_values.py` 在修改前的 src 上無法收集。C 由審查者另跑，細節在各 PR 說明。
- 每條線的 poteto 與 ponytail 兩軸審查 P1／P2 都清零。
- 沒有在 Linux 上跑。這一輪沒有部署，沒有 Hosted 或 dev 驗證，內容也還沒上 stage。

### 做法與教訓

- conftest 會釘住自己那份 src，`PYTHONPATH` 換不掉測試用的程式。要在基準版上驗新測試，得把基準版的 src 封存出來，再複製測試過去，並先印出模組路徑確認。
- 但 subprocess 測試只認 `PYTHONPATH`。全量跑時要把它設成被測 worktree 的 src，不能整個拿掉。
- 假 runtime 會產生假失敗，要用真 runtime 重跑才能判讀。
- 給審查者的範圍要列出確切的函式名稱，不能只給檔案。
- mutant runner 中途被砍掉時，可能把 mutant 留在 src 裡。之後先比對 git 狀態再繼續。
- 有一次 `git reset --hard` 把還沒 commit 的測試一起丟了。之後先 commit 再重設。
- 長時間全量一定要用 `-v -o console_output_style=classic -rfE`，每個測試一行，失敗名稱才找得回來。

### 未處理／後續

- 綁工作狀態的一批與 `NativeSessions.cut`、`follow`：理由同第十輪，CC ≥ 50 的 6 個不動。
- 這輪改過的檔案裡，還在 CC 25 以上、沒碰的：`derive_work` 35、`cumulative_decision` 34、`_current` 26、`claim` 25。
- 既有問題，這輪也沒有改：relay `_request` 收到 list 或 dict 形式的 code 時會丟 `TypeError`；`validate_native_files` 的 `AttributeError`；`tool.function` 不是 dict 時的 `AttributeError`。
- E 線的 `test_only_a_newer_sequence_moves_the_job` 有一半斷言走不到，還沒補。
- #1131 留下的 P3：`HostedRuntime` 那層 2 秒逾時的路徑現在沒有測試；沒有「子程序還在 import 時就 park」的測試；10 秒上限只剩約 3 秒餘裕；5 處重複的 `children[...].wait(timeout=15)` 可以抽成 helper。
- 各 PR 列出的其他 P3 沒有補。

## 重新量

```powershell
$src = 'src\fa-shared-context-poc\shared_context','src\fa-shared-context-poc\web_test_console','deploy','scripts' | ForEach-Object { '-s'; (Resolve-Path $_).Path }
crapper --root <repo 外的目錄> --no-coverage @src > crap.txt
Select-String -Path crap.txt -Pattern 'novnc' -NotMatch   # 去掉 novnc 的列
dryer --root <repo 外的目錄> --threshold 0.8 @src
```

計數方式：函式數是去掉 novnc 後、CC 欄是數字的列；dryer 的對數是 `DUPLICATE` 開頭的行，完全相同的是其中 `score=1.00` 的行。

`--root` 指到 repo 外面，`.metrics/` 才不會留在工作樹裡；這時 `-s` 要給絕對路徑，所以上面的指令要在 repo 根目錄執行（`Resolve-Path` 以目前目錄為準）。2026-10-26 實測給相對路徑時，dryer 回「No source files to analyze.」；crapper 沒有試。量覆蓋率要裝 `coverage`（只裝在本機 venv，不進相依清單）；pytest 在 PowerShell 工具裡要加 `--capture=sys`，否則空的 stdin 會讓 fd 擷取丟出 `WinError 6`。
