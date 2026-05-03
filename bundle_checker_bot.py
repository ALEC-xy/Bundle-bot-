# Solana Bundle Checker ULTIMATE - Telegram Bot

import os
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PUMP_MINT_AUTH = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"

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
                "jsonrpc": "2.0", "id": "get-holders",
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

    async def is_lp_or_program(owner):
        try:
            result = await rpc(client, "getAccountInfo", [owner, {"encoding": "base64"}])
            if not result:
                return False
            owner_program = result.get("owner", "")
            if owner_program in LP_PROGRAMS:
                return True
            space = result.get("space", 0)
            if space in (300, 165):
                return True
            return False
        except Exception:
            return False

    top25 = holders[:25]
    checks = await asyncio.gather(*[is_lp_or_program(h["owner"]) for h in top25])
    filtered = [h for h, is_lp in zip(top25, checks) if not is_lp]
    holders = filtered + holders[25:]
    holders.sort(key=lambda x: x["amount"], reverse=True)
    return holders


async def get_helius_txs(client, address, limit=20):
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
    except Exception as e:
        logger.warning(f"token metadata: {e}")
        return {}


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


async def get_wallet_age(client, wallet):
    sigs = await get_signatures(client, wallet, limit=1000)
    if not sigs:
        return None
    bt = sigs[-1].get("blockTime")
    if not bt:
        return None
    return round((datetime.now(timezone.utc).timestamp() - bt) / 86400, 1)


async def get_funder(client, wallet):
    txs = await get_helius_txs(client, wallet, limit=10)
    for tx in reversed(txs or []):
        fp = tx.get("feePayer")
        if fp and fp != wallet:
            return fp
    sigs = await get_signatures(client, wallet, limit=5)
    return sigs[-1].get("signature", "")[:20] if sigs else None


async def check_selling(client, wallet, mint):
    txs = await get_helius_txs(client, wallet, limit=10)
    for tx in (txs or []):
        for t in tx.get("tokenTransfers", []):
            if t.get("mint") == mint and t.get("fromUserAccount") == wallet:
                return True
    return False


async def check_sniper(client, wallet, launch_slot):
    if not launch_slot:
        return False
    txs = await get_helius_txs(client, wallet, limit=20)
    for tx in (txs or []):
        slot = tx.get("slot", 0)
        ttype = tx.get("type", "")
        if slot and abs(slot - launch_slot) <= 2 and ("SWAP" in ttype or "BUY" in ttype):
            return True
    return False


async def check_cex_deposit(client, wallet):
    txs = await get_helius_txs(client, wallet, limit=20)
    for tx in (txs or []):
        for transfer in tx.get("nativeTransfers", []):
            dest = transfer.get("toUserAccount", "")
            if dest in CEX_ADDRS:
                return CEX_ADDRS[dest]
    return None


async def check_wash_trading(client, wallet, mint):
    txs = await get_helius_txs(client, wallet, limit=30)
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
    txs = await get_helius_txs(client, mint, limit=1)
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


async def analyse(mint):
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


def score(data):
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


def report(data):
    if "error" in data:
        return f"❌ *Error:* {data['error']}"

    sc, label, emoji = score(data)
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
        f"🏦 *CEX DEPOSITS* in top 15: `{data['cex_count']}`",
        f"🔁 *WASH TRADERS* in top 15: `{data['wash_count']}`",
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔬 *Bundle Checker ULTIMATE*\n\n"
        "Most advanced free Solana bundle detector.\n\n"
        "*Signals:*\n"
        "🪢 Bundle clustering\n"
        "⚡ Same-block buys\n"
        "🎯 Sniper detection\n"
        "🆕 Fresh wallet flags\n"
        "🏦 CEX deposit detection\n"
        "🔁 Wash trading detection\n"
        "👨‍💻 Dev wallet tracker\n"
        "🔐 Mint + freeze authority\n"
        "⚠️ Copycat detection\n"
        "🔗 Social links\n"
        "💰 Price + market cap\n"
        "🚫 LP/bonding curve filtered\n\n"
        "Paste any CA or use `/bundle <CA>`",
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
        data = await analyse(mint)
        await msg.edit_text(report(data), parse_mode="Markdown")
    except Exception as e:
        logger.exception("analyse error")
        await msg.edit_text(f"❌ `{e}`", parse_mode="Markdown")


async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    b58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    if 32 <= len(text) <= 50 and all(c in b58 for c in text):
        context.args = [text]
        await cmd_bundle(update, context)
    else:
        await update.message.reply_text("Paste a CA or use `/bundle <CA>`", parse_mode="Markdown")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("bundle", cmd_bundle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    logger.info("Bundle Checker ULTIMATE started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
