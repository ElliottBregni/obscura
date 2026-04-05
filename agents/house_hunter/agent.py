"""House Hunter Agent — SE Michigan real estate scout.

Finds listings under $325k in SE Michigan, scores them, and sends daily digests.

Usage:
    # Daily scan mode (run via cron or scheduler)
    python -m agents.house_hunter.agent --mode scan

    # Score a specific listing
    python -m agents.house_hunter.agent --mode score --url "https://www.zillow.com/homedetails/..."
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from obscura.tools.policy.models import ToolPolicy

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUDGET_MAX = 325_000
REGION = "SE Michigan"
COMMUTE_DESTINATION: str | None = None  # Set to city/zip when known

# Score weights (must sum to 1.0)
WEIGHTS = {
    "price": 0.35,
    "school_rating": 0.25,
    "days_on_market": 0.20,
    "price_per_sqft": 0.20,
}

# SE Michigan average price/sqft (2024 estimate)
AVG_PRICE_PER_SQFT = 155.0

# ---------------------------------------------------------------------------
# Policy — agent can only read/search, never write files or run shell
# ---------------------------------------------------------------------------

HOUSE_HUNTER_POLICY = ToolPolicy.from_permission_config(
    name="house-hunter",
    allow=[
        "web_search",
        "web_fetch",
        "fetch_url",
        "run_python3",  # for scoring math only
        "websearch_search",
        "websearch_summarize",
    ],
    deny=[
        "run_shell",
        "run_command",
        "write_text_file",
        "edit_text_file",
        "remove_path",
        "git",
    ],
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Listing:
    address: str
    price: int
    beds: int
    baths: float
    sqft: int
    days_on_market: int
    school_rating: float  # GreatSchools 1–10
    url: str
    zip_code: str = ""
    city: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def price_per_sqft(self) -> float:
        if self.sqft <= 0:
            return 0.0
        return self.price / self.sqft


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------


def score_listing(listing: Listing) -> Listing:
    """Score a listing 0–10 across 4 dimensions. Higher = better."""

    breakdown: dict[str, float] = {}

    # 1. Price (lower = better, anything over budget = 0)
    if listing.price > BUDGET_MAX:
        breakdown["price"] = 0.0
    else:
        # Score scales from 10 (very cheap) to 5 (at budget ceiling)
        ratio = listing.price / BUDGET_MAX
        breakdown["price"] = round(10 - (ratio * 5), 2)

    # 2. School rating (GreatSchools 1–10, direct passthrough)
    breakdown["school_rating"] = round(listing.school_rating, 2)

    # 3. Days on market (fresher = better)
    if listing.days_on_market <= 3:
        breakdown["days_on_market"] = 10.0
    elif listing.days_on_market <= 14:
        breakdown["days_on_market"] = 8.0
    elif listing.days_on_market <= 30:
        breakdown["days_on_market"] = 6.0
    elif listing.days_on_market <= 60:
        breakdown["days_on_market"] = 4.0
    else:
        breakdown["days_on_market"] = 2.0

    # 4. Price per sqft vs SE Michigan average
    ppsf = listing.price_per_sqft()
    if ppsf == 0:
        breakdown["price_per_sqft"] = 5.0  # unknown, neutral
    elif ppsf < AVG_PRICE_PER_SQFT * 0.80:
        breakdown["price_per_sqft"] = 10.0  # 20%+ below avg = great deal
    elif ppsf < AVG_PRICE_PER_SQFT:
        breakdown["price_per_sqft"] = 8.0
    elif ppsf < AVG_PRICE_PER_SQFT * 1.10:
        breakdown["price_per_sqft"] = 6.0
    else:
        breakdown["price_per_sqft"] = 3.0  # above avg

    # Weighted total
    total = sum(breakdown[k] * WEIGHTS[k] for k in WEIGHTS)
    listing.score = round(total, 2)
    listing.score_breakdown = breakdown
    return listing


def format_report(listings: list[Listing]) -> str:
    """Format listings into a readable digest."""
    if not listings:
        return "No listings found matching your criteria today."

    sorted_listings = sorted(listings, key=lambda x: x.score, reverse=True)
    lines = [
        f"# House Hunt Digest — {datetime.now().strftime('%Y-%m-%d')}",
        f"Region: {REGION} | Budget: ${BUDGET_MAX:,}\n",
    ]

    for i, listing in enumerate(sorted_listings, 1):
        stars = "⭐" * round(listing.score / 2)
        lines.append(f"## {i}. {listing.address}")
        lines.append(f"   Score: {listing.score}/10 {stars}")
        lines.append(
            f"   Price: ${listing.price:,} | {listing.beds}bd/{listing.baths}ba | {listing.sqft:,} sqft"
        )
        lines.append(
            f"   $/sqft: ${listing.price_per_sqft():.0f} | DOM: {listing.days_on_market}d | Schools: {listing.school_rating}/10"
        )
        lines.append(f"   Breakdown: {json.dumps(listing.score_breakdown)}")
        lines.append(f"   → {listing.url}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent prompt builders
# ---------------------------------------------------------------------------

SCAN_PROMPT = f"""
You are a real estate scout for SE Michigan. Your job is to find the best new listings.

Search for homes for sale in SE Michigan (Detroit metro — Ann Arbor, Royal Oak, Birmingham,
Troy, Novi, Dearborn, Livonia, Ferndale, Berkley, Clawson, Warren, Sterling Heights, etc.)
that meet ALL of these criteria:
- Price: under ${BUDGET_MAX:,}
- Listed in the last 7 days
- At least 3 bedrooms
- At least 1.5 bathrooms

For each listing you find, return a JSON object with these fields:
- address (str)
- price (int)
- beds (int)
- baths (float)
- sqft (int, 0 if unknown)
- days_on_market (int)
- school_rating (float 1-10, use GreatSchools data or estimate from area reputation)
- url (str)
- city (str)
- zip_code (str)

Return a JSON array of listings. Find as many as you can, up to 10.
Only return the JSON array, no other text.
"""

SCORE_PROMPT_TEMPLATE = """
You are a real estate analyst. Extract listing details from this URL and return them as JSON.

URL: {url}

Fetch the page and extract:
- address (str)
- price (int, just the number)
- beds (int)
- baths (float)
- sqft (int, 0 if not listed)
- days_on_market (int, 0 if not listed)
- school_rating (float 1-10, from GreatSchools widget or neighborhood reputation)
- url (str, the URL provided)
- city (str)
- zip_code (str)

Return ONLY a JSON object with these fields.
"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="House Hunter Agent — SE Michigan")
    parser.add_argument(
        "--mode",
        choices=["scan", "score"],
        default="scan",
        help="scan=daily digest, score=analyze a specific URL",
    )
    parser.add_argument("--url", help="Listing URL to score (score mode only)")
    parser.add_argument(
        "--output", default="hunt_results.md", help="Output file for results"
    )
    args = parser.parse_args()

    print(
        f"🏠 House Hunter Agent | Mode: {args.mode} | Policy: {HOUSE_HUNTER_POLICY.name}"
    )
    print(f"   Budget: ${BUDGET_MAX:,} | Region: {REGION}\n")

    if args.mode == "scan":
        print(
            "Running daily scan... (wire up Obscura agent loop to execute SCAN_PROMPT)"
        )
        print("\nPrompt ready to send to agent:")
        print("-" * 60)
        print(SCAN_PROMPT)

    elif args.mode == "score":
        if not args.url:
            print("ERROR: --url required for score mode")
            return
        prompt = SCORE_PROMPT_TEMPLATE.format(url=args.url)
        print(f"Scoring: {args.url}")
        print("\nPrompt ready to send to agent:")
        print("-" * 60)
        print(prompt)

    print("\nPolicy enforced:")
    print(f"  ALLOW: {sorted(HOUSE_HUNTER_POLICY.allow_list)}")
    print(f"  DENY:  {sorted(HOUSE_HUNTER_POLICY.deny_list)}")


if __name__ == "__main__":
    main()
