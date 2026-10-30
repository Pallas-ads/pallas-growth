import { type ReactNode } from "react";
import { api, useData, useLive } from "./api";
import { TEAM } from "./avatars";
import Deliverables from "./Deliverables";
import Detail from "./Detail";
import Room from "./Room";
import { useRoute, type Route } from "./route";
import Settings from "./Settings";
import { OWNER } from "./ui";

function Sidebar({ route, waiting }: { route: Route; waiting: number }) {
  const page = route.page === "detail" ? "deliverables" : route.page;
  const item = (key: string, label: string, extra?: ReactNode) => (
    <a className={page === key ? "nav-i on" : "nav-i"} href={`#/${key}`} style={{ textDecoration: "none" }}>
      <span>{label}</span>{extra}
    </a>
  );
  return (
    <aside className="col rule-r" style={{ background: "var(--sunken)", padding: "18px 12px", gap: 22 }}>
      <div className="mark" style={{ padding: "0 6px" }}>
        <img className="mark-logo" src="/pallas-logo.png" alt="Pallas" />
        <div><div className="mark-name">Growth Team</div><div className="mark-sub">Pallas Fit · sandbox</div></div>
      </div>
      <nav className="nav">
        {item("room", "Team Room")}
        {item("deliverables", "Deliverables", waiting > 0 &&
          <span className="nav-dot" title={`${waiting} ${waiting === 1 ? "item" : "items"} waiting on ${OWNER}`} />)}
        {item("settings", "Settings")}
      </nav>
      <div className="grow" />
      <div className="col" style={{ gap: 8, padding: "0 6px" }}>
        <div className="eyebrow">The team</div>
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          {TEAM.map((t) => <img key={t.code} className="av sm" src={`/avatars/${t.code}.svg`} alt={t.name} title={t.name} />)}
        </div>
      </div>
      <div className="row" style={{ gap: 10, padding: "0 6px" }}>
        <img className="av" src="/owner.jpg" alt={OWNER} />
        <div><div className="ink" style={{ fontWeight: 600 }}>{OWNER}</div>
          <div className="mono muted" style={{ fontSize: 11 }}>Owner · Oct 29 2026</div></div>
      </div>
    </aside>
  );
}

export default function App() {
  const route = useRoute();
  const { tick, busy } = useLive();
  const status = useData(api.status, [tick]);
  const list = useData(api.deliverables, [tick]);
  const room = useData(api.room, [tick]);
  const detail = useData(() => (route.key ? api.detail(route.key) : Promise.resolve(null)), [tick, route.key]);
  const items = list.data?.items ?? [];
  const waiting = items.filter((d) => d.status === "wait").length;
  const sidebar = <Sidebar route={route} waiting={waiting} />;

  let page: ReactNode;
  if (route.page === "deliverables") page = <Deliverables items={items} route={route} sidebar={sidebar} />;
  else if (route.page === "detail") {
    const d = detail.data && detail.data.key === route.key ? detail.data : null;
    page = <Detail d={d} error={detail.error} sidebar={sidebar} onChange={() => undefined} />;
  } else if (route.page === "settings") page = <Settings tick={tick} busy={busy} sidebar={sidebar} />;
  else page = <Room items={room.data?.items ?? []} where={status.data?.where ?? ""} busy={busy}
                    error={status.data?.error ?? null} route={route} sidebar={sidebar} />;
  return <div className="frame">{page}</div>;
}
