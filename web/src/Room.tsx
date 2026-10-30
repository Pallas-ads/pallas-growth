// 4a Team Room: conversation only. Team-internal steps fold into one line; output arrives as
// cards that open in Deliverables; a message's replies open in the thread on the right.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { clock, type Fold, type Msg, type RoomItem } from "./api";
import Composer from "./Composer";
import { go, type Route } from "./route";
import { ArtCard, Av, Stack, Text } from "./ui";

function names(list: string[]) {
  const b = list.map((n, i) => <b key={i}>{n}</b>);
  if (b.length <= 1) return b;
  const out: ReactNode[] = [];
  b.forEach((x, i) => {
    if (i) out.push(i === b.length - 1 ? " and " : ", ");
    out.push(x);
  });
  return out;
}

function span(f: Fold) {
  const a = clock(f.ts_from), b = clock(f.ts_to);
  return a === b ? a : `${a}–${b}`;
}

function FoldRow({ f, open, onToggle }: { f: Fold; open: boolean; onToggle: () => void }) {
  return (
    <>
      <button className="fold" id={`i-${f.id}`} onClick={onToggle} aria-expanded={open}>
        <Stack people={f.actors} />
        <span>{names(f.names)} {f.tail}</span>
        <span className="t">{span(f)}</span>
        <span className="more">{open ? "Hide" : `${f.items.length} messages`}</span>
      </button>
      {open && (
        <div className="fold-items">
          {f.items.map((l) => (
            <div className="th-rep" key={l.id} id={`i-${l.id}`}>
              <Av who={l.who} size="sm" />
              <div><div className="mh"><b>{l.who.name}</b><span className="t">{clock(l.ts)}</span></div>
                <div className="mb">{l.text}</div></div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function Thr({ m, open }: { m: Msg; open: boolean }) {
  const n = m.replies.length;
  const people = [...new Map(m.replies.map((r) => [r.who.actor, r.who])).values()];
  const label = n ? `${n} ${n === 1 ? "reply" : "replies"}` : "Reply in thread";
  return (
    <button className="thr" onClick={(e) => { e.stopPropagation(); go("room", { thread: m.id }); }}>
      {n > 0 && <Stack people={people} />}
      <span className="n">{label}</span>
      {n > 0 && <span className="t">last {clock(m.replies[n - 1].ts)}{open ? " · open on the right" : ""}</span>}
    </button>
  );
}

function MsgRow({ m, open, flash }: { m: Msg; open: boolean; flash: boolean }) {
  const cls = "msg2" + (open ? " open" : "") + (flash ? " flash" : "");
  return (
    <div className={cls} id={`i-${m.id}`}>
      <Av who={m.who} />
      <div>
        <div className="mh"><b>{m.who.name}</b><span className="t">{clock(m.ts)}</span></div>
        <div className="mb"><Text text={m.text} /></div>
        {m.card && <ArtCard card={m.card} />}
        {(m.replies.length > 0 || m.mention || m.card) && <Thr m={m} open={open} />}
      </div>
    </div>
  );
}

function Thread({ m, before, onClose }: { m: Msg; before: Fold | null; onClose: () => void }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [m.id, m.replies.length]);
  const subject = m.card ? m.card.title : m.text.replace(/^@\w+\s/, "").slice(0, 60);
  return (
    <aside className="col rule-l" style={{ padding: "18px 18px 16px", background: "var(--paper)" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="h2">Thread</span>
        <button className="btn ghost xs" onClick={onClose}>Close</button>
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>{subject}</div>
      <div className="grow scroll bottom">
        <div className="scroll-in">
          {before && (
            <>
              <div className="th-div">Before this</div>
              <div className="th-ctx">{names(before.names)} {before.tail}. <span className="mono" style={{ fontSize: 11 }}>{span(before)}</span></div>
            </>
          )}
          <div className="th-rep" style={{ marginTop: 6 }}>
            <Av who={m.who} size="sm" />
            <div>
              <div className="mh"><b>{m.who.name}</b><span className="t">{clock(m.ts)}</span></div>
              <div className="mb"><Text text={m.text} /></div>
              {m.card && <ArtCard card={m.card} />}
            </div>
          </div>
          <div className="th-div">{m.replies.length ? `${m.replies.length} ${m.replies.length === 1 ? "reply" : "replies"}` : "No replies yet"}</div>
          {m.replies.map((r) => (
            <div className="th-rep" key={r.id}>
              <Av who={r.who} size="sm" />
              <div><div className="mh"><b>{r.who.name}</b><span className="t">{clock(r.ts)}</span></div>
                <div className="mb"><Text text={r.text} /></div></div>
            </div>
          ))}
          <div ref={end} />
        </div>
      </div>
      <Composer small placeholder="Reply in thread" thread={m.id} />
    </aside>
  );
}

export default function Room({ items, where, busy, error, route, sidebar }: {
  items: RoomItem[]; where: string; busy: string | null; error: string | null; route: Route;
  sidebar: ReactNode;
}) {
  const tab = route.q.get("tab") === "mentions" ? "mentions" : "all";
  const threadId = Number(route.q.get("thread")) || null;
  const focus = Number(route.q.get("focus")) || null;
  const [openFolds, setOpenFolds] = useState<Set<number>>(new Set());
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const mentions = items.filter((i): i is Msg => i.kind === "msg" && i.mention);
  const shown = tab === "mentions" ? mentions : items;
  const thread = items.find((i): i is Msg => i.kind === "msg" && i.id === threadId) ?? null;
  const before = useMemo(() => {
    if (!thread) return null;
    const at = items.indexOf(thread);
    for (let i = at - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === "fold") return it;
      if (it.kind !== "day") return null;
    }
    return null;
  }, [items, thread]);

  // Open the fold that holds a focused step.
  useEffect(() => {
    if (!focus) return;
    const f = items.find((i): i is Fold => i.kind === "fold" && i.ids.includes(focus) && i.id !== focus);
    if (f) setOpenFolds((s) => new Set(s).add(f.id));
  }, [focus, items]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (focus) {
      document.getElementById(`i-${focus}`)?.scrollIntoView({ block: "center" });
      return;
    }
    if (stick.current) el.scrollTop = el.scrollHeight;
  }, [shown.length, items, focus, openFolds]);

  const toggle = (id: number) => setOpenFolds((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  return (
    <div className={"app" + (thread ? " room with-thread" : " room")}>
      {sidebar}
      <main className="col" style={{ padding: "20px 26px 18px", minWidth: 0 }}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
          <div>
            <div className="eyebrow">{where}</div>
            <div className="h1" style={{ marginTop: 4 }}>Team Room</div>
          </div>
          <div className="row" style={{ gap: 14 }}>
            {busy && <span className="status-bar"><span className="dot work" />{busy}</span>}
            {!busy && error && <span className="status-bar err">{error}</span>}
            <div className="seg" role="tablist">
              <button className={tab === "all" ? "on" : ""} onClick={() => go("room", { thread: threadId })}>All</button>
              <button className={tab === "mentions" ? "on" : ""} onClick={() => go("room", { tab: "mentions", thread: threadId })}>
                Mentions <span className="mono" style={{ fontSize: 10.5 }}>{mentions.length}</span>
              </button>
            </div>
          </div>
        </div>
        <div className="grow scroll bottom" ref={scroller}
             onScroll={(e) => {
               const el = e.currentTarget;
               stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
             }}>
          <div className="scroll-in">
            {shown.length === 0 && <div className="empty">{tab === "mentions" ? "Nobody has mentioned you yet." : "The team is setting up the demo week."}</div>}
            {shown.map((it) => {
              if (it.kind === "day") return <div className="daydiv" key={`d-${it.id}`}>{it.label}</div>;
              if (it.kind === "fold") return <FoldRow key={`f-${it.id}`} f={it} open={openFolds.has(it.id)} onToggle={() => toggle(it.id)} />;
              return <MsgRow key={`m-${it.id}`} m={it} open={it.id === threadId} flash={it.id === focus} />;
            })}
          </div>
        </div>
        <Composer placeholder="Message the team, or type @ to address one role"
                  onSent={() => { stick.current = true; }} />
      </main>
      {thread && <Thread m={thread} before={before} onClose={() => go("room", { tab: tab === "mentions" ? "mentions" : null })} />}
    </div>
  );
}
