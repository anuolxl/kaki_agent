"""Domain values shared across UI, execution, context and storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

TurnStatus = Literal["queued", "running", "waiting_user", "completed", "interrupted", "cancelled", "failed"]
TERMINAL = {"completed", "interrupted", "cancelled", "failed"}


def now() -> str:
    # 所有持久事实统一使用 UTC 时间；界面显示再转换本地时区。
    return datetime.now(UTC).isoformat()


def uid(prefix: str = "") -> str:
    # 仅生成本地稳定标识，不把 request_id、模型 call_id 和执行 run_id 混为一谈。
    return prefix + uuid.uuid4().hex


@dataclass
class Message:
    # 协议角色；assistant/tool 是消息类型，不表示多启动了一个 Agent。
    # 会话中的一条领域消息；原文与原生协议项保留，存储 ID、窗口投影和可见正文分别处理。
    role: str
    # 原始可见正文；窗口裁剪只改变副本。
    content: str = ""
    # Store 赋的稳定消息 ID，0 表示尚未持久化；不是模型的 call_id。
    id: int = 0
    # 所属会话，决定历史/归档/恢复的作用域。
    session_id: str = ""
    # 接收这条消息的任务轮次，同一轮可以包含多次模型和工具交换。
    turn_id: str = ""
    # assistant 宣告的完整调用列表；必须与后续 tool 结果闭合。
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # tool 消息对应的上游调用 ID，用于协议配对。
    tool_call_id: str | None = None
    # Responses 原生项含 opaque reasoning；保留顺序，不能当普通正文重造。
    native_items: list[dict[str, Any]] = field(default_factory=list)
    # 受控图片等多模态引用；真正请求模型时才解析。
    parts: list[dict[str, Any]] = field(default_factory=list)
    # 完整观察或附件的引用，不能用可见截断文本替代它们。
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # 持久化事实的 UTC 时间，不是 UI 的本地格式化时间。
    created_at: str = field(default_factory=now)
    # 派生的原生项预算，不写 canonical payload；旧消息由调用日志关联恢复。
    native_token_reserve: int | None = None

    def payload(self) -> dict[str, Any]:
        # canonical JSON 只含事实内容；数据库 ID/时间在列中，token 预留是可重建派生值。
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "native_items": self.native_items,
            "parts": self.parts,
            "artifacts": self.artifacts,
        }


@dataclass(frozen=True)
class ToolCall:
    # 模型给出的 call_id；工具执行日志另有内部 run_id。
    # 模型提出的调用声明；不是已经执行的事实，也不携带可信权限。
    id: str
    # 必须匹配已注册的工具名，不能从字符串动态加载实现。
    name: str
    # 模型参数仍需 Schema、规范路径与权限预检。
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    # 真实观察正文，不是助手关于成功的判断。
    # 宿主执行后的真实观察；failed、denied、uncertain 分别影响日志和恢复。
    text: str
    # 工具失败标志；与整个 Turn 是否结束是两个层级。
    failed: bool = False
    # 输出超长时仍保留的完整内容引用。
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # 归一化的图片等输出片段。
    parts: list[dict[str, Any]] = field(default_factory=list)
    # 实际子进程退出码；未启动/非进程工具可为 None。
    exit_code: int | None = None
    # 实际改动的文件集合，便于快照和结果审计。
    touched_paths: list[str] = field(default_factory=list)
    # 是否进入工具实现；拒绝/预检失败必须为 False。
    executed: bool = True
    # 权限拒绝，不应被描述为已运行失败或未知副作用。
    denied: bool = False
    # 关联本次实际审批，不能复用另一 Turn 的批准。
    decision_id: str | None = None
    # 副作用结果未知，Store 记 UNKNOWN 并禁止盲目重放。
    uncertain: bool = False
    # 成功切分支后使旧规则/检索提示失效，同批余项暂不执行。
    context_refresh: bool = False


@dataclass
class ModelResponse:
    # 完整终事件中的可见正文，区别于尚未提交的流式 draft。
    # 一次完整模型响应；流式 delta 与网络尝试日志都不能代替这个提交对象。
    text: str = ""
    # 完整上游响应解析出的调用，提交意图后才能执行。
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 保持上游输出顺序，支持后续 reasoning/tool round-trip。
    native_items: list[dict[str, Any]] = field(default_factory=list)
    # 接口实际报告用量；缺失不等于调用成本为零。
    usage: dict[str, int] = field(default_factory=dict)
    # completed 才是完整输出，截断/失败不执行半个工具请求。
    finish_reason: str = "completed"
    # 实际响应模型标识，用于诊断而非自行切换配置。
    model: str = ""
    # 本机本次网络尝试 ID，对应 model_calls 主键。
    call_id: str = ""
    # 上游 response ID，不能与工具 call_id 或本地调用 ID 混用。
    provider_id: str = ""


@dataclass(frozen=True)
class Event:
    # 领域事件名；UI 映射与 SQLite 持久事件不是一一等同。
    # 同进程领域事件值；其负载供 UI 投影，关键持久事件另外写入 Store。
    type: str
    # 空值可表示运行时准备事件，有值才关联具体任务轮。
    turn_id: str = ""
    # 事件负载应为可公开、可序列化投影，不放 Future 或模型密钥。
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    # 本次被接受的任务 ID，不随工具循环次数增加。
    # 单轮收尾结果；会话包含多轮，测试证据与轮次完成状态独立判断。
    turn_id: str
    # 轮次终态；completed 只表示本轮有最终回答，不证明所有测试通过。
    status: TurnStatus
    # 最终回答或明确的失败/中断说明。
    text: str
    # 结束原因，区分最终回答、预算、取消、交互缺失及模型错误。
    stop_reason: str
    # 从已提交 tool_runs 重建的观察数，模型超时也不能清零。
    tools: int = 0
    # 已提交失败/拒绝观察数，不用它代替具体日志。
    tool_failures: int = 0
    # 按不同测试命令聚合最近证据，保留所有历史命令。
    verification: dict[str, Any] = field(default_factory=lambda: {"status": "not_run", "commands": []})
    # 本轮所有已知模型尝试的汇总，包括理解/Curator/主模型。
    usage: dict[str, Any] = field(default_factory=dict)
    # 首次获准写入/测试前的快照记录 ID，不等于 Git commit。
    before_snapshot: str | None = None
    # 收尾快照记录 ID；缺失/失败时不可声称安全自动恢复。
    after_snapshot: str | None = None
    # 脱敏后的公开错误，不包含原始 SDK 认证负载。
    error: str | None = None


class KakiError(Exception):
    # 可以安全展示的领域错误边界，仍需 safe_error 做凭据样式脱敏。
    """An actionable public error, without credentials or raw transport dumps."""


class ContextOverflow(KakiError):
    # 固定输入或配对/预算约束无法满足时明确失败，不偷偷删除用户要求。
    pass


class InteractionRequired(KakiError):
    # 无交互入口遇到授权/澄清时返回这个中断原因，而不是挂起等待。
    def __init__(self, kind: str, question: str):
        self.category = kind + "_required"
        super().__init__(question)


class ProviderError(KakiError):
    # 保留错误类别与是否已出现部分输出，用于判断可否安全有限重试。
    def __init__(self, message: str, category: str = "provider", *, partial: bool = False):
        # partial 防止可见输出已开始后又把同一响应当全新调用重试。
        super().__init__(message)
        self.category = category
        self.partial = partial


@dataclass(frozen=True)
class TokenBudget:
    # 本地配置的窗口 cap（tokens），不是自动查询的服务商规格。
    # 单个模型窗口的分配账本；token 计数是估算，不能当向量检索或计费真值。
    context_length: int
    # 给模型输出及 reasoning 留出的空间。
    reserved_output: int
    # 当前规则、身份和检索注入占用。
    reserved_system: int
    # 工具 Schema 占用，延迟发现会使它变化。
    reserved_tools: int
    # 当前真实输入占用，不能靠删掉它假装预算通过。
    reserved_user: int
    # 估算误差的安全余量。
    margin: int

    @property
    def input_limit(self) -> int:
        # 输出与安全余量先扣除，得到完整 input（含规则和工具）的上限。
        return self.context_length - self.reserved_output - self.margin

    @property
    def available_history(self) -> int:
        # 固定开销过大时历史预算可为零；当前用户输入仍不可删。
        return max(0, self.input_limit - self.reserved_system - self.reserved_tools - self.reserved_user)


@dataclass
class ContextPlan:
    # 只可引用当前 Session 已存在的事实 ID。
    # Curator 或本地策略给出的候选选择；引擎会验证 ID、配对与最终预算。
    include_message_ids: list[int] = field(default_factory=list)
    # 同会话可校验归档，不是任意文件路径。
    include_archive_refs: list[str] = field(default_factory=list)
    # 显式排除项也不能拆半个工具组或丢初始用户约束。
    drop_message_ids: list[int] = field(default_factory=list)
    # 有损工作摘要，权限与可信度低于当前用户原文。
    working_state_injection: str = ""
    # 只允许引擎认识的派生记忆分区。
    memory_sections: list[str] = field(default_factory=list)
    # 规划诊断，不是可执行指令。
    notes: str = ""


@dataclass
class AssembledContext:
    # 最终发给 Provider 的投影；原始 Store 历史不随它裁剪。
    # 通过宿主预算门后的一次窗口投影及审计元数据，不替代原始 Session。
    messages: list[Message]
    # 该窗口的输入/输出预留约束。
    budget: TokenBudget
    # 本地估算值，仍可能与接口 usage 存在差异。
    estimated_tokens: int
    # 选中/分页/归档 ID、策略和回退路径，供诊断与 UI 展示。
    metadata: dict[str, Any] = field(default_factory=dict)
