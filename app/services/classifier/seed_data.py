"""Seed corpus for the k-NN layer (Layer 3).

These examples train the embedding-similarity classifier on representative
market titles for every category we expect to see. Each row is hand-
classified so the k-NN layer has a deterministic ground-truth set without
any pretraining or external API call.

How to add an entry:
  Pick a real (or realistic) market title that's *not* matched by any
  Layer 2 rule today. The whole point of Layer 3 is to catch markets the
  prefix rules miss, so seeding it with examples that Layer 2 already
  catches is wasted capacity. As a sanity check, the unit tests assert
  that every seed is misclassified by Layers 1+2 alone — if a seed gets
  caught by Layer 2, the test will tell you which rule and you should
  either move that seed elsewhere or drop it.

How many seeds are enough:
  ~5 per (category, subcategory) is the typical sweet spot for char-ngram
  TF-IDF on short titles. More than that and we just slow down the k-NN
  search without improving accuracy. If you find a category with
  systematic misclassification, the right move is usually a new Layer 2
  rule, not "more seeds".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seed:
    """One labeled training example for the k-NN layer."""

    title: str
    category: str
    subcategory: str
    tags: tuple[str, ...]


SEEDS: list[Seed] = [
    # ---- Macro ------------------------------------------------------------
    Seed("Will the Federal Reserve raise interest rates this meeting?", "macro", "fed_decision", ("fomc", "scheduled_announcement")),
    Seed("Will the FOMC announce a rate cut at the next meeting?", "macro", "fed_decision", ("fomc", "scheduled_announcement")),
    Seed("Will core CPI come in above 3% next month?", "macro", "cpi", ("cpi", "scheduled_announcement")),
    Seed("Will inflation be above the Fed target this quarter?", "macro", "cpi", ("cpi", "scheduled_announcement")),
    Seed("Will the unemployment rate drop below 4%?", "macro", "jobs", ("nfp", "scheduled_announcement")),
    Seed("Will nonfarm payrolls beat consensus?", "macro", "jobs", ("nfp", "scheduled_announcement")),
    Seed("Will US GDP grow faster than 2% this quarter?", "macro", "gdp", ("gdp", "scheduled_announcement")),

    # ---- Corporate -------------------------------------------------------
    Seed("Will Microsoft acquire Activision before year-end?", "corporate", "merger", ("merger", "single_actor_leverage")),
    Seed("Will the FTC block the proposed merger?", "corporate", "merger", ("merger",)),
    Seed("Will Apple beat earnings expectations next quarter?", "corporate", "earnings", ("earnings", "scheduled_announcement")),
    Seed("Will Tesla report a Q3 earnings miss?", "corporate", "earnings", ("earnings", "scheduled_announcement")),
    Seed("Will the FDA approve drug XYZ this year?", "corporate", "fda", ("fda", "scheduled_announcement")),

    # ---- Judicial --------------------------------------------------------
    Seed("Will the Supreme Court rule on case ABC by June?", "judicial", "scotus", ("scotus", "scheduled_announcement")),
    Seed("Will the SCOTUS uphold the lower court ruling?", "judicial", "scotus", ("scotus", "scheduled_announcement")),
    Seed("Will the federal court issue a ruling this month?", "judicial", "ruling", ("judicial",)),
    Seed("Will the jury return a guilty verdict?", "judicial", "ruling", ("judicial",)),

    # ---- Sports outcomes — game winners ----------------------------------
    Seed("Will the Lakers beat the Celtics tonight?", "sports_outcome", "major_league_game", ("nba",)),
    Seed("Will the Yankees win the series against the Red Sox?", "sports_outcome", "major_league_game", ("mlb",)),
    Seed("Will Manchester United win their match this weekend?", "sports_outcome", "soccer_match", ("soccer",)),
    Seed("Will Djokovic win his quarterfinal match?", "sports_outcome", "tennis_match", ("tennis",)),

    # ---- Sports outcomes — combat ----------------------------------------
    Seed("Will Conor McGregor win his fight on Saturday?", "sports_outcome", "fight_winner", ("ufc", "combat_sport", "single_actor_leverage")),
    Seed("Will fighter X win by knockout?", "sports_outcome", "fight_winner", ("combat_sport", "single_actor_leverage")),
    Seed("Will the boxing match end in a decision?", "sports_outcome", "fight_winner", ("boxing", "combat_sport", "single_actor_leverage")),

    # ---- Sports derivatives ---------------------------------------------
    Seed("Will the Lakers cover the spread tonight?", "sports_derivative", "spread", ("nba", "spread")),
    Seed("Will the Cowboys beat the spread on Sunday?", "sports_derivative", "spread", ("nfl", "spread")),
    Seed("Will the total points be over 220 in the Lakers game?", "sports_derivative", "total", ("nba", "total")),
    Seed("Will the over/under hit on the NFL game?", "sports_derivative", "total", ("nfl", "total")),

    # ---- Sports props ----------------------------------------------------
    Seed("Will LeBron James score over 25 points tonight?", "sports_prop", "player_points", ("nba", "single_actor_leverage")),
    Seed("Will Aaron Judge hit the first home run of the game?", "sports_prop", "first_event", ("mlb", "single_actor_leverage")),
    Seed("Will player X record over 8 assists tonight?", "sports_prop", "player_points", ("nba", "single_actor_leverage")),

    # ---- Crypto strikes --------------------------------------------------
    Seed("Will Bitcoin be above $100k at 3pm ET?", "crypto_strike", "short_window", ("crypto", "btc", "public_underlying")),
    Seed("Will the BTC price close above strike at noon?", "crypto_strike", "short_window", ("crypto", "btc", "public_underlying")),
    Seed("Will Ethereum trade above $4000 at end of day?", "crypto_strike", "daily", ("crypto", "eth", "public_underlying")),
    Seed("Will Solana reach $250 today?", "crypto_strike", "*", ("crypto", "sol", "public_underlying")),

    # ---- Weather ---------------------------------------------------------
    Seed("Will the high temperature in NYC exceed 90F today?", "weather", "temperature", ("weather", "public_underlying")),
    Seed("Will it rain in Seattle on Friday?", "weather", "precipitation", ("weather", "public_underlying")),
    Seed("Will snowfall in Denver exceed 6 inches?", "weather", "precipitation", ("weather", "public_underlying")),

    # ---- Elections -------------------------------------------------------
    Seed("Will candidate X win the Iowa caucus?", "election", "primary", ("election", "primary")),
    Seed("Will the New Hampshire primary be won by candidate Y?", "election", "primary", ("election", "primary")),
    Seed("Who will win the 2028 presidential election?", "election", "general", ("election", "presidential")),
    Seed("Will the Republicans take the Senate majority?", "election", "general", ("election",)),
    Seed("Will the governor of Texas win re-election?", "election", "general", ("election",)),

    # ---- Pop culture -----------------------------------------------------
    Seed("Will Oppenheimer win Best Picture at the Oscars?", "popculture", "awards", ("awards",)),
    Seed("Will Taylor Swift win Album of the Year at the Grammys?", "popculture", "awards", ("awards",)),
    Seed("Will Succession win the Emmy for Best Drama?", "popculture", "awards", ("awards",)),
    Seed("Will the new Marvel film top the box office?", "popculture", "ratings", ("box_office",)),

    # ---- Exotic combos ---------------------------------------------------
    Seed("Will Bitcoin reach $150k AND the S&P drop below 4500 by year-end?", "exotic_combo", "*", ("combinatorial",)),
    Seed("Will the Lakers win AND total points be over 220?", "exotic_combo", "*", ("combinatorial",)),
]
