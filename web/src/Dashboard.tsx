// 5a Dashboard: the sandbox account's numbers, read from the same Insights-shaped rows the
// Analyst reads, with the team's actions pinned to the days they happened.
import { Fragment, useState, type ReactNode } from "react";
import { api, clock, useData, type Action, type DashboardData, type DraftNode, type Kpi, type Metrics } from "./api";
import { go, type Route } from "./route";

type Ready = Extract<DashboardData, { ready: true }>;

const usd = (n: number | null | undefined, digits = 2) =>
  n == null ? "–" : "$" + n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const int = (n: number | null | undefined) => (n == null ? "–" : n.toLocaleString("en-US"));
const pct = (n: number | null | undefined) => (n == null ? "–" : (n * 100).toFixed(2) + "%");

function fmt(k: Kpi): string {
  if (k.fmt === "money") return usd(k.value);
  if (k.fmt === "pct") return pct(k.value);
  return int(k.value);
}

function Tile({ k, d, open, onOpen }: { k: Kpi; d: Ready; open: boolean; onOpen: (key: string | null) => void }) {
  const ev = d.evidence;
  return (
    <button className={"kpi" + (k.hero ? " hero" : "")} aria-expanded={open}
            onMouseEnter={() => onOpen(k.key)} onMouseLeave={() => onOpen(null)}
            onFocus={() => onOpen(k.key)} onBlur={() => onOpen(null)}>
      <span className="k">{k.label}</span>
      <span className="v">{fmt(k)}</span>
      <span className={"d " + k.tone}>{k.sub}</span>
      {open && (
        <span className="tip" onClick={(e) => e.stopPropagation()}>
          <span className="m">{ev.rows} Insights rows · {ev.first} to {ev.last}</span>
          <span style={{ display: "block", marginTop: 5 }}>
            <b>{fmt(k)}</b> = {k.formula}, days {d.start} to {d.end}, every ad in the account.
          </span>
          <span className="m" style={{ display: "block", marginTop: 6 }}>
            fields: {ev.fields} · level={ev.level} · time_increment=1
          </span>
          {ev.used_in && (
            <span style={{ display: "block", marginTop: 7 }}>
              The Analyst read these rows for{" "}
              <a href={`#/deliverables/${ev.used_in.key}`} onMouseDown={(e) => e.preventDefault()}>{ev.used_in.title}</a>
            </span>
          )}
        </span>
      )}
    </button>
  );
}

/** Cost per result by day (line) over spend by day (bars, against the daily cap), with the
 *  team's actions pinned on the days they happened. */
function Chart({ d }: { d: Ready }) {
  const W = 560, H = 168, L = 38, R = 10, T = 12, B = 26;
  const pw = W - L - R, ph = H - T - B;
  const s = d.series;
  const vals = [...s.map((x) => x.cpr).filter((v): v is number => v != null), d.target];
  let lo = Math.floor(Math.min(...vals) * 2) / 2 - 0.5;
  let hi = Math.ceil(Math.max(...vals) * 2) / 2 + 0.5;
  lo = Math.max(0, lo);
  if (hi - lo < 1) hi = lo + 1;
  const step = (hi - lo) / 4;
  const ticks = [0, 1, 2, 3, 4].map((i) => lo + i * step);
  const n = s.length;
  const x = (i: number) => L + (pw * (i + 0.5)) / n;
  const y = (v: number) => T + (ph * (hi - v)) / (hi - lo);
  const bw = Math.min(28, (pw / n) * 0.46);
  const pts = s.map((p, i) => (p.cpr == null ? null : ([x(i), y(p.cpr)] as const)));
  const line = pts.filter(Boolean).map((p) => `${p![0].toFixed(1)},${p![1].toFixed(1)}`).join(" ");
  const pins = new Map<number, Action[]>();
  for (const a of d.actions) pins.set(a.day, [...(pins.get(a.day) ?? []), a]);
  const every = n > 14 ? 4 : n > 7 ? 2 : 1;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img"
         aria-label={`Cost per ${d.result_name} by day, days ${d.start} to ${d.end}, against the ${usd(d.target)} target`}>
      {s.map((p, i) => {
        const bh = (ph * 0.38 * Math.min(p.spend, d.daily_cap)) / d.daily_cap;
        return <rect key={p.day} x={x(i) - bw / 2} y={T + ph - bh} width={bw} height={bh} fill="#dde1e9">
          <title>Day {p.day}: {usd(p.spend)} spend, {p.results} {d.result_name}s</title></rect>;
      })}
      {ticks.map((v) => (
        <g key={v}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="#eef0f4" />
          <text x={L - 6} y={y(v) + 3.5} textAnchor="end" fontFamily="JetBrains Mono" fontSize="9.5" fill="#6b7182">{usd(v)}</text>
        </g>
      ))}
      <line x1={L} x2={W - R} y1={y(d.target)} y2={y(d.target)} stroke="oklch(0.66 0.17 52)" strokeWidth="1.5" strokeDasharray="4 3" />
      <text x={W - R - 4} y={y(d.target) + 12} textAnchor="end" fontFamily="JetBrains Mono" fontSize="9.5" fill="oklch(0.47 0.13 50)">target {usd(d.target)}</text>
      {[...pins.entries()].map(([day, list]) => {
        const i = s.findIndex((p) => p.day === day);
        if (i < 0) return null;
        return (
          <g key={day}>
            <line x1={x(i)} x2={x(i)} y1={T} y2={T + ph} stroke="#075bff" strokeWidth="1" strokeDasharray="2 2" />
            {list.map((a, j) => (
              <g key={a.n}>
                <circle cx={x(i)} cy={T + 7 + j * 17} r="7" fill="#075bff" />
                <text x={x(i)} y={T + 10.3 + j * 17} textAnchor="middle" fontFamily="JetBrains Mono" fontWeight="600" fontSize="9" fill="#fff">{a.n}</text>
              </g>
            ))}
          </g>
        );
      })}
      <polyline fill="none" stroke="#191f2b" strokeWidth="2" strokeLinejoin="round" points={line} />
      {pts.map((p, i) => p && <circle key={i} cx={p[0]} cy={p[1]} r="2.8" fill="#191f2b">
        <title>Day {s[i].day}: {usd(s[i].cpr)} per {d.result_name}</title></circle>)}
      {s.map((p, i) => (i % every === 0 || i === n - 1) && (
        <text key={p.day} x={x(i)} y={H - 8} textAnchor="middle" fontFamily="JetBrains Mono" fontSize="9.5" fill="#6b7182">Day {p.day}</text>
      ))}
    </svg>
  );
}

function Chip({ id }: { id: string }) {
  return <span className="chip" title="Evidence id: one Insights row, or a figure computed from them">{id}</span>;
}

function cells(m: Metrics) {
  const none = m.impressions === 0;
  return (
    <>
      <td className="num">{none ? "–" : usd(m.spend)}</td>
      <td className="num">{none ? "–" : int(m.impressions)}</td>
      <td className="num">{none ? "–" : pct(m.ctr)}</td>
      <td className="num">{none ? "–" : int(m.results)}</td>
      <td className={"num " + m.tone}>{none ? "–" : usd(m.cpr)}</td>
    </>
  );
}

function Table({ d }: { d: Ready }) {
  const name = d.result_name.charAt(0).toUpperCase() + d.result_name.slice(1);
  return (
    <table className="dtbl">
      <thead><tr><th>Name</th><th>Status</th><th className="num">Budget/day</th><th className="num">Spend</th>
        <th className="num">Impr.</th><th className="num">CTR</th><th className="num">{name}s</th>
        <th className="num">Cost/{d.result_name}</th><th className="num" style={{ paddingRight: 0 }}>Evidence</th></tr></thead>
      <tbody>
        {d.campaigns.map((c) => [
          <tr className="cmp" key={c.id}>
            <td>{c.name} <span className="mono muted" style={{ fontSize: 10.5, fontWeight: 400 }}>{c.id}</span>
              {c.in_learning && <span className="mono muted" style={{ fontSize: 10.5, fontWeight: 400 }}> · learning</span>}</td>
            <td><span className={"lbl " + c.status}>{c.status_label}</span></td>
            <td className="num">{usd(c.budget, 0)}</td>
            {cells(c)}
            <td style={{ textAlign: "right", paddingRight: 0 }}>
              <span className="mono muted" style={{ fontSize: 10.5 }}>{c.rows} rows</span></td>
          </tr>,
          ...c.ads.map((a) => (
            <tr className="sub" key={a.id}>
              <td>{a.name} <span className="mono muted" style={{ fontSize: 10.5 }}>{a.id}</span></td>
              <td><span className={"lbl " + a.status}>{a.status_label}</span></td>
              <td className="num" />
              {cells(a)}
              <td style={{ textAlign: "right", paddingRight: 0 }}>
                {a.evidence ? <Chip id={a.evidence} />
                  : a.waits ? <span className="mono muted" style={{ fontSize: 10.5 }}>waits on you</span> : null}
              </td>
            </tr>
          )),
        ])}
      </tbody>
    </table>
  );
}

function Actions({ d }: { d: Ready }) {
  return (
    <div className="card col" style={{ padding: "12px 14px 6px", minWidth: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <span className="h3">What the team did</span>
        <span className="mono muted" style={{ fontSize: 10.5 }}>{d.actions.length} {d.actions.length === 1 ? "action" : "actions"}</span>
      </div>
      {d.actions.length === 0 && <div className="muted" style={{ fontSize: 12, padding: "6px 0" }}>Nothing flagged or changed in these days.</div>}
      {d.actions.map((a) => (
        <div className="ann" key={a.n}>
          <span className="n">{a.n}</span>
          <span className="w">Day {a.day}</span>
          <span><b>{a.who.name}</b> {a.verb && a.verb + " "}{a.text}
            {a.link && <> <a href={`#/deliverables/${a.link.key}`} style={{ fontWeight: 500 }}>{a.link.title}</a></>}
            {!a.link && a.evidence.slice(0, 2).map((id) => <Fragment key={id}> <Chip id={id} /></Fragment>)}</span>
        </div>
      ))}
      <div className="grow" />
      {d.after && <div className="mono muted" style={{ fontSize: 10.5, padding: "6px 0 8px", borderTop: "1px solid var(--rule)" }}>{d.after}</div>}
    </div>
  );
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

function DraftRow({ n, depth }: { n: DraftNode; depth: number }) {
  const details = Object.entries(n.details).slice(0, 4);
  return (
    <>
      <div className="draft" style={{ paddingLeft: depth * 18 }}>
        <span className="lvl">{n.level}</span>
        <span className="nm">{n.name || <span className="muted">Untitled</span>}
          {n.id && <span className="mono muted" style={{ fontSize: 10.5 }}> {n.id}</span>}</span>
        {n.status && <span className="lbl">{n.status.toLowerCase()}</span>}
        <span className="mono muted dt">
          {details.map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(" · ")}
          {n.nested.length > 0 && `${details.length ? " · " : ""}${n.nested.map((k) => k.replace(/_/g, " ")).join(", ")} set`}
        </span>
      </div>
      {n.children.map((c, i) => <DraftRow key={`${c.id}-${i}`} n={c} depth={depth + 1} />)}
    </>
  );
}

/** Unpublished drafts staged in Ads Manager, as the Ads MCP returned them. PGT never edits them. */
function Drafts({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.drafts(), [tick]);
  const c = data?.counts;
  return (
    <div className="card" style={{ padding: "12px 14px 8px" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6, flexWrap: "wrap", gap: 8 }}>
        <span className="h3">Drafts in Ads Manager</span>
        <span className="mono muted" style={{ fontSize: 10.5 }}>
          {c ? `${plural(c["campaign"] ?? 0, "campaign")} · ${plural(c["ad set"] ?? 0, "ad set")} · ${plural(c["ad"] ?? 0, "ad")} · ` : ""}
          read only, not published</span>
      </div>
      {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
      {data && !data.available && <div className="muted" style={{ fontSize: 12, padding: "4px 0" }}>Drafts could not be read: {data.reason}</div>}
      {data?.available && data.drafts.length === 0 && <div className="muted" style={{ fontSize: 12, padding: "4px 0" }}>No drafts are waiting in Ads Manager.</div>}
      {data?.drafts.map((d, i) => <DraftRow key={`${d.id}-${i}`} n={d} depth={0} />)}
    </div>
  );
}

export default function Dashboard({ tick, route, sidebar }: { tick: number; route: Route; sidebar: ReactNode }) {
  const days = [7, 14, 28].includes(Number(route.q.get("days"))) ? Number(route.q.get("days")) : 7;
  const { data, error } = useData(() => api.dashboard(days), [tick, days]);
  const [tip, setTip] = useState<string | null>(null);
  const [how, setHow] = useState(false);
  const d = data && data.ready && !(data.real && data.campaigns.length === 0) ? data : null;
  return (
    <div className="app list">
      {sidebar}
      <main className="col scroll top" style={{ padding: "16px 26px 18px", gap: 10, minWidth: 0 }}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div>
            <div className="eyebrow">{data?.account_name ?? "Pallas Fit sandbox"}{d ? ` · days ${d.start} to ${d.end}` : ""}</div>
            <div className="h1" style={{ marginTop: 4 }}>Dashboard</div>
          </div>
          <div className="row" style={{ gap: 10 }}>
            {d?.pulled && <span className="mono muted" style={{ fontSize: 11 }}>Pulled by Analyst · day {d.pulled.day}, {clock(d.pulled.ts)}</span>}
            <div className="seg" role="tablist">
              {[7, 14, 28].map((n) => (
                <button key={n} className={n === days ? "on" : ""} onClick={() => go("dashboard", { days: n === 7 ? null : n })}>{n} days</button>
              ))}
            </div>
          </div>
        </div>
        {data?.real ? (
          <div className="synth">
            <span className="k">Real account</span>
            <span><span className="mono ink">{data.account}</span> · read through Meta's Ads MCP · stays on this computer</span>
          </div>
        ) : (
        <div className="synth">
          <span className="k">Synthetic</span>
          <span>Sandbox account <span className="mono ink">{data?.account ?? "act_sandbox_0001"}</span> · Marketing API Insights shape · no real money or people</span>
          <div className="grow" />
          <button className="btn ghost xs" onClick={() => setHow(!how)} aria-expanded={how}>How this is generated</button>
          {how && (
            <div style={{ flexBasis: "100%", fontSize: 12, lineHeight: 1.5, color: "var(--text)" }}>
              A seeded simulator plays out each ad's delivery one day at a time: impressions, clicks,
              {" "}{d?.result_name ?? "result"}s and spend, with fatigue as frequency rises. Each row has the fields
              {" "}of a Marketing API Insights call at ad level with time_increment=1, and an evidence id the
              {" "}team cites. The Analyst reads these same rows; the Operator's approved changes feed back into
              {" "}the next day's delivery.
            </div>
          )}
        </div>
        )}
        {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
        {data?.real && <Drafts tick={tick} />}
        {data?.real && !d && <div className="empty">Nothing is live on this account, so there are no numbers yet. Daily numbers appear once an ad set runs.</div>}
        {data && !data.ready && !data.real && <div className="empty">The team is setting up the demo week.</div>}
        {d && (
          <>
            {d.short && <div className="muted" style={{ fontSize: 12 }}>
              Only {d.end} {d.end === 1 ? "day has" : "days have"} run, so this shows days {d.start} to {d.end}.</div>}
            <div className="kpis">
              {d.kpis.map((k) => <Tile key={k.key} k={k} d={d} open={tip === k.key} onOpen={setTip} />)}
            </div>
            <div className="dash-row">
              <div className="card" style={{ minWidth: 0 }}>
                <div className="chart-wrap">
                  <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
                    <span className="h3">Cost per {d.result_name}, by day</span>
                    <div className="legend"><span><i />Cost per {d.result_name}</span><span><i className="t" />Target</span>
                      <span><i className="b" />Spend</span><span><i className="p" />Team action</span></div>
                  </div>
                  <Chart d={d} />
                </div>
              </div>
              <Actions d={d} />
            </div>
            <div className="card" style={{ padding: "12px 14px 4px", overflowX: "auto" }}>
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                <span className="h3">Campaigns and ads</span>
                <span className="mono muted" style={{ fontSize: 10.5 }}>
                  Daily total {usd(d.daily_total, 0)} of the {usd(d.daily_cap, 0)} house cap</span>
              </div>
              <Table d={d} />
            </div>
          </>
        )}
      </main>
    </div>
  );
}
