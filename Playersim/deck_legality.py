"""Constructed-deck legality checks used at load and by deck builders."""

from collections import Counter


def validate_deck_legality(deck, card_db, format_name=None, banned_names=None,
                           restricted_names=None, minimum_size=60):
    errors = []
    cards = list(deck.get("cards", deck) if isinstance(deck, dict) else deck)
    if len(cards) < minimum_size:
        errors.append(f"deck has {len(cards)} cards; minimum is {minimum_size}")
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
        types = {str(t).lower() for t in getattr(card, "card_types", [])}
        is_basic = "basic" in {str(t).lower() for t in getattr(card, "supertypes", [])} and "land" in types
        if not is_basic and count > 4:
            errors.append(f"{name}: {count} copies (maximum 4)")
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
