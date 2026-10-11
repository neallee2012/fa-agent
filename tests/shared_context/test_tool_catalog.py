"""Organization mcpServers catalog: format, secret references, placement and fail-closed loading."""

import json
import os

import pytest

from shared_context.errors import Problem
from shared_context.tool_catalog import (
    CatalogError, HttpEntry, StdioEntry, build_catalog, load_catalog, parse_entry_file, resolve_headers,
)

WEBIQ = {"mcpServers": {"WebIQ-MCP": {
    "url": "https://api.microsoft.ai/v3/mcp", "type": "http",
    "headers": {"x-apikey": "${secret:webiq-api-key}"}, "tools": ["web", "browse"],
}}}
PLAYWRIGHT = {"mcpServers": {"playwright": {
    "type": "local", "command": "/opt/fa-tools/bin/playwright-mcp",
    "args": ["--headless", "--isolated", "--browser=chromium", "--executable-path=/usr/bin/chromium"],
    "env": {"PLAYWRIGHT_BROWSERS_PATH": "0"}, "tools": ["browser_navigate", "browser_snapshot"],
}}}
PLAINTEXT_KEY = "Zx9Qm2Lp7Rt4Vw8Ky3Nb6Hc1Jd5Fg0Sa"


def raw(document):
    return json.dumps(document).encode("utf-8")


def variant(document, **changes):
    (server, config), = document["mcpServers"].items()
    updated = {**config, **changes}
    for name in [name for name, value in changes.items() if value is None]:
        del updated[name]
    return {"mcpServers": {server: updated}}


def rejected(document, code, filename="entry.json"):
    with pytest.raises(CatalogError) as error:
        parse_entry_file(filename, document if isinstance(document, bytes) else raw(document))
    assert error.value.code == code
    return str(error.value)


def test_secret_http_entry_is_an_api_client_and_stdio_entry_runs_in_hosted():
    webiq = parse_entry_file("webiq.json", raw(WEBIQ))
    assert webiq.execution == "api-client" and isinstance(webiq.config, HttpEntry)
    assert webiq.tool_names == ("WebIQ-MCP-web", "WebIQ-MCP-browse")
    assert webiq.secret_names == ("webiq-api-key",)
    assert webiq.config.headers == (("x-apikey", "${secret:webiq-api-key}"),)

    playwright = parse_entry_file("playwright.json", raw(PLAYWRIGHT))
    assert playwright.execution == "hosted-stdio" and isinstance(playwright.config, StdioEntry)
    assert playwright.config.type == "stdio" and playwright.secret_names == ()
    assert parse_entry_file("p.json", raw(variant(PLAYWRIGHT, type="stdio"))).entry_sha256 == playwright.entry_sha256


def test_entry_digest_ignores_json_layout_but_not_content():
    compact = parse_entry_file("a.json", raw(WEBIQ)).entry_sha256
    pretty = json.dumps({"mcpServers": {"WebIQ-MCP": dict(reversed(list(WEBIQ["mcpServers"]["WebIQ-MCP"].items())))}},
                        indent=4).encode("utf-8")
    assert parse_entry_file("b.json", pretty).entry_sha256 == compact
    assert parse_entry_file("c.json", raw(variant(WEBIQ, tools=["web"]))).entry_sha256 != compact


@pytest.mark.parametrize("headers", [
    {"x-apikey": PLAINTEXT_KEY},
    {"Authorization": "Bearer abc"},
    {"x-trace": PLAINTEXT_KEY},
    {"x-apikey": "prefix-${secret:webiq-api-key}"},
])
def test_plaintext_or_embedded_credentials_reject_the_whole_file_without_echoing_values(headers):
    message = rejected(variant(WEBIQ, headers=headers), "CATALOG_PLAINTEXT_SECRET", "webiq.json")
    assert message.startswith("webiq.json: ")
    assert all(value not in message for value in headers.values())


def test_secret_in_url_query_or_url_is_rejected():
    rejected(variant(WEBIQ, url="https://api.example.test/mcp?api_key=" + PLAINTEXT_KEY), "CATALOG_PLAINTEXT_SECRET")
    rejected(variant(WEBIQ, url="https://api.example.test/mcp/" + PLAINTEXT_KEY), "CATALOG_PLAINTEXT_SECRET")
    rejected(variant(WEBIQ, url="https://api.example.test/${secret:webiq-api-key}"), "CATALOG_PLAINTEXT_SECRET")
    message = rejected(variant(WEBIQ, url="https://admin:" + PLAINTEXT_KEY + "@api.example.test/mcp"), "CATALOG_FORMAT")
    assert "without credentials" in message and PLAINTEXT_KEY not in message
    rejected(variant(WEBIQ, url="http://api.example.test/mcp"), "CATALOG_FORMAT")


@pytest.mark.parametrize("document", [
    b'{"mcpServers":{"a":{"type":"http","url":"https://[::1/mcp","headers":{"x-apikey":"${secret:n}"},"tools":["t"]}}}',
    b"[" * (16 * 1024),
    raw(variant(PLAYWRIGHT, command="/opt/fa-tools/bin/" + "a" * 600)),
    b'{"mcpServers":{"a":{"type":"http","url":"https://x.test/","headers":{"x-apikey":"${secret:n}","x-trace":"\\ud800"},"tools":["t"]}}}',
    b'{"mcpServers":{"a":{"type":"stdio","command":"/opt/fa-tools/bin/x","args":["\\ud800"],"tools":["t"]}}}',
    b'{"mcpServers":{"a":{"type":"http","url":"https://x.test/\\ud800","headers":{"x-apikey":"${secret:n}"},"tools":["t"]}}}',
])
def test_hostile_input_fails_closed_without_quoting_it(document):
    message = rejected(document, "CATALOG_FORMAT")
    assert "aaaa" not in message and "::1" not in message


def test_http_entry_without_secret_is_pending_and_not_loaded():
    rejected(variant(WEBIQ, headers=None), "CATALOG_UNSUPPORTED_ENTRY")
    rejected(variant(WEBIQ, headers={"content-type": "application/json"}), "CATALOG_UNSUPPORTED_ENTRY")


@pytest.mark.parametrize("changes,code", [
    ({"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}, "CATALOG_UNPINNED_COMMAND"),
    ({"command": "/opt/fa-tools/bin/npx"}, "CATALOG_UNPINNED_COMMAND"),
    ({"command": "/usr/bin/node"}, "CATALOG_UNPINNED_COMMAND"),
    ({"command": "bin/playwright-mcp"}, "CATALOG_UNPINNED_COMMAND"),
    ({"command": "/opt/fa-tools/../usr/bin/bash"}, "CATALOG_UNPINNED_COMMAND"),
    ({"args": ["--package=@playwright/mcp@latest"]}, "CATALOG_UNPINNED_COMMAND"),
    ({"env": {"API_TOKEN": "${secret:webiq-api-key}"}}, "CATALOG_UNSUPPORTED_ENTRY"),
    ({"args": ["--token=${secret:webiq-api-key}"]}, "CATALOG_UNSUPPORTED_ENTRY"),
    ({"env": {"API_TOKEN": "literal"}}, "CATALOG_PLAINTEXT_SECRET"),
    ({"args": ["--token=" + PLAINTEXT_KEY]}, "CATALOG_PLAINTEXT_SECRET"),
    ({"args": [PLAINTEXT_KEY]}, "CATALOG_PLAINTEXT_SECRET"),
])
def test_stdio_entries_must_be_preinstalled_pinned_and_secret_free(changes, code):
    message = rejected(variant(PLAYWRIGHT, **changes), code)
    assert PLAINTEXT_KEY not in message


@pytest.mark.parametrize("document", [
    {"servers": {"WebIQ-MCP": WEBIQ["mcpServers"]["WebIQ-MCP"]}},
    {"mcpServers": {}},
    {"mcpServers": {**WEBIQ["mcpServers"], **PLAYWRIGHT["mcpServers"]}},
    {"mcpServers": {"WebIQ-MCP": WEBIQ["mcpServers"]["WebIQ-MCP"]}, "inputs": []},
    variant(WEBIQ, tools=None),
    variant(WEBIQ, tools=["*"]),
    variant(WEBIQ, tools=[]),
    variant(WEBIQ, tools=["web", "web"]),
    variant(WEBIQ, oauth={"clientId": "x"}),
    variant(WEBIQ, timeout="30"),
    variant(PLAYWRIGHT, cwd="/tmp"),
    {"mcpServers": {"bad name": WEBIQ["mcpServers"]["WebIQ-MCP"]}},
    {"mcpServers": {"x" * 20: {**WEBIQ["mcpServers"]["WebIQ-MCP"], "tools": ["t" * 45]}}},
])
def test_non_native_or_open_ended_entries_are_rejected(document):
    rejected(document, "CATALOG_FORMAT")


def test_unsupported_transport_types_and_bad_bytes_are_rejected():
    rejected(variant(WEBIQ, type="sse"), "CATALOG_UNSUPPORTED_ENTRY")
    rejected(variant(WEBIQ, type=None), "CATALOG_UNSUPPORTED_ENTRY")
    rejected(b'{"mcpServers": {"a": {}, "a": {}}}', "CATALOG_FORMAT")
    rejected(b"\xff\xfe", "CATALOG_FORMAT")
    rejected(b" " * (16 * 1024 + 1), "CATALOG_FORMAT")


def test_directory_load_is_deterministic_and_fails_closed(tmp_path):
    (tmp_path / "b-webiq.json").write_bytes(raw(WEBIQ))
    (tmp_path / "a-playwright.json").write_bytes(raw(PLAYWRIGHT))
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    catalog = load_catalog(tmp_path)
    assert [item.entry_id for item in catalog.entries] == ["WebIQ-MCP", "playwright"]
    assert catalog.entry("WebIQ-MCP").execution == "api-client" and catalog.entry("missing") is None
    assert catalog.catalog_sha256 == build_catalog(list(reversed(catalog.entries))).catalog_sha256

    (tmp_path / "c-bad.json").write_bytes(raw(variant(WEBIQ, headers={"x-apikey": PLAINTEXT_KEY})))
    with pytest.raises(CatalogError) as error:
        load_catalog(tmp_path)
    assert error.value.code == "CATALOG_PLAINTEXT_SECRET" and "c-bad.json" in str(error.value)
    assert PLAINTEXT_KEY not in str(error.value)

    (tmp_path / "c-bad.json").write_bytes(raw(WEBIQ))
    with pytest.raises(CatalogError) as error:
        load_catalog(tmp_path)
    assert error.value.code == "CATALOG_DUPLICATE"


def test_directory_load_rejects_links(tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(raw(WEBIQ))
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    try:
        os.symlink(target, catalog / "linked.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(CatalogError) as error:
        load_catalog(catalog)
    assert error.value.code == "CATALOG_FORMAT"


def test_secret_resolution_is_api_side_and_missing_secret_is_needs_auth():
    webiq = parse_entry_file("webiq.json", raw(WEBIQ))
    assert resolve_headers(webiq, {"webiq-api-key": "resolved-value"}.get) == {"x-apikey": "resolved-value"}
    with pytest.raises(Problem) as missing:
        resolve_headers(webiq, lambda name: None)
    assert missing.value.body["error"]["code"] == "CAPABILITY_NEEDS_AUTH"
    assert "resolved-value" not in json.dumps(missing.value.body)
    with pytest.raises(Problem) as denied:
        resolve_headers(parse_entry_file("p.json", raw(PLAYWRIGHT)), {"x": "y"}.get)
    assert denied.value.body["error"]["code"] == "CAPABILITY_EXECUTION_DENIED"
