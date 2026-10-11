# ResolvePilot: Claude Code ↔ DaVinci Resolve

ResolvePilot is a Claude Code plugin that lets Claude work in **DaVinci Resolve**, both the **free edition** and **Studio**.

## What Claude can do

It has 43 MCP tools. Claude can:
- **Projects:** create, open and save projects, and change their settings.
- **Media:** import media and organise bins.
- **Timelines:** build timelines and edit clips onto any track.
- **Clips:** trim ranges; set transforms, crop and opacity; add fades, speed changes and transitions.
- **Markers, titles and generators:** add markers; place titles and generators without rippling the timeline.
- **Color:** grade with LUTs, CDL and DRX files; copy grades; manage versions and color groups.
- **Fusion:** manage compositions.
- **Interchange:** export and import FCPXML, EDL, AAF, OTIO and DRT.
- **Output:** render, and export stills to *look* at the result.

For anything else, `resolve_exec` runs Python or Lua against the full official scripting API.

## How it works

```
Claude Code ──MCP/stdio──► server/mcp_server.py ──localhost TCP──► "ResolvePilot Bridge" (Lua, inside Resolve)
                               │                                       │
                               └── Studio only: direct DaVinciResolveScript connection (no bridge needed)
```

- **Why a bridge:** the free edition blocks external scripting (`scriptapp("Resolve")` returns `None`). Its Scripts menu also runs Lua in a sandbox without sockets. The **interactive Console** does allow LuaJIT FFI, so the bridge is started from there by one `dofile(...)` line.
- **Who types it:** Claude does it for you with `resolve_start_bridge`, which needs a running Resolve with a project open. You can also paste the line yourself.
- **What the bridge exposes:** the Resolve API, on `127.0.0.1` only, protected by a per-session token. It does not change Resolve's preferences or security settings.
- **Where tool logic runs:** in the MCP server against proxied API objects (about 0.1 ms per call). Bridge mode and Studio direct mode therefore run the exact same code.

> **New here? Follow the step-by-step [INSTALL.md](INSTALL.md).**

## Requirements

- DaVinci Resolve 18.5 or newer (tested on **21.1.1 free**, Windows 11). The free version is at https://www.blackmagicdesign.com/products/davinciresolve
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which launches the stdlib-only Python server (Python ≥ 3.8; uv downloads one if needed).
  - Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `uv` must be on the PATH that Claude Code sees. On macOS, apps launched from the Dock may not include `~/.local/bin` or `/opt/homebrew/bin`. If the MCP server fails to start, replace `"command": "uv"` in `.mcp.json` with uv's absolute path.
- The Mac App Store edition of Resolve is sandboxed and untested. Prefer the download from blackmagicdesign.com.
- Optional, for automatic bridge start:
  - **macOS:** give your terminal Accessibility permission.
  - **Linux:** install `xdotool` and `xclip` (X11).

## Install

1. Get the plugin, either way:
   - From GitHub: `claude plugin marketplace add Abdallah362728/resolve-pilot`.
   - Or locally: `git clone https://github.com/Abdallah362728/resolve-pilot`, then `claude plugin marketplace add ./resolve-pilot`.
2. Install it: `claude plugin install resolve-pilot@resolve-pilot --scope user`.
3. Start Claude Code and run `/resolve-pilot:setup`, or just ask for something like *"open Resolve and make a rough cut from ~/Videos/trip"*.

### Each Resolve session

Open a project in Resolve. Claude calls `resolve_start_bridge` when needed. To start the bridge by hand instead:
1. Press **F6** (Workspace → Console).
2. Paste the command below and press Enter. On the next session it's just F6, ↑, Enter.

```lua
dofile([[<your Fusion Scripts folder>/Utility/ResolvePilot Bridge.lua]])
```

`resolve_status` prints the exact line for your machine.

**Studio users** can skip the bridge entirely by enabling *Preferences → System → General → External scripting using: Local*.

## Configuration (environment variables, all optional)

| Variable | Purpose |
|---|---|
| `RESOLVE_EXECUTABLE`, `RESOLVE_INSTALL_DIR` | Non-standard Resolve install location |
| `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB` | Scripting docs / `fusionscript` library (Studio direct mode) |
| `RESOLVE_PILOT_SCRIPTS_DIR` | Resolve `Fusion/Scripts` folder for the bridge |
| `RESOLVE_PILOT_PORT` | Bridge port (default 52931). It must be set for both Resolve and Claude. |
| `RESOLVE_PILOT_TIMEOUT` | Seconds to wait for a single Resolve call (default 600) |
| `RESOLVE_PILOT_LUT_DIR` | Writable LUT folder used when applying external LUT files |
| `RESOLVE_PILOT_DISABLE_DIRECT=1` | Never try the Studio direct connection |

## Free vs Studio

**Free:** every scripting feature works through the bridge.

**Studio-only features** fail on Free, and the tools report them as such. ResolvePilot does not try to unlock them:
- AI tools: Magic Mask, Smart Reframe, scene-cut detection, transcription/auto-captions, voice isolation, IntelliSearch.
- Some codecs, and output above UHD/60 fps.

## Known Resolve quirks handled for you

**Placement and editing**
- Resolve's own title/generator insert ripples V1. `timeline_insert` defaults to a *place* mode, which wraps the effect in a compound clip and puts it on a free track.
- `AppendToTimeline` end frames are exclusive.
- Transitions need explicit alignment when clips have no handles.

**Color and stills**
- LUTs must live in Resolve's LUT folder, so external files are copied there.
- Still export only shows the current clip, so `export_frame` renders the composited frame instead.

**Settings**
- Setting `useCustomSettings` and `timelineFrameRate` in one call can deadlock Resolve 21.1 when the project has compound clips, so they are always applied separately.

**Bridge internals**
- Resolve's Lua lists carry a hidden `__flags` field, which the bridge uses to tell lists from dicts.

## Development

```bash
python tests/e2e_test.py             # needs Resolve running + bridge started; creates & deletes a scratch project
python tests/e2e_test.py --keep      # keep the scratch project for inspection
uv run --python 3.8 tests/e2e_test.py  # check Python 3.8 compatibility
```

The suite exercises every tool against a live Resolve: 147 checks. It covers editing, color, Fusion, interchange formats and rendering, and verifies output files.

## License

MIT
