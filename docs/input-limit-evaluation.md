# 已確認input-limit拒絕：分層觀察與安全停止

日期：2026-09-26。**現有FA能辨識精確的provider context-limit code，阻止再次外送，
並在真owned stop後保存failed結果；沒有觀察到compaction或成功恢復。**
這是TC-05的局部證據，不是四類壓力完整接受，不新增重試／壓縮授權。

## 固定來源與兩個階段

- Product source：`50cfcb664cb41b174f4f0fcd6fe233c20c7a83b0`／tree
  `e65313e8d55c0f1e8915e9452f0b0faca07f8c8b`，全程clean。
- SDK：`github-copilot-sdk==1.0.13`，官方tag/release對應
  `f13e4a2cc7e4e220974d2333142234e162a3252e`；五份installed Python關鍵檔逐byte與官方快照相同。
  Runtime實際回報1.0.83，exe/node採原固定hash，沒有更新SDK／harness或讀binary內部。
- 第一階段只讀FA／官方source，0 execution，報告與回條保持原樣。
  第二階段是獨立、先固定oracle與fixture的單一2-vector probe，不能回填為第一階段已實跑。
- Fixture是session artifact，SHA
  `008cea7519d0e9fa01406a988e6695ba26e41a7ed258b31c18aec778103b82a8`，**不是Git中的測試**。
  帳冊run baseline因此保持null，另以product source、fixture及環境hash描述精確組合，
  不把後來文件commit當受測source或full-target接受。

## 真正執行路徑

原signed instruction → controller／AssistantSession → 正式SDK HTTP callback →
原ticket model API → AssistantProvider。只有最外部provider由API-side MockTransport替代；
沒有SDK I/O double，觀察wrapper均轉呼叫原SDK方法。

使用ordinary `fa-instruction-v1`，checkpoint/capacity profile都None，business mode
`offline_contract`。它不是native業務模式安裝資格，也沒有新hook、retry、token-limit override。
每案真的收到model **POST** 後才回應小型HTTP400：

| vector | `error.code` | message／判定 |
|---|---|---|
| A | `context_length_exceeded` | 精確code；應保留FA context-limit診斷 |
| B | `invalid_request_error` | 只有message含context_length_exceeded字串；不能誤分類 |

測前固定的oracle是原provider只能收到一次POST、首failure後的後續model callback必被fence、
A/B診斷必須區分；runtime額外callbacks／事件／stop則先觀察，不預填0或6。
普通GET413與caller傳入的context_info／token_limit不作model壓力證據。

## 結果

唯一`probe-01`於2026-09-26T08:03:31+08開始，**2 PASS／0 FAIL/error/skip**，
JUnit23.911秒；A10.136秒、B8.734秒，不是效能比較。120秒outer watchdog未觸發。

| 實際觀察 | A | B |
|---|---:|---:|
| FA安全diagnostic | ASSISTANT_CONTEXT_LIMIT | ASSISTANT_PROVIDER_HTTP_FAILURE |
| Hosted分類 | ASSISTANT_MODEL_OUTCOME_UNKNOWN | ASSISTANT_MODEL_OUTCOME_UNKNOWN |
| 原provider POST | 1 | 1 |
| SDK model-path callbacks | 6 | 6 |
| 其中prior-failure fence | 5 | 5 |
| 其他HTTP／WebSocket callback | 0／0 | 0／0 |
| session.error／idle | 1／1 | 1／1 |
| compaction start／complete | 0／0 | 0／0 |
| 真runtime／真stop | 1／exit1, graceful=true, force0 | 1／exit1, graceful=true, force0 |
| 原result／交付路徑 | failed／forwarded | failed／forwarded |
| synthetic pending model cost | 53628 microUSD仍在 | 53628 microUSD仍在 |

六個callback不等於六次provider請求；只量到這兩個短案例，不能把五次追加定為SDK通用上限，
也不能命名為`context_limit_retry`壓縮流程。其他HTTP/WS計數只限CopilotRequestHandler接點，
不是所有SDK JSON-RPC。

兩案Popen退出與原SdkStop的PID／exit逐項對上，沒有從idle、timeout或例外推定停止。
Session.error的error_type不在預先文字白名單，僅保留hash
`a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947`；
error_code／status_code實際缺省為null，沒有拿外層400／502補填。

Failed result的passed_checks空、known/unresolved workeffects均0，**不代表model usage結清**。
Public accounting的pending仍為正、metered欄位absent/null，純GET再讀ledger hash不變。
53628是synthetic fixture帳本值，不是真花費、退款、lifetime成本或可花餘額；未讀raw model-intent
或另送replay，不增加TC-02／外部exactly-once資格。

Publicconversation記failed、reply_available=false、can_follow_up=true；後者只是旗標觀察，
本批未送新turn／coldresume、未擷取archivekind，不能據此宣稱可攜checkpoint或重試已獲准。

## 為何不是自動壓縮恢復

固定FA source中的第一個轉換接點在
`shared_context/behavior_assistant_hosted.py:298–320`：
API非200被轉為安全的MODEL_OUTCOME_UNKNOWN，原code留diagnostic。
`m2_copilot.py:200–272`記首個failure後拒絕再次外送；
固定[官方callback adapter](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/copilot_request_handler.py#L537-L607)
把未完成callback例外轉為502/error frame，並非vendor原400 envelope。

官方[CompactionTrigger](https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/python/copilot/generated/session_events.py#L12204-L12215)
確有context_limit_retry；這不能證明現行FA接點已把所需訊號交給runtime。
本次實際沒有compaction，故也**沒有exercise新非manual事件的guard分支**。
既有manual-no-custom first-resume不是input-limit handler；舊8個synthetic拒絕＋1ordinary guard
也不能代替這條provider路徑。

TC-05其餘接近token、明顯超額、image壓力與受控恢復仍未驗；TC-04的effective容量／128000
差額、自然background pressure、一般權限撤銷／stop未知也未新增證據。
沒有新增compaction profile、hook政策、SDK patch、第二個loop或把已消費請求恢復可重送。

## Verified against

| 原件 | SHA256 |
|---|---|
| 只讀source report | `8ed29f6068656863ee29127b70df6ac99c105bcae8ad87b4d9d377eb7ef5bcfb` |
| 只讀receipt | `32f7b0473d7d8d1b3da34dcf9c8b3d8d8e4bd2d01e860b3c1382aa7250116ed3` |
| 測前source manifest | `1776f2f91ad433f212b7f426726f4f78fd4c197157c8467894887f6c57b03bd5` |
| 測前environment | `c557a4ec163e3c4262c39b12d31a7c9adc2aa735d2e0598e8df020008f6a0ba5` |
| JUnit | `03baebd061037b4636dfbe5c857a62cfab7afb50de15671ed31a0ccb1fec2645` |
| A safe result | `d13a7f79f691af0cea1a4c95591c4a219c134ef6c8367084213d90d86d9c5c5f` |
| B safe result | `7753962b34ea942c65f3bfbd845dd4ebac4ce6bdab37d13b3dd303e00136cf1e` |
| Probe handoff | `64786b8fa8c59500337b6a96a8daaf6c3ff40d73299d658d1e7968d40a9f9448` |
| Cleanup | `21806c2cc3d4145dab49203b7816c4032ed6994cef2a5eefaa2359d6b8c53382` |

15份indexed原件hash／size、兩案完整counter／stop／結果／pending讀回已核。
Source manifest的NOT_STARTED及progress快照是測前／在途歷史，final A/B及JUnit才是完成原件。
只保存enum／hash／有界metadata，沒有prompt、summary、error text、provider body、
fullSDKevent、rawstate或credentials。三個ownedsynthetictemp已清，固定runtime/cache保留。
這輪0 Azure／真模型／付費推論／IAM／deployment，不改舊9guard、TC04、271或copy原批次。
