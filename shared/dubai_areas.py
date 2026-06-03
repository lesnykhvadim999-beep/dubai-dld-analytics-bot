# -*- coding: utf-8 -*-
"""
dubai_areas.py — Comprehensive UAE areas registry.

Sources (priority order):
  1. DB table `areas` (canonical names + aliases + emirate)
  2. Official DLD community list (famproperties.com/list-of-dld-communities, ~300 zones)
  3. Extended static fallback list

Usage:
    from dubai_areas import is_dubai_area, get_emirate, normalize_area, DUBAI_AREAS_LOWER

    is_dubai_area("JVC")           → True
    is_dubai_area("Al Reem Island") → False  (Abu Dhabi)
    get_emirate("Mina Al Arab")    → "Ras Al Khaimah"
    normalize_area("jvc")          → "Jumeirah Village Circle"
"""
import os, time
from typing import Optional

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache_ts    = 0.0
_CACHE_TTL   = 86400  # 24h
_dubai_lower: dict[str, str]  = {}   # lower → canonical
_uae_lower:   dict[str, str]  = {}   # lower → canonical
_emirate_map: dict[str, str]  = {}   # lower → emirate name


# ── Official DLD zones (Dubai only) — ~300 entries ───────────────────────────
_DLD_DUBAI_ZONES = [
    "Abu Hail","Al Asbaq","Al Aweer First","Al Aweer Second","Al Baagh","Al Bada",
    "Al Baharna","Al Baraha","Al Barsha","Al Barsha First","Al Barsha Second",
    "Al Barsha South Fifth","Al Barsha South Fourth","Al Barsha Third",
    "Al Barshaa South First","Al Barshaa South Second","Al Barshaa South Third",
    "Al Buteen","Al Dhagaya","Al Eyas","Al Faga'A","Al Fahidi","Al Garhoud",
    "Al Goze First","Al Goze Fourth","Al Goze Industrial First","Al Goze Industrial Fourth",
    "Al Goze Industrial Second","Al Goze Industrial Third","Al Goze Third","Al Hamriya",
    "Al Hamriya Port","Al Hathmah","Al Hebiah Fifth","Al Hebiah First","Al Hebiah Fourth",
    "Al Hebiah Second","Al Hebiah Sixth","Al Hebiah Third","Al Hudaiba","Al Jadaf",
    "Al Jafliya","Al Karama","Al Khabeesi","Al Khairan First","Al Khairan Second",
    "Al Khawaneej","Al Khawaneej First","Al Khawaneej Second","Al Kheeran","Al Kifaf",
    "Al Layan 2","Al Layan1","Al Lusaily","Al Maha","Al Mamzer","Al Manara","Al Mararr",
    "Al Marmoom","Al Merkadh","Al Meryal","Al Mezhar","Al Mezhar First","Al Mezhar Second",
    "Al Murqabat","Al Musalla (Dubai)","Al Muteena","Al Nahda First","Al Nahda Second",
    "Al Oshoosh","Al Qoaz","Al Qusais","Al Qusais First","Al Qusais Industrial Fifth",
    "Al Qusais Industrial First","Al Qusais Industrial Fourth","Al Qusais Industrial Second",
    "Al Qusais Industrial Third","Al Qusais Second","Al Qusais Third","Al Raffa","Al Ras",
    "Al Rashidiya","Al Rega","Al Rowaiyah First","Al Rowaiyah Second","Al Rowaiyah Third",
    "Al Ruwayyah","Al Sabkha","Al Safaa","Al Saffa First","Al Saffa Second",
    "Al Safouh First","Al Safouh Second","Al Sanaiyya","Al Satwa","Al Suq Al Kabeer",
    "Al Thanayah Fourth","Al Thanyah Fifth","Al Thanyah First","Al Thanyah Second",
    "Al Thanyah Third","Al Ttay","Al Twar First","Al Twar Second","Al Twar Third",
    "Al Waheda","Al Wajehah Al Bhariyah","Al Warqa Fifth","Al Warqa First","Al Warqa Fourth",
    "Al Warqa Second","Al Warqa Third","Al Warsan First","Al Warsan Second","Al Warsan Third",
    "Al Wasl","Al Wohoosh","Al Yelayiss 1","Al Yelayiss 2","Al Yelayiss 3","Al Yelayiss 4",
    "Al Yelayiss 5","Al Yufrah 1","Al Yufrah 2","Al Yufrah 3","Al Yufrah 4","Al Zaroob",
    "Al-Aweer","Al-Baloosh","Al-Bastakiyah","Al-Cornich","Al-Dzahiyyah Al-Jadeedah",
    "Al-Muhaisnah North","Al-Murar Jadeed","Al-Murar Qadeem","Al-Musalla (Deira)",
    "Al-Mustashfa West","Al-Nabgha","Al-Nahdah","Al-Nakhal","Al-Qiyadah","Al-Raulah",
    "Al-Riqqa East","Al-Riqqa West","Al-Safiyyah","Al-Shumaal","Al-Souq Al Kabeer (Deira)",
    "Al-Souq Al-Muqayatah","Al-Tawar","Al-Zarouniyyah","Almarmum First","Almarmum Third",
    "Almeydan","Anakhali","Bukadra","Bur Dubai","Burj Khalifa","Burj Nahar","Business Bay",
    "Cornich Deira","DIFC","Dubai International Airport","Dubai Investment Park First",
    "Dubai Investment Park Second","Emirates Hills Fourth","Eyal Nasser","Festival City First",
    "Ghadeer Al Tair","Ghadeer Barashy","Grayteesah",
    "Hadaeq Sheikh Mohammed Bin Rashid","Hafair","Hatta","Hessyan First","Hessyan Second",
    "Hor Al Anz","Hor Al Anz East","Island 2","Jabal Ali","Jabal Ali First",
    "Jabal Ali Industrial First","Jabal Ali Industrial Second","Jabal Ali Industrial Third",
    "Jabal Ali Second","Jabal Ali Third","Jumeira Island First","Jumeirah","Jumeirah First",
    "Jumeirah Second","Jumeirah Third","Le Hemaira","Lehbab","Lehbab First","Lehbab Second",
    "Madinat Al Mataar","Madinat Dubai Almelaheyah","Madinat Hind 1","Madinat Hind 2",
    "Madinat Hind 3","Madinat Hind 4","Madinat Latifa","Mankhool","Margham","Marsa Dubai",
    "Me'Aisem First","Me'Aisem Second","Mena Jabal Ali","Mereiyeel","Mirdif",
    "Muashrah Al Bahraana","Mugatrah","Muhaisanah Fifth","Muhaisanah First","Muhaisanah Fourth",
    "Muhaisanah Second","Muhaisanah Third","Muhaisna","Muragab","Mushrif","Mushrif Park",
    "Nad Al Hamar","Nad Al Shiba","Nad Al Shiba First","Nad Al Shiba Fourth","Nad Al Shiba Second",
    "Nad Al Shiba Third","Nad Rashid","Nad Shamma","Nadd Hessa","Naif","Naif North","Naif South",
    "Nazwah","Oud Al Muteena","Oud Al Muteena First","Oud Al Muteena Second","Oud Almuteena Third",
    "Oud Metha","Palm Deira","Palm Jabal Ali","Palm Jumeirah","Port Saeed",
    "Ras Al Khor","Ras Al Khor Industrial First","Ras Al Khor Industrial Second",
    "Ras Al Khor Industrial Third","Rega Al Buteen","Remah","Riqat Masali","Saih Aldahal",
    "Saih Alsalam","Saih Shuaelah","Saih Shuaib 1","Saih Shuaib 2","Saih Shuaib 3","Saih Shuaib 4",
    "Shandagha","Shandagha East","Shandagha West","Sikka Al Khail","Sikkat Al Khail North",
    "Sikkat Al Khail South","Souq Al-Lariyyah","Souq Al-Tamar","Souq Sikkat Al Khail",
    "Tareeq Abu Dhabi","Tareeq Al Aweer","Tawaa Al Sayegh","Tawi Al Muraqqab","Tawi Alfuqa",
    "Trade Center First","Trade Center Second","Um Al Sheif","Um Almoameneen","Um Esalay",
    "Um Hurair First","Um Hurair Second","Um Ramool","Um Suqaim","Um Suqaim First",
    "Um Suqaim Second","Um Suqaim Third","Umm Addamin","Umm Hurair","Universe Islands",
    "Wadi Al Amardi","Wadi Al Safa 2","Wadi Al Safa 3","Wadi Al Safa 4","Wadi Al Safa 5",
    "Wadi Al Safa 6","Wadi Al Safa 7","Wadi Alshabak","Warsan Fourth","World Islands",
    "Yaraah","Zaabeel First","Zaabeel Second","Zabeel East","Zareeba Duviya",
    # Commonly used spelling variants not in raw DLD list
    "Jumeirah Beach Residence","JBR","The Walk",
    "The Valley","Emaar Valley",
    "Jumeirah Village Triangle","JVT",
    "Madinat Jumeirah","Madinat",
    "District One","District One MBR","District 1",
    "Dubai Creek Harbor","Dubai Creek Harbour",
    "Maritime City","Dubai Maritime City",
    "Mina Rashid","Port Rashid",
    "Central Park","Central Park City Walk",
    "Azizi Riviera","Azizi",
    "The Oasis","Emaar The Oasis",
    "District 11","District 12",
    "Jumeirah Lakes Towers","JLT",
    "Al Wasl","Al Wasl Road",
    "Sobha One","Sobha Realty","Sobha Hartland","Sobha Hartland 2",
    "Tilal Al Ghaf","Majid Al Futtaim",
    "Nad Al Sheba","Nad Al Sheba 1","Nad Al Sheba 2","Nad Al Sheba 3","Nad Al Sheba 4",
    "Al Hebiah Fourth","Al Hebiah Fifth","Al Hebiah Sixth",
    "Al Barsha South Fifth","Al Barsha South Fourth",
    "Jumeirah 1","Jumeirah 2","Jumeirah 3",
    # Popular communities (not in raw DLD zones but widely used)
    "Downtown Dubai","Dubai Marina","Jumeirah Village Circle","JVC","Dubai Hills Estate",
    "Arabian Ranches","Arabian Ranches 2","Arabian Ranches 3","Jumeirah Lake Towers","JLT",
    "Dubai Silicon Oasis","DSO","Motor City","Jumeirah 1","Jumeirah 2","Jumeirah 3",
    "Al Barsha 1","Al Barsha 2","Al Barsha 3","Al Barsha South","Al Nahda","Al Quoz",
    "Al Furjan","International City","Dubai Investment Park","DIP","Dubailand","Remraam",
    "The Springs","The Meadows","The Lakes","The Views","Emirates Hills","Emirates Living",
    "Jumeirah Islands","Jumeirah Park","Green Community","Dubai South","Expo City",
    "Town Square","Mudon","Serena","Akoya Oxygen","Damac Hills","Damac Hills 2",
    "Damac Lagoons","MBR City","Mohammed Bin Rashid City","Sobha Hartland","Sobha Hartland 2",
    "Creek Harbour","Dubai Creek Harbour","Dubai Healthcare City","DHCC","Culture Village",
    "Jumeirah Golf Estates","Dubai Media City","DMC","Dubai Internet City","DIC",
    "Dubai Knowledge Park","Tecom","Barsha Heights","Palm Jebel Ali","Dubai Islands",
    "Deira Islands","Bluewaters Island","Bluewaters","Dubai Harbour","Port De La Mer",
    "La Mer","City Walk","Wasl","Al Khail","Al Warsan","Academic City",
    "International Media Production Zone","IMPZ","Studio City","Dubai Studio City",
    "Arjan","Liwan","Majan","Wadi Al Safa","Al Mizhar","Nad Al Sheba","Meydan",
    "Tilal Al Ghaf","Dubai Gate","Dubai Production City","Pearl Jumeirah",
    "Jumeirah Bay Island","Bulgari Resort","Al Safa","Zabeel","Rashidiya","Al Twar",
    "Muhaisnah","Nad Al Hamar","Al Khabaisi","Al Muteena","The Greens","The Grand",
    "Emaar Beachfront","Dubai Waterfront","Sheikh Zayed Road","SZR","Trade Centre",
    "Dubai International Financial Centre","World Trade Centre",
    "JBR","Jumeirah Beach Residence","Madinat Jumeirah","Al Sufouh","Al Sufouh 1","Al Sufouh 2",
    "Al Garhoud","Garhoud","Al Karama","Karama","Al Satwa","Satwa","Al Mankhool","Mankhool",
    "Oud Metha","Umm Hurair","Al Barari","Villanova","The Valley","Dubai Festival City",
    "Festival City","Al Khawaneej","Jebel Ali","Jebel Ali Village","Jebel Ali Industrial",
    "Emaar South","Living Legends","Falcon City","Al Raffa","Naif","Al Fahidi","Al Hamriya",
    "Al Muraqqabat","Jumeirah Village Triangle","JVT","Al Quoz Industrial",
    "Meydan City","Meydan One","Sobha Realty","Peninsula","Sports City","Silicon Oasis",
    # Additional variants found in production data
    "Umm Suqeim","Umm Suqeim 1","Umm Suqeim 2","Umm Suqeim 3",
    "Dubai Design District","D3","d3",
    "Dubai Water Canal","Dubai Canal","Water Canal",
    "Madinat Jumeirah Living","MJL","Al Jazi",
    "Creek Beach","Creek Island","Creek Gate",
    "The Acres","Acres",
    "Dubai Opera","Opera District","Opera",
    "Opal Gardens","Nad Al Sheba Gardens",
    "NARA","Nara",
    "Arabian Ranches 1","Arabian Ranches III",
    "Saadiyat Cultural District",
    "Al Barsha South 4","Al Barsha South 5","Al Barsha South Third",
    "Jumeirah Garden City","Jumeirah Gardens City",
    "Hartland 2","Sobha Hartland MBR",
    "Warsan 4","Al Warsan 4",
    "District One West","District One West Phase 1",
    "Al Hebiah 1","Al Hebiah 2","Al Hebiah 3","Al Hebiah 4",
    "Dubai Hills","Dubai Hills Park",
    "The Villa","The Villa Dubai",
    "Expo City Dubai","Expo City Valley",
    "Ghaf Woods","Athlon","The Wilds",
    "Palm Beach","Palm Grove",
    "Meydan Avenue","Meydan MBR",
    "Riviera","Azizi Riviera Phase 1",
    "Haven","The Falls at Haven",
    "Damac Bay","Cavali Bay",
    "Al Safa 1","Al Safa 2","Safa 1","Safa 2",
    "Dubai Science Park","Dubai South City",
    "Jumeirah Park Homes",
    "Park Views","Park Greens",
    "Villanova","Mudon Al Ranim","Town Square Dubai",
]

# ── Known non-Dubai UAE areas ─────────────────────────────────────────────────
_OTHER_UAE = {
    # Abu Dhabi
    "Al Reem Island": "Abu Dhabi", "Al Raha Beach": "Abu Dhabi", "Al Maryah Island": "Abu Dhabi",
    "Saadiyat Island": "Abu Dhabi", "Yas Island": "Abu Dhabi", "Khalifa City": "Abu Dhabi",
    "Al Reef": "Abu Dhabi", "Al Ghadeer": "Abu Dhabi", "Masdar City": "Abu Dhabi",
    "Al Shamkha": "Abu Dhabi", "Mohammed Bin Zayed City": "Abu Dhabi", "MBZ City": "Abu Dhabi",
    "Al Mushrif": "Abu Dhabi", "Corniche Abu Dhabi": "Abu Dhabi", "Al Bateen": "Abu Dhabi",
    "Al Khalidiyah": "Abu Dhabi", "Al Nahyan": "Abu Dhabi", "Tourist Club Area": "Abu Dhabi",
    "Al Wahda": "Abu Dhabi", "Electra": "Abu Dhabi",
    # Ras Al Khaimah
    "Al Marjan Island": "Ras Al Khaimah", "Mina Al Arab": "Ras Al Khaimah",
    "Al Hamra Village": "Ras Al Khaimah", "Yasmin Village": "Ras Al Khaimah",
    "Al Dhait": "Ras Al Khaimah", "Julphar": "Ras Al Khaimah",
    # Sharjah
    "Al Majaz": "Sharjah", "Al Taawun": "Sharjah", "Al Khan": "Sharjah",
    "Muwaileh": "Sharjah", "University City": "Sharjah", "Al Nahda Sharjah": "Sharjah",
    "Al Qasimia": "Sharjah", "Al Butina": "Sharjah", "Aljada": "Sharjah",
    # Ajman
    "Al Nuaimiya": "Ajman", "Al Rashidiya Ajman": "Ajman", "Emirates City": "Ajman",
    "Ajman Downtown": "Ajman",
    # Fujairah
    "Fujairah City": "Fujairah", "Dibba": "Fujairah",
    # Umm Al Quwain
    "UAQ": "Umm Al Quwain", "Umm Al Quwain": "Umm Al Quwain",
    "Siniyah Island": "Umm Al Quwain", "Hayat Island": "Ras Al Khaimah",
    "Mina Al Arab": "Ras Al Khaimah", "Al Hamra Village": "Ras Al Khaimah",
    "Al Marjan Island": "Ras Al Khaimah", "Al Hamra": "Ras Al Khaimah",
    "Danah Bay": "Ras Al Khaimah", "Al Mairid": "Ras Al Khaimah",
    "Saadiyat Cultural District": "Abu Dhabi", "Saadiyat Lagoons": "Abu Dhabi",
    "Yas Golf Collection": "Abu Dhabi", "Hudayriyat Island": "Abu Dhabi",
    "Ghantoot": "Abu Dhabi", "Al Jurf": "Abu Dhabi",
    "Maryam Island": "Sharjah", "Aljada": "Sharjah", "Al Zahia": "Sharjah",
    "Al Danah": "Abu Dhabi", "Ajmal Makan City": "Sharjah",
}


def _build_static() -> None:
    """Build lookup dicts from static lists."""
    global _dubai_lower, _uae_lower, _emirate_map
    # Dubai zones
    for name in _DLD_DUBAI_ZONES:
        k = name.lower()
        _dubai_lower[k] = name
        _uae_lower[k]   = name
        _emirate_map[k] = "Dubai"
    # Other emirates
    for name, emirate in _OTHER_UAE.items():
        k = name.lower()
        _uae_lower[k]   = name
        _emirate_map[k] = emirate


def _load_from_db() -> None:
    """Load areas + aliases from DB. Falls back silently on error."""
    global _dubai_lower, _uae_lower, _emirate_map, _cache_ts
    db_url = os.environ.get("DATABASE_URL", "") or os.environ.get("RESALE_DATABASE_URL", "")
    if not db_url:
        return
    try:
        import psycopg2
        with psycopg2.connect(db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT name, emirate, aliases FROM areas")
            rows = cur.fetchall()
        for name, emirate, aliases in rows:
            emirate = emirate or "Dubai"
            names_to_add = [name] + (aliases or [])
            for n in names_to_add:
                if not n:
                    continue
                k = n.lower()
                _uae_lower[k]   = name   # canonical = primary name
                _emirate_map[k] = emirate
                if emirate == "Dubai":
                    _dubai_lower[k] = name
        _cache_ts = time.time()
    except Exception:
        pass  # silently use static data


def _ensure_loaded() -> None:
    global _cache_ts
    if not _dubai_lower:
        _build_static()
    if time.time() - _cache_ts > _CACHE_TTL:
        _load_from_db()


# ── Public API ────────────────────────────────────────────────────────────────

def is_dubai_area(name: str) -> bool:
    """True if name (or alias) matches a known Dubai area."""
    _ensure_loaded()
    return name.lower().strip() in _dubai_lower


def is_uae_area(name: str) -> bool:
    """True if name matches any known UAE area (any emirate)."""
    _ensure_loaded()
    return name.lower().strip() in _uae_lower


def get_emirate(name: str) -> Optional[str]:
    """Return emirate name for area, or None if unknown."""
    _ensure_loaded()
    return _emirate_map.get(name.lower().strip())


def normalize_area(name: str) -> Optional[str]:
    """Return canonical area name, or None if not found."""
    _ensure_loaded()
    k = name.lower().strip()
    return _dubai_lower.get(k) or _uae_lower.get(k)


def all_dubai_areas() -> set[str]:
    """Return set of all canonical Dubai area names."""
    _ensure_loaded()
    return set(_dubai_lower.values())


def all_uae_areas() -> set[str]:
    """Return set of all canonical UAE area names."""
    _ensure_loaded()
    return set(_uae_lower.values())


# Convenience lowercase sets for fast `in` checks
@property
def DUBAI_AREAS_LOWER() -> dict:
    _ensure_loaded()
    return _dubai_lower

@property
def UAE_AREAS_LOWER() -> dict:
    _ensure_loaded()
    return _uae_lower


if __name__ == "__main__":
    _build_static()
    _load_from_db()
    print(f"Dubai areas: {len(_dubai_lower)}")
    print(f"UAE total:   {len(_uae_lower)}")
    print(f"is_dubai_area('JVC'): {is_dubai_area('JVC')}")
    print(f"is_dubai_area('Al Reem Island'): {is_dubai_area('Al Reem Island')}")
    print(f"get_emirate('Mina Al Arab'): {get_emirate('Mina Al Arab')}")
    print(f"get_emirate('Business Bay'): {get_emirate('Business Bay')}")
    print(f"normalize_area('jvc'): {normalize_area('jvc')}")
