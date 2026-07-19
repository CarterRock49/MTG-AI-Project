from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

SCHEMA = 3
RUN_KIND = "playersim_dynamic_card_probe"
CARD_KIND = "playersim_dynamic_card_probe_card"
TERMINAL = {"execution_passed", "coverage_gap", "failed"}
OBLIGATION_TERMINAL = {"exercised", "coverage_gap", "failed"}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

args = list(sys.argv[1:])
if not args:
    raise SystemExit(
        "usage: _probe_audit_tmp.py PROBE_ROOT [--verify-inputs] "
        "[--repo REPO_ROOT] [--observe-grouped]"
    )
root = Path(args.pop(0)).resolve()
verify_inputs = "--verify-inputs" in args
observe_grouped = "--observe-grouped" in args
repo = Path.cwd().resolve()
if "--repo" in args:
    position = args.index("--repo")
    if position + 1 >= len(args):
        raise SystemExit("--repo requires a path")
    repo = Path(args[position + 1]).resolve()

errors = []
warnings = []


def fail(code, detail):
    errors.append(f"{code}: {detail}")


def group_fail(code, detail):
    (warnings if observe_grouped else errors).append(f"{code}: {detail}")


def check(condition, code, detail):
    if not condition:
        fail(code, detail)
    return condition


def load_object(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail("json.read", f"{label} {path}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        fail("json.object", f"{label} is {type(value).__name__}")
        return {}
    return value


def canonical_sha256(value):
    """Independent schema-v3 canonical hash; imports no Playersim code."""
    body = {key: item for key, item in value.items() if key != "sha256"}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def physical_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_sha(value):
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def slug(name):
    value = re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")
    return (value or "card")[:80]


def recompute_card_status(payload):
    obligations = payload.get("obligations", [])
    issues = payload.get("issues", [])
    if any(row.get("severity") == "failed" for row in issues if isinstance(row, dict)):
        return "failed"
    if any(row.get("status") == "failed" for row in obligations if isinstance(row, dict)):
        return "failed"
    if any(not isinstance(row, dict) or row.get("status") != "exercised"
           for row in obligations):
        return "coverage_gap"
    return "coverage_gap" if issues else "execution_passed"


def normalized(value):
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip(" .")


run_path = root / "run.json"
report_path = root / "card_probe_report.json"
check(root.is_dir(), "root.directory", str(root))
run = load_object(run_path, "run")
report = load_object(report_path, "report")

for label, payload in (("run", run), ("report", report)):
    check(payload.get("kind") == RUN_KIND, f"{label}.kind", repr(payload.get("kind")))
    check(payload.get("schema_version") == SCHEMA, f"{label}.schema", repr(payload.get("schema_version")))
    check(payload.get("status") == "complete", f"{label}.status", repr(payload.get("status")))
    check(payload.get("semantic_status") == "unverified", f"{label}.semantic", repr(payload.get("semantic_status")))
    calculated = canonical_sha256(payload)
    check(valid_sha(payload.get("sha256")), f"{label}.sha.format", repr(payload.get("sha256")))
    check(payload.get("sha256") == calculated, f"{label}.sha.canonical",
          f"embedded={payload.get('sha256')} calculated={calculated}")

check(run.get("report") == "card_probe_report.json", "run.report.path", repr(run.get("report")))
check(run.get("report_sha256") == report.get("sha256"), "run.report.link",
      f"run={run.get('report_sha256')} report={report.get('sha256')}")
for key in ("input_identity", "scope", "limits", "branch_policy", "summary"):
    check(run.get(key) == report.get(key), f"run.report.{key}", "values differ")
check(bool(report.get("semantic_note")), "report.semantic_note", "missing")
check(bool(run.get("note")), "run.note", "missing")

identity = report.get("input_identity", {})
limits = report.get("limits", {})
scope = report.get("scope", {})
check(isinstance(identity, dict), "identity.type", type(identity).__name__)
check(isinstance(limits, dict), "limits.type", type(limits).__name__)
check(isinstance(scope, dict), "scope.type", type(scope).__name__)
check(identity.get("probe_schema_version") == SCHEMA, "identity.probe_schema",
      repr(identity.get("probe_schema_version")))
for field in ("max_decisions", "max_branches"):
    check(isinstance(limits.get(field), int) and limits.get(field, 0) >= 1,
          f"limits.{field}", repr(limits.get(field)))

rows = report.get("cards", [])
if not isinstance(rows, list):
    fail("report.cards.type", type(rows).__name__)
    rows = []
check(scope.get("selected_cards") == len(rows), "scope.selected_cards",
      f"scope={scope.get('selected_cards')} report={len(rows)}")
for field in ("cards", "statuses"):
    value = scope.get(field)
    check(isinstance(value, list), f"scope.{field}.type", type(value).__name__)
    if isinstance(value, list):
        check(value == sorted(set(map(str, value))), f"scope.{field}.canonical", repr(value))

indices = [row.get("index") for row in rows if isinstance(row, dict)]
names = [row.get("name") for row in rows if isinstance(row, dict)]
artifacts = [row.get("artifact") for row in rows if isinstance(row, dict)]
check(len(indices) == len(rows), "report.cards.objects", "non-object row")
check(all(isinstance(index, int) and not isinstance(index, bool) for index in indices),
      "report.indices.type", "non-integer index")
check(indices == sorted(indices), "report.indices.order", "not registry-index order")
check(len(indices) == len(set(indices)), "report.indices.unique", "duplicate index")
check(all(isinstance(name, str) and name for name in names), "report.names.type", "blank/non-string")
check(len(names) == len({str(name).casefold() for name in names}),
      "report.names.unique", "case-insensitive duplicate")
check(len(artifacts) == len(set(artifacts)), "report.artifacts.unique", "duplicate link")

expected_card_files = set()
for ordinal, row in enumerate(rows):
    if not isinstance(row, dict):
        continue
    index, name, artifact = row.get("index"), row.get("name"), row.get("artifact")
    check(row.get("runtime_status") in TERMINAL, "report.card.runtime_status",
          f"row {ordinal}: {row.get('runtime_status')!r}")
    check(row.get("semantic_status") == "unverified", "report.card.semantic_status",
          f"row {ordinal}: {row.get('semantic_status')!r}")
    check(valid_sha(row.get("sha256")), "report.card.sha.format",
          f"row {ordinal}: {row.get('sha256')!r}")
    if not isinstance(index, int) or isinstance(index, bool) or not isinstance(name, str):
        continue
    expected = f"cards/{index:05d}-{slug(name)}.json"
    check(artifact == expected, "report.card.artifact_name",
          f"{name}: {artifact!r} != {expected!r}")
    if isinstance(artifact, str):
        pure = PurePosixPath(artifact)
        check(not pure.is_absolute() and ".." not in pure.parts and
              len(pure.parts) == 2 and pure.parts[0] == "cards",
              "report.card.artifact_safe", repr(artifact))
        expected_card_files.add(artifact)

expected_all_files = {"run.json", "card_probe_report.json"} | expected_card_files
actual_all_files = {
    path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
}
actual_dirs = {
    path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
}
missing_files = sorted(expected_all_files - actual_all_files)
extra_files = sorted(actual_all_files - expected_all_files)
check(not missing_files, "artifacts.missing", repr(missing_files[:20]))
check(not extra_files, "artifacts.extra", repr(extra_files[:20]))
check("cards" in actual_dirs, "artifacts.cards_dir", "missing")
check(not (actual_dirs - {"cards"}), "artifacts.extra_dirs",
      repr(sorted(actual_dirs - {"cards"})[:20]))
symlinks = sorted(
    path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink()
)
check(not symlinks, "artifacts.symlinks", repr(symlinks[:20]))

results = []
by_name = {}
card_canonical_map = {}
card_physical_map = {}
state_evidence = Counter()
auxiliary_counts = Counter()


def inspect_state_hashes(value, where):
    if isinstance(value, dict):
        has_before = "state_before_sha256" in value
        has_after = "state_after_sha256" in value
        if has_before or has_after:
            state_evidence["before_after_nodes"] += 1
            check(has_before and has_after, "state_hash.pair", where)
            for key in ("state_before_sha256", "state_after_sha256"):
                if key in value:
                    state_evidence["hash_fields"] += 1
                    check(valid_sha(value[key]), "state_hash.format", f"{where}.{key}")
            before_key = next((key for key in ("state_before", "before_state") if key in value), None)
            after_key = next((key for key in ("state_after", "after_state") if key in value), None)
            if before_key is None and after_key is None:
                state_evidence["opaque_before_after_nodes"] += 1
            else:
                check(before_key is not None and after_key is not None,
                      "state_hash.payload_pair", where)
                for payload_key, hash_key, label in (
                    (before_key, "state_before_sha256", "before"),
                    (after_key, "state_after_sha256", "after"),
                ):
                    if payload_key is not None and isinstance(value[payload_key], dict):
                        state_evidence["recomputed_hash_fields"] += 1
                        check(value.get(hash_key) == canonical_sha256(value[payload_key]),
                              f"state_hash.{label}_recompute", where)
        if "state_sha256" in value:
            state_evidence["single_state_nodes"] += 1
            state_evidence["hash_fields"] += 1
            check(valid_sha(value["state_sha256"]), "state_hash.format",
                  f"{where}.state_sha256")
            payload_key = next((key for key in ("state", "state_payload") if key in value), None)
            if payload_key is None:
                state_evidence["opaque_single_state_nodes"] += 1
            elif isinstance(value[payload_key], dict):
                state_evidence["recomputed_hash_fields"] += 1
                check(value["state_sha256"] == canonical_sha256(value[payload_key]),
                      "state_hash.single_recompute", where)
        for key, item in value.items():
            inspect_state_hashes(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            inspect_state_hashes(item, f"{where}[{index}]")


for row in rows:
    if not isinstance(row, dict) or not isinstance(row.get("artifact"), str):
        continue
    artifact = row["artifact"]
    path = root.joinpath(*PurePosixPath(artifact).parts)
    if not path.is_file():
        continue
    payload = load_object(path, artifact)
    results.append(payload)
    card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
    label = f"{artifact} ({card.get('name', row.get('name'))})"
    calculated = canonical_sha256(payload)
    check(payload.get("kind") == CARD_KIND, "card.kind", label)
    check(payload.get("schema_version") == SCHEMA, "card.schema", label)
    check(payload.get("semantic_status") == "unverified", "card.semantic", label)
    check(valid_sha(payload.get("sha256")), "card.sha.format", label)
    check(payload.get("sha256") == calculated, "card.sha.canonical", label)
    check(row.get("sha256") == calculated, "report.card.sha.link", label)
    check(payload.get("input_identity") == identity, "card.input_identity", label)
    check(payload.get("limits") == limits, "card.limits", label)
    check(payload.get("branch_policy") == report.get("branch_policy"),
          "card.branch_policy", label)
    for field in ("index", "name", "ledger_status"):
        check(card.get(field) == row.get(field), f"card.{field}.link", label)
    check(valid_sha(card.get("oracle_sha256")), "card.oracle_sha.format", label)
    check(payload.get("deterministic_seed") == (int(row.get("index", 0)) & 0xFFFFFFFF),
          "card.seed", label)
    status = payload.get("runtime_status", payload.get("status"))
    check(status in TERMINAL, "card.runtime_status", f"{label}: {status!r}")
    check(payload.get("status") == status, "card.status.alias", label)
    check(status == recompute_card_status(payload), "card.status.recompute", label)
    check(row.get("runtime_status") == status, "report.card.status.link", label)
    for field in ("obligations", "paths", "issues", "diagnostics"):
        check(isinstance(payload.get(field), list), f"card.{field}.type", label)
    check(isinstance(payload.get("card_support_manifest_delta"), dict),
          "card.manifest.type", label)
    obligations = payload.get("obligations", [])
    paths = payload.get("paths", [])
    obligation_ids = [item.get("id") for item in obligations if isinstance(item, dict)]
    path_ids = [item.get("id") for item in paths if isinstance(item, dict)]
    check(len(obligation_ids) == len(obligations), "card.obligations.objects", label)
    check(len(obligation_ids) == len(set(obligation_ids)), "card.obligations.unique", label)
    check(len(path_ids) == len(paths), "card.paths.objects", label)
    check(len(path_ids) == len(set(path_ids)), "card.paths.unique", label)
    for item in obligations:
        if isinstance(item, dict):
            check(item.get("status") in OBLIGATION_TERMINAL,
                  "card.obligation.status", f"{label}: {item.get('id')}")
    for item in paths:
        if isinstance(item, dict):
            check(item.get("status") in OBLIGATION_TERMINAL,
                  "card.path.status", f"{label}: {item.get('id')}")
    diagnostics = payload.get("diagnostics", [])
    manifest = payload.get("card_support_manifest_delta", {})
    auxiliary_counts["diagnostic_records"] += len(diagnostics)
    auxiliary_counts["manifest_card_names"] += len(manifest)
    auxiliary_counts["manifest_report_emissions"] += sum(
        int(value.get("count", 0)) for value in manifest.values()
        if isinstance(value, dict)
    )
    inspect_state_hashes(payload, artifact)
    if isinstance(card.get("name"), str):
        by_name[card["name"]] = payload
    card_canonical_map[artifact] = calculated
    card_physical_map[artifact] = physical_sha256(path)

counts = {status: 0 for status in sorted(TERMINAL)}
by_ledger = {}
obligation_counts = {}
failed_surfaces = {}
diagnostic_cards = 0
manifest_cards = 0
for payload in results:
    status = payload.get("runtime_status", payload.get("status"))
    if status in counts:
        counts[status] += 1
    ledger_status = str((payload.get("card") or {}).get("ledger_status", "unknown"))
    ledger_counts = by_ledger.setdefault(
        ledger_status, {key: 0 for key in sorted(TERMINAL)}
    )
    if status in ledger_counts:
        ledger_counts[status] += 1
    for obligation in payload.get("obligations", []):
        if not isinstance(obligation, dict):
            continue
        kind = str(obligation.get("kind", "unknown"))
        obligation_status = str(obligation.get("status", "unknown"))
        kind_counts = obligation_counts.setdefault(kind, {})
        kind_counts[obligation_status] = kind_counts.get(obligation_status, 0) + 1
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict) or issue.get("severity") != "failed":
            continue
        surface = str(issue.get("surface", "unknown"))
        failed_surfaces[surface] = failed_surfaces.get(surface, 0) + 1
    diagnostic_cards += int(bool(payload.get("diagnostics")))
    manifest_cards += int(bool(payload.get("card_support_manifest_delta")))

recomputed_summary = {
    "total": len(results),
    "runtime_status_counts": counts,
    "runtime_status_by_ledger_status": by_ledger,
    "obligation_status_counts": obligation_counts,
    "failed_surface_counts": failed_surfaces,
    "cards_with_diagnostics": diagnostic_cards,
    "cards_with_manifest_reports": manifest_cards,
}
check(report.get("summary") == recomputed_summary, "report.summary.recompute",
      "stored summary differs")
check(run.get("summary") == recomputed_summary, "run.summary.recompute",
      "stored summary differs")

wanted_cards = scope.get("cards", []) if isinstance(scope.get("cards"), list) else []
wanted_statuses = scope.get("statuses", []) if isinstance(scope.get("statuses"), list) else []
if wanted_cards:
    check({name.casefold() for name in wanted_cards} ==
          {str(name).casefold() for name in names},
          "scope.cards.selection", "scope/report names differ")
if wanted_statuses:
    check(all(row.get("ledger_status") in wanted_statuses for row in rows),
          "scope.statuses.selection", "ledger status outside scope")
lower, upper = scope.get("from_index"), scope.get("to_index")
if lower is not None:
    check(all(index >= lower for index in indices), "scope.from_index.selection", repr(lower))
if upper is not None:
    check(all(index <= upper for index in indices), "scope.to_index.selection", repr(upper))
shard_index, shard_count = scope.get("shard_index"), scope.get("shard_count")
check(isinstance(shard_count, int) and shard_count >= 1,
      "scope.shard_count", repr(shard_count))
check(isinstance(shard_index, int) and isinstance(shard_count, int) and
      0 <= shard_index < shard_count, "scope.shard_index",
      f"{shard_index}/{shard_count}")
if isinstance(shard_index, int) and isinstance(shard_count, int) and shard_count >= 1:
    check(all(index % shard_count == shard_index for index in indices),
          "scope.shard.selection", f"{shard_index}/{shard_count}")

# Optional verification against the repo inputs. This imports no Playersim code.
input_verification = {"requested": verify_inputs, "repo": str(repo), "checked": False}
if verify_inputs:
    fmt = scope.get("format")
    snapshot_id = identity.get("snapshot", {})
    registry_id = identity.get("registry", {})
    ledger_id = identity.get("ledger", {})
    probe_id = identity.get("probe_source", {})
    snapshot_path = repo / "Format Card Lists" / str(snapshot_id.get("path", ""))
    registry_path = repo / "formats" / str(fmt) / str(registry_id.get("path", ""))
    ledger_path = repo / "formats" / str(fmt) / str(ledger_id.get("path", ""))
    probe_source_path = repo / "Playersim" / str(probe_id.get("path", ""))
    for label, path, item in (
        ("snapshot", snapshot_path, snapshot_id),
        ("registry", registry_path, registry_id),
        ("ledger", ledger_path, ledger_id),
        ("probe_source", probe_source_path, probe_id),
    ):
        check(path.is_file(), f"input.{label}.file", str(path))
        if path.is_file():
            check(item.get("path") == path.name, f"input.{label}.basename", str(path))
            check(item.get("sha256") == physical_sha256(path),
                  f"input.{label}.physical_sha", str(path))
    if snapshot_path.is_file():
        check(snapshot_id.get("size_bytes") == snapshot_path.stat().st_size,
              "input.snapshot.size", str(snapshot_path))
    registry_data = load_object(registry_path, "registry input") if registry_path.is_file() else {}
    ledger_data = load_object(ledger_path, "ledger input") if ledger_path.is_file() else {}
    if registry_data:
        check(registry_id.get("declared_sha256") == registry_data.get("sha256"),
              "input.registry.declared_link", str(registry_path))
        check(registry_id.get("cards") == len(registry_data.get("cards", [])),
              "input.registry.card_count", str(registry_path))
        dense = [row.get("index") for row in registry_data.get("cards", [])]
        check(dense == list(range(len(dense))), "input.registry.dense_indices",
              str(registry_path))
    if ledger_data:
        check(ledger_id.get("declared_sha256") == ledger_data.get("sha256"),
              "input.ledger.declared_link", str(ledger_path))
        check(ledger_id.get("cards") == len(ledger_data.get("cards", [])),
              "input.ledger.card_count", str(ledger_path))
    engine_id = identity.get("engine_source", {})
    engine_root = repo / "Playersim"
    engine_files = sorted(engine_root.rglob("*.py"))
    engine_hashes = {
        path.relative_to(engine_root).as_posix(): physical_sha256(path)
        for path in engine_files
    }
    check(engine_id.get("scope") == "Playersim/**/*.py", "input.engine.scope",
          repr(engine_id.get("scope")))
    check(engine_id.get("file_count") == len(engine_hashes), "input.engine.file_count",
          f"stored={engine_id.get('file_count')} actual={len(engine_hashes)}")
    check(engine_id.get("sha256") == canonical_sha256({"files": engine_hashes}),
          "input.engine.canonical_manifest", str(engine_root))
    check(identity.get("python_version") == sys.version.split()[0],
          "input.python_version",
          f"stored={identity.get('python_version')} actual={sys.version.split()[0]}")

    raw_rows = []
    if snapshot_path.is_file():
        for line_number, line in enumerate(
                snapshot_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except Exception as exc:
                fail("input.snapshot.jsonl", f"line {line_number}: {exc}")
                continue
            if not isinstance(raw, dict):
                fail("input.snapshot.object", f"line {line_number}")
                continue
            if raw.get("lang", "en") != "en":
                continue
            if fmt and raw.get("legalities", {}).get(fmt) != "legal":
                continue
            raw_rows.append(raw)
    raw_by_name = {}
    for raw in raw_rows:
        raw_by_name.setdefault(str(raw.get("name", "")).casefold(), raw)
    registry_by_name = {
        str(row.get("name", "")).casefold(): row
        for row in registry_data.get("cards", [])
    }
    ledger_by_name = {
        str(row.get("name", "")).casefold(): row
        for row in ledger_data.get("cards", [])
    }
    wanted_names = {str(name).casefold() for name in scope.get("cards", [])}
    wanted_statuses = {str(status) for status in scope.get("statuses", [])}
    selected = []
    for key, raw in raw_by_name.items():
        name = str(raw.get("name", ""))
        registry_row = registry_by_name.get(key)
        ledger_row = ledger_by_name.get(key)
        if registry_row is None or ledger_row is None:
            fail("input.selection.join_missing", name)
            continue
        index = int(registry_row["index"])
        ledger_status = str(ledger_row.get("status", "unknown"))
        if wanted_names and key not in wanted_names:
            continue
        if wanted_statuses and ledger_status not in wanted_statuses:
            continue
        if lower is not None and index < lower:
            continue
        if upper is not None and index > upper:
            continue
        if index % shard_count != shard_index:
            continue
        selected.append((index, name, ledger_status, registry_row, raw))
    selected.sort(key=lambda item: item[0])
    expected_rows = [(item[0], item[1], item[2]) for item in selected]
    actual_rows = [
        (row.get("index"), row.get("name"), row.get("ledger_status"))
        for row in rows
    ]
    check(actual_rows == expected_rows, "input.selection.exact",
          f"expected={len(expected_rows)} actual={len(actual_rows)}")
    for index, name, ledger_status, registry_row, raw in selected:
        payload = by_name.get(name)
        if payload is None:
            continue
        card = payload.get("card", {})
        check(card.get("oracle_id") == registry_row.get("oracle_id"),
              "input.card.oracle_id", name)
        check(card.get("layout") == raw.get("layout", "normal"),
              "input.card.layout", name)
        check(card.get("oracle_sha256") == canonical_sha256({"oracle_row": raw}),
              "input.card.oracle_sha", name)
    input_verification.update(
        checked=True,
        snapshot_selected_cards=len(selected),
        engine_files=len(engine_files),
    )

# Frozen exact 20-card / 22-clause grouped batch inventory.
grouped_expected = {
    "Twilight Diviner": [("whenever one or more other creatures you control enter", "grouped_etb_batch")],
    "Builder's Talent": [("whenever one or more noncreature, nonland permanents you control enter", "grouped_etb_batch")],
    "G'raha Tia": [("whenever one or more other creatures and/or artifacts you control die", "grouped_dies_batch")],
    "Chainsaw": [("whenever one or more creatures die", "grouped_dies_batch")],
    "Kambal, Profiteering Mayor": [
        ("whenever one or more tokens your opponents control enter", "grouped_etb_batch"),
        ("whenever one or more tokens you control enter", "grouped_etb_batch"),
    ],
    "Extraordinary Journey": [("whenever one or more nontoken creatures enter", "grouped_etb_batch")],
    "Satoru, the Infiltrator": [("whenever satoru and/or one or more other nontoken creatures you control enter", "grouped_etb_batch")],
    "Mister Fantastic, Reed Richards": [("whenever one or more tokens you control enter", "grouped_etb_batch")],
    "Valley Questcaller": [("whenever one or more other rabbits, bats, birds, and/or mice you control enter", "grouped_etb_batch")],
    "Enduring Innocence": [("whenever one or more other creatures you control with power 2 or less enter", "grouped_etb_batch")],
    "Blood Spatter Analysis": [("whenever one or more creatures die", "grouped_dies_batch")],
    "Scavenger's Talent": [("whenever one or more creatures you control die", "grouped_dies_batch")],
    "The Skullspore Nexus": [("whenever one or more nontoken creatures you control die", "grouped_dies_batch")],
    "Vengeful Townsfolk": [("whenever one or more other creatures you control die", "grouped_dies_batch")],
    "Spiritcall Enthusiast // Scrollboost": [("whenever one or more tokens you control enter", "grouped_etb_batch")],
    "Caretaker's Talent": [("whenever one or more tokens you control enter", "grouped_etb_batch")],
    "Frantic Scapegoat": [("whenever one or more other creatures you control enter", "grouped_etb_batch")],
    "Elvish Archivist": [
        ("whenever one or more artifacts you control enter", "grouped_etb_batch"),
        ("whenever one or more enchantments you control enter", "grouped_etb_batch"),
    ],
    "Homicide Investigator": [("whenever one or more nontoken creatures you control die", "grouped_dies_batch")],
    "Baron Bertram Graywater": [("whenever one or more tokens you control enter", "grouped_etb_batch")],
}
grouped_results = []
grouped_ok = 0
grouped_representations = Counter()
for card_name, clauses in grouped_expected.items():
    payload = by_name.get(card_name)
    if payload is None:
        for condition, arm in clauses:
            group_fail("grouped.card.missing", card_name)
            grouped_results.append({"card": card_name, "surface": None, "arm": arm, "ok": False})
        continue
    discovered = payload.get("discovered", [])
    obligations = {
        item.get("id"): item for item in payload.get("obligations", [])
        if isinstance(item, dict)
    }
    paths = {
        item.get("id"): item for item in payload.get("paths", [])
        if isinstance(item, dict)
    }
    for condition, arm in clauses:
        matches = [
            item for item in discovered
            if isinstance(item, dict) and item.get("class") == "TriggeredAbility"
            and normalized(item.get("trigger_condition")) == normalized(condition)
        ]
        clause_errors = []

        def clause_check(ok, detail):
            if not ok:
                clause_errors.append(detail)
                group_fail("grouped.clause", f"{card_name} / {condition}: {detail}")

        # Builder's grouped trigger is a locked Level-2 Class row at the
        # probe's current Level 1. It must remain an explicit unmatched
        # printed-trigger gap; registering it here would itself be a bug.
        allow_level_gated_printed_gap = (
            card_name == "Builder's Talent"
            and normalized(condition) == normalized(
                "whenever one or more noncreature, nonland permanents you control enter"
            )
            and not matches
        )
        if allow_level_gated_printed_gap:
            printed_matches = [
                item for item in discovered
                if isinstance(item, dict) and item.get("class") == "printed_trigger"
                and normalized(item.get("trigger_condition")) == normalized(condition)
            ]
            clause_check(len(printed_matches) == 1,
                         f"expected one printed Level-2 clause, found {len(printed_matches)}")
            printed = printed_matches[0] if len(printed_matches) == 1 else {}
            clause_check(printed.get("matched_surface") is None,
                         f"printed matched_surface={printed.get('matched_surface')!r}")
            printed_id = printed.get("printed_trigger_id")
            printed_obligations = [
                item for item in payload.get("obligations", [])
                if isinstance(item, dict) and item.get("kind") == "printed_trigger"
                and item.get("printed_trigger_id") == printed_id
            ]
            clause_check(len(printed_obligations) == 1,
                         f"expected one printed-trigger obligation, found {len(printed_obligations)}")
            printed_obligation = (
                printed_obligations[0] if len(printed_obligations) == 1 else {}
            )
            clause_check(printed_obligation.get("status") == "coverage_gap",
                         f"printed obligation status={printed_obligation.get('status')!r}")
            clause_check(
                "did not reconcile to any registered runtime triggeredability"
                in normalized(printed_obligation.get("reason")),
                f"printed obligation reason={printed_obligation.get('reason')!r}",
            )
            printed_obligation_id = printed_obligation.get("id")
            clause_check(printed_obligation_id not in paths,
                         f"unexpected runtime path {printed_obligation_id}")
            ok = not clause_errors
            grouped_ok += int(ok)
            if ok:
                grouped_representations["level_gated_printed_gap"] += 1
            grouped_results.append({
                "card": card_name,
                "surface": printed_obligation_id,
                "arm": arm,
                "representation": "level_gated_printed_gap",
                "ok": ok,
            })
            continue

        clause_check(len(matches) == 1,
                     f"expected one discovered runtime surface, found {len(matches)}")
        surface_id = matches[0].get("id") if len(matches) == 1 else None
        obligation = obligations.get(surface_id)
        path = paths.get(surface_id)
        clause_check(isinstance(obligation, dict), f"missing obligation {surface_id}")
        clause_check(isinstance(path, dict), f"missing positive path {surface_id}")
        if isinstance(obligation, dict):
            clause_check(obligation.get("kind") == "triggered",
                         f"obligation kind={obligation.get('kind')!r}")
            clause_check(obligation.get("status") == "coverage_gap",
                         f"obligation status={obligation.get('status')!r}")
            clause_check(obligation.get("event_arm_count") == 1,
                         f"event_arm_count={obligation.get('event_arm_count')!r}")
            clause_check(obligation.get("exercised_event_arm_count") == 0,
                         "exercised_event_arm_count is not zero")
            clause_check(obligation.get("event_path_ids") == [surface_id],
                         f"event_path_ids={obligation.get('event_path_ids')!r}")
        if isinstance(path, dict):
            reason = normalized(path.get("reason"))
            clause_check(path.get("kind") == "triggered",
                         f"positive kind={path.get('kind')!r}")
            clause_check(path.get("status") == "coverage_gap",
                         f"positive status={path.get('status')!r}")
            clause_check(path.get("event_arm") == arm,
                         f"event_arm={path.get('event_arm')!r}")
            clause_check("complete atomic" in reason and "batch" in reason,
                         f"positive reason={path.get('reason')!r}")
            for forbidden in (
                    "event_fixture", "failure", "dispatch", "trace",
                    "desired_trigger_left_pipeline", "stack_identity_before_resolution"):
                clause_check(forbidden not in path,
                             f"positive path contains {forbidden}")
        negative_id = f"{surface_id}:negative_event" if surface_id else None
        negative_obligation = obligations.get(negative_id)
        negative_path = paths.get(negative_id)
        clause_check(isinstance(negative_obligation, dict),
                     f"missing negative obligation {negative_id}")
        clause_check(isinstance(negative_path, dict),
                     f"missing negative path {negative_id}")
        if isinstance(negative_obligation, dict):
            clause_check(negative_obligation.get("kind") == "negative_event",
                         f"negative obligation kind={negative_obligation.get('kind')!r}")
            clause_check(negative_obligation.get("status") == "coverage_gap",
                         f"negative obligation status={negative_obligation.get('status')!r}")
            clause_check(negative_obligation.get("matched_surface") == surface_id,
                         "negative obligation surface mismatch")
            clause_check(negative_obligation.get("negative_arm_count") == 1,
                         f"negative_arm_count={negative_obligation.get('negative_arm_count')!r}")
            clause_check(negative_obligation.get("exercised_negative_arm_count") == 0,
                         "exercised_negative_arm_count is not zero")
            clause_check(negative_obligation.get("negative_path_ids") == [negative_id],
                         f"negative_path_ids={negative_obligation.get('negative_path_ids')!r}")
        if isinstance(negative_path, dict):
            reason = normalized(negative_path.get("reason"))
            clause_check(negative_path.get("kind") == "negative_event",
                         f"negative kind={negative_path.get('kind')!r}")
            clause_check(negative_path.get("status") == "coverage_gap",
                         f"negative status={negative_path.get('status')!r}")
            clause_check(negative_path.get("matched_surface") == surface_id,
                         "negative path surface mismatch")
            clause_check("no deterministic close non-event fixture" in reason,
                         f"negative reason={negative_path.get('reason')!r}")
            for forbidden in (
                    "event_fixture", "failure", "dispatch", "trace",
                    "desired_trigger_left_pipeline", "stack_identity_before_resolution"):
                clause_check(forbidden not in negative_path,
                             f"negative path contains {forbidden}")
        ok = not clause_errors
        grouped_ok += int(ok)
        if ok:
            grouped_representations["runtime_atomic_gap"] += 1
        grouped_results.append(
            {"card": card_name, "surface": surface_id, "arm": arm,
             "representation": "runtime_atomic_gap", "ok": ok}
        )

physical_map = {
    rel: physical_sha256(root.joinpath(*PurePosixPath(rel).parts))
    for rel in sorted(expected_all_files & actual_all_files)
}
output = {
    "root": str(root),
    "ok": not errors,
    "errors": len(errors),
    "warnings": len(warnings),
    "error_samples": errors[:50],
    "warning_samples": warnings[:50],
    "artifact_counts": {
        "report_cards": len(rows),
        "loaded_cards": len(results),
        "expected_files": len(expected_all_files),
        "actual_files": len(actual_all_files),
        "missing_files": len(missing_files),
        "extra_files": len(extra_files),
    },
    "canonical_sha256": {
        "run": canonical_sha256(run),
        "report": canonical_sha256(report),
        "card_manifest": canonical_sha256({"files": card_canonical_map}),
    },
    "physical_sha256": {
        "run": physical_map.get("run.json"),
        "report": physical_map.get("card_probe_report.json"),
        "card_manifest": canonical_sha256({"files": card_physical_map}),
        "all_artifact_manifest": canonical_sha256({"files": physical_map}),
    },
    "summary_recomputed": recomputed_summary,
    "auxiliary_counts": dict(sorted(auxiliary_counts.items())),
    "state_hash_evidence": dict(sorted(state_evidence.items())),
    "input_verification": input_verification,
    "grouped_zone_change": {
        "mode": "observe" if observe_grouped else "require",
        "expected_cards": len(grouped_expected),
        "expected_clauses": sum(len(value) for value in grouped_expected.values()),
        "clauses_ok": grouped_ok,
        "violations": sum(not item["ok"] for item in grouped_results),
        "representation_counts": dict(sorted(grouped_representations.items())),
        "event_arm_counts": dict(Counter(
            item["arm"] for item in grouped_results if item["ok"]
        )),
        "clauses": grouped_results,
    },
}
print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
raise SystemExit(0 if not errors else 1)
