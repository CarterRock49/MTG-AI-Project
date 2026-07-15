"""Deterministic opponent curriculum and matchup scheduling.

Matchmaking deliberately owns an RNG stream separate from shuffling and game
resolution.  A policy changing the number of random effects in one episode
therefore cannot change which deck, seat, or opponent profile it sees next.
"""

from __future__ import annotations

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
            for field in (
                    "min_decisive_win_rate", "max_decisive_loss_rate",
                    "max_timeout_rate"):
                value = float(gate.get(field, -1.0))
                if not 0.0 <= value <= 1.0:
                    raise ValueError(
                        f"Curriculum stage {stage_name} has invalid {field}")
                gate[field] = value
        stage["decks"] = stage_decks
        stage["profile_bag"] = bag
    spec["transition_semantics"] = (
        "central_mastery_next_reset" if progression == "mastery"
        else "global_timestep_next_reset")
    return spec


class CurriculumScheduler:
    """Stateless-cycle scheduler with commit-on-success episode counters."""

    def __init__(self, spec, matchup_seed):
        if spec is None:
            raise ValueError("CurriculumScheduler requires a resolved spec")
        self.spec = deepcopy(spec)
        self.matchup_seed = int(matchup_seed)
        self.timestep = 0
        self._stage_override = None
        self._episode_counts = [0] * len(self.spec["stages"])

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

    def peek(self, agent_is_p1):
        index = self.stage_index()
        stage = self.spec["stages"][index]
        episode = self._episode_counts[index]
        decks = list(stage["decks"])
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
        }

    def commit(self, stage_index):
        self._episode_counts[int(stage_index)] += 1

    def manifest(self):
        return deepcopy(self.spec)
