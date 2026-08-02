# ecoSeek Ollama Proxy

**HPC Ollama tunnel monitor, health-check API, and inference proxy for free `deepseek-r1:14b` on KU HPC Q6000 GPUs.**

Part of the [ecoSeek](https://ecoseek.org) ecosystem. Runs on `reumanlab-alpha` (100.123.27.68), monitors SSH tunnels from `reumanlab` to `kuhpc`, and provides a single stable endpoint for all ecoSeek services to access free LLM inference.

## Architecture

```
KU HPC (7-9 nodes, Q6000 GPUs, deepseek-r1:14b via Ollama)
  │
  │ SSH tunnels (reumanlab, 100.100.245.62)
  │   :19997 → rXrXn01:XXXXX
  │   :19998 → rYrYn01:YYYYY
  │   :19999 → rZrZn01:ZZZZZ
  │
  ▼ Tailscale mesh
reumanlab-alpha (100.123.27.68)
  │
  ├─ ecoseek-ollama-proxy (:8500)
  │   ├─ GET  /health               → tunnel status
  │   ├─ GET  /v1/models            → proxy to healthy tunnel
  │   └─ POST /v1/chat/completions  → proxy (round-robin)
  │
  └─ Background checker (every 60s)
      └─ All dead >5min → alert file (~/.cache/ollama-proxy-alert.json)
```

## API

### `GET /health`

```json
{
  "service": "ecoseek-ollama-proxy",
  "healthy": true,
  "tunnels_healthy": 3,
  "tunnels_total": 3,
  "all_dead_duration_seconds": 0,
  "tunnel_host": "100.100.245.62",
  "tunnels": {
    "19997": {"healthy": true, "model": "deepseek-r1:14b", "last_check": 1722555320.1},
    "19998": {"healthy": true, "model": "deepseek-r1:14b", "last_check": 1722555320.3},
    "19999": {"healthy": true, "model": "deepseek-r1:14b", "last_check": 1722555320.2}
  }
}
```

Returns HTTP 503 when all tunnels are dead.

### `GET /v1/models`

Proxied from a healthy Ollama tunnel (OpenAI-compatible model list).

### `POST /v1/chat/completions`

Proxied with automatic round-robin across healthy tunnels. Full OpenAI-compatible chat completions API.

```bash
curl -s -X POST http://100.123.27.68:8500/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-r1:14b",
    "messages": [{"role": "user", "content": "Explain photosynthesis briefly"}],
    "max_tokens": 200
  }'
```

**Important:** `deepseek-r1:14b` is a reasoning model. Set `max_tokens >= 150` to leave room for both reasoning and visible content. Low `max_tokens` values result in empty `content` (all tokens consumed by reasoning).

## Deployment

### On reumanlab-alpha

```bash
# Clone
git clone https://github.com/alrobles/ecoseek-ollama-proxy.git ~/dev/ecoseek-ollama-proxy

# Install systemd user service
mkdir -p ~/.config/systemd/user
cp ~/dev/ecoseek-ollama-proxy/ecoseek-ollama-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ecoseek-ollama-proxy

# Verify
curl http://127.0.0.1:8500/health
```

### Expose via Cloudflare Tunnel (optional)

Add to `/etc/cloudflared/config.yml` on the tunnel host:

```yaml
  - hostname: ollama.ecoseek.org
    service: http://100.123.27.68:8500
```

## Monitoring & Alerting

### Cron watchdog for dead-tunnel alert

On alpha, add a cron job to check the alert file:

```bash
# Every 5 minutes
hermes cron create \
  --name ollama-dead-alert \
  --schedule "every 5m" \
  --script ollama-dead-alert.sh \
  --no-agent
```

Where `ollama-dead-alert.sh`:
```bash
#!/bin/bash
ALERT_FILE="$HOME/.cache/ollama-proxy-alert.json"
if [ -f "$ALERT_FILE" ]; then
  echo "⚠️ ALL OLLAMA TUNNELS DEAD"
  cat "$ALERT_FILE"
else
  echo "✓ Ollama tunnels OK"
fi
```

### Hermes config for free inference

On any machine, configure the proxy as a custom provider:

```yaml
# ~/.hermes/config.yaml
custom_providers:
  hpc-ollama-proxy:
    api_key: ollama
    api_mode: chat_completions
    base_url: http://100.123.27.68:8500/v1
    default_model: deepseek-r1:14b
```

Use in Hermes: `custom:hpc-ollama-proxy/deepseek-r1:14b`

## Related Services

| Service | Endpoint | Purpose |
|---------|----------|---------|
| HPC Ollama tunnels | reumanlab :19997-19999 | SSH tunnels to KU HPC Ollama nodes |
| Tunnel watchdog | cron on reumanlab (every 5 min) | Rebuilds stale tunnels |
| Job governor | cron on reumanlab (every 10 min) | Maintains 9 Ollama jobs on HPC |
| ecoSeek Ollama Proxy | alpha :8500 | This service — monitor + proxy |

## Troubleshooting

### All tunnels dead

```bash
# Check proxy health
curl http://127.0.0.1:8500/health

# Check tunnels directly
for port in 19997 19998 19999; do
  curl -s --max-time 3 http://100.100.245.62:$port/v1/models | head -c 100
done

# Run watchdog manually on reumanlab
ssh reumanlab 'bash ~/.hermes/scripts/hpc-ollama-tunnel-watchdog.sh'

# Check HPC jobs
ssh kuhpc 'squeue -u a474r867 -n ollama-r1-14b -t R'
```

### Proxy returns empty content

`deepseek-r1:14b` splits output: `reasoning` (thinking) + `content` (answer). With low `max_tokens`, all tokens go to reasoning. Increase `max_tokens` to ≥150.

### Cannot reach alpha from other machines

The proxy binds to `127.0.0.1:8500` (localhost only). For cross-machine access, either:
- Use Tailscale: `http://100.123.27.68:8500` after changing bind to `0.0.0.0`
- Or expose via Cloudflare Tunnel

## License

MIT — part of the ecoSeek ecosystem.
