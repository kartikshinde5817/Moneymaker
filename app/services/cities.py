"""City rotation.

A campaign that searches London every morning finds the same businesses it
already has. Rotating through a pool keeps each run looking at fresh ground,
and the least-recently-used ordering means the whole pool gets covered before
anything repeats.

Only countries where cold B2B email is lawful are included - see
compliance.COUNTRY_POLICY. Adding a country here does not bypass that check;
the send gate still applies per company.
"""

from __future__ import annotations

import datetime as dt

CITY_POOL: dict[str, list[str]] = {
    "GB": [
        "London", "Manchester", "Birmingham", "Leeds", "Bristol", "Liverpool",
        "Sheffield", "Edinburgh", "Glasgow", "Newcastle upon Tyne", "Nottingham",
        "Leicester", "Southampton", "Brighton", "Cardiff", "Belfast", "Reading",
        "Oxford", "Cambridge", "York", "Bath", "Milton Keynes", "Coventry",
        "Norwich", "Exeter", "Aberdeen", "Derby", "Plymouth", "Hull", "Stoke-on-Trent",
    ],
    "US": [
        "New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia",
        "San Antonio", "San Diego", "Dallas", "Austin", "Jacksonville", "San Jose",
        "Columbus", "Charlotte", "Indianapolis", "Seattle", "Denver", "Boston",
        "Nashville", "Portland", "Las Vegas", "Detroit", "Atlanta", "Miami",
        "Minneapolis", "Tampa", "Orlando", "Raleigh", "Pittsburgh", "Sacramento",
    ],
    "IE": ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Kilkenny"],
    "AE": ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah", "Fujairah"],
    "NL": ["Amsterdam", "Rotterdam", "The Hague", "Utrecht", "Eindhoven", "Groningen"],
    "AU": [
        "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Gold Coast",
        "Canberra", "Newcastle", "Wollongong", "Hobart", "Geelong", "Townsville",
        "Cairns", "Darwin", "Sunshine Coast",
    ],
    "NZ": ["Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga", "Dunedin"],
    "SG": ["Singapore"],
    "IN": [
        "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Pune",
        "Kolkata", "Ahmedabad", "Jaipur", "Surat", "Nagpur", "Indore",
        "Kochi", "Chandigarh", "Coimbatore", "Nashik",
    ],
    "ZA": ["Johannesburg", "Cape Town", "Durban", "Pretoria", "Port Elizabeth"],
    "FR": ["Paris", "Lyon", "Marseille", "Toulouse", "Bordeaux", "Lille", "Nantes"],
    "SE": ["Stockholm", "Gothenburg", "Malmo", "Uppsala"],
}

# Countries this rotation may target. Only ones where compliance.COUNTRY_POLICY
# says cold B2B email is lawful - Germany, Austria, Russia, Canada, Denmark,
# Spain and Italy are absent on purpose. Adding a city here cannot bypass the
# send gate; that still checks the company's country at dispatch time.
ROTATION_COUNTRIES = list(CITY_POOL)


def pool_for(countries: list[str]) -> list[tuple[str, str]]:
    """[(country, city), ...] for every requested country we have a pool for."""
    out: list[tuple[str, str]] = []
    for code in countries or []:
        code = code.upper()
        for city in CITY_POOL.get(code, []):
            out.append((code, city))
    return out


def pick_cities(
    countries: list[str], history: dict | None, count: int = 2
) -> tuple[list[str], dict]:
    """Choose `count` least-recently-used cities and stamp them as used today.

    `history` maps "GB:London" -> ISO date last searched. Returns the chosen
    city names and the updated history, which the caller persists on the
    campaign so the next run continues where this one stopped.
    """
    history = dict(history or {})
    candidates = pool_for(countries)
    if not candidates:
        return [], history

    today = dt.date.today().isoformat()

    def city_last(entry: tuple[str, str]) -> str:
        # Never-searched cities sort first.
        return history.get(f"{entry[0]}:{entry[1]}", "")

    # Rotate at the country level too, not just the city level. Sorting on the
    # city date alone keeps returning the first country in the pool, because
    # every unsearched city ties on an empty date - so Australia and India
    # would never come up while any UK city remained.
    country_last: dict[str, str] = {}
    for code, city in candidates:
        seen = history.get(f"{code}:{city}", "")
        if seen > country_last.get(code, ""):
            country_last[code] = seen

    ordered = sorted(candidates, key=lambda e: (country_last.get(e[0], ""), city_last(e)))

    chosen: list[tuple[str, str]] = []
    used_countries: set[str] = set()
    for entry in ordered:
        if len(chosen) >= count:
            break
        if entry[0] in used_countries:
            continue  # one per country first, so a run spans markets
        chosen.append(entry)
        used_countries.add(entry[0])
    for entry in ordered:  # top up if fewer countries than cities requested
        if len(chosen) >= count:
            break
        if entry not in chosen:
            chosen.append(entry)

    for country, city in chosen:
        history[f"{country}:{city}"] = today
    return [city for _, city in chosen], history


def coverage(countries: list[str], history: dict | None) -> dict:
    """How much of the pool has been worked through."""
    history = history or {}
    candidates = pool_for(countries)
    used = sum(1 for c, city in candidates if f"{c}:{city}" in history)
    return {"total": len(candidates), "searched": used,
            "remaining": len(candidates) - used}
