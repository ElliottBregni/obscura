#!/usr/bin/env python3
"""
Analyze DDOG data from the successful Yahoo Finance API call
"""
import json
from datetime import datetime

# Data we successfully retrieved
quote_data = {
    "currency": "USD",
    "symbol": "DDOG",
    "regularMarketPrice": 115.66,
    "fiftyTwoWeekHigh": 201.69,
    "fiftyTwoWeekLow": 81.63,
    "regularMarketDayHigh": 123.06,
    "regularMarketDayLow": 114.78,
    "regularMarketVolume": 4549091,
    "previousClose": 120.600006,
    "longName": "Datadog, Inc.",
    "exchangeName": "NasdaqGS"
}

price = quote_data['regularMarketPrice']
prev_close = quote_data['previousClose']
day_change = price - prev_close
day_change_pct = (price / prev_close - 1) * 100

print("="*80)
print("📊 DATADOG (DDOG) OPTIONS TRADING RESEARCH")
print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
print("="*80)

print(f"\n{'='*80}")
print("PHASE 1: CURRENT MARKET SNAPSHOT")
print(f"{'='*80}")

print(f"\n💰 Stock Price:")
print(f"   Current: ${price:.2f}")
print(f"   Previous Close: ${prev_close:.2f}")
print(f"   Change: ${day_change:.2f} ({day_change_pct:+.2f}%)")
print(f"   Day Range: ${quote_data['regularMarketDayLow']:.2f} - ${quote_data['regularMarketDayHigh']:.2f}")
print(f"   52-Week Range: ${quote_data['fiftyTwoWeekLow']:.2f} - ${quote_data['fiftyTwoWeekHigh']:.2f}")

print(f"\n📈 Trading Activity:")
print(f"   Volume: {quote_data['regularMarketVolume']:,} shares")
print(f"   Exchange: {quote_data['exchangeName']}")

# Calculate some key levels
support_levels = [
    quote_data['fiftyTwoWeekLow'],
    110.0,  # Psychological level
    price * 0.95,  # 5% below current
]

resistance_levels = [
    price * 1.05,  # 5% above current
    130.0,  # Psychological level  
    quote_data['fiftyTwoWeekHigh'],
]

print(f"\n📍 Key Technical Levels:")
print(f"   Support Levels: ${support_levels[0]:.2f}, ${support_levels[1]:.2f}, ${support_levels[2]:.2f}")
print(f"   Resistance Levels: ${resistance_levels[0]:.2f}, ${resistance_levels[1]:.2f}, ${resistance_levels[2]:.2f}")

# Distance from 52-week extremes
pct_from_high = (price / quote_data['fiftyTwoWeekHigh'] - 1) * 100
pct_from_low = (price / quote_data['fiftyTwoWeekLow'] - 1) * 100

print(f"\n📏 Position in 52-Week Range:")
print(f"   From 52W High: {pct_from_high:.1f}% (${quote_data['fiftyTwoWeekHigh'] - price:.2f} below)")
print(f"   From 52W Low: {pct_from_low:+.1f}% (${price - quote_data['fiftyTwoWeekLow']:.2f} above)")

range_position = (price - quote_data['fiftyTwoWeekLow']) / (quote_data['fiftyTwoWeekHigh'] - quote_data['fiftyTwoWeekLow']) * 100
print(f"   Position in Range: {range_position:.1f}% (0% = low, 100% = high)")

print(f"\n{'='*80}")
print("PHASE 2: OPTIONS STRATEGY RECOMMENDATIONS")
print(f"{'='*80}")

# Based on current price action (down 4% today)
print(f"\n📉 Market Context: DDOG is down {abs(day_change_pct):.1f}% today")
print(f"   Current price (${price:.2f}) is {100-range_position:.1f}% below 52-week high")
print(f"   This represents a potential opportunity OR continued weakness")

print(f"\n{'='*80}")
print("STRATEGY 1: Bullish Call Debit Spread (Moderate Bullish)")
print(f"{'='*80}")
buy_call_strike = round(price * 1.02 / 5) * 5  # Round to nearest $5
sell_call_strike = round(price * 1.10 / 5) * 5
print(f"   BUY: ${buy_call_strike:.0f} Call")
print(f"   SELL: ${sell_call_strike:.0f} Call")
print(f"   Expiration: 30-45 DTE (Days To Expiration)")
print(f"\n   Rationale:")
print(f"   • Stock is down {abs(day_change_pct):.1f}% - potential bounce opportunity")
print(f"   • Limited risk if downtrend continues")
print(f"   • Max profit if DDOG recovers to ${sell_call_strike}")
print(f"\n   Estimated Metrics:")
print(f"   • Max Risk: ~$200-400 per spread")
print(f"   • Max Profit: ~${(sell_call_strike - buy_call_strike) * 100 - 300:.0f} per spread")
print(f"   • Breakeven: ~${buy_call_strike + 3:.0f}")

print(f"\n{'='*80}")
print("STRATEGY 2: Bear Put Spread (Moderate Bearish)")
print(f"{'='*80}")
buy_put_strike = round(price * 0.98 / 5) * 5
sell_put_strike = round(price * 0.90 / 5) * 5
print(f"   BUY: ${buy_put_strike:.0f} Put")
print(f"   SELL: ${sell_put_strike:.0f} Put")
print(f"   Expiration: 30-45 DTE")
print(f"\n   Rationale:")
print(f"   • Today's {abs(day_change_pct):.1f}% drop could signal more downside")
print(f"   • Stock is {abs(pct_from_high):.1f}% below 52W high - bearish momentum")
print(f"   • Limited risk if stock reverses")
print(f"\n   Estimated Metrics:")
print(f"   • Max Risk: ~$200-400 per spread")
print(f"   • Max Profit: ~${(buy_put_strike - sell_put_strike) * 100 - 300:.0f} per spread")
print(f"   • Breakeven: ~${buy_put_strike - 3:.0f}")

print(f"\n{'='*80}")
print("STRATEGY 3: Iron Condor (Neutral/Range-Bound)")
print(f"{'='*80}")
sell_call = round(price * 1.08 / 5) * 5
buy_call = round(price * 1.15 / 5) * 5
sell_put = round(price * 0.92 / 5) * 5
buy_put = round(price * 0.85 / 5) * 5
print(f"   CALL SPREAD: Sell ${sell_call:.0f} / Buy ${buy_call:.0f}")
print(f"   PUT SPREAD: Sell ${sell_put:.0f} / Buy ${buy_put:.0f}")
print(f"   Expiration: 30-45 DTE")
print(f"\n   Rationale:")
print(f"   • Collect premium if DDOG trades in ${sell_put:.0f}-${sell_call:.0f} range")
print(f"   • That's a {((sell_call-sell_put)/price)*100:.1f}% range from current price")
print(f"   • Good for sideways markets")
print(f"\n   Estimated Metrics:")
print(f"   • Max Risk: ~$400-500 per condor")
print(f"   • Max Profit: ~$150-250 (credit collected)")
print(f"   • Profit Range: ${sell_put:.0f} - ${sell_call:.0f}")

print(f"\n{'='*80}")
print("STRATEGY 4: Cash-Secured Put (Bullish Income)")
print(f"{'='*80}")
csp_strike = round(price * 0.93 / 5) * 5
print(f"   SELL: ${csp_strike:.0f} Put")
print(f"   Expiration: 30-45 DTE")
print(f"\n   Rationale:")
print(f"   • Get paid to potentially buy DDOG at ${csp_strike:.0f}")
print(f"   • That's {((csp_strike/price)-1)*100:.1f}% below current price")
print(f"   • Only use if you WANT to own DDOG long-term")
print(f"\n   Capital Required:")
print(f"   • ${csp_strike * 100:.0f} per contract (cash secured)")
print(f"\n   Estimated Premium:")
print(f"   • ~${csp_strike * 2:.0f}-${csp_strike * 5:.0f} per contract")
print(f"   • ~{((csp_strike * 3) / (csp_strike * 100)) * 100 * 12:.1f}% annualized return")

print(f"\n{'='*80}")
print("STRATEGY 5: Covered Call (If You Own DDOG)")
print(f"{'='*80}")
cc_strike = round(price * 1.10 / 5) * 5
print(f"   OWN: 100 shares of DDOG at ${price:.2f}")
print(f"   SELL: ${cc_strike:.0f} Call")
print(f"   Expiration: 30-45 DTE")
print(f"\n   Rationale:")
print(f"   • Generate income on existing position")
print(f"   • Willing to sell at ${cc_strike:.0f} ({((cc_strike/price)-1)*100:.1f}% upside)")
print(f"   • Keep premium if stock stays below ${cc_strike:.0f}")
print(f"\n   Estimated Premium:")
print(f"   • ~${price * 1.5:.0f}-${price * 3:.0f} per contract")

print(f"\n{'='*80}")
print("⚠️  CRITICAL RISK CONSIDERATIONS")
print(f"{'='*80}")
print(f"\n1. EARNINGS DATE:")
print(f"   • Always check next earnings date before trading!")
print(f"   • Implied Volatility spikes before earnings = expensive options")
print(f"   • Decide: Trade BEFORE earnings (exit early) or AFTER (lower IV)")
print(f"\n2. IMPLIED VOLATILITY:")
print(f"   • Check current IV vs historical IV")
print(f"   • High IV = expensive options (better for selling)")
print(f"   • Low IV = cheap options (better for buying)")
print(f"\n3. LIQUIDITY:")
print(f"   • Check bid-ask spread on options chain")
print(f"   • Wide spreads = harder to get filled, higher costs")
print(f"   • Stick to strikes with good volume & open interest")
print(f"\n4. POSITION SIZING:")
print(f"   • Never risk more than 1-2% of portfolio per trade")
print(f"   • For ${price:.2f} stock: 1 contract = ${price*100:.0f} notional exposure")
print(f"   • Start small, scale up after proving strategy works")
print(f"\n5. TIME DECAY (THETA):")
print(f"   • Options lose value every day (time decay)")
print(f"   • Accelerates in last 30 days before expiration")
print(f"   • Buying options: want stock to move FAST")
print(f"   • Selling options: want stock to move SLOW/sideways")

print(f"\n{'='*80}")
print("📋 NEXT STEPS TO COMPLETE RESEARCH")
print(f"{'='*80}")
print(f"\n1. ✅ Current Price: ${price:.2f} (COMPLETED)")
print(f"2. ⏳ Fetch Options Chain:")
print(f"   URL: https://finance.yahoo.com/quote/DDOG/options")
print(f"   • Get actual option prices for strikes above")
print(f"   • Check bid-ask spreads")
print(f"   • Review volume & open interest")
print(f"\n3. ⏳ Check Earnings Date:")
print(f"   URL: https://finance.yahoo.com/calendar/earnings?symbol=DDOG")
print(f"   • Avoid trading right before earnings (unless intentional)")
print(f"\n4. ⏳ Check Implied Volatility:")
print(f"   URL: https://www.barchart.com/stocks/quotes/DDOG/volatility-greeks")
print(f"   • Compare current IV to historical average")
print(f"   • IV Rank/Percentile helps determine if options are cheap/expensive")
print(f"\n5. ⏳ Review Recent News:")
print(f"   • Any major announcements affecting stock?")
print(f"   • Analyst upgrades/downgrades?")
print(f"   • Sector trends?")

print(f"\n{'='*80}")
print("⚠️  DISCLAIMER")
print(f"{'='*80}")
print("This is educational research only, NOT financial advice.")
print("Options trading carries significant risk of loss.")
print("Consult a licensed financial advisor before trading.")
print("Past performance does not guarantee future results.")
print("="*80)

# Save comprehensive data
output = {
    "timestamp": datetime.now().isoformat(),
    "symbol": "DDOG",
    "current_price": price,
    "quote_data": quote_data,
    "technical_levels": {
        "support": support_levels,
        "resistance": resistance_levels,
    },
    "strategies": {
        "bull_call_spread": {"buy": buy_call_strike, "sell": sell_call_strike},
        "bear_put_spread": {"buy": buy_put_strike, "sell": sell_put_strike},
        "iron_condor": {
            "call_spread": {"sell": sell_call, "buy": buy_call},
            "put_spread": {"sell": sell_put, "buy": buy_put}
        },
        "cash_secured_put": {"strike": csp_strike},
        "covered_call": {"strike": cc_strike}
    }
}

with open('ddog_research_output.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"\n💾 Full research data saved to: ddog_research_output.json")
