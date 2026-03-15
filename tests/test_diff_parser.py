"""Tests for diff parsing."""

from diff_fox.scm.diff_parser import parse_diff_files, parse_patch


def test_parse_empty_patch():
    assert parse_patch("") == []


def test_parse_single_hunk():
    patch = "@@ -1,3 +1,4 @@\n context\n+added line\n context\n context"
    hunks = parse_patch(patch)
    assert len(hunks) == 1
    assert hunks[0].old_start == 1
    assert hunks[0].new_start == 1
    assert hunks[0].new_lines == 4


def test_parse_diff_files():
    files_data = [
        {
            "filename": "src/app.py",
            "status": "modified",
            "additions": 5,
            "deletions": 2,
            "patch": "@@ -1,3 +1,6 @@\n context\n+new\n+new\n+new\n context\n context",
        },
        {
            "filename": "src/new.py",
            "status": "added",
            "additions": 10,
            "deletions": 0,
            "patch": "@@ -0,0 +1,10 @@\n+line1\n+line2",
        },
    ]
    result = parse_diff_files(files_data)
    assert len(result) == 2
    assert result[0].path == "src/app.py"
    assert result[0].status == "modified"
    assert result[1].status == "added"
    assert len(result[0].hunks) == 1


def test_parse_diff_files_no_patch():
    files_data = [{"filename": "binary.png", "status": "added", "additions": 0, "deletions": 0}]
    result = parse_diff_files(files_data)
    assert len(result) == 1
    assert result[0].hunks == []


def test_parse_diff_files_renamed():
    files_data = [
        {
            "filename": "new_name.py",
            "previous_filename": "old_name.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
        }
    ]
    result = parse_diff_files(files_data)
    assert result[0].status == "renamed"
    assert result[0].previous_path == "old_name.py"
