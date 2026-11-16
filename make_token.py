"""Print a fresh Memoria login pass for an agent, as an HTTP header (JSON).

Claude Code runs this automatically (its "headersHelper") each time it connects.
Usage:  .venv\\Scripts\\python.exe make_token.py <agent-id>
"""
import json
import sys

import identity

if len(sys.argv) != 2:
    sys.exit("Usage: make_token.py <agent-id>")
try:
    token = identity.make_token(sys.argv[1])
except identity.IdentityError as e:
    sys.exit(f"Could not make a login pass: {e}")
print(json.dumps({"Authorization": f"Bearer {token}"}))
