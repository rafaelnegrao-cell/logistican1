#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N1 APS/PCP - Backend Flask
- Login com senha + 4 perfis (master, gerente, operador, consulta)
- Persistencia compartilhada (PostgreSQL KV com fallback em disco e rehidratacao no boot)
- Log de auditoria
- Gestao de usuarios (somente master)
A pagina (index.html) e servida quase intacta; apenas __N1_USER_JSON__ e substituido.
"""

import os, json, base64, datetime, functools, time, threading
from flask import (Flask, request, session, redirect, url_for,
                   Response, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash

# ------------------------------------------------------------------ config
APP_DIR   = os.path.dirname(os.path.abspath(__file__))
INDEX     = os.path.join(APP_DIR, 'index.html')
APP_VERSION = '2026-07-22-03'   # bump a cada entrega visivel; conferivel em /version
DATA_DIR  = os.environ.get('DATA_DIR', '/data')
DISK_KV   = os.path.join(DATA_DIR, 'n1_aps_kv.json')

MASTER_USER = os.environ.get('N1_MASTER_USER', 'rafael')
MASTER_PASS = os.environ.get('N1_MASTER_PASS', 'n1master2026')
# SECRET_KEY: sem valor-padrao conhecido. Se nao vier do ambiente, geramos/lemos
# uma chave aleatoria PERSISTIDA no KV (finalizado logo apos o KV estar pronto).
_DEFAULT_SECRET = 'n1-aps-pcp-dev-secret-change-me'
SECRET_KEY  = os.environ.get('SECRET_KEY')  # pode ser None -> resolvido abaixo

LOG_CAP = 5000          # maximo de eventos guardados no log
ROLES   = ('master', 'gerente', 'operador_pro', 'operador', 'consulta')
ROLE_LABELS = {'master': 'Master', 'gerente': 'Gerente',
               'operador_pro': 'Operador Pro', 'operador': 'Operador',
               'consulta': 'Consulta'}

app = Flask(__name__)
# Endurecimento do cookie de sessao. SECURE por padrao (Railway = HTTPS);
# defina N1_INSECURE_COOKIE=1 apenas para testar localmente em http.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=(os.environ.get('N1_INSECURE_COOKIE') != '1'),
)

# ------------------------------------------------------------------ KV store
# Tenta PostgreSQL; se indisponivel, usa arquivo em disco (DATA_DIR).
_PG = None
_PG_OFF_REASON = ''
_DB_URL = os.environ.get('DATABASE_URL')
try:
    import psycopg2  # noqa: F401
    _HAS_PSYCOPG2 = True
except Exception as _e:
    _HAS_PSYCOPG2 = False
    _PG_OFF_REASON = ('o driver psycopg2 não carregou (%s) — provavelmente Python 3.13 com '
                      'psycopg2-binary 2.9.9. Atualize o projeto (psycopg2-binary 2.9.10 + runtime '
                      'python-3.12).' % type(_e).__name__)
if _DB_URL and _HAS_PSYCOPG2:
    if _DB_URL.startswith('postgres://'):
        _DB_URL = _DB_URL.replace('postgres://', 'postgresql://', 1)
    _PG = _DB_URL
elif not _DB_URL:
    _PG_OFF_REASON = ((_PG_OFF_REASON + ' | ') if _PG_OFF_REASON else '') + \
        'a variável DATABASE_URL não está definida no serviço do app'


def _pg_conn():
    import psycopg2
    return psycopg2.connect(_PG)


def _pg_init():
    con = _pg_conn(); cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS n1_kv (k TEXT PRIMARY KEY, v TEXT)")
    con.commit(); cur.close(); con.close()


def _disk_load():
    try:
        with open(DISK_KV, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_save(d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DISK_KV + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DISK_KV)
    except Exception:
        pass


def kv_get(key, default=None):
    if _PG:
        last = None
        for _ in range(3):
            try:
                con = _pg_conn(); cur = con.cursor()
                cur.execute("SELECT v FROM n1_kv WHERE k=%s", (key,))
                row = cur.fetchone(); cur.close(); con.close()
                if row and row[0] is not None:
                    return json.loads(row[0])
                return default               # chave REALMENTE ausente
            except Exception as e:
                last = e
                try: time.sleep(0.4)
                except Exception: pass
        # Postgres configurado porém indisponível: NÃO devolver disco vazio
        # (isso levaria a sobrescrever/zerar dados). Sinaliza erro para o chamador tratar.
        raise RuntimeError("KV read failed for %r: %s" % (key, last))
    d = _disk_load()
    return d.get(key, default)


def kv_set(key, value):
    payload = json.dumps(value, ensure_ascii=False)
    if _PG:
        try:
            con = _pg_conn(); cur = con.cursor()
            cur.execute(
                "INSERT INTO n1_kv (k,v) VALUES (%s,%s) "
                "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (key, payload))
            con.commit(); cur.close(); con.close()
            return
        except Exception:
            pass
    d = _disk_load(); d[key] = value; _disk_save(d)


def kv_mutate(key, mutate_fn, default=None):
    """Read-modify-write ATOMICO na mesma chave (evita clobber concorrente).

    `mutate_fn(valor_atual)` deve devolver o novo valor a gravar.
    - PostgreSQL: usa transacao com SELECT ... FOR UPDATE, serializando gravacoes
      concorrentes na mesma chave (com retry em falha transitoria).
    - Disco (fallback): usa trava de arquivo (fcntl.flock), valida entre os
      workers do gunicorn no mesmo host.
    Retorna o novo valor gravado. Levanta RuntimeError se o Postgres estiver
    configurado porem indisponivel (o chamador deve tratar sem zerar dados).
    """
    if _PG:
        last = None
        for _ in range(4):
            con = None
            try:
                con = _pg_conn()
                con.autocommit = False
                cur = con.cursor()
                # garante a existencia da linha para poder trava-la (fecha a corrida de 1a insercao)
                cur.execute("INSERT INTO n1_kv (k,v) VALUES (%s,%s) ON CONFLICT (k) DO NOTHING",
                            (key, json.dumps(default, ensure_ascii=False)))
                cur.execute("SELECT v FROM n1_kv WHERE k=%s FOR UPDATE", (key,))
                row = cur.fetchone()
                cur_val = default
                if row and row[0] is not None:
                    try:
                        cur_val = json.loads(row[0])
                    except Exception:
                        cur_val = default
                new_val = mutate_fn(cur_val)
                cur.execute("UPDATE n1_kv SET v=%s WHERE k=%s",
                            (json.dumps(new_val, ensure_ascii=False), key))
                con.commit(); cur.close(); con.close()
                return new_val
            except Exception as e:
                last = e
                try:
                    if con:
                        con.rollback(); con.close()
                except Exception:
                    pass
                try:
                    time.sleep(0.3)
                except Exception:
                    pass
        raise RuntimeError("KV mutate failed for %r: %s" % (key, last))
    # ---- fallback em disco com trava de arquivo ----
    try:
        import fcntl
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DISK_KV + '.lock', 'a+') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                d = _disk_load()
                new_val = mutate_fn(d.get(key, default))
                d[key] = new_val
                _disk_save(d)
                return new_val
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        # sem fcntl (SO nao-POSIX) ou erro de disco: melhor esforco, sem trava
        d = _disk_load()
        new_val = mutate_fn(d.get(key, default))
        d[key] = new_val
        _disk_save(d)
        return new_val


# ------------------------------------------------------------------ users
def _seed_users():
    # Lê os usuários atuais SEM nunca sobrescrever o conjunto existente.
    try:
        users = kv_get('users')
    except Exception:
        # Banco indisponível no momento: NÃO gravar nada (evita zerar usuários).
        return {MASTER_USER: {'pwhash': generate_password_hash(MASTER_PASS),
                              'role': 'master', 'nome': 'Master'}}
    if not isinstance(users, dict):
        users = {}
    # Garante o master apenas se ele não existir — preserva todos os demais.
    if MASTER_USER not in users:
        users[MASTER_USER] = {
            'pwhash': generate_password_hash(MASTER_PASS),
            'role': 'master',
            'nome': 'Master'
        }
        try:
            kv_set('users', users)
        except Exception:
            pass
    return users


def get_users():
    try:
        u = kv_get('users')
    except Exception:
        # Indisponibilidade temporária: master em memória, SEM gravar (não destrói nada).
        return {MASTER_USER: {'pwhash': generate_password_hash(MASTER_PASS),
                              'role': 'master', 'nome': 'Master'}}
    if isinstance(u, dict) and u:
        return u
    return _seed_users()


def storage_status():
    """Verifica, de verdade, se a persistência está ativa (grava e relê uma sonda)."""
    if _PG:
        try:
            probe = 'p' + str(int(time.time()))
            kv_set('_persist_probe', probe)
            if kv_get('_persist_probe') == probe:
                return ('ok', 'PostgreSQL conectado — usuários e dados são PERSISTENTES. '
                              'O que você cadastrar NÃO some em deploy/reinício.')
            return ('err', 'PostgreSQL configurado, mas a gravação/leitura de teste FALHOU. '
                           'Não cadastre usuários até verificar com a TI.')
        except Exception as e:
            return ('err', 'PostgreSQL configurado, porém SEM RESPOSTA agora (%s). '
                           'Aguarde normalizar antes de cadastrar.' % type(e).__name__)
    return ('warn', 'Armazenamento em DISCO EFÊMERO — o que for cadastrado pode SUMIR no próximo '
                    'deploy. Motivo: ' + (_PG_OFF_REASON or 'PostgreSQL não configurado') + '.')


if _PG:
    try:
        _pg_init()
    except Exception:
        pass
else:
    print("[N1 APS/PCP] ATENCAO: rodando em DISCO EFEMERO. Motivo: %s. "
          "Em containers efemeros (Railway) usuarios/dados NAO persistem entre deploys."
          % (_PG_OFF_REASON or 'PostgreSQL nao configurado'), flush=True)

# ---- resolucao do SECRET_KEY (evita chave-padrao conhecida em producao) ----
if not SECRET_KEY:
    try:
        _sk = kv_get('_secret_key')
        if not _sk:
            _sk = base64.b64encode(os.urandom(48)).decode('ascii')
            kv_set('_secret_key', _sk)
        SECRET_KEY = _sk
        print("[N1 APS/PCP] SECRET_KEY nao definida no ambiente: usando chave aleatoria "
              "persistida no KV (sessoes estaveis e nao-forjaveis).", flush=True)
    except Exception:
        SECRET_KEY = _DEFAULT_SECRET
        print("[N1 APS/PCP] AVISO CRITICO: SECRET_KEY ausente e KV indisponivel — usando "
              "chave-padrao TEMPORARIA. Sessoes podem ser forjaveis. Defina SECRET_KEY "
              "nas Variables do Railway.", flush=True)
app.secret_key = SECRET_KEY

_seed_users()

# ------------------------------------------------------------------ helpers
def current_user():
    u = session.get('user')
    if not u:
        return None
    users = get_users()
    if u not in users:
        return None
    info = users[u]
    return {'user': u, 'nome': info.get('nome', u), 'role': info.get('role', 'consulta')}


def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        if not current_user():
            return redirect(url_for('login'))
        return fn(*a, **k)
    return wrap


def role_required(*roles):
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            cu = current_user()
            if not cu:
                return redirect(url_for('login'))
            if cu['role'] not in roles:
                return abort(403)
            return fn(*a, **k)
        return wrap
    return deco


def add_log(action, detail=''):
    cu = current_user()
    entry = {
        'ts': _now_br().strftime('%Y-%m-%d %H:%M:%S'),
        'user': cu['user'] if cu else '?',
        'role': cu['role'] if cu else '?',
        'action': str(action)[:80],
        'detail': str(detail)[:300]
    }

    def _mut(log):
        if not isinstance(log, list):
            log = []
        log.append(entry)
        if len(log) > LOG_CAP:
            log = log[-LOG_CAP:]
        return log
    try:
        kv_mutate('audit_log', _mut, [])
    except Exception:
        # log nunca deve derrubar a requisicao do usuario
        pass


# ------------------------------------------------------------ user activity
# Registro de último acesso e tempo de navegação por usuário (blob user_activity).
def _now_br():
    # Brasil sem horário de verão desde 2019 -> UTC-3 fixo (Sertanópolis/PR, Itaju/SP)
    return datetime.datetime.utcnow() - datetime.timedelta(hours=3)


def _now_br_str():
    return _now_br().strftime('%d/%m/%Y %H:%M')


def get_activity():
    try:
        a = kv_get('user_activity')
    except Exception:
        a = None
    return a if isinstance(a, dict) else {}


def fmt_dur(secs):
    s = int(secs or 0)
    if s <= 0:
        return '\u2014'
    h = s // 3600
    m = (s % 3600) // 60
    if h:
        return '%dh %02dmin' % (h, m)
    if m:
        return '%dmin' % m
    return '<1min'


def touch_login(u):
    def _mut(a):
        if not isinstance(a, dict):
            a = {}
        rec = a.get(u, {})
        rec['last_login'] = _now_br_str()
        rec['last_seen'] = rec['last_login']
        rec['last_seen_epoch'] = time.time()
        rec['sessions'] = int(rec.get('sessions', 0)) + 1
        rec.setdefault('total_secs', 0)
        a[u] = rec
        return a
    try:
        kv_mutate('user_activity', _mut, {})
    except Exception:
        pass


def add_nav_time(u, secs):
    # Soma tempo de navegação (cap por ping para evitar lixo de abas ociosas/fechadas).
    try:
        secs = int(secs)
    except Exception:
        return
    secs = max(0, min(secs, 300))
    if not secs:
        return

    def _mut(a):
        if not isinstance(a, dict):
            a = {}
        rec = a.get(u, {})
        rec['total_secs'] = int(rec.get('total_secs', 0)) + secs
        rec['last_seen'] = _now_br_str()
        rec['last_seen_epoch'] = time.time()
        rec.setdefault('sessions', rec.get('sessions', 0))
        rec.setdefault('last_login', rec.get('last_seen'))
        a[u] = rec
        return a
    try:
        kv_mutate('user_activity', _mut, {})
    except Exception:
        pass


# ------------------------------------------------------------------ anti brute-force (login)
LOGIN_MAX = 6           # tentativas falhas antes de bloquear
LOGIN_WINDOW = 300      # janela (s) em que as tentativas contam
LOGIN_COOLDOWN = 300    # tempo (s) de bloqueio apos estourar


def _login_gate(u):
    """(ok, espera_seg): ok=False se o usuario esta em cooldown por excesso de falhas."""
    if not u:
        return True, 0
    try:
        d = kv_get('login_fails') or {}
    except Exception:
        return True, 0           # KV indisponivel: nao bloqueia (nao trava acesso legitimo)
    rec = d.get(u) or {}
    bu = rec.get('blocked_until', 0)
    now = time.time()
    if bu and now < bu:
        return False, int(bu - now)
    return True, 0


def _login_fail(u):
    if not u:
        return

    def _mut(d):
        if not isinstance(d, dict):
            d = {}
        now = time.time()
        rec = d.get(u) or {}
        ts = [t for t in rec.get('ts', []) if now - t < LOGIN_WINDOW]
        ts.append(now)
        rec['ts'] = ts[-20:]
        if len(ts) >= LOGIN_MAX:
            rec['blocked_until'] = now + LOGIN_COOLDOWN
            rec['ts'] = []
        d[u] = rec
        return d
    try:
        kv_mutate('login_fails', _mut, {})
    except Exception:
        pass


def _login_ok(u):
    """Login bem-sucedido: zera o contador de falhas do usuario."""
    def _mut(d):
        if isinstance(d, dict) and u in d:
            del d[u]
        return d if isinstance(d, dict) else {}
    try:
        kv_mutate('login_fails', _mut, {})
    except Exception:
        pass


# ------------------------------------------------------------------ pages
LOGIN_HTML = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>N1 APS/PCP - Acesso</title>
<style>
*{box-sizing:border-box;font-family:Calibri,Arial,sans-serif}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:linear-gradient(135deg,#008D67,#41C097)}
.card{background:#fff;padding:34px 30px;border-radius:14px;width:340px;
box-shadow:0 14px 40px rgba(0,0,0,.25)}
h1{margin:0 0 4px;color:#008D67;font-size:22px}
p.sub{margin:0 0 20px;color:#777;font-size:13px}
label{display:block;font-size:12px;color:#555;margin:12px 0 4px;font-weight:600}
input{width:100%;padding:10px 12px;border:1px solid #ccc;border-radius:8px;font-size:14px}
input:focus{outline:none;border-color:#008D67}
button{width:100%;margin-top:20px;padding:11px;background:#008D67;color:#fff;border:0;
border-radius:8px;font-size:15px;font-weight:700;cursor:pointer}
button:hover{background:#007355}
.err{background:#FDECEC;color:#DC3545;padding:9px 12px;border-radius:8px;font-size:13px;margin-top:14px}
</style></head><body>
<div class="card">
<h1>N1 APS/PCP</h1><p class="sub">Planejamento de Producao</p>
__ERR__
<form method="post">
<label>Usuario</label><input name="user" autofocus autocomplete="username">
<label>Senha</label><input name="pass" type="password" autocomplete="current-password">
<button type="submit">Entrar</button>
</form>
</div></body></html>"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    err = ''
    if request.method == 'POST':
        u = (request.form.get('user') or '').strip()
        p = request.form.get('pass') or ''
        ok_gate, wait = _login_gate(u)
        if not ok_gate:
            add_log('login_bloqueado', u)
            err = ('<div class="err">Muitas tentativas. Tente novamente em %d min.</div>'
                   % max(1, (wait + 59) // 60))
        else:
            users = get_users()
            info = users.get(u)
            if info and check_password_hash(info.get('pwhash', ''), p):
                _login_ok(u)
                session['user'] = u
                add_log('login', '')
                touch_login(u)
                return redirect(url_for('index'))
            _login_fail(u)
            err = '<div class="err">Usuario ou senha invalidos.</div>'
    return Response(LOGIN_HTML.replace('__ERR__', err), mimetype='text/html')


@app.route('/logout')
def logout():
    if current_user():
        add_log('logout', '')
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    cu = current_user()
    with open(INDEX, 'r', encoding='utf-8') as f:
        html = f.read()
    inner = json.dumps({'nome': cu['nome'], 'role': cu['role'], 'user': cu['user']},
                       ensure_ascii=False)
    # escapa para caber dentro de aspas duplas num literal JS: "...__N1_USER_JSON__..."
    escaped = inner.replace('\\', '\\\\').replace('"', '\\"')
    html = html.replace('__N1_USER_JSON__', escaped)
    resp = Response(html, mimetype='text/html')
    # HTML nunca deve ser cacheado: as Novidades e o app inteiro ficam embutidos aqui,
    # entao um deploy novo precisa chegar ao navegador imediatamente (sem versao velha em cache).
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/version')
def version():
    # confirma qual build esta no ar (util contra deploy/cache antigo do Railway)
    return jsonify({'version': APP_VERSION})


# ------------------------------------------------------------------ state API
@app.route('/api/state', methods=['GET', 'POST'])
@login_required
def api_state():
    cu = current_user()
    if request.method == 'GET':
        blob = kv_get('aps_state') or {'rev': -1, 'data': None}
        return jsonify(blob)
    # POST -> salvar (consulta nao pode)
    if cu['role'] == 'consulta':
        return jsonify({'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    data = body.get('data')
    base = body.get('base', None)
    allow_empty = bool(body.get('allowEmpty'))
    # Controle de versão (optimistic lock) ATOMICO: a checagem da base e o incremento
    # do rev ocorrem sob trava, entao duas gravacoes simultaneas com a MESMA base nao
    # se sobrescrevem — a segunda recebe 409 e faz o merge.
    holder = {'conflict': None, 'blocked': None, 'prev_n': 0, 'new_n': 0}

    def _mut(cur):
        if not isinstance(cur, dict):
            cur = {'rev': -1, 'data': None}
        cur_rev = cur.get('rev', -1)
        if isinstance(base, int) and base != cur_rev:
            holder['conflict'] = {'rev': cur_rev, 'data': cur.get('data')}
            return cur  # nao altera nada
        prev_n = _plan_naloc(_plan_of(cur.get('data')))
        new_n = _plan_naloc(_plan_of(data))
        holder['prev_n'] = prev_n
        holder['new_n'] = new_n
        holder['prev_data'] = cur.get('data') if prev_n > 0 else None
        # PROTECAO ANTI-ZERAGEM: nunca troca um plano COM alocacoes por um plano VAZIO
        # sem autorizacao explicita (botao "Limpar" manda allowEmpty). Fecha a porta para
        # perda silenciosa por aba desatualizada, falha de carga ou auto-save indevido.
        if prev_n > 0 and new_n == 0 and not allow_empty:
            holder['blocked'] = {'error': 'empty_overwrite', 'rev': cur_rev,
                                 'data': cur.get('data'), 'nAloc': prev_n}
            return cur
        return {'rev': cur_rev + 1, 'data': data}
    saved = kv_mutate('aps_state', _mut, {'rev': -1, 'data': None})
    if holder['conflict'] is not None:
        return jsonify(holder['conflict']), 409
    if holder['blocked'] is not None:
        try:
            add_log('plano_zeragem_bloqueada',
                    'gravacao de plano VAZIO recusada (plano atual tem %d alocacoes)'
                    % holder['prev_n'])
        except Exception:
            pass
        return jsonify(holder['blocked']), 409
    # rede de seguranca + rastro de auditoria de quedas relevantes
    try:
        # PRESERVA O PLANO ANTERIOR antes de ele ser substituido por outra semana
        # (ex.: "Carregar no Planejador") ou por um plano bem menor.
        prev_plan = _plan_of(holder.get('prev_data'))
        if prev_plan is not None:
            new_plan = _plan_of(data) or {}
            trocou_semana = str(prev_plan.get('planIni') or '') != str(new_plan.get('planIni') or '')
            encolheu = holder['new_n'] < (holder['prev_n'] * 0.9)
            if trocou_semana or encolheu:
                _backup_plan(prev_plan, holder['prev_n'], saved['rev'], force=True,
                             nota='guardado automaticamente antes de ser substituído')
                add_log('plano_substituido',
                        'semana %s (%d alocacoes) substituida por %s (%d alocacoes) — retrato guardado'
                        % (prev_plan.get('planIni'), holder['prev_n'],
                           new_plan.get('planIni'), holder['new_n']))
    except Exception:
        pass
    try:
        _plan_backup(saved, holder['new_n'])
    except Exception:
        pass
    try:
        pn, nn = holder['prev_n'], holder['new_n']
        if pn > 0 and nn == 0:
            add_log('plano_zerado', 'plano limpo (%d alocacoes -> 0)' % pn)
        elif pn >= 10 and nn < (pn * 0.5):
            add_log('plano_reduzido', 'alocacoes %d -> %d' % (pn, nn))
    except Exception:
        pass
    return jsonify({'rev': saved['rev']})


@app.route('/api/state/backups', methods=['GET', 'POST'])
@login_required
def api_state_backups():
    """Retratos automaticos do plano (rede de seguranca contra perda).

    GET  -> lista os retratos (sem o conteudo, so o resumo).
    POST -> {idx} restaura o retrato indicado como plano vivo (PRO/master).
    """
    cu = current_user() or {}
    b = kv_get('aps_state_bkp') or {'itens': []}
    itens = b.get('itens') if isinstance(b, dict) else []
    if not isinstance(itens, list):
        itens = []
    if request.method == 'GET':
        meta = [{'i': i, 'ts': it.get('ts'), 'user': it.get('user'), 'nome': it.get('nome'),
                 'rev': it.get('rev'), 'nAloc': it.get('nAloc'),
                 'planIni': it.get('planIni'), 'planFim': it.get('planFim')}
                for i, it in enumerate(itens) if isinstance(it, dict)]
        meta.reverse()   # mais recente primeiro
        return jsonify({'itens': meta})
    if cu.get('role') not in ('operador_pro', 'master'):
        return jsonify({'ok': False, 'error': 'perm'}), 403
    body = request.get_json(silent=True) or {}
    ts = str(body.get('ts') or '').strip()
    idx = None
    if ts:
        for i, it in enumerate(itens):
            if isinstance(it, dict) and str(it.get('ts')) == ts:
                idx = i
    if idx is None:
        try:
            idx = int(body.get('idx'))
        except Exception:
            return jsonify({'ok': False, 'error': 'idx'}), 400
    if idx < 0 or idx >= len(itens) or not isinstance(itens[idx], dict):
        return jsonify({'ok': False, 'error': 'idx'}), 400
    plan = itens[idx].get('state')
    if not isinstance(plan, dict):
        return jsonify({'ok': False, 'error': 'vazio'}), 400
    # guarda o plano ATUAL antes de substitui-lo (restaurar nunca pode destruir nada)
    try:
        _blob0, plan0 = _aps_plan()
        n0 = _plan_naloc(plan0)
        if n0 > 0:
            _backup_plan(plan0, n0, _blob0.get('rev'), force=True,
                         nota='guardado automaticamente antes de restaurar outro retrato')
    except Exception:
        pass

    def _mut(cur):
        if not isinstance(cur, dict):
            cur = {'rev': -1, 'data': None}
        d = cur.get('data') if isinstance(cur.get('data'), dict) else {}
        novo = {'state': json.loads(json.dumps(plan)),
                'seq': d.get('seq', 1), 'data': d.get('data')}
        return {'rev': cur.get('rev', -1) + 1, 'data': novo}
    saved = kv_mutate('aps_state', _mut, {'rev': -1, 'data': None})
    try:
        add_log('plano_restaurado_backup',
                'retrato de %s (%s alocacoes) restaurado como plano vivo'
                % (itens[idx].get('ts'), itens[idx].get('nAloc')))
    except Exception:
        pass
    return jsonify({'ok': True, 'rev': saved['rev'], 'nAloc': itens[idx].get('nAloc')})


@app.route('/diag')
@login_required
def diag():
    """Diagnostico rapido: persistencia, plano vivo, semanas e retratos disponiveis."""
    cu = current_user() or {}
    if cu.get('role') != 'master':
        return jsonify({'error': 'forbidden'}), 403
    st, msg = storage_status()
    try:
        blob, plan = _aps_plan()
    except Exception as e:
        return jsonify({'version': APP_VERSION, 'armazenamento': {'status': st, 'mensagem': msg},
                        'erro_leitura_plano': str(e)})
    try:
        w = _weeks_get()
    except Exception:
        w = {'current': None, 'weeks': {}}
    semanas = []
    for k, v in sorted((w.get('weeks') or {}).items()):
        semanas.append({'key': k, 'ini': v.get('ini'), 'fim': v.get('fim'),
                        'status': v.get('status'), 'frozenAt': v.get('frozenAt'),
                        'retrato_alocacoes': _plan_naloc(v.get('snapshot'))})
    bk = kv_get('aps_state_bkp') or {'itens': []}
    bks = [{'ts': it.get('ts'), 'user': it.get('user'), 'nAloc': it.get('nAloc'),
            'planIni': it.get('planIni')}
           for it in (bk.get('itens') or []) if isinstance(it, dict)]
    bks.reverse()
    return jsonify({
        'version': APP_VERSION,
        'agora_brasilia': _now_br().strftime('%Y-%m-%d %H:%M:%S'),
        'armazenamento': {'status': st, 'mensagem': msg},
        'plano_vivo': {'rev': blob.get('rev'), 'alocacoes': _plan_naloc(plan),
                       'planIni': (plan or {}).get('planIni'),
                       'planFim': (plan or {}).get('planFim'),
                       'congelado': bool((plan or {}).get('frozen'))},
        'semana_corrente': w.get('current'),
        'semanas': semanas,
        'retratos_automaticos': bks,
    })


# ------------------------------------------------------------------ lock API
LOCK_LEASE = 90  # segundos


@app.route('/api/lock', methods=['POST'])
@login_required
def api_lock():
    cu = current_user()
    if not cu or cu['role'] == 'consulta':
        return jsonify({'ok': False, 'reason': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    act = body.get('act', 'acquire')
    ped = str(body.get('ped', '')).strip()
    now = int(time.time())
    locks = kv_get('aps_locks') or {}
    # remove travas expiradas (lease vencido)
    locks = {k: v for k, v in locks.items()
             if isinstance(v, dict) and (now - int(v.get('ts', 0))) < LOCK_LEASE}
    me = cu['user']
    if act == 'list':
        kv_set('aps_locks', locks)
        return jsonify({'ok': True, 'locks': locks})
    if not ped:
        return jsonify({'ok': False, 'reason': 'no_ped'}), 400
    cur = locks.get(ped)
    if act == 'release':
        if cur and cur.get('by') == me:
            del locks[ped]
            kv_set('aps_locks', locks)
        return jsonify({'ok': True})
    if act in ('acquire', 'refresh'):
        if cur and cur.get('by') != me:
            return jsonify({'ok': False, 'by': cur.get('by'),
                            'nome': cur.get('nome', cur.get('by')), 'ts': cur.get('ts')})
        locks[ped] = {'by': me, 'nome': cu.get('nome', me), 'ts': now}
        kv_set('aps_locks', locks)
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'reason': 'bad_act'}), 400


# ------------------------------------------------------------------ log API
# ------------------------------------------------------------ Semanas de Producao (versionamento)
# Cada planejamento semanal (seg-sex, chave ISO) fica REGISTRADO e IMUTAVEL: edita-se apenas a
# semana corrente; ao congelar tira-se um retrato permanente. Nada e apagado. Todos os perfis leem.
# ENCAIXE (base deste chat): o plano vivo fica em aps_state['data']['state'] (o realState) — o
# cliente grava JSON {state, seq, data:{pedidos,linhas,compat}} em 'data'. Retrato/zeragem operam
# sobre ['data']['state']. Escritas usam kv_mutate (atomico), preservando a robustez do sistema.
def _parse_ymd(s):
    try:
        return datetime.datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _iso_week_key(d):
    y, w, _ = d.isocalendar()
    return '%04d-W%02d' % (y, w)


def _week_bounds(d):
    mon = d - datetime.timedelta(days=d.weekday())
    return mon, mon + datetime.timedelta(days=4)


def _upcoming_monday(d):
    wd = d.weekday()          # 0=seg ... 6=dom
    if wd >= 4:               # sex/sab/dom -> proxima semana
        return d + datetime.timedelta(days=(7 - wd))
    return d - datetime.timedelta(days=wd)


def _weeks_default():
    return {'rev': 0, 'current': None, 'weeks': {}}


def _weeks_get():
    w = kv_get('aps_weeks')
    if not isinstance(w, dict) or 'weeks' not in w:
        w = _weeks_default()
    return w


def _aps_plan():
    # (blob, plano) — plano = realState em blob['data']['state']
    blob = kv_get('aps_state') or {'rev': -1, 'data': None}
    d = blob.get('data') if isinstance(blob, dict) else None
    plan = d.get('state') if isinstance(d, dict) else None
    return blob, plan


def _plan_of(payload):
    """Extrai o plano (realState) de um payload {state, seq, data}."""
    if isinstance(payload, dict):
        s = payload.get('state')
        if isinstance(s, dict):
            return s
    return None


def _plan_naloc(plan):
    """Numero de alocacoes do plano (0 se vazio/ausente)."""
    try:
        a = (plan or {}).get('aloc')
        return len(a) if isinstance(a, list) else 0
    except Exception:
        return 0


_BKP_LOCK = threading.Lock()
_BKP_LAST = [0.0]          # ultimo backup gravado por este processo (epoch)
_BKP_MIN_GAP = 60          # nunca mais de 1 gravacao de retrato por minuto
_BKP_MAX_BYTES = 1500000   # nao guarda retrato absurdamente grande
_BKP_KEEP = 10             # quantidade de retratos mantidos


def _backup_plan(plan, n, rev, force=False, nota=''):
    """Guarda um retrato do plano informado. `force=True` ignora o limite de frequencia
    (usado para preservar o plano ANTERIOR antes de ele ser substituido)."""
    if not isinstance(plan, dict) or n <= 0:
        return
    if not force:
        agora = time.time()
        with _BKP_LOCK:
            if (agora - _BKP_LAST[0]) < _BKP_MIN_GAP:
                return
            _BKP_LAST[0] = agora
    cu = current_user() or {}
    try:
        payload = json.loads(json.dumps(plan))
        if len(json.dumps(payload, ensure_ascii=False)) > _BKP_MAX_BYTES:
            return
    except Exception:
        return
    nowdt = _now_br()
    now = nowdt.strftime('%Y-%m-%d %H:%M:%S')
    usr, nome = cu.get('user', '?'), cu.get('nome', '')

    def _worker():
        def _mut(b):
            if not isinstance(b, dict) or not isinstance(b.get('itens'), list):
                b = {'itens': []}
            itens = b['itens']
            last = itens[-1] if itens else None
            if last and str(last.get('ts')) == now and int(last.get('nAloc', -1)) == int(n) \
               and str(last.get('planIni') or '') == str(payload.get('planIni') or ''):
                return b       # mesmo retrato ja gravado neste segundo
            if (not force) and last and int(last.get('nAloc', -1)) == int(n):
                try:
                    dt = datetime.datetime.strptime(last.get('ts', ''), '%Y-%m-%d %H:%M:%S')
                    if (nowdt - dt).total_seconds() < 900:
                        return b       # mesma situacao ha pouco: nao duplica
                except Exception:
                    return b
            itens.append({'ts': now, 'user': usr, 'nome': nome, 'rev': rev,
                          'nAloc': int(n), 'planIni': payload.get('planIni'),
                          'planFim': payload.get('planFim'), 'nota': nota,
                          'state': payload})
            b['itens'] = itens[-_BKP_KEEP:]
            return b
        try:
            kv_mutate('aps_state_bkp', _mut, {'itens': []})
        except Exception:
            pass
    try:
        if force:
            _worker()          # preservacao critica: grava na hora, sem correr risco
        else:
            threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass


def _plan_backup(saved, n):
    """Retrato rotativo de rotina (em segundo plano, no maximo 1 por minuto)."""
    if not isinstance(saved, dict):
        return
    _backup_plan(_plan_of(saved.get('data')), n, saved.get('rev'))


def _week_diary(acao, detalhe=''):
    # anexa lancamento append-only ao diario da semana CORRENTE (atomico)
    try:
        cu = current_user() or {}

        def _mut(w):
            if not isinstance(w, dict) or 'weeks' not in w:
                w = _weeks_default()
            cur = w.get('current')
            if not cur or cur not in w['weeks']:
                return w
            w['weeks'][cur].setdefault('diary', []).append({
                'ts': _now_br_str(), 'user': cu.get('user', '?'),
                'nome': cu.get('nome', cu.get('user', '?')), 'role': cu.get('role', '?'),
                'acao': str(acao)[:60], 'detalhe': str(detalhe)[:300],
            })
            w['rev'] = int(w.get('rev', 0)) + 1
            return w
        kv_mutate('aps_weeks', _mut, _weeks_default())
    except Exception:
        pass


@app.route('/api/weeks', methods=['GET', 'POST'])
@login_required
def api_weeks():
    cu = current_user()
    if request.method == 'GET':
        return jsonify(_weeks_get())
    body = request.get_json(force=True, silent=True) or {}
    act = body.get('act')
    now = _now_br_str()

    if act == 'init':
        # ADOCAO idempotente: vincula o planejamento em andamento a uma semana, sem recriar nada.
        _blob, plan = _aps_plan()
        planini = (plan or {}).get('planIni') if isinstance(plan, dict) else None
        d = _parse_ymd(planini) or _upcoming_monday(_now_br().date())
        mon, fri = _week_bounds(d)
        key = _iso_week_key(mon)

        def _mut(w):
            if not isinstance(w, dict) or 'weeks' not in w:
                w = _weeks_default()
            # garante que a semana-alvo (a do plano carregado, ou a de hoje) exista no registro
            if key not in w['weeks']:
                w['weeks'][key] = {
                    'key': key, 'ini': mon.strftime('%Y-%m-%d'), 'fim': fri.strftime('%Y-%m-%d'),
                    'status': 'aberta', 'criado': now,
                    'criadoPor': {'user': '(migracao)', 'nome': 'Planejamento em andamento', 'role': ''},
                    'frozenBy': None, 'frozenAt': None, 'snapshot': None,
                    'diary': [{'ts': now, 'user': cu.get('user', '?'), 'nome': cu.get('nome', ''),
                               'role': cu.get('role', ''), 'acao': 'Semana adotada',
                               'detalhe': 'Planejamento em andamento vinculado a esta semana (' + key + ')'}],
                }
            cur = w.get('current')
            cur_ini = _parse_ymd(w['weeks'][cur]['ini']) if (cur and cur in w['weeks']) else None
            # Reconcilia o ponteiro "semana atual": se nao ha corrente, ou se a corrente esta A FRENTE
            # da semana-alvo (ponteiro avancou cedo demais — ex.: "Iniciar nova semana" no meio da
            # semana em curso), traz de volta para a semana-alvo. Semanas com retrato (historico)
            # NUNCA sao apagadas; removemos apenas placeholders futuros vazios e auto-criados.
            if (not cur) or (cur not in w['weeks']) or (cur_ini and cur_ini > mon):
                # NUNCA apaga semana alguma: apenas move o ponteiro da semana corrente.
                w['current'] = key
                w['rev'] = int(w.get('rev', 0)) + 1
            return w
        w2 = kv_mutate('aps_weeks', _mut, _weeks_default())
        if w2.get('current') == key:
            add_log('semana_adotada', key)
        return jsonify(_weeks_get())

    if act == 'close_open':
        if cu.get('role') != 'operador_pro':
            return jsonify({'ok': False, 'error': 'perm'}), 403
        w0 = _weeks_get()
        cur = w0.get('current')
        if not cur or cur not in w0['weeks']:
            return jsonify({'ok': False, 'error': 'sem_semana'}), 400
        motivos = body.get('motivos') or {}
        # retrato do plano vivo (realState em aps_state.data.state)
        _blob, plan = _aps_plan()
        try:
            snap_plan = json.loads(json.dumps(plan)) if plan is not None else None
        except Exception:
            snap_plan = plan

        holder = {'nkey': None, 'nmon': None, 'nfri': None}

        def _mutW(w):
            if not isinstance(w, dict) or 'weeks' not in w:
                w = _weeks_default()
            if cur not in w['weeks']:
                return w
            wk = w['weeks'][cur]
            wk['status'] = 'congelada'
            wk['frozenBy'] = {'user': cu.get('user'), 'nome': cu.get('nome'), 'role': cu.get('role')}
            wk['frozenAt'] = now
            wk['snapshot'] = snap_plan
            wk.setdefault('diary', []).append({'ts': now, 'user': cu.get('user'), 'nome': cu.get('nome'),
                                               'role': cu.get('role'), 'acao': 'Semana congelada',
                                               'detalhe': 'Retrato do planejamento salvo'})
            if motivos:
                wk['ociosidade'] = motivos
                for chave, m in motivos.items():
                    wk['diary'].append({'ts': now, 'user': cu.get('user'), 'nome': cu.get('nome'),
                                        'role': cu.get('role'), 'acao': 'Ociosidade planejada',
                                        'detalhe': str(chave) + ' - ocupada ' + str(m.get('pct', '?')) + '% - motivo: ' + str(m.get('motivo', ''))[:200]})
            fim_cur = _parse_ymd(wk.get('fim')) or _now_br().date()
            nmon = fim_cur + datetime.timedelta(days=3)
            nmon, nfri = _week_bounds(nmon)
            nkey = _iso_week_key(nmon)
            holder['nkey'] = nkey
            holder['nmon'] = nmon.strftime('%Y-%m-%d')
            holder['nfri'] = nfri.strftime('%Y-%m-%d')
            if nkey not in w['weeks']:
                w['weeks'][nkey] = {
                    'key': nkey, 'ini': holder['nmon'], 'fim': holder['nfri'],
                    'status': 'aberta', 'criado': now,
                    'criadoPor': {'user': cu.get('user'), 'nome': cu.get('nome'), 'role': cu.get('role')},
                    'frozenBy': None, 'frozenAt': None, 'snapshot': None,
                    'diary': [{'ts': now, 'user': cu.get('user'), 'nome': cu.get('nome'), 'role': cu.get('role'),
                               'acao': 'Semana aberta', 'detalhe': 'Base de pedidos integra; programacao zerada'}],
                }
            w['current'] = nkey
            w['rev'] = int(w.get('rev', 0)) + 1
            return w
        kv_mutate('aps_weeks', _mutW, _weeks_default())
        nkey = holder['nkey']

        # zera a PROGRAMACAO do plano vivo (aps_state.data.state), mantendo capBase/estoque/pedidos
        def _mutS(cb):
            if not isinstance(cb, dict):
                cb = {'rev': -1, 'data': None}
            d = cb.get('data')
            if not isinstance(d, dict) or not isinstance(d.get('state'), dict):
                return cb   # sem plano vivo -> nada a zerar
            st = d['state']
            st['aloc'] = []
            st['avulsas'] = []
            st['apont'] = {}
            st['capOv'] = {}
            st['frozen'] = False
            st['frozenBy'] = None
            if holder['nmon']:
                st['planIni'] = holder['nmon']
                st['planFim'] = holder['nfri']
            st['weekKey'] = nkey
            cb['rev'] = int(cb.get('rev', -1)) + 1
            return cb
        kv_mutate('aps_state', _mutS, {'rev': -1, 'data': None})

        add_log('semana_fechada_aberta', cur + ' -> ' + str(nkey))
        return jsonify(_weeks_get())

    if act == 'set_current':
        # Define uma semana existente como a CORRENTE (semana atual) e a reabre para edição.
        # Usado por "Carregar no Planejador": corrige o caso de o ponteiro ter avancado cedo demais.
        if cu.get('role') != 'operador_pro':
            return jsonify({'ok': False, 'error': 'perm'}), 403
        key = str(body.get('key') or '')
        w0 = _weeks_get()
        if not key or key not in w0.get('weeks', {}):
            return jsonify({'ok': False, 'error': 'sem_semana'}), 400

        def _mutSC(w):
            if not isinstance(w, dict) or 'weeks' not in w:
                w = _weeks_default()
            if key not in w['weeks']:
                return w
            alvo = w['weeks'][key]
            alvo_ini = _parse_ymd(alvo.get('ini'))
            alvo['status'] = 'aberta'
            alvo.setdefault('diary', []).append({
                'ts': now, 'user': cu.get('user'), 'nome': cu.get('nome'), 'role': cu.get('role'),
                'acao': 'Semana reaberta como atual',
                'detalhe': 'Definida como semana corrente e carregada no Planejador'})
            # NUNCA apaga semana alguma (nem placeholders): apenas define a corrente.
            w['current'] = key
            w['rev'] = int(w.get('rev', 0)) + 1
            return w
        kv_mutate('aps_weeks', _mutSC, _weeks_default())
        add_log('semana_definida_atual', key)
        return jsonify(_weeks_get())

    return jsonify({'ok': False, 'error': 'act'}), 400


@app.route('/api/log', methods=['POST'])
@login_required
def api_log():
    body = request.get_json(silent=True) or {}
    action = body.get('action', '')
    detail = body.get('detail', '')
    add_log(action, detail)
    # rastreabilidade por planejamento: o PRO alimenta o diario da semana corrente
    cu = current_user()
    if cu and cu.get('role') == 'operador_pro':
        _week_diary(action, detail)
    return jsonify({'ok': True})


# ------------------------------------------------------------ Central de Solicitacoes (Comunicacao)
# Canal formal de solicitacoes por pedido: Comercial/Logistica/Adm/Gerencia/Master abrem,
# o Operador PRO (PCP) responde. Tudo carimbado pela sessao (data/hora/usuario), append-only.
@app.route('/api/comms', methods=['GET', 'POST'])
def api_comms():
    cu = current_user()
    if not cu:
        return jsonify({'ok': False, 'error': 'auth'}), 401
    comms = kv_get('comms')
    if not isinstance(comms, dict):
        comms = {'rev': 0, 'threads': []}
    if request.method == 'GET':
        return jsonify(comms)
    body = request.get_json(force=True, silent=True) or {}
    act = body.get('act')
    ts = _now_br_str()

    def stamp(text):
        return {'ts': ts, 'user': cu['user'], 'nome': cu.get('nome', cu['user']),
                'role': cu['role'], 'text': str(text or '')[:2000]}

    # checagem de permissao que NAO depende do estado (feita antes da mutacao):
    # status so pode ser alterado por operador_pro/master (gerente NAO resolve).
    if act == 'status' and cu['role'] not in ('operador_pro', 'master'):
        return jsonify({'ok': False, 'error': 'perm'}), 403

    holder = {'err': None, 'code': 200, 'log': None}

    def _mut(comms):
        if not isinstance(comms, dict):
            comms = {'rev': 0, 'threads': []}
        threads = comms.get('threads', [])
        if act == 'new':
            tid = 'C' + datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')
            th = {
                'id': tid,
                'ped': str(body.get('ped', ''))[:40],
                'cliente': str(body.get('cliente', ''))[:120],
                'setor': str(body.get('setor', ''))[:40],
                'tipo': str(body.get('tipo', ''))[:60],
                'urgencia': str(body.get('urgencia', 'Normal'))[:20],
                'status': 'aberta',
                'criado': ts,
                'por': {'user': cu['user'], 'nome': cu.get('nome', cu['user']), 'role': cu['role']},
                'msgs': [stamp(body.get('mensagem'))],
            }
            threads.insert(0, th)
            holder['log'] = ('comm_nova', th['ped'] + ' | ' + th['tipo'] + ' | ' + th['urgencia'])
        elif act == 'reply':
            tid = body.get('id')
            th = next((t for t in threads if t.get('id') == tid), None)
            if not th:
                holder['err'] = 'thread'; holder['code'] = 404; return comms
            owner = (th.get('por') or {}).get('user')
            if cu['role'] not in ('operador_pro', 'master', 'gerente') and cu['user'] != owner:
                holder['err'] = 'perm'; holder['code'] = 403; return comms
            th.setdefault('msgs', []).append(stamp(body.get('mensagem')))
            if cu['role'] in ('operador_pro', 'master', 'gerente') and th.get('status') == 'aberta':
                th['status'] = 'respondida'
            holder['log'] = ('comm_resposta', th.get('ped', ''))
        elif act == 'status':
            tid = body.get('id')
            st = str(body.get('status', ''))[:20]
            th = next((t for t in threads if t.get('id') == tid), None)
            if not th:
                holder['err'] = 'thread'; holder['code'] = 404; return comms
            if st in ('aberta', 'respondida', 'resolvida'):
                th['status'] = st
                holder['log'] = ('comm_status', th.get('ped', '') + ' -> ' + st)
        else:
            holder['err'] = 'act'; holder['code'] = 400; return comms
        comms['threads'] = threads[:500]
        comms['rev'] = int(comms.get('rev', 0)) + 1
        return comms

    try:
        new_comms = kv_mutate('comms', _mut, {'rev': 0, 'threads': []})
    except Exception:
        return jsonify({'ok': False, 'error': 'kv'}), 503
    if holder['err']:
        return jsonify({'ok': False, 'error': holder['err']}), holder['code']
    if holder['log']:
        add_log(holder['log'][0], holder['log'][1])
    return jsonify(new_comms)


# Heartbeat: o SPA envia, de tempos em tempos, os segundos navegados (aba visível).
@app.route('/api/ping', methods=['POST'])
@login_required
def api_ping():
    cu = current_user()
    body = request.get_json(silent=True) or {}
    add_nav_time(cu['user'], body.get('secs', 0))
    return jsonify({'ok': True})


@app.route('/logs', methods=['GET', 'POST'])
@role_required('master')
def logs_view():
    if request.method == 'POST' and request.form.get('act') == 'clear':
        kv_set('audit_log', [])
        add_log('log_clear', '')
        return redirect(url_for('logs_view'))
    log = kv_get('audit_log') or []
    rows = ''.join(
        "<tr><td>{ts}</td><td>{user}</td><td>{role}</td><td>{action}</td><td>{detail}</td></tr>"
        .format(ts=e.get('ts',''), user=e.get('user',''), role=e.get('role',''),
                action=e.get('action',''), detail=(e.get('detail','') or '').replace('<','&lt;'))
        for e in reversed(log[-1000:]))
    page = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>Log - N1 APS/PCP</title><style>
*{font-family:Calibri,Arial,sans-serif}body{margin:0;padding:20px;background:#f4f6f5}
h1{color:#008D67;font-size:20px;margin:0 0 4px}
a{color:#008D67;text-decoration:none;font-weight:600}
.bar{display:flex;gap:14px;align-items:center;margin-bottom:14px}
.clear{color:#DC3545;background:none;border:0;font-family:inherit;font-size:inherit;font-weight:600;cursor:pointer;padding:0}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
box-shadow:0 2px 8px rgba(0,0,0,.08)}
th,td{padding:7px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{background:#008D67;color:#fff;position:sticky;top:0}
tr:hover td{background:#f0faf6}
</style></head><body>
<div class="bar"><h1>Log de Auditoria</h1>
<a href="/">&larr; Voltar</a>
<form method="post" style="margin:0;display:inline" onsubmit="return confirm('Limpar todo o log?')"><input type="hidden" name="act" value="clear"><button class="clear" type="submit">Limpar log</button></form>
<span style="color:#777;font-size:12px">__N__ eventos</span></div>
<table><thead><tr><th>Data/Hora</th><th>Usuario</th><th>Perfil</th><th>Acao</th><th>Detalhe</th></tr></thead>
<tbody>__ROWS__</tbody></table></body></html>"""
    page = page.replace('__ROWS__', rows or '<tr><td colspan="5" style="color:#999">Sem eventos.</td></tr>')
    page = page.replace('__N__', str(len(log)))
    return Response(page, mimetype='text/html')


# ------------------------------------------------------------------ users API
@app.route('/usuarios', methods=['GET', 'POST'])
@role_required('master')
def usuarios_view():
    msg = ''
    users = get_users()
    if request.method == 'POST':
        act = request.form.get('act')
        if act == 'add':
            u = (request.form.get('user') or '').strip()
            nome = (request.form.get('nome') or u).strip()
            role = request.form.get('role') or 'consulta'
            pw = request.form.get('pass') or ''
            if not u or not pw:
                msg = 'Informe usuario e senha.'
            elif role not in ROLES:
                msg = 'Perfil invalido.'
            elif u in users:
                msg = 'Usuario ja existe.'
            else:
                users[u] = {'pwhash': generate_password_hash(pw), 'role': role, 'nome': nome}
                kv_set('users', users); add_log('user_add', u + ' (' + role + ')')
                msg = 'Usuario ' + u + ' criado.'
        elif act == 'reset':
            u = request.form.get('user'); pw = request.form.get('pass') or ''
            if u in users and pw:
                users[u]['pwhash'] = generate_password_hash(pw)
                kv_set('users', users); add_log('user_reset', u)
                msg = 'Senha de ' + u + ' redefinida.'
        elif act == 'role':
            u = request.form.get('user'); role = request.form.get('role')
            if u in users and role in ROLES:
                users[u]['role'] = role
                kv_set('users', users); add_log('user_role', u + ' -> ' + role)
                msg = 'Perfil de ' + u + ' alterado.'
        elif act == 'del':
            u = request.form.get('user')
            if u in users and u != session.get('user') and users[u]['role'] != 'master':
                del users[u]; kv_set('users', users); add_log('user_del', u)
                msg = 'Usuario ' + u + ' removido.'
            else:
                msg = 'Nao e possivel remover este usuario.'
        users = get_users()

    opt = lambda sel: ''.join(
        '<option value="{r}"{s}>{lbl}</option>'.format(
            r=r, s=' selected' if r == sel else '', lbl=ROLE_LABELS.get(r, r))
        for r in ROLES)
    act = get_activity()
    rows = ''
    for u, info in sorted(users.items()):
        _ac = act.get(u, {})
        _ses = int(_ac.get('sessions', 0) or 0)
        _ult = _ac.get('last_login') or '\u2014'
        if _ses:
            _ult += ('<br><span style="color:#999;font-size:11px">%d acesso%s</span>'
                     % (_ses, '' if _ses == 1 else 's'))
        _tmp = fmt_dur(_ac.get('total_secs', 0))
        rows += """<tr><td>{u}</td><td>{nome}</td>
<td><form method="post" style="display:flex;gap:6px">
<input type="hidden" name="act" value="role"><input type="hidden" name="user" value="{u}">
<select name="role">{ro}</select><button>OK</button></form></td>
<td style="white-space:nowrap">{ult}</td><td style="white-space:nowrap;font-weight:600">{tmp}</td>
<td><form method="post" style="display:flex;gap:6px">
<input type="hidden" name="act" value="reset"><input type="hidden" name="user" value="{u}">
<input name="pass" type="password" placeholder="nova senha"><button type="button" class="pwtog" onclick="tpw(this)">ver</button><button>Redefinir</button></form></td>
<td><form method="post" onsubmit="return confirm('Remover {u}?')">
<input type="hidden" name="act" value="del"><input type="hidden" name="user" value="{u}">
<button class="del">Remover</button></form></td></tr>""".format(
            u=u, nome=info.get('nome', ''), ro=opt(info.get('role', 'consulta')),
            ult=_ult, tmp=_tmp)

    page = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>Usuarios - N1 APS/PCP</title><style>
*{font-family:Calibri,Arial,sans-serif}body{margin:0;padding:20px;background:#f4f6f5}
h1{color:#008D67;font-size:20px;margin:0 0 4px}h2{font-size:15px;color:#444;margin:24px 0 8px}
a{color:#008D67;text-decoration:none;font-weight:600}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
box-shadow:0 2px 8px rgba(0,0,0,.08)}
th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left}
th{background:#008D67;color:#fff}
input,select{padding:6px 8px;border:1px solid #ccc;border-radius:6px;font-size:13px}
button{padding:6px 10px;background:#008D67;color:#fff;border:0;border-radius:6px;cursor:pointer;font-weight:600}
button.del{background:#DC3545}
button.pwtog{background:#E6EFEC;color:#0A6B43;border:1px solid #BfDfD3;font-weight:600;white-space:nowrap}
.msg{background:#E8F8F1;color:#008D67;padding:9px 12px;border-radius:8px;margin:12px 0;font-size:13px}
.add{background:#fff;padding:16px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.08);
display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.add label{display:flex;flex-direction:column;font-size:12px;color:#555;gap:4px;font-weight:600}
</style></head><body>
<h1>Gestao de Usuarios</h1><a href="/">&larr; Voltar</a>
__STATUS__
__MSG__
<h2>Adicionar usuario</h2>
<form method="post" class="add">
<input type="hidden" name="act" value="add">
<label>Usuario<input name="user"></label>
<label>Nome<input name="nome"></label>
<label>Perfil<select name="role">__OPT__</select></label>
<label>Senha<span style="display:flex;gap:6px;align-items:stretch"><input name="pass" type="password" style="flex:1"><button type="button" class="pwtog" onclick="tpw(this)">ver</button></span></label>
<button type="submit">Criar</button>
</form>
<h2>Usuarios existentes</h2>
<table><thead><tr><th>Usuario</th><th>Nome</th><th>Perfil</th><th>&Uacute;ltimo acesso</th><th>Tempo navegando</th><th>Redefinir senha</th><th>Acao</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<script>
function tpw(b){var i=b.parentNode.querySelector('input[name=pass]');if(!i)return;
 var hidden=(i.type==='password');i.type=hidden?'text':'password';b.textContent=hidden?'ocultar':'ver';}
</script>
</body></html>"""
    page = page.replace('__MSG__', '<div class="msg">' + msg + '</div>' if msg else '')
    st_kind, st_text = storage_status()
    st_col = {'ok': ('#E8F8F1', '#0A6B43', '#0A6B43'),
              'warn': ('#FFF4E5', '#8A5300', '#FFB300'),
              'err': ('#FDECEA', '#B71C1C', '#DC3545')}[st_kind]
    st_icon = {'ok': '&#9989;', 'warn': '&#9888;', 'err': '&#9940;'}[st_kind]
    st_html = ('<div style="background:%s;color:%s;border-left:5px solid %s;'
               'padding:10px 14px;border-radius:8px;margin:12px 0;font-size:13px;font-weight:600">'
               '%s Persistência: %s</div>') % (st_col[0], st_col[1], st_col[2], st_icon, st_text)
    page = page.replace('__STATUS__', st_html)
    page = page.replace('__OPT__', opt('consulta'))
    page = page.replace('__ROWS__', rows)
    return Response(page, mimetype='text/html')


@app.errorhandler(403)
def forbidden(e):
    return Response('<h2 style="font-family:Arial;color:#DC3545">403 - Sem permissao</h2>'
                    '<a href="/">Voltar</a>', status=403, mimetype='text/html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
