"""Reserved agent names: "admin" and similar can't be taken by new agents."""
import asyncio

import pytest

import accounts
import identity
import manage_accounts
import memoria as m


@pytest.mark.parametrize("name", ["admin", "administrator", "root", "system", "host",
                                  "memoria", "owner", "superuser", "support", "staff",
                                  "moderator", "admin-bot", "admin2", "ADMIN"])
def test_these_names_are_reserved(name):
    assert identity.is_reserved(name)


@pytest.mark.parametrize("name", ["trader", "analyst", "main", "hosting-bot", "badmin",
                                  "my-admin", "memorial"])
def test_ordinary_names_are_not(name):
    assert not identity.is_reserved(name)


def test_new_local_agents_cant_take_a_reserved_name(keys):
    for name in ("admin", "host", "admin-bot"):
        with pytest.raises(identity.IdentityError, match="reserved name"):
            identity.register_agent(name)
    identity.register_agent("helper")  # ordinary names still work


def test_new_api_keys_cant_be_made_for_a_reserved_name(devs, capsys):
    for name in ("admin", "memoria", "root"):
        with pytest.raises(accounts.AccountError, match="reserved name"):
            accounts.create_key("acme", name)
    with pytest.raises(SystemExit):
        manage_accounts.main(["new-key", "acme", "admin"])
    assert identity.public_key_of("acme/admin") is None


def test_the_website_cant_request_a_key_for_a_reserved_name(devs):
    accounts.create_account("newco", email="dev@new.example")
    with pytest.raises(accounts.AccountError, match="reserved name"):
        accounts.start_key_request("newco", "dev@new.example", "admin")


def test_agents_that_already_have_a_reserved_name_keep_working(store, devs, monkeypatch):
    # An agent called "admin" made before the rule existed.
    real = identity.is_reserved
    monkeypatch.setattr(identity, "is_reserved", lambda name: False)
    old_key = accounts.create_key("acme", "admin")
    monkeypatch.setattr(identity, "is_reserved", real)  # the rule is back
    assert identity.is_reserved("admin")
    assert accounts.check_key(old_key) == ("acme", "admin")

    async def use_it():
        saved = await m.save_memory("admin", "Still works.", "r", "t", account="acme")
        found = await m.list_memories("admin", account="acme")
        return saved, found
    saved, found = asyncio.run(use_it())
    assert saved["verification"] == "verified" and found[0]["note"] == "Still works."
    new_key = accounts.create_key("acme", "admin")  # the host can still replace its key
    assert accounts.check_key(new_key) == ("acme", "admin")
