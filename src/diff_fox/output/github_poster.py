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
            repo,
            pr_number,
            len(review_comments),
        )
    except Exception:
        logger.exception(
            "Failed to submit review for %s PR #%d — falling back to individual comments",
            repo,
            pr_number,
        )
        stats = await _fallback_individual_posts(
            findings,
            comment_bodies,
            summary_body,
            repo,
            pr_number,
            commit_sha,
            scm,
        )

    return stats


async def resolve_addressed_comments(
    new_findings: list[Finding],
    repo: str,
    pr_number: int,
    scm: SCMProvider,
) -> int:
    """Reply to old DiffFox comments that are no longer flagged.

    Finds all comments from previous DiffFox reviews, checks if each
    is still present in the new findings, and replies appropriately.

    If a user has replied in the thread, their reply is acknowledged
    as context rather than blindly marking as addressed.

    Returns the number of comments resolved.
    """
    try:
        old_comments = await scm.get_review_comment_ids_for_difffox(repo, pr_number)
    except Exception:
        logger.warning("Failed to fetch old DiffFox comments for resolution")
        return 0

    if not old_comments:
        return 0

    # Build lookup sets: (path, line) for location match + (path, title_prefix) for content match
    new_locations: set[tuple[str, int]] = set()
    new_titles: set[tuple[str, str]] = set()
    for f in new_findings:
        for line in range(f.line_start, f.line_end + 1):
            new_locations.add((f.file_path, line))
        new_titles.add((f.file_path, f.title.lower().strip()[:40]))

    resolved_count = 0
    for comment in old_comments:
        # Skip comments with no valid line (can't match reliably)
        if not comment.get("line"):
            continue

        # Skip if location still matches a new finding
        if (comment["path"], comment["line"]) in new_locations:
            continue

        # Also check title-based match (handles line shifts after rebase)
        body_first_line = comment["body"].split("\n")[0].lower()
        still_flagged = any(
            title in body_first_line for _, title in new_titles if _ == comment["path"]
        )
        if still_flagged:
            continue

        # Skip if already resolved — check reply bodies, not the original comment
        user_replies = comment.get("user_replies", [])
        all_reply_text = " ".join(user_replies)
        if "Addressed" in all_reply_text or "Acknowledged" in all_reply_text:
            continue

        user_replies = comment.get("user_replies", [])

        try:
            if user_replies:
                # User has replied — acknowledge their input
                reply_summary = "; ".join(r[:100] for r in user_replies[:3])
                await scm.reply_to_comment(
                    repo,
                    pr_number,
                    comment["id"],
                    f"\u2705 **Acknowledged** — this issue is no longer detected. "
                    f"Noted your feedback: _{reply_summary}_",
                )
            else:
                # No user replies — simple resolution
                await scm.reply_to_comment(
                    repo,
                    pr_number,
                    comment["id"],
                    "\u2705 **Addressed** — this issue is no longer detected in the latest review.",
                )
            resolved_count += 1
        except Exception:
            logger.debug("Failed to reply to comment %d", comment["id"])

    if resolved_count:
        logger.info("Resolved %d previously flagged comments", resolved_count)

    return resolved_count


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
                    repo=repo,
                    pr_number=pr_number,
                    body=body,
                    path=finding.file_path,
                    line=line,
                    commit_sha=commit_sha,
                    start_line=start_line,
                )
                return True
            except Exception:
                return False

    results = await asyncio.gather(*[post_one(f, b) for f, b in zip(findings, comment_bodies)])
    stats["inline_posted"] = sum(1 for r in results if r)
    stats["inline_failed"] = sum(1 for r in results if not r)

    try:
        await scm.post_pr_comment(repo, pr_number, summary_body)
        stats["summary_posted"] = True
    except Exception:
        pass

    return stats
