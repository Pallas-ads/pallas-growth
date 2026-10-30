"""A real Meta ad account behind the same interface as the sandbox.

PGT's "campaign" is a Meta ad set: that is where the daily budget, the learning phase and the
ads live. Day 1 is the origin date; `day` counts the complete days since then, so today's partial
numbers are never read as a finished day.

Writes follow the L2 rules for real accounts:
  - new ads are created PAUSED, inside an ad set that already exists; live ads are never edited
  - an ad can be paused, but never turned on: that step stays with the owner in Ads Manager
  - a daily budget is changed only on an ad set that owns its daily budget (not CBO, not lifetime)
Approval and house rules are checked before the writer is called, by the Operator and the gate.
"""

from __future__ import annotations

import base64
import math
import os
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

from pgt.schemas.ads import AccountState, AdState, CampaignState, InsightRow, Status

from .graph import Graph, GraphError

# Currencies Meta bills without minor units; every other currency uses cents.
_NO_MINOR_UNITS = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}
_ADSET_FIELDS = ("id,name,status,effective_status,daily_budget,lifetime_budget,"
                 "learning_stage_info,campaign{id,objective,daily_budget,lifetime_budget}")
_AD_FIELDS = "id,name,adset_id,status,created_time"
_INSIGHT_FIELDS = "ad_id,adset_id,spend,impressions,reach,clicks,actions"


class WriteRefused(RuntimeError):
    """A write the real-account rules do not allow, refused before anything is sent to Meta."""


def _today() -> date:
    return date.today()


class MetaAccount:
    def __init__(
        self,
        graph: Graph,
        account_id: str,
        origin: date,
        *,
        adset_ids: list[str] | None = None,
        page_id: str | None = None,
        link_url: str | None = None,
        result_action: str | None = None,
        call_to_action: str | None = None,
        today=_today,
        changes: list[dict] | None = None,
        on_write=None,
    ) -> None:
        self._graph = graph
        self.account_id = account_id if account_id.startswith("act_") else f"act_{account_id}"
        self.origin = origin
        self.adset_ids = list(adset_ids or [])
        self.page_id = page_id
        self.link_url = link_url
        self.result_action = result_action or "mobile_app_install"
        self.call_to_action = call_to_action or "LEARN_MORE"
        self._today = today
        self._changes: list[dict] = deepcopy(changes or [])
        self._on_write = on_write  # called with each write PGT made, so the workspace keeps it
        self._state: AccountState | None = None
        self._budget_owner: dict[str, str] = {}
        self._rows: dict[int, list[InsightRow]] = {}
        self._currency: str | None = None

    @classmethod
    def from_env(cls, graph: Graph, account_id: str, origin: date, **kw) -> MetaAccount:
        ids = [s.strip() for s in os.environ.get("META_ADSET_IDS", "").split(",") if s.strip()]
        return cls(
            graph, account_id, origin,
            adset_ids=ids or None,
            page_id=os.environ.get("META_PAGE_ID") or None,
            link_url=os.environ.get("META_LINK_URL") or None,
            result_action=os.environ.get("META_RESULT_ACTION") or None,
            call_to_action=os.environ.get("META_CALL_TO_ACTION") or None,
            **kw,
        )

    # --- clock -----------------------------------------------------------------------------

    @property
    def day(self) -> int:
        return max(0, (self._today() - self.origin).days)

    def date_of(self, day: int) -> date:
        return self.origin + timedelta(days=day - 1)

    def _day_of(self, when: date) -> int:
        return (when - self.origin).days + 1

    def sync(self) -> None:
        """Drop what was read, so the next call reads the account again."""
        self._state = None
        self._rows = {}

    def advance_day(self) -> list[InsightRow]:
        """A real account moves with the calendar; this reads the newest complete day."""
        self.sync()
        return self.insights(self.day, self.day) if self.day else []

    # --- reads -----------------------------------------------------------------------------

    @property
    def _offset(self) -> int:
        return 1 if self.currency in _NO_MINOR_UNITS else 100

    @property
    def currency(self) -> str:
        if self._currency is None:
            self._currency = self._graph.get(self.account_id, fields="currency").get(
                "currency", "USD")
        return self._currency

    def _money(self, minor) -> float:
        return int(minor) / self._offset if minor not in (None, "", "0") else 0.0

    def _results(self, actions) -> int:
        return int(sum(float(a.get("value", 0)) for a in actions or []
                       if a.get("action_type") == self.result_action))

    def state(self) -> AccountState:
        if self._state is None:
            self._state = self._read_state()
        return self._state.model_copy(deep=True)

    def _read_state(self) -> AccountState:
        filtering = [{"field": "effective_status", "operator": "NOT_IN",
                      "value": ["DELETED", "ARCHIVED"]}]
        adsets = self._graph.rows(f"{self.account_id}/adsets", fields=_ADSET_FIELDS,
                                  filtering=filtering)
        if self.adset_ids:
            adsets = [a for a in adsets if a["id"] in self.adset_ids]
        scope = {a["id"] for a in adsets}
        ads = [a for a in self._graph.rows(f"{self.account_id}/ads", fields=_AD_FIELDS,
                                           filtering=filtering)
               if a.get("adset_id") in scope]
        lifetime = {r["ad_id"]: r for r in self._graph.rows(
            f"{self.account_id}/insights", level="ad", date_preset="maximum",
            fields="ad_id,spend,actions")}
        campaigns = []
        for a in adsets:
            parent = a.get("campaign") or {}
            if a.get("daily_budget"):
                owner, budget = "adset", a["daily_budget"]
            elif parent.get("daily_budget"):
                owner, budget = "campaign", parent["daily_budget"]
            else:
                owner, budget = "lifetime", 0
            self._budget_owner[a["id"]] = owner
            learning = a.get("learning_stage_info") or {}
            campaigns.append(CampaignState(
                campaign_id=a["id"],
                name=a.get("name", a["id"]),
                status=_status(a.get("effective_status")),
                objective=parent.get("objective", ""),
                daily_budget=self._money(budget),
                in_learning=learning.get("status") == "LEARNING",
                results_since_edit=int(learning.get("conversions") or 0),
                last_budget_change_day=self._last_budget_change(a["id"]),
                test_id=None,
            ))
        return AccountState(
            day=self.day,
            currency=self.currency,
            campaigns=campaigns,
            ads=[AdState(
                ad_id=ad["id"],
                campaign_id=ad["adset_id"],
                name=ad.get("name", ad["id"]),
                status="ACTIVE" if ad.get("status") == "ACTIVE" else "PAUSED",
                created_day=self._day_of(_date(ad.get("created_time"))),
                lifetime_spend=float(lifetime.get(ad["id"], {}).get("spend") or 0),
                lifetime_results=self._results(lifetime.get(ad["id"], {}).get("actions")),
            ) for ad in ads],
        )

    def _last_budget_change(self, adset_id: str) -> int | None:
        days = [c["day"] for c in self._changes
                if c["kind"] == "campaign_budget" and c["campaign_id"] == adset_id]
        return max(days) if days else None

    def _rows_to(self, end: int) -> list[InsightRow]:
        """Daily rows for days 1..end, with frequency counted from day 1 as in the sandbox.

        Meta's daily reach is not additive, so this frequency is a floor on the true one."""
        if end not in self._rows:
            scope = {c.campaign_id for c in self.state().campaigns}
            raw = self._graph.rows(
                f"{self.account_id}/insights", level="ad", time_increment=1,
                fields=_INSIGHT_FIELDS,
                time_range={"since": self.date_of(1).isoformat(),
                            "until": self.date_of(end).isoformat()},
            )
            rows, impressions, reach = [], {}, {}
            for r in sorted(raw, key=lambda r: (r["date_start"], r["ad_id"])):
                if r.get("adset_id") not in scope:
                    continue
                ad_id = r["ad_id"]
                shown, people = int(r.get("impressions") or 0), int(r.get("reach") or 0)
                impressions[ad_id] = impressions.get(ad_id, 0) + shown
                reach[ad_id] = reach.get(ad_id, 0) + people
                spend = float(r.get("spend") or 0)
                clicks = int(r.get("clicks") or 0)
                results = self._results(r.get("actions"))
                day = self._day_of(date.fromisoformat(r["date_start"]))
                rows.append(InsightRow(
                    evidence_id=f"ev-d{day}-{ad_id}", day=day, ad_id=ad_id,
                    campaign_id=r["adset_id"], spend=spend, impressions=shown, reach=people,
                    clicks=clicks, results=results,
                    ctr=clicks / shown if shown else 0.0,
                    cost_per_result=round(spend / results, 4) if results else None,
                    frequency=round(impressions[ad_id] / max(1, reach[ad_id]), 4),
                ))
            self._rows[end] = rows
        return self._rows[end]

    def insights(self, day_from: int = 1, day_to: int | None = None,
                 ad_ids: list[str] | None = None) -> list[InsightRow]:
        end = self.day if day_to is None else min(day_to, self.day)
        if end < 1:
            return []
        selected = None if ad_ids is None else set(ad_ids)
        return deepcopy([r for r in self._rows_to(end) if day_from <= r.day <= end
                         and (selected is None or r.ad_id in selected)])

    def changes(self) -> list[dict]:
        """Every write PGT made to this account, in order, each with the day it happened on."""
        return deepcopy(self._changes)

    def writer(self) -> MetaWriter:
        return MetaWriter(self)

    def dump(self) -> dict:
        """What PGT keeps between runs. No token and no ad data: those are read again."""
        return {"account_id": self.account_id, "origin": self.origin.isoformat(),
                "changes": deepcopy(self._changes)}


class MetaWriter:
    """Writes for the Operator. Each one is checked against the real-account rules first."""

    def __init__(self, account: MetaAccount) -> None:
        self._account = account
        self._graph = account._graph

    def _record(self, kind: str, **change) -> dict:
        record = {"day": self._account.day, "kind": kind, **change}
        self._account._changes.append(deepcopy(record))
        if self._account._on_write is not None:
            self._account._on_write(deepcopy(record))
        self._account.sync()
        return record

    def set_campaign_budget(self, campaign_id: str, daily_budget: float) -> dict:
        account = self._account
        campaign = account.state().campaign(campaign_id)
        if campaign is None:
            raise WriteRefused(f"Ad set {campaign_id} is not in the team's scope")
        owner = account._budget_owner.get(campaign_id)
        if owner == "campaign":
            raise WriteRefused("This ad set's budget is set on its campaign (Advantage campaign "
                               "budget). Change it in Ads Manager.")
        if owner != "adset":
            raise WriteRefused("This ad set runs on a lifetime budget. Change it in Ads Manager.")
        if not math.isfinite(daily_budget) or daily_budget <= 0:
            raise WriteRefused("Daily budget must be finite and positive")
        minor = round(daily_budget * account._offset)
        self._graph.post(campaign_id, daily_budget=str(minor))
        return self._record("campaign_budget", campaign_id=campaign_id,
                            previous=campaign.daily_budget, daily_budget=minor / account._offset)

    def set_ad_status(self, ad_id: str, status: Status) -> dict:
        ad = self._account.state().ad(ad_id)
        if ad is None:
            raise WriteRefused(f"Ad {ad_id} is not in the team's scope")
        if status != "PAUSED":
            raise WriteRefused("PGT never turns ads on in a real account. Turn it on in Ads "
                               "Manager.")
        self._graph.post(ad_id, status="PAUSED")
        return self._record("ad_status", ad_id=ad_id, previous=ad.status, status="PAUSED")

    def create_ad(self, campaign_id: str, name: str, creative=None) -> str:
        """Upload the image, build the creative, and create the ad PAUSED in an existing ad set."""
        account = self._account
        if account.state().campaign(campaign_id) is None:
            raise WriteRefused(f"Ad set {campaign_id} is not in the team's scope")
        if creative is None or not creative.images:
            raise WriteRefused("A new ad needs a reviewed creative with an image")
        if not account.page_id or not account.link_url:
            raise WriteRefused("Set META_PAGE_ID and META_LINK_URL to create ads")
        image = next((i for i in creative.images if i.ratio == "1:1"), creative.images[0])
        path = Path(image.path)
        if not path.is_file():
            raise WriteRefused(f"The image for {creative.creative_id} is missing")
        uploaded = self._graph.post(f"{account.account_id}/adimages",
                                    bytes=base64.b64encode(path.read_bytes()).decode())
        image_hash = next(iter((uploaded.get("images") or {}).values()), {}).get("hash")
        if not image_hash:
            raise GraphError("Meta did not return an image hash")
        copy = creative.ad_copy
        link_data = {
            "image_hash": image_hash, "link": account.link_url,
            "message": copy.primary_text, "name": copy.headline,
            "call_to_action": {"type": account.call_to_action,
                               "value": {"link": account.link_url}},
        }
        if copy.description:
            link_data["description"] = copy.description
        made = self._graph.post(
            f"{account.account_id}/adcreatives", name=f"PGT {creative.creative_id}",
            object_story_spec={"page_id": account.page_id, "link_data": link_data},
        )
        ad = self._graph.post(f"{account.account_id}/ads", name=name, adset_id=campaign_id,
                              creative={"creative_id": made["id"]}, status="PAUSED")
        self._record("ad_created", ad_id=ad["id"], campaign_id=campaign_id, name=name,
                     status="PAUSED", creative_id=creative.creative_id)
        return ad["id"]


def _status(effective: str | None) -> Status:
    return "ACTIVE" if effective == "ACTIVE" else "PAUSED"


def _date(created: str | None) -> date:
    if not created:
        return _today()
    return datetime.fromisoformat(created.replace("+0000", "+00:00")).date()
