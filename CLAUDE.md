# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Antigravity Bridge turns Google's free Antigravity desktop app into a REST API via Chrome DevTools Protocol (CDP). It provides access to 6 free AI models (Claude Opus 4.6, Gemini 3.1 Pro, Sonnet 4.6, etc.) through a Python HTTP server that injects prompts into Antigravity's Lexical editor.

## Running

```bash
# Install dependency
pip install websockets

# Start Antigravity with CDP port enabled
bash scripts/start_antigravity.sh

# Or manually: open -a Antigravity --args --remote-debugging-port=9229

# Start the bridge server
python3 scripts/bridge.py [--port 19999] [--cdp-port 9229]
```

## CLI Usage

```bash
# Chat via CLI (ag_chat.sh wraps the HTTP API)
ag "Your question" [opus|gemini|sonnet|flash|gpt] [timeout]

# IDE agent mode (remote SSH + CDP injection)
bash scripts/agy_invoke.sh "Task description" --model opus
```

## Architecture

```
HTTP Client → bridge.py (port 19999) → CDP WebSocket (port 9229) → Antigravity Electron app
                                                           ↓
                                                      language_server_macos_arm
                                                           ↓
                                                      googleapis.com (AI models)
```

### Key Components

**bridge.py** - Single-file Python HTTP server with persistent CDP WebSocket connection
- `Bridge` class: Core CDP wrapper with persistent WebSocket (`_ws`) and async event loop (`_al`)
- `_conn()`: Ensures WebSocket is connected, reuses existing connection
- `_ev()`: Evaluates JS in Antigravity page via CDP `Runtime.evaluate`
- `_cdp()`: Raw CDP method calls (e.g., `Security.setIgnoreCertificateErrors`)
- `_inject_obs()`: Injects MutationObserver for O(1) completion detection (v17-fast optimization)
- `_do_chat()`: Core chat flow — wait_ready → fast_mode → set_model → inject_obs → type_send → poll
- `_reload_page()`: Two-phase reload for `/new` endpoint

**cdp_inject.js** - Node.js script for IDE agent mode, injects prompts via CDP using clipboard events

### Critical: SSL Certificate Fix

Antigravity's `language_server_macos_arm` uses a self-signed SSL cert (`CN=localhost`). Electron's `fetch()` rejects it silently, causing messages to send but never reach the model. The bridge calls `Security.setIgnoreCertificateErrors({ignore: true})` via CDP before each chat.

### Model Names (use exact strings)

```
MODELS = [
    'Gemini 3.1 Pro (High)',
    'Gemini 3.1 Pro (Low)',
    'Gemini 3 Flash',
    'Claude Sonnet 4.6 (Thinking)',
    'Claude Opus 4.6 (Thinking)',
    'GPT-OSS 120B (Medium)'
]
```

## HTTP API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Sync chat (default 180s timeout) |
| POST | `/async` | Async chat, returns `task_id` |
| GET | `/task/{id}` | Poll async result |
| POST | `/new` | New conversation (two-phase reload) |
| POST | `/model` | Switch model |
| GET | `/health` | Health check |
| GET | `/models` | List available models |
| GET | `/history` | Current conversation content |
| GET | `/imgcount` | Count generated images |
| GET | `/extract?after=N` | Extract generated image as base64 |

## Common Operations

**Switch model**: The bridge auto-clicks model selector dropdown then finds and clicks the model name by text content.

**Fast mode**: Auto-switches from "Planning" to "Fast" mode to prevent hangs.

**Completion detection**: Uses MutationObserver watching for "Good"/"Bad" buttons (indicates response complete). Falls back to polling `document.body.innerText` every 15s if observer fails.

**Reload (`/new`)**: Two-phase process — triggers reload on old connection → waits → reconnects to new page. Needed to clear conversation context.

## Performance Optimizations

**Auto-Reload**: The bridge automatically reloads the Antigravity page after 10 messages (`s.mc` counter) to prevent state accumulation. This is transparent to users but occurs during chat operations.

**Adaptive Polling**: The `_poll()` method uses adaptive intervals to minimize CPU usage:
- 0.2s for first 5 seconds (fast response for quick queries)
- 0.4s for 5-30 seconds
- 0.8s for 30-120 seconds
- 1.5s after 120 seconds (long-running tasks)

**MutationObserver (v17-fast)**: O(1) completion detection using `window.__ag.done` flag. Injected observer watches for new "Good"/"Bad" buttons. Falls back to polling `document.body.innerText` every 15s if observer fails.

## Error Handling

**High Traffic Retry**: The `ag_chat.sh` CLI wrapper includes automatic retry logic (up to 3 retries with 15s intervals) for high traffic situations. This is client-side logic; the HTTP API returns `{"status":"high_traffic"}` and leaves retry to the client.

**Connection Recovery**: The bridge maintains persistent WebSocket connections with auto-reconnect on disconnect. CDP watchdog ensures the bridge stays connected to Antigravity.

## Remote SSH Invocation

The `agy_invoke.sh` script enables remote triggering of Antigravity's IDE agent mode via SSH:

**Requirements**:
- Remote Login enabled on Mac (System Settings → General → Sharing → Remote Login)
- Node.js with `ws` package installed on Mac
- Configuration via `~/.remote-mac.conf` or environment variables

**Configuration**:
- `MAC_SSH_HOST` — Mac SSH address (default: mac.local)
- `MAC_SSH_USER` — SSH user (default: current user)
- `MAC_SSH_PORT` — SSH port (default: 22)
- `AGY_NODE_PATH` — npm global modules path

**How it works**: Uploads `cdp_inject.js` to remote Mac, then executes it via SSH to inject prompts directly into Antigravity's Lexical editor using CDP clipboard events.

## Development Notes

**No Test Suite**: This codebase currently has no automated tests. Manual testing is done via:
- CLI: `ag "test prompt" opus`
- HTTP: `curl -X POST http://localhost:19999/chat -d '{"prompt":"test"}'`
- Health check: `curl http://localhost:19999/health`

**Debugging**: Check `/tmp/ag_bridge.log` for bridge server logs when using `start_antigravity.sh`. The bridge prints timestamped chat interactions to stdout.
