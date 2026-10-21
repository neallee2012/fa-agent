"""Loopback-only fixed-test bridge with durable non-secret once intents.

Remote mode (ADR-0008 §3) reuses the same code behind Container Apps Entra sign-in; see ``remote.py``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, closing, contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from .adapter import (
    CliTokenProvider, ConsoleConfig, SteadyConsoleConfig, ConsoleError, OriginalFailureAdapter, PullAdapter, TokenVault,
    canonical, now_utc, require, sha, strict_json,
)
from .standing import StandingConsoleConfig, StandingWorkObservation
from .work_policy import checked_observation
from .pull_continuation import PullContinuationSidecar
from .instructions import NATIVE_LANE, InstructionConnection
from .agent_connections import AgentConnections, AgentConsoles, MESSAGES as AGENT_MESSAGES, read_messages, validate_default
from .agent_onboarding import AgentOnboarding, MESSAGES as ONBOARDING_MESSAGES, parse_input as onboarding_input
from .native import NativeInstructionControl, MESSAGES as NATIVE_MESSAGES
from .native_hitl import MESSAGES as HITL_MESSAGES, install_hitl_routes
from .groups import GroupControl, MESSAGES as GROUP_MESSAGES, install_group_routes
from .native_questions import MESSAGES as QUESTION_MESSAGES, install_question_routes
from .native_consents import install_consent_routes
from .team_skills import MESSAGES as TEAM_SKILL_MESSAGES, install_team_skill_routes, is_team_skill_write
from .team_mcp import MESSAGES as TEAM_MCP_MESSAGES, install_team_mcp_routes, is_team_mcp_write
from .team_connectors import MESSAGES as TEAM_CONNECTOR_MESSAGES, install_team_connector_routes, is_connector_write
from .routine_settings import MESSAGES as ROUTINE_SETTINGS_MESSAGES, install_routine_settings_routes, routines_available
from . import realtime
from . import prewarm as typing_prewarm
from shared_context.assistant_image_wire import MAX_IMAGE_BYTES as _MAX_USER_IMAGE_BYTES

# One base64 PNG plus its name; the API refuses anything over the same image bound.
MAX_IMAGE_UPLOAD_BODY_BYTES = (_MAX_USER_IMAGE_BYTES + 2) // 3 * 4 + 4096
# A conversation's progress reads take turns. One that does not come back must not hold the next ones (a reload's first
# read included) forever: a read waits this long for its turn, then answers retryable and the page reads again.
PROGRESS_LOCK_WAIT_SECONDS = 25

MESSAGES = {
    **AGENT_MESSAGES,
    **ONBOARDING_MESSAGES,
    **NATIVE_MESSAGES,
    **HITL_MESSAGES,
    **GROUP_MESSAGES,
    **QUESTION_MESSAGES,
    **TEAM_SKILL_MESSAGES,
    **TEAM_MCP_MESSAGES,
    **TEAM_CONNECTOR_MESSAGES,
    **ROUTINE_SETTINGS_MESSAGES,
    **realtime.MESSAGES,
    **typing_prewarm.MESSAGES,
    "SUBMISSION_RECONCILE_REQUIRED": "先核對原接單；只有目前確認尚未接單或未排入佇列時，才能明確以完整原請求重試。",
    "SUBMISSION_OBSERVATION_INVALID": "原接單觀察與保存的 RID／完整內容不符；不會重送或換 ID。",
    "WORK_SUBMIT_UNKNOWN": "原接單仍無法核清；保留完整原請求，不重送或換 ID。",
    "DELEGATION_RESPONSE_INVALID": "子題回應與原父回合或保存格式不符；不顯示為已保存或已取消。",
    "DELEGATION_UNKNOWN": "原子題結果仍未確認；只讀取原紀錄，不重新委派。",
    "DELEGATION_UNAVAILABLE": "目前無法讀取原子題狀態；不當成沒有委派，也不改用 Agent 私有入口。",
    "DELEGATION_DISABLED": "原父回合沒有捕獲委派能力；不改用目前的新對話設定。",
    "DELEGATION_SESSION_READ_ONLY": "原委派 session 設定無法核對；未派送新工作。",
    "DELEGATION_PARENT_REQUIRED": "無法核對原委派的父工作；不查其他回合。",
    "DELEGATION_SCOPE_CHANGED": "子題不是這筆原父工作或執行；不顯示為本回合結果。",
    "DELEGATION_RECORD_INVALID": "原委派回條無法核對；原資料保留，不重新委派。",
    "DELEGATION_RESULT_INVALID": "原子題完整結果無法核對；不宣稱已保存結果。",
    "CONVERSATION_TRANSFER_NOT_READY": "原版本失敗尚未完成轉交，或不是可接續的版本失敗；請先更新原進度。",
    "CONVERSATION_TRANSFER_SCOPE": "原人員、Team、助理或前文範圍不符，不能移轉；可另開新對話。",
    "CONVERSATION_TRANSFER_ARCHIVE_INVALID": "原前文的保存資料缺件或格式／設定不相容，不能空白恢復；請開新對話。",
    "CONVERSATION_TRANSFER_UNAVAILABLE": "目前無法準備原對話的新環境；未代為傳送訊息。",
    "CONVERSATION_TRANSFER_UNKNOWN": "原新環境結果尚未確認；只能核對同一個環境，不能重建或換 ID。",
    "CONVERSATION_TRANSFER_CHANGED": "原對話、保存前文或顯示的目標版本已變更；請重新檢視，未改選其他環境。",
    "CONVERSATION_TRANSFER_CONFLICT": "這筆失敗回合已有原準備請求；不能換 ID 再建立環境。",
    "CONVERSATION_TRANSFER_INVALID": "原轉移紀錄或回條不符；不會替換資料或傳送訊息。",
    "CONVERSATION_TRANSFER_BUDGET": "目前工作額度不允許準備新環境；原對話與回條保持。",
    "CANCELLATION_INVALID": "停止請求的回條未通過原工作核對；回應仍未確認，不會重送或清除原資料。",
    "CANCELLATION_ALREADY_ATTEMPTED": "停止請求已有原紀錄；只更新原工作狀態，不會重新送出。",
    "CANCELLATION_NOT_NEEDED": "後端已回報這筆工作結束，未再送出停止請求；原結果與回條保留。",
    "CANCELLATION_ORIGINAL_REQUIRED": "只有保留原停止請求且回應未確認時，才提供這個唯讀核對。",
    "CANCELLATION_ORIGINAL_INVALID": "原停止意圖的工作、世代或內容不符；未查其他請求，也不會重送。",
    "CANCELLATION_RECEIPT_UNKNOWN": "仍未取得原取消回條；這不代表未執行，也不允許重送。",
    "CANCELLATION_RECEIPT_CHANGED": "原取消的世代或請求已無法配對；保留 UNKNOWN，不改查新的請求。",
    "CANCELLATION_RECEIPT_INVALID": "原取消世代不符合支援範圍，未送出任何取消。",
    "CANCELLATION_RECEIPT_UNAVAILABLE": "目前後端尚未提供原取消回條核對；原 UNKNOWN 保留，不會重送。",
    "TEMPLATE_UNAVAILABLE": "目前部署尚未提供模板參照，或來源已變更；請重新讀取。沒有套用任何設定。",
    "TEMPLATE_INVALID": "請選擇有效且不超過 8192 bytes 的 FA 模板 JSON；不會安裝或變更任何設定。",
    "TEMPLATE_RESPONSE_INVALID": "模板回應未通過格式或來源核對；沒有套用設定、啟動工作或取得新權限。",
    "SYSTEM_PROMPT_UNAVAILABLE": "後端還沒有提供系統提示查看功能（API 版本較舊）；沒有改用其他來源顯示。",
    "SYSTEM_PROMPT_RESPONSE_INVALID": "系統提示回應的長度或 SHA-256 核對不符；不顯示這份內容。",
    "SYSTEM_PROMPT_UNRECONSTRUCTABLE": "後端無法用目前程式的版本文字重組這份系統提示；不拿其他版本代替。",
    "AGENT_TEMPLATE_PROFILE_UNSUPPORTED": "這位助理還沒有可查的提示版本設定；不以其他版本代替。",
    "AGENT_ROLE_PROMPT_INVALID": "保存的角色說明無法通過核對；不顯示也不改用其他版本。",
    "AGENT_ROLE_PROMPT_MISSING": "這位助理的角色版本需要角色說明，但還沒有保存；沒有改用其他版本。",
    "CAPABILITY_SOURCES_CHANGED": "選定的能力已不在目前的組織清單；請重新讀取設定。",
    "TEMPLATE_SELECTION_UNAVAILABLE": "目前部署尚未提供新對話設定選擇或原回條 reader；原紀錄保留，不會改用其他入口或啟用 flags。",
    "TEMPLATE_SELECTION_INVALID": "新對話選擇請求不符合原 command 或完整模板的容量／格式；未送出。",
    "TEMPLATE_SELECTION_RESPONSE_INVALID": "新對話選擇回應未通過原請求、模板或回條核對；保持未確認，不重送。",
    "TEMPLATE_SELECTION_ORIGINAL_REQUIRED": "找不到本機保存的原選擇請求；不會猜 ID 或另造 command 核對。",
    "TEMPLATE_SELECTION_ORIGINAL_INVALID": "保存的原選擇請求或 hash 不符；保留原紀錄，不重建或重送。",
    "TEMPLATE_SELECTION_ALREADY_ATTEMPTED": "同一選擇意圖已有原紀錄；只能核對原回條，不會換 ID 重送。",
    "PROFILE_SELECTION_UNSUPPORTED": "後端不接受這份模板的 profile、角色 hash 或 checkpoint／SDK-start 相容設定；沒有新增操作權限。",
    "PROFILE_SELECTION_DISABLED": "後端尚未接通原新對話選擇權威；不會自動啟用。",
    "PROFILE_SELECTION_DENIED": "後端拒絕目前 human／Team 的設定選擇；模板預覽不是授權。",
    "PROFILE_SELECTION_CONFLICT": "這位助理的新對話設定剛被改過，這次沒有套用；後端在寫入前就拒絕了。請重新讀取最新設定，確認後再選。",
    "PROFILE_SELECTION_SOURCES_CHANGED": "目的 Team 的已批准技能版本／hash 已變更；原選擇未確認，不會替換參照。",
    "PROFILE_SELECTION_MISSING": "找不到原選擇版本；舊對話與原紀錄保持，不以目前設定補件。",
    "PROFILE_SELECTION_INVALID": "後端保留的選擇或回條無法核對；原請求不會被改寫。",
    "PROFILE_SELECTION_UNKNOWN": "原選擇仍未確認；只能用保存的完整原 command 核對，不換 ID 或重送。",
    "TURN_CONTEXT_INVALID": "本回合 Context 明細未通過來源或格式核對；原回覆與回條保留，不會啟動模型或確認收件。",
    "CONVERSATION_ARCHIVED": "這段對話已封存，請開始新的 session。",
    "CONVERSATION_SESSION_RESET_REQUIRED": "這是舊式對話，請明確 reset 或開始新的 session。",
    "CONVERSATION_SESSION_UNAVAILABLE": "原 Hosted session 無法使用；未自動替換，請選擇 reset 或 new session。",
    "CONVERSATION_SESSION_VERSION_CHANGED": "此對話的執行環境已被平台換成新版本，請開新對話；原歷史與未決紀錄保留，不會重送。",
    "CONVERSATION_TURN_UNRESOLVED": "原工作仍在執行或結果未明，不能直接 reset；請先完成、取消或核對。",
    "RESET_UNKNOWN": "原 reset 結果尚未核清，未重送；請更新原對話狀態。",
    "PREVIEW_ONLY": "本機預覽；尚未連接服務。",
    "LOGIN_REQUIRED": "請先讀取既有登入；不會自動開啟登入。",
    "LOGIN_EXPIRED": "登入已過期，請重新讀取既有登入；不會重新建立工作。",
    "IDENTITY_MISMATCH": "登入帳號或使用的應用程式不符；未送出。",
    "CLI_NOT_CONFIGURED": "尚未設定專用登入環境，請由管理者完成接線。",
    "SCOPE_EXPIRED": "這份舊短期許可尚未生效或已到期；不能直接重新啟用。",
    "BACKEND_OFFLINE": "後端無法連線或回應中斷；已送出的請求結果未知，不會自動重送。",
    "BACKEND_UNAVAILABLE": "後端未能完成請求；結果未確認，不會自動重送。",
    "BACKEND_DENIED": "後端拒絕：可能許可過期、撤銷或身份不符；不能當作沒有結果。",
    "PROGRESS_READ_BUSY": "這段對話的上一次讀取還沒回來；稍後自動再讀，不會重送訊息。",
    "BACKEND_LIMIT": "後端有限配額已達上限；不會自動重試或退額。",
    "NO_RESULT": "尚無可取得的成果；不是成功，也不是已收件。",
    "RESULT_UNKNOWN": "原請求結果仍未知；請查原工作狀態，不要重送。",
    "RESULT_INVALID": "結果未通過完整性檢查；不顯示，也不送收件確認。",
    "RESULT_BINDING_MISMATCH": "結果不屬於這筆工作或指定收件人；已拒絕。",
    "STATUS_INVALID": "後端狀態格式未通過核對；不能當作完成。",
    "STATUS_BINDING_MISMATCH": "狀態不是指定的原 job／work；已拒絕。",
    "RESPONSE_LIMIT": "後端回應超過有限長度或時間；結果未確認。",
    "ACK_UNCONFIRMED": "後端尚未確認收件；不能顯示已收到。",
    "CHALLENGE_LOST": "本機已失去原收件資料，不能重取或補送；請由管理者核對原回條。",
    "RESULT_NOT_LOADED": "先查看並核對結果，再確認收到。",
    "RECONCILE_REQUIRED": "原請求已記錄，不會重送；請查原工作狀態，必要時交管理者核對。",
    "ALREADY_SUBMITTED": "原固定測試已送出並獲回應；不再送出。可手動純讀狀態。",
    "ALREADY_FETCHED": "已驗結果保留於本次記憶體；不再 fetch，不消耗額外取得配額。",
    "ALREADY_ACKNOWLEDGED": "原 backend 已確認收件；不需再次 ACK。",
    "ACTION_NOT_ALLOWED": "此項目不提供這個操作。",
    "RETAINED_BACKEND_NOT_VERIFIED": "尚未提供已核對的原轉交資料；此結果的讀取保持停用。",
    "CONFIG_CHANGED": "本機 journal 已綁不同配置；禁止覆寫或用新配置解除原未知 intent。",
    "LOCAL_REQUEST_DENIED": "請由此 localhost 頁面操作；跨來源或不符的請求已拒絕。",
    "REMOTE_PRINCIPAL_DENIED": "只有已指派的登入帳號可以使用這個遠端工作台；請改用指定帳號登入。",
    "REMOTE_SESSION_CHANGED": "遠端工作台已重新啟動，沒有重送任何操作。頁面沒有自動重新整理時（例如還有未送出的內容），請先複製內容，再重新整理頁面。",
    "REMOTE_STATE_UNAVAILABLE": "遠端工作台暫時無法保存紀錄；沒有自動重送。請重新整理頁面，依原紀錄核對後再操作。",
    "REMOTE_STATE_INVALID": "遠端保存的紀錄格式不符；未載入、未覆寫，也沒有送出新工作。",
    "REMOTE_STATE_FULL": "遠端保存的紀錄已達容量上限；這次的變更沒有保存到遠端，也不會自動重送，已保存的紀錄不受影響。請交管理者處理後再操作。",
    "REMOTE_CONFIGURATION_INVALID": "遠端工作台的設定與登入者或保存位置不符；未啟動。",
    "LOCAL_FAILURE": "本機處理失敗；結果未確認，請保留 journal 核對，不重送。",
    "WORK_POLICY_UNAVAILABLE": "暫時無法核對這筆工作的權限與預算；不會送出，其他工作與讀取不受此項影響。",
    "WORK_POLICY_INVALID": "工作權限回應與原工作不符；未送出。",
    "HANDOFF_EXPIRED": "這筆歷史結果的轉交許可已到期；仍可查狀態，不會重新取得或確認收件。",
    "ORIGINAL_ACK_REQUIRED": "只有原收件確認已送出且回應未知時，才能核對原回條。",
    "ORIGINAL_ACK_INVALID": "原收件意圖與這筆工作不符或資料不完整；不能另造請求核對。",
    "ALREADY_RECONCILED": "已核對原回條，未重送收件確認。",
    "RECONCILE_OUTCOME_UNKNOWN": "這次核對的回應仍未知；不再送出。保留原紀錄，交管理者核對。",
    "INSTRUCTION_INVALID": "請輸入非空白且在顯示的傳輸容量內的指令；不會替你刪字或截斷。",
    "INSTRUCTION_SCOPE": "這不是本機保存的原交辦；未送出。",
    "INSTRUCTION_CHANGED": "同一則交辦的原文或請求已改變；不能用原 ID 送不同內容。",
    "INSTRUCTION_INCOMPLETE": "本機原交辦缺少完整 command；不能猜測附件或對話、補造原件或重送。請保留 journal 核對。",
    "ATTACHMENT_CATALOG_UNAVAILABLE": "目前無法讀取這段對話的附件來源目錄；可能尚未發布或原對話暫不可用。不改查新對話來源。",
    "ATTACHMENT_CATALOG_INVALID": "附件目錄的格式或原對話／草稿不符；未替換選擇，也未取得讀取權限。",
    "ATTACHMENT_SELECTION_INVALID": "附件需為最多4份完整來源參照及不重複的 logical filename；名稱最多128個英數字、點、底線或連字號，不接受路徑或URL。",
    "USER_FILE_CAPABILITY_INVALID": "原文字檔能力回應缺欄、格式或對話不符；不當成停用或改查目前新對話設定。",
    "USER_FILE_CAPABILITY_UNAVAILABLE": "原對話的文字檔能力目前無法核對；不改查global設定，也不自動啟用。",
    "USER_FILE_INPUT_INVALID": "只接受小型UTF-8 txt／md／csv／json文字檔；檔名不可是路徑或URL，總附件最多4份且不重名，原文加檔案JSON最多8192 bytes。",
    "USER_FILE_UNAVAILABLE": "找不到這筆原交辦的文字檔，或後端尚未提供原件讀取；不以空內容代替。",
    "USER_FILE_RESPONSE_INVALID": "原件bytes、hash或header核對失敗；沒有產生下載檔，不會用本機快照繞過讀權。",
    "INSTRUCTION_PENDING": "原對話仍有未確認或未完成的交辦；請先更新進度或核對原接單，不會換 ID 跳過。",
    "CONVERSATION_INVALID": "對話回應不符合原交辦紀錄；不拼造進度或回覆。",
    "MODELS_INVALID": "服務提供的模型清單不完整或不符合介面；沒有自行選模型。",
    "MODEL_NOT_AVAILABLE": "請從原專案目前提供的部署選擇模型；不會建立新部署或換帳號。",
}


class Journal:
    # Remote mode only: called after a commit that changed rows; raising stops the caller before its next POST.
    after_commit = None
    # Remote mode only: called after a new Agent's connection file is written; same rule as after_commit.
    after_file = None

    @staticmethod
    def check_existing(path, config):
        original = Path(path)
        require(not any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
                        for p in (original, *original.parents)), "CONFIG_CHANGED")
        if not original.exists():
            return
        require(original.is_file(), "CONFIG_CHANGED")
        try:
            if original.stat().st_size == 0:
                return
            with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                saved = db.execute("SELECT sha FROM config WHERE id=1").fetchone()
                require(saved is not None and saved[0] == config.sha256, "CONFIG_CHANGED")
        except (OSError, sqlite3.Error):
            raise ConsoleError("CONFIG_CHANGED") from None

    def __init__(self, path, config):
        self.check_existing(path, config)
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), sha TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS intents (
                task_id TEXT NOT NULL, phase TEXT NOT NULL, intent TEXT NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY(task_id, phase))""")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (config.sha256,))
            require(db.execute("SELECT sha FROM config WHERE id=1").fetchone()[0] == config.sha256, "CONFIG_CHANGED")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
            if Journal.after_commit is not None and db.total_changes:
                Journal.after_commit(self.path)
        finally:
            db.close()

    def get(self, task, phase):
        with self.connect() as db:
            row = db.execute("SELECT intent, outcome FROM intents WHERE task_id=? AND phase=?", (task, phase)).fetchone()
        return None if row is None else {"intent": json.loads(row[0]), **json.loads(row[1])}

    def entries(self, phase):
        with self.connect() as db:
            rows = db.execute(
                "SELECT task_id, intent, outcome FROM intents WHERE phase=? ORDER BY rowid DESC", (phase,),
            ).fetchall()
        return [(key, {"intent": json.loads(intent), **json.loads(outcome)}) for key, intent, outcome in rows]

    def begin(self, task, phase, intent):
        outcome = {"state": "UNKNOWN", "code": "RECONCILE_REQUIRED", "observed_at": intent["created_at"]}
        try:
            with self.connect() as db:
                db.execute("INSERT INTO intents VALUES (?, ?, ?, ?)",
                           (task, phase, canonical(intent).decode(), canonical(outcome).decode()))
        except sqlite3.IntegrityError:
            raise ConsoleError("RECONCILE_REQUIRED") from None

    def finish(self, task, phase, value, *, keep_confirmed=False):
        with self.connect() as db:
            if keep_confirmed:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT outcome FROM intents WHERE task_id=? AND phase=?", (task, phase)).fetchone()
                require(row is not None, "LOCAL_FAILURE", 500)
                if strict_json(row[0])["state"] == "CONFIRMED":
                    return
            require(db.execute("UPDATE intents SET outcome=? WHERE task_id=? AND phase=?",
                (canonical(value).decode(), task, phase)).rowcount == 1, "LOCAL_FAILURE", 500)


class Console:
    def __init__(self, config, journal_path, *, vault=None, token_provider=None, backend=None,
                 clock=now_utc, environment="NOT_RUN",
                 instruction_connection: InstructionConnection | None = None, api_prefix=""):
        require(config is None or type(config) in {ConsoleConfig, SteadyConsoleConfig, StandingConsoleConfig}, "CONFIG_CHANGED")
        self.config, self.clock, self.environment = config, clock, environment
        self.api_prefix = api_prefix
        self.lock = RLock()
        self.message_locks, self.locks_guard = {}, RLock()
        self.vault = None if config is None else vault or TokenVault(config, clock=clock)
        self.token_provider = None if config is None else token_provider or CliTokenProvider(config)
        self.authentication_refresh_at = None
        self.authentication_refresh_failed = False
        self.journal = None if config is None else Journal(journal_path, config)
        # Warm turns are about 12 s apart; a 5 s keep-alive re-did TCP+TLS before each send (dev: 317 vs 74 ms).
        # Container Apps ingress keeps an idle request 4 minutes by default (premium-ingress, request-idle-limit).
        # httpx ignores the client's `limits` when a transport is given, so they sit on the transport; the pool size is
        # the one in effect so far (httpx defaults), only the keep-alive changes.
        self.backend = backend or httpx.Client(transport=httpx.HTTPTransport(retries=0, limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=120)), trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(90, connect=10))
        self.owns_backend = backend is None
        self.adapter = None if config is None else PullAdapter(config, self.backend, api_prefix=api_prefix)
        self.failure_adapter = None if config is None else OriginalFailureAdapter(config, self.backend, api_prefix=api_prefix)
        self.groups = None if config is None else GroupControl(self)
        self.results, self.statuses, self.errors = {}, {}, {}
        self.instruction_observations = {}
        self.instruction_observation_errors = {}
        self.observed_here = set()
        self.instructions = None
        self.native_instructions = None
        if instruction_connection is not None:
            from .freeform import InstructionControl
            self.instructions = InstructionControl(self, instruction_connection)
            self.native_instructions = NativeInstructionControl(self, self.instructions)

    def native_thread(self, thread_id=None):
        return self.native_instructions is not None and self.native_instructions.handles(thread_id)

    def native_request(self, request_id):
        return None if self.native_instructions is None else self.native_instructions.thread_for(request_id)

    def configured_tasks(self):
        return [] if self.config is None else [task for task in self.config.tasks if task.mode != "normal_pull"]

    def tasks(self):
        return [] if self.config is None else self.configured_tasks() + (
            [] if self.instructions is None else self.instructions.tasks())

    def instruction_task(self, task):
        return self.instructions is not None and self.instructions.store.command(task.task_id) is not None

    def message_lock(self, key):
        with self.locks_guard:
            return self.message_locks.setdefault(key, RLock())

    @contextmanager
    def progress_lock(self, thread_id):
        lock = self.message_lock("progress:" + thread_id)
        if not lock.acquire(timeout=PROGRESS_LOCK_WAIT_SECONDS):
            raise ConsoleError("PROGRESS_READ_BUSY", 409, automatic_retry=True)
        try:
            yield
        finally:
            lock.release()

    def operation_lock(self, task_id):
        if self.instructions is not None and self.instructions.store.command(task_id) is not None:
            return self.message_lock(task_id)
        return self.lock

    def task(self, task_id):
        require(self.config is not None, "PREVIEW_ONLY")
        require(self.native_request(task_id) is None, "ACTION_NOT_ALLOWED", 403)
        if self.instructions is not None and self.instructions.store.command(task_id) is not None:
            return self.instructions.task(task_id)
        task = next((v for v in self.config.tasks if v.task_id == task_id), None)
        require(task is not None and task.mode != "normal_pull", "ACTION_NOT_ALLOWED", 403)
        return task

    def active(self):
        require(self.config is not None, "PREVIEW_ONLY")
        if type(self.config) is ConsoleConfig:
            require(self.config.not_before <= self.clock() < self.config.expires_at, "SCOPE_EXPIRED", 403)

    def gate(self, task, phase):
        self.active()
        permission = phase
        if phase in {"read-reply", "read-context", "read-delegation", "read-agent-messages",
                     "confirm-reply", "cancel", "read-cancel-receipt", "read-files"}:
            require(self.instruction_task(task), "ACTION_NOT_ALLOWED", 403)
            permission = "acknowledge" if phase == "confirm-reply" else "status"
        if phase in {"submit-reconcile","submit-retry"}:
            require(self.instruction_task(task), "ACTION_NOT_ALLOWED", 403)
            permission = "submit" if phase=="submit-retry" else "status"
        if phase == "reconcile":
            require(task.mode == "original_failure", "ACTION_NOT_ALLOWED", 403)
            permission = "acknowledge"
        if task.mode == "original_failure":
            self.failure_adapter.require_enabled(task)
            require(phase in {"status", "fetch", "acknowledge", "reconcile"}, "ACTION_NOT_ALLOWED", 403)
        require(permission in task.allowed_actions, "ACTION_NOT_ALLOWED", 403)
        token = self.vault.get()
        return token

    def adapter_for(self, task):
        if self.instruction_task(task):
            return self.instructions.adapter
        return self.failure_adapter if task.mode == "original_failure" else self.adapter

    def refresh_instruction_work(self, task, token):
        require(self.instruction_task(task), "ACTION_NOT_ALLOWED", 403)
        self.instruction_observations.pop(task.task_id, None)
        self.instruction_observation_errors[task.task_id] = "WORK_POLICY_UNAVAILABLE"
        query = self.instructions.query(task)
        try:
            observation = self.instructions.reader.read(query, SecretStr(token))
            observation = checked_observation(observation, query, self.clock(), instruction=True)
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            raise ConsoleError("WORK_POLICY_UNAVAILABLE", 503) from None
        self.instruction_observations[task.task_id] = observation
        self.instruction_observation_errors.pop(task.task_id, None)
        return observation

    def instruction_execution_view(self, task):
        observation = self.instruction_observations.get(task.task_id)
        if observation is None:
            code = self.instruction_observation_errors.get(task.task_id)
            if code is not None:
                return {"connection": "AWAITING_OBSERVATION", "blocking_code": code,
                        "message": MESSAGES.get(code, MESSAGES["WORK_POLICY_UNAVAILABLE"]), "observation": None}
            return None
        view = {"connection": "OBSERVED", "blocking_code": None, "message": "",
                "observation": {
                    "decision": observation.decision, "reason": observation.reason, "revision": observation.revision,
                    "observed_at": observation.observed_at.isoformat(),
                    "execution_deadline": (None if observation.execution_deadline is None
                                           else observation.execution_deadline.isoformat()),
                    "budget": observation.budget.model_dump(mode="json"),
                    "not_current_live_permission": True, "rechecked_before_submit": False}}
        if type(observation) is StandingWorkObservation:
            p = observation.preflight
            view["observation"]["standing"] = {
                "already_admitted": p.already_admitted, "capacity_available": p.capacity_available,
                "queue_deadline": None if p.queue_deadline is None else p.queue_deadline.isoformat(),
                "guard_revision": p.guard_revision, "reason": p.reason,
                "monetary_budget": p.monetary_budget.model_dump(mode="json"),
                "abstract_attempts_remaining": p.abstract_attempts_remaining,
                "abstract_units_remaining": p.abstract_units_remaining,
            }
        return view

    def phase(self, task, name):
        value = self.journal.get(task.task_id, name)
        if value is None:
            return {"state": "NOT_SENT"}
        if name == "submit" and self.instruction_task(task) and value["intent"].get("body", {}).get("files"):
            value = {**value, "intent": {**value["intent"],
                "body": {key: item for key, item in value["intent"]["body"].items() if key != "files"},
                "body_files_omitted": True, "user_files": self.instructions.user_file_metadata(task.task_id)}}
        return {**value, "historical": (task.task_id, name) not in self.observed_here,
                "not_current_live_result": True}

    def task_view(self, task):
        actions = {}
        result = self.results.get(task.task_id)
        for action in ("submit", "status", "fetch", "acknowledge", "reconcile", "submit-reconcile", "submit-retry", "read-reply", "read-context", "read-delegation", "confirm-reply", "cancel", "read-cancel-receipt", "read-files"):
            try:
                self.gate(task, action)
                if action == "submit-reconcile":
                    pending = self.journal.get(task.task_id, "submit")
                    require(pending is not None and pending["state"] == "UNKNOWN"
                            and self.instructions.admission(task) is None, "INSTRUCTION_PENDING")
                if action == "submit-retry":
                    require(self.instructions.retry_observation(task) is not None,"SUBMISSION_RECONCILE_REQUIRED")
                if action == "read-reply":
                    progress = self.instructions.progress(task)
                    require(progress is not None and progress["data"]["reply_available"], "NO_RESULT")
                if action in {"read-context", "read-delegation"}:
                    require(self.instructions.admission(task) is not None, "INSTRUCTION_PENDING")
                if action == "read-files":
                    require(self.instructions.admission(task) is not None, "INSTRUCTION_PENDING")
                    require(bool(self.instructions.command(task.task_id).files), "USER_FILE_UNAVAILABLE")
                if action == "cancel":
                    self.instructions.require_cancellable(task)
                if action == "read-cancel-receipt":
                    self.instructions.cancellation_intent(task)
                if action == "confirm-reply":
                    progress = self.instructions.progress(task)
                    require(task.task_id in self.instructions.replies and progress is not None
                            and progress["data"]["delivery_id"] is not None, "NO_RESULT")
                    require(self.journal.get(task.task_id, "acknowledge") is None, "ALREADY_ACKNOWLEDGED")
                    require(result is not None or self.journal.get(task.task_id, "fetch") is None, "CHALLENGE_LOST")
                if action == "reconcile":
                    self.failure_adapter.original_ack(task, self.journal.get(task.task_id, "acknowledge"), self.clock())
                    prior = self.journal.get(task.task_id, action)
                    require(prior is None, "ALREADY_RECONCILED" if prior and prior["state"] == "CONFIRMED"
                            else "RECONCILE_OUTCOME_UNKNOWN")
                if task.mode == "original_failure" and action in {"fetch", "acknowledge", "reconcile"}:
                    observed = self.statuses.get(task.task_id)
                    require(observed is None or not observed["data"]["expired"], "HANDOFF_EXPIRED", 403)
                if action in {"submit", "fetch", "acknowledge"}:
                    prior = self.journal.get(task.task_id, action)
                    code = ({"submit": "ALREADY_SUBMITTED", "fetch": "ALREADY_FETCHED",
                             "acknowledge": "ALREADY_ACKNOWLEDGED"}[action]
                            if prior is not None and prior["state"] in {"CONFIRMED", "VERIFIED"}
                            else "RECONCILE_REQUIRED")
                    if action == "fetch" and prior is not None and result is None:
                        code = "CHALLENGE_LOST"
                    require(prior is None, code)
                if action == "acknowledge":
                    require(result is not None, "CHALLENGE_LOST" if self.journal.get(task.task_id, "fetch")
                            else "RESULT_NOT_LOADED")
                actions[action] = {"enabled": True, "reason": ""}
            except ConsoleError as error:
                actions[action] = {"enabled": False, "reason": MESSAGES[error.code]}
        return {
            "task_id": task.task_id, "label": task.label, "description": task.description, "mode": task.mode,
            "binding_id": task.binding_id, "job_id": task.job_id, "work_id": task.work_id,
            "submit_request_id": task.submit_request_id, "fetch_request_id": task.fetch_request_id,
            "ack_request_id": task.ack_request_id,
            "instruction": (self.instructions.command(task.task_id).content if self.instruction_task(task) else None),
            "thread_id": (self.instructions.store.get(task.task_id)["thread_id"] if self.instruction_task(task) else None),
            "requested_model": (self.instructions.command(task.task_id).model if self.instruction_task(task) else None),
            "attachments": ([] if not self.instruction_task(task) else
                            [item.model_dump(mode="json") for item in self.instructions.command(task.task_id).attachments]),
            "user_files": [] if not self.instruction_task(task) else self.instructions.user_file_metadata(task.task_id),
            "admission": (None if not self.instruction_task(task) or self.instructions.admission(task) is None else
                          self.instructions.admission(task).model_dump(mode="json")),
            "progress": None if not self.instruction_task(task) else self.instructions.progress(task),
            "reply": None if not self.instruction_task(task) else self.instructions.replies.get(task.task_id),
            "status": self.statuses.get(task.task_id), "result": None if result is None else result.public(),
            "submission": self.phase(task, "submit"), "fetch": self.phase(task, "fetch"),
            "cancellation": self.phase(task, "cancel") if self.instruction_task(task) else None,
            "acknowledgment": self.phase(task, "acknowledge"), "actions": actions,
            "reconciliation": self.phase(task, "reconcile"),
            "execution": self.instruction_execution_view(task) if self.instruction_task(task) else None,
            "error": self.errors.get(task.task_id),
        }

    def state(self, thread_id=None):
        with self.lock:
            reason = None
            try:
                self.active()
            except ConsoleError as error:
                reason = MESSAGES[error.code]
            native = self.native_thread(thread_id) if self.instructions is not None else False
            composer = (None if self.instructions is None else
                        self.native_instructions.view(thread_id) if native else self.instructions.view(thread_id))
            tasks=(self.tasks() if composer is None else self.configured_tasks() if native else
                   [*self.configured_tasks(),*self.instructions.tasks(composer["thread_id"])])
            selection_error = None
            try:
                selections = self.template_selection_history()
            except ConsoleError as error:
                selections = []
                selection_error = {"code": error.code, "message": MESSAGES[error.code], "automatic_retry": False}
            authentication = self.authentication()
            if reason:
                authentication["authentication_refresh_at"] = None
            return {"mode": "PREVIEW_ONLY" if self.config is None else "FIXED_TESTS",
                "environment": self.environment, "reason": reason, "automatic_polling": self.instructions is not None,
                **authentication,
                "template_tools_available": type(self.config) is StandingConsoleConfig,
                "template_selections": selections, "template_selection_error": selection_error,
                "authorization_mode": ("backend-per-work" if isinstance(self.config, SteadyConsoleConfig) else "legacy-window"),
                "expires_at": self.config.expires_at.isoformat() if type(self.config) is ConsoleConfig else None,
                "scope_id": self.config.scope_id if type(self.config) is ConsoleConfig else None,
                "console_id": self.config.console_id if isinstance(self.config, SteadyConsoleConfig) else None,
                "backend_origin": None if self.config is None else self.config.backend_origin,
                "agents": ([] if self.instructions is None else [{
                    "id": self.config.standing_policy.template.agent_oid,
                    "name": "FA 助理", "description": "目前配置的工作助理",
                }]),
                "tasks": [self.task_view(t) for t in tasks] + (
                    self.native_instructions.views(composer["thread_id"]) if native else []),
                "composer": composer, "groups_available": self.groups is not None and self.groups.enabled
                and self.native_instructions is not None, "routines_available": routines_available(self)}

    def authentication(self):
        state = "LOGIN_REQUIRED" if self.vault is None else self.vault.state()
        return {"authentication": "LOGIN_EXPIRED" if self.authentication_refresh_failed else state,
                "authentication_refresh_at": None if state == "LOGIN_REQUIRED" else self.authentication_refresh_at}

    def authenticate(self, *, automatic=False, load_models=True):
        with self.lock:
            self.active()
            due = self.authentication()["authentication_refresh_at"]
            if automatic and (due is None or self.clock().timestamp() < due):
                return self.authentication()
            self.authentication_refresh_at = None
            self.authentication_refresh_failed = False
            if not automatic:
                self.vault.clear()
            # Publish an automatic replacement only after its claims pass the same checks.
            vault = TokenVault(self.config, clock=self.clock) if automatic else self.vault
            try:
                vault.set(self.token_provider())
            except ConsoleError:
                if not automatic:
                    raise
                self.vault.clear()
                self.authentication_refresh_failed = True
                raise ConsoleError("LOGIN_EXPIRED", 401) from None
            self.vault = vault
            self.authentication_refresh_at = vault.expires_at - 300
            if automatic and self.authentication_refresh_at <= self.clock().timestamp():
                self.authentication_refresh_at = vault.expires_at
            if automatic:
                return self.authentication()
            if load_models and self.instructions is not None:
                self.instructions.refresh_models(self.vault.get())
                self.native_instructions.refresh_routing(
                    self.vault.get(), thread_id=self.instructions.store.current()["thread_id"])
            return self.state()

    def run(self, task_id, action):
        with self.operation_lock(task_id):
            thread = self.native_request(task_id)
            if thread is not None:
                self.active()
                require(action in {"status", "submit-reconcile", "submit-retry"}, "ACTION_NOT_ALLOWED", 403)
                if action == "submit-retry":
                    self.native_instructions.resume_accepted(task_id, self.vault.get())
                elif action == "submit-reconcile" and self.native_instructions.profile(thread) == "foundry-responses-v1":
                    self.native_instructions.reconcile_response(task_id, self.vault.get())
                else:
                    self.native_instructions.refresh(thread, self.vault.get())
                return {"task": self.native_instructions.task_view(task_id, thread)}
            task = self.task(task_id)
            if action == "submit-reconcile":
                self.gate(task, action)
                self.instructions.reconcile(task)
                self.errors.pop(task_id,None)
                return {"task": self.task_view(task)}
            if action == "submit-retry":
                self.instructions.retry_submission(task)
                self.errors.pop(task_id,None)
                return {"task": self.task_view(task)}
            if action == "reconcile":
                return self.reconcile_ack(task)
            require(action in {"submit", "status", "fetch", "acknowledge"}, "ACTION_NOT_ALLOWED", 403)
            # Repeated clicks observe this client's original attempt, never re-POST.
            prior = None if action == "status" else self.journal.get(task_id, action)
            if prior is not None:
                if action == "fetch" and task_id not in self.results:
                    raise ConsoleError("CHALLENGE_LOST")
                return {"task": self.task_view(task)}
            token = self.gate(task, action)
            adapter = self.adapter_for(task)
            created = self.clock().isoformat()
            intent_started = False
            try:
                if action == "status":
                    if self.instruction_task(task):
                        try:
                            observation = self.refresh_instruction_work(task, token)
                            if type(observation) is StandingWorkObservation and not observation.preflight.already_admitted:
                                self.statuses[task_id] = {"data": None, "admission": "not_admitted",
                                    "observed_at": observation.observed_at.isoformat(), "not_current_live_result": True}
                                self.errors.pop(task_id, None)
                                return {"task": self.task_view(task)}
                        except ConsoleError as error:
                            code = error.code if error.code in MESSAGES else "WORK_POLICY_UNAVAILABLE"
                            self.instruction_observation_errors[task_id] = code
                    value = adapter.status(task, token)
                    self.statuses[task_id] = {"data": value.model_dump(mode="json"), "observed_at": created,
                                             "not_current_live_result": True}
                    self.errors.pop(task_id, None)
                    if self.instruction_task(task) and self.instructions.admission(task) is not None:
                        self.instructions.refresh_conversation(token, self.instructions.admission(task).conversation_id)
                    return {"task": self.task_view(task)}
                result = self.results.get(task_id)
                if action == "fetch":
                    status = adapter.status(task, token)
                    self.statuses[task_id] = {"data": status.model_dump(mode="json"), "observed_at": created,
                                             "not_current_live_result": True}
                    if task.mode == "original_failure":
                        require(not status.expired, "HANDOFF_EXPIRED", 403)
                    else:
                        require(status.result_forwarded and status.delivery_id is not None, "NO_RESULT")
                if action == "acknowledge":
                    require(result is not None, "CHALLENGE_LOST" if self.journal.get(task_id, "fetch")
                            else "RESULT_NOT_LOADED")
                    if task.mode == "original_failure":
                        current = adapter.status(task, token)
                        self.statuses[task_id] = {"data": current.model_dump(mode="json"), "observed_at": created,
                                                 "not_current_live_result": True}
                        require(not current.expired, "HANDOFF_EXPIRED", 403)
                    body = adapter.ack_body(task, result)
                    path = (adapter.paths(task)["acknowledge"] if task.mode == "original_failure" else
                            "/behavior/v1/bindings/"+task.binding_id+"/delivery/acknowledge")
                    method = "POST"
                else:
                    method, path, body = adapter.request(task, action)
                self.active()
                token = self.vault.get()
                intent = {"method": method, "path": path, "request_id": body["request_id"],
                          "body_sha256": sha(canonical(body)), "created_at": created,
                          "configuration_sha256": self.config.sha256}
                if isinstance(self.config, SteadyConsoleConfig):
                    intent.update(console_id=self.config.console_id, authorization_mode=self.config.authorization_mode)
                else:
                    intent["scope_id"] = self.config.scope_id
                if self.instruction_task(task):
                    intent["instruction_connection"] = self.instructions.connection.model_dump(mode="json")
                    observed = self.instructions.submission_context(task)
                    intent["instruction_context"] = {
                        "conversation_id": observed.conversation_id, "turn_number": observed.turn_number, "model": observed.model}
                if action != "acknowledge":
                    intent["body"] = body
                else:
                    intent.update(exchange_id=result.exchange_id, binding=result.binding.model_dump(mode="json"))
                self.journal.begin(task_id, action, intent)
                intent_started = True
                if action == "submit":
                    value = adapter.submit(task, token)
                    self.statuses[task_id] = {"data": value.model_dump(mode="json"), "observed_at": created,
                                             "not_current_live_result": True}
                    saved = {"state": "CONFIRMED", "observed_at": self.clock().isoformat()}
                    if self.instruction_task(task):
                        saved["admission"] = adapter.admissions[task.task_id].model_dump(mode="json")
                elif action == "fetch":
                    value = adapter.fetch(task, token, status.delivery_id)
                    self.results[task_id] = value
                    saved = {"state": "VERIFIED", "sha256": value.binding.payload_digest,
                             "observed_at": self.clock().isoformat()}
                else:
                    receipt = adapter.acknowledge(task, result, token)
                    saved = {"state": "CONFIRMED", "confirmation_level": "client_received", "receipt": receipt,
                             "payload_sha256": result.binding.payload_digest, "observed_at": self.clock().isoformat()}
                self.journal.finish(task_id, action, saved)
                self.observed_here.add((task_id, action))
                self.errors.pop(task_id, None)
                return {"task": self.task_view(task)}
            except ConsoleError as error:
                self.errors[task_id] = {"code": error.code, "message": MESSAGES[error.code],
                                       "observed_at": self.clock().isoformat(), "upstream_status": error.upstream_status}
                if intent_started:
                    self.journal.finish(task_id, action, {"state": "UNKNOWN", "code": error.code,
                        "observed_at": self.clock().isoformat(), "automatic_retry": False})
                raise

    def submit_instruction(self, request_id, content, model, attachments=(), files=(), images=()):
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        local = self.instructions.store.get(request_id)
        with self.message_lock(request_id):
            self.active()
            require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
            token = self.vault.get()
            self.instructions.validate_input(request_id, content, model, attachments, files, images=images)
            self.native_instructions.refresh_routing(token, thread_id=local["thread_id"])
            thread = self.native_request(request_id)
            if thread is not None:
                self.native_instructions.submit(request_id, content, model, attachments, files, token, images=images)
                return {"task": self.native_instructions.task_view(request_id, thread, self.instructions.store.get(request_id))}
            # Attached images are a native-lane input; the job-bound lane refuses them before any write.
            require(not images, "USER_IMAGES_UNSUPPORTED", 409)
            task = self.instructions.prepare(request_id, content, model, attachments, files)
            return self.run(task.task_id, "submit")

    def upload_instruction_image(self, request_id, name, png_base64):
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        local = self.instructions.store.get(request_id)
        self.active()
        token = self.vault.get()
        self.native_instructions.refresh_routing(token, thread_id=local["thread_id"])
        require(self.native_request(request_id) is not None, "USER_IMAGES_UNSUPPORTED", 409)
        return self.native_instructions.upload_image(request_id, name, png_base64, token)

    def instruction_attachment_sources(self, request_id):
        self.active()
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        if self.native_request(request_id) is not None:
            return self.native_instructions.attachment_sources(request_id, self.vault.get())
        return self.instructions.attachment_sources(request_id, self.vault.get())

    def append_instruction(self, request_id, append_id, content):
        with self.message_lock("append:" + request_id):
            self.active()
            require(self.native_request(request_id) is not None, "ACTION_NOT_ALLOWED", 403)
            value = self.native_instructions.appends.submit(request_id, append_id, content, self.vault.get())
            return {"append": value.model_dump(mode="json")}

    def read_instruction_appends(self, request_id):
        with self.message_lock("append-read:" + request_id):
            self.active()
            require(self.native_request(request_id) is not None, "ACTION_NOT_ALLOWED", 403)
            value = self.native_instructions.appends.refresh(request_id, self.vault.get())
            return {"appends": value.model_dump(mode="json")}

    def read_instruction_progress(self, request_id):
        with self.message_lock("activity-read:" + request_id):
            self.active()
            thread = self.native_request(request_id)
            require(thread is not None, "ACTION_NOT_ALLOWED", 403)
            self.native_instructions.read_progress(request_id, self.vault.get())
            task = self.native_instructions.task_view(request_id, thread)
            return {"request_id": request_id, "progress": task["progress"]}

    def instruction_file_capability(self, request_id):
        self.active()
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        if self.native_request(request_id) is not None:
            return self.native_instructions.text_file_capability(request_id, self.vault.get())
        return self.instructions.text_file_capability(request_id, self.vault.get())

    def download_instruction_file(self, request_id, filename):
        if self.native_request(request_id) is not None:
            self.active()
            return self.native_instructions.file(request_id, filename, self.vault.get())
        task = self.task(request_id)
        token = self.gate(task, "read-files")
        return self.instructions.read_original_user_file(task, filename, token)

    def instruction_skill_draft(self, request_id):
        self.active()
        require(self.native_request(request_id) is not None, "ACTION_NOT_ALLOWED", 403)
        return self.native_instructions.skill_draft(request_id, self.vault.get())

    def new_instruction(self, thread_id):
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        self.instructions.store.current(thread_id)
        with self.message_lock("thread:"+thread_id):
            self.active()
            require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
            if self.native_thread(thread_id):
                self.native_instructions.follow_up(thread_id, self.vault.get())
                return self.state(thread_id)
            self.instructions.follow_up(self.vault.get(), thread_id)
            return self.state(thread_id)

    def new_conversation(self):
        with self.lock:
            self.active()
            require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
            self.native_instructions.refresh_routing(self.vault.get())
            draft = self.instructions.store.new_thread()
            return self.state(draft["thread_id"])


    def reset_conversation(self,thread_id):
        from .freeform import ResetReply
        require(self.instructions is not None,"ACTION_NOT_ALLOWED",403)
        require(not self.native_thread(thread_id), "ACTION_NOT_ALLOWED", 403)
        with self.message_lock("thread:"+thread_id):
            self.active()
            token=self.vault.get()
            prior=self.journal.get(thread_id,"reset")
            if prior is not None and prior["state"]=="CONFIRMED":
                return self.state(prior["new_thread_id"])
            require(prior is None or prior["state"]=="REFUSED","RESET_UNKNOWN")
            cid=self.instructions.progress_conversation(thread_id)
            require(cid is not None,"ACTION_NOT_ALLOWED",403)
            projection=self.instructions.refresh_conversation(token,cid)
            require(projection.can_reset,"CONVERSATION_TURN_UNRESOLVED")
            request_id=sha(canonical(["reset",self.config.console_id,thread_id,cid]))
            body={"request_id":request_id}
            intent={"conversation_id":cid,"body":body,"created_at":self.clock().isoformat()}
            if prior is None:self.journal.begin(thread_id,"reset",intent)
            else:self.journal.finish(thread_id,"reset",{"state":"UNKNOWN","previous_refusal":prior["code"]})
            try:
                raw,_=self.instructions.adapter.send("POST","/work-policy/v1/conversations/"+cid+"/reset",body,token)
                reply=ResetReply.model_validate_json(raw)
                require(reply.conversation_id==cid and reply.reset_request_id==request_id
                        and reply.hosted_session_id==projection.hosted_session_id,"RESET_UNKNOWN")
                draft=self.instructions.store.complete_reset(thread_id,reply.model_dump(mode="json"))
                self.instructions.projections[cid]=projection.model_copy(update={
                    "archived":True,"can_reset":False,"can_follow_up":False,"follow_up_blocker":"CONVERSATION_ARCHIVED"})
            except ConsoleError as error:
                if error.code in {"CONVERSATION_TURN_UNRESOLVED","CONVERSATION_ARCHIVED"}:
                    self.journal.finish(thread_id,"reset",{"state":"REFUSED","code":error.code})
                raise
            except ValueError:
                raise ConsoleError("RESET_UNKNOWN",502) from None
            return {**self.state(draft["thread_id"]),"reset":reply.model_dump(mode="json")}

    def instruction_models(self, thread_id=None):
        with self.lock:
            self.active()
            require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
            self.native_instructions.refresh_received(self.vault.get(), thread_id=thread_id)
            thread_id = self.instructions.store.current(thread_id)["thread_id"]
            self.instructions.refresh_models(self.vault.get())
            self.native_instructions.refresh_routing(self.vault.get(), thread_id=thread_id)
            return self.state(thread_id)

    def instruction_progress(self, thread_id=None):
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        self.active()
        self.native_instructions.open_received(self.vault.get(), thread_id)
        thread_id = self.instructions.store.current(thread_id)["thread_id"]
        with self.progress_lock(thread_id):
            self.active()
            require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
            thread_id = self.instructions.store.current(thread_id)["thread_id"]
            if self.native_thread(thread_id):
                self.native_instructions.refresh(thread_id, self.vault.get())
            else:
                self.instructions.refresh_progress(self.vault.get(), thread_id)
            if self.instructions.models is None:
                # The first read failed or never ran (e.g. the API still starting after a redeploy): read it on first need.
                try:
                    self.instructions.refresh_models(self.vault.get())
                    self.native_instructions.refresh_routing(self.vault.get(), thread_id=thread_id)
                except ConsoleError:
                    pass  # the composer keeps 「尚無可用模型」 and the next poll reads again
            return self.state(thread_id)

    def instruction_received(self, request_id, thread_id=None):
        """A push hint named a conversation another Agent just opened here: import only that RID (one receipt
        read, as open_received does for a named thread) and return the page's own conversation unchanged."""
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        self.active()
        from shared_context.behavior_wire import Command
        try:
            Command(request_id=request_id)
        except ValueError:
            raise ConsoleError("INSTRUCTION_SCOPE") from None
        self.native_instructions.open_received(self.vault.get(), request_id)
        return self.state(thread_id)

    def instruction_status(self, request_id):
        """While the owner's turn runs: read only that turn and return the state, instead of the whole conversation."""
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        thread = self.native_request(request_id)
        require(thread is not None, "ACTION_NOT_ALLOWED", 403)
        with self.progress_lock(thread):
            self.active()
            self.native_instructions.turn_status(request_id, self.vault.get())
            return self.state(thread)

    def instruction_live_text(self, request_id, starting_after=None):
        """RTT B: the running turn's live text as bytes; read-only, so no message lock is held while it streams."""
        require(self.instructions is not None, "ACTION_NOT_ALLOWED", 403)
        require(self.native_request(request_id) is not None, "ACTION_NOT_ALLOWED", 403)
        self.active()
        return self.native_instructions.live_text(request_id, self.vault.get(), starting_after=starting_after)

    def instruction_platform_history(self, thread_id, *, after=None, previous=False):
        require(self.instructions is not None and self.native_thread(thread_id), "ACTION_NOT_ALLOWED", 403)
        with self.progress_lock(thread_id):
            self.active()
            self.native_instructions.read_platform_history(
                thread_id, self.vault.get(), after=after, previous=previous)
            return {"thread_id": thread_id, "platform_history": self.native_instructions.platform_history_view(thread_id)}

    def read_instruction_delegation(self, task_id):
        task = self.task(task_id)
        with self.message_lock("delegation:" + task_id):
            token = self.gate(task, "read-delegation")
            value = self.instructions.read_delegation(task, token)
            self.vault.get()
            admission = self.instructions.admission(task)
            return {"task_id": task_id, "job_id": task.job_id,
                    "conversation_id": admission.conversation_id, "turn_number": admission.turn_number,
                    "read_at": self.clock().isoformat(),
                    "delegation": None if value is None else value.model_dump(mode="json")}

    def template_token(self):
        self.active()
        require(type(self.config) is StandingConsoleConfig, "ACTION_NOT_ALLOWED", 403)
        return self.vault.get()

    def download_template(self):
        from .agent_template import download
        with self.message_lock("agent-template"):
            return download(self.adapter, self.template_token())

    def inspect_template(self, raw):
        from .agent_template import inspect
        with self.message_lock("agent-template"):
            return inspect(self.adapter, self.template_token(), raw)

    def template_options(self):
        from .agent_template import options
        with self.message_lock("agent-template"):
            return options(self.adapter, self.template_token())

    def template_system_prompt(self):
        from .agent_template import system_prompt
        with self.message_lock("agent-template"):
            value = system_prompt(self.adapter, self.template_token())
            return {"system_prompt": value.model_dump(mode="json"), "read_at": self.clock().isoformat()}

    def instruction_system_prompt(self, request_id):
        self.active()
        require(self.native_request(request_id) is not None, "ACTION_NOT_ALLOWED", 403)
        with self.message_lock(request_id):
            value = self.native_instructions.system_prompt(request_id, self.vault.get())
            return {"task_id": request_id, "system_prompt": value.model_dump(mode="json"),
                    "read_at": self.clock().isoformat()}

    def template_selection_history(self):
        if type(self.config) is not StandingConsoleConfig:
            return []
        from .agent_template import history
        try:
            return history(self.config, self.journal)
        except (KeyError, TypeError, ValueError):
            raise ConsoleError("TEMPLATE_SELECTION_ORIGINAL_INVALID", 502) from None

    def read_template_selection(self):
        from .agent_template import read_selection
        with self.message_lock("agent-template"):
            current = read_selection(self.adapter, self.template_token())
            return {"current": current.model_dump(mode="json"), "read_at": self.clock().isoformat(),
                    "selections": self.template_selection_history()}

    def select_template(self, command):
        from .agent_template import PATH, SELECTION_PHASE, intent_key, original, select, template_hash
        with self.message_lock("agent-template"):
            token = self.template_token()
            if command.template.settings.state_profile == "foundry-responses-v1":
                from .agent_template import options
                require(options(self.adapter, token).responses.enabled, "NATIVE_TURNS_DISABLED", 503)
            key = intent_key(command)
            prior = self.journal.get(key, SELECTION_PHASE)
            if prior is not None:
                original(self.config, key, prior)
                raise ConsoleError("TEMPLATE_SELECTION_ALREADY_ATTEMPTED")
            require(not any(item["request_id"] == command.request_id for item in self.template_selection_history()),
                    "TEMPLATE_SELECTION_ALREADY_ATTEMPTED")
            body = command.model_dump(mode="json")
            self.journal.begin(key, SELECTION_PHASE, {
                "method": "POST", "path": PATH + "/select", "config_sha256": self.config.sha256,
                "body": body, "body_sha256": sha(canonical(body)),
                "template_sha256": template_hash(command.template), "created_at": self.clock().isoformat(),
            })
            try:
                receipt = select(self.adapter, token, command)
                self.journal.finish(key, SELECTION_PHASE, {
                    "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                    "observed_at": self.clock().isoformat(), "automatic_retry": False,
                })
            except ConsoleError as error:
                from .agent_template import rejected_before_write
                self.journal.finish(key, SELECTION_PHASE, {
                    "state": "REJECTED" if rejected_before_write(error.code, error.upstream_status) else "UNKNOWN",
                    "code": error.code, "upstream_status": error.upstream_status,
                    "observed_at": self.clock().isoformat(), "automatic_retry": False,
                })
                raise
            return {"receipt": receipt.model_dump(mode="json"), "selections": self.template_selection_history(),
                    "request_id": command.request_id, "original_request_resent": False}

    def reconcile_template_selection(self, request_id):
        from .agent_template import PATH, SELECTION_PHASE, RECONCILE_PHASE, original, select
        with self.message_lock("agent-template"):
            token = self.template_token()
            retained = [(key, entry, original(self.config, key, entry))
                        for key, entry in self.journal.entries(SELECTION_PHASE)]
            found = [item for item in retained if item[2].request_id == request_id]
            require(len(found) == 1, "TEMPLATE_SELECTION_ORIGINAL_REQUIRED")
            key, entry, command = found[0]
            prior = self.journal.get(key, RECONCILE_PHASE)
            if prior is None:
                self.journal.begin(key, RECONCILE_PHASE, {
                    "method": "POST", "path": PATH + "/selection-reconcile",
                    "original_body_sha256": entry["intent"]["body_sha256"], "created_at": self.clock().isoformat(),
                })
            else:
                require(prior["intent"]["method"] == "POST"
                        and prior["intent"]["path"] == PATH + "/selection-reconcile"
                        and prior["intent"]["original_body_sha256"] == entry["intent"]["body_sha256"],
                        "TEMPLATE_SELECTION_ORIGINAL_INVALID", 502)
            try:
                receipt = select(self.adapter, token, command, reconcile=True)
                self.journal.finish(key, RECONCILE_PHASE, {
                    "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                    "observed_at": self.clock().isoformat(), "original_request_resent": False,
                })
            except ConsoleError as error:
                self.journal.finish(key, RECONCILE_PHASE, {
                    "state": "UNKNOWN", "code": error.code, "observed_at": self.clock().isoformat(),
                    "original_request_resent": False,
                }, keep_confirmed=True)
                raise
            return {"receipt": receipt.model_dump(mode="json"), "selections": self.template_selection_history(),
                    "request_id": command.request_id, "original_request_resent": False}

    def read_instruction_context(self, task_id):
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-context")
            value = self.instructions.read_turn_context(task, token)
            return {"task_id": task_id, "context": value.model_dump(mode="json")}

    def cancel_instruction(self, task_id):
        thread = self.native_request(task_id)
        if thread is not None:
            with self.message_lock(task_id):
                self.active()
                self.native_instructions.change(task_id, "cancel", self.vault.get())
                return {"task": self.native_instructions.task_view(task_id, thread)}
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "cancel")
            self.instructions.cancel(task, token)
            return {"task": self.task_view(task)}

    def read_cancellation_receipt(self, task_id):
        thread = self.native_request(task_id)
        if thread is not None:
            with self.message_lock(task_id):
                self.active()
                self.native_instructions.refresh(thread, self.vault.get())
                value = self.native_instructions.receipts[task_id]
                return {"schema_version": "fa-native-cancellation-observation-v1", "task_id": task_id,
                        "conversation_id": value.conversation_id, "status": value.status,
                        "cancel_requested": value.cancel_requested}
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-cancel-receipt")
            return self.instructions.read_cancellation(task, token)

    def read_instruction_reply(self, task_id):
        thread = self.native_request(task_id)
        if thread is not None:
            with self.message_lock(task_id):
                self.active()
                self.native_instructions.download(task_id, self.vault.get())
                return self.state(thread)
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-reply")
            self.instructions.reread_reply(task, token)
            return self.state(self.instructions.store.get(task_id)["thread_id"])

    def download_instruction_reply(self, task_id):
        if self.native_request(task_id) is not None:
            with self.message_lock(task_id):
                self.active()
                return self.native_instructions.download(task_id, self.vault.get())
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-reply")
            reply = self.instructions.reread_reply(task, token)
            return reply.text.encode("utf-8"), reply.turn_number

    def _instruction_images(self, task, token):
        from shared_context.assistant_image_wire import TurnImages
        raw, _ = self.instructions.adapter.send("GET", f"/behavior/v1/bindings/{task.binding_id}/images", None, token)
        try:
            strict_json(raw)
            value = TurnImages.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("RESULT_INVALID", 502) from None
        require(value.binding_id == task.binding_id and value.job_id == task.job_id, "RESULT_INVALID", 502)
        return value

    def instruction_images(self, task_id):
        """Screenshots the assistant delivered with this reply; metadata only, read with the human's own token."""
        if self.native_request(task_id) is not None:
            with self.message_lock(task_id):
                self.active()
                return self.native_instructions.images(task_id, self.vault.get())
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-reply")
            return self._instruction_images(task, token)

    def instruction_image(self, task_id, version_id):
        from shared_context.assistant_image_wire import MAX_IMAGE_BYTES
        from shared_context.m3_models import PNG_SIGNATURE
        require(len(version_id) == 32 and set(version_id) <= set("0123456789abcdef"), "LOCAL_REQUEST_DENIED", 403)
        if self.native_request(task_id) is not None:
            with self.message_lock(task_id):
                self.active()
                value = self.native_instructions.images(task_id, self.vault.get(), version_id=version_id)
                return value.data, value.entry.number
        task = self.task(task_id)
        with self.operation_lock(task_id):
            token = self.gate(task, "read-reply")
            entry = next((item for item in self._instruction_images(task, token).entries
                          if item.version_id == version_id), None)
            require(entry is not None, "NO_RESULT")
            raw, _ = self.instructions.adapter.send(
                "GET", f"/behavior/v1/bindings/{task.binding_id}/images/{version_id}", None, token, limit=MAX_IMAGE_BYTES)
            require(len(raw) == entry.byte_length and sha(raw) == entry.sha256 and raw[:8] == PNG_SIGNATURE,
                    "RESULT_INVALID", 502)
            return raw, entry.number

    def confirm_instruction_reply(self, task_id):
        thread = self.native_request(task_id)
        if thread is not None:
            with self.message_lock(task_id):
                self.active()
                self.native_instructions.change(task_id, "acknowledge", self.vault.get())
                return {"task": self.native_instructions.task_view(task_id, thread)}
        task = self.task(task_id)
        with self.operation_lock(task_id):
            self.gate(task, "confirm-reply")
            if self.journal.get(task_id, "acknowledge") is not None:
                return {"task": self.task_view(task)}
            require(task_id in self.instructions.replies, "NO_RESULT")
            if task_id not in self.results:
                require(self.journal.get(task_id, "fetch") is None, "CHALLENGE_LOST")
                self.run(task_id, "fetch")
            result = self.results[task_id]
            self.instructions.adapter.check_delivery_link(task, result)
            return self.run(task_id, "acknowledge")

    def reconcile_ack(self, task):
        token = self.gate(task, "reconcile")
        record = self.journal.get(task.task_id, "acknowledge")
        original = self.failure_adapter.original_ack(task, record, self.clock())
        original_hash = sha(canonical(record["intent"]))
        prior = self.journal.get(task.task_id, "reconcile")
        if prior is not None:
            require(prior["intent"].get("original_ack_intent_sha256") == original_hash
                    and prior["intent"].get("original_ack_body_sha256") == original.body_sha256,
                    "ORIGINAL_ACK_INVALID")
            return {"task": self.task_view(task)}
        started = False
        try:
            status = self.failure_adapter.status(task, token)
            self.statuses[task.task_id] = {"data": status.model_dump(mode="json"),
                "observed_at": self.clock().isoformat(), "not_current_live_result": True}
            require(not status.expired, "HANDOFF_EXPIRED", 403)
            self.active()
            token = self.vault.get()
            require(self.journal.get(task.task_id, "acknowledge") == record, "ORIGINAL_ACK_INVALID")
            method, path, body = self.failure_adapter.reconcile_request(task, original)
            intent = {"method": method, "path": path, "request_id": original.request_id,
                "body": body, "body_sha256": sha(canonical(body)), "created_at": self.clock().isoformat(),
                "configuration_sha256": self.config.sha256,
                "original_ack_intent_sha256": original_hash, "original_ack_body_sha256": original.body_sha256,
                "exchange_id": original.exchange_id, "binding": original.binding.model_dump(mode="json")}
            self.journal.begin(task.task_id, "reconcile", intent)
            started = True
            receipt = self.failure_adapter.reconcile(task, original, token)
            self.journal.finish(task.task_id, "reconcile", {
                "state": "CONFIRMED", "confirmation_level": "client_received", "receipt": receipt,
                "payload_sha256": original.binding.payload_digest, "observed_at": self.clock().isoformat(),
                "original_ack_state": "UNKNOWN", "ack_resent": False})
            self.observed_here.add((task.task_id, "reconcile"))
            self.errors.pop(task.task_id, None)
            return {"task": self.task_view(task)}
        except ConsoleError as error:
            self.errors[task.task_id] = {"code": error.code, "message": MESSAGES[error.code],
                "observed_at": self.clock().isoformat(), "upstream_status": error.upstream_status}
            if started:
                self.journal.finish(task.task_id, "reconcile", {
                    "state": "UNKNOWN", "code": error.code, "observed_at": self.clock().isoformat(),
                    "automatic_retry": False, "original_ack_state": "UNKNOWN", "ack_resent": False})
            raise

    def payload(self, task_id):
        if self.native_request(task_id) is not None:
            return self.download_instruction_reply(task_id)[0]
        task = self.task(task_id)
        if self.instruction_task(task) and task_id in self.instructions.reply_bytes:
            raw = self.instructions.reply_bytes[task_id]
            require(sha(raw) == self.instructions.replies[task_id]["sha256"], "RESULT_INVALID", 502)
            return raw
        require(task_id in self.results, "CHALLENGE_LOST")
        result = self.results[task_id]
        require(sha(result.payload) == result.binding.payload_digest, "RESULT_INVALID", 502)
        return result.payload

    def summary(self, task_id):
        if self.native_request(task_id) is not None:
            value = self.native_instructions.receipts.get(task_id)
            acknowledgment = self.journal.get(task_id, "acknowledge")
            return {"task_id": task_id, "state_profile": NATIVE_LANE,
                    "conversation_id": None if value is None else value.conversation_id,
                    "result_digest": None if value is None or value.result is None else value.result.result_digest,
                    "acknowledgment": acknowledgment, "not_current_live_result": True,
                    "note": "原回條的 delivered 只代表 client 收件，不代表真人已讀；不會重送。"}
        task = self.task(task_id)
        ack = self.journal.get(task_id, "acknowledge")
        if task.mode != "original_failure":
            summary = {"task_id": task_id, "mode": task.mode, "binding_id": task.binding_id, "job_id": task.job_id,
                    "observed_at": None if ack is None else ack["observed_at"], "not_current_live_result": True,
                    "confirmation_level": None if ack is None else ack.get("confirmation_level"),
                    "state": "NOT_CONFIRMED" if ack is None else ack["state"],
                    "receipt": None if ack is None else ack.get("receipt"),
                    "note": "只有原 backend 回條確認 client_received 才算收件；不表示真人已讀。"}
            if self.instruction_task(task):
                summary["reply_read"] = self.instructions.replies.get(task_id)
                summary["reply_read_is_not_receipt"] = True
                if self.journal.get(task_id, "cancel") is not None:
                    summary["cancellation"] = self.phase(task, "cancel")
            return summary
        reconciliation = self.journal.get(task_id, "reconcile") if task.mode == "original_failure" else None
        reconciled = reconciliation is not None and reconciliation["state"] == "CONFIRMED"
        observed = reconciliation if reconciled else ack
        return {"task_id": task_id, "mode": task.mode, "binding_id": task.binding_id, "job_id": task.job_id,
                "observed_at": None if observed is None else observed["observed_at"], "not_current_live_result": True,
                "confirmation_level": None if observed is None else observed.get("confirmation_level"),
                "state": "NOT_CONFIRMED" if observed is None else observed["state"],
                "receipt": None if observed is None else observed.get("receipt"),
                "confirmation_source": "original_ack_reconciliation" if reconciled else "acknowledgment",
                "original_ack": self.phase(task, "acknowledge"), "reconciliation": self.phase(task, "reconcile"),
                "ack_resent": False,
                "note": ("原ACK回應遺失；已核對原回條client_received（未重送）。原ACK UNKNOWN記錄保留。"
                         if reconciled else "只有原 backend 回條確認 client_received 才算收件；不表示真人已讀。")}


def create_app(config: ConsoleConfig | SteadyConsoleConfig | None = None, journal_path=None, *, token_provider=None,
               vault=None, backend=None, clock=now_utc, port=8765, environment="NOT_RUN",
               pull_continuation: PullContinuationSidecar | None = None,
               instruction_connection: InstructionConnection | None = None,
               agent_connections: AgentConnections | None = None, remote=None, realtime_endpoint=None, prewarm=False):
    """``remote`` is a RemoteBoundary (ADR-0008 §3); without it everything stays loopback-only and unchanged.

    ``realtime_endpoint`` (``FA_REALTIME_ENDPOINT``) turns on Web PubSub push hints; see ``realtime.py``.
    ``prewarm`` (``FA_CONSOLE_PREWARM=1``) lets typing wake the Agent's VM ahead of a send; see ``prewarm.py``.
    """
    require(type(port) is int and 1024 <= port <= 65535, "LOCAL_REQUEST_DENIED", 400)
    require(type(prewarm) is bool, "LOCAL_REQUEST_DENIED", 400)
    if agent_connections is not None:
        require(type(agent_connections) is AgentConnections and type(config) is StandingConsoleConfig
                and type(instruction_connection) is InstructionConnection and journal_path is not None,
                "AGENT_CONFIGURATION_INVALID")
        validate_default(agent_connections, config, journal_path, instruction_connection)
    prefix = "" if agent_connections is None else "/agents/" + agent_connections.default_agent_id
    console = Console(config, journal_path, token_provider=token_provider, vault=vault,
                      backend=backend, clock=clock, environment=environment, instruction_connection=instruction_connection,
                      api_prefix=prefix)
    agents = None if agent_connections is None else AgentConsoles(
        agent_connections, console, lambda config, journal, instructions, api_prefix: Console(
            config, journal, token_provider=console.token_provider, vault=console.vault, backend=console.backend,
            clock=clock, environment=environment, instruction_connection=instructions, api_prefix=api_prefix))
    onboarding = None if agents is None else AgentOnboarding(agents, journal_path, roles_by_server=remote is not None)
    if onboarding is not None:
        onboarding.restore_links()
    origin = f"http://127.0.0.1:{port}" if remote is None else remote.origin
    host = origin.split("://", 1)[1]
    cookie_name = "fa_console" if remote is None else "__Host-fa_console"
    cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    desktop_socket = f"ws://127.0.0.1:{port}" if remote is None else f"wss://{host}"
    realtime_host = realtime.endpoint_host(realtime_endpoint)
    realtime_socket = "" if realtime_host is None else f" wss://{realtime_host}"
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if console.vault is not None:
                console.vault.clear()
            for context in ([console] if agents is None else agents.consoles.values()):
                context.results.clear()
            if console.owns_backend:
                console.backend.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.console = console
    app.state.agent_consoles = agents

    def selected_agent(request: Request):
        values = request.headers.getlist("x-console-agent")
        require(len(values) <= 1, "LOCAL_REQUEST_DENIED", 403)
        return values[0] if values else None

    def requested_console(request: Request):
        identity = selected_agent(request)
        if agents is None:
            require(identity is None, "AGENT_NOT_AVAILABLE")
            return console
        return agents.resolve(identity)

    def scoped_state(value, request):
        return value if agents is None else agents.decorate(value, selected_agent(request))

    @app.middleware("http")
    async def local_boundary(request, call_next):
        try:
            if remote is not None:
                remote.admit(request.headers)
            # After Entra sign-in the browser lands on "/" from a cross-site redirect chain, so only the remote page
            # load accepts any Sec-Fetch-Site; it performs nothing and every API call stays same-origin only.
            landing = remote is not None and request.method == "GET" and request.url.path == "/"
            require(request.headers.get("host") == host
                    and (landing or request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"})
                    and (request.headers.get("origin") in {None, origin}),
                    "LOCAL_REQUEST_DENIED", 403)
            if request.url.path != "/":
                # A restarted remote replica issues a new page cookie; the page reloads once and resends nothing.
                require(secrets.compare_digest(request.cookies.get(cookie_name, ""), cookie),
                        "LOCAL_REQUEST_DENIED" if remote is None else "REMOTE_SESSION_CHANGED", 403)
            if request.method != "GET":
                require(request.method == "POST" and request.headers.get("origin") == origin
                        and secrets.compare_digest(request.headers.get("x-console-csrf", ""), csrf)
                        and request.headers.get("content-type", "").split(";")[0] == "application/json"
                        and not request.query_params, "LOCAL_REQUEST_DENIED", 403)
                text_input = (console.instructions is not None
                              and request.url.path.startswith("/api/instructions/")
                              and request.url.path.endswith("/submit"))
                append_input = (console.instructions is not None
                                and request.url.path.startswith("/api/instructions/")
                                and request.url.path.count("/") == 4 and request.url.path.endswith("/append"))
                template_input = (type(console.config) is StandingConsoleConfig
                                  and request.url.path in {"/api/agent-template/inspect", "/api/agent-template/select",
                                                           "/api/agent-template/selection-reconcile"})
                image_input = (console.instructions is not None
                               and request.url.path.startswith("/api/instructions/")
                               and request.url.path.count("/") == 4
                               and request.url.path.endswith("/image-upload"))
                onboarding_input_path = onboarding is not None and request.url.path == "/api/agents/onboarding"
                hitl_input = (console.instructions is not None and request.url.path.startswith("/api/native-hitl/")
                              and request.url.path.count("/") == 4
                              and request.url.path.rsplit("/", 1)[1] in {
                                  "rules", "decisions", "reconcile", "answers", "answer-reconcile", "consents",
                                  "consent-reconcile"})
                group_input = console.instructions is not None and request.url.path in {"/api/groups/open", "/api/groups/list"}
                # A bounded owner skill decision (2 KiB) or owner draft (128 KiB), capped in team_skills; the API still decides.
                team_skill_input = type(console.config) is StandingConsoleConfig and is_team_skill_write(request.url.path)
                # One bounded Team MCP change (16 KiB), capped in team_mcp; the API still decides.
                team_mcp_input = type(console.config) is StandingConsoleConfig and is_team_mcp_write(request.url.path)
                # One bounded connector change or Agent switch (8 KiB), capped in team_connectors; the API still decides.
                connector_input = type(console.config) is StandingConsoleConfig and is_connector_write(request.url.path)
                if (not text_input and not append_input and not template_input
                        and not image_input and not onboarding_input_path and not hitl_input and not group_input
                        and not team_skill_input and not team_mcp_input and not connector_input):
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        require(len(body) <= 16, "LOCAL_REQUEST_DENIED", 403)
                    require(bytes(body).strip() in {b"{}", b""}, "LOCAL_REQUEST_DENIED", 403)
            response = await call_next(request)
        except ConsoleError as error:
            response = JSONResponse({"error": {"code": error.code, "message": MESSAGES[error.code],
                "automatic_retry": False}}, status_code=error.status)
        # ws: is listed explicitly for the desktop viewer; data: covers noVNC's remote cursor image.
        # Remote: the Container Apps sign-in layer treats a cookie POST without Origin/Referer as forgery, so the page
        # sends its own origin as Referer to itself only.
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer" if remote is None else "same-origin", "Content-Security-Policy":
                "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                f"connect-src 'self' {desktop_socket}{realtime_socket}; img-src 'self' blob: data:; frame-ancestors 'none'; "
                "form-action 'none'; base-uri 'none'"})
        return response

    @app.exception_handler(ConsoleError)
    async def expected(request, error):
        # A server refusal passed through by its own code (not_dispatched) may not have a local sentence yet.
        return JSONResponse({"error": {"code": error.code, "message": MESSAGES.get(error.code, MESSAGES["BACKEND_DENIED"]),
            "upstream_status": error.upstream_status,
            "automatic_retry": error.automatic_retry and request.method == "GET",
            **({"not_dispatched": True} if error.not_dispatched else {}),
            **({"server_following": True} if error.server_following else {})}}, status_code=error.status)

    @app.exception_handler(Exception)
    async def failure(_request, _error):
        return JSONResponse({"error": {"code": "LOCAL_FAILURE", "message": MESSAGES["LOCAL_FAILURE"],
                                     "automatic_retry": False}}, status_code=500)

    from .desktop import install_desktop_routes
    install_desktop_routes(app, agents=agents, origin=origin, cookie=cookie, cookie_name=cookie_name, remote=remote)
    install_hitl_routes(app, requested_console)
    install_group_routes(app, requested_console)
    install_question_routes(app, requested_console)
    install_consent_routes(app, requested_console)
    install_team_skill_routes(app, requested_console)
    install_team_mcp_routes(app, requested_console)
    install_team_connector_routes(app, requested_console)
    install_routine_settings_routes(app, requested_console)

    @app.get("/")
    def page():
        response = HTMLResponse(typing_prewarm.page(realtime.page(
            Path(__file__).with_name("index.html").read_text(encoding="utf-8"), realtime_host), prewarm))
        response.set_cookie(cookie_name, cookie, httponly=True, samesite="strict", path="/", secure=remote is not None)
        return response

    @app.get("/api/state")
    def state(request: Request):
        # Remote only: the signed-in name for display; identity is still checked by the API, not by this label.
        shown = {} if remote is None else {"remote_user": remote.display_name(request.headers)}
        if agents is not None and selected_agent(request) is None:
            return {**agents.bootstrap(), "csrf": csrf, **shown}
        context = requested_console(request)
        return {**scoped_state(context.state(request.query_params.get("thread_id")), request), "csrf": csrf, **shown}

    @app.get("/api/realtime/negotiate")
    async def realtime_negotiate(request: Request, console=Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(realtime.negotiate, console, realtime_host)

    @app.get("/api/agents")
    async def agent_directory(request: Request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        require(agents is not None, "AGENT_DIRECTORY_UNAVAILABLE")
        return await run_in_threadpool(agents.refresh_directory)

    # Onboarding always uses the logged-in default Team entry; a not-yet-ready Agent is never a request target.
    async def no_input(request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        require(onboarding is not None, "AGENT_ONBOARDING_UNAVAILABLE")

    @app.get("/api/agents/onboarding")
    async def agent_onboardings(request: Request):
        await no_input(request)
        return await run_in_threadpool(onboarding.list)

    @app.post("/api/agents/onboarding")
    async def create_agent(request: Request):
        require(onboarding is not None, "AGENT_ONBOARDING_UNAVAILABLE")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            # Name, a 2048-byte role and up to 12 tool and 4 skill references stay well under this bound.
            require(len(raw) <= 16384, "AGENT_ONBOARDING_INPUT_INVALID", 413)
        return await run_in_threadpool(onboarding.create, onboarding_input(bytes(raw)))

    @app.get("/api/agents/onboarding/options")
    async def agent_onboarding_options(request: Request):
        await no_input(request)
        return await run_in_threadpool(onboarding.options)

    @app.get("/api/agents/onboarding/{request_id}")
    async def agent_onboarding_status(request_id: str, request: Request):
        await no_input(request)
        return await run_in_threadpool(onboarding.refresh, request_id)

    @app.post("/api/agents/onboarding/{request_id}/continue")
    async def continue_agent_onboarding(request_id: str):
        require(onboarding is not None, "AGENT_ONBOARDING_UNAVAILABLE")
        return await run_in_threadpool(onboarding.resume, request_id)

    @app.post("/api/agents/onboarding/{request_id}/prepare")
    async def prepare_agent_onboarding(request_id: str):
        require(onboarding is not None, "AGENT_ONBOARDING_UNAVAILABLE")
        return await run_in_threadpool(onboarding.prepare, request_id)

    @app.get("/api/agent-template")
    async def agent_template(request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        raw = await run_in_threadpool(console.download_template)
        return Response(raw, media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="fa-agent-template.json"',
            "x-content-sha256": sha(raw),
        })

    @app.post("/api/agent-template/inspect")
    async def inspect_template(request: Request, console: Console = Depends(requested_console)):
        from shared_context.behavior_agent_template import MAX_TEMPLATE_BYTES
        console.template_token()
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= MAX_TEMPLATE_BYTES, "TEMPLATE_INVALID", 413)
        result = await run_in_threadpool(console.inspect_template, bytes(raw))
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/agent-template/options")
    async def template_options(request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        result = await run_in_threadpool(console.template_options)
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/agent-template/system-prompt")
    async def template_system_prompt(request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return JSONResponse(await run_in_threadpool(console.template_system_prompt),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/agent-template/selection")
    async def template_selection(request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_template_selection)

    @app.post("/api/agent-template/select")
    async def select_template(request: Request, console: Console = Depends(requested_console)):
        from .agent_template import MAX_SELECTION_BYTES, parse_selection
        console.template_token()
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= MAX_SELECTION_BYTES, "TEMPLATE_SELECTION_INVALID", 413)
        return await run_in_threadpool(console.select_template, parse_selection(bytes(raw)))

    @app.post("/api/agent-template/selection-reconcile")
    async def reconcile_template_selection(request: Request, console: Console = Depends(requested_console)):
        from shared_context.behavior_wire import Command
        console.template_token()
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= 256, "TEMPLATE_SELECTION_INVALID", 413)
        try:
            strict_json(bytes(raw))
            command = Command.model_validate_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError("TEMPLATE_SELECTION_INVALID", 400) from None
        return await run_in_threadpool(console.reconcile_template_selection, command.request_id)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.post("/api/authenticate")
    def authenticate():
        return console.authenticate() if agents is None else agents.authenticate()

    @app.post("/api/authenticate/refresh")
    def refresh_authentication():
        return console.authenticate(automatic=True) if agents is None else agents.authenticate(automatic=True)

    @app.post("/api/instructions/{thread_id}/next")
    def new_instruction(thread_id: str, request: Request, console: Console = Depends(requested_console)):
        return scoped_state(console.new_instruction(thread_id), request)

    @app.post("/api/instructions/conversations/new")
    def new_conversation(request: Request, console: Console = Depends(requested_console)):
        return scoped_state(console.new_conversation(), request)

    @app.post("/api/instructions/prewarm")
    def prewarm_agent(console: Console = Depends(requested_console)):
        # README#typing-prewarm: a hint only; no lock, journal entry or state, and the page ignores the answer.
        return typing_prewarm.forward(console, prewarm)

    @app.post("/api/instructions/{thread_id}/reset")
    def reset_conversation(thread_id: str, request: Request, console: Console = Depends(requested_console)):
        return scoped_state(console.reset_conversation(thread_id), request)

    # P06: the workbench no longer prepares, advances, reconciles, or confirms environment transfer.
    # The server follows versions during submit and reports server_following when the same RID should be retried.

    @app.get("/api/instructions/progress")
    def instruction_progress(request: Request, console: Console = Depends(requested_console)):
        return {**scoped_state(console.instruction_progress(request.query_params.get("thread_id")), request), "csrf": csrf}

    @app.get("/api/instructions/models")
    def instruction_models(request: Request, console: Console = Depends(requested_console)):
        return {**scoped_state(console.instruction_models(request.query_params.get("thread_id")), request), "csrf": csrf}

    @app.get("/api/instructions/received")
    def instruction_received(request: Request, console: Console = Depends(requested_console)):
        query = request.query_params
        require(set(query) <= {"request_id", "thread_id"} and all(len(query.getlist(key)) == 1 for key in query)
                and "request_id" in query, "LOCAL_REQUEST_DENIED", 403)
        return {**scoped_state(console.instruction_received(query["request_id"], query.get("thread_id")), request),
                "csrf": csrf}

    @app.get("/api/instructions/{task_id}/reply")
    def instruction_reply(task_id: str, request: Request, console: Console = Depends(requested_console)):
        return {**scoped_state(console.read_instruction_reply(task_id), request), "csrf": csrf}

    @app.get("/api/instructions/{task_id}/reply.txt")
    async def instruction_reply_text(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        raw, turn_number = await run_in_threadpool(console.download_instruction_reply, task_id)
        return Response(raw, media_type="text/plain", headers={
            "Content-Disposition": f'attachment; filename="fa-reply-{turn_number}.txt"',
            "x-content-sha256": sha(raw),
        })

    @app.get("/api/instructions/{task_id}/context")
    async def instruction_context(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_context, task_id)

    @app.get("/api/instructions/{task_id}/system-prompt")
    async def instruction_system_prompt(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return JSONResponse(await run_in_threadpool(console.instruction_system_prompt, task_id),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/instructions/{task_id}/skill-draft")
    async def instruction_skill_draft(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        value = await run_in_threadpool(console.instruction_skill_draft, task_id)
        return value.model_dump(mode="json")

    @app.get("/api/instructions/{task_id}/skill-draft.md")
    async def instruction_skill_draft_text(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        value = await run_in_threadpool(console.instruction_skill_draft, task_id)
        require(value.receipt is not None and value.body is not None, "NATIVE_RESULT_MISSING", 409)
        data = value.body.encode("utf-8")
        return Response(data, media_type="text/markdown", headers={
            "Content-Disposition": 'attachment; filename="fa-skill-draft.md"', "x-content-sha256": sha(data),
            "X-Content-Type-Options": "nosniff"})

    @app.get("/api/instructions/{task_id}/images")
    async def instruction_images(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        value = await run_in_threadpool(console.instruction_images, task_id)
        return {"entries": [entry.model_dump(mode="json") for entry in value.entries]}

    @app.get("/api/instructions/{thread_id}/platform-history")
    async def platform_history(thread_id: str, request: Request, console: Console = Depends(requested_console)):
        require(set(request.query_params) <= {"after", "previous"}
                and len(request.query_params.multi_items()) == len(request.query_params)
                and ("previous" not in request.query_params or request.query_params["previous"] == "1"),
                "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(
            console.instruction_platform_history, thread_id, after=request.query_params.get("after"),
            previous=request.query_params.get("previous") == "1")

    @app.get("/api/instructions/{task_id}/images/{version_id}")
    async def instruction_image(task_id: str, version_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        raw, number = await run_in_threadpool(console.instruction_image, task_id, version_id)
        return Response(raw, media_type="image/png", headers={
            "Content-Disposition": f'attachment; filename="fa-screenshot-{number}.png"',
            "x-content-sha256": sha(raw), "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/instructions/{task_id}/delegation")
    async def instruction_delegation(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_delegation, task_id)

    @app.get("/api/instructions/{task_id}/agent-messages")
    async def agent_messages(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(read_messages, console, task_id)

    @app.post("/api/instructions/{task_id}/confirm-reply")
    def confirm_reply(task_id: str, console: Console = Depends(requested_console)):
        return console.confirm_instruction_reply(task_id)

    @app.post("/api/instructions/{task_id}/cancel")
    def cancel_instruction(task_id: str, console: Console = Depends(requested_console)):
        return console.cancel_instruction(task_id)

    @app.get("/api/instructions/{task_id}/cancel-receipt")
    async def cancellation_receipt(task_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_cancellation_receipt, task_id)

    @app.post("/api/instructions/{request_id}/submit")
    async def submit_instruction(request_id: str, request: Request, console: Console = Depends(requested_console)):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                require(len(raw) <= 32768, "LOCAL_REQUEST_DENIED", 403)
            body = strict_json(raw)
            require(type(body) is dict and {"content", "model"} <= set(body)
                    and set(body) <= {"content", "model", "attachments", "files", "images"}, "LOCAL_REQUEST_DENIED", 403)
        except (ValueError, TypeError):
            raise ConsoleError("INSTRUCTION_INVALID", 400) from None
        return await run_in_threadpool(console.submit_instruction, request_id, body["content"], body["model"],
                                       body.get("attachments", ()), body.get("files", ()), body.get("images", ()))

    @app.post("/api/instructions/{request_id}/image-upload")
    async def upload_instruction_image(request_id: str, request: Request, console: Console = Depends(requested_console)):
        """B1 PA-08: one browser-converted PNG (at most 512 KiB, sides at most 2000 px), stored before the send."""
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                require(len(raw) <= MAX_IMAGE_UPLOAD_BODY_BYTES, "LOCAL_REQUEST_DENIED", 403)
            body = strict_json(raw)
            require(type(body) is dict and set(body) == {"name", "png_base64"}
                    and all(type(body[key]) is str for key in body), "LOCAL_REQUEST_DENIED", 403)
        except (ValueError, TypeError):
            raise ConsoleError("USER_IMAGE_INPUT_INVALID", 400) from None
        return await run_in_threadpool(console.upload_instruction_image, request_id, body["name"], body["png_base64"])

    @app.post("/api/instructions/{request_id}/append")
    async def append_instruction(request_id: str, request: Request, console: Console = Depends(requested_console)):
        try:
            raw = await request.body()
            require(len(raw) <= 32768, "LOCAL_REQUEST_DENIED", 403)
            body = strict_json(raw)
            require(type(body) is dict and set(body) == {"append_id", "content"}, "LOCAL_REQUEST_DENIED", 403)
        except (ValueError, TypeError):
            raise ConsoleError("INSTRUCTION_INVALID", 400) from None
        return await run_in_threadpool(console.append_instruction, request_id, body["append_id"], body["content"])

    @app.get("/api/instructions/{request_id}/appends")
    async def instruction_appends(request_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_appends, request_id)

    @app.get("/api/instructions/{request_id}/status")
    async def instruction_status(request_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        value = await run_in_threadpool(console.instruction_status, request_id)
        return {**scoped_state(value, request), "csrf": csrf}

    @app.get("/api/instructions/{request_id}/stream")
    async def instruction_live_text(request_id: str, request: Request, console: Console = Depends(requested_console)):
        # RTT B: display-only live text, relayed as it arrives; the page still replaces it with /status.
        query = request.query_params
        require(set(query) <= {"starting_after"} and len(query.getlist("starting_after")) <= 1
                and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        raw = query.get("starting_after")
        require(raw is None or raw.isascii() and raw.isdigit() and len(raw) <= 12, "LOCAL_REQUEST_DENIED", 403)
        chunks = await run_in_threadpool(console.instruction_live_text, request_id,
                                         None if raw is None else int(raw))
        return StreamingResponse(chunks, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/instructions/{request_id}/progress")
    async def instruction_public_progress(request_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_progress, request_id)

    @app.get("/api/instructions/{request_id}/user-text-file-capability")
    async def instruction_file_capability(request_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.instruction_file_capability, request_id)

    @app.get("/api/instructions/{request_id}/files/{filename}")
    async def instruction_file(request_id: str, filename: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        raw = await run_in_threadpool(console.download_instruction_file, request_id, filename)
        return Response(raw, media_type="text/plain", headers={
            "Content-Disposition": f'attachment; filename="{filename}"', "x-content-sha256": sha(raw),
        })

    @app.get("/api/instructions/{request_id}/attachment-sources")
    async def instruction_attachment_sources(request_id: str, request: Request, console: Console = Depends(requested_console)):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.instruction_attachment_sources, request_id)

    @app.post("/api/tasks/{task_id}/{action}")
    def operate(task_id: str, action: str, console: Console = Depends(requested_console)):
        return console.run(task_id, action)

    @app.get("/api/tasks/{task_id}/payload")
    def download_payload(task_id: str, console: Console = Depends(requested_console)):
        return Response(console.payload(task_id), media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="verified-payload.json"'})

    @app.get("/api/tasks/{task_id}/summary")
    def download_summary(task_id: str, console: Console = Depends(requested_console)):
        return JSONResponse(console.summary(task_id), headers={
            "Content-Disposition": 'attachment; filename="receipt-summary.json"'})

    return app
