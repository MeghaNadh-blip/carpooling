# Car Pool Manager MongoDB Calendar UI

Python Flask + MongoDB carpooling application styled like the approved Google Calendar reference.

## Features
- Register/Login with name, email, password
- MongoDB database
- Create group
- Add members with gender-based avatars
- Members select Mon-Fri coming days
- Saturday/Sunday holidays
- Fair car duty rotation
- Can't bring car / may not come status
- Automatic reassignment when assigned driver is unavailable
- Alerts and history
- Responsive desktop/mobile UI

## Run locally

```bash
cd ~/Downloads
unzip carpooling-mongodb-calendar-ui.zip
cd carpooling_mongo_calendar_style

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env if needed
python app.py
```

Open: http://127.0.0.1:5000

## MongoDB
Use either local MongoDB:

```env
MONGO_URI=mongodb://localhost:27017/carpooling_calendar
DB_NAME=carpooling_calendar
```

or MongoDB Atlas:

```env
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@cluster0.xxxxx.mongodb.net/carpooling_calendar?retryWrites=true&w=majority
DB_NAME=carpooling_calendar
```

## Email notifications

Add these values in `.env` to send invite and group notification emails:

```env
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=carpoolmanager.notify@gmail.com
MAIL_PASSWORD=your_16_digit_gmail_app_password
MAIL_FROM=carpoolmanager.notify@gmail.com
APP_BASE_URL=http://127.0.0.1:5000
```

For deployed app, replace `APP_BASE_URL` with your public Render/Railway URL.


## Render + MongoDB Atlas notes

Set these environment variables on Render:

```env
PYTHON_VERSION=3.11.9
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@cluster0.xxxxx.mongodb.net/car_pool_manager?retryWrites=true&w=majority&tls=true&appName=Cluster0
DB_NAME=car_pool_manager
MAIL_FROM_NAME=Car Pool Manager
MONGO_TLS_ALLOW_INVALID=false
```

If Render still shows an Atlas TLS handshake error after using Python 3.11.9 and certifi, temporarily test with `MONGO_TLS_ALLOW_INVALID=true` only to confirm it is a certificate/TLS issue. Set it back to `false` for normal use.

Gmail spam note: Gmail may still place new-app emails in Spam because sender reputation is controlled by Gmail. Ask test users to click **Not spam**. For production, use a verified domain email with SPF/DKIM/DMARC.
