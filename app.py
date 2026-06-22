# N1 Alimentos - Formador de Carga
# Backend em Flask + PostgreSQL. Substitui o servidor Node.
# Mantem EXATAMENTE o mesmo contrato de API do frontend (index.html nao muda):
#   GET  /api/ping              -> {ok:true}
#   GET  /api/all               -> {auth:{rev,updatedAt,data}, app:{...}, audit:{...}}
#   GET  /api/rev               -> {auth:<rev>, app:<rev>, audit:<rev>}
#   GET  /api/<auth|app|audit>  -> {rev, updatedAt, data}
#   PUT  /api/<auth|app|audit>  body {data:...} -> {rev, updatedAt}  (rev incrementa)
# Os dados ficam no PostgreSQL (durável, compartilhado, sobrevive a deploy).
import os
import time
import json
from flask import Flask, request, jsonify, send_from_directory, abort
import psycopg2
from psycopg2.extras import Json

ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # ate 30 MB por requisicao

STORES = ("auth", "app", "audit")

# Railway expoe normalmente DATABASE_URL; aceitamos variantes por seguranca.
DB_URL = (os.environ.get("DATABASE_URL") or os.environ.get("DATABASE")
          or os.environ.get("POSTGRES_URL") or os.environ.get("PG_URL"))


def get_conn():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL nao definida no ambiente")
    return psycopg2.connect(DB_URL)


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kv_store ("
                " name TEXT PRIMARY KEY,"
                " rev INTEGER NOT NULL DEFAULT 0,"
                " updated_at BIGINT NOT NULL DEFAULT 0,"
                " data JSONB"
                ")"
            )
        conn.commit()
    finally:
        conn.close()


def read_store(name):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rev, updated_at, data FROM kv_store WHERE name=%s", (name,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return {"rev": 0, "updatedAt": 0, "data": None}
    return {"rev": row[0], "updatedAt": row[1], "data": row[2]}


def write_store(name, data):
    now = int(time.time() * 1000)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv_store (name, rev, updated_at, data) VALUES (%s, 1, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET rev = kv_store.rev + 1, "
                "updated_at = EXCLUDED.updated_at, data = EXCLUDED.data "
                "RETURNING rev, updated_at",
                (name, now, Json(data)),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    return {"rev": row[0], "updatedAt": row[1]}


def seed_store_if_absent(name, data):
    """Insere apenas se ainda nao existir (idempotente e a prova de concorrencia)."""
    now = int(time.time() * 1000)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv_store (name, rev, updated_at, data) VALUES (%s, 1, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (name, now, Json(data)),
            )
            inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted > 0


def migrate_from_volume():
    """Migra uma unica vez os dados do Volume (auth/app/audit.json) para o Postgres.
    So roda se o Postgres ainda nao tiver o registro; nao sobrescreve dados ja existentes."""
    data_dir = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not data_dir or not os.path.isdir(data_dir):
        return
    files = {"auth": "auth.json", "app": "app.json", "audit": "audit.json"}
    for name, fn in files.items():
        fp = os.path.join(data_dir, fn)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                obj = json.load(f)
            data = obj.get("data") if isinstance(obj, dict) else obj
            if data is not None:
                if seed_store_if_absent(name, data):
                    print("[migracao] '%s' importado do Volume para o Postgres" % name)
        except Exception as e:  # noqa
            print("[migracao] falha em '%s': %s" % (name, e))


_INIT_DONE = False


def ensure_init():
    """Garante a tabela criada e a migracao feita. Roda uma unica vez (ou tenta de novo
    se o banco ainda nao estava pronto). NAO roda no import, para nunca derrubar o worker."""
    global _INIT_DONE
    if _INIT_DONE:
        return
    init_db()
    migrate_from_volume()
    _INIT_DONE = True


# ---------------------------- API ----------------------------
@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True})


@app.route("/api/all")
def api_all():
    ensure_init()
    return jsonify({"auth": read_store("auth"), "app": read_store("app"), "audit": read_store("audit")})


@app.route("/api/rev")
def api_rev():
    ensure_init()
    return jsonify({
        "auth": read_store("auth")["rev"],
        "app": read_store("app")["rev"],
        "audit": read_store("audit")["rev"],
    })


@app.route("/api/<name>", methods=["GET", "PUT"])
def api_store(name):
    if name not in STORES:
        abort(404)
    ensure_init()
    if request.method == "GET":
        return jsonify(read_store(name))
    parsed = request.get_json(force=True, silent=True)
    if not isinstance(parsed, dict) or "data" not in parsed:
        return jsonify({"error": "bad json"}), 400
    return jsonify(write_store(name, parsed["data"]))


# ------------------------ arquivos estaticos ------------------------
@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/<path:fname>")
def static_files(fname):
    full = os.path.normpath(os.path.join(ROOT, fname))
    if not full.startswith(ROOT):
        abort(403)
    if os.path.isfile(full) and not fname.endswith(".py"):
        return send_from_directory(ROOT, fname)
    return send_from_directory(ROOT, "index.html")  # fallback


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
