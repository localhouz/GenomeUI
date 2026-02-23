
GenomeUI 2-22-2026
catch up through synapse
Synapse-synapse [get_context]
OUT
Error getting collection: Collection [41a85fcf-e508-4a13-8d6c-321cb76ae0a2] does not exist.
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md
Synapse-synapse [search_knowledge]GenomeUI project status tasks
OUT
<tool_use_error>Sibling tool call errored</tool_use_error>
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 2-101)
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 2-151)
Bash Check synapse directories for stored notes
IN
ls /c/Users/steve/Documents/GenomeUI/.synapse/ 2>/dev/null && ls /c/Users/steve/Documents/GenomeUI/synapse_data/ 2>/dev/null

OUT
config.json
chromadb
manifest.db
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 151-350)
Bash Check synapse config
IN
cat /c/Users/steve/Documents/GenomeUI/.synapse/config.json 2>/dev/null && cat /c/Users/steve/Documents/GenomeUI/synapse_data/config.json 2>/dev/null

OUT
Exit code 1
{
  "embedding_provider": "ollama",
  "embedding_model": "nomic-embed-text",
  "dimension": 768,
  "index_backend": "chromadb",
  "watch_paths": [
    "C:\\Users\\steve\\Documents\\GenomeUI"
  ],
  "min_chunk_chars": 500,
  "max_chunk_chars": 3500,
  "overlap_percentage": 0.15,
  "agent_enabled": false,
  "agent_model": "llama3",
  "agent_auto_store": true,
  "agent_auto_retrieve": true,
  "agent_store_threshold": 0.6
}
{
  "embedding_provider": "ollama",
  "embedding_model": "nomic-embed-text",
  "dimension": 768,
  "index_backend": "chromadb",
  "watch_paths": [
    "C:\\Users\\steve\\Documents\\GenomeUI"
  ],
  "min_chunk_chars": 500,
  "max_chunk_chars": 3500,
  "overlap_percentage": 0.15,
  "agent_enabled": false,
  "agent_model": "llama3",
  "agent_auto_store": true,
  "agent_auto_retrieve": true,
  "agent_store_threshold": 0.6
}
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 345-644)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 701-800)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 801-900)
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 641-840)
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 1801-2100)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 901-1000)
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 2096-2195)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 1001-1100)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 1101-1250)
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 2195-2394)
Let me jump to the very end of both files.

Bash Get line counts for key files
IN
wc -l /c/Users/steve/Documents/GenomeUI/docs/TASKBOARD.md && wc -l /c/Users/steve/Documents/GenomeUI/GenomeChat.md

OUT
5719 /c/Users/steve/Documents/GenomeUI/docs/TASKBOARD.md
1865 /c/Users/steve/Documents/GenomeUI/GenomeChat.md
Read c:\Users\steve\Documents\GenomeUI\docs\TASKBOARD.md (lines 5501-5720)
Read c:\Users\steve\Documents\GenomeUI\GenomeChat.md (lines 1701-1866)
Now I have a full picture. Let me check the current state of the MCP implementation since the last session ended mid-edit.

Bash Check current MCP stdio implementation state
IN
grep -n "MCP_CONFIG_PATH\|\.mcp\.json\|McpStdioProcess\|_mcp_stdio\|startup.*mcp\|shutdown.*mcp\|asyncio.*subprocess\|async_generator\|mcp_server_procs\|MCP_PROCS" /c/Users/steve/Documents/GenomeUI/backend/main.py | head -40

OUT
41:MCP_CONFIG_PATH = pathlib.Path(os.getenv("GENOMEUI_MCP_CONFIG_PATH", ".mcp.json"))
57:# MCP connector registry — populated at startup from .mcp.json, never from user input
847:async def _mcp_stdio_rpc(proc_info: dict[str, Any], method: str, params: dict, req_id: int | None, timeout: float = 10.0) -> dict[str, Any]:
880:async def _spawn_mcp_stdio_server(server_id: str, name: str, command: str, args: list[str], env_extra: dict[str, str]) -> list[dict[str, Any]]:
885:    proc = await asyncio.create_subprocess_exec(
887:        stdin=asyncio.subprocess.PIPE,
888:        stdout=asyncio.subprocess.PIPE,
889:        stderr=asyncio.subprocess.DEVNULL,
894:    await _mcp_stdio_rpc(proc_info, "initialize", {
899:    await _mcp_stdio_rpc(proc_info, "notifications/initialized", {}, req_id=None, timeout=5.0)
900:    tools_resp = await _mcp_stdio_rpc(proc_info, "tools/list", {}, req_id=2, timeout=8.0)
915:async def _call_mcp_stdio_tool(server_id: str, tool_name: str, arguments: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
923:        resp = await _mcp_stdio_rpc(proc_info, "tools/call", {
963:        return await _call_mcp_stdio_tool(server_id, tool_name, arguments)
968:# ── MCP config loading (from .mcp.json) ──────────────────────────────────────
971:    """Read .mcp.json and connect to all defined MCP servers automatically."""
972:    config_path = MCP_CONFIG_PATH
993:                tools = await _spawn_mcp_stdio_server(server_id, name, command, args, env_extra)
1040:async def shutdown_mcp_stdio_servers() -> None:
2321:    # Connect to MCP servers defined in .mcp.json — non-blocking, errors are stored per server
2338:    await shutdown_mcp_stdio_servers()
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 969-1048)
Read c:\Users\steve\Documents\GenomeUI\.mcp.json
Bash Check if manual MCP endpoints still exist
IN
grep -n "api/mcp/servers\|mcp_register\|mcp_list_servers\|mcp_remove\|mcp_refresh\|McpServerRegisterBody" /c/Users/steve/Documents/GenomeUI/backend/main.py | head -20

OUT
6975:class McpServerRegisterBody(BaseModel):
6981:@app.post("/api/mcp/servers")
6982:async def mcp_register_server(body: McpServerRegisterBody):
7040:@app.get("/api/mcp/servers")
7041:async def mcp_list_servers():
7049:@app.delete("/api/mcp/servers/{server_id}")
7050:async def mcp_remove_server(server_id: str):
7072:@app.post("/api/mcp/servers/{server_id}/refresh")
7073:async def mcp_refresh_server(server_id: str):
Bash Check MCP globals and persistence functions
IN
grep -n "MCP_STDIO_PROCESSES\|MCP_MAX_SERVERS\|MCP_MAX_CALLS\|save_mcp_registry\|load_mcp_registry" /c/Users/steve/Documents/GenomeUI/backend/main.py | head -20

OUT
62:MCP_STDIO_PROCESSES: dict[str, dict[str, Any]] = {}
911:    MCP_STDIO_PROCESSES[server_id] = proc_info
917:    proc_info = MCP_STDIO_PROCESSES.get(server_id)
1042:    for server_id, proc_info in list(MCP_STDIO_PROCESSES.items()):
1053:    MCP_STDIO_PROCESSES.clear()
6997:        if len(MCP_SERVER_REGISTRY) >= MCP_MAX_SERVERS:
6998:            raise HTTPException(status_code=400, detail=f"MCP server limit ({MCP_MAX_SERVERS}) reached")
7028:    save_mcp_registry()
7068:    save_mcp_registry()
7093:        save_mcp_registry()
7099:        save_mcp_registry()
Bash Check if save/load mcp_registry functions still exist
IN
grep -n "def save_mcp_registry\|def load_mcp_registry\|MCP_REGISTRY_PATH" /c/Users/steve/Documents/GenomeUI/backend/main.py | head -20

OUT
0
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 6976-7105)
Bash Check which MCP helper functions still exist
IN
grep -n "def discover_mcp_server_tools\|def is_safe_mcp_url\|def _sanitize_tool_text" /c/Users/steve/Documents/GenomeUI/backend/main.py

OUT
791:def is_safe_mcp_url(url: str) -> tuple[bool, str]:
821:def _sanitize_tool_text(text: str) -> str:
Bash Check MCP turn fallback integration
IN
grep -n "find_best_mcp_tool\|mcp_tool_call\|MCP.*fallback\|fallback.*MCP" /c/Users/steve/Documents/GenomeUI/backend/main.py | head -20

OUT
1074:def find_best_mcp_tool(intent_text: str) -> tuple[str | None, str | None, float]:
7212:        # MCP fallback: if no connector produced results and MCP servers are registered,
7215:            _mcp_sid, _mcp_url, _mcp_tool, _mcp_score = find_best_mcp_tool(intent)
7221:                        "op": "mcp_tool_call",
7236:                        "op": "mcp_tool_call",
28703:    if kind == "mcp_tool_call":
28705:        # Most MCP calls go through the fallback in /api/turn; this handles explicit ops.
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 1056-1095)
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 7206-7255)
Bash Check if call_mcp_tool still exists
IN
grep -n "^async def call_mcp_tool\|^def call_mcp_tool" /c/Users/steve/Documents/GenomeUI/backend/main.py

OUT
0
Good — I can see exactly what happened. The last session got cut off mid-edit and left several bugs. Here's the situation:

What was built before the cutoff:

Stdio-based MCP process management (_spawn_mcp_stdio_server, _mcp_stdio_rpc, _call_mcp_stdio_tool)
Auto-load from .mcp.json at startup (load_mcp_servers_from_config)
T1–T241 all complete
What broke when the session hit "Prompt is too long":

The turn fallback (line 7215) tries to unpack 4 values from find_best_mcp_tool which only returns 3 — ValueError at runtime
Same fallback calls call_mcp_tool(url, ...) (the HTTP version) which was deleted — NameError
The old manual registration endpoints (POST/DELETE/REFRESH /api/mcp/servers) still call save_mcp_registry() and discover_mcp_server_tools() — both deleted — NameError if hit
The fix is clean: patch the turn fallback to use _call_mcp_stdio_tool, and remove/simplify the dead manual endpoints. Let me do it.

Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Modified
Now fix the manual endpoints — replace them with a read-only list endpoint (since the new model is config-driven):

Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Removed 119 lines
Prompt is too long