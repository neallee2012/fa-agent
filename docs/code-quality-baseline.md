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
| #1010 | M2–M4 舊叢集 9 個模組（`hosted_main.py`、`hosted`、`environment`、`environment_backup`、`backup_client`、`production`、`m3_config`、`m3_cli`、`m3_artifacts_cli.py`）；4 個 Dockerfile 與 1 個 `.dockerignore`（`Dockerfile.api`、`Dockerfile.hosted`＋`.dockerignore`、`Dockerfile.behavior-hosted`、`Dockerfile.behavior-assistant`）；只測它們的測試 | `api`、`backup_api`、`m2_gateway`、`m3_api`、`m4_api`、`m4_config`、`m4_context`、`m4_fixtures`：約 240 個現行測試靠 `api.create_app` 建資料 |
| #1013 | 9 個只剩測試在用的 `behavior_*` 模組（1,166 行）、4 個測試檔；網頁交接的兩組服務層檢查改成直接呼叫服務（所在檔案缺 `test_channel_window` fixture，舊測試與新測試都從未實際執行） | — |

### 做法與教訓

- **並行 swarm**：每個 agent 一個檔案、各自 worktree 與 PR；最多約 10 條同時跑，上限是機器負載。
- **review 抓到的 P2 類型**：改變程式在哪些路徑執行（#1005）；只為了縮短而拆；同一組值用不同順序的 tuple；把可區分的 mutant 說成等價（#1004）；測試用私有 helper 算預期值。
- **最強的證據**：把新測試放到 origin/main 匯出的舊碼上跑，確認它在舊行為上也通過，才算鎖住行為。

### 未處理／後續

- **保留的 8 個 M2–M4 模組**：可以移到 `tests/`。
- **審查發現、已開成待辦的既有 bug**：adapter 遇到深層巢狀或 list 型錯誤碼會當機；恢復後的 Responses 回合收不到 `/cancel`。
- **不再被建立的 6 個服務類別**：`ClaimedSdkRecovery`、`TeamsPreAcquireRecovery`、`ExpiredTeamsCleanup`、`OriginalFailureDeliveryContinuation`、`OriginalFailureWebHandoff`、`RuntimeTransition`；web_test_console adapter 的 original_failure 模式也一樣。
- **仍提到已刪映像**：`deploy/shared-context/azure.yaml` 與 `infra/shared-context/behavior-job.bicep:8`。
- **`hosted_bootstrap.bootstrap()`** 仍會 exec 已刪的 `hosted_main.py`（生產 CMD 不會走到）。
- **依賴不存在的 `test_channel_window` fixture 的測試檔**，在任何環境都被跳過。
- **下次 stage 部署**：因 `shared_context` 有變，會重建 API 與 Hosted。

## 重新量

```powershell
$src = 'src\fa-shared-context-poc\shared_context','src\fa-shared-context-poc\web_test_console','deploy','scripts' | ForEach-Object { '-s'; (Resolve-Path $_).Path }
crapper --root <repo 外的目錄> --no-coverage @src > crap.txt
Select-String -Path crap.txt -Pattern 'novnc' -NotMatch   # 去掉 novnc 的列
dryer --root <repo 外的目錄> --threshold 0.8 @src
```

計數方式：函式數是去掉 novnc 後、CC 欄是數字的列；dryer 的對數是 `DUPLICATE` 開頭的行，完全相同的是其中 `score=1.00` 的行。

`--root` 指到 repo 外面，`.metrics/` 才不會留在工作樹裡；這時 `-s` 要給絕對路徑，所以上面的指令要在 repo 根目錄執行（`Resolve-Path` 以目前目錄為準）。2026-10-26 實測給相對路徑時，dryer 回「No source files to analyze.」；crapper 沒有試。量覆蓋率要裝 `coverage`（只裝在本機 venv，不進相依清單）；pytest 在 PowerShell 工具裡要加 `--capture=sys`，否則空的 stdin 會讓 fd 擷取丟出 `WinError 6`。
