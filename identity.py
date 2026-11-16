"""Agent identity for Memoria: Ed25519 keys, login passes and signatures.

Every registered agent has:
  - a SECRET key, never uploaded to GitHub:
      * this laptop's own agents: keys/<agent>.key, encrypted with Windows DPAPI so
        it only works for your Windows account on this laptop.
      * agents of developer accounts (for hosting online): keys/<account>/<agent>.key,
        encrypted with the server secret MEMORIA_SECRET_KEY (works on any computer
        that has that secret, e.g. a Linux server).
  - a PUBLIC key: listed in keys/public_keys.json. Anyone can use it to check
    that agent's signatures.

An agent's full name (its "principal") is just "<agent>" for this laptop's own
agents (the built-in "local" account) and "<account>/<agent>" for developer accounts.

This laptop's agents log in with a short-lived pass (a "token") signed with their
secret key. Developer accounts' agents log in with API keys instead (accounts.py).
This file deliberately does not import Cognee, so making a pass is fast.
"""
import base64
import ctypes
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import paths

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEYS_DIR = paths.KEYS
REGISTRY = KEYS_DIR / "public_keys.json"
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
TOKEN_LIFETIME = 3600  # a login pass is valid for 1 hour
CLOCK_SLACK = 300      # allow clocks to be up to 5 minutes apart
_DPAPI_ENTROPY = b"memoria-agent-key-v1"
LOCAL = "local"            # the built-in account for this laptop's own agents
SEALED = "sealed-v1:"      # start of a secret key encrypted with MEMORIA_SECRET_KEY

try:  # Windows only; on other computers only sealed keys are used
    import ctypes.wintypes as wt
except (ImportError, ValueError):
    wt = None


class IdentityError(ValueError):
    """Something about an agent's identity is wrong, explained in plain words."""


# ---------- small helpers ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _check_id(agent_id: str) -> str:
    agent_id = (agent_id or "").strip().lower()
    if not AGENT_ID_PATTERN.match(agent_id):
        raise IdentityError("Agent IDs are 2-40 lowercase letters, numbers and dashes.")
    return agent_id


def principal(account: str, agent_id: str) -> str:
    """An agent's full name: "trader" for this laptop's agents, "acme/trader" otherwise."""
    return agent_id if account == LOCAL else f"{account}/{agent_id}"


def split_principal(name: str) -> tuple[str, str]:
    """The reverse of principal(): (account, agent)."""
    account, _, agent = name.rpartition("/")
    return (account or LOCAL), agent


def _check_principal(name: str) -> str:
    name = (name or "").strip().lower()
    account, agent = split_principal(name)
    if name.count("/") > 1 or not AGENT_ID_PATTERN.match(agent)             or not AGENT_ID_PATTERN.match(account) or (account == LOCAL and "/" in name):
        raise IdentityError("Agent IDs are 2-40 lowercase letters, numbers and dashes.")
    return name


def _key_file(name: str) -> Path:
    account, agent = split_principal(name)
    folder = KEYS_DIR if account == LOCAL else KEYS_DIR / account
    return folder / f"{agent}.key"


# ---------- the server secret: encrypts developer accounts' agent keys ----------

def _server_secret() -> Fernet:
    secret = paths.server_secret()  # (also accepts the whole line, quotes or spaces)
    if not secret:
        raise IdentityError("MEMORIA_SECRET_KEY isn't set. Make one with "
                            "'manage_accounts.py make-secret' and put it in .env.")
    try:
        return Fernet(secret)
    except ValueError:
        raise IdentityError("MEMORIA_SECRET_KEY isn't a valid secret. Make one with "
                            "'manage_accounts.py make-secret'.") from None


def make_server_secret() -> str:
    return Fernet.generate_key().decode()


def _lock(raw: bytes, name: str) -> str:
    """Encrypt a secret key for storing on disk."""
    if split_principal(name)[0] == LOCAL:
        return _b64(_dpapi(raw, encrypt=True))
    return SEALED + _server_secret().encrypt(raw).decode()


def _unlock(text: str) -> bytes:
    if text.startswith(SEALED):
        try:
            return _server_secret().decrypt(text[len(SEALED):].encode())
        except InvalidToken:
            raise IdentityError("This agent's key was locked with a different "
                                "MEMORIA_SECRET_KEY.") from None
    return _dpapi(_unb64(text), encrypt=False)


# ---------- Windows DPAPI: encrypt secret keys for this Windows account ----------

if wt is not None:
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if wt is None:
        raise IdentityError("This laptop's agent keys only work on Windows.")
    inp, _keep1 = _blob(data)
    ent, _keep2 = _blob(_DPAPI_ENTROPY)
    out = _Blob()
    crypt32 = ctypes.windll.crypt32
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("Memoria agent key"),
                                      ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent),
                                        None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise IdentityError("Windows could not unlock the key (wrong Windows account?).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# ---------- registering agents and loading keys ----------

def _registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {}


def public_key_of(agent_id: str) -> str | None:
    """The agent's registered public key (text), or None if it isn't registered."""
    entry = _registry().get(agent_id)
    return entry["public_key"] if entry else None


def register_agent(agent_id: str) -> str:
    """Create a key pair for a new agent ("trader" or "acme/trader"). Returns its public key."""
    agent_id = _check_principal(agent_id)
    registry = _registry()
    key_file = _key_file(agent_id)
    if agent_id in registry or key_file.exists():
        raise IdentityError(f"'{agent_id}' is already registered.")

    secret = Ed25519PrivateKey.generate()
    raw_secret = secret.private_bytes(serialization.Encoding.Raw,
                                      serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
    public = _b64(secret.public_key().public_bytes(serialization.Encoding.Raw,
                                                   serialization.PublicFormat.Raw))
    locked = _lock(raw_secret, agent_id)  # before creating anything, in case it fails
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(locked, encoding="utf-8")
    registry[agent_id] = {
        "public_key": public,
        "registered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY)
    return public


def _secret_key(agent_id: str) -> Ed25519PrivateKey:
    key_file = _key_file(agent_id)
    if not key_file.exists():
        raise IdentityError(f"'{agent_id}' is not registered (no secret key on this computer).")
    raw = _unlock(key_file.read_text(encoding="utf-8").strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


# ---------- login passes ----------

def make_token(agent_id: str) -> str:
    """A login pass for this agent, signed with its secret key. Valid for 1 hour."""
    agent_id = _check_id(agent_id)
    issued = int(time.time())
    message = f"memoria-login-v1|{agent_id}|{issued}".encode()
    signature = _secret_key(agent_id).sign(message)
    return f"v1.{agent_id}.{issued}.{_b64(signature)}"


def check_token(token: str) -> str:
    """Return the agent ID a login pass proves, or raise IdentityError."""
    try:
        version, agent_id, issued, signature = (token or "").strip().split(".")
        issued_at = int(issued)
    except ValueError:
        raise IdentityError("Login pass is missing or malformed.") from None
    if version != "v1":
        raise IdentityError("Unknown login pass version.")
    if not AGENT_ID_PATTERN.match(agent_id):  # login passes are for this laptop's agents only
        raise IdentityError("Login pass is missing or malformed.")
    public = public_key_of(agent_id)
    if public is None:
        raise IdentityError(f"'{agent_id}' is not a registered agent.")
    now = time.time()
    if issued_at > now + CLOCK_SLACK:
        raise IdentityError("Login pass is dated in the future.")
    if now - issued_at > TOKEN_LIFETIME:
        raise IdentityError("Login pass has expired; make a new one.")
    message = f"memoria-login-v1|{agent_id}|{issued_at}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public)).verify(_unb64(signature), message)
    except (InvalidSignature, ValueError):
        raise IdentityError(f"Login pass was not signed with {agent_id}'s key.") from None
    return agent_id


# ---------- signing and checking memories ----------

def sign(agent_id: str, message: bytes) -> dict:
    """Sign data with the agent's secret key. Returns what to store with the memory."""
    public = public_key_of(agent_id)
    if public is None:
        raise IdentityError(f"'{agent_id}' is not registered, so it can't sign memories.")
    return {
        "algorithm": "ed25519",
        "signed_by": agent_id,
        "public_key": public,
        "signature": _b64(_secret_key(agent_id).sign(message)),
    }


def verify(message: bytes, sig: dict | None, expected_signer: str) -> tuple[bool, str]:
    """Check a stored signature. Returns (ok, short explanation)."""
    if not sig:
        return False, "no signature (saved before signing was added)"
    if sig.get("signed_by") != expected_signer:
        return False, f"signed by '{sig.get('signed_by')}', but the memory says '{expected_signer}' wrote it"
    registered = public_key_of(expected_signer)
    if registered is None:
        return False, f"'{expected_signer}' is not a registered agent"
    if sig.get("public_key") != registered:
        return False, f"the stored public key is not {expected_signer}'s registered key"
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(registered)).verify(
            _unb64(sig.get("signature", "")), message)
    except (InvalidSignature, ValueError):
        return False, "the memory was changed after it was signed"
    return True, f"signed by {expected_signer}"
