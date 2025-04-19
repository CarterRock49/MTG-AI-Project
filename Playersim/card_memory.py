# card_memory.py
import os
import json
import logging
import time
import pickle
import numpy as np
from collections import defaultdict
import gzip
import threading
import re
from typing import Dict, List, Tuple, Any, Optional, Union


class CardMemory:
    """
    Comprehensive memory system for tracking card performance across all games.
    Maintains detailed statistics on every card the AI has encountered.
    """
    
    def __init__(self, storage_path: str = "./card_memory", use_compression: bool = True):
        """
        Initialize card memory system.

        Args:
            storage_path: Directory to store card memory files
            use_compression: Whether to compress stored data
        """
        self.storage_path = storage_path
        self.use_compression = use_compression
        self.card_data = {}  # In-memory cache
        self.card_name_to_id = {}  # Mapping of card names to IDs
        self.card_id_to_name = {}  # Mapping of card IDs to names
        self.memory_lock = threading.RLock()  # Thread-safe operations
        self.cache = {}  # Simple memory cache
        self.cache_ttl = 300  # Cache TTL in seconds
        self.last_cache_cleanup = time.time()
        self.updates_since_save = 0
        self.save_frequency = 50  # Save after every 50 updates by default
        # Create storage directory if it doesn't exist
        os.makedirs(self.storage_path, exist_ok=True)

        # Load existing card data
        self.load_all_card_data()
        
    def clear_temporary_data(self) -> None:
        """
        Clears any temporary or game-specific data from the memory.
        Currently, this clears the internal cache.
        """
        with self.memory_lock:
            self.cache_clear() # Call existing cache clear method
            self.last_cache_cleanup = time.time() # Reset cleanup timer
            logging.info("Cleared temporary data (cache) from CardMemory.")

    def cache_clear(self) -> None:
        """Clear the entire cache"""
        with self.memory_lock:
            self.cache.clear()
            logging.debug("CardMemory cache cleared.")

        
    def cache_get(self, key):
        """Get a value from the cache if it exists and is not expired."""
        with self.memory_lock:
            current_time = time.time()
            
            # Clean up cache periodically
            if current_time - self.last_cache_cleanup > 60:  # Every minute
                self._cleanup_cache()
                self.last_cache_cleanup = current_time
            
            # Check if key exists and is not expired
            if key in self.cache:
                timestamp, value = self.cache[key]
                if current_time - timestamp < self.cache_ttl:
                    return value
            
            return None
        
    def _cleanup_cache(self):
        """Remove expired entries from the cache."""
        current_time = time.time()
        expired_keys = [
            key for key, (timestamp, _) in self.cache.items()
            if current_time - timestamp >= self.cache_ttl
        ]
        
        # Remove expired entries
        for key in expired_keys:
            del self.cache[key]
        
        # Limit cache size to prevent memory issues
        if len(self.cache) > 10000:
            # Keep most recent entries
            sorted_keys = sorted(
                self.cache.keys(),
                key=lambda k: self.cache[k][0],
                reverse=True
            )
            # Keep top 8000 entries
            keys_to_remove = sorted_keys[8000:]
            for key in keys_to_remove:
                del self.cache[key]

    def cache_set(self, key, value):
        """Set a value in the cache with current timestamp."""
        with self.memory_lock:
            self.cache[key] = (time.time(), value)
    
    def load_all_card_data(self) -> None:
        """Load all card data from storage into memory."""
        with self.memory_lock:
            try:
                cards_file = os.path.join(self.storage_path, "all_cards.json")
                compressed_file = cards_file + ".gz"
                
                # Try compressed file first
                if self.use_compression and os.path.exists(compressed_file):
                    with gzip.open(compressed_file, 'rt', encoding='utf-8') as f:
                        data = json.load(f)
                        self.card_data = data.get('cards', {})
                        self.card_name_to_id = data.get('name_to_id', {})
                        self.card_id_to_name = data.get('id_to_name', {})
                        logging.info(f"Loaded data for {len(self.card_data)} cards from compressed file")
                # Try uncompressed file
                elif os.path.exists(cards_file):
                    with open(cards_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.card_data = data.get('cards', {})
                        self.card_name_to_id = data.get('name_to_id', {})
                        self.card_id_to_name = data.get('id_to_name', {})
                        logging.info(f"Loaded data for {len(self.card_data)} cards from uncompressed file")
                else:
                    logging.info("No existing card memory file found, starting with empty memory")
            except Exception as e:
                logging.error(f"Error loading card data: {e}")
                # Initialize empty data structures if loading fails
                self.card_data = {}
                self.card_name_to_id = {}
                self.card_id_to_name = {}
    
    def save_all_card_data(self) -> bool:
        """Save all card data to storage."""
        with self.memory_lock:
            try:
                data = {
                    'cards': self.card_data,
                    'name_to_id': self.card_name_to_id,
                    'id_to_name': self.card_id_to_name,
                    'last_updated': time.time()
                }
                
                cards_file = os.path.join(self.storage_path, "all_cards.json")
                
                if self.use_compression:
                    with gzip.open(cards_file + ".gz", 'wt', encoding='utf-8') as f:
                        json.dump(data, f)
                else:
                    with open(cards_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f)
                
                logging.info(f"Saved data for {len(self.card_data)} cards")
                return True
            except Exception as e:
                logging.error(f"Error saving card data: {e}")
                return False
    
    def update_card_mapping(self, card_id: int, card_name: str) -> None:
        """Update mapping between card IDs and names."""
        with self.memory_lock:
            self.card_id_to_name[str(card_id)] = card_name
            self.card_name_to_id[card_name] = str(card_id)
    
    def get_card_stats(self, card_id: Union[int, str]) -> Dict:
        """
        Get statistics for a specific card.
        
        Args:
            card_id: The ID of the card to get statistics for
            
        Returns:
            Dict: Card statistics or empty dict if not found
        """
        card_key = str(card_id)
        with self.memory_lock:
            # Try to find by ID first
            if card_key in self.card_data:
                return self.card_data[card_key]
            
            # Try to find by name if it's a string that's not a numeric ID
            if not card_key.isdigit() and card_key in self.card_name_to_id:
                mapped_id = self.card_name_to_id[card_key]
                if mapped_id in self.card_data:
                    return self.card_data[mapped_id]
            
            # Card not found
            return {}
    
    def register_card(self, card_id: int, card_name: str, card_data: Dict = None) -> None:
        """
        Register a new card or update existing card data.
        
        Args:
            card_id: The ID of the card
            card_name: The name of the card
            card_data: Optional additional card data (mana cost, types, etc.)
        """
        card_key = str(card_id)
        with self.memory_lock:
            # Update ID-name mapping
            self.update_card_mapping(card_id, card_name)
            
            # Create or update card entry
            if card_key not in self.card_data:
                self.card_data[card_key] = {
                    'id': card_id,
                    'name': card_name,
                    'first_seen': time.time(),
                    'games_played': 0,
                    'wins': 0,
                    'losses': 0,
                    'draws': 0,
                    'win_rate': 0.0,
                    'times_drawn': 0,
                    'times_played': 0,
                    'turn_played': {},
                    'performance_by_turn': {},
                    'in_opening_hand': 0,
                    'wins_in_opening_hand': 0,
                    'mana_curve_performance': {
                        'on_curve': {'played': 0, 'wins': 0},
                        'below_curve': {'played': 0, 'wins': 0},
                        'above_curve': {'played': 0, 'wins': 0}
                    },
                    'archetype_performance': {},
                    'synergy_partners': {},
                    'effectiveness_rating': 0.5,  # Default neutral rating
                    'performance_trend': [],
                    'meta_position': {}
                }
            
            # Update card data if provided
            if card_data:
                for key, value in card_data.items():
                    if key not in ['id', 'name', 'first_seen']:  # Don't overwrite core fields
                        self.card_data[card_key][key] = value
    
    def update_card_performance(self, card_id: int, game_result: Dict) -> None:
        """
        Record performance of a card to the card memory system with improved error handling.
        
        Args:
            card_id: ID of the card
            game_result: Dictionary with game result information
        """
        if not isinstance(game_result, dict):
            logging.error(f"Invalid game_result format for card {card_id}: expected dict, got {type(game_result)}")
            return
            
        try:
            card_key = str(card_id)
            card_name = game_result.get('card_name', None)
            
            with self.memory_lock:
                # Register card if it doesn't exist
                if card_name and (card_key not in self.card_data):
                    self.register_card(card_id, card_name)
                    
                # Skip if card isn't registered and we don't have a name
                if card_key not in self.card_data:
                    logging.warning(f"Skipping update for unknown card: {card_id}")
                    return
                
                card_stats = self.card_data[card_key]
                
                # Ensure all required fields exist with defaults
                self._ensure_card_stats_fields(card_stats)
                
                # Update basic game stats
                card_stats['games_played'] += 1
                
                # Update win/loss/draw counters
                is_draw = game_result.get('is_draw', False)
                is_win = game_result.get('is_win', False)
                
                if is_draw:
                    card_stats['draws'] += 1
                elif is_win:
                    card_stats['wins'] += 1
                else:
                    card_stats['losses'] += 1
                
                # Update win rate (count draws as 0.5 wins)
                if card_stats['games_played'] > 0:
                    card_stats['win_rate'] = (card_stats['wins'] + 0.5 * card_stats['draws']) / card_stats['games_played']
                
                # Update play/draw statistics
                if game_result.get('was_drawn', False):
                    card_stats['times_drawn'] += 1
                    
                if game_result.get('was_played', False):
                    card_stats['times_played'] += 1
                    
                    # Update turn played statistics
                    turn_played = game_result.get('turn_played', 0)
                    if turn_played > 0:
                        # Initialize if this turn hasn't been recorded before
                        if str(turn_played) not in card_stats['turn_played']:
                            card_stats['turn_played'][str(turn_played)] = 0
                        card_stats['turn_played'][str(turn_played)] += 1
                        
                        # Track performance by turn
                        if str(turn_played) not in card_stats['performance_by_turn']:
                            card_stats['performance_by_turn'][str(turn_played)] = {
                                'played': 0, 'wins': 0, 'draws': 0, 'losses': 0
                            }
                        
                        perf = card_stats['performance_by_turn'][str(turn_played)]
                        perf['played'] += 1
                        if is_draw:
                            perf['draws'] += 1
                        elif is_win:
                            perf['wins'] += 1
                        else:
                            perf['losses'] += 1
                    
                    # Mana curve performance (if CMC is known)
                    if 'cmc' in game_result and turn_played > 0:
                        cmc = game_result['cmc']
                        curve_status = 'on_curve'
                        
                        if turn_played < cmc:
                            curve_status = 'below_curve'  # Played earlier than CMC
                        elif turn_played > cmc:
                            curve_status = 'above_curve'  # Played later than CMC
                        
                        # Update curve statistics
                        card_stats['mana_curve_performance'][curve_status]['played'] += 1
                        if is_win:
                            card_stats['mana_curve_performance'][curve_status]['wins'] += 1
                
                # Opening hand statistics
                if game_result.get('in_opening_hand', False):
                    card_stats['in_opening_hand'] += 1
                    if is_win:
                        card_stats['wins_in_opening_hand'] += 1
                
                # Archetype performance
                deck_archetype = game_result.get('deck_archetype', 'unknown')
                if deck_archetype not in card_stats['archetype_performance']:
                    card_stats['archetype_performance'][deck_archetype] = {
                        'games': 0, 'wins': 0, 'draws': 0, 'losses': 0
                    }
                
                arch_perf = card_stats['archetype_performance'][deck_archetype]
                arch_perf['games'] += 1
                if is_draw:
                    arch_perf['draws'] += 1
                elif is_win:
                    arch_perf['wins'] += 1
                else:
                    arch_perf['losses'] += 1
                
                # Update synergy partners
                self._update_synergy_partners(card_stats, game_result, is_win, is_draw)
                
                # Update performance trends
                self._update_performance_trend(card_stats, is_win, is_draw)
                
                # Calculate effectiveness rating
                self._calculate_effectiveness_rating(card_key)
                
                # Track updates and trigger periodic saving
                self.updates_since_save += 1
                if self.updates_since_save >= self.save_frequency:
                    self.save_memory_async()
                    self.updates_since_save = 0
                    logging.info(f"Triggered automatic save after {self.save_frequency} card updates")

        except Exception as e:
            logging.error(f"Error recording card performance for card {card_id}: {e}")
            import traceback
            logging.debug(traceback.format_exc())

    def _ensure_card_stats_fields(self, card_stats):
        """Ensure all required fields exist in card stats with defaults"""
        
        required_fields = {
            'games_played': 0,
            'wins': 0,
            'losses': 0,
            'draws': 0,
            'win_rate': 0.0,
            'times_drawn': 0,
            'times_played': 0,
            'turn_played': {},
            'performance_by_turn': {},
            'in_opening_hand': 0,
            'wins_in_opening_hand': 0,
            'mana_curve_performance': {
                'on_curve': {'played': 0, 'wins': 0},
                'below_curve': {'played': 0, 'wins': 0},
                'above_curve': {'played': 0, 'wins': 0}
            },
            'archetype_performance': {},
            'synergy_partners': {},
            'effectiveness_rating': 0.5,
            'performance_trend': [],
            'meta_position': {}
        }
        
        for key, default in required_fields.items():
            if key not in card_stats:
                card_stats[key] = default

    def _update_synergy_partners(self, card_stats, game_result, is_win, is_draw):
        """Update the synergy partners tracking"""
        synergy_partners = game_result.get('synergy_partners', [])
        for partner_id in synergy_partners:
            partner_key = str(partner_id)
            if partner_key not in card_stats['synergy_partners']:
                card_stats['synergy_partners'][partner_key] = {
                    'games_together': 0, 'wins_together': 0, 'draws_together': 0
                }
            
            partner_stats = card_stats['synergy_partners'][partner_key]
            partner_stats['games_together'] += 1
            if is_draw:
                partner_stats['draws_together'] += 1
            elif is_win:
                partner_stats['wins_together'] += 1

    def _update_performance_trend(self, card_stats, is_win, is_draw):
        """Update performance trend data"""
        # Update performance trends (last 10 games)
        if len(card_stats['performance_trend']) >= 10:
            card_stats['performance_trend'].pop(0)  # Remove oldest entry
        
        # Add result to trend (1 for win, 0.5 for draw, 0 for loss)
        if is_draw:
            card_stats['performance_trend'].append(0.5)
        elif is_win:
            card_stats['performance_trend'].append(1.0)
        else:
            card_stats['performance_trend'].append(0.0)
    
    def _calculate_effectiveness_rating(self, card_key: str) -> None:
        """
        Calculate a comprehensive effectiveness rating for a card based on all statistics,
        using a more data-driven, adaptive approach.
        Rating is between 0.0 (terrible) and 1.0 (excellent).
        
        Args:
            card_key: The card ID as a string
        """
        if card_key not in self.card_data:
            return
            
        card_stats = self.card_data[card_key]
        
        # Need minimum number of games for reliable rating
        if card_stats['games_played'] < 5:
            card_stats['effectiveness_rating'] = 0.5  # Neutral rating with insufficient data
            return
        
        # Create components for rating with dynamic weights
        components = []
        
        # Win rate component (base weight increases with sample size)
        win_rate = card_stats['win_rate']
        win_rate_weight = min(0.5, 0.3 + (card_stats['games_played'] / 100) * 0.2)
        components.append((win_rate, win_rate_weight))
        
        # Recent performance component (based on trend)
        if card_stats['performance_trend']:
            # Calculate trend direction and strength
            if len(card_stats['performance_trend']) >= 3:
                # Calculate simple trend direction
                recent_performance = sum(card_stats['performance_trend'][-3:]) / 3
                components.append((recent_performance, 0.2))
            else:
                # Simple average for short trends
                recent_performance = sum(card_stats['performance_trend']) / len(card_stats['performance_trend'])
                components.append((recent_performance, 0.2))
        
        # Play/draw component (how often the card is actually played when drawn)
        if card_stats['times_drawn'] > 0:
            play_rate = card_stats['times_played'] / card_stats['times_drawn']
            # Cards that are almost always played are likely good
            play_rate_weight = 0.1
            components.append((play_rate, play_rate_weight))
        
        # Curve efficiency component with adaptive weight
        curve_data = card_stats['mana_curve_performance']
        on_curve_played = curve_data['on_curve']['played']
        
        if on_curve_played > 0:
            on_curve_wins = curve_data['on_curve']['wins']
            curve_efficiency = on_curve_wins / on_curve_played
            
            # Add weight based on how many times it's been played on curve
            curve_weight = min(0.15, 0.05 + (on_curve_played / 20) * 0.1) 
            components.append((curve_efficiency, curve_weight))
        
        # Opening hand component with sample-size adjusted weight
        if card_stats['in_opening_hand'] > 0:
            opening_win_rate = card_stats['wins_in_opening_hand'] / card_stats['in_opening_hand']
            opening_weight = min(0.25, 0.1 + (card_stats['in_opening_hand'] / 20) * 0.15)
            components.append((opening_win_rate, opening_weight))
        
        # Metagame position component if available
        if 'meta_position' in card_stats and 'power_index' in card_stats['meta_position']:
            meta_weight = 0.1  # Fixed weight for meta position
            components.append((card_stats['meta_position']['power_index'], meta_weight))
        
        # Account for consistency across different deck archetypes
        if card_stats['archetype_performance']:
            # Calculate average performance across archetypes
            archetype_win_rates = []
            for arch, perf in card_stats['archetype_performance'].items():
                if perf['games'] > 0:
                    arch_win_rate = (perf['wins'] + 0.5 * perf.get('draws', 0)) / perf['games']
                    archetype_win_rates.append(arch_win_rate)
            
            if archetype_win_rates:
                # Calculate mean performance
                mean_arch_win_rate = sum(archetype_win_rates) / len(archetype_win_rates)
                components.append((mean_arch_win_rate, 0.15))
        
        # Calculate weighted average
        total_weight = sum(weight for _, weight in components)
        weighted_sum = sum(value * weight for value, weight in components)
        
        if total_weight > 0:
            card_stats['effectiveness_rating'] = weighted_sum / total_weight
        else:
            card_stats['effectiveness_rating'] = 0.5  # Default if no components
        
    def get_best_cards(self, min_games: int = 5, limit: int = 20) -> List[Dict]:
        """
        Get a list of the best performing cards.
        
        Args:
            min_games: Minimum number of games a card must have been in
            limit: Maximum number of cards to return
            
        Returns:
            List[Dict]: List of card data dictionaries sorted by effectiveness
        """
        with self.memory_lock:
            # Filter cards with enough games
            qualified_cards = [
                card_data for card_key, card_data in self.card_data.items()
                if card_data['games_played'] >= min_games
            ]
            
            # Sort by effectiveness rating (descending)
            qualified_cards.sort(key=lambda x: x['effectiveness_rating'], reverse=True)
            
            # Return limited number
            return qualified_cards[:limit]
    
    def get_card_synergies(self, card_id: Union[int, str], min_games: int = 3) -> List[Dict]:
        """
        Get best synergy partners for a specific card with improved synergy calculation.
        
        Args:
            card_id: ID of the card to find synergies for
            min_games: Minimum number of games cards must have been played together
            
        Returns:
            List[Dict]: List of synergy partners with statistics
        """
        card_key = str(card_id)
        with self.memory_lock:
            if card_key not in self.card_data:
                return []
                
            card_stats = self.card_data[card_key]
            synergies = []
            
            # Calculate baseline win rate for this card
            baseline_win_rate = card_stats['win_rate']
            
            for partner_id, partner_data in card_stats['synergy_partners'].items():
                if partner_data['games_together'] >= min_games:
                    # Calculate win rate together (counting draws as 0.5 wins)
                    win_rate = (partner_data['wins_together'] + 0.5 * partner_data['draws_together']) / partner_data['games_together']
                    
                    # Calculate synergy strength as improvement over baseline
                    synergy_strength = 0.0
                    if baseline_win_rate > 0:
                        # How much better do they perform together vs. card average
                        synergy_strength = (win_rate - baseline_win_rate) / baseline_win_rate
                    
                    # Get partner name if available
                    partner_name = self.card_id_to_name.get(partner_id, f"Card {partner_id}")
                    
                    # Get partner baseline win rate if available
                    partner_win_rate = 0.5  # Default
                    if partner_id in self.card_data:
                        partner_win_rate = self.card_data[partner_id].get('win_rate', 0.5)
                    
                    # A truly synergistic pair should outperform both cards' individual rates
                    combined_baseline = (baseline_win_rate + partner_win_rate) / 2
                    outperformance = win_rate - combined_baseline
                    
                    # Calculate sample size confidence factor (more games = more confidence)
                    confidence = min(1.0, partner_data['games_together'] / 10)
                    
                    # Final synergy score combines raw win rate, outperformance, and confidence
                    synergy_score = (win_rate * 0.5 + max(0, outperformance * 3) * 0.5) * confidence
                    
                    synergies.append({
                        'id': partner_id,
                        'name': partner_name,
                        'games_together': partner_data['games_together'],
                        'win_rate': win_rate,
                        'synergy_strength': synergy_strength,
                        'outperformance': outperformance,
                        'confidence': confidence,
                        'synergy_score': synergy_score
                    })
            
            # Sort by synergy score (descending)
            synergies.sort(key=lambda x: x['synergy_score'], reverse=True)
            return synergies
    
    def get_optimal_play_turn(self, card_id: Union[int, str]) -> int:
        """
        Get the optimal turn to play this card based on performance data.
        
        Args:
            card_id: ID of the card
            
        Returns:
            int: Optimal turn or 0 if unknown
        """
        card_key = str(card_id)
        with self.memory_lock:
            if card_key not in self.card_data:
                return 0
                
            card_stats = self.card_data[card_key]
            
            # If no performance data, return 0
            if not card_stats['performance_by_turn']:
                return 0
                
            # Find turn with best win rate (minimum 3 games)
            best_turn = 0
            best_win_rate = 0
            
            for turn, turn_data in card_stats['performance_by_turn'].items():
                if turn_data['played'] >= 3:
                    # Calculate win rate (counting draws as 0.5 wins)
                    win_rate = (turn_data['wins'] + 0.5 * turn_data['draws']) / turn_data['played']
                    
                    if win_rate > best_win_rate:
                        best_win_rate = win_rate
                        best_turn = int(turn)
            
            return best_turn
    
    def get_effectiveness_for_archetype(self, card_id: Union[int, str], archetype: str) -> float:
        """
        Get the effectiveness rating of a card for a specific deck archetype.
        
        Args:
            card_id: ID of the card
            archetype: The deck archetype
            
        Returns:
            float: Effectiveness rating (0.0-1.0) or 0.5 if unknown
        """
        card_key = str(card_id)
        
        # Check cache first
        cache_key = f"{card_key}_{archetype}"
        cached_effectiveness = self.cache_get(cache_key) if hasattr(self, 'cache_get') else None
        if cached_effectiveness is not None:
            return cached_effectiveness
            
        with self.memory_lock:
            if card_key not in self.card_data:
                return 0.5
                
            card_stats = self.card_data[card_key]
            
            # Check archetype performance
            if archetype in card_stats['archetype_performance']:
                arch_data = card_stats['archetype_performance'][archetype]
                
                # Need minimum games for reliable rating
                if arch_data['games'] < 3:
                    return 0.5
                    
                # Calculate win rate (counting draws as 0.5 wins)
                win_rate = (arch_data['wins'] + 0.5 * arch_data.get('draws', 0)) / arch_data['games']
                
                # Scale win rate to 0-1 range with better distribution
                # Win rates below 0.4 become worse, win rates above 0.6 become better
                effectiveness = 0.0
                if win_rate <= 0.5:
                    effectiveness = win_rate * 0.8  # Scale 0-0.5 to 0-0.4
                else:
                    effectiveness = 0.4 + (win_rate - 0.5) * 1.2  # Scale 0.5-1 to 0.4-1.0
                
                # Cache the result if we have caching capability
                if hasattr(self, 'cache_set'):
                    self.cache_set(cache_key, effectiveness)
                    
                return effectiveness
            
            # Fall back to overall effectiveness if archetype not found
            effectiveness = card_stats['effectiveness_rating']
            
            # Cache the result if we have caching capability
            if hasattr(self, 'cache_set'):
                self.cache_set(cache_key, effectiveness)
                
            return effectiveness
    
    def update_meta_position(self, card_id: Union[int, str], meta_data: Dict) -> None:
        """
        Update meta position data for a card with comprehensive metagame information.
        
        Args:
            card_id: ID of the card
            meta_data: Dictionary with meta position data, may include:
                - popularity: Float representing how often the card is played (0.0-1.0)
                - win_rate: Current win rate in the meta
                - played_count: How many games the card has been played in the meta
                - metagame_tier: Card's tier in the current metagame (S, A, B, C, etc.)
                - format: The format this data applies to (Standard, Modern, etc.)
                - common_synergies: List of cards frequently played with this one
                - counters: List of cards that counter this card
        """
        card_key = str(card_id)
        with self.memory_lock:
            if card_key not in self.card_data:
                logging.warning(f"Cannot update meta position for unknown card: {card_id}")
                return
                
            # Create meta_position dict if it doesn't exist
            if 'meta_position' not in self.card_data[card_key]:
                self.card_data[card_key]['meta_position'] = {
                    'popularity': 0.0,
                    'win_rate_trend': [],
                    'metagame_tier': 'unknown',
                    'last_updated': time.time()
                }
            
            meta_position = self.card_data[card_key]['meta_position']
            
            # Update with new meta data
            for key, value in meta_data.items():
                if key == 'win_rate':
                    # Store win rate history
                    meta_position['win_rate_trend'].append((time.time(), value))
                    # Limit trend size
                    if len(meta_position['win_rate_trend']) > 10:
                        meta_position['win_rate_trend'] = meta_position['win_rate_trend'][-10:]
                else:
                    # Update other fields directly
                    meta_position[key] = value
            
            # Always update timestamp
            meta_position['last_updated'] = time.time()
            
            # Calculate derived metrics
            if 'popularity' in meta_data and 'win_rate' in meta_data:
                # Power index: combination of popularity and win rate
                popularity = meta_data['popularity']
                win_rate = meta_data['win_rate']
                
                # Cards that are both popular and successful are powerful in the meta
                meta_position['power_index'] = (popularity * 0.4 + win_rate * 0.6) 
                
                # Determine meta tier based on power index
                power_index = meta_position['power_index']
                if power_index > 0.8:
                    meta_position['metagame_tier'] = 'S'  # Top tier
                elif power_index > 0.7:
                    meta_position['metagame_tier'] = 'A'  # Very strong
                elif power_index > 0.6:
                    meta_position['metagame_tier'] = 'B'  # Strong
                elif power_index > 0.5:
                    meta_position['metagame_tier'] = 'C'  # Average
                elif power_index > 0.4:
                    meta_position['metagame_tier'] = 'D'  # Below average
                else:
                    meta_position['metagame_tier'] = 'F'  # Weak
    
    def save_memory_async(self) -> None:
        """Save card memory asynchronously in a separate thread."""
        thread = threading.Thread(target=self.save_all_card_data)
        thread.daemon = True
        thread.start()