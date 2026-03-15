"""Pydantic models for SCM (source control management) data."""

from pydantic import BaseModel, Field


class PullRequest(BaseModel):
    """Represents a pull request from a source control provider."""

    number: int
    title: str
    body: str | None = None
    author: str
    base_branch: str
    head_branch: str
    head_sha: str
    base_sha: str
    state: str
    repo_full_name: str
    url: str


class DiffHunk(BaseModel):
    """Represents a single hunk in a unified diff."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    content: str


class DiffFile(BaseModel):
    """Represents a file that was changed in a diff."""

    path: str
    previous_path: str | None = None
    status: str
    additions: int = 0
    deletions: int = 0
    patch: str | None = None
    hunks: list[DiffHunk] = Field(default_factory=list)


class FileContent(BaseModel):
    """Represents the content of a file at a specific ref."""

    path: str
    content: str
    ref: str
    size: int
    encoding: str = "utf-8"
