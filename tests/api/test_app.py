"""Web API on the offline demo week: what waits on the owner, approvals, and the room."""

import time

import pytest
from fastapi.testclient import TestClient

from pgt.api.app import create_app, offline_team


def _idle(client: TestClient, limit: float = 60) -> dict:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        status = client.get("/api/status").json()
        if not status["busy"]:
            assert status["error"] is None, status["error"]
            return status
        time.sleep(0.05)
    raise AssertionError("team stayed busy")


@pytest.fixture
def client(tmp_path):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    with TestClient(create_app(team)) as c:
        _idle(c)
        yield c
    team.ws.close()


def _waiting(client):
    return [d for d in client.get("/api/deliverables").json()["items"] if d["status"] == "wait"]


def test_seeded_week_waits_on_the_test_plan(client):
    items = client.get("/api/deliverables").json()["items"]
    waiting = _waiting(client)
    assert [d["title"] for d in waiting] == ["Test plan · Week 1 · v1"]
    assert waiting[0]["from"]["name"] == "Growth Lead"
    # Empty daily change sets never show; ones with changes do.
    titles = {d["title"] for d in items}
    assert "Change set · Day 1 · v1" not in titles
    assert "Change set · Day 3 · v1" in titles
    day3 = next(d for d in items if d["title"] == "Change set · Day 3 · v1")
    assert day3["status"] == "applied"


def test_room_mentions_owner_and_folds_handoffs(client):
    items = client.get("/api/room").json()["items"]
    mentions = [i for i in items if i["kind"] == "msg" and i["mention"]]
    assert mentions and mentions[-1]["text"].startswith("@Jieyi ")
    assert mentions[-1]["card"]["status"] == "wait"
    assert any(i["kind"] == "fold" and i["tail"] == "worked on the Week 1 review" for i in items)
    simulated = [i for i in items if i["kind"] == "msg" and i["who"]["actor"] == "system"]
    assert simulated and simulated[0]["text"].startswith("Simulated approval")
    hidden = {"model.call", "evidence.added"}
    assert not any(i.get("type") in hidden for i in items)


def test_approving_plan_then_change_set_creates_paused_ads(client):
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    assert [d["title"] for d in _waiting(client)] == ["Change set · Week 1 · v1"]
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["can_approve"] and len(detail["ads"]) == 2
    url = detail["creatives"][0]["images"][0]["url"]
    assert client.get(url).status_code == 200
    assert client.post("/api/approve/week1/change_set").status_code == 200
    _idle(client)
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["status"] == "applied"
    room = client.get("/api/room").json()["items"]
    assert any(i["kind"] == "msg" and i["who"]["actor"] == "operator"
               and "all paused" in i["text"] for i in room)
    # Approving again is refused: nothing waits on the owner any more.
    assert client.post("/api/approve/week1/change_set").status_code == 409


def test_send_back_and_messages_land_in_the_thread(client):
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["kind"] == "msg" and i["mention"])
    client.post("/api/messages", json={"text": "Why lunch break?", "thread": root["id"]})
    assert client.post("/api/send-back/week1/test_plan", json={"note": "Lower the budget"}).status_code == 200
    assert _waiting(client) == []
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["id"] == root["id"])
    assert [r["text"] for r in root["replies"]] == ["Why lunch break?",
                                                     "Sent back: Lower the budget"]


def test_media_stays_inside_the_media_folder(client):
    assert client.get("/api/media/../ws.db").status_code == 404
