# Azure Files 共享資料夾接受哪些登入方式？

**現行決定：** 2026-09-13 20:02 +08，使用者已採用 [共享 context 方案](adr/0002-shared-context-without-file-share.md) 取代 File Share。本文保留為歷史認證研究與失敗紀錄，不再建議為本案繼續調整 SMB；這不否定 Azure Files 在其他支援環境的用途。

**原研究結論：真正的 SMB 共享掛載，除了帳戶金鑰與目錄帳號，現在也有正式的 Managed Identity／SMB OAuth 路線。但官方尚未在該 Linux 支援清單中列出目前使用的 Debian 12／Foundry Hosted，不能直接宣稱可用。** [來源：MI 主文件、2026 Q2 GA 公告][mi] [最新公告][new]

查證日期：**2026-09-13**。依當日 Microsoft 官方文件整理；起初是管理面唯讀查詢及官方套件檢查，後續經使用者批准才設定 SMB OAuth／限定角色並執行 Hosted 測試。最新結果見第 3 節與 `POC-RESULTS.md`。

## 1. 真正 SMB 掛載：主要有五條登入路線

| 登入方式 | 白話說明 | Linux 前提與本案限制 |
| --- | --- | --- |
| **Storage account key／NTLMv2** | 用儲存帳戶的管理鑰匙登入，不是使用個別使用者身分 | 官方有 Debian／Linux 掛載流程，不需加入網域。本 POC 早期使用這條路徑，在 SMB session setup 被拒絕。當時 MI 只負責從 Key Vault 取 key，不是直接登入 SMB。[Linux 掛載][linux-key]、[SMB 設定][smb] |
| **AD DS Kerberos** | 用公司的傳統 AD 網域帳號登入 | 官方 Linux 教學採加入 AD 網域，需可達的 DC／KDC、網域 DNS、時間同步與 Kerberos 設定。本案尚未確認這些條件，不能直接套用。[Linux AD DS 教學][linux-ad] |
| **Microsoft Entra Domain Services Kerberos** | 用 Azure 上受管理的 AD 網域帳號登入 | 官方支援 Linux，但需先有 managed domain、密碼雜湊同步、網域 DNS／KDC 網路及文件中的網域整合。不是「有 Entra tenant 就已經有這個服務」。[Domain Services][domain]、[Linux 教學][linux-domain] |
| **Microsoft Entra Kerberos：使用者登入** | 使用 Entra 的 hybrid 或 cloud-only 使用者身分 | 官方目前明文不支援 Linux，不能把 Windows／cloud-only GA 當成 Linux 也支援。不是目前 Linux Hosted 的直接方案。[Linux 限制][linux-domain]、[使用者登入前提][entra] |
| **Managed Identity／SMB OAuth：應用程式登入** | 讓 Azure 工作負載用自己的雲端身分登入，不必保存 storage account key | **2026 Q2 已 GA**；Storage 需啟用 `SMBOAuth`，Linux 使用 `azfilesauth`、credential cache 和真正的 CIFS mount。官方列 Azure Linux 3.0、Ubuntu 22.04／24.04、RHEL 9.6+、SLES 15 SP6+，未列 Debian 12／Foundry Hosted。本案已取得票證，但共享掛載尚未通過。[MI 主文件][mi]、[GA 公告][new] |

### 幾個容易混淆的地方

- **三個目錄身分來源只能三選一：** AD DS、Entra Domain Services、Entra Kerberos 不能在同一 storage account 同時啟用。新 MI 主文件則允許應用程式 MI 與使用者身分存取共存在同一 account，兩個規則不能混用。[身分來源總覽][identity]、[MI 主文件][mi]
- **Entra Kerberos 使用者登入，不等於 SMB OAuth／MI。** 新 MI Linux 流程雖使用 `sec=krb5`，卻有 `azfilesauth` 的不同登入流程；不能因前者不支援 Linux，就否定後者。[Linux 限制][linux-domain]、[MI 主文件][mi]
- **AD 服務帳戶、Entra service principal、Managed Identity 不是同一種身分。** 支援其中之一不能自動推論另外兩者可用。[Share 權限][share]、[MI 主文件][mi]
- **Domain join 並非所有 AD 情境的絕對要求。** 一般 AD／Domain Services 主文也有未加入網域但可直接連通 DC、提供明確憑證的情境；正式 Linux 教學採 domain join，不能把一般例外當作 Hosted 已有支援流程。[AD DS 總覽][ad]、[Domain Services][domain]、[Linux 教學][linux-ad]

## 2. 登入和檔案權限是兩件事

一般目錄身分的 SMB 存取有兩道門：

1. **Share-level RBAC：** 決定能否存取 share 及權限上限，例如 `Storage File Data SMB Share Reader`／`Contributor`／`Elevated Contributor`。
2. **Directory／file ACL：** 決定 root、子目錄和檔案實際允許哪些操作。普通身分必須同時符合兩層，有 share RBAC 不等於所有檔案都可讀寫。[授權總覽][authz]、[ACL 文件][acl]

Account key 是管理員級憑證，使用它掛載可完整存取檔案／目錄；不會依「讀 Key Vault 的 MI」套用個別 SMB 權限。新的 MI 文件則要求 **`Storage File Data SMB MI Admin`**，也是管理員級角色，不能假設現有 `SMB Share Contributor` 就足夠，更不能未經批准直接新增。[Account key 權限][acl]、[MI 文件][mi]、[MI Admin 角色][mi-role]

## 3. MI／SMB OAuth 對目前 POC 的意義

這條路線**有官方支援文件，而且不是 FileREST 替代方案：最後仍是真正 SMB 掛載**。但還有幾項未確認：

| 待確認事項 | 為什麼重要 |
| --- | --- |
| Storage 的獨立 `SMBOAuth` 狀態 | 已知的 `azureFilesIdentityBasedAuthentication=null` 不能代表 `SMBOAuth` 未啟用。官方允許在既有 account 啟用，不一定要新建 account。[mi] |
| Debian 12／Foundry Hosted client 相容性 | MI Linux 主文未列本案環境。需確認 `azfilesauth`、CIFS upcall、credential cache 與 Hosted 生命周期，不是只看 root／kernel。[mi] |
| Token 取得方式 | VM MI 模式用 IMDS；Linux 另支援自行提供 OAuth token。不能把 Hosted 當一般 VM，也不能僅因不是一般 VM 就直接否定。[mi] |
| Token audience 與續期 | 自帶 token 的 `aud` 要求為 `https://storage.azure.com`，尾端沒有 `/`；該模式必須自行更新 token。一次登入不代表休眠／恢復後仍能使用。[mi] |
| 專用管理員級角色與政策批准 | 須確認 `Storage File Data SMB MI Admin` 的 scope 與風險，及啟用 `SMBOAuth` 是否合規。這不等於開放 Shared Key，但仍是設定／權限變更。[mi]、[mi-role] |

### 已完成的核對與批准後變更

| 項目 | 實際查到的內容 |
| --- | --- |
| Storage 設定 | 已透過 `--enable-smb-oauth true` 啟用。ARM `2026-06-01` 讀回 `properties.azureFilesIdentityBasedAuthentication.smbOAuthSettings.isSmbOAuthEnabled=true`。先前把 CLI 參數名稱誤當 ARM 欄位，造成錯誤警報；不是服務未儲存設定。`allowSharedKeyAccess=false`、`publicNetworkAccess=Disabled` 與加密不變。 |
| 帳戶範圍 | 專用帳戶 `stfaexampledemo` 目前只有 `team-poc`，SMB、quota 5 GiB。 |
| 專用角色 | `Storage File Data SMB MI Admin`，ID `a235d3ee-5935-4cfb-8cc5-a3303ad5995e`，已依批准只在專用 Storage account 指派既有 Hosted runtime。包含讀寫、刪除、變更 ACL、取得擁有權等管理員級資料權限；沒有授予 controller 或擴大 scope。 |
| Linux 套件 | Microsoft Ubuntu 22.04 feed 的 `azfilesauth 1.0-12` 已核對 SHA256，並在 ACR Linux build 安裝、實際載入 library 1.0.12，通過 null log sink 等檢查。Debian 12 feed 沒有該套件，這次實驗成功不等於官方支援 Debian／Hosted。 |
| 不用 systemd 的整合方式 | 官方提供 `libazfilesauth.so` 的 `extern_smb_set_credential_oauth_token()` 等 API，可由應用程式提供 token 並自行續期；因此不必假設 Hosted 有 VM IMDS 或必須啟動 systemd。真正的 CIFS upcall／credential cache 相容性仍未驗證。[mi]、[helper] |

原始核對與變更輸出保留於 `evidence\mi-smb-20260913\`。實驗沿用現有 Debian／桌面映像，只增加 MI helper 與登入流程，先驗相容性，再啟用該 Storage 的 SMB OAuth，依官方步驟在**這個專用 Storage account** 指派 runtime 的 MI Admin 角色。這是實驗性 Debian／Hosted 組合；不需要新建 AD、Domain Services 或換成 REST。

使用者已明確批准上述設定與角色。首個 build `dsa` 因錯誤的 upcall 版本旗標失敗，
後續 `dsb`、`dsc` 分別修正套件 Python 路徑與 build 版本探針問題，`dsd` 成功。
**先前三次 build 額度已用完，之後使用者另批准最多 100 輪必要測試／建置。Storage 設定與限定角色已讀回；已完成六輪，最新 Hosted v7 仍未掛載成功。**
沒有開放 Shared Key 或公開網路，不能以 build 成功宣稱 G0 通過。

v4 的歷史測試與最新 v6／v7 都曾由 MI helper 取得票證，產生 owner10002／mode0600 的 cache，
但真正 mount 回傳 exit32／`mount_errno=2`。v7 額外證明：隔離匿名 keyring 的本機 dummy key 建立成功，
要求 kernel 喚起 helper 的 `request_key()` 則同樣回 ENOENT；container 內 helper／設定存在，
CIFS upcall wrapper 卻沒有 entry／exit 紀錄。問題已縮小到 kernel→user helper 呼叫環境，
不是單純沒有 root 或少加 Azure Files RBAC；仍需 Foundry 確認具體平台設定，不能外推所有 Hosted 都不支援。
依據：[Linux Key Request Service](https://docs.kernel.org/security/keys/request-key.html)、
[上游 keyutils 設定手冊](https://man7.org/linux/man-pages/man5/request-key.conf.5.html)。
六個診斷 session 已重新讀回 idle，憑證內容未保存。

### CIFS client 的必要修補

後續查到的具體相容性問題是：**未修補的 `cifs.upcall 7.0` 會要求 cache 裡有 TGT，即使已有有效 CIFS service ticket 也會拒絕。**
Azure helper 的公開實作則交換並存放 service ticket，不能要求它另外提供傳統網域 TGT。
上游修補 [`af76bf2a11a060afdfd97104617a701d19d5890d`](https://github.com/smfrench/smb3-utils/commit/af76bf2a11a060afdfd97104617a701d19d5890d)
在 7.2 首次提供「先接受有效 service ticket」的行為。
7.4 另包含舊 kernel／namespace 的 [`f4fd27cf60d6431d83ea18b4962aef845f9312bd`](https://github.com/smfrench/smb3-utils/commit/f4fd27cf60d6431d83ea18b4962aef845f9312bd) 修補。

成功的 `dsd` build 已編譯固定
[`7.4／edac7178bec9520fb57d14946e67f5dd33b82d43`](https://github.com/smfrench/smb3-utils/tree/edac7178bec9520fb57d14946e67f5dd33b82d43)
的 `cifs.upcall`，放在 `/usr/local/sbin/cifs.upcall`；保留原本 `mount.cifs`、OS、kernel 和 browser，
不追浮動 `for-next`，也沒有更換 Hosted kernel。
build 讀回原 cifs-utils 套件為 `2:7.0-2`；未逐項稽核 vendor backport，不能只憑「7.0」概括所有 vendor binary。
**排除這個 userspace 障礙，仍不代表 Hosted kernel 已能執行 upcall 或 mount；G0 必須真正驗證。**

## 4. FileREST：能操作檔案，但不等於掛載

FileREST 使用 HTTPS API／SDK 存取 Azure Files。它不會自動讓一般程式的本機 `open()` 路徑變成共享檔案系統，所以 REST 成功不是 SMB mount 證據。[FileREST 介面][rest-api]

| 憑證 | 官方說明 |
| --- | --- |
| Shared Key | 以 account key 簽署請求；禁止 Shared Key 時會受限。[shared-key] |
| Service SAS／Account SAS | 以 account key 簽署，在 `allowSharedKeyAccess=false` 時不是免 key 政策的替代品。[sas]、[shared-key] |
| Entra OAuth／MI／service principal | 明文支援。資料操作要求 API `2022-11-02+`；FileREST 的 FileService／FileShare 管理 API 要求 `2024-11-04+`。引用文件適用 `Microsoft.Storage` classic shares。[rest] |
| User delegation SAS | 新 SAS 主文與 REST 規格已列 Azure Files，但部分較舊總覽仍寫不支援／Blob-only；文件有落差，需按具體 API 版本及環境確認，不宣稱本案已驗證。[sas]、[uds]、[data-auth]、[shared-key] |

FileREST OAuth 另有 backup-semantics／`x-ms-file-intent: backup` 要求；典型角色為 `Storage File Data Privileged Reader`／`Privileged Contributor`，可越過檔案 ACL，不能與一般 SMB 角色混用。[rest]

**SAS 是 HTTP(S) 簽名權杖，不能當成 SMB mount 的密碼。** MI 的 SMB OAuth 流程也不是把 SAS 放進 `mount.cifs`。[sas]、[smb]、[mi]

## 5. NFS：另一種共享協定，不是另一種 SMB 登入

Azure Files NFSv4.1 的 Linux 掛載範例使用 `sec=sys`，以受信任網路、UID／GID、POSIX 權限及 root squash 等機制控制存取；不支援 NFS Kerberos 或 NFS ACL。這不是「用 MI 登入 NFS」。[NFS 主文][nfs]、[Linux mount][nfs-mount]

NFS 目前需要 SSD share，同一 share 不能同時提供 SMB 和 NFS。現有 Standard_LRS SMB share 不能只改 mount 參數就變成 NFS，涉及新 share／協定／SKU 與安全設計，超出目前 SMB POC 範圍。NFS 已有 AZNFS／TLS 傳輸加密支援，不應為改測 NFS 而關閉加密。[nfs]、[NFS TLS][nfs-tls]

## 6. 目前的拒絕原因仍須保留界線

本案既有結果：Hosted Linux 有 root／`CAP_SYS_ADMIN`，私有 Files DNS／TCP 445 可達，但使用 account key 的 mount 在 session setup 收到：

```text
0xc0000022 STATUS_ACCESS_DENIED
Send error in SessSetup = -13
```

已知 `allowSharedKeyAccess=false`；key 格式有效，但未確認與 Storage 現行 key 相同。**目前沒有成功 mount。**

官方 Linux error13 的 Cause4 確實把「storage account key access 被禁用或由 policy 禁止」列為可能原因，不能排除；SMB 的 `authenticationMethods` 移除 NTLMv2 又是另一個明文會禁止 account-key mount 的設定。已知欄位為 null，不能解釋成已移除 NTLMv2。[error13]、[smb]

因此，**政策／key access 是有官方依據的疑點，不是已證明唯一根因**。Shared Key 主文主要使用 HTTP／SAS／REST 的敘述，也不能只憑相似名詞就混同協定；本次不依舊摘要或 Q&A 宣稱 SMB 一定不受影響。[shared-key]、[error13]

文件研究後已完成成功映像與批准的 Storage／RBAC 變更，最新 Hosted v7 G0 卡在 kernel key-callout／mount errno2；
沒有更改網路或政策。固定 VM 仍待驗證，也未以 Docker、控制 VM、Storage REST 或 session-private `$HOME` 持久化代替 Hosted 共享掛載證據。

## 官方來源

[mi]: https://learn.microsoft.com/azure/storage/files/files-managed-identities
[helper]: https://github.com/Azure/AzFilesAuthenticator
[new]: https://learn.microsoft.com/azure/storage/files/files-whats-new#2026-quarter-2-april-may-june
[linux-key]: https://learn.microsoft.com/azure/storage/files/storage-how-to-use-files-linux
[smb]: https://learn.microsoft.com/azure/storage/files/files-smb-protocol
[identity]: https://learn.microsoft.com/azure/storage/files/storage-files-active-directory-overview
[linux-ad]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-auth-linux-kerberos-enable
[ad]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-ad-ds-overview
[domain]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-auth-domain-services-enable
[linux-domain]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-auth-linux-kerberos-entra-domain-services
[entra]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-auth-hybrid-identities-enable
[share]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-assign-share-level-permissions
[authz]: https://learn.microsoft.com/azure/storage/files/storage-files-authorization-overview
[acl]: https://learn.microsoft.com/azure/storage/files/storage-files-identity-configure-file-level-permissions
[mi-role]: https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/storage#storage-file-data-smb-mi-admin
[rest-api]: https://learn.microsoft.com/rest/api/storageservices/file-service-rest-api
[rest]: https://learn.microsoft.com/azure/storage/files/authorize-oauth-rest
[sas]: https://learn.microsoft.com/azure/storage/common/storage-sas-overview
[uds]: https://learn.microsoft.com/rest/api/storageservices/create-user-delegation-sas
[data-auth]: https://learn.microsoft.com/azure/storage/common/authorize-data-access
[shared-key]: https://learn.microsoft.com/azure/storage/common/shared-key-authorization-prevent
[error13]: https://learn.microsoft.com/troubleshoot/azure/azure-storage/files/connectivity/files-troubleshoot-smb-connectivity#cause-4-storage-account-key-access-is-disabled-or-disallowed-via-a-policy
[nfs]: https://learn.microsoft.com/azure/storage/files/files-nfs-protocol
[nfs-mount]: https://learn.microsoft.com/azure/storage/files/storage-files-how-to-mount-nfs-shares
[nfs-tls]: https://learn.microsoft.com/azure/storage/files/encryption-in-transit-for-nfs-shares
