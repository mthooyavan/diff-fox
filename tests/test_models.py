"""Smoke tests for core models and constants."""

from diff_fox.constants import ALL_AGENT_NAMES
from diff_fox.models import EnrichedContext, Finding, ReviewFindings
from diff_fox.scm.models import DiffFile, DiffHunk, FileContent, PullRequest


def test_all_agent_names():
    assert set(ALL_AGENT_NAMES) == {
        "logic",
        "security",
        "architecture",
        "performance",
        "risk",
        "cogs",
    }


def test_finding_creation():
    f = Finding(
        file_path="src/app.py",
        line_start=10,
        line_end=15,
        severity="warning",
        category="logic_error",
        title="Missing null check",
        description="get_user() can return None but caller doesn't check.",
        reasoning="Traced return path through get_user.",
        engineering_level="senior_engineer",
        impact_description="NoneType error in production.",
    )
    assert f.severity == "warning"
    assert f.category == "logic_error"
    assert f.suggested_code is None


def test_review_findings_empty():
    rf = ReviewFindings()
    assert rf.findings == []


def test_review_findings_with_findings():
    f = Finding(
        file_path="a.py",
        line_start=1,
        line_end=1,
        severity="critical",
        category="security",
        title="SQL injection",
        description="Unsanitized input in query.",
        reasoning="f-string used with user input.",
        engineering_level="security_architect",
        impact_description="Database compromise.",
    )
    rf = ReviewFindings(findings=[f])
    assert len(rf.findings) == 1


def test_enriched_context_defaults():
    ctx = EnrichedContext()
    assert ctx.symbols == []
    assert ctx.call_sites == {}
    assert ctx.callees == {}
    assert ctx.impact_map == {}
    assert ctx.related_files == []


def test_diff_file():
    df = DiffFile(path="src/app.py", status="modified", additions=5, deletions=2)
    assert df.path == "src/app.py"
    assert df.hunks == []


def test_diff_hunk():
    h = DiffHunk(old_start=1, old_lines=3, new_start=1, new_lines=5, content="+new line")
    assert h.new_lines == 5


def test_pull_request():
    pr = PullRequest(
        number=1,
        title="test",
        author="user",
        base_branch="main",
        head_branch="feature",
        head_sha="abc123",
        base_sha="def456",
        state="open",
        repo_full_name="owner/repo",
        url="https://github.com/owner/repo/pull/1",
    )
    assert pr.number == 1
    assert pr.body is None


def test_file_content():
    fc = FileContent(path="a.py", content="print('hello')", ref="main", size=15)
    assert fc.encoding == "utf-8"
