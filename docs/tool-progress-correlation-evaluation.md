# 公開工具事件配對：本機實作與分層結果

日期：2026-09-25。基準：`d5e875d0b9bb73824771dc1b005b03d1c33a05d7`＋本切片source/test WIP。
狀態：**局部實作／本機通過，未部署；不是完整 TURN-02、SP-03 或 TC-01 接受。**

## 契約與接線

原公開projection只留工具名稱。同名工具交錯完成時，人和評測無法知道完成事件屬於哪次呼叫；
原mapper也未拒絕缺ID／重複ID，會把對照覆寫或對不完整結果直接標failed。
本切片沿既有SDK事件，不新增工具迴圈、event bus、model request、資料讀權或交付回條。

| 接點 | 實際改動 |
|---|---|
| `m2_copilot.run_copilot::emit` | 把本次真正註冊的ToolSpec集合交給public mapper；不是信任模型在事件裡自稱工具已配置 |
| `behavior_assistant_progress.public_event` | 僅在記憶體以SDK原call ID對照工具名稱／公開start序號；對外只輸出`tool_start_seq` |
| `PublicProgressEvent`／`PublicProgress` | 新欄位可省略、None不序列化；核start／complete關係、可見起點、順序與重複complete |
| `AssistantSession._public_event` | 沿原8 KiB snapshot／coalescing／final-save路徑，不新增儲存或寫入通道 |
| signed API／`ConversationProjection` | 共用SDK-independent typed DTO，原身份／ticket／work／scope與收件條件不變 |
| `index.html::renderProcessing` | 以`textContent`顯示「工具關聯 #起點序號」；舊無欄位資料照舊，不從raw ID或錯誤型別猜配對 |

`tool_start_seq`是**同一execution的公開progress序列中的起點**，不是全域工具ID、呼叫次數、
工作ID、operation ID或重送key。Start引用自身seq，complete只引用較早start；
有效範圍為1至`2**53-1`，讓JavaScript不會把整數四捨五入成另一個關聯。
它不含原call ID、參數、結果正文、錯誤正文或private reasoning。

已完成ID保留記憶體標記；重複／重用ID不建立新配對。第二個start讓原關聯不再可信時，後續complete
不硬接任一個start。未配置工具使用同樣的ID也不能重新綁定既有／未來呼叫。
缺ID／非字串ID、錯誤data shape、非bool success只記固定診斷碼，不記原值；
非bool completion不當作failed，也不吃掉之後合法complete的關聯。
SDK只宣告call ID為string，沒有套用任意256字元上限；長ID照精確原字串比對，但不公開。

Registry未提供時預設為空，不假設固定工具可用。已註冊`read_skill`也能顯示開始／完成名稱；正文仍不讀入公開projection，
`read_skill`原權限／schema拒絕文字的契約不變。開始文案為「Configured tool call started」，
只證明配置中的工具開始，不冒稱業務操作已獲批准或產生效果。

公開snapshot仍有8 KiB上限，舊start可能已被省略；complete可保留指向該起點的序號，
但不能因此宣稱完整event replay。若起點還在snapshot，必須是同名工具的start，不能指向stage、
另一工具或另一個complete。Cold resume／下一個execution不沿舊記憶體map猜關聯；
原生SDK session可相同，但FA assignment session依原start request產生，公開關聯不可跨execution拼接。

## 相容性與採用

舊event／archive沒有此欄位，typed reader仍接受，而且序列化不增加null欄位；
既有原件、批准、digest及回條不重寫。此欄位只供呈現，不開啟`can_follow_up`、ACK或任意操作。
新code先更新API／bridge／console readers，再讓新的Hosted producer生效；
舊strict reader不認識新欄位時仍會拒絕，不能靠忽略未知欄位或匿名fallback掩蓋版本差異。
實際部署仍需原image／installed-code／session-version接線，本次沒有部署。

本切片改動公開producer／DTO／UI consumer，因此本工作樹的TC-01回到partial等待新完整資格；
舊`ed6f64e`的接受run、#27／#28回條原樣保留，不把這次subset借成新的TC-01 full acceptance。
SP-03也只標partial，沒有人的語意標籤。Main30加入第七個memory prefix時PA-02曾先partial；
後續main32已有完整七profile資格，本線不改prompt，沿其PA-02資格。原接受run與TC-11不改寫。

## 執行與結果

| 批次 | 結果 | 證據界線 |
|---|---|---|
| 初版測試 | 9 pass／11 fail | 缺欄位／mapper等反例；其中「超過256字元ID應拒絕」是後來依SDK契約撤回的測試假設，不當產品缺陷 |
| 初版局部修正 | 23 pass | 當時source/test WIP，後續又加入型別、emitter、保存與截短案例 |
| completion shape反例 | 4 fail | `None`／字串／數字／object被錯當failed且吃掉關聯；另行修正 |
| 第一個較完整合批 | 40 pass，7.97s | 仍含早期長ID假設；與最後版本分開 |
| **最終Python合批** | **40 pass／0 fail／0 skip，7.47s** | 實際mapper／DTO、SDK emitter的I/O doubles、signed API、保存archive與thin-client；無真SDK runtime／模型 |
| Minimal-DOM suite | 33 pass | 1個新配對案例＋32個既有案例；不是33段agent trace |
| 真瀏覽器唯讀呈現 | 交錯結果顯示`#1/#2/#2/#1`，非法ref不顯示 | 在8937隔離preview對真renderer注入合成資料；兩次`GET /api/state`，無POST、backend／模型或登入 |
| Native前提 | 1 setup error | worktree未配置固定runtime，當時尚未啟動SDK；不是產品FAIL或新的模型請求 |
| 固定runtime控制案 | 1 pass，2.83s | 真SDK／runtime的既有custom file-tool控制；不當作新配對案例 |
| 新native配對單案 | 1 pass，3.60s | 真SDK／runtime、2次scripted provider請求、2個工具開始／完成，source參照及graceful stop可核 |
| Native配對＋skill回歸 | 4 pass，32.11s | 新單案加原current-catalog接續及拒絕兩案；與上一列重疊，不加成5個獨立樣本 |

最終Python命令：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:COPILOT_SKIP_CLI_DOWNLOAD = '1'
.\.venv\Scripts\python.exe -X utf8 -m pytest `
  .\tests\shared_context\test_tool_progress_correlation.py `
  .\tests\shared_context\test_instruction_progress.py `
  .\tests\shared_context\test_instruction_completion.py `
  .\tests\shared_context\test_free_instruction.py::test_public_progress_is_bound_and_pure_to_read `
  .\tests\shared_context\test_instruction_flow.py::test_completion_notification_requires_original_saved_session_and_ticket `
  -q -p no:cacheprovider --tb=short
node --test .\tests\shared_context\test_web_test_console_workbench.cjs
```

### 固定runtime供應鏈與新增原生證據

SDK正式cache resolver未找到完整1.0.83 bundle，實際native控制測試也因缺件停止。
之後只透過既有核准npm feed安裝`@github/copilot-win32-x64@1.0.83`到ignored `.venv`，
`--ignore-scripts`、不修改package manifests或全域工具；沒有改走其他registry或直接下載繞過政策。
精確平台包發布時間為`2026-09-04T15:46:36Z`，查核時20完整日，符合7日政策。
原時間字串另以Python保留offset重算，不採PowerShell格式化後的21日顯示。

真SDK案例使用`mode="empty"`、`use_logged_in_user=False`、固定runtime及離線HTTP transport。
模型只收到scripted回應；SDK的tool loop、permission、event、source解析及owned stop是真執行。
它支持新配對接點的限定可行性，不是真模型選工具／摘要品質或Hosted驗收，
也不補齊所有profiles／compaction／公開view的完整TC-01資格。

```powershell
$env:M2_TEST_RUNTIME = Join-Path (Get-Location) `
  '.venv\copilot-native-validation\node_modules\@github\copilot-win32-x64\prebuilds\win32-x64\copilot-runtime.exe'
.\.venv\Scripts\python.exe -X utf8 -m pytest `
  .\tests\shared_context\test_tool_progress_correlation_native.py `
  .\tests\shared_context\test_approved_skill_native.py::test_fixed_sdk_new_turns_replace_only_current_catalog_and_preserve_historical_bodies `
  .\tests\shared_context\test_approved_skill_native.py::test_fixed_sdk_custom_tool_failure_never_discloses_body_or_enables_native_loading `
  -q -p no:cacheprovider --tb=short
```

先前已在未修改base重現的兩個progress fixture缺`assignment`，本切片為了直接驗coalescing／
final-save，在測試替身補入真constructor所需的`assignment.task.instruction=None`。
原產品constructor／start-binding gate不放寬，原兩個fail的歷史不回填。

Browser初次裸GET因沒有頁面cookie被拒；正常開啟preview後才有合法GET。
探針第一次假設Playwright server有全域`URL`而失敗，之後改成只用page API；
snapshot先寫入工具允許的workspace路徑，再由檔案工具保存至session artifacts。
沒有改server的cookie／origin防護，也沒有用高權限browser程式寫任意磁碟。
新開的測試tab已關閉、自己啟動的preview process已停止；不是留著運行的review UI。

## Main30 整合與修後限定驗證

`5823fd8`在base `d5e875d`上的完整Opus5.5 Standards／Spec均為0P1/P2。
審查期間main30先合併，本線以純merge `748bb6590724e997b741806184bcbc6deb51e0ce`
納入`cfd9b7d6639adad24776668916a4cc56f18eee69`，保留兩線README歷史，以及
`read_skill`／`read_work_memory`的原authority-refusal分類，沒有重做memory reader。

兩個非阻擋建議也一併處理：mapper的registry改為缺省空集合，測試明確傳其已配置集合；
UI標籤改成「工具關聯」，避免把公開start seq誤讀成第幾次呼叫。
已註冊`read_work_memory`現在自然有配對進度，但只含工具名稱／狀態／序號；
claim alias、claim正文、必要原件及raw call ID不進public progress，`source`保持None。

| 整合後批次 | 結果 | 範圍 |
|---|---|---|
| Python callbacks／API／舊reader | 68 pass，13.30s | 新registry default、工具配對、public reader與main30的skill／memory拒絕契約 |
| 真SDK／離線provider | 5 pass，47.87s | 配對＋原skill三案＋memory native；新增斷言核已保存memory進度有合法配對且不含正文／raw IDs |
| Minimal-DOM | 33 pass | 新中性標籤；不是再次browser執行，先前browser原件保留舊文案 |

此批是`748bb65`＋source/test WIP，snapshot
`accbf394537216acb4c086b6c360426ae91183ede2a8d5ed118c60f1d8da7ebd`；
八個delta檔案hash保存在manifest，其他程式由該merge base固定。先前40／33／4與本批不相加。
Ledger追加三筆至299、舊296不變；當前PA-02／TC-01／SP-03只partial，
不借先前六prefix或目前一個memory native案例宣稱完整七prefix／public-view資格。
修後final head仍需同兩軸重新核對，不能拿`5823fd8`的clear放行新的head。

| 整合原件 | SHA-256 |
|---|---|
| `public-pairs-main30-source-20260925.json` | `838ee26b9239eeb9b7c2eaf942c5df1c6b72f9d8772b43592d412d12706534cc` |
| `public-pairs-memory-integration-python-20260925.xml` | `66e6e518c2badf7f5d18832b83afeaa8f4546ddff54dc33a5efcc39b1736df8f` |
| `public-pairs-memory-integration-native-20260925.xml` | `bcd6fd58cc9b1531d01eb558d3504afdab8f77c8c940e79e1b24b421599a38ce` |
| `public-pairs-memory-integration-dom-20260925.xml` | `b37967cd1f8b14c792f5d38e0910980e6edeb047769787a75ed20927bb14ee06` |

## Main32 資料整合與資格沿革

`6905954`對`cfd9b7d`的完整Opus5.5 Standards／Spec均為0P1/P2；審查期間main32又合併。
主線`7b819cc`新增的是測試／文件與318-run資料，沒有產品source變更。本線取main318為精確前綴，
再用既有`append_run`接上本線14筆，成為332；14筆payload／hash／spec／entry hash不改，
只有未合併分支上的sequence從285起移到318起。原分支299前件、main318前件及對照表各自保留，
不宣稱兩條分支陣列都同時是新帳冊前綴。

已唯讀核#32的公開最終review回條：fixed head`1c3db9117f6469457bfefb24dc89e42f8c885ca6`
兩軸0P1/P2，tested source是`cb4b1805c9622817453f6711e23c7df0c82a034c`，
完整七profile矩陣為161 Python＋32 DOM。這不是本線的新執行。
本PR不改prompt／catalog prefix，因此PA-02保留該current資格，不沿用main30當時的缺資格理由。
TC-01則仍partial：本線公開producer／DTO／UI更新晚於`cb4b180`，不可借其view資格。
SP-03亦partial；目前PA-02／TC-11為2／76接受，scenario0／12、17partial、56待盤點。

純整合commit為`60cd87e440c1d068516b3ea685b380ba32e8734d`，產品source與已審`6905954`
相同；新main只有test／data／docs。其後在乾淨此commit實跑main32新增的
`reviewed-work-memory`／`reviewed-work-memory-checkpoint`兩案，**2 passed／0 failed／0 skipped，42.43s**，
真固定SDK＋離線provider的普通與compaction/cold-restore資料都能進strict公開DTO。
這只是新整合路徑的回歸，不代替完整TC-01；另加一筆後帳冊為333。
JUnit `public-pairs-main32-memory-prefix-20260925.xml` SHA-256：
`894479327bd6f15b3fe6b32d01b0f7974aa04b1387df3cc30aab5c6c3481cceb`。

## Verified against

**後續資格：** 配對切片已經兩軸0P1/P2並合併為`0dac615`。其後在該乾淨commit
完成[TC-01的109-check新重驗](tc01-public-pairs-requalification.md)；本頁各批舊結果保持原樣，
新的C候選與兩軸確認另記，不回填前面subset。

- 真實來源：上述四個產品接點、`behavior_instruction_progress.progress_record`、
  `behavior_instruction.save_reply`及`AssistantAuthority.issue`（execution SID綁原start request）。
- 官方型別：已讀本機固定`github-copilot-sdk==1.0.13`的
  `ToolExecutionStartData`／`ToolExecutionCompleteData`；都用`tool_call_id`，complete要求bool success。
  SDK/runtime pin不改；不是由文件推定新native執行已成功。
- 最終受測WIP快照：`snapshot:c191d28e71fa9f0bd7970e1643cfcb3668004403a96e91d988b5ac274e4fd073`；
  base為`d5e875d`，manifest記錄四個產品檔與三個測試檔working-byte hashes。
  Python3.12.10／pytest9.0.2；Node v24.19.0。瀏覽器只驗renderer，不是產品E2E。
- 後加native test SHA-256：`711985d5d7283f8ea40b35001152df3f3b219b1b138a52a37fd03cf296a6bf15`；
  runtime EXE／`runtime.node` hashes及NPM來源另在toolchain原件。產品檔仍與上面WIP snapshot相同，
  不是新commit上的測試或完整原生套件PASS。
- 新原件如下；原始輸出留本session，Git只放去敏摘要。原件hash與規格／run fingerprint不同，不互代。

| 原件 | SHA-256 |
|---|---|
| `tool-progress-correlation-red-20260925.xml` | `4b24014fa01b4dcdbed466a75bd468776f5650392da8930fe50856a3a1d643e4` |
| `tool-progress-correlation-green-20260925.xml` | `0071eeab6e2b127fbb77ecb71b04d26b067ea4193eb1709d8d4aeaee1f447b36` |
| `tool-progress-completion-shape-red-20260925.xml` | `5e15d2360a32f3995a535348508acf849ec34f1602673c4a67d75940b872159a` |
| `tool-progress-correlation-targeted-20260925.xml` | `53c5a9ad85d5ef796c006a696025af4a165d8863a935f6c1af32f3f7f7cd55fd` |
| `tool-progress-correlation-final-20260925.xml` | `7ba44b33b9b0df49305e1bfa69d7dd1b70cb0640e1d3bec573bc0627d1c73bbd` |
| `tool-progress-correlation-dom-20260925.xml` | `32413415be5aeceb27b4c2df6426ac8f31a5e2be34a91c25edfd741126c0bc85` |
| `tool-progress-final-source-snapshot-20260925.json` | `7d6d5ea40340e1dfcea279d3aa8a946c143598449ba30acc83ab0af838e3f2d1` |
| `tool-progress-correlation-visible-20260925.yml` | `779db1c93bb47bde6374a9fd48a8e0e31954e2e271a148cf4af41204dca2f7d2` |
| `tool-progress-correlation-network-20260925.txt` | `91469bb8eae838bcce10bc2e621d00e73f30a0c096a62cdb2075380e07a03329` |
| `runtime-provision-prerequisite-20260925.xml` | `9dc82630ec60e74c0a525ab80d8b091d985d9ebd24456465777fba562b2c1e3b` |
| `runtime-provision-native-control-20260925.xml` | `4c3661c53460417e05d23c19abe239a01daf9a863f210d2263f9f020176376fa` |
| `tool-progress-native-20260925.xml` | `956dec96f75862d6b3d35aedf0833e2017a38df93f249c2c92d0e600207ff0e6` |
| `tool-progress-native-skill-regression-20260925.xml` | `9fe9bb9fb77b00c6d9444280c1b1814ae7255986c68e2b0df0ce6173b5634e2a` |
| `tool-progress-native-toolchain-20260925.json` | `a0edb03d94e69da508f91bfa61262a839d924c8bba3f08fd5eb4e239357e0048` |
