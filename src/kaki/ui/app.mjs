import {statusFor, labels, terminalStates, duration, messageGroups} from './state.mjs';

const $ = id => document.getElementById(id);
const state = {wid:null, sid:null, path:null, detail:null, sessions:[], projects:[], connected:false, closed:false, closing:false,
  archived:false, sending:false, refreshAgain:false, refreshTask:null, socket:null, retry:null};
const el = (tag, cls, text) => {const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined)node.textContent=text; return node;};

async function api(path, options={}) {
  const response=await fetch(path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json','X-Kaki-Client':'1'},
    body:options.body===undefined?undefined:JSON.stringify(options.body)});
  if(!response.ok){let message; try{message=(await response.json()).error;}catch{message=await response.text().catch(()=>null);} throw new Error(message||`请求未完成 (${response.status})`);}
  return response.json();
}
function showError(error){$('error-banner').textContent=error?.message||String(error);$('error-banner').hidden=false;}
function clearError(){$('error-banner').hidden=true;}
function route(suffix=''){return `/api/workspaces/${state.wid}/sessions/${state.sid}${suffix}`;}
function timeLabel(value){return value?new Date(value).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';}

async function loadProjects(){
  const info=await api('/api/state');state.projects=info.projects;$('model').textContent=info.model.replace(/^openai\//,'');
  renderProjects();return info;
}
function renderProjects(){
  $('projects').replaceChildren();
  for(const project of state.projects){
    const button=el('button',`project-row${project.path===state.path?' selected':''}`);button.title=project.path;
    button.append(el('span','folder','▱'),el('span','',project.name));button.onclick=()=>openWorkspace(project.path).catch(showError);
    $('projects').append(button);
  }
}
async function loadSessions(){
  if(!state.wid)return;
  const workspace=state.wid;const data=await api(`/api/workspaces/${workspace}/sessions?archived=${state.archived?1:0}`);
  if(workspace!==state.wid)return;
  state.sessions=data.sessions;renderSessions();
}
function renderSessions(){
  $('sessions').replaceChildren();
  for(const session of state.sessions){
    const live=state.detail?.live;const running=live?.busy&&live.session_id===session.id;
    const button=el('button',`session-row${session.id===state.sid?' selected':''}`);button.title=session.title;
    button.append(el('i',`dot ${running?'running':session.status||''}`));
    const copy=el('div','session-copy');copy.append(el('div','session-name',session.title),el('div','session-preview',running?(live.label||'正在运行'):(session.preview||'开始新的对话')));
    button.append(copy);button.onclick=()=>selectSession(session.id).catch(showError);$('sessions').append(button);
  }
  if(!state.sessions.length)$('sessions').append(el('div','empty-history',state.archived?'没有归档会话':'还没有会话'));
  $('archive-toggle').textContent=state.archived?'返回会话':'查看归档';
}
async function openWorkspace(path){
  clearError();const result=await api('/api/workspaces',{method:'POST',body:{path}});
  state.wid=result.workspace_id;state.path=result.path;state.archived=false;
  await loadProjects();await loadSessions();
  await selectSession(result.session_id||state.sessions[0]?.id);
  $('project-dialog').close();$('prompt').focus();
}
window.kakiOpenWorkspace = path => openWorkspace(path).catch(showError);
async function selectSession(identity){
  state.sid=identity;state.detail=null;$('prompt').value='';clearError();renderSessions();
  if(identity)await refresh();else{renderTranscript();updateStatus();}
}
async function refresh(){
  if(!state.wid||!state.sid||state.closed)return;
  state.refreshAgain=true;
  if(state.refreshTask)return state.refreshTask;
  state.refreshTask=(async()=>{
    while(state.refreshAgain&&!state.closed){
      state.refreshAgain=false;const workspace=state.wid, session=state.sid;
      const detail=await api(route());
      if(workspace!==state.wid||session!==state.sid)continue;
      state.detail=detail;$('session-title').textContent=detail.session.title;
      $('project-title').textContent=state.path?.split('/').at(-1)||'工作区';$('project-title').title=state.path||'';
      renderTranscript();updateStatus();await loadSessions();
    }
  })().finally(()=>{state.refreshTask=null;});
  return state.refreshTask;
}
function safelyLink(container){
  for(const link of container.querySelectorAll('a')){
    try{const url=new URL(link.getAttribute('href'),location.origin); if(!['https:','http:'].includes(url.protocol)){link.removeAttribute('href');continue;}link.target='_blank';link.rel='noopener noreferrer';}catch{link.removeAttribute('href');}
  }
}
function assistant(text, html=null){
  const outer=el('div','message assistant');const label=el('div','assistant-label');const icon=el('img');icon.src='/assets/icon.svg';icon.alt='';
  label.append(icon,el('span','','Kaki'));outer.append(label);
  const body=el('div',html===null?'live-draft':'');if(html===null)body.textContent=text;else{body.innerHTML=html;safelyLink(body);}outer.append(body);return outer;
}
function toolCard(tool){
  const card=el('details','tool-card');const symbol=tool.state==='running'?'◌':tool.state==='succeeded'?'✓':'!';
  card.append(el('summary','',`${symbol} ${tool.name} · ${{running:'执行中',succeeded:'成功',unknown:'结果未知',planned:'已排队',failed:'失败',cancelled:'已停止',denied:'已拒绝，未执行'}[tool.state]||'未成功'}`));
  card.append(el('pre','',JSON.stringify(tool.arguments||{},null,2)+(tool.preview?'\n\n'+tool.preview:'')));return card;
}
function renderTranscript(){
  const scroll=$('transcript');const keepBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<120;
  $('messages').replaceChildren();const groups=messageGroups(state.detail);
  $('welcome').hidden=groups.length>0; $('welcome-open').hidden=!!state.wid;
  for(const turn of groups){
    const section=el('section','turn');section.dataset.turnId=turn.id;
    for(const message of turn.messages){section.append(message.role==='user'?el('div','message user',message.text):assistant(message.text,message.html));}
    for(const tool of turn.tools||[])section.append(toolCard(tool));
    if(terminalStates.has(turn.status)){
      if(turn.status==='failed')section.append(el('div','turn-error',turn.error||turn.result?.text||'任务失败，未标记成功'));
      else if(['cancelled','interrupted'].includes(turn.status)&&!turn.messages.some(m=>m.role==='assistant'))section.append(el('div','muted',turn.result?.text||labels[turn.status]));
      const footer=el('div','turn-footer');footer.append(el('span',`end-badge ${turn.status}`,({completed:'✓ 已完成',failed:'! 失败',cancelled:'■ 已停止',interrupted:'! 未完成'})[turn.status]));
      if(turn.result?.usage)footer.append(el('span','',`${turn.result.usage.total_tokens||0} tokens · ${turn.result.usage.calls||0} 次模型调用`));
      if(turn.result?.verification?.commands?.length)footer.append(el('span','',`测试记录：${turn.result.verification.status}`));
      const stamp=el('time','',timeLabel(turn.ended));stamp.dateTime=turn.ended||'';footer.append(stamp);section.append(footer);
    }
    $('messages').append(section);
  }
  renderLive();if(keepBottom)scroll.scrollTop=scroll.scrollHeight;
}
function renderLive(){
  const scroll=$('transcript'), keepBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<160;
  const container=$('live-message');container.replaceChildren();const detail=state.detail, live=detail?.live;
  if(!detail||live?.session_id!==state.sid)return;
  if(live.pending_text)container.append(el('div','message user',live.pending_text));
  for(const text of Object.values(live.drafts||{}))container.append(assistant(text));
  if(live.busy){
    const recorded=new Set((detail.turns.find(t=>t.id===live.turn_id)?.tools||[]).map(t=>t.tool_call_id));
    for(const tool of live.tools||[])if(tool.state==='running'&&!recorded.has(tool.tool_call_id))container.append(toolCard(tool));
    const box=el('div','working-card');box.append(el('i','spin'),document.createTextNode(labels[live.phase]||'正在运行'));
    container.append(box);
  }else if(live.result?.status==='failed'&&!detail.turns.some(t=>t.id===live.turn_id))container.append(el('div','turn-error',live.result.error||live.result.text));
  if(keepBottom)scroll.scrollTop=scroll.scrollHeight;
}
function updateStatus(){
  const status=statusFor(state.detail,state.connected,state.closed,state.closing);
  $('status-strip').className=`status-strip ${status.phase}`;$('status-text').textContent=status.label;
  $('status-symbol').className=status.busy?'spin':'status-symbol';
  $('status-symbol').textContent=status.busy?'':status.phase==='completed'?'✓':status.phase==='failed'?'!':status.phase==='cancelled'?'■':'○';
  $('send').disabled=!status.canSend||state.sending;$('prompt').disabled=!state.wid||state.closed||!!state.detail?.session.archived;
  $('send').textContent=status.question?'↵':'↑';$('send').ariaLabel=status.question?'回答问题':'发送任务';
  $('stop-task').hidden=!status.canStop;$('stop-task').disabled=state.sending;
  $('session-menu').disabled=!state.sid;$('new-chat').disabled=!state.wid||!state.connected||state.closed;
  $('jump-running').hidden=!status.otherRunning;
  $('composer-hint').textContent=status.question?'回答当前问题 · Enter 发送':status.otherRunning?'同一仓库的另一会话正在运行':status.busy?'任务仍在执行；需要结束时点“停止本轮”':'Enter 发送 · Shift+Enter 换行';
  $('connection').className=state.connected&&!state.closed?'':'offline';
  $('connection').replaceChildren(el('i','dot'),document.createTextNode(state.closing?'正在退出':state.closed?'已退出':state.connected?'本机已连接':'正在重连'));
  const own=state.detail?.live?.session_id===state.sid;
  const warnings=own?(state.detail.live.warnings||[]):[];
  $('warning-banner').hidden=!warnings.length;$('warning-banner').textContent=warnings.length?'提示：'+[...new Set(warnings)].join('；'):'';
  const question=own?status.question:null;$('question').hidden=!question;
  if(question){$('question-text').textContent=({approval:'【操作授权】',clarification:'【任务澄清】',task:'【补充信息】'}[question.kind]||'')+question.question;$('question-options').replaceChildren();for(const option of question.options){const button=el('button','',option);button.onclick=()=>sendText(option).catch(showError);$('question-options').append(button);}}
  updateElapsed();
}
function updateElapsed(){
  const live=state.detail?.live;
  $('elapsed').textContent=live?.busy&&live.session_id===state.sid&&live.started_at?`已运行 ${duration(Date.now()/1000-live.started_at)}`:'';
}
async function sendText(text){
  const status=statusFor(state.detail,state.connected,state.closed,state.closing);if(!status.canSend||!text.trim()||state.sending)return;
  state.sending=true;clearError();updateStatus();
  try{
    if(status.question)await api(route('/answer'),{method:'POST',body:{question_id:status.question.id,text}});
    else await api(route('/send'),{method:'POST',body:{text,request_id:crypto.randomUUID()}});
    $('prompt').value='';await refresh();
  }finally{state.sending=false;updateStatus();}
}
async function stopTask(){
  if(state.sending)return;state.sending=true;updateStatus();
  try{await api(route('/stop'),{method:'POST',body:{}});await refresh();}finally{state.sending=false;updateStatus();}
}
async function newSession(){
  if(!state.wid){$('project-dialog').showModal();return;}
  const result=await api(`/api/workspaces/${state.wid}/sessions`,{method:'POST',body:{}});
  state.archived=false;await loadSessions();await selectSession(result.session_id);$('prompt').focus();
}
async function quitApp(){
  if(state.closed)return;await api('/api/shutdown',{method:'POST',body:{}});
  state.closed=true;state.closing=true;updateStatus();
}
function connect(){
  if(state.closed)return;
  const socket=new WebSocket(location.origin.replace('http:','ws:')+'/api/events');state.socket=socket;
  socket.onmessage=event=>{
    const data=JSON.parse(event.data);
    if(data.type==='connected'){state.connected=true;clearError();updateStatus();refresh().catch(showError);return;}
    if(data.type==='app.closing'||data.type==='app.closed'){state.closed=true;state.closing=true;updateStatus();return;}
    if(data.type==='projects.changed'){loadProjects().then(loadSessions).catch(showError);return;}
    if(data.type==='resync'){refresh().catch(showError);return;}
    if(data.type==='navigate'){
      const project=state.projects.find(p=>p.workspace_id===data.workspace_id);
      if(project)openWorkspace(project.path).then(()=>selectSession(data.session_id)).catch(showError);
      else loadProjects().then(()=>{const p=state.projects.find(p=>p.workspace_id===data.workspace_id);if(p)return openWorkspace(p.path);}).catch(showError);
      return;
    }
    if(data.type==='workspace.changed'&&data.workspace_id===state.wid){
      if(state.detail)state.detail.live=data.live;
      renderLive();updateStatus();renderSessions();if(data.reload)refresh().catch(showError);
    }
  };
  socket.onclose=()=>{state.connected=false;if(state.closed)state.closing=false;updateStatus();if(!state.closed)state.retry=setTimeout(connect,1500);};
  socket.onerror=()=>socket.close();
}

$('open-project').onclick=$('welcome-open').onclick=()=>{$('project-error').textContent='';$('project-dialog').showModal();$('project-path').focus();};
$('project-cancel').onclick=()=>$('project-dialog').close();
$('project-form').onsubmit=async event=>{event.preventDefault();try{await openWorkspace($('project-path').value);}catch(error){$('project-error').textContent=error.message;}};
$('new-chat').onclick=()=>newSession().catch(showError);
$('composer').onsubmit=event=>{event.preventDefault();sendText($('prompt').value).catch(showError);};
$('stop-task').onclick=()=>stopTask().catch(showError);
$('quit-app').onclick=()=>quitApp().catch(showError);
$('prompt').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendText(event.target.value).catch(showError);}};
$('jump-running').onclick=()=>selectSession(state.detail.live.session_id).catch(showError);
$('archive-toggle').onclick=async()=>{state.archived=!state.archived;await loadSessions();};
$('session-menu').onclick=()=>{if(!state.detail)return;$('rename-title').value=state.detail.session.title;$('archive-session').textContent=state.detail.session.archived?'恢复会话':'归档会话';$('session-dialog').showModal();};
$('session-cancel').onclick=()=>$('session-dialog').close();
$('rename-form').onsubmit=async event=>{event.preventDefault();try{await api(route(),{method:'PATCH',body:{title:$('rename-title').value}});$('session-dialog').close();await refresh();}catch(error){showError(error);}};
$('archive-session').onclick=async()=>{try{await api(route(),{method:'PATCH',body:{archived:!state.detail.session.archived}});$('session-dialog').close();await loadSessions();await refresh();}catch(error){showError(error);}};
for(const button of document.querySelectorAll('[data-prompt]'))button.onclick=()=>{$('prompt').value=button.dataset.prompt;$('prompt').focus();};
document.addEventListener('keydown',event=>{
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='n'){event.preventDefault();newSession().catch(showError);}
  if(event.ctrlKey&&!event.metaKey&&event.key.toLowerCase()==='c'){event.preventDefault();quitApp().catch(showError);}
});
setInterval(updateElapsed,1000);
try{
  const info=await loadProjects();connect();if(info.initial_error)showError(info.initial_error);
  if(info.initial){const project=state.projects.find(p=>p.workspace_id===info.initial.workspace_id);if(project){state.wid=info.initial.workspace_id;state.path=project.path;await loadSessions();await selectSession(info.initial.session_id);renderProjects();}}
  updateStatus();
}catch(error){showError(error);$('status-text').textContent='请从 Kaki App 启动器重新打开窗口';}
