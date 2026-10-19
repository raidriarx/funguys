/* Offline mission control. All server/user strings are rendered as text. */
'use strict';
const $ = id => document.getElementById(id);
const uuid = () => typeof crypto.randomUUID==='function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2,'0')).join('');
let state, socket, reconnectTimer, pollTimer, signedIn = false, retries = 0, busy = 0;
let contractsDirty = false, reviewedVersion = null;
let lastStateSequence = -1;
let hostInstance = null;
let activeScreen = 'project';

const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
function error(message) { $('error').textContent = message || ''; $('error').hidden = !message; }

function setScreen(name) {
  activeScreen = name;
  if ($('screen-project')) $('screen-project').hidden = (name !== 'project');
  if ($('screen-workers')) $('screen-workers').hidden = (name !== 'workers');
  if ($('nav-btn-project')) $('nav-btn-project').className = 'tab-btn' + (name === 'project' ? ' active' : '');
  if ($('nav-btn-workers')) $('nav-btn-workers').className = 'tab-btn' + (name === 'workers' ? ' active' : '');
}

function showLogin() {
  signedIn = false;
  $('login').hidden = false; $('app').hidden = true; $('sign-out').hidden = true;
  clearTimeout(reconnectTimer); clearInterval(pollTimer);
  if (socket) socket.close();
}
async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  const response = await fetch(path, {method, credentials:'same-origin', headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json().catch(() => ({detail:`HTTP ${response.status}`}));
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  }
  return data;
}
async function action(fn) {
  busy++; $('busy').hidden = false; error('');
  try { return await fn(); }
  catch (err) { error(err.message || String(err)); }
  finally { busy--; $('busy').hidden = busy === 0; }
}
function bind(id, fn, form = false) { const node = $(id); if (node) node.addEventListener(form ? 'submit' : 'click', evt => { evt.preventDefault(); action(fn); }); }
function button(text, fn) { const node = el('button',text); node.type='button'; node.addEventListener('click', () => action(fn)); return node; }
function card(title, detail) { const node=el('div',undefined,'card'); node.append(el('h3',title)); if (detail) node.append(el('p',detail,'muted')); return node; }
function fillSelect(id, options, fallback) {
  const node = $(id);
  if (!node) return;
  const previous = node.value;
  node.replaceChildren(...options.map(([value,label]) => { const o=el('option',label); o.value=value; return o; }));
  if (options.some(([value]) => value===previous)) node.value=previous;
  else if (fallback) node.value=fallback;
}
async function refresh() { const data=await api('/api/state'); render(data); }
function connect() {
  if (!signedIn) return;
  socket = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
  socket.onopen = () => { retries=0; $('connection').textContent='LIVE'; $('connection').className='good'; };
  socket.onmessage = evt => {
    try {
      const message=JSON.parse(evt.data);
      if (message.type==='state_sync') render(message.state);
      else if (message.type==='grill_phase') $('grill-progress').textContent=message.detail || '';
      else queueRefresh();
    } catch (err) { error('Could not read a live update: '+err.message); }
  };
  socket.onclose = () => {
    $('connection').textContent='RECONNECTING / POLLING'; $('connection').className='warn';
    if (signedIn) reconnectTimer=setTimeout(connect,Math.min(30000,1000*2**retries++));
  };
}
let refreshQueued;
function queueRefresh() { clearTimeout(refreshQueued); refreshQueued=setTimeout(() => action(refresh),250); }
async function start() {
  await refresh(); signedIn=true;
  $('login').hidden=true; $('app').hidden=false; $('sign-out').hidden=false;
  connect(); clearInterval(pollTimer);
  pollTimer=setInterval(() => { if (signedIn && !busy) action(refresh); },15000);
}
function render(data) {
  if (data.host_instance!==hostInstance) { hostInstance=data.host_instance; lastStateSequence=-1; }
  if (Number.isInteger(data.state_sequence) && data.state_sequence<=lastStateSequence) return;
  lastStateSequence=data.state_sequence ?? lastStateSequence;
  state=data;
  const project=data.project;
  $('project-label').textContent=`${project.name} · ${project.description || ''}\n${project.workspace_path}`;
  $('project-status').textContent=project.plan_approved?'APPROVED':'PAUSED / REVIEW REQUIRED';
  $('approve').disabled=project.plan_approved || !data.tasks.length;
  $('pause').disabled=!project.plan_approved;
  if (!contractsDirty) { $('contracts').value=project.contracts_markdown || ''; reviewedVersion=project.plan_version; }
  else if (reviewedVersion!==project.plan_version) error('The server plan changed while you were editing. Your draft is preserved. Refresh and explicitly discard the draft to review the new plan.');
  $('contract-dirty').hidden=!contractsDirty;

  const workerCount = data.agents.length;
  if ($('nav-worker-count')) $('nav-worker-count').textContent = String(workerCount);
  if ($('worker-summary-count')) $('worker-summary-count').textContent = String(workerCount);

  // Position invite worker section:
  // If no worker nodes exist, place the invite interface at the top of the project screen so it is seen first.
  // Otherwise, place it inside the dedicated workers screen.
  const inviteColumns = $('invite-columns');
  const zeroWorkersContainer = $('zero-workers-container');
  const workersContainer = $('workers-container');
  if (inviteColumns && zeroWorkersContainer && workersContainer) {
    if (workerCount === 0) {
      if (inviteColumns.parentElement !== zeroWorkersContainer) {
        zeroWorkersContainer.replaceChildren(inviteColumns);
      }
      zeroWorkersContainer.hidden = false;
    } else {
      zeroWorkersContainer.hidden = true;
      if (inviteColumns.parentElement !== workersContainer) {
        workersContainer.replaceChildren(inviteColumns);
      }
    }
  }

  const targets = [];
  const localHost = location.hostname || 'localhost';
  const port = location.port ? `:${location.port}` : '';
  targets.push([`${location.protocol}//${localHost}${port}`, `This Computer (Localhost - ${localHost})`]);
  if (data.lan_ip && data.lan_ip !== '127.0.0.1' && data.lan_ip !== localHost) {
    targets.push([`http://${data.lan_ip}:${location.port || 8420}`, `Same Wi-Fi / Local Network (${data.lan_ip})`]);
  }
  if (data.public_url) {
    targets.push([data.public_url, `Public Internet (Cloudflare Tunnel)`]);
  }
  fillSelect('invite-target', targets, targets[0][0]);
  const profiles=Object.entries(data.profiles).filter(([,p]) => p.provider!=='api').map(([key,p]) => [key,`${p.display_name} · Tier ${p.tier}`]);
  fillSelect('invite-profile',profiles,'codex-pro'); fillSelect('local-profile',profiles,'codex-pro');
  const locals=data.agents.filter(a => a.executor==='local').map(a => [a.id,`${a.machine_name} · ${a.display_name} · ${a.status}`]);
  fillSelect('grill-agent',[['','Auto: idle local architect'],...locals]);
  fillSelect('chat-agent',[['auto','Auto: idle local provider'],...locals]);
  renderAgents(data.agents); renderTasks(data.tasks); renderReports(data.qa_reports);
  const grill=data.grill;
  $('grill-progress').textContent=grill.current_phase_detail || 'Start an interview, or define tasks manually.';
  $('grill-question').textContent=grill.current_question?.question || '';
  $('grill-options').replaceChildren(...(grill.status==='grilling' ? (grill.current_question?.options || []).map(option => button(option,() => answer(option))) : []));
  $('compile').disabled=grill.status!=='grilling';
  $('chat').replaceChildren(...data.chat_history.slice(-10).map(entry => {
    const node=card(entry.agent_codename || 'Provider',entry.question); node.append(el('pre',entry.answer)); return node;
  }));
  $('events').replaceChildren(...data.events.slice(0,40).map(event => el('p',`${new Date(event.timestamp).toLocaleTimeString()} [${event.source}] ${event.message}`)));
}
function renderAgents(agents) {
  $('agents').replaceChildren(...agents.map(agent => {
    const node=card(`${agent.machine_name} · ${agent.status}`,`${agent.display_name} · ${agent.executor} · quota ${agent.used_pct.toFixed(1)}% (reported estimate)`);
    const row=el('div',undefined,'row');
    row.append(button('Rename',async () => { const codename=prompt('Machine code name',agent.machine_name); if (codename) { await api(`/api/agents/${agent.id}/rename`,{codename}); await refresh(); } }),
      button('Report quota / reset',async () => {
        const value=prompt('Current measured quota usage (%)',String(agent.used_pct)); if (value===null) return;
        const pct=Number(value); if (!Number.isFinite(pct) || pct<0 || pct>100) throw new Error('Quota must be between 0 and 100');
        const task=state.tasks.find(t => t.id===agent.current_task_id);
        await api(`/api/agents/${agent.id}/quota`,{used_pct:pct,turns_delta:0,reset_window:pct<agent.used_pct,assignment_id:task?.assignment_id}); await refresh();
      }),button('Remove worker',async () => { if (confirm('Remove this worker and requeue its active task?')) { await api(`/api/agents/${agent.id}`,undefined,'DELETE'); await refresh(); } }));
    node.append(row); return node;
  }));
  if (!agents.length) $('agents').append(el('p','No workers connected. Create an invitation or register a local executor.','muted'));
}
function download(name,data) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const link=el('a'); link.href=url; link.download=name; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}
function renderTasks(tasks) {
  // Spotlight for what is currently being worked on:
  const spotlight = $('active-spotlight');
  if (spotlight) {
    const activeTasks = tasks.filter(t => ['in_progress','in_qa','blocked_environment','blocked_circuit_breaker'].includes(t.status));
    if (activeTasks.length > 0) {
      spotlight.replaceChildren(...activeTasks.map(task => {
        const isBlocked = task.status.startsWith('blocked_');
        const isQa = task.status === 'in_qa';
        const cardClass = isBlocked ? 'spotlight-card blocked' : (isQa ? 'spotlight-card qa' : 'spotlight-card');
        const tagText = isBlocked ? '⚠️ BLOCKED — NEEDS ATTENTION' : (isQa ? '🔍 IN QA VERIFICATION' : '⚡ CURRENTLY WORKING ON');
        const tagClass = isBlocked ? 'warn' : 'good';
        
        const node = el('div', undefined, cardClass);
        node.append(el('span', tagText, `spotlight-tag ${tagClass}`));
        node.append(el('h3', `${task.id} · ${task.title}`));
        
        const agent = state.agents.find(a => a.id === task.assigned_agent_id);
        const ownerName = task.assigned_human ? `Human teammate: ${task.assigned_human}` : (agent ? `${agent.machine_name} (${agent.display_name})` : 'Awaiting worker assignment');
        node.append(el('p', `Status: ${task.status.toUpperCase()} · Assigned to: ${ownerName} · Phase ${task.phase} · QA retries: ${task.retry_count}/${task.max_retries}`));
        node.append(el('p', task.description));
        node.append(el('p', `Target files: ${task.target_files.join(', ')}`, 'muted'));
        
        if (task.blocked_reason) node.append(el('pre', task.blocked_reason, 'warn'));
        if (task.last_error) {
          const details = el('details');
          details.append(el('summary', 'Last failure details'), el('pre', task.last_error));
          node.append(details);
        }
        
        const row = el('div', undefined, 'row');
        if (['blocked_environment','blocked_circuit_breaker'].includes(task.status)) {
          row.append(button('Reset after review', async () => { await api(`/api/tasks/${task.id}/reset-circuit-breaker`,{}); await refresh(); }));
        }
        if (task.status === 'blocked_environment' && task.candidate_revision) {
          row.append(button('Recheck retained code in QA', async () => { await api(`/api/tasks/${task.id}/retry-qa`,{}); await refresh(); }));
        }
        if (!['completed','in_qa'].includes(task.status) && state.project.plan_approved) {
          row.append(button(task.assigned_human ? 'Prepare human retry' : 'Claim as human', async () => {
            const human_name = prompt('Human teammate name', task.assigned_human || 'Host');
            if (!human_name) return;
            await api(`/api/tasks/${task.id}/claim-human`, {human_name});
            await refresh();
          }));
        }
        if (task.assigned_human && task.status !== 'in_qa') {
          row.append(button('Release to workers', async () => { await api(`/api/tasks/${task.id}/release-human`, {}); await refresh(); }));
        }
        if (row.children.length > 0) node.append(row);
        return node;
      }));
      spotlight.hidden = false;
    } else {
      spotlight.replaceChildren();
      spotlight.hidden = true;
    }
  }

  // Full Task List
  $('tasks').replaceChildren(...tasks.map(task => {
    const agent=state.agents.find(a => a.id===task.assigned_agent_id);
    const node=card(`${task.id} · ${task.title}`,`Phase ${task.phase} · Tier ${task.complexity_tier} · ${task.status} · QA retries ${task.retry_count}/${task.max_retries}`);
    node.append(el('p',task.description),el('p',`Files: ${task.target_files.join(', ')} · Depends on: ${task.dependencies.join(', ') || 'none'}`,'muted'));
    if (agent || task.assigned_human) node.append(el('p',`Owner: ${task.assigned_human || agent.machine_name}`));
    if (task.blocked_reason) node.append(el('pre',task.blocked_reason,'warn'));
    if (task.last_error) { const details=el('details'); details.append(el('summary','Last failure'),el('pre',task.last_error)); node.append(details); }
    if (task.handoff_digest) { const details=el('details'); details.append(el('summary','Handoff / interface changes'),el('pre',task.handoff_digest)); node.append(details); }
    if (task.architect_review) node.append(el('p',task.architect_review,'muted'));
    const row=el('div',undefined,'row');
    if (!['completed','in_qa'].includes(task.status) && state.project.plan_approved) row.append(button(task.assigned_human?'Prepare human retry':'Claim as human',async () => {
      const human_name=prompt('Human teammate name',task.assigned_human || 'Host'); if (!human_name) return;
      await api(`/api/tasks/${task.id}/claim-human`,{human_name}); await refresh();
    }));
    if (task.assigned_human && task.status!=='in_qa') row.append(button('Release to workers',async () => { await api(`/api/tasks/${task.id}/release-human`,{}); await refresh(); }));
    if (task.assigned_human && task.status==='in_progress') {
      row.append(button('Download assignment JSON',async () => { const snap=await api(`/api/workspace/snapshot?task_id=${encodeURIComponent(task.id)}&assignment_id=${task.assignment_id}`); download(`${task.id}.json`,{...snap,deleted_files:[]}); }));
      const input=el('input'); input.type='file'; input.accept='.json,application/json'; input.hidden=true;
      input.addEventListener('change',() => action(async () => {
        if (!input.files.length) return;
        const changes=JSON.parse(await input.files[0].text());
        await api(`/api/tasks/${task.id}/submit`,{files:changes.files || {},binary_files:changes.binary_files || {},deleted_files:changes.deleted_files || [],agent_id:null,assignment_id:task.assignment_id,project_id:task.project_id,plan_version:task.plan_version,base_revision:task.base_revision,submission_id:uuid(),commit_message:'Human task implementation'}); await refresh();
      }));
      row.append(button('Submit edited JSON',async () => input.click()),input);
      node.append(el('p','Edit the downloaded files map, then submit it. Use deleted_files for individual removals. QA verifies this assignment before merging.','muted'));
    }
    if (['blocked_environment','blocked_circuit_breaker'].includes(task.status)) row.append(button('Reset after review',async () => { await api(`/api/tasks/${task.id}/reset-circuit-breaker`,{}); await refresh(); }));
    if (task.status==='blocked_environment' && task.candidate_revision) row.append(button('Recheck retained code in QA',async () => { await api(`/api/tasks/${task.id}/retry-qa`,{}); await refresh(); }));
    node.append(row); return node;
  }));
  if (!tasks.length) $('tasks').append(el('p','No tasks yet. Compile an interview plan or add tasks manually.','muted'));
}
function renderReports(reports) {
  $('qa').replaceChildren(...reports.map(report => {
    const node=card(`${report.task_id} · attempt ${report.attempt} · ${report.overall_passed?'VERIFIED':report.environment_blocked?'ENVIRONMENT BLOCKED':'FAILED'}`);
    node.append(el('p',Object.entries(report.checks || {}).map(([name,result]) => `${name}: ${result}`).join(' · ')));
    const details=el('details'); details.append(el('summary','Check logs'),el('pre',report.logs)); node.append(details);
    if (report.screenshot_path) { const link=el('a','Open screenshot'); link.href=report.screenshot_path; link.target='_blank'; link.rel='noreferrer'; node.append(link); }
    return node;
  }));
}
async function answer(value) { await api('/api/grill/answer',{answer:value}); $('grill-answer').value=''; await refresh(); }
bind('login-form',async () => { await api('/api/session',{token:$('login-token').value}); $('login-token').value=''; await start(); },true);
bind('sign-out',async () => { await api('/api/session',undefined,'DELETE'); showLogin(); });
bind('restart-host',async () => {
  if (!confirm('Restart the Host server process to apply new code and refresh tunnels?')) return;
  try { await api('/api/host/restart',{}); } catch (_) {}
  $('connection').textContent = 'RESTARTING...';
  $('connection').className = 'warn';
  setTimeout(() => location.reload(), 3000);
});
bind('nav-btn-project', () => setScreen('project'));
bind('nav-btn-workers', () => setScreen('workers'));
bind('btn-back-to-project', () => setScreen('project'));
if ($('project-workers-badge')) $('project-workers-badge').addEventListener('click', () => setScreen('workers'));

$('contracts').addEventListener('input',() => { contractsDirty=true; $('contract-dirty').hidden=false; });
bind('refresh',async () => { if (contractsDirty && !confirm('Discard unsaved contract edits and reload the server plan?')) return; contractsDirty=false; await refresh(); });
bind('approve',async () => {
  if (state.project.plan_version!==reviewedVersion) throw new Error('Plan version changed; reload and review before approval');
  await api('/api/project/approve',{contracts_markdown:$('contracts').value,plan_version:reviewedVersion}); contractsDirty=false; error(''); await refresh();
});
bind('pause',async () => { await api('/api/project/pause',{}); await refresh(); });
bind('project-form',async () => { await api('/api/project/init',{name:$('project-name').value,description:$('project-description').value,workspace_path:$('project-path').value || null}); contractsDirty=false; await refresh(); },true);
bind('local-form',async () => {
  const profile=$('local-profile').value;
  let worker_id=localStorage.getItem(`funguys-local-${profile}`);
  if (!worker_id) { worker_id=uuid(); localStorage.setItem(`funguys-local-${profile}`,worker_id); }
  const existing=state.agents.find(a => a.worker_id===worker_id);
  if (existing) throw new Error('This local executor is already registered. Remove it before changing its profile.');
  await api('/api/agents/register',{owner_name:$('local-name').value,machine_name:'host-'+profile,profile_key:profile,worker_id,executor:'local'}); await refresh();
},true);
bind('invite-form',async () => {
  const chosenBase = $('invite-target').value;
  if (!chosenBase && !state.public_url && ['127.0.0.1','localhost','::1'].includes(state.listen_host)) throw new Error('The host is bound to loopback. Restart with --tunnel or --host 0.0.0.0 before inviting a remote worker.');
  const invite=await api('/api/invites',{});
  const base = chosenBase || state.public_url || location.origin;
  const query=new URLSearchParams({token:invite.token,name:$('invite-name').value,codename:$('invite-code').value,profile:$('invite-profile').value});
  $('win-command').value=`irm "${base}/join.ps1?${query}" | iex`;
  $('bash-command').value=`curl --fail -sSL "${base}/join.sh?${query}" | bash`;
  $('invite-expiry').textContent='One machine · expires '+new Date(invite.expires_at).toLocaleString(); $('invite-result').hidden=false;
},true);
document.querySelectorAll('[data-copy]').forEach(node => node.addEventListener('click',() => action(async () => {
  const source=$(node.dataset.copy);
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(source.value);
  else { source.focus(); source.select(); if (!document.execCommand('copy')) throw new Error('Select the command and copy it manually.'); }
  const label=node.textContent; node.textContent='Copied'; setTimeout(() => node.textContent=label,1500);
})));
bind('grill-form',async () => { await api('/api/grill/start',{project_name:$('grill-name').value,project_idea:$('grill-idea').value,lead_architect_agent_id:$('grill-agent').value || null,workspace_path:$('grill-path').value || null,continue_existing:$('grill-continue').checked}); await refresh(); },true);
bind('answer-form',() => answer($('grill-answer').value),true);
bind('compile',async () => { await api('/api/grill/compile',{}); contractsDirty=false; await refresh(); });
const csv=value => value.split(',').map(x => x.trim()).filter(Boolean);
bind('task-form',async () => { await api('/api/tasks',{id:$('task-id').value,title:$('task-title').value,description:$('task-description').value,target_files:csv($('task-files').value),dependencies:csv($('task-deps').value),complexity_tier:Number($('task-tier').value),verification_commands:$('task-verify').value.split('\n').map(x => x.trim()).filter(Boolean),require_tests:$('task-tests').checked,require_browser:$('task-browser').checked}); await refresh(); },true);
bind('clear-tasks',async () => { if (confirm('Clear the paused task plan? Active work must be paused first.')) { await api('/api/tasks',undefined,'DELETE'); contractsDirty=false; await refresh(); } });
bind('chat-form',async () => { await api('/api/chat/ask',{question:$('chat-question').value,preferred_agent_id:$('chat-agent').value}); $('chat-question').value=''; await refresh(); },true);
bind('graph-form',async () => { const result=await api(`/api/graphify/${$('graph-mode').value}`,{query:$('graph-query').value,target:$('graph-target').value}); $('graph-result').textContent=result.result; },true);
bind('docs-form',async () => { const result=await api('/api/tools/fetch-docs',{url:$('docs-url').value}); $('docs-result').textContent=result.markdown; },true);
bind('search-form',async () => {
  const result=await api('/api/tools/web-search',{query:$('search-query').value});
  $('search-result').replaceChildren(...result.results.map(item => { const node=card(item.title,item.snippet); const link=el('a',item.url); link.href=item.url; link.target='_blank'; link.rel='noreferrer'; node.append(link); return node; }));
},true);
bind('simulate',async () => { const result=await api('/api/simulate/hackathon',{}); $('simulation').hidden=false; $('simulation').textContent=result.note+'\n'+result.tasks.map(t => `${t.id}: ${t.status}${t.blocked_reason?'\n'+t.blocked_reason:''}`).join('\n'); });
start().catch(err => { showLogin(); if (!/Sign in/.test(err.message)) error(err.message); });
