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
- `ci-quarantine.txt` 還列著已刪測試檔的案例，下次動它時一起清。
- PR CI 不跑 `shared-context-tests.yml`，它只在每週排程與手動觸發時跑，刪除型 PR 要自己跑全量。

### 未處理／後續

- `installed_origin` 只留 fresh、`agent_origin`：測試 Harness 靠 bootstrap（實作者報約 150 檔，沒有另外數），要先改。非 fresh 分支只有手動跑 `behavior_server_main.py bootstrap` 才走得到，走到時會以無 policy 狀態開機。
- `MIGRATION_HISTORY_UNVERIFIED` 與 epoch 不符的拒絕沒有測試，只有 guard schema 2 走得到，收 `_guard_epoch` 時一起處理。`MIGRATION_HISTORY_INVALID` 已無人發出。
- 綁工作狀態的一批：`_run_turn`、m2_assistant、`install_assistant_routes`。要先核對資料，API 先上，Hosted 晚一個版本。
- `m2_copilot`：`send_request` 53、`_send_until_idle` 34、`_retain_latest_inline_png` 14、`_omit_unsendable_images` 13、`show_on_desktop` 13。
- 其他 CC 超過 9：`_submit`（`behavior_native_turn_api` 45）、`append_saved_result` 28、`_submit`（`behavior_store` 16）、`scan_due` 12、`work_response` 12。
- `web_test_console/standing.py:75,90,94`：`instruction=False` 與 `last_guard_revision` 還留著。
- #1076 留下的 P3：`native_toolbox.py` token backoff。
- 舊失敗：`test_native_capability_integration` 的 4 個（在隔離清單）；`test_catalog_gate_reads_one_guarded_legacy_row_snapshot`（`WebSearch-MCP` != `WebIQ-MCP`，不在隔離清單）；其餘 5 個見上面的 10 個失敗。

## 重新量

```powershell
$src = 'src\fa-shared-context-poc\shared_context','src\fa-shared-context-poc\web_test_console','deploy','scripts' | ForEach-Object { '-s'; (Resolve-Path $_).Path }
crapper --root <repo 外的目錄> --no-coverage @src > crap.txt
Select-String -Path crap.txt -Pattern 'novnc' -NotMatch   # 去掉 novnc 的列
dryer --root <repo 外的目錄> --threshold 0.8 @src
```

計數方式：函式數是去掉 novnc 後、CC 欄是數字的列；dryer 的對數是 `DUPLICATE` 開頭的行，完全相同的是其中 `score=1.00` 的行。

`--root` 指到 repo 外面，`.metrics/` 才不會留在工作樹裡；這時 `-s` 要給絕對路徑，所以上面的指令要在 repo 根目錄執行（`Resolve-Path` 以目前目錄為準）。2026-10-26 實測給相對路徑時，dryer 回「No source files to analyze.」；crapper 沒有試。量覆蓋率要裝 `coverage`（只裝在本機 venv，不進相依清單）；pytest 在 PowerShell 工具裡要加 `--capture=sys`，否則空的 stdin 會讓 fd 擷取丟出 `WinError 6`。
