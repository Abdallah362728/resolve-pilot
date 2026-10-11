# ResolvePilot field notes

Verified on DaVinci Resolve Free 21.1.1 (Windows). Read the section you need.

## Media and frame rates
- **VFR phone and screen recordings** are misread: Resolve guesses a rate (26, 41 and 65 fps have been seen), and playback goes frame by frame.
  - Check with `ffprobe`: if `r_frame_rate` ≠ `avg_frame_rate`, the file is VFR.
  - Transcode to CFR into a **new folder** before importing: `ffmpeg -i in.mp4 -vf fps=30 -c:v libx264 -crf 12 -c:a copy out.mp4`.
- **Audio that plays poorly:** convert raw ADTS `.aac` (no container) and audio inside **AV1** videos to 48 kHz WAV.
- **Audio source frames use the clip's own fps.** For a 20 fps clip's audio, `0..81` covers what 121 timeline frames do at 30 fps.
- **Free on Windows has no hardware HEVC decode.** 4K 10-bit HEVC phone footage stutters. What fixed it:
  1. Make frame-exact ProRes Proxy copies: `ffmpeg -i in.mov -vf "setpts=N/(30)/TB,scale=1080:1920" -r 30 -c:v prores_ks -profile:v 0 -c:a pcm_s16le proxy.mov`.
  2. Link them with `media_clip_update {clips, proxy_path}` (`MediaPoolItem.LinkProxyMedia`).
  3. Set these project settings: `perfProxyMediaMode:"1"`, `perfRenderCacheMode:"smart"`, `perfAutoRenderCacheTransition:"1"`, `perfAutoRenderCacheComposite:"1"`, `perfAutoRenderCacheFuEffect:"1"`.
  4. Renders still use the originals; output was bit-identical to the pre-proxy export.
- **Playback frame rate** (`timelinePlaybackFrameRate`) can't be set by script.
  - If it differs from the timeline rate, previews lag. `resolve_status` and `project_create` warn about this.
  - Ask the user to fix it: *File > Project Settings > Master Settings*.

## Edit semantics
- **`endFrame` is exclusive**, in raw `MediaPool.AppendToTimeline` too: `0..114` gives 114 frames.
- **Appends never overwrite.** If the range overlaps an existing clip, the existing clip wins and the new one is trimmed or not placed.
  - `timeline_append` reports these as `not_placed`.
  - Clear the slot first, or append videos first and stills into the gaps.
- **Stills:** `timeline_append` gives them exactly `end_frame - start_frame` frames. It sets the clip's FPS and mark in/out briefly, then restores them.
- **Freeze frame:** `speed:{Percentage:0, RippleTimeline:false}`.
- **No sub-clip trim API.** To re-cut a clip:
  1. Delete it.
  2. Append it again with the new source in/out.
  3. Re-apply its Zoom/Pan/Tilt/CDL.
- **`GetLeftOffset()` is unreliable on stills and frozen clips.** It returns the timeline start (e.g. 108000), not 0.
- **Keep the original:** `timeline_manage duplicate` copies the timeline, keeping Fusion comps, CDLs and transitions.
- **Batching:** many calls are much faster inside one `resolve_exec` using `ops.<fn>(resolve, {...})`, e.g. `ops.timeline_insert`, than as separate tool calls.

## Sizing
- Default input sizing is **scale to fit**.
- **Inspector Pan/Tilt** are output-pixel offsets (Tilt + is up). They apply after Zoom, around the frame centre.
- **A clip's Fusion comp runs at source resolution, before Inspector sizing.**
  - Positions in Fusion are normalised to the source frame: `X = 0.5 + pan_px / (fit_width * zoom)` and `Y = 0.5 + tilt_px / (fit_height * zoom)`.
  - Scaling about a source point (Transform `Pivot` + `Size`) maps to the same scale about the matching output point.
- **Quirk:** a very large still (5112×1992) with a Fusion comp rendered about 25% smaller than the fit maths predicts.
  - Measure the bars in a short test render and correct the Inspector zoom.

## Fusion
- **Script Fusion in Lua** (`resolve_exec language:"lua"`).
  - From Python, tool inputs aren't attributes: `tool.Center` is a function, so `GetConnectedOutput` fails.
  - If you must use Python, wrap edits in `comp.Lock()` … `finally: comp.Unlock()`.
- **Commit scripted changes or they won't render:** `fusion_comp {item, action:"commit"}` exports, re-imports and loads the comp.
  - `fusion_comp set_inputs` commits automatically.
- **Animate:**
  - Position: `tool:AddModifier("Center","XYPath")`, then set `path.X[f]` and `path.Y[f]`.
  - Size: `tool:AddModifier("Size","BezierSpline")`, then set `tool.Size[f]`.
  - Comp frames are 0-based from the clip's first frame (`COMPN_RenderStart`).
  - Default splines ease in and out. For linear motion, key every frame.
- **Fusion output cache:** use `timeline_item_update {fusion_output_cache:"auto"|"on"|"off"}`. The raw `SetFusionOutputCache` accepts only `resolve.CACHE_AUTO_ENABLED`, `CACHE_ENABLED` and `CACHE_DISABLED`, not strings.

## Titles and captions
- **Arabic and other RTL text:** Text+ shapes it correctly with `Font:"Arial", Style:"Bold"`.
- **Text+ inputs:**
  - Outline: `Enabled2, Thickness2, Red2/Green2/Blue2, Alpha2`.
  - Shadow: `Enabled3, Red3/Green3/Blue3, Alpha3, Softness3, ShadowOffset3`.
- **Captions from an SRT:** `captions_from_srt {path, style}`, typically after `transcribe`.
- **Editing placed titles by hand:** they are compound clips. Right-click the clip, choose *Open in Timeline*, select the inner Text+ and edit it in the Inspector.
  - The API cannot drop a bare Text+ onto an upper track. `mode:"insert"` gives an editable Text+ on V1 but ripples V1.
- **Subtitle tracks:** `AppendToTimeline` ignores `recordFrame` for SRT subtitle clips and appends them after the timeline end. Don't pass `startFrame`/`endFrame` for subtitle clips: that crashed Resolve 21.1.

## Transitions
- `list_transitions` lists the valid names.
  - `"Cross Dissolve"` uses `category:"simple"`.
  - Fusion ones (e.g. `"Glitch"`, `"Zoom In"`) need `category:"fusion"`.
- `GetItemListInTrack` includes transitions. Filter them out (`GetType() == "transition"`) when looping over clips.

## Audio
- **Clip volume:** `item.SetProperty("AudioVolume", dB)`.
- **Fades:** `timeline_item_update {fades:{FadeIn:0, FadeOut:n}}`. On a video item this is a fade to black.
- **Check for clipping after setting levels.** Render, then count samples at |x| ≥ 0.99. Setting every clip to 0 dB clipped talking heads.

## Color
- **CDL values are space-separated strings:** `item.SetCDL({"NodeIndex":"1","Slope":"r g b","Offset":"r g b","Power":"r g b","Saturation":"1.12"})`.

## Rendering
- **Range:**
  - `render_timeline` renders all frames unless you give `mark_in`/`mark_out`.
  - `export_frame` resets the one-frame range it uses.
- **Fast checks:** render a 20–80 frame range (about 2 s), then pull frames with ffmpeg. OpenCV can't seek reliably in Resolve's H.264 output.

## UI and desktop control
- Resolve dialogs such as Project Settings can open on the **second monitor**. If a click seems to do nothing, check the other display.
- Synthetic `Shift+9` and the File menu didn't open Project Settings. The gear icon at the bottom right did.
