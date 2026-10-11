#!/usr/bin/env python3
"""ResolvePilot MCP server -- lets Claude drive DaVinci Resolve (Free or Studio).

Stdlib only (Python 3.8+). Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).
"""

import json
import os
import re
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autostart  # noqa: E402
import resolve_link  # noqa: E402
from resolve_link import ResolveLink, ResolveError, NotConnected  # noqa: E402
from resolve_ops import OpError  # noqa: E402

SERVER_NAME = "resolve-pilot"
SERVER_VERSION = "1.1.1"
LINK = ResolveLink()

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}
O = {"type": "object"}
FRAME = {"type": ["integer", "string"], "description": "Frame number or timecode 'HH:MM:SS:FF'."}
TIMELINE = {"type": "string", "description": "Timeline name or id. Defaults to the current timeline."}
ITEMS = {"type": "array", "items": S, "description": "Timeline item ids (from timeline_info) or unique names."}
CLIPS = {"type": "array", "items": S,
         "description": "Media pool clip ids (from media_pool_list), names, 'Bin/Sub/name' paths or file paths."}


def arr(item, desc=None):
    d = {"type": "array", "items": item}
    if desc:
        d["description"] = desc
    return d


def enum(values, desc=None):
    d = {"type": "string", "enum": list(values)}
    if desc:
        d["description"] = desc
    return d


def d(schema, desc):
    out = dict(schema)
    out["description"] = desc
    return out


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


CLIP_SPEC = {"type": ["string", "object"], "description": (
    "Clip id/name, or {clip, start_frame, end_frame (source frames; end is EXCLUSIVE, so 0..48 = 48 frames; give both), "
    "track (1-based), record_frame (timeline frame or timecode), media_type: video|audio}.")}

# ---------------------------------------------------------------------------
# Tool table: name -> (description, input schema, handler)
# Handlers taking `ops:<fn>` run resolve_ops.<fn> inside Resolve.
# ---------------------------------------------------------------------------

TOOLS = []


def tool(name, desc, schema, handler):
    TOOLS.append({"name": name, "description": desc, "inputSchema": schema, "handler": handler})


# --- connection / app --------------------------------------------------------

def h_status(args):
    out = {"plugin_version": SERVER_VERSION}
    try:
        out["connection"] = LINK.info()
        out["resolve"] = LINK.run("status", {})
        out["connected"] = True
    except NotConnected as e:
        out["connected"] = False
        out["resolve_running"] = resolve_link.resolve_running()
        out["help"] = str(e)
        out["bridge_installed"] = os.path.exists(resolve_link.bridge_install_path())
        out["resolve_installed"] = os.path.exists(resolve_link.resolve_paths()["executable"])
    return out


def h_launch(args):
    if resolve_link.resolve_running():
        return {"note": "Resolve is already running"}
    res = resolve_link.launch_resolve(["-nogui"] if args.get("headless") else None)
    if args.get("wait", True):
        t0 = time.time()
        while time.time() - t0 < float(args.get("timeout", 120)):
            if resolve_link.resolve_running():
                break
            time.sleep(2)
        res["running"] = bool(resolve_link.resolve_running())
    res["next_step"] = ("Wait for Resolve to finish loading and ask the user to open a project, then call "
                        "resolve_start_bridge.")
    return res


def h_install_bridge(args):
    return resolve_link.install_bridge(args.get("scripts_dir"))


def h_start_bridge(args):
    if resolve_link.bridge_alive():
        return {"ok": True, "note": "bridge already running"}
    if not resolve_link.resolve_running():
        return {"ok": False, "error": "DaVinci Resolve is not running (use resolve_launch, then open a project)"}
    inst = resolve_link.install_bridge()
    res = autostart.start_bridge(resolve_link.bridge_install_path(), resolve_link.bridge_alive,
                                 float(args.get("timeout", 15)))
    res["bridge_file"] = inst["installed_to"]
    if res.get("ok"):
        LINK.reset()
    return res


def h_exec(args):
    if args.get("language", "python") == "lua":
        return LINK.exec_lua(args["code"], args.get("args"))
    return LINK.exec_python(args["code"], args.get("args"), bool(args.get("fresh")))


def h_api_reference(args):
    """Search the scripting API docs that ship with the user's own Resolve install."""
    p = resolve_link.resolve_paths()
    files = [os.path.join(p["api_dir"], "DaVinciResolveScript.pyi"), os.path.join(p["api_dir"], "README.md"),
             os.path.join(p["api_dir"], "CHANGELOG.md"), os.path.join(p["api_dir"], "README.txt")]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return {"error": "Scripting docs not found under %s (set RESOLVE_SCRIPT_API)" % p["api_dir"]}
    q = (args.get("query") or "").strip()
    cls = args.get("class_name")
    max_lines = int(args.get("max_lines", 120))
    out = []
    pyi = [f for f in files if f.endswith(".pyi")]
    if cls and pyi:
        lines = open(pyi[0], encoding="utf-8", errors="replace").read().splitlines()
        grab = False
        for ln in lines:
            if ln.startswith("class "):
                grab = ln.startswith("class %s" % cls) and (ln[6 + len(cls):7 + len(cls)] in ("(", ":", " "))
            if grab:
                s = ln.strip()
                if s.startswith("def ") or s.startswith("class ") or (s and not s.startswith('"""') and ":" in s and len(out) < 5):
                    out.append(ln.rstrip())
                elif s.startswith('"""') and out and out[-1].lstrip().startswith("def "):
                    out[-1] += "   # " + s.strip('"')
        return {"class": cls, "lines": out[:max_lines * 4]}
    if not q:
        return {"files": files, "hint": "Pass query (regex/text) or class_name (Resolve, ProjectManager, Project, "
                                        "MediaPool, MediaPoolItem, Timeline, TimelineItem, Folder, Graph, Gallery, "
                                        "GalleryStillAlbum, ColorGroup, MediaStorage, or a TypedDict like "
                                        "RenderSettings / TimelineItemProperties / ProjectSettings)."}
    rx = re.compile(q, re.I)
    for f in files:
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        for i, ln in enumerate(lines):
            if rx.search(ln):
                ctx = lines[i:i + 3] if f.endswith(".pyi") else lines[i:i + 1]
                out.append("%s:%d: %s" % (os.path.basename(f), i + 1, " | ".join(c.strip() for c in ctx)))
                if len(out) >= max_lines:
                    return {"matches": out, "truncated": True}
    return {"matches": out}


tool("resolve_status",
     "Check the connection to DaVinci Resolve and report product/edition/version, page, open project, current "
     "timeline and render state. Call this first. If not connected it explains exactly what to do.",
     obj({}), h_status)
tool("resolve_launch", "Start DaVinci Resolve if it is not running.",
     obj({"wait": d(B, "Wait for the process to appear (default true)."), "timeout": N,
          "headless": d(B, "Start with -nogui (scripting only, no UI).")}), h_launch)
tool("resolve_install_bridge",
     "Install/update the ResolvePilot Bridge (Lua) into Resolve's per-user Scripts/Utility folder. "
     "resolve_start_bridge does this automatically; call it directly only to repair or relocate the install.",
     obj({"scripts_dir": d(S, "Override the Resolve Fusion/Scripts folder.")}), h_install_bridge)
tool("resolve_start_bridge",
     "Start the ResolvePilot bridge inside a running DaVinci Resolve by opening its Console (F6) and typing one "
     "dofile(...) line (keystrokes are sent only after verifying the Console window has focus). Needed once per "
     "Resolve session on the free edition. Requires an open project. Returns the manual command if automation "
     "is not possible.",
     obj({"timeout": N}), h_start_bridge)
tool("resolve_exec",
     "Escape hatch with FULL scripting API access. language=python (default): Python code with globals "
     "`resolve` (live API object, same methods as the official Python API), `fusion`, `args`, `ops` "
     "(ResolvePilot helpers, e.g. ops.frames_to_tc); assign `result = ...` to return data; print() output is "
     "returned; the namespace persists between calls unless fresh=true. language=lua: Lua code executed inside "
     "Resolve (fastest for loops over many objects) with `resolve`, `fusion`, `bmd`, `args`; use `return value`. "
     "Look up methods with resolve_api_reference.",
     obj({"code": S, "args": O, "fresh": B, "language": enum(("python", "lua"))}, ["code"]), h_exec)
tool("resolve_api_reference",
     "Search the official scripting API documentation installed with Resolve (the .pyi/README for the user's "
     "exact version). Use class_name to list a class's methods, or query for a regex search.",
     obj({"query": S, "class_name": S, "max_lines": I}), h_api_reference)

# --- generic ops-backed tools --------------------------------------------------
OPS_TOOLS = [
    ("resolve_page", "open_page", "Switch the Resolve UI page.",
     obj({"page": enum(("media", "cut", "edit", "fusion", "color", "fairlight", "deliver"))}, ["page"])),
    ("resolve_layout_preset", "layout_presets", "List/load/save/update/delete UI layout presets.",
     obj({"action": enum(("list", "load", "save", "update", "delete")), "name": S})),

    ("project_list", "project_list",
     "List projects and project folders in the project manager (optionally inside a folder path like 'Clients/2026').",
     obj({"folder": S})),
    ("project_open", "project_open", "Open (load) a project by name.", obj({"name": S, "folder": S}, ["name"])),
    ("project_create", "project_create",
     "Create a new project and open it. Optional settings dict (e.g. {timelineFrameRate:'25', "
     "timelineResolutionWidth:'1920', timelineResolutionHeight:'1080'}).",
     obj({"name": S, "folder": S, "media_location": S, "settings": O}, ["name"])),
    ("project_manage", "project_manage",
     "Project housekeeping: save | close | rename | delete | export (.drp) | import | archive (.dra) | restore | "
     "create_folder | delete_folder.",
     obj({"action": enum(("save", "close", "rename", "delete", "export", "import", "archive", "restore",
                          "create_folder", "delete_folder")),
          "name": S, "path": S, "folder": S, "save": B, "with_stills_and_luts": B,
          "archive_media": B, "archive_render_cache": B, "archive_proxy": B}, ["action"])),
    ("project_settings", "project_settings",
     "Get or set project settings (or timeline settings when timeline is given). Without `set`, returns all "
     "settings, filtered by `keys` or a `filter` substring. Values are strings, e.g. {timelineFrameRate:'30', "
     "timelineResolutionWidth:'3840'}. Timeline settings need useCustomSettings:'1'.",
     obj({"set": O, "keys": arr(S), "filter": S, "timeline": {"type": ["string", "boolean"]}})),

    ("media_storage_browse", "media_storage_browse",
     "Browse disks as Resolve's Media Storage sees them: no path = mounted volumes; with path = subfolders and files.",
     obj({"path": S})),
    ("media_import", "media_import",
     "Import files or folders (absolute paths) into the media pool, optionally into a bin path (created if "
     "missing). image_sequence=true imports numbered stills as one clip.",
     obj({"paths": arr(S), "bin": S, "image_sequence": B}, ["paths"])),
    ("media_pool_list", "media_pool_list",
     "List media pool bins and clips (id, name, type, duration, fps, resolution, file path, bin).",
     obj({"bin": d(S, "Bin path like 'Master/Footage'. Default: root."), "recursive": B, "limit": I})),
    ("media_clip_info", "media_clip_info",
     "Full details for media pool clips: all clip properties, metadata, markers, flags, in/out, audio mapping.",
     obj({"clips": CLIPS}, ["clips"])),
    ("media_clip_update", "media_clip_update",
     "Modify media pool clips: rename, metadata, clip properties (Clip Attributes keys), clip color, flags, "
     "mark in/out, link/unlink proxy, replace source file.",
     obj({"clips": CLIPS, "name": S, "metadata": O, "properties": O,
          "clip_color": d({"type": ["string", "null"]}, "Orange, Apricot, Yellow, Lime, Olive, Green, Teal, Navy, "
                          "Blue, Purple, Violet, Pink, Tan, Beige, Brown, Chocolate; empty/null clears."),
          "add_flags": arr(S), "clear_flags": d(S, "Color or 'All'"), "mark_in": I, "mark_out": I,
          "clear_mark_in_out": B, "proxy_path": S, "unlink_proxy": B, "replace_path": S}, ["clips"])),
    ("media_bin", "media_bin",
     "Bins and clip organisation: create | set_current | delete (bins), move_clips | delete_clips | relink "
     "(clips to a folder path) | export_metadata (CSV) | select.",
     obj({"action": enum(("create", "set_current", "delete", "move_clips", "delete_clips", "relink",
                          "export_metadata", "select")),
          "path": S, "clips": CLIPS, "target": S}, ["action"])),

    ("timeline_list", "timeline_list", "List all timelines in the project with fps, range and track counts.", obj({})),
    ("timeline_create", "timeline_create",
     "Create a timeline (empty, or from media pool clips in order) and make it current. Optional timeline "
     "settings and start timecode.",
     obj({"name": S, "clips": arr(CLIP_SPEC), "bin": S, "settings": O, "start_timecode": S}, ["name"])),
    ("timeline_switch", "timeline_switch", "Make a timeline current (by name, id or 1-based index).",
     obj({"timeline": {"type": ["string", "integer"]}}, ["timeline"])),
    ("timeline_info", "timeline_info",
     "Timeline structure: tracks (name, enabled, locked), every item (id, name, start/end frames + timecode, "
     "source in/out, media pool clip id), markers, in/out marks, playhead, selection.",
     obj({"timeline": TIMELINE, "include_items": B})),
    ("timeline_append", "timeline_append",
     "Edit media pool clips into the timeline. Each clip may set source in/out (start_frame/end_frame), "
     "track, record_frame (where to place it) and media_type. Without record_frame clips are appended at the end.",
     obj({"clips": arr(CLIP_SPEC), "timeline": TIMELINE}, ["clips"])),
    ("timeline_item_info", "timeline_item_info",
     "Full details of timeline items: transform/crop/composite properties, markers, flags, color, fusion comps, "
     "fades, speed, linked items, grade version, node count.",
     obj({"items": ITEMS, "timeline": TIMELINE}, ["items"])),
    ("timeline_item_update", "timeline_item_update",
     "Modify timeline items. properties uses TimelineItemProperties keys, e.g. {Pan, Tilt, ZoomX, ZoomY, "
     "RotationAngle, CropLeft, CropRight, CropTop, CropBottom, Opacity, CompositeMode, FlipX, FlipY, "
     "RetimeProcess, ...} (see resolve_api_reference class_name=TimelineItemProperties). Also name, enabled, "
     "clip_color, flags. Resolve 21.1+: fades {FadeIn, FadeOut} (frames), speed {Percentage, PitchCorrection, "
     "RippleTimeline}, transition {type:'Cross Dissolve', category:'simple'|'fusion'|'ofx'|'audio', "
     "position:'start'|'end', alignment:'left'|'center'|'right', duration (frames)}, output_blanking.",
     obj({"items": ITEMS, "timeline": TIMELINE, "properties": O, "name": S, "enabled": B,
          "clip_color": {"type": ["string", "null"]}, "add_flags": arr(S), "clear_flags": S,
          "fades": O, "speed": O, "transition": O, "output_blanking": O}, ["items"])),
    ("timeline_items_delete", "timeline_items_delete", "Delete timeline items (optionally ripple delete).",
     obj({"items": ITEMS, "timeline": TIMELINE, "ripple": B}, ["items"])),
    ("timeline_items_link", "timeline_items_link", "Link or unlink timeline items (e.g. video with its audio).",
     obj({"items": ITEMS, "timeline": TIMELINE, "linked": B}, ["items"])),
    ("timeline_track", "timeline_track",
     "Tracks: add (optional audio subtype mono|stereo|5.1|...), delete, rename, enable (value), lock (value).",
     obj({"action": enum(("add", "delete", "rename", "enable", "lock")),
          "type": enum(("video", "audio", "subtitle")), "index": I, "name": S, "value": B, "subtype": S,
          "timeline": TIMELINE}, ["action"])),
    ("timeline_playhead", "timeline_playhead",
     "Get or set the playhead. position = timecode or absolute frame (relative=true: frames from timeline start).",
     obj({"position": FRAME, "relative": B, "timeline": TIMELINE})),
    ("timeline_manage", "timeline_manage",
     "Timeline operations: rename | duplicate | delete | set_start_timecode | set_mark_in_out | clear_mark_in_out | "
     "compound_clip | fusion_clip | grab_still | grab_all_stills | normalize_audio | detect_scene_cuts* | "
     "create_subtitles_from_audio* (*Studio).",
     obj({"action": enum(("rename", "duplicate", "delete", "set_start_timecode", "set_mark_in_out",
                          "clear_mark_in_out", "compound_clip", "fusion_clip", "grab_still", "grab_all_stills",
                          "normalize_audio", "detect_scene_cuts", "create_subtitles_from_audio")),
          "timeline": TIMELINE, "name": S, "timecode": S, "mark_in": FRAME, "mark_out": FRAME,
          "items": ITEMS, "settings": O, "source": I}, ["action"])),
    ("timeline_exchange", "timeline_exchange",
     "Export the timeline to an interchange file (fcpxml, fcp7xml, edl, edl_cdl, aaf, otio, drt, csv, tab, ale) "
     "or import one (AAF/EDL/XML/FCPXML/DRT/ADL/OTIO) as a new timeline.",
     obj({"action": enum(("export", "import")), "path": S, "format": S, "name": S, "options": O,
          "timeline": TIMELINE}, ["action", "path"])),
    ("timeline_insert", "timeline_insert",
     "Add a title / generator / Fusion composition. mode='place' (default) puts it on `track` (default: first free "
     "video track above V1, or a new one) at `position` for `duration` frames WITHOUT moving anything else (it is "
     "wrapped in a compound clip; max length = the effect's default, usually 5 s). mode='insert' uses Resolve's "
     "native insert edit at the playhead on V1, which ripples later clips. For Text+ titles, `text` sets the text "
     "and `title_inputs` sets Text+ inputs, e.g. {Size: 0.08, Font: 'Arial', Style: 'Bold', Red1: 1, Green1: 0.8, "
     "Blue1: 0, Center: [0.5, 0.15]}.",
     obj({"kind": enum(("generator", "fusion_generator", "ofx_generator", "title", "fusion_title",
                        "fusion_composition")),
          "name": d(S, "Effects-library name, e.g. 'Text+' (fusion_title), 'Text' / 'Scroll' (title), "
                       "'Solid Color' / '10 Step' (generator)."),
          "mode": enum(("place", "insert")), "position": FRAME, "relative": B, "track": I,
          "duration": d({"type": ["integer", "string"]}, "Frames or timecode length (place mode)."),
          "text": S, "title_inputs": O, "label": S, "bin": S, "timeline": TIMELINE}, ["kind"])),
    ("marker", "marker",
     "Markers on the timeline, a timeline item or a media pool clip: list | add | delete | update_custom_data. "
     "Timeline marker frames are relative to the timeline start (a timecode is converted). Colors: Blue, Cyan, "
     "Green, Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream.",
     obj({"action": enum(("list", "add", "delete", "update_custom_data")),
          "target": enum(("timeline", "item", "clip")), "id": d(S, "Item id or clip id when target is item/clip"),
          "frame": FRAME, "color": S, "name": S, "note": S, "duration": I, "custom_data": S,
          "timeline": TIMELINE})),

    ("color_info", "color_info",
     "Color page info for a timeline item (default: current clip): node list with labels/LUTs/tools, versions, "
     "color group.",
     obj({"item": S, "layer": I, "timeline": TIMELINE})),
    ("color_apply", "color_apply",
     "Grading actions on a timeline item (default current clip): lut (path, node) | cdl (slope/offset/power "
     "[r,g,b], saturation, node) | drx (apply grade file) | reset | node_enable | copy_grade_to (targets) | "
     "add_version | load_version | export_lut | assign_group | remove_group | stabilize | smart_reframe* | "
     "magic_mask* (*Studio).",
     obj({"action": enum(("lut", "cdl", "drx", "reset", "node_enable", "copy_grade_to", "add_version",
                          "load_version", "export_lut", "assign_group", "remove_group", "stabilize",
                          "smart_reframe", "magic_mask")),
          "item": S, "node": I, "layer": I, "path": S, "slope": arr(N), "offset": arr(N), "power": arr(N),
          "saturation": N, "grade_mode": I, "value": B, "targets": ITEMS, "name": S, "version_type": I,
          "size": S, "mode": S, "timeline": TIMELINE}, ["action"])),
    ("gallery", "gallery", "Gallery stills: list albums | export stills (to folder) | import | delete_all | create_album.",
     obj({"action": enum(("list", "export", "import", "delete_all", "create_album")),
          "album": S, "path": S, "paths": arr(S), "prefix": S, "format": d(S, "png, jpg, tif, dpx, cin, bmp, drx"),
          "name": S})),
    ("export_frame", "export_frame",
     "Save a timeline frame as an image so you can SEE the edit (then Read the file). method='render' (default) "
     "renders the fully composited frame (all tracks, titles, effects, grade) and restores render settings; "
     "method='still' is the faster Color-page still of the current clip only (no titles/upper tracks). "
     "Use .png, .jpg or .tif.",
     obj({"path": S, "position": FRAME, "relative": B, "method": enum(("render", "still")), "timeline": TIMELINE,
          "timeout": N}, ["path"])),

    ("render_options", "render_options",
     "Deliver options: render presets, formats, codecs for a format, resolutions for format+codec, current "
     "selection, quick-export presets.",
     obj({"format": S, "codec": S})),
    ("render_configure", "render_configure",
     "Configure render settings: preset, format+codec, mode (single|individual), target_dir, custom_name and any "
     "RenderSettings keys (e.g. {SelectAllFrames, MarkIn, MarkOut, ExportVideo, ExportAudio, FormatWidth, "
     "FormatHeight, FrameRate, VideoQuality, AudioCodec, ...}).",
     obj({"preset": S, "format": S, "codec": S, "mode": {"type": ["string", "integer"]}, "target_dir": S,
          "custom_name": S, "settings": O})),
    ("render_jobs", "render_jobs",
     "Render queue: list | add (current settings) | start (job_ids or all) | stop | status | delete | "
     "wait (until done or timeout seconds).",
     obj({"action": enum(("list", "add", "start", "stop", "status", "delete", "wait")),
          "job_ids": arr(S), "timeline": TIMELINE, "interactive": B, "timeout": N})),
    ("render_timeline", "render_timeline",
     "One-shot render of the current (or named) timeline: configures (preset or format/codec, default MP4 "
     "H.264), queues, renders and waits; returns the output path. Optional mark_in/mark_out range.",
     obj({"target_dir": S, "custom_name": S, "preset": S, "format": S, "codec": S, "settings": O,
          "timeline": TIMELINE, "mark_in": FRAME, "mark_out": FRAME, "wait": B, "timeout": N},
         ["target_dir"])),

    ("fusion_comp", "fusion_comp",
     "Fusion compositions on a timeline item: list (with tools) | add | import (.comp) | export | load | delete | "
     "rename | set_inputs ({tool, inputs:{InputName: value}}). For deeper Fusion scripting use resolve_exec "
     "with item.GetFusionCompByIndex(1).",
     obj({"item": S, "action": enum(("list", "add", "import", "export", "load", "delete", "rename", "set_inputs")),
          "path": S, "index": I, "name": S, "new_name": S, "tool": S, "inputs": O, "timeline": TIMELINE},
         ["item"])),
]

for _name, _fn, _desc, _schema in OPS_TOOLS:
    tool(_name, _desc, _schema, "ops:" + _fn)

ANNOTATIONS_READONLY = {"resolve_status", "resolve_api_reference", "project_list", "media_storage_browse",
                        "media_pool_list", "media_clip_info", "timeline_list", "timeline_info",
                        "timeline_item_info", "color_info", "render_options"}

# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

_out_lock = threading.Lock()


def send(msg):
    data = json.dumps(msg, ensure_ascii=False)
    with _out_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def tool_list():
    out = []
    for t in TOOLS:
        entry = {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        entry["annotations"] = {"readOnlyHint": t["name"] in ANNOTATIONS_READONLY}
        out.append(entry)
    return out


def call_tool(name, args):
    t = next((x for x in TOOLS if x["name"] == name), None)
    if t is None:
        raise ValueError("Unknown tool: %s" % name)
    h = t["handler"]
    if isinstance(h, str) and h.startswith("ops:"):
        return LINK.run(h[4:], args or {})
    return h(args or {})


def text_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Controls DaVinci Resolve (Free or Studio). Start with resolve_status; if not connected and "
                "Resolve is running with a project open, call resolve_start_bridge (once per Resolve session). "
                "Prefer the typed tools; use resolve_exec + resolve_api_reference for anything else."),
        }
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        name = params.get("name")
        try:
            return text_result(call_tool(name, params.get("arguments") or {}))
        except NotConnected as e:
            return text_result("NOT CONNECTED: %s" % e, True)
        except (OpError, ResolveError) as e:
            return text_result(str(e), True)
        except Exception as e:
            return text_result("%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-3000:]), True)
    if method == "ping":
        return {}
    if method in ("resources/list",):
        return {"resources": []}
    if method in ("prompts/list",):
        return {"prompts": []}
    if mid is None:
        return None  # notification
    raise LookupError("Method not found: %s" % method)


def main():
    # Make stdio UTF-8 and line-oriented regardless of platform defaults.
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        batch = msg if isinstance(msg, list) else [msg]
        for m in batch:
            mid = m.get("id")
            try:
                result = handle(m)
                if mid is not None:
                    send({"jsonrpc": "2.0", "id": mid, "result": result})
            except LookupError as e:
                if mid is not None:
                    send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": str(e)}})
            except Exception as e:
                if mid is not None:
                    send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
