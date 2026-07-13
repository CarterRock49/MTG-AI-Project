"""Parallel, checkpoint-aware production harvest and promotion protocol.

Examples::

    python harvest_protocol.py harvest --games 256 --workers 4 \
        --agent-model models/candidate.zip --output harvest_runs/candidate

    python harvest_protocol.py promote --games 64 --workers 4 \
        --candidate models/candidate.zip --baseline models/champion.zip \
        --output harvest_runs/promotion_001

    python harvest_protocol.py qualify --games 64 --workers 4 \
        --candidate models/candidate.zip --minimum-score 0.55 \
        --output harvest_runs/qualification_001

Each worker owns an isolated tracker/card-memory directory.  The root protocol
manifest is written only after every strict shard validates successfully.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
import sys
import time
from typing import Sequence

import harvest_fixtures as fixture


PROTOCOL_VERSION = "harvest-protocol-v1"
SEVERITY_RANK = {"partial": 0, "unparsed": 1, "crash": 2}


def partition_games(games: int, workers: int) -> list[dict]:
    """Partition a global deterministic schedule without duplicate game IDs."""
    if isinstance(games, bool) or not isinstance(games, int) or games < 1:
        raise ValueError("games must be a positive integer")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    worker_count = min(games, workers)
    base, remainder = divmod(games, worker_count)
    shards = []
    offset = 0
    for shard_index in range(worker_count):
        count = base + (1 if shard_index < remainder else 0)
        shards.append({"shard": shard_index, "offset": offset, "games": count})
        offset += count
    return shards


def _merge_manifests(manifests: Sequence[dict]) -> dict:
    merged: dict[str, dict] = {}
    for manifest in manifests:
        for card_name, entry in manifest.items():
            target = merged.setdefault(card_name, {
                "count": 0, "severity": "partial", "reasons": {},
            })
            target["count"] += int(entry.get("count", 0))
            if SEVERITY_RANK.get(entry.get("severity"), 0) \
                    > SEVERITY_RANK.get(target["severity"], 0):
                target["severity"] = entry["severity"]
            for reason, count in entry.get("reasons", {}).items():
                target["reasons"][reason] = (
                    target["reasons"].get(reason, 0) + int(count))
    return merged


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _run_shard(arguments: dict) -> dict:
    output = Path(arguments["output"])
    result = fixture.run_harvest(
        arguments["games"], arguments["seed"], output,
        max_steps=arguments["max_steps"],
        game_offset=arguments["offset"],
        agent_model=arguments.get("agent_model"),
        opponent_model=arguments.get("opponent_model"),
        agent_is_p1=arguments.get("agent_is_p1", True),
        decks_directory=arguments.get("decks_directory"),
        format_name=arguments.get("format_name"),
        format_dir=arguments.get("format_dir"),
    )
    return {
        "shard": arguments["shard"],
        "offset": arguments["offset"],
        "games": arguments["games"],
        "output": output.name,
        "agent_version": result["agent_version"],
        "results": dict(Counter(
            record["result"] for record in result["records"])),
        "records": result["records"],
        "fidelity": result["fidelity"],
        "manifest": result["manifest"],
        "agent_policy": result["run_manifest"].get("agent_policy"),
        "opponent_policy": result["run_manifest"].get("opponent_policy"),
        "lineage": result["run_manifest"].get("lineage"),
        "decks": result["run_manifest"].get("decks"),
    }


def run_parallel_harvest(
    games: int,
    workers: int,
    seed: int,
    output_directory: Path,
    *,
    max_steps: int = fixture.MAX_STEPS_PER_GAME,
    agent_model: Path | str | None = None,
    opponent_model: Path | str | None = None,
    agent_is_p1: bool = True,
    decks_directory: Path | str | None = None,
    format_name: str | None = None,
    format_dir: Path | str | None = None,
) -> dict:
    """Run strict isolated shards and publish one success-only root manifest."""
    shards = partition_games(games, workers)
    output = fixture.prepare_output_directory(Path(output_directory))
    # Capture the expected checkpoint bytes before any worker loads them.  Each
    # shard reports the identity it actually stamped into its fixture run; a
    # mismatch means this was not one coherent policy evaluation.
    agent_identity = fixture.checkpoint_identity(agent_model) if agent_model else None
    opponent_identity = (
        fixture.checkpoint_identity(opponent_model) if opponent_model else None)
    expected_agent_policy = agent_identity or {"kind": "random-valid"}
    expected_opponent_policy = opponent_identity or {"kind": "scripted"}
    started = time.perf_counter()
    arguments = [
        {
            **shard,
            "seed": seed,
            "max_steps": max_steps,
            "output": str(output / f"shard_{shard['shard']:03d}"),
            "agent_model": str(agent_model) if agent_model else None,
            "opponent_model": str(opponent_model) if opponent_model else None,
            "agent_is_p1": agent_is_p1,
            "decks_directory": (
                str(decks_directory) if decks_directory else None),
            "format_name": format_name,
            "format_dir": str(format_dir) if format_dir else None,
        }
        for shard in shards
    ]

    if len(arguments) == 1:
        results = [_run_shard(arguments[0])]
    else:
        results = []
        with ProcessPoolExecutor(max_workers=len(arguments)) as executor:
            futures = {executor.submit(_run_shard, item): item for item in arguments}
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda item: item["shard"])

    elapsed = max(time.perf_counter() - started, 1e-9)
    all_records = [record for shard in results for record in shard["records"]]
    if len(all_records) != games:
        raise RuntimeError(
            f"Parallel harvest returned {len(all_records)} records for {games} games")
    fidelity = {
        counter: sum(int(shard["fidelity"].get(counter, 0)) for shard in results)
        for counter in fixture.FIDELITY_COUNTERS
    }
    manifest = _merge_manifests([shard["manifest"] for shard in results])
    # Every shard loads the same corpus; a lineage mismatch means the run is
    # not one coherent stats scope and must not publish a root manifest.
    lineages = [shard.get("lineage") for shard in results]
    if any(lineage != lineages[0] for lineage in lineages[1:]):
        raise RuntimeError("Harvest shards disagree on corpus/format lineage")
    if any(shard.get("agent_policy") != expected_agent_policy
           for shard in results):
        raise RuntimeError(
            "Harvest shards disagree on the agent checkpoint identity")
    if any(shard.get("opponent_policy") != expected_opponent_policy
           for shard in results):
        raise RuntimeError(
            "Harvest shards disagree on the opponent checkpoint identity")
    # Catch a persistent replacement that occurs after workers start.  The
    # per-shard checks above also catch workers that observed different bytes.
    if agent_model and fixture.checkpoint_identity(agent_model) != agent_identity:
        raise RuntimeError("Agent checkpoint changed during parallel harvest")
    if opponent_model \
            and fixture.checkpoint_identity(opponent_model) != opponent_identity:
        raise RuntimeError("Opponent checkpoint changed during parallel harvest")
    protocol_manifest = {
        "schema_version": 1,
        "status": "complete",
        "protocol_version": PROTOCOL_VERSION,
        "seed": seed,
        "games": games,
        "workers": len(results),
        "max_steps": max_steps,
        "elapsed_seconds": elapsed,
        "games_per_second": games / elapsed,
        "agent_policy": expected_agent_policy,
        "opponent_policy": expected_opponent_policy,
        "agent_seat": "p1" if agent_is_p1 else "p2",
        "lineage": lineages[0],
        "decks": results[0].get("decks"),
        "results": dict(sorted(Counter(
            record["result"] for record in all_records).items())),
        "fidelity": fidelity,
        "manifest_entries": len(manifest),
        "shards": [
            {key: shard[key] for key in (
                "shard", "offset", "games", "output", "agent_version", "results")}
            for shard in results
        ],
    }
    _atomic_json(output / "harvest_protocol.json", protocol_manifest)
    _atomic_json(output / "card_support_manifest.json", manifest)
    return {
        "output": output,
        "records": all_records,
        "fidelity": fidelity,
        "manifest": manifest,
        "protocol_manifest": protocol_manifest,
    }


def _candidate_points(records: Sequence[dict], candidate_is_agent: bool) -> float:
    """Score agent-relative records for the policy occupying either role."""
    points = 0.0
    for record in records:
        result = record["result"]
        if result in {"draw", "draw_both_loss"}:
            points += 0.5
        elif (result == "win") == candidate_is_agent:
            points += 1.0
    return points


def run_promotion(
    candidate: Path | str,
    baseline: Path | str,
    games: int,
    workers: int,
    seed: int,
    output_directory: Path,
    *,
    minimum_score: float = 0.55,
    max_steps: int = fixture.MAX_STEPS_PER_GAME,
    decks_directory: Path | str | None = None,
    format_name: str | None = None,
    format_dir: Path | str | None = None,
) -> dict:
    """Run paired seats and issue a deterministic promotion decision."""
    if games < 2 or games % 2:
        raise ValueError("promotion games must be an even integer of at least 2")
    if not 0.5 <= minimum_score <= 1.0:
        raise ValueError("minimum_score must be between 0.5 and 1.0")
    output = fixture.prepare_output_directory(Path(output_directory))
    games_per_seat = games // 2
    corpus_kwargs = {
        "decks_directory": decks_directory,
        "format_name": format_name,
        "format_dir": format_dir,
    }
    candidate_p1 = run_parallel_harvest(
        games_per_seat, workers, seed, output / "candidate_p1",
        max_steps=max_steps, agent_model=candidate, opponent_model=baseline,
        **corpus_kwargs)
    candidate_p2 = run_parallel_harvest(
        games_per_seat, workers, seed, output / "candidate_p2",
        max_steps=max_steps, agent_model=baseline, opponent_model=candidate,
        **corpus_kwargs)

    points = (
        _candidate_points(candidate_p1["records"], True)
        + _candidate_points(candidate_p2["records"], False))
    score = points / games
    fidelity = {
        counter: (
            candidate_p1["fidelity"].get(counter, 0)
            + candidate_p2["fidelity"].get(counter, 0))
        for counter in fixture.FIDELITY_COUNTERS
    }
    merged_manifest = _merge_manifests([
        candidate_p1["manifest"], candidate_p2["manifest"]])
    severe_cards = sorted(
        name for name, entry in merged_manifest.items()
        if entry.get("severity") in {"unparsed", "crash"})
    fidelity_clean = not any(fidelity.values()) and not severe_cards
    promoted = fidelity_clean and score >= minimum_score
    decision = {
        "schema_version": 1,
        "status": "complete",
        "protocol_version": PROTOCOL_VERSION,
        "lineage": candidate_p1.get("protocol_manifest", {}).get("lineage"),
        "candidate": fixture.checkpoint_identity(candidate),
        "baseline": fixture.checkpoint_identity(baseline),
        "games": games,
        "games_per_seat": games_per_seat,
        "seed": seed,
        "candidate_points": points,
        "candidate_score": score,
        "minimum_score": minimum_score,
        "fidelity": fidelity,
        "severe_manifest_cards": severe_cards,
        "promote": promoted,
        "decision": "promote" if promoted else "reject",
    }
    _atomic_json(output / "promotion.json", decision)
    return decision


def _qualification_leg(
    result: dict,
    *,
    games: int,
    seat: str,
    candidate_identity: dict,
) -> tuple[dict, dict]:
    """Validate one completed qualification leg before publishing a decision."""
    protocol_manifest = result.get("protocol_manifest")
    if not isinstance(protocol_manifest, dict) \
            or protocol_manifest.get("status") != "complete":
        raise RuntimeError(
            f"Candidate {seat} qualification leg did not complete its protocol")
    records = result.get("records")
    if not isinstance(records, list) or len(records) != games:
        raise RuntimeError(
            f"Candidate {seat} qualification leg returned the wrong game count")
    expected_agent_is_p1 = seat == "p1"
    if any(record.get("agent_is_p1") is not expected_agent_is_p1
           for record in records):
        raise RuntimeError(
            f"Candidate {seat} qualification leg contains a mismatched game seat")
    if protocol_manifest.get("agent_policy") != candidate_identity:
        raise RuntimeError(
            f"Candidate checkpoint identity changed in the {seat} qualification leg")
    if protocol_manifest.get("agent_seat") != seat:
        raise RuntimeError(
            f"Candidate {seat} qualification leg recorded the wrong policy seat")
    return protocol_manifest, result


def run_qualification(
    candidate: Path | str,
    games: int,
    workers: int,
    seed: int,
    output_directory: Path,
    *,
    minimum_score: float = 0.55,
    max_steps: int = fixture.MAX_STEPS_PER_GAME,
    decks_directory: Path | str | None = None,
    format_name: str | None = None,
    format_dir: Path | str | None = None,
) -> dict:
    """Gate a checkpoint against scripted play in equal, paired seats.

    A 0.55 default requires evidence of an edge rather than merely tying the
    baseline. The decision is published only after both strict legs complete,
    agree on lineage/checkpoint identity, and yield validated game artifacts.
    """
    if games < 2 or games % 2:
        raise ValueError("qualification games must be an even integer of at least 2")
    if not 0.5 <= minimum_score <= 1.0:
        raise ValueError("minimum_score must be between 0.5 and 1.0")

    output = fixture.prepare_output_directory(Path(output_directory))
    candidate_identity = fixture.checkpoint_identity(candidate)
    games_per_seat = games // 2
    corpus_kwargs = {
        "decks_directory": decks_directory,
        "format_name": format_name,
        "format_dir": format_dir,
    }
    candidate_p1 = run_parallel_harvest(
        games_per_seat, workers, seed, output / "candidate_p1",
        max_steps=max_steps, agent_model=candidate, agent_is_p1=True,
        **corpus_kwargs)
    candidate_p2 = run_parallel_harvest(
        games_per_seat, workers, seed, output / "candidate_p2",
        max_steps=max_steps, agent_model=candidate, agent_is_p1=False,
        **corpus_kwargs)

    p1_protocol, candidate_p1 = _qualification_leg(
        candidate_p1, games=games_per_seat, seat="p1",
        candidate_identity=candidate_identity)
    p2_protocol, candidate_p2 = _qualification_leg(
        candidate_p2, games=games_per_seat, seat="p2",
        candidate_identity=candidate_identity)
    lineage = p1_protocol.get("lineage")
    if lineage != p2_protocol.get("lineage"):
        raise RuntimeError("Qualification seat legs disagree on corpus/format lineage")
    if fixture.checkpoint_identity(candidate) != candidate_identity:
        raise RuntimeError("Candidate checkpoint changed during qualification")

    records = candidate_p1["records"] + candidate_p2["records"]
    raw_results = Counter(record["result"] for record in records)
    outcome_counts = {
        "wins": raw_results.get("win", 0),
        "losses": raw_results.get("loss", 0),
        "draws": raw_results.get("draw", 0)
                 + raw_results.get("draw_both_loss", 0),
    }
    points = _candidate_points(records, True)
    score = points / games
    fidelity = {
        counter: (
            candidate_p1["fidelity"].get(counter, 0)
            + candidate_p2["fidelity"].get(counter, 0))
        for counter in fixture.FIDELITY_COUNTERS
    }
    merged_manifest = _merge_manifests([
        candidate_p1["manifest"], candidate_p2["manifest"]])
    severe_cards = sorted(
        name for name, entry in merged_manifest.items()
        if entry.get("severity") in {"unparsed", "crash"})
    fidelity_clean = not any(fidelity.values()) and not severe_cards
    score_passed = score >= minimum_score
    passed = fidelity_clean and score_passed
    decision = {
        "schema_version": 1,
        "status": "complete",
        "protocol_version": PROTOCOL_VERSION,
        "lineage": lineage,
        "candidate": candidate_identity,
        "opponent": {"kind": "scripted"},
        "games": games,
        "games_per_seat": games_per_seat,
        "seed": seed,
        "result_counts": dict(sorted(raw_results.items())),
        "outcome_counts": outcome_counts,
        "candidate_points": points,
        "candidate_score": score,
        "minimum_score": minimum_score,
        "score_passed": score_passed,
        "fidelity": fidelity,
        "severe_manifest_cards": severe_cards,
        "fidelity_clean": fidelity_clean,
        "passed": passed,
        "decision": "pass" if passed else "fail",
    }
    _atomic_json(output / "qualification.json", decision)
    return decision


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _add_corpus_arguments(subparser) -> None:
    subparser.add_argument(
        "--decks", type=Path, default=None,
        help="deck corpus directory (default: the audited eight-deck fixture)")
    subparser.add_argument(
        "--format", default=None,
        help="enforce strict format legality and use formats/<format>'s "
             "frozen registry and feature schema")
    subparser.add_argument(
        "--format-dir", type=Path, default=None,
        help="explicit frozen format-namespace directory "
             "(default: formats/<format> when --format is given)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    harvest = subparsers.add_parser("harvest", help="run strict parallel shards")
    harvest.add_argument("--games", type=_positive_int, required=True)
    harvest.add_argument("--workers", type=_positive_int, default=1)
    harvest.add_argument("--seed", type=int, default=fixture.DEFAULT_SEED)
    harvest.add_argument("--max-steps", type=_positive_int,
                         default=fixture.MAX_STEPS_PER_GAME)
    harvest.add_argument("--output", type=Path, required=True)
    harvest.add_argument("--agent-model", type=Path)
    harvest.add_argument("--opponent-model", type=Path)
    _add_corpus_arguments(harvest)

    promote = subparsers.add_parser(
        "promote", help="benchmark a candidate against a baseline in both seats")
    promote.add_argument("--candidate", type=Path, required=True)
    promote.add_argument("--baseline", type=Path, required=True)
    promote.add_argument("--games", type=_positive_int, required=True)
    promote.add_argument("--workers", type=_positive_int, default=1)
    promote.add_argument("--seed", type=int, default=fixture.DEFAULT_SEED)
    promote.add_argument("--minimum-score", type=float, default=0.55)
    promote.add_argument("--max-steps", type=_positive_int,
                         default=fixture.MAX_STEPS_PER_GAME)
    promote.add_argument("--output", type=Path, required=True)
    _add_corpus_arguments(promote)

    qualify = subparsers.add_parser(
        "qualify", help="gate a candidate against scripted play in both seats")
    qualify.add_argument("--candidate", type=Path, required=True)
    qualify.add_argument("--games", type=_positive_int, required=True)
    qualify.add_argument("--workers", type=_positive_int, default=1)
    qualify.add_argument("--seed", type=int, default=fixture.DEFAULT_SEED)
    qualify.add_argument("--minimum-score", type=float, default=0.55)
    qualify.add_argument("--max-steps", type=_positive_int,
                         default=fixture.MAX_STEPS_PER_GAME)
    qualify.add_argument("--output", type=Path, required=True)
    _add_corpus_arguments(qualify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "harvest":
            result = run_parallel_harvest(
                args.games, args.workers, args.seed, args.output,
                max_steps=args.max_steps, agent_model=args.agent_model,
                opponent_model=args.opponent_model,
                decks_directory=args.decks, format_name=args.format,
                format_dir=args.format_dir)
            summary = result["protocol_manifest"]
            print(
                f"Harvest complete: games={summary['games']} "
                f"workers={summary['workers']} "
                f"games_per_second={summary['games_per_second']:.3f}")
        elif args.command == "promote":
            decision = run_promotion(
                args.candidate, args.baseline, args.games, args.workers,
                args.seed, args.output, minimum_score=args.minimum_score,
                max_steps=args.max_steps, decks_directory=args.decks,
                format_name=args.format, format_dir=args.format_dir)
            print(
                f"Promotion {decision['decision']}: "
                f"score={decision['candidate_score']:.3f} "
                f"threshold={decision['minimum_score']:.3f}")
        else:
            decision = run_qualification(
                args.candidate, args.games, args.workers, args.seed, args.output,
                minimum_score=args.minimum_score, max_steps=args.max_steps,
                decks_directory=args.decks, format_name=args.format,
                format_dir=args.format_dir)
            print(
                f"Qualification {decision['decision']}: "
                f"score={decision['candidate_score']:.3f} "
                f"threshold={decision['minimum_score']:.3f} "
                f"fidelity_clean={decision['fidelity_clean']}")
            if not decision["passed"]:
                return 2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
