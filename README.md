# N1 Alimentos — Formador de Carga

Aplicativo de página única (um único `index.html`, **offline**, sem CDN) para montagem de cargas,
roteirização, frete, cadastros, **login com perfis de acesso**, **monitoria** e salvamento automático
no navegador. Acompanha um servidor estático mínimo (`server.js`, sem dependências) para publicação no Railway.

## Conteúdo do repositório

```
n1-formador-carga/
├── index.html        # o aplicativo completo (tudo embutido: lógica, dados geográficos, SheetJS)
├── server.js         # servidor estático Node (sem dependências) — usa process.env.PORT
├── package.json      # define "npm start" -> node server.js
├── .gitignore
└── README.md
```

## 1) Criar o repositório no GitHub

No computador, com Git instalado, dentro desta pasta:

```bash
git init
git add .
git commit -m "N1 Formador de Carga - versao inicial"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/n1-formador-carga.git
git push -u origin main
```

(Crie antes o repositório vazio em github.com → New repository, com o mesmo nome.)

## 2) Publicar no Railway

1. Acesse https://railway.app e faça login (pode usar a conta do GitHub).
2. **New Project → Deploy from GitHub repo** e selecione `n1-formador-carga`.
3. O Railway detecta o `package.json` (Node), roda `npm install` e depois `npm start`
   (que executa `node server.js`). Não há etapa de build.
4. Em **Settings → Networking → Generate Domain** para obter a URL pública (https).
5. Pronto — o sistema abre na URL gerada.

> Observação: o `server.js` usa a porta de `process.env.PORT`, que o Railway define automaticamente.

## 3) Primeiro acesso

- Na primeira abertura, o sistema pede para **criar a conta MASTER** (nome, usuário e senha).
- Depois, todo acesso passa pela tela de **login** com **verificação anti-robô** (código + "Não sou um robô").
- Com o perfil **master**, use a aba **Configurações** para:
  - criar **perfis de acesso** (marcando o que cada perfil pode **ver**, quais **ações** pode fazer,
    e **de quais filiais e estados (UF)** ele pode ver/trabalhar os pedidos);
  - criar **usuários** vinculados a um perfil.
- A aba **Monitoria** mostra acessos (data/hora), tempo de navegação e o **log de ações dos últimos 90 dias**.

## Importante sobre os dados (limitação a conhecer)

Este aplicativo guarda **tudo no navegador** (armazenamento local): pedidos, tabela de frete, cargas,
**usuários, perfis e logs**. Isso significa:

- Os dados ficam **no navegador/computador** onde o sistema é aberto. Não são compartilhados
  automaticamente entre máquinas ou usuários diferentes — cada navegador tem sua própria cópia.
- O login funciona como um **controle de acesso da interface**, adequado para uso interno, mas
  **não é uma autenticação de servidor** (qualquer pessoa com acesso ao navegador e conhecimento
  técnico poderia inspecionar os dados locais). Para login seguro, dados compartilhados entre vários
  usuários/dispositivos e auditoria centralizada, é necessário um **backend com banco de dados**.

Se desejar evoluir para essa arquitetura (backend + banco, login real e dados compartilhados),
isso pode ser construído como próximo passo, mantendo a mesma interface.
