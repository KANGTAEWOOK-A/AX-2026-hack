# Wind Recheck for Mac — Quick Start

**Works with Apple Silicon (M1/M2/M3/M4) and Intel Macs**, provided Python 3.10–3.14 and supported dependency wheels are available for your macOS version.

## 1. Install Python (first time only)

In Terminal, run `python3 --version`. Use Python **3.12** if possible.

If your Mac has only old Python 3.9 or no Python, install Python 3.12 from python.org, or with Homebrew:

```bash
brew install python@3.12
```

Homebrew is optional. You do **not** need Docker, Xcode, or a GPU for the web MVP.

## 2. Unzip and start

1. Double-click `Wind_Recheck_Mac.zip` to extract it.
2. Open the `wind_recheck_mvp` folder.
3. Double-click `Start_Wind_Recheck_Mac.command`.
4. On the first run, a Terminal window installs Python dependencies in a private `.venv` folder, creates `.env`, starts the server, and opens a browser at `http://127.0.0.1:8000` (or the next available port).
5. Keep the Terminal window open while using the app. Press **Control+C** to stop it.

If macOS blocks the downloaded `.command` script, you can run it in Terminal:

```bash
cd ~/Downloads/wind_recheck_mvp
chmod +x Start_Wind_Recheck_Mac.command
./Start_Wind_Recheck_Mac.command
```

Adjust the path if you extracted the folder elsewhere. If macOS prompts about an untrusted script, review the script before allowing it to run.

## 3. Connect the real AIStream model

Edit `.env` in the `wind_recheck_mvp` folder and provide the **full POST URL** of your team's wind model:

```dotenv
AISTREAM_WIND_URL=https://YOUR-HOST/jntp-gpu/SV-e434a471/predict/wind
AISTREAM_REQUEST_MODE=multipart
AISTREAM_IMAGE_FIELD=file
```

The displayed address is a placeholder; obtain the real host and request format from the team's deployment `/docs`. Restart the Terminal server after editing `.env`.

**Without a real model URL**, the app opens but cannot run real inference. For evaluation, also import wind images and their corresponding ground-truth labels as documented in `README.md`.

## 4. Troubleshooting

- **`Python 3.10-3.14 is required`**: install Python 3.12 and restart Terminal.
- **Dependency install failure**: verify internet access, Python version, and macOS compatibility. Remove `.venv` and retry only if the local environment is corrupted.
- **Browser doesn't open**: enter the URL printed in Terminal manually.
- **Port in use**: launcher automatically tries ports 8000 through 8004.
- **`AISTREAM_WIND_URL is not configured`**: the UI works; remote inference needs the real URL.
- **Want public access for judging?** This launcher only serves `127.0.0.1` on your Mac. Deploy the same FastAPI app to your hackathon compute server for a public URL; the Mac launcher alone is **not** a competition submission URL.

## 5. Developer commands (optional)

```bash
cd /path/to/wind_recheck_mvp
source .venv/bin/activate
pytest -q
```

No dataset, credentials, or trained model is bundled. Do not commit `.env`, credentials, or private data to GitHub.
