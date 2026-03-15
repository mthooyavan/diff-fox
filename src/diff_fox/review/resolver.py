"""Comment resolution for previously flagged DiffFox findings.

After a re-review, replies to old DiffFox comments that are no longer
flagged, acknowledging user replies if present.
"""

import logging

from diff_fox.models import Finding
from diff_fox.scm.base import SCMProvider

logger = logging.getLogger(__name__)


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
        old_comments = await scm.get_difffox_comments(repo, pr_number)
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

        try:
            if user_replies:
                reply_summary = "; ".join(r[:100] for r in user_replies[:3])
                await scm.reply_to_comment(
                    repo,
                    pr_number,
                    comment["id"],
                    f"\u2705 **Acknowledged** — this issue is no longer detected. "
                    f"Noted your feedback: _{reply_summary}_",
                )
            else:
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
