"""
Kite credentials helper.
Reads from ~/.kite_creds.json (never committed).
Falls back to env vars KITE_API_KEY / KITE_ACCESS_TOKEN.
"""
import json, os, pathlib

_CREDS_FILE = pathlib.Path.home() / ".kite_creds.json"


def load() -> tuple[str, str]:
    """Return (api_key, access_token). Raises if either missing."""
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")

    if _CREDS_FILE.exists():
        try:
            d = json.loads(_CREDS_FILE.read_text())
            api_key = api_key or d.get("api_key", "")
            token   = token   or d.get("access_token", "")
        except Exception:
            pass

    if not api_key or not token:
        raise RuntimeError(
            f"Kite credentials missing.\n"
            f"Save them to {_CREDS_FILE}:\n"
            f'  {{"api_key": "...", "access_token": "..."}}\n'
            f"Or set env vars KITE_API_KEY and KITE_ACCESS_TOKEN."
        )
    return api_key, token


def save(api_key: str, access_token: str) -> None:
    _CREDS_FILE.write_text(json.dumps({"api_key": api_key, "access_token": access_token}, indent=2))
    _CREDS_FILE.chmod(0o600)
    print(f"Saved to {_CREDS_FILE}")
