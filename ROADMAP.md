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
   every delivery (currently 9/9, 13/13, and 364/364, plus 16/16 fixture-
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
  or crash cards in the audited representative metagame. Round 7.68 promoted
  M.O.D.O.K. through exact registration, activation, timing, Connive, and
  continuous-effect evidence. Round 7.67 closed the corpus's
  final partial, Esper Origins, through the real Flashback-to-Saga transaction,
  all three chapters, finality cleanup, and focused execution evidence. Round
  7.65 runtime-verified Emeritus
  of Ideation's full Prepared transaction, including its spell-face copy and
  exact re-prepare payment. Other Prepared cards remain classified by their
  own spell/effect text rather than inheriting that verification. Round 7.64
  reconciled runtime classification diagnostics with static ledger evidence,
  promoted all nine warning-path cards through focused execution tests, and
  now records unclassified permanent clauses as support issues instead of
  silently calling them clean. The generic Spree casting transaction and exact
  Three Steps Ahead effects are supported; other Spree cards remain
  subject to their own effect-parser, scenario, and manifest evidence.
  Format-wide quantified coverage remains manifest-driven. Round 7.50
  statically preflighted all
  4,702 current Standard cards and now separates verified, corpus-clean,
  unseen, partial, unparsed, crash, and explicitly excluded evidence. The
  current July 12 ledger contains 86 verified, 73 observed-clean, 3,327
  unseen-clean, 788 partial, and 428 unparsed cards: 74.1386643981% is
  static-clean and 3.3815397703% is evidence-qualified. The lower static-clean fraction
  is an evidence correction: 321 previously silent unclassified clauses are
  now reported honestly. Round 7.55 added
  generic full-pool coverage for eight recurring mechanic families without
  regressing any previously clean card.
- Tier 3 (training/environment): ◐ policy plumbing, audit work, and the explicit
  paired-seat scripted qualification gate are complete; a trained checkpoint
  still needs to pass it before Harvest is promoted to policy-vs-policy.
- Tier 4 (verification/calibration): ◐ invariant and long-fuzz gates are green;
  the matchup calibration study remains open.
- Tier 5 (operations/integration): ◐ Harvest orchestration and the fail-closed
  strength-qualification protocol are complete and, since Round 7.46,
  format/corpus-configurable with full run lineage; running a production-scale
  candidate, throughput profiling, and deck-builder integration remain open.
- Target-format program: ◐ milestone 1 (format foundation and lineage) is
  complete — frozen canonical registry + feature schema in
  `formats/standard/`, explicit `--format`/`--decks` configuration, and
  lineage-stamped manifests. User-supplied decks now route into isolated format
  pools automatically; passing policy qualification and builder feedback remain
  open.
- Test gates: smoke 9/9, training 13/13, scenarios 364/364 (grown from 12),
  95/95 focused regression tests, 195/195 discovered unit tests,
  fixture harvest 16/16, production Harvest
  protocol 16/16, card registry
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
   registry below), watch the entry stop accumulating.
3. The deck builder loads the manifest and **excludes `crash`/`unparsed`
   cards from candidate pools** and down-weights `partial` cards'
   statistics until support lands. This closes the loop Carter asked for:
   unsupported cards can't silently poison deck search.

Remaining Tier 2 work:
- ✅ **Crash-severity wiring**: per-card resolution exceptions now attribute
  `crash` entries to the support manifest instead of only logging.
- ✅ **Per-card override registry**: card name → hand-written effect callable,
  consulted before the text parser, for cards regex can't express.
- ✅ **Coverage report**: script joins a format's card pool against the
  manifest to print "N of M cards fully supported" — the format milestone.
- ◐ **Parser expansion** (Rounds 1–7.20): a reusable diagnostic harness
  measured the factory's no-op fallback rate across common oracle clauses and
  drove roughly twenty gap-closing rounds. Across them ~90 effect/mechanic
  classes were closed — removal, bounce, counters, tokens, keywords,
  sacrifice, reanimation, control, mana, library manipulation, variable-count
  effects, prevention, animation, levelers, Adventure, and duplicate-ID zone
  semantics — and the sample miss rate fell 6→13→14→9→10→3 before the sweep
  moved into mechanic subsystems. Several dead subsystems surfaced along the way
  (see the appendix bug catalog). New support work is now ordered by real
  manifest counts and format-pool coverage, not speculative subsystem ordering.
- ◐ **First-touch coverage sweep**: one scenario for every subsystem that has
  never had one. This practice has repeatedly found phantom methods and dead or
  overfiring subsystems, so untested corners remain suspect. Rounds 7.21–7.22
  closed the audited sample's remaining high-risk partials (Saddle, Duress/
  Oildeep hand choices, Cacophony Scamp, Leyline, Patchwork Beastie, Optimistic
  Scavenger, the per-card override registry) and verified Beza's Treasure through
  the shared permanent mana-ability path. The current eight-deck sample has no
  known high-risk partial; next candidates come from manifest counts, format-pool
  coverage, and the consolidated v1 limitations below.

## Tier 3 — Training & environment quality

1. ✅ **Choice exposure audit**: spell, activated-ability,
   triggered-ability, and direct-effect targets are agent choices. Independent
   modal target slots, paged target lists, opponent trigger ordering,
   multi-target counter allocation, generic SacrificeEffect selection, Dig
   selection, and generic activated-ability sacrifice costs are complete.
   Non-self activation costs stage explicit, paginated permanent IDs before the
   shared cost transaction commits; self-sacrificing mana/token abilities keep
   their deterministic fast path.
2. ◐ **Opponent policy**: checkpoint/self-play policies install through
   `set_opponent_policy()`, receive their own observation and legal mask, and
   fall back safely when predicting an illegal action. `harvest_protocol.py
   qualify` now measures the candidate from both seats, requires a 55% score by
   default, and fails closed on any fidelity or checkpoint-provenance mismatch.
   Remaining: train a checkpoint that passes that gate, then promote Harvest to
   policy-vs-policy.
3. ✅ **Hidden-information audit**: `observation_for()` enforces a player
   perspective; changing unseen opponent hand identities and library order
   leaves every observation field unchanged. Face-down masking is also guarded.
4. ✅ **Replay logs**: seeded resets record actions, contexts, and deck names;
   `export_replay()` writes JSON and `replay()` verifies the selected decks
   before reproducing the episode.
5. ✅ **Deck legality validation**: `Playersim/deck_legality.py` validates
   minimum size, copy/basic-land rules, bans, restrictions, and format status;
   strict deck loading raises validation failures.

**Round history (7.23–7.46, condensed).** These rounds took the engine from
the completed choice-exposure audit through the first real strength-training
attempts, converting each run failure into a permanent fix plus a regression
guard:

- **7.23–7.26** — closed the Tier 3 choice-audit items (counter distribution,
  SacrificeEffect, Dig, non-self activation costs), delivered
  `harvest_fixtures.py` and the `tests/invariant_fuzz_test.py` invariant
  harness, and audited the full policy boundary (observation-space bounds,
  mask-valid dispatch, seat alternation, no scripted fallback after an illegal
  checkpoint prediction). Added failure-only atomic fuzz artifacts and the
  `harvest_protocol.py` parallel/promotion protocol.
- **7.27–7.31** — a dead-subsystem sweep found and fixed a string of silent
  bugs (attack-watcher trigger scoping, phase-beginning trigger ownership and
  double-dispatch, dead end-step / ETB-counter registration aliases, Impending,
  and a `strategy_memory.pkl` reproducibility leak now scoped per-env). Also
  moved to the cu130 GPU torch wheel and SubprocVecEnv rollouts.
- **7.32–7.36** — hardened the CUDA training pipeline (`training_run.json`
  provenance, spawn workers, atomic final-model publish with reload validation)
  and repaired the failures each strength attempt exposed (deferred-cast timing
  under transient priority, land/spell hand-slot pinning, scry-choice scripted
  fallback, NumPy action-id replay). Analysis showed the early runs were
  structurally worthless — nearly all games hit the turn limit.
- **7.37** — **reward function rebuilt** (change in bounded strategic potential
  plus one perspective-correct terminal reward), and the scripted opponent made
  to actually play lands/spells/combat. Tier 0 draw/opening/play telemetry was
  also repaired. **Because reward and baseline changed materially, do not resume
  any pre-7.37 checkpoint.**
- **7.38–7.43** — more strength runs, each converting a fidelity failure into a
  fix (Bushwhack modal-target validation, Pawpatch recursive trigger, Duress
  deferred-cast phase restore, Floodpits target-required mask). Round 7.43's
  analysis found four systemic reasons games never concluded — cast-time
  targeting over-blocked permanents, the scripted opponent could never tap mana,
  casting needed a learned tap-then-cast dance, and "exile instead" riders were
  dead — all fixed, and **`max_turns` was raised 20 → 30.**
- **7.44** — the corrected observation shape and scalar bounds **changed the
  declared Gym observation space, so checkpoints created before Round 7.44 must
  not be resumed.** Also delivered Spree exclusion→partial handling, Menace
  multi-block, and Mosswood Dreadknight.
- **7.45** — Spree became a real casting transaction (announce / price / target
  / resolve) with Three Steps Ahead covered exactly across all seven mode
  combinations.
- **7.46** — telemetry hardening: a lost or rotated TensorBoard event file can
  no longer take a training run down.

## Tier 4 — Verification & calibration

1. ✅ Golden scenario harness — 315 scenarios and growing; scenario-first is a
   working agreement, not a suggestion.
2. ✅ **Property/invariant harness**: exact non-token zone/stack conservation,
   SBA fixed points, mask-valid action execution/handler coverage, declared
   observation bounds and degradation checks, observation/info mask agreement,
   repeated mask purity, finite rewards, phase-boundary mana clearing, and
   repeated layer idempotence run under fixed seeds in
   `tests/invariant_fuzz_test.py`.
3. ✅ **Long-game fuzzing**: short (3 x 100), default (8 x 1,000), and long
   (32 x 10,000) profiles exist. Failures are written atomically with the seed,
   exact action/context history, state summary, and a one-command `--replay`
   path; successes leave no artifact directory. Weekly/manual CI runs the long
   profile and retains failure artifacts for 14 days. The final local run on
   the post-audit engine snapshot passed all 320,000 actions with no artifact.
4. ▢ **Calibration study**: 3–5 deck pairs with well-known matchup winrates;
   run at harvest scale; compare. This is the acceptance test for the whole
   pipeline and gates "harvest at scale."

## Tier 5 — Harvest operations & deck-builder integration

1. ◐ **Throughput**: isolated parallel workers and aggregate games/second
   telemetry are implemented. The trained-checkpoint 2-game/two-worker smoke
   measured 0.114 games/second; this tiny run validates loading and orchestration,
   not capacity. Profile a production-size checkpoint harvest, then optimize
   measured hot paths.
2. ✅ **Harvest protocol**: `harvest_fixtures.py` supplies strict deterministic
   shards and `harvest_protocol.py` supplies parallel operation, checkpoint
   loading/identity, aggregate success-only manifests, paired-seat scripted
   qualification, candidate scoring, and checkpoint promotion gates. The
   protocol is complete and regression-tested. The 20,480-transition CUDA
   canary and Round 7.67 qualification smoke prove checkpoint loading from
   training through Harvest, but that checkpoint failed the strength gate.
   Operational next step: train a longer strength candidate, pass scripted
   qualification, freeze a baseline checkpoint, then run the first paired-seat
   promotion and calibration study.
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
policy tied to one deck. Each policy may use multiple rollout or Harvest
workers, but its deck corpus, checkpoints, statistics, support observations,
and promotion/calibration artifacts remain in an isolated format namespace.
Statistics from different formats must never be merged merely because a card
or deck name appears in both.

All three format pipelines will share one versioned canonical-card registry
(stable Oracle identity rather than run-local integer IDs), one frozen
observation/action feature schema, and one format-parameterized deck builder.
The registry, format card-pool snapshot, deck-corpus snapshot, and feature
schema each need a recorded version/hash so adding a builder candidate cannot
silently change model input width or invalidate a checkpoint.

What exists today is useful foundation, not three completed format agents:
the three format card-list snapshots exist; deck loading has format-legality
hooks; the current eight-deck sample can bootstrap Standard; training can use
multiple environment workers; and Harvest can run isolated shards and
paired-seat checkpoint promotion. As of Round 7.46, training and Harvest
accept explicit format/corpus configuration, run-level manifests carry
format/pool/corpus/registry/schema lineage, and production Harvest is no
longer hard-coded to the audited eight decks. Round 7.52 added user-deck
ingress: one supplied list is legality-checked against the pinned snapshots,
its matching format(s) are detected, and it is added to that format's isolated
recursive deck pool. This is an input path, not the automatic deck-builder
feedback queue. Still open: the scripted opponent remains the training
baseline, no representative Modern or Pioneer training corpus exists, and
builder candidates do not yet enqueue themselves. A clean failure manifest
also does not prove that an unseen format card was simulated faithfully;
coverage must distinguish unseen, observed-clean, verified, partial, and
excluded cards.

Phased milestones:

1. ✅ **Format foundation and lineage** (Round 7.46, July 2026): training
   (`main.py`), fixture Harvest, and the parallel Harvest/promotion protocol
   all accept explicit `--format`, `--decks`, and `--format-dir`
   configuration with strict legality; `Playersim/card_registry.py` provides
   the canonical card registry (stable, append-only integer indices keyed by
   name + Scryfall oracle_id) and the frozen, versioned feature schema, both
   self-hashed and created by `python -m Playersim.card_registry freeze`;
   every run-level manifest (`training_run.json`, `harvest_run.json`,
   `harvest_protocol.json`, `promotion.json`) stamps a `lineage` object with
   format, pool-snapshot hash, corpus hash, and registry/schema
   version+hash, alongside the existing git/policy/checkpoint identities;
   production Harvest is generalized beyond the hard-coded sample fixture
   while the no-argument fixture remains the regression gate. See
   `STATS_SCHEMA.md` "Format namespaces and run lineage" for the consumer
   contract. `formats/standard/` now covers every one of the 4,702 legal
   English cards in the pinned Standard snapshot plus 28 retained bootstrap
   identities (4,730 registry entries total). The v2 feature schema has 259
   subtypes and feature_dim 436; the original 110 indices remain unchanged.
2. ◐ **Standard end to end**: the representative corpus is pinned, hydrated,
   and can be extended with validated imports. Continue closing impact-ranked
   support gaps, qualify the Standard policy against scripted play, promote it
   into a checkpoint league, calibrate known matchups, and produce the first
   format-isolated, fidelity-clean strength harvest.
3. ▢ **Modern end to end**: assemble a separate strictly legal Modern corpus,
   triage its observed support gaps, then repeat qualification, league
   promotion, calibration, and production harvest in the Modern namespace.
   Do not assume every current sample deck is Modern legal.
4. ▢ **Pioneer end to end**: assemble a separate strictly legal Pioneer
   corpus and pass the same support, strength, calibration, and Harvest gates
   in the Pioneer namespace.
5. ▢ **Unified builder feedback**: make one builder accept a format as an
   explicit input and consume only that format's legal pool, version-matched
   support ledger, fidelity-clean qualified-policy statistics, matchup data,
   and uncertainty/confidence. Builder candidates enter the affected format's
   support preflight and paired-seat evaluation queue; accepted candidates feed
   its training/Harvest corpus without contaminating held-out promotion or
   calibration results.

**Current execution order:** the format foundation, full Standard namespace,
representative metagame corpus, recursive pool layout, and validated user-deck
ingress now exist. Continue the impact-ranked Standard support sweep, then
qualify the Standard policy against scripted play, promote it into a checkpoint
league, and calibrate known matchups. Imported lists can widen the working pool
without overwriting the pinned metagame, but builder-driven queueing remains a
later milestone. Reuse the qualified pipeline for Modern and then Pioneer, and
finally enable the unified automatic builder feedback loop. Production-size
throughput profiling and calibration occur as gates in each format rather than
as one mixed-format exercise. New fidelity failures discovered along that path
pre-empt strength and integration work in the affected format.

**Round history (7.46–7.50, condensed).** Delivered the format foundation and
the first full-format coverage picture:

- **7.46** — the canonical card registry (`Playersim/card_registry.py`: stable,
  append-only integer indices keyed by name + Scryfall oracle_id), the frozen
  self-hashed feature schema, the `freeze` CLI, run-level `lineage` on every
  manifest, and generalized Harvest that accepts `--decks`/`--format`/
  `--format-dir`. Milestone 1 above records the durable contract; see
  `STATS_SCHEMA.md` "Format namespaces and run lineage" for the consumer rule.
- **7.47–7.48** — swept contained v1 limitations: all seven Roles, explicit
  day/night, clone-safe text-delayed triggers and created-object riders, snow
  payment, combat-plus-main phase insertion, and honest extractor naming
  (`FixedWindowMTGExtractor`, with `CompletelyFixedMTGExtractor` kept only as a
  load-compat alias).
- **7.49** — widened the frozen Standard namespace to all 4,702 snapshot cards
  plus 28 bootstrap identities (4,730 entries, feature_dim 436) without
  renumbering the original 110; added page-aligned target-identity observations
  and canonical mechanic-activation slots.
- **7.50** — the first full-format support preflight: every Standard card is
  constructed and probed, and `formats/standard/support_ledger.json` (schema-
  versioned, self-hashed, tied to snapshot/registry/corpus) separates verified,
  corpus-clean, unseen-clean, partial, and unparsed evidence. No card is called
  supported merely for never having produced telemetry.

**Round 7.51 (July 2026)** made the representative Standard corpus executable
and closed its first two impact-ranked mechanic gaps:
* **Deterministic corpus hydration** — `Playersim.deck_corpus` joins the compact
  eight-list metagame corpus against the pinned Standard JSONL snapshot, checks
  exact 60-card counts and missing identities, and atomically writes eight
  full-record deck files under `formats/standard/decks/`. Two tests guard
  deterministic output and fail-closed missing-card behavior.
* **Standard is the strict default** — training, hyperparameter optimization,
  fixture Harvest, scenario real-card discovery, and deck-stat discovery now
  use the hydrated corpus and frozen Standard namespace. The empty
  `DeckLists/` staging directory was removed; the old bootstrap decks were
  archived and their 28 rotated identities were extracted into the explicit
  `historical_bootstrap_cards.json` scenario fixture.
* **Earthbend** — fixed-N Earthbend selects a controlled land, makes it a 0/0
  land creature with haste, adds the printed +1/+1 counters, and returns it
  tapped under the Earthbend controller after death or exile. The previous
  12-slot named Earthbend gap is gone from the regenerated ledger.
* **Flashback** — printed and until-end-of-turn granted costs are payable from
  the graveyard, successful casts exile after resolution, multi-symbol costs
  parse intact, and the first six graveyard objects receive distinct executable
  actions rather than overwriting one singleton slot. The previous 10-slot
  named Flashback gap is gone from the regenerated ledger.
* **Measured change** — the static ledger moved from 3,386 clean/verified cards
  to 3,425 and from 427 unparsed cards to 413. Its current status split is 52
  verified, 78 corpus-clean, 3,295 unseen-clean, 864 partial, and 413 unparsed.
  Gates: 307/307 scenarios, 9/9 smoke, 13/13 training, 15/15 fixture Harvest,
  7/7 protocol, 19/19 registry, 1/1 support-preflight, and 2/2 corpus tests.

**Round 7.52 (July 2026)** added format-aware deck ingress and another exact-
card coverage slice:
* **Automatic format routing** — `python -m Playersim.deck_ingest <list>` reads
  Arena/simple text or JSON, resolves canonical names against the pinned card
  lists, enforces main-deck, sideboard, copy-limit, registry, schema, and format
  legality, reports every match, and selects Standard, then Pioneer, then
  Modern unless `--format` requires one. Dry-run, strict-support, explicit
  replacement, and fail-closed namespace controls are available.
* **Separated recursive pools** — generated metagame decks live under
  `formats/<format>/decks/metagame/`; validated user lists live under
  `formats/<format>/decks/imported/`. Training, Harvest, corpus identity,
  preflight frequency, provenance, and deck statistics discover both through
  stable recursive traversal, while metagame regeneration cannot overwrite an
  import.
* **Eight exact cards gained scenario-backed v1 verification** — Flow State,
  Accumulate Wisdom, and Consult the Star Charts cover conditional Dig counts
  and printed remainder ordering;
  Badgermole Cub covers its creature-mana replacement; Eddymurk Crab covers
  graveyard reduction, off-turn Flash entry, and zero-to-two targets; Spider
  Manifestation covers restricted mana choice and cast-trigger gating; Fabled
  Passage covers its single-shuffle atomic tapped search and four-land untap
  rider; and Beifong's Bounty Hunters covers last-known-power Earthbend X
  within the documented Earthbend v1 semantics.
* **Contained v1 limitations closed** — Dig remainder order now distinguishes
  preserve, policy-selected, and random instructions; begin-game cards receive
  independent accept/decline decisions; and Beifong's dynamic Earthbend value
  comes from the dying creature's last-known power. Printed once-per-turn
  triggers now enforce and reset their shared turn gate. Earthbend's
  choice-free delayed return still resolves immediately instead of using the
  stack.
* **Measured and conservative coverage** — the regenerated 4,702-card ledger
  now records 60 verified, 79 observed-clean, 3,310 unseen-clean, 843 partial,
  and 410 unparsed cards. Versus Round 7.51, static-clean/verified coverage rose
  from 3,425 to 3,449 cards, evidence-qualified coverage rose from 130 to 139,
  and unparsed fell from 413 to 410. Ten formerly clean cards were deliberately
  moved to partial after the audit exposed unimplemented Harmonize/source-
  duration semantics; the ledger no longer hides those gaps.

Gates added for this round: 315/315 scenarios and 13/13 deck-ingest tests.

**Round 7.53 (July 2026)** closed the representative metagame's remaining
severe support gaps and delivered a high-impact fidelity slice:
* **Zero severe representative cards** — Escape Tunnel, Aang, Swift Savior,
  and Cosmogrand Zenith moved out of `unparsed`; the regenerated representative
  corpus now contains no `unparsed` or `crash` entry.
* **Exact high-impact effects** — Escape Tunnel grants power-limited temporary
  unblockability; Aang airbends creatures or spells and grants the owner's
  `{2}` exile cast; Cosmogrand gates on the second spell and exposes its mode;
  Combustion Technique counts Lessons and installs its exile replacement;
  Daydream blinks with a counter; and Sage of the Skies copies its own creature
  spell into a token.
* **Policy-visible searches** — Brightglass Gearhulk and Starfield Shepherd
  now expose their exact restricted library candidates. Optional/up-to search
  can be declined. Starfield remains honestly partial because Warp itself is
  not implemented.
* **Harmonize is executable** — printed Harmonize cards are castable from the
  graveyard, may tap one chosen creature to reduce generic mana by its power,
  and exile after resolving. Winternight Stories also implements its one-
  creature-or-two-cards discard decision.
* **Measured coverage** — the 4,702-card ledger now records 68 verified, 76
  observed-clean, 3,337 unseen-clean, 857 partial, and 364 unparsed cards.
  Static-clean coverage is 74.0323266695%; evidence-qualified coverage is
  3.0625265844%. Warp's 31-card family is explicitly partial, replacing a
  false-clean classification.

Gates for this round: 322/322 scenarios, 9/9 smoke, 19/19 registry,
13/13 deck-ingest, 2/2 deck-corpus, and 1/1 support-preflight tests.

**Round 7.54 (July 2026)** closed every remaining partial in the pinned
representative Standard corpus as one shared-primitives release:
* **Warp end to end** — hand-indexed Warp alternative casts pay the printed
  cost, resolve normally, exile at the next end step through clone-safe delayed
  payloads, and grant a later ordinary cast from exile without changing the
  frozen action or feature dimensions.
* **Linked choices and transactions** — Erode and Lumbering Worldwagon expose
  optional tapped-land searches; Archdruid's Charm chooses a creature or land
  and applies its linked destination; No More Lies exposes payment and exiles
  only a spell countered its way; Deadly Cover-Up purges the chosen name across
  graveyard, hand, and library and replaces exiled hand cards; Strategic
  Betrayal gives the affected opponent its creature choice before exiling the
  graveyard; North Wind Avatar consumes an optional outside-game choice when
  that pool exists.
* **Temporary and zone rules** — Mistrise Village marks and consumes the next
  spell's uncounterability; Day of Black Sun snapshots the X-bounded set,
  removes abilities in layer 6, then destroys that set; finality counters exile
  dying creatures; Esper Origins can resolve from a graveyard through exile to
  its transformed Saga face with a finality counter. Multi-face Card objects
  now initialize from their front-face Scryfall fields instead of empty
  top-level fields.
* **Vehicles** — Crew reuses the power-threshold tapping chooser, taps the
  committed creatures, and registers the Vehicle's layer-4 animation. Lumbering
  Worldwagon also receives its land-count power CDA while crewed.
* **Measured closure** — the regenerated 4,702-card ledger records 68 verified,
  89 observed-clean, 3,360 unseen-clean, 821 partial, and 364 unparsed cards.
  Static-clean coverage is 74.7979583156%; evidence-qualified coverage is
  3.3390046789%. The representative corpus has zero unexplained partial,
  unparsed, or crash cards.

Gates for this round: 326/326 scenarios; the remaining repository gates are
listed in the status snapshot above.

**Round 7.55 (July 2026)** made a broad generic-mechanics pass over the frozen
Standard pool instead of closing cards one at a time:
* **Equipment and Vehicles** — ordinary Equip abilities now resolve through a
  reusable attach effect after the existing target-and-cost transaction, while
  generic Crew text shares the power-threshold creature chooser and layer-4
  Vehicle animation path. The implementation covers both registry ability
  records and generated Crew instructions.
* **Policy-visible keyword actions** — fixed-value Discover reveals through the
  first eligible nonland card, randomizes the rest onto the bottom, and exposes
  cast-without-paying versus hand. Connive draws, exposes the discard, adds the
  nonland counter, supports optional wording, and enforces printed once-per-turn
  gates. Suspect applies menace/can't-block state, supports clearing and an
  optional transfer choice, and cleans state on zone changes.
* **Broader shared effects** — Explore and Investigate recognize ordinary
  pronoun/controller templates, and Airbend accepts the supported range of
  nonland permanent targets while preserving owner-based exile-cast permission.
  Complex mixed clauses remain partial instead of being falsely claimed by a
  broad keyword match.
* **Measured full-pool gain** — 192 cards moved from partial/unparsed to
  unseen-clean with zero clean-card regressions. The 4,702-card ledger now
  records 68 verified, 89 observed-clean, 3,552 unseen-clean, 810 partial, and
  183 unparsed cards. Static-clean coverage rose from 74.7979583156% to
  78.8813270949%; evidence-qualified coverage remains 3.3390046789% until the
  newly generic cards appear in a runtime corpus.

Gates for this round: 329/329 scenarios and 63/63 repository unit tests.

**Round 7.56 (July 2026)** addressed the dynamic keyword-action limitations
left deliberately open by the preceding family sweep:
* **Dynamic Discover** — X can come from a targeted or triggering spell's mana
  value, including X actually paid. Completing the action emits a controller-
  scoped Discover event, so Curator of Sun's Creation repeats the same value and
  its printed once-per-turn gate prevents recursion.
* **Repeated Explore and Investigate** — Explore X preserves its remaining
  iterations through a nonland top/graveyard policy choice. Investigate now
  supports fixed repeats, the two-player hand-size comparison, and counts the
  creatures controlled by selected players.
* **Endure** — fixed and counter-derived values expose the required choice
  between +1/+1 counters and an X/X white Spirit. Nontoken/another creature
  trigger filters now reject token and self entries, and nontoken death watchers
  use last-known token status.
* **Conservative prerequisites** — Descendant of Storms and Krumar Initiate move
  only from unparsed to partial: their Endure result is understood, but optional
  mana payment and activated `{X}` plus X-life payment are not falsely claimed.
  Brass's Tunnel-Grinder likewise improves to partial while its unrelated front-
  face clauses remain open.
* **Measured gain** — ten cards moved to unseen-clean with zero clean-card
  regressions. The ledger now records 68 verified, 89 observed-clean, 3,562
  unseen-clean, 811 partial, and 172 unparsed cards. Static-clean coverage rose
  from 78.8813270949% to 79.0940025521%; evidence-qualified coverage remains
  3.3390046789%.

Gates for this round: 331/331 scenarios and 63/63 repository unit tests.

**Round 7.57 (July 2026)** continued the limitations work with shared payment
and action-exposure infrastructure:
* **Optional resolution payments** — exact “you may pay `{cost}`; if you do”
  instructions now expose pay and decline through the ordinary resolution-
  choice policy. Payment uses the auto-tap planner, and the paid follow-up plus
  any remaining resolving instructions retain their continuation/finalizer.
  Descendant of Storms and Subway Train become fully statically clean through
  this path.
* **Large X pagination** — spell X affordability is no longer truncated at ten.
  All affordable values are retained in the casting choice and exposed ten per
  page through the existing shared page action, preserving the frozen 480-slot
  action schema. X=0 remains the Pass alias, while page-local actions carry the
  exact absolute X value into payment and resolution.
* **Activated X transactions** — activated abilities now stage the same
  paginated announcement before paying anything. The chosen value is shared by
  `{X}` mana and “Pay X life,” committed with tap and other costs, and retained
  in the stack context for the resolving effect. Krumar Initiate is covered end
  to end through Endure X.
* **Conservative improvement** — Digsite Conservator moves from unparsed to
  partial because its optional `{4}` into Discover transaction now works, while
  its independent four-card graveyard targeting remains explicitly open.
* **Measured result** — three cards moved to unseen-clean and one moved from
  unparsed to partial with zero clean-card regressions. The ledger records 68
  verified, 89 observed-clean, 3,565 unseen-clean, 809 partial, and 171 unparsed
  cards. Static-clean coverage is 79.1578051893%; evidence-qualified coverage
  remains 3.3390046789%.

Gates for this round: 333/333 scenarios and 63/63 repository unit tests.

**Round 7.58 (July 2026)** began the severity-ranked Priority 0 integrity pass:
* **Controller-safe permanent transfer** — temporary control now moves the
  permanent's controller-scoped state, returns an explicit success result,
  preserves its original controller for cleanup, and cannot move an object back
  from a non-battlefield zone after it dies.
* **Control-dependent effect rebinding** — live static and text-derived
  replacement effects are rebuilt for the new controller on both the initial
  control change and the end-of-turn return. Additional-mana replacements are
  scenario-verified to stop applying to the old controller immediately.
* **Last-known death attribution** — a creature that dies while stolen is
  credited to its actual controller at last existence. This closes the separate
  control-at-death limitation; only the broader repeated-ID object-model issue
  remains.
* **Generic as-enters transaction** — first-entry parsing now handles creature
  type, color, card type, opponent, and counter choices before deferred ETB,
  Landfall, and Saga events fire. Choices are retained in both typed stores and
  a generic per-permanent record, and all variants use the ordinary action mask
  and scripted-opponent path. Arbitrary card-specific consumers remain bounded
  coverage work rather than a simulation-integrity gap.

Gates for this round: 335/335 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.59 (July 2026)** continued the Priority 0 layer-integrity pass:
* **Specific ability dependencies** — every parsed static layer effect now
  carries source-ability identity. A specific `remove_ability` dependency can
  suppress the matching generated effect without erasing unrelated abilities
  from the same source; exact-text inference preserves older registrations.
* **CR 305.7 basic land types** — setting a basic land type now removes old land
  subtypes and rules-text abilities, supplies the intrinsic basic mana output,
  suppresses registered activations and triggers, and restores printed state
  when the effect ends.
* **Blood Moon/Urborg ordering** — basic-land-type setting participates in
  within-layer dependency sorting, so it can remove the source ability of an
  earlier type-changing effect before that effect applies.
* **Dynamic nonbasic-land scope** — global nonbasic-land effects recompute their
  battlefield membership each pass, including lands that enter after the
  effect began. Arbitrary changing applicability sets outside the structured
  dynamic-scope vocabulary remain the narrowed Priority 0 limitation.

Gates for this round: 337/337 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.60 (July 2026)** completed the broad Priority 0 closure sweep:
* **True runtime card identity** — canonical registry IDs remain the stable
  deck/statistics namespace, while reset materializes every repeated physical
  card as a distinct runtime ID, mutable `Card` object, and explicit owner.
  Targeting, counters, linked exile, control changes, and lookahead now address
  copies independently; telemetry canonicalizes runtime plays at its boundary.
* **Clone-safe delayed execution** — accepted structured triggers and legacy
  function/method callbacks now survive lookahead cloning with captured game,
  player, subsystem, closure, and default-argument references rebound to the
  branch. Opaque callable objects are rejected at registration instead of
  being accepted and silently dropped.
* **Announcement-time counter divisions** — spell and activated-ability
  divisions are chosen after targets but before costs are paid or the object
  reaches the stack. The locked allocation survives in stack context; an
  illegal target loses its share rather than redistributing it at resolution.
* **Snow provenance and condition integrity** — ordinary, conditional, and
  phase-restricted pools preserve and consume snow provenance. Common turn,
  attack, death, life-change, control, and hand-comparison conditions are
  explicit; unknown conditions now fail closed and raise fidelity telemetry.
* **Live arbitrary layer scopes** — every layer handler resolves affected sets
  from current calculated characteristics. Clone-safe declarative boolean
  predicates (`all`/`any`/`not` plus characteristic comparisons) can gain or
  lose members after an earlier effect in the same layer.
* **Branch-local mutable identity** — every reachable card object and merged,
  melded, or specialized identity ledger is isolated before clone
  construction, preventing clone initialization and layer write-back from
  mutating the source branch.
* **Merged-object ownership and blink** — Mutate records each physical
  component's owner and separates into the correct private zones. Meld rejects
  non-owned components, and blinking a melded permanent returns both front
  faces separately through the same transaction.

Gates for this round: 346/346 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.61 (July 2026)** completed the broad Priority 1 decision sweep:
* **Paginated overflow actions** — action 479 now opens an observational,
  clone-safe catalog for legal hand objects beyond the ten fixed slots and
  activated abilities beyond the first three per permanent or first twenty
  permanents. The selected entry revalidates through its ordinary public
  handler; split second admits only overflow mana abilities.
* **Explicit Ward decisions** — mana, life, sacrifice, and discard Ward costs
  expose pay-or-decline choices, including paginated physical-card options.
  Paused resolution resumes against the identified live stack object, so Ward
  choices remain correct after lookahead cloning.
* **Copy retarget transactions** — each inherited target of a multi-target
  spell copy can be kept or changed independently without mutating the
  original spell. Target legality is recomputed for each selected slot.
* **Simultaneous and non-agent choices** — each-player discards collect both
  players' hidden selections before committing all zone moves together.
  Combat damage assignment order is exposed to either attacking seat rather
  than silently using a scripted order.
* **Structured mana output** — nonland mana abilities now support independent
  per-mana color allocation, fixed multi-symbol output alternatives, and
  colorless-versus-any-color choices after activation costs are committed.
* **Richer sacrifice predicates** — compound nonland/token/tap/type/subtype,
  color, keyword, source-excluding `another`, and numeric characteristic
  criteria filter the exact choice set before the policy acts.
* **Clone stack-controller integrity** — cloned stack tuples and their common
  context references are rebound to the cloned seats. Resolving a cloned spell
  can no longer move it into a detached deep-copied player dictionary.

Gates for this round: 355/355 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, and 13/13 training stages.

**Round 7.62 (July 2026)** retired the remaining Priority 1 limitations:
* **Unified overflow catalog** — action 479 now preserves colliding fixed
  actions and exposes graveyard permissions, Class/leveler actions, additional
  hand objects, and additional activated abilities through one paginated,
  revalidated protocol.
* **Exact policy-owned costs** — sacrifice predicates use a shared structured
  characteristic matcher. Non-self sacrifice and nonrandom discard activation
  costs require explicit staged policy selections; direct callers can no
  longer silently choose a payment.
* **Resource-derived X choices** — spell and activated-ability X ranges derive
  from the live resources that constrain them instead of a numeric ceiling.
  Parsed mana, life, sacrifice, discard, and source-counter X costs all use the
  same paginated announcement transaction. Widening the declared X/count
  observation bounds establishes a new checkpoint boundary.
* **Optional-action policy coverage** — Class and creature level-up remains a
  genuine activate-or-pass decision even beyond fixed source slots. The
  scripted baseline evaluates optional opening-hand placements instead of
  accepting every one.
* **Arbitrary keyword menus** — keyword-grant choices accept and paginate any
  parsed option count, retain the effect controller as chooser, and preserve
  subtype-qualified targeting text.

Gates for this round: 361/361 scenarios, 63/63 repository unit tests, 9/9 smoke
stages, 13/13 training stages, and the deterministic 8-seed / 8,000-action
default invariant fuzz profile. The strict 32-seed / 320,000-action profile
also completed without a failure artifact.

**Round 7.63 (July 2026)** completed a production-readiness integrity sweep:
* **Mana payment integrity** — hybrid, snow, Phyrexian, generic, restricted,
  and conditional mana now share exact affordability, auto-tap, payment, and
  refund accounting instead of double-counting or losing provenance.
* **Target and response contracts** — independent instruction target slots,
  conditional target branches, response catalog entries, prevention text,
  stack-spell/permanent target unions, and resolution-time revalidation now
  preserve mask-to-execution parity.
* **Choice and spell lifecycle integrity** — optional discard is one gated
  continuation; nested choices normalize their resume phase; Scry pauses later
  instructions and finalization; creature triggers are no longer mistaken for
  spell modes; stack spells return to hand as one physical object.
* **Schema and numeric stability** — Windows-spawn workers receive the exact
  frozen subtype schema, while symbolic/missing card values remain finite
  throughout observations, deck statistics, evaluation, threats, and search.
* **Continuous-effect coverage** — subtype-count characteristic-defining
  abilities and the newly observed mass-damage/counter scopes use their live
  game objects rather than phantom targets or placeholder printed values.

Gates for this round: 363/363 scenarios, 63/63 established repository tests,
40/40 focused regression tests, 9/9 smoke stages, 13/13 training stages, and
the deterministic 8-seed / 8,000-action default invariant fuzz profile. The
six-worker Windows-spawn canary `ALPHA_ZERO_MTG_V3.00_20260712_185338`
completed 12,288 rollout steps, saved and reloaded its model, then passed 256
fresh-environment validation steps with finite rewards, valid masks, public
progress, and no short cycle.

**Round 7.64 (July 2026)** reconciled runtime warnings with support evidence
and closed the former Priority 2 ledger-mismatch limitation:
* **Faithful warning-card execution** — Opt now resolves Scry before Draw on
  the real spell path; Deceit exposes and pays Evoke, gates its colored ETBs by
  actual mana spent, preserves optional bounce targeting, and provides its
  nonland hand choice; Quantum Riddler replaces a draw batch exactly once.
* **Permanent mechanic coverage** — Landfall is controller/type gated for
  Earthbender Ascension, Mightform Harmonizer, and Sazh's Chocobo. Earthbender's
  threshold reflexive rider, Mightform's live-power doubling, Leatherhead's
  keyword counter and remove-counter reflexive trigger, and Colorstorm's Opus
  mana threshold all execute through ordinary targets and choices.
* **Mind Swap lifecycle** — Superior Spider-Man chooses from either graveyard
  before entry, applies its exact copy exceptions, queues a non-target bound
  exile trigger, remains correct through MCTS cloning and out-and-back zone
  moves, paginates, and restores its printed identity on leave.
* **Evidence integrity** — unclassified permanent clauses now advance fidelity
  counters and the support manifest. The regenerated 4,702-card ledger records
  77 verified, 80 observed-clean, 3,278 unseen-clean, 775 partial, and 492
  unparsed cards. The static-clean fraction is 73.0540195661% and the
  evidence-qualified fraction is 3.3390046789%; the increased unparsed count is
  the intended correction of previously false-clean static evidence.

Gates for this round: 363/363 scenarios, 130/130 discovered unit tests, 9/9
smoke stages, 13/13 training-stack stages, and the deterministic 8-seed /
8,000-action default invariant fuzz profile.

**Round 7.65 (July 2026)** converted the 19:51 training canary's warning set
into executable rules and quieter diagnostics:
* **Prepared end to end** — Prepare-layout cards are creatures outside their
  virtual spell copy; entering prepared creates one castable exile copy, action
  451 pays the spell face's normal cost and obeys its timing and targets,
  casting unprepares the source, and resolution never moves the permanent.
  Emeritus of Ideation can select exactly eight graveyard occurrences
  atomically to re-prepare; entry, staged payment, copied-spell stack state,
  and departure all survive or clean up across MCTS cloning.
* **Canary card rules** — Gran-Gran proves its three-Lesson/noncreature
  reduction; Sunderflock prices from the greatest controlled Elemental, carries
  cast provenance into its ETB, and bounces only non-Elementals; Icetill
  Explorer grants stacked land plays and deep graveyard-land actions through
  the overflow catalog, with observation/planner parity and source-leave
  revocation; the Jennifer Walters spell lock is enforced in masks and cast
  execution.
* **Target and entry transactions** — Aura casts derive their mandatory target
  from `Enchant` text independently of later ETB targets, fixing Meltstrider's
  Resolve. Target-opponent reveal, empty sweepers, and already-departed bounce
  targets no longer fall into generic warning paths. Multiversal Passage now
  chooses and applies a basic land type, then independently pays 2 life or
  enters tapped; life-loss triggers, departure cleanup, and the mid-choice
  clone are guarded.
* **Operational signal** — verified rule declarations no longer register dead
  layer abilities (including Warp, Eddymurk, uncounterable, Prepared, and
  chosen-type text). Quiet subprocess workers stop writing a duplicate
  `mtg_debug_*` copy of their warning/error stream; explicit debug mode still
  restores the full worker debug file.
* **Measured evidence** — Emeritus of Ideation, Icetill Explorer, Multiversal
  Passage, and Sunderflock are runtime-verified. The regenerated 4,702-card
  ledger records 81 verified, 77 observed-clean, 3,278 unseen-clean, 774
  partial, and 492 unparsed cards: 73.0752871119% static-clean and
  3.3602722246% evidence-qualified.

Gates for this round: 364/364 scenarios, 144/144 discovered unit tests, 9/9
smoke stages, 13/13 training-stack stages, the new 4/4 Aura-warning, 7/7
canary-rules, and 3/3 Prepared suites, plus the deterministic 8-seed /
8,000-action default invariant fuzz profile.

**Round 7.66 (July 2026)** closed the 20:39 training warnings, then used
successive deterministic canaries to expose and remove the underlying target
alias rather than suppressing its diagnostics:
* **Canary card fidelity** — Hearth Elemental counts the instant/sorcery/
  Adventure graveyard union exactly once per card and never discounts Stoke
  Genius; Momo reduces only the first eligible non-Lemur flying creature spell
  on each of its controller's turns; and Doomsday Excruciator's cast-only ETB
  preserves each library's bottom six while exiling the rest face down.
* **Targets remain transactions** — mandatory empty target payloads fail unless
  resolution validation proves a committed target became illegal. Optional
  fight/bounce and empty mass sets remain legal no-ops. Cast-trigger event
  targets are deep-copied and separated from the triggered ability's targets,
  so Namor can no longer clear Spell Snare or Bounce Off's physical stack
  context. Bushwhack now announces independent friendly-fighter and opposing-
  creature roles, including copied-spell retargeting.
* **Hidden exile stays hidden** — face-down exile identities are removed from
  card features, deck composition, opponent-hand inference, candidate ranking,
  restricted targeting, and raw target IDs. Per-owner occurrence counts survive
  cloning and keep repeated hidden IDs masked until every occurrence leaves.
* **Measured evidence** — Doomsday Excruciator, Hearth Elemental // Stoke
  Genius, and Momo, Friendly Flier are promoted through focused real-card
  execution. The regenerated 4,702-card ledger records 84 verified, 74
  observed-clean, 3,278 unseen-clean, 774 partial, and 492 unparsed cards:
  73.0752871119% static-clean and 3.3602722246% evidence-qualified.
* **Operational signal** — the final six-worker, 3,072-step deterministic
  canary (`ALPHA_ZERO_MTG_V3.00_20260712_213007`) completed, reloaded its
  checkpoint successfully, and produced zero warning/error records. Quiet
  workers produced no duplicate warning or debug files.

Gates for this round: 364/364 scenarios, 174/174 discovered unit tests, 9/9
smoke stages, 13/13 training-stack stages, the new 10/10 target-lifecycle, 4/4
Hearth, 8/8 Doomsday, 4/4 Momo, and 4/4 Bushwhack suites, plus the
deterministic 8-seed / 8,000-action default invariant fuzz profile.

**Round 7.67 (July 2026)** closed the representative Standard corpus and made
policy strength a concrete, auditable gate:
* **Esper Origins fidelity** — a graveyard cast now resolves Surveil 2 and the
  life gain before performing the exile-to-transformed-battlefield transaction
  with a finality counter. Summon: Esper Maduin registers its exact three Saga
  chapters: permanent reveal-to-hand, `{G}{G}`, and the other-creature +2/+2
  and trample snapshot through end of turn. Chapter III completion routes the
  Saga through finality exile and removes stale lore state. Normal hand casts,
  nonpermanent reveals, parser surfaces, and the full Flashback lifecycle have
  focused real-card guards.
* **Scripted qualification gate** — `harvest_protocol.py qualify` runs equal
  candidate-as-P1 and candidate-as-P2 legs, counts draws as half a point, uses
  a 55% default threshold, and requires zero fidelity counters and no severe
  support entries. The atomic `qualification.json` records checkpoint identity,
  corpus lineage, outcomes, score, threshold, and decision; invalid protocols
  publish no decision. Worker-stamped identities, post-worker hashes, persisted
  record seats, and the candidate's seat-relative results are all validated.
* **Measured evidence** — Esper Origins is promoted with four representative-
  corpus copies and zero issues. The regenerated 4,702-card ledger records 85
  verified, 74 observed-clean, 3,278 unseen-clean, 773 partial, and 492
  unparsed cards: 73.0965546576% static-clean and 3.3815397703% evidence-
  qualified. No representative-corpus card is partial, unparsed, or crash.
* **Operational proof** — the latest 113,437,514-byte checkpoint completed the
  two-game paired-seat qualification smoke with clean fidelity and stable,
  worker-validated identity evidence. It lost both games, so the new gate
  correctly published a
  valid rejection rather than treating pipeline health as proof of strength.

Gates for this round: 364/364 scenarios, 188/188 discovered unit tests, 9/9
smoke stages, 13/13 training-stack stages, 16/16 fixture-Harvest tests, 16/16
production-protocol tests, 4/4 Esper Origins tests, 19/19 card-registry tests,
1/1 support-preflight test, 2/2 deck-corpus tests, 13/13 deck-ingest tests,
6/6 fuzz/replay tests, and the deterministic 8-seed / 8,000-action default
invariant fuzz profile.

**Round 7.68 (July 2026)** converted the 22:25 training failure and warning
set into permanent lifecycle and real-card guards:
* **Nested trigger choices remain transactional** — choosing Cosmogrand
  Zenith's modal trigger while ordering simultaneous triggers now preserves
  the parent `order_triggers` context. Both explicitly choosing the modal
  trigger early and auto-stacking it last resume the remaining order, finish
  any waiting APNAP batch, and put every trigger on the stack exactly once.
  Standalone modal choices normalize an invalid `CHOOSE` resume back to
  priority without losing the underlying turn phase.
* **Orphan wrappers cannot trap a policy** — action generation and opponent
  routing fall through to the live priority player when a transient
  `CHOOSE` phase has no context, instead of presenting the learned seat with
  an inert NO_OP forever. Strict-cycle diagnostics now always include recent
  actions and compact stack provenance.
* **M.O.D.O.K. is runtime-verified** — ability-word punctuation no longer
  hides its Pay 3 life activation; the dotted-name Connive instruction and
  own-turn restriction are enforced by mask, handler, and direct execution.
  Resolution draws, discards, and adds the nonland-discard counter, while
  Designed Only for Killing remains an opponent-only -1/-1 effect. Legal
  empty nontargeted group-counter instructions now resolve silently; missing
  mandatory targets still fail and warn.
* **Measured evidence** — the regenerated 4,702-card ledger records 86
  verified, 73 observed-clean, 3,327 unseen-clean, 788 partial, and 428
  unparsed cards: 74.1386643981% static-clean and 3.3815397703% evidence-
  qualified. All 120 distinct cards in the representative corpus remain
  verified or observed-clean; none is partial, unparsed, or crash.
* **Exact-seed operational proof** — the six-worker canary
  (`ALPHA_ZERO_MTG_V3.00_20260712_225422`) reused failing seed `20260713`,
  completed all 3,072 requested timesteps (512 per environment), and passed
  post-save checkpoint reload validation. Its mean episode length reached
  451, beyond the original failure at episode step 435, and it created no new
  warning or error log.

Gates for this round: 364/364 scenarios, 195/195 discovered unit tests, 9/9
smoke stages, 13/13 training-stack stages, the 7/7 choice-context and 3/3
M.O.D.O.K. warning-regression suites, and the deterministic exact-seed
six-worker training canary above.

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
the risk of corrupting rules outcomes or training statistics first, then missing
player decisions, bounded card/mechanic coverage, and operational constraints.
The detailed notes afterward are retained in their historical source order and
do not override this ranking.

### Priority 0 — simulation integrity

✅ No known Priority 0 simulation-integrity limitations remain.

Round 7.60 retired true per-copy runtime identity, dynamic layer applicability,
delayed-callback cloning, announcement-time counter divisions, restricted snow
provenance, condition fail-closed behavior, Mutate ownership/clone isolation,
Meld blink/clone isolation, and Specialize clone isolation. Missing Meld or
Specialize family data and bounded condition/mechanic vocabularies are honestly
classified as Priority 2 coverage; they fail closed rather than silently
diverge.

### Priority 1 — decision and action completeness

✅ No known Priority 1 decision/action-completeness limitations remain.

Round 7.62 retired the remaining overflow/collision paths, silent sacrifice
fallback, capped and partially staged X decisions, level-up source bounds,
unconditional scripted opening-hand acceptance, and two-option keyword menus.
Structured sacrifice and X-cost parsers remain intentionally bounded: unknown
Oracle families fail closed and belong to Priority 2 card-fidelity coverage,
not hidden policy decisions.

### Priority 2 — bounded mechanic and card fidelity

13. Generic `as enters` transactions are verified for creature type, color,
    card type, basic land type, opponent, counters, chained life payment, and
    deferred ETB events; arbitrary effects consuming those chosen values remain
    card-specific.
14. Emblem execution is implemented only for the currently recognized Kaito
    and Wrenn texts.
15. MDFC support still lacks direct nonland back-face entry and complete
    back-face targeting text. Prepare layouts are now classified separately and
    their virtual spell-copy lifecycle is verified.
16. Adventure-half parsing and targeting remain heuristic.
17. Generic Discover, Explore, Investigate, Endure, Connive, Suspect, Airbend,
    Equip, Crew, and the individual effects printed on Prepare spell faces
    intentionally cover bounded text families.
18. Reflexive triggers recognize a bounded set of exact rider templates.
19. Earthbend has bounded dynamic-X parsing and resolves its choice-free delayed
    return immediately after the initial zone move.
20. Uncommon linked optional-search templates still need exact transaction
    handlers.
21. Warp source-duration permissions remain conservatively partial.
22. Target-conditioned pricing recognizes only the implemented condition
    vocabulary.
23. Numeric die support omits modifiers, rerolls, ignored rolls, and generic
    result-value clauses.
24. Attack-trigger adjective scopes and defender gating remain bounded to the
    supported two-player vocabulary.
25. Rare phase-beginning scopes can pass ungated.
26. Screaming Nemesis relies on standard life-gain entry points and a single
    committed reflected-damage target.
27. ConditionalExileEffect is a single-target implementation.
28. Obliterator uses a conservative payer fallback when the damage source has
    left play.
29. Cavern of Souls offers only the top ten locally derived creature subtypes.
30. Obstinate Baloth conservatively leaves an undetermined-cause discard in the
    graveyard.
31. Meld requires its result printing to be present in the local card database;
    missing `all_parts` data fails closed instead of fetching at runtime.
32. Specialize requires all five local variant printings; incomplete families
    are fidelity-marked unparsed and excluded from supported play.
33. Trigger-condition parsing now fails closed but does not yet express every
    Oracle condition template. Structured sacrifice predicates and activation-
    cost parsing likewise cover the documented Oracle families rather than
    arbitrary future wording; unsupported families remain bounded coverage and
    fail closed.
34. Mutate still lacks per-component replacement choices and library-order
    choice; commander-specific routing is outside the current formats.
35. Face-down exile is hidden across observation, inference, and targeting,
    with per-owner counts for repeated hidden IDs. A legacy state containing a
    visible and a hidden exile occurrence that share one numeric ID cannot
    distinguish those occurrences perfectly; production runtime objects have
    unique IDs, and legacy ambiguity fails closed by hiding both.
The former runtime/ledger mismatch item is retired in Round 7.64. Opt,
Deceit, Leatherhead, Colorstorm Stallion, Superior Spider-Man, Earthbender
Ascension, Mightform Harmonizer, Sazh's Chocobo, and Quantum Riddler are now
focused-runtime verified, while future unclassified clauses become explicit
ledger issues instead of false-clean evidence.
The former representative-corpus Esper Origins partial is retired in Round
7.67; its exact transform-Saga path is focused-runtime verified. This does not
generalize Saga chapter support to unrelated cards without their own evidence.

### Priority 3 — operational and lineage constraints

36. Strategy memory is per environment and its optional enhancement pass makes
    saved memory content nondeterministic.
37. Format registries and schemas are intentionally lineage-bound, name plus
    Oracle-ID based, and best-of-one only; schema growth can require a new
    policy lineage. Round 7.62's widened X/count observation bounds likewise
    require a fresh policy rather than resuming an older checkpoint.
38. Treasure/Beza support is scenario-verified; its retained note documents the
    exact supported path rather than an active correctness gap.

### Detailed notes (historical source order)

- Emblem execution currently recognizes the Kaito Ninja anthem and Wrenn
  graveyard-permission texts used by the sample decks. Other emblem text is
  retained as a command-zone record but needs an effect implementation before
  its card can be considered supported.
- Specific `remove_ability` existence dependencies and CR 305.7 basic-land-type
  ability loss are scenario-verified, including Blood Moon/Urborg ordering,
  late-entering nonbasic lands, intrinsic mana, and state restoration (Round
  7.59). Round 7.60 routes every layer handler through live membership and adds
  clone-safe boolean characteristic predicates, including applicability changes
  caused by an earlier effect in the same layer. Oracle text still needs to be
  parsed into that declarative vocabulary as bounded card coverage.
- MDFC back-face casting — v1 support added (July 2026): is_mdfc() no longer
  requires "//" in the text (two non-transform faces suffice), Card exposes
  get_face_cost/get_face_text/get_face_type_line per face, and cast_spell uses
  the back face's cost + text when cast_as_back_face is set (the spell path
  previously always used the front cost). Remaining: MDFC back-face for
  non-land permanents entering directly, and back-face targeting text.
- Level-up creatures — v1 support added (July 2026): Card parses the
  "LEVEL N-M / N+" band format (distinct from Class enchantments) into
  is_leveler / leveler_bands / level_up_cost, with get_leveler_pt(counters)
  and get_leveler_abilities(counters). The action space exposes
  LEVEL_UP_CREATURE, pays the printed cost, adds level counters, and the layer
  system applies current-band P/T and abilities. Round 7.62 preserves the
  activate-or-pass policy decision and routes sources beyond the five fixed
  slots through the overflow catalog.
- Adventure cards — v1 support added (July 2026): casting the Adventure half
  resolves to exile instead of graveyard, marks the creature side castable from
  exile, exposes CAST_FROM_EXILE, consumes that permission on cast, and resolves
  the creature side to the battlefield. Remaining: adventure-half parsing and
  targeting are still heuristic.
- The fixed 480-action tensor retains compact fast-path slots, while Round
  7.62's action-479 catalog exposes additional hand, graveyard, activated,
  level-up, and colliding singleton-mechanic contexts. Catalog selections carry
  their source context and revalidate through the ordinary handler. Indices
  205–223 retain dormant labels for mechanics that have no implementation and
  are deliberately never mask-valid; those are bounded fidelity coverage, not
  omitted legal choices for supported mechanics.
- Canonical registry IDs are printing/statistics identities; deck entries
  materialize as distinct runtime IDs with separate mutable
  `Card` objects and explicit owners (Round 7.60). Linked exile and all live
  object state key by runtime ID, while play telemetry canonicalizes back to the
  registry ID before persistence.
- Structured oracle-text delayed triggers and legacy function/method callbacks
  are clone-safe. Captured game/player/subsystem references, closure cells, and
  default arguments rebind to the branch. Unsupported opaque callable objects
  are rejected by `register_delayed_trigger` rather than accepted and lost.
- Opponent trigger ordering routes through the installed policy; blocker damage
  ordering still uses the scripted/automatic path pending the next choice audit.
- Snow provenance is tracked and consumed for ordinary, restricted,
  phase-restricted, and atomic-source payment. `_evaluate_condition` covers the
  common turn, attack, death, life-change, hand-comparison, and control
  predicates; unknown Oracle conditions fail closed and raise fidelity
  telemetry until their bounded parser coverage is added.
- Reflexive-trigger v1 recognizes exact "When you do" / "When that player
  does" riders when the prerequisite itself parses; generic optional sacrifice
  now exposes both the permanent choice and decline path.
- Counter divisions are policy-selected after targets and before costs during
  casting or activation, then locked in stack context under CR 601.2d. Illegal
  targets lose their announced shares at resolution. Dig selects the kept card
  explicitly and follows each parsed instruction's preserve, policy-selected,
  or random remainder order.
- Earthbend v1 supports fixed numeric values, Beifong's last-known-power X
  expression, and the correct death/exile return destination. The choice-free
  delayed return currently resolves immediately after the initial zone move
  rather than entering the stack; dynamic X expressions beyond the supported
  last-known-power pattern still need dedicated parsing.
- Nonland mana abilities expose simple alternatives, multi-symbol packages,
  colorless alternatives, and independent per-mana choices through structured
  production transactions (Round 7.61).
- Optional "its controller may search" land-search riders preserve the
  pre-removal battlefield controller and expose decline when supported by the
  linked effect. Other uncommon linked-search templates still require exact
  transaction handlers.
- Warp's cast, next-end-step exile, and later exile-cast transaction is
  implemented. Source-duration wording such as "for as long as you control" is
  still
  conservatively partial where the permission or restriction outlives the
  resolving instruction.
- Round 7.55's generic family support is deliberately bounded. Round 7.56 added
  spell-mana-value and repeated-same-value Discover, Explore X, hand-comparison
  and selected-player creature-count Investigate, plus fixed/counter-derived
  Endure. Connive covers the ordinary one-card action and simple
  optional/once-per-turn templates; Suspect covers direct, clear-all, attached,
  and transfer forms; and Airbend covers nonland permanents plus the existing
  creature/spell path. Other dynamic count expressions, compound keyword
  clauses whose
  other instructions do not parse, unusual multi-object Suspect wording, and
  broader exile-cast cost modifiers remain conservatively partial. Generic
  Equip and Crew are executable, but cards with additional unsupported text
  remain partial on that independent text.
- Spell-copy retargeting exposes each inherited target slot independently, so
  a policy may keep or change any legal subset without mutating the original
  spell (Round 7.61).
- X choices paginate beyond ten within the fixed action range and derive their
  finite range from live mana, life, sacrifice, discard, or source-counter
  resources. Unknown cost wording is bounded parser coverage and must fail
  closed rather than inventing an arbitrary ceiling or payment.
- Simultaneous each-player discards stage both players' hidden selections and
  commit all zone moves together. The installed policy supplies non-agent
  choices (Round 7.61).
- Target, Dig, counter-distribution, SacrificeEffect, and activated-cost
  sacrifice choices paginate beyond ten. Non-self activation payments require
  explicit staged physical-card selections even for direct callers. The shared
  predicate covers type/subtype/supertype, token, tap/combat state, color,
  keyword, counters, source exclusion, names, and numeric characteristics;
  unfamiliar Oracle criteria fail closed and remain harvest-fidelity work.
- Round 7.16 target-conditioned pricing recognizes the sample cards' two exact
  conditions: a tapped permanent and a permanent you control. Arbitrary Oracle
  conditions that refer to target characteristics still need dedicated parsers.
- Meld v1 requires the meld-result card object to be present in `card_db`; the
  deck loader does not fetch a missing `all_parts` URI, so absent result data
  fails closed. Ownership is validated, branch identity is isolated, and blink
  returns both front-face components as separate objects (Round 7.60).
- Numeric die v1 supports ordinary result tables and emits die-roll events.
  Roll modifiers, rerolls/ignored rolls, and tableless "equal to the result"
  clauses remain future work. The nonnumeric planar die is intentionally out
  of scope with Planechase.
- Specialize v1 requires all five `all_parts` variant card objects in `card_db`;
  missing families are fidelity-marked `unparsed`. Lookahead specialization is
  branch-isolated and simultaneous copies specialize independently.
- Mutate records an explicit owner for every physical component and routes each
  one to its owner's private zone on separation. Ordered components, top/bottom
  identity, triggers, illegal-target fallback, token cessation, and clone
  isolation are covered. Per-component replacement choices, library ordering,
  and commander routing remain bounded mechanic/decision work.
- Ward target-tax snapshots obligations when targets are committed and exposes
  explicit pay-or-decline decisions for parsed mana, life, sacrifice, and
  discard costs, including paginated physical-card choices (Round 7.61).
- Attack triggers fire at declare-attackers-done through one ATTACKS dispatch:
  the attacker's own abilities plus "whenever a/another <type> [you control]
  attacks" and "attacks you" watchers on other permanents, scoped by controller
  and printed type (July 2026). Token/nontoken scopes are supported; other
  adjectives outside the card's type/subtype/supertype vocabulary remain
  conservative. Defender-side gating assumes two-player "attacks you".
- Opening-hand placement gives each eligible card an independent accept or
  decline decision. Round 7.62's scripted baseline rejects explicit downside
  placements and can use accumulated opening-hand performance before falling
  back to card evaluation. Both Leyline of Resonance's begin-game line and its
  copy trigger on a spell targeting exactly one friendly creature are
  scenario-verified.
- Screaming Nemesis v1: the reflected damage picks the first committed target,
  and the rest-of-game restriction is a player flag consulted by gain_life and
  lifelink. Effects that add life directly without those entry points would
  bypass it (the same pre-existing caveat as all life-gain replacements).
- ConditionalExileEffect (Anoint) is single-target v1 and reads the corrupted
  threshold from the target controller's poison_counters at resolution.
- When an Obliterator damage source left play before resolution, the forced-
  sacrifice payer falls back to the opponent of the trigger's controller.
- Cavern of Souls v1 stores the chosen type per runtime object per player,
  offers the top-10 creature subtypes from the controller's own cards as
  options, and applies the uncounterable rider when
  any of its restricted mana was spent on the cast. Counterspells can still
  TARGET the spell; they fizzle at resolution.
- Treasure tokens carry their printed sacrifice-for-mana text; Beza's token is
  scenario-verified through registration, activation costs, color choice,
  mana production, and the CR 605 no-stack path.
- Phase-beginning trigger owner gating (July 2026) covers "on your turn"
  (combat), your-upkeep/end-step/draw/precombat-main, and "an opponent's
  upkeep/end step" wordings. Rarer phase scopes ("each player's upkeep on
  their turn", named-player phases) still pass ungated.
- Obstinate Baloth v1 identifies an opponent-caused discard by finding the
  causing source on the stack (falling back to the source's current zone
  controller); an undeterminable cause conservatively keeps the graveyard
  destination.
- Keyword-grant choices are made by the effect's controller, accept arbitrary
  parsed option counts with pagination, and preserve subtype-qualified target
  text through selection and application (Round 7.62).
- `creatures_died_this_turn` now attributes deaths from the battlefield
  object's last-known controller, including a permanent under temporary
  control (Round 7.58). Round 7.60's runtime IDs remove the former repeated-copy
  ambiguity.
- ENTERS_BATTLEFIELD-registered replacements are now applied through the
  ENTER_BATTLEFIELD alias merge (July 2026). The revived generic "as enters"
  transaction is scenario-verified for creature-type, color, card-type,
  opponent, counter, and deferred-trigger paths (Round 7.58). Arbitrary
  card-specific effects that consume those chosen values remain bounded
  coverage work; Cavern of Souls keeps its dedicated mana consumer.
- Text-derived replacement registration is idempotent per card per game and
  cleared by `remove_effects_by_source` for phasing rebuilds. Round 7.58 also
  rebuilds controller-bound static and replacement effects after pure control
  changes and their end-of-turn reversion.
- strategy_memory persistence is per-env (under the env's storage
  directory). Cross-env sharing within one training run no longer happens
  implicitly; save_memory also has a 20% random "enhancement" pass, so
  memory file contents are not bit-deterministic (game RNG unaffected).
- Format-foundation v2 (Round 7.49): the frozen `formats/standard/` registry
  covers all 4,702 legal English cards in the pinned Standard pool snapshot
  plus 28 historical bootstrap identities. `freeze-pool` grows the registry
  append-only and rebuilds the feature schema for a new policy lineage;
  the original 110 indices remain stable. Canonical indices are name-sorted
  at first freeze and
  differ from legacy insertion-order IDs, so format-namespace runs are a
  new stats lineage and must not be mixed with pre-7.46 artifacts. Lineage
  lives in run-level manifests, not per-game `game_log.jsonl` lines.
  Registry identity matches by card name + oracle_id; per-printing
  distinctions (set/collector number) are deliberately out of scope. A
  frozen-schema subtype vocabulary that must grow requires a new schema
  version and therefore a new policy lineage; ordinary `freeze --extend`
  still accepts only width-preserving additions.
  Round 7.52's deck importer can bootstrap a wholly missing Standard, Pioneer,
  or Modern namespace from its pinned snapshot, but refuses a half-present
  registry/schema pair. Imported decks are hydrated into the detected format's
  isolated recursive pool; sideboards are retained and legality-checked but are
  not played by the current best-of-one runtime.

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

Each has a permanent guard scenario. Keep writing them first.
