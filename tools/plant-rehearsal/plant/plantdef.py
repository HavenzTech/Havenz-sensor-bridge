"""
The plant, as data: rooms, doors, screens, sensors, engines and people.

Taken from the AHI drawings and the site visit: one building, rooms 101-112, thirteen portrait
wall screens in multi-use room 107 (seven on the east wall, three north, three west), twenty face
readers with a door panel beside ten of them, one site agent in communication room 109, and four
gas engines of 2518 kW each in engine room 112. People and sensor names are invented.
"""

COMPANY_NAME = "AHI Rehearsal"
PROPERTY_NAME = "AHI CHP (rehearsal)"

# (key, name, areaType). Types are free text on the backend; none of these is one of the
# "restricted" types that would demand a contractor orientation.
AREAS = [
    ("101", "101 Entry", "entry"),
    ("102", "102 Lobby", "lobby"),
    ("103", "103 Corridor", "corridor"),
    ("104", "104 Server Room", "server_room"),
    ("105", "105 W/C", "washroom"),
    ("106", "106 Server Room", "server_room"),
    ("107", "107 Multi-use Room", "multi_use"),
    ("108", "108 Mechanical", "mechanical"),
    ("109", "109 Communication", "communication"),
    ("110", "110 MCC Room", "mcc"),
    ("111", "111 Equipment", "equipment"),
    ("112", "112 Engine Room", "engine_room"),
]

# (reader number 1-20, door name, area key, has a door panel)
DOORS = [
    (1, "Door 101-A", "101", True),
    (2, "Door 102-A", "102", True),
    (3, "Door 103-A", "103", True),
    (4, "Door 103-B", "103", True),
    (5, "Door 103-C", "103", True),
    (6, "Door 103-D", "103", True),
    (7, "Door 104-A", "104", True),
    (8, "Door 104-B", "104", True),
    (9, "Door 107-A", "107", True),
    (10, "Door 109-A", "109", True),
    (11, "Door 109-B", "109", False),
    (12, "Door 101-B", "101", False),
    (13, "Door 106-A", "106", False),
    (14, "Door 108-A", "108", False),
    (15, "Door 110-A", "110", False),
    (16, "Door 110-B", "110", False),
    (17, "Door 111-A", "111", False),
    (18, "Door 112-A", "112", False),
    (19, "Door 112-B", "112", False),
    (20, "Door 112-C", "112", False),
]

# The area whose door panels the "one area only" emergency is aimed at: the corridor, four panels.
AREA_SCOPED_EMERGENCY = "103"

# Thirteen wall screens, all portrait, one template each. None of these needs a credential the
# rehearsal does not have: the digital twin (Autodesk) and the world monitor (outside feeds) are
# left out on purpose.
WALLS = [
    ("AHI-107-E1", "AHI-107-E", "engine-room"),
    ("AHI-107-E2", "AHI-107-E", "engine-room-visual"),
    ("AHI-107-E3", "AHI-107-E", "power-platform"),
    ("AHI-107-E4", "AHI-107-E", "energy-flow"),
    ("AHI-107-E5", "AHI-107-E", "grid-interconnection"),
    ("AHI-107-E6", "AHI-107-E", "campus-health"),
    ("AHI-107-E7", "AHI-107-E", "operations-maintenance"),
    ("AHI-107-N1", "AHI-107-N", "live-sensors"),
    ("AHI-107-N2", "AHI-107-N", "security-command"),
    ("AHI-107-N3", "AHI-107-N", "access-personnel"),
    ("AHI-107-W1", "AHI-107-W", "power-market"),
    ("AHI-107-W2", "AHI-107-W", "revenue-margin"),
    ("AHI-107-W3", "AHI-107-W", "esg-compliance"),
]
SKIPPED_TEMPLATES = {
    "twin": "needs Autodesk credentials and a published model",
    "world-monitor": "pulls a dozen outside feeds; not part of an isolated run",
    "external-intel": "pulls outside weather feeds",
}

# Devices. The wall app only sees devices of type "sensor", and finds engines by NAME
# ("Engine <n>") plus a power reading - so that is how they are registered.
ENGINES = [
    {"key": f"engine-{n}", "name": f"Engine {n} Generator", "area": "112", "zone": "Engine room 112",
     "ratedKw": 2518} for n in range(1, 5)
]
LEAK_SENSORS = [
    {"key": "leak-engine-1", "name": "Leak - Engine 1 coolant skid", "area": "112", "zone": "Engine room 112"},
    {"key": "leak-engine-3", "name": "Leak - Engine 3 coolant skid", "area": "112", "zone": "Engine room 112"},
    {"key": "leak-mechanical", "name": "Leak - Mechanical room floor", "area": "108", "zone": "Mechanical 108"},
]
TEMPERATURE_SENSORS = [
    {"key": "temp-server-104", "name": "Temperature - Server room 104", "area": "104", "zone": "Server room 104", "base": 21.5},
    {"key": "temp-server-106", "name": "Temperature - Server room 106", "area": "106", "zone": "Server room 106", "base": 22.0},
    {"key": "temp-mcc-110", "name": "Temperature - MCC room 110", "area": "110", "zone": "MCC room 110", "base": 24.5},
    {"key": "temp-comm-109", "name": "Temperature - Communication 109", "area": "109", "zone": "Communication 109", "base": 23.0},
]
CONTACT_SENSORS = [
    {"key": "contact-engine-rollup", "name": "Contact - Engine hall roll-up door", "area": "112", "zone": "Engine room 112"},
    {"key": "contact-mechanical-ext", "name": "Contact - Mechanical exterior door", "area": "108", "zone": "Mechanical 108"},
]
POWER_METERS = [
    {"key": "meter-main", "name": "Meter - Main incomer 13.8 kV", "area": "110", "zone": "MCC room 110"},
    {"key": "meter-house", "name": "Meter - House load", "area": "110", "zone": "MCC room 110"},
]

# People. Area access by group; every count adds to 60.
ALL_AREAS = [a[0] for a in AREAS if a[0] != "105"]
GROUPS = [
    # (group, count, areas, backend role)
    ("admin", 4, ALL_AREAS, "admin"),
    ("shift-a", 18, ["101", "102", "103", "107", "110", "111", "112"], "employee"),
    ("shift-b", 18, ["101", "102", "103", "107", "110", "111", "112"], "employee"),
    ("office", 12, ["101", "102", "103", "107"], "employee"),
    ("maintenance", 6, ["101", "102", "103", "107", "108", "110", "111", "112"], "employee"),
    ("it", 2, ["101", "102", "103", "104", "106", "109"], "employee"),
]

FIRST_NAMES = [
    "Avery", "Blake", "Carmen", "Dmitri", "Elena", "Farid", "Grace", "Hassan", "Ingrid", "Jorge",
    "Keiko", "Liam", "Maya", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Samir", "Tessa",
    "Umar", "Vera", "Wade", "Ximena", "Yusuf", "Zara", "Anders", "Bianca", "Cedric", "Daria",
    "Emeka", "Fiona", "Gareth", "Hana", "Isaac", "Jolene", "Kwame", "Leila", "Mateo", "Nora",
    "Oskar", "Paloma", "Rafael", "Sienna", "Tomas", "Uma", "Victor", "Willa", "Xavier", "Yara",
    "Zeke", "Amara", "Bruno", "Celeste", "Darius", "Esme", "Felix", "Gemma", "Hugo", "Iris",
    "Jasper", "Kira", "Lars", "Mina", "Nico", "Odette",
]
LAST_NAMES = [
    "Lindqvist", "Okafor", "Reyes", "Tanaka", "Moreau", "Haddad", "Novak", "Fraser", "Banerjee", "Castillo",
    "Whitfield", "Sorensen", "Mbeki", "Petrov", "Gallagher", "Iyer", "Laurent", "Kowalski", "Nakamura", "Osei",
    "Delgado", "Friesen", "Halloran", "Jovanovic", "Kaplan", "Lemieux", "Marchetti", "Nordin", "Oyelaran", "Pereira",
    "Quigley", "Rosales", "Stavros", "Thibault", "Ueda", "Vasquez", "Wojcik", "Yamada", "Zielinski", "Abara",
    "Bergstrom", "Chaudhry", "Dubois", "Eriksen", "Fontaine", "Guerrero", "Hoang", "Ivanova", "Jansen", "Khoury",
    "Lachance", "Mwangi", "Nilsson", "Ortega", "Pawlak", "Ramos", "Schuster", "Tremblay", "Usman", "Vidal",
    "Wolanski", "Yilmaz", "Zamora", "Acheampong", "Bouchard", "Cardenas",
]

EMAIL_DOMAIN = "ahi-rehearsal.local"

CONTRACTOR = {
    "name": "Morgan Tessier",
    "email": f"morgan.tessier@coolant-services.{EMAIL_DOMAIN}",
    "vendorName": "Prairie Coolant Services",
    "phone": "+14035550142",
    "areas": ["101", "103", "108"],
}


def people(count=60):
    """The roster: a list of dicts in a stable order, so a re-seed produces the same plant."""
    roster = []
    index = 0
    scale = count / 60.0
    for group, size, areas, role in GROUPS:
        size = max(1, round(size * scale)) if count != 60 else size
        for n in range(size):
            first, last = FIRST_NAMES[index % len(FIRST_NAMES)], LAST_NAMES[index % len(LAST_NAMES)]
            roster.append({
                "key": f"{group}-{n + 1}",
                "group": group,
                "name": f"{first} {last}",
                "email": f"{first}.{last}@{EMAIL_DOMAIN}".lower(),
                "role": role,
                "areas": list(areas),
            })
            index += 1
    # Named parts in the scenarios.
    by_key = {p["key"]: p for p in roster}
    by_key["admin-1"]["part"] = "plant manager; alert recipient (facility manager)"
    by_key["shift-a-1"]["part"] = "shift A lead; alert recipient (operations lead)"
    by_key["shift-a-2"]["part"] = "opens a door from the mobile app"
    if "office-1" in by_key:
        by_key["office-1"]["part"] = "leaves the company mid-run"
        by_key["office-1"]["areas"] = list(ALL_AREAS)     # so there is something to remove everywhere
    return roster
