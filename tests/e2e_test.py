#!/usr/bin/env python3
"""End-to-end test: drives the ResolvePilot MCP server (stdio) against a live DaVinci Resolve.

Usage:  python tests/e2e_test.py [--media DIR] [--keep] [--only substr]

Creates a throwaway project "ResolvePilot_E2E_<time>", exercises every tool, then deletes it.
If --media is not given, test clips are generated with ffmpeg (must be on PATH).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server", "mcp_server.py")


class MCP(object):
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=sys.stderr, text=True, encoding="utf-8", bufsize=1)
        self.i = 0
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e2e", "version": "0"}})
        assert r["serverInfo"]["name"] == "resolve-pilot"
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.tools = {t["name"]: t for t in self.rpc("tools/list", {})["tools"]}

    def rpc(self, method, params):
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("server exited")
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    def call(self, _tool, **args):
        r = self.rpc("tools/call", {"name": _tool, "arguments": args})
        text = r["content"][0]["text"]
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return r.get("isError", False), data

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


def make_media(d):
    ff = shutil.which("ffmpeg")
    if not ff:
        sys.exit("ffmpeg not found; pass --media DIR with a few video/audio files")
    specs = [
        ("clipA.mp4", ["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24:duration=10", "-f", "lavfi", "-i",
                       "sine=frequency=440:duration=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-shortest"]),
        ("clipB.mp4", ["-f", "lavfi", "-i", "smptebars=size=1920x1080:rate=24:duration=8", "-f", "lavfi", "-i",
                       "sine=frequency=880:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-shortest"]),
        ("clipC.mp4", ["-f", "lavfi", "-i", "color=c=0x2266aa:size=1920x1080:rate=24:duration=6", "-c:v", "libx264",
                       "-pix_fmt", "yuv420p"]),
        ("music.wav", ["-f", "lavfi", "-i", "sine=frequency=220:duration=12"]),
    ]
    for name, a in specs:
        subprocess.check_call([ff, "-hide_banner", "-loglevel", "error", "-y"] + a + [os.path.join(d, name)])
    return d


class Runner(object):
    def __init__(self, mcp, only=None):
        self.mcp = mcp
        self.results = []
        self.only = only
        self.called = set()

    def step(self, label, _tool, expect_error=False, check=None, **args):
        # args are the tool's arguments; label/tool/expect_error/check are reserved names
        if self.only and self.only not in label and self.only not in _tool:
            return None
        self.called.add(_tool)
        t0 = time.time()
        try:
            err, data = self.mcp.call(_tool, **args)
            ok = (err == expect_error)
            why = ""
            if ok and check:
                try:
                    c = check(data)
                    if c is False:
                        ok, why = False, "check failed"
                except Exception as e:
                    ok, why = False, "check raised %s: %s" % (type(e).__name__, e)
            if err and not expect_error:
                why = str(data)[:600]
        except Exception as e:
            ok, data, why = False, None, "exception %s\n%s" % (e, traceback.format_exc())
        dt = time.time() - t0
        self.results.append((label, _tool, ok, why, dt))
        print("%s  %-46s %6.2fs %s" % ("PASS" if ok else "FAIL", label, dt, ("  -> " + why) if why else ""))
        sys.stdout.flush()
        return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media")
    ap.add_argument("--keep", action="store_true", help="keep the test project")
    ap.add_argument("--only")
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="resolvepilot_e2e_")
    if a.media:
        media = os.path.abspath(a.media)
    else:
        media = os.path.join(work, "media")
        os.makedirs(media)
        make_media(media)
    files = sorted(os.path.join(media, f) for f in os.listdir(media))
    videos = [f for f in files if f.lower().endswith((".mp4", ".mov", ".mxf", ".mkv"))]
    audios = [f for f in files if f.lower().endswith((".wav", ".mp3", ".aac"))]
    out = os.path.join(work, "out")
    os.makedirs(out)

    mcp = MCP()
    R = Runner(mcp, a.only)
    S = {}

    st = R.step("status connected", "resolve_status", check=lambda d: d["connected"] is True)
    if not st or not st.get("connected"):
        print("Not connected; aborting. %s" % (st or {}).get("help"))
        return 2
    print("    Resolve:", st["resolve"].get("product"), st["resolve"].get("version"), "via",
          st["connection"]["transport"])
    original_project = st["resolve"].get("project")

    pname = "ResolvePilot_E2E_%d" % int(time.time())
    R.step("api reference class", "resolve_api_reference", class_name="Timeline",
           check=lambda d: any("GetItemListInTrack" in l for l in d["lines"]))
    R.step("api reference query", "resolve_api_reference", query="AddTransition",
           check=lambda d: len(d["matches"]) > 0)
    R.step("install bridge (idempotent)", "resolve_install_bridge", check=lambda d: os.path.exists(d["installed_to"]))
    R.step("project list", "project_list", check=lambda d: isinstance(d["projects"], list))
    R.step("project create", "project_create", name=pname,
           settings={"timelineFrameRate": "24", "timelineResolutionWidth": "1920",
                     "timelineResolutionHeight": "1080"},
           check=lambda d: d["project"] == pname)
    R.step("project create duplicate -> error", "project_create", expect_error=True, name=pname)
    R.step("project settings get keys", "project_settings", keys=["timelineFrameRate", "timelineResolutionWidth"],
           check=lambda d: float(d["timelineFrameRate"]) == 24 and str(d["timelineResolutionWidth"]) == "1920")
    R.step("project settings filter", "project_settings", filter="resolution", check=lambda d: len(d) > 0)
    R.step("project settings set", "project_settings", set={"colorScienceMode": "davinciYRGB"},
           check=lambda d: d["ok"])
    R.step("page edit", "resolve_page", page="edit", check=lambda d: d["page"] == "edit")
    R.step("page invalid -> error", "resolve_page", expect_error=True, page="nonsense")
    R.step("layout presets list", "resolve_layout_preset", action="list", check=lambda d: "layouts" in d)

    R.step("media storage volumes", "media_storage_browse", check=lambda d: len(d["volumes"]) > 0)
    R.step("media storage folder", "media_storage_browse", path=media, check=lambda d: len(d["files"]) >= 1)
    R.step("media import", "media_import", paths=videos + audios, bin="E2E/Footage",
           check=lambda d: d["imported"] == len(videos + audios))
    R.step("media import missing -> error", "media_import", expect_error=True, paths=[os.path.join(media, "nope.mov")])
    pool = R.step("media pool list", "media_pool_list",
                  check=lambda d: len(d["clips"]) >= len(videos + audios) and any("E2E/Footage" in b for b in d["bins"]))
    clips = {c["name"]: c for c in (pool or {}).get("clips", [])}
    vnames = [os.path.basename(v) for v in videos]
    aname = os.path.basename(audios[0]) if audios else None
    S["clipA"] = clips.get(vnames[0], {}).get("id")
    S["clipB"] = clips.get(vnames[1], {}).get("id")
    S["clipC"] = clips.get(vnames[2], {}).get("id")
    S["music"] = clips.get(aname, {}).get("id")
    R.step("media pool list bin non-recursive", "media_pool_list", bin="E2E", recursive=False,
           check=lambda d: d["bins"][0].endswith("E2E") and any(b.endswith("E2E/Footage") for b in d["bins"]))
    R.step("media clip info", "media_clip_info", clips=[S["clipA"]],
           check=lambda d: d[0]["properties"].get("File Name") == vnames[0])
    R.step("media clip info by name", "media_clip_info", clips=[vnames[1]], check=lambda d: d[0]["id"] == S["clipB"])
    R.step("media clip update", "media_clip_update", clips=[S["clipA"]], metadata={"Scene": "42", "Comments": "e2e"},
           clip_color="Teal", add_flags=["Green"], mark_in=10, mark_out=100,
           check=lambda d: d[0]["metadata"] and d[0]["clip_color"] and all(d[0]["flags"]) and d[0]["mark_in_out"])
    R.step("media clip update verify", "media_clip_info", clips=[S["clipA"]],
           check=lambda d: d[0]["metadata"].get("Scene") == "42" and d[0]["clip_color"] == "Teal"
           and "Green" in d[0]["flags"])
    R.step("media clip clear", "media_clip_update", clips=[S["clipA"]], clip_color=None, clear_flags="All",
           clear_mark_in_out=True, check=lambda d: d[0]["clip_color"])
    R.step("media bin create", "media_bin", action="create", path="E2E/Other", check=lambda d: d["ok"])
    R.step("media bin move clips", "media_bin", action="move_clips", clips=[S["clipC"]], target="E2E/Other",
           check=lambda d: d["ok"])
    R.step("media bin moved verify", "media_pool_list", bin="E2E/Other",
           check=lambda d: [c["id"] for c in d["clips"]] == [S["clipC"]])
    R.step("media bin select", "media_bin", action="select", clips=[S["clipB"]], check=lambda d: d["ok"])
    R.step("media bin set current", "media_bin", action="set_current", path="E2E/Footage", check=lambda d: d["ok"])
    R.step("media export metadata", "media_bin", action="export_metadata", path=os.path.join(out, "meta.csv"),
           check=lambda d: d["ok"] and os.path.exists(os.path.join(out, "meta.csv")))

    # --- timelines ---
    R.step("timeline create from clips", "timeline_create", name="E2E Main", clips=[S["clipA"], S["clipB"]],
           check=lambda d: d["timeline"] == "E2E Main")
    R.step("timeline create empty", "timeline_create", name="E2E Empty", start_timecode="00:00:00:00",
           check=lambda d: d["ok"])
    R.step("timeline list", "timeline_list", check=lambda d: len(d) == 2 and sum(t["current"] for t in d) == 1)
    R.step("timeline switch by name", "timeline_switch", timeline="E2E Main", check=lambda d: d["ok"])
    info = R.step("timeline info", "timeline_info",
                  check=lambda d: d["tracks"][0]["item_count"] == 2 and d["start_tc"] == "01:00:00:00")
    v1 = [t for t in (info or {}).get("tracks", []) if t["type"] == "video" and t["index"] == 1]
    items = v1[0]["items"] if v1 else []
    S["itemA"] = items[0]["id"] if items else None
    S["itemB"] = items[1]["id"] if len(items) > 1 else None
    start = (info or {}).get("start_frame", 86400)
    R.step("timeline info item ranges", "timeline_info",
           check=lambda d: d["tracks"][0]["items"][0]["start"] == start
           and d["tracks"][0]["items"][1]["start"] == d["tracks"][0]["items"][0]["end"])
    R.step("track add video", "timeline_track", action="add", type="video", check=lambda d: d["count"] == 2)
    R.step("track add audio stereo", "timeline_track", action="add", type="audio", subtype="stereo",
           check=lambda d: d["ok"])
    R.step("track rename", "timeline_track", action="rename", type="video", index=2, name="Overlays",
           check=lambda d: d["ok"])
    R.step("track lock", "timeline_track", action="lock", type="video", index=2, value=True, check=lambda d: d["ok"])
    R.step("track unlock", "timeline_track", action="lock", type="video", index=2, value=False, check=lambda d: d["ok"])
    R.step("track disable", "timeline_track", action="enable", type="audio", index=1, value=False,
           check=lambda d: d["ok"])
    R.step("track enable", "timeline_track", action="enable", type="audio", index=1, value=True, check=lambda d: d["ok"])
    R.step("track state verify", "timeline_info", include_items=False,
           check=lambda d: [t for t in d["tracks"] if t["type"] == "video" and t["index"] == 2][0]["name"] == "Overlays")
    app = R.step("timeline append ranged on V2", "timeline_append",
                 clips=[{"clip": S["clipC"], "start_frame": 0, "end_frame": 48, "track": 2,
                         "record_frame": "01:00:02:00", "media_type": "video"}],
                 check=lambda d: d["appended"] == 1 and d["items"][0]["start"] == start + 48
                 and d["items"][0]["duration"] == 48 and d["items"][0]["track"] == "V2")
    S["itemC"] = (app or {}).get("items", [{}])[0].get("id")
    R.step("timeline append audio", "timeline_append",
           clips=[{"clip": S["music"], "track": 2, "record_frame": start}],
           check=lambda d: d["appended"] >= 1 and d["items"][0]["track"].startswith("A"))
    R.step("timeline append bad clip -> error", "timeline_append", expect_error=True, clips=["does-not-exist"])
    R.step("item info", "timeline_item_info", items=[S["itemA"]],
           check=lambda d: "ZoomX" in d[0]["properties"] and d[0]["media_pool_clip_id"] == S["clipA"])
    R.step("item update props", "timeline_item_update", items=[S["itemC"]],
           properties={"ZoomX": 0.5, "ZoomY": 0.5, "Pan": 300, "Opacity": 80}, clip_color="Orange",
           add_flags=["Red"], check=lambda d: d[0]["clip_color"])
    R.step("item update verify", "timeline_item_info", items=[S["itemC"]],
           check=lambda d: abs(d[0]["properties"]["ZoomX"] - 0.5) < 1e-3 and d[0]["properties"]["Pan"] == 300
           and d[0]["clip_color"] == "Orange")
    R.step("item disable/enable", "timeline_item_update", items=[S["itemC"]], enabled=False,
           check=lambda d: d[0]["enabled"])
    R.step("item re-enable", "timeline_item_update", items=[S["itemC"]], enabled=True, check=lambda d: d[0]["enabled"])
    R.step("item fades (21.1+)", "timeline_item_update", items=[S["itemB"]], fades={"FadeIn": 12, "FadeOut": 6},
           check=lambda d: d[0]["fades"])
    R.step("item fades verify", "timeline_item_info", items=[S["itemB"]],
           check=lambda d: d[0]["fades"] == {"FadeIn": 12, "FadeOut": 6})
    R.step("item speed (21.1+)", "timeline_item_update", items=[S["itemB"]], speed={"Percentage": 50},
           check=lambda d: d[0]["speed"])
    R.step("item speed verify", "timeline_item_info", items=[S["itemB"]],
           check=lambda d: d[0]["speed"]["Percentage"] == 50)
    R.step("item speed reset", "timeline_item_update", items=[S["itemB"]], speed={"Percentage": 100},
           check=lambda d: d[0]["speed"])
    R.step("item transition (21.1+)", "timeline_item_update", items=[S["itemB"]],
           transition={"type": "Cross Dissolve", "position": "start", "duration": 12},
           check=lambda d: d[0]["transition"]["ok"] and d[0]["transition"]["end"] - d[0]["transition"]["start"] == 12)
    R.step("item transition bad type -> error", "timeline_item_update", expect_error=True, items=[S["itemB"]],
           transition={"type": "No Such Transition"})
    R.step("item unknown -> error", "timeline_item_info", expect_error=True, items=["nope"])
    R.step("playhead set tc", "timeline_playhead", position="01:00:03:00",
           check=lambda d: d["timecode"] == "01:00:03:00" and d["frame"] == start + 72)
    R.step("playhead set relative frame", "timeline_playhead", position=24, relative=True,
           check=lambda d: d["timecode"] == "01:00:01:00")
    R.step("playhead get", "timeline_playhead", check=lambda d: d["timecode"] == "01:00:01:00")

    R.step("marker add timeline", "marker", action="add", frame=48, color="Red", name="M1", note="n",
           custom_data="cd1", check=lambda d: d["ok"])
    R.step("marker add timeline tc", "marker", action="add", frame="01:00:05:00", color="Green", name="M2",
           check=lambda d: d["frame"] == 120)
    R.step("marker list timeline", "marker", action="list",
           check=lambda d: [m["name"] for m in d] == ["M1", "M2"] and d[0]["customData"] == "cd1")
    R.step("marker update custom data", "marker", action="update_custom_data", frame=48, custom_data="cd2",
           check=lambda d: d["ok"])
    R.step("marker delete by frame", "marker", action="delete", frame=120, check=lambda d: d["ok"])
    R.step("marker bad color -> error", "marker", expect_error=True, action="add", frame=10, color="Chartreuse")
    R.step("marker item add", "marker", target="item", id=S["itemA"], action="add", frame=5, color="Blue", name="IM",
           check=lambda d: d["ok"])
    R.step("marker item list", "marker", target="item", id=S["itemA"], action="list",
           check=lambda d: d[0]["name"] == "IM")
    R.step("marker clip add", "marker", target="clip", id=S["clipB"], action="add", frame=3, color="Yellow",
           name="CM", check=lambda d: d["ok"])
    R.step("marker clip list", "marker", target="clip", id=S["clipB"], check=lambda d: d[0]["name"] == "CM")
    R.step("marker clip delete all", "marker", target="clip", id=S["clipB"], action="delete", color="All",
           check=lambda d: d["ok"])

    before = R.step("layout before insert", "timeline_info") or {}
    snap = lambda d: sorted((t["type"], t["index"], i["id"], i["start"], i["end"]) for t in d["tracks"] for i in t.get("items", []))
    title = R.step("place fusion title (no ripple)", "timeline_insert", kind="fusion_title", name="Text+",
                   position="01:00:06:00", duration=36, text="ResolvePilot",
                   title_inputs={"Size": 0.12, "Center": [0.5, 0.2]},
                   check=lambda d: d["start"] == start + 144 and d["duration"] == 36 and d.get("text_set") is True
                   and all(d["title_inputs"].values()))
    R.step("place left others untouched", "timeline_info",
           check=lambda d: set(snap(before)) <= set(snap(d)) and len(snap(d)) == len(snap(before)) + 1)
    S["titleItem"] = (title or {}).get("id")
    R.step("place generator on V3", "timeline_insert", kind="generator", name="Solid Color", position="01:00:12:00",
           track=3, duration="00:00:01:00", check=lambda d: d["start"] == start + 288 and d["track"] == "V3"
           and d["duration"] == 24)
    R.step("insert generator (native ripple)", "timeline_insert", kind="generator", name="10 Step", mode="insert",
           position="01:00:30:00", check=lambda d: d["start"] == start + 720)
    R.step("insert bad generator -> error", "timeline_insert", expect_error=True, kind="generator", name="NoSuchGen")
    R.step("items unlink", "timeline_items_link", items=[S["itemA"]], linked=False, check=lambda d: d["ok"])
    R.step("items relink", "timeline_items_link", items=[S["itemA"]], linked=True, check=lambda d: d["ok"])

    # --- color ---
    R.step("page color", "resolve_page", page="color", check=lambda d: d["page"] == "color")
    R.step("color info", "color_info", item=S["itemA"], check=lambda d: len(d["nodes"]) >= 1)
    R.step("color cdl", "color_apply", action="cdl", item=S["itemA"], node=1, slope=[1.1, 1.0, 0.9],
           offset=[0, 0, 0], power=[1, 1, 1], saturation=1.2, check=lambda d: d["ok"])
    R.step("color add version", "color_apply", action="add_version", item=S["itemA"], name="alt",
           check=lambda d: d["ok"])
    R.step("color info versions", "color_info", item=S["itemA"], check=lambda d: "alt" in (d["local_versions"] or []))
    R.step("color group assign", "color_apply", action="assign_group", item=S["itemA"], name="E2E Group",
           check=lambda d: d["ok"])
    R.step("color group verify", "color_info", item=S["itemA"], check=lambda d: d["color_group"] == "E2E Group")
    R.step("color group remove", "color_apply", action="remove_group", item=S["itemA"], check=lambda d: d["ok"])
    R.step("color copy grade", "color_apply", action="copy_grade_to", item=S["itemA"], targets=[S["itemB"]],
           check=lambda d: d["ok"])
    R.step("color export lut", "color_apply", action="export_lut", item=S["itemA"], size="33",
           path=os.path.join(out, "grade.cube"), check=lambda d: d["ok"] and os.path.exists(os.path.join(out, "grade.cube")))
    R.step("color apply lut", "color_apply", action="lut", item=S["itemB"], node=1,
           path=os.path.join(out, "grade.cube"), check=lambda d: d["ok"] and "grade.cube" in d["lut"])
    R.step("color info shows lut", "color_info", item=S["itemB"], check=lambda d: "grade.cube" in (d["nodes"][0]["lut"] or ""))
    R.step("color node disable", "color_apply", action="node_enable", item=S["itemB"], node=1, value=False,
           check=lambda d: d["ok"])
    R.step("color reset", "color_apply", action="reset", item=S["itemB"], check=lambda d: d["ok"])
    R.step("timeline grab still", "timeline_manage", action="grab_still", check=lambda d: d["ok"])
    R.step("gallery list", "gallery", action="list", check=lambda d: len(d["albums"]) >= 1)
    R.step("gallery export", "gallery", action="export", path=os.path.join(out, "stills"), format="png",
           check=lambda d: d["ok"] and len(os.listdir(os.path.join(out, "stills"))) >= 1)
    R.step("page edit again", "resolve_page", page="edit")
    R.step("export frame (composite render)", "export_frame", path=os.path.join(out, "frame.png"),
           position="01:00:06:12", check=lambda d: d["ok"] and os.path.getsize(d["path"]) > 10000)
    R.step("export frame (still)", "export_frame", path=os.path.join(out, "still.jpg"), method="still",
           position="01:00:01:00", check=lambda d: d["ok"] and os.path.exists(d["path"]))
    R.step("export frame left queue clean", "render_jobs", action="list", check=lambda d: len(d["jobs"]) == 0)

    # --- fusion ---
    R.step("fusion comp add", "fusion_comp", item=S["itemA"], action="add", check=lambda d: d["ok"])
    R.step("fusion comp list", "fusion_comp", item=S["itemA"], action="list",
           check=lambda d: len(d) >= 1 and any("MediaIn" in str(c["tools"]) for c in d))
    R.step("fusion comp export", "fusion_comp", item=S["itemA"], action="export", path=os.path.join(out, "c.comp"),
           check=lambda d: d["ok"] and os.path.exists(os.path.join(out, "c.comp")))

    # --- exec ---
    R.step("exec basic", "resolve_exec",
           code="tl = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()\nprint('hi')\n"
                "result = {'name': tl.GetName(), 'n': tl.GetTrackCount('video'), 'obj': tl}",
           check=lambda d: d["result"]["name"] == "E2E Main" and d["stdout"].strip() == "hi"
           and d["result"]["obj"].startswith("<"))
    R.step("exec persistent ns", "resolve_exec", code="result = tl.GetName() + str(args['x'])", args={"x": 1},
           check=lambda d: d["result"] == "E2E Main1")
    R.step("exec error -> error", "resolve_exec", expect_error=True, code="raise ValueError('boom')")
    R.step("exec fresh ns", "resolve_exec", code="result = 'tl' in globals()", fresh=True,
           check=lambda d: d["result"] is False)
    R.step("exec ops helper", "resolve_exec", code="result = ops.frames_to_tc(86400+25, 24)",
           check=lambda d: d["result"] == "01:00:01:01")
    R.step("exec lua", "resolve_exec", language="lua",
           code="local tl = resolve:GetProjectManager():GetCurrentProject():GetCurrentTimeline()\n"
                "print('n', tl:GetTrackCount('video'))\nreturn {name = tl:GetName(), x = args.x}", args={"x": 7},
           check=lambda d: d["result"] == {"name": "E2E Main", "x": 7} and d["stdout"].startswith("n"))
    R.step("exec lua syntax error -> error", "resolve_exec", expect_error=True, language="lua", code="return (")
    R.step("start bridge idempotent", "resolve_start_bridge", check=lambda d: d["ok"] and "already" in d["note"])

    # --- timeline management / interchange ---
    R.step("timeline mark in/out", "timeline_manage", action="set_mark_in_out", mark_in="01:00:00:00",
           mark_out="01:00:04:23", check=lambda d: d["ok"])
    R.step("timeline mark verify", "timeline_info", include_items=False,
           check=lambda d: d["mark_in_out"]["video"]["in"] == start and d["mark_in_out"]["video"]["out"] == start + 119)
    R.step("timeline clear marks", "timeline_manage", action="clear_mark_in_out", check=lambda d: d["ok"])
    for fmt in ("fcpxml", "edl", "otio", "drt", "fcp7xml", "csv"):
        R.step("timeline export %s" % fmt, "timeline_exchange", action="export", format=fmt,
               path=os.path.join(out, "tl_%s" % fmt), check=lambda d: d["ok"] and d["exists"])
    R.step("timeline import drt", "timeline_exchange", action="import", path=os.path.join(out, "tl_drt.drt"),
           name="E2E Imported", check=lambda d: d["ok"])
    R.step("timeline duplicate", "timeline_manage", action="duplicate", timeline="E2E Main", name="E2E Dup",
           check=lambda d: d["timeline"] == "E2E Dup")
    R.step("timeline rename", "timeline_manage", action="rename", timeline="E2E Dup", name="E2E Dup2",
           check=lambda d: d["ok"])
    R.step("timeline switch index", "timeline_switch", timeline=1, check=lambda d: d["ok"])
    R.step("timeline switch main", "timeline_switch", timeline="E2E Main")
    R.step("compound clip", "timeline_manage", action="compound_clip", items=[S["itemB"]], name="E2E Compound",
           check=lambda d: d["ok"])
    R.step("timeline settings (custom)", "project_settings", timeline="E2E Empty",
           set={"useCustomSettings": "1", "timelineFrameRate": "25"})
    R.step("timeline delete", "timeline_manage", action="delete", timeline="E2E Dup2", check=lambda d: d["ok"])
    R.step("timeline delete -> gone", "timeline_switch", expect_error=True, timeline="E2E Dup2")

    # --- render ---
    R.step("render options", "render_options", format="mp4",
           check=lambda d: "mp4" in d["formats"].values() and len(d["codecs"]) > 0)
    R.step("render configure", "render_configure", format="mp4", codec="H264", target_dir=out, custom_name="e2e_job",
           settings={"SelectAllFrames": True}, check=lambda d: d["settings"] and d["format_codec"])
    R.step("render configure bad codec -> error", "render_configure", expect_error=True, format="mp4", codec="NOPE")
    job = R.step("render job add", "render_jobs", action="add", check=lambda d: d["job_id"])
    R.step("render jobs list", "render_jobs", action="list", check=lambda d: len(d["jobs"]) >= 1)
    R.step("render job delete", "render_jobs", action="delete", job_ids=[(job or {}).get("job_id")],
           check=lambda d: d["ok"])
    R.step("render timeline (one-shot)", "render_timeline", target_dir=out, custom_name="e2e_render",
           mark_in="01:00:00:00", mark_out="01:00:01:23", timeout=600,
           check=lambda d: d["status"]["JobStatus"] == "Complete" and d["output_exists"])
    R.step("render status", "render_jobs", action="status", check=lambda d: d["rendering"] is False)
    R.step("render delete all", "render_jobs", action="delete", check=lambda d: d["ok"])

    R.step("items ripple delete", "timeline_items_delete", items=[S["itemC"]], ripple=False, check=lambda d: d["ok"])
    R.step("track delete", "timeline_track", action="delete", type="video", index=2, check=lambda d: d["ok"])
    R.step("bin delete", "media_bin", action="delete", path="E2E/Other", check=lambda d: d["ok"])

    # --- project lifecycle ---
    R.step("project save", "project_manage", action="save", check=lambda d: d["ok"])
    R.step("project export drp", "project_manage", action="export", path=os.path.join(out, "e2e.drp"),
           check=lambda d: d["ok"] and os.path.exists(os.path.join(out, "e2e.drp")))
    if not a.keep:
        if original_project and original_project != pname:
            R.step("reopen original project", "project_open", name=original_project)
        else:
            R.step("close test project", "project_manage", action="close", save=False)
        R.step("project delete", "project_manage", action="delete", name=pname, check=lambda d: d["ok"])
        R.step("project gone", "project_list", check=lambda d: pname not in d["projects"])

    not_called = sorted(set(mcp.tools) - R.called - {"resolve_launch"})
    mcp.close()
    fails = [r for r in R.results if not r[2]]
    print("\n%d steps, %d passed, %d failed. Output dir: %s" % (len(R.results), len(R.results) - len(fails),
                                                               len(fails), out))
    if not_called and not a.only:
        print("Tools not exercised:", not_called)
    for label, tool, ok, why, dt in fails:
        print("  FAIL %s (%s): %s" % (label, tool, why[:800]))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
