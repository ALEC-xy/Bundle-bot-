"""
Solana Bundle Checker PRO - Telegram Bot
Complete rewrite using correct Helius API
"""

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

o
# ── Core RPC ──────────────────────────────────────────────────────────────────

async def rpc(client: httpx.AsyncClient, method: str, params: list) -> any:
    try:
        r = await client.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }, timeout=30)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.warning(f"RPC {method}: {e}")
        return None


# ── Get token holders via Helius getTokenAccounts ─────────────────────────────

async def get_holders(client: httpx.AsyncClient, mint: str) -> list:
    """
    Helius-specific method: getTokenAccounts
    Returns list of {owner, amount}
    """
    if not HELIUS_API_KEY:
        logger.warning("No Helius API key")
        return []

    all_accounts = []
    cursor = None

    for _ in range(3):  # max 3 pages
        params = {
            "mint": mint,
            "limit": 100,
            "options": {"showZeroBalance": False}
        }
        if cursor:
            params["cursor"] = cursor

        try:
            r = await client.post(HELIUS_RPC, json={
                "jsonrpc": "2.0",
                "id": "helius-test",
                "method": "getTokenAccounts",
                "params": params
            }, timeout=30)
            r.raise_for_status()
            data = r.json()

            if "error" in data:
                logger.warning(f"getTokenAccounts error: {data['error']}")
                break

            result = data.get("result", {})
            accounts = result.get("token_accounts", [])
            all_accounts.extend(accounts)

            cursor = result.get("cursor")
            if not cursor or len(accounts) < 100:
                break

        except Exception as e:
            logger.warning(f"getTokenAccounts page error: {e}")
            break

    if not all_accounts:
        return []

    # Convert to standard format and sort by amount
    holders = []
    for acc in all_accounts:
        try:
            amount = float(acc.get("amount", 0))
            owner = acc.get("owner", "")
            if owner and amount > 0:
                holders.append({"owner": owner, "amount": amount})
        except Exception:
            continue

    # Filter out known program/bonding curve addresses
EXCLUDE = {
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # pump.fun bonding curve
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",  # pump.fun fee account
    "CebN5WGQ4jvEPvsVU4EoHEpgznyZKRC8HCeWoUpieTpq",  # pump.fun authority
    "4wTV81svKSf3iFE5qFJi9gGqpvunfHcqgSJ1AXcE4czP",  # pump.fun migration
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # token program
    "11111111111111111111111111111111",                 # system program
}
holders = [h for h in holders if h["owner"] not in EXCLUDE]

    return holders


async def get_supply(client: httpx.AsyncClient, mint: str) -> float:
    result = await rpc(client, "getTokenSupply", [mint])
    if not result:
        return 0.0
    try:
        info = result.get("value", {})
        ui = info.get("uiAmount")
        if ui:
            return float(ui)
        # Calculate from raw amount + decimals
        amount = int(info.get("amount", 0))
        decimals = int(info.get("decimals", 6))
        return amount / (10 ** decimals)
    except Exception:
        return 0.0


async def get_mint_authority(client: httpx.AsyncClient, mint: str) -> str:
    result = await rpc(client, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    try:
        return result["data"]["parsed"]["info"].get("mintAuthority", "")
    except Exception:
        return ""


async def get_signatures(client: httpx.AsyncClient, address: str, limit: int = 20) -> list:
    result = await rpc(client, "getSignaturesForAddress", [address, {"limit": limit}])
    return result if isinstance(result, list) else []


async def get_helius_txs(client: httpx.AsyncClient, address: str, limit: int = 10) -> list:
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


# ── Analysis helpers ──────────────────────────────────────────────────────────

async def get_wallet_age(client: httpx.AsyncClient, wallet: str) -> float | None:
    sigs = await get_signatures(client, wallet, limit=1000)
    if not sigs:
        return None
    bt = sigs[-1].get("blockTime")
    if not bt:
        return None
    return round((datetime.now(timezone.utc).timestamp() - bt) / 86400, 1)


async def get_funder(client: httpx.AsyncClient, wallet: str) -> str | None:
    txs = await get_helius_txs(client, wallet, limit=10)
    for tx in reversed(txs or []):
        fp = tx.get("feePayer")
        if fp and fp != wallet:
            return fp
    sigs = await get_signatures(client, wallet, limit=5)
    if sigs:
        return sigs[-1].get("signature", "")[:20]
    return None


async def get_deployer(client: httpx.AsyncClient, mint: str) -> str | None:
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


async def get_launch_slot(client: httpx.AsyncClient, mint: str) -> int | None:
    sigs = await get_signatures(client, mint, limit=50)
    return sigs[-1].get("slot") if sigs else None


async def is_selling(client: httpx.AsyncClient, wallet: str, mint: str) -> bool:
    txs = await get_helius_txs(client, wallet, limit=10)
    for tx in (txs or []):
        for t in tx.get("tokenTransfers", []):
            if t.get("mint") == mint and t.get("fromUserAccount") == wallet:
                return True
    return False


async def is_sniper(client: httpx.AsyncClient, wallet: str, launch_slot: int | None) -> bool:
    if not launch_slot:
        return False
    txs = await get_helius_txs(client, wallet, limit=20)
    for tx in (txs or []):
        slot = tx.get("slot", 0)
        ttype = tx.get("type", "")
        if slot and abs(slot - launch_slot) <= 2 and ("SWAP" in ttype or "BUY" in ttype):
            return True
    return False


# ── Main analysis ─────────────────────────────────────────────────────────────

async def analyse(mint: str) -> dict:
    async with httpx.AsyncClient() as client:

        supply, holders, mint_auth, deployer, launch_slot = await asyncio.gather(
            get_supply(client, mint),
            get_holders(client, mint),
            get_mint_authority(client, mint),
            get_deployer(client, mint),
            get_launch_slot(client, mint),
        )

        if not holders:
            return {"error": "No holder data found. Token may not exist or have no holders yet."}

        # Normalize amounts using actual supply
        total_from_holders = sum(h["amount"] for h in holders)

        # If supply fetch failed, estimate from holders
        if supply == 0:
            supply = total_from_holders * 1.05

        # Normalize: holders amounts may be raw (need decimals adjustment)
        # Try to detect if amounts look raw vs UI
        if total_from_holders > supply * 100:
            # amounts are raw, divide by 10^6
            for h in holders:
                h["amount"] = h["amount"] / 1_000_000
            total_from_holders = sum(h["amount"] for h in holders)

        top10_pct = sum(h["amount"] for h in holders[:10]) / supply * 100 if supply else 0
        top20_pct = sum(h["amount"] for h in holders[:20]) / supply * 100 if supply else 0

        # Pump.fun detection
        PUMP_AUTH = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
        is_pump = mint_auth == PUMP_AUTH or mint.endswith("pump")

        # Deployer holdings
        deployer_pct = 0
        if deployer:
            for h in holders:
                if h["owner"] == deployer:
                    deployer_pct = h["amount"] / supply * 100
                    break

        # Enrich top 15
        top15 = holders[:15]
        wallets = [h["owner"] for h in top15]

        ages, funders, snipers, selling = await asyncio.gather(
            asyncio.gather(*[get_wallet_age(client, w) for w in wallets]),
            asyncio.gather(*[get_funder(client, w) for w in wallets]),
            asyncio.gather(*[is_sniper(client, w, launch_slot) for w in wallets]),
            asyncio.gather(*[is_selling(client, w, mint) for w in wallets]),
        )

        for i, h in enumerate(top15):
            h["age_days"] = ages[i]
            h["funder"] = funders[i]
            h["is_sniper"] = snipers[i]
            h["is_selling"] = selling[i]
            h["is_fresh"] = ages[i] is not None and ages[i] < 7

        # Bundle detection
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

        return {
            "mint": mint,
            "supply": supply,
            "holders": holders,
            "holder_count": len(holders),
            "top10_pct": top10_pct,
            "top20_pct": top20_pct,
            "is_pump": is_pump,
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
        }


# ── Score ─────────────────────────────────────────────────────────────────────

def score(data: dict) -> tuple[int, str, str]:
    s = 0
    s += min(data["top10_pct"] * 0.5, 25)
    s += min(data["bundled_pct"] * 0.8, 20)
    s += min(len(data["bundle_groups"]) * 4, 15)
    s += min(data["sniper_pct"] * 1.2, 15)
    s += min(data["fresh_pct"] * 0.6, 10)
    s += min(data["deployer_pct"] * 0.5, 10)
    if data["is_pump"]: s += 5
    s = min(int(s), 100)
    if s >= 75: return s, "EXTREMELY BUNDLED", "🔴"
    if s >= 55: return s, "HEAVILY BUNDLED", "🟠"
    if s >= 35: return s, "MODERATELY BUNDLED", "🟡"
    if s >= 15: return s, "SLIGHTLY BUNDLED", "🟢"
    return s, "LOOKS CLEAN", "✅"


# ── Report ────────────────────────────────────────────────────────────────────

def report(data: dict) -> str:
    if "error" in data:
        return f"❌ *Error:* {data['error']}"

    sc, label, emoji = score(data)
    bar = "█" * (sc // 10) + "░" * (10 - sc // 10)
    dev = data["deployer"]
    dev_str = f"`{dev[:6]}...{dev[-4:]}`" if dev else "`unknown`"
    supply = data["supply"]

    lines = [
        f"🔬 *BUNDLE REPORT*",
        f"`{data['mint']}`",
        f"",
        f"{emoji} *{label}*",
        f"`[{bar}] {sc}/100`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 *CONCENTRATION* _(checked {data['holder_count']} holders)_",
        f"├ Top 10: `{data['top10_pct']:.1f}%`",
        f"└ Top 20: `{data['top20_pct']:.1f}%`",
        f"",
        f"🪢 *BUNDLES*",
        f"├ Clusters: `{len(data['bundle_groups'])}`",
        f"├ Wallets: `{len(data['bundled'])}`",
        f"└ Supply: `{data['bundled_pct']:.1f}%`",
        f"",
        f"🎯 *SNIPERS*",
        f"├ Count: `{data['sniper_count']}`",
        f"└ Supply: `{data['sniper_pct']:.1f}%`",
        f"",
        f"🆕 *FRESH WALLETS* _(<7d)_",
        f"├ Count: `{data['fresh_count']}`",
        f"└ Supply: `{data['fresh_pct']:.1f}%`",
        f"",
        f"👨‍💻 *DEV*: {dev_str} holding `{data['deployer_pct']:.1f}%`",
        f"🌊 *PUMP.FUN*: {'`YES ⚠️`' if data['is_pump'] else '`NO`'}",
        f"📉 *SELLERS*: `{data['sell_count']}` of top 15",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏆 *TOP 5*",
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
        if dev and owner == dev: flags.append("👨‍💻")
        age = f"{h['age_days']}d" if h.get("age_days") is not None else "?"
        lines.append(f"{i}. {short} `{pct:.2f}%` _{age}_ {' '.join(flags)}")

    lines += ["", "_🪢bundle 🎯sniper 🆕fresh 📉sell 👨‍💻dev_"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔬 *Bundle Checker PRO*\n\nPaste any Solana CA or use `/bundle <CA>`",
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
        f"🔬 Scanning `{mint[:8]}...` _(20-30s)_", parse_mode="Markdown"
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("bundle", cmd_bundle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
