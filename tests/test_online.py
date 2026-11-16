"""Running online (Railway): settings, the data folder, the Docker image and Git.

The settings in paths.py are read once when Memoria starts, so most checks start
a fresh Python with the environment variables of each situation.
"""
import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import dashboard
import db
import identity
import limits
import paths
import server

HERE = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
ONLINE_SETTINGS = ("MEMORIA_ONLINE", "MEMORIA_DATA_DIR", "MEMORIA_HOST", "MEMORIA_PORT", "PORT",
                   "MEMORIA_TRUST_PROXY", "MEMORIA_DASHBOARD", "RAILWAY_ENVIRONMENT_NAME",
                   "RAILWAY_PROJECT_ID", "RAILWAY_VOLUME_MOUNT_PATH", "MEMORIA_BACKUP_DIR")


def clean_env(**settings) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ONLINE_SETTINGS}
    env.update(settings)
    return env


def settings_in_fresh_python(**env) -> dict:
    """What Memoria's modules decide, in a new Python with only these settings."""
    script = (
        "import json, paths, db, identity, backup, restore_backup, limits, dashboard;"
        "print(json.dumps({'store': str(paths.STORE), 'keys': str(identity.KEYS_DIR),"
        " 'db': str(db.DB_FILE), 'backups': str(backup.backup_dir()),"
        " 'restore_store': str(restore_backup.STORE), 'host': paths.HOST, 'port': paths.PORT,"
        " 'online': paths.ONLINE, 'trust_proxy': limits._trust_proxy(),"
        " 'dashboard': dashboard.handles('/dashboard')}))"
    )
    out = subprocess.run([PYTHON, "-c", script], cwd=HERE, env=clean_env(**env),
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---------- the laptop stays exactly as it is ----------

def test_laptop_settings_are_unchanged():
    got = settings_in_fresh_python(PORT="9999")  # a stray PORT on the laptop is ignored
    assert got == {
        "store": str(HERE / "memory_store"), "keys": str(HERE / "keys"),
        "db": str(HERE / "memory_store" / "memoria.db"), "backups": str(HERE / "backups"),
        "restore_store": str(HERE / "memory_store"), "host": "127.0.0.1", "port": 8765,
        "online": False, "trust_proxy": False, "dashboard": True,
    }


# ---------- online ----------

def test_online_uses_one_data_folder_0000_and_port(tmp_path):
    got = settings_in_fresh_python(MEMORIA_ONLINE="1", MEMORIA_DATA_DIR=str(tmp_path), PORT="4321")
    data = tmp_path.resolve()
    assert got["store"] == str(data / "memory_store")
    assert got["db"] == str(data / "memory_store" / "memoria.db")
    assert got["keys"] == str(data / "keys")
    assert got["backups"] == got["restore_store"].replace("memory_store", "backups")
    assert got["backups"] == str(data / "backups")
    assert (got["host"], got["port"], got["online"]) == ("0.0.0.0", 4321, True)
    assert got["trust_proxy"] is True   # Railway sits behind a proxy
    assert got["dashboard"] is False    # never served online


def test_online_without_port_falls_back_to_8765():
    got = settings_in_fresh_python(MEMORIA_ONLINE="1")
    assert got["port"] == 8765


def test_data_folder_can_also_be_used_on_its_own(tmp_path):
    got = settings_in_fresh_python(MEMORIA_DATA_DIR=str(tmp_path))
    assert got["store"] == str(tmp_path.resolve() / "memory_store")
    assert (got["host"], got["online"], got["dashboard"]) == ("127.0.0.1", False, True)


def test_proxy_trust_can_still_be_turned_off_online():
    got = settings_in_fresh_python(MEMORIA_ONLINE="1", MEMORIA_TRUST_PROXY="0")
    assert got["trust_proxy"] is False


def test_dashboard_is_off_online_whatever_else_is_set(monkeypatch):
    monkeypatch.setattr(paths, "ONLINE", True)
    monkeypatch.setenv("MEMORIA_DASHBOARD", "on")
    assert not dashboard.handles("/dashboard") and not dashboard.handles("/")


# ---------- refusing to start half set up ----------

@pytest.fixture
def online(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ONLINE", True)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("LLM_API_KEY", "a-key")
    monkeypatch.setenv("MEMORIA_SECRET_KEY", identity.make_server_secret())
    for name in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_VOLUME_MOUNT_PATH"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "data"


def test_nothing_to_check_on_the_laptop(monkeypatch):
    monkeypatch.setattr(paths, "ONLINE", False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert paths.online_problems() == []


def test_fully_set_up_online_has_no_problems(online, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(online))
    assert paths.online_problems() == []


@pytest.mark.parametrize("change, problem", [
    ({"LLM_API_KEY": ""}, "LLM_API_KEY"),
    ({"MEMORIA_SECRET_KEY": ""}, "MEMORIA_SECRET_KEY isn't set"),
    ({"MEMORIA_SECRET_KEY": "not-a-real-secret"}, "isn't a valid secret"),
    ({"RAILWAY_ENVIRONMENT_NAME": "production"}, "No Railway volume"),
])
def test_missing_settings_are_reported(online, monkeypatch, change, problem):
    for name, value in change.items():
        monkeypatch.setenv(name, value)
    problems = paths.online_problems()
    assert len(problems) == 1 and problem in problems[0]


def test_missing_data_folder_is_reported(online, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", None)
    assert "MEMORIA_DATA_DIR isn't set" in paths.online_problems()[0]


def test_data_folder_outside_the_volume_is_reported(online, monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "volume"))
    assert "isn't on the Railway volume" in paths.online_problems()[0]
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "volume" / "memoria")  # inside: fine
    assert paths.online_problems() == []


def test_server_refuses_to_start_online_without_its_settings():
    env = clean_env(MEMORIA_ONLINE="1", MEMORIA_SECRET_KEY="", PORT=str(free_port()))
    result = subprocess.run([PYTHON, "server.py"], cwd=HERE, env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 1
    assert "can't start online" in result.stderr
    assert "MEMORIA_SECRET_KEY isn't set" in result.stderr
    assert "MEMORIA_DATA_DIR isn't set" in result.stderr


# ---------- /health ----------

def test_health_answers_without_a_key(store):
    client = TestClient(server.build_app())  # no "with": start-up jobs don't run
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_reports_a_broken_database(store, monkeypatch):
    def broken(*args, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(db, "query", broken)
    r = TestClient(server.health).get("/health")
    assert r.status_code == 503


def test_health_isnt_counted_against_spam_limits(store, monkeypatch):
    monkeypatch.setattr(limits.per_ip, "limit", 1)
    client = TestClient(server.build_app(), client=("203.0.113.9", 1))
    assert [client.get("/health").status_code for _ in range(3)] == [200, 200, 200]


# ---------- the Docker image ----------

def dockerfile() -> str:
    return (HERE / "Dockerfile").read_text(encoding="utf-8")


def copied_files() -> set[str]:
    sources = set()
    for line in re.findall(r"^COPY ((?:.*\\\n)*.*)$", dockerfile(), re.M):
        sources |= set(line.replace("\\\n", " ").split()[:-1])
    return sources


def test_image_copies_only_named_code_files():
    files = copied_files()
    assert "." not in files and "*" not in files and not any("/" in f for f in files)
    assert all(f.endswith(".py") or f == "requirements.txt" for f in files)
    assert all((HERE / f).exists() for f in files)
    assert not re.search(r"^ADD ", dockerfile(), re.M)


def test_image_has_every_module_the_server_needs():
    local = {p.stem for p in HERE.glob("*.py")}
    need, todo = set(), ["server"]
    while todo:
        module = todo.pop()
        if module in need:
            continue
        need.add(module)
        tree = ast.parse((HERE / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            todo += [n.split(".")[0] for n in names if n.split(".")[0] in local]
    assert {f"{m}.py" for m in need} <= copied_files()
    assert "manage_accounts.py" in copied_files()  # the host runs it on the server


def test_image_holds_no_secrets():
    text = dockerfile()
    for secret in ("LLM_API_KEY", "MEMORIA_SECRET_KEY"):
        assert not re.search(rf"{secret}\s*=", text), f"{secret} must come from Railway"
    assert "MEMORIA_ONLINE=1" in text and "MEMORIA_DATA_DIR=/data" in text
    assert 'CMD ["python", "server.py"]' in text


def test_dockerignore_shuts_out_secrets_and_data():
    rules = [line.strip() for line in (HERE / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert rules[0] == "*"  # everything is left out unless let in
    let_in = [r for r in rules if r.startswith("!")]
    assert set(let_in) == {"!requirements.txt", "!*.py"}
    last_let_in = max(rules.index(r) for r in let_in)
    for private in (".env", ".env.*", "keys", "memory_store", "backups", "data"):
        assert private in rules and rules.index(private) > last_let_in


def test_railway_config_checks_health():
    config = json.loads((HERE / "railway.json").read_text())
    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["deploy"]["healthcheckPath"] == "/health"


# ---------- GitHub ----------

@pytest.mark.parametrize("path", [".env", ".env.production", "keys/trader.key", "keys/acme/bot.key",
                                  "memory_store/memoria.db", "memory_store.old-1/x",
                                  "backups/memoria-backup-1.zip", "data/memory_store/memoria.db",
                                  "somewhere/else.db", "memory_store/memoria.db-wal"])
def test_git_never_uploads_secrets_or_data(path):
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=HERE)
    assert result.returncode == 0, f"{path} is not ignored by Git"


def test_nothing_private_is_already_in_git():
    tracked = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True,
                             check=True).stdout.split()
    private = [f for f in tracked if f == ".env" or f.startswith(("keys/", "memory_store",
                                                                  "backups/", "data/"))
               or f.endswith((".key", ".db"))]
    assert private == []


# ---------- end to end: Memoria running online, on this laptop ----------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_memoria_runs_online_with_all_data_in_the_data_folder(tmp_path):
    data, port = tmp_path / "data", free_port()
    secret = identity.make_server_secret()
    env = clean_env(MEMORIA_ONLINE="1", MEMORIA_DATA_DIR=str(data), PORT=str(port),
                    MEMORIA_SECRET_KEY=secret, LLM_API_KEY="not-used-in-this-test",
                    MEMORIA_HOST="127.0.0.1")  # (0.0.0.0 would make Windows ask about the firewall)
    before = sorted(p.name for p in (HERE / "memory_store").glob("*")) \
        if (HERE / "memory_store").exists() else []
    proc = subprocess.Popen([PYTHON, "server.py"], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(120):
            try:
                status, body = get(f"http://127.0.0.1:{port}/health")
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("Memoria didn't start: " + proc.stdout.read())
        assert status == 200 and json.loads(body) == {"status": "ok"}
        assert get(f"http://127.0.0.1:{port}/dashboard")[0] == 401  # no dashboard online
        assert get(f"http://127.0.0.1:{port}/signup")[0] == 404     # sign-up off by default

        # The host makes an account on the server, as they would through Railway's shell.
        made = subprocess.run([PYTHON, "manage_accounts.py", "create", "acme"], cwd=HERE,
                              env=env, capture_output=True, text=True, timeout=60)
        assert made.returncode == 0, made.stderr
        out = subprocess.run([PYTHON, "manage_accounts.py", "new-key", "acme", "bot"], cwd=HERE,
                             env=env, capture_output=True, text=True, timeout=60).stdout
        key = next(word for word in out.split() if word.startswith("mem_"))

        import asyncio

        from memoria_client import Memoria

        async def use_it():
            async with Memoria(api_key=key, url=f"http://127.0.0.1:{port}/mcp") as mem:
                return await mem.find("anything")
        assert asyncio.run(use_it()) == []
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    # Everything landed in the data folder...
    assert (data / "memory_store" / "memoria.db").exists()
    assert (data / "keys" / "acme" / "bot.key").read_text().startswith(identity.SEALED)
    assert (data / "keys" / "public_keys.json").exists()
    # ...and nothing in this laptop's own folders changed.
    after = sorted(p.name for p in (HERE / "memory_store").glob("*")) \
        if (HERE / "memory_store").exists() else []
    assert after == before
    assert not (HERE / "keys" / "acme").exists()


# ---------- the server secret, however it was pasted ----------

def pasted_ways(secret):
    return [secret, f"  {secret}  ", f'"{secret}"', f"'{secret}'",
            f"MEMORIA_SECRET_KEY={secret}", f"MEMORIA_SECRET_KEY = {secret}",
            f'MEMORIA_SECRET_KEY="{secret}"', f'"MEMORIA_SECRET_KEY={secret}"',
            f"memoria_secret_key={secret}\n"]


def test_secret_pasted_in_any_usual_way_is_accepted(online, monkeypatch):
    secret = identity.make_server_secret()
    for pasted in pasted_ways(secret):
        monkeypatch.setenv("MEMORIA_SECRET_KEY", pasted)
        assert paths.server_secret() == secret, pasted
        assert paths.online_problems() == [], pasted


def test_keys_locked_with_the_plain_secret_unlock_with_a_pasted_one(keys, monkeypatch):
    import accounts
    secret = identity.make_server_secret()
    monkeypatch.setenv("MEMORIA_SECRET_KEY", secret)
    accounts.create_account("acme")
    accounts.create_key("acme", "trader")
    for pasted in pasted_ways(secret):
        monkeypatch.setenv("MEMORIA_SECRET_KEY", pasted)
        assert identity.verify(b"hi", identity.sign("acme/trader", b"hi"), "acme/trader")[0]


@pytest.mark.parametrize("pasted", ["not-a-secret", "MEMORIA_SECRET_KEY=", "OTHER_NAME=abc",
                                    "MEMORIA_SECRET_KEY=too-short="])
def test_a_wrong_secret_is_still_refused_with_a_hint(online, monkeypatch, pasted):
    monkeypatch.setenv("MEMORIA_SECRET_KEY", pasted)
    problems = paths.online_problems()
    assert len(problems) == 1
    assert "isn't set" in problems[0] or "a secret is 44, ending in '='" in problems[0]


def test_make_secret_says_exactly_what_to_paste(capsys):
    import manage_accounts
    from cryptography.fernet import Fernet
    manage_accounts.main(["make-secret"])
    out = capsys.readouterr().out
    name = re.search(r"^  Name:\s+(\S+)$", out, re.M).group(1)
    value = re.search(r"^  Value:\s+(\S+)$", out, re.M).group(1)
    assert name == "MEMORIA_SECRET_KEY" and len(value) == 44 and value.endswith("=")
    Fernet(value)  # a real, working secret
    assert f"  MEMORIA_SECRET_KEY={value}" in out  # the .env / Raw Editor line too
    assert "Railway" in out and "private copy" in out
