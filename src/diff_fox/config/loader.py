"""Configuration loader and merger for .diff-fox/config.yml."""

import fnmatch
import logging
from pathlib import PurePosixPath

import httpx
import yaml

from diff_fox.config.models import AgentPathConfig, ResolvedAgentConfig, ResolvedConfig, ReviewConfig
from diff_fox.constants import ALL_AGENT_NAMES, CONFIG_FILE_NAME, MAX_PROJECT_CONFIG_DEPTH
from diff_fox.scm.base import SCMProvider

logger = logging.getLogger(__name__)


def parse_config_yaml(content: str) -> ReviewConfig:
    if not content or not content.strip():
        return ReviewConfig()
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        logger.warning("Failed to parse config YAML — using defaults")
        return ReviewConfig()
    if not isinstance(data, dict):
        return ReviewConfig()
    return ReviewConfig.model_validate(data)


def _normalize_agent_config(value) -> ResolvedAgentConfig:
    if isinstance(value, bool):
        return ResolvedAgentConfig(enabled=value)
    if isinstance(value, AgentPathConfig):
        return ResolvedAgentConfig(
            enabled=value.enabled, include=list(value.include),
            skip=list(value.skip), suppress_filters=list(value.suppress_filters),
        )
    if isinstance(value, dict):
        return ResolvedAgentConfig(**value)
    return ResolvedAgentConfig(enabled=bool(value))


def resolve_config(
    repo_config: ReviewConfig,
    project_config: ReviewConfig | None = None,
) -> ResolvedConfig:
    merged_agents = {n: ResolvedAgentConfig() for n in ALL_AGENT_NAMES}
    if repo_config.agents is not None:
        for name, val in repo_config.agents.items():
            if name in ALL_AGENT_NAMES:
                merged_agents[name] = _normalize_agent_config(val)

    merged_guidelines = {cat: list(rules) for cat, rules in repo_config.guidelines.items()}
    merged_include = list(repo_config.include)
    merged_skip = list(repo_config.skip)
    merged_suppress = list(repo_config.suppress_filters)
    merged_security_scan = repo_config.security_scan_instructions

    merged_jira_enabled = None
    if repo_config.jira and isinstance(repo_config.jira, dict):
        merged_jira_enabled = repo_config.jira.get("enabled")

    if project_config:
        if project_config.agents is not None:
            for name, val in project_config.agents.items():
                if name in ALL_AGENT_NAMES:
                    project_agent = _normalize_agent_config(val)
                    existing = merged_agents[name]
                    merged_agents[name] = ResolvedAgentConfig(
                        enabled=project_agent.enabled,
                        include=existing.include + project_agent.include,
                        skip=existing.skip + project_agent.skip,
                        suppress_filters=existing.suppress_filters + project_agent.suppress_filters,
                    )

        if project_config.skip_rules:
            _apply_skip_rules(merged_guidelines, project_config.skip_rules)

        for cat, rules in project_config.guidelines.items():
            if cat not in merged_guidelines:
                merged_guidelines[cat] = []
            merged_guidelines[cat].extend(rules)

        merged_include.extend(project_config.include)
        merged_skip.extend(project_config.skip)
        merged_suppress.extend(project_config.suppress_filters)

        if project_config.security_scan_instructions:
            merged_security_scan = project_config.security_scan_instructions

        if project_config.jira and isinstance(project_config.jira, dict):
            merged_jira_enabled = project_config.jira.get("enabled", merged_jira_enabled)

    return ResolvedConfig(
        agents=merged_agents, guidelines=merged_guidelines,
        include=merged_include, skip=merged_skip,
        suppress_filters=merged_suppress,
        security_scan_instructions=merged_security_scan,
        jira_enabled=merged_jira_enabled,
    )


def _apply_skip_rules(guidelines: dict[str, list[str]], skip_rules: list[str]) -> None:
    skip_lower = [r.lower() for r in skip_rules]
    for cat in guidelines:
        guidelines[cat] = [
            rule for rule in guidelines[cat]
            if not any(skip in rule.lower() for skip in skip_lower)
        ]


def should_include_file(file_path: str, include_patterns: list[str]) -> bool:
    if not include_patterns:
        return True
    normalized = PurePosixPath(file_path).as_posix()
    basename = PurePosixPath(normalized).name
    for pattern in include_patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(basename, pattern):
            return True
    return False


def filter_files_for_agent(
    diff_files: list,
    global_include: list[str],
    global_skip: list[str],
    agent_include: list[str] | None = None,
    agent_skip: list[str] | None = None,
) -> list:
    result = []
    for f in diff_files:
        path = f.path if hasattr(f, "path") else str(f)
        if not should_include_file(path, global_include):
            continue
        if should_skip_file(path, global_skip):
            continue
        if agent_include and not should_include_file(path, agent_include):
            continue
        if agent_skip and should_skip_file(path, agent_skip):
            continue
        result.append(f)
    return result


def should_skip_file(file_path: str, skip_patterns: list[str]) -> bool:
    if not skip_patterns:
        return False
    normalized = PurePosixPath(file_path).as_posix()
    basename = PurePosixPath(normalized).name
    for pattern in skip_patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(basename, pattern):
            return True
    return False


async def load_config_from_repo(
    repo: str,
    ref: str,
    scm: SCMProvider,
    changed_files: list[str] | None = None,
) -> ResolvedConfig:
    repo_config = await _fetch_config(repo, CONFIG_FILE_NAME, ref, scm)
    project_config = None
    if changed_files:
        project_config = await _find_project_config(repo, ref, scm, changed_files)
    resolved = resolve_config(repo_config, project_config)
    logger.info("Config loaded for %s", repo)
    return resolved


async def _fetch_config(repo: str, path: str, ref: str, scm: SCMProvider) -> ReviewConfig:
    try:
        fc = await scm.get_file_content(repo, path, ref)
        return parse_config_yaml(fc.content)
    except (FileNotFoundError, httpx.HTTPStatusError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code != 404:
            logger.warning("Error fetching config at %s: %s", path, exc)
        return ReviewConfig()
    except Exception:
        logger.warning("Error fetching config at %s", path, exc_info=True)
        return ReviewConfig()


async def _find_project_config(
    repo: str, ref: str, scm: SCMProvider, changed_files: list[str],
) -> ReviewConfig | None:
    primary_dir = _find_primary_subtree(changed_files)
    if not primary_dir:
        return None
    parts = PurePosixPath(primary_dir).parts
    depth = min(len(parts), MAX_PROJECT_CONFIG_DEPTH)
    for i in range(len(parts), max(len(parts) - depth, 0), -1):
        dir_path = "/".join(parts[:i])
        config_path = f"{dir_path}/{CONFIG_FILE_NAME}"
        try:
            fc = await scm.get_file_content(repo, config_path, ref)
            config = parse_config_yaml(fc.content)
            logger.info("Found project config at %s", config_path)
            return config
        except (FileNotFoundError, httpx.HTTPStatusError):
            continue
        except Exception:
            continue
    return None


def _find_primary_subtree(changed_files: list[str]) -> str | None:
    if not changed_files:
        return None
    dir_counts: dict[str, int] = {}
    deepest_per_dir: dict[str, str] = {}
    for f in changed_files:
        parts = PurePosixPath(f).parts
        if len(parts) < 2:
            continue
        top_dir = parts[0]
        dir_counts[top_dir] = dir_counts.get(top_dir, 0) + 1
        parent = "/".join(parts[:-1])
        if top_dir not in deepest_per_dir or len(parent) > len(deepest_per_dir[top_dir]):
            deepest_per_dir[top_dir] = parent
    if not dir_counts:
        return None
    primary = max(dir_counts, key=lambda d: dir_counts[d])
    return deepest_per_dir.get(primary)
