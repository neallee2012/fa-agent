"""從 test_web_test_console_attachments.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""


def draft(c, browser=None):
    return (browser or c.browser).ui.get("/api/state").json()["composer"]


def submit(c, attachments=None, *, request_id=None, browser=None, content="  Read this exact source.\n"):
    browser = browser or c.browser
    body = {"content": content, "model": "gpt-4.1-mini"}
    if attachments is not None:
        body["attachments"] = attachments
    return browser.ui.post(f"/api/instructions/{request_id or draft(c, browser)['request_id']}/submit",
                           json=body, headers=browser.headers)
