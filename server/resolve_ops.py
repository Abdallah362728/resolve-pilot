"""ResolvePilot operations -- executed INSIDE DaVinci Resolve's Python (via the bridge) or in the
MCP server process (direct/Studio mode). Every public function has the signature
`fn(resolve, args) -> JSON-safe data` and must only depend on the `resolve` object.

Keep this file compatible with Python 3.6+ (Resolve 18-21 script hosts) and stdlib-only.
"""

import os
import re
import time

TRACK_TYPES = ("video", "audio", "subtitle")
MARKER_COLORS = ["Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
                 "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"]
CLIP_COLORS = ["Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy", "Blue",
               "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate"]
PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


class OpError(Exception):
    pass


def _need(cond, msg):
    if not cond:
        raise OpError(msg)


def _call(obj, name, *args):
    """Call an API method if this Resolve version has it; raise a clear error otherwise."""
    fn = getattr(obj, name, None)
    if fn is None:
        raise OpError("%s is not available in this DaVinci Resolve version" % name)
    return fn(*args)


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _pm(resolve):
    pm = resolve.GetProjectManager()
    _need(pm, "Project manager unavailable (is Resolve fully started?)")
    return pm


def _project(resolve):
    p = _pm(resolve).GetCurrentProject()
    _need(p, "No project is open in DaVinci Resolve. Open or create one first (project_open / project_create).")
    return p


def _media_pool(resolve):
    mp = _project(resolve).GetMediaPool()
    _need(mp, "Media pool unavailable")
    return mp


def _timeline(resolve, name=None):
    proj = _project(resolve)
    if name:
        for i in range(1, (proj.GetTimelineCount() or 0) + 1):
            tl = proj.GetTimelineByIndex(i)
            if tl and (tl.GetName() == name or _try(tl.GetUniqueId) == name):
                return tl
        raise OpError("Timeline %r not found" % name)
    tl = proj.GetCurrentTimeline()
    _need(tl, "No current timeline. Create one (timeline_create) or switch to one (timeline_switch).")
    return tl


def _fps(tl_or_proj):
    try:
        v = tl_or_proj.GetSetting("timelineFrameRate")
    except Exception:
        v = None
    if not v:
        try:
            v = tl_or_proj.GetSettings().get("timelineFrameRate")
        except Exception:
            v = None
    s = str(v or "24")
    drop = "DF" in s.upper()
    try:
        fps = float(re.sub(r"[^0-9.]", "", s) or 24)
    except ValueError:
        fps = 24.0
    return fps, drop


def frames_to_tc(frames, fps, drop=False):
    frames = int(round(frames))
    base = int(round(fps))
    if drop and base in (30, 60):
        d = 2 if base == 30 else 4
        per10 = base * 600 - d * 9
        per1 = base * 60 - d
        tens, rem = divmod(frames, per10)
        if rem < d:
            rem = d
        frames += d * 9 * tens + d * ((rem - d) // per1)
    ff = frames % base
    s = frames // base
    sep = ";" if drop else ":"
    return "%02d:%02d:%02d%s%02d" % (s // 3600, (s // 60) % 60, s % 60, sep, ff)


def tc_to_frames(tc, fps, drop=False):
    parts = [int(p) for p in re.split(r"[:;.]", str(tc).strip())]
    while len(parts) < 4:
        parts.insert(0, 0)
    hh, mm, ss, ff = parts[-4:]
    base = int(round(fps))
    total = ((hh * 60 + mm) * 60 + ss) * base + ff
    if drop and base in (30, 60):
        d = 2 if base == 30 else 4
        mins = hh * 60 + mm
        total -= d * (mins - mins // 10)
    return total


def _to_frame(value, tl, relative=False):
    """Accept an int frame, a numeric string, or a timecode string; return an absolute frame."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = int(value)
    elif re.match(r"^-?\d+$", str(value).strip()):
        f = int(str(value).strip())
    else:
        fps, drop = _fps(tl)
        f = tc_to_frames(value, fps, drop)
        return f
    if relative:
        f += tl.GetStartFrame()
    return f


# ---------------------------------------------------------------------------
# Media pool helpers
# ---------------------------------------------------------------------------

def _walk_folders(folder, path=""):
    name = folder.GetName()
    here = (path + "/" + name) if path else name
    yield here, folder
    for sub in folder.GetSubFolderList() or []:
        for item in _walk_folders(sub, here):
            yield item


def _find_folder(mp, path, create=False):
    root = mp.GetRootFolder()
    if not path or path in ("/", root.GetName()):
        return root
    parts = [p for p in re.split(r"[/\\]", path) if p]
    if parts and parts[0] == root.GetName():
        parts = parts[1:]
    cur = root
    for p in parts:
        nxt = None
        for sub in cur.GetSubFolderList() or []:
            if sub.GetName() == p:
                nxt = sub
                break
        if nxt is None:
            _need(create, "Media pool bin %r not found" % path)
            nxt = mp.AddSubFolder(cur, p)
            _need(nxt, "Could not create bin %r" % p)
        cur = nxt
    return cur


def _clip_id(clip):
    return _try(clip.GetUniqueId) or _try(clip.GetMediaId) or clip.GetName()


def _iter_clips(mp):
    for path, folder in _walk_folders(mp.GetRootFolder()):
        for clip in folder.GetClipList() or []:
            yield path, clip


def _find_clips(mp, refs):
    """Resolve clip references (unique id, media id, name, or 'bin/name' path) to MediaPoolItems."""
    if isinstance(refs, (str, int)):
        refs = [refs]
    wanted = [str(r) for r in refs]
    found = {}
    for path, clip in _iter_clips(mp):
        cid = _try(clip.GetUniqueId)
        mid = _try(clip.GetMediaId)
        name = clip.GetName()
        for w in wanted:
            if w in found:
                continue
            if w == cid or w == mid or w == name or w == path + "/" + name or \
                    w == _try(lambda: clip.GetClipProperty("File Path")):
                found[w] = clip
    missing = [w for w in wanted if w not in found]
    _need(not missing, "Media pool clip(s) not found: %s" % ", ".join(missing))
    return [found[w] for w in wanted]


def _clip_summary(clip, path=None, full=False):
    props = _try(clip.GetClipProperty) or {}
    d = {
        "id": _clip_id(clip),
        "name": clip.GetName(),
        "type": props.get("Type"),
        "duration": props.get("Duration"),
        "fps": props.get("FPS"),
        "resolution": props.get("Resolution"),
        "file_path": props.get("File Path"),
        "clip_color": _try(clip.GetClipColor),
    }
    if path is not None:
        d["bin"] = path
    if full:
        d["properties"] = props
        d["metadata"] = _try(clip.GetMetadata) or {}
        d["markers"] = _markers_out(_try(clip.GetMarkers) or {})
        d["flags"] = _try(clip.GetFlagList) or []
        d["mark_in_out"] = _try(clip.GetMarkInOut) or {}
        d["audio_mapping"] = _try(clip.GetAudioMapping)
    return d


# ---------------------------------------------------------------------------
# Timeline helpers
# ---------------------------------------------------------------------------

def _iter_items(tl, track_type=None):
    types_ = [track_type] if track_type else list(TRACK_TYPES)
    for tt in types_:
        for idx in range(1, (tl.GetTrackCount(tt) or 0) + 1):
            for item in tl.GetItemListInTrack(tt, idx) or []:
                yield tt, idx, item


def _item_id(item):
    return _try(item.GetUniqueId) or "%s@%s" % (item.GetName(), item.GetStart())


def _find_items(tl, refs):
    if isinstance(refs, (str, int)):
        refs = [refs]
    wanted = [str(r) for r in refs]
    found = {}
    for tt, idx, item in _iter_items(tl):
        iid = _item_id(item)
        for w in wanted:
            if w not in found and w == iid:
                found[w] = item
    # second pass: names (only if unambiguous)
    for w in wanted:
        if w in found:
            continue
        matches = [it for _, _, it in _iter_items(tl) if it.GetName() == w]
        if len(matches) == 1:
            found[w] = matches[0]
        elif len(matches) > 1:
            raise OpError("Timeline item name %r is ambiguous (%d matches); use its id" % (w, len(matches)))
    missing = [w for w in wanted if w not in found]
    _need(not missing, "Timeline item(s) not found: %s (use timeline_info to list ids)" % ", ".join(missing))
    return [found[w] for w in wanted]


def _item_summary(item, tt=None, idx=None, fps=None, drop=False, full=False):
    d = {
        "id": _item_id(item),
        "name": item.GetName(),
        "start": item.GetStart(),
        "end": item.GetEnd(),
        "duration": item.GetDuration(),
    }
    if fps and d["start"] is not None and d["end"] is not None:
        d["start_tc"] = frames_to_tc(d["start"], fps, drop)
        d["end_tc"] = frames_to_tc(d["end"], fps, drop)
    if tt:
        d["track"] = "%s%d" % (tt[0].upper(), idx)
    d["source_start"] = _try(item.GetSourceStartFrame)
    d["source_end"] = _try(item.GetSourceEndFrame)
    mpi = _try(item.GetMediaPoolItem)
    if mpi:
        d["media_pool_clip_id"] = _clip_id(mpi)
    t = _try(item.GetType)
    if t:
        d["type"] = t
    en = _try(item.GetClipEnabled)
    if en is not None:
        d["enabled"] = en
    if full:
        d["properties"] = _try(item.GetProperties) or _try(item.GetProperty) or {}
        d["markers"] = _markers_out(_try(item.GetMarkers) or {})
        d["flags"] = _try(item.GetFlagList) or []
        d["clip_color"] = _try(item.GetClipColor)
        d["fusion_comps"] = _try(item.GetFusionCompNameList) or []
        d["left_offset"] = _try(item.GetLeftOffset)
        d["right_offset"] = _try(item.GetRightOffset)
        d["fades"] = _try(item.GetFades)
        d["speed"] = _try(item.GetSpeed)
        d["linked_item_ids"] = [_item_id(x) for x in (_try(item.GetLinkedItems) or [])]
        d["color_version"] = _try(item.GetCurrentVersion)
        grp = _try(item.GetColorGroup)
        d["color_group"] = _try(grp.GetName) if grp else None
        g = _try(item.GetNodeGraph)
        if g:
            d["num_nodes"] = _try(g.GetNumNodes)
    return d


def _markers_out(markers):
    out = []
    for frame in sorted(markers or {}, key=lambda k: float(k)):
        m = dict(markers[frame])
        m["frame"] = frame
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# Application / status
# ---------------------------------------------------------------------------

def _playback_fps_warning(proj):
    """Resolve's scripting API cannot change timelinePlaybackFrameRate; a mismatch makes previews stutter."""
    tl = _try(proj.GetCurrentTimeline)
    play = _try(lambda: proj.GetSetting("timelinePlaybackFrameRate"))
    base = _try(lambda: (tl or proj).GetSetting("timelineFrameRate"))
    if not play or not base or _same_setting(play, str(base)):
        return None
    return ("Playback frame rate (%s) differs from the timeline frame rate (%s), so preview playback will lag. "
            "The scripting API cannot change it: ask the user to set File > Project Settings > Master Settings > "
            "Playback frame rate to %s." % (play, base, base))


def status(resolve, args):
    d = {
        "product": _try(resolve.GetProductName),
        "version": _try(resolve.GetVersionString),
        "is_studio": _try(resolve.IsStudio),
        "page": _try(resolve.GetCurrentPage),
    }
    if d["is_studio"] is None and d["product"]:
        d["is_studio"] = "Studio" in d["product"]
    pm = _try(resolve.GetProjectManager)
    proj = _try(pm.GetCurrentProject) if pm else None
    if proj:
        d["project"] = proj.GetName()
        d["timeline_count"] = proj.GetTimelineCount()
        tl = _try(proj.GetCurrentTimeline)
        if tl:
            fps, drop = _fps(tl)
            d["timeline"] = {"name": tl.GetName(), "fps": fps, "drop_frame": drop,
                             "start_tc": _try(tl.GetStartTimecode),
                             "playhead_tc": _try(tl.GetCurrentTimecode)}
        d["rendering"] = _try(proj.IsRenderingInProgress)
        warn = _playback_fps_warning(proj)
        if warn:
            d["warning"] = warn
    if pm:
        d["database"] = _try(pm.GetCurrentDatabase)
    return d


def open_page(resolve, args):
    page = str(args["page"]).lower()
    _need(page in PAGES, "page must be one of %s" % ", ".join(PAGES))
    ok = resolve.OpenPage(page)
    return {"ok": bool(ok), "page": _try(resolve.GetCurrentPage)}


def layout_presets(resolve, args):
    action = args.get("action", "list")
    name = args.get("name")
    if action == "list":
        return {"layouts": _try(resolve.GetLayoutPresetList) or []}
    fn = {"load": "LoadLayoutPreset", "save": "SaveLayoutPreset", "update": "UpdateLayoutPreset",
          "delete": "DeleteLayoutPreset"}.get(action)
    _need(fn and name, "action must be list|load|save|update|delete and name is required")
    return {"ok": bool(_call(resolve, fn, name))}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _goto_folder(pm, folder_path):
    if folder_path is None:
        return
    pm.GotoRootFolder()
    for p in [x for x in re.split(r"[/\\]", folder_path) if x]:
        _need(pm.OpenFolder(p), "Project folder %r not found" % p)


def project_list(resolve, args):
    pm = _pm(resolve)
    _goto_folder(pm, args.get("folder"))
    cur = pm.GetCurrentProject()
    out = {
        "current_project": cur.GetName() if cur else None,
        "current_folder": _try(pm.GetCurrentFolder),
        "folders": pm.GetFolderListInCurrentFolder() or [],
        "projects": pm.GetProjectListInCurrentFolder() or [],
    }
    attrs = _try(pm.GetProjectAttributesInCurrentFolder)
    if attrs:
        out["project_attributes"] = attrs
    return out


def project_open(resolve, args):
    pm = _pm(resolve)
    _goto_folder(pm, args.get("folder"))
    name = args["name"]
    cur = pm.GetCurrentProject()
    if cur and cur.GetName() == name:
        return {"ok": True, "project": name, "note": "already open"}
    p = pm.LoadProject(name)
    _need(p, "Could not open project %r (does it exist in this folder? use project_list)" % name)
    return {"ok": True, "project": p.GetName()}


def project_create(resolve, args):
    pm = _pm(resolve)
    _goto_folder(pm, args.get("folder"))
    name = args["name"]
    if args.get("media_location"):
        p = pm.CreateProject(name, args["media_location"])
    else:
        p = pm.CreateProject(name)
    _need(p, "Could not create project %r (name already used, or unsaved changes blocking?)" % name)
    out = {"ok": True, "project": p.GetName()}
    if args.get("settings"):
        out["settings"] = project_settings(resolve, {"set": args["settings"]})
    warn = _playback_fps_warning(p)
    if warn:
        out["warning"] = warn
    return out


def project_manage(resolve, args):
    pm = _pm(resolve)
    action = args["action"]
    name = args.get("name")
    path = args.get("path")
    if action == "save":
        return {"ok": bool(pm.SaveProject())}
    if action == "close":
        p = _project(resolve)
        if args.get("save", True):
            pm.SaveProject()
        return {"ok": bool(pm.CloseProject(p))}
    if action == "rename":
        _need(name, "name required")
        return {"ok": bool(_project(resolve).SetName(name))}
    if action == "delete":
        _need(name, "name required")
        _goto_folder(pm, args.get("folder"))
        return {"ok": bool(pm.DeleteProject(name))}
    if action == "export":
        _need(path, "path required (.drp)")
        pname = name or _project(resolve).GetName()
        return {"ok": bool(pm.ExportProject(pname, path, bool(args.get("with_stills_and_luts", True)))),
                "path": path}
    if action == "import":
        _need(path, "path required")
        return {"ok": bool(pm.ImportProject(path, name) if name else pm.ImportProject(path))}
    if action == "archive":
        _need(path, "path required (.dra)")
        pname = name or _project(resolve).GetName()
        return {"ok": bool(pm.ArchiveProject(pname, path, bool(args.get("archive_media", True)),
                                             bool(args.get("archive_render_cache", True)),
                                             bool(args.get("archive_proxy", False))))}
    if action == "restore":
        _need(path, "path required")
        return {"ok": bool(pm.RestoreProject(path, name) if name else pm.RestoreProject(path))}
    if action == "create_folder":
        _need(name, "name required")
        _goto_folder(pm, args.get("folder"))
        return {"ok": bool(pm.CreateFolder(name))}
    if action == "delete_folder":
        _need(name, "name required")
        _goto_folder(pm, args.get("folder"))
        return {"ok": bool(pm.DeleteFolder(name))}
    raise OpError("unknown action %r" % action)


def _same_setting(actual, wanted):
    if str(actual) == wanted:
        return True
    try:
        return abs(float(actual) - float(re.sub(r"[^0-9.\-]", "", wanted))) < 1e-6 and \
            ("DF" in wanted.upper()) == ("DF" in str(actual).upper())
    except (TypeError, ValueError):
        return False


def project_settings(resolve, args):
    p = _project(resolve)
    target = p
    if args.get("timeline"):
        target = _timeline(resolve, None if args["timeline"] is True else args["timeline"])
    if args.get("set"):
        settings = {k: (v if isinstance(v, str) else str(v)) for k, v in args["set"].items()}
        # Apply useCustomSettings and timelineFrameRate in calls of their own: Resolve 21.1 deadlocks
        # when both are set in one SetSettings call while the project contains a compound clip.
        rest = dict(settings)
        groups = [{k: rest.pop(k)} for k in ("useCustomSettings", "timelineFrameRate") if k in rest]
        if rest:
            groups.append(rest)
        for g in groups:
            res = _try(lambda: target.SetSettings(g), "missing")
            if res == "missing":  # Resolve versions without SetSettings
                for k, v in g.items():
                    target.SetSetting(k, v)
        current = _try(target.GetSettings) or _try(target.GetSetting) or {}
        failed = [k for k, v in settings.items() if not _same_setting(current.get(k), v)]
        notes = []
        if "timelineFrameRate" in failed:
            notes.append("Timeline frame rate cannot change once a timeline has clips.")
        if "timelinePlaybackFrameRate" in failed:
            notes.append("timelinePlaybackFrameRate cannot be set through the API; ask the user to set it in "
                         "File > Project Settings > Master Settings.")
        out = {"ok": not failed, "not_applied": failed, "note": " ".join(notes) or None}
        warn = _playback_fps_warning(_project(resolve))
        if warn:
            out["warning"] = warn
        return out
    allset = _try(target.GetSettings) or _try(target.GetSetting) or {}
    keys = args.get("keys")
    if keys:
        return {k: allset.get(k) for k in keys}
    flt = args.get("filter")
    if flt:
        try:
            rx = re.compile(flt, re.I)
        except re.error:
            rx = re.compile(re.escape(flt), re.I)
        return {k: v for k, v in allset.items() if rx.search(k)}
    return allset


# ---------------------------------------------------------------------------
# Media storage / media pool
# ---------------------------------------------------------------------------

def media_storage_browse(resolve, args):
    ms = resolve.GetMediaStorage()
    path = args.get("path")
    if not path:
        return {"volumes": ms.GetMountedVolumeList() or []}
    return {"path": path, "folders": ms.GetSubFolderList(path) or [], "files": ms.GetFileList(path) or []}


def media_import(resolve, args):
    mp = _media_pool(resolve)
    paths = args["paths"]
    if isinstance(paths, str):
        paths = [paths]
    expanded = []
    for p in paths:
        p = os.path.expanduser(p)
        _need(os.path.exists(p), "Path does not exist: %s" % p)
        expanded.append(os.path.abspath(p))
    prev = mp.GetCurrentFolder()
    if args.get("bin"):
        mp.SetCurrentFolder(_find_folder(mp, args["bin"], create=True))
    try:
        items = None
        if args.get("image_sequence"):
            ms = resolve.GetMediaStorage()
            items = ms.AddItemListToMediaPool([{"media": p} for p in expanded])
        else:
            items = _try(lambda: mp.ImportMedia([{"FilePath": p} for p in expanded]))
            if not items:
                items = mp.ImportMedia(expanded)
    finally:
        if args.get("bin") and prev:
            mp.SetCurrentFolder(prev)
    items = items or []
    return {"imported": len(items), "clips": [_clip_summary(c) for c in items],
            "note": None if items else "Nothing imported (unsupported/duplicate media, or path not accessible)"}


def media_pool_list(resolve, args):
    mp = _media_pool(resolve)
    root = mp.GetRootFolder()
    start = _find_folder(mp, args.get("bin")) if args.get("bin") else root
    start_path = root.GetName() if start is root else "/".join(
        [root.GetName()] + [p for p in re.split(r"[/\\]", args["bin"]) if p and p != root.GetName()])
    limit = int(args.get("limit", 500))
    if args.get("recursive", True):
        folders = [(start_path + p[len(start.GetName()):], f) for p, f in _walk_folders(start)]
        bins = [p for p, _ in folders]
    else:
        folders = [(start_path, start)]
        bins = [start_path] + [start_path + "/" + s.GetName() for s in start.GetSubFolderList() or []]
    clips = []
    for path, folder in folders:
        for c in folder.GetClipList() or []:
            if len(clips) >= limit:
                break
            clips.append(_clip_summary(c, path))
    cur = mp.GetCurrentFolder()
    sel = _try(mp.GetSelectedClips) or []
    return {"current_bin": cur.GetName() if cur else None, "bins": bins, "clips": clips,
            "truncated": len(clips) >= limit,
            "selected_clip_ids": [_clip_id(c) for c in (sel.values() if isinstance(sel, dict) else sel)]}


def media_clip_info(resolve, args):
    mp = _media_pool(resolve)
    return [_clip_summary(c, full=True) for c in _find_clips(mp, args["clips"])]


def media_clip_update(resolve, args):
    mp = _media_pool(resolve)
    results = []
    for clip in _find_clips(mp, args["clips"]):
        r = {"id": _clip_id(clip)}
        if args.get("name"):
            r["name"] = bool(clip.SetName(args["name"]))
        if args.get("metadata"):
            res = _try(lambda: clip.SetMetadata(args["metadata"]), None)
            if res is None:
                res = all(clip.SetMetadata(k, str(v)) for k, v in args["metadata"].items())
            r["metadata"] = bool(res)
        if args.get("properties"):
            r["properties"] = {k: bool(clip.SetClipProperty(k, str(v) if not isinstance(v, str) else v))
                               for k, v in args["properties"].items()}
        if "clip_color" in args:
            r["clip_color"] = bool(clip.ClearClipColor() if not args["clip_color"]
                                   else clip.SetClipColor(args["clip_color"]))
        if args.get("add_flags"):
            r["flags"] = [bool(clip.AddFlag(f)) for f in args["add_flags"]]
        if args.get("clear_flags"):
            r["clear_flags"] = bool(clip.ClearFlags(args["clear_flags"]))
        if args.get("mark_in") is not None and args.get("mark_out") is not None:
            r["mark_in_out"] = bool(clip.SetMarkInOut(int(args["mark_in"]), int(args["mark_out"])))
        if args.get("clear_mark_in_out"):
            r["clear_mark_in_out"] = bool(clip.ClearMarkInOut())
        if args.get("proxy_path"):
            r["proxy"] = bool(clip.LinkProxyMedia(args["proxy_path"]))
        if args.get("unlink_proxy"):
            r["unlink_proxy"] = bool(clip.UnlinkProxyMedia())
        if args.get("replace_path"):
            r["replace"] = bool(clip.ReplaceClip(args["replace_path"]))
        results.append(r)
    return results


def media_bin(resolve, args):
    mp = _media_pool(resolve)
    action = args["action"]
    if action == "create":
        f = _find_folder(mp, args["path"], create=True)
        return {"ok": True, "bin": f.GetName()}
    if action == "set_current":
        f = _find_folder(mp, args["path"])
        return {"ok": bool(mp.SetCurrentFolder(f))}
    if action == "delete":
        f = _find_folder(mp, args["path"])
        _need(_try(f.GetUniqueId) != _try(mp.GetRootFolder().GetUniqueId), "cannot delete the root bin")
        return {"ok": bool(mp.DeleteFolders([f]))}
    if action == "move_clips":
        clips = _find_clips(mp, args["clips"])
        target = _find_folder(mp, args["target"], create=True)
        return {"ok": bool(mp.MoveClips(clips, target))}
    if action == "delete_clips":
        clips = _find_clips(mp, args["clips"])
        return {"ok": bool(mp.DeleteClips(clips))}
    if action == "relink":
        clips = _find_clips(mp, args["clips"])
        return {"ok": bool(mp.RelinkClips(clips, args["path"]))}
    if action == "export_metadata":
        clips = _find_clips(mp, args["clips"]) if args.get("clips") else None
        return {"ok": bool(mp.ExportMetadata(args["path"], clips) if clips else mp.ExportMetadata(args["path"]))}
    if action == "select":
        clip = _find_clips(mp, args["clips"])[0]
        return {"ok": bool(_call(mp, "SetSelectedClip", clip))}
    raise OpError("unknown action %r" % action)


# ---------------------------------------------------------------------------
# Timelines
# ---------------------------------------------------------------------------

def timeline_list(resolve, args):
    proj = _project(resolve)
    cur = proj.GetCurrentTimeline()
    cur_id = _try(cur.GetUniqueId) if cur else None
    out = []
    for i in range(1, (proj.GetTimelineCount() or 0) + 1):
        tl = proj.GetTimelineByIndex(i)
        if not tl:
            continue
        fps, drop = _fps(tl)
        out.append({"index": i, "name": tl.GetName(), "id": _try(tl.GetUniqueId),
                    "current": cur is not None and (_try(tl.GetUniqueId) == cur_id if cur_id else tl.GetName() == cur.GetName()),
                    "fps": fps, "start_frame": tl.GetStartFrame(), "end_frame": tl.GetEndFrame(),
                    "start_tc": _try(tl.GetStartTimecode),
                    "tracks": {t: tl.GetTrackCount(t) for t in TRACK_TYPES}})
    return out


def _clip_infos(mp, tl, clips, for_create=False):
    infos = []
    for c in clips:
        if isinstance(c, str):
            c = {"clip": c}
        item = _find_clips(mp, [c["clip"]])[0]
        info = {"mediaPoolItem": item}
        if c.get("start_frame") is not None:
            info["startFrame"] = int(c["start_frame"])
        if c.get("end_frame") is not None:
            info["endFrame"] = int(c["end_frame"])
        if not for_create:
            if c.get("track") is not None:
                info["trackIndex"] = int(c["track"])
            if c.get("media_type"):
                info["mediaType"] = {"video": 1, "audio": 2}.get(c["media_type"], c["media_type"])
        if c.get("record_frame") is not None and tl is not None:
            info["recordFrame"] = _to_frame(c["record_frame"], tl, relative=bool(c.get("record_relative")))
        elif c.get("record_frame") is not None:
            info["recordFrame"] = int(c["record_frame"])
        if "startFrame" in info and "endFrame" not in info or "endFrame" in info and "startFrame" not in info:
            raise OpError("start_frame and end_frame must be given together (clip %s)" % c["clip"])
        infos.append(info)
    return infos


def timeline_create(resolve, args):
    proj = _project(resolve)
    mp = proj.GetMediaPool()
    name = args["name"]
    clips = args.get("clips") or []
    if args.get("bin"):
        mp.SetCurrentFolder(_find_folder(mp, args["bin"], create=True))
    if clips:
        infos = _clip_infos(mp, None, clips, for_create=True)
        tl = _try(lambda: mp.CreateTimelineFromClips(name, infos))
        if not tl and all(len(i) == 1 for i in infos):
            tl = mp.CreateTimelineFromClips(name, [i["mediaPoolItem"] for i in infos])
    else:
        tl = mp.CreateEmptyTimeline(name)
    _need(tl, "Could not create timeline %r (name taken?)" % name)
    proj.SetCurrentTimeline(tl)
    if args.get("settings"):
        project_settings(resolve, {"timeline": tl.GetName(), "set": dict({"useCustomSettings": "1"},
                                                                          **args["settings"])})
    if args.get("start_timecode"):
        tl.SetStartTimecode(args["start_timecode"])
    return {"ok": True, "timeline": tl.GetName(), "id": _try(tl.GetUniqueId)}


def timeline_switch(resolve, args):
    proj = _project(resolve)
    ref = args["timeline"]
    tl = None
    if isinstance(ref, int) or str(ref).isdigit():
        tl = proj.GetTimelineByIndex(int(ref))
    if tl is None:
        tl = _timeline(resolve, str(ref))
    _need(proj.SetCurrentTimeline(tl), "Could not switch timeline")
    return {"ok": True, "timeline": tl.GetName()}


def timeline_info(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    fps, drop = _fps(tl)
    tracks = []
    want_items = args.get("include_items", True)
    for tt in TRACK_TYPES:
        for idx in range(1, (tl.GetTrackCount(tt) or 0) + 1):
            t = {"type": tt, "index": idx, "name": _try(lambda: tl.GetTrackName(tt, idx)),
                 "enabled": _try(lambda: tl.GetIsTrackEnabled(tt, idx)),
                 "locked": _try(lambda: tl.GetIsTrackLocked(tt, idx))}
            if tt == "audio":
                t["subtype"] = _try(lambda: tl.GetTrackSubType(tt, idx))
            items = tl.GetItemListInTrack(tt, idx) or []
            t["item_count"] = len(items)
            if want_items:
                t["items"] = [_item_summary(it, tt, idx, fps, drop) for it in items]
            tracks.append(t)
    sel = _try(tl.GetSelectedClips) or []
    return {
        "name": tl.GetName(), "id": _try(tl.GetUniqueId), "fps": fps, "drop_frame": drop,
        "start_frame": tl.GetStartFrame(), "end_frame": tl.GetEndFrame(),
        "start_tc": _try(tl.GetStartTimecode), "playhead_tc": _try(tl.GetCurrentTimecode),
        "mark_in_out": _try(tl.GetMarkInOut),
        "markers": _markers_out(_try(tl.GetMarkers) or {}),
        "selected_item_ids": [_item_id(x) for x in (sel.values() if isinstance(sel, dict) else sel)],
        "tracks": tracks,
    }


def timeline_append(resolve, args):
    proj = _project(resolve)
    mp = proj.GetMediaPool()
    tl = _timeline(resolve, args.get("timeline"))
    if args.get("timeline"):
        proj.SetCurrentTimeline(tl)
    infos = _clip_infos(mp, tl, args["clips"])
    for i in infos:  # Resolve silently falls back to track 1 when the track doesn't exist
        kind = "audio" if i.get("mediaType") == 2 else "video"
        while i.get("trackIndex") and tl.GetTrackCount(kind) < i["trackIndex"]:
            _need(tl.AddTrack(kind), "could not add %s track %d" % (kind, i["trackIndex"]))
    stills = [i for i in infos if "endFrame" in i and
              _try(lambda: i["mediaPoolItem"].GetClipProperty("Type")) == "Still"]
    rest = [i for i in infos if i not in stills]
    items = []
    if rest:
        items = _try(lambda: mp.AppendToTimeline(rest))
        if not items and all(len(i) == 1 for i in rest):
            items = mp.AppendToTimeline([i["mediaPoolItem"] for i in rest])
        items = list(items or [])
    for i in stills:
        it = _append_still(mp, tl, i)
        if it:
            items.append(it)
    _need(items, "AppendToTimeline failed (check frame ranges / track index / clip type)")
    # Resolve returns a dead item (no start/end) when the target range was already occupied.
    dropped = [it for it in items if _try(it.GetStart) is None]
    items = [it for it in items if it not in dropped]
    fps, drop = _fps(tl)
    out = []
    for it in items:
        # The edit is already applied; a summary failure must not turn it into a reported error.
        try:
            tt_idx = _try(it.GetTrackTypeAndIndex) or [None, None]
            out.append(_item_summary(it, tt_idx[0], tt_idx[1], fps, drop))
        except Exception as e:
            out.append({"id": _try(lambda: _item_id(it)), "name": _try(it.GetName), "summary_error": str(e)})
    res = {"appended": len(out), "items": out}
    if dropped:
        res["not_placed"] = len(dropped)
        res["note"] = ("%d clip(s) were not placed because their range overlapped existing clips: appends never "
                       "overwrite, the existing clip wins. Delete or move the clips in the way first."
                       % len(dropped))
    return res


def _append_still(mp, tl, info):
    """Place a still at recordFrame for exactly endFrame - startFrame frames.

    AppendToTimeline ignores startFrame/endFrame for stills (they always get the standard still duration),
    but it honours the clip's mark in/out, counted in the clip's own fps (stills import at the project's
    default rate, not the timeline's). So briefly give the still the timeline fps and a matching mark range,
    append it, then restore the clip's fps and marks; items already on timelines keep their length.
    """
    clip = info["mediaPoolItem"]
    length = info["endFrame"] - info.get("startFrame", 0)
    _need(length > 0, "end_frame must be greater than start_frame")
    prev_fps = _try(lambda: clip.GetClipProperty("FPS"))
    prev_marks = _try(clip.GetMarkInOut) or {}
    fps, _ = _fps(tl)
    try:
        clip.SetClipProperty("FPS", ("%g" % fps))
        clip.SetMarkInOut(0, length - 1, "all")
        rest = {k: v for k, v in info.items() if k not in ("startFrame", "endFrame")}
        placed = mp.AppendToTimeline([rest])
    finally:
        if prev_fps is not None:
            _try(lambda: clip.SetClipProperty("FPS", str(prev_fps)))
        _try(lambda: clip.ClearMarkInOut("all"))
        for kind in ("video", "audio"):
            m = prev_marks.get(kind) or {}
            if "in" in m and "out" in m:
                _try(lambda: clip.SetMarkInOut(m["in"], m["out"], kind))
    return placed[0] if placed else None


def timeline_item_info(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    fps, drop = _fps(tl)
    out = []
    for it in _find_items(tl, args["items"]):
        tt_idx = _try(it.GetTrackTypeAndIndex) or [None, None]
        out.append(_item_summary(it, tt_idx[0], tt_idx[1], fps, drop, full=True))
    return out


def timeline_item_update(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    results = []
    for it in _find_items(tl, args["items"]):
        r = {"id": _item_id(it)}
        if args.get("properties"):
            props = args["properties"]
            if getattr(it, "SetProperties", None) is not None:
                res = _try(lambda: it.SetProperties(props), "missing")
            else:
                res = "missing"
            if res == "missing":
                res = {k: bool(it.SetProperty(k, v)) for k, v in props.items()}
            r["properties"] = res
        if args.get("name"):
            r["name"] = bool(_call(it, "SetName", args["name"]))
        if "enabled" in args:
            r["enabled"] = bool(it.SetClipEnabled(bool(args["enabled"])))
        if "clip_color" in args:
            r["clip_color"] = bool(it.ClearClipColor() if not args["clip_color"] else it.SetClipColor(args["clip_color"]))
        if args.get("add_flags"):
            r["flags"] = [bool(it.AddFlag(f)) for f in args["add_flags"]]
        if args.get("clear_flags"):
            r["clear_flags"] = bool(it.ClearFlags(args["clear_flags"]))
        if args.get("fades"):
            r["fades"] = _call(it, "SetFades", args["fades"])
        if args.get("speed"):
            r["speed"] = _call(it, "SetSpeed", args["speed"])
        if args.get("transition"):
            r["transition"] = _add_transition(it, args["transition"])
        if args.get("output_blanking"):
            r["output_blanking"] = _call(it, "SetOutputBlanking", args["output_blanking"])
        if args.get("fusion_output_cache"):
            r["fusion_output_cache"] = bool(it.SetFusionOutputCache(_cache_const(resolve, args["fusion_output_cache"])))
        results.append(r)
    return results


def _cache_const(resolve, value):
    """SetFusionOutputCache only accepts resolve.CACHE_* constants, not strings like "auto"."""
    key = {"auto": "CACHE_AUTO_ENABLED", "on": "CACHE_ENABLED", "enabled": "CACHE_ENABLED",
           "off": "CACHE_DISABLED", "disabled": "CACHE_DISABLED"}.get(str(value).lower())
    _need(key, "fusion_output_cache must be auto|on|off")
    return getattr(resolve, key)


def _add_transition(item, opts):
    """AddTransition with sensible defaults. Centred transitions need media handles; when there are
    none, fall back to the alignment that stays inside the clip (start->right, end->left)."""
    opts = dict(opts)
    opts.setdefault("type", "Cross Dissolve")
    opts.setdefault("category", "simple")
    opts.setdefault("position", "start")
    tries = [opts]
    if "alignment" not in opts:
        inside = "right" if opts["position"] == "start" else "left"
        tries = [dict(opts, alignment="center"), dict(opts, alignment=inside)]
    for o in tries:
        t = _call(item, "AddTransition", o)
        if t:
            return {"ok": True, "options": o, "start": _try(t.GetStart), "end": _try(t.GetEnd)}
    raise OpError("AddTransition failed for %s (unknown type/category, or no room for the transition)" % opts)


def timeline_items_delete(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    items = _find_items(tl, args["items"])
    return {"ok": bool(tl.DeleteClips(items, bool(args.get("ripple", False)))), "deleted": len(items)}


def timeline_items_link(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    items = _find_items(tl, args["items"])
    return {"ok": bool(tl.SetClipsLinked(items, bool(args.get("linked", True))))}


def timeline_track(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    action = args["action"]
    tt = args.get("type", "video")
    _need(tt in TRACK_TYPES, "type must be video|audio|subtitle")
    idx = args.get("index")
    if action == "add":
        ok = tl.AddTrack(tt, args["subtype"]) if args.get("subtype") else tl.AddTrack(tt)
        return {"ok": bool(ok), "count": tl.GetTrackCount(tt)}
    _need(idx is not None, "index is required")
    idx = int(idx)
    if action == "delete":
        return {"ok": bool(tl.DeleteTrack(tt, idx))}
    if action == "rename":
        return {"ok": bool(tl.SetTrackName(tt, idx, args["name"]))}
    if action == "enable":
        return {"ok": bool(tl.SetTrackEnable(tt, idx, bool(args.get("value", True))))}
    if action == "lock":
        return {"ok": bool(tl.SetTrackLock(tt, idx, bool(args.get("value", True))))}
    raise OpError("unknown action %r" % action)


def timeline_playhead(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    pos = args.get("position")
    if pos is not None:
        if isinstance(pos, int) or re.match(r"^-?\d+$", str(pos)):
            fps, drop = _fps(tl)
            frame = int(pos)
            if args.get("relative"):
                frame += tl.GetStartFrame()
            tc = frames_to_tc(frame, fps, drop)
        else:
            tc = str(pos)
        _need(tl.SetCurrentTimecode(tc), "Could not set playhead to %s (open the Edit/Cut/Color page?)" % tc)
    cur = tl.GetCurrentTimecode()
    fps, drop = _fps(tl)
    return {"timecode": cur, "frame": tc_to_frames(cur, fps, drop) if cur else None}


class _OnPage(object):
    """Temporarily switch to `page` (if not already there) and switch back afterwards."""

    def __init__(self, resolve, page, avoid=None):
        self.resolve, self.page, self.avoid, self.prev = resolve, page, avoid, None

    def __enter__(self):
        self.prev = _try(self.resolve.GetCurrentPage)
        if self.prev != self.page and (self.avoid is None or self.prev in self.avoid):
            self.resolve.OpenPage(self.page)
        return self

    def __exit__(self, *exc):
        if self.prev and _try(self.resolve.GetCurrentPage) != self.prev:
            _try(lambda: self.resolve.OpenPage(self.prev))
        return False


def timeline_manage(resolve, args):
    proj = _project(resolve)
    action = args["action"]
    tl = _timeline(resolve, args.get("timeline"))
    if action == "rename":
        return {"ok": bool(tl.SetName(args["name"]))}
    if action == "duplicate":
        new = tl.DuplicateTimeline(args["name"]) if args.get("name") else tl.DuplicateTimeline()
        _need(new, "duplicate failed")
        return {"ok": True, "timeline": new.GetName()}
    if action == "delete":
        return {"ok": bool(proj.GetMediaPool().DeleteTimelines([tl]))}
    if action == "set_start_timecode":
        return {"ok": bool(tl.SetStartTimecode(args["timecode"]))}
    if action == "set_mark_in_out":
        # On the Deliver page marks are the render range in different units; use the Edit page.
        with _OnPage(resolve, "edit", avoid=("deliver",)):
            return {"ok": bool(tl.SetMarkInOut(_to_frame(args["mark_in"], tl), _to_frame(args["mark_out"], tl)))}
    if action == "clear_mark_in_out":
        with _OnPage(resolve, "edit", avoid=("deliver",)):
            return {"ok": bool(tl.ClearMarkInOut())}
    if action == "detect_scene_cuts":
        return {"ok": bool(_call(tl, "DetectSceneCuts"))}
    if action == "create_subtitles_from_audio":
        return {"ok": bool(_call(tl, "CreateSubtitlesFromAudio", args.get("settings") or {}))}
    if action == "grab_still":
        return {"ok": bool(tl.GrabStill())}
    if action == "grab_all_stills":
        return {"ok": bool(tl.GrabAllStills(int(args.get("source", 2))))}
    if action == "compound_clip":
        items = _find_items(tl, args["items"])
        info = {"name": args["name"]} if args.get("name") else {}
        return {"ok": bool(tl.CreateCompoundClip(items, info))}
    if action == "fusion_clip":
        items = _find_items(tl, args["items"])
        return {"ok": bool(tl.CreateFusionClip(items))}
    if action == "normalize_audio":
        items = _find_items(tl, args["items"])
        opts = args.get("settings")
        return {"ok": bool(_call(tl, "NormalizeAudioLevel", items, opts) if opts else _call(tl, "NormalizeAudioLevel", items))}
    raise OpError("unknown action %r" % action)


EXPORT_FORMATS = {
    # name: (type constant, subtype constant or None, default extension)
    "aaf": ("EXPORT_AAF", "EXPORT_AAF_NEW", ".aaf"),
    "drt": ("EXPORT_DRT", None, ".drt"),
    "edl": ("EXPORT_EDL", "EXPORT_NONE", ".edl"),
    "edl_cdl": ("EXPORT_EDL", "EXPORT_CDL", ".edl"),
    "fcp7xml": ("EXPORT_FCP_7_XML", None, ".xml"),
    "fcpxml": ("EXPORT_FCPXML_1_10", None, ".fcpxml"),
    "fcpxml_1_9": ("EXPORT_FCPXML_1_9", None, ".fcpxml"),
    "fcpxml_1_8": ("EXPORT_FCPXML_1_8", None, ".fcpxml"),
    "otio": ("EXPORT_OTIO", None, ".otio"),
    "csv": ("EXPORT_TEXT_CSV", None, ".csv"),
    "tab": ("EXPORT_TEXT_TAB", None, ".txt"),
    "ale": ("EXPORT_ALE", None, ".ale"),
}


def timeline_exchange(resolve, args):
    action = args["action"]
    path = os.path.abspath(os.path.expanduser(args["path"]))
    if action == "export":
        tl = _timeline(resolve, args.get("timeline"))
        fmt = args.get("format", "fcpxml").lower()
        _need(fmt in EXPORT_FORMATS, "format must be one of %s" % ", ".join(sorted(EXPORT_FORMATS)))
        t_name, st_name, ext = EXPORT_FORMATS[fmt]
        t = getattr(resolve, t_name, None)
        _need(t is not None, "Export format %s not supported by this Resolve version" % fmt)
        st = getattr(resolve, st_name, None) if st_name else getattr(resolve, "EXPORT_NONE", None)
        if not os.path.splitext(path)[1]:
            path += ext
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        ok = tl.Export(path, t, st) if st is not None else tl.Export(path, t)
        return {"ok": bool(ok), "path": path, "exists": os.path.exists(path)}
    if action == "import":
        _need(os.path.exists(path), "File not found: %s" % path)
        mp = _media_pool(resolve)
        opts = dict(args.get("options") or {})
        if args.get("name"):
            opts["timelineName"] = args["name"]
        tl = mp.ImportTimelineFromFile(path, opts) if opts else mp.ImportTimelineFromFile(path)
        _need(tl, "Import failed (unsupported file or media could not be linked)")
        return {"ok": True, "timeline": tl.GetName()}
    raise OpError("action must be export|import")


INSERT_FUNCS = {"generator": "InsertGeneratorIntoTimeline", "fusion_generator": "InsertFusionGeneratorIntoTimeline",
                "ofx_generator": "InsertOFXGeneratorIntoTimeline", "title": "InsertTitleIntoTimeline",
                "fusion_title": "InsertFusionTitleIntoTimeline",
                "fusion_composition": "InsertFusionCompositionIntoTimeline"}


def _insert_raw(tl, kind, name):
    fn = INSERT_FUNCS.get(kind)
    _need(fn, "kind must be one of %s" % "|".join(sorted(INSERT_FUNCS)))
    item = _call(tl, fn) if kind == "fusion_composition" else _call(tl, fn, name)
    _need(item, "Insert failed (unknown %s name %r? Names match the Effects library, e.g. 'Text+', "
                "'Solid Color', '10 Step', 'Scroll')" % (kind, name))
    return item


def _style_title(item, args):
    out = {}
    if args.get("text") is not None:
        out["text_set"] = _set_title_text(item, args["text"])
    if args.get("title_inputs"):
        out["title_inputs"] = _set_title_inputs(item, args["title_inputs"])
    return out


def _track_free(tl, track, start, end):
    for it in tl.GetItemListInTrack("video", track) or []:
        if it.GetStart() < end and it.GetEnd() > start:
            return False
    return True


def timeline_insert(resolve, args):
    """Add a generator/title/Fusion composition.

    mode="place" (default): build it in a temporary timeline, wrap it in a compound clip and place it on
    `track` (default: the lowest video track above V1 that is free over the range, else a new track) at
    `position` for `duration` frames. Nothing else on the timeline moves.
    mode="insert": Resolve's native insert edit at the playhead on the destination track (usually V1),
    which RIPPLES everything after the playhead and splits the clip under it.
    """
    proj = _project(resolve)
    mp = proj.GetMediaPool()
    tl = _timeline(resolve, args.get("timeline"))
    if args.get("timeline"):
        proj.SetCurrentTimeline(tl)
    kind, name = args["kind"], args.get("name")
    fps, drop = _fps(tl)
    mode = args.get("mode", "place")

    if mode == "insert":
        if args.get("position") is not None:
            timeline_playhead(resolve, {"position": args["position"], "relative": args.get("relative")})
        item = _insert_raw(tl, kind, name)
        tt_idx = _try(item.GetTrackTypeAndIndex) or [None, None]
        out = _item_summary(item, tt_idx[0], tt_idx[1], fps, drop)
        out.update(_style_title(item, args))
        return out

    _need(mode == "place", "mode must be place|insert")
    if args.get("position") is not None:
        start = _to_frame(args["position"], tl, relative=bool(args.get("relative")))
    else:
        cur = tl.GetCurrentTimecode()
        start = tc_to_frames(cur, fps, drop) if cur else tl.GetStartFrame()

    # 1. Build the effect in a scratch timeline.
    prev_folder = mp.GetCurrentFolder()
    mp.SetCurrentFolder(_find_folder(mp, args.get("bin") or "ResolvePilot/Generated", create=True))
    tmp = mp.CreateEmptyTimeline("__resolvepilot_tmp_%d" % int(time.time() * 1000))
    _need(tmp, "could not create a scratch timeline")
    try:
        proj.SetCurrentTimeline(tmp)
        tmp.SetCurrentTimecode(tmp.GetStartTimecode())
        item = _insert_raw(tmp, kind, name)
        styled = _style_title(item, args)
        native_len = item.GetDuration()
        label = args.get("label") or "%s - %s" % (name or kind, (args.get("text") or "")[:40])
        comp = tmp.CreateCompoundClip([item], {"name": label.strip(" -")})
        _need(comp, "could not wrap the item in a compound clip")
        mpi = comp.GetMediaPoolItem()
    finally:
        proj.SetCurrentTimeline(tl)
        mp.DeleteTimelines([tmp])
        if prev_folder:
            mp.SetCurrentFolder(prev_folder)

    # 2. Place it without touching other items.
    dur = args.get("duration")
    if dur is None:
        dur = native_len
    elif not (isinstance(dur, (int, float)) or re.match(r"^\d+$", str(dur))):
        dur = tc_to_frames(dur, fps, drop)
    dur = int(dur)
    _need(dur > 0, "duration must be positive")
    if dur > native_len:
        dur = native_len  # a compound cannot be longer than its content
    track = args.get("track")
    if track is None:
        n = tl.GetTrackCount("video")
        track = next((t for t in range(2, n + 1) if _track_free(tl, t, start, start + dur)), None)
        if track is None:
            tl.AddTrack("video")
            track = tl.GetTrackCount("video")
    track = int(track)
    while tl.GetTrackCount("video") < track:
        tl.AddTrack("video")
    placed = mp.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": 0, "endFrame": dur,
                                   "trackIndex": track, "recordFrame": start, "mediaType": 1}])
    _need(placed, "placing the compound clip failed (is the track locked?)")
    it = placed[0]
    tt_idx = _try(it.GetTrackTypeAndIndex) or ["video", track]
    out = _item_summary(it, tt_idx[0], tt_idx[1], fps, drop)
    out.update(styled)
    out["media_pool_clip_id"] = _clip_id(mpi)
    out["note"] = ("Placed as a compound clip so nothing rippled (max length %d frames). To edit the text by hand: "
                   "right-click the clip > Open in Timeline, select the Text+ clip inside, edit it in the "
                   "Inspector, then click the timeline name in the breadcrumb to go back. Resolve's API cannot "
                   "place a bare Text+ on an upper track; mode=\"insert\" puts a directly editable Text+ on V1 "
                   "but ripples that track." % native_len)
    return out


def _set_title_inputs(item, inputs):
    """Set Text+ tool inputs such as Size, Font, Style, Red1/Green1/Blue1, Center."""
    res = {}
    try:
        comp = item.GetFusionCompByIndex(1)
        tools = comp.GetToolList(False, "TextPlus") or {}
        tool = next(iter(tools.values() if isinstance(tools, dict) else tools), None)
        _need(tool, "no Text+ tool in this item")
        for k, v in inputs.items():
            if isinstance(v, (list, tuple)):
                v = {i + 1: x for i, x in enumerate(v)}  # e.g. Center = [0.5, 0.2]
            res[k] = _try(lambda: tool.SetInput(k, v), "error") != "error"
    except OpError:
        raise
    except Exception as e:
        raise OpError("could not set title inputs: %s" % e)
    return res


def _set_title_text(item, text):
    """Set the text of a Fusion title (Text+) or a standard title."""
    try:
        if (item.GetFusionCompCount() or 0) > 0:
            comp = item.GetFusionCompByIndex(1)
            tools = comp.GetToolList(False, "TextPlus") or {}
            for t in (tools.values() if isinstance(tools, dict) else tools):
                t.SetInput("StyledText", text)
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Markers (timeline, timeline item, media pool clip)
# ---------------------------------------------------------------------------

def _marker_target(resolve, args):
    target = args.get("target", "timeline")
    if target == "timeline":
        return _timeline(resolve, args.get("timeline")), True
    if target == "item":
        tl = _timeline(resolve, args.get("timeline"))
        return _find_items(tl, [args["id"]])[0], False
    if target == "clip":
        return _find_clips(_media_pool(resolve), [args["id"]])[0], False
    raise OpError("target must be timeline|item|clip")


def marker(resolve, args):
    obj, is_tl = _marker_target(resolve, args)
    action = args.get("action", "list")
    if action == "list":
        return _markers_out(obj.GetMarkers() or {})
    if action == "add":
        color = args.get("color", "Blue")
        _need(color in MARKER_COLORS, "color must be one of %s" % ", ".join(MARKER_COLORS))
        frame = args["frame"]
        if is_tl:
            # Timeline markers use frames relative to the timeline start.
            if not (isinstance(frame, int) or re.match(r"^-?\d+$", str(frame))):
                frame = _to_frame(frame, obj) - obj.GetStartFrame()
        frame = int(frame)
        cd = args.get("custom_data")
        a = [frame, color, args.get("name", ""), args.get("note", ""), int(args.get("duration", 1))]
        ok = obj.AddMarker(*(a + [cd] if cd else a))
        _need(ok, "AddMarker failed (a marker may already exist at frame %s, or frame is out of range)" % frame)
        return {"ok": True, "frame": frame}
    if action == "delete":
        if args.get("frame") is not None:
            return {"ok": bool(obj.DeleteMarkerAtFrame(int(args["frame"])))}
        if args.get("custom_data"):
            return {"ok": bool(obj.DeleteMarkerByCustomData(args["custom_data"]))}
        return {"ok": bool(obj.DeleteMarkersByColor(args.get("color", "All")))}
    if action == "update_custom_data":
        return {"ok": bool(obj.UpdateMarkerCustomData(int(args["frame"]), args["custom_data"]))}
    raise OpError("action must be list|add|delete|update_custom_data")


# ---------------------------------------------------------------------------
# Color
# ---------------------------------------------------------------------------

def _graph(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    if args.get("item"):
        it = _find_items(tl, [args["item"]])[0]
    else:
        it = tl.GetCurrentVideoItem()
        _need(it, "No current video item; pass item")
    g = _try(lambda: it.GetNodeGraph(int(args["layer"])) if args.get("layer") else it.GetNodeGraph())
    return tl, it, g


def lut_roots():
    """Resolve LUT folders, preferred first. System folders on macOS/Linux are usually root-owned, so
    per-user folders follow as fallbacks."""
    if os.name == "nt":
        return [os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Blackmagic Design",
                             "DaVinci Resolve", "Support", "LUT")]
    if os.path.exists("/Library/Application Support/Blackmagic Design") or os.path.exists("/Applications"):
        return ["/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
                os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT")]
    return ["/opt/resolve/LUT", os.path.expanduser("~/.local/share/DaVinciResolve/LUT")]


def _install_lut(path):
    import shutil
    roots = [os.environ["RESOLVE_PILOT_LUT_DIR"]] if os.environ.get("RESOLVE_PILOT_LUT_DIR") else lut_roots()
    errors = []
    for root in roots:
        dest_dir = os.path.join(root, "ResolvePilot")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(path))
            if os.path.abspath(dest) != os.path.abspath(path):
                shutil.copyfile(path, dest)
            return dest
        except OSError as e:
            errors.append("%s: %s" % (dest_dir, e))
    raise OpError("Resolve only applies LUTs from its LUT folders and copying there failed (%s). Copy the LUT "
                  "into a Resolve LUT folder manually or set RESOLVE_PILOT_LUT_DIR." % "; ".join(errors))


def color_info(resolve, args):
    tl, it, g = _graph(resolve, args)
    out = {"item": _item_id(it), "name": it.GetName(),
           "version": _try(it.GetCurrentVersion),
           "local_versions": _try(lambda: it.GetVersionNameList(0)),
           "remote_versions": _try(lambda: it.GetVersionNameList(1))}
    grp = _try(it.GetColorGroup)
    out["color_group"] = _try(grp.GetName) if grp else None
    nodes = []
    if g:
        for n in range(1, (g.GetNumNodes() or 0) + 1):
            nodes.append({"index": n, "label": _try(lambda: g.GetNodeLabel(n)), "lut": _try(lambda: g.GetLUT(n)),
                          "tools": _try(lambda: g.GetToolsInNode(n)),
                          "cache_mode": _try(lambda: g.GetNodeCacheMode(n))})
    else:
        num = _try(it.GetNumNodes) or 0
        for n in range(1, num + 1):
            nodes.append({"index": n, "label": _try(lambda: it.GetNodeLabel(n)), "lut": _try(lambda: it.GetLUT(n))})
    out["nodes"] = nodes
    out["groups"] = [_try(x.GetName) for x in (_try(_project(resolve).GetColorGroupsList) or [])]
    return out


def color_apply(resolve, args):
    tl, it, g = _graph(resolve, args)
    action = args["action"]
    node = int(args.get("node", 1))
    if action == "lut":
        target = g or it
        path = args["path"]
        ok = target.SetLUT(node, path)
        installed = None
        if not ok and os.path.isfile(path):
            # Resolve only applies LUTs found in its LUT folders: install a copy, refresh, retry.
            installed = _install_lut(path)
            _try(_project(resolve).RefreshLUTList)
            ok = target.SetLUT(node, installed)
        return {"ok": bool(ok), "lut": _try(lambda: target.GetLUT(node)), "installed_copy": installed}
    if action == "cdl":
        cdl = {"NodeIndex": str(node)}
        for k_in, k_out in (("slope", "Slope"), ("offset", "Offset"), ("power", "Power"), ("saturation", "Saturation")):
            v = args.get(k_in)
            if v is not None:
                cdl[k_out] = " ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
        return {"ok": bool(it.SetCDL(cdl))}
    if action == "drx":
        _need(g, "Node graph API unavailable in this version")
        return {"ok": bool(g.ApplyGradeFromDRX(args["path"], int(args.get("grade_mode", 0))))}
    if action == "reset":
        _need(g, "Node graph API unavailable in this version")
        return {"ok": bool(g.ResetAllGrades())}
    if action == "node_enable":
        return {"ok": bool(_call(g, "SetNodeEnabled", node, bool(args.get("value", True))))}
    if action == "copy_grade_to":
        targets = _find_items(tl, args["targets"])
        return {"ok": bool(it.CopyGrades(targets))}
    if action == "add_version":
        return {"ok": bool(it.AddVersion(args["name"], int(args.get("version_type", 0))))}
    if action == "load_version":
        return {"ok": bool(it.LoadVersionByName(args["name"], int(args.get("version_type", 0))))}
    if action == "export_lut":
        size = {"17": 0, "33": 1, "65": 2, "panasonic": 3}.get(str(args.get("size", "33")), 1)
        return {"ok": bool(it.ExportLUT(size, args["path"]))}
    if action == "assign_group":
        proj = _project(resolve)
        grp = None
        for x in proj.GetColorGroupsList() or []:
            if x.GetName() == args["name"]:
                grp = x
        if grp is None:
            grp = proj.AddColorGroup(args["name"])
        return {"ok": bool(it.AssignToColorGroup(grp))}
    if action == "remove_group":
        return {"ok": bool(it.RemoveFromColorGroup())}
    if action == "stabilize":
        return {"ok": bool(_call(it, "Stabilize"))}
    if action == "smart_reframe":
        return {"ok": bool(_call(it, "SmartReframe"))}
    if action == "magic_mask":
        return {"ok": bool(_call(it, "CreateMagicMask", args.get("mode", "F")))}
    raise OpError("unknown action %r" % action)


def gallery(resolve, args):
    gal = _project(resolve).GetGallery()
    action = args.get("action", "list")
    albums = gal.GetGalleryStillAlbums() or []
    if action == "list":
        out = []
        for a in albums:
            stills = a.GetStills() or []
            out.append({"name": _try(lambda: gal.GetAlbumName(a)), "stills": [_try(lambda: a.GetLabel(s)) for s in stills]})
        return {"albums": out, "powergrade_albums": [_try(lambda: gal.GetAlbumName(a)) for a in (_try(gal.GetGalleryPowerGradeAlbums) or [])]}
    album = gal.GetCurrentStillAlbum()
    if args.get("album"):
        for a in albums:
            if _try(lambda: gal.GetAlbumName(a)) == args["album"]:
                album = a
    if action == "export":
        stills = album.GetStills() or []
        os.makedirs(args["path"], exist_ok=True)
        ok = album.ExportStills(stills, args["path"], args.get("prefix", "still"), args.get("format", "png"))
        return {"ok": bool(ok), "count": len(stills), "path": args["path"]}
    if action == "import":
        return {"ok": bool(album.ImportStills(args["paths"]))}
    if action == "delete_all":
        return {"ok": bool(album.DeleteStills(album.GetStills() or []))}
    if action == "create_album":
        a = gal.CreateGalleryStillAlbum()
        if a and args.get("name"):
            gal.SetAlbumName(a, args["name"])
        return {"ok": bool(a)}
    raise OpError("action must be list|export|import|delete_all|create_album")


def export_frame(resolve, args):
    """Save one frame as an image.

    method="render" (default): renders the fully composited timeline frame (all tracks, titles, effects,
    grades) through the Deliver engine, then restores the previous render format/codec and removes the job.
    method="still": Resolve's gallery still export from the Color page; faster, but shows only the current
    clip (no titles or upper tracks).
    """
    proj = _project(resolve)
    tl = _timeline(resolve, args.get("timeline"))
    fps, drop = _fps(tl)
    if args.get("position") is not None:
        timeline_playhead(resolve, {"position": args["position"], "relative": args.get("relative")})
    path = os.path.abspath(os.path.expanduser(args["path"]))
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    if os.path.exists(path):
        os.remove(path)

    if args.get("method", "render") == "still":
        ok = _call(proj, "ExportCurrentFrameAsStill", path)
        if not ok or not os.path.exists(path):
            # Resolve only exports stills from the Color page; switch there briefly.
            page = _try(resolve.GetCurrentPage)
            resolve.OpenPage("color")
            ok = _call(proj, "ExportCurrentFrameAsStill", path)
            if page and page != "color":
                resolve.OpenPage(page)
        return {"ok": bool(ok) and os.path.exists(path), "path": path, "method": "still"}

    _need(not proj.IsRenderingInProgress(), "A render is in progress; try again when it finishes")
    cur = tl.GetCurrentTimecode()
    frame = tc_to_frames(cur, fps, drop) if cur else tl.GetStartFrame()
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    fmt, codec = {"png": ("png", "RGB8"), "jpg": ("jpg", "YUV444_8"), "jpeg": ("jpg", "YUV444_8"),
                  "tif": ("tif", "RGB8"), "tiff": ("tif", "RGB8")}.get(ext, ("png", "RGB8"))
    prev_page = _try(resolve.GetCurrentPage)
    prev_fc = _try(proj.GetCurrentRenderFormatAndCodec) or {}
    prev_mode = _try(proj.GetCurrentRenderMode)
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="resolvepilot_frame_")
    jid = None
    try:
        _need(proj.SetCurrentRenderFormatAndCodec(fmt, codec), "Image render format %s/%s unavailable" % (fmt, codec))
        proj.SetCurrentRenderMode(1)
        _need(proj.SetRenderSettings({"SelectAllFrames": False, "MarkIn": frame, "MarkOut": frame,
                                      "TargetDir": tmp, "CustomName": "frame", "ExportVideo": True,
                                      "ExportAudio": False}), "Could not configure the frame render")
        jid = proj.AddRenderJob()
        _need(jid, "Could not queue the frame render")
        _need(proj.StartRendering([jid], False), "Could not start the frame render")
        t0 = time.time()
        time.sleep(0.3)
        while proj.IsRenderingInProgress() and time.time() - t0 < float(args.get("timeout", 120)):
            time.sleep(0.2)
        files = [f for f in os.listdir(tmp) if f.lower().endswith("." + fmt)]
        _need(files, "Frame render produced no file (status: %s)" % _try(lambda: proj.GetRenderJobStatus(jid)))
        shutil.move(os.path.join(tmp, sorted(files)[0]), path)
    finally:
        if jid:
            _try(lambda: proj.DeleteRenderJob(jid))
        if prev_fc.get("format") and prev_fc.get("codec"):
            _try(lambda: proj.SetCurrentRenderFormatAndCodec(prev_fc["format"], prev_fc["codec"]))
        if prev_mode is not None:
            _try(lambda: proj.SetCurrentRenderMode(prev_mode))
        # The one-frame MarkIn/MarkOut would otherwise stick and make the next render a single frame.
        _try(lambda: proj.SetRenderSettings({"SelectAllFrames": True}))
        shutil.rmtree(tmp, ignore_errors=True)
        if prev_page and _try(resolve.GetCurrentPage) != prev_page:
            _try(lambda: resolve.OpenPage(prev_page))
    return {"ok": os.path.exists(path), "path": path, "method": "render", "frame": frame,
            "timecode": frames_to_tc(frame, fps, drop)}


# ---------------------------------------------------------------------------
# Render / Deliver
# ---------------------------------------------------------------------------

def render_options(resolve, args):
    p = _project(resolve)
    out = {"presets": _try(p.GetRenderPresetList) or [],
           "current": _try(p.GetCurrentRenderFormatAndCodec),
           "render_mode": _try(p.GetCurrentRenderMode)}
    fmts = _try(p.GetRenderFormats) or {}
    out["formats"] = fmts
    fmt = args.get("format")
    if fmt:
        ext = fmts.get(fmt, fmt)
        out["codecs"] = _try(lambda: p.GetRenderCodecs(ext)) or {}
        if args.get("codec"):
            out["resolutions"] = _try(lambda: p.GetRenderResolutions(ext, args["codec"]))
    qe = _try(p.GetQuickExportRenderPresets)
    if qe is not None:
        out["quick_export_presets"] = qe
    return out


def render_configure(resolve, args):
    p = _project(resolve)
    res = {}
    if args.get("preset"):
        res["preset"] = bool(p.LoadRenderPreset(args["preset"]))
        _need(res["preset"], "Unknown render preset %r" % args["preset"])
    if args.get("format") and args.get("codec"):
        fmts = _try(p.GetRenderFormats) or {}
        fmt = fmts.get(args["format"], args["format"])
        res["format_codec"] = bool(p.SetCurrentRenderFormatAndCodec(fmt, args["codec"]))
        _need(res["format_codec"], "Invalid format/codec %s/%s (see render_options)" % (fmt, args["codec"]))
    if args.get("mode") is not None:
        res["mode"] = bool(p.SetCurrentRenderMode(1 if args["mode"] in (1, "single") else 0))
    settings = dict(args.get("settings") or {})
    if args.get("target_dir"):
        td = os.path.abspath(os.path.expanduser(args["target_dir"]))
        if not os.path.isdir(td):
            os.makedirs(td)
        settings["TargetDir"] = td
    if args.get("custom_name"):
        settings["CustomName"] = args["custom_name"]
    if settings:
        res["settings"] = bool(p.SetRenderSettings(settings))
        _need(res["settings"], "SetRenderSettings rejected %s" % settings)
    res["current"] = _try(p.GetCurrentRenderFormatAndCodec)
    return res


def render_jobs(resolve, args):
    p = _project(resolve)
    action = args.get("action", "list")
    if action == "list":
        jobs = p.GetRenderJobList() or []
        for j in jobs:
            st = _try(lambda: p.GetRenderJobStatus(j.get("JobId")))
            if st:
                j["status"] = st
        return {"rendering": p.IsRenderingInProgress(), "jobs": jobs}
    if action == "add":
        if args.get("timeline"):
            p.SetCurrentTimeline(_timeline(resolve, args["timeline"]))
        jid = p.AddRenderJob()
        _need(jid, "AddRenderJob failed (set a target dir with render_configure first)")
        return {"job_id": jid}
    if action == "start":
        ids = args.get("job_ids") or [j["JobId"] for j in (p.GetRenderJobList() or [])]
        _need(ids, "No render jobs in queue")
        ok = p.StartRendering(ids, bool(args.get("interactive", False)))
        return {"ok": bool(ok), "job_ids": ids}
    if action == "stop":
        p.StopRendering()
        return {"ok": True}
    if action == "status":
        ids = args.get("job_ids") or [j["JobId"] for j in (p.GetRenderJobList() or [])]
        return {"rendering": p.IsRenderingInProgress(), "status": {i: p.GetRenderJobStatus(i) for i in ids}}
    if action == "delete":
        if args.get("job_ids"):
            return {"ok": all(p.DeleteRenderJob(i) for i in args["job_ids"])}
        return {"ok": bool(p.DeleteAllRenderJobs())}
    if action == "wait":
        timeout = float(args.get("timeout", 600))
        t0 = time.time()
        while p.IsRenderingInProgress() and time.time() - t0 < timeout:
            time.sleep(1.0)
        ids = args.get("job_ids") or [j["JobId"] for j in (p.GetRenderJobList() or [])]
        return {"rendering": p.IsRenderingInProgress(), "waited_s": round(time.time() - t0, 1),
                "status": {i: p.GetRenderJobStatus(i) for i in ids}}
    raise OpError("action must be list|add|start|stop|status|delete|wait")


def render_timeline(resolve, args):
    """One-shot: configure, queue the current (or named) timeline, start and optionally wait."""
    p = _project(resolve)
    args = dict(args, _prev_page=_try(resolve.GetCurrentPage))
    if args.get("timeline"):
        p.SetCurrentTimeline(_timeline(resolve, args["timeline"]))
    tl = _timeline(resolve)
    cfg = dict(args)
    cfg.setdefault("custom_name", tl.GetName())
    if not cfg.get("preset") and not cfg.get("format"):
        cfg["format"], cfg["codec"] = "mp4", "H264"
    conf = render_configure(resolve, cfg)
    if args.get("mark_in") is not None and args.get("mark_out") is not None:
        p.SetRenderSettings({"SelectAllFrames": False, "MarkIn": _to_frame(args["mark_in"], tl),
                             "MarkOut": _to_frame(args["mark_out"], tl)})
    elif "SelectAllFrames" not in (args.get("settings") or {}) and "MarkIn" not in (args.get("settings") or {}):
        p.SetRenderSettings({"SelectAllFrames": True})
    jid = p.AddRenderJob()
    _need(jid, "AddRenderJob failed")
    _need(p.StartRendering([jid], False), "StartRendering failed")
    out = {"job_id": jid, "configured": conf}
    prev_page = args.get("_prev_page")
    if args.get("wait", True):
        timeout = float(args.get("timeout", 900))
        t0 = time.time()
        time.sleep(0.5)
        while p.IsRenderingInProgress() and time.time() - t0 < timeout:
            time.sleep(1.0)
        out["status"] = p.GetRenderJobStatus(jid)
        out["seconds"] = round(time.time() - t0, 1)
        job = [j for j in (p.GetRenderJobList() or []) if j.get("JobId") == jid]
        if job:
            out["output"] = os.path.join(job[0].get("TargetDir", ""), job[0].get("OutputFilename", ""))
            out["output_exists"] = os.path.exists(out["output"])
        if prev_page and _try(resolve.GetCurrentPage) != prev_page:
            _try(lambda: resolve.OpenPage(prev_page))
    return out


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def _commit_comp(item, index=1):
    """Scripted Fusion edits (AddTool, SetInput, keyframes) don't render until the comp is reloaded.
    Round-trip it through a .comp file: export, import, load the imported comp."""
    import tempfile
    import shutil
    d = tempfile.mkdtemp(prefix="resolvepilot_comp_")
    try:
        path = os.path.join(d, "commit.comp")
        _need(item.ExportFusionComp(path, int(index)), "ExportFusionComp failed")
        _need(item.ImportFusionComp(path), "ImportFusionComp failed")
        names = item.GetFusionCompNameList() or []
        _need(names and item.LoadFusionCompByName(names[-1]), "LoadFusionCompByName failed")
        return {"committed": True, "active_comp": names[-1], "comps": names}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def fusion_comp(resolve, args):
    tl = _timeline(resolve, args.get("timeline"))
    it = _find_items(tl, [args["item"]])[0]
    action = args.get("action", "list")
    if action == "list":
        names = it.GetFusionCompNameList() or []
        out = []
        for i, n in enumerate(names, 1):
            comp = _try(lambda: it.GetFusionCompByIndex(i))
            tools = {}
            if comp:
                tl_ = _try(lambda: comp.GetToolList(False)) or {}
                for t in (tl_.values() if isinstance(tl_, dict) else tl_):
                    attrs = _try(t.GetAttrs) or {}
                    tools[attrs.get("TOOLS_Name", "?")] = attrs.get("TOOLS_RegID")
            out.append({"index": i, "name": n, "tools": tools})
        return out
    if action == "add":
        comp = it.AddFusionComp()
        return {"ok": bool(comp)}
    if action == "import":
        return {"ok": bool(it.ImportFusionComp(args["path"]))}
    if action == "export":
        return {"ok": bool(it.ExportFusionComp(args["path"], int(args.get("index", 1))))}
    if action == "load":
        return {"ok": bool(it.LoadFusionCompByName(args["name"]))}
    if action == "delete":
        return {"ok": bool(it.DeleteFusionCompByName(args["name"]))}
    if action == "rename":
        return {"ok": bool(it.RenameFusionCompByName(args["name"], args["new_name"]))}
    if action == "set_inputs":
        comp = it.GetFusionCompByIndex(int(args.get("index", 1)))
        _need(comp, "No Fusion comp on this item")
        tool = comp.FindTool(args["tool"])
        _need(tool, "Tool %r not found in comp" % args["tool"])
        res = {}
        for k, v in (args.get("inputs") or {}).items():
            res[k] = _try(lambda: tool.SetInput(k, v), "error") != "error"
        if args.get("commit", True):
            res["_commit"] = _commit_comp(it, int(args.get("index", 1)))
        return res
    if action == "commit":
        return _commit_comp(it, int(args.get("index", 1)))
    raise OpError("action must be list|add|import|export|load|delete|rename|set_inputs|commit")


# ---------------------------------------------------------------------------
# Captions from SRT (styled Text+)
# ---------------------------------------------------------------------------

CAPTION_STYLES = {
    # Text+ inputs: outline = shading element 2, shadow = element 3.
    "default": {"Font": "Arial", "Style": "Bold", "Size": 0.06, "Center": [0.5, 0.12],
                "Red1": 1, "Green1": 1, "Blue1": 1,
                "Enabled2": 1, "Thickness2": 0.08, "Red2": 0, "Green2": 0, "Blue2": 0, "Alpha2": 1},
    "shadow": {"Font": "Arial", "Style": "Bold", "Size": 0.06, "Center": [0.5, 0.12],
               "Red1": 1, "Green1": 1, "Blue1": 1,
               "Enabled3": 1, "Red3": 0, "Green3": 0, "Blue3": 0, "Alpha3": 0.8, "Softness3": 0.3,
               "ShadowOffset3": [0.004, -0.004]},
    "plain": {"Font": "Arial", "Style": "Regular", "Size": 0.05, "Center": [0.5, 0.12]},
}


def _parse_srt(path):
    with open(path, encoding="utf-8-sig") as f:
        blocks = re.split(r"\n\s*\n", f.read().replace("\r\n", "\n").strip())
    cues = []
    for b in blocks:
        lines = b.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
                text = "\n".join(lines[i + 1:]).strip()
                if text:
                    cues.append((start, end, re.sub(r"<[^>]+>", "", text)))
                break
    return cues


def captions_from_srt(resolve, args):
    """Place every SRT cue as a styled Text+ title (place mode: nothing ripples)."""
    tl = _timeline(resolve, args.get("timeline"))
    fps, drop = _fps(tl)
    path = os.path.abspath(os.path.expanduser(args["path"]))
    _need(os.path.isfile(path), "SRT not found: %s" % path)
    cues = _parse_srt(path)
    _need(cues, "No cues found in %s" % path)
    style = args.get("style") or "default"
    _need(style in CAPTION_STYLES, "style must be one of %s" % ", ".join(CAPTION_STYLES))
    inputs = dict(CAPTION_STYLES[style], **(args.get("title_inputs") or {}))
    offset = _to_frame(args["offset"], tl) if args.get("offset") is not None else tl.GetStartFrame()
    track = args.get("track")
    placed, failed = [], []
    for start, end, text in cues:
        rec = offset + int(round(start * fps))
        dur = max(1, int(round(end * fps)) - int(round(start * fps)))
        try:
            r = timeline_insert(resolve, {"kind": "fusion_title", "name": "Text+", "position": rec,
                                          "duration": dur, "text": text, "title_inputs": inputs,
                                          "track": track, "label": "Caption - " + text[:40],
                                          "bin": args.get("bin") or "ResolvePilot/Captions"})
            track = track or int(r["track"][1:])  # keep every caption on one track
            placed.append({"id": r["id"], "start_tc": r.get("start_tc"), "text": text})
        except Exception as e:
            failed.append({"text": text, "start_tc": frames_to_tc(rec, fps, drop), "error": str(e)})
    return {"placed": len(placed), "failed": failed, "track": "V%s" % track if track else None,
            "captions": placed[:50],
            "note": "Each caption is a Text+ inside a compound clip; see timeline_insert's note for editing."}
