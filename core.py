"""Persistent offer collection, ranking, delivery and scheduling."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import analysis
from content import plain, product_messages
from llm import LLM_DEFAULTS, ModelError, Runner, WORKER_ONLINE_SECONDS, fetch_image, public_llm, validate_llm

TZ = ZoneInfo("America/Sao_Paulo")
DEFAULTS = dict(keyword="", min_rating=4.5, relax_rating=False,
                fallback_rating=4.0, min_price=0.0, max_price=500.0, min_sales=0,
                min_commission=0.0, repeat_days=7, per_shop=2,
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
    # Ignore category_id left by older versions.
    result = dict(DEFAULTS)
    result.update({key: value for key, value in (existing or {}).items() if key in DEFAULTS})
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
                           ("per_shop", 1, 10),
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
                CREATE TABLE IF NOT EXISTS llm (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_auth (id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT, created TEXT);
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY, last_seen REAL, version TEXT, kinds TEXT);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, created TEXT,
                    deadline REAL, worker_id TEXT, claimed_at TEXT, finished TEXT, result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,kind,id);
                CREATE TABLE IF NOT EXISTS deliveries (
                    run_id INTEGER, product_key TEXT, state TEXT, photo_message_id INTEGER,
                    text_message_id INTEGER, sent_at TEXT, error TEXT, PRIMARY KEY(run_id, product_key));
            """)
            if "progress" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN progress TEXT")
            db.execute("INSERT OR IGNORE INTO llm VALUES(1,?)", (json.dumps(LLM_DEFAULTS),))
            first = dict(DEFAULTS)
            for env, key in (("SHOPEE_KEYWORD", "keyword"),
                             ("SHOPEE_MIN_RATING", "min_rating"), ("SHOPEE_PAGE_LIMIT", "page_limit"),
                             ("SHOPEE_MAX_PAGES", "max_pages")):
                if os.environ.get(env):
                    value = os.environ[env]
                    first[key] = type(DEFAULTS[key])(value)
            first["auto_send"] = os.environ.get("AUTO_SEND", "false").lower() == "true"
            first = validate_settings(first)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(first),))
            stored_settings = json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
            migrated_settings = validate_settings({}, stored_settings)
            if migrated_settings != stored_settings:
                db.execute("UPDATE settings SET body=? WHERE id=1", (json.dumps(migrated_settings),))
            # A process crash during delivery is ambiguous; never automatically resend it.
            db.execute("UPDATE runs SET delivery='unknown' WHERE delivery='sending'")
            db.execute("UPDATE runs SET status='failed', error='Execução interrompida; consulte os logs.', finished=? WHERE status IN ('running','analyzing')", (utcnow(),))
            db.execute("UPDATE schedule SET status='retry', next_retry=0 WHERE status='running'")
            db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            # Nobody waits for tasks created before a restart.
            db.execute("UPDATE tasks SET status='expired' WHERE status IN ('queued','claimed')")

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
            return validate_settings({}, json.loads(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0]))

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

    def exclusions(self):
        """Exclude every offer already sent, or possibly sent, to Telegram, plus its validated equivalents."""
        with self.db() as db:
            excluded = {r[0] for r in db.execute("SELECT product_key FROM decisions WHERE state IN ('discarded','published')")}
            for row in db.execute("""
                SELECT o.product_key,o.body FROM offers o JOIN runs r ON r.id=o.run_id
                WHERE o.selected=1 AND r.delivery IN ('sent','unknown','sending')"""):
                excluded.add(row[0])
                links = (json.loads(row[1]).get("analysis") or {}).get("links") or []
                excluded.update(entry["key"] for entry in links if entry.get("key"))
            return excluded

    def history(self, key):
        with self.db() as db:
            return [dict(started=r["started"], **json.loads(r["body"])) for r in db.execute("""
                SELECT r.started,o.body FROM offers o JOIN runs r ON r.id=o.run_id
                WHERE o.product_key=? ORDER BY r.id DESC LIMIT 60""", (key,))]

    def llm(self):
        with self.db() as db:
            return validate_llm({}, json.loads(db.execute("SELECT body FROM llm WHERE id=1").fetchone()[0]))

    def save_llm(self, payload):
        value = validate_llm(payload, self.llm())
        with self.db() as db:
            db.execute("UPDATE llm SET body=? WHERE id=1", (json.dumps(value),))
        self.log("info", "Modelos de visão/texto atualizados. Chaves preservadas no servidor.")
        return public_llm(value)

    def set_worker_token(self):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO worker_auth VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,created=excluded.created",
                       (hashlib.sha256(token.encode()).hexdigest(), utcnow()))
        self.log("info", "Novo token do worker gerado; o anterior deixou de valer.")
        return token

    def check_worker_token(self, token):
        with self.db() as db:
            row = db.execute("SELECT token_hash FROM worker_auth WHERE id=1").fetchone()
        return bool(row and row[0] and token) and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row[0])

    def worker_seen(self, worker_id, version, kinds):
        with self.db() as db:
            db.execute("INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,version=excluded.version,kinds=excluded.kinds",
                       (worker_id, time.time(), version, json.dumps(kinds)))

    def worker_touch(self, worker_id):
        with self.db() as db:
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (time.time(), worker_id))

    def worker_online(self, kind="llm"):
        with self.db() as db:
            rows = db.execute("SELECT kinds FROM workers WHERE last_seen>?", (time.time() - WORKER_ONLINE_SECONDS,)).fetchall()
        return any(kind in json.loads(r[0] or "[]") for r in rows)

    def worker_status(self):
        with self.db() as db:
            token = db.execute("SELECT created FROM worker_auth WHERE id=1").fetchone()
            rows = [dict(r) for r in db.execute("SELECT * FROM workers ORDER BY last_seen DESC LIMIT 5")]
            queued = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','claimed')").fetchone()[0]
        for row in rows:
            row["online"] = time.time() - row["last_seen"] < WORKER_ONLINE_SECONDS
            row["last_seen"] = datetime.fromtimestamp(row["last_seen"], timezone.utc).isoformat()
            row["kinds"] = json.loads(row["kinds"] or "[]")
        return dict(token_created=token[0] if token else None, workers=rows, pending_tasks=queued)

    def create_task(self, kind, payload, deadline):
        with self.db() as db:
            return db.execute("INSERT INTO tasks(kind,payload,status,created,deadline) VALUES(?,?,'queued',?,?)",
                              (kind, json.dumps(payload), utcnow(), time.time() + deadline)).lastrowid

    def task(self, task_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Tarefa não encontrada.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def expire_task(self, task_id):
        with self.db() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE id=? AND status IN ('queued','claimed')", (task_id,))

    def claim_task(self, worker_id, kinds):
        with self.db() as db:
            db.execute("UPDATE tasks SET status='expired' WHERE status IN ('queued','claimed') AND deadline<?", (time.time(),))
            marks = ",".join("?" for _ in kinds)
            row = db.execute(f"SELECT id FROM tasks WHERE status='queued' AND kind IN ({marks}) ORDER BY id LIMIT 1",
                             list(kinds)).fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET status='claimed',worker_id=?,claimed_at=? WHERE id=? AND status='queued'",
                       (worker_id, utcnow(), row[0]))
        task = self.task(row[0])
        return dict(id=task["id"], kind=task["kind"], payload=task["payload"],
                    seconds_left=max(0, int(task["deadline"] - time.time())))

    def finish_task(self, task_id, worker_id, ok, result=None, error=""):
        with self.db() as db:
            changed = db.execute("UPDATE tasks SET status=?,finished=?,result=?,error=? WHERE id=? AND worker_id=? AND status='claimed'",
                                 ("done" if ok else "failed", utcnow(), json.dumps(result or {}), safe_error(error)[:500],
                                  task_id, worker_id)).rowcount
        if changed != 1:
            raise ValueError("Tarefa expirada ou de outro worker; resultado ignorado.")

    def set_progress(self, run_id, status, progress):
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,progress=? WHERE id=?", (status, progress, run_id))

    def save_analysis(self, run_id, product):
        with self.db() as db:
            db.execute("UPDATE offers SET body=? WHERE run_id=? AND product_key=?",
                       (json.dumps(product), run_id, product["key"]))

    def deliveries(self, run_id):
        with self.db() as db:
            return {r["product_key"]: dict(r) for r in db.execute("SELECT * FROM deliveries WHERE run_id=?", (run_id,))}


def shopee_query(settings, page, sort_type=5):
    args = ["listType: 0", f"sortType: {sort_type}", f"page: {page}", f"limit: {settings['page_limit']}"]
    if settings["keyword"]:
        args.append("keyword: " + json.dumps(settings["keyword"], ensure_ascii=False))
    return """query { productOfferV2(%s) {
      nodes { itemId shopId productName shopName offerLink productLink imageUrl price priceMin priceMax
        commission commissionRate appExistRate appNewRate ratingStar sales
        periodStartTime periodEndTime }
      pageInfo { page hasNextPage }
    } }""" % ", ".join(args)


def request_json(url, body, headers=None):
    request = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def fetch_product_page(settings, page, credentials, sort_type=5):
    app_id, secret = credentials["app_id"], credentials["secret"]
    body = json.dumps({"query": shopee_query(settings, page, sort_type)},
                      ensure_ascii=False, separators=(",", ":")).encode()
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
    page_info = data.get("pageInfo") or {}
    if page_info.get("hasNextPage") and not data["nodes"]:
        raise ValueError("Paginação inconsistente: página vazia com continuação.")
    return data


def fetch_offer_pages(settings, credentials=None, sort_type=5,
                      max_pages=None, require_complete=True):
    credentials = credentials or dict(app_id=os.environ.get("SHOPEE_APP_ID"), secret=os.environ.get("SHOPEE_SECRET"))
    app_id, secret = credentials["app_id"], credentials["secret"]
    if not app_id or not secret:
        raise ValueError("Configure SHOPEE_APP_ID e SHOPEE_SECRET no .env.")
    page_count = settings["max_pages"] if max_pages is None else max_pages
    all_nodes, complete = [], False
    for page in range(1, page_count + 1):
        data = fetch_product_page(settings, page, credentials, sort_type)
        all_nodes.extend(data["nodes"])
        if not (data.get("pageInfo") or {}).get("hasNextPage"):
            complete = True
            break
    if require_complete and not complete:
        raise ValueError("Limite de páginas atingido. Restrinja o nicho ou aumente o limite; ranking incompleto não foi enviado.")
    return all_nodes


def fetch_offers(settings, credentials=None):
    """Rank a bounded product sample; the full Shopee catalog cannot be paginated in one run."""
    return fetch_offer_pages(settings, credentials, sort_type=5, require_complete=False)


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
        image = str(node.get("imageUrl") or "")
        image = image if image.startswith("https://") else ""
        if price <= 0 or payout <= 0 or price < settings["min_price"] or (settings["max_price"] and price > settings["max_price"]):
            continue
        if sales < settings["min_sales"] or payout < settings["min_commission"]:
            continue
        unique[key] = dict(key=key, item_id=str(node["itemId"]), shop_id=str(node.get("shopId", "")),
                           name=str(node.get("productName") or "Produto"), shop=str(node.get("shopName") or ""),
                           price=price, commission=payout, app_existing_rate=rate, app_new_rate=new_rate,
                           commission_basis="cliente existente" if rate > 0 else "comissão informada pela API",
                           rating=rating, sales=int(sales), image=image,
                           link=link, expires=end if math.isfinite(end) and end > 0 else None)
    eligible = list(unique.values())
    candidates = [p for p in eligible if p["rating"] >= settings["min_rating"]]
    warning = ""
    if len(candidates) < settings["top_count"] and settings["relax_rating"]:
        candidates = [p for p in eligible if p["rating"] >= settings["fallback_rating"]]
        warning = f"Nota mínima flexibilizada para {settings['fallback_rating']:.1f}, conforme sua configuração."
    if not candidates:
        raise ValueError("Nenhuma oferta inédita passou pelos filtros.")
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
    selected, shops = [], {}
    for p in candidates:
        shop = p["shop_id"]
        if shops.get(shop, 0) >= settings["per_shop"]:
            continue
        selected.append(p["key"])
        shops[shop] = shops.get(shop, 0) + 1
        if len(selected) == settings["top_count"]:
            break
    if len(selected) < settings["top_count"]:
        warning += f" Selecionadas {len(selected)} de {settings['top_count']}; filtros e diversidade foram preservados."
    return candidates, set(selected), warning.strip()


class TelegramRejected(Exception):
    """Telegram answered with a definite refusal: the message was not delivered."""


def search_equivalents(product, settings, credentials, excluded, budget):
    """One extra API page per selected product, ordered by relevance to its name."""
    keyword = analysis.equivalence_keyword(product["name"])
    if not keyword or budget <= 0:
        return []
    if not credentials.get("app_id") or not credentials.get("secret"):
        raise ValueError("Credenciais da Shopee ausentes.")
    data = fetch_product_page(dict(settings, keyword=keyword, page_limit=50), 1, credentials, sort_type=1)
    return analysis.select_candidates(data["nodes"], product, excluded, budget)


def telegram_preview(products):
    messages = []
    for product in products:
        caption, text, _ = product_messages(product)
        messages.append(dict(key=product["key"], name=product["name"], caption=plain(caption), text=plain(text),
                             status=(product.get("analysis") or {}).get("status", "fallback")))
    header = (f"{len(products)} produto(s) · {datetime.now(TZ):%d/%m/%Y} · comissões estimadas, não garantidas no canal Vídeo.\n"
              "Cada produto chega em uma mensagem com foto, legenda e links; o prompt vem em resposta a ela.")
    body = "\n\n".join(f"── MENSAGEM {i} (foto) ──\n{m['caption']}\n\n── RESPOSTA {i} ──\n{m['text']}"
                         for i, m in enumerate(messages, 1))
    return dict(text=header + "\n\n" + body, messages=messages)


def multipart(fields, name, filename, kind, data):
    boundary = "----studio" + secrets.token_hex(12)
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f'Content-Type: {kind}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Engine:
    def __init__(self, store, fetcher=None, sender=request_json, searcher=None, runner=None, image_fetcher=fetch_image):
        self.store, self.sender, self.image_fetcher = store, sender, image_fetcher
        self.fetcher = fetcher or (lambda settings: fetch_offers(settings, store.shopee()))
        self.searcher = searcher or (lambda product, settings, excluded, budget:
                                     search_equivalents(product, settings, store.shopee(), excluded, budget))
        self.runner = runner or Runner(store)
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
                excluded = self.store.exclusions()
                nodes = self.fetcher(settings)
                candidates, selected, warning = rank_offers(nodes, settings, excluded)
                with self.store.db() as db:
                    for product in candidates:
                        db.execute("INSERT INTO offers VALUES(?,?,?,?)", (run_id, product["key"], json.dumps(product), int(product["key"] in selected)))
                    db.execute("UPDATE runs SET settings=?,scanned=?,warning=? WHERE id=?",
                               (json.dumps(settings), len(nodes), warning, run_id))
                chosen = [p for p in candidates if p["key"] in selected]
                # Equivalents never repeat a sent offer, another selected product or another product's link.
                used = set(excluded) | selected
                for index, product in enumerate(chosen, 1):
                    self.store.set_progress(run_id, "analyzing", f"Produto {index}/{len(chosen)}: equivalentes e conteúdo")
                    self.heartbeat = time.time()
                    product["analysis"] = self.analyze(product, settings, used)
                    used.update(link["key"] for link in product["analysis"]["links"])
                    self.store.save_analysis(run_id, product)
                with self.store.db() as db:
                    db.execute("UPDATE runs SET status='ready',finished=?,progress=NULL WHERE id=?", (utcnow(), run_id))
                fallbacks = sum(p["analysis"]["status"] != "ok" for p in chosen)
                self.store.log("info" if not fallbacks else "error",
                               f"Consulta #{run_id}: {len(nodes)} ofertas consultadas, {len(selected)} selecionadas"
                               + (f", {fallbacks} sem validação do modelo." if fallbacks else "."))
            except Exception as error:
                with self.store.db() as db:
                    db.execute("UPDATE runs SET status='failed',finished=?,error=?,progress=NULL WHERE id=?", (utcnow(), safe_error(error), run_id))
                self.store.log("error", f"Consulta #{run_id}: {safe_error(error)}")
                raise
            return run_id
        finally:
            self.lock.release()

    def analyze(self, product, settings, excluded):
        notes = []
        budget = self.store.llm()["candidate_budget"]
        try:
            candidates = self.searcher(product, settings, excluded, budget) if budget else []
        except Exception as error:
            candidates = []
            notes.append("Busca de anúncios equivalentes falhou: " + safe_error(error)[:200])
        try:
            output, provider, attempts = self.runner.run(
                analysis.build_prompt(product, candidates), analysis.SCHEMA, analysis.images_for(product, candidates),
                analysis.make_check(candidates), log=lambda message: self.store.log("info", "Modelo: " + message))
            result = analysis.finalize(product, candidates, output, provider, attempts, notes)
            self.store.log("info", f"{product['name'][:60]}: {len(result['links'])} link(s), legenda com "
                                   f"{result['caption_length']} caracteres ({provider}).")
            return result
        except ModelError as error:
            self.store.log("error", f"{product['name'][:60]}: {safe_error(error)}")
            return analysis.fallback(product, candidates, safe_error(error)[:400], [], notes)

    def sendable_products(self, run):
        excluded = self.store.exclusions()
        return [p for p in run["products"] if p["selected"] and p["key"] not in excluded
                and p["decision"] not in ("discarded", "published")]

    def telegram_call(self, token, method, payload, photo=None):
        url = f"https://api.telegram.org/bot{token}/{method}"
        for attempt in range(2):
            try:
                if photo:
                    # Upload the original file: Telegram cannot always fetch Shopee CDN URLs by itself.
                    fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()}
                    body, kind = multipart(fields, "photo", "produto" + photo[2], photo[0], photo[1])
                    result = self.sender(url, body, {"Content-Type": kind})
                else:
                    result = self.sender(url, json.dumps(payload).encode())
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read() or b"{}")
                except (ValueError, OSError):
                    detail = {}
                if error.code == 429 and attempt == 0:
                    time.sleep(min(30, int((detail.get("parameters") or {}).get("retry_after", 5))))
                    continue
                if 400 <= error.code < 500:
                    raise TelegramRejected(f"HTTP {error.code}: {str(detail.get('description', ''))[:200]}") from None
                raise
            if not result.get("ok"):
                raise TelegramRejected(str(result.get("description") or "Telegram recusou o envio.")[:200])
            return result["result"]["message_id"]
        raise TelegramRejected("limite de envio do Telegram")

    def send_photo(self, token, base, product, caption):
        try:
            kind, data = self.image_fetcher(product["image"])
        except Exception:
            # Fall back to letting Telegram fetch the URL.
            return self.telegram_call(token, "sendPhoto", {**base, "photo": product["image"], "caption": caption})
        extension = {"image/png": ".png", "image/webp": ".webp"}.get(kind, ".jpg")
        return self.telegram_call(token, "sendPhoto", {**base, "caption": caption}, photo=(kind, data, extension))

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
            messages = {p["key"]: product_messages(p) for p in products}  # Validates limits before sending anything.
            base = {"chat_id": chat, "parse_mode": "HTML"}
            if telegram["thread_id"]:
                base["message_thread_id"] = int(telegram["thread_id"])
            with self.store.db() as db:
                changed = db.execute("UPDATE runs SET delivery='sending' WHERE id=? AND delivery='not_sent'", (run_id,)).rowcount
                if changed != 1:
                    raise ValueError("Envio já iniciado.")
            sent, ambiguous, problems, first_id, uncertain = set(), None, [], None, set()

            def mark(key, state, photo=None, text=None, error=""):
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?,?,?)",
                               (run_id, key, state, photo, text, utcnow() if state == "sent" else None, error[:300]))

            for index, product in enumerate(products):
                caption, text, _ = messages[product["key"]]
                mark(product["key"], "sending")
                try:
                    if index:
                        time.sleep(1)  # Stay well below Telegram's per-chat rate limit.
                    photo_id = None
                    if product.get("image"):
                        try:
                            photo_id = self.send_photo(token, base, product, caption)
                        except TelegramRejected as error:
                            problems.append(f"{product['name'][:40]}: foto recusada ({error}); enviado sem foto")
                    if photo_id is None:
                        photo_id = self.telegram_call(token, "sendMessage", {**base, "text": caption,
                                                                              "link_preview_options": {"is_disabled": True}})
                except TelegramRejected as error:
                    mark(product["key"], "failed", error=str(error))
                    problems.append(f"{product['name'][:40]}: {error}")
                    break  # A definite refusal (chat, token, permission) would repeat for every product.
                except Exception as error:
                    mark(product["key"], "unknown", error="Entrega não confirmada.")
                    uncertain.add(product["key"])
                    ambiguous = safe_error(error).replace(token, "[oculto]") or type(error).__name__
                    break
                first_id = first_id or photo_id
                sent.add(product["key"])
                try:
                    text_id = self.telegram_call(token, "sendMessage", {
                        **base, "text": text, "link_preview_options": {"is_disabled": True},
                        "reply_parameters": {"message_id": photo_id, "allow_sending_without_reply": True}})
                    mark(product["key"], "sent", photo_id, text_id)
                except TelegramRejected as error:
                    mark(product["key"], "sent", photo_id, error=f"prompt recusado: {error}")
                    problems.append(f"{product['name'][:40]}: prompt recusado ({error})")
                except Exception as error:
                    mark(product["key"], "sent", photo_id, error="prompt não confirmado")
                    ambiguous = safe_error(error).replace(token, "[oculto]") or type(error).__name__
                    break
            if ambiguous:
                state = "unknown"
            else:
                state = "sent" if sent else "not_sent"
            summary = "; ".join(problems)
            with self.store.db() as db:
                if state == "not_sent":
                    # Nothing reached Telegram: fix the configuration and send again.
                    db.execute("UPDATE runs SET delivery='not_sent',error=? WHERE id=?", (summary or "Envio recusado.", run_id))
                else:
                    error = summary if state == "sent" else "Entrega não confirmada. Confira o Telegram antes de qualquer novo envio."
                    db.execute("UPDATE runs SET delivery=?,sent_at=?,message_id=?,error=? WHERE id=?",
                               (state, utcnow() if sent else None, first_id, error or None, run_id))
                    # Only products confirmed (or possibly) delivered stay permanently excluded.
                    keep = sent | uncertain
                    for p in run["products"]:
                        if p["selected"] and p["key"] not in keep:
                            db.execute("UPDATE offers SET selected=0 WHERE run_id=? AND product_key=?", (run_id, p["key"]))
            if state == "not_sent":
                self.store.log("error", f"Envio #{run_id} recusado pelo Telegram: {summary}")
                raise ValueError("Telegram recusou o envio: " + summary)
            if state == "unknown":
                self.store.log("error", f"Envio #{run_id} não confirmado: {ambiguous}")
                raise ValueError("Entrega não confirmada; confira o Telegram. Não haverá reenvio automático.")
            self.store.log("info" if not problems else "error",
                           f"Envio #{run_id}: {len(sent)} produto(s) confirmados pelo Telegram." + (f" Avisos: {summary}" if summary else ""))
            return first_id
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
