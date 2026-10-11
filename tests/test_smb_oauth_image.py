from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"


class SmbOauthImageTests(unittest.TestCase):
    def test_package_and_base_are_pinned_and_sdk_install_cannot_fallback(self) -> None:
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        self.assertIn("@sha256:c3861e70477956cd341547ad5d75fc8c84a8bf7f6da36aa7fd4d771104563bfe", image)
        self.assertIn("https://packages.microsoft.com/ubuntu/22.04/prod/pool/main/a/azfilesauth/azfilesauth_1.0-12_amd64.deb", image)
        self.assertIn("6138d2c4c5ba3c1272bce7fbfd1e60cd236fbfed911b4968275e58cdaee7d5af", image)
        self.assertIn("sha256sum --check --strict", image)
        self.assertIn("AZFILESAUTH_SKIP_PIP_INSTALL=1", image)
        self.assertNotIn("systemctl", image)
        self.assertNotIn("POC_SMB_AUTH_MODE", image)
        self.assertEqual((ROOT / "requirements-smb-oauth.txt").read_text(), "PyYAML==6.0.3\n")

    def test_build_executes_real_abi_and_cli_checks_without_credentials(self) -> None:
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        self.assertIn("COPY poc_core.py smb_oauth.py check_smb_oauth_image.py /app/", image)
        self.assertIn("python /app/check_smb_oauth_image.py", image)
        checker = (ROOT / "check_smb_oauth_image.py").read_text()
        self.assertIn("native = NativeLibrary()", checker)
        self.assertNotIn("ctypes.CDLL", checker)
        self.assertIn('version == "1.0.12"', checker)
        self.assertIn("check=True", checker)
        self.assertIn('["azfilesauthmanager", "--version"]', checker)
        self.assertIn('["/usr/local/sbin/cifs.upcall", "--version"]', checker)
        self.assertIn("image_abi_cli_only_not_hosted_mount", checker)
        self.assertNotIn("get_token(", checker)
        self.assertNotIn("extern_smb_set_credential_oauth_token(", checker)
        self.assertNotIn("mount_shared", checker)

    def test_cache_and_configuration_are_private_and_not_image_credentials(self) -> None:
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        self.assertIn("chmod 644 /etc/azfilesauth/config.yaml", image)
        self.assertIn("USER_UID: 10002", image)
        self.assertIn("KRB5_CC_NAME: FILE:/tmp/krb5cc_10002", image)
        self.assertIn("LOG_FILE_PATH: /dev/null", image)
        self.assertIn("CURL_TOTAL_TIMEOUT: 30", image)
        self.assertNotIn("get-access-token", image)
        self.assertNotIn("client-secret", image)

    def test_upcall_has_service_ticket_and_old_kernel_fixes_without_replacing_mount(self) -> None:
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        self.assertIn("edac7178bec9520fb57d14946e67f5dd33b82d43", image)
        self.assertIn("ea161bdf224d77a76a1da9475be7a761bbb9ff6d2a37bd13fd7e366595bee876", image)
        self.assertIn("make -j2 cifs.upcall", image)
        self.assertIn("COPY --from=upcall-build /out/cifs.upcall /usr/local/sbin/cifs.upcall", image)
        self.assertNotIn("make install", image)
        self.assertNotIn("for-next", image)

    def test_upcall_version_flag_matches_pinned_upstream_cli(self) -> None:
        # edac7178 cifs.upcall.c:1368,1399,1423: --version maps to lowercase v, not V.
        supported_short_options = "cEk:K:ltve:"
        self.assertIn("v", supported_short_options)
        self.assertNotIn("V", supported_short_options)
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        checker = (ROOT / "check_smb_oauth_image.py").read_text()
        self.assertIn("./cifs.upcall --version", image)
        self.assertNotIn("./cifs.upcall -V", image)
        self.assertIn('["/usr/local/sbin/cifs.upcall", "--version"]', checker)
        self.assertNotIn('["/usr/local/sbin/cifs.upcall", "-V"]', checker)

    def test_package_postinst_uses_image_python_without_upgrading_sdk(self) -> None:
        image = (ROOT / "Dockerfile.smb-oauth").read_text()
        self.assertIn("python -m pip install", image)
        self.assertIn("import sys, azure.identity, azure.core, yaml", image)
        self.assertIn("DPkg::Path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", image)
        self.assertIn("AZFILESAUTH_SKIP_PIP_INSTALL=1", image)
        self.assertNotIn("--break-system-packages", image)

    def test_mount_version_does_not_require_builder_mount_capabilities(self) -> None:
        checker = (ROOT / "check_smb_oauth_image.py").read_text()
        self.assertIn('["dpkg-query", "--show",', checker)
        self.assertNotIn('["mount.cifs", "-V"]', checker)
        self.assertIn('shutil.which("mount.cifs")', checker)
        self.assertIn("except subprocess.CalledProcessError as exc:", checker)
        self.assertIn("stderr={exc.stderr[-1000:]!r}", checker)


if __name__ == "__main__":
    unittest.main()
