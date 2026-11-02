# GoDaddy cPanel Deployment Guide for VisaBot Server

Ye guide aapko batayegi ki aap apne **GoDaddy Economy Web Hosting (cPanel)** par VisaBot Server Dashboard & REST API kaise deploy karein.

---

## Step 1: Files Upload Karein

1. GoDaddy me **cPanel** login karein.
2. **File Manager** open karein.
3. Apne domain ya subdomain ke root folder me jayein (jaise `public_html/visabot` ya ek alag subdomain `hub.yourdomain.com`).
4. Is `server/` folder ki sari files zip karke wahan upload karein aur extract karein:
   - `app.py`
   - `config.py`
   - `database.py`
   - `passenger_wsgi.py`
   - `requirements.txt`
   - `templates/`
   - `static/`

---

## Step 2: cPanel me "Setup Python App" Configure Karein

1. cPanel dashboard me jayein aur search karein: **"Setup Python App"**.
2. **Create Application** button par click karein.
3. Form me details fill karein:
   - **Python Version**: Select `3.10` ya `3.11` (jo bhi latest available ho).
   - **Application root**: Path jahan aapne files rakhi hain (e.g. `visabot` ya `public_html/visabot`).
   - **Application URL**: Wo domain ya URL jahan aap dashboard open karna chahte hain (e.g. `yourdomain.com` ya `yourdomain.com/visabot`).
   - **Application startup file**: `passenger_wsgi.py`
   - **Application Entry point**: `application`
4. **Create** button dabayein.

---

## Step 3: Dependencies Install Karein (`requirements.txt`)

1. App create hone ke baad page ke upar ek command dikhegi:
   `source /home/youruser/virtualenv/.../bin/activate`
2. Page par niche scroll karein aur **Configuration files** section me `requirements.txt` enter karke **Run Pip Install** par click karein.
   *(Ya cPanel Terminal me jaakar upar wali virtualenv command paste karein aur run karein: `pip install -r requirements.txt`)*

---

## Step 4: Environment Variables (Optional)

cPanel Python App page par niche **Environment Variables** add kar sakte hain:
- `SECRET_KEY` = Koi bhi strong secret string
- `API_KEY` = `visabot-secure-api-key-2026` (Desktop app ke saath connect karne ke liye)
- `ADMIN_USERNAME` = `admin`
- `ADMIN_PASSWORD` = Aapka custom password

---

## Step 5: Restart & Test Karein

1. Page ke top par **Restart** button dabayein.
2. Apne browser me Application URL open karein (e.g. `https://yourdomain.com/visabot`).
3. Aapko sleek VisaBot Control Hub Dashboard dikhega!
4. Default credentials:
   - **Username**: `admin`
   - **Password**: `admin123` (ya jo aapne `.env` me set kiya)

Ab aapka GoDaddy Server Account Management aur Slot Trigger Webhook ke liye 24/7 live ready h!
