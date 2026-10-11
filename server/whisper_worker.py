#!/usr/bin/env python3
"""Transcription worker, run by ai_tools.py with the Python that has the speech engine installed.

Usage: python whisper_worker.py <request.json> <result.json>

request: {engine: faster-whisper|openai-whisper, input, model, language, task, device, word_timestamps}
result:  {ok, engine, model, device, language, duration, segments:[{start, end, text, words?}]} or {ok:false, error}
"""

import json
import sys
import traceback


def run_faster(req, device):
    from faster_whisper import WhisperModel
    compute = "int8" if device == "cpu" else "default"
    model = WhisperModel(req["model"], device=device, compute_type=compute)
    segs, info = model.transcribe(req["input"], language=req.get("language"), task=req.get("task", "transcribe"),
                                  word_timestamps=bool(req.get("word_timestamps")), vad_filter=True)
    out = []
    for s in segs:  # lazy generator: decoding errors surface here
        seg = {"start": s.start, "end": s.end, "text": s.text.strip()}
        if s.words:
            seg["words"] = [{"start": w.start, "end": w.end, "word": w.word} for w in s.words]
        out.append(seg)
    return {"language": info.language, "duration": info.duration, "segments": out}


def run_openai(req, device):
    import whisper
    model = whisper.load_model(req["model"], device=None if device == "auto" else device)
    res = model.transcribe(req["input"], language=req.get("language"), task=req.get("task", "transcribe"),
                           word_timestamps=bool(req.get("word_timestamps")), fp16=(model.device.type == "cuda"))
    out = []
    for s in res["segments"]:
        seg = {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        if s.get("words"):
            seg["words"] = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in s["words"]]
        out.append(seg)
    duration = out[-1]["end"] if out else 0.0
    return {"language": res.get("language"), "duration": duration, "segments": out}


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        req = json.load(f)
    fn = run_faster if req["engine"] == "faster-whisper" else run_openai
    device = req.get("device") or "auto"
    try:
        try:
            res = fn(req, device)
        except Exception:
            if device != "auto":
                raise
            # GPU found but its runtime libraries (CUDA/cuDNN) are missing or broken: fall back to CPU.
            device = "cpu"
            res = fn(req, device)
        res.update(ok=True, engine=req["engine"], model=req["model"], device=device)
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-2000:]}
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
