# Installing ClipForge on Windows (no typing required)

## Download → Install → Open

1. **Download**: on the GitHub page press the green **Code** button → **Download ZIP**, then right-click the
   ZIP → **Extract All…** and pick a folder you can find again, for example `C:\Users\<you>\ClipForge`.
   (Avoid `C:\Windows` and `Program Files`; the app keeps its videos next to itself.)
2. **Install**: open that folder and double-click **`Install ClipForge.bat`**. A window shows progress:
   - it looks for Python 3.11 or newer; if it is missing it offers to install Python 3.12 for your user only
     (no administrator password). After Python installs, close the window and double-click the installer again;
   - it creates a private folder `.venv` inside ClipForge, downloads the components (a few minutes the first time),
     runs an environment check, and puts a **ClipForge** shortcut on your Desktop and in the Start Menu;
   - at the end it asks whether to open ClipForge.
3. **Open**: double-click the **ClipForge** shortcut (or `Launch ClipForge.bat`). A small black window stays open
   while the app runs and your browser opens `http://127.0.0.1:8765`. Close that window to stop ClipForge.

Repeat the install any time to update: your videos, clips, sign-ins and settings are kept.

## First use

- Dashboard → paste a YouTube link (or a video file path) → **Add to queue** → **Run queue**.
- Review → watch the clips, approve the ones you like.
- Publish → connect YouTube or TikTok, or use **Manual…** to upload by hand with the caption copied for you.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "Python 3.11+ was not found" and no winget offer | Install Python from https://www.python.org/downloads/windows/ and tick **Add python.exe to PATH**, then run the installer again. |
| "installation failed" | Check the internet connection, then run the installer again. Details are in the setup log (below). |
| The environment check reports a failure | Open ClipForge → Settings → **Environment check**; the failing row says what is missing. ffmpeg is downloaded automatically, so this is rare. |
| The browser does not open | Open `http://127.0.0.1:8765` yourself while the black window is open. |
| "port is busy" | Another ClipForge window is already running; use that one or close it first. |
| Windows SmartScreen warns about the `.bat` file | It is a plain text script you can open in Notepad; choose **More info → Run anyway**. Nothing needs administrator rights. |
| Clip previews do not play in the browser | Use Chrome, Edge or Firefox; the card offers a download link otherwise. |

**Setup log**: `%LOCALAPPDATA%\ClipForge\setup.log` (paste `%LOCALAPPDATA%\ClipForge` into the Explorer address
bar). It contains program output only; no passwords or tokens are written there.
**App log**: `logs\clipforge.log` inside the ClipForge folder.

## Uninstall

Double-click **`Uninstall ClipForge.bat`**. It removes the shortcuts and the private `.venv` folder, then asks
separately whether to delete your data (`workspace\`, containing downloads, clips and sign-in tokens, and
`clipforge.yaml`). Answer **N** to keep them. Afterwards you can delete the ClipForge folder itself.

## What was verified

The scripts were written and reviewed for Windows PowerShell 5.1 and later. A first install, a repeat install and
the shortcut launch on a clean Windows machine have **not** been run by the author of this version: no Windows
machine was available. If something fails, send the setup log.
