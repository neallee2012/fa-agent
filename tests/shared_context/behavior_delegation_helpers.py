"""從 test_behavior_delegation.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""


TASK = {
    "agent_type": "fa-delegate-v1",
    "mode": "sync",
    "name": "compare-fragments",
    "description": "Compare the approved Team fragments",
    "prompt": "Compare Team fragment A v1 (850 total, 320 assigned) and B v1 (530 remaining).",
}
