"""The Growth Lead's first message on a newly connected account, real or sandbox.

It says what the team read and what comes next. Plain code: no model is called, nothing is
written to the account, and a read that fails is named instead of stopping the message."""

from __future__ import annotations

import asyncio

from pgt.ads.ads_mcp import McpError, draft_counts
from pgt.harness.workspace import Workspace


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


def _source(reads: dict[str, str]) -> str:
    used = {v for v in reads.values() if v in {"mcp", "graph"}}
    if used == {"mcp"}:
        return "Meta's Ads MCP"
    if used == {"graph"}:
        return "the Graph API"
    return "Meta's Ads MCP and the Graph API"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def kickoff_text(name: str, state, drafts: list[dict] | None, draft_error: str | None,
                 reads: dict[str, str] | None, scoped: bool) -> str:
    """`reads` names the source of each read on a real account; None means the sandbox, whose
    campaigns hold ads directly (a real account's ad sets stand in for them)."""
    if reads is None:
        lines = [f"I read {name}, a synthetic account. Nothing was changed."]
    else:
        lines = [f"I read {name} through {_source(reads)}. Nothing was changed."]
    unit = "campaign" if reads is None else "ad set"
    live = bool(state and state.campaigns)
    if state is not None:
        if live:
            spent = sum(a.lifetime_spend for a in state.ads)
            where = " in the scope you set" if scoped else ""
            money = (f"{_money(spent, state.currency)} spent so far" if spent
                     else "nothing spent yet")
            lines.append(f"Live{where}: {_plural(len(state.campaigns), unit)} with "
                         f"{_plural(len(state.ads), 'ad')}, {money}.")
        else:
            lines.append(f"Nothing is live: no active or paused {unit}s.")
    counts = draft_counts(drafts or [])
    if draft_error:
        err = draft_error.strip()
        lines.append(f"I could not read the drafts in Ads Manager: {err}"
                     + ("" if err.endswith((".", "!", "?")) else "."))
    elif drafts:
        parts = [_plural(counts[k], k) for k in ("campaign", "ad set", "ad") if counts[k]]
        names = [d["name"] for d in drafts if d["name"]][:3]
        named = f" ({', '.join(names)})" if names else ""
        lines.append(f"Ads Manager holds unpublished drafts: {_join(parts)}{named}. "
                     "They are listed under Dashboard, read only.")
    else:
        lines.append("No drafts are waiting in Ads Manager.")
    if live:
        lines.append("Next: run the first daily check, and I will bring you what moved and "
                     "what needs your decision.")
    elif drafts:
        lines.append("Next: publish the drafts in Ads Manager when they are ready. Daily checks "
                     "start once something is live.")
    else:
        lines.append("Next: publish a campaign in Ads Manager. Daily checks start once "
                     "something is live.")
    return " ".join(lines)


async def kickoff(ws: Workspace, account, name: str) -> str:
    """Read the account and its drafts, then post the Growth Lead's first message."""
    state, drafts, draft_error = None, None, None
    try:
        state = await asyncio.to_thread(account.state)
    except Exception as exc:  # noqa: BLE001 - named in the message; errors never carry the token
        text = (f"I could not read {name} yet: {exc}. Nothing was changed. Reconnect from "
                "Settings to try again.")
        ws.events.append("message.posted", "growth_lead", {"text": text}, sim_day=account.day)
        return text
    try:
        drafts = await asyncio.to_thread(account.drafts)
    except McpError as exc:
        draft_error = str(exc)
    reads = account.read_sources()["reads"] if hasattr(account, "read_sources") else None
    text = kickoff_text(name, state, drafts, draft_error, reads,
                        bool(getattr(account, "adset_ids", None)))
    ws.events.append("message.posted", "growth_lead", {"text": text}, sim_day=account.day)
    return text
