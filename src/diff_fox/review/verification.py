"""Verification pipeline for filtering false-positive findings.

Re-examines each candidate finding against actual code context to determine
if the finding is genuinely valid. Uses the LLM as a second-opinion verifier
with a different prompt perspective than the original review agents.

Conservative: uncertain findings are always kept. On LLM failure, findings are kept.
"""

import asyncio
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from diff_fox.llm import get_structured_output
from diff_fox.models import EnrichedContext, Finding
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
MAX_FINDINGS_TO_VERIFY = 50
MAX_CONCURRENT_VERIFICATIONS = 10


VERIFICATION_SYSTEM_PROMPT = """\
You are a code review verification agent. Your ONLY job is to determine whether
a proposed finding is GENUINELY VALID or a FALSE POSITIVE.

You will receive:
1. A proposed finding (title, description, reasoning, file, lines)
2. The relevant section of the PR diff (filtered to the finding's file)
3. Deep context for the relevant symbols

Rules:
- Only mark as "false_positive" if you have CLEAR EVIDENCE the finding is wrong
- If the code described in the finding matches what you see, mark as "valid"
- If you lack sufficient context to confirm or deny, mark as "uncertain"
- When in doubt, use "uncertain" — real bugs should never be silently dropped
- Do NOT add new findings — only verify existing ones
- Check the call sites and impact data to confirm the finding's real-world impact

A finding is FALSE POSITIVE only if:
- The described code behavior doesn't match the actual code
- The issue is clearly already handled in surrounding code
- The finding references code that doesn't exist in this PR
- The reasoning makes provably incorrect assumptions
"""

SECURITY_VERIFICATION_ADDENDUM = """\

SECURITY-SPECIFIC FALSE POSITIVE CRITERIA:
When verifying security findings, also apply these precedent rules:
1. UUIDs are unguessable — attacks requiring UUID guessing are invalid
2. React is secure against XSS unless using unsafe HTML injection methods
3. Environment variables and CLI flags are trusted values
4. Client-side JS/TS cannot perform SSRF or server-side path traversal
5. Resource management issues (memory leaks, file descriptor leaks) are NOT security vulns
6. Subtle web vulns (tabnabbing, XS-Leaks, prototype pollution, open redirects) are not valid
7. Command injection in shell scripts requires concrete untrusted input path
8. Missing hardening measures are not vulnerabilities
9. Logging non-PII data is NOT a vulnerability
10. SSRF that only controls the URL path (not host/protocol) is NOT valid
11. DOS/resource exhaustion is out of scope
12. Rate limiting concerns are out of scope
13. Test-only files should not have security findings

Mark as false_positive if the finding violates any of these precedents.
"""


class VerificationResult(BaseModel):
    """LLM output for a single finding verification."""

    verdict: Literal["valid", "false_positive", "uncertain"] = Field(
        description=(
            "valid: finding is genuine. "
            "false_positive: finding is provably incorrect. "
            "uncertain: cannot determine with available context."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the verdict (0.0 to 1.0)",
    )
    explanation: str = Field(
        description="Brief explanation of why this verdict was reached",
    )


async def verify_findings(
    findings: list[Finding],
    diff_files: list[DiffFile],
    enriched_context: EnrichedContext | None,
    client: anthropic.AsyncAnthropic,
    model: str,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[Finding]:
    """Verify findings in parallel, filtering out confirmed false positives.

    Conservative approach:
    - "valid" → KEEP
    - "uncertain" (any confidence) → KEEP
    - "false_positive" with confidence >= threshold → DROP
    - LLM failure → KEEP
    - Overflow beyond budget → KEEP (unverified)
    """
    if not findings:
        return []

    _sev_order = {"critical": 0, "warning": 1, "nit": 2, "pre_existing": 3}
    sorted_findings = sorted(findings, key=lambda f: _sev_order.get(f.severity, 99))

    to_verify = sorted_findings[:MAX_FINDINGS_TO_VERIFY]
    overflow = sorted_findings[MAX_FINDINGS_TO_VERIFY:]

    if overflow:
        logger.warning(
            "Too many findings (%d), only verifying first %d; %d kept unverified",
            len(findings),
            MAX_FINDINGS_TO_VERIFY,
            len(overflow),
        )

    diff_by_file = {f.path: f for f in diff_files}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATIONS)

    async def verify_with_limit(finding: Finding):
        async with semaphore:
            return await _verify_single_finding(
                finding,
                diff_by_file,
                enriched_context,
                client,
                model,
            )

    results = await asyncio.gather(
        *[verify_with_limit(f) for f in to_verify],
        return_exceptions=True,
    )

    verified: list[Finding] = []
    filtered_count = 0

    for finding, result in zip(to_verify, results):
        if isinstance(result, BaseException):
            logger.error("Verification exception for '%s': %s", finding.title, result)
            verified.append(finding)
            continue

        if result is None:
            verified.append(finding)
            continue

        if not isinstance(result, VerificationResult):
            verified.append(finding)
            continue

        if result.verdict == "false_positive" and result.confidence >= confidence_threshold:
            filtered_count += 1
            logger.info(
                "Filtered: '%s' — confidence=%.2f: %s",
                finding.title,
                result.confidence,
                result.explanation,
            )
        else:
            verified.append(finding)

    verified.extend(overflow)

    logger.info(
        "Verification complete: %d verified, %d filtered, %d unverified overflow (total: %d)",
        len(to_verify) - filtered_count,
        filtered_count,
        len(overflow),
        len(verified),
    )
    return verified


async def _verify_single_finding(
    finding: Finding,
    diff_by_file: dict[str, DiffFile],
    enriched_context: EnrichedContext | None,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> VerificationResult | None:
    """Verify a single finding with finding-specific context."""
    diff_text = _format_finding_diff(finding, diff_by_file)
    context_text = _format_finding_context(finding, enriched_context)

    finding_text = (
        f"Title: {finding.title}\n"
        f"File: {finding.file_path} (lines {finding.line_start}-{finding.line_end})\n"
        f"Severity: {finding.severity}\n"
        f"Category: {finding.category}\n"
        f"Description: {finding.description}\n"
        f"Reasoning: {finding.reasoning}\n"
        f"Impact: {finding.impact_description}\n"
    )
    if finding.suggested_fix:
        finding_text += f"Suggested Fix: {finding.suggested_fix}\n"
    if finding.related_locations:
        finding_text += f"Related Locations: {', '.join(finding.related_locations)}\n"

    user_message = (
        "<proposed_finding>\n"
        f"{finding_text}\n"
        "</proposed_finding>\n\n"
        "<relevant_diff>\n"
        f"{diff_text}\n"
        "</relevant_diff>\n\n"
        "<relevant_context>\n"
        f"{context_text}\n"
        "</relevant_context>"
    )

    system_prompt = VERIFICATION_SYSTEM_PROMPT
    if finding.category == "security":
        system_prompt += SECURITY_VERIFICATION_ADDENDUM

    try:
        result, _ = await get_structured_output(
            client,
            model,
            system_prompt,
            user_message,
            VerificationResult,
            timeout=60,
        )
        return result if isinstance(result, VerificationResult) else None
    except Exception:
        logger.exception("Verification failed for finding: %s", finding.title)
        return None


def _format_finding_diff(finding: Finding, diff_by_file: dict[str, DiffFile]) -> str:
    """Format only the diff for the finding's file."""
    diff_file = diff_by_file.get(finding.file_path)
    if not diff_file:
        return f"No diff available for {finding.file_path}"
    parts = [f"--- {diff_file.path} ({diff_file.status})"]
    if diff_file.patch:
        parts.append(diff_file.patch[:60_000])
    return "\n".join(parts)


def _format_finding_context(finding: Finding, ctx: EnrichedContext | None) -> str:
    """Format only the context relevant to the finding's symbols."""
    if not ctx or not ctx.symbols:
        return "No context available."

    parts: list[str] = []
    relevant = [s for s in ctx.symbols if s.file_path == finding.file_path]
    if not relevant:
        relevant = ctx.symbols[:5]

    for sym in relevant[:10]:
        parts.append(f"Symbol: {sym.qualified_name} ({sym.symbol_type})")
        parts.append(f"  Signature: {sym.signature}")
        if sym.full_body:
            parts.append(f"  Body:\n{sym.full_body}")
        sites = ctx.call_sites.get(sym.qualified_name, [])
        if sites:
            parts.append(f"  Call sites ({len(sites)}):")
            for s in sites[:5]:
                parts.append(f"    - {s.file_path}:{s.line_number}")
        impacts = ctx.impact_map.get(sym.qualified_name, [])
        if impacts:
            for imp in impacts[:5]:
                parts.append(f"  Impact: [{imp.severity}] {imp.description}")
    return "\n".join(parts)
