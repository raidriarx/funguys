"""End-to-end regressions for authenticated, isolated orchestration."""
from pathlib import Path
import uuid
import pytest
from fastapi.testclient import TestClient
from funguys.host.app import create_host_app
from funguys.host.qa_pipeline import AutonomousQAPipeline
from funguys.worker.adapters import generate_task_code_artifact

@pytest.fixture
def host(tmp_path,monkeypatch):
    monkeypatch.setenv('FUNGUYS_TRUSTED_EXECUTION','1')
    app=create_host_app(tmp_path,admin_token='audit-admin-token')
    with TestClient(app,headers={'Authorization':'Bearer audit-admin-token'}) as client:
        yield app,client

def register(client,name='Alex',profile='codex-pro',quota=0):
    result=client.post('/api/agents/register',json={'owner_name':name,'machine_name':name+'-pc','profile_key':profile,'initial_used_pct':quota,'worker_id':uuid.uuid4().hex})
    assert result.status_code==200,result.text
    return result.json()

def approve(client):
    state=client.get('/api/state').json()
    result=client.post('/api/project/approve',json={'plan_version':state['project']['plan_version']})
    assert result.status_code==200,result.text
    return result.json()

def submit(client,task,files,**extra):
    return client.post('/api/tasks/'+task['id']+'/submit',json={'agent_id':task['assigned_agent_id'],'assignment_id':task['assignment_id'],'project_id':task['project_id'],'plan_version':task['plan_version'],'base_revision':task['base_revision'],'submission_id':uuid.uuid4().hex,'files':files,**extra})

def test_plan_approval_gate_and_role_based_tier_routing(host):
    app,client=host
    alex=register(client,'Alex','claude-max',50)
    register(client,'Sam','gemini-cli',10)
    before=client.get('/api/state').json()
    assert not before['project']['plan_approved']
    assert all(t['status']!='in_progress' for t in before['tasks'])
    approved=approve(client)
    task=next(t for t in approved['tasks'] if t['id']=='task-1-contracts')
    assert task['assigned_agent_id']==alex['id']
    assert task['assignment_id'] and task['base_revision']
    assert not (Path(approved['project']['workspace_path'])/'backend/service.py').exists()

def test_baton_pass_handoff_and_full_hackathon_simulation(host,monkeypatch):
    app,client=host
    original=client.get('/api/state').json()
    async def browser(self,*args):
        return True,'Browser mocked for isolated orchestration regression',None
    monkeypatch.setattr(AutonomousQAPipeline,'_run_playwright_check',browser)
    result=client.post('/api/simulate/hackathon')
    assert result.status_code==200,result.text
    data=result.json()
    assert all(t['status']=='completed' for t in data['tasks']),[(t['id'],t['status'],t['blocked_reason'],t['last_error']) for t in data['tasks']]
    assert next(t for t in data['tasks'] if t['id']=='task-3-tests')['retry_count']==1
    assert data['graph']['node_count']>=5
    assert data['contract_alerts']
    after=client.get('/api/state').json()
    assert after['project']==original['project']
    assert after['agents']==original['agents']
    assert after['tasks']==original['tasks']

def test_circuit_breaker_trips_after_three_failed_qa_attempts(host):
    app,client=host
    register(client)
    data=approve(client)
    main=Path(data['project']['workspace_path'])
    head=app.state.orchestrator.git.head()
    for attempt in range(3):
        task=next(t for t in client.get('/api/state').json()['tasks'] if t['id']=='task-1-contracts')
        result=submit(client,task,{'backend/service.py':f'# attempt {attempt}\ndef broken(:\n pass\n'})
        assert result.status_code==200,result.text
        assert not (main/'backend/service.py').exists()
        assert app.state.orchestrator.git.head()==head
    task=next(t for t in client.get('/api/state').json()['tasks'] if t['id']=='task-1-contracts')
    assert task['status']=='blocked_circuit_breaker'
    assert task['retry_count']==3
    result=client.post('/api/tasks/task-1-contracts/reset-circuit-breaker')
    assert result.status_code==200,result.text
    assert result.json()['status']=='in_progress'
