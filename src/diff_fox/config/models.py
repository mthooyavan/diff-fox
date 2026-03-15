"""Configuration models for DiffFox."""

from pydantic import BaseModel, Field, field_validator

from diff_fox.constants import ALL_AGENT_NAMES


class AgentPathConfig(BaseModel):
    """Per-agent path and filter configuration."""

    enabled: bool = True
    include: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    suppress_filters: list[str] = Field(default_factory=list)


class ReviewConfig(BaseModel):
    """Raw config parsed from a single .diff-fox/config.yml file."""

    agents: dict[str, AgentPathConfig | bool] | None = Field(default=None)
    guidelines: dict[str, list[str]] = Field(default_factory=dict)
    include: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    skip_rules: list[str] = Field(default_factory=list)
    suppress_filters: list[str] = Field(default_factory=list)
    security_scan_instructions: str | None = Field(default=None)
    jira: dict | None = Field(default=None)

    @field_validator("agents")
    @classmethod
    def validate_agent_names(cls, v):
        if v is None:
            return v
        invalid = set(v) - set(ALL_AGENT_NAMES)
        if invalid:
            raise ValueError(f"Unknown agent names: {invalid}")
        return v


class ResolvedAgentConfig(BaseModel):
    """Resolved per-agent settings after merging."""

    enabled: bool = True
    include: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    suppress_filters: list[str] = Field(default_factory=list)


class ResolvedConfig(BaseModel):
    """Merged config after combining repo-level and project-level."""

    agents: dict[str, ResolvedAgentConfig] = Field(
        default_factory=lambda: {name: ResolvedAgentConfig() for name in ALL_AGENT_NAMES},
    )
    guidelines: dict[str, list[str]] = Field(default_factory=dict)
    include: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    suppress_filters: list[str] = Field(default_factory=list)
    security_scan_instructions: str | None = Field(default=None)
    jira_enabled: bool | None = Field(default=None)
