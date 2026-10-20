"""The rules of SanAlert's API contract for one place, in plain Python (no Home Assistant import), so
they can be tested alone. The level comes from the state's cells, never recomputed here (rule 3);
blind is not calm (rule 2); the official layer is told apart and never changes a level (rule 4).
The words are the website's (web/src/lib/text.ts): change them together."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
MAX_AGE_S = 60                          # rule 2: older than this by our clock is no data
LEVELS = ("quiet", "activity", "approaching", "near")
# The sensor's states: the four levels, rule 2's "no data", and a place outside Poland's grid.
STATES = (*LEVELS, "no_data", "out_of_range")
LANGUAGES = ("pl", "en", "uk")


def cell_id(lat: float, lon: float) -> str:
    """The contract's grid, exactly: the epsilon is part of it (50.9 / 0.1 is 508.999… in doubles)."""
    return f"{math.floor(lat * 10 + 1e-9)}:{math.floor(lon * 20 / 3 + 1e-9)}"


def _parse(ts: str | None) -> dt.datetime | None:
    try:
        at = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) and ts else None
    except ValueError:
        return None
    return at.replace(tzinfo=dt.timezone.utc) if at and at.tzinfo is None else at


def age_s(state: dict, now: dt.datetime) -> float | None:
    at = _parse(state.get("generated_at"))
    return (now - at).total_seconds() if at else None


def level(state: dict | None, cell: str, in_poland: bool, now: dt.datetime) -> str:
    """The place's level, or "no_data" whenever rule 2 says so, or "out_of_range" outside Poland."""
    if not in_poland:
        return "out_of_range"
    if not state or state.get("levels_valid") is not True or (state.get("health") or {}).get("status") == "unknown":
        return "no_data"
    age = age_s(state, now)
    if age is None or age > MAX_AGE_S:
        return "no_data"
    got = ((state.get("cells") or {}).get(cell) or {}).get("level", "quiet")
    return got if got in LEVELS else "no_data"


def degraded(state: dict | None) -> bool:
    return bool(state) and (state.get("health") or {}).get("status") == "degraded"


def official_available(state: dict | None, now: dt.datetime) -> bool:
    """Whether the official layer can be told at all: a document no older than rule 2's 60 s, with
    RCB's and RSO's feeds both readable. Without it "no message" would be a guess, not a fact."""
    if not state:
        return False
    age = age_s(state, now)
    sources = (state.get("health") or {}).get("sources") or {}
    return age is not None and age <= MAX_AGE_S and all(
        (sources.get(name) or {}).get("status") != "unknown" for name in ("rcb", "rso"))


RCB_STANDS = dt.timedelta(hours=12)


def in_force(it: dict, now: dt.datetime) -> bool:
    """Rule 4's "current": not cancelled, not an exercise or a cancellation; an RSO message while the
    server publishes it; an RCB alert until its end, and at most 12 hours after the latest sending we
    saw. It used to be its date only: the alert of 24.09 at 22:01 went off at midnight, its end came
    at 05:40. The same rule as SanAlert's website and apps."""
    if it.get("cancelled") or it.get("class") in ("exercise", "cancellation"):
        return False
    return it.get("source") != "rcb" or _rcb_stands(it, now)


def _rcb_stands(it: dict, now: dt.datetime) -> bool:
    """From its latest sending: the first, or sent again (an update that is not an end). With no time
    of ours for any, the day it is dated, as before."""
    updates = [u for u in it.get("updates") or [] if isinstance(u, dict)]
    times = [it.get("first_seen"), *(u.get("seen") for u in updates if not u.get("ended"))]
    sent = [at for at in map(_parse, times) if at]
    if not sent:
        return it.get("date") == now.astimezone(WARSAW).date().isoformat()
    return now < max(sent) + RCB_STANDS


def area(it: dict) -> list[str]:
    """Where a current message stands: for an RCB alert whose end went to part of its area, only the
    rest (24.09, 07:05: the end went to lubelskie, podkarpackie stood)."""
    rest = it.get("regions_in_force")
    return rest if isinstance(rest, list) else it.get("regions") or []


def current_official(state: dict | None, voivodeship: str | None, now: dt.datetime) -> list[dict]:
    """Official messages in force for the voivodeship (rule 4, `in_force` and `area`). A message
    narrowed to powiats still counts for its whole voivodeship here: this says "for the voivodeship",
    never "for you"."""
    if not state or not voivodeship:
        return []
    out = []
    for it in state.get("official") or []:
        if not isinstance(it, dict) or not in_force(it, now) or voivodeship not in area(it):
            continue
        out.append({"source": it.get("source"), "tier": TIER.get(it.get("class")), "title": it.get("title"),
                    "url": it.get("url"), "first_seen": it.get("first_seen")})
    return out


# RCB's three messages about the war across the eastern border (rule 4): a tier, never a level.
TIER = {"precaution": 1, "heightened": 2, "shelter": 3}


def t(lang: str, pl: str, en: str, uk: str) -> str:
    return en if lang == "en" else uk if lang == "uk" else pl


LEVEL_NAME = {
    "quiet": ("Spokój", "Quiet", "Спокій"), "activity": ("Aktywność", "Activity", "Активність"),
    "approaching": ("Zbliża się", "Approaching", "Наближається"), "near": ("Blisko", "Near", "Близько"),
    "no_data": ("Brak danych", "No data", "Немає даних"),
    "out_of_range": ("Poza zasięgiem", "Out of range", "Поза зоною"),
}
KIND = {
    "drone": ("Dron", "Drone", "Дрон"), "recon": ("Dron rozpoznawczy", "Reconnaissance drone", "Розвідувальний дрон"),
    "cruise_missile": ("Pocisk manewrujący", "Cruise missile", "Крилата ракета"),
    "ballistic": ("Pocisk balistyczny", "Ballistic missile", "Балістична ракета"),
}
KIND_MANY = {
    "drone": ("drony", "drones", "дрони"), "recon": ("drony rozpoznawcze", "reconnaissance drones", "розвідувальні дрони"),
    "cruise_missile": ("pociski manewrujące", "cruise missiles", "крилаті ракети"),
    "ballistic": ("pociski balistyczne", "ballistic missiles", "балістичні ракети"),
}
THREAT = {
    "drone": ("zagrożenie dronami", "drone threat", "загроза від дронів"),
    "missile": ("zagrożenie rakietowe", "missile threat", "ракетна загроза"),
    "ballistic": ("zagrożenie balistyczne", "ballistic threat", "загроза від балістичних ракет"),
    "aviation": ("zagrożenie z powietrza", "threat from aircraft", "авіаційна загроза"),
    "glide_bomb": ("zagrożenie bombami kierowanymi", "guided bomb threat", "загроза від керованих авіабомб"),
}
# Polish adjective, English name, Ukrainian adjective (text.ts OBLAST).
OBLAST = {
    "cherkasy": ("czerkaski", "Cherkasy", "Черкаська"), "chernihiv": ("czernihowski", "Chernihiv", "Чернігівська"),
    "chernivtsi": ("czerniowiecki", "Chernivtsi", "Чернівецька"), "dnipropetrovsk": ("dniepropetrowski", "Dnipropetrovsk", "Дніпропетровська"),
    "donetsk": ("doniecki", "Donetsk", "Донецька"), "ivano-frankivsk": ("iwanofrankiwski", "Ivano-Frankivsk", "Івано-Франківська"),
    "kharkiv": ("charkowski", "Kharkiv", "Харківська"), "kherson": ("chersoński", "Kherson", "Херсонська"),
    "khmelnytskyi": ("chmielnicki", "Khmelnytskyi", "Хмельницька"), "kirovohrad": ("kirowohradzki", "Kirovohrad", "Кіровоградська"),
    "kyiv": ("kijowski", "Kyiv", "Київська"), "luhansk": ("ługański", "Luhansk", "Луганська"), "lviv": ("lwowski", "Lviv", "Львівська"),
    "mykolaiv": ("mikołajowski", "Mykolaiv", "Миколаївська"), "odesa": ("odeski", "Odesa", "Одеська"), "poltava": ("połtawski", "Poltava", "Полтавська"),
    "rivne": ("rówieński", "Rivne", "Рівненська"), "sumy": ("sumski", "Sumy", "Сумська"), "ternopil": ("tarnopolski", "Ternopil", "Тернопільська"),
    "vinnytsia": ("winnicki", "Vinnytsia", "Вінницька"), "volyn": ("wołyński", "Volyn", "Волинська"), "zakarpattia": ("zakarpacki", "Zakarpattia", "Закарпатська"),
    "zaporizhzhia": ("zaporoski", "Zaporizhzhia", "Запорізька"), "zhytomyr": ("żytomierski", "Zhytomyr", "Житомирська"),
}
# Places, not oblasts: nominative, locative, genitive (Polish), English, nominative, locative, genitive (Ukrainian).
OBLAST_PLACE = {
    "kyiv-city": ("Kijów", "w Kijowie", "Kijowa", "Kyiv", "Київ", "у Києві", "Києва"),
    "crimea": ("Krym", "na Krymie", "Krymu", "Crimea", "Крим", "у Криму", "Криму"),
    "sevastopol": ("Sewastopol", "w Sewastopolu", "Sewastopola", "Sevastopol", "Севастополь", "у Севастополі", "Севастополя"),
}


def oblast(lang: str, oid: str | None, form: str = "nom") -> str:
    place, known = OBLAST_PLACE.get(oid or ""), OBLAST.get(oid or "")
    i = {"nom": 0, "loc": 1, "gen": 2}[form]
    if lang == "en":
        name = place[3] if place else f"{known[1]} oblast" if known else "Ukraine"
        return f"in {name}" if form == "loc" else name
    if lang == "uk":
        if place:
            return place[4 + i]
        if not known:
            return ("Україна", "в Україні", "України")[i]
        stem = known[2][:-1]
        if form == "nom":
            return f"{known[2]} область"
        if form == "loc":
            word = f"{stem}ій області"
            return ("в " if word[0].upper() in "АЕЄИІЇОУЮЯ" else "у ") + word
        return f"{stem}ої області"
    if place:
        return place[i]
    if not known:
        return ("Ukraina", "w Ukrainie", "Ukrainy")[i]
    return (f"obwód {known[0]}", f"w obwodzie {known[0]}m", f"obwodu {known[0]}ego")[i]


def _whole_uk(oid: str | None) -> str:
    return f"{'всього' if oid in OBLAST_PLACE else 'всієї'} {oblast('uk', oid, 'gen')}"


def _pick(table: dict, key: str | None, lang: str, fallback: tuple[str, str, str]) -> str:
    return t(lang, *table.get(key or "", fallback))


def explain(lang: str, state_level: str, drivers: list[dict]) -> str:
    """The same sentence the website shows under the level (text.ts `explain`)."""
    if state_level == "no_data":
        return t(lang, "Nie mamy teraz danych o obiektach w powietrzu. To nie znaczy, że jest spokojnie.",
                 "We have no data about objects in the air right now. That does not mean it is quiet.",
                 "Зараз у нас немає даних про об’єкти в повітрі. Це не означає, що спокійно.")
    if state_level == "out_of_range":
        return t(lang, "SanAlert liczy poziomy tylko dla Polski.", "SanAlert works out levels for Poland only.",
                 "SanAlert визначає рівні лише для Польщі.")
    if state_level == "quiet":
        return t(lang, "Nic z tego, co śledzimy, nie zmierza w stronę Twojego miejsca.", "Nothing we track is heading for your place.",
                 "Ніщо з того, що ми відстежуємо, не прямує до вашого місця.")
    d = drivers[0] if drivers else None
    if not d:
        return ""
    kind = _pick(KIND, d.get("kind"), lang, ("Nieznany obiekt", "Unknown object", "Невідомий об’єкт"))
    km, eta = d.get("km"), d.get("eta_min")
    code = d.get("code")
    if code == "object_near":
        return t(lang, f"{kind} jest blisko, około {km} km stąd.", f"{kind} is near, about {km} km from here.",
                 f"{kind} близько, приблизно за {km} км звідси.")
    if code == "object_toward":
        return t(lang, f"{kind} leci w tę stronę: około {eta} min, {km} km.", f"{kind} is heading this way: about {eta} min, {km} km.",
                 f"{kind} летить у цей бік: приблизно {eta} хв, {km} км.")
    if code == "object_nearby":
        # API 0.20: `course_known` false is an object with no course at all, never "not this way";
        # absent (an older API) keeps the old sentence.
        if d.get("course_known") is False:
            return t(lang, f"{kind} w odległości około {km} km, kierunek nieznany.", f"{kind} about {km} km away, direction unknown.",
                     f"{kind} приблизно за {km} км, напрямок невідомий.")
        return t(lang, f"{kind} w odległości około {km} km, nie leci w tę stronę.", f"{kind} about {km} km away, not heading this way.",
                 f"{kind} приблизно за {km} км, не летить у цей бік.")
    if code == "track_lost":
        # A track that was heading here vanished from the feed (API 0.20): the cell holds for a while.
        at = _parse(d.get("lost_at"))
        if not at:
            return ""
        hm, low = at.astimezone(WARSAW).strftime("%H:%M"), kind.lower()
        return t(lang, f"Źródła straciły ślad obiektu ({low}) o {hm}, około {km} km stąd. Leciał w tę stronę i mógł lecieć dalej.",
                 f"The sources lost track of an object ({low}) at {hm}, about {km} km from here. It was heading this way and may have continued.",
                 f"Джерела втратили слід об’єкта ({low}) о {hm}, приблизно за {km} км звідси. Він летів у цей бік і міг летіти далі.")
    if code == "ua_alarm_adjacent":
        threat = THREAT.get(d.get("threat") or "")
        what = f": {t(lang, *threat)}" if threat else ""
        where = oblast(lang, d.get("oblast"), "loc")
        head = t(lang, f"Alarm {where} za granicą", f"Alarm {where}, across the border", f"Тривога {where}, за кордоном")
        raion = d.get("raion")
        return head + (t(lang, f", rejon {raion}", f", {raion} raion", f", {raion} район") if raion else "") + f"{what}."
    if code == "area_warning":
        many = _pick(KIND_MANY, d.get("kind"), lang, ("nieznane obiekty", "unknown objects", "невідомі об’єкти"))
        return t(lang, f"Ostrzeżenie dla całego {oblast(lang, d.get('oblast'), 'gen')}: {many}. Źródło nie zna ich pozycji.",
                 f"Warning for the whole of {oblast(lang, d.get('oblast'), 'gen')}: {many}. The source does not know where they are.",
                 f"Попередження для {_whole_uk(d.get('oblast'))}: {many}. Джерело не знає, де вони.")
    return ""                           # a reason this version does not know: the level alone


def notice(state: dict | None, lang: str) -> str | None:
    n = (state or {}).get("notice") or {}
    return n.get(lang) or n.get("en") or n.get("pl")


def attribution(state: dict | None) -> str:
    """Rule 1: every source, with its link (NEPTUN's link is a condition of using its data), and its
    role when the state gives one, "name — role": the role carries the licence (CC BY 4.0, ODbL,
    CC BY 3.0 IGO)."""
    items = [a for a in (state or {}).get("attribution") or [] if isinstance(a, dict) and a.get("name")]
    named = ", ".join(f"{a['name']}{' — ' + a['role'] if a.get('role') else ''} ({a.get('url')})" for a in items)
    return "SanAlert (sanalert.pl), " + (named or "NEPTUN (https://neptun.in.ua)")


def summary(state: dict | None, cell: str, voivodeship: str | None, now: dt.datetime, lang: str) -> dict:
    """Everything the entities show for one place, worked out once per update."""
    in_poland = voivodeship is not None
    lvl = level(state, cell, in_poland, now)
    info = ((state or {}).get("cells") or {}).get(cell) or {}
    raised = lvl in ("activity", "approaching", "near")
    drivers = (info.get("drivers") or []) if raised else []
    first = drivers[0] if drivers else {}
    age = age_s(state, now) if state else None
    return {
        "level": lvl,
        "rank": LEVELS.index(lvl) if lvl in LEVELS else None,
        "level_name": t(lang, *LEVEL_NAME[lvl]),
        "reason": explain(lang, lvl, drivers),
        "reason_code": first.get("code"),
        "kind": first.get("kind"),
        "km": first.get("km"),
        "eta_min": first.get("eta_min"),
        "course_known": first.get("course_known"),
        "lost_at": first.get("lost_at"),
        "oblast": first.get("oblast"),
        "raion": first.get("raion"),
        "threat": first.get("threat"),
        "risk": info.get("risk") if raised else None,
        "confidence": info.get("confidence") if raised else None,
        "degraded": degraded(state),
        "data_age_s": round(age) if age is not None else None,
        "generated_at": (state or {}).get("generated_at"),
        "cell": cell,
        "voivodeship": voivodeship,
        # None, not [], when the official layer cannot be told: an old document is not "no message".
        "official": current_official(state, voivodeship, now) if official_available(state, now) else None,
        "notice": notice(state, lang),
        "attribution": attribution(state),
    }
