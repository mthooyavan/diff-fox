"""GitHub SCM provider implementation using httpx.AsyncClient."""

import base64
import logging
from typing import Any

import httpx

from diff_fox.scm.base import DiffFoxComment, SCMProvider
from diff_fox.scm.diff_parser import parse_diff_files
from diff_fox.scm.models import CommitInfo, DiffFile, FileContent, PullRequest

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubProvider(SCMProvider):
    """GitHub SCM provider that uses httpx.AsyncClient for HTTP requests.

    Supports connection pooling via the async context manager pattern and
    handles pagination for GitHub API endpoints.
    """

    def __init__(self, token: str, base_url: str = GITHUB_API_BASE) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubProvider":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "GitHubProvider must be used as an async context manager. "
                "Use 'async with GitHubProvider(token) as provider:'"
            )
        return self._client

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """Make a GET request to the GitHub API."""
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _get_paginated(self, url: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Make paginated GET requests, following Link headers."""
        if params is None:
            params = {}
        params.setdefault("per_page", 100)

        all_items: list[Any] = []
        next_url: str | None = url

        while next_url is not None:
            response = await self.client.get(next_url, params=params)
            response.raise_for_status()
            data = response.json()

            if isinstance(data, list):
                all_items.extend(data)
            else:
                all_items.append(data)

            # Parse Link header for pagination
            next_url = None
            link_header = response.headers.get("Link", "")
            if link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                        # When following pagination links, don't pass params again
                        # as they're already encoded in the URL
                        params = {}
                        break

        return all_items

    async def get_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        """Fetch a pull request from GitHub.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            pr_number: The pull request number.

        Returns:
            A PullRequest object with the PR metadata.
        """
        data = await self._get(f"/repos/{repo}/pulls/{pr_number}")
        return PullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body"),
            author=data["user"]["login"],
            base_branch=data["base"]["ref"],
            head_branch=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            base_sha=data["base"]["sha"],
            state=data["state"],
            repo_full_name=repo,
            url=data["html_url"],
        )

    async def get_diff(self, repo: str, pr_number: int) -> list[DiffFile]:
        """Fetch the diff files for a pull request.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            pr_number: The pull request number.

        Returns:
            A list of DiffFile objects representing the changed files.
        """
        files_data = await self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/files")
        return parse_diff_files(files_data)

    async def get_pr_commits(self, repo: str, pr_number: int) -> list[CommitInfo]:
        """Fetch all commits for a pull request."""
        data = await self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/commits")
        return [
            CommitInfo(sha=c["sha"], message=c["commit"]["message"])
            for c in data
        ]

    async def get_file_content(self, repo: str, path: str, ref: str) -> FileContent:
        """Fetch the content of a file at a specific ref.

        Handles base64 decoding of the file content returned by the GitHub API.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            path: The file path within the repository.
            ref: The git ref (branch, tag, or SHA) to fetch the file at.

        Returns:
            A FileContent object with the decoded file content.
        """
        data = await self._get(
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
        )

        content = data.get("content", "")
        encoding = data.get("encoding", "base64")

        if encoding == "base64" and content:
            decoded_content = base64.b64decode(content).decode("utf-8")
        else:
            decoded_content = content

        return FileContent(
            path=data["path"],
            content=decoded_content,
            ref=ref,
            size=data.get("size", 0),
            encoding="utf-8",
        )

    async def search_code(self, repo: str, query: str) -> list[dict]:
        """Search for code within a repository.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            query: The search query string.

        Returns:
            A list of search result dicts from the GitHub API.
        """
        data = await self._get(
            "/search/code",
            params={"q": f"{query} repo:{repo}"},
        )
        return data.get("items", [])

    async def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
        path: str,
        line: int,
        commit_sha: str,
        start_line: int | None = None,
    ) -> None:
        """Post a review comment on a specific line of a pull request.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            pr_number: The pull request number.
            body: The comment body text.
            path: The file path to comment on.
            line: The line number to comment on.
            commit_sha: The commit SHA to associate the comment with.
            start_line: Optional start line for multi-line comments.
        """
        payload: dict[str, Any] = {
            "body": body,
            "path": path,
            "line": line,
            "commit_id": commit_sha,
            "side": "RIGHT",
        }
        if start_line is not None:
            payload["start_line"] = start_line
            payload["start_side"] = "RIGHT"

        response = await self.client.post(
            f"/repos/{repo}/pulls/{pr_number}/comments",
            json=payload,
        )
        response.raise_for_status()

    async def get_review_comments(self, repo: str, pr_number: int) -> list[dict]:
        """Fetch all review comments on a pull request.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            pr_number: The pull request number.

        Returns:
            A list of comment dicts from the GitHub API.
        """
        return await self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/comments")

    async def submit_review(
        self,
        repo: str,
        pr_number: int,
        body: str,
        comments: list[dict],
        commit_sha: str,
    ) -> None:
        """Submit a pull request review with comments.

        Args:
            repo: The full repository name (e.g., "owner/repo").
            pr_number: The pull request number.
            body: The review body text.
            comments: A list of review comment dicts.
            commit_sha: The commit SHA to associate the review with.
        """
        payload: dict[str, Any] = {
            "body": body,
            "event": "COMMENT",
            "commit_id": commit_sha,
            "comments": comments,
        }

        response = await self.client.post(
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            json=payload,
        )
        response.raise_for_status()

    async def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a general comment on a pull request (issue comment)."""
        response = await self.client.post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        response.raise_for_status()

    async def reply_to_comment(self, repo: str, pr_number: int, comment_id: int, body: str) -> None:
        """Reply to an existing review comment thread."""
        response = await self.client.post(
            f"/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            json={"body": body},
        )
        response.raise_for_status()

    async def get_difffox_comments(self, repo: str, pr_number: int) -> list[DiffFoxComment]:
        """Get all inline comments from reviews whose body contains 'DiffFox'.

        Returns list of dicts with: id, path, line, body.
        """
        # Step 1: Find DiffFox review IDs (paginated)
        all_reviews = await self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/reviews")
        difffox_review_ids = set()
        for review in all_reviews:
            if "DiffFox" in (review.get("body") or ""):
                difffox_review_ids.add(review["id"])

        if not difffox_review_ids:
            return []

        # Step 2: Fetch all review comments (paginated via helper)
        all_comments = await self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/comments")

        # Build a map of comment_id -> list of reply bodies (from non-bot users)
        # Build reply maps: human-only + all (including bot)
        user_replies_by_parent: dict[int, list[str]] = {}
        all_replies_by_parent: dict[int, list[str]] = {}
        for c in all_comments:
            parent_id = c.get("in_reply_to_id")
            if parent_id:
                body = c.get("body", "")
                # All replies (for resolution dedup)
                if parent_id not in all_replies_by_parent:
                    all_replies_by_parent[parent_id] = []
                all_replies_by_parent[parent_id].append(body)
                # Human replies only (for acknowledgment context)
                user = c.get("user", {})
                if user.get("type", "") != "Bot":
                    if parent_id not in user_replies_by_parent:
                        user_replies_by_parent[parent_id] = []
                    user_replies_by_parent[parent_id].append(body)

        # Step 3: Filter to DiffFox review comments
        comments: list[dict] = []
        for c in all_comments:
            if c.get("pull_request_review_id") in difffox_review_ids:
                if c.get("in_reply_to_id"):
                    continue
                comments.append(
                    {
                        "id": c["id"],
                        "path": c.get("path", ""),
                        "line": c.get("line", 0) or c.get("original_line", 0),
                        "body": c.get("body", ""),
                        "user_replies": user_replies_by_parent.get(c["id"], []),
                        "all_replies": all_replies_by_parent.get(c["id"], []),
                    }
                )

        return comments
