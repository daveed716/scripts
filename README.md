# Scripts

## SATA hard drive deal finder

`best_sata_drive_deals.py` collects SATA hard-drive listings from eBay,
Amazon, and ServerPartsDeals, calculates the effective price per
terabyte, and writes the sorted results to a CSV file.

### Prerequisites

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

The script expects API credentials via environment variables:

- `EBAY_CLIENT_ID` – eBay REST API client identifier.
- `EBAY_CLIENT_SECRET` – eBay REST API client secret.
- `SERPAPI_KEY` – SerpAPI key used to query Amazon search results.

### Usage

```bash
python best_sata_drive_deals.py --output sata_deals.csv --limit 25 --verbose
```

The generated CSV contains the source marketplace, product title, item
link, advertised capacity, total price (price + shipping), and price per
terabyte.  Missing integrations are skipped automatically when the
required credentials are not provided.
