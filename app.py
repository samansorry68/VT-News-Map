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
from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", secrets.token_hex(32))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
# Keep collected news on the server, not in the browser cookie session.
RESULTS = {}
SOURCES = {}

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
    df["عرض"] = pd.to_numeric(df["عرض"], errors="coerce")
    df["طول"] = pd.to_numeric(df["طول"], errors="coerce")
    df = df.dropna(subset=["عرض", "طول"])
    df = df[(df["عرض"].between(24, 40)) & (df["طول"].between(44, 64))]
    if df.empty:
        raise ValueError("هیچ ردیفی با مختصات معتبر ایران پیدا نشد. عرض و طول را بررسی کن.")
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
    return render_template("index.html", results=RESULTS.get(session.get("sid", ""), []),
                           sources=SOURCES.get(session.get("sid", ""), []),
                           keywords=session.get("keywords", ""), username=session.get("username", ADMIN_USER))

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


@app.route("/search", methods=["POST"])
def search_saved_sources():
    if not logged_in():
        return jsonify({"ok": False, "error": "ابتدا وارد سامانه شو."}), 401
    payload = request.get_json(silent=True) or {}
    rows = payload.get("sources", [])
    keywords = str(payload.get("keywords", ""))
    if not isinstance(rows, list) or not rows:
        return jsonify({"ok": False, "error": "فهرست منابع در مرورگر پیدا نشد؛ اکسل را دوباره انتخاب کن."}), 400
    clean_rows = []
    for item in rows[:2000]:
        try:
            lat, lon = float(item.get("عرض", item.get("lat"))), float(item.get("طول", item.get("lon")))
            if not (24 <= lat <= 40 and 44 <= lon <= 64): continue
            clean_rows.append({
                "استان": str(item.get("استان", item.get("province", ""))).strip(),
                "شهرستان": str(item.get("شهرستان", item.get("county", ""))).strip(),
                "عرض": lat, "طول": lon,
                "rss_url": str(item.get("rss_url", "")).strip(),
                "telegram_url": str(item.get("telegram_url", "")).strip()
            })
        except Exception:
            continue
    if not clean_rows:
        return jsonify({"ok": False, "error": "مختصات معتبر در فایل پیدا نشد."}), 400
    found = []
    statuses = []
    keys = [k.strip() for k in keywords.splitlines() if k.strip()]
    for src in clean_rows:
        for field, kind in (("rss_url", "RSS"), ("telegram_url", "تلگرام")):
            url = src[field]
            if not url: continue
            if not url.startswith(("http://", "https://")):
                statuses.append({"place": src["شهرستان"], "kind": kind, "url": url, "message": "نشانی معتبر نیست", "count": 0})
                continue
            try:
                if kind == "RSS":
                    raw = fetch_rss(url, src)
                else:
                    raw = fetch_telegram(url, src)
                matches = [n for n in raw if not keys or any(k.casefold() in (n["title"]+" "+n["text"]).casefold() for k in keys)]
                found.extend(matches)
                message = ("دریافت شد" if raw else "محتوایی دریافت نشد؛ نشانی یا دسترسی منبع را بررسی کن")
                statuses.append({"place": src["شهرستان"], "kind": kind, "url": url, "message": message, "count": len(matches)})
            except Exception as exc:
                statuses.append({"place": src["شهرستان"], "kind": kind, "url": url, "message": "خطا: " + str(exc)[:160], "count": 0})
    session["keywords"] = keywords
    return jsonify({"ok": True, "results": found, "statuses": statuses, "source_count": len(clean_rows)})

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
        sources = []
        for _, row in df.iterrows():
            sources.append({"province": clean(row["استان"]), "county": clean(row["شهرستان"]),
                            "lat": float(row["عرض"]), "lon": float(row["طول"]),
                            "rss_url": clean(row.get("rss_url", "")),
                            "telegram_url": clean(row.get("telegram_url", ""))})
        results = collect(df, keywords)
        SOURCES[session["sid"]] = sources
        RESULTS[session["sid"]] = results
        session["keywords"] = keywords
        flash(f"اکسل با موفقیت خوانده شد: {len(sources)} شهرستان/منبع ثبت شد؛ {len(results)} خبر مطابق کلمات کلیدی پیدا شد.")
    except Exception as e:
        flash("خطا در خواندن اکسل: " + str(e))
    return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
