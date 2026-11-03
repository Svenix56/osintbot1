import re
import ipaddress
from enum import Enum


class TargetType(str, Enum):
    IP = "ip"
    DOMAIN = "domain"
    EMAIL = "email"
    COORDS = "coords"
    PHONE = "phone"
    TG_ID = "tg_id"
    CAR_PLATE = "car_plate"
    FULLNAME = "fullname"
    USERNAME = "username"
    UNKNOWN = "unknown"


LAT_TO_CYR_PLATE = str.maketrans("ABEKMHOPCTYXabekmhopctyx", "АВЕКМНОРСТУХАВЕКМНОРСТУХ")
CYR_TO_LAT_PLATE = str.maketrans("АВЕКМНОРСТУХавекмнорстух", "abekmhopctyxabekmhopctyx")

CAR_PLATE_RE = re.compile(
    r"^[АВЕКМНОРСТУХABEKMHOPCTYX]{1}\s*\d{3}\s*[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\s*\d{2,3}$",
    re.IGNORECASE
)

EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
DOMAIN_RE = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$")
COORDS_RE = re.compile(r"^(-?\d+(\.\d+)?),\s*(-?\d+(\.\d+)?)$")
PHONE_RE = re.compile(r"^\+?[7891-9]\d{8,14}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
FULLNAME_RE = re.compile(r"^[а-яА-ЯёЁa-zA-Z]{2,25}\s+[а-яА-ЯёЁa-zA-Z]{2,25}(?:\s+[а-яА-ЯёЁa-zA-Z]{2,25})?$")
SOCIAL_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|vk\.com|github\.com|instagram\.com|tiktok\.com|youtube\.com)/@?([a-zA-Z0-9_.-]+)/?$",
    re.IGNORECASE
)


def normalize_car_plate(raw: str) -> tuple[str, str]:
    no_spaces = re.sub(r"[\s\-]", "", raw.strip()).upper()
    cyr = no_spaces.translate(LAT_TO_CYR_PLATE)
    lat = cyr.translate(CYR_TO_LAT_PLATE).lower()
    return cyr, lat


def normalize_target_key(raw: str) -> str:
    _, clean = detect_target_type(raw)
    return clean.strip().lower().lstrip("@")


def detect_target_type(raw_target: str) -> tuple[TargetType, str]:
    clean = raw_target.strip()

    if CAR_PLATE_RE.match(clean):
        cyr_plate, _ = normalize_car_plate(clean)
        return TargetType.CAR_PLATE, cyr_plate

    url_match = SOCIAL_URL_RE.match(clean)
    if url_match:
        extracted = url_match.group(1)
        if extracted.startswith("id") and extracted[2:].isdigit():
            return TargetType.TG_ID, extracted[2:]
        return TargetType.USERNAME, extracted

    try:
        ipaddress.ip_address(clean)
        return TargetType.IP, clean
    except ValueError:
        pass

    if COORDS_RE.match(clean):
        return TargetType.COORDS, clean

    if EMAIL_RE.match(clean):
        return TargetType.EMAIL, clean.lower()

    if clean.isdigit() and 5 <= len(clean) <= 11:
        return TargetType.TG_ID, clean

    phone_candidate = re.sub(r"[\s\-\(\)]", "", clean)
    if PHONE_RE.match(phone_candidate):
        if phone_candidate.startswith("8") and len(phone_candidate) == 11:
            phone_candidate = "+7" + phone_candidate[1:]
        elif not phone_candidate.startswith("+"):
            phone_candidate = "+" + phone_candidate
        return TargetType.PHONE, phone_candidate

    if FULLNAME_RE.match(clean):
        return TargetType.FULLNAME, clean

    if DOMAIN_RE.match(clean) and not clean.startswith("@"):
        return TargetType.DOMAIN, clean.lower()

    if USERNAME_RE.match(clean.lstrip("@")):
        return TargetType.USERNAME, clean.lstrip("@")

    return TargetType.UNKNOWN, clean