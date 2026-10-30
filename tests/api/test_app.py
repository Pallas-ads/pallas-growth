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


def test_send_back_redoes_the_plan_and_asks_again(client):
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["kind"] == "msg" and i["mention"])
    client.post("/api/messages", json={"text": "Why lunch break?", "thread": root["id"]})
    assert client.post("/api/send-back/week1/test_plan", json={"note": " "}).status_code == 400
    res = client.post("/api/send-back/week1/test_plan", json={"note": "Lower the budget"})
    assert res.status_code == 200 and res.json()["redo"] is True
    _idle(client)
    room = client.get("/api/room").json()["items"]
    root = next(i for i in room if i["id"] == root["id"])
    assert [r["text"] for r in root["replies"]] == ["Why lunch break?",
                                                     "Sent back: Lower the budget"]
    # The Growth Lead redrafts with the note, and the new version waits on the owner.
    assert [d["title"] for d in _waiting(client)] == ["Test plan · Week 1 · v2"]
    v2 = client.get("/api/deliverables/week1/test_plan").json()
    assert v2["status"] == "wait"
    assert any("Redo the test plan with the owner's note: Lower the budget" in str(i)
               for i in room if i["kind"] == "fold")
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)


def test_send_back_redoes_the_new_ads_as_new_versions(client):
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    before = client.get("/api/deliverables/week1/change_set").json()
    note = "Use a calmer headline"
    assert client.post("/api/send-back/week1/change_set", json={"note": note}).status_code == 200
    _idle(client)
    assert [d["title"] for d in _waiting(client)] == ["Change set · Week 1 · v2"]
    after = client.get("/api/deliverables/week1/change_set").json()
    old_url = before["creatives"][0]["images"][0]["url"]
    new_url = after["creatives"][0]["images"][0]["url"]
    assert "-c1/" in old_url and "-c2/" in new_url
    assert [a["ad"] for a in before["ads"]] != [a["ad"] for a in after["ads"]]
    # Earlier images stay on disk; the redo writes its own.
    assert client.get(old_url).status_code == 200 and client.get(new_url).status_code == 200
    assert client.post("/api/approve/week1/change_set").status_code == 200
    _idle(client)
    assert client.get("/api/deliverables/week1/change_set").json()["status"] == "applied"


def test_media_stays_inside_the_media_folder(client):
    assert client.get("/api/media/../ws.db").status_code == 404


def test_settings_report_models_and_spend_per_role(client):
    s = client.get("/api/settings").json()
    assert s["mode"] == "offline" and s["cap"] is None and s["remaining"] is None
    roles = {u["who"]["actor"]: u for u in s["usage"]}
    assert {"analyst", "growth_lead", "strategist"} <= set(roles)
    assert roles["analyst"]["models"][0] == {
        "model": "muse-spark-1.3", "provider": "Meta API", "calls": roles["analyst"]["calls"]}
    assert sum(u["calls"] for u in s["usage"]) == s["model_calls"]


def test_messages_and_deliverables_name_their_models(client):
    items = client.get("/api/room").json()["items"]
    plan_msg = next(i for i in items if i["kind"] == "msg" and i["mention"])
    assert plan_msg["models"] == ["muse-spark-1.3"]
    d = client.get("/api/deliverables/week1/test_plan").json()
    assert d["made_with"]["calls"] >= 1
    assert d["made_with"]["models"][0]["model"] == "muse-spark-1.3"


def test_dashboard_sums_the_insight_rows_and_pins_team_actions(client):
    d = client.get("/api/dashboard").json()
    assert d["ready"] and (d["start"], d["end"]) == (1, 7)
    kpis = {k["key"]: k for k in d["kpis"]}
    series_spend = round(sum(s["spend"] for s in d["series"]), 2)
    assert kpis["spend"]["value"] == pytest.approx(series_spend)
    assert kpis["results"]["value"] == sum(s["results"] for s in d["series"])
    assert kpis["cpr"]["hero"] and kpis["cpr"]["tone"] == "good"
    assert d["evidence"]["rows"] == 28 and d["evidence"]["first"] == "ev-d1-ad_1"
    flag, change = d["actions"][0], d["actions"][1]
    assert (flag["day"], flag["who"]["name"], flag["verb"]) == (3, "Analyst", "flagged")
    assert "ev-d3-ad_1-ctr" in flag["evidence"]
    assert change["who"]["name"] == "Operator" and change["link"]["key"] == "day3/change_set"
    assert "paused Streak" in change["text"] and "simulated owner" in change["text"]
    assert d["after"].startswith("Cost per trial was")
    streak = next(a for c in d["campaigns"] for a in c["ads"] if a["name"] == "Streak")
    assert streak["status_label"] == "Paused day 3"
    assert client.get("/api/dashboard?days=14").json()["short"] is True


def test_assets_trace_each_creative_and_launch(client):
    assert [a["kind"] for a in client.get("/api/assets").json()["items"]] == []
    client.post("/api/approve/week1/test_plan")
    _idle(client)
    items = client.get("/api/assets").json()["items"]
    made = [a for a in items if a["kind"] == "made"]
    assert len(made) == 2 and all(a["status_label"] == "In change set" for a in made)
    steps = [s["title"] for s in made[0]["steps"]]
    assert steps[1] == "Cut out the phone" and steps[-1].startswith("Reviewer passed")
    assert made[0]["steps"][1]["model"]["model"] == "SAM 3.1"
    assert made[0]["steps"][3]["model"]["model"] == "Muse Image"
    assert made[0]["image"].startswith("/api/media/week1/")
    assert any(a["kind"] == "source" for a in items)
    client.post("/api/approve/week1/change_set")
    _idle(client)
    made = [a for a in client.get("/api/assets").json()["items"] if a["kind"] == "made"]
    assert {a["status_label"] for a in made} == {"Created paused"}
    assert all(dict(a["facts"])["Ad"].startswith("ad_") for a in made)
    ops = client.get("/api/dashboard").json()["actions"]
    assert "created 2 paused ads" in ops[-1]["text"].lower() and "Jieyi approved" in ops[-1]["text"]


def test_upload_accepts_images_only(client):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    r = client.post("/api/assets/upload?name=../../shot.jpg", content=buf.getvalue())
    assert r.status_code == 200
    up = next(a for a in client.get("/api/assets").json()["items"] if a["kind"] == "upload")
    assert up["title"] == "shot.jpg" and up["after"] == "40 x 30"
    assert client.get(up["image"]).status_code == 200
    assert client.post("/api/assets/upload", content=b"<svg/>").status_code == 415
    assert client.post("/api/assets/upload", content=b"").status_code == 400


def test_first_run_connects_the_sandbox_with_a_level_and_rules(client):
    a = client.get("/api/access").json()
    assert a["connection"] is None and a["level"] == 2
    assert a["meta_login"]  # no Meta app configured in tests
    r = client.post("/api/access/connect",
                    json={"kind": "sandbox", "level": 1, "rules": {"daily_budget_cap": 80}})
    assert r.status_code == 200
    a = client.get("/api/access").json()
    assert a["connection"]["account_id"] == "act_sandbox_0001"
    assert a["level"] == 1 and a["rules"]["daily_budget_cap"] == 80
    assert a["rules"]["preset"] == "custom" and a["fact_version"] == 2
    texts = [i["text"] for i in client.get("/api/room").json()["items"] if i["kind"] == "msg"]
    assert any(t.startswith("Set the team to L1 Propose") for t in texts)
    assert any(t.startswith("Changed house rules (facts version 2)") for t in texts)
    # A real account cannot be connected without the Meta login.
    assert client.post("/api/access/connect", json={"kind": "meta", "account_id": "act_1"}
                       ).status_code == 409
    assert client.get("/api/meta/login", follow_redirects=False).status_code == 409


def test_house_rules_reject_values_out_of_range(client):
    assert client.post("/api/house-rules", json={"daily_budget_cap": -5}).status_code == 422
    assert client.post("/api/house-rules", json={"max_budget_change_pct": 3}).status_code == 422
    assert client.post("/api/house-rules", json={"anything": 1}).status_code == 422
    r = client.post("/api/house-rules", json={"preset": "conservative"}).json()
    rules = client.get("/api/access").json()["rules"]
    assert r["fact_version"] == 2 and rules["preset"] == "conservative"
    assert rules["max_budget_change_pct"] == 0.10 and rules["daily_budget_cap"] == 60


def test_levels_gate_approvals_and_the_operator(client):
    client.post("/api/access/level", json={"level": 0})
    assert client.post("/api/approve/week1/test_plan").status_code == 409
    assert client.post("/api/run/week").status_code == 409
    client.post("/api/access/level", json={"level": 1})
    assert client.post("/api/approve/week1/test_plan").status_code == 200
    _idle(client)
    r = client.post("/api/approve/week1/change_set").json()
    assert r["handed_off"]
    _idle(client)
    detail = client.get("/api/deliverables/week1/change_set").json()
    assert detail["status"] == "ok"  # approved, not applied
    room = client.get("/api/room").json()["items"]
    assert any(i["kind"] == "msg" and i["who"]["actor"] == "operator"
               and i["text"].startswith("Nothing written: the team is at L1 Propose") for i in room)


def test_operator_refuses_below_l2_even_with_an_approval(tmp_path):
    from pgt.harness import access
    from pgt.roles.operator import apply_change_set

    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    with TestClient(create_app(team)) as c:
        _idle(c)
        key = team.ws.events.list("approval.granted")[0].payload["key"]  # a simulated day
        access.set_level(team.ws, 1)
        with pytest.raises(PermissionError, match="L1 Propose"):
            apply_change_set(team.ws, team.account, key)
        with pytest.raises(PermissionError):
            access.set_level(team.ws, 2, actor="operator")
    team.ws.close()
