"""Read-only probe of the OFFICIAL DTEK Telegram web channel; no bot credentials."""
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse

url="https://t.me/s/dtek_ua"
r=requests.get(url,timeout=25,headers={"User-Agent":"Mozilla/5.0 (compatible; SVITLO/1.0)"})
print("status",r.status_code,"bytes",len(r.content),"final_host",urlparse(r.url).hostname)
r.raise_for_status()
s=BeautifulSoup(r.text,"html.parser")
rows=s.select(".tgme_widget_message[data-post]")
print("message_count",len(rows))
for el in rows[-18:]:
    mid=el.get("data-post")
    t=el.select_one("time[datetime]")
    tx=el.select_one(".tgme_widget_message_text")
    text=tx.get_text(" ",strip=True) if tx else ""
    print("post",mid,"date",t.get("datetime") if t else None,"text_head",repr(text[:200]))
