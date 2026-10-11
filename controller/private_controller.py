from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from azure.identity import ManagedIdentityCredential

from run_poc import API_VERSION, HostedPocClient, run, run_succeeded, utc_now

DEFAULT_IMAGE = (
    "crcontosodemo01.azurecr.io/fa-hosted-sandbox-poc/"
    "fa-hosted-sandbox-poc-faexample"
    "@sha256:d6f8f7134abc4b2cf5089ecc2c5e1de2a6cf1b189bd962afb547f668c51bab01"
)
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def definition(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"[^@\s]+\.azurecr\.io/[^@\s]+@sha256:[0-9a-f]{64}", args.image):
        raise ValueError("Use an immutable Azure Container Registry image digest.")
    if args.smb_auth_mode not in ("account_key", "managed_identity"):
        raise ValueError("Unsupported SMB authentication mode.")
    environment = {
        "POC_STORAGE_ACCOUNT_NAME": args.storage_account,
        "POC_FILE_SHARE_NAME": args.share,
    }
    if args.smb_auth_mode == "account_key":
        environment.update({
            "POC_KEY_VAULT_URL": args.vault_url,
            "POC_STORAGE_KEY_SECRET_NAME": args.secret_name,
        })
    else:
        for value in (args.expected_runtime_principal, args.tenant_id):
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
                raise ValueError("Managed identity requires the expected principal and tenant UUIDs.")
        environment.update({
            "POC_SMB_AUTH_MODE": "managed_identity",
            "POC_EXPECTED_MI_OBJECT_ID": args.expected_runtime_principal,
            "POC_EXPECTED_MI_TENANT_ID": args.tenant_id,
        })
    return {
        "kind": "hosted", "cpu": "2", "memory": "4Gi",
        "container_configuration": {"image": args.image},
        "protocol_versions": [
            {"protocol": "invocations", "version": "2.0.0"},
            {"protocol": "responses", "version": "2.0.0"},
        ],
        "session_configuration": {"idle_timeout_seconds": 300},
        "environment_variables": environment,
    }


def check_private_dns(hosts: list[str]) -> dict:
    result = {}
    for host in hosts:
        addresses = sorted({
            item[4][0] for item in socket.getaddrinfo(host, 443, family=socket.AF_INET)
        })
        result[host] = addresses
    nonprivate = {
        host: addresses for host, addresses in result.items()
        if not addresses or any(
            not any(ipaddress.ip_address(address) in network for network in PRIVATE_NETWORKS)
            for address in addresses
        )
    }
    if nonprivate:
        raise RuntimeError(f"Controller DNS must resolve all endpoints to RFC1918 addresses: {nonprivate}")
    return {"observed_at_utc": utc_now(), "scope": "controller_only_not_hosted_G0", "addresses": result}


def request_json(client: HostedPocClient, method: str, suffix: str, **kwargs) -> dict:
    url = (
        f"{client.project_endpoint}/agents/{quote(client.agent_name, safe='')}"
        f"{suffix}?api-version={API_VERSION}"
    )
    response = client.http.request(method, url, headers=client._headers(), timeout=60, **kwargs)
    if response.status_code >= 400 and response.status_code != 404:
        record = {
            "observed_at_utc": utc_now(), "method": method, "url": url,
            "status_code": response.status_code, "body": response.text,
        }
        (client.output_dir / "last-api-error.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
    response.raise_for_status()
    return response.json()


def matches_definition(version: dict, expected: dict) -> bool:
    actual = version.get("definition", {})
    return all(actual.get(key) == value for key, value in expected.items())


def ensure_version(client: HostedPocClient, expected: dict, output: Path) -> dict:
    try:
        existing = request_json(client, "GET", "/versions")
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        existing = {"data": [], "has_more": False}
    if existing.get("has_more"):
        raise RuntimeError("Version list is paginated; inspect it before creating another version.")
    if not isinstance(existing.get("data"), list):
        raise RuntimeError("Unexpected agent version list response.")
    matches = [item for item in existing["data"] if matches_definition(item, expected)]
    if matches:
        version = matches[0]
    else:
        # Do not retry POST after a timeout: the server may already have created the version.
        version = request_json(
            client, "POST", "/versions",
            json={"definition": expected, "description": "Native Hosted sandbox POC over BYO VNet."},
        )
    deadline = time.monotonic() + 600
    while True:
        output.write_text(json.dumps(version, indent=2), encoding="utf-8")
        if version.get("status") == "active":
            if not matches_definition(version, expected):
                raise RuntimeError("Active version differs from the requested image/configuration.")
            return version
        if version.get("status") in {"failed", "deleted", "deleting"}:
            raise RuntimeError(f"Hosted version is not usable: {version}")
        if time.monotonic() >= deadline:
            raise TimeoutError("Version activation timed out; inspect the saved version before retrying.")
        time.sleep(15)
        version = request_json(client, "GET", f"/versions/{quote(version['version'], safe='')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Private-VNet control plane using the VM managed identity.")
    parser.add_argument("action", choices=("dns", "deploy", "g0", "g3", "all"))
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent-name", default="fa-hosted-sandbox-poc")
    parser.add_argument("--agent-version")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--storage-account", default="stfaexampledemo")
    parser.add_argument("--share", default="team-poc")
    parser.add_argument("--vault-url", default="https://kv-faexample-de.vault.azure.net")
    parser.add_argument("--secret-name", default="azure-files-account-key")
    parser.add_argument("--smb-auth-mode", choices=("account_key", "managed_identity"), default="account_key")
    parser.add_argument("--expected-runtime-principal", default="00000000-0000-0000-0000-000000000011")
    parser.add_argument("--tenant-id", default="00000000-0000-0000-0000-000000000013")
    parser.add_argument("--output", type=Path, default=Path("evidence"))
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("vnet-%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    endpoint = urlparse(args.project_endpoint)
    vault = urlparse(args.vault_url)
    if (
        endpoint.scheme != "https" or not endpoint.hostname
        or not endpoint.hostname.endswith(".services.ai.azure.com")
        or not endpoint.path.startswith("/api/projects/") or endpoint.query or endpoint.fragment
        or vault.scheme != "https" or not vault.hostname
        or not vault.hostname.endswith(".vault.azure.net")
    ):
        parser.error("Use Azure public-cloud Foundry project and Key Vault HTTPS endpoints.")
    expected = definition(args)
    args.output.mkdir(parents=True, exist_ok=True)
    dns = check_private_dns([
        endpoint.hostname, vault.hostname, f"{args.storage_account}.file.core.windows.net",
        args.image.split("/")[0],
    ])
    (args.output / "controller-dns.json").write_text(json.dumps(dns, indent=2), encoding="utf-8")
    if args.action == "dns":
        print(json.dumps(dns, indent=2))
        return
    client = HostedPocClient(
        args.project_endpoint, args.agent_name, args.output, ManagedIdentityCredential()
    )
    try:
        if args.action == "deploy":
            version = ensure_version(client, expected, args.output / "agent-version.json")
            print(json.dumps({
                "version": version["version"], "status": version["status"],
                "instance_identity": version.get("instance_identity"),
            }, indent=2))
            return
        if not args.agent_version:
            parser.error("--agent-version is required for g0/g3/all.")
        version = request_json(client, "GET", f"/versions/{quote(args.agent_version, safe='')}")
        if version.get("status") != "active" or not matches_definition(version, expected):
            raise RuntimeError("Gates require the exact active version/image/environment configuration.")
        args.poc_id = "fa-hosted-sandbox"
        args.image_digest = args.image.split("@", 1)[1]
        if args.action == "g3":
            from g3_only import run as run_g3

            result = run_g3(args, client=client)
            print(json.dumps({
                "status": result["status"],
                "shared_publish": result["shared_publish"],
                "cleanup": result["cleanup"],
                "error": result.get("error"),
            }, indent=2))
            if result["status"] != "PASS":
                raise SystemExit(1)
            return
        args.expected_source = f"//{args.storage_account}.file.core.windows.net/{args.share}"
        args.g0_only = args.action == "g0"
        args.include_g2 = args.action == "all"
        args.idle_timeout_seconds = expected["session_configuration"]["idle_timeout_seconds"]
        args.idle_seconds = args.idle_timeout_seconds + 30
        args.capture_logs = True
        result = run(args, client)
        print(json.dumps({"gates": result["gates"], "cleanup": result["cleanup"]}, indent=2))
        if not run_succeeded(args, result):
            raise SystemExit(1)
    finally:
        client.http.close()
        client.credential.close()


if __name__ == "__main__":
    main()
