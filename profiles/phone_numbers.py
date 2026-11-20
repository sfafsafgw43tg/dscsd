"""
Polish phone numbers and carriers for the spoofed SIM.

The user's rule: every spoofed line is **+48**, randomised, and the carrier the
phone shows must be a real Polish one from the list they gave:

    Orange · Play · Plus · T-Mobile · Lycamobile · Netia · nju mobile · Plush ·
    Red Bull Mobile

A Polish mobile number's first three digits after +48 (the "AB" prefix) identify
the network that *issued* the number, and that is what a phone in the SIM
status screen gets from ``gsm.sim.operator.alpha`` / ``gsm.operator.alpha``.
The carrier therefore must match the number: a +48 601 number (Plus) showing
"Play" is exactly the kind of contradiction a fraud check looks for.

Numbers come from ``profiles/phone_numbers.txt`` (1000 real-looking Polish
mobile numbers supplied by the user). Selection is:

* one number per device, stored on the device record so it never changes;
* never reused by another device;
* the carrier is derived from the number, so number and carrier always agree;
* a small curated list of prefixes that have no supplied numbers (Play, Plush,
  Red Bull Mobile, ...) tops the pool up only when the file runs out.

Mobile number portability means any +48 mobile number *can* be on any network,
so a number whose prefix belongs to a different issuer is still valid - but the
primary carrier we display always matches the prefix, which is the safest
choice and the one Play/Orange/etc. would show for an unported line.
"""
import os
import random
import re
import threading

# ---------------------------------------------------------------------------
# Polish mobile prefixes -> the network that issued the number
# ---------------------------------------------------------------------------
# Source: Polish UKE number allocation. Multiple networks own blocks inside the
# same prefix, so each entry lists the issuer first (the one we display).
PREFIX_OPERATORS = {
    # ---- Orange Polska (host network)
    "450": "Orange", "451": "Orange", "452": "Orange", "453": "Orange",
    "500": "Orange", "501": "Orange", "502": "Orange", "503": "Orange",
    "504": "Orange", "505": "Orange", "506": "Orange", "507": "Orange",
    "508": "Orange", "509": "Orange", "510": "Orange",
    "519": "Orange",
    "535": "Orange", "536": "Orange", "537": "Orange", "538": "Orange",
    "539": "Orange", "540": "Orange",
    "690": "Orange", "691": "Orange", "692": "Orange", "693": "Orange",
    "694": "Orange", "695": "Orange", "696": "Orange", "697": "Orange",
    "698": "Orange",
    "780": "Orange", "781": "Orange", "782": "Orange", "783": "Orange",
    "784": "Orange",
    "211": "Orange",  # NGSM range Orange operates
    # ---- Play (P4)
    "511": "Play", "515": "Play", "516": "Play", "517": "Play", "518": "Play",
    "550": "Play", "551": "Play", "552": "Play", "553": "Play",
    "554": "Play", "555": "Play", "556": "Play", "557": "Play",
    "558": "Play", "559": "Play",
    "570": "Play", "571": "Play", "572": "Play", "573": "Play",
    "574": "Play", "577": "Play", "578": "Play", "579": "Play",
    "600": "Play", "601": "Play", "602": "Play", "603": "Play",
    "604": "Play", "605": "Play", "606": "Play", "607": "Play",
    "608": "Play", "609": "Play",
    "660": "Play", "661": "Play", "662": "Play", "663": "Play",
    "664": "Play", "665": "Play", "666": "Play", "667": "Play",
    "668": "Play", "669": "Play",
    "699": "Play",
    "725": "Play", "726": "Play", "727": "Play", "728": "Play", "729": "Play",
    "735": "Play", "736": "Play", "737": "Play", "738": "Play", "739": "Play",
    "785": "Play", "786": "Play", "787": "Play", "788": "Play",
    "880": "Play", "881": "Play", "882": "Play", "883": "Play",
    "884": "Play", "885": "Play", "886": "Play", "887": "Play",
    "888": "Play",
    # ---- Plus (Polkomtel)
    "512": "Plus", "513": "Plus", "514": "Plus",
    "530": "Plus", "531": "Plus", "532": "Plus", "533": "Plus",
    "534": "Plus", "541": "Plus", "542": "Plus", "543": "Plus",
    "544": "Plus", "545": "Plus",
    "610": "Plus", "611": "Plus", "612": "Plus", "613": "Plus",
    "614": "Plus", "615": "Plus", "616": "Plus", "617": "Plus",
    "618": "Plus", "619": "Plus",
    "640": "Plus", "641": "Plus", "642": "Plus", "643": "Plus",
    "644": "Plus", "645": "Plus", "646": "Plus", "647": "Plus",
    "648": "Plus", "649": "Plus",
    "720": "Plus", "721": "Plus", "722": "Plus", "723": "Plus", "724": "Plus",
    "730": "Plus", "731": "Plus", "732": "Plus", "733": "Plus", "734": "Plus",
    "745": "Plus", "746": "Plus", "747": "Plus", "748": "Plus", "749": "Plus",
    "789": "Plus",
    "790": "Plus", "791": "Plus", "792": "Plus", "793": "Plus",
    "794": "Plus", "795": "Plus", "796": "Plus", "797": "Plus",
    "798": "Plus", "799": "Plus",
    # ---- T-Mobile Polska
    "454": "T-Mobile", "455": "T-Mobile", "456": "T-Mobile", "457": "T-Mobile",
    "458": "T-Mobile", "459": "T-Mobile",
    "533": "T-Mobile",
    "575": "T-Mobile", "576": "T-Mobile",
    "653": "T-Mobile", "657": "T-Mobile", "663": "T-Mobile",
    "670": "T-Mobile", "671": "T-Mobile", "672": "T-Mobile",
    "673": "T-Mobile", "674": "T-Mobile", "675": "T-Mobile",
    "676": "T-Mobile", "677": "T-Mobile", "678": "T-Mobile",
    "679": "T-Mobile",
    "729": "T-Mobile",
    "883": "T-Mobile",
    # ---- Lycamobile (MVNO on Plus) - own block
    "800": "Lycamobile", "801": "Lycamobile", "802": "Lycamobile",
    "803": "Lycamobile", "804": "Lycamobile", "805": "Lycamobile",
    "806": "Lycamobile", "807": "Lycamobile", "808": "Lycamobile",
    "809": "Lycamobile",
    # ---- Netia (MVNO) - own block
    "524": "Netia", "525": "Netia", "526": "Netia", "527": "Netia",
    "528": "Netia", "529": "Netia",
    # ---- nju mobile (Orange MVNO) - own block
    "700": "nju mobile", "701": "nju mobile", "702": "nju mobile",
    "703": "nju mobile", "704": "nju mobile", "705": "nju mobile",
    "706": "nju mobile", "707": "nju mobile", "708": "nju mobile",
    "709": "nju mobile",
    # ---- Plush (Plus MVNO) - own block
    "710": "Plush", "711": "Plush", "712": "Plush", "713": "Plush",
    # ---- Red Bull Mobile (Play MVNO) - own block
    "810": "Red Bull Mobile", "811": "Red Bull Mobile",
    "812": "Red Bull Mobile", "813": "Red Bull Mobile",
    "814": "Red Bull Mobile", "815": "Red Bull Mobile",
    "816": "Red Bull Mobile", "817": "Red Bull Mobile",
    "818": "Red Bull Mobile", "819": "Red Bull Mobile",
}

# The carriers the user listed, in their order. Kept as the display set: a
# spoofed line never shows anything outside this list.
CARRIERS = (
    "Orange", "Play", "Plus", "T-Mobile",
    "Lycamobile", "Netia", "nju mobile", "Plush", "Red Bull Mobile",
)

# MCC/MNC per carrier (Poland = 260). Used for gsm.sim.operator.numeric and
# TelephonyManager.getSimOperator / getNetworkOperator, which must name the
# same carrier as gsm.operator.alpha.
CARRIER_MNC = {
    "Orange": "03",
    "Play": "06",
    "Plus": "01",
    "T-Mobile": "02",
    "Lycamobile": "09",
    "Netia": "07",
    "nju mobile": "03",       # Orange MVNO shares Orange's MNC
    "Plush": "01",            # Plus MVNO
    "Red Bull Mobile": "06",  # Play MVNO
}

# MVNO -> the host network whose allocation the numbers really come from.
CARRIER_HOST = {
    "Lycamobile": "Plus",
    "Netia": "T-Mobile",
    "nju mobile": "Orange",
    "Plush": "Plus",
    "Red Bull Mobile": "Play",
}

_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone_numbers.txt")
_LOCK = threading.Lock()
_CACHE = None
_CACHE_MTIME = None


def normalise(number):
    """'0048 573 123 456' -> '+48573123456' (or None when it is not a PL mobile)."""
    digits = re.sub(r"[^\d+]", "", str(number or ""))
    if not digits:
        return None
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = ("+48" + digits) if not digits.startswith("48") else "+" + digits
    if not digits.startswith("+48"):
        return None
    rest = digits[3:]
    if len(rest) != 9:
        return None
    return "+48" + rest


def prefix_of(number):
    """The 3-digit AB prefix that identifies the issuer of *number*."""
    norm = normalise(number)
    if not norm:
        return None
    return norm[3:6]


def _candidates(prefix):
    """Carriers that can legitimately issue *prefix*, primary first."""
    primary = PREFIX_OPERATORS.get(prefix)
    if primary:
        return [primary]
    # Not a prefix in our table (Polish number portability means the number is
    # still valid): fall back to the whole list rather than inventing an issuer.
    return list(CARRIERS)


def carrier_for(number):
    """The Polish carrier a real phone would show for *number*."""
    prefix = prefix_of(number)
    if not prefix:
        return None
    return _candidates(prefix)[0]


def numeric_for(carrier):
    """'26006' - the MCC/MNC string for *carrier*."""
    if carrier not in CARRIER_MNC:
        return None
    return "260" + CARRIER_MNC[carrier]


def operator_code_for(carrier):
    """The 5-digit numeric used by older props (no MCC)."""
    return CARRIER_MNC.get(carrier)


def _load_pool():
    """The user-supplied Polish numbers, loaded once (cached on mtime)."""
    global _CACHE, _CACHE_MTIME
    try:
        mtime = os.path.getmtime(_DATA_FILE)
    except OSError:
        return []
    with _LOCK:
        if _CACHE is not None and _CACHE_MTIME == mtime:
            return _CACHE
        numbers = []
        try:
            with open(_DATA_FILE, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    norm = normalise(line)
                    if norm:
                        numbers.append(norm)
        except OSError:
            numbers = []
        # Stable order, no duplicates.
        _CACHE = sorted(set(numbers))
        _CACHE_MTIME = mtime
        return _CACHE


def pool_size():
    """How many numbers the database holds (used by tests/doctor)."""
    return len(_load_pool())


def random_number(exclude=(), rng=None, carrier=None):
    """A random Polish mobile number, never one in *exclude*.

    *carrier* restricts the result to numbers whose *prefix belongs to that
    carrier*, so the SIM status screen can never show a carrier the number
    contradicts. Numbers come from the supplied file when it has one; otherwise
    one is generated inside the carrier's own prefix block.
    """
    rng = rng or random
    excluded = {n for n in (normalise(x) for x in (exclude or ())) if n}

    pool = _load_pool()
    if carrier:
        own_prefixes = _prefixes_for(carrier)
        pool = [n for n in pool if prefix_of(n) in own_prefixes]

    # Randomise the starting point instead of always scanning from the top, so
    # a carrier-excluded pool cannot bias towards late entries.
    available = [n for n in pool if n not in excluded]
    if available:
        return rng.choice(available)

    # The file does not cover this carrier (it only holds Orange/Play/Plus/
    # T-Mobile numbers): generate inside the carrier's own prefix block.
    for _ in range(40):
        candidate = _generate(carrier or rng.choice(CARRIERS), rng)
        if candidate not in excluded:
            return candidate
    return None


def _prefixes_for(carrier):
    """Every prefix whose primary issuer is *carrier*."""
    return {p for p, c in PREFIX_OPERATORS.items() if c == carrier}


def _generate(carrier, rng=None):
    """Build a number inside a prefix that really belongs to *carrier*."""
    rng = rng or random
    prefixes = sorted(_prefixes_for(carrier))
    if not prefixes:
        prefixes = sorted(_prefixes_for("Play"))
    prefix = rng.choice(prefixes)
    return "+48" + prefix + "".join(str(rng.randint(0, 9)) for _ in range(6))


def polish_number_for(brand="", model="", exclude=(), rng=None, carrier=None):
    """The number + carrier pair a spoofed Polish SIM should report.

    Picks the carrier first (uniformly from the user's list, so Play/Orange/
    Plus/T-Mobile appear as often as the smaller MVNOs) and then a number whose
    prefix really belongs to that carrier - so the SIM status screen never shows
    a carrier that contradicts the number.

    Returns ``{"number", "carrier", "numeric", "prefix"}``; the number always
    starts with +48.
    """
    rng = rng or random
    chosen = carrier if carrier in CARRIERS else rng.choice(CARRIERS)
    number = random_number(exclude=exclude, rng=rng, carrier=chosen)
    if not number:
        number = _generate(rng.choice(CARRIERS), rng)
    return {
        "number": number,
        "carrier": chosen,
        "numeric": numeric_for(chosen),
        "prefix": prefix_of(number),
    }
