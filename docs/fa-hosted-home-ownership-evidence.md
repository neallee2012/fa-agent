# Hosted HOME：非 root 可寫權限的官方查證

日期：2026-09-14（+08）。原查證範圍：官方文件、第一方公開schema／source與既有POC結果；該階段沒有新增Azure操作。

**結論：`NOT_CONFIRMED`。** 官方明確支援session的`$HOME`持久化及stop後保留資料，
但這次查證沒有找到「任意non-root UID如何取得HOME初始寫入權限」的明確設定／初始化契約。
這是**尚未確認官方支援方式，不是判定non-root不受支援**。
後續12:40 +08使用者另批准受控root-init實驗，A7已通過非root純檔案stop/resume；
見[M1實測](M1-PRIVATE-ENVIRONMENT-POC.md)。這項實測不改寫下面的官方契約查證結論。

本地問題與支援草稿見[M1結果](M1-PRIVATE-ENVIRONMENT-POC.md#後續定位只查文件與重讀原件)。
本文件不包含raw trace、私人request IDs或支援封包；未發出Microsoft支援案件。

## 官方保證了什麼

| 問題 | 可引用的結論 | 來源 |
|---|---|---|
| 哪裡持久化 | 文件以`$HOME`描述session持久化檔案系統；`/files`上傳也寫入同一HOME儲存 | [S1] |
| idle／stop後 | idle可回收compute；同一session ID恢復時取回其持久狀態。手動stop保留persistent filesystem volume | [S1]、[S2] |
| 保留期限 | 文件描述30天無活動後清除；`expires_at`為距最後活動30天的rolling期限。不是固定最長30天壽命，也不是永久保存 | [S1]、[S4] |
| 寫入與關閉責任 | runtime contract要求在SIGTERM時flush HOME下的必要checkpoint／檔案 | [S3] |
| Files API的path | 明列為`relative to the session home directory`，不是選擇新的HOME、volume或POSIX使用者的參數 | [S6] |
| 初始UID/GID／mode | 已讀文件沒有明列HOME的初始owner、group、mode、初始化時序或non-root權限準備程序 | [S1]、[S2]、[S3] |

決定性原文：

> “The platform writes files uploaded via the `/files` endpoint into `$HOME`, where they share the same storage.” [S1]

> “Stopping a session terminates its running compute while preserving the persistent filesystem volume.” [S2]

因此不能把Storage REST、Session Files上傳成功或本機Docker結果當成應用UID可寫HOME的證明，
更不能把session-private HOME當成多agent共享filesystem mount。

## 實際公開schema有沒有權限設定

不是只看overview：TypeSpec固定在`Azure/azure-rest-api-specs`的
`e31efa5c52e535addb4571e6e5dab2592f818955`，並比對emitted OpenAPI v1。[S4]、[S5]

| 定義 | 該快照的公開欄位 |
|---|---|
| `HostedAgentDefinition` | `kind`、`cpu`、`memory`、`environment_variables`、`container_configuration`、`protocol_versions`、`code_configuration`、`telemetry_config`、`session_configuration`；另繼承`rai_config` |
| `ContainerConfiguration` | `image`、`registry_connection_id` |
| `SessionConfiguration` | `idle_timeout_seconds`；該schema範圍120–3600秒，預設900秒 |
| `CreateAgentSessionRequest` | `agent_session_id`、`version_indicator` |

**上述定義沒有找到run-as user、security context、`fsGroup`、volume ownership或HOME初始化控制項。**
這個結論只涵蓋列出的公開定義，不能外推為平台內部沒有能力或不存在未公開支援程序。
一般`environment_variables`可設定字串，也不等於每個保留環境變數都可覆寫，
或平台會跟著改掛載、snapshot範圍及ownership。

azd的`Agent.json`與Go mapping同樣沒有找到額外ownership控制項。[S7]
這是固定commit的公開source，不冒稱與本機`azd 1.32.0`／extension `1.0.0-beta.13`逐byte相同，
也不因此升級CLI或替既有環境改idle timeout。
舊`AgentContainers`介面標記`@removed(Versions.v1)`，不是現行session權限設定入口。[S12]

REST `api-version=v1`與container protocol `2.0.0`是不同層的版本，不能混用。

## 範例與相符SDK版本

官方note-taking範例用`os.environ.get("HOME", os.getcwd())`選路徑並直接開檔，
該寫入路徑沒有調整owner／mode；其Dockerfile沒有明列`USER`或HOME初始化。[S8]
這只表示範例假設目錄可寫；沒有查驗base image的實際UID，不能說官方已要求或驗證root執行。
本POC也不採用其cwd fallback來假裝持久化。

對應本POC的`azure-ai-agentserver-invocations_1.1.0`與
`azure-ai-agentserver-core_2.1.0`官方tags均指向
`52d921c904a95005c42ace58511ea194aa9d6c01`，以下SDK判讀使用這個release source，而不是current main。[S9]

- Invocations 1.1.0依賴`azure-ai-agentserver-core>=2.1.0,<2.2.0`；只pin Invocations並不是Core exact lock。A7候選另明列Core2.1.0，實際交付仍以各輪image digest定位。
- Core的state-path resolver讀`AGENTSERVER_STATE_ROOT`，預設`Path.home() / ".agentserver"`；它不是平台volume設定。
- 該resolver明說：`The path is not created on disk — callers mkdir lazily on first write.` 它不建立目錄、不改owner／mode，也不把HOME設定成`/home/session`。
- 同release的local task provider只在其寫入函式呼叫`path.parent.mkdir(parents=True, exist_ok=True)`；這不是HOME初始化器，也不是本POC已啟用task provider的證據。

`x-platform-server`可由容器內的Core host／ASGI middleware產生，Python版本取自`sys.version_info`。[S10]
所以本POC的`azure-ai-agentserver-core/2.1.0 (python/3.11)` header不是宿主VM／平台內部版本的證明；
Invocations constructor呼叫Core constructor，只有Core segment也不代表Invocations adapter沒載入。
已查的host／state-path路徑沒有補上HOME ownership契約，不能因此宣稱整個SDK都已排除為原因。

## 已知問題與容易誤用的線索

限定查閱第一方repos的六組issues／PR搜尋及azd、Core、Invocations changelogs，
沒有找到官方維護者對此種Hosted HOME root-owned／non-root不可寫問題給出的明確初始化修法。
這不是對所有公開或內部追蹤系統的完整否定。

- [microsoft-foundry/foundry-samples#680](https://github.com/microsoft-foundry/foundry-samples/issues/680)的[維護者回應](https://github.com/microsoft-foundry/foundry-samples/issues/680#issuecomment-4701846919)將其界定為Windows local-invoke的`/home`路徑問題，不是Linux Hosted HOME ownership契約。
- [microsoft-foundry/foundry-samples#515](https://github.com/microsoft-foundry/foundry-samples/issues/515)的[non-root回報](https://github.com/microsoft-foundry/foundry-samples/issues/515#issuecomment-5583361943)談`USER 1654`啟動；作者association為`NONE`，且不是HOME寫入／持久化驗收，不能作官方修法。

另外發現azd公開source有`AZD_AGENT_RESUME_SESSION_ON_DEPLOY`：
預設關閉、opt-in且best-effort，讓部署流程stop舊session後保留**同一session ID**供新版本使用。[S11]
其註解用語是`Empirically (validated against a live Foundry agent)`，並提到`/home/session`。
這是第一方client的實作線索，不是HOME ownership修正或替代session還原契約，
本機extension是否有此功能也未在本輪確認。

Learn另描述explicit `version_indicator`可固定session版本，即使已有新版本。[S2]
因此需向Microsoft確認explicit pin與endpoint-resolved session re-bind的差異；
不直接啟用carry-over、不覆寫本POC的version binding防護，也不再把「更新版本必須新session」寫成無條件規則。

## 決策與下一步

| 做法 | 判斷 |
|---|---|
| 平台HOME中的檔案沿同session stop/resume恢復 | 持久化語意有官方依據；A7經root-init的專用目錄另有實測，任意non-root初始化契約仍未確認 |
| Dockerfile或deployment改HOME | 字串／image設定不等於持久化目錄重新配置；本輪不作修法 |
| `AGENTSERVER_STATE_ROOT`改址 | SDK路徑設定，不是平台持久化或ownership控制面 |
| build-time `install -o`／`chown` | 本POC的這個候選未解決runtime權限；不可只靠image宣告推定成功 |
| root啟動後初始化再降權 | 後續使用者已批准限定實驗；A7只建立新的`.fa-m1`再降權，純檔案恢復通過，不代表官方通用保證 |
| Session Files upload或`/tmp`替代 | 不能取代應用UID的HOME可寫性與恢復驗收 |
| 明確受支援的non-root初始化修法 | **`NOT_CONFIRMED`；正式支援契約仍待確認，但不因此否定另獲批准的受控實驗** |

給Microsoft的五個問題（草稿，未送出）：

1. Hosted v1／Invocations `2.0.0`支援numeric non-root `USER`的條件為何？新session HOME的預期path、UID/GID、mode由哪個階段準備？
2. UID與HOME owner不同時，正式的API／部署設定／初始化方式是哪一個？是否依OCI `USER`準備權限？若需privileged bootstrap，請提供正式條件與範例。
3. Dockerfile HOME與deployment HOME的優先序／保留規則為何？覆寫是否真的改變持久化目錄，以及Session Files API的對應？
4. idle／stop-resume如何保存UID/GID、mode、ACL等POSIX metadata？版本變更、UID變更、explicit pin與session re-bind的正式邊界為何？
5. non-root看到root-owned 0755而不能mkdir，是否為已知限制／問題？請提供受影響版本、追蹤編號及最少必要診斷欄位。

如需補資料，另行批准的唯讀probe可取UID/GID／groups、HOME與passwd home／realpath、
必要目錄的stat，以及**僅與HOME相關**的mountinfo；不傾印完整env、mount清單、憑證或檔案內容。
這些觀察仍不等於已證明平台內部機制。後續A7實驗補到了SDK import前的HOME／owner與降權後UID/GID／groups／capabilities；
沒有擷取完整mountinfo、送出支援草稿或推定平台掛載實作。新的雲端範圍仍須明確批准，不沿用舊餘額盲測。

## 第一方來源快照

[S1]: https://github.com/MicrosoftDocs/azure-ai-docs/blob/83c7cb152d2b4fc9008966da1e80aa7fda08412e/articles/foundry/agents/concepts/hosted-agents.md
[S2]: https://github.com/MicrosoftDocs/azure-ai-docs/blob/83c7cb152d2b4fc9008966da1e80aa7fda08412e/articles/foundry/agents/how-to/manage-hosted-sessions.md
[S3]: https://github.com/MicrosoftDocs/azure-ai-docs/blob/83c7cb152d2b4fc9008966da1e80aa7fda08412e/articles/foundry/agents/concepts/hosted-agent-contract.md#L302-L316
[S4]: https://github.com/Azure/azure-rest-api-specs/blob/e31efa5c52e535addb4571e6e5dab2592f818955/specification/ai-foundry/data-plane/Foundry/src/agents/models.tsp
[S5]: https://github.com/Azure/azure-rest-api-specs/blob/e31efa5c52e535addb4571e6e5dab2592f818955/specification/ai-foundry/data-plane/Foundry/openapi3/v1/microsoft-foundry-openapi3.json
[S6]: https://github.com/Azure/azure-rest-api-specs/blob/e31efa5c52e535addb4571e6e5dab2592f818955/specification/ai-foundry/data-plane/Foundry/src/agents-session-files/models.tsp#L17-L47
[S7]: https://github.com/Azure/azure-dev/blob/f8f84e07cbb4cf1bffc9068ba032738f745b28a3/cli/azd/extensions/azure.ai.agents/schemas/Agent.json#L52-L151
[S8]: https://github.com/microsoft-foundry/foundry-samples/blob/1abe346cbb2428fce504970498bafecf3803f29c/samples/python/hosted-agents/bring-your-own/responses/notetaking-agent/src/notetaking-agent-responses-python/note_store.py#L23-L40
[S9]: https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_config.py#L254-L287
[S10]: https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_base.py#L132-L156
[S11]: https://github.com/Azure/azure-dev/blob/f8f84e07cbb4cf1bffc9068ba032738f745b28a3/cli/azd/extensions/azure.ai.agents/internal/cmd/session_carryover.go#L25-L68
[S12]: https://github.com/Azure/azure-rest-api-specs/blob/e31efa5c52e535addb4571e6e5dab2592f818955/specification/ai-foundry/data-plane/Foundry/src/agents-containers/routes.tsp#L13-L24

對應實作的補充定位：

- [azd definition mapping，509–525](https://github.com/Azure/azure-dev/blob/f8f84e07cbb4cf1bffc9068ba032738f745b28a3/cli/azd/extensions/azure.ai.agents/internal/pkg/agents/agent_yaml/map.go#L509-L525)
- [note-taking Dockerfile](https://github.com/microsoft-foundry/foundry-samples/blob/1abe346cbb2428fce504970498bafecf3803f29c/samples/python/hosted-agents/bring-your-own/responses/notetaking-agent/src/notetaking-agent-responses-python/Dockerfile)
- [Invocations 1.1.0 dependency，23–30](https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-invocations/pyproject.toml#L23-L30)
- [Core local provider write，182–188](https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/tasks/_local_provider.py#L182-L188)
- [Core version註冊，249–256](https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_base.py#L249-L256)
- [Python version來源，28–33](https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-core/azure/ai/agentserver/core/_server_version.py#L28-L33)
- [Invocations constructor，296–307](https://github.com/Azure/azure-sdk-for-python/blob/52d921c904a95005c42ace58511ea194aa9d6c01/sdk/agentserver/azure-ai-agentserver-invocations/azure/ai/agentserver/invocations/_invocation.py#L296-L307)
- [azd carry-over執行，207–242](https://github.com/Azure/azure-dev/blob/f8f84e07cbb4cf1bffc9068ba032738f745b28a3/cli/azd/extensions/azure.ai.agents/internal/cmd/session_carryover.go#L207-L242)
- [azd carry-over changelog，172–173](https://github.com/Azure/azure-dev/blob/f8f84e07cbb4cf1bffc9068ba032738f745b28a3/cli/azd/extensions/azure.ai.agents/CHANGELOG.md#L172-L173)

對照的Learn入口：[Hosted overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)、
[Session管理](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)、
[Runtime contract](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-contract)。
