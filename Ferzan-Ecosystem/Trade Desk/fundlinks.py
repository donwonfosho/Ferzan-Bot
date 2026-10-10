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
    "bsc": ("BNB Chain", "evm", [("BNB", "bnb_bsc")]),
    "arb": ("Arbitrum", "evm", [("ETH", "eth_arbitrum"), ("USDC", "usdc_arbitrum")]),
    "op": ("Optimism", "evm", [("ETH", "eth_optimism")]),
    "pol": ("Polygon", "evm", [("POL", "pol_polygon"), ("USDC", "usdc_polygon")]),
    "avax": ("Avalanche", "evm", [("AVAX", "avax_cchain")]),
    "ton": ("TON", "ton", [("TON", "ton")]),
    "trx": ("Tron", "trx", [("TRX", "trx")]),
}
# No fiat provider lists these: buy a coin on a direct chain, then use the Ferzan Bridge.
BRIDGE_ONLY = {"arc": "Arc", "hood": "Robinhood Chain", "monad": "Monad", "sonic": "Sonic",
               "hype": "HyperEVM", "pulse": "PulseChain", "ink": "Ink", "linea": "Linea", "stable": "Stable"}
NO_ROUTE = {"pulse", "stable"}  # neither a card provider nor the bridge reaches these yet
# Verified against MoonPay's currency list on the droplet (Oct 10). Buy only: sell support was not shown.
# Sonic (suspended), Ink and Stable are not listed, so they stay bridge-only. Polygon waits for a verified code.
DEFAULT_CODES: dict[str, list[tuple[str, str]]] = {
    "arc": [("USDC", "usdc_arc")],
    "hood": [("ETH", "eth_robinhood")],
    "linea": [("ETH", "eth_linea")],
    "monad": [("MON", "mon_mon")],
    "hype": [("HYPE", "hype_hyperevm"), ("USDC", "usdc_hyperevm")],
}
# Buy this on a direct chain, then bridge: (chain, coin label, MoonPay code, min arrival to react to)
BRIDGE_SOURCE = ("base", "ETH", "eth_base", 0.0003)
# Codes MoonPay confirmed sellable (Oct 10 check). Anything else gets no Cash out link.
SELL_OK = {"sol", "usdc_sol", "eth", "usdc", "eth_base", "usdc_base", "bnb_bsc", "eth_arbitrum", "usdc_arbitrum",
           "avax_cchain", "ton", "trx"}
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


def extra_codes() -> dict[str, list[tuple[str, str]]]:
    """Direct-buy codes added without a release: FERZAN_FUND_CODES="arc:USDC=usdc_arc,hood:ETH=eth_robinhood".
    A bridge-only chain with a code here becomes a direct buy (EVM address)."""
    out: dict[str, list[tuple[str, str]]] = {k: list(v) for k, v in DEFAULT_CODES.items()}
    for part in (os.getenv("FERZAN_FUND_CODES") or "").split(","):
        try:
            head, code = part.strip().split("=", 1)
            chain, coin = head.split(":", 1)
            chain, coin, code = chain.strip().lower(), coin.strip().upper(), code.strip()
            if chain in BRIDGE_ONLY and coin and code.replace("_", "").isalnum():
                out.setdefault(chain, []).append((coin, code))
        except ValueError:
            continue
    return out


PROVIDERS = [{"id": "moonpay", "name": "MoonPay", "buy": moonpay_buy, "sell": moonpay_sell}]


def options(chain: str, addresses: dict[str, str]) -> dict:
    """Everything a surface needs for one chain. addresses: {"sol","evm","ton","trx"} -> address."""
    if chain in BRIDGE_ONLY:
        extra = extra_codes().get(chain)
        if extra:  # a provider code is configured: this chain is a direct buy
            addr = addresses.get("evm", "")
            buy = [{"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["buy"](code, addr)}
                   for p in PROVIDERS for coin, code in extra if addr]
            sell = []  # sell support for these new listings isn't confirmed: bridge out to a direct chain to cash out
            return {"chain": chain, "label": BRIDGE_ONLY[chain], "address": addr, "bridge_only": False,
                    "buy": buy, "sell": sell, "note": SETTLE_WARNING}
        if chain in NO_ROUTE:
            return {"chain": chain, "label": BRIDGE_ONLY[chain], "address": "", "bridge_only": True,
                    "unsupported": True, "buy": [], "sell": [], "via": None,
                    "note": "Funding this chain from outside isn't available yet."}
        src, coin, code, _min = BRIDGE_SOURCE
        a = addresses.get("evm", "")
        via = {"chain": src, "coin": coin, "address": a,
               "buy": [{"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["buy"](code, a)}
                       for p in PROVIDERS if a]}
        return {"chain": chain, "label": BRIDGE_ONLY[chain], "address": a, "bridge_only": True,
                "buy": [], "sell": [], "via": via,
                "note": f"Buy {coin} on {src.title()} to your Ferzan address, then bridge to {BRIDGE_ONLY[chain]}."}
    if chain not in CHAINS:
        raise KeyError(chain)
    label, kind, coins = CHAINS[chain]
    addr = addresses.get(kind, "")
    buy, sell = [], []
    for p in PROVIDERS:
        for coin, code in coins:
            if addr:
                buy.append({"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["buy"](code, addr)})
            if code in SELL_OK:
                sell.append({"provider": p["id"], "providerName": p["name"], "coin": coin, "url": p["sell"](code)})
    return {"chain": chain, "label": label, "address": addr, "bridge_only": False,
            "buy": buy, "sell": sell, "note": SETTLE_WARNING}


def all_options(addresses: dict[str, str]) -> list[dict]:
    return [options(c, addresses) for c in list(CHAINS) + list(BRIDGE_ONLY)]
