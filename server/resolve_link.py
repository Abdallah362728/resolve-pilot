"""Connection layer: gives the MCP server a live `resolve` object, however Resolve is reachable.

Transports (tried in this order):

* bridge -- the "ResolvePilot Bridge" Lua script running inside Resolve (Free and Studio, every OS).
            Resolve API objects are proxied by handle over a localhost socket (~0.1 ms per call).
* direct -- DaVinciResolveScript.scriptapp("Resolve") from this process (Studio with
            Preferences > System > General > External scripting using = Local).

Tool code (resolve_ops.py) runs in this process against either kind of object.
"""

import contextlib
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import weakref

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_SRC = os.path.join(os.path.dirname(HERE), "bridge", "ResolvePilot Bridge.lua")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".resolve-pilot", "bridge.json")
BRIDGE_TIMEOUT = float(os.environ.get("RESOLVE_PILOT_TIMEOUT", "600"))
HELLO_TIMEOUT = 5.0

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import resolve_ops  # noqa: E402


class ResolveError(Exception):
    pass


class NotConnected(ResolveError):
    pass


# ---------------------------------------------------------------------------
# Install-location discovery
# ---------------------------------------------------------------------------

def system_name():
    return {"Darwin": "mac", "Windows": "windows"}.get(platform.system(), "linux")


def resolve_paths():
    """Default locations of Resolve's executable, scripting API/library and Scripts folders.
    Every entry can be overridden with an environment variable for non-standard installs."""
    osn = system_name()
    env = os.environ
    if osn == "windows":
        pd = env.get("PROGRAMDATA", r"C:\ProgramData")
        appdata = env.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
        install = env.get("RESOLVE_INSTALL_DIR") or os.path.join(
            env.get("PROGRAMFILES", r"C:\Program Files"), "Blackmagic Design", "DaVinci Resolve")
        api = os.path.join(pd, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
        lib = os.path.join(install, "fusionscript.dll")
        exe = os.path.join(install, "Resolve.exe")
        user_scripts = os.path.join(appdata, "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Scripts")
    elif osn == "mac":
        install = env.get("RESOLVE_INSTALL_DIR") or next(
            (c for c in ("/Applications/DaVinci Resolve/DaVinci Resolve.app",
                         "/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app",
                         "/Applications/DaVinci Resolve.app") if os.path.exists(c)),
            "/Applications/DaVinci Resolve/DaVinci Resolve.app")
        api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib = install + "/Contents/Libraries/Fusion/fusionscript.so"
        exe = install
        user_scripts = os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts")
    else:
        install = env.get("RESOLVE_INSTALL_DIR") or "/opt/resolve"
        api = "/opt/resolve/Developer/Scripting"
        lib = install + "/libs/Fusion/fusionscript.so"
        exe = install + "/bin/resolve"
        user_scripts = os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts")
    return {
        "os": osn,
        "install_dir": install,
        "executable": env.get("RESOLVE_EXECUTABLE", exe),
        "api_dir": env.get("RESOLVE_SCRIPT_API", api),
        "lib": env.get("RESOLVE_SCRIPT_LIB", lib),
        "user_scripts_dir": env.get("RESOLVE_PILOT_SCRIPTS_DIR", user_scripts),
    }


def bridge_install_path(scripts_dir=None):
    base = scripts_dir or resolve_paths()["user_scripts_dir"]
    return os.path.join(base, "Utility", os.path.basename(BRIDGE_SRC))


def bridge_alive():
    """True if a bridge described by the state file answers a hello."""
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
        c = BridgeClient(st)
        try:
            c.handshake()
            return True
        finally:
            c.close()
    except Exception:
        return False


def resolve_running():
    """True/False if we can tell whether a Resolve process is running, None if unknown."""
    try:
        if system_name() == "windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return "resolve.exe" in out.lower()
        pattern = "DaVinci Resolve.app/Contents/MacOS/Resolve" if system_name() == "mac" else "/opt/resolve/bin/resolve"
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=15).stdout
        return bool(out.strip())
    except Exception:
        return None


def install_bridge(scripts_dir=None):
    """Copy the bridge into Resolve's per-user Scripts/Utility folder (listed on every page)."""
    dest = bridge_install_path(scripts_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    existed = os.path.exists(dest)
    with open(BRIDGE_SRC, "rb") as fh:
        src = fh.read()
    same = existed and open(dest, "rb").read() == src
    if not same:
        shutil.copyfile(BRIDGE_SRC, dest)
    # Remove the obsolete Python bridge from older plugin versions.
    old = os.path.join(os.path.dirname(dest), "ResolvePilot Bridge.py")
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    return {"installed_to": dest, "updated": existed and not same, "already_current": same,
            "next_step": "Start it with the resolve_start_bridge tool, or in Resolve press F6 and run: "
                         + console_command()}


def launch_resolve(extra_args=None):
    p = resolve_paths()
    exe = p["executable"]
    if not os.path.exists(exe):
        raise ResolveError("Resolve executable not found at %s (set RESOLVE_EXECUTABLE)" % exe)
    if p["os"] == "mac":
        cmd = ["open", "-a", exe] + (["--args"] + extra_args if extra_args else [])
    else:
        cmd = [exe] + (extra_args or [])
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if p["os"] == "windows":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw["cwd"] = os.path.dirname(exe)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    return {"launched": cmd}


# ---------------------------------------------------------------------------
# Bridge transport
# ---------------------------------------------------------------------------

class EmptyResult(list):
    """Lua cannot tell an empty list from an empty dict; this behaves as both."""

    def get(self, key, default=None):
        return default

    def keys(self):
        return []

    def values(self):
        return []

    def items(self):
        return []


def _hashable(k):
    return tuple(k) if isinstance(k, list) else k


class BridgeClient(object):
    def __init__(self, state):
        self.port = int(state["port"])
        self.token = state["token"]
        self.lock = threading.RLock()
        self.next_id = 1
        self.pending_release = []
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        self.sock.settimeout(BRIDGE_TIMEOUT)
        self.file = self.sock.makefile("rwb")
        self.closed = False
        self.hello = None

    def handshake(self):
        """hello with a short timeout: a port left open by a crashed Resolve accepts but never answers."""
        self.sock.settimeout(HELLO_TIMEOUT)
        try:
            self.hello = self.request("hello")
        finally:
            if not self.closed:
                self.sock.settimeout(BRIDGE_TIMEOUT)
        return self.hello

    def close(self):
        self.closed = True
        for c in (self.file, self.sock):
            try:
                c.close()
            except Exception:
                pass

    def decode(self, value):
        if isinstance(value, list):
            if not value:
                return EmptyResult()
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "__h" in value and len(value) <= 2:
                return RemoteObject(self, value["__h"], value.get("__t", "Object"))
            if "__d" in value and len(value) == 1:
                return {_hashable(self.decode(k)): self.decode(v) for k, v in value["__d"]}
            return {k: self.decode(v) for k, v in value.items()}
        return value

    @staticmethod
    def encode(value):
        if isinstance(value, RemoteObject):
            return {"__h": value._h}
        if isinstance(value, (list, tuple)):
            return [BridgeClient.encode(v) for v in value]
        if isinstance(value, dict):
            if all(isinstance(k, str) for k in value):
                return {k: BridgeClient.encode(v) for k, v in value.items()}
            return {"__d": [[BridgeClient.encode(k), BridgeClient.encode(v)] for k, v in value.items()]}
        return value

    def request(self, op, **fields):
        with self.lock:
            if self.closed:
                raise NotConnected("bridge connection closed")
            if self.pending_release and op != "release":
                hs, self.pending_release = self.pending_release, []
                try:
                    self._send({"op": "release", "hs": hs})
                except ResolveError:
                    pass
            return self.decode(self._send(dict(fields, op=op)))

    def _send(self, msg):
        msg["id"] = self.next_id
        msg["token"] = self.token
        self.next_id += 1
        try:
            self.file.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.file.flush()
            line = self.file.readline()
        except socket.timeout:
            self.close()
            raise ResolveError("Resolve did not answer within %ss" % BRIDGE_TIMEOUT)
        except OSError as e:
            self.close()
            raise NotConnected("lost connection to the Resolve bridge: %s" % e)
        if not line:
            self.close()
            raise NotConnected("the Resolve bridge closed the connection (Resolve closed or bridge stopped)")
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            raise ResolveError(resp.get("error", "unknown bridge error"))
        return resp.get("result")

    def release_later(self, h):
        self.pending_release.append(h)


class RemoteObject(object):
    """Proxy for a Resolve API object living inside Resolve; method calls are forwarded."""

    __slots__ = ("_client", "_h", "_t", "__weakref__")

    def __init__(self, client, h, t):
        self._client = client
        self._h = h
        self._t = t
        if h:
            weakref.finalize(self, client.release_later, h)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.isupper():
            # Constants such as resolve.EXPORT_AAF are attributes, not methods.
            return self._client.request("attr", h=self._h, n=name)

        def method(*args):
            return self._client.request("call", h=self._h, m=name, a=BridgeClient.encode(list(args)))
        method.__name__ = name
        return method

    def __repr__(self):
        return "<%s>" % self._t

    __str__ = __repr__

    def __bool__(self):
        return True


# ---------------------------------------------------------------------------
# Direct transport (Studio)
# ---------------------------------------------------------------------------

def try_direct():
    if os.environ.get("RESOLVE_PILOT_DISABLE_DIRECT") == "1":
        return None
    p = resolve_paths()
    if not os.path.exists(p["lib"]):
        return None
    os.environ.setdefault("RESOLVE_SCRIPT_API", p["api_dir"])
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", p["lib"])
    mod_dir = os.path.join(p["api_dir"], "Modules")
    if mod_dir not in sys.path:
        sys.path.append(mod_dir)
    try:
        import DaVinciResolveScript as dvr
        r = dvr.scriptapp("Resolve")
        if r is not None and r.GetVersionString():
            return r
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Link
# ---------------------------------------------------------------------------

def json_safe(v, depth=0):
    if depth > 40:
        return "<max depth>"
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    if isinstance(v, (list, tuple, set)):
        return [json_safe(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): json_safe(x, depth + 1) for k, x in v.items()}
    if isinstance(v, RemoteObject):
        return repr(v)
    return "<%s>" % str(v).split(" ", 1)[0]


class ResolveLink(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.client = None
        self.root = None
        self.mode = None
        self.exec_ns = None

    def _bridge_state(self):
        try:
            with open(STATE_FILE) as fh:
                return json.load(fh)
        except Exception:
            return None

    def _alive(self):
        if self.mode == "bridge":
            return self.client is not None and not self.client.closed
        if self.mode == "direct":
            try:
                return bool(self.root.GetVersionString())
            except Exception:
                return False
        return False

    def resolve(self):
        """Return a live resolve object, (re)connecting if needed."""
        with self.lock:
            if self._alive():
                return self.root
            self.mode, self.exec_ns = None, None
            st = self._bridge_state()
            if st:
                try:
                    c = BridgeClient(st)
                    c.handshake()
                    self.client, self.root, self.mode = c, RemoteObject(c, 0, "Resolve"), "bridge"
                    return self.root
                except Exception:
                    pass
            d = try_direct()
            if d is not None:
                self.root, self.mode = d, "direct"
                return d
            raise NotConnected(not_connected_help())

    def info(self):
        with self.lock:
            r = self.resolve()
            if self.mode == "bridge":
                h = dict(self.client.hello or {})
                return dict(h, transport="bridge", port=self.client.port)
            return {"transport": "direct", "product": r.GetProductName(), "version": r.GetVersionString()}

    def run(self, fn, args=None):
        """Run resolve_ops.<fn>(resolve, args). Retries once if the bridge connection dropped."""
        with self.lock:
            for attempt in (0, 1):
                r = self.resolve()
                try:
                    return json_safe(getattr(resolve_ops, fn)(r, args or {}))
                except NotConnected:
                    self.mode = None
                    if attempt:
                        raise

    def exec_python(self, code, args=None, fresh=False):
        with self.lock:
            r = self.resolve()
            if self.exec_ns is None or fresh:
                self.exec_ns = {"resolve": r, "ops": resolve_ops, "__name__": "__resolve_exec__"}
                try:
                    self.exec_ns["fusion"] = r.Fusion()
                except Exception:
                    pass
            ns = self.exec_ns
            ns["args"] = args or {}
            ns.pop("result", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, "<resolve_exec>", "exec"), ns)
            return {"stdout": out.getvalue()[-100000:], "result": json_safe(ns.get("result"))}

    def reset(self):
        """Drop the current connection so the next call reconnects (e.g. after a bridge restart)."""
        with self.lock:
            if self.client:
                self.client.close()
            self.client, self.root, self.mode, self.exec_ns = None, None, None, None

    def exec_lua(self, code, args=None):
        with self.lock:
            self.resolve()
            if self.mode != "bridge":
                raise ResolveError("Lua execution needs the ResolvePilot Bridge (use language=python in direct mode)")
            return json_safe(self.client.request("exec_lua", code=code, args=args or {}))


def console_command():
    return "dofile([[%s]])" % bridge_install_path().replace("\\", "/")


def not_connected_help():
    running = resolve_running()
    steps = []
    if running is False:
        steps.append("DaVinci Resolve is not running: start it (tool resolve_launch) and open a project.")
    if not os.path.exists(bridge_install_path()):
        steps.append("Install the bridge script (tool resolve_install_bridge), then restart Resolve.")
    steps.append("Start the bridge: call the resolve_start_bridge tool (types one line into Resolve's Console), or "
                 "in Resolve press F6 and run: %s" % console_command())
    steps.append("Studio users may instead enable Preferences > System > General > External scripting using: Local.")
    return "Cannot reach DaVinci Resolve. " + " ".join("%d) %s" % (i + 1, s) for i, s in enumerate(steps))
