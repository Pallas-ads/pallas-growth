// The message box. Typing @ opens a list of roles; picking one addresses the message to it.
import { useRef, useState, type KeyboardEvent } from "react";
import { ADDRESSABLE } from "./avatars";
import { api } from "./api";
import { Av } from "./ui";

type Props = { placeholder: string; thread?: number | null; small?: boolean; onSent?: () => void };

export default function Composer({ placeholder, thread, small, onSent }: Props) {
  const [text, setText] = useState("");
  const [to, setTo] = useState<(typeof ADDRESSABLE)[number] | null>(null);
  const [pop, setPop] = useState<string | null>(null); // the text typed after @, while picking
  const [sel, setSel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  const options = pop == null ? [] : ADDRESSABLE.filter((r) =>
    r.name.toLowerCase().startsWith(pop.toLowerCase()));

  function change(value: string) {
    setText(value);
    const m = /(^|\s)@([\w ]{0,20})$/.exec(value);
    setPop(m && !to ? m[2] : null);
    setSel(0);
    const el = box.current;
    if (el) { el.style.height = "20px"; el.style.height = Math.min(el.scrollHeight, 120) + "px"; }
  }

  function pick(r: (typeof ADDRESSABLE)[number]) {
    setTo(r);
    setText(text.replace(/(^|\s)@[\w ]{0,20}$/, "$1"));
    setPop(null);
    box.current?.focus();
  }

  async function send() {
    const body = text.trim();
    if (!body) return;
    try {
      await api.post(body, to?.actor, thread);
      setText(""); setTo(null); setError(null);
      if (box.current) box.current.style.height = "20px";
      onSent?.();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function key(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (options.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); setSel((sel + 1) % options.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSel((sel + options.length - 1) % options.length); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(options[sel]); return; }
      if (e.key === "Escape") { setPop(null); return; }
    }
    if (e.key === "Backspace" && !text && to) setTo(null);
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  }

  return (
    <div className={small ? "composer sm" : "composer"}
         style={small ? { padding: "8px 8px 8px 12px", marginTop: 10 } : { marginTop: 12 }}>
      {options.length > 0 && (
        <div className="pop">
          <div className="pop-h eyebrow">Address one role</div>
          {options.map((r, i) => (
            <div key={r.actor} className={i === sel ? "pop-i on" : "pop-i"}
                 onMouseDown={(e) => { e.preventDefault(); pick(r); }}>
              <Av who={r} size="sm" />
              <div><div className="n">{r.name}</div><div className="d">{r.does}</div></div>
            </div>
          ))}
        </div>
      )}
      {to && <span className="to-chip">@{to.name}<button aria-label="Remove" onClick={() => setTo(null)}>×</button></span>}
      <textarea ref={box} rows={1} value={text} placeholder={error ? error : placeholder}
                onChange={(e) => change(e.target.value)} onKeyDown={key} aria-label={placeholder} />
      <button className="btn ink" style={small ? { padding: "6px 10px" } : undefined}
              disabled={!text.trim()} onClick={send}>Send</button>
    </div>
  );
}
