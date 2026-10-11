# Lessons

- **Test the real user entry point early.**
  - What happened: I validated the bridge by launching it from the Console, assumed the Scripts menu would behave the same, and told the user to click it. It didn't: menu scripts are sandboxed.
  - Rule: before telling the user to perform a step, perform it myself in the same context, including menu launches.
- **Don't rely on UI features without checking the edition.** UIManager dialogs triggered a Studio upsell on Free. Probe with harmless signals (for example `OpenPage` as a side channel) before building UI.
- **When a host app hangs, stop and bisect.**
  - Kill and restart the app, then reproduce with per-call prints and `timeout`.
  - Change one variable at a time: combined vs separate calls, with or without a compound clip.
  - Don't retry the full suite blindly.
- **Keep the user informed while working autonomously.** Long silent debugging made the user ask whether I was looping. Post a one-line status at each diagnosis milestone.
- **Never send blind keystrokes.** Verify the foreground window first (`autostart.py` does), because a pasted Ctrl+V into Resolve's timeline is an edit.
