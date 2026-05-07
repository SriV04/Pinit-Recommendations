from __future__ import annotations

import re
from typing import Dict, Optional, TypedDict


class Coordinate(TypedDict):
    latitude: float
    longitude: float


class LocationRectangle(TypedDict):
    low: Coordinate
    high: Coordinate


class LondonArea(TypedDict):
    aliases: list[str]
    rectangle: LocationRectangle


LONDON_DEFAULT_RECTANGLE: LocationRectangle = {
    "low": {"latitude": 51.4100, "longitude": -0.3250},
    "high": {"latitude": 51.5680, "longitude": 0.0200},
}

LONDON_AREA_RECTANGLES: Dict[str, LondonArea] = {
    "soho": {
        "aliases": ["soho", "chinatown", "old compton street"],
        "rectangle": {
            "low": {"latitude": 51.5090, "longitude": -0.1435},
            "high": {"latitude": 51.5175, "longitude": -0.1290},
        },
    },
    "covent_garden": {
        "aliases": ["covent garden", "seven dials", "strand"],
        "rectangle": {
            "low": {"latitude": 51.5090, "longitude": -0.1305},
            "high": {"latitude": 51.5168, "longitude": -0.1180},
        },
    },
    "mayfair": {
        "aliases": ["mayfair", "bond street", "berkeley square"],
        "rectangle": {
            "low": {"latitude": 51.5065, "longitude": -0.1575},
            "high": {"latitude": 51.5168, "longitude": -0.1395},
        },
    },
    "marylebone": {
        "aliases": ["marylebone", "baker street", "marylebone high street"],
        "rectangle": {
            "low": {"latitude": 51.5150, "longitude": -0.1650},
            "high": {"latitude": 51.5268, "longitude": -0.1420},
        },
    },
    "fitzrovia": {
        "aliases": ["fitzrovia", "goodge street", "charlotte street"],
        "rectangle": {
            "low": {"latitude": 51.5160, "longitude": -0.1445},
            "high": {"latitude": 51.5245, "longitude": -0.1300},
        },
    },
    "bloomsbury": {
        "aliases": ["bloomsbury", "russell square", "tottenham court road"],
        "rectangle": {
            "low": {"latitude": 51.5180, "longitude": -0.1335},
            "high": {"latitude": 51.5285, "longitude": -0.1135},
        },
    },
    "holborn": {
        "aliases": ["holborn", "chancery lane", "lincoln's inn"],
        "rectangle": {
            "low": {"latitude": 51.5140, "longitude": -0.1205},
            "high": {"latitude": 51.5230, "longitude": -0.1055},
        },
    },
    "clerkenwell": {
        "aliases": ["clerkenwell", "farringdon", "exmouth market"],
        "rectangle": {
            "low": {"latitude": 51.5180, "longitude": -0.1135},
            "high": {"latitude": 51.5320, "longitude": -0.0960},
        },
    },
    "kings_cross": {
        "aliases": ["king's cross", "kings cross", "st pancras", "coal drops yard"],
        "rectangle": {
            "low": {"latitude": 51.5250, "longitude": -0.1320},
            "high": {"latitude": 51.5400, "longitude": -0.1120},
        },
    },
    "camden": {
        "aliases": ["camden", "camden town", "camden market"],
        "rectangle": {
            "low": {"latitude": 51.5340, "longitude": -0.1540},
            "high": {"latitude": 51.5505, "longitude": -0.1300},
        },
    },
    "angel_islington": {
        "aliases": ["angel", "islington", "upper street"],
        "rectangle": {
            "low": {"latitude": 51.5290, "longitude": -0.1165},
            "high": {"latitude": 51.5480, "longitude": -0.0870},
        },
    },
    "shoreditch": {
        "aliases": ["shoreditch", "old street", "hoxton"],
        "rectangle": {
            "low": {"latitude": 51.5190, "longitude": -0.0925},
            "high": {"latitude": 51.5365, "longitude": -0.0685},
        },
    },
    "spitalfields": {
        "aliases": ["spitalfields", "brick lane", "aldgate east"],
        "rectangle": {
            "low": {"latitude": 51.5115, "longitude": -0.0825},
            "high": {"latitude": 51.5255, "longitude": -0.0615},
        },
    },
    "city_of_london": {
        "aliases": ["city of london", "the city", "bank", "monument", "liverpool street"],
        "rectangle": {
            "low": {"latitude": 51.5070, "longitude": -0.1120},
            "high": {"latitude": 51.5225, "longitude": -0.0730},
        },
    },
    "south_bank": {
        "aliases": ["south bank", "waterloo", "london eye"],
        "rectangle": {
            "low": {"latitude": 51.4990, "longitude": -0.1255},
            "high": {"latitude": 51.5085, "longitude": -0.1030},
        },
    },
    "london_bridge": {
        "aliases": ["london bridge", "borough", "borough market", "bankside"],
        "rectangle": {
            "low": {"latitude": 51.4990, "longitude": -0.1010},
            "high": {"latitude": 51.5110, "longitude": -0.0750},
        },
    },
    "bermondsey": {
        "aliases": ["bermondsey", "bermondsey street", "maltby street"],
        "rectangle": {
            "low": {"latitude": 51.4885, "longitude": -0.0925},
            "high": {"latitude": 51.5035, "longitude": -0.0580},
        },
    },
    "peckham": {
        "aliases": ["peckham", "peckham rye", "nunhead"],
        "rectangle": {
            "low": {"latitude": 51.4570, "longitude": -0.0860},
            "high": {"latitude": 51.4820, "longitude": -0.0400},
        },
    },
    "brixton": {
        "aliases": ["brixton", "brixton village", "herne hill"],
        "rectangle": {
            "low": {"latitude": 51.4480, "longitude": -0.1320},
            "high": {"latitude": 51.4705, "longitude": -0.0950},
        },
    },
    "clapham": {
        "aliases": ["clapham", "clapham common", "clapham junction"],
        "rectangle": {
            "low": {"latitude": 51.4520, "longitude": -0.1740},
            "high": {"latitude": 51.4725, "longitude": -0.1280},
        },
    },
    "battersea": {
        "aliases": ["battersea", "battersea power station", "nine elms"],
        "rectangle": {
            "low": {"latitude": 51.4690, "longitude": -0.1835},
            "high": {"latitude": 51.4905, "longitude": -0.1260},
        },
    },
    "chelsea": {
        "aliases": ["chelsea", "king's road", "sloane square"],
        "rectangle": {
            "low": {"latitude": 51.4800, "longitude": -0.1850},
            "high": {"latitude": 51.4975, "longitude": -0.1500},
        },
    },
    "south_kensington": {
        "aliases": ["south kensington", "gloucester road", "earls court"],
        "rectangle": {
            "low": {"latitude": 51.4870, "longitude": -0.2050},
            "high": {"latitude": 51.5020, "longitude": -0.1650},
        },
    },
    "notting_hill": {
        "aliases": ["notting hill", "portobello", "ladbroke grove"],
        "rectangle": {
            "low": {"latitude": 51.5060, "longitude": -0.2200},
            "high": {"latitude": 51.5255, "longitude": -0.1850},
        },
    },
    "paddington": {
        "aliases": ["paddington", "bayswater", "edgware road"],
        "rectangle": {
            "low": {"latitude": 51.5120, "longitude": -0.1900},
            "high": {"latitude": 51.5285, "longitude": -0.1600},
        },
    },
    "hammersmith": {
        "aliases": ["hammersmith", "brook green"],
        "rectangle": {
            "low": {"latitude": 51.4860, "longitude": -0.2400},
            "high": {"latitude": 51.5055, "longitude": -0.2100},
        },
    },
    "fulham": {
        "aliases": ["fulham", "parsons green", "fulham broadway"],
        "rectangle": {
            "low": {"latitude": 51.4640, "longitude": -0.2200},
            "high": {"latitude": 51.4905, "longitude": -0.1750},
        },
    },
    "hackney": {
        "aliases": ["hackney", "hackney central", "london fields", "broadway market"],
        "rectangle": {
            "low": {"latitude": 51.5300, "longitude": -0.0750},
            "high": {"latitude": 51.5555, "longitude": -0.0350},
        },
    },
    "dalston": {
        "aliases": ["dalston", "stoke newington", "stokey"],
        "rectangle": {
            "low": {"latitude": 51.5370, "longitude": -0.0900},
            "high": {"latitude": 51.5680, "longitude": -0.0600},
        },
    },
    "bethnal_green": {
        "aliases": ["bethnal green", "cambridge heath"],
        "rectangle": {
            "low": {"latitude": 51.5200, "longitude": -0.0710},
            "high": {"latitude": 51.5380, "longitude": -0.0430},
        },
    },
    "victoria_westminster": {
        "aliases": ["victoria", "westminster", "pimlico", "st james"],
        "rectangle": {
            "low": {"latitude": 51.4900, "longitude": -0.1510},
            "high": {"latitude": 51.5105, "longitude": -0.1200},
        },
    },
    "greenwich": {
        "aliases": ["greenwich", "north greenwich", "cutty sark"],
        "rectangle": {
            "low": {"latitude": 51.4710, "longitude": -0.0300},
            "high": {"latitude": 51.5070, "longitude": 0.0200},
        },
    },
    "canary_wharf": {
        "aliases": ["canary wharf", "docklands", "isle of dogs"],
        "rectangle": {
            "low": {"latitude": 51.4900, "longitude": -0.0350},
            "high": {"latitude": 51.5150, "longitude": -0.0005},
        },
    },
    "stratford": {
        "aliases": ["stratford", "westfield stratford", "olympic park"],
        "rectangle": {
            "low": {"latitude": 51.5310, "longitude": -0.0200},
            "high": {"latitude": 51.5600, "longitude": 0.0150},
        },
    },
    "wimbledon": {
        "aliases": ["wimbledon", "wimbledon village"],
        "rectangle": {
            "low": {"latitude": 51.4100, "longitude": -0.2350},
            "high": {"latitude": 51.4355, "longitude": -0.1900},
        },
    },
    "richmond": {
        "aliases": ["richmond", "richmond upon thames"],
        "rectangle": {
            "low": {"latitude": 51.4450, "longitude": -0.3250},
            "high": {"latitude": 51.4720, "longitude": -0.2700},
        },
    },
}


def _normalise_area_text(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9&+\-\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _copy_rectangle(rectangle: LocationRectangle) -> LocationRectangle:
    return {
        "low": dict(rectangle["low"]),
        "high": dict(rectangle["high"]),
    }


def find_london_area_rectangle(text: str) -> Optional[LocationRectangle]:
    normalised = _normalise_area_text(text)
    for area in LONDON_AREA_RECTANGLES.values():
        for alias in area["aliases"]:
            alias_text = _normalise_area_text(alias)
            if alias_text and _contains_phrase(normalised, alias_text):
                return _copy_rectangle(area["rectangle"])
    return None


def default_london_rectangle() -> LocationRectangle:
    return _copy_rectangle(LONDON_DEFAULT_RECTANGLE)
