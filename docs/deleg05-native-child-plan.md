# DELEG-05 一頁計畫：原生唯讀子任務

**已批准 D0／D1 本機工作，D2 live 另批；尚未宣稱完成。** 2026-10-02；現況核對基準 `652cd353`；固定 SDK 1.0.13／runtime [1.0.83][S7]。依 Neal #432 不設全域名額；舊四格提案已刪。

**現況：native 已能委派，不是從零建。** 原 capture 選入 `fa-delegate-v1` 才由正式 factory 接 SDK `task/custom_agents`；目前是同 Agent 內單一同步、零工具 child。PROGRESS DELEG-05 的「一般助理拒絕 child_jobs」指舊 Job binding，不是這條 C4 native 路徑。現有 NativeEffects 已先存子結果，post-tool hook 等保存完成才交原父模型；不是 checksum worker。缺口是 [CAP-01 §9.2](agent-runtime-target-state.md#92-cap-01-委派) 要求的「child 實際讀一次受控來源」及新原生入口 live 驗收，不能拿零工具或舊 v53 結果代替。

**SDK-first：直接沿現有接點。** 固定版正式支援 custom agents、同步 task 與 child 工具白名單，不需要 FA 再造執行循環。[S1][S2][S6] 省略 child `tools` 會繼承工具；現有 `tools=[]` 的零工具效果有本機真 SDK 反例。下一片只讓新配置的 child 使用父已具有的 `read_user_file`（原 CID 已完成真人回合的檔案），不給 shell、寫入、傳訊、任意路徑或 MCP。唯讀靠 SDK 工具限制及既有 reader，不靠 prompt；這不是 OS／Team 隔離。

**保存與歸屬不重建。** 沿原 Agent／CID／RID／SDK session／task call／child ID／輸入 digest 保存，再回原 parent；空結果、錯 parent、晚到結果與 unknown 保持原拒絕／只核回，不換 ID 重派。SDK hook 可等待，但不提供 FA 的持久回條，且 hook 例外可能被當無修改；沿現有明示失敗與 drain，不只靠事件或例外。[S3][S5][S8] 新 reader 的 hook 身分須先驗：官方子 hook 案開了額外旗標、子 sessionId 可能不同，不能直接假設等於父 SID。[S4]

**T5 分工：** child 是同一 Agent 內的短期 SDK 工作，不另建 VM／普通工作，也不重新排進該 Agent 的對話佇列；父在原回合等子結果並整合。T5 才是交給另一位長期 Agent，由對方自己的佇列處理，結果依原交辦回到 A。D1 唯讀只是第一步，不是 child 永久上限；之後放寬另提。

| 切片 | 最小交付與判準 |
|---|---|
| D0：沿現有路徑核對 | 公開原生 Responses 入口→N1→固定 SDK 離線 provider，核父→零工具 child→父、結果先存與原 ID 不重派；只補缺的接線案例，不重寫 C4。 |
| D1：受控 reader，預設關 | 新 opt-in 配置才公開 `read_user_file`，不放寬舊零工具 grant。核 child 真讀一次、強叫父寫入工具仍無效果、越界／錯 parent 拒絕、保存未知與取消不重派、恢復不放寬。父子工具仍不超原父權限；不加全域槽、排程器或額外 VM。 |
| D2：另批 live | 批准後用測試 Agent 的新 Responses CID，先保存兩份合成版本檔；下一原生回合請 child 比對，父核來源後整合。須看見真正 task／child reader、子結果先存且回原 RID、正確差異與版本、無外部效果，用量沿原 RID。失ACK／越界反例先本機做；live unknown 就停，原 ID 核回，費用及操作沿當批批准。 |

**D0 已交付；D1本機完成、兩軸清零，D2另批。** D0新增公開接線及既有反例共三案通過，未改產品；[分批來源](../src/fa-shared-context-poc/README.md#deleg-d0responses-公開接線核對test-onlylocal)。D1探針已核UUID輸出與同call重複通知，不再把handler當exactly-once；旗標未設／true的child hook身分都已觀察。[D1接點、結果與限制](../src/fa-shared-context-poc/README.md#deleg-d1選用原對話文字檔-readerlocal預設關閉)。

## Verified against

本提案形成時只讀程式與既有證據，未重跑測試、改產品、部署或更新帳本；後續 D0／D1 結果另記。程式位置皆在 `src/fa-shared-context-poc/shared_context`：`native_sdk.py:184–224,468–478`（reader／factory）、`behavior_delegation_wire.py:35–44`（零工具配置）、`native_delegation.py:83–215`（原件保存）、`behavior_delegation_runtime.py:183–275`（前後 hook／原父核對）；舊拒絕在 `behavior_assistant.py:194`、`behavior_work_policy.py:86`。
既有 `tests/shared_context/test_native_delegation.py:50–145` 核真 SDK／本機 State store、子結果先存與下一回合不重派；`test_delegation_zero_tools_native.py:20–104` 核 child 強叫父效果工具失敗。前者的 completions／responses 是模型 API，不是本輪新的 Foundry Responses live 證據。

**原則原文**
> 請以"足以防止資料損失、重送、與明顯錯誤"的最小機制為原則，在完成布署前，不得套用商業/軍規式的流程、指標、證據層級或繁瑣的gate。
> 不要比 grokbot 嚴格。
> 「SDK-first／Hosted 優先」原則，在 FA 自己建一層之前，應該先確認官方功能跟最佳實踐，確認是否真的不足。

**官方來源（同一固定 SDK commit）：** [custom agents][S1]、[Python client][S2]、[hook 處理][S3]、[子 hook 測試][S4]、[事件型別][S5]、[同步 task][S6]、[runtime 版本][S7]、[Tool／呼叫識別][S8]。
[S1]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/custom-agents.md
[S2]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/client.py
[S3]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/session.py#L2934-L3009
[S4]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/e2e/test_subagent_hooks_e2e.py#L66-L139
[S5]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py
[S6]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/fleet-mode.md#L329-L332
[S7]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/nodejs/package.json#L8
[S8]: https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/tools.py#L59-L86
