"""One explicit configuration merge, with secret-safe diagnostics."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic.alias_generators import to_camel

from kaki.types import KakiError


class Options(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Defaults(Options):
    provider: Literal["openai", "auto"] = "openai"
    model: str = "openai/gpt-5.6-sol"
    workspace: str | None = None
    max_tokens: int = Field(default=8192, ge=64)
    context_window_tokens: int = Field(default=65536, ge=1024)
    max_tool_iterations: int = Field(default=40, ge=1, le=200)
    temperature: float = 0.1
    empty_recovery_enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3

    @model_validator(mode="after")
    def budget_valid(self):
        if self.max_tokens >= self.context_window_tokens - 256:
            raise ValueError("maxTokens必须小于contextWindowTokens并留出输入空间")
        return self


class Agents(Options):
    defaults: Defaults = Field(default_factory=Defaults)


class OpenAIOptions(Options):
    api_base: str = "https://api.site/v1"
    api_key: SecretStr = SecretStr("")
    extra_headers: dict[str, str] | None = None
    models: list[str] = Field(default_factory=list)
    timeout: float = Field(default=120, ge=5, le=600)


class Providers(Options):
    openai: OpenAIOptions = Field(default_factory=OpenAIOptions)


class ContextOptions(Options):
    engine: Literal["unified"] = "unified"
    fast_path_threshold: float = Field(default=0.6, ge=0, le=1)
    curator_model: str | None = None
    curator_timeout_seconds: float = Field(default=30, gt=0, le=300)
    protect_first_n: int = Field(default=3, ge=0)
    archive_dir: str = "context/archive"
    relevance_decay: float = Field(default=0.95, gt=0, le=1)
    relevance_reference_boost: float = Field(default=0.15, ge=0, le=1)


class MemoryOptions(Options):
    backend: Literal["local"] | None = "local"
    user_id: str = "default"
    memory_top_k: int = Field(default=5, ge=0, le=30)


class CheckpointOptions(Options):
    policy: Literal["interactive", "always", "never"] = "interactive"
    shadow_dir: str = "checkpoints/kaki-shadow.git"


class RuntimeOptions(Options):
    checkpoint: CheckpointOptions = Field(default_factory=CheckpointOptions)


class ExecOptions(Options):
    timeout: int = Field(default=60, ge=1, le=600)
    path_append: str = ""


class McpOptions(Options):
    type: Literal["stdio"] = "stdio"
    command: str = "npx"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tool_timeout: int = Field(default=45, ge=1, le=180)


class ToolsOptions(Options):
    restrict_to_workspace: bool = True
    exec: ExecOptions = Field(default_factory=ExecOptions)
    mcp_servers: dict[str, McpOptions] = Field(default_factory=dict)
    disabled_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def browser_only(self):
        if set(self.mcp_servers) - {"browser"}:
            raise ValueError("v0.1只支持mcpServers.browser")
        return self


class SkillRouter(Options):
    enabled: bool = True
    top_k: int = Field(default=5, ge=0, le=20)
    local_min_score: float = 0.0


class SkillOptions(Options):
    router: SkillRouter = Field(default_factory=SkillRouter)
    inject_max: int = Field(default=2, ge=0, le=10)
    always_max: int = Field(default=5, ge=0, le=20)
    injection_mode: Literal["full_body", "summary"] = "full_body"


class Config(Options):
    agents: Agents = Field(default_factory=Agents)
    providers: Providers = Field(default_factory=Providers)
    context: ContextOptions = Field(default_factory=ContextOptions)
    memory: MemoryOptions = Field(default_factory=MemoryOptions)
    runtime: RuntimeOptions = Field(default_factory=RuntimeOptions)
    tools: ToolsOptions = Field(default_factory=ToolsOptions)
    skill_forge: SkillOptions = Field(default_factory=SkillOptions)

    def validate_credentials(self):
        if not self.providers.openai.api_key.get_secret_value():
            raise KakiError("缺少providers.openai.apiKey；请运行kaki-agent setup --import-config <已有配置>")
        if not self.providers.openai.api_base.startswith(
            ("https://", "http://127.0.0.1", "http://localhost")
        ):
            raise KakiError("apiBase必须使用HTTPS（本机测试端点除外）")


def kaki_home() -> Path:
    return Path(os.environ.get("KAKI_HOME") or "~/.kaki").expanduser().resolve()


def config_path(path: Path | None = None) -> Path:
    return (path or Path(os.environ.get("KAKI_CONFIG") or kaki_home() / "config.json")).expanduser()


def snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def normalize(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            snake(k): (v if snake(k) in {"env", "extra_headers"} else normalize(v)) for k, v in data.items()
        }
    if isinstance(data, list):
        return [normalize(x) for x in data]
    return data


def load_config(path: Path | None = None, *, model: str | None = None) -> Config:
    path = config_path(path)
    try:
        data = normalize(json.loads(path.read_text())) if path.exists() else {}
    except (ValueError, OSError) as exc:
        raise KakiError(f"配置无法读取：{path}；未覆盖原文件（{type(exc).__name__}）") from None
    if not isinstance(data, dict):
        raise KakiError("配置顶层必须是JSON对象")
    for key, value in os.environ.items():
        if not key.startswith("KAKI_") or "__" not in key or not value.strip():
            continue
        parts = key[5:].lower().split("__")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        try:
            node[parts[-1]] = json.loads(value)
        except ValueError:
            node[parts[-1]] = value
    if model:
        data.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
    try:
        return Config.model_validate(data)
    except ValueError as exc:
        # ValidationError input can contain a secret; only expose field locations and messages.
        errors = getattr(exc, "errors", lambda **_: [])(include_input=False)
        raise KakiError(
            "配置校验失败：" + "; ".join(".".join(map(str, e["loc"])) + ": " + e["msg"] for e in errors)
        ) from None


def save_config(config: Config, path: Path | None = None) -> Path:
    path = config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = config.model_dump(by_alias=True, mode="json")
    data["providers"]["openai"]["apiKey"] = config.providers.openai.api_key.get_secret_value()
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return path


def safe_error(error: BaseException | str) -> str:
    text = str(error) or type(error).__name__
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:1600]
