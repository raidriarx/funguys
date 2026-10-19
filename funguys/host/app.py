"""Authenticated host API and lifecycle for the FunGuys workflow."""
from __future__ import annotations
import asyncio
import base64
import json
import secrets
import time
import itertools
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from funguys.config import SUBSCRIPTION_PROFILES
from funguys.db import StateStore
from funguys.files import bundle, source_files, validate_dag,runtime_directory
from funguys.host.orchestrator import FunGuysOrchestrator, ConflictError
from funguys.host.security import AccessControl
from funguys.host.tunnel import get_lan_ip
from funguys.models import ApprovePlanRequest, ClaimTaskHumanRequest, CreateProjectRequest, RegisterAgentRequest, ReportQuotaRequest, TaskItem, TaskStatus

class SubmitTaskRequest(BaseModel):
    agent_id: Optional[str] = None
    files: dict[str,str] = Field(default_factory=dict)
    binary_files: dict[str,str] = Field(default_factory=dict)
    deleted_files: list[str] = Field(default_factory=list,max_length=2000)
    assignment_id: str
    project_id: str
    plan_version: str
    base_revision: str
    submission_id: str = Field(min_length=8, max_length=128)
    commit_message: str = Field(default='Complete task', max_length=500)
    used_pct_after: Optional[float] = Field(default=None, ge=0, le=100)

class StartGrillRequest(BaseModel):
    project_name: str
    project_idea: str
    lead_architect_agent_id: Optional[str] = None
    workspace_path: Optional[str] = None
    continue_existing: bool = False

class CreateTaskRequest(BaseModel):
    model_config = {'extra':'forbid'}
    id: str
    title: str = Field(min_length=1,max_length=300)
    description: str = Field(min_length=1,max_length=20000)
    phase: int = Field(default=1,ge=1)
    complexity_tier: Literal[1,2] = 2
    dependencies: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    require_tests: bool = False
    require_browser: bool = False

class AnswerGrillRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20000)

class AskAgentRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    preferred_agent_id: Optional[str] = 'auto'

class GraphQueryRequest(BaseModel):
    query: str
    target: Optional[str] = None

class WebSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=5, ge=1, le=10)

class FetchDocsRequest(BaseModel):
    url: str

class QuotaRequest(ReportQuotaRequest):
    wip_files: dict[str,str] = Field(default_factory=dict)
    wip_binary_files: dict[str,str] = Field(default_factory=dict)
    deleted_files: list[str] = Field(default_factory=list)

class HeartbeatRequest(BaseModel):
    assignment_id: Optional[str] = None

class FailureRequest(BaseModel):
    assignment_id: str
    error: str = Field(max_length=10000)

class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)

class BodyLimit:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit, chunks, size = 80_000_000, [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > limit:
                return await JSONResponse({'detail':'Request exceeds 30 MB limit'}, status_code=413)(scope, receive, send)
            chunks.append(message)
            if not message.get('more_body'):
                break
        async def replay():
            if chunks:
                return chunks.pop(0)
            return await receive()
        await self.app(scope, replay, send)


def create_host_app(base_dir: Optional[Path] = None, *, admin_token=None, public_url=None, bind_host='127.0.0.1',tunnel_url_provider=None):
    from funguys.host.locking import HostLock
    root = (base_dir or Path.cwd()).resolve()
    host_lock = HostLock(runtime_directory(root,'.funguys')/'host.lock')
    host_lock.acquire()
    try:
        return _create_host_app(root,admin_token=admin_token,public_url=public_url,bind_host=bind_host,tunnel_url_provider=tunnel_url_provider,host_lock=host_lock)
    except BaseException:
        host_lock.release()
        raise


def _create_host_app(base_dir,*,admin_token,public_url,bind_host,tunnel_url_provider,host_lock):
    from funguys.host.architect import GrillArchitectEngine, GrillSession
    root = (base_dir or Path.cwd()).resolve()
    state_dir = runtime_directory(root,'.funguys')
    store = StateStore(state_dir / 'state.db')
    access = AccessControl(store, state_dir, admin_token)
    clients = set()
    async def broadcast(payload):
        async def deliver(ws):
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=2)
            except Exception:
                clients.discard(ws)
        await asyncio.gather(*(deliver(ws) for ws in list(clients)))
    orch = FunGuysOrchestrator(store, state_dir / 'workspaces', broadcast)
    grill = GrillArchitectEngine()
    grill.logger = orch.wlog
    session_file = state_dir / 'grill_session.json'
    if session_file.exists():
        try:
            grill.session = GrillSession.model_validate_json(session_file.read_text(encoding='utf-8'))
        except (ValueError,OSError):
            pass
    plan_lock, enroll_lock = asyncio.Lock(), asyncio.Lock()
    state_counter = itertools.count()
    host_instance = secrets.token_hex(12)

    async def project_mutation():
        if plan_lock.locked():
            raise ConflictError('Another project or planning operation is in progress')
        async with plan_lock:
            yield

    async def maintenance():
        while True:
            try:
                if tunnel_url_provider:
                    app.state.public_url = tunnel_url_provider()
                await orch.schedule_ready_tasks()
            except Exception as exc:
                orch.wlog.exception('maintenance_error', 'Scheduler loop failed', exc)
                await orch.emit_event('host', 'maintenance_error', str(exc))
            await asyncio.sleep(5)

    async def local_execution():
        while True:
            try:
                await orch.run_autonomous_execution_loop()
            except Exception as exc:
                orch.wlog.exception('local_execution_error', 'Local execution loop failed', exc)
                await orch.emit_event('host','local_execution_error',str(exc))
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        host_lock.acquire()
        tasks = [asyncio.create_task(maintenance()), asyncio.create_task(local_execution())]
        try:
            yield
        finally:
            try:
                for cancelled in orch._local_cancellations.values():
                    cancelled.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await asyncio.gather(*(asyncio.wait_for(ws.close(),timeout=2) for ws in list(clients)),return_exceptions=True)
            finally:
                host_lock.release()
    app = FastAPI(title='FunGuys Host', version='0.2.0', lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.store, app.state.orchestrator, app.state.grill_engine = store, orch, grill
    app.state.host_lock = host_lock
    app.state.access, app.state.admin_token, app.state.public_url = access, access.admin_token, public_url
    app.state.lan_ip = get_lan_ip()
    login_failures = {}

    @app.exception_handler(ConflictError)
    async def conflict_handler(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=409)
    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=400)
    @app.exception_handler(RuntimeError)
    async def runtime_handler(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=503)

    @app.middleware('http')
    async def authorize(request, call_next):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail':'Cross-origin requests are not allowed'}, status_code=403)
        token = request.headers.get('authorization','')
        token = token[7:] if token.startswith('Bearer ') else request.cookies.get('funguys_session','')
        request.state.identity = access.identify(token)
        request.state.token = token
        path = request.url.path
        if path.startswith('/api/') and path != '/api/session':
            identity = request.state.identity
            if not identity:
                return JSONResponse({'detail':'Sign in or supply a worker credential'}, status_code=401)
            if identity['role'] != 'admin':
                permitted = (path == '/api/agents/register' and identity['role'] in ('invite','worker')) or (identity['role'] == 'worker' and (path in ('/api/worker/assignment','/api/workspace/snapshot') or path.startswith('/api/tasks/') and path.endswith(('/submit','/execution-failed')) or path == f"/api/agents/{identity['agent_id']}/heartbeat" or path == f"/api/agents/{identity['agent_id']}/quota"))
                if not permitted:
                    return JSONResponse({'detail':'Administrator access required'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' if path.startswith(('/api/','/join.')) else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def state():
        project = store.get_project()
        return {'host_instance':host_instance,'state_sequence':next(state_counter),'project':project.model_dump(), 'tasks':[orch.public_task(t) for t in store.list_tasks()], 'agents':[orch.public_agent(a) for a in store.list_agents()], 'qa_reports':[r.model_dump() for r in store.list_qa_reports() if r.project_id==project.id], 'contract_alerts':[a.model_dump() for a in store.list_contract_alerts() if a.project_id==project.id], 'events':[e.model_dump() for e in store.list_events()], 'chat_history':[c for c in store.list_chat() if c.get('project_id')==project.id], 'graph':orch.graph.last_graph_data, 'graph_report_markdown':'', 'grill':grill.session.model_dump(), 'lan_ip':app.state.lan_ip, 'listen_host':bind_host, 'public_url':app.state.public_url, 'available_workspaces':[{'path':str(p), 'folder_name':p.name, 'project_name':p.name} for p in sorted((state_dir/'workspaces').iterdir()) if p.is_dir()], 'profiles':{key:{'provider':p.provider,'display_name':p.display_name,'tier':p.tier} for key,p in SUBSCRIPTION_PROFILES.items()}}

    async def sync():
        await broadcast({'type':'state_sync', 'state':state()})

    @app.post('/api/session')
    async def login(req:SessionRequest, request:Request):
        ip = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        failures = [t for t in login_failures.get(ip,[]) if now-t < 60]
        if len(failures) >= 5:
            raise HTTPException(429, 'Too many login attempts; retry in one minute')
        if not secrets.compare_digest(req.token, access.admin_token):
            if len(login_failures) > 1024:
                login_failures.clear()
            login_failures[ip] = [*failures,now]
            raise HTTPException(401, 'Invalid administrator token')
        login_failures.pop(ip,None)
        response = JSONResponse({'signed_in':True})
        response.set_cookie('funguys_session', access.admin_token, httponly=True, samesite='strict', secure=request.url.scheme=='https', max_age=43200)
        return response

    @app.delete('/api/session')
    async def logout():
        response = JSONResponse({'signed_in':False})
        response.delete_cookie('funguys_session')
        return response

    @app.post('/api/invites')
    async def create_invite():
        return access.invite()

    @app.post('/api/host/restart')
    async def restart_host():
        import sys, os, subprocess
        async def do_restart():
            await asyncio.sleep(0.5)
            # Spawn a new host process using the same python interpreter and arguments
            args = [sys.executable, *sys.argv]
            env = os.environ.copy()
            env['FUNGUYS_TRUSTED_EXECUTION'] = '1'
            subprocess.Popen(args, cwd=str(root), env=env)
            os._exit(0)
        asyncio.create_task(do_restart())
        return {'restarting': True}

    @app.get('/api/state')
    async def get_state():
        return state()

    @app.post('/api/agents/register')
    async def register(req:RegisterAgentRequest, request:Request):
        async with enroll_lock:
            identity = access.identify(request.state.token)
            if not identity:
                raise HTTPException(401,'Invite expired or revoked')
            if identity['role'] != 'admin' and req.executor != 'remote':
                raise HTTPException(403,'Only the administrator can register a host executor')
            existing_id = identity['agent_id'] if identity['role']=='worker' else None
            if existing_id:
                existing = store.get_agent(existing_id)
                if not existing or existing.worker_id != req.worker_id:
                    raise HTTPException(403,'Credential belongs to a different worker')
            agent = await orch.register_agent(**req.model_dump(), existing_agent_id=existing_id)
            result = orch.public_agent(agent)
            if identity['role'] == 'invite':
                result['worker_token'] = access.enroll(request.state.token, agent.id)
            elif identity['role'] == 'admin' and req.executor == 'remote':
                from funguys.host.security import digest
                worker_token = secrets.token_urlsafe(32)
                store.save_credential(digest(worker_token),'worker',agent.id)
                result['worker_token'] = worker_token
        await sync()
        return result

    @app.get('/api/worker/assignment')
    async def assignment(request:Request):
        identity = request.state.identity
        if identity['role'] != 'worker':
            raise HTTPException(403,'Worker credential required')
        await orch.heartbeat(identity['agent_id'])
        return {'assignment':orch.assigned_work(identity['agent_id'])}

    def worker_scope(request, agent_id):
        if request.state.identity['role'] != 'admin' and request.state.identity['agent_id'] != agent_id:
            raise HTTPException(403,'Credential belongs to a different worker')

    @app.post('/api/agents/{agent_id}/heartbeat')
    async def heartbeat(agent_id:str, req:HeartbeatRequest, request:Request):
        worker_scope(request,agent_id)
        return await orch.heartbeat(agent_id, req.assignment_id)

    @app.post('/api/agents/{agent_id}/quota')
    async def quota(agent_id:str, req:QuotaRequest, request:Request):
        worker_scope(request,agent_id)
        result = await orch.report_quota_and_check_handoff(agent_id, **req.model_dump())
        await sync()
        return result

    @app.delete('/api/agents/{agent_id}')
    async def delete_agent(agent_id:str):
        await orch.delete_agent(agent_id)
        await sync()
        return {'deleted':agent_id}

    @app.post('/api/agents/{agent_id}/rename')
    async def rename_agent(agent_id:str, req:dict[str,str]):
        async with orch._state_lock:
            agent = store.get_agent(agent_id)
            if not agent:
                raise HTTPException(404,'Agent not found')
            agent.machine_name = (req.get('codename') or agent.machine_name).strip()
            agent.owner_name = (req.get('owner_name') or agent.owner_name).strip()
            agent.display_name = f'{agent.owner_name}: {SUBSCRIPTION_PROFILES[agent.profile_key].display_name}'
            store.save_agent(agent)
        await sync()
        return orch.public_agent(agent)

    @app.get('/api/workspace/snapshot')
    async def snapshot(request:Request, task_id:Optional[str]=None, assignment_id:Optional[str]=None):
        if not task_id:
            if request.state.identity['role'] != 'admin':
                raise HTTPException(403,'A current assignment is required')
            return await asyncio.to_thread(bundle,orch.git.workspace_path)
        task = store.get_task(task_id)
        if not task or task.assignment_id != assignment_id or task.status != TaskStatus.IN_PROGRESS:
            raise ConflictError('Stale or inactive snapshot request')
        if task.assigned_agent_id:
            worker_scope(request,task.assigned_agent_id)
        elif request.state.identity['role'] != 'admin':
            raise HTTPException(403,'Human snapshot requires administrator access')
        return {**await asyncio.to_thread(bundle,Path(task.candidate_path)), 'base_revision':task.base_revision, 'project_id':task.project_id, 'plan_version':task.plan_version}

    @app.post('/api/project/init',dependencies=[Depends(project_mutation)])
    async def initialize(req:CreateProjectRequest):
        await orch.initialize_project(**req.model_dump())
        await sync()
        return state()

    @app.post('/api/project/approve',dependencies=[Depends(project_mutation)])
    async def approve(req:ApprovePlanRequest):
        await orch.approve_plan(**req.model_dump())
        await sync()
        return state()

    @app.post('/api/project/pause')
    async def pause():
        await orch.pause_project()
        await sync()
        return state()

    @app.post('/api/tasks',dependencies=[Depends(project_mutation)])
    async def create_task(req:CreateTaskRequest):
        task = await orch.add_task(TaskItem(**req.model_dump()))
        await sync()
        return orch.public_task(task)

    @app.delete('/api/tasks',dependencies=[Depends(project_mutation)])
    async def clear_tasks():
        await orch.clear_tasks()
        await sync()
        return state()

    @app.post('/api/tasks/{task_id}/submit')
    async def submit(task_id:str, req:SubmitTaskRequest, request:Request):
        human = req.agent_id is None
        if human and request.state.identity['role'] != 'admin':
            raise HTTPException(403,'Human submission requires administrator access')
        if not human:
            worker_scope(request,req.agent_id)
        values = req.model_dump()
        result = await orch.submit_task_work(task_id, human=human, **values)
        await sync()
        return result

    @app.post('/api/tasks/{task_id}/execution-failed')
    async def execution_failed(task_id:str, req:FailureRequest, request:Request):
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(404,'Task not found')
        worker_scope(request,task.assigned_agent_id)
        result = await orch.fail_execution(task_id, task.assigned_agent_id, req.assignment_id, req.error)
        await sync()
        return result

    @app.post('/api/tasks/{task_id}/claim-human')
    async def claim_human(task_id:str, req:ClaimTaskHumanRequest):
        task = await orch.claim_task_by_human(task_id,req.human_name)
        await sync()
        return orch.public_task(task)

    @app.post('/api/tasks/{task_id}/release-human')
    async def release_human(task_id:str):
        task = await orch.release_human(task_id)
        await sync()
        return orch.public_task(task)

    @app.post('/api/tasks/{task_id}/reset-circuit-breaker')
    async def reset(task_id:str):
        task = await orch.reset_circuit_breaker(task_id)
        await sync()
        return orch.public_task(task)

    @app.post('/api/tasks/{task_id}/retry-qa')
    async def retry_qa(task_id:str):
        result = await orch.retry_verification(task_id)
        await sync()
        return result

    def persist_grill():
        temporary = session_file.with_suffix('.tmp')
        temporary.write_text(grill.session.model_dump_json(indent=2),encoding='utf-8')
        temporary.replace(session_file)

    def select_architect(agent_id):
        agents = store.list_agents()
        selected = next((a for a in agents if a.id==agent_id),None) if agent_id else min((a for a in agents if a.executor=='local' and a.status.value=='idle'),key=lambda a:a.tier,default=None)
        if not selected or selected.executor!='local' or selected.status.value!='idle' or selected.id in orch._consulting_agents:
            raise ConflictError('Choose an idle local architect. Remote nodes currently execute coding assignments; another provider will not be substituted.')
        grill.provider = selected.provider
        return selected

    async def planning_call(function,*args):
        async with orch._state_lock:
            orch.ensure_quiescent()
            orch._planning = True
        loop = asyncio.get_running_loop()
        def progress(stage,detail,error=None):
            asyncio.run_coroutine_threadsafe(broadcast({'type':'grill_phase','stage':stage,'detail':detail,'error':error}),loop)
        job = asyncio.create_task(asyncio.to_thread(function,*args,progress_cb=progress))
        try:
            while True:
                try:
                    result = await asyncio.shield(job)
                    break
                except asyncio.CancelledError:
                    if job.cancelled():
                        raise
                    # Finish the authorized, bounded draft operation despite an HTTP
                    # disconnect. Keep ownership until its result is installed, so
                    # an abandoned compile cannot leave a drafted-but-missing plan.
                    continue
            persist_grill()
            return result
        finally:
            orch._planning = False

    @app.post('/api/grill/start',dependencies=[Depends(project_mutation)])
    async def start_grill(req:StartGrillRequest):
        selected = select_architect(req.lead_architect_agent_id)
        current = store.get_project()
        requested = Path(req.workspace_path).resolve() if req.workspace_path else None
        if req.continue_existing and (not requested or requested==Path(current.workspace_path).resolve()):
            async with orch._state_lock:
                orch.ensure_quiescent()
            if current.plan_approved:
                await orch.pause_project()
            target = current.workspace_path
        else:
            if requested and requested==Path(current.workspace_path).resolve():
                raise ConflictError('Select continuation mode to reuse the current workspace')
            project = await orch.initialize_project(req.project_name,req.project_idea,req.workspace_path)
            target = project.workspace_path
        await planning_call(grill.start_interview,req.project_name,req.project_idea,selected.id,selected.display_name,target,req.continue_existing,store.list_tasks())
        await sync()
        return state()

    @app.post('/api/grill/answer',dependencies=[Depends(project_mutation)])
    async def answer_grill(req:AnswerGrillRequest):
        if grill.session.status!='grilling':
            raise ConflictError('Start an interview before submitting answers')
        select_architect(grill.session.lead_architect_agent_id)
        await planning_call(grill.submit_answer,req.answer)
        await sync()
        return state()

    @app.post('/api/grill/compile',dependencies=[Depends(project_mutation)])
    async def compile_grill():
        if grill.session.status!='grilling':
            raise ConflictError('Start an interview before compiling a plan')
        select_architect(grill.session.lead_architect_agent_id)
        existing = store.list_tasks()
        draft = await planning_call(grill.compile_plan_and_tasks,[orch.public_agent(a) for a in store.list_agents()],existing)
        tasks = draft['tasks']
        if grill.session.continue_existing:
            ids = {t.id for t in tasks}
            tasks = [t for t in existing if t.status==TaskStatus.COMPLETED and t.id not in ids] + tasks
        await orch.install_plan(tasks,draft['contracts_markdown'],grill.session.project_name,grill.session.project_idea,grill.session.lead_architect_agent_id)
        await sync()
        return state()

    @app.post('/api/chat/ask')
    async def chat(req:AskAgentRequest):
        result = await orch.ask_project_question(req.question,req.preferred_agent_id)
        await sync()
        return result

    @app.delete('/api/chat')
    async def clear_chat():
        store.clear_chat()
        orch.chat_history=[]
        await sync()
        return {'cleared':True}

    @app.post('/api/graphify/query')
    async def graph_query(req:GraphQueryRequest):
        return {'result':await asyncio.to_thread(orch.graph.query,req.query)}

    @app.post('/api/graphify/explain')
    async def graph_explain(req:GraphQueryRequest):
        return {'result':await asyncio.to_thread(orch.graph.explain_node,req.query)}

    @app.post('/api/graphify/path')
    async def graph_path(req:GraphQueryRequest):
        return {'result':await asyncio.to_thread(orch.graph.path_between,req.query,req.target or '')}

    @app.post('/api/tools/web-search')
    async def search(req:WebSearchRequest):
        from funguys.mcp_bridge.server import web_search
        return {'results':await web_search(req.query,req.max_results)}

    @app.post('/api/tools/fetch-docs')
    async def docs(req:FetchDocsRequest):
        from funguys.mcp_bridge.server import fetch_docs_markdown
        return {'markdown':await fetch_docs_markdown(req.url)}

    @app.post('/api/simulate/hackathon')
    async def simulate():
        from funguys.host.demo import simulate_demo
        return await simulate_demo(state_dir/'simulations')

    @app.get('/screenshots/{filename}')
    async def screenshot(filename:str,request:Request):
        if not request.state.identity or request.state.identity['role']!='admin':
            raise HTTPException(401,'Administrator login required')
        path = orch.qa.screenshots_dir / filename
        if path.suffix!='.png' or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(orch.qa.screenshots_dir.resolve()):
            raise HTTPException(404,'Screenshot not found')
        return FileResponse(path,media_type='image/png')

    @app.websocket('/ws')
    async def websocket(ws:WebSocket):
        origin = ws.headers.get('origin')
        identity = access.identify(ws.cookies.get('funguys_session',''))
        if not identity or identity['role']!='admin' or (origin and urlsplit(origin).netloc!=ws.headers.get('host')):
            await ws.close(code=1008)
            return
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_json({'type':'state_sync','state':state()})
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect,RuntimeError):
            pass
        finally:
            clients.discard(ws)

    @app.get('/worker.py')
    async def worker_source():
        from fastapi import Response
        path = Path(__file__).parent.parent/'worker'/'runtime.py'
        # Serve LF-normalized bytes to guarantee exact hash match across Windows and Linux
        content = path.read_bytes().replace(b'\r\n', b'\n')
        return Response(content=content, media_type='text/plain; charset=utf-8')

    def join_config(request,token,name,codename,profile,used_pct):
        identity = access.identify(token)
        if not identity or identity['role'] != 'invite':
            raise HTTPException(401,'A current invitation is required')
        if profile not in SUBSCRIPTION_PROFILES:
            raise ValueError('Unknown profile')
        request_base = str(request.base_url).rstrip('/')
        # Use request's actual host URL if accessed locally/LAN, fallback to public_url only if request came via tunnel
        host_url = request_base if not app.state.public_url or any(h in request_base for h in ('localhost', '127.0.0.1', app.state.lan_ip)) else app.state.public_url
        return {'host_url':host_url, 'token':token, 'name':name, 'codename':codename, 'profile':profile, 'used_pct':used_pct}

    @app.get('/join.ps1',response_class=PlainTextResponse)
    async def join_ps(request:Request,token:str='',name:str='Teammate',codename:str='',profile:str='codex-pro',used_pct:float=0):
        from funguys.worker.bootstrap import launcher
        return PlainTextResponse(launcher(join_config(request,token,name,codename,profile,used_pct),powershell=True))

    @app.get('/join.sh',response_class=PlainTextResponse)
    async def join_sh(request:Request,token:str='',name:str='Teammate',codename:str='',profile:str='codex-pro',used_pct:float=0):
        from funguys.worker.bootstrap import launcher
        return PlainTextResponse(launcher(join_config(request,token,name,codename,profile,used_pct)))

    @app.get('/',response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse((Path(__file__).parent/'dashboard.html').read_text(encoding='utf-8'))

    @app.get('/dashboard.js')
    async def dashboard_script():
        return FileResponse(Path(__file__).parent/'dashboard.js',media_type='text/javascript')

    return app
