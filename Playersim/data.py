"""Scryfall bulk-data loader: joins card data with rulings by oracle_id.

Previously this module ran all of its file loading at import time with paths
relative to the current working directory, so merely importing it (or any
tool that imports every module in the package) crashed unless you happened
to launch Python from the right folder. The logic now lives in functions,
paths resolve relative to this file, and the demo only runs under
`python -m Playersim.data` / `python data.py`.
"""

import json
import os

# Resolve relative to this file's parent (the repo root), not the CWD.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CARDS_PATH = os.path.join(_REPO_ROOT, "Json files", "default-cards-20250223100834.json")
DEFAULT_RULINGS_PATH = os.path.join(_REPO_ROOT, "Json files", "rulings-20250223220040.json")


def load_cards_with_rulings(cards_filepath=DEFAULT_CARDS_PATH,
                            rulings_filepath=DEFAULT_RULINGS_PATH):
    """Load Scryfall card and rulings dumps and attach rulings to each card.

    Returns:
        list: card dicts, each with a 'rulings' key (possibly an empty list).
    """
    with open(cards_filepath, 'r', encoding='utf-8') as f:
        cards_data = json.load(f)
    print(f"Loaded {len(cards_data)} cards.")

    with open(rulings_filepath, 'r', encoding='utf-8') as f:
        rulings_data = json.load(f)
    print(f"Loaded {len(rulings_data)} rulings.")

    # Map oracle_id -> list of rulings
    rulings_by_oracle = {}
    for ruling in rulings_data:
        oracle_id = ruling.get('oracle_id')
        if oracle_id:
            rulings_by_oracle.setdefault(oracle_id, []).append(ruling)

    # Attach rulings to each card (empty list when none exist)
    for card in cards_data:
        card_oracle_id = card.get('oracle_id')
        card['rulings'] = rulings_by_oracle.get(card_oracle_id, [])

    return cards_data


if __name__ == "__main__":
    cards = load_cards_with_rulings()
    first_card = cards[0]
    print("Card Name:", first_card.get("name"))
    print("Rulings:", first_card.get("rulings"))
