# FA 原始資料與權威儲存研究：參考 Grok Bot 的分層做法

查讀日期：2026-09-15。狀態：**研究建議，不是新 ADR、已部署能力或 runtime PASS**。

## 結論

**應參考 Grok Bot 的「事件／原件、工作狀態、記憶／索引分層」，不是照搬每 agent 一份 SQLite。**
FA 現有 Azure Table＋Blob 已能作為不經模型摘要的資料底層；眼前缺口主要是合規原件的完整保存、來源關聯、版本與恢復契約，不是少買了一個「無損資料庫」。既有 [ADR-0002](adr/0002-shared-context-without-file-share.md) 的方向不用推倒。

建議分成兩階段：

- **目前 POC：沿用 Table＋Blob，補原件／事件與已審 claim 的保存、精確讀回、引用保護和可重建投影。** 不為 M4 改資料庫，不把 Hosted 私人磁碟升格成共享權威。
- **產品化：當跨實體交易、歷史查詢、治理或復原要求超出 Table 的合理實作成本時，優先評估 Azure SQL Database＋Blob。** PostgreSQL Flexible Server＋Blob 也是完整候選；若團隊偏 PostgreSQL 生態或可攜性，可優先它。Cosmos DB 適合已有明確 partition 與分散式規模需求的情況，不因它有向量功能就優先採用。

這是按現有 FA 工作／批准／共同額度／交付需求提出的工程建議，不是服務效能排名。**US$100 仍是整個 POC 預算，不是新資料庫的預算；本研究沒有新建資源、遷移、權限變更或付費驗證。**

## 1. 證據範圍

本研究分清三種依據：

| 類別 | 能支持什麼 | 不能支持什麼 |
|---|---|---|
| 本輪查讀的 Microsoft／PostgreSQL 官方文件 | 服務與引擎的公開契約、限制、恢復及驗證方式 | 此訂閱已啟用該配置，或 FA 已通過其故障測試 |
| 使用者提供的 Grok Bot 本機查證稿 | 報告所述本機分層、引用、GC／同步實作及證據限制 | 本輪直接查過 Grok 原始碼，或已知道 Temporal 遠端實體資料庫 |
| FA 現行規格與已記錄進度 | 要維持的契約及尚未完成的能力 | 本機測試等於實際 Hosted、儲存備援或使用者交付保證 |

已讀使用者附件 `pasted-text-00000000-0000-0000-0000-000000000029.txt`，共 372 行，SHA256：
`94e7c09848be1c57f20c49090a02841bd4e19d53549d8da7542224f2eb792615`。
這是**收到的附件 hash**，不是 Grok source bundle、獨立 `REPORT.md` 或 `EVIDENCE.json` 的 hash。
附件中的 `CODE_VISIBLE`、`RUNTIME_OBSERVED` 等標籤屬於原查證者；本輪沒有連線到 Grok 主機，也沒有重驗那些程式行。

### 後續六機制查證補充

另已讀使用者提供的475行轉貼附件`pasted-text-00000000-0000-0000-0000-00000000002a.txt`，SHA256為`961fe2198d61060503e1858c03b9b78b5c4d9ea858569c92aafce516314ed74c`。其中包含六機制REPORT正文；轉交所稱37筆EVIDENCE不代表本端取得或重驗了完整結構化原檔。

該報告的版本標記為`a02b024`，不是前次`294fba1`，也不是已對證的Git commit。其host補算主程式SHA256為`a739bf55a7f64cded6255088bf3b716e163d00e351823a00afd61875debe87f1`；這是外部報告的`HOST_COMPUTED_HASHES`，不是本端計算，也沒有原子凍結全部來源。查證者在所檢查安裝中沒有找到適用測例、未執行動態測試；不推論上游或遠端完全沒有測試。

六項補證支持採用穩定event／tool ID、型別化引用、明確unknown、來源fingerprint與可回源查讀等原則，但沒有提供可直接照搬的完整保證。FA不採RAM 60秒暫留作存續保護、不跳過必要blob後宣稱完整還原、不以upgrade隱藏prompt當精確resume、不以fire-and-forget或最多3次redrive後清除欠答作可靠交付，也不以zombie escape代替舊executor停止證據。FA每obligation最多2次模型補交契約不改。這份補證用來完善各切片反例，不降低v1.2門檻，也不當作FA的runtime證據。

## 2. Grok Bot 哪些做法值得採用？

以下 Grok 欄位均來自使用者提供的查證稿；FA 欄位是本研究建議，不是現有實作完成表。

| Grok 報告中的元件／行為 | 應學的原則 | FA 對應方式 |
|---|---|---|
| `store.db.transcript_entries` 保存事件 JSON、唯一 ID／sequence | 先保存實際收到的事件，不讓摘要成為唯一歷史 | 受控事件帳本；保存合規 payload、來源、scope、sequence、schema、digest；更正新增版本 |
| `conversation-blobs.db` 保存 turn／step／payload，透過 root 引用 | 大內容與結構分開；按 ID 還原引用圖 | Blob 保存大原件，DB 保存 manifest／關聯；必要小材料可 inline 與控制狀態同交易 |
| 多數 blob 使用 content-address | 讀回內容可核對，避免可變名稱偷換版本 | 保存 SHA256、長度、codec、精確版本；取得原件後核對，不只比較檔名 |
| JSONL 是可讀投影，不含所有 binary／原始 prompt | 方便人看與查錯，但不能冒充完整備份 | 從已保存事件匯出文字；匯出檔標示覆蓋範圍及省略，不當權威原件 |
| `search-index.db` 選欄位、trim、截斷，可重建 | 搜尋是找資料的目錄，不是原始資料本身 | Memory／Search 只回來源候選；再向權威資料層按 ID／版本讀回 |
| 記憶檔是萃取、去重、可改寫的筆記 | 有用知識與逐字內容分開管理 | FA 保存已審 claim／失效狀態，Memory Store 只是檢索投影 |
| snapshot blobs 上傳後還要等 manifest 提交 | 有 blob 不代表整份備份或新狀態已提交 | 保存提交 receipt／版本集合／還原檢查結果；未知提交不當完成 |
| Temporal agent 明確走遠端路由，本機同步分支可能跳過 | 權威位置由實際執行責任決定，不由本機檔案存在決定 | FA 的權威在受控雲端資料層；Hosted 本機副本只能是 cache／工作副本 |

本機 SQLite／Docker 結果、Storage REST 可讀寫或 session 私人 `$HOME` 持續存在，都不是 Foundry Hosted 能掛載共享檔案系統的證據。

不照搬的地方同樣重要：

- 報告的 summary archive **不是 GC 保留邊**；FA 的未結工作、unknown operation、未交付 obligation 所需材料必須獨立受保護，不能只靠摘要指到它。
- 報告的 live root 有固定 slot 覆寫；若 FA 要還原歷史 checkpoint，應另留不可變版本，只有「目前 head」以條件更新。
- 報告的逐 DB snapshot 沒有證明跨 DB 同一時點一致；FA 不能把「每檔 hash 正確」當整個恢復集合完整。
- 報告有可選遠端同步，不等於已證明遠端成功。FA 不能在只有本機寫入時宣布雲端接單成功。
- 分層不強迫採完整 event sourcing。可先保留權威目前狀態，加必要的不可變事件／版本；全系統改成 replay 驅動會增加 schema、重建與維運成本，官方也提醒不要無條件套用。[S18]

## 3. 「無損」應如何定義？

| 層次 | 可驗收的說法 | 不能混用的說法 |
|---|---|---|
| 原件完整性 | 在已同意且允許保存的範圍內，對每個已承接事件／檔案按 ID 取回相同 bytes，核對長度與 hash | 「模型摘要包含大意」不是完整保存；「全部聊天永久保存」也不是預設要求 |
| 持久性與復原 | 明列故障場景、RPO（可能遺失多久的資料）、RTO（多久恢復）及保留窗口，實際演練還原 | ACID、備份、跨區副本、WORM 是不同功能，不能一概稱任何災難都零遺失 |
| 使用時找回 | 權威事實有按 ID／subject／版本的精確讀路徑，引用能回原件；找不到就明確說不足 | 原文還在，不保證向量搜尋每次命中，更不保證模型每次推理正確 |

「原始」也要指定邊界：入口若已先 trim、重編碼、去敏或截斷，只能保證保存**該邊界實際接受的內容**，不能聲稱保存上游未見的全部 bytes。必要的去敏應記錄轉換版本；原始憑證、未授權私人內容及不可取得的模型內部推理不納入保存承諾。

資料型別亦會改變保真範圍。例如 PostgreSQL `jsonb` 不保留空白、key 順序與重複 key；`json` 保留輸入文字，但仍受資料庫編碼限制。若驗收要求原始 bytes，使用 binary／Blob 欄位保存原件，另外建立可查詢欄位；不要拿重新序列化的 JSON hash 冒充原始檔 hash。[S10]

## 4. Azure 候選比較

| 服務 | 適合作為 FA 的哪一層 | 交易／並行邊界 | 主要代價與限制 |
|---|---|---|---|
| **Azure Table Storage＋Blob** | 現有 POC 的權威紀錄、小事件／材料；Blob 放大原件 | Table EGT 必須同 table、同 `PartitionKey`，最多 100 entities、4 MiB；ETag／If-Match 可做條件更新。[S1][S2] | 索引與關聯需自行設計；共同 guard 可能成熱點；Table 沒有內建 PITR，原件保護與自訂備份是額外工作。[S3] |
| **Azure SQL Database＋Blob** | 關係複雜的工作、grant、共用budget、obligation、版本與事件 | 本研究設計限定同一 DB 內交易；可選 isolation、鎖與條件更新。Azure SQL 預設 RCSI 是 statement snapshot，**不是自動 serializable**。[S6] | 需 schema、migration、索引、連線池及故障核對；大 Blob 仍不在 SQL 交易內；service tier／HA／備份影響成本 |
| **Azure Database for PostgreSQL Flexible Server＋Blob** | 相同關聯式權威需求，適合 PostgreSQL 生態 | 同一 DB 內交易；可用唯一約束、鎖／條件更新或 `SERIALIZABLE`。預設 `READ COMMITTED` 不保證多次查詢為同一快照。[S8][S9] | 須處理 serialization failure／連線／schema；不是把 JSON 全放進 `jsonb` 就保留逐 byte 原件；HA、server及備份成本另計 |
| **Azure Cosmos DB for NoSQL＋Blob** | 已有良好 partition 模型的文件、事件及分散式工作狀態 | 同 container、同 logical partition 的 transactional batch；官方限制 100 operations、2 MB、5 秒，snapshot isolation。[S12] | 跨 partition 的共同 grant／budget 不會自動 ACID；須選 read consistency，不能用可能過期的讀取授權。session token 不是版本備份。[S13] |
| **Azure Blob Storage** | 原始文字、工具 payload、附件、序列化 snapshot／manifest | 精確 object／version 的條件式讀寫；不是多 row 的控制交易資料庫 | versioning、soft delete、WORM、vaulted backup 各有不同保護範圍／成本；沒有替 DB＋Blob 提供共同提交點。[S4] |

補充候選：**Azure Cosmos DB for Table**。官方提供 Table SDK／wire 相容性、索引、可調 consistency 與備份，可作「想保留 Table API，但需要較強內建復原能力」的評估路徑。[S3][S20]
它不是 Cosmos DB for NoSQL；不能直接套用上表 NoSQL 的 batch／索引／API 契約。FA 已對 Table 的實際 multipart、ETag、失敗與 unknown 行為做有界實作，切後端仍需重新驗證，不能只換 endpoint 宣布相容。

### 身分與私人資料隔離

Table Storage、Azure SQL、PostgreSQL 及 Cosmos DB for NoSQL 都有 Microsoft Entra／managed identity 路徑，但**資料庫能驗 MI，不等於自動知道模型代表哪位人類或哪個 agent**。[S5][S7][S11][S16]
維持 Context API 的受信任 caller→tenant／team／subject 對應，不開放模型自行填 actor／scope，也不讓模型持有任意 SQL、Table 或 Blob 管理權限。

SQL／PostgreSQL 的 RLS 可再提供 DB 層防線；須配置讀取與寫入規則、低權限服務角色及可信 connection context。PostgreSQL owner／`BYPASSRLS` 等有繞過條件，不能用高權限連線證明私人隔離。[S21][S22]
Table 的 partition key、Cosmos 的 partition key、Blob 的路徑或內容 hash 都不是使用者授權。若用共用服務 MI，應用層仍要做存取檢查；對強隔離需求再評估分 table／container／database 的治理代價。

### 備份與「不丟資料」的真實邊界

| 服務 | 官方能力 | 對 FA 的意思 |
|---|---|---|
| Table | 沒有傳統 PITR；ZRS 同區同步，跨區 GRS 為非同步，非計畫 failover 可能遺失資料。[S3] | 複寫不是歷史備份；需要自訂一致性匯出／事件保存與實際還原演練，或改用具內建 PITR 的候選 |
| Azure SQL Database | 預設 PITR 7 天，通常可設 1–35 天，Basic 為 1–7 天；LTR 最多 10 年且非預設。[S17] | 保留窗口不等於即時同步；PITR／geo-restore／HA 需分開估 RPO／RTO。不能把 Managed Instance 的數字套用這個產品 |
| PostgreSQL Flexible Server | 原生備份保留 7–35 天；文件說 WAL 歸檔 RPO 延遲一般可到 5 分鐘，geo-backup 非同步延遲可到 1 小時。[S14] | 同區 HA 的同步承諾不能外推到備份或跨區；還原時間取決於資料／WAL，另核對 server配置與網路 |
| Cosmos DB | Continuous backup／PITR 依可用 tier／API；以實際 latest restorable timestamp 判斷可恢復範圍。[S15] | 不是每次寫入都已即刻備份；新 account 還原不自動帶回所有 RBAC、firewall、private endpoint；TTL 也可能讓還原資料再次被刪 |
| Blob | 版本、soft delete、WORM、PITR及vaulted backup有各自適用範圍；ARM lock 不保護資料面的單一 Blob 刪除。[S4] | 應先定保留／刪除與復原需求，再選組合；開 versioning 不等於所有版本永不被刪 |

多儲存層恢復不能只各自「還原成功」。必須驗證 event sequence、控制 receipt、manifest、全部必要 object versions 與 schema 能組成同一份可用狀態；未知／缺件不得回完整成功。還原舊 DB 也不能讓曾在外部生效的操作變回可重送，仍需保留原 operation identity 與核對責任。

## 5. Foundry Memory、Foundry 對話與 Search 的位置

官方明確把 Memory 描述為從對話**擷取、整合、檢索**的長期知識；LLM 會合併相似資訊。新版 Preview 也有 item CRUD、TTL與直接 remember／forget，所以不能說它只能自動摘要、完全不能保存指定內容。[S19][S23]
但這些 API **沒有在本次查讀文件中提供 FA 所需的 event＋grant＋budget＋obligation 多紀錄交易契約或完整原件保留／恢復保證**，不宜作唯一權威庫。

Foundry／SDK 的 conversation、thread、checkpoint 可服務各自執行上下文；本研究沒有驗證它們能完整匯出／保存所有跨 session 材料，也不把其內部使用某資料庫推定成 FA 可以任意讀寫的資料庫服務。
Azure AI Search 的官方可靠性文件直接說它**不是 primary data store**，索引可以由主資料重建；Foundry IQ／檢索層也不能代替精確工作與批准權威。[S24]

## 6. FA 的最小落地方式：採原則，不照搬檔案布局

建議資料流如下；這是後續實作草案，不要求本批 M4 多跑 gate：

```text
可信入口與保存規則
  -> 合規原件 bytes／精確版本
  -> 原子提交：事件＋權威狀態／claim＋必要材料關聯＋待處理 intent
       -> 可重建 Memory／Search／可讀 transcript 投影
       -> 本輪 ContextManifest：來源範圍、版本、hash、大小與省略理由
```

**寫入路徑：** 先驗來源與保存範圍。必要小材料跟 event／控制紀錄在同一交易內保存；大材料先有可核對的存續保護，控制交易再附著精確引用。接單成功必須對應已確認 receipt；ack 遺失保存原 ID／digest，先核對原提交，不重新抽取或換 ID 重做。
**讀取路徑：** 工作／批准／已審 claim 先精確讀；Memory／Search 只協助找來源。再核對來源存在、版本、scope、有效時間與支持關係，最後才放入 context。找不到 `cobalt` 時，應回到原 event／claim，不能靠相似摘要猜。
**投影路徑：** 保存 covered sequence、source versions、projector／model／schema 版本與處理結果。跨庫更新用持久 intent／outbox 接續；投影落後需可見，不阻斷已保留原件的精確讀回，也不把舊投影當最新批准。

建議保存的最小資訊：

| 紀錄 | 必要內容與限制 |
|---|---|
| 原始 event | 穩定 ID、可信 source、scope、sequence、schema、合規 payload或受保護參照、hash；內容相同不代表兩次合法事件相同 |
| Material／version | bytes、codec、長度、hash、精確 locator／version、可讀者與保存依據；hash不是授權也不是保留鎖 |
| Approved claim／工作決策 | subject、claim／狀態、支持來源、有效時間、version、supersedes／retracted；保存原話不代表原話已被批准或證實 |
| Checkpoint manifest | 執行者／runtime／schema、conversation與covered sequence、必要引用集合、每個版本及完整性檢查 |
| Projection receipt | 來源範圍／hash、投影版本、Memory native receipt／index進度；不得把處理`completed`當內容正確 |

### 與 v1.2 的銜接

這些建議落在既有 [v1.2 §6–8](FA-AGENT-BEHAVIOR-SPEC.md) 的材料保護、`ContextManifest` 與有來源記憶治理，不建立第二份競爭規格。
`behavior_store.py`、`behavior_operations.py`、`behavior_delivery.py` 是現有隔離控制 core；**並不等於完整 transcript／blob graph／記憶治理／備份系統**，native執行與交付仍未因此啟用。

- **F15：** DB交易不能讓外部遲到請求失效；非冪等重試仍要原 attempt 終局／不可再生效證據，或有效整單位冪等契約。
- **F16：** 批准要綁實際執行內容／版本；保存 hash 後重新讀 `current` alias 仍會出錯，換 SQL 也不會自動修好。
- **F17：** Blob與Table／SQL／PostgreSQL／Cosmos不天然跨資源ACID。引用提交與GC仍須排除競態；unknown不解除材料保護。
- 首切片沿用有據的「必要材料不自動回收」模式；這不是永久保存所有內容，也不能只靠未驗證的布林旗標。無法保護大材料先拒收，不偷偷縮成無保護pointer。
- 恢復只是重建狀態，不重放外部工具副作用；獨立可運行 consumer、送出批准、有效交付回條仍需另行接線與證據。F18–F20／HOST待辦不因本研究關閉。

## 7. 建議的開發與驗收順序

1. **先補來源留存與精確讀回。** 選已批准保存範圍，綁 immutable event／material／版本，不改既有M4 fixture、已發布SOP或unknown紀錄。
2. **再補可重建投影與 ContextManifest。** 摘要／搜尋遺漏時能回源；更正／撤銷先讓舊claim失效，不能用摘要自動批准。
3. **再驗還原完整性。** 同一採集邊界取回 bytes/hash；長內容、中文、換行及binary不被JSON轉換吞掉；缺一個必要blob時必須明確失敗。
4. **最後決定是否搬 DB。** 用共同額度競爭、交易大小／latency、查詢、RPO／RTO、備份演練與成本比較Table、SQL／PostgreSQL候選；不是以向量搜尋排名或Grok使用SQLite決定。

故障案例應包含：重複event不同payload、commit ack遺失、源版本更換、projection落後／損壞、壓縮後原件回讀、跨agent拒絕、retention碰到未結材料、還原後外部效果不重送。這是**建議後續驗收項目，不是本輪已執行測試，也不新增M4 gate**。

## 8. 成本與尚待產品決策的事項

Table＋Blob按儲存／交易／資料移轉及保護功能計費，已有資源不代表新增歷史與備份免費。[S3][S4]
Azure SQL serverless暫停時仍付storage，未暫停時有最低compute計費；持續掃描／consumer可能不適合假設能經常pause，冷啟動也有延遲。[S25]
PostgreSQL需計server、storage、備份與HA；Cosmos需計throughput／RU、storage、區域與備份／restore。[S14][S15]
本研究不填未查到的當區報價，也不承諾新服務可在 POC 總額 US$100 扣除既有用量後的餘額內啟用；部署前用當時的用量與官方計價核定精確範圍。

仍須依產品用途決定保存清單／期限、私人與Team邊界、可接受的RPO／RTO、資料量／寫入率、跨區需求，以及法遵刪除與WORM是否衝突。這些不妨礙先採分層與原件來源契約，但會影響正式SKU／資料庫選型。

## 官方來源

- [S1 Table EGT：同partition、100 entities、4 MiB](https://learn.microsoft.com/rest/api/storageservices/performing-entity-group-transactions)
- [S2 Table Update Entity：ETag／If-Match與412](https://learn.microsoft.com/rest/api/storageservices/update-entity2)
- [S3 Table可靠性、跨區複寫、缺少內建PITR](https://learn.microsoft.com/azure/reliability/reliability-storage-table)
- [S4 Blob資料保護選項、限制與成本](https://learn.microsoft.com/azure/storage/blobs/data-protection-overview)
- [S5 Table Entra／managed identity](https://learn.microsoft.com/azure/storage/tables/authorize-access-azure-active-directory)
- [S6 Azure SQL適用的transaction isolation](https://learn.microsoft.com/sql/t-sql/statements/set-transaction-isolation-level-transact-sql?view=sql-server-ver17)
- [S7 Azure SQL Entra／managed identity](https://learn.microsoft.com/azure/azure-sql/database/authentication-aad-overview?view=azuresql)
- [S8 PostgreSQL transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html)
- [S9 PostgreSQL isolation與預設READ COMMITTED](https://www.postgresql.org/docs/current/transaction-iso.html)
- [S10 PostgreSQL json／jsonb保真差異](https://www.postgresql.org/docs/current/datatype-json.html)
- [S11 Azure PostgreSQL managed identity](https://learn.microsoft.com/azure/postgresql/security/security-connect-with-managed-identity)
- [S12 Cosmos DB NoSQL transactional batch](https://learn.microsoft.com/azure/cosmos-db/transactional-batch)
- [S13 Cosmos DB consistency與session token邊界](https://learn.microsoft.com/azure/cosmos-db/consistency-levels)
- [S14 Azure PostgreSQL backup／restore](https://learn.microsoft.com/azure/postgresql/backup-restore/concepts-backup-restore)
- [S15 Cosmos continuous backup／restore及未恢復設定](https://learn.microsoft.com/azure/cosmos-db/continuous-backup-restore-introduction)
- [S16 Cosmos NoSQL Entra／data-plane RBAC](https://learn.microsoft.com/azure/cosmos-db/how-to-connect-role-based-access-control)
- [S17 Azure SQL Database automated backups](https://learn.microsoft.com/azure/azure-sql/database/automated-backups-overview?view=azuresql)
- [S18 Event Sourcing模式及採用成本](https://learn.microsoft.com/azure/architecture/patterns/event-sourcing)
- [S19 Foundry Memory：擷取、整合、檢索及Preview限制](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-memory)
- [S20 Cosmos DB for Table相容性與特性](https://learn.microsoft.com/azure/cosmos-db/table/overview)
- [S21 SQL Row-Level Security](https://learn.microsoft.com/sql/relational-databases/security/row-level-security?view=sql-server-ver17)
- [S22 PostgreSQL Row Security及繞過條件](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [S23 Foundry Memory CRUD／retention／commands](https://learn.microsoft.com/azure/foundry/agents/how-to/memory-usage)
- [S24 Azure AI Search不是primary data store](https://learn.microsoft.com/azure/reliability/reliability-ai-search)
- [S25 Azure SQL serverless成本與適用情境](https://learn.microsoft.com/azure/azure-sql/database/serverless-tier-overview?view=azuresql)
