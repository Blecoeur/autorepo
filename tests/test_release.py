import unittest
from unittest.mock import Mock

from release.draft import create_draft


class ReleaseTests(unittest.TestCase):
    def test_existing_draft_for_the_same_revision_is_idempotent(self):
        revision = "a" * 40
        env = {"RELEASE_TAG": "v1.2.3", "RELEASE_CI": "check.yml", "RELEASE_FIXED": "none",
               "RELEASE_REMAINING": "none", "RELEASE_AI": "none", "GITHUB_REF": "refs/heads/main",
               "GITHUB_SHA": revision, "GITHUB_EVENT_NAME": "workflow_dispatch"}
        client = Mock()
        client.request.side_effect = [{"default_branch": "main"}, {"commit": {"sha": revision}}]
        client.pages.return_value = [{"tag_name": "v1.2.3", "draft": True, "target_commitish": revision,
                                      "html_url": "https://example.invalid/release"}]
        self.assertEqual(create_draft(client, env), "https://example.invalid/release")
        self.assertEqual(client.request.call_count, 2)
