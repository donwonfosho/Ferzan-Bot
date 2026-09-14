# Deploying the Launch Bot (Full Stack)

Three separate pieces run on your droplet, alongside your existing bots:

1. **`launch_bot.py`** — the Telegram bot process (polling, like your others)
2. **`api.py`** — a FastAPI backend (uvicorn), separate process, separate systemd service
3. **`miniapp/*.html`** — static files, served over **HTTPS** (Telegram requires this for Mini Apps — a plain-http `web_app` URL is rejected outright)

## Why HTTPS is non-negotiable here

Every other bot in this project could run on plain HTTP internally since
users only ever interact via Telegram's own chat, which is already
encrypted end-to-end at the transport level by Telegram itself. A Mini
App is different — it's a real webpage loading in a browser context, and
Telegram enforces HTTPS for `web_app` buttons. This means you need a
domain name and a TLS certificate, which none of your other bots have
required so far.

## Setup

**1. Point a domain (or subdomain) at your droplet's IP** — e.g.
`launch.yourdomain.com` — via your DNS provider, an A record.

**2. Install nginx and certbot:**
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

**3. nginx config** (`/etc/nginx/sites-available/launch-bot`):
```nginx
server {
    listen 80;
    server_name launch.yourdomain.com;

    location /miniapp/ {
        alias /home/botuser/launch-bot/miniapp/;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/launch-bot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

**4. Get a free TLS certificate:**
```bash
sudo certbot --nginx -d launch.yourdomain.com
```
Certbot edits the nginx config to add HTTPS and sets up auto-renewal.

**5. Update your code to match:**
- `launch_bot.py`: `MINI_APP_BASE_URL=https://launch.yourdomain.com/miniapp`
- `miniapp/evm.html` and `solana.html`: `API_BASE = 'https://launch.yourdomain.com/api'`

## Running the bot and API as services

**`launch-bot.service`:**
```ini
[Unit]
Description=Launch Bot (Telegram)
After=network.target

[Service]
WorkingDirectory=/home/botuser/launch-bot
ExecStart=/home/botuser/launch-bot/venv/bin/python3 launch_bot.py
User=botuser
Restart=always
RestartSec=10
EnvironmentFile=/home/botuser/launch-bot/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=launch-bot

[Install]
WantedBy=multi-user.target
```

**`launch-bot-api.service`:**
```ini
[Unit]
Description=Launch Bot API
After=network.target

[Service]
WorkingDirectory=/home/botuser/launch-bot
ExecStart=/home/botuser/launch-bot/venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000
User=botuser
Restart=always
RestartSec=10
EnvironmentFile=/home/botuser/launch-bot/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=launch-bot-api

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp launch-bot.service launch-bot-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now launch-bot launch-bot-api
```

## .env needed

```
TELEGRAM_BOT_TOKEN=
ETHEREUM_RPC_URL=
BSC_RPC_URL=
BASE_RPC_URL=
ROBINHOOD_RPC_URL=
SOLANA_RPC_URL=
PLATFORM_TREASURY_EVM=
MINI_APP_BASE_URL=https://launch.yourdomain.com/miniapp
```

## Before this actually works end to end

Beyond the "honest status" notes in the main README, specifically for
getting the full stack running:

1. **Deploy the Solidity contracts** and fill in `FACTORY_ADDRESSES` in
   `api.py` — right now they're empty strings, and `build-tx` will
   correctly refuse to proceed until they're set.
2. **Get a Reown Project ID** (free, dashboard.reown.com) and put it in
   both Mini App HTML files.
3. **Verify the Mini App JS against Reown's current docs** — flagged
   heavily in both HTML files' comments, this is the least-tested part
   of the entire project.
4. **Fill in `pumpfun_launch.py`'s two `NotImplementedError` spots**
   before Solana pump.fun routing will work at all.
5. **Test the whole chain on testnets first**: Sepolia, BSC testnet,
   Base Sepolia, Robinhood Chain testnet, Solana devnet.
