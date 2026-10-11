# Evaluation 帳冊：Opus 5.5 review

日期：2026-09-24。模型：`claude-opus-5.5`。初次審查 base `d518af2772274a31816884e6dd2bb91fd65c819b`，head `eef74a6d2c29609a69b6d1247461c8dac31e09b1`。

以下保留兩個軸線的原優先級，不因同一根因重疊而合併或重新排序。這是 code review，不是人類 evaluation 標籤或產品 runtime 的成功證據。

## Standards

| ID | 初次判斷 | 問題 | 修正／回歸依據 |
|---|---|---|---|
| S-01 | P2 | `--previous` 只按 ID 比內容；調換先前 acceptance 與後來 failure 可掩蓋回歸 | 強制舊 run 精確前綴，並驗新記錄的 sequence；`test_previous_rejects_reordered_or_inserted_history` |
| S-02 | P2 | 忽略後來的人審拒絕；回歸後可挪用舊的人審或程式 checks | 共用 `unresolved`，只使用最後未解結果之後的完整驗收；human rejection、舊 C/H/M 證據重用的回歸案例 |
| S-03 | 判斷性 smell | 未解結果條件在 validate／append 重複，容易漏改 | 共用 `unresolved` 與規格／baseline 對應邏輯；非獨立升級的 P1/P2 |

Standards 原始結論：無 P1，兩個 P2；最嚴重層級為 P2。判斷性 smell 不冒稱工具已證明的硬違規。

## Spec

| ID | 初次判斷 | 問題 | 修正／回歸依據 |
|---|---|---|---|
| P-01 | P1 | 調序或前插失敗 run，仍可通過 `--previous` 並恢復 accepted | 精確前綴、immutable binding／sequence；與 S-01 同一修正，但保留本軸線 P1 |
| P-02 | P2 | 一份 `scope=target` 人審可同時套用多個 target | 新 run 的該 scope 只能有單一目標／情境 ID；多目標反例拒絕 |
| P-03 | P2 | 後來的人審拒絕未撤下 accepted，C target 也可用 rejected 的 pass 支持接受 | 拒絕作為未解結果；客觀 pass 與人的 rejection 均保留，但不讓它支持接受 |
| P-04 | P2 | 漏掉 12 情境／共同規則；更改規格後可重用舊驗收，previous 又被拿新規格檢查 | Schema 2 封存規格與逐 run 綁定、12 情境獨立計數、共同規則／依賴 hashes、明確 sync-spec／legacy migration |

Spec 原始結論：一個 P1、三個 P2；本軸線最嚴重為 P1。沒有以 Standards 的較低優先級遮掉此結論。

## 第一輪修正（歷史）

第一輪已新增回歸案例並完成本機修正，[測試紀錄](context-evaluation-opus-fixes-tests.json)為 **88 passed**；當時尚待 Opus 5.5 複核，沒有先標 clean。原 71 passed 工具紀錄及所有舊 evaluation run 保留，不覆寫成「原版本沒有缺口」。

P-03 沒採用「把客觀 pass 改掉」的方式。帳冊原契約把客觀結果與人審分開，故保留兩者，改為拒絕其接受資格並撤下既有宣告；後續 Spec 複核已確認此處理符合原意。

Schema 1 缺完整規格綁定，遷移時將其 run bindings 保留為 null，不假造它們曾按新共同規則或情境驗收。Raw traces、私有快照、reviewer 私有 reasoning 與本機路徑不包含在本檔。

## 第二次複核

Opus 5.5 的 Standards／Spec 均對 `1db940600c774dbf8a67acbab604595f3e112441` 複核。原 P1、單一條目人審、拒絕與規格版本／情境處理已解；兩軸線各保留同一個 **P2**：晚記錄但觀察日較舊的 pass／人審，仍可能跨過後來的失敗。

修正再加入觀察日期門檻，取所有相關未解結果的最晚觀察日，不只看最後一筆的日期；記錄順序仍須在最後未解結果之後，同日才用記錄順序解平手。舊客觀結果、人的決定與日期都保留。程式與人審兩條重現、晚到舊 failure 及同日新評估各有回歸案例。

## 已完成的來源程式複核

兩個 Opus 5.5 reviewer 都已對 base `a811f634615945c3f92c6fd860aa9bcdaddf8a93`、head **`77ac9c5ce6b874e78d54c31b29c091f681f1df13`** 完成複核，並依同一固定版載入新專案規範。

| 軸線 | 原剩餘觀察日期 P2 | 其他 P1／P2 | 證據 |
|---|---|---|---|
| Standards | 已解決 | 0／0 | 原 CLI repro 回 `INVALID_INPUT`；程式、人審、混合、情境、batch rejection 等案例拒絕過時證據，新結果仍可接受 |
| Spec | 已解決 | 0／0 | TC-11、SP-02、E-09 的晚到舊 pass 被拒絕；同日新評估、前件及 evidence hashes 核對保持 |

兩軸均獨立重現 **91 passed**，結論不只依賴測試數字。這是對指定 head 的 source-review clearance，不是 Hosted／Blob、真模型品質或全部 Target State 驗收。同日內的觀察先後仍只能沿記錄順序判斷，日期欄位的限制已明示。

本次收尾只更新紀錄、帳冊與摘要，`controller/context_target_eval.py` 及其測試保持上述 reviewed head 的內容。**最終 PR head 的逐版 review 狀態以 PR 說明及所列固定 SHA 為準**；不將本文件對 `77ac9c5` 的回條冒充對未來任意提交的批准，也不為回填未來審查而改寫此 head。

## Verified against

- 已核規範：固定 `a811f634615945c3f92c6fd860aa9bcdaddf8a93` 的 `AGENTS.md`、`docs/status/CURRENT-STATE.md`、`docs/CONVENTIONS.md`（先讀前兩份，再讀第 8 節及相關段落）；載入本身未更改工作樹。
- 審查來源：初審 `eef74a6d2c29609a69b6d1247461c8dac31e09b1`、複核 `1db940600c774dbf8a67acbab604595f3e112441`、來源程式清除 P1/P2 的最終複核 `77ac9c5ce6b874e78d54c31b29c091f681f1df13`；分軸結論及原優先級保留，不把舊失敗 head 改成 clean。
- 修正來源：`controller/context_target_eval.py:validate` 同時使用記錄位置及觀察日；程式／人審／多個未解觀察日反例在 `tests/context_eval/test_context_target_eval.py`。
- 最新工具結果：[91 passed 的獨立紀錄](context-evaluation-observation-date-tests.json)，不取代原 71／88 結果。兩軸最終回條另以 `source_review`／`coverage=none` 追加至[帳冊](context-evaluation-status.json)，不自動接受任何產品條目。
