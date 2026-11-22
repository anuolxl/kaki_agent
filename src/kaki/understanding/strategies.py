"""Conservative lexical fallback and evidence validation for optional model hints."""

from __future__ import annotations

import re

from .types import Intent, QueryPlan

POINTER = re.compile(r"这个|那个|它|上述|上面|刚才|继续|前者|后者|\b(?:it|that|this|continue)\b", re.I)
EDIT = re.compile(
    r"修复|修改|实现|重构|新增|创建|写入|删除|替换|\b(?:fix|edit|implement|refactor|create|write|delete)\b",
    re.I,
)
NO_WRITE = re.compile(
    r"不(?:要|能|得|允许)?(?:再)?(?:修改|改动|编辑|写入|删除)|只(?:回答|读|分析|介绍|解释)|read[ -]?only|do not (?:edit|modify|write)",
    re.I,
)
RUN_TEST = re.compile(
    r"运行.*测试|执行.*测试|跑.*测试|测试验证|验证.*测试|\b(?:run|execute)\b.*\btests?\b", re.I
)
PLAN_TEST = re.compile(r"测试(?:方案|计划|用例设计)|设计.*测试|\btest plan\b", re.I)
NO_TEST = re.compile(r"不(?:要|能|得)?(?:运行|执行|跑).*测试|do not run.*test", re.I)
IDENTIFIER = re.compile(
    r"[\w./-]+\.(?:py|go|js|ts|json|md|yaml|yml|toml|txt)|`([^`]+)`|\b[A-Za-z_][A-Za-z_0-9]*(?:[A-Z_][A-Za-z_0-9]*)\b"
)


def local_analysis(text, history, cfg):
    actions = []
    if re.search(r"介绍|解释|说明|什么|如何|\bexplain\b", text, re.I):
        actions.append("explain")
    if re.search(r"查找|定位|搜索|\b(?:find|locate|search)\b", text, re.I):
        actions.append("locate")
    if re.search(r"审查|评审|review", text, re.I):
        actions.append("review")
    if EDIT.search(text) and not NO_WRITE.search(text):
        actions.append("edit")
    if PLAN_TEST.search(text):
        actions.append("plan_tests")
    if RUN_TEST.search(text) and not PLAN_TEST.search(text) and not NO_TEST.search(text):
        actions.append("run_tests")
    coding = bool(re.search(r"代码|仓库|文件|函数|测试|bug|git|\.py\b|\.go\b|\bcode\b", text, re.I))
    domain = "coding" if coding else "general"
    standalone = text
    sources = []
    unresolved = []
    if POINTER.search(text):
        evidence = [m for m in history if m.role in ("user", "assistant") and m.content][-6:]
        if evidence:
            latest = evidence[-1]
            standalone = text + "\n前文检索线索（不代表已确认的对象）：" + latest.content[:1200]
            sources = [latest.id]
        if not any(IDENTIFIER.finditer(text)) and (not evidence or "edit" in actions):
            unresolved = ["请明确本轮操作的目标文件、函数或对象；可直接提供路径和要执行的动作。"]
    alias = []
    for word, translated in [
        ("超时", "timeout"),
        ("取消", "cancel"),
        ("会话", "session"),
        ("上下文", "context"),
        ("重试", "retry"),
    ]:
        if word in text:
            alias.append(translated)
    queries = {}
    for target in cfg.rewrite.targets:
        hints = standalone if standalone != text else " ".join(alias)
        queries[target] = list(dict.fromkeys([text, hints]))[: cfg.rewrite.max_queries_per_target]
        queries[target] = [q for q in queries[target] if q.strip()]
    return Intent(domain=domain, actions=actions), QueryPlan(
        standalone_query=standalone[:8000], queries=queries, source_ids=sources, unresolved=unresolved
    )


def validate_rewrite(plan, original, history, cfg):
    available = {m.id for m in history if m.id > 0}
    if set(plan.source_ids) - available:
        raise ValueError("invalid_source_ids")
    # Preserve exact code identifiers, numbers, case and negations in the standalone query.
    for match in IDENTIFIER.finditer(original):
        token = match.group(1) or match.group(0)
        if token not in plan.standalone_query:
            raise ValueError("identifier_lost")
    for number in re.findall(r"\d+(?:\.\d+)?(?:ms|s|秒|分钟)?", original):
        if number not in plan.standalone_query:
            raise ValueError("number_lost")
    if NO_WRITE.search(original) and not NO_WRITE.search(plan.standalone_query):
        raise ValueError("constraint_lost")
    if POINTER.search(original) and plan.standalone_query != original and not plan.source_ids:
        raise ValueError("unsourced_reference")
    clean = {}
    for target in cfg.rewrite.targets:
        hints = plan.queries.get(target, [])
        if len(hints) > 2 or any(not q.strip() or len(q) > 8000 for q in hints):
            raise ValueError("query_budget")
        clean[target] = list(dict.fromkeys([original, *hints]))[: cfg.rewrite.max_queries_per_target]
    return plan.model_copy(update={"queries": clean})
