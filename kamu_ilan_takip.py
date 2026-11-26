#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kamu İlan Takip Programı (v2)
-----------------------------
- Kaynak sayfaları tarar, ilan metnini okur, profilinize göre Uygun / Kontrol et / Uygun değil işaretler
- İl ve kadro filtresi (ayarlar.json)
- Son başvuru tarihine göre sıralama ve "kaç gün kaldı" uyarısı
- Telegram bildirimi (yeni uygun ilanlar + son günü yaklaşanlar)
- Telefonda açılan ilanlar.html; "Başvurdum" kutusu telefonun tarayıcısında saklanır
- GitHub Actions ile her gün otomatik çalışabilir (bkz. .github/workflows/gunluk.yml)

Sadece Python 3.8+ standart kütüphanesi. Çalıştırma:  python kamu_ilan_takip.py
"""
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

KLASOR = Path(__file__).resolve().parent

# ----------------------------------------------------------------- PROFİL
PROFIL = {
    "puan": 76,
    "puan_turu": "P94",
    "egitim": "ortaöğretim",
    "yas": 22,
    "aktif_ogrenci": True,
}

# ---------------------------------------------------------------- AYARLAR
# ayarlar.json ile değiştirilebilir. Örnek:
# {"tercih_iller": ["Ankara", "İstanbul"], "kadro_anahtar": ["zabıta", "vhki"],
#  "sadece_uygun": false, "profil_gizle": true, "uyari_gun": 3}
AYARLAR = {
    "tercih_iller": [],     # boş = tüm iller. İl belli değilse ilan yine de gösterilir.
    "kadro_anahtar": [],    # boş = tüm kadrolar (ör: ["zabıta", "bekçi", "vhki", "koruma"])
    "sadece_uygun": False,  # True ise "Uygun değil" ilanlar listeden çıkarılır
    "profil_gizle": False,  # True ise sayfada puanınız yazmaz (herkese açık yayında önerilir)
    "uyari_gun": 3,         # son başvuruya bu kadar gün kala bildirim gönder
}

KAYNAKLAR = [
    {"ad": "ÖSYM Duyurular", "url": "https://www.osym.gov.tr/TR,8797/duyurular.html"},
    {"ad": "İŞKUR", "url": "https://www.iskur.gov.tr"},
    {"ad": "Resmi Gazete İlan", "url": "https://www.resmigazete.gov.tr/ilanlar"},
    {"ad": "Adalet Bakanlığı", "url": "https://www.adalet.gov.tr/duyurular"},
    {"ad": "Aile Bakanlığı", "url": "https://www.aile.gov.tr/duyurular"},
    {"ad": "KGM", "url": "https://www.kgm.gov.tr/Sayfalar/KGM/SiteTr/Duyurular/Duyurular.aspx"},
]

BASVURU_YERI_NOTU = (
    "Çoğu alım Kariyer Kapısı (isealimkariyerkapisi.cbiko.gov.tr) veya e-Devlet, "
    "bazıları kurumun kendi sitesi ya da İŞKUR üzerinden yapılır. İlan metnini mutlaka okuyun."
)

ANAHTAR = re.compile(
    r"(alım|alımı|ilan|ilanı|personel|işçi|memur|zabıta|bekçi|koruma|sözleşmeli|"
    r"güvenlik|vhki|katip|şoför|hizmetli|kadro|istihdam)", re.I)

ILLER = ["Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya",
         "Ardahan", "Artvin", "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik",
         "Bingöl", "Bitlis", "Bolu", "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum",
         "Denizli", "Diyarbakır", "Düzce", "Edirne", "Elazığ", "Erzincan", "Erzurum",
         "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkari", "Hatay", "Iğdır",
         "Isparta", "İstanbul", "İzmir", "Kahramanmaraş", "Karabük", "Karaman", "Kars",
         "Kastamonu", "Kayseri", "Kilis", "Kırıkkale", "Kırklareli", "Kırşehir", "Kocaeli",
         "Konya", "Kütahya", "Malatya", "Manisa", "Mardin", "Mersin", "Muğla", "Muş",
         "Nevşehir", "Niğde", "Ordu", "Osmaniye", "Rize", "Sakarya", "Samsun", "Siirt",
         "Sinop", "Sivas", "Şanlıurfa", "Şırnak", "Tekirdağ", "Tokat", "Trabzon", "Tunceli",
         "Uşak", "Van", "Yalova", "Yozgat", "Zonguldak"]

MAX_DETAY = 80
BEKLEME = 1.0
TARIH_RE = r"(\d{1,2}[./]\d{1,2}[./]\d{4})"


# ------------------------------------------------------------------ YARDIMCI
class Ayiklayici(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.linkler, self.metin = [], []
        self._href, self._ltxt, self._skip = None, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._href, self._ltxt = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            t = " ".join("".join(self._ltxt).split())
            if t:
                self.linkler.append((t, self._href))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.metin.append(data)
        if self._href is not None:
            self._ltxt.append(data)


def indir(url):
    istek = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KamuIlanTakip)"})
    with urllib.request.urlopen(istek, timeout=25) as r:
        veri = r.read()
        kod = r.headers.get_content_charset() or "utf-8"
    try:
        return veri.decode(kod, errors="replace")
    except LookupError:
        return veri.decode("utf-8", errors="replace")


def sayfa_coz(url):
    p = Ayiklayici()
    p.feed(indir(url))
    return p


def kucuk(s):
    return s.replace("İ", "i").replace("I", "ı").lower()


def json_oku(ad, varsayilan):
    f = KLASOR / ad
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except ValueError:
            print(f"UYARI: {ad} okunamadı, varsayılan kullanılıyor.")
    return varsayilan


def tarih_cevir(s):
    try:
        return datetime.strptime(s.replace("/", "."), "%d.%m.%Y").date()
    except ValueError:
        return None


def son_tarih_bul(t):
    m = re.search(r"(?:son başvuru|başvuru bitiş|başvuruların sona|tarihine kadar|son gün)[^.]{0,80}?" + TARIH_RE, t)
    if m:
        return m.group(1)
    adaylar = [tarih_cevir(x) for x in re.findall(r"başvuru[^.]{0,100}?" + TARIH_RE, t)]
    adaylar = [a for a in adaylar if a]
    return max(adaylar).strftime("%d.%m.%Y") if adaylar else ""


# ------------------------------------------------------------------- ANALİZ
def analiz(baslik, metin):
    t = kucuk(baslik + " " + metin)
    notlar, kotu, belirsiz = [], False, False

    lise = bool(re.search(r"ortaöğretim|lise|p94|p-94", t))
    ust = bool(re.search(r"önlisans|ön lisans|lisans mezun|yükseköğretim mezun|fakülte mezun|p93|p3\b", t))
    if lise:
        notlar.append("Ortaöğretim/lise düzeyi geçiyor")
    if ust and not lise:
        kotu = True
        notlar.append("Önlisans/lisans şartı var gibi (sizin düzeyiniz değil)")
    elif ust and lise:
        belirsiz = True
        notlar.append("Hem lise hem üst düzey kadro olabilir, kadro satırını kontrol edin")
    if not lise and not ust:
        belirsiz = True
        notlar.append("Eğitim şartı metinde net bulunamadı")

    if "kpss" in t:
        notlar.append("KPSS şartı var")
        if re.search(r"p94|p-94|ortaöğretim", t) is None and re.search(r"p93|p3\b|p10|p11|p12", t):
            kotu = True
            notlar.append("Farklı KPSS puan türü isteniyor")
        sayilar = [int(x) for x in re.findall(r"\b([4-9]\d)\s*(?:\([^)]{0,25}\))?\s*(?:ve üzeri\s*)?puan", t)]
        sayilar += [int(x) for x in re.findall(r"(?:en az|asgari|taban)[^.\d]{0,40}\b([4-9]\d)\b", t)]
        sayilar = sorted(set(s for s in sayilar if 45 <= s <= 100))
        if sayilar:
            notlar.append("Metindeki puan değerleri: " + ", ".join(map(str, sayilar)))
            if min(sayilar) > PROFIL["puan"]:
                kotu = True
                notlar.append(f"Asgari puan {PROFIL['puan']}'dan yüksek")
    elif re.search(r"işkur|işçi|kura|mülakat|sözlü sınav", t):
        notlar.append("KPSS dışı alım (İŞKUR/kura/sınav) olabilir")
        belirsiz = True

    if PROFIL["aktif_ogrenci"] and re.search(r"örgün öğrenci|öğrenci olmamak|öğrenimine devam etmemek|öğrenci olmayan", t):
        belirsiz = True
        notlar.append("UYARI: öğrenci olmama şartı geçiyor, aktif öğrencilik engel olabilir")

    for m in re.finditer(r"(\d{2})\s*yaş", t):
        y = int(m.group(1))
        if 25 <= y <= 40:
            if PROFIL["yas"] > y:
                kotu = True
            notlar.append(f"Yaş sınırı: {y}")
            break

    iller = [i for i in ILLER if kucuk(i) in t][:6]
    tarih = son_tarih_bul(t)
    durum = "Uygun değil" if kotu else ("Kontrol et" if belirsiz else "Uygun")
    return durum, notlar, iller, tarih, t


def filtreden_gecer(r, t):
    tercih = [kucuk(x) for x in AYARLAR["tercih_iller"]]
    if tercih and r["iller"] and not any(kucuk(i) in tercih for i in r["iller"].split(", ")):
        return False
    kadro = [kucuk(x) for x in AYARLAR["kadro_anahtar"]]
    if kadro and not any(k in t for k in kadro):
        return False
    if AYARLAR["sadece_uygun"] and r["durum"] == "Uygun değil":
        return False
    return True


# ----------------------------------------------------------------- BİLDİRİM
def telegram_gonder(metin):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram ayarlı değil (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), bildirim atlandı.")
        return
    for parca in [metin[i:i + 3800] for i in range(0, len(metin), 3800)]:
        veri = json.dumps({"chat_id": chat, "text": parca, "disable_web_page_preview": True}).encode()
        istek = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=veri,
                                       headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(istek, timeout=20).read()
        except Exception as e:  # noqa: BLE001
            print("Telegram hatası:", e)
            return


def bildirim_hazirla(sonuc):
    yeni = [r for r in sonuc if r["yeni"] and r["durum"] != "Uygun değil"]
    yakin = [r for r in sonuc if r["durum"] != "Uygun değil" and r["kalan"] is not None
             and 0 <= r["kalan"] <= AYARLAR["uyari_gun"] and not r["yeni"]]
    satir = []
    if yeni:
        satir.append(f"🆕 {len(yeni)} yeni ilan:")
        for r in yeni[:12]:
            satir.append(f"• [{r['durum']}] {r['baslik']}"
                         + (f" ({r['iller']})" if r["iller"] else "")
                         + (f" – son: {r['son_basvuru']}" if r["son_basvuru"] else "") + f"\n{r['url']}")
    if yakin:
        satir.append(f"\n⏰ Son günü yaklaşanlar:")
        for r in yakin[:12]:
            satir.append(f"• {r['baslik']} – {r['kalan']} gün kaldı\n{r['url']}")
    return "\n".join(satir)


# --------------------------------------------------------------------- ANA
def calistir():
    ayar = json_oku("ayarlar.json", {})
    AYARLAR.update({k: v for k, v in ayar.items() if k in AYARLAR})
    gorulen = set(json_oku("gorulenler.json", []))
    kaynaklar = json_oku("kaynaklar.json", KAYNAKLAR)
    adaylar, durumlar = {}, []

    for k in kaynaklar:
        try:
            s = sayfa_coz(k["url"])
            sayac = 0
            for yazi, href in s.linkler:
                if len(yazi) > 12 and ANAHTAR.search(yazi) and not href.lower().startswith(("javascript", "mailto", "#")):
                    url = urljoin(k["url"], href)
                    if url not in adaylar:
                        adaylar[url] = (k["ad"], yazi)
                        sayac += 1
            durumlar.append((k["ad"], f"{sayac} aday bağlantı" if sayac else "bağlantı bulunamadı (JS ile yükleniyor olabilir)"))
        except Exception as e:  # noqa: BLE001
            durumlar.append((k["ad"], f"HATA: {e}"))
        time.sleep(BEKLEME)

    bugun, sonuc, okunan = date.today(), [], 0
    for url, (kaynak, baslik) in adaylar.items():
        metin = ""
        if okunan < MAX_DETAY and not url.lower().endswith((".pdf", ".doc", ".docx", ".zip")):
            try:
                metin = " ".join("".join(sayfa_coz(url).metin).split())
                okunan += 1
                time.sleep(BEKLEME)
            except Exception:  # noqa: BLE001
                pass
        durum, notlar, iller, tarih, t = analiz(baslik, metin)
        if not metin:
            notlar.append("İlan metni okunamadı, sadece başlığa göre değerlendirildi")
        kimlik = hashlib.sha1(url.encode()).hexdigest()
        td = tarih_cevir(tarih) if tarih else None
        kalan = (td - bugun).days if td else None
        r = {"durum": durum, "yeni": kimlik not in gorulen, "kaynak": kaynak, "baslik": baslik,
             "iller": ", ".join(iller), "son_basvuru": tarih, "kalan": kalan,
             "notlar": " | ".join(notlar), "url": url, "_id": kimlik}
        if kalan is not None and kalan < 0:
            r["gecmis"] = True
        if filtreden_gecer(r, t):
            sonuc.append(r)

    # Sıralama: yeni > durum > yaklaşan son tarih; süresi geçenler en alta
    sira = {"Uygun": 0, "Kontrol et": 1, "Uygun değil": 2}
    sonuc.sort(key=lambda r: (r.get("gecmis", False), not r["yeni"], sira[r["durum"]],
                              r["kalan"] if r["kalan"] is not None else 9999))

    rapor_yaz(sonuc, durumlar)
    mesaj = bildirim_hazirla(sonuc)
    if mesaj:
        telegram_gonder(mesaj)

    (KLASOR / "gorulenler.json").write_text(
        json.dumps(sorted(gorulen | {r["_id"] for r in sonuc})), "utf-8")
    print(f"{len(sonuc)} ilan işlendi, {sum(r['yeni'] for r in sonuc)} yeni. Rapor: {KLASOR / 'ilanlar.html'}")


SABLON = """<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kamu İlanları</title>
<style>
body{font-family:system-ui,sans-serif;margin:12px;max-width:1200px}
.k{border:1px solid #ddd;border-radius:10px;padding:10px;margin:8px 0}
.k.b{opacity:.45}.k.g{opacity:.6}
.u{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px}
.d{font-weight:700}.y{background:#0a58ca;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px}
.s{background:#a40e26;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px}
.k a{font-size:16px;font-weight:600;text-decoration:none}.n{font-size:12px;color:#555;margin-top:4px}
.f{margin:8px 0;font-size:14px}
</style>
<h2>Kamu İlanları</h2>
<p>Güncelleme: %%SIMDI%%%%PROFIL%%</p>
<p><i>%%NOT%%</i></p>
<div class="f"><label><input type="checkbox" id="gizle"> Başvurduklarımı gizle</label>
 · <span id="say"></span></div>
<div id="liste">%%KARTLAR%%</div>
<h3>Kaynak durumları</h3><ul>%%DURUM%%</ul>
<p class="n">Otomatik değerlendirme yanılabilir. Başvurmadan önce resmi ilan metnini okuyun.</p>
<script>
var K="basvurulan";
function oku(){try{return JSON.parse(localStorage.getItem(K)||"{}")}catch(e){return {}}}
function yaz(o){try{localStorage.setItem(K,JSON.stringify(o))}catch(e){}}
var cards=document.querySelectorAll(".k"),gz=document.getElementById("gizle");
function ciz(){var o=oku(),n=0;
 cards.forEach(function(c){var b=!!o[c.dataset.id];c.classList.toggle("b",b);
  c.querySelector("input").checked=b;if(b)n++;
  c.style.display=(b&&gz.checked)?"none":"";});
 document.getElementById("say").textContent=n+" ilana başvurdum";}
cards.forEach(function(c){c.querySelector("input").addEventListener("change",function(e){
 var o=oku();if(e.target.checked)o[c.dataset.id]=1;else delete o[c.dataset.id];yaz(o);ciz();});});
gz.addEventListener("change",ciz);ciz();
</script></html>"""


def rapor_yaz(sonuc, durumlar):
    with open(KLASOR / "ilanlar.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Durum", "Yeni", "Kaynak", "Başlık", "İller", "Son başvuru", "Kalan gün", "Notlar", "Bağlantı"])
        for r in sonuc:
            w.writerow([r["durum"], "evet" if r["yeni"] else "", r["kaynak"], r["baslik"], r["iller"],
                        r["son_basvuru"], "" if r["kalan"] is None else r["kalan"], r["notlar"], r["url"]])

    renk = {"Uygun": "#1a7f37", "Kontrol et": "#b7791f", "Uygun değil": "#a40e26"}
    kartlar = []
    for r in sonuc:
        rozet = ""
        if r["yeni"]:
            rozet += " <span class=y>YENİ</span>"
        if r["kalan"] is not None:
            if r["kalan"] < 0:
                rozet += " <span class=s style='background:#777'>süresi geçti</span>"
            elif r["kalan"] <= AYARLAR["uyari_gun"]:
                rozet += f" <span class=s>SON {r['kalan']} GÜN</span>"
        tarih = f"Son başvuru: {html.escape(r['son_basvuru'])}" if r["son_basvuru"] else "Son başvuru: ilanda bakın"
        kartlar.append(
            f"<div class='k' data-id='{r['_id']}'><div class=u>"
            f"<span class=d style='color:{renk[r['durum']]}'>{r['durum']}</span>{rozet}"
            f"<span>{html.escape(r['kaynak'])}</span>"
            f"<span>{html.escape(r['iller']) or 'İl belirtilmemiş'}</span><span>{tarih}</span>"
            f"<label style='margin-left:auto'><input type=checkbox> Başvurdum</label></div>"
            f"<a href='{html.escape(r['url'])}' target=_blank rel=noopener>{html.escape(r['baslik'])}</a>"
            f"<div class=n>{html.escape(r['notlar'])}</div></div>")
    profil = "" if AYARLAR["profil_gizle"] else f" · Profil: KPSS {PROFIL['puan_turu']} {PROFIL['puan']} · {PROFIL['egitim']}"
    dur = "".join(f"<li>{html.escape(a)}: {html.escape(d)}</li>" for a, d in durumlar)
    cikti = (SABLON.replace("%%SIMDI%%", datetime.now().strftime("%d.%m.%Y %H:%M"))
             .replace("%%PROFIL%%", profil).replace("%%NOT%%", html.escape(BASVURU_YERI_NOTU))
             .replace("%%KARTLAR%%", "".join(kartlar) or "<p>Şu an listelenecek ilan bulunamadı.</p>")
             .replace("%%DURUM%%", dur))
    (KLASOR / "ilanlar.html").write_text(cikti, "utf-8")


if __name__ == "__main__":
    try:
        calistir()
    except KeyboardInterrupt:
        sys.exit(1)
