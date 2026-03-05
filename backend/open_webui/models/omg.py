import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    and_,
    desc,
    func,
)
from sqlalchemy.orm import Session

from open_webui.internal.db import Base, get_db_context

log = logging.getLogger(__name__)


class OMGVM(Base):
    __tablename__ = "omg_vm"

    vm_id = Column(String, primary_key=True, unique=True)
    display_name = Column(Text, nullable=False)
    vm_hostname = Column(Text, nullable=True)

    auth_token_hash = Column(Text, nullable=False)
    capabilities = Column(JSON, nullable=False, server_default="{}")
    codex_version = Column(Text, nullable=True)

    last_seen_at = Column(BigInteger, nullable=True)
    last_heartbeat = Column(JSON, nullable=False, server_default="{}")
    is_online = Column(Boolean, nullable=False, default=False)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_omg_vm_last_seen_at", "last_seen_at"),
        Index("idx_omg_vm_is_online", "is_online"),
    )


class OMGAgentSession(Base):
    __tablename__ = "omg_agent_session"

    agent_id = Column(String, primary_key=True, unique=True)
    vm_id = Column(String, ForeignKey("omg_vm.vm_id", ondelete="CASCADE"), nullable=False)

    title = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="open")
    workdir_path = Column(Text, nullable=False)

    policy = Column(JSON, nullable=False, server_default="{}")
    meta = Column(JSON, nullable=False, server_default="{}")

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    closed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("idx_omg_agent_session_vm_status", "vm_id", "status"),
        Index("idx_omg_agent_session_updated_at", "updated_at"),
    )


class OMGAgentEvent(Base):
    __tablename__ = "omg_agent_event"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    agent_id = Column(
        String, ForeignKey("omg_agent_session.agent_id", ondelete="CASCADE"), nullable=False
    )
    seq = Column(BigInteger, nullable=True)
    event_type = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, server_default="{}")
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_omg_agent_event_agent_id_id", "agent_id", "id"),
        Index("idx_omg_agent_event_created_at", "created_at"),
    )


class OMGVMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    vm_id: str
    display_name: str
    vm_hostname: Optional[str] = None
    capabilities: dict = {}
    codex_version: Optional[str] = None
    last_seen_at: Optional[int] = None
    last_heartbeat: dict = {}
    is_online: bool = False
    created_at: int
    updated_at: int


class OMGVMListItem(OMGVMModel):
    open_agents: int = 0


class OMGAgentSessionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    vm_id: str
    title: str
    status: str
    workdir_path: str
    policy: dict = {}
    meta: dict = {}
    created_at: int
    updated_at: int
    closed_at: Optional[int] = None


class OMGAgentEventModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: str
    seq: Optional[int] = None
    event_type: str
    payload: dict = {}
    created_at: int


class OMGVms:
    @staticmethod
    def list_with_open_counts(db: Optional[Session] = None) -> list[OMGVMListItem]:
        with get_db_context(db) as db:
            rows = (
                db.query(
                    OMGVM,
                    func.count(OMGAgentSession.agent_id).label("open_agents"),
                )
                .outerjoin(
                    OMGAgentSession,
                    and_(
                        OMGAgentSession.vm_id == OMGVM.vm_id,
                        OMGAgentSession.status == "open",
                    ),
                )
                .group_by(OMGVM.vm_id)
                .order_by(desc(OMGVM.last_seen_at), OMGVM.display_name)
                .all()
            )

            return [
                OMGVMListItem(
                    **OMGVMModel.model_validate(vm).model_dump(),
                    open_agents=int(open_agents or 0),
                )
                for vm, open_agents in rows
            ]

    @staticmethod
    def get_by_id(vm_id: str, db: Optional[Session] = None) -> Optional[OMGVMModel]:
        with get_db_context(db) as db:
            vm = db.get(OMGVM, vm_id)
            if vm is None:
                return None
            return OMGVMModel.model_validate(vm)

    @staticmethod
    def upsert_from_hello(
        vm_id: str,
        auth_token_hash: str,
        vm_hostname: Optional[str] = None,
        capabilities: Optional[dict] = None,
        codex_version: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> OMGVMModel:
        with get_db_context(db) as db:
            now = int(time.time())
            vm = db.get(OMGVM, vm_id)
            if vm is None:
                vm = OMGVM(
                    vm_id=vm_id,
                    display_name=vm_hostname or vm_id,
                    vm_hostname=vm_hostname,
                    auth_token_hash=auth_token_hash,
                    capabilities=capabilities or {},
                    codex_version=codex_version,
                    last_seen_at=now,
                    last_heartbeat={},
                    is_online=True,
                    created_at=now,
                    updated_at=now,
                )
                db.add(vm)
            else:
                vm.vm_hostname = vm_hostname or vm.vm_hostname
                vm.capabilities = capabilities or vm.capabilities or {}
                vm.codex_version = codex_version or vm.codex_version
                vm.last_seen_at = now
                vm.is_online = True
                vm.updated_at = now

            db.commit()
            db.refresh(vm)
            return OMGVMModel.model_validate(vm)

    @staticmethod
    def set_offline(vm_id: str, db: Optional[Session] = None) -> None:
        with get_db_context(db) as db:
            vm = db.get(OMGVM, vm_id)
            if vm is None:
                return
            vm.is_online = False
            vm.updated_at = int(time.time())
            db.commit()

    @staticmethod
    def update_display_name(
        vm_id: str, display_name: str, db: Optional[Session] = None
    ) -> Optional[OMGVMModel]:
        with get_db_context(db) as db:
            vm = db.get(OMGVM, vm_id)
            if vm is None:
                return None
            vm.display_name = display_name
            vm.updated_at = int(time.time())
            db.commit()
            db.refresh(vm)
            return OMGVMModel.model_validate(vm)

    @staticmethod
    def update_heartbeat(
        vm_id: str,
        heartbeat_payload: Optional[dict] = None,
        db: Optional[Session] = None,
    ) -> Optional[OMGVMModel]:
        with get_db_context(db) as db:
            vm = db.get(OMGVM, vm_id)
            if vm is None:
                return None
            now = int(time.time())
            vm.last_seen_at = now
            vm.last_heartbeat = heartbeat_payload or {}
            vm.is_online = True
            vm.updated_at = now
            db.commit()
            db.refresh(vm)
            return OMGVMModel.model_validate(vm)


class OMGAgentSessions:
    @staticmethod
    def create(
        vm_id: str,
        title: str,
        workdir_path: str,
        policy: Optional[dict] = None,
        meta: Optional[dict] = None,
        db: Optional[Session] = None,
    ) -> OMGAgentSessionModel:
        with get_db_context(db) as db:
            now = int(time.time())
            agent_id = f"omg_{uuid.uuid4().hex}"
            session = OMGAgentSession(
                agent_id=agent_id,
                vm_id=vm_id,
                title=title,
                status="open",
                workdir_path=workdir_path,
                policy=policy or {},
                meta=meta or {},
                created_at=now,
                updated_at=now,
            )
            db.add(session)
            db.commit()
            db.refresh(session)
            return OMGAgentSessionModel.model_validate(session)

    @staticmethod
    def get(agent_id: str, db: Optional[Session] = None) -> Optional[OMGAgentSessionModel]:
        with get_db_context(db) as db:
            session = db.get(OMGAgentSession, agent_id)
            if session is None:
                return None
            return OMGAgentSessionModel.model_validate(session)

    @staticmethod
    def update_meta(
        agent_id: str, extra_meta: dict, db: Optional[Session] = None
    ) -> Optional[OMGAgentSessionModel]:
        with get_db_context(db) as db:
            session = db.get(OMGAgentSession, agent_id)
            if session is None:
                return None
            current_meta = session.meta or {}
            session.meta = {**current_meta, **(extra_meta or {})}
            session.updated_at = int(time.time())
            db.commit()
            db.refresh(session)
            return OMGAgentSessionModel.model_validate(session)

    @staticmethod
    def close(
        agent_id: str, reason: Optional[str] = None, db: Optional[Session] = None
    ) -> Optional[OMGAgentSessionModel]:
        with get_db_context(db) as db:
            session = db.get(OMGAgentSession, agent_id)
            if session is None:
                return None
            now = int(time.time())
            session.status = "archived"
            session.closed_at = now
            session.updated_at = now
            if reason:
                current_meta = session.meta or {}
                current_meta["close_reason"] = reason
                session.meta = current_meta
            db.commit()
            db.refresh(session)
            return OMGAgentSessionModel.model_validate(session)

    @staticmethod
    def list_open_by_vm(
        vm_id: str, db: Optional[Session] = None
    ) -> list[OMGAgentSessionModel]:
        with get_db_context(db) as db:
            rows = (
                db.query(OMGAgentSession)
                .filter(
                    and_(OMGAgentSession.vm_id == vm_id, OMGAgentSession.status == "open")
                )
                .order_by(desc(OMGAgentSession.updated_at))
                .all()
            )
            return [OMGAgentSessionModel.model_validate(row) for row in rows]

    @staticmethod
    def count_open_for_workdir(
        vm_id: str, workdir_path: str, db: Optional[Session] = None
    ) -> int:
        with get_db_context(db) as db:
            return int(
                db.query(func.count(OMGAgentSession.agent_id))
                .filter(
                    and_(
                        OMGAgentSession.vm_id == vm_id,
                        OMGAgentSession.status == "open",
                        OMGAgentSession.workdir_path == workdir_path,
                    )
                )
                .scalar()
                or 0
            )


class OMGAgentEvents:
    @staticmethod
    def add(
        agent_id: str,
        event_type: str,
        payload: Optional[dict] = None,
        seq: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> OMGAgentEventModel:
        with get_db_context(db) as db:
            item = OMGAgentEvent(
                agent_id=agent_id,
                seq=seq,
                event_type=event_type,
                payload=payload or {},
                created_at=int(time.time()),
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            return OMGAgentEventModel.model_validate(item)

    @staticmethod
    def list_after_cursor(
        agent_id: str, cursor: int = 0, limit: int = 200, db: Optional[Session] = None
    ) -> list[OMGAgentEventModel]:
        with get_db_context(db) as db:
            rows = (
                db.query(OMGAgentEvent)
                .filter(and_(OMGAgentEvent.agent_id == agent_id, OMGAgentEvent.id > cursor))
                .order_by(OMGAgentEvent.id.asc())
                .limit(limit)
                .all()
            )
            return [OMGAgentEventModel.model_validate(row) for row in rows]
