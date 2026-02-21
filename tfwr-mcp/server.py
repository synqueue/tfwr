"""
MCP Server for "The Farmer Was Replaced"

Two integration layers:

1. FILE-BASED (always available)
   Read/write .py scripts on disk; game reloads them via File Watcher.
   Save path: %LOCALAPPDATA%/../LocalLow/TheFarmerWasReplaced/TheFarmerWasReplaced/Saves/

2. MOD-BASED (requires MCPBridge BepInEx mod running inside the game)
   HTTP calls to localhost:7070 for live run/stop/state/output/grid/shop control.
   Tools in this layer are prefixed with "live_".
"""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Mod HTTP bridge
# ──────────────────────────────────────────────────────────────────────────────

MOD_BASE = os.environ.get("TFWR_MOD_URL", "http://localhost:7070")


def _mod_get(path: str) -> dict:
    """GET from the in-game mod bridge. Raises if mod not running."""
    try:
        with urllib.request.urlopen(f"{MOD_BASE}{path}", timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"MCPBridge mod not reachable ({e}). "
            "Make sure the game is running with the mod installed."
        ) from e


def _mod_post(path: str, body: dict | None = None) -> dict:
    """POST to the in-game mod bridge."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{MOD_BASE}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"MCPBridge mod not reachable ({e}). "
            "Make sure the game is running with the mod installed."
        ) from e


def _mod_delete(path: str) -> dict:
    req = urllib.request.Request(f"{MOD_BASE}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"MCPBridge mod not reachable ({e}).") from e

from mcp.server.fastmcp import FastMCP

# ──────────────────────────────────────────────────────────────────────────────
# Game data path
# ──────────────────────────────────────────────────────────────────────────────

_local_app_data = Path(os.environ.get("LOCALAPPDATA", "C:/Users/Public/AppData/Local"))
DEFAULT_GAME_DATA = _local_app_data.parent / "LocalLow" / "TheFarmerWasReplaced" / "TheFarmerWasReplaced"

GAME_DATA_PATH = Path(os.environ.get("TFWR_DATA_PATH", str(DEFAULT_GAME_DATA)))
SAVES_PATH = GAME_DATA_PATH / "Saves"
DOCS_PATH  = GAME_DATA_PATH / "docs"

# ──────────────────────────────────────────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "The Farmer Was Replaced",
    instructions=(
        "You help play 'The Farmer Was Replaced' by writing and running automation scripts.\n\n"
        "## Recommended game loop\n"
        "Use live_write_run_wait() as your primary action: it writes a script, starts it,\n"
        "waits for completion, and returns output + game state in one call.\n\n"
        "## Starting a session — always do these first\n"
        "1. live_snapshot()    — see unlocks, inventory, execution status.\n"
        "2. live_farm_grid()   — see what is planted where and where the drone(s) are.\n"
        "3. live_shop()        — see every upgrade that can be bought right now + its cost.\n"
        "4. get_builtins_reference() — understand available functions for the current unlock set.\n\n"
        "## Iteration cycle\n"
        "  write script → live_write_run_wait() → read output/state → decide next action.\n"
        "  After each run: check 'output' for errors, check 'state.items' for new resources,\n"
        "  then call live_shop() and live_buy_upgrade() to spend resources on unlocks.\n\n"
        "## Early game (few unlocks)\n"
        "  Without 'loops' or 'variables', scripts are single-pass (one linear run).\n"
        "  Call live_write_run_wait() repeatedly from here to simulate a loop.\n"
        "  Priority unlock order: variables → loops → functions → senses → expand → speed.\n"
        "  After EACH run, always check live_shop() — you may be able to unlock something.\n\n"
        "## Key unlocks\n"
        "  - variables:  store values between operations\n"
        "  - loops:      while — repeat actions indefinitely\n"
        "  - functions:  def — modular, reusable code\n"
        "  - senses:     get_entity_type(), get_ground_type() — the script can see the farm\n"
        "  - expand:     larger farm grid + move() command\n"
        "  - speed:      faster drone execution (stackable)\n"
        "  - watering:   auto-receive water over time\n"
        "  - fertilizer: auto-receive fertilizer over time\n"
        "  - megafarm:   multiple drones\n\n"
        "## File Watcher requirement\n"
        "  live_write_run_wait() requires 'File Watcher' enabled in Options (Autosave off).\n"
        "  Without it the player must press Run manually after write_script().\n\n"
        "## Reading output\n"
        "  Scripts use print() for debug output. 'output' in run results contains all\n"
        "  print() lines AND any runtime errors/warnings.\n"
    ),
)

# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _save_dir(save_name: str) -> Path:
    return SAVES_PATH / save_name


def _script_path(save_name: str, script_name: str) -> Path:
    name = script_name if script_name.endswith(".py") else f"{script_name}.py"
    return _save_dir(save_name) / name


def _list_saves() -> list[str]:
    if not SAVES_PATH.exists():
        return []
    return sorted(d.name for d in SAVES_PATH.iterdir() if d.is_dir())


def _validate_save(save_name: str) -> None:
    if not _save_dir(save_name).exists():
        available = _list_saves()
        raise ValueError(f"Save '{save_name}' not found. Available saves: {available}")


def _parse_items(serialize_list: list) -> dict[str, float]:
    """Parse ItemBlock.serializeList (alternating name/count pairs) into a dict."""
    items: dict[str, float] = {}
    it = iter(serialize_list)
    try:
        while True:
            name = next(it)
            count = next(it)
            if isinstance(name, str) and isinstance(count, (int, float)):
                items[name] = float(count)
    except StopIteration:
        pass
    return items


def _doc_file_path(doc_path: str) -> Path:
    """Convert a game doc path to a local path under DOCS_PATH.

    Strips a leading 'docs/' prefix to avoid double-nesting, and ensures
    the path ends with '.md'.
    """
    clean = doc_path
    if clean.startswith("docs/"):
        clean = clean[5:]
    if not clean.endswith(".md"):
        clean += ".md"
    return DOCS_PATH / clean


def _wait_for_done(timeout_sec: float) -> tuple[bool, float]:
    """Poll /api/status until isExecuting is False. Returns (timed_out, elapsed_sec)."""
    start = time.time()
    while True:
        time.sleep(1.0)
        try:
            status = _mod_get("/api/status")
        except RuntimeError:
            raise
        if not status.get("isExecuting", True):
            return False, round(time.time() - start, 1)
        if time.time() - start > timeout_sec:
            return True, round(time.time() - start, 1)

# ──────────────────────────────────────────────────────────────────────────────
# File-based tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_saves() -> list[str]:
    """List all available save slots for The Farmer Was Replaced."""
    saves = _list_saves()
    if not saves:
        return ["No saves found — launch the game at least once to create Save0."]
    return saves


@mcp.tool()
def list_scripts(save_name: str = "Save0") -> list[str]:
    """
    List the player-editable Python script files in a save slot.

    Args:
        save_name: Save slot name (default: Save0).
    """
    _validate_save(save_name)
    scripts = [
        f.stem
        for f in _save_dir(save_name).iterdir()
        if f.suffix == ".py" and f.name != "__builtins__.py"
    ]
    return sorted(scripts)


@mcp.tool()
def read_script(script_name: str, save_name: str = "Save0") -> str:
    """
    Read the current code of a script file.

    Args:
        script_name: Script name without the .py extension (e.g. "main").
        save_name:   Save slot name (default: Save0).
    """
    _validate_save(save_name)
    path = _script_path(save_name, script_name)
    if not path.exists():
        available = list_scripts(save_name)
        raise ValueError(
            f"Script '{script_name}' not found in save '{save_name}'. "
            f"Available: {available}"
        )
    return path.read_text(encoding="utf-8")


@mcp.tool()
def write_script(script_name: str, content: str, save_name: str = "Save0") -> str:
    """
    Write or replace a script file with new code.

    IMPORTANT: Enable 'File Watcher' in the game's Options menu (and disable
    Autosave) so the game picks up the change automatically while running.
    The new code takes effect the next time the player presses Run.

    Prefer live_write_run_wait() over this tool when the mod is available —
    it writes the script AND runs it AND waits for results in one step.

    The game uses a Python-like language — see get_builtins_reference() for
    the full list of available functions, entities, items, and unlocks.

    Args:
        script_name: Script name without the .py extension (e.g. "main").
        content:     Full source code to write.
        save_name:   Save slot name (default: Save0).
    """
    _validate_save(save_name)
    path = _script_path(save_name, script_name)
    path.write_text(content, encoding="utf-8")
    return (
        f"Wrote {len(content)} chars to '{script_name}.py' in save '{save_name}'. "
        "Press Run in-game (or File Watcher will reload it automatically)."
    )


@mcp.tool()
def read_game_state(save_name: str = "Save0") -> dict[str, Any]:
    """
    Read the current game state: unlocked features and inventory items.

    Returns a dict with:
      - unlocks: list of unlock strings (e.g. ["loops", "functions", "expand_3"])
      - items:   dict of item_name → quantity
      - version: save file version number

    Args:
        save_name: Save slot name (default: Save0).
    """
    _validate_save(save_name)
    save_json = _save_dir(save_name) / "save.json"
    if not save_json.exists():
        raise ValueError(f"save.json not found for save '{save_name}'.")
    data = json.loads(save_json.read_text(encoding="utf-8"))
    items_raw = data.get("items", {})
    serialize_list = items_raw.get("serializeList", [])
    return {
        "unlocks": data.get("unlocks", []),
        "items": _parse_items(serialize_list),
        "version": data.get("version", 0),
    }


@mcp.tool()
def get_builtins_reference(save_name: str = "Save0") -> str:
    """
    Return the full __builtins__.py stubs file, which documents every built-in
    function, entity type, item, ground, unlock, hat, leaderboard, and direction
    available in the game's scripting language.

    This is the authoritative reference for writing game scripts.

    Args:
        save_name: Save slot name (default: Save0).
    """
    _validate_save(save_name)
    path = _save_dir(save_name) / "__builtins__.py"
    if not path.exists():
        raise ValueError(
            f"__builtins__.py not found in save '{save_name}'. "
            "Launch the game once to generate it."
        )
    return path.read_text(encoding="utf-8")


@mcp.tool()
def get_game_info() -> str:
    """
    Return a concise overview of the game's scripting system: how scripts work,
    how the File Watcher integration works, and what the save directory path is.
    """
    return f"""# The Farmer Was Replaced — Scripting Overview

## How it works
- You write Python-like scripts that control a drone on a farm grid.
- Scripts are `.py` files stored at:
    {SAVES_PATH}/<SaveName>/
- The game reloads scripts when you press **Run** in-game.

## File Watcher (live reload)
- Enable **File Watcher** in the game's Options menu.
- Disable **Autosave** (they conflict).
- Any `.py` file saved externally is automatically reloaded in the game.
- This lets Claude write scripts that the game picks up without manual copy-paste.

## Key concepts
- The drone starts at position (0, 0) (south-west corner).
- X increases East, Y increases North.
- The grid wraps: moving off one edge brings you to the opposite side.
- Each action costs ticks; 1 tick ≈ 2.5 ms at base speed.
- Speed upgrades and `Items.Power` make the drone faster.

## Script files
- `main.py`    — entry point, executed when you press Run.
- Other `.py` files can be imported with `from filename import function_name`.

## Important unlocks to know about
- `loops`      — enables while loops
- `variables`  — enables variable assignment
- `functions`  — enables def
- `import`     — enables importing from other files
- `senses`     — enables get_entity_type(), get_ground_type(), get_pos_x/y()
- `expand`     — unlocks movement (move/can_move) and a bigger farm
- `megafarm`   — unlocks multiple drones (spawn_drone, wait_for, send/receive)

Use `get_builtins_reference()` for the full function and constant reference.
Use `read_game_state()` to see what is currently unlocked and in inventory.
"""

# ──────────────────────────────────────────────────────────────────────────────
# Resources
# ──────────────────────────────────────────────────────────────────────────────

@mcp.resource("tfwr://saves")
def resource_saves() -> str:
    """List of available save slots."""
    return json.dumps(_list_saves(), indent=2)


@mcp.resource("tfwr://saves/{save_name}/state")
def resource_state(save_name: str) -> str:
    """Game state (unlocks + inventory) for a save slot."""
    return json.dumps(read_game_state(save_name), indent=2)


@mcp.resource("tfwr://saves/{save_name}/scripts/{script_name}")
def resource_script(save_name: str, script_name: str) -> str:
    """Contents of a specific script file."""
    return read_script(script_name, save_name)


# ──────────────────────────────────────────────────────────────────────────────
# Live tools — basic (require MCPBridge mod + game running)
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def live_status() -> dict:
    """
    Get real-time game status from the running game (requires MCPBridge mod).

    Returns: isExecuting, isSimulating, worldSize, timeSec.
    """
    return _mod_get("/api/status")


@mcp.tool()
def live_run(script_name: str = "main") -> dict:
    """
    Start executing a script inside the running game (requires MCPBridge mod).

    The script must already be open as a code window in-game.
    If using File Watcher, write_script() first to update the code, then call this.

    Prefer live_write_run_wait() which combines write + run + wait into one call.

    Args:
        script_name: Name of the script to run (default: main).
    """
    return _mod_post("/api/run", {"script": script_name})


@mcp.tool()
def live_stop() -> dict:
    """Stop the currently running script (requires MCPBridge mod)."""
    return _mod_post("/api/stop")


@mcp.tool()
def live_step() -> dict:
    """
    Advance one execution step in step-by-step mode (requires MCPBridge mod).
    """
    return _mod_post("/api/step")


@mcp.tool()
def live_output() -> dict:
    """
    Get all print() output from the currently/last-run script (requires MCPBridge mod).

    Returns: { "output": "<text>" }
    """
    return _mod_get("/api/output")


@mcp.tool()
def live_clear_output() -> dict:
    """Clear the script output log in-game (requires MCPBridge mod)."""
    return _mod_delete("/api/output")


@mcp.tool()
def live_state() -> dict:
    """
    Get real-time game state: unlocks, inventory, world size, execution status
    (requires MCPBridge mod).

    Returns live data directly from the game process — more up-to-date than
    read_game_state() which reads from the save file.
    """
    return _mod_get("/api/state")


@mcp.tool()
def live_scripts() -> dict:
    """
    List all code windows currently open in the game, with execution status
    (requires MCPBridge mod).
    """
    return _mod_get("/api/scripts")


# ──────────────────────────────────────────────────────────────────────────────
# Live tools — game loop (require MCPBridge mod + game running)
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def live_snapshot() -> dict:
    """
    Get all useful game state in a single call: unlocks, inventory, world size,
    execution status, and current script output (requires MCPBridge mod).

    Use this at the start of a session and after each run instead of calling
    live_state() + live_output() separately.

    Returns the live_state() fields plus an additional "output" key.
    """
    state  = _mod_get("/api/state")
    output = _mod_get("/api/output")
    state["output"] = output.get("output", "")
    return state


@mcp.tool()
def live_run_and_wait(
    script_name: str = "main",
    timeout_sec: float = 120.0,
) -> dict:
    """
    Start a script that is already open in the game, wait until it finishes,
    then return a snapshot of the output + game state (requires MCPBridge mod).

    Use live_write_run_wait() instead if you also need to update the script code.

    Args:
        script_name: Name of the script to run (default: main).
        timeout_sec: Maximum seconds to wait before returning anyway (default: 120).

    Returns: { "output": str, "state": dict, "elapsed_sec": float, "timed_out": bool }
    """
    run_result = _mod_post("/api/run", {"script": script_name})
    if not run_result.get("ok"):
        return {
            "error": run_result.get("error", "Failed to start script"),
            "output": "",
            "state": {},
            "elapsed_sec": 0.0,
            "timed_out": False,
        }

    timed_out, elapsed = _wait_for_done(timeout_sec)
    return {
        "output":      _mod_get("/api/output").get("output", ""),
        "state":       _mod_get("/api/state"),
        "elapsed_sec": elapsed,
        "timed_out":   timed_out,
    }


@mcp.tool()
def live_write_run_wait(
    script_name: str,
    content: str,
    save_name: str = "Save0",
    timeout_sec: float = 120.0,
) -> dict:
    """
    Write a script to disk, start it in-game, wait until it finishes, and return
    output + game state — all in one call (requires MCPBridge mod + File Watcher).

    This is the primary tool for the game loop. Use it instead of write_script()
    + live_run() + polling live_status() separately.

    Requires 'File Watcher' to be enabled in Options (Autosave must be off) so
    the game picks up the new code automatically before running.

    Args:
        script_name: Script name without .py extension (e.g. "main").
        content:     Full source code to write.
        save_name:   Save slot name (default: Save0).
        timeout_sec: Max seconds to wait for the script to finish (default: 120).

    Returns: { "output": str, "state": dict, "elapsed_sec": float, "timed_out": bool }
    """
    _validate_save(save_name)
    _script_path(save_name, script_name).write_text(content, encoding="utf-8")

    # Give File Watcher time to reload the script into the game's code window
    time.sleep(0.5)

    # Stop any in-progress execution so live_run won't reject us
    try:
        _mod_post("/api/stop")
    except RuntimeError:
        pass  # mod unreachable — let live_run surface the real error

    run_result = _mod_post("/api/run", {"script": script_name})
    if not run_result.get("ok"):
        return {
            "error": run_result.get("error", "Failed to start script"),
            "output": "",
            "state": {},
            "elapsed_sec": 0.0,
            "timed_out": False,
        }

    timed_out, elapsed = _wait_for_done(timeout_sec)
    return {
        "output":      _mod_get("/api/output").get("output", ""),
        "state":       _mod_get("/api/state"),
        "elapsed_sec": elapsed,
        "timed_out":   timed_out,
    }


@mcp.tool()
def live_farm_grid() -> dict:
    """
    Get the full current state of the farm grid (requires MCPBridge mod).

    Returns:
      - world_size: {"x": N, "y": N}
      - drones:     [{"id": 0, "x": N, "y": N}, ...]
      - cells:      list of {"x", "y", "entity", "grown_pct", "harvestable", "ground", "water"}
                    entity is null when the cell is empty.
                    grown_pct and harvestable are only present for growable entities.

    Use this to understand what's planted where, what's ready to harvest,
    and where the drone(s) are. Essential before writing any farming script.
    """
    return _mod_get("/api/grid")


@mcp.tool()
def live_shop() -> list:
    """
    List all currently purchasable upgrades with their costs and affordability
    (requires MCPBridge mod).

    Only shows upgrades whose parent prerequisites are already met and that are
    not yet at maximum level — i.e. everything you can buy right now.

    Returns a list of dicts, each with:
      - name:        unlock identifier (pass to live_buy_upgrade)
      - level:       current upgrade level (0 = not yet purchased)
      - max_level:   maximum level for this upgrade
      - can_afford:  whether current inventory covers the cost
      - cost:        {"ItemName": quantity, ...}
      - parent:      parent unlock name (if any)
      - descr:       description (if any)

    Call this after every script run — new upgrades may have become affordable.
    """
    return _mod_get("/api/shop")


@mcp.tool()
def live_buy_upgrade(unlock_name: str) -> dict:
    """
    Purchase or upgrade an unlock using current inventory items
    (requires MCPBridge mod).

    The game automatically deducts the required items from inventory.
    Call live_shop() first to confirm the upgrade is available and affordable.

    Args:
        unlock_name: The unlock identifier, e.g. "loops", "variables", "expand", "speed".

    Returns: {"ok": true/false, "message"/"error": str}
    """
    return _mod_post("/api/buy", {"unlock": unlock_name})


# ──────────────────────────────────────────────────────────────────────────────
# Live tools — docs (require MCPBridge mod + game running)
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def live_get_open_docs() -> dict:
    """
    List all currently open DocsWindow instances in the game (requires MCPBridge mod).

    Returns a list of open docs windows, each with:
      - window:  the window name (e.g. "docs0") — pass to live_close_docs_window()
      - doc:     the doc path that is loaded (e.g. "unlocks/loops" or "docs/home.md")
      - content: the full rendered markdown text shown in the window
    """
    return _mod_get("/api/docs")


@mcp.tool()
def live_close_docs_window(window_name: str) -> dict:
    """
    Close a DocsWindow in the game by name (requires MCPBridge mod).

    Args:
        window_name: Window name from live_get_open_docs() (e.g. "docs0").

    Returns: {"ok": true/false, "message"/"error": str}
    """
    return _mod_post("/api/docs/close", {"window": window_name})


@mcp.tool()
def live_fetch_doc(doc_path: str) -> dict:
    """
    Fetch the text content of any game doc page without opening a UI window
    (requires MCPBridge mod).

    Supported path formats:
      - "docs/home.md"          — file-based markdown doc
      - "unlocks/loops"         — unlock tooltip text
      - "functions/harvest"     — function reference tooltip
      - "items/Hay"             — item tooltip
      - "objects/Wheat"         — farm object tooltip

    Args:
        doc_path: Game doc path to fetch.

    Returns: {"path": str, "content": str}
    """
    encoded = urllib.parse.quote(doc_path, safe="")
    return _mod_get(f"/api/docs/fetch?path={encoded}")


# ──────────────────────────────────────────────────────────────────────────────
# Persistent docs — save scraped game documentation to disk
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def save_game_doc(doc_path: str, content: str) -> str:
    """
    Save scraped game documentation to disk under the shared docs/ folder.

    The file is stored at:
        <game_data>/docs/<doc_path>.md
    with any leading 'docs/' prefix stripped to avoid double-nesting.

    Args:
        doc_path: Game doc path (e.g. "unlocks/loops" or "docs/home.md").
        content:  Markdown content to write.
    """
    path = _doc_file_path(doc_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Saved doc to '{path}'"


@mcp.tool()
def read_game_doc(doc_path: str) -> str:
    """
    Read a previously saved game doc from disk.

    Args:
        doc_path: Game doc path (e.g. "unlocks/loops" or "docs/home.md").
    """
    path = _doc_file_path(doc_path)
    if not path.exists():
        raise ValueError(f"Doc '{doc_path}' not found at {path}. Fetch it first with live_fetch_doc().")
    return path.read_text(encoding="utf-8")


@mcp.tool()
def list_game_docs() -> list[str]:
    """
    List all game documentation files saved to disk.

    Returns paths relative to the docs/ folder (e.g. ["home.md", "unlocks/loops.md"]).
    """
    if not DOCS_PATH.exists():
        return []
    return sorted(str(p.relative_to(DOCS_PATH)) for p in DOCS_PATH.rglob("*.md"))


@mcp.tool()
def capture_upgrade_docs() -> dict:
    """
    Read every open DocsWindow from the game, save each doc to disk, then
    close the windows (requires MCPBridge mod).

    Call this immediately after purchasing an upgrade — the game opens a
    DocsWindow automatically, and this tool captures and stores its content.

    Returns:
      - captured: list of {"doc": path, "window": name, "saved": bool}
      - message:  summary string
    """
    result = _mod_get("/api/docs")
    docs = result.get("docs", [])
    if not docs:
        return {"captured": [], "message": "No docs windows open"}

    captured = []
    for doc in docs:
        window_name = doc.get("window", "")
        doc_path    = doc.get("doc", "")
        content     = doc.get("content", "")

        entry: dict = {"doc": doc_path, "window": window_name}
        if doc_path and content:
            try:
                save_game_doc(doc_path, content)
                entry["saved"] = True
            except Exception as e:
                entry["saved"] = False
                entry["error"] = str(e)
        else:
            entry["saved"] = False
            entry["error"] = "empty doc path or content"

        captured.append(entry)

        if window_name:
            try:
                _mod_post("/api/docs/close", {"window": window_name})
            except Exception:
                pass

    saved_count = sum(1 for e in captured if e.get("saved"))
    return {
        "captured": captured,
        "message": f"Captured and saved {saved_count}/{len(captured)} docs",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Persistent notes & goals (per save slot)
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def read_notes(save_name: str = "Save0") -> str:
    """
    Read persistent notes for a save slot.

    Notes are free-form markdown where the model records observations,
    discoveries, and lessons learned during play.

    Args:
        save_name: Save slot (default: Save0).

    Returns: note contents, or empty string if none exist yet.
    """
    _validate_save(save_name)
    path = _save_dir(save_name) / "notes.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@mcp.tool()
def save_notes(content: str, save_name: str = "Save0") -> str:
    """
    Overwrite persistent notes for a save slot.

    Args:
        content:   Full markdown content to save.
        save_name: Save slot (default: Save0).
    """
    _validate_save(save_name)
    path = _save_dir(save_name) / "notes.md"
    path.write_text(content, encoding="utf-8")
    return f"Saved notes ({len(content)} chars) to '{path}'"


@mcp.tool()
def read_goals(save_name: str = "Save0") -> str:
    """
    Read persistent goals and progress for a save slot.

    Goals track what the model is working toward and how far along it is.

    Args:
        save_name: Save slot (default: Save0).

    Returns: goals/progress content, or empty string if none exist yet.
    """
    _validate_save(save_name)
    path = _save_dir(save_name) / "goals.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@mcp.tool()
def save_goals(content: str, save_name: str = "Save0") -> str:
    """
    Overwrite goals and progress for a save slot.

    Args:
        content:   Full markdown content describing current goals and progress.
        save_name: Save slot (default: Save0).
    """
    _validate_save(save_name)
    path = _save_dir(save_name) / "goals.md"
    path.write_text(content, encoding="utf-8")
    return f"Saved goals ({len(content)} chars) to '{path}'"


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
