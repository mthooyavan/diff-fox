"""Format findings as readable text for CLI/local output.

Used by the Claude Code plugin and dry-run mode.
"""

from diff_fox.models import Finding
from diff_fox.review.processor import SEVERITY_LABELS, SEVERITY_MARKERS, SEVERITY_ORDER


def format_findings_as_text(
    findings: list[Finding],
    enrichment_failed: bool = False,
) -> str:
    """Format all findings as a readable text report."""
    if not findings:
        lines = ["No issues found."]
        if enrichment_failed:
            lines.append("")
            lines.append("Note: Context enrichment failed. Review quality may be reduced.")
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    lines = [
        f"DiffFox Review: {len(findings)} finding{'s' if len(findings) != 1 else ''}",
        "=" * 60,
        "",
    ]

    for sev in ["critical", "warning", "nit", "pre_existing"]:
        count = counts.get(sev, 0)
        if count:
            marker = SEVERITY_MARKERS.get(sev, "")
            lines.append(f"  {marker} {sev.replace('_', ' ').title()}: {count}")

    lines.append("")

    if enrichment_failed:
        lines.append("Warning: Context enrichment failed. Some findings may have reduced accuracy.")
        lines.append("")

    sorted_findings = sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.file_path, f.line_start),
    )

    for i, f in enumerate(sorted_findings, 1):
        marker = SEVERITY_MARKERS.get(f.severity, "")
        label = SEVERITY_LABELS.get(f.severity, f.severity)
        lines.append(f"[{i}] {marker} {label}")
        lines.append(f"    {f.file_path}:{f.line_start}-{f.line_end}")
        lines.append(f"    {f.title}")
        lines.append(f"    {f.description}")

        if f.suggested_code:
            lines.append("")
            lines.append("    Suggested fix:")
            for code_line in f.suggested_code.split("\n"):
                lines.append(f"      {code_line}")
        elif f.suggested_fix:
            lines.append(f"    Fix: {f.suggested_fix}")

        if f.exploit_scenario:
            lines.append(f"    Attack vector: {f.exploit_scenario}")

        lines.append("")

    lines.append("-" * 60)
    return "\n".join(lines)
