# MCP probe execution capability resource

Start the normal resource-only MCP server:

```bash
python scripts/diagnosis_mcp_server.py \
  --snapshot /tmp/atlerror-demo/diagnosis.json
```

The server now exposes:

```text
atlerror://diagnosis/current
atlerror://diagnosis/status
atlerror://probe-execution/capabilities
```

The third resource is available even when active MCP probe tools are disabled. It returns the same host-local executor projection as:

```bash
python scripts/probe_execution.py capabilities --pretty
```

An agent can therefore inspect whether the current recommended semantic probe has a registered and locally available executor before an operator enables active diagnostics.

Example decision flow:

```text
current diagnosis
  -> recommended next probe
  -> probe execution capabilities
       -> registered + available: execution can be offered if tools are enabled
       -> unavailable: remain read-only or gather evidence another way
```

Reading the capability resource never starts a probe session or executes diagnostic work.
