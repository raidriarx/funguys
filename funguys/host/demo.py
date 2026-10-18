"""Disposable simulated builders with real QA, separate from the active project."""
import uuid
from funguys.db import StateStore
from funguys.host.orchestrator import FunGuysOrchestrator
from funguys.models import TaskStatus
from funguys.worker.adapters import generate_task_code_artifact


async def simulate_demo(simulations_dir):
    root = simulations_dir / uuid.uuid4().hex[:12]
    orch = FunGuysOrchestrator(StateStore(root/'state.db'),root/'workspaces')
    for name,profile,quota in [('Alex','claude-max',68),('Jordan','codex-pro',25),('Sam','gemini-cli',10)]:
        await orch.register_agent(name,name.lower(),profile,quota,worker_id=uuid.uuid4().hex)
    project = orch.store.get_project()
    await orch.approve_plan(plan_version=project.plan_version)
    task = orch.store.get_task('task-1-contracts')
    if task.assigned_agent_id:
        await orch.report_quota_and_check_handoff(task.assigned_agent_id,82.5,assignment_id=task.assignment_id,handoff_notes='Simulated quota handoff; complete the domain service.')
    for task_id in ('task-1-contracts','task-2-frontend','task-3-tests'):
        attempts = 2 if task_id=='task-3-tests' else 1
        for attempt in range(attempts):
            task = orch.store.get_task(task_id)
            if task.status!=TaskStatus.IN_PROGRESS:
                break
            files = generate_task_code_artifact(task.model_dump(),inject_bug_on_first_try=task_id=='task-3-tests' and attempt==0)
            await orch.submit_task_work(task.id,task.assigned_agent_id,files,'Simulated builder result',assignment_id=task.assignment_id,project_id=task.project_id,plan_version=task.plan_version,base_revision=task.base_revision,submission_id=uuid.uuid4().hex)
    return {'simulation':True, 'note':'Builders are simulated. QA results are real; missing runtime prerequisites are reported as blocked.', 'project':orch.store.get_project().model_dump(), 'tasks':[orch.public_task(t) for t in orch.store.list_tasks()], 'agents':[orch.public_agent(a) for a in orch.store.list_agents()], 'qa_reports':[r.model_dump() for r in orch.store.list_qa_reports()], 'contract_alerts':[a.model_dump() for a in orch.store.list_contract_alerts()], 'graph':orch.graph.last_graph_data, 'events':[e.model_dump() for e in orch.store.list_events()]}
