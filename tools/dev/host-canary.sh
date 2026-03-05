#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/.omg-canary"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}"

BACKEND_PORT="${OMG_BACKEND_PORT:-8080}"
FRONTEND_PORT="${OMG_FRONTEND_PORT:-5173}"
CORS_ALLOW_ORIGIN_VALUE="${OMG_CORS_ALLOW_ORIGIN:-*}"

BACKEND_PID_FILE="${RUNTIME_DIR}/backend.pid"
FRONTEND_PID_FILE="${RUNTIME_DIR}/frontend.pid"
BACKEND_LOG="${RUNTIME_DIR}/backend.log"
FRONTEND_LOG="${RUNTIME_DIR}/frontend.log"

usage() {
  cat <<EOF
Usage: $(basename "$0") <bootstrap|start|stop|restart|status|logs>

Environment overrides:
  OMG_BACKEND_PORT   Backend port (default: 8080)
  OMG_FRONTEND_PORT  Frontend dev port (default: 5173)
  OMG_CORS_ALLOW_ORIGIN CORS allow origin value (default: *)
  OMG_SKIP_BOOTSTRAP Set to 1 to skip dependency/bootstrap checks
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] Missing command: $cmd" >&2
    exit 1
  fi
}

check_node_version() {
  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if (( node_major < 18 || node_major > 22 )); then
    echo "[ERROR] Unsupported Node.js major version: ${node_major}. Use Node 18-22." >&2
    exit 1
  fi
}

ensure_runtime_dir() {
  mkdir -p "$RUNTIME_DIR"
}

pid_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  tr -d '[:space:]' <"$pid_file"
}

cleanup_stale_pid() {
  local pid_file="$1"
  local pid=""
  if pid="$(read_pid "$pid_file")"; then
    if ! pid_running "$pid"; then
      rm -f "$pid_file"
    fi
  fi
}

bootstrap_backend() {
  require_cmd python3
  if [[ ! -x "${BACKEND_DIR}/.venv/bin/python" ]]; then
    echo "[INFO] Creating backend venv"
    python3 -m venv "${BACKEND_DIR}/.venv"
  fi

  echo "[INFO] Ensuring backend dependencies"
  "${BACKEND_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
  "${BACKEND_DIR}/.venv/bin/pip" install -r "${BACKEND_DIR}/requirements.txt" >/dev/null
}

bootstrap_frontend() {
  require_cmd npm
  require_cmd node
  check_node_version
  if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
    echo "[INFO] Installing frontend dependencies"
    (cd "$FRONTEND_DIR" && npm ci)
  fi
}

bootstrap_all() {
  ensure_runtime_dir
  bootstrap_backend
  bootstrap_frontend
}

start_backend() {
  cleanup_stale_pid "$BACKEND_PID_FILE"
  local pid=""
  if pid="$(read_pid "$BACKEND_PID_FILE")" && pid_running "$pid"; then
    echo "[INFO] Backend already running (pid=$pid)"
    return 0
  fi

  echo "[INFO] Starting backend on :${BACKEND_PORT}"
  (
    cd "$BACKEND_DIR"
    nohup bash -lc "
      source .venv/bin/activate
      export ENV=dev
      export PORT=${BACKEND_PORT}
      export CORS_ALLOW_ORIGIN='${CORS_ALLOW_ORIGIN_VALUE}'
      python -m uvicorn open_webui.main:app --host 0.0.0.0 --port ${BACKEND_PORT} --forwarded-allow-ips '*' --workers 1
    " >>"$BACKEND_LOG" 2>&1 &
    echo $! >"$BACKEND_PID_FILE"
  )

  sleep 1
  if ! pid_running "$(read_pid "$BACKEND_PID_FILE" 2>/dev/null || true)"; then
    echo "[ERROR] Backend failed to start. Check log: $BACKEND_LOG" >&2
    tail -n 40 "$BACKEND_LOG" >&2 || true
    exit 1
  fi
}

start_frontend() {
  require_cmd node
  check_node_version
  cleanup_stale_pid "$FRONTEND_PID_FILE"
  local pid=""
  if pid="$(read_pid "$FRONTEND_PID_FILE")" && pid_running "$pid"; then
    echo "[INFO] Frontend already running (pid=$pid)"
    return 0
  fi

  echo "[INFO] Starting frontend on :${FRONTEND_PORT}"
  (
    cd "$FRONTEND_DIR"
    nohup bash -lc "npm run dev -- --port ${FRONTEND_PORT}" >>"$FRONTEND_LOG" 2>&1 &
    echo $! >"$FRONTEND_PID_FILE"
  )

  sleep 1
  if ! pid_running "$(read_pid "$FRONTEND_PID_FILE" 2>/dev/null || true)"; then
    echo "[ERROR] Frontend failed to start. Check log: $FRONTEND_LOG" >&2
    tail -n 40 "$FRONTEND_LOG" >&2 || true
    exit 1
  fi
}

stop_one() {
  local name="$1"
  local pid_file="$2"
  local pid=""
  if ! pid="$(read_pid "$pid_file")"; then
    echo "[INFO] ${name} not running"
    return 0
  fi

  if ! pid_running "$pid"; then
    rm -f "$pid_file"
    echo "[INFO] ${name} stale pid removed"
    return 0
  fi

  echo "[INFO] Stopping ${name} (pid=$pid)"
  kill "$pid" >/dev/null 2>&1 || true
  sleep 1
  if pid_running "$pid"; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$pid_file"
}

show_status() {
  local bpid="" fpid=""
  local bstate="stopped" fstate="stopped"

  cleanup_stale_pid "$BACKEND_PID_FILE"
  cleanup_stale_pid "$FRONTEND_PID_FILE"

  if bpid="$(read_pid "$BACKEND_PID_FILE")" && pid_running "$bpid"; then
    bstate="running (pid=$bpid)"
  fi
  if fpid="$(read_pid "$FRONTEND_PID_FILE")" && pid_running "$fpid"; then
    fstate="running (pid=$fpid)"
  fi

  echo "Backend  : ${bstate}  http://127.0.0.1:${BACKEND_PORT}"
  echo "Frontend : ${fstate}  http://127.0.0.1:${FRONTEND_PORT}"
  echo "Logs     : ${BACKEND_LOG} | ${FRONTEND_LOG}"
}

tail_logs() {
  ensure_runtime_dir
  touch "$BACKEND_LOG" "$FRONTEND_LOG"
  tail -f "$BACKEND_LOG" "$FRONTEND_LOG"
}

cmd="${1:-}"
case "$cmd" in
  bootstrap)
    bootstrap_all
    echo "[OK] Bootstrap complete"
    ;;
  start)
    ensure_runtime_dir
    if [[ "${OMG_SKIP_BOOTSTRAP:-0}" != "1" ]]; then
      bootstrap_all
    fi
    start_backend
    start_frontend
    show_status
    ;;
  stop)
    stop_one "frontend" "$FRONTEND_PID_FILE"
    stop_one "backend" "$BACKEND_PID_FILE"
    show_status
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    show_status
    ;;
  logs)
    tail_logs
    ;;
  *)
    usage
    exit 1
    ;;
esac
