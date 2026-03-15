"""Pre-posting validation for review findings.

Validates findings against diff lines, suppress filters, and already-posted comments.
"""

import asyncio
import logging
import re

import anthropic
from pydantic import BaseModel, Field

from diff_fox.llm import get_structured_output
from diff_fox.models import Finding
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)


def validate_findings_for_posting(
    findings: list[Finding],
    diff_files: list[DiffFile],
    suppress_filters: list[str] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Validate findings and split into postable vs rejected."""
    diff_lines = _build_diff_line_map(diff_files)

    valid: list[Finding] = []
    rejected: list[Finding] = []

    for f in findings:
        reason = _check_finding(f, diff_lines, suppress_filters)
        if reason:
            logger.info("Rejected: '%s' at %s:%d — %s", f.title, f.file_path, f.line_start, reason)
            rejected.append(f)
        else:
            valid.append(f)

    if rejected:
        logger.info("Validation: %d valid, %d rejected", len(valid), len(rejected))

    return valid, rejected


def _check_finding(
    finding: Finding,
    diff_lines: dict[str, set[int]],
    suppress_filters: list[str] | None,
) -> str | None:
    if finding.file_path not in diff_lines:
        return "file not in diff"

    file_lines = diff_lines[finding.file_path]
    if file_lines and finding.line_start not in file_lines:
        finding_range = set(range(finding.line_start, finding.line_end + 1))
        if not finding_range & file_lines:
            return f"line {finding.line_start} not in diff range"

    if suppress_filters:
        title_lower = finding.title.lower()
        for pattern in suppress_filters:
            if pattern.lower() in title_lower:
                return f"suppressed by filter: {pattern}"

    return None


def _build_diff_line_map(diff_files: list[DiffFile]) -> dict[str, set[int]]:
    """Build a map of file_path -> set of new-side line numbers in the diff."""
    result: dict[str, set[int]] = {}
    for f in diff_files:
        lines: set[int] = set()
        for hunk in f.hunks:
            current = hunk.new_start
            for line in hunk.content.split("\n"):
                if line.startswith("@@"):
                    continue
                if line.startswith("+") and not line.startswith("+++"):
                    lines.add(current)
                    current += 1
                elif line.startswith("-") and not line.startswith("---"):
                    continue
                else:
                    lines.add(current)
                    current += 1
        result[f.path] = lines
    return result


def filter_already_posted(
    findings: list[Finding],
    existing_comments: list[dict],
) -> tuple[list[Finding], list[Finding]]:
    """Filter out findings that match already-posted comments (heuristic)."""
    if not existing_comments:
        return findings, []

    new: list[Finding] = []
    already_posted: list[Finding] = []

    for f in findings:
        if _matches_existing(f, existing_comments):
            logger.info("Already posted: '%s' at %s:%d", f.title, f.file_path, f.line_start)
            already_posted.append(f)
        else:
            new.append(f)

    if already_posted:
        logger.info("Filtered %d already-posted findings", len(already_posted))

    return new, already_posted


def _matches_existing(finding: Finding, existing: list[dict]) -> bool:
    for comment in existing:
        if comment["path"] != finding.file_path:
            continue
        if abs(comment["line"] - finding.line_start) > 5:
            continue
        if finding.title.lower()[:30] in comment["body"].lower():
            return True
        title_words = set(finding.title.lower().split())
        body_lower = comment["body"].lower()
        matching_words = sum(1 for w in title_words if w in body_lower and len(w) > 3)
        if matching_words >= 3:
            return True
    return False


# --- LLM-based existing comment dedup ---

_DEDUP_SYSTEM_PROMPT = """\
You are checking if new code review findings duplicate comments already posted on a PR.

Two findings are duplicates if they describe the SAME underlying issue, even if:
- They point to different files
- They point to different lines in the same file
- They use different wording or phrasing
- One is more specific than the other

Do NOT mark as duplicate if the new finding raises a genuinely DIFFERENT concern.
"""


class DuplicateCheckResult(BaseModel):
    duplicate_indices: list[int] = Field(
        default_factory=list,
        description="1-based indices of new findings that duplicate existing comments",
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Brief reason for each duplicate match",
    )


async def llm_filter_already_posted(
    findings: list[Finding],
    existing_comments: list[dict],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> tuple[list[Finding], list[Finding]]:
    """Use LLM to identify findings that duplicate existing PR comments."""
    if not findings or not existing_comments:
        return findings, []

    try:
        result = await _llm_check_duplicates(findings, existing_comments, client, model)
    except Exception:
        logger.exception("LLM dedup against existing comments failed — keeping all")
        return findings, []

    if not result or not result.duplicate_indices:
        return findings, []

    dup_set = {i - 1 for i in result.duplicate_indices if 1 <= i <= len(findings)}

    new: list[Finding] = []
    already_posted: list[Finding] = []

    for i, f in enumerate(findings):
        if i in dup_set:
            already_posted.append(f)
        else:
            new.append(f)

    if already_posted:
        logger.info("LLM dedup filtered %d findings matching existing comments", len(already_posted))

    return new, already_posted


async def _llm_check_duplicates(
    findings: list[Finding],
    existing_comments: list[dict],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> DuplicateCheckResult | None:
    existing_text = ""
    for i, c in enumerate(existing_comments, 1):
        body = c.get("body", "")
        path = c["path"]
        line = c["line"]
        if path:
            summary = body.split("\n")[0][:120]
            existing_text += f"[{i}] {path}:{line} — {summary}\n"
        else:
            clean = re.sub(r"<[^>]+>", "", body).strip()
            clean = re.sub(r"\n{3,}", "\n\n", clean)
            existing_text += f"[{i}] [PR review]\n{clean}\n\n"

    findings_text = ""
    for i, f in enumerate(findings, 1):
        findings_text += f"[{i}] {f.file_path}:{f.line_start} — {f.title}: {f.description[:100]}\n"

    user_msg = (
        "<existing_comments>\n"
        f"{existing_text}"
        "</existing_comments>\n\n"
        "<new_findings>\n"
        f"{findings_text}"
        "</new_findings>\n\n"
        "Which new findings duplicate existing comments? "
        "Return the indices of new findings that are duplicates."
    )

    result, _ = await get_structured_output(
        client, model, _DEDUP_SYSTEM_PROMPT, user_msg, DuplicateCheckResult, timeout=30,
    )

    return result if isinstance(result, DuplicateCheckResult) else None
