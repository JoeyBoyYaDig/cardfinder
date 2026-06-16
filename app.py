import base64
import hashlib
import os
import re
from time import time
from urllib.parse import quote_plus

import requests
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
app = Flask(__name__)

POKEMON_TCG_API_URL = "https://api.pokemontcg.io/v2/cards"
API_KEY_ENV = "POKEMON_TCG_API_KEY"
EBAY_APP_ID_ENV = "EBAY_APP_ID"
EBAY_CLIENT_ID_ENV = "EBAY_CLIENT_ID"
EBAY_CLIENT_SECRET_ENV = "EBAY_CLIENT_SECRET"
EBAY_VERIFICATION_TOKEN_ENV = "EBAY_VERIFICATION_TOKEN"
EBAY_ENDPOINT_URL_ENV = "EBAY_ENDPOINT_URL"
EBAY_FINDING_API_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_MARKETPLACE_INSIGHTS_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)
EBAY_SOLD_URL = "https://www.ebay.com/sch/i.html"
EBAY_CACHE_SECONDS = 60 * 30
EBAY_SALES_CACHE = {}
EBAY_TOKEN_CACHE = {}
EBAY_LAST_ERROR = {}
EBAY_ERROR_ATTEMPTS = []
RESULTS_PER_PAGE = 24
PRICE_SORT_PAGE_SIZE = 250
PRICE_SORT_MAX_CARDS = 500
SORT_OPTIONS = {
    "name": "name,set.name,number",
    "set": "set.name,number,name",
    "number": "number,name",
    "newest": "-set.releaseDate,name",
    "oldest": "set.releaseDate,name",
    "price_low": "API average price: low to high",
    "price_high": "API average price: high to low",
}
PRICE_SORTS = {"price_low", "price_high"}
FULL_ART_TERMS = (
    "Full Art",
    "Illustration Rare",
    "Special Illustration Rare",
    "Ultra Rare",
    "Secret Rare",
    "Trainer Gallery",
    "Galarian Gallery",
    "Rare Ultra",
    "Rare Secret",
    "Rare Rainbow",
)
EDITION_PHRASES = (
    "first edition",
    "1st edition",
    "shadowless",
    "holo",
    "reverse holo",
    "reverse foil",
)


def escape_query_value(value):
    return value.replace('"', r"\"")


def remove_phrase(text, phrase):
    return re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)


def collapse_accidental_first_letter_repeat(text):
    words = []
    for word in text.split():
        if len(word) > 3 and word[0].lower() == word[1].lower():
            words.append(word[1:])
        else:
            words.append(word)
    return " ".join(words)


def parse_search_term(search_term):
    cleaned = " ".join(search_term.strip().split())
    lowered = cleaned.lower()
    marketplace_keywords = []

    api_text = cleaned
    for phrase in EDITION_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            marketplace_keywords.append(phrase)
            api_text = remove_phrase(api_text, phrase)

    number_total_match = re.search(
        r"\b(?P<number>[A-Za-z]*\d+[A-Za-z]*)\s*/\s*(?P<total>\d+)\b",
        api_text,
    )
    number = None
    total = None
    if number_total_match:
        number = number_total_match.group("number")
        total = number_total_match.group("total")
        api_text = (
            api_text[: number_total_match.start()]
            + " "
            + api_text[number_total_match.end() :]
        )

    api_text = " ".join(api_text.split())
    corrected_api_text = collapse_accidental_first_letter_repeat(api_text)

    return {
        "cleaned": cleaned,
        "api_text": api_text,
        "corrected_api_text": corrected_api_text,
        "number": number,
        "total": total,
        "marketplace_keywords": marketplace_keywords,
    }


def build_name_or_number_query(search_text):
    escaped = escape_query_value(search_text)
    return f'(name:"{escaped}" OR number:"{escaped}")'


def build_card_queries(search_term):
    intent = parse_search_term(search_term)
    queries = []

    if intent["number"] and intent["total"]:
        number = escape_query_value(intent["number"])
        total = escape_query_value(intent["total"])
        number_total_query = (
            f'(number:"{number}" AND (set.printedTotal:{total} OR set.total:{total}))'
        )

        if intent["api_text"]:
            name = escape_query_value(intent["api_text"])
            queries.append(f'({number_total_query} AND name:"{name}")')
        queries.append(number_total_query)

    for api_text in (intent["api_text"], intent["corrected_api_text"], intent["cleaned"]):
        if api_text and api_text not in queries:
            queries.append(build_name_or_number_query(api_text))

    return queries, intent


def apply_full_art_filter(query):
    rarity_query = " OR ".join(f'rarity:"{escape_query_value(term)}"' for term in FULL_ART_TERMS)
    subtype_query = 'subtypes:"Full Art"'
    return f'({query}) AND ({rarity_query} OR {subtype_query})'


def api_order_for_sort(sort):
    if sort in PRICE_SORTS:
        return SORT_OPTIONS["name"]
    return SORT_OPTIONS.get(sort, SORT_OPTIONS["name"])


def money(value):
    if value is None:
        return None
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def parse_money(value):
    if not value:
        return None

    match = re.search(r"(?<![A-Z])\$\s*([0-9,]+(?:\.[0-9]{1,2})?)", value)
    if not match:
        return None

    return price_value(match.group(1).replace(",", ""))


def format_finish(value):
    labels = {
        "1stEdition": "1st Edition",
        "unlimited": "Unlimited",
        "normal": "Normal",
        "holofoil": "Holofoil",
        "reverseHolofoil": "Reverse Holofoil",
    }
    if value in labels:
        return labels[value]

    spaced = re.sub(r"(?<!^)([A-Z])", r" \1", value).replace("_", " ")
    return spaced.title()


def price_value(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def average(values):
    usable_values = [value for value in values if value is not None]
    if not usable_values:
        return None
    return sum(usable_values) / len(usable_values)


def compare_to_average(price, average_price):
    if price is None or average_price is None or average_price <= 0:
        return {"label": "No comparison", "status": "missing"}

    percent = ((price - average_price) / average_price) * 100
    if abs(percent) < 1:
        return {"label": "Near average", "status": "fair"}

    if percent < 0:
        return {"label": f"{abs(percent):.0f}% under avg", "status": "deal"}

    return {"label": f"{percent:.0f}% over avg", "status": "high"}


def flatten_prices(card):
    price_rows = []

    tcgplayer = card.get("tcgplayer") or {}
    tcg_prices = tcgplayer.get("prices") or {}
    for finish, values in tcg_prices.items():
        market_value = price_value(values.get("market"))
        price_rows.append(
            {
                "source": "TCGplayer",
                "finish": format_finish(finish),
                "market_value": market_value,
                "low": money(values.get("low")),
                "mid": money(values.get("mid")),
                "high": money(values.get("high")),
                "market": money(market_value),
                "directLow": money(values.get("directLow")),
                "updatedAt": tcgplayer.get("updatedAt"),
            }
        )

    cardmarket = card.get("cardmarket") or {}
    cm_prices = cardmarket.get("prices") or {}
    if cm_prices:
        market_value = price_value(cm_prices.get("avg1"))
        price_rows.append(
            {
                "source": "Cardmarket",
                "finish": "Any",
                "market_value": market_value,
                "low": money(cm_prices.get("lowPrice")),
                "mid": money(cm_prices.get("averageSellPrice")),
                "high": money(cm_prices.get("trendPrice")),
                "market": money(market_value),
                "directLow": None,
                "updatedAt": cardmarket.get("updatedAt"),
            }
        )

    return price_rows


def wants_first_edition(intent):
    return any(
        phrase in {"first edition", "1st edition"}
        for phrase in intent["marketplace_keywords"]
    )


def has_first_edition_price(card):
    prices = ((card.get("tcgplayer") or {}).get("prices") or {})
    return "1stEdition" in prices


def filter_cards_for_intent(cards, intent):
    if wants_first_edition(intent):
        first_edition_cards = [card for card in cards if has_first_edition_price(card)]
        if first_edition_cards:
            return first_edition_cards

    return cards


def summarize_prices(price_rows):
    average_price = average(row.get("market_value") for row in price_rows)

    for row in price_rows:
        row["comparison"] = compare_to_average(row.get("market_value"), average_price)

    return {
        "average": money(average_price),
        "average_value": average_price,
    }


def build_marketplace_comparisons(price_rows, average_price, links):
    comparisons = []
    sources = {
        "TCGplayer": average(
            row.get("market_value")
            for row in price_rows
            if row.get("source") == "TCGplayer"
        ),
        "Cardmarket": average(
            row.get("market_value")
            for row in price_rows
            if row.get("source") == "Cardmarket"
        ),
    }

    for source, source_average in sources.items():
        comparisons.append(
            {
                "source": source,
                "price": money(source_average),
                "comparison": compare_to_average(source_average, average_price),
                "url": links.get(source.lower()),
                "note": "From Pokémon TCG API",
            }
        )

    for source, link_key in (
        ("eBay", "ebay"),
        ("PriceCharting", "pricecharting"),
        ("Google Shopping", "google_shopping"),
    ):
        comparisons.append(
            {
                "source": source,
                "price": None,
                "comparison": {"label": "Needs live pricing", "status": "missing"},
                "note": "Search listings",
                "url": links.get(link_key),
            }
        )

    return comparisons


def build_search_links(card, marketplace_keywords=None):
    set_name = (card.get("set") or {}).get("name", "")
    tcgplayer = card.get("tcgplayer") or {}
    cardmarket = card.get("cardmarket") or {}
    extra_keywords = " ".join(marketplace_keywords or [])
    query = " ".join(
        part
        for part in [
            card.get("name", ""),
            set_name,
            card.get("number", ""),
            extra_keywords,
            "pokemon card",
        ]
        if part
    )
    encoded = quote_plus(query)

    tcgplayer_search_url = (
        "https://www.tcgplayer.com/search/pokemon/product"
        f"?productLineName=pokemon&q={encoded}"
    )
    cardmarket_search_url = (
        "https://www.cardmarket.com/en/Pokemon/Products/Search"
        f"?searchString={encoded}"
    )
    tcgplayer_url = tcgplayer.get("url") or ""
    cardmarket_url = cardmarket.get("url") or ""

    return {
        "query": query,
        "tcgplayer": (
            tcgplayer_url
            if tcgplayer_url.startswith("https://www.tcgplayer.com/")
            else tcgplayer_search_url
        ),
        "ebay": f"https://www.ebay.com/sch/i.html?_nkw={encoded}",
        "ebay_sold": f"https://www.ebay.com/sch/i.html?_nkw={encoded}&LH_Sold=1&LH_Complete=1",
        "cardmarket": (
            cardmarket_url
            if cardmarket_url.startswith("https://www.cardmarket.com/")
            else cardmarket_search_url
        ),
        "pricecharting": f"https://www.pricecharting.com/search-products?q={encoded}&type=prices",
        "google_shopping": f"https://www.google.com/search?tbm=shop&q={encoded}",
    }


def normalize_card(card, marketplace_keywords=None):
    set_data = card.get("set") or {}
    images = card.get("images") or {}
    price_rows = flatten_prices(card)
    price_summary = summarize_prices(price_rows)

    links = build_search_links(card, marketplace_keywords)

    return {
        "id": card.get("id"),
        "name": card.get("name", "Unknown card"),
        "set_name": set_data.get("name", "Unknown set"),
        "number": card.get("number", "Unknown"),
        "rarity": card.get("rarity") or "Unknown",
        "artist": card.get("artist") or "Unknown",
        "image": images.get("large") or images.get("small"),
        "prices": price_rows,
        "price_summary": price_summary,
        "marketplace_comparisons": build_marketplace_comparisons(
            price_rows, price_summary["average_value"], links
        ),
        "ebay_sales": {
            "average": None,
            "count": 0,
            "message": "Checking sold listings...",
        },
        "links": links,
        "ebay_query": links["query"],
    }


def sort_cards_by_price(cards, sort):
    reverse = sort == "price_high"

    def price_key(card):
        value = card["price_summary"]["average_value"]
        if value is None:
            return float("inf") if not reverse else float("-inf")
        return value

    return sorted(cards, key=price_key, reverse=reverse)


def paginate_cards(cards, page, page_size):
    start = (page - 1) * page_size
    end = start + page_size
    return cards[start:end]


def extract_ebay_sold_prices(page_html):
    price_texts = re.findall(
        r'class="[^"]*s-item__price[^"]*"[^>]*>(.*?)</span>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    prices = []

    for raw_text in price_texts:
        text = re.sub(r"<[^>]+>", " ", raw_text)
        price = parse_money(text)
        if price is not None and price > 0:
            prices.append(price)
        if len(prices) >= 10:
            break

    if prices:
        return prices

    json_prices = re.findall(
        r'"value"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?[^{}]{0,120}"currency"\s*:\s*"USD"',
        page_html,
        flags=re.IGNORECASE,
    )
    for raw_price in json_prices:
        price = price_value(raw_price)
        if price is not None and price > 0:
            prices.append(price)
        if len(prices) >= 10:
            break

    return prices


def fetch_ebay_sold_sales(search_query):
    EBAY_ERROR_ATTEMPTS.clear()
    cache_key = search_query.strip().lower()
    cached = EBAY_SALES_CACHE.get(cache_key)
    if cached and time() - cached["created_at"] < EBAY_CACHE_SECONDS:
        return cached["payload"]

    client_id = os.getenv(EBAY_CLIENT_ID_ENV) or os.getenv(EBAY_APP_ID_ENV)
    client_secret = os.getenv(EBAY_CLIENT_SECRET_ENV)
    if client_id and client_secret:
        try:
            payload = fetch_ebay_sold_sales_marketplace_insights(
                search_query, client_id, client_secret
            )
            EBAY_SALES_CACHE[cache_key] = {"created_at": time(), "payload": payload}
            return payload
        except requests.RequestException as error:
            remember_ebay_error("Marketplace Insights", error)
            app.logger.exception("eBay Marketplace Insights lookup failed.")
            raise

    if client_id:
        try:
            payload = fetch_ebay_sold_sales_api(search_query, client_id)
            EBAY_SALES_CACHE[cache_key] = {"created_at": time(), "payload": payload}
            return payload
        except requests.RequestException as error:
            remember_ebay_error("Finding API", error)
            app.logger.exception("eBay Finding API lookup failed.")
            raise

    payload = fetch_ebay_sold_sales_page(search_query)
    EBAY_SALES_CACHE[cache_key] = {"created_at": time(), "payload": payload}
    return payload


def remember_ebay_error(source, error):
    response = getattr(error, "response", None)
    detail = error.__class__.__name__
    if response is not None:
        response_text = response.text[:160].replace("\n", " ").replace("\r", " ")
        detail = f"{detail}: {response_text}"

    error_data = {
        "source": source,
        "status_code": response.status_code if response is not None else None,
        "detail": detail,
    }
    EBAY_ERROR_ATTEMPTS.append(error_data)
    EBAY_LAST_ERROR.clear()
    EBAY_LAST_ERROR.update(error_data)


def fetch_ebay_access_token(client_id, client_secret):
    cached = EBAY_TOKEN_CACHE.get("production")
    if cached and time() < cached["expires_at"]:
        return cached["token"]

    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    response = requests.post(
        EBAY_OAUTH_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    EBAY_TOKEN_CACHE["production"] = {
        "token": token,
        "expires_at": time() + max(60, expires_in - 120),
    }
    return token


def fetch_ebay_sold_sales_marketplace_insights(
    search_query, client_id, client_secret
):
    token = fetch_ebay_access_token(client_id, client_secret)
    response = requests.get(
        EBAY_MARKETPLACE_INSIGHTS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params={
            "q": search_query,
            "limit": "10",
        },
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("itemSales", [])
    prices = []
    for item in items:
        price = price_value((item.get("price") or {}).get("value"))
        if price is not None and price > 0:
            prices.append(price)

    average_price = average(prices[:10])
    return {
        "average": money(average_price),
        "count": len(prices[:10]),
    }


def fetch_ebay_sold_sales_page(search_query):
    response = requests.get(
        EBAY_SOLD_URL,
        params={
            "_nkw": search_query,
            "LH_Sold": "1",
            "LH_Complete": "1",
            "_sop": "13",
            "_ipg": "60",
        },
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
        },
        timeout=8,
    )
    response.raise_for_status()

    prices = extract_ebay_sold_prices(response.text)
    average_price = average(prices[:10])
    return {
        "average": money(average_price),
        "count": len(prices[:10]),
    }


def fetch_ebay_sold_sales_api(search_query, app_id):
    response = requests.get(
        EBAY_FINDING_API_URL,
        params={
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "",
            "keywords": search_query,
            "itemFilter(0).name": "SoldItemsOnly",
            "itemFilter(0).value": "true",
            "sortOrder": "EndTimeSoonest",
            "paginationInput.entriesPerPage": "10",
        },
        timeout=4,
    )
    response.raise_for_status()
    payload = response.json()
    items = (
        payload.get("findCompletedItemsResponse", [{}])[0]
        .get("searchResult", [{}])[0]
        .get("item", [])
    )
    prices = []
    for item in items:
        price = (
            item.get("sellingStatus", [{}])[0]
            .get("convertedCurrentPrice", [{}])[0]
            .get("__value__")
        )
        price = price_value(price)
        if price is not None and price > 0:
            prices.append(price)

    average_price = average(prices[:10])
    return {
        "average": money(average_price),
        "count": len(prices[:10]),
    }


def search_cards(search_term, page=1, sort="name", full_art=False):
    headers = {}
    api_key = os.getenv(API_KEY_ENV)
    if api_key:
        headers["X-Api-Key"] = api_key

    queries, intent = build_card_queries(search_term)

    last_error = None
    had_successful_response = False
    for query in queries:
        if full_art:
            query = apply_full_art_filter(query)

        if sort in PRICE_SORTS:
            collected_cards = []
            total_count = 0
            api_page = 1

            while len(collected_cards) < PRICE_SORT_MAX_CARDS:
                params = {
                    "q": query,
                    "page": api_page,
                    "pageSize": PRICE_SORT_PAGE_SIZE,
                    "orderBy": api_order_for_sort(sort),
                    "select": "id,name,set,number,rarity,artist,images,tcgplayer,cardmarket,subtypes",
                }

                try:
                    response = requests.get(
                        POKEMON_TCG_API_URL,
                        headers=headers,
                        params=params,
                        timeout=12,
                    )
                    response.raise_for_status()
                except requests.RequestException as error:
                    last_error = error
                    break

                had_successful_response = True
                payload = response.json()
                total_count = payload.get("totalCount", 0)
                raw_cards = filter_cards_for_intent(payload.get("data", []), intent)
                collected_cards.extend(
                    normalize_card(card, intent["marketplace_keywords"])
                    for card in raw_cards
                )

                if len(collected_cards) >= total_count or not payload.get("data"):
                    break
                api_page += 1

            if collected_cards:
                sorted_cards = sort_cards_by_price(collected_cards, sort)
                return {
                    "cards": paginate_cards(sorted_cards, page, RESULTS_PER_PAGE),
                    "total_count": len(sorted_cards),
                    "page": page,
                    "page_size": RESULTS_PER_PAGE,
                }

            continue

        params = {
            "q": query,
            "page": page,
            "pageSize": RESULTS_PER_PAGE,
            "orderBy": api_order_for_sort(sort),
            "select": "id,name,set,number,rarity,artist,images,tcgplayer,cardmarket,subtypes",
        }

        try:
            response = requests.get(
                POKEMON_TCG_API_URL,
                headers=headers,
                params=params,
                timeout=12,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            last_error = error
            continue

        had_successful_response = True
        payload = response.json()
        raw_cards = filter_cards_for_intent(payload.get("data", []), intent)
        cards = [
            normalize_card(card, intent["marketplace_keywords"])
            for card in raw_cards
        ]
        if cards:
            return {
                "cards": cards,
                "total_count": payload.get("totalCount", len(cards)),
                "page": page,
                "page_size": RESULTS_PER_PAGE,
            }

    if last_error and not had_successful_response:
        raise last_error
    return {
        "cards": [],
        "total_count": 0,
        "page": page,
        "page_size": RESULTS_PER_PAGE,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/results")
def results():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "name")
    if sort not in SORT_OPTIONS:
        sort = "name"
    full_art = request.args.get("full_art") == "1"

    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    cards = []
    total_count = 0
    page_size = RESULTS_PER_PAGE
    error = None

    if query:
        try:
            result = search_cards(query, page, sort, full_art)
            cards = result["cards"]
            total_count = result["total_count"]
            page_size = result["page_size"]
        except requests.RequestException:
            error = "CardFinder could not reach the Pokémon TCG API. Please try again in a moment."

    total_pages = max(1, (total_count + page_size - 1) // page_size)
    return render_template(
        "results.html",
        query=query,
        cards=cards,
        error=error,
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
        sort=sort,
        sort_options=SORT_OPTIONS,
        full_art=full_art,
    )


@app.route("/api/ebay-sales")
def ebay_sales():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Missing search query."}), 400

    try:
        payload = fetch_ebay_sold_sales(query)
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else "unknown"
        app.logger.exception("eBay sold-pricing HTTP error: %s", status_code)
        source = EBAY_LAST_ERROR.get("source", "eBay API")
        detail = EBAY_LAST_ERROR.get("detail", error.__class__.__name__)
        return jsonify(
            {
                "status": "unavailable",
                "message": f"{source} rejected lookup ({status_code}).",
                "detail": detail,
                "attempts": EBAY_ERROR_ATTEMPTS,
            }
        ), 502
    except requests.RequestException:
        app.logger.exception("eBay sold-pricing lookup failed.")
        source = EBAY_LAST_ERROR.get("source", "eBay API")
        detail = EBAY_LAST_ERROR.get("detail", "Request failed")
        return jsonify(
            {
                "status": "unavailable",
                "message": f"{source} lookup unavailable.",
                "detail": detail,
                "attempts": EBAY_ERROR_ATTEMPTS,
            }
        ), 502

    if payload["count"] == 0:
        return jsonify(
            {
                "status": "empty",
                "message": "No sold prices found.",
            }
        )

    return jsonify(
        {
            "status": "ok",
            "average": payload["average"],
            "count": payload["count"],
        }
    )


@app.route("/ebay/account-deletion", methods=["GET", "POST"])
def ebay_account_deletion():
    if request.method == "GET":
        challenge_code = request.args.get("challenge_code", "").strip()
        if not challenge_code:
            return jsonify({"error": "Missing challenge_code."}), 400

        verification_token = os.getenv(EBAY_VERIFICATION_TOKEN_ENV, "")
        endpoint_url = os.getenv(EBAY_ENDPOINT_URL_ENV, "")
        challenge_response = hashlib.sha256(
            f"{challenge_code}{verification_token}{endpoint_url}".encode("utf-8")
        ).hexdigest()

        response = jsonify({"challengeResponse": challenge_response})
        response.headers["Content-Type"] = "application/json"
        return response

    payload = request.get_json(silent=True) or {}
    app.logger.info("Received eBay account deletion notification: %s", payload)
    return "", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
