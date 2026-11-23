"""Agent keys, login passes and signatures (identity.py)."""
import sys
import time

import pytest

import identity


# ---------- registering ----------

@pytest.mark.skipif(sys.platform != "win32", reason="checks Windows DPAPI encryption, "
                    "which only exists on Windows")
def test_register_creates_encrypted_secret_and_public_key(keys):
    secret_file = keys / "alice.key"
    assert secret_file.exists()
    assert identity.public_key_of("alice")
    # The secret key on disk is encrypted, so it isn't just the raw 32 key bytes.
    assert len(identity._unb64(secret_file.read_text())) > 32


def test_register_refuses_duplicates_and_bad_ids(keys):
    with pytest.raises(identity.IdentityError, match="already registered"):
        identity.register_agent("alice")
    for bad in ("", "A", "Has Caps", "-starts-with-dash", "x" * 41, "bad\\char",
                "too/many/parts", "local/sneaky", "/no-account", "acme/"):
        with pytest.raises(identity.IdentityError):
            identity.register_agent(bad)


# ---------- login passes ----------

def test_valid_pass_proves_the_agent(keys):
    assert identity.check_token(identity.make_token("alice")) == "alice"


def test_pass_for_unregistered_agent_cant_be_made(keys):
    with pytest.raises(identity.IdentityError, match="not registered"):
        identity.make_token("dave")


def test_expired_and_future_passes_are_refused(keys, monkeypatch):
    token = identity.make_token("alice")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + identity.TOKEN_LIFETIME + 1)
    with pytest.raises(identity.IdentityError, match="expired"):
        identity.check_token(token)
    monkeypatch.setattr(time, "time", lambda: now - identity.CLOCK_SLACK - 60)
    with pytest.raises(identity.IdentityError, match="future"):
        identity.check_token(token)


def test_pass_cant_be_reused_for_another_agent(keys):
    version, _, issued, signature = identity.make_token("alice").split(".")
    forged = ".".join([version, "bob", issued, signature])
    with pytest.raises(identity.IdentityError, match="not signed with bob's key"):
        identity.check_token(forged)


def test_pass_with_changed_time_is_refused(keys):
    version, agent, issued, signature = identity.make_token("alice").split(".")
    forged = ".".join([version, agent, str(int(issued) + 1), signature])
    with pytest.raises(identity.IdentityError, match="not signed"):
        identity.check_token(forged)


@pytest.mark.parametrize("token", ["", "garbage", "v1.alice.notanumber.sig",
                                   "v2.alice.1.sig", "v1.dave.1.sig"])
def test_malformed_passes_are_refused(keys, token):
    with pytest.raises(identity.IdentityError):
        identity.check_token(token)


# ---------- signatures ----------

def test_signature_checks(keys):
    message = b"hello"
    sig = identity.sign("alice", message)
    assert identity.verify(message, sig, "alice") == (True, "signed by alice")

    ok, why = identity.verify(b"changed", sig, "alice")
    assert not ok and "changed after it was signed" in why

    ok, why = identity.verify(message, sig, "bob")
    assert not ok and "signed by 'alice'" in why

    ok, why = identity.verify(message, None, "alice")
    assert not ok and "no signature" in why

    swapped = dict(sig, public_key=identity.public_key_of("bob"))
    ok, why = identity.verify(message, swapped, "alice")
    assert not ok and "registered key" in why


def test_signature_by_bob_relabelled_as_alice_fails(keys):
    sig = identity.sign("bob", b"hello")
    relabelled = dict(sig, signed_by="alice", public_key=identity.public_key_of("alice"))
    ok, why = identity.verify(b"hello", relabelled, "alice")
    assert not ok and "changed after it was signed" in why
