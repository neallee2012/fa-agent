from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import struct
import sys
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests


def verify_result(result: dict, nonce: str) -> None:
    if result["completed_value"] != nonce or result["page_ready"] is not True:
        raise RuntimeError("GUI callback did not match this run")
    if type(result["browser_uid"]) is not int or result["browser_uid"] <= 0 or result["browser_sandbox_disabled"] is not False:
        raise RuntimeError("Browser isolation baseline was not retained")
    url = urlparse(result["loopback_url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        raise RuntimeError("Browser did not use sandbox loopback")
    if any(
        argument.startswith(("--headless", "--no-sandbox", "--disable-setuid-sandbox", "--remote-debugging"))
        for argument in result["browser_command_line"]
    ):
        raise RuntimeError("Browser flags do not meet the headed sandbox baseline")
    if not result["browser_command_line"] or result["loopback_url"] not in result["browser_command_line"]:
        raise RuntimeError("Headed browser command line does not identify the loopback page")
    if len(result["cleanup"]) != 3 or any(
        type(item.get("return_code")) is not int for item in result["cleanup"]
    ):
        raise RuntimeError("Desktop process cleanup is incomplete")
    pids = [result["pids"][name] for name in ("xvfb", "openbox", "chromium")]
    if (
        any(type(pid) is not int or pid <= 0 for pid in pids)
        or len(set(pids)) != 3
        or {item.get("pid") for item in result["cleanup"]} != set(pids)
    ):
        raise RuntimeError("Desktop cleanup does not identify all started processes")
    if not result["display"].startswith(":") or any(
        not result["versions"].get(name) for name in ("xvfb", "openbox", "chromium")
    ):
        raise RuntimeError("Local display/version evidence is incomplete")
    commands = result["gui_commands"]
    if (
        len(commands) != 3
        or any(not command or command[0] != "xdotool" for command in commands)
        or commands[1] != ["xdotool", "type", "--clearmodifiers", "--delay", "20", nonce]
        or commands[2] != ["xdotool", "key", "--clearmodifiers", "Tab", "Return"]
    ):
        raise RuntimeError("OS GUI input evidence does not match this run")


def verify_png(data: bytes) -> tuple[int, int]:
    """Validate the non-interlaced PNGs produced by the root-display capture."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Downloaded display evidence is not PNG")
    offset, dimensions, compressed = 8, None, bytearray()
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise RuntimeError("Truncated PNG chunk")
        value = data[offset + 8:end - 4]
        if zlib.crc32(kind + value) != int.from_bytes(data[end - 4:end], "big"):
            raise RuntimeError("PNG chunk checksum differs")
        if dimensions is None and kind != b"IHDR":
            raise RuntimeError("PNG is missing its header")
        if kind == b"IHDR":
            if dimensions is not None or length != 13:
                raise RuntimeError("Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", value)
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
            valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (
                not 0 < width <= 4096 or not 0 < height <= 4096
                or channels is None or depth not in valid_depths[color]
                or compression != 0 or filtering != 0 or interlace != 0
            ):
                raise RuntimeError("Unsupported display PNG format or dimensions")
            dimensions = (width, height)
            row_size = (width * channels * depth + 7) // 8 + 1
        elif kind == b"IDAT":
            compressed.extend(value)
        elif kind == b"IEND":
            if length or end != len(data) or not compressed:
                raise RuntimeError("Invalid PNG end")
            decoder = zlib.decompressobj()
            expected = row_size * height
            pixels = decoder.decompress(compressed, expected + 1)
            if (
                len(pixels) != expected or not decoder.eof or decoder.unused_data
                or any(pixels[index] > 4 for index in range(0, expected, row_size))
            ):
                raise RuntimeError("Invalid PNG pixel data")
            return dimensions
        offset = end
    raise RuntimeError("PNG is missing its end")


def download_and_verify(
    client, session_id: str, result: dict, *, home: str, poc_id: str, run_id: str,
    nonce: str, run_dir: Path, downloads: list, save,
) -> None:
    """Download only this run's private evidence, never a replacement for /team I/O."""
    home_path = PurePosixPath(home)
    if not home_path.is_absolute() or ".." in home_path.parts:
        raise RuntimeError("Hosted HOME evidence is not an absolute Linux path")
    remote_root = PurePosixPath("fa-poc", "evidence", poc_id, run_id, "g3-desktop")
    files = {
        "before": "before.png", "after": "after.png",
        "browser_download": "downloads/browser-artifact.txt",
    }
    errors = []
    for key, relative in files.items():
        artifact = result["artifacts"][key]
        remote = remote_root / relative
        # Derive Files API's HOME-relative path from the tool response, not an assumed HOME.
        if artifact.get("path") != str(home_path / remote):
            raise RuntimeError(f"Private artifact path is outside this run: {key}")
        target = run_dir / PurePosixPath(relative).name
        record = {"key": key, "remote": str(remote), "local": target.name}
        downloads.append(record)
        try:
            record.update(client.download_session_file(session_id, str(remote), target))
            data = target.read_bytes()
            record.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            if record["sha256"] != artifact["sha256"]:
                raise RuntimeError(f"Downloaded evidence hash differs: {key}")
            if "size" in artifact and len(data) != artifact["size"]:
                raise RuntimeError(f"Downloaded evidence size differs: {key}")
            if key != "browser_download":
                record["dimensions"] = list(verify_png(data))
            elif data != f"{poc_id}|{run_id}|{nonce}|GUI_COMPLETE\n".encode("utf-8"):
                raise RuntimeError("Downloaded browser artifact bytes differ")
            record["verified"] = True
        except (requests.RequestException, OSError, RuntimeError, ValueError, KeyError, zlib.error) as exc:
            record["error"] = str(exc)
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                record["status_code"] = exc.response.status_code
            errors.append(f"{key}: {exc}")
        finally:
            save()
    if errors:
        raise RuntimeError("; ".join(errors))
    images = [item for item in downloads if item.get("key") in {"before", "after"}]
    if images[0]["sha256"] == images[1]["sha256"] or images[0]["dimensions"] != images[1]["dimensions"]:
        raise RuntimeError("Before/after display images are identical or dimensions differ")
    published = result["artifacts"].get("published_shared")
    if published:
        expected = f"{poc_id}|{run_id}|{nonce}|GUI_COMPLETE\n".encode("utf-8")
        if published["sha256"] != hashlib.sha256(expected).hexdigest() or published["size"] != len(expected):
            raise RuntimeError("Published shared artifact differs from browser download")


def run(args: argparse.Namespace, client=None) -> dict:
    from run_poc import HostedPocClient, payload, save_manifest, utc_now

    run_dir = args.output / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if client is None:
        client = HostedPocClient(args.project_endpoint, args.agent_name, run_dir)
    nonce = secrets.token_hex(12)
    manifest = {
        "schema_version": "1.0", "poc_id": args.poc_id, "run_id": args.run_id,
        "gate_id": "G3", "started_at_utc": utc_now(),
        "agent_name": args.agent_name, "agent_version": args.agent_version,
        "image_digest": args.image_digest, "project_endpoint": args.project_endpoint,
        "nonce": nonce, "sessions": {}, "events": [], "downloads": [], "cleanup": [],
        "status": "BLOCKED", "reason_code": "ENVIRONMENT_OR_APPROVAL",
        "shared_publish": "NOT_RUN_DEPENDENCY",
    }

    def save() -> None:
        save_manifest(run_dir, manifest)

    def invoke(operation: str, **values: str) -> dict:
        request = payload(args.poc_id, args.run_id, operation, **values)
        event = {"request": request, "started_at_utc": utc_now()}
        manifest["events"].append(event)
        save()
        try:
            response = client.invoke(manifest["sessions"]["A"], request)
        except Exception as exc:
            event["transport_error"] = {"type": type(exc).__name__, "message": str(exc)}
            save()
            raise
        event["response"] = response
        save()
        body = response["body"]
        if response["status_code"] != 200 or body.get("status") != "success":
            raise RuntimeError(f"{operation} failed: {body}")
        if body.get("runtime", {}).get("session_id") != manifest["sessions"]["A"]:
            raise RuntimeError("Invocation returned a different session")
        return body["result"]

    save()
    try:
        session = client.create_session(args.agent_version)
        manifest["sessions"]["A"] = session["agent_session_id"]
        manifest["session_created"] = session
        save()
        environment = invoke("probe_environment")
        if environment["agent"]["version"] != args.agent_version:
            raise RuntimeError("Session version does not match the requested version")
        result = invoke("desktop_smoke", step_id="g3-desktop", nonce=nonce)
        manifest["desktop_result"] = result
        verify_result(result, nonce)
        download_and_verify(
            client, manifest["sessions"]["A"], result, home=environment["home"],
            poc_id=args.poc_id, run_id=args.run_id, nonce=nonce, run_dir=run_dir,
            downloads=manifest["downloads"], save=save,
        )
        manifest["status"] = "PASS"
        manifest["reason_code"] = None
    except (requests.RequestException, RuntimeError, KeyError, OSError, ValueError) as exc:
        manifest["status"] = "PARTIAL" if len(manifest["events"]) > 1 else "BLOCKED"
        manifest["reason_code"] = "GUI_OR_BROWSER_RUNTIME"
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        session_id = manifest["sessions"].get("A")
        if session_id:
            try:
                manifest["session_logs"] = client.capture_session_logs(
                    session_id, args.agent_version, run_dir / "A-console.sse"
                )
                save()
            except Exception as exc:
                manifest["session_logs"] = {"error": str(exc)}
            try:
                response = client.stop_session(session_id)
                manifest["cleanup"].append({"session_id": session_id, "response": response})
            except Exception as exc:
                manifest["cleanup"].append({"session_id": session_id, "error": str(exc)})
                manifest["status"] = "BLOCKED"
                manifest["reason_code"] = "ENVIRONMENT_OR_APPROVAL"
        manifest["completed_at_utc"] = utc_now()
        try:
            save()
        finally:
            client.http.close()
            client.credential.close()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="G3 only; never attempts shared storage.")
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent-name", default="fa-hosted-sandbox-poc")
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--poc-id", default="fa-hosted-sandbox")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("evidence"))
    args = parser.parse_args()
    if not os.environ.get("AZURE_CONFIG_DIR") or os.environ.get("AZURE_DEV_USER_AGENT") != "microsoft_foundry_skill":
        parser.error("Set the isolated AZURE_CONFIG_DIR and inline AZURE_DEV_USER_AGENT first.")
    result = run(args)
    print(json.dumps({
        "status": result["status"], "sessions": result["sessions"],
        "manifest": str(args.output / args.run_id / "run-manifest.json"),
        "error": result.get("error"),
    }, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
