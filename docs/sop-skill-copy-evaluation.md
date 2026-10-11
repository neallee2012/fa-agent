# 受控SOP逐byte複製為skill草稿

日期：2026-09-26。#42 final`837d109`兩軸P1/P2全解、完成指定consumer檢查後，
正常合併為`fe482b14d7ce848128e061665578d37b9ac921b2`。
這只是SK-10的有界前置，不是自由Skillify、私人demo擷取、配套測項生成或M品質接受。

## 能力與權利

`fa-instruction-sop-copy-v1`預設OFF。原verified human的StandingPolicyInstallation
必須批准完整server profile中的`one-approved-sop-byte-copy-per-work`範圍，再綁原
registration/task/profile digest。來源、Team、target slot、version及literal purpose由server決定；
`copy_sop_to_skill_draft`只接受`{}`，不能由模型選body、身份、目的地或approval。

原Actor必須仍有Team read/write及owned legacy source work；Runtime work ID不是legacy work，
不建立假來源、不cast身份、不新增grant/IAM。SOP內容批准與`authorize_own_result`都不是寫入授權。
只有已批准且目前有權讀取的Team文字SOP能逐byte copy，不能截斷、改寫或加入私人user內容。
Team草稿可被合法同隊讀取，**未批准不等於private**；printable不是秘密掃描器。

每個work一個server-derived slot，不是整個installation lifetime只能一份。原未知work不換ID重送。
Create／upload沿原M3服務與原artifact effect ledger，先保存intent/stage；
create ACK不明只GET原ID核對，upload未知只stat/readback/reconcile原version。
缺Blob或欠目前對帳權仍unknown，不第二PUT、不假跨服務atomic／任意exactly-once。
新target未批准／未生效；human還要另走原version、bodyhash、metadatahash、ETag decision，
模型不能自批，也不更改既有approved內容或catalog。

## 初審發現與保存契約修補

初稿bee的Standards為1P1/1P2、Spec為1P1/0P2，P1同根：
把新的copy執行許可檢查放在保存前，取消／來源改版／欠新的M3寫權時連failed結果也存不下。
另以target「仍未批准且僅一版」驗原完成receipt，會把正常後續人審誤當原copy失效。

修後`save_text_reply`沿原current read/save契約，在原交易內讀ledger，不呼叫M3或重新copy：

| 原stage | 保存的事實 |
|---|---|
| 沒有intent | 沒有copy效果 |
| `create_unknown` | 原ID未決，不能假已建立 |
| `created`／`upload_unknown` | metadata create已知，copy仍未決 |
| `saved` | 原copy已確認 |

Success不能隱藏unknown；failed/cancelled保留known與unresolved。
同conversation不以新turn繞過原unknown，整體read/report也撤銷時仍blocked。
`fa-sop-copy-outcome-v1`串接archive/history/result/strictbridge；公開turn只有有界outcome，
完整`SopCopyReceipt`留在工具與archive，不為測試方便增加公開欄位。

Receipt必填`state_scope="at-original-copy-write"`；其中false/null是原寫入的歷史狀態，
不是當前target人審狀態。後續approve/reject或新版本不推翻原不可變bytes/metadata/Blob/lineage，
也不授予另一次copy。新的create/upload仍須目前的完整權限／來源批准／空slot。

## 分批來源與限制

| 批次 | 來源／結果 |
|---|---|
| 原beta 12批 | 10批WIP（含profile RED1、ticket404 RED1、consumer RED2等）；`ab9031b`先commit後67 API/wire PASS及fixedSDK1 PASS，1runtime/3offline requests |
| 保存修補11批 | cancel RED2、unknown-result RED2、target-lifecycle RED3、fixture422等都保留；`4f65916`先commit後39 PASS/2 FAIL及native取消1 PASS |
| Safe錯碼補線 | `12ba700`只safe-error表一行及兩assertions，受影響2 PASS；不把4f的39/2或native重標成此commit |
| 純Git整合 | `837d109`與main089合併，產品/controller等c1；incoming兩PA07 tests只新增case，不重跑原23批 |
| Spec指定consumer | parent在immutable837 archive真跑7 API/bridge cases，7 PASS/0 FAIL/error/skip，10.850秒；SDK I/O替身，不是第八profile全資格 |

真取消控制是SDK1.0.13/runtime1.0.83、business mode `offline_contract`：
copy後signed SOP新版及signed cancel，1runtime/2scripted requests、create1/upload1、official abort1；
Windows exit1／graceful／force0，cancelled結果保留knowncopy1/unresolved0。
ArchiveUnavailable(cancelled)如實保留，**不是可攜checkpoint或coldrestore證據**。
正常beta與取消控制是不同來源／批次，不能相加為品質樣本或外推Azure/native業務模式。

## 完整目錄穩定性的後續P1

初始68f資格測試跑238 Python＋33 DOM全過，但copy工具描述含每work不同的target IDs。
當時測試只比較去掉兩個ID欄位後的模板；Spec依Target State §5.1／PA-02判為不符完整
能力目錄穩定，Standards亦要求不能過度宣稱。原271個PASS保持執行事實，**不是PA-02 C接受**。

更嚴格的原路徑在第二work重現catalog hash變動；source-only system prefix仍為2645B／8f6151。
最小產品修補只從工具描述移除`target_artifact_id`／`target_version_id`：
原immutable task與receipt仍綁定IDs，空參數工具無需模型事先知道它們。
新檢查回到完整未正規化catalog跨work比較，不再忽略欄位或只驗template；
新固定c8e已[完整重跑271項](prompt-assembly-evaluation.md#修後完整canonical目錄與同源資格)，
完整canonical catalog跨work確實相同；ef7文件／資料經原兩軸各0P1/P2且明確建議後，
才在實測c8e恢復PA-02／TC-01 C，不用文件head取代實測baseline。
這不是provider cache-hit或效能量測。

## Verified against

- Product final：`837d109c335e7ea34c82c322a8e1f37cc264f529`／tree
  `63b443e4f6c4285081d405968339753566d88d35`，merge`fe482b1`。
  Authoring新寫入與歷史保存分別沿`behavior_skill_copy_source`、`behavior_instruction`及原M3／control guard。
- Beta handoff `2cd4b78c91a9315f2b31204b4e48a03a57766184cddea40143e1ff2ca00f953b`，
  index `0ebe1f7175d510881271ad4de83507f0f6e5cea373fe9c0c8e3b5d9af2490ab8`。
  Fix handoff `98461cd54ff94be039cd271dfda6887d63d6c79d1ed5b0099198f6a175ebd4fb`，
  index `b0e853142261191f5e9075012af9312210ca89e8b64f48382c5d6cf56d9dcf83`。
- Consumer7 JUnit `c7c56e11ab86a934878be67e15a57f5df9418dbdb49c2a53b990dad52fc6f3cd`，
  source archive `1e328a7e7c30ae1f214b03b9bac37ff2719cf005e7374ab7a215ce6ee8d3b46c`。
  原三個受測commit、early WIP、P1/P2與後續目錄缺口分開，不回填失敗／缺時點的原回條。
- No cloud/paid/IAM/build/SDK/harness更新，default-OFF未部署。完整SK-08／SK-10、私人記憶、
  模型語意品質與未知成本／真Hosted能力仍未接受。
