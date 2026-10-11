"""Public, nonbinding notices for the capture-only personal POC."""
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

NOTICES = {
    "/teams/poc": (
        "FA Personal POC\n\n"
        "這是指定測試者的 Teams 個人聊天概念驗證，不是正式服務。\n"
        "目前只有路由設定握手；一般交辦、模型、批准及自動送件均未啟用。\n"
        "安裝成功不代表助理可以工作，也不代表訊息已送達或已讀。\n"
    ),
    "/teams/privacy": (
        "FA Personal POC：資料處理說明\n\n"
        "路由設定只接受事先指定租戶與測試者的簽章 Teams 握手。\n"
        "它會保存租戶及測試者識別碼、Teams bot／user／conversation／service URL、"
        "活動識別碼、內容雜湊與觀察時間，供核對同一路由與防止重送。\n"
        "此設定流程不呼叫模型、不建立一般工作、不保存 bearer token，也不向其他收件人發訊息。\n"
        "路由及稽核資料保存在 POC 的 Azure 儲存體；適用權限的管理者仍可能存取，"
        "不宣稱可防止管理者覆寫。未設定自動清除；停止服務不等於刪除已保存資料。\n"
        "請勿傳送密碼、付款資料或其他敏感內容；資料更正或清理需求請交由執行本 POC 的管理者處理。\n"
        "一般助理處理訊息及送件啟用前，必須另行驗收並更新本說明。\n"
    ),
    "/teams/terms": (
        "FA Personal POC：測試使用說明\n\n"
        "此頁僅說明測試範圍，不新增付費、訂閱或其他具拘束力的服務契約。\n"
        "僅限已指定的測試者在正確租戶的 personal chat 使用；不支援群組、頻道、"
        "檔案上傳、通話或 Teams 內的人工批准。\n"
        "請先取得本次安裝及設定握手的明確授權，再依管理者指示進行。\n"
        "服務可能停止或拒絕請求，不應用於正式業務、緊急情況或高風險決策。\n"
        "工作完成、成果可取得、訊息送達及真人已讀是不同狀態，不可互相替代。\n"
    ),
}


def notice_router() -> APIRouter:
    router = APIRouter()
    def handler(content):
        def endpoint():
            return PlainTextResponse(content, headers={"Cache-Control": "no-store"})
        return endpoint
    for path, content in NOTICES.items():
        router.add_api_route(path, handler(content), methods=["GET"], include_in_schema=False)
    return router
