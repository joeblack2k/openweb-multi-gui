# omg-agentd

`omg-agentd` is the per-VM bridge daemon for OMG (OpenWebMultiGui).

## What it does

1. Opens an outbound WebSocket to the central OMG gateway.
2. Registers the VM (`HELLO`) and emits regular `HEARTBEAT` payloads.
3. Manages local OMG agent sessions and workdirs.
4. Executes Codex prompts through `codex exec` and forwards events/results.
5. Keeps a local `codex app-server` process alive (optional).

## Install on a VM

```bash
cd tools/omg-agentd
sudo ./install.sh
```

Then edit:

- `/etc/omg-agentd/config.json`

Required values:

- `gateway_ws_url`
- `vm_id`
- `token`

Restart:

```bash
sudo systemctl restart omg-agentd
sudo systemctl status omg-agentd
```

## Notes

- Default workdir root: `/var/lib/omg-agentd/workspaces`
- Service user: `omg-agentd`
- Logs: `journalctl -u omg-agentd -f`
