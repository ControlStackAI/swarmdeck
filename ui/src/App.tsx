import { useCallback, useEffect, useMemo, useState } from "react";
import { HiveCanvas } from "./HiveCanvas";
import { Inspector } from "./Inspector";
import { Launcher } from "./Launcher";
import { Metrics } from "./Metrics";
import { Topology } from "./Topology";
import { SwarmSummary, api } from "./api";
import { LEGEND_ORDER, STATUS, fmtCost, fmtCount, fmtDuration } from "./status";
import { useSwarm } from "./useSwarm";

type Tab = "hive" | "metrics" | "topology";

export default function App() {
  const [swarms, setSwarms] = useState<SwarmSummary[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("hive");
  const [showLauncher, setShowLauncher] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState<number | null>(null);
  const [inspect, setInspect] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [concurrency, setConcurrency] = useState<number | null>(null);

  const { store, conn } = useSwarm(current);

  const refreshSwarms = useCallback(async () => {
    try {
      const list = await api.swarms();
      setSwarms(list);
      setCurrent((prev) => prev ?? list[0]?.swarm_id ?? null);
      return list;
    } catch {
      return [];
    }
  }, []);

  useEffect(() => {
    refreshSwarms();
    const id = window.setInterval(refreshSwarms, 3000);
    return () => window.clearInterval(id);
  }, [refreshSwarms]);

  useEffect(() => {
    setSelected(new Set());
    setInspect(null);
    setFilter(null);
    setConcurrency(null);
  }, [current]);

  const agg = store.agg;
  const counts = agg?.by_status ?? {};

  const act = async (action: string, value?: number) => {
    if (!current) return;
    const body: Parameters<typeof api.control>[1] = { action, value };
    if (selected.size > 0 && action !== "concurrency") {
      body.indices = [...selected];
    } else if (filter !== null && action !== "concurrency") {
      body.status = STATUS[filter].key;
    }
    try {
      const res = await api.control(current, body);
      const n = res.affected ?? res.concurrency ?? 0;
      setNotice(`${action}: ${n} agent${n === 1 ? "" : "s"}`);
      window.setTimeout(() => setNotice(null), 2500);
    } catch (e) {
      setNotice((e as Error).message);
    }
  };

  const scope =
    selected.size > 0
      ? `${selected.size} selected`
      : filter !== null
        ? `all ${STATUS[filter].key}`
        : "whole swarm";

  const liveConcurrency = concurrency ?? agg?.peak_running ?? 0;

  const stepInspect = useCallback(
    (delta: number) => {
      setInspect((prev) => {
        if (prev === null) return prev;
        const next = prev + delta;
        return next >= 0 && next < store.total ? next : prev;
      });
    },
    [store.total],
  );

  const currentSummary = useMemo(
    () => swarms.find((s) => s.swarm_id === current),
    [swarms, current],
  );

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          <span>swarmdeck</span>
        </div>

        <select
          value={current ?? ""}
          onChange={(e) => setCurrent(e.target.value || null)}
          aria-label="Select swarm"
        >
          {swarms.length === 0 && <option value="">no swarms running</option>}
          {swarms.map((s) => (
            <option key={s.swarm_id} value={s.swarm_id}>
              {s.name} · {s.agg.total} agents · {s.phase}
            </option>
          ))}
        </select>

        {currentSummary && (
          <span className={`phase phase-${currentSummary.phase}`}>
            {currentSummary.phase}
          </span>
        )}
        <span className={`conn conn-${conn}`} title={`websocket ${conn}`}>
          {conn === "open" ? "live" : conn}
        </span>

        <div className="spacer" />

        {agg && (
          <div className="headline">
            <span>
              <strong>{fmtCount(agg.done)}</strong>/{fmtCount(agg.total)}
            </span>
            <span className="muted">{fmtDuration(agg.elapsed)}</span>
            <span className="muted">{agg.throughput}/s</span>
            {agg.cost_usd > 0 && <span className="muted">{fmtCost(agg.cost_usd)}</span>}
          </div>
        )}

        <button className="primary" onClick={() => setShowLauncher(true)}>
          New swarm
        </button>
      </header>

      <nav className="tabs">
        {(["hive", "metrics", "topology"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>

      {current && (
        <div className="controls">
          <ul className="legend">
            {LEGEND_ORDER.map((code) => {
              const style = STATUS[code];
              const count = counts[style.key] ?? 0;
              const active = filter === code;
              return (
                <li key={style.key}>
                  <button
                    className={active ? "legend-item active" : "legend-item"}
                    onClick={() => setFilter(active ? null : code)}
                    disabled={count === 0 && !active}
                    title={
                      count === 0 ? `no ${style.label} agents` : `filter to ${style.label}`
                    }
                  >
                    <span
                      className={`swatch ${style.texture === "hatch" ? "hatched" : ""}`}
                      style={{ background: style.fill }}
                      aria-hidden
                    />
                    {style.label}
                    <strong>{fmtCount(count)}</strong>
                  </button>
                </li>
              );
            })}
          </ul>

          <div className="actions">
            <span className="scope">{scope}</span>
            <button onClick={() => act("pause")}>Pause</button>
            <button onClick={() => act("resume")}>Resume</button>
            <button onClick={() => act("retry")}>Retry</button>
            <button className="danger" onClick={() => act("cancel")}>
              Cancel
            </button>
            {selected.size > 0 && (
              <button className="ghost" onClick={() => setSelected(new Set())}>
                clear
              </button>
            )}
            <label className="concurrency">
              concurrency
              <input
                type="range"
                min={1}
                max={Math.max(store.total || 1, 1)}
                value={liveConcurrency || 1}
                onChange={(e) => setConcurrency(Number(e.target.value))}
                onMouseUp={() => concurrency && act("concurrency", concurrency)}
                onTouchEnd={() => concurrency && act("concurrency", concurrency)}
              />
              <output>{liveConcurrency}</output>
            </label>
          </div>
        </div>
      )}

      <main className={inspect !== null ? "with-inspector" : ""}>
        <section className="view">
          {!current && (
            <div className="welcome">
              <h1>No swarm running</h1>
              <p className="muted">
                Launch one from a spec, or try the bundled demo:
                <br />
                <code>swarmdeck demo --agents 1000 --concurrency 200</code>
              </p>
              <button className="primary" onClick={() => setShowLauncher(true)}>
                Launch a swarm
              </button>
            </div>
          )}

          {current && tab === "hive" && (
            <HiveCanvas
              store={store}
              selected={selected}
              filter={filter}
              onInspect={setInspect}
              onSelect={(indices, additive) => {
                setSelected((prev) => {
                  if (!additive) return new Set(indices);
                  const next = new Set(prev);
                  for (const i of indices) next.add(i);
                  return next;
                });
              }}
            />
          )}
          {current && tab === "metrics" && <Metrics store={store} />}
          {current && tab === "topology" && <Topology swarmId={current} />}
        </section>

        {inspect !== null && current && (
          <Inspector
            swarmId={current}
            index={inspect}
            onClose={() => setInspect(null)}
            onStep={stepInspect}
          />
        )}
      </main>

      {current && store.feed.length > 0 && (
        <footer className="feed">
          {store.feed.slice(-3).map((entry, i) => (
            <p key={i} className={`feed-line feed-${entry.level}`}>
              <span className="muted">
                {entry.index >= 0 ? `a${String(entry.index).padStart(5, "0")}` : "swarm"}
              </span>
              {entry.msg}
            </p>
          ))}
        </footer>
      )}

      {notice && <div className="toast">{notice}</div>}

      {showLauncher && (
        <Launcher
          onClose={() => setShowLauncher(false)}
          onLaunched={(id, warnings) => {
            setShowLauncher(false);
            setCurrent(id);
            refreshSwarms();
            if (warnings.length) setNotice(warnings[0]);
          }}
        />
      )}
    </div>
  );
}
