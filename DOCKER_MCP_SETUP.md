# Connecting Claude Desktop to TriagePilot Running in Docker

This guide walks through connecting Claude Desktop (macOS) to a TriagePilot MCP server
running inside a Linux Docker container — so Claude can analyze GDB core dumps that live
inside the container.

## How it works

```
Claude Desktop (macOS)
        │
        │  stdio (JSON-RPC)
        ▼
  wrapper script (macOS)
        │
        │  docker exec -i <container>
        ▼
  triagepilot (inside Docker, Linux)
        │
        │  GDB MI protocol
        ▼
  GDB + core dump (inside Docker)
```

`docker exec -i` passes stdin/stdout through transparently, which is exactly what MCP's
stdio transport requires. All file paths passed in tool calls refer to **paths inside
the container**.

---

## Prerequisites

- Docker Desktop for Mac installed and running
- Claude Desktop installed on macOS
- TriagePilot installed inside a Docker image (see below)

---

## Step 1 — Build / prepare the Docker image

If you already have a container with triagepilot installed, note its image name and skip to Step 2.

**Build from this repo:**
```bash
# From the repo root on macOS
docker build -t triagepilot:latest -f- . <<'EOF'
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y \
    python3 python3-pip gdb gcc g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --upgrade pip setuptools wheel && pip install .
EOF
```

---

## Step 2 — Run the container as a long-lived daemon

The container must stay running (not exit after one command).

```bash
docker run -d \
  --name triagepilot-dev \
  --cap-add SYS_PTRACE \
  --security-opt seccomp=unconfined \
  -v "$(pwd)/examples:/app/examples" \
  triagepilot:latest \
  sleep infinity
```

- `--cap-add SYS_PTRACE` — required for GDB to attach to processes / read core dumps
- `--security-opt seccomp=unconfined` — allows GDB's `ptrace` syscalls
- `-v .../examples:/app/examples` — mounts your local `examples/` folder into the container
  so you can drop core dumps there from macOS and have them visible inside Docker
- `sleep infinity` — keeps the container alive

**Verify it is running:**
```bash
docker ps | grep triagepilot-dev
```

---

## Step 3 — Generate a core dump inside the container

```bash
# Enter the container
docker exec -it triagepilot-dev bash

# Inside container:
cd /app/examples
bash build.sh                    # compile the crash examples

ulimit -c unlimited
cd build/out
./use-after-free                 # crashes → writes core file

ls core*                         # note the exact filename, e.g. core.1234
```

> **Tip:** If no `core` file appears, check `/proc/sys/kernel/core_pattern`:
> ```bash
> cat /proc/sys/kernel/core_pattern
> ```
> If it says `/var/crash/%e.%p.crash` or similar, look there.
> To force a predictable name: `echo "core" | sudo tee /proc/sys/kernel/core_pattern`

---

## Step 4 — Create the wrapper script on macOS

This script is what Claude Desktop will run. It pipes MCP stdio into the container.

```bash
cat > ~/triagepilot-docker.sh << 'EOF'
#!/usr/bin/env bash
# Runs triagepilot MCP server inside the running Docker container.
# Claude Desktop launches this script; stdio is piped through docker exec.
exec docker exec -i triagepilot-dev triagepilot "$@"
EOF

chmod +x ~/triagepilot-docker.sh
```

**Test it manually** (should print MCP initialization JSON and then wait):
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' \
  | ~/triagepilot-docker.sh --debugger-type gdb
# Press Ctrl-C after you see the response
```

---

## Step 5 — Configure Claude Desktop

Open the config file:
```bash
open ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

If the file does not exist yet:
```bash
mkdir -p ~/Library/Application\ Support/Claude
touch ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Add (or merge) this block — adjust `image_path` and `repo_path` to match your container paths:

```json
{
  "mcpServers": {
    "triagepilot-gdb": {
      "command": "/Users/YOUR_USERNAME/triagepilot-docker.sh",
      "args": [
        "--debugger-type", "gdb",
        "--image-path", "/app/examples/build/out/use-after-free",
        "--repo-path", "/app/examples"
      ]
    }
  }
}
```

Replace `YOUR_USERNAME` with your macOS username (run `whoami` to check).

> **Multiple binaries:** If you want to analyze different binaries without editing the
> config each time, omit `--image-path` and `--repo-path` here and pass them per-call
> directly in Claude's tool invocation instead.

---

## Step 6 — Restart Claude Desktop

```bash
# Quit Claude Desktop completely (Cmd+Q), then reopen it.
# Or from the menu bar: Claude → Quit Claude
open -a "Claude"
```

In a new conversation, type:
```
What MCP tools do you have available?
```

You should see `triagepilot-gdb` tools listed: `analyze_dump`, `open_dump`,
`run_debugger_cmd`, `close_dump`, `list_dumps`.

---

## Step 7 — Analyze a core dump

Start a new conversation in Claude Desktop and ask:

```
Analyze the crash dump at /app/examples/build/out/core.
The binary is /app/examples/build/out/use-after-free.
The source is in /app/examples.
```

Claude will call `analyze_dump` with those paths (all resolved inside Docker) and return
a structured crash report.

**For an interactive deep-dive:**
```
Open the dump at /app/examples/build/out/core and then:
1. Show me the full backtrace of all threads
2. Show registers at the crash frame
3. Identify the root cause
```

---

## Useful docker commands

| Task | Command |
|------|---------|
| Start the container | `docker start triagepilot-dev` |
| Stop the container | `docker stop triagepilot-dev` |
| Open a shell | `docker exec -it triagepilot-dev bash` |
| View triagepilot logs | `docker exec triagepilot-dev journalctl` *(or check Claude Desktop logs)* |
| Copy a core out of Docker | `docker cp triagepilot-dev:/app/examples/build/out/core ./core` |
| List cores inside container | `docker exec triagepilot-dev find /app -name 'core*'` |

---

## Troubleshooting

### "No such container: triagepilot-dev"
The container exited or was never started. Run Step 2 again:
```bash
docker start triagepilot-dev   # if it exists but is stopped
# or
docker run -d --name triagepilot-dev ... sleep infinity  # if it doesn't exist
```

### Claude Desktop doesn't show triagepilot tools
1. Verify the wrapper script path in the config matches exactly (no `~`, use full path).
2. Check Claude Desktop logs: **Help → Enable Developer Tools → Console tab**.
3. Run the manual test in Step 4 to confirm the wrapper works.

### GDB can't read the core dump
```
/app/examples/build/out/core: not in executable format
```
The core file name may differ. Inside the container:
```bash
find /app -name 'core*' -o -name '*.core' 2>/dev/null
```
Also check the core pattern:
```bash
cat /proc/sys/kernel/core_pattern
```

### GDB permission denied / ptrace errors
The container must be run with `--cap-add SYS_PTRACE --security-opt seccomp=unconfined`.
If you started it without those flags, recreate it:
```bash
docker stop triagepilot-dev && docker rm triagepilot-dev
# then run Step 2 again
```

### Core dump not generated after crash
```bash
# Inside container — check current limit
ulimit -c

# Set unlimited for this shell session
ulimit -c unlimited

# Run the crasher from that same shell
./use-after-free
```

### "triagepilot: command not found" inside container
Reinstall inside the container:
```bash
docker exec -it triagepilot-dev bash
pip install --upgrade pip setuptools wheel
pip install /app
```

---

## Optional: per-analysis paths via Claude

You can omit `--image-path` and `--repo-path` from the Claude Desktop config and pass
them directly when asking Claude:

```
Using triagepilot, analyze the dump at /app/examples/build/out/core.
Use image path /app/examples/build/out/double-free
and repo path /app/examples.
```

Claude will pass those values as tool arguments for that specific call, making it easy
to switch between different crash scenarios without touching the config file.
