"""Graphify Knowledge Graph Engine & Deterministic AST Contract Delta Analyzer.

Implements zero-token Pass-1 AST parsing + NetworkX community detection & God Node
identification, plus compatibility with the `graphifyy` CLI (`graphify`).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import uuid
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple,Iterator

import networkx as nx

from funguys.models import ContractAlert, TaskItem

IGNORED_DIRS: Set[str] = {
    ".git",
    ".funguys",
    ".graphify",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
}

CODE_EXTENSIONS: Set[str] = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".json"}


class GraphifyEngine:
    """Builds and queries a compact structural knowledge graph of the project."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path
        from funguys.files import runtime_directory
        self.graph_dir = runtime_directory(workspace_path,'.graphify')
        self.G: nx.DiGraph = nx.DiGraph()
        self.last_signatures: Dict[str, Dict[str, str]] = {}
        baseline = self.graph_dir / 'signatures.json'
        if baseline.exists():
            try:
                self.last_signatures = json.loads(baseline.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass
        self.last_graph_data = {}
        self._baseline_loaded = baseline.exists()

    def build_graph(self) -> Dict[str, Any]:
        """Run deterministic Pass-1 AST analysis across the workspace (0 LLM tokens)."""
        self.last_error = None
        G = nx.DiGraph()
        current_signatures: Dict[str, Dict[str, str]] = {}
        warnings = []
        total_bytes = 0

        if not self.workspace_path.exists():
            self.workspace_path.mkdir(parents=True, exist_ok=True)

        for index,file_path in enumerate(self._iter_source_files()):
            from funguys.files import MAX_FILE,MAX_BUNDLE,MAX_FILES
            if index>=MAX_FILES:
                warnings.append('Graph truncated at the workspace file limit')
                break
            if file_path.stat().st_size>MAX_FILE:
                warnings.append('Oversized source file skipped: '+file_path.relative_to(self.workspace_path).as_posix())
                continue
            total_bytes += file_path.stat().st_size
            if total_bytes>MAX_BUNDLE:
                warnings.append('Graph truncated at the workspace byte limit')
                break
            rel_path = file_path.relative_to(self.workspace_path).as_posix()
            file_node = f"file:{rel_path}"
            G.add_node(
                file_node,
                id=file_node,
                label=rel_path,
                type="file",
                file=rel_path,
                signature=rel_path,
            )

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if file_path.suffix == ".py":
                self._parse_python_ast(G, rel_path, file_node, content, current_signatures)
            elif file_path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
                self._parse_ts_js_symbols(G, rel_path, file_node, content, current_signatures)

        self.G = G
        graph_data = self._export_graph_artifacts(G, current_signatures)
        graph_data['warnings'] = warnings
        self.last_graph_data = graph_data
        if not self._baseline_loaded:
            self.last_signatures = {k:dict(v) for k,v in current_signatures.items()}
            (self.graph_dir/'signatures.json').write_text(json.dumps(self.last_signatures),encoding='utf-8')
            self._baseline_loaded = True
        return graph_data

    def scan_and_detect_contract_deltas(
        self,
        task_id: str,
        agent_id: str,
        all_tasks: List[TaskItem],
    ) -> Tuple[Dict[str, Any], List[ContractAlert]]:
        """Re-compile the graph and return ~100-token ContractAlerts for changed signatures."""
        old_sigs = {k: dict(v) for k, v in self.last_signatures.items()}
        graph_data = self.build_graph()
        new_sigs = graph_data.get("signatures", {})

        alerts: List[ContractAlert] = []
        all_keys = set(old_sigs.keys()) | set(new_sigs.keys())
        for sym_key in sorted(all_keys):
            old_info = old_sigs.get(sym_key)
            new_info = new_sigs.get(sym_key)
            if old_info and new_info and old_info["signature"] != new_info["signature"]:
                dep_tasks = self._find_dependent_tasks(
                    new_info["file"], new_info["symbol"], task_id, all_tasks
                )
                alerts.append(
                    ContractAlert(
                        id=f"alert-{uuid.uuid4().hex[:8]}",
                        task_id=task_id,
                        agent_id=agent_id,
                        symbol_name=new_info["symbol"],
                        file_path=new_info["file"],
                        change_type="modified_signature",
                        old_signature=old_info["signature"],
                        new_signature=new_info["signature"],
                        dependent_task_ids=dep_tasks,
                        summary=(
                            f"[Contract Delta] {new_info['file']} :: `{new_info['signature']}` "
                            f"(was `{old_info['signature']}`) -> Notify tasks: {', '.join(dep_tasks) or 'none'}"
                        ),
                    )
                )
            elif not old_info and new_info:
                dep_tasks = self._find_dependent_tasks(
                    new_info["file"], new_info["symbol"], task_id, all_tasks
                )
                if dep_tasks:
                    alerts.append(
                        ContractAlert(
                            id=f"alert-{uuid.uuid4().hex[:8]}",
                            task_id=task_id,
                            agent_id=agent_id,
                            symbol_name=new_info["symbol"],
                            file_path=new_info["file"],
                            change_type="added_export",
                            old_signature="",
                            new_signature=new_info["signature"],
                            dependent_task_ids=dep_tasks,
                            summary=(
                                f"[New Contract Symbol] {new_info['file']} exported `{new_info['signature']}` -> Notify tasks: {', '.join(dep_tasks)}"
                            ),
                        )
                    )
            elif old_info and not new_info:
                dep_tasks = self._find_dependent_tasks(old_info['file'], old_info['symbol'], task_id, all_tasks)
                alerts.append(ContractAlert(id='alert-' + uuid.uuid4().hex, task_id=task_id, agent_id=agent_id,
                    symbol_name=old_info['symbol'], file_path=old_info['file'], change_type='removed_export',
                    old_signature=old_info['signature'], dependent_task_ids=dep_tasks,
                    summary=f"[Removed Contract] {old_info['file']} :: {old_info['signature']} -> Notify tasks: {', '.join(dep_tasks)}"))

        self.last_signatures = {k: dict(v) for k, v in new_sigs.items()}
        baseline = self.graph_dir / 'signatures.json'
        temporary = baseline.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.last_signatures), encoding='utf-8')
        temporary.replace(baseline)
        return graph_data, alerts

    def query(self, question: str) -> str:
        """Equivalent to `graphify query <question>` — returns matching nodes & edges."""
        if self.G.number_of_nodes() == 0:
            self.build_graph()
        terms = [t.lower() for t in re.split(r"\W+", question) if len(t) > 2]
        scored: List[Tuple[int, str, Dict[str, Any]]] = []
        for node_id, attrs in self.G.nodes(data=True):
            haystack = f"{node_id} {attrs.get('label', '')} {attrs.get('signature', '')}".lower()
            score = sum(2 if t in attrs.get("label", "").lower() else 1 for t in terms if t in haystack)
            if score > 0:
                scored.append((score, node_id, attrs))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:8]
        if not top:
            return "No matching graph nodes found. Check `.graphify/GRAPH_REPORT.md` for overview."
        lines = [f"### Graphify Query Results for: `{question}`"]
        for _, nid, attrs in top:
            neighbors = list(self.G.successors(nid))[:5] + list(self.G.predecessors(nid))[:5]
            lines.append(
                f"- **{attrs.get('label')}** (`{attrs.get('type')}` in `{attrs.get('file')}`): "
                f"`{attrs.get('signature')}` | Connected to: {', '.join(neighbors) or 'none'}"
            )
        return "\n".join(lines)

    def path_between(self, source_query: str, target_query: str) -> str:
        """Equivalent to `graphify path <NodeA> <NodeB>`."""
        if self.G.number_of_nodes() == 0:
            self.build_graph()
        src = self._resolve_node(source_query)
        dst = self._resolve_node(target_query)
        if not src or not dst:
            return f"Could not resolve nodes (`{source_query}` -> `{src}`, `{target_query}` -> `{dst}`)."
        try:
            undirected = self.G.to_undirected()
            path = nx.shortest_path(undirected, src, dst)
            return " -> ".join(path)
        except nx.NetworkXNoPath:
            return f"No structural dependency path between `{src}` and `{dst}`."

    def explain_node(self, symbol_query: str) -> str:
        """Equivalent to `graphify explain <Symbol>`."""
        if self.G.number_of_nodes() == 0:
            self.build_graph()
        nid = self._resolve_node(symbol_query)
        if not nid:
            return f"Symbol `{symbol_query}` not found in Graphify graph."
        attrs = self.G.nodes[nid]
        in_edges = [f"{u} ({d.get('relation', 'links')})" for u, _, d in self.G.in_edges(nid, data=True)]
        out_edges = [f"{v} ({d.get('relation', 'links')})" for _, v, d in self.G.out_edges(nid, data=True)]
        return (
            f"**Node:** `{nid}`\n"
            f"- **Type:** {attrs.get('type')}\n"
            f"- **File:** `{attrs.get('file')}`\n"
            f"- **Signature:** `{attrs.get('signature')}`\n"
            f"- **Incoming Dependencies:** {', '.join(in_edges) or 'none'}\n"
            f"- **Outgoing Calls/Defines:** {', '.join(out_edges) or 'none'}"
        )

    def get_subgraph_digest(self, target_files: List[str]) -> str:
        """Generate a compact ~150-token structural digest for Baton Pass handoffs."""
        if getattr(self,'last_error',None):
            return 'Graph unavailable; inspect current source directly. '+self.last_error
        if self.G.number_of_nodes() == 0:
            self.build_graph()
        lines = ["### Graphify Structural Context (Zero-Token AST Map)"]
        matched = 0
        for nid, attrs in self.G.nodes(data=True):
            if attrs.get("type") == "file":
                continue
            f = attrs.get("file", "")
            if not target_files or any(tf in f or f in tf for tf in target_files):
                lines.append(f"- `{f}` :: `{attrs.get('signature', attrs.get('label'))}`")
                matched += 1
                if matched >= 15:
                    break
        if matched == 0:
            lines.append("- Workspace has no symbols in target files yet (greenfield task).")
        return "\n".join(lines)

    def _resolve_node(self, query: str) -> Optional[str]:
        q = query.lower()
        for nid, attrs in self.G.nodes(data=True):
            if q == nid.lower() or q == str(attrs.get("label", "")).lower():
                return nid
        for nid, attrs in self.G.nodes(data=True):
            if q in nid.lower() or q in str(attrs.get("label", "")).lower():
                return nid
        return None

    def _find_dependent_tasks(
        self,
        changed_file: str,
        symbol_name: str,
        source_task_id: str,
        all_tasks: List[TaskItem],
    ) -> List[str]:
        dependents: List[str] = []
        for t in all_tasks:
            if t.id == source_task_id:
                continue
            if source_task_id in t.dependencies:
                dependents.append(t.id)
            elif any(changed_file in tf for tf in t.target_files):
                dependents.append(t.id)
            elif symbol_name in t.description:
                dependents.append(t.id)
        return sorted(set(dependents))

    def _iter_source_files(self) -> Iterator[Path]:
        from funguys.files import source_files
        return (p for p in source_files(self.workspace_path) if p.suffix in CODE_EXTENSIONS)

    def _parse_python_ast(
        self,
        G: nx.DiGraph,
        rel_path: str,
        file_node: str,
        content: str,
        signatures: Dict[str, Dict[str, str]],
    ) -> None:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ast.unparse(node.args)
                ret = ""
                if node.returns:
                    try:
                        ret = f" -> {ast.unparse(node.returns)}"
                    except Exception:
                        ret = ""
                prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
                sig = f"{prefix}{node.name}({args}){ret}"
                sym_node = f"sym:{rel_path}:{node.name}"
                G.add_node(
                    sym_node,
                    id=sym_node,
                    label=node.name,
                    type="function",
                    file=rel_path,
                    signature=sig,
                )
                G.add_edge(file_node, sym_node, relation="defines")
                signatures[sym_node] = {"file": rel_path, "symbol": node.name, "signature": sig}

                # Extract function calls inside body
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        G.add_edge(sym_node, f"call:{sub.func.id}", relation="calls")

            elif isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = f'{node.name}.{method.name}'
                        key = f'sym:{rel_path}:{name}'
                        signature = f"{'async ' if isinstance(method, ast.AsyncFunctionDef) else ''}def {name}({ast.unparse(method.args)})"
                        if method.returns:
                            signature += ' -> ' + ast.unparse(method.returns)
                        signatures[key] = {'file': rel_path, 'symbol': name, 'signature': signature}
                        G.add_node(key, id=key, label=name, type='method', file=rel_path, signature=signature)
                        G.add_edge(file_node, key, relation='defines')
                methods = [
                    n.name
                    for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                fields = [ast.unparse(n) for n in node.body if isinstance(n,ast.AnnAssign)]
                bases = ', '.join(ast.unparse(n) for n in node.bases)
                sig = f"class {node.name}({bases})[{'; '.join(fields)}; {', '.join(methods)}]"
                sym_node = f"sym:{rel_path}:{node.name}"
                G.add_node(
                    sym_node,
                    id=sym_node,
                    label=node.name,
                    type="class",
                    file=rel_path,
                    signature=sig,
                )
                G.add_edge(file_node, sym_node, relation="defines")
                signatures[sym_node] = {"file": rel_path, "symbol": node.name, "signature": sig}

            elif isinstance(node, ast.ImportFrom) and node.module:
                mod_path = node.module.replace(".", "/") + ".py"
                G.add_edge(file_node, f"file:{mod_path}", relation="imports")

    def _parse_ts_js_symbols(
        self,
        G: nx.DiGraph,
        rel_path: str,
        file_node: str,
        content: str,
        signatures: Dict[str, Dict[str, str]],
    ) -> None:
        # Match exported functions, components, interfaces, types
        patterns = [
            (r"export\s+(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*(\([^)]*\))", "function"),
            (r"export\s+interface\s+([A-Za-z0-9_]+)", "interface"),
            (r"export\s+const\s+([A-Za-z0-9_]+)\s*=", "const"),
            (r"export\s+type\s+([A-Za-z0-9_]+)\s*=", "type"),
        ]
        for regex, sym_type in patterns:
            for match in re.finditer(regex, content):
                name = match.group(1)
                params = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                sig = f"{sym_type} {name}{params}"
                # Conservative change fingerprint covers interface members, return types,
                # aliases and implementation changes without pretending regex is a TS parser.
                sig += ' [module:' + hashlib.sha256(content.encode()).hexdigest()[:16] + ']'
                sym_node = f"sym:{rel_path}:{name}"
                G.add_node(
                    sym_node,
                    id=sym_node,
                    label=name,
                    type=sym_type,
                    file=rel_path,
                    signature=sig,
                )
                G.add_edge(file_node, sym_node, relation="defines")
                signatures[sym_node] = {"file": rel_path, "symbol": name, "signature": sig}
        if re.search(r'\bexport\b|module\.exports',content):
            key = f'sym:{rel_path}:module_exports'
            sig = 'module exports [' + hashlib.sha256(content.encode()).hexdigest()[:16] + ']'
            G.add_node(key,id=key,label='module_exports',type='module',file=rel_path,signature=sig)
            G.add_edge(file_node,key,relation='defines')
            signatures[key] = {'file':rel_path,'symbol':'module_exports','signature':sig}

    def _export_graph_artifacts(
        self,
        G: nx.DiGraph,
        signatures: Dict[str, Dict[str, str]],
    ) -> Dict[str, Any]:
        # Compute God Nodes (highest degree centrality)
        degrees = sorted(G.degree(), key=lambda x: x[1], reverse=True)
        god_nodes = [
            {"id": nid, "degree": deg, "label": G.nodes[nid].get("label", nid)}
            for nid, deg in degrees[:8]
            if deg > 0
        ]

        # Compute Community Clusters using greedy modularity on undirected projection
        clusters: List[Dict[str, Any]] = []
        if G.number_of_nodes() > 0:
            try:
                undirected = G.to_undirected()
                comms = list(nx.community.greedy_modularity_communities(undirected))
                for idx, comm in enumerate(comms[:10]):
                    clusters.append(
                        {
                            "cluster_id": f"cluster-{idx + 1}",
                            "size": len(comm),
                            "nodes": sorted(list(comm))[:15],
                        }
                    )
            except Exception:
                pass

        nodes_list = [
            {
                "id": nid,
                "label": attrs.get("label", nid.split(":")[-1]),
                "type": attrs.get("type", "reference"),
                "file": attrs.get("file", ""),
                "signature": attrs.get("signature", ""),
                "degree": G.degree(nid),
            }
            for nid, attrs in G.nodes(data=True)
        ]
        edges_list = [
            {"source": u, "target": v, "relation": d.get("relation", "links")}
            for u, v, d in G.edges(data=True)
        ]

        graph_json = {
            "node_count": len(nodes_list),
            "edge_count": len(edges_list),
            "god_nodes": god_nodes,
            "clusters": clusters,
            "nodes": nodes_list,
            "edges": edges_list,
            "signatures": signatures,
        }

        # Write `.graphify/graph.json` and `.graphify/GRAPH_REPORT.md`
        (self.graph_dir / "graph.json").write_text(
            json.dumps(graph_json, indent=2), encoding="utf-8"
        )

        report_lines = [
            "# GRAPH_REPORT.md — Pre-Compiled Project Knowledge Graph",
            "",
            f"- **Total Structural Nodes:** {len(nodes_list)}",
            f"- **Total Dependency Edges:** {len(edges_list)}",
            f"- **Detected Community Clusters:** {len(clusters)}",
            "",
            "## God Nodes (Highly Connected Central Components)",
        ]
        if god_nodes:
            for gn in god_nodes:
                report_lines.append(f"- `{gn['id']}` (degree: {gn['degree']})")
        else:
            report_lines.append("- None yet")

        report_lines.append("")
        report_lines.append("## Exported Symbol & API Contracts")
        if signatures:
            for _, info in sorted(signatures.items()):
                report_lines.append(f"- `{info['file']}` :: `{info['signature']}`")
        else:
            report_lines.append("- No symbols parsed yet")

        (self.graph_dir / "GRAPH_REPORT.md").write_text(
            "\n".join(report_lines) + "\n", encoding="utf-8"
        )
        return graph_json
