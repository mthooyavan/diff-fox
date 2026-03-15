"""Posts review findings as a single batch review on GitHub PRs."""

import asyncio
import logging

from diff_fox.models import Finding
from diff_fox.review.processor import format_finding_comment, format_summary_comment
from diff_fox.scm.base import SCMProvider

logger = logging.getLogger(__name__)


async def post_review_to_pr(
    findings: list[Finding],
    repo: str,
    pr_number: int,
    commit_sha: str,
    scm: SCMProvider,
    enrichment_failed: bool = False,
    pre_formatted_comments: list[str] | None = None,
    pre_formatted_summary: str | None = None,
) -> dict[str, int | bool]:
    """Submit a complete review with all findings as a single batch."""
    stats: dict[str, int | bool] = {
        "inline_posted": 0,
        "inline_failed": 0,
        "summary_posted": False,
    }

    if pre_formatted_comments and len(pre_formatted_comments) == len(findings):
        comment_bodies = pre_formatted_comments
    else:
        comment_bodies = [format_finding_comment(f) for f in findings]

    if pre_formatted_summary:
        summary_body = pre_formatted_summary
    else:
        summary_body = format_summary_comment(findings, repo, pr_number, enrichment_failed)

    if not findings:
        try:
            await scm.post_pr_comment(repo, pr_number, summary_body)
            stats["summary_posted"] = True
        except Exception:
            logger.exception("Failed to post summary on PR #%d", pr_number)
        return stats

    review_comments: list[dict] = []
    for finding, body in zip(findings, comment_bodies):
        comment: dict = {
            "path": finding.file_path,
            "line": finding.line_start,
            "body": body,
        }
        if finding.line_end > finding.line_start:
            comment["start_line"] = finding.line_start
            comment["line"] = finding.line_end
        review_comments.append(comment)

    try:
        await scm.submit_review(
            repo=repo,
            pr_number=pr_number,
            body=summary_body,
            comments=review_comments,
            commit_sha=commit_sha,
        )
        stats["inline_posted"] = len(review_comments)
        stats["summary_posted"] = True
        logger.info(
            "Submitted review for %s PR #%d: %d inline comments",
            repo, pr_number, len(review_comments),
        )
    except Exception:
        logger.exception(
            "Failed to submit review for %s PR #%d — falling back to individual comments",
            repo, pr_number,
        )
        stats = await _fallback_individual_posts(
            findings, comment_bodies, summary_body, repo, pr_number, commit_sha, scm,
        )

    return stats


async def _fallback_individual_posts(
    findings: list[Finding],
    comment_bodies: list[str],
    summary_body: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    scm: SCMProvider,
) -> dict[str, int | bool]:
    stats: dict[str, int | bool] = {
        "inline_posted": 0,
        "inline_failed": 0,
        "summary_posted": False,
    }

    semaphore = asyncio.Semaphore(5)

    async def post_one(finding: Finding, body: str) -> bool:
        async with semaphore:
            try:
                start_line = None
                line = finding.line_start
                if finding.line_end > finding.line_start:
                    start_line = finding.line_start
                    line = finding.line_end
                await scm.post_review_comment(
                    repo=repo, pr_number=pr_number,
                    body=body, path=finding.file_path,
                    line=line, commit_sha=commit_sha,
                    start_line=start_line,
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(
        *[post_one(f, b) for f, b in zip(findings, comment_bodies)]
    )
    stats["inline_posted"] = sum(1 for r in results if r)
    stats["inline_failed"] = sum(1 for r in results if not r)

    try:
        await scm.post_pr_comment(repo, pr_number, summary_body)
        stats["summary_posted"] = True
    except Exception:
        pass

    return stats
