import json
import os

ORACLE_FILEPATH = os.path.join("Mtg_Cards", "oracle-cards-20260709210257.jsonl")
FORMAT_LISTS_DIR = "Format Card Lists"


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


def main():
    cards = load_oracle_cards()
    write_format_card_lists(cards)


if __name__ == "__main__":
    main()
