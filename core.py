"""Persistent offer collection, ranking, delivery and scheduling."""
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
DEFAULTS = dict(keyword="", category_id="", min_rating=4.5, relax_rating=False,
                fallback_rating=4.0, min_price=0.0, max_price=500.0, min_sales=0,
                min_commission=0.0, repeat_days=7, per_category=2, per_shop=2,
                top_count=5, commission_weight=50, rating_weight=30, sales_weight=20,
                schedule_time="12:00", schedule_enabled=True, auto_send=False,
                max_pages=100, page_limit=20)
DECISIONS = {"new", "planned", "published", "discarded"}
API = "https://open-api.affiliate.shopee.com.br/graphql"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def load_env(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def safe_error(error):
    message = str(error)
    for key in ("SHOPEE_SECRET", "TELEGRAM_BOT_TOKEN", "PANEL_PASSWORD", "PANEL_SECRET"):
        value = os.environ.get(key)
        if value:
            message = message.replace(value, "[oculto]")
    return message[:1000]


def validate_settings(payload, existing=None):
    result = dict(existing or DEFAULTS)
    if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
        raise ValueError("Configuração desconhecida.")
    for key, value in payload.items():
        original = DEFAULTS[key]
        if isinstance(original, bool):
            if type(value) is not bool:
                raise ValueError(f"{key}: informe verdadeiro ou falso.")
        elif isinstance(original, int):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or int(value) != value:
                raise ValueError(f"{key}: informe um inteiro.")
            value = int(value)
        elif isinstance(original, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key}: número inválido.")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"{key}: texto inválido.")
        result[key] = value
    if len(result["keyword"]) > 120:
        raise ValueError("Palavra-chave: máximo de 120 caracteres.")
    if result["category_id"] and (not result["category_id"].isdigit() or int(result["category_id"]) > 2147483647):
        raise ValueError("Categoria inválida.")
    try:
        datetime.strptime(result["schedule_time"], "%H:%M")
    except ValueError:
        raise ValueError("Horário inválido. Use HH:MM.") from None
    if not 0 <= result["fallback_rating"] <= result["min_rating"] <= 5:
        raise ValueError("Notas devem estar entre 0 e 5; a alternativa não pode superar a mínima.")
    for key in ("min_price", "max_price", "min_sales", "min_commission"):
        if result[key] < 0:
            raise ValueError(f"{key}: não pode ser negativo.")
    if result["max_price"] and result["max_price"] < result["min_price"]:
        raise ValueError("Preço máximo menor que o mínimo.")
    for key, low, high in (("repeat_days", 0, 365), ("top_count", 1, 10),
                           ("per_category", 1, 10), ("per_shop", 1, 10),
                           ("max_pages", 1, 500), ("page_limit", 1, 50)):
        if not low <= result[key] <= high:
            raise ValueError(f"{key}: use {low} a {high}.")
    weights = [result[k] for k in ("commission_weight", "rating_weight", "sales_weight")]
    if any(w < 0 or w > 100 for w in weights) or sum(weights) != 100:
        raise ValueError("Os pesos devem somar 100%.")
    return result


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "shopee.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS shopee (id INTEGER PRIMARY KEY CHECK(id=1), app_id TEXT, secret TEXT);
                CREATE TABLE IF NOT EXISTS telegram (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, chat_id TEXT, thread_id TEXT);
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT,
                    source TEXT, settings TEXT, warning TEXT, error TEXT, scanned INTEGER DEFAULT 0,
                    message_id INTEGER, delivery TEXT DEFAULT 'not_sent', sent_at TEXT);
                CREATE TABLE IF NOT EXISTS offers (
                    run_id INTEGER, product_key TEXT, body TEXT, selected INTEGER,
                    PRIMARY KEY(run_id, product_key));
                CREATE INDEX IF NOT EXISTS offers_product ON offers(product_key,run_id);
                CREATE TABLE IF NOT EXISTS decisions (
                    product_key TEXT PRIMARY KEY, state TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS schedule (
                    day TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, next_retry REAL DEFAULT 0,
                    status TEXT, run_id INTEGER);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created TEXT, level TEXT, message TEXT);
            """)
            first = dict(DEFAULTS)
            for env, key in (("SHOPEE_KEYWORD", "keyword"), ("SHOPEE_PRODUCT_CAT_ID", "category_id"),
                             ("SHOPEE_MIN_RATING", "min_rating"), ("SHOPEE_PAGE_LIMIT", "page_limit"),
                             ("SHOPEE_MAX_PAGES", "max_pages")):
                if os.environ.get(env):
                    value = os.environ[env]
                    first[key] = type(DEFAULTS[key])(value)
            first["auto_send"] = os.environ.get("AUTO_SEND", "false").lower() == "true"
            first = validate_settings(first)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(first),))
            # A process crash during delivery is ambiguous; never automatically resend it.
            db.execute("UPDATE runs SET delivery='unknown' WHERE delivery='sending'")
            db.execute("UPDATE runs SET status='failed', error='Execução interrompida; consulte os logs.', finished=? WHERE status='running'", (utcnow(),))
            db.execute("UPDATE schedule SET status='retry', next_retry=0 WHERE status='running'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.db() as db:
            return json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])

    def save_settings(self, payload):
        value = validate_settings(payload, self.settings())
        with self.db() as db:
            db.execute("UPDATE settings SET body=? WHERE id=1", (json.dumps(value),))
        self.log("info", "Configurações atualizadas.")
        return value

    def log(self, level, message):
        with self.db() as db:
            db.execute("INSERT INTO events(created,level,message) VALUES(?,?,?)", (utcnow(), level, safe_error(message)))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")

    def runs(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 60")]

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Consulta não encontrada.")
            result = dict(row)
            result["settings"] = json.loads(result["settings"])
            result["products"] = []
            for entry in db.execute("""
                SELECT o.body,o.selected,COALESCE(d.state,'new') decision
                FROM offers o LEFT JOIN decisions d ON o.product_key=d.product_key
                WHERE o.run_id=? ORDER BY o.selected DESC""", (run_id,)):
                product = json.loads(entry["body"])
                product.update(selected=bool(entry["selected"]), decision=entry["decision"])
                result["products"].append(product)
            result["products"].sort(key=lambda p: (not p["selected"], -p["score"]))
            return result

    def decision(self, key, state):
        if state not in DECISIONS:
            raise ValueError("Estado inválido.")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM offers WHERE product_key=?", (key,)).fetchone():
                raise ValueError("Produto não encontrado.")
            db.execute("INSERT INTO decisions VALUES(?,?,?) ON CONFLICT(product_key) DO UPDATE SET state=excluded.state,updated=excluded.updated",
                       (key, state, utcnow()))

    def shopee(self):
        with self.db() as db:
            row = db.execute("SELECT app_id,secret FROM shopee WHERE id=1").fetchone()
        return dict(row) if row else dict(app_id=os.environ.get("SHOPEE_APP_ID", ""), secret=os.environ.get("SHOPEE_SECRET", ""))

    def save_shopee(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"app_id", "secret"}:
            raise ValueError("Configuração Shopee inválida.")
        if any(not isinstance(v, str) for v in payload.values()):
            raise ValueError("Use texto nas credenciais.")
        app_id = payload.get("app_id", "").strip()
        secret = payload.get("secret", "").strip() or self.shopee()["secret"]
        if not app_id.isdigit() or not secret or len(secret) > 512:
            raise ValueError("Preencha um App ID numérico e o segredo da Shopee.")
        with self.db() as db:
            db.execute("INSERT INTO shopee VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET app_id=excluded.app_id,secret=excluded.secret", (app_id, secret))
        self.log("info", "Credenciais da Shopee atualizadas no servidor.")

    def telegram(self):
        with self.db() as db:
            row = db.execute("SELECT token,chat_id,thread_id FROM telegram WHERE id=1").fetchone()
        if row:
            return dict(row)
        return dict(token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
                    thread_id=os.environ.get("TELEGRAM_THREAD_ID", ""))

    def telegram_public(self):
        config = self.telegram()
        return dict(configured=bool(config["token"] and config["chat_id"]),
                    token_saved=bool(config["token"]), chat_id=config["chat_id"],
                    thread_id=config["thread_id"])

    def save_telegram(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"token", "chat_id", "thread_id"}:
            raise ValueError("Configuração Telegram inválida.")
        if any(not isinstance(v, str) for v in payload.values()):
            raise ValueError("Use texto nos campos do Telegram.")
        token = payload.get("token", "").strip() or self.telegram()["token"]
        chat = payload.get("chat_id", "").strip()
        topic = payload.get("thread_id", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Token do bot inválido.")
        if not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,}", chat):
            raise ValueError("ID do chat inválido.")
        if topic and (not topic.isdigit() or not 1 <= int(topic) <= 2147483647):
            raise ValueError("ID do tópico precisa ser um inteiro positivo.")
        with self.db() as db:
            db.execute("INSERT INTO telegram VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,chat_id=excluded.chat_id,thread_id=excluded.thread_id",
                       (token, chat, topic))
        self.log("info", "Destino Telegram atualizado. Token preservado em armazenamento local.")

    def exclusions(self, days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.db() as db:
            excluded = {r[0] for r in db.execute("SELECT product_key FROM decisions WHERE state IN ('discarded','published')")}
            if days:
                excluded.update(r[0] for r in db.execute("""
                    SELECT DISTINCT o.product_key FROM offers o JOIN runs r ON r.id=o.run_id
                    WHERE o.selected=1 AND r.sent_at>=? AND r.delivery='sent'""", (cutoff,)))
            return excluded

    def history(self, key):
        with self.db() as db:
            return [dict(started=r["started"], **json.loads(r["body"])) for r in db.execute("""
                SELECT r.started,o.body FROM offers o JOIN runs r ON r.id=o.run_id
                WHERE o.product_key=? ORDER BY r.id DESC LIMIT 60""", (key,))]


def shopee_query(settings, page):
    args = ["listType: 0", "sortType: 5", f"page: {page}", f"limit: {settings['page_limit']}"]
    if settings["keyword"]:
        args.append("keyword: " + json.dumps(settings["keyword"], ensure_ascii=False))
    if settings["category_id"]:
        args.append("productCatId: " + settings["category_id"])
    return """query { productOfferV2(%s) {
      nodes { itemId shopId productName shopName offerLink price priceMin priceMax
        commission commissionRate appExistRate appNewRate ratingStar sales
        productCatIds periodStartTime periodEndTime }
      pageInfo { page hasNextPage }
    } }""" % ", ".join(args)


def request_json(url, body, headers=None):
    request = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def fetch_offers(settings, credentials=None):
    credentials = credentials or dict(app_id=os.environ.get("SHOPEE_APP_ID"), secret=os.environ.get("SHOPEE_SECRET"))
    app_id, secret = credentials["app_id"], credentials["secret"]
    if not app_id or not secret:
        raise ValueError("Configure SHOPEE_APP_ID e SHOPEE_SECRET no .env.")
    all_nodes, complete = [], False
    for page in range(1, settings["max_pages"] + 1):
        body = json.dumps({"query": shopee_query(settings, page)}, ensure_ascii=False, separators=(",", ":")).encode()
        for attempt in range(3):
            timestamp = str(int(time.time()))
            signature = hashlib.sha256(app_id.encode() + timestamp.encode() + body + secret.encode()).hexdigest()
            try:
                result = request_json(API, body, {"Authorization": f"SHA256 Credential={app_id}, Signature={signature}, Timestamp={timestamp}"})
                break
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ValueError(f"Shopee retornou HTTP {error.code}.") from None
                time.sleep(2 ** (attempt + 1))
            except (OSError, TimeoutError):
                if attempt == 2:
                    raise ValueError("Shopee indisponível após três tentativas.") from None
                time.sleep(2 ** (attempt + 1))
        if result.get("errors"):
            raise ValueError("Shopee: " + "; ".join(e.get("message", "Erro GraphQL") for e in result["errors"]))
        data = (result.get("data") or {}).get("productOfferV2")
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            raise ValueError("Resposta da Shopee sem lista de ofertas válida.")
        all_nodes.extend(data["nodes"])
        if not (data.get("pageInfo") or {}).get("hasNextPage"):
            complete = True
            break
        if not data["nodes"]:
            raise ValueError("Paginação inconsistente: página vazia com continuação.")
    if not complete:
        raise ValueError("Limite de páginas atingido. Restrinja o nicho ou aumente o limite; ranking incompleto não foi enviado.")
    return all_nodes


def rank_offers(nodes, settings, excluded=None, now=None):
    excluded, now = excluded or set(), now or time.time()
    unique = {}
    for node in nodes:
        key = f"{node.get('shopId', '')}:{node.get('itemId', '')}"
        if not node.get("itemId") or key in unique or key in excluded:
            continue
        start = number(node.get("periodStartTime"), 0)
        end = number(node.get("periodEndTime"), float("inf"))
        if (start > 0 and start > now) or (end > 0 and end < now):
            continue
        price = number(node.get("price")) or number(node.get("priceMin"))
        rate = number(node.get("appExistRate"))
        new_rate = number(node.get("appNewRate"))
        # Conservative basis: existing customers, with API commission as fallback.
        payout = price * rate if rate > 0 else number(node.get("commission"))
        rating, sales = number(node.get("ratingStar")), max(0, number(node.get("sales")))
        link = str(node.get("offerLink") or "")
        if not link.startswith("https://"):
            continue
        if price <= 0 or payout <= 0 or price < settings["min_price"] or (settings["max_price"] and price > settings["max_price"]):
            continue
        if sales < settings["min_sales"] or payout < settings["min_commission"]:
            continue
        unique[key] = dict(key=key, item_id=str(node["itemId"]), shop_id=str(node.get("shopId", "")),
                           name=str(node.get("productName") or "Produto"), shop=str(node.get("shopName") or ""),
                           price=price, commission=payout, app_existing_rate=rate, app_new_rate=new_rate,
                           commission_basis="cliente existente" if rate > 0 else "comissão informada pela API",
                           rating=rating, sales=int(sales), category=str((node.get("productCatIds") or [""])[0]),
                           link=link, expires=end if math.isfinite(end) and end > 0 else None)
    candidates = [p for p in unique.values() if p["rating"] >= settings["min_rating"]]
    warning = ""
    if len(candidates) < settings["top_count"] and settings["relax_rating"]:
        candidates = [p for p in unique.values() if p["rating"] >= settings["fallback_rating"]]
        warning = f"Nota mínima flexibilizada para {settings['fallback_rating']:.1f}, conforme sua configuração."
    if not candidates:
        raise ValueError("Nenhuma oferta passou pelos filtros e pelo intervalo de repetição.")
    max_payout = max(p["commission"] for p in candidates)
    max_sales = max(p["sales"] for p in candidates)
    for p in candidates:
        components = dict(commission=math.log1p(p["commission"]) / math.log1p(max_payout),
                          rating=max(0, min(1, p["rating"] / 5)),
                          sales=math.log1p(p["sales"]) / math.log1p(max_sales) if max_sales else 0)
        p["score"] = round(sum(components[k] * settings[k + "_weight"] for k in components), 2)
        p["reasons"] = [f"Comissão estimada R$ {p['commission']:.2f} ({p['commission_basis']})",
                        f"Avaliação {p['rating']:.1f}/5", f"{p['sales']} vendas informadas"]
    candidates.sort(key=lambda p: (-p["score"], p["key"]))
    selected, categories, shops = [], {}, {}
    for p in candidates:
        cat, shop = p["category"] or "sem_categoria", p["shop_id"]
        if categories.get(cat, 0) >= settings["per_category"] or shops.get(shop, 0) >= settings["per_shop"]:
            continue
        selected.append(p["key"])
        categories[cat] = categories.get(cat, 0) + 1
        shops[shop] = shops.get(shop, 0) + 1
        if len(selected) == settings["top_count"]:
            break
    if len(selected) < settings["top_count"]:
        warning += f" Selecionadas {len(selected)} de {settings['top_count']}; filtros e diversidade foram preservados."
    return candidates, set(selected), warning.strip()


def telegram_text(products):
    lines = [f"Top {len(products)} Shopee · {datetime.now(TZ):%d/%m/%Y}", "Comissões estimadas, não garantidas no canal Vídeo.", ""]
    for index, p in enumerate(products, 1):
        lines.extend([f"{index}. {p['name'][:100]}", f"R$ {p['price']:.2f} · comissão estimada R$ {p['commission']:.2f}",
                      f"Nota {p['rating']:.1f} · {p['sales']} vendas · score {p['score']:.1f}", p["link"], ""])
    text = "\n".join(lines)
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Prévia excede o limite do Telegram; reduza a quantidade de produtos.")
    return text


class Engine:
    def __init__(self, store, fetcher=None, sender=request_json):
        self.store, self.sender = store, sender
        self.fetcher = fetcher or (lambda settings: fetch_offers(settings, store.shopee()))
        self.lock = threading.Lock()
        self.heartbeat = time.time()
        self.stop = threading.Event()

    def collect(self, source="manual"):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Uma operação já está em andamento.")
        try:
            settings = self.store.settings()
            with self.store.db() as db:
                run_id = db.execute("INSERT INTO runs(started,status,source,settings) VALUES(?,'running',?,?)",
                                    (utcnow(), source, json.dumps(settings))).lastrowid
            self.store.log("info", f"Consulta #{run_id} iniciada ({source}).")
            try:
                nodes = self.fetcher(settings)
                candidates, selected, warning = rank_offers(nodes, settings, self.store.exclusions(settings["repeat_days"]))
                with self.store.db() as db:
                    for product in candidates:
                        db.execute("INSERT INTO offers VALUES(?,?,?,?)", (run_id, product["key"], json.dumps(product), int(product["key"] in selected)))
                    db.execute("UPDATE runs SET status='ready',finished=?,scanned=?,warning=? WHERE id=?",
                               (utcnow(), len(nodes), warning, run_id))
                self.store.log("info", f"Consulta #{run_id}: {len(nodes)} ofertas consultadas, {len(selected)} selecionadas.")
            except Exception as error:
                with self.store.db() as db:
                    db.execute("UPDATE runs SET status='failed',finished=?,error=? WHERE id=?", (utcnow(), safe_error(error), run_id))
                self.store.log("error", f"Consulta #{run_id}: {safe_error(error)}")
                raise
            return run_id
        finally:
            self.lock.release()

    def sendable_products(self, run):
        excluded = self.store.exclusions(run["settings"]["repeat_days"])
        return [p for p in run["products"] if p["selected"] and p["key"] not in excluded
                and p["decision"] not in ("discarded", "published")]

    def send(self, run_id):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Uma operação já está em andamento.")
        try:
            run = self.store.run(run_id)
            if run["status"] != "ready" or run["delivery"] != "not_sent":
                raise ValueError("Consulta não está disponível para envio. Envios confirmados ou incertos não são repetidos.")
            if datetime.now(timezone.utc) - datetime.fromisoformat(run["finished"]) > timedelta(hours=24):
                raise ValueError("Consulta com mais de 24 horas. Atualize as ofertas antes de enviar.")
            products = self.sendable_products(run)
            if not products:
                raise ValueError("Nenhuma oferta selecionada disponível para envio.")
            if any(p["expires"] and p["expires"] < time.time() for p in products):
                raise ValueError("Há ofertas expiradas. Faça uma nova consulta.")
            telegram = self.store.telegram()
            token, chat = telegram["token"], telegram["chat_id"]
            if not token or not chat:
                raise ValueError("Configure o Telegram no .env.")
            text = telegram_text(products)
            with self.store.db() as db:
                changed = db.execute("UPDATE runs SET delivery='sending' WHERE id=? AND delivery='not_sent'", (run_id,)).rowcount
                if changed != 1:
                    raise ValueError("Envio já iniciado.")
            payload = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
            if telegram["thread_id"]:
                payload["message_thread_id"] = int(telegram["thread_id"])
            body = json.dumps(payload).encode()
            try:
                result = self.sender(f"https://api.telegram.org/bot{token}/sendMessage", body)
                if not result.get("ok"):
                    raise ValueError("Telegram recusou o envio.")
                message_id = result["result"]["message_id"]
            except Exception as error:
                # A timeout may occur AFTER Telegram accepted the message.
                with self.store.db() as db:
                    db.execute("UPDATE runs SET delivery='unknown',error=? WHERE id=?", ("Entrega não confirmada. Confira o Telegram antes de qualquer novo envio.", run_id))
                self.store.log("error", f"Envio #{run_id} não confirmado: " + safe_error(error).replace(token, "[oculto]"))
                raise ValueError("Entrega não confirmada; confira o Telegram. Não haverá reenvio automático.") from None
            with self.store.db() as db:
                db.execute("UPDATE runs SET delivery='sent',sent_at=?,message_id=? WHERE id=?", (utcnow(), message_id, run_id))
                # Only actually delivered products count towards repetition.
                delivered = {p["key"] for p in products}
                for p in run["products"]:
                    if p["selected"] and p["key"] not in delivered:
                        db.execute("UPDATE offers SET selected=0 WHERE run_id=? AND product_key=?", (run_id, p["key"]))
            self.store.log("info", f"Envio #{run_id} confirmado pelo Telegram.")
            return message_id
        finally:
            self.lock.release()

    def tick(self, now=None):
        now = now or datetime.now(TZ)
        self.heartbeat = time.time()
        settings = self.store.settings()
        if not settings["schedule_enabled"] or now.strftime("%H:%M") < settings["schedule_time"]:
            return
        day = now.strftime("%Y-%m-%d")
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO schedule(day,status) VALUES(?,'pending')", (day,))
            slot = dict(db.execute("SELECT * FROM schedule WHERE day=?", (day,)).fetchone())
            if slot["status"] in ("done", "failed", "unknown", "running") or slot["next_retry"] > now.timestamp():
                return
            if self.lock.locked():
                return
            db.execute("UPDATE schedule SET status='running',attempts=attempts+1 WHERE day=?", (day,))
        try:
            run_id = slot["run_id"]
            if run_id is not None:
                previous = self.store.run(run_id)
                if previous["delivery"] == "sent":
                    with self.store.db() as db:
                        db.execute("UPDATE schedule SET status='done' WHERE day=?", (day,))
                    return
                if previous["delivery"] in ("sending", "unknown"):
                    with self.store.db() as db:
                        db.execute("UPDATE schedule SET status='unknown' WHERE day=?", (day,))
                    return
            if run_id is None:
                run_id = self.collect("scheduled")
                with self.store.db() as db:
                    db.execute("UPDATE schedule SET run_id=? WHERE day=?", (run_id, day))
            if settings["auto_send"]:
                self.send(run_id)
            with self.store.db() as db:
                db.execute("UPDATE schedule SET status='done' WHERE day=?", (day,))
        except Exception as error:
            attempts = slot["attempts"] + 1
            with self.store.db() as db:
                db.execute("UPDATE schedule SET status=?,next_retry=? WHERE day=?",
                           ("failed" if attempts >= 3 else "retry", now.timestamp() + 300 * attempts, day))
            self.store.log("error", f"Agendamento tentativa {attempts}/3: {safe_error(error)}")

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as error:
                    self.store.log("error", "Agendador: " + safe_error(error))
                self.stop.wait(15)
        threading.Thread(target=loop, name="scheduler", daemon=True).start()
