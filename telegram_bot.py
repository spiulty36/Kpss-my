#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kamu İlan Telegram Botu
-----------------------
ilanlar.json dosyasını (kamu_ilan_takip.py üretir) okuyup komutlara cevap verir.
Sadece Python standart kütüphanesi.

Komutlar:
  /yeni            yeni çıkan ilanlar
  /uygun           size uygun görünen ilanlar
  /son             son başvuru günü yaklaşanlar
  /il Ankara       ile göre filtrele
  /kadro zabıta    başlık/şart metnine göre filtrele
  /ara kelime      başlıkta veya notlarda ara
  /yardim          komut listesi

Çalıştırma:
  python telegram_bot.py          sürekli çalışır (bilgisayar/sunucu/Termux)
  python telegram_bot.py --once   bekleyen mesajlara cevap verip çıkar (GitHub Actions)
Gerekli ortam değişkenleri: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

KLASOR = Path(__file__).resolve().parent
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
OFFSET_DOSYASI = KLASOR / "bot_offset.json"

YARDIM = (
    "Komutlar:\n"
    "/yeni – yeni ilanlar\n"
    "/uygun – size uygun görünenler\n"
    "/son – son günü yaklaşanlar\n"
    "/il Ankara – ile göre\n"
    "/kadro zabıta – kadroya göre\n"
    "/ara kelime – başlık/şart içinde ara\n\n"
    "Otomatik değerlendirme yanılabilir, başvurmadan önce resmi ilanı okuyun."
)


def kucuk(s):
    return str(s).replace("İ", "i").replace("I", "ı").lower()


def api(yontem, **p):
    veri = json.dumps(p).encode()
    istek = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{yontem}", data=veri,
                                   headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(istek, timeout=70) as r:
        return json.loads(r.read())


def gonder(chat, metin):
    for i in range(0, len(metin), 3800):
        try:
            api("sendMessage", chat_id=chat, text=metin[i:i + 3800], disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            print("Gönderme hatası:", e)


def veri_oku():
    f = KLASOR / "ilanlar.json"
    if not f.exists():
        return "", []
    d = json.loads(f.read_text("utf-8"))
    return d.get("guncelleme", ""), d.get("ilanlar", [])


def liste(rs, baslik, guncelleme, limit=10):
    rs = [r for r in rs if not r.get("gecmis")]
    if not rs:
        return f"{baslik}: şu an eşleşen ilan yok. (Son tarama: {guncelleme})"
    s = [f"{baslik}: {len(rs)} ilan (son tarama: {guncelleme})\n"]
    for r in rs[:limit]:
        satir = f"• [{r['durum']}] {r['baslik']}"
        if r.get("iller"):
            satir += f" ({r['iller']})"
        if r.get("son_basvuru"):
            satir += f"\n  Son başvuru: {r['son_basvuru']}"
            if r.get("kalan") is not None:
                satir += f" ({r['kalan']} gün)"
        s.append(satir + f"\n  {r['url']}")
    if len(rs) > limit:
        s.append(f"\n… ve {len(rs) - limit} ilan daha. Filtreyle daraltın: /il, /kadro, /ara")
    return "\n".join(s)


def komut(metin):
    parca = metin.strip().split(None, 1)
    if not parca:
        return YARDIM
    k = parca[0].split("@")[0].lower()
    arg = parca[1].strip() if len(parca) > 1 else ""
    guncelleme, rs = veri_oku()
    if not rs and not guncelleme:
        return "Henüz ilan verisi yok. Günlük tarama bir kez çalıştıktan sonra tekrar deneyin."

    uygun_degil = lambda r: r["durum"] == "Uygun değil"  # noqa: E731
    if k in ("/start", "/yardim", "/help"):
        return YARDIM
    if k == "/yeni":
        return liste([r for r in rs if r.get("yeni") and not uygun_degil(r)], "Yeni ilanlar", guncelleme)
    if k == "/uygun":
        return liste([r for r in rs if r["durum"] == "Uygun"], "Uygun ilanlar", guncelleme)
    if k == "/son":
        yakin = [r for r in rs if r.get("kalan") is not None and 0 <= r["kalan"] <= 7 and not uygun_degil(r)]
        yakin.sort(key=lambda r: r["kalan"])
        return liste(yakin, "Son 7 gün içinde bitenler", guncelleme)
    if k in ("/il", "/kadro", "/ara"):
        if not arg:
            return {"/il": "Örnek: /il Ankara", "/kadro": "Örnek: /kadro zabıta", "/ara": "Örnek: /ara hizmetli"}[k]
        a = kucuk(arg)
        if k == "/il":
            sec = [r for r in rs if a in kucuk(r.get("iller", ""))]
        else:
            sec = [r for r in rs if a in kucuk(r["baslik"] + " " + r.get("notlar", ""))]
        return liste([r for r in sec if not uygun_degil(r)], f"'{arg}' için ilanlar", guncelleme)
    return "Anlayamadım.\n\n" + YARDIM


def isle(guncelleme_listesi):
    for g in guncelleme_listesi:
        m = g.get("message") or {}
        chat = str(m.get("chat", {}).get("id", ""))
        metin = m.get("text", "")
        if not chat or not metin:
            continue
        if not CHAT:
            gonder(chat, f"Chat id'niz: {chat}\nBunu GitHub Secret'ına TELEGRAM_CHAT_ID olarak ekleyin.")
        elif chat != CHAT:
            if metin.startswith("/start"):
                gonder(chat, "Bu bot özeldir.")
        else:
            gonder(chat, komut(metin))


def offset_oku():
    try:
        return json.loads(OFFSET_DOSYASI.read_text("utf-8")).get("offset", 0)
    except (OSError, ValueError):
        return 0


def offset_yaz(o):
    OFFSET_DOSYASI.write_text(json.dumps({"offset": o}), "utf-8")


def calistir(tek_sefer):
    offset = offset_oku()
    while True:
        try:
            yanit = api("getUpdates", offset=offset, timeout=0 if tek_sefer else 50)
        except Exception as e:  # noqa: BLE001
            print("Telegram hatası:", e)
            if tek_sefer:
                return
            time.sleep(5)
            continue
        gunc = yanit.get("result", [])
        if gunc:
            isle(gunc)
            offset = gunc[-1]["update_id"] + 1
            offset_yaz(offset)
        if tek_sefer:
            return


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN tanımlı değil.")
    calistir("--once" in sys.argv)
