import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from open_webui.models.omg import (
    OMGVM,
    OMGAgentEvents,
    OMGAgentEventModel,
    OMGAgentSessions,
    OMGAgentSessionModel,
    OMGVMListItem,
    OMGVms,
)
from open_webui.utils.auth import get_admin_user
from open_webui.internal.db import get_db_context

log = logging.getLogger(__name__)

router = APIRouter()


DEFAULT_COMMAND_POLICY = {
    "approval_mode": "on-request",
    "sandbox_mode": "workspace-write",
    "allowlist": [
        "ls",
        "pwd",
        "cat",
        "df -h",
        "free -m",
        "uptime",
        "journalctl -n",
        "systemctl status",
    ],
    "confirm_required": [
        "apt",
        "dnf",
        "yum",
        "pip",
        "npm",
        "rm",
        "mv",
        "chmod",
        "chown",
        "systemctl restart",
        "docker",
    ],
}


class OMGConnectionManager:
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def register(self, vm_id: str, ws: WebSocket):
        async with self._lock:
            old = self._connections.get(vm_id)
            self._connections[vm_id] = ws
            self._send_locks.setdefault(vm_id, asyncio.Lock())

        if old and old is not ws:
            try:
                await old.close(code=4000, reason="Replaced by new agentd connection")
            except Exception:
                pass

    async def unregister(self, vm_id: str, ws: Optional[WebSocket] = None):
        async with self._lock:
            current = self._connections.get(vm_id)
            if current is None:
                return
            if ws is not None and current is not ws:
                return
            self._connections.pop(vm_id, None)
            self._send_locks.pop(vm_id, None)

    async def send(self, vm_id: str, message: dict[str, Any]) -> bool:
        async with self._lock:
            ws = self._connections.get(vm_id)
            lock = self._send_locks.get(vm_id)

        if ws is None or lock is None:
            return False

        try:
            async with lock:
                await ws.send_json(message)
            return True
        except Exception as e:
            log.warning("Failed sending OMG message to vm_id=%s: %s", vm_id, e)
            return False

    async def publish(self, event: dict[str, Any]):
        stale: list[asyncio.Queue] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                stale.append(queue)

        for queue in stale:
            self._subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subscribers.discard(queue)


gateway = OMGConnectionManager()


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RenameVMForm(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class CreateAgentForm(BaseModel):
    title: str = Field(default="New Agent", min_length=1, max_length=200)
    workdir_name: str = Field(min_length=1, max_length=255)
    reuse_existing: Optional[bool] = None


class AgentMessageForm(BaseModel):
    content: str = Field(min_length=1)


class AgentEventsResponse(BaseModel):
    items: list[OMGAgentEventModel]
    next_cursor: int


@router.get("/health")
async def get_health(user=Depends(get_admin_user)):
    return {"status": "ok", "connected_agents": len(gateway._connections)}


@router.get("/vms", response_model=list[OMGVMListItem])
async def list_vms(user=Depends(get_admin_user)):
    return OMGVms.list_with_open_counts()


@router.patch("/vms/{vm_id}", response_model=OMGVMListItem)
async def rename_vm(vm_id: str, form: RenameVMForm, user=Depends(get_admin_user)):
    vm = OMGVms.update_display_name(vm_id=vm_id, display_name=form.display_name.strip())
    if vm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")

    rows = OMGVms.list_with_open_counts()
    row = next((item for item in rows if item.vm_id == vm_id), None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")

    await gateway.publish({"type": "vm.updated", "payload": row.model_dump()})
    return row


@router.get("/vms/{vm_id}/agents", response_model=list[OMGAgentSessionModel])
async def list_vm_open_agents(vm_id: str, user=Depends(get_admin_user)):
    vm = OMGVms.get_by_id(vm_id)
    if vm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")
    return OMGAgentSessions.list_open_by_vm(vm_id)


@router.post("/vms/{vm_id}/agents", response_model=OMGAgentSessionModel)
async def create_agent(vm_id: str, form: CreateAgentForm, user=Depends(get_admin_user)):
    vm = OMGVms.get_by_id(vm_id)
    if vm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM not found")

    workdir_path = form.workdir_name.strip()
    open_for_workdir = OMGAgentSessions.count_open_for_workdir(vm_id, workdir_path)
    if open_for_workdir > 0 and form.reuse_existing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "WORKDIR_EXISTS",
                "message": "A running agent already uses this workdir on the selected VM.",
                "workdir_name": workdir_path,
            },
        )

    session = OMGAgentSessions.create(
        vm_id=vm_id,
        title=form.title.strip(),
        workdir_path=workdir_path,
        policy=DEFAULT_COMMAND_POLICY,
        meta={"reuse_existing": form.reuse_existing},
    )
    OMGAgentEvents.add(
        session.agent_id,
        "session.created",
        {
            "vm_id": vm_id,
            "title": session.title,
            "workdir_path": workdir_path,
        },
    )

    request_id = str(uuid.uuid4())
    sent = await gateway.send(
        vm_id,
        {
            "type": "START_AGENT",
            "request_id": request_id,
            "payload": {
                "agent_id": session.agent_id,
                "title": session.title,
                "workdir_path": workdir_path,
                "policy": DEFAULT_COMMAND_POLICY,
                "reuse_existing": form.reuse_existing,
            },
        },
    )
    if not sent:
        OMGAgentEvents.add(
            session.agent_id,
            "gateway.notice",
            {"message": "VM is currently offline. Command will be sent after reconnect."},
        )

    await gateway.publish(
        {"type": "agent.created", "payload": OMGAgentSessionModel.model_validate(session).model_dump()}
    )
    return session


@router.post("/agents/{agent_id}/messages")
async def send_agent_message(agent_id: str, form: AgentMessageForm, user=Depends(get_admin_user)):
    session = OMGAgentSessions.get(agent_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if session.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is archived")

    OMGAgentEvents.add(
        agent_id,
        "user.message",
        {"content": form.content},
    )

    sent = await gateway.send(
        session.vm_id,
        {
            "type": "SEND_MESSAGE",
            "request_id": str(uuid.uuid4()),
            "payload": {"agent_id": agent_id, "content": form.content},
        },
    )
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Target VM is offline",
        )

    await gateway.publish(
        {
            "type": "agent.message.sent",
            "payload": {"agent_id": agent_id, "vm_id": session.vm_id},
        }
    )
    return {"ok": True}


@router.post("/agents/{agent_id}/close", response_model=OMGAgentSessionModel)
async def close_agent(agent_id: str, user=Depends(get_admin_user)):
    session = OMGAgentSessions.get(agent_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if session.status == "archived":
        return session

    closed = OMGAgentSessions.close(agent_id, reason="closed_by_user")
    if closed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    await gateway.send(
        closed.vm_id,
        {
            "type": "CLOSE_AGENT",
            "request_id": str(uuid.uuid4()),
            "payload": {"agent_id": closed.agent_id},
        },
    )

    OMGAgentEvents.add(
        closed.agent_id,
        "session.archived",
        {"reason": "closed_by_user"},
    )
    await gateway.publish({"type": "agent.closed", "payload": closed.model_dump()})
    return closed


@router.get("/agents/{agent_id}/events", response_model=AgentEventsResponse)
async def get_agent_events(
    agent_id: str, cursor: int = 0, limit: int = 200, user=Depends(get_admin_user)
):
    session = OMGAgentSessions.get(agent_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    items = OMGAgentEvents.list_after_cursor(agent_id=agent_id, cursor=cursor, limit=min(limit, 500))
    next_cursor = cursor
    if items:
        next_cursor = max(item.id for item in items)

    return AgentEventsResponse(items=items, next_cursor=next_cursor)


@router.get("/stream")
async def omg_stream(request: Request, user=Depends(get_admin_user)):
    queue = gateway.subscribe()

    async def event_generator():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            gateway.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.websocket("/agentd/ws")
async def omg_agentd_ws(ws: WebSocket):
    await ws.accept()
    vm_id = None

    try:
        hello_message = await asyncio.wait_for(ws.receive_json(), timeout=15)
        if hello_message.get("type") != "HELLO":
            await ws.close(code=4400, reason="Expected HELLO as first message")
            return

        payload = hello_message.get("payload", {})
        vm_id = payload.get("vm_id")
        vm_hostname = payload.get("vm_hostname")
        token = payload.get("token")
        capabilities = payload.get("capabilities") or {}
        codex_version = payload.get("codex_version")

        if not vm_id or not token:
            await ws.close(code=4401, reason="Missing vm_id or token")
            return

        token_hash = hash_token(token)
        with get_db_context() as db:
            vm_row = db.get(OMGVM, vm_id)
            if vm_row is not None and vm_row.auth_token_hash != token_hash:
                await ws.close(code=4403, reason="Invalid token")
                return

        vm = OMGVms.upsert_from_hello(
            vm_id=vm_id,
            auth_token_hash=token_hash,
            vm_hostname=vm_hostname,
            capabilities=capabilities,
            codex_version=codex_version,
        )
        await gateway.register(vm_id, ws)

        await ws.send_json(
            {
                "type": "HELLO_ACK",
                "request_id": hello_message.get("request_id"),
                "payload": {"vm_id": vm_id},
            }
        )
        await gateway.publish({"type": "vm.online", "payload": vm.model_dump()})

        while True:
            message = await ws.receive_json()
            msg_type = message.get("type")
            msg_payload = message.get("payload", {})

            if msg_type == "HEARTBEAT":
                vm = OMGVms.update_heartbeat(vm_id, msg_payload or {})
                if vm:
                    await gateway.publish({"type": "vm.heartbeat", "payload": vm.model_dump()})

            elif msg_type == "AGENT_EVENT":
                agent_id = msg_payload.get("agent_id")
                event_type = msg_payload.get("event_type")
                data = msg_payload.get("data") or {}
                seq = msg_payload.get("seq")

                if not agent_id or not event_type:
                    continue

                event = OMGAgentEvents.add(
                    agent_id=agent_id,
                    event_type=event_type,
                    payload=data,
                    seq=seq,
                )

                codex_session_id = data.get("codex_session_id")
                if codex_session_id:
                    OMGAgentSessions.update_meta(agent_id, {"codex_session_id": codex_session_id})

                await gateway.publish(
                    {
                        "type": "agent.event",
                        "payload": {
                            "agent_id": agent_id,
                            "event": event.model_dump(),
                        },
                    }
                )

            elif msg_type == "AGENT_CLOSED":
                agent_id = msg_payload.get("agent_id")
                reason = msg_payload.get("reason", "closed")
                if not agent_id:
                    continue

                session = OMGAgentSessions.close(agent_id, reason=reason)
                if session:
                    OMGAgentEvents.add(
                        agent_id=agent_id,
                        event_type="session.archived",
                        payload={"reason": reason},
                    )
                    await gateway.publish(
                        {"type": "agent.closed", "payload": session.model_dump()}
                    )

            elif msg_type == "ERROR":
                agent_id = msg_payload.get("agent_id")
                if agent_id:
                    OMGAgentEvents.add(
                        agent_id=agent_id,
                        event_type="agent.error",
                        payload=msg_payload,
                    )
                await gateway.publish({"type": "agent.error", "payload": msg_payload})

            elif msg_type == "PING":
                await ws.send_json(
                    {"type": "PONG", "request_id": message.get("request_id"), "payload": {}}
                )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("omg agentd websocket failure: %s", e)
        try:
            await ws.close(code=1011, reason="internal error")
        except Exception:
            pass
    finally:
        if vm_id:
            await gateway.unregister(vm_id, ws)
            OMGVms.set_offline(vm_id)
            await gateway.publish({"type": "vm.offline", "payload": {"vm_id": vm_id}})
