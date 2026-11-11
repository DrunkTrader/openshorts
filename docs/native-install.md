# Running OpenShorts without Docker

Docker stays the supported path. This page covers running the backend and the
dashboard directly on Linux (including aarch64) and on Windows.

## Linux (x86_64 or aarch64)

1. System packages: `ffmpeg` (6.x works) and Python 3.11.
2. Backend:
   ```bash
   python3.11 -m venv .venv && . .venv/bin/activate
   pip install torch --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   pip install -U "yt-dlp[default]"
   ```
   `yt-dlp[default]` brings `yt-dlp-ejs`, the challenge solver. Without it
   YouTube answers "n challenge solving failed" / "No video formats found"
   (the Docker image installs it the same way).

   Do not use `requirements-cpu.lock`: it is built for x86_64.
   Verified on aarch64, CPU only, Python 3.11: with torch 2.11 from the CPU
   index, every pin in `requirements.txt` installed unchanged (`mediapipe
   0.10.14`, `faster-whisper`, `ultralytics` all have aarch64 wheels) and
   `pip check` is clean.
3. Dashboard (Node 20 or newer): `cd dashboard && npm ci`.
4. Run: `uvicorn app:app --host 127.0.0.1 --port 8000`, then in another shell
   `cd dashboard && VITE_PROXY_TARGET=http://localhost:8000 npm run dev`.

`bin/ffmpeg` (the NVENC thread/slot cap) is only put on `PATH` inside the
Docker image; native runs use the system `ffmpeg` as is.

## Windows

1. Install Python 3.11, Node 20 or newer and ffmpeg (on `PATH`).
2. Backend, in PowerShell:
   ```powershell
   py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install torch --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   pip install -U "yt-dlp[default]"   # challenge solver, see the Linux notes
   ```
3. Environment (or put the same keys in `.env`):
   ```powershell
   $env:GEMINI_API_KEY = "..."
   $env:MAX_CONCURRENT_JOBS = "1"
   $env:WHISPER_MODEL = "small"   # "base" on 2 cores
   ```
4. Run: `uvicorn app:app --host 127.0.0.1 --port 8000`; in a second terminal
   `cd dashboard; npm install; $env:VITE_PROXY_TARGET = "http://localhost:8000"; npm run dev`.

Job processes are forced to UTF-8 output on Windows, and the host-wide NVENC
slot lock (POSIX `fcntl`) is skipped. The `JOB_CPU` log line is absent there.
The test suite runs on `windows-latest` in CI.

## Sizing for small machines (2 cores, no GPU)

Set `MAX_CONCURRENT_JOBS=1` and `WHISPER_MODEL=small` or `base`.

## Voice activity detection

Whisper runs with faster-whisper's bundled silero VAD (`WHISPER_VAD=0` turns it
off, `WHISPER_VAD_MIN_SILENCE_MS` sets its silence gap). It needs `onnxruntime`,
a faster-whisper dependency: checked on Linux aarch64 (faster-whisper 1.2.1,
onnxruntime 1.31). On Windows and Windows-on-ARM the wheel is installed by pip
the same way; if no wheel exists for your interpreter, set `WHISPER_VAD=0`
(music and noise may then come back as stray words).

## Smoke test

`uvicorn app:app --port 8000`, then `GET http://127.0.0.1:8000/docs` must
return 200. A full run (a 1 to 2 minute upload) needs a Gemini key or an
OpenAI-compatible server (`LLM_BASE_URL`); it has not been verified end to end
in this setup.
