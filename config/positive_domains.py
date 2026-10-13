"""
Classification lists
================================
DEPLOYMENT-WIDE positive domains and the keywords that flag a result as
NEGATIVE.

Positive domains are normally PER SUBJECT (multi-subject phase 1): each
tracked person/brand owns its list in the database (positive_domains,
managed from /domains). POSITIVE_DOMAINS below is merged on top of EVERY
subject's list (merged_with) -- keep it empty unless a domain really is
positive for everyone this deployment tracks. Entries are bare domains
(without 'www.', without 'https://'); the matcher recognizes the exact
domain and any subdomain.

The fictional demo subject's domains (Jordan Ashworth, *.example) used to
live here; they are now seeded into the demo subject's own rows by
scripts/seed_demo_data.py.
"""

# Positive for EVERY subject (merged into each subject's DB list). Empty by default.
POSITIVE_DOMAINS: list[str] = []

# URLs / domains that are public registries or frequent homonyms → always
# neutral (before negative keywords or AI). UK Companies House
# officers/appointments.
FORCE_NEUTRAL_URL_FRAGMENTS = [
    "company-information.service.gov.uk",
]

# Words that suggest negative content
NEGATIVE_KEYWORDS = [
    "fraud",
    "scam",
    "lawsuit",
    "criminal",
    "arrested",
    "convicted",
    "indicted",
    "fraudster",
    "ponzi",
    "embezzle",
    "allegation",
    "alleged",
    "court",
    "sued",
]


def merged_with(extra: list[str] | None) -> list[str]:
    """POSITIVE_DOMAINS + extra domains (the operator's, in the DB), in
    lowercase and without duplicates, stable order. Single source of
    truth for the merge -- used by the dashboard and the weekly report
    (audit F0)."""
    seen, merged = set(), []
    for d in list(POSITIVE_DOMAINS) + list(extra or []):
        d = d.lower()
        if d not in seen:
            seen.add(d)
            merged.append(d)
    return merged
