"""Versioned, deterministic deck-strategy profiles.

This module is the single taxonomy boundary shared by deck ingestion,
analytics, the strategic planner, and Observation v6's exact-own strategy
field.  It deliberately does not inspect an opponent's hidden zones; the
public opponent belief remains an independent, public-information contract.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


TAXONOMY_VERSION = 1
CLASSIFIER_VERSION = 1


class PrimaryArchetype(str, Enum):
    """Closed macro-gameplan vocabulary used by new strategy profiles."""

    AGGRO = "aggro"
    TEMPO = "tempo"
    MIDRANGE = "midrange"
    CONTROL = "control"
    COMBO = "combo"
    RAMP = "ramp"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


PRIMARY_ARCHETYPES = tuple(item.value for item in PrimaryArchetype)

# Tags describe how a deck executes its macro plan.  They are intentionally
# separate from the primary enum: e.g. reanimator can be combo or midrange,
# and prowess can be aggro or tempo.  Order is part of the encoded contract.
STRATEGY_TAGS = (
    "alternate_win",
    "artifacts",
    "big_mana",
    "blink",
    "board_control",
    "burn",
    "counters",
    "discard",
    "enchantments",
    "equipment",
    "fliers",
    "go_wide",
    "graveyard",
    "landfall",
    "lands",
    "lessons",
    "lifegain",
    "mill",
    "prison",
    "prowess",
    "reanimator",
    "sacrifice",
    "spellslinger",
    "tokens",
    "toolbox",
    "typal",
    "voltron",
)

# Axes are quantized integers in [0, 100].  Quantization keeps serialization,
# hashing, and cross-process equality independent of floating-point details.
STRATEGY_AXES = (
    "speed",
    "threat_density",
    "interaction",
    "card_advantage",
    "mana_acceleration",
    "synergy_dependency",
    "combo_dependency",
    "graveyard_dependency",
    "board_width",
    "instant_speed",
)

PROFILE_SOURCES = {
    "declared_validated", "rules_inferred", "public_inferred",
}

_LEGACY_PRIMARY = {
    "aggro": PrimaryArchetype.AGGRO,
    "burn": PrimaryArchetype.AGGRO,
    "stompy": PrimaryArchetype.AGGRO,
    "voltron": PrimaryArchetype.AGGRO,
    "tempo": PrimaryArchetype.TEMPO,
    "spellslinger": PrimaryArchetype.TEMPO,
    "midrange": PrimaryArchetype.MIDRANGE,
    "tribal": PrimaryArchetype.MIDRANGE,
    "typal": PrimaryArchetype.MIDRANGE,
    "tokens": PrimaryArchetype.MIDRANGE,
    "lifegain": PrimaryArchetype.MIDRANGE,
    "blink": PrimaryArchetype.MIDRANGE,
    "toolbox": PrimaryArchetype.MIDRANGE,
    "control": PrimaryArchetype.CONTROL,
    "stax": PrimaryArchetype.CONTROL,
    "prison": PrimaryArchetype.CONTROL,
    "discard": PrimaryArchetype.CONTROL,
    "mill": PrimaryArchetype.CONTROL,
    "combo": PrimaryArchetype.COMBO,
    "reanimator": PrimaryArchetype.COMBO,
    "aristocrats": PrimaryArchetype.COMBO,
    "ramp": PrimaryArchetype.RAMP,
    "lands": PrimaryArchetype.RAMP,
    "hybrid": PrimaryArchetype.HYBRID,
    "unknown": PrimaryArchetype.UNKNOWN,
}

_RULE_CONTRACT = {
    "classifier_version": CLASSIFIER_VERSION,
    "hybrid_margin_percent": 8,
    "minimum_recognized_cards": 8,
    "rules": (
        "macro.integer_score.v1",
        "tags.tokenized_oracle.v1",
        "axes.quantized.v1",
        "ties.primary_order.v1",
    ),
}


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256_payload(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def taxonomy_identity() -> dict[str, Any]:
    payload = {
        "kind": "playersim_strategy_taxonomy",
        "taxonomy_version": TAXONOMY_VERSION,
        "primary_archetypes": list(PRIMARY_ARCHETYPES),
        "tags": list(STRATEGY_TAGS),
        "axes": list(STRATEGY_AXES),
    }
    return {**payload, "sha256": _sha256_payload(payload)}


def classifier_identity() -> dict[str, Any]:
    payload = {
        "kind": "playersim_full_deck_classifier",
        "taxonomy_sha256": taxonomy_identity()["sha256"],
        **_RULE_CONTRACT,
    }
    return {**payload, "sha256": _sha256_payload(payload)}


TAXONOMY_SHA256 = taxonomy_identity()["sha256"]
CLASSIFIER_SHA256 = classifier_identity()["sha256"]


@dataclass(frozen=True)
class ProfileValidationReport:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    normalized: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeckStrategyProfile:
    """Immutable classifier result with a stable serialized identity."""

    primary: PrimaryArchetype
    secondary: PrimaryArchetype | None
    tags: tuple[str, ...]
    axes: tuple[int, ...]
    confidence_bp: int
    source: str
    rule_ids: tuple[str, ...]
    evidence: tuple[tuple[str, int], ...]
    feature_hash: str
    taxonomy_version: int = TAXONOMY_VERSION
    classifier_version: int = CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        if self.taxonomy_version != TAXONOMY_VERSION:
            raise ValueError("unsupported strategy taxonomy version")
        if self.classifier_version != CLASSIFIER_VERSION:
            raise ValueError("unsupported strategy classifier version")
        if self.source not in PROFILE_SOURCES:
            raise ValueError(f"unsupported strategy profile source {self.source!r}")
        if tuple(sorted(set(self.tags))) != self.tags:
            raise ValueError("strategy tags must be unique and sorted")
        if any(tag not in STRATEGY_TAGS for tag in self.tags):
            raise ValueError("strategy profile contains an unknown tag")
        if len(self.axes) != len(STRATEGY_AXES):
            raise ValueError("strategy profile has the wrong axis count")
        if any(isinstance(value, bool) or not isinstance(value, int)
               or not 0 <= value <= 100 for value in self.axes):
            raise ValueError("strategy axes must be integer values in [0, 100]")
        if not 0 <= self.confidence_bp <= 10_000:
            raise ValueError("confidence_bp must be in [0, 10000]")
        if tuple(sorted(set(self.rule_ids))) != self.rule_ids:
            raise ValueError("rule IDs must be unique and sorted")
        if tuple(sorted(self.evidence)) != self.evidence:
            raise ValueError("evidence must be sorted by key")

    @property
    def axis_values(self) -> dict[str, int]:
        return dict(zip(STRATEGY_AXES, self.axes))

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "taxonomy_version": self.taxonomy_version,
            "classifier_version": self.classifier_version,
            "taxonomy_sha256": TAXONOMY_SHA256,
            "classifier_sha256": CLASSIFIER_SHA256,
            "primary": self.primary.value,
            "secondary": (
                self.secondary.value if self.secondary is not None else None),
            "tags": list(self.tags),
            "axes": self.axis_values,
            "confidence_bp": self.confidence_bp,
            "source": self.source,
            "rule_ids": list(self.rule_ids),
            "evidence": dict(self.evidence),
            "feature_hash": self.feature_hash,
        }

    @property
    def profile_hash(self) -> str:
        return _sha256_payload(self._identity_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self._identity_payload()
        payload["profile_hash"] = self.profile_hash
        return payload


def _primary(value: Any, *, allow_none: bool = False):
    if value is None and allow_none:
        return None
    raw = getattr(value, "value", value)
    try:
        return PrimaryArchetype(str(raw).strip().casefold())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown primary archetype {value!r}") from exc


def normalize_declared_profile(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize reviewed manifest profile metadata."""

    if not isinstance(payload, Mapping):
        raise ValueError("strategy_profile must be an object")
    try:
        version = int(payload.get("taxonomy_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy_profile taxonomy_version is required") from exc
    if version != TAXONOMY_VERSION:
        raise ValueError(
            f"strategy_profile taxonomy_version must be {TAXONOMY_VERSION}")
    primary = _primary(payload.get("primary"))
    if primary in {PrimaryArchetype.HYBRID, PrimaryArchetype.UNKNOWN}:
        raise ValueError("reviewed profiles require a concrete primary archetype")
    secondary = _primary(payload.get("secondary"), allow_none=True)
    if secondary in {PrimaryArchetype.HYBRID, PrimaryArchetype.UNKNOWN}:
        raise ValueError("reviewed secondary archetype must be concrete")
    if secondary == primary:
        raise ValueError("secondary archetype must differ from primary")

    raw_tags = payload.get("tags", [])
    if not isinstance(raw_tags, (list, tuple)):
        raise ValueError("strategy_profile tags must be a list")
    tags = tuple(sorted(str(tag).strip().casefold() for tag in raw_tags))
    if len(tags) != len(set(tags)):
        raise ValueError("strategy_profile tags must be unique")
    unknown_tags = sorted(set(tags) - set(STRATEGY_TAGS))
    if unknown_tags:
        raise ValueError("unknown strategy tags: " + ", ".join(unknown_tags))

    raw_axes = payload.get("axes")
    if not isinstance(raw_axes, Mapping):
        raise ValueError("strategy_profile axes must be an object")
    missing = [name for name in STRATEGY_AXES if name not in raw_axes]
    extra = sorted(set(raw_axes) - set(STRATEGY_AXES))
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ValueError("invalid strategy axes: " + "; ".join(details))
    axes = {}
    for name in STRATEGY_AXES:
        value = raw_axes[name]
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= 100:
            raise ValueError(f"strategy axis {name} must be an integer in [0, 100]")
        axes[name] = value

    review = payload.get("review")
    if not isinstance(review, Mapping) or review.get("status") != "reviewed":
        raise ValueError("strategy_profile review.status must be 'reviewed'")
    reviewed_at = str(review.get("reviewed_at", "")).strip()
    basis = str(review.get("basis", "")).strip()
    if not reviewed_at or not basis:
        raise ValueError("reviewed_at and review basis are required")
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "primary": primary.value,
        "secondary": secondary.value if secondary is not None else None,
        "tags": list(tags),
        "axes": axes,
        "review": {
            "status": "reviewed",
            "reviewed_at": reviewed_at,
            "basis": basis,
        },
    }


def validate_declared_profile(payload: Mapping[str, Any]) -> ProfileValidationReport:
    try:
        normalized = normalize_declared_profile(payload)
    except ValueError as exc:
        return ProfileValidationReport(False, (str(exc),))
    return ProfileValidationReport(True, normalized=normalized)


def declared_profile_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_payload(normalize_declared_profile(payload))


def _counts(card_counts: Mapping[Any, int] | Sequence[Any]) -> Counter:
    if isinstance(card_counts, Mapping):
        result = Counter()
        for card_id, raw_count in card_counts.items():
            if isinstance(raw_count, bool) or not isinstance(raw_count, int) \
                    or raw_count < 0:
                raise ValueError("deck copy counts must be nonnegative integers")
            if raw_count:
                result[card_id] += raw_count
        return result
    if isinstance(card_counts, (str, bytes)) or not isinstance(
            card_counts, Sequence):
        raise ValueError("deck must be a card-ID sequence or count mapping")
    return Counter(card_counts)


def _stable_card_key(card_id: Any) -> tuple[str, str]:
    return type(card_id).__name__, repr(card_id)


def deck_composition_hash(card_counts: Mapping[Any, int] | Sequence[Any]) -> str:
    counts = _counts(card_counts)
    payload = [
        [type(card_id).__name__, repr(card_id), count]
        for card_id, count in sorted(counts.items(), key=lambda row: _stable_card_key(row[0]))
    ]
    return _sha256_payload(payload)


def _attr(card: Any, name: str, default: Any = None) -> Any:
    if isinstance(card, Mapping):
        return card.get(name, default)
    return getattr(card, name, default)


def _finite_number(card: Any, name: str) -> float:
    try:
        value = float(_attr(card, name, 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _types(card: Any) -> set[str]:
    values = _attr(card, "card_types", None)
    if not values:
        type_line = str(_attr(card, "type_line", ""))
        values = re.split(r"\s+", type_line.split("—", 1)[0].split("-", 1)[0])
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _subtypes(card: Any) -> tuple[str, ...]:
    values = _attr(card, "subtypes", None)
    if values is None:
        type_line = str(_attr(card, "type_line", ""))
        pieces = re.split(r"\s+[—-]\s+", type_line, maxsplit=1)
        values = pieces[1].split() if len(pieces) == 2 else ()
    return tuple(str(value).strip().casefold() for value in values if str(value).strip())


def _ratio(numerator: int, denominator: int) -> int:
    return 0 if denominator <= 0 else min(
        100, max(0, (100 * numerator + denominator // 2) // denominator))


def _clamp(value: int) -> int:
    return min(100, max(0, int(value)))


def _extract_features(card_counts, card_db: Mapping[Any, Any]) -> tuple[dict[str, int], set[str], str]:
    counts = _counts(card_counts)
    features = Counter()
    subtype_counts = Counter()
    name_counts = Counter()
    cmc_hundred_sum = 0
    for card_id, count in sorted(counts.items(), key=lambda row: _stable_card_key(row[0])):
        features["deck_cards"] += count
        card = card_db.get(card_id) if isinstance(card_db, Mapping) else None
        if card is None:
            features["unknown_cards"] += count
            continue
        features["recognized_cards"] += count
        name = str(_attr(card, "name", card_id)).strip().casefold()
        text = str(_attr(card, "oracle_text", "") or "").casefold()
        types = _types(card)
        subtypes = _subtypes(card)
        name_counts[name] += count
        is_land = "land" in types
        is_creature = "creature" in types
        is_instant = "instant" in types
        is_sorcery = "sorcery" in types
        if is_land:
            features["lands"] += count
        else:
            features["nonlands"] += count
            cmc_hundred_sum += int(round(_finite_number(card, "cmc") * 100)) * count
            if _finite_number(card, "cmc") <= 2:
                features["cheap_nonlands"] += count
            if _finite_number(card, "cmc") >= 5:
                features["high_nonlands"] += count
        if is_creature:
            features["creatures"] += count
            if not is_land and _finite_number(card, "cmc") <= 2:
                features["cheap_creatures"] += count
            for subtype in subtypes:
                subtype_counts[subtype] += count
        if is_instant:
            features["instants"] += count
        if is_sorcery:
            features["sorceries"] += count
        if "artifact" in types:
            features["artifacts"] += count
        if "enchantment" in types:
            features["enchantments"] += count
        if "equipment" in subtypes or "equip " in text:
            features["equipment"] += count
        if "lesson" in subtypes or "lesson" in name:
            features["lessons"] += count
        if re.search(r"\b(haste|prowess|double strike|menace)\b", text):
            features["aggressive_keywords"] += count
        if "prowess" in text:
            features["prowess"] += count
        if "flying" in text and is_creature:
            features["fliers"] += count
        if re.search(r"\bcounter target\b|\breturn target\b.*\bhand\b", text):
            features["stack_or_tempo_interaction"] += count
        if re.search(
                r"\bcounter target\b|\bdestroy target\b|\bexile target\b|"
                r"\breturn target\b.*\bhand\b|\btarget creature gets -", text):
            features["interaction"] += count
        if re.search(
                r"destroy all|exile all|all creatures get -|damage to each creature", text):
            features["board_wipes"] += count
            features["interaction"] += count
        if re.search(r"\bdraw (a|one|two|three|x|that many) cards?\b", text):
            features["card_advantage"] += count
        if re.search(r"search your library.*\bcard\b", text):
            features["tutors"] += count
        if re.search(
                r"\badd \{[wubrgc]\}|\badd (one|two|three|x) mana\b|"
                r"search your library.*\bland card\b|put .*\bland card\b.*battlefield|"
                r"play an additional land", text):
            features["mana_acceleration"] += count
        if "landfall" in text:
            features["landfall"] += count
        if re.search(r"\bcreate\b.*\btokens?\b", text):
            features["token_generators"] += count
        if "+1/+1 counter" in text or "quest counter" in text:
            features["counters"] += count
        if re.search(r"\binstant or sorcery\b|\bnoncreature spell\b|second spell", text):
            features["spell_payoffs"] += count
        if re.search(r"\bdeals?\b.*\bdamage\b.*(any target|target player|opponent)", text):
            features["burn"] += count
        if "graveyard" in text:
            features["graveyard"] += count
        if re.search(r"return .* from (your|a) graveyard.*battlefield", text):
            features["reanimation"] += count
        if re.search(r"\bmill\b|cards? from the top of .*library.*graveyard", text):
            features["mill"] += count
        if re.search(r"target (opponent|player) discards|each opponent discards", text):
            features["discard"] += count
        if re.search(r"\bsacrifice (a|another|one|any number|this)\b|whenever .* dies", text):
            features["sacrifice"] += count
        if re.search(r"\bgain [x0-9]+ life\b|\blifelink\b", text):
            features["lifegain"] += count
        if re.search(r"exile .* you control.*return .*battlefield", text):
            features["blink"] += count
        if re.search(r"\bwin the game\b|\bloses the game\b|all but the bottom", text):
            features["alternate_win"] += count
        if re.search(r"can't attack|can't cast|doesn't untap|players can't|opponents can't", text):
            features["prison"] += count
        if "each creature you control" in text or "creatures you control get" in text:
            features["go_wide"] += count

    features["cmc_hundred_sum"] = cmc_hundred_sum
    if subtype_counts:
        dominant_subtype, dominant_count = min(
            subtype_counts.items(), key=lambda row: (-row[1], row[0]))
        features["dominant_subtype_count"] = dominant_count
        features["dominant_subtype_hash"] = int(
            hashlib.sha256(dominant_subtype.encode("utf-8")).hexdigest()[:8], 16)

    nonlands = features["nonlands"]
    creatures = features["creatures"]
    spells = features["instants"] + features["sorceries"]
    tags = set()
    direct_tags = {
        "alternate_win": "alternate_win", "blink": "blink",
        "board_wipes": "board_control", "burn": "burn",
        "counters": "counters", "discard": "discard",
        "equipment": "equipment", "graveyard": "graveyard",
        "landfall": "landfall", "lessons": "lessons",
        "lifegain": "lifegain", "mill": "mill", "prison": "prison",
        "prowess": "prowess", "reanimation": "reanimator",
        "sacrifice": "sacrifice", "token_generators": "tokens",
    }
    for feature, tag in direct_tags.items():
        if features[feature] > 0:
            tags.add(tag)
    if features["artifacts"] * 3 >= max(1, nonlands):
        tags.add("artifacts")
    if features["enchantments"] * 3 >= max(1, nonlands):
        tags.add("enchantments")
    if features["fliers"] * 2 >= max(1, creatures) and features["fliers"] >= 6:
        tags.add("fliers")
    if features["go_wide"] or features["token_generators"] >= 2:
        tags.add("go_wide")
    if features["landfall"] or features["lands"] * 100 >= max(1, features["recognized_cards"]) * 45:
        tags.add("lands")
    if features["mana_acceleration"] >= 6 and features["high_nonlands"] >= 4:
        tags.add("big_mana")
    if features["spell_payoffs"] >= 2 or (
            spells * 100 >= max(1, nonlands) * 65 and spells >= 12):
        tags.add("spellslinger")
    if (features["dominant_subtype_count"] >= 8
            and features["dominant_subtype_count"] * 2 >= max(1, creatures)):
        tags.add("typal")
    if features["equipment"] >= 4 and creatures >= 8:
        tags.add("voltron")

    identity_payload = {
        "composition": sorted(name_counts.items()),
        "features": sorted(features.items()),
    }
    return dict(features), tags, _sha256_payload(identity_payload)


def _axes(features: Mapping[str, int], tags: set[str]) -> tuple[int, ...]:
    nonlands = max(1, features.get("nonlands", 0))
    creatures = features.get("creatures", 0)
    spells = features.get("instants", 0) + features.get("sorceries", 0)
    cheap = _ratio(features.get("cheap_nonlands", 0), nonlands)
    high = _ratio(features.get("high_nonlands", 0), nonlands)
    threat = _ratio(creatures, nonlands)
    interaction = _ratio(features.get("interaction", 0), nonlands)
    advantage = _ratio(features.get("card_advantage", 0), nonlands)
    acceleration = _ratio(features.get("mana_acceleration", 0), nonlands)
    graveyard = _ratio(features.get("graveyard", 0), nonlands)
    instant_speed = _ratio(features.get("instants", 0), max(1, spells))
    speed = _clamp((cheap * 3 + threat * 2 - high * 2 + 100) // 4)
    synergy_markers = sum(features.get(name, 0) for name in (
        "spell_payoffs", "landfall", "token_generators", "counters",
        "reanimation", "sacrifice", "dominant_subtype_count"))
    synergy = _ratio(synergy_markers, nonlands)
    combo_markers = (
        features.get("alternate_win", 0) * 3
        + features.get("reanimation", 0) * 2
        + features.get("tutors", 0)
        + features.get("sacrifice", 0))
    combo = _ratio(combo_markers, nonlands)
    board_width = _clamp(
        threat + _ratio(features.get("token_generators", 0), nonlands) // 2)
    values = {
        "speed": speed,
        "threat_density": threat,
        "interaction": interaction,
        "card_advantage": advantage,
        "mana_acceleration": acceleration,
        "synergy_dependency": synergy,
        "combo_dependency": combo,
        "graveyard_dependency": graveyard,
        "board_width": board_width,
        "instant_speed": instant_speed,
    }
    return tuple(values[name] for name in STRATEGY_AXES)


def _macro_scores(features: Mapping[str, int], axes: tuple[int, ...], tags: set[str]):
    axis = dict(zip(STRATEGY_AXES, axes))
    nonlands = max(1, features.get("nonlands", 0))
    cheap = _ratio(features.get("cheap_nonlands", 0), nonlands)
    high = _ratio(features.get("high_nonlands", 0), nonlands)
    spell_ratio = _ratio(
        features.get("instants", 0) + features.get("sorceries", 0), nonlands)
    scores = {
        PrimaryArchetype.AGGRO: (
            axis["speed"] * 3 + axis["threat_density"] * 2 + cheap * 2
            + features.get("aggressive_keywords", 0) * 8
            + (45 if "burn" in tags else 0)
            + (35 if "go_wide" in tags else 0)),
        PrimaryArchetype.TEMPO: (
            axis["speed"] * 2 + axis["interaction"] * 3
            + axis["instant_speed"] + axis["threat_density"]
            + features.get("stack_or_tempo_interaction", 0) * 12
            + (80 if "prowess" in tags else 0)
            + (60 if "spellslinger" in tags else 0)),
        PrimaryArchetype.MIDRANGE: (
            axis["threat_density"] * 2 + axis["interaction"] * 2
            + axis["card_advantage"] * 2
            + (100 - abs(axis["speed"] - 55))
            + (40 if "typal" in tags else 0)),
        PrimaryArchetype.CONTROL: (
            axis["interaction"] * 4 + axis["card_advantage"] * 3
            + spell_ratio * 2 + (100 - axis["speed"])
            + features.get("board_wipes", 0) * 15
            + (50 if "prison" in tags else 0)),
        PrimaryArchetype.COMBO: (
            axis["combo_dependency"] * 5 + axis["synergy_dependency"] * 2
            + features.get("tutors", 0) * 12
            + (160 if "alternate_win" in tags else 0)
            + (100 if "reanimator" in tags else 0)
            + (50 if "sacrifice" in tags else 0)),
        PrimaryArchetype.RAMP: (
            axis["mana_acceleration"] * 5 + high * 2
            + features.get("landfall", 0) * 10
            + (80 if "big_mana" in tags else 0)
            + (40 if "lands" in tags else 0)),
    }
    return scores


def classify_full_deck(
        card_counts: Mapping[Any, int] | Sequence[Any],
        card_db: Mapping[Any, Any], *,
        declared: Mapping[str, Any] | None = None) -> DeckStrategyProfile:
    """Classify one full deck without randomness or mutable global state.

    A structurally valid reviewed profile is authoritative for the pinned deck
    corpus.  Rule inference remains available for imported/unknown decks and
    produces its feature evidence and confidence deterministically.
    """

    features, inferred_tags, feature_hash = _extract_features(card_counts, card_db)
    axes = _axes(features, inferred_tags)
    base_evidence = {
        "deck_cards": features.get("deck_cards", 0),
        "recognized_cards": features.get("recognized_cards", 0),
        "unknown_cards": features.get("unknown_cards", 0),
    }
    if declared is not None:
        normalized = normalize_declared_profile(declared)
        primary = PrimaryArchetype(normalized["primary"])
        secondary = (
            PrimaryArchetype(normalized["secondary"])
            if normalized["secondary"] else None)
        declared_axes = tuple(
            normalized["axes"][name] for name in STRATEGY_AXES)
        evidence = {**base_evidence, "declared_profile": 1}
        return DeckStrategyProfile(
            primary=primary, secondary=secondary,
            tags=tuple(normalized["tags"]), axes=declared_axes,
            confidence_bp=10_000, source="declared_validated",
            rule_ids=("profile.declared.reviewed.v1",),
            evidence=tuple(sorted(evidence.items())),
            feature_hash=feature_hash,
        )

    recognized = features.get("recognized_cards", 0)
    if recognized < _RULE_CONTRACT["minimum_recognized_cards"]:
        evidence = {**base_evidence, "minimum_required": 8}
        return DeckStrategyProfile(
            primary=PrimaryArchetype.UNKNOWN, secondary=None,
            tags=tuple(sorted(inferred_tags)), axes=axes, confidence_bp=0,
            source="rules_inferred", rule_ids=("macro.unknown.coverage.v1",),
            evidence=tuple(sorted(evidence.items())), feature_hash=feature_hash)

    scores = _macro_scores(features, axes, inferred_tags)
    order = {item: index for index, item in enumerate(PrimaryArchetype)}
    ranked = sorted(scores.items(), key=lambda row: (-row[1], order[row[0]]))
    (top, top_score), (runner_up, runner_score) = ranked[:2]
    margin_percent = (
        100 if top_score <= 0 else (100 * (top_score - runner_score)) // top_score)
    is_hybrid = margin_percent <= _RULE_CONTRACT["hybrid_margin_percent"]
    primary = PrimaryArchetype.HYBRID if is_hybrid else top
    secondary = top if is_hybrid else (
        runner_up if runner_score * 100 >= top_score * 72 else None)
    coverage = _ratio(recognized, max(1, features.get("deck_cards", 0)))
    confidence = _clamp(50 + margin_percent // 2)
    confidence_bp = confidence * coverage
    evidence = {
        **base_evidence,
        "coverage_percent": coverage,
        "margin_percent": margin_percent,
        "top_score": top_score,
        "runner_up_score": runner_score,
    }
    rule_ids = {"macro.integer_score.v1", "axes.quantized.v1"}
    rule_ids.update(f"tag.{tag}.v1" for tag in inferred_tags)
    if is_hybrid:
        rule_ids.add("macro.hybrid.margin.v1")
    return DeckStrategyProfile(
        primary=primary, secondary=secondary,
        tags=tuple(sorted(inferred_tags)), axes=axes,
        confidence_bp=min(10_000, confidence_bp), source="rules_inferred",
        rule_ids=tuple(sorted(rule_ids)), evidence=tuple(sorted(evidence.items())),
        feature_hash=feature_hash,
    )


def compatibility_primary(value: Any) -> PrimaryArchetype:
    """Map legacy/specialized labels to the closed primary macro enum."""

    if isinstance(value, DeckStrategyProfile):
        primary = value.primary
        if primary == PrimaryArchetype.HYBRID and value.secondary is not None:
            return value.secondary
        return primary
    raw = str(getattr(value, "value", value) or "unknown").strip().casefold()
    return _LEGACY_PRIMARY.get(raw, PrimaryArchetype.UNKNOWN)


def planner_strategy_label(profile: DeckStrategyProfile) -> str:
    """Return the existing planner's compatibility label for a profile."""

    tags = set(profile.tags)
    if "typal" in tags and profile.primary in {
            PrimaryArchetype.AGGRO, PrimaryArchetype.MIDRANGE,
            PrimaryArchetype.HYBRID}:
        return "tribal"
    macro = compatibility_primary(profile)
    if macro in {PrimaryArchetype.HYBRID, PrimaryArchetype.UNKNOWN}:
        return PrimaryArchetype.MIDRANGE.value
    return macro.value


def encode_profile(profile: DeckStrategyProfile) -> tuple[float, ...]:
    """Encode the active Observation-v6 exact-own strategy contract."""

    primary = tuple(
        1.0 if profile.primary.value == name else 0.0
        for name in PRIMARY_ARCHETYPES)
    secondary = tuple(
        1.0 if profile.secondary is not None
        and profile.secondary.value == name else 0.0
        for name in PRIMARY_ARCHETYPES)
    tags = tuple(1.0 if name in profile.tags else 0.0 for name in STRATEGY_TAGS)
    axes = tuple(value / 100.0 for value in profile.axes)
    return primary + secondary + tags + axes + (profile.confidence_bp / 10_000.0,)


PROFILE_VECTOR_SIZE = (
    len(PRIMARY_ARCHETYPES) * 2 + len(STRATEGY_TAGS)
    + len(STRATEGY_AXES) + 1)
