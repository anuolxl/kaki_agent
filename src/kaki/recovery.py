"""Reconcile interrupted work without pretending a side effect is exactly-once."""

from __future__ import annotations

import json


class Recovery:
    # 把日志中的中断转换成核验清单；恢复会话不代表重启旧的工具执行流程。
    def __init__(self, store):
        self.store = store

    async def reconcile(self, session: str) -> dict:
        # 先协调残留状态，再汇总确定结果/未知操作/快照；只有新观察才能解除未知。
        interrupted = await self.store.run("recover", session)
        turns = await self.store.run("turns", session)
        items = []
        for turn in turns[-5:]:
            if turn["status"] == "completed":
                continue
            runs = await self.store.run("tool_runs", turn["id"])
            snapshots = await self.store.run("snapshots", turn["id"])
            items.append(
                {
                    "turn_id": turn["id"],
                    "original_request": turn["text"],
                    "status": turn["status"],
                    "reason": turn["stop_reason"],
                    "known_results": [
                        {"name": r["name"], "state": r["state"]}
                        for r in runs
                        if r["state"] in ("succeeded", "failed")
                    ],
                    "unknown_or_cancelled": [
                        {"name": r["name"], "state": r["state"], "arguments": json.loads(r["arguments"])}
                        for r in runs
                        if r["state"] in ("unknown", "cancelled")
                    ],
                    "snapshots": [
                        {"id": s["id"], "phase": s["phase"], "state": s["state"], "commit": s["commit_id"]}
                        for s in snapshots
                    ],
                    "draft": json.loads(turn["result_json"] or "{}").get("draft"),
                }
            )
        return {
            "recovered_turns": len(interrupted),
            "items": items,
            "requires_new_observation": any(x["unknown_or_cancelled"] for x in items),
        }

    @staticmethod
    def context(plan: dict) -> str:
        # 给下一轮提供低权限事实线索；旧 DOM、旧批准和 UNKNOWN 结果都不能直接重放。
        if not plan.get("items"):
            return ""
        return (
            "# Recovery facts\n"
            + json.dumps(plan, ensure_ascii=False)
            + "\nUnknown副作用不允许自动重放。先读取当前文件/获取新浏览器状态；无法判断时询问用户。旧DOM引用已经失效。"
        )
