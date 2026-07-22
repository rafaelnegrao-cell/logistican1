# N1 Alimentos — Formador de Carga (Flask + PostgreSQL)

Backend migrado de Node para **Flask + PostgreSQL**. O frontend (`index.html`) e as regras
de negocio **nao mudaram** — apenas a camada de dados, que agora vive no PostgreSQL
(duravel, compartilhado entre todos os usuarios e a prova de deploy).

## Arquivos do projeto
- `index.html` — o app (frontend), inalterado.
- `app.py` — servidor Flask: serve o app e expoe a API de dados no Postgres.
- `requirements.txt` — dependencias Python.
- `Procfile` — comando de start (gunicorn).
- `.gitignore`

## API (identica a versao Node)
- `GET /api/ping` -> `{ok:true}`
- `GET /api/all` -> `{auth:{rev,updatedAt,data}, app:{...}, audit:{...}}`
- `GET /api/rev` -> revisoes atuais
- `GET /api/<auth|app|audit>` -> `{rev, updatedAt, data}`
- `PUT /api/<auth|app|audit>` body `{data:...}` -> grava e incrementa `rev`

## Passos para subir no Railway (servico logistican1)

1. **No repositorio do GitHub, REMOVA os arquivos do Node:**
   - `server.js`
   - `package.json`
   - `package-lock.json`
   - `node_modules/` (se existir)

   E **ADICIONE** os arquivos novos: `app.py`, `requirements.txt`, `Procfile`, `.gitignore`.
   Mantenha o `index.html`. (Isso e essencial: enquanto houver `package.json`, o Railway
   tenta buildar como Node em vez de Python.)

2. **Variavel de banco:** confirme que `DATABASE_URL` esta definida no servico do app
   (referencia ao Postgres). O `app.py` tambem aceita `DATABASE`, `POSTGRES_URL` ou `PG_URL`.

3. **Primeiro deploy — migracao automatica dos dados:** mantenha o **Volume atual ainda anexado**
   neste primeiro deploy. No boot, o `app.py` le `auth.json` / `app.json` / `audit.json` do Volume
   (via `DATA_DIR` ou `RAILWAY_VOLUME_MOUNT_PATH`) e importa **uma unica vez** para o Postgres,
   sem sobrescrever nada que ja exista no banco. Assim nenhum dado se perde.

4. O Railway detecta `requirements.txt` (Python) e inicia pelo `Procfile` (gunicorn).

5. **Verificacao:** abra `https://logistican1-production.up.railway.app/api/ping` (deve responder
   `{"ok":true}`). Abra o app, faca login e confirme que os dados aparecem. Faça um pequeno deploy
   de teste depois e confirme que os dados continuam (agora vem do Postgres).

6. Depois de confirmar que os dados migraram, o Volume passa a ser opcional (a verdade dos dados
   esta no Postgres). Nao apague o Volume antes de confirmar a migracao.

## Rodar localmente (opcional)
```
pip install -r requirements.txt
export DATABASE_URL=postgresql://usuario:senha@localhost:5432/n1log
python app.py
```
