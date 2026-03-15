"""Hard exclusion rules for common security false positives.

Runs after LLM verification but before semantic dedup to catch
obvious false-positive patterns. Fast regex-based filtering.
"""

import logging
import re
from typing import Pattern

from diff_fox.models import Finding

logger = logging.getLogger(__name__)

_DOS_PATTERNS: list[Pattern] = [
    re.compile(r"\b(denial of service|dos attack|resource exhaustion)\b", re.IGNORECASE),
    re.compile(r"\b(exhaust|overwhelm|overload).*?(resource|memory|cpu)\b", re.IGNORECASE),
    re.compile(r"\b(infinite|unbounded).*?(loop|recursion)\b", re.IGNORECASE),
]

_RATE_LIMITING_PATTERNS: list[Pattern] = [
    re.compile(r"\b(missing|lack of|no)\s+rate\s+limit", re.IGNORECASE),
    re.compile(r"\brate\s+limiting\s+(missing|required|not implemented)", re.IGNORECASE),
    re.compile(r"\b(implement|add)\s+rate\s+limit", re.IGNORECASE),
    re.compile(r"\bunlimited\s+(requests|calls|api)", re.IGNORECASE),
]

_RESOURCE_PATTERNS: list[Pattern] = [
    re.compile(r"\b(resource|memory|file)\s+leak\s+potential", re.IGNORECASE),
    re.compile(r"\bunclosed\s+\w*\s*(resource|file|connection)", re.IGNORECASE),
    re.compile(r"\b(close|cleanup|release)\s+(resource|file|connection)", re.IGNORECASE),
    re.compile(r"\bpotential\s+memory\s+leak", re.IGNORECASE),
    re.compile(r"\b(database|thread|socket|connection)\s+leak", re.IGNORECASE),
]

_OPEN_REDIRECT_PATTERNS: list[Pattern] = [
    re.compile(r"\b(open redirect|unvalidated redirect)\b", re.IGNORECASE),
    re.compile(r"\b(redirect\s*\.?\s*(attack|exploit|vulnerability))\b", re.IGNORECASE),
    re.compile(r"\b(malicious\s*\.?\s*redirect)\b", re.IGNORECASE),
]

_MEMORY_SAFETY_PATTERNS: list[Pattern] = [
    re.compile(r"\b(buffer overflow|stack overflow|heap overflow)\b", re.IGNORECASE),
    re.compile(r"\b(oob)\s+(read|write|access)\b", re.IGNORECASE),
    re.compile(r"\b(out.?of.?bounds?)\b", re.IGNORECASE),
    re.compile(r"\b(memory safety|memory corruption)\b", re.IGNORECASE),
    re.compile(r"\b(use.?after.?free|double.?free|null.?pointer.?dereference)\b", re.IGNORECASE),
    re.compile(r"\b(segmentation fault|segfault|memory violation)\b", re.IGNORECASE),
    re.compile(r"\b(integer overflow|integer underflow|integer conversion)\b", re.IGNORECASE),
    re.compile(r"\barbitrary.?(memory read|pointer dereference)\b", re.IGNORECASE),
]

_REGEX_INJECTION_PATTERNS: list[Pattern] = [
    re.compile(r"\b(regex|regular expression)\s+injection\b", re.IGNORECASE),
    re.compile(r"\b(regex|regular expression)\s+denial of service\b", re.IGNORECASE),
    re.compile(r"\b(regex|regular expression)\s+flooding\b", re.IGNORECASE),
    re.compile(r"\bredos\b", re.IGNORECASE),
]

_SSRF_PATTERNS: list[Pattern] = [
    re.compile(r"\b(ssrf|server\s*-?side\s*request\s*forgery)\b", re.IGNORECASE),
]

_LOG_SPOOFING_PATTERNS: list[Pattern] = [
    re.compile(r"\blog\s*(spoofing|injection|tampering)\b", re.IGNORECASE),
    re.compile(r"\b(unsanitized|unvalidated)\s+.*?\blog\b", re.IGNORECASE),
]

_C_CPP_EXTENSIONS = {".c", ".cc", ".cpp", ".h", ".hpp"}
_CLIENT_SIDE_EXTENSIONS = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx"}
_MARKDOWN_EXTENSIONS = {".md", ".mdx", ".rst"}
_TEST_PATTERNS = [
    re.compile(r"_test\.\w+$"),
    re.compile(r"\.test\.\w+$"),
    re.compile(r"\.spec\.\w+$"),
    re.compile(r"/__tests__/"),
    re.compile(r"/test_[^/]+$"),
    re.compile(r"/tests?/"),
]


def _get_file_ext(file_path: str) -> str:
    if "." not in file_path:
        return ""
    return f".{file_path.lower().rsplit('.', 1)[-1]}"


def _is_test_file(file_path: str) -> bool:
    return any(p.search(file_path) for p in _TEST_PATTERNS)


def _matches_any(text: str, patterns: list[Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def get_exclusion_reason(finding: Finding) -> str | None:
    """Check if a security finding should be excluded. Returns reason or None."""
    file_path = finding.file_path
    file_ext = _get_file_ext(file_path)

    if file_ext in _MARKDOWN_EXTENSIONS:
        return "Finding in documentation file"

    if _is_test_file(file_path):
        return "Finding in test file"

    combined = f"{finding.title} {finding.description}"

    if _matches_any(combined, _DOS_PATTERNS):
        return "DOS/resource exhaustion finding"
    if _matches_any(combined, _RATE_LIMITING_PATTERNS):
        return "Rate limiting recommendation"
    if _matches_any(combined, _RESOURCE_PATTERNS):
        return "Resource management finding (not a security vulnerability)"
    if _matches_any(combined, _OPEN_REDIRECT_PATTERNS):
        return "Open redirect finding (low impact)"
    if _matches_any(combined, _REGEX_INJECTION_PATTERNS):
        return "Regex injection/ReDoS finding"
    if _matches_any(combined, _LOG_SPOOFING_PATTERNS):
        return "Log spoofing finding"

    if file_ext not in _C_CPP_EXTENSIONS:
        if _matches_any(combined, _MEMORY_SAFETY_PATTERNS):
            return "Memory safety finding in non-C/C++ code"

    if file_ext in _CLIENT_SIDE_EXTENSIONS:
        if _matches_any(combined, _SSRF_PATTERNS):
            return "SSRF finding in client-side code"

    return None


def filter_security_findings(
    findings: list[Finding],
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into kept vs excluded using hard exclusion rules.

    Only processes findings with category == "security".
    """
    kept: list[Finding] = []
    excluded: list[Finding] = []

    for f in findings:
        if f.category != "security":
            kept.append(f)
            continue

        reason = get_exclusion_reason(f)
        if reason:
            logger.info(
                "Hard exclusion: '%s' at %s:%d — %s",
                f.title,
                f.file_path,
                f.line_start,
                reason,
            )
            excluded.append(f)
        else:
            kept.append(f)

    if excluded:
        logger.info("Hard exclusion filter: %d kept, %d excluded", len(kept), len(excluded))

    return kept, excluded
