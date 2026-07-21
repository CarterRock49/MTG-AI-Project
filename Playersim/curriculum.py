"""Deterministic opponent curriculum and matchup scheduling.

Matchmaking deliberately owns an RNG stream separate from shuffling and game
resolution.  A policy changing the number of random effects in one episode
therefore cannot change which deck, seat, or opponent profile it sees next.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import random


OPPONENT_PROFILES = frozenset({"passive", "novice", "scripted"})

COMBAT_CURRICULUM_V1 = {
    "id": "combat-v1",
    "version": 1,
    "progression": "fixed_timesteps",
    "allow_mirrors": False,
    "stages": [
        {
            "name": "goldfish",
            "start_timestep": 0,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 10,
        },
        {
            "name": "race",
            "start_timestep": 30_000,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["novice"] * 10,
        },
        {
            "name": "bridge",
            "start_timestep": 75_000,
            "decks": [
                "Selesnya Ouroboroid", "Mono-Green Landfall",
                "Izzet Prowess", "Azorius Momo",
            ],
            "profile_bag": ["novice"] * 5 + ["scripted"] * 5,
        },
        {
            "name": "full_pool",
            "start_timestep": 125_000,
            "decks": "*",
            "profile_bag": ["novice"] + ["scripted"] * 9,
        },
    ],
}


# Round 7.88: the first combat curriculum moved from passive play to a 100%
# novice field while the policy still timed out in most goldfish games.  V2
# keeps every matchup deterministic, but lets the trainer hold a stage until a
# rolling outcome window demonstrates that the policy is ready for the next
# distribution.  Opponent strength also ramps through mixtures instead of a
# single hard step.
COMBAT_CURRICULUM_V2 = {
    "id": "combat-v2",
    "version": 2,
    "progression": "mastery",
    "allow_mirrors": False,
    "stages": [
        {
            "name": "goldfish",
            "start_timestep": 0,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 10,
            "advance_when": {
                "window_episodes": 64,
                "min_stage_timesteps": 30_000,
                "min_decisive_win_rate": 0.60,
                "max_decisive_loss_rate": 0.25,
                "max_timeout_rate": 0.35,
            },
        },
        {
            "name": "race",
            "start_timestep": 30_000,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 7 + ["novice"] * 3,
            "advance_when": {
                "window_episodes": 96,
                "min_stage_timesteps": 45_000,
                "min_decisive_win_rate": 0.45,
                "max_decisive_loss_rate": 0.40,
                "max_timeout_rate": 0.25,
            },
        },
        {
            "name": "bridge",
            "start_timestep": 75_000,
            "decks": [
                "Selesnya Ouroboroid", "Mono-Green Landfall",
                "Izzet Prowess", "Azorius Momo",
            ],
            "profile_bag": (
                ["passive"] * 2 + ["novice"] * 5 + ["scripted"] * 3),
            "advance_when": {
                "window_episodes": 128,
                "min_stage_timesteps": 75_000,
                "min_decisive_win_rate": 0.30,
                "max_decisive_loss_rate": 0.55,
                "max_timeout_rate": 0.25,
            },
        },
        {
            "name": "full_pool",
            "start_timestep": 150_000,
            "decks": "*",
            "profile_bag": ["novice"] * 2 + ["scripted"] * 8,
        },
    ],
}


# Round 7.89: aggregate mastery let passive wins conceal near-zero win rates
# against active opponents, and an unbounded bridge stage left too little time
# for the full deck pool.  V3 retains deterministic matchup cycles while
# requiring evidence against each active profile.  Stage-duration ceilings are
# explicit fallbacks rather than mastery: they guarantee full-pool exposure by
# 375k timesteps in a fresh run even when a skill gate is not met.
COMBAT_CURRICULUM_V3 = {
    "id": "combat-v3",
    "version": 3,
    "progression": "mastery",
    "allow_mirrors": False,
    "stages": [
        {
            "name": "goldfish",
            "start_timestep": 0,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 10,
            "advance_when": {
                "window_episodes": 64,
                "min_stage_timesteps": 30_000,
                "max_stage_timesteps": 75_000,
                "min_decisive_win_rate": 0.60,
                "max_decisive_loss_rate": 0.25,
                "max_timeout_rate": 0.35,
            },
        },
        {
            "name": "race",
            "start_timestep": 30_000,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 4 + ["novice"] * 6,
            "advance_when": {
                "window_episodes": 96,
                "min_stage_timesteps": 45_000,
                "max_stage_timesteps": 100_000,
                "min_decisive_win_rate": 0.40,
                "max_decisive_loss_rate": 0.45,
                "max_timeout_rate": 0.25,
                "profile_requirements": {
                    "novice": {
                        "min_episodes": 48,
                        "min_decisive_win_rate": 0.20,
                    },
                },
            },
        },
        {
            "name": "bridge",
            "start_timestep": 75_000,
            "decks": [
                "Selesnya Ouroboroid", "Mono-Green Landfall",
                "Izzet Prowess", "Azorius Momo",
            ],
            "profile_bag": ["novice"] * 6 + ["scripted"] * 4,
            "advance_when": {
                "window_episodes": 128,
                "min_stage_timesteps": 75_000,
                "max_stage_timesteps": 200_000,
                "min_decisive_win_rate": 0.25,
                "max_decisive_loss_rate": 0.60,
                "max_timeout_rate": 0.25,
                "profile_requirements": {
                    "novice": {
                        "min_episodes": 64,
                        "min_decisive_win_rate": 0.20,
                    },
                    "scripted": {
                        "min_episodes": 40,
                        "min_decisive_win_rate": 0.15,
                    },
                },
            },
        },
        {
            "name": "full_pool",
            "start_timestep": 150_000,
            "decks": "*",
            "profile_bag": ["novice"] * 2 + ["scripted"] * 8,
        },
    ],
}


# Round 7.91: rounds 7.88-7.90 all showed the same cliff — ~60% decisive wins
# against passive opponents collapsing to ~5% against novice, flat for 100k+
# timesteps, so race always fell to its deadline.  V4 keeps the V3 gates and
# deadlines but gives each active stage a climbable slope:
# - "handicap": the stage's active profiles open weakened.  With probability
#   epsilon a handicapped opponent takes the goldfish (passive) baseline for
#   one priority decision instead of attacking/blocking/developing.  The
#   trainer ratchets epsilon toward zero each time the rolling decisive win
#   rate against the handicapped profile clears the stage target; mastery
#   still demands full-strength (epsilon-zero) evidence.
# - "max_turns": early stages shorten the turn limit.  Wins under the 31-turn
#   limit averaged turn 25 while losses averaged turn 17, so long stalls were
#   starving the learner of episodes; shorter games buy more terminal
#   outcomes per timestep and tighten credit assignment.
COMBAT_CURRICULUM_V4 = {
    "id": "combat-v4",
    "version": 4,
    "progression": "mastery",
    "allow_mirrors": False,
    "stages": [
        {
            "name": "goldfish",
            "start_timestep": 0,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 10,
            "max_turns": 20,
            "advance_when": {
                "window_episodes": 64,
                "min_stage_timesteps": 30_000,
                "max_stage_timesteps": 75_000,
                "min_decisive_win_rate": 0.60,
                "max_decisive_loss_rate": 0.25,
                "max_timeout_rate": 0.35,
            },
        },
        {
            "name": "race",
            "start_timestep": 30_000,
            "decks": ["Selesnya Ouroboroid", "Mono-Green Landfall"],
            "profile_bag": ["passive"] * 4 + ["novice"] * 6,
            "max_turns": 20,
            "handicap": {
                "profiles": ["novice"],
                "start": 0.75,
                "step": 0.25,
                "window_episodes": 24,
                "min_decisive_win_rate": 0.40,
            },
            "advance_when": {
                "window_episodes": 96,
                "min_stage_timesteps": 45_000,
                "max_stage_timesteps": 100_000,
                "min_decisive_win_rate": 0.40,
                "max_decisive_loss_rate": 0.45,
                "max_timeout_rate": 0.25,
                "profile_requirements": {
                    "novice": {
                        "min_episodes": 48,
                        "min_decisive_win_rate": 0.20,
                    },
                },
            },
        },
        {
            "name": "bridge",
            "start_timestep": 75_000,
            "decks": [
                "Selesnya Ouroboroid", "Mono-Green Landfall",
                "Izzet Prowess", "Azorius Momo",
            ],
            "profile_bag": ["novice"] * 6 + ["scripted"] * 4,
            "max_turns": 25,
            "handicap": {
                "profiles": ["scripted"],
                "start": 0.60,
                "step": 0.20,
                "window_episodes": 24,
                "min_decisive_win_rate": 0.25,
            },
            "advance_when": {
                "window_episodes": 128,
                "min_stage_timesteps": 75_000,
                "max_stage_timesteps": 200_000,
                "min_decisive_win_rate": 0.25,
                "max_decisive_loss_rate": 0.60,
                "max_timeout_rate": 0.25,
                "profile_requirements": {
                    "novice": {
                        "min_episodes": 64,
                        "min_decisive_win_rate": 0.20,
                    },
                    "scripted": {
                        "min_episodes": 40,
                        "min_decisive_win_rate": 0.15,
                    },
                },
            },
        },
        {
            "name": "full_pool",
            "start_timestep": 150_000,
            "decks": "*",
            "profile_bag": ["novice"] * 2 + ["scripted"] * 8,
        },
    ],
}


# Round 7.92: the v4 canary (run round-7.91-annealed-ramp-v3, ~730k steps)
# earned race mastery and reached a 0.281 evaluation at 400k, then plateaued.
# Two stage-level causes were measurable:
# - goldfish at 20 turns converted a fresh policy's slow kills into ~71%
#   timeouts, so the stage fell to its deadline instead of mastering; race at
#   20 turns worked once the policy arrived competent.  V5 gives goldfish 25.
# - full_pool cold-opened at 80% full-strength scripted play across all
#   eight decks (training decisive win rate ~15%).  V5 extends the proven
#   annealed handicap to full_pool so the four decks absent from bridge ramp
#   in the same way bridge's scripted profile did.
COMBAT_CURRICULUM_V5 = deepcopy(COMBAT_CURRICULUM_V4)
COMBAT_CURRICULUM_V5.update({"id": "combat-v5", "version": 5})
COMBAT_CURRICULUM_V5["stages"][0]["max_turns"] = 25
COMBAT_CURRICULUM_V5["stages"][3]["handicap"] = {
    "profiles": ["scripted"],
    "start": 0.40,
    "step": 0.20,
    "window_episodes": 24,
    "min_decisive_win_rate": 0.25,
}


# Round 7.93: the v5 canary (run round-7.92-combat-v5-v3, 925k steps) peaked
# at a 0.281 evaluation at 200k and decayed once scripted play reached full
# strength: 24-episode windows ratcheted on noise-level evidence (6/24 wins,
# exactly the stage floor) and the one-way ratchet offered no path back while
# the training win rate fell to ~8%.  The trainer now gates both directions
# on the window's 95% Wilson interval, and v6 widens the ratchet windows so
# the interval can resolve either way: at 48 episodes a 0.25 target tightens
# on 18/48 (37.5%) and hands a rung back at 6/48 (12.5%) or worse, where 24
# episodes would need 11/24 to tighten yet relax only at 1/24.
COMBAT_CURRICULUM_V6 = deepcopy(COMBAT_CURRICULUM_V5)
COMBAT_CURRICULUM_V6.update({"id": "combat-v6", "version": 6})
for stage in COMBAT_CURRICULUM_V6["stages"]:
    if stage.get("handicap"):
        stage["handicap"]["window_episodes"] = 48
del stage


# Round 7.95: the v6 canary (run round-7.94-tempo-v1, 1M steps) validated the
# two-way interval ratchet — no collapse, best-final eval of any run — but
# spent its whole back half ping-ponging the scripted epsilon 0.40<->0.20:
# ~38% decisive wins at 0.40 (tightens) versus ~12% at 0.20 (relaxes), so the
# 0.20 step spans the entire measured skill cliff.  V7 halves the scripted
# step to 0.10, giving the ladder rungs at 0.30 and 0.10 that the interval
# gate can actually resolve through.  The novice ramp keeps its 0.25 step:
# race has never oscillated.  Finer rungs double the ladder length and each
# rung needs a fresh 48-episode window, so v7 is paired with a 2M-step
# horizon (round-7.95) rather than 7.94's 1M.
COMBAT_CURRICULUM_V7 = deepcopy(COMBAT_CURRICULUM_V6)
COMBAT_CURRICULUM_V7.update({"id": "combat-v7", "version": 7})
for stage in COMBAT_CURRICULUM_V7["stages"]:
    handicap = stage.get("handicap")
    if handicap and handicap["profiles"] == ["scripted"]:
        handicap["step"] = 0.10
del stage, handicap


def _stable_seed(*parts) -> int:
    payload = ":".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def derive_matchup_seed(base_seed: int, worker_index: int) -> int:
    """Return a stable worker-specific seed without Python's salted hash."""
    return _stable_seed(base_seed, "training-matchups", worker_index)


def resolve_curriculum(name, decks):
    """Resolve and strictly validate a named curriculum for this corpus."""
    if name in (None, "none"):
        return None
    presets = {
        "combat-v1": COMBAT_CURRICULUM_V1,
        "combat-v2": COMBAT_CURRICULUM_V2,
        "combat-v3": COMBAT_CURRICULUM_V3,
        "combat-v4": COMBAT_CURRICULUM_V4,
        "combat-v5": COMBAT_CURRICULUM_V5,
        "combat-v6": COMBAT_CURRICULUM_V6,
        "combat-v7": COMBAT_CURRICULUM_V7,
    }
    if name not in presets:
        raise ValueError(f"Unknown curriculum: {name}")
    spec = deepcopy(presets[name])
    available = [
        deck.get("name") for deck in decks if isinstance(deck, dict)
    ]
    if len(available) != len(set(available)):
        raise ValueError("Curriculum deck names must be unique")
    available_set = set(available)
    stages = spec.get("stages") or []
    progression = str(spec.get("progression") or "fixed_timesteps")
    if progression not in {"fixed_timesteps", "mastery"}:
        raise ValueError(f"Unknown curriculum progression: {progression}")
    spec["progression"] = progression
    if not stages or stages[0].get("start_timestep") != 0:
        raise ValueError("Curriculum stage zero must start at timestep zero")
    previous = -1
    names = set()
    for stage in stages:
        start = int(stage.get("start_timestep", -1))
        if start <= previous:
            raise ValueError("Curriculum thresholds must be strictly increasing")
        previous = start
        stage_name = str(stage.get("name") or "")
        if not stage_name or stage_name in names:
            raise ValueError("Curriculum stage names must be unique and nonempty")
        names.add(stage_name)
        stage_decks = available if stage.get("decks") == "*" \
            else list(stage.get("decks") or [])
        missing = sorted(set(stage_decks) - available_set)
        if missing:
            raise ValueError(
                f"Curriculum stage {stage_name} requires missing decks: {missing}")
        if (not spec.get("allow_mirrors", False)
                and len(stage_decks) < 2):
            raise ValueError(
                f"Curriculum stage {stage_name} needs at least two decks")
        bag = list(stage.get("profile_bag") or [])
        unknown = sorted(set(bag) - OPPONENT_PROFILES)
        if not bag or unknown:
            raise ValueError(
                f"Curriculum stage {stage_name} has invalid profiles: {unknown}")
        max_turns = stage.get("max_turns")
        if max_turns is not None:
            max_turns = int(max_turns)
            if not 5 <= max_turns <= 100:
                raise ValueError(
                    f"Curriculum stage {stage_name} has an invalid max_turns")
            stage["max_turns"] = max_turns
        handicap = stage.get("handicap")
        if handicap is not None:
            if not isinstance(handicap, dict):
                raise ValueError(
                    f"Curriculum stage {stage_name} has an invalid handicap")
            handicap_profiles = [
                str(profile) for profile in (handicap.get("profiles") or [])]
            invalid_handicap_profiles = sorted(
                set(handicap_profiles) - (OPPONENT_PROFILES & set(bag))
                | ({"passive"} & set(handicap_profiles)))
            if not handicap_profiles or invalid_handicap_profiles:
                raise ValueError(
                    f"Curriculum stage {stage_name} handicaps profiles that "
                    f"are passive, unknown, or absent from its bag: "
                    f"{invalid_handicap_profiles}")
            start = float(handicap.get("start", -1.0))
            step = float(handicap.get("step", -1.0))
            handicap_window = int(handicap.get("window_episodes", 0))
            target = float(handicap.get("min_decisive_win_rate", -1.0))
            if (not 0.0 < start <= 1.0 or not 0.0 < step <= 1.0
                    or handicap_window <= 0 or not 0.0 <= target <= 1.0):
                raise ValueError(
                    f"Curriculum stage {stage_name} has invalid handicap "
                    "parameters")
            stage["handicap"] = {
                "profiles": handicap_profiles,
                "start": start,
                "step": step,
                "window_episodes": handicap_window,
                "min_decisive_win_rate": target,
            }
        gate = stage.get("advance_when")
        if progression == "mastery" and stage is not stages[-1]:
            if not isinstance(gate, dict):
                raise ValueError(
                    f"Curriculum stage {stage_name} needs an advance_when gate")
            window = int(gate.get("window_episodes", 0))
            minimum_steps = int(gate.get("min_stage_timesteps", -1))
            if window <= 0 or minimum_steps < 0:
                raise ValueError(
                    f"Curriculum stage {stage_name} has an invalid mastery window")
            gate["window_episodes"] = window
            gate["min_stage_timesteps"] = minimum_steps
            maximum_steps = gate.get("max_stage_timesteps")
            if maximum_steps is not None:
                maximum_steps = int(maximum_steps)
                if maximum_steps < minimum_steps:
                    raise ValueError(
                        f"Curriculum stage {stage_name} has an invalid "
                        "maximum stage duration")
                gate["max_stage_timesteps"] = maximum_steps
            for field in (
                    "min_decisive_win_rate", "max_decisive_loss_rate",
                    "max_timeout_rate"):
                value = float(gate.get(field, -1.0))
                if not 0.0 <= value <= 1.0:
                    raise ValueError(
                        f"Curriculum stage {stage_name} has invalid {field}")
                gate[field] = value
            requirements = gate.get("profile_requirements") or {}
            if not isinstance(requirements, dict):
                raise ValueError(
                    f"Curriculum stage {stage_name} has invalid profile "
                    "requirements")
            normalized_requirements = {}
            for profile, requirement in requirements.items():
                if profile not in OPPONENT_PROFILES or profile not in bag:
                    raise ValueError(
                        f"Curriculum stage {stage_name} requires unavailable "
                        f"profile: {profile}")
                if not isinstance(requirement, dict):
                    raise ValueError(
                        f"Curriculum stage {stage_name} has invalid {profile} "
                        "profile requirement")
                min_episodes = int(requirement.get("min_episodes", 0))
                min_win_rate = float(
                    requirement.get("min_decisive_win_rate", -1.0))
                if (min_episodes <= 0 or min_episodes > window
                        or not 0.0 <= min_win_rate <= 1.0):
                    raise ValueError(
                        f"Curriculum stage {stage_name} has invalid {profile} "
                        "profile requirement")
                normalized_requirements[profile] = {
                    "min_episodes": min_episodes,
                    "min_decisive_win_rate": min_win_rate,
                }
            gate["profile_requirements"] = normalized_requirements
        stage["decks"] = stage_decks
        stage["profile_bag"] = bag
    if progression == "mastery":
        has_deadline = any(
            (stage.get("advance_when") or {}).get("max_stage_timesteps")
            is not None
            for stage in stages[:-1])
        spec["transition_semantics"] = (
            "central_mastery_or_deadline_future_reset_with_activation_ack"
            if has_deadline
            else "central_mastery_next_reset")
    else:
        spec["transition_semantics"] = "global_timestep_next_reset"
    return spec


class CurriculumScheduler:
    """Stateless-cycle scheduler with commit-on-success episode counters."""

    # Minimum sampling weight so a deck the agent already wins with is never
    # fully starved of training games under matchup weighting.
    _MATCHUP_WEIGHT_FLOOR = 0.15

    def __init__(self, spec, matchup_seed, matchup_weighting=False):
        if spec is None:
            raise ValueError("CurriculumScheduler requires a resolved spec")
        self.spec = deepcopy(spec)
        self.matchup_seed = int(matchup_seed)
        self.timestep = 0
        self._stage_override = None
        self._episode_counts = [0] * len(self.spec["stages"])
        # Opt-in matchup weighting oversamples the agent piloting the decks it
        # is losing with (the reactive-deck collapse lever). Off by default:
        # peek() then does the original even round-robin over deck pairs.
        self.matchup_weighting = bool(matchup_weighting)
        self._agent_deck_wins = defaultdict(int)
        self._agent_deck_games = defaultdict(int)

    def record_agent_result(self, agent_deck, decisive_win):
        """Record a training game's outcome for the deck the agent piloted.

        Only consulted when matchup weighting is enabled; harmless to call
        otherwise. Decisive wins raise a deck's win rate and therefore lower
        its future sampling weight.
        """
        if agent_deck is None:
            return
        self._agent_deck_games[agent_deck] += 1
        if decisive_win:
            self._agent_deck_wins[agent_deck] += 1

    def _agent_deck_weight(self, deck):
        games = self._agent_deck_games.get(deck, 0)
        wins = self._agent_deck_wins.get(deck, 0)
        # Laplace-smoothed win rate so unseen decks start near 0.5 and a single
        # game cannot swing the weight to an extreme.
        smoothed_win_rate = (wins + 1) / (games + 2)
        return max(self._MATCHUP_WEIGHT_FLOOR, 1.0 - smoothed_win_rate)

    def set_timestep(self, timestep):
        self.timestep = max(0, int(timestep))

    def stage_index(self):
        if self._stage_override is not None:
            return self._stage_override
        selected = 0
        for index, stage in enumerate(self.spec["stages"]):
            if self.timestep < int(stage["start_timestep"]):
                break
            selected = index
        return selected

    def set_stage(self, stage_index, timestep=None):
        """Select an explicit centrally coordinated stage for future resets."""
        selected = int(stage_index)
        if not 0 <= selected < len(self.spec["stages"]):
            raise ValueError(f"Curriculum stage index is out of range: {selected}")
        self._stage_override = selected
        if timestep is not None:
            self.set_timestep(timestep)

    def _shuffled_cycle(self, values, label, stage, cycle):
        values = list(values)
        rng = random.Random(_stable_seed(
            self.matchup_seed, label, stage["name"], cycle))
        rng.shuffle(values)
        return values

    def _weighted_matchup(self, decks, stage, episode):
        """Sample the agent deck inversely to its win rate; opponent uniform.

        Deterministic given the episode and the current win/loss counts (the
        counts evolve as games are recorded, so exposure adapts). A per-episode
        seed keeps two workers on the same seed reproducible for a fixed
        history.
        """
        rng = random.Random(_stable_seed(
            self.matchup_seed, "weighted-matchup", stage["name"], episode))
        weights = [self._agent_deck_weight(deck) for deck in decks]
        agent_deck = rng.choices(decks, weights=weights, k=1)[0]
        opponent_pool = [
            deck for deck in decks
            if self.spec.get("allow_mirrors", False) or deck != agent_deck]
        opponent_deck = rng.choice(opponent_pool or decks)
        return agent_deck, opponent_deck

    def peek(self, agent_is_p1):
        index = self.stage_index()
        stage = self.spec["stages"][index]
        episode = self._episode_counts[index]
        decks = list(stage["decks"])
        if self.matchup_weighting:
            agent_deck, opponent_deck = self._weighted_matchup(
                decks, stage, episode)
        else:
            pairs = [
                (agent, opponent)
                for agent in decks for opponent in decks
                if self.spec.get("allow_mirrors", False) or agent != opponent
            ]
            pair_cycle, pair_offset = divmod(episode, len(pairs))
            ordered_pairs = self._shuffled_cycle(
                pairs, "pairs", stage, pair_cycle)
            agent_deck, opponent_deck = ordered_pairs[pair_offset]

        bag = list(stage["profile_bag"])
        profile_cycle, profile_offset = divmod(episode, len(bag))
        ordered_profiles = self._shuffled_cycle(
            bag, "profiles", stage, profile_cycle)
        profile = ordered_profiles[profile_offset]
        return {
            "stage": stage["name"],
            "stage_index": index,
            "matchup_episode_index": episode,
            "agent_deck": agent_deck,
            "opponent_deck": opponent_deck,
            "p1_deck": agent_deck if agent_is_p1 else opponent_deck,
            "p2_deck": opponent_deck if agent_is_p1 else agent_deck,
            "agent_is_p1": bool(agent_is_p1),
            "opponent_profile": profile,
            "max_turns": stage.get("max_turns"),
        }

    def commit(self, stage_index):
        self._episode_counts[int(stage_index)] += 1

    def manifest(self):
        return deepcopy(self.spec)
