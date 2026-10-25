"""Следит за поиском на Otomoto и присылает в Telegram новые объявления и снижения цен."""

import html
import json
import math
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
# Можно указать несколько ссылок, каждую с новой строки.
SEARCH_URLS = [u.strip() for u in os.environ.get("SEARCH_URL", "").split() if u.strip()]

STATE_FILE = "seen.json"
FULL_SCAN_EVERY = timedelta(minutes=60)  # как часто просматривать все страницы поиска
MAX_PAGES = 15
MAX_SEND_PER_RUN = 15
NEW_AD_MAX_AGE = timedelta(hours=48)     # объявления старше считаем «старыми», даже если не видели их раньше
MARKET_KEEP_DAYS = 30
HOT_DISCOUNT = -0.08                     # на 8% дешевле похожих = 🔥
# Снижение цены присылаем, только если оно крупное И машина после него не дороже похожих.
MIN_DROP_PLN = 3000
MIN_DROP_SHARE = 0.03
TZ = ZoneInfo("Europe/Warsaw")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

FUEL_RU = {
    "Benzyna": "бензин", "Diesel": "дизель", "Hybryda": "гибрид",
    "Hybryda Plug-in": "плагин-гибрид", "Elektryczny": "электро",
    "Benzyna+LPG": "бензин+газ", "Benzyna+CNG": "бензин+метан",
}

COUNTRY_RU = {
    "Polska": "Польша", "Niemcy": "Германия", "Belgia": "Бельгия", "Holandia": "Нидерланды",
    "Francja": "Франция", "Włochy": "Италия", "Szwajcaria": "Швейцария", "Austria": "Австрия",
    "Dania": "Дания", "Szwecja": "Швеция", "Norwegia": "Норвегия", "Hiszpania": "Испания",
    "Luksemburg": "Люксембург", "Czechy": "Чехия", "Słowacja": "Словакия", "Litwa": "Литва",
    "Wielka Brytania": "Великобритания", "Stany Zjednoczone": "США", "Kanada": "Канада",
    "Japonia": "Япония", "Korea": "Корея", "Finlandia": "Финляндия", "Irlandia": "Ирландия",
    "Portugalia": "Португалия", "Inny": "другая",
}

# Как страна обычно пишется в тексте объявления (по-польски).
COUNTRY_IN_TEXT = [
    (r"polski(ego|m)? salon|salon polska|z polskiego salonu", "Польша (польский салон)"),
    (r"niemiec|niemcy|niemczech", "Германия"),
    (r"belgi", "Бельгия"),
    (r"holandi", "Нидерланды"),
    (r"francji|francja", "Франция"),
    (r"szwajcari", "Швейцария"),
    (r"austrii|austria", "Австрия"),
    (r"danii|dania", "Дания"),
    (r"szwecji|szwecja", "Швеция"),
    (r"norwegii|norwegia", "Норвегия"),
    (r"włoch", "Италия"),
    (r"hiszpanii|hiszpania", "Испания"),
    (r"luksemburg", "Люксембург"),
    (r"\busa\b|stanów zjednoczonych|ameryki", "США"),
    (r"kanady|kanada", "Канада"),
    (r"anglii|wielkiej brytanii", "Великобритания"),
]

WARNING_WORDS = [
    (r"uszkodz", "повреждён"),
    (r"do naprawy|do remontu", "требует ремонта"),
    (r"po wypadku|powypadk|po kolizji|po szkodzie|szkoda całkowita", "после аварии"),
    (r"zalan|po powodzi", "был затоплен"),
    (r"anglik|kierownica po prawej", "правый руль"),
    (r"copart|iaai|salvage|z aukcji", "аукционная машина (часто битая)"),
    (r"nie odpala|silnik do (wymiany|naprawy|remontu)|skrzynia do (wymiany|naprawy|remontu)", "проблемы с мотором/коробкой"),
    (r"na części", "на запчасти"),
]


# ---------- ссылки ----------

def search_page_url(url, page=1):
    """Сортировка «сначала новые» и нужная страница."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("search[order]", "page")]
    query.append(("search[order]", "created_at_first:desc"))
    if page > 1:
        query.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def ad_key(url):
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}".lower()


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


# ---------- страница поиска ----------

def find_advert_search(obj):
    if isinstance(obj, str):
        if "advertSearch" in obj and obj.lstrip().startswith("{"):
            try:
                return find_advert_search(json.loads(obj))
            except ValueError:
                return None
    elif isinstance(obj, dict):
        s = obj.get("advertSearch")
        if isinstance(s, dict) and isinstance(s.get("edges"), list):
            return s
        for v in obj.values():
            found = find_advert_search(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_advert_search(v)
            if found:
                return found
    return None


def parse_search(page):
    """Возвращает (список объявлений, всего результатов)."""
    data = next_data(page)
    search = find_advert_search(data) if data else None
    ads, total = [], None
    if search:
        total = search.get("totalCount")
        for edge in search["edges"]:
            ad = ad_from_node((edge or {}).get("node"))
            if ad:
                ads.append(ad)
    if not ads:
        # Запасной вариант: просто ссылки на объявления.
        for url in re.findall(r'href="(https://www\.otomoto\.pl/[^"]*/oferta/[^"]+?\.html)', page):
            ads.append({"url": url, "key": ad_key(url), "title": None, "price": None, "params": {}})
    unique = {}
    for ad in ads:
        unique.setdefault(ad["key"], ad)
    return list(unique.values()), total


def ad_from_node(node):
    if not isinstance(node, dict) or not node.get("url"):
        return None
    amount = (node.get("price") or {}).get("amount") or {}
    params = {p["key"]: p for p in node.get("parameters") or [] if isinstance(p, dict) and p.get("key")}
    thumb = node.get("thumbnail") or {}
    seller = (node.get("seller") or {}).get("__typename")
    return {
        "url": node["url"],
        "key": ad_key(node["url"]),
        "title": node.get("title"),
        "subtitle": node.get("shortDescription"),
        "price": int(amount["units"]) if amount.get("units") is not None else None,
        "currency": amount.get("currencyCode", "PLN"),
        "params": params,
        "city": ((node.get("location") or {}).get("city") or {}).get("name"),
        "photo": thumb.get("x2") or thumb.get("x1"),
        "created": node.get("createdAt"),
        "seller": {"PrivateSeller": "частник", "ProfessionalSeller": "дилер"}.get(seller),
        "evaluation": (node.get("priceEvaluation") or {}).get("indicator"),
    }


def param_value(ad, key):
    p = ad["params"].get(key)
    return p.get("value") if p else None


def param_int(ad, key):
    try:
        return int(float(param_value(ad, key)))
    except (TypeError, ValueError):
        return None


# ---------- страница объявления ----------

def fetch_details(ad):
    """Страна, история, тревожные слова. Ошибки здесь не мешают отправке карточки."""
    try:
        data = next_data(get(ad["url"]))
        advert = data["props"]["pageProps"]["advert"]
    except Exception as e:
        print(f"Не удалось открыть объявление {ad['url']}: {e}")
        return {}
    params = {k: (v.get("values") or [{}])[0].get("label") for k, v in (advert.get("parametersDict") or {}).items()}
    description = re.sub(r"<[^>]+>", " ", advert.get("description") or "")
    text = f"{advert.get('title', '')} {description}".lower()

    country = COUNTRY_RU.get(params.get("country_origin"), params.get("country_origin"))
    country_note = None
    if not country:
        for pattern, name in COUNTRY_IN_TEXT:
            if re.search(pattern, text):
                country, country_note = name, "по описанию"
                break
    if not country:
        if params.get("is_imported_car") == "Tak":
            country = "импорт, страна не указана"
        elif params.get("is_imported_car") == "Nie":
            country = "Польша (не импорт)"

    warnings = []
    if params.get("damaged") == "Tak":
        warnings.append("отмечен как повреждённый")
    if params.get("rhd") and params["rhd"] != "Po lewej":
        warnings.append("правый руль")
    for pattern, label in WARNING_WORDS:
        for m in re.finditer(pattern, text):
            before = text[max(0, m.start() - 25):m.start()]
            if re.search(r"(\bbez|\bnie|\bnigdy|żadnych)\W*\w*\W*$", before) or text[max(0, m.start() - 3):m.start()] == "nie":
                continue  # «bez uszkodzeń», «nie był po wypadku» и т.п.
            warnings.append(f"{label} («{m.group(0)}»)")
            break

    good = []
    for key, label in [("no_accident", "без аварий"), ("original_owner", "первый владелец"),
                       ("service_record", "сервисная история"), ("registered", "зарегистрирован в PL")]:
        if params.get(key) == "Tak":
            good.append(label)

    return {
        "country": country,
        "country_note": country_note,
        "warnings": warnings,
        "good": good,
        "version": params.get("version"),
    }


# ---------- оценка цены ----------

def market_compare(ad, market):
    """Сравнивает цену с похожими машинами (та же модель, близкий год и пробег)."""
    model, year, mileage, price = param_value(ad, "model"), param_int(ad, "year"), param_int(ad, "mileage"), ad["price"]
    if not (model and year and price):
        return None
    rows = [r for k, r in market.items() if k != ad["key"] and r[0] == model and r[3]]
    tiers = [
        lambda r: abs(r[1] - year) <= 1 and (mileage is None or r[2] is None or abs(r[2] - mileage) <= 40000),
        lambda r: abs(r[1] - year) <= 1,
        lambda r: abs(r[1] - year) <= 2,
    ]
    for fits in tiers:
        prices = [r[3] for r in rows if r[1] and fits(r)]
        if len(prices) >= 4:
            median = statistics.median(prices)
            return {"diff": (price - median) / median, "median": median, "count": len(prices)}
    return None


def update_market(market, ads, today):
    for ad in ads:
        year = param_int(ad, "year")
        if ad["price"] and year and param_value(ad, "model"):
            market[ad["key"]] = [param_value(ad, "model"), year, param_int(ad, "mileage"), ad["price"], today]


# ---------- Telegram ----------

def telegram(method, payload):
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return r
    return r


def telegram_error(r):
    hint = ""
    if r.status_code in (401, 404):
        hint = " (неверный TELEGRAM_TOKEN)"
    elif r.status_code in (400, 403):
        hint = " (проверьте CHAT_ID и что бот добавлен в группу)"
    return f"Telegram error: {r.status_code} {r.text}{hint}"


def send(text, photo=None, button_url=None, silent=False):
    payload = {"chat_id": CHAT_ID, "parse_mode": "HTML", "disable_notification": "true" if silent else "false"}
    if button_url:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "Открыть на Otomoto", "url": button_url}]]})
    if photo and len(text) <= 1024:
        r = telegram("sendPhoto", {**payload, "photo": photo, "caption": text})
        if r.ok:
            return
    r = telegram("sendMessage", {**payload, "text": text, "disable_web_page_preview": "true"})
    if not r.ok:
        raise RuntimeError(telegram_error(r))


def money(value, currency="PLN"):
    return f"{value:,.0f}".replace(",", " ") + f" {currency}"


def esc(value):
    return html.escape(str(value))


def local_time(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return None


def ad_card(ad, compare, details):
    hot = bool(compare and compare["diff"] <= HOT_DISCOUNT) or (
        ad.get("evaluation") == "BELOW" and (compare is None or compare["diff"] <= 0))
    title = ad["title"] or "Новое объявление"
    version = details.get("version")
    if version and version.lower() not in title.lower():
        title = f"{title} {version}"
    lines = [f"{'🔥 ' if hot else '🚗 '}<b>{esc(title)}</b>"]

    if ad["price"]:
        price_line = f"💰 <b>{money(ad['price'], ad['currency'])}</b>"
        if compare:
            pct = round(compare["diff"] * 100)
            if pct < 0:
                price_line += f" · на {abs(pct)}% дешевле похожих"
            elif pct > 0:
                price_line += f" · на {pct}% дороже похожих"
            else:
                price_line += " · как у похожих"
            price_line += f" ({compare['count']} шт., медиана {money(compare['median'])})"
        lines.append(price_line)
    evaluation = {"BELOW": "ниже рынка ✅", "IN": "в рынке", "ABOVE": "выше рынка"}.get(ad.get("evaluation"))
    if evaluation:
        lines.append(f"📊 Оценка Otomoto: {evaluation}")

    specs = []
    if param_value(ad, "year"):
        specs.append(f"📅 {param_value(ad, 'year')}")
    if param_int(ad, "mileage") is not None:
        specs.append(f"🛣 {param_int(ad, 'mileage'):,} км".replace(",", " "))
    fuel = ad["params"].get("fuel_type", {}).get("displayValue")
    if fuel:
        specs.append(f"⛽ {FUEL_RU.get(fuel, fuel)}")
    power = ad["params"].get("engine_power", {}).get("displayValue")
    if power:
        specs.append(f"⚙️ {power.replace('KM', 'л.с.')}")
    if specs:
        lines.append(" · ".join(esc(s) for s in specs))

    if details.get("country"):
        note = f" ({details['country_note']})" if details.get("country_note") else ""
        lines.append(f"🌍 Откуда: <b>{esc(details['country'])}</b>{esc(note)}")
    who = [f"👤 {ad['seller']}"] if ad.get("seller") else []
    who += [f"✅ {g}" for g in details.get("good", [])]
    if who:
        lines.append(esc(" · ".join(who)))
    for w in details.get("warnings", []):
        lines.append(f"⚠️ {esc(w)}")

    place = [f"📍 {ad['city']}"] if ad.get("city") else []
    published = local_time(ad["created"]) if ad.get("created") else None
    if published:
        place.append(f"🕒 {published}")
    if place:
        lines.append(esc(" · ".join(place)))
    return "\n".join(lines), hot


def send_new_ad(ad, market):
    compare = market_compare(ad, market)
    details = fetch_details(ad)
    text, hot = ad_card(ad, compare, details)
    send(text, photo=ad.get("photo"), button_url=ad["url"], silent=not hot)


def worthy_after_drop(ad, market):
    """После снижения машина не дороже похожих или Otomoto считает цену ниже рынка."""
    if ad.get("evaluation") == "BELOW":
        return True
    compare = market_compare(ad, market)
    if compare:
        return compare["diff"] <= 0
    return ad.get("evaluation") == "IN"


def send_price_drop(ad, old_price, market):
    compare = market_compare(ad, market)
    diff = old_price - ad["price"]
    lines = [
        f"📉 <b>Снижение цены: {esc(ad['title'] or '')}</b>",
        f"было {money(old_price)} → стало <b>{money(ad['price'], ad['currency'])}</b> (−{money(diff)}, −{round(diff / old_price * 100)}%)",
    ]
    if compare:
        pct = round(compare["diff"] * 100)
        lines.append(f"Теперь {'на ' + str(abs(pct)) + '% дешевле' if pct < 0 else 'на ' + str(pct) + '% дороже' if pct > 0 else 'как у'} похожих ({compare['count']} шт.)")
    specs = [s for s in (param_value(ad, "year"), f"{param_int(ad, 'mileage'):,} км".replace(",", " ") if param_int(ad, "mileage") else None, ad.get("city")) if s]
    if specs:
        lines.append(esc(" · ".join(specs)))
    evaluation = {"BELOW": "ниже рынка ✅", "IN": "в рынке"}.get(ad.get("evaluation"))
    if evaluation:
        lines.append(f"📊 Оценка Otomoto: {evaluation}")
    send("\n".join(lines), photo=ad.get("photo"), button_url=ad["url"], silent=False)


# ---------- состояние ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    if state.get("version") != 2:
        # Старый формат: {ссылка: [объявления]}
        old = {k: v for k, v in state.items() if isinstance(v, list)}
        state = {"version": 2, "searches": {u: {"ads": {k: None for k in keys}} for u, keys in old.items()}, "market": {}}
    state.setdefault("searches", {})
    state.setdefault("market", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0, sort_keys=True)


# ---------- главный цикл ----------

def is_recent(ad, now):
    try:
        return now - datetime.fromisoformat(ad["created"].replace("Z", "+00:00")) <= NEW_AD_MAX_AGE
    except Exception:
        return True


def check(search_url, state, now):
    s = state["searches"].setdefault(search_url, {})
    known = s.setdefault("ads", {})
    first_run = not known
    today = now.strftime("%Y-%m-%d")

    last_full = s.get("last_full_scan")
    full_scan = first_run or not last_full or now - datetime.fromisoformat(last_full) >= FULL_SCAN_EVERY

    url = search_page_url(search_url)
    ads, total = parse_search(get(url))
    print(f"Стр. 1: {len(ads)} объявлений, всего в поиске: {total}")
    if not ads:
        raise RuntimeError("На странице не найдено объявлений. Возможно, Otomoto изменил сайт или блокирует запросы.")

    if full_scan and total:
        pages = min(MAX_PAGES, math.ceil(total / max(len(ads), 1)))
        for page in range(2, pages + 1):
            time.sleep(1.5)
            try:
                more, _ = parse_search(get(search_page_url(search_url, page)))
            except Exception as e:
                print(f"Стр. {page}: ошибка {e}")
                break
            print(f"Стр. {page}: {len(more)} объявлений")
            ads += [a for a in more if a["key"] not in {x["key"] for x in ads}]
        s["last_full_scan"] = now.isoformat(timespec="minutes")

    update_market(state["market"], ads, today)

    if first_run:
        for ad in ads:
            known[ad["key"]] = ad["price"]
        send(f"✅ Бот запущен и следит за поиском.\nСейчас в выдаче {len(ads)} объявлений. "
             f"Буду присылать новые и снижения цен.\n\n{esc(search_url)}")
        return

    new_ads, drops = [], []
    for ad in ads:
        if ad["key"] not in known:
            if is_recent(ad, now):
                new_ads.append(ad)
            else:
                known[ad["key"]] = ad["price"]  # старое объявление, просто запоминаем
        elif ad["price"] is not None:
            # known[...] = опорная цена: при первом виде или при последнем сообщении о снижении.
            ref = known[ad["key"]]
            price = ad["price"]
            if ref is None or price > ref:
                known[ad["key"]] = price  # цену подняли: считаем снижения от новой цены
            elif ref - price >= MIN_DROP_PLN and (ref - price) / ref >= MIN_DROP_SHARE:
                if worthy_after_drop(ad, state["market"]):
                    drops.append((ad, ref))
                    known[ad["key"]] = price
                # иначе опорную цену не трогаем: снижения копятся, пока вариант не станет достойным
    print(f"Новых: {len(new_ads)}, снижений цены: {len(drops)}")

    new_ads.sort(key=lambda a: a.get("created") or "")
    for ad in new_ads[:MAX_SEND_PER_RUN]:
        send_new_ad(ad, state["market"])
        known[ad["key"]] = ad["price"]
        save_state(state)
        time.sleep(1)
    if len(new_ads) > MAX_SEND_PER_RUN:
        for ad in new_ads[MAX_SEND_PER_RUN:]:
            known[ad["key"]] = ad["price"]
        send(f"…и ещё {len(new_ads) - MAX_SEND_PER_RUN} новых. Все тут: {esc(url)}")

    for ad, old in drops[:MAX_SEND_PER_RUN]:
        send_price_drop(ad, old, state["market"])
        time.sleep(1)

    # не храним бесконечно
    if len(known) > 5000:
        for k in list(known)[: len(known) - 5000]:
            del known[k]


def prune_market(market, now):
    cutoff = (now - timedelta(days=MARKET_KEEP_DAYS)).strftime("%Y-%m-%d")
    for k in [k for k, r in market.items() if r[4] < cutoff]:
        del market[k]


def main():
    if not TOKEN:
        sys.exit("Пустой TELEGRAM_TOKEN: добавьте его в Settings → Secrets and variables → Actions → Secrets.")
    if not CHAT_ID:
        sys.exit("Пустой CHAT_ID: добавьте его в Settings → Secrets and variables → Actions → Secrets.")
    if not SEARCH_URLS:
        sys.exit("Не задана ссылка поиска (переменная SEARCH_URL).")
    now = datetime.now(timezone.utc)
    state = load_state()
    failed = False
    for search_url in SEARCH_URLS:
        try:
            check(search_url, state, now)
        except Exception as e:
            failed = True
            print(f"Ошибка для {search_url}: {e}")
        save_state(state)
    prune_market(state["market"], now)
    save_state(state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
