"""GitHub Action entry point for DiffFox.

Reads PR event data from GITHUB_EVENT_PATH, runs the review pipeline,
and posts results to the PR.
"""

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("diff-fox")


async def main():
    # Read environment
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    github_token = os.environ.get("GITHUB_TOKEN")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("INPUT_MODEL", "claude-sonnet-4-6-20250514")
    post_comments = os.environ.get("INPUT_POST_COMMENTS", "true").lower() == "true"
    jira_enabled = os.environ.get("INPUT_JIRA_ENABLED", "false").lower() == "true"
    jira_mcp_url = os.environ.get("INPUT_JIRA_MCP_URL", "")

    if not event_path:
        logger.error("GITHUB_EVENT_PATH not set")
        sys.exit(1)

    if not github_token:
        logger.error("GITHUB_TOKEN not set")
        sys.exit(1)

    if not anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Parse event payload
    with open(event_path) as f:
        event = json.load(f)

    pr_data = event.get("pull_request")
    if not pr_data:
        logger.info("No pull_request in event payload — skipping")
        _set_output("status", "skipped")
        _set_output("findings-count", "0")
        return

    # Skip draft PRs
    if pr_data.get("draft", False):
        logger.info("Draft PR — skipping review")
        _set_output("status", "skipped")
        _set_output("findings-count", "0")
        return

    repo = event["repository"]["full_name"]
    pr_number = pr_data["number"]

    logger.info("Starting DiffFox review for %s PR #%d", repo, pr_number)

    # Import here to avoid loading everything for skip cases
    from diff_fox.llm import create_client
    from diff_fox.run_review import run_review
    from diff_fox.scm.github import GitHubProvider

    github_base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")

    async with GitHubProvider(token=github_token, base_url=github_base_url) as scm:
        client = create_client(anthropic_api_key)

        result = await run_review(
            repo=repo,
            pr_number=pr_number,
            scm=scm,
            client=client,
            model=model,
            post_comments=post_comments,
            jira_enabled=jira_enabled,
            jira_mcp_url=jira_mcp_url,
        )

    status = result.get("status", "failed")
    findings_count = result.get("findings_count", 0)

    logger.info("Review complete: status=%s, findings=%d", status, findings_count)

    _set_output("status", status)
    _set_output("findings-count", str(findings_count))

    if status == "failed":
        logger.error("Review failed: %s", result.get("error", "unknown"))
        sys.exit(1)


def _set_output(name: str, value: str):
    """Set a GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


if __name__ == "__main__":
    asyncio.run(main())
