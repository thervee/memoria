"""Manage developer accounts (for hosting Memoria online). Run it on the server.

    python manage_accounts.py make-secret
        Make a server secret. Put it in .env as MEMORIA_SECRET_KEY=... (once, before
        creating accounts). It locks developers' agent signing keys; keep it private
        and back it up: without it, those agents can't sign memories any more.

    python manage_accounts.py create <account> [--name "Acme Ltd"] [--email dev@acme.example]
    python manage_accounts.py new-key <account> <agent>     make an API key (shown once)
    python manage_accounts.py revoke-key <key-id>           stop a key working
    python manage_accounts.py list                          all accounts, keys and usage
    python manage_accounts.py disable <account>             turn an account off
    python manage_accounts.py enable <account>              turn it back on
    python manage_accounts.py set-limit <account> <limit> <number|default>
        Limits: requests_per_minute, writes_per_day, max_memories, max_agents
    python manage_accounts.py invite                        make a one-time invite code
                                                            (for MEMORIA_SIGNUP=invite)

Changes take effect straight away, even while Memoria is running.
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # MEMORIA_SECRET_KEY

import accounts  # noqa: E402
import identity  # noqa: E402


def _usage() -> dict:
    path = accounts.ACCOUNTS_FILE.parent / "usage.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _memory_counts() -> dict:
    path = accounts.ACCOUNTS_FILE.parent / "memories.json"
    counts = {}
    if path.exists():
        for mem in json.loads(path.read_text(encoding="utf-8")).values():
            account = mem.get("account", accounts.LOCAL)
            counts[account] = counts.get(account, 0) + 1
    return counts


def show_list() -> None:
    data = accounts.load()
    if not data["accounts"]:
        print("No developer accounts yet. Make one with: manage_accounts.py create <account>")
        return
    usage, counts = _usage(), _memory_counts()
    for account, info in sorted(data["accounts"].items()):
        limit = accounts.limits(account)
        today = usage.get(account, {})
        print(f"{account}  [{info['status']}]  {info.get('name') or ''} {info.get('email') or ''}")
        print(f"    memories: {counts.get(account, 0)} of {limit['max_memories']}   "
              f"writes today: {today.get('writes', 0)} of {limit['writes_per_day']}   "
              f"requests/minute: {limit['requests_per_minute']}")
        for key in accounts.keys_of(account):
            state = f"revoked {key['revoked_at']}" if key["revoked_at"] else "active"
            print(f"    key {key['key_id']}  agent '{key['agent']}'  {state}")
    unused = sum(1 for i in data["invites"].values() if not i["used_by"])
    print(f"\nUnused invite codes: {unused}.  Sign-up mode: {accounts.signup_mode()}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Manage Memoria developer accounts.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("make-secret")
    create = commands.add_parser("create")
    create.add_argument("account")
    create.add_argument("--name", default="")
    create.add_argument("--email", default="")
    new_key = commands.add_parser("new-key")
    new_key.add_argument("account")
    new_key.add_argument("agent")
    revoke = commands.add_parser("revoke-key")
    revoke.add_argument("key_id")
    commands.add_parser("list")
    for name in ("disable", "enable"):
        commands.add_parser(name).add_argument("account")
    set_limit = commands.add_parser("set-limit")
    set_limit.add_argument("account")
    set_limit.add_argument("limit")
    set_limit.add_argument("value")
    commands.add_parser("invite")
    args = parser.parse_args(argv)

    try:
        if args.command == "make-secret":
            print("Put this line in .env on the server (and keep a private copy):\n")
            print(f"MEMORIA_SECRET_KEY={identity.make_server_secret()}")
        elif args.command == "create":
            accounts.create_account(args.account, args.name, args.email)
            print(f"Created account '{args.account.strip().lower()}'. "
                  f"Now make an API key: manage_accounts.py new-key {args.account} <agent>")
        elif args.command == "new-key":
            key = accounts.create_key(args.account, args.agent)
            print(f"API key for agent '{args.agent}' of '{args.account}':\n\n  {key}\n")
            print("Give it to the developer now: it is shown only this once.")
        elif args.command == "revoke-key":
            key = accounts.revoke_key(args.key_id)
            print(f"Revoked key {args.key_id} ('{key['agent']}' of '{key['account']}').")
        elif args.command == "list":
            show_list()
        elif args.command in ("disable", "enable"):
            accounts.set_status(args.account, "disabled" if args.command == "disable" else "active")
            print(f"'{args.account}' is now {args.command}d.")
        elif args.command == "set-limit":
            value = None if args.value == "default" else int(args.value)
            accounts.set_limit(args.account, args.limit, value)
            print(f"{args.limit} for '{args.account}' is now "
                  f"{accounts.limits(args.account)[args.limit]}.")
        elif args.command == "invite":
            print(f"Invite code (works once):  {accounts.create_invite()}")
    except (accounts.AccountError, identity.IdentityError, ValueError) as e:
        sys.exit(f"Not done: {e}")


if __name__ == "__main__":
    main()
