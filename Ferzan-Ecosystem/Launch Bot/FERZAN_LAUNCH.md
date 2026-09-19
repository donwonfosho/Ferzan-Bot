# Ferzan Launch

Telegram `/launch` is live as soon as `LAUNCHBOT_TOKEN` is set.

Signing a real token still needs:

1. HTTPS Mini App domain (`MINI_APP_BASE_URL`)
2. FastAPI `api.py` + factory addresses
3. Reown project ID in `miniapp/evm.html` and `solana.html`
4. Testnet first — bonding curve is unaudited

`.env`

```
LAUNCHBOT_TOKEN=
MINI_APP_BASE_URL=https://yourdomain.com/miniapp
```

```
cp ferzan-launch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ferzan-launch
```
