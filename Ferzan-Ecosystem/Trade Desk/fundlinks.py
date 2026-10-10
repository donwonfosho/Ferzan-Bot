"""Add funds / Cash out: hosted provider links, shared by the bot, the Mini App and the site.

Ferzan never touches card data or KYC. Each provider is a hosted page; we only pre-fill the
receive address. Providers here must work without a business account (keyless). Add another
by appending to PROVIDERS with a build(code, address) -> url and the codes it supports.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import quote, urlencode

# chain key -> (label, address kind, [(button label, MoonPay currency code)])
# address kind: "sol" | "evm" | "ton" | "trx"
CHAINS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "sol": ("Solana", "sol", [("SOL", "sol"), ("USDC", "usdc_sol")]),
    "eth": ("Ethereum", "evm", [("ETH", "eth"), ("USDC", "usdc")]),
    "base": ("Base", "evm", [("ETH", "eth_base"), ("USDC", "usdc_base")]),
    "bsc": ("BNB Chain", "evm", [("BNB", "bnb")]),
    "arb": ("Arbitrum", "evm", [("ETH", "eth_arbitrum"), ("USDC", "usdc_arbitrum")]),
    "op": ("Optimism", "evm", [("ETH", "eth_optimism")]),
    "pol": ("Polygon", "evm", [("POL", "matic_polygon")]),
    "avax": ("Avalanche", "evm", [("AVAX", "avax_cchain")]),
    "ton": ("TON", "ton", [("TON", "ton")]),
    "trx": ("Tron", "trx", [("TRX", "trx")]),
}
# No fiat provider lists these: buy a coin on a direct chain, then use the Ferzan Bridge.
BRIDGE_ONLY = {"arc": "Arc", "hood": "Robinhood Chain", "monad": "Monad", "sonic": "Sonic",
               "hype": "HyperEVM", "pulse": "PulseChain", "ink": "Ink", "linea": "Linea", "stable": "Stable"}
SETTLE_WARNING = ("Set the provider's receive address to your Ferzan address for that chain. "
                  "If it shows the provider's own wallet, change it or cancel.")


def moonpay_buy(code: str, address: str) -> str:
    pk = (os.getenv("MOONPAY_PK") or os.getenv("MOONPAY_KEY") or "").strip()
    sk = (os.getenv("MOONPAY_SK") or "").strip()
    params = {"currencyCode": code, "walletAddress": address, "baseCurrencyCode": "usd",
              "enabledPaymentMethods": "apple_pay,google_pay,credit_debit_card",
              "showWalletAddressForm": "true"}
    if pk:
        params["apiKey"] = pk
    q = urlencode(params)
    url = "https://buy.moonpay.com/?" + q
    if pk and sk:
        sig = base64.b64encode(hmac.new(sk.encode(), ("?" + q).encode(), hashlib.sha256).digest()).decode()
        url += "&signature=" + quote(sig)
    return url


def moonpay_sell(code: str) -> str:
    pk = (os.getenv("MOONPAY_PK") or os.getenv("MOONPAY_KEY") or "").strip()
    params = {"baseCurrencyCode": code, "quoteCurrencyCode": "usd"}
    if pk:
        params["apiKey"] = pk
    return "https://sell.moonpay.com/?" + urlencode(params)


PROVIDERS = [{"id": "moonpay", "name": "MoonPay", "buy": moonpay_buy, "sell": moonpay_sell}]


def options(chain: str, addresses: dict[str, str]) -> dict:
    """Everything a surface needs for one chain. addresses: {"sol","evm","ton","trx"} -> address."""
    if chain in BRIDGE_ONLY:
        return {"chain": chain, "label": BRIDGE_ONLY[chain], "address": addresses.get("evm", ""),
                "bridge_only": True, "buy": [], "sell": [],
                "note": "No card provider lists this chain yet. Add funds on Base, Ethereum or Solana, then bridge."}
    if chain not in CHAINS:
        raise KeyError(chain)
    label, kind, coins = CHAINS[chain]
    addr = addresses.get(kind, "")
    buy, sell = [], []
    for p in PROVIDERS:
        for coin, code in coins:
            if addr:
                buy.append({"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["buy"](code, addr)})
            sell.append({"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["sell"](code)})
    return {"chain": chain, "label": label, "address": addr, "bridge_only": False,
            "buy": buy, "sell": sell, "note": SETTLE_WARNING}


def all_options(addresses: dict[str, str]) -> list[dict]:
    return [options(c, addresses) for c in list(CHAINS) + list(BRIDGE_ONLY)]
