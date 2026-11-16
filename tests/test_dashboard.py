"""The dashboard (dashboard.py): who may open it and who may change things."""
import asyncio

import pytest
from starlette.testclient import TestClient

import dashboard
import memoria as m

HOME = "http://127.0.0.1:8765"
SAME_ORIGIN = {"origin": HOME, "sec-fetch-site": "same-origin"}


@pytest.fixture
def client(store):
    with TestClient(dashboard.app, base_url=HOME) as c:
        yield c


def save(agent="alice", **kw):
    return asyncio.run(m.save_memory(agent, "Penguins huddle.", "r", "test/animals", **kw))


def test_page_and_memories_open_from_this_laptop(client):
    mid = save()["memory_id"]
    assert client.get("/dashboard").status_code == 200
    body = client.get("/dashboard/api/memories").json()
    assert [x["memory_id"] for x in body["memories"]] == [mid]
    assert body["agents"] == ["alice", "bob", "carol"]
    detail = client.get(f"/dashboard/api/memories/{mid}").json()
    assert detail["versions"][0]["current"] is True


def test_security_headers_are_sent(client):
    headers = client.get("/dashboard").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("host", ["evil.example:8765", "192.168.1.5:8765", "127.0.0.1:9999"])
def test_other_host_names_are_refused(client, host):
    # e.g. a website using "DNS rebinding" to reach the dashboard under its own name
    assert client.get("/dashboard/api/memories", headers={"host": host}).status_code == 403


@pytest.mark.parametrize("headers", [
    {},                                                          # no origin at all
    {"origin": "https://evil.example"},                          # another website
    {"origin": HOME, "sec-fetch-site": "cross-site"},            # browser says cross-site
    {"origin": "http://127.0.0.1:9999", "sec-fetch-site": "same-site"},
])
def test_changes_from_anywhere_but_the_dashboard_are_refused(client, headers):
    mid = save()["memory_id"]
    r = client.post(f"/dashboard/api/memories/{mid}/delete", headers=headers)
    assert r.status_code == 403
    assert [x["memory_id"] for x in m.all_memories()] == [mid]


def test_dashboard_changes_sharing_as_the_author(client):
    mid = save()["memory_id"]
    r = client.post(f"/dashboard/api/memories/{mid}/sharing", headers=SAME_ORIGIN,
                    json={"visibility": "shared", "shared_with": ["bob"]})
    assert r.status_code == 200 and r.json()["shared_with"] == ["bob"]
    log = m.ACTIVITY_FILE.read_text()
    assert '"action": "share"' in log and '"via": "dashboard"' in log


def test_dashboard_deletes_as_the_author(client):
    mid = save()["memory_id"]
    r = client.post(f"/dashboard/api/memories/{mid}/delete", headers=SAME_ORIGIN)
    assert r.status_code == 200 and m.all_memories() == []
    assert client.get(f"/dashboard/api/memories/{mid}").status_code == 404


def test_memory_whose_author_key_is_missing_cant_be_changed(client, keys):
    mid = save(agent="carol")["memory_id"]
    (keys / "carol.key").unlink()
    r = client.post(f"/dashboard/api/memories/{mid}/delete", headers=SAME_ORIGIN)
    assert r.status_code == 400 and "key isn't on this laptop" in r.json()["message"]


def test_oversized_and_bad_requests_are_refused(client):
    mid = save()["memory_id"]
    url = f"/dashboard/api/memories/{mid}/sharing"
    assert client.post(url, headers=SAME_ORIGIN, content=b"x" * 20_001).status_code == 400
    assert client.post(url, headers=SAME_ORIGIN, content=b"[1, 2]").status_code == 400
