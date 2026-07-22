# N1 APS/PCP

Planejamento de Producao com login, perfis, persistencia compartilhada e log de auditoria.

## Arquivos
- `app.py` — backend Flask (login, 4 perfis, API de estado, log, gestao de usuarios)
- `index.html` — app original + camada de sincronizacao/perfis/log
- `requirements.txt` — dependencias
- `Procfile` — comando de inicializacao (gunicorn)

## Setup no Railway (uma vez)
1. Suba todos os arquivos na RAIZ do repositorio.
2. Adicione o servico **PostgreSQL** (cria `DATABASE_URL` automaticamente).
3. Em Variables defina `SECRET_KEY` (string longa). Opcional: `N1_MASTER_USER`, `N1_MASTER_PASS`.
4. Railway faz deploy via `Procfile` -> `gunicorn app:app`.

> IMPORTANTE: a pagina deve ser servida pela rota `/` do Flask (gunicorn). Abrir o
> `index.html` diretamente NAO substitui o usuario e a app cai em modo "consulta".

## Login inicial
Usuario: `rafael` — Senha: `n1master2026` (troque depois em **Usuarios**).

## Perfis
- master: tudo + Usuarios + Log
- gerente: tudo operacional (sem usuarios/log)
- operador: apontamento de OP e OP avulsa (pos-congelamento)
- consulta: somente leitura

## Notas de seguranca / robustez
- **SECRET_KEY**: se nao definida em Variables, o app gera uma chave aleatoria e a
  persiste no banco (sessoes estaveis e nao-forjaveis). Ainda assim, o recomendado e
  definir `SECRET_KEY` manualmente em producao.
- **Cookie de sessao**: `Secure` + `HttpOnly` + `SameSite=Lax` por padrao. Para testar
  localmente em http (sem HTTPS), defina `N1_INSECURE_COOKIE=1`.
- **Gravacoes concorrentes**: estado do plano, solicitacoes (comms), log e atividade usam
  escrita atomica (transacao com trava no Postgres). Duas acoes simultaneas nao se
  sobrescrevem.
- **Login**: apos 6 tentativas falhas em 5 min, o usuario fica bloqueado por 5 min.
- **Limpar log**: agora e uma acao POST (o antigo link GET `?clear=1` nao apaga mais).
