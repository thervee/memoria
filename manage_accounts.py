"""Manage developer accounts (for hosting Memoria online). Run it on the server.

    python manage_accounts.py make-secret
        Make a server secret, once, before creating accounts. It prints the Name and
        Value to add in Railway's Variables tab (or the line for a .env file). It locks developers' agent signing keys; keep it private
        and back it up: without it, those agents can't sign memories any more.

    python manage_accounts.py create <account> [--name "Acme Ltd"] [--email dev@acme.example]
    python manage_accounts.py new-key <account> <agent>     make an API key (shown once)
    python manage_accounts.py revoke-key <key-id>           stop a key working
    python manage_accounts.py list                          all accounts, keys and usage
    python manage_accounts.py disable <account>             turn an account off
    python manage_accounts.py enable <account>              turn it back on
    python manage_accounts.py set-limit <account> <limit> <number|default>
        Limits: requests_per_minute, account_requests_per_minute, writes_per_day,
                max_memories, max_agents
    python manage_accounts.py invite                        make a one-time invite code
                                                            (for MEMORIA_SIGNUP=invite)

Paying with $MEMORIA (see billing.py):
    python manage_accounts.py balance <account>             actions left, wallets, payments
    python manage_accounts.py add-credits <account> <credits> [--reason "..."]
    python manage_accounts.py remove-credits <account> <credits> [--reason "..."]
        Change a balance by hand (1 credit = MEMORIA_ACTIONS_PER_CREDIT actions). Logged.
    python manage_accounts.py public-purchases <account> on|off
        Show (or hide) the account's purchase history at /purchases/<account>.

Changes take effect straight away, even while Memoria is running.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # MEMORIA_SECRET_KEY

import accounts  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import identity  # noqa: E402


def show_list() -> None:
    every = accounts.list_accounts()
    if not every:
        print("No developer accounts yet. Make one with: manage_accounts.py create <account>")
        return
    today = datetime.now(timezone.utc).date().isoformat()
    for info in every:
        account, limit = info["id"], accounts.limits(info["id"])
        print(f"{account}  [{info['status']}]  {info.get('name') or ''} {info.get('email') or ''}")
        print(f"    memories: {db.count_memories(account)} of {limit['max_memories']}   "
              f"writes today: {db.writes_today(account, today)} of {limit['writes_per_day']}   "
              f"requests/minute: {limit['requests_per_minute']} per agent, "
              f"{limit['account_requests_per_minute']} per account")
        for key in accounts.keys_of(account):
            state = f"revoked {key['revoked_at']}" if key["revoked_at"] else "active"
            print(f"    key {key['key_id']}  agent '{key['agent']}'  {state}")
    print(f"\nUnused invite codes: {accounts.unused_invites()}.  "
          f"Sign-up mode: {accounts.signup_mode()}")


def show_balance(account: str) -> None:
    if accounts.get_account(account) is None:
        raise billing.BillingError(f"No account called '{account}'.")
    info = billing.balance(account, "host")
    public = db.query("SELECT public_purchases FROM billing WHERE account = ?", (account,))
    print(f"{account}: {info['actions']} actions ({info['credits']} credits)   "
          f"billing is {info['billing']}   public purchase history: "
          f"{'on' if public and public[0][0] else 'off'}")
    print(f"    wallets: {', '.join(info['wallets']) or 'none linked'}")
    payments = billing.payments_of(account)
    if not payments:
        print("    payments: none")
    for pay in payments:
        print(f"    {pay['date']}  {pay['tokens']} $MEMORIA = {pay['credits']} credits "
              f"({pay['actions']} actions)  {pay['explorer']}")


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
    commands.add_parser("balance").add_argument("account")
    for name in ("add-credits", "remove-credits"):
        change = commands.add_parser(name)
        change.add_argument("account")
        change.add_argument("credits", type=int)
        change.add_argument("--reason", default="")
    public = commands.add_parser("public-purchases")
    public.add_argument("account")
    public.add_argument("setting", choices=["on", "off"])
    args = parser.parse_args(argv)

    try:
        if args.command == "make-secret":
            secret = identity.make_server_secret()
            print("Your new server secret. Keep a private copy (e.g. in a password manager):")
            print("if it's lost, developers' agents can no longer sign memories.\n")
            print("In Railway, Variables tab, New Variable:")
            print("  Name:   MEMORIA_SECRET_KEY")
            print(f"  Value:  {secret}\n")
            print("Or, in a .env file (or Railway's Raw Editor), this whole line:")
            print(f"  MEMORIA_SECRET_KEY={secret}")
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
        elif args.command == "balance":
            show_balance(args.account)
        elif args.command in ("add-credits", "remove-credits"):
            if args.credits < 1:
                raise ValueError("Give a whole number of credits, 1 or more.")
            sign = 1 if args.command == "add-credits" else -1
            info = billing.adjust(args.account, sign * args.credits, args.reason)
            print(f"{'Added' if sign > 0 else 'Removed'} {args.credits} credits. "
                  f"'{args.account}' now has {info['actions']} actions "
                  f"({info['credits']} credits).")
        elif args.command == "public-purchases":
            billing.set_public_purchases(args.account, args.setting == "on")
            print(f"Purchase history for '{args.account}' is now "
                  f"{'public at /purchases/' + args.account if args.setting == 'on' else 'hidden'}.")
    except (accounts.AccountError, billing.BillingError, identity.IdentityError,
            ValueError) as e:
        sys.exit(f"Not done: {e}")


if __name__ == "__main__":
    main()
