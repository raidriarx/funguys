"""Regression coverage for the workflow audit's failure and recovery paths."""
import asyncio
import base64
import json
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from funguys.files import bundle, confined_path, validate_dag, validate_relative_path
from funguys.host.app import create_host_app
from funguys.host.graph_engine import GraphifyEngine
from funguys.host.qa_pipeline import AutonomousQAPipeline
from funguys.host.execution import EnvironmentUnavailable
from funguys.host.architect import GrillArchitectEngine
from funguys.models import TaskItem, TaskStatus, AgentStatus, QARunReport
from funguys.worker.runtime import write_snapshot, collect_changes, task_prompt
from test_orchestrator_e2e import host, register, approve, submit


def test_authentication_worker_scope_and_single_use_invite(host):
    app,admin=host
    anonymous=TestClient(app)
    assert anonymous.get('/api/state').status_code==401
    assert anonymous.post('/api/project/approve',json={}).status_code==401
    assert anonymous.get('/').status_code==200
    assert anonymous.get('/join.sh').status_code==401
    invite=admin.post('/api/invites').json()['token']
    worker=TestClient(app,headers={'Authorization':'Bearer '+invite})
    body={'owner_name':'Friend','machine_name':'friend','profile_key':'codex-pro','worker_id':uuid.uuid4().hex}
    response=worker.post('/api/agents/register',json=body)
    assert response.status_code==200,response.text
    identity=response.json()
    assert identity['worker_token']!=invite
    assert worker.post('/api/agents/register',json={**body,'worker_id':uuid.uuid4().hex}).status_code==401
    worker.headers['Authorization']='Bearer '+identity['worker_token']
    assert worker.get('/api/state').status_code==403
    assert worker.delete('/api/tasks').status_code==403
    assert worker.post('/api/agents/not-my-id/heartbeat',json={}).status_code==403
    assert worker.post('/api/agents/register',json={**body,'worker_id':uuid.uuid4().hex}).status_code==403
    assert worker.get('/api/workspace/snapshot').status_code==403
    assert admin.get('/api/state',headers={'Origin':'https://unrelated.example'}).status_code==403


def test_cookie_login_logout_and_websocket_auth(host):
    app,admin=host
    browser=TestClient(app)
    assert browser.post('/api/session',json={'token':'wrong'}).status_code==401
    assert browser.post('/api/session',json={'token':'audit-admin-token'}).status_code==200
    assert browser.get('/api/state').status_code==200
    with browser.websocket_connect('/ws') as ws:
        assert ws.receive_json()['type']=='state_sync'
    browser.delete('/api/session')
    assert browser.get('/api/state').status_code==401
    with pytest.raises(Exception):
        with browser.websocket_connect('/ws'):
            pass


def test_snapshot_round_trip_secrets_binary_and_safe_join_scripts(host):
    app,client=host
    root=app.state.orchestrator.git.workspace_path
    (root/'.env').write_text('SECRET=never-export',encoding='utf-8')
    (root/'asset.bin').write_bytes(b'\x00\xff\x80')
    snap=client.get('/api/workspace/snapshot')
    assert snap.status_code==200,snap.text
    assert '.env' not in snap.json()['files']
    assert base64.b64decode(snap.json()['binary_files']['asset.bin'])==b'\x00\xff\x80'
    token=client.post('/api/invites').json()['token']
    payload='$(Write-Output NEVER_EXECUTE); "\n\' triple quotes \'\'\''
    for endpoint in ('/join.ps1','/join.sh'):
        result=client.get(endpoint,params={'token':token,'name':payload,'codename':payload})
        assert result.status_code==200,result.text
        assert payload not in result.text
        assert 'json.loads(\'\'\'' not in result.text
    assert client.get('/join.sh',params={'token':'audit-admin-token'}).status_code==401


def test_unknown_agent_unapproved_stale_and_empty_submission_rejected(host):
    app,client=host
    assert client.post('/api/tasks/task-1-contracts/submit',json={'agent_id':'unknown','files':{}}).status_code==422
    register(client)
    state=approve(client)
    task=next(t for t in state['tasks'] if t['id']=='task-1-contracts')
    assert submit(client,task,{}).status_code==400
    assert submit(client,task,{'backend/service.py':'x=1\n'},agent_id='unknown').status_code==409
    assert submit(client,task,{'backend/service.py':'x=1\n'},assignment_id='expired').status_code==409
    assert submit(client,task,{'backend/service.py':'x=1\n'},plan_version='old').status_code==409
    client.post('/api/project/pause')
    assert submit(client,task,{'backend/service.py':'x=1\n'}).status_code==409


def test_success_merge_and_duplicate_delivery_are_idempotent(host):
    app,client=host
    register(client)
    task=next(t for t in approve(client)['tasks'] if t['id']=='task-1-contracts')
    submission=uuid.uuid4().hex
    result=submit(client,task,{'backend/service.py':'def sample():\n    return 1\n'},submission_id=submission)
    assert result.status_code==200,result.text
    assert result.json()['status']=='completed'
    manager=app.state.orchestrator.git
    assert (manager.workspace_path/'backend/service.py').read_text(encoding='utf-8').endswith('return 1\n')
    head=manager.head()
    again=submit(client,task,{'backend/service.py':'def sample():\n    return 1\n'},submission_id=submission)
    assert again.status_code==200,again.text
    assert again.json()['replayed']
    assert manager.head()==head
    assert submit(client,task,{'backend/service.py':'x=2\n'},submission_id=submission).status_code==409
    assert submit(client,task,{'backend/service.py':'x=2\n'}).status_code==409


def test_two_workers_disjoint_changes_merge_without_overwriting(host):
    app,client=host
    client.delete('/api/tasks')
    for name in ('a','b'):
        response=client.post('/api/tasks',json={'id':name,'title':name,'description':'Independent file','target_files':[name+'.py'],'complexity_tier':1})
        assert response.status_code==200,response.text
        register(client,name)
    tasks=approve(client)['tasks']
    assert tasks[0]['assigned_agent_id']!=tasks[1]['assigned_agent_id']
    assert tasks[0]['base_revision']==tasks[1]['base_revision']
    for task in tasks:
        result=submit(client,task,{task['id']+'.py':f'VALUE = "{task["id"]}"\n'})
        assert result.status_code==200,result.text
        assert result.json()['status']=='completed',result.json()
    root=app.state.orchestrator.git.workspace_path
    assert (root/'a.py').is_file() and (root/'b.py').is_file()


def test_handoff_invalidates_old_result_and_preserves_wip(host):
    app,client=host
    first=register(client,'First',quota=0)
    second=register(client,'Second',quota=20)
    original=next(t for t in approve(client)['tasks'] if t['id']=='task-1-contracts')
    response=client.post(f"/api/agents/{first['id']}/quota",json={'used_pct':90,'assignment_id':original['assignment_id'],'wip_files':{'backend/service.py':'# retained WIP\n'}})
    assert response.status_code==200,response.text
    current=next(t for t in client.get('/api/state').json()['tasks'] if t['id']==original['id'])
    assert current['assigned_agent_id']==second['id']
    assert current['assignment_id']!=original['assignment_id']
    snap=client.get('/api/workspace/snapshot',params={'task_id':current['id'],'assignment_id':current['assignment_id']}).json()
    assert snap['files']['backend/service.py']=='# retained WIP\n'
    assert submit(client,original,{'backend/service.py':'# late overwrite\n'}).status_code==409
    assert not (app.state.orchestrator.git.workspace_path/'backend/service.py').exists()


def test_expired_heartbeat_and_deleted_worker_requeue(host):
    app,client=host
    worker=register(client,'First')
    register(client,'Second',quota=10)
    original=next(t for t in approve(client)['tasks'] if t['id']=='task-1-contracts')
    agent=app.state.store.get_agent(worker['id'])
    agent.last_heartbeat=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    app.state.store.save_agent(agent)
    assert submit(client,original,{'backend/service.py':'x=1\n'}).status_code==409
    client.post(f"/api/agents/{original['assigned_agent_id']}/heartbeat",json={})
    client.delete(f"/api/agents/{original['assigned_agent_id']}")
    current=next(t for t in client.get('/api/state').json()['tasks'] if t['id']==original['id'])
    assert current['assigned_agent_id']!=original['assigned_agent_id']
    assert current['assignment_id']!=original['assignment_id']


def test_quota_window_cooldown_and_same_owner_machines(host):
    app,client=host
    a=register(client,'Same Owner',quota=90)
    b=register(client,'Same Owner',quota=0)
    assert a['id']!=b['id']
    response=client.post(f"/api/agents/{a['id']}/quota",json={'used_pct':90,'turns_delta':0})
    assert response.json()['agent']['status']=='cooldown'
    response=client.post(f"/api/agents/{a['id']}/quota",json={'used_pct':0,'turns_delta':0,'reset_window':True})
    assert response.json()['agent']['used_pct']==0
    assert response.json()['agent']['status']=='idle'
    agent=app.state.store.get_agent(a['id'])
    agent.status=AgentStatus.COOLDOWN
    agent.cooldown_until='2000-01-01T00:00:00+00:00'
    agent.used_pct=99
    app.state.store.save_agent(agent)
    client.post(f"/api/agents/{a['id']}/heartbeat",json={})
    assert app.state.store.get_agent(a['id']).status==AgentStatus.IDLE
    assert app.state.store.get_agent(a['id']).used_pct==0


def test_human_retry_release_and_approval_dependencies(host):
    app,client=host
    assert client.post('/api/tasks/task-1-contracts/claim-human',json={'human_name':'User'}).status_code==409
    approve(client)
    assert client.post('/api/tasks/task-2-frontend/claim-human',json={'human_name':'User'}).status_code==409
    task=client.post('/api/tasks/task-1-contracts/claim-human',json={'human_name':'User'}).json()
    result=submit(client,task,{'backend/service.py':'def broken(:\n pass\n'},agent_id=None)
    assert result.status_code==200,result.text
    assert result.json()['task']['assigned_human']=='User'
    result=client.post('/api/tasks/task-1-contracts/claim-human',json={'human_name':'User'})
    assert result.status_code==200,result.text
    assert result.json()['assignment_id']!=task['assignment_id']
    result=client.post('/api/tasks/task-1-contracts/release-human')
    assert result.status_code==200,result.text
    assert result.json()['assigned_human'] is None


def test_approval_version_plan_validation_and_no_replacement_during_work(host):
    app,client=host
    assert client.post('/api/project/approve',json={'plan_version':'outdated'}).status_code==409
    assert client.post('/api/tasks',json={'id':'task-1-contracts','title':'duplicate','description':'no'}).status_code==409
    assert client.post('/api/tasks',json={'id':'new','title':'unknown dependency','description':'no','dependencies':['missing']}).status_code==400
    register(client)
    approve(client)
    assert client.post('/api/project/init',json={'name':'Other','description':'No'}).status_code==409
    assert client.delete('/api/tasks').status_code==409
    assert client.post('/api/tasks',json={'id':'new','title':'unreviewed','description':'no'}).status_code==409


@pytest.mark.parametrize('name',['../outside.py','/tmp/file','C:/secret','C:secret','x\\file','.git/config','.funguys/state.db','.env','.env.local','x/../file','x//file','CON','x/key.pem','x/trailing.'])
def test_path_validation_rejects_private_and_escaping_paths(name):
    with pytest.raises(ValueError):
        validate_relative_path(name)


def test_worker_diffs_multiline_json_binary_and_deletions(tmp_path):
    snap={'files':{'a.py':'print("quoted")\n# two lines\n','remove.txt':'remove me'},'binary_files':{'image.bin':base64.b64encode(b'\x80\xff').decode()}}
    write_snapshot(tmp_path,snap)
    (tmp_path/'a.py').write_text('VALUE=2\n',encoding='utf-8',newline='')
    (tmp_path/'remove.txt').unlink()
    (tmp_path/'.env').write_text('never send',encoding='utf-8')
    changes=collect_changes(tmp_path,snap)
    assert changes=={'files':{'a.py':'VALUE=2\n'},'binary_files':{},'deleted_files':['remove.txt']}
    assert (tmp_path/'image.bin').read_bytes()==b'\x80\xff'
    with pytest.raises(ValueError):
        write_snapshot(tmp_path,{'files':{'../escape':'bad'}})


def test_graph_detects_defaults_types_removals_and_restart(tmp_path):
    source=tmp_path/'service.py'
    source.write_text('def sample(x: int = 1, *, flag: bool = False):\n return x\nclass Thing:\n def method(self, x: int): pass\n',encoding='utf-8')
    graph=GraphifyEngine(tmp_path)
    graph.scan_and_detect_contract_deltas('producer','worker',[])
    graph=GraphifyEngine(tmp_path)
    source.write_text('def sample(x: str = "two", *, flag: bool = True):\n return x\n',encoding='utf-8')
    _,alerts=graph.scan_and_detect_contract_deltas('producer','worker',[])
    assert any(a.change_type=='modified_signature' and a.symbol_name=='sample' for a in alerts)
    assert any(a.change_type=='removed_export' and a.symbol_name=='Thing.method' for a in alerts)


def test_cyclic_plans_and_no_synthetic_architect_fallback(monkeypatch):
    tasks=[TaskItem(id='a',title='a',description='a',dependencies=['b']),TaskItem(id='b',title='b',description='b',dependencies=['a'])]
    with pytest.raises(ValueError,match='Cyclic'):
        validate_dag(tasks)
    engine=GrillArchitectEngine()
    monkeypatch.setattr(engine,'_call_architect_llm_json',lambda *a,**k:None)
    with pytest.raises(ValueError,match='no fallback'):
        engine.compile_plan_and_tasks([])


def test_browser_failure_is_not_a_pass_and_checks_are_honest(tmp_path,monkeypatch):
    qa=AutonomousQAPipeline(tmp_path,trusted=True)
    task=TaskItem(id='qa',title='qa',description='qa')
    monkeypatch.setattr(qa,'_execute',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('browser unavailable')))
    passed,logs,screenshot=asyncio.run(qa._run_playwright_check(task,1,'http://127.0.0.1:1/missing'))
    assert not passed and 'browser unavailable' in logs
    assert screenshot is None
    report=asyncio.run(qa.verify_task(task))
    assert report.checks=={'static':'passed','unit':'skipped','browser':'skipped'}
    assert not report.unit_passed and not report.playwright_passed
    task.require_tests=True
    assert not asyncio.run(qa.verify_task(task)).overall_passed


def test_missing_sandbox_blocks_without_spending_qa_retry(tmp_path,monkeypatch):
    from funguys.host.execution import SandboxRunner
    (tmp_path/'test_example.py').write_text('def test_ok(): assert True\n',encoding='utf-8')
    qa=AutonomousQAPipeline(tmp_path,trusted=False)
    monkeypatch.setattr(SandboxRunner,'run',lambda *a,**k:(_ for _ in ()).throw(EnvironmentUnavailable('Docker unavailable')))
    report=asyncio.run(qa.verify_task(TaskItem(id='qa',title='qa',description='qa')))
    assert report.environment_blocked
    assert not report.overall_passed
    assert report.checks['unit']=='unavailable'


def test_ssrf_rejects_private_addresses_and_non_https(monkeypatch):
    from funguys.mcp_bridge.server import public_destination
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    for url in ['https://example.com/','https://127.0.0.1/','http://example.com/','https://user:pass@example.com/','https://example.com:444/']:
        with pytest.raises(ValueError):
            public_destination(url)


def test_tunnel_stale_cache_timeout_and_app_creation_no_tunnel(tmp_path,monkeypatch):
    from funguys.host import tunnel
    monkeypatch.setattr(tunnel,'_TUNNEL_PROC',None)
    monkeypatch.setattr(tunnel,'_ACTIVE_TUNNEL_URL','https://stale.trycloudflare.com')
    assert tunnel.get_public_tunnel_url() is None
    monkeypatch.setattr(tunnel,'start_cloudflare_tunnel',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Factory must not create tunnels')))
    with TestClient(create_host_app(tmp_path,admin_token='test-admin')):
        pass


def test_no_provider_substitution_or_generic_artifact(monkeypatch,tmp_path):
    from funguys.worker import runtime,adapters
    monkeypatch.setattr(runtime.shutil,'which',lambda binary:'codex' if binary=='codex' else None)
    with pytest.raises(RuntimeError,match='no provider was substituted'):
        runtime.cli_command('claude',tmp_path,'question')
    with pytest.raises(ValueError,match='explicit demo'):
        adapters.generate_task_code_artifact({'id':'custom','target_files':['app.py']})


def test_case_colliding_files_are_rejected_before_any_write(tmp_path):
    from funguys.files import decode_files
    with pytest.raises(ValueError,match='collide'):
        decode_files({'A.py':'first','a.py':'second'})
    with pytest.raises(ValueError,match='collide'):
        write_snapshot(tmp_path,{'files':{'A.py':'first','a.py':'second'}})
    assert not list(tmp_path.iterdir())
