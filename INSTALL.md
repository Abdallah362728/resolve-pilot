# How to install ResolvePilot

ResolvePilot lets Claude edit, grade and render videos in **DaVinci Resolve**, both the free edition and Studio. Setup takes about 10 minutes, once per computer.

---

## 1. Install the prerequisites

### a) DaVinci Resolve
Download it from the official site and install it. The free version is fine:
https://www.blackmagicdesign.com/products/davinciresolve

Open it once, finish the first-run wizard, then close it.

### b) Claude Code
You need either the **Claude desktop app**, which has a *Code* tab, or the **Claude Code CLI**:
https://claude.com/claude-code

### c) uv (runs the plugin's small Python server)

**Windows** (PowerShell):
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux**:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen your terminal (and the Claude app) afterwards so `uv` is on your PATH. Check it with `uv --version`.

You don't need to install Python yourself, because uv downloads one if needed.

---

## 2. Install the plugin

### Option A: from a terminal (simplest)
```bash
claude plugin marketplace add Abdallah362728/resolve-pilot
claude plugin install resolve-pilot@resolve-pilot --scope user
```

### Option B: from the Claude desktop app
1. Do the **first** line of Option A in a terminal: `claude plugin marketplace add Abdallah362728/resolve-pilot`. The desktop app has no button for adding a marketplace from GitHub.
2. Open the Claude desktop app → **Code** tab → start a **local** session.
3. Click **+** next to the prompt box → **Plugins** → **Add plugin**.
4. Choose **resolve-pilot** and install it for your **user**.
5. If it isn't listed, restart the app.

### Option C: inside any Claude Code session
```
/plugin marketplace add Abdallah362728/resolve-pilot
/plugin install resolve-pilot@resolve-pilot
```

Restart Claude Code (or the desktop app) after installing.

---

## 3. Connect Claude to Resolve (each time you open Resolve)

1. Start DaVinci Resolve and **open a project**. Any project works, but it must be past the Project Manager screen.
2. In Claude, type:
   ```
   /resolve-pilot:setup
   ```
   You can also just ask for something like *"connect to Resolve"*.

Claude then opens Resolve's Console and starts the bridge for you.

> **If automatic start doesn't work**, start the bridge yourself:
> 1. In Resolve, press **F6** (Workspace → Console).
> 2. Paste the line Claude shows you. It looks like
>    `dofile([[C:/Users/<you>/AppData/Roaming/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility/ResolvePilot Bridge.lua]])`.
> 3. Press **Enter**.
>
> Next time you only need **F6, ↑ (Up arrow), Enter**. You can close the Console window afterwards, and the bridge keeps running.

**Why this step?** The free edition blocks outside programs from controlling it, but its built-in Console is allowed to. The bridge listens only on your own computer (127.0.0.1) and uses a secret token for each session.

**Studio users** can skip this step: in Resolve go to *Preferences → System → General → External scripting using* and choose **Local**.

---

## 4. Use it

Just describe what you want, for example:

> Make a new project "Trip", import everything from `D:\Videos\trip`, build a timeline with the clips in date order, add a title "Summer 2026" over the first 4 seconds, put `music.mp3` on audio track 2, add cross dissolves between clips, and render an H.264 MP4 to `D:\Exports`.

Claude can check its own work by exporting frames and looking at them.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Claude says **"NOT CONNECTED"** | Resolve must be running **with a project open**. Then run `/resolve-pilot:setup`, or use F6 and paste the line. |
| Plugin tools don't appear | Run `uv --version` in a terminal. If that fails, install uv (step 1c) and restart Claude. |
| macOS: uv isn't found when Claude is started from the Dock | In the plugin's `.mcp.json`, replace `"command": "uv"` with uv's full path (`which uv`). |
| macOS: automatic start fails | Give the app that runs Claude (Terminal / Claude) **Accessibility** permission: System Settings → Privacy & Security → Accessibility. Or use F6 and paste the line. |
| Linux: automatic start fails | Install `xdotool` and `xclip` (X11 only), or use F6 and paste the line. |
| A feature says it failed on the free version | Magic Mask, AI transcription, scene-cut detection, voice isolation and exports above 4K/60fps are **Studio-only**. |
| Resolve was restarted | Start the bridge again (step 3). It runs once per Resolve session. |

## Update or remove

```bash
claude plugin marketplace update resolve-pilot
claude plugin update resolve-pilot@resolve-pilot
claude plugin uninstall resolve-pilot@resolve-pilot
```

To remove the bridge script from Resolve, delete `ResolvePilot Bridge.lua` from Resolve's `Fusion/Scripts/Utility` folder.
