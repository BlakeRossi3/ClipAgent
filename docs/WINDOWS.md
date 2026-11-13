# ClipAgent on Windows

## Install

1. Download `ClipAgent-Setup.bat`: open
   <https://github.com/BlakeRossi3/ClipAgent/blob/claude/clever-allen-zi43bi/windows/ClipAgent-Setup.bat>
   and press **Download raw file**. (Direct link:
   `https://raw.githubusercontent.com/BlakeRossi3/ClipAgent/refs/heads/claude/clever-allen-zi43bi/windows/ClipAgent-Setup.bat`.
   If your browser shows text instead of downloading it, right-click → *Save as*.)
2. Double-click it. On "Windows protected your PC", click **More info → Run anyway**.
3. When the browser opens, follow the setup pages.

## What the installer does

`ClipAgent-Setup.bat` downloads `windows/setup.ps1` from GitHub and runs it. It is safe to run
again at any time (it repairs and updates rather than reinstalling).

1. Installs what is missing with **winget** (built into Windows 10/11): Python 3.13 (for your
   user only), Git, and FFmpeg (the "full" build by Gyan, which has the caption renderer).
2. Downloads ClipAgent into `%LOCALAPPDATA%\ClipAgent\app` (or updates it there).
3. Creates a private Python environment in `app\.venv` and installs the packages.
4. Creates your data folder and database, then the **ClipAgent** shortcuts (Desktop and Start menu).
5. Starts ClipAgent and opens `http://127.0.0.1:8000/` in your browser.

The shortcut runs `windows\ClipAgent.ps1`: if ClipAgent is already running it just opens the
browser; otherwise it quietly updates the app, starts it in a minimized window titled
"ClipAgent - close this window to stop ClipAgent", waits until it answers and opens the browser.

## Where things live

| What | Where |
|---|---|
| The app (replaced by updates) | `%LOCALAPPDATA%\ClipAgent\app` |
| Your settings (`config.env`, including keys) | `%LOCALAPPDATA%\ClipAgent\data\config.env` |
| Database, YouTube sign-in, videos | `%LOCALAPPDATA%\ClipAgent\data` |
| Memory vault (open it in Obsidian: *Open folder as vault*) | `%LOCALAPPDATA%\ClipAgent\data\vault` |
| Logs | `%LOCALAPPDATA%\ClipAgent\data\logs` |

`%LOCALAPPDATA%` is usually `C:\Users\<you>\AppData\Local`. Settings → *Open data folder* opens it.

ClipAgent only listens on `127.0.0.1` (this laptop). Opening it from your phone later needs a
public HTTPS address (see `deploy/README.md`) and the phone password made under
Settings → Power & privacy.

## Day to day

* **Start**: the ClipAgent shortcut. Optional: Settings → Power & privacy → *Start ClipAgent when
  Windows starts*.
* **Stop**: Settings → *Stop*, or close the minimized ClipAgent window.
* **Update**: happens by itself each time the shortcut starts ClipAgent; or Settings →
  *Check for updates* (updates and restarts).
* **Change anything**: Settings (the same forms as the first-run setup).

## Uninstall

1. Settings → *Stop*. If you ticked *Start when Windows starts*, untick it first (or delete
   `ClipAgent.lnk` from `shell:startup`).
2. Back up `%LOCALAPPDATA%\ClipAgent\data\vault` if you want to keep your memory.
3. Delete the folder `%LOCALAPPDATA%\ClipAgent` and the ClipAgent shortcuts (Desktop, Start menu).
4. Optional: remove Python, Git and FFmpeg under Settings → Apps (or `winget uninstall Gyan.FFmpeg`).

## Troubleshooting

* **"Windows protected your PC"**: the .bat is not code-signed. Click *More info* → *Run anyway*.
* **"winget (App Installer) is missing"**: open the Microsoft Store, install/update **App
  Installer**, then run the .bat again. Or install Python 3.13, Git and FFmpeg yourself and rerun.
* **Antivirus blocks or deletes the .bat / a script**: allow it, or add an exclusion for
  `%LOCALAPPDATA%\ClipAgent`. Some antivirus tools briefly lock files while scanning; just retry.
* **"ClipAgent did not start" / port already in use**: another program is using port 8000.
  Close it, or start ClipAgent on another port by running `windows\ClipAgent.bat -Port 8010`
  from the app folder. The reason is in `data\logs\clipagent-<date>.log`.
* **GPU transcription**: Settings → Power & privacy → *Use my NVIDIA GPU*. It needs the NVIDIA
  driver plus the CUDA 12 cuBLAS/cuDNN libraries; if they are missing ClipAgent logs a warning and
  switches back to the processor by itself, so nothing breaks.
* **Captions use a different font**: Windows has no Montserrat; captions fall back to Arial Black.
  Put `Montserrat-ExtraBold.ttf` (Google Fonts) into `app\assets\fonts\` or install it for your
  user, then restart.
* **Setup log**: `data\logs\setup-<date>.log`; launcher log: `data\logs\launcher-<date>.log`.
