from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repair.broker import InferenceBroker
from repair.propose import check_patch, source_path


class RepairPolicyTests(unittest.TestCase):
    def test_protected_paths_are_always_refused(self):
        for path in (".github/workflows/ci.yml", "src/example.test.ts", "scripts/build.ts",
                     "src/../tests/index.ts", "package.json", "src/install.sh", "src/.env"):
            with self.subTest(path=path):
                self.assertFalse(source_path(path, ["*"]))
        self.assertTrue(source_path("src/index.ts", ["src/*.ts"]))

    def test_patch_must_stay_inside_selected_files(self):
        with tempfile.TemporaryDirectory() as folder:
            git = ["git", "-C", folder, "-c", "user.name=Autorepo", "-c", "user.email=ci@example.invalid"]
            subprocess.run([*git, "init", "-q"], check=True)
            path = Path(folder, "app.ts")
            path.write_text("export const defaultValue = 1;\n")
            subprocess.run([*git, "add", "app.ts"], check=True)
            subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
            path.write_text("export const defaultValue = 2;\n")
            patch = subprocess.check_output([*git, "diff"], text=True)
            subprocess.run([*git, "restore", "app.ts"], check=True)
            self.assertEqual(check_patch(folder, patch, "HEAD", ["app.ts"]), patch)
            with self.assertRaisesRegex(ValueError, "protected"):
                check_patch(folder, patch, "HEAD", ["other.ts"])
            self.assertIn("= 1", path.read_text())

    def test_broker_caps_completion_and_request_count(self):
        with patch("socketserver.ThreadingUnixStreamServer.__init__", return_value=None):
            broker = InferenceBroker("unused.sock", "dummy", "test", 2, 80)
        self.assertEqual(broker.reserve({"max_tokens": 30}), 30)
        self.assertEqual(broker.reserve({"max_tokens": 400}), 50)
        self.assertIsNone(broker.reserve({"max_tokens": 1}))


if __name__ == "__main__":
    unittest.main()
