// 终态集合对应一轮 Turn；会话仍可继续下一轮，且测试通过另有独立证据。
export const terminalStates = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
export const labels = {
  idle: '就绪', preparing: '正在准备', running: '正在运行', model: '模型正在思考', tool: '正在执行工具',
  compacting: '正在整理上下文', waiting_user: '等待你的回答', finishing: '正在收尾，请稍候', stopping: '正在停止',
  completed: '本轮已完成，可以继续提问', failed: '本轮失败', cancelled: '本轮已停止', interrupted: '本轮未完成，可继续处理',
  unknown: '连接中断，任务状态待确认', closing: 'App 正在退出，保存执行状态…', closed: 'App 已退出', archived: '已归档，可恢复后继续', unresolved: '记录尚未结束，发送前将核对恢复',
};

// 状态投影的优先级：退出/断线 → 活跃任务 → 归档 → 持久终态；未知状态不能放开发送。
export function statusFor(detail, connected, closed = false, closing = false) {
  if (closing) return { phase: 'closing', label: labels.closing, busy: true, canSend: false, canStop: false };
  if (closed) return { phase: 'closed', label: labels.closed, busy: false, canSend: false, canStop: false };
  if (!connected) return { phase: 'unknown', label: labels.unknown, busy: false, canSend: false, canStop: false };
  if (!detail) return { phase: 'idle', label: '选择工作区开始', busy: false, canSend: false, canStop: false };
  const live = detail.live || {};
  // 当前浏览会话可能与执行会话不同，不能让一个页面误答另一个 Turn 的问题。
  const own = live.session_id === detail.session.id;
  // 模型正文已提交但 Task 仍 busy 时继续显示执行/收尾，不提前显示完成。
  if (live.busy && own) return { phase: live.phase, label: labels[live.phase] || labels.running, busy: true,
    canSend: live.phase === 'waiting_user', canStop: live.phase !== 'stopping', question: live.question };
  if (detail.session.archived) return { phase: 'archived', label: labels.archived, busy: false, canSend: false, canStop: false };
  const last = detail.turns.at(-1);
  let phase = last?.status || 'idle';
  if (own && live.result && !live.busy) phase = live.phase;
  if (!terminalStates.has(phase) && phase !== 'idle') phase = 'unresolved';
  return { phase, label: labels[phase] || labels.idle, busy: false, canSend: !live.busy, canStop: false, otherRunning: !!live.busy };
}

// 经过时间用于 UI，不参与任务状态判断。
export function duration(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}

// 稳定 turn_id 把消息归到任务轮，不以气泡数量推导轮次。
export function messageGroups(detail) {
  return (detail?.turns || []).map(turn => ({ ...turn, messages: detail.messages.filter(m => m.turn_id === turn.id) }));
}
