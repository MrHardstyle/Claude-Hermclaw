import { useRef, type KeyboardEvent } from "react";

export interface TabDef {
  id: string;
  label: string;
  count?: number | null;
}

/** WAI-ARIA tablist with roving focus (arrow keys, Home/End); activation follows focus. */
export function Tabs({ tabs, active, onChange, label, idPrefix }: { tabs: TabDef[]; active: string; onChange: (id: string) => void; label: string; idPrefix: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = -1;
    if (e.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    e.preventDefault();
    const t = tabs[next];
    if (!t) return;
    onChange(t.id);
    refs.current[next]?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t, i) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${t.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel`}
            tabIndex={selected ? 0 : -1}
            className={`tab ${selected ? "is-active" : ""}`}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => onKey(e, i)}
            data-testid={`tab-${t.id}`}
          >
            {t.label}
            {t.count !== undefined && t.count !== null ? <span className="tab-count">{t.count}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
