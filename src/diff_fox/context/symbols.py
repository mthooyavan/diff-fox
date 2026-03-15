"""Symbol extraction from diffs.

Parses diff hunks to identify which code symbols (functions, classes, methods)
were changed, and extracts their signatures and bodies from the source.
"""

from __future__ import annotations

import re
import logging
from pathlib import PurePosixPath

from diff_fox.models import SymbolContext
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for symbol detection across languages
# ---------------------------------------------------------------------------

# Function/method patterns keyed by language extension
_FUNCTION_PATTERNS: dict[str, re.Pattern[str]] = {
    ".py": re.compile(
        r"^[ \t]*(async\s+)?def\s+(?P<name>\w+)\s*\(",
        re.MULTILINE,
    ),
    ".js": re.compile(
        r"(?:^|\s)(?:async\s+)?function\s+(?P<name>\w+)\s*\("
        r"|(?:const|let|var)\s+(?P<name2>\w+)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>)",
        re.MULTILINE,
    ),
    ".ts": re.compile(
        r"(?:^|\s)(?:async\s+)?function\s+(?P<name>\w+)\s*[\(<]"
        r"|(?:const|let|var)\s+(?P<name2>\w+)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>)"
        r"|(?:public|private|protected|static|async)\s+(?P<name3>\w+)\s*\(",
        re.MULTILINE,
    ),
    ".java": re.compile(
        r"(?:public|private|protected|static|\s)+[\w<>\[\],\s]+\s+(?P<name>\w+)\s*\(",
        re.MULTILINE,
    ),
    ".go": re.compile(
        r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(?P<name>\w+)\s*\(",
        re.MULTILINE,
    ),
    ".rb": re.compile(
        r"^\s*def\s+(?:self\.)?(?P<name>\w+[\?!=]?)",
        re.MULTILINE,
    ),
    ".rs": re.compile(
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".kt": re.compile(
        r"^\s*(?:(?:public|private|protected|internal|override|suspend)\s+)*fun\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".swift": re.compile(
        r"^\s*(?:(?:public|private|internal|open|fileprivate|static|class|override|mutating)\s+)*func\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".cpp": re.compile(
        r"(?:[\w:]+\s+)+(?P<name>\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?{",
        re.MULTILINE,
    ),
    ".c": re.compile(
        r"(?:[\w]+\s+)+(?P<name>\w+)\s*\([^)]*\)\s*{",
        re.MULTILINE,
    ),
}

# Class patterns keyed by language extension
_CLASS_PATTERNS: dict[str, re.Pattern[str]] = {
    ".py": re.compile(
        r"^class\s+(?P<name>\w+)\s*[\(:]",
        re.MULTILINE,
    ),
    ".js": re.compile(
        r"^(?:export\s+)?class\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".ts": re.compile(
        r"^(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".java": re.compile(
        r"(?:public|private|protected|abstract|static|\s)*class\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".kt": re.compile(
        r"(?:(?:public|private|protected|internal|abstract|open|data|sealed|enum)\s+)*class\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".rb": re.compile(
        r"^\s*class\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".rs": re.compile(
        r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".swift": re.compile(
        r"^\s*(?:(?:public|private|internal|open|fileprivate|final)\s+)*(?:class|struct|enum|protocol)\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".cpp": re.compile(
        r"^\s*(?:class|struct)\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
    ".c": re.compile(
        r"^\s*(?:struct|enum|typedef\s+struct)\s+(?P<name>\w+)",
        re.MULTILINE,
    ),
}


def _make_qualified_name(file_path: str, name: str) -> str:
    """Build a qualified name from a file path and symbol name.

    Converts ``src/foo/bar.py`` + ``baz`` into ``src.foo.bar.baz``.
    """
    stem = PurePosixPath(file_path).with_suffix("").as_posix().replace("/", ".")
    return f"{stem}.{name}"


def _diff_hunks_for_symbol(
    diff_file: DiffFile,
    start_line: int,
    end_line: int,
) -> list[str]:
    """Collect raw hunk text that overlaps a symbol's line range."""
    hunks: list[str] = []
    for hunk in diff_file.hunks:
        hunk_start = hunk.new_start
        hunk_end = hunk.new_start + hunk.new_lines - 1
        if hunk_start <= end_line and hunk_end >= start_line:
            hunks.append(hunk.content)
    return hunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_changed_symbols_from_diff(
    diff_file: DiffFile,
    file_content: str | None = None,
) -> list[SymbolContext]:
    """Extract symbols that were changed in a diff file.

    Combines hunk line-number analysis with language-aware pattern matching to
    identify which functions, classes, and methods were affected.

    Args:
        diff_file: The diff file with parsed hunks.
        file_content: The full content of the file (new version).  If
            ``None`` only hunk-level heuristics are used.

    Returns:
        A list of ``SymbolContext`` objects for each changed symbol.
    """
    if not diff_file.hunks:
        return []

    ext = PurePosixPath(diff_file.path).suffix.lower()

    changed_lines = _extract_changed_line_numbers(diff_file)
    if not changed_lines:
        return []

    # If we have the full file content, use precise symbol detection
    if file_content is not None:
        if ext == ".py":
            return _extract_python_symbols(
                diff_file, file_content, changed_lines
            )
        else:
            return _extract_generic_symbols(
                diff_file, file_content, changed_lines, ext
            )

    # Fallback: extract symbol names from diff context lines
    symbols: list[SymbolContext] = []
    func_pattern = _FUNCTION_PATTERNS.get(ext)
    class_pattern = _CLASS_PATTERNS.get(ext)

    seen_names: set[str] = set()
    for hunk in diff_file.hunks:
        for line in hunk.content.split("\n"):
            # Check function patterns
            if func_pattern:
                m = func_pattern.search(line)
                if m:
                    name = (
                        m.group("name")
                        or m.groupdict().get("name2")
                        or m.groupdict().get("name3")
                    )
                    if name and name not in seen_names:
                        seen_names.add(name)
                        start = hunk.new_start
                        end = hunk.new_start + hunk.new_lines - 1
                        symbols.append(
                            SymbolContext(
                                name=name,
                                qualified_name=_make_qualified_name(diff_file.path, name),
                                file_path=diff_file.path,
                                symbol_type="function",
                                signature=line.strip(),
                                full_body=line.strip(),
                                change_type="modified",
                                diff_hunks=[hunk.content],
                                line_start=start,
                                line_end=end,
                            )
                        )
            # Check class patterns
            if class_pattern:
                m = class_pattern.search(line)
                if m:
                    name = m.group("name")
                    if name and name not in seen_names:
                        seen_names.add(name)
                        start = hunk.new_start
                        end = hunk.new_start + hunk.new_lines - 1
                        symbols.append(
                            SymbolContext(
                                name=name,
                                qualified_name=_make_qualified_name(diff_file.path, name),
                                file_path=diff_file.path,
                                symbol_type="class",
                                signature=line.strip(),
                                full_body=line.strip(),
                                change_type="modified",
                                diff_hunks=[hunk.content],
                                line_start=start,
                                line_end=end,
                            )
                        )

    return symbols


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_changed_line_numbers(diff_file: DiffFile) -> set[int]:
    """Extract the set of new-side line numbers that were added or modified.

    Args:
        diff_file: The diff file with parsed hunks.

    Returns:
        A set of 1-based line numbers on the new side that have changes.
    """
    changed: set[int] = set()
    for hunk in diff_file.hunks:
        current_line = hunk.new_start
        for raw_line in hunk.content.split("\n"):
            if raw_line.startswith("@@"):
                continue
            if raw_line.startswith("+"):
                changed.add(current_line)
                current_line += 1
            elif raw_line.startswith("-"):
                # Deleted lines don't advance the new-side counter
                # but we note the position as changed
                changed.add(current_line)
            else:
                # Context line
                current_line += 1
    return changed


def _extract_python_symbols(
    diff_file: DiffFile,
    content: str,
    changed_lines: set[int],
) -> list[SymbolContext]:
    """Extract Python symbols overlapping with changed lines.

    Uses indentation-based block detection since Python's structure is
    whitespace-significant.

    Args:
        diff_file: The diff file (for path and hunks).
        content: Full file content.
        changed_lines: Set of changed line numbers.

    Returns:
        List of ``SymbolContext`` for changed Python symbols.
    """
    symbols: list[SymbolContext] = []
    source_lines = _get_source_lines(content)
    if not source_lines:
        return symbols

    func_re = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
    class_re = re.compile(r"^(\s*)class\s+(\w+)\s*[\(:]", re.MULTILINE)

    seen: set[str] = set()

    # Find all function/method definitions
    for m in func_re.finditer(content):
        indent = len(m.group(1))
        name = m.group(3)
        start_line = content[: m.start()].count("\n") + 1
        end_line = _estimate_block_end(source_lines, start_line, indent)
        sym_type = "method" if indent > 0 else "function"

        if _symbol_overlaps_changes(start_line, end_line, changed_lines):
            key = f"{name}:{start_line}"
            if key not in seen:
                seen.add(key)
                sig = _build_python_signature(source_lines, start_line)
                body_lines = source_lines[start_line - 1 : end_line]
                full_body = "\n".join(body_lines)
                symbols.append(
                    SymbolContext(
                        name=name,
                        qualified_name=_make_qualified_name(diff_file.path, name),
                        file_path=diff_file.path,
                        symbol_type=sym_type,
                        signature=sig,
                        full_body=full_body,
                        change_type=_infer_change_type(diff_file),
                        diff_hunks=_diff_hunks_for_symbol(diff_file, start_line, end_line),
                        line_start=start_line,
                        line_end=end_line,
                    )
                )

    # Find all class definitions
    for m in class_re.finditer(content):
        indent = len(m.group(1))
        name = m.group(2)
        start_line = content[: m.start()].count("\n") + 1
        end_line = _estimate_block_end(source_lines, start_line, indent)

        if _symbol_overlaps_changes(start_line, end_line, changed_lines):
            key = f"{name}:{start_line}"
            if key not in seen:
                seen.add(key)
                body_lines = source_lines[start_line - 1 : end_line]
                symbols.append(
                    SymbolContext(
                        name=name,
                        qualified_name=_make_qualified_name(diff_file.path, name),
                        file_path=diff_file.path,
                        symbol_type="class",
                        signature=source_lines[start_line - 1].strip()
                        if start_line <= len(source_lines)
                        else "",
                        full_body="\n".join(body_lines),
                        change_type=_infer_change_type(diff_file),
                        diff_hunks=_diff_hunks_for_symbol(diff_file, start_line, end_line),
                        line_start=start_line,
                        line_end=end_line,
                    )
                )

    return symbols


def _extract_generic_symbols(
    diff_file: DiffFile,
    content: str,
    changed_lines: set[int],
    ext: str,
) -> list[SymbolContext]:
    """Extract symbols for non-Python languages using brace-matching heuristics.

    Args:
        diff_file: The diff file (for path and hunks).
        content: Full file content.
        changed_lines: Set of changed line numbers.
        ext: File extension (e.g. ``".js"``).

    Returns:
        List of ``SymbolContext`` for changed symbols.
    """
    symbols: list[SymbolContext] = []
    source_lines = _get_source_lines(content)
    if not source_lines:
        return symbols

    func_pattern = _FUNCTION_PATTERNS.get(ext)
    class_pattern = _CLASS_PATTERNS.get(ext)

    seen: set[str] = set()

    if func_pattern:
        for m in func_pattern.finditer(content):
            name = m.group("name")
            if name is None:
                # Try alternate named groups
                for grp in ("name2", "name3"):
                    try:
                        name = m.group(grp)
                        if name:
                            break
                    except IndexError:
                        continue
            if not name:
                continue

            start_line = content[: m.start()].count("\n") + 1
            end_line = _estimate_brace_block_end(source_lines, start_line)

            if _symbol_overlaps_changes(start_line, end_line, changed_lines):
                key = f"{name}:{start_line}"
                if key not in seen:
                    seen.add(key)
                    body_lines = source_lines[start_line - 1 : end_line]
                    symbols.append(
                        SymbolContext(
                            name=name,
                            qualified_name=_make_qualified_name(diff_file.path, name),
                            file_path=diff_file.path,
                            symbol_type="function",
                            signature=source_lines[start_line - 1].strip()
                            if start_line <= len(source_lines)
                            else "",
                            full_body="\n".join(body_lines),
                            change_type=_infer_change_type(diff_file),
                            diff_hunks=_diff_hunks_for_symbol(
                                diff_file, start_line, end_line
                            ),
                            line_start=start_line,
                            line_end=end_line,
                        )
                    )

    if class_pattern:
        for m in class_pattern.finditer(content):
            name = m.group("name")
            if not name:
                continue

            start_line = content[: m.start()].count("\n") + 1
            end_line = _estimate_brace_block_end(source_lines, start_line)

            if _symbol_overlaps_changes(start_line, end_line, changed_lines):
                key = f"{name}:{start_line}"
                if key not in seen:
                    seen.add(key)
                    symbols.append(
                        SymbolContext(
                            name=name,
                            qualified_name=_make_qualified_name(diff_file.path, name),
                            file_path=diff_file.path,
                            symbol_type="class",
                            signature=source_lines[start_line - 1].strip()
                            if start_line <= len(source_lines)
                            else "",
                            full_body="",
                            change_type=_infer_change_type(diff_file),
                            diff_hunks=_diff_hunks_for_symbol(
                                diff_file, start_line, end_line
                            ),
                            line_start=start_line,
                            line_end=end_line,
                        )
                    )

    return symbols


def _infer_change_type(diff_file: DiffFile) -> str:
    """Infer the change type from the diff file status."""
    status = diff_file.status.lower()
    if status in ("added", "new"):
        return "added"
    if status in ("removed", "deleted"):
        return "removed"
    if status in ("renamed",):
        return "renamed"
    return "modified"


def _build_python_signature(source_lines: list[str], start_line: int) -> str:
    """Build the full Python function/class signature, handling multi-line defs.

    Args:
        source_lines: All lines of the source file.
        start_line: 1-based line number where the def/class starts.

    Returns:
        The complete signature string.
    """
    if start_line < 1 or start_line > len(source_lines):
        return ""

    sig_parts: list[str] = []
    paren_depth = 0
    for i in range(start_line - 1, len(source_lines)):
        line = source_lines[i]
        sig_parts.append(line.strip())
        paren_depth += line.count("(") - line.count(")")
        if paren_depth <= 0 and (":" in line or "->" in line):
            break
        # Safety: don't scan too many lines for a signature
        if len(sig_parts) > 20:
            break

    return " ".join(sig_parts)


def _get_source_lines(content: str) -> list[str]:
    """Split content into lines, returning an empty list for empty content.

    Args:
        content: The source file content.

    Returns:
        A list of lines.
    """
    if not content:
        return []
    return content.split("\n")


def _estimate_block_end(
    source_lines: list[str],
    start_line: int,
    base_indent: int,
) -> int:
    """Estimate the end line of an indentation-based block (Python).

    Scans forward from *start_line* until a line at the same or lesser
    indentation is found (ignoring blank lines and comments).

    Args:
        source_lines: All lines of the source file.
        start_line: 1-based line number where the block starts.
        base_indent: The indentation level of the ``def``/``class`` keyword.

    Returns:
        The 1-based line number of the last line in the block.
    """
    last_line = start_line
    for i in range(start_line, len(source_lines)):
        line = source_lines[i]
        stripped = line.strip()
        if not stripped:
            continue
        # Determine this line's indentation
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= base_indent and i > start_line - 1:
            # We've exited the block
            break
        last_line = i + 1  # convert to 1-based

    return last_line


def _estimate_brace_block_end(
    source_lines: list[str],
    start_line: int,
) -> int:
    """Estimate the end line of a brace-delimited block.

    Scans forward from *start_line*, counting ``{`` and ``}`` until balanced.

    Args:
        source_lines: All lines of the source file.
        start_line: 1-based line number where the symbol starts.

    Returns:
        The 1-based line number of the closing brace.
    """
    brace_depth = 0
    found_open = False

    for i in range(start_line - 1, len(source_lines)):
        line = source_lines[i]
        for ch in line:
            if ch == "{":
                brace_depth += 1
                found_open = True
            elif ch == "}":
                brace_depth -= 1

        if found_open and brace_depth <= 0:
            return i + 1  # 1-based

    # If no braces found, return a reasonable range
    return min(start_line + 20, len(source_lines))


def _symbol_overlaps_changes(
    start_line: int,
    end_line: int,
    changed_lines: set[int],
) -> bool:
    """Check if a symbol's line range overlaps with changed lines.

    Args:
        start_line: 1-based start of the symbol.
        end_line: 1-based end of the symbol.
        changed_lines: Set of changed line numbers.

    Returns:
        ``True`` if any changed line falls within the symbol's range.
    """
    for line in changed_lines:
        if start_line <= line <= end_line:
            return True
    return False
