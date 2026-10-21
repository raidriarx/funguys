"""Interactive 'Grill-Me' Lead Architect Engine for FunGuys.

Allows the user to select which connected AI node acts as the Lead Architect,
conducts an open-ended multi-turn technical interview, and compiles a rich,
multi-stack `CONTRACTS.md` and arbitrary-depth Task DAG when the user clicks
'Compile Plan & DAG'.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from funguys.models import TaskItem, TaskStatus
from funguys.protocol import classify_task_role


class GrillQuestion(BaseModel):
    step: int
    total_steps: int = 0  # 0 indicates open-ended multi-turn chat until user clicks Compile
    question: str
    options: List[str] = Field(default_factory=list)
    ready_to_compile_hint: bool = False


class GrillQA(BaseModel):
    question: str
    answer: str


class GrillSession(BaseModel):
    project_name: str = ""
    project_idea: str = ""
    workspace_path: str = ""
    continue_existing: bool = False
    existing_files_count: int = 0
    scanned_files: List[str] = Field(default_factory=list)
    existing_codebase_summary: str = ""
    lead_architect_agent_id: Optional[str] = None
    lead_architect_label: str = "Lead Architect AI"
    status: str = "idle"  # "idle", "grilling", "drafted"
    current_phase_stage: str = "IDLE"
    current_phase_detail: str = ""
    last_error: Optional[str] = None
    qa_history: List[GrillQA] = Field(default_factory=list)
    current_question: Optional[GrillQuestion] = None


class GrillArchitectEngine:
    """Multi-turn Lead Architect interview and arbitrary-complexity DAG compiler."""

    def __init__(self) -> None:
        self.session = GrillSession()
        self.provider = "claude"

    def _update_phase(
        self,
        stage: str,
        detail: str,
        progress_cb: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> None:
        self.session.current_phase_stage = stage
        self.session.current_phase_detail = detail
        if error is not None:
            self.session.last_error = error
        if progress_cb:
            try:
                progress_cb(stage, detail, self.session.last_error)
            except Exception:
                pass

    @staticmethod
    def scan_existing_workspace(
        workspace_path: Optional[str],
        existing_tasks: Optional[List[TaskItem]] = None,
    ) -> tuple[int, List[str], str]:
        """Inspect an existing local directory on the laptop so the Lead Architect knows what is already built vs incomplete."""
        if not workspace_path:
            return 0, [], ""
        ws = Path(workspace_path)
        if not ws.exists() or not ws.is_dir():
            return 0, [], ""

        files_list: List[str] = []
        snippets: List[str] = []
        from funguys.files import source_files
        for p in source_files(ws):
            from funguys.files import MAX_FILES
            if len(files_list)>=MAX_FILES:
                raise ValueError('Workspace exceeds the planning file limit; exclude generated artifacts or split the project')
            rel_parts = p.relative_to(ws).parts
            if p.is_file() and not any(part in {".git", ".funguys", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"} for part in rel_parts):
                rel = str(p.relative_to(ws)).replace("\\", "/")
                files_list.append(rel)
                if len(snippets) < 10 and p.stat().st_size < 60_000:
                    try:
                        content = p.read_text(encoding="utf-8", errors="ignore")
                        snippets.append(f"--- EXISTING FILE: {rel} ---\n{content[:1200]}")
                    except Exception:
                        pass

        if not files_list:
            return 0, [], ""

        tasks_summary = ""
        if existing_tasks:
            t_lines = [
                f"- {t.id} (Phase {t.phase}, status={t.status.value}): {t.title} -> files: {', '.join(t.target_files)}"
                for t in existing_tasks
            ]
            tasks_summary = "\nExisting Task History:\n" + "\n".join(t_lines) + "\n"

        summary = (
            f"Workspace Path: {ws}\n"
            f"Existing Files ({len(files_list)}): {', '.join(files_list)}\n"
            f"{tasks_summary}\n"
            f"Code Snippets from Existing Half-Completed Project:\n" + "\n\n".join(snippets)
        )
        return len(files_list), files_list, summary

    def _call_architect_llm_json(self, prompt, timeout_sec=90, progress_cb=None):
        from funguys.worker.adapters import run_text_agent
        from funguys.host.workflow_log import NULL_LOGGER
        log = getattr(self, 'logger', None) or NULL_LOGGER
        self._update_phase("HEAD_BOT_THINKING", f"Consulting selected {self.provider} provider", progress_cb)
        raw = None
        try:
            try:
                raw = run_text_agent(self.provider, prompt + "\nReturn only the requested JSON object. Do not call tools.", timeout_sec, role="planning")
            except TypeError:
                raw = run_text_agent(self.provider, prompt + "\nReturn only the requested JSON object. Do not call tools.", timeout_sec)
            log.log('architect_response', f'{self.provider} answered', provider=self.provider, timeout_sec=timeout_sec, prompt=prompt, raw=raw)
            decoder = json.JSONDecoder()
            for start, character in enumerate(raw):
                if character != "{":
                    continue
                try:
                    result, _ = decoder.raw_decode(raw[start:])
                except ValueError:
                    continue
                if isinstance(result, dict):
                    return result
            raise ValueError("Selected architect returned invalid JSON. Retry planning; no fallback plan was substituted.")
        except Exception as exc:
            log.exception('architect_failed', f'{self.provider} architect call failed', exc, provider=self.provider, timeout_sec=timeout_sec, prompt=prompt, raw=raw)
            self._update_phase("ERROR", str(exc), progress_cb, error=str(exc))
            raise

    def start_interview(
        self,
        project_name: str,
        project_idea: str,
        lead_architect_agent_id: Optional[str] = None,
        lead_architect_label: str = "Lead Architect AI",
        workspace_path: Optional[str] = None,
        continue_existing: bool = False,
        existing_tasks: Optional[List[TaskItem]] = None,
        progress_cb: Optional[Any] = None,
    ) -> GrillSession:
        self.session = GrillSession(
            project_name=project_name.strip() or "New Project",
            project_idea=project_idea.strip(),
            workspace_path=workspace_path or "",
            continue_existing=continue_existing,
            lead_architect_agent_id=lead_architect_agent_id,
            lead_architect_label=lead_architect_label,
            status="grilling",
            current_phase_stage="SCANNING_FILES",
            current_phase_detail=f"Scanning local workspace files at {workspace_path or 'default workspace'}...",
            last_error=None,
            qa_history=[],
        )
        if progress_cb:
            try:
                progress_cb(self.session.current_phase_stage, self.session.current_phase_detail, None)
            except Exception:
                pass

        file_count, files_list, codebase_summary = self.scan_existing_workspace(workspace_path, existing_tasks)
        is_continuing = bool(continue_existing or file_count > 0)
        self.session.continue_existing = is_continuing
        self.session.existing_files_count = file_count
        self.session.scanned_files = files_list
        self.session.existing_codebase_summary = codebase_summary

        self._update_phase(
            "FILES_SCANNED",
            f"Scanned {file_count} existing file(s) ({', '.join(files_list[:6]) or 'clean workspace'}). Preparing Head Bot prompt...",
            progress_cb,
        )
        self.session.current_question = self._next_question(progress_cb=progress_cb)
        self._update_phase(
            "AWAITING_USER_TURN",
            f"Ready — Turn {self.session.current_question.step} loaded from {lead_architect_label}.",
            progress_cb,
        )
        return self.session

    def submit_answer(self, answer: str, progress_cb: Optional[Any] = None) -> GrillSession:
        if self.session.current_question:
            self.session.qa_history.append(
                GrillQA(
                    question=self.session.current_question.question,
                    answer=answer.strip(),
                )
            )
        self._update_phase(
            "RECORDING_DECISION",
            f"Locked Decision #{len(self.session.qa_history)}. Consulting {self.session.lead_architect_label} for Turn #{len(self.session.qa_history) + 1}...",
            progress_cb,
        )
        self.session.current_question = self._next_question(progress_cb=progress_cb)
        self._update_phase(
            "AWAITING_USER_TURN",
            f"Ready — Turn {self.session.current_question.step} loaded from {self.session.lead_architect_label}.",
            progress_cb,
        )
        return self.session

    def _next_question(self, progress_cb: Optional[Any] = None) -> GrillQuestion:
        step = len(self.session.qa_history) + 1
        history_text = "\n".join(
            f"Turn {i+1} Architect Question: {qa.question}\nTurn {i+1} User Response: {qa.answer}"
            for i, qa in enumerate(self.session.qa_history)
        )
        existing_block = ""
        if self.session.continue_existing and self.session.existing_codebase_summary:
            existing_block = (
                f"\nIMPORTANT — EXISTING / HALF-COMPLETED CODEBASE ON LAPTOP:\n"
                f"{self.session.existing_codebase_summary}\n"
                f"The user wants to CONTINUE and COMPLETE or Pivot/Extend this existing project. "
                f"Inspect what files already exist versus what is missing, stubbed, or needs real implementation.\n"
            )

        prompt = (
            f"You are '{self.session.lead_architect_label}', the Lead Architect AI in the FunGuys Multi-AI engineering platform.\n"
            f"You are conducting a deep technical 'Grill-Me' interview with the user to design/continue:\n"
            f"- Project Name: {self.session.project_name}\n"
            f"- Project / Continuation Goal: {self.session.project_idea}\n"
            f"- Mode: {'CONTINUE_EXISTING_PROJECT (' + str(self.session.existing_files_count) + ' existing files)' if self.session.continue_existing else 'NEW_PROJECT'}\n"
            f"{existing_block}\n"
            f"Interview transcript so far:\n{history_text or '(No turns yet — ask the first high-impact architectural question)'}\n\n"
            f"Instructions:\n"
            f"1. This workflow supports ANY domain: Web3 smart contracts (Solidity/Foundry/Rust), local desktop/internet automation bots (OS screen capture, mouse/keyboard control, browser automation, APIs), native games (Pygame, Godot/Unity scripts, WebGL/Canvas), CLI tools, or distributed backends.\n"
            f"2. If continuing an existing project, reference the actual existing files in the workspace and ask what should be kept, replaced, or added next.\n"
            f"3. NEVER guess or force a generic web app template. Ask the single most important next technical question to lock down the real runtime environment, libraries, protocols, architecture, or edge cases.\n"
            f"4. Provide 3 concrete options (prefix the first with '(Recommended) '), and set `ready_to_compile_hint` to true if you already have enough detail after 3+ turns.\n"
            f"Return ONLY valid JSON with this exact schema:\n"
            f'{{"question": "...", "options": ["(Recommended) ...", "Option 2 ...", "Option 3 ..."], "ready_to_compile_hint": false}}'
        )
        llm_q = self._call_architect_llm_json(prompt, timeout_sec=120, progress_cb=progress_cb)
        if (
            llm_q
            and isinstance(llm_q.get("question"), str)
            and isinstance(llm_q.get("options"), list)
            and len(llm_q["options"]) >= 2
        ):
            opts = [str(o) for o in llm_q["options"][:4]]
            if opts and not opts[0].startswith("(Recommended)"):
                opts[0] = f"(Recommended) {opts[0]}"
            return GrillQuestion(
                step=step,
                total_steps=0,
                question=llm_q["question"],
                options=opts,
                ready_to_compile_hint=bool(llm_q.get("ready_to_compile_hint", step >= 4)),
            )

        raise ValueError("Architect response did not contain a valid question and options")

    def compile_plan_and_tasks(
        self,
        connected_agents: Optional[List[Dict[str, Any]]] = None,
        existing_tasks: Optional[List[TaskItem]] = None,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Compile `CONTRACTS.md` and an arbitrary-depth multi-stack Task DAG using the Lead Architect AI."""
        name = self.session.project_name or "Engineering Project"
        idea = self.session.project_idea or ""

        self._update_phase(
            "SCANNING_FILES_FOR_DAG",
            f"Scanning workspace `{self.session.workspace_path or 'default'}` & existing task history before compiling DAG...",
            progress_cb,
        )
        if self.session.workspace_path:
            file_count, files_list, codebase_summary = self.scan_existing_workspace(self.session.workspace_path, existing_tasks)
            self.session.existing_files_count = file_count
            self.session.scanned_files = files_list
            self.session.existing_codebase_summary = codebase_summary

        # Distill decisions into a concise, token-efficient summary so reasoning completes fast
        qa_summary_lines = []
        for idx, item in enumerate(self.session.qa_history):
            clean_q = item.question.split("?")[0].strip() + "?" if "?" in item.question else item.question.strip()
            clean_a = item.answer.replace("(Recommended)", "").strip()
            qa_summary_lines.append(f"- Q{idx + 1}: {clean_q} -> Decision: {clean_a}")
        qa_block = "\n".join(qa_summary_lines) if qa_summary_lines else "- Direct specification from user prompt"

        agents_desc = "\n".join(
            f"- Node `{a.get('machine_name')}` ({a.get('display_name')}, Tier {a.get('tier')})"
            for a in (connected_agents or [])
        ) or "- Tier 1 (Lead Architect / Core Systems) and Tier 2 (Builder / Verification)"

        existing_context_prompt = ""
        if self.session.continue_existing and self.session.existing_codebase_summary:
            existing_context_prompt = (
                f"\nEXISTING HALF-COMPLETED PROJECT IN WORKSPACE:\n"
                f"{self.session.existing_codebase_summary}\n\n"
                f"CONTINUATION REQUIREMENT:\n"
                f"- Do NOT recreate files that are already complete unless they need to be refactored or replaced to fulfill the user's goal.\n"
                f"- Design the continuation tasks (`phase`: 1, 2, 3...) that build the missing or upgraded components on top of (or replacing) the existing workspace files.\n"
            )

        prompt = (
            f"You are '{self.session.lead_architect_label}', the Lead Architect AI in FunGuys.\n"
            f"Compile a comprehensive, production-grade `CONTRACTS.md` and multi-phase Task DAG for:\n"
            f"- Project Name: {name}\n"
            f"- Project / Continuation Specification: {idea}\n"
            f"- Mode: {'CONTINUE_EXISTING_CODEBASE' if self.session.continue_existing else 'NEW_PROJECT'}\n"
            f"- Connected AI Nodes in Pool:\n{agents_desc}\n"
            f"- Key Decisions from Interview:\n{qa_block}\n"
            f"{existing_context_prompt}\n"
            f"Requirements:\n"
            f"1. Do NOT use toy templates. Design the REAL files, modules, and architecture required for '{name}' (whether Web3, local macro/bot, game, CLI, or full-stack).\n"
            f"2. Generate between 3 and 6 tasks organized into sequential phases (`phase`: 1, 2, 3...) with accurate `dependencies`, `complexity_tier` (1 for important architectural/core work, 2 for gruntwork/boilerplate/tests), `task_role` / `work_role` ('important_coding', 'gruntwork', or 'testing'), `target_files`, `exported_symbols`, and `verification_commands`.\n"
            f"3. Route by work importance, NOT by token volume: if a task is large/token-heavy but architecturally critical, set `complexity_tier`: 1 and `work_role`: 'important_coding' so Argon Low / Opus High handles it; set `complexity_tier`: 2 and `work_role`: 'gruntwork' (or 'testing') for Flash High / Sol High / Opus Medium.\n"
            f"4. Parse out work so Argon / Opus High usage pools do not run out easily: split large features into a slim `important_coding` core-logic task and separate `gruntwork` (boilerplate/wiring) and `testing` tasks.\n"
            f"5. Return ONLY valid JSON matching this schema:\n"
            f'{{\n'
            f'  "contracts_markdown": "# CONTRACTS.md — {name}\\n\\n(Architecture specification, module boundaries, agreed interface contracts, and how to run)",\n'
            f'  "tasks": [\n'
            f'    {{\n'
            f'      "id": "task-p1-...",\n'
            f'      "title": "Phase 1: ...",\n'
            f'      "description": "...",\n'
            f'      "phase": 1,\n'
            f'      "complexity_tier": 1,\n'
            f'      "task_role": "important_coding",\n'
            f'      "work_role": "important_coding",\n'
            f'      "dependencies": [],\n'
            f'      "target_files": ["src/..."],\n'
            f'      "exported_symbols": ["..."],\n'
            f'      "verification_commands": ["..."]\n'
            f'    }}\n'
            f'  ]\n'
            f'}}'
        )

        llm_plan = self._call_architect_llm_json(prompt, timeout_sec=180, progress_cb=progress_cb)
        if (
            llm_plan
            and isinstance(llm_plan.get("contracts_markdown"), str)
            and isinstance(llm_plan.get("tasks"), list)
            and len(llm_plan["tasks"]) >= 1
        ):
            contracts_md = llm_plan["contracts_markdown"]
            tasks: List[TaskItem] = []
            import re as _re

            def _clean_id(value, fallback):
                cleaned = _re.sub(r'[^A-Za-z0-9_-]+', '-', str(value or '')).strip('-_')[:96]
                return cleaned or fallback

            id_map = {str(raw_t.get("id")): _clean_id(raw_t.get("id"), f"task-{i+1}") for i, raw_t in enumerate(llm_plan["tasks"]) if raw_t.get("id")}
            for idx, raw_t in enumerate(llm_plan["tasks"]):
                tid = _clean_id(raw_t.get("id"), f"task-{idx+1}")
                deps = [id_map.get(str(d), _clean_id(d, str(d))) for d in (raw_t.get("dependencies") or [])]
                try:
                    tier = 1 if int(raw_t.get("complexity_tier") or (1 if idx == 0 else 2)) <= 1 else 2
                except (TypeError, ValueError):
                    tier = 2
                raw_role = str(raw_t.get("task_role") or raw_t.get("work_role") or "").strip().lower()
                if raw_role not in ("important_coding", "gruntwork", "testing"):
                    raw_role = classify_task_role(raw_t)
                tasks.append(
                    TaskItem(
                        id=tid,
                        title=str(raw_t.get("title") or f"Phase {idx+1} Module"),
                        description=str(raw_t.get("description") or idea),
                        phase=max(1, int(raw_t.get("phase") or (1 if not deps else 2))),
                        complexity_tier=tier,
                        task_role=raw_role,
                        work_role=raw_role,
                        dependencies=deps,
                        target_files=[str(f) for f in (raw_t.get("target_files") or [f"module_{idx+1}.py"])],
                        exported_symbols=[str(s) for s in (raw_t.get("exported_symbols") or [])],
                        verification_commands=[str(c) for c in (raw_t.get("verification_commands") or [])],
                        require_tests=bool(raw_t.get("require_tests", False) or raw_role == "testing"),
                        require_browser=bool(raw_t.get("require_browser", False)),
                        status=TaskStatus.READY if not deps else TaskStatus.PENDING,
                        branch_name=f"task/{tid}",
                    )
                )
            from funguys.files import validate_dag
            validate_dag([*([t for t in (existing_tasks or []) if t.status == TaskStatus.COMPLETED and t.id not in {n.id for n in tasks}] if self.session.continue_existing else []), *tasks])
            self.session.status = "drafted"
            self._update_phase(
                "PLAN_COMPILED",
                f"Compiled CONTRACTS.md and {len(tasks)} multi-phase task(s). Ready to unlock execution!",
                progress_cb,
            )
            return {
                "contracts_markdown": contracts_md,
                "tasks": tasks,
                "session": self.session.model_dump(),
            }

        raise ValueError("Architect response did not contain a valid contracts/task plan; no fallback was substituted")
