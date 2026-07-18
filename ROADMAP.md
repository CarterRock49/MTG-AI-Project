# Playersim roadmap — current as of July 17, 2026

## Mission and scope

Train a strong two-player Magic policy whose games produce trustworthy
per-card, per-deck, and matchup statistics for a downstream deck-construction
AI. Work is prioritized by one question: **will this improve the reliability
of the builder's decisions?**

Target formats are Standard first, Modern second, and Pioneer third. Each
format gets an isolated card pool, deck corpus, support ledger, policy lineage,
checkpoint league, and statistics namespace.

Permanently out of scope: multiplayer, Commander, Planechase/planar dice, and
per-printing set/collector distinctions. The runtime is best-of-one; best-of-
three is a possible later addition only if a target format requires it.

Status legend: ✅ complete, ◐ active/partially complete, ▢ not started.

---

## Executive status

The engine, policy boundary, replay/fuzz infrastructure, format lineage, and
Harvest orchestration are operational. The representative Standard corpus does
not currently carry a severe support-ledger entry, but that is not a clean-
runtime guarantee; the new strict recovery counters must remain zero in a fresh
run. The project is **not production-ready** because
no trained checkpoint has passed the paired-seat strength gate, matchup
calibration has not run, format-wide card support remains incomplete, and the
deck-builder feedback loop is not connected.

### Round 7.92 verified delivery baseline

The corrected Observation v3 contract and the Round 7.92 ratchet, fidelity,
payment, canary, and qualification changes are green at the following current
working-tree counts:

| Gate | Result |
| --- | --- |
| Golden scenarios | 404/404 |
| Runtime smoke | 9/9 |
| Training smoke | 13/13 |
| Discovered unit tests | 430/430 |
| Default invariant fuzz | 8/8 seeds × 1,000 valid actions, plus phase-boundary check |
| Observation schema | v3 / `6e29a94e3443881681afd794185f061133f24ff72350a7df27f48524f00d4137` |

Standing broader gates last recorded green: fixture Harvest 18/18, production
Harvest protocol 17/17, card registry 19/19, deck ingestion 13/13, and
fuzz/replay configuration 6/6. The strict long-fuzz result (32 seeds × 10,000
valid actions) is historical until that scheduled/manual gate is rerun.

The final pre-run audit aligned target discovery, action masks, selection, and
resolution around the exact active instruction; unknown target grammar fails
closed. Card and deck analytics now preserve canonical card identity,
player-relative turns, draw-aware rates, and atomic per-file persistence.
Evaluator caches retain only static characteristics, while live state and
perspective are recomputed for each decision.

### Non-negotiable lineage rules

- **Start every new policy from the Round 7.89 Observation v3 boundary, using
  the current Round 7.95 reward/curriculum contract.** Observation v3 corrects learned
  mana, land-development, resource-advantage, and strategic-viability
  semantics. Adaptive card/deck history is recorded but excluded from live
  evaluator advice by default so worker-local histories cannot make the same
  public state nonstationary. Recorded archetypes are canonical and play-turn
  analytics are player-relative; targetable observations match the active
  target instruction. Do not resume an Observation v2 checkpoint into this
  lineage. Fresh Round 7.95 training uses
  `tempo-graded-potential-v1` and `combat-v7`; do not resume an older
  curriculum or reward checkpoint.
- Resume now verifies the companion manifest's reward contract and Observation
  version/hash. Curriculum continuation is intentionally rejected until its
  per-worker scheduler counters can be checkpointed; launch a fresh
  current-round run.
- Do not mix pre-7.46 statistics with format-namespace statistics.
- Statistics collected before July 2026 are unusable. They were affected by
  perspective, winner attribution, fabricated play-turn, first-strike, layer,
  and replacement-system defects and must be re-harvested.
- Any future policy-observation change creates another explicit schema and
  checkpoint boundary.

---

## Round 7.91 — climbable curriculum ramps and reward contract v6

Analysis of `round-7.90-memory-redundancy-v1` (stopped at ~188k steps)
confirmed the 7.89/7.90 canary behaved exactly as designed — goldfish mastered
at 47,488 in both runs, race fell to its deadline at 147,488 in both, and the
100k evaluations matched (9-44, qualification 0.141) — so the memory-redundancy
refactor is behavior-neutral. The agent, however, showed the same plateau as
Round 7.88's full million-step run:

- ~60% decisive wins against passive opponents collapsing to ~5% against
  novice, flat from 57k to 188k timesteps. There was no gradient between the
  two difficulty levels to climb.
- Decisive wins averaged turn 25 while losses averaged turn 17: the policy's
  clock was slower than every active opponent's, and ~250-step episodes meant
  only ~750 games in the whole run — starvation-level sample counts.
- The critic's explained variance collapsed (0.84 → 0.11) at each stage
  transition, and 6 of 15 evaluation "wins" were life-lead timeouts scored
  exactly like losses.

Round 7.91 lands four repairs (opponent-profile observation features were
deliberately rejected to keep the policy deck- and opponent-agnostic):

1. **`combat-v4` annealed handicap.** Race and bridge open with weakened
   active profiles: with probability epsilon the opponent takes the passive
   baseline for one priority decision. The trainer ratchets epsilon toward
   zero each time a rolling window of decisive wins at the current epsilon
   clears the stage target (race: novice 0.75 start, 0.25 step; bridge:
   scripted 0.60 start, 0.20 step). Mastery requires the anneal to finish and
   the profile floors are now satisfied only by full-strength episodes;
   `opponent_handicap` is recorded in every game log and mastery record.
2. **Stage turn limits.** Goldfish/race play to 20 turns and bridge to 25
   (full pool keeps the engine default), buying more terminal outcomes per
   timestep without moving the observation-space bound; the per-episode limit
   is recorded as `max_turns` in game logs.
3. **Reward contract v6** (`discounted-state-potential-v6`): a life lead at
   the turn limit now pays -8 instead of -10 (all other limit outcomes stay
   -10), so "almost won" is no longer worth exactly as much as losing while
   remaining worse than a decisive win (a decisive loss still pays -10). The convex
   damage-progress potential weight doubles (0.40 → 0.80). Resume remains
   version-gated, so v5 checkpoints cannot enter this lineage.
4. **Paired-seat observation audit.** The 7.90 evaluation's 5-27 (P1) versus
   10-20-2 (P2) split motivated a standing regression
   (`tests/seat_parity_test.py`) covering the audited `my_*`/`opp_*` fields on
   paired constructed states. It passes, so that tested extractor slice did
   not reproduce the split; it is not proof that every runtime seat asymmetry
   has been eliminated.

`combat-v4` was the Round 7.91 default and `combat-v3` remains resolvable for
reproducibility. Round 7.92 supersedes both for fresh runs with `combat-v5`.
Fixed evaluation continues to use full-strength scripted opponents and the
engine-default turn limit.

### Run 1 result (`round-7.91-annealed-ramp-v1`) and the choice-phase fix

The first v4 run validated the ramp design and died on a pre-existing engine
defect at ~223k steps. Goldfish fell to its 75k deadline (the 20-turn limit
converted the fresh policy's slow kills into ~71% timeouts — a stage-tuning
miss to revisit), but race then ratcheted 0.75 → 0 on demonstrated win rate
and **advanced via mastery at 169,332** — the first earned transition past
goldfish in any round, with ≥20% decisive wins against full-strength novices
(previous rounds plateaued near 5%). The 100k evaluation scored 0.188
(12-45), beating 7.88 (0.094), 7.89/7.90 (0.141), and matching 7.88's 300k
checkpoint. Bridge climbed 0.60 → 0.20 before the crash.

The crash: a combat-damage trigger fetched Multiversal Passage, its
as-enters choice opened mid-resolution, and `_finish_damage_step` overwrote
the CHOOSE phase with END_OF_COMBAT. Choice actions are phase-routed, so the
pending `as_enters_pay_life` decision became unreachable and strict training
correctly aborted on a period-1 PASS cycle. Fixed at both layers
(`tests/choice_phase_integrity_test.py`): the damage step now defers its
transition through `previous_priority_phase` when a decision context is
pending, and mask generation self-heals any future orphaned decision context
by restoring its phase and chooser instead of letting an episode brick. The
handicapped scripted opponents block far more than earlier rounds, which is
why this dormant path finally fired.

Run 2 (`round-7.91-annealed-ramp-v2`) reproduced run 1 deterministically,
annealed bridge to full strength at 244k, entered full_pool at 369k, and died
at ~375k on a second dormant defect: the COUNTER_SPELL mask matched
"counter target spell" without validating rider clauses, so a mask-valid
Spell Snare aimed at a mana-value-1 Daydream failed execution. The mask now
runs the same targeting-system validation as `cast_spell` and aims at a
spell that is actually a legal target (`tests/counter_spell_mask_test.py`).

The older Round 7.89/7.90 interrupted-run manifests can still report
`status: running`; that is a historical lifecycle-recording defect, not
evidence that either process remains active. Their checkpoint results remain
diagnostic only.

### Run 3 verdict (`round-7.91-annealed-ramp-v3`, interrupted at ~730k)

Run 3 cleared both fixed defects and produced the round's full result:
fastest climb in project history (0.281 qualification at 400k versus 7.88
needing 800k+ for 0.25), then an oscillating plateau — 0.188, 0.203, 0.109,
0.281, 0.188, 0.250, 0.109 across the seven evaluations. The old interval was
a descriptive Bernoulli estimate over repeated, paired cases and should not be
read as a precise independent-sample bound. The curriculum ramps raised the
*speed* to competence, but did not show a higher ceiling. The pooled 448-game
diagnostic suggests concentrated deck-skill holes rather than diffuse
weakness; because it reuses cases across correlated checkpoints, it does not
locate the ceiling precisely.

Those scores are not a controlled comparison with Rounds 7.89/7.90. Those
runs used eight environments, training/evaluation seeds `20260715`/`21260715`,
and schedule hash `f5aa…`; Round 7.91 used six environments, seeds
`42`/`1000042`, and schedule hash `bde3…`. Each suite was fixed within its own
run, but the cases and training configuration differed across rounds.

The pooled diagnostics were:

| Skill hole | Evidence (eval, all 7 checkpoints pooled) |
| --- | --- |
| Piloting reactive decks | 4c Control 0-43 (0%), Jeskai 12%, Dimir 11% as agent decks; training full_pool win rates 3-4% on the same decks — 37% of full_pool episodes produce near-pure loss signal |
| Beating token swarms | 1-54 vs Azorius Momo, 4-52 vs Selesnya as opponents |
| Closing against durdle | 30 and 34 of 56 games vs 4c Control / Jeskai hit the 31-turn limit |

Round 7.92 implements the two stage-level responses: `combat-v5` (now the
fresh-run default) gives goldfish 25 turns — runs 1-3 all lost goldfish to its
deadline at ~71% timeouts under 20 turns — and extends the annealed handicap
to the scripted portion of `full_pool` (epsilon 0.40 → 0 by ratchet). The
full-pool bag is 80% scripted and 20% novice, but before v5 both active
profiles began at full strength; it was therefore 100% active full-strength,
not 80% full-strength. The ratchet now keeps a separate window of qualifying
scripted outcomes, so interleaved novice games cannot prevent the required 24
scripted samples from ever accumulating.

The same fix batch makes fidelity enforcement cover failed effect
continuations and lost-spell recoveries (with diagnostic contexts), persists a
pair-aware 95% qualification interval, and gates promotion to `best_model.zip`
on its lower bound rather than the point estimate. These changes pass the
delivery gate.

The pilot-side skill holes need a different
lever and are deliberately **not** addressed by v5; candidates for the next
decision, in rough order of expected value:

1. Matchup-weighted scheduling: oversample the agent piloting its worst
   decks once a win-rate floor exists, or handicap opponents specifically
   when the agent pilots a reactive deck (per-matchup epsilon).
2. Larger held-out paired-seat qualification suites if the lower confidence
   bound remains too wide for a promotion decision.
3. Policy capacity/optimization (network width, rollout length, entropy
   schedule) — only after the curriculum-level levers are exhausted, and
   as a single attributable canary change.

The named `--canary-config round-7.92` contract checks its enumerated CLI and
complete PPO setting tree, reward-contract version/scalars, the full resolved
curriculum hash, feature-output width, CUDA device class, hashed lineage, and
evaluation schedule: one million timesteps, 100k periodic evaluation with 64
games (32 seat-swapped pairs), 50k checkpoints, eight training environments,
training seed `20260715`, independent evaluation seed `21260715`, Observation
v3, reward v6, and `combat-v5`. Mismatch in those checked fields fails before
training. Git/patch provenance, runtime libraries, and the specific GPU model
are recorded for audit rather than constrained by the canary selector.

### Run 1 result (`round-7.92-combat-v5-v1`) and the delayed-return fix

The first v5 run stopped at 393,256 timesteps when a mask-valid priority pass
advanced into the end step and Parting Gust returned Namor the Sub-Mariner
with a +1/+1 counter. Battlefield abilities were registered after enter
counters, so the counter's immediate layer pass encountered Namor's unresolved
`*` power before its characteristic-defining ability existed and raised a
`TypeError`. The pass action itself and its mask were correct.

Battlefield abilities are now registered before enter counters, and Layer 7c
leaves unresolved symbolic power/toughness alone until a characteristic-
defining effect resolves it. Replay also restores the recorded opponent
handicap and turn limit, making the retained 162-action production failure
artifact deterministic through the repaired transition. The exact replay,
430 unit tests, 404 golden scenarios, runtime and training smoke suites, and
the default invariant fuzz gate are green. Curriculum scheduler state is not
checkpointed, so the 350k checkpoint remains diagnostic only; launch a fresh
`round-7.92-combat-v5-v2` canary rather than resuming it.

---

## Earlier run finding — `round-7.87-curriculum-v5` is diagnostic-only; Round 7.88 repaired

`ALPHA_ZERO_MTG_V3.00_20260715_002725_round-7.87-curriculum-v5` was manually
interrupted at 107,200 steps. It was fidelity-clean, but the fixed curriculum
advanced much faster than demonstrated ability: goldfish finished 33 decisive
wins, no losses, and 49 timeouts; race then fell to 13 wins and 265 losses;
bridge recorded 5 wins and 169 losses. The 25k fixed evaluation consumed about
937 seconds and returned 1 win, 50 losses, and 13 timeouts, yet that first weak
checkpoint was still promoted to `best_model`. The 100k boundary was skipped
because the single evaluator was backlogged. Critic values also became much
larger than rollout rewards while fit quality was unstable, and per-card gzip
statistics produced avoidable small-file I/O.

Round 7.88 keeps reward v5 and Observation v2, but changes the training and
run-lifecycle controls:

- `combat-v2` advances only after a rolling mastery window meets decisive-win,
  loss, timeout, and minimum-stage-duration gates. Opponent strength ramps via
  mixtures instead of jumping from passive to 100% novice.
- Full evaluation defaults to every 100k steps. A checkpoint needs a 55%
  qualification score (decisive wins plus half non-timeout draws) before
  promotion to `best_model.zip`; best-so-far candidates remain observable.
- Evaluation game logs identify the exact checkpoint timestep and SHA-256.
  Backlog skips and interruption cancellations are durable, pending snapshots
  are cleaned up, and user interruption has its own manifest status and
  `interrupted_model` artifact.
- Every run has `logs/<run>/training.log`. Training statistics batch compressed
  aggregate writes for ten games and flush on shutdown. Critic scale telemetry
  now warns on repeated value/reward divergence.

Round 7.89 preserves `combat-v2` for reproducibility and made `combat-v3` that
round's fresh-run default. Its mastery window retains opponent profile; race
requires a novice floor, and bridge requires separate novice and scripted
floors. Stage
deadlines are reported as forced transitions and bound full-pool entry to about
375k timesteps, rather than allowing bridge to consume most of the run.

---

## Previous run finding — `round-7.86-combat-v4` is diagnostic-only; Round 7.87 repaired

`ALPHA_ZERO_MTG_V3.00_20260714_195653_round-7.86-combat-v4` was interrupted
after its 350k checkpoint. Combat was reachable and the engine recorded 1,303
training games, but the policy did not learn a reliable win condition: 309
games reached the turn limit, 99 of the 137 logged wins were only timeout life
leads, and just 38 games were decisive wins. The 125k checkpoint selected as
"best" later went 0–10 with six timeouts; the 250k checkpoint went 3–7 but was
not promoted. The critic was already fitting its targets (explained variance
about 0.874) while policy KL remained around 0.003, pointing to a weak policy
update and a bad objective/evaluator rather than an underfit value network.

The postmortem found one Priority 0 reward bug: when the scripted opponent's
action ended a game, its acting-seat `game_result` was discarded. PPO commonly
received the `-0.25` fallback instead of the learned seat's `+10`/`-10`
terminal outcome even though later statistics inferred the result correctly.
Round 7.87 establishes a new reward/training boundary:

- Reward contract v5 translates opponent-ended results back to the fixed
  learned seat, gives all turn limits and safety truncations `-10`, removes
  handler-local action reward from the optimized objective, and records a
  terminal-result sign diagnostic.
- Periodic evaluation uses one immutable 64-game suite of 32 seat-swapped
  deck/seat/seed pairs.
  Promotion is lexicographic: decisive wins, decisive win-minus-loss score,
  fewer timeouts, then shaped return. `evaluations.json` retains every case,
  resolved runtime identity, outcome, checkpoint SHA-256, and promotion
  decision. Adaptive card/deck history is disconnected from evaluator choices,
  so checkpoint order cannot change the fixed opponent.
- `combat-v1` provides deterministic, worker-isolated opponent progression:
  passive two-deck goldfish at 0, novice race at 30k, four-deck mixed bridge
  at 75k, and the full mostly-scripted pool at 125k. Fixed evaluation always
  overrides the curriculum and uses scripted play.
- PPO defaults move to learning rate `2e-4`, rollout `1024`, batch `256`, gamma
  `0.999`, GAE lambda `0.98`, value coefficient `0.25`, and five epochs. This
  is a measured canary configuration, not a claim that tuning is finished.
- Harvest qualification and promotion give timeout life leads zero points.
  Matchup/profile/stage identity is now present in run manifests, game logs,
  replays, and TensorBoard outcome telemetry.
- Reward-v5 telemetry now covers fail-closed early returns as well as ordinary
  terminal transitions. Dirty-run source patches include untracked files, and
  cloned deferred resolutions safely initialize missing turn-ledger buckets.

Observation v2 itself is unchanged at
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.
Do not resume `round-7.86-combat-v4`; its policy optimized the broken reward
and its checkpoints were selected by the old random, return-based evaluator.

---

## Previous run finding — `round-7.85-reward-v8` invalid, Round 7.86 repaired

`ALPHA_ZERO_MTG_V3.00_20260714_172013_round-7.85-reward-v8` was interrupted
at roughly 275k steps and is diagnostic-only. Its persisted training logs
contain 600 games: 548 turn limits (91.3%), 41 decking losses, only 10
life-total endings (1.7%), and zero fidelity-counter failures. The low lethal
rate was not primarily a policy-strength result: the public damage phases
allowed both players to pass directly to end of combat without ever invoking
the combat resolver. A minimal unblocked 3-power attacker reproduced the
failure with both masks exposing only Pass and the defender remaining at 20.

Round 7.86 closes the live combat and reward defects exposed by that run:

- Empty-stack double-pass now performs mandatory combat damage and fails
  closed if the resolver does not mark the current damage step complete.
  Multi-blocker ordering follows the same failure contract.
- Attack declaration taps non-vigilance attackers, both seats use the active
  turn player for damage ownership, and combatants persist through end of
  combat before being cleared.
- First-strike and regular damage are separate priority windows. Eligibility
  is snapshotted, blocked status survives departed blockers, and double strike,
  trample, planeswalker/battle targets, ordering, and simultaneous damage use
  the preserved combat declaration.
- Canonical source-damage events now cover players, creatures, planeswalkers,
  and battles with correct source/controller/type filters. Printed and granted
  lifelink share one actual-damage path and state-based actions wait until the
  simultaneous batch, including lifelink, is complete.
- Attackers and blockers beyond the 20-card detail window use the paged action
  catalog; overflow blockers can also complete or withdraw sequential menace
  declarations beyond the ten atomic multi-block targets.
- Declaration completion has one public policy action instead of a duplicate
  Pass alias. The scripted baseline opens overflow combat choices before
  finishing declarations.
- Reward contract v4 gives every turn-limit result a flat `-6`; a small life
  lead can no longer earn `+2` by avoiding combat until timeout.

This is a new reward/gameplay checkpoint boundary but not an Observation v2
schema change. The observation hash remains
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.
Do not resume the interrupted model, its best model, or its checkpoints.

## Previous run finding — `reward-v7` invalid, Round 7.85 repaired

The first full Observation v2 attempt,
`ALPHA_ZERO_MTG_V3.00_20260714_155433_reward-v7`, is not a usable policy
lineage. Strict fidelity correctly stopped it after a mask-valid Room unlock
failed in environment 2. Its 25k–100k evaluation trend is diagnostic only;
none of its best, checkpoint, or failed-model artifacts may be resumed,
qualified, or harvested.

Round 7.85 closes every issue found between the 7.84 freeze and that abort:

- Combat lookahead now restores life, poison, counters, P/T, and combat-trigger
  state after hypothetical damage, and damage accumulation no longer assumes a
  pre-existing dictionary entry.
- Stale attackers/blockers that already left the battlefield are excluded from
  combat simulation; star-P/T objects outside the battlefield safely use zero
  instead of raising on `None`.
- Room unlock masks and execution share one land-aware affordability/payment
  transaction. Generated actions pin controller, battlefield occurrence, Room
  ID, and door number; execution auto-taps the lands accepted by the mask, and
  full-unlock detection now reads the actual door dictionaries.
- Earthbend's reminder-defined target is reconstructed for activated-ability
  legality, target choice, stack validation, and resolution. Ba Sing Se now
  commits a controlled land before it taps or pays mana, eliminating the empty
  mandatory-target warning.

These are gameplay and policy-boundary repairs, not an observation-contract
change. Observation v2 remains at hash
`8b77a325816aec9fd6a8b7a8e924a2b936a092e163b81f2d0a22947387804ea8`.

---

## Latest finding — Observation v2 planner audit complete

Round 7.84 completes the audited, self-hashed Observation v2 policy contract
before its first training run:

- Canonical cards receive stable categorical identities: `0` is padding, `1`
  is visible unknown/off-registry, and canonical registry index `N` is `N+2`.
  All identity fields share one fixed 65,536-entry embedding. Per-game runtime
  IDs remain protocol metadata and never become learned semantic identity.
- Public state now includes both libraries; poison, energy, and experience;
  monarch and city's blessing; permanent counters, marked damage, attachments;
  exact attack targets and blocker assignments; richer top-first stack objects;
  both players' regular, snow, and restricted floating mana; and symmetric
  graveyard/exile windows with face-down visibility masks.
- Hand, battlefield, graveyard, exile, stack, target-page, and choice-page card
  windows carry categorical identity. Hidden opponent information remains
  zeroed; visible generated or off-registry objects use the unknown category.
- Public indices, controllers, battlefield counts, combat maps, targets, and
  zone windows are observer-relative. Perspective tests pin both seats.
- Dead or duplicated v1 fields were removed: absolute-seat life/battlefield
  copies, phase one-hot, duplicate battlefield counts/keywords/tapped/mana,
  `remaining_mana_sources`, `hand_performance`, and asymmetric key-card zones.
- `OBSERVATION_SCHEMA.md` records every field's shape, bounds, perspective,
  saturation, visibility, and extractor route. The schema version/hash is now
  mandatory lineage beside registry and feature-schema identity.
- Extractor coverage is exhaustive: phase uses its dedicated embedding,
  semantic identities use the shared categorical embedding, continuous fields
  use the symlog/MLP path, and masks/runtime IDs stay external.
- Strategy memory is no longer policy input. Its former empty-memory behavior
  injected a random legal action, reset-time reconstruction discarded unsaved
  evidence, and online per-worker state made evaluation non-comparable. The
  optional replacement is deterministic, action-specific, atomically saved,
  reused across resets, disabled by default, and isolated per environment.
- Planner-derived observations are now pure reads. Multi-turn mana development
  uses expected land draws instead of sampling, and wide-board attacker search
  uses a stable bounded combination order instead of the game RNG.
- Opponent archetype inference excludes face-down exile and face-down
  permanents. The fake `estimated_opponent_hand` tensor was removed because it
  ranked exact candidates from a runtime database containing hidden deck
  instances.
- Disabled `recommended_action` inputs were removed, and `strategic_metrics`
  was compacted from ten positions to its seven live values.

**Historical v2 verdict:** no high-priority correctness defect was known at
that freeze. Round 7.89's deeper audit nevertheless found semantic defects and
supersedes it with Observation v3 at
`6e29a94e3443881681afd794185f061133f24ff72350a7df27f48524f00d4137`.
Changing a field, bound, identity capacity, visibility rule, or extractor route
starts a new schema/checkpoint lineage. The next training run must record actual
v3 throughput and memory alongside behavior telemetry.

---

## Current execution plan

### Now — verify and run the Round 7.92 pinned canary

1. Keep the now-green delivery gate mandatory for the mixed-pool ratchet,
   strict fidelity counters, composite-payment transaction, canary validation,
   and paired qualification interval. Do not launch if that gate regresses.
2. Launch a fresh Standard candidate with
   `--canary-config round-7.92 --run-name round-7.92-combat-v5-v2`. The named
   configuration checks reward `discounted-state-potential-v6`, the full
   resolved `combat-v5` hash, feature width/device class, 1M timesteps, eight
   environments, 100k periodic
   evaluation with 64 games (32 seat-swapped pairs), 50k checkpoints, training
   seed `20260715`, and evaluation seed `21260715`.
   Do not resume an older curriculum checkpoint.
3. Confirm `race` and `bridge` satisfy their full-strength profile floors and
   reach epsilon zero before mastery. In the terminal `full_pool` stage,
   interleaved novice games must not starve the qualifying scripted window:
   epsilon must reach zero by 750,000 trainer timesteps, followed by at least
   24 full-strength scripted `full_pool` outcomes before the 1M-step run ends.
   Treat either miss as a failed canary acceptance criterion. A stage deadline
   transition must be reported as `deadline`.
4. At each 100k boundary, compare checkpoints through `evaluations.json`.
   Require balanced case/seat exposure, zero reward-sign failures, zero strict
   fidelity counters (including effect-continuation failures and lost-spell
   recoveries), and promotion to `best_model.zip` only when the pair-aware 95%
   qualification lower bound reaches 55%.
5. Keep the enumerated named-canary fields unchanged. A mismatch in the checked
   seed, worker-count, schedule, reward/PPO configuration, resolved curriculum,
   feature width/device class, or hashed lineage invalidates the comparison.
   Treat source, runtime-library, or specific-GPU differences recorded in the
   manifest as audit variables; the selector records but does not constrain
   them.

### Next — qualify and calibrate Standard

1. Pass an independent held-out paired-seat scripted qualification: the 95%
   lower confidence bound for decisive wins plus half non-timeout draws must
   reach the configured 55% threshold, with timeout life leads worth zero,
   zero fidelity counters, and exact checkpoint/lineage provenance. Do not
   reuse the training seed or the periodic-evaluation schedule as final
   qualification evidence. The current Harvest CLI enforces its 55% point
   score only, so its pass remains necessary but not sufficient until the
   protocol persists and enforces the same interval.
2. Freeze the qualified checkpoint as the baseline and promote it into the
   checkpoint league.
3. Run 3–5 known-matchup deck pairs at Harvest scale and compare simulated
   winrates with published or expert expectations.
4. Only after strength and calibration pass, run the first format-isolated,
   fidelity-clean Standard production harvest.

### Then — close the loop and expand formats

1. Implement the deck-builder consumer for `STATS_SCHEMA.md`, support status,
   uncertainty, matchup data, and format legality.
2. Automatically route builder candidates through legality, support preflight,
   paired-seat evaluation, and the appropriate format's Harvest queue.
3. Build a representative Modern corpus and repeat support triage,
   qualification, calibration, and production Harvest.
4. Repeat for Pioneer.

### Work that can proceed alongside training

- Fix newly observed fidelity failures immediately, scenario first.
- Continue the Standard support sweep in descending manifest impact order.
- Watch v3 identity-category coverage and observation degradation telemetry;
  treat any schema mismatch or hidden-information leak as a run-stopper.
- Profile production-sized training and Harvest workloads; land optimizations
  as separately verified changes.
- Measure cast transaction cost in the pinned canary before changing rollback:
  a logging-disabled 120-card microbenchmark measured a 15.21 ms median
  constructor-free checkpoint versus 34.30 ms for the canonical clone (12
  timed samples). Ordinary casts take one checkpoint and nonmana-cost preflight
  currently takes about two; optimize only if end-to-end profiling identifies
  this as a material training bottleneck.

---

## Active workstreams

### 1. Policy strength and opponent quality — ◐

Checkpoint/self-play policies already receive their own perspective-correct
observation and legal mask. The paired-seat qualification command fails closed
on illegal predictions, fidelity failures, and provenance mismatches. What
remains is operational: train a policy that passes, freeze it, then replace the
scripted Harvest baseline with qualified policy-vs-policy or league play.

### 2. Card fidelity and coverage — ◐

The support manifest records `crash`, `unparsed`, and `partial` clauses and
persists them beside statistics. Worst severity sticks per card. The builder
must exclude crash/unparsed cards and distrust or down-weight partial-card
statistics.

Standard's pinned 4,702-card ledger, last measured July 17, contains:

| Evidence class | Cards |
| --- | ---: |
| Verified | 96 |
| Observed clean | 63 |
| Unseen/static clean | 3,348 |
| Partial | 745 |
| Unparsed | 450 |

That is 74.6% static-clean but only 3.4% evidence-qualified. A clean manifest
for the representative corpus does not prove unseen cards are faithful.

Workflow: harvest → rank failures by real frequency/impact → write a failing
scenario → implement the smallest reusable parser or exact-card fix → verify
the ledger promotion. Untested subsystems remain suspect even when static
classification is clean.

### 3. Verification and replay — ✅ infrastructure, ◐ calibration

- Golden scenarios cover known regressions and policy contracts.
- Deterministic invariant fuzz checks zone/stack conservation, SBA fixed
  points, mask-valid execution, bounds, observation degradation, mask purity,
  finite rewards, mana clearing, and layer idempotence.
- Failures retain seed, actions, context, state, and a replay command; clean
  seeds leave no artifact.
- Successful fixed-evaluation games now retain evaluation-only, both-actor
  action/state traces in atomic gzip sidecars. The dependency-free Stats
  Workbench joins every case to its game-log provenance and loads one selected
  trace on demand; historical evaluations are explicitly summary-only.
- The remaining acceptance gap is matchup calibration, not test
  infrastructure.

### 4. Throughput — ◐

Reference-machine baseline: Ryzen 5 5600, 32 GB RAM, RTX 5060. Before async
evaluation, effective training was 10.6 steps/s while pure rollout reached
roughly 40–55 steps/s; synchronous evaluation consumed about 73% of wall time.
Round 7.76 moved evaluation to a dedicated process and widened the network to
use the otherwise idle GPU.

Measured hot paths remain environment-side: observation construction, repeated
436-field card feature generation, text parsing/string normalization, and
repeated 480-action mask generation.

Safe optimization order:

1. Cache immutable per-card feature slices; invalidate mutable P/T, counter,
   type, controller, and zone-dependent data explicitly.
2. Cache planner analysis only by **state version plus observing player**.
   Turn-only caching is forbidden because it caused the 7.82 same-turn and
   cross-seat correctness failures.
3. Cache action masks only against an action-relevant state/choice version.
4. Require bit-identical observations and masks under scenarios and fuzz, then
   re-profile. These optimizations must not change schema or semantics.

### 5. Harvest and deck-builder integration — ◐

Parallel deterministic shards, checkpoint identity, aggregate success-only
manifests, candidate scoring, paired-seat qualification, and promotion gates
are implemented. Remaining: production-scale throughput measurement, a
qualified checkpoint, calibration, builder-side exclusion/confidence logic,
and automatic candidate feedback. The unified Stats Workbench now exposes run
lineage, evaluations, isolated DeckStats scopes, fidelity/support evidence,
and Harvest/promotion manifests, but it is an operator/debugger rather than the
missing automated builder consumer.

### 6. Format program — ◐

- ✅ Shared foundation: canonical append-only registry, frozen self-hashed
  feature schema, explicit format/deck configuration, legality checks, and
  lineage-stamped manifests.
- ◐ Standard: corpus, namespace, and Observation v3 exist; qualification,
  calibration, and production Harvest remain.
- ▢ Modern: no representative training corpus yet.
- ▢ Pioneer: no representative training corpus yet.
- ▢ Unified builder: format-aware feedback queue not connected.

---

## Known limitations that still matter

The current fix batch has no known unresolved Priority 0 simulation-integrity
defect. Priority 1 agent choice exposure
is substantially complete, but combat still has bounded rules
families rather than complete coverage. The following behaviors fail closed or
remain fidelity-marked rather than being treated as fully supported.

| Area | Current boundary |
| --- | --- |
| Combat assignment | Multi-blocker damage uses an ordered automatic approximation and cannot express every legal split. |
| Combat requirements | Common evasion and restrictions work; lure effects, attack/block costs, and unusual must-attack/must-block combinations remain partial. |
| Combat objects | Planeswalker targeting is covered; battle protector/controller behavior remains incomplete. |
| Combat cleanup | Exert is recognized for attacking but does not yet enforce skipping the creature's next untap. |
| Damage modification | Core prevention, protection, indestructible, deathtouch, lifelink, and trample paths work; rare replacement/prevention wording remains parser-dependent. |
| `as enters` | Common creature type, color, card/basic-land type, opponent, counters, life-payment, and deferred-ETB choices work; arbitrary consumers remain card-specific. |
| Emblems | Kaito anthem and Wrenn graveyard permission are implemented; other emblem text is retained but not executed generically. |
| Double-faced/adventure cards | Back-face casting works; direct nonland back-face entry and complete back-face/adventure targeting remain incomplete or heuristic. |
| Generic mechanics | Discover, Explore, Investigate, Endure, Connive, Suspect, Airbend, Equip, Crew, Prepare, Warp, and Earthbend intentionally support bounded text/count families. |
| Trigger/condition parsing | Reflexive triggers, rare phase scopes, structured sacrifice, activation costs, target-conditioned pricing, and unusual attack adjectives use bounded vocabularies. Unknown templates fail closed. |
| Search/permissions | Uncommon linked optional searches and source-duration Warp permissions need exact transactions. |
| Dice | Ordinary result tables and roll events work; modifiers, rerolls, ignored rolls, and tableless result clauses do not. |
| Mutate/Meld/Specialize | Mutate lacks per-component replacement and library-order choices. Meld/Specialize require complete local family data and fail closed when absent. |
| Exact-card fallbacks | Screaming Nemesis, Anoint, Obliterator, Cavern of Souls, and Obstinate Baloth have documented conservative boundaries. |
| Hidden exile | Current runtime IDs are unique and hidden correctly. A legacy state sharing one ID between visible and hidden occurrences hides both. |
| Action tensor | Fixed at 480 actions. Action 479 pages overflow contexts through normal handlers; dormant indices 205–223 remain mask-invalid until their mechanics exist. |
| Format data | Registry/schema are lineage-bound and Bo1. Sideboards are retained and legality-checked but not played. Registry growth changes registry lineage; observation-field or identity-capacity changes require a new policy lineage. |

---

## Definition of done

The project is complete only when all of these are true:

1. Every delivery keeps the required scenario, smoke, training, invariant,
   replay, Harvest, registry, ingestion, and long-fuzz gates green.
2. There are no known stats-corrupting defects, and every fixed defect has a
   permanent guard.
3. Every card admitted to a format's builder pool is evidence-qualified or
   explicitly excluded/down-weighted according to its support status.
4. Every value-relevant player decision is exposed to the policy, including
   arbitrary legal multi-blocker damage distributions.
5. A trained policy passes independent held-out paired-seat qualification at
   the configured 95% lower-bound threshold, and statistics come from
   qualified policy/league play rather than the random or scripted baseline.
6. Known-matchup calibration passes within a documented tolerance.
7. The format-aware builder consumes version-matched statistics and support
   evidence, proposes legal candidates, and feeds them back into evaluation
   and Harvest without manual file editing.

---

## Checkpoint and schema boundaries

Historical boundaries are retained here so old artifacts cannot be resumed by
mistake. The practical rule is: **start fresh from the Round 7.89 Observation
v3 boundary using the current Round 7.92 reward/curriculum contract.**

| Minimum round | Incompatible change |
| --- | --- |
| 7.37 | Reward rebuild and playable scripted baseline |
| 7.44 | Declared observation-space change |
| 7.62 | X/count observation bounds |
| 7.72 | Discounted state-potential reward and critic baseline |
| 7.73 | Timeout terminal contract and symlog extractor inputs |
| 7.76 | Network width and async evaluation lineage |
| 7.80 | Offense-weighted reward v3 and live combat-damage observation |
| 7.82 | Exhaustive extractor routing and repaired observation semantics |
| 7.83 | Frozen Observation v2, categorical card identity, public-state expansion, v1 compaction, and deterministic strategy-memory boundary |
| 7.84 | Deterministic planner observations, hidden-information-safe inference, and removal of fake/dead planner inputs |
| 7.85 | First-v2-run fidelity boundary: isolated combat simulation, live participant filtering, Room mask/payment parity, and Earthbend target commitment |
| 7.86 | Mandatory public combat damage, split first-strike steps, preserved blocked status, canonical all-target damage/lifelink, overflow combat actions, and flat-timeout reward v4 |
| 7.87 | Correct opponent-terminal perspective, reward v5, fixed outcome evaluation, deterministic combat curriculum, and PPO canary defaults |
| 7.88 | Mastery-gated curriculum, asynchronous evaluation lifecycle, qualification-based promotion, and batched training statistics |
| 7.89 | Observation v3 semantic corrections, profile-specific mastery gates, bounded full-pool entry, balanced evaluation exposure, and the renewed combat audit |
| 7.90 | Analytics memory-redundancy refactor, validated behavior-neutral against the 7.89 canary trajectory |
| 7.91 | Annealed opponent handicap (`combat-v4`), stage turn limits, graded turn-limit reward v6, doubled damage-progress potential, and the paired-seat observation audit |
| 7.92 | `combat-v5` (goldfish 25 turns, full-pool handicap ramp), mixed-profile-safe terminal ratchet windows, strict recovery fidelity counters, pinned canary seeds/configuration, and pair-aware lower-bound qualification |

The canonical registry is append-only within the fixed identity capacity;
appends change registry lineage without changing observation width. Changing
feature vocabulary, observation fields, bounds, visibility semantics,
identity capacity, or extractor routing changes a schema hash and starts a new
policy lineage. Run manifests—not individual game-log rows—carry format, pool,
corpus, registry, both schema identities, policy, and checkpoint provenance.

---

## Delivered history — compact record

- **Rounds 1–7.22:** built the parser/effect foundation and corrected the
  earliest rules, targeting, token, layer, replacement, and statistics
  failures.
- **7.23–7.36:** completed the policy boundary, choice audits, replay and fuzz
  harnesses, parallel Harvest/promotion protocol, and reproducible CUDA worker
  pipeline.
- **7.37–7.45:** rebuilt reward/telemetry and the scripted opponent, repaired
  game-ending paths, changed the observation space, and implemented Spree as a
  real casting transaction.
- **7.46–7.50:** introduced format namespaces, canonical registry, frozen
  feature schema, full Standard snapshot, support preflight, and self-hashed
  lineage manifests.
- **7.51–7.62:** made the representative corpus clean, expanded reusable
  mechanics, established true per-copy runtime identity, and closed the known
  Priority 0/1 integrity and choice lists.
- **7.63–7.70:** hardened mana/choice/worker integrity, made coverage evidence
  honest, launched paired-seat qualification, and converted live training
  failures into guarded fixes.
- **7.71–7.79:** repaired choice paging and lifecycle traps, rebuilt the
  learning signal and critic input contract, moved evaluation asynchronous,
  widened the network, and aligned mask/selection/resolution targeting.
- **7.80–7.82:** introduced offense-weighted reward v3, audited observation
  liveness and semantics, revived combat search, completed extractor coverage,
  removed unstable IDs from learned inputs, repaired exact target/stack
  summaries and perspective freshness, and fixed the blocker lookup exposed by
  the real search path.
- **7.83:** froze Observation v2 with stable categorical card identity, filled
  missing public zones/resources/counters/attachments/combat/stack state,
  compacted dead v1 duplicates, removed online strategy memory from policy
  input, made the optional memory deterministic and disabled by default,
  documented and hashed the full contract, and made its identity part of every
  lineage manifest.
- **7.84:** completed the planner-observation audit: removed RNG consumption,
  excluded face-down identities from opponent inference, removed fake exact
  hand/action inputs, compacted live strategy metrics, and pinned the final v2
  hash with purity regressions.
- **7.85:** invalidated the first v2 training attempt after strict fidelity
  caught a Room unlock mismatch; made combat lookahead state-pure and resilient
  to stale/star-P/T participants, unified Room mask/payment execution, and
  restored mandatory Earthbend targeting without changing the v2 schema hash.
- **7.86:** invalidated the interrupted `round-7.85-reward-v8` lineage after
  proving public passes skipped combat damage; made damage mandatory and
  fail-closed, separated and snapshotted damage steps, preserved combat state,
  unified source events and lifelink, opened overflow combat choices, and
  removed the timeout life-lead incentive with reward contract v4.
- **7.87–7.90:** corrected terminal perspective with reward v5, introduced
  deterministic mastery gates and asynchronous fixed evaluation, moved to
  Observation v3 with semantic/profile-floor corrections, and validated the
  behavior-neutral analytics-memory refactor.
- **7.91:** introduced annealed active opponents, graded reward v6, explicit
  stage turn limits, and a scoped paired-seat observation audit; three runs
  demonstrated faster curriculum climbing but a concentrated matchup plateau.
- **7.92:** introduced `combat-v5`, repaired the full-pool qualifying-outcome
  ratchet, expanded strict recovery fidelity telemetry, pinned independent
  train/evaluation seeds in a named canary contract, and changed periodic
  checkpoint qualification from a point estimate to a pair-aware 95%
  lower-bound gate. The delivery suite is green at 430 unit tests, 404
  scenarios, 9/9 runtime smoke, 13/13 training smoke, and 8/8 default fuzz
  seeds plus the phase-boundary check. The held-out Harvest protocol still needs the same
  interval enforcement before its point-score pass is sufficient evidence.
- **7.93:** diagnosed the round-7.92 flatline (evaluation peaked at 0.281 at
  200k, then decayed once every stage fell to its deadline and 24-episode
  raw-rate windows ratcheted scripted play to full strength on noise-level
  evidence — 6/24 wins, exactly the stage floor — while the training win
  rate collapsed to ~8% with no path back). The handicap ratchet now gates
  both directions on the window's 95% Wilson interval and is reversible: a
  window confidently below target hands one rung back, never past the
  stage's configured start, including at epsilon zero. `combat-v6` widens
  the ratchet windows to 48 episodes so the interval can resolve either way,
  and the `round-7.93` canary pins the otherwise-unchanged 7.92 contract
  over it. The Three Steps Ahead modal continuation failure that ended the
  run was fixed separately. A low-play-rate card audit of ten suspects
  through the production mask/cast/trigger paths cleared eight as fully
  functional and fixed two real gaps: `_targets_available` now applies CR
  601.2b to modal spells through the shared `modal_mode_is_selectable`
  predicate (Bushwhack was uncastable whenever its fight mode lacked
  targets), and blight is now a supported optional additional casting cost
  with its paid/unpaid record gating "if this spell's additional cost was
  paid" riders at resolution (Requiting Hex previously skipped the cost and
  granted its life unconditionally). Three golden scenarios pin the fixes.
- **Post-7.93 low-play follow-up:** audited the eighteen reported cards against
  their opportunity-adjusted usage, production action masks, parsing, event
  routing, and resolution paths. The audit repaired modal Charm selection,
  stack-target and controller scoping for Surrak, nested intervening-if parsing
  for Earthbender Ascension, Crew-value/self-trigger recovery for Lumbering
  Worldwagon, Warp access beyond the first eight hand slots, optional and
  once-per-turn reflected damage for Jennifer Walters, layered/LKI source power
  for Ouroboroid, fail-to-find library search for Starfield Shepherd, and a
  false Badgermole replacement registration caused by reminder text. Exact
  per-game drawn/opening-hand counts are now retained in individual card stats,
  so the viewer can distinguish a card that was never seen from one declined
  despite being available. Icetill Explorer, Seam Rip, Spider Manifestation,
  Sage of the Skies, Deadly Cover-Up, Iroh's Demonstration, M.O.D.O.K., and Day
  of Judgment were cleared through their existing production paths. North Wind
  Avatar's wish resolution is functional when an outside-the-game pool is
  supplied; current deck data does not define such a pool, so no targets are
  invented. The delivery gate is green at 471 discovered unit tests and 408
  scenarios, plus all eight default invariant-fuzz seeds and the controlled
  phase-boundary check.
- **7.94:** overhauled the reward system after the stopped
  `round-7.93-cardfix-v1` run confirmed the pattern across 7.88-7.93: with
  flat +-10 terminals the policy converged on timeout-dominant play (82%
  goldfish / 76% race timeouts with zero decisive losses — it never lost,
  it just never finished). `tempo-graded-potential-v1` replaces the
  discounted-state-potential family: decisive wins earn a bounded speed
  premium (+10 up to +14 by unused engine-turn budget, stationary across
  stage turn limits), turn-limit stalls grade continuously on opponent
  damage (-10 untouched up to -7 near lethal, result labels ignored so
  lifegain cannot cushion the penalty, always below a real draw), draws pay
  -3 instead of the near-neutral -0.25, every step costs 0.005 reward so
  stalling bleeds and conceding hopeless games recycles samples, and the
  potential drops the hand-hoarding term while making the convex damage
  ramp dominant (life 0.10, board 0.15, damage 1.0 with slope 0.35x-1.65x).
  Shaping remains strictly potential-based and terminal-zeroed. The
  `round-7.94` canary pins the new contract over the unchanged combat-v6
  inputs and is validated against the live training config; the v6-era
  canaries and resume paths fail closed across the contract boundary.
- **7.95:** halved the scripted handicap ratchet step after run
  `round-7.94-tempo-v1` (1M steps, completed clean) validated the two-way
  interval ratchet but exposed its granularity: eval qualification rose
  0.047 -> 0.250 (best final of any run, still far under the 0.550 publish
  gate) while the back half of the run ping-ponged the scripted epsilon
  0.40 <-> 0.20 three times — ~38% decisive wins at 0.40 tightens, ~12% at
  0.20 relaxes, so the whole skill cliff sat inside one 0.20 rung.
  `combat-v7` sets the bridge and full_pool scripted steps to 0.10 (rungs
  at 0.30 and 0.10); the novice ramp keeps 0.25 — race never oscillated.
  Because each finer rung needs a fresh 48-episode window, the `round-7.95`
  canary doubles the horizon to 2M timesteps; every other input carries
  over from round 7.94 unchanged. Rewards are deliberately untouched this
  round: tempo-graded-potential-v1 has exactly one clean 1M-step run of
  evidence and the eval timeout tail (10-15%, scored as losses by the
  qualification gate) is its follow-up target if the finer ladder also
  plateaus. Two fixes from the round-7.94 stats audit ride along before
  launch. First, the hand "playable" observation flag computed
  affordability from floating mana only while the action mask used the
  land-aware check, so effectively every spell observed as unplayable;
  the environment now delegates to the action handler's affordability
  check and scenario 601.2f guards mask/observation agreement (the
  audit's affordability probe cleared the engine itself: masks, negative
  cases, conditional Verge activation, and tap payments were all
  correct). Second, record_game never received game_state, so every
  deck/card ahead/behind bucket sat at the tracker's parity default (0
  of 960 card files after 7.94); the environment now snapshots life
  totals per turn and classifies the winner's mid-game position (ahead,
  parity, or behind at the middle turn, 5-life margin), making
  snowball-versus-comeback analytics real.

### Institutional lessons retained from the silent-bug catalog

- An untested subsystem is assumed suspect; first-touch scenarios repeatedly
  found phantom methods, swallowed exceptions, and code paths that never ran.
- Mask, displayed choice, execution, and resolution must share one legality
  contract. Drift between them caused no-ops, target fizzles, and deterministic
  paging loops.
- Card identity, controller, and player perspective must be explicit at every
  boundary. Earlier ambiguity corrupted both gameplay and statistics.
- Observation shape/bounds are insufficient: every field needs liveness,
  semantics, perspective, extractor-routing, and degradation tests.
- Broad parsers must fail closed. A visible partial/unparsed ledger entry is
  safer than silently claiming support.

---

## Working agreements

- Write the failing scenario before implementing a defect fix.
- Treat any warning, degraded observation, swallowed exception, or fidelity
  counter as a correctness failure until classified.
- Keep masks and handlers on shared legality predicates and pin paged choices
  through execution.
- Run scenario, smoke, training smoke, and default invariant fuzz before a
  delivery; run focused suites for the changed subsystem and long fuzz on the
  scheduled/manual gate.
- Update the roadmap only with current decisions, measurable exits, new schema
  boundaries, or durable limitations; keep per-run debugging in logs.
- Never mix statistics or checkpoints across incompatible lineage hashes.
- Keep performance-only work semantically bit-identical and verify it before
  trusting benchmark improvements.
