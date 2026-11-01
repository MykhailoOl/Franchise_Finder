import re
import unicodedata
from dataclasses import dataclass

STREET_PREFIXES = {"ul", "ulica", "al", "aleja", "aleje", "pl", "plac", "os", "osiedle"}
HOUSE_PATTERN = re.compile(r"(?:^|\s)(\d+(?:[a-z]{1,3}|\s+[a-z])?(?:\s*[-/]\s*\d+[a-z]{0,3})*)$")
POSTAL_PATTERN = re.compile(r"\b\d{2}-\d{3}\b")
LEADING_HOUSE_PATTERN = re.compile(r"^(\d+[a-z]{0,3}(?:[-/]\d+[a-z]{0,3})*)\s+(.+)$")


def fold_polish(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold().replace("ł", "l"))
    return "".join(character for character in normalized if not unicodedata.combining(character))


def normalize_street(value: str) -> str:
    words = re.sub(r"[^\w\s]|_", " ", fold_polish(value)).split()
    if words and words[0] in STREET_PREFIXES:
        words.pop(0)
    return " ".join(words)


def normalize_house_number(value: str) -> str:
    return re.sub(r"\s+", "", fold_polish(value))


@dataclass(frozen=True)
class AddressQuery:
    street: str
    house_number: str | None
    postal_code: str | None
    full_street: str


def parse_address_query(value: str) -> AddressQuery:
    normalized = fold_polish(value)
    postal_codes = POSTAL_PATTERN.findall(normalized)
    if len(set(postal_codes)) > 1:
        raise ValueError("Enter one postal code per address search")
    postal_code = postal_codes[0] if len(set(postal_codes)) == 1 else None
    normalized = POSTAL_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"\b(?:warszawa|warsaw)\b", " ", normalized)
    normalized = " ".join(normalized.replace(",", " ").split())
    house_match = HOUSE_PATTERN.search(normalized)
    full_street = normalize_street(normalized)
    if house_match:
        return AddressQuery(
            normalize_street(normalized[: house_match.start()]),
            normalize_house_number(house_match.group(1)),
            postal_code,
            full_street,
        )
    leading_house = LEADING_HOUSE_PATTERN.match(normalized)
    if leading_house:
        return AddressQuery(
            normalize_street(leading_house.group(2)),
            normalize_house_number(leading_house.group(1)),
            postal_code,
            full_street,
        )
    return AddressQuery(full_street, None, postal_code, full_street)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
