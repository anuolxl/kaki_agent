export const terminalStates = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
export const labels = {
  idle: '就绪', preparing: '正在准备', running: '正在运行', model: '模型正在思考', tool: '正在执行工具',
  compacting: '正在整理上下文', waiting_user: '等待你的回答', finishing: '正在收尾，请稍候', stopping: '正在停止',
  completed: '本轮已完成，可以继续提问', failed: '本轮失败', cancelled: '本轮已停止', interrupted: '本轮未完成，可继续处理',
  unknown: '连接中断，任务状态待确认', closing: 'App 正在退出，保存执行状态…', closed: 'App 已退出', archived: '已归档，可恢复后继续', unresolved: '记录尚未结束，发送前将核对恢复',
};

export function statusFor(detail, connected, closed = false, closing = false) {
  if (closing) return { phase: 'closing', label: labels.closing, busy: true, canSend: false, canStop: false };
  if (closed) return { phase: 'closed', label: labels.closed, busy: false, canSend: false, canStop: false };
  if (!connected) return { phase: 'unknown', label: labels.unknown, busy: false, canSend: false, canStop: false };
  if (!detail) return { phase: 'idle', label: '选择工作区开始', busy: false, canSend: false, canStop: false };
  const live = detail.live || {};
  const own = live.session_id === detail.session.id;
  if (live.busy && own) return { phase: live.phase, label: labels[live.phase] || labels.running, busy: true,
    canSend: live.phase === 'waiting_user', canStop: live.phase !== 'stopping', question: live.question };
  if (detail.session.archived) return { phase: 'archived', label: labels.archived, busy: false, canSend: false, canStop: false };
  const last = detail.turns.at(-1);
  let phase = last?.status || 'idle';
  if (own && live.result && !live.busy) phase = live.phase;
  if (!terminalStates.has(phase) && phase !== 'idle') phase = 'unresolved';
  return { phase, label: labels[phase] || labels.idle, busy: false, canSend: !live.busy, canStop: false, otherRunning: !!live.busy };
}

export function duration(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
}

export function messageGroups(detail) {
  return (detail?.turns || []).map(turn => ({ ...turn, messages: detail.messages.filter(m => m.turn_id === turn.id) }));
}
