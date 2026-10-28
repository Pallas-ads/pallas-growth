"""Command-line entry points:

pgt simulate --days 7 [--live] [--db path]
pgt weekly [--yes] [--live] [--db path] [--out runs/media]
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
    sim.add_argument("--plant-injection", action="store_true")
    week = sub.add_parser("weekly", help="Simulate a week, then run the weekly review")
    week.add_argument("--seed", type=int, default=7)
    week.add_argument("--db", default=":memory:", help="SQLite file for the workspace")
    week.add_argument("--out", default="runs/media", help="Folder for creative images")
    week.add_argument("--live", action="store_true", help="Call Muse (costs money; needs a key)")
    week.add_argument("--model", default="muse-spark-1.3")
    week.add_argument("--yes", action="store_true", help="Approve both steps without asking")
    args = parser.parse_args(argv)
    asyncio.run(_simulate(args) if args.command == "simulate" else _weekly(args))


def _models(args):
    """Returns (text model, AsyncOpenAI client for images, fake or None)."""
    from pgt.llm.meta import meta_client, responses_model
    from pgt.llm.offline import offline_model

    if args.live:
        from dotenv import load_dotenv

        load_dotenv()
        client = meta_client()
        print(f"Live run on {args.model}.")
        return responses_model(client, args.model), client, None
    model, fake = offline_model(args.model)
    print("Offline run: a rule-based stand-in answers instead of Muse (no cost).")
    return model, fake.client(), fake


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
    from pgt.llm.meta import meta_client, responses_model
    from pgt.llm.offline import offline_model
    from pgt.loops.simulate import simulate

    fake = None
    if args.live:
        from dotenv import load_dotenv

        load_dotenv()
        model = responses_model(meta_client(), args.model)
        print(f"Live run on {args.model}.")
    else:
        model, fake = offline_model(args.model)
        print("Offline run: a rule-based stand-in answers instead of Muse (no cost).")

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
