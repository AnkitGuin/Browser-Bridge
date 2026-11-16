import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Approval, Benchmark, Event, Person, Run, RunList, Settings } from "./contracts";
import "./style.css";

const API = "/api/v1";
const EXAMPLE = "Find and escalate unresolved high-priority Team A tickets older than 48 hours. Check each linked CRM account and summarize the outcome.";
const TERMINAL = new Set(["COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "TIMED_OUT"]);
const PARKED = new Set(["WAITING_APPROVAL", "WAITING_HUMAN_LOGIN", "PAUSED"]);
const EMPTY: RunList = { items: [], total: 0, status_counts: {} };
const get = async <T,>(path: string, data?: unknown): Promise<T> => {
  const response = await fetch(API + path, data === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    throw new Error(failure.message ?? "Request failed (" + response.status + ")");
  }
  return response.json();
};
const words = (value: string) => value.replaceAll("_", " ").toLowerCase();
const date = (time: number) => new Date(time * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const compact = (value: string) => value.length > 83 ? value.slice(0, 80) + "…" : value;
type View = "overview" | "new" | "runs" | "review" | "lab" | "settings";
const NAV: [View, string, string][] = [
  ["overview", "Overview", "◫"], ["runs", "Run history", "≡"], ["review", "Approvals", "◇"],
  ["lab", "Scenario lab", "⌁"], ["settings", "Settings", "⚙"],
];

function Badge({ value }: { value: string }) {
  const flavor = ["COMPLETED", "VERIFIED", "completed", "passed"].includes(value) ? "success"
    : ["FAILED", "CANCELLED", "TIMED_OUT", "failed", "denied"].includes(value) ? "danger"
    : value.includes("WAITING") || value === "PARTIAL_SUCCESS" || value === "PAUSED" ? "warning" : "neutral";
  return <span className={"badge " + flavor}><i />{words(value)}</span>;
}
function Panel({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <section className="panel"><div className="panel-head"><h2>{title}</h2>{action}</div>{children}</section>;
}
function Alert({ value }: { value: string }) {
  return value ? <p className="alert" role="alert">{value}</p> : null;
}
function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
function Login({ onLogin }: { onLogin: (person: Person) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(event.currentTarget));
    setBusy(true); setError("");
    try { await get("/auth/login", fields); onLogin(await get<Person>("/auth/me")); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <main className="login-layout">
    <div className="login-brand"><span className="brand-mark">B</span> BrowserBridge</div>
    <section className="login-card">
      <h1>Operator console</h1><p className="muted">Sign in to run and review enterprise browser workflows.</p>
      <form onSubmit={submit}>
        <label>Email <input name="email" type="email" autoComplete="username" required placeholder="operator@demo.test" /></label>
        <label>Password <input name="password" type="password" autoComplete="current-password" required /></label>
        <Alert value={error} /><button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
      <a className="subtle-link" href="/login">Portal sign-in</a>
    </section>
  </main>;
}

function RunTable({ runs, open }: { runs: Run[]; open: (run: string) => void }) {
  if (!runs.length) return <Empty>No runs match this view.</Empty>;
  return <div className="table-scroll"><table><thead><tr><th>Objective</th><th>Mode</th><th>Status</th><th>Created</th><th>Verified</th></tr></thead>
    <tbody>{runs.map((run) => <tr key={run.id}>
      <td><button className="row-link" title={run.goal} onClick={() => open(run.id)}>{compact(run.goal)}</button></td>
      <td className="capitalize">{run.mode}</td><td><Badge value={run.status} /></td><td>{date(run.created_at)}</td>
      <td className="tabular">{run.summary.verified}</td>
    </tr>)}</tbody></table></div>;
}

function Overview({ data, approvals, open, history }: {
  data: RunList; approvals: Approval[]; open: (id: string) => void; history: () => void;
}) {
  const counts = data.status_counts;
  const completed = counts.COMPLETED ?? 0;
  const failed = (counts.PARTIAL_SUCCESS ?? 0) + (counts.FAILED ?? 0) + (counts.TIMED_OUT ?? 0);
  const active = Object.entries(counts).reduce((sum, [state, count]) =>
    sum + (!TERMINAL.has(state) && !PARKED.has(state) ? count : 0), 0);
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const average = data.items.length ? data.items.reduce((sum, run) => sum + run.summary.verified, 0) / data.items.length : 0;
  const buckets = [
    ["Completed", completed, "good"], ["Partial or failed", failed, "poor"],
    ["Active", active, "active"], ["Other", total - completed - failed - active, "other"],
  ] as const;
  const recentFailures = data.items.filter((run) => ["FAILED", "PARTIAL_SUCCESS", "TIMED_OUT"].includes(run.status));
  return <>
    <div className="stats">
      {[["Total runs", total], ["Completed", completed], ["Active queue", active], ["Pending approvals", approvals.length]].map(([label, value]) =>
        <div className="stat" key={label}><span>{label}</span><strong>{value}</strong></div>)}
    </div>
    <div className="split">
      <Panel title="Run outcomes" action={<span className="muted small">{total} total</span>}>
        <div className="bar-list">{buckets.map(([label, value, tone]) =>
          <div className="bar-line" key={label}><span>{label}</span>
            <div className="bar-track"><i className={tone} style={{ width: (total ? value / total * 100 : 0) + "%" }} /></div><b>{value}</b>
          </div>)}</div>
      </Panel>
      <Panel title="Verification"><div className="verification-stat"><strong>{average.toFixed(1)}</strong>
        <span>Verified records per run across the {data.items.length} most recent runs</span></div>
        {recentFailures.length > 0 && <div className="failure-line">Latest failure · <button className="row-link" onClick={() => open(recentFailures[0].id)}>{recentFailures[0].summary.stop_reason ? words(recentFailures[0].summary.stop_reason) : words(recentFailures[0].status)}</button></div>}
      </Panel>
    </div>
    <Panel title="Recent runs" action={<button className="subtle-link" onClick={history}>View history →</button>}>
      <RunTable runs={data.items.slice(0, 7)} open={open} />
    </Panel>
  </>;
}

function NewRun({ person, settings, open }: { person: Person; settings: Settings | null; open: (id: string) => void }) {
  const [goal, setGoal] = useState(new URLSearchParams(window.location.search).get("goal") ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const result = await get<{ id: string }>("/runs", {
        goal, mode: form.get("mode"), timeout_seconds: Number(form.get("timeout")),
        max_actions: Number(form.get("steps")), approval_required: form.has("approval"), apps: form.getAll("apps"),
      });
      open(result.id);
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="form-layout"><Panel title="Task configuration" action={<button className="subtle-link" onClick={() => setGoal(EXAMPLE)}>Use demo objective</button>}>
    <form className="form-body" onSubmit={submit}>
      <label>Objective <textarea name="goal" rows={5} required minLength={8} maxLength={1800} value={goal}
        onChange={(event) => setGoal(event.target.value)} placeholder="Describe the business task and the records it should apply to." /></label>
      <div className="form-grid">
        <label>Execution mode <select name="mode" defaultValue="hybrid">
          <option value="hybrid">Hybrid</option><option value="dom">DOM</option>
          <option value="vision" disabled={!settings?.model}>Vision{!settings?.model ? " · provider required" : ""}</option>
        </select></label>
        <label>Signed-in identity <input disabled value={person.name + " · " + person.tenant_id} /></label>
        <label>Timeout in seconds <input type="number" name="timeout" defaultValue={240} min={15} max={900} required /></label>
        <label>Browser action limit <input type="number" name="steps" defaultValue={160} min={10} max={1000} required /></label>
      </div>
      <fieldset><legend>Permitted applications</legend><div className="checks">
        {["support", "crm", "inventory"].map((app) => <label key={app}><input name="apps" type="checkbox" value={app} defaultChecked />{app === "crm" ? "CRM" : app}</label>)}
      </div></fieldset>
      <label className="inline"><input name="approval" type="checkbox" />Require review before ticket changes</label>
      <Alert value={error} /><button className="primary" disabled={busy}>{busy ? "Starting…" : "Start run"}</button>
    </form>
  </Panel><Panel title="Supported objectives"><div className="help">
    <p>Escalate stale, unresolved tickets by team, priority and age.</p><p>Look up CRM accounts, tickets and warehouse stock using their displayed reference.</p>
    <p>Managers can request transfers and reorders. Another manager reviews the exact movement.</p>
    {!settings?.model && <p className="model-note">Hybrid uses semantic targeting until a multimodal provider is configured.</p>}
  </div></Panel></div>;
}

function History({ open }: { open: (id: string) => void }) {
  const [data, setData] = useState<RunList>(EMPTY);
  const [filters, setFilters] = useState({ q: "", status: "", mode: "", date: "" });
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    const query = new URLSearchParams({ limit: "20", offset: String(offset) });
    for (const [key, value] of Object.entries(filters)) {
      if (value) query.set(key === "date" ? "since" : key, key === "date" ? String(Math.floor(new Date(value).getTime() / 1000)) : value);
    }
    let current = true;
    get<RunList>("/runs?" + query).then((next) => { if (current) { setData(next); setError(""); } })
      .catch((failure: Error) => { if (current) setError(failure.message); });
    return () => { current = false; };
  }, [filters, offset]);
  function change(key: keyof typeof filters, value: string) { setOffset(0); setFilters({ ...filters, [key]: value }); }
  return <Panel title="Run history" action={<span className="muted small">{data.total} matching</span>}>
    <div className="filter-row">
      <label>Objective <input value={filters.q} onChange={(e) => change("q", e.target.value)} placeholder="Search goals" /></label>
      <label>Mode <select value={filters.mode} onChange={(e) => change("mode", e.target.value)}>
        <option value="">All modes</option>{["dom", "hybrid", "vision"].map((mode) => <option key={mode}>{mode}</option>)}
      </select></label>
      <label>Status <select value={filters.status} onChange={(e) => change("status", e.target.value)}>
        <option value="">All statuses</option>{[...TERMINAL, ...PARKED, "CREATED", "OBSERVING", "ACTING"].map((state) =>
          <option key={state} value={state}>{words(state)}</option>)}
      </select></label>
      <label>Since <input type="date" value={filters.date} onChange={(e) => change("date", e.target.value)} /></label>
    </div><Alert value={error} /><RunTable runs={data.items} open={open} />
    <div className="pagination"><button disabled={offset === 0} onClick={() => setOffset(offset - 20)}>Previous</button>
      <button disabled={offset + 20 >= data.total} onClick={() => setOffset(offset + 20)}>Next</button></div>
  </Panel>;
}

function RunDetail({ id, person, back }: { id: string; person: Person; back: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Event | null>(null);
  const refresh = useCallback(() => get<Run>("/runs/" + id).then(setRun).catch((failure: Error) => setError(failure.message)), [id]);
  useEffect(() => {
    void refresh();
    const source = new EventSource(API + "/runs/" + id + "/events");
    source.onmessage = (message) => {
      const event: Event = JSON.parse(message.data);
      setEvents((old) => old.some((item) => item.sequence === event.sequence) ? old : [...old, event]);
      if (event.state_before !== event.state_after) void refresh();
    };
    source.addEventListener("end", () => { source.close(); void refresh(); });
    return () => source.close();
  }, [id, refresh]);
  const [page, setPage] = useState(0);
  const latestEvidence = [...events].reverse().find((event) => event.payload.evidence);
  const latestPage = [...events].reverse().find((event) => event.payload.url);
  const ordered = [...events].reverse();
  const evidence = selected?.payload.evidence ?? latestEvidence?.payload.evidence;
  async function control(action: string) {
    try { await get("/runs/" + id + "/" + action, {}); await refresh(); }
    catch (failure) { setError((failure as Error).message); }
  }
  return <>
    <button className="subtle-link back" onClick={back}>← Run history</button>
    <Alert value={error} />
    {run && <><div className="detail-heading"><div><h1>Run detail</h1><p>{run.goal}</p></div><Badge value={run.status} /></div>
      <div className="detail-summary">
        <span><b>Mode</b>{run.mode}</span><span><b>Applications</b>{run.spec.apps.join(", ")}</span>
        <span><b>Browser actions</b>{run.summary.actions} / {run.spec.max_actions}</span><span><b>Verified</b>{run.summary.verified}</span>
        <span><b>Model tokens</b>{run.summary.model_tokens}</span>
      </div>
      <div className="toolbar">
        {person.id === run.user_id && !TERMINAL.has(run.status) && <>
          {["PAUSED", "WAITING_HUMAN_LOGIN"].includes(run.status) ?
            <button onClick={() => void control("resume")}>Resume</button> : !PARKED.has(run.status) &&
            <button disabled={!!run.control} onClick={() => void control("pause")}>Pause</button>}
          <button className="danger-button" disabled={run.control === "cancel"} onClick={() => void control("cancel")}>Cancel</button>
        </>}
        {["json", "csv", "md"].map((format) => <a key={format} className="button" download={"browserbridge-" + id + "." + format}
          href={API + "/runs/" + id + "/report?format=" + format}>{format.toUpperCase()} report</a>)}
      </div>
      {run.summary.stop_reason && <p className="model-note" role="status">{words(run.summary.stop_reason)}</p>}
      <div className="split detail-columns">
        <Panel title="Event timeline" action={<span className="muted small">{events.length} events</span>}>
          {events.length === 0 ? <Empty>Waiting for events.</Empty> : <ol className="timeline">
            {ordered.slice(page * 20, (page + 1) * 20).map((event) => <li key={event.event_id}>
              <button onClick={() => setSelected(event)} className={selected?.event_id === event.event_id ? "selected" : ""}>
                <b>{words(event.event_type)}</b><span>#{event.sequence} · {words(event.state_after)}</span>
              </button>
            </li>)}</ol>}
          <div className="pagination"><button disabled={!page} onClick={() => setPage(page - 1)}>Newer</button>
            <button disabled={(page + 1) * 20 >= events.length} onClick={() => setPage(page + 1)}>Older</button></div>
        </Panel>
        <Panel title="Run evidence">
          <div className="evidence">
            {latestPage?.payload.url && <p className="muted small path">{latestPage.payload.url}</p>}
            {evidence ? <a target="_blank" rel="noreferrer" href={API + "/runs/" + id + "/evidence/" + evidence}>
              <img alt="Redacted verification screenshot" src={API + "/runs/" + id + "/evidence/" + evidence} />
            </a> : <Empty>Evidence appears after a record is verified or a review is requested.</Empty>}
            {selected && <pre>{JSON.stringify(selected.payload, null, 2)}</pre>}
          </div>
        </Panel>
      </div>
    </>}
  </>;
}

function Reviews({ items, person, refresh, open }: {
  items: Approval[]; person: Person; refresh: () => void; open: (id: string) => void;
}) {
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState<{ item: Approval; decision: "approve" | "deny" } | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!confirmation) return;
    const reason = new FormData(event.currentTarget).get("reason");
    const { item, decision } = confirmation;
    setBusy(true); setError("");
    try {
      await get("/approvals/" + item.id + "/decision", { action_hash: item.intent_id, decision, reason });
      setConfirmation(null); refresh();
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <><Alert value={error} />
    {items.length === 0 ? <Panel title="Pending reviews"><Empty>No approvals are awaiting review.</Empty></Panel> :
      items.map((item) => <Panel key={item.id} title={words(item.details.operation) + " · " + item.details.resource_id}
        action={<button className="subtle-link" onClick={() => open(item.run_id)}>View run →</button>}>
        <div className="review-body"><p>Requested by <b>{item.requested_by}</b> · Expires {date(item.expires)}</p>
          <div className="review-grid"><div><h3>Current state</h3><pre>{JSON.stringify(item.details.before, null, 2)}</pre></div>
            <div><h3>Proposed change</h3><pre>{JSON.stringify(item.details.requested, null, 2)}</pre></div></div>
          {item.details.evidence_id && <p><a className="subtle-link" target="_blank" rel="noreferrer"
            href={API + "/runs/" + item.run_id + "/evidence/" + item.details.evidence_id}>Open approval evidence ↗</a></p>}
          {person.role === "manager" && person.id !== item.requested_by ?
            <div className="toolbar"><button className="primary" onClick={() => setConfirmation({ item, decision: "approve" })}>Review approval</button>
              <button className="danger-button" onClick={() => setConfirmation({ item, decision: "deny" })}>Deny</button></div>
            : <p className="muted">A separate manager must decide this request.</p>}
        </div>
      </Panel>)}
    {confirmation && <div className="overlay" role="presentation"><form className="modal" role="dialog" aria-modal="true"
      aria-labelledby="confirm-title" onSubmit={submit} onKeyDown={(event) => { if (event.key === "Escape") setConfirmation(null); }}>
      <h2 id="confirm-title">Confirm {confirmation.decision === "approve" ? "approval" : "denial"}</h2>
      <p>{confirmation.item.details.operation} · {confirmation.item.details.resource_id}</p>
      <label>Reason <textarea name="reason" maxLength={300} autoFocus /></label>
      <Alert value={error} /><div className="toolbar"><button type="submit" className="primary" disabled={busy}>Confirm</button>
        <button type="button" onClick={() => setConfirmation(null)}>Go back</button></div>
    </form></div>}
  </>;
}

function Lab({ person, settings }: { person: Person; settings: Settings | null }) {
  const [jobs, setJobs] = useState<Benchmark[]>([]);
  const [job, setJob] = useState<Benchmark | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const updateJobs = useCallback(() => get<{ items: Benchmark[] }>("/benchmark/runs").then((data) => setJobs(data.items)), []);
  const activeId = job?.id;
  useEffect(() => {
    if (person.role !== "manager") return;
    void updateJobs().catch((failure: Error) => setError(failure.message));
    if (!activeId) return;
    const tick = () => get<Benchmark>("/benchmark/runs/" + activeId).then(setJob)
      .catch((failure: Error) => setError(failure.message));
    void tick();
    const timer = window.setInterval(() => void tick(), 1300);
    return () => window.clearInterval(timer);
  }, [activeId, person.role, updateJobs]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const result = await get<{ id: string }>("/benchmark/runs", {
        suite: form.get("suite"), mode: form.get("mode"), layout: form.get("layout"), seed: Number(form.get("seed")),
      });
      setJob(await get<Benchmark>("/benchmark/runs/" + result.id)); await updateJobs();
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  if (person.role !== "manager") return <Panel title="Scenario lab"><Empty>Sign in as a local manager to start benchmark runs.</Empty></Panel>;
  return <>
    <Panel title="Benchmark configuration"><form className="filter-row" onSubmit={submit}>
      <label>Suite <select name="suite"><option value="smoke">Smoke</option><option value="local120">Local 120</option></select></label>
      <label>Mode <select name="mode" defaultValue="hybrid"><option>hybrid</option><option>dom</option><option>scripted</option>
        <option disabled={!settings?.model}>vision</option><option>all</option></select></label>
      <label>Layout <select name="layout">{["all", "base", "renamed", "reordered", "modal", "slow", "masked", "compact"].map(
        (layout) => <option key={layout}>{layout}</option>)}</select></label>
      <label>Seed <input type="number" name="seed" defaultValue={42} min={0} max={1000000000} required /></label>
      <button className="primary" disabled={busy}>Run benchmark</button>
    </form><Alert value={error} /></Panel>
    {!!jobs.length && <Panel title="Previous evaluations"><div className="job-list">{jobs.map((item) =>
      <button key={item.id} onClick={() => setJob(item)}>{item.options.suite} · {item.options.mode} · {date(item.created_at)}</button>)}</div></Panel>}
    {job && <Panel title="Measured results" action={<Badge value={job.status} />}>
      <Alert value={job.results?.error ?? ""} />
      <div className="bench-stats">{Object.entries(job.results?.metrics ?? {}).map(([mode, row]) => <div key={mode}>
        <b>{mode}</b><span>{row.successes} / {row.n} passed{row.unavailable ? " · " + row.unavailable + " unavailable" : ""}</span>
        <small>{row.wall_ms_p95 === null ? "p95 unavailable" : "p95 " + row.wall_ms_p95 + " ms"}</small></div>)}</div>
      {job.results?.cases?.length ? <div className="table-scroll"><table><thead><tr>
        <th>Case</th><th>Mode</th><th>Layout</th><th>Result</th><th>Latency</th><th>Failure tags</th>
      </tr></thead><tbody>{job.results.cases.map((item) => <tr key={item.id + item.mode}>
        <td>{item.id}</td><td>{item.mode}</td><td>{item.layout}</td>
        <td><Badge value={item.task_success === null ? "unavailable" : item.task_success ? "passed" : "failed"} /></td>
        <td>{item.wall_ms ?? "—"}{item.wall_ms !== undefined && " ms"}</td><td>{item.failure_tags.map(words).join(", ") || "—"}</td>
      </tr>)}</tbody></table></div> : <Empty>Waiting for case results.</Empty>}
    </Panel>}
  </>;
}

function App() {
  const [person, setPerson] = useState<Person | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>(new URLSearchParams(window.location.search).has("goal") ? "new" : "overview");
  const [run, setRun] = useState<string | null>(null);
  const [data, setData] = useState<RunList>(EMPTY);
  const [items, setItems] = useState<Approval[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { get<Person>("/auth/me").then(setPerson).catch(() => null).finally(() => setLoading(false)); }, []);
  const refresh = useCallback(() => {
    void Promise.all([get<RunList>("/runs"), get<{ items: Approval[] }>("/approvals"), get<Settings>("/settings")])
      .then(([history, reviews, configuration]) => { setData(history); setItems(reviews.items); setSettings(configuration); setError(""); })
      .catch((failure: Error) => setError(failure.message));
  }, []);
  useEffect(() => {
    if (!person) return;
    refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => window.clearInterval(timer);
  }, [person, refresh]);
  function change(next: View) { setRun(null); setView(next); refresh(); }
  if (loading) return <main className="login-layout"><p role="status">Loading BrowserBridge…</p></main>;
  if (!person) return <Login onLogin={setPerson} />;
  const title = { overview: "Overview", new: "New task", runs: "Run history", review: "Approvals", lab: "Scenario lab", settings: "Settings" }[view];
  return <div className="shell">
    <a href="#content" className="skip">Skip to content</a>
    <aside className="sidebar">
      <button className="brand" onClick={() => change("overview")}><span className="brand-mark">B</span><span>BrowserBridge<small>Operator console</small></span></button>
      <button className="new-side" onClick={() => change("new")}><span>＋</span> New task</button>
      <nav aria-label="Console">{NAV.map(([key, label, icon]) => <button key={key}
        className={key === view && !run ? "on" : ""} aria-current={key === view && !run ? "page" : undefined} onClick={() => change(key)}>
        <span aria-hidden="true">{icon}</span>{label}{key === "review" && items.length > 0 && <b>{items.length}</b>}
      </button>)}</nav>
      <div className="portals"><p>Business apps</p>
        {[["Support desk", "support"], ["CRM directory", "crm"], ["Inventory", "inventory"]].map(([label, route]) =>
          <a key={route} href={"/" + route}>{label}<span aria-hidden="true">↗</span></a>)}
      </div>
      <div className="identity"><span className="avatar">{person.name.split(" ").map((part) => part[0]).join("")}</span>
        <div><b>{person.name}</b><small>{person.role} · {person.tenant_id}</small></div>
        <button aria-label="Sign out" title="Sign out" onClick={() => void get("/auth/logout", {}).then(() => setPerson(null))
          .catch((failure: Error) => setError(failure.message))}>↪</button></div>
    </aside>
    <main id="content" className="content">
      <header className="topbar"><span>Workspace <b> / </b> {run ? "Run detail" : title}</span><span className="workspace"><i /> Local environment</span></header>
      <div className="content-inner"><Alert value={error} />
        {run ? <RunDetail key={run} id={run} person={person} back={() => change("runs")} /> : <>
          <div className="page-heading"><div><h1>{title}</h1>
            {view === "overview" && <p>Track browser workflows, verified changes and human reviews.</p>}</div>
            {view === "overview" && <button className="primary" onClick={() => change("new")}>＋ New task</button>}
          </div>
          {view === "overview" && <Overview data={data} approvals={items} open={setRun} history={() => change("runs")} />}
          {view === "new" && <NewRun person={person} settings={settings} open={setRun} />}
          {view === "runs" && <History open={setRun} />}
          {view === "review" && <Reviews items={items} person={person} refresh={refresh} open={setRun} />}
          {view === "lab" && <Lab person={person} settings={settings} />}
          {view === "settings" && settings && <Panel title="Runtime configuration"><dl className="settings">
            <dt>Multimodal model</dt><dd>{settings.model ?? "Unconfigured"}</dd><dt>Demo identity</dt><dd>{settings.demo_identity ? "Enabled" : "Disabled"}</dd>
            <dt>Evidence retention</dt><dd>{settings.evidence_retention_seconds / 3600} hours</dd>
            <dt>Maximum run time</dt><dd>{settings.maximum_run_seconds} seconds</dd>
            <dt>Maximum browser actions</dt><dd>{settings.maximum_browser_actions}</dd>
          </dl></Panel>}
        </>}
      </div>
    </main>
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
