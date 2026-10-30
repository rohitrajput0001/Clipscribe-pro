import os, re, json, uuid, shutil, tempfile, subprocess, ipaddress, socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import requests

APP_NAME = "ClipScribe Pro"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "800"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "14400"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"]
)

def run(args, timeout=1800):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Processing command failed")[-3000:])
    return p.stdout

def safe_remote_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Only direct http(s) media URLs are accepted.")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        raise HTTPException(400, "Local network URLs are not allowed.")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "URLs pointing to private or reserved networks are blocked.")
    except socket.gaierror:
        raise HTTPException(400, "Could not resolve the video host.")
    return url

def download_remote(url, dest):
    safe_remote_url(url)
    total = 0
    try:
        with requests.get(url, stream=True, timeout=(15, 60), allow_redirects=False,
                          headers={"User-Agent": "ClipScribePro/1.0"}) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("Location")
                if not location:
                    raise HTTPException(400, "The video host returned an invalid redirect.")
                from urllib.parse import urljoin
                safe_remote_url(urljoin(url, location))
                raise HTTPException(400, "Redirected video URLs are not supported. Paste the final direct media URL.")
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").lower()
            if not any(x in ctype for x in ("video/", "application/octet-stream", "audio/")):
                raise HTTPException(400, f"The URL did not return a media file (content type: {ctype or 'unknown'}).")
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_UPLOAD_MB * 1024 * 1024:
                            raise HTTPException(413, f"Remote file exceeds {MAX_UPLOAD_MB} MB limit.")
                        f.write(chunk)
    except requests.RequestException as e:
        raise HTTPException(400, f"Could not download media URL: {e}")

def probe_media(path):
    raw = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    data = json.loads(raw)
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration > MAX_DURATION_SECONDS:
        raise HTTPException(413, f"Video is longer than the {MAX_DURATION_SECONDS // 60}-minute limit.")
    streams = data.get("streams", [])
    return {
        "duration": duration,
        "format": data.get("format", {}).get("format_name", "unknown"),
        "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "subtitle_streams": [
            {"index": s["index"], "codec": s.get("codec_name", "unknown"),
             "language": s.get("tags", {}).get("language", "und"),
             "title": s.get("tags", {}).get("title", "")}
            for s in streams if s.get("codec_type") == "subtitle"
        ]
    }

def extract_audio(video, wav):
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)], timeout=1800)

def extract_subtitles(video, stream_index, out):
    run(["ffmpeg", "-y", "-i", str(video), "-map", f"0:{stream_index}", "-c:s", "srt", str(out)])

def parse_srt(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", text.replace("\r", "").strip())
    items = []
    def sec(t):
        h, m, rest = t.split(":")
        s, ms = rest.replace(",", ".").split(".")
        return int(h)*3600 + int(m)*60 + int(s) + float("0."+ms)
    for block in blocks:
        lines = block.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = [x.strip().split()[0] for x in lines[1].split("-->")]
            txt = re.sub(r"<[^>]+>", "", " ".join(lines[2:])).strip()
            if txt:
                items.append({"start": sec(a), "end": sec(b), "text": txt, "source": "embedded_subtitle"})
    return items

def run_ocr(video, sample_interval=1.5, languages=None):
    try:
        import cv2, easyocr
    except ImportError:
        raise RuntimeError("OCR dependencies are not installed. Install opencv-python-headless and easyocr.")
    langs = languages or ["en"]
    reader = easyocr.Reader(langs, gpu=False, verbose=False)
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    step = max(1, int(sample_interval * fps))
    out, frame = [], 0
    while frame < frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = cap.read()
        if not ok: break
        h, w = image.shape[:2]
        crop = image[int(h*0.58):h, :]
        found = reader.readtext(crop, detail=0, paragraph=True)
        txt = " ".join(x.strip() for x in found if x.strip())
        if txt and (not out or txt.casefold() != out[-1]["text"].casefold()):
            out.append({"start": frame/fps, "end": frame/fps + sample_interval,
                        "text": txt, "source": "burned_in_ocr"})
        frame += step
    cap.release()
    return out

def run_diarization(audio_path):
    if not HF_TOKEN:
        raise RuntimeError("Speaker diarization requires HF_TOKEN and acceptance of the pyannote model terms.")
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise RuntimeError("pyannote.audio is not installed in this deployment.")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
    diar = pipeline(str(audio_path))
    turns = []
    for turn, _, speaker in diar.itertracks(yield_label=True):
        turns.append({"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)})
    return turns

def attach_speakers(segments, turns):
    for seg in segments:
        midpoint = (seg["start"] + seg["end"]) / 2
        matches = [t for t in turns if t["start"] <= midpoint <= t["end"]]
        seg["speaker"] = matches[0]["speaker"] if matches else "Unknown"
    return segments

def scene_detection(video):
    # FFmpeg showinfo writes frame timestamps to stderr.
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video), "-vf",
         "select='gt(scene,0.35)',showinfo", "-vsync", "vfr", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800
    )
    log = (p.stderr or "") + "\\n" + (p.stdout or "")
    times = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", log)]
    return sorted(set(times))

def make_script(segments, scenes, metadata):
    # A structured, conservative screenplay-style draft. No fabricated dialogue/actions.
    lines = ["TITLE: Transcript-based scene draft", ""]
    boundaries = [0.0] + scenes
    if not segments:
        return "No speech was detected."
    for i, seg in enumerate(segments):
        if i == 0 or any(abs(seg["start"] - s) < 1.2 for s in scenes):
            lines += ["", f"SCENE / BEAT — {timestamp(seg['start'])}", ""]
        who = seg.get("speaker", "Speaker")
        lines.append(f"{who.upper()}: {seg['text']}")
    lines += ["", "NOTE: Scene breaks are estimated from visual cuts. This draft contains transcribed speech only; it does not invent unseen actions."]
    return "\n".join(lines)

def timestamp(sec):
    sec = max(0, float(sec))
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

def llm_enhance(segments, mode):
    if not OPENAI_API_KEY:
        return None
    # Optional polishing/summary; source transcript is supplied as data and should be proofread.
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    transcript = "\n".join(f"{x.get('speaker','Speaker')}: {x['text']}" for x in segments)
    prompt = {
        "clean": "Clean punctuation and paragraphing. Do not add or remove meaning. Preserve speaker labels.",
        "summary": "Summarize the transcript accurately. Distinguish facts from uncertainty.",
        "screenplay": "Format the transcript as a screenplay-style dialogue draft. Do not invent dialogue or visual actions."
    }.get(mode, "Clean this transcript faithfully.")
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        input=f"{prompt}\n\nTRANSCRIPT:\n{transcript}"
    )
    return response.output_text

def build_result(video, opts):
    meta = probe_media(video)
    if meta["audio_streams"] == 0:
        raise HTTPException(422, "This file has no audio stream. Subtitle/OCR extraction may still work.")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        extract_audio(video, wav)
        from faster_whisper import WhisperModel
        model_name = opts["model"]
        device = "cuda" if os.getenv("USE_CUDA", "0") == "1" else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        model = WhisperModel(model_name, device=device, compute_type=compute)
        segments_iter, info = model.transcribe(
            str(wav), language=opts.get("language") or None,
            task=opts.get("task", "transcribe"),
            vad_filter=opts.get("vad", True), beam_size=5,
            word_timestamps=opts.get("word_timestamps", False)
        )
        speech = []
        for seg in segments_iter:
            item = {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip(),
                    "source": "speech"}
            if opts.get("word_timestamps") and seg.words:
                item["words"] = [{"start": float(w.start), "end": float(w.end), "word": w.word}
                                 for w in seg.words]
            speech.append(item)

        diar_turns = []
        if opts.get("diarize"):
            diar_turns = run_diarization(wav)
            speech = attach_speakers(speech, diar_turns)

        embedded = []
        for stream in meta["subtitle_streams"]:
            if opts.get("subtitle_stream") is not None and stream["index"] != opts["subtitle_stream"]:
                continue
            srt = Path(td) / f"sub_{stream['index']}.srt"
            try:
                extract_subtitles(video, stream["index"], srt)
                embedded.extend(parse_srt(srt))
            except Exception:
                continue

        ocr = run_ocr(video, opts.get("ocr_interval", 1.5), opts.get("ocr_languages", ["en"])) if opts.get("ocr") else []
        scenes = scene_detection(video) if opts.get("scenes") else []
        all_text = speech + embedded + ocr
        all_text.sort(key=lambda x: (x["start"], x["source"]))
        script = make_script(speech, scenes, meta) if opts.get("screenplay") else None
        enhanced = llm_enhance(speech, opts["enhance"]) if opts.get("enhance") in ("clean", "summary", "screenplay") else None
        return {
            "id": str(uuid.uuid4()), "metadata": meta, "detected_language": info.language,
            "language_probability": float(info.language_probability),
            "speech": speech, "embedded_subtitles": embedded, "on_screen_ocr": ocr,
            "speaker_turns": diar_turns, "scene_changes": scenes,
            "combined": all_text, "screenplay_draft": script, "ai_enhancement": enhanced,
            "warnings": [
                "Automatic speech recognition can make mistakes; proofread important material.",
                "Scene detection finds visual cuts, not semantic scene meaning.",
                "OCR is sampled and may miss fast, stylized, or non-English captions."
            ]
        }

@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": "1.0.0"}

@app.post("/api/transcribe")
async def transcribe(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    model: str = Form("small"),
    language: str = Form(""),
    task: str = Form("transcribe"),
    diarize: bool = Form(False),
    ocr: bool = Form(False),
    scenes: bool = Form(True),
    screenplay: bool = Form(True),
    vad: bool = Form(True),
    word_timestamps: bool = Form(False),
    subtitle_stream: Optional[int] = Form(None),
    enhance: str = Form("none"),
    ocr_interval: float = Form(1.5)
):
    if not file and not url:
        raise HTTPException(400, "Upload a video or provide a direct media URL.")
    if task not in ("transcribe", "translate"):
        raise HTTPException(400, "Invalid task.")
    if model not in ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3"):
        raise HTTPException(400, "Unsupported Whisper model.")
    if enhance not in ("none", "clean", "summary", "screenplay"):
        raise HTTPException(400, "Invalid enhancement mode.")

    with tempfile.TemporaryDirectory() as td:
        suffix = Path(file.filename).suffix if file and file.filename else ".mp4"
        media = Path(td) / ("input" + (suffix if len(suffix) < 10 else ".mp4"))
        if file:
            data = await file.read()
            if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_MB} MB limit.")
            media.write_bytes(data)
        else:
            download_remote(url, media)
        result = build_result(media, {
            "model": model, "language": language, "task": task, "diarize": diarize,
            "ocr": ocr, "scenes": scenes, "screenplay": screenplay, "vad": vad,
            "word_timestamps": word_timestamps, "subtitle_stream": subtitle_stream,
            "enhance": enhance, "ocr_interval": ocr_interval
        })
        return JSONResponse(result)

@app.post("/api/export/docx")
async def export_docx(payload: dict):
    try:
        from docx import Document
        doc = Document()
        doc.add_heading("ClipScribe Pro Transcript", 0)
        doc.add_paragraph(f"Detected language: {payload.get('detected_language', 'unknown')}")
        for item in payload.get("speech", []):
            p = doc.add_paragraph()
            p.add_run(f"[{timestamp(item['start'])}] ")
            if item.get("speaker"):
                p.add_run(item["speaker"] + ": ").bold = True
            p.add_run(item["text"])
        import io
        buf = io.BytesIO(); doc.save(buf); buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": 'attachment; filename="transcript.docx"'})
    except Exception as e:
        raise HTTPException(500, f"DOCX export failed: {e}")

@app.post("/api/export/pdf")
async def export_pdf(payload: dict):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        import io
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
        styles = getSampleStyleSheet()
        story = [Paragraph("ClipScribe Pro Transcript", styles["Title"]), Spacer(1, 8)]
        for item in payload.get("speech", []):
            text = f"[{timestamp(item['start'])}] {item.get('speaker','')} {item['text']}"
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(text, styles["BodyText"]))
            story.append(Spacer(1, 4))
        doc.build(story); buf.seek(0)
        return StreamingResponse(buf, media_type="application/pdf",
                                 headers={"Content-Disposition": 'attachment; filename="transcript.pdf"'})
    except Exception as e:
        raise HTTPException(500, f"PDF export failed: {e}")
