# Security policy

## Threat model

`claude-bridge` is a small MCP server intended to run inside a
**devcontainer that you control**. Its job is to dispatch
`claude -p` invocations on behalf of an MCP client (typically Claude
Desktop / Cowork) running on the host.

The trust boundary is the container. In particular:

- **Anyone who can `docker exec` into the container** can run the
  bridge's tools and, through them, drive `claude` with whatever auth
  lives there.
- **Anyone who can edit `claude_desktop_config.json` on the host** can
  configure an MCP server that gets launched whenever Claude Desktop
  starts. Treat that file as privileged.
- **The bridge does not authenticate MCP callers.** MCP stdio assumes
  the calling process has filesystem access to invoke the bridge.

## What the bridge persists

Inside `$CLAUDE_BRIDGE_STATE`'s parent directory:

- `sessions.json` — channel → Claude Code session id
- `jobs.json` — async dispatch state, including PIDs and on-disk
  output paths
- `schedules.json` — recurring dispatch state
- `events.json` — bounded ring buffer of recent state transitions
- `job-output/<job_id>/{stdout,stderr}` — raw subprocess output
- `bridge.log` *(only if `CLAUDE_BRIDGE_LOG` is set)* — append-only
  JSONL event log

**Prompts are not persisted or logged by default.** Set
`CLAUDE_BRIDGE_PERSIST_PROMPTS=1` and/or `CLAUDE_BRIDGE_LOG_PROMPTS=1`
to opt in for debugging.

## `bypassPermissions`

`permission_mode="bypassPermissions"` on `dispatch` /
`schedule_dispatch` removes Claude Code's permission gates entirely.
Only set this default when the container's network and filesystem
isolation are doing the work — never on a bridge that's host-mounted
or shares secrets with workloads you don't trust.

See the [official devcontainer guide](https://code.claude.com/docs/en/devcontainer)
for the recommended firewall configuration when running with elevated
permissions.

## Webhooks

`notify_url` POSTs include a truncated result preview (4 KB cap). If
the URL leaks, the recipient sees those previews. Pick a URL whose
inbound traffic you control, and use `notify_headers` to add a shared
secret if the relay supports it.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security bug.

- **Preferred**: open a private security advisory at
  <https://github.com/JosiahSiegel/claude-bridge/security/advisories/new>.
- **Email**: as a fallback, contact the maintainer through the email
  associated with the latest release commits.

Expect an initial reply within a few business days. Coordinated
disclosure is appreciated; the bridge has no auto-update mechanism, so
fixes ship as a tagged release that downstream consumers pick up by
re-installing.
