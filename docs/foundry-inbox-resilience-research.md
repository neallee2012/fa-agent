# T5 收件匣：Foundry 原生能力與接線界線

2026-10-01。**研究／未部署／未 live 驗證**。供 ADR-0007 的 T5 段落使用，不是可直接部署的規格。
本稿將官方契約、使用者決定及待驗證的組裝方式分開；不能用方法名稱或查無資料替能力背書。

## 已確定方向

- Neal 已允許 POC 使用 preview／experimental，權威資料不必留在 FA。
- 本階段先 N1～N3：Invocations＋State store；跨 VM 暫留既有 relay，不改走 A2A，也不等待 Responses。
- N4 預計用 Foundry Routines 承接 WAKE。Responses／原生 A2A 接收端留到 N6 評估。
- A 收到**持久接收回條**才說「已交給 B」，並結束原 SDK 回合。B 忙碌是排隊，不是失敗。
- B 完成後，結果持久送到 A；A 在原 CID 以新 RID／新回合處理，不復用已結束的 execution。
- 舊 B23 的 Table 工作只沿原停止／claim-fence證明收尾，不搬、不刪、不重送。

## 官方已明示的能力

| 能力 | 官方契約 | 可使用的部分／不能外推的部分 |
|---|---|---|
| Hosted Invocations | Hosted 可提供 Invocations 端點；平台管理 session／compute。見 [Hosted contract][contract]、[管理 Hosted][hosted]。 | 本階段沿既有 relay 接此協定；不能把一個 HTTP 接收回應當成 B 業務完成。 |
| State store | `FoundryStateStore` 是 experimental class；`create_item` 說明為「Create a new item and fail on duplicate keys」。`set_item`／`delete_item` 有 `if_match`，`list_keys` 有 tags。見 [API reference][store-api]、[State store][store]。 | 可做確定 key 的create-if-absent及單 item條件更新。這不是外部副作用 exactly-once，也不是多 item交易。 |
| Store scope | 官方以「unique within the project and agent」描述store名稱；目前API reference沒有用來指定另一個目標Agent的參數。見 [State store][store]、[endpoint API][store-endpoint]。 | **跨Agent的實際可見性／授權未由這些文字充分說明**。不能從沒有參數推論一定禁止跨Agent，也不能假設同名store必然共享。 |
| Resilient tasks | 官方提供task handle、持久執行狀態、重入及steering；並區分active task衝突與queued input。見 [resilience][resilience]、[reference][resilience-api]。 | 優先用官方的恢復和排隊原語；重入仍可能重跑handler，不自動使外部副作用只執行一次。 |
| 多回合／steering | `multi_turn_task`描述conversation chain，steerable input可排在active turn後；有`TaskRun.is_queued`等狀態。見 [reference][resilience-api]。 | **把work identity命名成Agent就能跨所有CID保證排他／FIFO，是待驗證的組裝假設**，不是官方直接承諾。不得先以此宣稱T2可刪除。 |
| 狀態與checkpoint次序 | 官方範例先保存deterministic checkpoint，再以ETag推進progress；重入讀既有checkpoint。見 [manage task state][task-state]。 | 可重用這個恢復方式，但範例沒有把多筆或跨Agent寫入變成原子交易。 |
| Routines | 官方提供dispatch及執行紀錄，支援Invocations／Responses的相應輸入，並有重試行為。見 [Routines][routines]。 | N4可承接WAKE投遞；仍須由完成處理啟動喚醒，不能假設State store每次寫入會自動發WAKE。重試必須沿同一wake ID。 |
| A2A | Foundry有A2A工具、接收端和身分模型；A2A協議也有非同步task語意。見 [A2A工具][a2a-tool]、[接收端][a2a-endpoint]、[驗證][a2a-auth]、[協議][a2a-spec]。 | 協議能非同步，不代表某一版Foundry工具會立即回turn；依本階段決定，先不切換到A2A。 |

State store預設TTL及實際限制須隨採用版本核對。不得把預設保留期限直接當作FA永久記憶／回條保存承諾。
以上API存在也不等於目前API／Hosted映像已經啟用；實際版本以manifest、N0試驗和部署讀回為準。

## T5 最小語意映射（提案，不是已實作）

### 1. 發送者與接收回條

沿已驗token與入口資料得到sender／Team，不接受body自稱；收件者由可信registry解析，不傳自己。
平台原生身分是可重用的基礎，但FA的Team對應及這組業務限制仍需明確接線。

在store跨Agent可見性尚未確認前，可採**收件端收到請求後寫入自己的store**作為保守接線選項；
這是建議，不是「官方禁止A直接寫B」的結論。採用其他方式前應先確認實際授權契約。[store][store]

回給A的「已交給B」只表示durable acceptance。B尚未執行、忙碌或稍後失敗，都不能把這張回條改稱成功結果。

### 2. 訊息ID及重入

key使用帶來源scope的穩定message ID，不用裸的`r1`或到達時間。
`create_item`衝突後先讀原payload hash／receipt／task outcome：[store-api][store-api]

- 同ID不同內容：拒絕，不覆寫原件。
- 已完成：回原回條／結果。
- 已接收但未啟動：核官方task handle／確定work identity，不能另外生成新工作。
- 執行結果未知：不重做外部副作用；按原task／回條恢復規則處理。

唯一result應綁**那一則已接受的request message key**，不是把整個root RID當成所有回覆共用的一個key。
結果來源要連回原B結束回條；server blocked連回真取消／停止證明，不冒稱取得B JWT或B的SDK答案。

### 3. B忙碌與A原CID接續

先驗官方resilient task／steering是否能做到所需的每Agent跨CID排他，以及在忙碌時持久保留輸入。[resilience][resilience]
至少要核對：work identity的scope、重入後排隊項目是否仍在、是否保持順序、是否會要求active handler提早結束，
以及不同CID的SDK歷史是否仍隔離。**不要先寫成只改task_id命名就全部成立。**

目標CID尚有未完成回合時，不能提前借舊`previous_archive`做新的admission。
建議先保留durable inbox intent，等CID可接續才建立新RID／新binding，然後進同一執行路徑；
這個時點須和N1/N2 DTO及官方locking接點共同確認，不能只喚醒舊A binding。

### 4. root上限與雙寫

委派深度2、root最多8則新交辦／各request唯一result、每回合100，是已決定的業務規則，
不需要假裝是Foundry內建的同名配額。

單一root可評估由**一個權威寫入點**持有一個小型metadata item，以`if_match`更新；
其他Agent向該寫入點請求名額，而不是任意互寫對方的store。[store-api][store-api]
權威寫入點可放在root-owner，也可由本階段既有relay承接；這是接線選項，不是官方強制只能用FA。

root metadata和獨立message item的雙寫仍須處理中斷：保留原ID／hash與待完成意圖，
以官方resilient task重入和確定key補完，不拿新ID再送一次。[task-state][task-state]
在沒有明示交易API前，**不能宣稱跨item／跨Agent原子提交**；也不能從「未承諾」反推「所有組合必定不可能」。

### 5. 結果與WAKE

先保存原結果和對應訊息，再建立／投遞同一個wake ID；N4預計由routine承接投遞／重試。[routines][routines]
接收端仍須用同一ID查回條，不能讓routine重試產生多個A新回合。
「B結果已保存」「A喚醒已接收」「A新回合完成」「原真人已收到」分開記錄。
Routines不取代上述業務關聯，也不代表模型在崩潰前做過的外部效果可安全再執行。

## 採用前僅需釐清的關鍵缺口

1. State store在兩個實際Agent身分下的scope／可見性，以及採用版TTL、大小與列舉限制。
2. 官方task identity／steering queue是否覆蓋每Agent跨CID排他、輸入保存及恢復；未確認前不刪現有保護。
3. root quota與message、結果與WAKE的不同item寫入，如何用原ID和官方task重入恢復；不虛構多item交易。
4. Invocations現有relay的sender binding和原CID／新RID輸入欄位如何接到N1/N2；不以A2A文件替這条路背書。

**官方缺口與FA最小程式碼：** State store的持久化／單item CAS和resilient tasks應優先直接使用；
FA只需接Team／sender／原CID等業務對應、既定上限與原件ID的去重／恢復。
哪些排隊或喚醒細節仍需adapter，要由上述官方契約與小型試驗決定，不能先設計另一個通用bus、scheduler或inbox框架。

## 舊B23收尾不是新架構前置

舊Table工作仍沿既有claim-fence／停止證明、worker completion及message回條收尾；
不為採用State store重寫原hash、清費用、搬移或刪除未知記錄。
這是既有資料格式的必要相容處理，不表示新權威資料必須繼續留在FA。

## 實際參考的一手來源

[hosted]: https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent
[contract]: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract
[store]: https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store
[store-api]: https://learn.microsoft.com/python/api/azure-ai-agentserver-core/azure.ai.agentserver.core.storage.foundrystatestore
[store-endpoint]: https://learn.microsoft.com/python/api/azure-ai-agentserver-core/azure.ai.agentserver.core.storage.foundrystorageendpoint
[resilience]: https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience
[resilience-api]: https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-reference
[task-state]: https://learn.microsoft.com/azure/foundry/agents/how-to/manage-task-state
[routines]: https://learn.microsoft.com/azure/foundry/agents/how-to/use-routines
[a2a-tool]: https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent
[a2a-endpoint]: https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
[a2a-auth]: https://learn.microsoft.com/azure/foundry/agents/concepts/agent-to-agent-authentication
[a2a-spec]: https://a2a-protocol.org/latest/specification/

- [Hosted管理][hosted]、[Hosted contract][contract]
- [State store概念][store]、[FoundryStateStore API][store-api]、[storage endpoint API][store-endpoint]
- [Resilience概念][resilience]、[長回合API][resilience-api]、[task state與checkpoint][task-state]
- [Routines][routines]
- [Foundry A2A工具][a2a-tool]、[接收端][a2a-endpoint]、[身分驗證][a2a-auth]、[A2A協議][a2a-spec]
