# CardFinder

Flask app for searching Pokemon TCG cards, viewing API price data, and opening marketplace searches.

## Run Locally

```powershell
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Deploy On Render

Create a new Render Web Service from the GitHub repo.

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

Set these environment variables in Render:

- `POKEMON_TCG_API_KEY`
- `EBAY_APP_ID` or `EBAY_CLIENT_ID` optional, for eBay sold-pricing API support
- `EBAY_VERIFICATION_TOKEN`
- `EBAY_ENDPOINT_URL`

For eBay Marketplace Account Deletion/Closure notifications, use this endpoint URL:

```text
https://cardfinder-wne6.onrender.com/ebay/account-deletion
```

Set `EBAY_ENDPOINT_URL` to the same value:

```text
https://cardfinder-wne6.onrender.com/ebay/account-deletion
```

Do not upload `.env`; it is for local development only.
