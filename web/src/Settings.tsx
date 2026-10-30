// Settings: the business facts the team works from, what the models cost, and the controls
// that move the simulated week forward.
import { Fragment, useState, type ReactNode } from "react";
import { api, money, useData } from "./api";

function rows(obj: Record<string, unknown>): [string, string][] {
  return Object.entries(obj).flatMap(([k, v]): [string, string][] => {
    const label = k.replace(/_/g, " ");
    if (v && typeof v === "object" && !Array.isArray(v)) return rows(v as Record<string, unknown>);
    if (Array.isArray(v)) return [[label, v.join("; ")]];
    if (k.endsWith("budget") || k.startsWith("target_cost") || k.endsWith("_cap")) return [[label, money(v as number)]];
    if (k.endsWith("_pct") || k.endsWith("_share")) return [[label, `${Math.round((v as number) * 100)}%`]];
    return [[label, String(v)]];
  });
}

export default function Settings({ tick, busy, sidebar }: { tick: number; busy: string | null; sidebar: ReactNode }) {
  const { data } = useData(api.settings, [tick]);
  const [error, setError] = useState<string | null>(null);
  const run = (fn: () => Promise<unknown>) => { setError(null); fn().catch((e: Error) => setError(e.message)); };
  return (
    <div className="app list">
      {sidebar}
      <main className="col scroll top" style={{ padding: "18px 28px", gap: 22, minWidth: 0 }}>
        <div>
          <div className="eyebrow">Pallas Fit sandbox{data?.day ? ` · day ${data.day}` : ""}</div>
          <div className="h1" style={{ marginTop: 4 }}>Settings</div>
        </div>
        <section className="section" style={{ maxWidth: 640 }}>
          <div className="h2">Simulation</div>
          <div className="muted" style={{ fontSize: 12.5 }}>
            The account is a synthetic sandbox. Each step runs the team on the {data?.offline ? "offline model, at no cost" : "configured model"}; nothing reaches a real ad account.
          </div>
          <div className="row" style={{ gap: 8, marginTop: 4 }}>
            <button className="btn ink" disabled={!!busy} onClick={() => run(api.runDay)}>Run the next day</button>
            <button className="btn ghost" disabled={!!busy} onClick={() => run(api.runWeek)}>Start the weekly review</button>
            {busy && <span className="status-bar"><span className="dot work" />{busy}</span>}
            {error && <span className="err" style={{ fontSize: 12 }}>{error}</span>}
          </div>
        </section>
        {data && (
          <>
            <section className="section" style={{ maxWidth: 640 }}>
              <div className="h2">Model use</div>
              <dl className="kv">
                <dt>Model calls</dt><dd>{data.model_calls}</dd>
                <dt>Tokens</dt><dd>{data.tokens.toLocaleString("en-US")}</dd>
                <dt>Cost{data.offline ? " (offline, priced as if live)" : ""}</dt><dd>${data.cost.toFixed(4)}</dd>
              </dl>
            </section>
            <section className="section" style={{ maxWidth: 820 }}>
              <div className="h2">Business facts · version {data.fact_version}</div>
              <dl className="kv facts">
                {rows(data.facts).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}
              </dl>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
