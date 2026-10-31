# V.T News Map

A Persian, mobile-friendly private dashboard for monitoring RSS feeds and publicly viewable Telegram channel pages using keywords and an Excel source list.

## Deploy on Render
- Root Directory: `VT-News-Map` (this project lives in a subfolder of the Robot-VT repository).
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Environment variables (set privately in Render; never commit secrets):
  - `ADMIN_USER`: your chosen admin username
  - `ADMIN_PASSWORD`: a long private password
  - `APP_SECRET`: a long random secret

## Excel columns
Required: `استان`, `شهرستان`, `عرض`, `طول`
Optional: `rss_url`, `telegram_url`

Download a sample at `/template`.

## Important limitations
- RSS sources are the preferred and most reliable input.
- Telegram collection attempts to read only public `t.me/s/<channel>` pages; Telegram may block or change the page, so this is best-effort and not guaranteed.
- No private Telegram account/session is connected.
- Map pins use the county/province coordinates from the Excel file. They are not verified event coordinates and are labelled as approximate.
- This MVP does not crawl arbitrary news websites. Add their RSS feed URL to the Excel sheet.
- OpenStreetMap tiles and the JavaScript/CSS libraries are loaded from public CDNs, so an internet connection is needed for the map.
