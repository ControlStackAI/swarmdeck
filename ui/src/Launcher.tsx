/** Pick a spec, choose a concurrency, launch. */

import { useEffect, useState } from "react";
import { SpecEntry, api } from "./api";

interface Props {
  onLaunched: (swarmId: string, warnings: string[]) => void;
  onClose: (() => void) | null;
}

export function Launcher({ onLaunched, onClose }: Props) {
  const [specs, setSpecs] = useState<SpecEntry[] | null>(null);
  const [chosen, setChosen] = useState<SpecEntry | null>(null);
  const [concurrency, setConcurrency] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .specs()
      .then((list) => {
        setSpecs(list);
        const first = list.find((s) => !s.error);
        if (first) {
          setChosen(first);
          setConcurrency(first.concurrency ?? 32);
        }
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  const launch = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.launch({
        spec_path: chosen.path,
        concurrency: concurrency || undefined,
      });
      onLaunched(res.swarm_id, res.warnings ?? []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => onClose?.()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Launch a swarm</h2>
          {onClose && (
            <button onClick={onClose} title="Close">
              ✕
            </button>
          )}
        </header>

        {error && <p className="error">{error}</p>}
        {!specs && !error && <p className="empty">reading specs…</p>}

        {specs && specs.length === 0 && (
          <p className="empty">
            No specs found.
            <br />
            <span className="muted">
              Point the server at a directory of <code>*.swarm.yaml</code> files with{" "}
              <code>swarmdeck serve --specs ./path</code>.
            </span>
          </p>
        )}

        {specs && specs.length > 0 && (
          <>
            <ul className="spec-list">
              {specs.map((spec) => (
                <li key={spec.path}>
                  <button
                    className={chosen?.path === spec.path ? "spec selected" : "spec"}
                    disabled={!!spec.error}
                    onClick={() => {
                      setChosen(spec);
                      setConcurrency(spec.concurrency ?? 32);
                    }}
                  >
                    <strong>{spec.name}</strong>
                    {spec.error ? (
                      <span className="error-inline">{spec.error}</span>
                    ) : (
                      <>
                        <span className="muted">{spec.description}</span>
                        <span className="spec-meta">
                          {spec.agents} agents · {spec.runtime} ·{" "}
                          <code>{spec.command}</code>
                        </span>
                      </>
                    )}
                  </button>
                </li>
              ))}
            </ul>

            {chosen && !chosen.error && (
              <div className="launch-row">
                <label>
                  concurrency
                  <input
                    type="number"
                    min={1}
                    max={chosen.agents ?? 10000}
                    value={concurrency}
                    onChange={(e) => setConcurrency(Number(e.target.value))}
                  />
                </label>
                <span className="muted">
                  {chosen.agents} agents, {concurrency} at a time
                </span>
                <button className="primary" onClick={launch} disabled={busy}>
                  {busy ? "launching…" : `Launch ${chosen.name}`}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
