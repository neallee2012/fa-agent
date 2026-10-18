"""Native conversation client. Local records pin routing and requests, never execution authority."""

from datetime import datetime, timedelta
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import MAX_NATIVE_TURN_HTTP_BYTES
from shared_context.behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NATIVE_TURN_ERRORS, NativeCancel, NativeConversation, NativeDelivered, NativeEnvironmentNotice,
    NativeHistoryPage, NativeReceipt, NativeResultPage, NativeAdmissionObservation, NativeReceivedPage,
    NativeTurnStatus, check_continuation_link,
)
from shared_context.behavior_text_files import file_reference
from shared_context.behavior_user_images import NativeUserImageStored, NativeUserImageUpload, UserImage, canonical_png
from shared_context.behavior_attachment_catalog import AttachmentSourceCatalog
from shared_context.native_skill_draft_wire import NativeSkillDraftView
from shared_context.behavior_system_prompt_view import TurnSystemPrompt, check_turn_view
from shared_context.native_image_wire import NativeImageContent, NativeTurnImages
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE
from shared_context.native_append_wire import NativeAppendReceipt, NativeAppends
from shared_context.native_progress_wire import NativeProgressSnapshot
from shared_context.native_continuation_wire import NativeContinuationView, MAX_AUTOMATIC_CONTINUATIONS
from shared_context.errors import Problem
from .adapter import ConsoleError, PullAdapter, canonical, require, sha, strict_json

PREFIX = "/work-policy/v1/native-turns"
_NOT_ACCEPTED = frozenset({"AGENT_VERSION_FOLLOWING", "NATIVE_ADMISSION_PENDING"})
_FETCH = object()
# A conversation another Agent opened here: its thread is its first RID, read back from this Agent's receipts.
RECEIVED_PHASE = "agent-received"
RECEIVED_SECONDS = 15
MESSAGES = {code: "原生請求未完成；保留原對話與請求，不會換環境或自動重送。" for code in NATIVE_TURN_ERRORS}
MESSAGES.update({
    "AGENT_CONVERSATION_READ_ONLY": "這是其他助理交辦給這位助理的對話，只能檢視；回覆已自動送回原交辦對話。",
    "INSTRUCTION_ROUTE_CHANGED": "這段對話已固定原本的路徑；不能換後端或補造送出意圖。",
    "NATIVE_RID_MISSING": "這次尚未讀到原接單回條；不代表從未接單，也不會換編號重送。",
    "NATIVE_STREAM_NOT_STARTED": "助理還沒開始寫回覆；即時文字稍後再開，回覆照常顯示。",
    "NATIVE_STREAM_UNAVAILABLE": "這則回覆沒有即時文字；完成後照常顯示保存的回覆。",
    "NATIVE_STATE_STORE_MISSING": "原生資料儲存區尚未就緒；這次查讀不會建立新資料。",
    "NATIVE_STATE_STORE_TTL": "原生資料的保存期限不符要求；保留原資料並停止送出。",
    "NATIVE_STATE_STORE_UNKNOWN": "原生資料目前無法確認；請稍後核對同一筆請求。",
    "NATIVE_SESSION_CHANGED": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_TURN_BUSY": "助理目前忙碌，這次尚未執行；請保留原內容。",
    "NATIVE_TURN_NOT_STARTED": "這次沒有開始執行，請重新送出；原紀錄保留，工作台不會自動重送。",
    "NATIVE_MODEL_REFUSED": "模型服務在回覆前就拒絕了這次請求，沒有執行任何動作；可以換模型再問一次，工作台不會自動重送。",
    "NATIVE_RESULT_INCOMPLETE": "原答案的分頁、長度或雜湊不符；不顯示不完整內容。",
    "NATIVE_RESULT_MISSING": "尚未讀到完整原答案；不填造回覆。",
    "NATIVE_STATE_INVALID": "原生回應格式或來源不符；保留原紀錄。",
    "FOUNDRY_INVOCATION_UNKNOWN": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "FOUNDRY_RESPONSE_INVALID": "原生服務的回應不完整；不當成完成。",
    "FOUNDRY_NATIVE_BINDING_MISMATCH": "原生回應所屬環境不同；不使用這份回覆。",
    "AGENT_USAGE_CONFLICT": "原用量報告不同；不覆寫帳本或重新執行。",
    "AGENT_VERSION_FOLLOWING": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_ENVIRONMENT_CONFLICT": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_ENVIRONMENT_UNKNOWN": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_FILES_UNSUPPORTED": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_FILES_SOURCE_UNAVAILABLE": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_FILES_UNSAFE": "工作檔含不支援的連結、檔型或敏感材料；未遷移，也未送出草稿。",
    "NATIVE_FILES_LIMIT": "工作檔超過本次保存範圍；未截斷、未送出草稿。",
    "NATIVE_FILES_SOURCE_CHANGED": "原工作檔在保存期間有變動；沒有發布不完整的新環境。",
    "NATIVE_FILES_CONFLICT": "原檔案與保存清單不同；不覆寫目的檔案，也不重傳。",
    "NATIVE_FILES_UNKNOWN": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_FILES_UNAVAILABLE": "原工作檔尚無法完整讀取；不當成空目錄，草稿保留。",
    "NATIVE_FILES_ACCESS_DENIED": "目前身分無法存取原工作檔；不增加權限或改走其他儲存。",
    "NATIVE_FILES_CONFIRMATION_UNSUPPORTED": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_FILES_CONFIRMATION_REQUIRED": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_ENVIRONMENT_CHECKPOINT_UNAVAILABLE": "這段原對話沒有可恢復的成功保存狀態；保留原紀錄，不會用空白前文接續。",
    "NATIVE_ADMISSION_PENDING": "上一筆接單尚未核清；保留原內容，不另開工作或自動重送。",
    "NATIVE_RESPONSE_UNCONFIRMED": "尚未確認接手；會有限次數核回同一筆請求，不換編號。你也可以取消原請求。",
    "NATIVE_IMAGE_INVALID": "圖片沒有上傳成功，請重新附加後再送。",
    "NATIVE_IMAGE_MISSING": "圖片沒有上傳成功，請重新附加後再送。",
    "NATIVE_IMAGE_STATE_FULL": "這段對話的圖片已達上限，請開新對話再附圖。",
    "NATIVE_IMAGE_UNSUPPORTED": "目前的模型不能讀圖片，請移除圖片或換模型。",
    "USER_IMAGE_INPUT_INVALID": "只接受 png／jpg／webp 轉成的 PNG，每張最多 512 KiB、邊長最多 2000 px，一次最多 4 張且檔名不重複。",
    "USER_IMAGES_UNSUPPORTED": "這段對話的路徑不支援附圖；請在原生對話中附圖。",
    "NATIVE_ENVIRONMENT_UNAVAILABLE": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "WORK_POLICY_REVOKED": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "NATIVE_BOOTSTRAP_UNKNOWN": "傳送結果尚未確認；保留原請求，不會自動重送。",
    "ATTACHMENTS_DISABLED": "這位助理目前不能附加來源，請移除附件後再送。",
    "USER_TEXT_FILES_DISABLED": "這位助理目前不能附加文字檔，請移除檔案後再送。",
    "ATTACHMENT_SOURCE_DENIED": "這段對話不能附這份來源，請移除後再送。",
    "BEHAVIOR_SOURCE_DENIED": "這段對話不能附這份來源，請移除後再送。",
    "SOURCE_SCOPE_DENIED": "這段對話不能附這份來源，請移除後再送。",
    "ATTACHMENT_FORMAT": "這份附件不是可讀的完整文字（只支援純文字、Markdown、JSON），請移除或換一份。",
    "ATTACHMENT_SOURCE_LIMIT": "附件（含它引用的來源）太多或太大，請減少後再送。",
    "SOURCE_GRAPH_LIMIT": "附件（含它引用的來源）太多或太大，請減少後再送。",
    "ATTACHMENT_SOURCE_CHANGED": "這份來源剛有變更，請重新選擇後再送。",
    "SOURCE_ACCESS_CHANGED": "這份來源剛有變更，請重新選擇後再送。",
    "SOURCE_WITHDRAWN": "這份來源已被撤回，請移除後再送。",
    "SOURCE_EXPIRED": "這份來源已過期，請移除後再送。",
    "SOURCE_REQUIRED_MISSING": "這份來源或它引用的內容已不存在或對不上，請重新選擇。",
    "SOURCE_REFERENCE_MISMATCH": "這份來源或它引用的內容已不存在或對不上，請重新選擇。",
    "SOURCE_GRAPH_INVALID": "這份來源或它引用的內容已不存在或對不上，請重新選擇。",
    "ATTACHMENT_HISTORY_INVALID": "這份來源或它引用的內容已不存在或對不上，請重新選擇。",
    "SOURCE_RECORD_INVALID": "這份來源目前無法讀取或驗證，訊息沒有送出；請稍後再試，或先移除附件。",
    "SOURCE_RECEIPT_MISMATCH": "這份來源目前無法讀取或驗證，訊息沒有送出；請稍後再試，或先移除附件。",
    "SOURCE_INTEGRITY_ERROR": "這份來源目前無法讀取或驗證，訊息沒有送出；請稍後再試，或先移除附件。",
    "SOURCE_BLOB_UNAVAILABLE": "這份來源目前無法讀取或驗證，訊息沒有送出；請稍後再試，或先移除附件。",
    "BEHAVIOR_UNSAFE_CONTENT": "這份來源的內容不符合輸出政策，請移除後再送。",
    "CAPABILITY_MODEL_UNSUPPORTED": "目前的模型不能讀圖片，請移除圖片或換模型。",
    "INSTRUCTION_MODEL_UNAVAILABLE": "選的模型目前不能用，請換一個模型。",
    "MODEL_CAPACITY_INVALID": "這位助理的模型容量設定有問題，訊息沒有送出；請聯絡管理員。",
    "SKILL_CATALOG_CAPACITY_UNKNOWN": "這位助理的模型容量設定有問題，訊息沒有送出；請聯絡管理員。",
    "SKILL_CATALOG_BUDGET_TOO_SMALL": "這位助理的模型容量設定有問題，訊息沒有送出；請聯絡管理員。",
    "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN": "這位助理的模型容量設定有問題，訊息沒有送出；請聯絡管理員。",
    "NATIVE_PROFILE_UNSUPPORTED": "這位助理的設定組合不支援；請聯絡管理員。",
    "NATIVE_REQUEST_TOO_LARGE": "訊息加上附件太大，請縮短或減少附件。",
    "INSTRUCTION_TASK_TOO_LARGE": "訊息加上附件太大，請縮短或減少附件。",
})

def _tool_in_flight(snapshot):
    """A started public tool event without its completion (matched by tool_start_seq, else by tool name)."""
    by_seq, by_name = set(), {}
    for event in snapshot.progress.events:
        if event.kind == "tool_started":
            if event.tool_start_seq is not None:
                by_seq.add(event.tool_start_seq)
            else:
                by_name[event.tool_name] = by_name.get(event.tool_name, 0) + 1
        elif event.kind == "tool_completed":
            if event.tool_start_seq is not None:
                by_seq.discard(event.tool_start_seq)
            elif by_name.get(event.tool_name):
                by_name[event.tool_name] -= 1
    return bool(by_seq) or any(by_name.values())


def _last_seq(snapshot):
    return snapshot.progress.events[-1].seq if snapshot is not None and snapshot.progress.events else 0


def _signed_out(error):
    return error.status == 401 or error.upstream_status == 401


def running_session(conversation, receipts):
    """The Hosted session (VM) this conversation runs on now; the page's desktop connects there.

    A receipt names the session that admitted its turn, and Hosted runs a turn only in that session
    (``native_turns._original_session``). This bridge reads the conversation only on a whole refresh, so after a
    version follow the first turn's receipt is newer than it (Neal 2026-10-07 13:12「桌面無法連線」). The newest of
    the turns in flight and the turns numbered after the conversation this bridge read wins; otherwise the
    conversation's own answer as before. One Agent runs one turn at a time, so a receipt still read as running behind
    a later-numbered turn is an old read and is not used. ``receipts``: this thread's receipts, oldest first.
    """
    last = 0 if conversation is None else conversation.last_turn
    top = max([last, *(item.turn_number for item in receipts if item.turn_number is not None)])
    newer = [item for item in receipts if item.status in {"accepted", "started"} and (
        item.turn_number is None or item.turn_number >= top) or item.turn_number is not None and item.turn_number > last]
    if newer:
        # Not yet numbered is the newest; among equals the one remembered last.
        return max(reversed(newer), key=lambda item: float("inf") if item.turn_number is None
                   else item.turn_number).hosted_session_id
    if conversation is not None:
        return conversation.execution_hosted_session_id or conversation.hosted_session_id
    return next((item.hosted_session_id for item in receipts), None)


def _whole_reply(raw, reference):
    """The complete original reply: its byte length and hash are the ones its receipt names."""
    return len(raw) == reference.text_bytes and sha(raw) == reference.text_sha256


def _without_unknown_notice(receipt):
    """ADR-0009 §8 notices are API-only display hints: one this page does not know (an API released after it, e.g. a
    new notice kind) is dropped, never a reason to refuse the receipt; the receipt's own material is still strict."""
    if not isinstance(receipt, dict) or "environment_notice" not in receipt:
        return False
    try:
        NativeEnvironmentNotice.model_validate_json(canonical(receipt["environment_notice"]))
        return False
    except (ValueError, TypeError):
        del receipt["environment_notice"]
        return True


def _decode(model, raw):
    try:
        value = strict_json(raw)
        if model is NativeReceipt and _without_unknown_notice(value) or model is NativeTurnStatus and isinstance(
                value, dict) and _without_unknown_notice(value.get("receipt")):
            raw = canonical(value)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("NATIVE_STATE_INVALID", 502) from None


class NativeAdapter(PullAdapter):
    submit_path = PREFIX + "/submit"
    cancel_path = PREFIX + "/cancel"

    def _read(self, path, model, token, *, params=None):
        raw, _ = self.send("GET", PREFIX + path, None, token,
                           params=params, limit=MAX_NATIVE_TURN_HTTP_BYTES)
        return _decode(model, raw)

    def submit(self, command, token):
        return self._submit_to(self.submit_path, command, token)

    def _submit_to(self, path, command, token):
        raw, _ = self.send("POST", path, command.model_dump(mode="json"), token,
                           limit=MAX_NATIVE_TURN_HTTP_BYTES)
        value = _decode(NativeReceipt, raw)
        require(value.command == command, "NATIVE_RID_CONFLICT", 502)
        return value

    def receipt(self, rid, token):
        value = self._read("/requests/" + rid, NativeReceipt, token)
        require(value.request_id == rid, "NATIVE_STATE_SCOPE", 502)
        return value

    def continuation(self, rid, token):
        value = self._read("/requests/" + rid + "/continuation", NativeContinuationView, token)
        require(value.original_request_id == rid, "NATIVE_STATE_SCOPE", 502)
        return value

    def admission_observation(self, rid, token):
        value = self._read("/requests/" + rid + "/admission-observation", NativeAdmissionObservation, token)
        require(value.request_id == rid, "NATIVE_STATE_SCOPE", 502)
        return value

    def append(self, command, token):
        raw, _ = self.send("POST", PREFIX + "/append", command.model_dump(mode="json"), token,
                           limit=MAX_NATIVE_TURN_HTTP_BYTES)
        return _decode(NativeAppendReceipt, raw)

    def appends(self, cid, rid, token):
        return self._read("/conversations/" + cid + "/requests/" + rid + "/appends", NativeAppends, token)

    def progress(self, cid, rid, token):
        return self._read("/conversations/" + cid + "/requests/" + rid + "/progress",
                          NativeProgressSnapshot, token)

    def turn_status(self, cid, rid, token):
        """API #664: one read for one RID, carrying its receipt, public progress and a completed single-page reply."""
        value = self._read("/conversations/" + cid + "/requests/" + rid + "/status", NativeTurnStatus, token)
        require((value.receipt.conversation_id, value.receipt.request_id) == (cid, rid), "NATIVE_STATE_SCOPE", 502)
        return value

    def conversation(self, cid, token):
        value = self._read("/conversations/" + cid, NativeConversation, token)
        require(value.conversation_id == cid, "NATIVE_STATE_SCOPE", 502)
        return value

    def received(self, token):
        return self._read("/received", NativeReceivedPage, token)

    def history(self, cid, token, *, after_turn=0):
        after, turns, seen, original = after_turn, [], set(), None
        while True:
            page = self._read("/conversations/" + cid + "/history", NativeHistoryPage, token,
                              params={"after_turn": after, "limit": 100})
            require(page.conversation.conversation_id == cid, "NATIVE_STATE_SCOPE", 502)
            if original is None:
                original = page.conversation
            require((page.conversation.agent_id, page.conversation.hosted_session_id) ==
                    (original.agent_id, original.hosted_session_id), "NATIVE_STATE_SCOPE", 502)
            for turn in page.turns:
                require(turn.conversation_id == cid and turn.turn_number > after
                        and turn.turn_number == turn.result.turn_number and turn.request_id not in seen,
                        "NATIVE_STATE_INVALID", 502)
                after = turn.turn_number
                seen.add(turn.request_id)
                turns.append(turn)
            if page.next_after_turn is None:
                return page.conversation, turns
            require(page.turns and page.next_after_turn == after, "NATIVE_STATE_INVALID", 502)

    def result(self, cid, rid, reference, token):
        parts, size = [], 0
        for index in range(reference.text_parts):
            page = self._read("/conversations/" + cid + "/requests/" + rid + "/result",
                              NativeResultPage, token, params={"part": index})
            require((page.conversation_id, page.request_id, page.part, page.result) ==
                    (cid, rid, index, reference), "NATIVE_RESULT_INCOMPLETE", 502)
            data = page.text.encode("utf-8")
            size += len(data)
            require(size <= reference.text_bytes and (data or reference.text_parts == 1),
                    "NATIVE_RESULT_INCOMPLETE", 502)
            parts.append(data)
        raw = b"".join(parts)
        require(_whole_reply(raw, reference), "NATIVE_RESULT_INCOMPLETE", 502)
        return raw

    def change(self, command, token):
        path = self.cancel_path if isinstance(command, NativeCancel) else PREFIX + "/delivered"
        raw, _ = self.send("POST", path, command.model_dump(mode="json"), token,
                           limit=MAX_NATIVE_TURN_HTTP_BYTES)
        value = _decode(NativeReceipt, raw)
        require((value.conversation_id, value.request_id) == (command.conversation_id, command.request_id),
                "NATIVE_STATE_SCOPE", 502)
        if isinstance(command, NativeDelivered):
            require(value.delivered and value.result is not None
                    and value.result.result_digest == command.result_digest, "NATIVE_DELIVERY_CONFLICT", 502)
        return value

    def skill_draft(self, cid, rid, token):
        value = self._read("/conversations/" + cid + "/requests/" + rid + "/skill-draft", NativeSkillDraftView, token)
        require((value.conversation_id, value.request_id) == (cid, rid), "NATIVE_STATE_SCOPE", 502)
        return value

    def system_prompt(self, rid, token):
        try:
            value = self._read("/requests/" + rid + "/system-prompt", TurnSystemPrompt, token)
        except ConsoleError as error:
            if error.upstream_status == 404 and error.code == "NO_RESULT":
                raise ConsoleError("SYSTEM_PROMPT_UNAVAILABLE", 409, 404) from None
            raise
        require(value.request_id == rid, "NATIVE_STATE_SCOPE", 502)
        try:
            return check_turn_view(value)
        except ValueError:
            raise ConsoleError("SYSTEM_PROMPT_RESPONSE_INVALID", 502) from None

    def images(self, cid, rid, token, *, version_id=None):
        path = "/conversations/" + cid + "/requests/" + rid + "/images"
        value = self._read(path if version_id is None else path + "/" + version_id,
                           NativeTurnImages if version_id is None else NativeImageContent, token)
        require((value.conversation_id, value.request_id) == (cid, rid), "NATIVE_STATE_SCOPE", 502)
        if version_id is not None:
            require(value.entry.version_id == version_id, "IMAGE_DELIVERY_CONFLICT", 502)
        return value

    def upload_image(self, command, token):
        raw, _ = self.send("POST", PREFIX + "/images", command.model_dump(mode="json"), token)
        value = _decode(NativeUserImageStored, raw)
        data = canonical_png(command.png_base64)
        require((value.sha256, value.byte_length) == (sha(data), len(data)), "NATIVE_STATE_INVALID", 502)
        return value



class NativeInstructionControl:
    def __init__(self, console, legacy):
        self.console, self.legacy = console, legacy
        self.store, self.journal, self.clock = legacy.store, console.journal, console.clock
        self.preferred = legacy.connection.state_profile
        self.adapter = NativeAdapter(console.config, console.backend, api_prefix=console.api_prefix)
        from .responses import ResponsesAdapter
        self.responses_adapter = ResponsesAdapter(console.config, console.backend, api_prefix=console.api_prefix)
        self.server_profile = None
        self.responses_options = None
        self.platform_pages, self.platform_seen = {}, {}
        self.platform_observed = {}
        self.receipts, self.request_threads, self.conversations, self.turns = {}, {}, {}, {}
        self.answers, self.observed, self.errors = {}, {}, {}
        self.progress_snapshots, self.progress_errors, self.status_seq = {}, {}, {}
        self.continuations, self.continuation_errors = {}, {}
        self.received_checked, self.received_error = None, None
        from .appends import AppendControl
        self.appends = AppendControl(self)
        from .native_hitl import NativeHitlControl
        self.hitl = NativeHitlControl(self)
        from .native_questions import NativeQuestionControl
        self.questions = NativeQuestionControl(self)
        from .native_consents import NativeConsentControl
        self.consents = NativeConsentControl(self)
        from .native_routines import NativeRoutineControl
        self.routines = NativeRoutineControl(self)

    def refresh_routing(self, token, *, thread_id=None):
        from .agent_template import TemplateProfileUnsupported, download, options, parse_template

        if thread_id is not None and self.store.has_fixed_lane(thread_id):
            # The conversation's lane was pinned by its first message. Whether a follow-up runs now is the server's
            # answer to that submit (a receipt, a not_dispatched refusal or server_following), so nothing is read here.
            self.server_profile = None
            if self.store.lane(thread_id) != NATIVE_RESPONSES_PROFILE:
                self.responses_options = None
            elif self.responses_options is None:
                # After a console restart nothing has read them yet; the composer shows model_switch from them.
                self.responses_options = options(self.adapter, token).responses
            self._current(token)
            return
        self.server_profile, self.responses_options = None, None
        try:
            raw = download(self.adapter, token)
        except TemplateProfileUnsupported:
            # No versioned profile or selection (e.g. a registered peer before its first selection): only the
            # job-bound submit accepts such an Agent, so only a job-bound connection keeps its own lane; a native
            # connection still refuses here, before any write, because native capture rejects the same Agent.
            if self.preferred is not None:
                raise
            self._current(token)
            return
        template = parse_template(raw, code="TEMPLATE_RESPONSE_INVALID", status=502)
        needed = template.settings.state_profile == NATIVE_RESPONSES_PROFILE
        offered = options(self.adapter, token) if needed else None
        self._current(token)
        self.server_profile = template.settings.state_profile
        self.responses_options = None if offered is None else offered.responses

    def profile(self, thread_id):
        preferred = NATIVE_RESPONSES_PROFILE if self.server_profile == NATIVE_RESPONSES_PROFILE else self.preferred
        return self.store.lane(thread_id, preferred=preferred)

    def turn_adapter(self, thread_id):
        return self.responses_adapter if self.profile(thread_id) == NATIVE_RESPONSES_PROFILE else self.adapter


    def _current(self, token):
        require(self.console.vault.get() == token, "LOGIN_REQUIRED", 401)

    def _receipt(self, rid, token):
        value = self.adapter.receipt(rid, token)
        self._current(token)
        return value

    def skill_draft(self, rid, token):
        self._current(token)
        thread = self.thread_for(rid)
        require(thread is not None, "ACTION_NOT_ALLOWED", 403)
        self._saved(thread)
        original = self.receipts.get(rid)
        require(original is not None, "NATIVE_RID_MISSING", 409)
        value = self.adapter.skill_draft(original.conversation_id, rid, token)
        self._current(token)
        require(value.agent_id == original.agent_id, "NATIVE_STATE_SCOPE", 502)
        if value.receipt is not None:
            require(value.receipt.native_source.submit_sha256 == original.submit_digest, "NATIVE_STATE_SCOPE", 502)
        return value

    def images(self, rid, token, *, version_id=None):
        self._current(token)
        thread = self.thread_for(rid)
        require(thread is not None, "ACTION_NOT_ALLOWED", 403)
        self._saved(thread)
        original = self.receipts.get(rid)
        require(original is not None, "NATIVE_RID_MISSING", 409)
        value = self.adapter.images(original.conversation_id, rid, token, version_id=version_id)
        self._current(token)
        require(value.agent_id == original.agent_id and value.submit_sha256 == original.submit_digest,
                "NATIVE_STATE_SCOPE", 502)
        return value

    def system_prompt(self, rid, token):
        """Read-only: the API rebuilds this turn's prompt from its own receipt; nothing is composed here."""
        self._current(token)
        thread = self.thread_for(rid)
        require(thread is not None, "ACTION_NOT_ALLOWED", 403)
        self._saved(thread)
        original = self.receipts.get(rid)
        conversation = self.conversations.get(thread)
        known = original or conversation
        require(known is not None, "NATIVE_RID_MISSING", 409)
        value = self.adapter.system_prompt(rid, token)
        self._current(token)
        require((value.agent_id, value.conversation_id) == (known.agent_id, known.conversation_id),
                "NATIVE_STATE_SCOPE", 502)
        return value

    def _refused_attempts(self, rid):
        with self.journal.connect() as db:
            return db.execute(
                "SELECT outcome FROM intents WHERE task_id=? AND phase LIKE 'native-refused-retry-%' ORDER BY rowid",
                (rid,),
            ).fetchall()

    def _latest_attempt(self, rid, original):
        """The answer of the latest same-RID resend (native-refused-retry-N) if any, else the first submit's."""
        retries = self._refused_attempts(rid)
        return strict_json(retries[-1][0]) if retries else original

    def _latest_submission(self, rid):
        """The first submit with the answer of its latest same-RID resend, if any: a refused submit retried
        successfully reads as CONFIRMED. The intent (its body and send time) stays the first one's."""
        original = self.journal.get(rid, "submit")
        return None if original is None else {**self._latest_attempt(rid, original), "intent": original["intent"]}

    def _not_accepted(self, rid):
        return self._refusal(rid) is not None

    def _refusal(self, rid):
        """The latest attempt's answer when the server said this RID was not accepted, else None."""
        if rid in self.receipts:
            return None
        original = self.journal.get(rid, "submit")
        observation = self.journal.get(rid, "native-version-refusal-observation")
        if (original is not None and original["state"] == "UNKNOWN"
                and original.get("code") == "NATIVE_SESSION_CHANGED" and original.get("upstream_status") == 409
                and observation is not None
                and observation["state"] == "CONFIRMED" and not self._refused_attempts(rid)):
            return original
        if original is None or not self._refused(original):
            return None
        latest = self._latest_attempt(rid, original)
        return latest if self._refused(latest) else None

    def _retryable(self, rid):
        """Only a version refusal goes out again with the same RID (#657). Any other refusal is final, like
        busy_refused: the owner sends the text again under a new RID, so the old RID must not also go out."""
        refusal = self._refusal(rid)
        return (refusal is not None and refusal["state"] == "REFUSED"
                and (refusal.get("code") in _NOT_ACCEPTED or refusal.get("code") == "NATIVE_SESSION_CHANGED"))

    def _unread_unknown(self, thread, rid, error):
        """A later send whose answer was lost before Hosted created rid:<RID> reads back as missing. It stays UNKNOWN
        (never resent) and is shown on its own turn; the rest of the conversation is still read. A first message has
        nothing else to read (no CID yet), so its error is raised as before."""
        # The latest attempt counts: a version refusal (REFUSED) whose same-RID resend was lost is UNKNOWN too.
        latest = self._latest_attempt(rid, self.journal.get(rid, "submit"))
        if (error.code != "NATIVE_RID_MISSING" or latest is None or latest["state"] != "UNKNOWN"
                or not any(other != rid and self.request_threads.get(other) == thread for other in self.receipts)):
            return False
        self.errors[rid] = error.code
        return True

    @staticmethod
    def _refused(value):
        # not_dispatched: the server refused this RID before creating rid:<RID>, so there is nothing to read back.
        return value["state"] == "REFUSED" and (value.get("code") in _NOT_ACCEPTED or value.get("not_dispatched") is True)

    def _read_original(self, thread, rid, token):
        try:
            return self._remember(thread, self._receipt(rid, token))
        except ConsoleError as error:
            original = self.journal.get(rid, "submit")
            if (error.code != "NATIVE_RID_MISSING" or original is None or original["state"] != "UNKNOWN"
                    or original.get("code") != "NATIVE_SESSION_CHANGED" or original.get("upstream_status") != 409
                    or self._refused_attempts(rid)):
                raise
        observation = self.adapter.admission_observation(rid, token)
        self._current(token)
        phase = "native-version-refusal-observation"
        if self.journal.get(rid, phase) is None:
            self.journal.begin(rid, phase, {
                "method": "GET", "request_id": rid, "created_at": self.clock().isoformat()})
        self.journal.finish(rid, phase, {
            "state": "CONFIRMED", "observation": observation.model_dump(mode="json"),
            "observed_at": self.clock().isoformat(), "automatic_retry": False}, keep_confirmed=True)
        self.errors.pop(rid, None)
        return None

    def handles(self, thread_id=None):
        current = self.store.current(thread_id)
        return self.profile(current["thread_id"]) in {NATIVE_STATE_PROFILE, NATIVE_RESPONSES_PROFILE}

    def thread_for(self, rid):
        if rid in self.request_threads:
            return self.request_threads[rid]
        try:
            local = self.store.get(rid)
        except ConsoleError:
            return None
        return local["thread_id"] if self.handles(local["thread_id"]) else None

    def received_thread(self, thread_id):
        """A conversation another Agent opened in this Agent; read-only here, its reply already went back."""
        return thread_id is not None and self.journal.get(thread_id, RECEIVED_PHASE) is not None

    def _import_received(self, rid, token, item=None):
        receipt = self._receipt(rid, token)
        source = receipt.source
        require(source is not None and source.kind == "agent_request" and receipt.continuation is None
                and receipt.command.conversation_id is None
                and receipt.request_id == rid and (item is None or (item.conversation_id, item.sender_agent_id)
                                                   == (receipt.conversation_id, source.sender.agent_id)),
                "NATIVE_STATE_SCOPE", 502)
        with self.console.message_lock(RECEIVED_PHASE):
            thread = self.store.observe_native(receipt)
            require(thread == rid, "INSTRUCTION_ROUTE_CHANGED")
            if self.journal.get(rid, RECEIVED_PHASE) is None:
                self.journal.begin(rid, RECEIVED_PHASE, {
                    "state_profile": receipt.state_profile, "sender_agent_id": source.sender.agent_id,
                    "method": "GET", "path": PREFIX + "/requests/" + rid, "created_at": self.clock().isoformat()})
            self.journal.finish(rid, RECEIVED_PHASE, {
                "state": "VERIFIED", "native_receipt": receipt.model_dump(mode="json"),
                "observed_at": self.clock().isoformat()})
            self._remember(thread, receipt)
        return thread

    def open_received(self, token, thread_id):
        """A thread named by the page that is not local yet is read by its original RID; never submits."""
        if thread_id is None:
            return
        try:
            self.store.current(thread_id)
        except ConsoleError as error:
            if error.code != "INSTRUCTION_SCOPE":
                raise
            try:
                self._import_received(thread_id, token)
            except ConsoleError as refused:
                if refused.status == 401:
                    raise
                raise ConsoleError("INSTRUCTION_SCOPE") from None

    def refresh_received(self, token, *, thread_id=None):
        """List conversations other Agents opened here from this Agent's own receipts; never submits.

        Opens a named thread first (see open_received). The listing itself is a best-effort page, at most every
        RECEIVED_SECONDS, read when this Agent is loaded (not on progress reads) so an older API or image never
        blocks or slows this Agent's own chat.
        """
        self.open_received(token, thread_id)
        now = self.clock()
        if self.preferred is None and self.server_profile is None:
            return  # A job-bound console has no native conversations to list.
        if self.received_checked is not None and now - self.received_checked < timedelta(seconds=RECEIVED_SECONDS):
            return
        self.received_checked = now
        try:
            page = self.adapter.received(token)
            self._current(token)
            for item in reversed(page.items):
                if self.journal.get(item.request_id, RECEIVED_PHASE) is None:
                    self._import_received(item.request_id, token, item)
            self.received_error = None
        except ConsoleError as error:
            if error.status == 401:
                raise
            self.received_error = error.code

    def threads(self):
        values = self.store.threads()
        for value in values:
            saved = self.journal.get(value["thread_id"], RECEIVED_PHASE)
            if saved is not None:
                value["sender_agent_id"] = saved["intent"]["sender_agent_id"]
        return values

    def _saved(self, thread_id):
        for local in self.store.all(thread_id):
            # Already held in memory (_remember sets request_threads and receipts together); every poll calls this, so
            # re-reading the journal for each known message cost up to four SQLite reads per message per poll.
            if local["request_id"] in self.receipts:
                continue
            item = (self.journal.get(local["request_id"], "submit")
                    or self.journal.get(local["request_id"], "group-observation")
                    or self.journal.get(local["request_id"], RECEIVED_PHASE))
            if item is not None and item["intent"].get("state_profile") in {NATIVE_STATE_PROFILE, NATIVE_RESPONSES_PROFILE}:
                self.request_threads[local["request_id"]] = thread_id
                observed = self.journal.get(local["request_id"], "native-read")
                retained = observed if observed is not None and observed.get("native_receipt") is not None else item
                if retained.get("native_receipt") is not None and local["request_id"] not in self.receipts:
                    self._remember(thread_id, _decode(NativeReceipt, canonical(retained["native_receipt"])), persist=False)

    def _remember(self, thread_id, value, *, persist=True, submit_result=False):
        profile = self.profile(thread_id)
        require(value.state_profile == profile, "INSTRUCTION_ROUTE_CHANGED", 502)
        for previous in (v for rid, v in self.receipts.items() if self.request_threads.get(rid) == thread_id):
            require(previous.agent_id == value.agent_id and (
                previous.status == "busy_refused" or value.status == "busy_refused"
                or previous.conversation_id == value.conversation_id), "NATIVE_STATE_SCOPE", 502)
        previous = self.receipts.get(value.request_id)
        if previous is not None:
            require((previous.command, previous.capture_digest, previous.submit_digest, previous.hosted_session_id) ==
                    (value.command, value.capture_digest, value.submit_digest, value.hosted_session_id),
                    "NATIVE_CAPTURE_CHANGED", 502)
            require(previous.response_id is None or previous.response_id == value.response_id, "NATIVE_STATE_SCOPE", 502)
            if previous.status == "completed":
                require(value.status == "completed" and value.result == previous.result, "NATIVE_STATE_INVALID", 502)
        local = self.store.command(value.request_id)
        if local is not None:
            require(InstructionSubmit.model_validate_json(canonical(local)) == value.command and value.human_origin,
                    "NATIVE_RID_CONFLICT", 502)
        item = self.journal.get(value.request_id, "submit")
        if item is not None:
            intent = item["intent"]
            require(intent.get("state_profile") == profile
                    and intent.get("body_sha256") == sha(canonical(intent.get("body")))
                    and intent.get("body") == value.command.model_dump(mode="json")
                    and value.human_origin, "INSTRUCTION_ROUTE_CHANGED")
        self.request_threads[value.request_id], self.receipts[value.request_id] = thread_id, value
        if persist:
            if submit_result:
                require(item is not None, "INSTRUCTION_ROUTE_CHANGED")
                require(item["intent"].get("state_profile") == profile, "INSTRUCTION_ROUTE_CHANGED")
                self.journal.finish(value.request_id, "submit", {
                    "state": "CONFIRMED", "native_receipt": value.model_dump(mode="json"),
                    "observed_at": self.clock().isoformat(),
                })
            else:
                # A progress poll reads the same receipt every second; rewriting an unchanged row would commit (and in
                # remote mode upload every journal) on each poll. Only a receipt that changed is written.
                receipt = value.model_dump(mode="json")
                observed = self.journal.get(value.request_id, "native-read")
                if observed is None:
                    self.journal.begin(value.request_id, "native-read", {
                        "state_profile": profile, "thread_id": thread_id,
                        "method": "GET", "path": PREFIX + "/requests/" + value.request_id,
                        "created_at": self.clock().isoformat(),
                    })
                if observed is None or observed.get("native_receipt") != receipt:
                    self.journal.finish(value.request_id, "native-read", {
                        "state": "VERIFIED", "native_receipt": receipt,
                        "observed_at": self.clock().isoformat(),
                    })
        return value

    def _cid(self, thread_id):
        self._saved(thread_id)
        items = [self.receipts.get(row["request_id"]) for row in self.store.all(thread_id)]
        for value in reversed(items):
            if value is not None and value.status != "busy_refused":
                return value.conversation_id
            if value is not None and value.command.conversation_id is not None:
                return value.command.conversation_id
        require(not any(self.journal.get(row["request_id"], "submit") is not None
                        and row["request_id"] not in self.receipts and not self._not_accepted(row["request_id"])
                        for row in self.store.all(thread_id)),
                "INSTRUCTION_PENDING")
        return None

    def upload_image(self, rid, name, png_base64, token):
        """B1 PA-08: store one browser-converted PNG with this Agent; content-addressed, so a repeat is the same item."""
        thread_id = self.store.get(rid)["thread_id"]
        require(self.handles(thread_id), "INSTRUCTION_ROUTE_CHANGED")
        try:
            command = NativeUserImageUpload(png_base64=png_base64)
            reference = UserImage(name=name, sha256=sha(canonical_png(png_base64)), byte_length=len(canonical_png(png_base64)))
        except (ValueError, TypeError):
            raise ConsoleError("USER_IMAGE_INPUT_INVALID", 400) from None
        require(reference.name.lower().endswith(".png"), "USER_IMAGE_INPUT_INVALID", 400)
        self.adapter.upload_image(command, token)
        return reference.model_dump(mode="json")

    def submit(self, rid, content, model, attachments, files, token, images=()):
        local = self.store.get(rid)
        thread_id = local["thread_id"]
        require(self.handles(thread_id), "INSTRUCTION_ROUTE_CHANGED")
        profile = self.profile(thread_id)
        if profile == NATIVE_RESPONSES_PROFILE and not self.store.has_fixed_lane(thread_id):
            # Only a first message, whose lane is not pinned yet, is checked against the options read just before.
            require(self.responses_options is not None and self.responses_options.enabled, "NATIVE_TURNS_DISABLED", 503)
        require(self.journal.get(rid, "submit") is None, "RECONCILE_REQUIRED")
        require(self.legacy.models is not None and model in {item.id for item in self.legacy.models.models},
                "MODEL_NOT_AVAILABLE")
        try:
            command = InstructionSubmit.model_validate_json(canonical({
                "request_id": rid, "conversation_id": self._cid(thread_id), "content": content,
                "model": model, "attachments": attachments, "files": files, "images": images,
            }))
        except (ValueError, TypeError):
            raise ConsoleError("USER_IMAGE_INPUT_INVALID" if images else "INSTRUCTION_INVALID", 400) from None
        adapter = self.turn_adapter(thread_id)
        self.store.prepare(command.model_dump(mode="json"), freeze=True, state_profile=profile)
        self.journal.begin(rid, "submit", {
            "state_profile": profile, "method": "POST", "path": adapter.submit_path,
            "body": command.model_dump(mode="json"), "body_sha256": sha(canonical(command.model_dump(mode="json"))),
            "created_at": self.clock().isoformat(),
        })
        try:
            result = adapter.submit(command, token)
            self._current(token)
            self._remember(thread_id, result, submit_result=True)
            self.errors.pop(rid, None)
        except ConsoleError as error:
            self.errors[rid] = error.code
            self.journal.finish(rid, "submit", {"state": "REFUSED" if error.code in _NOT_ACCEPTED or error.not_dispatched else "UNKNOWN",
                                              "code": error.code, "upstream_status": error.upstream_status or error.status,
                                              **({"not_dispatched": True} if error.not_dispatched else {}),
                                              "observed_at": self.clock().isoformat(), "automatic_retry": False})
            raise

    def refresh(self, thread_id, token):
        if self.profile(thread_id) == NATIVE_RESPONSES_PROFILE:
            self._refresh_responses(thread_id, token)
            self.routines.refresh(thread_id, token)
            self._refresh_continuations(thread_id, token)
            self.hitl.refresh(thread_id, token)
            self.questions.refresh(thread_id, token)
            self.consents.refresh(thread_id, token)
            return
        self._saved(thread_id)
        if self.received_thread(thread_id):
            root = self.receipts.get(thread_id)
            if root is None or root.status in {"accepted", "started", "unknown"}:
                self._remember(thread_id, self._receipt(thread_id, token))
        locals_ = self.store.all(thread_id)
        for index, local in enumerate(locals_):
            rid = local["request_id"]
            saved = self.journal.get(rid, "submit")
            if (saved is None or self._not_accepted(rid)
                    or index < len(locals_) - 1 and saved["state"] == "CONFIRMED" and rid in self.receipts):
                continue
            try:
                self._read_original(thread_id, rid, token)
            except ConsoleError as error:
                if self._unread_unknown(thread_id, rid, error):
                    continue
                raise
            self.errors.pop(rid, None)
        cid = self._cid(thread_id)
        if cid is not None and any(
                value.turn_number is not None for rid, value in self.receipts.items()
                if self.request_threads.get(rid) == thread_id):
            previous_turns = self.turns.get(thread_id, ())
            after = previous_turns[-1].turn_number if previous_turns else 0
            conversation, turns = self.adapter.history(cid, token, after_turn=after)
            self._current(token)
            for value in (v for rid, v in self.receipts.items() if self.request_threads.get(rid) == thread_id):
                require(value.agent_id == conversation.agent_id and value.conversation_id == conversation.conversation_id
                        and (conversation.execution_hosted_session_id is not None
                             or value.hosted_session_id == conversation.hosted_session_id),
                        "NATIVE_STATE_SCOPE", 502)
            prior = self.conversations.get(thread_id)
            require(prior is None or conversation.last_turn >= prior.last_turn, "NATIVE_STATE_INVALID", 502)
            self.conversations[thread_id], self.turns[thread_id] = conversation, [*previous_turns, *turns]
            if conversation.last_request_id is not None:
                self._remember(thread_id, self._receipt(conversation.last_request_id, token))
            for turn in self.turns[thread_id]:
                self.request_threads[turn.request_id] = thread_id
                if (turn.delegation is not None or turn.queued_relay is not None) and turn.request_id not in self.receipts:
                    self._remember(thread_id, self._receipt(turn.request_id, token))
                self._answer(turn.conversation_id, turn.request_id, turn.result, token)
        for rid, value in tuple(self.receipts.items()):
            if self.request_threads.get(rid) == thread_id:
                if value.delegation is not None or value.queued_relay is not None:
                    self._remember(thread_id, self._receipt(rid, token))
                    value = self.receipts[rid]
                if value.result is not None:
                    self._answer(value.conversation_id, rid, value.result, token)
        self._refresh_peer_replies(thread_id, token)
        self.observed[thread_id] = self.clock().isoformat()
        self.routines.refresh(thread_id, token)
        self._refresh_continuations(thread_id, token)
        self.hitl.refresh(thread_id, token)
        self.questions.refresh(thread_id, token)
        self.consents.refresh(thread_id, token)

    def _refresh_continuations(self, thread, token):
        checked = set()
        for _ in range(MAX_AUTOMATIC_CONTINUATIONS + 1):
            pending = [value for rid, value in self.receipts.items()
                       if self.request_threads.get(rid) == thread and rid not in checked
                       and value.status == "unknown" and value.continuation_available]
            for original in pending:
                rid = original.request_id
                checked.add(rid)
                try:
                    view = self.adapter.continuation(rid, token)
                    self._current(token)
                    require((view.agent_id, view.conversation_id, view.source_hosted_session_id) == (
                        original.agent_id, original.conversation_id, original.hosted_session_id),
                        "NATIVE_STATE_SCOPE", 502)
                    self.continuations[rid] = view
                    self.continuation_errors.pop(rid, None)
                    if view.request_id is not None:
                        try:
                            child = self._receipt(view.request_id, token)
                        except ConsoleError as error:
                            if error.code == "NATIVE_RID_MISSING" and view.state == "prepared":
                                continue
                            raise
                        check_continuation_link(child, original)
                        self._remember(thread, child)
                        if child.result is not None:
                            self._answer(child.conversation_id, child.request_id, child.result, token)
                except ConsoleError as error:
                    if error.code == "NATIVE_CONTINUATION_MISSING":
                        self.continuations.pop(rid, None)
                        self.continuation_errors.pop(rid, None)
                    else:
                        self.continuation_errors[rid] = error.code
                except Problem as error:
                    self.continuation_errors[rid] = error.body["error"]["code"]

    def _continuation_usage(self, rid, thread):
        related = [value for key, value in self.receipts.items() if self.request_threads.get(key) == thread
                   and (key == rid or value.continuation is not None and value.continuation.root_request_id == rid)]
        if not any(value.continuation is not None and value.continuation.root_request_id == rid for value in related):
            return None
        return {
            "root_request_id": rid,
            "known_requests": sum(item.requests for value in related if value.usage is not None
                                  for item in value.usage.usage),
            "unknown_attempts": sum(value.usage is None or value.usage.state == "partial" for value in related),
            "attempts": [{"request_id": value.request_id,
                          "attempt": 0 if value.continuation is None else value.continuation.attempt,
                          "status": value.status, "usage": None if value.usage is None else value.usage.model_dump(mode="json")}
                         for value in sorted(related, key=lambda value: 0 if value.continuation is None else value.continuation.attempt)],
        }

    def turn_status(self, rid, token):
        """One light read of a single turn: its receipt, its public progress until finished, and its reply once done.

        Used while the owner's own turn runs instead of re-reading the whole conversation every second. Approvals and
        ask_user wait inside a tool call, so this turn's pending items are read when a tool is in flight, when the
        public progress has not moved since the last read (a wait), or when progress cannot be read; and once more
        when the turn has ended, so a card read mid-turn closes. Any end other than completed is followed by one
        whole-conversation read from the page (continuations, blockers, history).

        Once the receipt names its conversation, the API's one ``/status`` read (#664) brings the receipt, the public
        progress and a completed single-page reply together; a longer reply is read page by page as before. If that
        read fails (an older Hosted image, or a progress record it cannot return), the separate receipt, progress and
        reply reads still deliver the turn; a sign-in failure is raised as before.
        """
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        known = self.receipts.get(rid)
        status = None
        if known is not None:
            try:
                status = self.adapter.turn_status(known.conversation_id, rid, token)
                self._current(token)
            except ConsoleError as error:
                if _signed_out(error):
                    raise
        value = self._remember(thread, self._receipt(rid, token) if status is None else status.receipt)
        prior = self.progress_snapshots.get(rid)
        snapshot = prior
        if prior is None or not prior.finished:
            try:
                snapshot = (self.read_progress(rid, token) if status is None
                            else self.read_progress(rid, token, value=status.progress))
            except ConsoleError as error:
                if _signed_out(error):
                    raise
        moved = self.status_seq.get(rid) != _last_seq(snapshot)
        self.status_seq[rid] = _last_seq(snapshot)
        if value.status in {"accepted", "started"}:
            if snapshot is None or not moved or _tool_in_flight(snapshot):
                self.hitl.refresh(thread, token, only=rid)
                self.questions.refresh(thread, token, only=rid)
                self.consents.refresh(thread, token, only=rid)
        else:
            # Once at the end, so a card read mid-turn closes instead of staying after the turn.
            if rid in self.hitl.pages and not self.hitl.pages[rid].get("closed"):
                self.hitl.refresh(thread, token, only=rid)
            if rid in self.questions.pages:
                self.questions.refresh(thread, token, only=rid)
            if rid in self.consents.pages:
                self.consents.refresh(thread, token, only=rid)
        if value.result is not None:
            # The receipt above is already saved here; a reply read that fails must not keep it from the page (which
            # would go on showing the turn as running). The failure stays on this turn, and the next read tries again.
            try:
                self._answer(value.conversation_id, rid, value.result, token,
                             text=None if status is None else status.text)
                self.errors.pop(rid, None)
            except ConsoleError as error:
                if _signed_out(error):
                    raise
                self.errors[rid] = error.code
        self.observed[thread] = self.clock().isoformat()
        return thread

    def live_text(self, rid, token, *, starting_after=None):
        """RTT B: this owner Responses turn's read-only live-text stream; never a result, a send or a retry."""
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        require(self.profile(thread) == NATIVE_RESPONSES_PROFILE, "NATIVE_STREAM_UNAVAILABLE")
        known = self.receipts.get(rid)
        if known is None:
            refused = ConsoleError("NATIVE_STREAM_NOT_STARTED", 409)
            refused.automatic_retry = True
            raise refused
        return self.responses_adapter.live_text(known.conversation_id, rid, token, starting_after=starting_after)

    def read_progress(self, rid, token, *, value=_FETCH):
        """``value``: a snapshot another read of this RID already brought (``/status``); checked here the same way."""
        thread_id = self.thread_for(rid)
        require(thread_id is not None, "INSTRUCTION_SCOPE")
        conversation = self.conversations.get(thread_id)
        receipt = self.receipts.get(rid)
        require(receipt is not None or conversation is not None, "INSTRUCTION_SCOPE")
        cid = receipt.conversation_id if receipt is not None else conversation.conversation_id
        agent_id = receipt.agent_id if receipt is not None else conversation.agent_id
        try:
            if value is _FETCH:
                value = self.adapter.progress(cid, rid, token)
                self._current(token)
            require(value is not None, "NATIVE_PROGRESS_UNCONFIRMED", 502)
            require((value.agent_id, value.conversation_id, value.request_id) == (agent_id, cid, rid),
                    "NATIVE_STATE_SCOPE", 502)
            if receipt is not None:
                require((value.hosted_session_id, value.submit_digest) ==
                        (receipt.hosted_session_id, receipt.submit_digest),
                        "NATIVE_STATE_SCOPE", 502)
            previous = self.progress_snapshots.get(rid)
            if previous is not None and previous.progress.events:
                require(value.recorded and value.progress.events
                        and value.progress.events[-1].seq >= previous.progress.events[-1].seq,
                        "NATIVE_PROGRESS_UNCONFIRMED", 502)
            self.progress_snapshots[rid] = value
            self.progress_errors.pop(rid, None)
            return value
        except ConsoleError as error:
            self.progress_errors[rid] = error.code
            raise

    def response_reconciliation(self, rid):
        from .responses import RECONCILE_PHASE, ResponseReconciliation

        entry = self.journal.get(rid, RECONCILE_PHASE)
        if entry is None:
            submitted = self.journal.get(rid, "submit") or self.journal.get(rid, "group-start")
            if submitted is not None and submitted.get("upstream_status") in {401, 403}:
                return ResponseReconciliation(state="stopped", failures=1, code=submitted.get("code"))
            return None
        if entry["state"] == "UNKNOWN":
            return None
        try:
            return ResponseReconciliation.model_validate_json(canonical({
                key: value for key, value in entry.items() if key != "intent"}))
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_STATE_INVALID", 502) from None

    def response_reconcile_allowed(self, value):
        if value is not None and (value.continuation is not None or value.routine is not None):
            return False
        from .responses import MAX_RECONCILE_FAILURES

        if (value is None or value.state_profile != NATIVE_RESPONSES_PROFILE or value.source is not None
                or value.status != "accepted" or value.response_id is not None or value.cancel_requested
                or value.dispatch is None or value.dispatch.state == "already_queued"
                or self.store.command(value.request_id) is None):
            return False
        observed = self.response_reconciliation(value.request_id)
        if observed is not None:
            failures = observed.failures + int(observed.state == "in_flight")
            return (observed.state != "stopped" and failures < MAX_RECONCILE_FAILURES
                    and (observed.due_at is None or self.clock() >= observed.due_at))
        original = self.journal.get(value.request_id, "submit") or self.journal.get(value.request_id, "group-start")
        return original is not None and self.clock() >= (
            datetime.fromisoformat(original["intent"]["created_at"]) + timedelta(seconds=1))

    def reconcile_response(self, rid, token):
        thread = self.thread_for(rid)
        require(thread is not None and self.profile(thread) == NATIVE_RESPONSES_PROFILE, "INSTRUCTION_SCOPE")
        original = self._remember(thread, self._receipt(rid, token))
        value = self._continue_response(thread, original, token)
        self.observed[thread] = self.clock().isoformat()
        return value

    def _continue_response(self, thread, original, token):
        from .responses import MAX_RECONCILE_FAILURES, RECONCILE_PHASE, ResponseReconciliation

        if (original.state_profile != NATIVE_RESPONSES_PROFILE or not original.human_origin
                or original.status != "accepted" or original.response_id is not None):
            return original
        require(original.dispatch is not None, "NATIVE_STATE_INVALID", 502)
        if original.cancel_requested or original.dispatch.state == "already_queued":
            return original
        rid = original.request_id
        with self.console.message_lock(rid):
            self._current(token)
            initial_state = self.response_reconciliation(rid)
            if initial_state is not None and initial_state.state == "stopped":
                return original
            command = self.legacy.command(rid)
            require(command == original.command, "NATIVE_RID_CONFLICT", 502)
            entry = self.journal.get(rid, RECONCILE_PHASE)
            if entry is None:
                sent = self.journal.get(rid, "submit") or self.journal.get(rid, "group-start")
                require(sent is not None, "INSTRUCTION_ROUTE_CHANGED", 502)
                initial_failure = int(sent["state"] == "UNKNOWN")
                self.journal.begin(rid, RECONCILE_PHASE, {
                    "state_profile": NATIVE_RESPONSES_PROFILE, "thread_id": thread,
                    "body_sha256": sha(canonical(command.model_dump(mode="json"))),
                    "conversation_id": original.conversation_id, "hosted_session_id": original.hosted_session_id,
                    "submit_digest": original.submit_digest, "initial_failures": initial_failure,
                    "created_at": sent["intent"]["created_at"],
                })
                entry = self.journal.get(rid, RECONCILE_PHASE)
            intent = entry["intent"]
            require(intent["state_profile"] == NATIVE_RESPONSES_PROFILE and intent["thread_id"] == thread
                    and intent["body_sha256"] == sha(canonical(command.model_dump(mode="json")))
                    and (intent["conversation_id"], intent["hosted_session_id"], intent["submit_digest"]) == (
                        original.conversation_id, original.hosted_session_id, original.submit_digest),
                    "INSTRUCTION_ROUTE_CHANGED", 502)
            observed = self.response_reconciliation(rid)
            if observed is None:
                observed = ResponseReconciliation(
                    state="waiting", failures=intent["initial_failures"],
                    due_at=datetime.fromisoformat(intent["created_at"]) + timedelta(seconds=1))
            if observed.state == "in_flight":
                observed = observed.model_copy(update={"failures": min(MAX_RECONCILE_FAILURES, observed.failures + 1)})
            if observed.state == "stopped" or observed.failures >= MAX_RECONCILE_FAILURES:
                self.journal.finish(rid, RECONCILE_PHASE, observed.model_copy(
                    update={"state": "stopped", "due_at": None}).model_dump(mode="json"))
                return original
            if observed.due_at is not None and self.clock() < observed.due_at:
                return original
            step = min(observed.wait_step + 1, 6)
            due = self.clock() + timedelta(seconds=min(60, 2 ** step))
            self.journal.finish(rid, RECONCILE_PHASE, ResponseReconciliation(
                state="in_flight", failures=observed.failures, wait_step=step, due_at=due).model_dump(mode="json"))
            try:
                value = self.responses_adapter.reconcile(command, token)
                self._current(token)
                self._remember(thread, value)
            except ConsoleError as error:
                failures = min(MAX_RECONCILE_FAILURES, observed.failures + 1)
                retryable = error.code in {"NATIVE_RESPONSE_UNCONFIRMED", "LOCAL_OFFLINE", "BACKEND_UNAVAILABLE"} or (
                    (error.upstream_status or error.status) >= 500)
                stopped = not retryable or failures == MAX_RECONCILE_FAILURES
                self.journal.finish(rid, RECONCILE_PHASE, ResponseReconciliation(
                    state="stopped" if stopped else "waiting", failures=failures, wait_step=step,
                    due_at=None if stopped else due, code=error.code).model_dump(mode="json"))
                self.errors[rid] = error.code
                return original
            pending = value.status == "accepted" and value.response_id is None and (
                value.dispatch is not None and value.dispatch.state != "already_queued")
            self.journal.finish(rid, RECONCILE_PHASE, ResponseReconciliation(
                state="waiting" if pending else "owned", failures=observed.failures, wait_step=step,
                due_at=due if pending else None).model_dump(mode="json"))
            self.errors.pop(rid, None)
            return value

    def _peer_requests(self, thread):
        return tuple(value for rid, value in self.receipts.items()
                     if self.request_threads.get(rid) == thread and value.status == "completed"
                     and value.delegation is not None)

    @staticmethod
    def _peer_return_matches(original, returned):
        delegated = original.delegation
        source = None if returned is None else returned.source
        if source is None or source.kind not in {"agent_result", "agent_notice"}:
            return False
        return (returned.agent_id == original.agent_id and returned.conversation_id == original.conversation_id
                and returned.request_id == delegated.message_id and source.message_id == delegated.message_id
                and source.in_reply_to == delegated.message_id and source.recipient_agent_id == original.agent_id
                and source.sender.agent_id == delegated.recipient_agent_id
                and source.sender.conversation_id == delegated.conversation_id
                and (source.continuation is None or
                     (source.continuation.agent_id, source.continuation.conversation_id, source.continuation.request_id)
                     == (original.agent_id, original.conversation_id, original.request_id)))

    def _refresh_peer_replies(self, thread, token):
        for original in self._peer_requests(thread):
            rid = original.delegation.message_id
            returned = self.receipts.get(rid)
            if returned is None or returned.status in {"accepted", "started"}:
                try:
                    returned = self._receipt(rid, token)
                except ConsoleError as error:
                    if error.code == "NATIVE_RID_MISSING":
                        continue
                    raise
                require(self._peer_return_matches(original, returned), "NATIVE_STATE_SCOPE", 502)
                self._remember(thread, returned)
            else:
                require(self._peer_return_matches(original, returned), "NATIVE_STATE_SCOPE", 502)
            if returned.result is not None:
                self._answer(returned.conversation_id, rid, returned.result, token)

    def _refresh_responses(self, thread, token):
        self._saved(thread)
        locals_ = self.store.all(thread)
        for index, local in enumerate(locals_):
            rid = local["request_id"]
            if self.journal.get(rid, "submit") is None or self._not_accepted(rid):
                continue
            known = self.receipts.get(rid)
            # An older turn already read as finished cannot change; the conversation read below still reads the
            # latest and in-flight turns. Same rule as the Invocations lane in refresh().
            if (index < len(locals_) - 1 and known is not None and known.status in {"completed", "busy_refused"}
                    and known.queued_relay is None):
                continue
            try:
                value = self._read_original(thread, rid, token)
            except ConsoleError as error:
                if self._unread_unknown(thread, rid, error):
                    continue
                raise
            if value is None:
                continue
            if value.status != "accepted" or value.response_id is not None or (
                    value.dispatch is not None and value.dispatch.state == "already_queued"):
                self.errors.pop(rid, None)
        cid = self._cid(thread)
        if cid is not None:
            conversation = self.adapter.conversation(cid, token)
            self._current(token)
            require(conversation.state_profile == NATIVE_RESPONSES_PROFILE, "NATIVE_STATE_SCOPE", 502)
            self.conversations[thread] = conversation
            for rid in {conversation.last_request_id, conversation.inflight_request_id} - {None}:
                self._remember(thread, self._receipt(rid, token))
        self._refresh_peer_replies(thread, token)
        self.observed[thread] = self.clock().isoformat()

    def read_platform_history(self, thread, token, *, after=None, previous=False):
        require(self.handles(thread) and self.profile(thread) == NATIVE_RESPONSES_PROFILE, "INSTRUCTION_SCOPE")
        self._current(token)
        if self.responses_options is None:
            from .agent_template import options
            self.responses_options = options(self.adapter, token).responses
            self._current(token)
        require(self.responses_options is not None and self.responses_options.history_source is not None,
                "NATIVE_TURNS_DISABLED", 503)
        kind = self.responses_options.history_source
        page = self.platform_pages.get(thread)
        if after is not None or previous:
            require(page is not None and page.source_kind == kind and not (after is not None and previous),
                    "NATIVE_STATE_SCOPE", 409)
            if previous:
                require(kind == "response" and page.next_after is None and page.previous_response_id is not None,
                        "NATIVE_STATE_SCOPE", 409)
                identifier = page.previous_response_id
            else:
                require(after == page.next_after, "NATIVE_STATE_SCOPE", 409)
                identifier = page.source_id
        else:
            self.platform_seen[thread] = set()
            if kind == "conversation":
                identifier = self._cid(thread)
            else:
                known = [value for rid, value in self.receipts.items()
                         if self.request_threads.get(rid) == thread and value.response_id is not None]
                identifier = max(known, key=lambda value: value.turn_number or 0).response_id if known else None
                if identifier is None:
                    current = self.conversations.get(thread)
                    identifier = None if current is None else current.last_response_id
        if identifier is None:
            self.platform_pages.pop(thread, None)
            return None
        seen = self.platform_seen.setdefault(thread, set())
        marker = kind, identifier, after
        require(marker not in seen, "NATIVE_STATE_INVALID", 502)
        value = self.responses_adapter.history_page(kind, identifier, token, after=after)
        self._current(token)
        seen.add(marker)
        self.platform_pages[thread] = value
        self.platform_observed[thread] = self.clock().isoformat()
        return value

    def platform_history_view(self, thread):
        # Read only on request (support details); a failed read is returned to that request, never kept here.
        page = self.platform_pages.get(thread)
        return {
            "page": None if page is None else page.model_dump(mode="json"),
            "error": None,
            "observed_at": self.platform_observed.get(thread),
        }

    def _answer(self, cid, rid, reference, token, *, text=None):
        """``text``: a one-page reply that ``/status`` already brought, checked against its receipt like a paged read."""
        self._current(token)
        prior = self.answers.get(rid)
        if prior is not None:
            require(prior[0] == reference, "NATIVE_RESULT_INCOMPLETE", 502)
            return prior[1]
        if text is None:
            raw = self.adapter.result(cid, rid, reference, token)
            self._current(token)
        else:
            raw = text.encode("utf-8")
            require(reference.text_parts == 1 and _whole_reply(raw, reference), "NATIVE_RESULT_INCOMPLETE", 502)
        self.answers[rid] = reference, raw
        return raw

    def follow_up(self, thread_id, token):
        """A new local RID for the next message; whether it runs now, queues or is refused is the server's answer to
        its submit (Responses: busy_refused before the model; Invocations: queued behind the earlier turn)."""
        require(not self.received_thread(self.store.current(thread_id)["thread_id"]),
                "AGENT_CONVERSATION_READ_ONLY")
        return self.store.new(thread_id)

    def resume_accepted(self, rid, token):
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        if self._not_accepted(rid):
            require(self._retryable(rid), "ACTION_NOT_ALLOWED", 403)
            self._retry_refused(thread, rid, token)
            return
        if self.profile(thread) == NATIVE_RESPONSES_PROFILE:
            return self.reconcile_response(rid, token)
        original = self._remember(thread, self._receipt(rid, token))
        require(original.source is None and original.status == "accepted", "INSTRUCTION_PENDING")
        command = self.legacy.command(rid)
        require(command == original.command, "NATIVE_RID_CONFLICT")
        with self.journal.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM intents WHERE task_id=? AND phase LIKE 'native-resume-%'",
                               (rid,)).fetchone()[0]
        phase = "native-resume-" + str(count + 1)
        self.journal.begin(rid, phase, {"state_profile": NATIVE_STATE_PROFILE, "body": command.model_dump(mode="json"),
                                      "created_at": self.clock().isoformat()})
        result = self.adapter.submit(command, token)
        self._current(token)
        self._remember(thread, result)
        self.journal.finish(rid, phase, {"state": "CONFIRMED", "observed_at": self.clock().isoformat(),
                                        "native_status": result.status})

    def _retry_refused(self, thread, rid, token):
        require(self.journal.get(rid, "native-version-refusal-observation") is None, "RECONCILE_REQUIRED")
        command = self.legacy.command(rid)
        original = self.journal.get(rid, "submit")["intent"]
        body = command.model_dump(mode="json")
        require(original.get("body") == body and original.get("body_sha256") == sha(canonical(body)),
                "NATIVE_RID_CONFLICT")
        phase = "native-refused-retry-" + str(len(self._refused_attempts(rid)) + 1)
        self.journal.begin(rid, phase, {
            "state_profile": self.profile(thread), "body": body, "body_sha256": sha(canonical(body)),
            "created_at": self.clock().isoformat(),
        })
        try:
            result = self.turn_adapter(thread).submit(command, token)
            self._current(token)
            self._remember(thread, result)
            self.journal.finish(rid, phase, {
                "state": "CONFIRMED", "native_status": result.status,
                "observed_at": self.clock().isoformat(), "automatic_retry": False,
            })
            self.errors.pop(rid, None)
        except ConsoleError as error:
            self.errors[rid] = error.code
            self.journal.finish(rid, phase, {
                "state": "REFUSED" if error.code in _NOT_ACCEPTED or error.not_dispatched else "UNKNOWN", "code": error.code,
                **({"not_dispatched": True} if error.not_dispatched else {}),
                "observed_at": self.clock().isoformat(), "automatic_retry": False,
            })
            raise

    def change(self, rid, phase, token):
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        known = self.receipts.get(rid)
        if (phase == "cancel" and known is not None and known.status in {"accepted", "started"}
                and not known.cancel_requested):
            # A turn already read as running: the stop goes out at once, and the API answers it from the original
            # receipt (it returns that receipt unchanged if the turn has ended). A receipt read first could fail or
            # hang and keep the stop from being sent (stage 2026-10-10).
            original = known
        else:
            original = self._remember(thread, self._receipt(rid, token))
        if phase == "cancel" and original is not known:
            self._refresh_continuations(thread, token)
        if phase == "cancel":
            continuation = self.continuations.get(rid)
            require(original.status in {"accepted", "started"} or original.status == "unknown" and (
                continuation is not None and continuation.continuation is not None
                and not continuation.cancel_requested), "INSTRUCTION_PENDING")
            command = NativeCancel(conversation_id=original.conversation_id, request_id=rid)
        else:
            require(original.result is not None, "NATIVE_RESULT_MISSING")
            self._answer(original.conversation_id, rid, original.result, token)
            command = NativeDelivered(conversation_id=original.conversation_id, request_id=rid,
                                      result_digest=original.result.result_digest)
        prior = self.journal.get(rid, phase)
        profile = self.profile(thread)
        body = command.model_dump(mode="json")
        if prior is not None:
            require(prior["intent"].get("state_profile") == profile, "INSTRUCTION_ROUTE_CHANGED")
            if not (prior["state"] == "REFUSED" and prior.get("not_dispatched") is True):
                return
            # The API invoked no session for the earlier attempt: the same command (same RID) goes out again, recorded
            # as unconfirmed on the same row before it is sent.
            require(prior["intent"].get("body") == body, "NATIVE_RID_CONFLICT")
            self.journal.finish(rid, phase, {"state": "UNKNOWN", "code": "RECONCILE_REQUIRED",
                                           "observed_at": self.clock().isoformat()})
        else:
            self.journal.begin(rid, phase, {"state_profile": profile, "body": body,
                                          "created_at": self.clock().isoformat()})
        self._current(token)
        try:
            result = self.turn_adapter(thread).change(command, token)
        except ConsoleError as error:
            if not error.not_dispatched:
                raise
            self.journal.finish(rid, phase, {"state": "REFUSED", "code": error.code, "not_dispatched": True,
                                           "observed_at": self.clock().isoformat(), "automatic_retry": False})
            if phase == "cancel":
                # The task view says the stop was not sent and can be pressed again; no error banner about a message.
                return
            raise
        self._current(token)
        value = self._remember(thread, result)
        confirmed = value.cancel_requested if phase == "cancel" else value.delivered
        if phase == "cancel" and value.status == "unknown" and not confirmed:
            self._refresh_continuations(thread, token)
            continuation = self.continuations.get(rid)
            confirmed = continuation is not None and continuation.cancel_requested
        if phase == "cancel" and not confirmed and value.status == "completed":
            # The API answered with the original receipt of a turn that had already completed (the bridge still held
            # it as running): the stop reached the API and was not needed. Record that answer, not an unknown stop.
            # Any other answer (busy_refused included) is not "the work finished"; it stays an unknown stop below.
            self.journal.finish(rid, phase, {"state": "CONFIRMED",
                                           "receipt": {"result": {"cancel_accepted": False, "status": value.status}},
                                           "observed_at": self.clock().isoformat(), "automatic_retry": False})
            return
        self.journal.finish(rid, phase, {"state": "CONFIRMED" if confirmed else "UNKNOWN",
                                       "confirmation_level": "client_received" if confirmed and phase == "acknowledge" else None,
                                       "observed_at": self.clock().isoformat(), "automatic_retry": False})

    def download(self, rid, token):
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        value = self._remember(thread, self._receipt(rid, token))
        require(value.result is not None, "NATIVE_RESULT_MISSING")
        return self._answer(value.conversation_id, rid, value.result, token), value.turn_number

    def file(self, rid, name, token):
        thread = self.thread_for(rid)
        require(thread is not None, "INSTRUCTION_SCOPE")
        value = self._remember(thread, self._receipt(rid, token))
        item = next((item for item in value.command.files if item.name == name), None)
        require(item is not None, "USER_FILE_UNAVAILABLE")
        return item.text.encode("utf-8")

    def input_template(self, rid, token):
        thread = self.thread_for(rid)
        require(thread is not None and self.store.current(thread)["request_id"] == rid, "INSTRUCTION_SCOPE")
        cid = self._cid(thread)
        if cid is None:
            from .agent_template import download, parse_template
            template = parse_template(download(self.legacy.adapter, token), code="USER_FILE_CAPABILITY_INVALID", status=502)
        else:
            template = self.adapter.conversation(cid, token).capture.template
        self._current(token)
        return thread, cid, template

    def text_file_capability(self, rid, token):
        thread, cid, template = self.input_template(rid, token)
        return {"thread_id": thread, "request_id": rid, "conversation_id": cid,
                "read_at": self.clock().isoformat(), "user_text_file_profile": template.settings.user_text_file_profile,
                "authorization": False}

    def attachment_sources(self, rid, token):
        thread = self.thread_for(rid)
        require(thread is not None and self.store.current(thread)["request_id"] == rid, "INSTRUCTION_SCOPE")
        cid = self._cid(thread)
        path = "/work-policy/v1/attachment-sources" if cid is None else PREFIX + "/conversations/" + cid + "/attachment-sources"
        raw, _ = self.adapter.send("GET", path, None, token)
        self._current(token)
        catalog = _decode(AttachmentSourceCatalog, raw)
        require(catalog.conversation_id == cid, "ATTACHMENT_CATALOG_INVALID", 502)
        return {"thread_id": thread, "request_id": rid, "conversation_id": cid, "catalog": catalog.model_dump(mode="json")}

    def views(self, thread_id):
        self._saved(thread_id)
        rows = {row["request_id"]: row for row in self.store.all(thread_id)}
        order = [turn.request_id for turn in self.turns.get(thread_id, ())]
        order.extend(rid for rid in rows if rid not in order)
        order.extend(rid for rid in self.receipts if self.request_threads.get(rid) == thread_id and rid not in order)
        if self.profile(thread_id) == NATIVE_RESPONSES_PROFILE:
            def position(rid):
                value = self.receipts.get(rid)
                number = None if value is None else value.turn_number
                return number is None, number or 0
            order.sort(key=position)
        return [self.task_view(rid, thread_id, rows.get(rid)) for rid in order]

    def task_view(self, rid, thread_id, local=None):
        value = self.receipts.get(rid)
        turn = next((turn for turn in self.turns.get(thread_id, ()) if turn.request_id == rid), None)
        command = None if value is None else value.command
        continuation = value.continuation if value is not None else None if turn is None else turn.continuation
        routine = value.routine if value is not None else None if turn is None else turn.routine
        user_text = (local["content"] if local is not None and (value is None or value.human_origin) else
                     turn.user_text if turn is not None else command.content if value is not None and value.human_origin else None)
        agent_text = (turn.agent_text if turn is not None else
                      command.content if value is not None and value.source is not None else None)
        routine_text = None
        if routine is not None:
            routine_text = command.content if command is not None else turn.routine_text
            user_text = agent_text = None
        continuation_text = None
        if continuation is not None:
            continuation_text = command.content if command is not None else turn.continuation_text
            user_text = agent_text = None
        source = value.source if value is not None else None if turn is None else turn.source
        result = self.answers.get(rid)
        authenticated = self.console.vault.state() == "AVAILABLE"
        current = value is not None and thread_id in self.observed
        not_accepted = self._not_accepted(rid)
        observation = self.journal.get(rid, "native-version-refusal-observation")
        status = (value.status if value is not None else "completed" if turn is not None
                  else "not_accepted" if not_accepted else "unknown")
        allowed = {"status": authenticated, "submit-reconcile": authenticated and not current,
                   "submit-retry": authenticated and (self._retryable(rid) or current and value is not None
                                   and value.human_origin and value.status == "accepted"),
                   "read-reply": authenticated and (result is not None or value is not None and value.result is not None),
                   "cancel": authenticated and current and status in {"accepted", "started"} and not value.cancel_requested,
                   "read-cancel-receipt": authenticated and self.journal.get(rid, "cancel") is not None,
                   "confirm-reply": authenticated and current and result is not None and not value.delivered,
                   "read-files": authenticated and command is not None and bool(command.files)}
        if observation is not None:
            allowed["submit-retry"] = False
        if continuation is not None:
            allowed["submit-retry"] = allowed["submit-reconcile"] = False
        handoff = self.continuations.get(rid)
        if authenticated and current and status == "unknown" and handoff is not None:
            allowed["cancel"] = handoff.continuation is not None and not handoff.cancel_requested
        allowed["read-skill-draft"] = (authenticated and value is not None and value.human_origin
            and value.capture.capabilities is not None
            and any(grant.entry_id == "fa-skillify-v1" for grant in value.capture.capabilities.grants))
        allowed["read-images"] = (authenticated and value is not None and value.turn_number is not None
            and value.capture.capabilities is not None
            and any(grant.entry_id == "fa-image-delivery-v1" for grant in value.capture.capabilities.grants))
        allowed["read-appends"] = authenticated and value is not None
        allowed["read-progress"] = authenticated and (value is not None or turn is not None)
        allowed["read-system-prompt"] = authenticated and (value is not None or turn is not None)
        if self.profile(thread_id) == NATIVE_RESPONSES_PROFILE and value is not None:
            allowed["submit-retry"] = False
            allowed["submit-reconcile"] = authenticated and self.response_reconcile_allowed(value)
        if routine is not None:
            allowed["submit-retry"] = allowed["submit-reconcile"] = False
        reconciliation = self.response_reconciliation(rid) if self.profile(thread_id) == NATIVE_RESPONSES_PROFILE else None
        for action in ("submit", "fetch", "acknowledge", "reconcile",
                       "submit-retry", "read-context", "read-delegation"):
            allowed.setdefault(action, False)
        # The model this message asked for, also when it was refused before any receipt (a model switch the
        # conversation could not take); only for naming it, never a selection.
        requested = getattr(command, "model", None)
        if not requested and value is None:
            requested = (self.store.command(rid) or {}).get("model")
        return {
            "state_profile": self.profile(thread_id), "task_id": rid, "submit_request_id": rid, "thread_id": thread_id,
            "label": (user_text or agent_text or routine_text or rid)[:60], "description": "原生回合；狀態以 Agent 保存的回條為準。",
            "mode": "native_turn", "instruction": user_text, "agent_text": agent_text,
            **({"routine": routine.model_dump(mode="json"), "routine_text": routine_text} if routine is not None else {}),
            "continuation": None if continuation is None else continuation.model_dump(mode="json"),
            "continuation_text": continuation_text,
            "continuation_state": (None if rid not in self.continuations else
                                    self.continuations[rid].model_dump(mode="json")),
            "continuation_error": self.continuation_errors.get(rid),
            "continuation_usage": self._continuation_usage(rid, thread_id),
            "source": None if source is None else source.model_dump(mode="json"),
            "admission": None if value is None else {"conversation_id": value.conversation_id,
                "turn_number": value.turn_number, "model": value.capture.model.id},
            **({"requested_model": requested} if requested else {}),
            "status": {"data": {"state": status}, "observed_at": self.observed.get(thread_id)},
            "native_receipt": None if value is None else {"status": value.status,
                "cancel_requested": value.cancel_requested, "delivered": value.delivered,
                "failure_code": value.failure_code, "notification_error": value.notification_error,
                "hosted_session_id": value.hosted_session_id, "response_id": value.response_id,
                **({"queued_relay": value.queued_relay.model_dump(mode="json")} if value.queued_relay is not None else {}),
                # Tool sources this turn ran without (their tool list could not be read); shown on the reply.
                **({"unavailable_sources": [item.model_dump(mode="json") for item in value.assembly.unavailable_sources]}
                   if value.assembly is not None and value.assembly.unavailable_sources else {}),
                "dispatch": None if value.dispatch is None else value.dispatch.model_dump(mode="json"),
                # ADR-0009 §8: the API's notice on this answer (a submit's per-Agent slot notice, or a read's
                # earlier-version view); the page shows it as a status line, never as an error.
                **({"environment_notice": value.environment_notice.model_dump(mode="json")}
                   if value.environment_notice is not None else {})},
            "not_accepted": not_accepted,
            "progress": (None if rid not in self.progress_snapshots else {
                "data": {"state": status, "progress": self.progress_snapshots[rid].progress.model_dump(mode="json")},
                "recorded": self.progress_snapshots[rid].recorded,
                "finished": self.progress_snapshots[rid].finished,
                "truncated": self.progress_snapshots[rid].truncated,
                "unconfirmed": self.progress_snapshots[rid].unconfirmed,
                "byte_length": self.progress_snapshots[rid].byte_length,
            }),
            "progress_error": self.progress_errors.get(rid), "result": None,
            "reply": None if result is None else {"data": {"text": result[1].decode("utf-8")},
                                                  "sha256": result[0].text_sha256, "byte_length": result[0].text_bytes,
                                                  "observed_at": self.observed.get(thread_id)},
            "submission": ({"state": "OBSERVED", "method": "GET"} if continuation is not None or routine is not None
                           or self.journal.get(rid, RECEIVED_PHASE) is not None else
                           self._latest_submission(rid) or {"state": "CONFIRMED" if turn is not None else "NOT_SENT"}),
            "submission_observation": None if observation is None else observation.get("observation"),
            "cancellation": self.journal.get(rid, "cancel"), "acknowledgment": self.journal.get(rid, "acknowledge"),
            "user_files": [] if command is None else [file_reference(item).model_dump(mode="json") for item in command.files],
            "user_images": [] if command is None else [item.model_dump(mode="json") for item in getattr(command, "images", ())],
            "attachments": [] if command is None else [item.model_dump(mode="json") for item in command.attachments],
            "actions": {action: {"enabled": enabled, "reason": "" if enabled else "此原生回合目前不能執行這個動作。"}
                        for action, enabled in allowed.items()},
            "error": None if rid not in self.errors else {"code": self.errors[rid]},
            "response_reconciliation": None if reconciliation is None else reconciliation.model_dump(mode="json"),
            "appends": self.appends.views(rid),
            "append_error": self.appends.errors.get(rid),
        }

    def view(self, thread_id=None):
        draft = self.store.current(thread_id)
        thread = draft["thread_id"]
        profile = self.profile(thread)
        self._saved(thread)
        tasks = self.views(thread)
        value = self.receipts.get(draft["request_id"])
        current = self.conversations.get(thread)
        last = None if current is None else self.receipts.get(current.last_request_id)
        if last is None:
            last = value
        for _ in range(MAX_AUTOMATIC_CONTINUATIONS):
            continuation = None if last is None else self.continuations.get(last.request_id)
            following = None if continuation is None else self.receipts.get(continuation.request_id)
            if following is None:
                break
            last = following
        authenticated = self.console.vault.state() == "AVAILABLE"
        command = self.store.command(draft["request_id"])
        prior = self.journal.get(draft["request_id"], "submit")
        received = self.received_thread(thread)
        imported = self.journal.get(draft["request_id"], "group-observation") is not None or received
        known = [item for rid, item in self.receipts.items() if self.request_threads.get(rid) == thread]
        cid = current.conversation_id if current is not None else next(
            (item.conversation_id for item in known if item.status != "busy_refused"), None)
        # Neal 2026-10-06 09:58 「FA portal為什麼要控制user能否傳送訊息、能否續問?」: the workbench is a thin client
        # (AGENTS.md 9). Whether a message runs now, queues or is refused is the server's answer to the submit (Hosted
        # rid:<RID>, busy_refused, AGENT_VERSION_FOLLOWING, continuation_blocker); the only local rule left is that a
        # sent RID is never sent again (submit: RECONCILE_REQUIRED; _cid: no CID while the first send is unknown).
        # A blocker the server already reported for this conversation is shown, not guessed.
        conversation_blocker = None if current is None else current.continuation_blocker
        can_follow = authenticated and draft["content"] is not None and not received and conversation_blocker is None
        blocker = None
        if self._not_accepted(draft["request_id"]):
            blocker = (prior["code"] if prior is not None and prior.get("not_dispatched") is True
                       else "NATIVE_SUBMISSION_NOT_ACCEPTED")
        if self._retryable(draft["request_id"]):
            # This text goes out again with the same RID once the version follow is done; a new RID would send it twice.
            can_follow = False
        blocker = conversation_blocker or blocker
        if received:
            # Its owner is the sending Agent's original request; a human message here would not reach it.
            can_follow, blocker = False, "AGENT_CONVERSATION_READ_ONLY"
        append_original = None if received else next((item for item in (value, last, *reversed(known)) if item is not None
                                and item.status in {"accepted", "started"} and not item.cancel_requested), None)
        append_target = (self.appends.proposal(append_original) if authenticated
                         and thread in self.observed and append_original is not None else None)
        models = self.legacy.models
        return {
            **draft, "state_profile": profile, "editable": authenticated and draft["content"] is None,
            "can_append": append_target is not None and not append_target["pending"],
            "append_target": append_target,
            "command_incomplete": command is None and prior is not None,
            "can_submit": authenticated and prior is None and not imported and models is not None
            and conversation_blocker is None,
            "can_follow_up": can_follow, "can_new_conversation": authenticated, "can_reset": False,
            "conversation_id": cid, "conversation": None if cid is None else {
                "state_profile": profile, "conversation_id": cid,
                "can_follow_up": can_follow,
                "turns": [{"request_id": task["task_id"], "state": task["status"]["data"]["state"]}
                          for task in tasks],
            },
            "hosted_session_id": running_session(current, known),
            "hosted_agent_version": None, "threads": self.threads(),
            "agent_conversation": None if not received else {
                "sender_agent_id": self.journal.get(thread, RECEIVED_PHASE)["intent"]["sender_agent_id"],
                "read_only": True},
            "received_error": None if self.received_error is None else {"code": self.received_error},
            "progress_available": authenticated and (prior is not None or cid is not None),
            "awaiting_agent_reply": any(
                not self._peer_return_matches(original, self.receipts.get(original.delegation.message_id))
                or self.receipts[original.delegation.message_id].status in {"accepted", "started"}
                for original in self._peer_requests(thread)) or any(
                    value.queued_relay is not None and value.queued_relay.status == "queued"
                    for rid, value in self.receipts.items() if self.request_threads.get(rid) == thread),
            "observed_at": self.observed.get(thread), "follow_up_blocker": blocker,
            "model": command.get("model") if command else None if models is None else models.default_model,
            "models": None if models is None else models.model_dump(mode="json"),
            "models_observed_at": self.legacy.models_observed_at,
            "attachments": [] if command is None else command.get("attachments", []),
            "user_files": [] if value is None else [file_reference(item).model_dump(mode="json") for item in value.command.files],
            "input_capacity": {"unit": "quoted-utf8-bytes", "maximum": 8192},
            "message": ("Responses尚未確認接手時，工作台以原RID有限核回；已開始或unknown不重送。"
                        if profile == NATIVE_RESPONSES_PROFILE else
                        "原生回合由 Agent 保存；失聯只核對原請求，不會自動重送。"),
            "progress_polling": {"method": "GET", "interval_ms": 1000, "model_or_reserve": False},
            "connection_metadata": self.legacy.connection.model_dump(mode="json"),
            "connection_metadata_is_not_live_attestation": True,
            "attention": self.routines.attention([*self.hitl.attention(), *self.questions.attention(),
                                                   *self.consents.attention()]),
            **({"routines": self.routines.view(thread)} if self.routines.enabled else {}),
            **({"native_hitl": self.hitl.view(thread), "native_questions": self.questions.view(thread),
                "native_consents": self.consents.view(thread)}
               if self.hitl.enabled else {}),
            "platform_history": self.platform_history_view(thread) if profile == NATIVE_RESPONSES_PROFILE else None,
            "responses": None if self.responses_options is None else self.responses_options.model_dump(mode="json"),
        }
