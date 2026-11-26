import unittest
from unittest.mock import patch

from scheduler import BOT_ID, GitHub, auto_merge, identity, policy, reconcile, target_key
from validate.verify import verify

BASE = "b" * 40
REPO = "example/project"
CONFIG = {
    "branch": "trunk", "validation_workflow": "checks.yml",
    "version_groups": {"production-patch": 4}, "retry_minutes": 15,
    "max_attempts": 2, "update_timeout_minutes": 60, "pause_for_critical": False,
}


def pr(number, group="production-patch-a1b2c3d4", user=BOT_ID):
    return {
        "number": number, "draft": False, "base": {"ref": "trunk"},
        "user": {"id": user, "login": "dependabot[bot]"},
        "created_at": f"2026-01-{number:02d}T00:00:00Z",
        "head": {"ref": f"dependabot/gomod/{group}", "repo": {"full_name": REPO},
                 "sha": str(number) * 40},
    }


def alert(number, severity="CRITICAL"):
    return {
        "number": 1, "dependabotUpdate": {"pullRequest": {"number": number, "state": "OPEN"}},
        "securityAdvisory": {"ghsaId": "GHSA-aaaa-bbbb-cccc", "severity": severity},
        "securityVulnerability": {"firstPatchedVersion": {"identifier": "1.2"}},
    }


class FakeGitHub:
    repository = REPO

    def __init__(self, pulls, runs=(), behind=()):
        self.pulls, self.runs, self.behind = pulls, runs, behind
        self.writes = []

    def pages(self, path, **query):
        if path == "pulls":
            return iter(self.pulls)
        if path.endswith("/runs"):
            return (run for run in self.runs
                    if query.get("status", run["status"]) == run["status"]
                    and query.get("head_sha", run["head_sha"]) == run["head_sha"])
        if path.endswith("/statuses"):
            return iter([])
        raise AssertionError(path)

    def request(self, path, body=None, **kwargs):
        if body is not None:
            self.writes.append((path, body))
            return None
        if path.startswith("branches/"):
            return {"commit": {"sha": BASE}}
        if path.startswith("compare/"):
            return {"ahead_by": 1, "behind_by": any(f"...{n * 40}" in path for n in self.behind)}
        raise AssertionError(path)


def run(candidate, *, status="completed", conclusion="success", base=BASE, targets=()):
    return {"head_sha": base, "id": 10, "display_title": identity(candidate, base, targets),
            "status": status, "conclusion": conclusion,
            "created_at": "2026-01-22T00:00:00Z", "updated_at": "2026-01-22T00:00:00Z"}


class GitHubTests(unittest.TestCase):
    @patch("scheduler.urlopen")
    def test_repository_metadata_url_has_no_trailing_slash(self, urlopen):
        client = GitHub(REPO, "test-token")
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"default_branch":"trunk"}'
        result = client.request("")
        self.assertEqual(result, {"default_branch": "trunk"})
        self.assertEqual(urlopen.call_args.args[0].full_url,
                         f"{client.api_url}/repos/{REPO}")


class ReconcileTests(unittest.TestCase):
    def reconcile(self, github, alerts=(), config=CONFIG):
        messages = []
        reconcile(github, config, alerts, messages.append)
        return "\n".join(messages)

    def test_critical_precedes_regular_and_excludes_untrusted_pr(self):
        github = FakeGitHub([pr(1), pr(2), pr(3, user=42)])
        report = self.reconcile(github, [alert(2)])
        [(path, payload)] = github.writes
        self.assertEqual(path, "actions/workflows/checks.yml/dispatches")
        self.assertEqual(payload["inputs"]["pr"], "2")
        self.assertEqual(payload["inputs"]["advisories"], "GHSA-aaaa-bbbb-cccc")
        self.assertNotIn("PR #3", report)

    def test_pending_validation_on_old_base_prevents_dispatch(self):
        github = FakeGitHub([pr(1), pr(2)], [run(pr(2), base="a" * 40, status="in_progress",
                                                conclusion=None)])
        self.reconcile(github)
        self.assertFalse(github.writes)

    def test_updates_only_selected_stale_branch(self):
        github = FakeGitHub([pr(1), pr(2)], behind=("1", "2"))
        self.reconcile(github)
        self.assertEqual([p for p, _ in github.writes], [f"statuses/{'1' * 40}", "pulls/1/update-branch"])

    def test_failed_validation_does_not_block_backlog(self):
        github = FakeGitHub([pr(1), pr(2)], [run(pr(1), conclusion="failure")])
        report = self.reconcile(github)
        self.assertIn("validation failure", report)
        self.assertEqual(github.writes[0][1]["inputs"]["pr"], "2")

    def test_retry_budget_is_bound_to_validation_identity(self):
        first = run(pr(1), conclusion="timed_out")
        second = {**first, "id": 11}
        github = FakeGitHub([pr(1), pr(2)], [first, second])
        self.assertIn("budget exhausted", self.reconcile(github))
        self.assertEqual(github.writes[0][1]["inputs"]["pr"], "2")

    def test_success_on_old_base_cannot_be_reused(self):
        github = FakeGitHub([pr(1)], [run(pr(1), base="a" * 40)])
        self.reconcile(github)
        self.assertEqual(github.writes[0][1]["inputs"]["base"], BASE)

    def test_current_validation_reused_but_never_merges(self):
        github = FakeGitHub([pr(1)], [run(pr(1))])
        self.assertIn("ready for supervised review", self.reconcile(github))
        self.assertFalse(github.writes)

    def test_unavailable_alerts_are_reported(self):
        github = FakeGitHub([pr(1)])
        report = self.reconcile(github, None)
        self.assertIn("priority unknown", report)
        self.assertIn("alerts unavailable", report)

    def test_critical_without_fix_can_pause_versions(self):
        critical = alert(2)
        critical["dependabotUpdate"] = None
        critical["securityVulnerability"]["firstPatchedVersion"] = None
        github = FakeGitHub([pr(1)])
        report = self.reconcile(github, [critical], {**CONFIG, "pause_for_critical": True})
        self.assertIn("no known patch", report)
        self.assertIn("paused", report)
        self.assertFalse(github.writes)

    def test_head_status_is_bound_to_current_validation_run(self):
        github = FakeGitHub([pr(1)], [run(pr(1))])
        self.reconcile(github, config={**CONFIG, "publish_status": True})
        [(path, payload)] = github.writes
        self.assertEqual(path, f"statuses/{'1' * 40}")
        self.assertEqual(payload["context"], "autorepo/validation")
        self.assertEqual(payload["state"], "success")
        self.assertIn(BASE[:12], payload["description"])

    def test_auto_merge_rejects_non_strict_protection(self):
        class UnprotectedGitHub:
            def request(self, path):
                return {"required_status_checks": {
                    "strict": False, "checks": [{"context": "CI", "app_id": 15368}],
                }}
        github = FakeGitHub([pr(1)])
        config = {**CONFIG, "merge": {
            "method": "squash", "required_checks": ["CI"], "lockfiles": ["go.sum"],
        }}
        self.assertIn("strict GitHub Actions checks",
                      auto_merge(github, UnprotectedGitHub(), config, pr(1), BASE, 4))
        self.assertFalse(github.writes)

    def test_verification_rejects_different_workflow_revision(self):
        github = FakeGitHub([pr(1)])
        env = {
            "BASE_BRANCH": "trunk", "HEAD_SHA": "1" * 40, "BASE_SHA": BASE,
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/trunk",
            "GITHUB_SHA": "c" * 40, "PR": "1", "ADVISORIES": "", "VALIDATION_KEY": target_key(""),
        }
        with self.assertRaisesRegex(ValueError, "workflow revision"):
            verify(github, env)

    def test_invalid_configuration_fails(self):
        with self.assertRaises(ValueError):
            policy({**CONFIG, "max_attempts": True})


if __name__ == "__main__":
    unittest.main()
