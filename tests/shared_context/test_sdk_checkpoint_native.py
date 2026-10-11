"""Real local SDK/process persistence; no cloud, paid model, or synthetic SDK events."""
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3


def test_real_sdk_large_tool_state_survives_a_new_client_and_process(tmp_path):
    runtime=Path(os.environ["M2_TEST_RUNTIME"])
    assert runtime.is_file(), "Provision the pinned runtime; do not silently skip this acceptance"
    script=Path(__file__).with_name("sdk_checkpoint_local.py")
    original_reference=None
    before_compact_rows=None
    for phase in ("create","resume","compact"):
        if phase=="compact":
            original_reference=(tmp_path/"create-reference.json").read_bytes()
            with sqlite3.connect(tmp_path/"artifacts.sqlite3") as db:
                before_compact_rows=db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        result=subprocess.run([sys.executable,str(script),str(tmp_path),phase,str(runtime)],
                              capture_output=True,text=True,encoding="utf-8",timeout=120)
        assert result.returncode==0,result.stdout+"\n"+result.stderr
    created=json.loads((tmp_path/"create-proof.json").read_bytes())
    resumed=json.loads((tmp_path/"resume-proof.json").read_bytes())
    assert created["raw_bytes"]>131072 and created["zlib_bytes"]>16000
    assert created["sdk_session_id"]==resumed["sdk_session_id"]
    assert created["conversation_id"]==resumed["conversation_id"]
    assert created["python_pid"]!=resumed["python_pid"]
    assert created["sdk_stop"]["pid"]!=resumed["sdk_stop"]["pid"]
    assert created["sdk_stop"]["graceful"] and resumed["sdk_stop"]["graceful"]
    assert resumed["tools"]==[] and "Restored original source CATALOG_" in resumed["answer"]
    for name,metadata in created["files"].items():
        if name.startswith("temp/"):
            assert resumed["files"][name]==metadata
    assert (tmp_path/"create-reference.json").is_file(), "last valid checkpoint was removed"
    compacted=json.loads((tmp_path/"compact-proof.json").read_bytes())
    assert compacted["native_compaction"] and compacted["native_compaction"][-1]["success"] is True
    assert compacted["checkpoint_status"]=="blocked_private_material"
    assert not compacted["reference_published"] and not compacted["sdk_files_rewritten"]
    assert compacted["last_valid_checkpoint_retained"]
    assert (tmp_path/"create-reference.json").read_bytes()==original_reference
    assert not (tmp_path/"compact-reference.json").exists()
    with sqlite3.connect(tmp_path/"artifacts.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]==before_compact_rows
    assert created["private_system_filtered"] and resumed["private_system_filtered"]
    assert created["transport_credential_absent"] and resumed["transport_credential_absent"]
