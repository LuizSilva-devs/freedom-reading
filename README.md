<p align="center"><img src="frontend/img/logo-full.png" alt="Freadom_Reading — Mais livros. Mais liberdade." width="280"></p>

# Freadom Reading — versão fullstack

Cole um trecho de um livro e descubra de qual obra ele é. Depois, leia a obra (domínio público) direto no navegador, com biblioteca, favoritos e progresso salvos na sua conta.

Esta é a versão fullstack pessoal, construída sobre o frontend estático do grupo e sobre a mesma stack do backend do Projeto Integrador (FastAPI + PostgreSQL/pg_trgm).

> **Projeto acadêmico**, sem fins comerciais. Desenvolvido para a disciplina **Projeto Integrador** do curso de
> **Engenharia de Software** do **Centro Universitário Dom Bosco (AEDB)**, Resende – RJ.
>
> **Autor:** Luiz Felipe Nunes Alves da Silva
>
> Existe também uma versão desenvolvida em grupo para a mesma disciplina. Este repositório é a versão
> individual e fullstack (backend, banco de dados, contas de usuário e leitor).

## Funcionalidades

- **Identificação por trecho**: busca por similaridade de trigramas (`pg_trgm` + índice GIN), tolerante a erros de digitação, acentos ausentes e ortografia antiga.
- **Leitor** de obras em domínio público, com páginas, marcadores, sublinhados coloridos com nota, tamanho de fonte e papel.
- **Tradução automática** de livros em inglês ao ler com o site em português (e vice-versa), com cache no banco.
- **Busca de livros** no catálogo da Open Library, com detalhes e capa.
- **Biblioteca**: favoritos, "continuar lendo" e progresso.
- **Conta**: cadastro, confirmação de e-mail, "esqueci minha senha", troca de senha, sair de todos os dispositivos e exclusão da conta. Sem conta, tudo funciona no navegador e é importado ao entrar.
- Interface em **português e inglês**.

## Tecnologias

| Camada | Tecnologias |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2, Pydantic v2, bcrypt, JWT |
| Banco | PostgreSQL 14+ com a extensão `pg_trgm` |
| Frontend | HTML, CSS e JavaScript puro (módulos ES), sem framework |
| Testes e qualidade | pytest (67 testes), ruff, bandit, pip-audit, ESLint, Schemathesis, Playwright, axe-core |
| Infra | Docker / docker-compose, AWS (EC2 + RDS) |

```
freedom-reading/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI + serve o frontend
│   │   ├── config.py          # variáveis do .env
│   │   ├── database.py        # engine, sessão, criação de tabelas + índice GIN
│   │   ├── models.py          # Book, Excerpt, User, UserBook, SearchLog
│   │   ├── schemas.py         # validação (Pydantic)
│   │   ├── security.py        # bcrypt + JWT
│   │   ├── i18n.py            # mensagens da API em PT/EN (Accept-Language)
│   │   ├── routers/           # auth, identify, catalog, me, annotations
│   │   └── services/          # matching (pg_trgm), gutenberg, openlibrary, translation, text_utils, cache
│   ├── scripts/
│   │   ├── ingest_gutenberg.py  # baixa e indexa livros no acervo
│   │   └── test_matching.py     # calibra o SIMILARITY_THRESHOLD
│   └── tests/                 # pytest (67 testes) + amostras offline
├── frontend/
│   ├── index.html
│   ├── css/styles.css
│   └── js/  api.js · store.js · ui.js · i18n.js · app.js
├── docker-compose.yml
└── Dockerfile
```

## Rodando localmente

Pré-requisitos: Python 3.11+ e Docker (ou um PostgreSQL 14+ instalado).

```bash
# 1. Banco
docker compose up -d db

# 2. Backend
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # troque o JWT_SECRET

# 3. Acervo de identificação (baixa do Project Gutenberg)
python -m scripts.ingest_gutenberg              # Dom Casmurro, Brás Cubas, Alice, Pride and Prejudice
python -m scripts.ingest_gutenberg 2701 84      # adicionar outros por ID

# 4. Subir
uvicorn app.main:app --reload
```

Abra **http://localhost:8000** (app) e **http://localhost:8000/docs** (Swagger da API).

### No Windows, sem Docker

Com Python 3.11+ e PostgreSQL instalados, crie o usuário e o banco no **SQL Shell (psql)**, conectado como `postgres`:

```sql
CREATE ROLE freedom LOGIN PASSWORD 'freedom';
CREATE DATABASE freedom OWNER freedom;
\c freedom
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Depois, no PowerShell, dentro de `backend`:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # só na primeira vez, se o activate for bloqueado
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # e cole no JWT_SECRET a saída de: python -c "import secrets; print(secrets.token_urlsafe(48))"
python -m scripts.ingest_gutenberg
uvicorn app.main:app --reload
```

Tudo em containers: `docker compose up --build` (a ingestão roda com `docker compose exec api python -m scripts.ingest_gutenberg`).

### Testes

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://freedom:freedom@localhost:5432/freedom_test pytest -q
```

O `docker-compose` já cria o banco `freedom_test`. Os testes usam amostras em `tests/fixtures`, então não precisam de internet.

### Verificações de qualidade

```bash
cd backend
pip install ruff pip-audit schemathesis
ruff check app scripts tests          # análise estática
pip-audit -r requirements.txt         # dependências com falhas conhecidas
# com a API rodando: manda centenas de requisições aleatórias procurando erro 500
st run http://localhost:8000/openapi.json --checks not_a_server_error -H "Authorization: Bearer <token>"
```

### Calibrar o threshold

```bash
python -m scripts.test_matching --samples 60
```

Sorteia trechos do acervo, degrada (sem acentos, erros de digitação, cortes) e mostra acerto por threshold. Ajuste `SIMILARITY_THRESHOLD` no `.env`.

## Como a identificação funciona

1. **Ingestão**: o `.txt` do Gutenberg perde o cabeçalho/licença e é dividido em trechos do tamanho de parágrafos (diálogos curtos são agrupados; parágrafos gigantes, quebrados por frase). Cada trecho é salvo com o texto original e uma versão **normalizada** (minúsculas, sem acento, sem pontuação), indexada com **GIN + `gin_trgm_ops`**.
2. **Busca**: o trecho do usuário passa pela mesma normalização e é comparado com `word_similarity` (operador `<%`). Diferente de `similarity`, ele compara com a *melhor sub-extensão* do parágrafo, então um pedaço curto dentro de um parágrafo longo ainda pontua alto.
3. **Trechos longos** são quebrados em janelas (o usuário pode ter copiado algo que atravessa dois parágrafos) e o resultado é agregado por livro.
4. O limite do pg_trgm é uma variável de **sessão**, então é definido com `set_config` na mesma conexão, logo antes da consulta — o mesmo cuidado do bug corrigido no backend do grupo.
5. A resposta traz o livro, a confiança, o trecho encontrado e **a página do leitor onde ele está**, para o botão "Ler a partir deste trecho".

Cada identificação fica registrada em `search_logs` (tamanho do trecho, score, threshold) — dá para calibrar com dados reais de uso.

## API

| Método | Rota | Descrição |
|---|---|---|
| POST | `/api/identify` | Identifica o livro de um trecho |
| GET | `/api/books` | Livros do acervo de identificação |
| GET | `/api/search?q=&mode=&language=` | Acervo + Open Library (`mode`: text, phrase, description; `language`: pt, en) |
| GET | `/api/details?key=` | Detalhes (`/works/OL…W` ou `gutenberg:<id>`) + versão gratuita |
| GET | `/api/reader/{gutenberg_id}?page=&lang=` | Página do livro; com `lang=pt\|en`, traduzida se o livro for de outro idioma |
| POST | `/api/auth/register` · `/api/auth/login` | Conta (JWT) |
| GET/PATCH | `/api/auth/me` | Usuário logado / renomear |
| GET | `/api/me/books` | Favoritos, biblioteca e progresso |
| PUT/DELETE | `/api/me/favorites` · `/api/me/library` | Favoritar / status (quero_ler, lendo, concluido) |
| PUT | `/api/me/progress` | Salva a página (marca "lendo"/"concluído" sozinho) |
| GET/PUT | `/api/me/settings` | Aparência e leitura |
| GET | `/api/me/stats` | Números do perfil |
| GET | `/api/me/annotations?book_key=` | Marcadores e sublinhados (de um livro ou todos) |
| PUT / DELETE | `/api/me/bookmarks` · `/api/me/bookmarks/{id}` | Marcar página (com anotação opcional) / remover |
| POST / PATCH / DELETE | `/api/me/highlights` · `/api/me/highlights/{id}` | Sublinhar trecho (4 cores) / mudar cor / remover |
| POST | `/api/me/import` | Migra os dados do modo visitante no primeiro login |
| GET | `/api/health` | Health check (EC2/ALB) |

## Idioma e tradução

- **Idioma do app** (Português / English): no seletor PT · EN da barra lateral ou em Configurações. Na primeira visita, segue o idioma do navegador. A API também responde as mensagens de erro no idioma escolhido (cabeçalho `Accept-Language`).
- **Tradução automática no leitor**: se o livro está em outro idioma, cada página é traduzida quando você chega nela, e a próxima já é pré-traduzida em segundo plano. O botão "Original" / "Traduzir" alterna na hora. Dá para desligar em Configurações.
- Cada página traduzida fica salva na tabela `page_translations` e é reaproveitada por todo mundo: o custo e a espera acontecem uma vez só.
- Provedor em `TRANSLATION_PROVIDER` no `.env`:
  - `mymemory` (padrão): grátis e sem chave, mas com limite diário. Sem e-mail, são uns 5 mil caracteres por dia, o que dá poucas páginas. Preencha `TRANSLATION_EMAIL` para subir para 50 mil.
  - `aws`: Amazon Translate. No EC2 usa o LabRole, sem chave no código. Precisa de `pip install boto3`. Confira no Learner Lab se o Translate está liberado para sua conta.
  - `libretranslate`: servidor próprio (dá para subir num container).
- Sublinhados guardam em qual texto foram feitos (original ou tradução) e aparecem só naquele texto, porque as posições dos caracteres são diferentes.

## Marcadores e sublinhados

- **Marcar página**: botão de marcador na barra do leitor. Cada marcador aceita uma anotação curta.
- **Sublinhar**: selecione um trecho no leitor e escolha a cor. Clique num sublinhado para trocar a cor ou remover.
- **Painel de anotações** no leitor (lista e navegação), no fim da página de detalhes do livro e na aba "Anotações" da biblioteca.
- A **tela inicial** mostra Continue lendo, Seus favoritos, Na sua biblioteca e Seus destaques.
- Visitante: fica no navegador e é importado ao criar conta ou entrar.

## O que mudou em relação à versão estática

**Arquitetura**
- Chamadas à Open Library, Gutendex e Gutenberg saíram do navegador e foram para o backend (cache, formato padronizado). O leitor não depende mais do proxy público `allorigins`.
- Favoritos, biblioteca, progresso e configurações saem do `localStorage` e vão para o PostgreSQL, por usuário. Sem conta, o app continua funcionando como antes (modo visitante), e tudo é importado ao criar a conta. Dados da versão estática antiga são migrados automaticamente.
- JS separado em módulos, com eventos delegados (`data-action`) em vez de `onclick` com JSON dentro de atributos HTML.
- Rotas por hash (`#/livro?key=…`, `#/ler/55752?p=3`): o botão Voltar do navegador funciona e links podem ser compartilhados.

**Funcionalidades**
- A busca por trecho (o objetivo do projeto) virou o centro da tela inicial, com resultado, nível de confiança, palavras em destaque e "ler a partir deste trecho".
- Foto de uma página agora tenta identificar o trecho; foto de capa busca pelo título.
- Status na biblioteca (Quero ler / Lendo / Concluído) editável nos detalhes.
- Leitor com modos de papel (branco, sépia, noite), setas do teclado e pré-carregamento da próxima página.

**Correções**
- "Aparência" (Automático/Claro/Escuro) e "Espaçamento entre linhas" existiam nas configurações mas não faziam nada.
- O botão Voltar dos detalhes sempre levava para a busca, mesmo vindo da Início.
- Alterar configuração mostrava o toast "Progresso salvo".
- Tesseract.js carregava em toda visita; agora só quando a busca por foto é usada.

**Design** — mesma linha visual (tons de livro, temas por gênero), com: Literata (fonte feita para leitura longa) + Instrument Sans na interface; caixa de trecho que imita uma página pautada; capas tipográficas geradas quando a Open Library não tem capa (em vez do 📕); ícones SVG; skeletons de carregamento; foco visível, `prefers-reduced-motion` e layout testado em 390px.

## Conta

- **Cadastro e login** com e-mail e senha (bcrypt + token JWT de 7 dias).
- **Confirmação de e-mail**: ao se cadastrar, a pessoa recebe um link. Dá para usar o app antes de confirmar; o perfil mostra um aviso e um botão para reenviar o e-mail.
- **Esqueci minha senha**: link no login → e-mail com link de uso único que vale 60 minutos → nova senha. Todos os aparelhos saem da conta e a pessoa já entra. A resposta é sempre a mesma, exista ou não a conta, então ninguém descobre quem está cadastrado.
- **Trocar senha** (em Perfil): pede a senha atual. Os outros aparelhos saem e links de redefinição pendentes deixam de valer.
- **Sair de todos os outros aparelhos**: cada token carrega uma "versão". Aumentar a versão invalida os tokens antigos, e este aparelho recebe um token novo.
- **Excluir conta**: pede a senha e apaga a conta com todos os dados (livros, marcadores, sublinhados, links pendentes).
- Os links dos e-mails usam `#/…?token=`: a parte depois do `#` nunca chega aos logs de servidor, e o app tira o token do endereço assim que a tela abre. O banco guarda só o hash SHA-256 dos tokens.

### Configurar o envio de e-mail

| `EMAIL_PROVIDER` | Quando usar |
|---|---|
| `console` (padrão) | Desenvolvimento e apresentação: o e-mail com o link aparece no terminal onde a API está rodando. |
| `smtp` | Qualquer servidor SMTP: Gmail com [senha de app](https://myaccount.google.com/apppasswords) (`smtp.gmail.com:587`), Outlook, Mailtrap ou o SMTP do Amazon SES. |
| `ses` | API do Amazon SES pelo LabRole (precisa de `pip install boto3`). No modo sandbox do SES, só envia para endereços verificados. Confira se o SES está liberado no seu Learner Lab. |

Em produção, defina também `APP_URL` com o endereço público do site. Se ele ficar como `localhost`, os links dos e-mails não funcionam fora da máquina, e a API avisa no log ao iniciar.

## Segurança

- Senhas com bcrypt; login com token JWT (7 dias).
- **Limite de tentativas por IP**: 10 logins errados em 5 minutos e 20 cadastros por hora (`LOGIN_ATTEMPTS_PER_5MIN`, `REGISTRATIONS_PER_HOUR`). O bloqueio vale mesmo se a senha certa for digitada durante ele.
- Sem `JWT_SECRET` no `.env`, a API usa uma chave aleatória temporária em vez da chave de exemplo (que é pública). Nesse caso os logins expiram quando o servidor reinicia, então **defina `JWT_SECRET` em produção**.
- No `docker-compose`, o PostgreSQL só escuta em `127.0.0.1`, e o container da API roda sem root.

## Deploy na AWS (Learner Lab)

Mesma arquitetura do projeto do grupo: **EC2 (t3.micro) + RDS PostgreSQL (db.t3.micro, single-AZ)**.

1. No RDS, crie o banco e rode uma vez `CREATE EXTENSION pg_trgm;` (a API também tenta criar ao subir).
2. No EC2 (Amazon Linux, LabInstanceProfile, key `vockey`): instale Python/git, clone o projeto, configure `backend/.env` com o endpoint do RDS e um `JWT_SECRET` forte.
3. `python -m scripts.ingest_gutenberg` e `uvicorn app.main:app --host 0.0.0.0 --port 8000` (ou `docker compose up --build` com a `DATABASE_URL` do RDS).
4. Security groups: EC2 aceita 80/8000 da internet; RDS aceita 5432 **só do security group do EC2**.

Como o Learner Lab desliga ao fim da sessão, o IP público do EC2 muda — use um Elastic IP se precisar de um endereço fixo para a apresentação.

## Créditos e licença

- Textos das obras: [Project Gutenberg](https://www.gutenberg.org) (obras em domínio público). O cabeçalho e a licença do Gutenberg são removidos na ingestão, e os textos baixados não fazem parte deste repositório.
- Catálogo, capas e detalhes: [Open Library](https://openlibrary.org) · busca de versões gratuitas: [Gutendex](https://gutendex.com).
- Tradução: [MyMemory](https://mymemory.translated.net) (padrão), LibreTranslate ou Amazon Translate.
- O código deste repositório está sob a licença [MIT](LICENSE).

