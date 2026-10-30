// Types and calls for the PGT web API (src/pgt/api). The server computes every status;
// the client only shows what it is given.
import { useEffect, useState } from "react";

export type Who = { actor: string; code: string | null; name: string; persona: string };
export type Status = "draft" | "review" | "wait" | "ok" | "applied" | "stale";

export type Deliverable = {
  key: string;
  artifact_id: string;
  kind: string;
  tab: string;
  title: string;
  sub: string;
  from: Who;
  status: Status;
  status_label: string;
  updated: string;
  sim_day: number | null;
  discussed: { event_id: number; sim_day: number | null; ts: string };
};

export type Card = Deliverable & { note: string; approved_ts?: string };
export type Reply = { id: number; ts: string; who: Who; text: string };
export type Line = { id: number; ts: string; sim_day: number | null; type: string; who: Who; text: string };

export type Msg = {
  kind: "msg"; id: number; ts: string; sim_day: number | null; who: Who; text: string;
  mention: boolean; card: Card | null; replies: Reply[]; models?: string[];
};
export type Fold = {
  kind: "fold"; id: number; ids: number[]; actors: Who[]; names: string[]; tail: string;
  ts_from: string; ts_to: string; sim_day: number | null; items: Line[];
};
export type DayDiv = { kind: "day"; id: number; label: string; sim_day: number | null };
export type RoomItem = Msg | Fold | DayDiv;

export type Check = { passed: boolean; text: string; rule: string; evidence?: string[] };
export type Evidence = { id: string; note: string; values: Record<string, number> };
export type Image = { ratio: string; width: number; height: number; url: string | null };
export type AdCopy = { name: string; headline: string; primary_text: string; description: string };
export type Creative = { key: string; title: string; test_id: string; ad_copy: AdCopy; images: Image[] };

export type Detail = Deliverable & {
  headline: string;
  content: Record<string, unknown>;
  content_hash: string;
  checks: Check[];
  evidence: Evidence[];
  can_approve: boolean;
  approve_blocked: string[];
  made_with: Tally | null;
  discussion: { anchor_id: number | null; anchor_ts: string | null; anchor_who: Who | null;
                root: Msg | null; lines: Line[] };
  ads?: { ad: string; name: string; test_id: string; campaign: string; campaign_id: string;
          budget: number | null }[];
  creatives?: Creative[];
  changes?: { kind: string; target_id: string; reason: string; campaign: string; allowed: boolean;
              new_daily_budget?: number }[];
  creative?: Creative;
  review?: { verdict: string; issues: string[] } | null;
  tests?: { test_id: string; name: string; hypothesis: string; campaign: string;
            campaign_id: string; weekly_budget: number; stop_rule: string;
            brief?: { angle: string; hook: string } }[];
};

export type Mode = "offline" | "live" | "mixed";
export type AppStatus = { busy: string | null; error: string | null; where: string;
                          day: number | null; last_event_id: number; owner: string; mode: Mode };

/** Model calls for one role or one deliverable, at list price. */
export type Tally = {
  models: { model: string; provider: string; calls: number }[];
  calls: number; failed: number; tokens: number; searches: number; cost: number; seconds: number;
};
export type SettingsData = {
  facts: Record<string, unknown>; fact_version: number; model_calls: number; tokens: number;
  cost: number; offline: boolean; mode: Mode; usage: (Tally & { who: Who })[];
  cap: number | null; remaining: number | null; day: number | null; now: string;
};

/** "muse-spark-1.3", or "offline stand-in" when no real model answered. */
export function modelLabel(models: string[] | undefined, mode: Mode | undefined): string {
  if (!models?.length) return "";
  const names = models.map((m) => m.replace(/^meta-models\//, "")).join(" + ");
  return mode === "offline" ? `${names}, offline stand-in` : names;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* plain text error */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => call<AppStatus>("/api/status"),
  room: () => call<{ where: string; items: RoomItem[] }>("/api/room"),
  deliverables: () => call<{ items: Deliverable[] }>("/api/deliverables"),
  detail: (key: string) => call<Detail>(`/api/deliverables/${key}`),
  approve: (key: string) => call(`/api/approve/${key}`, { method: "POST" }),
  sendBack: (key: string, note: string) =>
    call(`/api/send-back/${key}`, { method: "POST", body: JSON.stringify({ note }) }),
  post: (text: string, to?: string | null, thread?: number | null) =>
    call<{ id: number }>("/api/messages", {
      method: "POST", body: JSON.stringify({ text, to: to ?? null, thread: thread ?? null }),
    }),
  runDay: () => call("/api/run/day", { method: "POST" }),
  runWeek: () => call("/api/run/week", { method: "POST" }),
  settings: () => call<SettingsData>("/api/settings"),
};

/** A counter that goes up whenever the event log or the team's busy state changes. */
export function useLive(): { tick: number; busy: string | null } {
  const [state, setState] = useState({ tick: 0, busy: null as string | null });
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onmessage = (m) => {
      const data = JSON.parse(m.data);
      setState((s) => ({ tick: s.tick + 1, busy: data.busy }));
    };
    return () => es.close();
  }, []);
  return state;
}

/** Load `fn` now and again on every tick. Keeps the last good value while reloading. */
export function useData<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    fn().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

// Times. The demo week runs in seconds, so the simulated day carries the date and the clock
// shows when the step happened.
export function clock(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

export function when(ts: string | null | undefined, day: number | null | undefined): string {
  const t = clock(ts);
  return day ? `Day ${day} · ${t}` : t;
}

export function money(n: number | null | undefined): string {
  if (n == null) return "";
  return "$" + (Number.isInteger(n) ? n.toString() : n.toFixed(2));
}
