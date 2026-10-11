# Grok Bot「Box」：官方設計研究

研究基準：2026-09-12。由研究 agent 的官方來源查證結果整理並保存。

**2026-09-14 FA 決策更新：** [ADR-0003](adr/0003-hosted-environment-continuity.md) 已取代下方 2026-09-13 的固定 VM 限制，改採 Hosted 環境／工作連續性。競品文件事實保留；它們既不要求 FA 新增 VM，也不替代 FA 自身的持久環境或效能驗收。

本研究未登入、部署、安裝或操作 Grok Bot。以下是文件證據，不是 source implementation 或 Box API 的實測。FA 的人類觀看／接手需求已暫緩；本文件只記錄對標產品能力，不將其加回 FA 首版。

**2026-09-13 適用界線**：FA 已明確採用每 agent 長期固定同一台 VM，只在故障或明確維護時移轉，見 [設計決策](adr/0001-persistent-agent-computer.md)。下文保留競品文件事實；不能再把「持久資料＋可替換 compute」當成 FA 正常 idle／resume 的設計依據，也不能用 Grok Bot 證據支持尚未量測的 OpenClaw 10 倍效能目標。

## 核心結論

已找到高度吻合的一手產品：x.ai 的 Grok Bot。官方使用 **cloud computer／Agent Computer**；尚未確認「Box」是其正式資源名稱。

最重要的差異是：**Grok Bot 不是每 Bot 一台 microVM，而是每個使用者一台持久 Firecracker microVM，該使用者所有 Bots 共用。** 各 Bot 的 screen 是不同工作表面，不是安全邊界。

來源：[產品公告](https://x.ai/news/introducing-grok-bot)、[Teams and enterprises](https://docs.x.ai/grok-bot/teams-and-enterprises)、[Computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)。

## 1. 電腦映射與隔離

| 問題 | 文件結果 |
|---|---|
| 電腦實作 | **明文支持**：Firecracker microVM，各有 kernel、memory、virtual devices。 |
| 隔離單位 | **明文支持**：per-user；同一使用者的所有 Bots 共用一台電腦。 |
| Bot screen | **明文支持**：各 Bot 可有不同 screen，但不是隔離或 credentials 邊界。 |
| Team 邊界 | **明文支持**：member 的電腦可跨其所屬 Teams；不能把 Team 當成電腦隔離單位。 |
| OS／託管 | **明文支持**：Linux，Cursor cloud 管理。底層 hyperscaler、發行版、CPU／RAM／磁碟規格未確認。 |
| Box／Computer ID | **未公開確認**：未取得可驗證的 Box API 或 ID schema。 |

官方短引文：

> “one persistent Firecracker microVM per user, shared by every Bot that user runs.”

> “The screens are separate work surfaces, not separate security boundaries.”

來源：[Teams and enterprises](https://docs.x.ai/grok-bot/teams-and-enterprises)、[Computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)、[Identity and access](https://docs.x.ai/grok-bot/identity-and-access)。

## 2. 電腦生命週期

| 操作／狀態 | 官方描述與界線 |
|---|---|
| 關閉使用者 app／laptop | 背景 turn／routine 可繼續；不代表 compute 永遠不休眠。 |
| Idle | 自動 hibernate；不是刪除。門檻、RAM snapshot、wake SLA 未公開確認。 |
| Update | 以新 image 重建並保留 durable state／member files；手動套件、暫存與未提交 application state 應視為可替換。 |
| Recover | 可替換無法連線的電腦並保留 durable state；不等於任意時間点還原。 |
| Reset | 回到最近 durable snapshot／synced data；未同步工作可能遺失。 |
| Admin terminate | durable disk 保留；下次 session 啟用 fresh computer。 |
| Organization disable | 封鎖產品存取，但不因此刪除 member computers。 |
| Delete Bot | 移除 Bot profile、conversation、routines；共享檔案與登入可能仍留在電腦。 |
| 帳號刪除 | 官方說沒有獨立 Grok Bot account，刪除對應 Cursor account 的範圍更廣；不能等同只停掉 compute。 |

**不能推論的承諾**：官方沒有公開保證 hibernate／重建後原 RAM、所有程序、GUI 視窗或 network sockets 都復活。

**登入恢復的限定**：Computer 文件描述 supported sign-ins 可保留；Security FAQ 也警告 image recreation／政策變更可能掉登入。不能摘要成永遠不必重登。

來源：[Security](https://docs.x.ai/grok-bot/security)、[Computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)、[Troubleshooting](https://docs.x.ai/grok-bot/troubleshooting)、[Security FAQ](https://docs.x.ai/grok-bot/security-faq)、[Delete account](https://cursor.com/help/grok-bot/delete-account)。

## 3. Agent 身份不等於安全 principal

| 層次 | 文件結果 |
|---|---|
| Logical Bot | 有 name、job、profile、conversation 與累積工作 context，可 share／duplicate／delete。 |
| Bot 自身的 IAM identity | 官方明言 Bot 沒有自己的 identity 或 credentials，原則上代表 signed-in member 行動。不能把角色人格當成獨立安全 principal。 |
| 平台帳號 | Cursor account／SSO；沒有獨立 Grok Bot account。 |
| Browser／CLI credentials | 同一使用者的 Bots 共用 browser cookies、sessions 與電腦內 CLI credentials。 |
| OAuth connector tokens | 存在 Cursor backend，不放入電腦，也不交給模型；Bots 透過工具調用。 |
| Connector 例外 | Team-managed connectors 可以使用 Team／service-account credentials。不能省略例外後宣稱一律只受使用者權限限制。 |
| Revoke | 刪 Bot／terminate computer 不等於撤銷 IdP、SaaS browser session 或 connector grant。 |

官方短引文：

> “A Bot has no identity or credentials of its own”

> “Team-managed connectors are the one exception: they may use team or service-account credentials.”

來源：[Security](https://docs.x.ai/grok-bot/security)、[Bots](https://docs.x.ai/grok-bot/bots)、[Identity and access](https://docs.x.ai/grok-bot/identity-and-access)、[Computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)。

## 4. Desktop、工具位置與協作

- **明文支持**：shell、browser、computer-use 在 hosted computer 上執行；各 Bot 有 screen，也提供 Agent Computer 預覽／接手，所以不能概括成只有 headless browser。
- **不能擴張**：不是所有 tools 都必然在 microVM；connector authentication／tokens 在 backend，local execution 與 separate Cloud Agent computers 另有執行路徑。
- **明文支持**：Bots 可直接非同步傳訊息、group chat、交接，接收方可被喚醒。
- **明文支持**：同使用者 Bots 在同一台電腦共用 `/workspace`；可讀其他 Bots 保存的檔案。
- **未公開確認**：沒有足夠證據表明不同使用者／不同 microVM 能共同掛載同一 Team filesystem。
- **官方建議而非強制機制**：協作文件建議各階段 single owner；沒有公開證據可證明其實作了 write lease、CAS 或原子編輯权交接。
- **明文支持**：share Bot 會在接收者帳號建立 copy，不會分享原本電腦、登入與完整 conversation。

來源：[Computer and apps](https://docs.x.ai/grok-bot/computer-and-apps)、[Teams and enterprises](https://docs.x.ai/grok-bot/teams-and-enterprises)、[Files and results](https://docs.x.ai/grok-bot/files-and-results)、[Message and collaborate](https://docs.x.ai/grok-bot/chat-and-collaboration)、[Bots](https://docs.x.ai/grok-bot/bots)。

## 5. 對 FA 的設計含義

以下均為**設計推論**，不是宣稱 Foundry 已有相同能力，也不構成外部 VM 選型。

1. **持久 agent 與 compute 生命週期分開**：Agent 身份、角色、memory、durable data 不必隨執行資源更換而消失。依 ADR-0003，FA 可在 Hosted idle 後換 compute，但仍須驗證完整必要環境與工作恢復，不必承諾原程序復活；競品設計不能代替這項驗收。
2. **工具執行位置要一致且明確**：需要共用桌面檔案的 CLI／browser／computer-use 綁到同一 agent computer；connector credentials 可以由控制層管理，不必全部塞進 OS。
3. **不同生命週期分開操作**：pause routine、stop task、terminate compute、revoke grant／session、delete agent、delete data 不是同一件事。
4. **不要照抄 per-user 共享憑證邊界**：FA 已要求每 agent 專屬環境與 Team 授權；Grok Bot 的 per-user 共用電腦和登入，不等價於 FA 的 per-agent 隔離。
5. **共同目錄不證明跨電腦掛載**：Grok Bot 的 `/workspace` 是同一台 microVM 內的共享；FA 的多個 Hosted sessions 必須各自掛同一 Team share，仍需獨立證據。

## 6. 證據限制

- 「Box」正式名稱／API 尚未確認；本研究找到的是 x.ai 官方 Grok Bot 的 Agent Computer。
- 未確認底層 cloud vendor、microVM sizing、RAM snapshot、跨電腦 Team mount、公開 Box ID schema。
- 部分搜尋 metadata 的 crawl timestamp 晚於使用者提問時刻；研究未用它判定發布日期，核心內容使用官方原頁。
- Canonical billing 文件指出開始執行的 run 可能完成並超過 limit；未找到獨立 computer CPU-hour／disk-month／hibernate 費用契約，因此沒有推測資源價格。[Billing](https://cursor.com/help/grok-bot/plans)
- 官方 DPA 的資料刪除條款與法律保留例外，不是 idle disk TTL；control-plane backups 也不能外推成每台電腦都有客戶可用 PITR。[DPA](https://cursor.com/terms/dpa)

**停止點**：已有足夠一手資料辨識產品並比較生命週期、身份與共享範圍；未知項目保留未知，不擴張成競品全功能分析。
