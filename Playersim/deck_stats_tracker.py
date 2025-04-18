import os
import json
import logging
import hashlib
import numpy as np
import gzip
import asyncio
import statistics
import time
import csv
import datetime
import threading
import re
import glob
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional, Tuple, Set, Union, Callable
from enum import Enum
import math
import concurrent.futures
from .debug import DEBUG_MODE
# Version information for tracking schema changes
STATS_VERSION = "3.1.0"  # Updated version for new format

# Game stage definitions
class GameStage(Enum):
    EARLY = "early"  # Turns 1-3
    MID = "mid"      # Turns 4-7
    LATE = "late"    # Turns 8+

# Game state definitions
class GameState(Enum):
    AHEAD = "ahead"      # Winning position
    PARITY = "parity"    # Even position
    BEHIND = "behind"    # Losing position

# Format definitions
class GameFormat(Enum):
    STANDARD = "standard"
    MODERN = "modern"
    LEGACY = "legacy"
    VINTAGE = "vintage"
    COMMANDER = "commander"
    CUSTOM = "custom"

# Extended archetype definitions
class DeckArchetype(Enum):
    AGGRO = "aggro"
    CONTROL = "control"
    MIDRANGE = "midrange"
    COMBO = "combo"
    TEMPO = "tempo"
    RAMP = "ramp"
    BURN = "burn"
    TRIBAL = "tribal"
    REANIMATOR = "reanimator"
    MILL = "mill"
    TOKENS = "tokens"
    STOMPY = "stompy"
    VOLTRON = "voltron"
    STAX = "stax"
    PRISON = "prison"
    ARISTOCRATS = "aristocrats"
    LIFEGAIN = "lifegain"
    SPELLSLINGER = "spellslinger"
    LANDS = "lands"
    DISCARD = "discard"
    BLINK = "blink"
    TOOLBOX = "toolbox"

class DeckStatsTracker:
    """Comprehensive deck statistics tracker with analytics and recommendations"""
    
    def __init__(self, storage_path: str = "./deck_stats", card_db: Dict = None, use_compression: bool = True):
        # Initialize storage path
        self.base_path = storage_path
        self.use_compression = use_compression
        self.card_db = card_db or {}
        self._ensure_directories()
        self.current_deck_name_p1 = None
        self.current_deck_name_p2 = None
        # Set up locks for thread safety
        self.batch_lock = threading.RLock()
        self.lock = threading.RLock()
        
        # Initialize cache with default settings
        self.cache = self._create_statistics_cache(max_size=100, ttl=3600)
        
        # Initialize batch updates storage
        self.batch_updates = defaultdict(dict)
        
        self.deck_name_to_id = {}
        self.deck_id_to_name = {}
        self.card_id_to_name = {}
        self._initialize_mappings()
        
    def _initialize_mappings(self):
        """Initialize deck name to ID and card ID to name mappings"""
        # Build deck name to ID mapping from existing deck files
        deck_files = self._get_all_deck_files()
        for file_path in deck_files:
            try:
                deck_data = self.load(file_path)
                if deck_data and "name" in deck_data and "deck_id" in deck_data:
                    self.deck_name_to_id[deck_data["name"]] = deck_data["deck_id"]
                    self.deck_id_to_name[deck_data["deck_id"]] = deck_data["name"]
                    
                    # Also load card mappings from the card_list if available
                    if "card_list" in deck_data:
                        for card in deck_data["card_list"]:
                            if "id" in card and "name" in card:
                                self.card_id_to_name[card["id"]] = card["name"]
            except Exception as e:
                logging.error(f"Error initializing deck mapping for {file_path}: {str(e)}")
            
        # Check the Decks folder in the parent directory
        try:
            # Navigate to the parent directory of Playersim (Untitled Mtg project)
            parent_dir = os.path.dirname(self.base_path)
            decks_dir = os.path.join(parent_dir, "Decks")
            
            if os.path.exists(decks_dir) and os.path.isdir(decks_dir):
                logging.info(f"Scanning for deck files in {decks_dir}")
                deck_files = glob.glob(os.path.join(decks_dir, "*.json"))
                
                for deck_file in deck_files:
                    try:
                        # Extract deck name from filename
                        deck_name = os.path.basename(deck_file).replace('.json', '')
                        
                        # Read the deck file to calculate its fingerprint
                        with open(deck_file, 'r') as f:
                            deck_data = json.load(f)
                        
                        # Extract card IDs depending on format
                        if isinstance(deck_data, list):
                            # If it's a simple card list
                            card_ids = [card.get("id") for card in deck_data if isinstance(card, dict) and "id" in card]
                            if not card_ids:
                                card_ids = [card for card in deck_data if isinstance(card, int)]
                        elif isinstance(deck_data, dict) and "cards" in deck_data:
                            # If it's a structured deck with a cards array
                            card_ids = [card.get("id") for card in deck_data["cards"] if isinstance(card, dict) and "id" in card]
                        else:
                            continue  # Skip if we can't extract card IDs
                        
                        if card_ids:
                            # Calculate fingerprint and store mapping
                            deck_id = self.get_deck_fingerprint(card_ids)
                            self.deck_name_to_id[deck_name] = deck_id
                            self.deck_id_to_name[deck_id] = deck_name  # Add this line
                            logging.info(f"Added deck mapping: {deck_name} -> {deck_id}")
                    except Exception as e:
                        logging.debug(f"Error processing deck file {deck_file}: {e}")
                        continue
        except Exception as e:
            logging.debug(f"Error scanning Decks directory: {e}")
        
        # Build card ID to name mapping
        if self.card_db:
            for card_id, card in self.card_db.items():
                if hasattr(card, 'name'):
                    self.card_id_to_name[card_id] = card.name
    
    # === Storage Backend Methods ===
    
    def _ensure_directories(self):
        """Create necessary directories if they don't exist"""
        os.makedirs(self.base_path, exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "decks"), exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "meta"), exist_ok=True)
        os.makedirs(os.path.join(self.base_path, "cards"), exist_ok=True)
        
    def save(self, path: str, data: Any) -> bool:
        """Save data to a JSON file"""
        try:
            full_path = os.path.join(self.base_path, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            if self.use_compression:
                with gzip.open(f"{full_path}.gz", 'wt', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
            else:
                with open(full_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"Error saving data to {path}: {str(e)}")
            return False
        
    def validate_and_repair_compressed_files(self, directory: str):
        """
        Scan and attempt to repair corrupted compressed JSON files
        """
        import os
        import gzip
        import shutil

        for filename in os.listdir(directory):
            if filename.endswith('.json.gz'):
                full_path = os.path.join(directory, filename)
                try:
                    # Attempt to read the file
                    with gzip.open(full_path, 'rb') as f:
                        json.loads(f.read().decode('utf-8'))
                except Exception as e:
                    logging.warning(f"Corrupted file detected: {filename}. Error: {str(e)}")
                    
                    # Create a backup of the corrupted file
                    backup_path = f"{full_path}.corrupt"
                    shutil.copy(full_path, backup_path)
                    
                    # Attempt to recreate the file from the original JSON if possible
                    try:
                        # Remove .gz extension to get original JSON path
                        json_path = full_path[:-3]
                        if os.path.exists(json_path):
                            # Recompress the original JSON
                            with open(json_path, 'rb') as f_in:
                                with gzip.open(full_path, 'wb') as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                            logging.info(f"Successfully recompressed {filename}")
                    except Exception as repair_error:
                        logging.error(f"Could not repair {filename}: {str(repair_error)}")
            
    def load(self, path: str) -> Any:
        try:
            full_path = os.path.join(self.base_path, path)
            # If use_compression is enabled and a gzipped version exists, use that.
            if self.use_compression and os.path.exists(f"{full_path}.gz"):
                full_path += ".gz"
            if not os.path.exists(full_path):
                return None

            # Open in binary mode to check the first two bytes
            with open(full_path, 'rb') as f:
                magic = f.read(2)
            
            if magic == b'\x1f\x8b':
                # It is a gzip file; open in text mode with automatic decompression.
                try:
                    with gzip.open(full_path, 'rt', encoding='utf-8') as f:
                        return json.load(f)
                except UnicodeDecodeError as e:
                    logging.error(f"UTF-8 decode error for {path}, trying latin-1: {e}")
                    try:
                        with gzip.open(full_path, 'rt', encoding='latin-1') as f:
                            return json.load(f)
                    except Exception as decode_error:
                        logging.error(f"Failed to decode compressed file {path} with latin-1: {decode_error}")
                        return None
                except Exception as e:
                    logging.error(f"Error reading compressed file {path}: {e}")
                    return None
            else:
                # Not gzipped – open normally.
                with open(full_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logging.error(f"Error loading data from {path}: {e}")
            return None

    def exists(self, path: str) -> bool:
        """Check if a file exists"""
        full_path = os.path.join(self.base_path, path)
        return os.path.exists(full_path) or os.path.exists(f"{full_path}.gz")

    async def save_async(self, path: str, data: Any) -> bool:
        """Save data to a JSON file asynchronously"""
        def _save():
            return self.save(path, data)
            
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _save)
        
    async def load_async(self, path: str) -> Any:
        """Load data from a JSON file asynchronously"""
        def _load():
            return self.load(path)
            
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _load)
    
    def _validate_stats_types(self, stats: Dict) -> Dict:
        """
        Validate and correct data types in statistics dictionaries.
        
        Args:
            stats: Dictionary containing statistics data
            
        Returns:
            Dictionary with validated and corrected data types
        """
        # Define expected types for each field
        type_map = {
            "games": int,
            "wins": int,
            "losses": int,
            "draws": int,
            "total_turns": int,
            "win_rate": float,
            "usage_count": int,
            "games_played": int,
            "games_drawn": int,
            "wins_when_drawn": float,  # Float to handle draws = 0.5
            "games_not_drawn": int,
            "wins_when_not_drawn": float,
            "games_in_opening_hand": int,
            "wins_when_in_opening_hand": float
        }
        
        # Process top-level fields
        for field, expected_type in type_map.items():
            if field in stats:
                try:
                    # Convert to expected type
                    stats[field] = expected_type(stats[field])
                except (TypeError, ValueError):
                    # Set default value on error
                    if expected_type == int:
                        stats[field] = 0
                    elif expected_type == float:
                        stats[field] = 0.0
        
        # Process nested dictionaries
        for key, value in list(stats.items()):
            if isinstance(value, dict):
                # Recursively validate nested dictionaries
                stats[key] = self._validate_stats_types(value)
            elif isinstance(value, list):
                # Process lists of dictionaries
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        stats[key][i] = self._validate_stats_types(item)
        
        return stats
    
    # === Statistics Cache Methods ===
    def _safe_numeric_operation(self, operation: str, a: Any, b: Any, default: float = 0.0) -> float:
        # (Implementation remains the same)
        """
        Safely perform numeric operations with error handling and type checking.

        Args:
            operation: Operation to perform ('add', 'subtract', 'multiply', 'divide')
            a: First operand
            b: Second operand
            default: Default value to return on error

        Returns:
            Result of operation or default on error
        """
        try:
            # Convert inputs to float for consistent behavior
            a_float = float(a)
            b_float = float(b)

            if operation == 'add':
                return a_float + b_float
            elif operation == 'subtract':
                return a_float - b_float
            elif operation == 'multiply':
                return a_float * b_float
            elif operation == 'divide':
                # Prevent division by zero
                if b_float == 0:
                    return default
                return a_float / b_float
            else:
                logging.warning(f"Unknown operation: {operation}")
                return default
        except (TypeError, ValueError) as e:
            logging.warning(f"Numeric operation error: {str(e)}")
            return default
    
    def _create_statistics_cache(self, max_size: int = 100, ttl: int = 3600): # ttl is no longer used
        """Create a new cache for frequently accessed statistics with LRU eviction"""
        from collections import OrderedDict

        cache = {
            "max_size": max_size,
            # "ttl": ttl, # TTL mechanism removed
            "cache": OrderedDict(),  # Use OrderedDict for LRU behavior
            # "timestamps": {}, # Timestamps removed
            "lock": threading.RLock()
        }
        return cache


    def cache_get(self, key: str) -> Optional[Any]:
        """Get a value from the cache with LRU update (TTL removed)"""
        with self.cache["lock"]:
            if key in self.cache["cache"]:
                # No TTL check needed anymore
                # Move to end to mark as recently used
                self.cache["cache"].move_to_end(key)
                return self.cache["cache"][key]
            return None

    def cache_set(self, key: str, value: Any) -> None:
        """Store a value in the cache with LRU eviction (TTL removed)"""
        with self.cache["lock"]:
            # If key already exists, remove it first to update order
            if key in self.cache["cache"]:
                del self.cache["cache"][key]

            # Evict if full
            if len(self.cache["cache"]) >= self.cache["max_size"]:
                # Evict least recently used item (first item in OrderedDict)
                self.cache["cache"].popitem(last=False)
                # No timestamp to remove

            # Add to cache
            self.cache["cache"][key] = value
            # No timestamp to set

    def cache_invalidate(self, key: str) -> None:
        """Remove a specific key from the cache"""
        with self.cache["lock"]:
            if key in self.cache["cache"]:
                del self.cache["cache"][key]
                # No timestamp to remove

    def cache_clear(self) -> None:
        """Clear the entire cache"""
        with self.cache["lock"]:
            self.cache["cache"].clear()
            # No timestamps to clear

    def _cache_evict_oldest(self) -> None:
        """Evict the oldest entry from the cache (LRU via OrderedDict)"""
        if self.cache["cache"]:
             self.cache["cache"].popitem(last=False) # Removes the first (oldest) item
            
        oldest_key = min(self.cache["timestamps"], key=self.cache["timestamps"].get)
        del self.cache["cache"][oldest_key]
        del self.cache["timestamps"][oldest_key]
        
    def _check_deck_conflict(self, deck_id: str, deck_name: str, card_list: List) -> bool:
        """
        Verify this isn't a duplicate of a different deck with the same ID.
        
        Args:
            deck_id: The fingerprint/ID of the deck
            deck_name: The name of the deck
            card_list: The list of cards in the deck
            
        Returns:
            bool: True if a conflict is detected, False otherwise
        """
        # Check if this ID already exists but with a different name
        if deck_id in self.deck_id_to_name and self.deck_id_to_name[deck_id] != deck_name:
            existing_name = self.deck_id_to_name[deck_id]
            logging.warning(f"Potential deck fingerprint collision: '{deck_name}' has same ID as '{existing_name}': {deck_id}")
            
            # Try to load the existing deck to compare
            existing_stats = self.get_deck_stats(deck_id)
            if existing_stats and "card_list" in existing_stats:
                existing_cards = [card["id"] for card in existing_stats["card_list"]]
                similarity = self.calculate_deck_similarity(card_list, existing_cards)
                
                if similarity < 0.9:  # If decks are less than 90% similar, this is a real conflict
                    logging.error(f"Deck ID collision confirmed: {deck_name} and {existing_name} only have {similarity:.1%} similarity but share ID {deck_id}")
                    return True
                    
            return True  # Conservatively report a conflict if we can't verify
        
        return False
    
    def get_deck_fingerprint(self, card_list: List[Union[int, str, dict]], deck_name: str = None) -> str:
        """
        Generate a deterministic fingerprint for a deck based solely on its card composition.
        
        Args:
            card_list: List of card IDs or dictionaries with card details
            deck_name: Optional name of the deck (not used in fingerprint calculation)
            
        Returns:
            str: MD5 hash fingerprint of the deck
        """
        # Convert the card list to a consistent representation
        if card_list and isinstance(card_list[0], dict) and "id" in card_list[0] and "count" in card_list[0]:
            # Use the immutable composition with card IDs sorted
            items = sorted([(str(item["id"]), item["count"]) for item in card_list], key=lambda x: x[0])
            deck_str = ",".join(f"{card}:{count}" for card, count in items)
        else:
            # Count occurrences of each card
            card_counter = Counter([str(card) for card in card_list])
            items = sorted(card_counter.items(), key=lambda x: x[0])
            deck_str = ",".join(f"{card}:{count}" for card, count in items)
        
        # Generate the hash (no salt or timestamp)
        fingerprint = hashlib.md5(deck_str.encode()).hexdigest()
        
        logging.debug(f"Generated fingerprint {fingerprint} for deck with {len(set(card_list))} unique cards" + 
                    (f" (name: {deck_name})" if deck_name else ""))
        
        return fingerprint
    
    def identify_archetype(self, card_list: List[int]) -> str:
        """
        Identify the archetype of a deck based on its card composition.
        Uses key cards and patterns to determine the most likely archetype.
        Returns a DeckArchetype enum value.
        """
        card_counter = Counter(card_list)
        
        # Enhanced archetype detection based on card characteristics and patterns
        # Define archetype signatures with more comprehensive criteria
        archetype_signatures = {
            DeckArchetype.AGGRO: {
                "creature_ratio": 0.4,
                "avg_cmc_threshold": 2.5,
                "keyword_points": ["haste", "first strike", "menace"],
                "card_name_points": ["goblin", "slith", "knight", "warrior"]
            },
            DeckArchetype.CONTROL: {
                "noncreature_spell_ratio": 0.6,
                "avg_cmc_threshold": 3.5,
                "keyword_points": ["counterspell", "destroy", "exile", "return"],
                "card_name_points": ["wrath", "doom", "verdict", "counter", "deny", "cancel"]
            },
            DeckArchetype.MIDRANGE: {
                "creature_ratio": 0.3,
                "noncreature_spell_ratio": 0.3,
                "keyword_points": ["enters the battlefield", "when ~ enters", "value"],
                "card_name_points": ["titan", "gearhulk", "command", "charm"]
            },
            DeckArchetype.COMBO: {
                "key_cards_threshold": 3,
                "keyword_points": ["whenever", "triggers", "infinite", "search your library"],
                "card_name_points": ["twin", "splinter", "kiki", "oracle", "labman"]
            },
            DeckArchetype.RAMP: {
                "ramp_card_ratio": 0.15,
                "keyword_points": ["add {", "search your library for a land", "put a land"],
                "card_name_points": ["growth", "cultivate", "kodama", "oracle", "land"]
            },
            DeckArchetype.TEMPO: {
                "bounce_spell_count": 3,
                "cheap_creature_ratio": 0.25,
                "keyword_points": ["flash", "return", "bounce", "tap", "doesn't untap"],
                "card_name_points": ["delver", "sprite", "faerie", "tempo", "aggro-control"]
            },
            DeckArchetype.BURN: {
                "direct_damage_ratio": 0.3,
                "keyword_points": ["damage to", "damage to any target", "to target player"],
                "card_name_points": ["lightning", "bolt", "lava", "burn", "shock", "blaze"]
            },
            DeckArchetype.TRIBAL: {
                "tribal_creature_ratio": 0.4,
                "keyword_points": ["other", "you control", "creature type"],
                "card_name_points": ["lord", "chief", "master", "king", "champion", "sliver"]
            },
            DeckArchetype.REANIMATOR: {
                "reanimation_spell_count": 3,
                "graveyard_interaction_ratio": 0.2,
                "keyword_points": ["return", "from your graveyard", "put", "onto the battlefield"],
                "card_name_points": ["reanimate", "resurrection", "living", "dread", "animate"]
            },
            DeckArchetype.MILL: {
                "mill_spell_count": 4,
                "keyword_points": ["put", "cards from the top", "library into", "graveyard"],
                "card_name_points": ["mill", "glimpse", "archive", "thought", "memory"]
            },
            DeckArchetype.TOKENS: {
                "token_generator_count": 4,
                "keyword_points": ["create", "token", "tokens", "creatures", "populate"],
                "card_name_points": ["token", "anthem", "procession", "marshal", "crusade"]
            },
            DeckArchetype.STOMPY: {
                "big_creature_ratio": 0.3,
                "avg_power_threshold": 4.0,
                "keyword_points": ["trample", "fight", "power", "toughness"],
                "card_name_points": ["titan", "dinosaur", "wurm", "behemoth", "giant"]
            }
        }
        
        # Initialize score for each archetype
        archetype_scores = {archetype: 0.0 for archetype in DeckArchetype}
        
        # Count card types and other relevant metrics
        creatures = 0
        noncreature_spells = 0
        lands = 0
        ramp_cards = 0
        reanimation_spells = 0
        mill_spells = 0
        token_generators = 0
        bounce_spell_count = 0  # Changed from bounce_spells to bounce_spell_count
        direct_damage_spells = 0
        
        total_cmc = 0
        card_count = 0
        
        # Tribal detection (count by creature type)
        creature_types = Counter()
        
        # Known combo pieces
        combo_pieces_found = 0
        
        # Card name and text analysis
        card_names = []
        card_texts = []
        
        # Big creatures (power 4+)
        big_creatures = 0
        total_power = 0
        creature_count = 0
        
        # Cheap creatures (CMC <= 2)
        cheap_creatures = 0
        
        # Graveyard interaction
        graveyard_interaction = 0
        
        # Process each card for detailed analysis
        for card_id, count in card_counter.items():
            if card_id in self.card_db:
                card = self.card_db[card_id]
                card_count += count
                
                # Collect name and text for keyword analysis
                if hasattr(card, 'name'):
                    card_names.extend([card.name.lower()] * count)
                
                if hasattr(card, 'oracle_text'):
                    card_texts.extend([card.oracle_text.lower()] * count)
                
                # Type counting
                if hasattr(card, 'card_types'):
                    if 'creature' in card.card_types:
                        creatures += count
                        
                        # Track creature type for tribal detection
                        if hasattr(card, 'subtypes'):
                            for subtype in card.subtypes:
                                creature_types[subtype] += count
                        
                        # Track creature stats
                        if hasattr(card, 'power') and hasattr(card, 'toughness'):
                            creature_count += count
                            total_power += card.power * count
                            
                            if card.power >= 4:
                                big_creatures += count
                        
                        # Track cheap creatures
                        if hasattr(card, 'cmc') and card.cmc <= 2:
                            cheap_creatures += count
                        
                    elif 'instant' in card.card_types or 'sorcery' in card.card_types:
                        noncreature_spells += count
                        
                        # Analyze spell effects from oracle text
                        if hasattr(card, 'oracle_text'):
                            text = card.oracle_text.lower()
                            
                            # Ramp detection
                            if ("search your library for a land" in text or 
                                "add {" in text or 
                                "put a land" in text):
                                ramp_cards += count
                            
                            # Reanimation detection
                            if ("return" in text and 
                                "from your graveyard" in text and 
                                "to the battlefield" in text):
                                reanimation_spells += count
                                graveyard_interaction += count
                            
                            # Mill detection
                            if ("put" in text and 
                                ("cards from the top" in text or "into their graveyard" in text) and 
                                "library" in text):
                                mill_spells += count
                            
                            # Token generation
                            if "create" in text and "token" in text:
                                token_generators += count
                            
                            # Bounce detection
                            if ("return" in text and 
                                "to its owner's hand" in text):
                                bounce_spell_count += count
                            
                            # Direct damage
                            if ("damage" in text and 
                                ("to target" in text or "to any target" in text)):
                                direct_damage_spells += count
                            
                            # Graveyard interaction
                            if "graveyard" in text:
                                graveyard_interaction += count
                    
                    elif 'land' in card.card_types:
                        lands += count
                
                # CMC tracking
                if hasattr(card, 'cmc'):
                    total_cmc += card.cmc * count
                
                # Combo piece detection
                if hasattr(card, 'oracle_text') and any(combo_term in card.oracle_text.lower() 
                                                        for combo_term in ["infinite", "copy", "untap", "whenever", "triggers"]):
                    combo_pieces_found += 1
        
        # Calculate derived metrics
        nonland_count = card_count - lands
        if nonland_count > 0:
            creature_ratio = creatures / nonland_count
            noncreature_spell_ratio = noncreature_spells / nonland_count
            avg_cmc = total_cmc / nonland_count
            ramp_card_ratio = ramp_cards / nonland_count
            direct_damage_ratio = direct_damage_spells / nonland_count
            cheap_creature_ratio = cheap_creatures / nonland_count
            big_creature_ratio = big_creatures / nonland_count
            graveyard_interaction_ratio = graveyard_interaction / nonland_count
        else:
            creature_ratio = 0
            noncreature_spell_ratio = 0
            avg_cmc = 0
            ramp_card_ratio = 0
            direct_damage_ratio = 0
            cheap_creature_ratio = 0
            big_creature_ratio = 0
            graveyard_interaction_ratio = 0
        
        # Calculate average power
        avg_power = total_power / max(1, creature_count)
        
        # Find most common creature type for tribal
        most_common_type = creature_types.most_common(1)
        tribal_creature_count = most_common_type[0][1] if most_common_type else 0
        tribal_creature_ratio = tribal_creature_count / max(1, creatures)
        
        # Calculate scores for each archetype
        for archetype, criteria in archetype_signatures.items():
            score = 0.0
            
            # Ratio-based scoring
            if archetype == DeckArchetype.AGGRO:
                if creature_ratio >= criteria["creature_ratio"]:
                    score += 2.0
                if avg_cmc <= criteria["avg_cmc_threshold"]:
                    score += 2.0
            
            elif archetype == DeckArchetype.CONTROL:
                if noncreature_spell_ratio >= criteria["noncreature_spell_ratio"]:
                    score += 2.0
                if avg_cmc >= criteria["avg_cmc_threshold"]:
                    score += 2.0
            
            elif archetype == DeckArchetype.MIDRANGE:
                if (creature_ratio >= criteria["creature_ratio"] and 
                    noncreature_spell_ratio >= criteria["noncreature_spell_ratio"]):
                    score += 3.0
            
            elif archetype == DeckArchetype.COMBO:
                if combo_pieces_found >= criteria["key_cards_threshold"]:
                    score += 4.0
            
            elif archetype == DeckArchetype.RAMP:
                if ramp_card_ratio >= criteria["ramp_card_ratio"]:
                    score += 3.0
            
            elif archetype == DeckArchetype.TEMPO:
                if bounce_spell_count >= criteria.get("bounce_spell_count", 0):
                    score += 2.0
                if cheap_creature_ratio >= criteria.get("cheap_creature_ratio", 0):
                    score += 2.0
            
            elif archetype == DeckArchetype.BURN:
                if direct_damage_ratio >= criteria.get("direct_damage_ratio", 0):
                    score += 4.0
            
            elif archetype == DeckArchetype.TRIBAL:
                if tribal_creature_ratio >= criteria.get("tribal_creature_ratio", 0):
                    score += 4.0
            
            elif archetype == DeckArchetype.REANIMATOR:
                if reanimation_spells >= criteria.get("reanimation_spell_count", 0):
                    score += 2.0
                if graveyard_interaction_ratio >= criteria.get("graveyard_interaction_ratio", 0):
                    score += 2.0
            
            elif archetype == DeckArchetype.MILL:
                if mill_spells >= criteria.get("mill_spell_count", 0):
                    score += 4.0
            
            elif archetype == DeckArchetype.TOKENS:
                if token_generators >= criteria.get("token_generator_count", 0):
                    score += 4.0
            
            elif archetype == DeckArchetype.STOMPY:
                if big_creature_ratio >= criteria.get("big_creature_ratio", 0):
                    score += 2.0
                if avg_power >= criteria.get("avg_power_threshold", 0):
                    score += 2.0
            
            # Keyword-based scoring
            if "keyword_points" in criteria:
                keyword_matches = sum(1 for keyword in criteria["keyword_points"] 
                                     if any(keyword in text for text in card_texts))
                score += keyword_matches * 0.5
            
            # Card name-based scoring
            if "card_name_points" in criteria:
                name_matches = sum(1 for name_part in criteria["card_name_points"] 
                                  if any(name_part in name for name in card_names))
                score += name_matches * 0.5
            
            archetype_scores[archetype] = score
        
        # Find the archetype with the highest score
        if not archetype_scores:
            return DeckArchetype.MIDRANGE.value  # Default to midrange if no scores
        
        best_archetype = max(archetype_scores.items(), key=lambda x: x[1])
        
        # If the best score is very low, default to a more generic archetype
        if best_archetype[1] < 1.0:
            # Fallback based on simple heuristics
            if creature_ratio >= 0.4 and avg_cmc <= 3.0:
                return DeckArchetype.AGGRO.value
            elif noncreature_spell_ratio >= 0.5 and avg_cmc >= 3.0:
                return DeckArchetype.CONTROL.value
            else:
                return DeckArchetype.MIDRANGE.value
        
        return best_archetype[0].value
    
    def calculate_deck_similarity(self, deck1: List[int], deck2: List[int]) -> float:
        """
        Calculate similarity between two decks.
        Returns a value between 0 (completely different) and 1 (identical).
        """
        # Convert lists to sets for intersection/union calculation
        set1 = set(deck1)
        set2 = set(deck2)
        
        # Calculate Jaccard similarity coefficient
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        
        if union == 0:
            return 0
            
        return intersection / union
    
    def find_similar_decks(self, deck: List[int], threshold: float = 0.7) -> List[str]:
        """
        Find decks similar to the given deck.
        Returns a list of deck IDs that exceed the similarity threshold.
        """
        similar_decks = []
        deck_database = self._get_all_deck_data()
        
        for deck_id, deck_data in deck_database.items():
            if "card_list" in deck_data:
                other_deck = [card["id"] for card in deck_data["card_list"]]
                similarity = self.calculate_deck_similarity(deck, other_deck)
                
                if similarity >= threshold:
                    similar_decks.append(deck_id)
                    
        return similar_decks
    
    # === Statistics Validation Methods ===
    
    def validate_deck_stats(self, stats: Dict) -> Tuple[bool, List[str]]:
        """Validate deck statistics data"""
        errors = []
        
        # Check required fields
        required_fields = ["wins", "losses", "games", "avg_game_length", "archetype", "name", "card_list"]
        for field in required_fields:
            if field not in stats:
                errors.append(f"Missing required field: {field}")
                
        # Check draws field exists
        if "draws" not in stats:
            errors.append("Missing 'draws' field")
        
        # Check numeric fields for valid values
        numeric_fields = ["wins", "losses", "games", "avg_game_length", "draws"]
        for field in numeric_fields:
            if field in stats and (not isinstance(stats[field], (int, float)) or stats[field] < 0):
                errors.append(f"Invalid value for {field}: {stats[field]}")
                
        # Check consistency
        if "wins" in stats and "losses" in stats and "draws" in stats and "games" in stats:
            if stats["wins"] + stats["losses"] + stats["draws"] != stats["games"]:
                errors.append(f"Inconsistent game count: wins({stats['wins']}) + losses({stats['losses']}) + draws({stats['draws']}) != games({stats['games']})")
                
        # Check card list
        if "card_list" in stats:
            if not isinstance(stats["card_list"], list):
                errors.append("Card list must be an array")
            else:
                for card in stats["card_list"]:
                    if not isinstance(card, dict) or "id" not in card or "name" not in card or "count" not in card:
                        errors.append(f"Invalid card entry in card list: {card}")
        
        # Check performance_by_stage has draws
        if "performance_by_stage" in stats:
            for stage, stage_data in stats["performance_by_stage"].items():
                if "draws" not in stage_data:
                    errors.append(f"Missing 'draws' field in performance_by_stage.{stage}")
        
        return len(errors) == 0, errors
        
    def validate_card_stats(self, stats: Dict) -> Tuple[bool, List[str]]:
        """Validate card statistics data"""
        errors = []
        
        # Check required fields
        required_fields = ["wins", "losses", "games", "avg_game_length", "archetype", "name", "composition"]
        for field in required_fields:
            if field not in stats:
                errors.append(f"Missing required field: {field}")
                
        # Check numeric fields for valid values
        numeric_fields = ["games_played", "wins", "losses", "usage_count", "win_rate"]
        for field in numeric_fields:
            if field in stats and (not isinstance(stats[field], (int, float)) or (field != "win_rate" and stats[field] < 0)):
                errors.append(f"Invalid value for {field}: {stats[field]}")
                
        # Check consistency
        if "wins" in stats and "losses" in stats and "games_played" in stats:
            if stats["wins"] + stats["losses"] != stats["games_played"]:
                errors.append(f"Inconsistent game count: wins({stats['wins']}) + losses({stats['losses']}) != games_played({stats['games_played']})")
                
        # Check win rate calculation
        if "wins" in stats and "games_played" in stats and "win_rate" in stats:
            if stats["games_played"] > 0:
                expected_win_rate = stats["wins"] / stats["games_played"]
                if abs(stats["win_rate"] - expected_win_rate) > 0.001:  # Allow for floating point differences
                    errors.append(f"Inconsistent win rate: {stats['win_rate']} vs expected {expected_win_rate}")
                    
        return len(errors) == 0, errors
        
    def repair_deck_stats(self, stats: Dict) -> Dict:
        """Attempt to repair inconsistent deck statistics"""
        # Ensure draws field exists
        if "draws" not in stats:
            stats["draws"] = 0
        
        # Fix inconsistent game count by recalculating total games
        if "wins" in stats and "losses" in stats and "draws" in stats:
            stats["games"] = stats["wins"] + stats["losses"] + stats["draws"]
                
        if "wins" in stats and "games" in stats and stats["games"] > 0:
            # Calculate win rate including draws (0.5 points per draw)
            stats["win_rate"] = (stats["wins"] + 0.5 * stats["draws"]) / stats["games"]
                
        if "card_list" not in stats:
            stats["card_list"] = []
                
        if "archetype" not in stats:
            stats["archetype"] = DeckArchetype.MIDRANGE.value
                
        if "name" not in stats:
            stats["name"] = "Unknown Deck"
                
        if "avg_game_length" not in stats and "games" in stats and "total_turns" in stats:
            stats["avg_game_length"] = stats["total_turns"] / max(1, stats["games"])

        # Add additional stats repair for enhanced fields
        if "matchups" not in stats:
            stats["matchups"] = {}
                
        if "card_performance" not in stats:
            stats["card_performance"] = {}
                
        if "performance_by_stage" not in stats:
            stats["performance_by_stage"] = {
                "early": {"wins": 0, "losses": 0, "draws": 0},
                "mid": {"wins": 0, "losses": 0, "draws": 0},
                "late": {"wins": 0, "losses": 0, "draws": 0}
            }
        else:
            # Ensure each stage has draws field
            for stage in stats["performance_by_stage"]:
                if "draws" not in stats["performance_by_stage"][stage]:
                    stats["performance_by_stage"][stage]["draws"] = 0
                
        if "meta_position" not in stats:
            stats["meta_position"] = {}
        
        # Ensure performance_by_position has draws fields
        if "performance_by_position" in stats:
            for position in stats["performance_by_position"]:
                if "draws" not in stats["performance_by_position"][position]:
                    stats["performance_by_position"][position]["draws"] = 0
        else:
            stats["performance_by_position"] = {
                "ahead": {"wins": 0, "losses": 0, "draws": 0},
                "parity": {"wins": 0, "losses": 0, "draws": 0},
                "behind": {"wins": 0, "losses": 0, "draws": 0}
            }
            
        return stats
        
    def repair_card_stats(self, stats: Dict) -> Dict:
        """Attempt to repair inconsistent card statistics"""
        if "wins" in stats and "losses" in stats:
            stats["games_played"] = stats["wins"] + stats["losses"]
            
        if "wins" in stats and "games_played" in stats and stats["games_played"] > 0:
            stats["win_rate"] = stats["wins"] / stats["games_played"]
            
        if "name" not in stats:
            stats["name"] = "Unknown Card"
            
        if "usage_count" not in stats:
            stats["usage_count"] = stats.get("games_played", 0)
            
        # Repair enhanced card statistics
        if "performance_by_turn" not in stats:
            stats["performance_by_turn"] = {}
            
        if "drawn_win_rate" not in stats and "games_drawn" in stats and stats["games_drawn"] > 0:
            stats["drawn_win_rate"] = stats.get("wins_when_drawn", 0) / stats["games_drawn"]
            
        if "not_drawn_win_rate" not in stats and "games_not_drawn" in stats and stats["games_not_drawn"] > 0:
            stats["not_drawn_win_rate"] = stats.get("wins_when_not_drawn", 0) / stats["games_not_drawn"]
            
        if "opening_hand_win_rate" not in stats and "games_in_opening_hand" in stats and stats["games_in_opening_hand"] > 0:
            stats["opening_hand_win_rate"] = stats.get("wins_when_in_opening_hand", 0) / stats["games_in_opening_hand"]
            
        if "performance_by_position" not in stats:
            stats["performance_by_position"] = {
                "ahead": {"wins": 0, "losses": 0, "played": 0},
                "parity": {"wins": 0, "losses": 0, "played": 0},
                "behind": {"wins": 0, "losses": 0, "played": 0}
            }
            
        return stats

    # === Card Synergy Methods ===
    
    def calculate_synergy_score(self, card1_id: int, card2_id: int) -> float:
        """
        Calculate a synergy score between two cards with enhanced safety checks.
        Returns a value between 0 (no synergy) and 1 (maximum synergy).
        """
        # Check if score is already calculated
        key = f"{min(card1_id, card2_id)}_{max(card1_id, card2_id)}"
        cached_score = self.cache_get(f"synergy:{key}")
        if cached_score is not None:
            return cached_score
            
        # Get the card objects with explicit error handling
        card1 = self.card_db.get(card1_id)
        card2 = self.card_db.get(card2_id)
        
        if not card1 or not card2:
            return 0.0

        # Define synergy types with weights
        synergy_types = {
            "tribal": 0.8,       # Cards that share a creature type
            "mechanic": 0.7,     # Cards that share a keyword or mechanic
            "combo": 0.9,        # Cards that form a known combo
            "theme": 0.6,        # Cards that share a theme
            "color": 0.3,        # Cards that share a color
            "curve": 0.4,        # Cards that fit well on curve
            "support": 0.75      # Cards that directly support each other
        }
            
        score = 0.0
        max_score = 0.0
        
        # Safely check for tribal synergies
        has_tribal_synergy = False
        if hasattr(card1, 'subtypes') and hasattr(card2, 'subtypes'):
            shared_types = set(getattr(card1, 'subtypes', [])) & set(getattr(card2, 'subtypes', []))
            has_tribal_synergy = len(shared_types) > 0
        
        if has_tribal_synergy:
            score += synergy_types["tribal"]
        max_score += synergy_types["tribal"]
            
        # Check for mechanic synergies
        if hasattr(card1, 'keywords') and hasattr(card2, 'keywords'):
            shared_keywords = 0
            for i in range(min(len(card1.keywords), len(card2.keywords))):
                if card1.keywords[i] > 0 and card2.keywords[i] > 0:
                    shared_keywords += 1
            
            if shared_keywords > 0:
                score += synergy_types["mechanic"] * (shared_keywords / max(1, min(len(card1.keywords), len(card2.keywords))))
            max_score += synergy_types["mechanic"]
            
        # Check for color synergies
        if hasattr(card1, 'colors') and hasattr(card2, 'colors'):
            shared_colors = sum(1 for i in range(min(len(card1.colors), len(card2.colors))) 
                              if card1.colors[i] > 0 and card2.colors[i] > 0)
            
            if shared_colors > 0:
                score += synergy_types["color"] * (shared_colors / 5)  # Assuming 5 colors
            max_score += synergy_types["color"]
            
        # Check for curve synergies
        if hasattr(card1, 'cmc') and hasattr(card2, 'cmc'):
            # Cards with sequential CMCs often play well together
            if abs(card1.cmc - card2.cmc) == 1:
                score += synergy_types["curve"] * 0.8
            elif abs(card1.cmc - card2.cmc) == 2:
                score += synergy_types["curve"] * 0.5
            max_score += synergy_types["curve"]
            
        # Check for text-based synergies
        if hasattr(card1, 'oracle_text') and hasattr(card2, 'oracle_text'):
            # Check for references to each other's characteristics in oracle text
            card1_text = card1.oracle_text.lower()
            card2_text = card2.oracle_text.lower()
            
            # Check if card1 mentions anything related to card2
            card2_relevant_terms = []
            if hasattr(card2, 'card_types'):
                card2_relevant_terms.extend(card2.card_types)
            if hasattr(card2, 'subtypes'):
                card2_relevant_terms.extend(card2.subtypes)
            
            card1_relevant_terms = []
            if hasattr(card1, 'card_types'):
                card1_relevant_terms.extend(card1.card_types)
            if hasattr(card1, 'subtypes'):
                card1_relevant_terms.extend(card1.subtypes)
            
            # Count mentions
            mentions = 0
            for term in card2_relevant_terms:
                if term.lower() in card1_text:
                    mentions += 1
                    
            for term in card1_relevant_terms:
                if term.lower() in card2_text:
                    mentions += 1
            
            if mentions > 0:
                score += synergy_types["support"] * min(1.0, mentions / 3)
            max_score += synergy_types["support"]
            
        # Hard-coded known combos could be added here
        # This would require a database of known card combinations
        
        # Normalize the score
        if max_score > 0:
            final_score = score / max_score
        else:
            final_score = 0.0
            
        # Cache the result
        self.cache_set(f"synergy:{key}", final_score)
        
        return final_score
    
    def find_synergistic_cards(self, card_id: int, card_pool: List[int], threshold: float = 0.4) -> List[Tuple[int, float]]:
        """
        Find cards that have good synergy with the given card.
        Returns a list of (card_id, synergy_score) tuples.
        """
        synergies = []
        
        for other_id in card_pool:
            if other_id != card_id:
                score = self.calculate_synergy_score(card_id, other_id)
                if score >= threshold:
                    synergies.append((other_id, score))
                    
        # Sort by synergy score (highest first)
        synergies.sort(key=lambda x: x[1], reverse=True)
        
        return synergies
    
    def get_deck_synergy_matrix(self, deck: List[int]) -> Dict[Tuple[int, int], float]:
        """
        Calculate synergy scores between all pairs of cards in a deck.
        Returns a dictionary mapping (card1_id, card2_id) to synergy score.
        """
        synergy_matrix = {}
        
        for i, card1_id in enumerate(deck):
            for j, card2_id in enumerate(deck[i+1:], i+1):
                score = self.calculate_synergy_score(card1_id, card2_id)
                synergy_matrix[(card1_id, card2_id)] = score
                
        return synergy_matrix
    
    def calculate_deck_synergy_score(self, deck: List[int]) -> float:
        """
        Calculate an overall synergy score for a deck.
        Returns a value between 0 (low synergy) and 1 (high synergy).
        """
        if len(deck) <= 1:
            return 0.0
            
        # Get pairwise synergies
        synergy_matrix = self.get_deck_synergy_matrix(deck)
        
        # Calculate average synergy score
        if synergy_matrix:
            return sum(synergy_matrix.values()) / len(synergy_matrix)
        else:
            return 0.0
    
    # === Meta Analysis Methods ===
    
    def _load_meta_data(self) -> Dict:
        """Load meta data from storage"""
        meta_data = self.load("meta/meta_data.json")
        if not meta_data:
            # Initialize empty meta data
            meta_data = {
                "version": STATS_VERSION,
                # "last_updated": time.time(), # Removed time dependency
                "total_games": 0,
                "archetypes": {},
                "cards": {},
                "matchups": {},
                "trends": {
                    "weekly": {},
                    "monthly": {}
                }
            }
        return meta_data

    def save_meta_data(self) -> bool:
        """Save meta data to storage"""
        meta_data = self._load_meta_data()
        # meta_data["last_updated"] = time.time() # Removed time dependency
        return self.save("meta/meta_data.json", meta_data)
    
    def update_meta_with_game_result(self, winner_deck: List[int], loser_deck: List[int], 
                                winner_archetype: str, loser_archetype: str,
                                cards_played: Dict[int, List[int]], turn_count: int,
                                is_draw: bool = False) -> bool:
        """
        Update meta data with a new game result.
        
        Args:
            winner_deck: List of card IDs in the winning deck
            loser_deck: List of card IDs in the losing deck
            winner_archetype: Archetype of the winning deck
            loser_archetype: Archetype of the losing deck
            cards_played: Dictionary mapping player ID to list of cards played
            turn_count: Number of turns the game lasted
            is_draw: Whether the game ended in a draw
            
        Returns:
            bool: Whether the update was successful
        """
        # Get current meta data
        meta_data = self._load_meta_data()
        
        # Update total games count
        meta_data["total_games"] += 1
        
        # Add draws counter if it doesn't exist
        if "draws" not in meta_data:
            meta_data["draws"] = 0
        
        # Update archetype stats
        if winner_archetype not in meta_data["archetypes"]:
            meta_data["archetypes"][winner_archetype] = {
                "games": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0
            }
        if loser_archetype not in meta_data["archetypes"]:
            meta_data["archetypes"][loser_archetype] = {
                "games": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0
            }
            
        meta_data["archetypes"][winner_archetype]["games"] += 1
        meta_data["archetypes"][loser_archetype]["games"] += 1
        
        if is_draw:
            meta_data["draws"] += 1
            meta_data["archetypes"][winner_archetype]["draws"] += 1
            meta_data["archetypes"][loser_archetype]["draws"] += 1
        else:
            meta_data["archetypes"][winner_archetype]["wins"] += 1
            meta_data["archetypes"][loser_archetype]["losses"] += 1
        
        # Update win rates
        for archetype in [winner_archetype, loser_archetype]:
            arch_data = meta_data["archetypes"][archetype]
            if arch_data["games"] > 0:
                # Adjust win rate calculation to account for draws
                # Option 1: Count only decisive games (wins / (wins + losses))
                # Option 2: Count draws as half-wins (wins + 0.5*draws) / games
                # Using option 2 here
                arch_data["win_rate"] = (arch_data["wins"] + 0.5 * arch_data["draws"]) / arch_data["games"]
                
                
        # Update matchup matrix
        matchup_key = f"{winner_archetype}_vs_{loser_archetype}"
        reverse_key = f"{loser_archetype}_vs_{winner_archetype}"
        
        if matchup_key not in meta_data["matchups"]:
            meta_data["matchups"][matchup_key] = {"wins": 0, "losses": 0, "draws": 0, "win_rate": 0}
        if reverse_key not in meta_data["matchups"]:
            meta_data["matchups"][reverse_key] = {"wins": 0, "losses": 0, "draws": 0, "win_rate": 0}
            
        if is_draw:
            meta_data["matchups"][matchup_key]["draws"] += 1
            meta_data["matchups"][reverse_key]["draws"] += 1
        else:
            meta_data["matchups"][matchup_key]["wins"] += 1
            meta_data["matchups"][reverse_key]["losses"] += 1
        
        # Update win rates for matchups with draw consideration
        for key in [matchup_key, reverse_key]:
            matchup_data = meta_data["matchups"][key]
            total_games = matchup_data["wins"] + matchup_data["losses"] + matchup_data["draws"]
            if total_games > 0:
                # Count draws as half-wins
                matchup_data["win_rate"] = (matchup_data["wins"] + 0.5 * matchup_data["draws"]) / total_games
        
        # Update card stats - using card name instead of ID
        all_cards = set(winner_deck + loser_deck)
        
        for card_id in all_cards:
            # Get card name
            card_name = self._get_card_name(card_id)
            if not card_name:
                continue  # Skip cards without names
                
            # Use card name as key
            if card_name not in meta_data["cards"]:
                meta_data["cards"][card_name] = {
                    "usage_count": 0,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "win_rate": 0,
                    "play_rate": 0,
                    "archetypes": {}
                }
                
            card_data = meta_data["cards"][card_name]
            
            # Update usage count
            card_data["usage_count"] += (
                winner_deck.count(card_id) + loser_deck.count(card_id)
            )
            
            # Update game stats
            if card_id in winner_deck:
                card_data["games"] += 1
                if is_draw:
                    card_data["draws"] += 1
                else:
                    card_data["wins"] += 1
                
                # Update archetype association
                if winner_archetype not in card_data["archetypes"]:
                    card_data["archetypes"][winner_archetype] = 0
                card_data["archetypes"][winner_archetype] += 1
                
            if card_id in loser_deck:
                card_data["games"] += 1
                if is_draw:
                    # If the card appears in both decks, don't double-count the draw
                    if card_id not in winner_deck:
                        card_data["draws"] += 1
                else:
                    card_data["losses"] += 1
                
                # Update archetype association
                if loser_archetype not in card_data["archetypes"]:
                    card_data["archetypes"][loser_archetype] = 0
                card_data["archetypes"][loser_archetype] += 1
                
            # Update win rate with draws considered
            if card_data["games"] > 0:
                card_data["win_rate"] = (card_data["wins"] + 0.5 * card_data["draws"]) / card_data["games"]
                
            # Update play rate
            card_data["play_rate"] = card_data["games"] / meta_data["total_games"]
        
        # Save updated meta data
        return self.save("meta/meta_data.json", meta_data)
    
    def get_top_archetypes(self, limit: int = 5, min_games: int = 10) -> List[Dict]:
        """
        Get the top performing archetypes.
        
        Args:
            limit: Maximum number of archetypes to return
            min_games: Minimum number of games required for an archetype to be considered
            
        Returns:
            List of archetype data dictionaries
        """
        meta_data = self._load_meta_data()
        
        # Filter archetypes with enough games
        valid_archetypes = [
            (archetype, data) for archetype, data in meta_data["archetypes"].items()
            if data["games"] >= min_games
        ]
        
        # Sort by win rate (descending)
        valid_archetypes.sort(key=lambda x: x[1]["win_rate"], reverse=True)
        
        # Return limited number of results
        return [
            {"archetype": archetype, **data}
            for archetype, data in valid_archetypes[:limit]
        ]
    
    def get_top_cards(self, limit: int = 20, min_games: int = 5) -> List[Dict]:
        """
        Get the top performing cards.
        
        Args:
            limit: Maximum number of cards to return
            min_games: Minimum number of games required for a card to be considered
            
        Returns:
            List of card data dictionaries
        """
        meta_data = self._load_meta_data()
        
        # Filter cards with enough games
        valid_cards = [
            (card_name, data) for card_name, data in meta_data["cards"].items()
            if data["games"] >= min_games
        ]
        
        # Sort by win rate (descending)
        valid_cards.sort(key=lambda x: x[1]["win_rate"], reverse=True)
        
        # Return limited number of results
        return [
            {"card_name": card_name, **data}
            for card_name, data in valid_cards[:limit]
        ]
    
    def get_archetype_matchups(self, archetype: str) -> Dict[str, float]:
        """
        Get matchup win rates for a specific archetype.
        
        Args:
            archetype: The archetype to get matchups for
            
        Returns:
            Dictionary mapping opponent archetypes to win rates
        """
        meta_data = self._load_meta_data()
        matchups = {}
        
        # Look for all matchups involving this archetype
        for key, data in meta_data["matchups"].items():
            if key.startswith(f"{archetype}_vs_"):
                # Extract opponent archetype
                opponent = key.split("_vs_")[1]
                matchups[opponent] = data["win_rate"]
                
        return matchups
    
    def get_card_prevalence_by_archetype(self, card_name: str) -> Dict[str, float]:
        """
        Get the prevalence of a card across different archetypes.
        
        Args:
            card_name: The card name to analyze
            
        Returns:
            Dictionary mapping archetypes to prevalence
        """
        meta_data = self._load_meta_data()
        
        if card_name not in meta_data["cards"]:
            return {}
            
        card_data = meta_data["cards"][card_name]
        total_appearances = sum(card_data["archetypes"].values())
        
        if total_appearances == 0:
            return {}
            
        # Calculate prevalence percentages
        return {
            archetype: count / total_appearances
            for archetype, count in card_data["archetypes"].items()
        }
    
    def get_meta_snapshot(self) -> Dict:
        """
        Get a snapshot of the current metagame.
        
        Returns:
            Dictionary with metagame overview statistics
        """
        meta_data = self._load_meta_data()
        
        return {
            "total_games": meta_data["total_games"],
            "last_updated": meta_data["last_updated"],
            "top_archetypes": self.get_top_archetypes(),
            "top_cards": self.get_top_cards(),
            "archetype_distribution": {
                archetype: data["games"] / meta_data["total_games"]
                for archetype, data in meta_data["archetypes"].items()
                if data["games"] >= 5  # Filter for relevant archetypes
            }
        }
    
    # === Deck Recommendation Methods ===
    
    def suggest_card_replacements(self, deck: List[int], cards_to_replace: List[int]) -> Dict[int, List[Tuple[int, float]]]:
        """
        Suggest replacement cards for cards in a deck.
        
        Args:
            deck: List of card IDs in the deck
            cards_to_replace: List of card IDs to find replacements for
            
        Returns:
            Dictionary mapping card IDs to replace to list of (replacement_id, score) tuples
        """
        recommendations = {}
        
        # Get all decks from storage to form a card pool
        deck_files = self._get_all_deck_files()
        card_pool = set()
        
        for file_path in deck_files:
            deck_data = self.load(file_path)
            if deck_data and "card_list" in deck_data:
                for card in deck_data["card_list"]:
                    card_pool.add(card["id"])
        
        # For each card to replace, find suitable replacements
        for card_id in cards_to_replace:
            # Calculate synergy with the rest of the deck
            remaining_deck = [c for c in deck if c != card_id]
            
            # Calculate CMC and color requirements of the card to replace
            card = self.card_db.get(card_id)
            if not card:
                continue
                
            target_cmc = getattr(card, 'cmc', 0)
            target_colors = getattr(card, 'colors', [0, 0, 0, 0, 0])
            target_types = getattr(card, 'card_types', [])
            
            # Score potential replacements
            replacement_scores = []
            
            for candidate_id in card_pool:
                if candidate_id in deck:
                    continue  # Skip cards already in the deck
                    
                candidate = self.card_db.get(candidate_id)
                if not candidate:
                    continue
                    
                # Base score starts at 0
                score = 0.0
                
                # CMC similarity (closer is better)
                cmc_diff = abs(target_cmc - getattr(candidate, 'cmc', 0))
                cmc_score = max(0, 1 - (cmc_diff / 3))  # Normalize to 0-1
                score += cmc_score * 0.3  # 30% weight to CMC
                
                # Color compatibility
                candidate_colors = getattr(candidate, 'colors', [0, 0, 0, 0, 0])
                color_match = sum(1 for i in range(min(len(target_colors), len(candidate_colors))) 
                                if target_colors[i] > 0 and candidate_colors[i] > 0)
                color_score = color_match / max(1, sum(1 for c in target_colors if c > 0))
                score += color_score * 0.2  # 20% weight to color
                
                # Type compatibility
                candidate_types = getattr(candidate, 'card_types', [])
                type_match = len(set(target_types) & set(candidate_types))
                type_score = type_match / max(1, len(target_types))
                score += type_score * 0.2  # 20% weight to type
                
                # Synergy with remaining deck
                synergy_scores = [
                    self.calculate_synergy_score(candidate_id, other_id)
                    for other_id in remaining_deck
                ]
                avg_synergy = sum(synergy_scores) / max(1, len(synergy_scores))
                score += avg_synergy * 0.3  # 30% weight to synergy
                
                replacement_scores.append((candidate_id, score))
            
            # Sort by score (highest first) and take top 5
            replacement_scores.sort(key=lambda x: x[1], reverse=True)
            recommendations[card_id] = replacement_scores[:5]
            
        return recommendations
    
    def optimize_deck(self, deck: List[int], optimization_goal: str = "win_rate") -> Dict:
        """
        Suggest optimizations for a deck.
        
        Args:
            deck: List of card IDs in the deck
            optimization_goal: Target metric for optimization ("win_rate", "consistency", "synergy")
            
        Returns:
            Dictionary with optimization suggestions
        """
        # Analyze the current deck
        deck_stats = self._analyze_deck(deck)
        
        # Identify underperforming cards
        underperforming = []
        meta_data = self._load_meta_data()
        
        for card_id in deck:
            # Get card name
            card_name = self._get_card_name(card_id)
            if not card_name:
                continue
                
            # Check meta data for this card
            if card_name in meta_data["cards"]:
                card_data = meta_data["cards"][card_name]
                
                # Check if card is underperforming
                if card_data["games"] >= 5:  # Minimum sample size
                    if optimization_goal == "win_rate" and card_data["win_rate"] < 0.45:
                        underperforming.append(card_id)
                    elif optimization_goal == "consistency" and card_data["play_rate"] < 0.3:
                        underperforming.append(card_id)
            
            # If not enough meta data, check synergy
            else:
                if optimization_goal == "synergy":
                    # Calculate synergy with rest of deck
                    other_cards = [c for c in deck if c != card_id]
                    synergy_scores = [
                        self.calculate_synergy_score(card_id, other_id)
                        for other_id in other_cards
                    ]
                    avg_synergy = sum(synergy_scores) / max(1, len(synergy_scores))
                    
                    if avg_synergy < 0.3:  # Low synergy threshold
                        underperforming.append(card_id)
        
        # Get recommendations for underperforming cards
        replacements = self.suggest_card_replacements(deck, underperforming)
        
        # Adjust deck composition (mana curve, color balance)
        compositional_suggestions = self._get_compositional_suggestions(deck)
        
        return {
            "current_stats": deck_stats,
            "underperforming_cards": [
                {"id": card_id, "name": self._get_card_name(card_id) or f"Card {card_id}"}
                for card_id in underperforming
            ],
            "replacement_suggestions": {
                str(card_id): [
                    {"id": rep_id, "name": self._get_card_name(rep_id) or f"Card {rep_id}", "score": score}
                    for rep_id, score in reps
                ]
                for card_id, reps in replacements.items()
            },
            "compositional_suggestions": compositional_suggestions
        }
    
    def _analyze_deck(self, deck: List[int]) -> Dict:
        """
        Analyze a deck's characteristics and performance.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with deck statistics
        """
        # Initialize stats
        stats = {
            "card_count": len(deck),
            "mana_curve": {},
            "color_distribution": [0, 0, 0, 0, 0],  # WUBRG
            "card_types": {},
            "synergy_score": 0,
            "meta_position": {}
        }
        
        # Calculate mana curve
        for card_id in deck:
            card = self.card_db.get(card_id)
            if not card:
                continue
                
            # Update mana curve
            cmc = getattr(card, 'cmc', 0)
            cmc_str = str(int(cmc))
            if cmc_str not in stats["mana_curve"]:
                stats["mana_curve"][cmc_str] = 0
            stats["mana_curve"][cmc_str] += 1
            
            # Update color distribution
            if hasattr(card, 'colors'):
                for i, color in enumerate(card.colors):
                    stats["color_distribution"][i] += color
                    
            # Update card types
            if hasattr(card, 'card_types'):
                for card_type in card.card_types:
                    if card_type not in stats["card_types"]:
                        stats["card_types"][card_type] = 0
                    stats["card_types"][card_type] += 1
        
        # Calculate overall synergy score
        stats["synergy_score"] = self.calculate_deck_synergy_score(deck)
        
        # Determine meta position
        deck_fingerprint = self.get_deck_fingerprint(deck)
        deck_archetype = self.identify_archetype(deck)
        
        # Check if we have meta data for this archetype
        meta_data = self._load_meta_data()
        if deck_archetype in meta_data["archetypes"]:
            archetype_data = meta_data["archetypes"][deck_archetype]
            stats["meta_position"]["archetype_win_rate"] = archetype_data["win_rate"]
            stats["meta_position"]["archetype_popularity"] = archetype_data["games"] / max(1, meta_data["total_games"])
            
            # Get matchups
            stats["meta_position"]["matchups"] = self.get_archetype_matchups(deck_archetype)
        
        return stats
    
    def _get_compositional_suggestions(self, deck: List[int]) -> Dict:
        """
        Get suggestions for adjusting deck composition.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with compositional suggestions
        """
        suggestions = {}
        deck_stats = self._analyze_deck(deck)
        
        # Check mana curve
        curve = deck_stats["mana_curve"]
        curve_int = {int(k): v for k, v in curve.items()}
        
        # Ideal curve roughly follows a bell shape centered on 2-3 CMC
        ideal_curve = {
            0: 0.05,  # 5% 0-drops
            1: 0.15,  # 15% 1-drops
            2: 0.25,  # 25% 2-drops
            3: 0.25,  # 25% 3-drops
            4: 0.15,  # 15% 4-drops
            5: 0.10,  # 10% 5-drops
            6: 0.05   # 5% 6+ drops
        }
        
        curve_suggestions = []
        for cmc, ideal_ratio in ideal_curve.items():
            current_count = curve_int.get(cmc, 0)
            ideal_count = int(ideal_ratio * len(deck))
            diff = ideal_count - current_count
            
            if diff > 2:  # Need significantly more
                curve_suggestions.append(f"Add {diff} more {cmc}-cost cards")
            elif diff < -2:  # Need significantly fewer
                curve_suggestions.append(f"Remove {-diff} {cmc}-cost cards")
                
        if curve_suggestions:
            suggestions["mana_curve"] = curve_suggestions
            
        # Check color balance
        colors = ["white", "blue", "black", "red", "green"]
        color_distr = deck_stats["color_distribution"]
        total_colors = sum(color_distr)
        
        if total_colors > 0:
            color_ratios = [count / total_colors for count in color_distr]
            
            # Check if any color is very underrepresented or overrepresented
            color_suggestions = []
            for i, ratio in enumerate(color_ratios):
                if ratio > 0.4:  # Color is dominant
                    color_suggestions.append(f"Consider reducing {colors[i]} concentration")
                elif 0 < ratio < 0.1:  # Color is splashed
                    color_suggestions.append(f"Consider either strengthening or removing {colors[i]}")
                    
            if color_suggestions:
                suggestions["color_balance"] = color_suggestions
                
        # Check card type distribution
        card_types = deck_stats["card_types"]
        total_cards = sum(card_types.values())
        
        # Land count check
        land_count = card_types.get("land", 0)
        land_ratio = land_count / max(1, total_cards)
        
        if land_ratio < 0.33:
            suggestions["land_count"] = [f"Add more lands (currently {land_count}, aim for at least {int(0.38 * total_cards)})"]
        elif land_ratio > 0.45:
            suggestions["land_count"] = [f"Consider reducing land count (currently {land_count}, try {int(0.38 * total_cards)})"]
            
        # Creature/spell balance
        creature_count = card_types.get("creature", 0)
        creature_ratio = creature_count / max(1, total_cards - land_count)
        
        if creature_ratio < 0.3:
            suggestions["creature_count"] = [f"Add more creatures (currently {creature_count}, aim for at least {int(0.4 * (total_cards - land_count))})"]
        elif creature_ratio > 0.7:
            suggestions["creature_count"] = [f"Add more non-creature spells (currently {total_cards - land_count - creature_count})"]
        
        return suggestions
    
    # === Deck Stats Collection Methods ===
    
    def get_deck_stats(self, deck_key: str) -> Dict:
        """Get statistics for a specific deck"""
        # Check cache first
        cached_stats = self.cache_get(f"deck:{deck_key}")
        if cached_stats:
            return cached_stats
            
        # First try to find the deck by name in our name-to-id mapping
        deck_id = self.deck_name_to_id.get(deck_key, deck_key)
        
        # Try to load using the deck_id
        stats = self.load(f"decks/{deck_id}.json")
        
        # If not found, try to find files with the deck name or key in the filename
        if not stats:
            deck_files = self._get_all_deck_files()
            for file_path in deck_files:
                # Check if the file contains the deck name or key
                file_name = os.path.basename(file_path)
                if deck_key.lower() in file_name.lower():
                    deck_data = self.load(file_path)
                    if deck_data:
                        stats = deck_data
                        # Update mapping
                        if "name" in deck_data and "deck_id" in deck_data:
                            self.deck_name_to_id[deck_data["name"]] = deck_data["deck_id"]
                        break
        
        if stats:
            # Validate and repair if needed
            valid, errors = self.validate_deck_stats(stats)
            if not valid:
                logging.warning(f"Invalid deck stats for {deck_key}: {errors}")
                stats = self.repair_deck_stats(stats)
                
            # Update cache
            self.cache_set(f"deck:{deck_key}", stats)
            
        return stats or {}
    
    def update_deck_stats(self, deck_key: str, update_data: Dict) -> bool:
        """Update statistics for a specific deck with data validation."""
        with self.batch_lock:
            # Get current stats (or initialize if not present)
            current_stats = self.get_deck_stats(deck_key)
            
            # Validate data types before updating
            update_data = self._validate_stats_types(update_data)
            
            for key, value in update_data.items():
                if key == "card_list":
                    # Set static deck composition with enhanced data
                    if "card_list" not in current_stats or not current_stats["card_list"]:
                        enhanced_card_list = []
                        for card in value:
                            enhanced_card = card.copy()  # Copy the card data
                            
                            # Always ensure CMC data is present
                            if "cmc" not in enhanced_card and "id" in enhanced_card:
                                card_id = enhanced_card["id"]
                                if card_id in self.card_db:
                                    card_obj = self.card_db[card_id]
                                    if hasattr(card_obj, 'cmc'):
                                        enhanced_card["cmc"] = card_obj.cmc
                                    else:
                                        # Default CMC if not found
                                        enhanced_card["cmc"] = 0
                                else:
                                    # Default CMC if card not in database
                                    enhanced_card["cmc"] = 0
                            
                            enhanced_card_list.append(enhanced_card)
                            
                        current_stats["card_list"] = enhanced_card_list
                    
                    # Also ensure existing card list has CMC data
                    elif "card_list" in current_stats:
                        for card in current_stats["card_list"]:
                            if "cmc" not in card and "id" in card:
                                card_id = card["id"]
                                if card_id in self.card_db:
                                    card_obj = self.card_db[card_id]
                                    if hasattr(card_obj, 'cmc'):
                                        card["cmc"] = card_obj.cmc
                                    else:
                                        card["cmc"] = 0
                                else:
                                    card["cmc"] = 0
                        
                    # Update cumulative usage in a separate field "card_usage"
                    if "card_usage" not in current_stats:
                        current_stats["card_usage"] = {}
                    for new_card in value:
                        card_id = new_card["id"]
                        # Increment usage count for each card.
                        current_stats["card_usage"][card_id] = current_stats["card_usage"].get(card_id, 0) + new_card["count"]
                elif isinstance(value, dict) and isinstance(current_stats.get(key, {}), dict):
                    if key not in current_stats:
                        current_stats[key] = {}
                    current_stats[key].update(value)
                elif isinstance(value, list) and isinstance(current_stats.get(key, []), list):
                    # For lists other than "card_list", merge without duplication.
                    if key not in current_stats:
                        current_stats[key] = []
                    if key != "card_list":
                        existing_items = set(str(item) for item in current_stats[key])
                        for item in value:
                            if str(item) not in existing_items:
                                current_stats[key].append(item)
                                existing_items.add(str(item))
                else:
                    # For numeric fields, we add; for others, we replace.
                    if key in ["wins", "losses", "games", "total_turns"]:
                        current_stats[key] = current_stats.get(key, 0) + value
                    else:
                        current_stats[key] = value
            
            # Recalculate derived values.
            if "wins" in current_stats and "games" in current_stats and current_stats["games"] > 0:
                current_stats["win_rate"] = current_stats["wins"] / current_stats["games"]
            if "total_turns" in current_stats and "games" in current_stats and current_stats["games"] > 0:
                current_stats["avg_game_length"] = current_stats["total_turns"] / current_stats["games"]
            
            # Ensure consistency between total games and sum of outcomes
            if all(k in current_stats for k in ["wins", "losses", "draws"]):
                expected_games = current_stats["wins"] + current_stats["losses"] + current_stats["draws"]
                if current_stats.get("games", 0) != expected_games:
                    logging.info(f"Fixing inconsistent game count for {deck_key}: {current_stats.get('games', 0)} to {expected_games}")
                    current_stats["games"] = expected_games
                    # Update win rate with corrected game count
                    if current_stats["games"] > 0:
                        current_stats["win_rate"] = (current_stats["wins"] + 0.5 * current_stats["draws"]) / current_stats["games"]
            
            current_stats["last_updated"] = time.time()
                    
            if abs(current_stats.get("games", 0) - expected_games) <= 5:
                current_stats["games"] = expected_games
                
                # Update win rate with corrected game count
                if current_stats["games"] > 0:
                    current_stats["win_rate"] = (current_stats["wins"] + 0.5 * current_stats.get("draws", 0)) / current_stats["games"]
            
            # Update cache and batch updates.
            self.cache_set(f"deck:{deck_key}", current_stats)
            self.batch_updates[deck_key] = current_stats
            
            # Update mapping between deck names and IDs.
            if "name" in current_stats and "deck_id" in current_stats:
                self.deck_name_to_id[current_stats["name"]] = current_stats["deck_id"]
                self.deck_id_to_name[current_stats["deck_id"]] = current_stats["name"]
            
            return True
        
    def get_win_rate_confidence_interval(self, deck_key: str, confidence: float = 0.95) -> Tuple[float, float]:
        """Calculate win rate confidence interval using Wilson score interval"""
        stats = self.get_deck_stats(deck_key)
        wins = stats.get("wins", 0)
        games = stats.get("games", 0)
        
        if games == 0:
            return 0.0, 0.0
            
        # Wilson score interval
        z = 1.96  # 95% confidence
        if confidence == 0.99:
            z = 2.576
        elif confidence == 0.90:
            z = 1.645
            
        p = wins / games
        denominator = 1 + (z**2 / games)
        center = (p + (z**2 / (2 * games))) / denominator
        interval = z * math.sqrt((p * (1 - p) + (z**2 / (4 * games))) / games) / denominator
        
        lower = max(0, center - interval)
        upper = min(1, center + interval)
        
        return lower, upper
    
    def get_card_stats(self, card_id: int) -> Dict:
        """
        Get aggregated statistics for a specific card across all decks.
        
        Args:
            card_id: The card ID to get statistics for
            
        Returns:
            Dictionary with card statistics
        """
        # Get card name
        card_name = self._get_card_name(card_id)
        if not card_name:
            return {
                "name": "Unknown Card",
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "usage_count": 0,
                "win_rate": 0
            }
        
        # Try to get from individual card stats first
        card_file = f"cards/{self._sanitize_filename(card_name)}.json"
        card_stats = self.load(card_file)
        if card_stats:
            return card_stats
        
        # If not found, try to get from meta data
        meta_data = self._load_meta_data()
        if card_name in meta_data["cards"]:
            meta_card_stats = meta_data["cards"][card_name]
            return {
                "name": card_name,
                "games_played": meta_card_stats.get("games", 0),
                "wins": meta_card_stats.get("wins", 0),
                "losses": meta_card_stats.get("losses", 0),
                "usage_count": meta_card_stats.get("usage_count", 0),
                "win_rate": meta_card_stats.get("win_rate", 0),
                "archetypes": meta_card_stats.get("archetypes", {})
            }
        
        # Initialize default stats
        stats = {
            "name": card_name,
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "usage_count": 0,
            "win_rate": 0,
            "drawn_win_rate": 0,
            "performance_by_turn": {},
            "performance_by_position": {
                "ahead": {"wins": 0, "losses": 0, "played": 0},
                "parity": {"wins": 0, "losses": 0, "played": 0},
                "behind": {"wins": 0, "losses": 0, "played": 0}
            }
        }
        
        # Look through all deck files to collect card stats
        deck_files = self._get_all_deck_files()
        
        for file_path in deck_files:
            deck_stats = self.load(file_path)
            if not deck_stats or "card_performance" not in deck_stats:
                continue
            
            # Check if this card has stats in this deck by card ID
            card_str = str(card_id)
            if card_str in deck_stats["card_performance"]:
                card_perf = deck_stats["card_performance"][card_str]
                
                # Aggregate stats
                stats["games_played"] += card_perf.get("games_played", 0)
                stats["wins"] += card_perf.get("wins", 0)
                stats["losses"] += card_perf.get("losses", 0)
                stats["usage_count"] += card_perf.get("usage_count", 0)
                
                # Merge performance by position
                for position in ["ahead", "parity", "behind"]:
                    if position in card_perf.get("performance_by_position", {}):
                        pos_stats = card_perf["performance_by_position"][position]
                        stats["performance_by_position"][position]["wins"] += pos_stats.get("wins", 0)
                        stats["performance_by_position"][position]["losses"] += pos_stats.get("losses", 0)
                        stats["performance_by_position"][position]["played"] += pos_stats.get("played", 0)
                
                # Merge performance by turn
                for turn, turn_stats in card_perf.get("performance_by_turn", {}).items():
                    if turn not in stats["performance_by_turn"]:
                        stats["performance_by_turn"][turn] = {"wins": 0, "losses": 0, "played": 0}
                    
                    stats["performance_by_turn"][turn]["wins"] += turn_stats.get("wins", 0)
                    stats["performance_by_turn"][turn]["losses"] += turn_stats.get("losses", 0)
                    stats["performance_by_turn"][turn]["played"] += turn_stats.get("played", 0)
            
            # Also check by card name if present
            if "card_performance_by_name" in deck_stats and card_name in deck_stats["card_performance_by_name"]:
                card_perf = deck_stats["card_performance_by_name"][card_name]
                
                # Aggregate stats (avoiding double-counting if already counted by ID)
                if card_str not in deck_stats["card_performance"]:
                    stats["games_played"] += card_perf.get("games_played", 0)
                    stats["wins"] += card_perf.get("wins", 0)
                    stats["losses"] += card_perf.get("losses", 0)
                    stats["usage_count"] += card_perf.get("usage_count", 0)
        
        # Calculate aggregate win rates
        if stats["games_played"] > 0:
            stats["win_rate"] = stats["wins"] / stats["games_played"]
        
        # Save card stats for future use
        self.save(card_file, stats)
        
        return stats
    
    def record_game(self, winner_deck: List[int], loser_deck: List[int], 
                    card_db: Dict, turn_count: int, cards_played: Dict = None, 
                    winner_life: int = 20, winner_deck_name: str = None, 
                    loser_deck_name: str = None, is_draw: bool = False,
                    game_stage: str = None, game_state: Union[str, GameState] = "parity", 
                    mulligan_data: Dict = None, opening_hands: Dict = None,
                    draw_history: Dict = None, play_order: Dict = None) -> bool:
        """Record a game result with comprehensive error handling and additional tracking"""
        try:
            # Initialize card database if needed
            if not self.card_db and card_db:
                self.card_db = card_db
                self._initialize_mappings()
            
            # Generate fingerprints with error handling
            try:
                winner_deck_fingerprint = self.get_deck_fingerprint(winner_deck, winner_deck_name)
            except Exception as e:
                logging.error(f"Error generating winner deck fingerprint: {e}")
                winner_deck_fingerprint = hashlib.md5(str(winner_deck).encode()).hexdigest()
                
            try:
                loser_deck_fingerprint = self.get_deck_fingerprint(loser_deck, loser_deck_name)
            except Exception as e:
                logging.error(f"Error generating loser deck fingerprint: {e}")
                loser_deck_fingerprint = hashlib.md5(str(loser_deck).encode()).hexdigest()
            
            # Determine archetypes safely
            try:
                winner_archetype = self.identify_archetype(winner_deck)
            except Exception as e:
                logging.error(f"Error identifying winner archetype: {e}")
                winner_archetype = "midrange"  # Default fallback value
                
            try:
                loser_archetype = self.identify_archetype(loser_deck)
            except Exception as e:
                logging.error(f"Error identifying loser archetype: {e}")
                loser_archetype = "midrange"  # Default fallback value
            
            # Determine game stage and state
            # Handle game stage conversion
            if not game_stage:
                game_stage = "late" if turn_count >= 8 else "mid" if turn_count >= 4 else "early"
            
            # Convert string game stage to enum if needed
            if isinstance(game_stage, str):
                try:
                    game_stage = GameStage(game_stage)
                except ValueError:
                    game_stage = (GameStage.LATE if turn_count >= 8 
                                else GameStage.MID if turn_count >= 4 
                                else GameStage.EARLY)
            
            # Handle game state conversion
            if isinstance(game_state, str):
                try:
                    game_state = GameState(game_state)
                except ValueError:
                    game_state = GameState.PARITY  # Default to parity if invalid
            
            # Default mulligan data if not provided
            if mulligan_data is None:
                mulligan_data = {
                    "winner": 0,
                    "loser": 0
                }
            
            # Default opening hand data if not provided
            if opening_hands is None:
                opening_hands = {
                    "winner": [],
                    "loser": []
                }
            
            # Default draw history if not provided
            if draw_history is None:
                draw_history = {
                    "winner": {},
                    "loser": {}
                }
            
            # Default play order if not provided
            if play_order is None:
                play_order = {"first_player": "winner" if turn_count % 2 == 1 else "loser"}
            
            # Record game result
            game_result_success = self.update_meta_with_game_result(
                winner_deck=winner_deck,
                loser_deck=loser_deck,
                winner_archetype=winner_archetype,
                loser_archetype=loser_archetype,
                cards_played=cards_played or {0: [], 1: []},
                turn_count=turn_count,
                is_draw=is_draw
            )
            
            # In a draw, both decks are technically winners or losers, but for our tracking we'll just update both
            # with accurate is_draw flag rather than treating either as a definitive winner or loser
            success_1 = self._update_deck_stats(
                deck_id=winner_deck_fingerprint,
                deck=winner_deck,
                archetype=winner_archetype,
                is_winner=True if not is_draw else False,
                is_draw=is_draw,
                turn_count=turn_count,
                game_stage=game_stage,
                game_state=game_state,
                deck_name=winner_deck_name,
                mulligan_count=mulligan_data.get("winner", 0),
                opening_hand=opening_hands.get("winner", []),
                draw_history=draw_history.get("winner", {}),
                play_order=play_order.get("first_player") == "winner"
            )
            
            success_2 = self._update_deck_stats(
                deck_id=loser_deck_fingerprint,
                deck=loser_deck,
                archetype=loser_archetype,
                is_winner=False,
                is_draw=is_draw,
                turn_count=turn_count,
                game_stage=game_stage,
                game_state=game_state,
                deck_name=loser_deck_name,
                mulligan_count=mulligan_data.get("loser", 0),
                opening_hand=opening_hands.get("loser", []),
                draw_history=draw_history.get("loser", {}),
                play_order=play_order.get("first_player") == "loser"
            )
            
            # Update card statistics, passing draw information
            success_3 = self._update_card_stats(
                winner_deck_id=winner_deck_fingerprint,
                loser_deck_id=loser_deck_fingerprint,
                cards_played=cards_played or {0: [], 1: []},
                game_stage=game_stage,
                game_state=game_state,
                is_draw=is_draw,
                opening_hands=opening_hands,
                draw_history=draw_history,
                play_order=play_order
            )
            
            # Save updates
            self.save_updates_sync()
            
            if is_draw:
                logging.info(f"Game recorded: Draw between {winner_archetype} and {loser_archetype}, Turns: {turn_count}")
            else:
                logging.info(f"Game recorded: {winner_archetype} (W) vs {loser_archetype} (L), Turns: {turn_count}")
            
            return game_result_success and success_1 and success_2 and success_3
        
        except Exception as e:
            logging.error(f"Error recording game statistics: {e}")
            import traceback
            logging.error(traceback.format_exc())
        return False
        
    def _extract_deck_name_from_files(self, deck_id: str, fallback_archetype: str) -> str:
        """
        Extract a meaningful deck name from various potential sources.
        
        Args:
            deck_id: Unique identifier for the deck
            fallback_archetype: Archetype to use if no other name is found
        
        Returns:
            A cleaned and meaningful deck name
        """
        # Try using predefined deck names (if set during reset)
        if hasattr(self, 'current_deck_name_p1'):
            return self._sanitize_deck_name(self.current_deck_name_p1)
        
        # Try extracting name from filename
        try:
            # Split filename and remove extensions
            filename_parts = deck_id.split('_')
            
            # Look for meaningful parts before "Deck" or archetype
            meaningful_parts = [part for part in filename_parts 
                                if part.lower() not in ['deck', fallback_archetype.lower(), 
                                                        'combo', 'aggro', 'control', 'midrange']]
            
            # Join meaningful parts or use archetype as fallback
            if meaningful_parts:
                return self._sanitize_deck_name(' '.join(meaningful_parts))
        except Exception as e:
            logging.warning(f"Error extracting deck name from filename: {e}")
        
        # Fallback to archetype-based name
        return f"{fallback_archetype.title()} Deck"
    
    def _sanitize_deck_name(self, name: str) -> str:
        """
        Sanitize deck name, removing non-meaningful parts.
        
        Args:
            name: Original deck name
        
        Returns:
            str: Cleaned, meaningful deck name
        """
        # Remove common suffixes and prefixes
        name = re.sub(r'^(My\s*)?', '', name, flags=re.IGNORECASE)
        name = re.sub(r'(Deck)?$', '', name, flags=re.IGNORECASE)
        
        # Remove numeric identifiers
        name = re.sub(r'\d+$', '', name)
        
        # Remove extra whitespace
        name = name.strip()
        
        # Capitalize
        name = name.title()
        
        # Use a default if name becomes empty
        return name if name else "Unnamed Deck"
    
    def _update_deck_stats(self, deck_id: str, deck: List[int], archetype: str,
                            is_winner: bool, turn_count: int,
                            game_stage: GameStage, game_state: GameState,
                            deck_name: str = None, is_draw: bool = False,
                            mulligan_count: int = 0, opening_hand: List[int] = None,
                            draw_history: Dict = None, play_order: bool = True) -> bool:
        """
        Update statistics for a deck with enhanced tracking for mulligans and game progression.
        (Removed time.time() update for last_updated)
        """
        # (Get current stats logic remains)
        stats = self.get_deck_stats(deck_id)
        if not stats:
            # (Initialize new stats logic remains, but without last_updated time)
            card_list = []
            for card_id in set(deck):
                count = deck.count(card_id)
                card_name = self._get_card_name(card_id)
                card_list.append({
                    "id": card_id,
                    "name": card_name or f"Card {card_id}",
                    "count": count
                })
            if deck_name is None:
                deck_name = f"{archetype.title()} Deck"
            stats = {
                "name": deck_name,
                "deck_id": deck_id,
                "archetype": archetype,
                "card_list": card_list,
                "games": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "total_turns": 0,
                "win_rate": 0,
                "mulligan_stats": {
                    "total_mulligans": 0,
                    "games_with_mulligan": 0,
                    "win_rate_with_mulligan": 0,
                    "win_rate_without_mulligan": 0,
                    "avg_mulligans": 0
                },
                "play_order_stats": {
                    "play_first": {"games": 0, "wins": 0, "draws": 0},
                    "play_second": {"games": 0, "wins": 0, "draws": 0}
                },
                "performance_by_stage": {
                    "early": {"wins": 0, "losses": 0, "draws": 0},
                    "mid": {"wins": 0, "losses": 0, "draws": 0},
                    "late": {"wins": 0, "losses": 0, "draws": 0}
                },
                "performance_by_position": {
                    "ahead": {"wins": 0, "losses": 0, "draws": 0},
                    "parity": {"wins": 0, "losses": 0, "draws": 0},
                    "behind": {"wins": 0, "losses": 0, "draws": 0}
                },
                "performance_by_turn": {},
                "matchups": {},
                "specific_matchups": {},
                "card_performance": {},
                "card_performance_by_name": {},
                "meta_position": {},
                # "last_updated": time.time() # Removed time dependency
            }
        else:
            # (Ensure fields exist logic remains the same)
            if "draws" not in stats: stats["draws"] = 0
            if "performance_by_stage" in stats:
                for stage_data in stats["performance_by_stage"].values():
                    if "draws" not in stage_data: stage_data["draws"] = 0
            else: # Ensure structure exists
                stats["performance_by_stage"] = {"early": {"wins": 0,"losses": 0,"draws": 0}, "mid": {"wins": 0,"losses": 0,"draws": 0}, "late": {"wins": 0,"losses": 0,"draws": 0}}
            if "mulligan_stats" not in stats:
                 stats["mulligan_stats"] = {"total_mulligans": 0, "games_with_mulligan": 0, "win_rate_with_mulligan": 0, "win_rate_without_mulligan": 0, "avg_mulligans": 0}
            if "play_order_stats" not in stats:
                 stats["play_order_stats"] = {"play_first": {"games": 0, "wins": 0, "draws": 0}, "play_second": {"games": 0, "wins": 0, "draws": 0}}
            if "performance_by_turn" not in stats: stats["performance_by_turn"] = {}
            if "specific_matchups" not in stats: stats["specific_matchups"] = {}
            if "performance_by_position" not in stats: # Ensure structure exists
                 stats["performance_by_position"] = {"ahead": {"wins": 0,"losses": 0,"draws": 0}, "parity": {"wins": 0,"losses": 0,"draws": 0}, "behind": {"wins": 0,"losses": 0,"draws": 0}}
            else: # Add draws if missing
                for state_data in stats["performance_by_position"].values():
                    if "draws" not in state_data: state_data["draws"] = 0

            if deck_name is not None and deck_name != stats.get("name", ""): stats["name"] = deck_name


        # Update dynamic game statistics.
        stats["games"] += 1
        if is_draw:
            stats["draws"] += 1
        elif is_winner:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        stats["total_turns"] += turn_count
        # Update win_rate to account for draws (0.5 points per draw)
        if stats["games"] > 0: # Avoid division by zero
             stats["win_rate"] = (stats["wins"] + 0.5 * stats["draws"]) / stats["games"]
        else: stats["win_rate"] = 0.0

        # stats["last_updated"] = time.time() # Removed time dependency

        # (Rest of the update logic for mulligan, play order, stage, turn, position remains)
        # Update mulligan statistics
        if mulligan_count > 0:
            stats["mulligan_stats"]["total_mulligans"] += mulligan_count
            stats["mulligan_stats"]["games_with_mulligan"] += 1

        games_with_mulligan = stats["mulligan_stats"]["games_with_mulligan"]
        wins_with_mulligan = stats["mulligan_stats"].get("wins_with_mulligan", 0)
        draws_with_mulligan = stats["mulligan_stats"].get("draws_with_mulligan", 0) # Need to track draws with mulligan
        if mulligan_count > 0:
             if is_winner: wins_with_mulligan += 1
             if is_draw: draws_with_mulligan += 1
        stats["mulligan_stats"]["wins_with_mulligan"] = wins_with_mulligan # Store cumulative wins/draws with mulligans
        stats["mulligan_stats"]["draws_with_mulligan"] = draws_with_mulligan # Store cumulative draws

        games_without_mulligan = stats["games"] - games_with_mulligan
        wins_without_mulligan = stats["mulligan_stats"].get("wins_without_mulligan", 0)
        draws_without_mulligan = stats["mulligan_stats"].get("draws_without_mulligan", 0) # Track draws without mulligan
        if mulligan_count == 0:
             if is_winner: wins_without_mulligan += 1
             if is_draw: draws_without_mulligan += 1
        stats["mulligan_stats"]["wins_without_mulligan"] = wins_without_mulligan
        stats["mulligan_stats"]["draws_without_mulligan"] = draws_without_mulligan

        # Calculate win rates including draws
        if games_with_mulligan > 0: stats["mulligan_stats"]["win_rate_with_mulligan"] = (wins_with_mulligan + 0.5 * draws_with_mulligan) / games_with_mulligan
        else: stats["mulligan_stats"]["win_rate_with_mulligan"] = 0.0
        if games_without_mulligan > 0: stats["mulligan_stats"]["win_rate_without_mulligan"] = (wins_without_mulligan + 0.5 * draws_without_mulligan) / games_without_mulligan
        else: stats["mulligan_stats"]["win_rate_without_mulligan"] = 0.0
        if stats["games"] > 0: stats["mulligan_stats"]["avg_mulligans"] = stats["mulligan_stats"]["total_mulligans"] / stats["games"]
        else: stats["mulligan_stats"]["avg_mulligans"] = 0.0

        # Play order stats
        play_position = "play_first" if play_order else "play_second"
        if play_position not in stats["play_order_stats"]: # Ensure sub-dict exists
            stats["play_order_stats"][play_position] = {"games": 0, "wins": 0, "draws": 0}
        stats["play_order_stats"][play_position]["games"] += 1
        if is_draw: stats["play_order_stats"][play_position]["draws"] += 1
        elif is_winner: stats["play_order_stats"][play_position]["wins"] += 1

        # Perf by stage
        stage_key = game_stage.value
        if stage_key not in stats["performance_by_stage"]: # Ensure sub-dict exists
             stats["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0}
        if is_draw: stats["performance_by_stage"][stage_key]["draws"] += 1
        elif is_winner: stats["performance_by_stage"][stage_key]["wins"] += 1
        else: stats["performance_by_stage"][stage_key]["losses"] += 1

        # Perf by turn
        turn_key = str(turn_count)
        if turn_key not in stats["performance_by_turn"]: # Ensure sub-dict exists
            stats["performance_by_turn"][turn_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
        stats["performance_by_turn"][turn_key]["games"] += 1
        if is_draw: stats["performance_by_turn"][turn_key]["draws"] += 1
        elif is_winner: stats["performance_by_turn"][turn_key]["wins"] += 1
        else: stats["performance_by_turn"][turn_key]["losses"] += 1

        # Perf by position
        position_key = game_state.value
        if position_key not in stats["performance_by_position"]: # Ensure sub-dict exists
             stats["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0}
        if is_draw: stats["performance_by_position"][position_key]["draws"] += 1
        elif is_winner: stats["performance_by_position"][position_key]["wins"] += 1
        else: stats["performance_by_position"][position_key]["losses"] += 1

        # Opening hand stats
        if opening_hand:
            if "opening_hand_stats" not in stats: stats["opening_hand_stats"] = {}
            for card_id in opening_hand:
                card_key = str(card_id)
                if card_key not in stats["opening_hand_stats"]:
                    stats["opening_hand_stats"][card_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
                stats["opening_hand_stats"][card_key]["games"] += 1
                if is_draw: stats["opening_hand_stats"][card_key]["draws"] += 1
                elif is_winner: stats["opening_hand_stats"][card_key]["wins"] += 1
                else: stats["opening_hand_stats"][card_key]["losses"] += 1

        # Draw history stats
        if draw_history:
            if "draw_history_stats" not in stats: stats["draw_history_stats"] = {}
            for turn, cards in draw_history.items():
                turn_key = str(turn)
                if turn_key not in stats["draw_history_stats"]: stats["draw_history_stats"][turn_key] = {}
                for card_id in cards:
                    card_key = str(card_id)
                    if card_key not in stats["draw_history_stats"][turn_key]:
                        stats["draw_history_stats"][turn_key][card_key] = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
                    stats["draw_history_stats"][turn_key][card_key]["games"] += 1
                    if is_draw: stats["draw_history_stats"][turn_key][card_key]["draws"] += 1
                    elif is_winner: stats["draw_history_stats"][turn_key][card_key]["wins"] += 1
                    else: stats["draw_history_stats"][turn_key][card_key]["losses"] += 1

        # Update name mapping
        if "name" in stats:
            self.deck_name_to_id[stats["name"]] = deck_id
            self.deck_id_to_name[deck_id] = stats["name"]


        return self.update_deck_stats(deck_id, stats)

    def _update_card_stats(self, winner_deck_id: str, loser_deck_id: str, 
                        cards_played: Dict[int, List[int]],
                        game_stage: GameStage, game_state: GameState,
                        is_draw: bool = False, opening_hands: Dict = None,
                        draw_history: Dict = None, play_order: Dict = None) -> bool:
        """
        Update statistics for individual cards with enhanced tracking.
        
        Args:
            winner_deck_id: ID of the winning deck
            loser_deck_id: ID of the losing deck
            cards_played: Dictionary mapping player ID to list of cards played
            game_stage: Stage of the game when it ended
            game_state: State of the game from the winner's perspective
            is_draw: Whether the game ended in a draw
            opening_hands: Dictionary with opening hand cards for each player
            draw_history: Dictionary mapping turn numbers to cards drawn that turn
            play_order: Dictionary indicating which player went first
            
        Returns:
            bool: Whether all updates were successful
        """
        success = True
        
        # Get deck stats
        winner_stats = self.get_deck_stats(winner_deck_id)
        loser_stats = self.get_deck_stats(loser_deck_id)
        
        if not winner_stats or not loser_stats:
            return False
        
        # Ensure card_list is used consistently for composition
        winner_composition = winner_stats.get("card_list", [])
        loser_composition = loser_stats.get("card_list", [])
        
        # Initialize defaults for optional parameters
        if opening_hands is None:
            opening_hands = {"winner": [], "loser": []}
        
        if draw_history is None:
            draw_history = {"winner": {}, "loser": {}}
        
        if play_order is None:
            play_order = {"first_player": "unknown"}
            
        # Process cards for winner deck
        first_played = cards_played.get(0, [])
        first_opening_hand = opening_hands.get("winner", [])
        first_draw_history = draw_history.get("winner", {})
        
        # Track when cards were played (by turn)
        first_play_history = {}
        for card_id in first_played:
            # Find when this card was played (simplified - we don't have play history yet)
            # In a real implementation, we'd track when each card was played
            # For now, estimate based on CMC
            card = self.card_db.get(card_id)
            if card and hasattr(card, 'cmc'):
                estimated_turn = max(1, min(int(card.cmc), 20))
                if estimated_turn not in first_play_history:
                    first_play_history[estimated_turn] = []
                first_play_history[estimated_turn].append(card_id)
        
        # Update card performances for winner deck
        for card_entry in winner_composition:
            card_id = card_entry["id"]
            card_name = card_entry["name"]
            
            # Initialize ID-based card performance tracking
            if "card_performance" not in winner_stats:
                winner_stats["card_performance"] = {}
                
            if str(card_id) not in winner_stats["card_performance"]:
                winner_stats["card_performance"][str(card_id)] = {
                    "name": card_name,
                    "games_played": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "usage_count": 0,
                    "win_rate": 0,
                    "games_drawn": 0,
                    "wins_when_drawn": 0,
                    "games_not_drawn": 0,
                    "wins_when_not_drawn": 0,
                    "games_in_opening_hand": 0,
                    "wins_when_in_opening_hand": 0,
                    "performance_by_turn": {},
                    "performance_by_stage": {
                        "early": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "mid": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "late": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "performance_by_position": {
                        "ahead": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "parity": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "behind": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_order_performance": {
                        "play_first": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "play_second": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_curve_stats": {
                        "on_curve": {"games": 0, "wins": 0, "draws": 0},
                        "under_curve": {"games": 0, "wins": 0, "draws": 0},
                        "over_curve": {"games": 0, "wins": 0, "draws": 0}
                    }
                }
                
            # Initialize name-based card performance tracking
            if "card_performance_by_name" not in winner_stats:
                winner_stats["card_performance_by_name"] = {}
                
            if card_name not in winner_stats["card_performance_by_name"]:
                winner_stats["card_performance_by_name"][card_name] = {
                    "name": card_name,
                    "games_played": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "usage_count": 0,
                    "win_rate": 0,
                    "games_drawn": 0,
                    "wins_when_drawn": 0,
                    "games_not_drawn": 0,
                    "wins_when_not_drawn": 0,
                    "games_in_opening_hand": 0,
                    "wins_when_in_opening_hand": 0,
                    "performance_by_turn": {},
                    "performance_by_stage": {
                        "early": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "mid": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "late": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "performance_by_position": {
                        "ahead": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "parity": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "behind": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_order_performance": {
                        "play_first": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "play_second": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_curve_stats": {
                        "on_curve": {"games": 0, "wins": 0, "draws": 0},
                        "under_curve": {"games": 0, "wins": 0, "draws": 0},
                        "over_curve": {"games": 0, "wins": 0, "draws": 0}
                    }
                }
                
            # Get references to both card performance objects for more concise code
            card_perf = winner_stats["card_performance"][str(card_id)]
            name_perf = winner_stats["card_performance_by_name"][card_name]
            
            # Update basic game stats
            card_perf["games_played"] += 1
            name_perf["games_played"] += 1
            
            if is_draw:
                card_perf["draws"] += 1
                name_perf["draws"] += 1
            else:
                card_perf["wins"] += 1
                name_perf["wins"] += 1
            
            # Check if card was played
            was_played = card_id in first_played
            if was_played:
                card_perf["usage_count"] += 1
                name_perf["usage_count"] += 1
                
                # Track performance by game stage
                stage_key = game_stage.value
                if stage_key not in card_perf["performance_by_stage"]:
                    card_perf["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    
                card_perf["performance_by_stage"][stage_key]["played"] += 1
                if is_draw:
                    card_perf["performance_by_stage"][stage_key]["draws"] += 1
                else:
                    card_perf["performance_by_stage"][stage_key]["wins"] += 1
                    
                # Same for name-based tracking
                if stage_key not in name_perf["performance_by_stage"]:
                    name_perf["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    
                name_perf["performance_by_stage"][stage_key]["played"] += 1
                if is_draw:
                    name_perf["performance_by_stage"][stage_key]["draws"] += 1
                else:
                    name_perf["performance_by_stage"][stage_key]["wins"] += 1
                
                # Track performance by game state/position
                position_key = game_state.value
                if position_key not in card_perf["performance_by_position"]:
                    card_perf["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                card_perf["performance_by_position"][position_key]["played"] += 1
                if is_draw:
                    card_perf["performance_by_position"][position_key]["draws"] += 1
                else:
                    card_perf["performance_by_position"][position_key]["wins"] += 1
                
                # Same for name-based tracking
                if position_key not in name_perf["performance_by_position"]:
                    name_perf["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                name_perf["performance_by_position"][position_key]["played"] += 1
                if is_draw:
                    name_perf["performance_by_position"][position_key]["draws"] += 1
                else:
                    name_perf["performance_by_position"][position_key]["wins"] += 1
                
                # Track play order performance
                play_position = "play_first" if play_order.get("first_player") == "winner" else "play_second"
                if play_position not in card_perf["play_order_performance"]:
                    card_perf["play_order_performance"][play_position] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                card_perf["play_order_performance"][play_position]["played"] += 1
                if is_draw:
                    card_perf["play_order_performance"][play_position]["draws"] += 1
                else:
                    card_perf["play_order_performance"][play_position]["wins"] += 1
                    
                # Same for name-based tracking
                if play_position not in name_perf["play_order_performance"]:
                    name_perf["play_order_performance"][play_position] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                name_perf["play_order_performance"][play_position]["played"] += 1
                if is_draw:
                    name_perf["play_order_performance"][play_position]["draws"] += 1
                else:
                    name_perf["play_order_performance"][play_position]["wins"] += 1
                
                # Check if played on curve
                # First, find which turn the card was played
                for turn, cards in first_play_history.items():
                    if card_id in cards:
                        # Get card's CMC
                        card = self.card_db.get(card_id)
                        if card and hasattr(card, 'cmc'):
                            curve_status = ""
                            if turn == int(card.cmc):  # On curve
                                curve_status = "on_curve"
                            elif turn < int(card.cmc):  # Under curve (played early)
                                curve_status = "under_curve"
                            else:  # Over curve (played late)
                                curve_status = "over_curve"
                            
                            # Track curve performance
                            if curve_status:
                                # For ID-based tracking
                                card_perf["play_curve_stats"][curve_status]["games"] += 1
                                if is_draw:
                                    card_perf["play_curve_stats"][curve_status]["draws"] += 1
                                else:
                                    card_perf["play_curve_stats"][curve_status]["wins"] += 1
                                    
                                # For name-based tracking
                                name_perf["play_curve_stats"][curve_status]["games"] += 1
                                if is_draw:
                                    name_perf["play_curve_stats"][curve_status]["draws"] += 1
                                else:
                                    name_perf["play_curve_stats"][curve_status]["wins"] += 1
                        
                        # Record turn-by-turn performance
                        turn_key = str(turn)
                        if "performance_by_turn" not in card_perf:
                            card_perf["performance_by_turn"] = {}
                        if turn_key not in card_perf["performance_by_turn"]:
                            card_perf["performance_by_turn"][turn_key] = {"played": 0, "wins": 0, "draws": 0}
                            
                        card_perf["performance_by_turn"][turn_key]["played"] += 1
                        if is_draw:
                            card_perf["performance_by_turn"][turn_key]["draws"] += 1
                        else:
                            card_perf["performance_by_turn"][turn_key]["wins"] += 1
                            
                        # For name-based tracking
                        if "performance_by_turn" not in name_perf:
                            name_perf["performance_by_turn"] = {}
                        if turn_key not in name_perf["performance_by_turn"]:
                            name_perf["performance_by_turn"][turn_key] = {"played": 0, "wins": 0, "draws": 0}
                            
                        name_perf["performance_by_turn"][turn_key]["played"] += 1
                        if is_draw:
                            name_perf["performance_by_turn"][turn_key]["draws"] += 1
                        else:
                            name_perf["performance_by_turn"][turn_key]["wins"] += 1
            
            # Check if card was in opening hand
            in_opening_hand = card_id in first_opening_hand
            if in_opening_hand:
                card_perf["games_in_opening_hand"] += 1
                name_perf["games_in_opening_hand"] += 1
                
                if is_draw:
                    # Award half a win for draws
                    card_perf["wins_when_in_opening_hand"] += 0.5
                    name_perf["wins_when_in_opening_hand"] += 0.5
                else:
                    card_perf["wins_when_in_opening_hand"] += 1
                    name_perf["wins_when_in_opening_hand"] += 1
            
            # Track drawn cards
            was_drawn = False
            for turn, cards in first_draw_history.items():
                if card_id in cards:
                    was_drawn = True
                    turn_key = str(turn)
                    
                    # Initialize turn tracking if needed
                    if "draw_performance_by_turn" not in card_perf:
                        card_perf["draw_performance_by_turn"] = {}
                    if turn_key not in card_perf["draw_performance_by_turn"]:
                        card_perf["draw_performance_by_turn"][turn_key] = {
                            "drawn": 0, "wins": 0, "draws": 0
                        }
                    
                    # Update turn tracking
                    card_perf["draw_performance_by_turn"][turn_key]["drawn"] += 1
                    if is_draw:
                        card_perf["draw_performance_by_turn"][turn_key]["draws"] += 1
                    else:
                        card_perf["draw_performance_by_turn"][turn_key]["wins"] += 1
                        
                    # Same for name-based tracking
                    if "draw_performance_by_turn" not in name_perf:
                        name_perf["draw_performance_by_turn"] = {}
                    if turn_key not in name_perf["draw_performance_by_turn"]:
                        name_perf["draw_performance_by_turn"][turn_key] = {
                            "drawn": 0, "wins": 0, "draws": 0
                        }
                    
                    name_perf["draw_performance_by_turn"][turn_key]["drawn"] += 1
                    if is_draw:
                        name_perf["draw_performance_by_turn"][turn_key]["draws"] += 1
                    else:
                        name_perf["draw_performance_by_turn"][turn_key]["wins"] += 1
            
            # Update drawn card statistics
            if was_drawn or in_opening_hand:
                card_perf["games_drawn"] += 1
                if is_draw:
                    card_perf["wins_when_drawn"] += 0.5  # Count draw as half a win
                else:
                    card_perf["wins_when_drawn"] += 1
                    
                name_perf["games_drawn"] += 1
                if is_draw:
                    name_perf["wins_when_drawn"] += 0.5
                else:
                    name_perf["wins_when_drawn"] += 1
            else:
                card_perf["games_not_drawn"] += 1
                if is_draw:
                    card_perf["wins_when_not_drawn"] += 0.5
                else:
                    card_perf["wins_when_not_drawn"] += 1
                    
                name_perf["games_not_drawn"] += 1
                if is_draw:
                    name_perf["wins_when_not_drawn"] += 0.5
                else:
                    name_perf["wins_when_not_drawn"] += 1
            
            # Calculate win rates safely with draw consideration
            # Overall win rate
            games_played = max(1, card_perf["games_played"])
            card_perf["win_rate"] = (card_perf["wins"] + 0.5 * card_perf["draws"]) / games_played
            
            name_games_played = max(1, name_perf["games_played"])
            name_perf["win_rate"] = (name_perf["wins"] + 0.5 * name_perf["draws"]) / name_games_played
            
            # Win rate when drawn
            games_drawn = max(1, card_perf["games_drawn"])
            card_perf["drawn_win_rate"] = card_perf["wins_when_drawn"] / games_drawn
            
            name_games_drawn = max(1, name_perf["games_drawn"])
            name_perf["drawn_win_rate"] = name_perf["wins_when_drawn"] / name_games_drawn
            
            # Win rate when not drawn
            games_not_drawn = max(1, card_perf["games_not_drawn"])
            card_perf["not_drawn_win_rate"] = card_perf["wins_when_not_drawn"] / games_not_drawn
            
            name_games_not_drawn = max(1, name_perf["games_not_drawn"])
            name_perf["not_drawn_win_rate"] = name_perf["wins_when_not_drawn"] / name_games_not_drawn
            
            # Win rate when in opening hand
            opening_hand_games = max(1, card_perf["games_in_opening_hand"])
            card_perf["opening_hand_win_rate"] = card_perf["wins_when_in_opening_hand"] / opening_hand_games
            
            name_opening_hand_games = max(1, name_perf["games_in_opening_hand"])
            name_perf["opening_hand_win_rate"] = name_perf["wins_when_in_opening_hand"] / name_opening_hand_games
            
            # Calculate Improvement Factor (how much better is the win rate when drawn vs. not)
            # A value > 1 means the card improves your chances when drawn
            # A value < 1 means the card actually hurts your chances when drawn
            try:
                if card_perf["games_drawn"] > 0 and card_perf["games_not_drawn"] > 0:
                    improvement_factor = card_perf["drawn_win_rate"] / max(0.01, card_perf["not_drawn_win_rate"])
                    card_perf["improvement_factor"] = improvement_factor
                    
                if name_perf["games_drawn"] > 0 and name_perf["games_not_drawn"] > 0:
                    name_improvement_factor = name_perf["drawn_win_rate"] / max(0.01, name_perf["not_drawn_win_rate"])
                    name_perf["improvement_factor"] = name_improvement_factor
            except (ZeroDivisionError, TypeError):
                # Safety measure in case of division errors
                card_perf["improvement_factor"] = 1.0
                name_perf["improvement_factor"] = 1.0
                
            # Calculate performance rating (a normalized score from 0-1)
            try:
                # Base performance on improvement factor and win rate
                base_score = (card_perf["improvement_factor"] - 0.5) * 2  # Scale to roughly -1 to 1
                win_rate_boost = card_perf["win_rate"] - 0.5  # How much better than 50%
                
                # Combine factors and normalize to 0-1 range
                performance_rating = max(0.0, min(1.0, 0.5 + 0.25 * base_score + 0.25 * win_rate_boost))
                card_perf["performance_rating"] = performance_rating
                
                # Same for name-based
                name_base_score = (name_perf["improvement_factor"] - 0.5) * 2
                name_win_rate_boost = name_perf["win_rate"] - 0.5
                name_performance_rating = max(0.0, min(1.0, 0.5 + 0.25 * name_base_score + 0.25 * name_win_rate_boost))
                name_perf["performance_rating"] = name_performance_rating
            except (KeyError, TypeError):
                # Default rating if calculation fails
                card_perf["performance_rating"] = 0.5
                name_perf["performance_rating"] = 0.5
                    
            # Also save card stats to separate card file
            self._save_individual_card_stats(card_name, {
                "name": card_name,
                "id": card_id,
                "wins": 0 if is_draw else 1,
                "losses": 0,
                "draws": 1 if is_draw else 0,
                "games_played": 1,
                "was_played": was_played,
                "was_drawn": was_drawn,
                "in_opening_hand": in_opening_hand,
                "usage_count": 1 if was_played else 0,
                "win_rate": 0.5 if is_draw else 1.0,
                "game_stage": game_stage.value,
                "game_state": game_state.value,
                "deck_archetype": winner_stats.get("archetype", "unknown"),
                "play_position": play_order.get("first_player") == "winner"
            })

        # Process cards for loser deck (similar logic as above)
        second_played = cards_played.get(1, [])
        second_opening_hand = opening_hands.get("loser", [])
        second_draw_history = draw_history.get("loser", {})
        
        # Track when cards were played (by turn)
        second_play_history = {}
        for card_id in second_played:
            # Estimate play turn based on CMC (simplified)
            card = self.card_db.get(card_id)
            if card and hasattr(card, 'cmc'):
                estimated_turn = max(1, min(int(card.cmc), 20))
                if estimated_turn not in second_play_history:
                    second_play_history[estimated_turn] = []
                second_play_history[estimated_turn].append(card_id)
        
        # Update card performances for loser deck
        for card_entry in loser_composition:
            card_id = card_entry["id"]
            card_name = card_entry["name"]
            
            # Initialize card performance if needed for ID-based tracking
            if "card_performance" not in loser_stats:
                loser_stats["card_performance"] = {}
                
            if str(card_id) not in loser_stats["card_performance"]:
                loser_stats["card_performance"][str(card_id)] = {
                    "name": card_name,
                    "games_played": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "usage_count": 0,
                    "win_rate": 0,
                    "games_drawn": 0,
                    "wins_when_drawn": 0,
                    "games_not_drawn": 0,
                    "wins_when_not_drawn": 0,
                    "games_in_opening_hand": 0,
                    "wins_when_in_opening_hand": 0,
                    "performance_by_turn": {},
                    "performance_by_stage": {
                        "early": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "mid": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "late": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "performance_by_position": {
                        "ahead": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "parity": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "behind": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_order_performance": {
                        "play_first": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "play_second": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_curve_stats": {
                        "on_curve": {"games": 0, "wins": 0, "draws": 0},
                        "under_curve": {"games": 0, "wins": 0, "draws": 0},
                        "over_curve": {"games": 0, "wins": 0, "draws": 0}
                    }
                }
                    
            # Initialize card performance by name if needed
            if "card_performance_by_name" not in loser_stats:
                loser_stats["card_performance_by_name"] = {}
                
            if card_name not in loser_stats["card_performance_by_name"]:
                loser_stats["card_performance_by_name"][card_name] = {
                    "name": card_name,
                    "games_played": 0,
                    "wins": 0,
                    "losses": 0,
                    "draws": 0,
                    "usage_count": 0,
                    "win_rate": 0,
                    "games_drawn": 0,
                    "wins_when_drawn": 0,
                    "games_not_drawn": 0,
                    "wins_when_not_drawn": 0,
                    "games_in_opening_hand": 0,
                    "wins_when_in_opening_hand": 0,
                    "performance_by_turn": {},
                    "performance_by_stage": {
                        "early": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "mid": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "late": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "performance_by_position": {
                        "ahead": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "parity": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "behind": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_order_performance": {
                        "play_first": {"wins": 0, "losses": 0, "draws": 0, "played": 0},
                        "play_second": {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    },
                    "play_curve_stats": {
                        "on_curve": {"games": 0, "wins": 0, "draws": 0},
                        "under_curve": {"games": 0, "wins": 0, "draws": 0},
                        "over_curve": {"games": 0, "wins": 0, "draws": 0}
                    }
                }
            
            # Get references to both card performance objects for more concise code
            card_perf = loser_stats["card_performance"][str(card_id)]
            name_perf = loser_stats["card_performance_by_name"][card_name]
            
            # Update basic game stats
            card_perf["games_played"] += 1
            name_perf["games_played"] += 1
            
            if is_draw:
                card_perf["draws"] += 1
                name_perf["draws"] += 1
            else:
                card_perf["losses"] += 1  # Loss for the loser deck
                name_perf["losses"] += 1  # Loss for the loser deck
            
            # Check if card was played
            was_played = card_id in second_played
            if was_played:
                card_perf["usage_count"] += 1
                name_perf["usage_count"] += 1
                
                # Track performance by game stage
                stage_key = game_stage.value
                if stage_key not in card_perf["performance_by_stage"]:
                    card_perf["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    
                card_perf["performance_by_stage"][stage_key]["played"] += 1
                if is_draw:
                    card_perf["performance_by_stage"][stage_key]["draws"] += 1
                else:
                    card_perf["performance_by_stage"][stage_key]["losses"] += 1  # Loss for loser
                    
                # Same for name-based tracking
                if stage_key not in name_perf["performance_by_stage"]:
                    name_perf["performance_by_stage"][stage_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                    
                name_perf["performance_by_stage"][stage_key]["played"] += 1
                if is_draw:
                    name_perf["performance_by_stage"][stage_key]["draws"] += 1
                else:
                    name_perf["performance_by_stage"][stage_key]["losses"] += 1  # Loss for loser
                
                # Track performance by game state/position
                # Invert game state for loser's perspective
                position_key = GameState.BEHIND.value
                if game_state == GameState.BEHIND:
                    position_key = GameState.AHEAD.value
                elif game_state == GameState.PARITY:
                    position_key = GameState.PARITY.value
                    
                if position_key not in card_perf["performance_by_position"]:
                    card_perf["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                card_perf["performance_by_position"][position_key]["played"] += 1
                if is_draw:
                    card_perf["performance_by_position"][position_key]["draws"] += 1
                else:
                    card_perf["performance_by_position"][position_key]["losses"] += 1  # Loss for loser
                
                # Same for name-based tracking
                if position_key not in name_perf["performance_by_position"]:
                    name_perf["performance_by_position"][position_key] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                name_perf["performance_by_position"][position_key]["played"] += 1
                if is_draw:
                    name_perf["performance_by_position"][position_key]["draws"] += 1
                else:
                    name_perf["performance_by_position"][position_key]["losses"] += 1  # Loss for loser
                
                # Track play order performance
                play_position = "play_first" if play_order.get("first_player") == "loser" else "play_second"
                if play_position not in card_perf["play_order_performance"]:
                    card_perf["play_order_performance"][play_position] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                card_perf["play_order_performance"][play_position]["played"] += 1
                if is_draw:
                    card_perf["play_order_performance"][play_position]["draws"] += 1
                else:
                    card_perf["play_order_performance"][play_position]["losses"] += 1  # Loss for loser
                    
                # Same for name-based tracking
                if play_position not in name_perf["play_order_performance"]:
                    name_perf["play_order_performance"][play_position] = {"wins": 0, "losses": 0, "draws": 0, "played": 0}
                
                name_perf["play_order_performance"][play_position]["played"] += 1
                if is_draw:
                    name_perf["play_order_performance"][play_position]["draws"] += 1
                else:
                    name_perf["play_order_performance"][play_position]["losses"] += 1  # Loss for loser
                    
                # Check if played on curve
                # First, find which turn the card was played
                for turn, cards in second_play_history.items():
                    if card_id in cards:
                        # Get card's CMC
                        card = self.card_db.get(card_id)
                        if card and hasattr(card, 'cmc'):
                            curve_status = ""
                            if turn == int(card.cmc):  # On curve
                                curve_status = "on_curve"
                            elif turn < int(card.cmc):  # Under curve (played early)
                                curve_status = "under_curve"
                            else:  # Over curve (played late)
                                curve_status = "over_curve"
                            
                            # Track curve performance
                            if curve_status:
                                # For ID-based tracking
                                card_perf["play_curve_stats"][curve_status]["games"] += 1
                                if is_draw:
                                    card_perf["play_curve_stats"][curve_status]["draws"] += 1
                                else:  # Loss for loser
                                    pass
                                    
                                # For name-based tracking
                                name_perf["play_curve_stats"][curve_status]["games"] += 1
                                if is_draw:
                                    name_perf["play_curve_stats"][curve_status]["draws"] += 1
                                else:  # Loss for loser
                                    pass
                        
                        # Record turn-by-turn performance
                        turn_key = str(turn)
                        if "performance_by_turn" not in card_perf:
                            card_perf["performance_by_turn"] = {}
                        if turn_key not in card_perf["performance_by_turn"]:
                            card_perf["performance_by_turn"][turn_key] = {"played": 0, "wins": 0, "draws": 0}
                            
                        card_perf["performance_by_turn"][turn_key]["played"] += 1
                        if is_draw:
                            card_perf["performance_by_turn"][turn_key]["draws"] += 1
                        # No wins to record for loser
                            
                        # For name-based tracking
                        if "performance_by_turn" not in name_perf:
                            name_perf["performance_by_turn"] = {}
                        if turn_key not in name_perf["performance_by_turn"]:
                            name_perf["performance_by_turn"][turn_key] = {"played": 0, "wins": 0, "draws": 0}
                            
                        name_perf["performance_by_turn"][turn_key]["played"] += 1
                        if is_draw:
                            name_perf["performance_by_turn"][turn_key]["draws"] += 1
                        # No wins to record for loser
            
            # Check if card was in opening hand
            in_opening_hand = card_id in second_opening_hand
            if in_opening_hand:
                card_perf["games_in_opening_hand"] += 1
                name_perf["games_in_opening_hand"] += 1
                
                if is_draw:
                    # Award half a win for draws
                    card_perf["wins_when_in_opening_hand"] += 0.5
                    name_perf["wins_when_in_opening_hand"] += 0.5
                # No wins to add for loser
            
            # Track drawn cards
            was_drawn = False
            for turn, cards in second_draw_history.items():
                if card_id in cards:
                    was_drawn = True
                    turn_key = str(turn)
                    
                    # Initialize turn tracking if needed
                    if "draw_performance_by_turn" not in card_perf:
                        card_perf["draw_performance_by_turn"] = {}
                    if turn_key not in card_perf["draw_performance_by_turn"]:
                        card_perf["draw_performance_by_turn"][turn_key] = {
                            "drawn": 0, "wins": 0, "draws": 0
                        }
                    
                    # Update turn tracking
                    card_perf["draw_performance_by_turn"][turn_key]["drawn"] += 1
                    if is_draw:
                        card_perf["draw_performance_by_turn"][turn_key]["draws"] += 1
                    # No wins to add for loser
                        
                    # Same for name-based tracking
                    if "draw_performance_by_turn" not in name_perf:
                        name_perf["draw_performance_by_turn"] = {}
                    if turn_key not in name_perf["draw_performance_by_turn"]:
                        name_perf["draw_performance_by_turn"][turn_key] = {
                            "drawn": 0, "wins": 0, "draws": 0
                        }
                    
                    name_perf["draw_performance_by_turn"][turn_key]["drawn"] += 1
                    if is_draw:
                        name_perf["draw_performance_by_turn"][turn_key]["draws"] += 1
                    # No wins to add for loser
            
            # Update drawn card statistics
            if was_drawn or in_opening_hand:
                card_perf["games_drawn"] += 1
                if is_draw:
                    card_perf["wins_when_drawn"] += 0.5  # Count draw as half a win
                # No wins to add for loser
                    
                name_perf["games_drawn"] += 1
                if is_draw:
                    name_perf["wins_when_drawn"] += 0.5
                # No wins to add for loser
            else:
                card_perf["games_not_drawn"] += 1
                if is_draw:
                    card_perf["wins_when_not_drawn"] += 0.5
                # No wins to add for loser
                    
                name_perf["games_not_drawn"] += 1
                if is_draw:
                    name_perf["wins_when_not_drawn"] += 0.5
                # No wins to add for loser
            
            # Calculate win rates safely with draw consideration
            # Overall win rate
            games_played = max(1, card_perf["games_played"])
            card_perf["win_rate"] = (card_perf["wins"] + 0.5 * card_perf["draws"]) / games_played
            
            name_games_played = max(1, name_perf["games_played"])
            name_perf["win_rate"] = (name_perf["wins"] + 0.5 * name_perf["draws"]) / name_games_played
            
            # Win rate when drawn
            games_drawn = max(1, card_perf["games_drawn"])
            card_perf["drawn_win_rate"] = card_perf["wins_when_drawn"] / games_drawn
            
            name_games_drawn = max(1, name_perf["games_drawn"])
            name_perf["drawn_win_rate"] = name_perf["wins_when_drawn"] / name_games_drawn
            
            # Win rate when not drawn
            games_not_drawn = max(1, card_perf["games_not_drawn"])
            card_perf["not_drawn_win_rate"] = card_perf["wins_when_not_drawn"] / games_not_drawn
            
            name_games_not_drawn = max(1, name_perf["games_not_drawn"])
            name_perf["not_drawn_win_rate"] = name_perf["wins_when_not_drawn"] / name_games_not_drawn
            
            # Win rate when in opening hand
            opening_hand_games = max(1, card_perf["games_in_opening_hand"])
            card_perf["opening_hand_win_rate"] = card_perf["wins_when_in_opening_hand"] / opening_hand_games
            
            name_opening_hand_games = max(1, name_perf["games_in_opening_hand"])
            name_perf["opening_hand_win_rate"] = name_perf["wins_when_in_opening_hand"] / name_opening_hand_games
            
            # Calculate Improvement Factor (how much better is the win rate when drawn vs. not)
            try:
                if card_perf["games_drawn"] > 0 and card_perf["games_not_drawn"] > 0:
                    improvement_factor = card_perf["drawn_win_rate"] / max(0.01, card_perf["not_drawn_win_rate"])
                    card_perf["improvement_factor"] = improvement_factor
                    
                if name_perf["games_drawn"] > 0 and name_perf["games_not_drawn"] > 0:
                    name_improvement_factor = name_perf["drawn_win_rate"] / max(0.01, name_perf["not_drawn_win_rate"])
                    name_perf["improvement_factor"] = name_improvement_factor
            except (ZeroDivisionError, TypeError):
                # Safety measure in case of division errors
                card_perf["improvement_factor"] = 1.0
                name_perf["improvement_factor"] = 1.0
                
            # Calculate performance rating (a normalized score from 0-1)
            try:
                # Base performance on improvement factor and win rate
                base_score = (card_perf["improvement_factor"] - 0.5) * 2  # Scale to roughly -1 to 1
                win_rate_boost = card_perf["win_rate"] - 0.5  # How much better than 50%
                
                # Combine factors and normalize to 0-1 range
                performance_rating = max(0.0, min(1.0, 0.5 + 0.25 * base_score + 0.25 * win_rate_boost))
                card_perf["performance_rating"] = performance_rating
                
                # Same for name-based
                name_base_score = (name_perf["improvement_factor"] - 0.5) * 2
                name_win_rate_boost = name_perf["win_rate"] - 0.5
                name_performance_rating = max(0.0, min(1.0, 0.5 + 0.25 * name_base_score + 0.25 * name_win_rate_boost))
                name_perf["performance_rating"] = name_performance_rating
            except (KeyError, TypeError):
                # Default rating if calculation fails
                card_perf["performance_rating"] = 0.5
                name_perf["performance_rating"] = 0.5
            
            # Also save card stats to separate card file
            self._save_individual_card_stats(card_name, {
                "name": card_name,
                "id": card_id,
                "wins": 0,
                "losses": 0 if is_draw else 1,
                "draws": 1 if is_draw else 0,
                "games_played": 1,
                "was_played": was_played,
                "was_drawn": was_drawn,
                "in_opening_hand": in_opening_hand,
                "usage_count": 1 if was_played else 0,
                "win_rate": 0.5 if is_draw else 0.0,
                "game_stage": game_stage.value,
                "game_state": game_state.value,
                "deck_archetype": loser_stats.get("archetype", "unknown"),
                "play_position": play_order.get("first_player") == "loser"
            })
        
        # Save updated stats
        if not self.update_deck_stats(winner_deck_id, winner_stats):
            success = False
                
        if not self.update_deck_stats(loser_deck_id, loser_stats):
            success = False
                
        return success
        
    def _save_individual_card_stats(self, card_name: str, stats_update: Dict) -> bool:
        """
        Save statistics for an individual card to its own file.
        This allows better tracking of card performance across all decks.
        
        Args:
            card_name: Name of the card
            stats_update: New statistics to add
            
        Returns:
            bool: Whether the save was successful
        """
        if not card_name:
            return False

        # Create file path
        card_file = f"cards/{self._sanitize_filename(card_name)}.json"

        # Get existing stats or initialize new ones
        card_stats = self.load(card_file)
        if not card_stats:
            card_stats = {
                "name": card_name,
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "usage_count": 0,
                "win_rate": 0,
                "archetypes": {},
                "by_game_stage": {
                    "early": {"games": 0, "wins": 0, "draws": 0},
                    "mid": {"games": 0, "wins": 0, "draws": 0},
                    "late": {"games": 0, "wins": 0, "draws": 0}
                },
                "by_game_state": {
                    "ahead": {"games": 0, "wins": 0, "draws": 0},
                    "parity": {"games": 0, "wins": 0, "draws": 0},
                    "behind": {"games": 0, "wins": 0, "draws": 0}
                }
            }
        else:
            # For older card files that might be missing these keys, initialize them.
            if "by_game_stage" not in card_stats:
                card_stats["by_game_stage"] = {
                    "early": {"games": 0, "wins": 0, "draws": 0},
                    "mid": {"games": 0, "wins": 0, "draws": 0},
                    "late": {"games": 0, "wins": 0, "draws": 0}
                }
            if "by_game_state" not in card_stats:
                card_stats["by_game_state"] = {
                    "ahead": {"games": 0, "wins": 0, "draws": 0},
                    "parity": {"games": 0, "wins": 0, "draws": 0},
                    "behind": {"games": 0, "wins": 0, "draws": 0}
                }
            if "archetypes" not in card_stats:
                card_stats["archetypes"] = {}
            
            # Add draws field if it doesn't exist
            if "draws" not in card_stats:
                card_stats["draws"] = 0
                
            # Add draws to game stages if missing
            for stage in card_stats["by_game_stage"]:
                if "draws" not in card_stats["by_game_stage"][stage]:
                    card_stats["by_game_stage"][stage]["draws"] = 0
                    
            # Add draws to game states if missing
            for state in card_stats["by_game_state"]:
                if "draws" not in card_stats["by_game_state"][state]:
                    card_stats["by_game_state"][state]["draws"] = 0

        # Update basic stats
        card_stats["games_played"] += 1
        card_stats["wins"] += stats_update.get("wins", 0)
        card_stats["losses"] += stats_update.get("losses", 0)
        card_stats["draws"] += stats_update.get("draws", 0)

        if stats_update.get("was_played", False):
            card_stats["usage_count"] += 1

        # Update win rate (count draws as 0.5 wins)
        if card_stats["games_played"] > 0:
            card_stats["win_rate"] = (card_stats["wins"] + 0.5 * card_stats["draws"]) / card_stats["games_played"]

        # Update archetype stats
        deck_archetype = stats_update.get("deck_archetype", "unknown")
        if deck_archetype not in card_stats["archetypes"]:
            card_stats["archetypes"][deck_archetype] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["archetypes"][deck_archetype]["games"] += 1
        card_stats["archetypes"][deck_archetype]["wins"] += stats_update.get("wins", 0)
        card_stats["archetypes"][deck_archetype]["draws"] += stats_update.get("draws", 0)

        # Update game stage stats
        game_stage = stats_update.get("game_stage", "mid")
        if game_stage not in card_stats["by_game_stage"]:
            card_stats["by_game_stage"][game_stage] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["by_game_stage"][game_stage]["games"] += 1
        card_stats["by_game_stage"][game_stage]["wins"] += stats_update.get("wins", 0)
        card_stats["by_game_stage"][game_stage]["draws"] += stats_update.get("draws", 0)

        # Update game state stats
        game_state = stats_update.get("game_state", "parity")
        if game_state not in card_stats["by_game_state"]:
            card_stats["by_game_state"][game_state] = {"games": 0, "wins": 0, "draws": 0}

        card_stats["by_game_state"][game_state]["games"] += 1
        card_stats["by_game_state"][game_state]["wins"] += stats_update.get("wins", 0)
        card_stats["by_game_state"][game_state]["draws"] += stats_update.get("draws", 0)

        # Save updated stats
        return self.save(card_file, card_stats)
    
    # === Analysis and Recommendations ===
    
    def get_deck_analysis(self, deck: List[int]) -> Dict:
        """
        Get comprehensive analysis for a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with deck analysis information
        """
        # First try to find this deck in the database
        deck_id = self.get_deck_fingerprint(deck)
        stats = self.get_deck_stats(deck_id)
        
        # If not found, find similar decks
        similar_decks = []
        all_decks = self._get_all_deck_keys()
        
        for other_id in all_decks:
            other_stats = self.get_deck_stats(other_id)
            if other_stats and "card_list" in other_stats:
                other_deck = [card["id"] for card in other_stats["card_list"]]
                similarity = self.calculate_deck_similarity(deck, other_deck)
                
                if similarity >= 0.7:  # 70% similarity threshold
                    similar_decks.append((other_id, similarity, other_stats))
        
        # Sort similar decks by similarity
        similar_decks.sort(key=lambda x: x[1], reverse=True)
        
        # Generate analysis results
        result = {
            "deck_id": deck_id,
            "deck_size": len(deck),
            "archetype": self.identify_archetype(deck),
            "exact_match_found": stats is not None,
            "similar_decks": [
                {
                    "id": other_id,
                    "similarity": similarity,
                    "name": other_stats["name"],
                    "win_rate": other_stats.get("win_rate", 0),
                    "games_played": other_stats.get("games", 0)
                }
                for other_id, similarity, other_stats in similar_decks[:5]  # Top 5 similar decks
            ],
            "card_analysis": [],
            "synergy_analysis": self._analyze_deck_synergy(deck),
            "meta_position": self._analyze_meta_position(deck)
        }
        
        # Add card analysis
        for card_id in set(deck):
            count = deck.count(card_id)
            
            # Get card info
            card_name = self._get_card_name(card_id)
            
            # Calculate card metrics
            card_metrics = self._get_card_metrics(card_id)
            
            result["card_analysis"].append({
                "id": card_id,
                "name": card_name or f"Card {card_id}",
                "count": count,
                "metrics": card_metrics
            })
        
        return result
    
    def _analyze_deck_synergy(self, deck: List[int]) -> Dict:
        """
        Analyze synergies within a deck.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with synergy analysis
        """
        # Calculate overall synergy score
        overall_score = self.calculate_deck_synergy_score(deck)
        
        # Calculate synergy matrix
        synergy_matrix = self.get_deck_synergy_matrix(deck)
        
        # Find highest synergy pairs
        synergy_pairs = []
        for (card1_id, card2_id), score in synergy_matrix.items():
            # Get card names
            card1_name = self._get_card_name(card1_id)
            card2_name = self._get_card_name(card2_id)
                    
            synergy_pairs.append({
                "card1_id": card1_id,
                "card1_name": card1_name or f"Card {card1_id}",
                "card2_id": card2_id,
                "card2_name": card2_name or f"Card {card2_id}",
                "synergy_score": score
            })
            
        # Sort by synergy score (highest first)
        synergy_pairs.sort(key=lambda x: x["synergy_score"], reverse=True)
        
        # Find low-synergy cards
        card_avg_synergies = {}
        for card_id in set(deck):
            synergies = []
            
            for other_id in set(deck):
                if card_id != other_id:
                    key = (min(card_id, other_id), max(card_id, other_id))
                    if key in synergy_matrix:
                        synergies.append(synergy_matrix[key])
                        
            if synergies:
                card_avg_synergies[card_id] = sum(synergies) / len(synergies)
        
        # Find cards with low average synergy
        low_synergy_cards = []
        for card_id, avg_synergy in card_avg_synergies.items():
            if avg_synergy < 0.3:  # Threshold for low synergy
                card_name = self._get_card_name(card_id)
                        
                low_synergy_cards.append({
                    "id": card_id,
                    "name": card_name or f"Card {card_id}",
                    "avg_synergy": avg_synergy
                })
                
        # Sort by synergy (lowest first)
        low_synergy_cards.sort(key=lambda x: x["avg_synergy"])
        
        return {
            "overall_score": overall_score,
            "top_synergy_pairs": synergy_pairs[:10],  # Top 10 pairs
            "low_synergy_cards": low_synergy_cards[:5]  # 5 lowest synergy cards
        }
    
    def _analyze_meta_position(self, deck: List[int]) -> Dict:
        """
        Analyze a deck's position in the current metagame.
        
        Args:
            deck: List of card IDs in the deck
            
        Returns:
            Dictionary with meta position analysis
        """
        # Get deck archetype
        archetype = self.identify_archetype(deck)
        
        # Get meta snapshot
        meta_snapshot = self.get_meta_snapshot()
        
        # Check archetype position
        archetype_position = {}
        meta_data = self._load_meta_data()
        if archetype in meta_data["archetypes"]:
            arch_data = meta_data["archetypes"][archetype]
            
            # Calculate percentile rank among archetypes
            all_win_rates = [
                data["win_rate"] for name, data in meta_data["archetypes"].items()
                if data["games"] >= 10  # Minimum sample size
            ]
            
            if all_win_rates:
                all_win_rates.sort()
                percentile = sum(1 for wr in all_win_rates if wr <= arch_data["win_rate"]) / len(all_win_rates)
                
                archetype_position = {
                    "games_played": arch_data["games"],
                    "win_rate": arch_data["win_rate"],
                    "percentile": percentile,
                    "meta_share": arch_data["games"] / max(1, meta_snapshot["total_games"])
                }
                
        # Get archetype matchups
        matchups = self.get_archetype_matchups(archetype)
        
        # Calculate deck vs. meta performance
        meta_performance = {}
        
        # Get the top meta archetypes
        top_archetypes = self.get_top_archetypes()
        
        # Calculate expected performance against the meta
        if top_archetypes:
            meta_win_rate = 0
            weight_sum = 0
            
            for arch_data in top_archetypes:
                arch_name = arch_data["archetype"]
                arch_meta_share = arch_data["games"] / max(1, meta_snapshot["total_games"])
                
                matchup_win_rate = matchups.get(arch_name, 0.5)  # Default to 50% if unknown
                
                meta_win_rate += matchup_win_rate * arch_meta_share
                weight_sum += arch_meta_share
                
            if weight_sum > 0:
                meta_performance["expected_win_rate"] = meta_win_rate / weight_sum
                
            # Evaluate meta position
            position_score = 0
            if "expected_win_rate" in meta_performance:
                position_score = (meta_performance["expected_win_rate"] - 0.5) * 2  # -1 to 1 scale
                
            if position_score >= 0.3:
                meta_performance["evaluation"] = "Well positioned in the current meta"
            elif position_score >= 0:
                meta_performance["evaluation"] = "Adequately positioned in the current meta"
            elif position_score >= -0.3:
                meta_performance["evaluation"] = "Weakly positioned in the current meta"
            else:
                meta_performance["evaluation"] = "Poorly positioned in the current meta"
        
        return {
            "archetype": archetype,
            "archetype_position": archetype_position,
            "top_matchups": sorted(matchups.items(), key=lambda x: x[1], reverse=True)[:3],
            "worst_matchups": sorted(matchups.items(), key=lambda x: x[1])[:3],
            "meta_performance": meta_performance
        }
    
    def _get_card_metrics(self, card_id: int) -> Dict:
        """Get performance metrics for a card"""
        # Get card name
        card_name = self._get_card_name(card_id)
        
        # Check individual card stats first
        if card_name:
            card_file = f"cards/{self._sanitize_filename(card_name)}.json"
            card_stats = self.load(card_file)
            if card_stats:
                return {
                    "win_rate": card_stats.get("win_rate", 0),
                    "games_played": card_stats.get("games_played", 0),
                    "usage_rate": card_stats.get("usage_count", 0) / max(1, card_stats.get("games_played", 0)),
                    "best_archetype": max(card_stats.get("archetypes", {}).items(), 
                                       key=lambda x: x[1].get("wins", 0) / max(1, x[1].get("games", 1)))[0] 
                                       if card_stats.get("archetypes") else None,
                    "best_game_stage": max(card_stats.get("by_game_stage", {}).items(),
                                        key=lambda x: x[1].get("wins", 0) / max(1, x[1].get("games", 1)))[0]
                                        if card_stats.get("by_game_stage") else None
                }
        
        # Fall back to global stats
        stats = self.get_card_stats(card_id)
        
        # Get card data from database
        card = self.card_db.get(card_id)
        
        metrics = {
            "win_rate": stats.get("win_rate", 0),
            "games_played": stats.get("games_played", 0),
            "usage_rate": stats.get("usage_count", 0) / max(1, stats.get("games_played", 0)),
            "type": getattr(card, 'type_line', "Unknown") if card else "Unknown",
            "cmc": getattr(card, 'cmc', 0) if card else 0,
            "performance_rating": getattr(card, 'performance_rating', 0.5) if card else 0.5
        }
        
        # Calculate positional performance
        if stats.get("performance_by_position"):
            pos_perf = stats["performance_by_position"]
            metrics["ahead_win_rate"] = (
                pos_perf["ahead"]["wins"] / max(1, pos_perf["ahead"]["wins"] + pos_perf["ahead"]["losses"])
            )
            metrics["behind_win_rate"] = (
                pos_perf["behind"]["wins"] / max(1, pos_perf["behind"]["wins"] + pos_perf["behind"]["losses"])
            )
            metrics["comeback_potential"] = metrics["behind_win_rate"] * 2  # Scale up for emphasis
        
        return metrics
    
    # === Utility Methods ===
    
    def _get_card_name(self, card_id: int) -> Optional[str]:
        """Get the name of a card from its ID"""
        # Check cache first
        if card_id in self.card_id_to_name:
            return self.card_id_to_name[card_id]
            
        # Try to get from card database
        if card_id in self.card_db:
            card = self.card_db[card_id]
            if hasattr(card, 'name'):
                self.card_id_to_name[card_id] = card.name
                return card.name
        
        return None
    
    def _sanitize_filename(self, name: str) -> str:
        """
        Sanitize a string for use as a filename.
        Removes illegal characters and shortens if necessary.
        """
        # Remove illegal filename characters
        sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
        # Replace spaces with underscores
        sanitized = sanitized.replace(' ', '_')
        # Limit length
        max_length = 50  # Maximum reasonable filename length
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]
        return sanitized
    
    def _get_all_deck_files(self) -> List[str]:
        """Get all deck JSON files from storage"""
        deck_files = []
        deck_dir = os.path.join(self.base_path, "decks")
        
        if os.path.exists(deck_dir) and os.path.isdir(deck_dir):
            for file_name in os.listdir(deck_dir):
                if file_name.endswith(".json") or file_name.endswith(".json.gz"):
                    deck_files.append(f"decks/{file_name}")
                    
        return deck_files
    
    def _get_all_deck_keys(self) -> List[str]:
        """Get all deck keys in the database"""
        deck_dir = os.path.join(self.base_path, "decks")
        if os.path.exists(deck_dir) and os.path.isdir(deck_dir):
            return [
                os.path.splitext(file)[0] for file in os.listdir(deck_dir)
                if file.endswith(".json") or file.endswith(".json.gz")
            ]
        return []
    
    def _get_all_deck_data(self) -> Dict[str, Dict]:
        """Get all deck data from storage"""
        data = {}
        keys = self._get_all_deck_keys()
        
        for key in keys:
            stats = self.get_deck_stats(key)
            if stats:
                data[key] = stats
                
        return data
    
    def _generate_deck_filename(self, stats: Dict, deck_key: str, original_filename: str = None) -> str:
        # If a filename has already been generated, reuse it.
        if "filename" in stats:
            return stats["filename"]

        # Use the provided original filename, or fallback to the deck name.
        if original_filename:
            base_name = self._sanitize_deck_name(os.path.splitext(original_filename)[0])
        elif stats and "name" in stats:
            base_name = self._sanitize_deck_name(stats["name"])
        else:
            base_name = deck_key[:20]

        # Include the predicted archetype in the filename.
        archetype = stats.get("archetype", "Unknown")
        
        # Construct a deterministic filename using the base name, archetype, and deck_key.
        filename = f"{base_name}_{archetype}_{deck_key}.json"
        
        # Lock in the filename by saving it in stats.
        stats["filename"] = filename
        return filename

    # === Batch Update Methods ===
    
    async def save_batch_updates(self) -> bool:
        """Save all batched updates to storage with enhanced error handling and locking"""
        batch = None
        try:
            with self.batch_lock:
                # Copy and clear batch to minimize lock time
                batch = self.batch_updates.copy()
                self.batch_updates.clear()
        except Exception as e:
            logging.error(f"Error acquiring batch lock: {e}")
            return False
        
        if not batch:
            return True  # Nothing to save
        
        success = True
        for deck_key, stats in batch.items():
            try:
                # Validate before saving
                valid, errors = self.validate_deck_stats(stats)
                if not valid:
                    logging.warning(f"Invalid deck stats for {deck_key}: {errors}")
                    stats = self.repair_deck_stats(stats)
                
                # Check if original filename was stored
                original_filename = stats.get("name")
                
                # Create user-friendly filename
                filename = self._generate_deck_filename(stats, deck_key, original_filename)
                
                # Asynchronous save
                save_result = await self.save_async(f"decks/{filename}", stats)
                
                if not save_result:
                    logging.error(f"Failed to save stats for deck {deck_key}")
                    success = False
            
            except Exception as e:
                logging.error(f"Error saving deck stats for {deck_key}: {str(e)}")
                success = False
        
        return success
    
    def save_updates_sync(self):
        """
        Synchronous method to save all pending updates.
        
        This method provides a way to save batch updates when an async context 
        is not available or when a simple synchronous method is preferred.
        
        Returns:
            bool: True if updates were saved successfully, False otherwise
        """
        try:
            # Use run_until_complete to run the async method
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.save_batch_updates())
        except RuntimeError:
            # If no event loop exists, create a new one
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(self.save_batch_updates())
            except Exception as e:
                logging.error(f"Error saving updates synchronously (no event loop): {e}")
                return False
        except Exception as e:
            logging.error(f"Error saving updates synchronously: {e}")
            return False
    
    async def save_all_pending_updates(self) -> bool:
        """Save all pending updates to storage"""
        return await self.save_batch_updates()
    
    


class DeckStatsCollector:
    """
    A simplified interface to DeckStatsTracker for collecting stats only.
    This class is intended to be used in environments where only recording
    game results is needed, without the full analysis functionality.
    """
    
    def __init__(self, storage_path: str = "./deck_stats", card_db: Dict = None):
        self.tracker = DeckStatsTracker(storage_path, card_db)
    
    def save_pending_updates(self) -> bool:
        """Save any pending updates to disk"""
        return self.tracker.save_updates_sync()