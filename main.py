import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
import oandapyV20
import oandapyV20.endpoints.pricing as pricing
from concurrent.futures import ThreadPoolExecutor
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENVIRONMENT = "practice"

TG_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage" if TELEGRAM_TOKEN else ""
TG_UPDATES_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates" if TELEGRAM_TOKEN else ""
TREASURY_URL = "https://api.treasury.id/api/v1/antigrvty/gold/rate"
GOOGLE_FX_URL = "https://www.google.com/finance/quote/USD-IDR"

RE_PRICE = re.compile(r'data-last-price="([0-9.,]+)"')

TREASURY_HEADERS = {
    "User-Agent": "Mozilla/5.0", "Accept": "application/json",
    "Content-Type": "application/json", "Origin": "https://treasury.id",
}
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0"}

TIMEOUT_FAST = aiohttp.ClientTimeout(total=1)
TIMEOUT_SEND = aiohttp.ClientTimeout(total=2)
TIMEOUT_SLOW = aiohttp.ClientTimeout(total=3)

WIB = timezone(timedelta(hours=7))
DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")

HARI_INDONESIA = {
    'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu',
    'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu',
}

nominals = [
    (10_000_000, 9_669_000),
    (20_000_000, 19_330_000),
    (30_000_000, 28_995_000),
    (40_000_000, 38_660_000),
    (50_000_000, 48_325_000),
]

last_treasury_buy = None
last_treasury_update = None
custom_message = ""
last_update_id = 0
cached_usd_idr = None
cached_xau_usd = None

oanda_executor = ThreadPoolExecutor(max_workers=1)
_oanda_api = None


def get_oanda_api():
    global _oanda_api
    if _oanda_api is None and OANDA_ACCESS_TOKEN:
        _oanda_api = oandapyV20.API(access_token=OANDA_ACCESS_TOKEN, environment=OANDA_ENVIRONMENT)
    return _oanda_api


def is_weekend_quiet():
    now = datetime.now(WIB)
    wd = now.weekday()
    h, m = now.hour, now.minute

    if wd == 5:
        return h > 5 or (h == 5 and m >= 2)
    if wd == 6:
        return True
    if wd == 0:
        if h < 5 or (h == 5 and m <= 58):
            return True
        return False
    if h == 5 and m >= 2 and m <= 58:
        return True
    return False


def format_tanggal_indo(updated_at_str):
    try:
        clean = updated_at_str.split('+')[0].split('Z')[0]
        for fmt in DT_FORMATS:
            try:
                dt = datetime.strptime(clean, fmt)
                return f"{HARI_INDONESIA.get(dt.strftime('%A'), dt.strftime('%A'))} {dt.strftime('%H:%M:%S')}"
            except ValueError:
                continue
    except Exception:
        pass
    return updated_at_str


def format_id_number(number, decimal_places=0):
    if number is None:
        return "N/A"
    num = float(number)
    integer_part = int(num)
    formatted_int = f"{integer_part:,}".replace(',', '.')
    if decimal_places > 0:
        decimal_part = f"{num:.{decimal_places}f}".split('.')[1]
        return f"{formatted_int},{decimal_part}"
    return formatted_int


def get_status(new, old):
    if old is None:
        return "Baru"
    diff = new - old
    if diff > 0:
        return f"🟢Naik🚀 +{format_id_number(diff)} rupiah"
    if diff < 0:
        return f"🔴Turun🔻 -{format_id_number(-diff)} rupiah"
    return "━ Tetap"


def calc_spread(buy, sell):
    if buy and sell and buy > 0:
        spread_percent = ((buy - sell) / buy) * 100
        return f"({spread_percent:.2f}%)"
    return ""


def calc_profit(nominal, modal, buy, sell):
    gram = nominal / buy
    selisih = (gram * sell) - modal
    if selisih >= 0:
        return gram, selisih, "🟢", "+"
    return gram, -selisih, "🔴", "-"


def build_message(new_buy, new_sell, status_msg, tanggal_indo, xau, usd, custom):
    spread = calc_spread(new_buy, new_sell)
    parts = [
        "<b>", status_msg, "</b>\n\n",
        "<b>", tanggal_indo, " WIB</b>\n\n",
        "Beli: <b>Rp ", format_id_number(new_buy), "</b> ",
        "Jual: <b>Rp ", format_id_number(new_sell), "</b> <b>", spread, "</b>\n\n",
    ]
    for nominal, modal in nominals:
        gram, selisih, warna, sign = calc_profit(nominal, modal, new_buy, new_sell)
        parts.append(f"🥇 {nominal // 1_000_000} JT ➺ {gram:.4f}gr {warna} {sign}<b>Rp {format_id_number(selisih)}</b>\n")
    parts.append(f"\nHarga XAU: <b>{format_id_number(xau, 3)}</b> USD: <b>{format_id_number(usd, 4)}</b>")
    if custom:
        parts.append(f"\n\n<b>{custom}</b>")
    return "".join(parts)


async def send_telegram(session, message):
    if not TG_SEND_URL:
        return
    try:
        async with session.post(
            TG_SEND_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=TIMEOUT_SEND,
        ) as resp:
            await resp.read()
    except Exception:
        pass


async def get_telegram_updates(session):
    global last_update_id, custom_message
    if not TG_UPDATES_URL:
        return
    try:
        async with session.get(
            TG_UPDATES_URL,
            params={"offset": last_update_id + 1, "timeout": 0},
            timeout=TIMEOUT_FAST,
        ) as resp:
            if resp.status == 200:
                for update in (await resp.json()).get("result", []):
                    last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "")
                    if text.startswith("/atur "):
                        custom_message = text[6:].strip()
    except Exception:
        pass


async def fetch_single_treasury(session):
    try:
        async with session.post(TREASURY_URL, headers=TREASURY_HEADERS,
                                timeout=TIMEOUT_FAST) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and data.get("data"):
                    return data
    except Exception:
        pass
    return None


async def fetch_treasury_fastest(session, n=5):
    tasks = [asyncio.create_task(fetch_single_treasury(session)) for _ in range(n)]
    result = None
    try:
        while tasks:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = list(pending)
            for t in done:
                try:
                    r = t.result()
                    if r is not None:
                        result = r
                        return result
                except Exception:
                    pass
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return result


def fetch_oanda_sync():
    try:
        api = get_oanda_api()
        if api is None:
            return None
        r = pricing.PricingInfo(accountID=OANDA_ACCOUNT_ID, params={"instruments": "XAU_USD"})
        api.request(r)
        prices = r.response.get('prices') if r.response else None
        if prices:
            p = prices[0]
            return (float(p['bids'][0]['price']) + float(p['asks'][0]['price'])) / 2
    except Exception:
        pass
    return None


async def update_secondary_data(session):
    global cached_usd_idr, cached_xau_usd
    while True:
        try:
            async with session.get(GOOGLE_FX_URL, headers=BROWSER_HEADERS,
                                   timeout=TIMEOUT_SLOW) as resp:
                if resp.status == 200:
                    match = RE_PRICE.search(await resp.text())
                    if match:
                        cached_usd_idr = float(match.group(1).replace(",", ""))
        except Exception:
            pass

        try:
            if OANDA_ACCESS_TOKEN:
                result = await asyncio.get_running_loop().run_in_executor(
                    oanda_executor, fetch_oanda_sync
                )
                if result is not None:
                    cached_xau_usd = result
        except Exception:
            pass

        await asyncio.sleep(5)


async def main_async():
    global last_treasury_buy, last_treasury_update

    print("Bot berjalan... Menunggu perubahan harga Treasury.")

    connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=60, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        asyncio.create_task(update_secondary_data(session))

        counter = 0
        pending_tasks = set()

        while True:
            treasury_data = await fetch_treasury_fastest(session, n=5)

            if treasury_data:
                data = treasury_data["data"]
                buy_rate = data.get("buying_rate")
                sell_rate = data.get("selling_rate")
                updated_at = data.get("updated_at")

                if buy_rate is not None and sell_rate is not None and updated_at is not None:
                    new_buy = int(buy_rate)
                    new_sell = int(sell_rate)

                    if last_treasury_update is None or updated_at > last_treasury_update:
                        price_changed = last_treasury_buy is None or new_buy != last_treasury_buy

                        if not is_weekend_quiet() or price_changed:
                            status_msg = get_status(new_buy, last_treasury_buy)
                            tanggal_indo = format_tanggal_indo(updated_at)
                            msg = build_message(
                                new_buy, new_sell, status_msg, tanggal_indo,
                                cached_xau_usd, cached_usd_idr, custom_message,
                            )
                            print(f"Mengirim update... {updated_at}")
                            task = asyncio.create_task(send_telegram(session, msg))
                            pending_tasks.add(task)
                            task.add_done_callback(pending_tasks.discard)
                        else:
                            print(f"Weekend harga tetap, skip kirim. {updated_at}")

                        last_treasury_buy = new_buy
                        last_treasury_update = updated_at

            counter += 1
            if counter >= 100:
                counter = 0
                task = asyncio.create_task(get_telegram_updates(session))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            await asyncio.sleep(0)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nBot dihentikan.")
    finally:
        oanda_executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
