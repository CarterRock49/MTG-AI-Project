"use strict";

const state = {
  overview: {}, runs: [], run: null, evaluationGames: [], filteredGames: [],
  selectedGameKey: null, statsSources: [], stats: null, statsPage: 0,
  statsPageSize: 200, harvests: [], diagnostics: [], statsRequestGeneration: 0,
  statsGamesRequestGeneration: 0, runRequestGeneration: 0,
  evaluatorGameKey: null, evaluatorPage: 0,
  evaluatorPageSize: 50, currentEvaluatorEvents: [], currentEvaluatorTerminal: null,
  actionGameKey: null, actionPage: 0, actionPageSize: 100, currentActions: [],
  currentFullDebug: null, currentTraceReplay: null,
};

const $ = (id) => document.getElementById(id);
const value = (object, ...paths) => {
  for (const path of paths) {
    let current = object;
    for (const part of path.split(".")) current = current == null ? undefined : current[part];
    if (current !== undefined && current !== null) return current;
  }
  return undefined;
};
const number = (input, fallback = 0) => {
  const parsed = Number(input);
  return Number.isFinite(parsed) ? parsed : fallback;
};
const array = (input) => Array.isArray(input) ? input : [];
const objectEntries = (input) => input && typeof input === "object" && !Array.isArray(input) ? Object.entries(input) : [];
const escapeHTML = (input) => String(input ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const compact = (input, maximum = 24) => {
  const text = String(input ?? "—");
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
};
const fmtInt = (input) => number(input).toLocaleString();
const fmtRate = (input) => `${(number(input) * 100).toFixed(1)}%`;
const fmtReward = (input) => number(input).toFixed(3);
const fmtOptionalRate = (input) => input === undefined || input === null ? "—" : fmtRate(input);
const fmtOptionalReward = (input) => input === undefined || input === null ? "—" : fmtReward(input);
const fmtDate = (input) => {
  if (!input) return "—";
  const numeric = Number(input);
  const date = new Date(Number.isFinite(numeric) ? (numeric < 10_000_000_000 ? numeric * 1000 : numeric) : input);
  return Number.isNaN(date.getTime()) ? String(input) : date.toLocaleString();
};
const pretty = (input) => JSON.stringify(input ?? null, null, 2);

async function api(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options});
  let payload;
  try { payload = await response.json(); } catch (_) { payload = {error: response.statusText}; }
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

function showError(error) {
  const alert = $("alert");
  alert.textContent = error instanceof Error ? error.message : String(error);
  alert.classList.remove("hidden");
  $("health-dot").className = "status-dot error";
  $("health-label").textContent = "Artifact load failed";
}

function clearError() { $("alert").classList.add("hidden"); }

function diagnosticItems(payload) {
  const items = value(payload, "diagnostics", "health.diagnostics", "warnings", "errors", "scan_errors") || [];
  if (Array.isArray(items)) return items;
  return objectEntries(items).map(([source, message]) => ({source, message}));
}

function renderDiagnostics(targetId, items, emptyText = "No loader warnings.") {
  const target = $(targetId);
  if (!items.length) {
    target.className = "diagnostic-list empty-state";
    target.textContent = emptyText;
    return;
  }
  target.className = "diagnostic-list";
  target.innerHTML = items.map((item) => {
    const level = String(value(item, "level", "severity") || "warning").toLowerCase();
    const title = value(item, "source", "path", "kind") || "Artifact notice";
    const message = value(item, "message", "error", "detail") || (typeof item === "string" ? item : pretty(item));
    return `<div class="diagnostic ${level === "error" ? "error" : ""}"><strong>${escapeHTML(title)}</strong><span>${escapeHTML(message)}</span></div>`;
  }).join("");
}

function metric(label, amount, note) {
  return `<article class="metric-card"><span class="label">${escapeHTML(label)}</span><strong>${escapeHTML(amount)}</strong><small>${escapeHTML(note)}</small></article>`;
}

function renderOverview() {
  const overview = state.overview;
  const cards = [
    ["Training runs", fmtInt(value(overview, "runs", "run_count") || state.runs.length), "models + logs merged"],
    ["Evaluation checkpoints", fmtInt(value(overview, "evaluation_checkpoints", "evaluation_count")), "all discovered runs"],
    ["Evaluation games", fmtInt(value(overview, "evaluation_games", "evaluation_game_count")), "every persisted case"],
    ["Stats scopes", fmtInt(value(overview, "stats_sources", "stats_source_count") || state.statsSources.length), "train · eval · Harvest"],
    ["Deck aggregates", fmtInt(value(overview, "decks", "deck_count")), "scope-aware records"],
    ["Card aggregates", fmtInt(value(overview, "cards", "card_count")), "canonical IDs retained"],
  ];
  $("overview-cards").innerHTML = cards.map((item) => metric(...item)).join("");
  const scanned = value(overview, "scanned_at", "generated_at", "updated_at");
  $("scan-time").textContent = scanned ? `Scanned ${fmtDate(scanned)}` : "Artifact catalog loaded";
  const root = value(overview, "project_root", "root");
  if (root) $("footer-root").textContent = root;
  const diagnostics = diagnosticItems(overview);
  state.diagnostics = diagnostics;
  $("diagnostic-count").textContent = `${diagnostics.length} notice${diagnostics.length === 1 ? "" : "s"}`;
  renderDiagnostics("overview-diagnostics", diagnostics, "All discovered artifact headers are readable.");
  renderDiagnostics("all-diagnostics", diagnostics, "No loader warnings.");
}

function runId(run) { return String(value(run, "run_id", "id", "name") || ""); }
function renderRunSelector() {
  const selector = $("run-selector");
  const previous = selector.value;
  if (!state.runs.length) {
    selector.innerHTML = '<option value="">No run manifests or evaluation histories found</option>';
    selector.disabled = true;
    return;
  }
  selector.disabled = false;
  selector.innerHTML = state.runs.map((run) => {
    const id = runId(run), status = value(run, "status", "manifest.status") || "artifact-only";
    const evals = value(run, "evaluation_count", "evaluation_points", "evaluations") || 0;
    return `<option value="${escapeHTML(id)}">${escapeHTML(id)} · ${escapeHTML(status)} · ${fmtInt(evals)} evals</option>`;
  }).join("");
  selector.value = state.runs.some((run) => runId(run) === previous) ? previous : runId(state.runs[0]);
}

function firstDefined(object, candidates, fallback = "—") {
  const found = value(object, ...candidates);
  if (found === undefined || found === null || found === "") return fallback;
  if (typeof found === "object") return Array.isArray(found) ? found.join(", ") : pretty(found);
  return found;
}

function renderKv(targetId, pairs) {
  $(targetId).innerHTML = pairs.map(([label, displayed]) => `<dl class="kv"><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(displayed ?? "—")}</dd></dl>`).join("");
}

function getManifest() { return value(state.run, "manifest", "training_run", "raw.manifest") || {}; }
function getEvaluationDocument() { return value(state.run, "evaluation", "evaluations_document", "evaluation_history") || {}; }

function renderRun() {
  const detail = state.run || {}, manifest = getManifest();
  const id = value(detail, "run_id", "id") || value(manifest, "run_id") || $("run-selector").value;
  const status = value(manifest, "status") || value(detail, "status") || "artifact-only";
  const phase = value(manifest, "phase") || value(detail, "phase") || "unknown";
  $("run-status").innerHTML = `<span class="status-pill ${escapeHTML(String(status).toLowerCase())}">${escapeHTML(status)}</span><span class="chip">${escapeHTML(phase)}</span>`;

  const latest = evaluationGroups().at(-1);
  const started = firstDefined(manifest, ["timestamps.started_at", "started_at"]);
  const finished = firstDefined(manifest, ["timestamps.finished_at", "timestamps.updated_at", "completed_at"]);
  $("run-summary").className = "run-summary";
  $("run-summary").innerHTML = `<div class="summary-title"><div><h3>${escapeHTML(id)}</h3><p>${escapeHTML(started)} → ${escapeHTML(finished)}</p></div><span class="chip">${fmtInt(state.evaluationGames.length)} eval games</span></div>
    <div class="summary-kpis"><span>Latest checkpoint<strong>${latest ? fmtInt(latest.timestep) : "—"}</strong></span><span>Score / 95% lower<strong>${latest ? `${fmtRate(latest.score)} / ${latest.lowerAvailable ? fmtRate(latest.lower) : "not recorded"}` : "—"}</strong></span><span>Best candidate<strong>${fmtInt(firstDefined(getEvaluationDocument(), ["best_candidate_timestep", "best_timestep"], 0)) || "—"}</strong></span><span>Training timesteps<strong>${fmtInt(firstDefined(manifest, ["metrics.actual_added_timesteps", "resolved.total_timesteps", "request.cli.timesteps"], 0))}</strong></span></div>`;

  renderKv("run-config", [
    ["Run ID", id], ["Project version", firstDefined(manifest, ["project_version"])],
    ["Curriculum", firstDefined(manifest, ["resolved.curriculum.id", "request.cli.curriculum"])],
    ["Reward contract", firstDefined(manifest, ["resolved.reward_contract", "resolved.reward_contract_version"])],
    ["Requested steps", firstDefined(manifest, ["request.cli.timesteps", "resolved.total_timesteps"])],
    ["Workers", firstDefined(manifest, ["resolved.n_envs", "request.cli.n_envs"])],
    ["Evaluation schedule", firstDefined(manifest, ["resolved.evaluation_schedule.sha256", "lineage.evaluation_schedule.sha256"])],
    ["CLI", firstDefined(manifest, ["request.argv"])],
  ]);
  renderKv("run-lineage", [
    ["Format", firstDefined(manifest, ["lineage.format"])],
    ["Observation", compact(firstDefined(manifest, ["lineage.observation_schema.sha256"]), 18)],
    ["Registry", compact(firstDefined(manifest, ["lineage.card_registry.sha256"]), 18)],
    ["Features", compact(firstDefined(manifest, ["lineage.feature_schema.sha256"]), 18)],
    ["Corpus", compact(firstDefined(manifest, ["lineage.corpus.sha256"]), 18)],
    ["Git revision", compact(firstDefined(manifest, ["source.git.revision"]), 18)],
    ["Device", firstDefined(manifest, ["runtime.cuda_devices.0.name", "runtime.device", "resolved.device"])],
    ["Python / Torch", `${firstDefined(manifest, ["runtime.python.version", "runtime.python_version"])} / ${firstDefined(manifest, ["runtime.dependencies.torch"])}`],
  ]);
  $("run-manifest-json").textContent = pretty(manifest);

  const combinedDiagnostics = [...state.diagnostics, ...diagnosticItems(detail)];
  renderDiagnostics("all-diagnostics", combinedDiagnostics, "No loader warnings.");
  renderEvaluationFilters();
  applyEvaluationFilters();
  renderTrend();
}

function gameTimestep(game) { return number(value(game, "evaluation_timestep", "timesteps", "checkpoint_timestep", "raw.evaluation_timestep")); }
function gameCase(game) { return number(value(game, "case_index", "raw.case_index"), -1); }
function gameKey(game) { return String(value(game,"record_id") || `${gameTimestep(game)}:${gameCase(game)}:${value(game, "checkpoint_sha256", "raw.checkpoint_sha256") || ""}`); }
function gameResult(game) { return String(value(game, "game_result", "result", "raw.game_result") || "unknown"); }
function gameSeat(game) {
  const explicit = value(game, "agent_seat", "seat");
  if (explicit) return String(explicit).toUpperCase().replace("AGENT AS ", "");
  return value(game, "agent_is_p1", "case.agent_is_p1", "raw.case.agent_is_p1") === false ? "P2" : "P1";
}
function gameTrace(game) {
  const direct = value(game, "debug", "evaluation_debug");
  return value(game, "trace", "debug.trace", "evaluation_debug.trace", "raw.trace", "raw.debug.trace", "raw.evaluation_debug.trace") || (Array.isArray(direct) ? direct : null);
}
function gameReplay(game) {
  return value(game, "replay", "debug.replay", "evaluation_debug.replay", "raw.replay", "raw.debug.replay", "raw.evaluation_debug.replay") || null;
}
function traceAvailable(game) {
  if (game._debugLoaded) return Boolean(gameTrace(game));
  return Boolean(gameTrace(game) || value(game,"trace_available") || number(value(game,"trace_event_count")) > 0);
}
function replayAvailable(game) {
  if (game._debugLoaded) return Boolean(gameReplay(game));
  return Boolean(gameReplay(game) || value(game,"replay_available") || value(game,"replay_path") || number(value(game,"replay_action_count")) > 0);
}
function debugHasTerminalPayload(debug) {
  if (!debug || typeof debug !== "object" || Array.isArray(debug)) return false;
  return [
    "terminal", "terminal_only", "terminal_reason", "game_result",
    "reward_components", "reward_diagnostics", "policy_state", "fidelity",
    "final_state", "done", "truncated",
  ].some((key) => debug[key] !== undefined && debug[key] !== null);
}
function terminalDebugAvailable(game) {
  const loaded = value(game,"debug","evaluation_debug","raw.debug","raw.evaluation_debug");
  const artifact = value(game,"_debugArtifact","debug_artifact") || {};
  const legacyTerminalSource = ["diagnostics","policy_state"].includes(value(artifact,"source_key"));
  if (game._debugLoaded) return Boolean(debugHasTerminalPayload(loaded) || loaded && legacyTerminalSource);
  return Boolean(debugHasTerminalPayload(loaded) || value(game,"terminal_debug_available") || value(game,"debug_path"));
}
function anyDebugAvailable(game) { return traceAvailable(game) || replayAvailable(game) || terminalDebugAvailable(game); }
function debugAvailabilityLabel(game) {
  const labels = [];
  if (traceAvailable(game)) labels.push("trace");
  if (replayAvailable(game)) labels.push("replay");
  if (terminalDebugAvailable(game)) labels.push("terminal");
  return labels.join(" + ") || "summary";
}

function evaluatorCaptureEvents(capture) {
  if (Array.isArray(capture)) return capture;
  if (!capture || typeof capture !== "object") return [];
  return array(value(capture, "events", "evaluator_events"));
}

function collectEvaluatorActivity(game, debug, actions) {
  const terminal = value(
    debug || {}, "evaluator", "terminal.evaluator"
  ) || value(
    game, "evaluator", "terminal.evaluator", "raw.evaluator",
    "raw.terminal.evaluator"
  ) || null;
  const looseEvents = [
    ...array(value(game, "evaluator_events", "raw.evaluator_events")),
    ...array(value(debug || {}, "evaluator_events", "terminal.evaluator_events")),
  ];
  const captures = actions.map((action, index) => {
    const capture = value(action, "evaluator", "evaluator_activity") || null;
    const events = [
      ...evaluatorCaptureEvents(capture),
      ...array(value(action, "evaluator_events")),
    ];
    return {action, actionIndex: index, capture, events};
  }).filter((item) => item.capture || item.events.length);
  return {terminal, looseEvents, captures};
}

function renderEvaluatorEvent(item, absoluteIndex) {
  const event = item.event;
  const components = value(event, "components") || {};
  const history = value(event, "history") || {};
  const adjustments = value(event, "adjustments") || {};
  const canonicalId = value(event, "canonical_card_id", "card_id");
  const runtimeId = value(event, "runtime_card_id");
  const name = value(event, "card_name", "name") || "Unknown card";
  const context = value(event, "context") || "general";
  const perspective = value(event, "perspective") || "unknown perspective";
  const historySource = value(event, "history_source", "history.source") || "not recorded";
  const reliable = value(event, "history_reliable", "history.reliable", "reliable");
  const fallback = value(event, "fallback_reason", "history.fallback_reason");
  const error = value(event, "error", "history.card_memory_error");
  const evidenceParts = [
    value(history, "overall_games") !== undefined ? `overall ${value(history,"overall_games")}` : null,
    value(history, "archetype_games") !== undefined ? `archetype ${value(history,"archetype_games")}` : null,
    value(history, "deck_stats_games") !== undefined ? `DeckStats ${value(history,"deck_stats_games")}` : null,
  ].filter(Boolean);
  const rawFlags = value(event, "flags");
  const flags = Array.isArray(rawFlags)
    ? rawFlags
    : objectEntries(rawFlags).filter(([, enabled]) => Boolean(enabled)).map(([flag]) => flag);
  const warningTags = [
    fallback ? `fallback: ${fallback}` : null,
    error ? `exception: ${typeof error === "string" ? error : pretty(error)}` : null,
    ...flags,
  ].filter(Boolean);
  const component = (name) => value(components, name, `${name}_value`, name === "base" ? "base_value" : "");
  return `<article class="evaluator-event ${error ? "has-error" : fallback ? "has-warning" : ""}">
    <div class="evaluator-event-heading"><div><span class="seq">${String(value(event,"sequence") ?? absoluteIndex + 1).padStart(3, "0")}</span><strong>${escapeHTML(name)}</strong></div><span class="chip">${escapeHTML(context)} · ${escapeHTML(perspective)}</span></div>
    <p class="evaluator-source">${escapeHTML(item.source)}</p>
    <div class="evaluator-components"><span>Final<strong>${fmtOptionalReward(value(event, "final_score", "score"))}</strong></span><span>Base<strong>${fmtOptionalReward(component("base"))}</strong></span><span>Context<strong>${fmtOptionalReward(component("context"))}</strong></span><span>History<strong>${fmtOptionalReward(component("history"))}</strong></span><span>Stats<strong>${fmtOptionalReward(component("stats"))}</strong></span></div>
    <p>Canonical ID ${escapeHTML(canonicalId ?? "—")} · runtime ID ${escapeHTML(runtimeId ?? "—")} · history ${escapeHTML(historySource)} · reliable ${reliable === undefined ? "unknown" : reliable ? "yes" : "no"}${evidenceParts.length ? ` · evidence ${escapeHTML(evidenceParts.join(", "))}` : ""}</p>
    <p>Pre-clamp ${escapeHTML(fmtOptionalReward(value(adjustments,"pre_clamp")))} · stage ×${escapeHTML(value(adjustments,"stage_multiplier") ?? "—")} · position ×${escapeHTML(value(adjustments,"position_multiplier") ?? "—")} · aggression ×${escapeHTML(value(adjustments,"aggression_multiplier") ?? "—")}</p>
    ${warningTags.length ? `<div class="detail-tags">${warningTags.map((tag) => `<span class="chip warning-chip">${escapeHTML(tag)}</span>`).join("")}</div>` : ""}
    <details class="raw-drawer evaluator-raw" data-evaluator-event-index="${absoluteIndex}"><summary>Complete evaluator event</summary><pre class="lazy-raw">Open to materialize this event’s JSON.</pre></details>
  </article>`;
}

function renderEvaluatorActivity(game, debug, actions) {
  const activity = collectEvaluatorActivity(game, debug, actions);
  const terminalSummary = value(activity.terminal || {}, "summary") || {};
  const items = [];
  for (const capture of activity.captures) {
    const action = value(capture.action, "action", "action_idx", "index");
    const label = value(capture.action, "label", "action_label", "reason") || `Action ${action}`;
    const deduplicated = value(capture.capture || {}, "deduplicated_events");
    const dropped = value(capture.capture || {}, "dropped_events");
    const suffix = `${deduplicated === undefined ? "" : ` · ${fmtInt(deduplicated)} deduplicated`}${dropped === undefined ? "" : ` · ${fmtInt(dropped)} dropped`}`;
    capture.events.forEach((event) => items.push({event, source:`Trace ${capture.actionIndex + 1} · ${label}${suffix}`}));
  }
  activity.looseEvents.forEach((event) => items.push({event, source:"Episode-level evaluator activity"}));
  const unattached = value(activity.terminal || {}, "unattached");
  evaluatorCaptureEvents(unattached).forEach((event) => items.push({event, source:"Post-terminal unattached activity"}));
  state.currentEvaluatorEvents = items;
  state.currentEvaluatorTerminal = activity.terminal;
  if (!activity.terminal && !items.length) return "";
  const totalEvents = items.length;
  const summaryFields = [
    ["Calls", value(terminalSummary, "calls")],
    ["Recorded", value(terminalSummary, "recorded") ?? totalEvents],
    ["Deduplicated", value(terminalSummary, "deduplicated")],
    ["Dropped", value(terminalSummary, "dropped")],
    ["Fallbacks", value(terminalSummary, "fallbacks")],
    ["Exceptions", value(terminalSummary, "exceptions")],
    ["Cache hit / miss", `${value(terminalSummary, "cache_hits") ?? "—"} / ${value(terminalSummary, "cache_misses") ?? "—"}`],
    ["Pending", value(terminalSummary, "pending")],
  ];
  const pageCount = Math.max(1, Math.ceil(totalEvents / state.evaluatorPageSize));
  state.evaluatorPage = Math.min(Math.max(0,state.evaluatorPage),pageCount - 1);
  const start = state.evaluatorPage * state.evaluatorPageSize;
  const visible = items.slice(start,start + state.evaluatorPageSize);
  const eventsHtml = visible.map((item,index) => renderEvaluatorEvent(item,start + index)).join("");
  const pager = totalEvents > state.evaluatorPageSize ? `<div class="evaluator-pager"><button class="button ghost" data-evaluator-page="${state.evaluatorPage - 1}" ${state.evaluatorPage === 0 ? "disabled" : ""}>← Previous events</button><span>${fmtInt(start + 1)}–${fmtInt(start + visible.length)} / ${fmtInt(totalEvents)}</span><button class="button ghost" data-evaluator-page="${state.evaluatorPage + 1}" ${state.evaluatorPage + 1 >= pageCount ? "disabled" : ""}>Next events →</button></div>` : "";
  return `<section class="evaluator-activity">
    <div class="evaluator-notice"><strong>Evaluator activity · non-causal diagnostics</strong><span>These calls were captured before or during atomic actions and can include observation inputs or automatic subchoices. They do not claim why PPO selected an action.</span></div>
    <div class="summary-kpis evaluator-summary">${summaryFields.map(([label, amount]) => `<span>${escapeHTML(label)}<strong>${escapeHTML(amount ?? "—")}</strong></span>`).join("")}</div>
    ${pager}${eventsHtml || '<div class="empty-state">No individual evaluator events were retained.</div>'}${pager}
    ${activity.terminal ? `<details class="raw-drawer" data-lazy-raw="evaluator-terminal"><summary>Complete terminal evaluator diagnostics</summary><pre class="lazy-raw">Open to materialize evaluator diagnostics JSON.</pre></details>` : ""}
  </section>`;
}

function evaluationGroups() {
  const groups = new Map();
  for (const game of state.evaluationGames) {
    const timestep = gameTimestep(game);
    if (!groups.has(timestep)) groups.set(timestep, {timestep, games: [], wins: 0, losses: 0, draws: 0, timeouts: 0, promoted: false, qualified: false});
    const group = groups.get(timestep), result = gameResult(game);
    group.games.push(game);
    if (result === "win" && !value(game, "timeout", "raw.timeout")) group.wins++;
    else if (result === "loss" && !value(game, "timeout", "raw.timeout")) group.losses++;
    else group.draws++;
    if (value(game, "timeout", "raw.timeout")) group.timeouts++;
    group.promoted ||= Boolean(value(game, "promoted", "candidate_promoted", "evaluation.promoted"));
    group.qualified ||= Boolean(value(game, "qualified", "evaluation.qualified"));
  }
  for (const group of groups.values()) {
    const nonTimeoutDraws = group.games.filter((game) => gameResult(game) === "draw" && !value(game, "timeout", "raw.timeout")).length;
    group.score = (group.wins + .5 * nonTimeoutDraws) / Math.max(1, group.games.length);
    const stored = value(group.games[0], "qualification_score", "evaluation.qualification_score", "summary.qualification_score");
    if (stored !== undefined) group.score = number(stored);
    const lower = value(
      group.games[0],
      "evaluation.qualification_interval.lower_bound",
      "summary.qualification_interval.lower_bound"
    );
    group.lowerAvailable = lower !== undefined;
    group.lower = number(lower, group.score);
    group.metric = group.lowerAvailable ? group.lower : group.score;
  }
  return [...groups.values()].sort((a, b) => a.timestep - b.timestep);
}

function renderTrend() {
  const svg = $("evaluation-trend"), groups = evaluationGroups();
  svg.replaceChildren();
  if (!groups.length) {
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "380"); text.setAttribute("y", "130"); text.setAttribute("text-anchor", "middle"); text.setAttribute("class", "trend-axis-label"); text.textContent = "No evaluation checkpoints for this run"; svg.append(text);
    $("trend-caption").textContent = "No evaluations"; $("trend-legend").textContent = ""; return;
  }
  const ns = "http://www.w3.org/2000/svg", width = 760, height = 260, left = 48, right = 18, top = 18, bottom = 38;
  const x = (index) => left + index * ((width - left - right) / Math.max(1, groups.length - 1));
  const y = (score) => top + (1 - score) * (height - top - bottom);
  const defs = document.createElementNS(ns, "defs"); defs.innerHTML = '<linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#54d8c3" stop-opacity=".38"/><stop offset="1" stop-color="#54d8c3" stop-opacity="0"/></linearGradient>'; svg.append(defs);
  [0, .25, .5, .75, 1].forEach((score) => {
    const line = document.createElementNS(ns, "line"); line.setAttribute("x1", left); line.setAttribute("x2", width-right); line.setAttribute("y1", y(score)); line.setAttribute("y2", y(score)); line.setAttribute("class", "trend-grid"); svg.append(line);
    const label = document.createElementNS(ns, "text"); label.setAttribute("x", left-9); label.setAttribute("y", y(score)+3); label.setAttribute("text-anchor", "end"); label.setAttribute("class", "trend-axis-label"); label.textContent = `${score*100}%`; svg.append(label);
  });
  const threshold = number(value(getEvaluationDocument(), "minimum_qualification_score"), .55);
  const thresholdLine = document.createElementNS(ns, "line"); thresholdLine.setAttribute("x1", left); thresholdLine.setAttribute("x2", width-right); thresholdLine.setAttribute("y1", y(threshold)); thresholdLine.setAttribute("y2", y(threshold)); thresholdLine.setAttribute("class", "trend-threshold"); svg.append(thresholdLine);
  const points = groups.map((group, index) => [x(index), y(group.metric)]);
  const area = document.createElementNS(ns, "path"); area.setAttribute("d", `M ${points[0][0]} ${height-bottom} L ${points.map((point) => point.join(" ")).join(" L ")} L ${points.at(-1)[0]} ${height-bottom} Z`); area.setAttribute("class", "trend-area"); svg.append(area);
  const path = document.createElementNS(ns, "path"); path.setAttribute("d", `M ${points.map((point) => point.join(" ")).join(" L ")}`); path.setAttribute("class", "trend-line"); svg.append(path);
  groups.forEach((group, index) => {
    const point = document.createElementNS(ns, "circle"); point.setAttribute("cx", points[index][0]); point.setAttribute("cy", points[index][1]); point.setAttribute("r", 5); point.setAttribute("class", `trend-point ${group.promoted ? "promoted" : ""}`);
    const title = document.createElementNS(ns, "title"); title.textContent = `${fmtInt(group.timestep)}: score ${fmtRate(group.score)} · ${group.lowerAvailable ? `95% lower ${fmtRate(group.lower)}` : "lower bound not recorded"} · ${group.wins}W ${group.losses}L ${group.timeouts} timeout`; point.append(title); svg.append(point);
    const label = document.createElementNS(ns, "text"); label.setAttribute("x", points[index][0]); label.setAttribute("y", height-16); label.setAttribute("text-anchor", "middle"); label.setAttribute("class", "trend-axis-label"); label.textContent = group.timestep >= 1e6 ? `${(group.timestep/1e6).toFixed(1)}m` : `${Math.round(group.timestep/1000)}k`; svg.append(label);
  });
  const latest = groups.at(-1); $("trend-caption").textContent = `${groups.length} checkpoints · latest ${latest.lowerAvailable ? `lower bound ${fmtRate(latest.lower)}` : `legacy score ${fmtRate(latest.score)}`}`;
  $("trend-legend").textContent = `Pair-aware 95% lower bound when recorded; legacy histories use point score · dashed line ${fmtRate(threshold)} threshold`;
}

function renderEvaluationFilters() {
  const checkpoint = $("eval-checkpoint-filter"), prior = checkpoint.value;
  checkpoint.innerHTML = '<option value="">All checkpoints</option>' + evaluationGroups().map((group) => `<option value="${group.timestep}">${fmtInt(group.timestep)} · ${fmtRate(group.score)} score / ${group.lowerAvailable ? `${fmtRate(group.lower)} LB` : "legacy"} · ${group.wins}W/${group.losses}L/${group.timeouts}T</option>`).join("");
  if ([...checkpoint.options].some((option) => option.value === prior)) checkpoint.value = prior;
  const reasons = [...new Set(state.evaluationGames.map((game) => value(game, "terminal_reason", "raw.terminal_reason")).filter(Boolean))].sort();
  const reasonSelect = $("eval-terminal-filter"), previousReason = reasonSelect.value;
  reasonSelect.innerHTML = '<option value="">All reasons</option>' + reasons.map((reason) => `<option value="${escapeHTML(reason)}">${escapeHTML(reason)}</option>`).join("");
  if (reasons.includes(previousReason)) reasonSelect.value = previousReason;
}

function applyEvaluationFilters() {
  const timestep = $("eval-checkpoint-filter").value, result = $("eval-result-filter").value;
  const terminal = $("eval-terminal-filter").value, seat = $("eval-seat-filter").value;
  const debug = $("eval-trace-filter").value, search = $("eval-search").value.trim().toLowerCase();
  state.filteredGames = state.evaluationGames.filter((game) => {
    if (timestep && String(gameTimestep(game)) !== timestep) return false;
    if (result && gameResult(game) !== result) return false;
    if (terminal && String(value(game, "terminal_reason", "raw.terminal_reason")) !== terminal) return false;
    if (seat && gameSeat(game) !== seat) return false;
    if (debug === "trace" && !traceAvailable(game)) return false;
    if (debug === "replay" && !replayAvailable(game)) return false;
    if (debug === "terminal" && !terminalDebugAvailable(game)) return false;
    if (debug === "legacy" && anyDebugAvailable(game)) return false;
    if (debug === "timeout" && !value(game, "timeout", "raw.timeout")) return false;
    if (search && !pretty(game).toLowerCase().includes(search)) return false;
    return true;
  });
  renderEvaluationTable();
}

function renderEvaluationTable() {
  const body = $("eval-games-body");
  $("eval-visible-count").textContent = `${fmtInt(state.filteredGames.length)} / ${fmtInt(state.evaluationGames.length)} games`;
  if (!state.filteredGames.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty-cell">No evaluation games match these filters.</td></tr>';
    $("evaluation-game-detail").innerHTML = '<div class="empty-state">Adjust the filters or select another run.</div>';
    return;
  }
  body.innerHTML = state.filteredGames.map((game) => {
    const key = gameKey(game), caseIndex = gameCase(game), pairIndex = value(game, "pair_index") ?? (caseIndex >= 0 ? Math.floor(caseIndex/2) : "—");
    const agentDeck = value(game, "agent_deck", "raw.agent_deck", "case.agent_deck") || (gameSeat(game) === "P1" ? value(game,"p1_deck","case.p1_deck","raw.case.p1_deck") : value(game,"p2_deck","case.p2_deck","raw.case.p2_deck"));
    const opponentDeck = value(game, "opponent_deck", "raw.opponent_deck", "case.opponent_deck") || (gameSeat(game) === "P1" ? value(game,"p2_deck","case.p2_deck","raw.case.p2_deck") : value(game,"p1_deck","case.p1_deck","raw.case.p1_deck"));
    const result = gameResult(game), timeout = Boolean(value(game,"timeout","raw.timeout"));
    return `<tr data-game-key="${escapeHTML(key)}" class="${key === state.selectedGameKey ? "selected" : ""}"><td>${fmtInt(gameTimestep(game))}</td><td>#${caseIndex} / P${escapeHTML(pairIndex)}</td><td title="${escapeHTML(agentDeck)}">${escapeHTML(compact(agentDeck,22))}</td><td title="${escapeHTML(opponentDeck)}">${escapeHTML(compact(opponentDeck,22))}</td><td>${gameSeat(game)}</td><td><span class="result ${escapeHTML(result)}">${escapeHTML(result)}</span></td><td>${escapeHTML(timeout ? "turn_limit / timeout" : value(game,"terminal_reason","raw.terminal_reason") || "—")}</td><td>${fmtInt(value(game,"length","raw.length"))}</td><td>${fmtReward(value(game,"reward","raw.reward"))}</td><td class="${anyDebugAvailable(game) ? "trace-yes" : "trace-no"}">${escapeHTML(debugAvailabilityLabel(game))}</td></tr>`;
  }).join("");
  body.querySelectorAll("tr[data-game-key]").forEach((row) => row.addEventListener("click", () => selectEvaluationGame(row.dataset.gameKey)));
  if (!state.filteredGames.some((game) => gameKey(game) === state.selectedGameKey)) state.selectedGameKey = gameKey(state.filteredGames[0]);
  selectEvaluationGame(state.selectedGameKey, false);
}

async function selectEvaluationGame(key, rerenderRows = true) {
  const game = state.evaluationGames.find((candidate) => gameKey(candidate) === key);
  if (!game) return;
  state.selectedGameKey = key;
  if (state.evaluatorGameKey !== key) {
    state.evaluatorGameKey = key;
    state.evaluatorPage = 0;
  }
  if (state.actionGameKey !== key) {
    state.actionGameKey = key;
    state.actionPage = 0;
  }
  if (rerenderRows) $("eval-games-body").querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row.dataset.gameKey === key));
  const detail = $("evaluation-game-detail");
  if (anyDebugAvailable(game) && !game._debugLoaded) {
    const requestedRunId = $("run-selector").value;
    detail.innerHTML = '<div class="empty-state">Loading the selected compressed game trace…</div>';
    try {
      const payload = await api(
        `/api/evaluation-game-debug?run_id=${encodeURIComponent(requestedRunId)}`
        + `&timestep=${encodeURIComponent(gameTimestep(game))}`
        + `&case_index=${encodeURIComponent(gameCase(game))}`
        + `&checkpoint_sha256=${encodeURIComponent(value(game,"checkpoint_sha256") || "")}`
        + `&record_id=${encodeURIComponent(value(game,"record_id") || "")}`
      );
      if (payload) {
        if (payload.debug !== undefined && payload.debug !== null) game.debug = payload.debug;
        if (payload.replay !== undefined && payload.replay !== null) game.replay = payload.replay;
        game._debugArtifact = payload.debug_artifact || null;
        game._replayArtifact = payload.replay_artifact || null;
        game._artifactErrors = [payload.debug_error,payload.replay_error].filter(Boolean);
        ["trace_available","replay_available","terminal_debug_available","debug_available"].forEach((field) => {
          if (typeof payload[field] === "boolean") game[field] = payload[field];
        });
      }
      game._debugLoaded = true;
    } catch (error) {
      game._debugLoaded = true;
      game._debugLoadError = error instanceof Error ? error.message : String(error);
    }
    if (state.selectedGameKey !== key
        || $("run-selector").value !== requestedRunId
        || !state.evaluationGames.includes(game)) return;
  }
  const caseData = value(game,"case","raw.case") || {}, resolved = value(game,"resolved_case","raw.resolved_case") || {};
  const pairIndex = value(game,"pair_index") ?? Math.floor(Math.max(0,gameCase(game))/2);
  const mate = state.evaluationGames.find((candidate) => candidate !== game && gameTimestep(candidate) === gameTimestep(game) && (value(candidate,"pair_index") ?? Math.floor(Math.max(0,gameCase(candidate))/2)) === pairIndex);
  const debug = value(game,"debug","evaluation_debug","raw.debug","raw.evaluation_debug");
  const replay = gameReplay(game), trace = gameTrace(game);
  const hasTraceReplay = (trace !== null && trace !== undefined)
    || (replay !== null && replay !== undefined);
  const actionCandidates = [
    array(value(trace,"events","actions")),
    array(value(replay,"actions")),
    Array.isArray(trace) ? trace : [],
  ];
  const actions = actionCandidates.find((candidate) => candidate.length) || [];
  state.currentActions = actions;
  const tags = [gameSeat(game), value(game,"opponent_profile","case.opponent_profile","raw.case.opponent_profile"), `seed ${value(game,"seed","case.seed","raw.case.seed")}`, value(game,"timeout","raw.timeout") ? "timeout" : "decisive"].filter(Boolean);
  const artifactErrors = array(game._artifactErrors);
  let traceNotice = game._debugLoadError
    ? `<div class="legacy-notice">The selected debug artifact could not be loaded: ${escapeHTML(game._debugLoadError)}</div>`
    : artifactErrors.length
      ? `<div class="legacy-notice">${artifactErrors.map((error) => escapeHTML(value(error,"message","error") || pretty(error))).join(" · ")}</div>`
      : hasTraceReplay
        ? `<div class="legacy-notice">A trace/replay payload was persisted, but it contains no recognized action array. Open the complete raw payload below.</div>`
        : anyDebugAvailable(game)
        ? `<div class="legacy-notice">Terminal diagnostics exist, but no action trace or replay actions were persisted for this game.</div>`
        : `<div class="legacy-notice">This historical game contains only its terminal summary. Its action timeline was never written and cannot be reconstructed.</div>`;
  const actionPageCount = Math.max(1,Math.ceil(actions.length / state.actionPageSize));
  state.actionPage = Math.min(Math.max(0,state.actionPage),actionPageCount - 1);
  const actionStart = state.actionPage * state.actionPageSize;
  const visibleActions = actions.slice(actionStart,actionStart + state.actionPageSize);
  const actionPager = actions.length > state.actionPageSize ? `<div class="action-pager"><button class="button ghost" data-action-page="${state.actionPage - 1}" ${state.actionPage === 0 ? "disabled" : ""}>← Previous actions</button><span>${fmtInt(actionStart + 1)}–${fmtInt(actionStart + visibleActions.length)} / ${fmtInt(actions.length)}</span><button class="button ghost" data-action-page="${state.actionPage + 1}" ${state.actionPage + 1 >= actionPageCount ? "disabled" : ""}>Next actions →</button></div>` : "";
  let traceHtml = actions.length ? `${actionPager}<div class="trace-list">${visibleActions.map((step,index) => {
    const absoluteIndex = actionStart + index;
    const action = value(step,"action","action_idx","index");
    const actor = value(step,"actor","seat") || "agent";
    const label = value(step,"label","action_label","reason") || `Action ${action}`;
    const timing = [value(step,"turn","turn_after","pre.turn"), value(step,"phase_name","phase_after","pre.phase_name","pre.phase")].filter((item) => item !== undefined).join(" · ");
    const evaluatorCount = evaluatorCaptureEvents(value(step,"evaluator","evaluator_activity")).length + array(value(step,"evaluator_events")).length;
    return `<div class="trace-step"><span class="seq">${String(absoluteIndex+1).padStart(3,"0")}</span><div><strong>${escapeHTML(actor)} · ${escapeHTML(label)}</strong><small>${escapeHTML(timing)}${timing ? " · " : ""}index ${escapeHTML(action)}${evaluatorCount ? ` · ${fmtInt(evaluatorCount)} evaluator events` : ""}</small><details class="raw-drawer action-raw" data-action-index="${absoluteIndex}"><summary>Complete action/context JSON</summary><pre class="lazy-raw">Open to materialize this action’s JSON.</pre></details></div></div>`;
  }).join("")}</div>${actionPager}` : traceNotice;
  if (actions.length && (game._debugLoadError || artifactErrors.length)) traceHtml = `${traceNotice}${traceHtml}`;
  if (hasTraceReplay) traceHtml += `<details class="raw-drawer" data-lazy-raw="trace-replay"><summary>Complete trace/replay payload</summary><pre class="lazy-raw">Open to materialize both trace and replay JSON.</pre></details>`;
  state.currentFullDebug = debug;
  state.currentTraceReplay = {trace,replay};
  const evaluatorHtml = renderEvaluatorActivity(game, debug, actions);
  const terminal = value(debug || {},"terminal");
  const artifactInfo = [game._debugArtifact,game._replayArtifact].filter(Boolean);

  detail.innerHTML = `<div class="summary-title"><div><p class="eyebrow">Checkpoint ${fmtInt(gameTimestep(game))} · case ${gameCase(game)}</p><h3>${escapeHTML(gameResult(game).toUpperCase())} · ${escapeHTML(value(game,"terminal_reason","raw.terminal_reason") || "unknown")}</h3></div><span class="result ${escapeHTML(gameResult(game))}">${escapeHTML(gameResult(game))}</span></div>
    <div class="detail-tags">${tags.map((tag) => `<span class="chip">${escapeHTML(tag)}</span>`).join("")}</div>
    <div class="summary-kpis"><span>Reward<strong>${fmtReward(value(game,"reward","raw.reward"))}</strong></span><span>Length<strong>${fmtInt(value(game,"length","raw.length"))}</strong></span><span>Pair mate<strong>${mate ? `${escapeHTML(gameResult(mate))} · ${gameSeat(mate)}` : "missing"}</strong></span><span>Persisted debug<strong>${escapeHTML(debugAvailabilityLabel(game))} · ${fmtInt(actions.length)} actions</strong></span></div>
    ${artifactInfo.length ? `<details class="raw-drawer"><summary>Artifact verification</summary><pre>${escapeHTML(pretty(artifactInfo))}</pre></details>` : ""}
    <h4>Evaluation case</h4><pre class="raw">${escapeHTML(pretty({requested:caseData,resolved}))}</pre>
    <h4>${actions.length ? `Action timeline · ${actions.length} events` : "Action timeline"}</h4>${traceHtml}
    ${evaluatorHtml ? `<h4>Evaluator activity</h4>${evaluatorHtml}` : ""}
    ${terminal ? `<h4>Terminal diagnostics</h4><pre class="raw">${escapeHTML(pretty(terminal))}</pre>` : ""}
    ${debug ? `<details class="raw-drawer" data-lazy-raw="full-debug"><summary>Complete verified debug payload</summary><pre class="lazy-raw">Open to materialize the complete debug JSON.</pre></details>` : ""}
    <h4>Complete persisted episode</h4><pre class="raw">${escapeHTML(pretty(game.raw_episode || game.raw || game))}</pre>`;
}

function sourceId(source) { return String(value(source,"source_id","id") || ""); }
function renderStatsSourceSelector() {
  const selector = $("stats-source-selector"), previous = selector.value;
  if (!state.statsSources.length) { selector.innerHTML = '<option value="">No DeckStats scopes found</option>'; selector.disabled = true; return; }
  selector.disabled = false;
  selector.innerHTML = state.statsSources.map((source) => {
    const id = sourceId(source), label = value(source,"label","name") || id;
    const games = value(source,"games","game_count") || 0;
    return `<option value="${escapeHTML(id)}">${escapeHTML(label)} · ${fmtInt(games)} games</option>`;
  }).join("");
  selector.value = state.statsSources.some((source) => sourceId(source) === previous) ? previous : sourceId(state.statsSources[0]);
}

function normalizeRecords(records, keyName) {
  if (Array.isArray(records)) return records;
  return objectEntries(records).map(([key, item]) => ({[keyName]: key, ...(item || {})}));
}
function deckRecords() { return normalizeRecords(value(state.stats,"decks","deck_records") || [], "deck_id"); }
function cardRecords() { return normalizeRecords(value(state.stats,"cards","card_records") || [], "card_id"); }
function rawRecord(record) { return record.raw || record; }

function canonicalCardId(record) {
  const identifier = value(record || {}, "card_id", "id", "raw.card_id", "raw.id");
  return identifier === undefined || identifier === null ? null : String(identifier);
}
function cardMemoryEnvelope() { return value(state.stats || {}, "card_memory") || {}; }
function cardMemoryEntries() {
  const summary = value(state.stats || {},"card_memory_summary") || {};
  if (value(summary,"contract_supported") !== true || value(summary,"contract_valid") !== true) return {};
  const cards = value(cardMemoryEnvelope(), "cards");
  if (!cards || typeof cards !== "object" || Array.isArray(cards)) return {};
  const validIds = new Set(array(value(summary,"valid_card_ids")).map(String));
  return Object.fromEntries(objectEntries(cards).filter(([cardId]) => validIds.has(String(cardId))));
}
function cardDisplayRecords() {
  const memoryCards = cardMemoryEntries(), joined = new Set();
  const records = cardRecords().map((aggregate) => {
    const cardId = canonicalCardId(aggregate);
    const memory = cardId !== null && memoryCards[cardId] && typeof memoryCards[cardId] === "object"
      ? memoryCards[cardId]
      : null;
    if (memory) joined.add(cardId);
    return {cardId, aggregate, memory, memoryOnly: false};
  });
  for (const [cardId, memory] of objectEntries(memoryCards)) {
    if (joined.has(String(cardId)) || !memory || typeof memory !== "object" || Array.isArray(memory)) continue;
    records.push({cardId: String(cardId), aggregate: null, memory, memoryOnly: true});
  }
  return records;
}
function memorySeenGames(memory) {
  if (!memory) return 0;
  return Math.min(
    number(value(memory, "games_played")),
    number(value(memory, "times_drawn")) + number(value(memory, "in_opening_hand"))
  );
}
function memoryOpeningRate(memory) {
  const samples = number(value(memory || {}, "in_opening_hand"));
  if (!samples) return null;
  return (
    number(value(memory, "wins_in_opening_hand"))
    + .5 * number(value(memory, "draws_in_opening_hand"))
  ) / samples;
}
function memoryOptimalTurn(memory) {
  let bestTurn = null, bestRate = Number.NEGATIVE_INFINITY;
  for (const [turn, performance] of objectEntries(value(memory || {}, "performance_by_turn"))) {
    const played = number(value(performance, "played"));
    if (played < 3) continue;
    const rate = (
      number(value(performance, "wins"))
      + .5 * number(value(performance, "draws"))
    ) / played;
    if (rate > bestRate) { bestTurn = number(turn); bestRate = rate; }
  }
  return bestTurn;
}
function memoryTrendHtml(memory) {
  const trend = array(value(memory || {}, "performance_trend"));
  if (!trend.length) return '<span class="trace-no">—</span>';
  return `<span class="memory-trend" title="Oldest to newest: win, draw, loss">${trend.map((sample) => {
    const score = number(sample);
    const outcome = score >= .75 ? "win" : score >= .25 ? "draw" : "loss";
    return `<i class="${outcome}" aria-label="${outcome}"></i>`;
  }).join("")}</span>`;
}
function memoryDecisionLabel(summary) {
  const mode = value(summary || {}, "decision_use.mode") || "unknown";
  if (mode === "adaptive_input") return "adaptive evaluator input";
  if (mode === "recorded_only") return "recorded analytics · not a decision input";
  return "decision use unknown";
}
function setMemoryHealthBadge(targetId, summary) {
  const target = $(targetId), health = String(value(summary || {}, "health") || "missing");
  target.className = `chip memory-health ${escapeHTML(health)}`;
  target.textContent = `${health} · ${value(summary || {}, "status") || "missing"}`;
}

function recordWins(record) { return number(value(record,"wins","raw.wins")); }
function recordLosses(record) { return number(value(record,"losses","raw.losses")); }
function recordDraws(record) { return number(value(record,"draws","raw.draws")); }
function recordGames(record) { return number(value(record,"games","games_played","raw.games","raw.games_played")); }
function recordWinRate(record) {
  const stored = value(record,"win_rate","raw.win_rate");
  return stored === undefined ? recordWins(record)/Math.max(1,recordGames(record)) : number(stored);
}

function renderCardMemoryStatus(summary) {
  setMemoryHealthBadge("card-memory-health", summary);
  const target = $("card-memory-status"), status = value(summary || {}, "status") || "missing";
  const envelope = cardMemoryEnvelope();
  if (status === "missing") {
    target.className = "memory-status empty-state";
    target.textContent = "No CardMemory JSON snapshot exists for this exact statistics scope.";
    return;
  }
  if (status === "unreadable" || status === "invalid") {
    target.className = "memory-status";
    target.innerHTML = `<div class="memory-mode unknown"><strong>CardMemory snapshot ${escapeHTML(status)}</strong><span>The viewer did not derive card statistics from this artifact. See loader diagnostics for the exact failure.</span></div><pre class="raw">${escapeHTML(pretty(summary))}</pre>`;
    return;
  }
  if (value(summary,"contract_supported") !== true || value(summary,"contract_valid") !== true) {
    const unsupported = value(summary,"contract_supported") !== true;
    target.className = "memory-status";
    target.innerHTML = `<div class="memory-mode unknown"><strong>${unsupported ? "Unsupported CardMemory schema" : "Malformed CardMemory contract"} · raw only</strong><span>No counters, ratings, trends, joins, or per-card memory fields are interpreted ${unsupported ? "for this schema" : "until the structural/range violations are fixed"}.</span></div><dl class="memory-file"><dt>Scope-local file</dt><dd>${escapeHTML(value(summary,"file") || "not found")}</dd></dl>${array(value(summary,"validation_issues")).length || array(value(summary,"envelope_validation_issues")).length ? `<details class="raw-drawer"><summary>Contract validation issues</summary><pre>${escapeHTML(pretty({entries:value(summary,"validation_issues"),envelope:value(summary,"envelope_validation_issues")}))}</pre></details>` : ""}<details class="raw-drawer" open><summary>Complete raw CardMemory envelope</summary><pre>${escapeHTML(pretty(envelope))}</pre></details>`;
    return;
  }
  const join = value(summary, "join") || {};
  const ambiguity = array(value(summary, "ambiguous_names"));
  const mode = value(summary, "decision_use.mode") || "unknown";
  const modeText = memoryDecisionLabel(summary);
  const modeDetail = mode === "adaptive_input"
    ? "The run manifest explicitly records adaptive history as enabled for this scope."
    : mode === "recorded_only"
      ? "The run manifest explicitly records adaptive history as disabled. Outcomes were still recorded, but this snapshot was not an evaluator input."
      : "No compatible run provenance states whether adaptive decision history was enabled. Artifact presence alone is not evidence of use.";
  target.className = "memory-status";
  target.innerHTML = `<div class="memory-kpis"><span>Schema<strong>${escapeHTML(value(summary,"schema_version") ?? "—")}</strong></span><span>Updated<strong>${escapeHTML(fmtDate(value(summary,"last_updated")))}</strong></span><span>Memory cards<strong>${fmtInt(value(summary,"card_count"))}</strong></span><span>Exact ID joins<strong>${fmtInt(value(join,"joined_card_count"))}</strong></span><span>Aggregate only<strong>${fmtInt(value(join,"aggregate_without_memory_count"))}</strong></span><span>Memory only<strong>${fmtInt(value(join,"memory_without_aggregate_count"))}</strong></span><span>Counter mismatches<strong>${fmtInt(value(join,"field_mismatch_count"))}</strong></span><span>Ambiguous names<strong>${fmtInt(value(summary,"ambiguous_name_count"))}</strong></span></div>
    <div class="memory-mode ${escapeHTML(mode)}"><strong>${escapeHTML(modeText)}</strong><span>${escapeHTML(modeDetail)}</span></div>
    <dl class="memory-file"><dt>Scope-local file</dt><dd>${escapeHTML(value(summary,"file") || "not found")}</dd></dl>
    ${ambiguity.length ? `<p><strong>Ambiguous names:</strong> ${ambiguity.map(escapeHTML).join(", ")}. These remain separate canonical IDs and are never name-joined.</p>` : ""}
    ${(number(value(join,"aggregate_without_memory_count")) || number(value(join,"memory_without_aggregate_count")) || number(value(join,"field_mismatch_count")) || number(value(summary,"mapping_problem_count"))) ? `<details class="raw-drawer"><summary>Join and integrity diagnostics</summary><pre>${escapeHTML(pretty(join))}</pre></details>` : ""}
    <details class="raw-drawer"><summary>Complete CardMemory envelope</summary><pre>${escapeHTML(pretty(envelope))}</pre></details>`;
}

function renderStrategyTopRows(records, kind) {
  if (!records.length) return '<tr><td colspan="6" class="empty-cell">No ranked records were exported.</td></tr>';
  return records.map((record) => {
    const rawPattern = value(record,"pattern");
    const pattern = Array.isArray(rawPattern) ? rawPattern.join(" › ") : rawPattern ?? "—";
    const action = value(record,"action","action_index");
    return `<tr><td>${escapeHTML(pattern)}</td><td>${kind === "actions" ? escapeHTML(action ?? "—") : "—"}</td><td>${fmtInt(value(record,"count","evidence"))}</td><td>${fmtOptionalReward(value(record,"mean_reward"))}</td><td>${fmtOptionalRate(value(record,"positive_reward_rate"))}</td><td>${escapeHTML(value(record,"last_update","logical_update") ?? "—")}</td></tr>`;
  }).join("");
}

function renderStrategyMemoryStatus(summary) {
  setMemoryHealthBadge("strategy-memory-health", summary);
  const target = $("strategy-memory-status"), status = value(summary || {}, "status") || "missing";
  const payload = value(state.stats || {}, "strategy_memory");
  const unsafe = value(summary || {}, "unsafe_pickle");
  const pickleVerification = value(summary || {}, "source_pickle_verification") || {};
  const configuredMode = value(summary || {}, "configuration.mode") || "unknown";
  if (status === "missing") {
    target.className = "memory-status empty-state";
    target.textContent = `No safe strategy-memory diagnostics or legacy pickle exists in this scope. Run-manifest configuration: ${configuredMode}.`;
    return;
  }
  if (status === "unsafe_pickle_only") {
    target.className = "memory-status";
    target.innerHTML = `<div class="memory-mode unknown"><strong>Opaque pickle only · not inspected</strong><span>The viewer found ${escapeHTML(value(unsafe,"file") || "strategy_memory.pkl")} but will never deserialize pickle. Generate the adjacent strategy_memory.json.gz diagnostic export to inspect it safely.</span></div><pre class="raw">${escapeHTML(pretty(unsafe))}</pre>`;
    return;
  }
  if (status === "unreadable" || status === "invalid") {
    target.className = "memory-status";
    target.innerHTML = `<div class="memory-mode unknown"><strong>Safe strategy diagnostics ${escapeHTML(status)}</strong><span>The file was not interpreted. See artifact diagnostics and raw summary metadata.</span></div><pre class="raw">${escapeHTML(pretty(summary))}</pre>`;
    return;
  }
  if (value(summary,"contract_supported") !== true || value(summary,"contract_valid") !== true) {
    const unsupported = value(summary,"contract_supported") !== true;
    const stale = !unsupported && value(pickleVerification,"verified") !== true;
    target.className = "memory-status";
    target.innerHTML = `<div class="memory-mode unknown"><strong>${unsupported ? "Unsupported StrategyMemory diagnostics" : stale ? "Unverified or stale StrategyMemory diagnostics" : "Malformed StrategyMemory v1 diagnostics"} · raw only</strong><span>No evidence counts, rewards, rates, rankings, or truncation fields are interpreted ${unsupported ? "for this kind/schema" : stale ? "unless the JSON marker matches the exact opaque pickle bytes" : "until the contract violations are fixed"}.</span></div><p>Safe file: ${escapeHTML(value(summary,"file") || "—")} · run-manifest configuration ${escapeHTML(configuredMode)}</p>${array(value(summary,"validation_issues")).length ? `<details class="raw-drawer"><summary>Contract validation issues</summary><pre>${escapeHTML(pretty(value(summary,"validation_issues")))}</pre></details>` : ""}<details class="raw-drawer"><summary>Source pickle verification</summary><pre>${escapeHTML(pretty({source_pickle:value(summary,"source_pickle"),verification:pickleVerification,unsafe_pickle:unsafe}))}</pre></details><details class="raw-drawer" open><summary>Complete raw diagnostics JSON</summary><pre>${escapeHTML(pretty(payload))}</pre></details>`;
    return;
  }
  const counts = value(summary,"counts") || {};
  const aggregates = value(summary,"aggregates") || {};
  const patternMeanReward = value(
    aggregates, "pattern_evidence_weighted_mean_reward",
    "patterns.mean_reward", "pattern.mean_reward"
  );
  const patternPositiveRate = value(
    aggregates, "pattern_evidence_weighted_positive_reward_rate",
    "patterns.positive_reward_rate", "pattern.positive_reward_rate"
  );
  const actionMeanReward = value(
    aggregates, "action_evidence_weighted_mean_reward",
    "actions.mean_reward", "action.mean_reward"
  );
  const actionPositiveRate = value(
    aggregates, "action_evidence_weighted_positive_reward_rate",
    "actions.positive_reward_rate", "action.positive_reward_rate"
  );
  const topPatterns = array(value(payload || {}, "top_patterns"));
  const topActions = array(value(payload || {}, "top_actions"));
  target.className = "memory-status";
  target.innerHTML = `<div class="memory-kpis"><span>Schema<strong>${escapeHTML(value(summary,"schema_version") ?? "—")}</strong></span><span>Logical update<strong>${fmtInt(value(summary,"logical_update"))}</strong></span><span>Patterns<strong>${fmtInt(value(counts,"patterns"))}</strong></span><span>Pattern evidence<strong>${fmtInt(value(counts,"pattern_evidence"))}</strong></span><span>Pattern actions<strong>${fmtInt(value(counts,"pattern_actions"))}</strong></span><span>Action evidence<strong>${fmtInt(value(counts,"action_evidence"))}</strong></span><span>Sequences<strong>${fmtInt(value(counts,"action_sequences"))}</strong></span></div>
    <div class="memory-mode recorded_only"><strong>Safe diagnostics · reward evidence, not game outcomes</strong><span>Every positive-reward rate below means shaped reward &gt; 0. It is not a game win rate and must not be interpreted as one.</span></div>
    <div class="strategy-aggregate-grid"><div><span>Evidence-weighted pattern mean reward</span><strong>${fmtOptionalReward(patternMeanReward)}</strong><small>Shaped &gt;0 ${fmtOptionalRate(patternPositiveRate)}</small></div><div><span>Evidence-weighted action mean reward</span><strong>${fmtOptionalReward(actionMeanReward)}</strong><small>Shaped &gt;0 ${fmtOptionalRate(actionPositiveRate)}</small></div></div>
    <p>Safe file: ${escapeHTML(value(summary,"file") || "—")} · run-manifest configuration ${escapeHTML(configuredMode)}${unsafe ? ` · runtime pickle also present as opaque metadata and was not opened` : ""}</p>
    <details class="memory-ranking" open><summary>Top patterns · ${fmtInt(topPatterns.length)}</summary><div class="table-wrap compact"><table><thead><tr><th>Pattern</th><th>Action</th><th>Evidence</th><th>Mean reward</th><th>Shaped &gt;0</th><th>Last update</th></tr></thead><tbody>${renderStrategyTopRows(topPatterns,"patterns")}</tbody></table></div></details>
    <details class="memory-ranking"><summary>Top actions · ${fmtInt(topActions.length)}</summary><div class="table-wrap compact"><table><thead><tr><th>Pattern</th><th>Action</th><th>Evidence</th><th>Mean reward</th><th>Shaped &gt;0</th><th>Last update</th></tr></thead><tbody>${renderStrategyTopRows(topActions,"actions")}</tbody></table></div></details>
    <details class="raw-drawer"><summary>Limits, truncation & verified opaque pickle metadata</summary><pre>${escapeHTML(pretty({limits:value(summary,"limits"),truncation:value(summary,"truncation"),source_pickle:value(summary,"source_pickle"),source_pickle_verification:pickleVerification,unsafe_pickle:unsafe}))}</pre></details>
    <details class="raw-drawer"><summary>Complete safe StrategyMemory diagnostics</summary><pre>${escapeHTML(pretty(payload))}</pre></details>`;
}

function renderStats() {
  const bundle = state.stats || {}, source = value(bundle,"source") || state.statsSources.find((item) => sourceId(item) === $("stats-source-selector").value) || {};
  const decks = deckRecords(), cards = cardRecords();
  const memorySummary = value(bundle, "card_memory_summary") || {};
  const strategySummary = value(bundle, "strategy_memory_summary") || {};
  const integrity = value(bundle,"integrity.status","status") || (diagnosticItems(bundle).length ? "Review notices" : "Loaded");
  $("stats-integrity").textContent = integrity;
  $("stats-source-summary").innerHTML = `<span>${fmtInt(value(bundle,"game_count","games","summary.games"))} games</span><span>${fmtInt(decks.length)} DeckStats decks</span><span>${fmtInt(cards.length)} DeckStats cards</span><span>${fmtInt(value(memorySummary,"card_count"))} CardMemory cards</span><span>${escapeHTML(memoryDecisionLabel(memorySummary))}</span><span>${escapeHTML(value(source,"scope","phase") || "scope")}</span>`;
  renderCardMemoryStatus(memorySummary);
  renderStrategyMemoryStatus(strategySummary);
  renderDeckTable(); renderCardTable();
  renderJsonSummary("meta-detail", value(bundle,"meta","meta_data"), "No meta artifact exists in this scope.");
  renderJsonSummary("fidelity-detail", {fidelity:value(bundle,"fidelity","fidelity_report"), support_manifest:value(bundle,"support_manifest","card_support_manifest")}, "No fidelity or support artifact exists in this scope.");
  const combined = [...state.diagnostics, ...diagnosticItems(state.run || {}), ...diagnosticItems(bundle)];
  renderDiagnostics("all-diagnostics", combined, "No loader warnings.");
}

function renderJsonSummary(targetId, payload, emptyText) {
  const target = $(targetId);
  if (!payload || (typeof payload === "object" && !Object.keys(payload).length)) { target.className = "json-summary empty-state"; target.textContent = emptyText; return; }
  target.className = "json-summary";
  target.innerHTML = objectEntries(payload).map(([key,item]) => `<div class="json-group"><h4>${escapeHTML(key.replaceAll("_"," "))}</h4><pre>${escapeHTML(typeof item === "object" ? pretty(item) : item)}</pre></div>`).join("") || `<pre>${escapeHTML(pretty(payload))}</pre>`;
}

function renderDeckTable() {
  const query = $("deck-search").value.trim().toLowerCase();
  const records = deckRecords().filter((record) => !query || pretty(record).toLowerCase().includes(query)).sort((a,b) => recordGames(b)-recordGames(a));
  const body = $("deck-table-body");
  if (!records.length) { body.innerHTML = '<tr><td colspan="6" class="empty-cell">No deck aggregates in this scope.</td></tr>'; return; }
  body.innerHTML = records.map((record,index) => `<tr data-deck-index="${index}"><td>${escapeHTML(value(record,"name","raw.name") || value(record,"deck_id") || "Unknown")}</td><td>${escapeHTML(value(record,"archetype","raw.archetype") || "—")}</td><td>${fmtInt(recordGames(record))}</td><td>${recordWins(record)}–${recordLosses(record)}–${recordDraws(record)}</td><td>${fmtRate(recordWinRate(record))}</td><td>${number(value(record,"avg_game_length","raw.avg_game_length")).toFixed(1)}</td></tr>`).join("");
  body.querySelectorAll("tr[data-deck-index]").forEach((row) => row.addEventListener("click", () => renderDeckDetail(records[number(row.dataset.deckIndex)])));
}

function renderDeckDetail(record) {
  if (!record) return;
  const raw = rawRecord(record), cards = array(value(raw,"card_list","cards"));
  const cardList = cards.length ? `<div class="card-list">${cards.map((card) => {
    const item = typeof card === "object" ? card : {name:card};
    return `<div><span>${escapeHTML(value(item,"name","card_name","id") || "Unknown")}</span><strong>${escapeHTML(value(item,"count","copies") || 1)}</strong></div>`;
  }).join("")}</div>` : '<p>No deck list was persisted in this aggregate.</p>';
  $("deck-detail").innerHTML = `<p class="eyebrow">${escapeHTML(value(record,"archetype","raw.archetype") || "Deck aggregate")}</p><h3>${escapeHTML(value(record,"name","raw.name") || value(record,"deck_id"))}</h3><div class="detail-tags"><span class="chip">${fmtInt(recordGames(record))} games</span><span class="chip">${fmtRate(recordWinRate(record))} win</span><span class="chip">${recordWins(record)}–${recordLosses(record)}–${recordDraws(record)}</span></div><h4>Complete deck list</h4>${cardList}<h4>Every aggregate field</h4><pre class="raw">${escapeHTML(pretty(raw))}</pre>`;
}

function renderTurnPerformance(memory) {
  const rows = objectEntries(value(memory || {}, "performance_by_turn"))
    .sort(([left], [right]) => number(left) - number(right));
  if (!rows.length) return '<p>No player-relative turn performance was recorded.</p>';
  return `<div class="table-wrap memory-detail-table"><table><thead><tr><th>Player turn</th><th>Played</th><th>W–L–D</th><th>Effective result</th></tr></thead><tbody>${rows.map(([turn, item]) => {
    const played = number(value(item,"played"));
    const rate = (number(value(item,"wins")) + .5 * number(value(item,"draws"))) / Math.max(1, played);
    return `<tr><td>${escapeHTML(turn)}</td><td>${fmtInt(played)}</td><td>${fmtInt(value(item,"wins"))}–${fmtInt(value(item,"losses"))}–${fmtInt(value(item,"draws"))}</td><td>${fmtRate(rate)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function renderCurvePerformance(memory) {
  const curve = value(memory || {}, "mana_curve_performance") || {};
  const labels = {on_curve:"On curve",below_curve:"Earlier than mana value",above_curve:"Later than mana value"};
  const rows = Object.keys(labels).map((key) => {
    const item = value(curve,key) || {}, played = number(value(item,"played"));
    const rate = (number(value(item,"wins")) + .5 * number(value(item,"draws"))) / Math.max(1,played);
    return `<div><span>${escapeHTML(labels[key])}</span><strong>${fmtInt(played)} played</strong><small>${played ? `${fmtRate(rate)} effective result` : "no samples"}</small></div>`;
  });
  return `<div class="memory-bucket-grid">${rows.join("")}</div>`;
}

function renderArchetypePerformance(memory) {
  const rows = objectEntries(value(memory || {}, "archetype_performance"))
    .sort(([,left],[,right]) => number(value(right,"games"))-number(value(left,"games")));
  if (!rows.length) return '<p>No archetype buckets were recorded.</p>';
  return `<div class="table-wrap memory-detail-table"><table><thead><tr><th>Archetype</th><th>Games</th><th>W–L–D</th><th>Effective result</th></tr></thead><tbody>${rows.map(([archetype,item]) => {
    const games = number(value(item,"games"));
    const rate = (number(value(item,"wins")) + .5 * number(value(item,"draws"))) / Math.max(1,games);
    return `<tr><td>${escapeHTML(archetype)}</td><td>${fmtInt(games)}</td><td>${fmtInt(value(item,"wins"))}–${fmtInt(value(item,"losses"))}–${fmtInt(value(item,"draws"))}</td><td>${fmtRate(rate)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function renderSynergyPerformance(memory) {
  const idToName = value(cardMemoryEnvelope(), "id_to_name") || {};
  const memoryCards = cardMemoryEntries();
  const rows = objectEntries(value(memory || {}, "synergy_partners"))
    .sort(([,left],[,right]) => number(value(right,"games_together"))-number(value(left,"games_together")));
  if (!rows.length) return '<p>No synergy partner evidence was recorded.</p>';
  return `<div class="table-wrap memory-detail-table"><table><thead><tr><th>Partner</th><th>ID</th><th>Together</th><th>W / D</th><th>Effective result</th></tr></thead><tbody>${rows.map(([partnerId,item]) => {
    const games = number(value(item,"games_together"));
    const rate = (number(value(item,"wins_together")) + .5 * number(value(item,"draws_together"))) / Math.max(1,games);
    const partnerName = idToName[partnerId] || value(memoryCards[partnerId] || {},"name") || `Card ${partnerId}`;
    return `<tr><td>${escapeHTML(partnerName)}</td><td>${escapeHTML(partnerId)}</td><td>${fmtInt(games)}</td><td>${fmtInt(value(item,"wins_together"))} / ${fmtInt(value(item,"draws_together"))}</td><td>${fmtRate(rate)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function renderCardTable() {
  const query = $("card-search").value.trim().toLowerCase();
  const records = cardDisplayRecords()
    .filter((record) => !query || pretty(record).toLowerCase().includes(query))
    .sort((a,b) => {
      const leftGames = a.aggregate ? recordGames(a.aggregate) : number(value(a.memory,"games_played"));
      const rightGames = b.aggregate ? recordGames(b.aggregate) : number(value(b.memory,"games_played"));
      return rightGames-leftGames || String(a.cardId).localeCompare(String(b.cardId),undefined,{numeric:true});
    });
  const body = $("card-table-body");
  if (!records.length) { body.innerHTML = '<tr><td colspan="11" class="empty-cell">No DeckStats aggregates or CardMemory records in this scope.</td></tr>'; $("card-detail").innerHTML = '<div class="empty-state">No canonical card records match this filter.</div>'; return; }
  body.innerHTML = records.map((record,index) => {
    const aggregate = record.aggregate, memory = record.memory;
    const name = value(aggregate || {},"name","raw.name") || value(memory || {},"name") || "Unknown";
    const opening = memoryOpeningRate(memory);
    return `<tr data-card-index="${index}" class="${record.memoryOnly ? "memory-only-row" : ""}"><td>${escapeHTML(name)}${record.memoryOnly ? '<small class="row-source">memory only</small>' : ""}</td><td>${escapeHTML(record.cardId ?? "—")}</td><td>${aggregate ? fmtInt(recordGames(aggregate)) : "—"}</td><td>${aggregate ? `${recordWins(aggregate)}–${recordLosses(aggregate)}–${recordDraws(aggregate)}` : "—"}</td><td>${aggregate ? fmtRate(recordWinRate(aggregate)) : "—"}</td><td>${aggregate ? fmtInt(value(aggregate,"usage_count","raw.usage_count")) : "—"}</td><td>${memory ? fmtInt(value(memory,"games_played")) : "—"}</td><td>${memory ? fmtRate(value(memory,"effectiveness_rating") ?? .5) : "—"}</td><td>${memory ? `${fmtInt(memorySeenGames(memory))} / ${fmtInt(value(memory,"times_played"))}` : "—"}</td><td>${opening === null ? "—" : `${fmtRate(opening)} · n=${fmtInt(value(memory,"in_opening_hand"))}`}</td><td>${memoryTrendHtml(memory)}</td></tr>`;
  }).join("");
  body.querySelectorAll("tr[data-card-index]").forEach((row) => row.addEventListener("click", () => {
    body.querySelectorAll("tr").forEach((candidate) => candidate.classList.toggle("selected", candidate === row));
    renderCardDetail(records[number(row.dataset.cardIndex)]);
  }));
  const firstRow = body.querySelector("tr[data-card-index]");
  if (firstRow) firstRow.classList.add("selected");
  renderCardDetail(records[0]);
}

function renderCardDetail(record) {
  if (!record) return;
  const aggregate = record.aggregate, memory = record.memory;
  const rawAggregate = aggregate ? rawRecord(aggregate) : null;
  const name = value(aggregate || {},"name","raw.name") || value(memory || {},"name") || "Unknown card";
  const opening = memoryOpeningRate(memory), optimalTurn = memoryOptimalTurn(memory);
  const mode = memoryDecisionLabel(value(state.stats || {},"card_memory_summary") || {});
  const metadata = memory ? Object.fromEntries([
    "first_seen", "cmc", "mana_cost", "types", "colors", "oracle_id", "meta_position"
  ].filter((key) => value(memory,key) !== undefined).map((key) => [key,value(memory,key)])) : {};
  const memoryHtml = memory ? `<h4>CardMemory lifetime snapshot</h4>
    <div class="summary-kpis memory-card-kpis"><span>Memory games<strong>${fmtInt(value(memory,"games_played"))}</strong></span><span>Effective W–L–D<strong>${fmtInt(value(memory,"wins"))}–${fmtInt(value(memory,"losses"))}–${fmtInt(value(memory,"draws"))}</strong></span><span>Effectiveness<strong>${fmtRate(value(memory,"effectiveness_rating") ?? .5)}</strong></span><span>Drawn / seen / played<strong>${fmtInt(value(memory,"times_drawn"))} / ${fmtInt(memorySeenGames(memory))} / ${fmtInt(value(memory,"times_played"))}</strong></span><span>Opening effective result<strong>${opening === null ? "—" : `${fmtRate(opening)} · n=${fmtInt(value(memory,"in_opening_hand"))}`}</strong></span><span>Optimal sampled turn<strong>${optimalTurn !== null ? `player turn ${optimalTurn}` : "insufficient evidence"}</strong></span></div>
    <div class="memory-mode ${escapeHTML(value(state.stats,"card_memory_summary.decision_use.mode") || "unknown")}"><strong>${escapeHTML(mode)}</strong><span>This is the selected worker/scope's latest cumulative snapshot, not a reconstruction of memory at an earlier game or checkpoint.</span></div>
    <h4>Recent outcomes · oldest to newest</h4><div class="trend-detail">${memoryTrendHtml(memory)}<span>${escapeHTML(array(value(memory,"performance_trend")).map((score) => number(score) >= .75 ? "W" : number(score) >= .25 ? "D" : "L").join(" ") || "No samples")}</span></div>
    <h4>Performance by player-relative play turn</h4>${renderTurnPerformance(memory)}
    <h4>Mana-curve timing</h4>${renderCurvePerformance(memory)}
    <h4>Archetype performance</h4>${renderArchetypePerformance(memory)}
    <h4>Synergy partners</h4>${renderSynergyPerformance(memory)}
    <h4>Identity, printed metadata & optional meta position</h4><pre class="raw">${escapeHTML(pretty(metadata))}</pre>
    <details class="raw-drawer" open><summary>Every CardMemory field for canonical ID ${escapeHTML(record.cardId)}</summary><pre>${escapeHTML(pretty(memory))}</pre></details>` : '<div class="legacy-notice">No CardMemory entry has this exact canonical ID. The viewer did not attempt a name-based fallback.</div>';
  const aggregateHtml = aggregate
    ? `<h4>DeckStats aggregate · separate counters</h4><div class="detail-tags"><span class="chip">${fmtInt(recordGames(aggregate))} games</span><span class="chip">${fmtRate(recordWinRate(aggregate))} win rate</span><span class="chip">used ${fmtInt(value(aggregate,"usage_count","raw.usage_count"))}</span></div><details class="raw-drawer"><summary>Every DeckStats aggregate field</summary><pre>${escapeHTML(pretty(rawAggregate))}</pre></details>`
    : '<div class="legacy-notice">This is a CardMemory-only identity with no DeckStats aggregate in the selected scope.</div>';
  $("card-detail").innerHTML = `<p class="eyebrow">Canonical ID ${escapeHTML(record.cardId ?? "not persisted")}</p><h3>${escapeHTML(name)}</h3><div class="detail-tags"><span class="chip">DeckStats ${aggregate ? "present" : "missing"}</span><span class="chip">CardMemory ${memory ? "present" : "missing"}</span></div>${memoryHtml}${aggregateHtml}`;
}

async function loadStatsGames() {
  const sourceIdValue = $("stats-source-selector").value;
  if (!sourceIdValue) { renderStatsGames({items:[],total:0,offset:0,limit:state.statsPageSize}); return; }
  const requestedPage = state.statsPage;
  const generation = ++state.statsGamesRequestGeneration;
  try {
    const payload = await api(`/api/stats-games?source_id=${encodeURIComponent(sourceIdValue)}&offset=${requestedPage*state.statsPageSize}&limit=${state.statsPageSize}`);
    if (generation !== state.statsGamesRequestGeneration || $("stats-source-selector").value !== sourceIdValue || state.statsPage !== requestedPage) return;
    renderStatsGames(payload);
  } catch (error) {
    if (generation === state.statsGamesRequestGeneration && $("stats-source-selector").value === sourceIdValue && state.statsPage === requestedPage) showError(error);
  }
}

function renderStatsGames(payload) {
  const items = array(value(payload,"items","games")), total = number(value(payload,"total","game_count"),items.length), offset = number(value(payload,"offset"));
  const body = $("stats-games-body");
  $("stats-game-range").textContent = items.length ? `${fmtInt(offset+1)}–${fmtInt(offset+items.length)} / ${fmtInt(total)} games` : "0 games";
  $("game-page-label").textContent = `Page ${state.statsPage+1}`;
  $("game-prev").disabled = state.statsPage === 0; $("game-next").disabled = offset + items.length >= total;
  if (!items.length) { body.innerHTML = '<tr><td colspan="10" class="empty-cell">No authoritative game_log rows in this scope.</td></tr>'; $("stats-game-json").textContent = "No game-log row selected."; return; }
  body.innerHTML = items.map((game, index) => {
    const fidelity = value(game,"fidelity") || {}, issueCount = objectEntries(fidelity).reduce((sum,[,item]) => sum + (Array.isArray(item) ? item.length : number(item)),0);
    return `<tr data-stats-game-index="${index}"><td>${escapeHTML(fmtDate(value(game,"ts","timestamp")))}</td><td><span class="result ${escapeHTML(value(game,"result") || "draw")}">${escapeHTML(value(game,"result") || "—")}</span></td><td>${escapeHTML(value(game,"terminal_reason") || "—")}</td><td>${escapeHTML(compact(value(game,"p1_deck"),24))}</td><td>${escapeHTML(compact(value(game,"p2_deck"),24))}</td><td>${value(game,"agent_is_p1") === false ? "P2" : "P1"}</td><td>${fmtInt(value(game,"turn_count"))}</td><td>${escapeHTML(value(game,"curriculum_stage") || "fixed")}</td><td>${escapeHTML(compact(value(game,"evaluation_checkpoint_sha256","agent_version"),14))}</td><td class="${issueCount ? "trace-no" : "trace-yes"}">${issueCount ? `${issueCount} issues` : "clean"}</td></tr>`;
  }).join("");
  body.querySelectorAll("tr[data-stats-game-index]").forEach((row) => row.addEventListener("click", () => {
    body.querySelectorAll("tr").forEach((candidate) => candidate.classList.toggle("selected", candidate === row));
    $("stats-game-json").textContent = pretty(items[number(row.dataset.statsGameIndex)]);
  }));
  $("stats-game-json").textContent = pretty(items[0]);
}

function renderHarvests() {
  const target = $("harvest-list");
  if (!state.harvests.length) { target.className = "harvest-grid empty-state"; target.textContent = "No harvest_run, harvest_protocol, or promotion manifests discovered."; return; }
  target.className = "harvest-grid";
  target.innerHTML = state.harvests.map((harvest) => {
    const raw = value(harvest,"manifest","raw") || harvest;
    const kind = value(harvest,"kind") || value(raw,"kind") || "harvest";
    const status = value(raw,"status") || (value(raw,"promote") === true ? "promote" : value(raw,"decision")) || "recorded";
    return `<article class="harvest-card"><p class="eyebrow">${escapeHTML(kind)}</p><h3>${escapeHTML(value(harvest,"label","relative_path","id","path") || "Harvest artifact")}</h3><p>Status: ${escapeHTML(status)}</p><p>Games: ${fmtInt(value(raw,"games"))} · ${escapeHTML(value(raw,"games_per_second") ? `${number(value(raw,"games_per_second")).toFixed(2)} games/s` : "throughput unavailable")}</p><p>Fidelity: ${escapeHTML(value(raw,"fidelity") ? compact(pretty(value(raw,"fidelity")),90) : "not recorded")}</p><details class="raw-drawer"><summary>Manifest JSON</summary><pre>${escapeHTML(pretty(raw))}</pre></details></article>`;
  }).join("");
}

async function selectRun(id) {
  const generation = ++state.runRequestGeneration;
  if (!id) return;
  clearError();
  try {
    const [detail, gamesPayload] = await Promise.all([
      api(`/api/run?run_id=${encodeURIComponent(id)}`),
      api(`/api/evaluation-games?run_id=${encodeURIComponent(id)}`),
    ]);
    if (generation !== state.runRequestGeneration || $("run-selector").value !== id) return;
    state.run = detail; state.evaluationGames = array(value(gamesPayload,"items") || gamesPayload);
    state.selectedGameKey = null;
    state.evaluatorGameKey = null;
    state.actionGameKey = null;
    renderRun();
  } catch (error) {
    if (generation === state.runRequestGeneration && $("run-selector").value === id) showError(error);
  }
}

async function selectStatsSource(id) {
  if (!id) return;
  clearError(); state.statsPage = 0;
  const generation = ++state.statsRequestGeneration;
  ++state.statsGamesRequestGeneration;
  try {
    const bundle = await api(`/api/stats?source_id=${encodeURIComponent(id)}`);
    if (generation !== state.statsRequestGeneration || $("stats-source-selector").value !== id) return;
    state.stats = bundle;
    renderStats(); await loadStatsGames();
  } catch (error) {
    if (generation === state.statsRequestGeneration && $("stats-source-selector").value === id) showError(error);
  }
}

async function loadCatalog() {
  clearError();
  $("health-dot").className = "status-dot"; $("health-label").textContent = "Scanning artifacts…";
  const [overview, runsPayload, sourcesPayload, harvestPayload] = await Promise.all([
    api("/api/overview"), api("/api/runs"), api("/api/stats-sources"), api("/api/harvests"),
  ]);
  state.overview = overview; state.runs = array(value(runsPayload,"items") || runsPayload);
  state.statsSources = array(value(sourcesPayload,"items") || sourcesPayload); state.harvests = array(value(harvestPayload,"items") || harvestPayload);
  renderRunSelector(); renderStatsSourceSelector(); renderOverview(); renderHarvests();
  $("health-dot").className = "status-dot ok";
  $("health-label").textContent = `${fmtInt(state.runs.length)} runs · ${fmtInt(state.statsSources.length)} stats scopes`;
  await selectRun($("run-selector").value);
  const linkedSources = array(value(state.run,"stats_source_ids"));
  const linkedEvaluation = state.statsSources.find((source) =>
    linkedSources.includes(sourceId(source)) && value(source,"kind") === "evaluation");
  if (linkedEvaluation) $("stats-source-selector").value = sourceId(linkedEvaluation);
  await selectStatsSource($("stats-source-selector").value);
}

function bindEvents() {
  $("refresh-button").addEventListener("click", async () => {
    const button = $("refresh-button"); button.disabled = true; button.textContent = "Refreshing…";
    try { await api("/api/refresh", {method:"POST"}); await loadCatalog(); }
    catch (error) { showError(error); }
    finally { button.disabled = false; button.innerHTML = '<span aria-hidden="true">↻</span> Refresh artifacts'; }
  });
  $("run-selector").addEventListener("change", (event) => selectRun(event.target.value));
  $("stats-source-selector").addEventListener("change", (event) => selectStatsSource(event.target.value));
  ["eval-checkpoint-filter","eval-result-filter","eval-terminal-filter","eval-seat-filter","eval-trace-filter"].forEach((id) => $(id).addEventListener("change", applyEvaluationFilters));
  $("eval-search").addEventListener("input", applyEvaluationFilters);
  $("deck-search").addEventListener("input", renderDeckTable); $("card-search").addEventListener("input", renderCardTable);
  $("game-prev").addEventListener("click", () => { if (state.statsPage > 0) { state.statsPage--; loadStatsGames(); } });
  $("game-next").addEventListener("click", () => { state.statsPage++; loadStatsGames(); });
  document.addEventListener("click", async (event) => {
    const actionPageButton = event.target.closest("button[data-action-page]");
    if (actionPageButton && !actionPageButton.disabled) {
      state.actionPage = Math.max(0,number(actionPageButton.dataset.actionPage));
      await selectEvaluationGame(state.selectedGameKey,false);
      return;
    }
    const actionDetails = event.target.closest("details[data-action-index]");
    if (actionDetails && !actionDetails.dataset.loaded) {
      const index = number(actionDetails.dataset.actionIndex,-1);
      const pre = actionDetails.querySelector("pre");
      if (pre) pre.textContent = pretty(state.currentActions[index] ?? null);
      actionDetails.dataset.loaded = "true";
      return;
    }
    const evaluatorPageButton = event.target.closest("button[data-evaluator-page]");
    if (evaluatorPageButton && !evaluatorPageButton.disabled) {
      state.evaluatorPage = Math.max(0,number(evaluatorPageButton.dataset.evaluatorPage));
      await selectEvaluationGame(state.selectedGameKey,false);
      return;
    }
    const evaluatorDetails = event.target.closest("details[data-evaluator-event-index]");
    if (evaluatorDetails && !evaluatorDetails.dataset.loaded) {
      const index = number(evaluatorDetails.dataset.evaluatorEventIndex,-1);
      const item = state.currentEvaluatorEvents[index];
      const pre = evaluatorDetails.querySelector("pre");
      if (pre) pre.textContent = pretty(item ? item.event : null);
      evaluatorDetails.dataset.loaded = "true";
      return;
    }
    const lazyDetails = event.target.closest("details[data-lazy-raw]");
    if (lazyDetails && !lazyDetails.dataset.loaded) {
      const payloads = {
        "full-debug": state.currentFullDebug,
        "evaluator-terminal": state.currentEvaluatorTerminal,
        "trace-replay": state.currentTraceReplay,
      };
      const pre = lazyDetails.querySelector("pre");
      if (pre) pre.textContent = pretty(payloads[lazyDetails.dataset.lazyRaw]);
      lazyDetails.dataset.loaded = "true";
      return;
    }
    const button = event.target.closest(".copy-button"); if (!button) return;
    const target = $(button.dataset.copy); if (!target) return;
    try { await navigator.clipboard.writeText(target.textContent); button.textContent = "Copied"; setTimeout(() => button.textContent = "Copy JSON",1200); }
    catch (_) { button.textContent = "Copy failed"; }
  });
  const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    document.querySelectorAll(".section-nav a").forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${entry.target.id}`));
  }), {rootMargin:"-20% 0px -70%",threshold:0});
  document.querySelectorAll("main > section").forEach((section) => observer.observe(section));
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  try { await loadCatalog(); }
  catch (error) { showError(error); }
});
