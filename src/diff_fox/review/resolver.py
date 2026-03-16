"""Comment resolution for previously flagged DiffFox findings.

After a re-review, uses LLM verification to check whether each old finding
was actually fixed in the current code before marking it as resolved.
"""

import asyncio
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel

from diff_fox.llm import get_structured_output
from diff_fox.scm.base import DiffFoxComment, SCMProvider

logger = logging.getLogger(__name__)

MAX_CONCURRENT_CHECKS = 5
CODE_WINDOW = 30  # lines above and below the comment's line


class ResolutionVerdict(BaseModel):
    """LLM verdict on whether a previously flagged issue was fixed."""

    verdict: Literal["fixed", "not_fixed", "uncertain"] = "uncertain"
    reasoning: str = ""


RESOLUTION_SYSTEM_PROMPT = """\
You are a code review resolution agent. Your ONLY job is to determine whether
a previously flagged issue has been FIXED in the current code.

You will receive:
1. The original finding (the review comment body)
2. The current code around the location where the finding was flagged

Rules:
- Mark as "fixed" ONLY if you have clear evidence the issue described in the
  finding is no longer present in the current code
- Mark as "not_fixed" if the issue described in the finding is still present
- Mark as "uncertain" if you cannot determine from the available code
- Be conservative: if in doubt, use "not_fixed" or "uncertain"
- A formatting change (whitespace, line wrapping) does NOT fix a logic issue
- Focus on the SUBSTANCE of the finding, not superficial code changes
"""


async def _check_resolution(
    comment: DiffFoxComment,
    repo: str,
    head_sha: str,
    scm: SCMProvider,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> ResolutionVerdict:
    """Use LLM to verify whether a single finding was fixed."""
    try:
        file_content = await scm.get_file_content(repo, comment["path"], head_sha)
    except Exception:
        # File no longer exists → the code was removed, finding is addressed
        return ResolutionVerdict(verdict="fixed", reasoning="File no longer exists")

    # Extract code window around the comment's line
    lines = file_content.content.splitlines()
    center = max(0, comment["line"] - 1)  # 0-indexed
    start = max(0, center - CODE_WINDOW)
    end = min(len(lines), center + CODE_WINDOW + 1)
    numbered_lines = [f"{i + 1}: {lines[i]}" for i in range(start, end)]
    code_snippet = "\n".join(numbered_lines)

    user_message = (
        f"<original_finding>\n"
        f"File: {comment['path']}:{comment['line']}\n"
        f"{comment['body']}\n"
        f"</original_finding>\n\n"
        f'<current_code file="{comment["path"]}" '
        f'lines="{start + 1}-{end}">\n'
        f"{code_snippet}\n"
        f"</current_code>"
    )

    try:
        result, _ = await get_structured_output(
            client,
            model,
            RESOLUTION_SYSTEM_PROMPT,
            user_message,
            ResolutionVerdict,
            timeout=30.0,
            max_tokens=512,
        )
        return result
    except Exception:
        logger.debug("LLM resolution check failed for comment %d", comment["id"])
        return ResolutionVerdict(verdict="uncertain", reasoning="LLM check failed")


async def resolve_addressed_comments(
    repo: str,
    pr_number: int,
    head_sha: str,
    scm: SCMProvider,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> int:
    """Verify and resolve old DiffFox comments using LLM verification.

    For each old DiffFox comment, checks whether the issue was actually
    fixed in the current code before posting an "Addressed" reply.

    Returns the number of comments resolved.
    """
    try:
        old_comments = await scm.get_difffox_comments(repo, pr_number)
    except Exception:
        logger.warning("Failed to fetch old DiffFox comments for resolution")
        return 0

    if not old_comments:
        return 0

    # Filter to comments that need checking
    candidates: list[DiffFoxComment] = []
    for comment in old_comments:
        # Skip comments with no valid line
        if not comment.get("line"):
            continue

        # Skip if already resolved
        all_replies = comment.get("all_replies", [])
        all_reply_text = " ".join(all_replies)
        if "Addressed" in all_reply_text or "Acknowledged" in all_reply_text:
            continue

        candidates.append(comment)

    if not candidates:
        return 0

    logger.info("Checking %d old comments for resolution", len(candidates))

    # Run LLM verification concurrently (capped)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def check_with_limit(c: DiffFoxComment) -> tuple[DiffFoxComment, ResolutionVerdict]:
        async with semaphore:
            verdict = await _check_resolution(c, repo, head_sha, scm, client, model)
            return c, verdict

    results = await asyncio.gather(
        *(check_with_limit(c) for c in candidates),
        return_exceptions=True,
    )

    # Post replies for confirmed fixes
    resolved_count = 0
    for result in results:
        if isinstance(result, BaseException):
            logger.debug("Resolution check failed: %s", result)
            continue

        comment, verdict = result
        if verdict.verdict != "fixed":
            logger.debug(
                "Comment %d not resolved (%s): %s",
                comment["id"],
                verdict.verdict,
                verdict.reasoning,
            )
            continue

        user_replies = comment.get("user_replies", [])
        try:
            if user_replies:
                reply_summary = "; ".join(r[:100] for r in user_replies[:3])
                await scm.reply_to_comment(
                    repo,
                    pr_number,
                    comment["id"],
                    f"\u2705 **Acknowledged** \u2014 this issue has been fixed. "
                    f"Noted your feedback: _{reply_summary}_",
                )
            else:
                await scm.reply_to_comment(
                    repo,
                    pr_number,
                    comment["id"],
                    "\u2705 **Addressed** \u2014 this issue has been fixed in the current code.",
                )
            resolved_count += 1
        except Exception:
            logger.debug("Failed to reply to comment %d", comment["id"])

    if resolved_count:
        logger.info("Resolved %d previously flagged comments", resolved_count)

    return resolved_count
