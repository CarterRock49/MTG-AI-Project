"""Constructed-deck legality checks used at load and by deck builders."""

from collections import Counter
import re


# Constructed Magic has no numeric maximum, but the simulator must reject
# typo-sized inputs before expanding counts into per-card occurrence lists.
MAX_SIMULATOR_DECK_SIZE = 1000


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def deck_copy_limit(card):
    """Return a card's constructed copy limit, or ``None`` if unlimited.

    Basic lands and cards with an explicit deck-building permission are not
    governed by the ordinary four-copy rule.  The helper accepts either a
    hydrated ``Card`` or a raw snapshot dictionary so format detection and
    final loader validation cannot disagree.
    """
    if isinstance(card, dict):
        oracle_text = str(card.get("oracle_text", "") or "")
        type_line = str(card.get("type_line", "") or "").casefold()
        is_basic = "basic" in type_line and "land" in type_line
    else:
        oracle_text = str(getattr(card, "oracle_text", "") or "")
        types = {
            str(value).casefold()
            for value in getattr(card, "card_types", [])}
        supertypes = {
            str(value).casefold()
            for value in getattr(card, "supertypes", [])}
        is_basic = "basic" in supertypes and "land" in types
    if is_basic:
        return None
    if re.search(
            r"a deck can have any number of cards named\b",
            oracle_text, re.IGNORECASE):
        return None
    capped = re.search(
        r"a deck can have up to\s+(\d+|[a-z-]+)\s+cards named\b",
        oracle_text, re.IGNORECASE)
    if capped:
        value = capped.group(1).casefold()
        if value.isdigit():
            return int(value)
        if value in _NUMBER_WORDS:
            return _NUMBER_WORDS[value]
    return 4


def validate_deck_legality(deck, card_db, format_name=None, banned_names=None,
                           restricted_names=None, minimum_size=60):
    errors = []
    cards = list(deck.get("cards", deck) if isinstance(deck, dict) else deck)
    if len(cards) < minimum_size:
        errors.append(f"deck has {len(cards)} cards; minimum is {minimum_size}")
    if len(cards) > MAX_SIMULATOR_DECK_SIZE:
        errors.append(
            f"deck has {len(cards)} cards; simulator safety limit is "
            f"{MAX_SIMULATOR_DECK_SIZE}")
    banned = {str(name).casefold() for name in (banned_names or [])}
    restricted = {str(name).casefold() for name in (restricted_names or [])}
    counts = Counter(cards)
    for card_id, count in counts.items():
        card = card_db.get(card_id) if isinstance(card_db, dict) else None
        if card is None:
            errors.append(f"unknown card id {card_id}")
            continue
        name = str(getattr(card, "name", card_id))
        key = name.casefold()
        copy_limit = deck_copy_limit(card)
        if copy_limit is not None and count > copy_limit:
            errors.append(
                f"{name}: {count} copies (maximum {copy_limit})")
        if key in banned:
            errors.append(f"{name} is banned")
        if key in restricted and count > 1:
            errors.append(f"{name}: {count} copies (restricted to 1)")
        if format_name:
            status = getattr(card, "legalities", {}).get(str(format_name).lower())
            if status not in ("legal", "restricted"):
                errors.append(f"{name} is {status or 'not listed'} in {format_name}")
            if status == "restricted" and count > 1:
                errors.append(f"{name}: {count} copies (restricted to 1 in {format_name})")
    return errors


def require_legal_deck(*args, **kwargs):
    errors = validate_deck_legality(*args, **kwargs)
    if errors:
        raise ValueError("Illegal deck: " + "; ".join(errors))
    return True
