import React, { useState, useEffect } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, ScatterChart, Scatter, ZAxis } from 'recharts';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Loader } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

const CardGameStatsViewer = () => {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  
  // State for storing actual data
  const [metaData, setMetaData] = useState({
    totalGames: 0,
    lastUpdated: null,
    archetypes: [],
    matchups: [],
    recommendations: []
  });
  
  const [deckList, setDeckList] = useState([]);
  const [selectedDeck, setSelectedDeck] = useState(null);
  const [deckData, setDeckData] = useState(null);
  const [cardStats, setCardStats] = useState([]);
  const [selectedCard, setSelectedCard] = useState(null);
  const [cardData, setCardData] = useState(null);
  
  // Function to load all data
  const loadAllData = async () => {
    setLoading(true);
    setError(null);
    
    try {
      await Promise.all([
        loadMetaData(),
        loadDeckList(),
        loadCardsList()
      ]);
      setLoading(false);
    } catch (err) {
      console.error("Error loading data:", err);
      setError("Failed to load data. Check console for details.");
      setLoading(false);
    }
  };
  
  // Function to load meta data
  const loadMetaData = async () => {
    try {
      // Use the API endpoint that handles decompression on the server
      const response = await fetch('/api/meta_data');
      
      if (!response.ok) {
        throw new Error(`Failed to load meta data: ${response.status} ${response.statusText}`);
      }
      
      const data = await response.json();
      
      // Process archetypes for display
      const archetypeData = [];
      for (const [archetype, info] of Object.entries(data.archetypes || {})) {
        if (info.games >= 5) { // Minimum sample size
          archetypeData.push({
            archetype,
            winRate: info.win_rate || 0,
            metaShare: info.games / Math.max(1, data.total_games),
            games: info.games,
            wins: info.wins || 0,
            losses: info.losses || 0,
            draws: info.draws || 0 // Include draws in archetype data
          });
        }
      }
      
      // Process matchup data
      const matchupRows = [];
      const archetypes = new Set();
      
      // First, collect all archetypes mentioned in matchups
      for (const matchup in data.matchups || {}) {
        const parts = matchup.split('_vs_');
        if (parts.length === 2) {
          archetypes.add(parts[0]);
          archetypes.add(parts[1]);
        }
      }
      
      // Then create matchup rows
      const sortedArchetypes = Array.from(archetypes).sort();
      for (const arch1 of sortedArchetypes) {
        const row = { archetype: arch1 };
        
        for (const arch2 of sortedArchetypes) {
          if (arch1 === arch2) {
            row[`vs_${arch2}`] = 0.5; // Mirror match is 50%
          } else {
            const matchupKey = `${arch1}_vs_${arch2}`;
            const matchupData = (data.matchups || {})[matchupKey];
            row[`vs_${arch2}`] = matchupData ? matchupData.win_rate : 0.5;
          }
        }
        
        matchupRows.push(row);
      }
      
      // Also load recommendations
      const recResponse = await fetch('/api/recommendations');
      let recommendations = [];
      if (recResponse.ok) {
        recommendations = await recResponse.json();
      }
      
      setMetaData({
        totalGames: data.total_games || 0,
        lastUpdated: data.last_updated ? new Date(data.last_updated * 1000) : null,
        archetypes: archetypeData,
        matchups: matchupRows,
        archetypesList: sortedArchetypes,
        recommendations
      });
    } catch (err) {
      console.error("Error loading meta data:", err);
      setError("Failed to load meta data.");
      throw err;
    }
  };
  
  // Function to load cards list
  const loadCardsList = async () => {
    try {
      const response = await fetch('/api/cards');
      if (!response.ok) {
        throw new Error(`Failed to load cards list: ${response.status} ${response.statusText}`);
      }
      
      const cardsData = await response.json();
      
      // Convert from object to array format for UI
      const cardList = Object.entries(cardsData).map(([cardName, cardInfo]) => ({
        name: cardName,
        id: cardInfo.id, // May be undefined for name-only cards
        winRate: cardInfo.win_rate || 0,
        games: cardInfo.games_played || 0
      })).filter(card => card.games >= 5); // Only cards with enough data
      
      setCardStats(cardList);
      
      // If cards were loaded and no card is selected, select the first one
      if (cardList.length > 0 && !selectedCard) {
        setSelectedCard(cardList[0].name);
        loadCardDetails(cardList[0].name);
      }
    } catch (err) {
      console.error("Error loading cards list:", err);
      setError("Failed to load cards list.");
      throw err;
    }
  };
  
  // Function to load deck list
  const loadDeckList = async () => {
    try {
      const response = await fetch('/api/decks');
      if (!response.ok) {
          throw new Error(`Failed to load deck list: ${response.status} ${response.statusText}`);
      }
      const decks = await response.json();
      setDeckList(decks);
      
      // If decks were loaded and no deck is selected, select the first one
      if (decks.length > 0 && !selectedDeck) {
        // Use name as identifier (new format)
        setSelectedDeck(decks[0].name);
        loadDeckDetails(decks[0].name);
      }
    } catch (err) {
      console.error("Error loading deck list:", err);
      setError("Failed to load deck list.");
      throw err;
    }
  };
  
  // Function to load deck details - now using deck name
    const loadDeckDetails = async (deckName) => {
      if (!deckName) return;
      
      setLoading(true);
      try {
        // First try using the new endpoint format with deck name
        const response = await fetch(`/api/deck/${encodeURIComponent(deckName)}`);
        
        if (!response.ok) {
          throw new Error(`Failed to load deck details: ${response.status} ${response.statusText}`);
        }
        
        const data = await response.json();
        
        // Process mana curve data
        const manaCurve = [];
        const cards = data.card_list || [];
        
        // Group cards by mana cost
        const costCounts = {};
        cards.forEach(card => {
          // Try to get cost for each card - may use CMC or cost property
          const cost = card.cost || card.cmc || 0;
          const costBucket = cost >= 7 ? '7+' : String(Math.floor(cost));
          
          if (!costCounts[costBucket]) {
            costCounts[costBucket] = 0;
          }
          costCounts[costBucket] += card.count || 1;
        });
        
        // Convert to array for chart
        for (const [cost, count] of Object.entries(costCounts)) {
          manaCurve.push({ cost, count });
        }
        
        // Sort by mana cost
        manaCurve.sort((a, b) => {
          const costA = a.cost === '7+' ? 7 : parseInt(a.cost);
          const costB = b.cost === '7+' ? 7 : parseInt(b.cost);
          return costA - costB;
        });
        
        // Process card performance data - try name-based first, then ID-based
        const cardPerformance = [];
        
        if (data.card_performance_by_name) {
          // Name-based card performance (new format)
          for (const [cardName, cardInfo] of Object.entries(data.card_performance_by_name)) {
            cardPerformance.push({
              name: cardName,
              winRate: cardInfo.win_rate || 0,
              playCount: cardInfo.usage_count || 0,
              drawCount: cardInfo.games_drawn || 0,
              gamesPlayed: cardInfo.games_played || 0
            });
          }
        } else if (data.card_performance) {
          // ID-based card performance (old format)
          for (const [cardId, cardInfo] of Object.entries(data.card_performance)) {
            cardPerformance.push({
              id: cardId,
              name: cardInfo.name || `Card ${cardId}`,
              winRate: cardInfo.win_rate || 0,
              playCount: cardInfo.usage_count || 0,
              drawCount: cardInfo.draw_count || 0,
              gamesPlayed: cardInfo.games_played || 0
            });
          }
        }
        
        // Sort by win rate
        cardPerformance.sort((a, b) => b.winRate - a.winRate);
        
        // Process performance by game stage
        const stagePerformance = [];
        if (data.performance_by_stage) {
          for (const [stage, stageData] of Object.entries(data.performance_by_stage)) {
            // Include draws in the total calculation
            const total = stageData.wins + stageData.losses + (stageData.draws || 0);
            if (total > 0) {
              stagePerformance.push({
                stage: stage.charAt(0).toUpperCase() + stage.slice(1),
                winRate: stageData.wins / total,
                wins: stageData.wins,
                losses: stageData.losses,
                draws: stageData.draws || 0,
                total
              });
            }
          }
        }
        
        setDeckData({
          id: data.deck_id || '',
          name: data.name || deckName,
          archetype: data.archetype || 'Unknown',
          games: data.games || 0,
          wins: data.wins || 0,
          losses: data.losses || 0,
          draws: data.draws || 0, // Include draws in the deck data
          winRate: data.win_rate || 0,
          manaCurve,
          cardPerformance,
          stagePerformance
        });
        
        setLoading(false);
      } catch (err) {
        console.error("Error loading deck details:", err);
        setError("Failed to load deck details.");
        setLoading(false);
      }
    };
  
  // Function to load card details - now using card name
  const loadCardDetails = async (cardName) => {
    if (!cardName) return;
    
    setLoading(true);
    try {
      // Use the name-based endpoint
      const response = await fetch(`/api/card/${encodeURIComponent(cardName)}`);
      
      if (!response.ok) {
        throw new Error(`Failed to load card data: ${response.status} ${response.statusText}`);
      }
      
      const cardInfo = await response.json();
      
      if (!cardInfo) {
        throw new Error(`Card not found: ${cardName}`);
      }
      
      // Find which decks use this card by checking all decks
      const decksWithCard = [];
      const deckPromises = deckList.map(async (deck) => {
        const deckResponse = await fetch(`/api/deck/${encodeURIComponent(deck.id)}`);
        if (deckResponse.ok) {
          const deckData = await deckResponse.json();
          
          // Check card_list for this card
          const cardEntry = (deckData.card_list || []).find(card => 
            card.name === cardName || (cardInfo.id && card.id === cardInfo.id)
          );
          
          if (cardEntry) {
            // Look up performance data in card_performance_by_name or card_performance
            let performance = null;
            if (deckData.card_performance_by_name && deckData.card_performance_by_name[cardName]) {
              performance = deckData.card_performance_by_name[cardName];
            } else if (cardInfo.id && deckData.card_performance && deckData.card_performance[cardInfo.id]) {
              performance = deckData.card_performance[cardInfo.id];
            }
            
            decksWithCard.push({
              deckId: deck.id,
              deckName: deck.name,
              count: cardEntry.count || 0,
              winRate: performance ? performance.win_rate || 0 : 0
            });
          }
        }
      });
      
      // Wait for all deck checks to complete
      await Promise.all(deckPromises);
      
      // Process card performance by game stage
      const stageData = [];
      if (cardInfo.by_game_stage) {
        for (const [stage, data] of Object.entries(cardInfo.by_game_stage)) {
          if (data.games > 0) {
            stageData.push({
              stage: stage.charAt(0).toUpperCase() + stage.slice(1),
              winRate: data.wins / data.games,
              games: data.games,
              wins: data.wins
            });
          }
        }
      }
      
      // Process card performance by game state
      const stateData = [];
      if (cardInfo.by_game_state) {
        for (const [state, data] of Object.entries(cardInfo.by_game_state)) {
          if (data.games > 0) {
            stateData.push({
              state: state.charAt(0).toUpperCase() + state.slice(1),
              winRate: data.wins / data.games,
              games: data.games,
              wins: data.wins
            });
          }
        }
      }
      
      // Process archetype performance
      const archetypeData = [];
      if (cardInfo.archetypes) {
        for (const [archetype, games] of Object.entries(cardInfo.archetypes)) {
          archetypeData.push({
            archetype,
            count: games
          });
        }
        // Sort by count
        archetypeData.sort((a, b) => b.count - a.count);
      }
      
      setCardData({
        name: cardName,
        id: cardInfo.id,
        winRate: cardInfo.win_rate || 0,
        games: cardInfo.games_played || 0,
        decks: decksWithCard,
        stageData,
        stateData,
        archetypeData
      });
      
      setLoading(false);
    } catch (err) {
      console.error("Error loading card details:", err);
      setError("Failed to load card details.");
      setLoading(false);
    }
  };
  
  // Load data on component mount
  useEffect(() => {
    // Using setTimeout to let the component mount first
    // This can help with potential issues during development
    const timer = setTimeout(() => {
      loadAllData();
    }, 500);
    
    return () => clearTimeout(timer);
  }, []);
  
  // Load deck details when selected deck changes
  useEffect(() => {
    if (selectedDeck) {
      loadDeckDetails(selectedDeck);
    }
  }, [selectedDeck]);
  
  // Load card details when selected card changes
  useEffect(() => {
    if (selectedCard) {
      loadCardDetails(selectedCard);
    }
  }, [selectedCard]);
  
  // Format functions
  const formatDate = (date) => {
    if (!date) return 'Unknown';
    return date.toLocaleString();
  };
  
  const formatPercent = (value) => {
    return `${(value * 100).toFixed(1)}%`;
  };
  
  // Helper for formatting record with draws
  const formatRecord = (wins, losses, draws) => {
    if (typeof draws !== 'undefined' && draws > 0) {
      return `${wins}W / ${losses}L / ${draws}D`;
    }
    return `${wins}W / ${losses}L`;
  };
  
  // Find best win rate archetype
  const getBestWinRateArchetype = () => {
    if (!metaData.archetypes || metaData.archetypes.length === 0) return { name: 'Unknown', rate: 0 };
    
    const filtered = metaData.archetypes.filter(a => a.games >= 10); // Minimum sample size
    if (filtered.length === 0) return { name: 'Unknown', rate: 0 };
    
    const best = filtered.reduce((prev, current) => {
      return (prev.winRate > current.winRate) ? prev : current;
    });
    
    return { name: best.archetype, rate: best.winRate };
  };
  
  // Find top archetype by meta share
  const getTopArchetype = () => {
    if (!metaData.archetypes || metaData.archetypes.length === 0) return { name: 'Unknown', share: 0 };
    
    const top = metaData.archetypes.reduce((prev, current) => {
      return (prev.metaShare > current.metaShare) ? prev : current;
    });
    
    return { name: top.archetype, share: top.metaShare };
  };

  // Handle refresh button click
  const handleRefresh = () => {
    loadAllData();
  };
  
  // Render
  return (
    <div className="p-4 bg-gray-100 min-h-screen">
      <h1 className="text-center mb-6 text-blue-800 font-bold text-2xl">
        Card Game Statistics Viewer
      </h1>
      
      {loading && !metaData.archetypes.length ? (
        <div className="flex justify-center items-center h-64">
          <Loader className="h-12 w-12 animate-spin text-blue-600" />
          <p className="ml-4 text-xl">Loading data...</p>
        </div>
      ) : error ? (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
          <p><strong>Error:</strong> {error}</p>
        </div>
      ) : (
        <Tabs value={activeTab} onValueChange={setActiveTab} className="mb-6">
          <TabsList className="grid grid-cols-5 w-full">
            <TabsTrigger value="dashboard">Dashboard</TabsTrigger>
            <TabsTrigger value="deck">Deck Analysis</TabsTrigger>
            <TabsTrigger value="cards">Card Performance</TabsTrigger>
            <TabsTrigger value="meta">Meta Analysis</TabsTrigger>
            <TabsTrigger value="recommendations">Recommendations</TabsTrigger>
          </TabsList>

          <TabsContent value="dashboard">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Total Games</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="font-bold text-blue-600 text-3xl">{metaData.totalGames.toLocaleString()}</p>
                </CardContent>
              </Card>
              
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Top Archetype</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="font-bold text-blue-600 text-2xl">{getTopArchetype().name}</p>
                  <p className="text-gray-500 text-sm">{formatPercent(getTopArchetype().share)} of meta</p>
                </CardContent>
              </Card>
              
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Best Win Rate</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="font-bold text-blue-600 text-2xl">{formatPercent(getBestWinRateArchetype().rate)}</p>
                  <p className="text-gray-500 text-sm">{getBestWinRateArchetype().name} archetype</p>
                </CardContent>
              </Card>
              
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Last Updated</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="font-bold text-blue-600 text-2xl">{metaData.lastUpdated ? formatDate(metaData.lastUpdated).split(',')[0] : 'Unknown'}</p>
                  <p className="text-gray-500 text-sm">{metaData.lastUpdated ? formatDate(metaData.lastUpdated).split(',')[1] : ''}</p>
                </CardContent>
              </Card>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Meta Composition</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-80">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={metaData.archetypes}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="archetype" />
                        <YAxis tickFormatter={(value) => `${(value * 100).toFixed(0)}%`} />
                        <Tooltip formatter={(value) => `${(value * 100).toFixed(1)}%`} />
                        <Legend />
                        <Bar dataKey="metaShare" name="Meta Share" fill="#8884d8" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </CardContent>
              </Card>
              
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Win Rates by Archetype</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-80">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={metaData.archetypes}>
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis dataKey="archetype" />
                        <YAxis tickFormatter={(value) => `${(value * 100).toFixed(0)}%`} />
                        <Tooltip formatter={(value) => `${(value * 100).toFixed(1)}%`} />
                        <Legend />
                        <Bar dataKey="winRate" name="Win Rate" fill="#82ca9d" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </CardContent>
              </Card>
            </div>

            <div className="mt-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-gray-700">Matchup Heatmap</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse">
                      <thead>
                        <tr>
                          <th className="font-bold p-2 border text-left">VS</th>
                          {metaData.archetypesList?.map(archetype => (
                            <th key={archetype} className="font-bold p-2 border text-left">{archetype}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {metaData.matchups.map((row) => (
                          <tr key={row.archetype}>
                            <td className="font-bold p-2 border">{row.archetype}</td>
                            {metaData.archetypesList?.map(archetype => {
                              const winRate = row[`vs_${archetype}`];
                              return (
                                <td 
                                  key={archetype}
                                  className={`p-2 border ${
                                    winRate > 0.53 ? 'bg-green-100' : 
                                    winRate < 0.47 ? 'bg-red-100' : 
                                    'bg-gray-100'
                                  }`}
                                >
                                  {formatPercent(winRate)}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="deck">
            <div className="mb-4">
              <label className="block text-gray-700 text-sm font-bold mb-2">
                Select Deck:
              </label>
              <Select 
                value={selectedDeck || ''} 
                onValueChange={setSelectedDeck}
              >
                <SelectTrigger className="w-full md:w-64">
                  <SelectValue placeholder="Select a deck" />
                </SelectTrigger>
                <SelectContent>
                  {deckList.map((deck) => (
                    <SelectItem key={deck.id} value={deck.name}>
                      {deck.name} ({deck.archetype})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            
            {loading ? (
              <div className="flex justify-center items-center h-64">
                <Loader className="h-8 w-8 animate-spin text-blue-600" />
                <p className="ml-4">Loading deck data...</p>
              </div>
            ) : deckData ? (
              <>
                <div className="grid grid-cols-1 md:grid-cols-12 gap-4">
                  <div className="md:col-span-4">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-blue-700">{deckData.name}</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <p className="mb-4 text-gray-600">{deckData.archetype}</p>
                      
                      <div className="flex justify-between mb-2">
                        <p>Games: {deckData.games}</p>
                        <p>Win Rate: {formatPercent(deckData.winRate)}</p>
                      </div>
                      
                      <div className="flex justify-between">
                        <p>Record:</p>
                        <p className="font-bold">
                          {deckData.wins}W / {deckData.losses}L
                          {deckData.draws > 0 ? ` / ${deckData.draws}D` : ''}
                        </p>
                      </div>
                      
                      {deckData.stagePerformance && deckData.stagePerformance.length > 0 && (
                        <>
                          <h4 className="mt-4 mb-2 font-semibold">Performance by Game Stage</h4>
                          <table className="w-full text-sm">
                            <thead>
                              <tr>
                                <th className="text-left">Stage</th>
                                <th className="text-right">Win Rate</th>
                                <th className="text-right">W/L/D</th>
                              </tr>
                            </thead>
                            <tbody>
                              {deckData.stagePerformance.map(stage => (
                                <tr key={stage.stage}>
                                  <td>{stage.stage}</td>
                                  <td className="text-right">{formatPercent(stage.winRate)}</td>
                                  <td className="text-right">
                                    {stage.wins}/{stage.losses}
                                    {typeof stage.draws !== 'undefined' ? `/${stage.draws}` : ''}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </>
                      )}
                    </CardContent>
                  </Card>
                  </div>
                  
                  <div className="md:col-span-8">
                    <Card>
                      <CardHeader>
                        <CardTitle className="text-gray-700">Mana Curve</CardTitle>
                      </CardHeader>
                      <CardContent>
                        <div className="h-80">
                          <ResponsiveContainer width="100%" height="100%">
                            <BarChart data={deckData.manaCurve}>
                              <CartesianGrid strokeDasharray="3 3" />
                              <XAxis dataKey="cost" />
                              <YAxis />
                              <Tooltip />
                              <Legend />
                              <Bar dataKey="count" name="Card Count" fill="#8884d8" />
                            </BarChart>
                          </ResponsiveContainer>
                        </div>
                      </CardContent>
                    </Card>
                  </div>
                </div>

                <div className="mt-4">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-gray-700">Card Performance</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <table className="w-full border-collapse">
                        <thead>
                          <tr>
                            <th className="font-bold p-2 border text-left">Card Name</th>
                            <th className="font-bold p-2 border text-left">Win Rate</th>
                            <th className="font-bold p-2 border text-left">Played Count</th>
                            <th className="font-bold p-2 border text-left">Games</th>
                          </tr>
                        </thead>
                        <tbody>
                          {deckData.cardPerformance.map((card) => (
                            <tr key={card.id || card.name}>
                              <td className="p-2 border">{card.name}</td>
                              <td className="p-2 border">{formatPercent(card.winRate)}</td>
                              <td className="p-2 border">{card.playCount}</td>
                              <td className="p-2 border">{card.gamesPlayed}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </CardContent>
                  </Card>
                </div>
              </>
            ) : (
              <div className="text-center p-8 bg-gray-100 rounded">
                <p>Select a deck to view detailed analysis</p>
              </div>
            )}
          </TabsContent>

          <TabsContent value="cards">
            <div className="mb-4">
              <label className="block text-gray-700 text-sm font-bold mb-2">
                Select Card:
              </label>
              <Select 
                value={selectedCard || ''} 
                onValueChange={setSelectedCard}
              >
                <SelectTrigger className="w-full md:w-64">
                  <SelectValue placeholder="Select a card" />
                </SelectTrigger>
                <SelectContent>
                  {cardStats.map((card) => (
                    <SelectItem key={card.id || card.name} value={card.name}>
                      {card.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            
            {loading ? (
              <div className="flex justify-center items-center h-64">
                <Loader className="h-8 w-8 animate-spin text-blue-600" />
                <p className="ml-4">Loading card data...</p>
              </div>
            ) : cardData ? (
              <>
                <div className="grid grid-cols-1 md:grid-cols-12 gap-4">
                  <div className="md:col-span-4">
                    <Card>
                      <CardHeader>
                        <CardTitle className="text-blue-700">{cardData.name}</CardTitle>
                      </CardHeader>
                      <CardContent>
                        <div className="flex justify-between mb-2">
                          <p>Win Rate:</p>
                          <p className="font-bold">{formatPercent(cardData.winRate)}</p>
                        </div>
                        
                        <div className="flex justify-between">
                          <p>Games:</p>
                          <p className="font-bold">{cardData.games}</p>
                        </div>
                        
                        {cardData.archetypeData && cardData.archetypeData.length > 0 && (
                          <>
                            <h4 className="mt-4 mb-2 font-semibold">Archetype Distribution</h4>
                            <table className="w-full text-sm">
                              <thead>
                                <tr>
                                  <th className="text-left">Archetype</th>
                                  <th className="text-right">Count</th>
                                </tr>
                              </thead>
                              <tbody>
                                {cardData.archetypeData.slice(0, 5).map(arch => (
                                  <tr key={arch.archetype}>
                                    <td>{arch.archetype}</td>
                                    <td className="text-right">{arch.count}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </>
                        )}
                      </CardContent>
                    </Card>
                  </div>
                  
                  <div className="md:col-span-8">
                    <Card>
                      <CardHeader>
                        <CardTitle className="text-gray-700">Performance by Game Stage</CardTitle>
                      </CardHeader>
                      <CardContent>
                        <div className="h-80">
                          {cardData.stageData && cardData.stageData.length > 0 ? (
                            <ResponsiveContainer width="100%" height="100%">
                              <BarChart data={cardData.stageData}>
                                <CartesianGrid strokeDasharray="3 3" />
                                <XAxis dataKey="stage" />
                                <YAxis yAxisId="left" tickFormatter={(value) => `${(value * 100).toFixed(0)}%`} />
                                <YAxis yAxisId="right" orientation="right" />
                                <Tooltip formatter={(value, name) => 
                                  name === "winRate" ? formatPercent(value) : value
                                } />
                                <Legend />
                                <Bar yAxisId="left" dataKey="winRate" name="Win Rate" fill="#8884d8" />
                                <Bar yAxisId="right" dataKey="games" name="Games" fill="#82ca9d" />
                              </BarChart>
                            </ResponsiveContainer>
                          ) : (
                            <div className="flex justify-center items-center h-full">
                              <p>No game stage data available</p>
                            </div>
                          )}
                        </div>
                      </CardContent>
                    </Card>
                  </div>
                </div>

                <div className="mt-4">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-gray-700">Decks Using This Card</CardTitle>
                    </CardHeader>
                    <CardContent>
                      {cardData.decks && cardData.decks.length > 0 ? (
                        <table className="w-full border-collapse">
                          <thead>
                            <tr>
                              <th className="font-bold p-2 border text-left">Deck Name</th>
                              <th className="font-bold p-2 border text-left">Card Count</th>
                              <th className="font-bold p-2 border text-left">Win Rate in Deck</th>
                            </tr>
                          </thead>
                          <tbody>
                            {cardData.decks.map((deck) => (
                              <tr key={deck.deckId}>
                                <td className="p-2 border">{deck.deckName}</td>
                                <td className="p-2 border">{deck.count}</td>
                                <td className="p-2 border">{formatPercent(deck.winRate)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ) : (
                        <p>No deck data available for this card</p>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </>
            ) : (
              <div className="text-center p-8 bg-gray-100 rounded">
                <p>Select a card to view detailed analysis</p>
              </div>
            )}
          </TabsContent>

          <TabsContent value="meta">
            <Card>
              <CardHeader>
                <CardTitle className="text-gray-700">Meta Overview</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <h3 className="font-bold mb-2">Archetype Win Rates</h3>
                  <table className="w-full border-collapse">
                    <thead>
                      <tr>
                        <th className="font-bold p-2 border text-left">Archetype</th>
                        <th className="font-bold p-2 border text-left">Games</th>
                        <th className="font-bold p-2 border text-left">Win Rate</th>
                        <th className="font-bold p-2 border text-left">Record (W/L/D)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metaData.archetypes
                        .sort((a, b) => b.winRate - a.winRate)
                        .map((arch) => (
                          <tr key={arch.archetype}>
                            <td className="p-2 border">{arch.archetype}</td>
                            <td className="p-2 border">{arch.games}</td>
                            <td className="p-2 border">{formatPercent(arch.winRate)}</td>
                            <td className="p-2 border">
                              {arch.wins || 0}/{arch.losses || 0}/{arch.draws || 0}
                            </td>
                          </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                  
                  <div>
                    <h3 className="font-bold mb-2">Meta Share</h3>
                    <table className="w-full border-collapse">
                      <thead>
                        <tr>
                          <th className="font-bold p-2 border text-left">Archetype</th>
                          <th className="font-bold p-2 border text-left">Meta Share</th>
                        </tr>
                      </thead>
                      <tbody>
                        {metaData.archetypes
                          .sort((a, b) => b.metaShare - a.metaShare)
                          .map((arch) => (
                            <tr key={arch.archetype}>
                              <td className="p-2 border">{arch.archetype}</td>
                              <td className="p-2 border">{formatPercent(arch.metaShare)}</td>
                            </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
                
                <div className="mt-6">
                  <h3 className="font-bold mb-2">Win Rate vs. Meta Share</h3>
                  <div className="h-80">
                    <ResponsiveContainer width="100%" height="100%">
                      <ScatterChart
                        margin={{ top: 20, right: 20, bottom: 20, left: 20 }}
                      >
                        <CartesianGrid />
                        <XAxis 
                          type="number" 
                          dataKey="metaShare" 
                          name="Meta Share" 
                          unit="%" 
                          tickFormatter={(value) => `${(value * 100).toFixed(0)}%`}
                        />
                        <YAxis 
                          type="number" 
                          dataKey="winRate" 
                          name="Win Rate" 
                          unit="%" 
                          tickFormatter={(value) => `${(value * 100).toFixed(0)}%`}
                        />
                        <ZAxis type="number" dataKey="games" range={[50, 400]} name="Games" />
                        <Tooltip 
                          formatter={(value, name) => [
                            name.includes("Rate") ? formatPercent(value) : 
                            name.includes("Share") ? formatPercent(value) : 
                            value,
                            name
                          ]}
                          labelFormatter={(label) => `Archetype: ${label}`}
                        />
                        <Legend />
                        <Scatter 
                          name="Archetypes" 
                          data={metaData.archetypes.map(a => ({...a, name: a.archetype}))} 
                          fill="#8884d8" 
                        />
                      </ScatterChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="recommendations">
            <Card>
              <CardHeader>
                <CardTitle className="text-gray-700">Meta Recommendations</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="mb-4">
                  <h3 className="font-bold mb-2">Current Best Decks</h3>
                  <p className="mb-4">Based on win rate and meta share, consider playing these decks:</p>
                  
                  <ul className="list-disc pl-5">
                    {metaData.archetypes
                      .filter(a => a.games >= 20) // Only recommend with significant sample size
                      .sort((a, b) => b.winRate - a.winRate)
                      .slice(0, 3)
                      .map((arch) => (
                        <li key={arch.archetype} className="mb-1">
                          <span className="font-medium">{arch.archetype}</span> - 
                          Win Rate: {formatPercent(arch.winRate)}, 
                          Meta Share: {formatPercent(arch.metaShare)}
                        </li>
                    ))}
                  </ul>
                </div>
                
                <div>
                  <h3 className="font-bold mb-2">Positioning Against the Meta</h3>
                  <p className="mb-4">Based on matchup data, these decks counter the most popular archetypes:</p>
                  
                  {metaData.recommendations && metaData.recommendations.length > 0 ? (
                    <ul className="list-disc pl-5">
                      {metaData.recommendations.map(rec => (
                        <li key={rec.archetype} className="mb-2">
                          <span className="font-medium">{rec.archetype}</span> - 
                          Win rate vs. top meta: {formatPercent(rec.avg_win_rate_vs_meta)}
                          {rec.example_decks && rec.example_decks.length > 0 && (
                            <ul className="list-circle pl-5 mt-1">
                              {rec.example_decks.map(deck => (
                                <li key={deck.id}>
                                  {deck.name} - Win rate: {formatPercent(deck.win_rate)}
                                </li>
                              ))}
                            </ul>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>No specific recommendations available based on current data.</p>
                  )}
                </div>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      )}
      
      <div className="mt-6 text-center">
        <Button 
          variant="default" 
          className="bg-blue-600 hover:bg-blue-700"
          onClick={handleRefresh}
          disabled={loading}
        >
          {loading ? (
            <>
              <Loader className="h-4 w-4 animate-spin mr-2" />
              Refreshing...
            </>
          ) : 'Refresh Data'}
        </Button>
      </div>
      
      <div className="mt-6 text-center text-gray-500 text-sm">
        <p>Card Game Statistics Viewer - Powered by DeckStatsTracker</p>
        <p>Data last refreshed: {metaData.lastUpdated ? formatDate(metaData.lastUpdated) : 'Never'}</p>
      </div>
    </div>
  );
};

export default CardGameStatsViewer;