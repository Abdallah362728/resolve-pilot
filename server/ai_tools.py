"""Local open-source AI tools: speech-to-text (faster-whisper / openai-whisper) and image upscaling (Real-ESRGAN).

Used instead of Resolve's own (Studio-only) transcription and Super Scale. Nothing is downloaded unless
`install` is called, which the assistant only does after the user agrees. Sources are the official ones:
PyPI for the Whisper engines (models from their official hosts on first use) and the xinntao/Real-ESRGAN
GitHub release for the upscaler. Everything lives under RESOLVE_PILOT_AI_DIR (default ~/.resolve-pilot).
"""

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
AI_DIR = os.path.abspath(os.path.expanduser(os.environ.get("RESOLVE_PILOT_AI_DIR") or "~/.resolve-pilot"))
WHISPER_ENV = os.path.join(AI_DIR, "whisper-env")
ESRGAN_DIR = os.path.join(AI_DIR, "realesrgan")
IS_WIN = sys.platform.startswith("win")

ENGINES = {
    "faster-whisper": {"module": "faster_whisper", "pip": ["faster-whisper", "av<18"],  # PyAV 18+ broke faster-whisper 1.2.1
                       "source": "https://pypi.org/project/faster-whisper/ (models from huggingface.co/Systran)",
                       "size": "~150 MB plus the model (small: ~500 MB)"},
    "openai-whisper": {"module": "whisper", "pip": ["openai-whisper"],
                       "source": "https://pypi.org/project/openai-whisper/ (models from openaipublic.azureedge.net)",
                       "size": "~2-3 GB with PyTorch plus the model; needs ffmpeg on PATH"},
}

ESRGAN_RELEASE = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
ESRGAN_ZIPS = {  # official portable ncnn-vulkan builds, pinned by SHA-256
    "windows": ("realesrgan-ncnn-vulkan-20220424-windows.zip",
                "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d"),
    "macos": ("realesrgan-ncnn-vulkan-20220424-macos.zip",
              "e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a"),
    "linux": ("realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
              "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96"),
}
ESRGAN_MODELS = {"realesrgan-x4plus": (4,), "realesrgan-x4plus-anime": (4,), "realesr-animevideov3": (2, 3, 4)}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


class AIToolError(Exception):
    pass


def _need(cond, msg):
    if not cond:
        raise AIToolError(msg)


def _run(cmd, timeout=None):
    kw = {"creationflags": 0x08000000} if IS_WIN else {}  # CREATE_NO_WINDOW
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, **kw)


def _tail(b, n=1500):
    return (b or b"").decode("utf-8", "replace")[-n:]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_PROBE = r"""
import importlib.util as u, json, os, sys
out = {"python": sys.executable}
has = lambda m: u.find_spec(m) is not None
out["faster-whisper"] = has("faster_whisper") and has("ctranslate2") and has("av")
s = u.find_spec("whisper")  # make sure it is openai-whisper, not an unrelated package of the same name
out["openai-whisper"] = bool(s and s.submodule_search_locations and has("torch") and
    os.path.exists(os.path.join(list(s.submodule_search_locations)[0], "transcribe.py")))
print(json.dumps(out))
"""


def _venv_python(env):
    return os.path.join(env, "Scripts", "python.exe") if IS_WIN else os.path.join(env, "bin", "python")


def _python_candidates():
    cands = []
    if os.environ.get("RESOLVE_PILOT_WHISPER_PYTHON"):
        cands.append(os.environ["RESOLVE_PILOT_WHISPER_PYTHON"])
    if os.path.exists(_venv_python(WHISPER_ENV)):
        cands.append(_venv_python(WHISPER_ENV))
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p:  # skip the Microsoft Store stub
            cands.append(p)
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


_cache = {}


def whisper_engines(refresh=False):
    """{engine: python_path} for every engine found."""
    if "engines" in _cache and not refresh:
        return _cache["engines"]
    found = {}
    for py in _python_candidates():
        try:
            r = _run([py, "-c", _PROBE], timeout=30)
            info = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except Exception:
            continue
        for eng in ENGINES:
            if info.get(eng) and eng not in found:
                found[eng] = py
    _cache["engines"] = found
    return found


def _platform_key():
    if IS_WIN:
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def esrgan_binary():
    exe = os.environ.get("RESOLVE_PILOT_REALESRGAN")
    if exe and os.path.exists(exe):
        return exe
    local = os.path.join(ESRGAN_DIR, "realesrgan-ncnn-vulkan" + (".exe" if IS_WIN else ""))
    if os.path.exists(local):
        return local
    return shutil.which("realesrgan-ncnn-vulkan")


def _install_options(kind):
    if kind == "transcription":
        return [{"tool": k, "source": v["source"], "download_size": v["size"]} for k, v in ENGINES.items()]
    return [{"tool": "realesrgan", "source": ESRGAN_RELEASE + ESRGAN_ZIPS[_platform_key()][0],
             "download_size": "~45-75 MB (includes models)"}]


def needs_install(kind):
    """Structured reply telling the assistant to ask the user instead of falling back to Resolve."""
    what = "speech-to-text engine (faster-whisper or openai-whisper)" if kind == "transcription" else \
        "AI upscaler (Real-ESRGAN)"
    return {
        "ok": False, "needs_install": True, "missing": kind, "options": _install_options(kind),
        "ask_user": ("No local %s is installed. ASK THE USER whether to download it from the official source "
                     "listed in `options` (then call ai_tools_install with user_confirmed=true) or to skip the %s. "
                     "Do not use DaVinci Resolve's built-in AI features instead." % (what, kind)),
    }


def status(args=None):
    engines = whisper_engines(refresh=True)
    exe = esrgan_binary()
    return {
        "ai_dir": AI_DIR,
        "transcription": {"available": sorted(engines), "python": engines,
                          "preferred": "faster-whisper" if "faster-whisper" in engines else
                          ("openai-whisper" if engines else None),
                          "ffmpeg": shutil.which("ffmpeg")},
        "upscale": {"available": bool(exe), "binary": exe, "models": sorted(ESRGAN_MODELS)},
        "install_options": {"transcription": _install_options("transcription"),
                            "upscale": _install_options("upscale")},
    }


# ---------------------------------------------------------------------------
# Install (only after explicit user consent)
# ---------------------------------------------------------------------------

def _install_whisper(engine):
    os.makedirs(AI_DIR, exist_ok=True)
    py = _venv_python(WHISPER_ENV)
    uv = shutil.which("uv")
    log = []
    if not os.path.exists(py):
        if uv:
            # 3.12 has wheels for torch and ctranslate2 on every platform; uv fetches it if it isn't installed.
            cmd = [uv, "venv", "--python", "3.12", WHISPER_ENV]
        else:
            cmd = [sys.executable, "-m", "venv", WHISPER_ENV]
        r = _run(cmd, timeout=900)
        log.append(_tail(r.stderr, 400))
        _need(r.returncode == 0 and os.path.exists(py), "Could not create %s: %s" % (WHISPER_ENV, _tail(r.stderr)))
    pkgs = ENGINES[engine]["pip"]
    cmd = ([uv, "pip", "install", "--python", py] if uv else [py, "-m", "pip", "install"]) + pkgs
    r = _run(cmd, timeout=3600)
    _need(r.returncode == 0, "Installing %s failed: %s" % (engine, _tail(r.stderr) or _tail(r.stdout)))
    found = whisper_engines(refresh=True)
    _need(engine in found, "%s installed but cannot be imported from %s" % (engine, py))
    out = {"ok": True, "installed": engine, "python": py, "source": ENGINES[engine]["source"]}
    if engine == "openai-whisper" and not shutil.which("ffmpeg"):
        out["warning"] = "openai-whisper needs ffmpeg on PATH (https://ffmpeg.org/download.html)."
    out["note"] = "The speech model downloads from its official host on the first transcription."
    return out


def _install_esrgan():
    name, sha = ESRGAN_ZIPS[_platform_key()]
    url = ESRGAN_RELEASE + name
    os.makedirs(AI_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=AI_DIR)
    os.close(fd)
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        _need(h.hexdigest() == sha, "Checksum mismatch for %s (got %s); not installing." % (url, h.hexdigest()))
        if os.path.isdir(ESRGAN_DIR):
            shutil.rmtree(ESRGAN_DIR)
        os.makedirs(ESRGAN_DIR)
        with zipfile.ZipFile(tmp) as z:
            for m in z.namelist():
                dest = os.path.abspath(os.path.join(ESRGAN_DIR, m))
                _need(dest.startswith(ESRGAN_DIR + os.sep) or dest == ESRGAN_DIR, "Unsafe path in archive: %s" % m)
            z.extractall(ESRGAN_DIR)
    finally:
        os.remove(tmp)
    exe = os.path.join(ESRGAN_DIR, "realesrgan-ncnn-vulkan" + (".exe" if IS_WIN else ""))
    _need(os.path.exists(exe), "Binary missing after extraction: %s" % exe)
    if not IS_WIN:
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if sys.platform == "darwin":
            _run(["xattr", "-dr", "com.apple.quarantine", ESRGAN_DIR], timeout=30)
    return {"ok": True, "installed": "realesrgan", "binary": exe, "source": url, "sha256": sha}


def install(args):
    _need(args.get("user_confirmed") is True,
          "Ask the user first; call again with user_confirmed=true once they agree to the download.")
    t = args["tool"]
    if t in ENGINES:
        return _install_whisper(t)
    if t == "realesrgan":
        return _install_esrgan()
    raise AIToolError("Unknown tool %r (faster-whisper, openai-whisper, realesrgan)" % t)


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def _ts(sec, sep):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def _write_outputs(res, base, formats):
    segs = res["segments"]
    paths = {}
    for fmt in formats:
        p = base + "." + fmt
        with open(p, "w", encoding="utf-8") as f:
            if fmt == "srt":
                for i, s in enumerate(segs, 1):
                    f.write("%d\n%s --> %s\n%s\n\n" % (i, _ts(s["start"], ","), _ts(s["end"], ","), s["text"]))
            elif fmt == "vtt":
                f.write("WEBVTT\n\n")
                for s in segs:
                    f.write("%s --> %s\n%s\n\n" % (_ts(s["start"], "."), _ts(s["end"], "."), s["text"]))
            elif fmt == "txt":
                f.write("\n".join(s["text"] for s in segs) + "\n")
            elif fmt == "json":
                json.dump(res, f, ensure_ascii=False, indent=1)
        paths[fmt] = p
    return paths


def _output_base(src, output_dir, suffix=""):
    stem = os.path.splitext(os.path.basename(src))[0] + suffix
    for d in ([output_dir] if output_dir else [os.path.dirname(src), os.path.join(AI_DIR, "output")]):
        d = os.path.abspath(os.path.expanduser(d))
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".rp_write_test")
            open(probe, "w").close()
            os.remove(probe)
            return os.path.join(d, stem)
        except OSError:
            if output_dir:
                raise AIToolError("Output folder is not writable: %s" % d)
    raise AIToolError("No writable output folder")


def transcribe(args, src):
    engines = whisper_engines()
    if not engines:
        return needs_install("transcription")
    engine = args.get("engine") or ("faster-whisper" if "faster-whisper" in engines else "openai-whisper")
    if engine not in engines:
        out = needs_install("transcription")
        out["ask_user"] = "%s is not installed (available: %s). " % (engine, ", ".join(engines)) + out["ask_user"]
        return out
    if engine == "openai-whisper" and not shutil.which("ffmpeg"):
        raise AIToolError("openai-whisper needs ffmpeg on PATH; install ffmpeg or use faster-whisper.")
    req = {"engine": engine, "input": src, "model": args.get("model") or "small",
           "language": args.get("language") or None, "task": "translate" if args.get("translate") else "transcribe",
           "device": args.get("device") or "auto", "word_timestamps": bool(args.get("word_timestamps"))}
    work = tempfile.mkdtemp(prefix="rp_whisper_")
    try:
        rq, rs = os.path.join(work, "req.json"), os.path.join(work, "res.json")
        with open(rq, "w", encoding="utf-8") as f:
            json.dump(req, f)
        r = _run([engines[engine], os.path.join(HERE, "whisper_worker.py"), rq, rs],
                 timeout=float(args.get("timeout", 7200)))
        _need(os.path.exists(rs), "Transcription worker crashed: %s" % _tail(r.stderr))
        with open(rs, encoding="utf-8") as f:
            res = json.load(f)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _need(res.get("ok"), "Transcription failed: %s" % res.get("error"))
    formats = args.get("formats") or ["srt", "txt"]
    paths = _write_outputs(res, _output_base(src, args.get("output_dir")), formats)
    text = " ".join(s["text"] for s in res["segments"])
    return {"ok": True, "engine": engine, "model": res["model"], "device": res["device"],
            "language": res.get("language"), "duration_sec": round(res.get("duration") or 0, 2),
            "segments": len(res["segments"]), "files": paths,
            "text_preview": text[:4000] + ("..." if len(text) > 4000 else "")}


# ---------------------------------------------------------------------------
# Upscaling
# ---------------------------------------------------------------------------

def upscale(args, srcs):
    exe = esrgan_binary()
    if not exe:
        return needs_install("upscale")
    model = args.get("model") or "realesrgan-x4plus"
    _need(model in ESRGAN_MODELS, "model must be one of %s" % ", ".join(sorted(ESRGAN_MODELS)))
    scale = int(args.get("scale") or 4)
    _need(scale in ESRGAN_MODELS[model], "%s supports scale %s" % (model, "/".join(map(str, ESRGAN_MODELS[model]))))
    fmt = (args.get("format") or "png").lower()
    _need(fmt in ("png", "jpg", "webp"), "format must be png, jpg or webp")
    model_dir = os.path.join(os.path.dirname(exe), "models")
    outputs = []
    for src in srcs:
        _need(os.path.isfile(src), "Not a file: %s" % src)
        _need(src.lower().endswith(IMAGE_EXTS), "Not a supported image (%s): %s" % (", ".join(IMAGE_EXTS), src))
        dst = _output_base(src, args.get("output_dir"), "_x%d" % scale) + "." + fmt
        cmd = [exe, "-i", src, "-o", dst, "-n", model, "-s", str(scale), "-f", fmt]
        if os.path.isdir(model_dir):
            cmd += ["-m", model_dir]
        if args.get("tile"):
            cmd += ["-t", str(int(args["tile"]))]
        if args.get("gpu") is not None:
            cmd += ["-g", str(args["gpu"])]
        r = _run(cmd, timeout=float(args.get("timeout", 1800)))
        _need(r.returncode == 0 and os.path.exists(dst),
              "Real-ESRGAN failed on %s (a Vulkan-capable GPU driver is required): %s" % (src, _tail(r.stderr)))
        outputs.append({"input": src, "output": dst})
    return {"ok": True, "model": model, "scale": scale, "outputs": outputs}
