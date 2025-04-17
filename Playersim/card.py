import os
import json
import logging
import re
import numpy as np

from Playersim.ability_utils import EffectFactory
from .debug import DEBUG_MODE

class Card:
    """Encapsulates card attributes and behaviors."""
    # Initialize as empty list instead of setting it externally
    SUBTYPE_VOCAB = []
    
    ALL_CARD_TYPES = [
        'creature', 'artifact', 'enchantment', 'land', 'planeswalker',
        'instant', 'sorcery', 'battle', 'conspiracy', 'dungeon',
        'phenomenon', 'plane', 'scheme', 'vanguard', 'class', 'room' # Add more if needed
    ]
    
    # List of all keywords for optimization
    ALL_KEYWORDS = [
        'flying', 'trample', 'hexproof', 'lifelink', 'deathtouch',
        'first strike', 'double strike', 'vigilance', 'flash', 'haste',
        'menace', 'reach', 'defender', 'indestructible', 'protection',
        'ward', 'prowess', 'scry', 'cascade', 'unblockable', 'shroud',
        'regenerate', 'persist', 'undying', 'riot', 'enrage', 'afflict', 
        'exalted', 'mentor', 'convoke', 'absorb', 'affinity', 'afterlife',
        'amplify', 'annihilator', 'ascend', 'assist', 'aura swap',
        'awaken', 'battle cry', 'bestow', 'blitz', 'bloodthirst', 'boast',
        'bushido', 'buyback', 'casualty', 'champion', 'changeling',
        'cipher', 'cleave', 'companion', 'compleated', 'conspire', 'crew',
        'cycling', 'dash', 'daybound', 'nightbound', 'decayed', 'delve',
        'demonstrate', 'devoid', 'devour', 'disturb', 'dredge', 'echo',
        'embalm', 'emerge', 'enchant', 'encore', 'entwine', 'epic',
        'equip', 'escape', 'eternalize', 'evoke', 'evolve', 'exploit',
        'extort', 'fabricate', 'fading', 'fear', 'flanking', 'flashback',
        'forecast', 'foretell', 'fortify', 'frenzy', 'friends forever',
        'fuse', 'graft', 'gravestorm', 'haunt', 'hidden agenda', 'hideaway',
        'horsemanship', 'improvise', 'infect', 'ingest', 'intimidate',
        'jump-start', 'kicker', 'landwalk', 'level up', 'living weapon',
        'madness', 'melee', 'miracle', 'modular', 'morph', 'mutate',
        'myriad', 'ninjutsu', 'offering', 'outlast', 'overload',
        'partner', 'phasing', 'poisonous', 'provoke', 'prowl', 'rampage',
        'rebound', 'reconfigure', 'recover', 'reinforce', 'renown',
        'replicate', 'retrace', 'ripple', 'scavenge', 'shadow', 'skulk',
        'soulbond', 'soulshift', 'spectacle', 'splice', 'split second',
        'storm', 'sunburst', 'surge', 'suspend', 'totem armor', 'training',
        'transfigure', 'transmute', 'tribute', 'undaunted', 'unearth',
        'unleash', 'vanishing', 'wither', 'cumulative upkeep', 'banding',
        'aftermath', 'spree'
    ]

    def __init__(self, card_data):
        # Ensure card_data has all required fields with defaults
        self.name = card_data.get("name", f"Unknown Card {id(self)}")
        self.mana_cost = card_data.get("mana_cost", "")
        self.type_line = card_data.get("type_line", "unknown").lower()
        self.card_id = None # Initialize as None
        # Handle both 'faces' (internal format) and 'card_faces' (Scryfall API format)
        self.faces = card_data.get("faces", None) or card_data.get("card_faces", None)
        if self.faces:
            self.current_face = 0  # 0: front face, 1: back face
            self.is_transformed = False # Add is_transformed attribute
        else:
            self.current_face = None
            self.is_transformed = False

        # Parse type line using enhanced method
        self.card_types, self.subtypes, self.supertypes = self.parse_type_line(self.type_line)

        self.cmc = card_data.get("cmc", 0)
        self.power = self._safe_int(card_data.get("power", "0"))
        self.toughness = self._safe_int(card_data.get("toughness", "0"))
        self.oracle_text = card_data.get("oracle_text", "")
        self.keywords = self._extract_keywords(self.oracle_text.lower())
        self.colors = self._extract_colors(card_data.get("color_identity", []))
        self.subtype_vector = []

        # Add card_id property (will be set when the card is registered)
        self.card_id = None

        # Performance tracking and text embedding
        self.performance_rating = 0.5  # Initial default rating (range 0-1)
        self.usage_count = 0
        self.embedding = None  # This will be set later by an embedding system
        
        # Track counters on the card
        self.counters = {}
        
        # Initialize card type-specific attributes and parse corresponding data
        # Spree attributes
        self.is_spree = False
        self.spree_modes = []
        self._parse_spree_modes()
        
        # Room attributes
        self.is_room = False
        self.door1 = {}
        self.door2 = {}
        self._parse_room_data(card_data)
        
        # Class attributes
        self.is_class = False
        self.levels = []
        self.current_level = 1
        self.all_abilities = []
        self._parse_class_data(card_data)
        
        # Planeswalker attributes (if applicable)
        if 'planeswalker' in self.card_types:
            self._init_planeswalker(card_data)
            
    def reset_state_on_zone_change(self):
         """Reset temporary states when card leaves battlefield (e.g., flip, morph)."""
         # Reset morph/manifest state
         if hasattr(self, 'face_down') and self.face_down:
             gs = self.game_state # Need access to game state
             # Restore original state if possible
             original_info = None
             if self.card_id in getattr(gs, 'morphed_cards', {}):
                 original_info = gs.morphed_cards[self.card_id]['original']
                 del gs.morphed_cards[self.card_id]
             elif self.card_id in getattr(gs, 'manifested_cards', {}):
                 original_info = gs.manifested_cards[self.card_id]['original']
                 del gs.manifested_cards[self.card_id]

             if original_info:
                 temp_card = Card(original_info) # Create temp instance to read from
                 self.name = getattr(temp_card, 'name', self.name)
                 self.power = getattr(temp_card, 'power', self.power)
                 # ... restore other properties ...
                 self.face_down = False
                 logging.debug(f"Resetting face-down state for {self.name} leaving battlefield.")

         # Reset Class level? Usually Class state persists, check rules.
         # if hasattr(self, 'is_class') and self.is_class: self.current_level = 1

         # Reset flip state? Usually flip cards don't un-flip easily. Check specific card rules.

         # Reset counters (already happens if GS clears card.counters on move)
         # Ensure counters are cleared if needed
         self.counters = {}

         # Reset temporary attachments? Should be handled by GS attachment logic.
            
    def parse_type_line(self, type_line):
        """Enhanced parsing of type line with support for em dash separator."""
        if not type_line:
            return [], [], []

        normalized = type_line.lower().strip()
        if '//' in normalized:
            normalized = normalized.split('//')[0].strip()

        supertypes, card_types, subtypes = [], [], []
        # Handle both em dash and hyphen as subtype separators
        if '—' in normalized or '-' in normalized:
            separator = '—' if '—' in normalized else '-'
            main_types, subtype_text = normalized.split(separator, 1)
            subtypes = [s.strip() for s in subtype_text.strip().split()]
        else:
            main_types = normalized

        known_supertypes = ['legendary', 'basic', 'world', 'snow', 'tribal']
        known_card_types = Card.ALL_CARD_TYPES # Use class variable

        main_type_parts = main_types.split()
        for part in main_type_parts:
            if part in known_supertypes:
                supertypes.append(part)
            elif part in known_card_types:
                card_types.append(part)
            # Ignore potential unknown super/card types if needed, or add to a specific list

        return card_types, subtypes, supertypes
            
    def get_transform_trigger_type(self):
        """
        Determine what type of transformation trigger this card has.
        
        Returns:
            str: The type of transformation trigger, or None if not identified
            Possible values: 'day/night', 'flip', 'meld', 'manual', 'condition', 'cost'
        """
        if not self.faces:
            return None
            
        if not hasattr(self, 'oracle_text') or not self.oracle_text:
            return None
            
        oracle_text = self.oracle_text.lower()
        
        # Check for daybound/nightbound
        if 'daybound' in oracle_text or 'nightbound' in oracle_text:
            return 'day/night'
            
        # Check for werewolf transformation
        if 'werewolf' in oracle_text and 'transform' in oracle_text:
            return 'day/night'
            
        # Check for meld
        if 'meld' in oracle_text:
            return 'meld'
            
        # Check for cost-based transformation
        cost_patterns = [
            r'\{[^}]+\}:\s*transform',
            r'pay [^.]+to transform',
            r'discard [^.]+to transform'
        ]
        for pattern in cost_patterns:
            if re.search(pattern, oracle_text):
                return 'cost'
                
        # Check for condition-based transformation
        condition_patterns = [
            r'when [^.]+, transform',
            r'whenever [^.]+, transform',
            r'at the beginning of [^.]+, transform'
        ]
        for pattern in condition_patterns:
            if re.search(pattern, oracle_text):
                return 'condition'
        
        # Default to manual transformation if nothing else matched
        return 'manual'
            
    def can_transform(self, game_state=None):
        """
        Check if this card can transform in the current game state.
        
        Args:
            game_state: Optional game state object to check additional conditions
            
        Returns:
            bool: Whether the card can transform
        """
        # Must be a transforming double-faced card
        if not self.faces or len(self.faces) < 2:
            return False
            
        # Check if this is a transforming DFC (not a modal DFC)
        if not self.is_transforming_mdfc():
            return False
        
        # Check if the card is in a valid zone for transformation
        # Typically battlefield, but some cards transform in other zones
        if game_state and hasattr(game_state, 'get_card_zone'):
            zone = game_state.get_card_zone(self.card_id)
            if zone != 'battlefield' and 'hand' not in zone and 'exile' not in zone:
                return False
        
        # Check for transformation restrictions in the card text
        if hasattr(self, 'oracle_text'):
            restrictions = [
                "transforms only at night",
                "transforms only during your turn",
                "transforms only once each turn",
                "can't transform"
            ]
            
            for restriction in restrictions:
                if restriction in self.oracle_text.lower():
                    # If we have a game state, we could check if the restriction is satisfied
                    if not game_state:
                        return False
                    
                    # Basic night/day checking
                    if "only at night" in restriction and hasattr(game_state, 'is_night'):
                        return game_state.is_night
                        
                    # Turn checking
                    if "during your turn" in restriction and hasattr(game_state, 'is_player_turn'):
                        controller = game_state.get_card_controller(self.card_id)
                        return game_state.is_player_turn(controller)
                        
                    # Once per turn tracking
                    if "once each turn" in restriction and hasattr(game_state, 'transformed_this_turn'):
                        return self.card_id not in game_state.transformed_this_turn
                        
                    # Can't transform
                    if "can't transform" in restriction:
                        return False
        
        return True
    #
    # Spree card handling methods
    #

    def _parse_spree_modes(self):
        """
        Enhanced parsing of Spree modes with comprehensive handling of variations,
        including em dash separators. Ensures Spree mode text is handled distinctly.

        Parsing strategy:
        1. Identify if the card has the Spree keyword.
        2. Extract the text block containing the modes.
        3. Use regex to find individual modes, matching "+ COST {—|-} EFFECT".
        4. Parse details for each mode using helper functions.
        5. IMPORTANT: Mark the main card text that constitutes the Spree mechanic itself
           so it isn't parsed again as a separate static/triggered ability later.
        """
        # Reset spree-related attributes
        self.is_spree = False
        self.spree_modes = [] # Store list of mode dictionaries
        self._spree_related_text_marker = "" # Store text block belonging to spree

        # Check if card has oracle text
        if not hasattr(self, 'oracle_text') or not self.oracle_text:
            return # Cannot parse without text

        # Normalize oracle text for consistent parsing
        # Remove reminder text first
        oracle_text_cleaned = re.sub(r'\s*\([^()]*?\)\s*', ' ', self.oracle_text).strip()
        oracle_text_lower = oracle_text_cleaned.lower() # Use lowercase for matching

        # Robust Spree identification using keyword
        spree_keyword_match = re.search(r'\bspree\b', oracle_text_lower)
        if not spree_keyword_match:
            return # Not a spree card

        self.is_spree = True

        try:
            # --- Extract Modes Text and Mark ---
            # Find the start of the modes list, usually after "spree" keyword or introductory text.
            # Find the first '+' indicating a mode, potentially after the "spree" keyword.
            spree_block_start_index = spree_keyword_match.start()
            # Find first '+' AFTER the spree keyword
            first_mode_plus_match = re.search(r'\n\s*\+\s*', oracle_text_cleaned[spree_keyword_match.end():])
            modes_text_block = ""
            if first_mode_plus_match:
                 # The block starts from the '+' sign found after 'spree'
                 modes_start_offset = spree_keyword_match.end() + first_mode_plus_match.start()
                 modes_text_block = oracle_text_cleaned[modes_start_offset:].strip()
                 # Mark the entire text from 'Spree' keyword onwards as processed by this parser
                 self._spree_related_text_marker = oracle_text_cleaned[spree_block_start_index:].strip()
            else:
                 # Fallback: Maybe modes directly follow spree keyword without '+'? Or take rest of text?
                 # This case is less defined. Mark from spree onwards for now.
                 modes_text_block = oracle_text_cleaned[spree_keyword_match.end():].strip()
                 self._spree_related_text_marker = modes_text_block


            if not modes_text_block:
                 # If no modes found after keyword, still mark the keyword itself.
                 self._spree_related_text_marker = oracle_text_cleaned[spree_keyword_match.start() : spree_keyword_match.end()].strip()
                 logging.debug(f"Spree keyword found for {self.name}, but no modes parsed. Marking '{self._spree_related_text_marker}'.")
                 return # Stop parsing modes if block is empty

            # --- Parse Individual Modes ---
            # Regex: Find '+' sign, capture cost in {}, then capture effect after '-' or '—' until newline or end
            # Pattern accepts '-' OR '—', with optional whitespace around them.
            mode_pattern = r'^\+\s*(\{.+?\})\s*[-—\u2014]\s*(.*?)(?=(?:\n\s*\+)|$)' # Matches start of line ^, accepts dashes
            # Apply findall on the isolated modes_text_block line by line or using MULTILINE
            mode_matches = re.findall(mode_pattern, modes_text_block, re.MULTILINE | re.DOTALL)


            if not mode_matches:
                 logging.warning(f"No spree modes matched pattern for {self.name} in block: '{modes_text_block[:100]}...'")
                 # Mark the text block anyway, even if parsing failed
                 self._spree_related_text_marker = oracle_text_cleaned[spree_block_start_index:].strip()
                 return

            for cost_text, effect_text in mode_matches:
                # Basic cleanup of captured groups
                cost_cleaned = cost_text.strip()
                # Strip leading/trailing whitespace/newlines and potential trailing punctuation from effect
                effect_cleaned = re.sub(r'[\s\n]+$','', effect_text).strip().rstrip('.').strip()

                if not cost_cleaned or not effect_cleaned:
                     logging.warning(f"Skipped poorly matched spree mode for {self.name}: Cost='{cost_cleaned}', Effect='{effect_cleaned}'")
                     continue

                # Use helper functions to parse details (kept from original structure)
                mode_details = {
                    'cost': cost_cleaned,
                    'effect': effect_cleaned,
                    'cost_type': self._analyze_cost_type(cost_cleaned),
                    'cost_value': self._parse_cost_value(cost_cleaned), # Simple numeric value if applicable
                    'effect_details': self._parse_effect_details(effect_cleaned) # Detailed parsing
                }

                self.spree_modes.append(mode_details)
                logging.debug(f"Parsed Spree Mode for {self.name}: Cost='{mode_details['cost']}', Effect='{mode_details['effect'][:50]}...'")

            if not self.spree_modes:
                 logging.warning(f"Identified {self.name} as Spree card, but failed to parse any modes.")

            # Mark the parsed block
            self._spree_related_text_marker = oracle_text_cleaned[spree_block_start_index:].strip()

        except Exception as e:
            logging.error(f"Complex Spree mode parsing error for {self.name}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            # Ensure attributes are reset/empty on error
            self.is_spree = False
            self.spree_modes = []
            self._spree_related_text_marker = ""
            
    def _analyze_cost_type(self, cost_text):
        """Determine cost type (mana, life, sacrifice, etc.)"""
        if '{' in cost_text and '}' in cost_text:
            return 'mana'
        if 'life' in cost_text.lower():
            return 'life'
        if 'sacrifice' in cost_text.lower():
            return 'sacrifice'
        return 'generic'

    def _parse_cost_value(self, cost_text):
        """Extract numeric value from cost"""
        match = re.search(r'\{(\d+)\}', cost_text)
        return int(match.group(1)) if match else None
    
    def _parse_effect_details(self, effect_text):
        """
        Parse detailed effect information
        Returns a dictionary with parsed effect components
        """
        details = {
            'type': None,
            'targets': [],
            'conditions': [],
            'restrictions': []
        }
        
        # Effect type identification
        effect_types = {
            'damage': r'deals?\s+(\d+)\s+damage\s+to\s+(.+)',
            'draw': r'draw\s+(\d+)\s+cards?',
            'create_token': r'create\s+(\d+)\s+(.+)\s+token',
            'return_from_graveyard': r'return\s+(.+)\s+from\s+graveyard',
            'discard': r'discard\s+(\d+)\s+cards?'
        }
        
        for etype, pattern in effect_types.items():
            match = re.search(pattern, effect_text, re.IGNORECASE)
            if match:
                details['type'] = etype
                details['value'] = match.group(1) if len(match.groups()) > 0 else None
                if len(match.groups()) > 1:
                    details['target'] = match.group(2)
        
        # Target parsing
        target_patterns = [
            r'target\s+(creature|player|land|artifact|enchantment)',
            r'choose\s+(\w+)\s+to\s+target'
        ]
        
        for pattern in target_patterns:
            targets = re.findall(pattern, effect_text, re.IGNORECASE)
            if targets:
                details['targets'].extend(targets)
        
        return details
    
    def _parse_class_data(self, card_data):
        """
        Enhanced parsing of Class card data, handling base level and level-up costs/abilities.
        """
        # Reset Class-related attributes
        self.is_class = False
        self.levels = []
        self.current_level = 1 # Assume starting at level 1
        self.all_abilities = []
        self.level_up_costs = {} # Store costs keyed by target level {2: cost, 3: cost}

        if not hasattr(self, 'type_line') or 'class' not in self.type_line.lower():
            return

        self.is_class = True

        try:
            oracle_text = getattr(self, 'oracle_text', '')
            if not oracle_text:
                logging.warning(f"Class card {self.name} has no oracle_text to parse.")
                return

            # Normalize text: remove reminder text, normalize whitespace
            processed_text = re.sub(r'\s*\([^()]*?\)\s*', ' ', oracle_text).strip()
            processed_text = re.sub(r'\s+', ' ', processed_text)

            # --- Parse Base Level (Level 1) ---
            # Find text before the first level-up indicator (e.g., "{COST}: Level 2")
            level_2_marker_match = re.search(r"(\{.+?\}:\s*Level\s+2)", processed_text)
            base_text = processed_text
            if level_2_marker_match:
                base_text = processed_text[:level_2_marker_match.start()].strip()

            # Split base text into abilities (handle different separators)
            base_abilities = [a.strip() for a in re.split(r'\s*\n\s*|\s*[•●]\s*', base_text) if a.strip()]
            base_level_data = {
                'level': 1,
                'cost': None, # No cost to *reach* level 1 itself
                'abilities': base_abilities,
                'power': None, # Class usually isn't creature initially
                'toughness': None,
                'type_modifications': {}
            }
            self.levels.append(base_level_data)

            # --- Parse Higher Levels ---
            # Regex to find COST: Level N followed by ability text until next marker or end
            # Pattern explanation:
            # (\{.+?\}):\s*       # Group 1: Capture the cost like {3}{U}:
            # Level\s+(\d+)       # Group 2: Capture the level number
            # \s*                  # Optional whitespace
            # ([\s\S]*?)          # Group 3: Capture the abilities text (non-greedy)
            # (?=(\{.+?\}:\s*Level|\Z)) # Lookahead: Stop before the next level marker or end of string
            higher_level_pattern = r"(\{.+?\}):\s*Level\s+(\d+)\s*([\s\S]*?)(?=(?:\{.+?\}:\s*Level\s+\d)|\Z)"
            higher_level_matches = re.finditer(higher_level_pattern, processed_text, re.IGNORECASE)

            for match in higher_level_matches:
                cost = match.group(1).strip()
                level_num = int(match.group(2))
                abilities_text = match.group(3).strip()

                # Split ability text into individual abilities
                level_abilities = [a.strip() for a in re.split(r'\s*\n\s*|\s*[•●]\s*', abilities_text) if a.strip()]

                level_data = {
                    'level': level_num,
                    'cost': cost,
                    'abilities': level_abilities,
                    'power': None,
                    'toughness': None,
                    'type_modifications': {}
                }

                # Store the cost required to *reach* this level
                self.level_up_costs[level_num] = cost

                self.levels.append(level_data)

            # Sort levels just in case they weren't in order in the text
            self.levels.sort(key=lambda x: x['level'])

            # Consolidate initial abilities for level 1
            self._consolidate_abilities()

            # Log the parsed levels for verification
            logging.debug(f"Parsed Class '{self.name}' Levels: {self.levels}")
            logging.debug(f"Level Up Costs for '{self.name}': {self.level_up_costs}")

            if not self.levels or self.levels[0]['level'] != 1:
                 logging.warning(f"Class parsing might be incomplete for {self.name}. Base level not found or levels list empty.")


        except Exception as e:
            logging.error(f"Complex Class parsing error for {self.name}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            # Ensure levels is at least an empty list on error
            if not hasattr(self, 'levels'):
                self.levels = []
    
    def _parse_level_up_cost(self, level, oracle_text):
        """
        Sophisticated level-up cost parsing with multiple cost strategies
        """
        # Mana cost extraction
        mana_pattern = rf'level\s+{level}\s*\((\{{[^}}]+}})\)'
        mana_match = re.search(mana_pattern, oracle_text, re.IGNORECASE)
        
        if mana_match:
            return mana_match.group(1)
        
        # Alternative cost parsing strategies
        alternative_patterns = [
            rf'level\s+{level}:\s*pay\s+(\d+)\s+life',
            rf'level\s+{level}:\s*sacrifice\s+(\w+)',
            rf'level\s+{level}:\s*discard\s+(\w+)'
        ]
        
        for pattern in alternative_patterns:
            alt_match = re.search(pattern, oracle_text, re.IGNORECASE)
            if alt_match:
                return alt_match.group(1)
        
        return None

    def _parse_base_level(self, oracle_text):
        """
        Parse the base (level 1) class characteristics.
        
        Handles various base level descriptions:
        - Initial abilities
        - Base type line
        - Initial cost (if any)
        """
        # Initialize base level dictionary
        base_level = {
            'level': 1,
            'cost': '',
            'abilities': [],
            'power': None,
            'toughness': None,
            'type_line': self.type_line
        }
        
        # Extract base level abilities
        # Look for text before first "Level X:" or end of text
        base_match = re.search(r'^(.*?)(?:level \d+:|$)', oracle_text, re.DOTALL)
        
        if base_match:
            base_abilities_text = base_match.group(1).strip()
            
            # Remove explanatory text in parentheses
            base_abilities_text = re.sub(r'\([^)]*\)', '', base_abilities_text)
            
            # Split abilities, handling different separation methods
            abilities = [a.strip() for a in re.split(r'\n+|[•●\-]', base_abilities_text) if a.strip()]
            base_level['abilities'] = abilities
        
        self.levels.append(base_level)

    def _parse_base_level(self, oracle_text):
        """
        Parse the base (level 1) class characteristics.
        
        Handles various base level descriptions:
        - Initial abilities
        - Base type line
        - Initial cost (if any)
        """
        # Initialize base level dictionary
        base_level = {
            'level': 1,
            'cost': '',
            'abilities': [],
            'power': None,
            'toughness': None,
            'type_line': self.type_line
        }
        
        # Extract base level abilities
        # Look for text before first "Level X:" or end of text
        base_match = re.search(r'^(.*?)(?:level \d+:|$)', oracle_text, re.DOTALL)
        
        if base_match:
            base_abilities_text = base_match.group(1).strip()
            
            # Remove explanatory text in parentheses
            base_abilities_text = re.sub(r'\([^)]*\)', '', base_abilities_text)
            
            # Split abilities, handling different separation methods
            abilities = [a.strip() for a in re.split(r'\n+|[•●\-]', base_abilities_text) if a.strip()]
            base_level['abilities'] = abilities
        
        self.levels.append(base_level)

    def _parse_higher_levels(self, oracle_text):
        """
        Parse higher levels of the Class card.
        
        Handles:
        - Multiple level progressions
        - Level-up costs
        - Ability and type changes
        - Power/toughness transformations
        """
        # Find all level descriptions
        level_matches = re.findall(
            r'level\s+(\d+):\s*([^\n]*)(.*?)(?=level \d+:|$)', 
            oracle_text, 
            re.IGNORECASE | re.DOTALL
        )
        
        for level_num, cost_text, abilities_text in level_matches:
            level_data = {
                'level': int(level_num),
                'cost': cost_text.strip(),
                'abilities': [],
                'power': None,
                'toughness': None,
                'type_line': self.type_line  # Default to base type line
            }
            
            # Extract level-up cost from the cost text
            # For Classes, this is typically "{X}{Y}: Level N" format
            
            # Parse abilities
            # Remove marker text "//Level_N//"
            abilities_text = re.sub(r'//Level_\d+//', '', abilities_text)
            
            # Split by paragraphs or line breaks, filter out empty strings
            abilities = [
                a.strip() for a in re.split(r'\n+|[•●\-]', abilities_text.strip()) 
                if a.strip()
            ]
            level_data['abilities'] = abilities
            
            # Check for creature transformation
            creature_match = re.search(
                r'becomes?\s+a\s+(\d+)/(\d+)\s*(.*?)\s*creature', 
                abilities_text, 
                re.IGNORECASE
            )
            
            if creature_match:
                # Parse power, toughness, and potential new type
                level_data['power'] = int(creature_match.group(1))
                level_data['toughness'] = int(creature_match.group(2))
                
                # Extract potential new creature type
                new_type = creature_match.group(3).strip()
                if new_type:
                    level_data['type_line'] = f"Creature — {new_type}"
            
            # Add to levels list
            self.levels.append(level_data)

    def _consolidate_abilities(self):
        """
        Consolidate abilities across levels.
        
        Tracks:
        - Cumulative abilities
        - Current level abilities
        """
        # Reset all abilities
        self.all_abilities = []
        
        # Collect abilities up to current level
        for level_data in sorted(self.levels, key=lambda x: x['level']):
            if level_data['level'] <= self.current_level:
                self.all_abilities.extend(level_data['abilities'])

    def get_level_cost(self, level):
        """
        Get the mana cost to activate the ability to reach a specific level.

        Args:
            level (int): Target level to reach (e.g., 2 or 3)

        Returns:
            str: Mana cost string (e.g., "{3}{U}") or None if not found.
        """
        if not self.is_class:
            return None

        # Use the pre-parsed level_up_costs dictionary
        cost = self.level_up_costs.get(level)

        if cost:
            return cost
        else:
            logging.warning(f"Could not find level-up cost for level {level} of Class {self.name}.")
            return None

    def can_level_up(self):
        """
        Check if this Class can level up further.
        
        Returns:
            bool: Whether there are higher levels available
        """
        if not self.is_class:
            return False
        
        # Check if there's a next level available
        return any(level_data['level'] > self.current_level for level_data in self.levels)

    def level_up(self):
        """
        Attempt to level up the Class.
        
        Returns:
            bool: Whether leveling up was successful
        """
        if not self.can_level_up():
            return False
        
        # Find the next available level
        next_levels = [
            level_data for level_data in self.levels 
            if level_data['level'] > self.current_level
        ]
        
        # Sort to get the immediate next level
        next_levels.sort(key=lambda x: x['level'])
        next_level = next_levels[0]
        
        # Update current level
        self.current_level = next_level['level']
        
        # Recompute abilities
        self._consolidate_abilities()
        
        return True

    def get_current_class_data(self):
        """
        Get the consolidated data for the Class at its current level.
        Includes abilities from all levels up to and including the current one.

        Returns:
            dict or None: Comprehensive data for the current class state, or None if invalid.
        """
        if not self.is_class or not hasattr(self, 'levels') or not self.levels:
            logging.warning(f"Attempted to get class data for non-class card or card with unparsed levels: {self.name}")
            # Check if levels might be empty *after* parsing attempt due to error
            if not hasattr(self, 'levels'): self.levels = [] # Ensure it's a list
            if not self.levels:
                # Attempt re-parse as a fallback? Could be risky.
                # Log error and return None for now.
                logging.error(f"Cannot get class data for {self.name}: 'levels' list is empty after parsing.")
                return None
            # If list exists but somehow no level 1 (parsing error), also return None
            if not any(lvl.get('level') == 1 for lvl in self.levels):
                logging.error(f"Cannot get class data for {self.name}: Level 1 data missing.")
                return None
            # If code reaches here, self.levels exists but self.is_class might be false? Should be caught earlier.

        # Find data for the exact current level
        current_level_data = None
        for level_data in self.levels:
            if isinstance(level_data, dict) and level_data.get('level') == self.current_level:
                current_level_data = level_data
                break

        if current_level_data is None:
             # Fallback: Find the highest level achieved <= current level (Handles cases where current_level might be invalid temporarily)
             valid_levels = [lvl for lvl in self.levels if isinstance(lvl, dict) and lvl.get('level') is not None and lvl['level'] <= self.current_level]
             if valid_levels:
                 current_level_data = max(valid_levels, key=lambda x: x.get('level', 0))
                 logging.warning(f"Could not find exact data for Level {self.current_level} of {self.name}. Using highest valid level found: {current_level_data.get('level')}")
             else:
                  # Severe issue: No levels <= current_level found. Use Level 1 as absolute fallback.
                  level_1_data = next((lvl for lvl in self.levels if isinstance(lvl, dict) and lvl.get('level') == 1), None)
                  if level_1_data:
                       logging.error(f"No valid level data found at or below current level {self.current_level} for {self.name}. Defaulting to Level 1.")
                       current_level_data = level_1_data
                  else:
                       logging.error(f"CRITICAL: Cannot find ANY level data (not even Level 1) for {self.name}.")
                       return None # Cannot proceed

        # --- Consolidate data ---
        # Combine abilities from all levels up to current level
        consolidated_data = {
            'level': self.current_level,
            'all_abilities': [],
            'current_level_abilities': current_level_data.get('abilities', []), # Abilities specifically from this level
            # Include other relevant fields from the current level data
            'power': current_level_data.get('power'),
            'toughness': current_level_data.get('toughness'),
            'type_modifications': current_level_data.get('type_modifications', {})
        }

        # Add abilities from previous levels
        for level_data in self.levels:
            if isinstance(level_data, dict) and level_data.get('level') is not None and level_data['level'] <= self.current_level:
                consolidated_data['all_abilities'].extend(level_data.get('abilities', []))

        # Store the consolidated abilities on the instance if needed elsewhere quickly
        self.all_abilities = consolidated_data['all_abilities']

        return consolidated_data

    #
    # Room card handling methods
    #
    def _parse_room_data(self, card_data):
        """
        Advanced Room card parsing with comprehensive door handling
        """
        # Reset Room attributes with more detailed structure
        self.is_room = False
        self.doors = []
        
        if not hasattr(self, 'type_line') or 'room' not in self.type_line.lower():
            return

        self.is_room = True

        try:
            # Handle double-faced rooms
            if hasattr(self, 'card_faces') and len(self.card_faces) == 2:
                for face in self.card_faces:
                    door = self._parse_single_door(face)
                    if door:
                        self.doors.append(door)
            else:
                # Single-faced room parsing
                door = self._parse_single_door(self)
                if door:
                    self.doors.append(door)
        
        except Exception as e:
            logging.error(f"Advanced Room parsing error for {self.name}: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            
    def _parse_single_door(self, door_source):
        """
        Comprehensive single door parsing with advanced trigger and effect detection.
        Handles both Card objects and dictionary sources.
        """
        # Determine how to access data based on source type
        if isinstance(door_source, Card):
            get_attr = lambda name, default: getattr(door_source, name, default)
        elif isinstance(door_source, dict):
            get_attr = lambda name, default: door_source.get(name, default)
        else:
            logging.error(f"Unsupported door_source type in _parse_single_door: {type(door_source)}")
            return None # Cannot parse

        door_data = {
            'name': get_attr('name', 'Unnamed Door'), # Use helper
            'oracle_text': get_attr('oracle_text', '').lower(), # Use helper
            'triggers': [],
            'effects': [],
            'unlock_conditions': [],
            'mana_cost': get_attr('mana_cost', '') # Use helper
        }

        # Trigger parsing with more comprehensive patterns
        trigger_patterns = [
            r'when\s+(.*?),\s*(.*)',  # General when triggers
            r'whenever\s+(.*?),\s*(.*)',  # Whenever triggers
            r'at\s+the\s+beginning\s+of\s+(.*?),\s*(.*)'  # Phase-based triggers
        ]

        for pattern in trigger_patterns:
            triggers = re.findall(pattern, door_data['oracle_text'])
            for condition, effect in triggers:
                door_data['triggers'].append({
                    'type': 'conditional',
                    'condition': condition.strip(),
                    'effect': effect.strip()
                })

        # Effect parsing with advanced detection
        effect_patterns = [
            (r'create\s+(\d+)\s+(.+)\s+token', 'token_creation'),
            (r'return\s+(.+)\s+from\s+graveyard', 'graveyard_return'),
            (r'deals?\s+(\d+)\s+damage\s+to\s+(.+)', 'damage'),
            (r'draw\s+(\d+)\s+cards?', 'card_draw'),
            (r'surveil\s+(\d+)', 'surveil')
        ]

        for pattern, effect_type in effect_patterns:
            effects = re.findall(pattern, door_data['oracle_text'], re.IGNORECASE)
            for match in effects:
                door_data['effects'].append({
                    'type': effect_type,
                    # Ensure match is correctly structured (often a tuple or list)
                    'details': match if isinstance(match, (tuple, list)) else (match,)
                })

        # Unlock conditions parsing
        door_data['unlock_conditions'] = self._parse_door_unlock_conditions(door_data['oracle_text'])

        return door_data

    def _parse_door_unlock_conditions(self, oracle_text):
        """
        Sophisticated door unlock condition parsing
        """
        conditions = []
        
        unlock_patterns = [
            r'unlock\s+(?:only\s+)?(?:if|when)\s+(.*?)(?:\.|$)',
            r'door\s+unlocks\s+(?:only\s+)?(?:if|when)\s+(.*?)(?:\.|$)'
        ]
        
        for pattern in unlock_patterns:
            matches = re.findall(pattern, oracle_text, re.IGNORECASE)
            conditions.extend([condition.strip() for condition in matches])
        
        return conditions

    def _parse_door_triggers(self, oracle_text):
        """
        Parse trigger conditions for a door.
        
        Handles various trigger formats:
        - When you unlock this door
        - Whenever a creature dies
        - At the beginning of your upkeep
        """
        triggers = []
        
        # Trigger patterns
        trigger_patterns = [
            # When you unlock this door
            (r'when\s+you\s+unlock\s+this\s+door', 'unlock_trigger'),
            
            # Whenever patterns
            (r'whenever\s+(.*?),\s*(.*)', 'conditional_trigger'),
            
            # At the beginning of patterns
            (r'at\s+the\s+beginning\s+of\s+(.*?),\s*(.*)', 'phase_trigger')
        ]
        
        for pattern, trigger_type in trigger_patterns:
            match = re.search(pattern, oracle_text.lower(), re.IGNORECASE)
            if match:
                trigger_details = {
                    'type': trigger_type,
                    'condition': match.group(1) if len(match.groups()) > 0 else None,
                    'effect': match.group(2) if len(match.groups()) > 1 else None
                }
                triggers.append(trigger_details)
        
        return triggers

    def _parse_door_static_abilities(self, oracle_text):
        """
        Parse static abilities for a door.
        
        Handles various static ability formats:
        - Lands you control have additional abilities
        - You have no maximum hand size
        """
        static_abilities = []
        
        # Static ability patterns
        static_patterns = [
            # Lands have additional abilities
            (r'lands?\s+you\s+control\s+have\s+(.*)', 'land_ability'),
            
            # Player-wide static effects
            (r'you\s+have\s+no\s+maximum\s+hand\s+size', 'hand_size'),
            
            # Permanent-wide effects
            (r'(.*?)\s+you\s+control\s+have\s+(.*)', 'permanent_ability')
        ]
        
        for pattern, ability_type in static_patterns:
            match = re.search(pattern, oracle_text.lower(), re.IGNORECASE)
            if match:
                ability_details = {
                    'type': ability_type,
                    'description': match.group(0),
                    'scope': match.group(1) if len(match.groups()) > 0 else None,
                    'effect': match.group(2) if len(match.groups()) > 1 else None
                }
                static_abilities.append(ability_details)
        
        return static_abilities

    def _parse_door_effects(self, oracle_text):
        """
        Parse effects for a door.
        
        Handles various effect formats:
        - Create tokens
        - Return cards from graveyard
        - Deal damage
        """
        effects = []
        
        # Effect patterns
        effect_patterns = [
            # Token creation
            (r'create\s+(.*?)\s+token', 'token_creation'),
            
            # Graveyard interactions
            (r'return\s+(.*?)\s+from\s+your\s+graveyard', 'graveyard_return'),
            
            # Damage dealing
            (r'deals?\s+(\d+)\s+damage\s+to\s+(.*)', 'damage_effect'),
            
            # Drawing/discarding
            (r'(draw|discard)\s+(\d+)\s+cards?', 'card_manipulation'),
            
            # Surveil
            (r'surveil\s+(\d+)', 'surveil'),
            
            # Manifest
            (r'manifest\s+(.*)', 'manifest')
        ]
        
        for pattern, effect_type in effect_patterns:
            matches = re.findall(pattern, oracle_text.lower(), re.IGNORECASE)
            for match in matches:
                effect_details = {
                    'type': effect_type,
                    'details': match
                }
                effects.append(effect_details)
        
        return effects


    def get_current_room_data(self):
        """
        Get the combined data for currently unlocked doors in a Room.
        
        Returns a dictionary with merged information from unlocked doors.
        """
        if not self.is_room:
            return None
        
        # Start with empty data
        combined_data = {
            'name': self.name if hasattr(self, 'name') else "",
            'type_line': self.type_line if hasattr(self, 'type_line') else "",
            'oracle_text': "",
            'unlocked_doors': [],
            'triggers': [],
            'static_abilities': [],
            'effects': []
        }
        
        # Combine data from unlocked doors
        for door_name, door in [('Door 1', self.door1), ('Door 2', self.door2)]:
            if door.get('unlocked', False):
                combined_data['unlocked_doors'].append(door_name)
                combined_data['oracle_text'] += door.get('oracle_text', "") + "\n\n"
                combined_data['triggers'].extend(door.get('triggers', []))
                combined_data['static_abilities'].extend(door.get('static_abilities', []))
                combined_data['effects'].extend(door.get('effects', []))
        
        return combined_data

    def unlock_door(self, door_number, game_state=None, controller=None):
        """
        Unlock a specific door of the Room.
        
        Args:
            door_number (int): 1 or 2 representing the door to unlock
            game_state: Optional game state for processing triggers
            controller: Optional controller for processing triggers
        
        Returns:
            bool: Whether the door was successfully unlocked
        """
        if not self.is_room:
            return False
        
        if door_number not in [1, 2]:
            logging.warning(f"Invalid door number {door_number} for Room {self.name}")
            return False
        
        door = self.door1 if door_number == 1 else self.door2
        
        # Check if door is already unlocked
        if door.get('unlocked', False):
            logging.info(f"Door {door_number} of {self.name} is already unlocked")
            return False
        
        # Unlock the door
        door['unlocked'] = True
        
        # Trigger unlock effects if game state is provided
        if game_state and controller:
            try:
                for trigger in door.get('triggers', []):
                    if trigger['type'] == 'unlock_trigger':
                        logging.info(f"Processing unlock trigger for {self.name}'s Door {door_number}")
                        
                        # Create a trigger event
                        event_data = {
                            'door_number': door_number,
                            'door_name': door.get('name'),
                            'source_id': self.card_id,
                            'controller': controller
                        }
                        
                        # Process door unlock effects
                        for effect in door.get('effects', []):
                            self._process_door_effect(effect, game_state, controller)
                        
                        # Trigger any "when you unlock a door" abilities in play
                        if hasattr(game_state, 'trigger_ability'):
                            game_state.trigger_ability(self.card_id, "DOOR_UNLOCKED", event_data)
                            
                        # Check for unlock rewards or counters
                        if hasattr(self, 'venture_value'):
                            # Room unlocking acts as venture progress
                            game_state.venture_dungeon(controller, self.venture_value)
                            logging.debug(f"Room door unlock counted as venture progress")
            except Exception as e:
                logging.error(f"Error processing unlock triggers for {self.name}: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
        
        return True

    def _process_door_effect(self, effect, game_state, controller):
        """
        Process a door effect when unlocked by creating events or triggering abilities.
        """
        effect_type = effect.get('type')
        details = effect.get('details', '')

        if not hasattr(game_state, 'ability_handler') or not game_state.ability_handler:
             logging.warning("Cannot process door effect: AbilityHandler not found in GameState.")
             return

        # Context for the effect application
        context = {'source_id': self.card_id, 'controller': controller, 'effect_origin': 'door_unlock'}

        try:
            # Convert text descriptions to AbilityEffect objects or direct GameState actions
            if effect_type == 'token_creation':
                 # Assuming details = (count_str, type_desc)
                 token_details = self._parse_token_details(details[1]) # Use helper to parse description
                 token_data = { # Convert parsed details to data format for GS
                      "name": token_details.get('name', "Room Token"),
                      "type_line": token_details.get('type_line', "Token Creature"),
                      "power": token_details.get('power', 1),
                      "toughness": token_details.get('toughness', 1),
                      "colors": token_details.get('colors', [0, 0, 0, 0, 0]),
                      "subtypes": token_details.get('subtypes', []),
                      "abilities": token_details.get('abilities', [])
                 }
                 token_count = token_details.get('count', 1)
                 for _ in range(token_count):
                      game_state.create_token(controller, token_data) # Call GS method

            elif effect_type == 'card_manipulation':
                 # Assuming details = (action_str, count_str)
                 action, count = details
                 count = int(count) if isinstance(count, str) and count.isdigit() else 1
                 if action == 'draw': game_state.draw_cards(controller, count) # Call GS method
                 # Add discard logic if needed, possibly via effect

            elif effect_type == 'damage_effect':
                 # Assuming details = (amount_str, target_desc)
                 damage_amount, target_desc = details
                 damage_amount = int(damage_amount) if isinstance(damage_amount, str) and damage_amount.isdigit() else 1
                 # Create and apply DamageEffect via AbilityHandler
                 effect_text = f"deals {damage_amount} damage to {target_desc}" # Reconstruct text for parser
                 # Need to resolve targets first if text requires it
                 targets = None
                 if "target" in target_desc:
                     targets = game_state.targeting_system.resolve_targeting(self.card_id, controller, effect_text)
                 effects = EffectFactory.create_effects(effect_text, targets=targets)
                 for eff in effects: eff.apply(game_state, self.card_id, controller, targets)

            elif effect_type == 'graveyard_return':
                 # Assuming details = target_desc
                 target_desc = details
                 effect_text = f"return {target_desc} from your graveyard" # Reconstruct
                 targets = None # Search handled by effect itself? Or target selection first?
                 # For now, assume effect handles search
                 effects = EffectFactory.create_effects(effect_text)
                 for eff in effects: eff.apply(game_state, self.card_id, controller, targets)

            elif effect_type == 'surveil':
                 # Assuming details = count_str
                 count = int(details) if isinstance(details, str) and details.isdigit() else 1
                 game_state.surveil(controller, count) # Call GS method

            elif effect_type == 'manifest':
                 # Assuming details = ? (Manifest logic complex)
                 game_state.manifest_card(controller) # Call GS method (needs count if specified)

            # Add other effect types here, calling appropriate GameState methods or creating AbilityEffect objects

            logging.debug(f"Processed door effect: {effect_type}")

        except Exception as e:
            logging.error(f"Error processing door effect '{effect_type}': {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    
    def _parse_token_details(self, token_desc):
        """Parse token creation details from effect text."""
        token_data = {
            "name": "Room Token",
            "type_line": "Token Creature",
            "power": 1,
            "toughness": 1,
            "count": 1,
            "colors": [0, 0, 0, 0, 0],  # [W, U, B, R, G]
            "subtypes": [],
            "abilities": []
        }
        
        # Parse token count
        import re
        count_match = re.search(r'create (\w+|\d+)', token_desc.lower())
        if count_match:
            count_text = count_match.group(1)
            if count_text.isdigit():
                token_data["count"] = int(count_text)
            elif count_text == "a" or count_text == "an":
                token_data["count"] = 1
            elif count_text == "two":
                token_data["count"] = 2
            elif count_text == "three":
                token_data["count"] = 3
        
        # Parse power/toughness
        size_match = re.search(r'(\d+)/(\d+)', token_desc)
        if size_match:
            token_data["power"] = int(size_match.group(1))
            token_data["toughness"] = int(size_match.group(2))
        
        # Parse colors
        colors = {'white': 0, 'blue': 1, 'black': 2, 'red': 3, 'green': 4}
        for color, index in colors.items():
            if color in token_desc.lower():
                token_data["colors"][index] = 1
        
        # Parse token name and type
        token_type_match = re.search(r'(white|blue|black|red|green)?\s*([^\d/]+)(?:\s+(\d+)/(\d+))?', token_desc.lower())
        if token_type_match:
            token_type = token_type_match.group(2).strip()
            if token_type:
                subtypes = [s.strip().capitalize() for s in token_type.split() if s.strip()]
                if subtypes:
                    token_data["name"] = " ".join(subtypes) + " Token"
                    token_data["subtypes"] = subtypes
                    token_data["type_line"] = "Token Creature — " + " ".join(subtypes)
        
        # Parse abilities
        ability_match = re.search(r'with (.+)', token_desc.lower())
        if ability_match:
            abilities_text = ability_match.group(1)
            token_data["abilities"] = [abilities_text]
        
        return 
    
    def _parse_target_types(self, description):
        """Parse target types from effect description."""
        types = []
        
        if "creature" in description.lower():
            types.append("creature")
        if "artifact" in description.lower():
            types.append("artifact")
        if "enchantment" in description.lower():
            types.append("enchantment")
        if "land" in description.lower():
            types.append("land")
        if "permanent" in description.lower():
            types.extend(["creature", "artifact", "enchantment", "land", "planeswalker"])
        if "card" in description.lower() and not types:
            types.append("any")
        
        return types if types else ["any"]
    
    
    def _card_matches_type(self, card, type_filter):
        """Check if a card matches a specific type filter."""
        if type_filter == "any":
            return True
        
        if not hasattr(card, 'card_types'):
            return False
        
        if type_filter in card.card_types:
            return True
        
        if type_filter == "permanent" and any(t in card.card_types for t in ["creature", "artifact", "enchantment", "land", "planeswalker"]):
            return True
        
        return False


    def _parse_return_count(self, description, eligible_cards):
        """Parse the number of cards to return from graveyard."""
        count = 1  # Default
        
        # Check for specific count words
        if "up to one" in description.lower():
            count = min(1, len(eligible_cards))
        elif "up to two" in description.lower():
            count = min(2, len(eligible_cards))
        elif "up to three" in description.lower():
            count = min(3, len(eligible_cards))
        elif "all" in description.lower():
            count = len(eligible_cards)
        
        # Check for specific number
        import re
        count_match = re.search(r'return (\w+|\d+)', description.lower())
        if count_match:
            count_text = count_match.group(1)
            if count_text.isdigit():
                count = min(int(count_text), len(eligible_cards))
            elif count_text == "a" or count_text == "an" or count_text == "target":
                count = min(1, len(eligible_cards))
            elif count_text == "two":
                count = min(2, len(eligible_cards))
            elif count_text == "three":
                count = min(3, len(eligible_cards))
        
        return count

    def _parse_return_zone(self, description):
        """Parse the zone to return cards to from graveyard."""
        description = description.lower()
        
        if "to your hand" in description:
            return "hand"
        elif "to the battlefield" in description:
            return "battlefield"
        elif "to the top of your library" in description:
            return "library_top"
        elif "to the bottom of your library" in description:
            return "library_bottom"
        elif "to your library" in description:
            return "library_top"  # Default to top if not specified
        
        # Default to hand if not specified
        return "hand"

    def _parse_manifest_count(self, details):
        """Parse the number of cards to manifest."""
        # Default to 1
        count = 1
        
        if isinstance(details, str):
            if "manifest dread" in details.lower():
                # Special case for "manifest dread"
                return 1
            
            # Look for numbers
            import re
            match = re.search(r'manifest (\d+|a|an|one|two|three)', details.lower())
            if match:
                value = match.group(1)
                if value.isdigit():
                    count = int(value)
                elif value in ["a", "an", "one"]:
                    count = 1
                elif value == "two":
                    count = 2
                elif value == "three":
                    count = 3
        
        return count

    def _process_door_effect(self, effect, game_state, controller):
        """
        Process a door effect when unlocked with comprehensive handling of all effect types.
        
        Args:
            effect: The effect to process
            game_state: The game state
            controller: The controller of the door
        """
        effect_type = effect.get('type')
        details = effect.get('details', '')
        
        # Handle different effect types
        if effect_type == 'token_creation':
            # Enhanced token creation with specific token types
            if isinstance(details, str):
                token_details = self._parse_token_details(details)
                if hasattr(game_state, 'create_token'):
                    token_data = {
                        "name": token_details.get('name', "Room Token"),
                        "type_line": token_details.get('type_line', "Token Creature"),
                        "power": token_details.get('power', 1),
                        "toughness": token_details.get('toughness', 1),
                        "colors": token_details.get('colors', [0, 0, 0, 0, 0]),
                        "subtypes": token_details.get('subtypes', []),
                        "abilities": token_details.get('abilities', [])
                    }
                    # Support for multiple tokens
                    token_count = token_details.get('count', 1)
                    for _ in range(token_count):
                        token_id = game_state.create_token(controller, token_data)
                        logging.debug(f"Created token from door effect: {token_data['name']} ({token_id})")
                    
                    # Apply any triggers for token creation
                    if hasattr(game_state, 'trigger_ability'):
                        game_state.trigger_ability(self.card_id, "TOKEN_CREATED", {
                            "token_type": token_data["name"],
                            "count": token_count,
                            "controller": controller
                        })
            
        elif effect_type == 'graveyard_return':
            # Enhanced graveyard interaction with player choice
            if isinstance(details, str):
                # Parse target types
                target_types = self._parse_target_types(details)
                eligible_cards = []
                
                # Identify eligible cards in graveyard
                for card_id in controller['graveyard']:
                    card = game_state._safe_get_card(card_id)
                    if not card:
                        continue
                    
                    # Check if card matches any target type
                    for target_type in target_types:
                        if self._card_matches_type(card, target_type):
                            eligible_cards.append(card_id)
                            break
                
                if eligible_cards:
                    # Determine number of cards to return
                    return_count = self._parse_return_count(details, eligible_cards)
                    
                    # Implement player choice if available
                    if hasattr(game_state, 'choose_cards_from_list'):
                        cards_to_return = game_state.choose_cards_from_list(
                            controller, eligible_cards, return_count, 
                            f"Choose up to {return_count} card(s) to return from graveyard"
                        )
                    else:
                        # Fallback: Choose the first N eligible cards
                        cards_to_return = eligible_cards[:return_count]
                    
                    # Process the return
                    zone = self._parse_return_zone(details)
                    for card_id in cards_to_return:
                        controller['graveyard'].remove(card_id)
                        if zone == "hand":
                            controller['hand'].append(card_id)
                            card = game_state._safe_get_card(card_id)
                            logging.debug(f"Returned {card.name if card else 'a card'} from graveyard to hand")
                        elif zone == "battlefield":
                            controller['battlefield'].append(card_id)
                            controller['entered_battlefield_this_turn'].add(card_id)
                            card = game_state._safe_get_card(card_id)
                            logging.debug(f"Returned {card.name if card else 'a card'} from graveyard to battlefield")
                        elif zone == "library_top":
                            controller['library'].insert(0, card_id)
                            card = game_state._safe_get_card(card_id)
                            logging.debug(f"Returned {card.name if card else 'a card'} from graveyard to top of library")
                        elif zone == "library_bottom":
                            controller['library'].append(card_id)
                            card = game_state._safe_get_card(card_id)
                            logging.debug(f"Returned {card.name if card else 'a card'} from graveyard to bottom of library")
                        
                        # Trigger graveyard leave event
                        if hasattr(game_state, 'trigger_ability'):
                            game_state.trigger_ability(card_id, "LEAVE_GRAVEYARD", {
                                "from_zone": "graveyard",
                                "to_zone": zone,
                                "controller": controller
                            })
        
        elif effect_type == 'damage_effect':
            # Enhanced damage effect with multiple targets and player choices
            if isinstance(details, tuple) and len(details) >= 2:
                amount = int(details[0]) if details[0].isdigit() else 1
                target_desc = details[1]
                
                # Parse targets (player, creature, any target)
                if "each opponent" in target_desc:
                    # Deal damage to all opponents
                    opponents = game_state.get_opponents(controller)
                    for opponent in opponents:
                        opponent['life'] -= amount
                    logging.debug(f"Door effect: dealt {amount} damage to each opponent")
                elif "creature" in target_desc:
                    # Deal damage to a creature
                    opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
                    
                    if "you control" in target_desc:
                        # Own creature
                        valid_targets = controller["battlefield"]
                        target_controller = controller
                    elif "opponent" in target_desc:
                        # Opponent's creature
                        valid_targets = opponent["battlefield"]
                        target_controller = opponent
                    else:
                        # Any creature
                        valid_targets = controller["battlefield"] + opponent["battlefield"]
                        target_controller = None
                    
                    # Find valid creature targets
                    creature_targets = []
                    for target_id in valid_targets:
                        card = game_state._safe_get_card(target_id)
                        if card and hasattr(card, 'card_types') and 'creature' in card.card_types:
                            creature_targets.append(target_id)
                    
                    if creature_targets:
                        # Player choice if available
                        if hasattr(game_state, 'choose_card_from_list'):
                            target_id = game_state.choose_card_from_list(
                                controller, creature_targets, 
                                f"Choose a creature to deal {amount} damage to"
                            )
                        else:
                            # Fallback: first creature
                            target_id = creature_targets[0]
                        
                        # Deal damage
                        if target_id:
                            target_card = game_state._safe_get_card(target_id)
                            target_toughness = target_card.toughness if hasattr(target_card, 'toughness') else 0
                            
                            # Handle damage and possible destruction
                            if amount >= target_toughness:
                                # Destroy creature
                                if target_controller:
                                    target_controller["battlefield"].remove(target_id)
                                    target_controller["graveyard"].append(target_id)
                                    logging.debug(f"Door effect: destroyed {target_card.name if target_card else 'a creature'} with {amount} damage")
                            else:
                                # Just deal damage
                                logging.debug(f"Door effect: dealt {amount} damage to {target_card.name if target_card else 'a creature'}")
                else:
                    # Default: damage opponent
                    opponent = game_state.p2 if controller == game_state.p1 else game_state.p1
                    opponent['life'] -= amount
                    logging.debug(f"Door effect: dealt {amount} damage to opponent")
        
        elif effect_type == 'card_manipulation':
            # Enhanced card manipulation with comprehensive draw/discard handling
            if isinstance(details, tuple) and len(details) >= 2:
                action = details[0]  # 'draw' or 'discard'
                count = int(details[1]) if details[1].isdigit() else 1
                
                if action == 'draw':
                    # Draw cards using game_state's draw method
                    if hasattr(game_state, 'draw_cards'):
                        game_state.draw_cards(controller, count)
                    else:
                        # Fallback to manual draw
                        for _ in range(count):
                            if controller['library']:
                                card_id = controller['library'].pop(0)
                                controller['hand'].append(card_id)
                    logging.debug(f"Door effect: drew {count} cards")
                    
                elif action == 'discard':
                    # Discard with player choice
                    if len(controller['hand']) <= count:
                        # Discard entire hand
                        discarded = controller['hand'].copy()
                        controller['hand'].clear()
                        controller['graveyard'].extend(discarded)
                        logging.debug(f"Door effect: discarded entire hand ({len(discarded)} cards)")
                    else:
                        # Player chooses what to discard
                        if hasattr(game_state, 'choose_cards_from_list'):
                            cards_to_discard = game_state.choose_cards_from_list(
                                controller, controller['hand'], count, 
                                f"Choose {count} card(s) to discard"
                            )
                        else:
                            # Fallback: first N cards
                            cards_to_discard = controller['hand'][:count]
                        
                        # Process discard
                        for card_id in cards_to_discard:
                            controller['hand'].remove(card_id)
                            controller['graveyard'].append(card_id)
                            
                            # Trigger discard event
                            if hasattr(game_state, 'trigger_ability'):
                                game_state.trigger_ability(card_id, "DISCARD", {
                                    "controller": controller
                                })
                        
                        logging.debug(f"Door effect: discarded {len(cards_to_discard)} cards")
            
        elif effect_type == 'surveil':
            # Enhanced surveil with player choice
            details_value = details
            if isinstance(details, tuple) and len(details) > 0:
                details_value = details[0]
            count = int(details_value) if isinstance(details_value, str) and details_value.isdigit() else 1
            
            if controller['library'] and count > 0:
                # Look at top N cards
                look_at = []
                for _ in range(min(count, len(controller['library']))):
                    look_at.append(controller['library'].pop(0))
                
                # Player chooses which go to graveyard vs top of library
                to_graveyard = []
                to_library = look_at
                
                if hasattr(game_state, 'choose_cards_from_list'):
                    to_graveyard = game_state.choose_cards_from_list(
                        controller, look_at, count, 
                        f"Choose cards to put in your graveyard (Surveil {count})"
                    )
                    to_library = [card_id for card_id in look_at if card_id not in to_graveyard]
                
                # Put cards in graveyard
                controller['graveyard'].extend(to_graveyard)
                
                # Put remaining cards on top of library in chosen order
                if hasattr(game_state, 'choose_card_order'):
                    to_library = game_state.choose_card_order(
                        controller, to_library, 
                        "Choose order to put cards on top of your library (top card first)"
                    )
                
                # Update library
                controller['library'] = to_library + controller['library']
                
                logging.debug(f"Door effect: surveilled {count} cards, put {len(to_graveyard)} in graveyard")
                
                # Trigger surveil event
                if hasattr(game_state, 'trigger_ability'):
                    game_state.trigger_ability(self.card_id, "SURVEIL", {
                        "controller": controller,
                        "count": count,
                        "to_graveyard_count": len(to_graveyard)
                    })
        
        elif effect_type == 'manifest':
            # Implement manifest
            if hasattr(game_state, 'manifest_card'):
                manifest_count = self._parse_manifest_count(details)
                for _ in range(manifest_count):
                    game_state.manifest_card(controller)
                logging.debug(f"Door effect: manifested {manifest_count} card(s)")

    #
    # Planeswalker handling methods
    #
    def _init_planeswalker(self, card_data):
        """Initialize planeswalker-specific attributes with improved loyalty ability parsing"""
        self.loyalty = self._safe_int(card_data.get("loyalty", "0"))
        self.loyalty_abilities = []
        
        # Parse oracle text for loyalty abilities
        if self.oracle_text:
            ability_pattern = r'([+\-]?[0-9]+):\s+(.*?)(?=(?:[+\-]?[0-9]+:|$))'
            matches = re.findall(ability_pattern, self.oracle_text, re.DOTALL)
            for cost, effect in matches:
                self.loyalty_abilities.append({
                    "cost": int(cost),
                    "effect": effect.strip(),
                    "is_ultimate": int(cost) < -2  # Typical threshold for ultimate abilities
                })
                
        # If no abilities were found but this is a planeswalker, try alternative parsing
        if not self.loyalty_abilities and hasattr(self, 'card_types') and 'planeswalker' in self.card_types:
            lines = self.oracle_text.split('\n') if self.oracle_text else []
            for line in lines:
                match = re.match(r'^([+\-]?[0-9]+):\s+(.+)$', line.strip())
                if match:
                    cost, effect = match.groups()
                    self.loyalty_abilities.append({
                        "cost": int(cost),
                        "effect": effect.strip(),
                        "is_ultimate": int(cost) < -2
                    })
    #
    # Utility methods for card properties and features
    #
    def _safe_int(self, value):
        """Handle non-numeric power/toughness values, returning None and logging a warning."""
        try:
            # Handle '*' or similar symbolic values explicitly if needed
            if isinstance(value, str) and not value.isdigit() and value not in ['*', 'X', '?']: # Add more symbols if needed
                logging.warning(f"Non-numeric power/toughness value encountered: '{value}'. Returning None.")
                return None
            elif isinstance(value, str) and value in ['*', 'X', '?']:
                # Represent symbolic values as None or a special number like -1? Let's use None.
                return None
            return int(value)
        except (ValueError, TypeError):
            # Log as warning, not error, as it might be expected (like '*')
            logging.warning(f"Could not convert power/toughness value '{value}' to int. Returning None.")
            return None

    def transform(self):
        """
        Toggle the card's face if it is double-faced.
        This updates the card's key attributes to reflect the alternate face.
        """
        if not self.faces:
            # This card is not double-faced; nothing to do.
            return
        # Toggle face (assumes two faces only)
        self.current_face = 1 - self.current_face
        self.is_transformed = (self.current_face == 1)
        # Get new face data
        new_face = self.faces[self.current_face]
        # Update attributes – use defaults to retain any missing info from the current state
        self.name = new_face.get("name", self.name)
        self.mana_cost = new_face.get("mana_cost", self.mana_cost)
        self.type_line = new_face.get("type_line", self.type_line).lower()
        self.oracle_text = new_face.get("oracle_text", self.oracle_text)
        if "cmc" in new_face:
            self.cmc = new_face["cmc"]
        if "power" in new_face:
            self.power = self._safe_int(new_face["power"])
        if "toughness" in new_face:
            self.toughness = self._safe_int(new_face["toughness"])
        # Update keywords, colors, and other attributes as needed
        if "oracle_text" in new_face:
            self.keywords = self._extract_keywords(new_face["oracle_text"].lower())
        if "colors" in new_face:
            self.colors = new_face["colors"]
    
    def get_current_face(self):
        """Get the currently active face for double-faced cards."""
        if not self.faces:
            return None
        return self.faces[self.current_face]
    
    def get_front_face(self):
        """
        Get the front face data for double-faced cards.
        
        Returns:
            dict or None: Front face data, considering transformation state
        """
        if not self.faces or len(self.faces) < 1:
            return None
        
        # If on battlefield and transformed, return back face
        if hasattr(self, 'is_transformed') and self.is_transformed:
            return self.faces[1] if len(self.faces) > 1 else None
        
        # Otherwise, return front face
        return self.faces[0]

    def get_back_face(self):
        """
        Get the back face data for double-faced cards.
        
        Returns:
            dict or None: Back face data, considering transformation state
        """
        if not self.faces or len(self.faces) < 2:
            return None
        
        # If on battlefield and transformed, return front face
        if hasattr(self, 'is_transformed') and self.is_transformed:
            return self.faces[0]
        
        # Otherwise, return back face
        return self.faces[1]
    
    def is_transforming_mdfc(self):
        """
        Determine if the card is a transforming Double-Faced Card (DFC).
        
        Returns:
            bool: True if the card is a transforming DFC, False otherwise
        """
        # Check if the card has multiple faces
        if not self.faces or len(self.faces) < 2:
            return False
        
        # Look for transform-related keywords in oracle text
        if hasattr(self, 'oracle_text'):
            transform_terms = [
                'transform', 'night', 'daybound', 'nightbound', 
                'werewolf', 'transforms', 'transform itself'
            ]
            
            # Check if any transform term is in the oracle text
            text_indicates_transform = any(
                term in self.oracle_text.lower() 
                for term in transform_terms
            )
            
            if text_indicates_transform:
                return True
        
        # Additional heuristics for transformation
        for face in self.faces:
            # Check if either face mentions transformation
            if 'transform' in str(face).lower():
                return True
        
        return False

    #
    # Card type-specific properties
    #
    @property
    def room_name(self):
        """
        Return the correct Room name, handling split Room cards.
        
        Returns:
            str: The Room name, or None if not a Room card
        """
        if not self.is_room:
            return None
        
        # Handle split Room cards
        if hasattr(self, 'card_faces') and len(self.card_faces) == 2:
            # Combine door names
            door_names = [
                face.get('name', f'Unnamed Door {i+1}') 
                for i, face in enumerate(self.card_faces)
            ]
            return ' // '.join(door_names)
        
        # Fallback to name attribute
        return getattr(self, 'name', 'Unnamed Room')

    def is_saga(self):
        """Check if this card is a Saga."""
        return 'saga' in self.type_line.lower() or 'saga' in ' '.join(self.subtypes).lower()

    def is_battle(self):
        """Check if this card is a Battle."""
        return 'battle' in self.type_line.lower()

    def is_mdfc(self):
        """
        Check if this card is a Modal Double-Faced Card.
        MDFCs differ from transforming DFCs in that they don't transform after entering the battlefield.
        """
        if not self.faces:
            return False
            
        # Check for MDFC indicator in oracle text or type line
        if hasattr(self, 'oracle_text') and "//" in self.oracle_text:
            return True
        
        # If the card has faces but no transform mechanic, it's likely an MDFC
        has_transform = False
        if hasattr(self, 'oracle_text'):
            transform_terms = ["transform", "night", "daybound", "nightbound", "werewolf"]
            has_transform = any(term in self.oracle_text.lower() for term in transform_terms)
        
        return self.faces and not has_transform

    @property
    def back_face(self):
        """Get the back face data for double-faced cards."""
        if not self.faces or len(self.faces) < 2:
            return None
        return self.faces[1]

    def has_adventure(self):
        """Check if this card has an Adventure component."""
        if not hasattr(self, 'oracle_text'):
            return False
        return 'adventure' in self.oracle_text.lower()

    def get_adventure_data(self):
        """
        Parse the adventure portion of the card.
        Returns a dictionary with adventure name, cost, type, and effect.
        """
        if not self.has_adventure():
            return None
            
        oracle_text = self.oracle_text
        
        # Look for adventure pattern
        import re
        pattern = r"(?:^|\n)([^\n]+)\s+([^\n]+)\s*\(Adventure\)[\s\n]*([^\n]+)[\s\n]*((?:[^\n][\s\n]*)+)"
        match = re.search(pattern, oracle_text, re.IGNORECASE)
        
        if not match:
            return None
            
        adventure_name = match.group(1).strip()
        adventure_cost = match.group(2).strip()
        adventure_type = match.group(3).strip()
        adventure_effect = match.group(4).strip()
        
        return {
            "name": adventure_name,
            "cost": adventure_cost,
            "type": adventure_type,
            "effect": adventure_effect
        }

    #
    # Keyword and subtype handling methods
    #
    def _extract_keywords(self, oracle_text):
        """Extract keywords from oracle text with comprehensive support for all MTG keywords."""
        # Initialize keyword array with all zeros
        keywords = [0] * len(self.ALL_KEYWORDS)
        
        if not oracle_text:
            return keywords
            
        # Normalize oracle text for more reliable matching
        oracle_text = oracle_text.lower()
        
        # Use more efficient lookup with set operations
        oracle_words = set(re.findall(r'\b\w+\b', oracle_text))
        
        # Check for each keyword using more efficient matching
        for i, keyword in enumerate(self.ALL_KEYWORDS):
            # Handle multi-word keywords (e.g., "first strike")
            if ' ' in keyword:
                if keyword in oracle_text:
                    keywords[i] = 1
            else:
                # For single-word keywords, use word boundaries to avoid partial matches
                if keyword in oracle_words:
                    keywords[i] = 1
                # Check for variations (cycling -> cycles, etc.)
                elif f"{keyword}s" in oracle_words or f"{keyword}ing" in oracle_words:
                    keywords[i] = 1
        
        # Special handling for keywords with variations
        # "can't be blocked" -> unblockable
        if "can't be blocked" in oracle_text:
            idx = self.ALL_KEYWORDS.index('unblockable')
            if idx < len(keywords):
                keywords[idx] = 1
            
        # "protection from" -> protection
        if "protection from" in oracle_text:
            idx = self.ALL_KEYWORDS.index('protection')
            if idx < len(keywords):
                keywords[idx] = 1
            
        # Check landwalk variations
        for land_type in ["island", "mountain", "forest", "swamp", "plains", "desert", "legendary"]:
            if f"{land_type}walk" in oracle_text:
                idx = self.ALL_KEYWORDS.index('landwalk')
                if idx < len(keywords):
                    keywords[idx] = 1
                break
        
        return keywords

    def _extract_colors(self, color_identity):
        """Extract colors from color identity."""
        colors = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0}
        for c in color_identity:
            if c in colors:
                colors[c] = 1
        return list(colors.values())

    def compute_subtype_vector(self):
        """
        Compute a one-hot vector for this card's subtypes based on the global SUBTYPE_VOCAB.
        This method must be called after Card.SUBTYPE_VOCAB is set.
        """
        self.subtype_vector = [1 if subtype in self.subtypes else 0 for subtype in Card.SUBTYPE_VOCAB]

    #
    # Performance and learning methods
    #
    def update_performance(self, outcome, learning_rate=0.1):
        """
        Update the performance rating.
        outcome: a float in [-1, 1] (e.g., +1 for a strong positive outcome, -1 for negative)
        The performance_rating is updated using an exponential moving average.
        """
        # Normalize outcome to range 0-1 (e.g., -1 -> 0, +1 -> 1)
        normalized = (outcome + 1) / 2.0
        self.performance_rating = (1 - learning_rate) * self.performance_rating + learning_rate * normalized
        self.usage_count += 1

    #
    # Feature vector and cost analysis methods
    #
    def get_cost_vector(self):
        """
        Parse the mana_cost string and return a breakdown as a list:
        [W, U, B, R, G, generic]
        """
        tokens = re.findall(r'\{(.*?)\}', self.mana_cost)
        cost = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "generic": 0}
        for token in tokens:
            if token in cost:
                cost[token] += 1
            elif token.isdigit():
                cost["generic"] += int(token)
            else:
                try:
                    cost["generic"] += int(token)
                except ValueError:
                    pass
        return [cost["W"], cost["U"], cost["B"], cost["R"], cost["G"], cost["generic"]]

    def to_feature_vector(self):
        """
        Feature vector:
        - Base stats: cmc, is_land (1/0), power, toughness (4 dimensions)
        - Exact cost breakdown: [W, U, B, R, G, generic] (6 dimensions)
        - Keywords (11 dimensions: 5 basic + 6 advanced)
        - Colors (5 dimensions)
        - Subtype vector (dimension = len(Card.SUBTYPE_VOCAB))
        - Additional MDFC stats: is_mdfc, back_power, back_toughness (3 dimensions)
        Total dimension = 4 + 6 + 11 + 5 + len(Card.SUBTYPE_VOCAB) + 3
        """
        cost_vector = self.get_cost_vector()
        
        # Get back face info for MDFCs
        is_mdfc_val = 1.0 if self.is_mdfc() else 0.0
        back_power = 0
        back_toughness = 0
        
        if self.is_mdfc() and self.back_face:
            back_face = self.back_face
            if 'power' in back_face and back_face['power'] and isinstance(back_face['power'], str) and back_face['power'].isdigit():
                back_power = int(back_face['power'])
            if 'toughness' in back_face and back_face['toughness'] and isinstance(back_face['toughness'], str) and back_face['toughness'].isdigit():
                back_toughness = int(back_face['toughness'])
        
        base_vector = [
            self.cmc,
            1 if 'land' in self.type_line else 0,
            self.power if self.power is not None else 0,
            self.toughness if self.toughness is not None else 0
        ]
        
        mdfc_vector = [
            is_mdfc_val,
            back_power,
            back_toughness
        ]
        
        return np.array(base_vector + cost_vector + self.keywords + self.colors + self.subtype_vector + mdfc_vector, dtype=np.float32)

# Deck loading function
def load_decks_and_card_db(decks_folder):
    """Load decks from folder and build card database."""
    try:
        card_db = {}  # Change from list to dictionary
        card_name_to_id = {}
        decks = []
       
        for deck_file in os.listdir(decks_folder):
            if not deck_file.endswith('.json'):
                continue
               
            try:
                with open(os.path.join(decks_folder, deck_file), 'r') as f:
                    deck_data = json.load(f)
                    
                    # Extract deck name from filename (removing .json extension)
                    deck_name = os.path.splitext(deck_file)[0]
                    
                    # Create a deck dictionary with name and cards
                    current_deck = {
                        "name": deck_name,
                        "cards": []
                    }
                   
                    for entry in deck_data["deck"]:
                        card_data = entry["card"]
                       
                        # Ensure card_data has a name
                        if "name" not in card_data:
                            raise ValueError(f"Card missing name in {deck_file}")
                           
                        card_name = card_data["name"].lower()
                       
                        # Add to card database if new
                        if card_name not in card_name_to_id:
                            card = Card(card_data)
                            card_id = len(card_db)
                            card.card_id = card_id  # Set the card_id property
                            card_db[card_id] = card  # Store in dictionary with ID as key
                            card_name_to_id[card_name] = card_id
                       
                        # Get the card_id for this card
                        card_id = card_name_to_id[card_name]
                       
                        type_line = card_data.get("type_line", "").lower()
                        is_basic_land = 'basic' in type_line and 'land' in type_line
                        # Validate count
                        count = entry.get("count", 1)
                        if not is_basic_land and (count < 1 or count > 4):
                            raise ValueError(f"Invalid count {count} for {card_name} in {deck_file}")
                       
                        # Add the card ID to the deck
                        current_deck["cards"].extend([card_id] * count)
                   
                    if len(current_deck["cards"]) < 60:
                        raise ValueError(f"Deck {deck_file} has only {len(current_deck['cards'])} cards")
                       
                    decks.append(current_deck)
                   
            except Exception as e:
                logging.error(f"Error loading deck {deck_file}: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                continue
       
        if not card_db:
            raise ValueError("No cards loaded! Check deck files and folder path.")
        
        logging.info(f"Loaded {len(decks)} decks with {len(card_db)} unique cards")
        
        # Build subtype vocabulary (unchanged)
        all_subtypes = set()
        for card_id, card in card_db.items():
            if hasattr(card, "subtypes"):
                all_subtypes.update(card.subtypes)
        Card.SUBTYPE_VOCAB = sorted(all_subtypes)
        logging.info(f"Subtype vocabulary built with {len(Card.SUBTYPE_VOCAB)} entries: {Card.SUBTYPE_VOCAB}")
        
        # Compute each card's subtype vector (unchanged)
        for card_id, card in card_db.items():
            if hasattr(card, "compute_subtype_vector"):
                card.compute_subtype_vector()
        
        return decks, card_db
    except Exception as e:
        logging.error(f"Critical error in deck loading: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
       
        # Return minimal valid data
        default_deck = {
            "name": "Backup Deck",
            "cards": [0] * 60
        }
        return [default_deck], {0: Card({"name": "Backup Card", "type_line": "creature", "card_types": ["creature"], "colors": [0,0,0,0,0], "subtypes": []})}