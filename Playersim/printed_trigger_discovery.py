"""Lexically inventory printed triggered-ability surfaces.

This module deliberately does not depend on the ability parser.  It provides
an independent, side-effect-free inventory that callers can reconcile against
registered runtime abilities.  A parser omission therefore remains visible as
an unmatched printed surface instead of silently disappearing from coverage.

The inventory is lexical evidence, not a rules interpretation.  Parenthetical
reminder text is masked before ordinary trigger discovery.  Named mechanics
whose reminder text grants a triggered ability can be represented explicitly;
currently Offspring is the only such mechanic synthesized here.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any


PRINTED_TRIGGER_INVENTORY_SCHEMA_VERSION = 1

_TRIGGER_WORD_RE = re.compile(r"\b(Whenever|When|At)\b")
_OFFSPRING_RE = re.compile(r"\boffspring\b", re.IGNORECASE)
_SAFE_GRANT_CONTEXT_RE = re.compile(
    r"\b(?:has|have|with|gains?|gain)\b(?:(?![.;:]).){0,180}$",
    re.IGNORECASE,
)
_ABILITY_WORD_PREFIX_RE = re.compile(
    r"^\s*(?:[\u2022*]\s*)?.{0,120}(?:[\u2013\u2014]|\|)\s*$"
)
_TRIGGER_EVENT_RE = re.compile(
    r"(?:\bat\s+(?:the\s+)?(?:beginning|end)\b|"
    r"\b(?:enters?|attacks?|attacked|blocks?|dies?|casts?|plays?|"
    r"discards?|draws?|gains?|loses?|deals?|becomes?|leaves?|"
    r"taps?|untaps?|sacrifices?|exiles?|mills?|targets?|countered|"
    r"transforms?|unlocks?|surveils?|investigates?|commits?|"
    r"activates?|pays?|expends?|cycles?|explores?|saddles?|crews?|"
    r"forages?|discovers?|searches?|puts?)\b|"
    r"\b(?:water|earth|fire|air)bends?\b|"
    r"\bcollects? evidence\b|\bgives? a gift\b|"
    r"\bmanifests? dread\b|\bthere are\b|"
    r"\bis\s+(?:dealt|put|discarded|countered|tapped|untapped|"
    r"turned|returned|exiled|caused)\b|"
    r"\bare\s+(?:dealt|put|discarded|countered|tapped|untapped|"
    r"turned|returned|exiled|attacked|caused)\b|"
    r"\byou(?:'re| are) dealt\b)",
    re.IGNORECASE,
)
_EFFECT_START_RE = re.compile(
    r"^\s*(?:if\b|when(?:ever)?\b|at\b|otherwise\b|then\b|until\b|"
    r"for each\b|for as long\b|where\b|up to\b|"
    r"any (?:number|opponent)\b|an(?:other)?\b|you\b|each\b|"
    r"target\b|that\b|this\b|it\b|its\b|they\b|those\b|"
    r"creatures?\b|the (?:controller|owner|player|exiled)\b|"
    r"(?:add|amass|attach|blight|bolster|choose|connive|copy|create|"
    r"counter|destroy|discard|discover|draw|earthbend|exile|explore|"
    r"fight|gain|investigate|learn|look|manifest|mill|pay|populate|"
    r"proliferate|put|remove|return|reveal|sacrifice|scry|search|"
    r"suspect|surveil|tap|transform|untap|venture)\b)",
    re.IGNORECASE,
)
_PROPER_NAME_EFFECT_RE = re.compile(
    r"^\s*[A-Z][^,]{0,100}\b(?:gets?|gains?|loses?|deals?|becomes?|"
    r"has|is)\b"
)


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _raw_card(entry_or_raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(entry_or_raw, Mapping):
        raise TypeError("printed-trigger discovery requires a card mapping")
    nested = entry_or_raw.get("raw")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise TypeError("selected card entry 'raw' must be a mapping")
        return nested
    return entry_or_raw


def _text_surfaces(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return top-level oracle text followed by face text in printed order."""
    surfaces: list[dict[str, Any]] = []
    top_text = raw.get("oracle_text")
    if isinstance(top_text, str) and top_text:
        surfaces.append({
            "surface_order": 0,
            "surface": "top_level",
            "face_index": None,
            "face_name": str(raw.get("name") or ""),
            "oracle_text": top_text,
        })

    faces = raw.get("card_faces") or []
    if isinstance(faces, (list, tuple)):
        for face_index, face in enumerate(faces):
            if not isinstance(face, Mapping):
                continue
            face_text = face.get("oracle_text")
            if not isinstance(face_text, str) or not face_text:
                continue
            surfaces.append({
                "surface_order": face_index + 1,
                "surface": "card_face",
                "face_index": face_index,
                "face_name": str(
                    face.get("name") or raw.get("name") or ""),
                "oracle_text": face_text,
            })
    return surfaces


def _mask_parenthetical_text(text: str) -> tuple[str, tuple[bool, ...]]:
    """Mask balanced parenthetical reminder text without changing offsets."""
    masked = list(text)
    hidden = [False] * len(text)
    depth = 0
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
            hidden[index] = True
        elif character == ")" and depth:
            hidden[index] = True
            depth -= 1
        elif depth:
            hidden[index] = True
        if hidden[index] and character not in "\r\n":
            masked[index] = " "
    return "".join(masked), tuple(hidden)


def _line_ranges(text: str):
    offset = 0
    for line_index, retained_line in enumerate(text.splitlines(keepends=True)):
        line = retained_line.rstrip("\r\n")
        yield line_index, offset, line
        offset += len(retained_line)
    if not text:
        return
    if not text.splitlines(keepends=True):
        yield 0, 0, text


def _is_trigger_boundary(prefix: str) -> bool:
    if not prefix.strip():
        return True
    if re.search(r"[.!?][\"'\u2019\u201d]?\s*(?:[\u2022*]\s*)?$", prefix):
        return True
    return bool(_ABILITY_WORD_PREFIX_RE.fullmatch(prefix))


def _quote_spans(line: str) -> list[tuple[int, int, int, int]]:
    """Return (quote start/end, body start/end) for simple oracle quotes."""
    spans: list[tuple[int, int, int, int]] = []
    for match in re.finditer(r'"([^"\n]*)"|\u201c([^\u201d\n]*)\u201d', line):
        body_group = 1 if match.group(1) is not None else 2
        spans.append((
            match.start(), match.end(),
            match.start(body_group), match.end(body_group),
        ))
    return spans


def _condition_prefix(source_text: str) -> str:
    """Return the lexical event prefix without importing the rules parser."""
    stripped = str(source_text or "").strip()
    event_complete_delimiters = []
    for delimiter in re.finditer(r"[,\u2013\u2014:]", stripped):
        # A comma inside a printed number is not a grammar boundary.
        if (delimiter.group() == "," and delimiter.start() > 0
                and delimiter.end() < len(stripped)
                and stripped[delimiter.start() - 1].isdigit()
                and stripped[delimiter.end()].isdigit()):
            continue
        left = stripped[:delimiter.start()]
        if not _TRIGGER_EVENT_RE.search(left):
            continue
        event_complete_delimiters.append(delimiter)
        right = stripped[delimiter.end():]
        if (_EFFECT_START_RE.match(right)
                or _PROPER_NAME_EFFECT_RE.match(right)):
            return " ".join(left.split())

    # Preserve fail-visible behavior for novel effect vocabulary.  Crucially,
    # commas before the event verb (names, subtype lists, modifiers) are never
    # candidates, even on this fallback path.
    if event_complete_delimiters:
        left = stripped[:event_complete_delimiters[0].start()]
        return " ".join(left.split())
    sentence = re.split(r"[.!?]", stripped, maxsplit=1)[0]
    return " ".join(sentence.split())


def _record_identity(raw: Mapping[str, Any], record: Mapping[str, Any],
                     *, prefix: str) -> dict[str, Any]:
    payload = {
        "card_name": str(raw.get("name") or ""),
        "oracle_id": raw.get("oracle_id"),
        "record": dict(record),
    }
    digest = _canonical_hash(payload)
    result = dict(record)
    result["id"] = f"{prefix}:{digest[:24]}"
    result["sha256"] = digest
    return result


def _base_locator(raw: Mapping[str, Any], surface: Mapping[str, Any],
                  *, line_index: int | None,
                  start_offset: int | None) -> dict[str, Any]:
    return {
        "card_name": str(raw.get("name") or ""),
        "oracle_id": raw.get("oracle_id"),
        "surface": surface["surface"],
        "face_index": surface["face_index"],
        "face_name": surface["face_name"],
        "line_index": line_index,
        "start_offset": start_offset,
    }


def _discover_surface(raw: Mapping[str, Any], surface: Mapping[str, Any]):
    text = surface["oracle_text"]
    masked_text, hidden = _mask_parenthetical_text(text)
    trigger_drafts: list[dict[str, Any]] = []
    unmatched_drafts: list[dict[str, Any]] = []
    accepted_offsets: set[int] = set()

    for line_index, line_offset, line in _line_ranges(text):
        masked_line = masked_text[line_offset:line_offset + len(line)]
        raw_candidates = list(_TRIGGER_WORD_RE.finditer(line))
        explicit = [
            match for match in _TRIGGER_WORD_RE.finditer(masked_line)
            if _is_trigger_boundary(masked_line[:match.start()])
        ]

        quote_spans = _quote_spans(line)
        granted: list[tuple[re.Match[str], int, int]] = []
        for quote_start, _quote_end, body_start, body_end in quote_spans:
            masked_body = masked_line[body_start:body_end]
            if not _SAFE_GRANT_CONTEXT_RE.search(
                    masked_line[:quote_start]):
                continue
            for match in _TRIGGER_WORD_RE.finditer(masked_body):
                if _is_trigger_boundary(masked_body[:match.start()]):
                    granted.append((match, body_start, body_end))

        starts: list[tuple[int, str, str, int]] = []
        for match in explicit:
            starts.append((match.start(), "explicit", match.group(1), len(line)))
        for match, body_start, body_end in granted:
            starts.append((
                body_start + match.start(), "granted_quoted",
                match.group(1), body_end,
            ))
        starts.sort(key=lambda value: (value[0], value[1]))

        for start, discovery, trigger_word, natural_end in starts:
            if discovery == "explicit":
                later_starts = [
                    candidate_start for candidate_start, candidate_discovery,
                    _candidate_word, _candidate_end in starts
                    if candidate_discovery == "explicit"
                    and candidate_start > start
                ]
            else:
                later_starts = [
                    candidate_start for candidate_start, candidate_discovery,
                    _candidate_word, candidate_end in starts
                    if candidate_discovery == "granted_quoted"
                    and candidate_end == natural_end
                    and candidate_start > start
                ]
            end = min(later_starts) if later_starts else natural_end
            source_text = line[start:end].strip()
            if not source_text:
                continue
            absolute_start = line_offset + start
            accepted_offsets.add(absolute_start)
            draft = _base_locator(
                raw, surface, line_index=line_index,
                start_offset=absolute_start)
            draft.update({
                "kind": "printed_trigger",
                "discovery": discovery,
                "synthetic": False,
                "trigger_word": trigger_word,
                "trigger_condition_prefix": _condition_prefix(source_text),
                "source_text": source_text,
                "line_text": line.strip(),
                "_surface_order": surface["surface_order"],
                "_sort_offset": absolute_start,
            })
            trigger_drafts.append(draft)

        for candidate in raw_candidates:
            absolute_start = line_offset + candidate.start()
            if absolute_start in accepted_offsets:
                continue
            in_quote = any(
                body_start <= candidate.start() < body_end
                for _quote_start, _quote_end, body_start, body_end in quote_spans
            )
            if hidden[absolute_start]:
                reason = "reminder_text"
            elif in_quote:
                reason = "quoted_without_safe_grant_context"
            else:
                reason = "not_line_or_sentence_boundary"
            draft = _base_locator(
                raw, surface, line_index=line_index,
                start_offset=absolute_start)
            draft.update({
                "kind": "unmatched_trigger_lexeme",
                "trigger_word": candidate.group(1),
                "reason": reason,
                "source_text": line[candidate.start():].strip(),
                "line_text": line.strip(),
                "_surface_order": surface["surface_order"],
                "_sort_offset": absolute_start,
            })
            unmatched_drafts.append(draft)

    return trigger_drafts, unmatched_drafts


def _offspring_drafts(raw: Mapping[str, Any],
                      surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keywords = raw.get("keywords") or []
    if isinstance(keywords, str):
        keyword_present = keywords.casefold() == "offspring"
    else:
        keyword_present = (
            isinstance(keywords, (list, tuple, set, frozenset))
            and any(str(keyword).casefold() == "offspring"
                    for keyword in keywords)
        )
    faces = raw.get("card_faces") or []
    if isinstance(faces, (list, tuple)):
        for face in faces:
            if not isinstance(face, Mapping):
                continue
            face_keywords = face.get("keywords") or []
            if isinstance(face_keywords, str):
                keyword_present = (
                    keyword_present
                    or face_keywords.casefold() == "offspring")
            elif isinstance(face_keywords, (list, tuple, set, frozenset)):
                keyword_present = keyword_present or any(
                    str(keyword).casefold() == "offspring"
                    for keyword in face_keywords)
    text_present = any(
        _OFFSPRING_RE.search(surface["oracle_text"])
        for surface in surfaces
    )
    if not keyword_present and not text_present:
        return []

    drafts: list[dict[str, Any]] = []
    matching_lines: list[tuple[dict[str, Any], int, int, str]] = []
    for surface in surfaces:
        for line_index, line_offset, line in _line_ranges(surface["oracle_text"]):
            match = _OFFSPRING_RE.search(line)
            if match:
                matching_lines.append((
                    surface, line_index, line_offset + match.start(), line))

    if not matching_lines:
        fallback_surface = surfaces[0] if surfaces else {
            "surface_order": 0,
            "surface": "top_level",
            "face_index": None,
            "face_name": str(raw.get("name") or ""),
            "oracle_text": "",
        }
        matching_lines.append((fallback_surface, None, -1, "Offspring"))

    for surface, line_index, start_offset, line in matching_lines:
        cost_match = re.search(
            r"\bOffspring\s+([^\s(]+)", line, re.IGNORECASE)
        draft = _base_locator(
            raw, surface, line_index=line_index,
            start_offset=None if start_offset < 0 else start_offset)
        draft.update({
            "kind": "printed_trigger",
            "discovery": "keyword_offspring",
            "synthetic": True,
            "trigger_word": "When",
            "trigger_condition_prefix": (
                "When this permanent enters, if its offspring cost was paid"),
            "source_text": line.strip() or "Offspring",
            "line_text": line.strip() or "Offspring",
            "keyword": "Offspring",
            "offspring_cost": cost_match.group(1) if cost_match else None,
            "_surface_order": surface["surface_order"],
            "_sort_offset": start_offset,
        })
        drafts.append(draft)
    return drafts


def discover_printed_trigger_inventory(
        entry_or_raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic lexical trigger inventory for one printed card.

    ``entry_or_raw`` may be either a raw Scryfall-like card mapping or the
    selected-card wrapper used by :mod:`Playersim.card_probe` (with a ``raw``
    member).  The input mapping and all nested values are only read.
    """
    raw = _raw_card(entry_or_raw)
    surfaces = _text_surfaces(raw)
    trigger_drafts: list[dict[str, Any]] = []
    unmatched_drafts: list[dict[str, Any]] = []
    for surface in surfaces:
        discovered, unmatched = _discover_surface(raw, surface)
        trigger_drafts.extend(discovered)
        unmatched_drafts.extend(unmatched)
    trigger_drafts.extend(_offspring_drafts(raw, surfaces))

    trigger_drafts.sort(key=lambda row: (
        row["_surface_order"], row["_sort_offset"], row["discovery"],
        row["source_text"],
    ))
    unmatched_drafts.sort(key=lambda row: (
        row["_surface_order"], row["_sort_offset"], row["reason"],
    ))

    triggers = []
    for draft in trigger_drafts:
        record = {key: value for key, value in draft.items()
                  if not key.startswith("_")}
        triggers.append(_record_identity(
            raw, record, prefix="printed-trigger"))

    unmatched = []
    for draft in unmatched_drafts:
        record = {key: value for key, value in draft.items()
                  if not key.startswith("_")}
        unmatched.append(_record_identity(
            raw, record, prefix="unmatched-trigger-lexeme"))

    inventory: dict[str, Any] = {
        "schema_version": PRINTED_TRIGGER_INVENTORY_SCHEMA_VERSION,
        "card_name": str(raw.get("name") or ""),
        "oracle_id": raw.get("oracle_id"),
        "triggers": triggers,
        "unmatched_lexical_surfaces": unmatched,
    }
    inventory["sha256"] = _canonical_hash(inventory)
    return inventory


def discover_printed_triggers(
        entry_or_raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convenience API returning only the printed-trigger records."""
    return discover_printed_trigger_inventory(entry_or_raw)["triggers"]


__all__ = [
    "PRINTED_TRIGGER_INVENTORY_SCHEMA_VERSION",
    "discover_printed_trigger_inventory",
    "discover_printed_triggers",
]
