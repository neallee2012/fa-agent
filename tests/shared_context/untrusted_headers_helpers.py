"""工作台與 native 測試共用的偽造 header canary；不依賴舊送件路。"""

UNTRUSTED_CONTEXT_HEADERS = {
    "X-Actor-Id": "HEADER_ACTOR_CANARY",
    "X-MS-CLIENT-PRINCIPAL-ID": "HEADER_PRINCIPAL_CANARY",
    "X-Conversation-Id": "header-conversation-canary",
    "X-Recipient-Id": "header-recipient-canary",
    "X-Device-Id": "HEADER_DEVICE_CANARY",
    "X-Location": "HEADER_LOCATION_CANARY",
    "Forwarded": "for=_FA_METADATA_CANARY;host=metadata.invalid;proto=https",
    "X-Forwarded-For": "FA_SYNTHETIC_IP_CANARY",
    "User-Agent": "FA_SYNTHETIC_DEVICE_CANARY/0",
}
