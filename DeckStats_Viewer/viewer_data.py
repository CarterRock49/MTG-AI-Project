"""Read-only data access for the DeckStats viewer.

The viewer consumes artifacts that are already on disk.  This module deliberately
has no web-framework dependency and never accepts a filesystem path from callers;
public lookups use IDs created while scanning ``project_root``.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping


_MISSING = object()


def _json_safe(value: Any) -> Any:
    """Return a detached value accepted by strict JSON encoders."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=repr)
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sortable_float(value: Any) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return converted if math.isfinite(converted) else 0.0


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "p1", "player1", "player_1", "1"}:
            return True
        if lowered in {"false", "p2", "player2", "player_2", "0"}:
            return False
    return None


class ViewerRepository:
    """Cached, read-only index of Playersim run and statistics artifacts."""

    MAX_GAME_PAGE = 5_000
    _PAGE_CACHE_SIZE = 64
    _IGNORED_DIRECTORIES = frozenset({
        ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache",
        "__pycache__", "MTGenv", "node_modules", ".venv", "venv",
    })
    # These are generated test outputs, not user runs.  Keep the exclusion
    # precise so a real project directory merely named ``tests`` remains
    # inspectable while the repository's synthetic DeckStats fixtures never
    # appear beside production scopes.
    _IGNORED_ARTIFACT_ROOTS = (("tests", "test_artifacts"),)
    _HARVEST_MANIFESTS = {
        "harvest_run.json": "harvest_run",
        "harvest_protocol.json": "harvest_protocol",
        "promotion.json": "promotion",
    }

    def __init__(self, project_root: str | os.PathLike[str]):
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Viewer project root is not a directory: {root}")
        self.project_root = root
        self._lock = threading.RLock()
        self._json_cache: dict[Path, tuple[tuple[int, int, int], Any, Any]] = {}
        self._jsonl_indexes: dict[
            Path, tuple[tuple[int, int, int], tuple[tuple[int, int], ...]]
        ] = {}
        self._jsonl_page_cache: OrderedDict[tuple[Any, ...], tuple[Any, Any]] = \
            OrderedDict()
        self._evaluation_join_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._runs_by_id: dict[str, dict[str, Any]] = {}
        self._stats_by_id: dict[str, dict[str, Any]] = {}
        self._harvest_by_id: dict[str, dict[str, Any]] = {}
        self._scan_errors: list[dict[str, Any]] = []
        self._refreshed_at: str | None = None
        self._stats_summary_cache: list[dict[str, Any]] = []
        self._overview_cache: dict[str, Any] = {}
        self.refresh()

    def refresh(self) -> dict[str, Any]:
        """Rescan artifact locations while retaining unchanged file caches."""
        with self._lock:
            discovered = self._scan_tree()
            self._scan_errors = discovered["errors"]
            self._build_runs(
                discovered["training_manifests"],
                discovered["evaluation_histories"],
            )
            self._build_stats_sources(discovered["stats_scopes"])
            self._build_harvests(discovered["harvest_manifests"])
            self._refreshed_at = datetime.now(timezone.utc).isoformat()
            self._stats_summary_cache = [
                self._stats_source_summary(source)
                for source in self._stats_by_id.values()
            ]
            live_paths = set(discovered["all_files"])
            self._json_cache = {
                path: cached for path, cached in self._json_cache.items()
                if path in live_paths or path.exists()
            }
            self._evaluation_join_cache.clear()
            self._overview_cache = self._compute_overview()
            return _json_safe(self._overview_cache)

    def overview(self) -> dict[str, Any]:
        """Return lightweight counts and status information for the landing page."""
        with self._lock:
            if not self._overview_cache:
                self._overview_cache = self._compute_overview()
            return _json_safe(self._overview_cache)

    def _compute_overview(self) -> dict[str, Any]:
        run_rows = self._run_summaries()
        source_rows = self._stats_summary_cache
        statuses = Counter(
            str(row.get("status") or "unknown") for row in run_rows)
        return {
            "project_root": self.project_root.name,
            "refreshed_at": self._refreshed_at,
            "scanned_at": self._refreshed_at,
            "run_count": len(run_rows),
            "stats_source_count": len(self._stats_by_id),
            "harvest_manifest_count": len(self._harvest_by_id),
            "evaluation_checkpoints": sum(
                int(row.get("evaluation_points") or 0) for row in run_rows),
            "evaluation_game_count": sum(
                int(row.get("evaluation_game_count") or 0)
                for row in run_rows),
            "stats_game_count": sum(
                int(row.get("game_count") or 0) for row in source_rows),
            "deck_count": sum(
                int(row.get("deck_count") or 0) for row in source_rows),
            "card_count": sum(
                int(row.get("card_count") or 0) for row in source_rows),
            "statuses": dict(sorted(statuses.items())),
            "latest_run_id": run_rows[0]["run_id"] if run_rows else None,
            "ignored_artifact_roots": [
                "/".join(parts) for parts in self._IGNORED_ARTIFACT_ROOTS
            ],
            "scan_errors": _json_safe(self._scan_errors),
            "diagnostics": _json_safe(self._scan_errors),
        }

    def runs(self) -> list[dict[str, Any]]:
        """Return deterministic summaries for all model/log run IDs."""
        with self._lock:
            return _json_safe(self._run_summaries())

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        """Return complete run manifest and evaluation history for a known ID."""
        with self._lock:
            record = self._runs_by_id.get(str(run_id))
            if record is None:
                return None
            summary = self._run_summary(record)
            manifest, manifest_error = self._read_json(record.get("manifest_path"))
            history, history_error = self._read_json(record.get("evaluation_path"))
            errors = list(record.get("errors") or [])
            errors.extend(item for item in (manifest_error, history_error) if item)
            source_ids = sorted(
                source_id for source_id, source in self._stats_by_id.items()
                if source.get("run_id") == record["run_id"]
            )
            return _json_safe({
                **summary,
                "manifest": manifest,
                "evaluation_history": history,
                "stats_source_ids": source_ids,
                "errors": errors,
            })

    def evaluation_games(self, run_id: str, *,
                         include_debug: bool = True) -> list[dict[str, Any]]:
        """Return raw evaluation episodes with normalized debugger fields.

        Direct callers retain the convenient eager default.  The HTTP list
        endpoint disables it so hundreds of compressed game traces are not
        inflated merely to render the master table; one selected game's debug
        payload is fetched through :meth:`evaluation_game_debug` instead.
        """
        with self._lock:
            record = self._runs_by_id.get(str(run_id))
            if record is None:
                return []
            history, _error = self._read_json(record.get("evaluation_path"))
            history_map = _as_mapping(history)
            evaluations = _as_list(history_map.get("evaluations"))
            if not evaluations and isinstance(history, list):
                if history and all(
                        isinstance(item, dict) and "episodes" not in item
                        for item in history):
                    evaluations = [{"episodes": history}]
                else:
                    evaluations = history
            if not evaluations and _as_list(history_map.get("episodes")):
                evaluations = [history_map]

            log_rows = self._evaluation_log_rows(record["run_id"])
            join = self._make_evaluation_join(log_rows)
            games: list[dict[str, Any]] = []
            for evaluation_index, raw_evaluation in enumerate(evaluations):
                evaluation = _as_mapping(raw_evaluation)
                episodes = _as_list(evaluation.get("episodes"))
                for episode_index, raw_episode in enumerate(episodes):
                    episode = _as_mapping(raw_episode)
                    normalized = self._normalize_evaluation_episode(
                        evaluation, episode, evaluation_index, episode_index
                    )
                    normalized["record_id"] = self._evaluation_record_id(
                        record["run_id"], normalized,
                    )
                    log_row, join_diagnostic = self._take_joined_game(
                        join, normalized)
                    normalized["game_log_join"] = join_diagnostic
                    if log_row is not None:
                        normalized["game_log"] = _json_safe(log_row)
                        normalized["game_id"] = _first(
                            episode.get("game_id"), log_row.get("game_id")
                        )
                        self._backfill_evaluation_from_log(normalized, log_row)
                        for key in (
                            "result", "terminal_reason", "turn_count", "fidelity",
                            "reward_components", "policy_state",
                        ):
                            if normalized.get(key) is None and log_row.get(key) is not None:
                                normalized[key] = _json_safe(log_row[key])
                    normalized["raw_episode"] = _json_safe(raw_episode)
                    availability = self._evaluation_availability(
                        episode, log_row or {})
                    normalized.update(availability)
                    if include_debug:
                        replay, replay_error, replay_artifact = \
                            self._evaluation_payload(
                            "replay", episode, log_row,
                            record.get("evaluation_path"))
                        debug, debug_error, debug_artifact = \
                            self._evaluation_payload(
                            "debug", episode, log_row,
                            record.get("evaluation_path"))
                        if replay is not None:
                            normalized["replay"] = replay
                        if debug is not None:
                            normalized["debug"] = debug
                            derived_summary = self._evaluation_debug_summary(
                                debug)
                            if derived_summary is not None:
                                normalized["debug_summary"] = {
                                    **_as_mapping(normalized.get(
                                        "debug_summary")),
                                    **derived_summary,
                                }
                            debug_source_key = (
                                debug_artifact.get("source_key")
                                if isinstance(debug_artifact, Mapping) else None
                            )
                            loaded_terminal = bool(
                                self._debug_has_terminal_payload(debug)
                                or debug_source_key in {
                                    "diagnostics", "policy_state",
                                }
                            )
                            loaded_trace = bool(
                                isinstance(debug, list)
                                or isinstance(debug, Mapping)
                                and debug.get("trace") is not None
                            )
                            loaded_replay = bool(
                                replay is not None
                                or isinstance(debug, Mapping)
                                and debug.get("replay") is not None
                            )
                            normalized.update({
                                "terminal_debug_available": loaded_terminal,
                                "trace_available": loaded_trace,
                                "replay_available": loaded_replay,
                                "debug_available": bool(
                                    loaded_terminal or loaded_trace),
                            })
                        if replay_error is not None:
                            normalized["replay_error"] = replay_error
                        if debug_error is not None:
                            normalized["debug_error"] = debug_error
                        if replay_artifact is not None:
                            normalized["replay_artifact"] = replay_artifact
                        if debug_artifact is not None:
                            normalized["debug_artifact"] = debug_artifact
                    games.append(_json_safe(normalized))
            return games

    def evaluation_game_debug(self, run_id: str, timestep: int,
                              case_index: int, *,
                              checkpoint_sha256: str | None = None,
                              record_id: str | None = None,
                              ) -> dict[str, Any] | None:
        """Inflate the replay/debug payload for exactly one evaluation case."""
        with self._lock:
            record = self._runs_by_id.get(str(run_id))
            if record is None:
                return None
            target_timestep = _integer(timestep)
            target_case = _integer(case_index)
            if target_timestep is None or target_case is None:
                raise ValueError(
                    "Evaluation timestep and case index must be integers")
            games = self.evaluation_games(run_id, include_debug=False)
            candidates = [
                item for item in games
                if _integer(item.get("evaluation_timestep")) == target_timestep
                and _integer(item.get("case_index")) == target_case
            ]
            requested_record = str(record_id or "").strip()
            if requested_record:
                candidates = [
                    item for item in candidates
                    if str(item.get("record_id") or "") == requested_record
                ]
            requested_checkpoint = str(checkpoint_sha256 or "").strip()
            if requested_checkpoint:
                candidates = [
                    item for item in candidates
                    if str(item.get("checkpoint_sha256") or "")
                    == requested_checkpoint
                ]
            if not candidates:
                return None
            if len(candidates) != 1:
                raise ValueError(
                    "Evaluation game selection is ambiguous; provide its "
                    "record_id and checkpoint_sha256"
                )
            game = candidates[0]
            episode = _as_mapping(game.get("raw_episode"))
            game_log = _as_mapping(game.get("game_log"))
            replay, replay_error, replay_artifact = self._evaluation_payload(
                "replay", episode, game_log, record.get("evaluation_path"))
            debug, debug_error, debug_artifact = self._evaluation_payload(
                "debug", episode, game_log, record.get("evaluation_path"))
            debug_source_key = (
                debug_artifact.get("source_key")
                if isinstance(debug_artifact, Mapping) else None
            )
            terminal_debug_available = bool(
                self._debug_has_terminal_payload(debug)
                or debug is not None and debug_source_key in {
                    "diagnostics", "policy_state",
                }
            )
            derived_summary = self._evaluation_debug_summary(debug)
            debug_summary = {
                **_as_mapping(game.get("debug_summary")),
                **_as_mapping(derived_summary),
            } or None
            return _json_safe({
                "run_id": str(run_id),
                "evaluation_timestep": target_timestep,
                "case_index": target_case,
                "record_id": game.get("record_id"),
                "checkpoint_sha256": game.get("checkpoint_sha256"),
                "debug_summary": debug_summary,
                "debug": debug,
                "replay": replay,
                "debug_available": debug is not None,
                "replay_available": bool(
                    replay is not None
                    or isinstance(debug, dict) and debug.get("replay") is not None
                ),
                "trace_available": bool(
                    isinstance(debug, list)
                    or isinstance(debug, dict) and debug.get("trace") is not None
                ),
                "terminal_debug_available": terminal_debug_available,
                "debug_error": debug_error,
                "replay_error": replay_error,
                "debug_artifact": debug_artifact,
                "replay_artifact": replay_artifact,
            })

    def stats_sources(self) -> list[dict[str, Any]]:
        """Return all discovered statistics scopes with opaque source IDs."""
        with self._lock:
            rows = list(self._stats_summary_cache)
            rows.sort(key=lambda row: (row["relative_path"].casefold(), row["id"]))
            return _json_safe(rows)

    def stats_bundle(self, source_id: str) -> dict[str, Any] | None:
        """Load aggregate, fidelity, support, and card-memory data for one scope."""
        with self._lock:
            source = self._stats_by_id.get(str(source_id))
            if source is None:
                return None
            base: Path = source["path"]
            errors: list[dict[str, Any]] = []

            def document(*names: str) -> Any:
                path = self._first_file(base, names)
                value, error = self._read_json(path)
                if error:
                    errors.append(error)
                return value

            decks, deck_files, deck_errors = self._load_json_directory(base / "decks")
            cards, card_files, card_errors = self._load_json_directory(base / "cards")
            meta_documents, meta_files, meta_errors = self._load_json_directory(base / "meta")
            errors.extend(deck_errors + card_errors + meta_errors)
            meta = None
            for filename, item in zip(meta_files, meta_documents):
                if Path(filename).name.startswith("meta_data.json"):
                    meta = item
                    break
            if meta is None and meta_documents:
                meta = meta_documents[0]

            memory_base = self._memory_base(base)
            memory_path = self._first_file(
                memory_base, ("all_cards.json.gz", "all_cards.json")
            )
            card_memory, memory_error = self._read_json(memory_path)
            if memory_error:
                errors.append(memory_error)
            card_memory_summary, card_memory_diagnostics = (
                self._summarize_card_memory(
                    source, memory_path, card_memory, cards,
                    load_error=memory_error,
                )
            )
            errors.extend(card_memory_diagnostics)

            strategy_path = self._first_file(
                memory_base,
                ("strategy_memory.json.gz", "strategy_memory.json"),
            )
            strategy_memory, strategy_error = self._read_json(strategy_path)
            if strategy_error:
                errors.append(strategy_error)
            unsafe_strategy_path = memory_base / "strategy_memory.pkl"
            if (not unsafe_strategy_path.is_file()
                    or not self._is_internal(unsafe_strategy_path)):
                unsafe_strategy_path = None
            strategy_summary, strategy_diagnostics = (
                self._summarize_strategy_memory(
                    source,
                    strategy_path,
                    strategy_memory,
                    unsafe_strategy_path,
                    load_error=strategy_error,
                )
            )
            errors.extend(strategy_diagnostics)

            game_path = base / "game_log.jsonl"
            total_games = self._jsonl_count(game_path) if game_path.is_file() else 0
            related_sources = [
                self._stats_source_summary(candidate)
                for candidate in self._stats_by_id.values()
                if candidate.get("id") == source.get("id") or (
                    source.get("run_id") is not None
                    and candidate.get("run_id") == source.get("run_id")
                    and candidate.get("kind") == source.get("kind")
                )
            ]
            related_sources.sort(key=lambda item: (
                str(item.get("scope") or "").casefold(),
                str(item.get("relative_path") or "").casefold(),
            ))
            return _json_safe({
                "source": self._stats_source_summary(source),
                "related_sources": {
                    "same_run_and_kind": related_sources,
                    "source_count": len(related_sources),
                    "total_game_count": sum(
                        int(item.get("game_count") or 0)
                        for item in related_sources),
                    "selected_source_id": source.get("id"),
                    "aggregation": "not_merged",
                },
                "game_count": total_games,
                "fidelity_report": document("fidelity_report.json.gz", "fidelity_report.json"),
                "card_support_manifest": document(
                    "card_support_manifest.json.gz", "card_support_manifest.json"
                ),
                "harvest_run": document("harvest_run.json.gz", "harvest_run.json"),
                "meta": meta,
                "meta_documents": meta_documents,
                "meta_files": meta_files,
                "decks": decks,
                "deck_files": deck_files,
                "cards": cards,
                "card_files": card_files,
                "card_memory": card_memory,
                "card_memory_file": self._relative(memory_path) if memory_path else None,
                "card_memory_summary": card_memory_summary,
                "strategy_memory": strategy_memory,
                "strategy_memory_file": (
                    self._relative(strategy_path) if strategy_path else None
                ),
                "strategy_memory_summary": strategy_summary,
                "errors": errors,
            })

    def stats_games(self, source_id: str, offset: int = 0,
                    limit: int = 200) -> dict[str, Any] | None:
        """Return one cached page from a source's append-only game log."""
        with self._lock:
            source = self._stats_by_id.get(str(source_id))
            if source is None:
                return None
            offset_value = max(0, _integer(offset) or 0)
            limit_value = max(0, _integer(limit) or 0)
            limit_value = min(limit_value, self.MAX_GAME_PAGE)
            path: Path = source["path"] / "game_log.jsonl"
            rows, total, errors = self._jsonl_page(path, offset_value, limit_value)
            return _json_safe({
                "source_id": source["id"],
                "offset": offset_value,
                "limit": limit_value,
                "total": total,
                "games": rows,
                "errors": errors,
            })

    def harvests(self) -> list[dict[str, Any]]:
        """Return every Harvest run/protocol/promotion manifest found under root."""
        with self._lock:
            rows = []
            for record in self._harvest_by_id.values():
                raw, error = self._read_json(record["path"])
                raw_map = _as_mapping(raw)
                source_ids = sorted(
                    source_id for source_id, source in self._stats_by_id.items()
                    if source["path"] == record["path"].parent
                    or record["path"].parent in source["path"].parents
                )
                rows.append({
                    "id": record["id"],
                    "kind": record["kind"],
                    "relative_path": record["relative_path"],
                    "directory": self._relative(record["path"].parent),
                    "status": raw_map.get("status"),
                    "games": raw_map.get("games"),
                    "seed": raw_map.get("seed"),
                    "source_ids": source_ids,
                    "raw": raw,
                    "errors": [error] if error else [],
                })
            rows.sort(key=lambda row: (row["relative_path"].casefold(), row["kind"]))
            return _json_safe(rows)

    # -- scanning ---------------------------------------------------------

    def _scan_tree(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "training_manifests": [],
            "evaluation_histories": [],
            "harvest_manifests": [],
            "stats_scopes": set(),
            "all_files": set(),
            "errors": [],
        }

        def on_error(error: OSError) -> None:
            result["errors"].append({
                "path": self._relative(Path(error.filename)) if error.filename else None,
                "error": type(error).__name__,
                "message": str(error),
            })

        for directory, child_names, file_names in os.walk(
                self.project_root, topdown=True, onerror=on_error, followlinks=False):
            current = Path(directory)

            def ignored_artifact_root(path: Path) -> bool:
                relative = tuple(
                    part.casefold() for part in self._relative_parts(path))
                return any(
                    relative[:len(prefix)] == tuple(
                        part.casefold() for part in prefix)
                    for prefix in self._IGNORED_ARTIFACT_ROOTS
                )

            child_names[:] = sorted(
                name for name in child_names
                if name not in self._IGNORED_DIRECTORIES
                and self._is_internal(current / name)
                and not ignored_artifact_root(current / name)
            )
            file_names = sorted(file_names)
            file_set = set(file_names)

            is_named_scope = current.name.casefold() == "deck_stats"
            is_direct_scope = (
                "game_log.jsonl" in file_set
                and bool(file_set.intersection({
                    "fidelity_report.json", "card_support_manifest.json",
                    "harvest_run.json",
                }) or {"decks", "cards", "meta"}.intersection(child_names))
            )
            if is_named_scope or is_direct_scope:
                result["stats_scopes"].add(current)

            for filename in file_names:
                path = current / filename
                if not self._is_internal(path):
                    continue
                result["all_files"].add(path)
                relative = self._relative_parts(path)
                if (filename == "training_run.json" and len(relative) >= 3
                        and relative[0].casefold() == "models"):
                    result["training_manifests"].append(path)
                elif (filename == "evaluations.json" and len(relative) >= 4
                      and relative[0].casefold() == "logs"
                      and relative[2].casefold() == "evaluation"):
                    result["evaluation_histories"].append(path)
                if filename in self._HARVEST_MANIFESTS:
                    result["harvest_manifests"].append(path)

            # Aggregate directories can contain tens of thousands of files but
            # no nested artifact scopes.  Bundle calls enumerate them lazily.
            if is_named_scope:
                child_names[:] = []
            elif is_direct_scope:
                child_names[:] = [
                    name for name in child_names
                    if name.casefold() not in {"cards", "decks", "meta", "card_memory"}
                ]

        for key in ("training_manifests", "evaluation_histories", "harvest_manifests"):
            result[key] = sorted(set(result[key]), key=self._path_sort_key)
        result["stats_scopes"] = sorted(result["stats_scopes"], key=self._path_sort_key)
        return result

    def _build_runs(self, manifest_paths: Iterable[Path],
                    evaluation_paths: Iterable[Path]) -> None:
        runs: dict[str, dict[str, Any]] = {}
        directory_aliases: dict[str, str] = {}
        for path in manifest_paths:
            manifest, error = self._read_json(path)
            manifest_map = _as_mapping(manifest)
            directory_id = path.parent.name
            raw_id = manifest_map.get("run_id")
            run_id = str(raw_id).strip() if raw_id is not None else directory_id
            if not run_id:
                run_id = directory_id
            directory_aliases[directory_id] = run_id
            record = runs.setdefault(run_id, {
                "run_id": run_id, "manifest_path": None,
                "evaluation_path": None, "errors": [],
            })
            if record["manifest_path"] is None:
                record["manifest_path"] = path
            else:
                record["errors"].append({
                    "path": self._relative(path),
                    "error": "DuplicateRunManifest",
                    "message": f"More than one training manifest declared run_id {run_id!r}",
                })
            if error:
                record["errors"].append(error)

        for path in evaluation_paths:
            directory_id = path.parent.parent.name
            run_id = directory_aliases.get(directory_id, directory_id)
            record = runs.setdefault(run_id, {
                "run_id": run_id, "manifest_path": None,
                "evaluation_path": None, "errors": [],
            })
            if record["evaluation_path"] is None:
                record["evaluation_path"] = path
            else:
                record["errors"].append({
                    "path": self._relative(path),
                    "error": "DuplicateEvaluationHistory",
                    "message": f"More than one evaluation history matched {run_id!r}",
                })
        self._runs_by_id = dict(sorted(runs.items(), key=lambda item: item[0].casefold()))

    def _build_stats_sources(self, paths: Iterable[Path]) -> None:
        sources: dict[str, dict[str, Any]] = {}
        for path in paths:
            relative = self._relative(path)
            digest = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()[:20]
            source_id = f"stats-{digest}"
            kind, run_id, scope = self._classify_stats_path(path)
            sources[source_id] = {
                "id": source_id,
                "path": path,
                "relative_path": relative,
                "kind": kind,
                "run_id": run_id,
                "scope": scope,
                "label": f"{run_id}: {scope}" if run_id else scope,
            }
        self._stats_by_id = dict(sorted(
            sources.items(), key=lambda item: item[1]["relative_path"].casefold()
        ))

    def _build_harvests(self, paths: Iterable[Path]) -> None:
        records: dict[str, dict[str, Any]] = {}
        for path in paths:
            relative = self._relative(path)
            kind = self._HARVEST_MANIFESTS[path.name]
            digest = hashlib.sha256(
                f"{kind}:{relative.casefold()}".encode("utf-8")
            ).hexdigest()[:20]
            identifier = f"harvest-{digest}"
            records[identifier] = {
                "id": identifier,
                "kind": kind,
                "path": path,
                "relative_path": relative,
            }
        self._harvest_by_id = dict(sorted(
            records.items(), key=lambda item: item[1]["relative_path"].casefold()
        ))

    # -- summaries and normalization -------------------------------------

    def _run_summaries(self) -> list[dict[str, Any]]:
        rows = [self._run_summary(record) for record in self._runs_by_id.values()]
        rows.sort(key=lambda row: (
            str(row.get("started_at") or row.get("updated_at") or ""),
            str(row.get("run_id") or "").casefold(),
        ), reverse=True)
        return rows

    def _run_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        manifest, manifest_error = self._read_json(record.get("manifest_path"))
        history, history_error = self._read_json(record.get("evaluation_path"))
        manifest_map = _as_mapping(manifest)
        history_map = _as_mapping(history)
        timestamps = _as_mapping(manifest_map.get("timestamps"))
        metrics = _as_mapping(manifest_map.get("metrics"))
        evaluations = _as_list(history_map.get("evaluations"))
        if not evaluations and isinstance(history, list):
            if history and all(
                    isinstance(item, dict) and "episodes" not in item
                    for item in history):
                evaluations = [{"episodes": history}]
            else:
                evaluations = history
        game_count = sum(
            len(_as_list(_as_mapping(item).get("episodes"))) for item in evaluations
        )
        latest_evaluation = _as_mapping(evaluations[-1]) if evaluations else {}
        summary = _as_mapping(latest_evaluation.get("summary"))
        errors = list(record.get("errors") or [])
        errors.extend(item for item in (manifest_error, history_error) if item)
        return _json_safe({
            "run_id": record["run_id"],
            "status": manifest_map.get("status") or "unknown",
            "phase": manifest_map.get("phase"),
            "project_version": manifest_map.get("project_version"),
            "started_at": _first(timestamps.get("started_at"), timestamps.get("created_at")),
            "updated_at": timestamps.get("updated_at"),
            "finished_at": timestamps.get("finished_at"),
            "duration_seconds": _first(
                timestamps.get("duration_seconds"), metrics.get("duration_seconds")
            ),
            "final_timesteps": metrics.get("final_timesteps"),
            "has_manifest": record.get("manifest_path") is not None,
            "has_evaluation": record.get("evaluation_path") is not None,
            "evaluation_points": len(evaluations),
            "evaluation_game_count": game_count,
            "latest_evaluation_timestep": latest_evaluation.get("timesteps"),
            "latest_evaluation_summary": summary or None,
            "best_timestep": history_map.get("best_timestep"),
            "best_candidate_timestep": history_map.get("best_candidate_timestep"),
            "manifest_path": self._relative(record.get("manifest_path")),
            "evaluation_path": self._relative(record.get("evaluation_path")),
            "error_count": len(errors),
        })

    def _stats_source_summary(self, source: dict[str, Any]) -> dict[str, Any]:
        base: Path = source["path"]
        game_path = base / "game_log.jsonl"
        game_count = self._jsonl_count(game_path) if game_path.is_file() else 0
        memory_base = self._memory_base(base)
        card_memory_path = self._first_file(
            memory_base, ("all_cards.json.gz", "all_cards.json")
        )
        strategy_memory_path = self._first_file(
            memory_base,
            ("strategy_memory.json.gz", "strategy_memory.json"),
        )
        unsafe_strategy_path = memory_base / "strategy_memory.pkl"
        has_unsafe_strategy = bool(
            unsafe_strategy_path.is_file()
            and self._is_internal(unsafe_strategy_path)
        )
        return {
            "id": source["id"],
            "label": source["label"],
            "kind": source["kind"],
            "run_id": source["run_id"],
            "scope": source["scope"],
            "relative_path": source["relative_path"],
            "game_count": game_count,
            "has_fidelity": (base / "fidelity_report.json").is_file()
                            or (base / "fidelity_report.json.gz").is_file(),
            "has_support_manifest": (base / "card_support_manifest.json").is_file()
                                    or (base / "card_support_manifest.json.gz").is_file(),
            "deck_count": self._json_document_count(base / "decks"),
            "card_count": self._json_document_count(base / "cards"),
            "has_card_memory": card_memory_path is not None,
            "card_memory_file": self._relative(card_memory_path),
            "has_strategy_memory_json": strategy_memory_path is not None,
            "strategy_memory_file": self._relative(strategy_memory_path),
            "has_unsafe_strategy_memory_pickle": has_unsafe_strategy,
        }

    @staticmethod
    def _memory_base(stats_base: Path) -> Path:
        """Return the auxiliary-memory directory for one statistics scope."""
        if stats_base.name.casefold() == "deck_stats":
            return stats_base.parent / "card_memory"
        return stats_base / "card_memory"

    def _adaptive_history_provenance(
            self, source: dict[str, Any]) -> dict[str, Any]:
        """Report decision-time history use only when a manifest records it."""
        kind = str(source.get("kind") or "").casefold()
        key = {
            "training": "training_adaptive_decision_history",
            "evaluation": "evaluation_adaptive_decision_history",
        }.get(kind)
        result = {
            "mode": "unknown",
            "enabled": None,
            "manifest_key": key,
            "source": "training_run.resolved" if key else None,
        }
        run_id = source.get("run_id")
        record = self._runs_by_id.get(str(run_id)) if run_id is not None else None
        if key is None or record is None:
            return result
        manifest, error = self._read_json(record.get("manifest_path"))
        if error or not isinstance(manifest, dict):
            return result
        resolved = _as_mapping(manifest.get("resolved"))
        if key not in resolved:
            return result
        raw = resolved.get(key)
        enabled = _boolean(raw)
        if enabled is None and isinstance(raw, str):
            lowered = raw.strip().casefold()
            if lowered in {"enabled", "adaptive", "adaptive_input"}:
                enabled = True
            elif lowered in {"disabled", "recorded_only"}:
                enabled = False
        result["raw"] = _json_safe(raw)
        if enabled is not None:
            result["enabled"] = enabled
            result["mode"] = "adaptive_input" if enabled else "recorded_only"
        return result

    def _strategy_memory_provenance(
            self, source: dict[str, Any]) -> dict[str, Any]:
        """Return the explicit run-manifest StrategyMemory configuration."""
        result = {
            "mode": "unknown",
            "enabled": None,
            "manifest_key": "strategy_memory",
            "source": "training_run.resolved",
        }
        run_id = source.get("run_id")
        record = self._runs_by_id.get(str(run_id)) if run_id is not None else None
        if record is None:
            return result
        manifest, error = self._read_json(record.get("manifest_path"))
        if error or not isinstance(manifest, dict):
            return result
        resolved = _as_mapping(manifest.get("resolved"))
        if "strategy_memory" not in resolved:
            return result
        raw = resolved.get("strategy_memory")
        enabled = _boolean(raw)
        if enabled is None and isinstance(raw, str):
            lowered = raw.strip().casefold()
            if lowered in {"enabled", "on"}:
                enabled = True
            elif lowered in {"disabled", "off"}:
                enabled = False
        result["raw"] = _json_safe(raw)
        if enabled is not None:
            result["enabled"] = enabled
            result["mode"] = "enabled" if enabled else "disabled"
        return result

    @staticmethod
    def _aggregate_card_id(record: Any) -> str | None:
        if not isinstance(record, dict):
            return None
        identifier = _first(record.get("card_id"), record.get("id"))
        if identifier is None or isinstance(identifier, bool):
            return None
        return str(identifier)

    @staticmethod
    def _counter_value(record: dict[str, Any], *names: str) -> int | None:
        for name in names:
            if name not in record or isinstance(record.get(name), bool):
                continue
            try:
                return int(record[name])
            except (TypeError, ValueError, OverflowError):
                return None
        return None

    @staticmethod
    def _card_memory_entry_issues(identifier: str,
                                  entry: Any) -> list[str]:
        """Validate the persisted CardMemory v1/v2 entry contract."""
        if not isinstance(entry, dict):
            return ["entry is not an object"]
        issues: list[str] = []
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append("name must be a non-empty string")
        stored_id = entry.get("id", _MISSING)
        if stored_id is _MISSING:
            issues.append("id is missing")
        elif isinstance(stored_id, bool) or str(stored_id) != identifier:
            issues.append("id does not match the canonical envelope key")

        counters: dict[str, int] = {}
        for field in (
                "games_played", "wins", "losses", "draws", "times_drawn",
                "times_played", "in_opening_hand", "wins_in_opening_hand",
                "draws_in_opening_hand"):
            raw = entry.get(field, _MISSING)
            if (raw is _MISSING or isinstance(raw, bool)
                    or not isinstance(raw, int) or raw < 0):
                issues.append(f"{field} must be a non-negative integer")
                continue
            counters[field] = raw
        if all(field in counters for field in (
                "games_played", "wins", "losses", "draws")):
            if (counters["wins"] + counters["losses"] + counters["draws"]
                    != counters["games_played"]):
                issues.append("wins + losses + draws must equal games_played")
        if "games_played" in counters:
            for field in ("times_drawn", "times_played", "in_opening_hand"):
                if counters.get(field, 0) > counters["games_played"]:
                    issues.append(f"{field} cannot exceed games_played")
        if all(field in counters for field in (
                "wins_in_opening_hand", "draws_in_opening_hand",
                "in_opening_hand")):
            if (counters["wins_in_opening_hand"]
                    + counters["draws_in_opening_hand"]
                    > counters["in_opening_hand"]):
                issues.append("opening-hand outcomes exceed opening samples")

        rating = entry.get("effectiveness_rating", _MISSING)
        try:
            rating_value = float(rating)
        except (TypeError, ValueError, OverflowError):
            rating_value = math.nan
        if (isinstance(rating, bool) or not math.isfinite(rating_value)
                or not 0.0 <= rating_value <= 1.0):
            issues.append("effectiveness_rating must be between 0 and 1")

        trend = entry.get("performance_trend", _MISSING)
        if not isinstance(trend, list):
            issues.append("performance_trend must be an array")
        else:
            invalid_trend = False
            for sample in trend:
                try:
                    numeric_sample = float(sample)
                except (TypeError, ValueError, OverflowError):
                    invalid_trend = True
                    break
                if (isinstance(sample, bool)
                        or not math.isfinite(numeric_sample)
                        or numeric_sample not in {0.0, 0.5, 1.0}):
                    invalid_trend = True
                    break
            if invalid_trend:
                issues.append("performance_trend contains a non-outcome value")
        for field in (
                "turn_played", "performance_by_turn",
                "mana_curve_performance", "archetype_performance",
                "synergy_partners", "meta_position"):
            if not isinstance(entry.get(field), dict):
                issues.append(f"{field} must be an object")

        def valid_bucket(bucket: Any, total_field: str,
                         outcome_fields: tuple[str, ...], *,
                         exact_total: bool) -> bool:
            if not isinstance(bucket, Mapping):
                return False
            fields = (total_field, *outcome_fields)
            if any(not isinstance(bucket.get(field), int)
                   or isinstance(bucket.get(field), bool)
                   or bucket[field] < 0 for field in fields):
                return False
            outcome_total = sum(bucket[field] for field in outcome_fields)
            return (outcome_total == bucket[total_field]) if exact_total \
                else (outcome_total <= bucket[total_field])

        turn_played = entry.get("turn_played")
        if isinstance(turn_played, Mapping) and any(
                not isinstance(amount, int) or isinstance(amount, bool)
                or amount < 0 for amount in turn_played.values()):
            issues.append("turn_played values must be non-negative integers")
        performance_by_turn = entry.get("performance_by_turn")
        if isinstance(performance_by_turn, Mapping) and any(
                not valid_bucket(bucket, "played", ("wins", "losses", "draws"),
                                 exact_total=True)
                for bucket in performance_by_turn.values()):
            issues.append("performance_by_turn contains an invalid outcome bucket")
        curve = entry.get("mana_curve_performance")
        if isinstance(curve, Mapping):
            required_curve = {"on_curve", "below_curve", "above_curve"}
            if not required_curve.issubset(curve) or any(
                    not valid_bucket(curve.get(bucket), "played",
                                     ("wins", "draws"), exact_total=False)
                    for bucket in required_curve):
                issues.append("mana_curve_performance has invalid/missing buckets")
        archetypes = entry.get("archetype_performance")
        if isinstance(archetypes, Mapping) and any(
                not valid_bucket(bucket, "games", ("wins", "losses", "draws"),
                                 exact_total=True)
                for bucket in archetypes.values()):
            issues.append("archetype_performance contains an invalid outcome bucket")
        synergies = entry.get("synergy_partners")
        if isinstance(synergies, Mapping) and any(
                not valid_bucket(bucket, "games_together",
                                 ("wins_together", "draws_together"),
                                 exact_total=False)
                for bucket in synergies.values()):
            issues.append("synergy_partners contains an invalid outcome bucket")

        win_rate = entry.get("win_rate", _MISSING)
        try:
            win_rate_value = float(win_rate)
        except (TypeError, ValueError, OverflowError):
            win_rate_value = math.nan
        if (isinstance(win_rate, bool) or not math.isfinite(win_rate_value)
                or not 0.0 <= win_rate_value <= 1.0):
            issues.append("win_rate must be between 0 and 1")
        elif "games_played" in counters and counters["games_played"]:
            expected_rate = (
                counters.get("wins", 0) + .5 * counters.get("draws", 0)
            ) / counters["games_played"]
            if not math.isclose(win_rate_value, expected_rate,
                                rel_tol=1e-9, abs_tol=1e-9):
                issues.append("win_rate does not match outcome counters")
        return issues

    @staticmethod
    def _strategy_diagnostics_issues(payload: Mapping[str, Any]) -> list[str]:
        """Validate the safe StrategyMemory diagnostics v1 structure."""
        issues: list[str] = []

        def nonnegative_integer(value: Any) -> bool:
            return (isinstance(value, int) and not isinstance(value, bool)
                    and value >= 0)

        def finite_number(value: Any) -> bool:
            if isinstance(value, bool):
                return False
            try:
                return math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError):
                return False

        if not nonnegative_integer(payload.get("source_memory_schema_version")):
            issues.append("source_memory_schema_version must be non-negative integer")
        if not nonnegative_integer(payload.get("logical_update")):
            issues.append("logical_update must be a non-negative integer")

        semantics = payload.get("semantics")
        if not isinstance(semantics, Mapping):
            issues.append("semantics must be an object")
        else:
            for field in ("reward", "positive_reward_rate"):
                if not isinstance(semantics.get(field), str) \
                        or not semantics[field].strip():
                    issues.append(f"semantics.{field} must be a non-empty string")

        counts = payload.get("counts")
        count_fields = (
            "patterns", "pattern_evidence", "pattern_actions",
            "action_evidence", "action_sequences",
        )
        if not isinstance(counts, Mapping):
            issues.append("counts must be an object")
        else:
            for field in count_fields:
                if not nonnegative_integer(counts.get(field)):
                    issues.append(f"counts.{field} must be a non-negative integer")

        aggregates = payload.get("aggregates")
        aggregate_fields = (
            "pattern_evidence_weighted_mean_reward",
            "pattern_evidence_weighted_positive_reward_rate",
            "action_evidence_weighted_mean_reward",
            "action_evidence_weighted_positive_reward_rate",
        )
        if not isinstance(aggregates, Mapping):
            issues.append("aggregates must be an object")
        else:
            for field in aggregate_fields:
                raw = aggregates.get(field)
                if not finite_number(raw):
                    issues.append(f"aggregates.{field} must be finite")
                elif "positive_reward_rate" in field \
                        and not 0.0 <= float(raw) <= 1.0:
                    issues.append(f"aggregates.{field} must be between 0 and 1")

        limits = payload.get("limits")
        if not isinstance(limits, Mapping):
            issues.append("limits must be an object")
        else:
            for field in ("top_patterns", "top_actions"):
                if not nonnegative_integer(limits.get(field)):
                    issues.append(f"limits.{field} must be a non-negative integer")

        truncation = payload.get("truncation")
        if not isinstance(truncation, Mapping):
            issues.append("truncation must be an object")
        else:
            for field in ("top_patterns", "top_actions"):
                bucket = truncation.get(field)
                if not isinstance(bucket, Mapping):
                    issues.append(f"truncation.{field} must be an object")
                    continue
                total = bucket.get("total")
                returned = bucket.get("returned")
                if not nonnegative_integer(total) or not nonnegative_integer(returned):
                    issues.append(
                        f"truncation.{field} totals must be non-negative integers")
                elif returned > total:
                    issues.append(f"truncation.{field}.returned cannot exceed total")
                if not isinstance(bucket.get("truncated"), bool):
                    issues.append(f"truncation.{field}.truncated must be boolean")

        for field, require_action in (("top_patterns", False),
                                      ("top_actions", True)):
            records = payload.get(field)
            if not isinstance(records, list):
                issues.append(f"{field} must be an array")
                continue
            limit = limits.get(field) if isinstance(limits, Mapping) else None
            if nonnegative_integer(limit) and len(records) > limit:
                issues.append(f"{field} exceeds its declared limit")
            for index, record in enumerate(records):
                prefix = f"{field}[{index}]"
                if not isinstance(record, Mapping):
                    issues.append(f"{prefix} must be an object")
                    continue
                if not isinstance(record.get("pattern"), list):
                    issues.append(f"{prefix}.pattern must be an array")
                for numeric_field in ("count", "last_update"):
                    if not nonnegative_integer(record.get(numeric_field)):
                        issues.append(
                            f"{prefix}.{numeric_field} must be non-negative integer")
                if not finite_number(record.get("mean_reward")):
                    issues.append(f"{prefix}.mean_reward must be finite")
                rate = record.get("positive_reward_rate")
                if not finite_number(rate) or not 0.0 <= float(rate) <= 1.0:
                    issues.append(
                        f"{prefix}.positive_reward_rate must be between 0 and 1")
                if require_action and not nonnegative_integer(
                        _first(record.get("action_index"), record.get("action"))):
                    issues.append(f"{prefix}.action_index must be non-negative integer")
        return issues

    def _summarize_card_memory(
            self, source: dict[str, Any], path: Path | None,
            payload: Any, aggregate_cards: list[Any], *,
            load_error: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate and summarize CardMemory without merging statistics scopes."""
        provenance = self._adaptive_history_provenance(source)
        summary: dict[str, Any] = {
            "status": "missing" if path is None else "unreadable",
            "health": "missing" if path is None else "error",
            "file": self._relative(path),
            "schema_version": None,
            "contract_supported": False,
            "contract_valid": False,
            "last_updated": None,
            "card_count": 0,
            "valid_card_count": 0,
            "valid_card_ids": [],
            "validation_issue_count": 0,
            "validation_issues": [],
            "envelope_validation_issues": [],
            "ambiguous_name_count": 0,
            "ambiguous_names": [],
            "decision_use": provenance,
            "scope_kind": source.get("kind"),
            "scope": source.get("scope"),
            "join": {
                "aggregate_card_count": len(aggregate_cards),
                "joined_card_count": 0,
                "joined_card_ids": [],
                "aggregate_without_memory_count": 0,
                "aggregate_without_memory_ids": [],
                "memory_without_aggregate_count": 0,
                "memory_without_aggregate_ids": [],
                "field_mismatch_count": 0,
                "field_mismatches": [],
            },
        }
        diagnostics: list[dict[str, Any]] = []
        if path is None:
            return summary, diagnostics
        if load_error is not None:
            return summary, diagnostics
        if not isinstance(payload, dict):
            diagnostics.append({
                "level": "error",
                "source": self._relative(path) or "CardMemory",
                "error": "InvalidCardMemoryEnvelope",
                "message": "CardMemory root must be a JSON object.",
            })
            summary["status"] = "invalid"
            return summary, diagnostics

        summary["schema_version"] = payload.get("schema_version", 1)
        summary["last_updated"] = payload.get("last_updated")
        cards = payload.get("cards")
        if not isinstance(cards, dict):
            diagnostics.append({
                "level": "error",
                "source": self._relative(path) or "CardMemory",
                "error": "InvalidCardMemoryCards",
                "message": "CardMemory `cards` must be an object keyed by canonical ID.",
            })
            summary["status"] = "invalid"
            return summary, diagnostics

        version = _integer(summary["schema_version"])
        summary["status"] = "loaded" if version in {1, 2} else "loaded_unknown_schema"
        summary["health"] = "clean"
        summary["contract_supported"] = version in {1, 2}
        summary["card_count"] = len(cards)
        if version not in {1, 2}:
            summary["health"] = "review"
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "CardMemory",
                "error": "UnknownCardMemorySchema",
                "message": (
                    f"Schema version {summary['schema_version']!r} is shown raw; "
                    "derived fields may not follow the v1/v2 contract."
                ),
            })
            return summary, diagnostics

        envelope_issues: list[str] = []
        try:
            last_updated = float(payload.get("last_updated"))
        except (TypeError, ValueError, OverflowError):
            last_updated = math.nan
        if (isinstance(payload.get("last_updated"), bool)
                or not math.isfinite(last_updated) or last_updated < 0):
            envelope_issues.append(
                "last_updated must be a non-negative finite timestamp")
        if not isinstance(payload.get("id_to_name"), dict):
            envelope_issues.append("id_to_name must be an object")
        if not isinstance(payload.get("name_to_id"), dict):
            envelope_issues.append("name_to_id must be an object")
        if not isinstance(payload.get("ambiguous_names", []), list):
            envelope_issues.append("ambiguous_names must be an array")
        summary["envelope_validation_issues"] = envelope_issues
        if envelope_issues:
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "CardMemory",
                "error": "InvalidCardMemoryEnvelopeFields",
                "message": "; ".join(envelope_issues) + ".",
            })

        validation_issues: list[dict[str, Any]] = []
        memory_cards: dict[str, dict[str, Any]] = {}
        for card_id, entry in cards.items():
            identifier = str(card_id)
            issues = self._card_memory_entry_issues(identifier, entry)
            if issues:
                validation_issues.append({
                    "card_id": identifier,
                    "issues": issues,
                })
                continue
            memory_cards[identifier] = entry
        summary["valid_card_count"] = len(memory_cards)
        summary["valid_card_ids"] = sorted(memory_cards)
        summary["validation_issue_count"] = len(validation_issues)
        summary["validation_issues"] = validation_issues[:100]
        malformed_count = len(validation_issues)
        if malformed_count:
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "CardMemory",
                "error": "MalformedCardMemoryEntries",
                "message": (
                    f"{malformed_count} card entries violate the v{version} "
                    "structure or numeric ranges."
                ),
            })

        ambiguous = payload.get("ambiguous_names", [])
        if not isinstance(ambiguous, list):
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "CardMemory",
                "error": "InvalidAmbiguousNames",
                "message": "`ambiguous_names` is not an array.",
            })
            ambiguous = []
            summary["contract_valid"] = False
        ambiguous_names = sorted({str(name) for name in ambiguous})
        summary["ambiguous_names"] = ambiguous_names
        summary["ambiguous_name_count"] = len(ambiguous_names)

        aggregate_by_id: dict[str, dict[str, Any]] = {}
        duplicate_aggregate_ids: set[str] = set()
        aggregate_without_id = 0
        for record in aggregate_cards:
            identifier = self._aggregate_card_id(record)
            if identifier is None:
                aggregate_without_id += 1
                continue
            if identifier in aggregate_by_id:
                duplicate_aggregate_ids.add(identifier)
            if isinstance(record, dict):
                aggregate_by_id[identifier] = record

        aggregate_ids = set(aggregate_by_id)
        memory_ids = set(memory_cards)
        joined_ids = sorted(aggregate_ids & memory_ids)
        aggregate_only = sorted(aggregate_ids - memory_ids)
        memory_only = sorted(memory_ids - aggregate_ids)
        join = summary["join"]
        join.update({
            "aggregate_card_count": len(aggregate_cards),
            "aggregate_with_id_count": len(aggregate_by_id),
            "aggregate_without_id_count": aggregate_without_id,
            "duplicate_aggregate_id_count": len(duplicate_aggregate_ids),
            "duplicate_aggregate_ids": sorted(duplicate_aggregate_ids),
            "joined_card_count": len(joined_ids),
            "joined_card_ids": joined_ids,
            "aggregate_without_memory_count": len(aggregate_only),
            "aggregate_without_memory_ids": aggregate_only,
            "memory_without_aggregate_count": len(memory_only),
            "memory_without_aggregate_ids": memory_only,
        })

        mismatches: list[dict[str, Any]] = []
        comparisons = (
            ("games_played", ("games_played", "games"), ("games_played",)),
            ("wins", ("wins",), ("wins",)),
            ("losses", ("losses",), ("losses",)),
            ("draws", ("draws",), ("draws",)),
            ("times_played", ("usage_count",), ("times_played",)),
        )
        for identifier in joined_ids:
            aggregate = aggregate_by_id[identifier]
            memory = memory_cards[identifier]
            for label, aggregate_names, memory_names in comparisons:
                aggregate_value = self._counter_value(aggregate, *aggregate_names)
                memory_value = self._counter_value(memory, *memory_names)
                if (aggregate_value is not None and memory_value is not None
                        and aggregate_value != memory_value):
                    mismatches.append({
                        "card_id": identifier,
                        "field": label,
                        "deck_stats": aggregate_value,
                        "card_memory": memory_value,
                    })
        join["field_mismatch_count"] = len(mismatches)
        join["field_mismatches"] = mismatches[:100]

        mapping_problem_count = 0
        id_to_name = payload.get("id_to_name")
        name_to_id = payload.get("name_to_id")
        ambiguous_set = set(ambiguous_names)
        if isinstance(id_to_name, dict) and isinstance(name_to_id, dict):
            for identifier, entry in memory_cards.items():
                name = entry.get("name")
                stored_id = entry.get("id")
                if stored_id is not None and str(stored_id) != identifier:
                    mapping_problem_count += 1
                if isinstance(name, str):
                    if id_to_name.get(identifier) != name:
                        mapping_problem_count += 1
                    if (name not in ambiguous_set
                            and str(name_to_id.get(name)) != identifier):
                        mapping_problem_count += 1
        else:
            mapping_problem_count = 1
        summary["mapping_problem_count"] = mapping_problem_count
        summary["contract_valid"] = not (
            malformed_count or mapping_problem_count
            or envelope_issues
        )

        notice_parts = []
        if aggregate_without_id:
            notice_parts.append(f"{aggregate_without_id} aggregate rows lack an ID")
        if duplicate_aggregate_ids:
            notice_parts.append(
                f"{len(duplicate_aggregate_ids)} duplicate aggregate IDs")
        if aggregate_only:
            notice_parts.append(
                f"{len(aggregate_only)} aggregate IDs lack memory")
        if memory_only:
            notice_parts.append(
                f"{len(memory_only)} memory IDs lack aggregates")
        if mismatches:
            notice_parts.append(f"{len(mismatches)} counter mismatches")
        if mapping_problem_count:
            notice_parts.append(f"{mapping_problem_count} mapping inconsistencies")
        if malformed_count:
            notice_parts.append(f"{malformed_count} malformed entries")
        if envelope_issues:
            notice_parts.append(
                f"{len(envelope_issues)} malformed envelope fields")
        if notice_parts:
            summary["health"] = "review"
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "CardMemory",
                "error": "CardMemoryIntegrityNotice",
                "message": "; ".join(notice_parts) + ".",
            })
        return summary, diagnostics

    def _summarize_strategy_memory(
            self, source: dict[str, Any], path: Path | None, payload: Any,
            unsafe_pickle_path: Path | None, *,
            load_error: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Summarize safe strategy diagnostics and never deserialize pickle."""
        unsafe_info = None
        if unsafe_pickle_path is not None:
            try:
                stat = unsafe_pickle_path.stat()
                digest = hashlib.sha256()
                hashed_size = 0
                with unsafe_pickle_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        hashed_size += len(chunk)
                        digest.update(chunk)
                unsafe_info = {
                    "present": True,
                    "file": self._relative(unsafe_pickle_path),
                    "bytes": hashed_size,
                    "size_bytes": hashed_size,
                    "sha256": digest.hexdigest(),
                    "modified_at": stat.st_mtime,
                    "loaded": False,
                    "reason": "Pickle is intentionally never deserialized by the viewer.",
                }
            except OSError:
                unsafe_info = {
                    "present": True,
                    "file": self._relative(unsafe_pickle_path),
                    "loaded": False,
                }
        summary: dict[str, Any] = {
            "status": "missing",
            "health": "missing",
            "file": self._relative(path),
            "kind": None,
            "schema_version": None,
            "contract_supported": False,
            "contract_valid": False,
            "validation_issue_count": 0,
            "validation_issues": [],
            "source_memory_schema_version": None,
            "logical_update": None,
            "semantics": {},
            "counts": {},
            "aggregates": {},
            "limits": {},
            "truncation": {},
            "top_pattern_count": 0,
            "top_action_count": 0,
            "positive_reward_rate_semantics": (
                "fraction of evidence with shaped reward > 0; not game win rate"
            ),
            "configuration": self._strategy_memory_provenance(source),
            "unsafe_pickle": unsafe_info,
            "source_pickle": {},
            "source_pickle_verification": {
                "status": "not_checked",
                "verified": False,
            },
            "scope_kind": source.get("kind"),
            "scope": source.get("scope"),
        }
        diagnostics: list[dict[str, Any]] = []
        if path is None:
            if unsafe_info is not None:
                summary["status"] = "unsafe_pickle_only"
                summary["health"] = "review"
                diagnostics.append({
                    "level": "warning",
                    "source": (
                        unsafe_info.get("file") or "StrategyMemory pickle"
                    ),
                    "error": "StrategyMemoryPickleOnly",
                    "message": (
                        "strategy_memory.pkl is present without its safe JSON "
                        "diagnostic export. The viewer did not open or unpickle it."
                    ),
                })
            return summary, diagnostics
        if load_error is not None:
            summary["status"] = "unreadable"
            summary["health"] = "error"
            return summary, diagnostics
        if not isinstance(payload, dict):
            summary["status"] = "invalid"
            summary["health"] = "error"
            diagnostics.append({
                "level": "error",
                "source": self._relative(path) or "StrategyMemory",
                "error": "InvalidStrategyMemoryDiagnostics",
                "message": "Safe strategy-memory diagnostics root must be a JSON object.",
            })
            return summary, diagnostics

        summary.update({
            "status": (
                "loaded_with_opaque_pickle" if unsafe_info is not None else "loaded"
            ),
            "health": "clean",
            "kind": payload.get("kind"),
            "schema_version": payload.get("schema_version"),
            "source_memory_schema_version": payload.get(
                "source_memory_schema_version"),
            "logical_update": payload.get("logical_update"),
            "semantics": _as_mapping(payload.get("semantics")),
            "counts": _as_mapping(payload.get("counts")),
            "aggregates": _as_mapping(payload.get("aggregates")),
            "limits": _as_mapping(payload.get("limits")),
            "truncation": _as_mapping(payload.get("truncation")),
            "top_pattern_count": len(_as_list(payload.get("top_patterns"))),
            "top_action_count": len(_as_list(payload.get("top_actions"))),
            "source_pickle": _as_mapping(payload.get("source_pickle")),
        })
        expected_kind = "playersim.strategy_memory.diagnostics"
        version = _integer(payload.get("schema_version"))
        contract_supported = payload.get("kind") == expected_kind and version == 1
        summary["contract_supported"] = contract_supported
        if not contract_supported:
            summary["health"] = "review"
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "UnknownStrategyMemorySchema",
                "message": (
                    "Safe JSON was loaded, but kind/schema does not match "
                    "playersim.strategy_memory.diagnostics v1; shown raw."
                ),
            })
            return summary, diagnostics

        validation_issues = self._strategy_diagnostics_issues(payload)
        marker = payload.get("source_pickle")
        marker_valid = False
        expected_size = None
        expected_hash = None
        if isinstance(marker, Mapping):
            expected_size = marker.get("size_bytes")
            expected_hash = marker.get("sha256")
            marker_valid = bool(
                isinstance(expected_size, int)
                and not isinstance(expected_size, bool)
                and expected_size >= 0
                and isinstance(expected_hash, str)
                and len(expected_hash) == 64
                and all(character in "0123456789abcdef"
                        for character in expected_hash)
            )

        verification: dict[str, Any] = {
            "status": "missing_marker",
            "verified": False,
            "expected_size_bytes": expected_size,
            "expected_sha256": expected_hash,
            "actual_size_bytes": (
                unsafe_info.get("size_bytes")
                if isinstance(unsafe_info, Mapping) else None
            ),
            "actual_sha256": (
                unsafe_info.get("sha256")
                if isinstance(unsafe_info, Mapping) else None
            ),
        }
        verification_diagnostic: dict[str, Any] | None = None
        if marker is None:
            verification_diagnostic = {
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "StrategyMemorySourcePickleMarkerMissing",
                "message": (
                    "Safe StrategyMemory v1 diagnostics do not identify the "
                    "opaque pickle bytes they summarize; freshness cannot be "
                    "verified."
                ),
            }
        elif not marker_valid:
            verification["status"] = "invalid_marker"
            verification_diagnostic = {
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "InvalidStrategyMemorySourcePickleMarker",
                "message": (
                    "source_pickle must contain a non-negative integer "
                    "size_bytes and a lowercase 64-character SHA-256 digest."
                ),
            }
        elif not isinstance(unsafe_info, Mapping) \
                or unsafe_info.get("size_bytes") is None \
                or unsafe_info.get("sha256") is None:
            verification["status"] = "pickle_unavailable"
            verification_diagnostic = {
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "StrategyMemorySourcePickleUnavailable",
                "message": (
                    "The source pickle recorded by the safe diagnostics is "
                    "missing or unreadable, so freshness cannot be verified."
                ),
            }
        elif (unsafe_info.get("size_bytes") != expected_size
              or unsafe_info.get("sha256") != expected_hash):
            verification["status"] = "mismatch"
            verification_diagnostic = {
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "StrategyMemorySourcePickleMismatch",
                "message": (
                    "Safe diagnostics do not match the current opaque "
                    "strategy_memory.pkl bytes and may be stale."
                ),
                "expected": {
                    "size_bytes": expected_size,
                    "sha256": expected_hash,
                },
                "actual": {
                    "size_bytes": unsafe_info.get("size_bytes"),
                    "sha256": unsafe_info.get("sha256"),
                },
            }
        else:
            verification.update({"status": "verified", "verified": True})

        summary["source_pickle_verification"] = verification
        summary["validation_issue_count"] = len(validation_issues)
        summary["validation_issues"] = validation_issues[:100]
        summary["contract_valid"] = bool(
            not validation_issues and verification["verified"])
        if verification_diagnostic is not None:
            summary["health"] = "review"
            diagnostics.append(verification_diagnostic)
        if validation_issues:
            summary["health"] = "review"
            diagnostics.append({
                "level": "warning",
                "source": self._relative(path) or "StrategyMemory",
                "error": "MalformedStrategyMemoryDiagnostics",
                "message": (
                    f"Safe StrategyMemory v1 has {len(validation_issues)} "
                    "structural or numeric contract violations."
                ),
            })
        return summary, diagnostics

    @staticmethod
    def _evaluation_record_id(run_id: str,
                              game: Mapping[str, Any]) -> str:
        """Return a stable opaque identity for one persisted evaluation row."""
        identity = json.dumps({
            "run_id": str(run_id),
            "evaluation_index": _integer(game.get("evaluation_index")),
            "episode_index": _integer(game.get("episode_index")),
            "timestep": _integer(game.get("evaluation_timestep")),
            "case_index": _integer(game.get("case_index")),
            "checkpoint_sha256": game.get("checkpoint_sha256"),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "eval-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _debug_has_terminal_payload(value: Any) -> bool:
        """Return whether a debug mapping contains terminal diagnostics.

        A mapping that contains only an action trace/replay/evaluator capture is
        not terminal debug merely because it was stored under ``debug``.
        """
        if not isinstance(value, Mapping):
            return False
        terminal_keys = {
            "terminal", "terminal_only", "terminal_reason", "game_result",
            "reward_components", "reward_diagnostics", "policy_state",
            "fidelity", "final_state", "done", "truncated",
        }
        return any(value.get(key) is not None for key in terminal_keys)

    @staticmethod
    def _evaluation_debug_summary(value: Any) -> dict[str, Any] | None:
        """Index a loaded sidecar without copying its heavy event arrays."""
        if not isinstance(value, Mapping):
            if isinstance(value, (list, tuple)):
                return {
                    "schema_version": 1,
                    "trace_event_count": len(value),
                    "trace_actor_counts": {},
                    "replay_action_count": 0,
                    "card_catalog_count": 0,
                    "capture_status": "not_recorded",
                }
            return None
        trace = value.get("trace")
        trace = trace if isinstance(trace, (list, tuple)) else ()
        replay = value.get("replay")
        replay_actions = replay.get("actions") \
            if isinstance(replay, Mapping) else None
        replay_actions = replay_actions \
            if isinstance(replay_actions, (list, tuple)) else ()
        actor_counts: Counter[str] = Counter()
        for event in trace:
            if isinstance(event, Mapping):
                actor_counts[str(event.get("actor") or "unknown")] += 1

        capture = value.get("capture")
        capture_summary: dict[str, Any] = {}
        degraded = False
        if isinstance(capture, Mapping):
            for scope in ("trace", "replay", "terminal"):
                raw = capture.get(scope)
                if not isinstance(raw, Mapping):
                    continue
                selected = {
                    key: raw.get(key) for key in (
                        "recorded_events", "dropped_events",
                        "serialized_bytes", "sanitization_omissions",
                        "serialization_errors") if key in raw
                }
                capture_summary[scope] = selected
                degraded = degraded or any(
                    (_integer(selected.get(key)) or 0) > 0 for key in (
                        "dropped_events", "sanitization_omissions",
                        "serialization_errors")
                )
            errors = capture.get("errors")
            error_count = len(errors) \
                if isinstance(errors, (list, tuple)) else 0
            capture_summary["error_count"] = error_count
            degraded = degraded or error_count > 0

        catalog = value.get("card_catalog")
        if isinstance(catalog, Mapping):
            entries = catalog.get("entries")
            catalog_count = len(entries) \
                if isinstance(entries, (list, tuple)) \
                else (_integer(catalog.get("recorded_entries")) or 0)
            catalog_omitted = _integer(catalog.get("omitted_entries")) or 0
        elif isinstance(catalog, (list, tuple)):
            catalog_count, catalog_omitted = len(catalog), 0
        else:
            catalog_count, catalog_omitted = 0, 0
        degraded = degraded or catalog_omitted > 0
        terminal = _as_mapping(value.get("terminal"))
        evaluator = _as_mapping(value.get("evaluator"))
        return _json_safe({
            "schema_version": 1,
            "trace_event_count": len(trace),
            "trace_actor_counts": dict(sorted(actor_counts.items())),
            "replay_action_count": len(replay_actions),
            "card_catalog_count": catalog_count,
            "card_catalog_omitted": catalog_omitted,
            "capture_status": "degraded" if degraded else (
                "complete" if isinstance(capture, Mapping)
                else "not_recorded"),
            "capture": capture_summary,
            "terminal": {
                key: terminal.get(key) for key in (
                    "game_result", "terminal_reason", "reward", "done",
                    "truncated") if key in terminal
            },
            "evaluator": _as_mapping(evaluator.get("summary")) or None,
        })

    @staticmethod
    def _evaluation_availability(
            episode: Mapping[str, Any],
            game_log: Mapping[str, Any]) -> dict[str, bool]:
        """Separate action traces, replays, and terminal-only diagnostics."""
        sources = (episode, game_log)
        trace_available = False
        replay_available = False
        terminal_debug_available = False
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            inline_debug = source.get("debug")
            if isinstance(inline_debug, Mapping):
                trace_available = trace_available or inline_debug.get("trace") is not None
                replay_available = replay_available or inline_debug.get("replay") is not None
                terminal_debug_available = (
                    terminal_debug_available
                    or ViewerRepository._debug_has_terminal_payload(inline_debug)
                )
            trace_available = trace_available or any(
                source.get(key) is not None
                for key in ("trace", "debug_trace", "trace_path")
            )
            replay_available = replay_available or any(
                source.get(key) is not None for key in (
                    "replay", "replay_data", "replay_actions", "actions",
                    "replay_path", "replay_file",
                )
            )
            trace_available = trace_available or (
                (_integer(source.get("trace_event_count")) or 0) > 0
            )
            replay_available = replay_available or (
                (_integer(source.get("replay_action_count")) or 0) > 0
            )
            terminal_debug_available = terminal_debug_available or any(
                source.get(key) is not None for key in (
                    "diagnostics", "policy_state", "debug_path",
                )
            )
        return {
            "trace_available": bool(trace_available),
            "replay_available": bool(replay_available),
            "terminal_debug_available": bool(terminal_debug_available),
            # Compatibility alias for callers written before availability was
            # split into its three distinct contracts.
            "debug_available": bool(
                terminal_debug_available or trace_available),
        }

    def _normalize_evaluation_episode(
            self, evaluation: dict[str, Any], episode: dict[str, Any],
            evaluation_index: int, episode_index: int) -> dict[str, Any]:
        case = _as_mapping(_first(episode.get("case"), episode.get("resolved_case")))
        resolved = _as_mapping(episode.get("resolved_case"))
        case_index = _integer(_first(
            episode.get("case_index"), case.get("case_index"), episode_index
        ))
        timestep = _integer(_first(
            episode.get("evaluation_timestep"), episode.get("timesteps"),
            evaluation.get("timesteps"), evaluation.get("timestep"),
        ))
        checkpoint = _first(
            episode.get("evaluation_checkpoint_sha256"),
            episode.get("checkpoint_sha256"), evaluation.get("checkpoint_sha256"),
            evaluation.get("checkpoint"),
        )
        agent_is_p1 = _boolean(_first(
            case.get("agent_is_p1"), resolved.get("agent_is_p1"),
            episode.get("agent_is_p1"),
        ))
        p1_deck = _first(case.get("p1_deck"), resolved.get("p1_deck"),
                         episode.get("p1_deck"))
        p2_deck = _first(case.get("p2_deck"), resolved.get("p2_deck"),
                         episode.get("p2_deck"))
        agent_deck = episode.get("agent_deck")
        opponent_deck = episode.get("opponent_deck")
        if agent_is_p1 is not None:
            agent_deck = _first(agent_deck, p1_deck if agent_is_p1 else p2_deck)
            opponent_deck = _first(opponent_deck, p2_deck if agent_is_p1 else p1_deck)
        normalized_case = dict(case)
        for key, value in {
            "seed": _first(case.get("seed"), resolved.get("seed"), episode.get("seed")),
            "p1_deck": p1_deck,
            "p2_deck": p2_deck,
            "agent_is_p1": agent_is_p1,
            "opponent_profile": _first(
                case.get("opponent_profile"), resolved.get("opponent_profile"),
                episode.get("opponent_profile"),
            ),
        }.items():
            if value is not None:
                normalized_case[key] = value
        pair_index = (case_index // 2) if case_index is not None else None
        row = dict(episode)
        row.update({
            "evaluation_index": evaluation_index,
            "episode_index": episode_index,
            "timestep": timestep,
            "evaluation_timestep": timestep,
            "checkpoint": checkpoint,
            "checkpoint_sha256": checkpoint,
            "case": _json_safe(normalized_case),
            "normalized_case": _json_safe(normalized_case),
            "case_index": case_index,
            "seat": "p1" if agent_is_p1 is True else "p2" if agent_is_p1 is False else None,
            "agent_seat": "p1" if agent_is_p1 is True else "p2" if agent_is_p1 is False else None,
            "agent_is_p1": agent_is_p1,
            "pair": pair_index,
            "pair_index": pair_index,
            "p1_deck": p1_deck,
            "p2_deck": p2_deck,
            "agent_deck": agent_deck,
            "opponent_deck": opponent_deck,
            "result": _first(episode.get("game_result"), episode.get("result")),
            "evaluation": _json_safe({
                key: evaluation.get(key) for key in (
                    "completed_at", "qualified", "promoted",
                    "candidate_promoted", "qualification_score",
                    "qualification_interval", "promotion_key",
                    "snapshot_name", "schedule_sha256",
                ) if evaluation.get(key) is not None
            }),
            "summary": _json_safe(evaluation.get("summary")),
        })
        return row

    # -- evaluation game-log joins --------------------------------------

    def _evaluation_log_rows(self, run_id: str) -> list[dict[str, Any]]:
        sources = sorted(
            (source for source in self._stats_by_id.values()
             if source.get("run_id") == run_id and source.get("kind") == "evaluation"),
            key=lambda source: source["relative_path"].casefold(),
        )
        signatures = []
        for source in sources:
            path = source["path"] / "game_log.jsonl"
            signatures.append((source["id"], self._signature(path)))
        cache_key = (run_id, tuple(signatures))
        cached = self._evaluation_join_cache.get(cache_key)
        if cached is not None:
            return _json_safe(cached)

        rows: list[dict[str, Any]] = []
        for source in sources:
            path = source["path"] / "game_log.jsonl"
            total = self._jsonl_count(path)
            for offset in range(0, total, 1_000):
                page, _count, _errors = self._jsonl_page(path, offset, 1_000)
                for item in page:
                    if isinstance(item, dict):
                        row = dict(item)
                        row["_stats_source_id"] = source["id"]
                        rows.append(row)
        rows.sort(key=lambda row: (
            _sortable_float(row.get("ts")),
            str(row.get("game_id") or ""),
        ))
        for old_key in list(self._evaluation_join_cache):
            if old_key[0] == run_id and old_key != cache_key:
                self._evaluation_join_cache.pop(old_key, None)
        self._evaluation_join_cache[cache_key] = _json_safe(rows)
        return _json_safe(rows)

    @staticmethod
    def _make_evaluation_join(rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        exact: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        relaxed: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        legacy: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            game_id = row.get("game_id")
            if game_id is not None:
                by_id[str(game_id)].append(row)
            checkpoint = _first(
                row.get("evaluation_checkpoint_sha256"), row.get("checkpoint_sha256")
            )
            timestep = _integer(_first(
                row.get("evaluation_timestep"), row.get("timestep")
            ))
            case_index = _integer(_first(
                row.get("matchup_episode_index"), row.get("case_index")
            ))
            exact[(checkpoint, timestep, case_index)].append(row)
            relaxed[(timestep, case_index)].append(row)
            legacy[(
                row.get("episode_seed"), row.get("p1_deck"), row.get("p2_deck"),
                _boolean(row.get("agent_is_p1")),
            )].append(row)
        return {
            "by_id": by_id, "exact": exact, "relaxed": relaxed,
            "legacy": legacy, "used": set(),
        }

    @staticmethod
    def _take_joined_game(
            join: dict[str, Any], game: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Join an authoritative log row without crossing identity conflicts."""

        def identity_fields(row: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "checkpoint_sha256": _first(
                    row.get("evaluation_checkpoint_sha256"),
                    row.get("checkpoint_sha256"),
                ),
                "evaluation_timestep": _integer(_first(
                    row.get("evaluation_timestep"), row.get("timestep"),
                )),
                "case_index": _integer(_first(
                    row.get("matchup_episode_index"), row.get("case_index"),
                )),
            }

        expected = identity_fields(game)
        conflicts: list[dict[str, Any]] = []

        def compatible(row: dict[str, Any]) -> bool:
            actual = identity_fields(row)
            mismatches = {
                field: {"evaluation": expected[field], "game_log": actual[field]}
                for field in expected
                if (expected[field] is not None and actual[field] is not None
                    and expected[field] != actual[field])
            }
            if mismatches:
                conflicts.append({
                    "game_id": row.get("game_id"),
                    "stats_source_id": row.get("_stats_source_id"),
                    "identity": actual,
                    "mismatches": mismatches,
                })
                return False
            return True

        def take(rows: Iterable[dict[str, Any]], method: str, *,
                 require_missing_checkpoint: bool = False
                 ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            for row in rows:
                actual = identity_fields(row)
                if (require_missing_checkpoint
                        and expected["checkpoint_sha256"] is not None
                        and actual["checkpoint_sha256"] is not None):
                    compatible(row)  # Record a conflicting checkpoint, if any.
                    continue
                if not compatible(row):
                    continue
                row_identity = str(row.get("game_id") or id(row))
                if row_identity in join["used"]:
                    continue
                join["used"].add(row_identity)
                return row, {
                    "status": "matched",
                    "method": method,
                    "game_id": row.get("game_id"),
                    "stats_source_id": row.get("_stats_source_id"),
                    "identity": actual,
                }
            return None, None

        def conflict_diagnostic() -> dict[str, Any]:
            return {
                "status": "conflict",
                "error": "EvaluationGameLogJoinConflict",
                "message": (
                    "Authoritative game-log candidates conflict with persisted "
                    "evaluation identity; no fallback join was performed."),
                "evaluation_identity": expected,
                "conflicts": conflicts[:20],
            }

        game_id = game.get("game_id")
        if game_id is not None:
            row, diagnostic = take(
                join["by_id"].get(str(game_id), []), "game_id")
            if row is not None:
                return row, diagnostic
            if conflicts:
                return None, conflict_diagnostic()

        full_identity = all(value is not None for value in expected.values())
        if full_identity:
            row, diagnostic = take(join["exact"].get((
                expected["checkpoint_sha256"],
                expected["evaluation_timestep"],
                expected["case_index"],
            ), []), "exact")
            if row is not None:
                return row, diagnostic

        # Timestep/case fallback is legal only when one side genuinely lacks
        # the checkpoint identity; it must not bridge two different hashes.
        if (expected["evaluation_timestep"] is not None
                and expected["case_index"] is not None):
            relaxed_rows = join["relaxed"].get((
                expected["evaluation_timestep"], expected["case_index"]), [])
            row, diagnostic = take(
                relaxed_rows, "missing_checkpoint",
                require_missing_checkpoint=True)
            if row is not None:
                return row, diagnostic

        # Seed/deck/seat is a legacy fallback only when the evaluation itself
        # is missing at least one authoritative identity field.
        if not full_identity:
            legacy_key = (
                _as_mapping(game.get("case")).get("seed"),
                game.get("p1_deck"), game.get("p2_deck"),
                game.get("agent_is_p1"),
            )
            row, diagnostic = take(
                join["legacy"].get(legacy_key, []), "legacy_missing_identity")
            if row is not None:
                return row, diagnostic

        if conflicts:
            return None, conflict_diagnostic()
        return None, {
            "status": "unmatched",
            "error": "EvaluationGameLogNoMatch",
            "message": "No compatible unused authoritative game-log row was found.",
            "evaluation_identity": expected,
        }

    def _evaluation_payload(self, family: str, episode: dict[str, Any],
                            game_log: dict[str, Any] | None,
                            history_path: Path | None) -> tuple[
                                Any, dict[str, Any] | None,
                                dict[str, Any] | None]:
        """Load one inline/reference payload with optional artifact integrity.

        Reference failures never degrade into returning the path string.  That
        distinction is essential to the debugger: a reference is not a trace.
        """
        if family == "replay":
            keys = ("replay", "replay_data", "replay_actions", "actions",
                    "replay_path", "replay_file")
        else:
            # A complete sidecar/trace must win over terminal-only legacy
            # fields when both happen to coexist on one episode.
            keys = ("debug", "debug_path", "trace", "debug_trace",
                    "trace_path", "diagnostics", "policy_state")
        sources = (episode, game_log or {})
        # Key priority is global across episode + authoritative log.  A
        # terminal-only field in one source must never mask a complete sidecar
        # or explicit trace in the other.
        for key in keys:
            for source in sources:
                value = source.get(key, _MISSING)
                if value is _MISSING or value is None:
                    continue
                if key.endswith(("_path", "_file")):
                    if not isinstance(value, str) or not value.strip():
                        error = {
                            "level": "error",
                            "source": str(value),
                            "family": family,
                            "error": "InvalidArtifactReference",
                            "message": (
                                f"{family} artifact reference must be a "
                                "non-empty string"),
                        }
                        return None, error, {
                            "family": family,
                            "reference": _json_safe(value),
                            "verified": False,
                            "error": "InvalidArtifactReference",
                        }
                    loaded, error, artifact = self._load_internal_reference(
                        value,
                        history_path,
                        family=family,
                        expected_sha256=source.get(f"{family}_sha256"),
                        expected_size=source.get(f"{family}_size_bytes"),
                    )
                    if artifact is not None:
                        artifact["source_key"] = key
                    return loaded, error, artifact
                return _json_safe(value), None, {
                    "family": family,
                    "source_key": key,
                    "inline": True,
                    "verified": None,
                }
        return None, None, None

    @staticmethod
    def _backfill_evaluation_from_log(game: dict[str, Any],
                                      row: dict[str, Any]) -> None:
        """Fill normalized legacy episode fields from its authoritative log row."""
        replacements = {
            "evaluation_timestep": _integer(_first(
                row.get("evaluation_timestep"), row.get("timestep")
            )),
            "timestep": _integer(_first(
                row.get("evaluation_timestep"), row.get("timestep")
            )),
            "checkpoint": _first(
                row.get("evaluation_checkpoint_sha256"), row.get("checkpoint_sha256")
            ),
            "checkpoint_sha256": _first(
                row.get("evaluation_checkpoint_sha256"), row.get("checkpoint_sha256")
            ),
            "case_index": _integer(_first(
                row.get("matchup_episode_index"), row.get("case_index")
            )),
            "agent_is_p1": _boolean(row.get("agent_is_p1")),
            "p1_deck": row.get("p1_deck"),
            "p2_deck": row.get("p2_deck"),
            "agent_deck": row.get("agent_deck"),
            "opponent_deck": row.get("opponent_deck"),
        }
        for key, value in replacements.items():
            if game.get(key) is None and value is not None:
                game[key] = value
        seat = _boolean(game.get("agent_is_p1"))
        game["seat"] = game["agent_seat"] = (
            "p1" if seat is True else "p2" if seat is False else None
        )
        case_index = _integer(game.get("case_index"))
        game["pair"] = game["pair_index"] = (
            case_index // 2 if case_index is not None else None
        )
        if seat is not None:
            game["agent_deck"] = _first(
                game.get("agent_deck"),
                game.get("p1_deck") if seat else game.get("p2_deck"),
            )
            game["opponent_deck"] = _first(
                game.get("opponent_deck"),
                game.get("p2_deck") if seat else game.get("p1_deck"),
            )
        case = _as_mapping(game.get("case"))
        for key, value in {
            "seed": _first(row.get("episode_seed"), row.get("seed")),
            "p1_deck": game.get("p1_deck"),
            "p2_deck": game.get("p2_deck"),
            "agent_is_p1": seat,
            "opponent_profile": row.get("opponent_profile"),
        }.items():
            if case.get(key) is None and value is not None:
                case[key] = value
        game["case"] = game["normalized_case"] = case

    # -- cached file readers ---------------------------------------------

    def _read_json(self, path: Path | None) -> tuple[Any, dict[str, Any] | None]:
        if path is None:
            return None, None
        path = Path(path)
        if not self._is_internal(path):
            return None, self._file_error(path, "UnsafePath", "Path is outside project root")
        signature = self._signature(path)
        if signature is None:
            return None, self._file_error(path, "FileNotFoundError", "File does not exist")
        cached = self._json_cache.get(path)
        if cached is not None and cached[0] == signature:
            return _json_safe(cached[1]), _json_safe(cached[2])
        try:
            opener = gzip.open if path.name.casefold().endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle, parse_constant=lambda _value: None)
            safe_value = _json_safe(value)
            self._json_cache[path] = (signature, safe_value, None)
            return _json_safe(safe_value), None
        except (OSError, UnicodeError, json.JSONDecodeError, EOFError) as error:
            detail = self._file_error(path, type(error).__name__, str(error))
            self._json_cache[path] = (signature, None, detail)
            return None, _json_safe(detail)

    def _jsonl_count(self, path: Path) -> int:
        _signature, positions, _error = self._jsonl_index(path)
        return len(positions)

    def _jsonl_index(self, path: Path) -> tuple[
            tuple[int, int, int] | None, tuple[tuple[int, int], ...],
            dict[str, Any] | None]:
        if not self._is_internal(path):
            return None, (), self._file_error(
                path, "UnsafePath", "Path is outside project root"
            )
        signature = self._signature(path)
        if signature is None:
            return None, (), self._file_error(
                path, "FileNotFoundError", "Game log does not exist"
            )
        cached = self._jsonl_indexes.get(path)
        if cached is not None and cached[0] == signature:
            return signature, cached[1], None
        try:
            positions: list[tuple[int, int]] = []
            with path.open("rb") as handle:
                line_number = 0
                while True:
                    position = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    line_number += 1
                    if line.strip():
                        positions.append((position, line_number))
            frozen = tuple(positions)
            self._jsonl_indexes[path] = (signature, frozen)
            self._discard_jsonl_pages(path)
            return signature, frozen, None
        except OSError as error:
            return signature, (), self._file_error(path, type(error).__name__, str(error))

    def _jsonl_page(self, path: Path, offset: int,
                    limit: int) -> tuple[list[Any], int, list[dict[str, Any]]]:
        signature, positions, index_error = self._jsonl_index(path)
        total = len(positions)
        if signature is None or index_error:
            return [], total, [index_error] if index_error else []
        key = (path, signature, offset, limit)
        cached = self._jsonl_page_cache.get(key)
        if cached is not None:
            self._jsonl_page_cache.move_to_end(key)
            return _json_safe(cached[0]), total, _json_safe(cached[1])
        rows: list[Any] = []
        errors: list[dict[str, Any]] = []
        try:
            with path.open("rb") as handle:
                for position, line_number in positions[offset:offset + limit]:
                    handle.seek(position)
                    raw = handle.readline()
                    try:
                        value = json.loads(
                            raw.decode("utf-8"), parse_constant=lambda _value: None
                        )
                        rows.append(_json_safe(value))
                    except (UnicodeError, json.JSONDecodeError) as error:
                        errors.append({
                            "path": self._relative(path),
                            "line": line_number,
                            "error": type(error).__name__,
                            "message": str(error),
                        })
        except OSError as error:
            errors.append(self._file_error(path, type(error).__name__, str(error)))
        self._jsonl_page_cache[key] = (_json_safe(rows), _json_safe(errors))
        self._jsonl_page_cache.move_to_end(key)
        while len(self._jsonl_page_cache) > self._PAGE_CACHE_SIZE:
            self._jsonl_page_cache.popitem(last=False)
        return _json_safe(rows), total, _json_safe(errors)

    def _discard_jsonl_pages(self, path: Path) -> None:
        for key in list(self._jsonl_page_cache):
            if key[0] == path:
                self._jsonl_page_cache.pop(key, None)

    def _load_json_directory(self, directory: Path) -> tuple[
            list[Any], list[str], list[dict[str, Any]]]:
        if not directory.is_dir() or not self._is_internal(directory):
            return [], [], []
        try:
            paths = sorted(
                (path for path in directory.iterdir()
                 if path.is_file() and (
                     path.name.casefold().endswith(".json")
                     or path.name.casefold().endswith(".json.gz")
                 ) and self._is_internal(path)),
                key=self._path_sort_key,
            )
        except OSError as error:
            return [], [], [self._file_error(
                directory, type(error).__name__, str(error)
            )]
        values: list[Any] = []
        files: list[str] = []
        errors: list[dict[str, Any]] = []
        for path in paths:
            value, error = self._read_json(path)
            if error:
                errors.append(error)
                continue
            values.append(value)
            files.append(self._relative(path))
        return values, files, errors

    def _load_internal_reference(
            self, value: str, history_path: Path | None, *, family: str,
            expected_sha256: Any = None,
            expected_size: Any = None,
    ) -> tuple[Any, dict[str, Any] | None, dict[str, Any]]:
        """Resolve and integrity-check one project-local artifact reference."""
        artifact: dict[str, Any] = {
            "family": family,
            "reference": str(value),
            "file": None,
            "expected_sha256": expected_sha256,
            "expected_size_bytes": expected_size,
            "actual_sha256": None,
            "actual_size_bytes": None,
            "size_verified": None,
            "sha256_verified": None,
            "verified": None,
        }

        def failure(kind: str, message: str) -> tuple[
                None, dict[str, Any], dict[str, Any]]:
            error = {
                "level": "error",
                "source": str(value),
                "family": family,
                "error": kind,
                "message": message,
            }
            artifact["verified"] = False
            artifact["error"] = kind
            return None, error, _json_safe(artifact)

        candidates = [self.project_root / value]
        if history_path is not None:
            candidates.insert(0, history_path.parent / value)
        for candidate in candidates:
            try:
                path = candidate.resolve()
            except OSError:
                continue
            if not self._is_internal(path) or not path.is_file():
                continue
            artifact["file"] = self._relative(path)
            try:
                actual_size = path.stat().st_size
            except OSError as error:
                return failure(type(error).__name__, str(error))
            artifact["actual_size_bytes"] = actual_size

            if expected_size is not None:
                parsed_size = _integer(expected_size)
                if parsed_size is None or parsed_size < 0:
                    return failure(
                        "InvalidArtifactSizeMetadata",
                        f"Invalid expected {family} artifact size: "
                        f"{expected_size!r}",
                    )
                artifact["expected_size_bytes"] = parsed_size
                if actual_size != parsed_size:
                    artifact["size_verified"] = False
                    return failure(
                        "ArtifactSizeMismatch",
                        f"{family} artifact is {actual_size} bytes; history "
                        f"records {parsed_size}",
                    )
                artifact["size_verified"] = True

            if expected_sha256 is not None:
                expected_hash = str(expected_sha256).strip().casefold()
                if (len(expected_hash) != 64
                        or any(character not in "0123456789abcdef"
                               for character in expected_hash)):
                    return failure(
                        "InvalidArtifactHashMetadata",
                        f"Invalid expected {family} SHA-256",
                    )
                digest = hashlib.sha256()
                try:
                    with path.open("rb") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                except OSError as error:
                    return failure(type(error).__name__, str(error))
                actual_hash = digest.hexdigest()
                artifact["expected_sha256"] = expected_hash
                artifact["actual_sha256"] = actual_hash
                if actual_hash != expected_hash:
                    artifact["sha256_verified"] = False
                    return failure(
                        "ArtifactHashMismatch",
                        f"{family} artifact SHA-256 does not match history",
                    )
                artifact["sha256_verified"] = True

            if path.name.casefold().endswith((".json", ".json.gz")):
                loaded, error = self._read_json(path)
                if not error:
                    artifact["verified"] = True if (
                        expected_size is not None or expected_sha256 is not None
                    ) else None
                    return loaded, None, _json_safe(artifact)
                return failure(
                    str(error.get("error") or "ArtifactReadError"),
                    str(error.get("message") or "Could not read artifact"),
                )
            elif path.name.casefold().endswith(".jsonl"):
                rows, _total, errors = self._jsonl_page(
                    path, 0, min(self._jsonl_count(path), self.MAX_GAME_PAGE)
                )
                if not errors:
                    artifact["verified"] = True if (
                        expected_size is not None or expected_sha256 is not None
                    ) else None
                    return rows, None, _json_safe(artifact)
                first_error = errors[0]
                return failure(
                    str(first_error.get("error") or "ArtifactReadError"),
                    str(first_error.get("message") or "Could not read artifact"),
                )
            return failure(
                "UnsupportedArtifactType",
                f"Unsupported {family} artifact type",
            )
        return failure(
            "ArtifactNotFound",
            f"Referenced {family} artifact was not found inside project root",
        )

    # -- path helpers -----------------------------------------------------

    def _classify_stats_path(self, path: Path) -> tuple[str, str | None, str]:
        parts = self._relative_parts(path)
        lowered = [part.casefold() for part in parts]
        if path == self.project_root / "deck_stats":
            return "root", None, "deck_stats"
        if len(parts) >= 4 and lowered[0] == "logs" and "environment_data" in lowered:
            env_index = lowered.index("environment_data")
            run_id = parts[1]
            scope_parts = parts[env_index + 1:]
            scope = "/".join(scope_parts) or "environment_data"
            role = scope_parts[0].casefold() if scope_parts else ""
            kind = "evaluation" if role == "eval" else "training" if role == "train" else "run"
            return kind, run_id, scope
        if path.name.casefold().startswith("shard_") or any(
                part.casefold().startswith("shard_") for part in parts):
            return "harvest_shard", None, "/".join(parts[-2:])
        if (path / "harvest_run.json").is_file():
            return "harvest", None, path.name
        return "deck_stats", None, "/".join(parts)

    def _first_file(self, directory: Path,
                    names: Iterable[str]) -> Path | None:
        for name in names:
            candidate = directory / name
            if candidate.is_file() and self._is_internal(candidate):
                return candidate
        return None

    @staticmethod
    def _json_document_count(directory: Path) -> int:
        try:
            return sum(
                1 for path in directory.iterdir()
                if path.is_file() and path.name.casefold().endswith((".json", ".json.gz"))
            ) if directory.is_dir() else 0
        except OSError:
            return 0

    def _signature(self, path: Path) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ino", 0)
        except (OSError, ValueError):
            return None

    def _is_internal(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.project_root)
            return True
        except (OSError, ValueError):
            return False

    def _relative(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except (OSError, ValueError):
            return None

    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        try:
            return path.resolve().relative_to(self.project_root).parts
        except (OSError, ValueError):
            return ()

    def _path_sort_key(self, path: Path) -> str:
        return (self._relative(path) or str(path)).casefold()

    def _file_error(self, path: Path, kind: str, message: str) -> dict[str, Any]:
        return {
            "path": self._relative(path),
            "error": kind,
            "message": message,
        }
