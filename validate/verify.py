"""Verify the candidate against the GitHub state before checking out any code."""
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler import GitHub, SHA, eligible, target_key


def verify(client, env):
    branch, head, base = env["BASE_BRANCH"], env["HEAD_SHA"], env["BASE_SHA"]
    if env["GITHUB_EVENT_NAME"] != "workflow_dispatch" or env["GITHUB_REF"] != f"refs/heads/{branch}":
        raise ValueError("Validation must be dispatched on the trusted branch")
    if not SHA.fullmatch(head) or not SHA.fullmatch(base) or env["GITHUB_SHA"] != base:
        raise ValueError("Validation inputs do not match the workflow revision")
    if not re.fullmatch(r"[1-9][0-9]*", env["PR"]):
        raise ValueError("Invalid PR number")
    advisories = env["ADVISORIES"]
    if advisories and not re.fullmatch(r"GHSA(?:-[a-z0-9]{4}){3}(?:,GHSA(?:-[a-z0-9]{4}){3})*", advisories):
        raise ValueError("Invalid advisory IDs")
    if env["VALIDATION_KEY"] != target_key(advisories):
        raise ValueError("Validation key does not match advisory IDs")
    pr = client.request(f"pulls/{env['PR']}")
    if pr["state"] != "open" or not eligible(pr, client.repository, branch) or pr["head"]["sha"] != head:
        raise ValueError("PR is no longer eligible or its head changed")
    if client.request(f"branches/{quote(branch, safe='')}")["commit"]["sha"] != base:
        raise ValueError("Base changed before validation")
    comparison = client.request(f"compare/{base}...{head}?per_page=1")
    if comparison["behind_by"] or not comparison["ahead_by"]:
        raise ValueError("Candidate must incorporate the requested base and add commits")


if __name__ == "__main__":
    verify(GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]), os.environ)
