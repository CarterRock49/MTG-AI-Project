"""Static format-pool support preflight and versioned coverage ledger.

This audit is deliberately stricter than ``coverage_report``: a card with no
runtime manifest entry is not automatically called supported. Every pool card
is constructed, every face is passed through ability/replacement registration,
and spell/loyalty/activated/triggered effect text is probed through the shared
effect factory. The resulting static status is not a rules proof. Semantic
``verified`` requires schema-v2 Oracle/surface identities plus passing,
assertion-bearing exact-state unittest evidence.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import logging
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .ability_types import ActivatedAbility, ManaAbility, TriggeredAbility
from .ability_utils import EffectFactory
from .card import Card
from .card_registry import (
    _file_sha256,
    corpus_identity,
    load_pool_snapshot_cards,
    load_registry,
    registry_identity,
)
from .card_support import get_manifest, reset_manifest_for_tests
from .game_state import GameState

LEDGER_KIND = "format_card_support_ledger"
LEDGER_SCHEMA_VERSION = 2
OVERRIDES_SCHEMA_VERSION = 2
SEMANTIC_SURFACE_SCHEMA_VERSION = 2
SEMANTIC_SURFACE_MAX_DECISIONS = 96
SEMANTIC_SURFACE_MAX_BRANCHES = 256
# A passing test and a covers declaration are strong review inputs, but Python
# cannot prove that those assertions semantically establish every claimed
# Magic surface. Keep ledger promotion locked until that mapping has a
# machine-verifiable evidence artifact.
SEMANTIC_PROMOTION_ENABLED = False

_ORACLE_RULE_FIELDS = (
    "name", "oracle_id", "layout", "mana_cost", "cmc", "type_line",
    "oracle_text", "power", "toughness", "loyalty", "defense",
    "keywords", "colors", "color_indicator", "produced_mana",
)
_VERIFIED_RECORD_FIELDS = {
    "oracle_id", "oracle_rules_sha256", "surface_schema_version",
    "required_surfaces_sha256", "scenarios",
}
_SCENARIO_RECORD_FIELDS = {
    "test_node_id", "test_file_sha256", "test_node_sha256",
    "assertion_contract", "covers",
}
_INACTIVE_TEST_DECORATORS = {
    "skip", "skipif", "skipunless", "expectedfailure", "xfail",
}
_EVIDENCE_RUNNER = """
import importlib.util
import json
import sys
import unittest

path, class_name, method_name = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("_ledger_evidence_test", path)
if spec is None or spec.loader is None:
    raise SystemExit(2)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
case_class = getattr(module, class_name)
if not issubclass(case_class, unittest.TestCase):
    raise SystemExit(3)
case = case_class(method_name)
result = unittest.TestResult()
unittest.TestSuite([case]).run(result)
payload = {
    "tests_run": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "expected_failures": len(result.expectedFailures),
    "unexpected_successes": len(result.unexpectedSuccesses),
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload == {
    "tests_run": 1,
    "failures": 0,
    "errors": 0,
    "skipped": 0,
    "expected_failures": 0,
    "unexpected_successes": 0,
} else 1)
""".strip()
_EVIDENCE_RUNNER_SHA256 = hashlib.sha256(
    _EVIDENCE_RUNNER.encode("utf-8")).hexdigest()


def _canonical_hash(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rules_payload(raw_card: dict) -> dict:
    """Return only the frozen fields that can change a card's rules."""
    payload = {
        field: copy.deepcopy(raw_card.get(field))
        for field in _ORACLE_RULE_FIELDS
    }
    faces = raw_card.get("card_faces") or []
    payload["card_faces"] = [
        {field: copy.deepcopy(face.get(field)) for field in _ORACLE_RULE_FIELDS}
        for face in faces if isinstance(face, dict)
    ]
    return payload


def oracle_rules_sha256(raw_card: dict) -> str:
    """Hash the rules-bearing Oracle identity without price/image churn."""
    return _canonical_hash({"oracle_rules": _rules_payload(raw_card)})


def _ast_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _evidence_test_file(evidence_root, test_node_id: str):
    if not isinstance(test_node_id, str) or not test_node_id.strip():
        raise ValueError("test_node_id must be a non-empty string")
    if chr(92) in test_node_id:
        raise ValueError("test_node_id paths must use forward slashes")
    parts = test_node_id.split("::")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError(
            "test_node_id must name one unittest class and method")
    relative = Path(parts[0])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("test_node_id must stay within tests/")
    root = Path(evidence_root).resolve()
    tests_root = (root / "tests").resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(tests_root)
    except ValueError as exc:
        raise ValueError("test_node_id must stay within tests/") from exc
    if path.suffix != ".py" or not path.name.endswith("_test.py"):
        raise ValueError("evidence must reference a discoverable *_test.py file")
    if not path.is_file():
        raise ValueError(f"evidence test file does not exist: {parts[0]}")
    return parts, path


def _unittest_node(tree, parts, function_types, test_node_id):
    class_node = next((item for item in tree.body
                       if isinstance(item, ast.ClassDef)
                       and item.name == parts[1]), None)
    if class_node is None:
        raise ValueError(f"evidence test class does not exist: {test_node_id}")
    if not any(_ast_name(base).split(".")[-1] in {
            "TestCase", "IsolatedAsyncioTestCase"}
            for base in class_node.bases):
        raise ValueError("evidence unittest class must inherit from TestCase")
    if not parts[2].startswith("test_"):
        raise ValueError("evidence unittest methods must start with test_")
    node = next((item for item in class_node.body
                 if isinstance(item, function_types)
                 and item.name == parts[2]), None)
    if node is None:
        raise ValueError(f"evidence test method does not exist: {test_node_id}")
    return node, class_node


def _decorator_leaf(decorator) -> str:
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    return _ast_name(target).split(".")[-1].casefold().replace("_", "")


def _validate_test_ast(node, class_node, test_node_id: str,
                       required_card_name=None):
    for decorated in (class_node, node):
        inactive = {
            _decorator_leaf(decorator)
            for decorator in decorated.decorator_list
        }.intersection(_INACTIVE_TEST_DECORATORS)
        if inactive:
            raise ValueError(
                f"evidence test node is inactive: {test_node_id}")
    has_assertion = any(
        isinstance(item, ast.Assert)
        or (isinstance(item, ast.Call)
            and _ast_name(item.func).split(".")[-1].casefold().startswith(
                "assert"))
        for item in ast.walk(node))
    if not has_assertion:
        raise ValueError(
            f"evidence test node has no assertion: {test_node_id}")
    if required_card_name:
        card_key = required_card_name.casefold()
        string_literals = [
            item.value.casefold()
            for item in ast.walk(class_node)
            if isinstance(item, ast.Constant)
            and isinstance(item.value, str)
        ]
        if not any(card_key in value for value in string_literals):
            raise ValueError(
                f"evidence test node does not name card "
                f"{required_card_name}")


def _run_unittest_node(evidence_root, path, parts, test_node_id: str):
    try:
        completed = subprocess.run(
            [
                sys.executable, "-c", _EVIDENCE_RUNNER, str(path),
                parts[1], parts[2],
            ],
            cwd=Path(evidence_root).resolve(),
            capture_output=True,
            text=True,
            timeout=300,
            check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"evidence test node could not run: {test_node_id}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValueError(
            f"evidence test node did not pass exactly once: "
            f"{test_node_id}: {detail[-1000:]}")


def test_node_identity(evidence_root, test_node_id: str, *,
                       required_card_name=None, execute=False) -> dict:
    parts, path = _evidence_test_file(evidence_root, test_node_id)
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"evidence test file does not parse: {parts[0]}") from exc
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    node, class_node = _unittest_node(
        tree, parts, function_types, test_node_id)
    _validate_test_ast(
        node, class_node, test_node_id,
        required_card_name=required_card_name)
    if execute:
        _run_unittest_node(evidence_root, path, parts, test_node_id)
    node_dump = ast.dump(node, annotate_fields=True, include_attributes=False)
    return {
        "test_node_id": test_node_id,
        "test_file_sha256": _file_sha256(path),
        "test_node_sha256": hashlib.sha256(
            node_dump.encode("utf-8")).hexdigest(),
    }


def _casefold_name_list(value, label: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = {}
    for raw_name in value:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"{label} entries must be non-empty strings")
        name = raw_name.strip()
        key = name.casefold()
        if key in result:
            raise ValueError(f"{label} contains a duplicate card: {name}")
        result[key] = name
    return result


def _parse_verified_records(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("verified must be an object keyed by exact card name")
    result = {}
    for raw_name, record in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("verified card names must be non-empty strings")
        name = raw_name.strip()
        key = name.casefold()
        if key in result:
            raise ValueError(f"verified contains a duplicate card: {name}")
        if not isinstance(record, dict):
            raise ValueError(f"verified evidence for {name} must be an object")
        result[key] = {"name": name, "record": copy.deepcopy(record)}
    return result


def _parse_excluded(value) -> dict[str, str]:
    if isinstance(value, list):
        names = _casefold_name_list(value, "excluded")
        return {key: "explicit exclusion" for key in names}
    if not isinstance(value, dict):
        raise ValueError("excluded must be an object or list")
    result = {}
    for raw_name, reason in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("excluded card names must be non-empty strings")
        key = raw_name.strip().casefold()
        if key in result:
            raise ValueError(f"excluded contains a duplicate card: {raw_name}")
        result[key] = str(reason)
    return result


def _parse_override_payload(payload: dict, path) -> dict:
    allowed = {
        "schema_version", "legacy_verified_claims", "verified", "excluded"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(
            f"{path} has unknown override field(s): "
            + ", ".join(sorted(unknown)))
    legacy = _casefold_name_list(
        payload.get("legacy_verified_claims", []),
        "legacy_verified_claims")
    verified = _parse_verified_records(payload.get("verified", {}))
    excluded = _parse_excluded(payload.get("excluded", {}))
    overlaps = {
        "legacy_verified_claims and verified": set(legacy) & set(verified),
        "legacy_verified_claims and excluded": set(legacy) & set(excluded),
        "verified and excluded": set(verified) & set(excluded),
    }
    for label, overlap in overlaps.items():
        if overlap:
            raise ValueError(
                f"Cards cannot appear in both {label}: "
                + ", ".join(sorted(overlap)))
    return {
        "legacy_verified_claims": legacy,
        "verified": verified,
        "excluded": excluded,
    }


def _read_overrides(path) -> dict:
    if path is None or not Path(path).is_file():
        return {
            "legacy_verified_claims": {}, "verified": {}, "excluded": {}}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != OVERRIDES_SCHEMA_VERSION:
        raise ValueError(f"{path} has unsupported overrides schema_version")
    return _parse_override_payload(payload, path)


def load_corpus_frequencies(decks_directory) -> tuple[Counter, dict[str, list[str]]]:
    """Return casefolded card-copy counts and deck membership."""
    counts = Counter()
    memberships = defaultdict(list)
    if decks_directory is None:
        return counts, memberships
    corpus_path = Path(decks_directory)
    payloads = []
    if corpus_path.is_file():
        with corpus_path.open("r", encoding="utf-8") as handle:
            corpus = json.load(handle)
        payloads = [(corpus_path, deck) for deck in corpus.get("decks", [])]
    else:
        for path in sorted(
                corpus_path.rglob("*.json"),
                key=lambda item: item.relative_to(
                    corpus_path).as_posix().casefold()):
            with path.open("r", encoding="utf-8") as handle:
                payloads.append((path, json.load(handle)))
    for path, payload in payloads:
        deck_name = str(payload.get("name") or path.stem)
        entries = payload.get("deck", payload.get("cards", payload))
        if not isinstance(entries, list):
            raise ValueError(f"{path} does not contain a deck list")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            card_data = entry.get("card", entry)
            name = (card_data.get("name") if isinstance(card_data, dict)
                    else card_data)
            if not name:
                continue
            count = max(0, int(entry.get("count", 1)))
            key = str(name).casefold()
            counts[key] += count
            if deck_name not in memberships[key]:
                memberships[key].append(deck_name)
    return counts, memberships


def _player_state(game_state, player_num):
    seed_id = next(iter(game_state.card_db))
    player = game_state._init_player([seed_id] * 8, player_num)
    for zone in ("library", "hand", "battlefield", "graveyard", "exile"):
        player[zone] = []
    return player


def _face_surfaces(raw_card: dict) -> list[dict]:
    faces = raw_card.get("card_faces") or []
    if faces:
        return [{
            "name": face.get("name", raw_card.get("name", "")),
            "oracle_text": face.get("oracle_text", "") or "",
            "type_line": face.get("type_line", "") or "",
        } for face in faces]
    return [{
        "name": raw_card.get("name", ""),
        "oracle_text": raw_card.get("oracle_text", "") or "",
        "type_line": raw_card.get("type_line", "") or "",
    }]


def _probe_effect_text(text, source_name, _depth=0):
    text = (text or "").strip()
    if not text:
        return 0
    effects = EffectFactory.create_effects(text, source_name=source_name)
    if _depth < 4:
        from .ability_types import DelayedTriggerEffect
        for effect in effects:
            if isinstance(effect, DelayedTriggerEffect):
                _probe_effect_text(
                    effect.inner_text, source_name, _depth=_depth + 1)
    return len(effects)


def _probe_registered_effects(abilities, source_name):
    probes = 0
    for ability in abilities:
        if isinstance(ability, ManaAbility):
            continue
        if not isinstance(ability, (ActivatedAbility, TriggeredAbility)):
            continue
        effect_text = (getattr(ability, "effect", None)
                       or getattr(ability, "effect_text", ""))
        if isinstance(ability, TriggeredAbility):
            effect_text = getattr(ability, "effect", effect_text)
        probes += 1
        _probe_effect_text(effect_text, source_name)
    return probes


def _probe_surface_effects(surface, source_name):
    text = surface["oracle_text"]
    type_line = surface["type_line"].lower()
    probes = 0
    if "instant" in type_line or "sorcery" in type_line:
        probes += 1
        _probe_effect_text(text, source_name)
    for line in text.splitlines():
        if re.match(r"^\s*[+\-−]?\d+\s*:", line):
            probes += 1
            _probe_effect_text(line.split(":", 1)[1], source_name)
    return probes


def _cleanup_registration(game_state, card_id, card):
    try:
        game_state.ability_handler.unregister_card_abilities(card_id)
    except Exception:
        pass
    game_state.ability_handler.registered_abilities.pop(card_id, None)
    text_registered = getattr(
        game_state.replacement_effects, "_text_registered_cards", None)
    if text_registered is not None:
        text_registered.discard(card_id)
    try:
        card.set_current_face(0)
    except Exception:
        pass
    if hasattr(card, "reset_to_printed"):
        card.reset_to_printed()


def _semantic_option(option: dict) -> dict:
    reason = " ".join(str(option.get("reason", "")).split())
    reason = re.sub(
        r"^([A-Z_]+)\s*\(\d+\):\s*",
        lambda match: f"{match.group(1)}: ", reason)
    return {
        "action_type": str(option.get("action_type", "")),
        "reason": reason,
    }


def _surface_descriptor(row: dict) -> dict:
    retained = (
        "id", "kind", "matched_surface", "oracle_surface", "clause_sha256",
        "mode_indices", "printed_trigger_id", "face_index", "decision_kind",
        "branch_prefix", "event_arm", "negative_arm", "trigger_condition",
    )
    return {
        key: copy.deepcopy(row.get(key))
        for key in retained if row.get(key) is not None
    }


def _add_surface(target: dict, surface_id: str, kind: str, descriptor: dict):
    record = {
        "id": str(surface_id), "kind": str(kind),
        "descriptor": copy.deepcopy(descriptor),
    }
    prior = target.get(record["id"])
    if prior is not None and prior != record:
        raise ValueError(f"semantic surface ID collision: {record['id']}")
    target[record["id"]] = record


def _surface_atoms_from_probe(result: dict) -> list[dict]:
    """Expand aggregate probe obligations into independently covered atoms."""
    atoms = {}
    paths = {str(row.get("id")): row for row in result.get("paths", [])}
    for obligation in result.get("obligations", []):
        kind = str(obligation.get("kind", "unknown"))
        if kind == "static_preflight_evidence":
            continue
        if kind == "choice_branch":
            options = obligation.get("semantic_options", []) or []
            if not options:
                _add_surface(
                    atoms, obligation["id"], kind,
                    _surface_descriptor(obligation))
            for option in options:
                normalized = _semantic_option(option)
                option_hash = _canonical_hash({"option": normalized})[:20]
                surface_id = f"{obligation['id']}:option:{option_hash}"
                descriptor = _surface_descriptor(obligation)
                descriptor["semantic_option"] = normalized
                _add_surface(atoms, surface_id, "choice_option", descriptor)
            continue
        path_ids = (
            obligation.get("event_path_ids")
            or obligation.get("negative_path_ids")
            or [])
        if path_ids:
            for path_id in path_ids:
                path = paths.get(str(path_id), {"id": str(path_id)})
                descriptor = _surface_descriptor(obligation)
                descriptor["path"] = _surface_descriptor(path)
                arm_kind = (
                    "trigger_event_arm" if kind == "triggered"
                    else "trigger_negative_arm")
                _add_surface(atoms, str(path_id), arm_kind, descriptor)
            continue
        _add_surface(
            atoms, obligation["id"], kind,
            _surface_descriptor(obligation))
    return [atoms[key] for key in sorted(atoms, key=str.casefold)]


def _oracle_surface_atoms(raw_card: dict) -> list[dict]:
    """Build parser-independent obligations directly from frozen Oracle text."""
    atoms = {}
    faces = raw_card.get("card_faces") or []
    sources = faces if faces else [raw_card]
    for face_index, source in enumerate(sources):
        face_name = str(source.get("name") or raw_card.get("name") or "")
        oracle_text = str(source.get("oracle_text") or "")
        for line_index, raw_line in enumerate(oracle_text.splitlines()):
            normalized = " ".join(raw_line.strip().split())
            if not normalized:
                continue
            if normalized.startswith("(") and normalized.endswith(")"):
                continue
            rule_text = normalized
            prior = None
            while prior != rule_text:
                prior = rule_text
                rule_text = re.sub(r"\([^()]*\)", "", rule_text)
            rule_text = " ".join(rule_text.split()).strip(" .")
            if not rule_text:
                continue
            descriptor = {
                "face_index": face_index,
                "face_name": face_name,
                "line_index": line_index,
                "oracle_text": rule_text,
            }
            line_hash = _canonical_hash(descriptor)[:20]
            line_id = (
                f"oracle:face:{face_index}:line:{line_index}:{line_hash}")
            _add_surface(
                atoms, line_id, "oracle_rule_line", descriptor)
            if re.match(r"^(?:when|whenever|at)\b", rule_text,
                        re.IGNORECASE):
                _add_surface(
                    atoms, f"{line_id}:event",
                    "oracle_trigger_event", descriptor)
                _add_surface(
                    atoms, f"{line_id}:non_event",
                    "oracle_trigger_non_event", descriptor)
            elif ":" in rule_text:
                _add_surface(
                    atoms, f"{line_id}:activate",
                    "oracle_activation", descriptor)
                _add_surface(
                    atoms, f"{line_id}:illegal",
                    "oracle_activation_illegal", descriptor)

    type_line = str(raw_card.get("type_line") or "").casefold()
    is_land = "land" in {
        token for token in re.split(r"[^a-z]+", type_line) if token}
    play_descriptor = {
        "card_name": raw_card.get("name"),
        "type_line": raw_card.get("type_line"),
    }
    if is_land:
        _add_surface(
            atoms, "oracle:card:play", "oracle_card_play",
            play_descriptor)
        _add_surface(
            atoms, "oracle:card:play_illegal",
            "oracle_card_play_illegal", play_descriptor)
    else:
        _add_surface(
            atoms, "oracle:card:cast", "oracle_card_cast",
            play_descriptor)
        _add_surface(
            atoms, "oracle:card:cast_illegal",
            "oracle_card_cast_illegal", play_descriptor)
    return [atoms[key] for key in sorted(atoms, key=str.casefold)]


def generate_semantic_surface_inventory(
        raw_card: dict, registry_entry: dict) -> dict:
    """Return the required, recomputed semantic surface set for one card.

    Schema v1 deliberately derives from the fail-closed dynamic probe, expands
    its compound trigger event/negative arms, and splits every discovered
    public choice into semantic options. It also inherits the probe's bounded
    discovery limits: novel non-trigger Oracle grammar that neither static
    preflight nor the probe recognizes cannot be proven absent. Consequently,
    any static issue blocks verification, printed-trigger discovery remains an
    independent obligation, and hitting the branch cap fails this inventory
    instead of accepting a claimant-provided notion of completeness.
    """
    from .card_probe import PROBE_SCHEMA_VERSION, probe_card

    entry = {
        "index": int(registry_entry["index"]),
        "name": raw_card["name"],
        "oracle_id": registry_entry.get("oracle_id"),
        "ledger_status": "observed_clean",
        "ledger_issues": [],
        "raw": copy.deepcopy(raw_card),
    }
    result = probe_card(
        entry,
        input_identity={
            "semantic_surface_schema_version":
                SEMANTIC_SURFACE_SCHEMA_VERSION,
        },
        max_decisions=SEMANTIC_SURFACE_MAX_DECISIONS,
        max_branches=SEMANTIC_SURFACE_MAX_BRANCHES)
    if result.get("runtime_status") == "failed":
        raise ValueError(
            f"semantic surface discovery failed for {raw_card['name']}")
    capped = [
        row for row in (
            list(result.get("obligations", []))
            + list(result.get("paths", [])))
        if any(marker in str(row.get("reason", "")).casefold()
               for marker in ("branch cap", "bounded branch replay"))
    ]
    if capped:
        raise ValueError(
            f"semantic surface discovery hit its branch cap for "
            f"{raw_card['name']}")
    surfaces_by_id = {}
    for surface in (
            _oracle_surface_atoms(raw_card)
            + _surface_atoms_from_probe(result)):
        surface_id = surface["id"]
        if (surface_id in surfaces_by_id
                and surfaces_by_id[surface_id] != surface):
            raise ValueError(
                f"semantic surface ID collision: {surface_id}")
        surfaces_by_id[surface_id] = surface
    surfaces = [
        surfaces_by_id[key]
        for key in sorted(surfaces_by_id, key=str.casefold)]
    inventory = {
        "schema_version": SEMANTIC_SURFACE_SCHEMA_VERSION,
        "probe_schema_version": PROBE_SCHEMA_VERSION,
        "oracle_inventory": "frozen_oracle_lines_v1",
        "card_name": raw_card["name"],
        "oracle_id": raw_card.get("oracle_id"),
        "surfaces": surfaces,
    }
    inventory["sha256"] = _canonical_hash(inventory)
    return inventory


def _require_exact_fields(value: dict, expected: set[str], label: str):
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"{label} has invalid fields: " + "; ".join(details))


def _validated_scenario_record(record: dict, evidence_root, card_name: str):
    if not isinstance(record, dict):
        raise ValueError(f"scenario evidence for {card_name} must be an object")
    _require_exact_fields(
        record, _SCENARIO_RECORD_FIELDS,
        f"scenario evidence for {card_name}")
    if record.get("assertion_contract") != "exact_state_v1":
        raise ValueError(
            f"scenario evidence for {card_name} must use exact_state_v1")
    actual_identity = test_node_identity(
        evidence_root, record.get("test_node_id"),
        required_card_name=card_name, execute=True)
    for field in (
            "test_node_id", "test_file_sha256", "test_node_sha256"):
        if record.get(field) != actual_identity[field]:
            raise ValueError(
                f"scenario evidence for {card_name} has stale {field}")
    covers = record.get("covers")
    if (not isinstance(covers, list) or not covers
            or any(not isinstance(value, str) or not value
                   for value in covers)):
        raise ValueError(
            f"scenario evidence for {card_name} needs non-empty covers")
    if len(covers) != len(set(covers)):
        raise ValueError(
            f"scenario evidence for {card_name} repeats a surface")
    return actual_identity, set(covers)


def validate_verified_evidence(
        raw_card: dict, registry_entry: dict, record: dict,
        evidence_root) -> dict:
    """Fail closed unless exact tests cover the recomputed surface set."""
    card_name = raw_card["name"]
    _require_exact_fields(
        record, _VERIFIED_RECORD_FIELDS,
        f"verified evidence for {card_name}")
    raw_oracle_id = raw_card.get("oracle_id")
    if not isinstance(raw_oracle_id, str) or not raw_oracle_id.strip():
        raise ValueError(
            f"verified evidence for {card_name} needs a non-empty oracle_id")
    if (record.get("oracle_id") != raw_oracle_id
            or registry_entry.get("oracle_id") != raw_oracle_id):
        raise ValueError(f"verified evidence for {card_name} has stale oracle_id")
    rules_hash = oracle_rules_sha256(raw_card)
    if record.get("oracle_rules_sha256") != rules_hash:
        raise ValueError(
            f"verified evidence for {card_name} has stale "
            "oracle_rules_sha256")
    if (record.get("surface_schema_version")
            != SEMANTIC_SURFACE_SCHEMA_VERSION):
        raise ValueError(
            f"verified evidence for {card_name} has unsupported "
            "surface_schema_version")
    scenarios = record.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(
            f"verified evidence for {card_name} needs scenarios")
    covered = set()
    nodes = []
    seen_nodes = set()
    for scenario_record in scenarios:
        identity, scenario_surfaces = _validated_scenario_record(
            scenario_record, evidence_root, card_name)
        node_id = identity["test_node_id"]
        if node_id in seen_nodes:
            raise ValueError(
                f"verified evidence for {card_name} repeats test node "
                f"{node_id}")
        seen_nodes.add(node_id)
        nodes.append(identity)
        covered.update(scenario_surfaces)
    inventory = generate_semantic_surface_inventory(
        raw_card, registry_entry)
    if record.get("required_surfaces_sha256") != inventory["sha256"]:
        raise ValueError(
            f"verified evidence for {card_name} has stale "
            "required_surfaces_sha256")
    required = {surface["id"] for surface in inventory["surfaces"]}
    if covered != required:
        missing = sorted(required - covered)
        unknown = sorted(covered - required)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(
            f"verified evidence for {card_name} does not exactly cover "
            "required surfaces: " + "; ".join(details))
    return {
        "assertion_contract": "exact_state_v1",
        "oracle_rules_sha256": rules_hash,
        "required_surfaces_sha256": inventory["sha256"],
        "required_surface_count": len(required),
        "scenario_count": len(nodes),
        "test_runner_sha256": _EVIDENCE_RUNNER_SHA256,
        "test_nodes": nodes,
    }


def audit_pool_cards(raw_cards: list[dict], registry: dict) -> list[dict]:
    """Construct and statically probe every supplied pool card."""
    registry_by_name = {entry["name"].casefold(): entry
                        for entry in registry["cards"]}
    cards = {}
    construction_issues = {}
    for raw in raw_cards:
        entry = registry_by_name.get(raw["name"].casefold())
        if entry is None:
            raise ValueError(f"Pool card absent from registry: {raw['name']}")
        try:
            card = Card(copy.deepcopy(raw))
            card.card_id = entry["index"]
            cards[entry["index"]] = card
        except Exception as exc:
            construction_issues[entry["index"]] = str(exc)[:160]

    game_state = GameState(cards)
    game_state.p1 = _player_state(game_state, 1)
    game_state.p2 = _player_state(game_state, 2)
    game_state.priority_player = game_state.p1
    reset_manifest_for_tests()
    rows = []

    for raw in raw_cards:
        registry_entry = registry_by_name[raw["name"].casefold()]
        card_id = registry_entry["index"]
        base = {
            "index": card_id,
            "name": raw["name"],
            "oracle_id": registry_entry.get("oracle_id"),
            "layout": raw.get("layout", "normal"),
            "keywords": sorted({str(value) for value in raw.get("keywords", [])}),
            "faces_audited": len(_face_surfaces(raw)),
            "abilities_registered": 0,
            "replacement_effects_registered": 0,
            "effect_probes": 0,
            "issues": [],
        }
        if card_id in construction_issues:
            base["issues"].append({
                "severity": "crash",
                "reason": "Card construction failed: " + construction_issues[card_id],
            })
            rows.append(base)
            continue

        card = cards[card_id]
        game_state.p1["battlefield"] = [card_id]
        fidelity_before = sum(
            game_state.fidelity_counters.get(key, 0)
            for key in ("unparsed_mana", "unparsed_modal", "unparsed_effects"))
        try:
            surfaces = _face_surfaces(raw)
            for face_index, surface in enumerate(surfaces):
                if face_index and hasattr(card, "set_current_face"):
                    card.set_current_face(face_index)
                game_state.ability_handler._parse_and_register_abilities(
                    card_id, card)
                abilities = list(game_state.ability_handler.registered_abilities.get(
                    card_id, []))
                base["abilities_registered"] += len(abilities)
                base["effect_probes"] += _probe_registered_effects(
                    abilities, raw["name"])
                base["effect_probes"] += _probe_surface_effects(
                    surface, raw["name"])
                replacements = game_state.replacement_effects \
                    .register_card_replacement_effects(card_id, game_state.p1)
                base["replacement_effects_registered"] += len(replacements or [])
                _cleanup_registration(game_state, card_id, card)
        except Exception as exc:
            base["issues"].append({
                "severity": "crash",
                "reason": f"Static preflight raised: {type(exc).__name__}: {exc}"[:200],
            })
        fidelity_after = sum(
            game_state.fidelity_counters.get(key, 0)
            for key in ("unparsed_mana", "unparsed_modal", "unparsed_effects"))
        manifest_entry = get_manifest().entries.get(raw["name"], {})
        for reason, count in sorted(manifest_entry.get("reasons", {}).items()):
            base["issues"].append({
                "severity": manifest_entry.get("severity", "partial"),
                "reason": reason,
                "count": int(count),
            })
        if fidelity_after > fidelity_before and not base["issues"]:
            base["issues"].append({
                "severity": "unparsed",
                "reason": "ability/replacement registration advanced fidelity counters",
                "count": fidelity_after - fidelity_before,
            })
        rows.append(base)
        game_state.p1["battlefield"] = []
        _cleanup_registration(game_state, card_id, card)
    reset_manifest_for_tests()
    return rows


def _issue_mechanics(row: dict) -> set[str]:
    issue_text = " ".join(issue["reason"] for issue in row["issues"]).lower()
    keyword_tags = {
        keyword for keyword in row.get("keywords", [])
        if keyword.lower() in issue_text
    }
    if keyword_tags:
        return keyword_tags
    if row.get("layout") not in (None, "normal"):
        return {f"layout:{row['layout']}"}
    return {"unclassified oracle text"}


def build_support_ledger(
        raw_cards, registry, audit_rows, decks_directory=None,
        corpus_label="bootstrap", overrides_path=None,
        snapshot_path=None, evidence_root=None) -> dict:
    overrides = _read_overrides(overrides_path)
    legacy_verified = overrides["legacy_verified_claims"]
    verified = overrides["verified"]
    excluded = overrides["excluded"]
    raw_by_name = {
        raw["name"].casefold(): raw for raw in raw_cards}
    registry_by_name = {
        entry["name"].casefold(): entry for entry in registry["cards"]}
    audit_by_name = {
        row["name"].casefold(): row for row in audit_rows}
    claimed = set(legacy_verified) | set(verified) | set(excluded)
    unknown = claimed - set(raw_by_name)
    if unknown:
        raise ValueError(
            "Support overrides reference cards outside the pool: "
            + ", ".join(sorted(unknown)))
    for key, claimed_name in legacy_verified.items():
        if claimed_name != raw_by_name[key]["name"]:
            raise ValueError(
                f"legacy claim must use exact card name: {claimed_name}")
    for key, claim in verified.items():
        if claim["name"] != raw_by_name[key]["name"]:
            raise ValueError(
                f"verified evidence must use exact card name: "
                f"{claim['name']}")
    evidence_root = (
        Path(evidence_root).resolve() if evidence_root is not None
        else Path(__file__).resolve().parents[1])
    frequencies, memberships = load_corpus_frequencies(decks_directory)
    severity_rank = {"partial": 1, "unparsed": 2, "crash": 3}
    status_counts = Counter()
    static_status_counts = Counter()
    semantic_status_counts = Counter()
    ranked_cards = []
    mechanic_rows = defaultdict(lambda: {
        "affected_cards": 0, "deck_slots": 0, "deck_cards": 0,
        "status_counts": Counter(), "examples": [],
    })

    for row in audit_rows:
        key = row["name"].casefold()
        row["corpus_copies"] = int(frequencies.get(key, 0))
        row["corpus_decks"] = sorted(memberships.get(key, []))
        if key in excluded:
            row["issues"].append({
                "severity": "excluded", "reason": excluded[key]})
            static_status = "excluded"
        elif row["issues"]:
            static_status = max(
                (issue.get("severity", "partial") for issue in row["issues"]),
                key=lambda value: severity_rank.get(value, 0))
        elif row["corpus_copies"]:
            static_status = "observed_clean"
        else:
            static_status = "unseen"
        semantic_status = "unverified"
        semantic_evidence = None
        status = static_status
        if key in verified:
            if row["issues"]:
                raise ValueError(
                    f"verified evidence cannot override static issues for "
                    f"{row['name']}")
            if not SEMANTIC_PROMOTION_ENABLED:
                raise ValueError(
                    "semantic promotion is disabled until exact-state "
                    "scenario-to-surface coverage is machine-verifiable")
            semantic_evidence = validate_verified_evidence(
                raw_by_name[key], registry_by_name[key],
                verified[key]["record"], evidence_root)
            semantic_status = "verified"
            status = "verified"
        row["legacy_verified_claim"] = key in legacy_verified
        row["static_status"] = static_status
        row["semantic_status"] = semantic_status
        row["semantic_evidence"] = semantic_evidence
        row["status"] = status
        status_counts[status] += 1
        static_status_counts[static_status] += 1
        semantic_status_counts[semantic_status] += 1

        if status in ("partial", "unparsed", "crash", "excluded"):
            ranked_cards.append({
                "name": row["name"], "status": status,
                "deck_slots": row["corpus_copies"],
                "decks": row["corpus_decks"],
                "reasons": sorted({issue["reason"] for issue in row["issues"]}),
            })
            for mechanic in _issue_mechanics(row):
                target = mechanic_rows[mechanic]
                target["affected_cards"] += 1
                target["deck_slots"] += row["corpus_copies"]
                target["deck_cards"] += int(bool(row["corpus_copies"]))
                target["status_counts"][status] += 1
                target["examples"].append((row["corpus_copies"], row["name"]))

    ranked_cards.sort(key=lambda item: (
        -item["deck_slots"], -severity_rank.get(item["status"], 0),
        item["name"].casefold()))
    ranked_mechanics = []
    for mechanic, values in mechanic_rows.items():
        ranked_mechanics.append({
            "mechanic": mechanic,
            "affected_cards": values["affected_cards"],
            "deck_slots": values["deck_slots"],
            "deck_cards": values["deck_cards"],
            "status_counts": dict(sorted(values["status_counts"].items())),
            "examples": [name for _, name in sorted(
                values["examples"], key=lambda pair: (-pair[0], pair[1].casefold()))[:10]],
        })
    ranked_mechanics.sort(key=lambda item: (
        -item["deck_slots"], -item["affected_cards"],
        item["mechanic"].casefold()))

    total = len(audit_rows)
    corpus_source = None
    if decks_directory:
        corpus_path = Path(decks_directory)
        corpus_source = ({
            "path": corpus_path.name,
            "size_bytes": corpus_path.stat().st_size,
            "sha256": _file_sha256(corpus_path),
        } if corpus_path.is_file() else corpus_identity(corpus_path))
    ledger = {
        "kind": LEDGER_KIND,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "evidence_model": {
            "verified": (
                "reserved; promotion is disabled until exact-state "
                "scenario-to-surface coverage is machine-verifiable"),
            "legacy_verified_claim": (
                "historical name-only claim retained for audit; never "
                "semantic verification"),
            "observed_clean": (
                "in configured corpus and clean in static preflight; static "
                "evidence only"),
            "unseen": "not in configured corpus; static preflight found no issue",
            "partial": "some text registered/parsed but a fallback was reported",
            "unparsed": "an effect or registration path produced no runnable result",
            "crash": "construction, registration, or probing raised",
            "excluded": "explicit support override excludes the card",
        },
        "pool_snapshot": ({
            "path": Path(snapshot_path).name,
            "size_bytes": Path(snapshot_path).stat().st_size,
            "sha256": _file_sha256(snapshot_path),
        } if snapshot_path else None),
        "card_registry": registry_identity(registry),
        "corpus": {
            "label": corpus_label,
            "path": Path(decks_directory).name if decks_directory else None,
            "identity": corpus_source,
            "deck_count": len({deck for decks in memberships.values() for deck in decks}),
            "pool_card_slots": sum(
                frequencies.get(row["name"].casefold(), 0) for row in audit_rows),
            "note": ("Ranking reflects this configured corpus only; a bootstrap "
                     "corpus is not a current metagame sample."),
        },
        "overrides": ({
            "path": Path(overrides_path).name,
            "sha256": _file_sha256(overrides_path),
        } if overrides_path and Path(overrides_path).is_file() else None),
        "summary": {
            "total": total,
            "status_counts": dict(sorted(status_counts.items())),
            "static_status_counts": dict(sorted(static_status_counts.items())),
            "semantic_status_counts": dict(sorted(
                semantic_status_counts.items())),
            "legacy_verified_claims": len(legacy_verified),
            "semantic_verified_fraction": (
                semantic_status_counts["verified"] / total
                if total else 0.0),
            "static_clean_fraction": (
                (static_status_counts["observed_clean"]
                 + static_status_counts["unseen"]) / total
                if total else 1.0),
        },
        "ranked_mechanics": ranked_mechanics,
        "ranked_cards": ranked_cards,
        "cards": sorted(audit_rows, key=lambda row: row["index"]),
    }
    ledger["sha256"] = _canonical_hash(ledger)
    return ledger


def write_ledger(path, ledger):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def run_preflight(snapshot, registry_path, output, decks=None,
                  corpus_label="bootstrap", overrides=None, format_name=None,
                  evidence_root=None):
    raw_cards = load_pool_snapshot_cards(snapshot, format_name=format_name)
    registry = load_registry(registry_path)
    audit_rows = audit_pool_cards(raw_cards, registry)
    ledger = build_support_ledger(
        raw_cards, registry, audit_rows, decks_directory=decks,
        corpus_label=corpus_label, overrides_path=overrides,
        snapshot_path=snapshot, evidence_root=evidence_root)
    write_ledger(output, ledger)
    return ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--decks", default=None)
    parser.add_argument("--corpus-label", default="bootstrap")
    parser.add_argument("--overrides", default=None)
    parser.add_argument("--format", dest="format_name", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.disable(logging.CRITICAL)
    ledger = run_preflight(
        args.snapshot, args.registry, args.output, decks=args.decks,
        corpus_label=args.corpus_label, overrides=args.overrides,
        format_name=args.format_name)
    print(json.dumps({
        "output": args.output,
        "summary": ledger["summary"],
        "top_mechanics": ledger["ranked_mechanics"][:10],
        "sha256": ledger["sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
