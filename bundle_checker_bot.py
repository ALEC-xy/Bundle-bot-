"""
Solana Memecoin Bundle Checker PRO - Telegram Bot
Uses Helius DAS API for reliable holder + transaction data
"""

import os
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

PUMP_FUN_MINT_AUTH = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Helius RPC base ───────────────────────────────────────────────────────────

def helius_rpc_url():
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    return RPC_URL


async def rpc_post(client: httpx.AsyncClient, method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = await client.post(helius_rpc_url(), json=payload, timeout=30)
        r.raise_for_status()
        result = r.json().get("result")
        return result if result is not None else {}
    except Exception as e:
        logger.warning(f"RPC {method} error: {e}")
        return {}


# ── Helius REST API ───────────────────────────────────────────────────────────

async def helius_get_token_holders(client: httpx.AsyncClient, mint: str, limit: int = 20) -> list:
    """Use Helius getTokenAccounts — most reliable way to get holders."""
    if not HELIUS_API_KEY:
        return []
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccounts",
        "params": {
            "mint": mint,
            "limit": limit,
            "options": {"showZeroBalance": False}
        }
    }
    try:
        r = await client.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        accounts = data.get("result", {}).get("token_accounts", [])
        return accounts
    except Exception as e:
        logger.warning(f"helius getTokenAccounts error: {e}")
        return []


async def helius_get_transactions(client: httpx.AsyncClient, address: str, limit: int = 20) -> list:
    if not HELIUS_API_KEY:
        return []
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"helius transactions error: {e}")
        return []


async def helius_get_token_supply(client: httpx.AsyncClient, mint: str) -> float:
    result = await rpc_post(client, "getTokenSupply", [mint, {"commitment": "finalized"}])
    try:
        return float(result.get("value", {}).get("uiAmount") or 0)
    except Exception:
        return 0.0


async def helius_get_mint_info(client: httpx.AsyncClient, mint: str) -> dict:
    result = await rpc_post(client, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    try:
        return result.get("data", {}).get("parsed", {}).get("info", {})
    except Exception:
        return {}


async def get_signatures(client: httpx.AsyncClient, address: str, limit: int = 20) -> list:
    result = await rpc_post(client, "getSignaturesForAddress", [address, {"limit": limit}])
    return result if isinstance(result, list) else []


async def get_transaction(client: httpx.AsyncClient, sig: str) -> dict:
    return await rpc_post(client, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])


# ── Detection helpers ─────────────────────────────────────────────────────────

async def detect_pump_fun(client: httpx.AsyncClient, mint: str) -> bool:
    mint_info = await helius_get_mint_info(client, mint)
    mint_auth = mint_info.get("mintAuthority", "")
    freeze_auth = mint_info.get("freezeAuthority", "")
    if PUMP_FUN_MINT_AUTH in (mint_auth, freeze_auth):
        return True
    # Check if mint address ends with "pump" (pump.fun pattern)
    if mint.endswith("pump"):
        return True
    return False


async def get_deployer_wallet(client: httpx.AsyncClient, mint: str) -> str | None:
    sigs = await get_signatures(client, mint, limit=50)
    if not sigs:
        return None
    oldest_sig = sigs[-1].get("signature") if sigs else None
    if not oldest_sig:
        return None
    tx = await get_transaction(client, oldest_sig)
    try:
        account_keys = tx["transaction"]["message"]["accountKeys"]
        for key in account_keys:
            if isinstance(key, dict) and key.get("signer") and key.get("writable"):
                return key.get("pubkey")
            elif isinstance(key, str):
                return key
    except (KeyError, TypeError, IndexError):
        pass
    # Helius fallback
    txs = await helius_get_transactions(client, mint, limit=1)
    if txs:
        return txs[-1].get("feePayer")
    return None


async def get_wallet_age_days(client: httpx.AsyncClient, wallet: str) -> float | None:
    sigs = await get_signatures(client, wallet, limit=1000)
    if not sigs:
        return None
    block_time = sigs[-1].get("blockTime")
    if not block_time:
        return None
    return round((datetime.now(timezone.utc).timestamp() - block_time) / 86400, 1)


async def get_funder_wallet(client: httpx.AsyncClient, wallet: str) -> str | None:
    txs = await helius_get_transactions(client, wallet, limit=10)
    if txs:
        for tx in reversed(txs):
            fee_payer = tx.get("feePayer")
            if fee_payer and fee_payer != wallet:
                return fee_payer
    sigs = await get_signatures(client, wallet, limit=5)
    return sigs[-1].get("signature", "")[:16] if sigs else None


async def get_launch_slot(client: httpx.AsyncClient, mint: str) -> int | None:
    sigs = await get_signatures(client, mint, limit=50)
    return sigs[-1].get("slot") if sigs else None


async def check_sniper(client: httpx.AsyncClient, wallet: str, launch_slot: int | None) -> bool:
    if not launch_slot or not HELIUS_API_KEY:
        return False
    txs = await helius_get_transactions(client, wallet, limit=20)
    for tx in txs:
        slot = tx.get("slot", 0)
        tx_type = tx.get("type", "")
        if slot and abs(slot - launch_slot) <= 2 and ("SWAP" in tx_type or "BUY" in tx_type):
            return True
    return False


async def check_holder_selling(client: httpx.AsyncClient, wallet: str, mint: str) -> bool:
    if not HELIUS_API_KEY:
        return False
    txs = await helius_get_transactions(client, wallet, limit=10)
    for tx in txs:
        for transfer in tx.get("tokenTransfers", []):
            if transfer.get("mint") == mint and transfer.get("fromUserAccount") == wallet:
                return True
    return False


# ── Main analysis ─────────────────────────────────────────────────────────────

async def analyse_bundle(mint: str) -> dict:
    async with httpx.AsyncClient() as client:

        # Parallel: supply, holders, pump detection, deployer, launch slot
        supply, raw_holders, is_pump, deployer, launch_slot = await asyncio.gather(
            helius_get_token_supply(client, mint),
            helius_get_token_holders(client, mint, limit=20),
            detect_pump_fun(client, mint),
            get_deployer_wallet(client, mint),
            get_launch_slot(client, mint),
        )

        if supply == 0:
            return {"error": "Could not fetch token supply. Check the CA is correct."}

        if not raw_holders:
            # Fallback to standard RPC largest accounts
            result = await rpc_post(client, "getTokenLargestAccounts", [mint, {"commitment": "finalized"}])
            accounts = result.get("value", [])
            if not accounts:
                return {"error": "No holder data found. Token may be too new or invalid."}

            # Resolve owners
            async def resolve_owner(ta):
                res = await rpc_post(client, "getAccountInfo", [ta["address"], {"encoding": "jsonParsed"}])
                try:
                    owner = res["data"]["parsed"]["info"]["owner"]
                except (KeyError, TypeError):
                    owner = None
                return {
                    "owner": owner,
                    "amount": float(ta.get("uiAmount") or ta.get("uiAmountString") or 0),
                }
            holders = await asyncio.gather(*[resolve_owner(ta) for ta in accounts[:20]])
            holders = [h for h in holders if h["owner"]]
        else:
            holders = [
                {
                    "owner": h.get("owner"),
                    "amount": float(h.get("amount", 0)) / (10 ** 6),  # adjust decimals
                }
                for h in raw_holders if h.get("owner")
            ]
            # Re-sort by amount descending
            holders.sort(key=lambda x: x["amount"], reverse=True)

        if not holders:
            return {"error": "Could not resolve any holder wallets."}

        # Recalculate supply from holders if supply was 0
        if supply == 0:
            supply = sum(h["amount"] for h in holders) * 1.1

        top10_pct = sum(h["amount"] for h in holders[:10]) / supply * 100 if supply else 0
        top20_pct = sum(h["amount"] for h in holders[:20]) / supply * 100 if supply else 0

        # Deployer holdings
        deployer_pct = 0
        if deployer:
            for h in holders:
                if h["owner"] == deployer:
                    deployer_pct = h["amount"] / supply * 100
                    break

        # Enrich top 15
        top15 = holders[:15]
        wallet_list = [h["owner"] for h in top15]

        ages, funders, snipers, selling = await asyncio.gather(
            asyncio.gather(*[get_wallet_age_days(client, w) for w in wallet_list]),
            asyncio.gather(*[get_funder_wallet(client, w) for w in wallet_list]),
            asyncio.gather(*[check_sniper(client, w, launch_slot) for w in wallet_list]),
            asyncio.gather(*[check_holder_selling(client, w, mint) for w in wallet_list]),
        )

        for i, h in enumerate(top15):
            h["age_days"] = ages[i]
            h["funder"] = funders[i]
            h["is_sniper"] = snipers[i]
            h["is_selling"] = selling[i]
            h["is_fresh"] = ages[i] is not None and ages[i] < 7

        # Bundle groups
        funding_map = defaultdict(list)
        for h in top15:
            if h.get("funder"):
                funding_map[h["funder"]].append(h["owner"])
        bundle_groups = [w for w in funding_map.values() if len(w) >= 2]
        bundled_wallets = list(set(w for g in bundle_groups for w in g))
        bundled_pct = sum(h["amount"] for h in holders if h["owner"] in bundled_wallets) / supply * 100 if supply else 0

        sniper_wallets = [h for h in top15 if h.get("is_sniper")]
        sniper_pct = sum(h["amount"] for h in sniper_wallets) / supply * 100 if supply else 0
        fresh_wallets = [h for h in top15 if h.get("is_fresh")]
        fresh_pct = sum(h["amount"] for h in fresh_wallets) / supply * 100 if supply else 0
        selling_count = sum(1 for h in top15 if h.get("is_selling"))

        return {
            "mint": mint,
            "supply": supply,
            "holders": holders,
            "top10_pct": top10_pct,
            "top20_pct": top20_pct,
            "is_pump_fun": is_pump,
            "deployer": deployer,
            "deployer_pct": deployer_pct,
            "bundle_groups": bundle_groups,
            "bundled_wallets": bundled_wallets,
            "bundled_pct": bundled_pct,
            "sniper_count": len(sniper_wallets),
            "sniper_pct": sniper_pct,
            "fresh_wallet_count": len(fresh_wallets),
            "fresh_pct": fresh_pct,
            "selling_count": selling_count,
        }


# ── Scoring ───────────────────────────────────────────────────────────────────

def compute_score(data: dict) -> tuple[int, str, str]:
    score = 0
    score += min(data["top10_pct"] * 0.5, 25)
    score += min(data["bundled_pct"] * 0.8, 20)
    score += min(len(data["bundle_groups"]) * 4, 15)
    score += min(data["sniper_pct"] * 1.2, 15)
    score += min(data["fresh_pct"] * 0.6, 10)
    score += min(data["deployer_pct"] * 0.5, 10)
    if data["is_pump_fun"]:
        score += 5
    score = min(int(score), 100)

    if score >= 75:
        return score, "EXTREMELY BUNDLED", "🔴"
    elif score >= 55:
        return score, "HEAVILY BUNDLED", "🟠"
    elif score >= 35:
        return score, "MODERATELY BUNDLED", "🟡"
    elif score >= 15:
        return score, "SLIGHTLY BUNDLED", "🟢"
    else:
        return score, "LOOKS CLEAN", "✅"


# ── Report ────────────────────────────────────────────────────────────────────

def format_report(data: dict) -> str:
    if "error" in data:
        return f"❌ *Error:* {data['error']}"

    score, risk_label, emoji = compute_score(data)
    supply = data["supply"]
    filled = int(score / 10)
    bar = "█" * filled + "░" * (10 - filled)
    dev = data["deployer"]
    dev_str = f"`{dev[:6]}...{dev[-4:]}`" if dev else "`unknown`"

    lines = [
        f"🔬 *BUNDLE ANALYSIS REPORT*",
        f"`{data['mint']}`",
        f"",
        f"{emoji} *{risk_label}*",
        f"`[{bar}] {score}/100`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 *CONCENTRATION*",
        f"├ Top 10: `{data['top10_pct']:.1f}%`  Top 20: `{data['top20_pct']:.1f}%`",
        f"",
        f"🪢 *BUNDLE CLUSTERS*",
        f"├ Groups sharing a funder: `{len(data['bundle_groups'])}`",
        f"├ Bundled wallets: `{len(data['bundled_wallets'])}`",
        f"└ Bundled supply: `{data['bundled_pct']:.1f}%`",
        f"",
        f"🎯 *SNIPERS* _(bought block 1-2)_",
        f"├ Sniper wallets: `{data['sniper_count']}`",
        f"└ Sniper supply: `{data['sniper_pct']:.1f}%`",
        f"",
        f"🆕 *FRESH WALLETS* _(< 7 days old)_",
        f"├ Count: `{data['fresh_wallet_count']}`",
        f"└ Supply held: `{data['fresh_pct']:.1f}%`",
        f"",
        f"👨‍💻 *DEV WALLET*",
        f"├ Address: {dev_str}",
        f"└ Still holding: `{data['deployer_pct']:.1f}%`",
        f"",
        f"🌊 *PUMP.FUN*: {'`YES ⚠️`' if data['is_pump_fun'] else '`NO`'}",
        f"📉 *ACTIVE SELLERS* in top 15: `{data['selling_count']}`",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏆 *TOP 5 HOLDERS*",
    ]

    for i, h in enumerate(data["holders"][:5], 1):
        pct = h["amount"] / supply * 100 if supply else 0
        owner = h["owner"]
        short = f"`{owner[:6]}...{owner[-4:]}`"
        flags = []
        if owner in data["bundled_wallets"]: flags.append("🪢")
        if h.get("is_sniper"): flags.append("🎯")
        if h.get("is_fresh"): flags.append("🆕")
        if h.get("is_selling"): flags.append("📉")
        if dev and owner == dev: flags.append("👨‍💻")
        age = f"{h['age_days']}d" if h.get("age_days") is not None else "?d"
        lines.append(f"{i}. {short} `{pct:.2f}%` _{age}_ {' '.join(flags)}")

    lines += [
        f"",
        f"_🪢bundle 🎯sniper 🆕fresh 📉selling 👨‍💻dev_",
        f"_Data: Helius + Solana RPC_",
    ]

    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔬 *Bundle Checker PRO*\n\n"
        "Advanced Solana memecoin bundle detector.\n\n"
        "*Signals:*\n"
        "🪢 Shared funder clustering\n"
        "🎯 Block 1-2 sniper detection\n"
        "🆕 Fresh wallet flags\n"
        "👨‍💻 Dev wallet tracker\n"
        "🌊 Pump.fun detection\n"
        "📉 Active sell pressure\n\n"
        "*Usage:* `/bundle <CA>` or just paste a CA",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "`/bundle <CA>` — Full analysis\n"
        "`/start` — Welcome\n\n"
        "*Score:*\n"
        "✅ 0-14 Clean\n"
        "🟢 15-34 Slightly bundled\n"
        "🟡 35-54 Moderate\n"
        "🟠 55-74 Heavy\n"
        "🔴 75+ Extremely bundled",
        parse_mode="Markdown",
    )


async def cmd_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: `/bundle <CA>`", parse_mode="Markdown")
        return

    mint = args[0].strip()
    if len(mint) < 32 or len(mint) > 50:
        await update.message.reply_text("❌ Invalid address length.")
        return

    msg = await update.message.reply_text(
        f"🔬 *Scanning* `{mint[:8]}...`\n\n"
        f"Fetching holders, checking bundles...\n"
        f"_(15-30s)_",
        parse_mode="Markdown"
    )

    try:
        data = await analyse_bundle(mint)
        report = format_report(data)
        await msg.edit_text(report, parse_mode="Markdown")
    except httpx.ReadTimeout:
        await msg.edit_text("⏱ Timeout. Try again.")
    except Exception as e:
        logger.exception("Bundle error")
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    b58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    if 32 <= len(text) <= 50 and all(c in b58 for c in text):
        context.args = [text]
        await cmd_bundle(update, context)
    else:
        await update.message.reply_text(
            "Paste a Solana CA or use `/bundle <CA>`",
            parse_mode="Markdown"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("bundle", cmd_bundle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🔬 Bundle Checker PRO started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
