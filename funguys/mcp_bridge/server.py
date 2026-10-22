"""Bounded public HTTPS document retrieval with pinned, validated DNS addresses, plus stdio MCP worker server for IDE agents."""
from __future__ import annotations
import asyncio
import http.client
import ipaddress
import json
import platform
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional
from bs4 import BeautifulSoup

_FETCH_LIMIT = asyncio.Semaphore(4)
MAX_RESPONSE = 2_000_000

def public_destination(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Only public HTTPS URLs on port 443 are accepted')
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
        raise ValueError('Local, private and reserved network destinations are blocked')
    return parsed, addresses[0]

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname,443,timeout=10,context=ssl.create_default_context())
        self.address = address
    def connect(self):
        sock = socket.create_connection((self.address,443),timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:
            sock.close()
            raise

def fetch_public_page(url):
    deadline = time.monotonic()+45
    for _ in range(5):
        if time.monotonic()>deadline:
            raise ValueError('Document request exceeded its time limit')
        parsed, address = public_destination(url)
        connection = PinnedHTTPSConnection(parsed.hostname,address)
        try:
            path = urllib.parse.urlunsplit(('', '', parsed.path or '/',parsed.query,''))
            connection.request('GET',path,headers={'User-Agent':'FunGuys/0.2 documentation reader','Accept-Encoding':'identity'})
            response = connection.getresponse()
            if response.status in (301,302,303,307,308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Upstream redirect has no destination')
                url = urllib.parse.urljoin(url,location)
                continue
            if response.status != 200:
                raise ValueError(f'Upstream returned HTTP {response.status}')
            if response.getheader('Content-Encoding','identity').lower() != 'identity':
                raise ValueError('Compressed upstream responses are not accepted')
            content_type = response.getheader('Content-Type','text/html').split(';')[0].lower()
            if not (content_type.startswith('text/') or content_type in ('application/json','application/xhtml+xml')):
                raise ValueError('The requested resource is not a text document')
            chunks,size = [],0
            while size<=MAX_RESPONSE:
                if time.monotonic()>deadline:
                    raise ValueError('Document download exceeded its time limit')
                chunk = response.read1(min(65536,MAX_RESPONSE+1-size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
            if len(raw)>MAX_RESPONSE:
                raise ValueError('Upstream document exceeds the 2 MB limit')
            return raw.decode('utf-8',errors='replace')
        finally:
            connection.close()
    raise ValueError('Too many upstream redirects')

async def fetch_docs_markdown(url,max_chars=8000):
    async with _FETCH_LIMIT:
        html = await asyncio.wait_for(asyncio.to_thread(fetch_public_page,url),timeout=55)
    soup = BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','nav','footer','noscript']):
        tag.decompose()
    return re.sub(r'\n{3,}','\n\n',soup.get_text(separator='\n')).strip()[:max_chars]

async def web_search(query,max_results=5):
    url = 'https://html.duckduckgo.com/html/?q=' + urllib.parse.quote_plus(query)
    async with _FETCH_LIMIT:
        html = await asyncio.wait_for(asyncio.to_thread(fetch_public_page,url),timeout=55)
    soup = BeautifulSoup(html,'html.parser')
    results = []
    for item in soup.select('.result')[:max(1,min(max_results,10))]:
        title = item.select_one('.result__title a')
        snippet = item.select_one('.result__snippet')
        if title:
            link = str(title.get('href',''))
            destination = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('uddg',[link])[0]
            if urllib.parse.urlsplit(destination).scheme not in ('http','https'):
                continue
            results.append({'title':title.get_text(strip=True),'url':destination,'snippet':snippet.get_text(strip=True) if snippet else ''})
    return results


MCP_TOOLS = [
    {
        "name": "fg_poll",
        "description": "Register/heartbeat this IDE agent with the FunGuys Host and retrieve any assigned task, sliced contracts, blackboard facts, and Controller steering prompts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fg_listen",
        "description": "Wait for / fetch the next assigned task or Main Controller directive for this IDE agent (alias for fg_poll with blackboard and inbox status).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fg_check_inbox",
        "description": "Check queued Main Controller steering prompts, `/btw` notes, and shared blackboard facts/tripwires without altering task execution.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fg_signal",
        "description": "Emit a 1-line C2 coordination signal (CLAIM, RELEASE, FACT, TRIPWIRE, TRIGGER, STEER, HOLD, GO, VETO, DISPUTE, HANDOFF) to the Main Controller and shared blackboard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "description": "CLAIM | RELEASE | FACT | TRIPWIRE | TRIGGER | STEER | HOLD | GO | VETO | DISPUTE | HANDOFF"},
                "target": {"type": "string", "description": "File path, fact key, task_id, or target agent_id"},
                "payload": {"type": "string", "description": "Short fact value, steering message, or preferred agent_id"},
            },
            "required": ["op", "target"],
        },
    },
    {
        "name": "fg_dispute",
        "description": "Escalate a contract contradiction, locked dependency, or cross-task conflict to the Main Controller for arbitration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Conflicting file path, module, or task_id"},
                "reason": {"type": "string", "description": "Description of the contract mismatch or dispute"},
                "task_id": {"type": "string"},
            },
            "required": ["target", "reason"],
        },
    },
    {
        "name": "fg_handoff",
        "description": "Trigger an on-demand context window reset and WIP baton-pass handoff for an agent without quota cooldown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to reset and hand off (defaults to self)"},
                "target_agent_id": {"type": "string", "description": "Optional preferred successor agent_id"},
                "handoff_notes": {"type": "string", "description": "Baton-pass summary for the next agent"},
                "wip_files": {"type": "object", "description": "Optional map of uncommitted WIP file paths -> UTF-8 content"},
            },
            "required": [],
        },
    },
    {
        "name": "fg_steer",
        "description": "Queue a prompt, live-steer, or immediately interrupt another agent via the Main Controller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_agent_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string", "description": "queue | steer | interrupt"},
                "task_id": {"type": "string"},
            },
            "required": ["target_agent_id", "prompt"],
        },
    },
    {
        "name": "fg_btw",
        "description": "Send a network-wide `/btw` live steering guidance message to the Main Controller so it triages and streams guidance to active workers without interrupting their tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The `/btw` guidance or question for the Main Controller"},
                "preferred_agent_id": {"type": "string", "description": "Optional target agent ID (defaults to 'auto' for Controller triage)"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "fg_submit",
        "description": "Submit completed file changes for the currently assigned task to the Host 3-layer QA pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "assignment_id": {"type": "string"},
                "project_id": {"type": "string"},
                "plan_version": {"type": "string"},
                "base_revision": {"type": "string"},
                "files": {"type": "object", "description": "Map of relative file path -> UTF-8 content"},
                "commit_message": {"type": "string"},
            },
            "required": ["task_id", "assignment_id", "project_id", "plan_version", "base_revision", "files"],
        },
    },
]


class MCPWorkerServer:
    """JSON-RPC 2.0 stdio MCP server allowing Cursor, Claude Desktop, Windsurf, or VS Code to join the FunGuys swarm."""

    def __init__(self, host_url: str, token: str, name: str = "IDE-Agent", profile: str = "mcp-ide", is_controller: bool = False) -> None:
        self.host = host_url.rstrip("/")
        self.token = token
        self.name = name
        self.profile = profile
        self.is_controller = is_controller
        self.worker_id = f"mcp-{uuid.uuid4().hex[:16]}"
        self.agent_id: Optional[str] = None

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.host + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def ensure_registered(self) -> str:
        if self.agent_id:
            return self.agent_id
        res = self._request(
            "POST",
            "/api/agents/register",
            {
                "worker_id": self.worker_id,
                "owner_name": self.name,
                "machine_name": f"{platform.node() or 'ide'}-mcp",
                "profile_key": self.profile,
                "initial_used_pct": 0.0,
                "executor": "mcp",
                "is_controller": self.is_controller,
            },
        )
        self.agent_id = res["id"]
        if res.get("worker_token"):
            self.token = res["worker_token"]
        return self.agent_id

    def handle_call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        agent_id = self.ensure_registered()
        if name in ("fg_poll", "fg_listen"):
            hb = self._request("POST", f"/api/agents/{agent_id}/heartbeat", {})
            assign = self._request("GET", "/api/worker/assignment")
            return {"agent_id": agent_id, "heartbeat": hb, "assignment": assign.get("assignment")}
        if name == "fg_check_inbox":
            hb = self._request("POST", f"/api/agents/{agent_id}/heartbeat", {})
            bb = self._request("GET", "/api/blackboard")
            return {
                "agent_id": agent_id,
                "queued_prompts": hb.get("queued_prompts", []),
                "assignment": hb.get("assignment"),
                "blackboard": bb.get("blackboard", []),
            }
        if name == "fg_signal":
            return self._request(
                "POST",
                "/api/controller/signal",
                {
                    "sender_agent_id": agent_id,
                    "signals": [
                        {
                            "op": arguments.get("op", "FACT"),
                            "target": arguments.get("target", ""),
                            "payload": arguments.get("payload", ""),
                        }
                    ],
                },
            )
        if name == "fg_dispute":
            return self._request(
                "POST",
                "/api/controller/arbitrate",
                {
                    "sender_agent_id": agent_id,
                    "target": arguments["target"],
                    "reason": arguments["reason"],
                    "task_id": arguments.get("task_id"),
                },
            )
        if name == "fg_handoff":
            target_agent = arguments.get("agent_id") or agent_id
            return self._request(
                "POST",
                f"/api/agents/{target_agent}/handoff",
                {
                    "target_agent_id": arguments.get("target_agent_id"),
                    "handoff_notes": arguments.get("handoff_notes", "MCP on-demand context reset & handoff"),
                    "wip_files": arguments.get("wip_files", {}),
                },
            )
        if name == "fg_steer":
            target_id = arguments["target_agent_id"]
            return self._request(
                "POST",
                f"/api/agents/{target_id}/steer",
                {
                    "prompt": arguments["prompt"],
                    "mode": arguments.get("mode", "steer"),
                    "task_id": arguments.get("task_id"),
                    "sender_agent_id": agent_id,
                },
            )
        if name == "fg_btw":
            msg = (arguments.get("message") or "").strip()
            if not msg.lower().startswith("/btw"):
                msg = "/btw " + msg
            return self._request(
                "POST",
                "/api/chat/ask",
                {
                    "question": msg,
                    "preferred_agent_id": arguments.get("preferred_agent_id", "auto"),
                },
            )
        if name == "fg_submit":
            tid = arguments["task_id"]
            return self._request(
                "POST",
                f"/api/tasks/{tid}/submit",
                {
                    "agent_id": agent_id,
                    "assignment_id": arguments["assignment_id"],
                    "project_id": arguments["project_id"],
                    "plan_version": arguments["plan_version"],
                    "base_revision": arguments["base_revision"],
                    "files": arguments.get("files", {}),
                    "submission_id": uuid.uuid4().hex,
                    "commit_message": arguments.get("commit_message", f"MCP submission for {tid}"),
                },
            )
        raise ValueError(f"Unknown MCP tool: {name}")

    def handle_rpc(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "funguys-mcp-worker", "version": "0.3.0"},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
        if method == "tools/call":
            params = msg.get("params") or {}
            try:
                out = self.handle_call_tool(params.get("name", ""), params.get("arguments") or {})
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False},
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
                }
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        return None


def run_mcp_stdio_worker(host_url: str, token: str, name: str = "IDE-Agent", profile: str = "mcp-ide", is_controller: bool = False) -> None:
    """Run the JSON-RPC 2.0 MCP worker loop over stdin/stdout."""
    server = MCPWorkerServer(host_url=host_url, token=token, name=name, profile=profile, is_controller=is_controller)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = server.handle_rpc(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
