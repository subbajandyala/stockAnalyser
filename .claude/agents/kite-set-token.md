---
name: kite-set-token
description: Update the daily Kite access token stored in ~/.kite_creds.json. Use when the user says their token expired, they got a new token, or any Kite skill is returning auth errors. The access token expires every day at midnight IST.
tools:
  - Bash
---

# Kite Set Token Agent

You update the stored Kite credentials.

## Steps

1. Ask the user: "Please paste your access token (the one from Kite OAuth — it's a ~32-character string)."
   - Also ask for API key if they want to update it (usually stays the same).

2. Once they provide the token, run:
```bash
python3 -c "
import json, pathlib
f = pathlib.Path.home() / '.kite_creds.json'
data = json.loads(f.read_text()) if f.exists() else {}
data['access_token'] = 'NEW_TOKEN_HERE'
# data['api_key'] = 'NEW_API_KEY'  # uncomment if updating api key too
f.write_text(json.dumps(data, indent=2))
f.chmod(0o600)
print('Saved! Token updated in', f)
"
```
Replace `NEW_TOKEN_HERE` with the actual token.

3. Confirm: "Token saved. You can now use /kite-oi-history, /kite-option-snapshot, /kite-expiry-drama, and /kite-agent-flow."

## Notes
- Access tokens are valid for one calendar day (midnight IST reset)
- The credentials file is at `~/.kite_creds.json` and is never committed to git
- The API key (`plz6ik09bgb62mey`) typically never changes
