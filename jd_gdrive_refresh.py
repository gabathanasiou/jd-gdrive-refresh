#!/usr/bin/env python3
"""jd_gdrive_refresh.py — Auto-refresh JDownloader's Google Drive login cookies.

JDownloader's Google Drive plugin is cookie-based: it does not support OAuth
tokens, so its login dies whenever Google expires or rotates your browser
session. This script:

  1. Connects to JDownloader via the (free) MyJDownloader API.
  2. Checks the drive.google.com account status.
  3. If the cookies are expired: opens Safari at drive.google.com so you can
     log in, harvests the fresh Google cookies from Safari's cookie store,
     and pushes them straight into JDownloader.
  4. Verifies the account is valid again.

Usage:
  ./jd_gdrive_refresh.py --status         show the drive.google.com account state
  ./jd_gdrive_refresh.py                  fix expired cookies once (interactive)
  ./jd_gdrive_refresh.py --force          re-push fresh cookies even if valid
  ./jd_gdrive_refresh.py --watch 900      keep checking every 900s; fix automatically
  ./jd_gdrive_refresh.py --test-cookies   check that browser cookies can be read
  ./jd_gdrive_refresh.py --manual         only put fresh cookies on the clipboard
  ./jd_gdrive_refresh.py --paste          push cookie JSON from the clipboard into JD

Requires (see README.md):
  - a free MyJDownloader account, signed in inside JD2 (Settings -> MyJDownloader)
  - Full Disk Access for your terminal app (to read Safari's cookies)
  - "Stay signed in" enabled on the Google login in Safari
"""

import argparse
import getpass
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "jd-gdrive-refresh" / "config.json"

# Safari stores cookies here (first match wins). The first path is the modern
# sandboxed location; the second is the legacy location.
SAFARI_COOKIE_FILES = [
    "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    "~/Library/Cookies/Cookies.binarycookies",
]

DRIVE_URL = "https://drive.google.com/drive/my-drive"
DRIVE_HOSTNAMES = ("drive.google.com", "docs.google.com")

# Without at least one of these, we don't have a usable Google login session.
AUTH_COOKIE_NAMES = ("SID", "__Secure-1PSID", "__Secure-3PSID")

ACCOUNT_QUERY = [{
    "startAt": 0,
    "maxResults": -1,
    "userName": True,
    "validUntil": True,
    "trafficLeft": True,
    "trafficMax": True,
    "enabled": True,
    "valid": True,
    "error": True,
    "UUIDList": [],
}]

FDA_HELP = (
    "macOS is blocking access to Safari's cookie file.\n"
    "Fix: System Settings -> Privacy & Security -> Full Disk Access -> enable your\n"
    "terminal app (Terminal / iTerm / Warp), then QUIT and reopen the terminal\n"
    "(and re-run this script)."
)


class CookieAccessError(RuntimeError):
    """Raised when browser cookies cannot be read (FDA, missing file, ...)."""


class JDError(RuntimeError):
    """Raised for JDownloader/MyJDownloader connection problems (retryable in watch mode)."""


def log(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    log("ERROR: " + msg)
    sys.exit(code)


def prompt(text):
    try:
        return input(text).strip()
    except EOFError:
        die("Aborted: no input available. Run this script from an interactive terminal.")


# ---------------------------------------------------------------- config ----

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        die("Could not read %s: %s" % (CONFIG_PATH, exc))


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CONFIG_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    log("(saved settings to %s)" % CONFIG_PATH)


def ensure_config(args):
    cfg = load_config()
    if args.browser:
        cfg["browser"] = args.browser
    if args.device:
        cfg["myjd_device"] = args.device
    if not cfg.get("browser"):
        cfg["browser"] = "safari"
    return cfg


def ensure_myjd_credentials(cfg):
    if cfg.get("myjd_email") and cfg.get("myjd_password"):
        return
    if not sys.stdin.isatty():
        die(
            "No MyJDownloader credentials configured yet. Run this script once from an\n"
            "interactive terminal (e.g. double-click run.command) to set them up."
        )
    log("First run: I need your MyJDownloader account (free, https://my.jdownloader.org).")
    log("JDownloader must also be signed in to it: JD2 -> Settings -> MyJDownloader.\n")
    cfg["myjd_email"] = prompt("MyJDownloader email: ")
    cfg["myjd_password"] = getpass.getpass("MyJDownloader password: ")
    save_config(cfg)


# ------------------------------------------------------ JDownloader side ----

def jd_connect(cfg):
    try:
        import myjdapi
    except ImportError:
        die("Python package 'myjdapi' is missing. Run: .venv/bin/pip install -r requirements.txt")

    ensure_myjd_credentials(cfg)

    jd = myjdapi.Myjdapi()
    try:
        jd.connect(cfg["myjd_email"], cfg["myjd_password"])
    except Exception as exc:
        raise JDError("Could not log in to MyJDownloader (%s).\n"
                      "Check the email/password (delete %s and re-run to re-enter)." % (exc, CONFIG_PATH))

    devices = jd.list_devices() or []
    if not devices:
        jd.disconnect()
        raise JDError("No JDownloader device found. Is JD2 running? In JD2 -> Settings ->\n"
                      "MyJDownloader: make sure the extension is enabled and signed in with the\n"
                      "same MyJDownloader account.")

    wanted = cfg.get("myjd_device")
    device = None
    if wanted:
        for dev in devices:
            if wanted in (dev.get("name"), dev.get("id")):
                device = jd.get_device(device_id=dev["id"])
                break
        if device is None:
            log("Warning: configured device '%s' not found; available: %s"
                % (wanted, ", ".join(d.get("name", "?") for d in devices)))

    if device is None and len(devices) == 1:
        device = jd.get_device(device_id=devices[0]["id"])

    if device is None:
        log("Multiple JDownloader devices found:")
        for i, dev in enumerate(devices, 1):
            log("  %d. %s (%s)" % (i, dev.get("name"), dev.get("id")))
        if not sys.stdin.isatty():
            die("Pick one with --device NAME.")
        choice = prompt("Which device? [1-%d]: " % len(devices))
        try:
            dev = devices[int(choice) - 1]
        except Exception:
            die("Invalid choice.")
        device = jd.get_device(device_id=dev["id"])
        cfg["myjd_device"] = dev.get("name")
        save_config(cfg)

    return jd, device


def list_accounts(device):
    try:
        accounts = device.accounts.list_accounts(ACCOUNT_QUERY)
    except Exception as exc:
        raise JDError("Could not list JDownloader accounts: %s" % exc)
    return accounts if isinstance(accounts, list) else []


def drive_accounts(accounts):
    return [a for a in accounts if a.get("hostname") in DRIVE_HOSTNAMES]


def account_problem(acc):
    """Return None if the account is healthy, else a human-readable problem."""
    if not acc.get("enabled", True):
        return "account is disabled in JDownloader"
    if acc.get("error"):
        return str(acc["error"])
    if not acc.get("valid"):
        return "login cookies expired / account invalid"
    return None


def account_name(acc):
    return "%s [%s]" % (acc.get("hostname"), acc.get("userName") or "?")


def format_valid_until(acc):
    ts = acc.get("validUntil") or 0
    try:
        ts = int(ts)
        if ts > 100000000000:  # milliseconds
            ts //= 1000
        if ts > 0:
            return " (valid until %s)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        pass
    return ""


# ---------------------------------------------------------- Safari/other ----

def resolve_safari_cookie_file():
    for raw in SAFARI_COOKIE_FILES:
        path = os.path.expanduser(raw)
        try:
            if os.stat(path).st_size > 0:
                return path
        except PermissionError as exc:
            raise CookieAccessError(FDA_HELP) from exc
        except OSError:
            continue
    raise CookieAccessError(
        "Safari's cookie file was not found. Open Safari, visit drive.google.com\n"
        "once and log in (with 'Stay signed in'), then try again.\n\n" + FDA_HELP
    )


def to_cookie_editor(cookie):
    """Map an http.cookiejar.Cookie to Cookie-Editor's JSON schema (what JD expects)."""
    rest = getattr(cookie, "_rest", {}) or {}
    http_only = ("HTTPOnly" in rest) or bool(rest.get("HttpOnly"))
    same_site = rest.get("SameSite") or rest.get("SameSite")
    if isinstance(same_site, str):
        same_site = same_site.lower()
        if same_site not in ("no_restriction", "lax", "strict"):
            same_site = "unspecified"
    else:
        same_site = "unspecified"
    return {
        "domain": cookie.domain,
        "expirationDate": cookie.expires,
        "hostOnly": not (cookie.domain or "").startswith("."),
        "httpOnly": http_only,
        "name": cookie.name,
        "path": cookie.path or "/",
        "sameSite": same_site,
        "secure": bool(cookie.secure),
        "session": cookie.expires is None,
        "storeId": "0",
        "value": cookie.value,
    }


def harvest_google_cookies(browser, cookie_file=None):
    """Read all google.com cookies from the given browser, Cookie-Editor format.

    cookie_file lets you point at a specific cookie store, e.g. a dedicated
    Firefox profile's cookies.sqlite (overrides the default location).
    """
    try:
        import browser_cookie3
    except ImportError:
        die("Python package 'browser-cookie3' is missing. Run: .venv/bin/pip install -r requirements.txt")

    funcs = {
        "safari": browser_cookie3.safari,
        "firefox": browser_cookie3.firefox,
        "chrome": browser_cookie3.chrome,
        "chromium": browser_cookie3.chromium,
        "brave": browser_cookie3.brave,
        "edge": browser_cookie3.edge,
        "opera": browser_cookie3.opera,
        "vivaldi": browser_cookie3.vivaldi,
    }
    name = (browser or "safari").lower()
    if name not in funcs:
        die("Unsupported browser '%s'. Supported: %s." % (browser, ", ".join(sorted(funcs))))

    kwargs = {}
    if cookie_file:
        kwargs["cookie_file"] = os.path.expanduser(cookie_file)
    elif name == "safari":
        kwargs["cookie_file"] = resolve_safari_cookie_file()

    try:
        jar = funcs[name](domain_name="google.com", **kwargs)
    except CookieAccessError:
        raise
    except Exception as exc:
        if name == "safari":
            raise CookieAccessError(
                "Could not read Safari's cookies (%s).\n\n%s" % (exc, FDA_HELP)
            )
        raise CookieAccessError("Could not read cookies from %s: %s" % (name, exc))

    cookies = [to_cookie_editor(c) for c in jar if (c.domain or "").endswith("google.com")]
    return cookies


def read_clipboard_cookies():
    """Read Cookie-Editor style cookie JSON (list of cookie objects) from the clipboard."""
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, check=True)
    except Exception as exc:
        die("Could not read the clipboard: %s" % exc)
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        die("The clipboard is empty. In your private window: Cookie-Editor -> Export as JSON,\n"
            "then re-run with --paste.")
    try:
        data = json.loads(text)
    except Exception:
        die("The clipboard does not contain JSON. Export the cookies with Cookie-Editor\n"
            "(export format: JSON), then re-run with --paste.")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        die("Unexpected clipboard JSON (expected a list of cookies, as exported by Cookie-Editor).")
    cookies = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        domain = str(item.get("domain") or "")
        if not name or "value" not in item:
            continue
        if not domain.endswith("google.com"):
            continue
        cookies.append(item)
    if not cookies:
        die("No google.com cookies found in the clipboard JSON. Make sure you export from\n"
            "the drive.google.com tab inside your private window.")
    if not has_login_cookies(cookies):
        die("The clipboard cookies do not include a Google login (SID / __Secure-*PSID).\n"
            "Are you logged into drive.google.com in that window?")
    return cookies


def has_login_cookies(cookies):
    names = set(c["name"] for c in cookies)
    return any(n in names for n in AUTH_COOKIE_NAMES)


# ------------------------------------------------------------ login wait ----

def open_drive_page():
    try:
        subprocess.run(["open", "-a", "Safari", DRIVE_URL], check=True, capture_output=True)
    except Exception as exc:
        log("Could not open Safari automatically (%s). Please open %s yourself." % (exc, DRIVE_URL))


def stdin_enter_pressed():
    if not sys.stdin.isatty():
        return False
    try:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
    except Exception:
        return False
    if readable:
        sys.stdin.readline()
        return True
    return False


def wait_for_login(browser, timeout, cookie_file=None):
    log("Waiting for a logged-in Google session in your browser...")
    log("(Log in if needed -- this continues automatically once cookies appear,")
    log(" or press Enter here to try right now.)")
    deadline = time.time() + max(30, timeout)
    last_note = time.time()
    while True:
        if stdin_enter_pressed():
            return True
        try:
            if has_login_cookies(harvest_google_cookies(browser, cookie_file)):
                return True
        except CookieAccessError:
            raise
        except Exception:
            pass  # file mid-write or not ready yet
        if time.time() >= deadline:
            return False
        if time.time() - last_note >= 30:
            log("  ... still waiting")
            last_note = time.time()
        time.sleep(2)


# ----------------------------------------------------------------- push ----

def google_account_username(cfg, account=None):
    if account and account.get("userName"):
        return account["userName"]
    if not cfg.get("google_email"):
        if sys.stdin.isatty():
            cfg["google_email"] = prompt("Google email to label the JD account (Enter for 'google.drive'): ") or "google.drive"
            save_config(cfg)
        else:
            cfg["google_email"] = "google.drive"
    return cfg["google_email"]


def push_cookies(device, account, cookie_text, cfg):
    """Push cookies into JD. Returns (uuid or None, username)."""
    if account is not None:
        uuid = account.get("uuid")
        username = google_account_username(cfg, account)
        if not account.get("enabled", True):
            log("Account is disabled in JDownloader - re-enabling it.")
            try:
                device.accounts.enable_accounts([uuid])
            except Exception as exc:
                log("  (could not enable the account: %s)" % exc)
        log("Updating cookies for %s in JDownloader..." % account_name(account))
        device.accounts.set_user_name_and_password(uuid, username, cookie_text)
        return uuid, username

    username = google_account_username(cfg)
    log("No Google Drive account in JDownloader yet - adding one as [%s]..." % username)
    device.accounts.add_account("drive.google.com", username, cookie_text)
    return None, username


def wait_until_valid(device, uuid=None, hostname=None, timeout=60):
    """Poll JD until the (updated/created) account reports valid. Returns (account, problem)."""
    deadline = time.time() + timeout
    last_problem = None
    while time.time() < deadline:
        for acc in drive_accounts(list_accounts(device)):
            if uuid is not None and acc.get("uuid") != uuid:
                continue
            if uuid is None and hostname is not None and acc.get("hostname") != hostname:
                continue
            problem = account_problem(acc)
            if problem is None:
                return acc, None
            last_problem = problem
        time.sleep(3)
    return None, last_problem


def manual_fallback(cookie_text, reason):
    log("")
    log("Falling back to manual update. %s" % reason)
    try:
        subprocess.run(["pbcopy"], input=cookie_text.encode("utf-8"), check=True)
        log("The fresh cookies are now on your clipboard.")
    except Exception:
        log("Could not copy to the clipboard - use --print-cookies to display them.")
    log("In JDownloader: Settings -> Account Manager -> drive.google.com -> Edit ->")
    log("paste into the 'Cookies' field -> Save.")
    log("(Headless/Docker: my.jdownloader.org -> your device -> Settings -> Account")
    log(" Manager; cookies go into the 'password' field.)")


# ---------------------------------------------------------------- modes ----

def cmd_status(device):
    accounts = list_accounts(device)
    drive = drive_accounts(accounts)
    if not drive:
        found = ", ".join(a.get("hostname", "?") for a in accounts) or "none"
        log("No drive.google.com account in JDownloader (accounts present: %s)." % found)
        return 1
    ok = True
    for acc in drive:
        problem = account_problem(acc)
        if problem:
            ok = False
        log("%s %s%s" % ("OK " if problem is None else "BAD",
                         account_name(acc),
                         format_valid_until(acc)))
        if problem:
            log("    -> %s" % problem)
    return 0 if ok else 1


def cmd_test_cookies(cfg, args):
    log("Testing cookie access (browser: %s)..." % cfg["browser"])
    cookies = harvest_google_cookies(cfg["browser"], getattr(args, "cookie_file", None))
    log("Read %d cookies for google.com." % len(cookies))
    if has_login_cookies(cookies):
        names = ", ".join(sorted(set(c["name"] for c in cookies)))
        log("OK: Google login cookies found (SID / __Secure-*PSID present).")
        log("Cookie names: %s" % names)
        return 0
    log("No Google login cookies found. Are you logged into drive.google.com in the")
    log("browser, with 'Stay signed in' enabled?")
    return 1


def refresh_flow(jd, device, cfg, args):
    """Ensure the drive account is valid. Returns True when the account is good."""
    accounts = drive_accounts(list_accounts(device))
    cookie_file = getattr(args, "cookie_file", None)

    if args.paste:
        if accounts:
            targets = accounts
            for acc in targets:
                log("Updating %s from clipboard cookies..." % account_name(acc))
        else:
            log("No Google Drive account found in JDownloader - I will create one.")
            targets = [None]
        cookies = read_clipboard_cookies()
    else:
        if accounts:
            targets = accounts if args.force else [a for a in accounts if account_problem(a)]
            if not targets:
                for acc in accounts:
                    log("OK: %s is valid%s" % (account_name(acc), format_valid_until(acc)))
                return True
            for acc in targets:
                log("EXPIRED: %s -> %s" % (account_name(acc), account_problem(acc)))
        else:
            log("No Google Drive account found in JDownloader - I will create one.")
            targets = [None]

        if not args.no_open:
            log("Opening %s in Safari..." % DRIVE_URL)
            open_drive_page()

        cookies = harvest_google_cookies(cfg["browser"], cookie_file)
        if not has_login_cookies(cookies) and not args.no_open:
            if wait_for_login(cfg["browser"], args.login_timeout, cookie_file):
                cookies = harvest_google_cookies(cfg["browser"], cookie_file)

        if not has_login_cookies(cookies):
            log("No logged-in Google session found in the browser. Make sure you are")
            log("logged into drive.google.com (with 'Stay signed in') and try again.")
            return False

    cookie_text = json.dumps(cookies, separators=(",", ":"))
    if args.print_cookies:
        log(cookie_text)

    if args.manual:
        manual_fallback(cookie_text, "Requested --manual.")
        return False

    for target in targets:
        try:
            uuid, username = push_cookies(device, target, cookie_text, cfg)
        except Exception as exc:
            manual_fallback(cookie_text, "Could not update JDownloader automatically (%s)." % exc)
            return False

        if uuid is not None:
            try:
                device.accounts.refresh_accounts([uuid])
            except Exception:
                pass

        acc, problem = wait_until_valid(
            device, uuid=uuid, hostname=(None if uuid is not None else "drive.google.com")
        )
        if acc is not None:
            log("SUCCESS: %s is valid again (%d cookies pushed)%s"
                % (account_name(acc), len(cookies), format_valid_until(acc)))
        else:
            log("JDownloader still reports the account as invalid: %s" % (problem or "unknown error"))
            manual_fallback(cookie_text, "JD did not accept the cookies via the API.")
            return False
    return True


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Auto-refresh JDownloader's Google Drive login cookies via Safari.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --status          show the drive.google.com account state\n"
            "  %(prog)s                   fix expired cookies once (interactive)\n"
            "  %(prog)s --watch 900       keep checking; fix automatically when needed\n"
            "  %(prog)s --test-cookies    verify browser cookies can be read\n"
            "  %(prog)s --paste           push Cookie-Editor JSON from the clipboard\n"
        ),
    )
    p.add_argument("--status", action="store_true", help="show the account state and exit")
    p.add_argument("--force", action="store_true", help="re-push cookies even if the account looks valid")
    p.add_argument("--watch", nargs="?", type=int, const=600, default=None, metavar="SECONDS",
                   help="keep running; check every N seconds (default 600) and fix automatically when needed")
    p.add_argument("--login-timeout", type=int, default=300, metavar="SECONDS",
                   help="how long to wait for you to log in (default 300)")
    p.add_argument("--browser", help="browser to read cookies from (default: safari)")
    p.add_argument("--device", help="MyJDownloader device name (if you have several)")
    p.add_argument("--no-open", action="store_true", help="do not open the Google Drive page automatically")
    p.add_argument("--print-cookies", action="store_true",
                   help="also print the cookie JSON to stdout (sensitive: full Google session!)")
    p.add_argument("--manual", action="store_true",
                   help="do not touch JD automatically; just put fresh cookies on the clipboard")
    p.add_argument("--paste", action="store_true",
                   help="use cookie JSON from the clipboard (e.g. exported by Cookie-Editor in a "
                        "private window) instead of reading the browser")
    p.add_argument("--cookie-file", metavar="PATH",
                   help="read cookies from this specific cookie store, e.g. a dedicated Firefox "
                        "profile's cookies.sqlite")
    p.add_argument("--test-cookies", action="store_true",
                   help="only test that browser cookies can be read; no JDownloader connection")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.paste and args.watch is not None:
        die("--paste is a one-off update; don't combine it with --watch.")
    cfg = ensure_config(args)

    if args.test_cookies:
        return cmd_test_cookies(cfg, args)

    if args.watch is not None:
        log("Watching every %ds -- press Ctrl-C to stop." % max(30, args.watch))
        while True:
            jd = None
            try:
                jd, device = jd_connect(cfg)
                refresh_flow(jd, device, cfg, args)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log("WARNING: %s" % exc)
            finally:
                if jd is not None:
                    try:
                        jd.disconnect()
                    except Exception:
                        pass
            time.sleep(max(30, args.watch))

    jd, device = jd_connect(cfg)
    try:
        if args.status:
            return cmd_status(device)
        return 0 if refresh_flow(jd, device, cfg, args) else 1
    finally:
        try:
            jd.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CookieAccessError as exc:
        log("")
        log("ERROR: %s" % exc)
        sys.exit(1)
    except JDError as exc:
        log("")
        log("ERROR: %s" % exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)
