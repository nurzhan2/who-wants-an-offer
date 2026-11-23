"""The cities this market's candidates live in, as each language writes them.

Shared by the CV header (:mod:`app.documents.render`) and the letter's line
about where the candidate works from (:mod:`app.letters.generator`), so the
two documents sent together name the place and its time zone the same way.
Geography, not a claim about the candidate: a city the table does not hold is
printed as stored, with nothing added.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Place:
    """A city as each CV language writes it, with its country and UTC offset."""

    en: str
    ru: str
    country_en: str
    country_ru: str
    utc: str


#: The cities this market's candidates live in. The reference CVs open the
#: contact line with "Almaty, Kazakhstan (UTC+5)": a remote recruiter's first
#: two questions are where and which hours, and the city alone answers neither
#: for someone who has never heard of it. None of this is a claim about the
#: candidate — it is the geography of the city the profile records — and a
#: city not listed here is printed exactly as stored, with nothing added.
#: Kazakhstan has been on a single UTC+5 since March 2024.
PLACES: tuple[Place, ...] = (
    Place("Almaty", "Алматы", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Astana", "Астана", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Shymkent", "Шымкент", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Karaganda", "Караганда", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Aktobe", "Актобе", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Atyrau", "Атырау", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Pavlodar", "Павлодар", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Oskemen", "Усть-Каменогорск", "Kazakhstan", "Казахстан", "UTC+5"),
    Place("Bishkek", "Бишкек", "Kyrgyzstan", "Кыргызстан", "UTC+6"),
    Place("Tashkent", "Ташкент", "Uzbekistan", "Узбекистан", "UTC+5"),
    Place("Moscow", "Москва", "Russia", "Россия", "UTC+3"),
    Place("Saint Petersburg", "Санкт-Петербург", "Russia", "Россия", "UTC+3"),
    Place("Minsk", "Минск", "Belarus", "Беларусь", "UTC+3"),
    Place("Tbilisi", "Тбилиси", "Georgia", "Грузия", "UTC+4"),
)

#: Other spellings of the same cities, as resumes write them.
PLACE_ALIASES: dict[str, str] = {
    "алма-ата": "Almaty",
    "alma-ata": "Almaty",
    "нур-султан": "Astana",
    "nur-sultan": "Astana",
    "усть-каменогорск": "Oskemen",
    "ust-kamenogorsk": "Oskemen",
    "st. petersburg": "Saint Petersburg",
    "санкт петербург": "Saint Petersburg",
    "karagandy": "Karaganda",
}


def place_of(city: str) -> Place | None:
    """The place a stored city names, in either alphabet, or None."""
    key = " ".join(city.split()).casefold()
    key = PLACE_ALIASES.get(key, key).casefold()
    return next(
        (place for place in PLACES if key in (place.en.casefold(), place.ru.casefold())), None
    )
