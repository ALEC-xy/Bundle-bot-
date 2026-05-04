# Solana Bundle Checker + Alpha Call Bot - Combined
# /bundle <CA> - Full bundle analysis
# Auto-alerts for high conviction plays and dead token resurrections

import os
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PUMP_MINT_AUTH = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Programs that own LP/bonding curve accounts
LP_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS",
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",
    "7YttLkHDoNj9wyDur5pM1ejNaAvT9X4eqaYcHQqtj2G5",
}

CEX_ADDRS = {
    "5tzFkiKscXHK5ms9wgXx3Ks5KKdPMY2qzyR7RxRkb3a1": "Binance",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5acaAfA": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Coinbase",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "OKX",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Kraken",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kucoin",
}

FAMOUS_TOKENS = {
    "bonk": "BONK", "dogwifhat": "WIF", "popcat": "POPCAT",
    "pepe": "PEPE", "shib": "SHIB", "doge": "DOGE",
    "trump": "TRUMP", "melania": "MELANIA", "maga": "MAGA",
    "solana": "SOL", "bitcoin": "BTC", "ethereum": "ETH",
    "floki": "FLOKI", "wojak": "WOJAK", "ponke": "PONKE",
}

# Scanner state
paused = False
alerted_tokens = set()
token_history = {}
wallet_db = {}
cobuy_db = defaultdict(list)
alert_log = []

scanner_settings = {
    "mcap_min": 5000,
    "mcap_max": 20000,
    "mcap_dead_min": 2000,
    "mcap_dead_max": 10000,
    "mcap_confirmed_min": 20000,
    "mcap_confirmed_max": 100000,
    "bundle_max": 35,
    "min_quality_wallets": 2,
    "min_buy_sell_ratio": 0.65,
    "min_unique_buyers": 12,
    "confirmation_count": 3,
}


# ── RPC ───────────────────────────────────────────────────────────────────────

async def rpc(client, method, params):
    try:
        r = await client.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }, timeout=30)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.warning(f"RPC {method}: {e}")
        return None


async def helius_txs(client, address, limit=20):
    if not HELIUS_API_KEY:
        return []
    try:
        r = await client.get(
            f"https://api.helius.xyz/v0/addresses/{address}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": limit},
            timeout=20
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"helius txs {address[:8]}: {e}")
        return []


# ── Holder fetching with LP filter ────────────────────────────────────────────

async def get_holders(client, mint):
    if not HELIUS_API_KEY:
        return []

    all_accounts = []
    cursor = None
    for _ in range(5):
        params = {"mint": mint, "limit": 100, "options": {"showZeroBalance": False}}
        if cursor:
            params["cursor"] = cursor
        try:
            r = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": "holders",
                "method": "getTokenAccounts", "params": params
            }, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                break
            result = data.get("result", {})
            accounts = result.get("token_accounts", [])
            all_accounts.extend(accounts)
            cursor = result.get("cursor")
            if not cursor or len(accounts) < 100:
                break
        except Exception as e:
            logger.warning(f"getTokenAccounts: {e}")
            break

    holders = []
    for acc in all_accounts:
        owner = acc.get("owner", "")
        amount = float(acc.get("amount", 0))
        if owner and amount > 0:
            holders.append({"owner": owner, "amount": amount})

    holders.sort(key=lambda x: x["amount"], reverse=True)

    # Filter LP and bonding curve accounts by checking program ownership + account size
    async def is_lp_account(owner):
        try:
            result = await rpc(client, "getAccountInfo", [owner, {"encoding": "base64"}])
            if not result:
                return False
            # Check program owner
            owner_program = result.get("owner", "")
            if owner_program in LP_PROGRAMS:
                return True
            # Check account data size - bonding curve = 300 bytes, LP = 165 bytes
            space = result.get("space", 0)
            if space in (300, 165):
                return True
            return False
        except Exception:
            return False

    # Check top 25 holders
    top25 = holders[:25]
    checks = await asyncio.gather(*[is_lp_account(h["owner"]) for h in top25])
    filtered = [h for h, is_lp in zip(top25, checks) if not is_lp]
    holders = filtered + holders[25:]
    holders.sort(key=lambda x: x["amount"], reverse=True)
    return holders


async def get_supply(client, mint):
    result = await rpc(client, "getTokenSupply", [mint])
    if not result:
        return 0.0
    try:
        info = result.get("value", {})
        ui = info.get("uiAmount")
        if ui:
            return float(ui)
        amount = int(info.get("amount", 0))
        decimals = int(info.get("decimals", 6))
        return amount / (10 ** decimals)
    except Exception:
        return 0.0


async def get_mint_info(client, mint):
    result = await rpc(client, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    try:
        info = result["data"]["parsed"]["info"]
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
        }
    except Exception:
        return {}


async def get_signatures(client, address, limit=20):
    result = await rpc(client, "getSignaturesForAddress", [address, {"limit": limit}])
    return result if isinstance(result, list) else []


async def get_price(client, mint):
    try:
        r = await client.get(f"https://price.jup.ag/v6/price?ids={mint}", timeout=10)
        r.raise_for_status()
        return float(r.json().get("data", {}).get(mint, {}).get("price", 0) or 0)
    except Exception:
        return 0


async def get_token_metadata(client, mint):
    if not HELIUS_API_KEY:
        return {}
    try:
        r = await client.post(
            f"https://api.helius.xyz/v0/token-metadata?api-key={HELIUS_API_KEY}",
            json={"mintAccounts": [mint], "includeOffChain": True, "disableCache": False},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else {}
    except Exception:
        return {}


# ── Wallet analysis ───────────────────────────────────────────────────────────

async def get_wallet_age(client, wallet):
    sigs = await get_signatures(client, wallet, limit=1000)
    if not sigs:
        return None
    bt = sigs[-1].get("blockTime")
    if not bt:
        return None
    return round((datetime.now(timezone.utc).timestamp() - bt) / 86400, 1)


async def get_funder(client, wallet):
    txs = await helius_txs(client, wallet, limit=10)
    for tx in reversed(txs or []):
        fp = tx.get("feePayer")
        if fp and fp != wallet:
            return fp
    sigs = await get_signatures(client, wallet, limit=5)
    return sigs[-1].get("signature", "")[:20] if sigs else None


async def check_selling(client, wallet, mint):
    txs = await helius_txs(client, wallet, limit=10)
    for tx in (txs or []):
        for t in tx.get("tokenTransfers", []):
            if t.get("mint") == mint and t.get("fromUserAccount") == wallet:
                return True
    return False


async def check_sniper(client, wallet, launch_slot):
    if not launch_slot:
        return False
    txs = await helius_txs(client, wallet, limit=20)
    for tx in (txs or []):
        slot = tx.get("slot", 0)
        ttype = tx.get("type", "")
        if slot and abs(slot - launch_slot) <= 2 and ("SWAP" in ttype or "BUY" in ttype):
            return True
    return False


async def check_cex_deposit(client, wallet):
    txs = await helius_txs(client, wallet, limit=20)
    for tx in (txs or []):
        for transfer in tx.get("nativeTransfers", []):
            dest = transfer.get("toUserAccount", "")
            if dest in CEX_ADDRS:
                return CEX_ADDRS[dest]
    return None


async def check_wash_trading(client, wallet, mint):
    txs = await helius_txs(client, wallet, limit=30)
    buys = sells = 0
    for tx in (txs or []):
        for t in tx.get("tokenTransfers", []):
            if t.get("mint") != mint:
                continue
            if t.get("toUserAccount") == wallet:
                buys += 1
            if t.get("fromUserAccount") == wallet:
                sells += 1
    return buys >= 2 and sells >= 2


async def get_deployer(client, mint):
    sigs = await get_signatures(client, mint, limit=50)
    if not sigs:
        return None
    oldest = sigs[-1].get("signature")
    if not oldest:
        return None
    tx = await rpc(client, "getTransaction", [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    if not tx:
        return None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        for k in keys:
            if isinstance(k, dict) and k.get("signer") and k.get("writable"):
                return k["pubkey"]
            elif isinstance(k, str):
                return k
    except Exception:
        pass
    txs = await helius_txs(client, mint, limit=1)
    if txs:
        return txs[-1].get("feePayer")
    return None


async def get_launch_slot(client, mint):
    sigs = await get_signatures(client, mint, limit=50)
    return sigs[-1].get("slot") if sigs else None


async def detect_same_block_buys(client, mint, launch_slot):
    if not launch_slot or not HELIUS_API_KEY:
        return [], 0
    try:
        r = await client.get(
            f"https://api.helius.xyz/v0/addresses/{mint}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": 50},
            timeout=20
        )
        r.raise_for_status()
        txs = r.json()
    except Exception:
        return [], 0

    slot_map = defaultdict(list)
    for tx in txs:
        slot = tx.get("slot", 0)
        buyer = tx.get("feePayer", "")
        ttype = tx.get("type", "")
        if slot and buyer and ("SWAP" in ttype or "BUY" in ttype):
            slot_map[slot].append(buyer)

    same_block_wallets = []
    group_count = 0
    for buyers in slot_map.values():
        if len(buyers) >= 2:
            same_block_wallets.extend(buyers)
            group_count += 1

    return list(set(same_block_wallets)), group_count


def check_copycat(name, symbol):
    name_lower = (name or "").lower()
    symbol_lower = (symbol or "").lower()
    for keyword, original in FAMOUS_TOKENS.items():
        if keyword in name_lower or keyword in symbol_lower:
            return original
    return None


def extract_socials(metadata):
    socials = {}
    try:
        offchain = metadata.get("offChainData", {})
        extensions = offchain.get("extensions", {})
        socials["twitter"] = extensions.get("twitter", "")
        socials["telegram"] = extensions.get("telegram", "")
        socials["website"] = extensions.get("website", offchain.get("external_url", ""))
    except Exception:
        pass
    return socials


# ── Bundle analysis ───────────────────────────────────────────────────────────

async def analyse_bundle(mint):
    async with httpx.AsyncClient() as client:

        supply, holders, mint_info, deployer, launch_slot, metadata, price = await asyncio.gather(
            get_supply(client, mint),
            get_holders(client, mint),
            get_mint_info(client, mint),
            get_deployer(client, mint),
            get_launch_slot(client, mint),
            get_token_metadata(client, mint),
            get_price(client, mint),
        )

        if not holders:
            return {"error": "No holder data found. Token may not exist or have no holders yet."}

        total = sum(h["amount"] for h in holders)
        if supply == 0:
            supply = total * 1.05
        if total > supply * 100:
            for h in holders:
                h["amount"] = h["amount"] / 1_000_000

        token_name = token_symbol = ""
        try:
            onchain = metadata.get("onChainMetadata", {}).get("metadata", {}).get("data", {})
            token_name = onchain.get("name", "").strip()
            token_symbol = onchain.get("symbol", "").strip()
        except Exception:
            pass

        market_cap = price * supply if price and supply else 0
        mint_authority = mint_info.get("mint_authority")
        freeze_authority = mint_info.get("freeze_authority")
        can_mint = bool(mint_authority)
        can_freeze = bool(freeze_authority)
        is_pump = mint_authority == PUMP_MINT_AUTH or freeze_authority == PUMP_MINT_AUTH or mint.endswith("pump")

        top10_pct = sum(h["amount"] for h in holders[:10]) / supply * 100 if supply else 0
        top20_pct = sum(h["amount"] for h in holders[:20]) / supply * 100 if supply else 0

        deployer_pct = 0
        if deployer:
            for h in holders:
                if h["owner"] == deployer:
                    deployer_pct = h["amount"] / supply * 100
                    break

        top15 = holders[:15]
        wallets = [h["owner"] for h in top15]

        ages, funders, snipers, selling, cex_flags, wash_flags = await asyncio.gather(
            asyncio.gather(*[get_wallet_age(client, w) for w in wallets]),
            asyncio.gather(*[get_funder(client, w) for w in wallets]),
            asyncio.gather(*[check_sniper(client, w, launch_slot) for w in wallets]),
            asyncio.gather(*[check_selling(client, w, mint) for w in wallets]),
            asyncio.gather(*[check_cex_deposit(client, w) for w in wallets]),
            asyncio.gather(*[check_wash_trading(client, w, mint) for w in wallets]),
        )

        same_block_wallets, same_block_groups = await detect_same_block_buys(client, mint, launch_slot)
        socials = extract_socials(metadata)

        for i, h in enumerate(top15):
            h["age_days"] = ages[i]
            h["funder"] = funders[i]
            h["is_sniper"] = snipers[i]
            h["is_selling"] = selling[i]
            h["is_fresh"] = ages[i] is not None and ages[i] < 7
            h["cex"] = cex_flags[i]
            h["wash"] = wash_flags[i]
            h["same_block"] = h["owner"] in same_block_wallets

        fmap = defaultdict(list)
        for h in top15:
            if h.get("funder"):
                fmap[h["funder"]].append(h["owner"])
        bundle_groups = [w for w in fmap.values() if len(w) >= 2]
        bundled = list(set(w for g in bundle_groups for w in g))
        bundled_pct = sum(h["amount"] for h in holders if h["owner"] in bundled) / supply * 100 if supply else 0

        sniper_list = [h for h in top15 if h.get("is_sniper")]
        sniper_pct = sum(h["amount"] for h in sniper_list) / supply * 100 if supply else 0
        fresh_list = [h for h in top15 if h.get("is_fresh")]
        fresh_pct = sum(h["amount"] for h in fresh_list) / supply * 100 if supply else 0
        sell_count = sum(1 for h in top15 if h.get("is_selling"))
        cex_count = sum(1 for h in top15 if h.get("cex"))
        wash_count = sum(1 for h in top15 if h.get("wash"))
        same_block_pct = sum(h["amount"] for h in top15 if h.get("same_block")) / supply * 100 if supply else 0
        copycat = check_copycat(token_name, token_symbol)

        return {
            "mint": mint,
            "token_name": token_name,
            "token_symbol": token_symbol,
            "supply": supply,
            "price": price,
            "market_cap": market_cap,
            "holders": holders,
            "holder_count": len(holders),
            "top10_pct": top10_pct,
            "top20_pct": top20_pct,
            "is_pump": is_pump,
            "can_mint": can_mint,
            "can_freeze": can_freeze,
            "deployer": deployer,
            "deployer_pct": deployer_pct,
            "bundle_groups": bundle_groups,
            "bundled": bundled,
            "bundled_pct": bundled_pct,
            "sniper_count": len(sniper_list),
            "sniper_pct": sniper_pct,
            "fresh_count": len(fresh_list),
            "fresh_pct": fresh_pct,
            "sell_count": sell_count,
            "cex_count": cex_count,
            "wash_count": wash_count,
            "same_block_groups": same_block_groups,
            "same_block_pct": same_block_pct,
            "copycat": copycat,
            "socials": socials,
        }


def bundle_score(data):
    s = 0
    s += min(data["top10_pct"] * 0.4, 20)
    s += min(data["bundled_pct"] * 0.8, 15)
    s += min(len(data["bundle_groups"]) * 3, 10)
    s += min(data["sniper_pct"] * 1.0, 12)
    s += min(data["fresh_pct"] * 0.5, 8)
    s += min(data["deployer_pct"] * 0.5, 8)
    s += min(data["same_block_pct"] * 1.0, 10)
    s += min(data["wash_count"] * 3, 6)
    s += min(data["cex_count"] * 2, 4)
    if data["can_mint"]: s += 5
    if data["can_freeze"]: s += 3
    if data["copycat"]: s += 3
    if data["is_pump"]: s += 2
    s = min(int(s), 100)
    if s >= 75: return s, "EXTREMELY BUNDLED", "🔴"
    if s >= 55: return s, "HEAVILY BUNDLED", "🟠"
    if s >= 35: return s, "MODERATELY BUNDLED", "🟡"
    if s >= 15: return s, "SLIGHTLY BUNDLED", "🟢"
    return s, "LOOKS CLEAN", "✅"


def fmt_num(n):
    if n >= 1_000_000_000: return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000: return f"${n/1_000:.1f}K"
    return f"${n:.2f}"


def format_bundle_report(data):
    if "error" in data:
        return f"❌ *Error:* {data['error']}"

    sc, label, emoji = bundle_score(data)
    bar = "█" * (sc // 10) + "░" * (10 - sc // 10)
    dev = data["deployer"]
    dev_str = f"`{dev[:6]}...{dev[-4:]}`" if dev else "`unknown`"
    supply = data["supply"]
    price_str = f"${data['price']:.8f}" if data["price"] else "N/A"
    mcap_str = fmt_num(data["market_cap"]) if data["market_cap"] else "N/A"
    name_line = f"*{data['token_name']}* (${data['token_symbol']})\n" if data["token_name"] else ""

    s = data.get("socials", {})
    social_parts = []
    if s.get("twitter"): social_parts.append(f"[Twitter]({s['twitter']})")
    if s.get("telegram"): social_parts.append(f"[TG]({s['telegram']})")
    if s.get("website"): social_parts.append(f"[Web]({s['website']})")
    social_str = " | ".join(social_parts) if social_parts else "None"

    lines = [
        f"🔬 *BUNDLE REPORT*",
        f"{name_line}`{data['mint']}`",
        f"🔗 {social_str}",
        f"",
        f"💰 Price: `{price_str}` | MCap: `{mcap_str}`",
        f"👥 Holders: `{data['holder_count']}`",
    ]

    if data["copycat"]:
        lines.append(f"⚠️ *COPYCAT* — mimics `${data['copycat']}`")

    lines += [
        f"",
        f"{emoji} *{label}*",
        f"`[{bar}] {sc}/100`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 *CONCENTRATION*",
        f"├ Top 10: `{data['top10_pct']:.1f}%`",
        f"└ Top 20: `{data['top20_pct']:.1f}%`",
        f"",
        f"🪢 *BUNDLES*",
        f"├ Clusters: `{len(data['bundle_groups'])}`",
        f"├ Wallets: `{len(data['bundled'])}`",
        f"└ Supply: `{data['bundled_pct']:.1f}%`",
        f"",
        f"⚡ *SAME-BLOCK BUYS*",
        f"├ Groups: `{data['same_block_groups']}`",
        f"└ Supply: `{data['same_block_pct']:.1f}%`",
        f"",
        f"🎯 *SNIPERS* _(block 1-2)_",
        f"├ Count: `{data['sniper_count']}`",
        f"└ Supply: `{data['sniper_pct']:.1f}%`",
        f"",
        f"🆕 *FRESH WALLETS* _(<7d)_",
        f"├ Count: `{data['fresh_count']}`",
        f"└ Supply: `{data['fresh_pct']:.1f}%`",
        f"",
        f"🏦 *CEX DEPOSITS*: `{data['cex_count']}`",
        f"🔁 *WASH TRADERS*: `{data['wash_count']}`",
        f"",
        f"👨‍💻 *DEV*: {dev_str} holding `{data['deployer_pct']:.1f}%`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🔐 *SECURITY*",
        f"├ Pump.fun: {'`YES ⚠️`' if data['is_pump'] else '`NO ✅`'}",
        f"├ Mint auth: {'`OPEN ⚠️`' if data['can_mint'] else '`REVOKED ✅`'}",
        f"├ Freeze auth: {'`OPEN ⚠️`' if data['can_freeze'] else '`REVOKED ✅`'}",
        f"└ Active sellers: `{data['sell_count']}` of top 15",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏆 *TOP 5 HOLDERS*",
    ]

    for i, h in enumerate(data["holders"][:5], 1):
        pct = h["amount"] / supply * 100 if supply else 0
        owner = h["owner"]
        short = f"`{owner[:6]}...{owner[-4:]}`"
        flags = []
        if owner in data["bundled"]: flags.append("🪢")
        if h.get("is_sniper"): flags.append("🎯")
        if h.get("is_fresh"): flags.append("🆕")
        if h.get("is_selling"): flags.append("📉")
        if h.get("same_block"): flags.append("⚡")
        if h.get("cex"): flags.append(f"🏦{h['cex']}")
        if h.get("wash"): flags.append("🔁")
        if dev and owner == dev: flags.append("👨‍💻")
        age = f"{h['age_days']}d" if h.get("age_days") is not None else "?"
        lines.append(f"{i}. {short} `{pct:.2f}%` _{age}_ {' '.join(flags)}")

    lines += [
        f"",
        f"_🪢bundle ⚡same-block 🎯sniper 🆕fresh 📉sell 🏦CEX 🔁wash 👨‍💻dev_",
        f"_🚫 LP and bonding curve wallets filtered_",
    ]
    return "\n".join(lines)


# ── Call bot scanner ──────────────────────────────────────────────────────────

async def get_recent_trades(client, mint, limit=40):
    txs = await helius_txs(client, mint, limit=limit)
    trades = []
    for tx in (txs or []):
        ttype = tx.get("type", "")
        is_buy = "BUY" in ttype or "SWAP" in ttype
        is_sell = "SELL" in ttype
        if is_buy or is_sell:
            trades.append({
                "wallet": tx.get("feePayer", ""),
                "type": "buy" if is_buy else "sell",
                "slot": tx.get("slot", 0),
                "ts": tx.get("timestamp", 0),
                "sol_amount": sum(abs(t.get("amount", 0)) for t in tx.get("nativeTransfers", [])),
            })
    return trades


def organic_volume_score(trades, holder_count):
    if not trades:
        return 0
    score = 100
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    unique_buyers = set(t["wallet"] for t in buys)
    unique_sellers = set(t["wallet"] for t in sells)
    recyclers = unique_buyers & unique_sellers
    wash_ratio = len(recyclers) / max(len(unique_buyers), 1)
    if wash_ratio > 0.3: score -= 30
    slot_map = defaultdict(list)
    for t in buys:
        slot_map[t["slot"]].append(t["wallet"])
    coordinated = [w for w in slot_map.values() if len(w) >= 3]
    if len(coordinated) >= 2: score -= 25
    buy_amounts = [t["sol_amount"] for t in buys if t["sol_amount"] > 0]
    if buy_amounts:
        tiny = sum(1 for a in buy_amounts if a < 0.05)
        if tiny / max(len(buy_amounts), 1) > 0.7: score -= 20
    if holder_count < 8 and len(trades) > 25: score -= 20
    buy_ratio = len(buys) / max(len(trades), 1)
    if buy_ratio < scanner_settings["min_buy_sell_ratio"]: score -= 15
    if len(buys) >= 5:
        timestamps = sorted(t["ts"] for t in buys if t["ts"])
        if timestamps and timestamps[-1] - timestamps[0] < 30: score -= 15
    return max(0, score)


async def profile_wallet_quick(client, wallet):
    if wallet in wallet_db:
        cached = wallet_db[wallet]
        if time.time() - cached.get("ts", 0) < 300:
            return cached

    txs = await helius_txs(client, wallet, limit=30)
    tx_count = len(txs or [])

    buy_times = {}
    sell_times = {}
    for tx in (txs or []):
        ttype = tx.get("type", "")
        ts = tx.get("timestamp", 0)
        for transfer in tx.get("tokenTransfers", []):
            mint = transfer.get("mint", "")
            if not mint:
                continue
            if ("BUY" in ttype or "SWAP" in ttype) and mint not in buy_times:
                buy_times[mint] = ts
            elif "SELL" in ttype and mint not in sell_times:
                sell_times[mint] = ts

    hold_times = []
    for mint in buy_times:
        if mint in sell_times and buy_times[mint] and sell_times[mint]:
            hold_times.append(abs(sell_times[mint] - buy_times[mint]) / 60)

    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
    is_farmer = avg_hold < 2 and len(hold_times) > 5

    if is_farmer:
        quality = 20
    elif avg_hold > 15 and tx_count >= 10:
        quality = 80
    elif avg_hold > 5 and tx_count >= 5:
        quality = 65
    elif tx_count >= 20:
        quality = 60
    else:
        quality = 40

    profile = {
        "wallet": wallet,
        "quality": quality,
        "avg_hold": avg_hold,
        "is_farmer": is_farmer,
        "tx_count": tx_count,
        "ts": time.time(),
    }
    wallet_db[wallet] = profile
    return profile


def update_momentum(mint, mcap, holders):
    now = time.time()
    if mint not in token_history:
        token_history[mint] = {"snaps": [], "first_seen": now, "confirmations": 0}
    h = token_history[mint]
    h["snaps"].append({"ts": now, "mcap": mcap, "holders": holders})
    h["snaps"] = h["snaps"][-10:]
    age_mins = (now - h["first_seen"]) / 60
    snaps = h["snaps"]
    if len(snaps) < 2:
        return 50, "neutral", age_mins
    old_mcap = snaps[0]["mcap"]
    mcap_change = (mcap - old_mcap) / max(old_mcap, 1) * 100
    holder_change = holders - snaps[0]["holders"]
    score = 50
    if mcap_change > 30: score += 15
    if mcap_change > 80: score += 10
    if holder_change > 3: score += 10
    if age_mins >= 3 and mcap > old_mcap: score += 10
    if mcap_change < -10: score -= 25
    if len(snaps) >= 3:
        recent = [s["mcap"] for s in snaps[-3:]]
        if recent[2] > recent[1] > recent[0]: score += 10
    score = max(0, min(100, score))
    if score >= 70: trend = "accelerating"
    elif score >= 50: trend = "growing"
    elif score >= 30: trend = "slowing"
    else: trend = "declining"
    return score, trend, age_mins


async def check_dead_resurrection(client, mint, mcap):
    if not (scanner_settings["mcap_dead_min"] <= mcap <= scanner_settings["mcap_dead_max"]):
        return False, 0
    try:
        sigs = await rpc(client, "getSignaturesForAddress", [mint, {"limit": 100}])
        if not sigs or not isinstance(sigs, list) or len(sigs) < 10:
            return False, 0
        now = time.time()
        timestamps = sorted([s.get("blockTime", 0) for s in sigs if s.get("blockTime")], reverse=True)
        recent_5min = sum(1 for t in timestamps[:20] if now - t < 300)
        gap_hours = (timestamps[0] - timestamps[20]) / 3600 if len(timestamps) > 20 else 0
        return gap_hours >= 1.5 and recent_5min >= 4, gap_hours
    except Exception:
        return False, 0


async def scan_and_alert(client, mint, app):
    global paused
    if paused or mint in alerted_tokens:
        return

    try:
        supply_task = get_supply(client, mint)
        price_task = get_price(client, mint)
        supply, price = await asyncio.gather(supply_task, price_task)
        mcap = price * supply if price and supply else 0
        if mcap == 0:
            return

        mint_info = await get_mint_info(client, mint)
        if mint_info.get("mint_authority") or mint_info.get("freeze_authority"):
            return

        holders_r = await client.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": "scan",
            "method": "getTokenAccounts",
            "params": {"mint": mint, "limit": 30, "options": {"showZeroBalance": False}}
        }, timeout=20)
        holders_data = holders_r.json().get("result", {}).get("token_accounts", [])
        holders = [{"owner": h.get("owner"), "amount": float(h.get("amount", 0))} for h in holders_data if h.get("owner")]
        holders.sort(key=lambda x: x["amount"], reverse=True)
        holder_count = len(holders)

        in_range = scanner_settings["mcap_min"] <= mcap <= scanner_settings["mcap_max"]
        in_confirmed = scanner_settings["mcap_confirmed_min"] <= mcap <= scanner_settings["mcap_confirmed_max"]
        is_dead, gap_hours = await check_dead_resurrection(client, mint, mcap)

        if not (in_range or in_confirmed or is_dead):
            return

        trades = await get_recent_trades(client, mint, limit=40)
        organic = organic_volume_score(trades, holder_count)
        if organic < 35 and not is_dead:
            return

        buys = [t for t in trades if t["type"] == "buy"]
        buy_ratio = len(buys) / max(len(trades), 1)
        unique_buyers = len(set(t["wallet"] for t in buys))

        if not is_dead:
            if buy_ratio < scanner_settings["min_buy_sell_ratio"]:
                return
            if unique_buyers < scanner_settings["min_unique_buyers"]:
                return

        profiles = await asyncio.gather(*[profile_wallet_quick(client, h["owner"]) for h in holders[:15]])
        quality_wallets = [p for p in profiles if p and p["quality"] >= 60 and not p["is_farmer"]]
        qw_addrs = [p["wallet"] for p in quality_wallets]

        cobuy_count = 0
        if len(qw_addrs) >= 2:
            for i in range(len(qw_addrs)):
                for j in range(i+1, len(qw_addrs)):
                    pair = frozenset([qw_addrs[i], qw_addrs[j]])
                    cobuy_count = max(cobuy_count, len(cobuy_db.get(pair, [])))

        momentum_score, momentum_trend, age_mins = update_momentum(mint, mcap, holder_count)

        if not is_dead:
            if age_mins < 3:
                if mint not in token_history:
                    return
                token_history[mint]["confirmations"] = token_history[mint].get("confirmations", 0) + 1
                if token_history[mint]["confirmations"] < scanner_settings["confirmation_count"]:
                    return
            if momentum_score < 30:
                return

        if in_range and len(quality_wallets) >= scanner_settings["min_quality_wallets"]:
            alert_type = "HIGH_CONVICTION"
        elif in_range:
            alert_type = "EARLY"
        elif in_confirmed:
            alert_type = "CONFIRMED"
        elif is_dead:
            alert_type = "DEAD"
        else:
            return

        supply_val = supply if supply > 0 else 1
        quick_bundle = min(int(sum(h["amount"] for h in holders[:10]) / supply_val * 100 * 0.7), 100)
        if quick_bundle > scanner_settings["bundle_max"] and not is_dead:
            return

        conf_score = 0
        reasons = []
        conf_score += 10 if mcap > 0 else 0
        conf_score += min(len(quality_wallets) * 8, 24)
        if len(quality_wallets) >= 2: reasons.append(f"{len(quality_wallets)} quality wallets holding")
        if len(quality_wallets) >= 5: conf_score += 10; reasons.append("⚡ Coordinated quality entry")
        elite = [p for p in quality_wallets if p["quality"] >= 85]
        if elite: conf_score += 15; reasons.append(f"🐋 Elite wallet entered ({len(elite)})")
        if cobuy_count >= 3: conf_score += 15; reasons.append(f"Co-bought {cobuy_count}x before")
        elif cobuy_count >= 1: conf_score += 8
        conf_score += int(momentum_score * 0.15)
        if momentum_score >= 70: reasons.append(f"Strong momentum ({momentum_trend})")
        conf_score += int(organic * 0.1)
        if organic >= 80: reasons.append("Organic volume confirmed")
        elif organic < 40: conf_score -= 15
        if quick_bundle <= 20: conf_score += 10; reasons.append("Clean bundle score")
        if age_mins >= 3: conf_score += 8; reasons.append("Survived 3 min rule")
        if is_dead: conf_score += 15; reasons.append(f"Dead {gap_hours:.1f}h, resurrecting")
        conf_score = max(0, min(100, int(conf_score)))

        if conf_score < 40:
            return

        type_labels = {
            "EARLY": "🎯 EARLY ENTRY",
            "HIGH_CONVICTION": "🔥 HIGH CONVICTION",
            "CONFIRMED": "💎 CONFIRMED PLAY",
            "DEAD": "💀 DEAD RESURRECTION",
        }

        if conf_score >= 75: tier = "🔥 HIGH CONVICTION"
        elif conf_score >= 50: tier = "👀 WATCH"
        else: tier = "⚠️ RISKY"

        alert_lines = [
            f"{type_labels.get(alert_type, '🚨 ALERT')}",
            f"",
            f"`{mint}`",
            f"",
            f"*{tier}* `{conf_score}/100`",
            f"",
            f"💰 MCap: `${mcap:,.0f}`",
            f"💵 Price: `${price:.8f}`",
            f"👥 Holders: `{holder_count}`",
            f"📈 Momentum: `{momentum_trend}`",
            f"",
            f"👛 Quality wallets: `{len(quality_wallets)}`",
            f"🤝 Co-buy history: `{cobuy_count}x`",
            f"🧺 Organic volume: `{organic}/100`",
            f"🪢 Bundle score: `{quick_bundle}/100`",
            f"",
            f"📋 *Signals:*",
        ]
        for r in reasons[:5]:
            alert_lines.append(f"• {r}")
        alert_lines += [
            f"",
            f"[Dexscreener](https://dexscreener.com/solana/{mint}) | [Photon](https://photon-sol.tinyastro.io/en/lp/{mint})",
        ]

        alerted_tokens.add(mint)

        if ALERT_CHAT_ID and app:
            try:
                await app.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text="\n".join(alert_lines),
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"send alert: {e}")

        if len(qw_addrs) >= 2:
            for i in range(len(qw_addrs)):
                for j in range(i+1, len(qw_addrs)):
                    pair = frozenset([qw_addrs[i], qw_addrs[j]])
                    if mint not in cobuy_db[pair]:
                        cobuy_db[pair].append(mint)

        alert_log.append({
            "mint": mint, "type": alert_type, "tier": tier,
            "score": conf_score, "mcap_entry": mcap, "mcap_30m": 0,
            "ts": time.time(),
        })

        logger.info(f"Alert: {alert_type} {mint[:8]} score={conf_score} mcap=${mcap:,.0f}")

    except Exception as e:
        logger.warning(f"scan {mint[:8]}: {e}")


async def get_new_pump_tokens(client):
    try:
        sigs = await rpc(client, "getSignaturesForAddress", [PUMP_FUN_PROGRAM, {"limit": 15}])
        if not sigs:
            return []
        mints = []
        for sig_info in (sigs or []):
            sig = sig_info.get("signature", "")
            tx = await rpc(client, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx:
                continue
            try:
                for ix in tx["transaction"]["message"]["instructions"]:
                    for acc in ix.get("accounts", []):
                        if isinstance(acc, str) and len(acc) > 40 and acc.endswith("pump"):
                            if acc not in mints and acc not in alerted_tokens:
                                mints.append(acc)
            except Exception:
                continue
        return mints[:5]
    except Exception as e:
        logger.warning(f"get_new_tokens: {e}")
        return []


async def scanner_loop(app):
    logger.info("Scanner started")
    scan_count = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if not paused:
                    new_mints = await get_new_pump_tokens(client)
                    for mint in new_mints:
                        await scan_and_alert(client, mint, app)

                    scan_count += 1
                    if scan_count % 10 == 0:
                        recent = [m for m in list(token_history.keys())[-15:] if m not in alerted_tokens]
                        for mint in recent:
                            await scan_and_alert(client, mint, app)

                    now = time.time()
                    for entry in alert_log:
                        if entry["mcap_30m"] == 0 and now - entry["ts"] >= 1800:
                            price = await get_price(client, entry["mint"])
                            supply = await get_supply(client, entry["mint"])
                            if price and supply:
                                entry["mcap_30m"] = price * supply

            except Exception as e:
                logger.error(f"Scanner error: {e}")

            await asyncio.sleep(12)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Solana Alpha Bot*\n\n"
        "*Bundle Checker:*\n"
        "Paste any CA or use `/bundle <CA>`\n\n"
        "*Auto Alerts:*\n"
        "🎯 Early Entry — 5-20k organic\n"
        "🔥 High Conviction — quality wallets in\n"
        "💎 Confirmed Play — 20-100k sustained\n"
        "💀 Dead Resurrection — 2-10k revival\n\n"
        "*Commands:*\n"
        "`/bundle <CA>` — Bundle analysis\n"
        "`/setmcap 5000 20000` — Set MCap range\n"
        "`/setbundle 35` — Max bundle score\n"
        "`/pause` / `/resume` — Pause alerts\n"
        "`/topwallets` — Best tracked wallets\n"
        "`/performance` — Alert win rate\n"
        "`/stats` — Current settings",
        parse_mode="Markdown"
    )


async def cmd_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/bundle <CA>`", parse_mode="Markdown")
        return
    mint = args[0].strip()
    if len(mint) < 32 or len(mint) > 50:
        await update.message.reply_text("❌ Invalid address.")
        return
    msg = await update.message.reply_text(
        f"🔬 Scanning `{mint[:8]}...`\n_(20-40s)_",
        parse_mode="Markdown"
    )
    try:
        data = await analyse_bundle(mint)
        await msg.edit_text(format_bundle_report(data), parse_mode="Markdown")
    except Exception as e:
        logger.exception("bundle error")
        await msg.edit_text(f"❌ `{e}`", parse_mode="Markdown")


async def cmd_setmcap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: `/setmcap 5000 20000`", parse_mode="Markdown")
        return
    try:
        scanner_settings["mcap_min"] = int(args[0])
        scanner_settings["mcap_max"] = int(args[1])
        await update.message.reply_text(f"MCap range: `${scanner_settings['mcap_min']:,}` — `${scanner_settings['mcap_max']:,}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid values.")


async def cmd_setbundle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/setbundle 35`", parse_mode="Markdown")
        return
    try:
        scanner_settings["bundle_max"] = int(args[0])
        await update.message.reply_text(f"Max bundle: `{scanner_settings['bundle_max']}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid value.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text("⏸ Alerts paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text("▶️ Alerts resumed.")


async def cmd_topwallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not wallet_db:
        await update.message.reply_text("No wallets tracked yet.")
        return
    top = sorted(wallet_db.values(), key=lambda w: w["quality"], reverse=True)[:10]
    lines = ["👛 *Top Wallets*", ""]
    for i, w in enumerate(top, 1):
        addr = w["wallet"]
        farmer = "🚫" if w["is_farmer"] else "✅"
        lines.append(f"{i}. `{addr[:6]}...{addr[-4:]}` score:`{w['quality']}` hold:`{w['avg_hold']:.0f}m` {farmer}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not alert_log:
        await update.message.reply_text("No alerts fired yet.")
        return
    total = len(alert_log)
    completed = [a for a in alert_log if a["mcap_30m"] > 0]
    wins = [a for a in completed if a["mcap_30m"] > a["mcap_entry"] * 1.2]
    win_rate = len(wins) / max(len(completed), 1) * 100
    lines = [
        "📊 *Performance*", f"",
        f"Total alerts: `{total}`",
        f"Completed: `{len(completed)}`",
        f"Win rate (>20%): `{win_rate:.1f}%`",
        f"", f"*Recent:*",
    ]
    for a in alert_log[-5:]:
        if a["mcap_30m"] > 0:
            change = (a["mcap_30m"] - a["mcap_entry"]) / a["mcap_entry"] * 100
            emoji = "✅" if change > 20 else "❌"
            lines.append(f"{emoji} `{a['mint'][:8]}` {change:+.0f}% [{a['type']}]")
        else:
            lines.append(f"⏳ `{a['mint'][:8]}` pending [{a['type']}]")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"Early MCap: `${scanner_settings['mcap_min']:,}` — `${scanner_settings['mcap_max']:,}`\n"
        f"Confirmed MCap: `${scanner_settings['mcap_confirmed_min']:,}` — `${scanner_settings['mcap_confirmed_max']:,}`\n"
        f"Dead token MCap: `${scanner_settings['mcap_dead_min']:,}` — `${scanner_settings['mcap_dead_max']:,}`\n"
        f"Max bundle: `{scanner_settings['bundle_max']}`\n"
        f"Status: `{'⏸ Paused' if paused else '▶️ Running'}`\n"
        f"Wallets tracked: `{len(wallet_db)}`\n"
        f"Alerts fired: `{len(alert_log)}`",
        parse_mode="Markdown"
    )


async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    b58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    if 32 <= len(text) <= 50 and all(c in b58 for c in text):
        context.args = [text]
        await cmd_bundle(update, context)
    else:
        await update.message.reply_text("Paste a CA or use `/bundle <CA>`", parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("bundle", cmd_bundle))
    app.add_handler(CommandHandler("setmcap", cmd_setmcap))
    app.add_handler(CommandHandler("setbundle", cmd_setbundle))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("topwallets", cmd_topwallets))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    async def post_init(application):
        asyncio.create_task(scanner_loop(application))

    app.post_init = post_init

    logger.info("Solana Alpha Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
