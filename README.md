# ClipScribe Pro — phone-friendly deployment

This repository contains:
- `index.html` — GitHub Pages website (repository root; ready for Pages)
- `manifest.webmanifest`, `sw.js`, `icons/` — installable PWA shell
- `backend/` — Python/FastAPI processing service
- `Dockerfile`, `render.yaml` — backend deployment starter

## Important architecture

GitHub Pages hosts the website only. It cannot run Python, FFmpeg, Whisper, OCR or speaker diarization. The backend must be deployed separately. The website then connects to the backend URL.

## Deploy using only an Android phone

### Part A — Upload the project to GitHub

1. Download the ZIP from ChatGPT and extract it using your phone's Files app or a ZIP extractor.
2. Open GitHub in Chrome and sign in.
3. Create a new repository, for example `clipscribe-pro`.
4. Open the repository and use **Add file → Upload files**.
5. Select the extracted project contents and upload them, keeping their folder structure. Make sure `index.html` is at the repository root (not only inside `frontend/`).
6. Commit the files.

If the mobile upload picker makes selecting many files difficult, use GitHub in Chrome with **Desktop site** enabled. Upload folders/files in batches while preserving the directory structure.

### Part B — Publish the website

1. In the repository, open **Settings → Pages**.
2. Under Build and deployment, choose **Deploy from a branch**.
3. Select branch `main` and folder `/ (root)`.
4. Save and wait for GitHub Pages to publish.
5. Your website address will look like `https://YOUR_USERNAME.github.io/clipscribe-pro/`.

### Part C — Deploy the Python backend

The backend is a separate service. Use a Docker-capable host such as Render:

1. Open the hosting provider dashboard in Chrome and sign in.
2. Create a new Web Service from your GitHub repository.
3. Choose Docker / use the root `Dockerfile`.
4. Set a service region and an instance with enough memory and disk for the models. A small free instance is not suitable for the full feature set; model downloads and OCR/diarization can exceed its memory.
5. Configure environment variables:
   - `MAX_UPLOAD_MB=800`
   - `MAX_DURATION_SECONDS=14400`
   - `USE_CUDA=0`
   - `ALLOWED_ORIGINS=https://YOUR_USERNAME.github.io`
   - `HF_TOKEN` only if enabling speaker diarization
   - `OPENAI_API_KEY` only if enabling AI cleanup/summary/screenplay enhancement
6. Deploy. Wait for the service to finish building and starting.
7. Open `https://YOUR-BACKEND-URL/api/health`. It should return JSON containing `"ok": true`.

Hosting costs and available instance sizes change. Check the provider's current pricing before deploying; professional transcription models usually require paid compute.

### Part D — Connect website and backend

1. Open your GitHub Pages website.
2. Paste the backend base URL into **Backend API address** (example: `https://your-service.onrender.com`).
3. Tap **Test backend**.
4. If it says Connected, upload a short test clip and choose the `base` or `small` model.
5. Enable OCR or speaker diarization only after basic transcription works.

## Install it like an app on Android

After GitHub Pages is live:
1. Open the website in Chrome.
2. Open the three-dot menu.
3. Tap **Install app** or **Add to Home screen** (wording varies).
4. Confirm. ClipScribe Pro will get an icon on your home screen.

The PWA caches the website shell, not the Python backend or AI models. Internet and a live backend are still required to transcribe.

## Local development

Install Python 3.10+ and FFmpeg, then:
```bash
python -m venv .venv
# Android/Termux users: this project is not packaged for direct Termux execution.
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

## Feature inventory

- Whisper speech recognition, multiple model sizes, multilingual transcription
- Speech translation to English
- Voice activity detection and optional word-level timestamps
- FFmpeg embedded subtitle extraction
- EasyOCR burned-in caption reading
- Optional pyannote speaker diarization
- FFmpeg scene-cut detection
- Transcript-based screenplay/dialogue draft
- Optional OpenAI transcript cleanup, summary, screenplay formatting
- TXT, SRT, VTT, JSON, DOCX and PDF exports

## Limitations and troubleshooting

- This is not a magic universal downloader. Video URLs must be direct downloadable media URLs; platform watch-page links (YouTube, Instagram, TikTok, Netflix, etc.) are not supported.
- Some remote hosts block cloud downloads, require cookies, or use redirects. Upload the file instead.
- Embedded subtitles require a real subtitle stream. Burned-in captions are pixels and need OCR.
- OCR is sampled and can miss fast-changing, stylized, small, low-contrast, or unsupported-language text.
- Diarization needs `HF_TOKEN`, model access approval, and enough memory. It labels anonymous speakers; it does not identify actors by real name.
- OpenAI enhancements need `OPENAI_API_KEY` and incur usage charges.
- Large Whisper models, OCR and diarization are resource-intensive. Use a paid backend with sufficient RAM; start with `base` or `small`.
- Very long videos may exceed web-service request timeouts. Production use should add background jobs, chunked processing, storage and user authentication.
- If GitHub Pages shows 404, confirm `index.html` is at repository root and Pages is set to `main` / root.
- If browser shows CORS error, set `ALLOWED_ORIGINS` to exactly `https://YOUR_USERNAME.github.io` and redeploy backend.
- If backend health check fails, inspect provider logs; confirm Docker build, FFmpeg installation and service port.
- If backend crashes during model loading, choose a larger instance or a smaller Whisper model.
- If OCR fails, inspect logs and verify EasyOCR/OpenCV installed.
- If speaker diarization fails, verify HF token, model terms and RAM.
- If PDF/DOCX export fails, check `reportlab` and `python-docx` installation.

## Security and privacy

- Keep tokens only in backend environment variables; never paste secrets into `index.html` or GitHub.
- Restrict `ALLOWED_ORIGINS` to your GitHub Pages origin.
- The URL ingestion endpoint includes basic SSRF protections but is not a hardened public URL-fetching service. For a public multi-user product, add stronger egress filtering, authentication, rate limits, quotas, malware scanning and background jobs.
- Temporary uploads are deleted after each request, but hosting logs and infrastructure policies may still apply.
- Process only media you have permission to use.

## Validation performed for this source package

Python syntax compilation and static feature checks were performed during generation. This is not a live cloud deployment or an end-to-end test with real media. Test the deployed service with a short non-sensitive video before sharing it.
