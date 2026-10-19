"""Concurrent requests, retained-code recovery and real HTTP worker transport."""
import asyncio
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
import uvicorn
from fastapi.testclient import TestClient
from funguys.host.app import create_host_app
from funguys.host.qa_pipeline import AutonomousQAPipeline
from funguys.host.execution import EnvironmentUnavailable
from funguys.models import QARunReport,TaskStatus
from funguys.worker.runtime import WorkerRuntime
from test_orchestrator_e2e import host, register, approve, submit


def test_pause_during_qa_rejects_result_and_keeps_server_responsive(host,monkeypatch):
    app,client=host
    register(client)
    task=next(t for t in approve(client)['tasks'] if t['id']=='task-1-contracts')
    entered,release=threading.Event(),threading.Event()
    async def wait_qa(self,task,*args):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return QARunReport(id=uuid.uuid4().hex,task_id=task.id,attempt=1,static_passed=True,unit_passed=True,playwright_passed=True,overall_passed=True)
    monkeypatch.setattr(AutonomousQAPipeline,'verify_task',wait_qa)
    head=app.state.orchestrator.git.head()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(submit,client,task,{'backend/service.py':'VALUE=1\n'})
        try:
            assert entered.wait(10)
            start=time.monotonic()
            assert client.get('/api/state').status_code==200
            assert time.monotonic()-start<2
            assert client.post('/api/project/pause').status_code==200
            assert client.post('/api/project/init',json={'name':'Replacement','description':'No'}).status_code==409
        finally:
            release.set()
        response=future.result(timeout=10)
    assert response.status_code==409,response.text
    assert app.state.orchestrator.git.head()==head
    assert not (app.state.orchestrator.git.workspace_path/'backend/service.py').exists()


def test_concurrent_conflicting_edits_preserve_first_verified_change(host):
    app,client=host
    client.delete('/api/tasks')
    manager=app.state.orchestrator.git
    (manager.workspace_path/'shared.py').write_bytes(b'VALUE=0\n')
    manager.git('add','--','shared.py')
    manager.git('commit','-m','Initial shared file')
    for name in ('first','second'):
        response=client.post('/api/tasks',json={'id':name,'title':name,'description':'Change the shared value','target_files':['shared.py'],'complexity_tier':1})
        assert response.status_code==200,response.text
        register(client,name)
    tasks=approve(client)['tasks']
    first=submit(client,tasks[0],{'shared.py':'VALUE=1\n'})
    assert first.status_code==200 and first.json()['status']=='completed',first.text
    second=submit(client,tasks[1],{'shared.py':'VALUE=2\n'})
    assert second.status_code==400,second.text
    assert (manager.workspace_path/'shared.py').read_text()=='VALUE=1\n'
    current=next(t for t in client.get('/api/state').json()['tasks'] if t['id']==tasks[1]['id'])
    assert current['status']=='blocked_environment'
    assert 'conflict' in current['blocked_reason'].lower()
    assert client.post('/api/tasks/'+current['id']+'/reset-circuit-breaker').status_code==200
    current=next(t for t in client.get('/api/state').json()['tasks'] if t['id']==tasks[1]['id'])
    assert current['status']=='in_progress'
    snap=client.get('/api/workspace/snapshot',params={'task_id':current['id'],'assignment_id':current['assignment_id']}).json()
    assert snap['files']['shared.py']=='VALUE=1\n'


def test_environment_recheck_reuses_candidate_without_new_builder(host,monkeypatch):
    app,client=host
    register(client)
    task=next(t for t in approve(client)['tasks'] if t['id']=='task-1-contracts')
    async def unavailable(self,task,*args):
        return QARunReport(id=uuid.uuid4().hex,task_id=task.id,attempt=1,static_passed=True,unit_passed=False,playwright_passed=False,overall_passed=False,environment_blocked=True,checks={'static':'passed','unit':'unavailable','browser':'skipped'},logs='Missing QA environment')
    monkeypatch.setattr(AutonomousQAPipeline,'verify_task',unavailable)
    result=submit(client,task,{'backend/service.py':'VALUE=5\n'})
    assert result.status_code==200,result.text
    assert result.json()['status']=='blocked_environment'
    assert result.json()['task']['retry_count']==0
    async def available(self,task,*args):
        assert (self.workspace_path/'backend/service.py').read_text()=='VALUE=5\n'
        return QARunReport(id=uuid.uuid4().hex,task_id=task.id,attempt=1,static_passed=True,unit_passed=True,playwright_passed=True,overall_passed=True)
    monkeypatch.setattr(AutonomousQAPipeline,'verify_task',available)
    result=client.post('/api/tasks/task-1-contracts/retry-qa')
    assert result.status_code==200,result.text
    assert result.json()['status']=='completed'
    assert (app.state.orchestrator.git.workspace_path/'backend/service.py').read_text()=='VALUE=5\n'


def test_real_http_worker_recovers_assignment_created_during_registration(tmp_path,monkeypatch):
    monkeypatch.setenv('FUNGUYS_TRUSTED_EXECUTION','1')
    app=create_host_app(tmp_path,admin_token='transport-admin')
    admin=TestClient(app,headers={'Authorization':'Bearer transport-admin'})
    assert admin.delete('/api/tasks').status_code==200
    assert admin.post('/api/tasks',json={'id':'transport','title':'HTTP transport','description':'Write VALUE=7 into app.py','target_files':['app.py'],'complexity_tier':1}).status_code==200
    approve(admin)
    token=admin.post('/api/invites').json()['token']
    calls=[]
    def fake_cli(provider,root,prompt,*args,**kwargs):
        calls.append((provider,root))
        assert (root/'CONTRACTS.md').is_file()
        (root/'app.py').write_bytes(b'VALUE=7\n')
        return True,False,'Implemented'
    monkeypatch.setattr('funguys.worker.runtime.execute_cli',fake_cli)
    monkeypatch.setattr('funguys.worker.adapters.run_cli_agent_task',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Host must not execute a remote assignment')))
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    server_thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
    server_thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:
        time.sleep(.02)
    assert server.started
    worker=WorkerRuntime({'host_url':f'http://127.0.0.1:{listener.getsockname()[1]}','name':'Friend','codename':'transport-node','profile':'codex-pro','token':token,'state_dir':str(tmp_path/'worker-identity')})
    worker_errors=[]
    def run():
        try:
            worker.run()
        except Exception as exc:
            worker_errors.append(str(exc))
    worker_thread=threading.Thread(target=run,daemon=True)
    worker_thread.start()
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            task=app.state.store.get_task('transport')
            if task.status.value=='completed':
                break
            time.sleep(.05)
        assert task.status.value=='completed',(task.model_dump(),worker_errors)
        assert len(calls)==1
        assert (app.state.orchestrator.git.workspace_path/'app.py').read_bytes()==b'VALUE=7\n'
        identity=worker.identity
        assert identity['token']!=token
        assert worker.identity_path.exists()
    finally:
        worker.stop.set()
        worker_thread.join(timeout=10)
        server.should_exit=True
        server_thread.join(timeout=10)
        listener.close()
    assert not worker_errors,worker_errors


def test_worker_recovers_from_stale_identity_file_using_fresh_invite(tmp_path):
    import json, hashlib
    app = create_host_app(tmp_path, admin_token='audit-admin-token')
    admin = TestClient(app, headers={'Authorization': 'Bearer audit-admin-token'})
    token = admin.post('/api/invites').json()['token']

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    server_thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started

    state_dir = tmp_path / 'worker-identity'
    state_dir.mkdir(parents=True, exist_ok=True)
    machine = 'stale-node'
    host_url = f'http://127.0.0.1:{port}'
    name = hashlib.sha256((host_url + '\n' + machine).encode()).hexdigest()[:24]
    identity_file = state_dir / (name + '.json')
    identity_file.write_text(json.dumps({
        'worker_id': 'stale-worker-id',
        'token': 'invalid-or-revoked-token',
        'agent_id': 'agent-old'
    }), encoding='utf-8')

    worker = WorkerRuntime({
        'host_url': host_url,
        'name': 'Friend',
        'codename': machine,
        'profile': 'codex-pro',
        'token': token,
        'state_dir': str(state_dir)
    })
    try:
        result = worker.register()
        assert result['id'].startswith('agent-')
        assert worker.token != 'invalid-or-revoked-token'
        assert worker.token == result['worker_token']
        saved = json.loads(identity_file.read_text(encoding='utf-8'))
        assert saved['token'] == result['worker_token']
        assert saved['agent_id'] == result['id']
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        listener.close()


def test_typescript_interface_shape_change_is_detected(tmp_path):
    from funguys.host.graph_engine import GraphifyEngine
    source=tmp_path/'types.ts'
    source.write_text('export interface Item { id: string; }',encoding='utf-8')
    graph=GraphifyEngine(tmp_path)
    graph.build_graph()
    source.write_text('export interface Item { id: number; required: boolean; }',encoding='utf-8')
    _,alerts=graph.scan_and_detect_contract_deltas('producer','worker',[])
    assert any(a.change_type=='modified_signature' and a.symbol_name=='Item' for a in alerts)


def test_contract_edit_approval_rotates_version_and_rejects_older_review(host):
    app,client=host
    old=client.get('/api/state').json()['project']['plan_version']
    response=client.post('/api/project/approve',json={'plan_version':old,'contracts_markdown':'# Revised reviewed contract'})
    assert response.status_code==200,response.text
    assert response.json()['project']['plan_version']!=old
    assert client.post('/api/project/approve',json={'plan_version':old}).status_code==409


def test_snapshot_failure_blocks_worker_instead_of_polling_forever(tmp_path,monkeypatch):
    worker=WorkerRuntime({'host_url':'https://example.com','token':'test','state_dir':str(tmp_path)})
    worker.agent_id='worker'
    assignment={'task':{'id':'task','assignment_id':'assignment'}}
    def failed_snapshot(*args,**kwargs):
        raise ValueError('Snapshot too large')
    deliveries=[]
    monkeypatch.setattr(worker,'request',failed_snapshot)
    monkeypatch.setattr(worker,'deliver',lambda path,payload:deliveries.append((path,payload)))
    with pytest.raises(ValueError,match='too large'):
        worker.execute(assignment)
    assert len(deliveries)==1
    assert deliveries[0][0]=='/api/tasks/task/execution-failed'
    assert worker.assignment is None


def test_container_boundary_filters_credentials_and_limits_resources(tmp_path,monkeypatch):
    from funguys.host import execution
    root=tmp_path/'source'
    root.mkdir()
    (root/'.env').write_text('SECRET=never-export',encoding='utf-8')
    (root/'public.py').write_text('VALUE=1',encoding='utf-8')
    commands=[]
    def process(argv,cwd,timeout=60,env=None):
        commands.append(argv)
        if 'run' in argv:
            mount=argv[argv.index('--mount')+1]
            scratch=Path(mount.split('source=',1)[1].split(',target=',1)[0])
            assert (scratch/'public.py').exists()
            assert not (scratch/'.env').exists()
        return 0,'OK'
    monkeypatch.setattr(execution.shutil,'which',lambda name:'docker')
    monkeypatch.setattr(execution,'run_process',process)
    monkeypatch.setattr(execution.subprocess,'run',lambda *a,**k:None)
    assert execution.SandboxRunner(root,trusted=False).run(['python','-m','pytest'])==(0,'OK')
    run=next(command for command in commands if 'run' in command)
    for value in ['--read-only','--cap-drop','--security-opt','--memory','--pids-limit','--user']:
        assert value in run
    assert run[run.index('--network')+1]=='none'


def test_private_redirect_rejected_before_second_connection(monkeypatch):
    from funguys.mcp_bridge import server
    calls=[]
    def resolve(host,*args,**kwargs):
        return [(2,1,6,'',('127.0.0.1' if host=='127.0.0.1' else '93.184.216.34',443))]
    class Response:
        status=302
        def getheader(self,name,default=None):
            return 'https://127.0.0.1/secret' if name=='Location' else default
    class Connection:
        def __init__(self,host,address):
            calls.append((host,address))
        def request(self,*args,**kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(server.socket,'getaddrinfo',resolve)
    monkeypatch.setattr(server,'PinnedHTTPSConnection',Connection)
    with pytest.raises(ValueError,match='blocked'):
        server.fetch_public_page('https://example.com/')
    assert calls==[('example.com','93.184.216.34')]


def test_second_host_cannot_modify_a_running_hosts_state(host):
    app,client=host
    store=app.state.store
    task=store.get_task('task-1-contracts')
    task.status=TaskStatus.IN_QA
    store.save_task(task)
    root=store.db_path.parent.parent
    with pytest.raises(RuntimeError,match='host lock'):
        create_host_app(root,admin_token='second-host')
    assert store.get_task(task.id).status.value=='in_qa'


def test_host_lock_releases_on_shutdown_for_restart(tmp_path):
    for _ in range(2):
        with TestClient(create_host_app(tmp_path,admin_token='restart-token'),headers={'Authorization':'Bearer restart-token'}) as client:
            assert client.get('/api/state').status_code==200


def test_failed_workspace_activation_keeps_original_project_usable(host,tmp_path):
    app,client=host
    manager=app.state.orchestrator.git
    original=client.get('/api/state').json()['project']
    unborn=tmp_path/'unborn'
    unborn.mkdir()
    manager.git('init','-b','main',cwd=unborn)
    response=client.post('/api/project/init',json={'name':'Invalid workspace','description':'Unborn Git repository','workspace_path':str(unborn)})
    assert response.status_code==503,response.text
    assert app.state.orchestrator.git is manager
    assert client.get('/api/state').json()['project']==original
    assert client.get('/api/workspace/snapshot').status_code==200


def test_cancelled_planning_keeps_ownership_until_bounded_job_finishes(tmp_path,monkeypatch):
    from funguys.host.app import AnswerGrillRequest
    from funguys.models import AgentNode
    from funguys.host.architect import GrillQuestion
    app=create_host_app(tmp_path,admin_token='planning-token')
    engine=app.state.grill_engine
    engine.session.status='grilling'
    engine.session.lead_architect_agent_id='local'
    engine.session.current_question=GrillQuestion(step=1,question='Question?',options=['Option A','Option B'])
    app.state.store.save_agent(AgentNode(id='local',owner_name='Host',machine_name='host',profile_key='codex-pro',provider='codex',display_name='Local Codex',executor='local',worker_id=uuid.uuid4().hex))
    entered,release=threading.Event(),threading.Event()
    def blocked(answer,progress_cb=None):
        entered.set()
        release.wait(5)
        return engine.session
    monkeypatch.setattr(engine,'submit_answer',blocked)
    endpoint=next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/api/grill/answer')
    async def exercise():
        task=asyncio.create_task(endpoint(AnswerGrillRequest(answer='A')))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel()
            await asyncio.sleep(.05)
            assert app.state.orchestrator._planning
            assert not task.done()
        finally:
            release.set()
        result=await task
        assert result['grill']['status']=='grilling'
        assert not app.state.orchestrator._planning
    try:
        asyncio.run(exercise())
    finally:
        app.state.host_lock.release()


def test_generated_command_output_is_bounded_without_log_files(tmp_path):
    import sys
    from funguys.host.execution import run_process
    code,text=run_process([sys.executable,'-c',"import sys; sys.stdout.write('x'*2000000)"],tmp_path,timeout=5)
    assert code==0
    assert len(text)<=100000
    assert set(text)=={'x'}
    assert not list(tmp_path.iterdir())


def test_quota_updates_during_qa_do_not_erase_the_next_assignment(host,monkeypatch):
    app,client=host
    client.delete('/api/tasks')
    for name in ('first','second'):
        assert client.post('/api/tasks',json={'id':name,'title':name,'description':'Independent work','target_files':[name+'.py'],'complexity_tier':1}).status_code==200
    worker=register(client)
    first=next(t for t in approve(client)['tasks'] if t['id']=='first')
    entered,release=threading.Event(),threading.Event()
    async def qa(self,task,*args):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return QARunReport(id=uuid.uuid4().hex,task_id=task.id,attempt=1,static_passed=True,unit_passed=True,playwright_passed=True,overall_passed=True)
    monkeypatch.setattr(AutonomousQAPipeline,'verify_task',qa)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(submit,client,first,{'first.py':'VALUE=1\n'})
        try:
            assert entered.wait(10)
            assert client.post(f"/api/agents/{worker['id']}/quota",json={'used_pct':90,'assignment_id':first['assignment_id']}).status_code==200
            assert client.post(f"/api/agents/{worker['id']}/quota",json={'used_pct':0,'turns_delta':0,'reset_window':True}).status_code==200
            assert app.state.store.get_agent(worker['id']).current_task_id=='first'
            assert app.state.store.get_task('second').status==TaskStatus.READY
        finally:
            release.set()
        response=future.result(timeout=10)
    assert response.status_code==200,response.text
    assert app.state.store.get_agent(worker['id']).current_task_id=='second'
    assert app.state.store.get_task('second').status==TaskStatus.IN_PROGRESS
