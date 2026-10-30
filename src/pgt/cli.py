"""Command-line entry points:

pgt simulate --days 7 [--live] [--provider meta|mixed] [--db path]
pgt weekly [--yes] [--live] [--provider meta|mixed] [--db path] [--out runs/media]
pgt serve [--port 8000] [--db path]   (web app on an offline demo workspace)

--provider mixed runs the text roles on Muse Spark (open weights) on Baseten and keeps the
Creative Strategist (hosted web search) and image work on the Meta API.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from pgt.harness.workspace import Workspace


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser(prog="pgt", description="Pallas Growth Team")
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("simulate", help="Run the daily check over simulated days")
    sim.add_argument("--days", type=int, default=7)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--db", default=":memory:", help="SQLite file for the workspace")
    sim.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    sim.add_argument("--model", default="muse-spark-1.3")
    sim.add_argument("--provider", choices=["meta", "mixed"], default="meta")
    sim.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        default="medium",
        help="Reasoning effort on Baseten (--provider mixed)",
    )
    sim.add_argument("--plant-injection", action="store_true")
    week = sub.add_parser("weekly", help="Simulate a week, then run the weekly review")
    week.add_argument("--seed", type=int, default=7)
    week.add_argument("--db", default=":memory:", help="SQLite file for the workspace")
    week.add_argument("--out", default="runs/media", help="Folder for creative images")
    week.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    week.add_argument("--model", default="muse-spark-1.3")
    week.add_argument("--provider", choices=["meta", "mixed"], default="meta")
    week.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        default="medium",
        help="Reasoning effort on Baseten (--provider mixed)",
    )
    week.add_argument("--yes", action="store_true", help="Approve both steps without asking")
    serve = sub.add_parser("serve", help="Run the web app on an offline demo workspace")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=None, help="SQLite file (default: a new one in runs/web)")
    serve.add_argument("--web", default="web/dist", help="Built frontend to serve, if present")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    asyncio.run(_simulate(args) if args.command == "simulate" else _weekly(args))


def _models(args):
    """Returns (text model or per-role router, AsyncOpenAI client for images, fake or None)."""
    from pgt.llm.meta import meta_client, responses_model
    from pgt.llm.offline import offline_model

    if args.live:
        from dotenv import load_dotenv

        load_dotenv()
        client = meta_client()
        model = responses_model(client, args.model)
        if args.provider == "mixed":
            from pgt.llm.baseten import BASETEN_MODEL, baseten_client, chat_model
            from pgt.llm.routing import mixed

            model = mixed(model, chat_model(baseten_client(), effort=args.effort))
            print(f"Live run: text roles on {BASETEN_MODEL} (Baseten), web search on {args.model}.")
        else:
            print(f"Live run on {args.model}.")
        return model, client, None
    model, fake = offline_model(args.model)
    print("Offline run: a rule-based stand-in answers instead of Muse (no cost).")
    return model, fake.client(), fake


def _serve(args) -> None:
    from datetime import datetime

    import uvicorn

    from pgt.api.app import create_app, offline_team

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path("runs/web") / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    db = args.db or str(run_dir / "workspace.db")
    team = offline_team(db, str(run_dir / "media"))
    print("Offline demo: a rule-based stand-in answers instead of Muse (no cost).")
    print(f"Workspace {db}. Open http://{args.host}:{args.port}")
    uvicorn.run(create_app(team, args.web), host=args.host, port=args.port, log_level="warning")


def _ask(question: str, yes: bool) -> bool:
    if yes:
        print(f"{question} yes (--yes)")
        return True
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


async def _weekly(args) -> None:
    from pgt.llm.media import MediaClient
    from pgt.loops.simulate import simulate
    from pgt.loops.weekly import apply_week, plan_week, produce_week
    from pgt.schemas import ChangeSet, TestPlan

    model, image_client, fake = _models(args)
    ws = Workspace(args.db)
    try:
        sim = await simulate(7, model, ws, seed=args.seed)
        account = sim.account
        print(f"Simulated days 1-{account.day}: {len(sim.briefs)} daily checks.")

        summary = await plan_week(ws, account, model)
        if summary.stage != "awaiting_plan_approval":
            return _stop(summary)
        plan = TestPlan.model_validate(ws.artifacts.latest(summary.plan_id).content)
        print(f"\nWeek {plan.week} test plan: {plan.summary}")
        print(f"Test budget: ${plan.available_test_budget:g} for the week.")
        for t in plan.tests:
            print(f"  {t.test_id} {t.name}: ${t.weekly_budget:g} in {t.campaign_id}")
            print(f"    Hypothesis: {t.hypothesis}")
            print(f"    Hook: {t.brief.hook}  (evidence {', '.join(t.evidence_ids)})")
        if not _ask("Approve the test plan?", args.yes):
            print("Stopped before production. Nothing was changed.")
            return
        ws.approvals.grant(summary.plan_id, ws.artifacts.latest(summary.plan_id))

        summary = await produce_week(ws, account, model, MediaClient(image_client), args.out)
        for e in ws.events.list("question.answered", loop_id=f"weekly-w{plan.week}"):
            print(f"  Analyst answered {e.payload['task_id']}: {e.payload['answer']}")
        for e in ws.events.list("review.redo", loop_id=f"weekly-w{plan.week}"):
            p = e.payload
            print(f"  Redo {p['subject']} (by {p.get('by', 'reviewer')}): {'; '.join(p['issues'])}")
        if summary.stage != "awaiting_change_approval":
            return _stop(summary)
        key = f"week{plan.week}/change_set"
        change_set = ChangeSet.model_validate(ws.artifacts.latest(key).content)
        print(f"\n{change_set.summary}")
        for d in change_set.ad_drafts:
            creative = ws.artifacts.latest(f"week{plan.week}/creative/{d.test_id}").content
            copy = creative["ad_copy"]
            print(f'  {d.name} in {d.campaign_id}: "{copy["headline"]}"')
            print(f"    {copy['primary_text']}")
            folder = Path(creative["images"][0]["path"]).parent
            print(f"    Images (1:1, 4:5, 9:16): {folder}")
        if not _ask("Approve creating these ads, paused?", args.yes):
            print("Stopped before the sandbox. Nothing was changed.")
            return
        ws.approvals.grant(key, ws.artifacts.latest(key))
        summary, receipt = apply_week(ws, account)
        print(f"\nCreated {', '.join(receipt.created_ad_ids)} in the sandbox, all PAUSED.")
        _totals(ws, fake, f"weekly-w{plan.week}")
    finally:
        ws.close()


def _stop(summary) -> None:
    print(f"\nWeekly review stopped: {summary.stage}.")
    for reason in summary.escalations:
        print(f"  Needs you: {reason}")


def _totals(ws, fake, loop_id: str) -> None:
    calls = ws.events.list("model.call", loop_id=loop_id)
    tokens = sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls)
    cost = sum(e.payload["cost"] for e in calls)
    label = "estimated at list price" if fake else "at list price"
    print(f"Weekly review: {len(calls)} model calls, {tokens:,} tokens, ${cost:.4f} {label}.")
    if fake is not None:
        print(f"Meta request rule violations: {len(fake.violations)}")


async def _simulate(args) -> None:
    from pgt.loops.simulate import simulate

    model, _, fake = _models(args)

    ws = Workspace(args.db)
    try:
        sim = await simulate(
            args.days, model, ws, seed=args.seed, plant_injection=args.plant_injection
        )
        receipts = {r.day: r for r in sim.receipts}
        for brief in sim.briefs:
            print(f"\n{brief.headline}")
            for f in brief.findings:
                print(f"  [{f.severity}] {f.subject_id}: {f.claim}  ({', '.join(f.evidence_ids)})")
            for reason in brief.blocked:
                print(f"  {reason}")
            if brief.review and brief.review.issues:
                print(f"  Review {brief.review.verdict}: {'; '.join(brief.review.issues)}")
            if brief.day in receipts:
                print(f"  Applied changes {receipts[brief.day].applied} (simulated approval).")
        calls = ws.events.list("model.call")
        tokens = sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in calls)
        cost = sum(e.payload["cost"] for e in calls)
        label = "estimated at list price" if fake else "at list price"
        print(f"\n{len(calls)} model calls, {tokens:,} tokens, ${cost:.4f} {label}.")
        if fake is not None:
            print(f"Meta request rule violations: {len(fake.violations)}")
    finally:
        ws.close()
