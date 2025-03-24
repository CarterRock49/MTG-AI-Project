import os
import json
import dash
from dash import dcc, html, Input, Output, State, callback
import dash_bootstrap_components as dbc
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import networkx as nx
from flask import Flask
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from flask import jsonify
from flask_cors import CORS
import gzip
import glob
import datetime

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
server = app.server
CORS(server)  # Enable CORS for all routes

class StatisticsLoader:
    """Loads and processes data from the DeckStatsTracker storage"""
    
    def __init__(self, data_path: str):
        self.data_path = data_path
        self.decks_data = {}
        self.meta_data = None
        self.card_data = {}  # Added to store individual card data
        self.deck_name_to_id = {}  # Added to map deck names to IDs
        self.card_name_to_id = {}  # Added to map card names to IDs
        self.reload_data()
        
    def reload_data(self):
        """Reload all data from storage"""
        self._load_decks_data()
        self._load_meta_data()
        self._load_card_data()  # Added to load individual card stats
        
    def _load_decks_data(self):
        """Load all deck data from the storage"""
        decks_dir = os.path.join(self.data_path, "decks")
        self.decks_data = {}
        
        if os.path.exists(decks_dir):
            for filename in os.listdir(decks_dir):
                if filename.endswith(".json") or filename.endswith(".json.gz"):
                    try:
                        filepath = os.path.join(decks_dir, filename)
                        if filename.endswith(".gz"):
                            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                                deck_data = json.load(f)
                        else:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                deck_data = json.load(f)
                        
                        # Use deck_id as key if available, otherwise use filename
                        key = deck_data.get("deck_id", os.path.splitext(filename)[0])
                        
                        # Also store name to ID mapping if available
                        if "name" in deck_data and "deck_id" in deck_data:
                            self.deck_name_to_id[deck_data["name"]] = deck_data["deck_id"]
                        
                        self.decks_data[key] = deck_data
                    except Exception as e:
                        print(f"Error loading {filename}: {e}")
    
    def _load_card_data(self):
        """Load individual card stats from the cards directory"""
        cards_dir = os.path.join(self.data_path, "cards")
        self.card_data = {}
        
        if os.path.exists(cards_dir):
            for filename in os.listdir(cards_dir):
                if filename.endswith(".json") or filename.endswith(".json.gz"):
                    try:
                        filepath = os.path.join(cards_dir, filename)
                        if filename.endswith(".gz"):
                            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                                card_stats = json.load(f)
                        else:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                card_stats = json.load(f)
                        
                        # Use card name as key
                        card_name = card_stats.get("name")
                        if card_name:
                            self.card_data[card_name] = card_stats
                            
                            # Store ID mapping if available
                            if "id" in card_stats:
                                self.card_name_to_id[card_name] = card_stats["id"]
                    except Exception as e:
                        print(f"Error loading card data {filename}: {e}")
                        
    def _load_meta_data(self):
        """Load meta data from storage with improved total games calculation"""
        # Try multiple paths for meta data
        possible_paths = [
            os.path.join(self.data_path, "meta", "meta_data.json.gz"),
            os.path.join(self.data_path, "meta", "meta_data.json"),
            os.path.join(self.data_path, "meta_data.json.gz"),
            os.path.join(self.data_path, "meta_data.json")
        ]
        
        for meta_path in possible_paths:
            try:
                if meta_path.endswith(".gz"):
                    with gzip.open(meta_path, 'rt', encoding='utf-8') as f:
                        self.meta_data = json.load(f)
                    break
                else:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        self.meta_data = json.load(f)
                    break
            except FileNotFoundError:
                continue
            except Exception as e:
                print(f"Error loading meta data from {meta_path}: {e}")
        
        if not self.meta_data:
            print("No meta data file found in any of the expected locations")
            self.meta_data = {}
            return
        
        # Recalculate total games from all decks to ensure accuracy
        total_games = 0
        for deck_id, deck_data in self.decks_data.items():
            total_games += deck_data.get("games", 0)
        
        # Update the meta data with the correct total
        if total_games > 0:
            self.meta_data["total_games"] = total_games

    
    def get_deck_list(self) -> List[Dict]:
        """Get a list of all decks with basic information"""
        return [
            {
                "id": deck_id,
                "name": data.get("name", "Unknown Deck"),
                "archetype": data.get("archetype", "Unknown"),
                "win_rate": data.get("win_rate", 0),
                "games": data.get("games", 0)
            }
            for deck_id, data in self.decks_data.items()
        ]
    
    def get_deck_by_name(self, deck_name: str) -> Optional[Dict]:
        """Get deck details by name instead of ID"""
        deck_id = self.deck_name_to_id.get(deck_name)
        if deck_id:
            return self.decks_data.get(deck_id)
        
        # If not found by direct mapping, try a case-insensitive search
        for deck_id, data in self.decks_data.items():
            if data.get("name", "").lower() == deck_name.lower():
                return data
        
        return None
    
    def get_deck_details(self, deck_id: str) -> Optional[Dict]:
        """Get detailed information for a specific deck"""
        # Check if this is a name instead of an ID
        if deck_id in self.deck_name_to_id:
            deck_id = self.deck_name_to_id[deck_id]
            
        return self.decks_data.get(deck_id)
    
    def get_meta_snapshot(self) -> Dict:
        """Get the current meta snapshot"""
        if not self.meta_data:
            return {}
            
        # Prepare meta snapshot data
        archetypes = {}
        for archetype, data in self.meta_data.get("archetypes", {}).items():
            if data.get("games", 0) >= 5:  # Minimum sample size
                archetypes[archetype] = {
                    "games": data.get("games", 0),
                    "win_rate": data.get("win_rate", 0),
                    "meta_share": data.get("games", 0) / max(1, self.meta_data.get("total_games", 1))
                }
                
        return {
            "total_games": self.meta_data.get("total_games", 0),
            "last_updated": self.meta_data.get("last_updated", 0),
            "archetypes": archetypes
        }
    
    def get_card_stats(self) -> Dict[str, Dict]:
        """Get statistics for all cards - now using names as keys"""
        # Use the card data from individual files
        if self.card_data:
            return self.card_data
            
        # Fallback to meta data if individual files not available
        if not self.meta_data:
            return {}
            
        card_stats = {}
        # Convert from ID-based to name-based indexing
        for card_name, card_info in self.meta_data.get("cards", {}).items():
            card_stats[card_name] = card_info
            
        return card_stats
    
    def get_card_details(self, card_name: str) -> Optional[Dict]:
        """Get detailed information for a specific card by name"""
        # First check individual card files
        if card_name in self.card_data:
            return self.card_data[card_name]
            
        # Then check meta data
        if self.meta_data and "cards" in self.meta_data:
            return self.meta_data["cards"].get(card_name)
            
        return None
    
    def get_matchup_matrix(self) -> Dict:
        """Get the matchup matrix between archetypes"""
        if not self.meta_data:
            return {}
            
        # Process matchup data into a matrix format
        archetypes = set()
        for matchup in self.meta_data.get("matchups", {}):
            parts = matchup.split("_vs_")
            if len(parts) == 2:
                archetypes.add(parts[0])
                archetypes.add(parts[1])
                
        archetypes = sorted(list(archetypes))
        matrix = {}
        
        for arch1 in archetypes:
            matrix[arch1] = {}
            for arch2 in archetypes:
                if arch1 == arch2:
                    matrix[arch1][arch2] = 0.5  # Mirror match is 50%
                else:
                    matchup_key = f"{arch1}_vs_{arch2}"
                    matchup_data = self.meta_data.get("matchups", {}).get(matchup_key, {})
                    matrix[arch1][arch2] = matchup_data.get("win_rate", 0.5)
        
        return {
            "archetypes": archetypes,
            "matrix": matrix
        }
    
    def get_synergy_data(self) -> Dict:
        """Extract card synergy data from deck stats"""
        synergy_pairs = []
        
        for deck_id, deck_data in self.decks_data.items():
            # Check if we have card performance data - now either by ID or by name
            card_perf_by_id = deck_data.get("card_performance", {})
            card_perf_by_name = deck_data.get("card_performance_by_name", {})
            
            # Prefer name-based data if available
            if card_perf_by_name:
                card_names = list(card_perf_by_name.keys())
                
                # For each pair of cards, calculate correlation in performance
                for i in range(len(card_names)):
                    for j in range(i+1, len(card_names)):
                        card1 = card_perf_by_name[card_names[i]]
                        card2 = card_perf_by_name[card_names[j]]
                        
                        # Simple correlation: if they win together, they might have synergy
                        if card1.get("wins", 0) > 0 and card2.get("wins", 0) > 0:
                            synergy_pairs.append({
                                "card1_name": card_names[i],
                                "card2_name": card_names[j],
                                "deck_id": deck_id,
                                "deck_name": deck_data.get("name", "Unknown Deck")
                            })
            elif card_perf_by_id:
                # Fallback to ID-based if name-based not available
                card_ids = list(card_perf_by_id.keys())
                
                for i in range(len(card_ids)):
                    for j in range(i+1, len(card_ids)):
                        card1 = card_perf_by_id[card_ids[i]]
                        card2 = card_perf_by_id[card_ids[j]]
                        
                        if card1.get("wins", 0) > 0 and card2.get("wins", 0) > 0:
                            synergy_pairs.append({
                                "card1_id": card_ids[i],
                                "card1_name": card1.get("name", f"Card {card_ids[i]}"),
                                "card2_id": card_ids[j],
                                "card2_name": card2.get("name", f"Card {card_ids[j]}"),
                                "deck_id": deck_id,
                                "deck_name": deck_data.get("name", "Unknown Deck")
                            })
        
        return synergy_pairs
    
    def get_deck_recommendations(self, meta_data=None) -> List[Dict]:
        """Get deck recommendations based on meta analysis with improved fallbacks"""
        if not meta_data:
            meta_data = self.get_meta_snapshot()
            
        # Fallback if no meta data
        if not meta_data or not meta_data.get("archetypes"):
            # Just recommend best performing decks
            top_decks = []
            for deck_id, deck_data in self.decks_data.items():
                if deck_data.get("games", 0) >= 3:  # Lower threshold for recommendations
                    top_decks.append({
                        "archetype": deck_data.get("archetype", "Unknown"),
                        "avg_win_rate_vs_meta": deck_data.get("win_rate", 0),
                        "example_decks": [{
                            "id": deck_id,
                            "name": deck_data.get("name", "Unknown"),
                            "win_rate": deck_data.get("win_rate", 0),
                            "games": deck_data.get("games", 0)
                        }]
                    })
            
            # Group by archetype and take best deck for each
            archetype_decks = {}
            for deck in top_decks:
                archetype = deck["archetype"]
                if archetype not in archetype_decks or deck["avg_win_rate_vs_meta"] > archetype_decks[archetype]["avg_win_rate_vs_meta"]:
                    archetype_decks[archetype] = deck
            
            # Sort by win rate and return
            results = list(archetype_decks.values())
            results.sort(key=lambda x: x["avg_win_rate_vs_meta"], reverse=True)
            return results[:5]  # Return top 5
            
        # Get matchup data
        matchup_data = self.get_matchup_matrix()
        
        # Find top meta archetypes
        top_archetypes = []
        for archetype, data in meta_data.get("archetypes", {}).items():
            if data.get("games", 0) >= 3:  # Lower threshold
                top_archetypes.append({
                    "archetype": archetype,
                    "meta_share": data.get("games", 0) / max(1, meta_data.get("total_games", 1)),
                    "win_rate": data.get("win_rate", 0)
                })
                
        # Sort by meta share
        top_archetypes.sort(key=lambda x: x["meta_share"], reverse=True)
        top_archetypes = top_archetypes[:3]  # Top 3
        
        # If still no top archetypes, use fallback
        if not top_archetypes:
            # Return the top-performing decks
            top_decks = []
            for deck_id, deck_data in self.decks_data.items():
                if deck_data.get("games", 0) >= 3:
                    top_decks.append({
                        "archetype": deck_data.get("archetype", "Unknown"),
                        "avg_win_rate_vs_meta": deck_data.get("win_rate", 0),
                        "example_decks": [{
                            "id": deck_id,
                            "name": deck_data.get("name", "Unknown"),
                            "win_rate": deck_data.get("win_rate", 0),
                            "games": deck_data.get("games", 0)
                        }]
                    })
            
            top_decks.sort(key=lambda x: x["avg_win_rate_vs_meta"], reverse=True)
            return top_decks[:5]
        
        recommendations = []
        
        # First try with matchup data
        if matchup_data and matchup_data.get("matrix"):
            for arch, matchups in matchup_data.get("matrix", {}).items():
                # Calculate average win rate against top archetypes
                win_rates = []
                for top_arch in top_archetypes:
                    win_rates.append(matchups.get(top_arch["archetype"], 0.5))
                    
                avg_win_rate = sum(win_rates) / max(1, len(win_rates))
                
                # Find decks of this archetype
                example_decks = []
                for deck_id, deck_data in self.decks_data.items():
                    if deck_data.get("archetype") == arch and deck_data.get("games", 0) >= 3:
                        example_decks.append({
                            "id": deck_id,
                            "name": deck_data.get("name", "Unknown"),
                            "win_rate": deck_data.get("win_rate", 0),
                            "games": deck_data.get("games", 0)
                        })
                
                # Sort example decks by win rate
                example_decks.sort(key=lambda x: x["win_rate"], reverse=True)
                
                # Add to recommendations if looks promising
                if avg_win_rate > 0.45 and example_decks:  # Lower threshold
                    recommendations.append({
                        "archetype": arch,
                        "avg_win_rate_vs_meta": avg_win_rate,
                        "example_decks": example_decks[:2]  # Top 2 examples
                    })
        
        # If we still don't have recommendations, fall back to best decks
        if not recommendations:
            # Use decks with best win rates
            for deck_id, deck_data in self.decks_data.items():
                if deck_data.get("games", 0) >= 3:
                    arch = deck_data.get("archetype", "Unknown")
                    
                    # Check if this archetype is already in recommendations
                    existing = next((r for r in recommendations if r["archetype"] == arch), None)
                    
                    if existing:
                        # Add to example decks if not already there
                        if not any(d["id"] == deck_id for d in existing["example_decks"]):
                            existing["example_decks"].append({
                                "id": deck_id,
                                "name": deck_data.get("name", "Unknown"),
                                "win_rate": deck_data.get("win_rate", 0),
                                "games": deck_data.get("games", 0)
                            })
                            # Re-sort example decks
                            existing["example_decks"].sort(key=lambda x: x["win_rate"], reverse=True)
                            existing["example_decks"] = existing["example_decks"][:2]  # Keep top 2
                    else:
                        # Add new archetype
                        recommendations.append({
                            "archetype": arch,
                            "avg_win_rate_vs_meta": deck_data.get("win_rate", 0),
                            "example_decks": [{
                                "id": deck_id,
                                "name": deck_data.get("name", "Unknown"),
                                "win_rate": deck_data.get("win_rate", 0),
                                "games": deck_data.get("games", 0)
                            }]
                        })
        
        # Sort by win rate
        recommendations.sort(key=lambda x: x["avg_win_rate_vs_meta"], reverse=True)
        
        return recommendations[:5]  # Top 5 recommendations

# Initialize data loader
current_dir = os.path.dirname(os.path.abspath(__file__))
# Build the data path relative to the file's location
data_dir = os.path.join(current_dir, "..", "deck_stats")
data_loader = StatisticsLoader(data_dir)

# Layout will be defined here
app.layout = html.Div([
    # Navigation bar
    dbc.NavbarSimple(
        brand="Card Game Statistics Viewer",
        brand_href="#",
        color="primary",
        dark=True,
        children=[
            dbc.NavItem(dbc.NavLink("Dashboard", href="#")),
            dbc.NavItem(dbc.NavLink("Decks", href="#")),
            dbc.NavItem(dbc.NavLink("Cards", href="#")),
            dbc.NavItem(dbc.NavLink("Meta", href="#")),
            dbc.NavItem(dbc.NavLink("Recommendations", href="#")),
        ],
    ),
    
    # Main content
    dbc.Container([
        dbc.Row([
            dbc.Col([
                html.H1("Dashboard", className="mt-4"),
                html.Hr(),
            ])
        ]),
        
        # Tabs for different views
        dcc.Tabs([
            # Dashboard tab
            dcc.Tab(label="Dashboard", children=[
                dbc.Row([
                    # Summary metrics
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Total Games"),
                            dbc.CardBody(id="total-games", children="Loading...")
                        ], className="mb-4"),
                    ], width=3),
                    
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Top Archetype"),
                            dbc.CardBody(id="top-archetype", children="Loading...")
                        ], className="mb-4"),
                    ], width=3),
                    
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Best Win Rate"),
                            dbc.CardBody(id="best-win-rate", children="Loading...")
                        ], className="mb-4"),
                    ], width=3),
                    
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Last Updated"),
                            dbc.CardBody(id="last-updated", children="Loading...")
                        ], className="mb-4"),
                    ], width=3),
                ], className="mt-4"),
                
                dbc.Row([
                    # Meta composition chart
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Meta Composition"),
                            dbc.CardBody([
                                dcc.Graph(id="meta-composition-pie")
                            ])
                        ]),
                    ], width=6),
                    
                    # Win rate by archetype chart
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Win Rates by Archetype"),
                            dbc.CardBody([
                                dcc.Graph(id="win-rate-by-archetype")
                            ])
                        ]),
                    ], width=6),
                ], className="mt-4"),
                
                dbc.Row([
                    # Matchup heatmap
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Matchup Heatmap"),
                            dbc.CardBody([
                                dcc.Graph(id="matchup-heatmap")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
            ]),
            
            # Deck analysis tab
            dcc.Tab(label="Deck Analysis", children=[
                dbc.Row([
                    dbc.Col([
                        html.Label("Select Deck:"),
                        dcc.Dropdown(id="deck-selector", options=[]),
                    ], width=6, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Deck performance card
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader(id="deck-name-header"),
                            dbc.CardBody([
                                html.Div(id="deck-details")
                            ])
                        ]),
                    ], width=4, className="mt-4"),
                    
                    # Mana curve chart
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Mana Curve"),
                            dbc.CardBody([
                                dcc.Graph(id="mana-curve-chart")
                            ])
                        ]),
                    ], width=8, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Card performance table
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Card Performance"),
                            dbc.CardBody([
                                html.Div(id="card-performance-table")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
            ]),
            
            # Card analysis tab
            dcc.Tab(label="Card Analysis", children=[
                dbc.Row([
                    dbc.Col([
                        html.Label("Select Card:"),
                        dcc.Dropdown(id="card-selector", options=[]),
                    ], width=6, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Card details
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader(id="card-name-header"),
                            dbc.CardBody([
                                html.Div(id="card-details")
                            ])
                        ]),
                    ], width=4, className="mt-4"),
                    
                    # Card win rate by game stage
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Performance by Game Stage"),
                            dbc.CardBody([
                                dcc.Graph(id="card-performance-by-stage")
                            ])
                        ]),
                    ], width=8, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Card synergy graph
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Card Synergies"),
                            dbc.CardBody([
                                dcc.Graph(id="card-synergy-graph")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
            ]),
            
            # Meta analysis tab
            dcc.Tab(label="Meta Analysis", children=[
                dbc.Row([
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Meta Overview"),
                            dbc.CardBody([
                                html.Div(id="meta-overview")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
                
                dbc.Row([
                    # Meta archetype popularity vs. win rate
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Popularity vs. Win Rate"),
                            dbc.CardBody([
                                dcc.Graph(id="popularity-vs-winrate")
                            ])
                        ]),
                    ], width=6, className="mt-4"),
                    
                    # Meta shifts over time
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Meta Shifts"),
                            dbc.CardBody([
                                dcc.Graph(id="meta-shifts")
                            ])
                        ]),
                    ], width=6, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Full matchup table
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Full Matchup Table"),
                            dbc.CardBody([
                                html.Div(id="full-matchup-table")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
            ]),
            
            # Recommendations tab
            dcc.Tab(label="Recommendations", children=[
                dbc.Row([
                    dbc.Col([
                        html.Label("Select Deck to Improve:"),
                        dcc.Dropdown(id="improve-deck-selector", options=[]),
                    ], width=6, className="mt-4"),
                ]),
                
                dbc.Row([
                    # Deck improvement suggestions
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Improvement Suggestions"),
                            dbc.CardBody([
                                html.Div(id="improvement-suggestions")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
                
                dbc.Row([
                    # Card replacement recommendations
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Card Replacement Recommendations"),
                            dbc.CardBody([
                                html.Div(id="card-replacements")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
                
                dbc.Row([
                    # Meta positioning recommendations
                    dbc.Col([
                        dbc.Card([
                            dbc.CardHeader("Meta Positioning Recommendations"),
                            dbc.CardBody([
                                html.Div(id="meta-recommendations")
                            ])
                        ]),
                    ], className="mt-4"),
                ]),
            ]),
        ]),
        
        # Refresh button
        dbc.Row([
            dbc.Col([
                dbc.Button("Refresh Data", id="refresh-button", color="primary", className="mt-4"),
            ], width=2),
        ]),
        
        # Footer
        dbc.Row([
            dbc.Col([
                html.Hr(),
                html.P("Card Game Statistics Viewer - Powered by DeckStatsTracker", className="text-center"),
            ]),
        ], className="mt-4"),
        
    ], fluid=True),
])

# Callbacks to update the UI components would be defined here
@app.callback(
    [Output("total-games", "children"),
     Output("top-archetype", "children"),
     Output("best-win-rate", "children"),
     Output("last-updated", "children"),
     Output("meta-composition-pie", "figure"),
     Output("win-rate-by-archetype", "figure"),
     Output("matchup-heatmap", "figure")],
    [Input("refresh-button", "n_clicks")]
)
def update_dashboard(n_clicks):
    """Update the dashboard tab"""
    # Reload data
    data_loader.reload_data()
    
    # Get meta snapshot
    meta_snapshot = data_loader.get_meta_snapshot()
    
    # Update metrics
    total_games = meta_snapshot.get("total_games", 0)
    
    # Find top archetype by meta share
    archetypes = meta_snapshot.get("archetypes", {})
    top_archetype = "Unknown"
    top_meta_share = 0
    
    for archetype, data in archetypes.items():
        if data.get("meta_share", 0) > top_meta_share:
            top_meta_share = data.get("meta_share", 0)
            top_archetype = archetype
    
    # Find best win rate
    best_win_rate = 0
    best_win_rate_arch = "Unknown"
    
    for archetype, data in archetypes.items():
        if data.get("games", 0) >= 10 and data.get("win_rate", 0) > best_win_rate:
            best_win_rate = data.get("win_rate", 0)
            best_win_rate_arch = archetype
    
    # Format last updated timestamp
    import datetime
    last_updated = "Unknown"
    if meta_snapshot.get("last_updated"):
        last_updated = datetime.datetime.fromtimestamp(
            meta_snapshot["last_updated"]
        ).strftime('%Y-%m-%d %H:%M:%S')
    
    # Create meta composition pie chart
    meta_data = []
    for archetype, data in archetypes.items():
        if data.get("games", 0) >= 5:  # Minimum sample size
            meta_data.append({
                "archetype": archetype,
                "games": data.get("games", 0),
                "meta_share": data.get("meta_share", 0)
            })
    
    meta_df = pd.DataFrame(meta_data)
    if not meta_df.empty:
        meta_pie = px.pie(
            meta_df, 
            values='meta_share', 
            names='archetype',
            title='Meta Composition'
        )
    else:
        meta_pie = go.Figure()
        meta_pie.add_annotation(
            text="No meta data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    # Create win rate by archetype chart
    win_rate_data = []
    for archetype, data in archetypes.items():
        if data.get("games", 0) >= 5:  # Minimum sample size
            win_rate_data.append({
                "archetype": archetype,
                "win_rate": data.get("win_rate", 0) * 100,  # Convert to percentage
                "games": data.get("games", 0)
            })
    
    win_rate_df = pd.DataFrame(win_rate_data)
    if not win_rate_df.empty:
        win_rate_chart = px.bar(
            win_rate_df.sort_values('win_rate', ascending=False),
            x='archetype',
            y='win_rate',
            color='win_rate',
            title='Win Rates by Archetype',
            labels={'win_rate': 'Win Rate (%)', 'archetype': 'Archetype'},
            text='win_rate',
            color_continuous_scale=px.colors.sequential.Blues
        )
        win_rate_chart.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
    else:
        win_rate_chart = go.Figure()
        win_rate_chart.add_annotation(
            text="No win rate data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    # Create matchup heatmap
    matchup_data = data_loader.get_matchup_matrix()
    
    if matchup_data and matchup_data.get("archetypes"):
        # Create a DataFrame for the heatmap
        matrix_data = []
        for arch1 in matchup_data["archetypes"]:
            for arch2 in matchup_data["archetypes"]:
                matrix_data.append({
                    "arch1": arch1,
                    "arch2": arch2,
                    "win_rate": matchup_data["matrix"].get(arch1, {}).get(arch2, 0.5) * 100
                })
        
        matrix_df = pd.DataFrame(matrix_data)
        matrix_pivot = matrix_df.pivot(index="arch1", columns="arch2", values="win_rate")
        
        matchup_heatmap = px.imshow(
            matrix_pivot,
            title="Matchup Win Rates (%)",
            labels=dict(x="Opponent Archetype", y="Player Archetype", color="Win Rate (%)"),
            color_continuous_scale=px.colors.diverging.RdBu,
            color_continuous_midpoint=50,
            aspect="auto"
        )
    else:
        matchup_heatmap = go.Figure()
        matchup_heatmap.add_annotation(
            text="No matchup data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    return [
        f"{total_games:,}",
        f"{top_archetype} ({top_meta_share:.1%})",
        f"{best_win_rate_arch} ({best_win_rate:.1%})",
        last_updated,
        meta_pie,
        win_rate_chart,
        matchup_heatmap
    ]

# Callback to populate deck selector dropdown
@app.callback(
    Output("deck-selector", "options"),
    [Input("refresh-button", "n_clicks")]
)
def update_deck_selector(n_clicks):
    """Update the deck selector dropdown options"""
    decks = data_loader.get_deck_list()
    # Use name as both label and value for more user-friendly selection
    return [{"label": f"{deck['name']} ({deck['archetype']})", "value": deck["name"]} for deck in decks]

# Initialize the deck selector on startup
@app.callback(
    Output("deck-selector", "value"),
    [Input("deck-selector", "options")]
)
def initialize_deck_selector(options):
    """Initialize the deck selector with the first value"""
    if options and len(options) > 0:
        return options[0]["value"]
    return None

# Callback to populate card selector dropdown
@app.callback(
    Output("card-selector", "options"),
    [Input("refresh-button", "n_clicks")]
)
def update_card_selector(n_clicks):
    """Update the card selector dropdown options"""
    card_stats = data_loader.get_card_stats()
    # Use card names as both label and value
    return [{"label": name, "value": name} for name in card_stats.keys()]

# Callback to populate card details
@app.callback(
    [Output("card-name-header", "children"),
     Output("card-details", "children"),
     Output("card-performance-by-stage", "figure")],
    [Input("card-selector", "value")]
)
def update_card_details(card_name):
    """Update the card details view based on selected card"""
    if not card_name:
        return "Select a Card", "No card selected", go.Figure()
    
    # Get card details
    card_data = data_loader.get_card_details(card_name)
    
    if not card_data:
        return f"Card: {card_name}", "No data available for this card", go.Figure()
    
    # Create card details HTML
    details_html = html.Div([
        html.H4(card_data.get("name", card_name)),
        html.P([
            html.Strong("Win Rate: "), 
            f"{card_data.get('win_rate', 0) * 100:.1f}%"
        ]),
        html.P([
            html.Strong("Games Played: "), 
            f"{card_data.get('games_played', 0)}"
        ]),
        html.P([
            html.Strong("Usage Count: "), 
            f"{card_data.get('usage_count', 0)}"
        ]),
        
        # Add archetype performance if available
        html.H5("Archetype Performance:", className="mt-3"),
        html.Div([
            html.Table(
                [html.Tr([html.Th("Archetype"), html.Th("Games"), html.Th("Wins")])] +
                [html.Tr([
                    html.Td(archetype),
                    html.Td(data.get("games", 0)),
                    html.Td(data.get("wins", 0))
                ]) for archetype, data in card_data.get("archetypes", {}).items()],
                className="table table-striped table-sm"
            ) if card_data.get("archetypes") else html.P("No archetype data available")
        ])
    ])
    
    # Create performance by game stage chart
    stage_data = []
    for stage, data in card_data.get("by_game_stage", {}).items():
        if data.get("games", 0) > 0:
            win_rate = data.get("wins", 0) / data.get("games", 0)
            stage_data.append({
                "stage": stage.capitalize(),
                "win_rate": win_rate * 100,
                "games": data.get("games", 0)
            })
    
    if stage_data:
        stage_fig = px.bar(
            pd.DataFrame(stage_data),
            x="stage",
            y="win_rate",
            color="win_rate",
            title="Win Rate by Game Stage",
            labels={"win_rate": "Win Rate (%)", "stage": "Game Stage"},
            text="games",
            color_continuous_scale=px.colors.sequential.Blues
        )
        stage_fig.update_traces(texttemplate='%{text} games', textposition='outside')
    else:
        stage_fig = go.Figure()
        stage_fig.add_annotation(
            text="No game stage data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    return f"Card: {card_name}", details_html, stage_fig

# Callback to update deck details
@app.callback(
    [Output("deck-name-header", "children"),
     Output("deck-details", "children"),
     Output("mana-curve-chart", "figure"),
     Output("card-performance-table", "children")],
    [Input("deck-selector", "value")]
)
def update_deck_details(deck_name):
    """Update the deck details based on the selected deck"""
    if not deck_name:
        return "Select a Deck", "No deck selected", go.Figure(), "No data"
    
    # Get deck details by name first
    deck_data = data_loader.get_deck_by_name(deck_name)
    
    # If not found by name, try by ID (backward compatibility)
    if not deck_data:
        deck_data = data_loader.get_deck_details(deck_name)
    
    if not deck_data:
        return f"Deck: {deck_name}", "No data available for this deck", go.Figure(), "No data"
    
    # Create deck details HTML
    details_html = html.Div([
        html.H4(deck_data.get("name", deck_name)),
        html.P([
            html.Strong("Archetype: "), 
            deck_data.get("archetype", "Unknown")
        ]),
        html.P([
            html.Strong("Games: "), 
            f"{deck_data.get('games', 0)}"
        ]),
        html.P([
            html.Strong("Win Rate: "), 
            f"{deck_data.get('win_rate', 0) * 100:.1f}%"
        ]),
        html.P([
            html.Strong("Record: "), 
            f"{deck_data.get('wins', 0)}W / {deck_data.get('losses', 0)}L"
            + (f" / {deck_data.get('draws', 0)}D" if "draws" in deck_data else "")
        ]),
        
        # Add game stage performance if available
        html.H5("Performance by Game Stage:", className="mt-3"),
        html.Div([
            html.Table(
                [html.Tr([html.Th("Stage"), html.Th("Wins"), html.Th("Losses"), 
                          html.Th("Draws" if "draws" in deck_data else "")])],
                className="table table-striped table-sm"
            )
        ])
    ])
    
    # Add rows for each stage
    if "performance_by_stage" in deck_data:
        table = details_html.children[-1].children[0]
        for stage, data in deck_data.get("performance_by_stage", {}).items():
            stage_row = [
                html.Td(stage.capitalize()),
                html.Td(data.get("wins", 0)),
                html.Td(data.get("losses", 0))
            ]
            
            # Add draws column if it exists
            if "draws" in deck_data:
                stage_row.append(html.Td(data.get("draws", 0) if "draws" in data else 0))
                
            table.children.append(html.Tr(stage_row))
    
    # Create mana curve chart
    mana_curve_data = []
    cards = deck_data.get("card_list", [])
    
    # Group cards by mana cost with improved data access
    cost_counts = {}
    for card in cards:
        # Try multiple approaches to get the mana cost
        cost = None
        
        # 1. Check if card has cmc directly
        if isinstance(card, dict) and "cmc" in card:
            cost = card["cmc"]
        
        # 2. Try to look up card in meta data by name
        elif isinstance(card, dict) and "name" in card:
            card_name = card["name"]
            # Look for card in meta data
            meta_data = data_loader._load_meta_data()
            if meta_data and "cards" in meta_data and card_name in meta_data["cards"]:
                meta_card = meta_data["cards"][card_name]
                if "cmc" in meta_card:
                    cost = meta_card["cmc"]
        
        # 3. Fallback to default value
        if cost is None:
            cost = card.get("cost", 0) if isinstance(card, dict) else 0
        
        # Create cost bucket
        cost_bucket = str(int(cost)) if cost < 7 else "7+"
        
        if cost_bucket not in cost_counts:
            cost_counts[cost_bucket] = 0
        cost_counts[cost_bucket] += card.get("count", 1) if isinstance(card, dict) else 1
    
    # Convert to list for chart
    for cost, count in cost_counts.items():
        mana_curve_data.append({
            "cost": cost,
            "count": count
        })
    
    # Sort by mana cost
    mana_curve_data.sort(key=lambda x: int(x["cost"].replace("+", "")))
    
    if mana_curve_data:
        mana_curve_fig = px.bar(
            pd.DataFrame(mana_curve_data),
            x="cost",
            y="count",
            title="Mana Curve",
            labels={"count": "Number of Cards", "cost": "Mana Cost"},
            color="count",
            color_continuous_scale=px.colors.sequential.Blues
        )
    else:
        mana_curve_fig = go.Figure()
        mana_curve_fig.add_annotation(
            text="No mana curve data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    # Create card performance table
    card_performance = []
    
    # Prefer name-based card performance data if available
    if "card_performance_by_name" in deck_data:
        card_perf = deck_data["card_performance_by_name"]
        for card_name, data in card_perf.items():
            if data.get("games_played", 0) > 0:
                win_rate = data.get("wins", 0) / data.get("games_played", 0)
                card_performance.append({
                    "name": card_name,
                    "win_rate": win_rate,
                    "games": data.get("games_played", 0),
                    "usage": data.get("usage_count", 0)
                })
    # Fallback to ID-based card performance
    elif "card_performance" in deck_data:
        card_perf = deck_data["card_performance"]
        for card_id, data in card_perf.items():
            if data.get("games_played", 0) > 0:
                win_rate = data.get("wins", 0) / data.get("games_played", 0)
                card_performance.append({
                    "name": data.get("name", f"Card {card_id}"),
                    "win_rate": win_rate,
                    "games": data.get("games_played", 0),
                    "usage": data.get("usage_count", 0)
                })
    
    # Sort by win rate (highest first)
    card_performance.sort(key=lambda x: x["win_rate"], reverse=True)
    
    # Create table HTML
    if card_performance:
        table_html = html.Table(
            [html.Tr([
                html.Th("Card Name"),
                html.Th("Win Rate"),
                html.Th("Games"),
                html.Th("Times Used")
            ])] +
            [html.Tr([
                html.Td(card["name"]),
                html.Td(f"{card['win_rate'] * 100:.1f}%"),
                html.Td(card["games"]),
                html.Td(card["usage"])
            ]) for card in card_performance],
            className="table table-striped"
        )
    else:
        table_html = html.P("No card performance data available")
    
    return f"Deck: {deck_data.get('name', deck_name)}", details_html, mana_curve_fig, table_html

@app.callback(
    Output("meta-recommendations", "children"),
    [Input("refresh-button", "n_clicks")]
)
def update_meta_recommendations(n_clicks):
    """Update meta recommendations based on current meta"""
    recommendations = data_loader.get_deck_recommendations()
    
    if not recommendations:
        # First check if there are any decks in the system
        all_decks = data_loader._get_all_deck_keys()
        if not all_decks:
            return html.P("No decks found in the system. Please add some decks first.")
        
        return html.P("No recommendations available. Need more data to generate meaningful recommendations.")
    
    # Create HTML for recommendations
    recommendations_html = html.Div([
        html.H5("Recommended Decks Against Current Meta"),
        html.P("These archetypes have strong matchups against the most popular decks:"),
        html.Ul([
            html.Li([
                html.Strong(f"{rec['archetype']} "),
                f"- Average win rate vs. top meta: {rec['avg_win_rate_vs_meta']:.1%}",
                html.Ul([
                    html.Li(f"{deck['name']} - Win rate: {deck['win_rate']:.1%}")
                    for deck in rec["example_decks"]
                ]) if rec["example_decks"] else ""
            ])
            for rec in recommendations
        ])
    ])
    
    return recommendations_html

# Add meta overview callback
@app.callback(
    [Output("meta-overview", "children"),
     Output("popularity-vs-winrate", "figure")],
    [Input("refresh-button", "n_clicks")]
)
def update_meta_overview(n_clicks):
    """Update meta overview and popularity vs win rate chart"""
    meta_snapshot = data_loader.get_meta_snapshot()
    
    if not meta_snapshot or not meta_snapshot.get("archetypes"):
        return html.P("No meta data available"), go.Figure()
    
    # Create meta overview HTML
    meta_html = html.Div([
        html.H4("Meta Overview"),
        html.P([
            html.Strong("Total Games: "),
            f"{meta_snapshot.get('total_games', 0):,}"
        ]),
        html.P([
            html.Strong("Number of Archetypes: "),
            f"{len(meta_snapshot.get('archetypes', {}))} archetypes with data"
        ]),
        html.P([
            html.Strong("Last Updated: "),
            f"{datetime.datetime.fromtimestamp(meta_snapshot.get('last_updated', 0)).strftime('%Y-%m-%d %H:%M:%S')}"
        ])
    ])
    
    # Create popularity vs win rate scatter plot
    scatter_data = []
    for archetype, data in meta_snapshot.get("archetypes", {}).items():
        if data.get("games", 0) >= 5:  # Minimum sample size
            scatter_data.append({
                "archetype": archetype,
                "meta_share": data.get("meta_share", 0) * 100,  # Convert to percentage
                "win_rate": data.get("win_rate", 0) * 100,      # Convert to percentage
                "games": data.get("games", 0)
            })
    
    if scatter_data:
        scatter_fig = px.scatter(
            pd.DataFrame(scatter_data),
            x="meta_share",
            y="win_rate",
            title="Popularity vs. Win Rate",
            labels={"meta_share": "Meta Share (%)", "win_rate": "Win Rate (%)"},
            text="archetype",
            size="games",
            hover_data=["games"],
            color="win_rate",
            color_continuous_scale=px.colors.diverging.RdYlBu_r
        )
        scatter_fig.update_traces(textposition="top center")
        scatter_fig.add_hline(y=50, line_dash="dash", line_color="gray")
        scatter_fig.add_vline(x=100/len(scatter_data), line_dash="dash", line_color="gray")
    else:
        scatter_fig = go.Figure()
        scatter_fig.add_annotation(
            text="No meta data available",
            showarrow=False,
            font=dict(size=20)
        )
    
    return meta_html, scatter_fig

# API Endpoints
@server.route('/api/decks', methods=['GET'])
def get_deck_list_api():
    """API endpoint to get list of available decks"""
    decks = data_loader.get_deck_list()
    return jsonify(decks)

@server.route('/api/deck/<deck_id>', methods=['GET'])
def get_deck_details_api(deck_id):
    """API endpoint to get details for a specific deck"""
    # Try by name first, then by ID
    deck = data_loader.get_deck_by_name(deck_id)
    if not deck:
        deck = data_loader.get_deck_details(deck_id)
        
    if deck:
        return jsonify(deck)
    return jsonify({"error": "Deck not found"}), 404

@server.route('/api/card/<card_name>', methods=['GET'])
def get_card_details_api(card_name):
    """API endpoint to get details for a specific card by name"""
    card = data_loader.get_card_details(card_name)
    if card:
        return jsonify(card)
    return jsonify({"error": "Card not found"}), 404

@server.route('/api/meta', methods=['GET'])
def get_meta_snapshot_api():
    """API endpoint to get meta snapshot data"""
    meta = data_loader.get_meta_snapshot()
    return jsonify(meta)

@server.route('/api/meta_data', methods=['GET'])
def get_meta_data_raw():
    """API endpoint to get raw meta data file
    This handles decompression of the gzipped meta data file"""
    try:
        meta_path = os.path.join(data_loader.data_path, "meta", "meta_data.json.gz")
        if os.path.exists(meta_path):
            # Read and decompress the file
            with gzip.open(meta_path, 'rt', encoding='utf-8') as f:
                data = json.load(f)
                return jsonify(data)
        else:
            # Try non-gzipped version as fallback
            meta_path = os.path.join(data_loader.data_path, "meta", "meta_data.json")
            if os.path.exists(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return jsonify(data)
            return jsonify({"error": "Meta data file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@server.route('/api/cards', methods=['GET'])
def get_card_stats_api():
    """API endpoint to get card statistics"""
    cards = data_loader.get_card_stats()
    return jsonify(cards)

@server.route('/api/matchups', methods=['GET'])
def get_matchups_api():
    """API endpoint to get matchup matrix data"""
    matchups = data_loader.get_matchup_matrix()
    return jsonify(matchups)

@server.route('/api/synergy', methods=['GET'])
def get_synergy_api():
    """API endpoint to get card synergy data"""
    synergy = data_loader.get_synergy_data()
    return jsonify(synergy)

@server.route('/api/recommendations', methods=['GET'])
def get_recommendations_api():
    """API endpoint to get deck recommendations"""
    recommendations = data_loader.get_deck_recommendations()
    return jsonify(recommendations)

if __name__ == "__main__":
    app.run_server(debug=True)