import os
import io
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", secrets.token_hex(32))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
# Keep collected news on the server, not in the browser cookie session.
RESULTS = {}

REQUIRED = ["استان", "شهرستان", "عرض", "طول"]
OPTIONAL = ["rss_url", "telegram_url"]

def logged_in():
    return session.get("logged_in") is True

def clean(v):
    return str(v).strip() if pd.notna(v) else ""

def load_sources(file):
    df = pd.read_excel(file)
    # Accept Persian or English header aliases.
    aliases = {"province":"استان", "county":"شهرستان", "latitude":"عرض", "longitude":"طول",
               "rss":"rss_url", "telegram":"telegram_url"}
    df = df.rename(columns={c: aliases.get(str(c).strip().lower(), str(c).strip()) for c in df.columns})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("ستون‌های ضروری اکسل موجود نیست: " + "، ".join(missing))
    for c in OPTIONAL:
        if c not in df.columns: df[c] = ""
    return df

def fetch_rss(url, source):
    items = []
    if not url.startswith(("http://", "https://")): return items
    feed = feedparser.parse(url)
    for entry in feed.entries[:100]:
        title = clean(entry.get("title", ""))
        summary = BeautifulSoup(clean(entry.get("summary", entry.get("description", ""))), "html.parser").get_text(" ", strip=True)
        link = clean(entry.get("link", url))
        published = clean(entry.get("published", entry.get("updated", "")))
        items.append({"title": title, "text": summary, "url": link, "date": published,
                      "province": source["استان"], "county": source["شهرستان"],
                      "lat": float(source["عرض"]), "lon": float(source["طول"]),
                      "source": urlparse(url).netloc, "kind": "RSS", "location_quality": "مرکز شهرستان/استان"})
    return items

def fetch_telegram(url, source):
    # Reads only publicly viewable t.me/s/<channel> pages. Telegram may block or limit requests.
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", url)
    if not m: return []
    channel = m.group(1)
    page = "https://t.me/s/" + channel
    try:
        res = requests.get(page, timeout=12, headers={"User-Agent":"Mozilla/5.0"})
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        out = []
        for node in soup.select(".tgme_widget_message")[-100:]:
            body = node.select_one(".tgme_widget_message_text")
            if not body: continue
            text = body.get_text(" ", strip=True)
            anchor = node.select_one("a.tgme_widget_message_date")
            href = anchor.get("href") if anchor else page
            date_node = node.select_one("time")
            date = date_node.get("datetime", "") if date_node else ""
            out.append({"title": text[:120], "text": text, "url": href, "date": date,
                        "province": source["استان"], "county": source["شهرستان"],
                        "lat": float(source["عرض"]), "lon": float(source["طول"]),
                        "source": "@"+channel, "kind": "Telegram", "location_quality": "مرکز شهرستان/استان"})
        return out
    except Exception:
        return []

def collect(df, keywords):
    results = []
    for _, row in df.iterrows():
        src = {k: clean(row.get(k, "")) for k in ["استان","شهرستان","rss_url","telegram_url"]}
        try:
            src["عرض"], src["طول"] = float(row["عرض"]), float(row["طول"])
        except Exception:
            continue
        if src["rss_url"]: results.extend(fetch_rss(src["rss_url"], src))
        if src["telegram_url"]: results.extend(fetch_telegram(src["telegram_url"], src))
    keys = [k.strip() for k in keywords.splitlines() if k.strip()]
    if keys:
        results = [x for x in results if any(k.casefold() in (x["title"]+" "+x["text"]).casefold() for k in keys)]
    return results

@app.route("/", methods=["GET"])
def home():
    if not logged_in(): return redirect(url_for("login"))
    return render_template("index.html", results=RESULTS.get(session.get("sid", ""), []), keywords=session.get("keywords", ""),
                           username=session.get("username", ADMIN_USER))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            flash("مدیر سامانه هنوز ADMIN_PASSWORD را در تنظیمات هاست تعیین نکرده است.")
        elif request.form.get("username","") == ADMIN_USER and secrets.compare_digest(request.form.get("password",""), ADMIN_PASSWORD):
            session.clear(); session["logged_in"] = True; session["username"] = ADMIN_USER; session["sid"] = secrets.token_urlsafe(18)
            return redirect(url_for("home"))
        else: flash("نام کاربری یا رمز عبور اشتباه است.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/template")
def template_file():
    df = pd.DataFrame([{"استان":"تهران","شهرستان":"تهران","عرض":35.6892,"طول":51.3890,
                        "rss_url":"https://example.com/rss.xml","telegram_url":"https://t.me/s/example"}])
    data = io.BytesIO()
    with pd.ExcelWriter(data, engine="openpyxl") as writer: df.to_excel(writer, index=False)
    data.seek(0)
    return send_file(data, as_attachment=True, download_name="VT_News_Map_Template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/scan", methods=["POST"])
def scan():
    if not logged_in(): return redirect(url_for("login"))
    file = request.files.get("excel")
    keywords = request.form.get("keywords","")
    if not file or not file.filename.lower().endswith((".xlsx",".xls")):
        flash("لطفاً فایل اکسل معتبر انتخاب کن.")
        return redirect(url_for("home"))
    try:
        df = load_sources(file)
        results = collect(df, keywords)
        RESULTS[session["sid"]] = results
        session["keywords"] = keywords
        flash(f"بررسی تمام شد؛ {len(results)} مورد مطابق کلمات کلیدی پیدا شد.")
    except Exception as e:
        flash("خطا در خواندن اکسل: " + str(e))
    return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
