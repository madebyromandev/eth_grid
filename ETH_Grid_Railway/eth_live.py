"""Bybit spot ETH/USDT. Python 3.9+. Local commands: check, run, deposit, status."""
import argparse
import os
import fcntl
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from decimal import Decimal as D, ROUND_CEILING
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PAIR = {"category": "spot", "symbol": "ETHUSDT"}
STEP, TARGET, ORDER, KEEP = D(".03"), D(".03"), D(10), D(2)
SLIP = D(".0005")
DB = Path("/data/eth_live.sqlite")


def encode(x):
    return {"decimal": str(x)}


def decode(x):
    return D(x["decimal"]) if set(x) == {"decimal"} else x


def floor(x, unit):
    return (x // unit) * unit


class API:
    def __init__(self, region):
        self.host = "https://api.bybit." + region
        self.key = os.getenv("BYBIT_API_KEY", "").strip()
        self.secret = os.getenv("BYBIT_API_SECRET", "").strip().encode()
        if not self.key or not self.secret:
            raise ValueError("Пустой ключ")

    def call(self, path, params, private=True, post=False):
        data = json.dumps(params, separators=(",", ":")) if post else urlencode(params)
        headers = {"Content-Type": "application/json"}
        if private:
            stamp = str(int(time.time() * 1000))
            signature = hmac.new(self.secret, (stamp+self.key+"5000"+data).encode(),
                                 hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": stamp,
                            "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN": signature})
        url = self.host + "/v5/" + path
        req = Request(url if post else url+"?"+data, data=data.encode() if post else None,
                      headers=headers)
        with urlopen(req, timeout=12) as response:
            result = json.load(response)
        if result["retCode"]:
            raise RuntimeError(f"Bybit {result['retCode']}: {result['retMsg']}")
        if not post and abs(time.time() - int(result["time"])/1000) > 30:
            raise ValueError("Проверьте часы Windows и свежесть данных")
        return result["result"]

    def balances(self):
        coins = self.call("account/wallet-balance", {"accountType":"UNIFIED",
                          "coin":"ETH,USDT"})["list"][0]["coin"]
        return {c["coin"]: D(c["walletBalance"]) - D(c.get("locked") or 0)
                - D(c.get("borrowAmount") or 0) for c in coins}

    def setup(self):
        info = self.call("user/query-api", {})
        perms = info["permissions"]
        if int(info["readOnly"]) or "SpotTrade" not in perms.get("Spot", []):
            raise ValueError("Нужно разрешение SpotTrade")
        if not info.get("userID"):
            raise ValueError("Не удалось проверить UID")
        self.owner = self.host + "/" + str(info["userID"])
        item = self.call("market/instruments-info", PAIR, False)["list"][0]
        if item["status"] != "Trading":
            raise ValueError("Торги ETHUSDT недоступны")
        self.unit = D(item["lotSizeFilter"]["basePrecision"])
        self.tick = D(item["priceFilter"]["tickSize"])
        self.minimum = D(item["lotSizeFilter"]["minOrderAmt"])
        self.fee = D(self.call("account/fee-rate", PAIR)["list"][0]["takerFeeRate"])
        if not 0 <= self.fee < D(".02") or self.minimum > ORDER:
            raise ValueError("Проверьте комиссию и минимальный размер заявки")

    def quote(self):
        ticker = self.call("market/tickers", PAIR, False)["list"][0]
        bid, ask = D(ticker["bid1Price"]), D(ticker["ask1Price"])
        if not 0 < bid <= ask:
            raise ValueError("Ошибка котировки")
        return bid, ask


def initial():
    return dict(capital=D(100), cash=D(100), profit=D(0), lots=[], dust=D(0),
                pending=None, owner=None, anchor=None, previous=None, peak=None)


def transaction(db, action):
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM wallet WHERE id=1").fetchone()
        state = json.loads(row[0], object_hook=decode) if row else initial()
        result = action(state)
        updated = json.dumps(state, default=encode)
        if not row or updated != row[0]:
            db.execute("INSERT OR REPLACE INTO wallet VALUES(1,?)", (updated,))
    return result


def decide(s, api, bid, ask):
    prev = s["previous"]
    s["previous"] = ask
    sell_price = (bid*(1-SLIP)/api.tick).to_integral_value(rounding=ROUND_CEILING)*api.tick
    net = sell_price*(1-api.fee)
    lots = s["lots"]
    equity = s["cash"] + sum(floor(l["qty"], api.unit)*net for l in lots)
    for lot in lots:
        qty = floor(lot["qty"], api.unit)
        if qty*sell_price < api.minimum:
            continue
        gain = qty*net-lot["cost"]  # conservatively charge all remaining cost
        target = sell_price >= lot["entry"]*(1+TARGET)
        covered = (sell_price <= lot["entry"]*(1-STEP)**3 and gain < 0
                   and s["profit"]+gain >= KEEP and equity-s["capital"] >= KEEP)
        if target or covered:
            return "Sell", qty, sell_price, lot["id"], lot["level"]
    level = None
    if not lots:
        s["peak"] = max(s["peak"] or ask, ask)
        if s["anchor"] is None or ask <= s["peak"]*(1-STEP):
            level = 0
    elif prev is not None and ask < prev:
        occupied = {l["level"] for l in lots}
        price, index = s["anchor"], 0
        while price >= ask:
            if price < prev and index not in occupied:
                level = index
            price *= 1-STEP
            index += 1
    if level is not None and s["cash"] >= ORDER:
        price = floor(ask*(1+SLIP), api.tick)
        qty = floor(ORDER / (price*(1+api.fee)), api.unit)
        if qty*price >= api.minimum:
            return "Buy", qty, price, None, level


def settle(s, order, api):
    p = s["pending"]
    if not p or p["link"] != order["orderLinkId"]:
        return
    if order["orderStatus"] not in ("Filled","Cancelled","Rejected","PartiallyFilledCanceled"):
        return
    qty = D(order.get("cumExecQty") or 0)
    if qty:
        value = D(order["cumExecValue"])
        fees = order["cumFeeDetail"]
        if not isinstance(fees, dict) or set(fees)-{"ETH","USDT"}:
            raise ValueError("Неизвестная валюта комиссии. Требуется сверка исполнения.")
        eth, usdt = D(fees.get("ETH",0)), D(fees.get("USDT",0))
        if p["side"] == "Buy":
            cost = value+usdt
            if not s["lots"]:
                s["anchor"] = value/qty
            s["lots"].append(dict(id=p["link"], level=p["level"], entry=value/qty,
                                  qty=qty-eth, cost=cost))
            s["cash"] -= cost
        else:
            lot = next(l for l in s["lots"] if l["id"] == p["lot"])
            sold = qty+eth
            if sold > lot["qty"]:
                raise ValueError("Количество ETH требует сверки")
            cost = lot["cost"] * sold/lot["qty"]
            lot["qty"] -= sold
            lot["cost"] -= cost
            s["cash"] += value-usdt
            s["profit"] += value-usdt-cost
            if floor(lot["qty"],api.unit)*p["price"] < api.minimum:
                s["dust"] += lot["qty"]
                s["profit"] -= lot["cost"]
                s["lots"].remove(lot)
            if not s["lots"]:
                s["peak"] = value/qty
    s["pending"] = None
    print(order["orderStatus"], p["side"], qty, "ETH", p["link"], flush=True)


def run(db, api):
    heartbeat = 0
    while True:
        if time.monotonic() >= heartbeat:
            print("Бот работает | ETH/USDT", flush=True)
            heartbeat = time.monotonic() + 900
        pending = transaction(db, lambda s:s["pending"])
        if pending:
            params = dict(PAIR, orderLinkId=pending["link"])
            for endpoint in ("order/realtime","order/history"):
                rows = api.call(endpoint, params)["list"]
                match = next((r for r in rows if r["orderLinkId"] == pending["link"]), None)
                if match:
                    transaction(db, lambda s:settle(s, match, api))
                    break
            else:
                print("Ожидание сверки заявки", pending["link"], flush=True)
        else:
            balances = api.balances()
            bid, ask = api.quote()
            def prepare(s):
                if s["pending"]:
                    return
                intent = decide(s, api, bid, ask)
                if not intent:
                    return
                side, qty, price, lot, level = intent
                if balances.get("USDT",0) < s["cash"] or (side=="Sell" and balances.get("ETH",0)<qty):
                    raise ValueError("Доступный баланс меньше выделенного боту")
                s["pending"] = dict(link=uuid.uuid4().hex, side=side, qty=qty,
                                    price=price, level=level, lot=lot)
                return s["pending"]
            p = transaction(db, prepare)
            if p:
                print("Отправка", p["side"], p["qty"], "ETH", p["link"], flush=True)
                api.call("order/create", dict(PAIR, side=p["side"], orderType="Limit",
                         timeInForce="IOC", qty=format(p["qty"],"f"), price=format(p["price"],"f"),
                         isLeverage=0, orderLinkId=p["link"]), post=True)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command",choices=["run","status","check","deposit"])
    parser.add_argument("amount",type=D,nargs="?")
    parser.add_argument("--region",choices=["kz","com"],default=os.getenv("BYBIT_REGION", "kz"))
    args = parser.parse_args()
    if args.region not in ("kz","com"):
        raise ValueError("BYBIT_REGION должен быть kz или com")
    if os.getenv("RAILWAY_VOLUME_MOUNT_PATH") != "/data" or not DB.parent.is_dir():
        raise ValueError("Подключите Railway Volume в /data")
    if args.command == "run":
        # Keep handle open until main exits. The kernel releases it on process exit.
        lock = (DB.parent / "worker.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = sqlite3.connect(DB,timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS wallet(id INTEGER PRIMARY KEY,data TEXT)")
    if args.command == "status":
        s = transaction(db,lambda s:s)
        print("Внесено:",s["capital"],"Свободно:",s["cash"],"Реализовано:",s["profit"],
              "Партий:",len(s["lots"]),"Остаток ETH округления:",s["dust"],"Ожидает:",s["pending"])
        return
    api = API(args.region)
    api.setup()
    balances = api.balances()
    bid, ask = api.quote()
    def verify(s):
        if s["owner"] not in (None,api.owner):
            raise ValueError("Эта база принадлежит другому аккаунту или региону")
        if args.command=="deposit":
            amount = args.amount
            if amount is None or not amount.is_finite() or amount<=0 or s["pending"]:
                raise ValueError("Проверьте сумму или дождитесь исполнения заявки")
            if balances.get("USDT",0)<s["cash"]+amount:
                raise ValueError("Сначала переведите USDT на единый торговый аккаунт")
            s["cash"] += amount
            s["capital"] += amount
        if not s["pending"] and balances.get("USDT",0)<s["cash"]:
            raise ValueError("Недостаточно свободных USDT для бюджета бота")
        if args.command!="check":
            s["owner"]=api.owner
        print("ETH/USDT",bid,ask,"| Бюджет",s["capital"],"| Свободно у бота",s["cash"])
    transaction(db,verify)
    if args.command=="run":
        run(db,api)


if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлен. Состояние сохранено.")
    except Exception as exc:
        raise SystemExit(f"Остановлен: {exc}")
