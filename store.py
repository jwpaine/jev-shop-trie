"""Shop a 100-product outdoor store; every visit, and how it ended, grows one live trie.

Run:  .venv/bin/uvicorn store:app --reload   then open http://localhost:8000
      (press "+100 shoppers" a few times first, so the trie has a crowd)

    store/catalog.json    Northwoods Outfitters: made-up names and art, real prices,
                          ratings and review counts (see store/build_catalog.py)
    store/page.json       the page skeleton, with a "modules" slot
    store/modules/*.json  suggestion strips
    store/trie.json       the crowd trie (written by the app; Clear deletes it)

A step is a page *type*, not a product, because that's how shopping moves:
home, a search topic, a category listing, a product page (grouped by
category, the product itself is counted inside the node), the cart, and
"bought". Each node counts the visits that passed through it, where they went
next, how those visits ended (bought or left) and what they bought, so every
node has a conversion rate.

    stats     ── your odds of buying and your most likely purchase, from your node's outcomes
    Jev       ── from the second step, two calls per click, side by side:
                 · reads your mindset (decisive, comparing, browsing, gift hunting, deal hunting,
                   about to leave) from your pages plus the crowd's view from your position;
                   MINDSET_STRIPS picks the strips (nudges)
                 · predicts your purchase by matching your visit against recorded purchase
                   journeys (those continuing from exactly here first, then ones sharing a
                   shorter start); the closest match wins, ties go to the most shoppers
    ask       ── routes from here that ended in a purchase; one Jev call scores each for fit
                 with your question, the best fit wins, ties go to the route most shoppers took
    simulator ── realistic visits (most leave early, few buy), choosing products in proportion
                 to their real review counts

    GET /users/{id}   a shopper's position, mindset, odds of buying and likely purchase
"""

import asyncio
import html
import json
import math
import os
import random
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, create_model

load_dotenv()  # jev reads TYPESAFE_API_KEY from the environment

import jev
from store_art import art

ROOT = Path(__file__).parent / "store"
PAGE = json.loads((ROOT / "page.json").read_text())
MODULES = {m["id"]: m for m in (json.loads(p.read_text()) for p in sorted((ROOT / "modules").glob("*.json")))}
CATALOG = json.loads((ROOT / "catalog.json").read_text())
STORE = CATALOG["store"]
CATEGORIES: list[str] = CATALOG["categories"]
PRODUCTS = {p["id"]: p for p in CATALOG["products"]}
BY_CATEGORY = {c: sorted((p for p in PRODUCTS.values() if p["category"] == c), key=lambda p: -p["reviews"])
               for c in CATEGORIES}
SLUG = {c: re.sub(r"[^a-z]+", "-", c.lower()).strip("-") for c in CATEGORIES}
CATEGORY_OF = {v: k for k, v in SLUG.items()}
# Validated categorical palette (dataviz reference), light and dark steps, in category order.
HUE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
HUE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

TOPICS = {  # search topics: label, which products match
    "gifts": ("Gift ideas", lambda p: p["price"] <= 60 and p["type"] in
              ("beanie", "gloves", "mug", "bottle", "blanket", "moccasin", "pullover", "lantern", "shirt")),
    "cold": ("Cold-weather gear", lambda p: p["type"] in ("boot", "jacket", "vest", "pullover", "gloves", "beanie")),
    "camping": ("Camping", lambda p: p["type"] in ("tent", "sleeping", "lantern", "chair", "backpack", "bottle")),
    "cozy": ("Cozy at home", lambda p: p["type"] in ("blanket", "mug", "moccasin", "pullover", "shirt")),
    "deals": ("Deals under $30", lambda p: p["price"] < 30),
}

DEPTH = 10  # steps of a visit kept in the trie
MIN_ODDS = 5  # finished visits through a node before quoting its odds of buying
PRUNE = 0.006  # the drawing hides branches below this share of all visits (your path always shows)
MAX_ROUTES = 40  # purchase routes Jev scores per question
FIT = ["unrelated", "partly", "good", "exactly"]
NEW_SECONDS = 4

# ---------------------------------------------------------------------------
# Steps: page types
# ---------------------------------------------------------------------------


def step_of(kind: str, arg: str = "") -> str:
    if kind == "product":
        return f"product:{SLUG[PRODUCTS[arg]['category']]}"
    return f"{kind}:{arg}" if arg and kind in ("search", "list") else kind


def label(step: str) -> str:
    kind, _, arg = step.partition(":")
    return {"home": "Home", "cart": "Cart", "bought": "Bought",
            "search": f"Search: {TOPICS[arg][0]}" if arg in TOPICS else "Search",
            "list": CATEGORY_OF.get(arg, arg), "product": f"{CATEGORY_OF.get(arg, arg)} product"}[kind]


def category_index(step: str) -> int | None:
    kind, _, arg = step.partition(":")
    return CATEGORIES.index(CATEGORY_OF[arg]) if kind in ("list", "product") and arg in CATEGORY_OF else None


# ---------------------------------------------------------------------------
# Memory: the crowd trie, with outcomes
# ---------------------------------------------------------------------------


def blank() -> dict[str, Any]:
    return {"visits": 0, "born": time.time(), "next": {}, "out": {"bought": 0, "left": 0}, "buys": {}, "views": {},
            "kids": {}}


class CrowdTrie:
    """node = {"visits", "next": {step: n}, "out": {"bought", "left"}, "buys": {product: n},
    "views": {product: n}, "kids": {step: node}}"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.root = json.loads(path.read_text()) if path.exists() else blank()

    def find(self, trail: list[str]) -> dict[str, Any]:
        node = self.root
        for s in trail[:DEPTH]:
            if s not in node["kids"]:
                return blank()
            node = node["kids"][s]
        return node

    def step(self, trail: list[str], product: str | None = None) -> None:
        """Count the trail's last step (if it's within DEPTH)."""
        if len(trail) > DEPTH:
            return
        node = self.root
        if len(trail) == 1:
            node["visits"] += 1
        for s in trail[:-1]:
            node = node["kids"].setdefault(s, blank())
        node["next"][trail[-1]] = node["next"].get(trail[-1], 0) + 1
        kid = node["kids"].setdefault(trail[-1], blank())
        kid["visits"] += 1
        if product:
            kid["views"][product] = kid["views"].get(product, 0) + 1

    def finish(self, trail: list[str], outcome: Literal["bought", "left"], items: list[str]) -> None:
        """Credit how the visit ended to every node it passed through."""
        node = self.root
        for s in [None, *trail[:DEPTH]]:
            if s is not None:
                if s not in node["kids"]:
                    break
                node = node["kids"][s]
            node["out"][outcome] += 1
            for p in items if outcome == "bought" else []:
                node["buys"][p] = node["buys"].get(p, 0) + 1

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.root))
        os.replace(tmp, self.path)


crowd = CrowdTrie(ROOT / "trie.json")


def odds(node: dict[str, Any]) -> tuple[float | None, int]:
    done = node["out"]["bought"] + node["out"]["left"]
    return (node["out"]["bought"] / done if done >= MIN_ODDS else None), done


def backoff(trail: list[str]) -> tuple[dict[str, Any], int]:
    """The node for the longest start of this visit that enough finished visits shared,
    and how many steps that is. Deep, unusual paths fall back to their first few steps."""
    trail = trail[:DEPTH]
    for k in range(len(trail), 0, -1):
        node = crowd.find(trail[:k])
        if odds(node)[0] is not None:
            return node, k
    return crowd.root, 0


def continuation(trail: list[str]) -> tuple[list[str], str | None]:
    """The likeliest way from here to a purchase: at each step, the branch through which
    the most visits went on to buy. Returns the steps after `trail` and the product most
    bought at the end, or ([], None) if no visit from here ever bought."""
    trail = trail[:DEPTH]
    node, steps = crowd.find(trail), []
    while len(trail) + len(steps) < DEPTH:
        kids = node["kids"]
        if kids.get("bought", {}).get("visits"):
            steps.append("bought")
            return steps, top(kids["bought"]["buys"])
        best = max(kids, key=lambda k: (kids[k]["out"]["bought"], kids[k]["visits"]), default=None)
        if best is None or not kids[best]["out"]["bought"]:
            break
        steps.append(best)
        node = kids[best]
    return [], None


def top(counts: dict[str, int]) -> str | None:
    return max(counts, key=counts.get) if counts else None


# ---------------------------------------------------------------------------
# The simulator: realistic visits, products weighted by real review counts
# ---------------------------------------------------------------------------


def weighted(products: list[dict[str, Any]], rng: random.Random) -> dict[str, Any]:
    return rng.choices(products, weights=[math.sqrt(p["reviews"]) for p in products])[0]


CATEGORY_WEIGHTS = [sum(math.sqrt(p["reviews"]) for p in BY_CATEGORY[c]) for c in CATEGORIES]


def simulate_visit(rng: random.Random) -> tuple[list[tuple[str, str]], str, list[str]]:
    """One shopper: a list of (kind, arg) pages, how it ended, and what was bought."""
    pages: list[tuple[str, str]] = []
    cart: list[str] = []
    category = rng.choices(CATEGORIES, weights=CATEGORY_WEIGHTS)[0]
    r = rng.random()
    if r < 0.55:
        pages.append(("home", ""))
    elif r < 0.8:
        pages.append(("search", rng.choices(list(TOPICS), weights=[4, 3, 2, 2, 3])[0]))
    else:  # landed on a product from an ad or a link
        pages.append(("product", weighted(BY_CATEGORY[category], rng)["id"]))
    for _ in range(24):
        kind, arg = pages[-1]
        r = rng.random()
        if kind == "home":
            if r < 0.68:
                category = rng.choices(CATEGORIES, weights=CATEGORY_WEIGHTS)[0]
                pages.append(("list", SLUG[category]))
            elif r < 0.8:
                pages.append(("search", rng.choice(list(TOPICS))))
            else:
                return pages, "left", []
        elif kind == "search":
            hits = [p for p in PRODUCTS.values() if TOPICS[arg][1](p)]
            if r < 0.68:
                p = weighted(hits, rng)
                category = p["category"]
                pages.append(("product", p["id"]))
            else:
                return pages, "left", []
        elif kind == "list":
            if r < 0.68:
                pages.append(("product", weighted(BY_CATEGORY[category], rng)["id"]))
            elif r < 0.84:
                category = rng.choices(CATEGORIES, weights=CATEGORY_WEIGHTS)[0]
                pages.append(("list", SLUG[category]))
            else:
                return pages, "left", []
        elif kind == "product":
            if r < 0.4:
                pages.append(("list", SLUG[category]))
            elif r < 0.64:
                pages.append(("product", weighted(BY_CATEGORY[category], rng)["id"]))
            elif r < 0.69:
                cart.append(arg)
                pages.append(("cart", ""))
            else:
                return pages, "left", []
        elif kind == "cart":
            if r < 0.4:
                pages.append(("bought", ""))
                return pages, "bought", cart
            if r < 0.7:
                pages.append(("list", SLUG[category]))
            else:
                return pages, "left", []
    return pages, "left", []


def simulate(n: int) -> None:
    rng = random.Random()
    for _ in range(n):
        pages, outcome, items = simulate_visit(rng)
        trail: list[str] = []
        for kind, arg in pages:
            trail.append(step_of(kind, arg))
            crowd.step(trail, arg if kind == "product" else None)
        crowd.finish(trail, outcome, items)  # type: ignore[arg-type]
    crowd.save()


# ---------------------------------------------------------------------------
# Judgment: Jev reads the shopper's mindset, and maps questions to routes
# ---------------------------------------------------------------------------

MINDSETS = {
    "decisive": ("Decisive", "knows what they want"),
    "comparing": ("Comparing", "weighing options in one category"),
    "browsing": ("Browsing", "wandering across categories"),
    "gift": ("Gift hunting", "shopping for someone else"),
    "deal": ("Deal hunting", "looking for low prices"),
    "leaving": ("About to leave", "losing interest"),
}
MINDSET_STRIPS = {
    "decisive": ["path_buys"],
    "comparing": ["compare", "path_buys"],
    "browsing": ["popular", "path_buys"],
    "gift": ["gift_picks", "path_buys"],
    "deal": ["deals", "path_buys"],
    "leaving": ["free_ship"],
}


class Mindset(jev.BaseModel):
    mindset: Literal["decisive", "comparing", "browsing", "gift", "deal", "leaving"] = Field(description=(
        "Which best describes this shopper right now? "
        "decisive: heading straight for a product and the cart. "
        "comparing: looking at several products in the same category, going back and forth. "
        "browsing: wandering across different categories without settling. "
        "gift: shopping for someone else (gift searches, gift-type items, mixed sizes or styles). "
        "deal: drawn to low prices, deals searches, cheap items. "
        "leaving: losing interest (bouncing back without engaging, long aimless trail)."
    ))
    clear: bool = Field(description=(
        "Is that mindset clear? Yes when three or more steps fit it and no other fits as well; "
        "no when it's a guess from one or two steps."
    ))


def describe(p: dict[str, Any]) -> str:
    return f'{p["name"]} ({p["category"]}, {p["color"]}, ${p["price"]:.2f}, {p["rating"]}★ from {p["reviews"]:,} reviews)'


def routes(trail: list[str]) -> list[dict[str, Any]]:
    """Up to MAX_ROUTES (route, product) pairs: routes from here that ended in buying that product."""
    start = trail[:DEPTH]
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: list[str]) -> None:
        for s, kid in node["kids"].items():
            if s == "bought":
                for product, n in sorted(kid["buys"].items(), key=lambda kv: -kv[1])[:3]:
                    found.append({"route": [*path, s], "product": product, "shoppers": n})
            elif len(start) + len(path) + 1 < DEPTH:
                walk(kid, [*path, s])

    walk(crowd.find(start), [])
    # Most shoppers first, but spread across what was bought: take routes round-robin by the
    # category of their top purchase, so a camping question can find camping routes.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(found, key=lambda r: -r["shoppers"]):
        buckets.setdefault(PRODUCTS[r["product"]]["category"], []).append(r)
    picked: list[dict[str, Any]] = []
    while len(picked) < MAX_ROUTES and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(picked) < MAX_ROUTES:
                picked.append(bucket.pop(0))
    return sorted(picked, key=lambda r: -r["shoppers"])


def fit_model(candidates: list[dict[str, Any]]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for i, r in enumerate(candidates):
        fields[f"route_{i}"] = (int, Field(ge=0, le=3, json_schema_extra={"levels": FIT}, description=(
            f"Shoppers went {' → '.join(label(s) for s in r['route'])} and bought the {describe(PRODUCTS[r['product']])}. "
            "How well does that product match what this shopper asked for (who it's for, budget, use)?"
        )))
    return create_model("Fit", **fields)


async def ask(trail: list[str], question: str) -> dict[str, Any]:
    start = trail[:DEPTH] if "bought" not in trail else []
    candidates = routes(start)
    from_start = not candidates and bool(start)
    if from_start:  # no purchase continues from exactly here: score whole journeys instead
        start, candidates = [], routes([])
    result: dict[str, Any] = {"question": question, "from": start, "routes": [], "error": None, "ms": 0.0,
                              "scored": len(candidates), "from_start": from_start}
    if not candidates:
        result["error"] = "No purchase has happened from here yet. Add shoppers first."
        return result
    started = time.perf_counter()
    try:
        fits = (await jev.adecide({"shopper_asked": question, "store": f"{STORE}, an outdoor and home goods store",
                                   "categories": CATEGORIES}, fit_model(candidates))).model_dump()
        ranked = sorted(((fits[f"route_{i}"], r) for i, r in enumerate(candidates)),
                        key=lambda fr: (-fr[0], -fr[1]["shoppers"]))
        seen: set[str] = set()  # one route per product: its best-fitting, most-traveled one
        result["routes"] = [{**r, "fit": FIT[f]} for f, r in ranked
                            if r["product"] not in seen and not seen.add(r["product"])][:3]
    except Exception as exc:  # a demo: say why
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["ms"] = (time.perf_counter() - started) * 1000
    return result


def crowd_view(trail: list[str]) -> dict[str, Any]:
    """What the crowd did from your position (or the longest start of your visit with data)."""
    node, k = backoff(trail)
    nxt = node["next"]
    total = sum(nxt.values())
    p_buy, done = odds(node)
    return {
        "shoppers_who_matched_your_first_steps": node["visits"] if k else crowd.root["visits"],
        "steps_matched": k,
        "where_they_went_next": {label(st): f"{n / total:.0%}" for st, n in sorted(nxt.items(), key=lambda kv: -kv[1])[:5]}
        if total else {},
        "share_who_bought": f"{p_buy:.1%}" if p_buy is not None else "unknown",
        "what_they_bought": [PRODUCTS[b]["name"] for b, _ in sorted(node["buys"].items(), key=lambda kv: -kv[1])[:3]],
    }


MATCH = ["different", "somewhat", "close", "same way"]


def match_candidates(trail: list[str]) -> list[dict[str, Any]]:
    """Recorded purchase journeys to compare a visit against: those continuing from its
    exact position first, then those sharing shorter and shorter starts with it."""
    trail = trail[:DEPTH]
    found, seen = [], set()
    for k in range(len(trail), -1, -1):
        for r in routes(trail[:k]):
            full = [*trail[:k], *r["route"]]
            key = (tuple(full), r["product"])
            if key not in seen:
                seen.add(key)
                found.append({"route": full, "product": r["product"], "shoppers": r["shoppers"], "shared": k})
            if len(found) >= MAX_ROUTES:
                return found
    return found


def match_model(candidates: list[dict[str, Any]]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for i, c in enumerate(candidates):
        fields[f"route_{i}"] = (int, Field(ge=0, le=3, json_schema_extra={"levels": MATCH}, description=(
            f"Other shoppers went {' → '.join(label(st) for st in c['route'])} and bought the "
            f"{describe(PRODUCTS[c['product']])}. How closely does this shopper's visit so far, and where they are now, "
            "match the start of that route, so that they are likely heading the same way?"
        )))
    return create_model("Match", **fields)


MATCH_WEIGHT = [0, 1, 3, 6]  # different, somewhat, close, same way


async def predict_match(s: "Session") -> None:
    """Jev's purchase prediction: Jev scores how closely each recorded purchase journey matches
    this visit; each journey then votes for its product with weight = match × shoppers ×
    (1 + steps it shares with the visit). The product with the most support wins."""
    if s.page_kind == "cart" and s.cart:  # at the cart, the cart is the prediction
        s.prediction = {"product": s.cart[-1], "route": [*s.trail[:DEPTH], "bought"], "match": "in your cart",
                        "shoppers": 0, "shared": len(s.trail[:DEPTH]), "support": 1.0, "scored": 0, "at": len(s.trail)}
        return
    candidates = match_candidates(s.trail)
    if not candidates:
        s.prediction = None
        return
    started = time.perf_counter()
    try:
        scores = (await jev.adecide({
            "context": f"A shopper browsing {STORE}. Predict where their visit is heading by comparing it with "
                       "routes other shoppers took to a purchase.",
            "this_visit_so_far": visit_steps(s),
            "current_page": label(s.trail[-1]),
            "cart": [describe(PRODUCTS[i]) for i in s.cart],
            "last_mindset_read": s.mindset,
        }, match_model(candidates))).model_dump()
        scored = [(scores[f"route_{i}"], c) for i, c in enumerate(candidates)]
        mass: dict[str, float] = {}
        for score, c in scored:
            mass[c["product"]] = mass.get(c["product"], 0) + MATCH_WEIGHT[score] * c["shoppers"] * (1 + c["shared"])
        if not any(mass.values()):
            s.prediction = None
            return
        product = max(mass, key=mass.get)
        # the route to draw: this product's best-matching, most-shared, most-traveled journey
        score, best = max(((sc, c) for sc, c in scored if c["product"] == product),
                          key=lambda sc: (sc[0], sc[1]["shared"], sc[1]["shoppers"]))
        backers = sum(c["shoppers"] for sc, c in scored if c["product"] == product and sc >= 2)
        s.prediction = {**best, "match": MATCH[score], "shoppers": backers or best["shoppers"],
                        "support": mass[product] / sum(mass.values()), "scored": len(candidates),
                        "ms": (time.perf_counter() - started) * 1000, "at": len(s.trail)}
    except Exception as exc:  # a demo: keep the last prediction and say why
        s.error = f"Jev error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Drawing: the trie, pruned to the branches that matter
# ---------------------------------------------------------------------------

COL_W, PAD, LEFT, ASPECT = 34, 20, 70, 3.4


def mark(step: str, x: float, y: float, r: float) -> str:
    kind = step.partition(":")[0]
    i = category_index(step)
    cls = f'class="m{" c" + str(i) if i is not None else " k-" + kind}"'
    if kind == "list":
        return f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r:.1f}" height="{2 * r:.1f}" rx="3" {cls}/>'
    if kind == "product":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" {cls}/>'
    if kind == "bought":
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" {cls}/>'
                f'<path d="M{x - r * .45:.1f},{y:.1f} l{r * .32:.1f},{r * .35:.1f} l{r * .6:.1f},{-r * .7:.1f}" class="tick"/>')
    if kind == "cart":
        return (f'<path d="M{x - r:.1f},{y - r * .6:.1f} h{2 * r:.1f} l{-r * .3:.1f},{r * 1.2:.1f} h{-r * 1.4:.1f} z" {cls}/>')
    if kind == "search":
        return f'<polygon points="{x:.1f},{y - r:.1f} {x + r:.1f},{y:.1f} {x:.1f},{y + r:.1f} {x - r:.1f},{y:.1f}" {cls}/>'
    return f'<polygon points="{x:.1f},{y - r:.1f} {x + r:.1f},{y - r * .1:.1f} {x + r * .7:.1f},{y + r:.1f} ' \
           f'{x - r * .7:.1f},{y + r:.1f} {x - r:.1f},{y - r * .1:.1f}" {cls}/>'  # home


def trie_svg(trail: list[str], route: list[str] | None = None, clean: bool = False, aspect: float = ASPECT,
             stages: dict[int, tuple[str, str]] | None = None, you_art: str = "",
             pick: tuple[str, str, str] | None = None,
             ghost: list[str] | None = None) -> str:
    """Top-down crowd trie: shape = page type, color = category, size = visits,
    branch width = shoppers, % = share of visits through that node that bought.
    Your visit glows gold; the route Jev picked is dashed blue. `clean` is the
    share-card version: only the main branches, no axis or %, and labels: Jev's read at
    each `stages` depth of your path, the product you're on (`you_art`), and the predicted
    purchase at the end of the dotted route (`pick` = label, name, art). `ghost` steps are a
    projected continuation drawn dashed below your node when no recorded route continues it."""
    root, route = crowd.root, route or []
    left = PAD if clean else LEFT
    total = max(1, root["visits"])
    if not root["kids"]:
        return '<p class="empty">No shoppers yet. Press <b>+100 shoppers</b>, or start browsing.</p>'
    trail = trail[:DEPTH]
    ghost = ghost or []
    floor = max(2, total * (0.008 if clean else PRUNE))

    stem = max(3, total * 0.0004)  # a branch's biggest child carries it on down if it has this many visits

    def keep(path: list[str], kid: dict[str, Any]) -> bool:
        return kid["visits"] >= floor or trail[: len(path)] == path or route[: len(path)] == path

    def kids_of(node: dict[str, Any], path: list[str]) -> list[str]:
        if clean and ghost and path == trail:  # your node continues only as the dashed projection
            return []
        kept = [s for s, k in node["kids"].items() if keep([*path, s], k)]
        if path and node["kids"]:  # continue the branch through its biggest child
            biggest = max(node["kids"], key=lambda s: node["kids"][s]["visits"])
            if biggest not in kept and node["kids"][biggest]["visits"] >= stem and len(kept) == 0:
                kept.append(biggest)
        return sorted(kept, key=lambda s: -node["kids"][s]["visits"])

    def count(node: dict[str, Any], path: list[str]) -> int:
        ks = kids_of(node, path)
        return sum(count(node["kids"][s], [*path, s]) for s in ks) if ks else 1

    width = left + PAD + max(0, count(root, []) - 1) * COL_W
    depth = max(1, height_of(root, [], kids_of), len(trail) + len(ghost))
    row_h = max(30 if clean else 40, (width / aspect - 2 * PAD) / depth)
    nodes, edges, overlay, leaves, now = [], [], [], [0], time.time()
    u = width / 1100  # clean-mode label scale: about one screen pixel on the 1200px card
    stages = stages or {}

    def place(node: dict[str, Any], path: list[str]) -> float:
        d = len(path)
        ks = kids_of(node, path)
        if not ks:
            x = left + leaves[0] * COL_W
            leaves[0] += 1
        else:
            xs = [place(node["kids"][s], [*path, s]) for s in ks]
            x = (xs[0] + xs[-1]) / 2
            y1, y2 = PAD + d * row_h, PAD + (d + 1) * row_h
            for s, cx in zip(ks, xs):
                on, asked = trail[: d + 1] == [*path, s], route[: d + 1] == [*path, s]
                w = 0.8 + 1.5 * math.log2(1 + node["next"].get(s, 0) * 100 / total)
                mid = (y1 + y2) / 2
                cls = "edge on" if on else "edge ask" if asked else "edge"
                edges.append(f'<path d="M{x:.1f},{y1:.1f} C{x:.1f},{mid:.1f} {cx:.1f},{mid:.1f} {cx:.1f},{y2:.1f}" '
                             f'class="{cls}" stroke-width="{w:.1f}"/>')
        if path:
            y, r = PAD + d * row_h, min(13, 4 + 2.2 * math.log2(1 + node["visits"] * 100 / total))
            p_buy, done = odds(node)
            on, asked = trail[:d] == path, route[:d] == path
            tip = f"{' → '.join(label(s) for s in path)}\n{node['visits']:,} visits"
            if p_buy is not None:
                tip += f" · {p_buy:.0%} of {done:,} finished visits bought"
            if b := top(node["buys"]):
                tip += f"\nmost bought: {PRODUCTS[b]['name']}"
            ring = (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r + 4:.1f}" class="here"/>' if on else
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r + 4:.1f}" class="asked"/>' if asked else "")
            pct = (f'<text x="{x:.1f}" y="{y + r + 11:.1f}">{p_buy:.0%}</text>'
                   if not clean and p_buy is not None and (node["visits"] >= total * 0.03 or on or asked)
                   and path[-1] != "bought" else "")
            if clean and on and (d in stages or d == len(trail)):
                # Jev's read at this stage, beside the node, clear of the path below it
                side = "end" if x > width * 0.75 else "start"
                tx = x - r - 6 * u if side == "end" else x + r + 6 * u
                text, nudge = stages.get(d, ("", ""))
                if d == len(trail):
                    text = f"you · {text}" if text else "you"
                cls = "tag you" if d == len(trail) else "tag stage"
                # A pill: beside "you", or centered in the gap above an earlier stage's row (where only edges run)
                big = d == len(trail)
                f1, f2 = (15 if big else 12.5) * u, 11.5 * u
                w = max(len(text) * f1 * 0.6, len(nudge) * f2 * 0.58) + 14 * u
                h = (f1 + (f2 + 4 * u if nudge else 0)) + 10 * u
                cy = y if big else y - row_h / 2
                px = (x + r + 8 * u) if side == "start" else (x - r - 8 * u - w)
                if not big:
                    px = x + 7 * u if side == "start" else x - 7 * u - w
                overlay.append(f'<rect x="{px:.1f}" y="{cy - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{6 * u:.1f}" '
                               f'class="pill{" you" if big else ""}"/>')
                tx = px + 7 * u
                line2 = (f'<tspan x="{tx:.1f}" dy="{f2 + 4 * u:.1f}" class="nudge" style="font-size:{f2:.1f}px">'
                         f'{html.escape(nudge)}</tspan>' if nudge else "")
                overlay.append(f'<text x="{tx:.1f}" y="{cy - h / 2 + 5 * u + f1 * 0.85:.1f}" class="{cls} flat" '
                               f'style="font-size:{f1:.1f}px">{html.escape(text)}{line2}</text>')
                if d == len(trail) and you_art:
                    a = 52 * u
                    ax = x - r - 10 * u - a if side == "start" else x + r + 10 * u
                    overlay.append(f'<rect x="{ax:.1f}" y="{y - a / 2:.1f}" width="{a:.1f}" height="{a:.1f}" rx="{8 * u:.1f}" class="artbg"/>'
                                   + you_art.replace("<svg ", f'<svg x="{ax + 4 * u:.1f}" y="{y - a / 2 + 4 * u:.1f}" '
                                                              f'width="{a - 8 * u:.1f}" height="{a - 8 * u:.1f}" ', 1))
            if clean and on and d == len(trail) and ghost:
                gy = y
                for i, g in enumerate(ghost, 1):
                    ny = PAD + (d + i) * row_h
                    overlay.insert(0, f'<path d="M{x:.1f},{gy:.1f} L{x:.1f},{ny:.1f}" class="edge ask" stroke-width="2"/>')
                    overlay.append(f'<g class="ghost">{mark(g, x, ny, 9)}</g>')
                    gy = ny
                if pick:
                    _pick_at(overlay, pick, x, gy + 9, u)
            elif clean and asked and d == len(route) and pick:
                _pick_at(overlay, pick, x, y + r, u)
            fresh = " new" if now - node.get("born", 0) < NEW_SECONDS else ""
            nodes.append(f'<g class="node{fresh}"><title>{html.escape(tip)}</title>{ring}{mark(path[-1], x, y, r)}{pct}</g>')
        else:
            nodes.append(f'<circle cx="{x:.1f}" cy="{PAD:.1f}" r="6" class="root"/>')
        return x

    place(root, [])
    height = PAD * 2 + depth * row_h + (120 * u if clean else 0)  # room for the predicted product under its node
    rows = "" if clean else "".join(
        f'<text class="axis" x="4" y="{PAD + d * row_h + 4:.1f}">{"start" if d == 0 else f"step {d}"}</text>'
        for d in range(depth + 1))
    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="Crowd trie">'
            f'{rows}{"".join(edges)}{"".join(nodes)}{"".join(overlay)}</svg>')


def _pick_at(overlay: list[str], pick: tuple[str, str, str], x: float, y: float, u: float) -> None:
    """The predicted product's art and name, centered under (x, y)."""
    a, ay = 64 * u, y + 8 * u
    overlay.append(f'<rect x="{x - a / 2:.1f}" y="{ay:.1f}" width="{a:.1f}" height="{a:.1f}" rx="{10 * u:.1f}" class="artbg pick"/>'
                   + pick[2].replace("<svg ", f'<svg x="{x - a / 2 + 5 * u:.1f}" y="{ay + 5 * u:.1f}" '
                                              f'width="{a - 10 * u:.1f}" height="{a - 10 * u:.1f}" ', 1)
                   + f'<text x="{x:.1f}" y="{ay + a + 18 * u:.1f}" class="tag pick" text-anchor="middle" '
                     f'style="font-size:{14 * u:.1f}px">{html.escape(pick[0])}<tspan x="{x:.1f}" dy="{17 * u:.1f}" '
                     f'class="tag-sub" style="font-size:{13 * u:.1f}px">{html.escape(pick[1])}</tspan></text>')


def height_of(node: dict[str, Any], path: list[str], kids_of) -> int:
    ks = kids_of(node, path)
    return 1 + max((height_of(node["kids"][s], [*path, s], kids_of) for s in ks), default=0) if path else \
        max((height_of(node["kids"][s], [s], kids_of) for s in ks), default=0)


# ---------------------------------------------------------------------------
# Rendering: JSON → HTML
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    s: "Session"
    node: dict[str, Any]

    @property
    def route(self) -> list[str]:
        a = self.s.asked
        return [*a["from"], *a["routes"][0]["route"]] if a and a["routes"] else []


def kids(node: dict[str, Any], ctx: Ctx) -> str:
    return "".join(render(c, ctx) for c in node.get("children", []))


def pic(p: dict[str, Any], uid: str = "") -> str:
    return art(p["type"], p["hex"], plaid=p["color"] == "Red Plaid", uid=uid or p["id"])


def go(kind: str, arg: str, inner: str, cls: str = "", title: str = "", via: str = "") -> str:
    """A button that takes a step; `via` names the nudge (strip) it sits in."""
    t = f' title="{html.escape(title)}"' if title else ""
    v = f'<input type="hidden" name="via" value="{html.escape(via)}">' if via else ""
    return (f'<form method="post" action="/go"><input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="arg" value="{html.escape(arg)}">{v}<button class="{cls}"{t}>{inner}</button></form>')


def stars(p: dict[str, Any]) -> str:
    return f'<span class="stars">★ {p["rating"]}</span> <span class="muted">({p["reviews"]:,})</span>'


def card(p: dict[str, Any], uid: str = "") -> str:
    return go("product", p["id"], f'<span class="art">{pic(p, uid)}</span><span class="pn">{html.escape(p["name"])}</span>'
                                  f'<span class="pp">${p["price"]:.2f} · {stars(p)}</span>', "pcard", p["name"])


def r_page(n, ctx):
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(n["title"])}</title><style>{CSS}</style></head>'
            f'<body><main>{kids(n, ctx)}</main><script>{JS}</script></body></html>')


def r_topbar(n, ctx):
    s = ctx.s
    steps = "".join(f'<span class="crumb">{html.escape(label(st))}</span>' for st in s.trail[-5:])
    more = f'<span class="muted">… </span>' if len(s.trail) > 5 else ""
    return (f'<header class="top"><div><h1>{html.escape(n["title"])}</h1><p class="sub">{html.escape(n["subtitle"])}</p></div>'
            f'<div class="trail"><span class="muted">Visit {s.visits + 1}</span>{more}{steps or "<span class=muted>not started</span>"}</div></header>')


def r_stats(n, ctx):
    s, node = ctx.s, ctx.node
    total = crowd.root["visits"]
    rate, _ = odds(crowd.root)
    hero = (f'<div class="stat hero"><span class="label">Shopper visits in the trie</span>'
            f'<span class="value">{total:,}</span>'
            f'<span class="note">{f"{rate:.1%} bought" if rate is not None else "no finished visits yet"} · '
            f'choices weighted by real reviews</span></div>')
    if s.mindset is None:
        mind = '<span class="value dim">—</span><span class="note">Jev reads you from step 2</span>'
    else:
        short, desc = MINDSETS[s.mindset]
        badge = '<span class="badge ok">clear</span>' if s.clear else '<span class="badge">forming</span>'
        mind = f'<span class="value">{short} {badge}</span><span class="note">{desc} · Jev, {s.latency_ms:.0f} ms</span>'
    basis, k = backoff(s.trail)
    p_buy, done = odds(basis)
    likely = PRODUCTS.get(top(basis["buys"]) or "")
    if p_buy is None:
        buy = '<span class="value dim">—</span><span class="note">no finished visits yet</span>'
    else:
        what = (f'<span class="mini-art">{pic(likely, "st")}</span> most likely {html.escape(likely["name"])}'
                if likely else f"from {done} finished visits")
        matched = ("all shoppers" if k == 0 else "your exact path" if k == min(len(s.trail), DEPTH)
                   else f"your first {k} step{'s' if k > 1 else ''}")
        buy = (f'<span class="value">{p_buy:.0%} <span class="badge">{matched}</span></span>'
               f'<span class="note likely">{what}</span>')
    pr = s.prediction
    if pr and "bought" not in s.trail:
        q = PRODUCTS[pr["product"]]
        if pr["match"] == "in your cart":
            how = "it's in your cart"
        else:
            how = (f'{pr["support"]:.0%} of the matching evidence · {pr["shoppers"]} shopper'
                   f'{"s" if pr["shoppers"] != 1 else ""} on close routes bought it')
        nxt = (f'<span class="value small"><span class="mini-art">{pic(q, "pr")}</span>{html.escape(q["name"])}</span>'
               f'<span class="note">{how}</span>')
    else:
        nxt = '<span class="value dim">—</span><span class="note">Jev matches you to other shoppers from step 2</span>'
    return (f'<section class="stats">{hero}'
            f'<div class="stat"><span class="label">Jev reads you as</span>{mind}</div>'
            f'<div class="stat"><span class="label">Your odds of buying</span>{buy}</div>'
            f'<div class="stat"><span class="label">Jev predicts you\'ll buy</span>{nxt}</div></section>')


def r_ask(n, ctx):
    a = ctx.s.asked
    value = html.escape(a["question"]) if a else ""
    form = (f'<form method="post" action="/ask" class="askform">'
            f'<input name="question" value="{value}" placeholder="{html.escape(n["placeholder"])}" '
            f'aria-label="Ask what you are shopping for" required><button class="primary">Find my route</button></form>')
    if not a:
        return f'<section class="panel ask">{form}<p class="hint">{html.escape(n["hint"])}</p></section>'
    if a["error"]:
        return f'<section class="panel ask">{form}<p class="hint">{html.escape(a["error"])}</p></section>'
    best = a["routes"][0]
    if best["fit"] == "unrelated":
        return (f'<section class="panel ask">{form}<p class="hint">None of the {a["scored"]} purchase routes from here '
                f'fits that. Start a new visit, or add shoppers.</p></section>')
    p = PRODUCTS[best["product"]]
    path = '<span class="arrow">→</span>'.join(f'<span class="chip">{html.escape(label(st))}</span>' for st in best["route"])
    alts = "; ".join(f'{PRODUCTS[r["product"]]["name"]} ({r["fit"]}, {r["shoppers"]})' for r in a["routes"][1:])
    nxt = best["route"][0]
    take = ""
    if nxt != "bought" and ctx.s.page_kind != "bought" and not a.get("from_start"):
        k, _, arg = nxt.partition(":")
        if k == "product":  # the product shoppers on this route bought most, in that category
            arg = (best["product"] if SLUG[PRODUCTS[best["product"]]["category"]] == arg
                   else BY_CATEGORY[CATEGORY_OF[arg]][0]["id"])
        elif k == "cart":
            arg = ctx.s.page_arg if ctx.s.page_kind == "product" else ""
        if k != "cart" or arg:
            take = go(k, arg, f"Take the next step →", "ghost route-go")
    return (f'<section class="panel ask">{form}<div class="answer">'
            f'<span class="art xs">{pic(p, "ask")}</span>'
            f'<span><b>Shoppers like you bought the {html.escape(p["name"])}</b> <span class="muted">${p["price"]:.2f}</span></span>'
            f'<span class="route">{path}</span>'
            f'<span class="fit {best["fit"]}" title="{best["shoppers"]} bought this way. Runners-up: {html.escape(alts)}">'
            f'{best["fit"]} fit</span>'
            f'<span class="muted small" title="{"No purchase route continues from your exact path, so Jev scored whole journeys." if a.get("from_start") else ""}">'
            f'Jev scored {a["scored"]} {"journeys" if a.get("from_start") else "routes"} · {a["ms"]:.0f} ms</span>'
            f'{take}<form method="post" action="/ask/clear"><button class="ghost" title="Clear">×</button></form></div></section>')


def r_columns(n, ctx):
    return f'<div class="columns">{kids(n, ctx)}</div>'


def r_column(n, ctx):
    return f'<div class="col {n.get("width", "")}">{kids(n, ctx)}</div>'


def r_storefront(n, ctx):
    s = ctx.s
    nav = "".join(go("list", SLUG[c], html.escape(c), "navlink" + (" on" if s.page_kind == "list" and s.page_arg == SLUG[c] else ""))
                  for c in CATEGORIES)
    topics = "".join(go("search", k, html.escape(v[0]), "topic") for k, v in TOPICS.items())
    bar = (f'<div class="shopbar">{go("home", "", f"<b>{html.escape(STORE)}</b>", "brand")}'
           f'<span class="cartn">{go("cart", "", f"Cart ({len(s.cart)})", "ghost") if s.cart else "Cart (0)"}</span>'
           f'<form method="post" action="/leave"><button class="ghost">Leave store</button></form></div>'
           f'<nav class="nav">{nav}</nav><div class="topics"><span class="muted">Search:</span>{topics}</div>')
    return f'<section class="panel shop">{bar}<div class="view">{view(ctx)}</div></section>'


def view(ctx: Ctx) -> str:
    s = ctx.s
    k, arg = s.page_kind, s.page_arg
    if k == "start":
        return ('<div class="hello"><h2>Welcome to the store</h2><p class="muted">Pick a category or a search topic '
                'above. Every click is a step in the trie.</p></div>' + home_tiles())
    if k == "home":
        return '<h2>Shop by category</h2>' + home_tiles()
    if k == "search":
        hits = sorted((p for p in PRODUCTS.values() if TOPICS[arg][1](p)), key=lambda p: -p["reviews"])[:9]
        return f'<h2>{html.escape(TOPICS[arg][0])} <span class="muted">· {len(hits)} shown</span></h2><div class="pgrid">{"".join(card(p) for p in hits)}</div>'
    if k == "list":
        c = CATEGORY_OF[arg]
        return f'<h2>{html.escape(c)} <span class="muted">· {len(BY_CATEGORY[c])} products</span></h2><div class="pgrid">{"".join(card(p) for p in BY_CATEGORY[c][:9])}</div>'
    if k == "product":
        p = PRODUCTS[arg]
        related = [q for q in BY_CATEGORY[p["category"]] if q["id"] != p["id"]][:3]
        return (f'<div class="pdp"><span class="art big">{pic(p, "pdp")}</span><div>'
                f'<p class="muted small">{html.escape(p["category"])} · {html.escape(p["color"])}</p>'
                f'<h2>{html.escape(p["name"])}</h2><p class="price">${p["price"]:.2f} <span class="small">{stars(p)}</span></p>'
                f'<p class="muted">{html.escape(p["blurb"])}</p><div class="row">{go("cart", p["id"], "Add to cart", "primary")}'
                f'{go("list", SLUG[p["category"]], f"Back to {html.escape(p["category"])}", "ghost")}</div></div></div>'
                f'<h3>More in {html.escape(p["category"])}</h3><div class="strip">{"".join(mini(q, "r") for q in related)}</div>')
    if k == "cart":
        items = [PRODUCTS[i] for i in s.cart]
        rows = "".join(f'<li><span class="art xs">{pic(p, "c" + p["id"])}</span>{html.escape(p["name"])}<b>${p["price"]:.2f}</b></li>'
                       for p in items)
        total = sum(p["price"] for p in items)
        return (f'<h2>Your cart</h2><ul class="cart">{rows}</ul><p class="price">Total ${total:.2f}</p>'
                f'<div class="row">{go("bought", "", "Check out", "primary")}{go("home", "", "Keep shopping", "ghost")}</div>')
    if k == "bought":
        return ('<div class="hello"><h2>Order placed ✓</h2><p class="muted">Your visit just joined the trie as a purchase.</p>'
                '<form method="post" action="/leave"><button class="primary">Start a new visit</button></form></div>')
    return ""


def home_tiles() -> str:
    tiles = "".join(go("list", SLUG[c], f'<span class="art">{pic(BY_CATEGORY[c][0], "h" + str(i))}</span>'
                                       f'<span class="pn">{html.escape(c)}</span>', f"ctile k{i}")
                    for i, c in enumerate(CATEGORIES))
    return f'<div class="ctiles">{tiles}</div>'


def r_slot(n, ctx):
    s = ctx.s
    if n["name"] != "modules" or s.mindset is None or s.page_kind in ("bought", "start"):
        return ""
    return '<div class="modules">' + "".join(
        f'<div class="module" data-module="{mid}">{render(MODULES[mid]["node"], ctx)}</div>'
        for mid in MINDSET_STRIPS[s.mindset]) + "</div>"


def mini(p: dict[str, Any], uid: str, via: str = "") -> str:
    return go("product", p["id"], f'<span class="art xs">{pic(p, uid + p["id"])}</span>'
                                  f'<span class="sn">{html.escape(p["name"].split(" ", 1)[-1])}</span>'
                                  f'<span class="muted">${p["price"]:.0f}</span>', "mini", p["name"], via)


def select_of(mid: str) -> str:
    """The products select inside a strip module."""
    return next(c["select"] for c in MODULES[mid]["node"]["children"] if c["type"] == "products")


def r_products(n, ctx):
    s, node = ctx.s, ctx.node
    sel, limit = n["select"], n.get("limit", 3)
    here = PRODUCTS[s.page_arg]["category"] if s.page_kind == "product" else CATEGORY_OF.get(s.page_arg)
    if sel == "compare":
        pool = sorted(BY_CATEGORY[here or CATEGORIES[0]], key=lambda p: (-p["rating"], -p["reviews"]))
    elif sel == "gifts":
        pool = sorted((p for p in PRODUCTS.values() if TOPICS["gifts"][1](p)), key=lambda p: -p["reviews"])
    elif sel == "deals":
        pool = sorted((p for p in PRODUCTS.values() if p["price"] < 30), key=lambda p: -p["reviews"])
    elif sel == "popular":
        pool = sorted((p for p in PRODUCTS.values() if p["category"] != here), key=lambda p: -p["reviews"])
    else:  # path_buys: what visits that started like yours ended up buying (the longest start with any)
        buys = next((b for k in range(min(len(s.trail), DEPTH), -1, -1) if (b := crowd.find(s.trail[:k])["buys"])), {})
        pool = [PRODUCTS[i] for i, _ in sorted(buys.items(), key=lambda kv: -kv[1])]
    pool = [p for p in pool if not (s.page_kind == "product" and p["id"] == s.page_arg)][:limit]
    if not pool:
        return '<span class="muted small">nothing yet</span>'
    return '<div class="strip">' + "".join(mini(p, n["select"], via=n["select"]) for p in pool) + "</div>"


def r_trie(n, ctx):
    legend = "".join(f'<span><i class="sw c{i}"></i>{html.escape(c)}</span>' for i, c in enumerate(CATEGORIES))
    shapes = ('<span><i class="sh home"></i>home</span><span><i class="sh search"></i>search</span>'
              '<span><i class="sh list"></i>listing</span><span><i class="sh product"></i>product</span>'
              '<span><i class="sh cart"></i>cart</span><span><i class="sh bought"></i>bought</span>'
              '<span><i class="lg-here"></i>you</span><span><i class="lg-ask"></i>your route</span><span>% = bought</span>')
    return (f'<section class="panel trie"><div class="bar"><h2>The crowd trie</h2>'
            f'<span class="actions"><form method="post" action="/simulate"><button class="ghost">+100 shoppers</button></form>'
            f'<form method="post" action="/simulate"><input type="hidden" name="n" value="10000">'
            f'<button class="ghost">+10K shoppers</button></form>'
            f'<form method="post" action="/clear" onsubmit="return confirm(&quot;Delete every visit in the trie?&quot;)">'
            f'<button class="ghost">Clear</button></form></span></div>'
            f'<div class="legend">{shapes}{legend}</div>'
            f'<div id="trie">{trie_svg(ctx.s.trail, ctx.route)}</div></section>')


def r_debug(n, ctx):
    s = ctx.s
    return (f'<footer class="debug"><span>{html.escape(s.error) if s.error else html.escape(s.why)}</span>'
            f'<span>Demo store: made-up names and art; real prices, ratings and review counts (Amazon Reviews 2023) · '
            f'<a href="/share">share card</a> · <a href="/users/{s.uid}">user {s.uid}</a></span></footer>')


RENDERERS = {
    "page": r_page, "topbar": r_topbar, "stats": r_stats, "ask": r_ask, "columns": r_columns, "column": r_column,
    "storefront": r_storefront, "slot": r_slot, "products": r_products, "trie": r_trie, "debug": r_debug,
    "card": lambda n, ctx: f'<div class="card">{kids(n, ctx)}</div>',
    "heading": lambda n, ctx: f'<h{n.get("level", 2)}>{html.escape(n["text"])}</h{n.get("level", 2)}>',
}


def render(node: dict[str, Any], ctx: Ctx) -> str:
    return RENDERERS[node["type"]](node, ctx)


CAT_CSS = "\n".join(f".c{i} {{ fill: {HUE_LIGHT[i]}; background: {HUE_LIGHT[i]}; }}" for i in range(len(CATEGORIES)))
CAT_CSS_DARK = "\n".join(f".c{i} {{ fill: {HUE_DARK[i]}; background: {HUE_DARK[i]}; }}" for i in range(len(CATEGORIES)))

CSS = f"""
:root {{ --bg: #0e1013; --panel: #161a1f; --panel-2: #1c2128; --line: #262c34; --fg: #eef1f4; --muted: #8a94a0;
        --accent: #f5c451; --accent-soft: rgba(245, 196, 81, .16); --ok: #4cc38a; --edge: #39414b;
        --route: #6cb6ff; --route-soft: rgba(108, 182, 255, .16); --art: #232a33; }}
{CAT_CSS_DARK}
@media (prefers-color-scheme: light) {{
  :root {{ --bg: #f3f1ec; --panel: #ffffff; --panel-2: #f7f6f2; --line: #e2dfd7; --fg: #17191c; --muted: #6c7178;
          --accent: #c48a00; --accent-soft: rgba(196, 138, 0, .12); --ok: #2e8a57; --edge: #cfcabf;
          --route: #1f6fd1; --route-soft: rgba(31, 111, 209, .12); --art: #efe9dd; }}
  {CAT_CSS} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 13px/1.4 Inter, ui-sans-serif, system-ui, sans-serif;
       -webkit-font-smoothing: antialiased; }}
main {{ max-width: 1280px; margin: 0 auto; padding: 10px 20px 8px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }}
h1 {{ margin: 0; font-size: 21px; letter-spacing: -.02em; }}
h2 {{ margin: 0 0 8px; font-size: 14px; font-weight: 650; }}
h3 {{ margin: 8px 0 5px; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
form {{ margin: 0; }} p {{ margin: 0; }}
button {{ font: inherit; color: var(--fg); cursor: pointer; background: none; border: 0; padding: 0; text-align: inherit; }}
.muted {{ color: var(--muted); font-weight: normal; }} .small {{ font-size: 12px; }}
.top {{ display: flex; align-items: end; justify-content: space-between; gap: 16px; }}
.top > div:first-child {{ min-width: 0; }}
.sub {{ margin-top: 2px; color: var(--muted); }}
.trail {{ display: flex; flex-wrap: wrap; justify-content: end; align-items: center; gap: 4px; font-size: 11px; max-width: 46%; }}
.crumb {{ padding: 2px 8px; border: 1px solid var(--line); border-radius: 99px; background: var(--panel); }}
.crumb + .crumb::before {{ content: ""; }}
.stats {{ display: grid; grid-template-columns: 1.1fr 1fr 1fr 1fr; gap: 10px; }}
.stat {{ display: grid; align-content: start; gap: 1px; padding: 8px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; min-width: 0; }}
.stat .label {{ font-size: 12px; color: var(--muted); }}
.stat .value {{ display: flex; align-items: center; gap: 8px; font-size: 22px; font-weight: 650; letter-spacing: -.02em; min-height: 28px; }}
.stat .value.small {{ font-size: 19px; }}
.stat .value.dim {{ color: var(--muted); }}
.stat.hero .value {{ font-size: 36px; line-height: 1; min-height: 0; color: var(--accent); }}
.stat .note {{ font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.note.likely {{ display: flex; align-items: center; gap: 6px; }}
.mini-art svg {{ width: 22px; height: 22px; display: block; background: var(--art); border-radius: 5px; }}
.badge {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 99px; background: var(--panel-2); color: var(--muted); border: 1px solid var(--line); letter-spacing: 0; }}
.badge.ok {{ color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, transparent); }}
.panel, .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }}
.panel {{ padding: 10px 14px; }}
.primary {{ padding: 7px 14px; border-radius: 8px; background: var(--accent); color: #17191c; font-weight: 650; }}
.ghost {{ padding: 4px 10px; border-radius: 7px; border: 1px solid var(--line); font-size: 12px; color: var(--muted); }}
.ghost:hover {{ color: var(--fg); border-color: var(--muted); }}
.row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
.ask {{ display: grid; gap: 8px; }}
.askform {{ display: flex; gap: 8px; }}
.askform input {{ flex: 1; min-width: 0; font: inherit; font-size: 14px; color: var(--fg); background: var(--panel-2);
                 border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; }}
.askform input:focus {{ outline: 2px solid var(--route); outline-offset: -1px; }}
.hint {{ font-size: 12px; color: var(--muted); }}
.answer {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }}
.answer > form:last-child {{ margin-left: auto; }}
.route {{ display: inline-flex; flex-wrap: wrap; align-items: center; gap: 3px; margin-top: 3px; }}
.arrow {{ color: var(--muted); }}
.chip {{ padding: 1px 8px; border-radius: 99px; font-size: 12px; background: var(--route-soft);
        border: 1px solid color-mix(in srgb, var(--route) 45%, transparent); }}
.fit {{ font-weight: 600; padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line); font-size: 12px; }}
.fit.exactly, .fit.good {{ color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, transparent); }}
.route-go {{ color: var(--route); border-color: color-mix(in srgb, var(--route) 50%, transparent); }}
.columns {{ display: grid; grid-template-columns: 470px minmax(0, 1fr); gap: 8px; align-items: stretch; }}
.col {{ display: grid; gap: 8px; align-content: start; min-width: 0; }}
.col.main {{ grid-template-rows: auto 1fr; }}
.shop {{ display: grid; gap: 6px; }}
.shopbar {{ display: flex; align-items: center; gap: 8px; }}
.shopbar .brand {{ font-size: 15px; letter-spacing: -.01em; margin-right: auto; }}
.cartn {{ font-size: 12px; color: var(--muted); }}
.nav, .topics {{ display: flex; gap: 4px; align-items: center; font-size: 12px; overflow-x: auto; scrollbar-width: none; white-space: nowrap; }}
.nav form, .topics form {{ flex: none; }}
.navlink {{ padding: 2px 7px; border-radius: 6px; background: var(--panel-2); }}
.navlink:hover, .navlink.on {{ background: var(--accent-soft); color: var(--accent); }}
.topic {{ padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line); color: var(--muted); }}
.topic:hover {{ color: var(--fg); border-color: var(--muted); }}
.view {{ border-top: 1px solid var(--line); padding-top: 8px; }}
.art {{ display: block; background: var(--art); border-radius: 8px; overflow: hidden; }}
.art svg {{ display: block; width: 100%; height: auto; }}
.art.xs {{ width: 30px; height: 30px; flex: none; border-radius: 6px; }}
.art.big {{ width: 112px; flex: none; }}
.pgrid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
.pgrid .pcard:nth-child(n + 7) {{ display: none; }}
.pcard {{ display: grid; gap: 3px; width: 100%; }}
.pcard .art {{ aspect-ratio: 4 / 3; display: grid; place-items: center; }}
.pcard .art svg {{ width: 70%; }}
.pcard:hover .art {{ outline: 2px solid var(--accent); }}
.pn {{ font-weight: 600; font-size: 12px; line-height: 1.25; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
.pp {{ font-size: 11px; color: var(--muted); }}
.stars {{ color: var(--accent); }}
.ctiles {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
.ctile {{ display: grid; gap: 4px; width: 100%; }}
.ctile .art {{ aspect-ratio: 1; display: grid; place-items: center; }} .ctile .art svg {{ width: 75%; }}
.ctile:hover .art {{ outline: 2px solid var(--accent); }}
.hello {{ display: grid; gap: 6px; margin-bottom: 10px; }}
.pdp {{ display: flex; gap: 14px; align-items: start; }}
.pdp h2 {{ font-size: 16px; margin: 1px 0 2px; }}
.price {{ font-size: 16px; font-weight: 650; margin: 1px 0 4px; }}
.pgrid.three .pcard .art svg {{ width: 55%; }}
.cart {{ list-style: none; padding: 0; margin: 0 0 8px; display: grid; gap: 6px; }}
.cart li {{ display: flex; align-items: center; gap: 8px; }} .cart li b {{ margin-left: auto; }}
.modules {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
.module .card {{ height: 100%; }}
.module {{ animation: in .25s ease-out; }} @keyframes in {{ from {{ opacity: 0; transform: translateY(4px); }} }}
.card {{ padding: 8px 10px; }} .card h3 {{ margin: 0 0 6px; }}
.strip {{ display: flex; gap: 6px; }}
.mini {{ display: grid; justify-items: center; gap: 1px; width: 70px; font-size: 11px; text-align: center; }}
.mini .art.xs {{ width: 34px; height: 34px; }}
.mini:hover .art {{ outline: 2px solid var(--accent); }}
.sn {{ line-height: 1.15; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }}
.trie {{ display: flex; flex-direction: column; height: 100%; }}
.bar {{ display: flex; align-items: center; gap: 10px; }}
.bar h2 {{ margin: 0; }}
.actions {{ display: flex; gap: 6px; margin-left: auto; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 2px 10px; margin-top: 6px; font-size: 10.5px; color: var(--muted); }}
.legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
.sw {{ width: 9px; height: 9px; border-radius: 2px; display: inline-block; }}
.sh {{ width: 10px; height: 10px; display: inline-block; background: var(--muted); }}
.sh.product {{ border-radius: 50%; }} .sh.list {{ border-radius: 2px; }}
.sh.search {{ transform: rotate(45deg) scale(.8); }}
.sh.home {{ clip-path: polygon(50% 0, 100% 45%, 85% 100%, 15% 100%, 0 45%); }}
.sh.cart {{ clip-path: polygon(0 20%, 100% 20%, 85% 90%, 15% 90%); background: var(--fg); }}
.sh.bought {{ border-radius: 50%; background: var(--ok); }}
.lg-here, .lg-ask {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
.lg-here {{ border: 2px solid var(--accent); }} .lg-ask {{ border: 2px dashed var(--route); }}
#trie {{ flex: 1; display: grid; place-items: center; margin-top: 4px; min-height: 200px; }}
#trie svg {{ display: block; width: 100%; height: auto; }}
.empty {{ color: var(--muted); }}
.edge {{ fill: none; stroke: var(--edge); stroke-linecap: round; }}
.edge.on {{ stroke: var(--accent); filter: drop-shadow(0 0 4px var(--accent)); }}
.edge.ask {{ stroke: var(--route); stroke-dasharray: 5 4; filter: drop-shadow(0 0 3px var(--route)); }}
.root {{ fill: var(--muted); }}
.m.k-home, .m.k-search {{ fill: var(--muted); }} .m.k-cart {{ fill: var(--fg); }} .m.k-bought {{ fill: var(--ok); }}
.tick {{ fill: none; stroke: var(--bg); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.here {{ fill: none; stroke: var(--accent); stroke-width: 2.5; filter: drop-shadow(0 0 5px var(--accent)); }}
.asked {{ fill: none; stroke: var(--route); stroke-width: 2; stroke-dasharray: 3 2; }}
.node text {{ font-size: 9px; fill: var(--muted); text-anchor: middle; }}
.axis {{ font-size: 10px; fill: var(--muted); letter-spacing: .04em; text-transform: uppercase; }}
.node.new > :not(title) {{ animation: pop 1s ease-out 2; transform-box: fill-box; transform-origin: center; }}
@keyframes pop {{ 0% {{ transform: scale(1.7); opacity: .3; }} 100% {{ transform: scale(1); opacity: 1; }} }}
.debug {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 16px; color: var(--muted); font-size: 11px; }}
.debug a {{ color: inherit; }}
@media (max-width: 960px) {{
  .stats {{ grid-template-columns: 1fr 1fr; }}
  .columns {{ grid-template-columns: minmax(0, 1fr); }}
  #trie {{ min-height: 0; overflow-x: auto; place-items: start; }}
  #trie svg {{ width: auto; min-width: 100%; height: 320px; }}
  .ctiles {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
}}
"""

# Redraw the trie every 3 s so other shoppers' clicks appear; skip while the tab is hidden.
JS = """
setInterval(async () => {
  if (document.hidden) return;
  try {
    const r = await fetch('/trie.svg');
    if (r.ok) document.getElementById('trie').innerHTML = await r.text();
  } catch {}
}, 3000);
"""

# ---------------------------------------------------------------------------
# Web app
# ---------------------------------------------------------------------------


@dataclass
class Session:
    trail: list[str] = field(default_factory=list)  # steps this visit
    pages: list[tuple[str, str]] = field(default_factory=list)  # (kind, arg) this visit, for Jev
    page_kind: str = "start"
    page_arg: str = ""
    cart: list[str] = field(default_factory=list)
    mindset: str | None = None
    readings: dict[int, str] = field(default_factory=dict)  # trail length → Jev's mindset read then
    followed: dict[int, str] = field(default_factory=dict)  # trail length → the nudge (strip select) clicked there
    prediction: dict[str, Any] | None = None  # Jev's closest-matching purchase journey
    clear: bool = False
    latency_ms: float = 0.0
    error: str | None = None
    why: str = "start: browse, ask, or add shoppers"
    asked: dict[str, Any] | None = None
    visits: int = 0
    uid: str = field(default_factory=lambda: secrets.token_hex(4))
    last_seen: float = field(default_factory=time.time)

    def ctx(self) -> Ctx:
        return Ctx(self, crowd.find(self.trail) if self.trail else crowd.root)

    def position(self) -> dict[str, Any]:
        node = crowd.find(self.trail) if self.trail else crowd.root
        basis, matched = backoff(self.trail)
        p_buy, done = odds(basis)
        likely = top(basis["buys"])
        return {
            "user": self.uid, "visit": self.visits + 1, "steps": [label(s) for s in self.trail],
            "page": {"kind": self.page_kind, "arg": self.page_arg}, "cart": [PRODUCTS[i]["name"] for i in self.cart],
            "mindset": self.mindset, "mindset_clear": self.clear,
            "odds_of_buying": round(p_buy, 3) if p_buy is not None else None, "based_on_finished_visits": done,
            "odds_from_first_steps": matched,
            "likely_purchase": PRODUCTS[likely]["name"] if likely else None,
            "likely_next_step": label(top(node["next"])) if node["next"] else None,
            "jev_prediction": self.prediction and {
                "product": PRODUCTS[self.prediction["product"]]["name"], "match": self.prediction["match"],
                "route": [label(st) for st in self.prediction["route"]], "shoppers": self.prediction["shoppers"],
                "steps_shared_with_you": self.prediction["shared"],
                "support": round(self.prediction["support"], 3)},
            "asked": self.asked and {"question": self.asked["question"],
                                     "routes": [{"route": [label(s) for s in r["route"]], "fit": r["fit"],
                                                 "shoppers": r["shoppers"], "product": PRODUCTS[r["product"]]["name"]}
                                                for r in self.asked["routes"]]},
            "last_active": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.last_seen)),
        }


sessions: dict[str, Session] = {}
app = FastAPI()


def session_of(request: Request) -> tuple[str, Session]:
    sid = getattr(request.state, "sid", None) or request.cookies.get("sid")
    if sid not in sessions:
        sid = uuid.uuid4().hex
        sessions[sid] = Session()
    request.state.sid = sid
    sessions[sid].last_seen = time.time()
    return sid, sessions[sid]


def visit_steps(s: Session) -> list[str]:
    steps = []
    for kind, arg in s.pages[-12:]:
        if kind == "product":
            steps.append(f"viewed {describe(PRODUCTS[arg])}")
        elif kind == "cart" and arg:
            steps.append(f"added {PRODUCTS[arg]['name']} to cart")
        else:
            steps.append(f"opened {label(step_of(kind, arg))}")
    return steps


async def read_mindset(s: Session) -> None:
    started = time.perf_counter()
    try:
        read = await Mindset.adecide({
            "context": f"A shopper browsing {STORE}, an online outdoor and home goods store.",
            "steps_this_visit": visit_steps(s),
            "cart": [describe(PRODUCTS[i]) for i in s.cart],
            "crowd_from_here": crowd_view(s.trail),  # how other shoppers at this point behaved
        })
        s.clear = read.clear or read.mindset == s.mindset  # or the same reading twice running
        s.mindset = read.mindset
        s.readings[len(s.trail)] = read.mindset
        s.error = None
    except Exception as exc:  # a demo: keep the last reading and say why
        s.error = f"Jev error: {type(exc).__name__}: {exc}"
    s.latency_ms = (time.perf_counter() - started) * 1000
    if s.mindset:
        s.why = (f"Jev: mindset={s.mindset} ({'clear' if s.clear else 'forming'}) "
                 f"→ strips: {', '.join(MINDSET_STRIPS[s.mindset])}")


def redirect(sid: str) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    sid, s = session_of(request)
    response = HTMLResponse(render(PAGE, s.ctx()))
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# The share card: one 1200×627 image for LinkedIn, at /share
# ---------------------------------------------------------------------------

SHARE = json.loads((ROOT / "share.json").read_text())


def r_share(n, ctx):
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=1200">'
            f'<title>{html.escape(n["title"])}</title><style>{SHARE_CSS}</style></head>'
            f'<body><div class="card">{kids(n, ctx)}</div></body></html>')


def predicted_product(s: "Session") -> str | None:
    """What Jev's read of the shopper points to, within the category they're in."""
    here = PRODUCTS.get(s.page_arg) if s.page_kind == "product" else None
    cat = here["category"] if here else CATEGORY_OF.get(s.page_arg) if s.page_kind == "list" else None
    if s.mindset == "decisive" and here:
        return here["id"]
    # anything but "decisive" points somewhere new: never the product already on screen
    pool = [p for p in (BY_CATEGORY[cat] if cat else PRODUCTS.values()) if not here or p["id"] != here["id"]]
    line = next((g for g in ("Women's", "Men's") if here and f" {g} " in here["name"]), None)
    if line:  # stay with the line they're shopping (men's or women's) when there's a choice
        pool = [p for p in pool if f" {line} " in p["name"] or ("Men's" not in p["name"] and "Women's" not in p["name"])] or pool
    if s.mindset == "gift":
        same_line = lambda p: not line or f" {line} " in p["name"] or ("Men's" not in p["name"] and "Women's" not in p["name"])
        gifts = [p for p in pool if TOPICS["gifts"][1](p)] or [p for p in PRODUCTS.values()
                                                                  if TOPICS["gifts"][1](p) and p is not here and same_line(p)]
        return max(gifts, key=lambda p: p["reviews"])["id"]
    if s.mindset == "deal":
        return min((p for p in pool if p["reviews"] >= 500), key=lambda p: p["price"], default=pool[0])["id"]
    if s.mindset == "comparing":
        return max(pool, key=lambda p: (p["rating"], p["reviews"]))["id"]
    buys = next((b for k in range(min(len(s.trail), DEPTH), -1, -1) if (b := crowd.find(s.trail[:k])["buys"])), {})
    in_cat = {p: n for p, n in buys.items() if (not cat or PRODUCTS[p]["category"] == cat) and p != s.page_arg}
    return top(in_cat) or top(buys) or (pool[0]["id"] if pool else None)


def ghost_steps(s: "Session", product: str) -> list[str]:
    """The steps from this page to buying `product`."""
    k = s.page_kind
    if k == "cart":
        return ["bought"]
    if k == "product":
        return ["cart", "bought"] if s.page_arg == product else [step_of("product", product), "cart", "bought"]
    return [step_of("product", product), "cart", "bought"]


def share_prediction(s: "Session") -> tuple[list[str], str | None, str]:
    """The dotted continuation for the share card, as a full route from the start: the route
    Jev picked for your question if it continues from where you are, else the crowd's
    likeliest way from here to a purchase."""
    trail = s.trail[:DEPTH] if "bought" not in s.trail else []
    a = s.asked
    if a and a["routes"] and a["routes"][0]["fit"] != "unrelated" and not a.get("from_start") and a["from"] == trail:
        return [*trail, *a["routes"][0]["route"]], a["routes"][0]["product"], "question"
    steps, product = continuation(trail)
    return ([*trail, *steps], product, "crowd") if steps else ([], None, "none")


def r_share_trie(n, ctx):
    s = ctx.s
    route, pid, source = share_prediction(s)
    ghost: list[str] = []
    if source != "question" and s.trail and len(s.trail) < DEPTH and "bought" not in s.trail:
        pr = s.prediction
        if pr and pr["shared"] == len(s.trail):  # the matched journey continues from here: draw it
            route, pid = pr["route"], pr["product"]
        else:  # a partial match (or none): project from here to the predicted product
            route, pid = [], pr["product"] if pr else predicted_product(s)
            ghost = ghost_steps(s, pid) if pid else []
    def nudge(d: int, m: str) -> str:
        shown = MINDSET_STRIPS[m]
        took = s.followed.get(d)
        if took:  # name the strip they actually clicked
            mid = next(x for x in shown if select_of(x) == took)
            return f"nudge: {MODULES[mid]['title']} ✓ taken"
        return f"nudge: {MODULES[shown[0]]['title']}"

    stages, last = {}, None
    for d, m in sorted(s.readings.items()):  # a stage gets a label when Jev's read changes or a nudge was taken
        if d <= DEPTH and (m != last or d in s.followed):
            stages[d] = (f"Jev: {MINDSETS[m][0].lower()}", nudge(d, m))
        last = m
    if s.mindset and len(s.trail) <= DEPTH:
        stages[len(s.trail)] = (MINDSETS[s.mindset][0].lower(), nudge(len(s.trail), s.mindset))
    here = PRODUCTS.get(s.page_arg) if s.page_kind == "product" else None
    pick = ("Jev predicts", PRODUCTS[pid]["name"], pic(PRODUCTS[pid], "pick")) if pid else None
    svg = trie_svg(s.trail, route, clean=True, aspect=1.95, stages=stages,
                   you_art=pic(here, "here") if here else "", pick=pick, ghost=ghost)
    return f'<figure>{svg}</figure>'


RENDERERS.update({"share": r_share, "share_trie": r_share_trie})

SHARE_CAT_CSS = "\n".join(f".c{i} {{ fill: {HUE_DARK[i]}; }}" for i in range(len(CATEGORIES)))
SHARE_CSS = f"""
:root {{ --bg: #0b0d10; --fg: #f2f4f6; --muted: #8d97a3; --line: #232a33; --accent: #f5c451; --route: #6cb6ff;
        --ok: #4cc38a; --edge: #2c333c; --panel: rgba(255, 255, 255, .04); --art: #1e252e; }}
{SHARE_CAT_CSS}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); }}
body {{ min-height: 100vh; display: grid; place-items: center; color: var(--fg);
       font: 15px/1.4 Inter, ui-sans-serif, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }}
.card {{ position: relative; width: 1200px; height: 627px; overflow: hidden; padding: 28px 40px;
        display: grid;
        background: radial-gradient(900px 500px at 78% 40%, rgba(245, 196, 81, .10), transparent 60%),
                    radial-gradient(600px 400px at 0% 100%, rgba(108, 182, 255, .08), transparent 60%), var(--bg); }}
figure {{ margin: 0; display: grid; place-items: center; min-height: 0; }}
figure svg {{ width: 100%; height: 100%; min-height: 0; }}
.edge {{ fill: none; stroke: var(--edge); stroke-linecap: round; }}
.edge.on {{ stroke: var(--accent); stroke-width: 4 !important; filter: drop-shadow(0 0 6px var(--accent)); }}
.edge.ask {{ stroke: var(--route); stroke-width: 3.5 !important; stroke-dasharray: 7 5; filter: drop-shadow(0 0 5px var(--route)); }}
.root {{ fill: var(--fg); }}
.m.k-home, .m.k-search {{ fill: #66717d; }} .m.k-cart {{ fill: var(--fg); }} .m.k-bought {{ fill: var(--ok); }}
.tick {{ fill: none; stroke: var(--bg); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.here {{ fill: none; stroke: var(--accent); stroke-width: 3; filter: drop-shadow(0 0 6px var(--accent)); }}
.asked {{ fill: none; stroke: var(--route); stroke-width: 2.5; stroke-dasharray: 3 2; }}
.tag {{ font-size: 14px; font-weight: 700; letter-spacing: .02em;
       paint-order: stroke; stroke: var(--bg); stroke-width: 5px; stroke-linejoin: round; }}
.tag.you {{ fill: var(--accent); }} .tag.pick {{ fill: var(--route); }}
.tag-sub {{ font-weight: 500; fill: var(--fg); }}
.tag.stage {{ fill: var(--muted); font-weight: 600; }}
.nudge {{ fill: var(--route); font-weight: 600; }}
.pill {{ fill: rgba(11, 13, 16, .88); stroke: var(--line); stroke-width: 1; }}
.pill.you {{ stroke: color-mix(in srgb, var(--accent) 55%, transparent); }}
.tag.flat {{ stroke: none; }}
.artbg {{ fill: var(--art); stroke: var(--accent); stroke-width: 1.5; }}
.artbg.pick {{ stroke: var(--route); stroke-dasharray: 4 3; }}
.ghost .m {{ fill: var(--bg) !important; stroke: var(--route); stroke-width: 2; stroke-dasharray: 3 2; }}
.ghost .tick {{ stroke: var(--route); }}
"""


@app.get("/share", response_class=HTMLResponse)
def share(request: Request):
    """The LinkedIn card: 1200×627, screenshot it as is."""
    sid, s = session_of(request)
    response = HTMLResponse(render(SHARE, s.ctx()))
    response.set_cookie("sid", sid, httponly=True, samesite="lax")
    return response


@app.get("/trie.svg")
def trie(request: Request):
    _, s = session_of(request)
    return Response(trie_svg(s.trail, s.ctx().route), media_type="image/svg+xml")


@app.post("/go")
async def go_route(request: Request, kind: str = Form(), arg: str = Form(""), via: str = Form("")):
    """Take one step: a page view, adding to cart, or checking out."""
    sid, s = session_of(request)
    valid = ((kind == "home" and not arg) or (kind == "search" and arg in TOPICS) or (kind == "list" and arg in CATEGORY_OF)
             or (kind == "product" and arg in PRODUCTS) or (kind == "cart" and (not arg or arg in PRODUCTS))
             or (kind == "bought" and s.cart))
    if not valid or s.page_kind == "bought":
        return redirect(sid)
    if kind == "cart" and arg:
        s.cart.append(arg)
    if via and s.mindset and via in {select_of(m) for m in MINDSET_STRIPS[s.mindset]}:
        s.followed[len(s.trail)] = via  # took a nudge Jev's read put on this page
    s.pages.append((kind, arg))
    s.trail.append(step_of(kind, arg if kind == "product" else "" if kind in ("cart", "bought", "home") else arg))
    crowd.step(s.trail, arg if kind == "product" else None)
    s.page_kind, s.page_arg = kind, arg
    if kind == "bought":
        crowd.finish(s.trail, "bought", s.cart)
        s.why = "visit finished as a purchase"
    elif len(s.trail) >= 2:
        await asyncio.gather(read_mindset(s), predict_match(s))  # both Jev calls at once
    crowd.save()
    return redirect(sid)


@app.post("/leave")
def leave(request: Request):
    """End this visit (as "left" unless it was a purchase) and start a new one."""
    sid, s = session_of(request)
    if s.trail and s.page_kind != "bought":
        crowd.finish(s.trail, "left", [])
        crowd.save()
    sessions[sid] = Session(uid=s.uid, visits=s.visits + 1)
    return redirect(sid)


@app.post("/ask")
async def ask_route(request: Request, question: str = Form()):
    sid, s = session_of(request)
    if question.strip():
        s.asked = await ask(s.trail, question.strip()[:200])
    return redirect(sid)


@app.post("/ask/clear")
def ask_clear(request: Request):
    sid, s = session_of(request)
    s.asked = None
    return redirect(sid)


@app.post("/simulate")
def simulate_route(request: Request, n: int = Form(100)):
    sid, _ = session_of(request)
    simulate(n if n in (100, 10_000) else 100)
    return redirect(sid)


@app.post("/clear")
def clear(request: Request):
    """Empty the trie, on disk and in memory, and restart everyone's visit."""
    global crowd
    crowd.path.unlink(missing_ok=True)
    crowd = CrowdTrie(crowd.path)
    sessions.clear()
    sid, _ = session_of(request)
    return redirect(sid)


@app.get("/users")
def users():
    return JSONResponse([s.position() for s in sorted(sessions.values(), key=lambda s: -s.last_seen)])


@app.get("/users/{uid}")
def user(uid: str):
    s = next((s for s in sessions.values() if s.uid == uid), None)
    if s is None:
        return JSONResponse({"error": f"no user {uid}"}, status_code=404)
    return JSONResponse(s.position())
