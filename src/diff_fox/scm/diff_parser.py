"""Diff parsing utilities for unified diff format."""

import re

from diff_fox.scm.models import DiffFile, DiffHunk

HUNK_HEADER_RE = r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"


def parse_patch(patch: str) -> list[DiffHunk]:
    """Parse a unified diff patch string into a list of DiffHunk objects.

    Uses regex to find hunk headers and extract the content for each hunk.

    Args:
        patch: A unified diff patch string.

    Returns:
        A list of DiffHunk objects parsed from the patch.
    """
    hunks: list[DiffHunk] = []
    if not patch:
        return hunks

    lines = patch.split("\n")
    current_hunk: DiffHunk | None = None
    current_lines: list[str] = []

    for line in lines:
        match = re.match(HUNK_HEADER_RE, line)
        if match:
            # Save the previous hunk if it exists
            if current_hunk is not None:
                current_hunk.content = "\n".join(current_lines)
                hunks.append(current_hunk)

            old_start = int(match.group(1))
            old_lines = int(match.group(2)) if match.group(2) else 1
            new_start = int(match.group(3))
            new_lines = int(match.group(4)) if match.group(4) else 1

            current_hunk = DiffHunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                content="",
            )
            current_lines = [line]
        elif current_hunk is not None:
            current_lines.append(line)

    # Don't forget the last hunk
    if current_hunk is not None:
        current_hunk.content = "\n".join(current_lines)
        hunks.append(current_hunk)

    return hunks


def parse_diff_files(files_data: list[dict]) -> list[DiffFile]:
    """Parse GitHub API files response into a list of DiffFile objects.

    Args:
        files_data: A list of file dicts from the GitHub API pull request files endpoint.

    Returns:
        A list of DiffFile objects with parsed hunks.
    """
    diff_files: list[DiffFile] = []

    for file_data in files_data:
        patch = file_data.get("patch", "")
        hunks = parse_patch(patch) if patch else []

        diff_file = DiffFile(
            path=file_data.get("filename", ""),
            previous_path=file_data.get("previous_filename"),
            status=file_data.get("status", "modified"),
            additions=file_data.get("additions", 0),
            deletions=file_data.get("deletions", 0),
            patch=patch or None,
            hunks=hunks,
        )
        diff_files.append(diff_file)

    return diff_files
