---
name: davinci-resolve
description: Use when the user wants to edit video, organise media, build or change timelines, add markers/titles, grade, use Fusion, or render/export in DaVinci Resolve. Explains how to drive Resolve through the ResolvePilot MCP tools (mcp__plugin_resolve-pilot_resolve__*), how to connect on Free vs Studio, and verification habits.
---

# Driving DaVinci Resolve with ResolvePilot

## 1. Connect first
Call `resolve_status`, then act on what it reports:
- **`connected: true`**: go ahead.
- **Resolve not running**: call `resolve_launch`, then ask the user to open, or let you create, a project. The bridge needs the main window with a project open, not the Project Manager.
- **Running but not connected**: call `resolve_start_bridge`. It opens Resolve's Console (F6) and types one `dofile(...)` line. It is needed once per Resolve session.
  - If it fails (focus could not be taken, Accessibility permission on macOS, or missing xdotool on Linux), give the user the returned `console_command`. They press F6 in Resolve, paste it and press Enter. Next session it's F6, Up-arrow, Enter.
- **Studio users** may instead enable *Preferences → System → General → External scripting using: Local*. No bridge is needed then.

Why the Console? On the free edition, Scripts-menu scripts run sandboxed without network access, while the interactive Console is allowed. The bridge then serves the scripting API on 127.0.0.1 only, protected by a token.

## 2. Mental model
- **Media pool clip** (`media_*`): source media. It is identified by `id` from `media_pool_list`. Names and file paths also work.
- **Timeline item** (`timeline_*`): a clip instance on a track. It is identified by `id` from `timeline_info`. New edits create new ids, so re-read them.
- **Frames**: item `start`/`end` are absolute frames. A timeline usually starts at 86400, which is 01:00:00:00 at 24 fps.
  - In `timeline_append`, `start_frame`/`end_frame` are **source** frames and the end is **exclusive** (0..48 is 48 frames).
  - `record_frame` is the absolute timeline frame or a timecode.
  - Timeline **markers** use frames *relative to the timeline start*. Timecodes are converted for you.
- Track indices are 1-based. Track types are `video`, `audio`, `subtitle`.

## 3. Recipes
- **Rough cut**: `media_import` → `media_pool_list` → `timeline_create {name, clips:[...]}` → `timeline_info`.
- **Put a clip at a time and track**: `timeline_append {clips:[{clip, start_frame, end_frame, track:2, record_frame:"01:00:10:00"}]}`.
- **Titles and generators**: `timeline_insert {kind:"fusion_title", name:"Text+", position:"01:00:05:00", duration:72, text:"Hello", title_inputs:{Size:0.1}}`.
  - The default `mode:"place"` puts the item on a free track above V1 and moves nothing else. It is wrapped in a compound clip with a maximum length of about 5 s.
  - `mode:"insert"` is Resolve's own ripple insert on V1.
- **Transitions, fades, speed** (Resolve 21.1+): use `timeline_item_update` with:
  - `transition:{type:"Cross Dissolve", position:"start"|"end", duration}`
  - `fades:{FadeIn, FadeOut}`
  - `speed:{Percentage}`
- **Transform/crop/opacity**: `timeline_item_update {items, properties:{ZoomX:1.2, ZoomY:1.2, Pan:100, Opacity:50}}`.
- **Grade**:
  - `color_apply {action:"lut"|"cdl"|"drx"|"copy_grade_to"|...}`. External LUT files are copied into Resolve's LUT folder automatically.
  - `color_info` lists the node graph.
- **Deliver**:
  - `render_timeline {target_dir, preset? | format+codec}` renders, waits and returns the output path. Use `render_options` for valid values.
  - On the free edition, output is capped at UHD/60 fps.
- **Interchange**: `timeline_exchange {action:"export", format:"fcpxml"|"edl"|"otio"|"aaf"|"drt"|...}` or `action:"import"`.
- **See the result**: `export_frame {path:"<tmp>/f.png", position:"01:00:05:00"}`, then read the image. Do this to verify visual changes.

## 4. Anything else → `resolve_exec`
- Use `resolve_exec {code}` (Python) with `resolve`, `fusion`, `args` and `ops` in scope.
  - It mirrors the official Python API.
  - Set `result = ...` to return data. The namespace persists between calls.
- Use `language:"lua"` to run Lua *inside* Resolve, with `resolve`, `fusion`, `bmd` and `args`; return a value. It is fastest for big loops.
- Look up exact methods with `resolve_api_reference {class_name:"TimelineItem"}` or `{query:"Transition"}`. These come from the user's installed version.

## 5. Free vs Studio, and known Resolve quirks
- **Studio-only features** return False or errors on Free. Report this plainly; don't retry or try to bypass it.
  - AI tools: Magic Mask, Smart Reframe, scene-cut detection, transcription/auto-subtitles, voice isolation, IntelliSearch, speech generation.
  - Some codecs, output above UHD/60p, and Dolby Vision.
- **Frame rate** can't change once a timeline has clips. `project_settings` reports this under `not_applied`.
- **Frame checks:** `export_frame` renders the fully composited frame (titles, upper tracks, grade) by default. `method:"still"` is quicker but shows only the clip under the playhead.

## 6. Etiquette
- **Destructive actions need the user's go-ahead** unless they asked for them: deleting clips, timelines, projects or render jobs, and ripple deletes.
- **Save** with `project_manage {action:"save"}` after significant changes.
- **Long renders**: use `render_timeline {wait:false}`, then `render_jobs {action:"status"}`.
