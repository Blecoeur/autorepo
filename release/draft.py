"""Create a reviewed release draft without running repository code."""
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler import GitHub, SHA


def validated_list(value, pattern, label):
    if value.strip() == "none":
        return []
    values = value.split()
    if not values or not all(re.fullmatch(pattern, item) for item in values):
        raise ValueError(f"Enter valid {label}, or none")
    return sorted(set(values))


def create_draft(client, env):
    tag = env["RELEASE_TAG"]
    ci = env["RELEASE_CI"]
    if not re.fullmatch(r"v(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){2}", tag):
        raise ValueError("A stable vMAJOR.MINOR.PATCH tag is required")
    if not re.fullmatch(r"[\w.-]+\.ya?ml", ci):
        raise ValueError("Invalid release validation workflow")
    advisory = r"GHSA(?:-[a-z0-9]{4}){3}"
    fixed = validated_list(env["RELEASE_FIXED"], advisory, "fixed advisory IDs")
    remaining = validated_list(env["RELEASE_REMAINING"], advisory, "remaining advisory IDs")
    ai_repairs = validated_list(env["RELEASE_AI"], r"[1-9]\d*", "repair PR numbers")
    if set(fixed) & set(remaining):
        raise ValueError("An advisory cannot be both fixed and remaining")
    branch = client.request("")["default_branch"]
    revision = env["GITHUB_SHA"]
    if (env["GITHUB_EVENT_NAME"] != "workflow_dispatch"
            or env["GITHUB_REF"] != f"refs/heads/{branch}" or not SHA.fullmatch(revision)):
        raise ValueError("Dispatch release drafting on the current default branch")
    repositories = f"branches/{quote(branch, safe='')}"
    if client.request(repositories)["commit"]["sha"] != revision:
        raise ValueError("The default branch changed before release drafting")

    for release in client.pages("releases"):
        if release["tag_name"] == tag:
            if release["draft"] and release["target_commitish"] == revision:
                return release["html_url"]
            raise ValueError("This tag already has a release for another revision or is published")
    try:
        client.request(f"git/ref/tags/{quote(tag, safe='')}")
    except HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError("The release tag already exists")

    baseline = next((run for run in client.pages(f"actions/workflows/{ci}/runs",
                    key="workflow_runs", branch=branch, event="push", head_sha=revision)
                    if run["head_sha"] == revision and run["conclusion"] == "success"), None)
    if not baseline:
        raise ValueError("The default-branch revision has no successful release baseline")
    reference = "\n".join((
        f"Validated revision: `{revision}`",
        f"Fixed advisories: {', '.join(fixed) or 'None recorded'}",
        f"Known remaining advisories: {', '.join(remaining) or 'None recorded'}",
        "Reviewed AI repair pull requests: " + (", ".join(f"#{pr}" for pr in ai_repairs) or "None recorded"),
    ))
    if client.request(repositories)["commit"]["sha"] != revision:
        raise ValueError("The default branch changed while drafting release notes")
    result = client.request("releases", {
        "tag_name": tag, "target_commitish": revision, "draft": True,
        "generate_release_notes": True, "body": reference,
    })
    return result["html_url"]


if __name__ == "__main__":
    client = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    result = create_draft(client, os.environ)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        print(f"[Maintenance release draft]({result})", file=summary)
