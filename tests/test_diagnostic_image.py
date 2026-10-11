from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"
BASE = (
    "crcontosodemo01.azurecr.io/fa-hosted-sandbox-poc/"
    "fa-hosted-sandbox-poc-faexample"
    "@sha256:d6f8f7134abc4b2cf5089ecc2c5e1de2a6cf1b189bd962afb547f668c51bab01"
)


class DiagnosticImageTests(unittest.TestCase):
    def test_image_inherits_verified_packages_and_only_updates_probe(self):
        lines = (ROOT / "Dockerfile.diagnostics").read_text().splitlines()
        self.assertEqual([line for line in lines if line], [
            f"FROM {BASE}",
            "ENV POC_MOUNT_DIAGNOSTICS=1",
            "COPY poc_core.py /app/poc_core.py",
            "RUN python -m py_compile /app/poc_core.py \\",
            "    && dmesg --kernel --syslog --buffer-size=65536 --color=never --nopager --help > /dev/null \\",
            r"    && printf '<6>[   10.000000] CIFS: diagnostic fixture\n' > /tmp/fasample-dmesg-fixture " + "\\",
            "    && dmesg --kernel --buffer-size=65536 --color=never --nopager --file /tmp/fasample-dmesg-fixture > /tmp/fasample-dmesg-output \\",
            r"    && grep -Eq '^\[[[:space:]]*10\.000000\] CIFS: diagnostic fixture$' /tmp/fasample-dmesg-output " + "\\",
            "    && rm /tmp/fasample-dmesg-fixture /tmp/fasample-dmesg-output",
        ])

    def test_build_context_is_an_explicit_allowlist(self):
        self.assertEqual((ROOT / ".dockerignore").read_text().splitlines(), [
            "**", "!Dockerfile", "!Dockerfile.diagnostics", "!Dockerfile.smb-oauth",
            "!requirements.txt", "!requirements-smb-oauth.txt",
            "!main.py", "!poc_core.py", "!desktop_smoke.py", "!smb_oauth.py",
            "!check_smb_oauth_image.py",
            "!Dockerfile.smb-diagnostics", "!smb_diagnostics.py",
        ])


if __name__ == "__main__":
    unittest.main()
