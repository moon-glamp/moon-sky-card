
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime, timedelta
import sqlite3, uuid, math, io, os, json
from urllib.parse import quote

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import qrcode

APP_DIR = Path(__file__).parent
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

pdfmetrics.registerFont(TTFont("MoonSans", FONT_REGULAR))
pdfmetrics.registerFont(TTFont("MoonSansBold", FONT_BOLD))
DATA_DIR = APP_DIR / "data"
OUT_DIR = APP_DIR / "generated"
DB_PATH = DATA_DIR / "moon_sky.db"
OUT_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PIN = os.getenv("ADMIN_PIN", "2468")

SETTINGS = {
    "latitude": float(os.getenv("MOON_LAT", "43.565")),
    "longitude": float(os.getenv("MOON_LON", "41.278")),
    "utc_offset": float(os.getenv("MOON_UTC_OFFSET", "3")),
    "place": os.getenv("MOON_PLACE", "Архыз"),
    "brand": "MOON OBSERVATORY",
    "subbrand": "MOON GLAMP",
}

STARS = [
("Сириус",6.752,-16.716,-1.46),("Арктур",14.261,19.182,-0.05),("Вега",18.615,38.784,0.03),
("Капелла",5.279,45.998,0.08),("Ригель",5.243,-8.202,0.13),("Процион",7.655,5.225,0.34),
("Бетельгейзе",5.919,7.407,0.42),("Альтаир",19.846,8.868,0.77),("Альдебаран",4.599,16.509,0.86),
("Антарес",16.490,-26.432,0.96),("Спика",13.420,-11.161,0.98),("Поллукс",7.755,28.026,1.14),
("Фомальгаут",22.961,-29.622,1.16),("Денеб",20.691,45.280,1.25),("Регул",10.139,11.967,1.35),
("Кастор",7.577,31.888,1.58),("Беллатрикс",5.419,6.350,1.64),("Альнилам",5.603,-1.201,1.69),
("Альнитак",5.679,-1.943,1.74),("Мирфак",3.405,49.861,1.79),("Дубхе",11.062,61.751,1.79),
("Алиот",12.900,55.959,1.76),("Мицар",13.399,54.925,2.23),("Алкаид",13.792,49.313,1.86),
("Полярная",2.530,89.264,1.98),("Хамаль",2.119,23.462,2.00),("Мирах",1.162,35.621,2.05),
("Эниф",21.736,9.875,2.39),("Рас Альхаге",17.582,12.560,2.08),("Эльтанин",17.943,51.489,2.24),
("Кохаб",14.845,74.156,2.08),("Каф",0.153,59.150,2.28),("Шедар",0.675,56.537,2.24),
("Маркаб",23.079,15.205,2.49),("Альферац",0.139,29.090,2.06),("Альбирео",19.512,27.960,3.05),
("Унукальхай",15.737,6.426,2.63),("Сабик",17.173,-15.724,2.43)
]

app = FastAPI(title="Moon Sky Card v2")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        guest_name TEXT,
        visit_date TEXT NOT NULL,
        visit_time TEXT NOT NULL,
        observed_objects TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        pdf_filename TEXT
    )
    """)
    con.commit()
    con.close()

init_db()

def julian_date(dt_utc):
    y,m=dt_utc.year,dt_utc.month
    d=dt_utc.day+(dt_utc.hour+dt_utc.minute/60+dt_utc.second/3600)/24
    if m<=2: y-=1; m+=12
    A=y//100
    B=2-A+A//4
    return int(365.25*(y+4716))+int(30.6001*(m+1))+d+B-1524.5

def gmst_hours(dt_utc):
    D=julian_date(dt_utc)-2451545.0
    return (18.697374558+24.06570982441908*D)%24

def altaz(ra_h, dec_deg, dt_local):
    dt_utc=dt_local-timedelta(hours=SETTINGS["utc_offset"])
    lst=(gmst_hours(dt_utc)+SETTINGS["longitude"]/15.0)%24
    H=math.radians((lst-ra_h)*15)
    dec=math.radians(dec_deg); lat=math.radians(SETTINGS["latitude"])
    sin_alt=math.sin(dec)*math.sin(lat)+math.cos(dec)*math.cos(lat)*math.cos(H)
    alt=math.asin(max(-1,min(1,sin_alt)))
    y=-math.sin(H)*math.cos(dec)
    x=math.sin(dec)*math.cos(lat)-math.cos(dec)*math.sin(lat)*math.cos(H)
    az=math.atan2(y,x)
    return math.degrees(alt), math.degrees(az)%360

def sky_xy(alt, az, cx, cy, radius):
    r=radius*(90-alt)/90
    a=math.radians(az)
    return cx-r*math.sin(a), cy+r*math.cos(a)

def qr_bytes(url):
    qr=qrcode.QRCode(version=4,box_size=8,border=1)
    qr.add_data(url); qr.make(fit=True)
    img=qr.make_image(fill_color="#202328",back_color="#FAF8F2")
    b=io.BytesIO(); img.save(b,format="PNG"); b.seek(0); return b

def create_pdf(card_id, guest_name, visit_date, visit_time, objects, notes=""):
    dt = datetime.strptime(f"{visit_date} {visit_time}", "%Y-%m-%d %H:%M")
    path = OUT_DIR / f"MoonSky_{card_id}.pdf"

    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4

    ivory = HexColor("#FAF8F2")
    charcoal = HexColor("#313337")
    gold = HexColor("#BFA46F")
    night = HexColor("#171C24")
    sand = HexColor("#E9DFC9")
    soft = HexColor("#77766F")
    starwhite = HexColor("#FFFDF6")

    # Фон
    c.setFillColor(ivory)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # -----------------------------
    # ФИРМЕННЫЙ ЗНАК: ЛУНА + ГОРЫ
    # -----------------------------
    logo_y = H - 20 * mm

    c.setFillColor(gold)
    c.circle(W / 2, logo_y, 7 * mm, fill=1, stroke=0)

    # вырез серпа
    c.setFillColor(ivory)
    c.circle(W / 2 + 3 * mm, logo_y + 1 * mm, 7 * mm, fill=1, stroke=0)

    # горы
    c.setStrokeColor(charcoal)
    c.setLineWidth(1.1)
    c.line(W / 2 - 8 * mm, logo_y - 5 * mm,
           W / 2 - 2 * mm, logo_y + 1 * mm)
    c.line(W / 2 - 2 * mm, logo_y + 1 * mm,
           W / 2 + 3 * mm, logo_y - 4 * mm)
    c.line(W / 2 + 1 * mm, logo_y - 2 * mm,
           W / 2 + 5 * mm, logo_y + 1 * mm)
    c.line(W / 2 + 5 * mm, logo_y + 1 * mm,
           W / 2 + 9 * mm, logo_y - 5 * mm)

    # -----------------------------
    # ЗАГОЛОВОК
    # -----------------------------
    c.setFillColor(charcoal)
    c.setFont("MoonSans", 13)
    c.drawCentredString(
        W / 2,
        H - 33 * mm,
        "M O O N   O B S E R V A T O R Y"
    )

    c.setStrokeColor(gold)
    c.setLineWidth(0.5)
    c.line(W / 2 - 22 * mm, H - 38 * mm,
           W / 2 - 4 * mm, H - 38 * mm)
    c.line(W / 2 + 4 * mm, H - 38 * mm,
           W / 2 + 22 * mm, H - 38 * mm)

    c.setFillColor(gold)
    c.circle(W / 2, H - 38 * mm, 1.1 * mm, fill=1, stroke=0)

    c.setFillColor(charcoal)
    c.setFont("MoonSansBold", 23)
    c.drawCentredString(
        W / 2,
        H - 51 * mm,
        "НЕБО ВАШЕЙ НОЧИ"
    )

    # Дата / время / место
    c.setFont("MoonSans", 9.5)
    c.setFillColor(charcoal)

    meta = (
        f"{dt.strftime('%d.%m.%Y')}   •   "
        f"{visit_time}   •   {SETTINGS['place']}"
    )

    c.drawCentredString(
        W / 2,
        H - 59 * mm,
        meta
    )

    # -----------------------------
    # КАРТА НЕБА
    # -----------------------------
    cx = W / 2
    cy = H - 137 * mm
    R = 68 * mm

    c.setFillColor(night)
    c.circle(cx, cy, R, fill=1, stroke=0)

    # золотая рамка
    c.setStrokeColor(gold)
    c.setLineWidth(0.65)
    c.circle(cx, cy, R, fill=0, stroke=1)

    # внутренние круги
    c.setStrokeColor(HexColor("#404650"))
    c.setLineWidth(0.25)

    for f in (0.33, 0.66):
        c.circle(cx, cy, R * f, fill=0, stroke=1)

    # направления
    c.setFillColor(gold)
    c.setFont("MoonSansBold", 9)

    c.drawCentredString(cx, cy + R + 4 * mm, "N")
    c.drawCentredString(cx, cy - R - 6 * mm, "S")
    c.drawCentredString(cx - R - 4 * mm, cy - 2, "E")
    c.drawCentredString(cx + R + 4 * mm, cy - 2, "W")

    # звезды
    visible = []

    for nm, ra, dec, mag in STARS:
        alt, az = altaz(ra, dec, dt)

        if alt > 0:
            x, y = sky_xy(alt, az, cx, cy, R)

            visible.append((nm, x, y, mag))

            size = max(0.8, 3.3 - mag * 0.55)

            # лёгкое сияние для ярких звёзд
            if mag < 1.5:
                c.setFillColor(HexColor("#D9D2C5"))
                c.circle(x, y, size + 1.8, fill=1, stroke=0)

            c.setFillColor(starwhite)
            c.circle(x, y, size, fill=1, stroke=0)

    # подписи наиболее ярких звёзд
    
for nm, x, y, mag in sorted(visible, key=lambda z: z[3])[:11]:
    c.setFillColor(starwhite)
    c.setFont("MoonSans", 6.2)

    if x < cx:
        label_x = max(x - 4.5, cx - R + 18)
        c.drawRightString(label_x, y + 2.0, nm)
    else:
        label_x = min(x + 4.5, cx + R - 18)
        c.drawString(label_x, y + 2.0, nm)
    # -----------------------------
    # ЧТО НАБЛЮДАЛИ
    # -----------------------------
    info_y = 58 * mm

    c.setFillColor(charcoal)
    c.setFont("MoonSansBold", 7.5)
    c.drawCentredString(
        W / 2,
        info_y + 17 * mm,
        "С Е Г О Д Н Я   В Ы   Н А Б Л Ю Д А Л И"
    )

    observed = " • ".join(objects) if objects else "Звёздное небо Архыза"

    c.setFillColor(gold)

    obj_font_size = 16
    if len(observed) > 35:
        obj_font_size = 12

    c.setFont("MoonSans", obj_font_size)
    c.drawCentredString(
        W / 2,
        info_y + 8 * mm,
        observed
    )

    # декоративная линия
    c.setStrokeColor(gold)
    c.setLineWidth(0.5)
    c.line(W / 2 - 35 * mm, info_y + 3 * mm,
           W / 2 - 5 * mm, info_y + 3 * mm)
    c.line(W / 2 + 5 * mm, info_y + 3 * mm,
           W / 2 + 35 * mm, info_y + 3 * mm)

    c.setFillColor(gold)
    c.circle(W / 2, info_y + 3 * mm, 1 * mm, fill=1, stroke=0)

    # -----------------------------
    # ПЕРСОНАЛИЗАЦИЯ
    # -----------------------------
    personal = (
        f"{guest_name}, сохраните эту ночь."
        if guest_name and guest_name.strip() and guest_name.strip().upper() not in ("NA", "N/A", "NONE")
        else "Сохраните эту ночь."
    )

    c.setFillColor(charcoal)
    c.setFont("MoonSans", 11)
    c.drawCentredString(
        W / 2,
        info_y - 4 * mm,
        personal
    )

    c.setFillColor(soft)
    c.setFont("MoonSans", 6.8)

    if notes and notes.strip().lower() not in ("test", "тест", "-", "none"):
        c.drawCentredString(
            W / 2,
            info_y - 10 * mm,
            notes[:100]
        )
    else:
        c.drawCentredString(
            W / 2,
            info_y - 10 * mm,
            "Сохраните эту ночь среди звёзд."
        )

    # -----------------------------
    # QR-КОД
    # -----------------------------
    public_url = f"{BASE_URL}/sky/{card_id}"
    qr = qr_bytes(public_url)

    qr_x = 15 * mm
    qr_y = 10 * mm
    qr_size = 25 * mm

    # рамка QR
    c.setStrokeColor(gold)
    c.setLineWidth(0.7)
    c.roundRect(
        qr_x - 2 * mm,
        qr_y - 2 * mm,
        qr_size + 4 * mm,
        qr_size + 4 * mm,
        2 * mm,
        fill=0,
        stroke=1
    )

    c.drawImage(
        ImageReader(qr),
        qr_x,
        qr_y,
        qr_size,
        qr_size,
        mask="auto"
    )

    c.setFillColor(charcoal)
    c.setFont("MoonSansBold", 6.5)
    c.drawString(
        qr_x + qr_size + 6 * mm,
        qr_y + 17 * mm,
        "ОТКРОЙТЕ ЦИФРОВУЮ"
    )
    c.drawString(
        qr_x + qr_size + 6 * mm,
        qr_y + 13 * mm,
        "ВЕРСИЮ КАРТЫ"
    )

    c.setFillColor(soft)
    c.setFont("MoonSans", 5.5)
    c.drawString(
        qr_x + qr_size + 6 * mm,
        qr_y + 7 * mm,
        "Наведите камеру на QR-код"
    )
    c.drawString(
        qr_x + qr_size + 6 * mm,
        qr_y + 4 * mm,
        "и сохраните эту ночь."
    )

    # -----------------------------
    # MOON GLAMP ВНИЗУ
    # -----------------------------
    c.setFillColor(charcoal)
    c.setFont("MoonSans", 9)
    c.drawCentredString(
        W / 2,
        17 * mm,
        "M O O N   G L A M P"
    )

    c.setFillColor(gold)
    c.setFont("MoonSans", 6)
    c.drawCentredString(
        W / 2,
        12.5 * mm,
        "A R K H Y Z"
    )

    # ID карты
    c.setFillColor(gold)
    c.circle(W - 38 * mm, 18 * mm, 1.2 * mm, fill=1, stroke=0)

    c.setFillColor(charcoal)
    c.setFont("MoonSansBold", 5.5)
    c.drawString(
        W - 34 * mm,
        18 * mm,
        "ID КАРТЫ"
    )

    c.setFillColor(soft)
    c.setFont("MoonSans", 5.5)
    c.drawString(
        W - 34 * mm,
        13.5 * mm,
        card_id
    )

    c.save()
    return path

def layout(title, body):
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--iv:#faf8f2;--ch:#414244;--go:#c4aa78;--sa:#e7ddc9;--ni:#202328;--sg:#77796e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--iv);color:var(--ch);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}
.wrap{{max-width:1080px;margin:28px auto;padding:0 18px}}.top{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--sa);padding:12px 0 18px}}
.logo{{letter-spacing:7px;font-size:27px}}.sub{{letter-spacing:4px;color:var(--go);font-size:10px;margin-top:5px}}
.card{{background:white;border:1px solid var(--sa);border-radius:18px;padding:24px;margin-top:22px}}
input,textarea{{width:100%;padding:11px;border:1px solid var(--sa);border-radius:10px;font-size:15px}}label{{display:block;font-size:12px;color:var(--sg);margin:12px 0 5px}}
button,.btn{{display:inline-block;background:var(--go);color:white;padding:12px 16px;border-radius:10px;text-decoration:none;border:0;font-weight:700;cursor:pointer}}
.btn.dark{{background:var(--ni)}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid var(--sa);text-align:left;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.checks{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.checks label{{background:var(--iv);padding:9px;border-radius:9px;color:var(--ch);margin:0}}
.hero{{text-align:center;padding:20px 0}}.hero h1{{font-family:Georgia,serif;font-size:34px;font-weight:400;margin:8px}}
.meta{{color:var(--go);letter-spacing:2px;font-size:12px}}.objects{{text-align:center;font-size:18px;margin:24px 0}}
.small{{font-size:12px;color:var(--sg);line-height:1.5}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.logo{{font-size:20px;letter-spacing:5px}}}}
</style></head><body><div class="wrap"><div class="top"><div><div class="logo">MOON</div><div class="sub">OBSERVATORY • MOON GLAMP</div></div><div>SKY CARD</div></div>{body}</div></body></html>"""

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/admin")

@app.get("/admin", response_class=HTMLResponse)
def admin(pin: str = ""):
    if pin != ADMIN_PIN:
        return layout("Moon Sky Card — вход",f"""
        <div class="card" style="max-width:430px;margin:60px auto">
        <h2>Вход сотрудника</h2>
        <form><label>PIN</label><input type="password" name="pin" autofocus>
        <button style="margin-top:16px;width:100%">ВОЙТИ</button></form>
        <p class="small">PIN меняется через переменную ADMIN_PIN. По умолчанию: 2468.</p></div>""")
    con=db()
    rows=con.execute("SELECT * FROM cards ORDER BY created_at DESC LIMIT 100").fetchall()
    con.close()
    trs="".join([f"<tr><td>{r['visit_date']} {r['visit_time']}</td><td>{r['guest_name'] or '—'}</td><td>{r['observed_objects']}</td><td><a href='/sky/{r['id']}'>Открыть</a></td></tr>" for r in rows])
    return layout("Moon Sky Card — админ",f"""
    <div class="grid">
      <div class="card"><h2>Создать карту</h2>
      <form action="/admin/create?pin={quote(pin)}" method="post">
      <label>Имя гостя</label><input name="guest_name">
      <label>Дата</label><input name="visit_date" type="date" required>
      <label>Время</label><input name="visit_time" type="time" required>
      <label>Что наблюдали</label><div class="checks">
      {''.join([f'<label><input type="checkbox" name="objects" value="{x}"> {x}</label>' for x in ["Луна","Сатурн","Юпитер","Марс","Млечный Путь","Созвездия"]])}
      </div>
      <label>Заметка</label><textarea name="notes" rows="3" placeholder="Например: отличная прозрачность атмосферы"></textarea>
      <button style="margin-top:16px;width:100%">СОЗДАТЬ КАРТУ</button></form></div>
      <div class="card"><h2>Как работает</h2><p class="small">После создания сервис выдаёт уникальный ID, сохраняет карту в SQLite, формирует PDF и создаёт публичную ссылку вида <b>{BASE_URL}/sky/ID</b>. QR-код в PDF ведёт на эту страницу.</p>
      <p class="small">Для реальных QR на телефонах BASE_URL должен быть публичным адресом, например <b>https://sky.moonglamp.ru</b>.</p></div>
    </div>
    <div class="card"><h2>Последние карты</h2><table><tr><th>Дата</th><th>Гость</th><th>Наблюдали</th><th></th></tr>{trs or '<tr><td colspan=4>Пока нет карт</td></tr>'}</table></div>""")

@app.post("/admin/create")
def create_card(
    pin: str,
    guest_name: str = Form(""),
    visit_date: str = Form(...),
    visit_time: str = Form(...),
    objects: list[str] = Form(default=[]),
    notes: str = Form("")
):
    if pin != ADMIN_PIN: raise HTTPException(403,"Wrong PIN")
    card_id=uuid.uuid4().hex[:12].upper()
    pdf=create_pdf(card_id,guest_name.strip(),visit_date,visit_time,objects,notes.strip())
    con=db()
    con.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)",
        (card_id,guest_name.strip(),visit_date,visit_time,json.dumps(objects,ensure_ascii=False),notes.strip(),datetime.now().isoformat(timespec="seconds"),pdf.name))
    con.commit(); con.close()
    return RedirectResponse(f"/sky/{card_id}",status_code=303)

@app.get("/sky/{card_id}", response_class=HTMLResponse)
def public_card(card_id: str):
    con=db()
    r=con.execute("SELECT * FROM cards WHERE id=?",(card_id.upper(),)).fetchone()
    con.close()
    if not r: raise HTTPException(404,"Card not found")
    objects=json.loads(r["observed_objects"] or "[]")
    name=r["guest_name"] or "Гость Moon"
    return layout("Moon Observatory — ваша карта",f"""
    <div class="card hero">
      <div class="meta">{r['visit_date']} • {r['visit_time']} • {SETTINGS['place']}</div>
      <h1>Небо вашей ночи</h1>
      <p>{name}</p>
      <div class="objects">{' • '.join(objects) if objects else 'звёздное небо Архыза'}</div>
      {f"<p class='small'>{r['notes']}</p>" if r['notes'] else ""}
      <p><a class="btn" href="/sky/{r['id']}/pdf">СКАЧАТЬ PDF</a></p>
      <p class="small">ID карты: {r['id']}</p>
    </div>""")

@app.get("/sky/{card_id}/pdf")
def get_pdf(card_id: str):
    con=db()
    r=con.execute("SELECT * FROM cards WHERE id=?",(card_id.upper(),)).fetchone()
    con.close()
    if not r: raise HTTPException(404,"Card not found")
    path=OUT_DIR/r["pdf_filename"]
    if not path.exists(): raise HTTPException(404,"PDF missing")
    return FileResponse(path,media_type="application/pdf",filename=path.name)

@app.get("/health")
def health():
    return {"ok":True,"base_url":BASE_URL}

if __name__ == "__main__":
    import uvicorn
    print(f"Moon Sky Card v2: {BASE_URL}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))
