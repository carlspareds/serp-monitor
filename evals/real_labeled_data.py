"""Real SERP-style benchmark data for evals/run_evals.py.

Every row below is a REAL result from a real web search (via a general web
search tool, run on 2026-09-21) for six well-known public figures/
companies -- Tim Cook, Boeing, Elon Musk, Wells Fargo, Patagonia, Taylor
Swift -- none of them clients, none of them the classifier's own test
fixtures. Labels were produced by an LLM reading each result's actual
title/URL/content, independently of processing.classifier's rules, and
are not yet human-verified. See evals/README.md for
why this exists and its limitations (no exact SERP rank position -- a
general web search, not a Google/Bing scrape; single-evaluator labels, not
cross-checked; title-only, no snippet text, which is a harder and more
honest test than feeding the classifier a snippet that already contains the
search terms).

Each row: (query, url, title, expected_label).
"""

REAL_LABELED_RESULTS: list[tuple[str, str, str, str]] = [
    # --- Tim Cook (Apple CEO transition, Sept 2026) ---
    ("Tim Cook", "https://abcnews.com/US/apple-ceo-tim-cook-stepping-john-ternus-set/story?id=132217743",
     "Apple CEO Tim Cook is stepping down, John Ternus set to replace him", "neutral"),
    ("Tim Cook", "https://www.apple.com/newsroom/2026/04/tim-cook-to-become-apple-executive-chairman-john-ternus-to-become-apple-ceo/",
     "Tim Cook to become Apple Executive Chairman John Ternus to become Apple CEO", "positive"),
    ("Tim Cook", "https://www.nbcbayarea.com/news/local/tim-cook-apple-ceo/4135772/",
     "Tim Cook to step down as Apple CEO after 15-year tenure", "neutral"),
    ("Tim Cook", "https://www.foxbusiness.com/markets/tim-cooks-last-day-apple-ceo-how-he-led-tech-giants-rise-4t",
     "Tim Cook steps down as Apple CEO after 15 years, names a successor", "positive"),
    ("Tim Cook", "https://www.npr.org/2026/09/01/g-s1-141411/apple-ceo-tim-cook-john-ternus",
     "John Ternus replaces Tim Cook as CEO of Apple", "neutral"),
    ("Tim Cook", "https://techcrunch.com/2026/08/31/tim-cooks-parting-message-apple-is-in-the-hands-of-a-product-builder/",
     "Tim Cook's parting message: Apple is in the hands of a product builder", "positive"),
    ("Tim Cook", "https://finance.yahoo.com/technology/article/what-tim-cook-told-employees-on-his-last-day-at-apple-171506698.html",
     "What Tim Cook told employees on his last day at Apple", "neutral"),
    ("Tim Cook", "https://cbsnews.com/sanfrancisco/tag/tim-cook/4",
     "Apple CEO Tim Cook Says Fake News Is 'Killing People's Minds'", "neutral"),
    ("Tim Cook", "https://cbsnews.com/sanfrancisco/tag/tim-cook/3",
     "Newspapers Largely Shun Apple's News Subscription Service", "negative"),

    # --- Boeing (737 MAX coverage) ---
    ("Boeing", "https://www.cbsnews.com/tag/boeing-737-max/",
     "Boeing 737 Max news - Today's latest updates", "neutral"),
    ("Boeing", "https://www.faa.gov/newsroom/updates-boeing-737-9-max-aircraft",
     "Updates on Boeing 737-9 MAX Aircraft", "negative"),
    ("Boeing", "https://boeing.mediaroom.com/2026-07-20-SMBC-Aviation-Capital-Orders-100-Boeing-737-MAX-Jets",
     "SMBC Aviation Capital Orders 100 Boeing 737 MAX Jets", "positive"),
    ("Boeing", "https://boeing.mediaroom.com/2026-01-13-Aviation-Capital-Group-Orders-50-Boeing-737-MAX-Jets",
     "Aviation Capital Group Orders 50 Boeing 737 MAX Jets", "positive"),
    ("Boeing", "https://www.boeing.com/737-max-updates/",
     "Boeing 737 MAX Updates - 737 MAX Return To Service Updates & Information", "neutral"),
    ("Boeing", "https://www.pbs.org/newshour/tag/boeing-737-max/page/4",
     "Full Episode", "neutral"),
    ("Boeing", "https://www.pbs.org/newshour/tag/boeing-737/page/2",
     "Full Episode", "neutral"),
    ("Boeing", "https://www.cbsnews.com/chicago/news/boeings-newest-737-first-flight",
     "Watch CBS News: Boeing's newest 737 first flight", "positive"),
    ("Boeing", "https://cbsnews.com/video/faa-lifts-grounding-order-on-boeing-737-max",
     "Watch CBS News: FAA lifts grounding order on Boeing 737 Max", "positive"),

    # --- Elon Musk ---
    ("Elon Musk", "https://www.pbs.org/newshour/tag/elon-musk",
     "Elon Musk | PBS News", "neutral"),
    ("Elon Musk", "https://www.rollingstone.com/t/elon-musk/",
     "Elon Musk", "neutral"),
    ("Elon Musk", "https://www.commondreams.org/tag/elon-musk",
     "Elon Musk on Common Dreams", "negative"),
    ("Elon Musk", "https://x.com/elonmusk",
     "Elon Musk (@elonmusk) on X", "neutral"),
    ("Elon Musk", "https://www.youtube.com/watch?v=5PIbIk7I4QQ",
     "Elon Musk UNLEASHES his AI VISION at the G20: the FUTURE is CLOSER than YOU THINK", "positive"),
    ("Elon Musk", "https://www.youtube.com/watch?v=iwBOQeFPAwg",
     "Elon Musk & Gwynne Shotwell on AI Risks and Peer Review, Starship, Terafab, SpaceX/Tesla Merger",
     "neutral"),
    ("Elon Musk", "https://teslanewstonight.substack.com/p/elon-musk",
     "elon musk", "neutral"),
    ("Elon Musk", "https://stagezeronews.substack.com",
     "Stage Zero News", "neutral"),

    # --- Wells Fargo (fake-accounts scandal coverage) ---
    ("Wells Fargo", "https://www.mheducation.com/highered/blog/2025/07/after-the-scandal--wells-fargo-is-allowed-to-grow-again.html",
     "After the Scandal: Wells Fargo Is Allowed to Grow Again", "positive"),
    ("Wells Fargo", "https://www.congress.gov/crs-product/IF11129",
     "Wells Fargo - A Timeline of Recent Consumer Protection and Corporate Governance Scandals",
     "negative"),
    ("Wells Fargo", "https://www.learnsignal.com/blog/wells-fargo-fake-accounts-scandal-overview/",
     "The Wells Fargo Fake Accounts Scandal", "negative"),
    ("Wells Fargo", "https://www.gsb.stanford.edu/faculty-research/publications/wells-fargo-cross-selling-scandal",
     "The Wells Fargo Cross-Selling Scandal", "negative"),
    ("Wells Fargo", "https://www.bankingdive.com/news/occ-3-ex-wells-fargo-execs-fine-18-million-russ-anderson-mclinko-julian-fake-accounts/737386/",
     "3 ex-Wells Fargo execs fined $18.5M over fake-accounts scandal", "negative"),
    ("Wells Fargo", "https://www.forrester.com/cx-cast/78-what-cx-pros-can-learn-from-the-wells-fargo-scandal",
     "78: what CX pros can learn from the Wells Fargo scandal", "negative"),
    ("Wells Fargo", "https://www.npr.org/series/508986161/investigating-the-wells-fargo-scandal/archive",
     "Investigating the Wells Fargo Scandal", "negative"),
    ("Wells Fargo", "https://cbsnews.com/news/john-stumpf-exits-wells-fargo-after-account-scandal",
     "John Stumpf exits Wells Fargo after account scandal", "negative"),
    ("Wells Fargo", "https://www.nbcnews.com/news/amp/ncna793016",
     "Wells Fargo Picks First Woman to Lead Board as Scandal Worsens", "negative"),

    # --- Patagonia (brand reputation coverage) ---
    ("Patagonia", "https://www.outsideonline.com/outdoor-gear/gear-news/patagonia-axios-harris-poll-2023/",
     "Patagonia Has the Best Reputation of Any Business in America, Poll Shows", "positive"),
    ("Patagonia", "https://www.fashionbeans.com/article/is-patagonia-a-good-brand/",
     "Is Patagonia a Good Brand? Why This Outdoor Brand Stands Above the Rest", "positive"),
    ("Patagonia", "https://www.inc.com/jason-aten/patagonia-was-just-ranked-most-respected-brand-this-unconventional-idea-from-its-founder-explains-why.html",
     "Patagonia Was Just Ranked the Most Respected Brand", "positive"),
    ("Patagonia", "https://www.trustpilot.com/review/www.patagonia.com",
     "Patagonia Reviews | Read Customer Service Reviews of www.patagonia.com", "neutral"),
    ("Patagonia", "https://thesustainableagency.com/blog/is-patagonia-greenwashing/",
     "Is Patagonia greenwashing? A look at 2025's Impact Report", "negative"),
    ("Patagonia", "https://directory.goodonyou.eco/brand/patagonia",
     "Patagonia - Sustainability Rating - Good On You", "neutral"),
    ("Patagonia", "https://www.forbes.com/sites/dougsundheim/2023/12/12/how-patagonia-became-the-most-reputable-brand-in-the-united-states/",
     "How Patagonia Became The Most Reputable Brand In The United States", "positive"),
    ("Patagonia", "https://www.axios.com/2023/05/25/patagonia-harris-poll-reputation",
     "Patagonia eco-brand unscathed by polarized climate", "positive"),
    ("Patagonia", "https://www.nbcnews.com/business/business-news/patagonia-clothing-company-has-hidden-election-message-customers-n1240154",
     "Patagonia clothing company has hidden election message for customers", "neutral"),

    # --- Taylor Swift ---
    ("Taylor Swift", "https://x.com/DailyTSwiftNews",
     "Taylor Swift News (@DailyTSwiftNews) on X", "neutral"),
    ("Taylor Swift", "https://www.foxnews.com/category/person/taylor-swift",
     "Taylor Swift | Taylor Swift Latest News & Updates | Travis Kelce Relationship", "neutral"),
    ("Taylor Swift", "https://en.wikipedia.org/wiki/Bryan_West_(journalist)",
     "Bryan West (journalist)", "neutral"),
    ("Taylor Swift", "https://www.taylorswift.com/",
     "The Official Website of Taylor Swift", "positive"),
    ("Taylor Swift", "https://www.eonline.com/news/taylor_swift",
     "Taylor Swift News, Pictures, and Videos", "neutral"),
    ("Taylor Swift", "https://www.cbsnews.com/newyork/news/taylor-swift-to-announce-really-cool-news-during-live-web-chat",
     "Taylor Swift to announce really cool news during live web chat", "positive"),
    ("Taylor Swift", "https://www.cbsnews.com/news/taylor-swift-is-working-on-new-music-according-to-gigi-hadid",
     "Taylor Swift is working on new music according to Gigi Hadid", "positive"),
    ("Taylor Swift", "https://www.yahoo.com/topics/taylor-swift",
     "taylor swift", "neutral"),
    ("Taylor Swift", "https://au.rollingstone.com/t/vmas-2022",
     "VMAs 2022", "neutral"),
]
