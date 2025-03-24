
import json
import os

# Define file paths relative to your project directory
cards_filepath = os.path.join("Json files", "default-cards-20250223100834.json")
rulings_filepath = os.path.join("Json files", "rulings-20250223220040.json")

# Load cards data
with open(cards_filepath, 'r', encoding='utf-8') as f:
    cards_data = json.load(f)
print(f"Loaded {len(cards_data)} cards.")

# Load rulings data
with open(rulings_filepath, 'r', encoding='utf-8') as f:
    rulings_data = json.load(f)
print(f"Loaded {len(rulings_data)} rulings.")

# Create a dictionary to map oracle_id to its rulings
rulings_by_oracle = {}
for ruling in rulings_data:
    oracle_id = ruling.get('oracle_id')
    if oracle_id:
        rulings_by_oracle.setdefault(oracle_id, []).append(ruling)

# Optionally, attach rulings to each card (if the card has an 'oracle_id')
for card in cards_data:
    card_oracle_id = card.get('oracle_id')
    if card_oracle_id and card_oracle_id in rulings_by_oracle:
        card['rulings'] = rulings_by_oracle[card_oracle_id]
    else:
        card['rulings'] = []  # No rulings available for this card

# Example: Print out the rulings for the first card (if any)
first_card = cards_data[0]
print("Card Name:", first_card.get("name"))
print("Rulings:", first_card.get("rulings"))
