"""Production bootstrap for Amplify.

Wraps app.py's import + main entry in try/except so any startup failure
(import error, missing config, port bind failure, etc.) is captured to
both stdout AND /tmp/amplify_startup.log so the user can retrieve it
via deployment logs or the /__startup_log HTTP route.
"""
import faulthandler
import os
import socket
import sys
import time
import traceback

LOG_PATH = os.environ.get("AMPLIFY_STARTUP_LOG", "/tmp/amplify_startup.log")

try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
except Exception:
    pass


def _log(msg: str) -> None:
    line = f"[bootstrap {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


faulthandler.enable()
_log("=== Amplify production bootstrap starting ===")
_log(f"python={sys.version.split()[0]} cwd={os.getcwd()} pid={os.getpid()}")
_log(f"PORT={os.environ.get('PORT', '<unset>')} HOST={socket.gethostname()}")
_log(f"sys.path[0..3]={sys.path[:4]}")

_keys_present = sorted(
    k for k in os.environ.keys()
    if any(t in k for t in ("API_KEY", "TOKEN", "SECRET"))
)
_log(f"secret-like env keys present (names only): {_keys_present}")

try:
    _log("importing app module...")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app as app_module  # noqa: F401
    _log("app module imported OK")
except BaseException:
    _log("FATAL: import app failed")
    tb = traceback.format_exc()
    for line in tb.splitlines():
        _log(line)
    sys.stdout.flush()
    sys.stderr.flush()
    raise

try:
    # Kick off the background attachment-backfill sweep (Task #104). The
    # starter is idempotent and self-gates on S3 being enabled, so this
    # is safe even when AMPLIFY_IMAGE_STORAGE_BACKEND is still ``local``.
    app_module._start_background_attachment_backfill()
    _log("attachment-backfill sweep starter invoked")
except BaseException:
    _log("WARN: attachment-backfill sweep starter failed (continuing)")
    for line in traceback.format_exc().splitlines():
        _log(line)

try:
    port = int(os.environ.get("PORT", 5000))
    _log(f"starting waitress on 0.0.0.0:{port}")
    from waitress import serve
    serve(
        app_module.app,
        host="0.0.0.0",
        port=port,
        _quiet=False,
        channel_timeout=300,
        recv_bytes=65536,
        threads=8,
    )
except BaseException:
    _log("FATAL: server failed to start / crashed")
    tb = traceback.format_exc()
    for line in tb.splitlines():
        _log(line)
    sys.stdout.flush()
    sys.stderr.flush()
    raise
