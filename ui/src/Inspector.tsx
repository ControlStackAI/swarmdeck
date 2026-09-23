/** One agent, in full: logs, task, result, timing, declared edges. */

import { useCallback, useEffect, useRef, useState } from "react";
import { AgentDetail, api } from "./api";
import { STATUS_BY_KEY, fmtCost, fmtCount, fmtDuration, styleFor } from "./status";

interface Props {
  swarmId: string;
  index: number;
  onClose: () => void;
  onStep: (delta: number) => void;
}

export function Inspector({ swarmId, index, onClose, onStep }: Props) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.agent(swarmId, index));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [swarmId, index]);

  useEffect(() => {
    setDetail(null);
    load();
  }, [load]);

  // Poll only while the agent can still change.
  useEffect(() => {
    if (detail && ["succeeded", "failed", "cancelled", "timeout"].includes(detail.status)) {
      return;
    }
    const id = window.setInterval(load, 1000);
    return () => window.clearInterval(id);
  }, [detail, load]);

  useEffect(() => {
    if (follow && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [detail, follow]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
      if (ev.key === "ArrowDown" || ev.key === "j") onStep(1);
      if (ev.key === "ArrowUp" || ev.key === "k") onStep(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onStep]);

  const style = detail ? styleFor(STATUS_BY_KEY[detail.status] ?? 0) : null;

  return (
    <aside className="inspector" aria-label={`Agent ${index}`}>
      <header>
        <div>
          <h2>a{String(index).padStart(5, "0")}</h2>
          {style && (
            <span className="pill">
              <span className="swatch" style={{ background: style.fill }} aria-hidden />
              {detail?.status}
              {detail && detail.attempt > 0 && ` · attempt ${detail.attempt + 1}`}
            </span>
          )}
        </div>
        <div className="inspector-nav">
          <button onClick={() => onStep(-1)} title="Previous agent (k)">
            ↑
          </button>
          <button onClick={() => onStep(1)} title="Next agent (j)">
            ↓
          </button>
          <button onClick={onClose} title="Close (Esc)">
            ✕
          </button>
        </div>
      </header>

      {error && <p className="error">{error}</p>}
      {!detail && !error && <p className="empty">loading…</p>}

      {detail && (
        <div className="inspector-body">
          <dl className="facts">
            <dt>elapsed</dt>
            <dd>{fmtDuration(detail.duration)}</dd>
            <dt>progress</dt>
            <dd>{Math.round(detail.progress * 100)}%</dd>
            {detail.pid !== null && (
              <>
                <dt>pid</dt>
                <dd>{detail.pid}</dd>
              </>
            )}
            {detail.exit_code !== null && (
              <>
                <dt>exit</dt>
                <dd>{detail.exit_code}</dd>
              </>
            )}
            {(detail.tokens_in > 0 || detail.tokens_out > 0) && (
              <>
                <dt>tokens</dt>
                <dd>
                  {fmtCount(detail.tokens_in)} in · {fmtCount(detail.tokens_out)} out
                </dd>
              </>
            )}
            {detail.cost_usd > 0 && (
              <>
                <dt>cost</dt>
                <dd>{fmtCost(detail.cost_usd)}</dd>
              </>
            )}
            {detail.tags.length > 0 && (
              <>
                <dt>tags</dt>
                <dd>{detail.tags.join(", ")}</dd>
              </>
            )}
          </dl>

          {detail.error && (
            <section>
              <h3>Error</h3>
              <pre className="error-box">{detail.error}</pre>
            </section>
          )}

          {Object.keys(detail.metrics).length > 0 && (
            <section>
              <h3>Custom metrics</h3>
              <dl className="facts">
                {Object.entries(detail.metrics).map(([k, v]) => (
                  <>
                    <dt key={`${k}-dt`}>{k}</dt>
                    <dd key={`${k}-dd`}>{fmtCount(v)}</dd>
                  </>
                ))}
              </dl>
            </section>
          )}

          {detail.edges.length > 0 && (
            <section>
              <h3>Edges</h3>
              <ul className="plain">
                {detail.edges.map((e, i) => (
                  <li key={i}>
                    {e.kind} → {e.to}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section>
            <h3>Task</h3>
            <pre>{JSON.stringify(detail.task, null, 2)}</pre>
          </section>

          {detail.result !== null && detail.result !== undefined && (
            <section>
              <h3>Result</h3>
              <pre>{JSON.stringify(detail.result, null, 2)}</pre>
            </section>
          )}

          <section className="logs-section">
            <h3>
              Logs
              <span className="muted">
                {" "}
                {fmtCount(detail.log_count)} lines
                {detail.dropped_logs > 0 && ` · ${detail.dropped_logs} dropped`}
              </span>
              <label className="follow">
                <input
                  type="checkbox"
                  checked={follow}
                  onChange={(e) => setFollow(e.target.checked)}
                />
                follow
              </label>
            </h3>
            <div className="logs" ref={logRef}>
              {detail.logs.length === 0 && <p className="empty">no output</p>}
              {detail.logs.map((line, i) => (
                <p key={i} className={`log log-${line.level}`}>
                  <span className="log-ts">
                    {new Date(line.ts * 1000).toLocaleTimeString()}
                  </span>
                  {line.text}
                </p>
              ))}
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
