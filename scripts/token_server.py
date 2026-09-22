"""
Tiny token update server.
Visit http://157.245.154.130:7777 — enter new token, click Save.
"""
import json, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

CREDS = pathlib.Path.home() / ".kite_creds.json"
PORT  = 7777

HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kite Token Update</title>
<style>
body{{font-family:sans-serif;background:#0a0e1a;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:#161b2e;padding:32px;border-radius:12px;width:90%;max-width:420px}}
h2{{margin:0 0 20px;color:#00d4aa}}
input{{width:100%;padding:12px;border-radius:8px;border:1px solid #333;background:#0a0e1a;color:#fff;font-size:14px;box-sizing:border-box}}
button{{margin-top:16px;width:100%;padding:14px;background:#00d4aa;color:#000;font-weight:bold;font-size:15px;border:none;border-radius:8px;cursor:pointer}}
.msg{{margin-top:14px;padding:10px;border-radius:6px;text-align:center;font-size:14px}}
.ok{{background:#1a3a2a;color:#00d4aa}}.err{{background:#3a1a1a;color:#ff6b6b}}
</style>
</head>
<body>
<div class="box">
  <h2>Kite Token Update</h2>
  {msg}
  <form method="POST">
    <input name="token" placeholder="Paste today's access token" autocomplete="off" autofocus>
    <button type="submit">Save Token</button>
  </form>
</div>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        self._send(HTML.format(msg=""))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data   = parse_qs(self.rfile.read(length).decode())
        token  = data.get("token", [""])[0].strip()
        if token:
            try:
                creds = json.loads(CREDS.read_text()) if CREDS.exists() else {}
                creds["access_token"] = token
                CREDS.write_text(json.dumps(creds, indent=2))
                msg = '<div class="msg ok">Token saved successfully</div>'
            except Exception as e:
                msg = f'<div class="msg err">Error: {e}</div>'
        else:
            msg = '<div class="msg err">Token cannot be empty</div>'
        self._send(HTML.format(msg=msg))

if __name__ == "__main__":
    print(f"Token server running at http://157.245.154.130:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
