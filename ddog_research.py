#!/usr/bin/env python3
"""
Datadog (DDOG) Options Trading Research Script
Fetches market data, options chains, earnings info, and generates trade plans
"""

import urllib.request
import json
import re
from datetime import datetime
from typing import Dict, List, Any

class DDOGResearcher:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        self.symbol = 'DDOG'
        self.data = {}
    
    def fetch_json_api(self, url: str, description: str) -> Dict:
        """Fetch JSON data from API endpoint"""
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read().decode('utf-8')
                data = json.loads(content)
                print(f"✅ {description}: Success")
                return data
        except Exception as e:
            print(f"❌ {description}: {e}")
            return {}
    
    def fetch_yahoo_quote(self):
        """Fetch real-time quote from Yahoo Finance API"""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self.symbol}"
        data = self.fetch_json_api(url, "Yahoo Finance Quote")
        
        if data and 'chart' in data and 'result' in data['chart']:
            result = data['chart']['result'][0]
            meta = result.get('meta', {})
            
            self.data['price'] = meta.get('regularMarketPrice')
            self.data['previous_close'] = meta.get('chartPreviousClose')
            self.data['day_high'] = meta.get('regularMarketDayHigh')
            self.data['day_low'] = meta.get('regularMarketDayLow')
            self.data['volume'] = meta.get('regularMarketVolume')
            self.data['currency'] = meta.get('currency', 'USD')
            
            print(f"\n📊 Current Price: ${self.data.get('price', 'N/A')}")
            print(f"   Day Range: ${self.data.get('day_low')} - ${self.data.get('day_high')}")
            print(f"   Volume: {self.data.get('volume'):,}" if self.data.get('volume') else "   Volume: N/A")
    
    def fetch_yahoo_stats(self):
        """Fetch key statistics from Yahoo Finance"""
        modules = "defaultKeyStatistics,financialData,earningsHistory,calendarEvents"
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{self.symbol}?modules={modules}"
        data = self.fetch_json_api(url, "Yahoo Finance Statistics")
        
        if data and 'quoteSummary' in data and 'result' in data['quoteSummary']:
            result = data['quoteSummary']['result'][0]
            
            stats = result.get('defaultKeyStatistics', {})
            self.data['market_cap'] = stats.get('marketCap', {}).get('raw')
            self.data['pe_ratio'] = stats.get('forwardPE', {}).get('raw')
            self.data['52w_high'] = stats.get('fiftyTwoWeekHigh', {}).get('raw')
            self.data['52w_low'] = stats.get('fiftyTwoWeekLow', {}).get('raw')
            
            calendar = result.get('calendarEvents', {})
            earnings = calendar.get('earnings', {})
            if earnings.get('earningsDate'):
                earnings_dates = earnings['earningsDate']
                if earnings_dates:
                    ts = earnings_dates[0]['raw']
                    self.data['next_earnings'] = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            
            fin_data = result.get('financialData', {})
            self.data['target_price'] = fin_data.get('targetMeanPrice', {}).get('raw')
            self.data['recommendation'] = fin_data.get('recommendationKey')
            
            print(f"\n📈 Fundamentals:")
            if self.data.get('market_cap'):
                print(f"   Market Cap: ${self.data['market_cap']:,.0f}")
            print(f"   P/E Ratio: {self.data.get('pe_ratio', 'N/A')}")
            if self.data.get('52w_low') and self.data.get('52w_high'):
                print(f"   52W Range: ${self.data['52w_low']:.2f} - ${self.data['52w_high']:.2f}")
            print(f"   Next Earnings: {self.data.get('next_earnings', 'N/A')}")
            if self.data.get('target_price'):
                print(f"   Analyst Target: ${self.data['target_price']:.2f}")
    
    def fetch_options_chain(self):
        """Fetch options chain data"""
        url = f"https://query2.finance.yahoo.com/v7/finance/options/{self.symbol}"
        data = self.fetch_json_api(url, "Options Chain")
        
        if not data or 'optionChain' not in data:
            return
        
        result = data['optionChain']['result'][0]
        expirations = result.get('expirationDates', [])
        self.data['expiration_dates'] = [
            datetime.fromtimestamp(ts).strftime('%Y-%m-%d') 
            for ts in expirations[:10]
        ]
        
        print(f"\n📅 Available Expirations (next 5):")
        for exp in self.data['expiration_dates'][:5]:
            print(f"   • {exp}")
        
        options = result.get('options', [{}])[0]
        calls = options.get('calls', [])
        puts = options.get('puts', [])
        
        current_price = self.data.get('price', 0)
        if not current_price:
            return
        
        print(f"\n🎯 Near-ATM Options (Price: ${current_price:.2f}):")
        print("\nCALLS:")
        print(f"{'Strike':<8} {'Last':<8} {'Bid':<8} {'Ask':<8} {'Vol':<8} {'OI':<8} {'IV':<8}")
        print("-" * 64)
        
        atm_calls = [c for c in calls if abs(c['strike'] - current_price) / current_price < 0.10]
        for call in atm_calls[:8]:
            iv = call.get('impliedVolatility', 0) * 100
            print(f"${call['strike']:<7.2f} ${call.get('lastPrice', 0):<7.2f} "
                  f"${call.get('bid', 0):<7.2f} ${call.get('ask', 0):<7.2f} "
                  f"{call.get('volume', 0):<8} {call.get('openInterest', 0):<8} {iv:<7.1f}%")
        
        print("\nPUTS:")
        print(f"{'Strike':<8} {'Last':<8} {'Bid':<8} {'Ask':<8} {'Vol':<8} {'OI':<8} {'IV':<8}")
        print("-" * 64)
        
        atm_puts = [p for p in puts if abs(p['strike'] - current_price) / current_price < 0.10]
        for put in atm_puts[:8]:
            iv = put.get('impliedVolatility', 0) * 100
            print(f"${put['strike']:<7.2f} ${put.get('lastPrice', 0):<7.2f} "
                  f"${put.get('bid', 0):<7.2f} ${put.get('ask', 0):<7.2f} "
                  f"{put.get('volume', 0):<8} {put.get('openInterest', 0):<8} {iv:<7.1f}%")
        
        if atm_calls:
            avg_iv = sum(c.get('impliedVolatility', 0) for c in atm_calls) / len(atm_calls) * 100
            self.data['avg_call_iv'] = avg_iv
            print(f"\n📊 Average ATM Call IV: {avg_iv:.1f}%")
        
        if atm_puts:
            avg_iv = sum(p.get('impliedVolatility', 0) for p in atm_puts) / len(atm_puts) * 100
            self.data['avg_put_iv'] = avg_iv
            print(f"📊 Average ATM Put IV: {avg_iv:.1f}%")
        
        self.data['options_chain'] = {'calls': calls, 'puts': puts}
    
    def calculate_trade_ideas(self):
        """Generate options trade ideas"""
        print("\n" + "="*80)
        print("💡 OPTIONS TRADE IDEAS - DATADOG (DDOG)")
        print("="*80)
        
        price = self.data.get('price')
        if not price:
            print("❌ Cannot generate trade ideas without current price")
            return
        
        earnings = self.data.get('next_earnings', 'Unknown')
        avg_iv = (self.data.get('avg_call_iv', 0) + self.data.get('avg_put_iv', 0)) / 2
        
        print(f"\n📋 Context:")
        print(f"   Current Price: ${price:.2f}")
        print(f"   Next Earnings: {earnings}")
        print(f"   Average IV: {avg_iv:.1f}%")
        print(f"   Analyst Target: ${self.data.get('target_price', 'N/A')}")
        
        print(f"\n{'='*80}")
        print("STRATEGY 1: Bullish Call Debit Spread")
        print(f"{'='*80}")
        print(f"Buy: ${price * 1.02:.2f} Call | Sell: ${price * 1.08:.2f} Call")
        print("Use: Moderate bullish outlook, limited risk")
        print("Risk: Debit paid (~$200-400) | Max Profit: ~$300-600")
        print("⚠️  Exit before earnings if avoiding volatility")
        
        print(f"\n{'='*80}")
        print("STRATEGY 2: Bearish Put Debit Spread")
        print(f"{'='*80}")
        print(f"Buy: ${price * 0.98:.2f} Put | Sell: ${price * 0.92:.2f} Put")
        print("Use: Moderate bearish outlook, limited risk")
        print("Risk: Debit paid (~$200-400) | Max Profit: ~$300-600")
        
        print(f"\n{'='*80}")
        print("STRATEGY 3: Iron Condor (Neutral)")
        print(f"{'='*80}")
        print(f"Sell: ${price * 1.05:.2f} Call | Buy: ${price * 1.10:.2f} Call")
        print(f"Sell: ${price * 0.95:.2f} Put | Buy: ${price * 0.90:.2f} Put")
        print("Use: Range-bound expectation, collect premium")
        print("Risk: ~$400-500 | Max Profit: ~$100-200 (credit)")
        print("⚠️  Avoid earnings week (IV crush risk)")
        
        print(f"\n{'='*80}")
        print("STRATEGY 4: Cash-Secured Put (Income)")
        print(f"{'='*80}")
        print(f"Sell: ${price * 0.95:.2f} Put")
        print(f"Capital: ~${price * 95:.0f} per contract")
        print(f"Premium: ~${price * 2:.0f}-${price * 5:.0f}")
        print("Use: Willing to own DDOG at a discount")
        
        print(f"\n{'='*80}")
        print("⚠️  RISK WARNINGS")
        print(f"{'='*80}")
        print("• Educational only - not financial advice")
        print("• Verify actual pricing in real-time options chain")
        print("• Risk only 1-2% of portfolio per trade")
        print("• Options can expire worthless")
        print("• Wide spreads = poor liquidity")
        
    def run_full_research(self):
        """Execute complete research workflow"""
        print("="*80)
        print(f"🔍 DATADOG (DDOG) OPTIONS RESEARCH")
        print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*80)
        
        self.fetch_yahoo_quote()
        self.fetch_yahoo_stats()
        self.fetch_options_chain()
        self.calculate_trade_ideas()
        
        print("\n" + "="*80)
        print("✅ Research Complete")
        print("="*80)
        
        return self.data

if __name__ == '__main__':
    researcher = DDOGResearcher()
    data = researcher.run_full_research()
    
    output_file = '/Users/elliottbregni/dev/obscura-main/ddog_research_results.json'
    with open(output_file, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\n💾 Data saved to: {output_file}")
