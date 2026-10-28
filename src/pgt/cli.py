"""Command-line entry point: `pgt simulate --days 7 [--live] [--db path]`."""

from __future__ import annotations

import argparse
import asyncio
import os

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
    args = parser.parse_args(argv)
    asyncio.run(_simulate(args))


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
