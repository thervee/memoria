"""Register a new agent: creates its secret key and public key.

Usage:  .venv\\Scripts\\python.exe register_agent.py <agent-id>
Or double-click register_agent.bat and type the ID.
"""
import sys

import identity

if len(sys.argv) != 2:
    sys.exit("Usage: register_agent.py <agent-id>")
try:
    public = identity.register_agent(sys.argv[1])
except identity.IdentityError as e:
    sys.exit(f"Not registered: {e}")
print(f"Registered '{sys.argv[1].strip().lower()}'.")
print(f"  Public key (safe to share): {public}")
print(f"  Secret key: saved encrypted in the keys folder. Never share it.")
