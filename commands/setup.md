---
description: Connect Claude to DaVinci Resolve (install the bridge, launch Resolve, start the bridge, verify)
---

Set up the ResolvePilot connection to DaVinci Resolve:

1. Call `resolve_status`.
2. If `resolve_installed` is false, tell the user to install DaVinci Resolve (free) from https://www.blackmagicdesign.com/products/davinciresolve and stop.
3. If `bridge_installed` is false, call `resolve_install_bridge`.
4. If Resolve is not running, call `resolve_launch`. Then ask the user to open a project, or offer to create one once connected. Wait for their confirmation.
5. Call `resolve_start_bridge`. If it fails, show the user the returned `console_command`. They press F6 in Resolve, paste it and press Enter.
6. Call `resolve_status` again. Report the product, version, open project and transport, then briefly list what they can ask for: editing, titles, markers, color, Fusion and rendering.
