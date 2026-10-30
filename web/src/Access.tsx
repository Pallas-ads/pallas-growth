// The Meta connection: which ad account the team works on, how much it may do there (L0 to L2),
// and the house rules every change set is checked against in code. The first-run wizard sets all
// three. The public demo only offers the labelled sandbox; the real Facebook Login for Business
// runs on facebook.com, from a local install.
import { useEffect, useState } from "react";
import { api, money, type AccessData, type Account, type Rules, type RulesPatch } from "./api";
import { go } from "./route";
import { OWNER } from "./ui";

function date(ts: string) {
  return new Date(ts).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function useAct(onDone: () => void) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const act = (fn: () => Promise<unknown>) => {
    setError(null);
    setBusy(true);
    fn().then(onDone).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  return { act, error, busy };
}

// ---------------------------------------------------------------- connection and levels

export function Connection({ a, onChange }: { a: AccessData; onChange: () => void }) {
  const { act, error, busy } = useAct(onChange);
  const c = a.connection;
  const real = c?.kind === "meta";
  return (
    <div className="col" style={{ gap: 18 }}>
      {c ? (
        <div className="card conn">
          <div className="ic">act</div>
          <div style={{ minWidth: 0 }}>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <span className="h3">{c.name}</span>
              <span className="mono muted" style={{ fontSize: 11 }}>{c.account_id}</span>
              <span className="okdot"><i />Connected</span>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
              {real ? "Meta ad account, read-only in this build." : "Sandbox connection, synthetic data."}{" "}
              Connected by {c.by === "user" ? OWNER : c.by} on {date(c.ts)} · {c.currency} · {c.timezone}
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn ghost xs" onClick={() => go("settings", { s: "connection", setup: 1 })}>Switch account</button>
            <button className="btn ghost xs" disabled={busy} onClick={() => act(api.disconnect)}>Disconnect</button>
          </div>
        </div>
      ) : (
        <div className="card conn">
          <div className="ic off">act</div>
          <div>
            <div className="h3">No ad account connected</div>
            <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
              The team keeps what it already made, but runs nothing new until an account is connected.
            </div>
          </div>
          <button className="btn pri xs" onClick={() => go("settings", { s: "connection", setup: 1 })}>Set up</button>
        </div>
      )}
      <div>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8, gap: 12, flexWrap: "wrap" }}>
          <span className="h3">How much the team may do</span>
          <span className="mono muted" style={{ fontSize: 10.5 }}>Changing this is logged in the room</span>
        </div>
        <div className="lvls">
          {a.levels.map((l) => {
            const locked = real && l.level > 1;
            return (
              <button key={l.level} className={"lvl" + (a.level === l.level ? " on" : "")}
                      disabled={busy || locked || a.level === l.level} aria-pressed={a.level === l.level}
                      onClick={() => act(() => api.setLevel(l.level))}>
                <div className="top"><span className="rd" /><span className="code">{l.code}</span><span className="n">{l.name}</span></div>
                <div className="d">{locked ? "Not available for a real account in this build." : l.text}</div>
                <div className="sc">{l.scopes.join(", ")}</div>
              </button>
            );
          })}
        </div>
        {error && <div className="err" style={{ fontSize: 12, marginTop: 8 }}>{error}</div>}
      </div>
      <div className="note">
        <b>Checked twice.</b> The server refuses approvals below L1, and the Operator refuses to write
        below L2 even when it holds a valid approval. At L1 an approved change set is handed back to
        you to apply in Ads Manager; nothing is written.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- house rules

const PRESET_NAMES = ["conservative", "balanced", "aggressive"] as const;

type Draft = { cap: string; pct: string; days: string; mult: string; learn: boolean };

function draftOf(r: Rules): Draft {
  return {
    cap: String(r.daily_budget_cap), pct: String(Math.round(r.max_budget_change_pct * 100)),
    days: String(r.min_days_between_budget_changes), mult: String(r.min_spend_multiple_before_pause),
    learn: r.protect_learning_phase,
  };
}

function patchOf(d: Draft, r: Rules): RulesPatch | string {
  const cap = Number(d.cap), pct = Number(d.pct) / 100, days = Number(d.days), mult = Number(d.mult);
  if (!(cap > 0)) return "The daily cap must be above $0";
  if (!(pct > 0 && pct <= 1)) return "The largest budget change must be between 1% and 100%";
  if (!(Number.isInteger(days) && days >= 0 && days <= 30)) return "Days between changes must be 0 to 30";
  if (!(mult >= 0 && mult <= 10)) return "Spend before pausing must be 0 to 10 times the target";
  const out: RulesPatch = {};
  if (cap !== r.daily_budget_cap) out.daily_budget_cap = cap;
  if (Math.abs(pct - r.max_budget_change_pct) > 1e-9) out.max_budget_change_pct = pct;
  if (days !== r.min_days_between_budget_changes) out.min_days_between_budget_changes = days;
  if (mult !== r.min_spend_multiple_before_pause) out.min_spend_multiple_before_pause = mult;
  if (d.learn !== r.protect_learning_phase) out.protect_learning_phase = d.learn;
  return out;
}

function Field({ value, onChange, pre, post, label }: {
  value: string; onChange: (v: string) => void; pre?: string; post?: string; label: string;
}) {
  return (
    <span className="field">
      {pre}<input inputMode="decimal" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} />{post}
    </span>
  );
}

export function HouseRulesForm({ a, onChange }: { a: AccessData; onChange: () => void }) {
  const { act, error, busy } = useAct(onChange);
  const [d, setD] = useState(() => draftOf(a.rules));
  const key = JSON.stringify(a.rules);
  useEffect(() => setD(draftOf(a.rules)), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const patch = patchOf(d, a.rules);
  const dirty = typeof patch === "string" || Object.keys(patch).length > 0;
  const set = (k: keyof Draft) => (v: string | boolean) => setD({ ...d, [k]: v });
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div className="seg" role="group" aria-label="Preset">
          {PRESET_NAMES.map((p) => (
            <button key={p} className={a.rules.preset === p ? "on" : ""} disabled={busy}
                    onClick={() => act(() => api.houseRules({ preset: p }))}>{p[0].toUpperCase() + p.slice(1)}</button>
          ))}
          {a.rules.preset === "custom" && <button className="on" disabled>Custom</button>}
        </div>
        <span className="mono muted" style={{ fontSize: 10.5 }}>Checked in code on every change set</span>
      </div>
      <div className="rules">
        <div className="r">Daily total budget cap<small>Across active campaigns. The team can never propose a day above this</small></div>
        <div className="v"><Field label="Daily total budget cap" pre="$" value={d.cap} onChange={set("cap")} /></div>
        <div className="r">Largest budget change in one step<small>Per campaign, up or down</small></div>
        <div className="v"><Field label="Largest budget change" post="%" value={d.pct} onChange={set("pct")} /></div>
        <div className="r">Days between budget changes<small>Per campaign, so Meta's delivery can settle</small></div>
        <div className="v"><Field label="Days between budget changes" post=" days" value={d.days} onChange={set("days")} /></div>
        <div className="r">Spend before an ad may be paused<small>As a multiple of the target cost per result</small></div>
        <div className="v"><Field label="Spend before pausing" post=" x target" value={d.mult} onChange={set("mult")} /></div>
        <div className="r">Leave campaigns in learning alone<small>No budget changes while Meta is still learning</small></div>
        <div className="v"><button className={"tog" + (d.learn ? "" : " off")} role="switch" aria-checked={d.learn}
                                   aria-label="Leave campaigns in learning alone" onClick={() => set("learn")(!d.learn)} /></div>
        <div className="r">New ads start paused<small>Fixed. The Operator cannot turn a new ad on</small></div>
        <div className="v"><span className="lock-t">fixed</span><span className="tog lock" /></div>
        <div className="r">Every number in an ad needs evidence<small>Fixed. Checked in code before the Reviewer sees it</small></div>
        <div className="v"><span className="lock-t">fixed</span><span className="tog lock" /></div>
        <div className="r">Weekly budget<small>From Business facts. Part of it is kept for creative tests</small></div>
        <div className="v">{money(a.weekly_budget)} · {money(a.weekly_test_budget)} for tests</div>
        <div className="r">What ads may never say<small>From Business facts</small></div>
        <div className="v" style={{ whiteSpace: "normal", maxWidth: 320 }}>{a.constraints.join("; ")}</div>
      </div>
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <button className="btn ink xs" disabled={busy || !dirty || typeof patch === "string"}
                onClick={() => typeof patch !== "string" && act(() => api.houseRules(patch))}>Save rules</button>
        {dirty && <button className="btn ghost xs" disabled={busy} onClick={() => setD(draftOf(a.rules))}>Undo</button>}
        <span className={typeof patch === "string" || error ? "err" : "muted"} style={{ fontSize: 12 }}>
          {typeof patch === "string" ? patch : error ??
            `Facts version ${a.fact_version}. Saving makes a new version; work built on the old rules is marked stale.`}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- first-run wizard

const STEPS = ["Ad account", "Pick account", "How much the team may do", "House rules"];

export function Wizard({ a, start, onClose, onDone }: {
  a: AccessData; start: number; onClose: () => void; onDone: () => void;
}) {
  const [step, setStep] = useState(start);
  const [kind, setKind] = useState<"sandbox" | "meta">(start > 0 && a.meta_accounts.length ? "meta" : "sandbox");
  const [acct, setAcct] = useState<Account | null>(
    kind === "meta" ? a.meta_accounts[0] ?? null : a.sandbox);
  const [level, setLevel] = useState(kind === "meta" ? 1 : 2);
  const [cap, setCap] = useState(String(a.rules.daily_budget_cap));
  const [pct, setPct] = useState(String(Math.round(a.rules.max_budget_change_pct * 100)));
  const { act, error, busy } = useAct(onDone);
  const metaOff = a.meta_login;
  const pick = (k: "sandbox" | "meta") => {
    setKind(k);
    setAcct(k === "sandbox" ? a.sandbox : a.meta_accounts[0] ?? null);
    setLevel(k === "sandbox" ? 2 : 1);
  };
  const next = () => {
    if (step === 0 && kind === "meta" && !a.meta_accounts.length) {
      window.location.href = "/api/meta/login"; // Facebook Login for Business on facebook.com
      return;
    }
    setStep(step + 1);
  };
  const finish = () => {
    const rules: RulesPatch = {};
    const c = Number(cap), p = Number(pct) / 100;
    if (c > 0 && c !== a.rules.daily_budget_cap) rules.daily_budget_cap = c;
    if (p > 0 && p <= 1 && Math.abs(p - a.rules.max_budget_change_pct) > 1e-9) rules.max_budget_change_pct = p;
    act(() => api.connect({ kind, account_id: acct?.account_id, level,
                            rules: Object.keys(rules).length ? rules : undefined }));
  };
  const sub = step === 0 ? "First run · about 2 minutes" : `${acct?.name ?? ""} · ${acct?.account_id ?? ""}`;
  return (
    <div className="wiz-bg" role="dialog" aria-modal="true" aria-labelledby="wiz-h">
      <div className="wiz">
        <div className="row" style={{ gap: 10 }}>
          <img className="mark-logo" src="/pallas-logo.png" alt="Pallas" />
          <div><div className="mark-name">Set up your Growth Team</div><div className="mark-sub">{sub}</div></div>
        </div>
        <div className="steps">
          {STEPS.map((n, i) => <div key={n} className={i < step ? "done" : i === step ? "now" : ""}>{i + 1}. {n}</div>)}
        </div>

        {step === 0 && (<>
          <div><div className="h2" id="wiz-h">Which ad account should the team work on?</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>The team reads results every morning and proposes changes. You approve everything that spends money.</div></div>
          <div className="col" style={{ gap: 10 }}>
            <button className={"pick" + (kind === "sandbox" ? " on" : "")} onClick={() => pick("sandbox")}>
              <span className="rd" /><div><div className="n">Use the sandbox account <span className="lbl src">Demo</span></div>
                <div className="d">A synthetic fitness app, Pallas Fit, with a week of results already in. No Meta login, nothing real is touched. This is what the public demo runs on.</div></div>
            </button>
            <button className={"pick" + (kind === "meta" ? " on" : "") + (metaOff ? " dis" : "")} disabled={!!metaOff} onClick={() => pick("meta")}>
              <span className="rd" /><div><div className="n">Connect a Meta ad account <span className="lbl paused">Local only</span></div>
                <div className="d">Opens Facebook Login for Business on facebook.com. You choose the ad account and grant access there; PGT never sees your password.{metaOff ? ` ${metaOff}` : ""}</div></div>
            </button>
          </div>
          <div className="note"><b>What PGT asks Meta for:</b> <span className="mono">ads_read</span> to read results, and <span className="mono">ads_management</span> only at L2. You can disconnect in Settings at any time.</div>
        </>)}

        {step === 1 && (<>
          <div><div className="h2" id="wiz-h">Pick the account</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              {kind === "sandbox" ? "The sandbox has one account. Its numbers are generated, and so is every result after a change."
                : "These are the ad accounts you granted on facebook.com. This build reads them only."}</div></div>
          <div className="col" style={{ gap: 8 }}>
            {(kind === "sandbox" ? [a.sandbox] : a.meta_accounts).map((x) => (
              <button key={x.account_id} className={"pick" + (acct?.account_id === x.account_id ? " on" : "")} onClick={() => setAcct(x)}>
                <span className="rd" /><div><div className="n">{x.name} <span className="mono muted" style={{ fontSize: 11, fontWeight: 400 }}>{x.account_id}</span></div>
                  <div className="d">{x.currency} · {x.timezone}{x.kind === "sandbox" ? " · synthetic data" : ""}</div></div>
              </button>
            ))}
          </div>
        </>)}

        {step === 2 && (<>
          <div><div className="h2" id="wiz-h">How much may the team do?</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>You can raise or lower this later in Settings. Every level keeps you as the only one who approves.</div></div>
          <div className="col" style={{ gap: 8 }}>
            {a.levels.map((l) => {
              const off = kind === "meta" && l.level > 1;
              const tag = kind === "meta" ? (l.level === 1 ? "Recommended to start" : null)
                : (l.level === 2 ? "Sandbox default" : null);
              return (
                <button key={l.level} className={"pick" + (level === l.level ? " on" : "") + (off ? " dis" : "")}
                        disabled={off} onClick={() => setLevel(l.level)}>
                  <span className="rd" /><div>
                    <div className="n"><span className="mono" style={{ fontSize: 11, color: level === l.level ? "var(--blue)" : "var(--muted)" }}>{l.code}</span> {l.name}
                      {tag && <span className="lbl review">{tag}</span>}</div>
                    <div className="d">{off ? "Not available for a real account in this build: the team only writes to the sandbox." : l.text}
                      <span className="sc">{l.scopes.join(", ")}</span></div></div>
                </button>
              );
            })}
          </div>
        </>)}

        {step === 3 && (<>
          <div><div className="h2" id="wiz-h">House rules</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>Checked in code on every change set, before you see it. The full list is in Settings.</div></div>
          <div className="rules">
            <div className="r">Daily total budget cap<small>The team can never propose a day above this</small></div>
            <div className="v"><Field label="Daily total budget cap" pre="$" value={cap} onChange={setCap} /></div>
            <div className="r">Largest budget change in one step<small>Per campaign, up or down</small></div>
            <div className="v"><Field label="Largest budget change" post="%" value={pct} onChange={setPct} /></div>
            <div className="r">New ads start paused<small>Fixed</small></div>
            <div className="v"><span className="lock-t">fixed</span><span className="tog lock" /></div>
            <div className="r">What ads may never say<small>From Business facts</small></div>
            <div className="v" style={{ whiteSpace: "normal", maxWidth: 300 }}>{a.constraints.join("; ")}</div>
          </div>
        </>)}

        {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
        <div className="row" style={{ gap: 8, justifyContent: "space-between" }}>
          <span className="mono muted" style={{ fontSize: 11 }}>
            {step < 3 ? `Next: ${STEPS[step + 1].toLowerCase()}` : "You can change all of this in Settings"}</span>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn ghost" disabled={busy} onClick={() => (step === 0 ? onClose() : setStep(step - 1))}>
              {step === 0 ? "Not now" : "Back"}</button>
            {step < 3
              ? <button className="btn pri" disabled={!acct && step > 0} onClick={next}>
                  {step === 0 ? (kind === "sandbox" ? "Continue with the sandbox" : "Continue to facebook.com") : "Continue"}</button>
              : <button className="btn pri" disabled={busy} onClick={finish}>Start the team</button>}
          </div>
        </div>
      </div>
    </div>
  );
}
