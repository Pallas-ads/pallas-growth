"""FastAPI app over the workspace: read models, server-sent events, and the owner's actions.

Everything runs on one event loop, so the workspace connection is only used from one thread at a
time. Approvals go through `Approvals.grant`; the next stage then runs in the background.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from pgt.ads import meta_login
from pgt.api import board, views
from pgt.harness import access
from pgt.harness.workspace import Workspace
from pgt.schemas import ChangeSet
from pgt.schemas.settings import PRESETS, HouseRules


@dataclass
class Team:
    ws: Workspace
    model: object
    media: object
    media_dir: str
    account: object = None
    fake: object = None
    meter: object = None  # CostMeter shared by every loop; live runs carry a total cap
    mode: str = "offline"  # "offline", "live" (Meta API) or "mixed" (Baseten + Meta API)
    busy: str | None = None  # what the team is doing right now, if anything
    error: str | None = None
    tasks: set = field(default_factory=set)
    public: bool = False  # the hosted demo: sandbox only, no real Meta login
    meta: dict = field(default_factory=dict)  # login state, token and granted accounts; memory only

    def run(self, label: str, coro) -> None:
        if self.busy:
            coro.close()
            raise HTTPException(409, f"The team is busy: {self.busy}")
        self.busy, self.error = label, None

        async def wrapper():
            try:
                await coro
            except Exception as exc:  # noqa: BLE001 - shown in the UI; the event log has the rest
                self.error = f"{label} failed: {exc}"
            finally:
                self.busy = None

        task = asyncio.create_task(wrapper())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


async def seed(team: Team, days: int = 7, seed: int = 7) -> None:
    """Simulate a week of daily checks, then plan the weekly review up to the plan approval."""
    from pgt.loops.simulate import simulate
    from pgt.loops.weekly import plan_week

    sim = await simulate(days, team.model, team.ws, seed=seed, meter=team.meter)
    team.account = sim.account
    await plan_week(team.ws, team.account, team.model, meter=team.meter)


def offline_team(db: str, media_dir: str) -> Team:
    from pgt.harness.policy import CostMeter
    from pgt.llm.media import MediaClient
    from pgt.llm.offline import offline_model

    model, fake = offline_model("muse-spark-1.3")
    ws = Workspace(db)
    return Team(ws, model, MediaClient(fake.client()), media_dir, fake=fake,
                meter=CostMeter(ws.events))


def live_team(
    db: str, media_dir: str, model_name: str = "muse-spark-1.3", provider: str = "meta",
    effort: str = "medium", cap: float = 5.0,
) -> Team:
    """A team on the real models. Every paid call is checked against `cap` (US dollars, at list
    price, for the whole workspace) before it is sent. Keys come from the environment or .env."""
    from dotenv import load_dotenv

    from pgt.harness.policy import CostMeter
    from pgt.llm.media import MediaClient
    from pgt.llm.meta import meta_client, responses_model

    load_dotenv()
    client = meta_client()
    model = responses_model(client, model_name)
    if provider == "mixed":
        from pgt.llm.baseten import baseten_client, chat_model
        from pgt.llm.routing import mixed

        model = mixed(model, chat_model(baseten_client(), effort=effort))
    ws = Workspace(db)
    return Team(ws, model, MediaClient(client), media_dir, meter=CostMeter(ws.events, total=cap),
                mode="mixed" if provider == "mixed" else "live")


class Message(BaseModel):
    text: str
    to: str | None = None
    thread: int | None = None


class Note(BaseModel):
    note: str = ""


class Rules(BaseModel):
    """A change to the house rules. Missing fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["conservative", "balanced", "aggressive"] | None = None
    daily_budget_cap: float | None = Field(None, gt=0, le=100_000)
    max_budget_change_pct: float | None = Field(None, gt=0, le=1)
    min_days_between_budget_changes: int | None = Field(None, ge=0, le=30)
    min_spend_multiple_before_pause: float | None = Field(None, ge=0, le=10)
    protect_learning_phase: bool | None = None


class Connect(BaseModel):
    kind: Literal["sandbox", "meta"] = "sandbox"
    account_id: str | None = None
    level: int = Field(access.DEFAULT_LEVEL, ge=0, le=2)
    rules: Rules | None = None


class Level(BaseModel):
    level: int = Field(ge=0, le=2)


MAX_UPLOAD = 5 * 1024 * 1024


def create_app(team: Team, web_dist: str | Path | None = None, seed_days: int | None = 7) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if seed_days and not team.ws.events.list("loop.started"):
            team.run("Setting up the demo week", seed(team, seed_days))
        yield
        for task in list(team.tasks):
            task.cancel()

    app = FastAPI(title="Pallas Growth Team", lifespan=lifespan)
    app.state.team = team

    def _record(key: str):
        record = team.ws.artifacts.latest(key)
        if record is None:
            raise HTTPException(404, f"No deliverable {key}")
        return record

    @app.get("/api/status")
    async def status():
        last = team.ws.events.list(since_id=0)[-1:] or []
        return {"busy": team.busy, "error": team.error, "where": views.where(team.ws),
                "day": team.account.day if team.account else None,
                "last_event_id": last[0].id if last else 0, "owner": views.OWNER,
                "mode": team.mode}

    @app.get("/api/stream")
    async def stream():
        async def gen():
            last, busy = -1, object()
            while True:
                tail = team.ws.events.list(since_id=max(last, 0))
                newest = tail[-1].id if tail else max(last, 0)
                if newest != last or team.busy != busy:
                    last, busy = newest, team.busy
                    yield f"data: {json.dumps({'last_event_id': last, 'busy': busy})}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/api/room")
    async def room():
        idx = views.Index.build(team.ws)
        return {"where": views.where(team.ws), "items": views.room(team.ws, team.account, idx)}

    @app.get("/api/deliverables")
    async def deliverables():
        return {"items": views.deliverables(team.ws, team.account)}

    @app.get("/api/deliverables/{key:path}")
    async def deliverable(key: str):
        d = views.detail(team.ws, key, team.account, team.media_dir)
        if d is None:
            raise HTTPException(404, f"No deliverable {key}")
        return d

    @app.post("/api/approve/{key:path}")
    async def approve(key: str):
        from pgt.loops.weekly import apply_week, produce_week
        from pgt.roles.operator import apply_change_set

        record = _record(key)
        idx = views.Index.build(team.ws)
        if idx.waiting(record) is None:
            raise HTTPException(409, "This version is not waiting on you")
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}")
        if not access.can_propose(team.ws):
            raise HTTPException(409, "The team is set to L0 Observe and acts on nothing. Raise "
                                     "the level in Settings to approve proposals.")
        approved = None
        if record.kind == "ChangeSet" and record.content.get("changes"):
            approved = [g.change_index for g in ChangeSet.model_validate(record.content).gate
                        if g.allowed]
        try:
            team.ws.approvals.grant(key, record, approved=approved)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        period = key.split("/")[0]
        if key.endswith("/change_set") and not access.can_apply(team.ws):
            content = record.content
            team.ws.events.append(
                "changeset.handed_off", "operator",
                {"key": key, "changes": len(approved or []),
                 "ads": len(content.get("ad_drafts") or [])},
                refs=[record.artifact_id], sim_day=team.account.day if team.account else None)
            return {"ok": True, "handed_off": True}
        if key.endswith("/test_plan") and period.startswith("week"):
            week = int(period[4:])
            team.run("Producing creative", produce_week(
                team.ws, team.account, team.model, team.media, team.media_dir, week=week,
                meter=team.meter))
        elif key.endswith("/change_set") and period.startswith("week"):
            week = int(period[4:])

            async def apply():
                apply_week(team.ws, team.account, week=week)

            team.run("Applying the change set", apply())
        elif key.endswith("/change_set"):
            async def apply_day():
                apply_change_set(team.ws, team.account, key)

            team.run("Applying the change set", apply_day())
        return {"ok": True}

    @app.post("/api/send-back/{key:path}")
    async def send_back(key: str, body: Note):
        record = _record(key)
        if views.Index.build(team.ws).waiting(record) is None:
            raise HTTPException(409, "This version is not waiting on you")
        team.ws.events.append("approval.returned", "user", {"key": key, "note": body.note.strip()},
                              refs=[record.artifact_id], sim_day=record.sim_day)
        return {"ok": True}

    @app.post("/api/messages")
    async def post_message(body: Message):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Empty message")
        day = team.account.day if team.account else None
        e = team.ws.events.append("message.posted", "user",
                                  {"text": text, "to": body.to, "thread": body.thread},
                                  sim_day=day)
        return {"id": e.id}

    @app.post("/api/run/day")
    async def run_day():
        """Advance the sandbox one day and run the daily check. Changes wait on the owner."""
        from pgt.loops.daily import run_daily_check

        if team.account is None:
            raise HTTPException(409, "The demo week is still being set up")

        async def day():
            team.account.advance_day()
            brief = await run_daily_check(team.ws, team.account, team.model, team.meter)
            key = f"day{team.account.day}/change_set"
            record = team.ws.artifacts.latest(key)
            if (record and access.can_propose(team.ws)
                    and brief.review and brief.review.verdict == "pass"
                    and any(g.allowed for g in ChangeSet.model_validate(record.content).gate)):
                team.ws.events.append("approval.requested", "media_buyer",
                                      {"key": key, "what": "change set"},
                                      refs=[record.artifact_id], sim_day=team.account.day,
                                      loop_id=f"daily-d{team.account.day}")

        team.run(f"Daily check, day {team.account.day + 1}", day())
        return {"ok": True}

    @app.post("/api/run/week")
    async def run_week():
        from pgt.loops.weekly import plan_week

        if team.account is None:
            raise HTTPException(409, "The demo week is still being set up")
        if not access.can_propose(team.ws):
            raise HTTPException(409, "The team is set to L0 Observe, so it does not plan tests. "
                                     "Raise the level in Settings first.")
        team.run("Planning the week",
                 plan_week(team.ws, team.account, team.model, meter=team.meter))
        return {"ok": True}

    @app.get("/api/settings")
    async def settings():
        calls = team.ws.events.list("model.call")
        cap = getattr(team.meter, "total", None)
        return {
            "facts": team.ws.facts.current().model_dump(mode="json"),
            "fact_version": team.ws.facts.version,
            "model_calls": len(calls),
            "tokens": sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls),
            "cost": round(sum(e.payload["cost"] for e in calls), 4),
            "offline": team.fake is not None,
            "mode": team.mode,
            "usage": views.usage(team.ws),
            "cap": cap,
            "remaining": None if cap is None else round(team.meter.remaining(), 4),
            "day": team.account.day if team.account else None,
            "now": datetime.now(UTC).isoformat(),
        }

    def _day():
        return team.account.day if team.account else None

    @app.get("/api/access")
    async def get_access():
        facts = team.ws.facts.current()
        return {
            "connection": access.connection(team.ws),
            "level": access.level(team.ws),
            "levels": access.LEVELS,
            "sandbox": access.SANDBOX,
            "public": team.public,
            "meta_login": meta_login.unavailable(team.public),
            "meta_accounts": team.meta.get("accounts", []),
            "rules": facts.house_rules.model_dump(mode="json"),
            "presets": PRESETS,
            "weekly_budget": facts.weekly_budget,
            "weekly_test_budget": round(facts.weekly_budget * facts.test_budget_share, 2),
            "constraints": facts.constraints,
            "fact_version": team.ws.facts.version,
        }

    def _set_rules(body: Rules) -> int:
        if team.busy:
            raise HTTPException(409, f"The team is busy: {team.busy}. Change the rules after.")
        facts = team.ws.facts.current()
        values = facts.house_rules.model_dump()
        if body.preset:
            values.update(PRESETS[body.preset], preset=body.preset)
        fields = body.model_dump(exclude_none=True, exclude={"preset"})
        if fields:
            values.update(fields, preset="custom")
        rules = HouseRules(**values)
        if rules == facts.house_rules:
            return team.ws.facts.version
        return team.ws.facts.set(facts.model_copy(update={"house_rules": rules}), "user")

    @app.post("/api/house-rules")
    async def house_rules(body: Rules):
        return {"ok": True, "fact_version": _set_rules(body)}

    @app.post("/api/access/connect")
    async def connect(body: Connect):
        if body.kind == "sandbox":
            account = dict(access.SANDBOX)
        else:
            reason = meta_login.unavailable(team.public)
            if reason:
                raise HTTPException(409, reason)
            granted = {a["account_id"]: a for a in team.meta.get("accounts", [])}
            if body.account_id not in granted:
                raise HTTPException(409, "Sign in with Facebook and grant this ad account first")
            account = granted[body.account_id]
        try:
            access.connect(team.ws, account, body.level, sim_day=_day())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if body.rules:
            _set_rules(body.rules)
        return {"ok": True}

    @app.post("/api/access/level")
    async def set_level(body: Level):
        current = access.connection(team.ws)
        if current and current.get("kind") == "meta" and body.level > access.PROPOSE:
            raise HTTPException(409, "A real Meta ad account is read-only in this build (L0 or L1)")
        access.set_level(team.ws, body.level, sim_day=_day())
        return {"ok": True, "level": body.level}

    @app.post("/api/access/disconnect")
    async def disconnect():
        access.disconnect(team.ws, sim_day=_day())
        team.meta.clear()
        return {"ok": True}

    @app.get("/api/meta/login")
    async def meta_start(request: Request):
        """Send the owner to Facebook Login for Business on facebook.com. Local installs only."""
        reason = meta_login.unavailable(team.public)
        if reason:
            raise HTTPException(409, reason)
        state = meta_login.new_state()
        redirect = str(request.url_for("meta_callback"))
        team.meta = {"state": state, "redirect": redirect}
        return RedirectResponse(meta_login.login_url(redirect, state), status_code=302)

    @app.get("/api/meta/callback", name="meta_callback")
    async def meta_callback(code: str = "", state: str = "", error: str = ""):
        expected = team.meta.get("state")
        if error or not code:
            team.meta.clear()
            return RedirectResponse("/#/settings?meta=cancelled", status_code=302)
        if not expected or not secrets.compare_digest(state, expected):
            team.meta.clear()
            raise HTTPException(400, "Login state does not match. Start again from Settings.")
        try:
            token = await asyncio.to_thread(meta_login.exchange, code, team.meta["redirect"])
            accounts = await asyncio.to_thread(meta_login.ad_accounts, token)
        except Exception:  # noqa: BLE001 - the token and Meta's reply are not shown or logged
            team.meta.clear()
            return RedirectResponse("/#/settings?meta=failed", status_code=302)
        team.meta = {"token": token, "accounts": accounts}
        return RedirectResponse("/#/settings?meta=pick", status_code=302)

    @app.get("/api/dashboard")
    async def dashboard(days: int = 7):
        return board.dashboard(team.ws, team.account, days)

    @app.get("/api/assets")
    async def assets():
        return board.assets(team.ws, team.account, team.media_dir)

    @app.post("/api/assets/upload")
    async def upload(request: Request, name: str = "upload.png"):
        """A product photo from the owner. Checked as an image, re-saved as PNG, never trusted."""
        import hashlib
        import io
        import re

        from PIL import Image, UnidentifiedImageError

        body = await request.body()
        if not body:
            raise HTTPException(400, "Empty upload")
        if len(body) > MAX_UPLOAD:
            raise HTTPException(413, "Images up to 5 MB")
        try:
            img = Image.open(io.BytesIO(body))
            if img.format not in ("PNG", "JPEG"):
                raise HTTPException(415, "PNG or JPEG only")
            if img.width * img.height > 40_000_000:
                raise HTTPException(413, "Image too large")
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise HTTPException(415, "Not an image") from exc
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        out = io.BytesIO()
        img.save(out, "PNG")
        data = out.getvalue()
        digest = hashlib.sha256(data).hexdigest()[:12]
        folder = Path(team.media_dir) / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.png"
        path.write_bytes(data)
        clean = re.sub(r"[^A-Za-z0-9 ._-]", "", Path(name).name)[:80] or "upload.png"
        sim_day = team.account.day if team.account else None
        team.ws.events.append("asset.uploaded", "user", {
            "file": path.name, "path": str(path), "name": clean,
            "width": img.width, "height": img.height, "sha256": digest,
        }, sim_day=sim_day)
        return {"ok": True, "id": f"upload/{path.name}"}

    @app.get("/api/media/{rel:path}")
    async def media(rel: str):
        root = Path(team.media_dir).resolve()
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    if web_dist and Path(web_dist).is_dir():
        dist = Path(web_dist)
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            file = (dist / path).resolve()
            if path and file.is_relative_to(dist.resolve()) and file.is_file():
                return FileResponse(file)
            return FileResponse(dist / "index.html")

    return app
