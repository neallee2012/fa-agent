"""Organization MCP catalog: one standard ``mcpServers`` JSON file per server.

The catalog only describes what the organization pre-installed or connected.
It grants nothing: Team enablement, human selection and per-work capture are
separate. Secret values never appear in the JSON; header values may only name a
server-side secret as ``${secret:NAME}``. Execution location follows the entry:
an ``http`` entry that carries a secret is used by the FA API as the MCP client,
a ``stdio`` entry runs a pinned, image-preinstalled command in the Agent's
own Hosted sandbox, and a ``toolbox`` entry names one MCP server label inside a
Foundry project toolbox that the native Hosted turn calls with its own agent
identity (the credential stays on the project connection). This module is
SDK-independent (stdlib and pydantic only).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field

from .behavior_wire import Digest, WireModel
from .errors import Problem

MAX_ENTRY_FILE_BYTES = 16 * 1024
MAX_CATALOG_ENTRIES = 32
MAX_TOOLS_PER_ENTRY = 32
MAX_TOOL_FUNCTION_NAME = 64
PREINSTALLED_TOOL_ROOT = PurePosixPath("/opt/fa-tools")

_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SECRET_NAME = r"[A-Za-z0-9][A-Za-z0-9-]{0,126}"
_SECRET_REFERENCE = re.compile(r"^\$\{secret:(" + _SECRET_NAME + r")\}$")
_SECRET_MARKER = "${secret:"
_SENSITIVE_NAME = re.compile(
    r"(?i)(key|token|secret|auth|passw|credential|cookie|session|signature|sig$|sas|bearer|private)")
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9+/=_.~-]{24,}$")
_PACKAGE_RUNNERS = frozenset({
    "npx", "npm", "pnpm", "pnpx", "yarn", "bunx", "bun", "uvx", "uv", "pipx", "pip", "pip3", "docker", "podman",
    "curl", "wget", "sh", "bash", "zsh", "dash", "env",
})
_HTTP_FIELDS = {"type", "url", "headers", "tools", "timeout"}
_STDIO_FIELDS = {"type", "command", "args", "env", "tools", "timeout"}
_TOOLBOX_FIELDS = {"type", "toolbox", "server_label", "tools", "timeout", "version"}
_TOOLBOX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_TOOLBOX_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

Execution = Literal["api-client", "hosted-stdio", "foundry-toolbox"]


class CatalogError(ValueError):
    """A rejected catalog file; messages name files and fields, never values."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HttpEntry(WireModel):
    type: Literal["http"]
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    headers: tuple[tuple[str, str], ...]
    timeout_ms: Annotated[int, Field(ge=1, le=600_000)] | None = None


class StdioEntry(WireModel):
    type: Literal["stdio"]
    command: Annotated[str, Field(min_length=1, max_length=512)]
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    timeout_ms: Annotated[int, Field(ge=1, le=600_000)] | None = None


class ToolboxEntry(WireModel):
    """One MCP ``server_label`` of a project toolbox; the connection, not FA, holds its credential."""

    type: Literal["toolbox"]
    toolbox: Annotated[str, Field(pattern=_TOOLBOX_NAME.pattern)]
    server_label: Annotated[str, Field(pattern=_TOOLBOX_NAME.pattern)]
    version: Annotated[str, Field(pattern=_TOOLBOX_VERSION.pattern)] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    timeout_ms: Annotated[int, Field(ge=1, le=600_000)] | None = None


class CatalogEntry(WireModel):
    schema_version: Literal["fa-mcp-catalog-entry-v1"] = "fa-mcp-catalog-entry-v1"
    entry_id: Annotated[str, Field(pattern=_SERVER_NAME.pattern)]
    execution: Execution
    tools: Annotated[tuple[Annotated[str, Field(pattern=_TOOL_NAME.pattern)], ...],
                     Field(min_length=1, max_length=MAX_TOOLS_PER_ENTRY)]
    config: HttpEntry | StdioEntry | ToolboxEntry
    entry_sha256: Digest

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Model-facing names follow the SDK convention ``<server>-<tool>``."""
        return tuple(f"{self.entry_id}-{tool}" for tool in self.tools)

    @property
    def secret_names(self) -> tuple[str, ...]:
        if not isinstance(self.config, HttpEntry):
            return ()
        return tuple(sorted({_SECRET_REFERENCE.fullmatch(value).group(1)
                             for _, value in self.config.headers if _SECRET_REFERENCE.fullmatch(value)}))


class ToolCatalog(WireModel):
    schema_version: Literal["fa-mcp-catalog-v1"] = "fa-mcp-catalog-v1"
    entries: Annotated[tuple[CatalogEntry, ...], Field(max_length=MAX_CATALOG_ENTRIES)]
    catalog_sha256: Digest

    def entry(self, entry_id: str) -> CatalogEntry | None:
        return next((item for item in self.entries if item.entry_id == entry_id), None)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("CATALOG_FORMAT", "duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def _string_map(value, label, name_pattern) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise CatalogError("CATALOG_FORMAT", f"{label} must be an object of at most 32 string values")
    for name, item in value.items():
        if not isinstance(name, str) or not name_pattern.fullmatch(name):
            raise CatalogError("CATALOG_FORMAT", f"{label} contains an invalid name")
        if not isinstance(item, str) or len(item) > 2048:
            raise CatalogError("CATALOG_FORMAT", f"{label} values must be strings of at most 2048 characters")
    return value


def _looks_secret(value: str) -> bool:
    """Conservative guess for an opaque credential written in plain text."""
    if not _OPAQUE_VALUE.fullmatch(value):
        return False
    classes = sum(bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    counts = {char: value.count(char) for char in set(value)}
    entropy = -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())
    return classes >= 2 and entropy >= 3.5


def _check_plain_value(label: str, name: str, value: str) -> None:
    if _SECRET_MARKER in value:
        raise CatalogError("CATALOG_PLAINTEXT_SECRET", f"{label} '{name}' embeds a secret reference inside other text")
    if _SENSITIVE_NAME.search(name) or _looks_secret(value):
        raise CatalogError("CATALOG_PLAINTEXT_SECRET",
                           f"{label} '{name}' looks like a credential; use ${{secret:NAME}} instead of a plain value")


def _tools(value, server: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_TOOLS_PER_ENTRY:
        raise CatalogError("CATALOG_FORMAT", "tools must be an explicit non-empty allowlist")
    if any(not isinstance(item, str) or not _TOOL_NAME.fullmatch(item) for item in value):
        raise CatalogError("CATALOG_FORMAT", "tools must name individual tools; wildcards are not accepted")
    if len(set(value)) != len(value):
        raise CatalogError("CATALOG_FORMAT", "tools must be distinct")
    if any(len(f"{server}-{item}") > MAX_TOOL_FUNCTION_NAME for item in value):
        raise CatalogError("CATALOG_FORMAT", "server and tool names together exceed the model function-name bound")
    return tuple(value)


def _timeout(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 600_000:
        raise CatalogError("CATALOG_FORMAT", "timeout must be an integer number of milliseconds up to 600000")
    return value


def _http_entry(config: dict) -> HttpEntry:
    url = config.get("url")
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise CatalogError("CATALOG_FORMAT", "url must be a non-empty string")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise CatalogError("CATALOG_FORMAT", "url must be an https URL without credentials or fragment")
    if _SECRET_MARKER in url:
        raise CatalogError("CATALOG_PLAINTEXT_SECRET", "secret references are accepted only in header values")
    if any(_looks_secret(segment) for segment in parts.path.split("/")):
        raise CatalogError("CATALOG_PLAINTEXT_SECRET", "url path looks like it embeds a credential")
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        _check_plain_value("url query parameter", name, value)
    headers = _string_map(config.get("headers"), "headers", _HEADER_NAME)
    if len({name.lower() for name in headers}) != len(headers):
        raise CatalogError("CATALOG_FORMAT", "header names must be distinct ignoring case")
    for name, value in headers.items():
        if not _SECRET_REFERENCE.fullmatch(value):
            _check_plain_value("header", name, value)
    return HttpEntry(type="http", url=url, headers=tuple(sorted(headers.items())), timeout_ms=_timeout(config.get("timeout")))


def _stdio_entry(config: dict) -> StdioEntry:
    command = config.get("command")
    if not isinstance(command, str) or not command or len(command) > 512:
        raise CatalogError("CATALOG_FORMAT", "command must be a non-empty string of at most 512 characters")
    path = PurePosixPath(command)
    if (not path.is_absolute() or ".." in path.parts or path.name.lower() in _PACKAGE_RUNNERS
            or not path.is_relative_to(PREINSTALLED_TOOL_ROOT) or "@" in command or "\\" in command):
        raise CatalogError("CATALOG_UNPINNED_COMMAND",
                           f"command must be an image-preinstalled absolute path under {PREINSTALLED_TOOL_ROOT}; "
                           "package runners and runtime downloads are not accepted")
    args = config.get("args", [])
    if not isinstance(args, list) or len(args) > 64 or any(not isinstance(item, str) or len(item) > 1024 for item in args):
        raise CatalogError("CATALOG_FORMAT", "args must be at most 64 strings")
    for index, item in enumerate(args):
        if _SECRET_MARKER in item:
            raise CatalogError("CATALOG_UNSUPPORTED_ENTRY",
                               "stdio entries run in Hosted; secret references are not supported for them")
        if re.search(r"(?i)@latest\b|@next\b|@canary\b", item):
            raise CatalogError("CATALOG_UNPINNED_COMMAND", "stdio args must not select an unpinned package version")
        flag, separator, value = item.partition("=")
        if separator and flag.startswith("-"):
            _check_plain_value("argument", flag, value)
        elif _looks_secret(item):
            raise CatalogError("CATALOG_PLAINTEXT_SECRET", f"argument {index} looks like a credential")
    env = _string_map(config.get("env"), "env", _ENV_NAME)
    for name, value in env.items():
        if _SECRET_REFERENCE.fullmatch(value):
            raise CatalogError("CATALOG_UNSUPPORTED_ENTRY",
                               "stdio entries run in Hosted; secret references are not supported for them")
        _check_plain_value("env", name, value)
    return StdioEntry(type="stdio", command=command, args=tuple(args), env=tuple(sorted(env.items())),
                      timeout_ms=_timeout(config.get("timeout")))


def _toolbox_entry(config: dict) -> ToolboxEntry:
    names = {}
    for field in ("toolbox", "server_label"):
        value = config.get(field)
        if not isinstance(value, str) or not _TOOLBOX_NAME.fullmatch(value):
            raise CatalogError("CATALOG_FORMAT", f"{field} must be 1-63 letters, digits, '_' or '-'")
        names[field] = value
    version = config.get("version")
    if version is not None:
        if not isinstance(version, str) or not _TOOLBOX_VERSION.fullmatch(version):
            raise CatalogError("CATALOG_FORMAT", "version must be 1-64 letters, digits, '.', '_' or '-'")
        names["version"] = version
    return ToolboxEntry(type="toolbox", timeout_ms=_timeout(config.get("timeout")), **names)


def parse_entry_file(filename: str, raw: bytes) -> CatalogEntry:
    """Parse one catalog file holding exactly one server; any violation rejects the whole file."""
    try:
        if len(raw) > MAX_ENTRY_FILE_BYTES:
            raise CatalogError("CATALOG_FORMAT", "catalog file exceeds its byte bound")
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
            json.dumps(document, ensure_ascii=False).encode("utf-8")
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise CatalogError("CATALOG_FORMAT", "catalog file must be well-formed UTF-8 JSON") from None
        if not isinstance(document, dict) or set(document) != {"mcpServers"}:
            raise CatalogError("CATALOG_FORMAT", "catalog file must contain only a top-level mcpServers object")
        servers = document["mcpServers"]
        if not isinstance(servers, dict) or len(servers) != 1:
            raise CatalogError("CATALOG_FORMAT", "catalog file must define exactly one server")
        (server, config), = servers.items()
        if not isinstance(server, str) or not _SERVER_NAME.fullmatch(server):
            raise CatalogError("CATALOG_FORMAT", "server name must be 1-32 letters, digits, '_' or '-'")
        if not isinstance(config, dict):
            raise CatalogError("CATALOG_FORMAT", "server configuration must be an object")
        kind = config.get("type")
        if kind == "http":
            fields, build, execution = _HTTP_FIELDS, _http_entry, "api-client"
        elif kind in ("stdio", "local"):
            fields, build, execution = _STDIO_FIELDS, _stdio_entry, "hosted-stdio"
        elif kind == "toolbox":
            fields, build, execution = _TOOLBOX_FIELDS, _toolbox_entry, "foundry-toolbox"
        else:
            raise CatalogError("CATALOG_UNSUPPORTED_ENTRY", "type must be http, stdio, local or toolbox")
        unknown = set(config) - fields
        if unknown:
            raise CatalogError("CATALOG_FORMAT", "unsupported fields: " + ", ".join(sorted(unknown)))
        tools = _tools(config.get("tools"), server)
        entry = build(config)
        if isinstance(entry, HttpEntry) and not any(_SECRET_REFERENCE.fullmatch(value) for _, value in entry.headers):
            raise CatalogError("CATALOG_UNSUPPORTED_ENTRY",
                               "an http entry without a secret reference is pending evaluation and is not loaded")
        material = {"entry_id": server, "execution": execution, "tools": list(tools), "config": entry.model_dump(mode="json")}
        return CatalogEntry(entry_id=server, execution=execution, tools=tools, config=entry,
                            entry_sha256=hashlib.sha256(_canonical(material)).hexdigest())
    except CatalogError as error:
        raise CatalogError(error.code, f"{filename}: {error}") from None
    except (ValueError, RecursionError, UnicodeError):
        # Unexpected parser or model errors may quote input; never surface them.
        raise CatalogError("CATALOG_FORMAT", f"{filename}: catalog entry is malformed") from None


def build_catalog(entries: list[CatalogEntry] | tuple[CatalogEntry, ...]) -> ToolCatalog:
    ordered = tuple(sorted(entries, key=lambda item: item.entry_id))
    if len({item.entry_id for item in ordered}) != len(ordered):
        raise CatalogError("CATALOG_DUPLICATE", "two catalog files define the same server name")
    if len(ordered) > MAX_CATALOG_ENTRIES:
        raise CatalogError("CATALOG_FORMAT", f"catalog accepts at most {MAX_CATALOG_ENTRIES} servers")
    digest = hashlib.sha256(_canonical([[item.entry_id, item.entry_sha256] for item in ordered])).hexdigest()
    return ToolCatalog(entries=ordered, catalog_sha256=digest)


def load_catalog(directory: Path) -> ToolCatalog:
    """Load every ``*.json`` regular file; one rejected file rejects the whole catalog."""
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise CatalogError("CATALOG_FORMAT", "catalog directory must be an existing real directory")
    entries = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.suffix.lower() != ".json":
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise CatalogError("CATALOG_FORMAT", f"{path.name}: catalog files must be regular files, not links")
            if info.st_size > MAX_ENTRY_FILE_BYTES:
                raise CatalogError("CATALOG_FORMAT", f"{path.name}: catalog file exceeds its byte bound")
            raw = path.read_bytes()
        except OSError:
            raise CatalogError("CATALOG_FORMAT", f"{path.name}: catalog file could not be read") from None
        entries.append(parse_entry_file(path.name, raw))
    return build_catalog(entries)


def resolve_headers(entry: CatalogEntry, lookup: Callable[[str], str | None]) -> dict[str, str]:
    """API-side only: resolve secret references for one request; never log or return the values elsewhere."""
    if not isinstance(entry.config, HttpEntry) or entry.execution != "api-client":
        raise Problem(403, "CAPABILITY_EXECUTION_DENIED", "only API-client catalog entries resolve secrets")
    resolved = {}
    for name, value in entry.config.headers:
        match = _SECRET_REFERENCE.fullmatch(value)
        if match is None:
            resolved[name] = value
            continue
        secret = lookup(match.group(1))
        if not isinstance(secret, str) or not secret:
            raise Problem(503, "CAPABILITY_NEEDS_AUTH",
                          "a configured server-side secret for this capability is unavailable",
                          capability=entry.entry_id)
        resolved[name] = secret
    return resolved
