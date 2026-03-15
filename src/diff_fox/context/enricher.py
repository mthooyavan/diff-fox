"""Context enrichment pipeline.

Orchestrates the full enrichment flow for a set of diff files:

1. Fetch file contents for each diff file.
2. Extract changed symbols from each file.
3. Find call sites (callers) for each symbol.
4. Extract callees from each symbol's body.
5. Run impact analysis for each symbol against its call sites.
6. Collect related file paths referenced by call sites.

The pipeline is fully async and uses bounded concurrency.
"""

from __future__ import annotations

import asyncio
import logging

from diff_fox.context.call_graph import extract_callees_from_body, find_call_sites
from diff_fox.context.impact import analyze_impact
from diff_fox.context.symbols import extract_changed_symbols_from_diff
from diff_fox.models import (
    CallSite,
    Callee,
    EnrichedContext,
    ImpactEntry,
    SymbolContext,
)
from diff_fox.scm.base import SCMProvider
from diff_fox.scm.models import DiffFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTEXT_FILES = 50
MAX_CONCURRENT_FETCHES = 15

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def enrich_context(
    diff_files: list[DiffFile],
    repo: str,
    head_ref: str,
    scm: SCMProvider,
) -> EnrichedContext:
    """Run the full enrichment pipeline over a list of diff files.

    For each file:
    1. Fetches the current file content.
    2. Extracts changed symbols.
    3. Finds call sites and callees for each symbol.
    4. Runs impact analysis.
    5. Gathers related file paths.

    Args:
        diff_files: The diff files to enrich.
        scm: An SCM provider for fetching content and searching code.
        repo: The repository identifier (e.g. ``"owner/repo"``).
        ref: The git ref (SHA, branch, tag) to read from.

    Returns:
        A single ``EnrichedContext`` aggregating all files.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

    all_symbols: list[SymbolContext] = []
    all_call_sites: dict[str, list[CallSite]] = {}
    all_callees: dict[str, list[Callee]] = {}
    all_impact_map: dict[str, list[ImpactEntry]] = {}
    all_related_files: set[str] = set()

    async def _enrich_one(diff_file: DiffFile) -> None:
        async with semaphore:
            try:
                await _enrich_single_file(
                    diff_file, scm, repo, head_ref,
                    all_symbols, all_call_sites, all_callees,
                    all_impact_map, all_related_files,
                )
            except Exception:
                logger.warning(
                    "Enrichment failed for %s",
                    diff_file.path,
                    exc_info=True,
                )

    tasks = [_enrich_one(df) for df in diff_files]
    await asyncio.gather(*tasks)

    return EnrichedContext(
        symbols=all_symbols,
        call_sites=all_call_sites,
        callees=all_callees,
        impact_map=all_impact_map,
        related_files=sorted(all_related_files),
    )


# ---------------------------------------------------------------------------
# Internal pipeline steps
# ---------------------------------------------------------------------------


async def _enrich_single_file(
    diff_file: DiffFile,
    scm: SCMProvider,
    repo: str,
    ref: str,
    out_symbols: list[SymbolContext],
    out_call_sites: dict[str, list[CallSite]],
    out_callees: dict[str, list[Callee]],
    out_impact_map: dict[str, list[ImpactEntry]],
    out_related_files: set[str],
) -> None:
    """Enrich a single diff file, appending results to the shared outputs.

    Args:
        diff_file: The diff file to enrich.
        scm: An SCM provider.
        repo: The repository identifier.
        ref: The git ref to read from.
        out_symbols: Shared list to append extracted symbols.
        out_call_sites: Shared dict to populate call sites keyed by qualified name.
        out_callees: Shared dict to populate callees keyed by qualified name.
        out_impact_map: Shared dict to populate impact entries keyed by qualified name.
        out_related_files: Shared set to collect related file paths.
    """
    # Step 1: Fetch the file content
    file_content: str | None = None
    try:
        fc = await scm.get_file_content(repo, diff_file.path, ref)
        file_content = fc.content
    except Exception:
        logger.debug(
            "Could not fetch content for %s (may be deleted)",
            diff_file.path,
            exc_info=True,
        )

    # Step 2: Extract changed symbols
    symbols = extract_changed_symbols_from_diff(diff_file, file_content)
    logger.debug(
        "Extracted %d symbols from %s",
        len(symbols),
        diff_file.path,
    )

    if not symbols:
        return

    out_symbols.extend(symbols)

    # Step 3 & 4: Find call sites and extract callees (concurrently per symbol)
    call_site_tasks = [
        find_call_sites(sym, scm, repo, ref) for sym in symbols
    ]
    call_site_results = await asyncio.gather(*call_site_tasks, return_exceptions=True)

    for sym, cs_result in zip(symbols, call_site_results):
        qname = sym.qualified_name

        # Call sites
        sym_call_sites: list[CallSite] = []
        if isinstance(cs_result, Exception):
            logger.debug(
                "Call site search failed for %s: %s",
                sym.name,
                cs_result,
            )
        else:
            sym_call_sites = cs_result
            if sym_call_sites:
                out_call_sites[qname] = sym_call_sites
                # Collect related files from call sites
                for cs in sym_call_sites:
                    if cs.file_path and cs.file_path != diff_file.path:
                        out_related_files.add(cs.file_path)

        # Callees
        try:
            callee_names = extract_callees_from_body(sym)
            if callee_names:
                callee_objects = [
                    Callee(name=name, file_path="", signature="")
                    for name in callee_names
                ]
                out_callees[qname] = callee_objects
        except Exception:
            logger.debug(
                "Callee extraction failed for %s",
                sym.name,
                exc_info=True,
            )

        # Step 5: Impact analysis
        try:
            impact_entries = analyze_impact(sym, sym_call_sites)
            if impact_entries:
                out_impact_map[qname] = impact_entries
        except Exception:
            logger.debug(
                "Impact analysis failed for %s",
                sym.name,
                exc_info=True,
            )
