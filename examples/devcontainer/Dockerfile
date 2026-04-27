# Reference Dockerfile that bakes claude-bridge into a devcontainer so
# `docker exec -i <container> claude-bridge` Just Works.
#
# Adjust the base image to match your actual stack — the only
# requirements are a working Python 3.11+ and the `claude` CLI on PATH.

FROM mcr.microsoft.com/devcontainers/python:1-3.12

# 1. Install claude-bridge into the system Python so it lands on the
#    default PATH (/usr/local/bin/claude-bridge). docker exec doesn't
#    activate venvs, so this is the simplest way to make the bare
#    `claude-bridge` name resolvable from a host MCP client.
#
#    Once the package is published to PyPI:
#      pip install --break-system-packages claude-bridge-mcp
#
#    (Note: the PyPI distribution is named "claude-bridge-mcp" because
#    the bare "claude-bridge" name was already taken by an unrelated
#    HTTP gateway. The Python import, the CLI command, and the MCP
#    server identifier are still all just "claude_bridge" / "claude-bridge".)
#
#    Until then, install from GitHub:
RUN pip install --break-system-packages \
    "git+https://github.com/JosiahSiegel/claude-bridge.git@main"

# 2. Install the Claude Code CLI. The bridge shells out to `claude -p`,
#    so this binary must exist on PATH inside the container.
#    Replace with your preferred install method (npm, brew, distro
#    package, etc.). See https://docs.claude.com/en/docs/claude-code
#    for current options.
#
# RUN npm install -g @anthropic-ai/claude-code

# 3. Authentication is intentionally not configured here. The bridge
#    inherits the container's environment unchanged, so any of these
#    work without bridge configuration:
#      - ANTHROPIC_API_KEY in containerEnv
#      - CLAUDE_CODE_OAUTH_TOKEN in containerEnv (from `claude setup-token`)
#      - On-disk ~/.claude/.credentials.json (from `claude /login`,
#        usually mounted via a volume that survives container rebuilds)
