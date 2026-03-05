# OMG Host-Native Canary Runbook

This runbook is the default development/test workflow.
Use Docker builds only after host-native behavior is tuned and validated.

## Scope

- Starts OpenWebUI backend + frontend directly on the host.
- Keeps OMG gateway/API features available in the same backend process.
- Intended for canary/dev validation.

## Prerequisites

- Python 3.11+
- Node.js 18-22 and npm

## One-command workflow

From repo root:

```bash
./tools/dev/host-canary.sh start
```

This will:

1. Create `backend/.venv` if needed.
2. Install backend requirements.
3. Install frontend dependencies (`npm ci`) if `node_modules` is missing.
4. Start backend on `:8080`.
5. Start frontend dev server on `:5173`.

## Control commands

```bash
./tools/dev/host-canary.sh status
./tools/dev/host-canary.sh logs
./tools/dev/host-canary.sh stop
./tools/dev/host-canary.sh restart
```

## Ports and overrides

Defaults:

- Backend: `8080`
- Frontend: `5173`

Override example:

```bash
OMG_BACKEND_PORT=18080 OMG_FRONTEND_PORT=15173 ./tools/dev/host-canary.sh start
```

Set explicit CORS value (useful when testing from another machine/browser origin):

```bash
OMG_CORS_ALLOW_ORIGIN='*' ./tools/dev/host-canary.sh start
```

Skip dependency bootstrap on restart loops:

```bash
OMG_SKIP_BOOTSTRAP=1 ./tools/dev/host-canary.sh start
```

## Runtime artifacts

- Runtime directory: `.omg-canary/`
- PID files:
  - `.omg-canary/backend.pid`
  - `.omg-canary/frontend.pid`
- Logs:
  - `.omg-canary/backend.log`
  - `.omg-canary/frontend.log`
