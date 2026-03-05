"""Peewee migrations -- add omg gateway tables."""

from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator

with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    @migrator.create_model
    class OmgVm(pw.Model):
        vm_id = pw.TextField(primary_key=True, unique=True)
        display_name = pw.TextField()
        vm_hostname = pw.TextField(null=True)

        auth_token_hash = pw.TextField()
        capabilities = pw.TextField(default="{}")
        codex_version = pw.TextField(null=True)

        last_seen_at = pw.BigIntegerField(null=True)
        last_heartbeat = pw.TextField(default="{}")
        is_online = pw.BooleanField(default=False)

        created_at = pw.BigIntegerField()
        updated_at = pw.BigIntegerField()

        class Meta:
            table_name = "omg_vm"

    @migrator.create_model
    class OmgAgentSession(pw.Model):
        agent_id = pw.TextField(primary_key=True, unique=True)
        vm = pw.ForeignKeyField(
            column_name="vm_id",
            field="vm_id",
            model=migrator.orm["omg_vm"],
            on_delete="CASCADE",
        )
        title = pw.TextField()
        status = pw.TextField(default="open")
        workdir_path = pw.TextField()
        policy = pw.TextField(default="{}")
        meta = pw.TextField(default="{}")

        created_at = pw.BigIntegerField()
        updated_at = pw.BigIntegerField()
        closed_at = pw.BigIntegerField(null=True)

        class Meta:
            table_name = "omg_agent_session"

    @migrator.create_model
    class OmgAgentEvent(pw.Model):
        id = pw.AutoField()
        agent = pw.ForeignKeyField(
            column_name="agent_id",
            field="agent_id",
            model=migrator.orm["omg_agent_session"],
            on_delete="CASCADE",
        )
        seq = pw.BigIntegerField(null=True)
        event_type = pw.TextField()
        payload = pw.TextField(default="{}")
        created_at = pw.BigIntegerField()

        class Meta:
            table_name = "omg_agent_event"

    migrator.add_index("omg_vm", "last_seen_at", unique=False)
    migrator.add_index("omg_vm", "is_online", unique=False)
    migrator.add_index("omg_agent_session", "vm_id", "status", unique=False)
    migrator.add_index("omg_agent_session", "updated_at", unique=False)
    migrator.add_index("omg_agent_event", "agent_id", "id", unique=False)
    migrator.add_index("omg_agent_event", "created_at", unique=False)


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    migrator.remove_model("omg_agent_event")
    migrator.remove_model("omg_agent_session")
    migrator.remove_model("omg_vm")
