import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


class PrivateCollectionTests(unittest.TestCase):
    def test_export_contract(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "invoke-private-controller.ps1"
        ).read_text(encoding="utf-8")
        export = source.split("$export = @'\n", 1)[1].split("\n'@", 1)[0]
        python = export.split("\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            for run in ("current", "older"):
                (evidence / run).mkdir(parents=True)
                (evidence / run / "console.sse").write_text(run, encoding="utf-8")
            (evidence / "agent-version.json").write_text("{}", encoding="utf-8")
            for selector, expected, success in (
                ("current", {"agent-version.json", "current/console.sse"}, True),
                ("", {"agent-version.json", "current/console.sse", "older/console.sse"}, True),
                ("missing", set(), False),
                ("../older", set(), False),
            ):
                with self.subTest(selector=selector):
                    code = python.replace(
                        'Path("/opt/fasample-controller")', f"Path({str(root)!r})"
                    ).replace("__COLLECT_RUN_ID__", selector)
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    if not success:
                        self.assertEqual(result.stdout, "")
                        continue
                    metadata = json.loads(result.stdout)
                    archive_path = root / "evidence-export.zip"
                    data = archive_path.read_bytes()
                    self.assertEqual(metadata["size"], len(data))
                    self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
                    with zipfile.ZipFile(archive_path) as archive:
                        self.assertEqual(set(archive.namelist()), expected)


if __name__ == "__main__":
    unittest.main()
