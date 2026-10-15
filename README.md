# jd-gdrive-refresh

Keeps JDownloader's Google Drive account alive.

JDownloader's Google Drive plugin is **cookie-based** — it cannot use OAuth tokens,
so its login expires whenever Google rotates or expires your browser session. This
script watches the account through the (free) MyJDownloader API and, when the
cookies go stale, it:

1. opens Safari at `drive.google.com` so you can log in (one click, no cookie
   extensions needed),
2. harvests the fresh Google cookies straight out of Safari's local cookie store,
3. pushes them into JDownloader's `drive.google.com` account and verifies it
   works — or creates the account if it doesn't exist yet.

## One-time setup

1. **MyJDownloader account** (free): sign up at https://my.jdownloader.org, then in
   JD2 go to `Settings -> MyJDownloader` and sign the client in with the same
   account. The script talks to JD through this API.

2. **Full Disk Access for your terminal**: Safari's cookie file is TCC-protected.
   Go to `System Settings -> Privacy & Security -> Full Disk Access` and enable
   your terminal app (`Terminal`, `iTerm`, `Warp`, ...). Then **quit and reopen**
   the terminal.
   (Running `run.command` by double-click opens Terminal — grant it the same way.)

3. **Stay signed in**: log into `drive.google.com` in your browser once and tick
   *"Stay signed in"*. Without it, Google doesn't persist the login cookies to
   disk and the script has nothing to harvest.

## Usage

```bash
cd jd-gdrive-refresh

# 1. Check that cookies are readable (do this first!)
./run.command --test-cookies

# 2. Check the JDownloader account state
./run.command --status

# 3. Fix expired cookies once (opens Safari if needed)
./run.command

# 4. Or keep it running: check every 10 minutes, auto-fix when needed
./run.command --watch
```

Other flags:

| Flag | Meaning |
| --- | --- |
| `--force` | re-push fresh cookies even if the account currently looks valid |
| `--watch [SECONDS]` | keep checking (default 600s) and fix automatically |
| `--login-timeout SECONDS` | how long to wait for you to log in (default 300) |
| `--browser NAME` | browser to read cookies from: `safari` (default), `firefox`, `chrome`, `brave`, `edge`, ... |
| `--no-open` | never open the browser automatically |
| `--manual` | don't touch JD; just put fresh cookies on the clipboard |
| `--paste` | use cookie JSON from the clipboard (e.g. exported by Cookie-Editor from a private window) |
| `--cookie-file PATH` | read cookies from a specific cookie store (e.g. a dedicated Firefox profile's `cookies.sqlite`) |
| `--print-cookies` | also print the cookie JSON to stdout (**sensitive**) |
| `--test-cookies` | only test browser cookie access, no JD connection |

The first interactive run asks for your MyJDownloader credentials and saves them
to `~/.config/jd-gdrive-refresh/config.json` (chmod 600). Delete that file to reset.

## Complying with JDownloader's private-window / profile advice

JDownloader recommends feeding it cookies from a **private window or a separate browser
profile** rather than your everyday session (if you log out of your daily session, JD
gets logged out too). By default this script reads your normal Safari session — zero
manual work, and it self-heals when the session dies. For the stricter setup:

**Option A: private window + Cookie-Editor (manual export, ~4 clicks per refresh)**

Private-window cookies are never written to disk — that's how private browsing works —
so no script can harvest them automatically. But you can export them by hand:

1. Open a private window, log into https://drive.google.com (tick "Stay signed in").
2. Install [Cookie-Editor](https://cookie-editor.com/) and allow it in private windows.
3. On the drive.google.com tab: Cookie-Editor → Export → JSON (copies to clipboard).
4. Run:

   ```bash
   ./run.command --paste
   ```

The script reads the clipboard, pushes the cookies into JD, and verifies them.
If it reports an error, the clipboard didn't contain a valid Cookie-Editor export —
re-do step 3 and try again.

**Option B: a dedicated browser profile (full automation, one-time setup)**

- **Firefox (works today):** create a separate profile (run `firefox -P`), log into
  drive.google.com there once, then point the script at that profile's cookie store:

  ```bash
  ./run.command --browser firefox \
      --cookie-file "$HOME/Library/Application Support/Firefox/Profiles/<profile>/cookies.sqlite"
  ```

  Because that profile is only used for Drive, your regular browsing can't log JD out.
  To make it permanent, add `"browser"` and `"cookie_file"` to `config.json`.
  Works with `--watch` too.
- **Safari profiles:** Safari does isolate per-profile cookies, but it doesn't expose
  a clearly readable per-profile cookie file on disk, so the automatic harvest can't
  be pointed at one (yet).

## Running it in the background (launchd)

To have it always lurking, create `~/Library/LaunchAgents/local.jd-gdrive-refresh.plist`
(replace `YOU` with your username):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.jd-gdrive-refresh</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/Documents/Software Apps/jd-gdrive-refresh/.venv/bin/python</string>
    <string>/Users/YOU/Documents/Software Apps/jd-gdrive-refresh/jd_gdrive_refresh.py</string>
    <string>--watch</string>
    <string>900</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/jd-gdrive-refresh.log</string>
  <key>StandardErrorPath</key><string>/tmp/jd-gdrive-refresh.log</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/local.jd-gdrive-refresh.plist
```

Important: a launchd process does **not** inherit Terminal's permissions. Add
`.venv/bin/python` (drag it in with Cmd+Shift+G: `/Users/YOU/Documents/Software Apps/jd-gdrive-refresh/.venv/bin/python`)
to `Full Disk Access` as well, or the cookie harvest will be blocked.
Logs: `tail -f /tmp/jd-gdrive-refresh.log`.

## Troubleshooting

- **"macOS is blocking access to Safari's cookie file"** — grant Full Disk Access
  to your terminal (step 2 above), then quit/reopen it.
- **"No Google login cookies found"** — log into `drive.google.com` in Safari with
  *Stay signed in* enabled. If you only ever use a private window or logged out,
  there is nothing on disk to read.
- **JD still reports the account invalid after pushing** — the script puts the
  cookies on your clipboard and tells you where to paste them manually
  (`Settings -> Account Manager -> drive.google.com -> Edit -> Cookies field`).
- **Cookies expire again within minutes** — that's Google rotating the session
  your everyday Safari also uses. Re-run the script (or let `--watch` do it), or
  isolate it: create a second browser profile, stay logged into Drive only there,
  and use `--browser firefox` pointing at that profile's cookie store. If it
  keeps annoying you, consider `rclone` instead — it uses a real OAuth token that
  refreshes itself, at the cost of not being integrated into JD's queue.
- **"No JDownloader device found"** — JD2 must be running and signed into
  MyJDownloader (setup step 1).

## Security notes

- The exported cookies are your **entire Google session** (Gmail included). The
  script only sends them to your local JDownloader; `--print-cookies` and the
  clipboard fallback put them in places (terminal scrollback, clipboard) you
  should treat as sensitive.
- `~/.config/jd-gdrive-refresh/config.json` stores your MyJDownloader password
  (file mode 600). It is not your Google password — that one never leaves Safari.
- If you ever want to revoke everything: sign out of Google in the browser you
  let the script read (this also kills JD's session — that's expected), and delete
  the config file.
