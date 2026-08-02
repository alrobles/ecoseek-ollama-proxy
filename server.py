#!/usr/bin/env python3
"""
ecoseek-ollama-proxy — HPC Ollama Tunnel Monitor & Proxy

Monitors SSH tunnels to KU HPC Ollama nodes (deepseek-r1:14b on Q6000 GPUs).
Exposes a health-check API and proxies inference requests to healthy tunnels
with automatic round-robin load balancing.

Endpoints:
    GET  /health              — tunnel status + aggregate health
    GET  /v1/models           — proxy to healthy tunnel
    POST /v1/chat/completions — proxy to healthy tunnel (round-robin)

Port: 8500 (configurable via OLLAMA_PROXY_PORT env var)
"""

import json
import os
import sys
import time
import signal
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ─── Configuration ─────────────────────────────────────────────────────

TUNNEL_HOST = os.environ.get("OLLAMA_TUNNEL_HOST", "100.100.245.62")
TUNNEL_PORTS = [19997, 19998, 19999]
CHECK_INTERVAL = int(os.environ.get("OLLAMA_CHECK_INTERVAL", "60"))  # seconds
ALERT_AFTER = int(os.environ.get("OLLAMA_ALERT_AFTER", "300"))  # 5 min all-dead
PROXY_PORT = int(os.environ.get("OLLAMA_PROXY_PORT", "8500"))
ALERT_FILE = os.environ.get(
    "OLLAMA_ALERT_FILE",
    str(Path.home() / ".cache" / "ollama-proxy-alert.json")
)

# ─── Global state (thread-safe via GIL for simple dict swaps) ──────────

_tunnel_status = {}       # {port: {"healthy": bool, "last_check": float, "model": str}}
_healthy_ports = []       # list of healthy ports, updated atomically
_all_dead_since = None    # timestamp when all tunnels first went dead
_round_robin_idx = 0
_lock = threading.Lock()

# ─── Tunnel checking ───────────────────────────────────────────────────

def check_tunnel(port: int) -> dict:
    """Check a single tunnel. Returns status dict."""
    url = f"http://{TUNNEL_HOST}:{port}/v1/models"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            model = data.get("data", [{}])[0].get("id", "unknown")
            return {"healthy": True, "model": model, "error": None}
    except Exception as e:
        return {"healthy": False, "model": None, "error": str(e)[:200]}


def check_all_tunnels():
    """Check all tunnels, update global state."""
    global _all_dead_since
    now = time.time()

    new_status = {}
    healthy = []

    for port in TUNNEL_PORTS:
        result = check_tunnel(port)
        result["last_check"] = now
        new_status[port] = result
        if result["healthy"]:
            healthy.append(port)

    with _lock:
        _tunnel_status.clear()
        _tunnel_status.update(new_status)
        _healthy_ports[:] = healthy
        _round_robin_idx = 0

        if healthy:
            _all_dead_since = None
            # Clear alert file if it existed
            try:
                os.remove(ALERT_FILE)
            except OSError:
                pass
        else:
            if _all_dead_since is None:
                _all_dead_since = now
            # If all dead for > ALERT_AFTER, write alert file
            if now - _all_dead_since > ALERT_AFTER:
                _write_alert()


def _write_alert():
    """Write alert file for external watchdog/cron consumption."""
    alert = {
        "alert": "ALL_TUNNELS_DEAD",
        "since": _all_dead_since,
        "tunnel_host": TUNNEL_HOST,
        "ports_checked": TUNNEL_PORTS,
        "message": f"All {len(TUNNEL_PORTS)} HPC Ollama tunnels dead for "
                   f"{int(time.time() - _all_dead_since)}s",
        "timestamp": time.time(),
    }
    Path(ALERT_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_FILE, "w") as f:
        json.dump(alert, f, indent=2)


# ─── Background checker thread ─────────────────────────────────────────

def checker_loop():
    """Run check_all_tunnels every CHECK_INTERVAL seconds."""
    while True:
        try:
            check_all_tunnels()
        except Exception as e:
            print(f"[checker] Error: {e}", file=sys.stderr, flush=True)
        time.sleep(CHECK_INTERVAL)


# ─── HTTP Proxy helpers ─────────────────────────────────────────────────

def get_healthy_port():
    """Get next healthy port (round-robin). Returns None if none healthy."""
    with _lock:
        if not _healthy_ports:
            return None
        global _round_robin_idx
        port = _healthy_ports[_round_robin_idx % len(_healthy_ports)]
        _round_robin_idx = (_round_robin_idx + 1) % len(_healthy_ports)
        return port


def proxy_to_ollama(path: str, method: str, body: bytes = None,
                    headers: dict = None) -> tuple[int, str, dict]:
    """Proxy a request to a healthy Ollama tunnel. Returns (status, body, headers)."""
    port = get_healthy_port()
    if port is None:
        return (503, json.dumps({"error": "No healthy Ollama tunnels available"}),
                {"Content-Type": "application/json"})

    url = f"http://{TUNNEL_HOST}:{port}{path}"
    try:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("host", "content-length", "connection"):
                    req.add_header(k, v)

        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_body = resp.read()
            resp_headers = {
                "Content-Type": resp.headers.get("Content-Type", "application/json"),
            }
            return (resp.status, resp_body.decode(), resp_headers)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        return (e.code, err_body, {"Content-Type": "application/json"})
    except Exception as e:
        return (502, json.dumps({"error": f"Tunnel proxy error: {e}"}),
                {"Content-Type": "application/json"})


# ─── HTTP Request Handler ──────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        """Suppress default logging to stderr (too noisy)."""
        pass

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.handle_health()
        elif self.path.startswith("/v1/"):
            self.handle_proxy("GET")
        else:
            self._json_response(404, {"error": "Not found"})

    def do_POST(self):
        if self.path.startswith("/v1/"):
            self.handle_proxy("POST")
        else:
            self._json_response(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def handle_health(self):
        with _lock:
            status_copy = dict(_tunnel_status)
            all_dead_since = _all_dead_since
            healthy = list(_healthy_ports)

        tunnels = {}
        all_healthy = True
        for port in TUNNEL_PORTS:
            s = status_copy.get(port, {"healthy": False, "model": None, "error": "unchecked"})
            tunnels[str(port)] = {
                "healthy": s["healthy"],
                "model": s.get("model"),
                "last_check": s.get("last_check", 0),
            }
            if not s["healthy"]:
                all_healthy = False

        now = time.time()
        dead_duration = (now - all_dead_since) if all_dead_since and not healthy else 0

        self._json_response(200 if healthy else 503, {
            "service": "ecoseek-ollama-proxy",
            "healthy": len(healthy) > 0,
            "tunnels_healthy": len(healthy),
            "tunnels_total": len(TUNNEL_PORTS),
            "all_dead_duration_seconds": round(dead_duration, 1),
            "tunnel_host": TUNNEL_HOST,
            "tunnels": tunnels,
        })

    def handle_proxy(self, method: str):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        req_headers = {}
        for k, v in self.headers.items():
            req_headers[k] = v

        status, resp_body, resp_headers = proxy_to_ollama(
            self.path, method, body, req_headers
        )

        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp_body.encode())


# ─── Signal handling ───────────────────────────────────────────────────

def shutdown(signum, frame):
    print(f"\n[proxy] Shutting down (signal {signum})...", file=sys.stderr, flush=True)
    sys.exit(0)


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Initial check
    print(f"[proxy] Checking {len(TUNNEL_PORTS)} tunnels at {TUNNEL_HOST}...",
          file=sys.stderr, flush=True)
    check_all_tunnels()

    healthy = len(_healthy_ports)
    print(f"[proxy] Tunnels healthy: {healthy}/{len(TUNNEL_PORTS)}",
          file=sys.stderr, flush=True)
    for port, status in _tunnel_status.items():
        icon = "✓" if status["healthy"] else "✗"
        model = status.get("model") or "N/A"
        print(f"  {icon} :{port} → {model}", file=sys.stderr, flush=True)

    # Start background checker
    checker = threading.Thread(target=checker_loop, daemon=True, name="tunnel-checker")
    checker.start()

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", PROXY_PORT), ProxyHandler)
    print(f"\n[proxy] Listening on http://127.0.0.1:{PROXY_PORT}",
          file=sys.stderr, flush=True)
    print(f"[proxy] Endpoints:", file=sys.stderr, flush=True)
    print(f"  GET  http://127.0.0.1:{PROXY_PORT}/health", file=sys.stderr, flush=True)
    print(f"  GET  http://127.0.0.1:{PROXY_PORT}/v1/models", file=sys.stderr, flush=True)
    print(f"  POST http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
          file=sys.stderr, flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
