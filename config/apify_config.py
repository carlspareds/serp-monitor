"""
Apify configuration
"""

# Main actor (correct format for the API)
GOOGLE_SEARCH_SCRAPER = "apify~google-search-scraper"

# Bing actor (1.1, multi-engine extraction): known public Bing actor on
# Apify. No real confirmed payload sample as of this item -- see
# apify.actors.normalize_bing (DEFENSIVE normalizer).
BING_SEARCH_SCRAPER = "tri_angle~bing-search-scraper"

# Default actor configuration
DEFAULT_INPUT = {
    "queries": [],
    "maxPagesPerQuery": 1,
    "resultsPerPage": 10,
    "countryCode": "us",
    "languageCode": "en",
    "maxConcurrency": 10
}

# Language by country (as in the manual Excel). Defaults to 'en'.
COUNTRY_LANG = {
    "us": "en",
    "ca": "en",
    "gb": "en",
    "global": "en",
    "hk": "en",
    "fr": "fr",
    "ch": "de",
    "de": "de",
    "it": "it",
    "nl": "nl",
}


def lang_for(country_code: str) -> str:
    return COUNTRY_LANG.get((country_code or "").lower(), "en")