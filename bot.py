"""Проверяет поиск на Otomoto и присылает новые объявления в Telegram."""

import html
import json
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT_ID = os.environ["CHAT_ID"].strip()
# Можно указать несколько ссылок, каждую с новой строки.
SEARCH_URLS = [u.strip() for u in os.environ.get("SEARCH_URL", "").split() if u.strip()]

SEEN_FILE = "seen.json"
MAX_SEEN = 3000          # сколько старых объявлений помнить на одну ссылку
MAX_SEND_PER_RUN = 15    # защита от потока сообщений

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

PARAM_KEYS = {
    "year": "📅",
    "mileage": "🛣",
    "fuel_type": "⛽",
    "engine_capacity": "🔧",
    "gearbox": "⚙️",
}


# ---------- ссылка поиска ----------

def newest_first(url):
    """Сортировка «сначала новые» и первая страница, чтобы новые объявления всегда были видны."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("search[order]", "page")]
    query.append(("search[order]", "created_at_first:desc"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def ad_key(url):
    """Ссылка на объявление без лишних параметров. По ней запоминаем, что уже видели."""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}".lower()


# ---------- разбор страницы ----------

def parse_ads(page):
    ads = []
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if match:
        try:
            data = json.loads(match.group(1))
        except ValueError:
            data = None
        if data is not None:
            edges = []
            find_search_edges(data, edges)
            for edge in edges:
                ad = ad_from_node(edge.get("node") if isinstance(edge, dict) else None)
                if ad:
                    ads.append(ad)

    if not ads:
        # Запасной вариант: просто ссылки на объявления из HTML.
        for url in re.findall(r'href="(https://www\.otomoto\.pl/[^"]*/oferta/[^"]+?\.html)', page):
            ads.append({"url": url, "title": None, "price": None, "params": [], "city": None, "photo": None})

    unique = {}
    for ad in ads:
        unique.setdefault(ad_key(ad["url"]), ad)
    return list(unique.values())


def find_search_edges(obj, out):
    """Ищет список результатов поиска (advertSearch.edges) внутри данных страницы."""
    if isinstance(obj, str):
        if "advertSearch" in obj and obj.lstrip().startswith("{"):
            try:
                find_search_edges(json.loads(obj), out)
            except ValueError:
                pass
    elif isinstance(obj, dict):
        search = obj.get("advertSearch")
        if isinstance(search, dict) and isinstance(search.get("edges"), list):
            out.extend(search["edges"])
        for value in obj.values():
            find_search_edges(value, out)
    elif isinstance(obj, list):
        for value in obj:
            find_search_edges(value, out)


def ad_from_node(node):
    if not isinstance(node, dict) or not node.get("url"):
        return None
    price = None
    amount = (node.get("price") or {}).get("amount") or {}
    if amount.get("units") is not None:
        price = f"{int(amount['units']):,}".replace(",", " ") + f" {amount.get('currencyCode', 'PLN')}"
    params = []
    for p in node.get("parameters") or []:
        if isinstance(p, dict) and p.get("key") in PARAM_KEYS:
            params.append(f"{PARAM_KEYS[p['key']]} {p.get('displayValue') or p.get('value')}")
    location = node.get("location") or {}
    city = (location.get("city") or {}).get("name")
    thumb = node.get("thumbnail") or {}
    photo = thumb.get("x2") or thumb.get("x1")
    return {
        "url": node["url"],
        "title": node.get("title"),
        "price": price,
        "params": params,
        "city": city,
        "photo": photo,
    }


# ---------- Telegram ----------

def telegram(method, payload):
    for attempt in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=payload, timeout=30)
        if r.status_code == 429:  # слишком часто, Telegram просит подождать
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return r
    return r


def send_ad(ad):
    lines = [f"🚗 <b>{html.escape(ad['title'] or 'Новое объявление')}</b>"]
    if ad["price"]:
        lines.append(f"💰 {html.escape(ad['price'])}")
    if ad["params"]:
        lines.append(html.escape(" · ".join(ad["params"])))
    if ad["city"]:
        lines.append(f"📍 {html.escape(ad['city'])}")
    lines.append(f'<a href="{html.escape(ad["url"])}">Открыть на Otomoto</a>')
    text = "\n".join(lines)

    if ad["photo"]:
        r = telegram("sendPhoto", {"chat_id": CHAT_ID, "photo": ad["photo"], "caption": text, "parse_mode": "HTML"})
        if r.ok:
            return
    r = telegram("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    if not r.ok:
        raise RuntimeError(telegram_error(r))


def send_text(text):
    r = telegram("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"})
    if not r.ok:
        raise RuntimeError(telegram_error(r))


def telegram_error(r):
    hint = ""
    if r.status_code in (401, 404):
        hint = " (неверный TELEGRAM_TOKEN: проверьте секрет, без пробелов и без слова bot в начале)"
    elif r.status_code in (400, 403):
        hint = " (проверьте CHAT_ID и что бот добавлен в группу)"
    return f"Telegram error: {r.status_code} {r.text}{hint}"


# ---------- состояние ----------

def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


# ---------- главный цикл ----------

def check(search_url, seen):
    url = newest_first(search_url)
    r = requests.get(url, headers=HEADERS, timeout=30)
    print(f"GET {url} -> {r.status_code}, {len(r.text)} bytes")
    r.raise_for_status()
    ads = parse_ads(r.text)
    print(f"Найдено объявлений на странице: {len(ads)}")
    if not ads:
        raise RuntimeError("На странице не найдено ни одного объявления. Возможно, Otomoto изменил сайт или блокирует запросы.")

    known = seen.get(search_url)
    if known is None:
        # Первый запуск для этой ссылки: просто запоминаем всё, что уже есть.
        # Сначала сообщение, потом запоминаем: если Telegram не ответил, попробуем снова в следующий раз.
        send_text(f"✅ Бот запущен и следит за поиском.\nСейчас в выдаче {len(ads)} объявлений. "
                  f"Буду присылать только новые.\n\n{html.escape(search_url)}")
        seen[search_url] = [ad_key(a["url"]) for a in ads]
        return

    known_set = set(known)
    new_ads = [a for a in ads if ad_key(a["url"]) not in known_set]
    print(f"Новых: {len(new_ads)}")

    # Старые сверху, свежие последними
    for ad in reversed(new_ads[:MAX_SEND_PER_RUN]):
        send_ad(ad)
        time.sleep(1)
    if len(new_ads) > MAX_SEND_PER_RUN:
        send_text(f"…и ещё {len(new_ads) - MAX_SEND_PER_RUN} новых. Смотрите все: {html.escape(url)}")

    seen[search_url] = ([ad_key(a["url"]) for a in new_ads] + known)[:MAX_SEEN]


def main():
    if not SEARCH_URLS:
        sys.exit("Не задана ссылка поиска (переменная SEARCH_URL).")
    seen = load_seen()
    failed = False
    for search_url in SEARCH_URLS:
        try:
            check(search_url, seen)
        except Exception as e:  # одна ссылка не должна ломать остальные
            failed = True
            print(f"Ошибка для {search_url}: {e}")
        save_seen(seen)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
