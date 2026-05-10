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

## Endpoints

- `GET  /health` — health check
- `POST /report` — returns rendered HTML report
- `POST /api/report-data` — returns raw JSON (for testing)
