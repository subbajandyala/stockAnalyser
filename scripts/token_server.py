"""
Kite Token Server — two ways to refresh the daily access token:

  1. ONE-CLICK LOGIN (mobile-friendly):
     Visit http://157.245.154.130:7777 → click "Login with Kite"
     → log in on Zerodha → token saved automatically.

  2. MANUAL PASTE (fallback):
     Copy access_token from Network tab → paste in the form.

Requires api_key + api_secret in ~/.kite_creds.json.
Redirect URL in Kite developer console must be:
    http://157.245.154.130:7777/callback
"""
import json
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

CREDS = pathlib.Path.home() / ".kite_creds.json"
PORT  = 7777
HOST  = "157.245.154.130"

# ── HTML templates ────────────────────────────────────────────────────────────

_CSS = """
body{font-family:sans-serif;background:#0a0e1a;color:#fff;
     display:flex;align-items:center;justify-content:center;
     min-height:100vh;margin:0;padding:16px;box-sizing:border-box}
.box{background:#161b2e;padding:28px 24px;border-radius:14px;
     width:100%;max-width:420px}
h2{margin:0 0 6px;color:#00d4aa;font-size:1.25rem}
.sub{color:#6e7681;font-size:0.8rem;margin-bottom:22px}
.btn{display:block;width:100%;padding:14px;font-size:15px;font-weight:700;
     border:none;border-radius:10px;cursor:pointer;text-align:center;
     text-decoration:none;box-sizing:border-box;margin-bottom:12px}
.btn-kite{background:#387ed1;color:#fff}
.btn-save{background:#00d4aa;color:#000}
.divider{display:flex;align-items:center;gap:10px;margin:16px 0;
         color:#4a5568;font-size:0.78rem}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:#2a2e39}
input{width:100%;padding:12px;border-radius:8px;border:1px solid #333;
      background:#0a0e1a;color:#fff;font-size:14px;box-sizing:border-box}
.msg{margin-top:14px;padding:12px;border-radius:8px;
     text-align:center;font-size:14px}
.ok{background:#1a3a2a;color:#00d4aa}
.err{background:#3a1a1a;color:#ff6b6b}
"""

_HOME = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kite Token</title>
<style>{css}</style>
</head>
<body>
<div class="box">
  <h2>Kite Token</h2>
  <div class="sub">Refresh your daily access token</div>
  {msg}
  <a class="btn btn-kite" href="/login">🔑 Login with Kite</a>
  <div class="divider">or paste manually</div>
  <form method="POST" action="/">
    <input name="token" placeholder="Paste access_token here" autocomplete="off">
    <button class="btn btn-save" type="submit" style="margin-top:12px">Save Token</button>
  </form>
</div>
</body>
</html>"""

_OK = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token Saved</title>
<style>{css}</style>
</head>
<body>
<div class="box" style="text-align:center">
  <div style="font-size:3rem;margin-bottom:12px">✅</div>
  <h2>Token Saved!</h2>
  <div class="sub">Kite access token updated successfully.<br>
  Bots will use the new token on the next run.</div>
  <a class="btn btn-kite" href="/" style="margin-top:20px;background:#1a3a2a;
     color:#00d4aa;border:1px solid #00d4aa33;">← Back</a>
</div>
</body>
</html>"""

_ERR = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Error</title>
<style>{css}</style>
</head>
<body>
<div class="box" style="text-align:center">
  <div style="font-size:3rem;margin-bottom:12px">❌</div>
  <h2 style="color:#f85149">Error</h2>
  <div class="msg err">{error}</div>
  <a class="btn btn-kite" href="/" style="margin-top:20px;background:#2a1a1a;
     color:#ff6b6b;border:1px solid #f8514933;">← Try Again</a>
</div>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_creds() -> dict:
    try:
        return json.loads(CREDS.read_text()) if CREDS.exists() else {}
    except Exception:
        return {}


def _save_token(token: str) -> None:
    creds = _load_creds()
    creds["access_token"] = token
    CREDS.write_text(json.dumps(creds, indent=2))


def _kite_login_url(api_key: str) -> str:
    return f"https://kite.trade/connect/login?{urlencode({'api_key': api_key, 'v': '3'})}"


def _exchange_token(request_token: str) -> str:
    """Exchange request_token → access_token via kiteconnect."""
    from kiteconnect import KiteConnect
    creds = _load_creds()
    api_key    = creds.get("api_key", "")
    api_secret = creds.get("api_secret", "")
    if not api_key or not api_secret:
        raise ValueError(
            "api_key and api_secret must be set in ~/.kite_creds.json"
        )
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    # ── GET routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/":
            self._send(_HOME.format(css=_CSS, msg=""))

        elif path == "/login":
            creds   = _load_creds()
            api_key = creds.get("api_key", "")
            if not api_key:
                self._send(_ERR.format(css=_CSS,
                    error="api_key not found in ~/.kite_creds.json"))
                return
            self._redirect(_kite_login_url(api_key))

        elif path == "/callback":
            params        = parse_qs(parsed.query)
            status        = params.get("status", [""])[0]
            request_token = params.get("request_token", [""])[0]

            if status != "success" or not request_token:
                self._send(_ERR.format(css=_CSS,
                    error=f"Kite login failed or cancelled (status={status})"))
                return

            try:
                access_token = _exchange_token(request_token)
                _save_token(access_token)
                self._send(_OK.format(css=_CSS))
            except Exception as e:
                self._send(_ERR.format(css=_CSS, error=str(e)))

        else:
            self._send("<h1>404</h1>", 404)

    # ── POST / manual paste ───────────────────────────────────────────────────

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data   = parse_qs(self.rfile.read(length).decode())
        token  = data.get("token", [""])[0].strip()
        if not token:
            self._send(_HOME.format(css=_CSS,
                msg='<div class="msg err">Token cannot be empty</div>'))
            return
        try:
            _save_token(token)
            self._send(_OK.format(css=_CSS))
        except Exception as e:
            self._send(_HOME.format(css=_CSS,
                msg=f'<div class="msg err">Error: {e}</div>'))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    HTTPServer.allow_reuse_address = True
    print(f"Token server running at http://{HOST}:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
