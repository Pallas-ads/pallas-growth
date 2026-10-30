"""Read models for the web app, derived from the event log and the artifact store.

Nothing here writes. The room is conversation; Deliverables lists every output with one status.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pgt.harness.artifacts import ArtifactRecord
from pgt.harness.events import Event
from pgt.harness.workspace import Workspace

OWNER = "Jieyi"

# role id -> (avatar code, role name, persona name)
ROLES: dict[str, tuple[str | None, str, str]] = {
    "analyst": ("AN", "Analyst", "Tally"),
    "growth_lead": ("GL", "Growth Lead", "Compass"),
    "strategist": ("CS", "Creative Strategist", "Scout"),
    "producer": ("CP", "Creative Producer", "Pixel"),
    "reviewer": ("RV", "Reviewer", "Philly"),
    "media_buyer": ("MB", "Media Buyer", "Dial"),
    "operator": ("OP", "Operator", "Bolt"),
    "user": ("YOU", OWNER, OWNER),
    "system": (None, "System", "System"),
    "policy": (None, "House rules", "House rules"),
}

# artifact kind -> (type label, Deliverables tab)
TYPES = {
    "TestPlan": ("Test plan", "plans"),
    "ChangeSet": ("Change set", "changes"),
    "CreativeSet": ("Creative", "creative"),
    "Findings": ("Findings", "findings"),
    "MarketResearch": ("Research", "findings"),
    "TestReadout": ("Readout", "findings"),
}

HIDDEN = {
    "model.call", "model.cost", "tool.call", "context.assembled", "evidence.added",
    "loop.finished", "loop.stage", "artifact.status", "fact.changed", "approval.simulated",
    "approval.voided", "media.call",
}


def role(actor: str) -> dict:
    code, name, persona = ROLES.get(actor, (None, actor.replace("_", " ").title(), actor))
    return {"actor": actor, "code": code, "name": name, "persona": persona}


def period(key: str) -> str:
    head = key.split("/")[0]
    for prefix, label in (("week", "Week"), ("day", "Day")):
        if head.startswith(prefix) and head[len(prefix):].isdigit():
            return f"{label} {head[len(prefix):]}"
    return head


def title(record: ArtifactRecord) -> str:
    """Names follow `{Type} · {Period} [· {Subject}] · v{n}`."""
    parts = record.key.split("/")
    label = TYPES.get(record.kind, (record.kind, ""))[0]
    subject = [parts[-1]] if len(parts) > 2 else []
    return " · ".join([label, period(record.key), *subject, f"v{record.version}"])


def is_empty_change_set(record: ArtifactRecord) -> bool:
    c = record.content
    return record.kind == "ChangeSet" and not c.get("changes") and not c.get("ad_drafts")


def _money(value: float) -> str:
    return f"${value:,.2f}".replace(".00", "")


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


@dataclass
class Index:
    """One pass over the event log, shared by the room and the Deliverables views."""

    events: list[Event]
    published: dict[str, Event] = field(default_factory=dict)  # artifact_id -> publish event
    requested: dict[str, Event] = field(default_factory=dict)  # key -> latest request
    grants: dict[str, list[Event]] = field(default_factory=dict)
    voided: set[int] = field(default_factory=set)
    applied: dict[str, Event] = field(default_factory=dict)  # artifact_id -> applied event
    returned: dict[str, Event] = field(default_factory=dict)  # key -> latest send-back
    simulated: set[int] = field(default_factory=set)  # grant ids that a simulated user made

    @classmethod
    def build(cls, ws: Workspace) -> Index:
        idx = cls(ws.events.list())
        last_simulated: dict[str, int] = {}
        for e in idx.events:
            p = e.payload
            if e.type == "artifact.published":
                idx.published[p["artifact_id"]] = e
            elif e.type == "approval.requested":
                idx.requested[p["key"]] = e
            elif e.type == "approval.simulated":
                last_simulated[p["key"]] = e.id
            elif e.type == "approval.granted":
                idx.grants.setdefault(p["key"], []).append(e)
                if p["key"] in last_simulated:
                    idx.simulated.add(e.id)
                    del last_simulated[p["key"]]
            elif e.type == "approval.voided":
                idx.voided.add(p["approval_id"])
            elif e.type == "changeset.applied" and e.refs:
                idx.applied[e.refs[0]] = e
            elif e.type == "approval.returned":
                idx.returned[p["key"]] = e
        return idx

    def valid_grant(self, record: ArtifactRecord) -> Event | None:
        grants = self.grants.get(record.key, [])
        if not grants:
            return None
        g = grants[-1]
        if g.id in self.voided or g.payload["content_hash"] != record.content_hash:
            return None
        return g

    def waiting(self, record: ArtifactRecord) -> Event | None:
        """The open request on this exact version, if the user still has to act on it."""
        req = self.requested.get(record.key)
        if req is None or record.artifact_id not in req.refs:
            return None
        later = [g for g in self.grants.get(record.key, []) if g.id > req.id]
        back = self.returned.get(record.key)
        if later or (back and back.id > req.id):
            return None
        return req

    def status(self, record: ArtifactRecord) -> tuple[str, str]:
        if record.status in ("stale", "superseded"):
            return "stale", "Stale"
        if record.artifact_id in self.applied:
            return "applied", "Applied"
        if self.valid_grant(record):
            return "ok", "Approved"
        if self.waiting(record):
            return "wait", "Waiting on you"
        if record.status == "draft":
            return "draft", "Draft"
        return "review", "In review"


def campaign_names(account) -> dict[str, str]:
    if account is None:
        return {}
    return {c.campaign_id: c.name for c in account.state().campaigns}


def subtitle(ws: Workspace, record: ArtifactRecord, names: dict[str, str]) -> str:
    c = record.content
    if record.kind == "TestPlan":
        tests = c.get("tests", [])
        return (f"{_plural(len(tests), 'test')}, {_money(c.get('available_test_budget', 0))} "
                "test budget for the week")
    if record.kind == "ChangeSet":
        drafts, changes = c.get("ad_drafts", []), c.get("changes", [])
        if drafts:
            camps = sorted({names.get(d["campaign_id"], d["campaign_id"]) for d in drafts})
            return f"{_plural(len(drafts), 'new ad')}, created paused in {', '.join(camps)}"
        blocked = sum(1 for g in c.get("gate", []) if not g.get("allowed", True))
        text = _plural(len(changes), "change")
        return text + (f", {blocked} blocked by house rules" if blocked else "")
    if record.kind == "CreativeSet":
        return f'"{c.get("ad_copy", {}).get("headline", "")}"'
    if record.kind == "Findings":
        items = c.get("findings", [])
        act = sum(1 for f in items if f.get("severity") == "act")
        return f"{_plural(len(items), 'finding')}, {act} to act on"
    if record.kind == "MarketResearch":
        insights = c.get("insights", [])
        sources = sum(len(i.get("sources", [])) for i in insights)
        return f"{_plural(len(insights), 'insight')} from {_plural(sources, 'source')}"
    if record.kind == "TestReadout":
        return f"{c.get('recommendation', '').capitalize()}: {c.get('rationale', '')}"
    return ""


def deliverable(ws: Workspace, idx: Index, record: ArtifactRecord, names: dict[str, str]) -> dict:
    cls, label = idx.status(record)
    pub = idx.published.get(record.artifact_id)
    req = idx.requested.get(record.key)
    anchor = req if req and record.artifact_id in req.refs else pub
    return {
        "key": record.key,
        "artifact_id": record.artifact_id,
        "kind": record.kind,
        "tab": TYPES[record.kind][1],
        "title": title(record),
        "sub": subtitle(ws, record, names),
        "from": role(record.author),
        "status": cls,
        "status_label": label,
        "updated": pub.ts if pub else None,
        "sim_day": record.sim_day,
        "discussed": {"event_id": anchor.id, "sim_day": anchor.sim_day, "ts": anchor.ts}
        if anchor else None,
    }


def deliverables(ws: Workspace, account=None, idx: Index | None = None) -> list[dict]:
    idx = idx or Index.build(ws)
    names = campaign_names(account)
    latest: dict[str, ArtifactRecord] = {}
    for record in ws.artifacts.list():
        if record.kind in TYPES:
            latest[record.key] = record  # list() is in publish order, so the last one wins
    items = [deliverable(ws, idx, r, names) for r in latest.values() if not is_empty_change_set(r)]
    return sorted(items, key=lambda d: d["updated"] or "", reverse=True)


# ---------------------------------------------------------------- room


def _msg(e: Event, actor: str, text: str, mention: bool = False, card: dict | None = None) -> dict:
    return {"kind": "msg", "id": e.id, "ts": e.ts, "sim_day": e.sim_day, "who": role(actor),
            "text": (f"@{OWNER} " if mention else "") + text, "mention": mention,
            "card": card, "replies": []}


def _card(ws: Workspace, idx: Index, record: ArtifactRecord, names: dict[str, str]) -> dict:
    d = deliverable(ws, idx, record, names)
    note = ""
    if d["status"] == "ok":
        g = idx.valid_grant(record)
        note = "Approved by the simulated owner" if g.id in idx.simulated else f"Approved by {OWNER}"
        d["approved_ts"] = g.ts
    elif d["status"] == "applied":
        r = idx.applied[record.artifact_id].payload
        created = r.get("created_ad_ids") or []
        note = (f"Created {', '.join(created)}, all paused" if created
                else f"Applied {_plural(len(r.get('applied', [])), 'change')}")
    d["note"] = note
    return d


def _fold_line(ws: Workspace, e: Event) -> tuple[str, str] | None:
    """(actor, text) for one step the team took between themselves, or None to hide it."""
    p = e.payload
    if e.type == "task.handoff":
        if p.get("from") == "system":
            return "growth_lead", p["task"]
        return p["from"], f"To {role(p['to'])['name']}: {p['task']}"
    if e.type == "question.asked":
        return p["asker"], f"To {role(p['to'])['name']}: {p['question']}"
    if e.type == "question.answered":
        return "analyst", p["answer"]
    if e.type == "review.redo":
        return p.get("by", "reviewer"), f"Sent {p['subject']} back: " + "; ".join(p["issues"])
    if e.type == "policy.blocked":
        return "policy", (f"Blocked change {p['change_index'] + 1} of {p['change_set_id']}: "
                          + ", ".join(r.replace("_", " ") for r in p["rules"]))
    if e.type in ("stage.refused", "question.refused"):
        return e.actor, p.get("reason") or p.get("question") or e.type
    if e.type == "fact.proposed":
        return e.actor, f"Proposed a change to {p.get('key')}: {p.get('reason', '')}"
    if e.type == "artifact.published":
        record = ws.artifacts.get(p["artifact_id"])
        if record is None:
            return None
        c = record.content
        if record.kind == "ReviewVerdict":
            issues = c.get("issues") or []
            return "reviewer", "Checked today's change set: " + (
                "; ".join(issues) if issues else c.get("verdict", "pass"))
        if record.kind == "CreativeReview":
            issues = c.get("issues") or []
            return "reviewer", f"Reviewed {record.key.split('/')[-1]}: " + (
                "; ".join(issues) if issues else "pass")
        if record.kind == "WeeklyBrief":
            return record.author, c.get("summary") or "Wrote the brief for the week."
        if record.kind in TYPES:
            if is_empty_change_set(record):
                return record.author, "No changes needed today."
            return record.author, f"Posted {title(record)}: {subtitle(ws, record, {})}"
        return None
    return None


def _fold(lines: list[dict], loop_id: str | None) -> dict:
    actors: list[str] = []
    for line in lines:
        if line["who"]["actor"] not in actors and line["who"]["code"]:
            actors.append(line["who"]["actor"])
    names = [role(a)["name"] for a in actors]
    if loop_id and loop_id.startswith("daily-d"):
        tail = f"prepared the Day {loop_id[7:]} check"
    elif loop_id and loop_id.startswith("weekly-w"):
        tail = f"worked on the Week {loop_id[8:]} review"
    elif lines and all(line["type"] == "task.handoff" for line in lines):
        tail = "handed out the work"
    elif lines and all(line["type"].startswith("question.") for line in lines):
        tail = "traded questions and answers"
    else:
        tail = f"posted {_plural(len(lines), 'update')}"
    return {"kind": "fold", "id": lines[0]["id"], "ids": [line["id"] for line in lines],
            "actors": [role(a) for a in actors], "names": names, "tail": tail,
            "ts_from": lines[0]["ts"], "ts_to": lines[-1]["ts"], "sim_day": lines[0]["sim_day"],
            "items": lines}


def room(ws: Workspace, account=None, idx: Index | None = None) -> list[dict]:
    idx = idx or Index.build(ws)
    names = campaign_names(account)
    asked = {ref for e in idx.events if e.type == "approval.requested" for ref in e.refs}
    items: list[dict] = []
    roots: dict[str, dict] = {}  # key -> message that asked for approval
    by_id: dict[int, dict] = {}
    fold: list[dict] = []
    fold_loop: str | None = None

    def flush():
        nonlocal fold
        if fold:
            items.append(_fold(fold, fold_loop))
            fold = []

    def add(item: dict):
        flush()
        items.append(item)
        by_id[item["id"]] = item

    def reply(root: dict | None, e: Event, actor: str, text: str):
        r = {"id": e.id, "ts": e.ts, "who": role(actor), "text": text}
        if root is None:
            add(_msg(e, actor, text))
        else:
            root["replies"].append(r)

    for e in idx.events:
        p = e.payload
        if e.type in HIDDEN:
            continue
        if e.type == "loop.started":
            flush()
            fold_loop = e.loop_id
            if p.get("loop") == "weekly_review":
                label = f"Day {e.sim_day} · weekly review · Week {p.get('week')}"
            else:
                label = f"Day {e.sim_day} · daily check"
            items.append({"kind": "day", "id": e.id, "label": label, "sim_day": e.sim_day})
            continue
        if e.type == "artifact.published":
            record = ws.artifacts.get(p["artifact_id"])
            if record is None or record.artifact_id in asked:
                continue
            if record.kind == "DailyBrief":
                c = record.content
                text = c.get("headline", "")
                if c.get("blocked"):
                    text += " " + " ".join(c["blocked"])
                cs = ws.artifacts.latest(f"day{c.get('day')}/change_set")
                card = None
                if cs is not None and not is_empty_change_set(cs):
                    card = _card(ws, idx, cs, names)
                add(_msg(e, record.author, text, card=card))
                continue
        if e.type == "approval.requested":
            record = ws.artifacts.get(e.refs[0]) if e.refs else ws.artifacts.latest(p["key"])
            if record is None:
                continue
            what = p.get("what", "deliverable")
            verb = "are" if what.endswith("s") else "is"
            text = f"The {period(record.key)} {what} {verb} ready. "
            text += record.content.get("summary") or subtitle(ws, record, names)
            if record.kind == "ChangeSet":
                text += " Nothing reaches the account until you approve."
            msg = _msg(e, record.author, text.strip(), mention=True,
                       card=_card(ws, idx, record, names))
            add(msg)
            roots[p["key"]] = msg
            continue
        if e.type == "approval.granted":
            record = ws.artifacts.get(e.refs[0]) if e.refs else None
            name = title(record) if record else p["key"]
            if e.id in idx.simulated:
                n = len(p.get("approved", []))
                add(_msg(e, "system", f"Simulated approval: approved {_plural(n, 'change')} in "
                         f"{name}, only the ones the house rules allowed."))
            else:
                reply(roots.get(p["key"]), e, "user", f"Approved {name}.")
            continue
        if e.type == "approval.returned":
            reply(roots.get(p["key"]), e, "user", f"Sent back: {p.get('note', '')}".strip())
            continue
        if e.type == "changeset.applied":
            record = ws.artifacts.get(e.refs[0]) if e.refs else None
            created = p.get("created_ad_ids") or []
            if created:
                text = f"Created {', '.join(created)} in the sandbox, all paused."
            else:
                text = f"Applied {_plural(len(p.get('applied', [])), 'change')} to the sandbox."
                if p.get("skipped"):
                    text += f" Skipped {_plural(len(p['skipped']), 'change')}."
            add(_msg(e, "operator", text, card=_card(ws, idx, record, names) if record else None))
            continue
        if e.type == "changeset.refused":
            add(_msg(e, "operator", p.get("reason") or "Refused to apply without a valid approval."))
            continue
        if e.type == "escalation.raised":
            add(_msg(e, e.actor, p.get("reason", ""), mention=True))
            continue
        if e.type == "message.posted":
            thread = p.get("thread")
            text = (f"@{role(p['to'])['name']} " if p.get("to") else "") + p["text"]
            if thread and thread in by_id:
                by_id[thread]["replies"].append(
                    {"id": e.id, "ts": e.ts, "who": role(e.actor), "text": text})
            else:
                add(_msg(e, e.actor, text))
            continue
        line = _fold_line(ws, e)
        if line is None:
            continue
        if fold and fold_loop != e.loop_id and e.loop_id is not None:
            flush()
        fold.append({"id": e.id, "ts": e.ts, "sim_day": e.sim_day, "type": e.type,
                     "who": role(line[0]), "text": line[1]})
    flush()
    return items


def where(ws: Workspace) -> str:
    """Eyebrow for the room: the loop the team is in."""
    started = ws.events.list("loop.started")
    if not started:
        return "Getting started"
    p = started[-1].payload
    if p.get("loop") == "weekly_review":
        return f"Weekly review · Week {p.get('week')}"
    return f"Daily check · Day {p.get('day')}"


# ---------------------------------------------------------------- detail


def _evidence(ws: Workspace, ids: list[str]) -> list[dict]:
    out = []
    for eid in ids:
        ev = ws.ledger.get(eid)
        out.append({"id": eid, "note": ev.note if ev else "", "values": ev.values if ev else {}})
    return out


def _checks(ws: Workspace, record: ArtifactRecord) -> list[dict]:
    """What was checked in code, as PASS / FAIL rows."""
    c = record.content
    rows = []
    if record.kind == "ChangeSet":
        for g in c.get("gate", []):
            for r in g.get("results", []):
                rows.append({"passed": r["passed"], "text": f"Change {g['change_index'] + 1}: "
                             f"{r['reason']}", "rule": r["rule"]})
        for d in c.get("ad_drafts", []):
            review = ws.artifacts.latest(f"{record.key.split('/')[0]}/review/{d['test_id']}")
            if review is not None:
                issues = review.content.get("issues") or []
                rows.append({"passed": review.content.get("verdict") == "pass",
                             "text": f"Reviewer checked {d['test_id']}: "
                             + ("; ".join(issues) if issues else "every claim traces to evidence"),
                             "rule": "creative_review", "evidence": d.get("evidence_ids", [])})
    return rows


def _media_url(path: str, media_dir: str | None) -> str | None:
    if not media_dir:
        return None
    from pathlib import Path

    try:
        rel = Path(path).resolve().relative_to(Path(media_dir).resolve())
    except ValueError:
        return None
    return f"/api/media/{rel.as_posix()}"


def _creative(ws: Workspace, key: str, media_dir: str | None) -> dict | None:
    record = ws.artifacts.latest(key)
    if record is None:
        return None
    c = record.content
    return {"key": key, "title": title(record), "ad_copy": c.get("ad_copy", {}),
            "test_id": c.get("test_id"),
            "images": [{**img, "url": _media_url(img["path"], media_dir)}
                       for img in c.get("images", [])]}


def detail(ws: Workspace, key: str, account=None, media_dir: str | None = None) -> dict | None:
    record = ws.artifacts.latest(key)
    if record is None or record.kind not in TYPES:
        return None
    idx = Index.build(ws)
    names = campaign_names(account)
    item = deliverable(ws, idx, record, names)
    c = record.content
    week = record.key.split("/")[0]
    extra: dict = {}
    if record.kind == "ChangeSet" and c.get("ad_drafts"):
        plan = ws.artifacts.latest(f"{week}/test_plan")
        budgets = {t["test_id"]: t.get("weekly_budget") for t in (plan.content["tests"]
                   if plan else [])}
        rows, creatives = [], []
        for d in c["ad_drafts"]:
            cr = _creative(ws, f"{week}/creative/{d['test_id']}", media_dir)
            creatives.append(cr)
            rows.append({"ad": (cr or {}).get("ad_copy", {}).get("headline") or d["name"],
                         "name": d["name"], "test_id": d["test_id"],
                         "campaign": names.get(d["campaign_id"], d["campaign_id"]),
                         "campaign_id": d["campaign_id"], "budget": budgets.get(d["test_id"])})
        extra = {"ads": rows, "creatives": [x for x in creatives if x]}
    elif record.kind == "ChangeSet":
        extra = {"changes": [{**ch, "campaign": names.get(ch["target_id"], ch["target_id"]),
                              "allowed": next((g["allowed"] for g in c.get("gate", [])
                                               if g["change_index"] == i), True)}
                             for i, ch in enumerate(c.get("changes", []))]}
    elif record.kind == "CreativeSet":
        review = ws.artifacts.latest(f"{week}/review/{c.get('test_id')}")
        extra = {"creative": _creative(ws, key, media_dir),
                 "review": review.content if review else None}
    elif record.kind == "TestPlan":
        extra = {"tests": [{**t, "campaign": names.get(t["campaign_id"], t["campaign_id"])}
                           for t in c.get("tests", [])]}
    req = idx.waiting(record)
    headline = (c.get("summary") or subtitle(ws, record, names)).strip()
    return {
        **item,
        "headline": headline,
        "content": c,
        "content_hash": record.content_hash,
        "checks": _checks(ws, record),
        "evidence": _evidence(ws, record.evidence_ids),
        "can_approve": req is not None,
        "approve_blocked": [r["rule"] for r in _checks(ws, record)
                            if not r["passed"] and r["rule"] != "creative_review"],
        "discussion": discussion(ws, idx, record, account),
        **extra,
    }


def discussion(ws: Workspace, idx: Index, record: ArtifactRecord, account=None) -> dict:
    """The room message that carried this item, with its thread, plus the related team steps."""
    items = room(ws, account, idx)
    root = item_anchor = None
    for it in items:
        if it["kind"] == "msg" and it.get("card") and it["card"]["key"] == record.key:
            root = it
            break
    pub = idx.published.get(record.artifact_id)
    if root is None and pub is not None:
        for it in items:
            if it["kind"] == "fold" and pub.id in it["ids"]:
                item_anchor = it
                break
    anchor = root or item_anchor
    lines: list[dict] = []
    test_id = record.content.get("test_id")
    for it in items:
        if it["kind"] != "fold":
            continue
        for line in it["items"]:
            mine = pub is not None and line["id"] == pub.id
            if mine or (test_id and test_id in line["text"]):
                lines.append(line)
    return {
        "anchor_id": anchor["id"] if anchor else None,
        "anchor_ts": anchor.get("ts") or anchor.get("ts_from") if anchor else None,
        "anchor_who": (anchor.get("who") or (anchor.get("actors") or [None])[0]) if anchor else None,
        "root": root,
        "lines": lines,
    }
