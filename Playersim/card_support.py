"""Card support manifest (July 2026).

Tracks cards the engine cannot fully or faithfully handle, so that
(a) support can be added deliberately, card by card, and
(b) the downstream deck-construction AI can EXCLUDE or down-weight those
    cards until support lands, instead of building decks around cards whose
    effects silently no-op.

The manifest is a process-wide accumulator persisted as JSON next to the
deck statistics (``card_support_manifest.json``). Persisting merges with the
existing file, so counts accumulate across games and process restarts.

Severity ladder (worst sticks per card):
    crash    -- handling this card raised an exception
    unparsed -- an entire effect/ability produced nothing the engine can run
    partial  -- some clauses parsed; at least one fell back to a no-op

Consumption contract (documented in STATS_SCHEMA.md): the deck builder
should exclude 'crash' and 'unparsed' cards from candidate pools, and treat
'partial' cards' statistics as lower-confidence.
"""

import json
import logging
import os
import threading
from datetime import date

_SEVERITY_RANK = {"partial": 0, "unparsed": 1, "crash": 2}


class CardSupportManifest:
    """Accumulates per-card support issues; persists/merges to JSON."""

    FILENAME = "card_support_manifest.json"

    def __init__(self):
        self._lock = threading.Lock()
        # {card_name: {"reasons": {reason: count}, "severity": str,
        #              "count": int, "first_seen": iso, "last_seen": iso}}
        self.entries = {}

    def report(self, card_name, reason, severity="partial"):
        """Record one support issue for ``card_name``.

        ``reason`` should identify the failing clause or mechanism (it is
        truncated for storage). Unknown severities are treated as 'partial'.
        Returns the entry dict for convenience.
        """
        if not card_name:
            return None
        card_name = str(card_name)
        reason = (str(reason) or "unspecified")[:120]
        if severity not in _SEVERITY_RANK:
            severity = "partial"
        today = date.today().isoformat()
        with self._lock:
            entry = self.entries.setdefault(card_name, {
                "reasons": {},
                "severity": severity,
                "count": 0,
                "first_seen": today,
                "last_seen": today,
            })
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
            entry["count"] += 1
            entry["last_seen"] = today
            if _SEVERITY_RANK[severity] > _SEVERITY_RANK.get(entry["severity"], 0):
                entry["severity"] = severity
            return entry

    def persist(self, directory):
        """Merge-write the manifest into ``directory``/card_support_manifest.json.

        Loads whatever is on disk first so multiple processes / restarts
        accumulate rather than clobber. Never raises: persistence failures
        are logged and swallowed (the game must not die for telemetry).
        """
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, self.FILENAME)
            on_disk = {}
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        on_disk = json.load(f) or {}
                except Exception as e:
                    logging.warning(f"Card support manifest unreadable, rewriting: {e}")
                    on_disk = {}
            with self._lock:
                merged = dict(on_disk)
                for name, entry in self.entries.items():
                    if name not in merged:
                        merged[name] = json.loads(json.dumps(entry))
                        continue
                    tgt = merged[name]
                    tgt["count"] = int(tgt.get("count", 0)) + entry["count"]
                    reasons = tgt.setdefault("reasons", {})
                    for r, c in entry["reasons"].items():
                        reasons[r] = int(reasons.get(r, 0)) + c
                    if _SEVERITY_RANK.get(entry["severity"], 0) > _SEVERITY_RANK.get(tgt.get("severity", "partial"), 0):
                        tgt["severity"] = entry["severity"]
                    tgt["first_seen"] = min(tgt.get("first_seen", entry["first_seen"]), entry["first_seen"])
                    tgt["last_seen"] = max(tgt.get("last_seen", entry["last_seen"]), entry["last_seen"])
                tmp_path = path + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(merged, f, indent=2, sort_keys=True)
                os.replace(tmp_path, path)
                # In-memory counts are now on disk; reset so the next persist
                # doesn't double-merge them.
                self.entries = {}
            return path
        except Exception as e:
            logging.error(f"Failed to persist card support manifest: {e}")
            return None

    @staticmethod
    def load(directory):
        """Read the merged manifest from disk (for the deck builder)."""
        path = os.path.join(directory, CardSupportManifest.FILENAME)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f) or {}
        except Exception as e:
            logging.error(f"Failed to load card support manifest: {e}")
            return {}


_manifest = CardSupportManifest()


def get_manifest():
    """Process-wide manifest accumulator."""
    return _manifest


def report_unsupported(card_name, reason, severity="partial"):
    """Convenience: record a support issue on the process-wide manifest."""
    return _manifest.report(card_name, reason, severity)


def reset_manifest_for_tests():
    """Testing hook: clear in-memory entries."""
    global _manifest
    _manifest = CardSupportManifest()

def coverage_report(card_names, directory=None):
    """Join a card pool against the manifest: who is safe to build with?

    Combines the on-disk manifest (if ``directory`` given) with in-memory
    entries. Returns a dict with:
      - total, supported_fraction
      - fully_supported: names with no recorded issues
      - degraded: 'partial' severity (stats are a floor; down-weight)
      - excluded: 'unparsed' or 'crash' (deck builder must avoid)
    """
    merged = {}
    if directory:
        merged.update(CardSupportManifest.load(directory))
    with _manifest._lock:
        for name, entry in _manifest.entries.items():
            if name in merged:
                if _SEVERITY_RANK.get(entry["severity"], 0) > _SEVERITY_RANK.get(merged[name].get("severity", "partial"), 0):
                    merged[name] = dict(merged[name], severity=entry["severity"])
            else:
                merged[name] = entry
    fully, degraded, excluded = [], [], []
    for name in card_names:
        entry = merged.get(name)
        if entry is None:
            fully.append(name)
        elif entry.get("severity") == "partial":
            degraded.append(name)
        else:
            excluded.append(name)
    total = len(card_names)
    return {
        "total": total,
        "fully_supported": fully,
        "degraded": degraded,
        "excluded": excluded,
        "supported_fraction": (len(fully) / total) if total else 1.0,
    }


if __name__ == "__main__":  # pragma: no cover - convenience CLI
    # Usage: python -m Playersim.card_support <deck.json> [stats_dir]
    import sys
    deck_path = sys.argv[1]
    stats_dir = sys.argv[2] if len(sys.argv) > 2 else "./deck_stats"
    with open(deck_path) as f:
        deck = json.load(f)
    names = [e["card"]["name"] if isinstance(e.get("card"), dict) else e.get("card", e)
             for e in deck.get("deck", deck)]
    rep = coverage_report(names, stats_dir)
    print(json.dumps(rep, indent=2))
