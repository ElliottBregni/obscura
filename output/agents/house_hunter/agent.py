"""House Hunter Agent — SE Michigan real estate scout.

Finds listings under $325k, scores them, sends daily digests.

Setup:
    pip install requests

    export ZILLOW_API_KEY="your_rapidapi_key_here"

Usage:
    python output/agents/house_hunter/agent.py --mode scan
    python output/agents/house_hunter/agent.py --mode score --url "https://www.zillow.com/homedetails/..."
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

import requests

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUDGET_MAX = 325_000
REGION = "SE Michigan"
AVG_PRICE_PER_SQFT = 155.0

ZILLOW_API_KEY = os.getenv("ZILLOW_API_KEY", "")
ZILLOW_HOST = "zillow-com1.p.rapidapi.com"

WEIGHTS = {
    "price": 0.35,
    "school_rating": 0.25,
    "days_on_market": 0.20,
    "price_per_sqft": 0.20,
}

SE_MICHIGAN_ZIPS = [
    "48009",  # Birmingham
    "48067",  # Royal Oak
    "48073",  # Royal Oak north
    "48220",  # Ferndale
    "48072",  # Berkley
    "48084",  # Troy
    "48083",  # Troy
    "48336",  # Farmington Hills
    "48375",  # Novi
    "48152",  # Livonia
    "48154",  # Livonia
    "48197",  # Ypsilanti
    "48103",  # Ann Arbor west
    "48104",  # Ann Arbor
    "48105",  # Ann Arbor north
    "48034",  # Southfield
    "48076",  # Southfield
    "48185",  # Westland
    "48180",  # Taylor
    "48025",  # Franklin
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Listing:
    address: str
    price: int
    beds: int
    baths: float
    sqft: int
    days_on_market: int
    school_rating: float
    url: str
    city: str = ""
    zip_code: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def price_per_sqft(self) -> float:
        return self.price / self.sqft if self.sqft > 0 else 0.0


# ---------------------------------------------------------------------------
# Zillow API
# ---------------------------------------------------------------------------


def search_listings_by_zip(zip_code: str) -> list[dict]:
    if not ZILLOW_API_KEY:
        return _mock_listings()

    headers = {
        "X-RapidAPI-Key": ZILLOW_API_KEY,
        "X-RapidAPI-Host": ZILLOW_HOST,
    }
    params = {
        "location": zip_code,
        "home_type": "Houses",
        "minPrice": "50000",
        "maxPrice": str(BUDGET_MAX),
        "status_type": "ForSale",
        "sort": "Days_on_Zillow",
    }
    try:
        resp = requests.get(
            "https://zillow-com1.p.rapidapi.com/propertyExtendedSearch",
            headers=headers,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("props", [])
    except Exception as e:
        _log.error("Zillow API error for %s: %s", zip_code, e)
        return []


def fetch_listing_details(zillow_url: str) -> dict:
    if not ZILLOW_API_KEY:
        return _mock_listing_detail()

    import re

    match = re.search(r"(\d{8,})", zillow_url)
    if not match:
        _log.error("Could not extract zpid from: %s", zillow_url)
        return {}

    headers = {
        "X-RapidAPI-Key": ZILLOW_API_KEY,
        "X-RapidAPI-Host": ZILLOW_HOST,
    }
    try:
        resp = requests.get(
            "https://zillow-com1.p.rapidapi.com/property",
            headers=headers,
            params={"zpid": match.group(1)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _log.error("Zillow detail error: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_search_result(raw: dict, zip_code: str = "") -> Listing | None:
    try:
        price = int(raw.get("price", 0))
        if price <= 0 or price > BUDGET_MAX:
            return None
        return Listing(
            address=raw.get("address", "Unknown"),
            price=price,
            beds=int(raw.get("bedrooms", 0)),
            baths=float(raw.get("bathrooms", 0)),
            sqft=int(raw.get("livingArea", 0)),
            days_on_market=int(raw.get("daysOnZillow", 0)),
            school_rating=float(raw.get("schoolRating", 5.0)),
            url=raw.get("detailUrl", ""),
            city=raw.get("city", ""),
            zip_code=zip_code,
        )
    except (ValueError, TypeError):
        return None


def normalize_detail(raw: dict, url: str = "") -> Listing | None:
    try:
        schools = raw.get("schools", [])
        ratings = [s.get("rating", 5) for s in schools if s.get("rating")]
        school_rating = round(sum(ratings) / len(ratings), 1) if ratings else 5.0
        addr = raw.get("address", {})
        return Listing(
            address=addr.get("streetAddress", "Unknown"),
            price=int(raw.get("price", 0)),
            beds=int(raw.get("bedrooms", 0)),
            baths=float(raw.get("bathrooms", 0)),
            sqft=int(raw.get("livingArea", 0)),
            days_on_market=int(raw.get("daysOnZillow", 0)),
            school_rating=school_rating,
            url=url,
            city=addr.get("city", ""),
            zip_code=addr.get("zipcode", ""),
        )
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_listing(listing: Listing) -> Listing:
    b: dict[str, float] = {}

    # Price
    if listing.price > BUDGET_MAX:
        b["price"] = 0.0
    else:
        b["price"] = round(10 - (listing.price / BUDGET_MAX * 5), 2)

    # Schools
    b["school_rating"] = round(min(listing.school_rating, 10.0), 2)

    # Days on market
    dom = listing.days_on_market
    b["days_on_market"] = (
        10.0
        if dom <= 3
        else 8.0
        if dom <= 14
        else 6.0
        if dom <= 30
        else 4.0
        if dom <= 60
        else 2.0
    )

    # Price per sqft
    ppsf = listing.price_per_sqft()
    avg = AVG_PRICE_PER_SQFT
    b["price_per_sqft"] = (
        5.0
        if ppsf == 0
        else 10.0
        if ppsf < avg * 0.80
        else 8.0
        if ppsf < avg
        else 6.0
        if ppsf < avg * 1.10
        else 3.0
    )

    listing.score = round(sum(b[k] * WEIGHTS[k] for k in WEIGHTS), 2)
    listing.score_breakdown = b
    return listing


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_digest(listings: list[Listing]) -> str:
    if not listings:
        return "No listings found today."

    top = sorted(listings, key=lambda x: x.score, reverse=True)[:10]
    lines = [
        f"# House Hunt Digest — {datetime.now().strftime('%Y-%m-%d')}",
        f"Region: {REGION} | Budget: ${BUDGET_MAX:,} | Found: {len(listings)} listings\n",
    ]
    for i, lst in enumerate(top, 1):
        stars = "⭐" * round(lst.score / 2)
        ppsf = f"${lst.price_per_sqft():.0f}/sqft" if lst.sqft > 0 else "sqft unknown"
        lines += [
            f"## {i}. {lst.address} ({lst.city})",
            f"Score: {lst.score}/10 {stars}",
            f"  ${lst.price:,} | {lst.beds}bd/{lst.baths}ba | {lst.sqft:,} sqft | {ppsf}",
            f"  DOM: {lst.days_on_market}d | Schools: {lst.school_rating}/10",
            f"  {lst.url}\n",
        ]
    return "\n".join(lines)


def format_score_report(listing: Listing) -> str:
    stars = "⭐" * round(listing.score / 2)
    verdict = (
        "STRONG BUY"
        if listing.score >= 7.5
        else "Worth a look"
        if listing.score >= 6.0
        else "Pass"
    )
    return f"""
{listing.address}, {listing.city}
Score: {listing.score}/10 {stars} — {verdict}

  Price:        ${listing.price:,}         → {listing.score_breakdown.get("price", 0)}/10
  Schools:      {listing.school_rating}/10          → {listing.score_breakdown.get("school_rating", 0)}/10
  Days on mkt:  {listing.days_on_market}d             → {listing.score_breakdown.get("days_on_market", 0)}/10
  $/sqft:       ${listing.price_per_sqft():.0f}           → {listing.score_breakdown.get("price_per_sqft", 0)}/10

  {listing.url}
""".strip()


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------


def _mock_listings() -> list[dict]:
    return [
        {
            "address": "123 Oak St, Royal Oak, MI 48067",
            "price": 289000,
            "bedrooms": 3,
            "bathrooms": 1.5,
            "livingArea": 1400,
            "daysOnZillow": 5,
            "schoolRating": 7.2,
            "detailUrl": "https://zillow.com/mock/1",
            "city": "Royal Oak",
        },
        {
            "address": "456 Maple Ave, Ferndale, MI 48220",
            "price": 315000,
            "bedrooms": 4,
            "bathrooms": 2.0,
            "livingArea": 1800,
            "daysOnZillow": 2,
            "schoolRating": 6.8,
            "detailUrl": "https://zillow.com/mock/2",
            "city": "Ferndale",
        },
        {
            "address": "789 Elm Dr, Berkley, MI 48072",
            "price": 278000,
            "bedrooms": 3,
            "bathrooms": 1.0,
            "livingArea": 1100,
            "daysOnZillow": 12,
            "schoolRating": 8.1,
            "detailUrl": "https://zillow.com/mock/3",
            "city": "Berkley",
        },
        {
            "address": "321 Pine Rd, Troy, MI 48084",
            "price": 299000,
            "bedrooms": 3,
            "bathrooms": 2.0,
            "livingArea": 1600,
            "daysOnZillow": 1,
            "schoolRating": 9.0,
            "detailUrl": "https://zillow.com/mock/4",
            "city": "Troy",
        },
        {
            "address": "654 Birch Ln, Novi, MI 48375",
            "price": 310000,
            "bedrooms": 4,
            "bathrooms": 2.5,
            "livingArea": 2100,
            "daysOnZillow": 7,
            "schoolRating": 8.5,
            "detailUrl": "https://zillow.com/mock/5",
            "city": "Novi",
        },
    ]


def _mock_listing_detail() -> dict:
    return {
        "price": 295000,
        "bedrooms": 3,
        "bathrooms": 2.0,
        "livingArea": 1650,
        "daysOnZillow": 8,
        "address": {"streetAddress": "999 Test Ln", "city": "Troy", "zipcode": "48084"},
        "schools": [{"rating": 7}, {"rating": 8}, {"rating": 6}],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_scan() -> None:
    print(f"Scanning {len(SE_MICHIGAN_ZIPS)} zip codes...")
    all_listings: list[Listing] = []
    seen: set[str] = set()

    for i, zip_code in enumerate(SE_MICHIGAN_ZIPS, 1):
        print(f"  [{i}/{len(SE_MICHIGAN_ZIPS)}] {zip_code}...", end="\r")
        for raw in search_listings_by_zip(zip_code):
            lst = normalize_search_result(raw, zip_code)
            if lst and lst.address not in seen:
                seen.add(lst.address)
                all_listings.append(score_listing(lst))

    print(f"\nFound {len(all_listings)} listings under ${BUDGET_MAX:,}")
    report = format_digest(all_listings)
    print("\n" + report)

    out = f"hunt_{datetime.now().strftime('%Y%m%d')}.md"
    with open(out, "w") as f:
        f.write(report)
    print(f"\nSaved to {out}")


def run_score(url: str) -> None:
    print(f"Fetching: {url}")
    raw = fetch_listing_details(url)
    if not raw:
        print("Could not fetch listing")
        return
    lst = normalize_detail(raw, url=url)
    if not lst:
        print("Could not parse listing")
        return
    print("\n" + format_score_report(score_listing(lst)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="House Hunter — SE Michigan")
    parser.add_argument("--mode", choices=["scan", "score"], default="scan")
    parser.add_argument("--url", help="Zillow URL to score")
    args = parser.parse_args()

    print(f"House Hunter | Budget: ${BUDGET_MAX:,} | Region: {REGION}")
    if not ZILLOW_API_KEY:
        print("No ZILLOW_API_KEY — using mock data\n")

    if args.mode == "scan":
        run_scan()
    else:
        if not args.url:
            print("--url required for score mode")
            return
        run_score(args.url)


if __name__ == "__main__":
    main()
