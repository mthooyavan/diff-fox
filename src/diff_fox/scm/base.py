"""Abstract base class for SCM (source control management) providers."""

from abc import ABC, abstractmethod
from typing import TypedDict

from diff_fox.scm.models import DiffFile, FileContent, PullRequest


class DiffFoxComment(TypedDict):
    """Typed structure for DiffFox review comments used in resolution."""

    id: int
    path: str
    line: int
    body: str
    user_replies: list[str]  # Human replies only (for acknowledgment context)
    all_replies: list[str]  # All replies including bot (for dedup/resolution check)


class SCMProvider(ABC):
    """Abstract base class that defines the interface for SCM providers."""

    @abstractmethod
    async def get_pull_request(self, repo: str, pr_number: int) -> PullRequest: ...

    @abstractmethod
    async def get_diff(self, repo: str, pr_number: int) -> list[DiffFile]: ...

    @abstractmethod
    async def get_file_content(self, repo: str, path: str, ref: str) -> FileContent: ...

    @abstractmethod
    async def search_code(self, repo: str, query: str) -> list[dict]: ...

    @abstractmethod
    async def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
        path: str,
        line: int,
        commit_sha: str,
        start_line: int | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_review_comments(self, repo: str, pr_number: int) -> list[dict]: ...

    @abstractmethod
    async def submit_review(
        self,
        repo: str,
        pr_number: int,
        body: str,
        comments: list[dict],
        commit_sha: str,
    ) -> None: ...

    @abstractmethod
    async def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None: ...

    @abstractmethod
    async def reply_to_comment(
        self, repo: str, pr_number: int, comment_id: int, body: str
    ) -> None: ...

    @abstractmethod
    async def get_difffox_comments(self, repo: str, pr_number: int) -> list[DiffFoxComment]: ...
