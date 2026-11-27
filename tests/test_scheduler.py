import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scheduler import (ACTIONS_BOT_ID, BOT_ID, REPORT_MARKER, GitHub, auto_merge,
                       identity, policy, queue_repair, reconcile, target_key)
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
        if path.startswith("pulls/"):
            return {**next(pr for pr in self.pulls if pr["number"] == int(path.split("/")[1])),
                    "state": "open"}
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

    @patch("scheduler.sleep")
    def test_updates_only_selected_stale_branch(self, _sleep):
        github = FakeGitHub([pr(1), pr(2)], behind=("1", "2"))
        self.assertIn("branch update not confirmed", self.reconcile(github))
        self.assertEqual([p for p, _ in github.writes], [f"statuses/{'1' * 40}", "pulls/1/update-branch"])

    @patch("scheduler.sleep")
    def test_dispatches_validation_for_updated_head_in_same_run(self, _sleep):
        candidate = pr(1)
        refreshed = {**candidate, "state": "open", "head": {**candidate["head"], "sha": "a" * 40}}

        class UpdatingGitHub(FakeGitHub):
            def request(self, path, body=None, **kwargs):
                if path == "pulls/1":
                    return next(observed)
                return super().request(path, body, **kwargs)

        observed = iter([{**candidate, "state": "open"}, refreshed])
        github = UpdatingGitHub([candidate, pr(2)], behind=("1", "2"))
        report = self.reconcile(github, config={**CONFIG, "publish_status": True})
        validation_status = next(body for path, body in github.writes if path == f"statuses/{'a' * 40}")
        self.assertEqual(validation_status["state"], "pending")
        [inputs] = [body["inputs"] for path, body in github.writes if path.endswith("/dispatches")]
        self.assertEqual((inputs["pr"], inputs["head"], inputs["base"]), ("1", "a" * 40, BASE))
        self.assertIn("PR #2 (P4): queued", report)

    def test_closes_empty_updated_pr_with_a_comment_and_continues_the_queue(self):
        candidate = pr(1)
        refreshed = {**candidate, "state": "open", "changed_files": 0,
                     "base": {**candidate["base"], "sha": BASE},
                     "head": {**candidate["head"], "sha": "a" * 40}}

        class EmptyUpdateGitHub(FakeGitHub):
            def pages(self, path, **query):
                if path == "issues/1/comments":
                    return iter([])
                return super().pages(path, **query)

            def request(self, path, body=None, **kwargs):
                if path == "pulls/1" and body is None:
                    return refreshed
                if path == f"compare/{BASE}...{'a' * 40}?per_page=1":
                    return {"ahead_by": 4, "behind_by": 0, "files": []}
                return super().request(path, body, **kwargs)

        github = EmptyUpdateGitHub([candidate, pr(2)], behind=("1",))
        report = self.reconcile(github)
        self.assertIn("PR #1: closed with a comment", report)
        comment, closure, validation = github.writes[-3:]
        self.assertEqual(comment[0], "issues/1/comments")
        self.assertIn("no changed files", comment[1]["body"])
        self.assertEqual(closure, ("pulls/1", {"state": "closed"}))
        self.assertEqual(validation[1]["inputs"]["pr"], "2")

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

    def test_repair_requires_the_configured_failure_and_a_passing_base(self):
        class RepairGitHub(FakeGitHub):
            def __init__(self, jobs):
                super().__init__([])
                self.jobs = jobs

            def pages(self, path, **query):
                if path.endswith("/jobs"):
                    return iter(self.jobs)
                if path == "actions/workflows/checks.yml/runs" and query.get("event") == "push":
                    return iter([{"head_sha": BASE, "conclusion": "success"}])
                return iter([])

        config = {**CONFIG, "repair": {
            "workflow": "repair.yml",
            "rules": [{"job": "compatibility", "step": "dependency API regression",
                       "paths": "src/client.ts", "diagnosis": "The locked API moved."}],
        }}
        jobs = [{"name": "compatibility", "conclusion": "failure", "steps": [
            {"name": "dependency API regression", "conclusion": "failure"},
        ]}]
        github = RepairGitHub(jobs)
        self.assertTrue(queue_repair(github, config, pr(1), BASE, run(pr(1), conclusion="failure"), print))
        self.assertEqual(github.writes[0][1]["inputs"]["head"], "1" * 40)
        jobs.append({"name": "dependency-audit", "conclusion": "failure"})
        github = RepairGitHub(jobs)
        self.assertFalse(queue_repair(github, config, pr(1), BASE, run(pr(1), conclusion="failure"), print))
        self.assertFalse(github.writes)

    def test_invalid_configuration_fails(self):
        with self.assertRaises(ValueError):
            policy({**CONFIG, "max_attempts": True})

    def test_pr_report_flag_must_be_boolean(self):
        self.assertTrue(policy({**CONFIG, "pr_report": True})["pr_report"])
        with self.assertRaises(ValueError):
            policy({**CONFIG, "pr_report": "yes"})

    def test_pr_report_posts_one_comment_per_managed_pr(self):
        class ReportGitHub(FakeGitHub):
            def pages(self, path, **query):
                if path == "issues/1/comments":
                    return iter([])
                return super().pages(path, **query)

        github = ReportGitHub([pr(1)])
        self.reconcile(github, config={**CONFIG, "pr_report": True})
        [write] = [w for w in github.writes if w[0] == "issues/1/comments"]
        self.assertIn(REPORT_MARKER, write[1]["body"])
        self.assertIn("PR #1", write[1]["body"])
        self.assertIn("CI requested for head", write[1]["body"])

    def test_pr_report_is_off_by_default(self):
        github = FakeGitHub([pr(1)])
        self.reconcile(github)
        self.assertFalse(any(path.startswith("issues/") for path, _ in github.writes))

    def test_pr_report_updates_the_existing_bot_comment_only_when_changed(self):
        class FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return datetime(2026, 1, 22, 12, 0, tzinfo=tz)
            fromisoformat = staticmethod(datetime.fromisoformat)

        class ReportGitHub(FakeGitHub):
            comments = []

            def pages(self, path, **query):
                if path == "issues/1/comments":
                    return iter(self.comments)
                return super().pages(path, **query)

        with patch("scheduler.datetime", FrozenDatetime):
            github = ReportGitHub([pr(1)])
            self.reconcile(github, config={**CONFIG, "pr_report": True})
            [(path, payload)] = [w for w in github.writes if w[0].startswith("issues/")]
            self.assertEqual(path, "issues/1/comments")
            github.comments = [{"id": 7, "user": {"id": ACTIONS_BOT_ID}, "body": payload["body"]}]
            github.writes.clear()
            self.reconcile(github, config={**CONFIG, "pr_report": True})
            self.assertFalse(any(p.startswith("issues/") for p, _ in github.writes))
            github.comments[0]["body"] = f"{REPORT_MARKER}\nstale"
            self.reconcile(github, config={**CONFIG, "pr_report": True})
            [(path, payload)] = [w for w in github.writes if w[0].startswith("issues/")]
            self.assertEqual(path, "issues/1/comments/7")
            self.assertIn(REPORT_MARKER, payload["body"])


if __name__ == "__main__":
    unittest.main()
