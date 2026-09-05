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

STORES = ("auth", "app", "audit", "antt")

# Teto do registro de auditoria. Espelha o pruneAudit do index.html: o corte e so por
# tamanho, nunca por data, e sempre remove os mais antigos. Desde a 1.17.0 a poda e
# aplicada aqui no servidor, porque o PUT /api/audit passou a FUNDIR em vez de substituir
# (se o cliente podasse e enviasse, o servidor ressuscitaria o que ele acabou de cortar).
AUD_MAXL = 20000
AUD_MAXS = 5000

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
    seed_store_if_absent("comms", {"threads": []})
    seed_store_if_absent("antt", {"portaria": "", "vigencia": "", "atualizado": 0, "coef": []})
    seed_store_if_absent("audit", {"sessions": [], "logs": []})
    _INIT_DONE = True


# ---------- Comunicacao (Central de Solicitacoes) ----------
def comms_read():
    r = read_store("comms")
    d = r.get("data") or {"threads": []}
    if not isinstance(d, dict) or "threads" not in d:
        d = {"threads": []}
    return d


def comms_mutate(fn):
    """Le-modifica-grava a store 'comms' de forma atomica (SELECT ... FOR UPDATE),
    para nao perder mensagens quando dois usuarios escrevem ao mesmo tempo."""
    now = int(time.time() * 1000)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM kv_store WHERE name='comms' FOR UPDATE")
            row = cur.fetchone()
            data = (row[0] if row and row[0] else None) or {"threads": []}
            if "threads" not in data:
                data["threads"] = []
            result = fn(data)
            cur.execute(
                "INSERT INTO kv_store (name, rev, updated_at, data) VALUES ('comms', 1, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET rev = kv_store.rev + 1, "
                "updated_at = EXCLUDED.updated_at, data = EXCLUDED.data",
                (now, Json(data)),
            )
        conn.commit()
    finally:
        conn.close()
    return result


# ---------- Registro de auditoria (fusao atomica) ----------
# O separador da chave e o U+0001, o mesmo do _audKey() do index.html. Escrito com chr(1)
# de proposito: nao ha contrabarra no fonte para se perder em copia ou edicao.
_AUD_SEP = chr(1)


def _aud_s(v):
    """Converte como o String() do JS faz dentro de Array.join: None vira string vazia,
    numero inteiro vira o inteiro. Sem isso, uma acao sem detalhe teria chave 'None' aqui
    e '' no front, e cada gravacao duplicaria a entrada."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _aud_key(e):
    """Identidade de uma entrada de log: ts|user|action|detail.
    Tem que dar exatamente o mesmo resultado que o _audKey() do index.html."""
    if not isinstance(e, dict):
        return None
    return _AUD_SEP.join([_aud_s(e.get("ts")), _aud_s(e.get("user")),
                          _aud_s(e.get("action")), _aud_s(e.get("detail"))])


def _audit_merge(base, incoming):
    """Une dois retratos do registro. Mesma regra do mergeAudit() do index.html:
      - logs: identidade ts|user|action|detail; uniao sem duplicar, ordenada por ts
      - sessions: identidade id; em duplicata vence a de maior logoutAt
    Ao final aplica a poda, cortando sempre os mais antigos."""
    base = base if isinstance(base, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}

    logs = []
    vistos = set()
    for e in list(base.get("logs") or []) + list(incoming.get("logs") or []):
        if not isinstance(e, dict):
            continue
        k = _aud_key(e)
        if k in vistos:
            continue
        vistos.add(k)
        logs.append(e)
    logs.sort(key=lambda x: x.get("ts") or 0)

    por_id = {}
    ordem = []
    for s in list(base.get("sessions") or []) + list(incoming.get("sessions") or []):
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = s["id"]
        ex = por_id.get(sid)
        if ex is None:
            por_id[sid] = s
            ordem.append(sid)
        elif (s.get("logoutAt") or 0) > (ex.get("logoutAt") or 0):
            por_id[sid] = s
    sessions = [por_id[i] for i in ordem]
    sessions.sort(key=lambda x: x.get("loginAt") or 0)

    if len(logs) > AUD_MAXL:
        logs = logs[len(logs) - AUD_MAXL:]
    if len(sessions) > AUD_MAXS:
        sessions = sessions[len(sessions) - AUD_MAXS:]
    return {"sessions": sessions, "logs": logs}


def audit_mutate(incoming):
    """Le-modifica-grava a store 'audit' de forma atomica (SELECT ... FOR UPDATE), no molde
    do comms_mutate. O PUT /api/audit deixa de substituir o registro inteiro e passa a
    FUNDIR: um cliente com uma copia desatualizada nao consegue mais apagar acoes de outro.

    Contrato de rede inalterado -- mesmo verbo, mesma URL, mesmo corpo {data:...} e mesma
    resposta {rev, updatedAt} -- para o front que ja esta nos navegadores nao mudar nada.

    A linha e semeada antes do FOR UPDATE de proposito: sem linha nao ha o que travar, e
    duas primeiras gravacoes simultaneas ainda se atropelariam."""
    now = int(time.time() * 1000)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv_store (name, rev, updated_at, data) VALUES ('audit', 0, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (now, Json({"sessions": [], "logs": []})),
            )
            cur.execute("SELECT data FROM kv_store WHERE name='audit' FOR UPDATE")
            row = cur.fetchone()
            base = (row[0] if row and row[0] else None) or {"sessions": [], "logs": []}
            data = _audit_merge(base, incoming)
            cur.execute(
                "INSERT INTO kv_store (name, rev, updated_at, data) VALUES ('audit', 1, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET rev = kv_store.rev + 1, "
                "updated_at = EXCLUDED.updated_at, data = EXCLUDED.data "
                "RETURNING rev, updated_at",
                (now, Json(data)),
            )
            r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    return {"rev": r[0], "updatedAt": r[1]}


def _author(body):
    by = body.get("by") or {}
    user = by.get("user") or "?"
    return {"user": user, "nome": by.get("nome") or user, "role": by.get("role") or ""}


def _msg(author, text, sys=False):
    return {"ts": int(time.time() * 1000), "sys": bool(sys),
            "user": author["user"], "nome": author["nome"], "role": author["role"], "text": text}


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
    if name == "audit":
        # registro de auditoria: funde em vez de substituir (ver audit_mutate).
        # auth/app/antt continuam em write_store -- para eles substituir e o correto.
        return jsonify(audit_mutate(parsed["data"]))
    return jsonify(write_store(name, parsed["data"]))


# ------------------------ arquivos estaticos ------------------------
@app.route("/api/comms", methods=["GET", "POST"])
def api_comms():
    ensure_init()
    if request.method == "GET":
        return jsonify(comms_read())
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    author = _author(body)

    if action == "open":
        assunto = (body.get("assunto") or "").strip()
        mensagem = (body.get("mensagem") or "").strip()
        if not assunto or not mensagem:
            return jsonify({"error": "assunto e mensagem sao obrigatorios"}), 400

        def op(d):
            th = {
                "id": "C" + str(int(time.time() * 1000)) + "-" + str(len(d["threads"])),
                "ts": int(time.time() * 1000), "status": "aberto",
                "pedido": body.get("pedido", ""), "cliente": body.get("cliente", ""),
                "setor": body.get("setor", ""), "tipo": body.get("tipo", ""),
                "categoria": body.get("categoria", "Pedido"),
                "urgencia": body.get("urgencia", "") or "Media",
                "assunto": assunto, "by": author,
                "msgs": [_msg(author, mensagem)]
            }
            d["threads"].insert(0, th)
            return th
        return jsonify(comms_mutate(op))

    if action == "reply":
        tid = body.get("id")
        mensagem = (body.get("mensagem") or "").strip()
        if not mensagem:
            return jsonify({"error": "mensagem vazia"}), 400

        def op(d):
            for th in d["threads"]:
                if th["id"] == tid:
                    th["msgs"].append(_msg(author, mensagem))
                    if body.get("reopen") and th["status"] != "aberto":
                        th["status"] = "aberto"
                        th["msgs"].append(_msg(author, "Solicitacao reaberta.", sys=True))
                    return th
            return None
        r = comms_mutate(op)
        return (jsonify(r), 200) if r else (jsonify({"error": "nao encontrado"}), 404)

    if action == "status":
        tid = body.get("id")
        novo = body.get("status")
        if novo not in ("aberto", "resolvido"):
            return jsonify({"error": "status invalido"}), 400

        def op(d):
            for th in d["threads"]:
                if th["id"] == tid:
                    th["status"] = novo
                    th["msgs"].append(_msg(author, "Marcada como resolvida." if novo == "resolvido" else "Solicitacao reaberta.", sys=True))
                    return th
            return None
        r = comms_mutate(op)
        return (jsonify(r), 200) if r else (jsonify({"error": "nao encontrado"}), 404)

    return jsonify({"error": "acao invalida"}), 400


@app.route("/api/comms/count")
def api_comms_count():
    ensure_init()
    ths = comms_read().get("threads", [])
    return jsonify({"open": sum(1 for t in ths if t.get("status") == "aberto"), "total": len(ths)})


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
