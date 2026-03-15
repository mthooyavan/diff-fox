"""Result processing: deduplication, severity ranking, and markdown formatting."""

import logging

from diff_fox.models import Finding

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "nit": 2, "pre_existing": 3}

SEVERITY_MARKERS = {
    "critical": "\U0001f534",  # red circle
    "warning": "\U0001f7e1",  # yellow circle
    "nit": "\U0001f535",  # blue circle
    "pre_existing": "\U0001f7e3",  # purple circle
}

SEVERITY_LABELS = {
    "critical": "Bug — fix before merging",
    "warning": "Issue — worth fixing",
    "nit": "Nit — minor improvement",
    "pre_existing": "Pre-existing — not introduced by this PR",
}

EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".js": "javascript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".sh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sql": "sql",
}


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Remove duplicate findings using 10-line bucket heuristic."""
    if not findings:
        return []

    deduped: dict[str, Finding] = {}
    for f in findings:
        key = _dedup_key(f)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = f
        elif _severity_rank(f) < _severity_rank(existing):
            deduped[key] = f

    result = list(deduped.values())
    if len(findings) != len(result):
        logger.info(
            "Deduplication: %d → %d findings (%d duplicates removed)",
            len(findings),
            len(result),
            len(findings) - len(result),
        )
    return result


def _severity_rank(f: Finding) -> int:
    return SEVERITY_ORDER.get(f.severity, 99)


def _dedup_key(f: Finding) -> str:
    title_prefix = f.title.lower().strip()[:40]
    line_bucket = f.line_start // 10
    return f"{f.file_path}:{line_bucket}:{f.category}:{title_prefix}"


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by severity (critical first), then by file and line."""
    return sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.file_path, f.line_start),
    )


def format_finding_comment(finding: Finding) -> str:
    """Format a single finding as a concise GitHub inline comment."""
    marker = SEVERITY_MARKERS.get(finding.severity, "")
    label = SEVERITY_LABELS.get(finding.severity, finding.severity)

    lines = [
        f"{marker} **{label}**: {finding.title}",
        "",
        finding.description,
    ]

    if finding.category == "security" and finding.exploit_scenario:
        lines.append("")
        lines.append(f"**Attack vector**: {finding.exploit_scenario}")

    if finding.suggested_code:
        lines.append("")
        lines.append(f"```suggestion\n{finding.suggested_code}\n```")
    elif finding.suggested_fix:
        lines.append("")
        lines.append(f"**Fix:** {finding.suggested_fix}")

    return "\n".join(lines)


def format_summary_comment(
    findings: list[Finding],
    repo: str,
    pr_number: int,
    enrichment_failed: bool = False,
    alignment=None,
) -> str:
    """Format a PR-level summary comment."""
    if not findings:
        lines = [
            "## \u2705 DiffFox — No Issues Found",
            "",
            f"Reviewed PR #{pr_number} in `{repo}`. No issues detected.",
        ]
        if enrichment_failed:
            lines.append("")
            lines.append(
                "\u26a0\ufe0f *Note: Context enrichment failed. Review quality may be reduced.*"
            )
        lines.append("")
        lines.append("---")
        lines.append("*DiffFox*")
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    lines = [
        f"## DiffFox — {len(findings)} Finding{'s' if len(findings) != 1 else ''}",
        "",
    ]

    for sev in ["critical", "warning", "nit", "pre_existing"]:
        count = counts.get(sev, 0)
        if count:
            marker = SEVERITY_MARKERS.get(sev, "")
            lines.append(f"{marker} **{sev.replace('_', ' ').title()}**: {count}")

    lines.append("")

    if enrichment_failed:
        lines.append(
            "\u26a0\ufe0f *Context enrichment failed. Some findings may have reduced accuracy.*"
        )
        lines.append("")

    files: dict[str, int] = {}
    for f in findings:
        files[f.file_path] = files.get(f.file_path, 0) + 1

    lines.append("**Files with findings:**")
    for path, count in sorted(files.items()):
        lines.append(f"- `{path}` ({count})")

    if alignment and alignment.verdict == "misaligned":
        lines.append("")
        lines.append("---")
        lines.append("### \u26a0\ufe0f Jira Alignment Issue")
        lines.append("")
        lines.append(
            "**MISALIGNMENT detected**: The code changes appear unrelated "
            "to the referenced Jira ticket."
        )
        lines.append(f"**What the PR implements**: {alignment.what_pr_implements}")
        lines.append(f"**What the Jira ticket describes**: {alignment.what_jira_requires}")
        lines.append("**Recommendation**: Verify the correct Jira ticket is referenced.")
    elif alignment and alignment.verdict == "partial":
        lines.append("")
        lines.append("---")
        lines.append("### \u26a0\ufe0f Partial Jira Alignment")
        lines.append("")
        lines.append(f"**What the PR implements**: {alignment.what_pr_implements}")
        lines.append("**Missing acceptance criteria**:")
        for criterion in alignment.missing_criteria:
            lines.append(f"- {criterion}")

    lines.append("")
    lines.append("---")
    lines.append("*DiffFox*")
    return "\n".join(lines)


def process_findings(
    findings: list[Finding],
    repo: str,
    pr_number: int,
    enrichment_failed: bool = False,
    alignment=None,
) -> tuple[list[Finding], list[str], str]:
    """Full processing pipeline: dedup -> rank -> format."""
    deduped = deduplicate_findings(findings)
    ranked = rank_findings(deduped)
    inline_comments = [format_finding_comment(f) for f in ranked]
    summary = format_summary_comment(
        ranked,
        repo,
        pr_number,
        enrichment_failed,
        alignment=alignment,
    )
    return ranked, inline_comments, summary
