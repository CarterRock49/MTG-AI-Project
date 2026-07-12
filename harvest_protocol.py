"""Parallel, checkpoint-aware production harvest and promotion protocol.

Examples::

    python harvest_protocol.py harvest --games 256 --workers 4 \
        --agent-model models/candidate.zip --output harvest_runs/candidate

    python harvest_protocol.py promote --games 64 --workers 4 \
        --candidate models/candidate.zip --baseline models/champion.zip \
        --output harvest_runs/promotion_001

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
    decks_directory: Path | str | None = None,
    format_name: str | None = None,
    format_dir: Path | str | None = None,
) -> dict:
    """Run strict isolated shards and publish one success-only root manifest."""
    shards = partition_games(games, workers)
    output = fixture.prepare_output_directory(Path(output_directory))
    started = time.perf_counter()
    arguments = [
        {
            **shard,
            "seed": seed,
            "max_steps": max_steps,
            "output": str(output / f"shard_{shard['shard']:03d}"),
            "agent_model": str(agent_model) if agent_model else None,
            "opponent_model": str(opponent_model) if opponent_model else None,
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
    agent_identity = fixture.checkpoint_identity(agent_model) if agent_model else None
    opponent_identity = (
        fixture.checkpoint_identity(opponent_model) if opponent_model else None)
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
        "agent_policy": agent_identity or {"kind": "random-valid"},
        "opponent_policy": opponent_identity or {"kind": "scripted"},
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
        else:
            decision = run_promotion(
                args.candidate, args.baseline, args.games, args.workers,
                args.seed, args.output, minimum_score=args.minimum_score,
                max_steps=args.max_steps, decks_directory=args.decks,
                format_name=args.format, format_dir=args.format_dir)
            print(
                f"Promotion {decision['decision']}: "
                f"score={decision['candidate_score']:.3f} "
                f"threshold={decision['minimum_score']:.3f}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
