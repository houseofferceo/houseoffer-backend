# HouseOffer Backend

Flask backend that generates free property reports using the PropertyData API.

## Local development

```bash
pip install -r requirements.txt
export PROPERTYDATA_API_KEY=your_key_here
python app.py
```

Then POST to http://localhost:5000/report with:
```json
{
  "property_url": "https://www.rightmove.co.uk/properties/12345678",
  "asking_price": 285000,
  "bedrooms": 3,
  "property_type": "semi-detached",
  "postcode": "DE1 1DR",
  "floor_area_sqm": 85
}
```

## Deploy to Render

1. Push this repo to GitHub
2. Create a new Web Service on render.com
3. Connect your GitHub repo
4. Set environment variable: PROPERTYDATA_API_KEY
5. Build command: `pip install -r requirements.txt`
6. Start command: `gunicorn app:app`

## Property URL scraping

The backend scrapes **Rightmove** and **Zoopla** listing URLs to fill postcode, price, beds, and type when the user only pastes a link.

- Rightmove: parses embedded `PAGE_MODEL` (including the newer compressed format).
- Zoopla: parses `__NEXT_DATA__` and JSON-LD when the page is returned.

Zoopla often blocks cloud hosts (403 on Render). If that happens, set a UK residential proxy:

```bash
SCRAPER_PROXY_URL=http://user:pass@your-proxy:port
```

Test locally:

```bash
curl "http://localhost:5000/debug-scrape?url=https://www.rightmove.co.uk/properties/12345678"
```

## Endpoints

- `GET  /health` — health check
- `GET  /debug-scrape?url=...` — test Rightmove/Zoopla scraper (JSON)
- `POST /report` — returns rendered HTML report
- `POST /api/report-data` — returns raw JSON (for testing)
- `POST /submit` — email flow (scrapes URL if postcode/price missing)
