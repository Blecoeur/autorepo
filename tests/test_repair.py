import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from repair.propose import (attempt_number, blocked, build_prompt, collect_patch,
                            matches, read_policy, write_request, vibe_usage)


class FakeGitHub:
    def __init__(self, policy):
        self.requests = []
        self.policy = policy

    def request(self, endpoint):
        self.requests.append(endpoint)
        if endpoint.startswith("contents/.github/autorepo-repair.json"):
            content = base64.b64encode(json.dumps(self.policy).encode()).decode()
            return {"content": content}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    def pages(self, endpoint, key, **filters):
        return []


class BlocklistTests(unittest.TestCase):
    def test_glob_segments_and_double_star(self):
        self.assertTrue(matches(".github/workflows/ci.yml", ".github/**"))
        self.assertTrue(matches("src/deep/a.test.ts", "**/*.test.*"))
        self.assertTrue(matches("package.json", "package.json"))
        self.assertFalse(matches("nested/package.json", "*"))
        self.assertTrue(matches("package.json", "*"))
        self.assertTrue(matches("a/b/c.txt", "**"))
        self.assertFalse(matches("src/index.ts", "src/*.js"))

    def test_blocked_paths_drive_the_check(self):
        policy = {"blocked_paths": [".github/**", "**/*.test.*", "package.json"]}
        for path in (".github/workflows/ci.yml", "src/example.test.ts", "package.json"):
            with self.subTest(path=path):
                self.assertTrue(blocked(path, policy))
        for path in ("src/index.ts", "pnpm-lock.yaml", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(blocked(path, policy))

    def test_policy_validation(self):
        client = FakeGitHub({"enabled": True, "allow_bash": True,
                             "blocked_paths": [".github/**"],
                             **{key: 1 for key in ("max_attempts", "max_turns", "max_tokens",
                                                   "timeout_seconds", "budget_cents")}})
        policy = read_policy(client, "main")
        self.assertEqual(policy["blocked_paths"], [".github/**"])
        for mutation in ({"enabled": False}, {"allow_bash": "yes"},
                         {"blocked_paths": []}, {"blocked_paths": ".github/**"},
                         {"blocked_paths": [".github/**", 7]}, {"max_turns": 0},
                         {"max_tokens": 100000}, {"budget_cents": "5"}):
            with self.subTest(mutation=mutation):
                broken = {**client.policy, **mutation}
                self.assertRaises(ValueError, read_policy, FakeGitHub(broken), "main")


class RepairRequestTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"enabled": True, "allow_bash": False, "blocked_paths": [".github/**"],
                       "max_attempts": 2, "max_turns": 4, "max_tokens": 800,
                       "timeout_seconds": 300, "budget_cents": 50}

    def test_prompt_states_the_blocklist_and_hints(self):
        client = FakeGitHub(self.policy)
        client.request = lambda endpoint: ({"content": base64.b64encode(json.dumps(self.policy).encode()).decode()}
                                           if endpoint.startswith("contents/.github/autorepo-repair.json")
                                           else {"behind_by": 0, "ahead_by": 2,
                                                 "files": [{"filename": "package.json",
                                                            "patch": "+\"x\": \"1\""}]})
        env = {"REPAIR_PATHS": "src/a.ts src/b.ts src/a.ts", "REPAIR_DIAGNOSIS":
               "The upgrade renamed the exported helper."}
        prompt = build_prompt(client, env, "base", "head", 42, self.policy)
        self.assertIn(json.dumps(self.policy["blocked_paths"]), prompt)
        self.assertIn(json.dumps(["src/a.ts", "src/b.ts"]), prompt)
        self.assertIn("GitHub Actions run 42", prompt)
        self.assertIn("renamed the exported helper", prompt)
        self.assertIn("Dependency changes:", prompt)

    def test_hints_and_diagnosis_are_validated(self):
        client = FakeGitHub(self.policy)
        env = {"REPAIR_PATHS": ".github/workflows/ci.yml", "REPAIR_DIAGNOSIS": "x" * 20}
        self.assertRaises(ValueError, build_prompt, client, env, "base", "head", 1, self.policy)
        env = {"REPAIR_PATHS": "", "REPAIR_DIAGNOSIS": "short"}
        self.assertRaises(ValueError, build_prompt, client, env, "base", "head", 1, self.policy)

    def test_request_files_carry_budgets_and_tool_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {"REPAIR_PROMPT": str(Path(folder) / "prompt.txt"),
                   "REPAIR_ARGS": str(Path(folder) / "args")}
            write_request(env, "repair the code", self.policy)
            self.assertEqual((Path(env["REPAIR_PROMPT"])).read_text(), "repair the code")
            args = (Path(env["REPAIR_ARGS"])).read_text().split()
            self.assertEqual(args[:2], ["--agent", "auto-approve"])
            self.assertEqual(args[args.index("--max-turns") + 1], "4")
            self.assertEqual(args[args.index("--max-price") + 1], "0.5")
            self.assertIn("bash", args)
            with_bash = {**self.policy, "allow_bash": True}
            write_request(env, "repair the code", with_bash)
            self.assertNotIn("bash", (Path(env["REPAIR_ARGS"])).read_text().split())

    def test_attempt_limit_counts_previous_runs(self):
        client = FakeGitHub({**self.policy, "max_attempts": 4})
        client.pages = lambda endpoint, key, **filters: [
            {"id": 1, "display_title": "Autorepo repair #7 h b", "run_attempt": 2},
            {"id": 2, "display_title": "Autorepo repair #7 h b", "run_attempt": 1},
            {"id": 9, "display_title": "Autorepo repair #7 h b", "run_attempt": 1},
        ]
        env = {"GITHUB_RUN_ID": "5", "REPAIR_PR": "7", "GITHUB_RUN_ATTEMPT": "1"}
        capped = client.policy
        self.assertEqual(attempt_number(client, env, "b", "b", "h", ".github/workflows/r.yml",
                                        capped), 4)
        env["GITHUB_RUN_ID"] = "10"
        self.assertRaises(ValueError, attempt_number, client, env, "b", "b", "h",
                          ".github/workflows/r.yml", capped)


class CollectPatchTests(unittest.TestCase):
    policy = {"blocked_paths": [".github/**", "**/*.test.*"]}

    def fixture(self):
        root = tempfile.TemporaryDirectory(prefix="autorepo-test-")
        self.addCleanup(root.cleanup)
        candidate = Path(root.name) / "candidate"
        candidate.mkdir()
        git = ["git", "-C", candidate, "-c", "user.name=Autorepo", "-c", "user.email=ci@example.invalid"]
        subprocess.run([*git, "init", "-q"], check=True)
        for name, body in {"src/index.ts": "export const value = 1;\n",
                           ".github/workflows/ci.yml": "name: CI\n",
                           "package.json": '{"name":"test"}\n'}.items():
            path = candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "fixture"], check=True)
        return candidate

    def test_accepts_application_changes(self):
        candidate = self.fixture()
        (candidate / "src/index.ts").write_text("export const value = 2;\n")
        (candidate / "src/new.ts").write_text("export const created = true;\n")
        patch = collect_patch(str(candidate), "HEAD", self.policy)
        self.assertIn("diff --git a/src/index.ts b/src/index.ts", patch)
        self.assertIn("diff --git a/src/new.ts b/src/new.ts", patch)

    def test_rejects_blocked_paths_and_modes(self):
        for change in ("blocked-edit", "blocked-new", "blocked-delete", "mode", "untouched"):
            candidate = self.fixture()
            if change == "blocked-edit":
                (candidate / ".github/workflows/ci.yml").write_text("name: Evil\n")
            elif change == "blocked-new":
                (candidate / ".github/workflows/evil.yml").write_text("on: push\n")
            elif change == "blocked-delete":
                (candidate / ".github/workflows/ci.yml").unlink()
            elif change == "mode":
                (candidate / "src/index.ts").chmod(0o755)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "blocked path|mode|no text changes"):
                collect_patch(str(candidate), "HEAD", self.policy)

    def test_deletion_of_application_files_is_allowed(self):
        candidate = self.fixture()
        (candidate / "src/index.ts").unlink()
        patch = collect_patch(str(candidate), "HEAD", self.policy)
        self.assertIn("diff --git a/src/index.ts b/src/index.ts", patch)
        self.assertIn("deleted file", patch)

    def test_diff_is_taken_against_the_verified_head(self):
        candidate = self.fixture()
        head = subprocess.check_output(["git", "-C", candidate, "rev-parse", "HEAD"],
                                       text=True).strip()
        git = ["git", "-C", candidate, "-c", "user.name=Autorepo", "-c", "user.email=ci@example.invalid"]
        (candidate / "src/index.ts").write_text("export const value = 2;\n")
        subprocess.run([*git, "add", "src/index.ts"], check=True)
        subprocess.run([*git, "commit", "-qm", "vibe commit"], check=True)
        (candidate / "src/index.ts").write_text("export const value = 3;\n")
        env_patch = collect_patch(str(candidate), head, self.policy)
        self.assertIn("value = 3", env_patch)
        self.assertEqual(env_patch.count("diff --git"), 1)

    def test_usage_is_read_from_the_vibe_export(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(vibe_usage(folder), {})
            (Path(folder) / "export.json").write_text(json.dumps(
                {"model": "mistral", "usage": {"prompt_tokens": 5}, "cost": 0.01}))
            self.assertEqual(vibe_usage(folder), {"model": "mistral",
                                                 "usage": {"prompt_tokens": 5}, "cost": 0.01})


if __name__ == "__main__":
    unittest.main()
