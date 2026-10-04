"""Build store/catalog.json: 100 fictional "Northwoods Outfitters" products grounded in real data.

Run:  .venv/bin/python store/build_catalog.py      (downloads ~360 MB of samples into store/.cache once)

Source: Amazon Reviews 2023 item metadata (McAuley Lab, UCSD;
https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023), the first
40–200 MB of three category files. For each product type we keep the
most-reviewed matching items, one per brand, and take their real price, star
rating and review count. Brands, titles and photos are dropped: names are made
up from the kind of item (e.g. "Women's ... Duck Boot"), and the app draws its
own art. Each product keeps its source ASIN for provenance.
"""

import json
import random
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
BASE = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw/meta_categories"
SAMPLES = {"Sports": ("Sports_and_Outdoors", 200_000_000), "Clothing": ("Clothing_Shoes_and_Jewelry", 120_000_000),
           "Home": ("Home_and_Kitchen", 40_000_000)}

# category: [(type, count, source files, title must match, title must not match, price range)]
CATALOG = {
    "Footwear": [
        ("boot", 6, "Clothing", r"\b(hiking|winter|duck|snow|work|chukka|rain) boots?\b",
         r"kid|toddler|girl|boy|baby|sock|insert|lace|charm|liner|tray|dryer|cover", (40, 250)),
        ("moccasin", 4, "Clothing", r"\b(moccasin|slipper)s?\b", r"kid|toddler|girl|boy|baby|sock|disposable|spa", (20, 120)),
        ("sneaker", 3, "Clothing", r"\b(trail running shoes?|hiking shoes?|trail shoes?|camp shoes?)\b",
         r"kid|toddler|girl|boy|baby|lace|insole|sock|wedge|platform|heel", (40, 180)),
    ],
    "Outerwear": [
        ("jacket", 9, "Clothing", r"\b(rain jacket|parka|puffer jacket|down jacket|insulated jacket|softshell jacket|"
                                  r"field jacket|barn jacket|anorak|ski jacket)\b", r"kid|toddler|girl|boy|baby|dog|pet", (50, 400)),
        ("vest", 4, "Clothing", r"\b(puffer vest|fleece vest|down vest|insulated vest|quilted vest)\b",
         r"kid|toddler|girl|boy|baby|dog|pet|safety|reflective", (30, 200)),
    ],
    "Fleece & Sweaters": [
        ("pullover", 11, "Clothing", r"\b(fleece (pullover|jacket)|quarter zip|1/4 zip|cable knit sweater|wool sweater|"
                                     r"fisherman sweater)\b", r"kid|toddler|girl|boy|baby|dog|pet", (30, 200)),
    ],
    "Shirts & Tees": [
        ("shirt", 8, "Clothing", r"\b(flannel shirt|chamois shirt|plaid shirt|button[- ]down shirt|shirt jacket|shacket)\b",
         r"kid|toddler|girl|boy|baby|dog|pet", (20, 120)),
        ("tee", 5, "Clothing", r"\b(t-shirt|tee shirt|crewneck tee|pocket tee|henley)\b",
         r"kid|toddler|girl|boy|baby|dog|pet|funny|graphic|novelty|\bgift\b|print|cardigan", (10, 60)),
    ],
    "Packs & Bags": [
        ("backpack", 7, "Sports", r"\b(hiking backpack|daypack|backpacking (pack|backpack)|travel backpack|rucksack)\b",
         r"kid|bladder|rain cover|strap", (30, 250)),
        ("tote", 5, "Sports|Clothing|Home", r"\b(duffel bag|duffle bag|canvas tote|tote bag|weekender bag)\b",
         r"kid|organizer|insert|liner|cooler|snowshoe|pole", (25, 200)),
    ],
    "Camp & Hike": [
        ("tent", 4, "Sports", r"\b(\d[- ]person tent|camping tent|backpacking tent|dome tent|cabin tent)\b",
         r"footprint|stake|pole|repair|kid|play|beach|canopy", (60, 500)),
        ("sleeping", 3, "Sports", r"\bsleeping bags?\b", r"liner|sack|kid|pad|cot", (40, 350)),
        ("lantern", 4, "Sports|Home", r"\blanterns?\b", r"bulb|mantle|hook|string light|kid|paper|kerosene|oil", (15, 120)),
        ("chair", 3, "Sports", r"\b(camp chair|camping chair|folding chair|backpacking chair)\b",
         r"cover|replacement|kid|cushion|beach", (25, 200)),
    ],
    "Home & Blankets": [
        ("blanket", 8, "Home", r"\b(wool blanket|throw blanket|fleece blanket|sherpa blanket|camp blanket|quilt)\b",
         r"baby|kid|pet|dog|cat|weighted|electric|swaddle", (25, 250)),
        ("mug", 4, "Home|Sports", r"\b(camp mug|enamel mug|coffee mug|travel mug|insulated mug)\b",
         r"set of \d|lid only|warmer|kid", (10, 45)),
    ],
    "Accessories": [
        ("beanie", 5, "Clothing", r"\b(beanie|knit hat|watch cap|trapper hat)\b", r"kid|toddler|girl|boy|baby|dog|pet|led|bluetooth", (10, 50)),
        ("gloves", 3, "Clothing", r"\b(winter gloves|wool gloves|leather gloves|mittens|insulated gloves)\b",
         r"kid|toddler|girl|boy|baby|disposable|nitrile|latex|work gloves|garden|cosplay|paw", (15, 90)),
        ("bottle", 4, "Sports|Home", r"\b(water bottle|insulated bottle|thermos|vacuum bottle)\b",
         r"lid only|replacement|brush|kid|baby|sleeve|boot|cleaning|filter|straw|gallon", (15, 60)),
    ],
}

# What to call each type, from words in the real title: first match wins, the last entry is the default.
NOUNS = {
    "boot": [("duck", "Duck Boot"), ("rain", "Rain Boot"), ("snow", "Snow Boot"), ("chukka", "Chukka Boot"),
             ("work", "Work Boot"), ("winter", "Insulated Winter Boot"), ("", "Waterproof Hiking Boot")],
    "moccasin": [("moccasin", "Shearling Moccasin"), ("", "Camp Slipper")],
    "sneaker": [("trail", "Trail Runner"), ("", "Hiking Shoe")],
    "jacket": [("rain", "Rain Jacket"), ("parka", "Parka"), ("down", "Down Jacket"), ("softshell", "Softshell Jacket"),
               ("field", "Field Coat"), ("barn", "Barn Coat"), ("ski", "Insulated Ski Jacket"), ("anorak", "Anorak"),
               ("", "Puffer Jacket")],
    "vest": [("fleece", "Fleece Vest"), ("down", "Down Vest"), ("", "Quilted Vest")],
    "pullover": [("sweater", "Wool Sweater"), ("quarter", "Quarter-Zip Fleece"), ("1/4", "Quarter-Zip Fleece"),
                 ("", "Fleece Pullover")],
    "shirt": [("flannel", "Flannel Shirt"), ("chamois", "Chamois Shirt"), ("jacket", "Shirt Jacket"),
              ("shacket", "Shirt Jacket"), ("", "Plaid Button-Down")],
    "tee": [("henley", "Waffle Henley"), ("pocket", "Pocket Tee"), ("", "Heritage Tee")],
    "beanie": [("trapper", "Trapper Hat"), ("pom", "Pom Beanie"), ("watch", "Watch Cap"), ("", "Ribbed Beanie")],
    "gloves": [("mitten", "Wool Mittens"), ("leather", "Leather Gloves"), ("", "Insulated Gloves")],
    "backpack": [("daypack", "Daypack"), ("travel", "Travel Pack"), ("", "Hiking Pack")],
    "tote": [("tote", "Canvas Tote"), ("weekender", "Weekender"), ("", "Duffle Bag")],
    "tent": [("1 person", "Solo Tent"), ("2 person", "2-Person Tent"), ("cabin", "Cabin Tent"), ("", "Dome Tent")],
    "sleeping": [("mummy", "Mummy Sleeping Bag"), ("", "3-Season Sleeping Bag")],
    "lantern": [("rechargeable", "Rechargeable Lantern"), ("", "LED Camp Lantern")],
    "chair": [("backpacking", "Backpacking Chair"), ("", "Camp Chair")],
    "blanket": [("wool", "Wool Blanket"), ("sherpa", "Sherpa Throw"), ("quilt", "Patchwork Quilt"), ("", "Fleece Throw")],
    "mug": [("enamel", "Enamel Camp Mug"), ("travel", "Insulated Travel Mug"), ("", "Stoneware Mug")],
    "bottle": [("thermos", "Vacuum Flask"), ("", "Insulated Bottle")],
}
PLACES = ["Ridgeline", "Timberline", "Cold Brook", "Granite", "Birch Run", "Lakeshore", "Northstar", "Ironwood",
          "Harbor", "Summit", "Boundary", "Kettle Pond", "Pine Hollow", "Driftwood", "Saltmarsh", "Highland",
          "Black Spruce", "Trailhead", "Cedar", "Larchmont", "Fieldstone", "Windward", "Tamarack", "Basin"]
COLORS = {"Forest": "#2f5d46", "Navy": "#273b5c", "Rust": "#b5532f", "Charcoal": "#3d4045", "Tan": "#b8956a",
          "Moss": "#6b7a3a", "Slate": "#5a6f80", "Cream": "#e3d6bb", "Red Plaid": "#a8322d", "Mustard": "#c9a03a",
          "Burgundy": "#6e2433", "Sky": "#6f9fc9"}
BLURBS = {
    "boot": "Seam-sealed and lugged for slush, mud and the walk to the mailbox.",
    "moccasin": "Soft suede and a cozy lining, with a sole sturdy enough for the porch.",
    "sneaker": "Light, grippy and ready for the trail the day you get them.",
    "jacket": "Built to block wind and weather without the bulk.",
    "vest": "Core warmth that layers under a shell or over a flannel.",
    "pullover": "The layer you reach for first, from fall mornings to ski lodges.",
    "shirt": "Brushed for softness and cut roomy enough to layer.",
    "tee": "Heavyweight cotton that gets better every wash.",
    "beanie": "Rib-knit warmth that stays put in the wind.",
    "gloves": "Warm hands for shoveling, skating and everything in between.",
    "backpack": "Padded straps, plenty of pockets, room for a full day out.",
    "tote": "Tough canvas that hauls groceries, gear and firewood alike.",
    "tent": "Quick to pitch, steady in the wind, roomy enough to wait out rain.",
    "sleeping": "Warm, packable and rated for shoulder-season nights.",
    "lantern": "Bright, even light for camp, the cabin or a power outage.",
    "chair": "Folds flat, sets up fast, and is comfier than the log.",
    "blanket": "Warm enough for the cabin, soft enough for the couch.",
    "mug": "For coffee at the campsite or cocoa on the porch.",
    "bottle": "Keeps drinks cold all day or hot through the morning.",
}


def fetch(name: str, size: int) -> Path:
    """The first `size` bytes of a metadata file, cached."""
    path = CACHE / f"meta_{name}.jsonl"
    if not path.exists() or path.stat().st_size < size:
        CACHE.mkdir(exist_ok=True)
        print(f"downloading {size / 1e6:.0f} MB of {name} …")
        req = urllib.request.Request(f"{BASE}/meta_{name}.jsonl", headers={"Range": f"bytes=0-{size - 1}"})
        with urllib.request.urlopen(req) as r, open(path, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    return path


def usable(r: object) -> bool:
    try:
        return (isinstance(r, dict) and bool(r.get("title")) and r.get("price") not in (None, "None")
                and int(r.get("rating_number") or 0) >= 30 and float(r.get("average_rating") or 0) >= 3.8)
    except (TypeError, ValueError):
        return False


def load(name: str, size: int) -> list[dict]:
    rows = []
    for line in fetch(name, size).open():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:  # the sample's last line is cut off
            continue
        if usable(r):
            rows.append(r)
    return rows


def noun(kind: str, title: str) -> str:
    t = title.lower()
    return next(n for key, n in NOUNS[kind] if key in t)


def who(title: str) -> str:
    t = title.lower()
    return "Women's " if re.search(r"\bwomen'?s\b|\bwomens\b", t) else "Men's " if re.search(r"\bmen'?s\b|\bmens\b", t) else ""


def main() -> None:
    rows = {key: load(name, size) for key, (name, size) in SAMPLES.items()}
    rng = random.Random(7)
    places = PLACES[:]
    rng.shuffle(places)
    products, names = [], set()
    for category, kinds in CATALOG.items():
        for kind, count, files, include, exclude, (lo, hi) in kinds:
            pool = [r for f in files.split("|") for r in rows[f]
                    if re.search(include, r["title"].lower()) and not re.search(exclude, r["title"].lower())
                    and lo <= float(r["price"]) <= hi]
            pool.sort(key=lambda r: -int(r["rating_number"]))
            brands, picked = set(), []
            for r in pool:  # most reviewed first, one per brand
                if r.get("store") in brands:
                    continue
                brands.add(r.get("store"))
                picked.append(r)
                if len(picked) == count:
                    break
            if len(picked) < count:
                raise SystemExit(f"only {len(picked)} {kind} items; fetch more data")
            for r in picked:
                base = f"{who(r['title'])}{noun(kind, r['title'])}"
                name = next(f"{p} {base}" for p in places * 4 if f"{p} {base}" not in names)
                names.add(name)
                places.append(places.pop(0))  # rotate so names vary within a type
                color = rng.choice([c for c in COLORS if c != "Red Plaid" or kind in ("shirt", "blanket")])
                products.append({
                    "id": f"p{len(products) + 1:03d}",
                    "name": name,
                    "category": category,
                    "type": kind,
                    "color": color,
                    "hex": COLORS[color],
                    "price": round(float(r["price"]), 2),
                    "rating": float(r["average_rating"]),
                    "reviews": int(r["rating_number"]),
                    "blurb": BLURBS[kind],
                    "source": {"dataset": "Amazon Reviews 2023 (McAuley Lab)", "parent_asin": r["parent_asin"]},
                })
    out = HERE / "catalog.json"
    out.write_text(json.dumps({"store": "Northwoods Outfitters", "categories": list(CATALOG), "products": products},
                              indent=1))
    print(f"wrote {len(products)} products in {len(CATALOG)} categories to {out}")


if __name__ == "__main__":
    main()
