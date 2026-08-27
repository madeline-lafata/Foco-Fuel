"""
FoCo Fuel — rules layer (pure Python, no network, no AI).

This module OWNS SAFETY. Every item the AI ever sees has already passed through
here: dietary filtering happens in `passes_prefs`, and only surviving items are
put on the shortlist. This is also the fallback plate builder used whenever the
AI path is unavailable or returns nothing usable.

Ported from the browser's proven rules engine so behavior matches the app.
"""

import re

FOCO_ID = "alias05"

# --- keyword vocabularies (whole-word matched, see `_name_has`) --------------
# Toppings/condiments/garnishes and desserts -> "other" (never a main role).
_OTHER_WORDS = [
    "sauce", "dressing", "salsa", "sour cream", "syrup", "butter", "ketchup",
    "mayo", "mustard", "cream cheese", "jam", "jelly", "honey", "dip", "gravy",
    "glaze", "aioli", "vinaigrette", "pesto", "hummus", "spread", "topping",
    "sprinkle", "whipped", "bits", "crumble", "flakes", "shredded", "grated",
    "diced onion", "relish", "chutney", "condiment",
    "cookie", "cake", "pie", "brownie", "donut", "doughnut", "pastry", "tart",
    "cobbler", "pudding", "cheesecake", "cupcake", "frosting", "icing",
    "choc chip", "chocolate chip", "ice cream", "crisp", "streusel", "turnover",
    "danish", "scone", "shortcake", "parfait", "smoothie", "milkshake",
]
# Protein foods -> "protein", and this WINS over any carb/veg word in the same
# name ("Chicken Salad", "Turkey Tips with Peppers" are proteins, not veg).
# Checked after _OTHER_WORDS so condiments/desserts ("Bacon Bits", "Bacon Cream
# Cheese") stay "other". Deliberately excludes bare "burger"/"hot dog" so
# "Burger Bun"/"Hot Dog Bun" stay carbs, and pizza-topping-only words so meat
# pizzas stay carbs.
_PROTEIN_WORDS = [
    "chicken", "turkey", "beef", "steak", "veal", "lamb", "pork", "ham",
    "bacon", "sausage", "salami", "capicola", "prosciutto", "chorizo",
    "salmon", "tuna", "cod", "tilapia", "haddock", "fish", "shrimp", "crab",
    "lobster", "scallop", "clam", "egg", "tofu", "tempeh", "seitan",
    "meatball", "gyro", "brisket", "carnitas", "hamburger", "cheeseburger",
    "falafel",
]
# Strong grain/bread/pasta bases. When one of these appears WITH a protein word,
# the dish is carb-anchored with protein mixed in ("Penne with Shrimp", "Tuna
# Pita", "Chicken Wrap") -> tag carb, not protein.
_STRONG_CARB_WORDS = [
    "pasta", "penne", "spaghetti", "pita", "sandwich", "wrap", "burrito", "bowl",
]
# Bread/grain/starch -> "carb". Checked BEFORE veg words (a "Spinach Wrap" is a
# tortilla, not a vegetable).
_CARB_WORDS = [
    "wrap", "tortilla", "roll", "bun", "bread", "bagel", "pita", "naan", "rice",
    "pasta", "noodle", "penne", "spaghetti", "macaroni", "potato", "fries",
    "waffle", "pancake", "oatmeal", "cereal", "muffin", "biscuit", "couscous",
    "quinoa", "grits", "polenta", "dumpling", "rigatoni", "ziti", "fettuccine",
    "linguine", "lasagna", "gnocchi", "orzo", "ravioli", "tortellini",
    "farfalle", "rotini", "ramen", "udon", "risotto", "pilaf", "mac", "hoagie",
    "sub", "toast", "sandwich", "burrito", "bowl",
]
# Vegetables -> "veg". (Sweet fruits live in _FRUIT_WORDS below and tag as carb.)
_VEG_WORDS = [
    "broccoli", "carrot", "spinach", "kale", "lettuce", "salad", "greens",
    "tomato", "cucumber", "pepper", "mushroom", "zucchini", "squash",
    "green bean", "asparagus", "cauliflower", "brussels", "cabbage", "beet",
    "corn", "veggie", "vegetable", "edamame", "pea", "kernel", "celery",
    "onion", "radish", "artichoke", "eggplant", "okra", "leek", "bok choy",
]
# Fruit -> "carb". Nutritionally fruit is a sugar/carbohydrate source, so it
# must NOT satisfy the "include a vegetable" rule. This is a category, not a
# banana special-case: add fruits here and they're all handled the same way.
_FRUIT_WORDS = [
    "fruit", "apple", "banana", "orange", "berry", "strawberry", "blueberry",
    "raspberry", "blackberry", "cranberry", "melon", "watermelon", "cantaloupe",
    "honeydew", "grape", "pineapple", "mango", "peach", "pear", "plum", "kiwi",
    "cherry", "apricot", "nectarine", "clementine", "mandarin", "pomegranate",
    "fig", "guava", "papaya", "raisin",
]

# Which roles make up the fallback plate for each load, in order. High loads
# lead with a substantial carb to refuel; rest drops the carb.
PLATE_PLAN = {
    "game day": ["carb", "protein", "veg", "other"],
    "hard":     ["carb", "protein", "veg"],
    "moderate": ["protein", "carb", "veg"],
    "easy":     ["protein", "carb", "veg"],
    "rest":     ["protein", "veg"],
}


# --- nutrition helpers ------------------------------------------------------
def num_of(item, nid):
    """Numeric value of a nutrient, or None when blank/zero (= unknown, never 0)."""
    for n in item.get("nutrients") or []:
        if n.get("id") == nid:
            v = n.get("value")
            if v in (None, "") or float(v) == 0:
                return None
            return float(v)
    return None


def _rank(item, nid):
    """Sort key: unknown nutrition ranks LAST (never treated as 0)."""
    v = num_of(item, nid)
    return -1 if v is None else v


def macros_of(item):
    """Compact macro dict for the shortlist / rendering. None = unknown."""
    return {
        "calories": num_of(item, "calories"),
        "protein":  num_of(item, "protein"),
        "carbs":    num_of(item, "totalCarbohydrates"),
        "fat":      num_of(item, "totalFat"),
        "fiber":    num_of(item, "dietaryFiber"),
    }


# --- item helpers -----------------------------------------------------------
def item_id(item):
    """Short, stable, unique id for an item (the menu's own id)."""
    return str(item.get("id"))


def is_header(item):
    """ALL-CAPS rows ('ENTREES', 'SIDES') are station headers, not food."""
    name = (item.get("itemName") or "").strip()
    return len(name) > 0 and name == name.upper()


def _name_has(name, words):
    """True if any keyword appears as a whole word (singular or common plural:
    +s, +es, or y->ies, so 'peaches'/'berries'/'tomatoes' match their singular).
    """
    for w in words:
        e = re.escape(w)
        alts = [e, e + "s", e + "es"]
        if w.endswith("y"):
            alts.append(re.escape(w[:-1]) + "ies")
        if re.search(r"\b(?:" + "|".join(alts) + r")\b", name):
            return True
    return False


def served_at(item, date, meal):
    """Is this item served on this exact date for this meal period?"""
    for da in item.get("datesAvailable") or []:
        if da.get("date") != date:
            continue
        for m in da.get("menus") or []:
            if m.get("mealPeriod") == meal:
                return True
    return False


def role_of(item):
    """Tag an item protein / carb / veg / other from names + nutrition."""
    name = (item.get("itemName") or "").lower()
    if _name_has(name, _OTHER_WORDS):
        return "other"
    # Protein name wins over veg always. But a protein sitting on a strong
    # grain/bread/pasta base ("Penne with Shrimp", "Tuna Pita") is a
    # carb-anchored dish -> tag carb; otherwise it's a protein.
    if _name_has(name, _PROTEIN_WORDS):
        if _name_has(name, _STRONG_CARB_WORDS):
            return "carb"
        return "protein"
    # Bread/grain/starch AND fruit both tag as carb (fruit is a sugar/carb
    # source, so it can't stand in for a vegetable). Checked before veg words.
    if _name_has(name, _CARB_WORDS) or _name_has(name, _FRUIT_WORDS):
        return "carb"
    if _name_has(name, _VEG_WORDS):
        return "veg"
    p, c = num_of(item, "protein"), num_of(item, "totalCarbohydrates")
    if p is not None and p >= 12:
        return "protein"
    if c is not None and c >= 20:
        return "carb"
    if p is not None and p >= 7:
        return "protein"
    if c is not None and c >= 12:
        return "carb"
    return "other"


# --- SAFETY: dietary hard filter (runs in Python, never in the model) -------
def passes_prefs(item, prefs):
    """Drop items that violate the user's saved dietary preferences."""
    labels = [p.get("label") for p in (item.get("meetsPreferences") or [])]
    allergens = [a.get("label") for a in (item.get("containsAllergens") or [])]
    # Vegetarian filter also accepts Vegan (vegan is a stricter vegetarian).
    if prefs.get("vegetarian") and not ("Vegetarian" in labels or "Vegan" in labels):
        return False
    if prefs.get("glutenFree") and "Gluten-free" not in labels:
        return False
    for a in prefs.get("avoid") or []:
        if a in allergens:
            return False
    return True


def safe_items(menu_items, date, meal, prefs):
    """Items for this meal/date that are real food AND safe for this user."""
    out = []
    for it in menu_items:
        if is_header(it):
            continue
        if not served_at(it, date, meal):
            continue
        if not passes_prefs(it, prefs):
            continue
        out.append(it)
    return out


# --- shared ranking ---------------------------------------------------------
def ranked_pools(items):
    """Bucket items by role and sort each best-first, with sanity caps.

    Shared by the shortlist builder and the fallback composer so the AI sees the
    same "best candidates" the rules would pick.
    """
    pools = {"protein": [], "carb": [], "veg": [], "other": []}
    for it in items:
        pools[role_of(it)].append(it)

    # Keep 900+ kcal bulk trays out of the protein/other buckets.
    def plate_sized(pool):
        f = [it for it in pool if (num_of(it, "calories") is None or num_of(it, "calories") <= 900)]
        return f or pool
    pools["protein"] = plate_sized(pools["protein"])
    pools["other"] = plate_sized(pools["other"])

    # Keep very sugary dishes out of the veg bucket.
    real_veg = [it for it in pools["veg"] if (num_of(it, "totalSugars") is None or num_of(it, "totalSugars") <= 18)]
    if real_veg:
        pools["veg"] = real_veg

    pools["protein"].sort(key=lambda it: _rank(it, "protein"), reverse=True)
    pools["carb"].sort(key=lambda it: _rank(it, "totalCarbohydrates"), reverse=True)
    pools["veg"].sort(key=lambda it: _rank(it, "dietaryFiber"), reverse=True)
    pools["other"].sort(key=lambda it: _rank(it, "protein"), reverse=True)
    return pools


# Per-role caps so the AI gets a tight, high-quality shortlist (a 300-item
# prompt is slow and expensive; the best dozen or so per role is plenty).
SHORTLIST_CAPS = {"protein": 15, "carb": 15, "veg": 15, "other": 10}


# --- shortlist (what the AI is allowed to see) ------------------------------
def build_shortlist(items, caps=SHORTLIST_CAPS):
    """Compact, id-keyed view of the BEST safe items: id, name, role, macros.

    Capped per role (pass caps=None for no cap). This is the only thing the AI
    ever sees — every item here has already passed the safety filter.
    """
    pools = ranked_pools(items)
    shortlist = []
    for role in ("protein", "carb", "veg", "other"):
        picked = pools[role][: caps[role]] if caps else pools[role]
        for it in picked:
            shortlist.append({
                "id": item_id(it),
                "name": it.get("itemName"),
                "role": role,
                "macros": macros_of(it),
            })
    return shortlist


# --- fallback plate (rules only) --------------------------------------------
def portion_cue(demand, carb_name=None):
    """A short, QUALITATIVE portion nudge for a plate.

    The real dining-hall lever is how much you scoop of the same item, so this
    scales *carb emphasis* to the training load while protein stays steady.
    Strictly qualitative — never grams, calories, or any numeric target, and
    always fueling framing, never dieting.
    """
    carb = f"the {carb_name}" if carb_name else "your carbs"
    if demand in ("hard", "game day"):
        return (f"Go big on {carb} — you've earned it today. "
                f"Keep your usual portion of protein.")
    if demand == "rest":
        return (f"A normal scoop of {carb} is plenty today. "
                f"Keep your usual portion of protein.")
    # moderate / easy
    return (f"A solid, satisfying serving of {carb} will fuel you well. "
            f"Keep your usual portion of protein.")


def _plate_note(demand):
    return {
        "game day": "A fuller plate to fuel game day.",
        "hard": "A fuller plate to refuel after hard training.",
        "moderate": "A balanced plate for a moderate day.",
        "easy": "A balanced, easygoing plate for a lighter day.",
        "rest": "A lighter, protein-and-veg-forward plate for a rest day.",
    }.get(demand, "A balanced plate for today.")


def compose_rules_plate(items, demand):
    """The rules-only fallback: one sensible plate as [{items:[id], why}].

    Same logic as the browser engine: categorize, rank within role (with sanity
    caps so bulk trays / sugary dishes don't win), fill the demand's plan.
    """
    pools = ranked_pools(items)
    plan = PLATE_PLAN.get(demand, PLATE_PLAN["moderate"])
    chosen, used = [], set()
    for role in plan:
        for it in pools[role]:
            if item_id(it) not in used:
                chosen.append(item_id(it))
                used.add(item_id(it))
                break

    if not chosen:
        return []
    return [{"items": chosen, "why": _plate_note(demand)}]
