# Resolve Pilot — Claude Code plugin for DaVinci Resolve

## Plan
- [x] Download DaVinci Resolve 21.1.1 (free, official Blackmagic API, signature verified) and install silently
- [x] Probe scripting on Free: external scripting blocked; Scripts-menu Lua sandboxed (no io/ffi/require);
      Python menu scripts not listed; interactive Console Lua has LuaJIT FFI
- [x] Architecture: stdlib Python MCP server + Lua FFI socket bridge (Console-started) + Studio direct mode;
      tool logic runs server-side on proxied API objects (one code path for both transports)
- [x] Bridge auto-start tool (Windows tested; macOS AppleScript / Linux xdotool best-effort)
- [x] 43 MCP tools: status/launch/install/start, exec (python|lua), API reference, project, media, timeline,
      tracks, items, markers, titles/generators, interchange, color, gallery, Fusion, render
- [x] Plugin packaging: plugin.json, marketplace.json, .mcp.json (uv), skill, /resolve-pilot:setup
- [x] E2E suite against live Resolve: 147/147 (Python 3.12 and 3.8)
- [x] Fresh headless Claude session completed a full edit+render task using only the plugin
- [x] Installed globally (user scope)
- [x] Portability review (macOS/Linux): fixed SIGPIPE, mac/linux autostart hardening, install-dir override, LUT fallbacks
- [x] Composite frame export (render engine) + page restore after renders (Deliver page changes mark semantics)
- [x] Final cold-start Claude sessions (x2) succeeded; e2e 149/149

- [x] v1.2.0: local open-source AI. `transcribe` (faster-whisper / openai-whisper) and `upscale_image` (Real-ESRGAN
      ncnn-vulkan), plus `ai_tools_status` and `ai_tools_install`. Missing tools return needs_install so Claude asks
      before downloading from the official sources. Verified: clean-venv install, transcription, SRT import into the
      media pool, checksum-verified Real-ESRGAN install, and a 4x upscale.

## Review
- **Free edition findings.**
  - `scriptapp` returns None from outside the app.
  - Scripts-menu, `fusion:Execute` and `RunScript` contexts have no ffi, io or require.
  - Custom UI (UIManager) shows a Studio upsell.
  - The F6 Console has ffi and is the supported way in.
- **Resolve quirks found and handled.**
  - Lua lists carry a hidden `__flags` (0x400000 = list) field.
  - `AppendToTimeline` endFrame is exclusive.
  - LUTs must be in Resolve's LUT folder.
  - `AddTransition` needs an alignment when clips have no handles.
  - Still export works only on the Color page.
  - Native title insert ripples V1.
  - A single `SetSettings({useCustomSettings, timelineFrameRate})` call deadlocks Resolve when compound clips exist.
- **Local AI (v1.2.0).**
  - faster-whisper 1.2.1 breaks with PyAV 18 and newer (`metadata_errors` kwarg), so installs pin `av<18`.
  - A `whisper` package without torch counts as not installed.
  - An SRT imported into the media pool becomes a Subtitle clip.
- **Performance.** A bridge round trip takes about 0.14 ms, and most tools finish in under 100 ms.
