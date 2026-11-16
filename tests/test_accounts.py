"""Developer accounts and API keys (accounts.py) and the host's tool (manage_accounts.py)."""
import json

import pytest

import accounts
import identity
import manage_accounts
from conftest import everything_stored


# ---------- accounts ----------

def test_account_and_key_work(keys):
    accounts.create_account("acme", name="Acme", email="dev@acme.example")
    key = accounts.create_key("acme", "trader")
    assert key.startswith("mem_")
    assert accounts.check_key(key) == ("acme", "trader")


def test_only_a_hash_of_the_key_is_stored(keys):
    accounts.create_account("acme")
    key = accounts.create_key("acme", "trader")
    secret = key.split("_", 2)[2]
    stored = everything_stored()
    assert secret not in stored and accounts._hash(secret) in stored


def test_keys_whose_secret_contains_underscores_work(keys):
    accounts.create_account("acme")
    for _ in range(40):
        key = accounts.create_key("acme", "trader")
        if "_" in key.split("_", 2)[2]:
            break
    else:
        pytest.skip("no key with an underscore came up")
    assert accounts.check_key(key) == ("acme", "trader")


@pytest.mark.parametrize("bad", ["", "nonsense", "mem_", "mem_abc", "mem_nosuchid_secret",
                                 "Bearer mem_x_y", "v1.alice.1.sig"])
def test_bad_keys_are_refused(keys, bad):
    accounts.create_account("acme")
    accounts.create_key("acme", "trader")
    with pytest.raises(accounts.AccountError, match="Invalid API key"):
        accounts.check_key(bad)


def test_key_with_wrong_secret_is_refused(keys):
    accounts.create_account("acme")
    key = accounts.create_key("acme", "trader")
    with pytest.raises(accounts.AccountError, match="Invalid API key"):
        accounts.check_key(key[:-3] + ("aaa" if not key.endswith("aaa") else "bbb"))


def test_revoked_key_and_disabled_account_are_refused(keys):
    accounts.create_account("acme")
    first, second = accounts.create_key("acme", "trader"), accounts.create_key("acme", "analyst")
    accounts.revoke_key(first.split("_")[1])
    with pytest.raises(accounts.AccountError, match="revoked"):
        accounts.check_key(first)
    accounts.set_status("acme", "disabled")
    with pytest.raises(accounts.AccountError, match="disabled"):
        accounts.check_key(second)
    accounts.set_status("acme", "active")
    assert accounts.check_key(second) == ("acme", "analyst")


def test_account_names_are_checked(keys):
    accounts.create_account("acme")
    with pytest.raises(accounts.AccountError, match="already"):
        accounts.create_account("acme")
    with pytest.raises(accounts.AccountError, match="reserved"):
        accounts.create_account("local")
    with pytest.raises(accounts.AccountError, match="lowercase"):
        accounts.create_account("Not Valid!")
    with pytest.raises(accounts.AccountError, match="No account"):
        accounts.create_key("nobody", "trader")


def test_number_of_agents_is_limited(keys):
    accounts.create_account("acme")
    accounts.set_limit("acme", "max_agents", 2)
    accounts.create_key("acme", "one")
    accounts.create_key("acme", "two")
    accounts.create_key("acme", "two")  # a second key for an existing agent is fine
    with pytest.raises(accounts.AccountError, match="most agents"):
        accounts.create_key("acme", "three")


def test_limits_default_and_can_be_changed(keys):
    accounts.create_account("acme")
    assert accounts.limits("acme") == accounts.DEFAULT_LIMITS
    assert accounts.limits("local") is None  # this laptop's agents have no limits
    accounts.set_limit("acme", "writes_per_day", 5)
    assert accounts.limits("acme")["writes_per_day"] == 5
    accounts.set_limit("acme", "writes_per_day", None)
    assert accounts.limits("acme")["writes_per_day"] == accounts.DEFAULT_LIMITS["writes_per_day"]
    with pytest.raises(accounts.AccountError, match="Unknown limit"):
        accounts.set_limit("acme", "everything", 1)


# ---------- developers' agent signing keys ----------

def test_developer_agent_keys_are_locked_with_the_server_secret(keys):
    accounts.create_account("acme")
    accounts.create_key("acme", "trader")
    key_file = keys / "acme" / "trader.key"
    assert key_file.read_text().startswith(identity.SEALED)
    sig = identity.sign("acme/trader", b"hello")
    assert identity.verify(b"hello", sig, "acme/trader")[0]


def test_without_the_server_secret_developer_keys_cant_be_made(keys, monkeypatch):
    accounts.create_account("acme")
    monkeypatch.delenv("MEMORIA_SECRET_KEY")
    with pytest.raises(identity.IdentityError, match="MEMORIA_SECRET_KEY isn't set"):
        accounts.create_key("acme", "trader")
    assert not (keys / "acme" / "trader.key").exists()


def test_a_different_server_secret_cant_unlock_the_keys(keys, monkeypatch):
    accounts.create_account("acme")
    accounts.create_key("acme", "trader")
    monkeypatch.setenv("MEMORIA_SECRET_KEY", identity.make_server_secret())
    with pytest.raises(identity.IdentityError, match="different"):
        identity.sign("acme/trader", b"hello")


def test_developer_agents_cant_use_laptop_login_passes(keys):
    accounts.create_account("acme")
    accounts.create_key("acme", "trader")
    with pytest.raises(identity.IdentityError):
        identity.make_token("acme/trader")
    with pytest.raises(identity.IdentityError):
        identity.check_token("v1.acme/trader.1.sig")


# ---------- invites ----------

def test_invite_works_once(keys):
    code = accounts.create_invite()
    accounts.use_invite(code, "acme")
    with pytest.raises(accounts.AccountError, match="already been used"):
        accounts.use_invite(code, "globex")
    with pytest.raises(accounts.AccountError, match="isn't valid"):
        accounts.use_invite("inv_made_up", "globex")
    assert code not in everything_stored()  # stored as a hash too


# ---------- manage_accounts.py ----------

def run_tool(capsys, *args):
    manage_accounts.main(list(args))
    return capsys.readouterr().out


def test_host_tool_creates_accounts_and_keys(keys, capsys):
    assert "MEMORIA_SECRET_KEY=" in run_tool(capsys, "make-secret")
    run_tool(capsys, "create", "acme", "--name", "Acme Ltd", "--email", "dev@acme.example")
    out = run_tool(capsys, "new-key", "acme", "trader")
    key = next(word for word in out.split() if word.startswith("mem_"))
    assert accounts.check_key(key) == ("acme", "trader")
    listing = run_tool(capsys, "list")
    assert "acme  [active]  Acme Ltd dev@acme.example" in listing and "agent 'trader'" in listing
    run_tool(capsys, "revoke-key", key.split("_")[1])
    assert "revoked" in run_tool(capsys, "list")


def test_host_tool_limits_status_and_invites(keys, capsys):
    run_tool(capsys, "create", "acme")
    assert "is now 7" in run_tool(capsys, "set-limit", "acme", "writes_per_day", "7")
    assert "is now 200" in run_tool(capsys, "set-limit", "acme", "writes_per_day", "default")
    assert "disabled" in run_tool(capsys, "disable", "acme")
    assert accounts.get_account("acme")["status"] == "disabled"
    code = run_tool(capsys, "invite").split()[-1]
    assert code.startswith("inv_")
    assert "Unused invite codes: 1" in run_tool(capsys, "list")


def test_host_tool_explains_mistakes(keys):
    with pytest.raises(SystemExit, match="No account"):
        manage_accounts.main(["new-key", "nobody", "trader"])
    with pytest.raises(SystemExit, match="Not done"):
        manage_accounts.main(["set-limit", "nobody", "writes_per_day", "lots"])


def test_changes_made_by_another_program_are_seen_at_once(keys):
    """manage_accounts.py runs separately from Memoria; Memoria must see its changes."""
    import sqlite3

    import db
    accounts.create_account("acme")
    key = accounts.create_key("acme", "trader")
    assert accounts.check_key(key)
    other_program = sqlite3.connect(db.DB_FILE)  # its own connection, like a second program
    other_program.execute("UPDATE accounts SET status = 'disabled' WHERE id = 'acme'")
    other_program.commit()
    other_program.close()
    with pytest.raises(accounts.AccountError, match="disabled"):
        accounts.check_key(key)
