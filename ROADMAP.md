# Playersim — Project Plan (overhauled July 2026)

**Mission:** train an AI to play two-player Magic well enough that its games
yield trustworthy per-card and per-deck statistics, which feed a downstream
deck-construction AI searching for the best deck per format. Everything in
this plan is ranked by one question: *does it make the statistics more
trustworthy for the deck-builder?*

Out of scope permanently: multiplayer, Commander, Planechase/planar dice,
and match-play (Bo3 is a possible late add only if target formats demand it).

---

## Definition of done ("complete project")

The project is complete when all of the following hold:

1. **Green gates, always.** Smoke, training, and scenario suites pass on
   every delivery (currently 9/9, 13/13, and 365/365, plus 16/16 fixture-
   harvest tests, 16/16 production-protocol tests, 19/19 card-registry tests,
   1/1 support-preflight tests, 2/2 deck-corpus tests, 13/13 deck-ingest tests,
   6/6 fuzz/replay tests, and the deterministic 8-seed / 8,000-action
   default fuzz profile, and the strict 32-seed / 320,000-action long
   profile).
2. **Zero known stats-corrupting bugs.** The silent-bug catalog (appendix)
   is closed; every fixed bug has a permanent guard scenario.
3. **Quantified card coverage.** For each target format's card pool, the
   card support manifest reports what fraction of the pool simulates
   faithfully; the milestone per format is: every card in the pool is either
   fully supported or explicitly excluded by the deck builder.
4. **Choices are the agent's.** No rules decision that affects card value is
   silently auto-resolved for the agent (trigger order ✅, damage assignment
   order ✅, targeting ▢, modal choice audit ✅, X choice audit ✅).
5. **Trained play beats scripted play** and stats are harvested under
   self-play or league play, not vs. the random opponent.
6. **Calibration passes.** Engine winrates for a small set of known-matchup
   decks fall within tolerance of published/human data — the end-to-end
   sanity check that play quality and rules fidelity are sufficient.
7. **The loop closes.** The deck builder consumes `STATS_SCHEMA.md` data +
   the support manifest, produces candidate decks, and those decks feed back
   into harvest runs without manual file surgery.

---

## Status snapshot (July 2026)

- Tier 0 (stats plumbing): ✅ complete; Round 7.37 production-tested opening
  hands, canonical draw history, real play turns, and terminal-cause telemetry
  across all six training workers.
- Tier 1 (rules correctness): ✅ complete — all seven items plus the P1
  placeholder triage delivered; see appendix for the bug catalog.
- Tier 2 (card coverage): ◐ the support ledger reports no partial, unparsed,
  or crash cards in the audited representative metagame (last promotions:
  M.O.D.O.K. in 7.68, Esper Origins in 7.67, Emeritus of Ideation's Prepared
  transaction in 7.65). Format-wide coverage is manifest-driven: the July 12
  ledger over all 4,702 pinned Standard cards records 86 verified, 73
  observed-clean, 3,327 unseen-clean, 788 partial, and 428 unparsed —
  74.1% static-clean, 3.4% evidence-qualified. The static-clean fraction
  dropped in 7.64 as an honesty correction: 321 previously silent
  unclassified clauses are now reported instead of passing as clean.
- Tier 3 (training/environment): ◐ policy plumbing, audit work, the explicit
  paired-seat scripted qualification gate, the Round 7.72–7.73 reward/critic
  stabilization, and the 7.74 modal-trigger pause lifecycle repair are
  complete; a fresh trained checkpoint still needs to pass qualification
  before Harvest is promoted to policy-vs-policy.
- Tier 4 (verification/calibration): ◐ invariant and long-fuzz gates are green;
  the matchup calibration study remains open.
- Tier 5 (operations/integration): ◐ Harvest orchestration and the fail-closed
  strength-qualification protocol are complete and, since Round 7.46,
  format/corpus-configurable with full run lineage; running a production-scale
  candidate, throughput profiling, and deck-builder integration remain open.
- Target-format program: ◐ milestone 1 (format foundation and lineage) is
  complete — frozen canonical registry + feature schema in
  `formats/standard/`, explicit `--format`/`--decks` configuration, and
  lineage-stamped manifests. User-supplied decks route into isolated format
  pools automatically; passing policy qualification and builder feedback remain
  open.
- Test gates: smoke 9/9, training 13/13, scenarios 368/368 (grown from 12),
  108/108 focused regression tests, 201/201 discovered unit tests,
  fixture harvest 16/16, production Harvest protocol 16/16, card registry
  19/19, deck ingestion 13/13, fuzz/replay configuration 6/6, deterministic
  default fuzz 8 seeds x 1,000 valid actions, and strict long fuzz 32 seeds x
  10,000 valid actions.
- **Stats collected before July 2026 are unusable** (wrong player, wrong
  winner, fictional play turns, cosmetic first strike, compounding P/T,
  dead replacement system). Wipe and re-harvest after the current engine
  is deployed.

---

## Tier 2 — Card coverage, driven by the support manifest

The card support manifest (`Playersim/card_support.py`, July 2026) is the
engine of this tier. Any card whose text the engine cannot faithfully run is
automatically recorded — with the failing clause, a severity, and running
counts — and persisted to `deck_stats/card_support_manifest.json` alongside
the game statistics.

**Severities:** `crash` (handling raised), `unparsed` (whole effect produced
nothing runnable), `partial` (some clauses fell back to no-ops). Worst
severity sticks per card.

**The workflow this enables:**
1. Run harvest games; the manifest fills itself, ranked by real play
   frequency (counts), never alphabetically.
2. Adding support = pick the highest-count entry, write a failing scenario
   for the card's clause, extend the parser (or the per-card override
   registry), watch the entry stop accumulating.
3. The deck builder loads the manifest and **excludes `crash`/`unparsed`
   cards from candidate pools** and down-weights `partial` cards'
   statistics until support lands. This closes the loop Carter asked for:
   unsupported cards can't silently poison deck search.

Remaining Tier 2 work:
- ✅ **Crash-severity wiring**, **per-card override registry**, and the
  **coverage report** (format pool joined against the manifest) all exist.
- ◐ **Parser expansion** (Rounds 1–7.20): a reusable diagnostic harness drove
  ~20 gap-closing rounds and closed ~90 effect/mechanic classes; several dead
  subsystems surfaced along the way (see the appendix bug catalog). New
  support work is now ordered by real manifest counts and format-pool
  coverage, not speculative subsystem ordering.
- ◐ **First-touch coverage sweep**: one scenario for every subsystem that has
  never had one. This practice has repeatedly found phantom methods and dead
  or overfiring subsystems, so untested corners remain suspect. The current
  eight-deck sample has no known high-risk partial; next candidates come from
  manifest counts, format-pool coverage, and the consolidated v1 limitations
  below.

## Tier 3 — Training & environment quality

1. ✅ **Choice exposure audit**: spell, activated-ability, triggered-ability,
   and direct-effect targets are agent choices. Independent modal target
   slots, paged target lists, opponent trigger ordering, multi-target counter
   allocation, generic SacrificeEffect selection, Dig selection, and generic
   activated-ability sacrifice costs are complete. Non-self activation costs
   stage explicit, paginated permanent IDs before the shared cost transaction
   commits.
2. ◐ **Opponent policy**: checkpoint/self-play policies install through
   `set_opponent_policy()`, receive their own observation and legal mask, and
   fall back safely when predicting an illegal action. `harvest_protocol.py
   qualify` measures the candidate from both seats, requires a 55% score by
   default, and fails closed on any fidelity or checkpoint-provenance
   mismatch. Remaining: train a checkpoint that passes that gate, then
   promote Harvest to policy-vs-policy.
3. ✅ **Hidden-information audit**: `observation_for()` enforces a player
   perspective; changing unseen opponent hand identities and library order
   leaves every observation field unchanged. Face-down masking is guarded.
4. ✅ **Replay logs**: seeded resets record actions, contexts, and deck names;
   `export_replay()` writes JSON and `replay()` verifies the selected decks
   before reproducing the episode.
5. ✅ **Deck legality validation**: `Playersim/deck_legality.py` validates
   minimum size, copy/basic-land rules, bans, restrictions, and format
   status; strict deck loading raises validation failures.
6. ▢ **Throughput program** — baseline measured July 14, 2026 on the
   reference trainer (Ryzen 5 5600, 6c/12t, 32 GB, RTX 5060). Effective
   training speed was 10.6 steps/s against a pure-rollout ~40–55 steps/s:
   the default evaluation cadence (every 10k steps × 20 episodes in one
   single-threaded eval env) idled all six workers for **73% of wall
   time**. A single environment simulates at ~17.5 steps/s; profiling
   attributes ~62% of each step to observation building (win-condition/
   threat/synergy analyses recomputed per step — `identify_win_conditions`
   ~24×/step — plus ~37 per-card 436-field feature rebuilds and ~2,500
   regex / ~23,000 `str.lower` calls per step) and the 480-action legality
   mask is regenerated ~9× per step across the scripted-opponent loop.
   The GPU idles (<11%) by design: the learner needs ~10 s per 10+ minute
   rollout, so no GPU-side change buys throughput.
   - **Run configuration (no code):** `--n-envs 8` (worker count is
     RAM-bounded — the Dict observation costs ~0.3 MB per rollout-buffer
     step). With evaluation asynchronous since 7.76, `--eval-freq 25000
     --eval-episodes 10` is a comfortable cadence: the dedicated evaluator
     needs ~5 minutes per 10 episodes, and the callback skips a boundary
     (with a warning) rather than queueing a backlog if the cadence ever
     outruns it.
   - **Long-term goals (code; every item gated on bit-identical
     observations and masks under the scenario + fuzz suites — these are
     caches, not semantic changes, so no schema/lineage impact):**
     1. Cache per-card feature vectors; dirty-flag only the mutable
        P/T/counters slice (`Card.to_feature_vector` is rebuilt ~37× per
        step from scratch).
     2. Memoize strategic-planner analyses (win conditions, threats,
        synergies, combo pieces) per turn or on state change instead of
        per observation.
     5. ✅ (7.76) Periodic evaluation runs concurrently in a dedicated
        process (`AsyncMaskableEvalCallback`): training workers never
        idle behind it, the evaluated snapshot itself is promoted to
        `best_model.zip`, and a worker failure fails the run.
     6. ✅ (7.76) The idle GPU now carries a doubled network (1024-dim
        extractor, 512/256/128 heads, doubled per-key widths) for sample
        efficiency at near-zero wall-time cost.

## Tier 4 — Verification & calibration

1. ✅ Golden scenario harness — 365 scenarios and growing; scenario-first is a
   working agreement, not a suggestion.
2. ✅ **Property/invariant harness**: exact non-token zone/stack conservation,
   SBA fixed points, mask-valid action execution/handler coverage, declared
   observation bounds and degradation checks, observation/info mask agreement,
   repeated mask purity, finite rewards, phase-boundary mana clearing, and
   repeated layer idempotence run under fixed seeds in
   `tests/invariant_fuzz_test.py`.
3. ✅ **Long-game fuzzing**: short (3 x 100), default (8 x 1,000), and long
   (32 x 10,000) profiles exist. Failures are written atomically with the
   seed, exact action/context history, state summary, and a one-command
   `--replay` path; successes leave no artifact directory. Weekly/manual CI
   runs the long profile and retains failure artifacts for 14 days.
4. ▢ **Calibration study**: 3–5 deck pairs with well-known matchup winrates;
   run at harvest scale; compare. This is the acceptance test for the whole
   pipeline and gates "harvest at scale."

## Tier 5 — Harvest operations & deck-builder integration

1. ◐ **Throughput**: isolated parallel workers and aggregate games/second
   telemetry are implemented (trained-checkpoint smoke measured 0.114
   games/second — validates orchestration, not capacity). Profile a
   production-size checkpoint harvest, then optimize measured hot paths.
2. ✅ **Harvest protocol**: `harvest_fixtures.py` supplies strict
   deterministic shards and `harvest_protocol.py` supplies parallel
   operation, checkpoint loading/identity, aggregate success-only manifests,
   paired-seat scripted qualification, candidate scoring, and checkpoint
   promotion gates — complete and regression-tested. Operational next step:
   train a longer strength candidate, pass scripted qualification, freeze a
   baseline checkpoint, then run the first paired-seat promotion and
   calibration study.
3. ▢ **Deck-builder contract**: `STATS_SCHEMA.md` + support manifest are the
   full interface; the builder's exclusion logic and confidence weighting
   consume them directly.
4. ▢ **Feedback loop**: builder-proposed decks auto-enter the harvest queue;
   their novel cards populate the manifest; support work is prioritized by
   what the builder actually wants to play.

### Target-format program — Standard, Modern, Pioneer

The narrowed constructed scope is **Standard first, Modern second, and Pioneer
third**. A "format agent" means one format-specialist policy and checkpoint
league trained across a representative corpus of decks for that format, not a
policy tied to one deck. Each format's deck corpus, checkpoints, statistics,
support observations, and promotion/calibration artifacts live in an isolated
format namespace; statistics from different formats must never be merged
merely because a card or deck name appears in both.

All three format pipelines share one versioned canonical-card registry
(stable Oracle identity rather than run-local integer IDs), one frozen
observation/action feature schema, and one format-parameterized deck builder.
The registry, format card-pool snapshot, deck-corpus snapshot, and feature
schema each carry a recorded version/hash so adding a builder candidate cannot
silently change model input width or invalidate a checkpoint.

Phased milestones:

1. ✅ **Format foundation and lineage** (Round 7.46): training, fixture
   Harvest, and the parallel Harvest/promotion protocol all accept explicit
   `--format`, `--decks`, and `--format-dir` configuration with strict
   legality; `Playersim/card_registry.py` provides the canonical registry
   (stable, append-only integer indices keyed by name + Scryfall oracle_id)
   and the frozen, versioned feature schema, both self-hashed and created by
   `python -m Playersim.card_registry freeze`; every run-level manifest
   stamps a `lineage` object (format, pool-snapshot hash, corpus hash,
   registry/schema version+hash) alongside git/policy/checkpoint identities.
   See `STATS_SCHEMA.md` "Format namespaces and run lineage" for the consumer
   contract. `formats/standard/` covers all 4,702 legal English cards in the
   pinned Standard snapshot plus 28 retained bootstrap identities; the v2
   feature schema has 259 subtypes and feature_dim 436 with the original 110
   indices unchanged.
2. ◐ **Standard end to end**: the representative corpus is pinned, hydrated,
   and extensible with validated imports. Continue closing impact-ranked
   support gaps, qualify the Standard policy against scripted play, promote
   it into a checkpoint league, calibrate known matchups, and produce the
   first format-isolated, fidelity-clean strength harvest.
3. ▢ **Modern end to end**: assemble a separate strictly legal Modern corpus,
   triage its observed support gaps, then repeat qualification, league
   promotion, calibration, and production harvest in the Modern namespace.
4. ▢ **Pioneer end to end**: same gates in the Pioneer namespace.
5. ▢ **Unified builder feedback**: one builder accepts a format as explicit
   input and consumes only that format's legal pool, version-matched support
   ledger, fidelity-clean qualified-policy statistics, matchup data, and
   uncertainty. Builder candidates enter the affected format's support
   preflight and paired-seat evaluation queue without contaminating held-out
   promotion or calibration results.

**Current execution order:** launch a fresh Round 7.75 Standard candidate
(`reward-v3` was stopped at ~130k steps by the 7.75 observation-overflow
find, with the critic healthy: explained variance 0.60 and rising) using
the Tier 3 throughput-program run flags (`--eval-freq 25000
--eval-episodes 10 --n-envs 8`; evaluation is asynchronous since 7.76).
Inspect it for nonzero
`reward/state_change_nonzero`, bounded return/value scales, a critic
explained-variance trend that is not persistently negative, and — the open
question at the 300k-step checkpoint — a `terminal/life_total_rate` share
that grows at the expense of `terminal/turn_limit_rate`; if the terminal
mix is still ~90% turn-limit with flat negative episode reward there,
reweight the state potential's damage-progress component before burning
the rest of the budget. Schedule the throughput-program code items as
their own verified rounds, never bundled with a live experiment. Continue the impact-ranked Standard support sweep while
that run proceeds, then qualify the candidate against scripted play, promote it
into a checkpoint league, and calibrate known matchups. Imported lists can
widen the working pool without overwriting the pinned metagame; builder-driven
queueing is a later milestone. Reuse the qualified pipeline for Modern, then
Pioneer, then enable the unified builder feedback loop. Throughput profiling
and calibration are per-format gates, and new fidelity failures pre-empt
strength and integration work in the affected format.

Still open on this path: the scripted opponent remains the training baseline,
no representative Modern or Pioneer training corpus exists, and builder
candidates do not yet enqueue themselves. A clean failure manifest does not
prove an unseen format card simulated faithfully; coverage distinguishes
unseen, observed-clean, verified, partial, and excluded cards.

---

## Delivered round history (condensed)

Full per-round narratives were compressed July 2026; the durable contracts,
checkpoint boundaries, and institutional lessons are retained below and in
the limitations/appendix sections. Per-round gate tallies are dropped — the
status snapshot holds the current gate set.

**7.23–7.36 — policy boundary, fuzz harness, CUDA pipeline.** Closed the
Tier 3 choice-audit items; delivered `harvest_fixtures.py`, the invariant
fuzz harness, and the parallel Harvest/promotion protocol; audited the full
policy boundary (observation bounds, mask-valid dispatch, seat alternation,
no scripted fallback after an illegal checkpoint prediction). A
dead-subsystem sweep fixed attack-watcher scoping, phase-trigger ownership,
dead ETB/end-step registration aliases, Impending, and a `strategy_memory.pkl`
reproducibility leak. Hardened CUDA training (provenance manifests, spawn
workers, atomic publish + reload validation). Moved to cu130 wheels and
SubprocVecEnv rollouts.

**7.37 — reward rebuilt.** Change in bounded strategic potential plus one
perspective-correct terminal reward; the scripted opponent made to actually
play lands/spells/combat; Tier 0 telemetry repaired. **Do not resume any
pre-7.37 checkpoint** (reward and baseline changed materially).

**7.38–7.45 — games that finish.** Each strength run converted a fidelity
failure into a fix (Bushwhack modal targets, Pawpatch recursion, Duress
deferred-cast phase, Floodpits mask). 7.43 fixed four systemic reasons games
never concluded (cast-time targeting over-blocked, scripted opponent could
not tap mana, tap-then-cast dance, dead "exile instead" riders) and raised
`max_turns` 20 → 30. **7.44 changed the declared Gym observation space —
checkpoints from before 7.44 must not be resumed.** 7.45 made Spree a real
casting transaction.

**7.46–7.50 — format foundation.** Canonical card registry + frozen
self-hashed feature schema + `freeze` CLI + run-level lineage on every
manifest (7.46); Roles, day/night, clone-safe delayed triggers, snow payment
(7.47–7.48); the registry widened to the full 4,702-card Standard snapshot,
feature_dim 436, without renumbering (7.49); the first full-format support
preflight and the schema-versioned, self-hashed
`formats/standard/support_ledger.json` separating verified / corpus-clean /
unseen-clean / partial / unparsed evidence (7.50). Format-namespace runs are
a new stats lineage — do not mix with pre-7.46 artifacts.

**7.51–7.54 — the representative corpus runs clean.** Deterministic corpus
hydration into `formats/standard/decks/` with Standard as the strict default
(7.51); format-aware deck ingress (`python -m Playersim.deck_ingest`,
automatic format routing, fail-closed legality, separated metagame/imported
recursive pools) (7.52); the corpus's last severe entries closed (7.53); and
one shared-primitives release closing every remaining representative partial
— Warp end to end, linked choice transactions, Mistrise/finality/Esper
zone rules, and Crew/Vehicle animation (7.54). Exact-card support for
Earthbend, Flashback, Harmonize, and ~20 named corpus cards landed across
these rounds with scenario guards.

**7.55–7.57 — generic mechanic families.** Full-pool generic support for
Equip, Crew, Discover, Connive, Suspect, Explore, Investigate, Endure, and
Airbend, then their dynamic-count variants; optional resolution payments
("you may pay {cost}; if you do") through the ordinary choice policy; X
affordability paginated beyond ten with resource-derived ranges staged
identically for spells and activated abilities. ~200 cards moved to clean
with zero clean-card regressions; complex mixed clauses stay honestly
partial rather than being claimed by broad keyword matches.

**7.58–7.60 — Priority 0 closed.** Controller-safe control transfer with
live effect rebinding and last-known death attribution; specific
`remove_ability` dependencies, CR 305.7 basic-land-type setting, and
Blood Moon/Urborg ordering; dynamic layer applicability from live
characteristics; **true per-copy runtime card identity** (registry IDs
remain the stats namespace; every physical copy is a distinct runtime ID
and mutable Card object); clone-safe delayed execution; announcement-time
counter divisions; snow provenance with fail-closed unknown conditions;
Mutate/Meld ownership, blink, and branch isolation.

**7.61–7.62 — Priority 1 closed.** The paginated action-479 overflow catalog
(hand, graveyard permissions, Class/leveler, extra activated abilities, all
revalidated through ordinary handlers); explicit Ward pay-or-decline;
per-slot copy retargeting; simultaneous hidden each-player discards;
structured multi-color mana output; staged sacrifice/discard cost
selections with a shared characteristic predicate; resource-derived X
ranges. **7.62 widened the declared X/count observation bounds — a new
policy lineage; do not resume older checkpoints.**

**7.63 — production-readiness integrity sweep.** Unified
hybrid/snow/Phyrexian/conditional mana affordability–payment–refund
accounting; mask-to-execution target parity; choice/spell lifecycle
integrity; exact frozen schema in Windows-spawn workers; finite numerics
throughout; plus the first clean six-worker spawn training canary with
save/reload validation.

**7.64–7.67 — evidence honesty and the strength gate.** Unclassified
permanent clauses now advance fidelity counters and the ledger instead of
passing silently (the static-clean fraction dropped accordingly — an
intended correction); nine warning-path cards promoted through focused
execution tests (7.64). Prepared implemented end to end; verified rule
declarations stopped registering dead layer abilities; quiet workers stopped
duplicating debug files (7.65). Targets became strict transactions —
mandatory empty payloads fail unless resolution validation proves a
committed target became illegal; cast-trigger event targets are deep-copied
and separated from the trigger's own targets; face-down exile is hidden
across observation, inference, and targeting (7.66). Esper Origins verified
and the paired-seat scripted qualification gate went live
(`harvest_protocol.py qualify`: both seats, draws half a point, 55% default
threshold, zero fidelity counters, fail-closed provenance) — and correctly
rejected the first real checkpoint on strength, not pipeline health (7.67).

**7.68–7.70 — training-failure triage rounds.** Each converted a live
training failure and its warning set into engine fixes:

- **7.68** — nested trigger choices stay transactional (Cosmogrand modal
  trigger inside `order_triggers`); orphan `CHOOSE` wrappers can no longer
  trap a policy in NO_OP; M.O.D.O.K. runtime-verified; strict-cycle
  diagnostics carry recent actions and stack provenance. Verified by an
  exact-seed six-worker canary past the original failure point.
- **7.69** — Crew legality is one shared mask/handler predicate (CR
  702.121c guard); keyword abilities carry integer activation indices;
  optional airbend selections resolve as zero-minimum; `threat_assessment`
  saturates at its declared bound.
- **7.70** — converted the 01:25 parallel-run error/warning set: the
  declare-blockers reward-farming loop closed (redundant identical
  multi-block re-assignment fails and is masked out — it was a mask-valid
  +0.1 no-op the policy repeated until the 2000-step strict limit killed
  training); "when you cast this spell" triggers scoped to the actual
  self-cast and made functional from the stack (they fired for every spell
  and never for the spell itself); Room doors parse per-face costs and can
  no longer unlock free; Earthbend activations open their target choice at
  stack time; unbounded observation quantities (P/T, mana, life) are
  declared saturation points that clip silently — structural features keep
  the hard bound check; unqualified multi-target damage maps to any-target;
  DRAW replacement-loop diagnostics name the pending effects; dangling
  "put it into your hand" splitter fragments are suppressed. Open follow-up:
  permanent guard scenarios for the multi-block no-op, self-cast trigger
  scope, and Room door costs.

**7.71–7.72 — failed-run lifecycle and learning-signal repair.** The July 13
failure logs were converted into engine and training-contract fixes:

- **7.71** — the final action-479 target page is one-way and cannot wrap back
  into a stale selection transaction; copied Brightglass Gearhulk and North
  Wind Avatar text now reaches the same semantic effect paths as the original
  spells. The focused target-lifecycle suite is green at 13/13.
- **7.72** — repaired the disconnected `reward/state_change` signal. The
  environment now has one bounded state potential and pays discounted shaping
  `0.25 * (gamma * Phi(next) - Phi(current))`, with the next potential zeroed
  at terminal states. Direct handler/action heuristics are scaled to 10% so
  procedural actions cannot dominate game results. The PPO baseline is now
  learning rate `1e-4`, batch size `512`, three update epochs, value clipping
  `0.2`, and target KL `0.02`; TensorBoard records absolute/nonzero reward
  components plus rollout return/value/advantage scales and pre-update critic
  explained variance. Training smoke is 13/13 and rules scenarios are 365/365.
  **This is a reward-contract boundary: do not resume pre-7.72 checkpoints.**

**7.73 — timeout stalling and critic-input repair.** The first 7.72 run
(`reward-v1`, stopped at ~172k/1M steps) diagnosed two coupled failures.
First, 88% of rollout episodes ended at the turn limit and only 3.6% by
life total: a life-lead timeout paid +5 (half a real win), so accumulating
per-action shaping while coasting to the limit beat trying to close games.
The terminal contract is now `discounted-state-potential-v2`: real results
stay ±10, but timeouts pay win +2 / draw −4 / loss −8, and
`action_reward_scale` drops 0.10 → 0.02 (~0.24/episode, tie-break only).
Second, the critic diverged (value loss 1e7, |V| to 1.4e5 against true
returns of ~±20) because saturation-bounded observation scalars (P/T and
combat damage to 1e6, raw card ids to 2^31) fed `Linear` layers unsquashed;
one degenerate game contaminated the GAE targets of its whole batch. The
extractor now applies stateless symlog `sign(x)·log1p(|x|)` to every
continuous input, keeping magnitudes ≤ ~22 with no VecNormalize state to
save or sync. **This is a reward-contract and extractor boundary: do not
resume pre-7.73 checkpoints.**

**7.74 — modal-trigger pause lifecycle.** The first 7.73 run (`reward-v2`)
died at ~52k steps to the strict non-progress guard: a `trigger_mode` choice
(Cosmogrand Zenith, scripted seat) was stranded with the phase back in
PRIORITY, unanswerable by any routing. Root cause was a three-part
choice-lifecycle hole in trigger stacking: the non-interactive batch loop
kept stacking triggers after one of them opened the mode pause (burying it
and recursing into the NAP batch), `start_pending_stack_target_choice`
stamped PHASE_TARGETING over PHASE_CHOOSE without saving it as a resume
anchor, and `_push_trigger_to_stack` overwrote any pending `choice_context`
unconditionally. Fixes: batch stacking now stops at a mode pause and parks
the remainder (rest of batch + NAP batch) on the same
`parent_order_triggers` continuation the nested CR 603.3b flow uses; the
deferred target opener refuses to run while any `choice_context` is
pending (every choice-completion path calls back in); and a modal trigger
facing a foreign pending choice stacks unmoded with a loud warning instead
of clobbering it. Guard scenario added (verified to reproduce the deadlock
pre-fix); scenarios 366/366, smoke 9/9, training smoke 13/13, target
lifecycle + stack integrity 14/14. Engine-only fix — no reward-contract or
checkpoint boundary; the 7.73 boundary still applies.

**7.75 — observation overflow and category-key target fizzle.** The 7.74
run (`reward-v3`, user-stopped at ~130k steps with a healthy critic:
explained variance 0.60 and rising) surfaced two independent bugs.
First, a doubling combo drove total creature power past C-long range and
`np.array(value, dtype=int32)` raised OverflowError *before* the declared
saturation clip could run — aborting the whole creature-stats population
block and silently degrading unrelated observation features every step of
that game. Unbounded game integers (life, total P/T, advantages, mana) are
now clamped to their declared bounds in pure Python before ndarray
construction (`_bounded_int_array`), which is the documented saturating
behavior. Second, the twice-seen Prismari Charm fizzle: the committed
target map files each target under exactly one category key, and a
battlefield Summon (enchantment creature saga) arrived keyed
`enchantments`; any-target damage read only its own keys, dropped the
legal target, and falsely fizzled. `DamageEffect` now treats the key as a
routing hint, not the legality authority — a committed target whose actual
printed identity satisfies the requirement is accepted regardless of key
placement. Both guard scenarios verified to reproduce their failures
pre-fix; scenarios 368/368, smoke 9/9, training smoke 13/13, focused
damage/targeting suites 22/22. Engine-only fixes — the 7.73 checkpoint
boundary still applies.

**7.76 — asynchronous evaluation and network widening (Tier 3 throughput
items 5–6).** Periodic evaluation moved out of the training loop into one
long-lived spawned process: `AsyncMaskableEvalCallback` saves a policy
snapshot at each evaluation boundary, a dedicated worker (own strict eval
env, CPU device, mask-aware episodes) scores it, and results fold into
TensorBoard on arrival with `eval/evaluated_at_timesteps` preserving the
snapshot's true step. The evaluated checkpoint itself is promoted to
`best_model.zip`; a worker failure fails the run; a backlogged cadence
skips boundaries with a warning instead of queueing stale snapshots; the
final-validation env is now built lazily in the main process only when a
real checkpoint exists. Separately, the policy network doubled across the
pipeline (extractor 512→1024, per-key sub-networks doubled, merger
256→512, default heads medium→large 512/256/128) to spend the idle GPU on
sample efficiency. Verified end-to-end with a real-corpus mini-run (async
evals completed mid-training, best model promoted, clean shutdown).
Training smoke 13/13 (eval-callback stage rewritten for the async
contract). **Checkpoint boundary: the width change starts a new lineage —
do not resume pre-7.76 checkpoints.**

---

## Working agreements

- **Scenario-first**: failing scenario before implementation, every slice.
- **Turn-start drift check**: diff workspace vs `/mnt/project/` before work;
  classify any drift before building on it.
- **Delivery**: one zip per turn containing every file differing from the
  project — complete drop-in files, `Playersim/` and `tests/` paths.
- **Three-suite gate** before every delivery; roadmap updated with every
  slice, including v1 limitations, honestly stated.

## Known v1 limitations (consolidated)

The order below is the canonical implementation priority. Severity is based on
the risk of corrupting rules outcomes or training statistics first, then
missing player decisions, bounded card/mechanic coverage, and operational
constraints. (Items 1–12 were the Priority 0/1 lists, retired in Rounds
7.60/7.62; numbering of the surviving items is preserved for reference.)

### Priority 0 — simulation integrity

✅ No known Priority 0 simulation-integrity limitations remain (closed in
Round 7.60). Missing Meld or Specialize family data and bounded
condition/mechanic vocabularies are honestly classified as Priority 2
coverage; they fail closed rather than silently diverge.

### Priority 1 — decision and action completeness

✅ No known Priority 1 decision/action-completeness limitations remain
(closed in Round 7.62). Structured sacrifice and X-cost parsers remain
intentionally bounded: unknown Oracle families fail closed and belong to
Priority 2 card-fidelity coverage, not hidden policy decisions. One residual
scripted path: blocker-side damage ordering still uses the automatic order
pending the next choice audit (attacker-side assignment order is exposed).

### Priority 2 — bounded mechanic and card fidelity

13. Generic `as enters` transactions are verified for creature type, color,
    card type, basic land type, opponent, counters, chained life payment, and
    deferred ETB events; arbitrary effects consuming those chosen values
    remain card-specific (Cavern of Souls keeps its dedicated mana consumer).
14. Emblem execution is implemented only for the recognized Kaito anthem and
    Wrenn graveyard-permission texts; other emblem text is retained as a
    command-zone record but needs an effect implementation.
15. MDFC support still lacks direct nonland back-face entry and complete
    back-face targeting text (back-face casting itself works: per-face
    cost/text/type accessors, and `cast_spell` honors `cast_as_back_face`).
    Prepare layouts are classified separately and verified.
16. Adventure-half parsing and targeting remain heuristic (the exile-then-
    cast-creature-side lifecycle itself is implemented).
17. Generic Discover, Explore, Investigate, Endure, Connive, Suspect,
    Airbend, Equip, Crew, and the effects printed on Prepare spell faces
    intentionally cover bounded text families; dynamic count expressions and
    compound clauses outside them stay conservatively partial.
18. Reflexive triggers recognize a bounded set of exact "When you do" /
    "When that player does" rider templates.
19. Earthbend has bounded dynamic-X parsing (fixed values plus
    last-known-power X) and resolves its choice-free delayed return
    immediately after the initial zone move rather than using the stack.
20. Uncommon linked optional-search templates still need exact transaction
    handlers (the controller-preserving tapped-land-search rider works).
21. Warp source-duration permissions ("for as long as you control") remain
    conservatively partial; the cast / next-end-step exile / later
    exile-cast transaction is implemented.
22. Target-conditioned pricing recognizes only the implemented condition
    vocabulary (currently: tapped permanent; permanent you control).
23. Numeric die support covers ordinary result tables and emits roll events;
    modifiers, rerolls, ignored rolls, and tableless "equal to the result"
    clauses remain open. The planar die is out of scope with Planechase.
24. Attack triggers fire at declare-attackers-done through one ATTACKS
    dispatch, scoped by controller and printed type; adjectives outside the
    type/subtype/supertype vocabulary remain conservative, and defender-side
    gating assumes two-player "attacks you".
25. Rare phase-beginning scopes ("each player's upkeep on their turn",
    named-player phases) can pass ungated; the common your/opponent's
    upkeep/end-step/draw/main wordings are gated.
26. Screaming Nemesis relies on standard life-gain entry points (gain_life /
    lifelink) and a single committed reflected-damage target; effects adding
    life outside those entry points bypass it.
27. ConditionalExileEffect (Anoint) is single-target and reads the corrupted
    threshold from the target controller's poison counters at resolution.
28. Obliterator uses a conservative payer fallback (opponent of the
    trigger's controller) when the damage source has left play.
29. Cavern of Souls offers the top-10 creature subtypes derived from the
    controller's own cards; the uncounterable rider applies when its
    restricted mana was spent — counterspells may still target and fizzle.
30. Obstinate Baloth identifies an opponent-caused discard from the causing
    stack source; an undeterminable cause conservatively keeps the graveyard
    destination.
31. Meld requires the meld-result printing in the local card database;
    missing `all_parts` data fails closed instead of fetching at runtime.
32. Specialize requires all five local variant printings; incomplete
    families are fidelity-marked unparsed and excluded from supported play.
33. Trigger-condition, structured-sacrifice, and activation-cost parsing
    fail closed on Oracle templates outside their documented vocabularies;
    unsupported families are bounded coverage work, not silent divergence.
34. Mutate still lacks per-component replacement choices and library-order
    choice; commander-specific routing is outside the current formats.
35. Face-down exile is hidden across observation, inference, and targeting,
    with per-owner counts for repeated hidden IDs. A legacy state where a
    visible and a hidden exile occurrence share one numeric ID hides both;
    production runtime objects have unique IDs.

### Priority 3 — operational and lineage constraints

36. Strategy memory is per environment (under the env's storage directory);
    its optional 20% "enhancement" pass makes saved memory content
    nondeterministic (game RNG unaffected).
37. Format registries and schemas are lineage-bound, name + oracle_id based,
    and best-of-one only. Canonical indices are name-sorted at first freeze
    and differ from legacy insertion-order IDs; `freeze-pool` grows the
    registry append-only; `freeze --extend` accepts only width-preserving
    additions, and a subtype vocabulary that must grow requires a new schema
    version and therefore a new policy lineage. Lineage lives in run-level
    manifests, not per-game `game_log.jsonl` lines. Per-printing
    (set/collector) distinctions are deliberately out of scope. The deck
    importer can bootstrap a wholly missing format namespace from its pinned
    snapshot but refuses a half-present registry/schema pair; sideboards are
    retained and legality-checked but not played by the Bo1 runtime.
    Checkpoint-resume boundaries: pre-7.37 (reward rebuild), pre-7.44
    (observation space), pre-7.62 (X/count bounds), pre-7.72 (discounted
    state-potential reward contract and critic baseline), pre-7.73
    (timeout terminal rebalance and symlog extractor inputs), and
    pre-7.76 (network width) checkpoints must not be resumed; pre-7.46
    stats artifacts must not be mixed with format-namespace lineages.
38. Treasure/Beza support is scenario-verified through registration,
    activation costs, color choice, mana production, and the CR 605
    no-stack path; the retained note documents the supported path, not an
    active gap.

Additional standing implementation notes:

- The fixed 480-action tensor keeps compact fast-path slots; the action-479
  catalog exposes overflow contexts, each revalidated through its ordinary
  handler. Indices 205–223 hold dormant labels for unimplemented mechanics
  and are deliberately never mask-valid — bounded fidelity coverage, not
  omitted legal choices.
- Canonical registry IDs are printing/statistics identities; runtime play
  telemetry canonicalizes distinct runtime IDs back to the registry ID at
  the persistence boundary.
- Text-derived replacement registration is idempotent per card per game and
  cleared by `remove_effects_by_source` for phasing rebuilds; controller-
  bound static and replacement effects are rebuilt on control changes and
  their end-of-turn reversion.

---

## Appendix — the silent-bug catalog (institutional knowledge)

Every one of these shipped silently and was found by a first-ever scenario.
The pattern to internalize: **untested subsystem ⇒ assume broken.**

Phantom methods (calls to functions that never existed anywhere):
`_extract_condition_clause` (trigger conditions), `gs._build_type_line`
(crashed every layer-1 copy), `_is_effect_expired` (crashed the entire
replacement system when any effect registered), plus a Card-object `.get()`
in replacement ordering.

Dead-on-arrival subsystems: text-parsed triggered abilities (optional regex
separator mangled every condition — no parsed trigger ever fired);
replacement effects (two latent crashes, exceptions swallowed); mana
doubling (listened for an event nothing fired); dies-copy tokens (set a flag
nothing read); phasing (permanents oscillated out of existence, lost their
abilities permanently, force-untapped); attack triggers
(handle_attack_triggers had no callers and attackers_this_turn was never
written — Boast could never activate and the agent's attacked-this-turn
observations were always zero); dealt-damage triggers ("is dealt damage"
matched no event class — every enrage-style trigger was dead).

Stats-corrupting, found in Round 7.19: resolving a spell executed its printed
activated-ability lines (Herd Migration discarded and gained 3 life on cast);
token subtypes outside the loaded pool's feature vocabulary were dropped
(tokens missed tribal/anthem interactions); "any target" spells crashed
target selection when players and permanents shared the valid set
(int-vs-str sort).

Found in Round 7.20: generic activated abilities stacked with an empty
context resolved to nothing (every ability_handler.activate_ability call was
a no-op — Boast was dead twice over); generic creature tokens had no card
type unless the parsed name contained the literal word "creature", and
colored tokens were always colorless (Card reads "color_identity" letters,
not the "colors" vector); cast_spell's context omitted the card, making all
conditional "spend only" mana unusable for casts; the targeting parser
captured state adjectives as the target type ("target attacking creature"
had zero legal targets).

Stats-corrupting: layer base fed back on itself (+1/+1 compounded every
phase); layer write-back leaked across games via shared card_db objects;
first strike was cosmetic (no SBA between damage steps); play turns
fabricated from CMC; winner/loser card attribution swapped on p2 wins; all
plays credited to p2 (dict-vs-index comparison); cost reductions applied
before increases (601.2f inverted); SBAs never applied to cards
(isinstance-str vs int ids — creatures never died); spell resolution crashed
at target validation and deleted the card; layer engine skipped computation
with no effects registered; stdlib `copy` shadowed by numpy; prevention
'creature' class shielded players.

Found in Round 7.70: every Room door unlocked for free (parsing read
`card_faces` where the attribute is `faces`, and the `door1`/`door2` dicts
the unlock handler consumes were initialized empty and never populated —
"assuming free?" was load-bearing); "when you cast this spell" triggers fired
for every spell the controller cast but never for the spell itself (the
battlefield zone-match short-circuited the self-cast check, and the trigger
scan never looked at the stack); redundant multi-block re-assignment was a
mask-valid +0.1-reward no-op the policy farmed until the step limit killed
training. (Guard scenarios for these three are the open Round 7.70
follow-up.)

Each earlier entry has a permanent guard scenario. Keep writing them first.
