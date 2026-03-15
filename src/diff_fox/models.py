"""Consolidated models for DiffFox.

Context models (symbols, call sites, callees, impact) and
review finding models (Finding, ReviewFindings).
Ported from Prism with types preserved exactly.
"""

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Context models
# ---------------------------------------------------------------------------


class SymbolContext(BaseModel):
    """Full context for a single changed symbol (function, class, method)."""

    name: str
    qualified_name: str
    file_path: str
    symbol_type: str  # "function" | "method" | "class" | "variable" | "file"
    signature: str
    docstring: str | None = None
    full_body: str
    change_type: str  # "modified" | "added" | "removed"
    diff_hunks: list[str] = []
    line_start: int = 0
    line_end: int = 0


class CallSite(BaseModel):
    """A location where a symbol is called/referenced."""

    file_path: str
    line_number: int
    surrounding_code: str  # ±10 lines around the call
    caller_function: str | None = None
    call_expression: str


class Callee(BaseModel):
    """A function/method that the changed symbol calls."""

    name: str
    file_path: str
    signature: str
    return_type: str | None = None
    docstring: str | None = None


class ImpactEntry(BaseModel):
    """A specific downstream impact of a change."""

    file_path: str
    line_number: int
    caller_function: str | None = None
    impact_type: str  # "return_type_change" | "param_change" | "behavior_change" | "removed"
    description: str
    severity: str  # "high" | "medium" | "low"


class EnrichedContext(BaseModel):
    """Complete enriched context for a review."""

    symbols: list[SymbolContext] = []
    call_sites: dict[str, list[CallSite]] = {}  # symbol qualified_name -> call sites
    callees: dict[str, list[Callee]] = {}  # symbol qualified_name -> functions it calls
    impact_map: dict[str, list[ImpactEntry]] = {}  # symbol qualified_name -> impacts
    related_files: list[str] = []


# ---------------------------------------------------------------------------
# Review finding models
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """A single review finding produced by an agent."""

    file_path: str = Field(description="Path to the file containing the issue")
    line_start: int = Field(description="Starting line number of the issue")
    line_end: int = Field(description="Ending line number of the issue")
    severity: Literal["critical", "warning", "nit", "pre_existing"] = Field(
        description=(
            "critical: bug that should block merge. "
            "warning: issue worth fixing but not blocking. "
            "nit: minor style/quality issue. "
            "pre_existing: bug in codebase not introduced by this PR."
        )
    )
    category: Literal[
        "logic_error",
        "security",
        "architecture",
        "performance",
        "maintainability",
        "risk",
        "tech_debt",
        "cost",
    ] = Field(description="The category of issue found")
    title: str = Field(
        description="Short title (under 10 words). Example: 'Missing null check on getUser return'"
    )
    description: str = Field(
        description="1-2 sentences max. State the issue and why it matters. No filler."
    )
    reasoning: str = Field(
        description="Internal reasoning (not shown to user). Brief notes on how you found this."
    )
    engineering_level: Literal[
        "senior_engineer",
        "lead_engineer",
        "staff_engineer",
        "principal_engineer",
        "security_architect",
        "engineering_manager",
    ] = Field(description="Which engineering perspective caught this")
    impact_description: str = Field(
        description="1 sentence: what breaks if this isn't fixed"
    )
    suggested_fix: str | None = Field(
        default=None,
        description="Plain text explanation of how to fix.",
    )
    suggested_code: str | None = Field(
        default=None,
        description=(
            "Exact replacement code that can be applied directly. "
            "Must be valid code replacing lines at line_start to line_end."
        ),
    )
    related_locations: list[str] | None = Field(
        default=None,
        description="Other files/lines impacted (e.g. 'src/api.py:42')",
    )
    exploit_scenario: str | None = Field(
        default=None,
        description="For security findings: the exact attack vector.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence in the finding (0.0-1.0).",
    )


class ReviewFindings(BaseModel):
    """Collection of findings from a single agent."""

    findings: list[Finding] = Field(
        default_factory=list,
        description="List of issues found during review. Empty list if no issues.",
    )
