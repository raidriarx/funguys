"""LAN IP detection and optional Cloudflare/ngrok tunnel launcher for Host PC."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from typing import Optional, Tuple


def get_lan_ip() -> str:
    """Return the Host PC's IP address, prioritizing Tailscale / ZeroTier mesh overlay IPs if active."""
    # Check for Tailscale (100.64.0.0/10) or ZeroTier virtual adapter IPs
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        for iface_name, snics in addrs.items():
            iface_lower = iface_name.lower()
            if any(k in iface_lower for k in ["tailscale", "zerotier", "zt"]):
                for snic in snics:
                    if snic.family == socket.AF_INET and not snic.address.startswith("127."):
                        return snic.address
        # Also check all IP addresses for standard Tailscale CGNAT range (100.64.0.0 - 100.127.255.255)
        for snics in addrs.values():
            for snic in snics:
                if snic.family == socket.AF_INET and snic.address.startswith("100."):
                    parts = snic.address.split(".")
                    if len(parts) == 4 and 64 <= int(parts[1]) <= 127:
                        return snic.address
    except Exception:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_public_tunnel_url() -> Optional[str]:
    """Check if a Cloudflare Quick Tunnel or Ngrok tunnel is currently active locally."""
    # 1. Cloudflare Quick Tunnel local metrics port (20241)
    try:
        import urllib.request
        import json

        req = urllib.request.Request("http://127.0.0.1:20241/quicktunnel", headers={"User-Agent": "funguys"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            hostname = data.get("hostname")
            if hostname:
                return f"https://{hostname}"
    except Exception:
        pass

    # 2. Ngrok local API (4040)
    try:
        import urllib.request
        import json

        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels", headers={"User-Agent": "funguys"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for tun in data.get("tunnels", []):
                if tun.get("public_url"):
                    return str(tun["public_url"])
    except Exception:
        pass

    return None


def start_cloudflare_tunnel(port: int) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Start a free Cloudflare TryCloudflare tunnel if `cloudflared` is installed."""
    existing_url = get_public_tunnel_url()
    if existing_url:
        return None, existing_url

    cf_bin = shutil.which("cloudflared")
    if not cf_bin:
        return None, None
    try:
        proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.stdout:
            for _ in range(40):
                line = proc.stdout.readline()
                if not line:
                    break
                match = re.search(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)", line)
                if match:
                    return proc, match.group(1)
        return proc, None
    except Exception:
        return None, None

