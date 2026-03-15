"""Impact analysis for changed symbols.

Analyzes the potential impact of code changes by examining the changed
symbol's properties (return types, exceptions, parameters) and how callers
use the symbol (None-handling, error-handling, argument counts).
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from diff_fox.models import CallSite, ImpactEntry, SymbolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_impact(
    symbol: SymbolContext,
    call_sites: list[CallSite],
) -> list[ImpactEntry]:
    """Analyze the impact of a changed symbol on its callers.

    Examines:
    - Whether the symbol can return ``None``/``Optional``.
    - Whether it raises exceptions.
    - Its parameter count.
    - Whether callers handle ``None`` returns.
    - Whether callers have error handling.

    Args:
        symbol: The changed symbol to analyze.
        call_sites: The call sites where this symbol is used.

    Returns:
        A list of ``ImpactEntry`` objects describing potential impacts.
    """
    entries: list[ImpactEntry] = []

    has_opt = _has_optional_return(symbol)
    raises = _raises_exceptions(symbol)
    param_count = _count_parameters(symbol)

    for cs in call_sites:
        # Check if the caller handles None returns
        if has_opt and not _caller_handles_none(cs):
            entries.append(
                ImpactEntry(
                    file_path=cs.file_path,
                    line_number=cs.line_number,
                    caller_function=cs.caller_function,
                    impact_type="return_type_change",
                    description=(
                        f"'{symbol.name}' can return None/Optional but caller "
                        f"'{cs.caller_function or '<unknown>'}' does not appear to handle it."
                    ),
                    severity="high",
                )
            )

        # Check if the caller handles exceptions
        if raises and not _caller_has_error_handling(cs):
            entries.append(
                ImpactEntry(
                    file_path=cs.file_path,
                    line_number=cs.line_number,
                    caller_function=cs.caller_function,
                    impact_type="behavior_change",
                    description=(
                        f"'{symbol.name}' raises exceptions but caller "
                        f"'{cs.caller_function or '<unknown>'}' has no error handling."
                    ),
                    severity="high",
                )
            )

        # Check argument count mismatches
        arg_count = _count_call_arguments(cs.call_expression, symbol.name)
        if arg_count >= 0 and param_count > 0 and arg_count != param_count:
            entries.append(
                ImpactEntry(
                    file_path=cs.file_path,
                    line_number=cs.line_number,
                    caller_function=cs.caller_function,
                    impact_type="param_change",
                    description=(
                        f"'{symbol.name}' expects {param_count} parameter(s) but "
                        f"call site passes {arg_count} argument(s)."
                    ),
                    severity="medium",
                )
            )

    return entries


# ---------------------------------------------------------------------------
# Return type analysis
# ---------------------------------------------------------------------------


def _has_optional_return(symbol: SymbolContext) -> bool:
    """Check if a symbol can return ``None`` or has an ``Optional`` return type.

    Examines both the signature (type annotations) and the body (bare
    ``return`` statements, explicit ``return None``).

    Args:
        symbol: The symbol to check.

    Returns:
        ``True`` if the symbol may return ``None``.
    """
    # Check signature for Optional or None in return type
    sig = symbol.signature
    if sig:
        # Python-style: -> Optional[X] or -> X | None
        if re.search(r"->\s*Optional\b", sig):
            return True
        if re.search(r"->\s*.*\|\s*None", sig):
            return True
        if re.search(r"->\s*None\b", sig):
            return True

    # Check body for return None or bare return
    body = symbol.full_body
    if body:
        # Bare return (no value)
        if re.search(r"^\s*return\s*$", body, re.MULTILINE):
            return True
        # Explicit return None
        if re.search(r"^\s*return\s+None\b", body, re.MULTILINE):
            return True

    return False


def _caller_handles_none(call_site: CallSite) -> bool:
    """Check if a call site handles potential ``None`` returns.

    Looks for patterns like ``if result is None``, ``if result:``,
    ``or`` fallbacks, etc. in the surrounding code.

    Args:
        call_site: The call site to check.

    Returns:
        ``True`` if the caller appears to handle ``None``.
    """
    snippet = call_site.surrounding_code
    if not snippet:
        return True  # Assume handled if we can't see the code

    # Look for None-checking patterns
    none_patterns = [
        r"\bis\s+None\b",
        r"\bis\s+not\s+None\b",
        r"\bif\s+\w+\s*:",  # Truthiness check
        r"\bif\s+not\s+\w+\s*:",  # Falsiness check
        r"\bor\s+",  # Fallback with or
        r"\?\.",  # Optional chaining (JS/TS)
        r"\?\?",  # Nullish coalescing (JS/TS)
        r"\.unwrap_or",  # Rust unwrap_or
        r"\.unwrap\(\)",  # Rust unwrap (somewhat handles it)
        r"\.getOrElse",  # Scala/Kotlin
        r"\borElse\b",  # Java Optional
    ]

    for pattern in none_patterns:
        if re.search(pattern, snippet):
            return True

    return False


# ---------------------------------------------------------------------------
# Parameter analysis
# ---------------------------------------------------------------------------


def _count_parameters(symbol: SymbolContext) -> int:
    """Count the number of parameters a symbol accepts.

    Uses language-specific parsing based on the file extension.

    Args:
        symbol: The symbol to analyze.

    Returns:
        The number of parameters (0 if unable to determine).
    """
    if not symbol.signature:
        return 0

    ext = PurePosixPath(symbol.file_path).suffix.lower()
    if ext == ".py":
        return _count_python_params(symbol.signature)
    else:
        return _count_generic_params(symbol.signature)


def _count_python_params(signature: str) -> int:
    """Count parameters in a Python function signature.

    Excludes ``self`` and ``cls`` from the count.

    Args:
        signature: The function signature string.

    Returns:
        The number of parameters.
    """
    # Extract the part between parentheses
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        # Try multi-line: just grab everything after the first (
        m = re.search(r"\((.+)", signature, re.DOTALL)
        if not m:
            return 0

    params_str = m.group(1).strip()
    if not params_str:
        return 0

    # Split by comma, handling nested brackets
    params: list[str] = []
    depth = 0
    current = ""
    for ch in params_str:
        if ch in ("(", "[", "{"):
            depth += 1
            current += ch
        elif ch in (")", "]", "}"):
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            params.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        params.append(current.strip())

    # Filter out self, cls, *, /, and **kwargs-like
    count = 0
    for p in params:
        name = p.split(":")[0].split("=")[0].strip()
        if name in ("self", "cls", "*", "/"):
            continue
        if not name:
            continue
        count += 1

    return count


def _count_generic_params(signature: str) -> int:
    """Count parameters in a generic (non-Python) function signature.

    Args:
        signature: The function signature string.

    Returns:
        The number of parameters.
    """
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return 0

    params_str = m.group(1).strip()
    if not params_str:
        return 0

    # Simple comma-split with nesting awareness
    depth = 0
    count = 1  # At least one param if params_str is non-empty
    for ch in params_str:
        if ch in ("<", "(", "[", "{"):
            depth += 1
        elif ch in (">", ")", "]", "}"):
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1

    return count


def _count_call_arguments(call_expression: str, symbol_name: str) -> int:
    """Count the number of arguments passed at a call site.

    Args:
        call_expression: The call expression string.
        symbol_name: The symbol being called.

    Returns:
        The number of arguments, or -1 if unable to determine.
    """
    pattern = re.compile(re.escape(symbol_name) + r"\s*\(([^)]*)\)")
    m = pattern.search(call_expression)
    if not m:
        return -1

    args_str = m.group(1).strip()
    if not args_str:
        return 0

    # Count commas at depth 0
    depth = 0
    count = 1
    for ch in args_str:
        if ch in ("(", "[", "{", "<"):
            depth += 1
        elif ch in (")", "]", "}", ">"):
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1

    return count


# ---------------------------------------------------------------------------
# Exception analysis
# ---------------------------------------------------------------------------


def _raises_exceptions(symbol: SymbolContext) -> bool:
    """Check if a symbol raises exceptions.

    Looks for ``raise`` statements (Python), ``throw`` statements
    (Java/JS/TS/C++), and ``panic!`` (Rust) in the body.

    Args:
        symbol: The symbol to check.

    Returns:
        ``True`` if the symbol raises/throws exceptions.
    """
    body = symbol.full_body
    if not body:
        return False

    # Language-agnostic: look for raise/throw patterns
    patterns = [
        r"^\s*raise\s+\w+",  # Python raise
        r"^\s*throw\s+",  # Java/JS/TS/C++ throw
        r"\bpanic!\s*\(",  # Rust panic
        r"\bunwrap\(\)",  # Rust unwrap (can panic)
        r"\bexpect\(",  # Rust expect (can panic)
    ]

    for pattern in patterns:
        if re.search(pattern, body, re.MULTILINE):
            return True

    return False


def _caller_has_error_handling(call_site: CallSite) -> bool:
    """Check if a call site has error handling around the call.

    Looks for try/except, try/catch, error checking patterns in the
    surrounding code.

    Args:
        call_site: The call site to check.

    Returns:
        ``True`` if the caller has error handling.
    """
    snippet = call_site.surrounding_code
    if not snippet:
        return True  # Assume handled if we can't see the code

    error_patterns = [
        r"\btry\s*:",  # Python try
        r"\btry\s*\{",  # Java/JS/TS/C++ try
        r"\bexcept\s+",  # Python except
        r"\bcatch\s*\(",  # Java/JS/TS/C++ catch
        r"\bresult\s*\.\s*is_err\b",  # Rust Result check
        r"\bif\s+err\s*!=\s*nil\b",  # Go error check
        r"\.catch\(",  # JS promise catch
        r"\.then\(",  # JS promise chain (partial handling)
    ]

    for pattern in error_patterns:
        if re.search(pattern, snippet):
            return True

    return False
