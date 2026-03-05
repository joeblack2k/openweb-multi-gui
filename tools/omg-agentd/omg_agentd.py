#!/usr/bin/env python3
import asyncio
import json
import os
import pathlib
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


@dataclass
class SessionState:
    agent_id: str
    title: str
    workdir_path: str
    codex_session_id: Optional[str] = None
    busy: bool = False
    seq: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _hostname() -> str:
    return os.uname().nodename


def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    return 0


class OMGAgentd:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.gateway_ws_url = cfg["gateway_ws_url"]
        self.vm_id = cfg["vm_id"]
        self.token = cfg["token"]
        self.vm_name = cfg.get("vm_name") or _hostname()
        self.codex_model = cfg.get("codex_model", "gpt-5.3-codex")
        self.base_workdir = pathlib.Path(
            cfg.get("base_workdir", str(pathlib.Path.home() / "omg-workspaces"))
        )

        app_server_cfg = cfg.get("codex_app_server", {})
        self.app_server_enabled = bool(app_server_cfg.get("enabled", True))
        self.app_server_listen = app_server_cfg.get("listen", "ws://127.0.0.1:8787")
        self.app_server_process: Optional[asyncio.subprocess.Process] = None

        self.sessions: dict[str, SessionState] = {}
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self._app_server_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def send(self, message: dict[str, Any]):
        if self.ws is None:
            return
        async with self.send_lock:
            await self.ws.send(json.dumps(message))

    async def send_error(
        self, code: str, message: str, request_id: Optional[str] = None, agent_id: Optional[str] = None
    ):
        payload = {"code": code, "message": message}
        if agent_id:
            payload["agent_id"] = agent_id
        await self.send(
            {
                "type": "ERROR",
                "request_id": request_id,
                "payload": payload,
            }
        )

    async def send_agent_event(self, session: SessionState, event_type: str, data: dict[str, Any]):
        session.seq += 1
        await self.send(
            {
                "type": "AGENT_EVENT",
                "payload": {
                    "agent_id": session.agent_id,
                    "event_type": event_type,
                    "data": data,
                    "seq": session.seq,
                },
            }
        )

    async def send_agent_closed(self, agent_id: str, reason: str):
        await self.send({"type": "AGENT_CLOSED", "payload": {"agent_id": agent_id, "reason": reason}})

    def _extract_session_id(self, obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            for key in ("session_id", "sessionId", "conversation_id", "thread_id"):
                value = obj.get(key)
                if isinstance(value, str) and UUID_RE.match(value):
                    return value
            for value in obj.values():
                session_id = self._extract_session_id(value)
                if session_id:
                    return session_id
        elif isinstance(obj, list):
            for item in obj:
                session_id = self._extract_session_id(item)
                if session_id:
                    return session_id
        return None

    async def _run_codex(self, session: SessionState, content: str):
        if session.busy:
            await self.send_error(
                code="AGENT_BUSY",
                message="Agent is already processing another request.",
                agent_id=session.agent_id,
            )
            return

        session.busy = True
        out_file = pathlib.Path("/tmp") / f"{session.agent_id}-last-message.txt"
        if out_file.exists():
            out_file.unlink(missing_ok=True)

        if session.codex_session_id:
            cmd = [
                "codex",
                "exec",
                "resume",
                session.codex_session_id,
                content,
                "--json",
                "--output-last-message",
                str(out_file),
                "--skip-git-repo-check",
                "-m",
                self.codex_model,
            ]
        else:
            cmd = [
                "codex",
                "exec",
                content,
                "--json",
                "--output-last-message",
                str(out_file),
                "--skip-git-repo-check",
                "-C",
                session.workdir_path,
                "-m",
                self.codex_model,
            ]

        await self.send_agent_event(
            session,
            "run.started",
            {"command": cmd, "started_at": int(time.time())},
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    continue

                parsed: dict[str, Any] | None = None
                try:
                    parsed = json.loads(line_text)
                except Exception:
                    parsed = {"raw": line_text}

                discovered_session = self._extract_session_id(parsed)
                if discovered_session and discovered_session != session.codex_session_id:
                    session.codex_session_id = discovered_session
                    await self.send_agent_event(
                        session,
                        "session.info",
                        {"codex_session_id": discovered_session},
                    )

                await self.send_agent_event(session, "run.event", parsed)

            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
            returncode = await proc.wait()

            if out_file.exists():
                final_message = out_file.read_text(encoding="utf-8", errors="replace").strip()
                if final_message:
                    await self.send_agent_event(
                        session,
                        "assistant.message",
                        {"content": final_message},
                    )

            if returncode != 0:
                await self.send_agent_event(
                    session,
                    "run.error",
                    {"returncode": returncode, "stderr": stderr},
                )
            else:
                await self.send_agent_event(
                    session,
                    "run.completed",
                    {"returncode": returncode},
                )
        finally:
            session.busy = False

    async def _handle_start_agent(self, message: dict[str, Any]):
        payload = message.get("payload", {})
        request_id = message.get("request_id")
        agent_id = payload.get("agent_id")
        title = payload.get("title", "Agent")
        workdir_path = payload.get("workdir_path", "workspace")
        reuse_existing = payload.get("reuse_existing")

        if not agent_id:
            await self.send_error("INVALID_PAYLOAD", "Missing agent_id", request_id=request_id)
            return

        if not os.path.isabs(workdir_path):
            target = self.base_workdir / workdir_path
        else:
            target = pathlib.Path(workdir_path)

        if target.exists() and not reuse_existing:
            await self.send_error(
                "WORKDIR_EXISTS",
                f"Workdir exists: {target}",
                request_id=request_id,
                agent_id=agent_id,
            )
            return

        target.mkdir(parents=True, exist_ok=True)
        session = SessionState(
            agent_id=agent_id,
            title=title,
            workdir_path=str(target),
        )
        self.sessions[agent_id] = session

        await self.send_agent_event(
            session,
            "session.started",
            {
                "workdir_path": str(target),
                "reused": bool(target.exists() and reuse_existing),
            },
        )

    async def _handle_send_message(self, message: dict[str, Any]):
        payload = message.get("payload", {})
        request_id = message.get("request_id")
        agent_id = payload.get("agent_id")
        content = payload.get("content", "")

        session = self.sessions.get(agent_id)
        if session is None:
            await self.send_error(
                "UNKNOWN_AGENT",
                f"Unknown agent_id: {agent_id}",
                request_id=request_id,
                agent_id=agent_id,
            )
            return

        asyncio.create_task(self._run_codex(session, content))

    async def _handle_close_agent(self, message: dict[str, Any]):
        payload = message.get("payload", {})
        agent_id = payload.get("agent_id")
        if not agent_id:
            return
        self.sessions.pop(agent_id, None)
        await self.send_agent_closed(agent_id, "closed_by_gateway")

    async def _handle_message(self, message: dict[str, Any]):
        msg_type = message.get("type")

        if msg_type == "START_AGENT":
            await self._handle_start_agent(message)
        elif msg_type == "SEND_MESSAGE":
            await self._handle_send_message(message)
        elif msg_type == "CLOSE_AGENT":
            await self._handle_close_agent(message)
        elif msg_type == "PING":
            await self.send(
                {
                    "type": "PONG",
                    "request_id": message.get("request_id"),
                    "payload": {"ts": int(time.time())},
                }
            )

    async def _heartbeat_loop(self):
        while not self.shutdown_event.is_set():
            payload = {
                "vm_id": self.vm_id,
                "cpu": None,
                "mem_free_mb": _mem_available_mb(),
                "loadavg": list(os.getloadavg()),
                "open_sessions": len(self.sessions),
            }
            try:
                await self.send({"type": "HEARTBEAT", "payload": payload})
            except Exception:
                return
            await asyncio.sleep(10)

    async def _app_server_loop(self):
        while not self.shutdown_event.is_set():
            if not self.app_server_enabled:
                await asyncio.sleep(10)
                continue

            if self.app_server_process is None or self.app_server_process.returncode is not None:
                cmd = ["codex", "app-server", "--listen", self.app_server_listen]
                self.app_server_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            await asyncio.sleep(5)

    async def _connect_loop(self):
        backoff = 1
        while not self.shutdown_event.is_set():
            try:
                async with websockets.connect(self.gateway_ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    backoff = 1

                    hello = {
                        "type": "HELLO",
                        "request_id": str(int(time.time())),
                        "payload": {
                            "vm_id": self.vm_id,
                            "vm_hostname": self.vm_name,
                            "token": self.token,
                            "capabilities": {
                                "codex_exec": True,
                                "codex_app_server": self.app_server_enabled,
                            },
                            "codex_version": self._codex_version(),
                        },
                    }
                    await self.send(hello)

                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_message(message)

            except Exception as e:
                print(f"[omg-agentd] gateway connection error: {e}", file=sys.stderr)
            finally:
                self.ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    def _codex_version(self) -> str:
        return os.popen("codex --version 2>/dev/null").read().strip() or "unknown"

    async def run(self):
        self.base_workdir.mkdir(parents=True, exist_ok=True)
        self._app_server_task = asyncio.create_task(self._app_server_loop())
        await self._connect_loop()

    async def close(self):
        self.shutdown_event.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._app_server_task:
            self._app_server_task.cancel()
        if self.app_server_process and self.app_server_process.returncode is None:
            self.app_server_process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.app_server_process.wait(), timeout=5)
            except Exception:
                self.app_server_process.kill()


async def _main():
    config_path = os.environ.get(
        "OMG_AGENTD_CONFIG", str(pathlib.Path.home() / ".config/omg-agentd/config.json")
    )
    cfg = _read_json(pathlib.Path(config_path))
    agentd = OMGAgentd(cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(agentd.run())
    await stop_event.wait()
    await agentd.close()
    runner.cancel()


if __name__ == "__main__":
    asyncio.run(_main())
