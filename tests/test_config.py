"""Tests for config parsing and resolution."""

from diff_fox.config.loader import parse_config_yaml, resolve_config, should_skip_file
from diff_fox.config.models import ReviewConfig
from diff_fox.constants import ALL_AGENT_NAMES


def test_parse_empty_config():
    config = parse_config_yaml("")
    assert config == ReviewConfig()


def test_parse_invalid_yaml():
    config = parse_config_yaml("{{invalid")
    assert config == ReviewConfig()


def test_parse_agents_boolean():
    config = parse_config_yaml("agents:\n  logic: true\n  security: false")
    assert config.agents["logic"] is True
    assert config.agents["security"] is False


def test_resolve_defaults():
    config = resolve_config(ReviewConfig())
    assert len(config.agents) == len(ALL_AGENT_NAMES)
    for name in ALL_AGENT_NAMES:
        assert config.agents[name].enabled is True


def test_resolve_disable_agent():
    raw = parse_config_yaml("agents:\n  cogs: false")
    config = resolve_config(raw)
    assert config.agents["cogs"].enabled is False
    assert config.agents["logic"].enabled is True


def test_should_skip_file():
    assert should_skip_file("vendor/lib.js", ["vendor/**"]) is True
    assert should_skip_file("src/app.py", ["vendor/**"]) is False
    assert should_skip_file("foo.min.js", ["*.min.js"]) is True


def test_skip_empty_patterns():
    assert should_skip_file("anything.py", []) is False


def test_guidelines_merge():
    repo = parse_config_yaml("guidelines:\n  security:\n    - rule1")
    project = parse_config_yaml("guidelines:\n  security:\n    - rule2")
    config = resolve_config(repo, project)
    assert config.guidelines["security"] == ["rule1", "rule2"]


def test_suppress_filters():
    raw = parse_config_yaml("suppress_filters:\n  - missing docstring")
    config = resolve_config(raw)
    assert "missing docstring" in config.suppress_filters
