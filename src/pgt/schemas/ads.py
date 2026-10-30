"""Ad account data shapes. The sandbox and the real Ads MCP adapter both return these."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["ACTIVE", "PAUSED"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignState(_Strict):
    campaign_id: str
    name: str
    status: Status
    objective: str  # e.g. "APP_PROMOTION"
    daily_budget: float
    in_learning: bool
    results_since_edit: int  # results since launch or the last significant edit
    last_budget_change_day: int | None  # sim day of the last budget change, None if never
    test_id: str | None  # set when the campaign runs a weekly test


class AdState(_Strict):
    ad_id: str
    campaign_id: str
    name: str
    status: Status
    created_day: int
    lifetime_spend: float
    lifetime_results: int


class AccountState(_Strict):
    day: int  # current sim day; insight rows exist for days 1..day
    currency: str
    campaigns: list[CampaignState]
    ads: list[AdState]

    def campaign(self, campaign_id: str) -> CampaignState | None:
        return next((c for c in self.campaigns if c.campaign_id == campaign_id), None)

    def ad(self, ad_id: str) -> AdState | None:
        return next((a for a in self.ads if a.ad_id == ad_id), None)


class InsightRow(_Strict):
    """One day of delivery for one ad. Every row is an evidence entry."""

    evidence_id: str  # "ev-d{day}-{ad_id}"
    day: int
    ad_id: str
    campaign_id: str
    spend: float
    impressions: int
    reach: int  # unique people reached that day
    clicks: int
    results: int
    ctr: float  # clicks / impressions, 0 when no impressions
    cost_per_result: float | None  # None when results == 0
    frequency: float  # cumulative impressions / cumulative reach since the ad launched
    purchase_value: float | None = None  # revenue from results, when the account tracks it


class Evidence(_Strict):
    """An entry in the evidence ledger: raw rows from the account, or numbers derived in code."""

    evidence_id: str
    kind: Literal["insight", "derived", "state"]
    values: dict[str, float]  # the numbers a deliverable may cite
    inputs: list[str] = []  # evidence IDs this was derived from
    note: str = ""  # formula or plain-language description
