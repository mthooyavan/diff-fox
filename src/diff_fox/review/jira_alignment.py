"""Post-review Jira alignment validation.

Compares what a PR implements against what the Jira ticket requires.
Detects misalignment (wrong ticket) or partial implementation.
"""

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from diff_fox.llm import get_structured_output
from diff_fox.models import Finding
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)

MIN_DESCRIPTION_LENGTH = 50

ALIGNMENT_SYSTEM_PROMPT = """\
You are validating whether a PR's code changes align with the referenced Jira ticket.

Determine alignment:
- ALIGNED: Code addresses Jira requirements (80%+ of acceptance criteria met)
- PARTIAL: Code addresses main requirement but misses significant criteria (20%+)
- MISALIGNED: Code implements completely different functionality than Jira describes

Rules:
- Use SEMANTIC understanding, not keyword matching
- "input form" and "feedback page" → ALIGNED (same concept)
- "evaluation framework" and "feedback UI" → MISALIGNED (different concepts)
- Minor discrepancies or slightly different terminology is ALIGNED
- Missing 1 out of 5 acceptance criteria is ALIGNED (80%+)
- Missing 2+ out of 5 is PARTIAL
"""


class AlignmentResult(BaseModel):
    verdict: Literal["aligned", "partial", "misaligned"] = Field(
        description="aligned (80%+), partial (some criteria missing), or misaligned (wrong ticket)"
    )
    what_pr_implements: str = Field(
        description="1-2 sentence summary of what the code changes actually do"
    )
    what_jira_requires: str = Field(
        description="1-2 sentence summary of what the Jira ticket describes"
    )
    missing_criteria: list[str] = Field(
        default_factory=list,
        description="Specific acceptance criteria from Jira that are not addressed",
    )
    explanation: str = Field(description="Brief explanation of the alignment verdict")


async def check_jira_alignment(
    jira_context,
    findings: list[Finding],
    diff_files: list[DiffFile],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> AlignmentResult | None:
    """Check if PR implementation aligns with Jira requirements."""
    if not jira_context.tickets:
        return None

    if not _should_run_alignment(jira_context):
        logger.info("Jira description too vague — skipping alignment check")
        return None

    try:
        return await _run_alignment_check(jira_context, findings, diff_files, client, model)
    except Exception:
        logger.warning("Jira alignment check failed", exc_info=True)
        return None


def _should_run_alignment(jira_context) -> bool:
    for ticket in jira_context.tickets:
        desc = (ticket.description or "").strip()
        if len(desc) > MIN_DESCRIPTION_LENGTH:
            return True
    return False


async def _run_alignment_check(
    jira_context,
    findings: list[Finding],
    diff_files: list[DiffFile],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> AlignmentResult | None:
    jira_text = ""
    for ticket in jira_context.tickets:
        jira_text += f"## {ticket.key}: {ticket.summary}\n"
        if ticket.description:
            jira_text += f"Description: {ticket.description[:2000]}\n"
        if ticket.acceptance_criteria:
            jira_text += f"Acceptance Criteria: {ticket.acceptance_criteria}\n"
        jira_text += "\n"

    diff_summary = ""
    for f in diff_files[:20]:
        diff_summary += f"- {f.path} ({f.status}, +{f.additions}/-{f.deletions})\n"

    findings_text = ""
    if findings:
        for f in findings[:10]:
            findings_text += f"- [{f.severity}] {f.title}: {f.description[:100]}\n"

    user_msg = (
        "<jira_requirements>\n"
        f"{jira_text}"
        "</jira_requirements>\n\n"
        "<pr_changes>\n"
        f"Files changed:\n{diff_summary}\n"
    )
    if findings_text:
        user_msg += f"Review findings:\n{findings_text}\n"
    user_msg += (
        "</pr_changes>\n\nDoes the PR implementation align with the Jira ticket requirements?"
    )

    result, _ = await get_structured_output(
        client,
        model,
        ALIGNMENT_SYSTEM_PROMPT,
        user_msg,
        AlignmentResult,
        timeout=30,
    )

    if isinstance(result, AlignmentResult):
        logger.info("Jira alignment: %s — %s", result.verdict, result.explanation[:100])
        return result
    return None
