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
        for p in sorted(ws.rglob("*")):
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

    def _call_architect_llm_json(
        self,
        prompt: str,
        timeout_sec: int = 35,
        progress_cb: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Invoke the Lead Architect AI CLI to generate dynamic JSON for Grill-Me turns or Task DAGs."""
        codex_bin = shutil.which("codex") or r"C:\Users\USER\AppData\Local\Programs\OpenAI\Codex\bin\codex.EXE"
        if not os.path.exists(codex_bin):
            self._update_phase(
                "WARNING_FALLBACK",
                "Codex CLI binary not found on host; using built-in Architect fallback.",
                progress_cb,
                error="Codex CLI binary not found on host PATH.",
            )
            return None
        out_file = os.path.join(tempfile.gettempdir(), f"funguys_grill_{uuid.uuid4().hex[:6]}.txt")
        full_prompt = (
            f"{prompt}\n\n"
            f"CRITICAL INSTRUCTION: Do NOT run any shell commands, file reads, or tool calls. "
            f"All workspace files and context are already provided above. Respond IMMEDIATELY with ONLY the raw JSON object."
        )
        cmd = [
            codex_bin,
            "exec",
            "-C",
            tempfile.gettempdir(),
            "--skip-git-repo-check",
            "--ephemeral",
            "-o",
            out_file,
            full_prompt,
        ]

        self._update_phase(
            "CONNECTING_TO_HEAD_BOT",
            f"Connecting to Lead Architect ({self.session.lead_architect_label})...",
            progress_cb,
        )
        try:
            self._update_phase(
                "HEAD_BOT_THINKING",
                f"{self.session.lead_architect_label} is analyzing {self.session.existing_files_count} workspace file(s) & reasoning...",
                progress_cb,
            )
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            self._update_phase(
                "PARSING_RESPONSE",
                f"Received response from {self.session.lead_architect_label}; parsing JSON payload...",
                progress_cb,
            )
            if os.path.exists(out_file):
                raw = Path(out_file).read_text(encoding="utf-8", errors="ignore").strip()
                try:
                    os.remove(out_file)
                except Exception:
                    pass
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    parsed = json.loads(raw[start : end + 1])
                    self.session.last_error = None
                    return parsed
                else:
                    self._update_phase(
                        "WARNING_FALLBACK",
                        "Head Bot returned non-JSON output; using structured fallback.",
                        progress_cb,
                        error=f"Head Bot output did not contain valid JSON (exit={proc.returncode}).",
                    )
            else:
                stderr_snip = (proc.stderr or proc.stdout or "").strip()[:240]
                self._update_phase(
                    "WARNING_FALLBACK",
                    "Head Bot produced no output file; using structured fallback.",
                    progress_cb,
                    error=f"Head Bot CLI exited ({proc.returncode}): {stderr_snip or 'No output'}",
                )
        except subprocess.TimeoutExpired:
            self._update_phase(
                "WARNING_FALLBACK",
                f"Head Bot timed out after {timeout_sec}s; using structured fallback so you aren't blocked.",
                progress_cb,
                error=f"Head Bot CLI timed out after {timeout_sec}s.",
            )
        except Exception as exc:
            self._update_phase(
                "ERROR",
                f"Head Bot invocation error: {exc}",
                progress_cb,
                error=str(exc),
            )
        return None

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
        llm_q = self._call_architect_llm_json(prompt, timeout_sec=25, progress_cb=progress_cb)
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

        # Fallback if CLI is unreachable
        return GrillQuestion(
            step=step,
            total_steps=0,
            question=f"[Turn {step}] What additional runtime constraints, target platform details, or libraries should we lock in for '{self.session.project_name}'? (Or click [COMPILE_PLAN_&_DAG] if ready)",
            options=[
                f"(Recommended) Audit existing {self.session.existing_files_count} workspace files and build the missing execution modules for: {self.session.project_idea}"
                if self.session.continue_existing
                else f"(Recommended) Proceed with native execution stack tailored to: {self.session.project_idea}",
                "Include live CLI/telemetry diagnostics and automated smoke tests",
                "Modularize into core engine, execution harness, and verification suite",
            ],
            ready_to_compile_hint=(step >= 3),
        )

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
            f"2. Generate between 3 and 6 tasks organized into sequential phases (`phase`: 1, 2, 3...) with accurate `dependencies`, `complexity_tier` (1 for core engine/architecture, 2 for UI/tests/packaging), `target_files`, `exported_symbols`, and `verification_commands`.\n"
            f"3. Return ONLY valid JSON matching this schema:\n"
            f'{{\n'
            f'  "contracts_markdown": "# CONTRACTS.md — {name}\\n\\n(Architecture specification, module boundaries, agreed interface contracts, and how to run)",\n'
            f'  "tasks": [\n'
            f'    {{\n'
            f'      "id": "task-p1-...",\n'
            f'      "title": "Phase 1: ...",\n'
            f'      "description": "...",\n'
            f'      "phase": 1,\n'
            f'      "complexity_tier": 1,\n'
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
            for idx, raw_t in enumerate(llm_plan["tasks"]):
                tid = str(raw_t.get("id") or f"task-{idx+1}")
                deps = [str(d) for d in (raw_t.get("dependencies") or [])]
                tasks.append(
                    TaskItem(
                        id=tid,
                        title=str(raw_t.get("title") or f"Phase {idx+1} Module"),
                        description=str(raw_t.get("description") or idea),
                        phase=int(raw_t.get("phase") or (1 if not deps else 2)),
                        complexity_tier=int(raw_t.get("complexity_tier") or (1 if idx == 0 else 2)),
                        dependencies=deps,
                        target_files=[str(f) for f in (raw_t.get("target_files") or [f"module_{idx+1}.py"])],
                        exported_symbols=[str(s) for s in (raw_t.get("exported_symbols") or [])],
                        verification_commands=[str(c) for c in (raw_t.get("verification_commands") or [])],
                        status=TaskStatus.READY if not deps else TaskStatus.PENDING,
                        branch_name=f"task/{tid}",
                    )
                )
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

        # Multi-phase fallback if CLI is unreachable or times out
        contracts_md = f"# CONTRACTS.md — {name}\n\n## Specification\n{idea}\n\n## Interview Decisions\n{qa_block}\n"
        tasks = [
            TaskItem(
                id="task-p1-core-engine",
                title=f"[{name}] Phase 1: Core Engine, Tracking & Control Pipeline",
                description=f"Implement the core tracking, prediction, and device/runtime control engine for {name} per locked specification:\n{idea}\n\n{qa_block}",
                phase=1,
                complexity_tier=1,
                dependencies=[],
                target_files=["src/core/engine.py", "src/core/calibration.py"],
                exported_symbols=[],
                verification_commands=["python -m py_compile src/core/engine.py src/core/calibration.py"],
                status=TaskStatus.READY,
                branch_name="task/task-p1-core-engine",
            ),
            TaskItem(
                id="task-p2-desktop-ui",
                title=f"[{name}] Phase 2: Desktop Window, Live Preview, Calibration & Diagnostics",
                description=f"Implement the desktop UI, live preview, calibration setup, Start/Pause controls, and 5-second diagnostic bundle recorder for {name}:\n{qa_block}",
                phase=2,
                complexity_tier=2,
                dependencies=["task-p1-core-engine"],
                target_files=["src/ui/app_window.py", "src/core/diagnostics.py", "run_app.py"],
                exported_symbols=[],
                verification_commands=["python -m py_compile src/ui/app_window.py src/core/diagnostics.py run_app.py"],
                status=TaskStatus.PENDING,
                branch_name="task/task-p2-desktop-ui",
            ),
            TaskItem(
                id="task-p3-sim-and-packaging",
                title=f"[{name}] Phase 3: Simulated Gameplay Harness, Guided Device Test & Portable Packaging",
                description=f"Build the simulated gameplay validation harness, guided live phone latency/FPS test, portable Windows .exe build script, and automated pytest suite for {name}:\n{qa_block}",
                phase=3,
                complexity_tier=2,
                dependencies=["task-p1-core-engine", "task-p2-desktop-ui"],
                target_files=["src/simulator/gameplay_sim.py", "build_portable.py", "tests/test_bot_suite.py"],
                exported_symbols=[],
                verification_commands=["python -m pytest tests/test_bot_suite.py"],
                status=TaskStatus.PENDING,
                branch_name="task/task-p3-sim-and-packaging",
            ),
        ]
        self.session.status = "drafted"
        self._update_phase(
            "PLAN_COMPILED_FALLBACK",
            f"Compiled CONTRACTS.md and {len(tasks)}-phase Task DAG from your interview decisions.",
            progress_cb,
        )
        return {
            "contracts_markdown": contracts_md,
            "tasks": tasks,
            "session": self.session.model_dump(),
        }
