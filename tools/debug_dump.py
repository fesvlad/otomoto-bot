"""Сохраняет сырые данные страниц Otomoto для разработки (запускается вручную)."""
import os, re, sys
sys.path.insert(0, os.getcwd())
os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("CHAT_ID", "x")
import requests
import bot

os.makedirs("dump", exist_ok=True)
url = bot.newest_first(bot.SEARCH_URLS[0])
r = requests.get(url, headers=bot.HEADERS, timeout=30)
open("dump/search.html", "w").write(r.text)
ads = bot.parse_ads(r.text)
print(len(ads), ads[:2])
for i, ad in enumerate(ads[:2]):
    a = requests.get(ad["url"], headers=bot.HEADERS, timeout=30)
    print(ad["url"], a.status_code, len(a.text))
    open(f"dump/ad{i}.html", "w").write(a.text)
