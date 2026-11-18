"""從 test_skill_draft.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""


PURPOSE = "Procedure for preserving operator handoff steps."


BODY = "## Procedure\n\n1. Confirm the original request.\n2. Save only non-secret team guidance.\n"


OTHER = BODY + "3. Different content must not replace the original draft.\n"


OLD_PROMPTS = (
    (None, 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f", ()),
    ("fa-instruction-v1", 2094, "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb",
     ("read_context", "execute_operation")),
    ("fa-instruction-skills-v1", 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     ("read_context", "execute_operation", "read_skill")),
    ("fa-instruction-skill-purpose-v1", 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     ("read_context", "execute_operation", "read_skill")),
    ("fa-instruction-user-turns-v1", 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372",
     ("read_context", "execute_operation", "read_user_turn")),
    ("fa-instruction-work-memory-v1", 2719, "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3",
     ("read_context", "execute_operation", "read_work_memory")),
    ("fa-instruction-sop-copy-v1", 2645, "8f6151198fefda9410c59070d311d5ddfb724d6bebc845165b8027aa66cefa13",
     ("read_context", "execute_operation", "copy_sop_to_skill_draft")),
    ("fa-instruction-skill-resources-v1", 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     ("read_context", "execute_operation", "read_skill", "read_skill_resource")),
    ("fa-instruction-user-files-v1", 3201, "9c8fd82691f5b40e5b01c5d90d15a807dec08828bdb9b2b3fb9ddf1f77965296",
     ("read_context", "execute_operation", "read_user_turn", "read_user_file")),
    ("fa-instruction-assistant-replies-v1", 3299, "bc7ec9b2f7ee9cb783ce21c51872e5a82e38841737b67caf37ac7c37ae50a983",
     ("read_context", "execute_operation", "read_user_turn", "read_assistant_reply")),
    ("fa-instruction-history-v1", 3678, "3d1c1e75d3eb95e36090c169195264cacee24d957e3e0e356b76f25db0d5648f",
     ("read_context", "execute_operation", "read_user_turn", "read_user_file", "read_assistant_reply")),
)
