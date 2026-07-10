import json
import os

ORACLE_FILEPATH = os.path.join("Mtg_Cards", "oracle-cards-20260709210257.jsonl")
FORMAT_LISTS_DIR = "Format Card Lists"
DECKLISTS_DIR = "DeckLists"
DECKS_DIR = "Decks"


def load_oracle_cards():
    cards = []
    with open(ORACLE_FILEPATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cards.append(json.loads(line))
    print(f"Loaded {len(cards)} cards from {ORACLE_FILEPATH}.")
    return cards


def write_format_card_lists(cards):
    # Collect every format that appears in any card's legalities
    formats = set()
    for card in cards:
        formats.update(card.get("legalities", {}))

    for fmt in sorted(formats):
        out_path = os.path.join(FORMAT_LISTS_DIR, f"{fmt}.jsonl")
        if os.path.exists(out_path):
            print(f"Skipping {out_path} (already exists).")
            continue
        legal_cards = [c for c in cards if c.get("legalities", {}).get(fmt) == "legal"]
        with open(out_path, "w", encoding="utf-8") as f:
            for card in legal_cards:
                f.write(json.dumps(card) + "\n")
        print(f"Wrote {len(legal_cards)} legal cards to {out_path}.")


def build_name_lookup(cards):
    # Map lowercase card names to card objects. Double-faced cards are also
    # indexed by their front-face name (the part before " // "), but exact
    # full names always take priority over front-face aliases.
    lookup = {}
    for card in cards:
        name = card.get("name")
        if name:
            lookup.setdefault(name.lower(), card)
    for card in cards:
        name = card.get("name")
        if name and " // " in name:
            front = name.split(" // ")[0]
            lookup.setdefault(front.lower(), card)
    return lookup


def parse_decklist(filepath):
    # Each non-empty line is "<count> <card name>", e.g. "4 Lightning Strike"
    entries = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2 or not parts[0].isdigit():
                raise ValueError(f"Line {line_num} is not '<count> <card name>': {line!r}")
            entries.append((int(parts[0]), parts[1].strip()))
    return entries


def convert_decklists(cards):
    if not os.path.isdir(DECKLISTS_DIR):
        print(f"No {DECKLISTS_DIR} folder found, skipping deck conversion.")
        return

    decklist_files = [f for f in os.listdir(DECKLISTS_DIR) if f.lower().endswith(".txt")]
    if not decklist_files:
        print(f"No deck lists found in {DECKLISTS_DIR}.")
        return

    lookup = build_name_lookup(cards)

    for filename in decklist_files:
        in_path = os.path.join(DECKLISTS_DIR, filename)
        out_path = os.path.join(DECKS_DIR, os.path.splitext(filename)[0] + ".json")
        if os.path.exists(out_path):
            print(f"Skipping {in_path}: {out_path} already exists.")
            continue

        try:
            entries = parse_decklist(in_path)
        except ValueError as e:
            print(f"Error in {in_path}: {e}")
            continue

        deck = []
        unknown = []
        for count, name in entries:
            card = lookup.get(name.lower())
            if card is None:
                unknown.append(name)
            else:
                deck.append({"count": count, "card": card})

        if unknown:
            print(f"Error in {in_path}: card names not found: {', '.join(unknown)}")
            continue

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"deck": deck}, f, indent=4)
        print(f"Wrote {out_path} ({sum(e['count'] for e in deck)} cards).")


def main():
    cards = load_oracle_cards()
    write_format_card_lists(cards)
    convert_decklists(cards)


if __name__ == "__main__":
    main()
