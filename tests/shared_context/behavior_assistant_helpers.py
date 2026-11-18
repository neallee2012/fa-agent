"""從 test_behavior_assistant.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import json


def call(name, args, number):
    return {"tool_calls": [{"index": 0, "id": f"call_{number}", "type": "function",
                           "function": {"name": name, "arguments": json.dumps(args)}}]}
