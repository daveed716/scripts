"""Fetch best SATA hard-drive price per TB deals from multiple retailers.

This script currently supports eBay, Amazon, and ServerPartsDeals.  It
expects API credentials for eBay and Amazon to be provided via
environment variables.  ServerPartsDeals is scraped from the public
storefront.

Usage example::

    python best_sata_drive_deals.py --output deals.csv --limit 25

Required environment variables:

```
EBAY_APP_ID="your-ebay-app-id"
SERPAPI_KEY="your-serpapi-key"  # used for Amazon search results
```

The script degrades gracefully when an integration cannot be queried.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except Exception:  # pragma: no cover - optional dependency
    cloudscraper = None

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CAPACITY_REGEX = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>tb|t|terabyte|gb|g|gigabyte)",
    re.IGNORECASE,
)
PRICE_REGEX = re.compile(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")


@dataclass
class Deal:
    source: str
    title: str
    link: str
    capacity_tb: float
    price: float
    shipping: float = 0.0

    @property
    def total_price(self) -> float:
        return self.price + self.shipping

    @property
    def price_per_tb(self) -> float:
        if not self.capacity_tb:
            return math.inf
        return self.total_price / self.capacity_tb


class DealCollector:
    def __init__(self) -> None:
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        if cloudscraper is not None:
            session = cloudscraper.create_scraper()  # type: ignore[assignment]
        else:
            session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        return session

    # --- Normalisation helpers -------------------------------------------------

    @staticmethod
    def _clean_price(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        match = PRICE_REGEX.search(value)
        if not match:
            try:
                return float(value.replace(",", "").strip("$"))
            except (ValueError, AttributeError):
                return None
        return float(match.group(1).replace(",", ""))

    @staticmethod
    def _parse_capacity_tb(text: str) -> Optional[float]:
        best: Optional[float] = None
        for match in CAPACITY_REGEX.finditer(text):
            raw_value = match.group("value").replace(",", "")
            try:
                value = float(raw_value.replace(",", "").replace(" ", ""))
            except ValueError:
                continue
            unit = match.group("unit").lower()
            if unit in {"gb", "g", "gigabyte"}:
                value = value / 1024.0
            if best is None or value > best:
                best = value
        return best

    @staticmethod
    def _looks_like_sata(text: str) -> bool:
        return "sata" in text.lower()

    # --- Source fetchers -------------------------------------------------------

    def fetch_all(self, limit: int = 25) -> List[Deal]:
        deals: List[Deal] = []
        for fetcher in (
            self._fetch_ebay,
            self._fetch_amazon,
            self._fetch_serverpartsdeals,
        ):
            try:
                deals.extend(fetcher(limit))
            except Exception as exc:  # pragma: no cover - defensive
                logging.exception("Failed to load results from %s", fetcher.__name__)
                logging.error("%s", exc)
        unique: dict[tuple[str, str], Deal] = {}
        for deal in deals:
            key = (deal.source, deal.link)
            if key not in unique or deal.price_per_tb < unique[key].price_per_tb:
                unique[key] = deal
        return sorted(unique.values(), key=lambda d: d.price_per_tb)

    # --- eBay ------------------------------------------------------------------

    def _fetch_ebay(self, limit: int) -> Sequence[Deal]:
        app_id = os.getenv("EBAY_APP_ID")
        if not app_id:
            logging.info("Skipping eBay; EBAY_APP_ID is not set")
            return []
        params = {
            "OPERATION-NAME": "findItemsByKeywords",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": "SATA internal hard drive",
            "itemFilter(0).name": "ListingType",
            "itemFilter(0).value": "FixedPrice",
            "itemFilter(1).name": "Condition",
            "itemFilter(1).value": "New",
            "paginationInput.entriesPerPage": str(limit),
            "outputSelector": "SellerInfo",
        }
        response = self.session.get(
            "https://svcs.ebay.com/services/search/FindingService/v1",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        raw_items = (
            payload.get("findItemsByKeywordsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        deals: List[Deal] = []
        for item in raw_items:
            title = item.get("title", [""])[0]
            if not self._looks_like_sata(title):
                continue
            capacity = self._parse_capacity_tb(title)
            if not capacity:
                subtitle = item.get("subtitle", [""])[0]
                capacity = self._parse_capacity_tb(subtitle)
            if not capacity:
                continue
            selling = item.get("sellingStatus", [{}])[0]
            price = self._clean_price(
                selling.get("convertedCurrentPrice", [{}])[0].get("__value__")
            )
            if price is None:
                continue
            shipping_cost = self._clean_price(
                item.get("shippingInfo", [{}])[0]
                .get("shippingServiceCost", [{}])[0]
                .get("__value__")
            ) or 0.0
            link = item.get("viewItemURL", [""])[0]
            deals.append(
                Deal(
                    source="eBay",
                    title=title,
                    link=link,
                    capacity_tb=capacity,
                    price=price,
                    shipping=shipping_cost,
                )
            )
        return deals

    # --- Amazon via SerpAPI ----------------------------------------------------

    def _fetch_amazon(self, limit: int) -> Sequence[Deal]:
        api_key = os.getenv("SERPAPI_KEY")
        if not api_key:
            logging.info("Skipping Amazon; SERPAPI_KEY is not set")
            return []
        params = {
            "engine": "amazon",
            "amazon_domain": "amazon.com",
            "api_key": api_key,
            "keywords": "SATA internal hard drive",
            "type": "search",
            "page": 1,
        }
        response = self.session.get(
            "https://serpapi.com/search.json", params=params, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("organic_results", [])
        deals: List[Deal] = []
        for result in results[:limit]:
            title = result.get("title", "")
            if not title:
                continue
            if not self._looks_like_sata(title):
                continue
            price_text = result.get("price") or result.get("price_str")
            price = self._clean_price(price_text)
            if price is None:
                continue
            link = result.get("link")
            if not link:
                continue
            capacity = self._parse_capacity_tb(title)
            if not capacity:
                subtitle = " ".join(result.get("extensions", []))
                capacity = self._parse_capacity_tb(subtitle)
            if not capacity:
                continue
            deals.append(
                Deal(
                    source="Amazon",
                    title=title,
                    link=link,
                    capacity_tb=capacity,
                    price=price,
                )
            )
        return deals

    # --- ServerPartsDeals ------------------------------------------------------

    def _fetch_serverpartsdeals(self, limit: int) -> Sequence[Deal]:
        search_url = "https://r.jina.ai/https://www.serverpartsdeals.com/search"
        response = self.session.get(search_url, params={"q": "sata hard drive"}, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # Shopify based stores use elements such as `.grid-product__content` or `.productitem`
        product_nodes = soup.select(".grid-product__content, .productitem, .product-grid-item")
        deals: List[Deal] = []
        for node in product_nodes[:limit]:
            title_node = node.select_one("a")
            price_node = node.select_one(".price-item, .price")
            if not title_node or not price_node:
                continue
            title = title_node.get_text(strip=True)
            if not self._looks_like_sata(title):
                continue
            capacity = self._parse_capacity_tb(title)
            if not capacity:
                caption = node.get_text(" ", strip=True)
                capacity = self._parse_capacity_tb(caption)
            if not capacity:
                continue
            price = self._clean_price(price_node.get_text())
            if price is None:
                continue
            link = title_node.get("href") or ""
            if link.startswith("/"):
                link = f"https://www.serverpartsdeals.com{link}"
            deals.append(
                Deal(
                    source="ServerPartsDeals",
                    title=title,
                    link=link,
                    capacity_tb=capacity,
                    price=price,
                )
            )
        return deals


def write_csv(deals: Sequence[Deal], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Source", "Title", "Link", "Capacity (TB)", "Price", "Shipping", "$/TB"])
        for deal in deals:
            writer.writerow(
                [
                    deal.source,
                    deal.title,
                    deal.link,
                    f"{deal.capacity_tb:.2f}",
                    f"${deal.price:.2f}",
                    f"${deal.shipping:.2f}",
                    f"${deal.price_per_tb:.2f}",
                ]
            )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="sata_drive_deals.csv",
        help="Path to the CSV file that will be written (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of listings to request per source",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    collector = DealCollector()
    deals = collector.fetch_all(limit=args.limit)
    if not deals:
        logging.warning("No deals were found. Ensure credentials are set and the stores are reachable.")
    else:
        logging.info("Collected %d deals", len(deals))
    write_csv(deals, args.output)
    logging.info("Wrote results to %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
