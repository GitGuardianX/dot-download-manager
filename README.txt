DOT DOWNLOAD MANAGER 0.4.0

A download manager for Windows in the spirit of IDM: parallel byte-range transfers,
pause/resume, queues, scheduling, a speed limit, SHA-256 checks, video downloads from
YouTube and many other sites, and a browser extension that sends downloads to DOT.

-------------------------------------------------------------------------------
INSTALL
-------------------------------------------------------------------------------

Run DOT-Setup-0.4.0.exe. By default it installs for your Windows account only, with no
admin prompt, to %LOCALAPPDATA%\Programs\DOT (the wizard also offers "all users").
Options: a desktop shortcut, and "Start DOT in the tray when I sign in to Windows"
(recommended: browser downloads only reach DOT while it is running).

The installer is not code-signed, so Windows SmartScreen may say "Windows protected your
PC": choose More info > Run anyway.

Installed folder: DOT.exe, _internal\ (Python runtime and DLLs: no Python install needed),
tools\ (yt-dlp, Deno, FFmpeg), browser-extension\, licenses\, and the uninstaller.
Your download list and settings: %LOCALAPPDATA%\DOT. Downloads: your Windows Downloads
folder, in category subfolders (Video, Music, Archives, ...), changeable in Settings.

Uninstall from Windows Settings > Apps > Installed apps > DOT Download Manager. It asks
whether to also delete your download list and settings; downloaded files are never removed.

-------------------------------------------------------------------------------
CONNECT YOUR BROWSER (Brave, Chrome, Edge)
-------------------------------------------------------------------------------

On first start DOT opens "Connect your browser" (also in Settings):
  1. Open your browser's extensions page (DOT has a button for it).
  2. Turn on Developer mode.
  3. If an older DOT extension is listed, click Remove.
  4. Click Load unpacked and choose %LOCALAPPDATA%\Programs\DOT\browser-extension.
  5. Click the DOT icon in the browser toolbar once: the guide turns green.
After DOT updates, its status bar asks you to click Reload on DOT in the extensions page.
(Store installs like IDM's need a Chrome Web Store listing; planned for later.)

If IDM is installed, turn off IDM's integration for that browser: IDM takes matching
downloads before the browser, or DOT, ever sees them.

What it does:
  - Downloads you start in the browser open DOT's Download File Info window, with the
    file size already checked. The browser's copy is cancelled.
  - A "Download with DOT" button appears over a playing video. Direct video files are sent
    as-is; streams use the HLS/DASH playlist; sites such as YouTube are read with yt-dlp,
    and DOT offers every quality (1080p, 720p, ... or audio only).
  - Right-click a link or video: "Download with DOT". The toolbar popup shows the
    connection, media found on the page, and "Download the video on this page".
When DOT cannot take a download (closed, refused by the site, several at once), the
browser simply keeps it.

-------------------------------------------------------------------------------
USING DOT
-------------------------------------------------------------------------------

DOT lives in the notification area (tray). Closing its window keeps it running; click the
tray icon to open it, right-click for Pause all / Start all / Exit. Starting DOT again
(Start Menu) brings the running window back. Settings can turn the tray behavior off.

New download: paste HTTP/HTTPS URLs (one per line), choose a folder, Start or Add paused.
Select rows to pause/resume; double-click opens the folder; right-click copies the URL or
SHA-256, retries, or removes. Settings: default folder, 1-16 connections per file, 1-6
simultaneous downloads, a speed limit (0 = unlimited), and "Update yt-dlp" (sites change
often; update when a video site stops working).
Keys: Ctrl+N new download, Ctrl+V paste links, Space pause/resume, Delete remove.

Limits: DRM-protected video (Netflix, Prime Video, Disney+, Spotify) cannot be downloaded
by DOT, IDM, or any legitimate tool. Private, members-only or age-restricted videos fail
with yt-dlp's reason; DOT does not send your account logins to yt-dlp. Sites that only
work inside the browser (Cloudflare checks, some one-time links) stay in the browser.

-------------------------------------------------------------------------------
SAFETY DESIGN (why the 0.2 integration was rebuilt)
-------------------------------------------------------------------------------

DOT 0.2's integration was withdrawn after a full-system freeze on 26 Nov 2026 (cause not
proven; see analysis\INCIDENT-20261126.txt). Since 0.3: the browser never starts a
program (no native messaging); nothing runs at browser start-up; hand-offs are bounded
(one at a time, 8 automatic per minute, at most 3 open windows); the video button is
event-driven; DOT's bridge accepts only its own extension. yt-dlp and FFmpeg are started
by DOT only, after you confirm a download.

-------------------------------------------------------------------------------
PORTABLE COPY AND SOURCE CODE
-------------------------------------------------------------------------------

Portable copy: copy the installed DOT folder anywhere and put an empty portable.txt beside
DOT.exe; it then keeps its data in .local and downloads beside itself.

DOT is open source under the MIT License:
https://github.com/GitGuardianX/dot-download-manager
Building from source, the tests and the project layout are described in its README.md.
