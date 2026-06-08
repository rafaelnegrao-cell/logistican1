# N1 Alimentos — Formador de Carga (dados compartilhados)

Aplicativo de logística (montagem de cargas, roteirização, frete, cadastros, login com perfis,
monitoria) com **dados compartilhados pelo servidor**: todas as pessoas que abrem o **mesmo link**
veem e trabalham sobre os **mesmos dados** (usuários, perfis, pedidos, cargas, tabela de frete,
priorizados e logs). Acompanha um servidor Node sem dependências, pronto para o Railway.

## Conteúdo

```
n1-formador-carga/
├── index.html     # o app (interface completa)
├── server.js      # servidor Node: serve o app + API de dados compartilhados (sem dependências)
├── package.json   # "npm start" -> node server.js
├── .gitignore
└── README.md
```

## Como funciona o compartilhamento

- O servidor guarda os dados em disco (arquivos `auth.json`, `app.json`, `audit.json`) e expõe uma
  API simples (`/api/...`). O app, ao abrir, **carrega do servidor** e **salva no servidor** a cada
  alteração; além disso **sincroniza automaticamente** a cada ~7 segundos, então o que um usuário faz
  aparece para os demais sem precisar recarregar a página.
- O **login e os perfis** também ficam no servidor: o master criado por uma pessoa vale para todos
  no mesmo link.
- Edições simultâneas seguem a regra "última gravação vence" (adequado para uma equipe pequena).

## 1) Subir para o GitHub

```bash
git init
git add .
git commit -m "N1 Formador de Carga - dados compartilhados"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/n1-formador-carga.git
git push -u origin main
```

## 2) Publicar no Railway

1. https://railway.app → **New Project → Deploy from GitHub repo** → selecione o repositório.
2. O Railway detecta o `package.json` (Node) e roda `npm start` (`node server.js`).
3. **Settings → Networking → Generate Domain** para obter a URL pública (https).
4. Abra a URL: no primeiro acesso o sistema pede para **criar a conta master**; depois todos entram
   pelo login, com os mesmos dados.

## 3) Manter os dados entre os deploys (IMPORTANTE)

Por padrão o servidor grava os dados na pasta `data/` do próprio container. No Railway, o disco do
container é **reiniciado a cada novo deploy** — para **não perder os dados** ao atualizar a versão,
adicione um **Volume**:

1. No projeto do Railway → serviço → aba **Variables**: crie `DATA_DIR` com valor `/data`.
2. Aba **Volumes** (ou **Settings → Volumes**) → **New Volume** e monte em **`/data`**.
3. Faça redeploy. A partir daí, `auth.json`, `app.json` e `audit.json` ficam no volume e
   **persistem entre os deploys**.

> Sem o Volume o app funciona e compartilha normalmente, mas os dados podem ser zerados quando o
> Railway recriar o container (novo deploy). Com o Volume, ficam permanentes.

## Segurança e acesso

- Login com senha e verificação anti-robô; sessão com opção "manter conectado" (expira após 24h de
  inatividade).
- Perfis de acesso definem o que cada um **vê** (abas), **faz** (ações) e de **quais filiais/UF**.
- A senha é guardada apenas como **hash** (não há como recuperá-la). Em Configurações há **backup e
  restauração de acessos** e, para o master, a opção de **limpar dados**.
- A aba **Monitoria** mostra acessos, tempo de navegação e o log de ações dos últimos 90 dias.

## Observações

- O servidor não tem dependências externas (`npm install` não baixa nada); só precisa de Node 18+.
- O `index.html` também funciona aberto isoladamente (offline), mas nesse modo os dados ficam apenas
  naquele navegador. O compartilhamento entre pessoas acontece quando o app é servido pelo
  `server.js` (Railway).
