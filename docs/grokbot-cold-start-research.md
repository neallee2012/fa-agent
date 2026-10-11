# Grok Bot 冷啟動與送訊息延遲研究

調查日期：2026-10-05（台北）。範圍：x.ai Grok Bot / Cursor-hosted Agent Computer。本文只引用官方 x.ai / Cursor 文件、x.ai 公告，以及 Firecracker 官方文件作為泛用 microVM 背景；沒有登入、安裝、操作 Grok Bot，也沒有使用外洩原始碼。

## 一句話結論

官方可確認 Grok Bot 的工作跑在 Cursor-hosted persistent cloud computer，且個人 Bots 共用「每使用者一台」Firecracker microVM；背景 turn 和 routine 可在 app 或 laptop 關閉後繼續，但 idle computer 會自動 hibernate。官方只給「initial setup / image update 可花 several minutes」、「fresh computer reconnect can take a few minutes」、「30 days inactive 可自動 terminate」這類粗粒度時間；沒有公布從 client 送訊息到 Bot 開始處理的分段秒數。[X0][X1][X2][X3][X9]

## 架構與名詞

- **Bot**：有 name、job、自己的 conversation 和會累積的 working context；不是單次 prompt。[X5]
- **Agent Computer / cloud computer**：persistent cloud computer，有 browser、filesystem、terminal；使用者可看 Agent Computer，也可在敏感步驟 takeover。[X0][X1]
- **產品公告語氣**：x.ai 公告說 Bots have their own computer、可跨 apps / inboxes 工作；官方 docs 進一步釐清，個人帳號下是所有 Bots 共用同一台 user computer。[X16][X1][X2]
- **隔離單位**：官方企業文件明寫「one persistent Firecracker microVM per user」；同一使用者的 Bots 共用檔案、cookies、CLI credentials 和 computer，Bot screen 是工作面，不是安全邊界。[X1][X2][X8]
- **託管位置**：Grok Bot runs in Cursor's cloud / Cursor-hosted cloud computers；不是 Microsoft VM，也不是 BYO image 或 on-prem。[X2][X3]
- **client 位置**：desktop / mobile app 主要做 chat、review、approval；工作跑在 hosted computer。Team Bot 也可從 Slack DM、channel、thread 被叫醒。[X2][X4][X6]
- **backend 位置**：OAuth connector tokens stay on Cursor's connector backend；Bots invoke tools without receiving tokens。Cursor manages model selection，沒有 customer-facing model picker。[X2][X3]
- **microVM 內的位置**：shell、browser、computer use inside the hosted computer；`/workspace` 是同使用者 Bots 共用的 durable workspace。[X1][X2][X10]
- **Team Bot 例外**：Team Bot 在 owner chat 用 owner computer；teammate app/Slack DM 用 teammate computer；Slack channel/group/thread 用該 Team Bot 的 shared computer。[X6]
- **Firecracker 泛用事實（不是 Grok Bot 數字）**：Firecracker 官方規格在特定 host / minimal guest 下測 `InstanceStart` 到 Linux `/sbin/init` <=125 ms；snapshot restore 以 memory file on-demand mapping 加速，但 performance depends on memory size、vCPU count、device count，且網路連線不保證保留。[F1][F2]

## 送訊息到開始處理的步驟表

| 步驟 | 誰負責 | 官方說法 | 官方數字 | 來源 |
|---|---|---|---|---|
| 0. client 送出訊息 | client | 使用者可在 desktop / mobile 對 Bot 傳文字、語音、附件；Team Bot 可在 Slack DM、channel、thread 被 DM 或 @mention。 | 官方文件沒有給數字 | [X4][X6][X11] |
| 1. client / backend 接收與同步 | client + backend | desktop/mobile 是同一批 Bots、conversations、routines、connectors、shared cloud computer；desktop and mobile apps are thin clients for chat, review, approvals。 | 官方文件沒有給數字 | [X2][X11] |
| 2. Bot / conversation 路由 | Grok Bot backend | 官方公開使用者層是 Bot conversation / group / Team Bot chat；沒有公開等同 FA `agent_session_id` 的 server session 建立流程。 | 官方文件沒有給數字 | [X4][X5][X6] |
| 3. 找到要用的 computer | backend | 個人 Bot：每使用者一台 persistent Firecracker microVM，所有 personal Bots 共用；Team Bot 依 conversation 類型選 owner、teammate 或 Slack shared computer。 | 官方文件沒有給數字 | [X2][X6][X8] |
| 4. 暖路徑：computer 已醒 | backend + microVM | cloud work 可在 app/laptop 關閉後繼續；一個 Bot 在自己的 screen 一次只能跑一個 computer-use task，多個 Bots 可在不同 screens 平行。 | 官方文件沒有給數字 | [X0][X1] |
| 5. idle 路徑：hibernate 後喚醒 | backend + microVM | Idle computers hibernate automatically；hibernation is not deletion。Sleeping computers apply policy changes when they next wake。 | 官方沒有給 wake 秒數 | [X3][X12] |
| 6. fresh computer 路徑 | backend + microVM | terminate 會刪 running/hibernated computer 但保留 durable disk；member 下一則 message starts a fresh computer。After terminate，reconnect can take a few minutes。 | 「a few minutes」；沒有更細分 | [X9] |
| 7. 初次 setup / image update | backend + microVM | 初次 computer setup runs in background；troubleshooting 說 initial setup and image update can take several minutes，UI 可能顯示 **Starting your computer** / **Updating your computer**。 | 「several minutes」；沒有 percentile | [X7][X13] |
| 8. Bot turn 開始 | backend + model/microVM | Bot takes on multi-step work，conversation transcript shows tool activity、computer use、questions、approvals、files。Direct message from user can take priority over background work and redirect current turn。 | 官方文件沒有給數字 | [X0][X4] |
| 9. 工具與模型執行 | backend + microVM | shell/browser/computer use 在 hosted computer；OAuth connector tokens 在 Cursor backend；Cloud Agent delegation runs on separate computers；Cursor manages model selection。 | 官方文件沒有給數字 | [X2][X3] |
| 10. 結果 / attention | backend + client | conversation 顯示結果、草稿、tool activity；Bot notification 可在完成或需 input 時通知，mobile push still rolling out。 | 官方文件沒有給數字 | [X4][X11][X14] |

## 冷啟動 vs 暖啟動

- **Grok Bot 官方沒有使用 FA 式 cold/warm 名詞**；本文只用 lifecycle 口語定義，不能當官方狀態名。[X3][X9]
- **暖啟動**：computer 已醒、Bot turn 可直接排入既有 cloud computer；官方沒有公布「訊息到第一個 model/tool activity」的時間。[X0][X1]
- **hibernate wake**：idle computer 自動 hibernate，但不是刪除；官方沒有給 idle 門檻或 wake latency，只說 sleeping computers 在 next wake 套用 policy changes。[X3][X12]
- **app / laptop closed**：不會停止 cloud work；background turn 和 routine 可繼續，mobile 也連到同一批 Bots、conversations、routines 和 shared cloud computer。[X0][X1][X11]
- **routine wake**：routine 可排 schedule 或 event trigger；background routines can run while your laptop is closed；若長時間離開，Grok Bot may ask whether to keep routines running and pause them if no response。[X15]
- **computer starting UI**：若顯示 **Starting your computer** 或 **Updating your computer**，官方說 initial setup / image update can take several minutes；若進度不動，才 retry / restart / update。[X13]
- **recreate / update**：admin recreate 會 build replacement computer，current computer stays available until replacement ready；mid-turn Bot pauses at a safe point and resumes on new computer，若不能及時 pause 該 member recreate 失敗。[X9]
- **terminate / fresh computer**：terminate 停止 running work；下一則 message starts a fresh computer on same durable disk；running turn is lost；reconnect can take a few minutes。[X9]
- **recover / reset**：Recover computer 和 Update preserve durable files and logins；Reset restores last saved snapshot and can lose recent or unsynced work。[X13]
- **30 天 inactive terminate**：Enterprise 可開 Terminate Inactive Computers；computer hibernated 30 days without use 後 terminate，下一則 message 會啟 fresh computer；awake / working computer 不會被這設定 terminate。[X9]
- **Firecracker 泛用 cold/snapshot 數字不能套到 Grok Bot**：Firecracker 的 <=125 ms boot test 是 minimal VM 到 `/sbin/init`，不是 Cursor Grok Bot 的 backend、model、tool、desktop readiness 或 user-visible wake latency。[F1][F2]

## 與 FA 的概念差異

| 面向 | Grok Bot / Agent Computer | FA Portal / Foundry Hosted Agents |
|---|---|---|
| 執行單位 | 個人 Bots 共用每使用者一台 Firecracker microVM；Team Bot 在 Slack shared conversation 可用該 Bot 的 shared computer。[X2][X6] | Neal 本題指定：一個 Foundry Hosted session（VM/container）per Agent；詳見 `hosted-cold-start-latency-research.md`。[FA1] |
| client → server | desktop/mobile/Slack 是 chat/review/approval client，work 在 Cursor cloud computer；官方未公開完整 message queue / turn admission 內部流程。[X2][X4][X6] | Neal 本題指定：FA web portal sends a message to FA API；FA API invokes Hosted agent via Foundry invocations protocol。[FA1] |
| cold / warm 控制 | 官方只說 idle hibernate、fresh computer after terminate、setup/update several minutes；沒有公開 idle threshold 或 per-step latency。[X3][X9][X13] | Neal 本題指定：Hosted sessions can go idle after timeout；stage 目前 900 s；cold vs warm 細節看 `hosted-cold-start-latency-research.md`。[FA1] |
| 工具位置 | shell/browser/computer-use 在 microVM；OAuth connector tokens 在 Cursor backend；models 由 Cursor 管理。[X2][X3] | Neal 本題指定：FA 經 FA API 呼叫 Foundry Hosted agent；FA 欄不另查 Foundry，詳見既有 FA 研究。[FA1] |
| 發版 / 換版 | 官方有 Update/Recreate/Terminate/Reset computer；沒有看到「每 Bot follow 到新 Agent version」的 Grok Bot 公開契約。[X9][X13] | Neal 本題指定：release 後每個 Agent 必須 follow 到新 Agent version。[FA1] |
| 協作邊界 | personal Bots 共享同一 user computer；Team Bot Slack channel/thread 可能用 shared computer，需避免在 channel thread 放不該共享的資料。[X1][X6] | Neal 本題指定：FA 比較只做概念層；完整 Team trust boundary 看專案 ADR，不在本文件重查。[FA1] |

## 需要實測才能知道的項目

1. desktop/mobile/Slack client 按送出，到 Grok Bot backend 接收並建立 turn 的時間。
2. backend queue / admission / Bot routing / Team Bot Slack routing 各段時間。
3. warm computer：訊息送出到第一個 assistant token、第一個 status、第一個 tool/computer action 的時間。
4. hibernated computer：從送出訊息到 **Starting / reconnecting / first action** 的時間；官方沒有 wake SLA。
5. terminated fresh computer：下一則 message 到 computer ready、Bot 收到 turn、第一個 action 的時間。
6. initial setup、Update、Recreate、Recover、Reset 的使用者可見耗時和失敗率。
7. `/workspace`、browser profile、Team Setup、network client、檔案量是否影響 wake / fresh start。
8. routine schedule/event trigger 到 first action 的延遲；app/laptop closed 時是否不同。
9. 同一 Bot screen 正在跑 computer-use task 時，第二則 message 被排隊或 redirect 的實際延遲。
10. 多個 Bots 平行工作時，同一 user microVM 的 CPU/IO contention 對 latency 的影響。
11. connector backend call、browser automation、shell command、model first-token latency 的分段。
12. approval / Auto Review / secure secret handoff 對首次 tool action 的延遲。
13. Slack Team Bot shared computer 和 app 1:1 conversation 的 latency 差異。
14. 30 天 inactive terminate 或手動 terminate 後，fresh computer 是否穩定保留 synced logins/files。
15. Firecracker microVM 啟動數字和 Cursor Grok Bot user-visible latency 之間的差距。

## 使用者實測（待補）

- 測試日期／帳號類型：待補。
- client：desktop / mobile / Slack / other。
- 起始狀態：active / hibernated / terminated / recreate / reset。
- 量測點：send click、backend acknowledgement、computer status、first assistant token、first tool/computer action、final result。
- 結果表：待補。

## 第三方分析（非官方，僅供線索）

- `leescot` 的 Grok Bot Internals 四章分析了公開 client bundle / `host-main.cjs` 中的 prompt assembly、compaction、skills 等線索；它不是 x.ai / Cursor 官方文件，本文沒有用它支撐主表任何 latency 或 lifecycle claim。[T1]
- 若未來用第三方 bundle 分析設計量測點，仍需以官方文件或使用者自己的實測確認；不能把非官方常數寫成 Grok Bot production SLA。[T1]

## 官方來源清單

[X0]: https://docs.x.ai/grok-bot/overview
[X1]: https://docs.x.ai/grok-bot/computer-and-apps
[X2]: https://docs.x.ai/grok-bot/teams-and-enterprises
[X3]: https://docs.x.ai/grok-bot/security
[X4]: https://docs.x.ai/grok-bot/chat-and-collaboration
[X5]: https://docs.x.ai/grok-bot/bots
[X6]: https://docs.x.ai/grok-bot/team-bots
[X7]: https://docs.x.ai/grok-bot/get-started
[X8]: https://docs.x.ai/grok-bot/security-faq
[X9]: https://docs.x.ai/grok-bot/computers
[X10]: https://docs.x.ai/grok-bot/files-and-results
[X11]: https://docs.x.ai/grok-bot/mobile
[X12]: https://docs.x.ai/grok-bot/private-networks
[X13]: https://docs.x.ai/grok-bot/troubleshooting
[X14]: https://docs.x.ai/grok-bot/settings-and-notifications
[X15]: https://docs.x.ai/grok-bot/skills-routines-and-automations
[X16]: https://x.ai/news/introducing-grok-bot
[F1]: https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/SPECIFICATION.md
[F2]: https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/snapshotting/snapshot-support.md
[F3]: https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/design.md
[FA1]: hosted-cold-start-latency-research.md
[T1]: https://leescot.com/research/grok-bot-internals/
