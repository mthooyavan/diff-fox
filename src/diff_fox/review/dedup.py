"""LLM-based semantic deduplication for review findings.

Sends ALL findings to the LLM in one call to merge cross-file and
cross-agent duplicates that describe the same underlying issue.
"""

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from diff_fox.llm import get_structured_output
from diff_fox.models import Finding

logger = logging.getLogger(__name__)

MERGE_SYSTEM_PROMPT = """\
You are a deduplication agent for code review findings. You receive findings
from multiple review agents that analyzed the same PR. Many findings describe
the SAME underlying issue from different angles or in different files.

Your job:
1. Identify findings that describe the SAME underlying issue — even if they:
   - Use different wording
   - Point to different files (e.g., same rename in es.py and opensearch.py)
   - Have different severities
   - Focus on different aspects of the same problem

2. For each group of duplicates, produce ONE merged finding that:
   - Uses the clearest, most specific title
   - Takes the highest severity from the group
   - Combines the best description elements into 1-2 sentences
   - References ALL affected files/lines (e.g., "Affects es.py:15 and opensearch.py:25")
   - Keeps the best suggested_code or suggested_fix
   - Uses the primary file's path (most impactful location)

3. Keep truly unique findings as-is

IMPORTANT:
- Be AGGRESSIVE about merging. "Invalid KNN syntax" and "Incorrect KNN query
  structure" are the SAME issue. "Missing ES implementation" and "hybrid_search
  not in es.py" are the SAME issue.
- "Removed feedback boosting" flagged by risk, architecture, performance, and
  cost agents are ALL the same issue — merge into ONE finding.
- Cross-file duplicates are common: index rename in es.py and opensearch.py
  = ONE finding.
- Target: reduce to roughly 8-12 unique findings for a typical PR.
"""


class MergedFinding(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    severity: Literal["critical", "warning", "nit", "pre_existing"]
    category: str
    title: str = Field(description="Best title from the merged group")
    description: str = Field(description="1-2 sentences. Mention all affected files.")
    suggested_fix: str | None = None
    suggested_code: str | None = None
    exploit_scenario: str | None = None
    confidence: float | None = None
    related_locations: list[str] | None = None
    engineering_level: str | None = None
    merged_from: list[int] = Field(
        description="List of original finding numbers [1,2,...] that were merged into this"
    )
    merge_reason: str = Field(
        default="",
        description="Brief reason why these were merged",
    )


class MergeResult(BaseModel):
    findings: list[MergedFinding]


async def semantic_dedup(
    findings: list[Finding],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> list[Finding]:
    """Merge duplicate findings using LLM-based semantic analysis."""
    if len(findings) <= 5:
        return findings

    try:
        merged = await _merge_all_findings(findings, client, model)
        logger.info("Semantic dedup: %d → %d findings", len(findings), len(merged))
        return merged
    except Exception:
        logger.exception("Semantic dedup failed — keeping originals")
        return findings


async def _merge_all_findings(
    findings: list[Finding],
    client: anthropic.AsyncAnthropic,
    model: str,
) -> list[Finding]:
    """Send all findings to LLM for cross-file merge."""
    findings_text = ""
    for i, f in enumerate(findings, 1):
        findings_text += (
            f"\n[{i}] {f.severity.upper()} | {f.category} | "
            f"{f.file_path}:{f.line_start}-{f.line_end}\n"
            f"Title: {f.title}\n"
            f"Description: {f.description}\n"
        )
        if f.suggested_code:
            findings_text += f"Code: {f.suggested_code[:150]}\n"
        if f.suggested_fix:
            findings_text += f"Fix: {f.suggested_fix[:150]}\n"

    user_msg = (
        f"<findings>\n{findings_text}\n</findings>\n\n"
        f"Merge duplicates aggressively. Target ~8-12 unique findings."
    )

    parsed, _ = await get_structured_output(
        client, model, MERGE_SYSTEM_PROMPT, user_msg, MergeResult, timeout=120,
    )

    if not isinstance(parsed, MergeResult) or not parsed.findings:
        return findings

    for mf in parsed.findings:
        if len(mf.merged_from) > 1:
            originals = [
                f"[{n}] {findings[n-1].title}" if n <= len(findings) else f"[{n}]"
                for n in mf.merged_from
            ]
            logger.info(
                "  MERGED %d findings → '%s': %s — %s",
                len(mf.merged_from), mf.title, ", ".join(originals), mf.merge_reason,
            )

    valid_categories = {
        "logic_error", "security", "architecture", "performance",
        "maintainability", "risk", "tech_debt", "cost",
    }
    valid_levels = {
        "senior_engineer", "lead_engineer", "staff_engineer",
        "principal_engineer", "security_architect", "engineering_manager",
    }

    merged: list[Finding] = []
    for mf in parsed.findings:
        category = mf.category if mf.category in valid_categories else "logic_error"
        primary_idx = mf.merged_from[0] if mf.merged_from else None
        primary = findings[primary_idx - 1] if primary_idx and primary_idx <= len(findings) else None

        raw_level = mf.engineering_level or (primary.engineering_level if primary else "senior_engineer")
        eng_level = raw_level if raw_level in valid_levels else (primary.engineering_level if primary else "senior_engineer")

        merged.append(Finding(
            file_path=mf.file_path,
            line_start=mf.line_start,
            line_end=mf.line_end,
            severity=mf.severity,
            category=category,
            title=mf.title,
            description=mf.description,
            reasoning=primary.reasoning if primary else "Merged from multiple agent findings",
            engineering_level=eng_level,
            impact_description=primary.impact_description if primary else mf.description,
            suggested_fix=mf.suggested_fix,
            suggested_code=mf.suggested_code,
            exploit_scenario=mf.exploit_scenario or (primary.exploit_scenario if primary else None),
            confidence=mf.confidence or (primary.confidence if primary else None),
            related_locations=mf.related_locations or (primary.related_locations if primary else None),
        ))

    return merged if merged else findings
