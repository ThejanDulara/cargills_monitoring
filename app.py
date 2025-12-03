import os
from urllib.parse import urlparse
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template, request, redirect, url_for
from flask import flash
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# -------------------------------------------------
# Load environment variables
# -------------------------------------------------
load_dotenv()

API_KEY = os.getenv("API_KEY")
CX = os.getenv("CSE_CX")
GOOGLE_URL = "https://www.googleapis.com/customsearch/v1"

DATABASE_URL = os.getenv("DATABASE_URL")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
TZ_NAME = os.getenv("TZ", "Asia/Colombo")
TZ = pytz.timezone(TZ_NAME)

# -------------------------------------------------
# Flask app
# -------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")  # change in prod

# -------------------------------------------------
# Database setup (SQLAlchemy)
# -------------------------------------------------
Base = declarative_base()

class PressArticle(Base):
    __tablename__ = "press_articles"

    id = Column(Integer, primary_key=True)
    newspaper = Column(String(100))
    language = Column(String(20))
    title = Column(Text)
    url = Column(Text, unique=True, index=True)
    snippet = Column(Text)
    query_used = Column(String(255))
    publish_date = Column(Text)   # stored as raw text (e.g. 2024-12-01T10:00:00+05:30)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create engine & session
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# -------------------------------------------------
# Newspaper mapping & queries (from your script)
# -------------------------------------------------
NEWS_MAP = {
    "dailymirror.lk": "Daily Mirror",
    "ft.lk": "Daily FT",
    "sundayobserver.lk": "Sunday Observer",
    "sundaytimes.lk": "Sunday Times",
    "ceylontoday.lk": "Ceylon Today",
    "themorning.lk": "The Morning",
    "dailynews.lk": "Daily News",
    "island.lk": "The Island",
    "lankadeepa.lk": "Lanka Deepa",
    "mawbima.lk": "Maubima",
    "ada.lk": "Ada",
    "aruna.lk": "Aruna",
    "divaina.lk": "Divaina",
    "dinamina.lk": "Dinamina"
}

ENGLISH_QUERIES = ['"cargills"', '"cargils"']
SINHALA_QUERIES = ['"කාගිල්ස්"', '"කාගීල්ස්"']

SINHALA_DOMAINS = [
    "lankadeepa.lk",
    "mawbima.lk",
    "ada.lk",
    "aruna.lk",
    "divaina.lk",
    "dinamina.lk"
]

def get_newspaper_name(url):
    domain = urlparse(url).netloc.replace("www.", "")
    for key, name in NEWS_MAP.items():
        if key in domain:
            return name
    return "Unknown"

def get_language(url):
    domain = urlparse(url).netloc.replace("www.", "")
    for d in SINHALA_DOMAINS:
        if d in domain:
            return "Sinhala"
    return "English"

def google_search(query, site):
    """Run a site-specific CSE query (up to 100 results)."""
    results = []
    start = 1

    while True:
        params = {
            "q": f"{query} site:{site}",
            "key": API_KEY,
            "cx": CX,
            "start": start
        }

        r = requests.get(GOOGLE_URL, params=params)
        data = r.json()

        if "items" not in data:
            break

        results.extend(data["items"])

        if len(data["items"]) < 10:
            break

        start += 10

    return results

# -------------------------------------------------
# Email
# -------------------------------------------------
def send_email(articles, subject):
    if not EMAIL_USER or not EMAIL_PASS:
        print("Email credentials not configured, skipping email.")
        return

    if not articles:
        print("No articles to send in email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER  # send to yourself as requested

    # Build HTML body
    html_rows = ""
    for a in articles:
        pub = a.publish_date or "Unknown"
        html_rows += f"""
        <tr>
          <td>{a.newspaper}</td>
          <td>{a.language}</td>
          <td>{a.title}</td>
          <td>{pub}</td>
          <td><a href="{a.url}">{a.url}</a></td>
        </tr>
        """

    html_body = f"""
    <html>
      <body>
        <p>Hi Thejan,<br><br>
           Here are the new Cargills-related articles.<br><br>
        </p>
        <table border="1" cellpadding="5" cellspacing="0">
          <tr>
            <th>Newspaper</th>
            <th>Language</th>
            <th>Title</th>
            <th>Publish Date</th>
            <th>URL</th>
          </tr>
          {html_rows}
        </table>
        <br>
        <p>Press monitoring bot.</p>
      </body>
    </html>
    """

    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print("Email sent successfully.")
    except Exception as e:
        print("Error sending email:", e)

# -------------------------------------------------
# Core scan function (used by manual trigger & daily job)
# -------------------------------------------------
def run_scan_and_save(send_email_immediate=False):
    session = SessionLocal()
    new_articles = []

    try:
        for domain in NEWS_MAP.keys():
            print(f"Scanning {domain}...")
            queries = SINHALA_QUERIES if domain in SINHALA_DOMAINS else ENGLISH_QUERIES

            for query in queries:
                print(f"  Using query {query}")
                items = google_search(query, domain)

                for item in items:
                    url = item.get("link", "")
                    if not url:
                        continue

                    # Check if already in DB
                    existing = session.query(PressArticle).filter_by(url=url).first()
                    if existing:
                        continue

                    publish_date = (
                        item.get("pagemap", {})
                            .get("metatags", [{}])[0]
                            .get("article:published_time", "Unknown")
                    )

                    article = PressArticle(
                        newspaper=get_newspaper_name(url),
                        language=get_language(url),
                        title=item.get("title"),
                        url=url,
                        snippet=item.get("snippet"),
                        query_used=query,
                        publish_date=publish_date
                    )

                    session.add(article)
                    new_articles.append(article)

        # Commit inserts
        session.commit()

        if send_email_immediate and new_articles:
            send_email(new_articles, subject="Cargills Press Monitoring – Manual Trigger")

    except Exception as e:
        session.rollback()
        print("Error during scan:", e)
    finally:
        session.close()

    return new_articles

# -------------------------------------------------
# Daily scheduled job at 10:00 AM Sri Lanka time
# -------------------------------------------------
def daily_job():
    # 1) Run scan once
    run_scan_and_save(send_email_immediate=False)

    # 2) Send email with articles found in last 24 hours
    session = SessionLocal()
    try:
        now = datetime.now(TZ)
        since = now - timedelta(days=1)

        # created_at is stored in UTC by default; convert to naive UTC for comparison
        # simplest: just compare naive UTC datetimes assuming server time ~ UTC
        # For precise behavior, you'd store times with tz properly; this is OK for now.
        # Here we treat created_at as UTC, and since_utc as utcnow - 1 day:
        now_utc = datetime.utcnow()
        since_utc = now_utc - timedelta(days=1)

        recent_articles = (
            session.query(PressArticle)
            .filter(PressArticle.created_at >= since_utc,
                    PressArticle.created_at <= now_utc)
            .order_by(PressArticle.created_at.desc())
            .all()
        )

        if recent_articles:
            send_email(
                recent_articles,
                subject="Cargills Press Monitoring – Daily Report (Last 24 hours)"
            )
        else:
            print("No new articles in last 24 hours; skipping daily email.")
    finally:
        session.close()

# Scheduler
scheduler = BackgroundScheduler(timezone=TZ_NAME)
scheduler.add_job(daily_job, "cron", hour=10, minute=0)  # 10:00 AM Sri Lanka time
scheduler.start()

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    session = SessionLocal()

    # Filters from query params
    language = request.args.get("language", "").strip()
    newspaper = request.args.get("newspaper", "").strip()
    publish_date = request.args.get("publish_date", "").strip()  # expected: YYYY-MM-DD

    try:
        query = session.query(PressArticle)

        if language:
            query = query.filter(PressArticle.language == language)
        if newspaper:
            query = query.filter(PressArticle.newspaper == newspaper)
        if publish_date:
            # publish_date stored as "YYYY-MM-DD..." string, so prefix match
            like_pattern = f"{publish_date}%"
            query = query.filter(PressArticle.publish_date.like(like_pattern))

        articles = query.order_by(PressArticle.id.desc()).all()
    finally:
        session.close()

    # No "new_articles" by default (only after manual run)
    return render_template(
        "index.html",
        new_articles=[],
        articles=articles,
        filters={
            "language": language,
            "newspaper": newspaper,
            "publish_date": publish_date
        },
        newspapers_sorted=sorted(set(NEWS_MAP.values()))
    )

@app.route("/run-scan", methods=["POST"])
def run_scan():
    # Run scan and get list of new articles
    new_articles = run_scan_and_save(send_email_immediate=True)

    # After scan, reload full list (no filters) for bottom section
    session = SessionLocal()
    try:
        articles = (
            session.query(PressArticle)
            .order_by(PressArticle.id.desc())
            .all()
        )
    finally:
        session.close()

    flash(f"Scan completed. {len(new_articles)} new articles found.")
    return render_template(
        "index.html",
        new_articles=new_articles,
        articles=articles,
        filters={
            "language": "",
            "newspaper": "",
            "publish_date": ""
        },
        newspapers_sorted=sorted(set(NEWS_MAP.values()))
    )

# -------------------------------------------------
# Entry point
# -------------------------------------------------
if __name__ == "__main__":
    # For local development
    app.run(host="0.0.0.0", port=5000, debug=True)
