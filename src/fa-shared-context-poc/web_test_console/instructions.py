"""Local original-text intents; no backend permission or conversation authority."""
import json
import secrets
from typing import Literal
from pydantic import Field

from .adapter import ConsoleError, canonical, require, strict_json
from shared_context import behavior_wire as wire
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE

INSTRUCTION_QUOTED_UTF8_LIMIT = 8192
STATE_PROFILES = (None, NATIVE_STATE_PROFILE, NATIVE_RESPONSES_PROFILE)


class InstructionConnection(wire.WireModel):
    """Deployment wiring only; never an approval or replacement policy."""
    schema_version: Literal["fa-console-instructions-connection-v1"]
    configuration_sha256: wire.Digest
    backend_core_sha256: wire.Digest
    state_profile: Literal["foundry-native-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)


def check_content(content):
    require(type(content) is str and bool(content.strip()), "INSTRUCTION_INVALID", 400)
    try:
        raw = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeError:
        raise ConsoleError("INSTRUCTION_INVALID", 400) from None
    require(len(raw) <= INSTRUCTION_QUOTED_UTF8_LIMIT, "INSTRUCTION_INVALID", 400)
    return content


class InstructionStore:
    """Local thread labels are not SDK conversation identities or permissions."""
    def __init__(self, journal, clock):
        self.journal, self.clock = journal, clock
        with journal.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS instructions (
                request_id TEXT PRIMARY KEY, content TEXT, created_at TEXT NOT NULL, thread_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS instruction_threads (
                thread_id TEXT PRIMARY KEY, draft_request_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS instruction_commands (
                request_id TEXT PRIMARY KEY, command TEXT NOT NULL)""")
            db.execute("BEGIN IMMEDIATE")
            schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='instruction_lanes'").fetchone()
            migrate = schema is not None and "foundry-responses-v1" not in schema[0]
            if migrate:
                db.execute("ALTER TABLE instruction_lanes RENAME TO instruction_lanes_legacy")
            db.execute("""CREATE TABLE IF NOT EXISTS instruction_lanes (
                thread_id TEXT PRIMARY KEY, state_profile TEXT
                CHECK(state_profile IS NULL OR state_profile IN ('foundry-native-v1','foundry-responses-v1')))""")
            if migrate:
                db.execute("INSERT INTO instruction_lanes SELECT thread_id,state_profile FROM instruction_lanes_legacy")
                db.execute("DROP TABLE instruction_lanes_legacy")
            if db.execute("SELECT 1 FROM instruction_threads LIMIT 1").fetchone() is None:
                self._insert_thread(db)

    def _insert_thread(self, db):
        request_id = "text-" + secrets.token_hex(16)
        db.execute("INSERT INTO instructions VALUES (?,NULL,?,?)", (request_id, self.clock().isoformat(), request_id))
        db.execute("INSERT INTO instruction_threads VALUES (?,?)", (request_id, request_id))
        return request_id

    def new_thread(self):
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            thread_id = self._insert_thread(db)
        return self.current(thread_id)

    def complete_reset(self,thread_id,receipt):
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT intent,outcome FROM intents WHERE task_id=? AND phase='reset'",(thread_id,)).fetchone()
            require(row is not None,"RESET_UNKNOWN")
            intent,outcome=map(strict_json,row)
            require(intent["conversation_id"]==receipt["conversation_id"]
                    and intent["body"]["request_id"]==receipt["reset_request_id"],"RESET_UNKNOWN")
            if outcome["state"]=="CONFIRMED":
                new_thread=outcome["new_thread_id"]
            else:
                require(outcome["state"]=="UNKNOWN","RESET_UNKNOWN")
                new_thread=self._insert_thread(db)
                result={"state":"CONFIRMED","receipt":receipt,"new_thread_id":new_thread,
                        "observed_at":self.clock().isoformat()}
                db.execute("UPDATE intents SET outcome=? WHERE task_id=? AND phase='reset'",
                           (canonical(result).decode(),thread_id))
        return self.current(new_thread)

    def current(self, thread_id=None):
        with self.journal.connect() as db:
            if thread_id is None:
                latest = db.execute("SELECT thread_id FROM instruction_threads ORDER BY rowid DESC LIMIT 1").fetchone()
                require(latest is not None, "LOCAL_FAILURE", 500)
                thread_id = latest[0]
            row = db.execute("""SELECT i.request_id,i.content,i.created_at,i.thread_id FROM instructions i
                JOIN instruction_threads t ON i.request_id=t.draft_request_id WHERE t.thread_id=?""", (thread_id,)).fetchone()
        require(row is not None, "INSTRUCTION_SCOPE")
        return dict(zip(("request_id", "content", "created_at", "thread_id"), row))

    def new(self, thread_id=None):
        current = self.current(thread_id)
        thread_id = current["thread_id"]
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT i.request_id,i.content FROM instructions i
                JOIN instruction_threads t ON i.request_id=t.draft_request_id WHERE t.thread_id=?""", (thread_id,)).fetchone()
            require(row is not None, "INSTRUCTION_SCOPE")
            if row[1] is not None:
                request_id = "text-" + secrets.token_hex(16)
                db.execute("INSERT INTO instructions VALUES (?,NULL,?,?)", (request_id, self.clock().isoformat(), thread_id))
                db.execute("UPDATE instruction_threads SET draft_request_id=? WHERE thread_id=?", (request_id, thread_id))
        return self.current(thread_id)

    def freeze(self, request_id, content):
        content = check_content(content)
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT content,thread_id FROM instructions WHERE request_id=?", (request_id,)).fetchone()
            require(row is not None, "INSTRUCTION_SCOPE", 409)
            require(row[0] is None or row[0] == content, "INSTRUCTION_CHANGED", 409)
            if row[0] is None:
                current = db.execute("SELECT draft_request_id FROM instruction_threads WHERE thread_id=?", (row[1],)).fetchone()
                require(current is not None and current[0] == request_id, "INSTRUCTION_SCOPE", 409)
                db.execute("UPDATE instructions SET content=? WHERE request_id=? AND content IS NULL", (content, request_id))
        return self.get(request_id)

    def get(self, request_id):
        with self.journal.connect() as db:
            row = db.execute("SELECT request_id,content,created_at,thread_id FROM instructions WHERE request_id=?",
                             (request_id,)).fetchone()
        require(row is not None, "INSTRUCTION_SCOPE", 409)
        return dict(zip(("request_id", "content", "created_at", "thread_id"), row))

    def all(self, thread_id=None):
        with self.journal.connect() as db:
            rows = db.execute("""SELECT request_id,content,created_at,thread_id FROM instructions
                WHERE content IS NOT NULL AND (? IS NULL OR thread_id=?) ORDER BY rowid""", (thread_id, thread_id)).fetchall()
        return [dict(zip(("request_id", "content", "created_at", "thread_id"), row)) for row in rows]

    def threads(self):
        with self.journal.connect() as db:
            rows = db.execute("""SELECT t.thread_id,i.content FROM instruction_threads t
                JOIN instructions i ON i.request_id=t.thread_id ORDER BY t.rowid""").fetchall()
        return [{"thread_id":row[0], "label":row[1][:60] if row[1] else "新的對話"} for row in rows]

    def command(self, request_id):
        with self.journal.connect() as db:
            row = db.execute("SELECT command FROM instruction_commands WHERE request_id=?", (request_id,)).fetchone()
        return None if row is None else strict_json(row[0])

    def has_submission(self, request_id):
        with self.journal.connect() as db:
            return db.execute("SELECT 1 FROM intents WHERE task_id=? AND "
                              "(phase='submit' OR phase LIKE 'submit-reconcile-%')", (request_id,)).fetchone() is not None

    @staticmethod
    def _stored_lane(db, thread_id):
        pinned = db.execute("SELECT state_profile FROM instruction_lanes WHERE thread_id=?", (thread_id,)).fetchone()
        if pinned is not None:
            return pinned
        prior = db.execute("""SELECT 1 FROM instructions i WHERE i.thread_id=? AND (
            EXISTS(SELECT 1 FROM instruction_commands c WHERE c.request_id=i.request_id)
            OR EXISTS(SELECT 1 FROM intents x WHERE x.task_id=i.request_id)) LIMIT 1""", (thread_id,)).fetchone()
        return (None,) if prior is not None else None

    @classmethod
    def _lane(cls, db, thread_id, preferred):
        pinned = cls._stored_lane(db, thread_id)
        return pinned[0] if pinned is not None else preferred

    def has_fixed_lane(self, thread_id):
        self.current(thread_id)
        with self.journal.connect() as db:
            return self._stored_lane(db, thread_id) is not None

    def observe_native(self, receipt):
        """Attach an API-verified group turn for ordinary reads/attention, never as a new submission."""
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT thread_id,content FROM instructions WHERE request_id=?",
                             (receipt.request_id,)).fetchone()
            if row is not None:
                require(row[1] == receipt.command.content and self._lane(db, row[0], None) == receipt.state_profile,
                        "INSTRUCTION_ROUTE_CHANGED")
                return row[0]
            thread = receipt.request_id
            db.execute("INSERT INTO instructions VALUES (?,?,?,?)",
                       (receipt.request_id, receipt.command.content, self.clock().isoformat(), thread))
            db.execute("INSERT INTO instruction_threads VALUES (?,?)", (thread, receipt.request_id))
            db.execute("INSERT INTO instruction_lanes VALUES (?,?)", (thread, receipt.state_profile))
        return thread

    def lane(self, thread_id, *, preferred=None):
        require(preferred in STATE_PROFILES, "INSTRUCTION_ROUTE_CHANGED")
        self.current(thread_id)
        with self.journal.connect() as db:
            return self._lane(db, thread_id, preferred)

    def prepare(self, body, *, freeze=False, state_profile=None):
        require(state_profile in STATE_PROFILES, "INSTRUCTION_ROUTE_CHANGED")
        content = check_content(body["content"])
        encoded = canonical(body).decode()
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            original = db.execute("SELECT content,thread_id FROM instructions WHERE request_id=?",
                                  (body["request_id"],)).fetchone()
            require(original is not None, "INSTRUCTION_SCOPE")
            require(original[0] == content or freeze and original[0] is None, "INSTRUCTION_CHANGED")
            require(self._lane(db, original[1], state_profile) == state_profile, "INSTRUCTION_ROUTE_CHANGED")
            db.execute("INSERT OR IGNORE INTO instruction_lanes VALUES (?,?)", (original[1], state_profile))
            row = db.execute("SELECT command FROM instruction_commands WHERE request_id=?", (body["request_id"],)).fetchone()
            if row is None:
                require(db.execute("SELECT 1 FROM intents WHERE task_id=? AND "
                                   "(phase='submit' OR phase LIKE 'submit-reconcile-%')",
                                   (body["request_id"],)).fetchone() is None, "INSTRUCTION_INCOMPLETE")
                if freeze:
                    current = db.execute("SELECT draft_request_id FROM instruction_threads WHERE thread_id=?",
                                         (original[1],)).fetchone()
                    require(current is not None and current[0] == body["request_id"], "INSTRUCTION_SCOPE")
                    db.execute("UPDATE instructions SET content=? WHERE request_id=?", (content, body["request_id"]))
                db.execute("INSERT INTO instruction_commands VALUES (?,?)", (body["request_id"], encoded))
            else:
                require(row[0] == encoded and original[0] == content, "INSTRUCTION_CHANGED", 409)
        return body
