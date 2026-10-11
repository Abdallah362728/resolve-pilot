"""Start the ResolvePilot bridge by typing one line into DaVinci Resolve's Console (Workspace > Console).

Why: on Resolve (free) only the interactive Console runs Lua with LuaJIT FFI (needed for sockets);
Scripts-menu scripts are sandboxed. This module automates what the user would do by hand:
open the Console (F6), paste `dofile(...)`, press Enter.

Safety: keystrokes are only sent after verifying that Resolve's Console window is the foreground
window; otherwise nothing is typed and the manual instructions are returned.
"""

import os
import subprocess
import sys
import time


def console_command(bridge_path):
    p = bridge_path.replace("\\", "/")
    return "dofile([[%s]])" % p


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _win_start(command):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.IsIconic.argtypes = [wintypes.HWND]

    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=15).stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("resolve.exe"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    if not pids:
        return {"ok": False, "error": "DaVinci Resolve is not running"}

    def windows():
        found = []

        def cb(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                found.append((hwnd, buf.value))
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found

    def activate(hwnd):
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # A synthetic Alt tap allows SetForegroundWindow from a background process.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        return user32.GetForegroundWindow() == hwnd

    def key(vk, up=False):
        user32.keybd_event(vk, 0, 2 if up else 0, 0)

    def tap(vk, ctrl=False):
        if ctrl:
            key(0x11)
        key(vk)
        key(vk, True)
        if ctrl:
            key(0x11, True)
        time.sleep(0.05)

    wins = windows()
    main = [w for w in wins if w[1].startswith("DaVinci Resolve")]
    console = [w for w in wins if w[1] == "Resolve"]
    if not main:
        return {"ok": False, "error": "Resolve main window not found (is a project open?)"}
    if not console:
        if not activate(main[0][0]):
            return {"ok": False, "error": "could not bring Resolve to the foreground"}
        tap(0x75)  # F6 opens Workspace > Console
        for _ in range(20):
            time.sleep(0.25)
            console = [w for w in windows() if w[1] == "Resolve"]
            if console:
                break
    if not console:
        return {"ok": False, "error": "the Console window did not open (F6 may be remapped)"}
    hwnd = console[0][0]
    if not activate(hwnd):
        return {"ok": False, "error": "could not focus the Console window"}

    clip_prev = _win_clipboard_get()
    try:
        if not _win_clipboard_set(command):
            return {"ok": False, "error": "could not use the clipboard"}
        if user32.GetForegroundWindow() != hwnd:
            return {"ok": False, "error": "focus changed before typing; aborted"}
        tap(0x56, ctrl=True)  # Ctrl+V
        time.sleep(0.15)
        tap(0x0D)  # Enter
        time.sleep(0.3)
    finally:
        if clip_prev is not None:
            _win_clipboard_set(clip_prev)
    return {"ok": True}


def _win_clipboard_get():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _win_clipboard_set(text):
    try:
        env = dict(os.environ, RESOLVE_PILOT_CLIP=text)
        p = subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:RESOLVE_PILOT_CLIP"],
                           env=env, capture_output=True, text=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# macOS (needs Accessibility permission for the app running Claude Code)
# ---------------------------------------------------------------------------

_MAC_SCRIPT = '''
on run argv
    set cmd to item 1 of argv
    set oldClip to ""
    try
        set oldClip to the clipboard as text
    end try
    tell application "System Events"
        set procs to (application processes whose name contains "Resolve")
        if (count of procs) is 0 then error "DaVinci Resolve is not running"
        set p to item 1 of procs
        set frontmost of p to true
        delay 0.5
        set consoleWin to missing value
        repeat with w in (windows of p)
            if name of w is "Resolve" or name of w is "Console" then set consoleWin to w
        end repeat
        if consoleWin is missing value then
            try
                click menu item "Console" of menu "Workspace" of menu bar 1 of p
            on error
                key code 97 -- F6
            end try
            repeat 20 times
                delay 0.25
                repeat with w in (windows of p)
                    if name of w is "Resolve" or name of w is "Console" then set consoleWin to w
                end repeat
                if consoleWin is not missing value then exit repeat
            end repeat
        end if
        if consoleWin is missing value then error "the Console window did not open"
        perform action "AXRaise" of consoleWin
        delay 0.3
        if not (frontmost of p) then error "Resolve is not frontmost; aborted"
        if name of (front window of p) is not (name of consoleWin) then error "Console is not focused; aborted"
        set the clipboard to cmd
        keystroke "v" using command down
        delay 0.1
        key code 36 -- Return
    end tell
    delay 0.3
    set the clipboard to oldClip
end run
'''


def _mac_start(command):
    p = subprocess.run(["osascript", "-e", _MAC_SCRIPT, command], capture_output=True, text=True, timeout=45)
    if p.returncode != 0:
        return {"ok": False, "error": "osascript failed (grant Accessibility permission to the app running Claude "
                                      "Code in System Settings > Privacy & Security): %s" % p.stderr.strip()}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Linux (X11 + xdotool + xclip)
# ---------------------------------------------------------------------------

def _linux_start(command):
    from shutil import which
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return {"ok": False, "error": "automatic start is not possible on a pure Wayland session"}
    if not which("xdotool") or not which("xclip"):
        return {"ok": False, "error": "automatic start on Linux needs xdotool and xclip"}
    quiet = {"capture_output": True, "text": True, "timeout": 10}

    def find(pattern):
        return subprocess.run(["xdotool", "search", "--onlyvisible", "--name", pattern], **quiet).stdout.split()

    console = find("^Resolve$")
    if not console:
        main = find("^DaVinci Resolve")
        if not main:
            return {"ok": False, "error": "Resolve window not found"}
        subprocess.run(["xdotool", "windowactivate", main[0]], **quiet)
        time.sleep(0.5)
        subprocess.run(["xdotool", "key", "F6"], **quiet)
        for _ in range(20):
            time.sleep(0.25)
            console = find("^Resolve$")
            if console:
                break
    if not console:
        return {"ok": False, "error": "the Console window did not open"}
    subprocess.run(["xdotool", "windowactivate", console[0]], **quiet)
    time.sleep(0.4)
    active = subprocess.run(["xdotool", "getactivewindow"], **quiet).stdout.strip()
    if active != console[0]:
        return {"ok": False, "error": "could not focus the Console window"}
    old = subprocess.run(["xclip", "-selection", "clipboard", "-o"], **quiet).stdout
    subprocess.run(["xclip", "-selection", "clipboard"], input=command, capture_output=True, text=True, timeout=10)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v", "Return"], **quiet)
    time.sleep(0.3)
    subprocess.run(["xclip", "-selection", "clipboard"], input=old, capture_output=True, text=True, timeout=10)
    return {"ok": True}


def start_bridge(bridge_path, wait_for_state, timeout=15.0):
    """Type the bridge command into Resolve's Console, then wait until the bridge reports in."""
    command = console_command(bridge_path)
    try:
        if sys.platform.startswith("win"):
            res = _win_start(command)
        elif sys.platform == "darwin":
            res = _mac_start(command)
        else:
            res = _linux_start(command)
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    res["console_command"] = command
    if res.get("ok"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if wait_for_state():
                res["bridge_running"] = True
                return res
            time.sleep(0.5)
        res["ok"] = False
        res["error"] = "command was typed but the bridge did not report in; check the Console for errors"
    res["manual_steps"] = ("In Resolve press F6 (Workspace > Console), paste the console_command line, press Enter. "
                           "Next time just press F6, Up-arrow, Enter.")
    return res
