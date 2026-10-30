"""Local clock, delivery simulation, and Operator-only mutation interface."""

from __future__ import annotations

import math
import random
from copy import deepcopy
from decimal import Decimal

from pgt.schemas.ads import AccountState, AdState, InsightRow, Status

from .scenario import AdPerformance, Scenario


class SandboxAccount:
    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario.model_copy(deep=True)
        self._state = AccountState(
            day=0,
            currency=scenario.currency,
            campaigns=deepcopy(scenario.campaigns),
            ads=deepcopy(scenario.ads),
        )
        self._rows: list[InsightRow] = []
        self._impressions = dict.fromkeys((ad.ad_id for ad in scenario.ads), 0)
        self._reach = dict.fromkeys((ad.ad_id for ad in scenario.ads), 0)
        self._changes: list[dict] = []

    @property
    def day(self) -> int:
        return self._state.day

    def _frequency(self, ad_id: str) -> float:
        return self._impressions[ad_id] / max(1, self._reach[ad_id])

    @staticmethod
    def _fatigue(frequency: float) -> float:
        return max(0.4, 1.0 - 0.35 * max(0.0, frequency - 2.5))

    def advance_day(self) -> list[InsightRow]:
        day = self.day + 1
        new_rows = []
        for campaign in self._state.campaigns:
            active = sorted(
                (
                    ad for ad in self._state.ads
                    if ad.campaign_id == campaign.campaign_id and ad.status == "ACTIVE"
                ),
                key=lambda ad: ad.ad_id,
            )
            if campaign.status != "ACTIVE" or not active:
                continue
            rngs = {ad.ad_id: random.Random(f"{self._scenario.seed}:{day}:{ad.ad_id}")
                    for ad in active}
            # Averaging per-ad draws keeps pacing independent of other campaigns.
            pacing = sum(rngs[ad.ad_id].uniform(0.9, 1.0) for ad in active) / len(active)
            cents = round(campaign.daily_budget * pacing * 100)
            weights = []
            for ad in active:
                params = self._scenario.performance[ad.ad_id]
                weights.append(
                    params.base_ctr * self._fatigue(self._frequency(ad.ad_id))
                    * params.conversion_rate * 1000 / params.base_cpm
                )
            # Allocate integer cents by largest remainder so spend stays conserved.
            shares = [cents * weight / sum(weights) for weight in weights]
            allocations = [math.floor(share) for share in shares]
            remainder_order = sorted(
                range(len(active)), key=lambda i: (-(shares[i] - allocations[i]), i)
            )
            for i in remainder_order[:cents - sum(allocations)]:
                allocations[i] += 1
            learning_multiplier = 0.9 if campaign.in_learning else 1.0
            for ad, allocation in zip(active, allocations):
                ad_id = ad.ad_id
                rng = rngs[ad_id]
                params = self._scenario.performance[ad_id]
                spend = allocation / 100
                cpm = params.base_cpm * rng.uniform(0.97, 1.03)
                impressions = round(spend / cpm * 1000)
                unseen = max(0.0, 1 - self._reach[ad_id] / params.audience_size)
                # Reach cannot exceed the remaining audience; retain one new person
                # per delivery day at saturation to keep the demo model simple.
                remaining = max(1, params.audience_size - self._reach[ad_id])
                reach = min(impressions, remaining, max(1, round(impressions * unseen)))
                self._impressions[ad_id] += impressions
                self._reach[ad_id] += reach
                frequency = self._frequency(ad_id)
                ctr = params.base_ctr * self._fatigue(frequency) * rng.uniform(0.95, 1.05)
                clicks = min(impressions, round(impressions * ctr))
                results = min(clicks, round(
                    clicks * params.conversion_rate * learning_multiplier * rng.uniform(0.97, 1.03)
                ))
                row = InsightRow(
                    evidence_id=f"ev-d{day}-{ad_id}",
                    day=day,
                    ad_id=ad_id,
                    campaign_id=campaign.campaign_id,
                    spend=spend,
                    impressions=impressions,
                    reach=reach,
                    clicks=clicks,
                    results=results,
                    ctr=round(clicks / impressions, 6) if impressions else 0.0,
                    cost_per_result=round(spend / results, 2) if results else None,
                    frequency=round(frequency, 2),
                )
                new_rows.append(row)
                ad.lifetime_spend = round(ad.lifetime_spend + spend, 2)
                ad.lifetime_results += results
                campaign.results_since_edit += results
            if campaign.results_since_edit >= 50:
                campaign.in_learning = False
        self._rows.extend(new_rows)
        self._state.day = day
        return deepcopy(sorted(new_rows, key=lambda row: (row.day, row.ad_id)))

    def state(self) -> AccountState:
        return self._state.model_copy(deep=True)

    def insights(
        self, day_from: int = 1, day_to: int | None = None, ad_ids: list[str] | None = None
    ) -> list[InsightRow]:
        end = self.day if day_to is None else day_to
        selected = None if ad_ids is None else set(ad_ids)
        return deepcopy(sorted(
            (row for row in self._rows if day_from <= row.day <= end
             and (selected is None or row.ad_id in selected)),
            key=lambda row: (row.day, row.ad_id),
        ))

    def changes(self) -> list[dict]:
        """Every write the Operator made, in order, each with the sim day it happened on."""
        return deepcopy(self._changes)

    def writer(self) -> SandboxWriter:
        return SandboxWriter(self)

    def dump(self) -> dict:
        return {
            "scenario": self._scenario.model_dump(mode="json"),
            "state": self._state.model_dump(mode="json"),
            "rows": [row.model_dump(mode="json") for row in self._rows],
            "impressions": dict(self._impressions),
            "reach": dict(self._reach),
            "changes": deepcopy(self._changes),
        }

    @classmethod
    def load(cls, data: dict) -> SandboxAccount:
        account = cls(Scenario.model_validate(data["scenario"]))
        account._state = AccountState.model_validate(deepcopy(data["state"]))
        account._rows = [InsightRow.model_validate(deepcopy(row)) for row in data["rows"]]
        account._impressions = dict(data["impressions"])
        account._reach = dict(data["reach"])
        account._changes = deepcopy(data["changes"])
        return account


class SandboxWriter:
    """Mutations for the Operator; approval and house rules belong to policy."""

    def __init__(self, account: SandboxAccount) -> None:
        self._account = account

    def _record(self, kind: str, **change) -> dict:
        record = {"day": self._account.day, "kind": kind, **change}
        self._account._changes.append(deepcopy(record))
        return record

    def set_campaign_budget(self, campaign_id: str, daily_budget: float) -> dict:
        campaign = self._account._state.campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        if not math.isfinite(daily_budget) or daily_budget <= 0:
            raise ValueError("Daily budget must be finite and positive")
        previous = campaign.daily_budget
        # Decimal avoids treating an exact 20% edit as significant through float error.
        significant = (
            abs(Decimal(str(daily_budget)) - Decimal(str(previous)))
            > Decimal(str(previous)) * Decimal("0.20")
        )
        campaign.daily_budget = daily_budget
        campaign.last_budget_change_day = self._account.day
        if significant:
            campaign.results_since_edit = 0
            campaign.in_learning = True
        return self._record(
            "campaign_budget", campaign_id=campaign_id, previous=previous,
            daily_budget=daily_budget, significant_edit=significant,
        )

    def set_ad_status(self, ad_id: str, status: Status) -> dict:
        ad = self._account._state.ad(ad_id)
        if ad is None:
            raise KeyError(ad_id)
        if status not in ("ACTIVE", "PAUSED"):
            raise ValueError("Ad status must be ACTIVE or PAUSED")
        previous = ad.status
        ad.status = status
        return self._record("ad_status", ad_id=ad_id, previous=previous, status=status)

    def create_ad(self, campaign_id: str, name: str) -> str:
        account = self._account
        if account._state.campaign(campaign_id) is None:
            raise KeyError(campaign_id)
        next_id = max((int(ad.ad_id.removeprefix("ad_")) for ad in account._state.ads), default=0) + 1
        ad_id = f"ad_{next_id}"
        account._state.ads.append(AdState(
            ad_id=ad_id, campaign_id=campaign_id, name=name, status="PAUSED",
            created_day=account.day, lifetime_spend=0.0, lifetime_results=0,
        ))
        account._scenario.performance[ad_id] = AdPerformance()
        account._impressions[ad_id] = 0
        account._reach[ad_id] = 0
        self._record("ad_created", ad_id=ad_id, campaign_id=campaign_id, name=name, status="PAUSED")
        return ad_id
