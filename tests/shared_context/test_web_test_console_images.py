"""8765 shows screenshots delivered with a reply through the human's own read; no public URL, no writes."""

import copy
import hashlib

from shared_context.assistant_image_api import install_image_routes
from shared_context.assistant_image_wire import TITLE
from shared_context.assistant_images import image_artifact_id
from shared_context.m3_models import CreateArtifact, VersionInput
from test_assistant_images import png
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_answer_download import saved
from test_web_test_console_frozen_interface import real_interface


def deliver(c, task, data):
    """Write one screenshot version as the owning agent, the way the delivery route stores it."""
    c.h.runtime.refresh_bindings(task["binding_id"])
    binding = c.h.runtime.bindings[task["binding_id"]]
    actor, source = c.h.assistant.actors[binding.agent_oid], c.h.runtime.profiles[task["binding_id"]].legacy_source_work
    artifacts, artifact = c.h.assistant.artifacts, image_artifact_id(task["job_id"])
    artifacts.create(actor, binding.team_id, artifact, CreateArtifact(kind="deliverable", title=TITLE, source_work=source))
    sha = hashlib.sha256(data).hexdigest()
    artifacts.upload(actor, binding.team_id, artifact, sha[:32], artifacts.get(actor, binding.team_id, artifact).etag,
                     VersionInput(filename="screenshot-1.png", media_type="image/png", sha256=sha, source_work=source), data)
    return sha


def test_reply_screenshots_are_listed_and_served_only_as_verified_png(real_interface):
    c = real_interface
    task = saved(c)
    install_image_routes(c.h.app, c.h.assistant, c.h.verifier)
    base = "/api/instructions/" + task["task_id"] + "/images"
    empty = c.browser.ui.get(base)
    assert empty.status_code == 200 and empty.json()["entries"] == []
    data = png(8)
    sha = deliver(c, task, data)
    before, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    listing = c.browser.ui.get(base)
    assert listing.status_code == 200, listing.text
    assert [(entry["number"], entry["sha256"]) for entry in listing.json()["entries"]] == [(1, sha)]
    image = c.browser.ui.get(base + "/" + sha[:32])
    assert image.status_code == 200 and image.content == data
    assert image.headers["content-type"] == "image/png" and image.headers["cache-control"] == "no-store"
    assert image.headers["content-disposition"] == 'attachment; filename="fa-screenshot-1.png"'
    assert "blob:" in c.browser.ui.get("/").headers["content-security-policy"]
    assert c.browser.ui.get(base + "/not-a-version").status_code == 403
    assert c.browser.ui.get(base + "/" + "0" * 32).status_code != 200
    assert c.browser.ui.get(base + "?url=1").status_code == 403
    assert all(call["method"] == "GET" for call in c.calls)
    assert c.h.transport.backing.rows == before and c.journal.read_bytes() == journal
