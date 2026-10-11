# 每個對話一個長駐 worker，縮短熱路徑第一個反應

狀態：superseded by ADR-0006（每個 Agent 一台 VM、只在入口核對）與 ADR-0007（每個 Agent 的排隊與恢復改用官方 `@multi_turn_task`、新對話狀態放 State store）｜原草擬日期：2026-09-30。

本文保留作為量測與 PoC 證據：第 2 回合約 610 批授權往返、長駐只省約 0.25 秒；ADR-0006 §背景引用此結果。PoC 程式（#223）預設不啟用；是否移除，等 ADR-0007 N1 上線後再決定。

**2026-10-01 依 [ADR-0006](0006-team-trust-boundary-grokbot-mode.md) 調整：** 常駐 worker 改成每個 Agent 一個（一台 VM），可服務多個對話；同一 Agent 的對話依序排隊，新對話只在原 worker 開新 SDK session。熱路徑只需入口 ticket 和回合結束時的保存，不再每回合做 Hosted claim。本文 PoC 程式與結果保留；下文「每個對話一個」的描述是當時紀錄。

**2026-10-06 native 已採用（CODE/offline，未部署）：** Neal 09:24 問「回合結束時停止 SDK 程序（約 2.3 秒）需要每回合結束都停止 SDK 程序嗎? 是否有參考 sdk 官方最佳實踐?」。SDK 官方（固定 commit [`f13e4a2`](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/session-persistence.md)）建議一位使用者一個長駐 CLI server（Pattern 1），每個工作結束只 `session.disconnect()`，範例沒有每回合停程序；Grok Bot 的 `host-main.cjs` 與 charter-agent 的 `ResponsesHostServer` 也都長駐。因此 native 路徑直接沿用本 PoC 的保留條件，不加開關：
- 每台 VM（`NativeSdkExecutor`）一個 `NativeResidentPool`，以 `sdk_session_id` 為 key，最多保留 2 個閒置 SDK（LRU，超過就照舊 disconnect＋stop 最舊的）。
- 保留條件：回合 idle 完成、程序活著、checkpoint 正常、沒有 manual compaction／diag codec、這回合沒呼叫過委派（`task`）、沒有還在跑的自動 compaction，且 idle 後 files 沒有再變。其他情況照舊停程序，並以 warning 記 `RESIDENT_SDK_STOPPED reason=...`。（2026-10-06 v16 dev 修正：原本「選了委派」或「審過任何工具」就停，開了委派與 HITL 的 Agent 每回合都停。2026-10-06 v17：出過批准卡或提問的回合也保留，理由見 [README](../../src/fa-shared-context-poc/README.md#resident-sdk-after-hitl)。）
- 下一回合只有在「拿到的已發布 checkpoint sha256 等於 release 時的快照」且「files 在 release 後沒變」時才沿用；否則停掉常駐程序、照舊從 checkpoint 還原（files 變動記 `RESIDENT_FILES_MOVED`）。
- SDK session 的 hooks（operations guard、委派）與 ask_user 只在建立 session 時登記一次，改成和工具、權限、事件、模型一樣轉接到當前回合。
- 退版：native checkpoint 不存 `sdk_stop`，發布格式不變；退回前一版仍能從同一份 checkpoint 還原。

實作、殘餘風險與測項見 [native 常駐 SDK](../../src/fa-shared-context-poc/README.md#native-resident-sdk)。

## 背景

使用者要求 FA「越靠近 Grok Bot 越好」。Grok Bot 送出後約 1 秒內就有反應，這是使用者的體感，沒有量測值。

FA 目前的量測來自 live job 4db84cb8（2026-09-30 10:03 UTC，main 57d269e，Hosted 56，新對話）：
- 第一個處理步驟出現在第 15.6 秒。
- 第一個工具在第 28.8 秒。

| 區段（第 0 秒＝接單完成） | 秒數 | 負責 |
|---|---|---|
| 接單（admission，在第 0 秒之前） | 3.4 | FA（7 次 EGT） |
| native_prepare | 0.14 | FA |
| hosted_create（只在第 1 回合；容器冷啟動） | 3.2 | Hosted Agent |
| hosted_invocation（含兩次 claim，未拆開） | 4.9 | Hosted Agent＋FA |
| acquire＋SDK 啟動前的 catalog／授權（含遠端 WebIQ list_tools） | 約 6 | FA |
| SDK 啟動＋session＋MCP | 小於 1 | SDK |
| 第一次 model_reserve（含等待 progress 寫入） | 3.4 | FA |
| 未計時的空檔 | 約 2.1 | 推測為 FA |
| model_authority＋model_consume | 2.6 | FA |
| model_to_headers（FA before_send＋模型首包，未拆開） | 4.6 | FA＋模型 |

第 2 回合起沒有 hosted_create，但每回合仍有以下成本：
- Foundry invocation、兩次 claim。
- 從 archive restore SDK 狀態（1.4～3.1 秒）。
- 重新啟動 SDK 與 MCP。
- 回合結束時停止 SDK（約 2.4 秒），再寫 archive（約 1.4 秒）。

## Grok Bot 的作法

來源：2026-09-30 使用者請 Grok Bot 內建的 Copilot CLI 做唯讀調查（host bundle `bcd53e8`）。這是對方的報告，FA 沒有獨立驗證。

- **長駐 host**：`sand-supervisor` 監督一個長駐的 `host-main.cjs`。每則訊息在同一個程序內跑一輪（`enqueueExclusiveRun`），不重啟程序。同一個 session 的 runner 會快取沿用；新訊息會中止舊的那一輪。
- **觸發**：`POST /api/sendPrompt` 立刻回 `{accepted:true}`，這一輪在程序內非同步執行。
- **授權**：gateway 在記憶體內比對 Bearer token（`timingSafeEqual`）。模型 credential 快取在記憶體，到期前 2 分鐘續期。
- **防重送**：送出層用 `clientNonce` 加上 acceptance digest ledger（最多 256 筆）；stream 以 request_id 丟棄過期更新。工具層有沒有 exactly-once，對方回答不確定。
- **完成與取消**：由迴圈條件判定一輪結束（沒有工具呼叫、沒有排隊訊息等）。取消用 AbortSignal，不殺程序。
- **狀態**：每個 agent 的狀態存在同一個 box 的本機磁碟，另有 crash／升級恢復模組。

## 差距來源

1. **每回合都是新 job**：invocation、claim、SDK restore、啟動與停止，從第 2 回合起仍是約 5～10 秒的固定成本。
2. **每次 Hosted→API 呼叫都從 Table 重讀完整授權**：第一次模型呼叫前後就花掉 10 秒以上。

第 2 項由「拔除非必要卡控」另案處理，但第 1 項不變。熱路徑要接近 1 秒，需要長駐 worker。

## 提議

- **每個對話一個長駐 worker**
  - worker 跑在該對話原本的 Hosted session 內；SDK 程序、SDK session 和 MCP server 在回合之間保持存活。
  - 新回合用輕量觸發交給同一個 worker：可以是同 session 的 invocation 只負責排入，或由 worker 向 API 拉取。
  - 接單後立刻回報「開始處理」。
- **授權分兩層**
  - 每回合開始時做一次完整授權，沿用一次性 claim／assignment 與 ticket。
  - 回合內的 Hosted→API 呼叫只核對 ticket 和 session 的對應。撤權、取消和 execution 的檢查放在寫入交易的 CAS 內（reserve、consume、保存），撤權最遲在下一次寫入時生效。
  - 不跨回合在記憶體保留授權。
- **完成證據**
  - 由「SDK 正常停止」改為四件事都成立：SDK idle 事件、必要結果已保存、沒有未決工具動作、checkpoint 已寫入。
  - 防重送效果必須和現行相同。
- **冷路徑保留**
  - 閒置逾時後 compute 會被回收（見官方依據），新對話與閒置後的對話走現有 checkpoint／restore。
  - 熱路徑不做 restore。
- **取消**
  - 熱路徑用 SDK abort 中止這一輪，不殺程序。
  - 取消前已送出的工具動作仍依現有規則核對，不自行重送。
- **換版**
  - 只在回合之間換版，沿用現有的「在新環境接續」流程。

### 保留不變

總協調 2026-09-30 已確認以下項目全部保留：
- K1：所有寫入走 CAS。
- K2：模型只送一次（先 reserve intent，consume 後才送）。
- K3：用量與費用帳寫入。
- K4：一次性 claim／launch permit。
- K5：ticket↔session 核對（輕量版）。
- K6：reserve／consume 交易內的撤權、取消、execution 檢查。
- K7：結果與 SDK 狀態保存走 CAS 並留回條。
- K8：接單時的政策與預算檢查。
- `session_after_invocation` 的換版偵測。

不跟 Grok Bot 學的部分：Grok Bot 沒有工具層 exactly-once，FA 保留 K2、K4、K7。

## 目標

- 熱路徑（同一對話第 2 則起，worker 仍在線）：送出到第一個可見反應 ≤ 1 秒，到第一個模型請求 ≤ 2 秒（不含模型首包）。
- 冷路徑：維持現況，另案優化。

## 費用

計價來自 Azure Retail Prices API，West US 3「Foundry Agents」：
- Hosted vCPU：US$0.0994／小時。
- Hosted Memory：US$0.0118／GiB‧小時。
- 以 2 vCPU／4 GiB 計，每個 active session 約 US$0.246／小時。

依官方說明，compute 在最後一個請求後的閒置逾時到了才釋放，計費按 active 期間計算。

FA 現在回合結束時不停止 conversation session：`_cleanup_instruction_session` 遇到 `hosted_session_id` 且不是 retire 時直接返回。因此 session 本來就 active 到閒置逾時。

- 長駐 worker 只是在同一段 active 時間內讓 SDK 保持存活。只要不加 keep-alive、也不調高逾時，compute 時間不變。
- 若要延長熱路徑而調高閒置逾時（上限 60 分鐘），每個對話在閒置期間最多每小時約多 US$0.25。
- live FA Hosted 版本的閒置逾時不在 repo 內，需要純讀核對；官方預設是 15 分鐘。

## 代價與風險

- **狀態一致**：程序內狀態可能與 archive 不一致。每回合結束仍要寫 checkpoint；可以非同步寫，但下一回合開始前必須已完成，或處於可恢復的狀態。
- **取消**：改成 abort 後，需要驗證工具動作的最終狀態。
- **文件同步**：需要改動現行「SDK 正常停止」的完成要求。若採用本 ADR，須同步更新 ADR-0004、[行為控制規格](../FA-AGENT-BEHAVIOR-SPEC.md) 和 [Grok 目標](../grok-aligned-target-state.md) 的對應段落。

## 概念驗證（PoC）

以下四項都通過才算驗收：
1. 同一對話連續兩回合：量第 2 回合從送出到第一個模型請求的時間（用 FA_TIMING 和 table_gets）。
2. 完成證據：idle、保存和 checkpoint 都成立；中途殺掉 worker 再恢復，不重送已完成的工具或模型請求。
3. 熱路徑取消：中止這一輪，不產生額外效果。
4. 撤權：下一次寫入就被擋下。

費用估計約 US$3，上限 US$6：
- ACR build 2 次：約 US$0.5。
- Hosted compute 約 2 小時：約 US$0.5。
- 約 40 回合 mini 模型：約 US$1～2。

須經使用者批准後才動 live。

## PoC 結果（2026-10-01）

使用者批准 PoC（上限 US$50）。程式以 #223 合併，預設不啟用。live 在 agent B 上執行：main 59658f5，API 與 Hosted 同一 build，Hosted B 17，gpt-4.1-mini。

B 暫時選用 PoC 模板（selection `ab308813…`），跑完已選回原設定（selection `ece8ed15…`，模板 sha `371cfe3c…`），選回後讀回核對 role、settings、selection 都相符。共 10 回合，約 US$0.02。

| 項目 | 結果 |
|---|---|
| 1. 第 2 回合沿用 SDK | Pass：出現 `resident_restore`，沒有 `sdk_restore`／`sdk_process_start`，答案記得前一回合 |
| 2. worker 遺失後恢復 | Pass：stop session 之後，下一回合改走 `sdk_restore`，前文還在；每回合 model_requests 都是 1，沒有重送 |
| 3. 熱路徑取消 | Pass（第二次配置）：回合狀態 cancelled，有 `sdk_process_stop`，下一回合走 `sdk_restore`。第一次配置的回合太短，取消送到前回合已完成，記為未觸及 |
| 4. 撤權 | 只做離線測試，不算 live 驗收：standing policy 每個 domain 只能安裝一次 |

### 第 2 回合首反應的組成（伺服器時間約 8.2 秒）

| 區段 | 耗時 | Table 讀取／依序批次 |
|---|---|---|
| admission | 2.6 秒 | 386／286 |
| hosted_invocation（含兩次 claim：0.71 秒、0.75 秒） | 3.2 秒 | 兩次 claim 共 223／171 |
| hosted_acquire | 0.71 秒 | API 端 71／49 |
| resident_restore | 0.71 秒 | API 端 61／51 |
| 其他（native_prepare、hosted_verify、空檔） | 約 1 秒 | — |

### 結論

- 長駐 worker 能用：防重送、取消與冷路徑恢復都符合本 ADR 的要求。
- 但只靠長駐，離「熱路徑 1 秒」還很遠。它只省掉 restore 與 SDK 啟動，實測 SDK 啟動只要 70ms、冷 restore 為 884ms，所以第一個反應之前只省了約 0.25 秒。每回合結束時也少了約 2.3 秒的 SDK 停止時間。
- 主要成本在每回合的授權往返：admission、兩次 claim、acquire、restore 合計約 610 個依序批次，約 5 秒；另有 Foundry invocation 本身約 1.7 秒。
- 下一步：先減少依序批次（步驟 2 拔除非必要卡控、admission 與 claim 批次合併）。改用常駐觸發、避免每回合都做 invocation 與 claim，等批次減少後再評估是否需要，並另案提出。
- PoC 程式維持預設不啟用。要保留或移除由總協調決定。

## 官方依據

- [Hosted agents：per-session VM 隔離、閒置逾時 2～60 分鐘（預設 15）、計費按 active session 的 CPU 與記憶體](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Hosted session 的管理](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- [Azure Retail Prices API](https://prices.azure.com/api/retail/prices)
