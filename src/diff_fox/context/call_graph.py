"""Call-graph analysis for changed symbols.

Finds call sites (callers) and callees for symbols that were changed in a
diff.  Uses text-based search through the SCM provider for cross-file lookup
and regex-based extraction for callee analysis within function bodies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath

from diff_fox.models import CallSite, SymbolContext
from diff_fox.scm.base import SCMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CALL_SITES_PER_SYMBOL = 20
SURROUNDING_LINES = 10

# Semaphore to limit concurrent SCM searches
_search_semaphore = asyncio.Semaphore(10)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def find_call_sites(
    symbol: SymbolContext,
    scm: SCMProvider,
    repo: str,
    ref: str,
) -> list[CallSite]:
    """Find locations where *symbol* is called across the repository.

    Uses the SCM provider's code-search API to find references, then
    extracts surrounding context for each hit.

    Args:
        symbol: The symbol to search for.
        scm: An SCM provider for code search.
        repo: The repository identifier.
        ref: The git ref to search at.

    Returns:
        A list of ``CallSite`` objects, capped at ``MAX_CALL_SITES_PER_SYMBOL``.
    """
    call_sites: list[CallSite] = []

    try:
        async with _search_semaphore:
            results = await scm.search_code(repo, symbol.name)
    except Exception:
        logger.debug("Code search failed for symbol '%s'", symbol.name, exc_info=True)
        return call_sites

    for result in results[:MAX_CALL_SITES_PER_SYMBOL * 2]:
        file_path = result.get("path", "")
        # Skip the file where the symbol is defined
        if file_path == symbol.file_path:
            continue

        # Try to get the content for context extraction
        try:
            content = result.get("content", "")
            if not content:
                async with _search_semaphore:
                    file_obj = await scm.get_file_content(repo, file_path, ref)
                content = file_obj.content
        except Exception:
            logger.debug("Could not fetch content for %s", file_path, exc_info=True)
            continue

        # Find all occurrences of the symbol in this file
        found = _find_symbol_in_content(symbol.name, content, file_path)
        for site in found:
            call_sites.append(site)
            if len(call_sites) >= MAX_CALL_SITES_PER_SYMBOL:
                return call_sites

    return call_sites


def extract_callees_from_body(
    symbol: SymbolContext,
) -> list[str]:
    """Extract names of functions/methods called within a symbol's body.

    Uses language-specific heuristics to identify call expressions.

    Args:
        symbol: The symbol whose body to analyze.

    Returns:
        A deduplicated list of callee names.
    """
    if not symbol.full_body:
        return []

    ext = PurePosixPath(symbol.file_path).suffix.lower()

    if ext == ".py":
        return _extract_python_callees(symbol.full_body, symbol.name)
    else:
        return _extract_generic_callees(symbol.full_body, symbol.name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_symbol_in_content(
    name: str,
    content: str,
    file_path: str,
) -> list[CallSite]:
    """Find all occurrences of *name* as a call in *content*.

    Args:
        name: The symbol name to search for.
        content: The file content to search in.
        file_path: The path of the file (for ``CallSite.file_path``).

    Returns:
        A list of ``CallSite`` objects.
    """
    sites: list[CallSite] = []
    lines = content.split("\n")

    # Match the symbol name followed by ( -- a function call
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")

    for i, line in enumerate(lines):
        m = pattern.search(line)
        if m:
            line_number = i + 1  # 1-based
            surrounding = _get_surrounding_lines(lines, i, SURROUNDING_LINES)
            caller = _find_enclosing_function(lines, i)
            call_expr = line.strip()
            sites.append(
                CallSite(
                    file_path=file_path,
                    line_number=line_number,
                    surrounding_code=surrounding,
                    caller_function=caller or None,
                    call_expression=call_expr,
                )
            )

    return sites


def _extract_python_callees(body: str, own_name: str) -> list[str]:
    """Extract callee names from a Python function body.

    Args:
        body: The source code body of the function.
        own_name: The function's own name (excluded from results).

    Returns:
        Deduplicated list of callee names.
    """
    # Match function calls: name( or obj.name(
    call_re = re.compile(r"(?<!\bdef\s)(?<!\bclass\s)\b([\w.]+)\s*\(")
    callees: list[str] = []
    seen: set[str] = set()

    # Common builtins and keywords to skip
    skip = {
        "if", "elif", "while", "for", "with", "assert", "return", "yield",
        "raise", "except", "import", "from", "print", "len", "range",
        "enumerate", "zip", "map", "filter", "sorted", "reversed",
        "list", "dict", "set", "tuple", "str", "int", "float", "bool",
        "isinstance", "issubclass", "type", "super", "property",
        "staticmethod", "classmethod", "hasattr", "getattr", "setattr",
        "delattr", "callable", "repr", "id", "hash", "abs", "round",
        "min", "max", "sum", "any", "all", "next", "iter", "open",
        own_name,
    }

    for m in call_re.finditer(body):
        raw_name = m.group(1)
        name = _get_call_name(raw_name)
        if name and name not in seen and name not in skip:
            seen.add(name)
            callees.append(name)

    return callees


def _get_call_name(raw: str) -> str:
    """Extract the meaningful part of a dotted call expression.

    For ``self.foo``, returns ``foo``.
    For ``module.bar``, returns ``bar``.
    For ``foo``, returns ``foo``.

    Args:
        raw: The raw matched call expression (may contain dots).

    Returns:
        The extracted call name.
    """
    parts = raw.split(".")
    if len(parts) >= 2:
        # Skip common prefixes like self, cls
        if parts[0] in ("self", "cls"):
            return parts[-1]
        # Return the full dotted name for module calls
        return raw
    return raw


def _extract_generic_callees(body: str, own_name: str) -> list[str]:
    """Extract callee names from a function body for non-Python languages.

    Uses a simpler pattern that looks for ``identifier(`` patterns.

    Args:
        body: The source code body.
        own_name: The function's own name (excluded from results).

    Returns:
        Deduplicated list of callee names.
    """
    call_re = re.compile(r"\b(\w+)\s*\(")
    callees: list[str] = []
    seen: set[str] = set()

    # Common keywords across languages to skip
    skip = {
        "if", "else", "while", "for", "switch", "case", "return", "throw",
        "new", "delete", "sizeof", "typeof", "instanceof", "catch", "try",
        "finally", "class", "struct", "enum", "interface", "function",
        "var", "let", "const", "auto", "void", "int", "float", "double",
        "string", "bool", "boolean", "char", "byte", "long", "short",
        own_name,
    }

    for m in call_re.finditer(body):
        name = m.group(1)
        if name and name not in seen and name not in skip:
            seen.add(name)
            callees.append(name)

    return callees


def _get_surrounding_lines(
    lines: list[str],
    index: int,
    context: int,
) -> str:
    """Extract lines surrounding a given index.

    Args:
        lines: All lines of the file.
        index: The 0-based line index to center on.
        context: Number of lines of context on each side.

    Returns:
        The surrounding lines joined as a single string.
    """
    start = max(0, index - context)
    end = min(len(lines), index + context + 1)
    return "\n".join(lines[start:end])


def _find_enclosing_function(
    lines: list[str],
    index: int,
) -> str:
    """Find the name of the function enclosing the line at *index*.

    Scans backwards from *index* looking for a function definition pattern.

    Args:
        lines: All lines of the file.
        index: The 0-based line index to search from.

    Returns:
        The enclosing function name, or an empty string if not found.
    """
    # Patterns for function definitions across common languages
    func_patterns = [
        re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),        # Python
        re.compile(r"^\s*(?:async\s+)?function\s+(\w+)\s*\("),    # JS
        re.compile(r"^\s*(?:public|private|protected|static|\s)*[\w<>\[\],\s]+\s+(\w+)\s*\("),  # Java/C#
        re.compile(r"^\s*func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*\("),  # Go
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"),    # Rust
    ]

    for i in range(index, -1, -1):
        line = lines[i]
        for pat in func_patterns:
            m = pat.match(line)
            if m:
                return m.group(1)

    return ""
