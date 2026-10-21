"""LAN IP detection and automatic Cloudflare/ngrok tunnel launcher for Host PC."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

_ACTIVE_TUNNEL_URL: Optional[str] = None
_TUNNEL_PROC: Optional[subprocess.Popen] = None


def get_lan_ip() -> str:
    """Return the Host PC's IP address, prioritizing Tailscale / ZeroTier mesh overlay IPs if active."""
    try:
        import psutil

        addrs = psutil.net_if_addrs()
        for iface_name, snics in addrs.items():
            iface_lower = iface_name.lower()
            if any(k in iface_lower for k in ["tailscale", "zerotier", "zt"]):
                for snic in snics:
                    if snic.family == socket.AF_INET and not snic.address.startswith("127."):
                        return snic.address
        for snics in addrs.values():
            for snic in snics:
                if snic.family == socket.AF_INET and snic.address.startswith("100."):
                    parts = snic.address.split(".")
                    if len(parts) == 4 and 64 <= int(parts[1]) <= 127:
                        return snic.address
    except Exception:
        pass

    try:
        cmd = ["ipconfig"] if os.name == "nt" else ["ifconfig"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=3)
        for match in re.finditer(r"\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b", out):
            return match.group(1)
    except Exception:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_public_tunnel_url() -> Optional[str]:
    """Return only a URL owned by a still-running tunnel process."""
    global _ACTIVE_TUNNEL_URL, _TUNNEL_PROC
    if _TUNNEL_PROC is not None and _TUNNEL_PROC.poll() is None:
        return _ACTIVE_TUNNEL_URL
    _ACTIVE_TUNNEL_URL = None
    _TUNNEL_PROC = None
    return None


def stop_cloudflare_tunnel():
    global _ACTIVE_TUNNEL_URL, _TUNNEL_PROC
    proc = _TUNNEL_PROC
    _ACTIVE_TUNNEL_URL = None
    _TUNNEL_PROC = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def start_cloudflare_tunnel(port: int, timeout_sec: float = 15) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """CLI-owned startup, bounded by a deadline; persisted URLs are never trusted."""
    global _ACTIVE_TUNNEL_URL, _TUNNEL_PROC
    if get_public_tunnel_url():
        return _TUNNEL_PROC, _ACTIVE_TUNNEL_URL
    binary = shutil.which('cloudflared')
    if not binary:
        return None, None
    try:
        proc = subprocess.Popen([binary,'tunnel','--url',f'http://127.0.0.1:{port}'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    except OSError:
        return None,None
    _TUNNEL_PROC = proc
    ready = threading.Event()
    def drain():
        global _ACTIVE_TUNNEL_URL
        try:
            for line in proc.stdout:
                match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)',line)
                if match and _TUNNEL_PROC is proc:
                    _ACTIVE_TUNNEL_URL = match.group(1)
                    ready.set()
        finally:
            ready.set()
    threading.Thread(target=drain,daemon=True).start()
    if not ready.wait(timeout_sec) or proc.poll() is not None or not _ACTIVE_TUNNEL_URL:
        stop_cloudflare_tunnel()
        return None,None
    return proc,_ACTIVE_TUNNEL_URL
