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
    # 所有配置节点共用别名与严格字段校验，拼错 key 不会被静默忽略。
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Defaults(Options):
    # 主模型与窗口/循环参数；token 与迭代预算分别限制内容大小和执行长度。
    provider: Literal["openai", "auto"] = "openai"
    model: str = "openai/gpt-5.6-sol"
    workspace: str | None = None
    # 输出 token 上限含推理输出，不是历史条数。
    max_tokens: int = Field(default=8192, ge=64)
    # 本地输入/输出总 cap，服务商不匹配时仍需处理 overflow。
    context_window_tokens: int = Field(default=65536, ge=1024)
    # 一次主循环的模型迭代上限；一次迭代可能提出多个工具。
    max_tool_iterations: int = Field(default=40, ge=1, le=200)
    temperature: float = 0.1
    empty_recovery_enabled: bool = True
    post_tool_empty_max_nudges: int = 1
    thinking_prefill_max_retries: int = 2
    empty_content_max_retries: int = 3

    @model_validator(mode="after")
    def budget_valid(self):
        # 输出预留不能吃掉整个窗口；校验失败应显式修配置而不是悄悄裁用户请求。
        if self.max_tokens >= self.context_window_tokens - 256:
            raise ValueError("maxTokens必须小于contextWindowTokens并留出输入空间")
        return self


class Agents(Options):
    # 保留兼容的 agents.defaults 层级，UI 与 headless 读取同一份模型设置。
    defaults: Defaults = Field(default_factory=Defaults)


class OpenAIOptions(Options):
    # Right Code 的 Responses 兼容连接参数；密钥只留本机，timeout 单位为秒。
    # 兼容服务入口，域名配置与模型名是两个独立选项。
    api_base: str = "https://api.site/v1"
    # SecretStr 防普通 repr 泄漏，不能因此把整个配置任意打印。
    api_key: SecretStr = SecretStr("")
    extra_headers: dict[str, str] | None = None
    models: list[str] = Field(default_factory=list)
    # 单次网络请求超时秒数，与整轮任务时长不同。
    timeout: float = Field(default=120, ge=5, le=600)


class Providers(Options):
    # Provider 配置容器，目前只接入 openai 兼容传输。
    openai: OpenAIOptions = Field(default_factory=OpenAIOptions)


class ContextOptions(Options):
    # 统一窗口引擎的规划、保护与归档参数，不是另一套历史数据库。
    engine: Literal["unified"] = "unified"
    fast_path_threshold: float = Field(default=0.6, ge=0, le=1)
    curator_model: str | None = None
    # 私有规划调用的总 deadline，失效后走本地回退。
    curator_timeout_seconds: float = Field(default=30, gt=0, le=300)
    # 初始用户组的约束保护；工具交换可按完整组分页。
    protect_first_n: int = Field(default=3, ge=0)
    # 必须仍在当前项目 state 内，不能任意写出工作区状态域。
    archive_dir: str = "context/archive"
    relevance_decay: float = Field(default=0.95, gt=0, le=1)
    relevance_reference_boost: float = Field(default=0.15, ge=0, le=1)


class MemoryOptions(Options):
    # 显式本地记忆的范围与召回上限；None 关闭记忆而非请求外部 Myna。
    backend: Literal["local"] | None = "local"
    user_id: str = "default"
    memory_top_k: int = Field(default=5, ge=0, le=30)


class CheckpointOptions(Options):
    # 影子快照策略与 state 内的独立 Git 目录。
    policy: Literal["interactive", "always", "never"] = "interactive"
    shadow_dir: str = "checkpoints/kaki-shadow.git"


class RuntimeOptions(Options):
    # 内核资源策略容器，与某个 Session 的运行状态分开。
    checkpoint: CheckpointOptions = Field(default_factory=CheckpointOptions)


class ExecOptions(Options):
    # 本机结构化命令的时限与 PATH 追加项，不开放任意 Shell。
    timeout: int = Field(default=60, ge=1, le=600)
    path_append: str = ""


class DevelopmentOptions(Options):
    # 开发能力开关与保护分支；允许 push/MR 注册不等于批准本次远端写。
    enabled: bool = False
    # 本地分支 fnmatch 模式；主干/release 和 detached 写入都需拒绝。
    protected_branches: list[str] = Field(default_factory=lambda: ["master", "main", "release/*"])
    # 仅注册/开放能力，远端 push 仍需本次具体批准。
    allow_push: bool = False
    # 只支持草稿创建，不产生自动合并权限。
    allow_mr_create: bool = False


class CommandOptions(Options):
    # 管理员预先声明的可信命令 profile，模型只能选择名称和受限参数。
    description: str
    # 固定可执行程序与参数前缀，不做 Shell 插值。
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    # 由本机可信配置分类，而非模型自己声明。
    capability: Literal[
        "workspace.check", "test.execute", "process.execute", "external.read", "external.write"
    ] = "process.execute"
    # 该 profile 的进程时限，单位秒。
    timeout: int = Field(default=120, ge=1, le=3600)
    # 未开启时拒绝任何附加参数，防模型改写固定工作流。
    allow_arguments: bool = False
    # 附加选项白名单；脚本本身还会校验参数语义。
    allowed_flags: list[str] = Field(default_factory=list)
    # 输入文件参数按工作区规范化并绑定内容 hash。
    path_flags: list[str] = Field(default_factory=list)
    # 工作流输出参数仅允许项目 state/workflows。
    write_flags: list[str] = Field(default_factory=list)
    # 可信实现依赖的固定 SHA256 pin；不要把它当业务文件自动版本更新机制。
    files: dict[str, str] = Field(default_factory=dict)
    # 可信子进程覆盖，键保留大小写；不向模型展示这些值。
    env: dict[str, str] = Field(default_factory=dict)


class DiscoveryOptions(Options):
    # 延迟加载工具 Schema 的开关与一次召回数，控制模型窗口开销。
    enabled: bool = True
    max_results: int = Field(default=5, ge=1, le=10)


class McpOptions(Options):
    # 专用浏览器 MCP 子进程及超时配置，启动信任不等于页面操作授权。
    type: Literal["stdio"] = "stdio"
    command: str = "npx"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tool_timeout: int = Field(default=45, ge=1, le=180)


class ToolsOptions(Options):
    # 工作区、命令、MCP 与能力发现的配置根，执行时仍有逐操作权限门。
    restrict_to_workspace: bool = True
    exec: ExecOptions = Field(default_factory=ExecOptions)
    mcp_servers: dict[str, McpOptions] = Field(default_factory=dict)
    disabled_tools: list[str] = Field(default_factory=list)
    development: DevelopmentOptions = Field(default_factory=DevelopmentOptions)
    commands: dict[str, CommandOptions] = Field(default_factory=dict)
    discovery: DiscoveryOptions = Field(default_factory=DiscoveryOptions)

    @model_validator(mode="after")
    def browser_only(self):
        # 当前只接受 browser MCP，拼错或添加未实现 backend 会立即报错。
        if set(self.mcp_servers) - {"browser"}:
            raise ValueError("v0.1只支持mcpServers.browser")
        return self


class SkillRouter(Options):
    # 本地候选召回参数；命中分数并不证明 Skill 适用或能执行。
    enabled: bool = True
    top_k: int = Field(default=5, ge=0, le=20)
    local_min_score: float = 0.0


class SkillOptions(Options):
    # 少量正文加元数据目录的注入预算，避免安装越多窗口越大。
    router: SkillRouter = Field(default_factory=SkillRouter)
    inject_max: int = Field(default=2, ge=0, le=10)
    always_max: int = Field(default=5, ge=0, le=20)
    injection_mode: Literal["full_body", "summary"] = "full_body"
    catalog_max: int = Field(default=40, ge=1, le=200)


class IntentOptions(Options):
    # 意图识别策略可独立选择，不能据此给工具授予权限。
    strategy: Literal["rules", "llm", "hybrid"] = "hybrid"


class RewriteOptions(Options):
    # 改写策略与检索目标；原文总在，派生词只辅助查找。
    strategy: Literal["local", "llm", "hybrid"] = "hybrid"
    targets: list[Literal["skills", "history", "memory", "code"]] = Field(
        default_factory=lambda: ["skills", "history", "memory", "code"]
    )
    max_queries_per_target: int = Field(default=2, ge=1, le=2)


class UnderstandingModelOptions(Options):
    # 额外联合理解调用的单次/时限/输入输出预算，主循环不共享这次重试额度。
    model: str | None = None
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_extra_calls_per_turn: Literal[1] = 1
    max_attempts: Literal[1] = 1
    input_tokens: int = Field(default=4096, ge=512, le=8192)
    output_tokens: int = Field(default=1024, ge=256, le=2048)


class AmbiguityOptions(Options):
    # 关键歧义如何交互；默认问清，也可把决定留给主模型。
    policy: Literal["ask", "model_decide"] = "ask"
    max_preflight_rounds: int = Field(default=2, ge=1, le=3)
    on_unresolved: Literal["interrupt"] = "interrupt"


class UnderstandingOptions(Options):
    # 把意图、改写、模型预算和澄清规则组合为一个独立模块。
    enabled: bool = True
    intent: IntentOptions = Field(default_factory=IntentOptions)
    rewrite: RewriteOptions = Field(default_factory=RewriteOptions)
    llm: UnderstandingModelOptions = Field(default_factory=UnderstandingModelOptions)
    ambiguity: AmbiguityOptions = Field(default_factory=AmbiguityOptions)


class ShellPermissions(Options):
    # 普通 Shell 入口只接受固定结构化模板，未知命令默认拒绝。
    mode: Literal["structured_only"] = "structured_only"
    unknown_command: Literal["deny"] = "deny"


class McpPermissions(Options):
    # 未知浏览器动作的审批策略，高风险固定黑名单另行硬拒绝。
    unknown_tool: Literal["ask", "deny"] = "ask"


class PermissionOptions(Options):
    # 配置是能力上限，真实当前用户任务与明确批准决定具体可执行范围。
    mode: Literal["task_scoped", "read_only", "ask"] = "task_scoped"
    default_decision: Literal["ask", "deny"] = "ask"
    grant_scopes: list[Literal["once", "turn"]] = Field(default_factory=lambda: ["once", "turn"])
    shell: ShellPermissions = Field(default_factory=ShellPermissions)
    mcp: McpPermissions = Field(default_factory=McpPermissions)


class Config(Options):
    # 本机唯一配置根；CLI、非空环境覆盖与 JSON 合并后再交给所有适配器。
    agents: Agents = Field(default_factory=Agents)
    providers: Providers = Field(default_factory=Providers)
    context: ContextOptions = Field(default_factory=ContextOptions)
    memory: MemoryOptions = Field(default_factory=MemoryOptions)
    runtime: RuntimeOptions = Field(default_factory=RuntimeOptions)
    tools: ToolsOptions = Field(default_factory=ToolsOptions)
    skill_forge: SkillOptions = Field(default_factory=SkillOptions)
    understanding: UnderstandingOptions = Field(default_factory=UnderstandingOptions)
    permissions: PermissionOptions = Field(default_factory=PermissionOptions)

    def validate_credentials(self):
        # 只做可启动前提检查，不在报错里回显 Key，也不声称接口实际认证成功。
        if not self.providers.openai.api_key.get_secret_value():
            raise KakiError("缺少providers.openai.apiKey；请运行kaki-agent setup --import-config <已有配置>")
        if not self.providers.openai.api_base.startswith(
            ("https://", "http://127.0.0.1", "http://localhost")
        ):
            raise KakiError("apiBase必须使用HTTPS（本机测试端点除外）")


def kaki_home() -> Path:
    # 项目状态根由 KAKI_HOME 或用户目录决定；它与业务 Git 工作树分离。
    return Path(os.environ.get("KAKI_HOME") or "~/.kaki").expanduser().resolve()


def config_path(path: Path | None = None) -> Path:
    # 显式配置优先，否则使用 Kaki 自己的私密配置，不从 Pico 路径猜测。
    return (path or Path(os.environ.get("KAKI_CONFIG") or kaki_home() / "config.json")).expanduser()


def snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def normalize(data: Any) -> Any:
    # 统一 camelCase/snake_case key；env、headers、files 这类映射的内部键必须保留原样。
    if isinstance(data, dict):
        return {
            snake(k): (v if snake(k) in {"env", "extra_headers", "files"} else normalize(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [normalize(x) for x in data]
    return data


def load_config(path: Path | None = None, *, model: str | None = None) -> Config:
    # 读取 JSON → 应用非空环境覆盖 → CLI 覆盖 → 严格校验；不依赖 Shell profile 保存 API Key。
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
    # SecretStr 仅在本机配置写入点显式取值；原子写并保持0600，不用于日志序列化。
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


def redact_text(text: str) -> str:
    # 对常见凭据样式做展示脱敏，不应被当成完整 DLP 或 OS 安全隔离。
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text


def safe_error(error: BaseException | str) -> str:
    # 公开错误边界：脱敏并限长，避免 SDK 异常把认证数据带入 UI/日志。
    return redact_text(str(error) or type(error).__name__)[:1600]
