from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import stat
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"))

from smb_oauth import (  # noqa: E402
    CACHE_NAME, CREDENTIAL_UID, NativeLibrary, SmbOAuthError, SmbOAuthSession,
    STORAGE_AUDIENCE, STORAGE_SCOPE, parse_config, token_claims,
)
import smb_oauth

OID = "00000000-0000-0000-0000-000000000011"
TID = "00000000-0000-0000-0000-000000000013"
NOW = 1_800_000_000
CONFIG = """USER_UID: 10002
KRB5_CC_NAME: FILE:/tmp/krb5cc_10002
LOG_DESTINATION: file
LOG_FILE_PATH: /dev/null
CURL_CONNECT_TIMEOUT: 10
CURL_TOTAL_TIMEOUT: 30
"""


def token(**claims):
    payload = {"aud": STORAGE_AUDIENCE, "oid": OID, "tid": TID,
               "exp": NOW + 3600, "nbf": NOW - 10, **claims}
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value).encode()).rstrip(b"=").decode()
    # Deliberately unsigned test fixture; these tests do not authenticate to Azure.
    return f'{encode({"alg": "RS256"})}.{encode(payload)}.c3ludGhldGlj'


class TokenTests(unittest.TestCase):
    def test_exact_scope_audience_and_minimal_claims(self):
        claims, expiry = token_claims(
            token(upn="not-for-evidence", roles=["not-for-evidence"]),
            NOW + 3600, OID, TID, NOW,
        )
        self.assertEqual(STORAGE_SCOPE, "https://storage.azure.com/.default")
        self.assertNotIn("//.default", STORAGE_SCOPE)
        self.assertEqual(claims, {"aud": "https://storage.azure.com", "oid": OID, "tid": TID})
        self.assertEqual(expiry, NOW + 3600)

    def test_wrong_audience_expiry_and_principal_fail_without_token_disclosure(self):
        for changes in (
            {"aud": STORAGE_AUDIENCE + "/"}, {"aud": ["https://storage.azure.com"]},
            {"exp": NOW}, {"exp": True}, {"nbf": NOW + 30},
            {"oid": TID}, {"tid": OID}, {"oid": "invalid"},
        ):
            with self.subTest(changes=changes):
                value = token(**changes)
                with self.assertRaises(SmbOAuthError) as caught:
                    token_claims(value, NOW + 3600, OID, TID, NOW)
                self.assertNotIn(value, str(caught.exception) + json.dumps(caught.exception.details))

    def test_malformed_tokens_and_sdk_expiry_fail_closed(self):
        for value in ("", "not-a-jwt", "a.b.c", "a..b", token() + "\n", None, "x" * 32769):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(SmbOAuthError):
                    token_claims(value, NOW + 3600, OID, TID, NOW)
        with self.assertRaises(SmbOAuthError):
            token_claims(token(), NOW, OID, TID, NOW)


class ConfigTests(unittest.TestCase):
    def test_expected_nonsecret_config(self):
        config = parse_config(CONFIG)
        self.assertEqual(config["USER_UID"], CREDENTIAL_UID)
        self.assertEqual(config["KRB5_CC_NAME"], CACHE_NAME)

    def test_ambiguous_or_unsafe_config_is_rejected(self):
        for text in (
            CONFIG + "USER_UID: 10002\n",
            CONFIG + "ENVIRONMENT: {TOKEN: do-not-log}\n",
            CONFIG.replace("10002", "10003"),
            CONFIG.replace("/dev/null", "/tmp/transport.log"),
            CONFIG.replace("TOTAL_TIMEOUT: 30", "TOTAL_TIMEOUT: 300"),
            CONFIG.replace("FILE:/tmp/krb5cc_10002", "FILE:/team/cache"),
            CONFIG.replace("USER_UID: 10002", 'USER_UID: "10002"'),
            CONFIG.replace("USER_UID: 10002", "USER_UID: 0x2712"),
            CONFIG.replace("USER_UID: 10002", "USER_UID: &uid 10002"),
            CONFIG.replace("USER_UID: 10002", "USER_UID: !!int 10002"),
            CONFIG + "TOKEN: sensitive-value\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(SmbOAuthError):
                    parse_config(text)

    def test_config_is_root_controlled_and_readable_after_native_seteuid(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1)
        pwd = ModuleType("pwd")
        pwd.getpwuid = Mock(return_value=SimpleNamespace(pw_name="azfilesuser"))
        handle = Mock()
        handle.read.return_value = CONFIG
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"pwd": pwd}))
            stack.enter_context(patch.object(os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(os, "O_NOFOLLOW", 131072, create=True))
            stack.enter_context(patch.object(os, "O_NONBLOCK", 2048, create=True))
            stack.enter_context(patch.object(os, "makedev", return_value=259, create=True))
            stack.enter_context(patch("smb_oauth._trusted_parents"))
            stack.enter_context(patch("smb_oauth.os.open", return_value=123))
            opened = stack.enter_context(patch("smb_oauth.os.fdopen"))
            opened.return_value.__enter__.return_value = handle
            stack.enter_context(patch("smb_oauth.os.fstat", return_value=info))
            stack.enter_context(patch.object(Path, "lstat", return_value=SimpleNamespace(
                st_mode=stat.S_IFCHR, st_rdev=259, st_uid=0,
            )))
            smb_oauth.validate_config()
            handle.read.assert_called_with(8193)
            for mode, uid in ((0o600, 0), (0o666, 0), (0o644, 10002)):
                info.st_mode, info.st_uid = stat.S_IFREG | mode, uid
                with self.subTest(mode=mode, uid=uid), self.assertRaises(SmbOAuthError):
                    smb_oauth.validate_config()


class CacheTests(unittest.TestCase):
    def test_private_regular_cache_metadata_only(self):
        path = Mock()
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=10002, st_nlink=1,
                               st_size=100, st_dev=1, st_ino=2, st_mtime_ns=3)
        path.lstat.return_value = info
        with patch("smb_oauth.CACHE_PATH", path), patch("smb_oauth._trusted_parents"):
            self.assertTrue(smb_oauth.cache_state()["nonempty"])
            for field, value in (
                ("st_uid", 0), ("st_mode", stat.S_IFREG | 0o644),
                ("st_mode", stat.S_IFLNK | 0o600), ("st_nlink", 2),
            ):
                previous = getattr(info, field)
                setattr(info, field, value)
                with self.subTest(field=field, value=value), self.assertRaises(SmbOAuthError):
                    smb_oauth.cache_state()
                setattr(info, field, previous)
            path.lstat.side_effect = FileNotFoundError()
            self.assertEqual(smb_oauth.cache_state(), {"present": False})
        path.open.assert_not_called()
        path.read_bytes.assert_not_called()

    def test_only_root_owned_sticky_tmp_can_be_writable(self):
        def metadata(path):
            return SimpleNamespace(st_mode=stat.S_IFDIR | (0o1777 if path == Path("/tmp") else 0o755), st_uid=0)

        with patch.object(Path, "lstat", metadata):
            smb_oauth._trusted_parents(Path("/tmp/krb5cc_10002"), cache=True)
            with self.assertRaises(SmbOAuthError):
                smb_oauth._trusted_parents(Path("/tmp/config.yaml"))
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)):
            with self.assertRaises(SmbOAuthError):
                smb_oauth._trusted_parents(Path("/tmp/krb5cc_10002"), cache=True)

    def test_native_log_fd_fixture_requires_append_write_cloexec_null_sink(self):
        fcntl = ModuleType("fcntl")
        fcntl.F_GETFL, fcntl.F_GETFD, fcntl.FD_CLOEXEC = 3, 1, 1
        flags = {10: os.O_WRONLY | os.O_APPEND, 11: os.O_RDONLY, 12: os.O_WRONLY | os.O_APPEND}
        fcntl.fcntl = Mock(side_effect=lambda fd, op: flags[fd] if op == 3 else 1)
        paths = [Path(f"/proc/self/fd/{fd}") for fd in flags]
        with (
            patch.dict(sys.modules, {"fcntl": fcntl}),
            patch.object(os, "O_ACCMODE", 3, create=True),
            patch.object(Path, "iterdir", return_value=iter(paths)),
            patch("smb_oauth.os.readlink", side_effect=lambda p: "/tmp/private-cache" if p.name == "12" else "/dev/null"),
        ):
            self.assertEqual(smb_oauth.native_log_fds(), {10})
        self.assertFalse(any(call.args[0] == 12 for call in fcntl.fcntl.call_args_list))
        with (
            patch.dict(sys.modules, {"fcntl": fcntl}),
            patch.object(Path, "iterdir", side_effect=PermissionError("not evidence")),
        ):
            with self.assertRaises(SmbOAuthError) as caught:
                smb_oauth.native_log_fds()
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_LOG_SINK_UNVERIFIED")


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.checked = self.stack.enter_context(patch("smb_oauth.validate_config"))
        self.stack.enter_context(patch("smb_oauth.validate_library"))
        self.log_fds = self.stack.enter_context(patch("smb_oauth.native_log_fds", side_effect=[set(), {123}]))
        self.euid = self.stack.enter_context(patch.object(os, "geteuid", return_value=0, create=True))
        self.library = SimpleNamespace(
            extern_smb_set_credential_oauth_token=Mock(),
            extern_smb_clear_credential=Mock(return_value=0),
            extern_smb_version=Mock(return_value=b"1.0.12"),
        )
        self.loader = Mock(side_effect=self.load)
        self.native = NativeLibrary(loader=self.loader)
        self.log_fds.side_effect = None
        self.log_fds.return_value = {123}

    def load(self, path, **kwargs):
        self.checked.assert_called()
        return self.library

    def test_exact_ctypes_abi_and_in_memory_buffer_erasure(self):
        pointers = []

        def set_credential(endpoint, secret, expires):
            self.assertEqual(endpoint, b"https://testaccount.file.core.windows.net")
            self.assertEqual(ctypes.string_at(secret).decode(), token())
            pointers.append(secret)
            ctypes.cast(expires, ctypes.POINTER(ctypes.c_uint))[0] = 3600
            return 0

        self.native.set_function.side_effect = set_credential
        self.assertEqual(self.native.set("https://testaccount.file.core.windows.net", token()), 3600)
        self.assertEqual(ctypes.string_at(pointers[0]), b"")
        self.assertEqual(self.native.set_function.argtypes,
                         [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)])
        self.assertIs(self.native.set_function.restype, ctypes.c_int)
        self.assertEqual(self.native.clear_function.argtypes, [ctypes.c_char_p])
        self.assertIs(self.native.clear_function.restype, ctypes.c_int)
        self.assertEqual(self.native.version_function.argtypes, [])
        self.assertIs(self.native.version_function.restype, ctypes.c_char_p)
        self.loader.assert_called_once_with(str(smb_oauth.LIBRARY_PATH), use_errno=True)
        self.native.clear("https://testaccount.file.core.windows.net")
        self.native.clear_function.assert_called_once_with(b"https://testaccount.file.core.windows.net")

    def test_native_failure_and_exception_do_not_disclose_token(self):
        for failure in (7, RuntimeError(token())):
            self.native.set_function.side_effect = None
            self.native.set_function.return_value = failure
            if isinstance(failure, Exception):
                self.native.set_function.side_effect = failure
            with self.subTest(failure_type=type(failure).__name__), self.assertRaises(SmbOAuthError) as caught:
                self.native.set("https://testaccount.file.core.windows.net", token())
            self.assertNotIn(token(), str(caught.exception) + json.dumps(caught.exception.details))
        self.native.clear_function.return_value = -1
        with self.assertRaises(SmbOAuthError) as caught:
            self.native.clear("https://testaccount.file.core.windows.net")
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_CLEAR_FAILED")

    def test_native_exception_still_erases_our_token_buffer(self):
        pointers = []

        def fail(endpoint, secret, expires):
            pointers.append(secret)
            raise RuntimeError(token())

        self.native.set_function.side_effect = fail
        with self.assertRaises(SmbOAuthError):
            self.native.set("https://testaccount.file.core.windows.net", token())
        self.assertEqual(ctypes.string_at(pointers[0]), b"")

    def test_loading_version_and_effective_uid_fail_closed(self):
        with self.assertRaises(SmbOAuthError) as caught:
            NativeLibrary(loader=Mock(side_effect=OSError(token())))
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_LIBRARY_UNAVAILABLE")
        self.assertNotIn(token(), str(caught.exception))
        self.library.extern_smb_version.return_value = b"other-version"
        self.log_fds.side_effect = [set(), {123}]
        with self.assertRaises(SmbOAuthError) as caught:
            NativeLibrary(loader=self.loader)
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_VERSION_MISMATCH")
        self.log_fds.side_effect = None
        self.euid.side_effect = [0, 10002]
        with self.assertRaises(SmbOAuthError) as caught:
            self.native.clear("https://testaccount.file.core.windows.net")
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_IDENTITY_NOT_RESTORED")

    def test_unverified_or_lost_native_log_sink_blocks_credentials(self):
        with self.assertRaises(SmbOAuthError) as caught:
            NativeLibrary(loader=self.loader)
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_LOG_SINK_UNVERIFIED")
        self.log_fds.return_value = set()
        with self.assertRaises(SmbOAuthError):
            self.native.set("https://testaccount.file.core.windows.net", token())
        self.native.set_function.assert_not_called()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("smb_oauth.validate_config"))
        self.cache = {"present": False}
        self.stack.enter_context(patch("smb_oauth.cache_state", side_effect=lambda: dict(self.cache)))
        self.remove = self.stack.enter_context(patch("smb_oauth.remove_cache_file", side_effect=self.remove_cache))
        self.native = Mock(version="1.0.12")
        self.native.set.side_effect = self.set_cache
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(token=token(), expires_on=NOW + 3600)
        self.now = NOW
        self.session = SmbOAuthSession(
            "testaccount", OID, TID, credential_factory=lambda: self.credential,
            native_factory=lambda: self.native, clock=lambda: self.now,
        )

    def set_cache(self, endpoint, value):
        self.cache = {"present": True, "nonempty": True, "owner_uid": 10002, "mode": "0600", "inode": 123}
        return 3600

    def remove_cache(self):
        self.cache = {"present": False}

    def test_request_driven_reuse_refresh_and_missing_cache(self):
        first = self.session.ensure()
        self.credential.get_token.assert_called_once_with(STORAGE_SCOPE, logging_enable=False)
        self.native.set.assert_called_once_with("https://testaccount.file.core.windows.net", token())
        self.assertEqual(first["claims"], {"aud": STORAGE_AUDIENCE, "oid": OID, "tid": TID})
        self.assertNotIn(token(), json.dumps(first) + repr(vars(self.session)))
        self.assertEqual(self.session.ensure()["refresh_action"], "reused")
        self.native.set.assert_called_once()
        self.now += 3350
        self.assertEqual(self.session.ensure()["refresh_action"], "refreshed")
        self.assertEqual(self.native.set.call_count, 2)
        self.remove_cache()
        self.session.ensure()
        self.assertEqual(self.native.set.call_count, 3)
        self.session.ensure(force=True)
        self.assertEqual(self.native.set.call_count, 4)

    def test_sdk_and_bad_token_failures_never_reach_native_set(self):
        for outcome in (RuntimeError(token()), SimpleNamespace(token=token(aud="wrong"), expires_on=NOW + 3600)):
            self.credential.get_token.side_effect = outcome if isinstance(outcome, Exception) else None
            self.credential.get_token.return_value = outcome
            with self.subTest(kind=type(outcome).__name__), self.assertRaises(SmbOAuthError) as caught:
                self.session.ensure()
            self.assertNotIn(token(), str(caught.exception) + json.dumps(caught.exception.details))
            self.native.set.assert_not_called()

    def test_sdk_exception_logging_is_scrubbed_and_other_threads_are_unchanged(self):
        logger = logging.getLogger("azure.identity.test-fixture")
        previous = logging.getLogRecordFactory()

        def get_token(*args, **kwargs):
            thread = threading.Thread(target=lambda: logger.warning("other-thread-observation"))
            thread.start()
            thread.join(2)
            try:
                raise RuntimeError(token())
            except RuntimeError:
                logger.warning("token request failed: %s", token(), exc_info=True)
                raise

        self.credential.get_token.side_effect = get_token
        with self.assertLogs(logger, level="WARNING") as logs:
            with self.assertRaises(SmbOAuthError):
                self.session.ensure()
        self.assertNotIn(token(), "\n".join(logs.output))
        self.assertIn("other-thread-observation", "\n".join(logs.output))
        self.assertIs(logging.getLogRecordFactory(), previous)

    def test_invalid_config_blocks_token_and_native_calls(self):
        with patch("smb_oauth.validate_config", side_effect=SmbOAuthError("SMB_AUTH_CONFIG_INVALID")):
            with self.assertRaises(SmbOAuthError):
                self.session.ensure()
        self.credential.get_token.assert_not_called()
        self.native.set.assert_not_called()
        self.native.clear.assert_not_called()

    def test_native_success_without_private_cache_or_lifetime_is_not_success(self):
        self.native.set.side_effect = None
        self.native.set.return_value = 3600
        with self.assertRaises(SmbOAuthError) as caught:
            self.session.ensure()
        self.assertEqual(caught.exception.reason_code, "SMB_CACHE_MISSING")
        self.native.set.return_value = 0
        with self.assertRaises(SmbOAuthError) as caught:
            self.session.ensure()
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_LIFETIME_INVALID")

    def test_cleanup_failure_stays_explicit_even_if_file_is_removed(self):
        self.session.ensure()
        self.native.clear.side_effect = SmbOAuthError("SMB_NATIVE_CLEAR_FAILED", return_code=-1)
        with self.assertRaises(SmbOAuthError) as caught:
            self.session.ensure(force=True)
        self.assertEqual(caught.exception.reason_code, "SMB_CACHE_CLEANUP_FAILED")
        self.assertTrue(caught.exception.details["cache_removed"])
        self.assertEqual(caught.exception.details["native_return_code"], -1)
        self.assertEqual(caught.exception.details["cleanup"], {"status": "failed", "not_retried": True})
        self.assertFalse(self.cache["present"])
        self.assertEqual(self.native.set.call_count, 1)
        self.native.clear.assert_called_once()

    def test_concurrent_refreshes_do_not_overlap_native_calls(self):
        entered, release = threading.Event(), threading.Event()
        active = 0
        maximum = 0
        errors = []

        def native_set(endpoint, value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test synchronization timed out")
            result = self.set_cache(endpoint, value)
            active -= 1
            return result

        def prepare():
            try:
                self.session.ensure(force=True)
            except Exception as exc:
                errors.append(type(exc).__name__)

        self.native.set.side_effect = native_set
        first, second = threading.Thread(target=prepare), threading.Thread(target=prepare)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        release.set()
        first.join(3)
        second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)
        self.assertEqual(self.native.set.call_count, 2)

    def test_credential_factory_is_hosted_managed_identity_only(self):
        azure, identity = ModuleType("azure"), ModuleType("azure.identity")
        azure.__path__ = []
        identity.ManagedIdentityCredential = Mock(return_value=self.credential)
        identity.DefaultAzureCredential = Mock(side_effect=AssertionError("no fallback"))
        identity.AzureCliCredential = Mock(side_effect=AssertionError("no operator credential"))
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}):
            self.assertIs(smb_oauth.managed_identity_credential(), self.credential)
        identity.ManagedIdentityCredential.assert_called_once_with(
            connection_timeout=5, read_timeout=10, retry_total=0, logging_enable=False,
        )
        identity.DefaultAzureCredential.assert_not_called()
        identity.AzureCliCredential.assert_not_called()


if __name__ == "__main__":
    unittest.main()
